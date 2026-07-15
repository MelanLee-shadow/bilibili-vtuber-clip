from __future__ import annotations

import argparse
import base64
import difflib
import re
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.auto_review import (
    AutoReviewManifest,
    DecisionAction,
    JingtingProvenance,
    REQUIRED_PUBLISH_ARTIFACT_KEYS,
    ReviewDecision,
    auto_review_manifest_sha256,
    evaluate_required_evidence,
    is_publish_gate_satisfied,
    review_candidate,
)
from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution, TalkCue, resolve_talk_boundary
from src.autoslice.chat_authority import canonicalize_hard_surfaces
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.branding_intro import (
    BrandingIntroError,
    prepend_branding_intro,
    require_branding_intro,
)
# _editorial_score is deliberately shared with the analyzer so the semantic
# authority override cannot drift from the canonical editorial formula.
from src.autoslice.content_evidence import _editorial_score, analyze_content_evidence
from src.autoslice.cpa_semantic_qa import (
    apply_cpa_semantic_qa_to_review_evidence,
    evaluate_cpa_semantic_response_artifact,
    load_request_artifact,
)
from src.autoslice.render_qa import RenderRequest, RenderedTimelineMetadata, evaluate_render_pts
from src.autoslice.review_evidence import ReviewEvidence, SourceCue, to_candidate_review
from src.autoslice.llm_client import LlmCall, LlmConfig, build_llm_call, extract_json_object
from src.autoslice.song_repair import (
    AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION,
    AudioLrcAligner,
    LYRIC_VOCAL_ASSERTION_KEYS,
    LrcProvider,
    LrcResult,
    SongRepairResult,
    attempt_song_repair,
    build_composite_lrc_provider,
    build_kugou_lrc_provider,
    build_lrclib_lrc_provider,
    build_netease_lrc_provider,
    derive_live_arrangement_completeness,
    fetch_lrclib_lrc,
    fetch_netease_lrc,
    load_audio_lrc_json_artifact,
    live_performance_failure_reason_codes,
    normalize_lyric_text,
    validate_audio_lrc_canonical_projection,
    validate_audio_lrc_execution_metadata,
    validate_live_performance_observation,
)
from src.autoslice.host_vocal_proof import verify_host_vocal_proof_claim
from src.autoslice.source_integrity import MediaSegmentObservation, build_source_range_ledger, plan_bilibili_replay_compensation
from src.autoslice.source_context_executor import AgyExecutionResult, SourceContextExecutionResult, execute_source_context_job
from src.autoslice.source_context_planner import JingtingJobProvenance, plan_source_context_jingting_jobs
from src.autoslice.subtitle_timing_qa import SpeechSpansProvider, sanitize_cue_timing
from src.autoslice.style_profile import ManualStyleProfile, apply_style_profile
from src.autoslice.subtitle_rendering import (
    ASS_MAX_CHARS_PER_LINE,
    ASS_MAX_VISUAL_LINES,
    ASS_MIN_SUBCUE_MS,
    _TEXT_BREAK_STRONG,
    _TEXT_BREAK_WEAK,
    _ass_escape_text,
    _escape_ffmpeg_filter_path,
    _format_ass_time,
    _layout_cue_for_display,
    _pack_segments,
    _parse_srt,
    _parse_time_ms,
    _split_text_segments,
    _wrap_ass_text,
    _write_lidousha_sapphire_ass_from_srt,
)

CHANNEL_PROFILE = load_channel_profile(ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id


def profile_asset_file(key: str) -> Path:
    return CHANNEL_PROFILE.asset_file(key, repo_root=ROOT)


def profile_asset_text(key: str) -> str:
    try:
        return profile_asset_file(key).read_text(encoding="utf-8").strip()
    except OSError:
        return "(资产文件缺失)"


HostVocalProver = Callable[
    [Path, str, Mapping[str, object], Mapping[str, object], Path],
    Mapping[str, object],
]


def run_shadow_pipeline(
    *,
    review_package: Path | None = None,
    source_video: Path | None = None,
    source_srt: Path | None = None,
    refined_srt: Path | None = None,
    source_context_job: Mapping[str, object] | None = None,
    agy_result: AgyExecutionResult | None = None,
    source_context_agy_runner: Callable[[Path, Path, Path], AgyExecutionResult] | None = None,
    room_id: str | None = None,
    title: str | None = None,
    output_dir: Path,
    no_upload: bool = True,
    source_context_run_ffmpeg: bool = True,
    lrc_provider: LrcProvider | None = None,
    song_hint_llm_call: LlmCall | None = None,
    song_lrc_queries: Sequence[str] = (),
    audio_lrc_aligner: AudioLrcAligner | None = None,
    host_vocal_prover: HostVocalProver | None = None,
    burn_preview: bool = False,
    branding_intro: Mapping[str, object] | None = None,
    publish_staging: bool = False,
    title_llm_call: LlmCall | None = None,
    art_direction_llm_call: LlmCall | None = None,
    speech_spans_provider: SpeechSpansProvider | None = None,
    fresh_talk_transcriber: Callable[[Path], str] | None = None,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "evidence").mkdir(exist_ok=True)

    if review_package is not None:
        summary = _run_review_package(review_package=review_package, output_dir=output_dir, no_upload=no_upload)
    elif source_video is not None:
        summary = _run_live_source(
            source_video=source_video,
            source_srt=source_srt,
            refined_srt=refined_srt,
            source_context_job=source_context_job,
            agy_result=agy_result,
            source_context_agy_runner=source_context_agy_runner,
            room_id=room_id,
            title=title,
            output_dir=output_dir,
            no_upload=no_upload,
            source_context_run_ffmpeg=source_context_run_ffmpeg,
            lrc_provider=lrc_provider,
            song_hint_llm_call=song_hint_llm_call,
            song_lrc_queries=song_lrc_queries,
            audio_lrc_aligner=audio_lrc_aligner,
            host_vocal_prover=host_vocal_prover,
            burn_preview=burn_preview,
            branding_intro=branding_intro,
            publish_staging=publish_staging,
            title_llm_call=title_llm_call,
            art_direction_llm_call=art_direction_llm_call,
            speech_spans_provider=speech_spans_provider,
            fresh_talk_transcriber=fresh_talk_transcriber,
        )
    else:
        raise ValueError("review_package or source_video is required")

    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "README.md").write_text(_readme(summary), encoding="utf-8")
    return summary


from src.autoslice.shadow_review import (
    _artifact_hashes,
    _base_checks,
    _build_shadow_decision_record,
    _build_shadow_inputs_record,
    _default_lidousha_profile,
    _empty_counts,
    _failed_block_reason_codes,
    _float,
    _gap_summary,
    _increment_action_count,
    _package_path,
    _package_path_exists,
    _read_jingting_provenance,
    _read_jingting_provenance_path,
    _read_json,
    _read_review_required,
    _read_review_required_marker,
    _read_upload_enabled,
    _record_paths_exist,
    _required_artifact_hash_checks,
    _required_artifact_hash_status,
    _review_required_failed,
    _run_review_package as _run_review_package_impl,
    _sha256,
    _shadow_item_paths,
    _stat_fingerprint,
    _write_json_file,
    _write_shadow_markers,
)


