import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.apply_subtitle_text_overrides import apply_document
from src.autoslice import producer_package_finalization as finalization
from src.autoslice.source_fact_rescore_provenance import (
    PROVENANCE_FIELD,
    canonical_sha256,
)
from src.autoslice.story_contract import cover_story_contract_binding
from src.autoslice.surface_canon import CHANNEL_PROFILE


def _deferred_exact_truth_audit() -> dict:
    return {
        "status": "DEFERRED_TO_REDELIVERY_BASELINE",
        "deferred_strategy": (
            "exact_reviewed_interval_replay_then_reapply_source_truth"
        ),
        "failures": [{"truth_id": "reviewed-cue-shape"}],
    }


def test_final_authority_persists_verified_baseline_owner_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtitle = tmp_path / "candidate.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n审定字幕\n",
        encoding="utf-8",
    )
    baseline_path = tmp_path / "candidate.redelivery-baseline.json"
    baseline_audit = {
        "status": "APPLIED",
        "mappings": [{"output_cue_index": 1}],
    }
    baseline_path.write_text(
        json.dumps(baseline_audit, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    def verify_and_annotate(*_args, **_kwargs) -> bool:
        baseline_audit["mappings"][0].update(
            {
                "final_owner_scope": "DELIVERY",
                "final_owner_verified": True,
            }
        )
        return True

    monkeypatch.setattr(
        finalization,
        "verify_chat_authority_final_surfaces",
        verify_and_annotate,
    )
    chat_path = tmp_path / "candidate.chat-authority.json"
    result = finalization._verify_final_authority(
        cid="candidate",
        final_start=0,
        final_end=1_000,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=tmp_path / "candidate.mp4",
            subtitle_path=subtitle,
            text_manifest_path=None,
            text_manifest=None,
            redelivery_baseline_audit_path=baseline_path,
            redelivery_baseline_audit=baseline_audit,
        ),
        speaker=finalization.SpeakerArtifacts(
            manifest=None,
            review_srt=None,
            ass=None,
            manifest_path=None,
        ),
        chat_authority_audit={},
        chat_authority_path=chat_path,
        subtitle_regression_path=None,
    )

    assert result == finalization.AuthorityArtifacts(None, None)
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == (
        baseline_audit
    )
    assert baseline_audit["mappings"][0]["final_owner_verified"] is True


def test_deferred_exact_replay_requires_same_truth_id_reverification() -> None:
    audit = finalization._audit_deferred_exact_replay_reverification(
        pre_truth_audit=_deferred_exact_truth_audit(),
        baseline_audit={
            "status": "APPLIED",
            "application_strategy": "exact_reviewed_interval_replay",
        },
        post_truth_audit={
            "status": "ALREADY_SATISFIED",
            "applied": [],
            "satisfied": [{"truth_id": "reviewed-cue-shape"}],
        },
    )

    assert audit["status"] == "PASS"
    assert audit["missing_truth_ids"] == []


def test_deferred_reviewed_restore_requires_same_truth_id_reverification() -> None:
    pre = _deferred_exact_truth_audit()
    pre["deferred_strategy"] = (
        "reviewed_text_restore_then_reapply_source_truth"
    )
    audit = finalization._audit_deferred_exact_replay_reverification(
        pre_truth_audit=pre,
        baseline_audit={
            "status": "APPLIED",
        },
        post_truth_audit={
            "status": "ALREADY_SATISFIED",
            "satisfied": [{"truth_id": "reviewed-cue-shape"}],
        },
    )

    assert audit["status"] == "PASS"
    assert audit["reverified_truth_ids"] == ["reviewed-cue-shape"]


def test_deferred_exact_replay_rejects_missing_truth_id() -> None:
    audit = finalization._audit_deferred_exact_replay_reverification(
        pre_truth_audit=_deferred_exact_truth_audit(),
        baseline_audit={
            "status": "APPLIED",
            "application_strategy": "exact_reviewed_interval_replay",
        },
        post_truth_audit={
            "status": "ALREADY_SATISFIED",
            "satisfied": [{"truth_id": "different-truth"}],
        },
    )

    assert audit["status"] == "FAILED"
    assert audit["missing_truth_ids"] == ["reviewed-cue-shape"]


def test_deferred_replay_does_not_require_post_boundary_context_witness() -> None:
    pre = _deferred_exact_truth_audit()
    pre["deferred_strategy"] = (
        "reviewed_text_restore_then_reapply_source_truth"
    )
    pre["failures"] = [
        {
            "truth_id": "story-content",
            "boundary_role": "story_content",
            "source_start_ms": 2_050_000,
            "source_end_ms": 2_055_000,
        },
        {
            "truth_id": "next-sc-nickname",
            "boundary_role": "next_topic_witness",
            "source_start_ms": 2_062_300,
            "source_end_ms": 2_064_990,
        },
    ]
    audit = finalization._audit_deferred_exact_replay_reverification(
        pre_truth_audit=pre,
        baseline_audit={
            "status": "APPLIED",
            "current_source_interval": {
                "absolute_source_start_ms": 1_863_550,
                "absolute_source_end_ms": 2_056_480,
            },
        },
        post_truth_audit={
            "status": "ALREADY_SATISFIED",
            "satisfied": [{"truth_id": "story-content"}],
        },
    )

    assert audit["status"] == "PASS"
    assert audit["required_truth_ids"] == ["story-content"]
    assert audit["context_only_truth_ids"] == ["next-sc-nickname"]
    assert audit["missing_truth_ids"] == []


def test_deferred_replay_passes_when_only_failure_is_post_boundary_context() -> None:
    pre = _deferred_exact_truth_audit()
    pre["deferred_strategy"] = (
        "reviewed_text_restore_then_reapply_source_truth"
    )
    pre["failures"] = [
        {
            "truth_id": "next-sc-nickname",
            "boundary_role": "next_topic_witness",
            "source_start_ms": 2_062_300,
            "source_end_ms": 2_064_990,
        }
    ]
    audit = finalization._audit_deferred_exact_replay_reverification(
        pre_truth_audit=pre,
        baseline_audit={
            "status": "APPLIED",
            "current_source_interval": {
                "absolute_source_start_ms": 1_863_550,
                "absolute_source_end_ms": 2_056_480,
            },
        },
        post_truth_audit={
            "status": "APPLIED",
            "satisfied": [
                {"truth_id": "unrelated-story-truth"}
            ],
        },
    )

    assert audit["status"] == "PASS"
    assert audit["reason_code"] == (
        "ALL_DEFERRED_TRUTH_CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY"
    )
    assert audit["required_truth_ids"] == []
    assert audit["context_only_truth_ids"] == ["next-sc-nickname"]
    assert audit["missing_truth_ids"] == []


def test_deferred_replay_treats_ordinary_post_context_truth_as_context_only() -> None:
    pre = _deferred_exact_truth_audit()
    pre["deferred_strategy"] = (
        "reviewed_text_restore_then_reapply_source_truth"
    )
    pre["failures"] = [
        {
            "truth_id": "ordinary-later-story-truth",
            "boundary_role": "story_content",
            "source_start_ms": 2_062_300,
            "source_end_ms": 2_064_990,
        }
    ]
    audit = finalization._audit_deferred_exact_replay_reverification(
        pre_truth_audit=pre,
        baseline_audit={
            "status": "APPLIED",
            "current_source_interval": {
                "absolute_source_start_ms": 1_863_550,
                "absolute_source_end_ms": 2_056_480,
            },
        },
        post_truth_audit={
            "status": "NO_RELEVANT_INTERVAL",
            "satisfied": [],
        },
    )

    assert audit["status"] == "PASS"
    assert audit["required_truth_ids"] == []
    assert audit["context_only_truth_ids"] == [
        "ordinary-later-story-truth"
    ]
    assert audit["straddling_truth_ids"] == []


@pytest.mark.parametrize(
    ("source_start_ms", "source_end_ms"),
    [
        (1_862_000, 1_864_000),
        (2_056_000, 2_058_000),
        (1_862_000, 2_058_000),
    ],
)
def test_deferred_replay_rejects_truth_straddling_final_interval(
    source_start_ms: int,
    source_end_ms: int,
) -> None:
    pre = _deferred_exact_truth_audit()
    pre["failures"] = [
        {
            "truth_id": "straddling-truth",
            "boundary_role": "story_content",
            "source_start_ms": source_start_ms,
            "source_end_ms": source_end_ms,
        }
    ]
    audit = finalization._audit_deferred_exact_replay_reverification(
        pre_truth_audit=pre,
        baseline_audit={
            "status": "APPLIED",
            "application_strategy": "exact_reviewed_interval_replay",
            "current_source_interval": {
                "absolute_source_start_ms": 1_863_550,
                "absolute_source_end_ms": 2_056_480,
            },
        },
        post_truth_audit={
            "status": "ALREADY_SATISFIED",
            "satisfied": [{"truth_id": "straddling-truth"}],
        },
    )

    assert audit["status"] == "FAILED"
    assert audit["reason_code"] == (
        "DEFERRED_TRUTH_STRADDLES_FINAL_DELIVERY"
    )
    assert audit["straddling_truth_ids"] == ["straddling-truth"]
    assert audit["context_only_truth_ids"] == []


def test_deferred_replay_rejects_empty_failure_requirement() -> None:
    pre = _deferred_exact_truth_audit()
    pre["failures"] = []
    audit = finalization._audit_deferred_exact_replay_reverification(
        pre_truth_audit=pre,
        baseline_audit={
            "status": "APPLIED",
            "application_strategy": "exact_reviewed_interval_replay",
        },
        post_truth_audit={
            "status": "ALREADY_SATISFIED",
            "satisfied": [],
        },
    )

    assert audit["status"] == "FAILED"
    assert audit["reason_code"] == "DEFERRED_TRUTH_REQUIREMENT_EMPTY"


def test_deferred_replay_keeps_overlapping_next_topic_truth_required() -> None:
    pre = _deferred_exact_truth_audit()
    pre["failures"] = [
        {
            "truth_id": "overlapping-witness",
            "boundary_role": "next_topic_witness",
            "source_start_ms": 2_056_000,
            "source_end_ms": 2_058_000,
        }
    ]
    audit = finalization._audit_deferred_exact_replay_reverification(
        pre_truth_audit=pre,
        baseline_audit={
            "status": "APPLIED",
            "application_strategy": "exact_reviewed_interval_replay",
            "current_source_interval": {
                "absolute_source_start_ms": 1_863_550,
                "absolute_source_end_ms": 2_056_480,
            },
        },
        post_truth_audit={
            "status": "ALREADY_SATISFIED",
            "satisfied": [],
        },
    )

    assert audit["status"] == "FAILED"
    assert audit["reason_code"] == (
        "DEFERRED_TRUTH_STRADDLES_FINAL_DELIVERY"
    )
    assert audit["required_truth_ids"] == []
    assert audit["context_only_truth_ids"] == []
    assert audit["straddling_truth_ids"] == ["overlapping-witness"]
    assert audit["missing_truth_ids"] == []


def test_rebase_source_truth_audit_moves_resolved_projection_to_padded_axis():
    audit = {
        "applied": [
            {
                "action": "replace_cue",
                "cue_indexes": [1],
                "local_windows": [{"start_ms": 100, "end_ms": 900}],
                "resolved_target_projection": {
                    "schema_version": (
                        "source-truth-resolved-target-projection.v1"
                    ),
                    "selector": (
                        "half-open-overlap-gte-min-then-action-resolution"
                    ),
                    "min_overlap_ms": 80,
                    "action": "replace_cue",
                    "status": "RESOLVED",
                    "cues": [
                        {
                            "cue_index": 1,
                            "start_ms": 120,
                            "end_ms": 880,
                            "before_text": "旧",
                            "after_text": "新",
                        }
                    ],
                },
                "timing_pin": {
                    "before_start_ms": 120,
                    "after_start_ms": 140,
                },
            }
        ]
    }

    rebased = finalization._rebase_source_truth_audit_to_padded(
        audit,
        final_start=10_000,
    )

    row = rebased["applied"][0]
    assert row["local_windows"] == [
        {"start_ms": 10_100, "end_ms": 10_900}
    ]
    assert row["resolved_target_projection"]["cues"][0][
        "start_ms"
    ] == 10_120
    assert row["resolved_target_projection"]["cues"][0][
        "end_ms"
    ] == 10_880
    assert row["timing_pin"]["after_start_ms"] == 10_140
    assert audit["applied"][0]["local_windows"][0]["start_ms"] == 100


def test_exact_final_review_gate_binds_post_boundary_recut_bytes(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "candidate.recut.srt"
    final_text = (
        "1\n00:00:00,000 --> 00:00:01,000\n最终边界内字幕\n\n"
        "2\n00:00:01,100 --> 00:00:02,000\n完整收束\n"
    )
    subtitle.write_text(final_text, encoding="utf-8")
    chat_path = tmp_path / "candidate.chat-authority.json"
    seen: dict = {}

    def exact_review(
        text,
        authority,
        timeline_offset_ms,
        source_final_end_ms,
    ):
        seen.update(
            {
                "text": text,
                "authority": authority,
                "timeline_offset_ms": timeline_offset_ms,
                "source_final_end_ms": source_final_end_ms,
            }
        )
        return {
            "schema_version": "final-review-audit.v2",
            "status": "CLEAN",
            "release_gate": "PASS",
            "reason_codes": [],
            "reviewed_srt_sha256": (
                "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            ),
            "discovery": {
                "status": "COMPLETE",
                "explicit_empty_findings": True,
            },
            "correction_mutation_authority": {
                "schema_version": "subtitle-correction-mutation-audit.v1",
                "status": "PASS",
                "applied_count": 0,
                "validated_mutation_count": 0,
                "failures": [],
            },
            "findings": [],
            "validated_finding_count": 0,
            "boundary_semantic_review": {
                "schema_version": "talk-boundary-semantic-review.v1",
                "status": "PASS",
                "review_scope": "final_delivery",
                "reason_codes": [],
                "request_sha256": "sha256:" + "d" * 64,
                "cue_grid_sha256": "sha256:" + "e" * 64,
                "source_separation_witness": {
                    "schema_version": (
                        "talk-boundary-source-separation-witness.v1"
                    ),
                    "status": "PASS",
                    "source_review_sha256": "sha256:" + "a" * 64,
                    "source_request_sha256": "sha256:" + "b" * 64,
                    "source_cue_grid_sha256": "sha256:" + "c" * 64,
                    "source_final_start_ms": timeline_offset_ms,
                    "source_final_end_ms": source_final_end_ms,
                    "reason_codes": [],
                },
                "final_endpoint_binding": {
                    "schema_version": "talk-boundary-final-endpoint-binding.v1",
                    "status": "PASS",
                    "recommended_end_cue_index": 1,
                    "recommended_end_ms": 9_000,
                    "final_closure_cue_index": 1,
                    "final_snapped_end_ms": 9_000,
                    "final_end_ms": 9_400,
                    "semantic_cue_grid_sha256": "sha256:" + "e" * 64,
                    "final_cue_grid_sha256": "sha256:" + "e" * 64,
                    "reason_codes": [],
                },
            },
        }

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=unused,
        run_exact_final_review=exact_review,
    )
    chat = {"source_subtitle_truth_audit": {"status": "APPLIED"}}
    finalization._run_exact_final_review_gate(
        cid="candidate",
        out_root=tmp_path,
        final_start=12_345,
        final_end=21_745,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=tmp_path / "candidate.recut.mp4",
            subtitle_path=subtitle,
            text_manifest_path=None,
            text_manifest=None,
        ),
        chat_authority_audit=chat,
        chat_authority_path=chat_path,
        adapters=adapters,
    )

    assert seen["text"] == final_text
    assert seen["authority"] is chat
    assert seen["timeline_offset_ms"] == 12_345
    assert seen["source_final_end_ms"] == 21_745
    assert chat["final_review_audit"]["status"] == "CLEAN"
    assert (tmp_path / "candidate.review-flags.json").is_file()


def test_exact_final_review_gate_hashes_raw_crlf_bytes_without_normalizing(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "candidate.recut.srt"
    raw_srt = (
        b"1\r\n00:00:00,000 --> 00:00:01,000\r\n"
        b"raw-byte binding\r\n"
    )
    subtitle.write_bytes(raw_srt)
    chat_path = tmp_path / "candidate.chat-authority.json"
    normalized_sha256 = "sha256:" + hashlib.sha256(
        raw_srt.replace(b"\r\n", b"\n")
    ).hexdigest()

    def exact_review(
        text,
        _authority,
        _timeline_offset_ms,
        _source_final_end_ms,
    ):
        assert "\r\n" in text
        return {
            "schema_version": "final-review-audit.v2",
            # Simulate the historical bug: hashing newline-normalized text
            # instead of the exact bytes read from the final SRT.
            "reviewed_srt_sha256": normalized_sha256,
        }

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=unused,
        run_exact_final_review=exact_review,
    )

    with pytest.raises(
        SystemExit,
        match="FINAL_REVIEW_SRT_BINDING_MISMATCH",
    ):
        finalization._run_exact_final_review_gate(
            cid="candidate",
            out_root=tmp_path,
            final_start=0,
            final_end=1_000,
            recut=finalization.FinalRecutArtifacts(
                recut_dir=tmp_path,
                media_path=tmp_path / "candidate.recut.mp4",
                subtitle_path=subtitle,
                text_manifest_path=None,
                text_manifest=None,
            ),
            chat_authority_audit={},
            chat_authority_path=chat_path,
            adapters=adapters,
        )

    assert normalized_sha256 != (
        "sha256:" + hashlib.sha256(raw_srt).hexdigest()
    )


def test_finalize_routes_exact_endpoint_receipt_into_story_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subtitle = tmp_path / "candidate.recut.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n最终闭合句\n",
        encoding="utf-8",
    )
    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"media")
    recut = finalization.FinalRecutArtifacts(
        recut_dir=tmp_path,
        media_path=media,
        subtitle_path=subtitle,
        text_manifest_path=None,
        text_manifest=None,
    )
    delivery_review = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "candidate_id": "candidate",
        "status": "PASS",
        "review_scope": "final_delivery",
        "request_sha256": "sha256:" + "1" * 64,
        "cue_grid_sha256": "sha256:" + "2" * 64,
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS",
            "semantic_cue_grid_sha256": "sha256:" + "2" * 64,
            "final_cue_grid_sha256": "sha256:" + "2" * 64,
        },
    }
    exact_audit = {"boundary_semantic_review": delivery_review}
    captured: dict = {}
    materialization_spec = {"projection_activation": "selected"}

    monkeypatch.setattr(
        finalization,
        "materialization_spec_for_selected_projection",
        lambda current_spec, current_audit: (
            captured.update(
                {
                    "projection_input_spec": current_spec,
                    "projection_input_audit": current_audit,
                }
            )
            or materialization_spec
        ),
    )
    monkeypatch.setattr(
        finalization,
        "_materialize_final_recut",
        lambda **kwargs: (
            captured.setdefault("materialization_spec", kwargs["spec"])
            and recut
        ),
    )
    monkeypatch.setattr(
        finalization,
        "_run_exact_final_review_gate",
        lambda **_kwargs: exact_audit,
    )
    monkeypatch.setattr(
        finalization,
        "_finalize_speaker",
        lambda **_kwargs: finalization.SpeakerArtifacts(
            None, None, None, None
        ),
    )
    monkeypatch.setattr(
        finalization,
        "_verify_final_authority",
        lambda **_kwargs: finalization.AuthorityArtifacts(None, None),
    )
    monkeypatch.setattr(
        finalization,
        "_build_and_burn_record",
        lambda **_kwargs: {"artifact_hashes": {}},
    )
    monkeypatch.setattr(
        finalization,
        "_deliver_staged_record",
        lambda **kwargs: captured.setdefault("record", kwargs["staged"].record)
        and 0,
    )
    monkeypatch.setattr(
        finalization, "load_candidate_cover_reference", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        finalization, "build_llm_call", lambda _config: lambda _prompt: ""
    )

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    def stage(record, *, candidate_id, **_kwargs):
        record["publish_staging"] = {
            "title": candidate_id,
            "title_authority_status": "READY",
            "cover_status": "SKIPPED",
        }
        return record

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=stage,
        generate_upload_tags=lambda *_args, **_kwargs: {"status": "READY"},
        delivery_root=unused,
        run_exact_final_review=unused,
    )
    spec = {
        "date": "2026-07-22",
        "pieces": [],
        "selection_hook": "最终闭合句",
        "selection_scorecard": {
            "status": "VALID",
            "dimensions": {"self_contained": 4, "comedic_payoff": 4},
        },
        "boundary_semantic_review": {"status": "PASS", "stale": True},
    }
    boundary_audit: dict = {}

    result = finalization.finalize_producer_package(
        options=finalization.ProducerFinalizationOptions(
            spec=tmp_path / "spec.json",
            substrate="source",
            correct="reviewed",
            speaker_mode="off",
            speaker_overrides=None,
            speaker_source_session_anchors=None,
            speaker_mixed_overlap_evidence=None,
            speaker_python=tmp_path / "python",
            reuse_cover=True,
        ),
        profile_id="lidousha",
        speaker_subtitle_style_id="style",
        spec=spec,
        cid="candidate",
        out_root=tmp_path,
        host="free",
        padded=tmp_path / "padded.mp4",
        padded_provenance_path=tmp_path / "padded.json",
        piece_provenance_rows=[],
        final_start=10_000,
        final_end=11_000,
        sanitized=[],
        timing_qa={},
        audit=boundary_audit,
        text_override_path=None,
        subtitle_regression_path=None,
        chat_authority_audit={},
        chat_authority_path=tmp_path / "chat.json",
        branding_intro=None,
        adapters=adapters,
    )

    assert result == 0
    assert captured["projection_input_spec"] is spec
    assert captured["projection_input_audit"] is boundary_audit
    assert captured["materialization_spec"] is materialization_spec
    assert spec["boundary_semantic_review"] == delivery_review
    assert boundary_audit["final_delivery_boundary_semantic_review"] == (
        delivery_review
    )
    assert captured["record"]["story_contract"][
        "boundary_semantic_review"
    ] == delivery_review


