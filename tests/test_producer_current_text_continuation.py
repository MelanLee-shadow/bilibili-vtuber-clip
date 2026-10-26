import copy
import hashlib
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.clip_context import ClipContextError
from src.autoslice.producer_boundary_owner_contract import (
    freeze_required_boundary_owner_contract,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_completed_correction_envelope_wrong_mode_fails_before_audio_dispatch(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("must reject the input before provider discovery")

    monkeypatch.setattr(pipeline, "discover_priority_findings", forbidden)
    monkeypatch.setattr(pipeline, "_run_exact_final_release_review", forbidden)
    audit = {"correction_pass": {"status": "FLAGGED", "applied_count": 15, "findings": []}}
    original = copy.deepcopy(audit)
    with pytest.raises(pipeline.CurrentTextContinuationError, match="COMPLETED_CORRECTION_ENVELOPE_REQUIRES_CONTINUATION"):
        pipeline._build_exact_final_review_callback(
            spec={}, out_root=tmp_path, cid="clip", padded=tmp_path / "source.mp4",
            adapters=None, authoritative_chat=[], clip_context={}, final_review_audit=audit,
            verify_confusable_entity=None, truth_ownership=None, screen_read_probe=None,
            continuation_correction=False,
        )
    assert audit == original


@pytest.mark.parametrize("source_scope", ["source_full_window", "final_delivery"])
@pytest.mark.parametrize("source_clock_offset", [0, 1_531_330])
def test_exact_callback_uses_explicit_source_role_without_rebinding_old_delivery(
    tmp_path, monkeypatch, source_scope, source_clock_offset,
):
    old_delivery = {"review_scope": "final_delivery", "final_endpoint_binding": {"final_start_ms": 0}}
    audit = {"boundary_semantic_review": old_delivery, "correction_pass": {"applied_count": 0}}
    source = {"review_scope": source_scope, "final_endpoint_binding": {"final_start_ms": 10_630 + source_clock_offset}}
    before = copy.deepcopy(audit)
    seen = []
    physical_discovery_offsets = []
    physical_review_offsets = []

    def boundary(**kwargs):
        seen.append(kwargs)
        return kwargs["correction_audit"]

    monkeypatch.setattr(pipeline, "exact_delivery_correction_audit", boundary)
    def discover(*_args, **kwargs):
        physical_discovery_offsets.append(kwargs["timeline_offset_ms"])
        return [], {}

    def exact_review(**kwargs):
        physical_review_offsets.append(kwargs["timeline_offset_ms"])
        return kwargs["correction_audit"]

    monkeypatch.setattr(pipeline, "discover_priority_findings", discover)
    monkeypatch.setattr(pipeline, "validated_postburn_priority_findings", lambda **_k: None)
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: None)
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _c: "")
    monkeypatch.setattr(pipeline, "fidelity_kept_contexts", lambda *_a: [])
    monkeypatch.setattr(pipeline, "checkpoint_final_review_carryover", lambda *_a: None)
    monkeypatch.setattr(pipeline, "_run_exact_final_release_review", exact_review)
    kwargs = dict(
        spec={}, out_root=tmp_path, cid="clip", padded=tmp_path / "source.mp4",
        adapters=None, authoritative_chat=[], clip_context={}, final_review_audit=audit,
        verify_confusable_entity=None, truth_ownership=None, screen_read_probe=None,
        continuation_correction=True, source_boundary_review=source,
        source_boundary_timeline_offset_ms=source_clock_offset,
    )
    if source_scope != "source_full_window":
        with pytest.raises(pipeline.CurrentTextContinuationError, match="SOURCE_BOUNDARY_REVIEW_SCOPE"):
            pipeline._build_exact_final_review_callback(**kwargs)
        assert not seen
    else:
        callback = pipeline._build_exact_final_review_callback(**kwargs)
        result = callback("1\n00:00:00,000 --> 00:00:01,000\n原句\n", {}, 10_630, 216_290)
        assert seen[0]["correction_audit"]["boundary_semantic_review"] == source
        assert seen[0]["source_final_start_ms"] == 10_630 + source_clock_offset
        assert seen[0]["source_final_end_ms"] == 216_290 + source_clock_offset
        assert physical_discovery_offsets == physical_review_offsets == [10_630]
        assert result["boundary_semantic_review"] == source
    assert audit == before and audit["boundary_semantic_review"] is old_delivery


@pytest.mark.parametrize("offset", [True, -1, 1.5, "1531330", None])
def test_exact_callback_rejects_invalid_source_clock_origin_before_review(tmp_path, offset):
    with pytest.raises(pipeline.CurrentTextContinuationError, match="SOURCE_BOUNDARY_TIMELINE_OFFSET"):
        pipeline._build_exact_final_review_callback(
            spec={}, out_root=tmp_path, cid="clip", padded=tmp_path / "source.mp4",
            adapters=None, authoritative_chat=[], clip_context={}, final_review_audit={},
            verify_confusable_entity=None, truth_ownership=None, screen_read_probe=None,
            source_boundary_review={"review_scope": "source_full_window"},
            source_boundary_timeline_offset_ms=offset,
        )


def test_retained_timing_loader_binds_the_same_complete_json_bytes(tmp_path):
    path = tmp_path / "resolution.json"
    path.write_bytes(json.dumps({"sanitized_cues": [{"text": "完整文字"}]}, ensure_ascii=False).encode())
    continuation = SimpleNamespace(source_timing_resolution_path=path,
                                   expected_source_timing_resolution_sha256="sha256:" + _sha(path.read_bytes()))
    value, digest = pipeline._continuation_load_timing_resolution(continuation)
    assert value["sanitized_cues"][0]["text"] == "完整文字"
    assert digest == continuation.expected_source_timing_resolution_sha256
    path.write_bytes(b'{}')
    with pytest.raises(pipeline.CurrentTextContinuationError, match="DRIFT"):
        pipeline._continuation_load_timing_resolution(continuation)


@pytest.mark.parametrize("case", ["missing_path", "missing_sha", "symlink", "invalid_json", "non_object"])
def test_invalid_timing_reference_fails_before_current_review(tmp_path, case):
    path = tmp_path / "resolution.json"
    path.write_bytes(b'{}')
    expected = _sha(path.read_bytes())
    if case == "missing_path":
        path = None
    elif case == "missing_sha":
        expected = None
    elif case == "symlink":
        link = tmp_path / "alias.json"
        link.symlink_to(path)
        path = link
    else:
        path.write_bytes(b'not json' if case == "invalid_json" else b'[]')
        expected = _sha(path.read_bytes())
    continuation = SimpleNamespace(source_timing_resolution_path=path,
                                   expected_source_timing_resolution_sha256=expected,
                                   transcriber=lambda: None)
    with pytest.raises(pipeline.CurrentTextContinuationError, match="CURRENT_SOURCE_TIMING_RESOLUTION"):
        pipeline._run_current_text_continuation(
            spec={}, durations=[], padded=tmp_path / "not_read", padded_dur=0,
            host="localhost", cid="candidate", out_root=tmp_path, adapters=None,
            continuation=continuation,
        )


from tests.continuation_support import srt as _srt


def _valid_authority(tmp_path: Path) -> tuple[dict, dict, str]:
    srt = _srt()
    srt_sha = _sha(srt.encode())
    context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": "relationship-candidate",
        "recording_date": "2026-10-04",
        "pieces": [{"source_media_sha256": "sha256:" + "a" * 64}],
        "whole_clip_draft_srt": srt,
        "whole_clip_draft_srt_sha256": "sha256:" + srt_sha,
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "mutation_authorized": False,
    }
    payload = dict(context)
    context["context_sha256"] = "sha256:" + _sha(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    from tests.continuation_support import review as synthetic_review
    review = synthetic_review()
    # Fresh synthetic discovery for continuation-mechanics tests, not an
    # upgrade of the archived file's old resolved/disclosed judgments. Only
    # this low-level provider reply is fixed; real release validators run.
    from src.autoslice.final_review_meaning_policy import POLICY_VERSION
    from src.autoslice.final_review_auditor import audit_final_subtitles
    current_findings = audit_final_subtitles(srt, llm_call=lambda prompt: '{"findings":[]}',
                                             extract_json=json.loads)
    assert not current_findings
    review.update(meaning_policy=POLICY_VERSION, findings=current_findings,
                  resolved_findings=[], unresolved_findings_disclosed=[], validated_finding_count=0)
    chat = {
        "schema_version": "chat-authority-audit.v2",
        "status": "NO_MATCH",
        "final_status": "FINAL_ARTIFACTS_VERIFIED",
        "final_output_srt_sha256": "sha256:" + srt_sha,
        "mutation_authorized": False,
        "entity_repairs": [],
        "final_review_audit": review,
    }
    return chat, context, srt_sha


def test_existing_normal_authority_is_consumed_without_private_schema(tmp_path):
    chat, context, srt_sha = _valid_authority(tmp_path)
    result = pipeline._continuation_validate_existing_authority(
        chat,
        srt_text=_srt(),
        srt_sha256=srt_sha,
        clip_context=context,
        candidate_id="relationship-candidate",
    )
    assert result["schema_version"] == "final-review-audit.v2"
    assert not hasattr(pipeline.CurrentTextContinuation, "typed_evidence_path")


def _parser_blocked_authority(tmp_path):
    chat, context, srt_sha = _valid_authority(tmp_path)
    runtime = {"transport": "cpa_command", "models": ["test-model"], "effort": "medium"}
    binding = {
        "candidate_id": "relationship-candidate",
        "final_srt_sha256": "sha256:" + srt_sha,
        "actual_media_sha256": "sha256:" + "b" * 64,
        "witness_srt_sha256": "sha256:" + "c" * 64,
        "audio_sha256": "sha256:" + "d" * 64,
        "raw_result_sha256": "sha256:" + "e" * 64,
        "receipt_sha256": "sha256:" + "f" * 64,
        "intro_offset_ms": 5_000,
    }
    request = {
        **binding,
        "schema_version": "postburn-subtitle-audio-cpa-request.v1",
        "witness_authority": "EVIDENCE_ONLY",
        "candidate_context_sha256": "sha256:" + _sha(
            pipeline.clip_context_prompt_text(context).encode()
        ),
        "cpa_cache_identity": runtime,
        "provider_runtime_binding": {"host": "runtime"},
    }
    request["request_sha256"] = pipeline._postburn_request_sha256(request)
    prompt, response = tmp_path / "prompt.txt", tmp_path / "response.txt"
    prompt.write_text("existing ordinary prompt")
    response.write_text('{"findings": [{"cue_index": 1, "proposed_full_cue": null}]}')
    postburn = {
        "schema_version": "postburn-subtitle-audio-review.v1",
        "status": "AUDITOR_UNAVAILABLE",
        "release_gate": "BLOCK",
        "reason_codes": ["POSTBURN_SUBTITLE_AUDIO_CPA_UNAVAILABLE"],
        "error_type": "FinalReviewAuditError",
        "reviewed_srt_sha256": "sha256:" + srt_sha,
        "validated_finding_count": 0,
        "findings": [],
        "subtitle_audio_binding": binding,
        "cpa_request": request,
        "cpa_cache_identity": runtime,
        "provider_runtime_binding": request["provider_runtime_binding"],
        "cpa_calls": [{
            "dispatch_status": "COMPLETED",
            "request_sha256": request["request_sha256"],
            "prompt_path": str(prompt),
            "prompt_sha256": "sha256:" + _sha(prompt.read_bytes()),
            "response_path": str(response),
            "response_sha256": "sha256:" + _sha(response.read_bytes()),
        }],
    }
    chat["postburn_subtitle_audio_review"] = postburn
    chat["final_review_audit"].update(
        status="AUDITOR_UNAVAILABLE", release_gate="BLOCK",
        reason_codes=postburn["reason_codes"], postburn_subtitle_audio_review=postburn,
    )
    return chat, context, srt_sha


def test_completed_parser_failure_resumes_blocked_input_without_release(tmp_path):
    chat, context, srt_sha = _parser_blocked_authority(tmp_path)
    before = json.dumps(chat, sort_keys=True)
    result = pipeline._continuation_validate_existing_authority(
        chat, srt_text=_srt(), srt_sha256=srt_sha,
        clip_context=context, candidate_id="relationship-candidate",
    )
    assert json.dumps(chat, sort_keys=True) == before
    assert result["release_gate"] == "BLOCK"
    with pytest.raises(pipeline.FinalReviewContractError, match="RELEASE_GATE_BLOCKED"):
        pipeline.validate_final_review_release(result, expected_srt_sha256="sha256:" + srt_sha)


@pytest.mark.parametrize("failure", ["raw_drift", "dispatch_incomplete", "wrong_candidate", "provider_failure", "boundary_block", "self_heal_drift"])
def test_parser_resume_does_not_admit_other_failures(tmp_path, failure):
    chat, context, srt_sha = _parser_blocked_authority(tmp_path)
    postburn = chat["postburn_subtitle_audio_review"]
    if failure == "raw_drift":
        Path(postburn["cpa_calls"][0]["response_path"]).write_text("changed")
    elif failure == "dispatch_incomplete":
        postburn["cpa_calls"][0]["dispatch_status"] = "RUNNING"
    elif failure == "wrong_candidate":
        postburn["cpa_request"]["candidate_id"] = "other"
        postburn["cpa_request"]["request_sha256"] = pipeline._postburn_request_sha256(postburn["cpa_request"])
    elif failure == "provider_failure":
        postburn["error_type"] = "TimeoutError"
    elif failure == "self_heal_drift":
        chat["exact_final_cpa_self_heal"] = {"status": "PASS", "final_srt_sha256": "sha256:" + "0" * 64}
    else:
        chat["final_review_audit"]["boundary_semantic_review"]["status"] = "BLOCK"
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            chat, srt_text=_srt(), srt_sha256=srt_sha,
            clip_context=context, candidate_id="relationship-candidate",
        )


