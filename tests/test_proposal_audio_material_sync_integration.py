"""Ordinary retry-budget/proposal integration with synthetic, bound observations.

No production audio, historical ledger, network, or real provider is used.
"""
from copy import deepcopy
import hashlib
import json
import socket

import pytest

from src.autoslice.final_review_auditor import adjudicate_context_finding
from src.autoslice.final_review_provider_budget import ContextAdjudicationBudget
from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder

SOURCE = "a" * 64
SRT = "1\n00:00:01,000 --> 00:00:04,000\n我们分析音含波动率\n\n2\n00:00:05,000 --> 00:00:08,000\n期权价格和波动率\n"
CONTEXT = {"source_media_sha256": SOURCE, "candidate_id": "proposal-sync-integration",
           "pieces": [{"source_media_sha256": SOURCE}]}
PLAN = {"action": "reframe_question", "question": "Compare the fixture term with a bound new observation.",
        "why_previous_failed": "Two text questions did not determine the spoken term.",
        "expected_gain": "Consume newly observed audio before the next proposal.",
        "verification": "Verify physical source, target clock, native request and observed bytes."}
FINDING = {"cue_index": 1, "suspect": "音含", "repair_class": "phonetic",
           "why": "Fixture term is uncertain", "continuation_plan": PLAN}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected network access in offline proposal test")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)



