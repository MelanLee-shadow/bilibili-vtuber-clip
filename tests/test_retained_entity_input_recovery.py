"""Pending source receipts are input evidence, never completed release decisions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.chat_repair import apply_audio_entity_verification
from src.autoslice.chat_evidence import ReferentEntity, ReferentGroup
from src.autoslice.final_review_auditor import adjudicate_context_finding
from src.autoslice.retained_reviewed_draft import (
    RetainedReviewedDraftError,
    pending_entity_review_findings,
    require_pending_entity_review,
    validate_pending_entity_checkpoint,
)
from tests.test_final_review_auditor import _witness
from tests.test_final_review_meaning_policy import equivalent
from tests.test_retained_reviewed_draft import (
    _focused_parent_fixture, _validate_focused_parent, _retained_transcript_entity_row,
)
from tests.test_producer_text_pipeline_final_review import _adapters

CURRENT = "1\n00:00:00,010 --> 00:00:00,030\n保留文字\n"


def _sha(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _pending(tmp_path):
    authority, context, config, _audit, _path, padded_raw = _focused_parent_fixture(tmp_path)

    def verifier(request):
        return {**_retained_transcript_entity_row()["verdict"],
                "request_sha256": request["request_sha256"], "needs_audio": True}

    source, native = apply_audio_entity_verification(
        "1\n00:00:29,960 --> 00:00:29,980\n保留文子\n",
        referent_groups=[ReferentGroup((ReferentEntity("文字", ("文子",)),),
                                       positions=("transcript_only",))],
        entity_verifier=verifier,
    )
    authority["transcript_entity_audit"] = native
    authority["entity_repairs"] = deepcopy(native["repairs"])
    original = json.dumps(authority, sort_keys=True)
    validation = _validate_focused_parent(authority, context, config, padded_raw)
    assert json.dumps(authority, sort_keys=True) == original
    assert validation["status"] == "INPUT_VALIDATED_PENDING_REVIEW"
    with pytest.raises(pipeline.CurrentTextContinuationError, match="VERDICT_INVALID"):
        pipeline._validate_source_stage_transcript_entity_repair(
            native["repairs"][0], authority=authority, srt_text=source)
    item = validation["pending_entity_reviews"][0]
    # The native loader's projection is tested by the isolated original replay;
    # this focused fixture exercises its consumer on a different candidate/grid.
    item["projection"] = {"new_cue_index": "1", "new_start_ms": 10, "new_end_ms": 30}
    retained = {
        "source_media_sha256s": ["sha256:" + "a" * 64],
        "proof": {"candidate_id": "candidate-1", "parent_authority_sha256": _sha(original),
                  "pending_entity_reviews": [item]},
    }
    return retained, authority, context


def _native_current_review(context, *, audio=True):
    row = {
        "cue_index": 1, "kind": "context", "suspect": "保留文字",
        "suggestion": "保留蚊子", "proposed_full_cue": "保留蚊子",
        "repair_class": "phonetic", "why": "fixture asks the ordinary current judge",
        "content_risk": "noncritical_uncertainty", "risk_basis": "fixture exact listening",
        "meaning_equivalence": equivalent("同一话语的实体词面", "保留文字"),
    }
    from src.autoslice.final_review_auditor import audit_final_subtitles
    response = {"findings": [{**row, "cue": 1}]}
    row = audit_final_subtitles(CURRENT, llm_call=lambda _prompt: json.dumps(response),
        extract_json=json.loads, candidate_context=context)[0]
    calls = []

    def judge(_prompt):
        calls.append(_prompt)
        return json.dumps({
            "choice": "CURRENT", "reason": "fixture current utterance review",
            "needs_audio": audio and len(calls) == 1,
            "current_utterance_supported": True,
            "current_utterance_support_reason": "fixture complete target support",
        })

    output, decision = adjudicate_context_finding(
        CURRENT, row, clip_context=context,
        entity_verifier=lambda request: _witness(request, "bao liu wen zi"),
        judge_llm_call=judge,
    )
    assert decision["verdict"]["status"] == ("OBSERVED" if audio else "UNCERTAIN")
    assert output == CURRENT and decision["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    row["context_audio_adjudication"] = decision
    row["routed"] = "disclosure"
    return {"schema_version": "final-review-audit.v1", "status": "FLAGGED",
            "findings": [row], "applied_count": 0}


def _consume(tmp_path, retained, review, *, output=CURRENT):
    authority = {"retained_reviewed_draft": deepcopy(retained["proof"])}
    require_pending_entity_review(
        retained, input_srt_text=CURRENT, output_srt_text=output,
        findings=pending_entity_review_findings(retained, srt_text=CURRENT),
        review=review, out_root=tmp_path, cid="candidate-1", authority=authority,
    )
    return authority


def test_native_pending_input_keeps_original_receipt_and_nominates_no_answer(tmp_path):
    retained, authority, _context = _pending(tmp_path)
    before = deepcopy(authority)
    rows = pending_entity_review_findings(retained, srt_text=CURRENT)
    assert rows[0]["cue_index"] == 1
    assert rows[0]["proposed_full_cue"] is None
    assert "verdict" not in rows[0] and "mutation_authority" not in rows[0]
    assert authority == before and authority["entity_repairs"][0]["verdict"]["needs_audio"] is True


@pytest.mark.parametrize("needs_audio", [None, 1, "true"])
def test_non_boolean_legacy_pending_is_not_admitted(tmp_path, needs_audio):
    _retained, authority, context = _pending(tmp_path)
    _unused, _context, config, _audit, _path, padded = _focused_parent_fixture(tmp_path)
    row = authority["entity_repairs"][0]
    row["verdict"]["needs_audio"] = needs_audio
    authority["transcript_entity_audit"]["repairs"] = [deepcopy(row)]
    with pytest.raises(pipeline.CurrentTextContinuationError, match="VERDICT_INVALID"):
        _validate_focused_parent(authority, context, config, padded)


@pytest.mark.parametrize("change", ["word", "clock"])
def test_current_target_drift_cannot_become_an_unbound_question(tmp_path, change):
    retained, _authority, _context = _pending(tmp_path)
    text = CURRENT.replace("文字", "别字") if change == "word" else CURRENT.replace("00,010", "00,011")
    with pytest.raises(RetainedReviewedDraftError, match="CURRENT_TARGET_DRIFT"):
        pending_entity_review_findings(retained, srt_text=text)


@pytest.mark.parametrize("status", ["CLEAN", "SKIPPED_PINNED_REPLAY", "SKIPPED_TRUTH_FULL_OWNERSHIP",
                                  "SKIPPED", "AUDITOR_UNAVAILABLE"])
def test_empty_or_skipped_review_persists_pending_and_cannot_advance(tmp_path, status):
    retained, _authority, _context = _pending(tmp_path)
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume(tmp_path, retained, {"status": status, "findings": [], "applied_count": 0})
    saved = list(tmp_path.glob("candidate-1.retained-entity-review.*.json"))
    assert len(saved) == 1
    pending = json.loads(saved[0].read_text())
    assert pending["status"] == "PENDING_CURRENT_REVIEW"
    assert len(pending["pending"]) == 1 and pending["release_authorized"] is False
    assert pending["input_srt_text"] == pending["output_srt_text"] == CURRENT


@pytest.mark.parametrize("change", ["needs_audio", "missing_audio_flag", "request_hash",
                                  "source_hash", "source_clock", "candidate", "disclosure"])
def test_new_review_must_actually_resolve_the_same_input(tmp_path, monkeypatch, change):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    retained, _authority, context = _pending(tmp_path)
    review = _native_current_review(context)
    decision = review["findings"][0]["context_audio_adjudication"]
    request = decision["request"]
    judge = decision["witness_judge"]["judge"]
    if change == "needs_audio":
        judge["needs_audio"] = True
    elif change == "missing_audio_flag":
        judge.pop("needs_audio", None)
    elif change == "request_hash":
        request["request_sha256"] = "f" * 64
    elif change == "source_hash":
        request["clip_context_binding"]["source_media_sha256s"] = ["sha256:" + "f" * 64]
    elif change == "source_clock":
        request["source_media_timeline_offset_ms"] = 29950
    elif change == "candidate":
        request["clip_context_binding"]["candidate_id"] = "other-candidate"
    else:
        review["findings"][0].pop("context_audio_adjudication")
    if change in {"source_hash", "source_clock", "candidate"}:
        # Even rehashing a different source/clock cannot borrow this decision.
        request["request_sha256"] = hashlib.sha256(json.dumps(
            {k: v for k, v in request.items() if k != "request_sha256"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume(tmp_path, retained, review)


def test_fresh_native_decision_consumes_question_and_cold_replays_without_old_mutation(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    retained, original, context = _pending(tmp_path)
    old = deepcopy(original)
    review = _native_current_review(context)
    authority = _consume(tmp_path, retained, review)
    validate_pending_entity_checkpoint(retained, {"chat_authority": json.loads(json.dumps(authority))})
    assert authority["retained_entity_review"]["status"] == "CURRENT_REVIEW_COMPLETED"
    assert authority["retained_entity_review"]["release_authorized"] is False
    assert original == old and old["entity_repairs"][0]["verdict"]["needs_audio"] is True


@pytest.mark.parametrize("change", ["missing_completion", "completion_bytes", "rehash_false_clean"])
def test_cold_checkpoint_cannot_claim_completion_without_the_saved_decision(tmp_path, monkeypatch, change):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    retained, _original, context = _pending(tmp_path)
    authority = _consume(tmp_path, retained, _native_current_review(context))
    reference = authority["retained_entity_review"]
    if change == "missing_completion":
        authority.pop("retained_entity_review")
    else:
        path = Path(reference["path"])
        doc = json.loads(path.read_text())
        doc["normal_correction_review"]["findings"] = []
        path.write_text(json.dumps(doc, ensure_ascii=False))
        if change == "rehash_false_clean":
            reference["sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY"):
        validate_pending_entity_checkpoint(retained, {"chat_authority": authority})


def test_normal_empty_discovery_cannot_erase_the_priority_question(tmp_path, monkeypatch):
    retained, _original, context = _pending(tmp_path)
    nominees = pending_entity_review_findings(retained, srt_text=CURRENT)
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call",
                        lambda: lambda _prompt: '{"findings":[]}')
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _context: "")
    output, review = pipeline._run_final_review(
        srt_text=CURRENT, chat_authority_audit={}, handled_entity_cues=set(),
        verify_confusable_entity=lambda _request: pytest.fail("empty discovery cannot buy audio"),
        adapters=_adapters(), clip_context=context, priority_raw_findings=nominees,
    )
    assert output == CURRENT
    assert review["status"] == "AUDITOR_UNAVAILABLE" and review["release_gate"] == "BLOCK"
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume(tmp_path, retained, review)


@pytest.mark.parametrize("change", ["evidence_only", "raw_audio_request", "raw_completion_hash",
                                  "unresolved_type", "unrelated_span", "raw_choice",
                                  "raw_ranking", "witness_hash", "witness_missing"])
def test_evidence_and_unrelated_changes_cannot_settle_the_entity(tmp_path, monkeypatch, change):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    retained, _original, context = _pending(tmp_path)
    review = _native_current_review(context)
    decision = review["findings"][0]["context_audio_adjudication"]
    judge = decision["witness_judge"]["judge"]
    if change == "evidence_only":
        decision["decision_authority"] = "EVIDENCE_ONLY"
    elif change == "raw_audio_request":
        raw = json.loads(judge["span_review_raw_completion"])
        raw["needs_audio"] = True
        judge["span_review_raw_completion"] = json.dumps(raw)
        judge["completion_sha256"] = hashlib.sha256(judge["span_review_raw_completion"].encode()).hexdigest()
    elif change == "raw_completion_hash":
        judge["span_review_raw_completion"] += " "
    elif change == "unresolved_type":
        judge["needs_audio_request_unresolved"] = 0
    elif change in {"raw_choice", "raw_ranking"}:
        raw = json.loads(judge["span_review_raw_completion"])
        if change == "raw_choice":
            raw["choice"] = "PROPOSED"
        else:
            raw["ranking"] = [{"choice": "PROPOSED", "p": 1.0}]
        judge["span_review_raw_completion"] = json.dumps(raw)
        judge["completion_sha256"] = hashlib.sha256(
            judge["span_review_raw_completion"].encode()).hexdigest()
    elif change == "witness_hash":
        decision["verdict"]["request_sha256"] = "f" * 64
    elif change == "witness_missing":
        decision.pop("verdict")
    else:
        request = decision["request"]
        request["suspect"] = "保留"
        request["proposed_cue"] = "留下文字"
        request["request_sha256"] = hashlib.sha256(json.dumps(
            {k: v for k, v in request.items() if k != "request_sha256"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        judge["check_request_sha256"] = request["request_sha256"]
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume(tmp_path, retained, review)


@pytest.mark.parametrize("change", ["source_hash", "delivery_offset", "audit", "request",
                                  "receipt_clock", "receipt_before_hash"])
def test_pending_admission_keeps_all_source_identity_guards(tmp_path, change):
    _retained, authority, context = _pending(tmp_path)
    _unused, _context, config, _audit, _path, padded = _focused_parent_fixture(tmp_path)
    row = authority["entity_repairs"][0]
    if change == "source_hash":
        context["pieces"][0]["source_media_sha256"] = "sha256:" + "e" * 64
    elif change == "delivery_offset":
        config["source_boundary_delivery_offset_ms"] += 1
    elif change == "request":
        row["request_sha256"] = "e" * 64
    elif change == "receipt_clock":
        row["cue_binding"]["coordinate_space"] = "delivery"
    elif change == "receipt_before_hash":
        row["cue_binding"]["before_sha256"] = "e" * 64
    authority["transcript_entity_audit"]["repairs"] = [] if change == "audit" else [deepcopy(row)]
    with pytest.raises(pipeline.CurrentTextContinuationError):
        _validate_focused_parent(authority, context, config, padded)


def test_two_pending_entities_cannot_be_consumed_by_one_decision(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    retained, _original, context = _pending(tmp_path)
    second = deepcopy(retained["proof"]["pending_entity_reviews"][0])
    second["source_repair_index"] = 1
    second["entity_start_codepoint"], second["entity_end_codepoint"] = 0, 2
    retained["proof"]["pending_entity_reviews"].append(second)
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume(tmp_path, retained, _native_current_review(context))
    saved = json.loads(next(tmp_path.glob("candidate-1.retained-entity-review.*.json")).read_text())
    assert len(saved["completed"]) == 1 and len(saved["pending"]) == 1
    assert saved["pending"][0]["suspect"] == "保留"


@pytest.mark.parametrize("current,proposed,start,end,expected", [
    ("甲门到乙门", "甲门到乙文", 1, 2, False),
    ("甲门到乙门", "甲文到乙门", 1, 2, True),
    ("甲门到乙门", "甲文到乙门", 4, 5, False),
    ("甲门到乙门", "甲门到乙文", 4, 5, True),
    ("甲门到乙门", "甲门到乙门", 4, 5, False),
    ("左文字右", "新文字旧", 1, 3, False),
    ("😀保留文字", "😀保留文词", 3, 5, True),
    ("保留文字", "保留文字", 2, 4, True),
])
def test_repeated_entity_and_unicode_positions_are_not_interchangeable(
    current, proposed, start, end, expected,
):
    from src.autoslice.retained_reviewed_draft import _request_targets_pending_entity
    suspect = current[start:end]
    request = {"current_cue": current, "proposed_cue": proposed, "suspect": suspect}
    nominee = {"suspect": suspect, "span_start_codepoint": start, "span_end_codepoint": end}
    assert _request_targets_pending_entity(request, nominee) is expected


def test_native_ranking_decoder_preserves_actual_choice_precedence():
    from src.autoslice.acoustic_witness_adjudication import parse_judge_choice
    assert parse_judge_choice(
        {"choice": "current", "ranking": [
            {"choice": "CURRENT", "p": False},
            {"choice": "proposed", "p": 0.8}, {"choice": "CURRENT", "p": 0.2},
        ]}, {"CURRENT", "PROPOSED", "NEITHER"},
    ) == ("PROPOSED", [{"choice": "PROPOSED", "p": 0.8}, {"choice": "CURRENT", "p": 0.2}])


def test_native_text_only_decision_cannot_close_the_old_audio_question(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    retained, _original, context = _pending(tmp_path)
    review = _native_current_review(context, audio=False)
    with pytest.raises(RetainedReviewedDraftError, match="PENDING_ENTITY_CURRENT_REVIEW_REQUIRED"):
        _consume(tmp_path, retained, review)


def test_explicit_native_span_mismatch_cannot_be_hidden_by_matching_suspect(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    retained, _original, context = _pending(tmp_path)
    review = _native_current_review(context)
    request = review["findings"][0]["context_audio_adjudication"]["request"]
    request["edit_span_binding"]["codepoint_span"] = [0, 2]
    request["request_sha256"] = hashlib.sha256(json.dumps(
        {k: v for k, v in request.items() if k != "request_sha256"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    from src.autoslice.retained_reviewed_draft import _request_targets_pending_entity
    nominee = pending_entity_review_findings(retained, srt_text=CURRENT)[0]
    assert _request_targets_pending_entity(request, nominee) is False