@pytest.mark.parametrize(
    (
        "decision",
        "original_hook",
        "final_hook",
        "final_title",
        "classification",
    ),
    [
        (
            "KEEP",
            f"弹幕自称表妹却叫{CHANNEL_PROFILE.display_name}老公。",
            f"弹幕自称表妹却叫{CHANNEL_PROFILE.display_name}老公。",
            f"{CHANNEL_PROFILE.talk_title_prefix}弹幕自称表妹却叫她老公，她强调自己才是真的表妹",
            "talk",
        ),
        (
            "REPAIRED",
            f"弹幕叫{CHANNEL_PROFILE.display_name}老公。",
            f"弹幕自称表妹却叫{CHANNEL_PROFILE.display_name}老公，{CHANNEL_PROFILE.display_name}强调自己才是真的表妹。",
            f"{CHANNEL_PROFILE.talk_title_prefix}弹幕自称表妹却叫她老公，她强调自己才是真的表妹",
            "talk",
        ),
        (
            "KEEP",
            f"{CHANNEL_PROFILE.display_name}演唱《暖暖》。",
            f"{CHANNEL_PROFILE.display_name}演唱《暖暖》。",
            f"{CHANNEL_PROFILE.song_title_prefix}《暖暖》",
            "song",
        ),
    ],
)
def test_stage_record_uses_post_source_fact_story_contract_everywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    original_hook: str,
    final_hook: str,
    final_title: str,
    classification: str,
) -> None:
    subtitle = tmp_path / "candidate.recut.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n"
        "我才是真的表妹\n",
        encoding="utf-8",
    )
    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"media")
    recut = finalization.FinalRecutArtifacts(
        recut_dir=tmp_path,
        media_path=media,
        subtitle_path=subtitle,
        text_manifest_path=None,
        text_manifest=None,
    )
    captured: dict[str, object] = {}
    rescore_provenance = {
        "schema_version": "source-fact-scorecard-rescore-provenance.v1",
        "candidate_id": "candidate",
        "corrected_selection_hook": original_hook,
        "selection_scorecard_sha256": canonical_sha256(None),
        "provenance_sha256": "sha256:" + "a" * 64,
    }

    monkeypatch.setattr(
        finalization, "load_candidate_cover_reference", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        finalization, "build_llm_call", lambda _config: lambda _prompt: ""
    )

    def stage(record: dict, **kwargs) -> dict:
        captured["pre_review_contract"] = record["story_contract"]
        rebuilt = kwargs["story_contract_rebuilder"](final_hook)
        receipt = {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": "PASS",
            "decision": decision,
            "final_selection_hook": final_hook,
            "final_title": final_title,
        }
        rebuilt["source_fact_review"] = receipt
        record["story_contract"] = rebuilt
        record["publish_staging"] = {
            "title": final_title,
            "title_authority_status": (
                "RESOLVED_CPA_SOURCE_FACT_REPAIR"
                if decision == "REPAIRED"
                else "READY"
            ),
            "source_fact_review": receipt,
            "cover_status": "AI_COVER_READY",
            "cover_text": "我才是真的表妹",
            "cover_generation": {
                "story_contract": cover_story_contract_binding(rebuilt),
            },
        }
        captured["post_review_contract"] = rebuilt
        return record

    def audit_cover(
        staging: dict, story_contract: dict
    ) -> tuple[list[str], list[dict[str, object]]]:
        captured["audited_contract"] = story_contract
        assert (
            staging["cover_generation"]["story_contract"]["selection_hook"]
            == story_contract["selection_hook"]
        )
        return [], [{"status": "PASS", "artifact_kind": "cover_text"}]

    monkeypatch.setattr(
        finalization, "_audit_story_bound_cover", audit_cover
    )

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=stage,
        generate_upload_tags=lambda *_a, **_k: {"status": "READY"},
        delivery_root=unused,
        run_exact_final_review=unused,
    )
    staged = finalization._stage_record(
        options=finalization.ProducerFinalizationOptions(
            spec=tmp_path / "spec.json",
            substrate="source",
            correct="reviewed",
            speaker_mode="off",
            speaker_overrides=None,
            speaker_source_session_anchors=None,
            speaker_mixed_overlap_evidence=None,
            speaker_python=tmp_path / "python",
            reuse_cover=False,
        ),
        spec={
            "date": "2026-07-29",
            "pieces": [],
            "selection_hook": original_hook,
            "selection_scorecard": None,
            "classification": classification,
                PROVENANCE_FIELD: rescore_provenance,
        },
        cid="candidate",
        recut=recut,
        record={"artifact_hashes": {}},
        adapters=adapters,
    )

    active = staged.record["story_contract"]
    expected_hash = (
        "sha256:" + hashlib.sha256(final_hook.encode("utf-8")).hexdigest()
    )
    assert active is captured["audited_contract"]
    assert active is not captured["pre_review_contract"]
    assert active["selection_hook"] == final_hook
    assert active["selection_hook_sha256"] == expected_hash
    assert active["source_fact_review"] == staged.staging[
        "source_fact_review"
    ]
    assert active[PROVENANCE_FIELD] == rescore_provenance
    assert staged.record[PROVENANCE_FIELD] == rescore_provenance
    assert staged.staging[PROVENANCE_FIELD] == rescore_provenance
    assert captured["pre_review_contract"][PROVENANCE_FIELD] == (
        rescore_provenance
    )
    assert captured["post_review_contract"][PROVENANCE_FIELD] == (
        rescore_provenance
    )
    assert active["cover_output_audits"][0]["status"] == "PASS"
    assert "cover_output_audits" not in captured["pre_review_contract"]


def test_stage_record_gives_every_cpa_bridge_call_its_full_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtitle = tmp_path / "candidate.recut.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n完整剧情\n",
        encoding="utf-8",
    )
    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"media")
    recut = finalization.FinalRecutArtifacts(
        recut_dir=tmp_path,
        media_path=media,
        subtitle_path=subtitle,
        text_manifest_path=None,
        text_manifest=None,
    )
    captured_configs: list[finalization.LlmConfig] = []

    def capture_llm_config(
        config: finalization.LlmConfig,
    ):
        captured_configs.append(config)

        def no_network_call(_prompt: str) -> str:
            raise AssertionError("captured LLM callable must not run")

        return no_network_call

    monkeypatch.setattr(
        finalization, "load_candidate_cover_reference", lambda *_a, **_k: None
    )
    monkeypatch.setattr(finalization, "build_llm_call", capture_llm_config)

    def stage(record: dict, **_kwargs) -> dict:
        record["publish_staging"] = {
            "title": "完整剧情",
            "title_authority_status": "READY",
            "cover_status": "SKIPPED",
        }
        return record

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=stage,
        generate_upload_tags=lambda *_a, **_k: {"status": "READY"},
        delivery_root=unused,
        run_exact_final_review=unused,
    )

    finalization._stage_record(
        options=finalization.ProducerFinalizationOptions(
            spec=tmp_path / "spec.json",
            substrate="source",
            correct="reviewed",
            speaker_mode="off",
            speaker_overrides=None,
            speaker_source_session_anchors=None,
            speaker_mixed_overlap_evidence=None,
            speaker_python=tmp_path / "python",
            reuse_cover=False,
        ),
        spec={
            "date": "2026-08-11",
            "pieces": [],
            "selection_hook": "完整剧情",
            "selection_scorecard": None,
            "classification": "talk",
        },
        cid="candidate",
        recut=recut,
        record={"artifact_hashes": {}},
        adapters=adapters,
    )

    assert len(captured_configs) == 3
    assert all(
        config.transport == "command"
        and "scripts/llm_via_cpa.sh" in (config.command_template or "")
        for config in captured_configs
    )
    assert [config.timeout_seconds for config in captured_configs] == [
        600.0,
        600.0,
        600.0,
    ]


