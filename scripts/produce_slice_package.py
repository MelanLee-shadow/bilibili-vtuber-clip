#!/usr/bin/env python3
"""Produce a finished channel talk-slice package from explicit window specs.

This is the standard "圈中候选→出成品" driver (Ivan 2026-07-04). Its core
contract is the TOPIC-CLOSURE boundary rule: a clip must end where the topic
lands, on a COMPLETE sentence — never mid-sentence, never mid-story.

Flow: remote accurate piece cuts (supports cross-segment stitching) → local
concat → fresh whole-window transcription (glossary+danmaku+screen text) →
sentence-snap the final end to a transcription cue boundary near the semantic
target (fail closed if none) → VAD boundary audit + deterministic red flags →
SELF-REPAIR loop (Ivan 2026-07-10: flags move the cut to the next verifiably
clean sentence end / pull the opening onto the straddled sentence; an
unrepairable boundary fails closed — no quarantine state) → final accurate cut
→ VAD-sanitized text-final subtitles → speaker finalization → colour ASS burn
→ title/cover staging → flat delivery copy to the profile output directory.

Spec JSON:
{
  "candidate_id": "...",
  "date": "2026-07-02",
  "output_root": "reports/.../finals",
  "delivery_name": "买弹幕梗当场拆台",
  "selection_hook": "弹幕让李豆沙表演上下摇……", # selected main event; auto-title must retain it
  "given_title": null,                      # Ivan-given title is verbatim-final
  "lead_pad_ms": 300,
  "pieces": [                                # concatenated in order
    {"remote_media": "<abs path on free>", "start_ms": ..., "end_ms": ...,
     "danmaku_xml_local": "<local path>"}
  ],
  "semantic_end_ms": <absolute ms in the LAST piece's segment timeline>
}
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess  # compatibility seam: speaker tests and callers patch this module object
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_auto_review_shadow_pipeline import (
    _accurate_reencode_recut_command,
    _burn_preview_subtitles,
    _sha256,
    _stage_publish_draft,
    _write_source_range_srt,
)
from scripts.run_full_session_selector_cpa_shadow import (
    _build_aggregate_asr_transcriber,
    _build_ssh_agy_transcribe_runner,
)
from scripts.apply_subtitle_text_overrides import apply_document as apply_text_override_document
from scripts.apply_speaker_turn_overrides import SPEAKER_SUBTITLE_STYLE_ID
from scripts.gemini_slice_jingting import (
    approved_timely_terms,
    glossary as _review_glossary,
)
from scripts.suggest_upload_tags import generate_upload_tags
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.speaker_finalizer import (
    finalize_fast_solo_subtitles,
)
from src.autoslice.speaker_session_router import (
    verify_speaker_routing_claim,
    verify_speaker_routing_claim_for_candidate,
)
from src.autoslice.topic_entity_graph import load_topic_entity_graph

CHANNEL_PROFILE = load_channel_profile(ROOT)


def profile_asset_file(key: str) -> Path:
    """Resolve a selected-profile asset against this producer's repository."""

    return CHANNEL_PROFILE.asset_file(key, repo_root=ROOT)


def profile_delivery_root() -> Path:
    """Return the selected profile's local delivery root."""

    return CHANNEL_PROFILE.delivery_root_for(ROOT)


def profile_voiceprint_reference_dir() -> Path:
    """Return the selected profile's production voiceprint enrollment root."""

    return (
        Path("/opt/bilive/autoslice/voiceprints")
        / CHANNEL_PROFILE.voiceprint_reference_subdirectory
    )


def _topic_graph_disabled() -> bool:
    return (
        os.environ.get("AUTOSLICE_DISABLE_TOPIC_ENTITY_GRAPH") == "1"
        or os.environ.get("LIDOUSHA_DISABLE_TOPIC_ENTITY_GRAPH") == "1"
    )


def _topic_graph_path() -> Path:
    configured = (
        os.environ.get("AUTOSLICE_TOPIC_ENTITY_GRAPH")
        or os.environ.get("LIDOUSHA_TOPIC_ENTITY_GRAPH")
    )
    return Path(configured) if configured else profile_asset_file("topic_entity_graph")


def _topic_graph_expected_sha256() -> str:
    return (
        os.environ.get("AUTOSLICE_TOPIC_ENTITY_GRAPH_SHA256")
        or os.environ.get("LIDOUSHA_TOPIC_ENTITY_GRAPH_SHA256")
        or ""
    )

