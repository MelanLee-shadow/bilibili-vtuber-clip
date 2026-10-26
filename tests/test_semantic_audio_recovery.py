"""Recovery unit seams and source-bound offline public-entry regressions.

Only complete-protocol fixtures may assert a new usable audio observation.
Artifactless rows below are explicit rejection cases, never acoustic success.
"""
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

import test_targeted_relisten_public_contract as protocol
from src.autoslice import semantic_audio_recovery as recovery
from src.autoslice import targeted_relisten as policy
from src.autoslice.final_review_auditor import build_context_adjudication_request

SOURCE = "1\n00:00:01,000 --> 00:00:03,000\n前面开始解释\n\n2\n00:00:03,000 --> 00:00:03,720\n了\n\n3\n00:00:03,720 --> 00:00:06,000\n后面把话说完\n"
FINDING = {"cue_index": 2, "suspect": "了", "suggestion": "说了",
           "proposed_full_cue": "说了", "repair_class": "spoken_unit"}


def missing_proposal_case(*, clip_context=None):
    finding = {"cue_index": 2, "suspect": "了", "suggestion": None,
               "proposed_full_cue": None, "repair_class": "phonetic"}
    from src.autoslice.final_review_auditor import _candidate_blind_missing_proposal_scaffold
    request = build_context_adjudication_request(
        SOURCE, _candidate_blind_missing_proposal_scaffold(SOURCE, finding),
        source_media_timeline_offset_ms=30000, clip_context=clip_context,
    )
    gate = {"schema_version": "subtitle-missing-proposal-cpa-convergence.v1",
            "status": "UNRESOLVED", "decision": "UNRESOLVED", "needs_audio": True,
            "decision_authority": "CPA_JUDGE", "mutation_authorized": False,
            "timing_immutable": True, "reason_code": "CPA_CONVERGENCE_UNRESOLVED",
            "reason": "单句听写冲突，需要完整话语补听", "cue_index": 2,
            "current_cue_sha256": "sha256:" + hashlib.sha256("了".encode()).hexdigest(),
            "final_srt_sha256": "sha256:" + hashlib.sha256(SOURCE.encode()).hexdigest(),
            "prompt_sha256": "a" * 64, "completion_sha256": "b" * 64,
            "context_sha256": "c" * 64}
    finding["exact_release_adjudication"] = {
        "schema_version": "subtitle-span-adjudication.v1", "status": "UNCERTAIN",
        "repaired": False, "decision_authority": "CPA_JUDGE",
        "policy_branch": "JUDGE_REQUESTS_MORE_AUDIO", "request": request,
        "cpa_missing_proposal_convergence": gate,
    }
    return finding, request


def setup_case():
    finding = {"cue_index": 2, "suspect": "了", "suggestion": "说了",
               "proposed_full_cue": "说了", "repair_class": "spoken_unit"}
    request = build_context_adjudication_request(SOURCE, finding, source_media_timeline_offset_ms=30000)
    finding["exact_release_adjudication"] = {
        "status": "UNCERTAIN", "repaired": False,
        "reason_code": "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED",
        "policy_branch": "HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY",
        "decision_authority": "CPA_PROPOSAL_ONLY", "witness_authority": "ACOUSTIC_WITNESS_REQUIRED",
        "request": request,
        "history_convergence_acoustic_witness": {
            "schema_version": "history-convergence-acoustic-witness-authority.v1", "status": "BLOCK",
            "reason_code": "CPA_REQUESTS_MORE_AUDIO", "needs_audio": True,
            "reason": "目标太短，需要完整句的原音", "judge_prompt_sha256": "a" * 64,
            "judge_completion_sha256": "b" * 64, "judge_served_from_cache": False,
        },
    }
    return finding, request


def ordinary_audio_case():
    finding, request = setup_case()
    finding["exact_release_adjudication"] = {
        "schema_version": "subtitle-span-adjudication.v1", "status": "OBSERVED",
        "repaired": False, "timing_immutable": True,
        "decision_authority": "CPA_JUDGE", "witness_authority": "EVIDENCE_ONLY",
        "policy_branch": "JUDGE_REQUESTS_MORE_AUDIO", "request": request,
        "witness_judge": {"judge": {
            "schema_version": "acoustic-witness-adjudication.v1", "status": "JUDGED",
            "choice": "CURRENT", "needs_audio": True, "needs_audio_requested": True,
            "reason": "短窗不能辨清前半句，需要完整话语原音",
            "prompt_sha256": "a" * 64, "completion_sha256": "b" * 64,
            "check_request_sha256": request["request_sha256"],
        }},
    }
    return finding, request



