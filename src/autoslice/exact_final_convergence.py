"""Hash-bound CPA convergence for repeated exact-final cue rewrites.

The ordinary exact-final judge owns each CURRENT/PROPOSED choice, but each
review pass is intentionally independent.  Without same-run memory, two valid
CPA receipts can therefore reverse one another until the bounded self-heal
budget is exhausted.  This module does not let code or the acoustic witness
choose text.  When an exact cue/time window is reconsidered, it gives CPA the
prior receipt chain and the new adjudication, records CPA's supported choice,
and reuses that choice only while the exact local cue context remains unchanged.
"""

from __future__ import annotations

from src.autoslice.structured_output import call_json

from src.autoslice.final_review_agenda import reviewer_reason

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from src.autoslice.acoustic_witness_adjudication import (
    AUDIO_EVIDENCE_PRIORITY,
    build_witness_request,
    valid_inaudible_drop_authority,
    valid_witness_evidence,
)
from src.autoslice.exact_final_witness_authority import (
    _valid_native_witness,
    binding_from_adjudication,
    convergence_witness_gate,
    downgrade_convergence_finding,
    GATE_SCHEMA,
    valid_convergence_witness_gate,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import extract_json_object
from src.autoslice.exact_final_cpa_history import repair_history


MEMO_SCHEMA = "exact-final-cpa-convergence-memo.v1"
CYCLE_MEMO_SCHEMA = "exact-final-cpa-cycle-memo.v1"
CYCLE_RECEIPT_SCHEMA = "exact-final-cpa-cycle-adjudication.v1"
CACHE_SCHEMA = "exact-final-cpa-convergence-cache.v1"
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")

_PROMPT = AUDIO_EVIDENCE_PRIORITY + """# Exact-final 字幕反复改写收敛（CPA 最终文字裁决）

同一个精确字幕时间窗已经有一条或多条 CPA 授权的修改收据，本轮独立审片又
提出了新的 CURRENT / PROPOSED 二元选择。AGY/Gemini 仅提供声学观察，不能
决定文字。你是最终文字裁决者；证据足够时在 CURRENT 与 PROPOSED 中选择，
证据冲突或不足时保留 UNRESOLVED，不得为了结束反复审查而强行选边。

这次决定会绑定当前 cue、局部语境、精确时间窗、历史收据链和本轮证据哈希；
在这些绑定不变时，后续独立扫描不得再次反转。不要因为“上一轮改过”就机械
保留，也不要为了追求变化选择 PROPOSED；结合全部历史和本轮证据选最终文本。
短窗拼音与已绑定的完整语义块听写冲突时，明确比较覆盖范围与不确定性，不能只因
短窗拼音吻合就忽略完整句证据。宽窗听写也不自动证明当前单 cue 的每个字，不能
多数投票。确需新的原音证据时返回 needs_audio=true；不得把历史次数当作必须选边的理由。

## 绑定
- FINAL_SRT_SHA256: {srt_sha256}
- CUE_INDEX: {cue_index}
- MATCHED_START_MS: {start_ms}
- MATCHED_END_MS: {end_ms}
- CURRENT_CUE_SHA256: {current_sha256}
- PROPOSED_CUE_SHA256: {proposed_sha256}
- LOCAL_CONTEXT_SHA256: {context_sha256}
- HISTORY_SHA256: {history_sha256}
- NEW_EVIDENCE_SHA256: {evidence_sha256}

## 本轮闭集
- CURRENT: {current}
- PROPOSED: {proposed}

## 前后文
前文:
{before}
目标: {current}
后文:
{after}

## 先前 CPA 修改收据链
{history_json}

## 本轮 CPA/声学证据
{evidence_json}

只回 JSON（无 markdown、无其他文字）：
{{"choice":"CURRENT"或"PROPOSED"或"UNRESOLVED","needs_audio":false或true,"reason":"选择依据或具体仍缺的证据"}}
"""

_CYCLE_PROMPT = AUDIO_EVIDENCE_PRIORITY + """# Exact-final 字幕振荡闭集裁决（CPA 最终文字裁决）

同一个精确字幕时间窗在有界 self-heal 中出现了候选回流：本轮候选包含此前
已经被另一条 CPA 修改收据替换掉的文本，或者同轮出现多个互斥提案。普通的
CURRENT / PROPOSED 二选一已不足以收敛。你是最终文字裁决者。

证据足够时只能从下面列出的完整候选闭集中选一个 candidate_id；证据冲突或不足时
返回 UNRESOLVED，确需新原音证据时返回 needs_audio=true，不得为结束振荡强行选边。
比较已绑定短窗与完整语义块的覆盖范围和不确定性，不能丢弃宽窗或按多数投票。
AGY/Gemini 及语义
审片只有 evidence authority，不能替你选字。历史收据、当前文字、本轮全部
合法 finding 与已有声学/语义证据均已给出。不要多数表决，也不要因某文本
较新就偏好它。DROP 只有在候选列表明确包含 LEGAL_DROP 时才合法。

这次裁决绑定精确时间窗、局部语境、候选全集和证据全集；在这些证据没有新增
且最终文字仍匹配时，后续扫描不得把选择翻回旧候选。

## 绑定
- FINAL_SRT_SHA256: {srt_sha256}
- CUE_INDEX: {cue_index}
- MATCHED_START_MS: {start_ms}
- MATCHED_END_MS: {end_ms}
- CURRENT_CUE_SHA256: {current_sha256}
- LOCAL_CONTEXT_SHA256: {context_sha256}
- CANDIDATE_SET_SHA256: {candidate_set_sha256}
- HISTORY_SHA256: {history_sha256}
- CURRENT_EVIDENCE_SHA256: {evidence_sha256}

## 候选闭集
{candidates_json}

## 前后文
前文:
{before}
目标: {current}
后文:
{after}

## 完整已持久化历史修改收据
{history_json}

## 本轮全部 CPA/AGY/语义 finding
{evidence_json}{shared_receipts_section}

只回 JSON（无 markdown、无其他文字）：
{{"choice_id":"候选列表中的 candidate_id 或 UNRESOLVED","needs_audio":false或true,"reason":"选择依据或具体仍缺的证据"}}
"""


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _cycle_prompt_receipts(history: object, evidence: object) -> tuple[str, str, str]:
    """Represent repeated receipts once, without dropping any historical fact."""
    counts: dict[str, int] = {}
    values: dict[str, object] = {}
    sizes: dict[str, int] = {}
    keys: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            keys.update(value)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif not isinstance(value, str):
            return
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = _text_sha256(encoded)
        counts[digest] = counts.get(digest, 0) + 1
        values[digest] = value
        sizes[digest] = len(encoded.encode("utf-8"))

    collect(history)
    collect(evidence)
    shared = {digest for digest, count in counts.items() if count > 1 and sizes[digest] >= 256}
    reference_key = "$receipt_ref"
    while reference_key in keys:
        reference_key += "_"
    table: dict[str, object] = {}
    ids = {digest: "R" + str(index) for index, digest in enumerate(sorted(shared), 1)}

    def render(value: object, *, expand: bool = False) -> object:
        if isinstance(value, (dict, list, str)):
            digest = _json_sha256(value)
            if not expand and digest in shared:
                reference = ids[digest]
                if reference not in table:
                    table[reference] = render(values[digest], expand=True)
                return {reference_key: reference}
            if isinstance(value, dict):
                return {key: render(child) for key, child in value.items()}
            if isinstance(value, list):
                return [render(child) for child in value]
            return value
        return value

    def dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    original_history, original_evidence = dump(history), dump(evidence)
    if not shared:
        return original_history, original_evidence, ""
    rendered_history, rendered_evidence = dump(render(history)), dump(render(evidence))
    section = (
        "\n## 共享收据（无损去重）\n"
        f"仅含 {reference_key} 的对象引用下表同名完整收据；按引用递归展开。"
        "引用没有裁决权，不代表新证据；候选、旧拒绝理由、原观察和哈希均保留。"
        "HISTORY_SHA256/CURRENT_EVIDENCE_SHA256 绑定展开后的原始对象，未改动。\n"
        + dump(table)
    )
    if len(rendered_history) + len(rendered_evidence) + len(section) >= len(original_history) + len(original_evidence):
        return original_history, original_evidence, ""
    return rendered_history, rendered_evidence, section


class _ConvergenceJudgeFailure(RuntimeError):
    def __init__(self, stage: str, exc: Exception, completion_sha256: str | None = None):
        super().__init__(stage)
        self.detail = {
            "stage": stage,
            "exception_type": type(exc).__name__,
            "exception_sha256": _text_sha256(str(exc)),
        }
        if completion_sha256 is not None:
            self.detail["completion_sha256"] = completion_sha256


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _local_context_binding(
    srt_text: str,
    cue_index: int,
    *,
    target_text: str | None = None,
) -> tuple[str, list[str], list[str]]:
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not 1 <= cue_index <= len(cues):
        raise ValueError("CONVERGENCE_CUE_INDEX_INVALID")
    cue = cues[cue_index - 1]
    final_text = cue.text if target_text is None else target_text
    before_rows = cues[max(0, cue_index - 4) : cue_index - 1]
    after_rows = cues[cue_index : min(len(cues), cue_index + 3)]
    before = [row.text for row in before_rows]
    after = [row.text for row in after_rows]
    binding = {
        "cue_index": cue_index,
        "matched_start_ms": cue.start_ms,
        "matched_end_ms": cue.end_ms,
        "target_text": final_text,
        "before": before,
        "after": after,
    }
    return _json_sha256(binding), before, after


def _valid_repair(row: object) -> bool:
    if not isinstance(row, Mapping):
        return False
    before = row.get("before")
    after = row.get("after")
    mutation = row.get("mutation_authority")
    return bool(
        row.get("schema_version") == "exact-final-cpa-self-heal.v1"
        and row.get("decision_authority") == "CPA_JUDGE"
        and row.get("timing_immutable") is True
        and isinstance(before, str)
        and bool(before)
        and isinstance(after, str)
        and bool(after.strip())
        and row.get("before_sha256") == "sha256:" + _text_sha256(before)
        and row.get("after_sha256") == "sha256:" + _text_sha256(after)
        and _valid_sha256(row.get("finding_sha256"))
        and _valid_sha256(row.get("request_sha256"))
        and isinstance(row.get("matched_start_ms"), int)
        and not isinstance(row.get("matched_start_ms"), bool)
        and isinstance(row.get("matched_end_ms"), int)
        and not isinstance(row.get("matched_end_ms"), bool)
        and int(row["matched_start_ms"]) < int(row["matched_end_ms"])
        and isinstance(mutation, Mapping)
        and mutation.get("schema_version") == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
    )


def _repair_histories(
    authority_audit: Mapping[str, object],
) -> dict[tuple[int, int], list[dict[str, Any]]]:
    self_heal = authority_audit.get("exact_final_cpa_self_heal")
    if (not isinstance(self_heal, Mapping)
            or self_heal.get("schema_version") != "exact-final-cpa-self-heal-audit.v1"
            or self_heal.get("status") not in {"REVIEW_PENDING", "PASS"}):
        return {}
    # A later normal review retains completed runs before starting its own
    # passes. Those original, hash-validated receipts still describe prior
    # reversals; omitting them makes a postburn successor forget the cycle.
    repair_history(self_heal)
    runs = [*(entry["audit"] for entry in self_heal.get("retained_previous_runs", [])),
            self_heal]
    passes = [row for run in runs for row in run["passes"]]
    histories: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for pass_receipt in passes if isinstance(passes, list) else []:
        repairs = (
            pass_receipt.get("repairs")
            if isinstance(pass_receipt, Mapping)
            and pass_receipt.get("schema_version") == "exact-final-cpa-self-heal-pass.v1"
            else None
        )
        for raw in repairs if isinstance(repairs, list) else []:
            if not _valid_repair(raw):
                continue
            row = dict(raw)
            key = (int(row["matched_start_ms"]), int(row["matched_end_ms"]))
            history = histories.setdefault(key, [])
            if history and history[-1]["after_sha256"] != row["before_sha256"]:
                # Keep only the latest contiguous, hash-bound chain.  A gap is
                # not proof that the older decision applies to the live cue.
                history.clear()
            history.append(row)
    return histories


def _compact_history(
    history: Iterable[Mapping[str, Any]],
) -> list[dict[str, object]]:
    return [
        {
            "before": row.get("before"),
            "after": row.get("after"),
            "before_sha256": row.get("before_sha256"),
            "after_sha256": row.get("after_sha256"),
            "finding_sha256": row.get("finding_sha256"),
            "request_sha256": row.get("request_sha256"),
            "mutation_basis": (
                row.get("mutation_authority", {}).get("basis")
                if isinstance(row.get("mutation_authority"), Mapping)
                else None
            ),
        }
        for row in history
    ]


def _compact_candidate_blind_transcript_observations(
    adjudication: Mapping[str, Any],
    request: Mapping[str, Any],
) -> list[dict[str, object]]:
    """Carry validated native observations into the convergence prompt.

    ``independent_asr`` keeps one accepted row per provider, while ``verdict``
    is only the observation used by the ordinary judge.  Convergence used to
    project the latter's blind-pinyin-shaped fields and therefore discarded a
    second provider's exact transcript and its crop provenance.  Revalidate the
    ordinary adjudication first, then each retained native row against the
    candidate-free witness request.  The compact projection deliberately omits
    provider response bodies and unrelated context, while keeping enough of
    the native/source timeline and hashes for CPA to compare the evidence.
    """

    primary_binding = binding_from_adjudication(adjudication)
    if primary_binding is None:
        return []
    primary = primary_binding.get("witness")
    witness_request = primary_binding.get("witness_request")
    if not (
        isinstance(primary, Mapping)
        and primary.get("witness_protocol") == "candidate_blind_transcript"
        and isinstance(witness_request, Mapping)
    ):
        return []
    try:
        rebuilt_witness_request = build_witness_request(request)
    except (KeyError, TypeError, ValueError):
        return []
    if dict(witness_request) != rebuilt_witness_request:
        return []

    independent_asr = adjudication.get("independent_asr")
    accepted = (
        independent_asr.get("accepted_observations")
        if isinstance(independent_asr, Mapping)
        else None
    )
    # Older candidate-blind adjudications carried only the ordinary verdict.
    # Keep that already validated observation usable, but never fall back from
    # an explicitly present, malformed accepted-observation list.
    if accepted is None:
        accepted = [primary]
    if not isinstance(accepted, list):
        return []

    anchor_keys = (
        "source_media_sha256",
        "audio_clip_sha256",
        "audio_start_ms",
        "audio_end_ms",
        "request_sha256",
    )
    if any(primary.get(key) is None for key in anchor_keys):
        return []
    observations: list[dict[str, object]] = []
    for row in accepted:
        if not isinstance(row, Mapping):
            continue
        if not _valid_native_witness(
            row,
            witness_request=rebuilt_witness_request,
        ):
            continue
        if not valid_witness_evidence(
            row,
            request_sha256=str(rebuilt_witness_request["request_sha256"]),
        ):
            continue
        transcript = row.get("exact_transcript")
        if (
            not isinstance(transcript, str)
            or not transcript.strip()
            or "\n" in transcript
            or "\r" in transcript
        ):
            continue
        # Every provider must remain tied to the same already validated source
        # crop and candidate-free request.  This prevents a valid-looking row
        # from another source/window from becoming convergence authority.
        if any(row.get(key) != primary.get(key) for key in anchor_keys):
            continue
        native = row.get("native_observation")
        if not isinstance(native, Mapping):
            continue
        native_keys = (
            "schema_version",
            "provider",
            "model",
            "status",
            "candidate_exposure",
            "authority",
            "mutation_authorized",
            "source_media_sha256",
            "input_audio_sha256",
            "response_sha256",
            "receipt_sha256",
            "target_start_ms",
            "target_end_ms",
            "crop_is_exact_target",
            "observation_scope",
            "transcript",
            "native_segments",
            "raw_native_segments",
            "source_segments",
            "native_timeline",
            "transcript_basis",
            "transcript_timeline_available",
            "speaker_identity_observed",
        )
        compact_native = {
            key: native[key] for key in native_keys if key in native
        }
        observations.append(
            {
                "schema_version": row.get("schema_version"),
                "witness_protocol": row.get("witness_protocol"),
                "status": row.get("status"),
                "target_audible": row.get("target_audible"),
                "candidate_exposure": row.get("candidate_exposure"),
                "authority": row.get("authority"),
                "mutation_authorized": row.get("mutation_authorized"),
                "provider": row.get("provider"),
                "model": row.get("model"),
                "exact_transcript": row.get("exact_transcript"),
                "source_media_sha256": row.get("source_media_sha256"),
                "audio_clip_sha256": row.get("audio_clip_sha256"),
                "audio_start_ms": row.get("audio_start_ms"),
                "audio_end_ms": row.get("audio_end_ms"),
                "crop_is_exact_target": native.get("crop_is_exact_target"),
                "observation_scope": native.get("observation_scope"),
                "source_media_timeline_offset_ms": rebuilt_witness_request.get(
                    "source_media_timeline_offset_ms"
                ),
                "require_complete_utterance_support": request.get(
                    "require_complete_utterance_support"
                ),
                "matched_start_ms": rebuilt_witness_request.get("matched_start_ms"),
                "matched_end_ms": rebuilt_witness_request.get("matched_end_ms"),
                "context_start_ms": rebuilt_witness_request.get("context_start_ms"),
                "context_end_ms": rebuilt_witness_request.get("context_end_ms"),
                "check_request_sha256": request.get("request_sha256"),
                "request_sha256": row.get("request_sha256"),
                "response_sha256": row.get("response_sha256"),
                "receipt_sha256": native.get("receipt_sha256"),
                "native_observation": compact_native,
            }
        )
    return observations


def _compact_current_evidence(
    finding: Mapping[str, Any],
    adjudication: Mapping[str, Any],
) -> dict[str, object]:
    request = adjudication.get("request")
    verdict = adjudication.get("verdict")
    witness_judge = adjudication.get("witness_judge")
    judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
    similarities = (
        witness_judge.get("candidate_pinyin_similarity")
        if isinstance(witness_judge, Mapping)
        else None
    )
    if not isinstance(similarities, Mapping) and isinstance(judge, Mapping):
        similarities = judge.get("candidate_pinyin_similarity")
    compact: dict[str, object] = {
        "finding_base_text_sha256": finding.get("base_text_sha256"),
        "finding_proposed_full_cue": finding.get("proposed_full_cue"),
        "finding_reason": reviewer_reason(finding, limit=240),
        "request_sha256": (request.get("request_sha256") if isinstance(request, Mapping) else None),
        "candidate_provenance": (
            request.get("candidate_provenance")
            if isinstance(request, Mapping)
            else finding.get("candidate_provenance")
        ),
        # These observations have already passed the ordinary acoustic request
        # builder. Preserve its scoped evidence through the history judge;
        # raw finding-side observations are deliberately not imported here.
        "bound_context_observations": (
            {
                key: request[key]
                for key in (
                    "require_complete_utterance_support",
                    "source_media_timeline_offset_ms",
                    "prior_acoustic_observations",
                    "context_only_range_acoustic_observations",
                    "context_only_screen_observations",
                )
                if key in request
            }
            if isinstance(request, Mapping)
            else {}
        ),
        "witness": (
            {
                key: verdict.get(key)
                for key in (
                    "status",
                    "witness_protocol",
                    "target_audible",
                    "heard_pinyin",
                    "confidence",
                    "request_sha256",
                    "audio_clip_sha256",
                )
                if key in verdict
            }
            if isinstance(verdict, Mapping)
            else None
        ),
        "candidate_pinyin_similarity": (
            dict(similarities) if isinstance(similarities, Mapping) else None
        ),
        "binary_judge": (
            {
                key: judge.get(key)
                for key in (
                    "choice",
                    "reason",
                    "ranking",
                    "prompt_sha256",
                    "completion_sha256",
                )
                if key in judge
            }
            if isinstance(judge, Mapping)
            else None
        ),
    }
    if isinstance(request, Mapping):
        native_observations = _compact_candidate_blind_transcript_observations(
            adjudication,
            request,
        )
        # Do not add an empty field to blind-pinyin evidence: its historical
        # prompt/cache identity remains byte-for-byte unchanged.
        if native_observations:
            compact["candidate_blind_transcript_observations"] = native_observations
    return compact


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _candidate_id(text: str) -> str:
    return "DROP" if text == "" else "TEXT_" + _text_sha256(text)


def _cycle_candidate_from_finding(
    finding: Mapping[str, Any],
) -> str | None:
    """Return a fully adjudicated current candidate, including legal DROP."""

    adjudication = finding.get("exact_release_adjudication")
    if not isinstance(adjudication, Mapping):
        return None
    request = adjudication.get("request")
    witness_judge = adjudication.get("witness_judge")
    judge = (
        witness_judge.get("judge")
        if isinstance(witness_judge, Mapping)
        else None
    )
    mutation = adjudication.get("mutation_authority")
    if not (
        adjudication.get("schema_version")
        == "subtitle-span-adjudication.v1"
        and adjudication.get("status") == "OBSERVED"
        and adjudication.get("decision_authority") == "CPA_JUDGE"
        and adjudication.get("repaired") is True
        and adjudication.get("timing_immutable") is True
        and isinstance(request, Mapping)
        and request.get("schema_version")
        == "subtitle-span-acoustic-check-request.v1"
        and _valid_sha256(request.get("request_sha256"))
        and isinstance(judge, Mapping)
        and judge.get("status") == "JUDGED"
        and isinstance(mutation, Mapping)
        and mutation.get("schema_version")
        == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
    ):
        return None
    proposed = request.get("proposed_cue")
    if not isinstance(proposed, str):
        return None
    if proposed:
        return (
            proposed
            if proposed.strip() and judge.get("choice") == "PROPOSED"
            else None
        )
    return (
        ""
        if judge.get("choice") == "DROP"
        and finding.get("repair_class") == "acoustic_drop_cue"
        and valid_inaudible_drop_authority(adjudication)
        else None
    )


def _cycle_group_key(
    finding: Mapping[str, Any],
) -> tuple[int, int] | None:
    adjudication = finding.get("exact_release_adjudication")
    request = (
        adjudication.get("request")
        if isinstance(adjudication, Mapping)
        else None
    )
    if not isinstance(request, Mapping):
        return None
    start_ms = request.get("matched_start_ms")
    end_ms = request.get("matched_end_ms")
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or not 0 <= start_ms < end_ms
    ):
        return None
    return start_ms, end_ms