def test_cover_audit_rejects_pre_repair_selection_hook_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_contract = {
        "schema_version": "lidousha-story-contract.v1",
        "selection_hook": "修复后的表妹主钩子",
        "relation_state": "NONE",
        "participants": [],
        "cover_counterpart_reference_available": False,
        "cover_reference_authority": None,
        "source_media_sha256s": [],
        "clip_context_binding": None,
        "boundary_semantic_review": None,
        "human_boundary_authority": None,
        "cover_fallback_mode": "HOST_ONLY_GENERIC",
    }
    stale_contract = dict(current_contract)
    stale_contract["selection_hook"] = "修复前的值妹主钩子"
    generation = {
        "cover_text": "修复后的表妹主钩子",
        "rendered_lines": ["修复后的表妹主钩子"],
        "story_contract": cover_story_contract_binding(stale_contract),
        "rendered_text_pixels": {
            "font_file_name": "font.ttf",
            "font_file_sha256": "sha256:" + "1" * 64,
            "mask_path": str(tmp_path / "mask.png"),
        },
        "final_cover": str(tmp_path / "cover.png"),
        "pre_overlay_path": str(tmp_path / "pre.png"),
        "ai_background": str(tmp_path / "background.png"),
    }
    monkeypatch.setattr(
        finalization,
        "audit_story_artifact",
        lambda *_a, **_k: {"status": "PASS", "violations": []},
    )
    monkeypatch.setattr(
        finalization,
        "validate_cover_route_decision",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        finalization,
        "validate_rendered_text_pixel_evidence",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        finalization,
        "resolve_trusted_cover_font",
        lambda *_a, **_k: tmp_path / "font.ttf",
    )
    monkeypatch.setattr(
        finalization,
        "verify_rendered_text_pixel_artifacts",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        finalization,
        "verify_pre_overlay_route_background",
        lambda *_a, **_k: True,
    )

    reasons, _audits = finalization._audit_story_bound_cover(
        {
            "cover_text": "修复后的表妹主钩子",
            "cover_generation": generation,
        },
        current_contract,
    )

    assert reasons == ["COVER_STORY_CONTRACT_BINDING_MISSING_OR_STALE"]


def test_cover_audit_rejects_814_shaped_full_title_thumbnail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = {
        "schema_version": "lidousha-story-contract.v1",
        "selection_hook": "转发生日信息能拿菲尔兹奖，小李追问为何自己没有。",
        "relation_state": "NONE",
        "participants": [],
        "cover_counterpart_reference_available": False,
        "cover_reference_authority": None,
        "source_media_sha256s": [],
        "clip_context_binding": None,
        "boundary_semantic_review": None,
        "human_boundary_authority": None,
        "cover_fallback_mode": "HOST_ONLY_GENERIC",
    }
    cover_text = (
        "SC称转发佐伯沙弥香生日信息能拿菲尔兹奖，"
        "小李追问“我也磕原点组怎么没有”，难道磕错了？"
    )
    generation = {
        "cover_text": cover_text,
        "cover_text_mode": "full",
        "rendered_lines": [
            "SC称转发",
            "佐伯沙弥香生",
            "日信息能拿",
            "菲尔兹奖，小",
            "李追问“我",
            "也磕原点组怎",
            "么没有”，",
            "难道磕错了？",
        ],
        "art_direction": {
            "cover_punch_semantic_review": {
                "status": "FAILED",
                "reason_code": "CPA_PUNCH_SEMANTIC_REVIEW_REJECTED",
            }
        },
        "story_contract": cover_story_contract_binding(contract),
        "rendered_text_pixels": {
            "font_file_name": "font.ttf",
            "font_file_sha256": "sha256:" + "1" * 64,
            "mask_path": str(tmp_path / "mask.png"),
        },
        "final_cover": str(tmp_path / "cover.png"),
        "pre_overlay_path": str(tmp_path / "pre.png"),
        "ai_background": str(tmp_path / "background.png"),
    }
    monkeypatch.setattr(
        finalization,
        "audit_story_artifact",
        lambda *_a, **_k: {"status": "PASS", "violations": []},
    )
    monkeypatch.setattr(
        finalization, "validate_cover_route_decision", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        finalization,
        "validate_rendered_text_pixel_evidence",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        finalization,
        "resolve_trusted_cover_font",
        lambda *_a, **_k: tmp_path / "font.ttf",
    )
    monkeypatch.setattr(
        finalization,
        "verify_rendered_text_pixel_artifacts",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        finalization,
        "verify_pre_overlay_route_background",
        lambda *_a, **_k: True,
    )

    reasons, _audits = finalization._audit_story_bound_cover(
        {"cover_text": cover_text, "cover_generation": generation},
        contract,
    )

    assert "COVER_PUNCH_REQUIRED_FOR_THUMBNAIL" in reasons
    assert "COVER_THUMBNAIL_TEXT_UNREADABLE" in reasons
    assert "COVER_FULL_TEXT_CONTRACT_MISSING_OR_INVALID" in reasons