@pytest.fixture
def legacy_rig(tmp_path, monkeypatch, request):
    artifact_root = os.environ.get("AUTOSLICE_RELISTEN_TEST_ARTIFACTS")
    if artifact_root:
        tmp_path = Path(artifact_root) / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
        tmp_path.mkdir(parents=True, exist_ok=False)
    rig = protocol.build_offline_rig(
        tmp_path, monkeypatch, srt=SOURCE, finding=FINDING,
        offset_ms=30000, source_duration_ms=40000)
    # One heard syllable for the one-codepoint target; the wider window tests
    # the potentially omitted sentence onset, not recognition quality.
    rig.transport.pinyin = "le"
    return rig


def full_case(rig, kind="ordinary"):
    """Add actual audio/source/raw material to each existing typed gate shape."""
    from src.autoslice.final_review_auditor import _candidate_blind_missing_proposal_scaffold
    finding, _ = missing_proposal_case() if kind == "missing" else setup_case()
    scaffold = (_candidate_blind_missing_proposal_scaffold(SOURCE, finding)
                if kind == "missing" else finding)
    request = build_context_adjudication_request(
        SOURCE, scaffold, clip_context=rig.context, source_media_timeline_offset_ms=30000)
    witness = protocol.acquire_narrow(rig, request)
    ordinary = protocol.pending_finding(rig, request, witness)
    if kind == "ordinary":
        return ordinary, request, witness
    prior = finding["exact_release_adjudication"]
    prior["request"] = request
    prior["verdict"] = witness
    # The legacy gate adapter is the unit under test. Its prompt/completion
    # references point to a real complete-protocol CPA stub, not hex placeholders.
    judge = ordinary["exact_release_adjudication"]["witness_judge"]["judge"]
    prior["offline_original_cpa_judgment"] = deepcopy(judge)
    if kind == "missing":
        gate = prior["cpa_missing_proposal_convergence"]
        gate.update(prompt_sha256=judge["prompt_sha256"],
                    completion_sha256=judge["completion_sha256"],
                    context_sha256=recovery._digest(request.get("clip_context_binding") or {}))
    else:
        gate = prior["history_convergence_acoustic_witness"]
        gate.update(judge_prompt_sha256=judge["prompt_sha256"],
                    judge_completion_sha256=judge["completion_sha256"])
    return finding, request, witness


def range_plan(first=1, last=3, **extra):
    return {"decision": "OBSERVE", "provider": "gemini",
            "first_cue": first, "last_cue": last, "purpose": "FULL_QA",
            "reason": "把疑点的句首和后续回答纳入实际原音，辨别短句是否遗漏前半句。",
            **extra}


def run_range(rig, finding, request, plan, *, srt=SOURCE):
    prompts = []
    def judge(prompt):
        prompts.append(prompt)
        value = plan(protocol.planner_data(prompt)) if callable(plan) else plan
        return json.dumps(value, ensure_ascii=False)
    output, audit = recovery.recover_semantic_audio_range(
        srt_text=srt, finding=finding, check_request=request,
        entity_verifier=rig.verifier, llm_call=judge)
    return output, audit, prompts


def completed_range(rig, kind="ordinary", *, last=3):
    finding, request, witness = full_case(rig, kind)
    output, audit, _ = run_range(rig, finding, request, range_plan(last=last))
    assert audit["status"] == "OBSERVED", audit
    assert audit["new_effective_observations_this_attempt"] == 1
    assert len(rig.transport.calls) == 2
    return finding, request, witness, output, audit


def current_check(rig, finding, *, srt=SOURCE):
    return build_context_adjudication_request(
        srt, finding, clip_context=rig.context, source_media_timeline_offset_ms=30000)


def plan_cache_identity():
    _, request = missing_proposal_case()
    return {"original_request_sha256": request["request_sha256"],
            "current_request_sha256": request["request_sha256"],
            "current_srt_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
            "history_audio_recovery_sha256": recovery._digest({"typed_fixture": "pending_audio"})}


@pytest.mark.parametrize("decision", ["OBSERVE", "NO_NEW_AUDIO"])
def test_successful_range_plan_survives_downstream_block(tmp_path, monkeypatch, decision):
    """Cache unit seam: a saved routing answer grants no acoustic authority."""
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    prompt, identity, calls = "完整话语取证路由", plan_cache_identity(), []
    raw = json.dumps({"decision": decision, "first_cue": 1, "last_cue": 3,
                      "reason": "完整话语取证路由"})
    def judge(value):
        calls.append(value)
        return raw
    judge.cpa_cache_identity = {"transport": "fixture", "models": ["judge"], "effort": "medium"}
    first, first_cached = recovery._range_plan_completion(prompt, judge, identity)
    cache = next((tmp_path / "cache" / "semantic-range-plans").glob("*.json"))
    saved = cache.read_bytes()
    second, second_cached = recovery._range_plan_completion(prompt, judge, identity)
    assert first == second == raw and first_cached is False and second_cached is True
    assert calls == [prompt] and cache.read_bytes() == saved
    stored = json.loads(saved)
    assert stored["prompt"] == prompt and stored["completion"] == raw
    assert "observations" not in stored and "mutation_authorized" not in stored


