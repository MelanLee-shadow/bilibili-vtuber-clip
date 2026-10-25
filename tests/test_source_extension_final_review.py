import copy
import hashlib
import json
from dataclasses import replace

import pytest

from src.autoslice import producer_package_finalization as finalization
from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.exact_final_cpa_history import build_run_audit, source_extension_successors
from src.autoslice.final_review_contract import FinalReviewContractError, _validate_exact_final_cpa_self_heal
from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues, _format_srt_time
from src.autoslice.producer_boundary_resolution import _current_delivery_source_extension
from tests.test_producer_current_text_continuation import _caption_successor_clock
from tests.test_boundary_resolver import _current_projection


def render(cues):
    return "\n\n".join(f"{c.index}\n{_format_srt_time(c.start_ms)} --> {_format_srt_time(c.end_ms)}\n{c.text}" for c in cues) + "\n"


def case(tmp_path, *, flash=False):
    chat, _, before = _caption_successor_clock(tmp_path)
    old = parse_srt_cues(before)
    offset = 35020
    projected = [replace(c, start_ms=c.start_ms + offset, end_ms=c.end_ms + offset) for c in old]
    n, terminal = len(old), projected[-1].end_ms
    tail = ([SrtCue(str(n+1), terminal+1000, terminal+1600, "稍等"),
             SrtCue(str(n+2), terminal+2500, terminal+4000, "完整后续互动")] if flash else
            [SrtCue(str(n+1), terminal+1000, terminal+3000, "完整后续互动")])
    source = [*projected, *tail, SrtCue(str(n+len(tail)+1), terminal+5000, terminal+7000, "下一个话题")]
    end = source[-2].end_ms
    review = {"schema_version": "talk-boundary-semantic-review.v1", "status": "PASS",
              "review_scope": "source_full_window", "request_sha256": "sha256:" + "1"*64,
              "cue_grid_sha256": cue_grid_sha256(source),
              "recommended_end_cue_index": n+len(tail), "recommended_end_ms": end}
    projection = {**_current_projection(projected), "offset_ms": offset}
    combined, extension = _current_delivery_source_extension(
        source_cues=source, current_delivery_cues=projected, current_delivery_projection=projection,
        semantic_review=review, closure_cue=source[-2], snapped_end_ms=end,
        final_start_ms=offset, final_end_ms=end, manual_end_mode="semantic_lower_bound")
    boundary = {"final_start_ms": offset, "final_end_ms": end,
                "snapped_sentence_end_ms": end, "boundary_semantic_review": review,
                "current_delivery_source_projection": projection,
                "current_delivery_closure_binding": extension}
    source_text = render(source)
    source_path = tmp_path / "source.srt"
    source_path.write_text(source_text)
    chat["final_review_audit"]["source_boundary_context"] = {
        "source_srt_path": str(source_path),
        "source_srt_sha256": "sha256:" + hashlib.sha256(source_text.encode()).hexdigest()}
    (tmp_path / "candidate.boundary_audit.json").write_text(json.dumps(boundary))
    if flash:
        cue = tail[0]
        timing = {"schema_version": "subtitle-timing-qa.v3",
                  "window": {"start_ms": offset, "end_ms": end},
                  "policy": {"min_readable_ms": 1000},
                  "actions": [{"action": "retime", "reasons": ["flash_extended"], "text": cue.text,
                      "before": {"start_ms": cue.start_ms, "end_ms": cue.end_ms},
                      "after": {"start_ms": cue.start_ms, "end_ms": cue.start_ms+1000}}]}
        q=tmp_path / "replacement_recuts"
        q.mkdir()
        (q / "candidate.recut.timing_qa.json").write_text(json.dumps(timing))
        combined[len(old)] = replace(combined[len(old)], end_ms=cue.start_ms+1000)
    after = render([replace(c, index=str(i), start_ms=c.start_ms-offset, end_ms=c.end_ms-offset)
                    for i,c in enumerate(combined,1)])
    assert after.startswith(before.rstrip() + "\n\n")
    return chat, before, after, boundary, source_path


def test_pending_typed_repairs_resume_through_draft_extension_without_release(tmp_path):
    chat, before, after, _, _ = case(tmp_path)
    baseline = copy.deepcopy(chat)
    receipt = finalization._resume_source_extended_review(cid="candidate", out_root=tmp_path,
        final_text=after, chat_authority_audit=chat)
    assert chat == baseline
    assert receipt["passes"] == chat["exact_final_cpa_self_heal"]["passes"]
    assert receipt["status"] == "REVIEW_PENDING"
    assert len(source_extension_successors(receipt)) == 1
    digest = "sha256:" + hashlib.sha256(after.encode()).hexdigest()
    _validate_exact_final_cpa_self_heal({"exact_final_cpa_self_heal": receipt}, expected_srt_sha256=digest,
                                       allow_pending=True)
    with pytest.raises(FinalReviewContractError, match="AUDIT_INVALID"):
        _validate_exact_final_cpa_self_heal({"exact_final_cpa_self_heal": receipt}, expected_srt_sha256=digest)
    sealed = build_run_audit(receipt["passes"], [], final_srt_sha256=digest,
        caption_display_successors=receipt["caption_display_successors"],
        source_extension_successors=receipt["source_extension_successors"])
    _validate_exact_final_cpa_self_heal({"exact_final_cpa_self_heal": sealed}, expected_srt_sha256=digest)
    assert sealed["passes"] == receipt["passes"]
    assert before != after