def test_exact_final_review_gate_persists_deterministic_block(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "candidate.recut.srt"
    final_text = (
        "1\n00:00:00,000 --> 00:00:01,000\n仍有错误\n\n"
        "2\n00:00:01,100 --> 00:00:02,000\n完整收束\n"
    )
    subtitle.write_text(final_text, encoding="utf-8")
    # A temporary QC workspace can contain a cleaner-looking copy, but it is
    # not the active recut authority and must never be accepted for delivery.
    qc_clean = tmp_path / ".qc-upload-final" / "candidate.recut.srt"
    qc_clean.parent.mkdir()
    qc_clean.write_text(
        final_text.replace("仍有错误", "临时副本看起来已修好"),
        encoding="utf-8",
    )
    chat_path = tmp_path / "candidate.chat-authority.json"

    def exact_review(
        text,
        _authority,
        _timeline_offset_ms,
        _source_final_end_ms,
    ):
        return {
            "schema_version": "final-review-audit.v2",
            "status": "FLAGGED",
            "release_gate": "BLOCK",
            "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
            "reviewed_srt_sha256": (
                "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            ),
                "discovery": {
                    "status": "COMPLETE",
                    "explicit_empty_findings": False,
                },
                "correction_mutation_authority": {
                    "schema_version": "subtitle-correction-mutation-audit.v1",
                    "status": "PASS",
                    "applied_count": 0,
                    "validated_mutation_count": 0,
                    "failures": [],
                },
                "findings": [{"cue_index": 1, "suspect": "错误"}],
            "validated_finding_count": 1,
            "boundary_semantic_review": {
                    "schema_version": "talk-boundary-semantic-review.v1",
                    "status": "PASS",
                    "review_scope": "final_delivery",
                    "reason_codes": [],
                    "request_sha256": "sha256:" + "d" * 64,
                    "cue_grid_sha256": "sha256:" + "e" * 64,
                    "source_separation_witness": {
                        "schema_version": (
                            "talk-boundary-source-separation-witness.v1"
                        ),
                        "status": "PASS",
                        "source_review_sha256": "sha256:" + "a" * 64,
                        "source_request_sha256": "sha256:" + "b" * 64,
                        "source_cue_grid_sha256": "sha256:" + "c" * 64,
                        "source_final_start_ms": 0,
                        "source_final_end_ms": 9_400,
                        "reason_codes": [],
                    },
                    "final_endpoint_binding": {
                        "schema_version": "talk-boundary-final-endpoint-binding.v1",
                        "status": "PASS",
                        "recommended_end_cue_index": 1,
                        "recommended_end_ms": 9_000,
                        "final_closure_cue_index": 1,
                        "final_snapped_end_ms": 9_000,
                        "final_end_ms": 9_400,
                        "semantic_cue_grid_sha256": "sha256:" + "e" * 64,
                        "final_cue_grid_sha256": "sha256:" + "e" * 64,
                        "reason_codes": [],
                    },
                },
        }

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=unused,
        run_exact_final_review=exact_review,
    )
    with pytest.raises(
        SystemExit,
        match="FINAL_REVIEW_UNRESOLVED_FINDINGS",
    ):
        finalization._run_exact_final_review_gate(
            cid="candidate",
            out_root=tmp_path,
            final_start=0,
            final_end=9_400,
            recut=finalization.FinalRecutArtifacts(
                recut_dir=tmp_path,
                media_path=tmp_path / "candidate.recut.mp4",
                subtitle_path=subtitle,
                text_manifest_path=None,
                text_manifest=None,
            ),
            chat_authority_audit={},
            chat_authority_path=chat_path,
            adapters=adapters,
        )

    persisted = json.loads(
        (tmp_path / "candidate.review-flags.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["findings"][0]["suspect"] == "错误"
    assert subtitle.read_text(encoding="utf-8") == final_text
    assert persisted["release_gate"] == "BLOCK"
    assert persisted["reviewed_srt_sha256"] == (
        "sha256:" + hashlib.sha256(final_text.encode("utf-8")).hexdigest()
    )
    assert "临时副本看起来已修好" in qc_clean.read_text(
        encoding="utf-8"
    )


def test_exact_final_review_gate_self_heals_cpa_authorized_finding(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "candidate.recut.srt"
    original_cue = "这怎么怎么是 TTT 的头像"
    repaired_cue = "这怎么怎么是 ttt15 的头像"
    original_sha256 = hashlib.sha256(original_cue.encode("utf-8")).hexdigest()
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n"
        + original_cue
        + "\n\n2\n00:00:01,100 --> 00:00:02,000\n完整收束\n",
        encoding="utf-8",
    )
    chat_path = tmp_path / "candidate.chat-authority.json"
    baseline_audit_path = tmp_path / "candidate.redelivery-baseline.json"
    calls: list[str] = []
    (tmp_path / "candidate.final-review-carryover.json").write_text(
        json.dumps(
            {
                "schema_version": "final-review-carryover.v1",
                "findings": [
                    {
                        "cue": 1,
                        "base_text_sha256": original_sha256,
                        "kind": "context",
                        "suspect": "TTT",
                        "proposed_full_cue": repaired_cue,
                        "exact_release_adjudication": {
                            "schema_version": "subtitle-span-adjudication.v1",
                            "status": "OBSERVED",
                            "decision_authority": "CPA_JUDGE",
                            "repaired": True,
                            "timing_immutable": True,
                            "mutation_authority": {
                                "schema_version": (
                                    "subtitle-correction-mutation-authority.v1"
                                ),
                                "status": "PASS",
                            },
                            "request": {
                                "schema_version": (
                                    "subtitle-span-acoustic-check-request.v1"
                                ),
                                "request_sha256": "f" * 64,
                                "base_text_sha256": original_sha256,
                                "current_cue": original_cue,
                                "proposed_cue": repaired_cue,
                                "matched_start_ms": 0,
                                "matched_end_ms": 1_000,
                            },
                            "witness_judge": {
                                "judge": {
                                    "status": "JUDGED",
                                    "choice": "PROPOSED",
                                }
                            },
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def boundary_receipt() -> dict:
        return {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
            "review_scope": "final_delivery",
            "reason_codes": [],
            "request_sha256": "sha256:" + "d" * 64,
            "cue_grid_sha256": "sha256:" + "e" * 64,
            "source_separation_witness": {
                "schema_version": (
                    "talk-boundary-source-separation-witness.v1"
                ),
                "status": "PASS",
                "source_review_sha256": "sha256:" + "a" * 64,
                "source_request_sha256": "sha256:" + "b" * 64,
                "source_cue_grid_sha256": "sha256:" + "c" * 64,
                "source_final_start_ms": 0,
                "source_final_end_ms": 2_400,
                "reason_codes": [],
            },
            "final_endpoint_binding": {
                "schema_version": (
                    "talk-boundary-final-endpoint-binding.v1"
                ),
                "status": "PASS",
                "recommended_end_cue_index": 2,
                "recommended_end_ms": 2_000,
                "final_closure_cue_index": 2,
                "final_snapped_end_ms": 2_000,
                "final_end_ms": 2_400,
                "semantic_cue_grid_sha256": "sha256:" + "e" * 64,
                "final_cue_grid_sha256": "sha256:" + "e" * 64,
                "reason_codes": [],
            },
        }

    def mutation_audit() -> dict:
        return {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "PASS",
            "applied_count": 0,
            "validated_mutation_count": 0,
            "failures": [],
        }

    def exact_review(
        text,
        _authority,
        _timeline_offset_ms,
        _source_final_end_ms,
    ):
        calls.append(text)
        common = {
            "schema_version": "final-review-audit.v2",
            "reviewed_srt_sha256": (
                "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            ),
            "boundary_semantic_review": boundary_receipt(),
            "correction_mutation_authority": mutation_audit(),
            "correction_pass": {
                "findings": [
                    {
                        "cue_index": 1,
                        "base_text_sha256": original_sha256,
                        "suspect": "TTT",
                        "routed": "disclosure",
                        "carryover_replay_remap": {
                            "schema_version": (
                                "final-review-carryover-remap.v1"
                            ),
                            "status": "PASS",
                            "basis": "base_text_sha256",
                        },
                    }
                ]
            },
        }
        if original_cue in text:
            base_sha256 = original_sha256
            return {
                **common,
                "status": "FLAGGED",
                "release_gate": "BLOCK",
                "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
                "discovery": {
                    "status": "COMPLETE",
                    "explicit_empty_findings": False,
                },
                "findings": [
                    {
                        "cue_index": 1,
                        "base_text_sha256": base_sha256,
                        "proposed_full_cue": repaired_cue,
                        "exact_release_adjudication": {
                            "schema_version": (
                                "subtitle-span-adjudication.v1"
                            ),
                            "status": "OBSERVED",
                            "decision_authority": "CPA_JUDGE",
                            "repaired": True,
                            "timing_immutable": True,
                            "mutation_authority": {
                                "schema_version": (
                                    "subtitle-correction-mutation-authority.v1"
                                ),
                                "status": "PASS",
                                "basis": (
                                    "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION"
                                ),
                            },
                            "request": {
                                "schema_version": (
                                    "subtitle-span-acoustic-check-request.v1"
                                ),
                                "request_sha256": "f" * 64,
                                "base_text_sha256": base_sha256,
                                "current_cue": original_cue,
                                "proposed_cue": repaired_cue,
                            },
                            "witness_judge": {
                                "judge": {
                                    "schema_version": (
                                        "acoustic-witness-adjudication.v1"
                                    ),
                                    "status": "JUDGED",
                                    "choice": "PROPOSED",
                                }
                            },
                        },
                    }
                ],
                "validated_finding_count": 1,
            }
        return {
            **common,
            "status": "CLEAN",
            "release_gate": "PASS",
            "reason_codes": [],
            "discovery": {
                "status": "COMPLETE",
                "explicit_empty_findings": True,
            },
            "findings": [],
            "validated_finding_count": 0,
        }

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=unused,
        run_exact_final_review=exact_review,
    )
    baseline_audit: dict[str, object] = {"status": "APPLIED"}
    chat_authority_audit = {
        "entity_repairs": [
            {
                "mode": "final_review_context_adjudication",
                "matched_start_ms": 0,
                "matched_end_ms": 1_000,
                "before": ["older ASR text"],
                "after": [original_cue],
                "structured_exact_text": original_cue,
            }
        ]
    }
    result = finalization._run_exact_final_review_gate(
        cid="candidate",
        out_root=tmp_path,
        final_start=0,
        final_end=2_400,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=tmp_path / "candidate.recut.mp4",
            subtitle_path=subtitle,
            text_manifest_path=None,
            text_manifest=None,
            redelivery_baseline_audit_path=baseline_audit_path,
            redelivery_baseline_audit=baseline_audit,
        ),
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_path,
        adapters=adapters,
    )

    assert result["status"] == "CLEAN"
    assert len(calls) == 2
    assert original_cue in calls[0]
    assert repaired_cue in calls[1]
    assert repaired_cue in subtitle.read_text(encoding="utf-8")
    rows = chat_authority_audit["entity_repairs"]
    assert len(rows) == 2
    assert rows[0]["reconciliation"]["status"] == (
        "SUPERSEDED_BY_EXACT_FINAL_CPA"
    )
    assert rows[1]["mode"] == "exact_final_cpa_self_heal"
    assert rows[1]["structured_exact_text"] == repaired_cue
    assert rows[1]["matched_start_ms"] == 0
    assert rows[1]["matched_end_ms"] == 1_000
    assert rows[1]["boundary_required"] is False
    assert rows[1]["boundary_owner_rejection"] == (
        "POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER"
    )
    assert chat_authority_audit[
        "exact_final_cpa_surface_registrations"
    ][0]["superseded_entity_repair_indexes"] == [0]
    assert finalization.verify_chat_authority_final_surfaces(
        chat_authority_audit,
        final_text_srt=subtitle.read_text(encoding="utf-8"),
        final_speaker_srt=subtitle.read_text(encoding="utf-8"),
        delivery_start_ms=0,
        delivery_end_ms=2_400,
    )
    # Packages produced before explicit surface registrations retain only the
    # hash-bound self-heal chain. The same exact cue/time/hash proof must retire
    # the older correction owner without trusting a generic overlap.
    legacy_audit = {
        "entity_repairs": [
            {
                "mode": "final_review_context_adjudication",
                "matched_start_ms": 0,
                "matched_end_ms": 1_000,
                "before": ["older ASR text"],
                "after": [original_cue],
                "structured_exact_text": original_cue,
            }
        ],
        "exact_final_cpa_self_heal": result[
            "exact_final_cpa_self_heal"
        ],
    }
    assert finalization.verify_chat_authority_final_surfaces(
        legacy_audit,
        final_text_srt=subtitle.read_text(encoding="utf-8"),
        final_speaker_srt=subtitle.read_text(encoding="utf-8"),
        delivery_start_ms=0,
        delivery_end_ms=2_400,
    )
    assert legacy_audit["entity_repairs"][0]["reconciliation"][
        "status"
    ] == "SUPERSEDED_BY_EXACT_FINAL_CPA"
    assert legacy_audit[
        "final_superseded_by_exact_final_cpa_count"
    ] == 1
    assert result["exact_final_cpa_self_heal"]["status"] == "PASS"
    assert baseline_audit["exact_final_cpa_self_heal"]["status"] == "PASS"
    assert json.loads(
        baseline_audit_path.read_text(encoding="utf-8")
    )["exact_final_cpa_self_heal"]["status"] == "PASS"


def test_exact_final_review_gate_suppresses_baseline_owned_finding(
    tmp_path: Path,
) -> None:
    """zsm8 shape (2026-08-08): baseline-applied cues 52/58/59 must survive
    self-heal untouched, and the final owner-verifier must then PASS.

    redelivery_subtitle_baseline.py writes owned_intervals for cues whose
    text came from an Ivan-reviewed baseline diff, but nothing previously
    read it back: the exact-final CPA self-heal channel independently
    re-listened to the same window and overwrote the reviewed text. This
    reproduces that shape at the gate boundary — a CPA-authorized finding
    targeting a baseline-owned cue must be dropped with a typed
    ``BASELINE_OWNED_CUE_SELF_HEAL_SUPPRESSED`` receipt instead of applied.
    """

    subtitle = tmp_path / "candidate.recut.srt"
    baseline_true_cue = "我想死在你手里吗"
    self_heal_proposed_cue = "哈哈，我的信原来在你手里吗"
    baseline_sha256 = hashlib.sha256(
        baseline_true_cue.encode("utf-8")
    ).hexdigest()
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n"
        + baseline_true_cue
        + "\n\n2\n00:00:01,100 --> 00:00:02,000\n完整收束\n",
        encoding="utf-8",
    )
    chat_path = tmp_path / "candidate.chat-authority.json"
    baseline_audit_path = tmp_path / "candidate.redelivery-baseline.json"
    calls: list[str] = []

    def boundary_receipt() -> dict:
        return {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
            "review_scope": "final_delivery",
            "reason_codes": [],
            "request_sha256": "sha256:" + "d" * 64,
            "cue_grid_sha256": "sha256:" + "e" * 64,
            "source_separation_witness": {
                "schema_version": (
                    "talk-boundary-source-separation-witness.v1"
                ),
                "status": "PASS",
                "source_review_sha256": "sha256:" + "a" * 64,
                "source_request_sha256": "sha256:" + "b" * 64,
                "source_cue_grid_sha256": "sha256:" + "c" * 64,
                "source_final_start_ms": 0,
                "source_final_end_ms": 2_400,
                "reason_codes": [],
            },
            "final_endpoint_binding": {
                "schema_version": (
                    "talk-boundary-final-endpoint-binding.v1"
                ),
                "status": "PASS",
                "recommended_end_cue_index": 2,
                "recommended_end_ms": 2_000,
                "final_closure_cue_index": 2,
                "final_snapped_end_ms": 2_000,
                "final_end_ms": 2_400,
                "semantic_cue_grid_sha256": "sha256:" + "e" * 64,
                "final_cue_grid_sha256": "sha256:" + "e" * 64,
                "reason_codes": [],
            },
        }

    def mutation_audit() -> dict:
        return {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "PASS",
            "applied_count": 0,
            "validated_mutation_count": 0,
            "failures": [],
        }

    def exact_review(
        text,
        _authority,
        _timeline_offset_ms,
        _source_final_end_ms,
    ):
        calls.append(text)
        return {
            "schema_version": "final-review-audit.v2",
            "reviewed_srt_sha256": (
                "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            ),
            "boundary_semantic_review": boundary_receipt(),
            "correction_mutation_authority": mutation_audit(),
            "correction_pass": {"findings": []},
            "status": "FLAGGED",
            "release_gate": "BLOCK",
            "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
            "discovery": {
                "status": "COMPLETE",
                "explicit_empty_findings": False,
            },
            "findings": [
                {
                    "cue_index": 1,
                    "base_text_sha256": baseline_sha256,
                    "proposed_full_cue": self_heal_proposed_cue,
                    "exact_release_adjudication": {
                        "schema_version": (
                            "subtitle-span-adjudication.v1"
                        ),
                        "status": "OBSERVED",
                        "decision_authority": "CPA_JUDGE",
                        "repaired": True,
                        "timing_immutable": True,
                        "mutation_authority": {
                            "schema_version": (
                                "subtitle-correction-mutation-authority.v1"
                            ),
                            "status": "PASS",
                        },
                        "request": {
                            "schema_version": (
                                "subtitle-span-acoustic-check-request.v1"
                            ),
                            "request_sha256": "f" * 64,
                            "base_text_sha256": baseline_sha256,
                            "current_cue": baseline_true_cue,
                            "proposed_cue": self_heal_proposed_cue,
                        },
                        "witness_judge": {
                            "judge": {
                                "status": "JUDGED",
                                "choice": "PROPOSED",
                            }
                        },
                    },
                }
            ],
            "validated_finding_count": 1,
        }

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=unused,
        run_exact_final_review=exact_review,
    )
    baseline_audit: dict[str, object] = {
        "status": "APPLIED",
        "owned_intervals": [{"start_ms": 0, "end_ms": 1_000}],
        "mappings": [
            {
                "mapping_kind": "exact_reviewed_interval_replay",
                "baseline_cue_index": 1,
                "output_cue_index": 1,
                "start_ms": 0,
                "end_ms": 1_000,
                "text": baseline_true_cue,
            }
        ],
    }
    # Production wires the same baseline audit object into both the recut
    # (self-heal reads owned_intervals from here) and the chat authority
    # (the final owner-verifier reads mappings from here) — see
    # ``_materialize_final_recut``'s
    # ``chat_authority_audit["redelivery_subtitle_baseline_audit"] =
    # redelivery_baseline_audit`` binding.
    chat_authority_audit: dict[str, object] = {
        "redelivery_subtitle_baseline_audit": baseline_audit,
    }

    result = finalization._run_exact_final_review_gate(
        cid="candidate",
        out_root=tmp_path,
        final_start=0,
        final_end=2_400,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=tmp_path / "candidate.recut.mp4",
            subtitle_path=subtitle,
            text_manifest_path=None,
            text_manifest=None,
            redelivery_baseline_audit_path=baseline_audit_path,
            redelivery_baseline_audit=baseline_audit,
        ),
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_path,
        adapters=adapters,
    )

    assert result["status"] == "CLEAN"
    assert result["release_gate"] == "PASS"
    assert result["findings"] == []
    # Never retried/repaired: suppression clears the gate on the first pass.
    assert len(calls) == 1
    assert baseline_true_cue in subtitle.read_text(encoding="utf-8")
    assert self_heal_proposed_cue not in subtitle.read_text(encoding="utf-8")
    suppressed = result["baseline_owned_findings_suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0]["reason_code"] == (
        "BASELINE_OWNED_CUE_SELF_HEAL_SUPPRESSED"
    )
    assert suppressed[0]["cue_index"] == 1
    assert suppressed[0]["owned_interval"] == {
        "start_ms": 0,
        "end_ms": 1_000,
    }
    # No self-heal pass ever ran, so entity_repairs stays untouched.
    assert "exact_final_cpa_self_heal" not in chat_authority_audit
    # The final owner-verifier (the gate zsm8 actually failed at) must PASS
    # now that self-heal left the baseline-owned cue untouched.
    assert finalization.verify_chat_authority_final_surfaces(
        chat_authority_audit,
        final_text_srt=subtitle.read_text(encoding="utf-8"),
        final_speaker_srt=subtitle.read_text(encoding="utf-8"),
        delivery_start_ms=0,
        delivery_end_ms=2_400,
    )
    assert "final_verification_failure" not in chat_authority_audit


def test_exact_final_review_gate_suppresses_chat_authority_owned_finding(
    tmp_path: Path,
) -> None:
    """2026-08-11 主包/主播 shape (Ivan 2026-08-19 审片裁定 #2「弹幕不修正」):
    a cue whose text chat authority already applied verbatim from matched
    danmaku evidence (「主包给…」, a livestream meme spelling of 主播) must
    survive self-heal untouched — a general-text self-heal judge, unaware the
    span is chat-verbatim-owned, must not be allowed to "correct" the meme
    spelling back to 主播.

    Uses a **nonzero** ``final_start`` (chat authority's ``matched_start_ms``/
    ``matched_end_ms`` are in the full-session/global timeline, like every
    other such field in this repo; ``final_text``'s cues are delivery-local)
    to catch the offset-rebase bug this suppression must not have.
    """

    danmu_text = "主包给我讲讲这是怎么回事"
    self_heal_proposed_cue = "主播给我讲讲这是怎么回事"
    danmu_sha256 = hashlib.sha256(danmu_text.encode("utf-8")).hexdigest()
    delivery_start_ms = 200_000
    delivery_end_ms = 202_400

    subtitle = tmp_path / "candidate.recut.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n"
        + danmu_text
        + "\n\n2\n00:00:01,100 --> 00:00:02,000\n完整收束\n",
        encoding="utf-8",
    )
    chat_path = tmp_path / "candidate.chat-authority.json"
    calls: list[str] = []

    def boundary_receipt() -> dict:
        return {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
            "review_scope": "final_delivery",
            "reason_codes": [],
            "request_sha256": "sha256:" + "d" * 64,
            "cue_grid_sha256": "sha256:" + "e" * 64,
            "source_separation_witness": {
                "schema_version": (
                    "talk-boundary-source-separation-witness.v1"
                ),
                "status": "PASS",
                "source_review_sha256": "sha256:" + "a" * 64,
                "source_request_sha256": "sha256:" + "b" * 64,
                "source_cue_grid_sha256": "sha256:" + "c" * 64,
                "source_final_start_ms": 0,
                "source_final_end_ms": 2_400,
                "reason_codes": [],
            },
            "final_endpoint_binding": {
                "schema_version": (
                    "talk-boundary-final-endpoint-binding.v1"
                ),
                "status": "PASS",
                "recommended_end_cue_index": 2,
                "recommended_end_ms": 2_000,
                "final_closure_cue_index": 2,
                "final_snapped_end_ms": 2_000,
                "final_end_ms": 2_400,
                "semantic_cue_grid_sha256": "sha256:" + "e" * 64,
                "final_cue_grid_sha256": "sha256:" + "e" * 64,
                "reason_codes": [],
            },
        }

    def mutation_audit() -> dict:
        return {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "PASS",
            "applied_count": 0,
            "validated_mutation_count": 0,
            "failures": [],
        }

    def exact_review(
        text,
        _authority,
        _timeline_offset_ms,
        _source_final_end_ms,
    ):
        calls.append(text)
        return {
            "schema_version": "final-review-audit.v2",
            "reviewed_srt_sha256": (
                "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            ),
            "boundary_semantic_review": boundary_receipt(),
            "correction_mutation_authority": mutation_audit(),
            "correction_pass": {"findings": []},
            "status": "FLAGGED",
            "release_gate": "BLOCK",
            "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
            "discovery": {
                "status": "COMPLETE",
                "explicit_empty_findings": False,
            },
            "findings": [
                {
                    "cue_index": 1,
                    "base_text_sha256": danmu_sha256,
                    "proposed_full_cue": self_heal_proposed_cue,
                    "exact_release_adjudication": {
                        "schema_version": (
                            "subtitle-span-adjudication.v1"
                        ),
                        "status": "OBSERVED",
                        "decision_authority": "CPA_JUDGE",
                        "repaired": True,
                        "timing_immutable": True,
                        "mutation_authority": {
                            "schema_version": (
                                "subtitle-correction-mutation-authority.v1"
                            ),
                            "status": "PASS",
                        },
                        "request": {
                            "schema_version": (
                                "subtitle-span-acoustic-check-request.v1"
                            ),
                            "request_sha256": "f" * 64,
                            "base_text_sha256": danmu_sha256,
                            "current_cue": danmu_text,
                            "proposed_cue": self_heal_proposed_cue,
                        },
                        "witness_judge": {
                            "judge": {
                                "status": "JUDGED",
                                "choice": "PROPOSED",
                            }
                        },
                    },
                }
            ],
            "validated_finding_count": 1,
        }

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=unused,
        run_exact_final_review=exact_review,
    )
    # matched_start_ms/matched_end_ms are global-timeline, like every other
    # chat-authority matched window; delivery_start_ms offsets them into the
    # delivered clip's local SRT time (cue1 is at local 0-1_000ms).
    chat_authority_audit: dict[str, object] = {
        "applied": [
            {
                "evidence_id": "danmu-1",
                "kind": "danmaku",
                "exact_text": danmu_text,
                "cue_indexes": [1],
                "matched_start_ms": delivery_start_ms,
                "matched_end_ms": delivery_start_ms + 1_000,
                "mode": "exact_span",
            }
        ],
    }

    result = finalization._run_exact_final_review_gate(
        cid="candidate",
        out_root=tmp_path,
        final_start=delivery_start_ms,
        final_end=delivery_end_ms,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=tmp_path / "candidate.recut.mp4",
            subtitle_path=subtitle,
            text_manifest_path=None,
            text_manifest=None,
            redelivery_baseline_audit_path=None,
            redelivery_baseline_audit=None,
        ),
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_path,
        adapters=adapters,
    )

    assert result["status"] == "CLEAN"
    assert result["release_gate"] == "PASS"
    assert result["findings"] == []
    # Never retried/repaired: suppression clears the gate on the first pass.
    assert len(calls) == 1
    assert danmu_text in subtitle.read_text(encoding="utf-8")
    assert self_heal_proposed_cue not in subtitle.read_text(encoding="utf-8")
    suppressed = result["chat_authority_owned_findings_suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0]["reason_code"] == (
        "CHAT_AUTHORITY_OWNED_CUE_SELF_HEAL_SUPPRESSED"
    )
    assert suppressed[0]["cue_index"] == 1
    # Rebased into delivery-local ms (global 200_000-201_000 minus the
    # 200_000ms delivery start): must equal the recut SRT's own cue1 window.
    assert suppressed[0]["owned_interval"] == {
        "start_ms": 0,
        "end_ms": 1_000,
    }
    assert "exact_final_cpa_self_heal" not in chat_authority_audit


def test_exact_final_self_heal_registration_failure_rolls_back_all_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subtitle = tmp_path / "candidate.recut.srt"
    original_srt = (
        b"1\n00:00:00,000 --> 00:00:01,000\n"
        b"original exact-final bytes\n"
    )
    subtitle.write_bytes(original_srt)
    chat_path = tmp_path / "candidate.chat-authority.json"
    review_path = tmp_path / "candidate.review-flags.json"
    baseline_path = tmp_path / "candidate.redelivery-baseline.json"
    original_chat_bytes = b'{"persisted":"chat-before"}\n'
    original_review_bytes = b'{"persisted":"review-before"}\n'
    original_baseline_bytes = b'{"persisted":"baseline-before"}\n'
    chat_path.write_bytes(original_chat_bytes)
    review_path.write_bytes(original_review_bytes)
    baseline_path.write_bytes(original_baseline_bytes)
    chat_authority_audit = {"sentinel": {"value": "chat-before"}}
    baseline_audit = {"sentinel": {"value": "baseline-before"}}
    expected_chat = deepcopy(chat_authority_audit)
    expected_baseline = deepcopy(baseline_audit)
    repaired_srt = original_srt.decode("utf-8").replace(
        "original exact-final bytes",
        "CPA repaired bytes",
    )
    repair = {
        "before_sha256": "sha256:"
        + hashlib.sha256(b"original exact-final bytes").hexdigest(),
        "after_sha256": "sha256:"
        + hashlib.sha256(b"CPA repaired bytes").hexdigest(),
    }

    def exact_review(text, *_args):
        return {
            "schema_version": "final-review-audit.v2",
            "reviewed_srt_sha256": "sha256:"
            + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "findings": [{"cue_index": 1}],
        }

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=unused,
        run_exact_final_review=exact_review,
    )
    monkeypatch.setattr(
        finalization,
        "validate_final_review_release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            finalization.FinalReviewContractError(
                "FINAL_REVIEW_UNRESOLVED_FINDINGS"
            )
        ),
    )
    monkeypatch.setattr(
        finalization,
        "_apply_exact_final_cpa_repairs",
        lambda *_args, **_kwargs: (repaired_srt, [repair]),
    )
    monkeypatch.setattr(
        finalization,
        "_register_exact_final_cpa_repairs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("registration failed")
        ),
    )

    with pytest.raises(RuntimeError, match="registration failed"):
        finalization._run_exact_final_review_gate(
            cid="candidate",
            out_root=tmp_path,
            final_start=0,
            final_end=1_000,
            recut=finalization.FinalRecutArtifacts(
                recut_dir=tmp_path,
                media_path=tmp_path / "candidate.recut.mp4",
                subtitle_path=subtitle,
                text_manifest_path=None,
                text_manifest=None,
                redelivery_baseline_audit_path=baseline_path,
                redelivery_baseline_audit=baseline_audit,
            ),
            chat_authority_audit=chat_authority_audit,
            chat_authority_path=chat_path,
            adapters=adapters,
        )

    assert subtitle.read_bytes() == original_srt
    assert chat_authority_audit == expected_chat
    assert baseline_audit == expected_baseline
    assert chat_path.read_bytes() == original_chat_bytes
    assert review_path.read_bytes() == original_review_bytes
    assert baseline_path.read_bytes() == original_baseline_bytes