@pytest.mark.parametrize("change", ["model", "effort", "prompt", "source_request"])
def test_range_plan_reuse_requires_exact_semantic_and_provider_binding(tmp_path, monkeypatch, change):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    identity, prompt, calls = plan_cache_identity(), "完整话语取证路由", []
    def judge(value):
        calls.append(value)
        return '{"decision":"NO_NEW_AUDIO","reason":"书写不由新听音裁决"}'
    judge.cpa_cache_identity = {"transport": "fixture", "models": ["judge"], "effort": "medium"}
    recovery._range_plan_completion(prompt, judge, identity)
    saved = {p: p.read_bytes() for p in (tmp_path / "cache" / "semantic-range-plans").glob("*.json")}
    if change == "model":
        judge.cpa_cache_identity["models"] = ["new-judge"]
    elif change == "effort":
        judge.cpa_cache_identity["effort"] = "high"
    elif change == "prompt":
        prompt += "；核明句首遗漏的来源"
    else:
        # Pure cache-key variation, not permission to forge a public source.
        identity["current_request_sha256"] = recovery._digest({"different_bound_request": True})
    _, cached = recovery._range_plan_completion(prompt, judge, identity)
    assert len(calls) == 2 and cached is False
    assert all(p.read_bytes() == raw for p, raw in saved.items())


def test_corrupt_saved_range_plan_blocks_without_redispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    identity, prompt, calls = plan_cache_identity(), "完整话语取证路由", []
    def judge(value):
        calls.append(value)
        return '{"decision":"NO_NEW_AUDIO","reason":"仍需正常裁决"}'
    judge.cpa_cache_identity = {"transport": "fixture", "models": ["judge"], "effort": "medium"}
    recovery._range_plan_completion(prompt, judge, identity)
    path = next((tmp_path / "cache" / "semantic-range-plans").glob("*.json"))
    saved = json.loads(path.read_bytes())
    saved["completion"] += " changed"
    path.write_text(json.dumps(saved))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="semantic range plan cache binding or raw response drifted"):
        recovery._range_plan_completion(prompt, judge, identity)
    assert calls == [prompt] and path.read_bytes() == original


@pytest.mark.parametrize('reword_reason', [False, True])
def test_no_new_audio_route_without_proof_remains_cached_diagnosis(legacy_rig, reword_reason):
    rig = legacy_rig
    finding, request, _ = full_case(rig)
    original, prompts = deepcopy(finding), []
    def judge(prompt):
        prompts.append(prompt)
        return '{"decision":"NO_NEW_AUDIO","reason":"仍缺合法穷尽依据"}'
    judge.cpa_cache_identity = {"transport": "fixture", "models": ["judge"], "effort": "medium"}
    def run():
        return recovery.recover_semantic_audio_range(
            srt_text=SOURCE, finding=finding, check_request=request,
            entity_verifier=rig.verifier, llm_call=judge)
    _, first = run()
    path = next((rig.tmp / "base" / "cache" / "semantic-range-plans").glob("*.json"))
    saved = path.read_bytes()
    if reword_reason:
        finding['exact_release_adjudication']['witness_judge']['judge']['reason'] = '同一疑点的规划理由换措辞'
        original = deepcopy(finding)
    output, second = run()
    assert first["status"] == second["status"] == "BLOCK"
    assert first["reason_code"] == second["reason_code"] == "RELISTEN_NO_GAIN_OBSERVATION_PROOF_REQUIRED"
    assert second["range_plan_served_from_cache"] is True
    assert second["no_gain_diagnosis_only"] is True and "no_gain_proof" not in second
    assert len(prompts) == 1 and len(rig.transport.calls) == 1
    assert output == finding == original and path.read_bytes() == saved
    assert second["new_effective_observations_this_attempt"] == 0


def test_malformed_range_answer_is_contract_error_without_reasking(legacy_rig):
    rig = legacy_rig
    finding, request, _ = full_case(rig)
    calls = []
    def judge(prompt):
        calls.append(prompt)
        return 'not JSON'
    judge.cpa_cache_identity = {"transport": "fixture", "models": ["judge"], "effort": "medium"}
    audits = [recovery.recover_semantic_audio_range(srt_text=SOURCE, finding=finding,
        check_request=request, entity_verifier=rig.verifier, llm_call=judge)[1] for _ in range(2)]
    assert len(calls) == 1
    assert all(a['reason_code'] == 'SEMANTIC_RANGE_PLAN_FAILED' and
               a['failure_layer'] == 'CONTRACT_ERROR' for a in audits)
    assert len(rig.transport.calls) == 1  # Only the preexisting real-fixture narrow observation.