from src.autoslice.producer_boundary import (
    ISLAND_CONTINUES_FLAG_MS,
    adaptive_tail_cut,
    boundary_audit,
    boundary_red_flags,
    needs_tail_refinement,
    next_clean_closure,
    repair_start_for_straddler,
    snap_end_to_sentence,
    snap_start_to_sentence,
    syntactic_tail_audit,
    tail_requires_forward_extension,
)
from src.autoslice.producer_boundary_resolution import (
    BoundaryResolutionAdapters,
    resolve_producer_boundary,
)


from src.autoslice.producer_chat_input import (
    _load_independent_chat_support_srts,
    _load_superchats,
    _piece_chat_evidence,
)


                             # SC BACKLOG in batches, reading SCs minutes after they
                             # appeared (《想要成为真正的拉拉》SC was read ~4min later),
                             # so "recent" is not enough — content-match picks the right
                             # one out of the backlog, irrelevant ones are ignored.
                               # minutes; giftName is unmasked structured evidence
                               # (see chat_authority._apply_gift_name_repairs), the
                               # masked sender name is not repaired here.


# 7/10 incident: the first plausible closure ended at +25,030ms and the old
# 25,000ms hard edge excluded it before VAD/next-cue cleanliness could even be
# evaluated.  Keep the normal repair bounded to 30s.  A runner retry may raise
# that absolute-from-semantic-target ceiling once to 60s, but only alongside
# more source context and only for deterministic continuation evidence.


from src.autoslice.producer_media import (
    FAST_FRESH_DERIVATION_SCHEMA,
    RECUT_PROVENANCE_SCHEMA,
    _begin_fast_media_transaction,
    _commit_fast_media_transaction,
    _rollback_fast_media_transaction,
    _valid_cached_provenance,
    _validate_fast_transaction_outputs,
    _validated_burned_artifact,
    ffprobe_duration_ms,
    run,
    _derive_fresh_fast_media as _derive_fresh_fast_media_impl,
)
from src.autoslice.producer_package_finalization import (
    ProducerFinalizationAdapters,
    ProducerFinalizationOptions,
    finalize_producer_package,
)
from src.autoslice.producer_request import (
    load_producer_request,
    parse_producer_args,
)
from src.autoslice.producer_source_media import prepare_source_media


def _derive_fresh_fast_media(
    *,
    host: str,
    media_path: Path,
    claimed_segment_path: str,
    expected_segment_sha256: str,
    final_source_start_ms: int,
    final_source_end_ms: int,
) -> dict[str, object]:
    """Compatibility seam for patched media command adapters."""

    return _derive_fresh_fast_media_impl(
        host=host,
        media_path=media_path,
        claimed_segment_path=claimed_segment_path,
        expected_segment_sha256=expected_segment_sha256,
        final_source_start_ms=final_source_start_ms,
        final_source_end_ms=final_source_end_ms,
        accurate_command_builder=_accurate_reencode_recut_command,
        run_command=run,
        duration_probe=ffprobe_duration_ms,
    )


from src.autoslice.producer_text_finalization import (
    verify_chat_authority_final_surfaces,
)
from src.autoslice.producer_text_pipeline import (
    TextPipelineAdapters,
    run_text_pipeline,
)
from src.autoslice.talk_filler import write_final_filler_audit


def _load_term_boundary_surfaces(spec: dict) -> list[str]:
    """Known-proper-noun surfaces for cross-cue boundary unification.

    Reuses only already-gated loader outputs — the timely-terms snapshot and
    topic_entity_graph paths free_session_autoslice.py substitutes per
    AUTOSLICE_BLIND_TIMELY_TERMS / AUTOSLICE_BLIND_TOPIC_ENTITY_GRAPH before
    invoking this script — the same env vars ``approved_timely_terms`` and
    the topic-resolution block below already trust.  No reviewed asset path
    is read directly here.
    """

    surfaces: list[str] = []
    for record in approved_timely_terms():
        surfaces.append(str(record.get("canonical") or ""))
        surfaces.extend(str(value) for value in record.get("readings") or [])
        surfaces.extend(str(value) for value in record.get("aliases") or [])
    if not _topic_graph_disabled():
        graph_path = _topic_graph_path()
        if graph_path.is_file() and not graph_path.is_symlink():
            try:
                graph, _graph_sha = load_topic_entity_graph(
                    graph_path,
                    expected_sha256=_topic_graph_expected_sha256(),
                )
                if dt.datetime.now(dt.timezone.utc) <= dt.datetime.fromisoformat(graph["expires_at"]):
                    # Full graph, not topic-resolved: resolution below scopes
                    # entities using this very transcript as evidence, so it
                    # cannot run before the boundary fix that repairs it.
                    for entity in graph.get("entities") or []:
                        surfaces.append(str(entity.get("canonical_zh") or ""))
                        surfaces.extend(str(value) for value in entity.get("native_names") or [])
                        surfaces.extend(str(value) for value in entity.get("aliases") or [])
            except (OSError, ValueError):
                pass
    return surfaces


