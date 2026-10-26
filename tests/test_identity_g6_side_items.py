"""Identity sweep, side items fixed with G1/G6 (M10, M62, M15/M16).

Each one compared a local representation (window, offset or the whole
clip-context record) where the physical target or the source identity is the
question.  Admin (G6 class ruling, 41 updated): the whole clip-context
record is a recorded delta on one clock and across a source/delivery clock
change; source binding, source-clock target and cue text decide.  Also M65's
cross-clock branch.  Offline; no provider.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.final_review_auditor import build_context_adjudication_request

CUE = "上班好累"
OFFSET = 9800


def _srt(shift):
    def stamp(ms):
        return f"00:00:{ms // 1000:02d},{ms % 1000:03d}"
    return f"1\n{stamp(15000 - shift)} --> {stamp(19000 - shift)}\n{CUE}\n"


def _native_row(witness_request):
    from src.autoslice.local_asr_target_evidence import MODELS, SCHEMA, digest
    from src.autoslice.native_context_witness import _native_transcript_witness, _prepare_request

    prepared = _prepare_request(witness_request)
    receipt = {
        "schema_version": SCHEMA, "status": "OBSERVED", "provider": "mai", "model": MODELS["mai"],
        "candidate_exposure": "none", "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
        "source_media_sha256": "a" * 64, "input_audio_sha256": "b" * 64, "response_sha256": "c" * 64,
        "target_start_ms": prepared.physical_start_ms, "target_end_ms": prepared.physical_end_ms,
        "crop_is_exact_target": True, "observation_scope": "complete_exact_cue_no_padding",
        "transcript": CUE, "native_segments": [],
    }
    receipt["receipt_sha256"] = digest(receipt)
    return _native_transcript_witness(request=witness_request, prepared=prepared,
                                      native_observation=receipt, source_sha256="a" * 64, provider="mai")


def test_m10_prior_scoped_observation_carries_across_the_padded_delivery_clock():
    """final_review_auditor.build_context_adjudication_request: the prior keeps its own
    request; before G6 a padded (0) vs delivery (9800) clock dropped it."""
    finding = {"cue_index": 1, "suspect": "好", "suggestion": "很", "proposed_full_cue": "上班很累"}
    prior = build_context_adjudication_request(_srt(0), dict(finding), source_media_timeline_offset_ms=0)
    verdict = _native_row(build_witness_request(prior))
    carried_finding = {**finding, "_prior_acoustic_observations": [{"request": prior, "verdict": verdict}]}

    delivery = build_context_adjudication_request(
        _srt(OFFSET), deepcopy(carried_finding), source_media_timeline_offset_ms=OFFSET)
    carried = delivery["prior_acoustic_observations"]
    assert len(carried) == 1
    assert carried[0]["witness_request"]["request_sha256"] == verdict["request_sha256"]

    # Another physical window is still another hearing.
    moved = build_context_adjudication_request(
        _srt(OFFSET - 40), deepcopy(carried_finding), source_media_timeline_offset_ms=OFFSET)
    assert not moved.get("prior_acoustic_observations")


def test_m15_m16_carryover_replay_matches_the_physical_window_on_another_clock(tmp_path, monkeypatch):
    """producer_package_finalization._replayable_exact_final_carryover_findings: a request
    sealed on another local clock is the same cue when its window + own offset is the
    current window + review offset; without the review offset the former local rule stays."""
    import hashlib

    from src.autoslice import producer_package_finalization as finalization

    proposed = "上班很累"
    base_sha = hashlib.sha256(CUE.encode("utf-8")).hexdigest()
    request = {"base_text_sha256": base_sha, "current_cue": CUE, "proposed_cue": proposed,
               "matched_start_ms": 15000, "matched_end_ms": 19000, "source_media_timeline_offset_ms": 0}
    row = {"base_text_sha256": base_sha, "exact_release_adjudication": {"request": request}}
    monkeypatch.setattr(finalization, "load_final_review_carryover", lambda _path: [deepcopy(row)])
    monkeypatch.setattr(finalization, "adjudicated_proposed_full_cue", lambda *_a, **_k: proposed)
    delivery = _srt(OFFSET)

    replayed = finalization._replayable_exact_final_carryover_findings(
        delivery, tmp_path / "carryover.json", review_timeline_offset_ms=OFFSET)
    assert len(replayed) == 1
    assert replayed[0]["carryover_exact_replay"]["matched_start_ms"] == 15000 - OFFSET

    assert finalization._replayable_exact_final_carryover_findings(
        delivery, tmp_path / "carryover.json") == []
    assert finalization._replayable_exact_final_carryover_findings(
        delivery, tmp_path / "carryover.json", review_timeline_offset_ms=OFFSET - 40) == []


# --- M62: an assisted hearing's anchor and the whole clip-context record -------------------

import test_targeted_relisten_public_contract as contract

rig = contract.rig  # the offline protocol rig fixture


def _assistance(rig):
    from src.autoslice import context_assisted_audio as assisted

    _witness, _check, _finding, audits = contract.three_observations(rig)
    row = audits[1]["observations"][0]
    assert row["observation_class"] == assisted.OBSERVATION_CLASS
    return row["assistance"]


def _shift_srt(srt, delta_ms):
    from src.autoslice.jingting_chunker import parse_srt_cues

    def stamp(ms):
        return f"00:00:{ms // 1000:02d},{ms % 1000:03d}"
    return "".join(f"{i}\n{stamp(c.start_ms + delta_ms)} --> {stamp(c.end_ms + delta_ms)}\n{c.text}\n\n"
                   for i, c in enumerate(parse_srt_cues(srt), 1))


def _rebuilt(check):
    check = deepcopy(check)
    check["clip_context_binding"] = {**check["clip_context_binding"],
                                     "whole_clip_draft_srt_sha256": "f" * 64}
    return check


def test_m62_rebuilt_clip_context_on_one_clock_keeps_the_assistance(rig):
    from src.autoslice import context_assisted_audio as assisted

    assistance = _assistance(rig)
    check = build_context_adjudication_request(
        contract.SRT, deepcopy(contract.FINDING), clip_context=rig.context)
    rebuilt = _rebuilt(check)
    assert assisted.anchor(rebuilt) != assistance["anchor"]
    validated = assisted.validate_assistance(assistance, rebuilt, original_srt=contract.SRT)
    assert validated["assistance_sha256"] == assistance["assistance_sha256"]


def test_m62_rebuilt_clip_context_across_clocks_keeps_the_assistance(rig):
    """Admin correction-lane clock -> delivery clock with a rebuilt clip context."""
    from src.autoslice import context_assisted_audio as assisted

    assistance = _assistance(rig)
    delivery = build_context_adjudication_request(
        _shift_srt(contract.SRT, -500), deepcopy(contract.FINDING), clip_context=rig.context,
        source_media_timeline_offset_ms=500)
    validated = assisted.validate_assistance(assistance, _rebuilt(delivery), original_srt=contract.SRT)
    assert validated["assistance_sha256"] == assistance["assistance_sha256"]


@pytest.mark.parametrize("change", ["source_sha", "physical"])
def test_m62_across_clocks_another_source_or_window_still_drifts(rig, change):
    from src.autoslice import context_assisted_audio as assisted

    assistance = _assistance(rig)
    offset = 500 if change == "source_sha" else 400
    delivery = _rebuilt(build_context_adjudication_request(
        _shift_srt(contract.SRT, -500), deepcopy(contract.FINDING), clip_context=rig.context,
        source_media_timeline_offset_ms=offset))
    if change == "source_sha":
        delivery["clip_context_binding"] = {**delivery["clip_context_binding"],
                                            "source_media_sha256s": ["f" * 64]}
    with pytest.raises(ValueError, match="ASSISTED_AUDIO_CURRENT_CONTEXT_DRIFT"):
        assisted.validate_assistance(assistance, delivery, original_srt=contract.SRT)


def test_m62_another_source_identity_still_drifts(rig):
    from src.autoslice import context_assisted_audio as assisted

    assistance = _assistance(rig)
    check = build_context_adjudication_request(
        contract.SRT, deepcopy(contract.FINDING), clip_context=rig.context)
    other = deepcopy(check)
    other["clip_context_binding"] = {**other["clip_context_binding"], "candidate_id": "another_candidate"}
    with pytest.raises(ValueError, match="ASSISTED_AUDIO_CURRENT_CONTEXT_DRIFT"):
        assisted.validate_assistance(assistance, other, original_srt=contract.SRT)


# --- M65: the prior request of a history audio gate --------------------------------------

def _g6_requests():
    from test_identity_g6_audio_question import _rehash, _request
    return _rehash, _request


def test_m65_history_prior_request_matches_across_clocks_with_a_rebuilt_clip_context():
    from src.autoslice.exact_final_witness_authority import _history_audio_prior_request_matches

    _rehash, _request = _g6_requests()
    correction = _request(padded=True, mode=False)
    exact_final = _request(padded=False, mode=True, context_sha="3" * 64, draft_sha="4" * 64,
                           neighbour="前一句改过的话")
    assert _history_audio_prior_request_matches({"request": correction}, exact_final) is True


@pytest.mark.parametrize("change", ["source_sha", "physical", "cue_text", "proposed"])
def test_m65_history_prior_request_still_rejects_another_source_target_or_text(change):
    from src.autoslice.exact_final_witness_authority import _history_audio_prior_request_matches

    _rehash, _request = _g6_requests()
    correction = _request(padded=True, mode=False)
    exact_final = _request(padded=False, mode=True, context_sha="3" * 64, draft_sha="4" * 64)
    if change == "source_sha":
        exact_final = _rehash({**exact_final, "clip_context_binding": {
            **exact_final["clip_context_binding"], "source_media_sha256s": ["f" * 64]}})
    elif change == "physical":
        exact_final = _rehash({**exact_final, "matched_end_ms": exact_final["matched_end_ms"] + 300})
    elif change == "cue_text":
        exact_final = _rehash({**exact_final, "current_cue": exact_final["current_cue"] + "啊"})
    else:
        exact_final = _request(padded=False, mode=True, proposed="我听，听到你不行哦", suggestion="行")
    assert _history_audio_prior_request_matches({"request": correction}, exact_final) is False