def test_second_proposal_consumes_new_native_audio_after_three_uncertain_answers(tmp_path):
    calls, observations = [], []

    def cpa(prompt):
        calls.append(prompt)
        return json.dumps({"status": "UNRESOLVED", "decision": "UNRESOLVED",
                           "replacement_text": "", "needs_audio": True,
                           "reason": "Synthetic observer remains uncertain."})
    cpa.cpa_cache_identity = {"transport": "offline-fixture", "models": ["proposal-test"], "effort": "none"}

    # Produce two real completed synthetic calls, not fabricated history rows.
    initial = SemanticAttemptRecorder(srt_text=SRT, finding=deepcopy(FINDING),
                                      clip_context=CONTEXT, offset_ms=0)
    recorded = initial.wrap(cpa)
    recorded("Fixture preliminary question A")
    recorded("Fixture preliminary question B")

    def observe(request):
        assert "current_cue" not in request and "proposed_cue" not in request
        start = request["matched_start_ms"] + request["source_media_timeline_offset_ms"]
        end = request["matched_end_ms"] + request["source_media_timeline_offset_ms"]
        row = {"schema_version": "subtitle-span-acoustic-witness.v1",
               "witness_protocol": "blind_pinyin", "request_sha256": request["request_sha256"],
               "status": "OBSERVED", "target_audible": True,
               "heard_pinyin": "wo men fen xi yin han bo dong lv", "uncertain_positions": [],
               "syllable_count": 9, "confidence": 0.9,
               "source_media_sha256": SOURCE, "audio_clip_sha256": hashlib.sha256(b"fixture-audio").hexdigest(),
               "response_sha256": hashlib.sha256(b"fixture-native-response").hexdigest(),
               "provider": "fixture", "model": "fixture-dictation",
               "audio_start_ms": start, "audio_end_ms": end,
               "authority": "EVIDENCE_ONLY", "mutation_authorized": False}
        observations.append({"request": deepcopy(request), "response": deepcopy(row)})
        return row

    budget = ContextAdjudicationBudget(limit=100, entity_verifier=observe,
        clip_context=CONTEXT, source_media_timeline_offset_ms=0,
        judge_llm_call=cpa, screen_read_probe=None, adjudicate_context=adjudicate_context_finding)
    result = budget.adjudicate(SRT, deepcopy(FINDING))
    summary = {"cpa_calls": len(calls), "observation_count": len(observations),
               "reason_code": result.get("reason_code"), "status": result.get("status"),
               "progress": result.get("semantic_retry_progress"),
               "native_request_keys": sorted(observations[0]["request"]) if observations else [],
               "audio_in_later_prompt": any('AUDIO_OBSERVED' in p for p in calls[3:])}
    (tmp_path / "proposal-call-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print("PROPOSAL_CALL_SUMMARY", json.dumps(summary, ensure_ascii=False))
    assert observations, summary
    count = len(calls)
    assert count == 4, summary
    assert any('AUDIO_OBSERVED' in prompt for prompt in calls[3:]), summary
    assert any("bound_judge_evidence" in event.get("changed_material_fields", [])
               for event in result["semantic_retry_progress"]["events"])
    assert result.get("repaired") is not True


def _ordinary_case(tmp_path, *, protocol="pinyin", damage=None, plan=True,
                   provider_allowed=True, unknown_fourth=False):
    """Return repeatable ordinary admissions, not a replacement execution path."""
    finding = deepcopy(FINDING)
    if not plan:
        finding.pop("continuation_plan")
    calls, observations = [], []
    observation_version = [0]
    metadata_version = [0]

    def cpa(prompt):
        calls.append(prompt)
        if unknown_fourth and len(calls) == 4:
            raise TimeoutError("Synthetic fourth-call transport outcome is unknown")
        return json.dumps({"status": "UNRESOLVED", "decision": "UNRESOLVED",
                           "replacement_text": "", "needs_audio": True,
                           "reason": "Synthetic evidence remains inconclusive."})
    cpa.cpa_cache_identity = {"transport": "offline-fixture", "models": ["proposal-controls"], "effort": "none"}
    recorder = SemanticAttemptRecorder(srt_text=SRT, finding=finding,
                                       clip_context=CONTEXT, offset_ms=0)
    recorded = recorder.wrap(cpa)
    recorded("Fixture preliminary question A")
    recorded("Fixture preliminary question B")

    def observe(request):
        assert "current_cue" not in request and "candidate_entities" not in request
        if protocol != "pinyin":
            row = {"schema_version": "subtitle-span-acoustic-witness.v1",
                   "witness_protocol": "blind_pinyin", "status": "UNCERTAIN",
                   "request_sha256": request["request_sha256"], "reason_code": "WITNESS_REPORT_INVALID"}
        else:
            start = request["matched_start_ms"] + request["source_media_timeline_offset_ms"]
            end = request["matched_end_ms"] + request["source_media_timeline_offset_ms"]
            row = {"schema_version": "subtitle-span-acoustic-witness.v1",
                   "witness_protocol": "blind_pinyin", "request_sha256": request["request_sha256"],
                   "status": "OBSERVED", "target_audible": True,
                   "heard_pinyin": "wo men fen xi yin han bo dong lv", "uncertain_positions": [],
                   "syllable_count": 9, "confidence": 0.9,
                   "source_media_sha256": SOURCE, "audio_clip_sha256": hashlib.sha256(f"fixture-control-audio:{observation_version[0]}".encode()).hexdigest(),
                   "response_sha256": hashlib.sha256(f"fixture-control-response:{observation_version[0]}".encode()).hexdigest(),
                   "provider": f"fixture-{metadata_version[0]}", "model": f"fixture-dictation-{metadata_version[0]}",
                   "audio_start_ms": start, "audio_end_ms": end,
                   "authority": "EVIDENCE_ONLY", "mutation_authorized": False}
            if damage == "source":
                row["source_media_sha256"] = "b" * 64
            elif damage == "request":
                row["request_sha256"] = "c" * 64
            elif damage == "audio_digest":
                row["audio_clip_sha256"] = "bad-digest"
            elif damage == "response_digest":
                row["response_sha256"] = "bad-digest"
            elif damage == "coverage":
                row["audio_start_ms"] += 1
            elif damage == "clock":
                row["timeline_binding"] = {
                    "source_media_timeline_offset_ms": 1,
                    "delivery_local": {"target_start_ms": start, "target_end_ms": end},
                    "source_media": {"target_start_ms": start, "target_end_ms": end,
                                     "crop_start_ms": start, "crop_end_ms": end}}
            elif damage == "mutation":
                row["mutation_authorized"] = True
            elif damage == "candidate_exposure":
                row["proposed_cue"] = "Candidate text is not acoustic evidence"
            elif damage == "legacy_missing_bytes":
                for key in ("source_media_sha256", "audio_clip_sha256", "response_sha256"):
                    row.pop(key)
        observations.append(deepcopy(row))
        return row

    def transcribe(request):
        from src.autoslice.entity_audio_verifier import _prepare_audio_span
        from src.autoslice.exact_source_transcript_contract import seal_exact_source_transcript_observation
        assert "current_cue" not in request and "candidate_entities" not in request
        span = _prepare_audio_span(request=request, context_mode=True, source_media_timeline_offset_ms=0,
                                   source_duration_ms=10000, witness_mode=True)
        row = seal_exact_source_transcript_observation(
            request=request, exact_transcript="我们分析、ありがとう", audible_language="mixed",
            source_media_sha256=SOURCE, audio_clip_sha256="b" * 64,
            provider="agy", model="fixture-original-language", response_sha256="c" * 64,
            timeline_binding=span.timeline_binding)
        if damage == "native_seal":
            row["exact_transcript"] = "Changed after native sealing"
        observations.append(deepcopy(row))
        return row
    if protocol == "original_language":
        observe.exact_source_transcript = transcribe

    def admit():
        return ContextAdjudicationBudget(limit=100, entity_verifier=observe,
            clip_context=CONTEXT, source_media_timeline_offset_ms=0,
            judge_llm_call=cpa, screen_read_probe=None, adjudicate_context=adjudicate_context_finding,
            provider_calls_allowed=provider_allowed).adjudicate(SRT, deepcopy(finding))
    admit.observation_version = observation_version
    admit.metadata_version = metadata_version
    return admit, calls, observations, recorder.directory


def _history(directory):
    return {p.name: p.read_bytes() for p in directory.glob("*.json")}


def test_original_language_audio_is_native_material_before_the_second_proposal(tmp_path):
    admit, calls, observed, directory = _ordinary_case(tmp_path, protocol="original_language")
    result = admit()
    assert len(calls) == 4, result.get("semantic_retry_progress")
    assert "我们分析、ありがとう" in calls[3]
    assert len(observed) == 2
    rows = [json.loads(raw) for raw in _history(directory).values()]
    bound = [r["request"]["material"].get("bound_judge_evidence") for r in rows]
    assert any(items and items[0]["audio_clip_sha256"] == "b" * 64 for items in bound)
    assert result.get("repaired") is not True
    assert observed[-1]["exact_transcript"] == "我们分析、ありがとう"


@pytest.mark.parametrize("damage", ["source", "request", "audio_digest", "response_digest", "coverage",
                                    "clock", "mutation", "candidate_exposure", "legacy_missing_bytes"])
def test_invalid_or_unbound_proposal_audio_never_admits_a_fourth_call(tmp_path, damage):
    admit, calls, _, directory = _ordinary_case(tmp_path, damage=damage)
    result = admit()
    assert len(calls) == 3
    assert result.get("repaired") is not True
    assert all(not json.loads(raw)["request"]["material"].get("bound_judge_evidence")
               for raw in _history(directory).values())


@pytest.mark.parametrize("protocol,damage", [("unavailable", None), ("original_language", "native_seal")])
def test_weak_text_or_invalid_native_transcript_cannot_supply_new_audio(tmp_path, protocol, damage):
    admit, calls, _, _ = _ordinary_case(tmp_path, protocol=protocol, damage=damage)
    result = admit()
    assert len(calls) == 3
    assert result.get("repaired") is not True


def test_new_proposal_audio_cannot_invent_a_missing_continuation_plan(tmp_path):
    admit, calls, _, _ = _ordinary_case(tmp_path, plan=False)
    result = admit()
    assert len(calls) == 3
    progress = result["semantic_retry_progress"]
    assert progress["status"] == "CONTINUATION_VALUE_PLAN_REQUIRED"
    assert progress["mutation_authorized"] is False


def test_disabled_provider_budget_is_not_overridden_by_proposal_sync(tmp_path):
    admit, calls, observed, directory = _ordinary_case(tmp_path, provider_allowed=False)
    before = _history(directory)
    result = admit()
    assert len(calls) == 2 and observed == []
    assert result["status"] == "SKIPPED_PROVIDER_DISABLED"
    assert _history(directory) == before


@pytest.mark.parametrize("protocol", ["pinyin", "original_language"])
def test_identical_proposal_bytes_replay_without_new_cpa_calls_or_history_rewrite(tmp_path, protocol):
    admit, calls, _, directory = _ordinary_case(tmp_path, protocol=protocol)
    first = admit()
    count = len(calls)
    assert count == 4, first.get("semantic_retry_progress")
    before = _history(directory)
    second = admit()
    assert len(calls) == 4
    assert _history(directory) == before
    assert second.get("repaired") is not True


def test_unknown_proposal_call_is_preserved_and_cannot_be_reissued(tmp_path):
    admit, calls, _, directory = _ordinary_case(tmp_path, unknown_fourth=True)
    first = admit()
    count = len(calls)
    assert count == 4, first.get("semantic_retry_progress")
    before = _history(directory)
    unknown = [json.loads(raw) for raw in before.values() if json.loads(raw)["status"] == "OUTCOME_UNKNOWN"]
    assert len(unknown) == 1
    second = admit()
    assert len(calls) == 4
    assert _history(directory) == before
    assert second.get("repaired") is not True


def test_unknown_call_does_not_discard_genuinely_new_bound_audio(tmp_path):
    admit, calls, _, directory = _ordinary_case(tmp_path, unknown_fourth=True)
    admit()
    count = len(calls)
    assert count == 4
    before = _history(directory)
    admit.observation_version[0] += 1
    result = admit()
    count = len(calls)
    assert count == 5  # New physical audio/response bytes, not a renamed prompt.
    after = _history(directory)
    assert all(after[name] == raw for name, raw in before.items())
    assert any(json.loads(raw)["status"] == "OUTCOME_UNKNOWN" for raw in after.values())
    assert result.get("repaired") is not True


def test_provider_label_change_does_not_make_proposal_audio_new(tmp_path):
    admit, calls, _, directory = _ordinary_case(tmp_path)
    admit()
    before = _history(directory)
    count = len(calls)
    assert count == 4
    admit.metadata_version[0] += 1
    result = admit()
    count = len(calls)
    assert count == 4
    assert _history(directory) == before
    assert result.get("repaired") is not True
