"""Retained completion and durable routing must coexist at the public boundary.

These fixtures supply raw provider responses only. The ordinary nomination,
correction, witness, recorder and retained-consumption implementations run live.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import socket

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.final_review_auditor import adjudicate_context_finding
from src.autoslice.retained_reviewed_draft import (
    RetainedReviewedDraftError,
    pending_entity_review_findings,
    validate_pending_entity_checkpoint,
)
from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
from tests.test_noncritical_routing import (
    BoundWitness, _complete_judge, _finding, srt,
)
from tests.test_producer_text_pipeline_final_review import _adapters
from tests.test_retained_entity_input_recovery import CURRENT, _consume, _pending
from tests.test_retained_entity_public_correction import _normal_review


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))

    def forbidden(*_args, **_kwargs):
        pytest.fail("Integration fixtures cannot dispatch network calls")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("candidate", [False, True])
def test_retained_completion_records_each_raw_judge_at_its_physical_issue(
    tmp_path, monkeypatch, candidate,
):
    retained, original, context = _pending(tmp_path)
    original_before = deepcopy(original)
    seen = {}

    def run(full_context, witness):
        provider = pipeline._build_final_review_llm_call()
        provider.cpa_cache_identity = {"model": "isolated-retained-routing"}
        assert getattr(witness, "argon_escalation", None) is None
        seen["context"] = full_context
        seen["nomination"] = pending_entity_review_findings(retained, srt_text=CURRENT)
        return pipeline._run_final_review(
            srt_text=CURRENT, chat_authority_audit={}, handled_entity_cues=set(),
            verify_confusable_entity=witness, adapters=_adapters(),
            clip_context=full_context, priority_raw_findings=seen["nomination"],
            initial_provider_adjudication_count=50,
        )

    output, review, events, witnesses = _normal_review(
        tmp_path, monkeypatch, retained, context, candidate=candidate, runner=run)
    assert output == CURRENT and len(witnesses) == 1
    assert events.count("audio_judge") == 1
    authority = _consume(tmp_path, retained, review, output=output)
    assert authority["retained_entity_review"]["status"] == "CURRENT_REVIEW_COMPLETED"
    assert authority["retained_entity_review"]["release_authorized"] is False

    # Reopen actual durable rows, then join them to the raw provider invocations.
    root = tmp_path / "runtime" / "cache" / "semantic-cpa-attempts"
    paths = sorted(root.glob("*/*.json"))
    assert paths
    rows = [json.loads(path.read_bytes()) for path in paths]
    calls = json.loads((tmp_path / "provider-replies.json").read_bytes())
    judged_calls = [call for call in calls
                    if call["phase"] not in ("discovery", "current_risk", "rebuilt_risk")]
    assert all(row["status"] == "COMPLETED" for row in rows)
    assert len({row["request_sha256"] for row in rows}) == len(rows)
    assert Counter(hashlib.sha256(row["request"]["prompt"].encode()).hexdigest()
                   for row in rows) == Counter(call["prompt_sha256"] for call in judged_calls)
    raw_by_prompt = {call["prompt_sha256"]: call["raw_completion"] for call in judged_calls}
    expected_issue = SemanticAttemptRecorder(
        srt_text=CURRENT, finding={"cue_index": 1},
        clip_context=seen["context"], offset_ms=0).issue
    for row in rows:
        prompt_hash = hashlib.sha256(row["request"]["prompt"].encode()).hexdigest()
        assert row["issue_sha256"] == expected_issue
        assert row["request"]["model"] == {"model": "isolated-retained-routing"}
        assert row["completion"] == raw_by_prompt[prompt_hash]
        assert row["completion_sha256"] == hashlib.sha256(row["completion"].encode()).hexdigest()

    # Reading the serialized completion gate must preserve every recorded byte.
    before = {str(path): path.read_bytes() for path in paths}
    validate_pending_entity_checkpoint(
        retained, {"chat_authority": json.loads(json.dumps(authority))})
    assert {str(path): path.read_bytes() for path in paths} == before
    assert original == original_before


def test_public_argon_none_reuses_an_existing_recorder_without_double_wrapping(
    tmp_path, monkeypatch,
):
    source = srt("我把杯子放这儿")
    finding = _finding(source)
    context = {"candidate_id": "isolated", "source_media_sha256": "a" * 64}
    witness = BoundWitness("wo ba bei zi fang zhe er")
    judge = _complete_judge("CURRENT")
    recorder = SemanticAttemptRecorder(
        srt_text=source, finding=finding, clip_context=context, offset_ms=0)
    wrapped = recorder.wrap(judge)
    assert wrapped._semantic_attempt_recorder is recorder
    output, audit = adjudicate_context_finding(
        source, finding, entity_verifier=witness, judge_llm_call=wrapped,
        clip_context=context, require_complete_utterance_support=True)
    assert output == source and audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    rows = recorder._entries()
    assert len(rows) == len(judge.calls) > 0
    assert all(row["status"] == "COMPLETED" for row in rows)
    assert {row["request"]["prompt"] for row in rows} == set(judge.calls)
    assert sum(event["status"] == "COMPLETED" for event in recorder.events) == len(rows)
    assert audit["semantic_retry_progress"] == recorder.audit()


def test_partial_unknown_cannot_complete_a_retained_nomination(tmp_path, monkeypatch):
    retained, _, context = _pending(tmp_path)
    proof = {
        "partial_logical_usage_status": "UNKNOWN_PRESERVED_IN_ORIGINAL_EVENTS",
        "partial_logical_adjudication_count": None,
        "raw_cpa_usage": {"completed": 59, "unknown": 1, "usage_records": 142},
    }
    # Use the normal retained loader output, rather than a serialized final-audit projection.
    authority = {"retained_reviewed_draft": {"partial_evidence": proof}}
    before = deepcopy(authority)

    def run(full_context, witness):
        provider = pipeline._build_final_review_llm_call()
        provider.cpa_cache_identity = {"model": "isolated-retained-routing"}
        return pipeline._run_final_review(
            srt_text=CURRENT, chat_authority_audit=authority,
            handled_entity_cues=set(), verify_confusable_entity=witness,
            adapters=_adapters(), clip_context=full_context,
            priority_raw_findings=pending_entity_review_findings(retained, srt_text=CURRENT),
            initial_provider_adjudication_count=108,
        )

    output, review, events, witnesses = _normal_review(
        tmp_path, monkeypatch, retained, context, candidate=True, runner=run)
    assert events == [] and witnesses == []
    assert "RETAINED_PARTIAL_CPA_USAGE_UNKNOWN" in json.dumps(review)
    assert review["retained_partial_evidence"] == proof
    assert authority == before
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume(tmp_path, retained, review, output=output)
    assert not (tmp_path / "provider-replies.json").exists()
    assert not list((tmp_path / "runtime").glob("cache/semantic-cpa-attempts/*/*.json"))