def _semantic_blocked_authority(tmp_path):
    chat, context, srt_sha = _parser_blocked_authority(tmp_path)
    cue = pipeline.parse_srt_cues(_srt())[0]
    finding = {
        "cue_index": 1, "kind": "context", "suspect": cue.text,
        "base_text_sha256": _sha(cue.text.encode()),
        "proposed_full_cue": None, "why": "Meaning needs further evidence",
    }
    postburn = chat["postburn_subtitle_audio_review"]
    postburn.update(status="FLAGGED", validated_finding_count=1,
                    findings=[finding], reason_codes=["POSTBURN_SUBTITLE_AUDIO_CPA_FINDINGS"])
    postburn.pop("error_type")
    chat["final_review_audit"].update(
        status="FLAGGED", release_gate="BLOCK", findings=[dict(finding)],
        validated_finding_count=1, reason_codes=["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
    )
    return chat, context, srt_sha


def test_unresolved_semantic_postburn_resumes_review_input_but_not_release(tmp_path):
    chat, context, srt_sha = _semantic_blocked_authority(tmp_path)
    before = json.dumps(chat, sort_keys=True)
    result = pipeline._continuation_validate_existing_authority(
        chat, srt_text=_srt(), srt_sha256=srt_sha,
        clip_context=context, candidate_id="relationship-candidate",
    )
    assert json.dumps(chat, sort_keys=True) == before
    assert result["status"] == "FLAGGED"
    assert result["release_gate"] == "BLOCK"
    assert len(result["findings"]) == 1
    with pytest.raises(pipeline.FinalReviewContractError, match="FINAL_REVIEW_FINDING_REVIEW_PENDING"):
        pipeline.validate_final_review_release(result, expected_srt_sha256="sha256:" + srt_sha)


def _semantic_pending_after_clean_postburn(tmp_path):
    chat, context, digest = _semantic_blocked_authority(tmp_path)
    postburn = chat["postburn_subtitle_audio_review"]
    postburn.update(status="CLEAN", release_gate="PASS", findings=[],
                    validated_finding_count=0, reason_codes=[])
    call = postburn["cpa_calls"][0]
    Path(call["response_path"]).write_text('{"findings": []}')
    call["response_sha256"] = "sha256:" + _sha(Path(call["response_path"]).read_bytes())
    return chat, context, digest


def test_later_semantic_finding_resumes_after_same_byte_clean_postburn(tmp_path):
    chat, context, digest = _semantic_pending_after_clean_postburn(tmp_path)
    before = json.dumps(chat, sort_keys=True)
    # A clean receipt never becomes ordinary priority discovery implicitly.
    assert pipeline.validated_postburn_priority_findings(
        final_srt_text=_srt(), verified_authority_audit=chat,
        candidate_id="relationship-candidate", clip_context=context,
    ) is None
    result = pipeline._continuation_validate_existing_authority(
        chat, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    assert json.dumps(chat, sort_keys=True) == before
    assert result["status"] == "FLAGGED"
    assert result["release_gate"] == "BLOCK"
    assert len(result["findings"]) == 1
    with pytest.raises(pipeline.FinalReviewContractError, match="FINAL_REVIEW_FINDING_REVIEW_PENDING"):
        pipeline.validate_final_review_release(result, expected_srt_sha256="sha256:" + digest)


@pytest.mark.parametrize("failure", [
    "raw_drift", "unfinished_dispatch", "wrong_srt", "wrong_context",
    "nested_mismatch", "other_reason", "finding_count", "typed_chain",
])
def test_clean_postburn_pending_recovery_rejects_unbound_or_unfinished_input(tmp_path, failure):
    chat, context, digest = _semantic_pending_after_clean_postburn(tmp_path)
    postburn = chat["postburn_subtitle_audio_review"]
    if failure == "raw_drift":
        Path(postburn["cpa_calls"][0]["response_path"]).write_text("changed")
    elif failure == "unfinished_dispatch":
        postburn["cpa_calls"][0]["dispatch_status"] = "RUNNING"
    elif failure == "wrong_srt":
        postburn["reviewed_srt_sha256"] = "sha256:" + "0" * 64
    elif failure == "wrong_context":
        postburn["cpa_request"]["candidate_context_sha256"] = "sha256:" + "0" * 64
    elif failure == "nested_mismatch":
        chat["final_review_audit"]["postburn_subtitle_audio_review"] = {}
    elif failure == "other_reason":
        chat["final_review_audit"]["reason_codes"].append("OTHER_REASON")
    elif failure == "finding_count":
        chat["final_review_audit"]["validated_finding_count"] = 0
    else:
        chat["final_review_audit"]["exact_final_cpa_self_heal"]["passes"][-1]["output_srt_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            chat, srt_text=_srt(), srt_sha256=digest,
            clip_context=context, candidate_id="relationship-candidate",
        )


def _preburn_pending_authority(tmp_path):
    chat, context, srt_sha = _valid_authority(tmp_path)
    review = chat["final_review_audit"]
    pending_self_heal = copy.deepcopy(review.pop("exact_final_cpa_self_heal"))
    pending_self_heal["status"] = "REVIEW_PENDING"
    chat["exact_final_cpa_self_heal"] = pending_self_heal
    review.update(
        status="FLAGGED",
        release_gate="BLOCK",
        reason_codes=["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
        findings=[{"cue_index": 1, "reason": "preburn unresolved finding"}],
        validated_finding_count=1,
    )
    return chat, context, srt_sha


def test_preburn_pending_resumes_as_review_input_but_keeps_release_blocked(tmp_path):
    chat, context, srt_sha = _preburn_pending_authority(tmp_path)
    before = json.dumps(chat, sort_keys=True)
    result = pipeline._continuation_validate_existing_authority(
        chat,
        srt_text=_srt(),
        srt_sha256=srt_sha,
        clip_context=context,
        candidate_id="relationship-candidate",
    )
    assert json.dumps(chat, sort_keys=True) == before
    assert result["status"] == "FLAGGED"
    assert result["release_gate"] == "BLOCK"
    # The resumed input keeps its stored legacy code; the release gate reports
    # the shared lifecycle code for the still-pending finding.
    assert result["reason_codes"] == ["FINAL_REVIEW_UNRESOLVED_FINDINGS"]
    with pytest.raises(pipeline.FinalReviewContractError, match="FINAL_REVIEW_FINDING_REVIEW_PENDING"):
        pipeline.validate_final_review_release(result, expected_srt_sha256="sha256:" + srt_sha)


def _last_typed_pass_clock(tmp_path):
    chat, _context, digest = _preburn_pending_authority(tmp_path)
    cues = list(pipeline.parse_srt_cues(_srt()))
    last = chat["exact_final_cpa_self_heal"]["passes"][-1]
    for repair in last["repairs"]:
        position = repair["cue_index"] - 1
        cue = cues[position]
        cues[position] = type(cue)(
            index=cue.index, start_ms=cue.start_ms, end_ms=cue.end_ms,
            text=repair["before"],
        )
    reference = pipeline._render_cues_to_srt(cues)
    assert "sha256:" + _sha(reference.encode()) == last["input_srt_sha256"]
    return chat, reference, digest


def test_source_clock_replays_normal_typed_text_suffix_without_promoting_review(tmp_path):
    chat, reference, digest = _last_typed_pass_clock(tmp_path)
    before = copy.deepcopy(chat)
    pipeline._continuation_replay_clock_text_chain(
        chat, reference_text=reference, reference_sha=_sha(reference.encode()),
        current_text=_srt(), current_sha=digest,
    )
    assert chat == before
    assert chat["final_review_audit"]["release_gate"] == "BLOCK"
    assert chat["exact_final_cpa_self_heal"]["status"] == "REVIEW_PENDING"


def _caption_successor_clock(tmp_path):
    from src.autoslice.exact_final_cpa_history import with_caption_successor
    chat, reference, _digest = _last_typed_pass_clock(tmp_path)
    receipt = chat["exact_final_cpa_self_heal"]
    row = copy.deepcopy(receipt["passes"][-1])
    row["pass_index"] = 1
    repair = row["repairs"][0]
    cues = list(pipeline.parse_srt_cues(reference))
    before = pipeline._render_cues_to_srt(cues)
    cue = cues[repair["cue_index"] - 1]
    repair.update(before=cue.text, after="谢谢观众的 superchat")
    repair["before_sha256"] = "sha256:" + _sha(repair["before"].encode())
    repair["after_sha256"] = "sha256:" + _sha(repair["after"].encode())
    cues[repair["cue_index"] - 1] = type(cue)(
        index=cue.index, start_ms=cue.start_ms, end_ms=cue.end_ms, text=repair["after"],
    )
    current = pipeline._render_cues_to_srt(cues)
    row.update(input_srt_sha256="sha256:" + _sha(before.encode()),
               output_srt_sha256="sha256:" + _sha(current.encode()))
    receipt["passes"] = [row]
    receipt.pop("final_srt_sha256", None)
    normalized, _ = pipeline.normalize_caption_display_surfaces(current)
    chat["exact_final_cpa_self_heal"] = with_caption_successor(receipt, current, normalized)
    return chat, before, normalized


def test_caption_successor_keeps_typed_history_and_clock_without_a_release(tmp_path):
    from src.autoslice.exact_final_cpa_history import build_run_audit
    chat, reference, current = _caption_successor_clock(tmp_path)
    history = copy.deepcopy(chat)
    pipeline._continuation_replay_clock_text_chain(
        chat, reference_text=reference, reference_sha=_sha(reference.encode()),
        current_text=current, current_sha=_sha(current.encode()),
    )
    receipt = chat["exact_final_cpa_self_heal"]
    assert len(receipt["passes"]) == 1
    assert receipt["passes"][0]["repairs"][0]["after"] == "谢谢观众的 superchat"
    assert receipt["status"] == "REVIEW_PENDING"
    assert chat["final_review_audit"]["release_gate"] == "BLOCK"
    assert chat == history
    pending = build_run_audit(receipt["passes"], [],
                             caption_display_successors=receipt["caption_display_successors"])
    pipeline._validate_exact_final_cpa_self_heal(
        {"exact_final_cpa_self_heal": pending},
        expected_srt_sha256="sha256:" + _sha(current.encode()), allow_pending=True,
    )
    with pytest.raises(pipeline.FinalReviewContractError):
        pipeline._validate_exact_final_cpa_self_heal(
            {"exact_final_cpa_self_heal": pending},
            expected_srt_sha256="sha256:" + _sha(current.encode()),
        )
    # A later real typed pass starts at canonical bytes while old receipts
    # retain their original input/output spellings and pass count.
    row = copy.deepcopy(receipt["passes"][0])
    row["pass_index"] = 2
    repair = row["repairs"][0]
    repair.update(before="谢谢观众的 SC", after="谢谢观众的 SC！")
    repair["before_sha256"] = "sha256:" + _sha(repair["before"].encode())
    repair["after_sha256"] = "sha256:" + _sha(repair["after"].encode())
    later = current.replace("谢谢观众的 SC", "谢谢观众的 SC！")
    row.update(input_srt_sha256="sha256:" + _sha(current.encode()),
               output_srt_sha256="sha256:" + _sha(later.encode()))
    receipt["passes"].append(row)
    pipeline._continuation_replay_clock_text_chain(
        chat, reference_text=reference, reference_sha=_sha(reference.encode()),
        current_text=later, current_sha=_sha(later.encode()),
    )


def _pending_caption_successor_authority(tmp_path):
    chat, _reference, current = _caption_successor_clock(tmp_path)
    terminal = chat["exact_final_cpa_self_heal"]["caption_display_successors"][-1]
    chat["final_output_srt_sha256"] = terminal["input_srt_sha256"]
    chat["final_review_audit"]["reviewed_srt_sha256"] = "sha256:" + _sha(current.encode())
    _old_chat, context, _digest = _valid_authority(tmp_path)
    return chat, context, current


def test_pending_authority_consumes_registered_terminal_caption_successor(tmp_path):
    chat, context, current = _pending_caption_successor_authority(tmp_path)
    before = copy.deepcopy(chat)
    result = pipeline._continuation_validate_existing_authority(
        chat, srt_text=current, srt_sha256=_sha(current.encode()),
        clip_context=context, candidate_id="relationship-candidate",
    )
    assert chat == before
    assert result["release_gate"] == "BLOCK"
    assert chat["exact_final_cpa_self_heal"]["status"] == "REVIEW_PENDING"
    with pytest.raises(pipeline.FinalReviewContractError):
        pipeline.validate_final_review_release(result, expected_srt_sha256="sha256:" + _sha(current.encode()))


@pytest.mark.parametrize("failure", ["missing", "old_pointer", "bytes", "profile", "timing", "chain", "old_review"])
def test_pending_authority_rejects_unproved_caption_successor(tmp_path, failure):
    chat, context, current = _pending_caption_successor_authority(tmp_path)
    receipt = chat["exact_final_cpa_self_heal"]
    if failure == "missing":
        receipt.pop("caption_display_successors")
    elif failure == "old_pointer":
        chat["final_output_srt_sha256"] = "sha256:" + "0" * 64
    elif failure == "bytes":
        current = current.replace("观众", "另一个人")
    elif failure == "profile":
        receipt["caption_display_successors"][0]["normalization_audit"]["authority"] = "invented"
    elif failure == "timing":
        current = current.replace("00:00:00,000", "00:00:00,001")
    elif failure == "chain":
        receipt["passes"][-1]["output_srt_sha256"] = "sha256:" + "1" * 64
    else:
        chat["final_review_audit"]["reviewed_srt_sha256"] = chat["final_output_srt_sha256"]
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            chat, srt_text=current, srt_sha256=_sha(current.encode()),
            clip_context=context, candidate_id="relationship-candidate",
        )


@pytest.mark.parametrize("failure", ["text", "hash", "profile_audit", "grid", "pass_index", "chain", "duplicate"])
def test_caption_successor_rejects_unregistered_text_or_rebound_chain(tmp_path, failure):
    chat, reference, current = _caption_successor_clock(tmp_path)
    receipt = chat["exact_final_cpa_self_heal"]
    row = receipt["caption_display_successors"][0]
    if failure == "text":
        row["input_srt_text"] = row["input_srt_text"].replace("观众", "别的人")
    elif failure == "hash":
        row["output_srt_sha256"] = "sha256:" + "0" * 64
    elif failure == "profile_audit":
        row["normalization_audit"]["authority"] = "invented-authority"
    elif failure == "grid":
        row["input_srt_text"] = row["input_srt_text"].replace("00:00:00,000", "00:00:00,001")
    elif failure == "pass_index":
        row["after_pass_index"] = True
    elif failure == "chain":
        receipt["passes"][0]["output_srt_sha256"] = "sha256:" + "1" * 64
    else:
        receipt["caption_display_successors"].append(copy.deepcopy(row))
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_replay_clock_text_chain(
            chat, reference_text=reference, reference_sha=_sha(reference.encode()),
            current_text=current, current_sha=_sha(current.encode()),
        )


@pytest.mark.parametrize("failure", ["old_bytes", "repair_window", "current_bytes", "missing_suffix"])
def test_source_clock_typed_suffix_rejects_unproved_bytes_and_timing(tmp_path, failure):
    chat, reference, digest = _last_typed_pass_clock(tmp_path)
    reference_sha = _sha(reference.encode())
    current = _srt()
    if failure == "old_bytes":
        reference = reference.replace("合成中间句", "不同的原句")
    elif failure == "repair_window":
        chat["exact_final_cpa_self_heal"]["passes"][-1]["repairs"][0]["matched_end_ms"] += 1
    elif failure == "current_bytes":
        current = current.replace("00:00:00,000", "00:00:00,001")
    else:
        reference_sha = "0" * 64
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_replay_clock_text_chain(
            chat, reference_text=reference, reference_sha=reference_sha,
            current_text=current, current_sha=digest,
        )


def _boundary_pending_checkpoint(tmp_path):
    reference, context, digest = _preburn_pending_authority(tmp_path)
    reference["final_review_audit"]["boundary_semantic_review"]["candidate_id"] = "relationship-candidate"
    reference["post_transcript_entity_output_srt_sha256"] = "sha256:" + "9" * 64
    reference["frozen_boundary_owner_contract"] = {"owner_set_sha256": "sha256:" + "8" * 64}
    current = copy.deepcopy(reference)
    current["final_review_audit"].update(
        reason_codes=["FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"],
        findings=[], validated_finding_count=0,
        boundary_semantic_review={
            "schema_version": "talk-boundary-semantic-review.v1", "status": "BLOCK",
            "reason_codes": ["CURRENT_TEXT_REQUIRES_SOURCE_REVIEW"],
        },
        pre_boundary_text_recovery={
            "status": "TEXT_COMPLETE_BOUNDARY_PENDING", "release_gate": "BLOCK",
            "reviewed_srt_sha256": "sha256:" + digest,
        },
    )
    path = tmp_path / "prior-normal-chat.json"
    path.write_text(json.dumps(reference), encoding="utf-8")
    continuation = SimpleNamespace(
        source_clock_authority_path=path,
        expected_source_clock_authority_sha256="sha256:" + _sha(path.read_bytes()),
    )
    return current, context, digest, continuation


def test_completed_text_checkpoint_uses_same_byte_clock_but_keeps_boundary_block(tmp_path):
    current, context, digest, continuation = _boundary_pending_checkpoint(tmp_path)
    before = json.dumps(current, sort_keys=True)
    clock, binding = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    review = pipeline._continuation_validate_existing_authority(
        current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
        candidate_id="relationship-candidate", source_clock_review=clock,
    )
    assert binding["role"] == "EVIDENCE_ONLY_SOURCE_CLOCK"
    assert binding["historical_release_gate"] == "BLOCK"
    assert review["boundary_semantic_review"]["status"] == "BLOCK"
    assert json.dumps(current, sort_keys=True) == before
    with pytest.raises(pipeline.FinalReviewContractError, match="BOUNDARY_SEMANTIC_BLOCKED"):
        pipeline.validate_final_review_release(review, expected_srt_sha256="sha256:" + digest)


def _discovery_pending_checkpoint(tmp_path):
    current, context, digest, continuation = _boundary_pending_checkpoint(tmp_path)
    review = current["final_review_audit"]
    review.pop("pre_boundary_text_recovery")
    review.update(status="AUDITOR_UNAVAILABLE", reason_codes=["FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"],
                  discovery={"status": "AUDITOR_UNAVAILABLE", "detail": "LlmCallError"})
    clock, _ = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    return current, context, digest, clock


@pytest.mark.parametrize("clock_status", ["CLEAN", "FLAGGED"])
def test_unavailable_discovery_recovers_only_as_blocked_input_with_valid_original_clock(tmp_path, clock_status):
    current, context, digest, clock = _discovery_pending_checkpoint(tmp_path)
    if clock_status == "CLEAN":
        clock.update(status="CLEAN", release_gate="PASS", reason_codes=[], findings=[], validated_finding_count=0)
    original = copy.deepcopy(current)
    original_clock = copy.deepcopy(clock)
    review = pipeline._continuation_validate_existing_authority(
        current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
        candidate_id="relationship-candidate", source_clock_review=clock,
    )
    assert current == original and clock == original_clock
    assert review == original["final_review_audit"]
    assert review["release_gate"] == "BLOCK"
    assert review["discovery"]["status"] == "AUDITOR_UNAVAILABLE"
    with pytest.raises(pipeline.FinalReviewContractError, match="DISCOVERY_INCOMPLETE"):
        pipeline.validate_final_review_release(review, expected_srt_sha256="sha256:" + digest)


@pytest.mark.parametrize("drift", ["no_clock", "clock_grid", "mutation", "current_hash", "finding", "reason"])
def test_incomplete_discovery_cannot_hide_other_invalid_input(tmp_path, drift):
    current, context, digest, clock = _discovery_pending_checkpoint(tmp_path)
    review = current["final_review_audit"]
    if drift == "no_clock":
        clock = None
    elif drift == "clock_grid":
        clock["boundary_semantic_review"]["final_endpoint_binding"]["status"] = "BLOCK"
    elif drift == "mutation":
        review["correction_mutation_authority"]["status"] = "BLOCK"
    elif drift == "current_hash":
        review["reviewed_srt_sha256"] = "sha256:" + "f" * 64
    elif drift == "finding":
        review.update(findings=[{"cue_index": 1}], validated_finding_count=1)
    else:
        review["reason_codes"] = ["UNKNOWN_FAILURE"]
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
            candidate_id="relationship-candidate", source_clock_review=clock,
        )


def _resolver_pending_checkpoint(tmp_path, status):
    current, context, digest, continuation = _boundary_pending_checkpoint(tmp_path)
    grid = pipeline.cue_grid_sha256(pipeline.parse_srt_cues(_srt()))
    projected_grid = "sha256:" + "b" * 64
    current["final_review_audit"]["current_delivery_source_projection"] = {
        "schema_version": "delivery-to-source-timeline-projection.v1",
        "status": "PASS", "delivery_srt_cue_grid_sha256": grid,
        "projected_source_cue_grid_sha256": projected_grid,
    }
    current["final_review_audit"]["boundary_semantic_review"] = {
        "schema_version": "talk-boundary-semantic-review.v1", "status": status,
        "candidate_id": "relationship-candidate", "review_scope": "source_full_window",
        "request_sha256": "sha256:" + "c" * 64, "cue_grid_sha256": "sha256:" + "d" * 64,
        "current_delivery_text_evidence": {
            "role": "CURRENT_TEXT_PROPOSAL_NOT_FINAL_CLEAN",
            "delivery_cue_grid_sha256": grid,
            "projected_source_cue_grid_sha256": projected_grid,
            "source_cue_grid_sha256": "sha256:" + "d" * 64,
        },
    }
    return current, context, digest, continuation


@pytest.mark.parametrize("status", ["PASS", "BLOCK"])
def test_actual_resolver_intermediate_checkpoint_resumes_but_cannot_release(tmp_path, status):
    current, context, digest, continuation = _resolver_pending_checkpoint(tmp_path, status)
    before = json.dumps(current, sort_keys=True)
    clock, _ = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    review = pipeline._continuation_validate_existing_authority(
        current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
        candidate_id="relationship-candidate", source_clock_review=clock,
    )
    assert review["boundary_semantic_review"]["status"] == status
    assert review["release_gate"] == "BLOCK"
    assert json.dumps(current, sort_keys=True) == before
    with pytest.raises(pipeline.FinalReviewContractError):
        pipeline.validate_final_review_release(review, expected_srt_sha256="sha256:" + digest)


@pytest.mark.parametrize("failure", [
    "candidate", "request", "source_grid", "current_grid", "projected_grid",
    "missing_projected_grid", "endpoint", "missing_clock", "clock_geometry", "typed_chain",
])
def test_resolver_pending_checkpoint_rejects_unbound_or_drifted_evidence(tmp_path, failure):
    current, context, digest, continuation = _resolver_pending_checkpoint(tmp_path, "PASS")
    boundary = current["final_review_audit"]["boundary_semantic_review"]
    evidence = boundary["current_delivery_text_evidence"]
    if failure == "candidate":
        boundary["candidate_id"] = "other"
    elif failure == "request":
        boundary["request_sha256"] = "not-a-request-hash"
    elif failure == "source_grid":
        evidence["source_cue_grid_sha256"] = "sha256:" + "0" * 64
    elif failure == "current_grid":
        evidence["delivery_cue_grid_sha256"] = "sha256:" + "0" * 64
    elif failure == "projected_grid":
        evidence["projected_source_cue_grid_sha256"] = "sha256:" + "0" * 64
    elif failure == "missing_projected_grid":
        evidence.pop("projected_source_cue_grid_sha256")
        current["final_review_audit"]["current_delivery_source_projection"].pop("projected_source_cue_grid_sha256")
    elif failure == "endpoint":
        boundary["final_endpoint_binding"] = {"status": "PASS"}
    elif failure == "typed_chain":
        current["exact_final_cpa_self_heal"]["passes"][-1]["output_srt_sha256"] = "sha256:" + "0" * 64
    clock, _ = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    if failure == "missing_clock":
        clock = None
    elif failure == "clock_geometry":
        clock["boundary_semantic_review"]["final_endpoint_binding"]["final_snapped_end_ms"] += 1
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
            candidate_id="relationship-candidate", source_clock_review=clock,
        )