_EVIDENCE_HASH_KEYS = frozenset(
    {
        "audio_clip_sha256",
        "audio_sha256",
        "source_sha256",
        "source_event_sha256",
        "chat_source_sha256",
        "frame_sha256",
        "screenshot_sha256",
        "transcript_sha256",
        "witness_sha256",
    }
)

_EVIDENCE_CONTENT_ANCHOR_KEYS = frozenset(
    {
        "audio_clip_sha256",
        "audio_sha256",
        "target_audible",
        "heard_pinyin",
        "witness_protocol",
        "candidate_pinyin_similarity",
        "current_fit",
        "proposed_fit",
        "confidence",
        "uncertain_positions",
    }
)

_EVIDENCE_CONTENT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "reason_code",
        "audio_clip_sha256",
        "audio_sha256",
        "target_audible",
        "heard_pinyin",
        "witness_protocol",
        "candidate_pinyin_similarity",
        "current_fit",
        "proposed_fit",
        "confidence",
        "uncertain_positions",
        "syllable_count",
        "model",
        "model_id",
        "provider",
        "completion_sha256",
        "raw_completion_sha256",
        "transcript",
        "observed_text",
    }
)


def _evidence_authority_fingerprints(
    value: object,
) -> list[str]:
    """Extract stable evidence identities, excluding direction-bound prompts."""

    identities: set[str] = set()

    def visit(node: object, path: tuple[str, ...]) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                key_text = str(key)
                next_path = (*path, key_text)
                if (
                    key_text in _EVIDENCE_HASH_KEYS
                    and isinstance(child, str)
                    and _valid_sha256(child)
                ):
                    identities.add(
                        key_text
                        + "="
                        + child.removeprefix("sha256:")
                    )
                elif (
                    key_text in {"source_event_id", "evidence_cue_ids"}
                    and child is not None
                    and child != ""
                    and child != []
                ):
                    identities.add(
                        key_text
                        + "="
                        + _canonical_json(child)
                    )
                visit(child, next_path)
        elif isinstance(node, list):
            for child in node:
                visit(child, path)

    visit(value, ())
    return sorted(identities)


