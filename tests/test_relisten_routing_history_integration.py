"""Relisten evidence and typed closure must use the existing physical request ledger.

Synthetic audio and complete transport replies come from the admitted public
fixture. Provider sends remain stubbed; these assertions concern software only.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice import semantic_audio_recovery as recovery
from src.autoslice.semantic_request_history import with_partial_usage_guard
from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
from tests.test_targeted_relisten_public_contract import (
    acquire_narrow, acquire_range, apply_save_and_cold_review, build_offline_rig,
    observe_plan, assisted_plan, pending_finding, public_judge,
)

MODEL = {"transport": "offline-integration", "models": ["synthetic-protocol-cpa"], "effort": "high"}


def _rows(rig):
    return sorted((rig.tmp / "base/cache/semantic-cpa-attempts").glob("*/*.json"))


def _bytes(paths):
    return {str(path): path.read_bytes() for path in paths}


def _judge(rig):
    delegate = public_judge(rig)
    calls = []
    def call(prompt):
        raw = delegate(prompt)
        calls.append((prompt, raw))
        return raw
    call.cpa_cache_identity = MODEL
    call.calls = calls
    return call


@pytest.mark.parametrize("prewrapped", [False, True])
def test_public_relisten_typed_apply_and_cold_review_keep_one_durable_recorder(tmp_path, monkeypatch, prewrapped):
    rig = build_offline_rig(tmp_path, monkeypatch)
    judge = _judge(rig)
    recorder = SemanticAttemptRecorder(srt_text=rig.srt, finding=rig.finding,
        clip_context=rig.context, offset_ms=rig.offset_ms)
    bound = recorder.wrap(judge) if prewrapped else judge
    if prewrapped:
        assert bound._semantic_attempt_recorder is recorder
    output, audit = auditor.adjudicate_context_finding(
        rig.srt, deepcopy(rig.finding), entity_verifier=rig.verifier,
        clip_context=rig.context, judge_llm_call=bound,
        require_complete_utterance_support=True)
    assert output == rig.srt
    assert audit.get("repaired") is True
    assert audit["contextual_terminal_handoff"]["status"] == "TYPED_EDITORIAL_AUTHORITY"
    assert audit["contextual_terminal_handoff"]["literal_transcription"] is False
    assert len(rig.transport.calls) == 3

    paths = _rows(rig)
    rows = [json.loads(path.read_bytes()) for path in paths]
    assert len(rows) == len(judge.calls) > 0
    assert all(row["status"] == "COMPLETED" and row["issue_sha256"] == recorder.issue for row in rows)
    assert Counter((row["request"]["prompt"], row["completion"]) for row in rows) == Counter(judge.calls)
    assert all(row["request"]["model"] == MODEL for row in rows)
    assert sum(event["status"] == "COMPLETED"
        for event in audit["semantic_retry_progress"]["events"]) == len(judge.calls)
    assert any(prompt.startswith("# CPA 补听范围选择") for prompt, _ in judge.calls)

    before = _bytes(paths)
    call_count = len(judge.calls)
    apply_save_and_cold_review(rig, [{**rig.finding, "exact_release_adjudication": audit}])
    assert _bytes(_rows(rig)) == before and len(judge.calls) == call_count
    cold = json.loads((rig.output / "cold-process.json").read_text())
    outcome = json.loads(cold["stdout"])
    assert outcome["cold_owner_validated"] and outcome["gate"] == "BLOCK"
    assert outcome["gate_rejected_missing_boundary"]
    original_cold = json.loads((rig.output / "cold-current-review.json").read_text())
    assert set(original_cold["reason_codes"]) == {
        "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID",
        "FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"}
    completed_cold = _complete_current_correction_review(rig)
    assert completed_cold["correction_mutation_status"] == "PASS"
    assert completed_cold["reason_codes"] == ["FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"]
    assert _bytes(_rows(rig)) == before
    (rig.output / "integration-recorder.json").write_text(json.dumps({
        "prewrapped": prewrapped, "physical_issue": recorder.issue,
        "actual_stub_cpa_calls": len(judge.calls), "durable_rows": len(rows),
        "stub_audio_dispatches": len(rig.transport.calls),
        "rows_unchanged_after_cold": True, "cold_gate": outcome["gate"],
        "external_real_provider_calls": 0,
        "admitted_minimal_fixture_cold_reasons": original_cold["reason_codes"],
        "normal_correction_pass_cold_reasons": completed_cold["reason_codes"],
        "row_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
    }, indent=2))


def test_new_relisten_loop_preserves_unknown_original_planner_and_does_not_resend(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    witness = acquire_narrow(rig)
    finding = pending_finding(rig, rig.check, witness)
    original = deepcopy(finding)
    calls = []
    def unknown(prompt):
        calls.append(prompt)
        raise RuntimeError("SYNTHETIC_ORIGINAL_PLANNER_OUTCOME_UNKNOWN")
    unknown.cpa_cache_identity = MODEL
    _, first = auditor.adjudicate_context_finding(
        rig.srt, finding, entity_verifier=rig.verifier, clip_context=rig.context,
        judge_llm_call=unknown, require_complete_utterance_support=True)
    assert len(calls) == 1 and len(rig.transport.calls) == 1
    rows = _rows(rig)
    assert len(rows) == 1 and json.loads(rows[0].read_bytes())["status"] == "OUTCOME_UNKNOWN"
    before = _bytes(rows)
    carried = {**rig.finding, "exact_release_adjudication": deepcopy(first)}
    output, again = auditor.adjudicate_context_finding(
        rig.srt, carried, entity_verifier=rig.verifier, clip_context=rig.context,
        judge_llm_call=unknown, require_complete_utterance_support=True)
    assert output == rig.srt and again.get("repaired") is False
    assert len(calls) == 1 and len(rig.transport.calls) == 1
    assert "PRIOR_CALL_OUTCOME_UNKNOWN" in json.dumps(again)
    assert _bytes(_rows(rig)) == before and finding == original


def test_unconsumed_cold_range_survives_partial_unknown_but_new_cpa_is_refused(tmp_path, monkeypatch):
    rig = build_offline_rig(tmp_path, monkeypatch)
    witness = acquire_narrow(rig)
    finding = pending_finding(rig, rig.check, witness)
    available, first, _ = acquire_range(rig, finding, rig.check, observe_plan())
    assert first["status"] == "OBSERVED"
    native_path = Path(rig.verifier.relisten_budget_state()["receipt_path"])
    native_before = native_path.read_bytes()
    original = deepcopy(finding)
    sent = []
    def paid(prompt):
        sent.append(prompt)
        pytest.fail("Unknown partial CPA usage cannot admit a new paid request")
    paid.cpa_cache_identity = MODEL
    proof = {"partial_logical_usage_status": "UNKNOWN_PRESERVED_IN_ORIGINAL_EVENTS",
             "partial_logical_adjudication_count": None}
    authority = {"retained_reviewed_draft": {"partial_evidence": proof}}
    authority_before = deepcopy(authority)
    guarded = with_partial_usage_guard(paid, authority)
    resumed, cold = recovery.recover_semantic_audio_range(
        srt_text=rig.srt, finding=available, check_request=rig.check,
        entity_verifier=rig.verifier, llm_call=guarded)
    assert cold["status"] == "OBSERVED" and cold["served_from_cache"] is True
    assert cold["provider_calls_this_attempt"] == cold["new_effective_observations_this_attempt"] == 0
    assert cold["mutation_authorized"] is False

    output, rejected = auditor.adjudicate_context_finding(
        rig.srt, resumed, entity_verifier=rig.verifier, clip_context=rig.context,
        judge_llm_call=guarded, require_complete_utterance_support=True)
    assert output == rig.srt and rejected.get("repaired") is False
    assert "RETAINED_PARTIAL_CPA_USAGE_UNKNOWN" in json.dumps(rejected)
    assert not sent and len(rig.transport.calls) == 2
    assert native_path.read_bytes() == native_before
    assert finding == original and authority == authority_before
    assert not (rig.output / "saved-authority.json").exists()
    rows = [json.loads(path.read_bytes()) for path in _rows(rig)]
    assert rows and all(row["status"] == "NOT_DISPATCHED" for row in rows)


def _complete_current_correction_review(rig):
    """Generate the correction pass through its actual producer consumer.

    The admitted fixture's missing-status audit is preserved as a negative
    control. This fresh process obtains a complete no-mutation correction
    audit from normal discovery/routing instead of assigning it a PASS flag.
    """
    script = r'''
import hashlib, json, pathlib, sys
from types import SimpleNamespace
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.final_review_contract import validate_final_review_release
root=pathlib.Path(sys.argv[1])
srt=(root/"saved.srt").read_text()
authority=json.loads((root/"saved-authority.json").read_text())
context=json.loads((root/"saved-context.json").read_text())
calls=[]
def reply(prompt):
    calls.append(hashlib.sha256(prompt.encode()).hexdigest())
    return '{"findings": []}'
reply.cpa_cache_identity={"transport":"offline-integration","models":["synthetic-protocol-cpa"],"effort":"high"}
def no_audio(*args,**kwargs):
    raise AssertionError("COMPLETE_COLD_REVIEW_MUST_NOT_DISPATCH_AUDIO")
pipeline._build_final_review_llm_call=lambda: reply
adapters=SimpleNamespace(review_glossary=lambda: "")
output,correction=pipeline._run_final_review(
    srt_text=srt,chat_authority_audit=authority,handled_entity_cues=set(),
    verify_confusable_entity=no_audio,adapters=adapters,clip_context=context,
    source_media_sha256=context["pieces"][0]["source_media_sha256"])
assert output==srt
assert correction["applied_count"]==0
review=pipeline._run_exact_final_release_review(
    srt_text=output,correction_audit=correction,adapters=adapters,
    authoritative_chat=(),selection_hook="",clip_context=context,
    verified_authority_audit=authority,verify_confusable_entity=no_audio,
    final_review_llm=reply,pronoun_audit_llm=reply,
    source_media_sha256=context["pieces"][0]["source_media_sha256"])
assert review["correction_mutation_authority"]["status"]=="PASS"
assert review["release_gate"]=="BLOCK"
assert review["reason_codes"]==["FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"]
try:
    validate_final_review_release(review,expected_srt_sha256=review["reviewed_srt_sha256"])
except ValueError:
    pass
else:
    raise AssertionError("Boundary remains required")
(root/"normal-correction-pass.json").write_text(json.dumps(correction,ensure_ascii=False))
(root/"normal-correction-cold-current-review.json").write_text(json.dumps(review,ensure_ascii=False))
print(json.dumps({"correction_status":correction["status"],
    "correction_mutation_status":review["correction_mutation_authority"]["status"],
    "reason_codes":review["reason_codes"],"gate":review["release_gate"],
    "stub_text_calls":len(calls),"prompt_sha256s":calls,"external_real_provider_calls":0}))
'''
    result = subprocess.run([sys.executable, "-B", "-c", script, str(rig.output)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
    (rig.output / "normal-correction-cold-process.json").write_text(json.dumps({
        "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _unconsumed_assisted_material(rig):
    witness = acquire_narrow(rig)
    original = pending_finding(rig, rig.check, witness)
    first_input, first, _ = acquire_range(rig, original, rig.check, observe_plan())
    assert first["status"] == "OBSERVED"
    check = auditor.build_context_adjudication_request(rig.srt, first_input, clip_context=rig.context)
    carried = pending_finding(rig, check, witness, previous=original["exact_release_adjudication"])
    carried["exact_release_adjudication"]["semantic_audio_recovery"] = first
    current, second, _ = acquire_range(rig, carried, check, assisted_plan())
    assert second["status"] == "OBSERVED"
    return witness, current, second["consumer_check_request"]


@pytest.mark.parametrize("status", ["COMPLETED", "STARTED", "ERROR", "OUTCOME_UNKNOWN"])
def test_assisted_information_is_consumed_across_fresh_consumer_and_offered_states(tmp_path, monkeypatch, status):
    from src.autoslice.targeted_relisten import with_unconsumed_assisted_plan
    rig = build_offline_rig(tmp_path, monkeypatch)
    witness, finding, check = _unconsumed_assisted_material(rig)
    original = deepcopy(finding)
    recorder = SemanticAttemptRecorder(srt_text=rig.srt, finding=finding,
        clip_context=rig.context, offset_ms=rig.offset_ms)
    planned = with_unconsumed_assisted_plan(finding=finding, check_request=check, recorder=recorder)
    assert planned["continuation_plan"]["action"] == "expand_context"
    refs = planned["semantic_retry_evidence_basis"]["observations"]
    assert len(refs) == 1 and refs[0]["input_information"]["context_source_sha256s"]
    recorder.update_material(srt_text=rig.srt, finding=planned,
        clip_context=rig.context, offset_ms=rig.offset_ms)
    delegate = public_judge(rig)
    calls = []
    def send(prompt):
        calls.append(prompt)
        if status == "STARTED":
            raise KeyboardInterrupt("synthetic interrupted request")
        if status in {"ERROR", "OUTCOME_UNKNOWN"}:
            raise RuntimeError("synthetic uncertain transport outcome")
        return delegate(prompt)
    send.cpa_cache_identity = MODEL
    full_check = auditor.build_context_adjudication_request(rig.srt, planned, clip_context=rig.context)
    # Record one complete synthetic CPA input through the real durable
    # boundary. This test grants no word decision or typed mutation authority.
    prompt = json.dumps({"check_request": full_check,
        "continuation_plan": planned["continuation_plan"]}, ensure_ascii=False, sort_keys=True)
    def consume():
        return recorder.wrap(send)(prompt)
    if status == "STARTED":
        with pytest.raises(KeyboardInterrupt):
            consume()
    elif status in {"ERROR", "OUTCOME_UNKNOWN"}:
        with pytest.raises(RuntimeError):
            consume()
    else:
        consume()
    paths = _rows(rig)
    assert len(paths) == len(calls) == 1
    row = json.loads(paths[0].read_bytes())
    if status == "ERROR":
        # Legacy ERROR has unknown dispatch semantics. Only this synthetic
        # fixture's lifecycle spelling changes; request/raw hashes stay intact.
        assert row["status"] == "OUTCOME_UNKNOWN"
        row["status"] = "ERROR"
        paths[0].write_text(json.dumps(row, ensure_ascii=False))
    else:
        assert row["status"] == status
    before = _bytes(paths)
    next_output = tmp_path / "another-consumer"
    next_output.mkdir()
    monkeypatch.chdir(next_output)
    fresh = SemanticAttemptRecorder(srt_text=rig.srt, finding=finding,
        clip_context=rig.context, offset_ms=rig.offset_ms)
    assert fresh.directory == recorder.directory
    resumed = with_unconsumed_assisted_plan(finding=finding, check_request=check, recorder=fresh)
    assert resumed == finding and "continuation_plan" not in resumed
    assert _bytes(_rows(rig)) == before and finding == original
    assert len(calls) == 1 and len(rig.transport.calls) == 3


@pytest.mark.parametrize("defect", ["missing_original", "conflicting_input_claim"])
def test_assisted_plan_refuses_history_whose_original_range_cannot_be_reopened(tmp_path, monkeypatch, defect):
    from src.autoslice.targeted_relisten import with_unconsumed_assisted_plan
    rig = build_offline_rig(tmp_path, monkeypatch)
    _, finding, check = _unconsumed_assisted_material(rig)
    recorder = SemanticAttemptRecorder(srt_text=rig.srt, finding=finding,
        clip_context=rig.context, offset_ms=rig.offset_ms)
    assert "continuation_plan" in with_unconsumed_assisted_plan(
        finding=finding, check_request=check, recorder=recorder)
    damaged = deepcopy(finding)
    if defect == "missing_original":
        damaged["_prior_range_acoustic_observations"][0]["artifact_bindings"]["raw_response_path"] = str(
            tmp_path / "missing-original.json")
    else:
        alias = deepcopy(damaged["_prior_range_acoustic_observations"][-1])
        alias["targeted_relisten_input"]["admission"]["input_information"]["context_source_sha256s"] = ["0" * 64]
        damaged["_prior_range_acoustic_observations"].append(alias)
    before = _bytes(_rows(rig))
    assert "continuation_plan" not in with_unconsumed_assisted_plan(
        finding=damaged, check_request=check, recorder=recorder)
    assert _bytes(_rows(rig)) == before and len(rig.transport.calls) == 3