def _mixed_pending_checkpoint(tmp_path):
    current, context, digest, continuation = _boundary_pending_checkpoint(tmp_path)
    review = current["final_review_audit"]
    review.update(
        reason_codes=[
            "FINAL_REVIEW_UNRESOLVED_FINDINGS",
            "FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED",
        ],
        findings=[{"cue_index": 1, "reason": "retained unresolved text finding"}],
        validated_finding_count=1,
    )
    # This mirrors the real mixed v02 checkpoint: the boundary BLOCK is
    # retained, while its separate source-clock artifact supplies geometry;
    # no synthetic text-complete receipt is required on the current chat.
    review.pop("pre_boundary_text_recovery")
    return current, context, digest, continuation


def test_mixed_pending_checkpoint_resumes_with_bound_source_clock_and_keeps_both_blocks(tmp_path):
    current, context, digest, continuation = _mixed_pending_checkpoint(tmp_path)
    before = json.dumps(current, sort_keys=True)
    clock, binding = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    review = pipeline._continuation_validate_existing_authority(
        current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
        candidate_id="relationship-candidate", source_clock_review=clock,
    )
    assert binding["role"] == "EVIDENCE_ONLY_SOURCE_CLOCK"
    assert review["release_gate"] == "BLOCK"
    assert review["reason_codes"] == [
        "FINAL_REVIEW_UNRESOLVED_FINDINGS",
        "FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED",
    ]
    assert review["findings"] == [{"cue_index": 1, "reason": "retained unresolved text finding"}]
    assert review["boundary_semantic_review"] == {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "BLOCK",
        "reason_codes": ["CURRENT_TEXT_REQUIRES_SOURCE_REVIEW"],
    }
    assert json.dumps(current, sort_keys=True) == before
    with pytest.raises(pipeline.FinalReviewContractError, match="FINAL_REVIEW_FINDING_REVIEW_PENDING"):
        pipeline.validate_final_review_release(review, expected_srt_sha256="sha256:" + digest)


def _mixed_final_endpoint_checkpoint(tmp_path):
    from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint

    current, context, digest, continuation = _mixed_pending_checkpoint(tmp_path)
    clock = json.loads(continuation.source_clock_authority_path.read_text())
    boundary = copy.deepcopy(clock["final_review_audit"]["boundary_semantic_review"])
    endpoint = boundary["final_endpoint_binding"]
    boundary.update(status="BLOCK", reason_codes=["TERMINAL_SOURCE_SEPARATION_WITNESS_BLOCKED"])
    boundary["source_separation_witness"].update(
        status="BLOCK", reason_codes=["SOURCE_BOUNDARY_REVIEW_NOT_PASS"],
    )
    cues = pipeline.parse_srt_cues(_srt())
    boundary, reasons = bind_final_semantic_endpoint(
        semantic_review=boundary, cues=cues, closure_cue=cues[-1],
        snapped_end_ms=cues[-1].end_ms,
        final_start_ms=endpoint["final_start_ms"],
        final_end_ms=endpoint["final_end_ms"],
    )
    assert reasons == ["BOUNDARY_SEMANTIC_REVIEW_NOT_PASS"]
    current["final_review_audit"]["boundary_semantic_review"] = boundary
    return current, context, digest, continuation


def test_mixed_final_endpoint_checkpoint_resumes_review_without_promoting_boundary(tmp_path):
    current, context, digest, continuation = _mixed_final_endpoint_checkpoint(tmp_path)
    before = json.dumps(current, sort_keys=True)
    clock, _ = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    review = pipeline._continuation_validate_existing_authority(
        current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
        candidate_id="relationship-candidate", source_clock_review=clock,
    )
    assert review["release_gate"] == "BLOCK"
    assert review["boundary_semantic_review"]["final_endpoint_binding"]["status"] == "BLOCK"
    assert review["validated_finding_count"] == 1
    assert json.dumps(current, sort_keys=True) == before
    with pytest.raises(pipeline.FinalReviewContractError, match="FINAL_REVIEW_FINDING_REVIEW_PENDING"):
        pipeline.validate_final_review_release(review, expected_srt_sha256="sha256:" + digest)


def _ordinary_boundary_pending_checkpoint(tmp_path):
    current, context, digest, continuation = _mixed_final_endpoint_checkpoint(tmp_path)
    review = current["final_review_audit"]
    review.update(
        findings=[], validated_finding_count=0,
        reason_codes=["FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"],
    )
    review.pop("pre_boundary_text_recovery", None)
    return current, context, digest, continuation


def test_ordinary_exact_boundary_checkpoint_resumes_without_pre_boundary_marker(tmp_path):
    current, context, digest, continuation = _ordinary_boundary_pending_checkpoint(tmp_path)
    before = json.dumps(current, sort_keys=True)
    clock, _ = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    review = pipeline._continuation_validate_existing_authority(
        current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
        candidate_id="relationship-candidate", source_clock_review=clock,
    )
    assert review["release_gate"] == "BLOCK"
    assert "pre_boundary_text_recovery" not in review
    assert json.dumps(current, sort_keys=True) == before
    with pytest.raises(pipeline.FinalReviewContractError, match="BOUNDARY_SEMANTIC_BLOCKED"):
        pipeline.validate_final_review_release(review, expected_srt_sha256="sha256:" + digest)


@pytest.mark.parametrize("failure", [
    "missing_clock", "candidate", "endpoint_time", "typed_chain", "not_pending", "reason",
])
def test_ordinary_exact_boundary_checkpoint_rejects_missing_or_drifted_evidence(tmp_path, failure):
    current, context, digest, continuation = _ordinary_boundary_pending_checkpoint(tmp_path)
    if failure == "candidate":
        current["final_review_audit"]["boundary_semantic_review"]["candidate_id"] = "other"
    elif failure == "endpoint_time":
        current["final_review_audit"]["boundary_semantic_review"]["final_endpoint_binding"]["final_end_ms"] += 1
    elif failure == "typed_chain":
        current["exact_final_cpa_self_heal"]["passes"][-1]["output_srt_sha256"] = "sha256:" + "0" * 64
    elif failure == "not_pending":
        current["exact_final_cpa_self_heal"]["status"] = "COMPLETE"
    elif failure == "reason":
        current["final_review_audit"]["reason_codes"].append("OTHER_REASON")
    clock, _ = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    if failure == "missing_clock":
        clock = None
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
            candidate_id="relationship-candidate", source_clock_review=clock,
        )


@pytest.mark.parametrize("failure", [
    "candidate", "request", "closure", "grid", "endpoint_time", "endpoint_reason",
    "missing_clock", "typed_chain",
])
def test_mixed_final_endpoint_checkpoint_rejects_drift(tmp_path, failure):
    current, context, digest, continuation = _mixed_final_endpoint_checkpoint(tmp_path)
    boundary = current["final_review_audit"]["boundary_semantic_review"]
    endpoint = boundary["final_endpoint_binding"]
    if failure == "candidate":
        boundary["candidate_id"] = "other"
    elif failure == "request":
        boundary["request_sha256"] = "not-a-request-hash"
    elif failure == "closure":
        endpoint["closure_text_sha256"] = "sha256:" + "0" * 64
    elif failure == "grid":
        endpoint["final_cue_grid_sha256"] = "sha256:" + "0" * 64
    elif failure == "endpoint_time":
        endpoint["final_end_ms"] += 1
    elif failure == "endpoint_reason":
        endpoint["reason_codes"].append("BOUNDARY_SEMANTIC_CUE_GRID_MISMATCH")
    elif failure == "typed_chain":
        current["exact_final_cpa_self_heal"]["passes"][-1]["output_srt_sha256"] = "sha256:" + "0" * 64
    clock, _ = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    if failure == "missing_clock":
        clock = None
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
            candidate_id="relationship-candidate", source_clock_review=clock,
        )


