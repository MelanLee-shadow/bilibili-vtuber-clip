from __future__ import annotations

import threading
import time

import src.autoslice.microcue_acoustic_discovery as microcue_module
from src.autoslice.microcue_acoustic_discovery import discover_microcue_findings


SRT = """1
00:00:00,000 --> 00:00:01,121
好爽哦

2
00:00:02,000 --> 00:00:04,000
这是一句比较长的话
"""


def _witness(heard: str, *, confidence: float = 0.95):
    def verify(request):
        assert "current_cue" not in request
        assert "proposed_cue" not in request
        assert "candidate_entities" not in request
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": heard,
            "uncertain_positions": [],
            "syllable_count": len(heard.split()),
            "confidence": confidence,
        }

    return verify


def test_microcue_mismatch_creates_proposal_less_cpa_finding():
    findings, audit = discover_microcue_findings(
        SRT,
        timeline_offset_ms=10_000,
        entity_verifier=_witness("hao piao liang o"),
    )

    assert audit["status"] == "PASS"
    assert audit["candidate_text_exposed_to_witness"] is False
    assert audit["eligible_count"] == 1
    assert audit["finding_count"] == 1
    assert findings[0]["cue"] == 1
    assert findings[0]["suspect"] == "好爽哦"
    assert findings[0]["proposed_full_cue"] is None
    assert "hao piao liang o" in findings[0]["why"]


def test_matching_microcue_does_not_create_finding():
    findings, audit = discover_microcue_findings(
        SRT,
        timeline_offset_ms=10_000,
        entity_verifier=_witness("hao shuang o"),
    )

    assert findings == []
    assert audit["finding_count"] == 0
    assert audit["eligible"][0]["status"] == "OBSERVED"


def test_implausible_syllable_count_is_not_promoted_to_cpa():
    findings, audit = discover_microcue_findings(
        SRT,
        timeline_offset_ms=10_000,
        entity_verifier=_witness("en"),
    )

    assert findings == []
    assert audit["finding_count"] == 0
    assert audit["eligible"][0]["status"] == "SYLLABLE_COUNT_OUTLIER"


def test_uncertain_microcue_is_disclosed_without_blocking():
    def verify(request):
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "UNCERTAIN",
            "reason_code": "AGY_QUOTA_EXHAUSTED",
        }

    findings, audit = discover_microcue_findings(
        SRT,
        timeline_offset_ms=10_000,
        entity_verifier=verify,
    )

    assert findings == []
    assert audit["status"] == "PARTIAL"
    assert audit["uncertain_count"] == 1


def test_inaudible_microcue_nominates_empty_drop_but_does_not_mutate():
    findings, audit = discover_microcue_findings(
        "1\n00:00:00,000 --> 00:00:01,121\n咳咳\n",
        timeline_offset_ms=9_660,
        entity_verifier=lambda request: {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": False,
            "heard_pinyin": "",
            "syllable_count": 0,
            "uncertain_positions": [],
            "confidence": 1.0,
        },
    )

    assert findings == [
        {
            "cue": 1,
            "kind": "context",
            "proposed_full_cue": "",
            "repair_class": "acoustic_drop_cue",
            "source_surface": None,
            "candidate_memory_id": None,
            "evidence_cue_ids": [],
            "suspect": "咳咳",
                "replacement": "",
                "why": (
                    "候选无关短句声学巡检确认整条目标时窗无可闻语音且音节数为 0；"
                    "CPA 随后必须在 CURRENT/PROPOSED/DROP typed 三选一"
                    "中明确选择 DROP 才可整 cue 删除"
                ),
        }
    ]
    assert audit["status"] == "PASS"
    assert audit["uncertain_count"] == 0
    assert audit["eligible"][0]["status"] == (
        "INAUDIBLE_DROP_PROPOSED_TO_CPA"
    )
    assert audit["mutation_authorized"] is False


def test_functional_prefix_tail_mismatch_survives_whole_cue_dilution():
    srt = "1\n00:00:00,000 --> 00:00:01,600\n谢谢你呀蓝莓星\n"

    findings, audit = discover_microcue_findings(
        srt,
        timeline_offset_ms=0,
        entity_verifier=_witness("xie xie ni ya lan mei dao"),
    )

    row = audit["eligible"][0]
    assert audit["eligible_count"] == 1
    assert row["eligibility_scope"] == "functional_prefix_extended"
    assert row["functional_prefix"] == "谢谢你呀"
    assert row["comparison_scope"] == "functional_prefix_tail"
    assert row["whole_cue_pinyin_similarity"] > (
        microcue_module.MAX_PINYIN_SIMILARITY_FOR_FINDING
    )
    assert row["tail_pinyin_similarity"] < (
        microcue_module.MAX_PINYIN_SIMILARITY_FOR_FINDING
    )
    assert audit["finding_count"] == 1
    assert findings[0]["comparison_scope"] == "functional_prefix_tail"
    assert findings[0]["suspect"] == "蓝莓星"
    assert findings[0]["whole_cue_pinyin_similarity"] == row[
        "whole_cue_pinyin_similarity"
    ]
    assert findings[0]["tail_pinyin_similarity"] == row[
        "tail_pinyin_similarity"
    ]