def test_exact_final_self_heal_uses_cpa_request_target_after_span_rejection():
    from src.autoslice import producer_package_finalization as finalization

    current = "就是面部的时候没有什么制作这个机体"
    proposed = "就是制作这个机体"
    base_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
    srt_text = f"1\n00:00:00,000 --> 00:00:02,000\n{current}\n"
    audit = {
        "findings": [
            {
                "cue_index": 1,
                "base_text_sha256": base_sha256,
                "proposed_full_cue": None,
                "suggestion_rejected_reason": "EDIT_LENGTH_DELTA_TOO_LARGE",
                "exact_release_adjudication": {
                    "schema_version": "subtitle-span-adjudication.v1",
                    "status": "OBSERVED",
                    "decision_authority": "CPA_JUDGE",
                    "repaired": True,
                    "timing_immutable": True,
                    "mutation_authority": {
                        "schema_version": (
                            "subtitle-correction-mutation-authority.v1"
                        ),
                        "status": "PASS",
                    },
                    "request": {
                        "schema_version": (
                            "subtitle-span-acoustic-check-request.v1"
                        ),
                        "request_sha256": "f" * 64,
                        "base_text_sha256": base_sha256,
                        "current_cue": current,
                        "proposed_cue": proposed,
                    },
                    "witness_judge": {
                        "judge": {
                            "status": "JUDGED",
                            "choice": "PROPOSED",
                        }
                    },
                },
            }
        ]
    }

    repaired, receipts = finalization._apply_exact_final_cpa_repairs(
        srt_text,
        audit,
    )

    assert current not in repaired
    assert proposed in repaired
    assert receipts[0]["before"] == current
    assert receipts[0]["after"] == proposed


def _exact_source_transcript_self_heal_fixture():
    from src.autoslice.acoustic_witness_adjudication import (
        build_witness_request,
    )
    from src.autoslice.exact_source_transcript_contract import (
        build_exact_source_transcript_request,
        seal_exact_source_transcript_observation,
    )
    from src.autoslice.exact_source_transcript_provider import (
        rebuild_candidate_from_exact_source_transcript,
    )
    from src.autoslice.final_review_auditor import (
        _derive_single_span_edit,
        build_context_adjudication_request,
    )

    current = "难听难听，对吧"
    proposed = "nineteen nineteen，对吧"
    srt_text = (
        "1\n00:00:05,000 --> 00:00:09,000\n"
        f"{current}\n"
    )
    clip_context = {
        "schema_version": "clip-context.v1",
        "candidate_id": "auto_213135_806_1068",
        "context_sha256": "sha256:" + "d" * 64,
        "whole_clip_draft_srt_sha256": "sha256:" + "e" * 64,
    }
    initial_finding = {
        "cue_index": 1,
        "suspect": "难听难听",
        "suggestion": "南町nightin",
        "proposed_full_cue": "南町nightin，对吧",
        "repair_class": "phonetic",
    }
    initial_request = build_context_adjudication_request(
        srt_text,
        initial_finding,
        clip_context=clip_context,
    )
    witness_request = build_witness_request(initial_request)
    timeline = {
        "schema_version": "subtitle-audio-timeline-binding.v1",
        "source_media_timeline_offset_ms": 0,
        "delivery_local": {
            "target_start_ms": 5_000,
            "target_end_ms": 9_000,
            "context_start_ms": 4_500,
            "context_end_ms": 9_500,
        },
        "source_media": {
            "target_start_ms": 5_000,
            "target_end_ms": 9_000,
            "crop_start_ms": 4_600,
            "crop_end_ms": 9_400,
        },
    }
    witness = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": "blind_pinyin",
        "request_sha256": witness_request["request_sha256"],
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": "nan ting nan ting dui ba",
        "uncertain_positions": [],
        "syllable_count": 6,
        "confidence": 0.9,
        "source_media_sha256": "a" * 64,
        "audio_clip_sha256": "b" * 64,
        "audio_start_ms": 4_600,
        "audio_end_ms": 9_400,
        "timeline_binding": timeline,
    }
    physical_request = build_exact_source_transcript_request(
        witness_request
    )
    observation = seal_exact_source_transcript_observation(
        request=physical_request,
        exact_transcript=proposed,
        audible_language="mixed",
        source_media_sha256="a" * 64,
        audio_clip_sha256="b" * 64,
        provider="agy",
        model="Gemini 3.6 Flash (High)",
        response_sha256="c" * 64,
        timeline_binding=timeline,
    )
    rebuilt_finding, handoff = rebuild_candidate_from_exact_source_transcript(
        finding=initial_finding,
        initial_check_request=initial_request,
        witness_request=witness_request,
        witness=witness,
        srt_text=srt_text,
        clip_context=clip_context,
        provider=lambda _request: observation,
        derive_single_span_edit=_derive_single_span_edit,
    )
    assert rebuilt_finding is not None
    request = build_context_adjudication_request(
        srt_text,
        rebuilt_finding,
        clip_context=clip_context,
    )
    base_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
    finding = {
        **rebuilt_finding,
        "base_text_sha256": base_sha256,
        "exact_release_adjudication": {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "decision_authority": "CPA_JUDGE",
            "repaired": True,
            "timing_immutable": True,
            "mutation_authority": {
                "schema_version": (
                    "subtitle-correction-mutation-authority.v1"
                ),
                "status": "PASS",
            },
            "request": request,
            "verdict": witness,
            "witness_judge": {
                "judge": {
                    "status": "JUDGED",
                    "choice": "PROPOSED",
                }
            },
            "exact_source_transcript_handoff": handoff,
        },
    }
    return srt_text, finding, clip_context


def test_exact_source_transcript_same_pass_apply_requires_full_handoff():
    from copy import deepcopy

    from src.autoslice import producer_package_finalization as finalization

    srt_text, finding, clip_context = (
        _exact_source_transcript_self_heal_fixture()
    )
    repaired, receipts = finalization._apply_exact_final_cpa_repairs(
        srt_text,
        {"findings": [finding]},
        clip_context=clip_context,
    )
    assert "nineteen nineteen，对吧" in repaired
    assert len(receipts) == 1

    stripped = deepcopy(finding)
    stripped["exact_release_adjudication"].pop(
        "exact_source_transcript_handoff"
    )
    unchanged, receipts = finalization._apply_exact_final_cpa_repairs(
        srt_text,
        {"findings": [stripped]},
        clip_context=clip_context,
    )
    assert unchanged == srt_text
    assert receipts == []

    unchanged, receipts = finalization._apply_exact_final_cpa_repairs(
        srt_text,
        {"findings": [finding]},
        clip_context={},
    )
    assert unchanged == srt_text
    assert receipts == []

    tampered = deepcopy(finding)
    adjudication = tampered["exact_release_adjudication"]
    adjudication["exact_source_transcript_handoff"]["receipt_sha256"] = (
        "sha256:" + "f" * 64
    )
    adjudication["request"]["exact_source_transcript_handoff"][
        "receipt_sha256"
    ] = "sha256:" + "f" * 64
    unchanged, receipts = finalization._apply_exact_final_cpa_repairs(
        srt_text,
        {"findings": [tampered]},
        clip_context=clip_context,
    )
    assert unchanged == srt_text
    assert receipts == []

    drifted_context = {**clip_context, "context_sha256": "sha256:" + "f" * 64}
    unchanged, receipts = finalization._apply_exact_final_cpa_repairs(
        srt_text,
        {"findings": [finding]},
        clip_context=drifted_context,
    )
    assert unchanged == srt_text
    assert receipts == []


def test_exact_source_transcript_checkpoint_and_replay_keep_strict_receipt(
    tmp_path: Path,
):
    from copy import deepcopy

    from src.autoslice import producer_package_finalization as finalization
    from src.autoslice.final_review_carryover import (
        carryover_path,
        checkpoint_final_review_carryover,
        load_replayable_final_review_carryover,
        persist_final_review_carryover,
    )

    srt_text, finding, clip_context = (
        _exact_source_transcript_self_heal_fixture()
    )
    reviewed_sha256 = "sha256:" + hashlib.sha256(
        srt_text.encode("utf-8")
    ).hexdigest()
    audit = {
        "schema_version": "final-review-audit.v2",
        "reviewed_srt_sha256": reviewed_sha256,
        "findings": [finding],
    }
    path = carryover_path(tmp_path, "auto_213135_806_1068")
    assert checkpoint_final_review_carryover(path, audit) == 1
    checkpoint_rows = load_replayable_final_review_carryover(path)
    assert checkpoint_rows[0]["exact_release_adjudication"][
        "exact_source_transcript_handoff"
    ]["receipt_sha256"]

    assert persist_final_review_carryover(path, audit) == 1
    replayed = finalization._replayable_exact_final_carryover_findings(
        srt_text,
        path,
        clip_context=clip_context,
    )
    assert len(replayed) == 1

    drifted_srt = srt_text + (
        "\n2\n00:00:10,000 --> 00:00:11,000\n新增上下文\n"
    )
    assert finalization._replayable_exact_final_carryover_findings(
        drifted_srt,
        path,
        clip_context=clip_context,
    ) == []

    drifted_context = {**clip_context, "context_sha256": "sha256:" + "f" * 64}
    assert finalization._replayable_exact_final_carryover_findings(
        srt_text,
        path,
        clip_context=drifted_context,
    ) == []
    assert finalization._replayable_exact_final_carryover_findings(
        srt_text,
        path,
        clip_context={},
    ) == []

    tampered = deepcopy(finding)
    tampered["exact_release_adjudication"].pop(
        "exact_source_transcript_handoff"
    )
    tampered_path = carryover_path(tmp_path, "tampered")
    assert checkpoint_final_review_carryover(
        tampered_path,
        {**audit, "findings": [tampered]},
    ) == 0
    assert load_replayable_final_review_carryover(tampered_path) == []


