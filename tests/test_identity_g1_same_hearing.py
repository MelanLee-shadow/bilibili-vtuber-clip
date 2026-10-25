"""G1 (identity sweep): a saved hearing is reused by its own request, not by
the current request hash (which mixes evidence_id = whole-SRT hash + wording, cue
ordinals and the syllable hint).  41: 「候选文字…变化不是新声学材料」; 「已保存观察按它
自己的原请求投影…嵌套观察保留其加候选前的原请求绑定，仍须覆盖当前目标窗与时钟」.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.autoslice.acoustic_witness_adjudication import (
    build_witness_request,
    hearing_binding,
    own_witness_request,
    same_hearing,
)
from src.autoslice.final_review_auditor import build_context_adjudication_request


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}")
    return "\n\n".join(blocks) + "\n"


def _check(source=None, *, offset=10630):
    return build_context_adjudication_request(source or _srt("上班好累"), {
        "cue_index": 1, "suspect": "好", "suggestion": "很", "proposed_full_cue": "上班很累",
    }, source_media_timeline_offset_ms=offset)


def _earlier(check, evidence_id="independent-source-request"):
    """The candidate-free request an independent route heard: same geometry, other evidence_id."""
    return build_witness_request({**check, "evidence_id": evidence_id})


def _native_row(witness_request, *, transcript="上班好累"):
    from src.autoslice.local_asr_target_evidence import MODELS, SCHEMA, digest
    from src.autoslice.native_context_witness import _native_transcript_witness, _prepare_request

    prepared = _prepare_request(witness_request)
    receipt = {
        "schema_version": SCHEMA, "status": "OBSERVED", "provider": "mai", "model": MODELS["mai"],
        "candidate_exposure": "none", "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
        "source_media_sha256": "a" * 64, "input_audio_sha256": "b" * 64, "response_sha256": "c" * 64,
        "target_start_ms": prepared.physical_start_ms, "target_end_ms": prepared.physical_end_ms,
        "crop_is_exact_target": True, "observation_scope": "complete_exact_cue_no_padding",
        "transcript": transcript, "native_segments": [],
    }
    receipt["receipt_sha256"] = digest(receipt)
    return _native_transcript_witness(request=witness_request, prepared=prepared,
                                      native_observation=receipt, source_sha256="a" * 64, provider="mai")


def test_same_hearing_exact_bound_and_unbound():
    check = _check()
    target = build_witness_request(check)
    earlier = _earlier(check)
    assert earlier["request_sha256"] != target["request_sha256"]
    row = _native_row(earlier)
    assert hearing_binding(row, earlier) == "EXACT"
    assert hearing_binding(row, target, bound_request=earlier) == "BOUND"
    # A bare foreign hash without its own request is not a hearing of this target.
    assert same_hearing(row, target) is False
    tampered = {**earlier, "evidence_id": "changed-without-rehash"}
    assert same_hearing(row, target, bound_request=tampered) is False
    moved = build_witness_request({**check, "evidence_id": "independent-source-request",
                                   "matched_start_ms": check["matched_start_ms"] + 40})
    assert same_hearing(_native_row(moved), target, bound_request=moved) is False


def test_same_hearing_bound_request_on_another_clock_is_the_same_physical_target():
    padded = _check(offset=10630)
    delivery_source = "1\n00:00:00,000 --> 00:00:04,000\n上班好累\n"
    delivery = _check(delivery_source, offset=15630)
    assert (padded["matched_start_ms"] + 10630) == (delivery["matched_start_ms"] + 15630)
    earlier = _earlier(padded)
    row = _native_row(earlier)
    assert hearing_binding(row, build_witness_request(delivery), bound_request=earlier) == "BOUND"


def test_own_witness_request_only_accepts_a_self_hashing_payload_of_the_rows_binding():
    check = _check()
    earlier = _earlier(check)
    row = _native_row(earlier)
    assert own_witness_request(row, None, build_witness_request(check), earlier) is earlier
    assert own_witness_request(row, {**earlier, "evidence_id": "x"}) is None
    assert own_witness_request({**row, "request_sha256": "c" * 64}, earlier) is None


def test_catalogue_projects_candidate_source_rows_against_their_travelling_own_request():
    """The guimi F2 shape: independent-route MAI rows heard for the candidate-free
    request, catalogued under a later request whose evidence_id differs."""
    from src.autoslice.span_phoneme_resolution import observation_catalogue

    check = _check()
    earlier = _earlier(check)
    row = _native_row(earlier)
    base = {**check, "proposed_candidates": [{"text": "上班好累", "source": row}]}

    _, invalid = observation_catalogue(base, {})
    assert [r["reason_code"] for r in invalid] == ["SPAN_NATIVE_TRANSCRIPT_RECEIPT_INVALID"]

    with_generator = {**base, "independent_asr_evidence": {"witness_request": earlier}}
    result, invalid = observation_catalogue(with_generator, {})
    rows = list(result.values()) if isinstance(result, dict) else list(result)
    assert invalid == []
    assert [r["kind"] for r in rows] == ["native_transcript"]
    assert rows[0]["witness_request"]["request_sha256"] == earlier["request_sha256"]

    paired = {**base, "prior_acoustic_observations": [
        {"witness_request": earlier, "verdict": deepcopy(row)}]}
    result, invalid = observation_catalogue(paired, {})
    assert invalid == [] and len(list(result.values()) if isinstance(result, dict) else result) == 1

    # A row bound to a random hash still cannot force the target (existing contract).
    forged = {**base, "independent_asr_evidence": {"witness_request": earlier},
              "proposed_candidates": [{"text": "上班好累", "source": {**row, "request_sha256": "c" * 64}}]}
    _, invalid = observation_catalogue(forged, {})
    assert [r["reason_code"] for r in invalid] == ["SPAN_NATIVE_TRANSCRIPT_RECEIPT_INVALID"]


def test_independent_asr_audit_carries_the_request_its_routes_were_heard_for():
    from src.autoslice.independent_context_adjudication import independent_asr_candidates

    check = _check()
    target = build_witness_request(check)
    row = _native_row(target)

    def verifier(_request):
        return None
    verifier.observe_independent_witnesses = lambda _request: [row]
    _, _, audit = independent_asr_candidates(check, verifier)
    assert audit["status"] == "OBSERVED"
    assert audit["witness_request"] == target
    assert own_witness_request(audit["accepted_observations"][0], audit["witness_request"]) == target


def test_narrow_verdict_carried_from_an_earlier_round_is_paired_with_that_rounds_request():
    from src.autoslice.semantic_audio_recovery import narrow_witness_request

    first = _check()
    second = build_context_adjudication_request(_srt("上班好累"), {
        "cue_index": 1, "suspect": "好", "suggestion": "超", "proposed_full_cue": "上班超累",
    }, source_media_timeline_offset_ms=10630)
    heard_first = build_witness_request(first)
    assert heard_first["request_sha256"] != build_witness_request(second)["request_sha256"]
    verdict = _native_row(heard_first)
    prior = {"request": second, "verdict": verdict, "prior_audio_adjudication": {"request": first}}
    gate_bound = build_witness_request(second)
    assert narrow_witness_request(prior, gate_bound, verdict) == heard_first
    stray = {**verdict, "request_sha256": "0" * 64}
    assert narrow_witness_request({**prior, "verdict": stray}, gate_bound, stray) is gate_bound


# Real members (guimi b4cd6dc-v1 finding #11, cue 82; reduced read-only copy, MAI/MOSS rows
# unchanged): the independent routes heard the candidate-free request of the earlier round
# (witness binding 2498e5c4...); the later consumer check has another evidence_id.
_GUIMI = Path(__file__).parent / "fixtures" / "identity_g1_guimi_mai_rows.json"


def _guimi():
    if not _GUIMI.exists():  # private fixture; stripped from the public snapshot
        pytest.skip("private real-record fixture absent")
    return json.loads(_GUIMI.read_text(encoding="utf-8"))


def _kinds(result):
    return sorted(r["kind"] for r in (result.values() if isinstance(result, dict) else result))


def test_real_guimi_independent_rows_are_accepted_through_their_travelling_request():
    from src.autoslice.span_phoneme_resolution import observation_catalogue

    fx = _guimi()
    heard_for = build_witness_request(fx["prior_audio_adjudication"]["request"])
    rows = fx["independent_asr_accepted_observations"]
    assert {r["provider"] for r in rows} == {"mai", "moss"}
    assert {r["request_sha256"] for r in rows} == {heard_for["request_sha256"]}
    check = deepcopy(fx["consumer_check_request"])
    check["prior_acoustic_observations"] = []
    check["proposed_candidates"] = [{"text": r["exact_transcript"], "source": r} for r in rows]
    assert build_witness_request(check)["request_sha256"] != heard_for["request_sha256"]

    # A bare foreign binding stays rejected (existing contract).
    _, invalid = observation_catalogue(check, {})
    assert [r["reason_code"] for r in invalid] == ["SPAN_NATIVE_TRANSCRIPT_RECEIPT_INVALID"] * 2

    travelling = {**check, "independent_asr_evidence": {"witness_request": heard_for}}
    result, invalid = observation_catalogue(travelling, {})
    assert invalid == [] and _kinds(result) == ["native_transcript", "native_transcript"]

    # Sweep §6 positive: another proposed wording and an edit in an unrelated cue
    # (another whole-SRT evidence_id) are not new acoustic material.
    moved = {**travelling, "proposed_cue": travelling["proposed_cue"] + "呀",
             "evidence_id": "another-unrelated-cue-edited"}
    result, invalid = observation_catalogue(moved, {})
    assert invalid == [] and _kinds(result) == ["native_transcript", "native_transcript"]


def test_real_guimi_narrow_verdict_is_paired_with_the_request_it_was_heard_under():
    from src.autoslice.semantic_audio_recovery import narrow_witness_request
    from src.autoslice.span_phoneme_resolution import observation_catalogue

    fx = _guimi()
    prior, gate = fx["prior_audio_adjudication"], fx["gate_bound_witness_request"]
    heard_for = build_witness_request(prior["request"])
    assert prior["verdict"]["request_sha256"] == heard_for["request_sha256"] != gate["request_sha256"]
    assert narrow_witness_request(prior, gate, prior["verdict"]) == heard_for

    def catalogue(pairing, **changes):
        check = deepcopy(fx["consumer_check_request"])
        check["prior_acoustic_observations"] = [{"verdict": prior["verdict"], "witness_request": pairing}]
        return observation_catalogue({**check, **changes}, {})

    # What the b4cd6dc recorder wrote (gate-bound pairing) is rejected.
    _, invalid = catalogue(gate)
    assert [r["reason_code"] for r in invalid] == ["SPAN_NATIVE_TRANSCRIPT_RECEIPT_INVALID"]
    result, invalid = catalogue(heard_for)
    assert invalid == [] and _kinds(result) == ["native_transcript"]
    result, invalid = catalogue(heard_for, proposed_cue=fx["consumer_check_request"]["proposed_cue"] + "呀",
                                evidence_id="another-unrelated-cue-edited")
    assert invalid == [] and _kinds(result) == ["native_transcript"]