@pytest.mark.parametrize("drift", ["prefix", "tail", "clock", "source", "boundary", "projection"])
def test_source_extension_rejects_drift_before_review_dispatch(tmp_path, drift):
    chat, before, after, boundary, source = case(tmp_path)
    if drift == "prefix":
        after = after.replace("谢谢观众", "谢谢另一个人")
    elif drift == "tail":
        after = after.replace("完整后续互动", "手写的新尾部")
    elif drift == "clock":
        boundary["final_start_ms"] += 1
    elif drift == "source":
        source.write_text(source.read_text().replace("完整后续互动", "漂移"))
    elif drift == "boundary":
        boundary["boundary_semantic_review"]["status"] = "BLOCK"
    else:
        boundary["current_delivery_source_projection"]["rows"][-1]["text_sha256"] = "sha256:" + "0"*64
    (tmp_path / "candidate.boundary_audit.json").write_text(json.dumps(boundary))
    with pytest.raises(FinalReviewContractError):
        finalization._resume_source_extended_review(cid="candidate", out_root=tmp_path,
            final_text=after, chat_authority_audit=chat)


def test_real_timing_flash_extension_is_accepted_only_for_new_tail(tmp_path):
    chat, _, after, _, _ = case(tmp_path, flash=True)
    receipt = finalization._resume_source_extended_review(cid="candidate", out_root=tmp_path,
        final_text=after, chat_authority_audit=chat)
    assert len(source_extension_successors(receipt)) == 1
    timing = tmp_path / "replacement_recuts/candidate.recut.timing_qa.json"
    timing.unlink()
    with pytest.raises(FinalReviewContractError, match="SOURCE_EXTENSION_INVALID"):
        finalization._resume_source_extended_review(cid="candidate", out_root=tmp_path,
            final_text=after, chat_authority_audit=chat)


@pytest.mark.parametrize("failure", ["missing", "type", "count", "prefix_length", "source"])
def test_recovery_contracts_have_distinct_error_codes(tmp_path, failure):
    from src.autoslice.exact_final_cpa_history import (
        RECOVERY_MATERIAL_MISSING, RECOVERY_TYPE_INAPPLICABLE, RECOVERY_PREFIX_INVALID,
    )
    chat, before, after, boundary, _ = case(tmp_path)
    path = tmp_path / "candidate.boundary_audit.json"
    if failure == "missing":
        path.unlink()
        reason = RECOVERY_MATERIAL_MISSING
    elif failure == "type":
        boundary["current_delivery_closure_binding"]["schema_version"] = "current-delivery-closure-binding.v1"
        reason = RECOVERY_TYPE_INAPPLICABLE
    elif failure == "count":
        boundary["current_delivery_closure_binding"]["current_prefix_cue_count"] = True
        reason = RECOVERY_PREFIX_INVALID
    elif failure == "source":
        chat["final_review_audit"] = None
        reason = RECOVERY_MATERIAL_MISSING
    else:
        after = before
        reason = RECOVERY_PREFIX_INVALID
    if failure != "missing":
        path.write_text(json.dumps(boundary))
    with pytest.raises(FinalReviewContractError) as caught:
        finalization._resume_source_extended_review(cid="candidate", out_root=tmp_path,
            final_text=after, chat_authority_audit=chat)
    assert caught.value.reason_code == reason


@pytest.mark.parametrize("marker", [None, "current-delivery-closure-binding.v1", "current-delivery-source-extension.v1"])
def test_gate_routes_only_explicit_source_extensions(tmp_path, monkeypatch, marker):
    from types import SimpleNamespace
    from src.autoslice.exact_final_cpa_history import CHAIN_TERMINAL_MISMATCH
    chat, before, after, boundary, _ = case(tmp_path)
    path = tmp_path / "candidate.boundary_audit.json"
    if marker is None:
        path.unlink()
    else:
        boundary["current_delivery_closure_binding"]["schema_version"] = marker
        path.write_text(json.dumps(boundary))
    subtitle = tmp_path / "current.srt"
    subtitle.write_text(after)
    def recover(**kwargs):
        raise RuntimeError("EXPLICIT_SOURCE_EXTENSION_ROUTED")
    monkeypatch.setattr(finalization, "_resume_source_extended_review", recover)
    def reviewer(*args):
        raise AssertionError("unbound chain must not reach reviewer")
    expected = RuntimeError if marker == "current-delivery-source-extension.v1" else FinalReviewContractError
    with pytest.raises(expected) as caught:
        finalization._run_exact_final_review_gate(cid="candidate", out_root=tmp_path,
            final_start=0, final_end=100000, recut=SimpleNamespace(subtitle_path=subtitle,
                redelivery_baseline_audit=None, redelivery_baseline_audit_path=None),
            chat_authority_audit=chat, chat_authority_path=tmp_path / "authority.json", run_exact_final_review=reviewer)
    if expected is FinalReviewContractError:
        assert caught.value.reason_code == CHAIN_TERMINAL_MISMATCH
        assert caught.value.context["actual"] != caught.value.context["expected"]