def _evidence_content_fingerprints(value: object) -> list[str]:
    """Bind material acoustic witness content, not only media identity.

    CPA judge prompts/completions are deliberately excluded: they are the
    decision surface, while these fingerprints describe newly produced
    witness observations that must reopen an older closed-set memo.
    """

    fingerprints: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            schema = str(node.get("schema_version") or "").lower()
            anchored = bool(
                _EVIDENCE_CONTENT_ANCHOR_KEYS.intersection(node)
                or "acoustic-witness" in schema
            )
            if anchored:
                material = {
                    str(key): child
                    for key, child in node.items()
                    if str(key) in _EVIDENCE_CONTENT_KEYS
                }
                if material:
                    fingerprints.add(
                        "sha256:" + _json_sha256(material)
                    )
            for key, child in node.items():
                key_text = str(key)
                if key_text in {"judge", "witness_judge"}:
                    continue
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return sorted(fingerprints)


def _complete_cycle_evidence(
    findings: Iterable[Mapping[str, Any]],
) -> list[dict[str, object]]:
    """Keep every persisted field used by the current exact-final decisions."""

    rows: list[dict[str, object]] = []
    ordered = sorted(
        findings,
        key=lambda finding: (
            _candidate_id(
                _cycle_candidate_from_finding(finding) or ""
            ),
            _json_sha256(finding),
        ),
    )
    for finding in ordered:
        adjudication = finding.get("exact_release_adjudication")
        rows.append(
            {
                "finding": {
                    key: value
                    for key, value in finding.items()
                    if key != "exact_release_adjudication"
                },
                "exact_release_adjudication": (
                    dict(adjudication)
                    if isinstance(adjudication, Mapping)
                    else None
                ),
            }
        )
    return rows


def _complete_cycle_history(
    history: Iterable[Mapping[str, Any]],
    *,
    authority_audit: Mapping[str, object],
    window: tuple[int, int],
) -> list[dict[str, object]]:
    """Return the full receipts and prior cycle/convergence memos for a window."""

    rows: list[dict[str, object]] = [
        {"kind": "exact_final_repair", "receipt": dict(row)}
        for row in history
    ]
    memos = authority_audit.get("exact_final_cpa_convergence_memos")
    for memo in memos if isinstance(memos, list) else []:
        if (
            isinstance(memo, Mapping)
            and memo.get("matched_start_ms") == window[0]
            and memo.get("matched_end_ms") == window[1]
            and (_valid_memo(memo) or _valid_cycle_memo(memo))
        ):
            rows.append({"kind": "prior_cpa_memo", "receipt": dict(memo)})
    return rows


def _cache_path(prompt_sha256: str) -> Path | None:
    base = os.environ.get("AUTOSLICE_BASE")
    if not base:
        return None
    return (
        Path(base)
        / "cache"
        / "exact-final-cpa-convergence"
        / prompt_sha256[:2]
        / f"{prompt_sha256}.json"
    )


def _load_or_call_judge(
    prompt: str,
    *,
    judge_llm_call: Callable[[str], str],
) -> tuple[Mapping[str, Any], str, bool]:
    prompt_sha256 = _text_sha256(prompt)
    path = _cache_path(prompt_sha256)
    # Legacy parsed-only entries have no answering-model binding. Preserve
    # their bytes; model-bound calls use the shared raw-answer lane instead.
    model_bound = bool(getattr(judge_llm_call, 'cpa_cache_identity', None))
    if path is not None and not model_bound:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, Mapping)
                and cached.get("schema_version") == CACHE_SCHEMA
                and cached.get("prompt_sha256") == prompt_sha256
                and isinstance(cached.get("completion_sha256"), str)
                and isinstance(cached.get("payload"), Mapping)
            ):
                return (
                    cached["payload"],
                    str(cached["completion_sha256"]),
                    True,
                )
        except (OSError, ValueError):
            pass
    try:
        completion = call_json(judge_llm_call, prompt)
    except Exception as exc:
        raise _ConvergenceJudgeFailure("judge_call", exc) from exc
    completion_sha256 = _text_sha256(completion)
    try:
        payload = extract_json_object(completion)
    except Exception as exc:
        raise _ConvergenceJudgeFailure("completion_json_parse", exc, completion_sha256) from exc
    if path is not None and not model_bound:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": CACHE_SCHEMA,
                        "prompt_sha256": prompt_sha256,
                        "completion_sha256": completion_sha256,
                        "payload": dict(payload),
                    },
                    ensure_ascii=False,
                    indent=1,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return payload, completion_sha256, False