@pytest.mark.parametrize(
    "failure", ["missing_source_clock", "other_reason", "source_clock_drift", "typed_chain"],
)
def test_mixed_pending_checkpoint_rejects_missing_or_drifted_binding(tmp_path, failure):
    current, context, digest, continuation = _mixed_pending_checkpoint(tmp_path)
    if failure == "missing_source_clock":
        continuation.source_clock_authority_path = None
        continuation.expected_source_clock_authority_sha256 = None
    elif failure == "other_reason":
        current["final_review_audit"]["reason_codes"].append("OTHER_REASON")
    elif failure == "typed_chain":
        current["exact_final_cpa_self_heal"]["passes"][-1]["output_srt_sha256"] = (
            "sha256:" + "0" * 64
        )
    clock, _ = pipeline._continuation_source_clock_reference(
        continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
        clip_context=context, candidate_id="relationship-candidate",
    )
    if failure == "source_clock_drift":
        clock["boundary_semantic_review"]["final_endpoint_binding"]["final_snapped_end_ms"] += 1
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
            candidate_id="relationship-candidate", source_clock_review=clock,
        )


@pytest.mark.parametrize("failure", ["no_reference", "pair", "file_drift", "srt_drift", "candidate", "source", "owner", "chain", "not_complete"])
def test_pending_clock_recovery_rejects_drift_and_unfinished_text(tmp_path, failure):
    current, context, digest, continuation = _boundary_pending_checkpoint(tmp_path)
    if failure == "no_reference":
        continuation.source_clock_authority_path = None
        continuation.expected_source_clock_authority_sha256 = None
    elif failure == "pair":
        continuation.expected_source_clock_authority_sha256 = None
    elif failure == "file_drift":
        continuation.source_clock_authority_path.write_text("{}")
    elif failure in {"srt_drift", "candidate"}:
        reference = json.loads(continuation.source_clock_authority_path.read_text())
        if failure == "srt_drift":
            reference["final_output_srt_sha256"] = "sha256:" + "0" * 64
        else:
            reference["final_review_audit"]["boundary_semantic_review"]["candidate_id"] = "other"
        continuation.source_clock_authority_path.write_text(json.dumps(reference))
        continuation.expected_source_clock_authority_sha256 = "sha256:" + _sha(continuation.source_clock_authority_path.read_bytes())
    elif failure == "source":
        current["post_transcript_entity_output_srt_sha256"] = "sha256:" + "0" * 64
    elif failure == "owner":
        current["frozen_boundary_owner_contract"] = {}
    elif failure == "chain":
        current["exact_final_cpa_self_heal"]["passes"][-1]["output_srt_sha256"] = "sha256:" + "0" * 64
    else:
        current["final_review_audit"]["findings"] = [{"cue_index": 1}]
        current["final_review_audit"]["validated_finding_count"] = 1
    with pytest.raises(pipeline.CurrentTextContinuationError):
        clock, _ = pipeline._continuation_source_clock_reference(
            continuation, chat_authority=current, srt_text=_srt(), srt_sha256=digest,
            clip_context=context, candidate_id="relationship-candidate",
        )
        pipeline._continuation_validate_existing_authority(
            current, srt_text=_srt(), srt_sha256=digest, clip_context=context,
            candidate_id="relationship-candidate", source_clock_review=clock,
        )


def _pending_with_exact_final_owner(tmp_path, judge_schema, *, owner_offset=None):
    from src.autoslice.producer_package_finalization import _register_exact_final_cpa_repairs

    chat, context, srt_sha = _preburn_pending_authority(tmp_path)
    repair = chat["exact_final_cpa_self_heal"]["passes"][-1]["repairs"][0]
    repair["judge"]["schema_version"] = judge_schema
    repair["judge"].pop("check_request_sha256")
    boundary = chat["final_review_audit"]["boundary_semantic_review"]
    boundary_offset = boundary["source_separation_witness"]["source_final_start_ms"] - boundary["final_endpoint_binding"]["final_start_ms"]
    offset = boundary_offset if owner_offset is None else owner_offset
    # Exact-final replay must use the clock recorded by both request records
    # on the repair itself; this fixture is intentionally explicit rather than
    # relying on the boundary's current delivery offset.
    repair["acoustic_witness_binding"] = {
        "check_request": {"source_media_timeline_offset_ms": offset},
        "witness_request": {"source_media_timeline_offset_ms": offset},
    }
    _register_exact_final_cpa_repairs(chat, [repair], delivery_start_ms=offset)
    return chat, context, srt_sha