from src.autoslice.producer_speaker import (
    SpeakerFinalizationAdapters,
    _default_speaker_mode,
    _rebase_remote_speaker_manifest,
    _write_route_mixed_overlap_evidence,
    run_producer_speaker_finalization as _run_producer_speaker_finalization,
    run_speaker_finalizer,
)


def run_producer_speaker_finalization(**kwargs: object) -> dict:
    """Compatibility boundary preserving the producer's patchable adapters."""

    return _run_producer_speaker_finalization(
        **kwargs,
        adapters=SpeakerFinalizationAdapters(
            verify_route=verify_speaker_routing_claim,
            verify_candidate_route=verify_speaker_routing_claim_for_candidate,
            write_mixed_overlap_evidence=_write_route_mixed_overlap_evidence,
            begin_transaction=_begin_fast_media_transaction,
            derive_fresh_media=_derive_fresh_fast_media,
            finalize_fast=finalize_fast_solo_subtitles,
            validate_transaction=_validate_fast_transaction_outputs,
            commit_transaction=_commit_fast_media_transaction,
            rollback_transaction=_rollback_fast_media_transaction,
            run_binary_finalizer=run_speaker_finalizer,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_producer_args(
        argv,
        description=__doc__ or "",
        speaker_display_name=CHANNEL_PROFILE.display_name,
        default_speaker_mode=_default_speaker_mode(),
    )
    request = load_producer_request(
        args,
        repo_root=ROOT,
        profile_asset_file=profile_asset_file,
    )
    spec = request.spec
    boundary_repair_extend_cap_ms = request.boundary_repair_extend_cap_ms
    branding_intro = request.branding_intro
    cid = request.cid
    out_root = request.out_root
    host = request.host
    text_override_path = request.text_override_path
    subtitle_regression_path = request.subtitle_regression_path

    # 1. Remote accurate piece cuts (production encode params), pull local.
    source_media = prepare_source_media(
        spec=spec,
        cid=cid,
        out_root=out_root,
        host=host,
    )
    durations = source_media.durations
    padded = source_media.padded
    padded_dur = source_media.padded_duration_ms
    padded_provenance_path = source_media.padded_provenance_path
    piece_provenance_rows = source_media.piece_provenance_rows

    # 2. Danmaku + on-screen SUPER_CHATs merged onto the concat timeline.
    text_result = run_text_pipeline(
        spec=spec,
        durations=durations,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        text_override_path=text_override_path,
        cid=cid,
        out_root=out_root,
        substrate=args.substrate,
        correct=args.correct,
        screen_text=args.screen_text,
        adapters=TextPipelineAdapters(
            build_aggregate_transcriber=_build_aggregate_asr_transcriber,
            build_agy_transcriber=_build_ssh_agy_transcribe_runner,
            load_term_boundary_surfaces=_load_term_boundary_surfaces,
            profile_asset_file=profile_asset_file,
            review_glossary=_review_glossary,
            topic_graph_disabled=_topic_graph_disabled,
            topic_graph_path=_topic_graph_path,
            topic_graph_expected_sha256=_topic_graph_expected_sha256,
        ),
    )
    transcriber = text_result.transcriber
    spans = text_result.spans
    cues = text_result.cues
    chat_authority_audit = text_result.chat_authority_audit
    chat_authority_path = text_result.chat_authority_path
    # The same immutable context digest must reach subtitle adjudication,
    # StoryContract, title, and cover.  Keep it on the in-memory spec only;
    # the full artifact is already persisted beside the producer evidence.
    spec["clip_context"] = text_result.clip_context
    spec["clip_context_path"] = str(text_result.clip_context_path)

    # 4a. Sentence-snap the START (the clip must open on a sentence).
    last_piece = spec["pieces"][-1]
    semantic_target_rel = sum(durations[:-1]) + (
        spec["semantic_end_ms"] - last_piece["start_ms"]
    )
    required_tail_end_ms = max(
        (
            int(row["matched_end_ms"])
            for row in chat_authority_audit.get("applied") or []
            if row.get("kind") in {"danmaku", "superchat"}
            and int(row.get("source_offset_ms") or 0) <= semantic_target_rel + 1_000
            and semantic_target_rel
            < int(row.get("matched_end_ms") or 0)
            <= semantic_target_rel + 15_000
            and int(row.get("matched_start_ms") or 0) <= semantic_target_rel + 5_000
        ),
        default=None,
    )
    final_review_audit = chat_authority_audit.get("final_review_audit") or {}
    if isinstance(final_review_audit, dict):
        boundary_semantic_review = final_review_audit.get(
            "boundary_semantic_review"
        )
        if isinstance(boundary_semantic_review, dict):
            spec["boundary_semantic_review"] = boundary_semantic_review
    boundary = resolve_producer_boundary(
        spec=spec,
        durations=durations,
        padded=padded,
        padded_dur=padded_dur,
        cid=cid,
        out_root=out_root,
        transcriber=transcriber,
        cues=cues,
        spans=spans,
        boundary_repair_extend_cap_ms=boundary_repair_extend_cap_ms,
        required_tail_end_ms=required_tail_end_ms,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=_accurate_reencode_recut_command,
            run_command=run,
        ),
    )
    minimum_effective_duration_ms = spec.get("minimum_effective_duration_ms")
    if minimum_effective_duration_ms is not None:
        if (
            isinstance(minimum_effective_duration_ms, bool)
            or not isinstance(minimum_effective_duration_ms, int)
            or minimum_effective_duration_ms < 0
        ):
            raise ValueError("minimum_effective_duration_ms must be a non-negative integer")
        if boundary.final_end - boundary.final_start <= minimum_effective_duration_ms:
            raise SystemExit(
                "TALK_EFFECTIVE_DURATION_NOT_OVER_45S_AFTER_BOUNDARY: "
                f"effective={boundary.final_end - boundary.final_start}ms "
                f"minimum_exclusive={minimum_effective_duration_ms}ms"
            )
    talk_filler_audit_path = write_final_filler_audit(
        spec=spec,
        durations=durations,
        final_start_ms=boundary.final_start,
        final_end_ms=boundary.final_end,
        piece_provenance_rows=piece_provenance_rows,
        branding_intro=branding_intro,
        output_path=out_root / f"{cid}.filler-audit.json",
    )
    # 5. Final accurate cut + VAD-sanitized subtitles rebased to the cut.
    finalization_options = ProducerFinalizationOptions(
        spec=args.spec,
        substrate=args.substrate,
        correct=args.correct,
        speaker_mode=args.speaker_mode,
        speaker_overrides=args.speaker_overrides,
        speaker_source_session_anchors=args.speaker_source_session_anchors,
        speaker_mixed_overlap_evidence=args.speaker_mixed_overlap_evidence,
        speaker_python=args.speaker_python,
        reuse_cover=args.reuse_cover,
    )
    return finalize_producer_package(
        options=finalization_options,
        profile_id=CHANNEL_PROFILE.profile_id,
        speaker_subtitle_style_id=SPEAKER_SUBTITLE_STYLE_ID,
        spec=spec,
        cid=cid,
        out_root=out_root,
        host=host,
        padded=padded,
        padded_provenance_path=padded_provenance_path,
        piece_provenance_rows=piece_provenance_rows,
        final_start=boundary.final_start,
        final_end=boundary.final_end,
        sanitized=boundary.sanitized_cues,
        timing_qa=boundary.timing_qa,
        audit=boundary.audit,
        text_override_path=text_override_path,
        subtitle_regression_path=subtitle_regression_path,
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_authority_path,
        branding_intro=branding_intro,
        talk_filler_audit_path=talk_filler_audit_path,
        adapters=ProducerFinalizationAdapters(
            accurate_recut_command=_accurate_reencode_recut_command,
            run_command=run,
            write_source_range_srt=_write_source_range_srt,
            apply_text_override_document=apply_text_override_document,
            run_speaker_finalization=run_producer_speaker_finalization,
            burn_preview_subtitles=_burn_preview_subtitles,
            stage_publish_draft=_stage_publish_draft,
            generate_upload_tags=generate_upload_tags,
            delivery_root=profile_delivery_root,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
