"""Terminal B/C consumption of real span obligations."""

from copy import deepcopy

import pytest

from src.autoslice import context_assisted_audio
from src.autoslice.span_finding_obligations import (
    _valid_closing_adjudication,
    derive_obligations,
    enforce_review,
)
from src.autoslice.span_phoneme_resolution import digest, outside_question_packet
from tests.test_span_phoneme_resolution import model_for, run_case, sha, srt
from tests.test_targeted_relisten_public_contract import build_offline_rig
from tests.test_terminal_contract_roundtrip import c_reply, produce


_SRT = (
    "1\n00:00:01,000 --> 00:00:04,000\n你想，甲0呀\n\n"
    "2\n00:00:04,000 --> 00:00:05,000\n中间\n\n"
    "3\n00:00:05,000 --> 00:00:07,000\n刚才只是在哼唱\n\n"
    "4\n00:00:07,000 --> 00:00:09,000\n问的是前半句\n"
)
_FINDING = {
    "cue_index": 1,
    "suspect": "你想",
    "suggestion": "你很想",
    "proposed_full_cue": "你很想，甲0呀",
    "repair_class": "spoken_unit",
    "content_risk": "factual_error",
    "risk_basis": "测试终裁保留当前整句。",
}


def _supported(data, refs):
    return {
        **c_reply(data, refs),
        "grade": "B",
        "keep_route": "B_EVIDENCE_SUPPORTS_CURRENT",
        "evidence_decisive": True,
        "comparison_text": data["current"],
        "evidence_refs": data["current_support_observation_refs"],
        "meaning_equivalence": None,
    }


def _assisted_plan():
    return {
        "decision": "OBSERVE",
        "provider": "gemini",
        "first_cue": 1,
        "last_cue": 2,
        "observation_class": context_assisted_audio.OBSERVATION_CLASS,
        "purpose": "SOURCED_CONTEXT",
        "reason": "用相邻语境核对目标句。",
        "question": "目标句是否完整？",
        "context_refs": ["cue:3"],
        "context_summary": "相邻语境核对目标句。",
        "proper_names": [],
        "include_candidates": True,
    }


def _terminal_audit(tmp_path, monkeypatch, reply):
    """Build a real terminal audit whose current cue is a span-fixture cue."""
    import tests.test_targeted_relisten_public_contract as public_contract

    monkeypatch.setattr(public_contract, "SRT", _SRT)
    monkeypatch.setattr(public_contract, "assisted_plan", _assisted_plan)
    rig = build_offline_rig(
        tmp_path / "terminal-rig",
        monkeypatch,
        srt=_SRT,
        finding=_FINDING,
        source_duration_ms=10_000,
    )
    audit, captured = produce(rig, reply, native=True)
    return audit, captured


def _origin(row):
    return {
        "obligation_id": row["obligation_id"],
        "codepoint_span": list(row["codepoint_span"]),
        "base_text_sha256": row["base_text_sha256"],
        "cue_window_ms": list(row["cue_window_ms"]),
        "obligation_ids": [row["obligation_id"]],
        "source": deepcopy(row["origin"]),
    }


def _real_unapplied_authority():
    """Make two valid packets from the real span adjudicator, not fake rows."""
    packets = []
    for question_span in ([0, 2], [0, 3]):
        _, adjudication, _, _, _ = run_case(
            model=model_for(question_span=question_span)
        )
        packet = outside_question_packet(
            adjudication["request"],
            adjudication["verdict"],
            adjudication["witness_judge"]["judge"],
        )
        assert packet is not None
        packets.append(packet)
    return {
        "schema_version": "terminal-rework2-test-authority.v1",
        "span_unapplied_questions": [
            {
                "after_repair_count": 0,
                "repair_prefix_sha256": digest([]),
                "packet": packet,
            }
            for packet in packets
        ],
    }


def test_real_terminal_b_closes_matching_span_obligation(tmp_path, monkeypatch):
    audit, _ = _terminal_audit(tmp_path, monkeypatch, _supported)
    request = audit["request"]
    assert _valid_closing_adjudication(
        audit,
        before=request["current_cue"],
        after=request["current_cue"],
        window=[request["matched_start_ms"], request["matched_end_ms"]],
        keep=True,
    )


