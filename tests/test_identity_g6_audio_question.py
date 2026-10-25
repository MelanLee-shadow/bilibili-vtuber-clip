"""G6 (identity sweep): one audio question across rounds, lanes, modes and clocks.

Admin ruling a needs-audio gate used as collection authority only
does not carry mode fields (``require_complete_utterance_support``) in its
question identity; they, the local clock and cue ordinals are recorded as
deltas.  A text change still needs the current judge.  Admin (G6
class ruling, 41 updated): on one clock and across a source/delivery clock
change the source binding (candidate, recordings, piece clock), the source-clock
target and the cue text decide; the whole clip-context hashes are deltas.  The
Argon trigger follows the same rule.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from src.autoslice.audio_question_identity import same_audio_question
from src.autoslice.exact_final_witness_authority import (
    _sha_json,
    history_audio_recovery_from_adjudication,
    valid_history_audio_recovery,
)
from src.autoslice.final_review_auditor import build_context_adjudication_request
from src.autoslice.semantic_audio_recovery import _same_pending_audio_target

CUE = "我听，听到你不许哦"
PROPOSED = "我听，听到你不是哦"
OFFSET = 9800


def _srt(blocks):
    lines = []
    for index, (start, end, text) in enumerate(blocks, start=1):
        def stamp(ms):
            return f"00:{ms // 60000:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
        lines.append(f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}")
    return "\n\n".join(lines) + "\n"


def _rehash(request):
    request = dict(request)
    request.pop("request_sha256", None)
    request["request_sha256"] = _sha_json(request)
    return request


def _clip(context_sha, draft_sha):
    return {"schema_version": "clip-context.v1", "context_sha256": context_sha,
            "candidate_id": "auto_200311_218_282", "source_media_sha256s": ["a" * 64],
            "whole_clip_draft_srt_sha256": draft_sha, "speech_memory_ledger_sha256": "c" * 64}


def _request(*, padded: bool, mode: bool, context_sha="1" * 64, draft_sha="2" * 64,
             neighbour="前一句的话", proposed=PROPOSED, suggestion="是"):
    """The same physical cue on the padded clock (offset 0) or the delivery clock (offset 9800)."""
    shift = 0 if padded else OFFSET
    srt = _srt([(15000 - shift, 17000 - shift, neighbour), (20000 - shift, 22500 - shift, CUE)])
    request = build_context_adjudication_request(
        srt, {"cue_index": 2, "suspect": "许", "suggestion": suggestion, "proposed_full_cue": proposed},
        source_media_timeline_offset_ms=shift, require_complete_utterance_support=mode,
    )
    request["clip_context_binding"] = _clip(context_sha, draft_sha)
    return _rehash(request)


def _request_after_inserted_cue(*, mode: bool):
    """The same physical cue on the padded clock after a cue was inserted before it:
    another ordinal, another whole SRT, a rebuilt clip context, the same source."""
    srt = _srt([(12000, 13000, "插入的一句"), (15000, 17000, "前一句的话"), (20000, 22500, CUE)])
    request = build_context_adjudication_request(
        srt, {"cue_index": 3, "suspect": "许", "suggestion": "是", "proposed_full_cue": PROPOSED},
        source_media_timeline_offset_ms=0, require_complete_utterance_support=mode,
    )
    request["clip_context_binding"] = _clip("5" * 64, "6" * 64)
    return _rehash(request)


def _ordinary_gate_adjudication(request):
    gate = {
        "schema_version": "acoustic-witness-adjudication.v1", "status": "JUDGED",
        "needs_audio": True, "needs_audio_requested": True, "reason": "需要原音分辨“许/是”",
        "prompt_sha256": "d" * 64, "completion_sha256": "e" * 64,
        "check_request_sha256": request["request_sha256"],
    }
    return {
        "schema_version": "subtitle-span-adjudication.v1", "status": "UNCERTAIN",
        "repaired": False, "timing_immutable": True, "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY", "policy_branch": "JUDGE_REQUESTS_MORE_AUDIO",
        "request": request, "witness_judge": {"judge": gate},
    }


def test_same_question_ignores_mode_clock_and_ordinal_fields():
    correction = _request(padded=True, mode=False)
    exact_final = _request(padded=False, mode=True, neighbour="前一句改过的话")
    assert correction["request_sha256"] != exact_final["request_sha256"]
    same, deltas = same_audio_question(correction, exact_final, compare_proposed=True)
    assert same is True
    assert deltas["require_complete_utterance_support"] == [None, True]
    assert deltas["source_media_timeline_offset_ms"] == [0, OFFSET]
    assert "evidence_id" in deltas
    assert not any(key.startswith("clip_context_binding.") for key in deltas)


def _with_pieces(request, pieces):
    return _rehash({**request, "clip_context_binding": {**request["clip_context_binding"], "pieces": pieces}})


PIECES = [{"source_media_sha256": "a" * 64, "start_ms": 1000, "end_ms": 61000}]


def test_rebuilt_clip_context_is_a_recorded_delta_on_one_clock_and_across_clocks():
    """Admin (G6 class ruling, 41 updated): correction lane (offset 0) ->
    exact-final (offset 9800) with a rebuilt clip context is the same audio question."""
    correction = _request(padded=True, mode=False)
    for padded in (False, True):
        rebuilt = _request(padded=padded, mode=True, context_sha="3" * 64, draft_sha="4" * 64,
                           neighbour="前一句改过的话")
        same, deltas = same_audio_question(correction, rebuilt, compare_proposed=True)
        assert same is True, deltas
        assert deltas["clip_context_binding.context_sha256"] == ["1" * 64, "3" * 64]
        assert deltas["clip_context_binding.whole_clip_draft_srt_sha256"] == ["2" * 64, "4" * 64]
    # The Argon trigger identity follows the same rule.
    from src.autoslice.argon_targeted_relisten import trigger_question_identity
    rebuilt_delivery = _request(padded=False, mode=True, context_sha="3" * 64, draft_sha="4" * 64)
    same, diagnostic = trigger_question_identity(correction, rebuilt_delivery)
    assert same is True, diagnostic
    assert "clip_context_binding.context_sha256" in diagnostic["recorded_deltas"]
    # A piece clock carried by both bindings stays identity.
    assert same_audio_question(_with_pieces(correction, PIECES), _with_pieces(rebuilt_delivery, PIECES),
                               compare_proposed=True)[0] is True


@pytest.mark.parametrize("change", ["source_sha", "candidate", "piece_clock", "physical", "cue_text"])
def test_a_cross_clock_rebuilt_context_with_another_source_target_or_text_is_another_question(change):
    correction = _request(padded=True, mode=False)
    rebuilt = _request(padded=False, mode=True, context_sha="3" * 64, draft_sha="4" * 64)
    clip = rebuilt["clip_context_binding"]
    if change == "source_sha":
        rebuilt = _rehash({**rebuilt, "clip_context_binding": {**clip, "source_media_sha256s": ["f" * 64]}})
    elif change == "candidate":
        rebuilt = _rehash({**rebuilt, "clip_context_binding": {**clip, "candidate_id": "auto_200311_219_282"}})
    elif change == "piece_clock":
        correction = _with_pieces(correction, PIECES)
        rebuilt = _with_pieces(rebuilt, [{**PIECES[0], "start_ms": 1300}])
    elif change == "physical":
        rebuilt = _rehash({**rebuilt, "matched_start_ms": rebuilt["matched_start_ms"] + 40})
    else:
        rebuilt = _rehash({**rebuilt, "current_cue": CUE + "啊"})
    same, deltas = same_audio_question(correction, rebuilt, compare_proposed=True)
    assert same is False
    assert deltas["reason"] in {"SOURCE_BINDING_DIFFERS", "PHYSICAL_TARGET_DIFFERS", "QUESTION_TEXT_DIFFERS"}
    from src.autoslice.argon_targeted_relisten import trigger_question_identity
    assert trigger_question_identity(correction, rebuilt)[0] is False


@pytest.mark.parametrize("change", ["proposed", "current", "physical", "source"])
def test_a_different_question_target_or_source_is_a_different_question(change):
    old = _request(padded=True, mode=False)
    new = _request(padded=False, mode=True)
    if change == "proposed":
        new = _request(padded=False, mode=True, proposed="我听，听到你不行哦", suggestion="行")
    elif change == "current":
        new = _rehash({**new, "current_cue": CUE + "啊"})
    elif change == "physical":
        new = _rehash({**new, "matched_start_ms": new["matched_start_ms"] + 40})
    else:
        new = _rehash({**new, "clip_context_binding": {
            **new["clip_context_binding"], "source_media_sha256s": ["f" * 64]}})
    assert same_audio_question(old, new, compare_proposed=True)[0] is False


def test_a_cross_clock_carry_needs_the_source_binding_on_both_sides():
    old = _request(padded=True, mode=False)
    new = _request(padded=False, mode=False)
    bare_old = _rehash({k: v for k, v in old.items() if k != "clip_context_binding"})
    bare_new = _rehash({k: v for k, v in new.items() if k != "clip_context_binding"})
    assert same_audio_question(bare_old, bare_new, compare_proposed=True)[0] is False
    # Same local representation without a binding keeps the former same-clock rule.
    assert same_audio_question(bare_old, deepcopy(bare_old), compare_proposed=True)[0] is True
    assert same_audio_question(bare_old, old, compare_proposed=True, strict_source=True)[0] is False


def test_k14_carried_ordinary_gate_is_collection_authority_for_the_exact_final_request():
    """F7 path: a correction-lane adjudication (mode X, padded clock 0) carried into
    exact-final (mode Y, delivery clock 9800, a neighbour cue edited, the clip
    context rebuilt) for the same physical cue.  Before G6 the gate hash had to
    equal the exact-final request hash, so HISTORY_CONVERGENCE_AUDIO_GATE_INVALID
    followed with zero CPA calls."""
    correction = _request(padded=True, mode=False)
    adjudication = _ordinary_gate_adjudication(correction)
    exact_final = _request(padded=False, mode=True, context_sha="3" * 64, draft_sha="4" * 64,
                           neighbour="前一句改过的话")
    recovery = history_audio_recovery_from_adjudication(adjudication, request=exact_final)
    assert recovery["status"] == "BLOCK", recovery
    assert recovery["gate_source"] == "ordinary_witness_judge"
    assert recovery["original_request"]["request_sha256"] == correction["request_sha256"]
    assert recovery["question_deltas"]["source_media_timeline_offset_ms"] == [0, OFFSET]
    assert recovery["question_deltas"]["clip_context_binding.context_sha256"] == ["1" * 64, "3" * 64]
    assert (recovery["matched_start_ms"], recovery["matched_end_ms"]) == (
        exact_final["matched_start_ms"], exact_final["matched_end_ms"])
    # The typed envelope is valid for the fresh exact-final request (dispatch guard).
    assert valid_history_audio_recovery(recovery, request=exact_final) is True
    # The gate is still bound to the request it answered: never re-signed.
    assert recovery["gate"]["check_request_sha256"] == correction["request_sha256"]


@pytest.mark.parametrize("change", ["proposed", "physical", "forged_gate", "source_sha", "cue_text"])
def test_k14_carry_still_rejects_another_question_or_an_unbound_gate(change):
    correction = _request(padded=True, mode=False)
    adjudication = _ordinary_gate_adjudication(correction)
    exact_final = _request(padded=False, mode=True, context_sha="3" * 64, draft_sha="4" * 64)
    if change == "proposed":
        exact_final = _request(padded=False, mode=True, proposed="我听，听到你不行哦", suggestion="行")
    elif change == "source_sha":
        exact_final = _rehash({**exact_final, "clip_context_binding": {
            **exact_final["clip_context_binding"], "source_media_sha256s": ["f" * 64]}})
    elif change == "cue_text":
        exact_final = _rehash({**exact_final, "current_cue": CUE + "啊"})
    elif change == "physical":
        exact_final = _rehash({**exact_final, "matched_end_ms": exact_final["matched_end_ms"] + 300})
    else:
        adjudication["witness_judge"]["judge"]["check_request_sha256"] = "0" * 64
    recovery = history_audio_recovery_from_adjudication(adjudication, request=exact_final)
    assert recovery["status"] == "INVALID"


def test_valid_history_recovery_without_its_original_request_keeps_the_exact_hash_rule():
    correction = _request(padded=True, mode=False)
    adjudication = _ordinary_gate_adjudication(correction)
    exact_final = _request(padded=False, mode=True)
    recovery = history_audio_recovery_from_adjudication(adjudication, request=exact_final)
    stripped = {k: v for k, v in recovery.items() if k not in {"original_request", "question_deltas"}}
    assert valid_history_audio_recovery(stripped, request=exact_final) is False


def test_pending_audio_target_carries_across_clocks_with_a_rebuilt_clip_context():
    """M70 (consumers: semantic recovery, targeted_relisten:712, argon_targeted_relisten:32
    via its trigger identity, same rule); admin the whole clip-context
    record is a delta on one clock and across clocks."""
    old = _request(padded=True, mode=False)
    new = _request(padded=False, mode=True, neighbour="前一句改过的话")
    assert _same_pending_audio_target(old, new) is True
    rebuilt_cross_clock = _request(padded=False, mode=True, context_sha="3" * 64, draft_sha="4" * 64)
    assert _same_pending_audio_target(old, rebuilt_cross_clock) is True
    other_source = _rehash({**rebuilt_cross_clock, "clip_context_binding": {
        **rebuilt_cross_clock["clip_context_binding"], "source_media_sha256s": ["f" * 64]}})
    assert _same_pending_audio_target(old, other_source) is False
    rebuilt_same_clock = _request(padded=True, mode=True, context_sha="3" * 64, draft_sha="4" * 64)
    assert _same_pending_audio_target(old, rebuilt_same_clock) is True
    moved = _rehash({**new, "matched_start_ms": new["matched_start_ms"] + 10})
    assert _same_pending_audio_target(old, moved) is False


def test_screen_context_candidate_binding_is_the_candidate_not_the_context_hash():
    import inspect
    from src.autoslice import screen_read_witness
    body = inspect.getsource(screen_read_witness.validate_screen_context_observation)
    assert 'observation.get("clip_context_sha256") != binding.get("context_sha256")' not in body
    assert 'observation.get("candidate_id") != binding.get("candidate_id")' in body


# --------------------------------------------------- real sister v1-002 Argon triggers

_FORMER_TRIGGER_FIELDS = (
    "current_cue", "base_text_sha256", "suspect", "repair_class", "cue_indexes",
    "matched_start_ms", "matched_end_ms", "source_media_timeline_offset_ms",
    "context_start_ms", "context_end_ms", "clip_context_binding", "whole_clip_current_srt")


def _real_triggers():
    import json
    from pathlib import Path
    path = Path(__file__).parent / "fixtures" / "identity_g6_sister_argon_triggers.json"
    if not path.exists():  # private fixture; stripped from the public snapshot
        pytest.skip("private real-record fixture absent")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("label", ["12", "13a", "13b", "15"])
def test_real_sister_argon_trigger_survives_an_edit_in_another_cue(label):
    """Sister v1-002 (faf4d2d5): cue12/13x2/15 Argon hearings were OBSERVED but rejected
    with ARGON_TARGETED_CURRENT_CONTEXT_DRIFT because cue27 changed 死男人 -> 老男人
    elsewhere in the film.  Same question -> same trigger; the whole-SRT change is a delta."""
    from src.autoslice.argon_targeted_relisten import trigger_question_identity
    from src.autoslice.jingting_chunker import parse_srt_cues

    fixture = _real_triggers()
    bound = fixture["triggers"][label]["check"]
    current = {**bound, "whole_clip_current_srt": fixture["current_reviewed_srt"]}
    old_cues = [c for c in parse_srt_cues(bound["whole_clip_current_srt"]) if c.text.strip()]
    new_cues = [c for c in parse_srt_cues(current["whole_clip_current_srt"]) if c.text.strip()]
    changed = [i + 1 for i, (a, b) in enumerate(zip(old_cues, new_cues)) if a.text != b.text]
    assert changed == [27] and bound["cue_indexes"] != [27]
    # The former 12-field equality rejected exactly this record.
    assert [k for k in _FORMER_TRIGGER_FIELDS if bound.get(k) != current.get(k)] == ["whole_clip_current_srt"]
    same, diagnostic = trigger_question_identity(bound, current)
    assert same is True, diagnostic
    assert diagnostic["recorded_deltas"]["whole_clip_current_srt"] == "DIFFERS"
    # Control: another physical target on the same record is another question.
    moved = {**current, "matched_start_ms": current["matched_start_ms"] + 40}
    rejected, why = trigger_question_identity(bound, moved)
    assert rejected is False and why["failed"][0] == "PHYSICAL_TARGET_DIFFERS"
    assert why["current_check"]["matched_ms"][0] == moved["matched_start_ms"]


@pytest.mark.parametrize("label", ["26", "12"])
def test_real_trigger_survives_its_own_argon_transcript_becoming_the_candidate(label):
    """Stomach cue11/15/26 and sister cue26: twelve former fields equal on an as-is or
    candidate-blind rebuild, yet rejected.  After Argon OBSERVED the consuming request is
    rebuilt with Argon's transcript as the candidate (minimal span suspect, spoken_unit):
    candidate-derived fields, recorded as deltas, not a new audio question."""
    from src.autoslice.argon_targeted_relisten import trigger_question_identity

    bound = _real_triggers()["triggers"][label]["check"]
    other_class = "phonetic" if bound["repair_class"] == "spoken_unit" else "spoken_unit"
    consuming = {**bound, "suspect": bound["current_cue"][:2], "repair_class": other_class,
                 "proposed_cue": bound["current_cue"][:2] + "X" + bound["current_cue"][2:],
                 "candidate_provenance": {"kind": "argon_transcript_minimal_candidate"}}
    same, diagnostic = trigger_question_identity(bound, consuming)
    assert same is True, diagnostic
    assert {"suspect", "repair_class", "candidate_provenance"} <= set(diagnostic["recorded_deltas"])


def test_real_sister_cue26_trigger_rejection_now_names_what_differed():
    """cue26x2: the rebuilt current request matched all twelve former fields, yet the run
    rejected it; the audit kept no current_check.  The shared judgment's diagnostic now
    carries both compared checks so the differing field is readable."""
    from src.autoslice.argon_targeted_relisten import trigger_question_identity

    bound = _real_triggers()["triggers"]["26"]["check"]
    assert trigger_question_identity(bound, dict(bound))[0] is True
    shifted = {**bound, "context_end_ms": bound["context_end_ms"] + 500}
    same, diagnostic = trigger_question_identity(bound, shifted)
    assert same is False and diagnostic["failed"] == ["CONTEXT_WINDOW_DIFFERS"]
    assert diagnostic["current_check"]["context_ms"][1] == bound["context_end_ms"] + 500
    assert diagnostic["bound_check"]["context_ms"][1] == bound["context_end_ms"]


def _rejected_argon_record(detail="ARGON_TARGETED_CURRENT_CONTEXT_DRIFT", status="OBSERVED"):
    """The stomach faf4d2d cue11 shape: OBSERVED Argon refused by the trigger identity."""
    return {"schema_version": "subtitle-span-adjudication.v1", "status": "UNCERTAIN",
            "repaired": False, "timing_immutable": True, "reason_code": "ARGON_INTEGRATION_REJECTED",
            "detail": detail, "argon_escalation": {"status": status},
            "prior_audio_adjudication": None, "request": _request(padded=True, mode=False),
            "mutation_authority": {"schema_version": "subtitle-correction-mutation-authority.v1",
                                   "status": "NOT_APPLIED", "basis": "ARGON_INTEGRATION_REJECTED"}}


def test_f7_handoff_carries_an_observed_argon_refused_by_the_identity_check():
    from src.autoslice.retained_reviewed_draft import _exact_final_handoff_reasons

    assert _exact_final_handoff_reasons(_rejected_argon_record()) == ["ARGON_OBSERVED_IDENTITY_REJECTED"]


@pytest.mark.parametrize("variant", ["other_detail", "not_observed", "authority", "applied"])
def test_f7_handoff_keeps_rejecting_other_argon_refusals(variant):
    from src.autoslice.retained_reviewed_draft import _exact_final_handoff_reasons

    record = _rejected_argon_record()
    if variant == "other_detail":
        record = _rejected_argon_record(detail="ARGON_CONSUMER_SOURCE_DRIFT")
    elif variant == "not_observed":
        record = _rejected_argon_record(status="FAILED")
    elif variant == "authority":
        record["decision_authority"] = "OPERATOR"
    else:
        record["mutation_authority"] = {**record["mutation_authority"], "status": "PASS"}
    assert _exact_final_handoff_reasons(record) == []


def test_f7_handoff_end_to_end_carries_the_rejected_argon_record_into_exact_final(tmp_path, monkeypatch):
    """Through the real K14 consumer (``require_pending_entity_review`` ->
    ``retained_entity_handoff_findings``), mirroring the Argon-pending constructor of
    tests/test_retained_entity_exact_final_handoff.py: the rejected record keeps the
    session request (as the pending form does) and is carried, not dead-ended with
    PENDING_ENTITY_CURRENT_REVIEW_REQUIRED."""
    from src.autoslice.retained_reviewed_draft import retained_entity_handoff_findings
    from tests.test_retained_entity_exact_final_handoff import _k04_stopped_review
    from tests.test_retained_entity_input_recovery import CURRENT, _consume, _pending

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    retained, _authority, context = _pending(tmp_path)
    review = _k04_stopped_review(context)
    record = _rejected_argon_record()
    record["request"] = review["findings"][0]["context_audio_adjudication"]["request"]
    record["argon_rejection_diagnostic"] = {"failed": ["PHYSICAL_TARGET_DIFFERS"]}
    review["findings"][0]["context_audio_adjudication"] = record
    authority = _consume(tmp_path, retained, review)

    pointer = authority["retained_entity_review"]
    assert pointer["status"] == "CURRENT_REVIEW_COMPLETED"
    assert pointer["carried_to_exact_final_count"] == 1
    carried = retained_entity_handoff_findings(CURRENT, authority, timeline_offset_ms=0)
    assert len(carried) == 1
    assert carried[0]["candidate_provenance"]["handoff_reason_codes"] == ["ARGON_OBSERVED_IDENTITY_REJECTED"]
    assert carried[0]["exact_release_adjudication"] == record


def test_recorder_gate_recovery_envelope_validates_for_its_no_gain_closure(tmp_path, monkeypatch):
    """Sister v1-002 cue24 / cue33x2: recorder gate open, Argon UNCERTAIN (terminal), yet
    the no-gain closure stopped at RELISTEN_NO_GAIN_ORIGINAL_AUDIO_GATE_DRIFT because the
    shared envelope validator had no branch for the recorder gate (41 §4: same strength
    as a convergence receipt).  Real recorder rows; forged or drifted gates stay invalid."""
    from test_relisten_recorder_gate import _k04_recorder, _scaffold_request, _stopped_audit
    from test_targeted_relisten_public_contract import build_offline_rig

    rig = build_offline_rig(tmp_path, monkeypatch)
    finding = deepcopy(rig.finding)
    recorder = _k04_recorder(rig, finding)
    request = _scaffold_request(rig, finding)
    gate = recorder.needs_audio_gate(check_request=request)
    envelope = history_audio_recovery_from_adjudication(_stopped_audit(gate, request))
    assert envelope["gate_source"] == "recorder_original_needs_audio"
    original = envelope["original_request"]
    assert valid_history_audio_recovery(envelope, request=original) is True
    forged = {**envelope, "gate": {**gate, "reason": "rewritten"}}
    assert valid_history_audio_recovery(forged, request=original) is False
    moved = {**envelope, "matched_start_ms": envelope["matched_start_ms"] + 1}
    assert valid_history_audio_recovery(moved, request=original) is False


def test_pending_audio_target_survives_an_inserted_cue_on_the_same_clock():
    """M70 behaviour change: before G6 a different cue ordinal on one clock fell to the
    cross-clock branch and needed an identical clip context; ordinals are representation."""
    old = _request(padded=True, mode=False)
    new = _request_after_inserted_cue(mode=True)
    assert old["cue_indexes"] != new["cue_indexes"]
    assert _same_pending_audio_target(old, new) is True
    same, deltas = same_audio_question(old, new, compare_proposed=False)
    assert same is True and deltas["cue_indexes"] == [old["cue_indexes"], new["cue_indexes"]]
