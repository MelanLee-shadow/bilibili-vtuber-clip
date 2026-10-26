"""Zero-network regressions through the real budget and common judge boundary."""
from copy import deepcopy
import hashlib
import json
import socket

import pytest

from src.autoslice.acoustic_witness_adjudication import build_witness_request, judge_word_choice
from src.autoslice.acoustic_witness_availability import pending_acoustic_witness
from src.autoslice.final_review_auditor import build_context_adjudication_request
from src.autoslice.final_review_provider_budget import ContextAdjudicationBudget

SRT = "1\n00:00:01,000 --> 00:00:03,000\n这是白猫\n"
SOURCE = "a" * 64
CONTEXT = {"source_media_sha256": SOURCE, "candidate_id": "synthetic-material-sync",
           "pieces": [{"source_media_sha256": SOURCE}]}
PLAN = {"action": "reframe_question", "question": "Check the synthetic final noun against audio.",
        "why_previous_failed": "Text alone did not distinguish the fixture nouns.",
        "expected_gain": "Use a newly acquired, bound observation.",
        "verification": "Check source, target and the original response identity."}
FINDING = {"cue_index": 1, "suspect": "猫", "suggestion": "帽",
           "proposed_full_cue": "这是白帽", "repair_class": "phonetic",
           "why": "Synthetic noun uncertainty", "continuation_plan": PLAN}