def _converge_one(
    srt_text: str,
    finding: Mapping[str, Any],
    *,
    history: list[dict[str, Any]],
    judge_llm_call: Callable[[str], str],
) -> tuple[dict[str, Any], bool] | None:
    adjudication = finding.get("exact_release_adjudication")
    if not isinstance(adjudication, Mapping):
        return None
    request = adjudication.get("request")
    witness_judge = adjudication.get("witness_judge")
    binary_judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
    mutation = adjudication.get("mutation_authority")
    if not (
        adjudication.get("schema_version") == "subtitle-span-adjudication.v1"
        and adjudication.get("status") == "OBSERVED"
        and adjudication.get("decision_authority") == "CPA_JUDGE"
        and adjudication.get("repaired") is True
        and adjudication.get("timing_immutable") is True
        and isinstance(mutation, Mapping)
        and mutation.get("schema_version") == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
        and isinstance(request, Mapping)
        and request.get("schema_version") == "subtitle-span-acoustic-check-request.v1"
        and _valid_sha256(request.get("request_sha256"))
        and isinstance(binary_judge, Mapping)
        and binary_judge.get("status") == "JUDGED"
        and binary_judge.get("choice") == "PROPOSED"
    ):
        return None
    try:
        cue_index = int(finding.get("cue_index") or 0)
    except (TypeError, ValueError):
        return None
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not 1 <= cue_index <= len(cues):
        return None
    cue = cues[cue_index - 1]
    current = cue.text
    proposed = request.get("proposed_cue")
    current_sha256 = _text_sha256(current)
    if not (
        isinstance(proposed, str)
        and proposed.strip()
        and proposed != current
        and request.get("current_cue") == current
        and request.get("base_text_sha256") == current_sha256
        and finding.get("base_text_sha256") == current_sha256
        and request.get("matched_start_ms") == cue.start_ms
        and request.get("matched_end_ms") == cue.end_ms
        and history
        and history[-1].get("after_sha256") == "sha256:" + current_sha256
    ):
        return None
    compact_history = _compact_history(history)
    current_evidence = _compact_current_evidence(finding, adjudication)
    context_sha256, before, after = _local_context_binding(srt_text, cue_index)
    history_sha256 = _json_sha256(compact_history)
    evidence_sha256 = _json_sha256(current_evidence)
    srt_sha256 = _text_sha256(srt_text)
    proposed_sha256 = _text_sha256(proposed)
    prompt = _PROMPT.format(
        srt_sha256=srt_sha256,
        cue_index=cue_index,
        start_ms=cue.start_ms,
        end_ms=cue.end_ms,
        current_sha256=current_sha256,
        proposed_sha256=proposed_sha256,
        context_sha256=context_sha256,
        history_sha256=history_sha256,
        evidence_sha256=evidence_sha256,
        current=current,
        proposed=proposed,
        before="\n".join(before) or "（无）",
        after="\n".join(after) or "（无）",
        history_json=json.dumps(compact_history, ensure_ascii=False, sort_keys=True),
        evidence_json=json.dumps(current_evidence, ensure_ascii=False, sort_keys=True),
    )
    prompt_sha256 = _text_sha256(prompt)
    try:
        payload, completion_sha256, served_from_cache = _load_or_call_judge(
            prompt,
            judge_llm_call=judge_llm_call,
        )
    except Exception:
        return None
    choice = str(payload.get("choice") or "").strip().upper()
    reason = str(payload.get("reason") or "")[:240]
    if not reason:
        return None
    needs_audio = payload.get("needs_audio") is True
    if needs_audio or choice == "UNRESOLVED":
        return downgrade_convergence_finding(
            finding,
            gate={
                "schema_version": GATE_SCHEMA,
                "status": "BLOCK",
                "reason_code": (
                    "CPA_REQUESTS_MORE_AUDIO" if needs_audio
                    else "CPA_HISTORY_CONVERGENCE_UNRESOLVED"
                ),
                "judge_prompt_sha256": "sha256:" + prompt_sha256,
                "judge_completion_sha256": "sha256:" + completion_sha256,
                "judge_served_from_cache": served_from_cache,
                "needs_audio": needs_audio,
                "reason": reason,
            },
        ), False
    if choice not in {"CURRENT", "PROPOSED"}:
        return None
    acoustic_gate = None
    if choice == "PROPOSED":
        acoustic_gate = convergence_witness_gate(
            proposed=proposed,
            window=(cue.start_ms, cue.end_ms),
            findings=[finding],
            history=history,
        )
        if acoustic_gate.get("status") != "PASS":
            return downgrade_convergence_finding(
                finding,
                gate=acoustic_gate,
            ), False
    final_text = current if choice == "CURRENT" else proposed
    bound_context_sha256, _before, _after = _local_context_binding(
        srt_text,
        cue_index,
        target_text=final_text,
    )
    memo = {
        "schema_version": MEMO_SCHEMA,
        "status": "RESOLVED",
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
        "choice": choice,
        "final_text": final_text,
        "final_text_sha256": "sha256:" + _text_sha256(final_text),
        "cue_index": cue_index,
        "matched_start_ms": cue.start_ms,
        "matched_end_ms": cue.end_ms,
        "origin_srt_sha256": "sha256:" + srt_sha256,
        "origin_current_cue_sha256": "sha256:" + current_sha256,
        "origin_proposed_cue_sha256": "sha256:" + proposed_sha256,
        "origin_local_context_sha256": "sha256:" + context_sha256,
        "bound_local_context_sha256": "sha256:" + bound_context_sha256,
        "history_sha256": "sha256:" + history_sha256,
        "new_evidence_sha256": "sha256:" + evidence_sha256,
        "new_evidence": current_evidence,
        "origin_request_sha256": "sha256:"
        + str(request.get("request_sha256") or "").removeprefix("sha256:"),
        "history_receipts": compact_history,
        "prompt_sha256": "sha256:" + prompt_sha256,
        "completion_sha256": "sha256:" + completion_sha256,
        "reason": reason,
        "timing_immutable": True,
    }
    if served_from_cache:
        memo["served_from_cache"] = True
    if acoustic_gate is not None:
        memo["acoustic_witness_gate"] = acoustic_gate
    row = dict(finding)
    final_adjudication = dict(adjudication)
    final_adjudication["pre_convergence_adjudication"] = {
        "policy_branch": adjudication.get("policy_branch"),
        "witness_judge": adjudication.get("witness_judge"),
        "mutation_authority": adjudication.get("mutation_authority"),
    }
    final_adjudication["exact_final_cpa_convergence_memo"] = memo
    final_adjudication["witness_judge"] = {
        "witness_status": "HISTORY_AND_CURRENT_EVIDENCE_BOUND",
        "decision_authority": "CPA_JUDGE",
        "judge": {
            "schema_version": "exact-final-cpa-convergence-judge.v1",
            "status": "JUDGED",
            "choice": choice,
            "reason": reason,
            "prompt_sha256": "sha256:" + prompt_sha256,
            "completion_sha256": "sha256:" + completion_sha256,
        },
    }
    if choice == "CURRENT":
        final_adjudication.update(
            repaired=False,
            policy_branch="CPA_HISTORY_CONVERGENCE_KEEP_CURRENT",
            mutation_authority={
                "schema_version": ("subtitle-correction-mutation-authority.v1"),
                "status": "NOT_APPLIED",
                "basis": "CPA_HISTORY_CONVERGENCE_KEEP_CURRENT",
            },
        )
        row["resolution"] = "CPA_HISTORY_CONVERGENCE_KEEP_CURRENT"
    else:
        final_adjudication["history_convergence_acoustic_witness"] = (
            acoustic_gate
        )
        final_adjudication.update(
            repaired=True,
            policy_branch="CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED",
            mutation_authority={
                "schema_version": ("subtitle-correction-mutation-authority.v1"),
                "status": "PASS",
                "basis": "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED",
            },
        )
    row["exact_release_adjudication"] = final_adjudication
    _retain_superseded_cycle_blocks(row, memo=memo, srt_text=srt_text)
    return row, choice == "CURRENT"


def _cycle_is_present(
    history: list[dict[str, Any]],
    findings: Iterable[Mapping[str, Any]],
) -> bool:
    """Detect A→B→A or another previously rejected surface returning."""

    historical_rejected = {
        str(row.get("before"))
        for row in history
        if isinstance(row.get("before"), str)
    }
    proposals = [
        candidate
        for candidate in (
            _cycle_candidate_from_finding(row) for row in findings
        )
        if candidate is not None
    ]
    return bool(
        any(candidate in historical_rejected for candidate in proposals)
        or len(set(proposals)) > 1
    )


def _cycle_block_receipt(
    *,
    reason_code: str,
    window: tuple[int, int],
    current_sha256: str,
    candidate_set_sha256: str,
    history_sha256: str,
    evidence_sha256: str,
    prompt_sha256: str | None = None,
    completion_sha256: str | None = None,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": CYCLE_RECEIPT_SCHEMA,
        "status": "BLOCK",
        "reason_code": reason_code,
        "decision_authority": "CPA_JUDGE_REQUIRED",
        "matched_start_ms": window[0],
        "matched_end_ms": window[1],
        "current_cue_sha256": "sha256:" + current_sha256,
        "candidate_set_sha256": "sha256:" + candidate_set_sha256,
        "history_sha256": "sha256:" + history_sha256,
        "current_evidence_sha256": "sha256:" + evidence_sha256,
        "timing_immutable": True,
    }
    if prompt_sha256 is not None:
        receipt["prompt_sha256"] = "sha256:" + prompt_sha256
    if completion_sha256 is not None:
        receipt["completion_sha256"] = "sha256:" + completion_sha256
    return receipt