def test_candidate_less_cpa_request_enters_existing_semantic_range(legacy_rig):
    from src.autoslice.exact_final_witness_authority import (
        history_audio_recovery_from_adjudication, valid_history_audio_recovery,
    )
    rig = legacy_rig
    finding, request, _ = full_case(rig, "missing")
    original = deepcopy(finding)
    envelope = history_audio_recovery_from_adjudication(finding["exact_release_adjudication"], request=request)
    assert valid_history_audio_recovery(envelope, request=request)
    assert envelope["gate"] == original["exact_release_adjudication"]["cpa_missing_proposal_convergence"]
    assert envelope["gate"]["status"] == "UNRESOLVED"
    output, audit, _ = run_range(rig, finding, request, range_plan())
    assert audit["status"] == "OBSERVED", audit
    assert audit["range_witness_request"]["cue_indexes"] == [1, 2, 3]
    assert len(rig.transport.calls) == 2 and audit["provider_calls_this_attempt"] == 1
    assert audit["new_effective_observations_this_attempt"] == 1
    assert finding == original and output["exact_release_adjudication"] == original["exact_release_adjudication"]


def test_artifactless_success_row_is_not_a_valid_audio_observation(legacy_rig):
    rig = legacy_rig
    finding, request, _ = full_case(rig)
    original = deepcopy(finding)
    def invalid_observer(**kwargs):
        return [{"status": "OBSERVED", "evidence_scope": "RANGE_CONTEXT_ONLY",
                 "mutation_authorized": False}]
    invalid_observer.available_providers = ("gemini",)
    rig.verifier.observe_range_context = invalid_observer
    output, audit, _ = run_range(rig, finding, request, range_plan())
    assert audit["status"] == "BLOCK"
    assert audit["reason_code"] == "SEMANTIC_RANGE_NO_VALID_CONTEXT_EVIDENCE"
    assert audit["new_effective_observations_this_attempt"] == 0
    assert len(rig.transport.calls) == 1 and output == finding == original
    assert audit["observation_projection_before"]["effective_distinct_observations"] == 1


@pytest.mark.parametrize("change", ["needs_audio", "prompt", "current", "srt", "cue", "mutation"])
def test_candidate_less_gate_drift_cannot_request_audio(change):
    from src.autoslice.exact_final_witness_authority import history_audio_recovery_from_adjudication
    finding, request = missing_proposal_case()
    gate = finding["exact_release_adjudication"]["cpa_missing_proposal_convergence"]
    key, value = {"needs_audio": ("needs_audio", False), "prompt": ("prompt_sha256", "invalid"),
                  "current": ("current_cue_sha256", "d" * 64), "srt": ("final_srt_sha256", "d" * 64),
                  "cue": ("cue_index", 3), "mutation": ("mutation_authorized", True)}[change]
    gate[key] = value
    envelope = history_audio_recovery_from_adjudication(finding["exact_release_adjudication"], request=request)
    assert envelope["status"] == "INVALID"



def test_ordinary_explicit_audio_request_enters_range_before_another_text_pass(legacy_rig, monkeypatch):
    """An ordinary needs_audio request executes audio before another text pass."""
    from src.autoslice import final_review_auditor as auditor
    from src.autoslice.exact_final_witness_authority import (
        history_audio_recovery_from_adjudication, valid_history_audio_recovery,
    )
    rig = legacy_rig
    finding, request, _ = full_case(rig)
    original = deepcopy(finding)
    envelope = history_audio_recovery_from_adjudication(finding["exact_release_adjudication"])
    assert valid_history_audio_recovery(envelope, request=request)
    assert envelope["gate"] == finding["exact_release_adjudication"]["witness_judge"]["judge"]
    def ordinary(srt, current, **kwargs):
        assert len(rig.transport.calls) == 2
        assert "exact_release_adjudication" not in current
        assert current["_prior_range_acoustic_observations"]
        return srt, {"status": "UNCERTAIN", "repaired": False}
    monkeypatch.setattr(auditor, "_adjudicate_context_finding", ordinary)
    output, audit = auditor.adjudicate_context_finding(
        SOURCE, finding, entity_verifier=rig.verifier, clip_context=rig.context,
        source_media_timeline_offset_ms=30000,
        judge_llm_call=lambda _: json.dumps(range_plan(), ensure_ascii=False))
    assert output == SOURCE and audit["repaired"] is False
    assert audit["semantic_audio_recovery"]["status"] == "OBSERVED"
    assert audit["semantic_audio_recovery"]["range_witness_request"]["cue_indexes"] == [1, 2, 3]
    assert audit["prior_audio_adjudication"] == original["exact_release_adjudication"]
    assert finding == original