@pytest.mark.parametrize(
    "damage",
    ["window", "whole_cue", "request_hash", "raw_completion", "observation"],
)
def test_terminal_b_rejects_tampered_or_incomplete_real_evidence(
    damage, tmp_path, monkeypatch
):
    audit, _ = _terminal_audit(tmp_path, monkeypatch, _supported)
    original = audit["request"]
    damaged = deepcopy(audit)
    if damage == "window":
        damaged["request"]["matched_end_ms"] += 1
    elif damage == "whole_cue":
        damaged["request"]["current_cue"] = "改动后的整句"
    elif damage == "request_hash":
        damaged["request"]["request_sha256"] = "0" * 64
    elif damage == "raw_completion":
        damaged["terminal_review"]["raw_completion"] = "{}"
    else:
        observations = damaged["terminal_review"]["request"]["audio_gain_termination"][
            "projection"
        ]["observations"]
        assert observations
        observations.pop()
    assert not _valid_closing_adjudication(
        damaged,
        before=original["current_cue"],
        after=original["current_cue"],
        window=[original["matched_start_ms"], original["matched_end_ms"]],
        keep=True,
    )


def test_real_terminal_c_cannot_close_an_independent_span(tmp_path, monkeypatch):
    audit, _ = _terminal_audit(tmp_path, monkeypatch, c_reply)
    request = audit["request"]
    assert audit["keep_route"] == "C_MEANING_EQUIVALENT"
    assert not _valid_closing_adjudication(
        audit,
        before=request["current_cue"],
        after=request["current_cue"],
        window=[request["matched_start_ms"], request["matched_end_ms"]],
        keep=True,
    )


def test_enforce_review_closes_matching_origin_and_retains_unrelated_pending(
    tmp_path, monkeypatch
):
    audit, _ = _terminal_audit(tmp_path, monkeypatch, _supported)
    authority = _real_unapplied_authority()
    rows = derive_obligations(
        [],
        closures=(),
        unapplied=authority["span_unapplied_questions"],
    )
    assert len(rows) == 2
    matching, unrelated = rows
    current_srt = srt("你想，甲0呀")
    finding = {
        **audit["rebuilt_finding"],
        "exact_release_adjudication": audit,
        "origin_span_obligation": _origin(matching),
        "base_text_sha256": matching["base_text_sha256"],
        "span_start_codepoint": matching["codepoint_span"][0],
        "span_end_codepoint": matching["codepoint_span"][1],
        "cue_index": matching["cue_index"],
    }
    review = {
        "schema_version": "final-review-audit.v2",
        "reviewed_srt_sha256": "sha256:" + sha(current_srt),
        "findings": [],
        "resolved_findings": [],
        "unresolved_findings_disclosed": [],
    }
    from scripts.predeploy_contract_gauntlet import ordinary_span_closure
    from src.autoslice.review_evidence_store import persist_review
    source = tmp_path / 'candidate.review-flags.json'
    packed = persist_review(tmp_path / 'candidate.chat-authority.json', authority)
    assert packed != authority  # Exercise the real compact reader boundary too.
    enforced = ordinary_span_closure({'file': str(source)}, {'srt': current_srt, 'audit': {
        'reviewed_srt_sha256': review['reviewed_srt_sha256'],
        'unresolved_findings_disclosed': [finding],
    }}, review)
    states = {row["obligation_id"]: row["status"] for row in enforced["span_obligation_review"]["states"]}
    assert states[matching["obligation_id"]] == "RESOLVED"
    assert states[unrelated["obligation_id"]] == "UNRESOLVED"
    assert len(enforced["span_obligation_review"]["closures"]) == 1
    assert enforced["findings"]
    assert unrelated["obligation_id"] in enforced["findings"][0]["origin_span_obligation"]["obligation_ids"]
    assert enforced["status"] == "FLAGGED"
    assert enforced["release_gate"] == "BLOCK"
    with pytest.raises(ValueError, match='REPLAY_ORIGINAL_KEEP_SRT_DRIFT'):
        ordinary_span_closure({'file': str(source)}, {'srt': current_srt, 'audit': {
            'reviewed_srt_sha256': 'sha256:' + '0' * 64,
            'unresolved_findings_disclosed': [finding],
        }}, deepcopy(review))