def test_matching_functional_tail_and_pure_thanks_do_not_report():
    srt = """1
00:00:00,000 --> 00:00:01,600
谢谢你呀蓝莓星

2
00:00:03,000 --> 00:00:04,600
谢谢大家
"""

    def verify(request):
        heard = (
            "xie xie ni ya lan mei xing"
            if _cue_index_from_request(request) == 1
            else "xie xie da jia"
        )
        return _witness(heard)(request)

    findings, audit = discover_microcue_findings(
        srt,
        timeline_offset_ms=0,
        entity_verifier=verify,
    )

    assert findings == []
    assert audit["finding_count"] == 0
    matching_row = next(
        row for row in audit["eligible"] if row["cue_index"] == 1
    )
    pure_thanks_row = next(
        row for row in audit["eligible"] if row["cue_index"] == 2
    )
    assert matching_row["comparison_scope"] == "functional_prefix_tail"
    assert matching_row["tail_pinyin_similarity"] == 1.0
    assert pure_thanks_row["functional_prefix"] == "谢谢大家"
    assert pure_thanks_row["comparison_scope"] == "whole_cue"
    assert pure_thanks_row["tail_pinyin_similarity"] is None


def test_functional_prefix_mismatch_keeps_whole_cue_rule():
    srt = "1\n00:00:00,000 --> 00:00:01,600\n谢谢你呀蓝莓星\n"

    findings, audit = discover_microcue_findings(
        srt,
        timeline_offset_ms=0,
        entity_verifier=_witness("xie xie ta ya lan mei xing"),
    )

    row = audit["eligible"][0]
    assert findings == []
    assert row["functional_prefix"] == "谢谢你呀"
    assert row["functional_prefix_aligned"] is False
    assert row["comparison_scope"] == "whole_cue"
    assert row["tail_pinyin_similarity"] is None
    assert row["whole_cue_pinyin_similarity"] > (
        microcue_module.MAX_PINYIN_SIMILARITY_FOR_FINDING
    )


def test_functional_duration_extension_is_bounded_and_total_budget_is_unchanged():
    assert microcue_module._eligible(
        "谢谢你呀蓝莓星", microcue_module.FUNCTIONAL_MAX_DURATION_MS
    )
    assert not microcue_module._eligible(
        "谢谢你呀蓝莓星", microcue_module.FUNCTIONAL_MAX_DURATION_MS + 1
    )
    assert not microcue_module._eligible("普通长句内容", 2_000)

    srt = (
        _multi_cue_srt(microcue_module.MAX_CUES)
        + "\n13\n00:00:24,000 --> 00:00:26,000\n普通长句内容\n"
    )
    findings, audit = discover_microcue_findings(
        srt,
        timeline_offset_ms=0,
        entity_verifier=_witness("hao kai xin"),
    )

    assert len(findings) <= microcue_module.MAX_CUES
    assert audit["eligible_count"] == microcue_module.MAX_CUES
    assert len(audit["eligible"]) == microcue_module.MAX_CUES
    assert all(row["cue_index"] != 13 for row in audit["eligible"])


# --------------------------------------------------------------------------
# Bounded concurrency for independent per-cue witness calls
# --------------------------------------------------------------------------

_MULTI_CUE_TEXTS = [
    "好开心",
    "太厉害",
    "真好玩",
    "有点晕",
    "好紧张",
    "太意外",
    "真感动",
    "好惊喜",
]


def _fmt_ts(ms: int) -> str:
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _multi_cue_srt(count: int) -> str:
    """``count`` distinct 1s eligible microcues, one every 2s."""

    blocks = []
    for i in range(count):
        start_ms = i * 2_000
        end_ms = start_ms + 1_000
        blocks.append(
            f"{i + 1}\n{_fmt_ts(start_ms)} --> {_fmt_ts(end_ms)}\n"
            f"{_MULTI_CUE_TEXTS[i % len(_MULTI_CUE_TEXTS)]}\n"
        )
    return "\n".join(blocks)


def _cue_index_from_request(request) -> int:
    indexes = request["cue_indexes"]
    assert len(indexes) == 1
    return int(indexes[0])