def _attach_cycle_block(
    findings: Iterable[Mapping[str, Any]],
    receipt: Mapping[str, object],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for finding in findings:
        row = dict(finding)
        row["exact_final_cpa_cycle_adjudication"] = dict(receipt)
        adjudication = row.get("exact_release_adjudication")
        if isinstance(adjudication, Mapping):
            copied = dict(adjudication)
            copied["pre_cycle_adjudication"] = dict(adjudication)
            copied.update(
                repaired=False,
                decision_authority=receipt["decision_authority"],
                policy_branch=str(
                    receipt.get("reason_code")
                    or "CPA_CYCLE_ADJUDICATION_BLOCKED"
                ),
                mutation_authority={
                    "schema_version": (
                        "subtitle-correction-mutation-authority.v1"
                    ),
                    "status": "NOT_APPLIED",
                    "basis": str(
                        receipt.get("reason_code")
                        or "CPA_CYCLE_ADJUDICATION_BLOCKED"
                    ),
                },
            )
            copied["exact_final_cpa_cycle_adjudication"] = dict(receipt)
            row["exact_release_adjudication"] = copied
        rows.append(row)
    return rows


def _cycle_candidates(
    current: str,
    history: Iterable[Mapping[str, Any]],
    findings: Iterable[Mapping[str, Any]],
) -> list[dict[str, object]]:
    by_text: dict[str, dict[str, object]] = {}

    def add(text: str, role: str, *, legal_drop: bool = False) -> None:
        row = by_text.setdefault(
            text,
            {
                "candidate_id": _candidate_id(text),
                "text": text,
                "text_sha256": "sha256:" + _text_sha256(text),
                "roles": [],
                "legal_drop": False,
            },
        )
        roles = row["roles"]
        if isinstance(roles, list) and role not in roles:
            roles.append(role)
        if legal_drop:
            row["legal_drop"] = True

    add(current, "CURRENT")
    for index, repair in enumerate(history, start=1):
        before = repair.get("before")
        after = repair.get("after")
        if isinstance(before, str) and before:
            add(before, f"HISTORY_PASS_{index}_REJECTED")
        if isinstance(after, str) and after:
            add(after, f"HISTORY_PASS_{index}_SELECTED")
    for finding in findings:
        candidate = _cycle_candidate_from_finding(finding)
        if candidate is None:
            continue
        add(
            candidate,
            "CURRENT_FINDING",
            legal_drop=candidate == "",
        )
    return sorted(
        by_text.values(),
        key=lambda row: (
            row["candidate_id"] != _candidate_id(current),
            str(row["candidate_id"]),
        ),
    )


def _cycle_memo(
    *,
    choice_id: str,
    reason: str,
    cue_index: int,
    current: str,
    selected_text: str,
    window: tuple[int, int],
    srt_sha256: str,
    context_sha256: str,
    bound_context_sha256: str,
    candidates: list[dict[str, object]],
    history: list[dict[str, object]],
    evidence: list[dict[str, object]],
    evidence_fingerprints: list[str],
    evidence_content_fingerprints: list[str],
    prompt_sha256: str,
    completion_sha256: str,
    served_from_cache: bool,
) -> dict[str, Any]:
    memo: dict[str, Any] = {
        "schema_version": CYCLE_MEMO_SCHEMA,
        "status": "LOCKED",
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
        "choice_id": choice_id,
        "final_text": selected_text,
        "final_text_sha256": "sha256:" + _text_sha256(selected_text),
        "cue_index": cue_index,
        "matched_start_ms": window[0],
        "matched_end_ms": window[1],
        "origin_srt_sha256": "sha256:" + srt_sha256,
        "origin_current_cue_sha256": "sha256:" + _text_sha256(current),
        "origin_local_context_sha256": "sha256:" + context_sha256,
        "bound_local_context_sha256": "sha256:" + bound_context_sha256,
        "candidate_set": candidates,
        "candidate_set_sha256": "sha256:" + _json_sha256(candidates),
        "history_receipts": history,
        "history_sha256": "sha256:" + _json_sha256(history),
        "current_evidence": evidence,
        "current_evidence_sha256": "sha256:" + _json_sha256(evidence),
        "evidence_authority_fingerprints": evidence_fingerprints,
        "evidence_authority_fingerprints_sha256": (
            "sha256:" + _json_sha256(evidence_fingerprints)
        ),
        "evidence_content_fingerprints": evidence_content_fingerprints,
        "evidence_content_fingerprints_sha256": (
            "sha256:" + _json_sha256(evidence_content_fingerprints)
        ),
        "prompt_sha256": "sha256:" + prompt_sha256,
        "completion_sha256": "sha256:" + completion_sha256,
        "reason": reason,
        "timing_immutable": True,
    }
    if served_from_cache:
        memo["served_from_cache"] = True
    return memo


def _apply_cycle_choice(
    *,
    srt_text: str,
    findings: list[dict[str, Any]],
    current: str,
    cue_index: int,
    window: tuple[int, int],
    selected_text: str,
    memo: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return at most one CPA-authorized mutation and resolve all competitors."""

    selected_template = next(
        (
            row
            for row in findings
            if _cycle_candidate_from_finding(row) == selected_text
        ),
        findings[0],
    )
    resolved: list[dict[str, Any]] = []
    if selected_text == current:
        for finding in findings:
            row = dict(finding)
            adjudication = row.get("exact_release_adjudication")
            final_adjudication = (
                dict(adjudication)
                if isinstance(adjudication, Mapping)
                else {}
            )
            final_adjudication.update(
                repaired=False,
                policy_branch="CPA_EXACT_FINAL_CYCLE_KEEP_CURRENT",
                decision_authority="CPA_JUDGE",
                witness_authority="EVIDENCE_ONLY",
                timing_immutable=True,
                mutation_authority={
                    "schema_version": (
                        "subtitle-correction-mutation-authority.v1"
                    ),
                    "status": "NOT_APPLIED",
                    "basis": "CPA_EXACT_FINAL_CYCLE_KEEP_CURRENT",
                },
                exact_final_cpa_cycle_memo=dict(memo),
            )
            row["resolution"] = "CPA_EXACT_FINAL_CYCLE_KEEP_CURRENT"
            row["exact_release_adjudication"] = final_adjudication
            _retain_superseded_cycle_blocks(row, memo=memo, srt_text=srt_text)
            resolved.append(row)
        return [], resolved

    selected = dict(selected_template)
    template_adjudication = selected.get("exact_release_adjudication")
    if not isinstance(template_adjudication, Mapping):
        return _attach_cycle_block(
            findings,
            {
                "schema_version": CYCLE_RECEIPT_SCHEMA,
                "status": "BLOCK",
                "reason_code": "CPA_CYCLE_SELECTED_TEMPLATE_INVALID",
            },
        ), []
    if selected_text == "":
        final_adjudication = dict(template_adjudication)
        final_adjudication["exact_final_cpa_cycle_memo"] = dict(memo)
        selected["exact_release_adjudication"] = final_adjudication
    else:
        request_without_sha: dict[str, object] = {
            "schema_version": "subtitle-span-acoustic-check-request.v1",
            "repair_class": "exact_final_cycle_closed_set",
            "base_text_sha256": _text_sha256(current),
            "current_cue": current,
            "proposed_cue": selected_text,
            "matched_start_ms": window[0],
            "matched_end_ms": window[1],
            "cycle_prompt_sha256": str(
                memo.get("prompt_sha256") or ""
            ).removeprefix("sha256:"),
            "candidate_set_sha256": str(
                memo.get("candidate_set_sha256") or ""
            ).removeprefix("sha256:"),
            "current_evidence_sha256": str(
                memo.get("current_evidence_sha256") or ""
            ).removeprefix("sha256:"),
            "timing_immutable": True,
        }
        request = dict(request_without_sha)
        request["request_sha256"] = _json_sha256(request_without_sha)
        final_adjudication = {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "decision_authority": "CPA_JUDGE",
            "witness_authority": "EVIDENCE_ONLY",
            "repaired": True,
            "timing_immutable": True,
            "policy_branch": (
                "CPA_EXACT_FINAL_CYCLE_CLOSED_SET_APPLY"
            ),
            "request": request,
            "verdict": {
                "schema_version": "exact-final-cycle-evidence-bundle.v1",
                "status": "OBSERVED",
                "evidence_authority": "EVIDENCE_ONLY",
                "current_evidence_sha256": memo.get(
                    "current_evidence_sha256"
                ),
                "history_sha256": memo.get("history_sha256"),
            },
            "witness_judge": {
                "witness_status": "CYCLE_HISTORY_AND_EVIDENCE_BOUND",
                "decision_authority": "CPA_JUDGE",
                "witness_authority": "EVIDENCE_ONLY",
                "judge": {
                    "schema_version": (
                        "exact-final-cpa-cycle-judge.v1"
                    ),
                    "status": "JUDGED",
                    "choice": "PROPOSED",
                    "selected_candidate_id": memo.get("choice_id"),
                    "reason": memo.get("reason"),
                    "prompt_sha256": memo.get("prompt_sha256"),
                    "completion_sha256": memo.get(
                        "completion_sha256"
                    ),
                },
            },
            "mutation_authority": {
                "schema_version": (
                    "subtitle-correction-mutation-authority.v1"
                ),
                "status": "PASS",
                "basis": (
                    "CPA_EXACT_FINAL_CYCLE_CLOSED_SET_ADJUDICATION"
                ),
            },
            "pre_cycle_adjudications": [
                row.get("exact_release_adjudication")
                for row in findings
            ],
            "exact_final_cpa_cycle_memo": dict(memo),
        }
        if memo.get("acoustic_witness_gate") is not None:
            final_adjudication[
                "history_convergence_acoustic_witness"
            ] = memo["acoustic_witness_gate"]
        selected.update(
            cue_index=cue_index,
            base_text_sha256=_text_sha256(current),
            proposed_full_cue=selected_text,
            suggestion=selected_text,
            repair_class="exact_final_cycle_closed_set",
            exact_release_adjudication=final_adjudication,
        )

    for finding in findings:
        if finding is selected_template:
            continue
        row = dict(finding)
        adjudication = row.get("exact_release_adjudication")
        final_adjudication = (
            dict(adjudication)
            if isinstance(adjudication, Mapping)
            else {}
        )
        final_adjudication.update(
            repaired=False,
            policy_branch=(
                "CPA_EXACT_FINAL_CYCLE_COMPETING_PROPOSAL_REJECTED"
            ),
            decision_authority="CPA_JUDGE",
            witness_authority="EVIDENCE_ONLY",
            timing_immutable=True,
            mutation_authority={
                "schema_version": (
                    "subtitle-correction-mutation-authority.v1"
                ),
                "status": "NOT_APPLIED",
                "basis": (
                    "CPA_EXACT_FINAL_CYCLE_COMPETING_PROPOSAL_REJECTED"
                ),
            },
            exact_final_cpa_cycle_memo=dict(memo),
        )
        row["resolution"] = (
            "CPA_EXACT_FINAL_CYCLE_COMPETING_PROPOSAL_REJECTED"
        )
        row["exact_release_adjudication"] = final_adjudication
        resolved.append(row)
    _retain_superseded_cycle_blocks(selected, memo=memo, srt_text=srt_text)
    for row in resolved:
        _retain_superseded_cycle_blocks(row, memo=memo, srt_text=srt_text)
    return [selected], resolved


def _converge_cycle_group(
    srt_text: str,
    findings: list[dict[str, Any]],
    *,
    history: list[dict[str, Any]],
    authority_audit: Mapping[str, object],
    judge_llm_call: Callable[[str], str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings = sorted(
        findings,
        key=lambda finding: (
            _candidate_id(
                _cycle_candidate_from_finding(finding) or ""
            ),
            _json_sha256(finding),
        ),
    )
    window = _cycle_group_key(findings[0])
    if window is None:
        return findings, []
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    cue_indexes = {
        int(row.get("cue_index") or 0)
        for row in findings
        if isinstance(row.get("cue_index"), int)
        and not isinstance(row.get("cue_index"), bool)
    }
    if len(cue_indexes) != 1:
        return findings, []
    cue_index = next(iter(cue_indexes))
    if not 1 <= cue_index <= len(cues):
        return findings, []
    cue = cues[cue_index - 1]
    current = cue.text
    current_sha256 = _text_sha256(current)
    if (
        (cue.start_ms, cue.end_ms) != window
        or any(
            (
                row.get("base_text_sha256") != current_sha256
                or _cycle_candidate_from_finding(row) is None
            )
            for row in findings
        )
        or (
            history
            and history[-1].get("after_sha256")
            != "sha256:" + current_sha256
        )
    ):
        return findings, []

    candidates = _cycle_candidates(current, history, findings)
    complete_history = _complete_cycle_history(
        history,
        authority_audit=authority_audit,
        window=window,
    )
    complete_evidence = _complete_cycle_evidence(findings)
    evidence_fingerprints = _evidence_authority_fingerprints(
        {
            "history": complete_history,
            "current": complete_evidence,
        }
    )
    evidence_content_fingerprints = _evidence_content_fingerprints(
        complete_evidence
    )
    context_sha256, before, after = _local_context_binding(
        srt_text,
        cue_index,
    )
    srt_sha256 = _text_sha256(srt_text)
    candidate_set_sha256 = _json_sha256(candidates)
    history_sha256 = _json_sha256(complete_history)
    evidence_sha256 = _json_sha256(complete_evidence)
    history_json, evidence_json, shared_receipts_section = _cycle_prompt_receipts(
        complete_history, complete_evidence
    )
    prompt = _CYCLE_PROMPT.format(
        srt_sha256=srt_sha256,
        cue_index=cue_index,
        start_ms=window[0],
        end_ms=window[1],
        current_sha256=current_sha256,
        context_sha256=context_sha256,
        candidate_set_sha256=candidate_set_sha256,
        history_sha256=history_sha256,
        evidence_sha256=evidence_sha256,
        candidates_json=json.dumps(
            candidates,
            ensure_ascii=False,
            sort_keys=True,
        ),
        before="\n".join(before) or "（无）",
        current=current,
        after="\n".join(after) or "（无）",
        history_json=history_json,
        evidence_json=evidence_json,
        shared_receipts_section=shared_receipts_section,
    )
    prompt_sha256 = _text_sha256(prompt)
    if judge_llm_call is None:
        receipt = _cycle_block_receipt(
            reason_code="CPA_CYCLE_ADJUDICATION_UNAVAILABLE",
            window=window,
            current_sha256=current_sha256,
            candidate_set_sha256=candidate_set_sha256,
            history_sha256=history_sha256,
            evidence_sha256=evidence_sha256,
            prompt_sha256=prompt_sha256,
        )
        receipt["failure"] = {"stage": "judge_callback_missing"}
        return _attach_cycle_block(findings, receipt), []
    try:
        payload, completion_sha256, served_from_cache = (
            _load_or_call_judge(
                prompt,
                judge_llm_call=judge_llm_call,
            )
        )
    except Exception as exc:
        receipt = _cycle_block_receipt(
            reason_code="CPA_CYCLE_ADJUDICATION_UNAVAILABLE",
            window=window,
            current_sha256=current_sha256,
            candidate_set_sha256=candidate_set_sha256,
            history_sha256=history_sha256,
            evidence_sha256=evidence_sha256,
            prompt_sha256=prompt_sha256,
        )
        receipt["failure"] = {
            **(exc.detail if isinstance(exc, _ConvergenceJudgeFailure) else {
                "stage": "judge_path",
                "exception_type": type(exc).__name__,
                "exception_sha256": _text_sha256(str(exc)),
            }),
            "prompt_char_count": len(prompt),
            "prompt_utf8_byte_count": len(prompt.encode("utf-8")),
        }
        return _attach_cycle_block(findings, receipt), []
    needs_audio = payload.get("needs_audio") is True
    choice_id = str(payload.get("choice_id") or "").strip()
    if needs_audio or choice_id.upper() == "UNRESOLVED":
        receipt = _cycle_block_receipt(
            reason_code=(
                "CPA_REQUESTS_MORE_AUDIO" if needs_audio
                else "CPA_CYCLE_ADJUDICATION_UNRESOLVED"
            ),
            window=window,
            current_sha256=current_sha256,
            candidate_set_sha256=candidate_set_sha256,
            history_sha256=history_sha256,
            evidence_sha256=evidence_sha256,
            prompt_sha256=prompt_sha256,
        )
        receipt.update(
            judge_completion_sha256="sha256:" + completion_sha256,
            judge_served_from_cache=served_from_cache,
            needs_audio=needs_audio,
            reason=str(payload.get("reason") or "")[:240],
        )
        return _attach_cycle_block(findings, receipt), []
    # Preserve the pre-v2 binary convergence contract only when the complete
    # closed set really contains exactly CURRENT and one PROPOSED candidate.
    # Multi-candidate cycles (the 814 failure mode) must name a candidate_id.
    if not choice_id and len(candidates) == 2 and len(findings) == 1:
        legacy_choice = str(payload.get("choice") or "").strip().upper()
        if legacy_choice == "CURRENT":
            choice_id = _candidate_id(current)
        elif legacy_choice == "PROPOSED":
            proposed = _cycle_candidate_from_finding(findings[0])
            if proposed is not None:
                choice_id = _candidate_id(proposed)
    reason = str(payload.get("reason") or "").strip()[:240]
    selected = next(
        (
            row
            for row in candidates
            if row.get("candidate_id") == choice_id
        ),
        None,
    )
    if (
        selected is None
        or not reason
        or (
            selected.get("text") == ""
            and selected.get("legal_drop") is not True
        )
    ):
        receipt = _cycle_block_receipt(
            reason_code="CPA_CYCLE_ADJUDICATION_INVALID",
            window=window,
            current_sha256=current_sha256,
            candidate_set_sha256=candidate_set_sha256,
            history_sha256=history_sha256,
            evidence_sha256=evidence_sha256,
            prompt_sha256=prompt_sha256,
            completion_sha256=completion_sha256,
        )
        return _attach_cycle_block(findings, receipt), []
    selected_text = str(selected.get("text") or "")
    bound_context_sha256, _before, _after = _local_context_binding(
        srt_text,
        cue_index,
        target_text=selected_text,
    )
    memo = _cycle_memo(
        choice_id=choice_id,
        reason=reason,
        cue_index=cue_index,
        current=current,
        selected_text=selected_text,
        window=window,
        srt_sha256=srt_sha256,
        context_sha256=context_sha256,
        bound_context_sha256=bound_context_sha256,
        candidates=candidates,
        history=complete_history,
        evidence=complete_evidence,
        evidence_fingerprints=evidence_fingerprints,
        evidence_content_fingerprints=(
            evidence_content_fingerprints
        ),
        prompt_sha256=prompt_sha256,
        completion_sha256=completion_sha256,
        served_from_cache=served_from_cache,
    )
    if selected_text and selected_text != current:
        acoustic_gate = convergence_witness_gate(
            proposed=selected_text,
            window=window,
            findings=findings,
            history=history,
        )
        memo["acoustic_witness_gate"] = acoustic_gate
        if acoustic_gate.get("status") != "PASS":
            downgraded: list[dict[str, Any]] = []
            for finding in findings:
                row = downgrade_convergence_finding(
                    finding,
                    gate=acoustic_gate,
                )
                row["exact_release_adjudication"][
                    "exact_final_cpa_cycle_memo"
                ] = dict(memo)
                downgraded.append(row)
            return downgraded, []
    return _apply_cycle_choice(
        srt_text=srt_text,
        findings=findings,
        current=current,
        cue_index=cue_index,
        window=window,
        selected_text=selected_text,
        memo=memo,
    )


def converge_reconsidered_exact_final_findings(
    srt_text: str,
    findings: Iterable[Mapping[str, Any]],
    *,
    authority_audit: Mapping[str, object],
    judge_llm_call: Callable[[str], str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give CPA the prior receipt chain before accepting another cue rewrite."""

    rows = [dict(row) for row in findings]
    histories = _repair_histories(authority_audit)
    unresolved: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    grouped_indexes: dict[tuple[int, int], list[int]] = {}
    for index, row in enumerate(rows):
        key = _cycle_group_key(row)
        if key is not None:
            grouped_indexes.setdefault(key, []).append(index)
    handled: set[int] = set()
    for key, indexes in grouped_indexes.items():
        history = histories.get(key, [])
        group = [rows[index] for index in indexes]
        if not _cycle_is_present(history, group):
            continue
        cycle_unresolved, cycle_resolved = _converge_cycle_group(
            srt_text,
            group,
            history=history,
            authority_audit=authority_audit,
            judge_llm_call=judge_llm_call,
        )
        unresolved.extend(cycle_unresolved)
        resolved.extend(cycle_resolved)
        handled.update(indexes)
    for index, row in enumerate(rows):
        if index in handled:
            continue
        adjudication = row.get("exact_release_adjudication")
        request = adjudication.get("request") if isinstance(adjudication, Mapping) else None
        key = (
            (
                int(request["matched_start_ms"]),
                int(request["matched_end_ms"]),
            )
            if isinstance(request, Mapping)
            and isinstance(request.get("matched_start_ms"), int)
            and not isinstance(request.get("matched_start_ms"), bool)
            and isinstance(request.get("matched_end_ms"), int)
            and not isinstance(request.get("matched_end_ms"), bool)
            else None
        )
        converged = (
            _converge_one(
                srt_text,
                row,
                history=histories.get(key, []),
                judge_llm_call=judge_llm_call,
            )
            if key is not None and judge_llm_call is not None
            else None
        )
        if converged is None:
            unresolved.append(row)
            continue
        final_row, keep_current = converged
        (resolved if keep_current else unresolved).append(final_row)
    return unresolved, resolved


def _valid_cycle_memo(memo: object) -> bool:
    if not isinstance(memo, Mapping):
        return False
    candidates = memo.get("candidate_set")
    history = memo.get("history_receipts")
    evidence = memo.get("current_evidence")
    fingerprints = memo.get("evidence_authority_fingerprints")
    content_fingerprints = memo.get(
        "evidence_content_fingerprints"
    )
    final_text = memo.get("final_text")
    if not (
        memo.get("schema_version") == CYCLE_MEMO_SCHEMA
        and memo.get("status") == "LOCKED"
        and memo.get("decision_authority") == "CPA_JUDGE"
        and memo.get("witness_authority") == "EVIDENCE_ONLY"
        and isinstance(memo.get("choice_id"), str)
        and isinstance(final_text, str)
        and memo.get("final_text_sha256")
        == "sha256:" + _text_sha256(final_text)
        and isinstance(memo.get("matched_start_ms"), int)
        and not isinstance(memo.get("matched_start_ms"), bool)
        and isinstance(memo.get("matched_end_ms"), int)
        and not isinstance(memo.get("matched_end_ms"), bool)
        and int(memo["matched_start_ms"]) < int(memo["matched_end_ms"])
        and _valid_sha256(memo.get("origin_srt_sha256"))
        and _valid_sha256(memo.get("origin_current_cue_sha256"))
        and _valid_sha256(memo.get("origin_local_context_sha256"))
        and _valid_sha256(memo.get("bound_local_context_sha256"))
        and _valid_sha256(memo.get("candidate_set_sha256"))
        and _valid_sha256(memo.get("history_sha256"))
        and _valid_sha256(memo.get("current_evidence_sha256"))
        and _valid_sha256(
            memo.get("evidence_authority_fingerprints_sha256")
        )
        and _valid_sha256(
            memo.get("evidence_content_fingerprints_sha256")
        )
        and _valid_sha256(memo.get("prompt_sha256"))
        and _valid_sha256(memo.get("completion_sha256"))
        and isinstance(candidates, list)
        and len(candidates) >= 2
        and isinstance(history, list)
        and isinstance(evidence, list)
        and bool(evidence)
        and isinstance(fingerprints, list)
        and all(isinstance(value, str) for value in fingerprints)
        and isinstance(content_fingerprints, list)
        and all(
            isinstance(value, str)
            for value in content_fingerprints
        )
        and memo.get("timing_immutable") is True
    ):
        return False
    candidate_ids: set[str] = set()
    selected: Mapping[str, object] | None = None
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            return False
        text = candidate.get("text")
        candidate_id = candidate.get("candidate_id")
        if not (
            isinstance(text, str)
            and isinstance(candidate_id, str)
            and candidate_id == _candidate_id(text)
            and candidate.get("text_sha256")
            == "sha256:" + _text_sha256(text)
            and isinstance(candidate.get("roles"), list)
            and bool(candidate.get("roles"))
            and candidate_id not in candidate_ids
        ):
            return False
        candidate_ids.add(candidate_id)
        if candidate_id == memo.get("choice_id"):
            selected = candidate
    return bool(
        selected is not None
        and selected.get("text") == final_text
        and (final_text != "" or selected.get("legal_drop") is True)
        and memo.get("candidate_set_sha256")
        == "sha256:" + _json_sha256(candidates)
        and memo.get("history_sha256")
        == "sha256:" + _json_sha256(history)
        and memo.get("current_evidence_sha256")
        == "sha256:" + _json_sha256(evidence)
        and memo.get("evidence_authority_fingerprints_sha256")
        == "sha256:" + _json_sha256(fingerprints)
        and memo.get("evidence_content_fingerprints_sha256")
        == "sha256:" + _json_sha256(content_fingerprints)
    )


def _valid_memo(memo: object) -> bool:
    if not isinstance(memo, Mapping):
        return False
    final_text = memo.get("final_text")
    history = memo.get("history_receipts")
    new_evidence = memo.get("new_evidence")
    basic = bool(
        memo.get("schema_version") == MEMO_SCHEMA
        and memo.get("status") == "RESOLVED"
        and memo.get("decision_authority") == "CPA_JUDGE"
        and memo.get("witness_authority") == "EVIDENCE_ONLY"
        and memo.get("choice") in {"CURRENT", "PROPOSED"}
        and isinstance(final_text, str)
        and bool(final_text.strip())
        and memo.get("final_text_sha256") == "sha256:" + _text_sha256(final_text)
        and isinstance(memo.get("matched_start_ms"), int)
        and not isinstance(memo.get("matched_start_ms"), bool)
        and isinstance(memo.get("matched_end_ms"), int)
        and not isinstance(memo.get("matched_end_ms"), bool)
        and int(memo["matched_start_ms"]) < int(memo["matched_end_ms"])
        and _valid_sha256(memo.get("origin_srt_sha256"))
        and _valid_sha256(memo.get("origin_current_cue_sha256"))
        and _valid_sha256(memo.get("origin_proposed_cue_sha256"))
        and _valid_sha256(memo.get("origin_local_context_sha256"))
        and _valid_sha256(memo.get("bound_local_context_sha256"))
        and _valid_sha256(memo.get("history_sha256"))
        and _valid_sha256(memo.get("new_evidence_sha256"))
        and _valid_sha256(memo.get("origin_request_sha256"))
        and _valid_sha256(memo.get("prompt_sha256"))
        and _valid_sha256(memo.get("completion_sha256"))
        and isinstance(history, list)
        and bool(history)
        and isinstance(new_evidence, Mapping)
        and memo.get("timing_immutable") is True
    )
    if not basic:
        return False
    valid_history = all(
        isinstance(row, Mapping)
        and isinstance(row.get("before"), str)
        and isinstance(row.get("after"), str)
        and row.get("before_sha256") == "sha256:" + _text_sha256(str(row["before"]))
        and row.get("after_sha256") == "sha256:" + _text_sha256(str(row["after"]))
        and _valid_sha256(row.get("finding_sha256"))
        and _valid_sha256(row.get("request_sha256"))
        for row in history
    )
    return bool(
        valid_history
        and memo.get("history_sha256") == "sha256:" + _json_sha256(history)
        and memo.get("new_evidence_sha256") == "sha256:" + _json_sha256(new_evidence)
    )


def _valid_any_memo(memo: object) -> bool:
    return _valid_memo(memo) or _valid_cycle_memo(memo)


def exact_final_convergence_memo_identity(
    memo: Mapping[str, Any],
) -> str | None:
    """Return the stable identity of one valid CPA convergence memo.

    Context rebinding changes the memo's bound output/context fields while the
    same CPA response remains the same authority.  The prompt/completion pair
    and exact selected text/window therefore form the replay identity.
    """

    if not _valid_any_memo(memo):
        return None
    identity = {
        "schema_version": memo["schema_version"],
        "matched_start_ms": memo["matched_start_ms"],
        "matched_end_ms": memo["matched_end_ms"],
        "final_text_sha256": memo["final_text_sha256"],
        "prompt_sha256": memo["prompt_sha256"],
        "completion_sha256": memo["completion_sha256"],
    }
    return "sha256:" + _json_sha256(identity)


def valid_exact_final_convergence_memo_binding(
    memo: object,
    *,
    input_srt_sha256: str,
    before_text: str,
    after_text: str,
    before_sha256: str,
    after_sha256: str,
    matched_start_ms: int,
    matched_end_ms: int,
) -> bool:
    """Validate a memo against the exact self-heal repair it authorizes.

    The structural memo check deliberately reuses ``collect`` so this seam
    cannot grow a second, weaker convergence validator.  The additional
    fields bind that already-valid memo to the current SRT, cue text and
    immutable local window before a cycle-return repair can be recorded.
    """

    if (
        isinstance(memo, bool)
        or not isinstance(memo, Mapping)
        or not isinstance(before_text, str)
        or not isinstance(after_text, str)
        or not isinstance(input_srt_sha256, str)
        or not isinstance(before_sha256, str)
        or not isinstance(after_sha256, str)
        or isinstance(matched_start_ms, bool)
        or not isinstance(matched_start_ms, int)
        or isinstance(matched_end_ms, bool)
        or not isinstance(matched_end_ms, int)
        or matched_start_ms >= matched_end_ms
    ):
        return False
    schema = memo.get("schema_version")
    memo_key = (
        "exact_final_cpa_cycle_memo"
        if schema == CYCLE_MEMO_SCHEMA
        else "exact_final_cpa_convergence_memo"
        if schema == MEMO_SCHEMA
        else None
    )
    if memo_key is None:
        return False
    if len(
        collect_exact_final_convergence_memos(
            {
                "findings": [
                    {
                        "exact_release_adjudication": {
                            memo_key: dict(memo),
                        }
                    }
                ]
            }
        )
    ) != 1:
        return False
    identity = exact_final_convergence_memo_identity(memo)
    if identity is None:
        return False
    return bool(
        memo.get("matched_start_ms") == matched_start_ms
        and memo.get("matched_end_ms") == matched_end_ms
        and memo.get("final_text") == after_text
        and memo.get("final_text_sha256") == after_sha256
        and memo.get("origin_current_cue_sha256")
        == before_sha256
        and memo.get("origin_srt_sha256") == input_srt_sha256
        and before_sha256 == "sha256:" + _text_sha256(before_text)
        and after_sha256 == "sha256:" + _text_sha256(after_text)
    )


def _retain_superseded_cycle_blocks(
    row: dict[str, Any], *, memo: Mapping[str, Any], srt_text: str,
) -> None:
    """Keep an old same-cue BLOCK as history after a bound new CPA decision.

    Carryover rows include the earlier needs-audio control receipt. Leaving it
    current after successful convergence makes the normal self-heal consumer
    reject the new typed decision. A failed or drifted memo cannot retire it.
    """
    cue_index = row.get("cue_index")
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if (type(cue_index) is not int or not 1 <= cue_index <= len(cues)
            or memo.get("cue_index") != cue_index):
        return
    cue = cues[cue_index - 1]
    after = memo.get("final_text")
    if not isinstance(after, str) or not valid_exact_final_convergence_memo_binding(
        memo, input_srt_sha256="sha256:" + _text_sha256(srt_text),
        before_text=cue.text, after_text=after,
        before_sha256="sha256:" + _text_sha256(cue.text),
        after_sha256="sha256:" + _text_sha256(after),
        matched_start_ms=cue.start_ms, matched_end_ms=cue.end_ms,
    ):
        return
    gate = memo.get("acoustic_witness_gate")
    if gate is not None and not valid_convergence_witness_gate(
        gate, proposed=after, window=(cue.start_ms, cue.end_ms),
    ):
        return
    adjudication = row.get("exact_release_adjudication")
    owners = [row]
    if isinstance(adjudication, Mapping):
        copied = dict(adjudication)
        row["exact_release_adjudication"] = copied
        owners.append(copied)
    for owner in owners:
        previous = owner.get("exact_final_cpa_cycle_adjudication")
        history = owner.get("prior_exact_final_cpa_cycle_adjudications", [])
        if not (isinstance(previous, Mapping)
                and previous.get("schema_version") == CYCLE_RECEIPT_SCHEMA
                and previous.get("status") == "BLOCK"
                and previous.get("matched_start_ms") == cue.start_ms
                and previous.get("matched_end_ms") == cue.end_ms
                and previous.get("current_cue_sha256")
                == "sha256:" + _text_sha256(cue.text)
                and isinstance(history, list)):
            continue
        retained = list(history)
        if previous not in retained:
            retained.append(dict(previous))
        owner["prior_exact_final_cpa_cycle_adjudications"] = retained
        owner.pop("exact_final_cpa_cycle_adjudication")


def collect_exact_final_convergence_memos(
    audit: Mapping[str, object],
) -> list[dict[str, Any]]:
    """Collect typed memos from current unresolved and resolved audit rows."""

    by_window: dict[tuple[int, int], dict[str, Any]] = {}
    for bucket in (
        audit.get("findings"),
        audit.get("resolved_findings"),
        audit.get("unresolved_findings_disclosed"),
    ):
        for row in bucket if isinstance(bucket, list) else []:
            adjudication = (
                row.get("exact_release_adjudication") if isinstance(row, Mapping) else None
            )
            memo = (
                (
                    adjudication.get("exact_final_cpa_cycle_memo")
                    or adjudication.get(
                        "exact_final_cpa_convergence_memo"
                    )
                )
                if isinstance(adjudication, Mapping)
                else None
            )
            if not _valid_any_memo(memo):
                continue
            key = (
                int(memo["matched_start_ms"]),
                int(memo["matched_end_ms"]),
            )
            by_window[key] = dict(memo)
    return list(by_window.values())


def rebind_exact_final_convergence_memos(
    srt_text: str,
    memos: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Advance valid memos across other CPA-authorized repairs in one pass.

    The convergence judge runs before the producer atomically applies every
    repair from that audit.  A neighboring cue can therefore change in the
    same output and legitimately alter the local-context hash.  Rebind only
    when the memo's own exact text and time window survive; record both hashes
    and the complete output SRT hash so this is auditable, not a loose remap.
    """

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    output_srt_sha256 = "sha256:" + _text_sha256(srt_text)
    rebound: list[dict[str, Any]] = []
    for raw in memos:
        if not _valid_any_memo(raw):
            continue
        memo = dict(raw)
        matches = [
            (index, cue)
            for index, cue in enumerate(cues, start=1)
            if cue.start_ms == memo["matched_start_ms"]
            and cue.end_ms == memo["matched_end_ms"]
            and cue.text == memo["final_text"]
        ]
        if len(matches) != 1:
            rebound.append(memo)
            continue
        cue_index, _cue = matches[0]
        context_sha256, _before, _after = _local_context_binding(
            srt_text,
            cue_index,
        )
        after_binding = "sha256:" + context_sha256
        before_binding = str(memo["bound_local_context_sha256"])
        if before_binding != after_binding:
            history = memo.get("context_rebindings")
            history_rows = list(history) if isinstance(history, list) else []
            history_rows.append(
                {
                    "schema_version": ("exact-final-cpa-convergence-context-rebind.v1"),
                    "status": "PASS",
                    "basis": ("SAME_PASS_CPA_AUTHORIZED_REPAIRS_FINAL_OUTPUT"),
                    "before_local_context_sha256": before_binding,
                    "after_local_context_sha256": after_binding,
                    "output_srt_sha256": output_srt_sha256,
                    "own_final_text_sha256": memo["final_text_sha256"],
                    "timing_immutable": True,
                }
            )
            memo["context_rebindings"] = history_rows
            memo["bound_local_context_sha256"] = after_binding
        memo["bound_output_srt_sha256"] = output_srt_sha256
        rebound.append(memo)
    return rebound


def resolve_findings_from_exact_final_convergence_memos(
    srt_text: str,
    findings: Iterable[Mapping[str, Any]],
    *,
    authority_audit: Mapping[str, object],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Close repeated scans only while the memo's exact local context survives."""

    memos = authority_audit.get("exact_final_cpa_convergence_memos")
    valid_memos = [
        dict(memo)
        for memo in (memos if isinstance(memos, list) else [])
        if _valid_any_memo(memo)
        # Historical structural validity preserves the original typed repair
        # chain.  Reusing it to close today's review additionally requires its
        # claimed acoustic authority to satisfy the current normal contract.
        # In particular, a legacy judge still asking for audio cannot become
        # KEEP merely because the memo's text and context hashes match.
        and (
            memo.get("acoustic_witness_gate") is None
            or valid_convergence_witness_gate(
                memo["acoustic_witness_gate"],
                proposed=memo["final_text"],
                window=(memo["matched_start_ms"], memo["matched_end_ms"]),
            )
        )
    ]
    if not valid_memos:
        return [dict(row) for row in findings], []
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    pending: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for finding in findings:
        try:
            cue_index = int(finding.get("cue_index") or 0)
        except (TypeError, ValueError):
            cue_index = 0
        if not 1 <= cue_index <= len(cues):
            pending.append(dict(finding))
            continue
        cue = cues[cue_index - 1]
        context_sha256, _before, _after = _local_context_binding(
            srt_text,
            cue_index,
        )
        matched = next(
            (
                memo
                for memo in reversed(valid_memos)
                if memo["matched_start_ms"] == cue.start_ms
                and memo["matched_end_ms"] == cue.end_ms
                and memo["final_text"] == cue.text
                and memo["final_text_sha256"] == "sha256:" + _text_sha256(cue.text)
                and memo["bound_local_context_sha256"] == "sha256:" + context_sha256
            ),
            None,
        )
        if matched is None:
            pending.append(dict(finding))
            continue
        if matched.get("schema_version") == CYCLE_MEMO_SCHEMA:
            proposed = _cycle_candidate_from_finding(finding)
            if proposed is None and isinstance(
                finding.get("proposed_full_cue"),
                str,
            ):
                # Raw discovery rows remain replay-compatible; adjudicated
                # rows use their effective CPA request target above.
                proposed = str(finding["proposed_full_cue"])
            candidate_ids = {
                str(candidate.get("candidate_id"))
                for candidate in matched.get("candidate_set") or []
                if isinstance(candidate, Mapping)
            }
            new_fingerprints = set(
                _evidence_authority_fingerprints(finding)
            )
            known_fingerprints = set(
                str(value)
                for value in (
                    matched.get("evidence_authority_fingerprints")
                    or []
                )
            )
            new_content_fingerprints = set(
                _evidence_content_fingerprints(finding)
            )
            known_content_fingerprints = set(
                str(value)
                for value in (
                    matched.get("evidence_content_fingerprints")
                    or []
                )
            )
            if (
                not isinstance(proposed, str)
                or _candidate_id(proposed) not in candidate_ids
                or not new_fingerprints.issubset(known_fingerprints)
                or not new_content_fingerprints.issubset(
                    known_content_fingerprints
                )
            ):
                # A genuinely new candidate, source/audio identity, or
                # materially changed witness observation reopens CPA.
                pending.append(dict(finding))
                continue
        row = dict(finding)
        memo_request = {
            "schema_version": (
                "exact-final-cpa-cycle-memo-replay.v1"
                if matched.get("schema_version") == CYCLE_MEMO_SCHEMA
                else "exact-final-cpa-convergence-memo-replay.v1"
            ),
            "cue_index": cue_index,
            "matched_start_ms": cue.start_ms,
            "matched_end_ms": cue.end_ms,
            "current_cue_sha256": _text_sha256(cue.text),
            "bound_local_context_sha256": context_sha256,
            "memo_prompt_sha256": matched["prompt_sha256"],
            "timing_immutable": True,
        }
        memo_request["request_sha256"] = _json_sha256(memo_request)
        cycle_memo = matched.get("schema_version") == CYCLE_MEMO_SCHEMA
        row["resolution"] = (
            "CPA_EXACT_FINAL_CYCLE_MEMO_LOCKED_FINAL_TEXT"
            if cycle_memo
            else "CPA_HISTORY_CONVERGENCE_MEMO_FINAL_TEXT"
        )
        row["exact_release_adjudication"] = {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "repaired": False,
            "policy_branch": (
                "CPA_EXACT_FINAL_CYCLE_MEMO_KEEP_FINAL"
                if cycle_memo
                else "CPA_HISTORY_CONVERGENCE_MEMO_KEEP_FINAL"
            ),
            "timing_immutable": True,
            "request": memo_request,
            "verdict": {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "status": "NOT_REQUIRED",
                "target_audible": None,
                "reason_code": (
                    "CPA_EXACT_FINAL_CYCLE_MEMO"
                    if cycle_memo
                    else "CPA_HISTORY_CONVERGENCE_MEMO"
                ),
            },
            "witness_judge": {
                "witness_status": "MEMO_REPLAY",
                "judge": {
                    "schema_version": (
                        "exact-final-cpa-cycle-judge.v1"
                        if cycle_memo
                        else "exact-final-cpa-convergence-judge.v1"
                    ),
                    "status": "JUDGED",
                    "choice": "CURRENT",
                    "reason": matched.get("reason"),
                    "prompt_sha256": matched.get("prompt_sha256"),
                    "completion_sha256": matched.get("completion_sha256"),
                },
            },
            (
                "exact_final_cpa_cycle_memo"
                if cycle_memo
                else "exact_final_cpa_convergence_memo"
            ): matched,
            "decision_authority": "CPA_JUDGE",
            "witness_authority": "EVIDENCE_ONLY",
            "mutation_authority": {
                "schema_version": ("subtitle-correction-mutation-authority.v1"),
                "status": "NOT_APPLIED",
                "basis": (
                    "CPA_EXACT_FINAL_CYCLE_MEMO_KEEP_FINAL"
                    if cycle_memo
                    else "CPA_HISTORY_CONVERGENCE_MEMO_KEEP_FINAL"
                ),
            },
        }
        resolved.append(row)
    return pending, resolved
