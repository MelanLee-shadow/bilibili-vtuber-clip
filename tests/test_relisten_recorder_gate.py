"""Recorder-backed pending-audio gate after a K04 stop (admin round 2,).

Rules under test (assertions are the rules; no truth injected):
* SUB-20 (requirement summary: 要求补听就必须真补听) / SUB-04 (公开规则
 「第三次的补听就已经可以用Argon了」): a target whose own COMPLETED raw CPA answer asked for audio
 keeps that obligation across the ordinary continuation stop and reaches the
 existing ``recover_semantic_audio_range`` owner.
* Equal strength to a convergence gate: only COMPLETED rows; STARTED/ERROR/UNKNOWN
 never open it and are never re-sent (K22 per request, 41「不完整 STARTED 保留未知，
 不盲重发。」); one changed raw byte closes it.
* One counter: the owner decides with the target-covering
 ``effective_distinct_observations``; offered whole-clip range answers do not
 spend the target's three.
* SUB-18 order: with three distinct observations the no-gain terminal needs Argon
 terminal or structurally unavailable.
Synthetic WAV and protocol stubs only; no provider, no network.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice import semantic_audio_recovery as recovery
from src.autoslice import targeted_relisten as policy
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.exact_final_witness_authority import history_audio_recovery_from_adjudication
from src.autoslice.semantic_retry_progress import RetryNeedsPlan, SemanticAttemptRecorder, digest
from test_targeted_relisten_public_contract import (  # noqa: F401  (rig fixture)
    SRT, acquire_narrow, observe_plan, planner_data, rig,
)

ASK = json.dumps({"status": "UNRESOLVED", "decision": "UNRESOLVED", "needs_audio": True,
                  "reason": "尾音两次听写互相冲突，需不同语义范围的原音。"}, ensure_ascii=False)


def _model(answer=ASK):
    calls = []

    def call(prompt):
        calls.append(prompt)
        if isinstance(answer, Exception):
            raise answer
        return answer
    call.cpa_cache_identity = {"transport": "fixture", "models": ["recorder-gate"], "effort": "high"}
    call.calls = calls
    return call


def _k04_recorder(rig, finding, *, answers=3, unknown=False):
    """Three real (stub) COMPLETED answers asking for audio, then the K04 stop."""
    model = _model()
    if unknown:
        failing = _model(ConnectionError("synthetic send, outcome unknown"))
        with pytest.raises(ConnectionError):
            SemanticAttemptRecorder(srt_text=rig.srt, finding=finding, clip_context=rig.context,
                                    offset_ms=0).wrap(failing)("## 声音证据的判断次序 unknown")
    for i in range(answers):
        SemanticAttemptRecorder(srt_text=rig.srt, finding=finding, clip_context=rig.context,
                                offset_ms=0).wrap(model)("## 声音证据的判断次序 prior " + str(i))
    recorder = SemanticAttemptRecorder(srt_text=rig.srt, finding=finding, clip_context=rig.context, offset_ms=0)
    with pytest.raises(RetryNeedsPlan):
        recorder.wrap(model)("## 声音证据的判断次序 no new material")
    assert recorder.blocked == "CONTINUATION_VALUE_PLAN_REQUIRED"
    return recorder


def _scaffold_request(rig, finding):
    scaffold = auditor._candidate_blind_missing_proposal_scaffold(rig.srt, finding)
    return auditor.build_context_adjudication_request(rig.srt, scaffold, clip_context=rig.context)


def _stopped_audit(gate, request, **extra):
    return {"schema_version": "subtitle-span-adjudication.v1", "status": "MISSING_PROPOSAL_UNRESOLVED",
            "repaired": False, "reason_code": "CONTINUATION_VALUE_PLAN_REQUIRED",
            "decision_authority": "CPA_JUDGE_FAILED", "request": request,
            "recorder_needs_audio_gate": gate, **extra}


def _reseal(gate, **changes):
    forged = {**deepcopy(gate), **changes}
    forged.pop("gate_sha256")
    forged["gate_sha256"] = digest(forged)
    return forged


def test_completed_needs_audio_answer_opens_gate_and_enters_the_existing_owner(rig):
    witness = acquire_narrow(rig)
    finding = deepcopy(rig.finding)
    recorder = _k04_recorder(rig, finding)
    request = _scaffold_request(rig, finding)
    gate = recorder.needs_audio_gate(check_request=request)
    assert gate is not None and gate["needs_audio"] is True and gate["mutation_authorized"] is False
    audit = _stopped_audit(gate, request, verdict=witness)
    pending = history_audio_recovery_from_adjudication(audit)
    assert pending["status"] == "BLOCK" and pending["gate_source"] == "recorder_original_needs_audio"
    assert pending["gate"]["reason"] == json.loads(ASK)["reason"]
    before = len(rig.transport.calls)
    planner = []

    def plan(prompt):
        planner.append(planner_data(prompt))
        return json.dumps(observe_plan(), ensure_ascii=False)
    _, recovered = recovery.recover_semantic_audio_range(
        srt_text=rig.srt, finding={**finding, "exact_release_adjudication": audit},
        check_request=request, entity_verifier=rig.verifier, llm_call=plan)
    assert recovered["status"] == "OBSERVED", recovered.get("reason_code")
    assert len(rig.transport.calls) == before + 1  # One real (offline-intercepted) distinct relisten.
    assert "pending_audio_reason" not in planner[0]
    assert recovered["range_plan_telemetry"]["pending_audio_reason"] == json.loads(ASK)["reason"]
    assert recovered["argon_state"] == {"state": "STRUCTURALLY_UNAVAILABLE", "reason": "ARGON_NOT_CONFIGURED"}


def test_k22_is_per_request_an_unknown_sibling_does_not_close_a_completed_gate(rig):
    """41:「不完整 STARTED 保留未知，不盲重发。」 (admin ruling per request)."""
    witness = acquire_narrow(rig)
    finding = deepcopy(rig.finding)
    recorder = _k04_recorder(rig, finding, unknown=True)
    rows = recorder._entries()
    unknown = [r for r in rows if r["status"] == "OUTCOME_UNKNOWN"]
    assert len(unknown) == 1
    unknown_path = recorder.directory / (unknown[0]["request_sha256"] + ".json")
    unknown_bytes = unknown_path.read_bytes()
    request = _scaffold_request(rig, finding)
    gate = recorder.needs_audio_gate(check_request=request)
    assert gate is not None and gate["row_request_sha256"] != unknown[0]["request_sha256"]
    audit = _stopped_audit(gate, request, verdict=witness)
    assert history_audio_recovery_from_adjudication(audit)["status"] == "BLOCK"
    prompts = []

    def plan(prompt):
        prompts.append(prompt)
        return json.dumps(observe_plan(), ensure_ascii=False)
    _, recovered = recovery.recover_semantic_audio_range(
        srt_text=rig.srt, finding={**finding, "exact_release_adjudication": audit},
        check_request=request, entity_verifier=rig.verifier, llm_call=plan)
    assert recovered["status"] == "OBSERVED"
    # What was dispatched is a new range question, never the unknown request.
    assert unknown[0]["request"]["prompt"] not in prompts
    assert unknown_path.read_bytes() == unknown_bytes  # UNKNOWN kept as is.


def test_only_unknown_or_non_audio_answers_never_open_the_gate(rig):
    finding = deepcopy(rig.finding)
    no_audio = json.dumps({"status": "UNRESOLVED", "decision": "UNRESOLVED", "needs_audio": False,
                           "reason": "文字层仍未决。"}, ensure_ascii=False)
    failing = _model(ConnectionError("synthetic send, outcome unknown"))
    with pytest.raises(ConnectionError):
        SemanticAttemptRecorder(srt_text=rig.srt, finding=finding, clip_context=rig.context,
                                offset_ms=0).wrap(failing)("## 声音证据的判断次序 unknown")
    model = _model(no_audio)
    for i in range(3):
        SemanticAttemptRecorder(srt_text=rig.srt, finding=finding, clip_context=rig.context,
                                offset_ms=0).wrap(model)("## 声音证据的判断次序 prior " + str(i))
    recorder = SemanticAttemptRecorder(srt_text=rig.srt, finding=finding, clip_context=rig.context, offset_ms=0)
    with pytest.raises(RetryNeedsPlan):
        recorder.wrap(model)("## 声音证据的判断次序 no new material")
    assert recorder.needs_audio_gate(check_request=_scaffold_request(rig, finding)) is None
    # A gate pointing at a row that is not COMPLETED is invalid even if resealed.
    clean = deepcopy(rig.finding)
    clean["cue_index"] = 3  # Separate physical target with its own history.
    clean.update(suspect="哼唱", suggestion="歌唱", proposed_full_cue="刚才只是在歌唱，豆沙知道")
    good = _k04_recorder(rig, clean)
    request3 = _scaffold_request(rig, clean)
    gate = good.needs_audio_gate(check_request=request3)
    assert history_audio_recovery_from_adjudication(_stopped_audit(gate, request3))["status"] == "BLOCK"
    path = Path(rig.tmp / "base") / gate["row_relpath"]
    row = json.loads(path.read_text())
    path.write_text(json.dumps({**row, "status": "STARTED"}, ensure_ascii=False))
    from src.autoslice.evidence_identity import regular_file_sha256
    forged = _reseal(gate, row_content_sha256=regular_file_sha256(path))
    assert history_audio_recovery_from_adjudication(_stopped_audit(forged, request3))["status"] == "INVALID"


def test_one_changed_raw_byte_closes_the_gate(rig):
    finding = deepcopy(rig.finding)
    recorder = _k04_recorder(rig, finding)
    request = _scaffold_request(rig, finding)
    gate = recorder.needs_audio_gate(check_request=request)
    path = Path(rig.tmp / "base") / gate["row_relpath"]
    raw = path.read_bytes()
    index = raw.index(b"UNRESOLVED")
    path.write_bytes(raw[:index] + b"V" + raw[index + 1:])
    assert history_audio_recovery_from_adjudication(_stopped_audit(gate, request))["status"] == "INVALID"
    from src.autoslice.evidence_identity import regular_file_sha256
    forged = _reseal(gate, row_content_sha256=regular_file_sha256(path))
    # The content digest matches the edited file, but the raw answer bytes no
    # longer match the recorded completion digest.
    assert history_audio_recovery_from_adjudication(_stopped_audit(forged, request))["status"] == "INVALID"


def test_refused_resend_of_an_unknown_request_does_not_close_the_targets_gate(rig):
    """K22 refuses re-sending that one request; the latest K04 stop with a
    COMPLETED needs_audio row still opens the gate (per-request reading)."""
    finding = deepcopy(rig.finding)
    recorder = _k04_recorder(rig, finding)
    recorder.events.insert(0, {"status": "PRIOR_CALL_OUTCOME_UNKNOWN", "provider_calls": 0})
    gate = recorder.needs_audio_gate(check_request=_scaffold_request(rig, finding))
    assert gate is not None and gate["stop_reason"] == "CONTINUATION_VALUE_PLAN_REQUIRED"


def test_offered_whole_clip_ranges_do_not_spend_the_targets_three(rig):
    """1 target-covering observation + 10 offered range answers -> one distinct relisten."""
    finding = deepcopy(rig.finding)
    witness = acquire_narrow(rig, _scaffold_request(rig, finding))
    fake_ranges = [{"provider": "moss", "model": "fixture-range",
                    "artifact_bindings": {name + "_sha256": digest([name, i]) for name in
                                          ("source_media", "audio_clip", "raw_response")},
                    "verdict": {"response_sha256": digest(["verdict", i])}} for i in range(10)]
    noisy = {**finding, "_prior_range_acoustic_observations": fake_ranges}
    recorder = _k04_recorder(rig, noisy)
    offered = {item["raw_response"] for row in recorder._entries()
               for item in row["request"]["material"].get("bound_range_evidence", [])}
    assert len(offered) == 10
    request = _scaffold_request(rig, finding)
    gate = recorder.needs_audio_gate(check_request=request)
    audit = _stopped_audit(gate, request, verdict=witness)
    seen = []

    def plan(prompt):
        seen.append(planner_data(prompt)["effective_distinct_observations"])
        return json.dumps(observe_plan(), ensure_ascii=False)
    before = len(rig.transport.calls)
    _, recovered = recovery.recover_semantic_audio_range(
        srt_text=rig.srt, finding={**noisy, "exact_release_adjudication": audit},
        check_request=request, entity_verifier=rig.verifier, llm_call=plan)
    assert seen == [1]
    assert recovered["status"] == "OBSERVED" and len(rig.transport.calls) == before + 1


def test_three_distinct_needs_argon_terminal_before_the_no_gain_terminal():
    projection = {"effective_distinct_observations": 3, "observations": []}
    with pytest.raises(ValueError, match="RELISTEN_NO_GAIN_ARGON_NOT_TERMINAL"):
        policy.material_exhaustion(projection, cue_count=4, argon_state={"state": "NOT_TRIGGERED"})
    done = policy.material_exhaustion(projection, cue_count=4,
                                      argon_state={"state": "TERMINAL", "status": "UNCERTAIN"})
    assert done["basis"] == "DEFAULT_DISTINCT_SPENT_AND_ARGON_TERMINAL"
    assert policy._valid_exhaustion(done, projection)
    assert not policy._valid_exhaustion({**done, "argon": {"state": "NOT_TRIGGERED"}}, projection)
    one = {"effective_distinct_observations": 1, "observations": []}
    # The target's own CPA answer asked for audio: text-level no-gain is not exhaustion.
    with pytest.raises(ValueError, match="RELISTEN_NO_GAIN_MATERIAL_NOT_EXHAUSTED"):
        policy.material_exhaustion(one, cue_count=4, argon_state={"state": "NOT_TRIGGERED"})
    widest = {"effective_distinct_observations": 1, "observations": [
        {"input_information": {"cue_indexes": [1, 2, 3, 4]}}]}
    heard = policy.material_exhaustion(widest, cue_count=4, argon_state={"state": "NOT_TRIGGERED"})
    assert heard["basis"] == "EARLY_NO_GAIN_WIDEST_BLOCK_HEARD" and policy._valid_exhaustion(heard, widest)
    early = policy.material_exhaustion(one, cue_count=4, argon_state={"state": "NOT_TRIGGERED"},
                                       cpa_requested_audio=False)
    assert early["basis"] == "EARLY_NO_GAIN_CPA_DID_NOT_REQUEST_AUDIO"  # 41: no padding to three.
    assert not policy._valid_exhaustion({**early, "cpa_requested_audio": True}, one)


def test_range_wrapper_attaches_the_gate_after_a_k04_stop(rig):
    finding = deepcopy(rig.finding)
    recorder = _k04_recorder(rig, finding)
    model = _model()
    judge = recorder.wrap(model)
    stopped = {"schema_version": "subtitle-span-adjudication.v1", "status": "MISSING_PROPOSAL_UNRESOLVED",
               "repaired": False, "reason_code": "CONTINUATION_VALUE_PLAN_REQUIRED"}
    gated = auditor._with_recorder_needs_audio_gate(
        rig.srt, finding, stopped, judge_llm_call=judge, entity_verifier=rig.verifier,
        clip_context=rig.context, source_media_timeline_offset_ms=0,
        require_complete_utterance_support=False, argon_session=None)
    assert gated["recorder_needs_audio_gate"]["schema_version"] == "recorder-needs-audio-gate.v1"
    assert gated["request"] == gated["recorder_needs_audio_gate"]["original_request"]
    assert history_audio_recovery_from_adjudication(gated)["status"] == "BLOCK"
    import inspect
    assert "_with_recorder_needs_audio_gate(" in inspect.getsource(auditor._adjudicate_context_finding_with_range)


def test_judge_path_k04_stop_gets_the_gate_but_a_later_completed_call_settles_it(rig):
    """Sister cue13/25 shape: witness judge JUDGE_UNAVAILABLE at the K04 stop,
    text-first NOT_DISPATCHED, no other history gate."""
    finding = deepcopy(rig.finding)
    recorder = _k04_recorder(rig, finding)
    judge = recorder.wrap(_model())
    request = _scaffold_request(rig, finding)
    stopped = {"schema_version": "subtitle-span-adjudication.v1", "status": "UNCERTAIN", "repaired": False,
               "policy_branch": "JUDGE_UNCERTAIN_KEEP_CURRENT", "request": rig.check,
               "text_first_judge": {"status": "JUDGE_UNAVAILABLE", "dispatch_outcome": "NOT_DISPATCHED",
                                    "retry_reason_code": "CONTINUATION_VALUE_PLAN_REQUIRED"},
               "witness_judge": {"judge": {"status": "JUDGE_UNAVAILABLE", "dispatch_outcome": "NOT_DISPATCHED",
                                           "failure_layer": "LOCAL_RETRY_ADMISSION",
                                           "retry_reason_code": "CONTINUATION_VALUE_PLAN_REQUIRED"}}}
    assert history_audio_recovery_from_adjudication(stopped) is None
    # D's typed refusal opens no other gate on a target with no executed history.
    typed = deepcopy(stopped)
    typed["witness_judge"]["judge"].update(status="RETRY_BLOCKED", reason_code="CONTINUATION_VALUE_PLAN_REQUIRED")
    typed["text_first_judge"].update(status="RETRY_BLOCKED", reason_code="CONTINUATION_VALUE_PLAN_REQUIRED")
    assert history_audio_recovery_from_adjudication(typed) is None
    options = dict(judge_llm_call=judge, entity_verifier=rig.verifier, clip_context=rig.context,
                   source_media_timeline_offset_ms=0, require_complete_utterance_support=False,
                   argon_session=None)
    gated = auditor._with_recorder_needs_audio_gate(rig.srt, finding, stopped, **options)
    assert gated["request"] is rig.check  # A judge audit keeps its own request.
    assert history_audio_recovery_from_adjudication(gated, request=rig.check)["status"] == "BLOCK"
    # An audit that does not itself carry the stop gets no gate.
    settled = {**stopped, "witness_judge": {"judge": {"status": "JUDGED", "choice": "CURRENT"}},
               "text_first_judge": {}, "policy_branch": "JUDGE_KEEPS_CURRENT"}
    assert "recorder_needs_audio_gate" not in auditor._with_recorder_needs_audio_gate(
        rig.srt, finding, settled, **options)
    # A later completed call settles the recorder: no obligation remains.
    recorder.plan = {"action": "reframe_question", "question": "重新核对尾音", "why_previous_failed": "x",
                     "expected_gain": "y", "verification": "z"}
    recorder.wrap(_model(json.dumps({"choice": "CURRENT", "needs_audio": False, "reason": "已听清"})))(
        "## 声音证据的判断次序 settled")
    assert recorder.needs_audio_gate(check_request=request) is None


def test_rejudgment_k04_stop_after_consumed_audio_keeps_the_owner_loop_going(rig, monkeypatch):
    """OCI3 e9c50824 sister cue33 shape: the owner bound a new range observation,
    CPA consumed it (COMPLETED, still asking for audio), and the next same-plan
    call hit the recorder's continuation stop. SUB-20/SUB-04 -> SUB-18: that
    target's own completed needs_audio obligation is kept across this stop as
    at the entry, so the same owner plans again (next distinct listen, Argon or
    no-gain) instead of the loop ending MISSING_PROPOSAL_UNRESOLVED.
    """
    witness = acquire_narrow(rig)
    finding = deepcopy(rig.finding)
    recorder = _k04_recorder(rig, finding)
    planner = []

    def model(prompt):
        if '"already_heard_ranges"' in prompt:
            planner.append(planner_data(prompt))
            if len(planner) == 1:
                return json.dumps(observe_plan(), ensure_ascii=False)
            return json.dumps({"decision": "NO_NEW_AUDIO", "reason": "fixture stop"}, ensure_ascii=False)
        return ASK
    model.cpa_cache_identity = {"transport": "fixture", "models": ["recorder-gate"], "effort": "high"}
    judge = recorder.wrap(model)
    request = _scaffold_request(rig, finding)
    gate = recorder.needs_audio_gate(check_request=request)
    stopped = _stopped_audit(gate, request, verdict=witness)
    rejudged = []

    def rejudge(srt_text, working, **kwargs):
        # The ordinary consumer re-asks CPA with the new audio once (COMPLETED,
        # still needs_audio), then the same plan again: the recorder's K04 stop.
        prompt = "## 声音证据的判断次序 consumed new range " + str(len(rejudged))
        rejudged.append(prompt)
        judge(prompt)
        with pytest.raises(RetryNeedsPlan):
            judge(prompt + " (same plan, reworded)")
        return srt_text, {"schema_version": "subtitle-span-adjudication.v1",
                          "status": "MISSING_PROPOSAL_UNRESOLVED", "repaired": False,
                          "reason_code": recorder.blocked, "decision_authority": "CPA_JUDGE_FAILED"}
    monkeypatch.setattr(auditor, "_adjudicate_context_finding", rejudge)
    before = len(rig.transport.calls)

    _, audit = auditor._adjudicate_context_finding_with_range(
        rig.srt, {**finding, "exact_release_adjudication": stopped},
        entity_verifier=rig.verifier, clip_context=rig.context, judge_llm_call=judge)

    assert recorder.blocked == "CONTINUATION_PLAN_ALREADY_UNRESOLVED"
    assert len(rejudged) == 1 and len(rig.transport.calls) == before + 1  # One real relisten.
    assert len(planner) == 2  # The owner planned again after the consumed observation.
    assert planner[1]["effective_distinct_observations"] > planner[0]["effective_distinct_observations"]
    assert [row["status"] for row in audit["targeted_relisten_trace"]][0] == "OBSERVED"
    assert len(audit["targeted_relisten_trace"]) == 2


def test_settled_convergence_does_not_shadow_the_later_judges_audio_request(rig):
    """SUB-20 / 41: a real CPA needs_audio reaches the recovery owner. A
    candidate-less convergence that already RESOLVED (KEEP_EXISTING) is no
    audio request; when the later ordinary witness judge asked for audio
    (JUDGE_REQUESTS_MORE_AUDIO), that judge is the gate (OCI3 e9c50824 sister
    cue35: HISTORY_CONVERGENCE_AUDIO_GATE_INVALID -> SEMANTIC_RANGE_CURRENT_GATE_DRIFT).
    """
    from test_targeted_relisten_public_contract import pending_finding
    witness = acquire_narrow(rig)
    prior = pending_finding(rig, rig.check, witness)["exact_release_adjudication"]
    assert history_audio_recovery_from_adjudication(prior)["gate_source"] == "ordinary_witness_judge"
    settled = {**prior, "cpa_missing_proposal_convergence": {
        "schema_version": "subtitle-missing-proposal-cpa-convergence.v1", "status": "RESOLVED",
        "decision": "KEEP_EXISTING", "decision_authority": "CPA_JUDGE", "mutation_authorized": False}}
    pending = history_audio_recovery_from_adjudication(settled)
    assert pending["status"] == "BLOCK" and pending["needs_audio"] is True
    assert pending["gate_source"] == "ordinary_witness_judge"
    # A convergence that itself asks for audio keeps owning the gate (unchanged).
    asking = {**prior, "cpa_missing_proposal_convergence": {
        **settled["cpa_missing_proposal_convergence"], "status": "UNRESOLVED", "needs_audio": True}}
    assert history_audio_recovery_from_adjudication(asking)["gate_source"] == "cpa_missing_proposal_convergence"


def test_narrow_receipt_is_paired_with_the_request_it_was_heard_under(rig):
    """41: candidate/wording changes are no new acoustic material, and a
    receipt is checked against its own request, physical source and clocks.
    Under a recorder gate whose sealed request differs only by evidence_id,
    the prior narrow receipt stays bound to the prior adjudication's own
    request and counts for the same physical target (OCI3 e9c50824 sister
    cue24/25, guimi F2: SPAN_NATIVE/BLIND_*_RECEIPT_INVALID on equal crops).
    """
    witness = acquire_narrow(rig)
    finding = deepcopy(rig.finding)
    recorder = _k04_recorder(rig, finding)
    gate = recorder.needs_audio_gate(check_request=_scaffold_request(rig, finding))
    assert (build_witness_request(gate["original_request"])["request_sha256"]
            != witness["request_sha256"] == build_witness_request(rig.check)["request_sha256"])
    audit = _stopped_audit(gate, rig.check, verdict=witness)

    def plan(prompt):
        return json.dumps(observe_plan(), ensure_ascii=False)
    _, recovered = recovery.recover_semantic_audio_range(
        srt_text=rig.srt, finding={**finding, "exact_release_adjudication": audit},
        check_request=rig.check, entity_verifier=rig.verifier, llm_call=plan)

    before = recovered["observation_projection_before"]
    assert before["rejected_observations"] == []
    assert before["effective_distinct_observations"] == 1
    paired = recovered["consumer_check_request"]["prior_acoustic_observations"][-1]
    assert paired["witness_request"]["request_sha256"] == witness["request_sha256"]
    # A receipt bound to neither the prior nor the gate request is still rejected.
    stray = {**deepcopy(audit), "verdict": {**witness, "request_sha256": "0" * 64}}
    _, refused = recovery.recover_semantic_audio_range(
        srt_text=rig.srt, finding={**finding, "exact_release_adjudication": stray},
        check_request=rig.check, entity_verifier=rig.verifier, llm_call=plan)
    assert refused["observation_projection_before"]["effective_distinct_observations"] == 0


def test_range_admitted_before_a_receipt_counted_still_replays_its_own_admission(rig):
    """41 admission chronology: a saved range replays what its own admission
    could see. A narrow receipt rejected then and counted now (pairing fix)
    is not part of that admission, so the real range hearing is not lost to
    RELISTEN_ADMISSION_REPLAY_DRIFT in a later run.
    """
    from src.autoslice import targeted_relisten as policy
    witness = acquire_narrow(rig)
    finding = deepcopy(rig.finding)
    recorder = _k04_recorder(rig, finding)
    gate = recorder.needs_audio_gate(check_request=_scaffold_request(rig, finding))
    earlier = _stopped_audit(gate, rig.check, verdict={**witness, "request_sha256": "0" * 64})

    def plan(prompt):
        return json.dumps(observe_plan(), ensure_ascii=False)
    output, first = recovery.recover_semantic_audio_range(
        srt_text=rig.srt, finding={**finding, "exact_release_adjudication": earlier},
        check_request=rig.check, entity_verifier=rig.verifier, llm_call=plan)
    assert first["status"] == "OBSERVED"
    assert first["observation_projection_before"]["effective_distinct_observations"] == 0
    later = deepcopy(first["consumer_check_request"])
    later["prior_acoustic_observations"][-1].update(
        verdict=witness, witness_request=build_witness_request(rig.check))
    later.pop("request_sha256")
    later["request_sha256"] = recovery._digest(later)

    projection = policy.evidence_projection(later, ranges=output["_prior_range_acoustic_observations"])

    assert projection["rejected_observations"] == []
    assert projection["effective_distinct_observations"] == 2  # The receipt and the range.