def _run_review_package(*, review_package: Path, output_dir: Path, no_upload: bool) -> dict[str, object]:
    """Compatibility seam for callers that patch the shadow evidence adapters."""

    return _run_review_package_impl(
        review_package=review_package,
        output_dir=output_dir,
        no_upload=no_upload,
        analyze_content=analyze_content_evidence,
        apply_style=apply_style_profile,
    )


def _run_live_source(
    *,
    source_video: Path,
    source_srt: Path | None,
    refined_srt: Path | None,
    source_context_job: Mapping[str, object] | None,
    agy_result: AgyExecutionResult | None,
    source_context_agy_runner: Callable[[Path, Path, Path], AgyExecutionResult] | None,
    room_id: str | None,
    title: str | None,
    output_dir: Path,
    no_upload: bool,
    source_context_run_ffmpeg: bool,
    lrc_provider: LrcProvider | None = None,
    song_hint_llm_call: LlmCall | None = None,
    song_lrc_queries: Sequence[str] = (),
    audio_lrc_aligner: AudioLrcAligner | None = None,
    host_vocal_prover: HostVocalProver | None = None,
    burn_preview: bool = False,
    branding_intro: Mapping[str, object] | None = None,
    publish_staging: bool = False,
    title_llm_call: LlmCall | None = None,
    art_direction_llm_call: LlmCall | None = None,
    speech_spans_provider: SpeechSpansProvider | None = None,
    fresh_talk_transcriber: Callable[[Path], str] | None = None,
) -> dict[str, object]:
    candidate_id = str((source_context_job or {}).get("candidate_id") or source_video.stem)
    title = title or candidate_id
    counts = _empty_counts()
    counts["candidates_evaluated"] = 1
    source_integrity = _live_source_integrity_record(source_video, source_context_job, room_id)

    if source_srt is None or not source_srt.is_file():
        record = _write_live_source_gap_record(
            output_dir=output_dir,
            candidate_id=candidate_id,
            reason_codes=("DRAFT_SRT_MISSING",),
            source_context=None,
        )
        counts["retry"] = 1
        return _live_source_summary(
            source_video=source_video,
            source_srt=source_srt,
            refined_srt=refined_srt,
            room_id=room_id,
            counts=counts,
            records=[record],
            no_upload=no_upload,
            source_integrity=source_integrity,
        )

    job_manifest = _prepare_live_source_job(
        source_video=source_video,
        source_srt=source_srt,
        source_context_job=source_context_job,
        room_id=room_id,
    )
    candidate_id = str(job_manifest.get("candidate_id") or candidate_id)
    source_context = execute_source_context_job(
        job_manifest,
        source_video_path=source_video,
        output_dir=output_dir / "source_context" / candidate_id,
        full_source_srt_path=source_srt,
        refined_srt_path=refined_srt,
        agy_result=agy_result,
        agy_runner=source_context_agy_runner or (_run_source_context_agy if refined_srt is None else None),
        run_ffmpeg=source_context_run_ffmpeg,
    )

    source_context_subtitle_path = source_context.context_refined_srt_path
    song_agy_context_fallback = bool(
        _job_is_song_candidate(job_manifest)
        and source_context.decision == "RETRY_INFRA"
        and source_context.context_draft_srt_path
        and Path(source_context.context_draft_srt_path).is_file()
        and set(source_context.reason_codes)
        & {
            "AGY_SOURCE_CONTEXT_RUNNER_FAILED",
            "AGY_QUOTA_EXHAUSTED",
            "AGY_EMPTY_OUTPUT",
            "AGY_FAILED_RC",
            "AGY_TIMEOUT",
            "AGY_AND_GEMINI_API_FAILED",
        }
    )
    if song_agy_context_fallback:
        source_context_subtitle_path = source_context.context_draft_srt_path
        job_manifest = {
            **dict(job_manifest),
            "song_context_subtitle_fallback": {
                "status": "USED_FOR_PROOF_CONTEXT_ONLY",
                "subtitle_path": source_context_subtitle_path,
                "reason_codes": list(source_context.reason_codes),
                "final_subtitle_authority": "verified_external_lrc_required",
            },
        }

    if (
        source_context.decision != "READY" and not song_agy_context_fallback
    ) or not source_context_subtitle_path:
        record = _write_live_source_gap_record(
            output_dir=output_dir,
            candidate_id=candidate_id,
            reason_codes=source_context.reason_codes,
            source_context=source_context,
        )
        counts["retry"] = 1 if source_context.decision.startswith("RETRY") else 0
        counts["block"] = 0 if source_context.decision.startswith("RETRY") else 1
        return _live_source_summary(
            source_video=source_video,
            source_srt=source_srt,
            refined_srt=refined_srt,
            room_id=room_id,
            counts=counts,
            records=[record],
            no_upload=no_upload,
            source_integrity=source_integrity,
        )

    context_start_ms = _int(_mapping(job_manifest.get("timeline")).get("context_start_ms"), 0)
    context_duration_ms = _int(_mapping(job_manifest.get("timeline")).get("context_duration_ms"), 0)
    cues = _parse_srt(Path(source_context_subtitle_path), source_offset_ms=context_start_ms)
    # ``source_srt`` is the fresh full-source ASR transcript (e.g. BCUT),
    # already source_video-relative and untouched by AGY subtitle refinement
    # — unlike ``cues`` above, which can be AGY-refined text/timing and is
    # rebased by ``context_start_ms``.  Song lyric global-shift anchoring
    # needs the former: AGY's own audio-listening offset can carry a uniform
    # lateness bias (2026-07-11 fix) that a fresh independent ASR timeline
    # does not share.
    song_asr_anchor_cues = _parse_srt(source_srt) if source_srt is not None else ()
    job_manifest, song_repair_result = _attempt_song_repair_stage(
        job_manifest,
        cues,
        candidate_id=candidate_id,
        output_dir=output_dir,
        lrc_provider=lrc_provider,
        hint_llm_call=song_hint_llm_call,
        extra_queries=song_lrc_queries,
        # The AGY audio/LRC proof, CAM++ proof, and final recut must share one
        # canonical source.  Proving a disposable context copy and rendering
        # from ``source_video`` left no exact verified-to-output path binding.
        source_media_path=source_video,
        audio_lrc_aligner=audio_lrc_aligner,
        asr_anchor_cues=song_asr_anchor_cues,
    )
    job_manifest = _attempt_host_vocal_proof_stage(
        job_manifest,
        candidate_id=candidate_id,
        # song_boundary/alignment timestamps are source-video-relative; the
        # refined context media may begin later and would shift every sample.
        source_media_path=source_video,
        output_dir=output_dir,
        host_vocal_prover=host_vocal_prover,
    )
    boundary_resolution = _resolve_live_source_boundary(job_manifest, cues, output_dir=output_dir)
    evidence = analyze_content_evidence(candidate_id=candidate_id, cues=cues, title=title)
    counts["candidates_with_content_evidence"] = 1
    evidence = _apply_live_source_context_metadata(
        evidence,
        source_context_job=job_manifest,
        source_context=source_context,
        boundary_resolution=boundary_resolution,
    )
    evidence = _apply_live_source_machine_evidence(
        evidence,
        source_context_job=job_manifest,
        source_context=source_context,
        title=title,
        output_dir=output_dir,
    )
    if song_repair_result is not None:
        evidence = replace(
            evidence,
            metadata={**dict(evidence.metadata), "song_repair": song_repair_result.to_manifest()},
        )
    duration_seconds = _duration_seconds(cues, context_duration_ms / 1000.0)
    evidence = apply_style_profile(evidence, _default_lidousha_profile(), title=title, duration_seconds=duration_seconds)
    counts["candidates_with_style_profile_evidence"] = 1
    lyric_timeline_loaded = _load_lyric_timeline(job_manifest, output_dir=output_dir)
    song_output_proof_binding = (
        _song_output_proof_binding(job_manifest, output_dir=output_dir)
        if lyric_timeline_loaded is not None
        else None
    )
    materialized_recut = _materialize_recut_record(
        source_video=source_video,
        candidate_id=candidate_id,
        boundary_resolution=boundary_resolution,
        output_dir=output_dir,
        cues=cues,
        run_ffmpeg=source_context_run_ffmpeg,
        lyric_timeline=lyric_timeline_loaded[0] if lyric_timeline_loaded else None,
        lyric_offset_ms=lyric_timeline_loaded[1] if lyric_timeline_loaded else None,
        song_output_proof_binding=song_output_proof_binding,
        speech_spans_provider=speech_spans_provider,
        fresh_talk_transcriber=fresh_talk_transcriber,
    )
    if burn_preview:
        materialized_recut = _burn_preview_subtitles(
            materialized_recut,
            run_ffmpeg=source_context_run_ffmpeg,
            branding_intro=branding_intro,
        )
    evidence = _apply_materialized_recut_render_qa(evidence, materialized_recut)
    evidence = _apply_cpa_semantic_review_from_job(evidence, job_manifest, output_dir=output_dir)
    evidence = _apply_semantic_authority_evidence(evidence, job_manifest)
    provenance = _read_jingting_provenance_path(Path(source_context.jingting_manifest_path) if source_context.jingting_manifest_path else None)
    live_review_required = _read_review_required_marker(
        Path(source_context.review_required_path) if source_context.review_required_path else None
    )
    verified_song_lrc_authority = bool(
        song_agy_context_fallback
        and lyric_timeline_loaded is not None
        and evidence.song_complete is True
        and evidence.lyrics_alignment_ready is True
    )
    decision = review_candidate(
        to_candidate_review(
            evidence,
            provenance,
            jingting_done=source_context.jingting_done,
            review_required=live_review_required,
            verified_song_lrc_authority=verified_song_lrc_authority,
        )
    )
    decision = _merge_cpa_semantic_review_into_decision(decision, evidence)
    decision = _merge_boundary_resolution_into_decision(decision, boundary_resolution)
    # Last: an unverified full-song claim must surface as BLOCK regardless of how
    # the talk-boundary fallback would otherwise dispose of the candidate.
    decision = _merge_song_proof_into_decision(decision, evidence)
    _increment_action_count(counts, decision.action.value)

    if publish_staging:
        materialized_recut = _stage_publish_after_release_gate(
            materialized_recut,
            decision=decision,
            candidate_id=candidate_id,
            title=title,
            cues=cues,
            output_dir=output_dir,
            run_ffmpeg=source_context_run_ffmpeg,
            title_llm_call=title_llm_call,
            art_direction_llm_call=art_direction_llm_call,
        )

    evidence_path = output_dir / "evidence" / f"{candidate_id}.evidence.json"
    evidence_path.write_text(json.dumps(evidence.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record = {
        "candidate_id": candidate_id,
        "title": title,
        "decision_action": decision.action.value,
        "reason_codes": list(decision.reason_codes),
        "score": decision.score,
        "evidence_path": str(evidence_path),
        "subtitle_source": (
            "verified_external_lrc"
            if verified_song_lrc_authority
            else "source_context_refined_srt"
            if source_context.context_refined_srt_path
            else "source_context_draft_for_song_proof"
        ),
        "subtitle_path": source_context_subtitle_path,
        "source_context_job": _source_context_job_record(job_manifest),
        "source_context": _source_context_record(source_context),
        "boundary_resolution": _boundary_resolution_record(boundary_resolution),
        "recut_plan": _recut_plan_record(
            source_video=source_video,
            candidate_id=candidate_id,
            boundary_resolution=boundary_resolution,
            output_dir=output_dir,
            strict_song_streams=lyric_timeline_loaded is not None,
        ),
        "materialized_recut": materialized_recut,
    }
    return _live_source_summary(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        room_id=room_id,
        counts=counts,
        records=[record],
        no_upload=no_upload,
        source_integrity=source_integrity,
    )


from src.autoslice.recut_materialization import (
    MATERIALIZED_RECUT_SCHEMA_VERSION,
    SONG_STREAM_CONTRACT_SCHEMA_VERSION,
    TALK_MATERIALIZED_RECUT_SCHEMA_VERSION,
    VERIFIED_SONG_OUTPUT_BINDING_SCHEMA_VERSION,
    _accurate_reencode_recut_command,
    _apply_materialized_recut_render_qa,
    _burn_preview_subtitles,
    _canonical_existing_path,
    _evaluate_materialized_recut_render_qa,
    _ffprobe_json,
    _float_or_none,
    _format_srt_time,
    _fresh_srt_to_source_cues,
    _load_lyric_timeline,
    _probe_rendered_timeline_metadata,
    _render_qa_actual_cut_error_ms,
    _sha256_prefixed,
    _song_output_proof_binding,
    _song_stream_contract,
    _video_dimensions,
    _write_bound_materialized_manifest,
    _write_lyric_timeline_srt,
    _write_source_range_srt,
    _materialize_recut_record as _materialize_recut_record_impl,
)


def _materialize_recut_record(
    *,
    source_video: Path,
    candidate_id: str,
    boundary_resolution: BoundaryResolution | None,
    output_dir: Path,
    cues: Sequence[SourceCue],
    run_ffmpeg: bool,
    lyric_timeline: Sequence[tuple[int, str]] | None = None,
    lyric_offset_ms: int | None = None,
    song_output_proof_binding: Mapping[str, object] | None = None,
    speech_spans_provider: SpeechSpansProvider | None = None,
    fresh_talk_transcriber: Callable[[Path], str] | None = None,
) -> dict[str, object] | None:
    """Compatibility seam for callers that patch render-QA evaluation."""

    return _materialize_recut_record_impl(
        source_video=source_video,
        candidate_id=candidate_id,
        boundary_resolution=boundary_resolution,
        output_dir=output_dir,
        cues=cues,
        run_ffmpeg=run_ffmpeg,
        lyric_timeline=lyric_timeline,
        lyric_offset_ms=lyric_offset_ms,
        song_output_proof_binding=song_output_proof_binding,
        speech_spans_provider=speech_spans_provider,
        fresh_talk_transcriber=fresh_talk_transcriber,
        evaluate_recut_render_qa=_evaluate_materialized_recut_render_qa,
    )


from src.autoslice.live_source_review import (
    _apply_cpa_semantic_failure,
    _apply_cpa_semantic_review_from_job,
    _apply_live_source_context_metadata,
    _apply_live_source_machine_evidence,
    _apply_semantic_authority_evidence,
    _attempt_host_vocal_proof_stage,
    _boundary_resolution_record,
    _cpa_semantic_optional,
    _cpa_semantic_request_path,
    _cpa_semantic_response_path,
    _default_source_context_job,
    _duplicate_similarity_from_job,
    _first_int,
    _host_vocal_post_song_anchor_start,
    _int,
    _job_anchor_candidate,
    _job_is_song_candidate,
    _job_provenance,
    _live_performance_block_reasons,
    _live_source_integrity_record,
    _live_source_summary,
    _load_known_songs,
    _mapping,
    _merge_boundary_resolution_into_decision,
    _merge_cpa_semantic_review_into_decision,
    _merge_song_proof_into_decision,
    _normalize_similarity_text,
    _optional_bool,
    _p95,
    _prepare_live_source_job,
    _recut_plan_record,
    _resolve_live_source_boundary,
    _resolve_song_boundary,
    _song_boundary_duration_seconds,
    _song_boundary_ready,
    _song_candidate_gate_reason_codes,
    _source_context_job_record,
    _source_context_record,
    _string_or_none,
    _subtitle_alignment_p95_ms,
    _tighten_song_boundary_to_verified_host_anchor,
    _to_talk_cue,
    _verify_host_vocal_claim,
    _verify_live_performance_observation,
    _verify_lyrics_alignment_proof,
    _write_live_source_gap_record,
    _attempt_song_repair_stage as _attempt_song_repair_stage_impl,
    _pinned_lrc_for_song as _pinned_lrc_for_song_impl,
)


def _pinned_lrc_for_song(cues: Sequence[SourceCue]) -> list[LrcResult]:
    """Compatibility seam for tests and callers that patch LRC fetchers."""

    return _pinned_lrc_for_song_impl(
        cues,
        fetch_lrclib=fetch_lrclib_lrc,
        fetch_netease=fetch_netease_lrc,
    )


def _attempt_song_repair_stage(
    job_manifest: Mapping[str, object],
    cues: Sequence[SourceCue],
    *,
    candidate_id: str,
    output_dir: Path,
    lrc_provider: LrcProvider | None,
    hint_llm_call: LlmCall | None = None,
    extra_queries: Sequence[str] = (),
    source_media_path: Path | None = None,
    audio_lrc_aligner: AudioLrcAligner | None = None,
    asr_anchor_cues: Sequence[SourceCue] = (),
) -> tuple[Mapping[str, object], SongRepairResult | None]:
    return _attempt_song_repair_stage_impl(
        job_manifest,
        cues,
        candidate_id=candidate_id,
        output_dir=output_dir,
        lrc_provider=lrc_provider,
        hint_llm_call=hint_llm_call,
        extra_queries=extra_queries,
        source_media_path=source_media_path,
        audio_lrc_aligner=audio_lrc_aligner,
        asr_anchor_cues=asr_anchor_cues,
        pinned_lrc_resolver=_pinned_lrc_for_song,
    )


LIDOUSHA_COVER_WORKFLOW = "cpa-openai-compatible-image-edit-cover-plus-approved-local-title-overlay"


# Title policy applies only to LLM-generated titles. Manual titles still pass
# through untouched in ``_stage_publish_draft``.  Keep these imports explicit
# because tests and older callers also import the compatibility names here.
from src.autoslice.title_policy import (  # noqa: E402
    _LIDOUSHA_TITLE_PREFIX,
    _SELECTION_HOOK_GENERIC_ANCHORS,
    _SELECTION_HOOK_GENERIC_WORDS,
    _SELECTION_HOOK_MEANINGLESS_RE,
    _TITLE_BANNED_FILLER_WORDS,
    _TITLE_BANNED_HYPE_WORDS,
    _TITLE_BANNED_MIAO_RE,
    _TITLE_BANNED_REGEXES,
    _TITLE_BANNED_SUFFIX_RE,
    _TITLE_MAX_ATTEMPTS,
    _TITLE_MAX_LEN,
    _TITLE_MIN_LEN,
    _TITLE_SUFFIX_ONLY_HYPE_WORDS,
    _ensure_lidousha_prefix,
    _selection_hook_anchor_valid,
    _selection_hook_fallback_title,
    _selection_hook_first_clause,
    _title_policy_violations,
)


def _stage_publish_after_release_gate(
    materialized_recut: dict[str, object] | None,
    *,
    decision: ReviewDecision,
    candidate_id: str,
    title: str,
    cues: Sequence[SourceCue],
    output_dir: Path,
    run_ffmpeg: bool,
    title_llm_call: LlmCall | None,
    art_direction_llm_call: LlmCall | None = None,
) -> dict[str, object] | None:
    """Persist the final content decision before any title or cover side effect."""

    recut = dict(materialized_recut or {})
    reason_codes = list(decision.reason_codes)
    gate_satisfied = bool(
        decision.action == DecisionAction.AUTO_UPLOAD
        and not reason_codes
        and recut.get("status") == "MATERIALIZED"
    )
    snapshot_path = output_dir / f"{candidate_id}.cover-release-gate.json"
    snapshot = {
        "schema_version": "slice-cover-release-gate.v1",
        "candidate_id": candidate_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision_action": decision.action.value,
        "reason_codes": reason_codes,
        "materialized_recut_status": recut.get("status"),
        "artifact_hashes": dict(recut.get("artifact_hashes") or {}),
        "satisfied": gate_satisfied,
    }
    _write_json_file(snapshot_path, snapshot)
    if materialized_recut is None:
        # Keep the release decision snapshot as audit evidence, but do not
        # manufacture a recut-shaped record when the singing gate prevented
        # materialization in the first place.
        return None
    recut["cover_release_gate"] = {**snapshot, "path": str(snapshot_path)}

    if not gate_satisfied:
        recut["publish_staging"] = {
            "status": "SKIPPED_RELEASE_GATE",
            "decision_action": decision.action.value,
            "reason_codes": reason_codes,
            "release_gate_path": str(snapshot_path),
            "upload_enabled": False,
        }
        return recut

    staged = _stage_publish_draft(
        recut,
        candidate_id=candidate_id,
        title=title,
        cues=cues,
        run_ffmpeg=run_ffmpeg,
        title_llm_call=title_llm_call,
        art_direction_llm_call=art_direction_llm_call,
    )
    if staged is not None:
        staged = dict(staged)
        staged["cover_release_gate"] = recut["cover_release_gate"]
    return staged


def _stage_publish_draft(
    materialized_recut: dict[str, object] | None,
    *,
    candidate_id: str,
    title: str,
    cues: Sequence[SourceCue],
    run_ffmpeg: bool,
    title_llm_call: LlmCall | None,
    art_direction_llm_call: LlmCall | None = None,
    skip_cover: bool = False,
    selection_hook: str | None = None,
) -> dict[str, object] | None:
    """Mirror production local_prepare: AI title + cover + publish.json draft.

    Always writes ``upload_enabled: false`` — publishing stays behind the
    AUTO_UPLOAD manifest/hash gate and is out of scope for the shadow lane.
    """

    if not materialized_recut or materialized_recut.get("status") != "MATERIALIZED":
        return materialized_recut
    record = dict(materialized_recut)
    media_path = Path(str(record["media_path"]))
    publish_json_path = media_path.with_suffix(".publish.json")

    # Iron rule: Ivan's manual title (title_llm_call=None) is final and passes
    # through a字不改 — no prefix forcing, no length gate, no policy check.
    # Prefix / length / banned-word enforcement applies ONLY to auto titles.
    staged_title = title
    title_source = "job_title"
    title_policy_violations: list[str] = []
    title_authority_error: str | None = None
    title_authority_status = "RESOLVED_MANUAL" if title_llm_call is None else "UNRESOLVED_AUTO"
    if title_llm_call is not None:
        selection_hook = str(selection_hook or "").strip()
        selection_hook_clause = _selection_hook_first_clause(selection_hook)
        transcript_sample = _staged_transcript_sample(record, cues)
        style_asset = profile_asset_text("title_style")
        persona_asset = profile_asset_text("persona")
        selection_hook_contract = ""
        output_contract = '{"title": "标题"}'
        if selection_hook:
            selection_hook_contract = (
                f"\n选片主钩子（这是为什么选中本片，权威高于后续陪衬话题）: {selection_hook}\n"
                f"标题必须保留第一分句的核心事件: {selection_hook_clause}\n"
                "同时输出 selection_hook_anchor：从该第一分句原样复制的 2–12 字具体短语，"
                f"避开‘{CHANNEL_PROFILE.display_name}/{CHANNEL_PROFILE.short_name}/主播/直播/弹幕/观众/自己/这个/那个/然后/时候/表演’等泛词；"
                "该短语必须逐字出现在标题里。不得把片段后半段的陪衬话题偷换成主标题。\n"
            )
            output_contract = '{"title": "标题", "selection_hook_anchor": "第一分句中的具体短语"}'
        base_prompt = (
            f"为一条{CHANNEL_PROFILE.display_name}(B站虚拟主播)的直播切片起中文标题。\n"
            f"最重要的原则：观众是因为'这是{CHANNEL_PROFILE.display_name}'才点进来的,不是因为内容——标题必须围绕{CHANNEL_PROFILE.display_name}本人"
            "(她的反应、气质、口癖、梗、名字谐音),切片内容只是辅助素材。引人注目为先。\n"
            f"\n{CHANNEL_PROFILE.display_name}特质:\n{persona_asset}\n"
            f"\n标题风格规范与历史标题范例(严格模仿这个风格):\n{style_asset}\n"
            f"\n本切片转写内容节选(辅助素材): {transcript_sample}\n"
            f"{selection_hook_contract}"
            f"硬性要求：含{CHANNEL_PROFILE.talk_title_prefix}前缀后 12–30 字；"
            "禁用空洞夸张词(炸裂/震惊/天花板/绝了/犯规/太顶),"
            "更不许用'X到犯规/炸裂/离谱'这种万能后缀——标题必须具体到这条切片里到底发生了什么"
            "(描述性的'越看越离谱/越整越离谱'这类是可以的,禁的是空洞的'X到离谱'后缀)。\n"
            f"只输出一个 JSON 对象：{output_contract}"
        )
        llm_title = ""
        llm_error: str | None = None
        # Bounded retry: regenerate up to _TITLE_MAX_ATTEMPTS times, calling out
        # the banned-word violation each retry so the model rewrites concretely.
        for attempt in range(_TITLE_MAX_ATTEMPTS):
            prompt = base_prompt
            if attempt > 0:
                prompt = (
                    base_prompt
                    + "\n注意：上一次标题违反了硬约束（违禁词，或没有保留选片第一分句的具体核心短语），已被否决。"
                    "不要用任何万能强调词，也不要把后续陪衬话题改成主标题；"
                    "写她具体做了/说了什么，并按要求重新只输出 JSON。"
                )
            try:
                payload = extract_json_object(title_llm_call(prompt))
            except Exception as exc:
                llm_error = type(exc).__name__
                break
            candidate = str(payload.get("title") or "").strip()
            if not candidate:
                llm_error = "empty_title"
                break
            llm_title = candidate
            title_policy_violations = _title_policy_violations(candidate)
            if selection_hook and not _selection_hook_anchor_valid(
                anchor=payload.get("selection_hook_anchor"),
                selection_hook=selection_hook,
                title=candidate,
            ):
                title_policy_violations.append("selection_hook_anchor_missing")
            if not title_policy_violations:
                break

        if llm_title:
            if selection_hook and "selection_hook_anchor_missing" in title_policy_violations:
                fallback = _selection_hook_fallback_title(selection_hook)
                if fallback is not None:
                    llm_title = fallback
                    title_policy_violations = _title_policy_violations(fallback)
                    title_source = "selection_hook_fallback_after_llm_mismatch"
            prefixed = _ensure_lidousha_prefix(llm_title)
            if _TITLE_MIN_LEN <= len(prefixed) <= _TITLE_MAX_LEN:
                staged_title = prefixed
                if title_source == "job_title":
                    title_source = f"llm+{PROFILE_ID}_style_asset"
                if title_policy_violations:
                    title_source = f"llm+{PROFILE_ID}_style_asset(title_policy_violation)"
                    title_authority_error = "title_policy_violation:" + ",".join(title_policy_violations)
                elif title_source == "selection_hook_fallback_after_llm_mismatch":
                    title_authority_status = "RESOLVED_DETERMINISTIC_FALLBACK"
                else:
                    title_authority_status = "RESOLVED_LLM"
            else:
                # Length gate rejects the auto title → fall back to the job title
                # untouched (prefix forcing never touches non-LLM titles).
                title_source = f"job_title(llm_length_out_of_bounds:{len(prefixed)})"
                title_authority_error = f"title_length_out_of_bounds:{len(prefixed)}"
        elif llm_error is not None:
            title_source = f"job_title(llm_failed: {llm_error})"
            title_authority_error = llm_error

    # Ivan 2026-07-13 梗词铁律的标题/封面确定性兜底（字幕面在
    # normalize_code_switch_surfaces；LLM 标题若仍写出「直女」这里回正）。
    staged_title = canonicalize_hard_surfaces(staged_title)
    cover_text = _lidousha_cover_text(staged_title)
    if title_authority_error is not None:
        # A candidate id / job fallback is not publish-title authority.  Fail
        # before art direction or any paid image request; the runner will keep
        # this attempt as title_failed and retry it under the bounded policy.
        cover_result = {
            "status": "BLOCKED_TITLE_AUTHORITY",
            "cover_path": None,
            "cover_generation": {
                "status": "NOT_ATTEMPTED",
                "reason": "title authority unresolved before cover generation",
                "attempted_models": [],
            },
            "reason_codes": ["TITLE_AUTHORITY_UNRESOLVED"],
        }
    elif skip_cover:
        # Subtitle-only re-run: keep the existing delivered cover, skip the
        # expensive AI cover (art-direction LLM + gpt-image-2 ~90s/clip).
        cover_result = {
            "status": "REUSED_COVER",
            "cover_path": None,
            "cover_generation": {"status": "REUSED", "note": "subtitle-only re-run: existing cover kept"},
            "reason_codes": [],
        }
    else:
        cover_result = _stage_lidousha_ai_cover(
            record,
            media_path=media_path,
            candidate_id=candidate_id,
            title=staged_title,
            cover_text=cover_text,
            run_ffmpeg=run_ffmpeg,
            art_direction_llm_call=art_direction_llm_call,
        )
    cover_status = str(cover_result["status"])
    cover_path_value = cover_result.get("cover_path") if cover_status == "AI_COVER_READY" else None
    cover_generation = cover_result["cover_generation"]
    raw_reason_codes = cover_result.get("reason_codes")
    reason_codes = [str(value) for value in raw_reason_codes] if isinstance(raw_reason_codes, list) else []
    artifact_hashes = {str(k): str(v) for k, v in dict(record.get("artifact_hashes") or {}).items()}
    for key in ("cover_sha256", "ai_background_sha256", "cover_reference_sha256"):
        value = cover_result.get(key)
        if isinstance(value, str) and value:
            artifact_hashes[key] = value

    publish_draft = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": candidate_id,
        "upload_enabled": False,
        "title": staged_title,
        "title_source": title_source,
        "title_authority_status": title_authority_status,
        "title_authority_error": title_authority_error,
        "title_policy_violations": title_policy_violations,
        "video_path": str(media_path),
        "cover_text": cover_text,
        "cover_path": cover_path_value,
        "cover_status": cover_status,
        "cover_generation": cover_generation,
        "reason_codes": reason_codes,
        "artifact_hashes": artifact_hashes,
    }
    publish_json_path.write_text(json.dumps(publish_draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record["artifact_hashes"] = artifact_hashes
    record["publish_staging"] = {
        "status": "STAGED" if title_authority_error is None else "BLOCKED_TITLE_AUTHORITY",
        "title": staged_title,
        "title_source": title_source,
        "title_authority_status": title_authority_status,
        "title_authority_error": title_authority_error,
        "title_policy_violations": title_policy_violations,
        "cover_status": cover_status,
        "cover_path": cover_path_value,
        "cover_text": cover_text,
        "cover_generation": cover_generation,
        "reason_codes": reason_codes,
        "publish_json_path": str(publish_json_path),
        "upload_enabled": False,
    }
    return record


def _stage_lidousha_ai_cover(
    materialized_recut: Mapping[str, object],
    *,
    media_path: Path,
    candidate_id: str,
    title: str,
    cover_text: str,
    run_ffmpeg: bool,
    art_direction_llm_call: LlmCall | None = None,
) -> dict[str, object]:
    cover_generation: dict[str, object] = {
        "workflow": LIDOUSHA_COVER_WORKFLOW,
        "method": "images.edit",
        "model": _cpa_image_model_candidates()[0],
        "image_gen_model": "cpa",
        "fallback_used": False,
        "model_fallback_used": False,
        "cover_text": cover_text,
        "title": title,
    }
    base_url = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("CPA_API_KEY", "").strip()
    if not base_url or not api_key:
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "CPA_CREDENTIALS_MISSING"],
            "CPA_BASE_URL/CPA_API_KEY missing; deterministic frame covers are not publish-grade",
        )
    if not run_ffmpeg:
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "COVER_REFERENCE_EXTRACTION_DISABLED"],
            "ffmpeg disabled, so no identity/reference frame can be extracted for CPA images.edit",
        )

    artifact_root = _materialized_artifact_root(materialized_recut, media_path)
    cover_refs_dir = artifact_root / "cover_refs"
    ai_dir = artifact_root / "covers_ai_original"
    covers_dir = artifact_root / "covers"
    evidence_dir = artifact_root / "evidence"
    for directory in (cover_refs_dir, ai_dir, covers_dir, evidence_dir):
        directory.mkdir(parents=True, exist_ok=True)

    reference_path = cover_refs_dir / f"{candidate_id}.cover-ref.png"
    # 受监督重产时可指定封面参考帧（内容时间轴毫秒，Ivan 点名画面用）；
    # 未设置则维持 thumbnail 自动代表帧。
    cover_ref_override = os.environ.get("AUTOSLICE_COVER_REF_MS", "").strip()
    if cover_ref_override.isdigit():
        ref_command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{int(cover_ref_override) / 1000:.3f}",
            "-i",
            str(media_path),
            "-vf",
            "scale=1920:-2",
            "-frames:v",
            "1",
            str(reference_path),
        ]
    else:
        ref_command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
            "-vf",
            "thumbnail=120,scale=1920:-2",
            "-frames:v",
            "1",
            str(reference_path),
        ]
    completed = subprocess.run(ref_command, check=False, capture_output=True, text=True)
    if completed.returncode != 0 or not reference_path.is_file():
        cover_generation["reference_command"] = ref_command
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "COVER_REFERENCE_EXTRACTION_FAILED"],
            completed.stderr[-500:] or "reference frame extraction failed",
        )

    # Art direction is picked AFTER the fail-closed gates (creds/ffmpeg/ref frame)
    # so a blocked cover never spends an LLM call. It is fail-OPEN (deterministic
    # baseline) while the cover IMAGE stays fail-closed.
    art_direction = _lidousha_cover_art_direction(
        candidate_id=candidate_id,
        title=title,
        cover_text=cover_text,
        art_direction_llm_call=art_direction_llm_call,
    )
    cover_generation["art_direction"] = asdict(art_direction)

    ai_background_path = ai_dir / f"{candidate_id}.ai-bg.cpa-image-edit.png"
    request_path = evidence_dir / f"{candidate_id}.cover-cpa-request.redacted.json"
    response_path = evidence_dir / f"{candidate_id}.cover-cpa-response.redacted.json"
    cpa_result = _call_cpa_image_edit(
        base_url=base_url,
        api_key=api_key,
        reference_path=reference_path,
        output_path=ai_background_path,
        prompt=_lidousha_cover_prompt(title=title, cover_text=cover_text, art_direction=art_direction),
        request_path=request_path,
        response_path=response_path,
    )
    cover_generation.update(
        {
            "reference_image": str(reference_path),
            "reference_sha256": "sha256:" + _sha256(reference_path),
            "request_path": str(request_path),
            "response_path": str(response_path),
            "attempted_models": list(cpa_result.get("attempted_models") or []),
            "model_fallback_used": bool(cpa_result.get("model_fallback_used")),
        }
    )
    if cpa_result.get("selected_model"):
        cover_generation["model"] = str(cpa_result["selected_model"])
    if cpa_result.get("status") != "AI_BACKGROUND_READY" or not ai_background_path.is_file():
        cover_generation["cpa_status"] = cpa_result.get("status")
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", str(cpa_result.get("reason_code") or "CPA_IMAGE_EDIT_FAILED")],
            str(cpa_result.get("detail") or "CPA image edit did not return an image"),
        )

    final_cover_path = covers_dir / f"{candidate_id}.ai-title.cover.png"
    overlay = _overlay_lidousha_cover_title(ai_background_path, final_cover_path, cover_text=cover_text, art_direction=art_direction)
    cover_generation.update(
        {
            "ai_background": str(ai_background_path),
            "ai_background_sha256": "sha256:" + _sha256(ai_background_path),
            "final_cover": str(final_cover_path),
            "final_cover_sha256": "sha256:" + _sha256(final_cover_path),
            **overlay,
        }
    )
    return {
        "status": "AI_COVER_READY",
        "reason_codes": [],
        "cover_path": str(final_cover_path),
        "cover_generation": cover_generation,
        "cover_sha256": "sha256:" + _sha256(final_cover_path),
        "ai_background_sha256": "sha256:" + _sha256(ai_background_path),
        "cover_reference_sha256": "sha256:" + _sha256(reference_path),
    }