MODEL = {"transport": "zero-network-fixture", "models": ["fixed-response"], "effort": "none"}


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    def forbidden(*args, **kwargs):
        pytest.fail("No network is permitted in material-sync tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def request_for(finding=None, srt=SRT):
    return build_context_adjudication_request(srt, finding or FINDING,
                                             clip_context=CONTEXT)


def observed(request, label="first", *, protocol="blind_pinyin"):
    target = request if request.get("schema_version") == "subtitle-span-acoustic-witness-request.v1" else build_witness_request(request)
    source_start = target["matched_start_ms"] + target["source_media_timeline_offset_ms"]
    source_end = target["matched_end_ms"] + target["source_media_timeline_offset_ms"]
    row = {"schema_version": "subtitle-span-acoustic-witness.v1",
           "witness_protocol": protocol, "request_sha256": target["request_sha256"],
           "status": "OBSERVED", "target_audible": True, "source_media_sha256": SOURCE,
           "audio_clip_sha256": hashlib.sha256(("audio:" + label).encode()).hexdigest(),
           "response_sha256": hashlib.sha256(("response:" + label).encode()).hexdigest(),
           "provider": "fixture", "model": "fixture-dictation",
           "audio_start_ms": source_start, "audio_end_ms": source_end,
           "authority": "EVIDENCE_ONLY", "mutation_authorized": False}
    if protocol == "blind_pinyin":
        row.update(heard_pinyin="zhe shi bai mao", uncertain_positions=[],
                   syllable_count=4, confidence=0.9)
    else:
        row.update(candidate_exposure="none", exact_transcript="这是白猫")
    return row


def fixed_call(calls):
    def call(prompt):
        calls.append(prompt)
        return json.dumps({"choice": "NEITHER", "needs_audio": True,
                           "reason": "Synthetic evidence remains inconclusive."})
    call.cpa_cache_identity = deepcopy(MODEL)
    return call


def budget_for(call, adjudicator):
    return ContextAdjudicationBudget(limit=100, entity_verifier=None,
        clip_context=CONTEXT, source_media_timeline_offset_ms=0,
        judge_llm_call=call, screen_read_probe=None, adjudicate_context=adjudicator)


def test_budget_common_judge_consumes_fresh_witness_in_same_admission():
    calls, receipts = [], []
    def ordinary_boundary(srt, finding, *, judge_llm_call, **kwargs):
        request = request_for(finding, srt)
        pending = pending_acoustic_witness(build_witness_request(request))
        # Three real recorded text-first calls: no fake ledger or provider history.
        for index in range(3):
            receipts.append(judge_word_choice(llm_call=judge_llm_call,
                check_request=request, witness=pending, context_before=f"Fixture question {index}"))
        receipts.append(judge_word_choice(llm_call=judge_llm_call,
            check_request=request, witness=observed(request)))
        return srt, {"status": "UNCERTAIN", "repaired": False,
                     "decision_authority": "CPA_JUDGE", "judge": receipts[-1]}
    result = budget_for(fixed_call(calls), ordinary_boundary).adjudicate(SRT, deepcopy(FINDING))
    assert len(calls) == 4, receipts[-1]
    assert receipts[-1]["status"] == "JUDGED"
    assert result["semantic_retry_progress"]["events"][-1]["status"] == "COMPLETED"
    assert result["repaired"] is False
    assert result["semantic_retry_progress"]["mutation_authorized"] is False


@pytest.fixture
def uncached_judge(monkeypatch):
    # Exercise the durable recorder, not a shortcut in the parsed judge cache.
    import src.autoslice.acoustic_witness_adjudication as module
    monkeypatch.setattr(module, "_judge_cache_root", lambda: None)


def run_calls(steps, *, finding=None, call=None, srt=SRT):
    calls, receipts = [], []
    call = call or fixed_call(calls)
    def chain(text, item, *, judge_llm_call, **kwargs):
        for request, witness, extra in steps:
            receipts.append(judge_word_choice(llm_call=judge_llm_call,
                check_request=request, witness=witness, **extra))
        return text, {"status": "UNCERTAIN", "repaired": False, "judge": receipts[-1]}
    result = budget_for(call, chain).adjudicate(srt, deepcopy(finding or FINDING))
    return calls, receipts, result


def three_text_calls(request):
    pending = pending_acoustic_witness(build_witness_request(request))
    return [(request, pending, {"context_before": f"Fixture question {i}"}) for i in range(3)]


def test_exact_raw_replay_and_evidence_order_are_not_new_calls(uncached_judge):
    from src.autoslice.independent_context_adjudication import bind_request
    request = request_for()
    one = observed(request, "one", protocol="candidate_blind_transcript")
    two = observed(request, "two", protocol="candidate_blind_transcript")
    multi = bind_request(request, proposed_candidates=[
        {"text": one["exact_transcript"], "source": one},
        {"text": two["exact_transcript"], "source": two}])
    changed = deepcopy(one)
    changed.update(served_from_cache=True, elapsed_seconds=123, model="renamed-only",
                   provider="label-only", code_sha="metadata-not-evidence")
    reordered = bind_request(request, proposed_candidates=list(reversed(multi["proposed_candidates"])))
    steps = three_text_calls(request) + [
        (multi, one, {}), (multi, one, {}),
        (reordered, changed, {"context_before": "Cosmetic rewording, same evidence"})]
    calls, receipts, result = run_calls(steps)
    assert len(calls) == 4
    assert receipts[3]["completion_sha256"] == receipts[4]["completion_sha256"]
    assert receipts[-1]["retry_reason_code"] == "CONTINUATION_PLAN_ALREADY_UNRESOLVED"
    assert result["semantic_retry_progress"]["events"][-2]["status"] == "EXACT_RAW_REPLAY"
    assert receipts[-1]["dispatch_outcome"] == "NOT_DISPATCHED"
    assert not result["repaired"]


@pytest.mark.parametrize("change", ["candidate", "other_cue", "provider_metadata", "empty_evidence", "drop_and_readd"])
def test_consumed_evidence_cannot_be_reopened_by_controller_changes(change, uncached_judge):
    request = request_for()
    initial = observed(request, protocol="candidate_blind_transcript")
    steps = three_text_calls(request) + [(request, initial, {})]
    changed_request, changed_witness = deepcopy(request), deepcopy(initial)
    if change == "candidate":
        changed_finding = {**FINDING, "suggestion": "毛", "proposed_full_cue": "这是白毛"}
        changed_request = request_for(changed_finding)
        changed_witness = observed(changed_request, protocol="candidate_blind_transcript")
    elif change == "other_cue":
        changed_request = request_for(srt=SRT + "\n2\n00:00:04,000 --> 00:00:05,000\n另句已修好\n")
        changed_witness = observed(changed_request, protocol="candidate_blind_transcript")
    elif change == "provider_metadata":
        changed_witness.update(provider="alternate-label", model="alternate-label", elapsed_seconds=999)
    elif change == "empty_evidence":
        changed_witness = pending_acoustic_witness(build_witness_request(request))
    else:
        steps.append((request, pending_acoustic_witness(build_witness_request(request)),
                      {"context_before": "Temporarily omit the heard evidence"}))
    steps.append((changed_request, changed_witness, {"context_before": "Another prompt, no new bytes"}))
    calls, receipts, _ = run_calls(steps)
    assert len(calls) == 4
    assert receipts[-1]["retry_reason_code"] == "CONTINUATION_PLAN_ALREADY_UNRESOLVED"


def test_missing_plan_is_not_synthesized_from_new_audio(uncached_judge):
    finding = {k: v for k, v in FINDING.items() if k != "continuation_plan"}
    request = request_for(finding)
    calls, receipts, _ = run_calls(three_text_calls(request) + [(request, observed(request), {})],
                                    finding=finding)
    assert len(calls) == 3
    assert receipts[-1]["retry_reason_code"] == "CONTINUATION_VALUE_PLAN_REQUIRED"
    assert receipts[-1]["failure_layer"] == "LOCAL_RETRY_ADMISSION"
    assert receipts[-1]["choice"] == "UNCERTAIN"


@pytest.mark.parametrize("bad", ["request_hash", "witness_hash", "source", "context_source", "offset", "coverage", "response_hash", "timeline", "mutation"])
def test_invalid_binding_never_dispatches_or_writes_attempt(bad, tmp_path, uncached_judge):
    from src.autoslice.independent_context_adjudication import bind_request
    request = request_for()
    witness = observed(request)
    if bad == "request_hash":
        request["request_sha256"] = "f" * 64
    elif bad == "witness_hash":
        witness["request_sha256"] = "f" * 64
    elif bad == "source":
        witness["source_media_sha256"] = "f" * 64
    elif bad == "context_source":
        request = bind_request(request, clip_context_binding={**request["clip_context_binding"],
                               "source_media_sha256s": ["f" * 64]})
    elif bad == "offset":
        request = bind_request(request, source_media_timeline_offset_ms=100)
        witness = observed(request)
    elif bad == "coverage":
        witness["audio_start_ms"] += 1
    elif bad == "response_hash":
        witness["response_sha256"] = "not-a-digest"
    elif bad == "timeline":
        witness["timeline_binding"] = {"source_media_timeline_offset_ms": 0,
            "delivery_local": {"target_start_ms": 1000, "target_end_ms": 3000},
            "source_media": {"target_start_ms": 999, "target_end_ms": 3000,
                             "crop_start_ms": 1000, "crop_end_ms": 3000}}
    else:
        witness["mutation_authorized"] = True
    calls, receipts, result = run_calls([(request, witness, {})])
    assert not calls
    assert receipts[-1]["retry_reason_code"] == "SEMANTIC_JUDGE_EVIDENCE_BINDING_INVALID"
    assert not list((tmp_path / "runtime/cache/semantic-cpa-attempts").rglob("*.json"))
    assert not result["repaired"]


def test_judge_model_switch_does_not_make_consumed_bytes_new(uncached_judge):
    request = request_for()
    witness = observed(request, protocol="candidate_blind_transcript")
    calls, _, _ = run_calls(three_text_calls(request) + [(request, witness, {})])
    assert len(calls) == 4
    later = []
    call = fixed_call(later)
    call.cpa_cache_identity = {**MODEL, "models": ["a-different-judge"]}
    _, receipts, _ = run_calls([(request, witness, {"context_before": "Same evidence, different judge"})], call=call)
    assert not later
    assert receipts[-1]["retry_reason_code"] == "CONTINUATION_PLAN_ALREADY_UNRESOLVED"


def test_real_auditor_and_independent_asr_reach_shared_boundary(tmp_path, uncached_judge):
    from src.autoslice.final_review_auditor import adjudicate_context_finding
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    calls = []
    call = fixed_call(calls)
    recorder = SemanticAttemptRecorder(srt_text=SRT, finding=deepcopy(FINDING),
                                       clip_context=CONTEXT, offset_ms=0)
    for index in range(3):
        recorder.wrap(call)(f"Synthetic prior text question {index}")
    seen = []
    def verifier(target):
        seen.append("narrow")
        return observed(target)
    def native(target):
        seen.append("independent")
        return [observed(target, "independent", protocol="candidate_blind_transcript")]
    verifier.observe_independent_witnesses = native
    budget = ContextAdjudicationBudget(limit=100, entity_verifier=verifier,
        clip_context=CONTEXT, source_media_timeline_offset_ms=0,
        judge_llm_call=call, screen_read_probe=None, adjudicate_context=adjudicate_context_finding)
    result = budget.adjudicate(SRT, deepcopy(FINDING))
    assert "narrow" in seen and "independent" in seen
    records = [json.loads(p.read_text()) for p in (tmp_path / "runtime/cache/semantic-cpa-attempts").rglob("*.json")]
    consumed = [x for row in records if row["status"] == "COMPLETED"
                for x in row["request"]["material"].get("bound_judge_evidence", [])]
    assert observed(request_for())["response_sha256"] in {x["response_sha256"] for x in consumed}
    assert observed(request_for(), "independent")["response_sha256"] in {x["response_sha256"] for x in consumed}
    assert result["repaired"] is False
    assert result["mutation_authority"]["status"] == "NOT_APPLIED"
    assert result["semantic_retry_progress"]["mutation_authorized"] is False


@pytest.mark.parametrize("stored_status", ["STARTED", "ERROR", "OUTCOME_UNKNOWN", "COMPLETED"])
def test_legacy_material_same_transport_is_not_redispatched(stored_status, tmp_path, uncached_judge):
    # Generate a new synthetic legacy-format row via the actual old-compatible
    # recorder seam. This is not reconstructed from any historical run.
    from src.autoslice.acoustic_witness_adjudication import _word_choice_prompt
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    request = request_for()
    witness = observed(request, protocol="candidate_blind_transcript")
    old_calls = []
    old_call = fixed_call(old_calls)
    prompt, *_ = _word_choice_prompt(llm_call=old_call, check_request=request, witness=witness,
                                    context_before="", context_after="", structured_chat_context="")
    recorder = SemanticAttemptRecorder(srt_text=SRT, finding=deepcopy(FINDING),
                                       clip_context=CONTEXT, offset_ms=0)
    raw = recorder.wrap(old_call)(prompt)
    path, = (tmp_path / "runtime/cache/semantic-cpa-attempts").rglob("*.json")
    row = json.loads(path.read_text())
    assert "bound_judge_evidence" not in row["request"]["material"]
    if stored_status != "COMPLETED":
        row["status"] = stored_status
        row.pop("completion")
        row.pop("completion_sha256")
        path.write_text(json.dumps(row))
    original = path.read_bytes()
    new_calls = []
    _, receipts, result = run_calls([(request, witness, {})], call=fixed_call(new_calls))
    assert not new_calls
    assert path.read_bytes() == original
    assert len(list(path.parent.glob("*.json"))) == 1
    if stored_status == "COMPLETED":
        assert receipts[-1]["completion_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
        assert result["semantic_retry_progress"]["events"][-1]["status"] == "EXACT_RAW_REPLAY"
    else:
        assert receipts[-1]["retry_reason_code"] == "PRIOR_CALL_OUTCOME_UNKNOWN"
    assert not result["repaired"]


def test_transport_error_is_retained_when_local_guard_refuses_retry(tmp_path, uncached_judge):
    calls = []
    def failed_transport(prompt):
        calls.append(prompt)
        raise ConnectionError("Synthetic lost response after possible dispatch")
    failed_transport.cpa_cache_identity = deepcopy(MODEL)
    request = request_for()
    _, receipts, _ = run_calls([(request, observed(request), {})], call=failed_transport)
    assert len(calls) == 1
    verdict = receipts[-1]
    assert verdict["failure_layer"] == "LOCAL_RETRY_ADMISSION"
    assert verdict["retry_reason_code"] == "PRIOR_CALL_OUTCOME_UNKNOWN"
    assert len(verdict["prior_call_errors"]) == 1
    assert "ConnectionError" in verdict["error_cascade"][0]
    assert verdict["provider_retry_attempted"] is True
    assert verdict["dispatch_outcome"] == "UNKNOWN"
    assert verdict["local_admission_dispatch_outcome"] == "NOT_DISPATCHED"
    path, = (tmp_path / "runtime/cache/semantic-cpa-attempts").rglob("*.json")
    assert json.loads(path.read_text())["status"] == "OUTCOME_UNKNOWN"


def test_added_independent_receipt_is_consumed_once_not_voted(uncached_judge):
    from src.autoslice.independent_context_adjudication import bind_request
    request = request_for()
    first = observed(request, "one", protocol="candidate_blind_transcript")
    second = observed(request, "two", protocol="candidate_blind_transcript")
    augmented = bind_request(request, proposed_candidates=[
        {"text": first["exact_transcript"], "source": first},
        {"text": second["exact_transcript"], "source": second}])
    steps = three_text_calls(request) + [(request, first, {}), (augmented, first, {}),
        (request, second, {"context_before": "Remove and reuse one already consumed source"})]
    calls, receipts, result = run_calls(steps)
    assert len(calls) == 5
    assert receipts[4]["status"] == "JUDGED"
    assert receipts[-1]["retry_reason_code"] == "CONTINUATION_PLAN_ALREADY_UNRESOLVED"
    assert not result["repaired"]


def test_padded_witness_uses_its_bound_physical_clock(uncached_judge):
    request = request_for()
    witness = observed(request)
    witness.update(audio_start_ms=800, audio_end_ms=3200, timeline_binding={
        "source_media_timeline_offset_ms": 0,
        "delivery_local": {"target_start_ms": 1000, "target_end_ms": 3000},
        "source_media": {"target_start_ms": 1000, "target_end_ms": 3000,
                         "crop_start_ms": 800, "crop_end_ms": 3200}})
    calls, receipts, result = run_calls(three_text_calls(request) + [(request, witness, {})])
    assert len(calls) == 4 and receipts[-1]["status"] == "JUDGED"
    assert result["semantic_retry_progress"]["mutation_authorized"] is False


def test_reattaching_same_observation_on_finding_is_not_novel(uncached_judge):
    request = request_for()
    witness = observed(request, protocol="candidate_blind_transcript")
    calls, _, _ = run_calls(three_text_calls(request) + [(request, witness, {})])
    assert len(calls) == 4
    # A cold caller carries the same receipt on the finding. This changes the
    # legacy finding-level digest, but adds no actually observed bytes.
    carried = {**FINDING, "_prior_acoustic_observations": [
        {"request": request, "verdict": {**witness, "elapsed_seconds": 99, "served_from_cache": True}}]}
    current = request_for(carried)
    later, receipts, _ = run_calls([(current, witness, {"context_before": "The same saved observation"})],
                                   finding=carried)
    assert not later
    assert receipts[-1]["retry_reason_code"] == "CONTINUATION_PLAN_ALREADY_UNRESOLVED"



def test_cached_completed_judge_still_uses_zero_additional_callbacks():
    request = request_for()
    witness = observed(request, protocol="candidate_blind_transcript")
    calls, receipts, _ = run_calls([(request, witness, {}), (request, witness, {})])
    assert len(calls) == 1
    assert receipts[1]["served_from_cache"] is True
    assert receipts[0]["completion_sha256"] == receipts[1]["completion_sha256"]


def test_stream_piece_is_not_falsely_equated_to_rendered_source(uncached_judge):
    # The real clip context can name original stream pieces, whereas the
    # acquired witness names a recut. The unchanged piece binding and an
    # acquisition-validated witness are distinct provenance domains.
    context = {"candidate_id": "synthetic-recut", "pieces": [{"source_media_sha256": "b" * 64}]}
    request = build_context_adjudication_request(SRT, FINDING, clip_context=context)
    calls = []
    def chain(srt, finding, *, judge_llm_call, **kwargs):
        for _, pending, extras in three_text_calls(request):
            judge_word_choice(llm_call=judge_llm_call, check_request=request, witness=pending, **extras)
        receipt = judge_word_choice(llm_call=judge_llm_call, check_request=request,
                                    witness=observed(request))
        return srt, {"status": "UNCERTAIN", "repaired": False, "judge": receipt}
    budget = ContextAdjudicationBudget(limit=100, entity_verifier=None,
        clip_context=context, source_media_timeline_offset_ms=0, judge_llm_call=fixed_call(calls),
        screen_read_probe=None, adjudicate_context=chain)
    result = budget.adjudicate(SRT, deepcopy(FINDING))
    assert len(calls) == 4
    assert result["judge"]["status"] == "JUDGED" and not result["repaired"]


def test_consumed_rendered_source_cannot_change_on_same_physical_issue(uncached_judge):
    context = {"candidate_id": "synthetic-recut", "pieces": [{"source_media_sha256": "b" * 64}]}
    request = build_context_adjudication_request(SRT, FINDING, clip_context=context)
    calls, receipts = [], []
    def chain(srt, finding, *, judge_llm_call, **kwargs):
        for _, pending, extras in three_text_calls(request):
            judge_word_choice(llm_call=judge_llm_call, check_request=request, witness=pending, **extras)
        for label, source in [("first", SOURCE), ("different-source", "c" * 64)]:
            evidence = {**observed(request, label), "source_media_sha256": source}
            receipts.append(judge_word_choice(llm_call=judge_llm_call, check_request=request,
                witness=evidence, context_before=label))
        return srt, {"status": "UNCERTAIN", "repaired": False, "judge": receipts[-1]}
    budget = ContextAdjudicationBudget(limit=100, entity_verifier=None,
        clip_context=context, source_media_timeline_offset_ms=0, judge_llm_call=fixed_call(calls),
        screen_read_probe=None, adjudicate_context=chain)
    budget.adjudicate(SRT, deepcopy(FINDING))
    assert len(calls) == 4
    assert receipts[-1]["retry_reason_code"] == "SEMANTIC_JUDGE_SOURCE_DRIFT"



def test_nonzero_source_offset_is_kept_not_rebased(uncached_judge):
    offset = 700
    request = build_context_adjudication_request(SRT, FINDING, clip_context=CONTEXT,
                                                 source_media_timeline_offset_ms=offset)
    calls = []
    def chain(srt, finding, *, judge_llm_call, **kwargs):
        for _, pending, extras in three_text_calls(request):
            judge_word_choice(llm_call=judge_llm_call, check_request=request, witness=pending, **extras)
        result = judge_word_choice(llm_call=judge_llm_call, check_request=request,
                                   witness=observed(request))
        return srt, {"status": "UNCERTAIN", "repaired": False, "judge": result}
    budget = ContextAdjudicationBudget(limit=100, entity_verifier=None,
        clip_context=CONTEXT, source_media_timeline_offset_ms=offset,
        judge_llm_call=fixed_call(calls), screen_read_probe=None, adjudicate_context=chain)
    result = budget.adjudicate(SRT, deepcopy(FINDING))
    assert len(calls) == 4 and result["judge"]["status"] == "JUDGED"
    assert not result["repaired"]
