"""Candidate-blind acoustic discovery for short, plausible-looking cues.

The ordinary exact-final reviewer sees text only. That is sufficient for
nonwords and discourse contradictions, but it cannot discover a fluent ASR
homophone such as a plausible ``好X哦`` phrase with the wrong middle word. This module
opens a deliberately narrow discovery lane for short Mandarin cues:

* the audio witness receives only timing geometry and returns toneless pinyin;
* code compares that pinyin with the current cue pronunciation;
* a material mismatch becomes a proposal-less final-review finding;
* the existing CPA proposal bootstrap and CPA CURRENT/PROPOSED judge still own
  every textual candidate and every mutation.

The witness never sees current/proposed text and this module never edits SRT.
Provider uncertainty is disclosed but does not stall unrelated production.
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping

from src.autoslice.acoustic_pinyin import (
    heard_pinyin_tokens,
    pinyin_similarity,
    text_pinyin_tokens,
)
from src.autoslice.acoustic_witness_adjudication import (
    build_witness_request,
    valid_witness_evidence,
)
from src.autoslice.acoustic_witness_availability import (
    unavailable_acoustic_witness,
)
from src.autoslice.jingting_chunker import parse_srt_cues

SCHEMA_VERSION = "microcue-candidate-blind-acoustic-discovery.v1"
MAX_DURATION_MS = 1_300
MIN_CJK_COUNT = 2
MAX_TEXT_CODEPOINTS = 12
MAX_CUES = 12
MIN_WITNESS_CONFIDENCE = 0.72
MAX_PINYIN_SIMILARITY_FOR_FINDING = 0.80
MAX_SYLLABLE_COUNT_DELTA = 1

# Each eligible microcue's witness request is a self-contained, distinct
# (unique evidence_id/request_sha256) blind-pinyin probe: it reads only the
# immutable parsed-SRT cue geometry, never the previous cue's witness/verdict,
# and does not touch any cross-cue budget or ledger counter (unlike
# ``ContextAdjudicationBudget`` in ``final_review_provider_budget.py``, whose
# ``provider_adjudication_count`` is a shared cap and stays serial).  That
# makes the per-cue loop body embarrassingly parallel; bound the fan-out so a
# pathological ``MAX_CUES`` batch cannot open unbounded concurrent AGY/Gemini
# sessions.  Keep this a plain module constant, matching the
# ``produce_dispatch.py`` ThreadPoolExecutor convention — no new env knob.
MICROCUE_WITNESS_CONCURRENCY = 4

_CJK_RX = re.compile(r"[\u3400-\u9fff]")
_KANA_RX = re.compile(r"[\u3040-\u30ff]")
_LATIN_RX = re.compile(r"[A-Za-z]")
_FILLER_ONLY_RX = re.compile(r"[嗯呃啊哦诶欸哎唉嘿哈呵哼呀嘛呢吧啦]+")


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _eligible(text: str, duration_ms: int) -> bool:
    compact = "".join(text.split())
    return bool(
        0 < duration_ms <= MAX_DURATION_MS
        and len(compact) <= MAX_TEXT_CODEPOINTS
        and len(_CJK_RX.findall(compact)) >= MIN_CJK_COUNT
        and not _KANA_RX.search(compact)
        and not _LATIN_RX.search(compact)
        and not _FILLER_ONLY_RX.fullmatch(compact)
    )


def _witness_one_microcue(
    ordinal: int,
    cue: Any,
    *,
    timeline_offset_ms: int,
    entity_verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run one cue's candidate-blind witness probe and score it.

    Pure per-cue work: reads only ``ordinal``/``cue``/``timeline_offset_ms``
    (all immutable inputs for this call) and calls ``entity_verifier`` once.
    Returns the ``receipt["eligible"]`` row plus an optional finding — never
    mutates any shared collection, so callers may run many of these
    concurrently and simply assemble the return values back in input order.
    """

    check_request = {
        "evidence_id": hashlib.sha256(
            f"microcue:{ordinal}:{cue.start_ms}:{cue.end_ms}".encode("utf-8")
        ).hexdigest(),
        "cue_indexes": [ordinal],
        "matched_start_ms": int(cue.start_ms),
        "matched_end_ms": int(cue.end_ms),
        "context_start_ms": max(0, int(cue.start_ms) - 1_500),
        "context_end_ms": int(cue.end_ms) + 1_500,
        "source_media_timeline_offset_ms": int(timeline_offset_ms),
    }
    witness_request = build_witness_request(check_request)
    try:
        raw_witness = entity_verifier(witness_request)
        # F21：``dict(None)`` 会抛 TypeError 并被下面记成
        # MICROCUE_AUDIO_VERIFIER_ERROR，把「根本没有证人」伪装成
        # 「证人炸了」。非 Mapping（含 None）一律走 typed 不可用尾巴。
        witness = (
            dict(raw_witness)
            if isinstance(raw_witness, Mapping)
            else unavailable_acoustic_witness(witness_request)
        )
    except Exception as exc:
        witness = {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": witness_request["request_sha256"],
            "status": "UNCERTAIN",
            "reason_code": "MICROCUE_AUDIO_VERIFIER_ERROR",
            "error_type": type(exc).__name__,
        }
    valid = valid_witness_evidence(
        witness, request_sha256=str(witness_request["request_sha256"])
    )
    confidence = witness.get("confidence")
    current_tokens = text_pinyin_tokens(cue.text) or []
    heard_tokens = [
        token
        for token in heard_pinyin_tokens(witness.get("heard_pinyin"))
        if token != "?"
    ]
    similarity = pinyin_similarity(
        current_tokens,
        heard_tokens,
        character_level=True,
    )
    observed = bool(
        valid
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is True
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and float(confidence) >= MIN_WITNESS_CONFIDENCE
        and heard_tokens
    )
    inaudible_observed = bool(
        valid
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is False
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and float(confidence) >= MIN_WITNESS_CONFIDENCE
        and not heard_tokens
        and witness.get("syllable_count") == 0
    )
    syllable_count_plausible = bool(
        observed
        and abs(len(current_tokens) - len(heard_tokens))
        <= MAX_SYLLABLE_COUNT_DELTA
    )
    row = {
        "cue_index": ordinal,
        "start_ms": int(cue.start_ms),
        "end_ms": int(cue.end_ms),
        "current_text_sha256": "sha256:"
        + hashlib.sha256(cue.text.encode("utf-8")).hexdigest(),
        "witness_request_sha256": "sha256:"
        + str(witness_request["request_sha256"]).removeprefix("sha256:"),
        "witness": witness,
        "current_pinyin": " ".join(current_tokens),
        "heard_pinyin": " ".join(heard_tokens),
        "pinyin_similarity": round(similarity, 4),
        "status": (
            "INAUDIBLE_OBSERVED"
            if inaudible_observed
            else (
                "OBSERVED"
                if syllable_count_plausible
                else (
                    "SYLLABLE_COUNT_OUTLIER"
                    if observed
                    else "UNCERTAIN"
                )
            )
        ),
    }
    finding: dict[str, Any] | None = None
    if inaudible_observed:
        finding = {
            "cue": ordinal,
            "kind": "context",
            "proposed_full_cue": "",
            "repair_class": "acoustic_drop_cue",
            "source_surface": None,
            "candidate_memory_id": None,
            "evidence_cue_ids": [],
            "suspect": cue.text,
            "replacement": "",
            "why": (
                "候选无关短句声学巡检确认整条目标时窗无可闻语音且音节数为"
                " 0；CPA 随后必须在 CURRENT/PROPOSED/DROP typed 三选一"
                "中明确选择 DROP 才可整 cue 删除"
            ),
        }
        row["status"] = "INAUDIBLE_DROP_PROPOSED_TO_CPA"
        row["finding_sha256"] = "sha256:" + _sha256_json(finding)
    elif not observed or not syllable_count_plausible:
        pass
    elif similarity < MAX_PINYIN_SIMILARITY_FOR_FINDING:
        finding = {
            "cue": ordinal,
            "kind": "context",
            "proposed_full_cue": None,
            "repair_class": "phonetic",
            "source_surface": None,
            "candidate_memory_id": None,
            "evidence_cue_ids": [],
            "suspect": cue.text,
            "why": (
                "候选盲短句声学巡检听得拼音 "
                f"{' '.join(heard_tokens)}，与现稿拼音 "
                f"{' '.join(current_tokens)} 显著不一致；请 CPA 只生成候选，"
                "随后仍需盲听证人与 CPA 闭集裁决"
            ),
        }
        row["status"] = "MISMATCH_PROPOSED_TO_CPA"
        row["finding_sha256"] = "sha256:" + _sha256_json(finding)
    return row, finding