def _blocked_ai_cover_result(cover_generation: Mapping[str, object], reason_codes: Sequence[str], detail: str) -> dict[str, object]:
    generation = {**dict(cover_generation), "status": "BLOCKED", "detail": detail}
    return {
        "status": "BLOCKED_AI_COVER_REQUIRED",
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "cover_path": None,
        "cover_generation": generation,
    }


def _materialized_artifact_root(materialized_recut: Mapping[str, object], media_path: Path) -> Path:
    manifest_value = materialized_recut.get("manifest_path")
    if isinstance(manifest_value, str) and manifest_value:
        manifest_path = Path(manifest_value)
        if manifest_path.parent.name in {"recuts", "media"}:
            return manifest_path.parent.parent
        return manifest_path.parent
    if media_path.parent.name in {"recuts", "media"}:
        return media_path.parent.parent
    return media_path.parent


def _staged_transcript_sample(record: Mapping[str, object], cues: Sequence[SourceCue]) -> str:
    """Title/cover text sample: prefer the FINAL subtitle (fresh transcription
    with glossary corrections) over the context cues, so the title uses the
    corrected names (Ado, 小室) rather than the coarse-ASR spellings."""

    subtitle_path = record.get("subtitle_path")
    if isinstance(subtitle_path, str) and Path(subtitle_path).is_file():
        try:
            from src.autoslice.jingting_chunker import parse_srt_cues

            parsed = parse_srt_cues(Path(subtitle_path).read_text(encoding="utf-8"))
            sample = " ".join(" ".join(cue.text.split()) for cue in parsed if cue.text.strip())[:600]
            if sample:
                return sample
        except OSError:
            pass
    return " ".join(cue.text.strip() for cue in cues if cue.text.strip())[:600]