def _range_projection_owner(tmp_path, *, mutation=None):
    """Build a registered owner whose only acoustic authority is a range projection."""
    from src.autoslice.exact_final_witness_authority import (
        RANGE_WORD_PROJECTION_FIELD,
        build_range_word_projection_binding,
    )
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from src.autoslice.producer_package_finalization import _register_exact_final_cpa_repairs

    try:
        from test_independent_context_adjudication import _native_range_artifacts
    except ImportError:  # pragma: no cover - package-style pytest collection
        from tests.test_independent_context_adjudication import _native_range_artifacts

    source, finding, clip, _narrow, observation, _paths = _native_range_artifacts(
        tmp_path, "mai", word_timing=True,
    )
    request = build_context_adjudication_request(
        source,
        {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip,
        source_media_timeline_offset_ms=100,
    )
    request = copy.deepcopy(request)
    request["proposed_cue"] = "乙"
    request_payload = dict(request)
    request_payload.pop("request_sha256")
    request["request_sha256"] = _sha(json.dumps(
        request_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode())
    range_row = request["context_only_range_acoustic_observations"][0]
    binding = build_range_word_projection_binding(
        check_request=request, range_observation=range_row,
    )
    assert binding is not None

    repair = {
        "schema_version": "exact-final-cpa-self-heal.v1",
        "cue_index": 2,
        "matched_start_ms": request["matched_start_ms"],
        "matched_end_ms": request["matched_end_ms"],
        "before": request["current_cue"],
        "after": request["proposed_cue"],
        "before_sha256": "sha256:" + _sha(request["current_cue"].encode()),
        "after_sha256": "sha256:" + _sha(request["proposed_cue"].encode()),
        "finding_sha256": "sha256:" + _sha(b"range-owner-finding"),
        "request_sha256": request["request_sha256"],
        "decision_authority": "CPA_JUDGE",
        "action": "REPLACE_CUE_TEXT",
        "repair_class": "phonetic",
        "policy_branch": "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED",
        "acoustic_witness": None,
        RANGE_WORD_PROJECTION_FIELD: binding,
        "judge": {
            "schema_version": "exact-final-cpa-convergence-judge.v1",
            "status": "JUDGED",
            "choice": "PROPOSED",
        },
        "mutation_authority": {
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "PASS",
            "basis": "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED",
        },
        "timing_immutable": True,
    }
    if mutation == "both":
        repair["acoustic_witness_binding"] = {"schema_version": "ambiguous"}
    elif mutation == "offset":
        binding = copy.deepcopy(binding)
        binding["witness_request"]["source_media_timeline_offset_ms"] += 1
        binding_payload = dict(binding)
        binding_payload.pop("binding_sha256")
        binding["binding_sha256"] = "sha256:" + _sha(json.dumps(
            binding_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode())
        repair[RANGE_WORD_PROJECTION_FIELD] = binding

    replay: dict[str, object] = {}
    _register_exact_final_cpa_repairs(
        replay, [copy.deepcopy(repair)], delivery_start_ms=100,
    )
    return {
        "exact_final_cpa_self_heal": {
            "passes": [{"repairs": [repair]}],
        },
        "entity_repairs": replay["entity_repairs"],
        "exact_final_cpa_surface_registrations": replay[
            "exact_final_cpa_surface_registrations"
        ],
    }


def test_exact_final_owner_replays_native_range_projection_without_narrow_relabel(tmp_path):
    chat = _range_projection_owner(tmp_path)
    before = copy.deepcopy(chat)
    pipeline._continuation_validate_exact_final_owner(
        chat["entity_repairs"][0], row_index=0, chat_authority=chat,
    )
    assert chat == before
    repair = chat["exact_final_cpa_self_heal"]["passes"][0]["repairs"][0]
    assert repair.get("acoustic_witness_binding") is None
    assert repair["range_word_projection_binding"][
        "witness_protocol"
    ] == "native_word_timed_range_context"


@pytest.mark.parametrize("mutation", ["both", "offset"])
def test_exact_final_owner_rejects_ambiguous_or_drifted_range_projection(tmp_path, mutation):
    chat = _range_projection_owner(tmp_path, mutation=mutation)
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match=(
            "CURRENT_EXACT_FINAL_OWNER_WITNESS_INVALID"
            if mutation == "both"
            else "CURRENT_EXACT_FINAL_OWNER_WITNESS_INVALID"
        ),
    ):
        pipeline._continuation_validate_exact_final_owner(
            chat["entity_repairs"][0], row_index=0, chat_authority=chat,
        )


def _refresh_exact_owner_digest(chat):
    repair = chat["exact_final_cpa_self_heal"]["passes"][-1]["repairs"][0]
    digest = "sha256:" + _sha(json.dumps(
        repair, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode())
    chat["entity_repairs"][0]["exact_final_repair_sha256"] = digest
    chat["exact_final_cpa_surface_registrations"][0]["exact_final_repair_sha256"] = digest


def test_exact_final_owner_uses_repair_local_clock(tmp_path):
    chat, context, srt_sha = _pending_with_exact_final_owner(
        tmp_path, "exact-final-cpa-cycle-judge.v1", owner_offset=10_000,
    )
    result = pipeline._continuation_validate_existing_authority(
        chat, srt_text=_srt(), srt_sha256=srt_sha,
        clip_context=context, candidate_id="relationship-candidate",
    )
    assert result["release_gate"] == "BLOCK"


def _retained_exact_final_owner(tmp_path):
    from src.autoslice.exact_final_cpa_history import retained_runs

    chat, _context, digest = _pending_with_exact_final_owner(
        tmp_path, "exact-final-cpa-cycle-judge.v1", owner_offset=10_000,
    )
    completed = copy.deepcopy(chat["exact_final_cpa_self_heal"])
    completed.update(status="PASS", final_srt_sha256="sha256:" + digest)
    chat["exact_final_cpa_self_heal"] = {
        "schema_version": "exact-final-cpa-self-heal-audit.v1",
        "status": "REVIEW_PENDING",
        "passes": [],
        "retained_previous_runs": retained_runs(completed),
    }
    return chat


def test_exact_final_owner_replays_completed_retained_run_without_promoting_it(tmp_path):
    chat = _retained_exact_final_owner(tmp_path)
    before = copy.deepcopy(chat)
    pipeline._continuation_validate_exact_final_owner(
        chat["entity_repairs"][0], row_index=0, chat_authority=chat,
    )
    assert chat == before
    assert chat["exact_final_cpa_self_heal"]["status"] == "REVIEW_PENDING"
    assert chat["final_review_audit"]["release_gate"] == "BLOCK"


@pytest.mark.parametrize("mutation", ["envelope", "rehashed_broken_chain", "registration", "duplicate"])
def test_exact_final_owner_retained_history_still_rejects_drift(tmp_path, mutation):
    chat = _retained_exact_final_owner(tmp_path)
    history = chat["exact_final_cpa_self_heal"]["retained_previous_runs"]
    entry = history[0]
    if mutation == "envelope":
        entry["audit"]["passes"][0]["output_srt_sha256"] = "sha256:" + "0" * 64
    elif mutation == "rehashed_broken_chain":
        entry["audit"]["passes"][0]["output_srt_sha256"] = "sha256:" + "0" * 64
        entry["audit_sha256"] = "sha256:" + _sha(json.dumps(
            entry["audit"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode())
    elif mutation == "registration":
        chat["exact_final_cpa_surface_registrations"] = []
    else:
        history.append(copy.deepcopy(entry))
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_exact_final_owner(
            chat["entity_repairs"][0], row_index=0, chat_authority=chat,
        )


@pytest.mark.parametrize("clock_mutation", ["missing", "conflicting"])
def test_exact_final_owner_rejects_missing_or_conflicting_request_clock(tmp_path, clock_mutation):
    chat, context, srt_sha = _pending_with_exact_final_owner(
        tmp_path, "exact-final-cpa-cycle-judge.v1",
    )
    binding = chat["exact_final_cpa_self_heal"]["passes"][-1]["repairs"][0][
        "acoustic_witness_binding"
    ]
    if clock_mutation == "missing":
        del binding["check_request"]["source_media_timeline_offset_ms"]
    else:
        binding["witness_request"]["source_media_timeline_offset_ms"] += 1
    _refresh_exact_owner_digest(chat)
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_EXACT_FINAL_OWNER_CLOCK_INVALID",
    ):
        pipeline._continuation_validate_existing_authority(
            chat, srt_text=_srt(), srt_sha256=srt_sha,
            clip_context=context, candidate_id="relationship-candidate",
        )


def test_exact_final_owner_validates_and_retires_superseded_successor(tmp_path):
    from src.autoslice.producer_package_finalization import _register_exact_final_cpa_repairs

    chat, context, _srt_sha = _pending_with_exact_final_owner(
        tmp_path, "exact-final-cpa-cycle-judge.v1",
    )
    receipt = chat["exact_final_cpa_self_heal"]
    predecessor = receipt["passes"][-1]["repairs"][0]
    successor = copy.deepcopy(predecessor)
    successor["before"] = predecessor["after"]
    successor["after"] = predecessor["after"] + "。"
    successor["before_sha256"] = "sha256:" + _sha(successor["before"].encode())
    successor["after_sha256"] = "sha256:" + _sha(successor["after"].encode())
    receipt["passes"][-1]["repairs"].append(successor)
    _register_exact_final_cpa_repairs(
        chat, [successor],
        delivery_start_ms=predecessor["acoustic_witness_binding"]["check_request"][
            "source_media_timeline_offset_ms"
        ],
    )
    pipeline._continuation_validate_exact_final_owner(
        chat["entity_repairs"][0], row_index=0, chat_authority=chat,
    )
    assert chat["entity_repairs"][0]["reconciliation"]["status"] == (
        "SUPERSEDED_BY_EXACT_FINAL_CPA"
    )


def test_exact_final_owner_rejects_broken_supersession_chain(tmp_path):
    from src.autoslice.producer_package_finalization import _register_exact_final_cpa_repairs

    chat, _context, _srt_sha = _pending_with_exact_final_owner(
        tmp_path, "exact-final-cpa-cycle-judge.v1",
    )
    receipt = chat["exact_final_cpa_self_heal"]
    predecessor = receipt["passes"][-1]["repairs"][0]
    successor = copy.deepcopy(predecessor)
    successor["before"] = predecessor["after"]
    successor["after"] = predecessor["after"] + "。"
    successor["before_sha256"] = "sha256:" + _sha(successor["before"].encode())
    successor["after_sha256"] = "sha256:" + _sha(successor["after"].encode())
    receipt["passes"][-1]["repairs"].append(successor)
    _register_exact_final_cpa_repairs(
        chat, [successor],
        delivery_start_ms=predecessor["acoustic_witness_binding"]["check_request"][
            "source_media_timeline_offset_ms"
        ],
    )
    chat["entity_repairs"][0]["reconciliation"]["after_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID",
    ):
        pipeline._continuation_validate_exact_final_owner(
            chat["entity_repairs"][0], row_index=0, chat_authority=chat,
        )


@pytest.mark.parametrize("schema", ["exact-final-cpa-cycle-judge.v1", "exact-final-cpa-convergence-judge.v1"])
def test_exact_final_owner_resumes_its_own_contract_without_ordinary_request_field(tmp_path, schema):
    chat, context, srt_sha = _pending_with_exact_final_owner(tmp_path, schema)
    original = copy.deepcopy(chat)
    result = pipeline._continuation_validate_existing_authority(
        chat, srt_text=_srt(), srt_sha256=srt_sha,
        clip_context=context, candidate_id="relationship-candidate",
    )
    assert chat == original
    assert result["release_gate"] == "BLOCK"
    with pytest.raises(pipeline.FinalReviewContractError, match="FINAL_REVIEW_FINDING_REVIEW_PENDING"):
        pipeline.validate_final_review_release(result, expected_srt_sha256="sha256:" + srt_sha)


@pytest.mark.parametrize("failure", ["repair_digest", "judge", "window", "registration", "chain"])
def test_exact_final_owner_requires_existing_chain_and_registration(tmp_path, failure):
    chat, context, srt_sha = _pending_with_exact_final_owner(tmp_path, "exact-final-cpa-cycle-judge.v1")
    row = chat["entity_repairs"][0]
    if failure == "repair_digest":
        row["exact_final_repair_sha256"] = "sha256:" + "0" * 64
    elif failure == "judge":
        # A separate tampered owner cannot borrow the original repair's SHA.
        row["judge"] = {**row["judge"], "choice": "CURRENT"}
    elif failure == "window":
        row["matched_start_ms"] += 1
    elif failure == "registration":
        chat["exact_final_cpa_surface_registrations"][0]["owner_entity_repair_index"] = 99
    else:
        chat["exact_final_cpa_self_heal"]["passes"][-1]["output_srt_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            chat, srt_text=_srt(), srt_sha256=srt_sha,
            clip_context=context, candidate_id="relationship-candidate",
        )


def _pending_with_historical_postburn(tmp_path):
    chat, context, srt_sha = _preburn_pending_authority(tmp_path)
    old_sha = chat["exact_final_cpa_self_heal"]["passes"][0]["input_srt_sha256"]
    postburn = {
        "schema_version": "postburn-subtitle-audio-review.v1",
        "status": "FLAGGED", "release_gate": "BLOCK",
        "reviewed_srt_sha256": old_sha,
        "findings": [{"cue_index": 1, "reason": "original postburn finding"}],
    }
    chat["postburn_subtitle_audio_review"] = postburn
    chat["final_review_audit"]["postburn_subtitle_audio_review"] = postburn
    return chat, context, srt_sha


def test_pending_typed_successor_resumes_without_rebinding_old_postburn(tmp_path):
    chat, context, srt_sha = _pending_with_historical_postburn(tmp_path)
    before = json.dumps(chat, sort_keys=True)
    result = pipeline._continuation_validate_existing_authority(
        chat, srt_text=_srt(), srt_sha256=srt_sha,
        clip_context=context, candidate_id="relationship-candidate",
    )
    assert json.dumps(chat, sort_keys=True) == before
    assert result["release_gate"] == "BLOCK"
    assert pipeline.validated_postburn_priority_findings(
        final_srt_text=_srt(), verified_authority_audit=chat,
        candidate_id="relationship-candidate", clip_context=context,
    ) is None
    with pytest.raises(pipeline.FinalReviewContractError, match="FINAL_REVIEW_FINDING_REVIEW_PENDING"):
        pipeline.validate_final_review_release(result, expected_srt_sha256="sha256:" + srt_sha)


@pytest.mark.parametrize("failure", ["outside_chain", "current_bytes", "chain_drift", "receipt_mismatch"])
def test_historical_postburn_resume_requires_current_typed_successor(tmp_path, failure):
    chat, context, srt_sha = _pending_with_historical_postburn(tmp_path)
    if failure == "outside_chain":
        chat["postburn_subtitle_audio_review"]["reviewed_srt_sha256"] = "sha256:" + "0" * 64
    elif failure == "current_bytes":
        chat["postburn_subtitle_audio_review"]["reviewed_srt_sha256"] = "sha256:" + srt_sha
    elif failure == "chain_drift":
        chat["exact_final_cpa_self_heal"]["passes"][-1]["output_srt_sha256"] = "sha256:" + "0" * 64
    else:
        chat["final_review_audit"]["postburn_subtitle_audio_review"] = {}
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            chat, srt_text=_srt(), srt_sha256=srt_sha,
            clip_context=context, candidate_id="relationship-candidate",
        )


@pytest.mark.parametrize(
    "failure",
    ["terminal_sha", "chain", "typed", "window", "geometry", "missing_review"],
)
def test_preburn_pending_rejects_drifted_or_missing_authority(tmp_path, failure):
    chat, context, srt_sha = _preburn_pending_authority(tmp_path)
    self_heal = chat["exact_final_cpa_self_heal"]
    if failure == "terminal_sha":
        self_heal["passes"][-1]["output_srt_sha256"] = "sha256:" + "0" * 64
    elif failure == "chain":
        self_heal["passes"][1]["input_srt_sha256"] = "sha256:" + "0" * 64
    elif failure == "typed":
        self_heal["passes"][0]["repairs"][0]["mutation_authority"]["status"] = "BLOCK"
    elif failure == "window":
        chat["final_review_audit"]["boundary_semantic_review"]["final_endpoint_binding"][
            "final_snapped_end_ms"
        ] += 1
    elif failure == "geometry":
        chat["final_review_audit"]["boundary_semantic_review"]["status"] = "BLOCK"
    else:
        del chat["final_review_audit"]
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._continuation_validate_existing_authority(
            chat,
            srt_text=_srt(),
            srt_sha256=srt_sha,
            clip_context=context,
            candidate_id="relationship-candidate",
        )


@pytest.mark.parametrize("failure", [
    "raw_drift", "dispatch_incomplete", "wrong_candidate", "context_drift",
    "boundary_block", "finding_drift", "missing_postburn", "invalid_disclosure",
    "findings_count", "self_heal_drift",
])
def test_semantic_resume_preserves_other_input_contracts(tmp_path, failure):
    chat, context, srt_sha = _semantic_blocked_authority(tmp_path)
    postburn = chat["postburn_subtitle_audio_review"]
    if failure == "raw_drift":
        Path(postburn["cpa_calls"][0]["response_path"]).write_text("changed")
    elif failure == "dispatch_incomplete":
        postburn["cpa_calls"][0]["dispatch_status"] = "RUNNING"
    elif failure == "wrong_candidate":
        postburn["cpa_request"]["candidate_id"] = "other"
        postburn["cpa_request"]["request_sha256"] = pipeline._postburn_request_sha256(postburn["cpa_request"])
    elif failure == "context_drift":
        context["whole_clip_draft_srt"] += "changed"
    elif failure == "boundary_block":
        chat["final_review_audit"]["boundary_semantic_review"]["status"] = "BLOCK"
    elif failure == "finding_drift":
        postburn["findings"][0]["base_text_sha256"] = "0" * 64
    elif failure == "missing_postburn":
        del chat["postburn_subtitle_audio_review"]
        del chat["final_review_audit"]["postburn_subtitle_audio_review"]
    elif failure == "invalid_disclosure":
        chat["final_review_audit"]["unresolved_findings_disclosed"] = [{"decision": "PROPOSED"}]
    elif failure == "findings_count":
        chat["final_review_audit"]["validated_finding_count"] = 0
    else:
        chat["exact_final_cpa_self_heal"] = {"status": "PASS", "final_srt_sha256": "sha256:" + "0" * 64}
    expected_error = ClipContextError if failure == "context_drift" else pipeline.CurrentTextContinuationError
    with pytest.raises(expected_error):
        pipeline._continuation_validate_existing_authority(
            chat, srt_text=_srt(), srt_sha256=srt_sha,
            clip_context=context, candidate_id="relationship-candidate",
        )


def test_existing_owner_freeze_is_the_real_consumer(tmp_path):
    chat, _context, _srt_sha = _valid_authority(tmp_path)
    spec = {
        "pieces": [{"start_ms": 0, "end_ms": 800, "role": "content"}],
        "semantic_end_ms": 800,
        "given_end_ms": 800,
        "given_end_mode": "exact_source_pin",
        "human_truth_mode": "withheld",
        "boundary_repair_extend_cap_ms": 30_000,
    }
    target_ms, scope = freeze_required_boundary_owner_contract(
        spec=spec,
        durations=[800],
        chat_authority_audit=chat,
        required_boundary_owners=[],
    )
    assert target_ms == 800
    assert scope["status"] == "PASS"
    assert chat["frozen_boundary_owner_contract"]["status"] == "FROZEN"


def _search_extension_contract(cap=30_000, **changes):
    spec = {
        "pieces": [{"start_ms": 0, "end_ms": 223_770, "role": "content"}],
        "semantic_start_ms": 10_880,
        "semantic_end_ms": 159_140,
        "boundary_repair_extend_cap_ms": cap,
        **changes,
    }
    audit = {}
    owners = changes.pop("required_boundary_owners", [])
    freeze_required_boundary_owner_contract(
        spec=spec, durations=[223_770], chat_authority_audit=audit,
        required_boundary_owners=owners,
    )
    return audit["frozen_boundary_owner_contract"]


def test_current_search_extension_preserves_owners_and_requires_real_witness_reserve():
    before = _search_extension_contract()
    after = _search_extension_contract(49_630)
    originals = copy.deepcopy((before, after))
    contract, receipt = pipeline._continuation_boundary_owner_contract(
        before, after, padded_dur=223_770,
    )
    assert contract == after
    assert contract["boundary_search_scope"]["max_recommended_end_ms"] == 208_770
    assert receipt["required_local_source_context_end_ms"] == 223_770
    assert receipt["prior_semantic_verdict_reused"] is False
    assert receipt["owners_unchanged"] is True
    assert (before, after) == originals
    with pytest.raises(pipeline.CurrentTextContinuationError, match="OWNER_SET_DRIFT"):
        pipeline._continuation_boundary_owner_contract(before, after, padded_dur=208_770)


def test_identical_current_search_contract_is_replayed_without_extension():
    frozen = _search_extension_contract()
    contract, receipt = pipeline._continuation_boundary_owner_contract(
        frozen, copy.deepcopy(frozen), padded_dur=223_770,
    )
    assert contract == frozen
    assert receipt is None


@pytest.mark.parametrize("invalid", [None, "receipt", "window", "timing"])
def test_text_supersession_retains_only_its_bound_frozen_source_geometry(invalid):
    spec = {
        "pieces": [{"start_ms": 0, "end_ms": 223_770}],
        "semantic_start_ms": 10_880, "semantic_end_ms": 159_140,
    }
    repair = {"cue_index": 2, "before": "旧字", "after": "新字"}
    audit = {"entity_repairs": [{
        "matched_start_ms": 123_180, "matched_end_ms": 129_140,
        "before": ["旧字"], "after": ["新字"],
    }]}
    freeze_required_boundary_owner_contract(
        spec=copy.deepcopy(spec), durations=[223_770],
        chat_authority_audit=audit, required_boundary_owners=[],
    )
    frozen = copy.deepcopy(audit["frozen_boundary_owner_contract"])
    audit["exact_final_cpa_self_heal"] = {"passes": [{"repairs": [repair]}]}
    row = audit["entity_repairs"][0]
    row["reconciliation"] = {
        "schema_version": "exact-final-cpa-supersession.v1",
        "status": "SUPERSEDED_BY_EXACT_FINAL_CPA", "timing_immutable": True,
        "exact_final_repair_sha256": "sha256:" + _sha(json.dumps(
            repair, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()),
    }
    if invalid == "receipt":
        row["reconciliation"]["exact_final_repair_sha256"] = "sha256:" + "0" * 64
    elif invalid == "window":
        row["matched_end_ms"] += 1
    elif invalid == "timing":
        row["reconciliation"]["timing_immutable"] = False
    original = copy.deepcopy(audit)
    evidence = copy.deepcopy(audit)
    pipeline._continuation_retain_superseded_boundary_geometry(evidence)
    freeze_required_boundary_owner_contract(
        spec=copy.deepcopy(spec), durations=[223_770],
        chat_authority_audit=evidence, required_boundary_owners=[],
    )
    assert audit == original  # Text/reconciliation authority is preserved.
    if invalid is None:
        actual, extension = pipeline._continuation_boundary_owner_contract(
            frozen, evidence["frozen_boundary_owner_contract"], padded_dur=223_770,
        )
        assert actual == frozen and extension is None
        assert actual["required_owner_count"] == 1
    else:
        with pytest.raises(pipeline.CurrentTextContinuationError, match="OWNER_SET_DRIFT"):
            pipeline._continuation_boundary_owner_contract(
                frozen, evidence["frozen_boundary_owner_contract"], padded_dur=223_770,
            )


@pytest.mark.parametrize("change", [
    {"semantic_end_ms": 158_000},
    {"given_end_ms": 160_000},
    {"semantic_tail_trim_cap_ms": 1_000},
    {"pieces": [{"start_ms": 1_000, "end_ms": 224_770, "role": "content"}]},
    {"required_boundary_owners": [{
        "owner_kind": "source_subtitle_truth", "owner_id": "new-owner", "required": True,
        "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
    }]},
])
def test_search_extension_rejects_changed_anchor_floor_clock_or_owner(change):
    before = _search_extension_contract()
    after = _search_extension_contract(49_630, **change)
    with pytest.raises(pipeline.CurrentTextContinuationError, match="OWNER_SET_DRIFT"):
        pipeline._continuation_boundary_owner_contract(before, after, padded_dur=223_770)


def test_current_search_extension_cannot_enlarge_an_exact_delivery_pin():
    # A normal exact pin's derived ceiling stays fixed even if its search cap
    # changes. It must never acquire the ordinary context-extension route.
    before = _search_extension_contract(given_end_mode="exact_source_pin", given_end_ms=159_140)
    after = _search_extension_contract(49_630, given_end_mode="exact_source_pin", given_end_ms=159_140)
    with pytest.raises(pipeline.CurrentTextContinuationError, match="OWNER_SET_DRIFT"):
        pipeline._continuation_boundary_owner_contract(before, after, padded_dur=223_770)


def test_current_search_extension_does_not_narrow_the_existing_search():
    before = _search_extension_contract()
    after = _search_extension_contract(20_000)
    with pytest.raises(pipeline.CurrentTextContinuationError, match="OWNER_SET_DRIFT"):
        pipeline._continuation_boundary_owner_contract(before, after, padded_dur=223_770)


def test_current_search_extension_rejects_tampered_contract():
    before = _search_extension_contract()
    after = _search_extension_contract(49_630)
    after["boundary_search_scope"]["repair_cap_ms"] = 50_000
    with pytest.raises(pipeline.CurrentTextContinuationError, match="CONTRACT_INVALID"):
        pipeline._continuation_boundary_owner_contract(before, after, padded_dur=223_770)


def _changed_repair(*, choice: str = "PROPOSED", check_request_sha: str = "a" * 64):
    return {
        "cue_indexes": [1],
        "before": ["旧字幕"],
        "after": ["新字幕"],
        "action": "REPLACE_CUE_TEXT",
        "request_sha256": "a" * 64,
        "decision_authority": "CPA_JUDGE",
        "timing_immutable": True,
        "judge": {
            "status": "JUDGED",
            "choice": choice,
            "check_request_sha256": check_request_sha,
        },
        "mutation_authority": {
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "PASS",
        },
    }


def _typed_drop() -> dict:
    effective = "a" * 64
    original = "b" * 64
    witness = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "status": "OBSERVED",
        "target_audible": False,
        "request_sha256": effective,
    }
    judge = {
        "status": "JUDGED",
        "choice": "DROP",
        "decision_contract": "inaudible-current-proposed-drop.v1",
        "choice_set": ["CURRENT", "PROPOSED", "DROP"],
        "check_request_sha256": original,
        "prompt_sha256": effective,
        "completion_sha256": effective,
    }
    return {
        "action": "DROP_CUE",
        "repair_class": "acoustic_drop_cue",
        "policy_branch": "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE",
        "decision_authority": "CPA_JUDGE",
        "cue_indexes": [44],
        "before": ["咳咳"],
        "after": [""],
        "request_sha256": effective,
        "timing_immutable": True,
        "acoustic_witness": witness,
        "judge": judge,
        "drop_authority": {
            "schema_version": "subtitle-cpa-inaudible-drop-authority.v1",
            "status": "PASS",
            "decision_authority": "CPA_JUDGE",
            "choice": "DROP",
            "decision_contract": "inaudible-current-proposed-drop.v1",
            "target_audible": False,
            "timing_immutable": True,
            "original_request_sha256": original,
            "effective_drop_request_sha256": effective,
            "original_witness_request_sha256": effective,
            "judge_prompt_sha256": effective,
            "judge_completion_sha256": effective,
        },
        "mutation_authority": {
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "PASS",
            "basis": "CPA_EXPLICIT_INAUDIBLE_DROP",
        },
    }


def test_existing_typed_drop_keeps_original_and_effective_request_bindings(tmp_path):
    chat, context, srt_sha = _valid_authority(tmp_path)
    chat["entity_repairs"] = [_typed_drop()]
    pipeline._continuation_validate_existing_authority(
        chat,
        srt_text=_srt(),
        srt_sha256=srt_sha,
        clip_context=context,
        candidate_id="relationship-candidate",
    )
    chat["entity_repairs"][0]["drop_authority"]["original_request_sha256"] = (
        "a" * 64
    )
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="DROP_AUTHORITY_INVALID",
    ):
        pipeline._continuation_validate_existing_authority(
            chat,
            srt_text=_srt(),
            srt_sha256=srt_sha,
            clip_context=context,
            candidate_id="relationship-candidate",
        )


def test_response_choice_cannot_disagree_with_existing_typed_row(tmp_path):
    chat, context, srt_sha = _valid_authority(tmp_path)
    chat["entity_repairs"] = [_changed_repair(choice="CURRENT")]
    with pytest.raises(pipeline.CurrentTextContinuationError, match="CHOICE_MISMATCH"):
        pipeline._continuation_validate_existing_authority(
            chat,
            srt_text=_srt(),
            srt_sha256=srt_sha,
            clip_context=context,
            candidate_id="relationship-candidate",
        )


@pytest.mark.parametrize("selected,applied", [
    ("Yeah yeah.", "Yes."),
    ("羡慕三分刚峰", "羡慕三丰干部。"),
])
def test_historical_native_candidate_must_match_applied_text(selected, applied):
    row = _changed_repair()
    row["after"] = [applied]
    row["judge"].update(
        decision_contract="current-proposed-candidate-id-neither.v1",
        candidate_id="SOURCE_1", selected_candidate_text=selected,
    )
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_ENTITY_REPAIR_SELECTED_TRANSCRIPT_MISMATCH",
    ):
        pipeline._continuation_validate_existing_repair(
            row, srt_text=_srt(), clip_context={},
        )
    row["after"] = [selected]
    pipeline._continuation_validate_existing_repair(
        row, srt_text=_srt(), clip_context={},
    )


def test_request_binding_drift_is_rejected_without_row_fallback(tmp_path):
    chat, context, srt_sha = _valid_authority(tmp_path)
    chat["entity_repairs"] = [_changed_repair(check_request_sha="b" * 64)]
    with pytest.raises(pipeline.CurrentTextContinuationError, match="REQUEST_DRIFT"):
        pipeline._continuation_validate_existing_authority(
            chat,
            srt_text=_srt(),
            srt_sha256=srt_sha,
            clip_context=context,
            candidate_id="relationship-candidate",
        )


def test_private_or_missing_review_artifact_is_not_an_authority(tmp_path):
    chat, context, srt_sha = _valid_authority(tmp_path)
    del chat["final_review_audit"]
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="FINAL_REVIEW_AUDIT_MISSING",
    ):
        pipeline._continuation_validate_existing_authority(
            chat,
            srt_text=_srt(),
            srt_sha256=srt_sha,
            clip_context=context,
            candidate_id="relationship-candidate",
        )