def discover_microcue_findings(
    srt_text: str,
    *,
    timeline_offset_ms: int,
    entity_verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return proposal-less findings plus a hash-bound discovery receipt."""

    srt_sha256 = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "UNAVAILABLE",
        "decision_authority": "NONE_DISCOVERY_ONLY",
        "mutation_authorized": False,
        "candidate_text_exposed_to_witness": False,
        "srt_sha256": "sha256:" + srt_sha256,
        "timeline_offset_ms": int(timeline_offset_ms),
        "eligible": [],
        "findings": [],
    }
    if entity_verifier is None or text_pinyin_tokens("测试") is None:
        receipt["reason_code"] = "MICROCUE_AUDIO_DISCOVERY_UNAVAILABLE"
        return [], receipt

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    eligible = sorted(
        [
        (ordinal, cue)
        for ordinal, cue in enumerate(cues, start=1)
        if _eligible(cue.text, cue.end_ms - cue.start_ms)
        ],
        key=lambda row: (
            row[1].end_ms - row[1].start_ms,
            len(row[1].text),
            row[0],
        ),
    )[:MAX_CUES]
    if not eligible:
        receipt.update(status="PASS", reason_code="NO_ELIGIBLE_MICROCUES")
        return [], receipt

    findings: list[dict[str, Any]] = []
    uncertain_count = 0
    syllable_outlier_count = 0
    # Every eligible cue's witness call is an independent, distinct-request
    # blind probe (see MICROCUE_WITNESS_CONCURRENCY above) — run them with a
    # bounded thread pool, but always assemble ``findings``/``eligible`` back
    # in the original ``eligible`` order so output is byte-for-byte identical
    # to the prior serial loop regardless of completion order.
    workers = min(MICROCUE_WITNESS_CONCURRENCY, len(eligible))
    if workers <= 1:
        results = [
            _witness_one_microcue(
                ordinal,
                cue,
                timeline_offset_ms=timeline_offset_ms,
                entity_verifier=entity_verifier,
            )
            for ordinal, cue in eligible
        ]
    else:
        pool = ThreadPoolExecutor(max_workers=workers)
        futures = [
            pool.submit(
                _witness_one_microcue,
                ordinal,
                cue,
                timeline_offset_ms=timeline_offset_ms,
                entity_verifier=entity_verifier,
            )
            for ordinal, cue in eligible
        ]
        try:
            results = [future.result() for future in futures]
        except BaseException:
            # ``_witness_one_microcue`` already converts every
            # ``entity_verifier`` exception into a typed UNCERTAIN row, so
            # this only fires for a genuine bug elsewhere in the per-cue
            # scoring code.  Cancel any not-yet-started sibling calls so a
            # bug does not spend extra AGY/Gemini calls beyond what the
            # prior serial loop would have made before raising at the same
            # point.  In-flight calls (already running) still complete.
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        pool.shutdown(wait=True)

    for row, finding in results:
        if finding is not None:
            findings.append(finding)
        elif row["status"] == "UNCERTAIN":
            uncertain_count += 1
        elif row["status"] == "SYLLABLE_COUNT_OUTLIER":
            syllable_outlier_count += 1
        receipt["eligible"].append(row)

    receipt["findings"] = [
        {
            "cue_index": int(row["cue"]),
            "finding_sha256": "sha256:" + _sha256_json(row),
        }
        for row in findings
    ]
    receipt["eligible_count"] = len(eligible)
    receipt["observed_count"] = len(eligible) - uncertain_count
    receipt["uncertain_count"] = uncertain_count
    receipt["syllable_outlier_count"] = syllable_outlier_count
    receipt["finding_count"] = len(findings)
    receipt["status"] = "PASS" if uncertain_count == 0 else "PARTIAL"
    return findings, receipt
