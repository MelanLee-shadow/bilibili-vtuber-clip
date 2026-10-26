"""K14 exit (admin F7): a re-adjudicated, rule-handed-off question goes to G.

Rule: 维护者 every doubt must be resolved before release (SUB-18). A
retained entity question that the current normal review actually
re-adjudicated, and that the review's own rules then hand to the exact-final
owner (any current unresolved adjudication, independent of stop branch), is
carried into exact-final as a priority finding with its current evidence and
request, and exact-final stays BLOCKED until it is resolved. A question the
current review never reached still raises PENDING_ENTITY_CURRENT_REVIEW_REQUIRED.
Stomach e9c5082 cue11 shape: the final adjudication is K04-stopped and
``history_audio_recovery_from_adjudication`` is None. Normal adjudicator and
attempt recorder; provider boundaries stubbed; no network.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.exact_final_witness_authority import history_audio_recovery_from_adjudication
from src.autoslice.final_review_auditor import adjudicate_context_finding, audit_final_subtitles
from src.autoslice.retained_reviewed_draft import (
    RetainedReviewedDraftError, retained_entity_handoff_findings, validate_pending_entity_checkpoint,
    _exact_final_handoff_reasons, pending_entity_review_findings, require_pending_entity_review,
)
from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
from tests.test_final_review_auditor import _witness
from tests.test_final_review_meaning_policy import equivalent
from tests.test_producer_text_pipeline_final_review import _adapters, _correction_pass
from tests.test_retained_entity_input_recovery import CURRENT, _consume, _native_current_review, _pending

IDENTITY = {"transport": "fixture", "models": ["judge"], "effort": "medium"}


@pytest.fixture
def real_cue11():
    path = Path(__file__).parent / "fixtures/retained_entity_handoff/stomach-b1ccaf63-cue11.json"
    if not path.exists():
        pytest.skip("Private real-subtitle fixture is excluded from the OSS snapshot")
    return json.loads(path.read_text())


def _real_retained(fixture):
    return {
        "source_media_sha256s": fixture["source_media_sha256s"],
        "proof": {key: deepcopy(fixture[key]) for key in
                  ("candidate_id", "parent_authority_sha256", "pending_entity_reviews")},
    }


def _consume_real(tmp_path, fixture):
    retained = _real_retained(fixture)
    authority = {"retained_reviewed_draft": deepcopy(retained["proof"])}
    text = fixture["input_srt_text"]
    require_pending_entity_review(
        retained, input_srt_text=text, output_srt_text=text,
        findings=pending_entity_review_findings(retained, srt_text=text),
        review=fixture["normal_correction_review"], out_root=tmp_path,
        cid=fixture["candidate_id"], authority=authority,
    )
    return retained, authority


def test_real_cue11_old_code_guard(real_cue11):
    """The actual starting generator rejects a judged record outside its state list."""
    root = Path(__file__).resolve().parents[1]
    source = subprocess.check_output(
        ["git", "show", "007688e23:src/autoslice/retained_reviewed_draft.py"],
        cwd=root, text=True,
    )
    old = {"__name__": "retained_handoff_starting_guard"}
    exec(compile(source, "007688e23:retained_reviewed_draft.py", "exec"), old)
    adjudication = real_cue11["normal_correction_review"]["findings"][0]["context_audio_adjudication"]
    assert old["_current_review_adjudicated"](adjudication) is True
    assert old["_exact_final_handoff_reasons"](adjudication) == []
    retained = _real_retained(real_cue11)
    text = real_cue11["input_srt_text"]
    findings = pending_entity_review_findings(retained, srt_text=text)
    assert old["_request_targets_pending_entity"](adjudication["request"], findings[0]) is True
    completed, carried, pending = old["_pending_entity_review_decisions"](
        retained, input_srt_text=text, output_srt_text=text,
        findings=findings,
        review=real_cue11["normal_correction_review"], cid=real_cue11["candidate_id"],
    )
    assert completed == carried == [] and len(pending) == 1


@pytest.mark.parametrize("reverse_rows", [False, True])
def test_real_cue11_current_record_survives_an_unadjudicated_duplicate(tmp_path, real_cue11, reverse_rows):
    rows = real_cue11["normal_correction_review"]["findings"]
    assert [row["cue_index"] for row in rows] == [11, 11]
    assert "context_audio_adjudication" not in rows[1]
    adjudication = deepcopy(rows[0]["context_audio_adjudication"])
    if reverse_rows:
        rows.reverse()
    retained, authority = _consume_real(tmp_path, real_cue11)
    validate_pending_entity_checkpoint(retained, {"chat_authority": authority})
    carried = retained_entity_handoff_findings(real_cue11["input_srt_text"], authority)
    assert len(carried) == 1 and carried[0]["cue_index"] == 11
    assert carried[0]["candidate_provenance"]["handoff_reason_codes"] == [
        "CURRENT_REVIEW_UNRESOLVED_CARRIED", "JUDGE_REQUESTS_MORE_AUDIO",
        "ARGON_READY_CURRENT_CPA_CONSUMPTION_REQUIRED",
    ]
    assert carried[0]["exact_release_adjudication"] == adjudication
    assert carried[0]["candidate_provenance"]["release_authorized"] is False


@pytest.mark.parametrize("change", ["unreviewed", "operator", "witness_authority", "repaired",
                                  "applied", "missing_mutation", "timing", "other_text", "schema",
                                  "resolved", "applied_status"])
def test_current_record_rejection_controls(tmp_path, real_cue11, change):
    row = real_cue11["normal_correction_review"]["findings"][0]
    adjudication = row["context_audio_adjudication"]
    if change == "unreviewed":
        row.pop("context_audio_adjudication")
    elif change == "operator":
        adjudication["decision_authority"] = "OPERATOR"
    elif change == "witness_authority":
        adjudication["witness_authority"] = "DECISION"
    elif change == "repaired":
        adjudication["repaired"] = True
    elif change == "applied":
        adjudication["mutation_authority"]["status"] = "PASS"
    elif change == "missing_mutation":
        adjudication.pop("mutation_authority")
    elif change == "timing":
        adjudication["timing_immutable"] = False
    elif change == "other_text":
        row["base_text_sha256"] = "f" * 64
    elif change == "schema":
        adjudication["schema_version"] = "not-a-current-adjudication"
    elif change == "resolved":
        adjudication["status"] = "RESOLVED"
    else:
        adjudication["status"] = "APPLIED"
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume_real(tmp_path, real_cue11)


def _unlisted_review(context):
    review = _k04_stopped_review(context)
    request = review["findings"][0]["context_audio_adjudication"]["request"]
    review["findings"][0]["context_audio_adjudication"] = {
        "schema_version": "subtitle-span-adjudication.v1", "status": "CURRENT_REVIEW_WAITING",
        "decision_authority": "CPA_JUDGE", "witness_authority": "EVIDENCE_ONLY",
        "repaired": False, "timing_immutable": True,
        "mutation_authority": {"status": "NOT_APPLIED"}, "request": request,
        "policy_branch": "ANOTHER_CURRENT_REVIEW_STOP",
        "semantic_audio_recovery": {"status": "BLOCK", "reason_code": "ANOTHER_RECOVERY_STOP"},
    }
    return review


def _current_row(context):
    row = {"cue_index": 1, "kind": "context", "suspect": "保留文字", "suggestion": "保留蚊子",
           "proposed_full_cue": "保留蚊子", "repair_class": "phonetic", "why": "fixture current review",
           "content_risk": "noncritical_uncertainty", "risk_basis": "fixture exact listening",
           "meaning_equivalence": equivalent("同一话语的实体词面", "保留文字")}
    return audit_final_subtitles(CURRENT, llm_call=lambda _p: json.dumps({"findings": [{**row, "cue": 1}]}),
                                 extract_json=json.loads, candidate_context=context)[0]


def _k04_stopped_review(context, *, unresolved_answers=3):
    """The normal judge has answered this exact material unresolved repeatedly."""

    row = _current_row(context)

    def old_call(_prompt):
        return '{"decision":"UNRESOLVED","needs_audio":true}'
    old_call.cpa_cache_identity = IDENTITY
    for index in range(unresolved_answers):
        SemanticAttemptRecorder(srt_text=CURRENT, finding=row, clip_context=context,
                                offset_ms=0).wrap(old_call)(f"earlier current question {index}")

    def judge(_prompt):
        return json.dumps({"choice": "CURRENT", "needs_audio": True, "reason": "still unclear",
                           "current_utterance_supported": True,
                           "current_utterance_support_reason": "fixture"})
    judge.cpa_cache_identity = IDENTITY
    recorder = SemanticAttemptRecorder(srt_text=CURRENT, finding=row, clip_context=context, offset_ms=0)
    wrapped = recorder.wrap(judge)
    wrapped.cpa_cache_identity = IDENTITY
    _output, decision = adjudicate_context_finding(
        CURRENT, row, clip_context=context,
        entity_verifier=lambda request: _witness(request, "bao liu wen zi"), judge_llm_call=wrapped)
    row["context_audio_adjudication"] = decision
    return {"schema_version": "final-review-audit.v1", "status": "FLAGGED",
            "findings": [row], "applied_count": 0}


def _argon_pending_record():
    return {
        "schema_version": "subtitle-span-adjudication.v1",
        "status": "UNCERTAIN", "repaired": False, "timing_immutable": True,
        "policy_branch": "ARGON_ESCALATION_PENDING_OR_UNRESOLVED",
        "reason_code": "ARGON_ESCALATION_PENDING_OR_UNRESOLVED",
        "mutation_authority": {"status": "NOT_APPLIED"},
        "semantic_audio_recovery": {"status": "ARGON_PENDING"},
        "semantic_retry_progress": {"events": [{"status": "COMPLETED"},
                                               {"status": "COMPLETED"}]},
    }


def test_argon_pending_record_without_judge_authority_is_carried(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    record = _argon_pending_record()
    assert "decision_authority" not in record and "witness_authority" not in record
    assert "ARGON_ESCALATION_PENDING_OR_UNRESOLVED" in _exact_final_handoff_reasons(record)

    retained, _authority, context = _pending(tmp_path)
    review = _k04_stopped_review(context)
    record["request"] = review["findings"][0]["context_audio_adjudication"]["request"]
    review["findings"][0]["context_audio_adjudication"] = record
    authority = _consume(tmp_path, retained, review)

    pointer = authority["retained_entity_review"]
    assert pointer["status"] == "CURRENT_REVIEW_COMPLETED"
    assert pointer["carried_to_exact_final_count"] == 1
    carried = retained_entity_handoff_findings(CURRENT, authority, timeline_offset_ms=0)
    assert len(carried) == 1
    assert carried[0]["candidate_provenance"]["handoff_reason_codes"] == [
        "ARGON_ESCALATION_PENDING_OR_UNRESOLVED"]
    assert carried[0]["exact_release_adjudication"] == record


def test_argon_pending_record_with_foreign_authority_is_not_carried():
    record = _argon_pending_record()
    record["decision_authority"] = "OPERATOR"
    assert _exact_final_handoff_reasons(record) == []


def test_non_argon_record_still_requires_cpa_judge():
    record = _argon_pending_record()
    record.update({"policy_branch": "CONTINUATION_VALUE_PLAN_REQUIRED",
                   "reason_code": "CONTINUATION_VALUE_PLAN_REQUIRED",
                   "semantic_audio_recovery": {"status": "UNRESOLVED"},
                   "decision_authority": None, "witness_authority": "EVIDENCE_ONLY"})
    assert _exact_final_handoff_reasons(record) == []


def test_k04_stopped_re_adjudication_is_carried_not_dead_ended(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    retained, _authority, context = _pending(tmp_path)
    review = _k04_stopped_review(context)
    adjudication = review["findings"][0]["context_audio_adjudication"]
    assert adjudication["witness_judge"]["judge"]["retry_reason_code"] == "CONTINUATION_VALUE_PLAN_REQUIRED"
    assert history_audio_recovery_from_adjudication(adjudication, request=adjudication["request"]) is None

    authority = _consume(tmp_path, retained, review)

    pointer = authority["retained_entity_review"]
    assert pointer["status"] == "CURRENT_REVIEW_COMPLETED"
    assert pointer["carried_to_exact_final_count"] == 1
    assert pointer["release_authorized"] is False and pointer["mutation_authorized"] is False
    validate_pending_entity_checkpoint(retained, {"chat_authority": json.loads(json.dumps(authority))})
    carried = retained_entity_handoff_findings(CURRENT, authority, timeline_offset_ms=0)
    assert len(carried) == 1
    finding = carried[0]
    assert finding["cue_index"] == 1 and finding["suspect"] == "文字"
    assert finding["candidate_provenance"]["kind"] == "retained_entity_current_review_handoff"
    assert finding["candidate_provenance"]["handoff_reason_codes"] == ["CONTINUATION_VALUE_PLAN_REQUIRED"]
    assert finding["candidate_provenance"]["release_authorized"] is False
    # Evidence and original request travel unchanged.
    assert finding["exact_release_adjudication"] == adjudication


def test_unreviewed_question_still_raises_k14(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    retained, _authority, context = _pending(tmp_path)
    review = _k04_stopped_review(context)
    review["findings"][0].pop("context_audio_adjudication")
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume(tmp_path, retained, review)


def test_current_record_does_not_require_enumerated_retry_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    retained, _authority, context = _pending(tmp_path)
    authority = _consume(tmp_path, retained, _unlisted_review(context))
    carried = retained_entity_handoff_findings(CURRENT, authority)
    assert carried[0]["candidate_provenance"]["handoff_reason_codes"] == [
        "CURRENT_REVIEW_UNRESOLVED_CARRIED", "ANOTHER_CURRENT_REVIEW_STOP", "ANOTHER_RECOVERY_STOP"]


def test_editorial_handoff_reason_stays_compatible():
    record = _argon_pending_record()
    record.update({"policy_branch": "NO_GAIN_EDITORIAL_TERMINAL",
                   "decision_authority": "CPA_JUDGE", "witness_authority": "EVIDENCE_ONLY",
                   "reason_code": "EDITORIAL_EXACT_FINAL_REVIEW_REQUIRED",
                   "semantic_audio_recovery": {"status": "BLOCK"}})
    assert _exact_final_handoff_reasons(record) == ["EDITORIAL_EXACT_FINAL_REVIEW_REQUIRED"]


def test_decided_keep_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    retained, _authority, context = _pending(tmp_path)
    authority = _consume(tmp_path, retained, _native_current_review(context))
    assert authority["retained_entity_review"]["status"] == "CURRENT_REVIEW_COMPLETED"
    assert "carried_to_exact_final_count" not in authority["retained_entity_review"]
    assert retained_entity_handoff_findings(CURRENT, authority) == []


def test_carried_completion_cannot_be_rewritten_into_a_different_question(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    retained, _authority, context = _pending(tmp_path)
    authority = _consume(tmp_path, retained, _k04_stopped_review(context))
    with pytest.raises(RetainedReviewedDraftError, match="RETAINED_ENTITY_HANDOFF_TARGET_DRIFT"):
        retained_entity_handoff_findings(CURRENT, authority, timeline_offset_ms=7)
    tampered = deepcopy(authority)
    tampered["retained_entity_review"]["sha256"] = "sha256:" + "0" * 64
    with pytest.raises(RetainedReviewedDraftError):
        retained_entity_handoff_findings(CURRENT, tampered)


@pytest.mark.parametrize("review_builder", [_k04_stopped_review, _unlisted_review])
@pytest.mark.parametrize("mode", [None, "per_finding"])
def test_exact_final_receives_the_carried_question_and_stays_blocked(tmp_path, monkeypatch, review_builder, mode):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    if mode is None:
        monkeypatch.delenv("AUTOSLICE_FINAL_REVIEW_MODE", raising=False)
    else:
        monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_MODE", mode)
    retained, _authority, context = _pending(tmp_path)
    authority = _consume(tmp_path, retained, review_builder(context))
    seen = []
    real = pipeline.audit_final_subtitles

    def capture(*args, **kwargs):
        seen.append([json.loads(n.payload_json) for n in kwargs["discovery_batch"].nominations]
                    if mode == "per_finding" else list(kwargs["extra_raw_findings"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "audit_final_subtitles", capture)
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda *_: "")
    monkeypatch.setattr(pipeline, "discover_candidate_pronoun_findings", lambda *a, **k: ([], {}))

    def review_llm(prompt):
        if "本次不授权改字" in prompt:  # current-risk review of the priority question
            return json.dumps({"findings": [{"cue": 1, "content_risk": "meaning_reversal",
                               "risk_basis": "实体名写错会改变所指对象。"}]}, ensure_ascii=False)
        if "终审审片员" in prompt:  # whole-clip discovery finds nothing new
            return '{"findings": []}'
        return json.dumps({"choice": "CURRENT", "needs_audio": True, "reason": "still unclear",
                           "current_utterance_supported": True,
                           "current_utterance_support_reason": "fixture"})
    review_llm.cpa_cache_identity = IDENTITY
    receipt = pipeline._run_exact_final_release_review(
        srt_text=CURRENT, correction_audit=_correction_pass(), adapters=_adapters(),
        authoritative_chat=(), selection_hook="", clip_context=context, timeline_offset_ms=0,
        verified_authority_audit=authority,
        verify_confusable_entity=lambda request: _witness(request, "bao liu wen zi"),
        final_review_llm=review_llm, pronoun_audit_llm=review_llm,
    )
    handed = [row for row in seen[0] if (row.get("candidate_provenance") or {}).get("kind")
              == "retained_entity_current_review_handoff"]
    assert len(handed) == 1 and handed[0]["cue_index"] == 1
    # Still unresolved in exact-final: BLOCK on the question itself, not a
    # pre-G dead end and not an auditor failure.
    assert receipt["status"] == "FLAGGED" and receipt["release_gate"] == "BLOCK"
    assert "FINAL_REVIEW_FINDING_REVIEW_PENDING" in receipt["reason_codes"]
    assert [row["cue_index"] for row in receipt["findings"]] == [1]