@pytest.mark.parametrize("change", ["legacy_default", "missing_flag", "prompt", "request", "payload", "repaired"])
def test_ordinary_audio_gate_cannot_be_forged_or_inferred_from_legacy_default(change, monkeypatch):
    from src.autoslice import final_review_auditor as auditor
    from src.autoslice.exact_final_witness_authority import history_audio_recovery_from_adjudication
    finding, request = ordinary_audio_case()
    prior = finding["exact_release_adjudication"]
    judge = prior["witness_judge"]["judge"]
    if change == "legacy_default":
        judge["needs_audio_requested"] = False
    elif change == "missing_flag":
        judge.pop("needs_audio_requested")
    elif change == "prompt":
        judge["prompt_sha256"] = "invalid"
    elif change == "request":
        judge["check_request_sha256"] = "d" * 64
    elif change == "payload":
        request["source_media_timeline_offset_ms"] += 1
    else:
        prior["repaired"] = True
    assert history_audio_recovery_from_adjudication(prior)["status"] == "INVALID"
    def verifier(_):
        return None
    verifier.observe_range_context = lambda **_: pytest.fail("invalid gate cannot dispatch audio")
    monkeypatch.setattr(auditor, "_adjudicate_context_finding", lambda *_args, **_kw: pytest.fail("unchanged evidence cannot be text-retried"))
    output, audit = auditor.adjudicate_context_finding(
        SOURCE, finding, entity_verifier=verifier, source_media_timeline_offset_ms=30000,
    )
    assert output == SOURCE and audit["repaired"] is False
    assert audit["semantic_audio_recovery"]["status"] == "BLOCK"



def test_requested_full_sentence_changes_physical_audio_not_only_context(legacy_rig):
    rig = legacy_rig
    finding, request, _ = full_case(rig, "history")
    original = deepcopy(finding)
    result, audit, prompts = run_range(rig, finding, request, range_plan())
    broad = audit["range_witness_request"]
    assert [broad["matched_start_ms"], broad["matched_end_ms"]] == [1000, 6000]
    assert broad["source_media_timeline_offset_ms"] == 30000
    assert broad["cue_indexes"] == [1, 2, 3]
    assert "current_cue" not in broad and "proposed_cue" not in broad
    assert "syllable_count_hint" not in broad
    assert "说了" not in json.dumps(broad, ensure_ascii=False)
    payload = dict(broad)
    digest = payload.pop("request_sha256")
    assert digest == recovery._digest(payload)
    assert len(prompts) == 1 and audit["status"] == "OBSERVED", audit
    assert len(rig.transport.calls) == 2
    assert rig.transport.calls[-1]["class"] == "CANDIDATE_BLIND_RANGE"
    assert rig.transport.calls[0]["audio_sha256"] != rig.transport.calls[1]["audio_sha256"]
    assert "说了" not in rig.transport.calls[-1]["prompt"]
    row = result["_prior_range_acoustic_observations"][0]
    assert row["mutation_authorized"] is False
    assert row["artifact_bindings"]["raw_response_sha256"] == rig.transport.calls[-1]["raw_sha256"]
    assert finding == original and result["exact_release_adjudication"] == original["exact_release_adjudication"]


@pytest.mark.parametrize("change", ["source_offset", "text", "request_hash", "invalid_gate"])
def test_drifted_pending_gate_does_not_dispatch(change):
    finding, request = setup_case()
    if change == "source_offset":
        request = {**request, "source_media_timeline_offset_ms": 0}
    elif change == "text":
        request = {**request, "current_cue": "别的"}
    elif change == "request_hash":
        finding["exact_release_adjudication"]["request"]["request_sha256"] = "c" * 64
    else:
        finding["exact_release_adjudication"]["history_convergence_acoustic_witness"]["judge_prompt_sha256"] = "invalid"
    def forbidden(_):
        raise AssertionError("drift must not spend provider calls")
    _, audit = recovery.recover_semantic_audio_range(srt_text=SOURCE, finding=finding,
                                                     check_request=request, entity_verifier=None, llm_call=forbidden)
    assert audit["status"] == "BLOCK"
    assert audit["reason_code"] == "SEMANTIC_RANGE_CURRENT_GATE_DRIFT"



def test_unbacked_provider_failure_is_not_a_usable_uncertain_observation(legacy_rig):
    """A failure labeled UNCERTAIN is not a complete uncertain audio response."""
    rig = legacy_rig
    finding, request, _ = full_case(rig)
    original = deepcopy(finding)
    def failed_observer(**kwargs):
        return [{"status": "UNCERTAIN", "reason_code": "PROVIDER_FAILED"}]
    failed_observer.available_providers = ("gemini",)
    rig.verifier.observe_range_context = failed_observer
    output, audit, _ = run_range(rig, finding, request, range_plan())
    assert audit["status"] == "BLOCK"
    assert audit["native_observations"][0]["reason_code"] == "PROVIDER_FAILED"
    assert audit["new_effective_observations_this_attempt"] == 0
    assert audit["observation_projection_before"]["effective_distinct_observations"] == 1
    assert len(rig.transport.calls) == 1 and output == finding == original
    assert finding["exact_release_adjudication"]["witness_judge"]["judge"]["needs_audio"] is True


