from __future__ import annotations

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