def _boundary_binding(cues, *, source_start_ms=59_740):
    delivery_end_ms = cues[-1].end_ms
    delivery_grid = pipeline.cue_grid_sha256(cues)
    return {
        "boundary_semantic_review": {
            "final_endpoint_binding": {
                "status": "PASS",
                "final_start_ms": 0,
                "final_end_ms": delivery_end_ms,
                "semantic_cue_grid_sha256": delivery_grid,
                "final_cue_grid_sha256": delivery_grid,
            },
            "source_separation_witness": {
                "status": "PASS",
                "source_final_start_ms": source_start_ms,
                "source_final_end_ms": source_start_ms + delivery_end_ms,
                "source_request_sha256": "sha256:" + "1" * 64,
                "source_review_sha256": "sha256:" + "2" * 64,
            },
        }
    }


def test_delivery_text_is_projected_into_bound_source_domain_without_rebinding_text():
    cues = pipeline.parse_srt_cues(
        "1\n00:00:00,250 --> 00:00:03,070\n我好怀念\n\n"
        "2\n00:00:04,000 --> 00:00:05,000\n亲密\n"
    )
    projected, binding = pipeline._continuation_project_delivery_to_source(
        cues,
        final_review_audit=_boundary_binding(cues),
    )
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in projected] == [
        (59_990, 62_810, "我好怀念"),
        (63_740, 64_740, "亲密"),
    ]
    assert binding["offset_ms"] == 59_740
    assert binding["source_first_start_ms"] == 59_990
    assert binding["source_final_end_ms"] == 64_740
    assert binding["delivery_srt_role"] == "CURRENT_FINAL_DELIVERY_TEXT"


def test_delivery_source_projection_rejects_stale_current_grid():
    cues = pipeline.parse_srt_cues(
        "1\n00:00:00,250 --> 00:00:03,070\n我好怀念\n"
    )
    audit = _boundary_binding(cues)
    audit["boundary_semantic_review"]["final_endpoint_binding"][
        "final_cue_grid_sha256"
    ] = "sha256:" + "f" * 64
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="DELIVERY_GRID_DRIFT",
    ):
        pipeline._continuation_project_delivery_to_source(cues, final_review_audit=audit)


def test_source_boundary_uses_normal_post_transcript_grid_and_keeps_old_context_evidence():
    source_srt = (
        "1\n00:00:00,000 --> 00:00:01,000\n旧源\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n后续\n"
    )
    historical_srt = "1\n00:00:00,000 --> 00:00:01,000\n历史\n"
    source_cues = pipeline.parse_srt_cues(source_srt)
    audit = {
        "boundary_semantic_review": {
            "source_separation_witness": {
                "source_cue_grid_sha256": pipeline.cue_grid_sha256(source_cues),
            }
        }
    }
    chat = {
        "post_transcript_entity_output_srt": source_srt,
        "post_transcript_entity_output_srt_sha256": "sha256:" + _sha(source_srt.encode()),
    }
    context = {
        "whole_clip_draft_srt": historical_srt,
        "whole_clip_draft_srt_sha256": "sha256:" + _sha(historical_srt.encode()),
    }
    cues, binding = pipeline._continuation_source_boundary_cues(
        chat_authority=chat,
        clip_context=context,
        final_review_audit=audit,
        padded_dur=4_000,
    )
    assert [cue.text for cue in cues] == ["旧源", "后续"]
    assert binding["source_srt_sha256"] == chat["post_transcript_entity_output_srt_sha256"]
    assert binding["historical_clip_context_srt_sha256"] == context[
        "whole_clip_draft_srt_sha256"
    ]
    assert binding["historical_role"] == "EVIDENCE_ONLY_PREVIOUS_TRANSCRIPT"


def test_source_boundary_rejects_post_transcript_grid_drift():
    source_srt = "1\n00:00:00,000 --> 00:00:01,000\n源\n"
    chat = {
        "post_transcript_entity_output_srt": source_srt,
        "post_transcript_entity_output_srt_sha256": "sha256:" + _sha(source_srt.encode()),
    }
    context = {
        "whole_clip_draft_srt": source_srt,
        "whole_clip_draft_srt_sha256": "sha256:" + _sha(source_srt.encode()),
    }
    audit = {
        "boundary_semantic_review": {
            "source_separation_witness": {
                "source_cue_grid_sha256": "sha256:" + "0" * 64,
            }
        }
    }
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="SOURCE_BOUNDARY_GRID_DRIFT",
    ):
        pipeline._continuation_source_boundary_cues(
            chat_authority=chat,
            clip_context=context,
            final_review_audit=audit,
            padded_dur=2_000,
        )


def _executor_draft_inputs(tmp_path):
    import dataclasses
    import json
    chat, context, audit, path, _, _ = _explicit_source_boundary_inputs(tmp_path)
    audit["boundary_semantic_review"]["source_separation_witness"]["source_cue_grid_sha256"] = pipeline.cue_grid_sha256(
        pipeline.parse_srt_cues(chat["post_transcript_entity_output_srt"])
    )
    raw = chat["post_transcript_entity_output_srt"] + "\n3\n00:00:04,000 --> 00:00:05,000\n下一条原始语境\n"
    path.write_text(raw)
    source_sha = "sha256:" + "a" * 64
    job = {
        "schema_version": "source-context-jingting-job.v1", "job_kind": "SOURCE_CONTEXT_JINGTING",
        "candidate_id": "candidate", "job_id": "scj_existing", "source_offset_ms": 10_000,
        "timeline": {"context_start_ms": 10_000, "context_end_ms": 16_000, "context_duration_ms": 6_000},
        "input": {"source_offset_ms": 10_000, "duration_ms": 6_000, "source_uri": "/existing-source.mp4", "source_sha256": source_sha},
        "provenance": {"source_uri": "/existing-source.mp4", "source_sha256": source_sha},
    }
    manifest = {
        "schema_version": "jingting-source-context-result.v1", "job_id": "scj_existing",
        "provider": "source_draft_context", "refinement_required": False,
        "agy_rc": 0, "provider_fallback_used": False, "source_offset_ms": 10_000,
        "source_sha256": source_sha, "input_sha256": "sha256:" + _sha(raw.encode()),
        "output_sha256": "sha256:" + _sha(raw.encode()),
    }
    job_path, manifest_path = tmp_path / "job.json", tmp_path / "manifest.json"
    job_path.write_text(json.dumps(job))
    manifest_path.write_text(json.dumps(manifest))
    continuation = pipeline.CurrentTextContinuation(
        current_srt_path=tmp_path / "current.srt", chat_authority_path=tmp_path / "chat.json",
        clip_context_path=tmp_path / "context.json", padded_sha256="b" * 64,
        source_media_sha256s=(source_sha,), spans=[], transcriber=lambda: None,
        source_boundary_srt_path=path, expected_source_boundary_srt_sha256="sha256:" + _sha(raw.encode()),
    )
    continuation = dataclasses.replace(
        continuation, source_context_job_path=job_path,
        expected_source_context_job_sha256="sha256:" + _sha(job_path.read_bytes()),
        source_context_manifest_path=manifest_path,
        expected_source_context_manifest_sha256="sha256:" + _sha(manifest_path.read_bytes()),
    )
    spec = {"pieces": [{"start_ms": 10_000, "end_ms": 16_000, "remote_media": "/existing-source.mp4", "source_media_sha256": source_sha}]}
    return continuation, spec, chat, context, audit


def test_executor_expanded_draft_supplies_new_cues_without_inheriting_old_witness(tmp_path):
    continuation, spec, chat, context, audit = _executor_draft_inputs(tmp_path)
    original = copy.deepcopy((chat, context, audit))
    binding = pipeline._continuation_source_context_draft_binding(
        continuation, spec=spec, candidate_id="candidate", padded_dur=6_000,
        source_media_sha256s=continuation.source_media_sha256s,
    )
    cues, receipt = pipeline._continuation_source_boundary_cues(
        chat_authority=chat, clip_context=context, final_review_audit=audit, padded_dur=6_000,
        source_boundary_srt_path=continuation.source_boundary_srt_path,
        expected_source_boundary_srt_sha256=continuation.expected_source_boundary_srt_sha256,
        source_context_binding=binding,
    )
    assert cues[-1].text == "下一条原始语境"
    assert receipt["fresh_source_review_required"] is True
    assert receipt["source_witness_role"] == "HISTORICAL_GRID_ONLY"
    assert binding["prior_boundary_pass_inherited"] is False
    assert (chat, context, audit) == original