@pytest.mark.parametrize("adaptable", [True, False])
def test_existing_range_after_other_cue_text_repair_uses_normal_carryover(legacy_rig, monkeypatch, adaptable):
    rig = legacy_rig
    finding, _, _, output, _ = completed_range(rig)
    carried = deepcopy(output)
    original = deepcopy(carried)
    old_row = carried["_prior_range_acoustic_observations"][0]
    raw_path = Path(old_row["artifact_bindings"]["raw_response_path"])
    original_raw = raw_path.read_bytes()
    changed = SOURCE.replace("前面开始解释", "前面继续解释")
    if not adaptable:
        raw_path.write_text("{}")
    current = current_check(rig, carried, srt=changed)
    seen = []
    adapt = recovery.adapt_range_acoustic_context_observation
    def traced_adapt(*args):
        seen.append(args)
        return adapt(*args)
    monkeypatch.setattr(recovery, "adapt_range_acoustic_context_observation", traced_adapt)
    def plan(_):
        if adaptable:
            pytest.fail("verified unconsumed carryover must not spend another routing call")
        return {"decision": "NO_NEW_AUDIO", "reason": "原始响应损坏，不能据此声称观察穷尽"}
    result, audit, prompts = run_range(rig, carried, current, plan, srt=changed)
    assert carried == original and len(rig.transport.calls) == 2
    assert audit["new_effective_observations_this_attempt"] == 0
    if adaptable:
        # 41:1489-1492 (admin ruling): the text-free range passes strict
        # validation, so the carryover adapter is no longer needed; reuse is the same.
        assert not prompts
        assert audit["status"] == "OBSERVED" and audit["served_from_cache"] is True
        assert audit["provider_calls_this_attempt"] == 0
        rebound = result["_prior_range_acoustic_observations"][0]
        assert recovery._observation_identity(rebound) == recovery._observation_identity(old_row)
        assert rebound["mutation_authorized"] is False
        assert raw_path.read_bytes() == original_raw
    else:
        assert audit["status"] == "BLOCK" and result == original
        assert raw_path.read_text() == "{}"
        assert "no_gain_proof" not in audit


@pytest.mark.parametrize("candidate_less", [False, True])
def test_normal_auditor_collects_range_before_proposal_and_keeps_history(legacy_rig, monkeypatch, candidate_less):
    """Auditor orchestration seam; the recovered row was validated beforehand."""
    from src.autoslice import final_review_auditor as auditor
    rig = legacy_rig
    finding, _, _, recovered, recovery_audit = completed_range(
        rig, "missing" if candidate_less else "history")
    finding.update(suggestion=None, proposed_full_cue=None)
    marker = recovered["_prior_range_acoustic_observations"][0]
    def recover(**kwargs):
        return ({**kwargs["finding"], "_prior_range_acoustic_observations": [marker]},
                deepcopy(recovery_audit))
    monkeypatch.setattr(recovery, "recover_semantic_audio_range", recover)
    called = []
    def ordinary(srt, current, **kwargs):
        called.append(current)
        return srt, {"status": "UNCERTAIN", "repaired": False}
    monkeypatch.setattr(auditor, "_adjudicate_context_finding", ordinary)
    unchanged, audit = auditor.adjudicate_context_finding(
        SOURCE, finding, entity_verifier=rig.verifier, clip_context=rig.context,
        source_media_timeline_offset_ms=30000)
    assert unchanged == SOURCE and called[0]["_prior_range_acoustic_observations"] == [marker]
    assert "exact_release_adjudication" not in called[0]
    assert audit["prior_audio_adjudication"] == finding["exact_release_adjudication"]
    assert audit["semantic_audio_recovery"]["status"] == "OBSERVED"
    assert len(rig.transport.calls) == 2


def test_new_range_does_not_bind_old_proposal_gate_to_current_scaffold(legacy_rig, monkeypatch):
    """Old PROPOSED=说了 cannot authorize a new candidate-less CURRENT=了."""
    from src.autoslice import final_review_auditor as auditor
    from src.autoslice.exact_final_witness_authority import history_audio_recovery_from_adjudication
    rig = legacy_rig
    finding, original_request, _, recovered, recovery_audit = completed_range(rig, "history")
    old = deepcopy(finding["exact_release_adjudication"])
    finding.update(suggestion=None, proposed_full_cue=None)
    def recover(**kwargs):
        return ({**kwargs["finding"], "_prior_range_acoustic_observations":
                 recovered["_prior_range_acoustic_observations"]}, deepcopy(recovery_audit))
    monkeypatch.setattr(recovery, "recover_semantic_audio_range", recover)
    def ordinary(srt, current, **kwargs):
        scaffold = auditor._candidate_blind_missing_proposal_scaffold(srt, current)
        request = build_context_adjudication_request(
            srt, scaffold, clip_context=rig.context, source_media_timeline_offset_ms=30000)
        assert original_request["proposed_cue"] == "说了"
        assert request["proposed_cue"] == request["current_cue"] == "了"
        assert history_audio_recovery_from_adjudication(old, request=request)["status"] == "INVALID"
        assert history_audio_recovery_from_adjudication(current, request=request) is None
        return srt, {"status": "UNCERTAIN", "repaired": False,
                     "reason_code": "CANDIDATE_BLIND_AUDIO_REQUIRED"}
    monkeypatch.setattr(auditor, "_adjudicate_context_finding", ordinary)
    output, audit = auditor.adjudicate_context_finding(
        SOURCE, finding, entity_verifier=rig.verifier, clip_context=rig.context,
        source_media_timeline_offset_ms=30000)
    assert output == SOURCE and audit["status"] == "UNCERTAIN"
    assert audit["repaired"] is False and audit["prior_audio_adjudication"] == old
    assert finding["exact_release_adjudication"] == old
    assert len(rig.transport.calls) == 2


