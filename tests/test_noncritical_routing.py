"""Noncritical classifications must not suppress the ordinary decision chain.

All observations and model replies here are synthetic offline fixtures. The
ordinary parser, adjudicator, mutation consumer and release validator remain real.
"""
from copy import deepcopy
import hashlib
import json
import socket

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice.final_review_contract import (
    FinalReviewContractError, is_audio_decided_keep_current,
    is_noncritical_disclosed, validate_final_review_release,
)
from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
from src.autoslice.unresolved_findings_gate import unresolved_findings_problem
from tests.test_final_review_meaning_policy import equivalent, _audit
from tests.test_span_phoneme_resolution import model_for, srt


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))

    def forbidden(*args, **kwargs):
        pytest.fail("No real provider/network in noncritical routing tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _finding(source, *, repair_class="phonetic", proposed="我把杯子放这里"):
    # Let ordinary discovery compile its current risk and question bindings.
    raw = {
        "cue": 1, "kind": "context", "repair_class": repair_class,
        "suspect": "儿" if proposed else "我把杯子放这儿",
        "proposed_full_cue": proposed,
        "why": "同一放置地点的口语词形仍需听音确认",
        "content_risk": "noncritical_uncertainty",
        "risk_basis": "这儿与这里指向相同地点，仍须确认实际口语字词",
        "meaning_equivalence": equivalent(
            "这儿/这里的口语词形", "说话人把杯子放在当前指示地点"),
    }
    return auditor.audit_final_subtitles(
        source, llm_call=lambda _: json.dumps({"findings": [raw]}, ensure_ascii=False),
        extract_json=json.loads,
    )[0]


class BoundWitness:
    """One synthetic configured witness, with request-exact cache probing."""
    def __init__(self, heard):
        self.heard = heard
        self.calls = []
        self.cache = {}

    def __call__(self, request):
        self.calls.append(deepcopy(request))
        row = {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "blind_pinyin",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED", "target_audible": True,
            "heard_pinyin": self.heard,
            "syllable_count": len(self.heard.split()),
            "uncertain_positions": [], "confidence": 0.9,
            "source_media_sha256": "a" * 64,
            "audio_clip_sha256": hashlib.sha256(b"offline audio fixture").hexdigest(),
            "prompt_sha256": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
            "response_sha256": hashlib.sha256(self.heard.encode()).hexdigest(),
            "provider": "agy", "model": "offline-routing-fixture",
            "audio_start_ms": request["matched_start_ms"] + request["source_media_timeline_offset_ms"],
            "audio_end_ms": request["matched_end_ms"] + request["source_media_timeline_offset_ms"],
            "reason": "Synthetic complete listening observation",
        }
        self.cache[request["request_sha256"]] = deepcopy(row)
        return row

    def probe_witness_cache(self, request):
        row = self.cache.get(request["request_sha256"])
        return {**deepcopy(row), "served_from_cache": True} if row else None


def _complete_judge(choice):
    word_model = model_for(
        choice=choice, target_quote="儿" if choice == "CURRENT" else "里",
        blind_quote="er" if choice == "CURRENT" else "li",
        outside=False, audio=True,
    )
    calls = []

    def judge(prompt):
        calls.append(prompt)
        reply = json.loads(word_model(prompt))
        # A text-first preference requests audio. Only the subsequent reply
        # with actual supplied observations explicitly completes that duty.
        if "\nSPAN_REVIEW_INPUT\n" in prompt:
            data = json.loads(prompt.split("\nSPAN_REVIEW_INPUT\n", 1)[1]
                              .split("\nEND_SPAN_REVIEW_INPUT\n", 1)[0])
            if data["observations"]:
                reply.update(needs_audio=False,
                             current_utterance_supported=choice == "CURRENT",
                             current_utterance_support_reason="The supplied complete utterance is supported")
                reply["span_review"].update(target_needs_audio=False, audio_questions=[])
        return json.dumps(reply, ensure_ascii=False)

    judge.calls = calls
    judge.cpa_cache_identity = {"models": ["offline-routing-fixture"], "effort": "medium"}
    return judge


@pytest.mark.parametrize("choice", ["CURRENT", "PROPOSED"])
def test_complete_fixture_already_satisfies_ordinary_context_contract(choice):
    # This control fails if the new fixture is incomplete, independently of
    # the exact-final routing fix. Do not weaken it to make routing look good.
    source = srt("我把杯子放这儿")
    row = _finding(source)
    witness = BoundWitness("wo ba bei zi fang zhe " + ("er" if choice == "CURRENT" else "li"))
    judge = _complete_judge(choice)
    output, decision = auditor.adjudicate_context_finding(
        source, row, entity_verifier=witness, judge_llm_call=judge,
        require_complete_utterance_support=True,
    )
    assert witness.calls and judge.calls
    assert decision["policy_branch"] == (
        "JUDGE_KEEPS_CURRENT" if choice == "CURRENT" else "WITNESS_JUDGE_APPLY_PROPOSED")
    assert decision["repaired"] is (choice == "PROPOSED")
    assert output == (source if choice == "CURRENT" else srt("我把杯子放这里"))


@pytest.mark.parametrize("choice", ["CURRENT", "PROPOSED"])
def test_noncritical_public_adjudication_produces_consumable_decision(choice):
    source = srt("我把杯子放这儿")
    row = _finding(source)
    assert is_noncritical_disclosed(row) and not is_audio_decided_keep_current(row)
    witness = BoundWitness("wo ba bei zi fang zhe " + ("er" if choice == "CURRENT" else "li"))
    judge = _complete_judge(choice)
    pending, resolved = auditor.adjudicate_exact_release_findings(
        source, [row], entity_verifier=witness, judge_llm_call=judge,
        initial_provider_adjudication_count=62,
    )
    assert witness.calls and judge.calls
    actual, = pending + resolved
    decision = actual["exact_release_adjudication"]
    assert decision["cpa_resource_pressure"]["provider_adjudication_count"] == 63
    if choice == "CURRENT":
        assert not pending and len(resolved) == 1
        assert is_audio_decided_keep_current(actual)
        assert unresolved_findings_problem({"unresolved_findings_disclosed": resolved}) is None
        assert validate_final_review_release(_audit(source, resolved, disclosed=True))["release_gate"] == "PASS"
    else:
        assert len(pending) == 1 and not resolved
        output, repairs = _apply_exact_final_cpa_repairs(source, {"findings": pending})
        assert output == srt("我把杯子放这里") and len(repairs) == 1
        # Application is not a new final review or release.
        with pytest.raises(FinalReviewContractError):
            validate_final_review_release(_audit(source, pending))
    assert row.get("exact_release_adjudication") is None


@pytest.mark.parametrize("repair_class", ["phonetic", "editorial_rendering", "disclosure_only"])
def test_noncritical_provider_disabled_cache_miss_keeps_gate_blocked(repair_class):
    source = srt("我把杯子放这儿")
    row = _finding(source, repair_class=repair_class)

    def forbidden(*args, **kwargs):
        pytest.fail("Provider-disabled cache miss may not dispatch")

    pending, resolved = auditor.adjudicate_exact_release_findings(
        source, [row], entity_verifier=forbidden, judge_llm_call=forbidden,
        provider_calls_allowed=False, initial_provider_adjudication_count=62,
    )
    assert not resolved
    decision = pending[0]["exact_release_adjudication"]
    assert decision["status"] == "SKIPPED_PROVIDER_DISABLED"
    assert decision["cpa_resource_pressure"]["provider_adjudication_count"] == 62
    assert "POSTBURN_UNRESOLVED_FINDINGS" in unresolved_findings_problem(
        {"unresolved_findings_disclosed": pending})
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(_audit(source, pending, disclosed=True))


def test_noncritical_completed_keep_cold_reuses_both_caches_without_provider():
    source = srt("我把杯子放这儿")
    row = _finding(source)
    witness = BoundWitness("wo ba bei zi fang zhe er")
    judge = _complete_judge("CURRENT")
    pending, resolved = auditor.adjudicate_exact_release_findings(
        source, [row], entity_verifier=witness, judge_llm_call=judge,
        initial_provider_adjudication_count=62,
    )
    assert not pending and len(resolved) == 1
    counts = (len(witness.calls), len(judge.calls))
    saved = json.loads(json.dumps(resolved))

    def forbidden(_prompt):
        pytest.fail("Completed same-input CPA must come from its real cache")

    forbidden.cpa_cache_identity = judge.cpa_cache_identity
    # A new budget instance enters through the same public function and
    # consumes the existing typed row, rather than a hand-made KEEP flag.
    cold_pending, cold_resolved = auditor.adjudicate_exact_release_findings(
        source, saved, entity_verifier=witness, judge_llm_call=forbidden,
        provider_calls_allowed=False, initial_provider_adjudication_count=63,
    )
    assert not cold_pending and len(cold_resolved) == 1
    assert counts == (len(witness.calls), len(judge.calls))
    cold = cold_resolved[0]["exact_release_adjudication"]
    assert cold["provider_budget_replay"]["provider_call_count"] == 0
    assert cold["cpa_resource_pressure"]["provider_adjudication_count"] == 63
    assert is_audio_decided_keep_current(cold_resolved[0])
    assert validate_final_review_release(_audit(source, cold_resolved, disclosed=True))["release_gate"] == "PASS"



def test_typed_context_only_keep_needs_no_audio_and_cold_reuses_original_cpa():
    source = srt("我把杯子放这儿")
    row = _finding(source, repair_class="disclosure_only", proposed=None)
    calls = []

    def no_audio(*args, **kwargs):
        pytest.fail("A complete context-only CPA decision creates no listening duty")

    def judge(prompt):
        calls.append(prompt)
        return json.dumps({"status": "UNRESOLVED", "proposed_cue": None,
            "decision": "KEEP_EXISTING", "replacement_text": None,
            "needs_audio": False, "reason": "Complete current context supports retaining this presentation"})

    judge.cpa_cache_identity = {"models": ["offline-context-fixture"], "effort": "medium"}
    pending, resolved = auditor.adjudicate_exact_release_findings(
        source, [row], entity_verifier=no_audio, judge_llm_call=judge,
        initial_provider_adjudication_count=62)
    assert not pending and len(resolved) == 1 and len(calls) == 2
    assert resolved[0]["exact_release_adjudication"]["verdict"]["status"] == "NOT_REQUIRED"
    assert validate_final_review_release(_audit(source, resolved, disclosed=True))["release_gate"] == "PASS"
    saved = json.loads(json.dumps(resolved))

    def no_cpa(_prompt):
        pytest.fail("Complete same-input context-only CPA must cold reuse its original caches")

    no_cpa.cpa_cache_identity = judge.cpa_cache_identity
    pending, replayed = auditor.adjudicate_exact_release_findings(
        source, saved, entity_verifier=no_audio, judge_llm_call=no_cpa,
        provider_calls_allowed=False, initial_provider_adjudication_count=63)
    assert not pending and len(replayed) == 1
    actual = replayed[0]["exact_release_adjudication"]
    assert actual["cpa_resource_pressure"]["provider_adjudication_count"] == 63
    assert actual["provider_budget_replay"]["provider_call_count"] == 0
    assert actual["cpa_missing_proposal_convergence"]["served_from_cache"] is True
    assert validate_final_review_release(_audit(source, replayed, disclosed=True))["release_gate"] == "PASS"
    assert len(calls) == 2


@pytest.mark.parametrize("state", ["UNCERTAIN", "OUTCOME_UNKNOWN"])
def test_noncritical_incomplete_audio_keeps_gate_blocked(state):
    source = srt("我把杯子放这儿")
    row = _finding(source)
    witness = BoundWitness("wo ba bei zi fang zhe er")

    def incomplete(request):
        result = witness(request)
        result.update(status=state, target_audible=None)
        return result

    pending, resolved = auditor.adjudicate_exact_release_findings(
        source, [row], entity_verifier=incomplete, judge_llm_call=_complete_judge("CURRENT"))
    assert witness.calls and pending and not resolved
    assert not is_audio_decided_keep_current(pending[0])
    assert "POSTBURN_UNRESOLVED_FINDINGS" in unresolved_findings_problem(
        {"unresolved_findings_disclosed": pending})
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(_audit(source, pending, disclosed=True))


@pytest.mark.parametrize("drift", ["source", "time", "text", "request", "response"])
def test_noncritical_saved_range_reaches_novelty_and_rejects_drift(tmp_path, drift):
    from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation
    from src.autoslice.retained_range_context import persisted_native_range_artifacts
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    from tests.test_independent_context_adjudication import _native_range_artifacts

    old, original_finding, clip, request, raw, paths = _native_range_artifacts(tmp_path, "mai")
    valid, reason = validate_range_acoustic_observation(old, request, raw)
    assert valid is not None, reason
    original_request = auditor.build_context_adjudication_request(old,
        {**original_finding, "_prior_range_acoustic_observations": [raw]},
        clip_context=clip, source_media_timeline_offset_ms=100)
    references = persisted_native_range_artifacts({"request": original_request})
    assert len(references) == 1
    original_bytes = {k: p.read_bytes() for k, p in paths.items()}
    current = old.replace("当前句", "邻句已修复")
    raw_target = {"cue": 1, "kind": "context", "suspect": "第一句",
        "repair_class": "phonetic", "proposed_full_cue": None,
        "why": "Current target requires complete utterance evidence",
        "content_risk": "noncritical_uncertainty", "risk_basis": "同一整句的口语细节仍待听音",
        "meaning_equivalence": equivalent("口语细节", "说话人说出第一句")}
    target, = auditor.audit_final_subtitles(current,
        llm_call=lambda _: json.dumps({"findings": [raw_target]}), extract_json=json.loads)
    assert is_noncritical_disclosed(target)
    calls = []

    def judge(prompt):
        calls.append(prompt)
        return json.dumps({"status": "UNRESOLVED", "decision": "UNRESOLVED",
            "reason": "Complete range is still inconclusive", "needs_audio": True,
            "replacement_text": None})

    judge.cpa_cache_identity = {"model": "offline-range-fixture"}
    for question in ("old1", "old2", "old3"):
        SemanticAttemptRecorder(srt_text=current, finding=target, clip_context=clip,
            offset_ms=100).wrap(judge)(question)
    pending, resolved = auditor.adjudicate_exact_release_findings(
        current, [target], entity_verifier=None, clip_context=clip,
        source_media_timeline_offset_ms=100, judge_llm_call=judge,
        initial_provider_adjudication_count=62, prior_range_acoustic_observations=references)
    assert len(calls) > 3 and not resolved
    assert "整句范围听写" in calls[3]
    assert pending[0]["continuation_plan"]["action"] == "expand_context"
    decision = pending[0]["exact_release_adjudication"]
    assert decision["cpa_resource_pressure"]["provider_adjudication_count"] == 63
    assert decision["semantic_retry_progress"]["events"][0]["changed_material_fields"] == ["audio_observations_sha256"]
    assert original_bytes == {k: p.read_bytes() for k, p in paths.items()}

    # Alter only synthetic artifacts or this replay's binding. Never re-sign an
    # observation or give the failed original range a new evidence identity.
    altered = deepcopy(references)
    changed_clip = deepcopy(clip)
    offset = 100
    if drift == "source":
        changed_clip["source_media_sha256"] = "f" * 64
    elif drift == "time":
        offset = 101
    elif drift == "text":
        paths["reviewed_srt"].write_text(old.replace("第一句", "forged original target"))
    elif drift == "request":
        envelope = json.loads(paths["native_envelope"].read_text())
        envelope["request"]["request_sha256"] = "f" * 64
        paths["native_envelope"].write_text(json.dumps(envelope))
    else:
        paths["raw_response"].write_bytes(b"changed synthetic response")
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append("provider")
        pytest.fail("No new evidence must not reopen an already failed range plan")

    forbidden.cpa_cache_identity = judge.cpa_cache_identity
    blocked, finished = auditor.adjudicate_exact_release_findings(
        current, [target], entity_verifier=forbidden, clip_context=changed_clip,
        source_media_timeline_offset_ms=offset, judge_llm_call=forbidden,
        provider_calls_allowed=False, initial_provider_adjudication_count=63,
        prior_range_acoustic_observations=altered)
    assert blocked and not finished and not attempts
    assert "continuation_plan" not in blocked[0]
    assert "POSTBURN_UNRESOLVED_FINDINGS" in unresolved_findings_problem(
        {"unresolved_findings_disclosed": blocked})
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(_audit(current, blocked, disclosed=True))


def test_noncritical_enabled_without_novelty_does_not_pay_again():
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    source = srt("我把杯子放这儿")
    row = _finding(source, proposed=None)
    calls = []

    def old_judge(prompt):
        calls.append(prompt)
        return '{"status":"UNRESOLVED","decision":"UNRESOLVED","needs_audio":true}'

    old_judge.cpa_cache_identity = {"model": "offline-no-novelty-fixture"}
    clip = {"source_media_sha256": "a" * 64}
    for question in ("old1", "old2", "old3"):
        SemanticAttemptRecorder(srt_text=source, finding=row, clip_context=clip,
            offset_ms=0).wrap(old_judge)(question)

    def forbidden(*args, **kwargs):
        pytest.fail("An enabled provider is not permission to repeat absent novelty")

    forbidden.cpa_cache_identity = old_judge.cpa_cache_identity
    pending, resolved = auditor.adjudicate_exact_release_findings(
        source, [row], entity_verifier=forbidden, judge_llm_call=forbidden, clip_context=clip,
        provider_calls_allowed=True, initial_provider_adjudication_count=62)
    assert pending and not resolved and len(calls) == 3
    serialized = json.dumps(pending)
    assert "CONTINUATION_VALUE_PLAN_REQUIRED" in serialized
    assert "POSTBURN_UNRESOLVED_FINDINGS" in unresolved_findings_problem(
        {"unresolved_findings_disclosed": pending})


def test_correction_with_only_noncritical_rows_preserves_normal_exact_agenda():
    source = srt("我把杯子放这儿")
    row = _finding(source)
    corrected, route = auditor.route_findings(source, [row],
        protected_term_set=frozenset(), registered_term_set=frozenset(), exact_cue_canon_set=frozenset())
    assert corrected == source and route["applied_count"] == 0
    assert len(route["findings"]) == 1
    forwarded = route["findings"][0]
    assert forwarded["base_text_sha256"] == row["base_text_sha256"]
    assert forwarded["routed"] == "disclosure"
    pending, resolved = auditor.adjudicate_exact_release_findings(
        corrected, route["findings"], entity_verifier=BoundWitness("wo ba bei zi fang zhe er"),
        judge_llm_call=_complete_judge("CURRENT"))
    assert not pending and len(resolved) == 1



@pytest.mark.parametrize("drift", ["srt", "source", "time", "context", "cache_miss", "bootstrap_cache_miss", "pending_audio", "phonetic"])
def test_typed_context_only_cache_reuse_cannot_close_changed_or_unfinished_duty(tmp_path, drift):
    import os
    from pathlib import Path
    source = srt("我把杯子放这儿")
    row = _finding(source, repair_class="disclosure_only", proposed=None)

    def no_provider(*args, **kwargs):
        pytest.fail("The isolated replay must not dispatch a provider")

    def judge(_prompt):
        return json.dumps({"status": "UNRESOLVED", "proposed_cue": None,
            "decision": "KEEP_EXISTING", "replacement_text": None,
            "needs_audio": False, "reason": "Complete current context supports this presentation"})

    judge.cpa_cache_identity = {"models": ["offline-context-drift-fixture"], "effort": "medium"}
    pending, resolved = auditor.adjudicate_exact_release_findings(
        source, [row], entity_verifier=no_provider, judge_llm_call=judge)
    assert not pending and len(resolved) == 1
    assert validate_final_review_release(_audit(source, resolved, disclosed=True))["release_gate"] == "PASS"
    saved = json.loads(json.dumps(resolved))
    actual_source, clip, offset = source, None, 0
    if drift == "srt":
        actual_source = source.replace("这儿", "那儿")
    elif drift == "source":
        clip = {"source_media_sha256": "b" * 64}
    elif drift == "time":
        offset = 1
    elif drift == "context":
        clip = {"candidate_id": "another-context", "source_sha256": "b" * 64,
                "topic_summary": "A different source context with the same subtitle bytes"}
    elif drift in {"cache_miss", "bootstrap_cache_miss"}:
        removed = 0
        for path in Path(os.environ["AUTOSLICE_BASE"]).rglob("*.json"):
            payload = json.loads(path.read_text())
            wanted = ("proposal-rebuild-cache.v1" if drift == "bootstrap_cache_miss"
                      else "missing-proposal-cpa-convergence-cache.v1")
            stage = ('missing-proposal' if drift == 'bootstrap_cache_miss' else 'missing-proposal-convergence')
            raw_match = (payload.get('schema_version') == 'completed-review-response.v1'
                         and payload.get('binding', {}).get('stage') == stage)
            if payload.get("schema_version") == wanted or raw_match:
                path.unlink()
                removed += 1
        assert removed == 1
    elif drift == "pending_audio":
        saved[0]["exact_release_adjudication"]["witness_judge"]["judge"].update(
            needs_audio=True, needs_audio_requested=True, needs_audio_request_unresolved=True)
    else:
        current = _finding(source, repair_class="phonetic", proposed=None)
        current["exact_release_adjudication"] = saved[0]["exact_release_adjudication"]
        saved = [current]
    no_provider.cpa_cache_identity = judge.cpa_cache_identity
    pending, resolved = auditor.adjudicate_exact_release_findings(
        actual_source, saved, entity_verifier=no_provider, judge_llm_call=no_provider,
        clip_context=clip, source_media_timeline_offset_ms=offset,
        provider_calls_allowed=False, initial_provider_adjudication_count=63)
    assert pending and not resolved
    assert pending[0]["exact_release_adjudication"]["cpa_resource_pressure"]["provider_adjudication_count"] == 63
    assert "POSTBURN_UNRESOLVED_FINDINGS" in unresolved_findings_problem(
        {"unresolved_findings_disclosed": pending})
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(_audit(actual_source, pending, disclosed=True))


def test_complete_audio_keep_restores_in_a_new_process(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    source = srt("我把杯子放这儿")
    witness = BoundWitness("wo ba bei zi fang zhe er")
    judge = _complete_judge("CURRENT")
    pending, resolved = auditor.adjudicate_exact_release_findings(
        source, [_finding(source)], entity_verifier=witness, judge_llm_call=judge,
        initial_provider_adjudication_count=62)
    assert not pending and len(resolved) == 1
    saved_path = tmp_path / "cold-input.json"
    saved_path.write_text(json.dumps({"source": source, "rows": resolved,
        "witness_cache": witness.cache, "identity": judge.cpa_cache_identity}))
    code = r'''
import json, socket, sys
from pathlib import Path
from src.autoslice.final_review_auditor import adjudicate_exact_release_findings
from src.autoslice.final_review_contract import validate_final_review_release
from tests.test_final_review_meaning_policy import _audit
payload = json.loads(Path(sys.argv[1]).read_text())
def forbidden(*a, **kw):
    raise AssertionError("Cold process attempted a real provider or network")
socket.socket.connect = forbidden
socket.create_connection = forbidden
class ColdWitness:
    def __call__(self, request):
        forbidden()
    def probe_witness_cache(self, request):
        row = payload['witness_cache'].get(request['request_sha256'])
        return {**row, 'served_from_cache': True} if row else None
forbidden.cpa_cache_identity = payload['identity']
pending, resolved = adjudicate_exact_release_findings(payload['source'], payload['rows'],
    entity_verifier=ColdWitness(), judge_llm_call=forbidden, provider_calls_allowed=False,
    initial_provider_adjudication_count=63)
assert not pending and len(resolved) == 1
assert resolved[0]['exact_release_adjudication']['provider_budget_replay']['provider_call_count'] == 0
assert resolved[0]['exact_release_adjudication']['cpa_resource_pressure']['provider_adjudication_count'] == 63
assert validate_final_review_release(_audit(payload['source'], resolved, disclosed=True))['release_gate'] == 'PASS'
print(json.dumps({'resolved':len(resolved),'new_provider_calls':0,'cumulative_adjudications':63}))
'''
    result = subprocess.run([sys.executable, "-B", "-c", code, str(saved_path)],
        cwd=Path(__file__).resolve().parents[1], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout) == {"resolved": 1, "new_provider_calls": 0, "cumulative_adjudications": 63}