# --------------------------------------------------------------------------
# Persona-driven cover art-direction (Ivan 2026-07-04 redesign).
# Old covers were "wallpaper + a single-color bottom title bar", all alike.
# The new system rotates layouts, matches the FACE to the clip's in-character
# role, varies the background, highlights a hook word, and backs the text with a
# soft dark card so any fill color reads on a bright pop background.  CPA still
# makes only a text-free background; the title is overlaid locally (fail-closed).
from src.autoslice.cover_generation import (
    LidoushaCoverArtDirection,
    _COVER_BASE_FILL,
    _COVER_BG_BUSY,
    _COVER_BG_CALM,
    _COVER_BG_PHRASES,
    _COVER_CANVAS,
    _COVER_CLOSING_PUNCT,
    _COVER_COMPATIBILITY_MODEL,
    _COVER_FORBIDDEN_EXPR,
    _COVER_HOOK_COLORS,
    _COVER_HOOK_LEXICON,
    _COVER_LAYOUT_RENDER,
    _COVER_MIN_EMPH,
    _COVER_MISSING_CHECKERS,
    _COVER_OPENING_PUNCT,
    _COVER_OUTLINE_NAVY_RATIO,
    _COVER_OUTLINE_WHITE_RATIO,
    _COVER_PRIMARY_MODEL,
    _COVER_REQUEST_SIZE,
    _COVER_ROLE_LEXICON,
    _COVER_SCRIM_BAR,
    _COVER_SCRIM_SIDE,
    _COVER_SCRIM_SOFT,
    _COVER_SONG_LAYOUT,
    _COVER_STROKE,
    _COVER_TALK_LAYOUTS,
    _COVER_TEXT_BACKING,
    _COVER_WHITE,
    _COVER_ZCOOL_WRONG_GLYPHS,
    _atom_em_width,
    _bind_punctuation_atoms,
    _build_cover_glow,
    _build_cover_panel,
    _call_cpa_image_edit as _cover_call_cpa_image_edit,
    _cover_art_direction_prompt,
    _cover_char_font,
    _cover_default_hook_word,
    _cover_draw_layered,
    _cover_fallback_font_path,
    _cover_font_for_text,
    _cover_fonts,
    _cover_line_width,
    _cover_lines_canon,
    _cover_missing_checker,
    _cover_outlines_for,
    _cover_role_from_title,
    _cover_seg_outlines,
    _cover_segment_line,
    _cover_stable_hash,
    _cpa_image_model_candidates,
    _explicit_cpa_model_unavailable,
    _find_cover_font,
    _fit_cover_lines,
    _lidousha_cover_art_direction,
    _lidousha_fontsdir,
    _lidousha_cover_prompt,
    _lidousha_cover_text,
    _lidousha_identity_descriptor,
    _multipart_form_data,
    _normalize_cover_art_direction,
    _normalize_cover_canvas,
    _overlay_lidousha_cover_title,
    _regroup_lines,
    _split_wide_atom,
    _split_wide_atoms,
    _validated_cover_lines,
    _validated_cover_words,
    _wrap_even,
)


