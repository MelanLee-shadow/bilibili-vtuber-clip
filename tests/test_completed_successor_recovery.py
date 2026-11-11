import copy
import json
from pathlib import Path

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from tests.test_producer_current_text_auto_resume import (
    _checkpoint,
    _completed_current_review,
    _native_checkpoint,
    _sha,
)


def _completed_successor_case(tmp_path: Path, *, damage: str | None = None, pending=False):
    checkpoint = _checkpoint(tmp_path)
    checkpoint_authority = json.loads(checkpoint["authority_path"].read_text())
    parent_srt = checkpoint["srt"]
    parent_sha = _sha(parent_srt.encode())
    checkpoint_authority.update(
        post_transcript_entity_output_srt=parent_srt,
        post_transcript_entity_output_srt_sha256=parent_sha,
        final_review_audit={"boundary_semantic_review": {"review_scope": "source_full_window"}},
    )
    checkpoint["authority_path"].write_text(json.dumps(checkpoint_authority))
    manifest, _ = _native_checkpoint(checkpoint)
    original_manifest = json.loads(manifest.read_text())
    old_checkpoint_authority = Path(original_manifest["authority"]["path"]).read_bytes()

    delivery = checkpoint["out_root"] / "replacement_recuts" / "candidate.recut.srt"
    delivery.parent.mkdir()
    delivery_text = parent_srt.replace("当前文字", "后续完成的文字")
    delivery.write_text(delivery_text)
    delivery_sha = _sha(delivery_text.encode())
    review, _source = _completed_current_review(delivery_text)
    boundary = review["boundary_semantic_review"]
    boundary["candidate_id"] = checkpoint["cid"]
    boundary["final_endpoint_binding"].update(final_start_ms=0, final_end_ms=50)
    boundary["source_separation_witness"].update(source_final_start_ms=0, source_final_end_ms=50)
    review["current_delivery_source_projection"].update(
        schema_version="delivery-to-source-timeline-projection.v1",
        status="PASS",
        source_final_start_ms=0,
        source_final_end_ms=50,
        delivery_srt_cue_grid_sha256=pipeline.cue_grid_sha256(
            pipeline.parse_srt_cues(delivery_text)
        ),
        projected_source_cue_grid_sha256=pipeline.cue_grid_sha256(
            pipeline.parse_srt_cues(delivery_text)
        ),
    )
    boundary["source_separation_witness"]["source_cue_grid_sha256"] = pipeline.cue_grid_sha256(
        pipeline.parse_srt_cues(parent_srt)
    )
    if damage == "parent":
        live_parent_sha = "f" * 64
    else:
        live_parent_sha = parent_sha
    if damage == "projection":
        boundary.pop("source_separation_witness")
    if pending:
        review.update(
            status="FLAGGED", release_gate="BLOCK",
            reason_codes=["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
            findings=[{"cue_index": 1, "suspect": "后续完成的文字"}],
            validated_finding_count=1,
        )

    live = copy.deepcopy(checkpoint_authority)
    live.update(
        final_text_srt_path=str(delivery),
        final_output_srt_sha256=delivery_sha,
        post_transcript_entity_output_srt_sha256=live_parent_sha,
        final_review_audit=review,
    )
    checkpoint["authority_path"].write_text(json.dumps(live))
    return checkpoint, manifest, Path(original_manifest["authority"]["path"]), old_checkpoint_authority, review


def _patch_snapshot_seams(monkeypatch, review):
    source_review = {"status": "FLAGGED", "release_gate": "BLOCK"}
    monkeypatch.setattr(
        pipeline,
        "_continuation_source_clock_reference",
        lambda *_args, **_kwargs: (source_review, {}),
    )
    monkeypatch.setattr(
        pipeline,
        "_continuation_validate_existing_authority",
        lambda authority, **_kwargs: authority["final_review_audit"],
    )
    return source_review


def test_changed_completed_successor_delivery_reuses_parent_and_projection(
    tmp_path, monkeypatch,
):
    checkpoint, manifest, frozen_authority_path, old_authority, review = _completed_successor_case(tmp_path)
    source_review = _patch_snapshot_seams(monkeypatch, review)
    result = pipeline._automatic_current_checkpoint_snapshot(
        spec=checkpoint["spec"], durations=[100], padded=checkpoint["padded"],
        padded_dur=100, cid=checkpoint["cid"], out_root=checkpoint["out_root"],
    )

    assert result["srt_text"] == "1\n00:00:00,000 --> 00:00:00,050\n后续完成的文字\n"
    assert result["chat_authority"]["final_review_audit"] == review
    assert source_review["status"] == "FLAGGED"
    assert source_review["release_gate"] == "BLOCK"
    promoted = json.loads(manifest.read_text())
    assert promoted["release_authorized"] is False
    assert frozen_authority_path.read_bytes() == old_authority


def test_pending_delivery_successor_reaches_input_validator_and_keeps_block(tmp_path, monkeypatch):
    checkpoint, manifest, frozen_path, old_authority, review = _completed_successor_case(
        tmp_path, pending=True,
    )
    _patch_snapshot_seams(monkeypatch, review)
    seen = []

    def validate(authority, **kwargs):
        seen.append((copy.deepcopy(authority["final_review_audit"]), kwargs["srt_text"]))
        return authority["final_review_audit"]

    monkeypatch.setattr(pipeline, "_continuation_validate_existing_authority", validate)
    snapshot = pipeline._automatic_current_checkpoint_snapshot(
        spec=checkpoint["spec"], durations=[100], padded=checkpoint["padded"],
        padded_dur=100, cid=checkpoint["cid"], out_root=checkpoint["out_root"],
    )
    assert seen == [(review, snapshot["srt_text"])]
    assert snapshot["chat_authority"]["final_review_audit"] == review
    assert review["status"] == "FLAGGED" and review["release_gate"] == "BLOCK"
    assert review["validated_finding_count"] == 1
    assert json.loads(manifest.read_text())["release_authorized"] is False
    assert frozen_path.read_bytes() == old_authority
    with pytest.raises(pipeline.FinalReviewContractError):
        pipeline.validate_final_review_release(review)


@pytest.mark.parametrize("damage", ["parent", "projection", "input_authority"])
def test_pending_delivery_successor_rejects_drift_before_checkpoint_write(tmp_path, monkeypatch, damage):
    checkpoint, manifest, frozen_path, old_authority, review = _completed_successor_case(
        tmp_path, damage=damage, pending=True,
    )
    before = manifest.read_bytes()
    _patch_snapshot_seams(monkeypatch, review)
    if damage == "input_authority":
        def reject(*args, **kwargs):
            raise pipeline.CurrentTextContinuationError("CURRENT_PENDING_INPUT_INVALID")
        monkeypatch.setattr(pipeline, "_continuation_validate_existing_authority", reject)
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._automatic_current_checkpoint_snapshot(
            spec=checkpoint["spec"], durations=[100], padded=checkpoint["padded"],
            padded_dur=100, cid=checkpoint["cid"], out_root=checkpoint["out_root"],
        )
    assert manifest.read_bytes() == before
    assert frozen_path.read_bytes() == old_authority


@pytest.mark.parametrize("damage", ["parent", "projection"])
def test_changed_completed_successor_rejects_parent_or_projection_drift(
    tmp_path, monkeypatch, damage,
):
    checkpoint, _manifest, _frozen_authority_path, _old_authority, review = _completed_successor_case(
        tmp_path, damage=damage,
    )
    _patch_snapshot_seams(monkeypatch, review)
    with pytest.raises(
        pipeline.CurrentTextContinuationError,
        match="CURRENT_COMPLETED_SUCCESSOR_PARENT_DRIFT",
    ):
        pipeline._automatic_current_checkpoint_snapshot(
            spec=checkpoint["spec"], durations=[100], padded=checkpoint["padded"],
            padded_dur=100, cid=checkpoint["cid"], out_root=checkpoint["out_root"],
        )


@pytest.mark.parametrize("clock_boundary", ["pass_input", "pass_output", "successor_output"])
def test_source_clock_replays_completed_release_grade_successor_without_release(clock_boundary):
    from src.autoslice.exact_final_cpa_history import with_release_grade_merge_successor
    from tests.test_release_grade_merge_successor import member

    receipt, reference, before = member()
    merged, merge_report = pipeline.merge_release_grade_cues(before)
    assert merged != before
    chat = {
        "exact_final_cpa_self_heal": receipt,
        "final_review_audit": {"release_gate": "BLOCK"},
    }
    chat["exact_final_cpa_self_heal"] = with_release_grade_merge_successor(
        chat["exact_final_cpa_self_heal"],
        before=before,
        after=merged,
        merge_report=merge_report,
    )
    reference = {"pass_input": reference, "pass_output": before, "successor_output": merged}[clock_boundary]
    chat_before = copy.deepcopy(chat)
    pipeline._continuation_replay_clock_text_chain(
        chat,
        reference_text=reference,
        reference_sha=_sha(reference.encode()),
        current_text=merged,
        current_sha=_sha(merged.encode()),
    )
    assert chat == chat_before
    assert chat["final_review_audit"]["release_gate"] == "BLOCK"

    unrelated = reference.replace("\n", "\r\n")
    with pytest.raises(pipeline.CurrentTextContinuationError, match="CURRENT_SOURCE_CLOCK_TEXT_CHAIN_MISSING"):
        pipeline._continuation_replay_clock_text_chain(
            chat, reference_text=unrelated, reference_sha=_sha(unrelated.encode()),
            current_text=merged, current_sha=_sha(merged.encode()),
        )


def test_historical_contract_disclosure_reopens_real_authority_as_input_only(
    tmp_path, monkeypatch,
):
    """A real old terminal row is input evidence, never a release union member."""
    from src.autoslice.final_review_contract import (
        is_release_resolved_keep, validate_final_review_release,
    )
    from src.autoslice.finding_lifecycle import classify
    from tests.test_finding_lifecycle_contract_retry import (
        review_of, _b_supported, _cue25_rig,
        _patch_cue25_range_plans,
    )
    from tests.test_terminal_contract_roundtrip import produce
    from tests.test_producer_current_text_continuation import _semantic_blocked_authority, _srt

    _patch_cue25_range_plans(monkeypatch)
    rig = _cue25_rig(tmp_path / "terminal", monkeypatch, "legacy")
    current_audit, captured = produce(rig, _b_supported, native=("你喜欢", "你喜欢"))
    # The real Xiada disclosure was recorded under the prior v6 terminal
    # contract.  Keep that version (rather than collapsing to the v1 helper)
    # and retain its sealed loop-stop proof.  The old projection's derived
    # count is deliberately stale: the current cold consumer must reject the
    # old proof with its own FINAL_STOP_OBSERVATION_DRIFT reason, not treat it
    # as a new hearing.  Rebind only the legacy proof hash and prompt, as the
    # historical producer did; no answer/material is invented here.
    legacy_request = copy.deepcopy(captured["request"])
    legacy_request["terminal_contract_version"] = 6
    legacy_request.pop("answer_contract_version", None)
    legacy_proof = copy.deepcopy(legacy_request["audio_gain_termination"])
    legacy_proof["projection"]["effective_distinct_observations"] += 1
    from src.autoslice.targeted_relisten import digest
    legacy_proof["proof_sha256"] = digest(
        {key: value for key, value in legacy_proof.items() if key != "proof_sha256"}
    )
    legacy_request["audio_gain_termination"] = legacy_proof
    from src.autoslice.semantic_terminal_adjudication import terminal_prompt
    legacy_prompt = terminal_prompt(legacy_request)
    raw = json.dumps(captured["answer"], ensure_ascii=False)
    cue_text = pipeline.parse_srt_cues(rig.srt)[24].text
    row = {
        **rig.finding,
        "base_text_sha256": _sha(cue_text.encode()),
        # Preserve the real current terminal ruling and replace only its
        # recorded prompt/answer binding with the old contract shape.  The
        # current consumer must reject that historical answer while the
        # disclosure classifier still recognizes its observed KEEP_CURRENT
        # provenance as input evidence.
        "exact_release_adjudication": {
            **copy.deepcopy(current_audit),
            "terminal_review": review_of(legacy_request, legacy_prompt, raw),
        },
    }
    from src.autoslice.final_review_meaning_policy import bind_current_risk
    row["current_content_risk"] = bind_current_risk(
        row, srt_text=rig.srt,
        cue_count=len(pipeline.parse_srt_cues(rig.srt)),
        prompt="saved current meaning review",
        meaning_equivalence={
            "status": "EQUIVALENT",
            "current_meaning": "当前句表达原有命题",
            "alternative_meaning": "尾部细节存在另一种写法",
            "shared_meaning": "完整句意和关系不变",
            "uncertain_detail": "尾部非关键字音",
            "context_basis": "完整上下文支持原句意",
            "evidence_cue_ids": [25],
        },
    )
    lifecycle = classify(row, terminal=True)
    assert lifecycle["state"] == "CONTRACT_ERROR"
    assert lifecycle["recovery"] == "REASK_UNDER_CURRENT_CONTRACT"
    assert not is_release_resolved_keep(row)

    authority_root = tmp_path / "authority"
    authority_root.mkdir()
    authority, context, srt_sha = _semantic_blocked_authority(authority_root)
    review = authority["final_review_audit"]
    review.update(
        status="CLEAN", release_gate="PASS", reason_codes=[], findings=[],
        validated_finding_count=0, unresolved_findings_disclosed=[row],
    )
    before = copy.deepcopy(authority)
    with pytest.raises(
        pipeline.FinalReviewContractError,
        match="FINAL_REVIEW_FINDINGS_CONTRACT_INVALID",
    ):
        validate_final_review_release(review, expected_srt_sha256="sha256:" + srt_sha)

    result = pipeline._continuation_validate_existing_authority(
        authority, srt_text=_srt(), srt_sha256=srt_sha, clip_context=context,
        candidate_id="relationship-candidate",
    )
    assert result["status"] == "CLEAN"
    assert result["release_gate"] == "PASS"
    assert pipeline._continuation_historical_disclosure_pending(authority, review)
    assert json.dumps(authority, sort_keys=True) == json.dumps(before, sort_keys=True)
    # The separate postburn receipt remains the ordinary priority input and is
    # not silently consumed as a release decision.
    assert pipeline.validated_postburn_priority_findings(
        final_srt_text=_srt(), verified_authority_audit=authority,
        candidate_id="relationship-candidate", clip_context=context,
    ) is not None