def test_microcue_witness_calls_run_up_to_the_concurrency_cap():
    """Independent per-cue witness calls overlap, bounded by the module cap."""

    lock = threading.Lock()
    in_flight = 0
    max_observed = 0
    seen_request_shas: set[str] = set()

    def verify(request):
        nonlocal in_flight, max_observed
        assert request["request_sha256"] not in seen_request_shas, (
            "two calls shared a request_sha256 -- would race on the same "
            "content-addressed cache/job_dir entry"
        )
        seen_request_shas.add(request["request_sha256"])
        with lock:
            in_flight += 1
            max_observed = max(max_observed, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": "hao kai xin",
            "uncertain_positions": [],
            "syllable_count": 3,
            "confidence": 0.95,
        }

    srt = _multi_cue_srt(8)
    findings, audit = discover_microcue_findings(
        srt, timeline_offset_ms=0, entity_verifier=verify
    )

    assert audit["eligible_count"] == 8
    assert max_observed == microcue_module.MICROCUE_WITNESS_CONCURRENCY
    assert len(seen_request_shas) == 8


def test_microcue_parallel_output_matches_serial_output_byte_for_byte():
    """Concurrency-4 output must equal single-worker output, in input order."""

    def verify(request):
        cue_index = _cue_index_from_request(request)
        # Deterministic but varied per-cue outcome: mismatch / match /
        # syllable outlier / uncertain, cycling by cue index.
        branch = cue_index % 4
        if branch == 0:
            return {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "request_sha256": request["request_sha256"],
                "status": "OBSERVED",
                "target_audible": True,
                "heard_pinyin": "hen bu kai xin",
                "uncertain_positions": [],
                "syllable_count": 4,
                "confidence": 0.95,
            }
        if branch == 1:
            return {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "request_sha256": request["request_sha256"],
                "status": "OBSERVED",
                "target_audible": True,
                "heard_pinyin": "tai li hai",
                "uncertain_positions": [],
                "syllable_count": 3,
                "confidence": 0.95,
            }
        if branch == 2:
            return {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "request_sha256": request["request_sha256"],
                "status": "OBSERVED",
                "target_audible": True,
                "heard_pinyin": "en",
                "uncertain_positions": [],
                "syllable_count": 1,
                "confidence": 0.95,
            }
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "UNCERTAIN",
            "reason_code": "AGY_QUOTA_EXHAUSTED",
        }

    srt = _multi_cue_srt(8)

    findings_parallel, audit_parallel = discover_microcue_findings(
        srt, timeline_offset_ms=0, entity_verifier=verify
    )

    original_concurrency = microcue_module.MICROCUE_WITNESS_CONCURRENCY
    microcue_module.MICROCUE_WITNESS_CONCURRENCY = 1
    try:
        findings_serial, audit_serial = discover_microcue_findings(
            srt, timeline_offset_ms=0, entity_verifier=verify
        )
    finally:
        microcue_module.MICROCUE_WITNESS_CONCURRENCY = original_concurrency

    assert findings_parallel == findings_serial
    assert audit_parallel == audit_serial
    # Sanity: the fixture actually exercises multiple distinct branches
    # (findings + syllable outlier + uncertain), so equality above is not
    # vacuous.
    assert audit_parallel["finding_count"] == 4
    assert audit_parallel["uncertain_count"] == 2
    assert audit_parallel["syllable_outlier_count"] == 2
    assert [row["cue_index"] for row in audit_parallel["eligible"]] == list(
        range(1, 9)
    )


_MULTI_CUE_HEARD_PINYIN = [
    "hao kai xin",
    "tai li hai",
    "zhen hao wan",
    "you dian yun",
    "hao jin zhang",
    "tai yi wai",
    "zhen gan dong",
    "hao jing xi",
]


def test_microcue_single_witness_failure_stays_fail_closed_and_isolated():
    """One cue's verifier exception must not corrupt or drop sibling cues."""

    def verify(request):
        cue_index = _cue_index_from_request(request)
        if cue_index == 3:
            raise RuntimeError("boom")
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": _MULTI_CUE_HEARD_PINYIN[cue_index - 1],
            "uncertain_positions": [],
            "syllable_count": 3,
            "confidence": 0.95,
        }

    srt = _multi_cue_srt(8)
    findings, audit = discover_microcue_findings(
        srt, timeline_offset_ms=0, entity_verifier=verify
    )

    assert audit["eligible_count"] == 8
    assert audit["status"] == "PARTIAL"
    assert audit["uncertain_count"] == 1
    failed_row = next(
        row for row in audit["eligible"] if row["cue_index"] == 3
    )
    assert failed_row["status"] == "UNCERTAIN"
    assert failed_row["witness"]["reason_code"] == (
        "MICROCUE_AUDIO_VERIFIER_ERROR"
    )
    # Every other cue still got a real verdict, unaffected by the failure.
    other_rows = [row for row in audit["eligible"] if row["cue_index"] != 3]
    assert all(row["status"] == "OBSERVED" for row in other_rows)


def test_microcue_concurrency_cap_is_module_level_and_conservative():
    assert microcue_module.MICROCUE_WITNESS_CONCURRENCY == 4