@pytest.mark.parametrize("failure", ["missing_pair", "candidate", "clock", "source", "draft", "refined_authority", "artifact_bytes", "old_witness"])
def test_executor_expanded_draft_rejects_drift_and_cannot_replace_old_authority(tmp_path, failure):
    import dataclasses
    import json
    continuation, spec, chat, context, audit = _executor_draft_inputs(tmp_path)
    if failure == "missing_pair":
        continuation = dataclasses.replace(continuation, expected_source_context_job_sha256=None)
    elif failure == "artifact_bytes":
        continuation.source_context_manifest_path.write_text("{}")
    elif failure == "old_witness":
        audit["boundary_semantic_review"]["source_separation_witness"]["source_cue_grid_sha256"] = "sha256:" + "e" * 64
    else:
        job = json.loads(continuation.source_context_job_path.read_text())
        manifest = json.loads(continuation.source_context_manifest_path.read_text())
        if failure == "candidate":
            job["candidate_id"] = "different"
        elif failure == "clock":
            job["timeline"]["context_start_ms"] += 1
        elif failure == "source":
            manifest["source_sha256"] = "sha256:" + "c" * 64
        elif failure == "draft":
            manifest["output_sha256"] = "sha256:" + "d" * 64
        else:
            manifest["refinement_required"] = True
        continuation.source_context_job_path.write_text(json.dumps(job))
        continuation.source_context_manifest_path.write_text(json.dumps(manifest))
        continuation = dataclasses.replace(
            continuation,
            expected_source_context_job_sha256="sha256:" + _sha(continuation.source_context_job_path.read_bytes()),
            expected_source_context_manifest_sha256="sha256:" + _sha(continuation.source_context_manifest_path.read_bytes()),
        )
    with pytest.raises(pipeline.CurrentTextContinuationError):
        binding = pipeline._continuation_source_context_draft_binding(
            continuation, spec=spec, candidate_id="candidate", padded_dur=6_000,
            source_media_sha256s=continuation.source_media_sha256s,
        )
        pipeline._continuation_source_boundary_cues(
            chat_authority=chat, clip_context=context, final_review_audit=audit, padded_dur=6_000,
            source_boundary_srt_path=continuation.source_boundary_srt_path,
            expected_source_boundary_srt_sha256=continuation.expected_source_boundary_srt_sha256,
            source_context_binding=binding,
        )


def _explicit_source_boundary_inputs(tmp_path):
    historical_srt = "1\n00:00:00,000 --> 00:00:01,000\n历史上下文\n"
    old_source_srt = (
        "1\n00:00:00,000 --> 00:00:01,000\n旧源55\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n旧源尾\n"
    )
    expanded_srt = (
        "1\n00:00:00,000 --> 00:00:01,000\n扩展源\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n完整源\n"
    )
    source_path = tmp_path / "expanded-source.srt"
    source_path.write_text(expanded_srt, encoding="utf-8")
    source_cues = pipeline.parse_srt_cues(expanded_srt)
    audit = {
        "boundary_semantic_review": {
            "source_separation_witness": {
                "source_cue_grid_sha256": pipeline.cue_grid_sha256(source_cues),
                "source_request_sha256": "sha256:" + "1" * 64,
                "source_review_sha256": "sha256:" + "2" * 64,
            }
        }
    }
    chat = {
        "post_transcript_entity_output_srt": old_source_srt,
        "post_transcript_entity_output_srt_sha256": "sha256:" + _sha(old_source_srt.encode()),
    }
    context = {
        "whole_clip_draft_srt": historical_srt,
        "whole_clip_draft_srt_sha256": "sha256:" + _sha(historical_srt.encode()),
    }
    expected = "sha256:" + _sha(source_path.read_bytes())
    return chat, context, audit, source_path, expected, expanded_srt


def test_source_boundary_explicit_artifact_uses_bound_expanded_grid_and_keeps_old_source(tmp_path):
    chat, context, audit, source_path, expected, expanded_srt = _explicit_source_boundary_inputs(tmp_path)
    old_source = chat["post_transcript_entity_output_srt"]
    cues, binding = pipeline._continuation_source_boundary_cues(
        chat_authority=chat,
        clip_context=context,
        final_review_audit=audit,
        padded_dur=4_000,
        source_boundary_srt_path=source_path,
        expected_source_boundary_srt_sha256=expected,
    )
    assert [cue.text for cue in cues] == ["扩展源", "完整源"]
    assert chat["post_transcript_entity_output_srt"] == old_source
    assert binding["source_srt_role"] == "EXPLICIT_SOURCE_BOUNDARY_ARTIFACT"
    assert binding["source_srt_path"] == str(source_path)
    assert binding["source_srt_sha256"] == expected
    assert binding["source_cue_grid_sha256"] == pipeline.cue_grid_sha256(
        pipeline.parse_srt_cues(expanded_srt)
    )
    assert binding["source_witness_request_sha256"] == "sha256:" + "1" * 64
    assert binding["source_witness_review_sha256"] == "sha256:" + "2" * 64
    assert binding["historical_clip_context_srt_sha256"] == context[
        "whole_clip_draft_srt_sha256"
    ]


@pytest.mark.parametrize(
    "failure",
    ["missing_path", "missing_sha", "wrong_hash", "wrong_grid", "text_drift", "text_drift_rehashed", "out_of_window"],
)
def test_source_boundary_explicit_artifact_rejects_unbound_or_drifted_input(tmp_path, failure):
    chat, context, audit, source_path, expected, expanded_srt = _explicit_source_boundary_inputs(tmp_path)
    path_arg = source_path
    expected_arg = expected
    if failure == "missing_path":
        path_arg = None
    elif failure == "missing_sha":
        expected_arg = None
    elif failure == "wrong_hash":
        expected_arg = "sha256:" + "0" * 64
    elif failure == "wrong_grid":
        audit["boundary_semantic_review"]["source_separation_witness"][
            "source_cue_grid_sha256"
        ] = "sha256:" + "f" * 64
    elif failure in {"text_drift", "text_drift_rehashed"}:
        source_path.write_text(expanded_srt.replace("扩展源", "文本漂移"), encoding="utf-8")
        if failure == "text_drift_rehashed":
            expected_arg = "sha256:" + _sha(source_path.read_bytes())
    else:
        out_of_window = expanded_srt.replace("00:00:03,000", "00:00:05,001")
        source_path.write_text(out_of_window, encoding="utf-8")
        expected_arg = "sha256:" + _sha(out_of_window.encode())
    expected_error = {
        "missing_path": "ARTIFACT_PAIR_INVALID",
        "missing_sha": "ARTIFACT_PAIR_INVALID",
        "wrong_hash": "ARTIFACT_DRIFT",
        "wrong_grid": "SOURCE_BOUNDARY_GRID_DRIFT",
        "text_drift": "ARTIFACT_DRIFT",
        "text_drift_rehashed": "SOURCE_BOUNDARY_GRID_DRIFT",
        "out_of_window": "SOURCE_BOUNDARY_SRT_INVALID",
    }[failure]
    with pytest.raises(pipeline.CurrentTextContinuationError, match=expected_error):
        pipeline._continuation_source_boundary_cues(
            chat_authority=chat,
            clip_context=context,
            final_review_audit=audit,
            padded_dur=4_000,
            source_boundary_srt_path=path_arg,
            expected_source_boundary_srt_sha256=expected_arg,
        )


@pytest.mark.parametrize("pending_clock", [False, True])
def test_continuation_returns_projected_cues_and_persists_final_review_contract(
    tmp_path, monkeypatch, pending_clock
):
    current_srt = (
        "1\n00:00:00,250 --> 00:00:03,070\n我好怀念\n\n"
        "2\n00:00:04,000 --> 00:00:05,000\n亲密\n"
    )
    current_srt_path = tmp_path / "current.srt"
    current_srt_path.write_text(current_srt, encoding="utf-8")
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded-fixture")
    chat_path = tmp_path / "chat.json"
    chat_path.write_text(json.dumps({"entity_repairs": [], "final_output_srt_sha256": _sha(current_srt.encode())}), encoding="utf-8")
    original_chat_bytes = chat_path.read_bytes()
    context_path = tmp_path / "context.json"
    context_path.write_text("{}", encoding="utf-8")
    original_context_bytes = context_path.read_bytes()
    source_boundary_path = tmp_path / "source-boundary.srt"
    source_boundary_path.write_text("source boundary", encoding="utf-8")
    source_boundary_sha = "sha256:" + _sha(source_boundary_path.read_bytes())
    out_root = tmp_path / "out"
    cues = pipeline.parse_srt_cues(current_srt)
    final_review = _boundary_binding(cues)
    final_review["schema_version"] = "final-review-audit.v2"
    final_review["status"] = "PASS"
    final_review["correction_pass"] = {
        "schema_version": "final-review-audit.v1",
        "status": "APPLIED",
        "applied_count": 0,
        "findings": [],
        "boundary_semantic_review": {"status": "HISTORICAL"},
    }
    clock_review = copy.deepcopy(final_review)
    if pending_clock:
        final_review.update(release_gate="BLOCK", findings=[])
        final_review["boundary_semantic_review"] = {"status": "BLOCK"}
        monkeypatch.setattr(
            pipeline, "_continuation_source_clock_reference",
            lambda *args, **kwargs: (clock_review, {"role": "EVIDENCE_ONLY_SOURCE_CLOCK"}),
        )

    monkeypatch.setattr(
        pipeline,
        "_continuation_validate_existing_authority",
        lambda *args, **kwargs: final_review,
    )
    monkeypatch.setattr(
        pipeline,
        "validate_clip_context",
        lambda context, **kwargs: context,
    )
    source_boundary_calls = []

    def source_boundary(**kwargs):
        source_boundary_calls.append(kwargs)
        return cues, {"status": "PASS", "source_cue_count": len(cues)}

    monkeypatch.setattr(pipeline, "_continuation_source_boundary_cues", source_boundary)
    monkeypatch.setattr(pipeline, "_continuation_chat_from_context", lambda context: ())
    monkeypatch.setattr(pipeline, "ledger_required_owner_contracts", lambda **kwargs: [])
    monkeypatch.setattr(pipeline, "_redelivery_baseline_boundary_owner", lambda spec, durations: [])

    def fake_freeze(*, chat_authority_audit, **kwargs):
        scope = {
            "status": "PASS",
            "semantic_target_ms": 5_000,
            "recommendation_forward_ms": 0,
            "max_recommended_end_ms": 100_000,
        }
        chat_authority_audit["frozen_boundary_owner_contract"] = {
            "status": "FROZEN",
            "owner_set_sha256": "sha256:" + "3" * 64,
            "required_owner_count": 0,
            "owner_eligibility_scope": {"required_owner_count": 0},
            "boundary_search_scope": scope,
            "owners": [],
        }
        return 5_000, scope

    monkeypatch.setattr(pipeline, "freeze_required_boundary_owner_contract", fake_freeze)
    monkeypatch.setattr(
        pipeline,
        "review_source_boundary",
        lambda **kwargs: (None, {"status": "BLOCK", "reason": "FINAL_REVIEW_DISABLED"}, {}, None),
    )
    monkeypatch.setattr(pipeline, "build_env_screen_read_probe", lambda _padded, *, prior_observations=None: None)
    verifier_context_calls = []
    def normal_verifier(_request):
        return None

    def normal_verifier_context(**kwargs):
        verifier_context_calls.append(kwargs)
        return SimpleNamespace(verify_confusable_entity=normal_verifier)

    monkeypatch.setattr(pipeline, "_build_entity_verification_context", normal_verifier_context)

    adapters = pipeline.TextPipelineAdapters(
        build_aggregate_transcriber=lambda: None,
        build_agy_transcriber=lambda: None,
        load_term_boundary_surfaces=lambda _spec: [],
        profile_asset_file=lambda name: tmp_path / name,
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=lambda: tmp_path / "topic.json",
        topic_graph_expected_sha256=lambda: "",
    )
    continuation = pipeline.CurrentTextContinuation(
        current_srt_path=current_srt_path,
        chat_authority_path=chat_path,
        clip_context_path=context_path,
        padded_sha256="sha256:" + _sha(padded.read_bytes()),
        source_media_sha256s=("sha256:" + "a" * 64,),
        spans=[SimpleNamespace(start_ms=0, end_ms=100)],
        transcriber=lambda *_args: "",
        source_boundary_srt_path=source_boundary_path,
        expected_source_boundary_srt_sha256=source_boundary_sha,
    )
    result = pipeline.run_text_pipeline(
        spec={
            "date": "2026-10-04",
            "human_truth_mode": "withheld",
            "pieces": [{"source_media_sha256": "sha256:" + "a" * 64}],
        },
        durations=[100_000],
        padded=padded,
        padded_dur=100_000,
        host="oci3",
        text_override_path=None,
        cid="relationship-candidate",
        out_root=out_root,
        substrate="existing_srt",
        correct="none",
        screen_text=False,
        adapters=adapters,
        continuation=continuation,
    )

    assert result.srt_text == current_srt
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues] == [
        (59_990, 62_810, "我好怀念"),
        (63_740, 64_740, "亲密"),
    ]
    assert result.chat_authority_audit["final_review_audit"] is final_review
    assert result.boundary_context_cues is not None
    assert result.boundary_context_cues != result.cues
    assert source_boundary_calls[0]["source_boundary_srt_path"] == source_boundary_path
    assert source_boundary_calls[0]["expected_source_boundary_srt_sha256"] == source_boundary_sha
    if pending_clock:
        assert source_boundary_calls[0]["final_review_audit"] is clock_review
        assert final_review["boundary_semantic_review"]["status"] == "BLOCK"
        assert final_review["boundary_semantic_review"]["reason"] == "FINAL_REVIEW_DISABLED"
        assert final_review["source_boundary_context"]["clock_reference"]["role"] == "EVIDENCE_ONLY_SOURCE_CLOCK"
    assert verifier_context_calls[0]["host"] == "oci3"
    assert verifier_context_calls[0]["srt_text"] == current_srt
    captured = {}

    def exact_boundary(**kwargs):
        captured["correction"] = kwargs["correction_audit"]
        return kwargs["correction_audit"]

    monkeypatch.setattr(pipeline, "exact_delivery_correction_audit", exact_boundary)
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: None)
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _context: "")
    discovery_calls = []
    microcue_row = {"cue": 2, "reason": "blind acoustic discrepancy"}
    postburn_row = {"cue": 1, "reason": "unresolved semantic reading"}

    def priority_discovery(srt_text, **kwargs):
        discovery_calls.append((srt_text, kwargs))
        return [microcue_row], {"status": "OBSERVED"}

    monkeypatch.setattr(pipeline, "discover_priority_findings", priority_discovery)
    monkeypatch.setattr(
        pipeline, "validated_postburn_priority_findings",
        lambda **kwargs: ([postburn_row], "sha256:" + "4" * 64),
    )
    priority_batches = []
    def exact_review(**kwargs):
        assert kwargs["verify_confusable_entity"] is normal_verifier
        priority_batches.append(kwargs["priority_raw_findings"])
        assert kwargs["acoustic_discovery_audit"] == {"status": "OBSERVED"}
        return pipeline.audit_correction_mutation_authority(kwargs["correction_audit"])

    monkeypatch.setattr(pipeline, "_run_exact_final_release_review", exact_review)
    monkeypatch.setattr(pipeline, "checkpoint_final_review_carryover", lambda *_args: None)
    verdict = result.review_exact_final_srt(current_srt, result.chat_authority_audit, 59_740, 64_740)
    assert verdict["status"] == "PASS"
    assert verdict["applied_count"] == 0
    assert captured["correction"]["schema_version"] == "final-review-audit.v1"
    assert captured["correction"]["boundary_semantic_review"]["status"] == "BLOCK"
    assert final_review["correction_pass"]["boundary_semantic_review"]["status"] == "HISTORICAL"
    assert priority_batches == [[microcue_row, postburn_row]]
    assert discovery_calls[0][0] == current_srt
    assert discovery_calls[0][1]["timeline_offset_ms"] == 59_740
    assert discovery_calls[0][1]["entity_verifier"] is normal_verifier

    # A bad historical count must still be rejected by the real consumer.
    final_review["correction_pass"]["applied_count"] = 1
    assert result.review_exact_final_srt(
        current_srt, result.chat_authority_audit, 59_740, 64_740
    )["status"] == "BLOCK"
    # The unchanged postburn request is not reintroduced on a second pass;
    # ordinary discovery still inspects the actual current delivery text.
    assert priority_batches[1] == [microcue_row]
    persisted_chat = json.loads(result.chat_authority_path.read_text(encoding="utf-8"))
    assert persisted_chat["final_review_audit"]["boundary_semantic_review"]["status"] == "BLOCK"
    display_audit = result.chat_authority_audit["current_caption_display_surface_audit"]
    assert display_audit["status"] == "NO_CHANGE"
    assert not {
        "prior_final_review_audit",
        "prior_source_clock_review",
        "prior_source_clock_reference",
        "prior_delivery_source_projection",
        "prior_source_boundary_context",
        "input_chat_authority_path",
        "input_chat_authority_sha256",
        "input_clip_context_path",
        "input_clip_context_sha256",
        "input_source_clock_authority_path",
        "input_source_clock_authority_sha256",
        "prior_final_review_summary",
        "prior_source_clock_review_summary",
    } & display_audit.keys()
    assert current_srt_path.read_text(encoding="utf-8") == current_srt
    assert chat_path.read_bytes() == original_chat_bytes
    assert context_path.read_bytes() == original_context_bytes