def test_exact_source_transcript_checkpoint_reenters_correction_with_receipt(
    tmp_path: Path,
):
    from src.autoslice.final_review_auditor import (
        adjudicate_context_finding,
        audit_final_subtitles,
    )
    from src.autoslice.final_review_carryover import (
        carryover_path,
        checkpoint_final_review_carryover,
        load_replayable_final_review_carryover,
    )

    srt_text, finding, clip_context = (
        _exact_source_transcript_self_heal_fixture()
    )
    path = carryover_path(tmp_path, "auto_213135_806_1068")
    assert checkpoint_final_review_carryover(
        path,
        {
            "schema_version": "final-review-audit.v2",
            "reviewed_srt_sha256": "sha256:"
            + hashlib.sha256(srt_text.encode("utf-8")).hexdigest(),
            "findings": [finding],
        },
    ) == 1
    normalized = audit_final_subtitles(
        srt_text,
        llm_call=lambda _prompt: '{"findings":[]}',
        extract_json=json.loads,
        candidate_context=clip_context,
        extra_raw_findings=load_replayable_final_review_carryover(
            path
        ),
        prioritize_extra_raw_findings=True,
    )
    assert len(normalized) == 1
    assert normalized[0]["candidate_provenance"]["kind"] == (
        "bounded_exact_source_audio_transcript"
    )
    assert normalized[0]["exact_release_adjudication"][
        "exact_source_transcript_handoff"
    ]["receipt_sha256"]

    handoff = finding["exact_release_adjudication"][
        "exact_source_transcript_handoff"
    ]

    def fresh_witness(request):
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "blind_pinyin",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": "nan ting nan ting dui ba",
            "uncertain_positions": [],
            "syllable_count": 6,
            "confidence": 0.9,
            "source_media_sha256": handoff["source_media_sha256"],
            "audio_clip_sha256": handoff["audio_clip_sha256"],
            "audio_start_ms": handoff["timeline_binding"][
                "source_media"
            ]["crop_start_ms"],
            "audio_end_ms": handoff["timeline_binding"][
                "source_media"
            ]["crop_end_ms"],
            "timeline_binding": handoff["timeline_binding"],
        }

    repaired, adjudication = adjudicate_context_finding(
        srt_text,
        normalized[0],
        entity_verifier=fresh_witness,
        judge_llm_call=lambda _prompt: json.dumps(
            {"choice": "PROPOSED"}
        ),
        clip_context=clip_context,
    )
    assert "nineteen nineteen，对吧" in repaired
    assert adjudication["mutation_authority"]["status"] == "PASS"
    assert adjudication["witness_judge"]["witness_conflict_gate"][
        "exact_source_transcript_handoff"
    ] is True


def test_exact_final_typed_drop_removes_cue_renumbers_and_registers_owner():
    current = "咳咳"
    base_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
    srt_text = (
        "1\n00:00:00,000 --> 00:00:01,000\n咳咳\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\n保留下一句\n"
    )
    witness_request_sha256 = "a" * 64
    finding = {
        "cue_index": 1,
        "base_text_sha256": base_sha256,
        "proposed_full_cue": "",
        "repair_class": "acoustic_drop_cue",
        "exact_release_adjudication": {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "decision_authority": "CPA_JUDGE",
            "witness_authority": "EVIDENCE_ONLY",
            "repaired": True,
            "timing_immutable": True,
            "policy_branch": "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE",
            "mutation_authority": {
                "schema_version": (
                    "subtitle-correction-mutation-authority.v1"
                ),
                "status": "PASS",
                "basis": "CPA_EXPLICIT_INAUDIBLE_DROP",
            },
            "request": {
                "schema_version": (
                    "subtitle-span-acoustic-check-request.v1"
                ),
                "request_sha256": "f" * 64,
                "base_text_sha256": base_sha256,
                "current_cue": current,
                "proposed_cue": "",
                "repair_class": "acoustic_drop_cue",
            },
            "verdict": {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "status": "OBSERVED",
                "request_sha256": witness_request_sha256,
                "target_audible": False,
            },
            "witness_judge": {
                "decision_authority": "CPA_JUDGE",
                "witness_authority": "EVIDENCE_ONLY",
                "selected_action": "DROP_CUE",
                "selected_repair_class": "acoustic_drop_cue",
                "selected_target_cue": "",
                "judge": {
                    "schema_version": "acoustic-witness-adjudication.v1",
                    "status": "JUDGED",
                    "choice": "DROP",
                    "prompt_sha256": "b" * 64,
                    "completion_sha256": "c" * 64,
                    "check_request_sha256": "f" * 64,
                    "decision_contract": (
                        "inaudible-current-proposed-drop.v1"
                    ),
                    "choice_set": ["CURRENT", "DROP", "PROPOSED"],
                }
            },
            "drop_authority": {
                "schema_version": (
                    "subtitle-cpa-inaudible-drop-authority.v1"
                ),
                "status": "PASS",
                "decision_authority": "CPA_JUDGE",
                "choice": "DROP",
                "decision_contract": "inaudible-current-proposed-drop.v1",
                "original_request_sha256": "sha256:" + "f" * 64,
                "effective_drop_request_sha256": "sha256:" + "f" * 64,
                "original_witness_request_sha256": (
                    "sha256:" + witness_request_sha256
                ),
                "effective_witness_request_sha256": (
                    "sha256:" + witness_request_sha256
                ),
                "judge_prompt_sha256": "sha256:" + "b" * 64,
                "judge_completion_sha256": "sha256:" + "c" * 64,
                "target_audible": False,
                "timing_immutable": True,
            },
        },
    }

    repaired, receipts = finalization._apply_exact_final_cpa_repairs(
        srt_text,
        {"findings": [finding]},
    )

    assert "咳咳" not in repaired
    assert repaired.startswith(
        "1\n00:00:02,000 --> 00:00:03,000\n保留下一句"
    )
    assert len(receipts) == 1
    assert receipts[0]["action"] == "DROP_CUE"
    assert receipts[0]["after"] == ""
    assert receipts[0]["after_sha256"] == (
        "sha256:" + hashlib.sha256(b"").hexdigest()
    )

    chat_authority: dict[str, object] = {"entity_repairs": []}
    finalization._register_exact_final_cpa_repairs(
        chat_authority,
        receipts,
        delivery_start_ms=0,
    )
    owner = chat_authority["entity_repairs"][0]
    assert owner["mode"] == "exact_final_cpa_self_heal"
    assert owner["repair_class"] == "acoustic_drop_cue"
    assert owner["structured_exact_text"] == ""
    assert finalization.verify_chat_authority_final_surfaces(
        chat_authority,
        final_text_srt=repaired,
        final_speaker_srt=repaired,
        delivery_start_ms=0,
        delivery_end_ms=3_000,
    )

    forged = json.loads(json.dumps(finding))
    forged["exact_release_adjudication"]["verdict"][
        "target_audible"
    ] = True
    unchanged, forged_receipts = (
        finalization._apply_exact_final_cpa_repairs(
            srt_text,
            {"findings": [forged]},
        )
    )
    assert unchanged == srt_text
    assert forged_receipts == []


def test_exact_final_inaudible_nonempty_proposed_requires_override_receipt():
    from src.autoslice.final_review_auditor import adjudicate_context_finding

    source = "1\n00:00:00,000 --> 00:00:01,000\n咳咳\n"
    base_sha256 = hashlib.sha256("咳咳".encode("utf-8")).hexdigest()

    def inaudible(request):
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "status": "OBSERVED",
            "request_sha256": request["request_sha256"],
            "target_audible": False,
            "heard_pinyin": "",
            "uncertain_positions": [],
            "syllable_count": 0,
            "confidence": 0.9,
        }

    _output, adjudication = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "咳咳",
            "suggestion": "嗯嗯",
            "proposed_full_cue": "嗯嗯",
            "repair_class": "phonetic",
            "base_text_sha256": base_sha256,
        },
        entity_verifier=inaudible,
        judge_llm_call=lambda _prompt: json.dumps(
            {"choice": "PROPOSED"}
        ),
    )
    finding = {
        "cue_index": 1,
        "base_text_sha256": base_sha256,
        "proposed_full_cue": "嗯嗯",
        "repair_class": "phonetic",
        "exact_release_adjudication": adjudication,
    }

    repaired, receipts = finalization._apply_exact_final_cpa_repairs(
        source,
        {"findings": [finding]},
    )

    assert "嗯嗯" in repaired
    assert receipts[0]["inaudible_witness_override"]["status"] == "PASS"
    assert receipts[0]["mutation_authority"]["basis"] == (
        "CPA_EXPLICIT_OVERRIDE_INAUDIBLE_WITNESS"
    )
    chat_authority: dict[str, object] = {"entity_repairs": []}
    finalization._register_exact_final_cpa_repairs(
        chat_authority,
        receipts,
        delivery_start_ms=0,
    )
    owner = chat_authority["entity_repairs"][0]
    assert owner["inaudible_witness_override"]["status"] == "PASS"
    assert owner["structured_exact_text"] == "嗯嗯"

    forged = json.loads(json.dumps(finding))
    forged["exact_release_adjudication"]["witness_judge"].pop(
        "inaudible_witness_override"
    )
    unchanged, forged_receipts = finalization._apply_exact_final_cpa_repairs(
        source,
        {"findings": [forged]},
    )
    assert unchanged == source
    assert forged_receipts == []


def test_exact_final_cpa_retires_same_geometry_exact_read_substring():
    """1411 regression: final CPA ``电`` must retire older chat ``点``."""

    before = "小李你怎么被点了！，不知道"
    after = "小李你怎么被电了！，不知道"
    receipt = {
        "schema_version": "exact-final-cpa-self-heal.v1",
        "cue_index": 1,
        "matched_start_ms": 250,
        "matched_end_ms": 2_690,
        "before": before,
        "after": after,
        "before_sha256": (
            "sha256:" + hashlib.sha256(before.encode("utf-8")).hexdigest()
        ),
        "after_sha256": (
            "sha256:" + hashlib.sha256(after.encode("utf-8")).hexdigest()
        ),
        "finding_sha256": "sha256:" + "a" * 64,
        "request_sha256": "sha256:" + "b" * 64,
        "decision_authority": "CPA_JUDGE",
        "action": "REPLACE_CUE_TEXT",
        "repair_class": "phonetic",
        "policy_branch": "CPA_JUDGE_WITH_TEXT_AUTHORITY_APPLY_PROPOSED",
        "acoustic_witness": {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "status": "OBSERVED",
            "target_audible": True,
        },
        "mutation_authority": {
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "PASS",
            "basis": "CPA_JUDGED_WITH_TEXTUAL_ORTHOGRAPHY_EVIDENCE",
        },
        "timing_immutable": True,
    }
    audit: dict[str, object] = {
        "applied": [
            {
                "mode": "exact_span",
                "matched_start_ms": 9_920,
                "matched_end_ms": 12_360,
                "exact_text": "小李你怎么被点了！",
            },
            {
                "mode": "exact_span",
                "matched_start_ms": 9_920,
                "matched_end_ms": 12_360,
                "exact_text": "不知道",
            },
        ],
        "entity_repairs": [],
    }

    finalization._register_exact_final_cpa_repairs(
        audit,
        [receipt],
        delivery_start_ms=9_670,
    )

    superseded = audit["applied"][0]["reconciliation"]
    assert superseded["status"] == "SUPERSEDED_BY_EXACT_FINAL_CPA"
    assert superseded["timing_immutable"] is True
    assert "reconciliation" not in audit["applied"][1]
    registration = audit["exact_final_cpa_surface_registrations"][0]
    assert registration["superseded_applied_indexes"] == [0]
    assert finalization.verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=(
            "1\n00:00:00,250 --> 00:00:02,690\n"
            f"{after}\n"
        ),
        final_speaker_srt=(
            "1\n00:00:00,250 --> 00:00:02,690\n"
            f"{after}\n"
        ),
        delivery_start_ms=9_670,
        delivery_end_ms=12_360,
    )