def test_failed_range_preserves_active_old_audio_gate_without_text_retry(monkeypatch):
    from src.autoslice import final_review_auditor as auditor
    finding, _ = setup_case()
    finding.update(suggestion=None, proposed_full_cue=None)
    def verifier(_):
        return None
    verifier.observe_range_context = lambda **_: None
    monkeypatch.setattr(recovery, "recover_semantic_audio_range", lambda **kw: (
        kw["finding"], {"status": "BLOCK", "reason_code": "NO_VALID_CONTEXT_EVIDENCE"}))
    def forbidden(*args, **kwargs):
        raise AssertionError("failed evidence cannot consume another text-only decision")
    monkeypatch.setattr(auditor, "_adjudicate_context_finding", forbidden)
    output, audit = auditor.adjudicate_context_finding(SOURCE, finding, entity_verifier=verifier)
    assert output == SOURCE and audit["status"] == "UNCERTAIN"
    assert audit["history_convergence_acoustic_witness"]["needs_audio"] is True
    assert audit["semantic_audio_recovery"]["status"] == "BLOCK"



@pytest.mark.parametrize("repeat", [True, False])
def test_more_audio_request_does_not_consume_already_judged_range_as_new(legacy_rig, repeat):
    rig = legacy_rig
    finding, _, witness, prior_output, first = completed_range(rig, last=2)
    request = current_check(rig, prior_output)
    pending = protocol.pending_finding(
        rig, request, witness, previous=finding["exact_release_adjudication"])
    pending["exact_release_adjudication"]["semantic_audio_recovery"] = first
    original = deepcopy(pending)
    def plan(data):
        assert data["effective_distinct_observations"] == 2
        return range_plan(last=2 if repeat else 3)
    output, audit, plans = run_range(rig, pending, request, plan)
    assert pending == original and len(plans) == 1
    assert audit["previously_considered_observations"]
    if repeat:
        assert len(rig.transport.calls) == 2 and audit["status"] == "BLOCK"
        assert audit["reason_code"] == "RELISTEN_REPEATED_INPUT_NO_INFORMATION_GAIN"
        assert audit["provider_calls_this_attempt"] == 0
        assert audit["new_effective_observations_this_attempt"] == 0
    else:
        assert len(rig.transport.calls) == 3 and audit["status"] == "OBSERVED", audit
        broad = audit["range_witness_request"]
        assert [broad["matched_start_ms"], broad["matched_end_ms"]] == [1000, 6000]
        assert audit["new_effective_observations_this_attempt"] == 1
        assert audit["observation_projection_after"]["effective_distinct_observations"] == 3
        assert output["_prior_range_acoustic_observations"][-1]["artifact_bindings"]["raw_response_sha256"] == rig.transport.calls[-1]["raw_sha256"]
    assert output["exact_release_adjudication"] == original["exact_release_adjudication"]


@pytest.mark.parametrize("heard_provider", ["mai", "agy", "gemini_api"])
@pytest.mark.parametrize("private_carryover", [True, False])
def test_provider_label_cannot_make_an_already_heard_input_novel(legacy_rig, heard_provider, private_carryover):
    """Admission unit over real receipts; provider labels are not new inputs.

    This does not impersonate a MAI response or re-sign a Gemini artifact.
    The public repeat test above covers the normal no-dispatch boundary.
    """
    rig = legacy_rig
    _, _, witness, output, first = completed_range(rig)
    request = current_check(rig, output)
    projection = policy.evidence_projection(request, witness=witness)
    assert projection["effective_distinct_observations"] == 2
    labelled = deepcopy(projection)
    for row in labelled["observations"]:
        row["provider"] = heard_provider  # explanatory projection metadata only
    prior = {"prior_semantic_audio_recovery": deepcopy(first)} if private_carryover else None
    with pytest.raises(ValueError, match="RELISTEN_REPEATED_INPUT_NO_INFORMATION_GAIN"):
        policy.admit_plan(
            range_plan(provider="gemini", reason="不同提供者再次听同一完整句"),
            request, labelled, source_window=[31000, 36000], cue_indexes=[1, 2, 3],
            prior_audit=prior)
    assert len(rig.transport.calls) == 2
    assert projection["observations"] != labelled["observations"]
    assert first["new_effective_observations_this_attempt"] == 1