def test_repaired_current_projection_keeps_clock_without_rebinding_old_review():
    original = pipeline.parse_srt_cues("1\n00:00:00,000 --> 00:00:01,000\n原稿\n")
    repaired = pipeline.parse_srt_cues("1\n00:00:00,000 --> 00:00:01,000\n修复\n")
    audit = _boundary_binding(original)
    before = copy.deepcopy(audit)
    projected, receipt = pipeline._continuation_project_delivery_to_source(
        repaired, final_review_audit=audit, reference_cues=original,
    )
    assert projected[0].text == "修复"
    assert receipt["offset_ms"] == projected[0].start_ms
    assert audit == before
    drifted = pipeline.parse_srt_cues("1\n00:00:00,001 --> 00:00:01,000\n修复\n")
    with pytest.raises(pipeline.CurrentTextContinuationError, match="TIMING_DRIFT"):
        pipeline._continuation_project_delivery_to_source(
            drifted, final_review_audit=audit, reference_cues=original,
        )


@pytest.mark.parametrize("gate_failure", [None, "before_authority", "after_authority"])
@pytest.mark.parametrize("tail_gap_ms", [0, 280])
def test_current_continuation_consumes_caption_display_canon_before_fresh_review(
    tmp_path, monkeypatch, gate_failure, tail_gap_ms,
):
    """A registered display spelling reopens the actual current text consumer."""

    from src.autoslice import producer_package_finalization as finalization

    current_srt = (
        "1\n00:00:00,250 --> 00:00:03,070\n"
        "谢谢 Alice 的 SuperChat!\n\n"
        "2\n00:00:04,000 --> 00:00:05,000\n"
        "直女、mysuperchat 和 SuperChatGPT\n"
    )
    normalized_srt = (
        "1\n00:00:00,250 --> 00:00:03,070\n"
        "谢谢 Alice 的 SC!\n\n"
        "2\n00:00:04,000 --> 00:00:05,000\n"
        "直女、mysuperchat 和 SuperChatGPT\n"
    )
    current_srt_path = tmp_path / "current.srt"
    current_srt_path.write_text(current_srt, encoding="utf-8")
    original_current_bytes = current_srt_path.read_bytes()
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded-fixture")
    source_boundary_path = tmp_path / "source-boundary.srt"
    source_boundary_path.write_text("source boundary", encoding="utf-8")
    source_boundary_sha = "sha256:" + _sha(source_boundary_path.read_bytes())
    context = tmp_path / "context.json"
    context.write_text("{}", encoding="utf-8")
    original_context_bytes = context.read_bytes()
    cues = pipeline.parse_srt_cues(current_srt)
    final_review = {
        **_boundary_binding(cues),
        "schema_version": "final-review-audit.v2",
        "meaning_policy": "contextual-meaning-equivalence.v1",
        "status": "CLEAN",
        "release_gate": "PASS",
        "reviewed_srt_sha256": "sha256:" + _sha(original_current_bytes),
        "findings": [],
        "validated_finding_count": 0,
        "correction_pass": {
            "schema_version": "final-review-audit.v1",
            "status": "APPLIED",
            "applied_count": 0,
            "findings": [],
            "boundary_semantic_review": {"status": "HISTORICAL"},
        },
    }
    boundary = final_review["boundary_semantic_review"]
    boundary["final_endpoint_binding"]["final_end_ms"] += tail_gap_ms
    boundary["source_separation_witness"]["source_final_end_ms"] += tail_gap_ms
    chat = {
        "schema_version": "chat-authority-audit.v2",
        "final_output_srt_sha256": _sha(original_current_bytes),
        "mutation_authorized": False,
        "entity_repairs": [],
        "final_review_audit": final_review,
    }
    chat_path = tmp_path / "chat.json"
    chat_path.write_text(json.dumps(chat), encoding="utf-8")
    original_chat_bytes = chat_path.read_bytes()

    monkeypatch.setattr(
        pipeline,
        "_continuation_source_clock_reference",
        lambda *args, **kwargs: (None, None),
    )
    monkeypatch.setattr(
        pipeline,
        "_continuation_validate_existing_authority",
        lambda *args, **kwargs: final_review,
    )
    monkeypatch.setattr(pipeline, "validate_clip_context", lambda context, **kwargs: context)
    monkeypatch.setattr(pipeline, "_continuation_chat_from_context", lambda context: ())
    monkeypatch.setattr(pipeline, "ledger_required_owner_contracts", lambda **kwargs: [])
    monkeypatch.setattr(pipeline, "_redelivery_baseline_boundary_owner", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        pipeline,
        "_continuation_source_boundary_cues",
        lambda **kwargs: (cues, {"status": "PASS"}),
    )

    def fake_freeze(*, chat_authority_audit, **kwargs):
        scope = {
            "status": "PASS",
            "semantic_target_ms": 5_000,
            "recommendation_forward_ms": 0,
            "max_recommended_end_ms": 100_000,
        }
        chat_authority_audit["frozen_boundary_owner_contract"] = {
            "status": "FROZEN",
            "owner_set_sha256": "sha256:" + "3" * 64,
            "required_owner_count": 0,
            "owner_eligibility_scope": {"required_owner_count": 0},
            "boundary_search_scope": scope,
            "owners": [],
        }
        return 5_000, scope

    monkeypatch.setattr(pipeline, "freeze_required_boundary_owner_contract", fake_freeze)
    source_review_calls = []

    def fake_source_review(**kwargs):
        source_review_calls.append(kwargs)
        return None, {"status": "PASS"}, {}, None

    monkeypatch.setattr(pipeline, "review_source_boundary", fake_source_review)
    monkeypatch.setattr(pipeline, "build_env_screen_read_probe", lambda _padded, *, prior_observations=None: None)

    callback_reviews = []
    callback_offsets = []

    def fake_review_builder(**kwargs):
        def review(final_srt_text, _authority, _offset, _end):
            callback_reviews.append(final_srt_text)
            callback_offsets.append((_offset, _end))
            return {
                "reviewed_srt_sha256": "sha256:" + _sha(final_srt_text.encode()),
                "discovery": {"status": "COMPLETE"},
                "correction_mutation_authority": {
                    "schema_version": "subtitle-correction-mutation-audit.v1", "status": "PASS"},
                "schema_version": "final-review-audit.v2",
                "meaning_policy": "contextual-meaning-equivalence.v1",
                "status": "FLAGGED",
                "release_gate": "BLOCK",
                "reason_codes": ["FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"],
                "findings": [],
                "validated_finding_count": 0,
                "boundary_semantic_review": {
                    "schema_version": "talk-boundary-semantic-review.v1",
                    "status": "BLOCK",
                    "reason_codes": ["CURRENT_TEXT_REQUIRES_SOURCE_REVIEW"],
                },
                "correction_pass": copy.deepcopy(final_review["correction_pass"]),
            }

        return review

    monkeypatch.setattr(pipeline, "_build_exact_final_review_callback", fake_review_builder)
    gate_calls = []
    ordinary_gate = finalization._run_exact_final_review_gate

    def capture_gate(**kwargs):
        gate_calls.append(kwargs)
        if gate_failure == "before_authority":
            raise RuntimeError("INJECTED_GATE_FAILED_BEFORE_AUTHORITY")
        audit = ordinary_gate(**kwargs)
        if gate_failure == "after_authority":
            raise RuntimeError("INJECTED_GATE_FAILED_AFTER_SAVED_REVIEW")
        return audit

    monkeypatch.setattr(finalization, "_run_exact_final_review_gate", capture_gate)

    adapters = pipeline.TextPipelineAdapters(
        build_aggregate_transcriber=lambda: None,
        build_agy_transcriber=lambda: None,
        load_term_boundary_surfaces=lambda _spec: [],
        profile_asset_file=lambda name: tmp_path / name,
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=lambda: tmp_path / "topic.json",
        topic_graph_expected_sha256=lambda: "",
    )
    continuation = pipeline.CurrentTextContinuation(
        current_srt_path=current_srt_path,
        chat_authority_path=chat_path,
        clip_context_path=context,
        padded_sha256="sha256:" + _sha(padded.read_bytes()),
        source_media_sha256s=("sha256:" + "a" * 64,),
        spans=[SimpleNamespace(start_ms=0, end_ms=100)],
        transcriber=lambda *_args: "",
        source_boundary_srt_path=source_boundary_path,
        expected_source_boundary_srt_sha256=source_boundary_sha,
        verify_confusable_entity=lambda _request: None,
    )
    arguments = dict(
        spec={
            "date": "2026-10-04",
            "human_truth_mode": "withheld",
            "pieces": [{"source_media_sha256": "sha256:" + "a" * 64}],
        },
        durations=[100_000],
        padded=padded,
        padded_dur=100_000,
        host="oci3",
        text_override_path=None,
        cid="relationship-candidate",
        out_root=tmp_path / "out",
        substrate="existing_srt",
        correct="none",
        screen_text=False,
        adapters=adapters,
        continuation=continuation,
    )

    if gate_failure:
        with pytest.raises(RuntimeError, match="INJECTED_GATE_FAILED") as caught:
            pipeline.run_text_pipeline(**arguments)
        native = json.loads((tmp_path / "out/relationship-candidate.current-text-checkpoint.json").read_bytes())
        expected = current_srt if gate_failure == "before_authority" else normalized_srt
        frozen_srt = Path(native["srt"]["path"])
        frozen_authority = Path(native["authority"]["path"])
        before_authority = frozen_authority.read_bytes()
        assert frozen_srt.read_text() == expected
        if gate_failure == "before_authority":
            assert any("CURRENT_CHECKPOINT_NOT_ADVANCED" in note for note in caught.value.__notes__)
        recovery = tmp_path / "out/current-text-recovery"
        (recovery / "relationship-candidate.current.srt").write_text("next partial attempt")
        (recovery / "relationship-candidate.chat-authority.json").write_text("{}")
        assert frozen_srt.read_text() == expected
        assert frozen_authority.read_bytes() == before_authority
        assert current_srt_path.read_bytes() == original_current_bytes
        assert not source_review_calls
        return
    result = pipeline.run_text_pipeline(**arguments)

    assert result.srt_text == normalized_srt
    assert current_srt_path.read_bytes() == original_current_bytes
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in result.cues] == [
        (59_990, 62_810, "谢谢 Alice 的 SC!"),
        (63_740, 64_740, "直女、mysuperchat 和 SuperChatGPT"),
    ]
    display_audit = result.chat_authority_audit["current_caption_display_surface_audit"]
    assert display_audit["status"] == "APPLIED"
    assert display_audit["input_srt_sha256"] == _sha(original_current_bytes)
    assert display_audit["output_srt_sha256"] == _sha(normalized_srt.encode())
    assert display_audit["repairs"][0]["replacements"][0]["authority"] == (
        "lidousha-caption-display-canon.v1"
    )
    assert display_audit["input_srt_path"] == str(current_srt_path)
    assert display_audit["input_chat_authority_path"] == str(chat_path)
    assert display_audit["input_chat_authority_sha256"] == "sha256:" + _sha(original_chat_bytes)
    assert display_audit["input_clip_context_path"] == str(context)
    assert display_audit["input_clip_context_sha256"] == "sha256:" + _sha(original_context_bytes)
    assert display_audit["input_source_clock_authority_path"] is None
    assert display_audit["input_source_clock_authority_sha256"] is None
    assert display_audit["prior_final_review_summary"] == {
        "schema_version": "final-review-audit.v2",
        "status": "CLEAN",
        "release_gate": "PASS",
        "reviewed_srt_sha256": "sha256:" + _sha(original_current_bytes),
    }
    assert display_audit["prior_source_clock_review_summary"] is None
    assert not {
        "prior_final_review_audit",
        "prior_source_clock_review",
        "prior_source_clock_reference",
        "prior_delivery_source_projection",
        "prior_source_boundary_context",
    } & display_audit.keys()
    assert display_audit["input_cue_grid_sha256"] == pipeline.cue_grid_sha256(cues)
    assert display_audit["output_cue_grid_sha256"] == pipeline.cue_grid_sha256(
        pipeline.parse_srt_cues(normalized_srt)
    )
    assert len(gate_calls) == 1
    assert gate_calls[0]["text_recovery_only"] is True
    assert callback_reviews == [normalized_srt]
    assert callback_offsets == [(59_740, 64_740)]
    assert gate_calls[0]["final_start"] == 59_990
    assert gate_calls[0]["final_end"] == 64_740
    assert final_review["boundary_semantic_review"]["source_separation_witness"]["source_final_end_ms"] == 64_740 + tail_gap_ms
    assert source_review_calls[0]["current_source_cues"][0].text == "谢谢 Alice 的 SC!"
    assert source_review_calls[0]["current_source_cues"][1].text == (
        "直女、mysuperchat 和 SuperChatGPT"
    )
    assert chat_path.read_bytes() == original_chat_bytes
    assert context.read_bytes() == original_context_bytes
    persisted_chat = json.loads(result.chat_authority_path.read_text(encoding="utf-8"))
    assert persisted_chat["current_caption_display_surface_audit"] == display_audit


def test_original_historical_fixture_does_not_inherit_current_policy_pass():
    from tests.continuation_support import review as synthetic_review
    raw = json.dumps(synthetic_review()).encode()
    historical = json.loads(raw)
    assert "meaning_policy" not in historical
    with pytest.raises(pipeline.FinalReviewContractError, match="FINAL_REVIEW_MEANING_POLICY_REVIEW_REQUIRED"):
        pipeline.validate_final_review_release(historical)