def test_exact_final_carryover_replays_only_on_same_hash_and_time(tmp_path):
    from src.autoslice import producer_package_finalization as finalization

    current = "就是面部的时候没有什么制作这个机体"
    proposed = "就是制作这个机体"
    base_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
    srt_text = f"1\n00:00:22,920 --> 00:00:26,140\n{current}\n"
    path = tmp_path / "candidate.final-review-carryover.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "final-review-carryover.v1",
                "findings": [
                    {
                        "cue": 11,
                        "base_text_sha256": base_sha256,
                        "kind": "context",
                        "suspect": "面部的时候没有什么",
                        "proposed_full_cue": proposed,
                        "exact_release_adjudication": {
                            "schema_version": "subtitle-span-adjudication.v1",
                            "status": "OBSERVED",
                            "decision_authority": "CPA_JUDGE",
                            "repaired": True,
                            "timing_immutable": True,
                            "mutation_authority": {
                                "schema_version": (
                                    "subtitle-correction-mutation-authority.v1"
                                ),
                                "status": "PASS",
                            },
                            "request": {
                                "schema_version": (
                                    "subtitle-span-acoustic-check-request.v1"
                                ),
                                "request_sha256": "f" * 64,
                                "base_text_sha256": base_sha256,
                                "current_cue": current,
                                "proposed_cue": proposed,
                                "matched_start_ms": 22_920,
                                "matched_end_ms": 26_140,
                            },
                            "witness_judge": {
                                "judge": {
                                    "status": "JUDGED",
                                    "choice": "PROPOSED",
                                }
                            },
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rows = finalization._replayable_exact_final_carryover_findings(
        srt_text,
        path,
    )
    assert len(rows) == 1
    repaired, receipts = finalization._apply_exact_final_cpa_repairs(
        srt_text,
        {"findings": rows},
    )
    assert proposed in repaired
    assert receipts[0]["before"] == current

    drifted = srt_text.replace("00:00:22,920", "00:00:22,921")
    assert finalization._replayable_exact_final_carryover_findings(
        drifted,
        path,
    ) == []


def test_exact_final_review_gate_allows_five_bounded_repair_rounds(
    tmp_path: Path,
) -> None:
    subtitle = tmp_path / "candidate.recut.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n版本0\n",
        encoding="utf-8",
    )
    chat_path = tmp_path / "candidate.chat-authority.json"
    calls: list[str] = []

    def exact_review(text, *_args):
        calls.append(text)
        version = int(text.split("版本", 1)[1].splitlines()[0])
        reviewed_sha = "sha256:" + hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()
        common = {
            "schema_version": "final-review-audit.v2",
            "reviewed_srt_sha256": reviewed_sha,
            "boundary_semantic_review": {
                "schema_version": "talk-boundary-semantic-review.v1",
                "status": "PASS",
                "review_scope": "final_delivery",
                "reason_codes": [],
                "request_sha256": "sha256:" + "d" * 64,
                "cue_grid_sha256": "sha256:" + "e" * 64,
                "source_separation_witness": {
                    "schema_version": "talk-boundary-source-separation-witness.v1",
                    "status": "PASS",
                    "source_review_sha256": "sha256:" + "a" * 64,
                    "source_request_sha256": "sha256:" + "b" * 64,
                    "source_cue_grid_sha256": "sha256:" + "c" * 64,
                    "source_final_start_ms": 0,
                    "source_final_end_ms": 1_400,
                    "reason_codes": [],
                },
                "final_endpoint_binding": {
                    "schema_version": "talk-boundary-final-endpoint-binding.v1",
                    "status": "PASS",
                    "recommended_end_cue_index": 1,
                    "recommended_end_ms": 1_000,
                    "final_closure_cue_index": 1,
                    "final_snapped_end_ms": 1_000,
                    "final_end_ms": 1_400,
                    "semantic_cue_grid_sha256": "sha256:" + "e" * 64,
                    "final_cue_grid_sha256": "sha256:" + "e" * 64,
                    "reason_codes": [],
                },
            },
            "correction_mutation_authority": {
                "schema_version": "subtitle-correction-mutation-audit.v1",
                "status": "PASS",
                "applied_count": 0,
                "validated_mutation_count": 0,
                "failures": [],
            },
        }
        if version == 5:
            return {
                **common,
                "status": "CLEAN",
                "release_gate": "PASS",
                "reason_codes": [],
                "discovery": {
                    "status": "COMPLETE",
                    "explicit_empty_findings": True,
                },
                "findings": [],
                "validated_finding_count": 0,
            }
        current = f"版本{version}"
        proposed = f"版本{version + 1}"
        base_sha = hashlib.sha256(current.encode("utf-8")).hexdigest()
        return {
            **common,
            "status": "FLAGGED",
            "release_gate": "BLOCK",
            "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
            "discovery": {
                "status": "COMPLETE",
                "explicit_empty_findings": False,
            },
            "findings": [{
                "cue_index": 1,
                "base_text_sha256": base_sha,
                "proposed_full_cue": proposed,
                "exact_release_adjudication": {
                    "schema_version": "subtitle-span-adjudication.v1",
                    "status": "OBSERVED",
                    "decision_authority": "CPA_JUDGE",
                    "repaired": True,
                    "timing_immutable": True,
                    "mutation_authority": {
                        "schema_version": "subtitle-correction-mutation-authority.v1",
                        "status": "PASS",
                        "basis": "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION",
                    },
                    "request": {
                        "schema_version": "subtitle-span-acoustic-check-request.v1",
                        "request_sha256": "f" * 64,
                        "base_text_sha256": base_sha,
                        "current_cue": current,
                        "proposed_cue": proposed,
                    },
                    "witness_judge": {"judge": {
                        "schema_version": "acoustic-witness-adjudication.v1",
                        "status": "JUDGED",
                        "choice": "PROPOSED",
                    }},
                },
            }],
            "validated_finding_count": 1,
        }

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=unused,
        run_exact_final_review=exact_review,
    )
    result = finalization._run_exact_final_review_gate(
        cid="candidate",
        out_root=tmp_path,
        final_start=0,
        final_end=1_400,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=tmp_path / "candidate.recut.mp4",
            subtitle_path=subtitle,
            text_manifest_path=None,
            text_manifest=None,
        ),
        chat_authority_audit={},
        chat_authority_path=chat_path,
        adapters=adapters,
    )

    assert result["status"] == "CLEAN"
    assert len(calls) == 6
    assert "版本5" in subtitle.read_text(encoding="utf-8")
    assert len(result["exact_final_cpa_self_heal"]["passes"]) == 5


def test_delivery_summary_uses_persisted_boundary_audit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    burned = tmp_path / "burned.mp4"
    burned.write_bytes(b"burned")
    burned_ass = tmp_path / "burned.final-sapphire72.ass"
    burned_ass.write_text("[Events]\n", encoding="utf-8")
    subtitle = tmp_path / "final.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n完成\n", encoding="utf-8")
    record_path = tmp_path / "record.json"
    record_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        finalization, "_validated_burned_artifact", lambda _record: burned
    )
    monkeypatch.setattr(
        finalization,
        "_validated_burned_ass_artifact",
        lambda _record: burned_ass,
    )

    copy_commands: list[list[str]] = []

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated finalization adapter was called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=lambda command, **_kwargs: copy_commands.append(command),
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )
    recut = finalization.FinalRecutArtifacts(
        recut_dir=tmp_path,
        media_path=tmp_path / "recut.mp4",
        subtitle_path=subtitle,
        text_manifest_path=None,
        text_manifest=None,
    )
    speaker = finalization.SpeakerArtifacts(None, None, None, None)
    authority = finalization.AuthorityArtifacts(None, None)
    staged = finalization.StagedRecord(
        record={"duration_ms": 12_345},
        staging={"title": "标题"},
        record_path=record_path,
    )
    audit = {
        "closure_sentence": "这是落点",
        "verdict": "ok_sentence_boundary_cut",
        "red_flags": [],
        "boundary_repairs": [{"reason": "tail_clamped"}],
    }

    result = finalization._deliver_staged_record(
        spec={"date": "2026-07-15", "delivery_name": "成品"},
        cid="candidate-1",
        final_end=12_345,
        audit=audit,
        timing_qa={"counts": {"output": 1}},
        recut=recut,
        speaker=speaker,
        authority=authority,
        chat_authority_path=tmp_path / "chat-authority.json",
        staged=staged,
        adapters=adapters,
    )

    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["duration_ms"] == 12_345
    assert summary["closure_sentence"] == "这是落点"
    assert summary["red_flags"] == []
    assert summary["boundary_repairs"] == [{"reason": "tail_clamped"}]
    assert len(copy_commands) == 4
    assert [
        "cp",
        str(burned_ass),
        str(
            tmp_path
            / "delivery/2026-07-15/成品.final-sapphire72.ass"
        ),
    ] in copy_commands


def test_final_recut_rebases_timeline_bound_text_override(
    tmp_path: Path,
) -> None:
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded")
    (tmp_path / "out").mkdir()
    padded_provenance = tmp_path / "padded.provenance.json"
    padded_provenance.write_text("{}\n", encoding="utf-8")
    override = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/subtitle_text_overrides/auto_225942_962_980.text.v1.json"
    )

    def run_command(command: list[str], **_kwargs) -> None:
        Path(command[-1]).write_bytes(b"recut")

    def write_source_range_srt(
        _cues, _start_ms: int, _end_ms: int, output: Path
    ) -> None:
        output.write_text(
            "1\n00:00:04,770 --> 00:00:07,970\n让李豆沙线下叫停了时\n",
            encoding="utf-8",
        )

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated finalization adapter was called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda **kwargs: ["recut", str(kwargs["output_media"])],
        run_command=run_command,
        write_source_range_srt=write_source_range_srt,
        apply_text_override_document=apply_document,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )

    recut = finalization._materialize_final_recut(
        spec={"pieces": [{"start_ms": 952_920}]},
        cid="auto_225942_962_980",
        out_root=tmp_path / "out",
        padded=padded,
        padded_provenance_path=padded_provenance,
        piece_provenance_rows=[],
        final_start=9_770,
        final_end=32_840,
        sanitized=[],
        timing_qa={},
        text_override_path=override,
        adapters=adapters,
    )

    assert "让李豆沙线下叫kmx" in recut.subtitle_path.read_text(encoding="utf-8")
    assert recut.text_manifest is not None
    assert recut.text_manifest["source_timeline_offset_ms"] == 9_770


def test_final_recut_provenance_keeps_every_bound_source_piece(
    tmp_path: Path,
) -> None:
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded")
    (tmp_path / "out").mkdir()
    padded_provenance = tmp_path / "padded.provenance.json"
    padded_provenance.write_text("{}\n", encoding="utf-8")

    def run_command(command: list[str], **_kwargs) -> None:
        Path(command[-1]).write_bytes(b"recut")

    def write_source_range_srt(
        _cues, _start_ms: int, _end_ms: int, output: Path
    ) -> None:
        output.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n完整台词\n",
            encoding="utf-8",
        )

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated finalization adapter was called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda **kwargs: [
            "recut",
            str(kwargs["output_media"]),
        ],
        run_command=run_command,
        write_source_range_srt=write_source_range_srt,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )
    pieces = [
        {
            "source_path": "/recordings/first.mp4",
            "source_sha256": "a" * 64,
            "start_ms": 119_070,
            "end_ms": 125_720,
        },
        {
            "source_path": "/recordings/second.mp4",
            "source_sha256": "b" * 64,
            "start_ms": 1_336_800,
            "end_ms": 1_489_320,
        },
    ]

    recut = finalization._materialize_final_recut(
        spec={
            "pieces": [
                {"start_ms": row["start_ms"], "end_ms": row["end_ms"]}
                for row in pieces
            ]
        },
        cid="multi-piece",
        out_root=tmp_path / "out",
        padded=padded,
        padded_provenance_path=padded_provenance,
        piece_provenance_rows=pieces,
        final_start=0,
        final_end=1_000,
        sanitized=[],
        timing_qa={},
        text_override_path=None,
        adapters=adapters,
    )

    provenance = json.loads(
        recut.media_path.with_suffix(".provenance.json").read_text(
            encoding="utf-8"
        )
    )
    assert provenance["source_piece"] == pieces
    assert provenance["final_recut"]["absolute_source_start_ms"] is None
    assert provenance["final_recut"]["absolute_source_end_ms"] is None


def test_final_recut_applies_hash_bound_redelivery_baseline_outside_truth(
    tmp_path: Path,
) -> None:
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded")
    (tmp_path / "out").mkdir()
    padded_provenance = tmp_path / "padded.provenance.json"
    padded_provenance.write_text("{}\n", encoding="utf-8")
    baseline = tmp_path / "prior-delivery.srt"
    baseline.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n旧审定boku第一句\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n旧错误第二句\n\n"
        "3\n00:00:02,000 --> 00:00:03,000\n旧静音幻听\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "subtitle-truth.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "entries": [
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "new-reviewed-line",
                        "recording_basename": "recording.mp4",
                        "source_start_ms": 102_020,
                        "source_end_ms": 103_020,
                        "action": "replace_cue",
                        "text": "Ivan新源真值",
                        "authority": "newer source review",
                        "required": True,
                    },
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "drop-silent-cue",
                        "recording_basename": "recording.mp4",
                        "source_start_ms": 103_020,
                        "source_end_ms": 104_020,
                        "action": "drop_cue",
                        "authority": "reviewed silence",
                        "required": True,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    def run_command(command: list[str], **_kwargs) -> None:
        Path(command[-1]).write_bytes(b"recut")

    def write_source_range_srt(
        _cues, _start_ms: int, _end_ms: int, output: Path
    ) -> None:
        output.write_text(
            "1\n00:00:00,020 --> 00:00:01,020\n随机漂移第一句\n\n"
            "2\n00:00:01,020 --> 00:00:02,020\nIvan新源真值\n",
            encoding="utf-8",
        )

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated finalization adapter was called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda **kwargs: [
            "recut",
            str(kwargs["output_media"]),
        ],
        run_command=run_command,
        write_source_range_srt=write_source_range_srt,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )
    chat_audit = {
        "source_subtitle_truth_audit": {
            "status": "APPLIED",
            "ledger_path": str(ledger),
            "applied": [
                {
                    "action": "replace_cue",
                    # Padded timeline; final_start=1000 rebases this to the
                    # second delivery cue.
                    "local_windows": [
                        {"start_ms": 2_020, "end_ms": 3_020}
                    ],
                },
                {
                    "action": "drop_cue",
                    "local_windows": [
                        {"start_ms": 3_020, "end_ms": 4_020}
                    ],
                }
            ],
            "satisfied": [],
        }
    }
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/recording.mp4",
                "start_ms": 100_000,
                "end_ms": 104_020,
            }
        ],
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v1",
            "mode": "preserve_text_outside_source_truth",
            "path": str(baseline),
            "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
            "authority": "previous reviewed delivery",
        },
    }

    recut = finalization._materialize_final_recut(
        spec=spec,
        cid="candidate",
        out_root=tmp_path / "out",
        padded=padded,
        padded_provenance_path=padded_provenance,
        piece_provenance_rows=[],
        final_start=1_000,
        final_end=4_020,
        sanitized=[],
        timing_qa={},
        text_override_path=None,
        adapters=adapters,
        spec_parent=tmp_path,
        chat_authority_audit=chat_audit,
    )

    output = recut.subtitle_path.read_text(encoding="utf-8")
    assert "00:00:00,020 --> 00:00:01,020\n旧审定ぼく第一句" in output
    assert "boku" not in output
    assert "00:00:01,020 --> 00:00:02,020\nIvan新源真值" in output
    assert "旧错误第二句" not in output
    assert "旧静音幻听" not in output
    assert recut.redelivery_baseline_audit is not None
    assert recut.redelivery_baseline_audit["status"] == "APPLIED"
    assert recut.redelivery_baseline_audit[
        "post_redelivery_japanese_native_script_audit"
    ]["status"] == "APPLIED"
    assert recut.redelivery_baseline_audit["source_truth_reapplication"][
        "status"
    ] == "APPLIED"
    assert recut.redelivery_baseline_audit["protected_intervals"] == [
        {"start_ms": 2_020, "end_ms": 3_020}
    ]
    assert recut.redelivery_baseline_audit_path is not None
    assert recut.redelivery_baseline_audit_path.is_file()