def _call_cpa_image_edit(
    *,
    base_url: str,
    api_key: str,
    reference_path: Path,
    output_path: Path,
    prompt: str,
    request_path: Path,
    response_path: Path,
    timeout_seconds: float = 180.0,
    model_candidates: Sequence[str] | None = None,
) -> dict[str, object]:
    """Compatibility seam: shadow-module monkeypatches still steer canvas QA."""

    return _cover_call_cpa_image_edit(
        base_url=base_url,
        api_key=api_key,
        reference_path=reference_path,
        output_path=output_path,
        prompt=prompt,
        request_path=request_path,
        response_path=response_path,
        timeout_seconds=timeout_seconds,
        model_candidates=model_candidates,
        normalize_canvas=_normalize_cover_canvas,
    )


def _run_source_context_agy(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
    from scripts.gemini_slice_jingting import (
        AGY_MODEL,
        GEMINI_MODEL,
        parse_timeout_seconds,
        run_agy,
        run_gemini_api,
    )

    # The song selector intentionally gives long-form song proof a much larger
    # AGY budget.  Source-context text refinement is only the preferred first
    # lane before Gemini API and must not inherit that process-global budget.
    # Pass this budget explicitly so concurrent callers cannot race through
    # environment mutation and audio/LRC proof remains unchanged.
    # Formal blind runs completed healthy source-context AGY work in
    # 6m06s-8m06s.  Ten minutes keeps observed-good work alive while still
    # bounding a hung first provider below the independent 30m audio/LRC proof budget.
    print_timeout = os.environ.get("SOURCE_CONTEXT_AGY_PRINT_TIMEOUT", "10m").strip()
    if not re.fullmatch(r"[1-9]\d*[smh]?", print_timeout):
        print_timeout = "10m"
    try:
        timeout_grace_seconds = int(
            os.environ.get("SOURCE_CONTEXT_AGY_TIMEOUT_GRACE_SECONDS", "60")
        )
    except ValueError:
        timeout_grace_seconds = 60
    timeout_grace_seconds = min(300, max(0, timeout_grace_seconds))
    process_timeout_seconds = parse_timeout_seconds(print_timeout) + timeout_grace_seconds

    try:
        job_dir = run_agy(
            str(media_path),
            str(draft_srt_path),
            str(output_srt_path),
            print_timeout=print_timeout,
            process_timeout_seconds=process_timeout_seconds,
        )
    except Exception as agy_exc:
        try:
            job_dir = run_gemini_api(
                str(media_path), str(draft_srt_path), str(output_srt_path)
            )
        except Exception as gemini_exc:
            from src.autoslice.source_context_executor import AgyRunnerError

            retry_after = getattr(agy_exc, "retry_after_seconds", None)
            raise AgyRunnerError(
                "AGY_AND_GEMINI_API_FAILED",
                "AGY failed and Gemini API fallback also failed: "
                f"agy={type(agy_exc).__name__}; gemini={type(gemini_exc).__name__}: {gemini_exc}",
                retry_after_seconds=retry_after,
            ) from gemini_exc
        return AgyExecutionResult(
            provider="gemini_api",
            model=GEMINI_MODEL,
            agy_rc=None,
            provider_fallback_used=True,
            provider_request_id=job_dir,
        )
    return AgyExecutionResult(
        provider="agy",
        model=AGY_MODEL,
        agy_rc=0,
        provider_fallback_used=False,
        provider_request_id=job_dir,
    )


def _duration_seconds(cues: Sequence[SourceCue], default: float) -> float:
    if not cues:
        return default
    start_ms = min(cue.source_start_ms for cue in cues)
    end_ms = max(cue.source_end_ms for cue in cues)
    return max(0.001, (end_ms - start_ms) / 1000.0)


def _readme(summary: Mapping[str, object]) -> str:
    return "# Auto Review Shadow Pipeline\n\n```json\n" + json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n```\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local auto-review shadow evidence pipeline.")
    parser.add_argument("--review-package", type=Path)
    parser.add_argument("--source-video", type=Path)
    parser.add_argument("--source-srt", type=Path, help="Full source-level draft ASR/SRT for live-source shadow integration.")
    parser.add_argument("--refined-srt", type=Path, help="Precomputed agy/jingting refined context SRT for dry-run integration.")
    parser.add_argument("--source-context-job", type=Path, help="Optional source-context job manifest JSON.")
    parser.add_argument("--room-id")
    parser.add_argument("--title")
    parser.add_argument("--agy-model")
    parser.add_argument("--agy-rc", type=int)
    parser.add_argument("--agy-fallback-used", choices=("true", "false", "unknown"), default="unknown")
    parser.add_argument("--skip-ffmpeg", action="store_true", help="Use executor dry-run media placeholder instead of invoking ffmpeg.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-upload", action="store_true", help="Reserved; default shadow mode never uploads.")
    parser.add_argument("--lrc-provider", choices=("none", "netease", "lrclib", "kugou", "auto"), default="none", help="External LRC discovery provider for repair-first song completeness.")
    parser.add_argument("--burn-preview", action="store_true", help="Burn recut subtitles into a shadow preview render.")
    parser.add_argument(
        "--branding-intro-manifest",
        type=Path,
        help="Committed branding intro manifest; when it is enabled the burned render must carry the mandatory delivery intro (fail closed).",
    )
    parser.add_argument("--song-hint-llm-command", help="LLM command template ({prompt_file} {completion_file}) for song-name guessing from garbled ASR.")
    parser.add_argument("--publish-staging", action="store_true", help="Stage AI title + cover + publish.json draft (upload_enabled always false).")
    parser.add_argument("--title-llm-command", help="LLM command template for title generation; falls back to the job title.")
    parser.add_argument("--cover-art-direction-llm-command", help="LLM command template ({prompt_file} {completion_file}) that picks cover art direction (role/expression/background/layout/hook); falls back to the deterministic persona baseline.")
    args = parser.parse_args(argv)
    source_context_job = _read_json(args.source_context_job) if args.source_context_job else None
    if source_context_job is not None and not isinstance(source_context_job, Mapping):
        raise ValueError("--source-context-job must contain a JSON object")
    agy_result = None
    if args.agy_model or args.agy_rc is not None or args.agy_fallback_used != "unknown":
        agy_result = AgyExecutionResult(
            provider="agy",
            model=args.agy_model,
            agy_rc=args.agy_rc,
            provider_fallback_used={"true": True, "false": False, "unknown": None}[args.agy_fallback_used],
        )
    lrc_provider = None
    if args.lrc_provider == "netease":
        lrc_provider = build_netease_lrc_provider()
    elif args.lrc_provider == "lrclib":
        lrc_provider = build_lrclib_lrc_provider()
    elif args.lrc_provider == "kugou":
        lrc_provider = build_kugou_lrc_provider()
    elif args.lrc_provider == "auto":
        lrc_provider = build_composite_lrc_provider(
            build_netease_lrc_provider(),
            build_lrclib_lrc_provider(),
            build_kugou_lrc_provider(),
        )
    branding_intro = (
        require_branding_intro(ROOT, manifest_path=args.branding_intro_manifest)
        if args.branding_intro_manifest is not None
        else None
    )
    summary = run_shadow_pipeline(
        review_package=args.review_package,
        source_video=args.source_video,
        source_srt=args.source_srt,
        refined_srt=args.refined_srt,
        source_context_job=source_context_job if isinstance(source_context_job, Mapping) else None,
        agy_result=agy_result,
        room_id=args.room_id,
        title=args.title,
        output_dir=args.output_dir,
        no_upload=not args.allow_upload,
        source_context_run_ffmpeg=not args.skip_ffmpeg,
        lrc_provider=lrc_provider,
        song_hint_llm_call=build_llm_call(LlmConfig(transport="command", command_template=args.song_hint_llm_command))
        if args.song_hint_llm_command
        else None,
        burn_preview=args.burn_preview,
        branding_intro=branding_intro,
        publish_staging=args.publish_staging,
        title_llm_call=build_llm_call(LlmConfig(transport="command", command_template=args.title_llm_command))
        if args.title_llm_command
        else None,
        art_direction_llm_call=build_llm_call(LlmConfig(transport="command", command_template=args.cover_art_direction_llm_command))
        if args.cover_art_direction_llm_command
        else None,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