@pytest.mark.parametrize("first,last,before,after,accepted", [
    (1, 3, 1500, 500, True), (1, 3, 32000, 0, False),
    (2, 2, 1, 0, False), (2, 2, 0, 1, False),
])
def test_cpa_complete_block_preroll_keeps_source_and_declared_neighbors_bound(
    legacy_rig, first, last, before, after, accepted,
):
    rig = legacy_rig
    finding, request, _ = full_case(rig)
    _, audit, _ = run_range(rig, finding, request, range_plan(
        first, last, before_ms=before, after_ms=after, purpose="SENTENCE_ONSET",
        reason="片头前文需要源音，核句首是否遗漏；保持原源时钟"))
    if accepted:
        assert len(rig.transport.calls) == 2 and audit["status"] == "OBSERVED", audit
        broad = audit["range_witness_request"]
        assert broad["matched_start_ms"] == -500 and broad["matched_end_ms"] == 6500
        assert broad["source_media_timeline_offset_ms"] == 30000
        assert broad["context_start_ms"] == broad["matched_start_ms"]
        assert "current_cue" not in broad and "proposed_cue" not in broad
        assert audit["admission"]["input_information"]["source_window_ms"] == [29500, 36500]
        assert audit["provider_calls_this_attempt"] == audit["new_effective_observations_this_attempt"] == 1
    else:
        assert len(rig.transport.calls) == 1
        assert audit["reason_code"] == "SEMANTIC_RANGE_TIMELINE_INVALID"
        assert audit["provider_calls_this_attempt"] == audit["new_effective_observations_this_attempt"] == 0


@pytest.mark.parametrize("ordinary_budget", [False, True])
def test_newly_acquired_range_gets_recorded_plan_before_same_call_proposal(legacy_rig, monkeypatch, ordinary_budget):
    """Keep the existing CPA budget seam, supplied with a real acquired range."""
    from src.autoslice import final_review_auditor as auditor
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    rig = legacy_rig
    finding, _, _, recovered, recovery_audit = completed_range(rig, "missing")
    context = rig.context
    calls = []
    def call(prompt):
        calls.append(prompt)
        return '{"decision":"UNRESOLVED","needs_audio":true}'
    call.cpa_cache_identity = {"model": "fixture"}
    for i in range(3):
        SemanticAttemptRecorder(
            srt_text=SOURCE, finding={"cue_index": 2}, clip_context=context, offset_ms=30000
        ).wrap(call)(f"old question {i}")
    observation = recovered["_prior_range_acoustic_observations"][0]
    def recovered_seam(**kwargs):
        return ({**kwargs["finding"], "_prior_range_acoustic_observations": [observation]},
                deepcopy(recovery_audit))
    monkeypatch.setattr(recovery, "recover_semantic_audio_range", recovered_seam)
    def proposal(srt_text, current, **kwargs):
        assert current["continuation_plan"]["action"] == "expand_context"
        assert current["semantic_retry_evidence_basis"]["mutation_authorized"] is False
        if ordinary_budget:
            kwargs["judge_llm_call"]("new range question")
        else:
            SemanticAttemptRecorder(
                srt_text=srt_text, finding=current, clip_context=context, offset_ms=30000
            ).wrap(call)("new range question")
        return srt_text, {"status": "UNCERTAIN", "repaired": False}
    monkeypatch.setattr(auditor, "_adjudicate_context_finding", proposal)
    original = deepcopy(finding)
    if ordinary_budget:
        pending, _ = auditor.adjudicate_exact_release_findings(
            SOURCE, [finding], entity_verifier=rig.verifier, judge_llm_call=call,
            clip_context=context, source_media_timeline_offset_ms=30000)
        text, audit = SOURCE, pending[0]["exact_release_adjudication"]
    else:
        text, audit = auditor.adjudicate_context_finding(
            SOURCE, finding, entity_verifier=rig.verifier, judge_llm_call=call,
            clip_context=context, source_media_timeline_offset_ms=30000)
    assert text == SOURCE and finding == original and len(calls) == 4
    assert len(rig.transport.calls) == 2
    assert audit["prior_audio_adjudication"] == original["exact_release_adjudication"]
    rows = [json.loads(p.read_text()) for p in (rig.tmp / "base" / "cache" / "semantic-cpa-attempts").rglob("*.json")]
    new = next(row for row in rows if row["request"]["prompt"] == "new range question")
    assert new["continuation_reason"]["prior_unresolved_answers"] == 3
    assert new["request"]["continuation_plan"]["action"] == "expand_context"