@pytest.mark.parametrize("with_boundary_witness_reserve", [False, True])
def test_final_recut_v2_projects_reviewed_text_and_remerges_release_sliver(
    tmp_path: Path,
    with_boundary_witness_reserve: bool,
) -> None:
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded")
    (tmp_path / "out").mkdir()
    padded_provenance = tmp_path / "padded.provenance.json"
    padded_provenance.write_text("{}\n", encoding="utf-8")
    baseline = tmp_path / "reviewed.srt"
    baseline.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n裁掉的旧开头\n\n"
        "2\n00:00:01,000 --> 00:00:01,200\n哦\n\n"
        "3\n00:00:01,200 --> 00:00:03,000\n人工审定乙\n",
        encoding="utf-8",
    )

    def run_command(command: list[str], **_kwargs) -> None:
        Path(command[-1]).write_bytes(b"recut")

    def write_source_range_srt(
        _cues, _start_ms: int, _end_ms: int, output: Path
    ) -> None:
        output.write_text(
            "1\n00:00:00,000 --> 00:00:00,200\n随机甲\n\n"
            "2\n00:00:00,200 --> 00:00:02,000\n随机乙\n\n"
            "3\n00:00:02,000 --> 00:00:03,000\n新延长尾句\n",
            encoding="utf-8",
        )

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated finalization adapter was called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda **kwargs: [
            "recut",
            str(kwargs["output_media"]),
        ],
        run_command=run_command,
        write_source_range_srt=write_source_range_srt,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )
    source_sha256 = "a" * 64
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/recording.mp4",
                "start_ms": 100_000,
                "end_ms": 104_000,
            }
        ],
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v2",
            "mode": "preserve_text_outside_source_truth",
            "path": str(baseline),
            "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
            "authority": "Ivan reviewed delivery",
            "source_recording_basename": "recording.mp4",
            "source_sha256": source_sha256,
            "absolute_source_start_ms": 100_000,
            "absolute_source_end_ms": 103_000,
        },
    }
    piece_provenance_rows = [
        {
            "source_path": "/recordings/recording.mp4",
            "source_sha256": source_sha256,
        }
    ]
    if with_boundary_witness_reserve:
        for ordinal in (1, 2):
            spec["pieces"].append(
                {
                    "remote_media": f"/recordings/next-{ordinal}.mp4",
                    "start_ms": 0,
                    "end_ms": 30_000,
                    "piece_role": "boundary_witness_reserve",
                }
            )
            piece_provenance_rows.append(
                {
                    "source_path": f"/recordings/next-{ordinal}.mp4",
                    "source_sha256": f"{ordinal + 1:x}" * 64,
                }
            )

    recut = finalization._materialize_final_recut(
        spec=spec,
        cid="candidate-v2",
        out_root=tmp_path / "out",
        padded=padded,
        padded_provenance_path=padded_provenance,
        piece_provenance_rows=piece_provenance_rows,
        final_start=1_000,
        final_end=4_000,
        sanitized=[],
        timing_qa={},
        text_override_path=None,
        adapters=adapters,
        spec_parent=tmp_path,
        chat_authority_audit={},
    )

    output = recut.subtitle_path.read_text(encoding="utf-8")
    assert "哦，人工审定乙" in output
    assert "\n哦\n" not in output
    assert "裁掉的旧开头" not in output
    assert "新延长尾句" in output
    assert recut.redelivery_baseline_audit is not None
    assert recut.redelivery_baseline_audit["status"] == "APPLIED"
    assert recut.redelivery_baseline_audit["final_release_grade_cue_merges"] == [
        {
            "block": 1,
            "text": "哦",
            "action": "MERGED_INTO_NEXT",
        }
    ]
    assert recut.redelivery_baseline_audit[
        "post_release_grade_output_sha256"
    ] == hashlib.sha256(output.encode("utf-8")).hexdigest()
    assert recut.redelivery_baseline_audit["uncovered_current_cue_count"] == 1
    assert recut.redelivery_baseline_audit["omitted_baseline_cue_count"] == 1
    provenance = json.loads(
        recut.media_path.with_suffix(".provenance.json").read_text(
            encoding="utf-8"
        )
    )
    assert provenance["final_recut"]["absolute_source_start_ms"] == 101_000
    assert provenance["final_recut"]["absolute_source_end_ms"] == 104_000
    if with_boundary_witness_reserve:
        assert len(provenance["source_piece"]) == 3
    else:
        assert provenance["source_piece"]["source_path"] == (
            "/recordings/recording.mp4"
        )


@pytest.mark.parametrize(
    ("second_piece_role", "final_end", "drop_provenance", "reason"),
    [
        (
            None,
            4_000,
            False,
            "REDELIVERY_BASELINE_V2_REQUIRES_ONE_BOUND_SOURCE_PIECE",
        ),
        (
            "boundary_witness_reserve",
            4_001,
            False,
            "REDELIVERY_BASELINE_V2_FINAL_INTERVAL_OUTSIDE_CONTENT_PIECE",
        ),
        (
            "boundary_witness_reserve",
            4_000,
            True,
            "REDELIVERY_BASELINE_V2_REQUIRES_ONE_BOUND_SOURCE_PIECE",
        ),
    ],
)
def test_final_recut_v2_rejects_ambiguous_or_cross_source_binding(
    tmp_path: Path,
    second_piece_role: str | None,
    final_end: int,
    drop_provenance: bool,
    reason: str,
) -> None:
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded")
    (tmp_path / "out").mkdir()
    padded_provenance = tmp_path / "padded.provenance.json"
    padded_provenance.write_text("{}\n", encoding="utf-8")
    baseline = tmp_path / "reviewed.srt"
    baseline.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\n人工审定\n",
        encoding="utf-8",
    )

    def unused(*_args, **_kwargs):
        raise AssertionError("finalization must fail before adapters run")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )
    source_sha256 = "a" * 64
    second_piece = {
        "remote_media": "/recordings/next.mp4",
        "start_ms": 0,
        "end_ms": 30_000,
    }
    if second_piece_role is not None:
        second_piece["piece_role"] = second_piece_role
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/recording.mp4",
                "start_ms": 100_000,
                "end_ms": 104_000,
            },
            second_piece,
        ],
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v2",
            "mode": "preserve_text_outside_source_truth",
            "path": str(baseline),
            "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
            "authority": "Ivan reviewed delivery",
            "source_recording_basename": "recording.mp4",
            "source_sha256": source_sha256,
            "absolute_source_start_ms": 100_000,
            "absolute_source_end_ms": 103_000,
        },
    }
    provenance = [
        {
            "source_path": "/recordings/recording.mp4",
            "source_sha256": source_sha256,
        },
        {
            "source_path": "/recordings/next.mp4",
            "source_sha256": "b" * 64,
        },
    ]
    if drop_provenance:
        provenance.pop()

    with pytest.raises(SystemExit, match=reason):
        finalization._materialize_final_recut(
            spec=spec,
            cid="candidate-v2-negative",
            out_root=tmp_path / "out",
            padded=padded,
            padded_provenance_path=padded_provenance,
            piece_provenance_rows=provenance,
            final_start=1_000,
            final_end=final_end,
            sanitized=[],
            timing_qa={},
            text_override_path=None,
            adapters=adapters,
            spec_parent=tmp_path,
            chat_authority_audit={},
        )


def test_final_recut_replays_truth_after_broad_window_was_satisfied(
    tmp_path: Path,
) -> None:
    """One canonical mention must not hide a wrong sibling in the same window."""

    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"padded")
    (tmp_path / "out").mkdir()
    padded_provenance = tmp_path / "padded.provenance.json"
    padded_provenance.write_text("{}\n", encoding="utf-8")
    baseline = tmp_path / "prior-delivery.srt"
    baseline.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n旧审定第一句\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n就灰神，好像是灰神吧\n\n"
        "3\n00:00:02,000 --> 00:00:03,000\n灰神说救救李姐\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "subtitle-truth.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "entries": [
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "entity-spelling",
                        "recording_basename": "recording.mp4",
                        "source_start_ms": 102_000,
                        "source_end_ms": 104_000,
                        "action": "replace_substring",
                        "replacements": [
                            {"surface": "灰神", "canonical": "毁神"}
                        ],
                        "required_text": "毁神",
                        "authority": "reviewed spoken-name spelling",
                        "required": True,
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    def run_command(command: list[str], **_kwargs) -> None:
        Path(command[-1]).write_bytes(b"recut")

    def write_source_range_srt(
        _cues, _start_ms: int, _end_ms: int, output: Path
    ) -> None:
        output.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n随机漂移第一句\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n就毁神，好像是毁神吧\n\n"
            "3\n00:00:02,000 --> 00:00:03,000\n鼠神说救救李姐\n",
            encoding="utf-8",
        )

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated finalization adapter was called")

    adapters = finalization.ProducerFinalizationAdapters(
        accurate_recut_command=lambda **kwargs: [
            "recut",
            str(kwargs["output_media"]),
        ],
        run_command=run_command,
        write_source_range_srt=write_source_range_srt,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=lambda: tmp_path / "delivery",
    )
    chat_audit = {
        "source_subtitle_truth_audit": {
            "schema_version": "source-subtitle-truth-audit.v1",
            "status": "ALREADY_SATISFIED",
            "ledger_path": str(ledger),
            "applied": [],
            "satisfied": [
                {
                    "truth_id": "entity-spelling",
                    "action": "replace_substring",
                    "local_windows": [
                        {"start_ms": 2_000, "end_ms": 4_000}
                    ],
                }
            ],
            "failures": [],
        }
    }
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/recording.mp4",
                "start_ms": 100_000,
                "end_ms": 104_000,
            }
        ],
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v1",
            "mode": "preserve_text_outside_source_truth",
            "path": str(baseline),
            "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
            "authority": "previous reviewed delivery",
        },
    }

    recut = finalization._materialize_final_recut(
        spec=spec,
        cid="candidate",
        out_root=tmp_path / "out",
        padded=padded,
        padded_provenance_path=padded_provenance,
        piece_provenance_rows=[],
        final_start=1_000,
        final_end=4_000,
        sanitized=[],
        timing_qa={},
        text_override_path=None,
        adapters=adapters,
        spec_parent=tmp_path,
        chat_authority_audit=chat_audit,
    )

    output = recut.subtitle_path.read_text(encoding="utf-8")
    assert "旧审定第一句" in output
    assert "就毁神，好像是毁神吧" in output
    assert "毁神说救救李姐" in output
    assert "鼠神" not in output
    assert "灰神" not in output
    assert chat_audit["source_subtitle_truth_audit"]["status"] == "APPLIED"
    assert chat_audit["source_subtitle_truth_audit"]["applied"][0][
        "local_windows"
    ] == [{"start_ms": 2_000, "end_ms": 4_000}]
    assert chat_audit["source_subtitle_truth_post_redelivery_audit"][
        "timeline_basis"
    ] == "final_delivery"
    assert recut.redelivery_baseline_audit is not None
    assert recut.redelivery_baseline_audit["source_truth_reapplication"][
        "status"
    ] == "APPLIED"


def test_redelivery_baseline_resolves_actual_unproven_foreign_shape() -> None:
    source_language_audit = {
        "status": "DEFERRED_TO_REDELIVERY_BASELINE",
        "unproven_foreign_introductions": [
            {
                "cue_index": 37,
                "start_ms": 85_220,
                "end_ms": 87_900,
                "draft": "分牙三四关就毁神",
                "attempted": "非常やさしい，就病院坂灵",
            }
        ],
    }
    baseline_audit = {
        "status": "APPLIED",
        "failures": [],
        "mappings": [
            {
                "current_cue_index": 34,
                "start_ms": 75_220,
                "end_ms": 77_900,
            }
        ],
    }
    final_text = (
        "1\n00:01:15,220 --> 00:01:17,900\n分牙三四关就毁神——\n"
    )

    resolved = (
        finalization._resolve_deferred_foreign_introductions_after_redelivery(
            source_language_audit=source_language_audit,
            final_text=final_text,
            baseline_audit=baseline_audit,
            final_start=10_000,
        )
    )

    assert resolved
    assert source_language_audit["status"] == (
        "RESOLVED_BY_REDELIVERY_BASELINE"
    )
    assert source_language_audit["deferred_resolution"]["status"] == "PASS"


def test_redelivery_baseline_keeps_block_when_foreign_surface_survives() -> None:
    source_language_audit = {
        "status": "DEFERRED_TO_REDELIVERY_BASELINE",
        "unproven_foreign_introductions": [
            {
                "cue_index": 37,
                "start_ms": 85_220,
                "end_ms": 87_900,
                "attempted": "非常やさしい，就病院坂灵",
            }
        ],
    }
    baseline_audit = {
        "status": "APPLIED",
        "failures": [],
        "mappings": [
            {
                "current_cue_index": 34,
                "start_ms": 75_220,
                "end_ms": 77_900,
            }
        ],
    }
    final_text = (
        "1\n00:01:15,220 --> 00:01:17,900\n仍然非常やさしい\n"
    )

    resolved = (
        finalization._resolve_deferred_foreign_introductions_after_redelivery(
            source_language_audit=source_language_audit,
            final_text=final_text,
            baseline_audit=baseline_audit,
            final_start=10_000,
        )
    )

    assert not resolved
    assert source_language_audit["status"] == (
        "BLOCKED_REDELIVERY_BASELINE_DID_NOT_RESOLVE_FOREIGN_INTRODUCTION"
    )


def test_redelivery_baseline_owns_native_script_canon_and_cut_context() -> None:
    source_language_audit = {
        "status": "DEFERRED_TO_REDELIVERY_BASELINE",
        "unproven_foreign_introductions": [
            {
                "cue_index": 1,
                "start_ms": 0,
                "end_ms": 2_280,
                "attempted": "女主像个傻子，她叫ぼく",
            },
            {
                "cue_index": 7,
                "start_ms": 18_010,
                "end_ms": 19_410,
                "attempted": "TA要是ぼく",
            },
            {
                "cue_index": 10,
                "start_ms": 23_720,
                "end_ms": 26_200,
                "attempted": "我想下ぼく怎么翻译",
            },
            {
                "cue_index": 39,
                "start_ms": 130_130,
                "end_ms": 131_890,
                "attempted": "おら翻译成老子吗",
            },
        ],
    }
    baseline_audit = {
        "status": "APPLIED",
        "failures": [],
        "baseline_sha256": "a" * 64,
        "mappings": [
            {
                "mapping_kind": "exact_reviewed_interval_replay",
                "baseline_cue_index": 4,
                "output_cue_index": 4,
                "start_ms": 8_220,
                "end_ms": 9_620,
                "text": "TA要是boku",
            },
            {
                "mapping_kind": "exact_reviewed_interval_replay",
                "baseline_cue_index": 7,
                "output_cue_index": 7,
                "start_ms": 13_950,
                "end_ms": 16_410,
                "text": "我想下boku怎么翻译",
            },
            {
                "mapping_kind": "exact_reviewed_interval_replay",
                "baseline_cue_index": 36,
                "output_cue_index": 36,
                "start_ms": 120_340,
                "end_ms": 122_100,
                "text": "おら翻译成老子吗",
            },
        ],
    }
    final_text = (
        "1\n00:00:08,220 --> 00:00:09,620\nTA要是ぼく\n\n"
        "2\n00:00:13,950 --> 00:00:16,410\n我想下ぼく怎么翻译\n\n"
        "3\n00:02:00,340 --> 00:02:02,100\nおら翻译成老子吗\n"
    )

    resolved = (
        finalization._resolve_deferred_foreign_introductions_after_redelivery(
            source_language_audit=source_language_audit,
            final_text=final_text,
            baseline_audit=baseline_audit,
            final_start=9_790,
        )
    )

    assert resolved
    assert source_language_audit["status"] == (
        "RESOLVED_BY_REDELIVERY_BASELINE"
    )
    findings = source_language_audit["deferred_resolution"]["findings"]
    assert findings[0]["reason_code"] == "FINDING_OUTSIDE_FINAL_DELIVERY"
    assert findings[1]["witnessed_surfaces"] == ["ぼく"]
    assert findings[1]["reason_code"] == (
        "INTRODUCED_FOREIGN_SURFACE_WITNESSED_BY_"
        "REDELIVERY_NATIVE_SCRIPT_CANON"
    )
    assert findings[1]["positive_witness_authority_ids"] == [
        "redelivery-baseline-cue-4"
    ]
    assert findings[2]["witnessed_surfaces"] == ["ぼく"]
    assert findings[3]["witnessed_surfaces"] == ["おら"]
    assert findings[3]["positive_witness_authority_ids"] == [
        "redelivery-baseline-cue-36"
    ]
