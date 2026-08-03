"""Transactional recut materialization, subtitle burn, and artifact binding.

This leaf module owns media-side effects and their fail-closed hash/stream
contracts. Discovery and editorial review remain outside it.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .boundary_resolver import BoundaryResolution
from .branding_intro import BrandingIntroError, prepend_branding_intro
from .channel_profile import load_channel_profile
from .cover_generation import _profile_fontsdir
from .live_source_review import (
    _int,
    _mapping,
    _recut_plan_record,
    _verify_lyrics_alignment_proof,
)
from .render_qa import RenderRequest, RenderedTimelineMetadata, evaluate_render_pts
from .review_evidence import ReviewEvidence, SourceCue
from .shadow_review import _sha256
from .subtitle_rendering import (
    _escape_ffmpeg_filter_path,
    _write_sapphire_ass_from_srt,
)
from .subtitle_timing_qa import SpeechSpansProvider, sanitize_cue_timing

ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id


MATERIALIZED_RECUT_SCHEMA_VERSION = "materialized-recut.v2"

TALK_MATERIALIZED_RECUT_SCHEMA_VERSION = "materialized-recut.v1"

VERIFIED_SONG_OUTPUT_BINDING_SCHEMA_VERSION = "verified-song-output-binding.v1"

SONG_STREAM_CONTRACT_SCHEMA_VERSION = "song-av-stream-contract.v1"

def _sha256_prefixed(path: Path) -> str:
    return "sha256:" + _sha256(path)

def _canonical_existing_path(path: Path | str) -> str:
    return str(Path(path).resolve(strict=True))

def _song_stream_contract() -> dict[str, object]:
    return {
        "schema_version": SONG_STREAM_CONTRACT_SCHEMA_VERSION,
        "input_video_stream_index": 0,
        "input_audio_stream_index": 0,
        "ffmpeg_maps": ["0:v:0", "0:a:0"],
        "output_stream_types": ["video", "audio"],
        "allow_additional_streams": False,
        "allow_subtitle_streams": False,
        "allow_data_streams": False,
        "allow_attachment_streams": False,
    }

def _write_bound_materialized_manifest(path: Path, payload: Mapping[str, object]) -> str:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _sha256_prefixed(path)

def _video_dimensions(path: Path) -> tuple[int | None, int | None]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    parts = completed.stdout.strip().split(",")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None, None

def _burn_preview_subtitles(
    materialized_recut: dict[str, object] | None,
    *,
    run_ffmpeg: bool,
    branding_intro: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """Burn the recut subtitles into a preview render (shadow artifact, never published).

    ``branding_intro`` is the resolved delivery-time intro context from
    ``src.autoslice.branding_intro.require_branding_intro``.  Delivery entry
    points must pass it so the burned artifact — the exact bytes every later
    hash binding and upload manifest freezes — already carries the mandatory
    opening.  Preview/shadow callers leave it None.
    """

    if not materialized_recut or materialized_recut.get("status") != "MATERIALIZED":
        return materialized_recut
    media_path = Path(str(materialized_recut["media_path"]))
    subtitle_path = Path(str(materialized_recut["subtitle_path"]))
    record = dict(materialized_recut)
    strict_song_output = record.get("subtitle_source") == "external_lrc_global_shift"
    prebuilt_ass_value = record.get("subtitle_ass_path")
    if isinstance(prebuilt_ass_value, str) and prebuilt_ass_value:
        ass_path = Path(prebuilt_ass_value)
        burned_path = media_path.with_suffix(".burned-final-speaker.mp4")
        subtitle_style = str(
            record.get("subtitle_style")
            or f"{PROFILE_ID}-speaker-sapphire-host-white-guest-v2"
        )
        expected_ass_sha = (record.get("artifact_hashes") or {}).get("ass_sha256")
        actual_ass_sha = "sha256:" + _sha256(ass_path) if ass_path.is_file() else None
        if (
            actual_ass_sha is None
            or not isinstance(expected_ass_sha, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_ass_sha) is None
            or expected_ass_sha != actual_ass_sha
        ):
            record["burned_preview"] = {
                "status": "FAILED",
                "path": str(burned_path),
                "ass_path": str(ass_path),
                "reason_code": "PREBUILT_ASS_MISSING_OR_HASH_MISMATCH",
            }
            return record
    else:
        ass_path = media_path.with_suffix(".final-sapphire72.ass")
        burned_path = media_path.with_suffix(".burned-final-sapphire72.mp4")
        subtitle_style = f"{PROFILE_ID}-final-sapphire72"
        _write_sapphire_ass_from_srt(subtitle_path, ass_path)
    if not run_ffmpeg:
        burned_path.write_bytes(b"dry-run burned preview placeholder\n")
        record["burned_preview"] = {"status": "DRY_RUN", "path": str(burned_path), "ass_path": str(ass_path)}
        if branding_intro is not None:
            record["burned_preview"]["branding_intro"] = {
                "status": "DRY_RUN_SKIPPED",
                "intro_id": str(branding_intro.get("intro_id") or ""),
            }
        return record
    escaped_subtitle = _escape_ffmpeg_filter_path(ass_path)
    fontsdir = _profile_fontsdir(media_path)
    sub = f"subtitles='{escaped_subtitle}'"
    if fontsdir is not None:
        sub += f":fontsdir='{_escape_ffmpeg_filter_path(fontsdir)}'"
    w, h = _video_dimensions(media_path)
    vertical = w is not None and h is not None and h > w
    if vertical:
        # Vertical stream (e.g. 1080x1920) → horizontal 1920x1080 for Bilibili:
        # center the vertical video, fill the sides with a scaled + blurred
        # copy of itself (pillarbox), then burn subtitles on the 16:9 frame so
        # the subtitle is sized/positioned for 1920x1080.
        fc = (
            ("[0:v:0]" if strict_song_output else "[0:v]") + "split=2[bg][fg];"
            "[bg]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,gblur=sigma=24,eq=brightness=-0.06[bgb];"
            "[fg]scale=-2:1080[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:0[pad];"
            f"[pad]{sub}[v]"
        )
        stream_args = (
            [
                "-map", "[v]", "-map", "0:a:0",
                "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
            ]
            if strict_song_output
            else ["-map", "[v]", "-map", "0:a?"]
        )
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media_path),
            "-filter_complex", fc, *stream_args,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy", str(burned_path),
        ]
    else:
        stream_args = (
            [
                "-map", "0:v:0", "-map", "0:a:0",
                "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
            ]
            if strict_song_output
            else []
        )
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media_path),
            "-vf", sub, *stream_args,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy", str(burned_path),
        ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0 or not burned_path.is_file():
        record["burned_preview"] = {
            "status": "FAILED",
            "path": str(burned_path),
            "ass_path": str(ass_path),
            "stderr_tail": completed.stderr[-500:],
        }
        return record
    branding_intro_binding: dict[str, object] | None = None
    if branding_intro is not None:
        # Mandatory delivery intro (维护者): splice before hashing so
        # every downstream sha256 binding freezes the with-intro bytes.  Any
        # intro problem fails the burn instead of shipping without the intro.
        try:
            branding_intro_binding = prepend_branding_intro(
                context=branding_intro,
                main_path=burned_path,
                work_dir=burned_path.parent / (burned_path.name + ".intro-work"),
            )
        except (BrandingIntroError, subprocess.TimeoutExpired) as exc:
            record["burned_preview"] = {
                "status": "FAILED",
                "path": str(burned_path),
                "ass_path": str(ass_path),
                "reason_code": "BRANDING_INTRO_FAILED",
                "error": f"{type(exc).__name__}: {exc}",
            }
            return record
    burned_sha = "sha256:" + _sha256(burned_path)
    record["burned_preview"] = {
        "status": "BURNED",
        "path": str(burned_path),
        "ass_path": str(ass_path),
        "burned_sha256": burned_sha,
        "subtitle_style": subtitle_style,
        "pillarbox_16_9": bool(vertical),
        "command": command,
        "stream_contract": _song_stream_contract() if strict_song_output else None,
        "branding_intro": branding_intro_binding,
    }
    hashes = dict(record.get("artifact_hashes") or {})
    hashes["burned_video_sha256"] = burned_sha
    hashes["ass_sha256"] = "sha256:" + _sha256(ass_path)
    record["artifact_hashes"] = hashes
    if strict_song_output:
        manifest_value = record.get("manifest_path")
        manifest_path = Path(str(manifest_value)) if isinstance(manifest_value, str) else None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path is not None else None
        except (OSError, ValueError):
            manifest = None
        binding = dict(record.get("verified_output_binding") or {})
        binding_artifacts = dict(binding.get("artifacts") or {})
        if not isinstance(manifest, dict) or manifest.get("schema_version") != MATERIALIZED_RECUT_SCHEMA_VERSION or not binding:
            record["status"] = "RETRY_INFRA"
            record["reason_codes"] = list(dict.fromkeys([*(record.get("reason_codes") or []), "SONG_RECUT_MANIFEST_UPDATE_FAILED"]))
            record["burned_preview"] = {
                **dict(record["burned_preview"]),
                "status": "FAILED",
                "reason_code": "SONG_RECUT_MANIFEST_UPDATE_FAILED",
            }
            return record
        binding_artifacts.update(
            {
                "burned_media_path": _canonical_existing_path(burned_path),
                "burned_media_sha256": burned_sha,
                "ass_path": _canonical_existing_path(ass_path),
                "ass_sha256": hashes["ass_sha256"],
            }
        )
        binding["artifacts"] = binding_artifacts
        binding["burn_transform"] = {
            "schema_version": "song-subtitle-burn-transform.v1",
            "command": command,
            "subtitle_style": subtitle_style,
            "pillarbox_16_9": bool(vertical),
        }
        if branding_intro_binding is not None:
            binding["branding_intro"] = {
                "intro_id": branding_intro_binding["intro_id"],
                "intro_media_sha256": branding_intro_binding["intro_media_sha256"],
                "intro_offset_ms": branding_intro_binding["intro_offset_ms"],
                "method": branding_intro_binding["method"],
            }
        record["verified_output_binding"] = binding
        manifest["artifact_hashes"] = hashes
        manifest["burned_preview"] = dict(record["burned_preview"])
        manifest["verified_output_binding"] = binding
        record["manifest_sha256"] = _write_bound_materialized_manifest(manifest_path, manifest)
    return record

@dataclass(frozen=True)
class _RecutSubtitleState:
    subtitle_source: str
    timing_qa_record: dict[str, object] | None
    speech_spans: list | None


def _write_initial_recut_subtitles(
    *,
    source_video: Path,
    cues: Sequence[SourceCue],
    start_ms: int,
    end_ms: int,
    subtitle_path: Path,
    timing_qa_path: Path,
    lyric_timeline: Sequence[tuple[int, str]] | None,
    lyric_offset_ms: int | None,
    speech_spans_provider: SpeechSpansProvider | None,
) -> _RecutSubtitleState:
    """Materialize the authoritative initial subtitle timeline for the recut."""

    if lyric_timeline is not None and lyric_offset_ms is not None:
        _write_lyric_timeline_srt(
            lyric_timeline,
            lyric_offset_ms,
            start_ms,
            end_ms,
            subtitle_path,
        )
        return _RecutSubtitleState("external_lrc_global_shift", None, None)

    timing_qa_record: dict[str, object] | None = None
    speech_spans_cache: list | None = None
    sanitized_cues = cues
    if speech_spans_provider is not None:
        try:
            speech_spans_cache = list(
                speech_spans_provider(source_video, start_ms, end_ms)
            )
            sanitized_cues, timing_qa_record = sanitize_cue_timing(
                cues,
                speech_spans_cache,
                window_start_ms=start_ms,
                window_end_ms=end_ms,
            )
            timing_qa_path.write_text(
                json.dumps(
                    timing_qa_record,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            timing_qa_record = {
                "status": "SUBTITLE_TIMING_QA_UNAVAILABLE",
                "error": f"{type(exc).__name__}: {exc}",
            }
    _write_source_range_srt(sanitized_cues, start_ms, end_ms, subtitle_path)
    return _RecutSubtitleState("asr_cues", timing_qa_record, speech_spans_cache)


@dataclass(frozen=True)
class _FreshTalkSubtitleState:
    subtitle_source: str
    timing_qa_record: dict[str, object] | None
    fresh_transcription_record: dict[str, object] | None
    subtitle_sha256: str | None


def _refresh_talk_subtitles(
    *,
    media_path: Path,
    subtitle_path: Path,
    timing_qa_path: Path,
    subtitle_source: str,
    timing_qa_record: dict[str, object] | None,
    speech_spans_cache: list | None,
    start_ms: int,
    end_ms: int,
    duration_ms: int,
    transcriber: Callable[[Path], str] | None,
) -> _FreshTalkSubtitleState:
    """Replace coarse talk subtitles with a fresh final-media transcription."""

    if transcriber is None or subtitle_source != "asr_cues":
        return _FreshTalkSubtitleState(subtitle_source, timing_qa_record, None, None)
    try:
        clip_speech_spans = (
            [
                (span.start_ms - start_ms, span.end_ms - start_ms)
                for span in speech_spans_cache
            ]
            if speech_spans_cache
            else None
        )
        if len(inspect.signature(transcriber).parameters) >= 2:
            fresh_srt_text = transcriber(media_path, clip_speech_spans)
        else:
            fresh_srt_text = transcriber(media_path)
        fresh_cues = _fresh_srt_to_source_cues(
            fresh_srt_text,
            window_start_ms=start_ms,
            duration_ms=duration_ms,
        )
        sanitized_cues = fresh_cues
        if speech_spans_cache is not None:
            sanitized_cues, timing_qa_record = sanitize_cue_timing(
                fresh_cues,
                speech_spans_cache,
                window_start_ms=start_ms,
                window_end_ms=end_ms,
            )
            timing_qa_path.write_text(
                json.dumps(
                    timing_qa_record,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        _write_source_range_srt(sanitized_cues, start_ms, end_ms, subtitle_path)
        return _FreshTalkSubtitleState(
            "fresh_agy_transcription",
            timing_qa_record,
            {
                "status": "USED",
                "cue_count": len(fresh_cues),
                "replaced_subtitle_source": "asr_cues",
            },
            "sha256:" + _sha256(subtitle_path),
        )
    except Exception as exc:
        return _FreshTalkSubtitleState(
            subtitle_source,
            timing_qa_record,
            {
                "status": "FAILED_FALLBACK_ASR_CUES",
                "error": f"{type(exc).__name__}: {exc}",
            },
            None,
        )


@dataclass(frozen=True)
class _AccurateRecutState:
    render_qa: Mapping[str, object] | None
    accurate_rerender_used: bool
    accurate_command: list[str] | None


def _ensure_accurate_recut(
    *,
    source_video: Path,
    media_path: Path,
    candidate_id: str,
    render_qa_path: Path,
    start_ms: int,
    end_ms: int,
    duration_ms: int,
    strict_song_output: bool,
    subtitle_source: str,
    run_ffmpeg: bool,
    source_binding: Mapping[str, object] | None,
    render_qa: Mapping[str, object] | None,
    artifact_hashes: dict[str, str],
    reason_codes: list[str],
    evaluate_recut_render_qa: Callable[..., dict[str, object]] | None,
) -> _AccurateRecutState:
    """Repair packet/keyframe cut drift with a sample-accurate re-encode."""

    accurate_rerender_used = False
    accurate_command: list[str] | None = None
    cut_error_ms = _render_qa_actual_cut_error_ms(render_qa)
    needs_rerender = (
        cut_error_ms is not None and cut_error_ms > 100
    ) or subtitle_source == "external_lrc_global_shift"
    if not run_ffmpeg or not needs_rerender:
        return _AccurateRecutState(render_qa, False, None)
    accurate_command = _accurate_reencode_recut_command(
        source_video=source_video,
        output_media=media_path,
        start_ms=start_ms,
        duration_ms=duration_ms,
        strict_song_streams=strict_song_output,
    )
    completed = subprocess.run(
        accurate_command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        reason_codes.append("FFMPEG_ACCURATE_RECUT_FAILED")
        return _AccurateRecutState(render_qa, False, accurate_command)
    accurate_rerender_used = True
    artifact_hashes["video_sha256"] = "sha256:" + _sha256(media_path)
    if strict_song_output and source_binding is not None:
        try:
            source_sha_after = _sha256_prefixed(source_video)
        except OSError:
            source_sha_after = None
        if source_sha_after != source_binding.get("sha256"):
            reason_codes.append("SONG_SOURCE_DRIFT_DURING_RECUT")
    render_qa = (evaluate_recut_render_qa or _evaluate_materialized_recut_render_qa)(
        candidate_id=candidate_id,
        media_path=media_path,
        render_qa_path=render_qa_path,
        requested_start_ms=start_ms,
        requested_end_ms=end_ms,
        enabled=True,
    )
    return _AccurateRecutState(render_qa, accurate_rerender_used, accurate_command)


def _song_materialized_status(
    *,
    run_ffmpeg: bool,
    subtitle_source: str,
    accurate_rerender_used: bool,
    render_qa: Mapping[str, object] | None,
    reason_codes: list[str],
) -> str:
    if not run_ffmpeg or subtitle_source != "external_lrc_global_shift":
        return "MATERIALIZED"
    ready = (
        accurate_rerender_used is True
        and isinstance(render_qa, Mapping)
        and render_qa.get("pass") is True
        and "FFMPEG_ACCURATE_RECUT_FAILED" not in reason_codes
        and "SONG_SOURCE_DRIFT_DURING_RECUT" not in reason_codes
    )
    if not ready and "FFMPEG_ACCURATE_RECUT_FAILED" not in reason_codes:
        reason_codes.append("SONG_ACCURATE_RENDER_QA_FAILED")
    return "MATERIALIZED" if ready else "RETRY_INFRA"


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
    evaluate_recut_render_qa: Callable[..., dict[str, object]] | None = None,
) -> dict[str, object] | None:
    strict_song_output = lyric_timeline is not None and lyric_offset_ms is not None
    manifest_schema_version = (
        MATERIALIZED_RECUT_SCHEMA_VERSION
        if strict_song_output
        else TALK_MATERIALIZED_RECUT_SCHEMA_VERSION
    )
    source_binding: dict[str, object] | None = None
    stream_contract: dict[str, object] | None = None
    if strict_song_output:
        if not isinstance(song_output_proof_binding, Mapping):
            return {
                "status": "BLOCKED",
                "reason_codes": ["SONG_OUTPUT_PROOF_BINDING_MISSING"],
                "candidate_id": candidate_id,
                "artifact_hashes": {},
            }
        try:
            source_video = Path(_canonical_existing_path(source_video))
            source_binding = {
                "canonical_path": str(source_video),
                "sha256": _sha256_prefixed(source_video),
            }
        except OSError as exc:
            return {
                "status": "RETRY_INFRA",
                "reason_codes": ["SONG_SOURCE_BINDING_UNAVAILABLE"],
                "candidate_id": candidate_id,
                "error": f"{type(exc).__name__}: {exc}",
                "artifact_hashes": {},
            }
        stream_contract = _song_stream_contract()
    plan = _recut_plan_record(
        source_video=source_video,
        candidate_id=candidate_id,
        boundary_resolution=boundary_resolution,
        output_dir=output_dir,
        strict_song_streams=lyric_timeline is not None and lyric_offset_ms is not None,
    )
    if plan is None:
        return None
    if plan.get("status") != "PLANNED":
        return dict(plan)
    start_ms = _int(plan.get("start_ms"), 0)
    end_ms = _int(plan.get("end_ms"), start_ms)
    duration_ms = max(0, end_ms - start_ms)
    if strict_song_output:
        post_song_anchor_start_ms = song_output_proof_binding.get("post_song_anchor_start_ms")
        if (
            not isinstance(post_song_anchor_start_ms, int)
            or isinstance(post_song_anchor_start_ms, bool)
            or end_ms > post_song_anchor_start_ms
        ):
            return {
                "status": "BLOCKED",
                "reason_codes": ["SONG_OUTPUT_OVERLAPS_POST_SONG_HOST_ANCHOR"],
                "candidate_id": candidate_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "post_song_anchor_start_ms": post_song_anchor_start_ms,
                "source_binding": source_binding,
                "artifact_hashes": {},
            }
    media_path = Path(str(plan["output_media_path"]))
    subtitle_path = media_path.with_suffix(".srt")
    manifest_path = media_path.with_suffix(".manifest.json")
    render_qa_path = media_path.with_suffix(".render_qa.json")
    timing_qa_path = media_path.with_suffix(".timing_qa.json")
    media_path.parent.mkdir(parents=True, exist_ok=True)

    subtitle_state = _write_initial_recut_subtitles(
        source_video=source_video,
        cues=cues,
        start_ms=start_ms,
        end_ms=end_ms,
        subtitle_path=subtitle_path,
        timing_qa_path=timing_qa_path,
        lyric_timeline=lyric_timeline,
        lyric_offset_ms=lyric_offset_ms,
        speech_spans_provider=speech_spans_provider,
    )
    subtitle_source = subtitle_state.subtitle_source
    timing_qa_record = subtitle_state.timing_qa_record
    speech_spans_cache = subtitle_state.speech_spans

    reason_codes: list[str] = []
    if run_ffmpeg:
        completed = subprocess.run([str(part) for part in plan["command"]], check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            reason_codes.append("FFMPEG_RECUT_FAILED")
            manifest = {
                "schema_version": manifest_schema_version,
                "status": "RETRY_INFRA",
                "reason_codes": reason_codes,
                "requested_range": {"start_ms": start_ms, "end_ms": end_ms, "duration_ms": duration_ms},
                "command": plan["command"],
                "candidate_id": candidate_id,
                "source_binding": source_binding,
                "stream_contract": stream_contract,
                "song_output_proof_binding": dict(song_output_proof_binding or {}),
                "stderr_tail": completed.stderr[-1000:],
            }
            manifest_sha256 = _write_bound_materialized_manifest(manifest_path, manifest)
            return {
                "status": "RETRY_INFRA",
                "reason_codes": reason_codes,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": duration_ms,
                "media_path": str(media_path),
                "subtitle_path": str(subtitle_path),
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_sha256,
                "candidate_id": candidate_id,
                "source_binding": source_binding,
                "stream_contract": stream_contract,
                "dry_run_placeholder": False,
                "artifact_hashes": {},
            }
    else:
        media_path.write_bytes(b"dry-run materialized recut placeholder\n")

    artifact_hashes = {
        "video_sha256": "sha256:" + _sha256(media_path),
        "subtitle_sha256": "sha256:" + _sha256(subtitle_path),
    }
    render_qa = (evaluate_recut_render_qa or _evaluate_materialized_recut_render_qa)(
        candidate_id=candidate_id,
        media_path=media_path,
        render_qa_path=render_qa_path,
        requested_start_ms=start_ms,
        requested_end_ms=end_ms,
        enabled=run_ffmpeg,
    )
    accurate_state = _ensure_accurate_recut(
        source_video=source_video,
        media_path=media_path,
        candidate_id=candidate_id,
        render_qa_path=render_qa_path,
        start_ms=start_ms,
        end_ms=end_ms,
        duration_ms=duration_ms,
        strict_song_output=strict_song_output,
        subtitle_source=subtitle_source,
        run_ffmpeg=run_ffmpeg,
        source_binding=source_binding,
        render_qa=render_qa,
        artifact_hashes=artifact_hashes,
        reason_codes=reason_codes,
        evaluate_recut_render_qa=evaluate_recut_render_qa,
    )
    render_qa = accurate_state.render_qa
    accurate_rerender_used = accurate_state.accurate_rerender_used
    accurate_command = accurate_state.accurate_command

    # External-LRC timing is only valid against the sample-accurate re-render.
    # Keeping the coarse packet/keyframe copy as MATERIALIZED after that render
    # failed made a hash-valid but timing-quantized song eligible for delivery.
    # Dry-run placeholders remain inspectable, but every real song render must
    # prove both that the accurate command ran and that its fresh render QA
    # passed before materialization can be considered successful.
    materialized_status = _song_materialized_status(
        run_ffmpeg=run_ffmpeg,
        subtitle_source=subtitle_source,
        accurate_rerender_used=accurate_rerender_used,
        render_qa=render_qa,
        reason_codes=reason_codes,
    )

    # Fresh whole-window transcription (talk only): the coarse integer-second
    # production ASR is fine for recall but repeatedly shipped text/timing
    # mismatches in finals — re-transcribing the finished clip media gives
    # cue timing and text that actually correspond to the audio.  Runs after
    # the accurate re-render so the subtitle matches the final media exactly.
    # Fail-open with a recorded fallback: a transcriber outage must not kill
    # materialization, but it must be visible in the evidence.
    fresh_state = _refresh_talk_subtitles(
        media_path=media_path,
        subtitle_path=subtitle_path,
        timing_qa_path=timing_qa_path,
        subtitle_source=subtitle_source,
        timing_qa_record=timing_qa_record,
        speech_spans_cache=speech_spans_cache,
        start_ms=start_ms,
        end_ms=end_ms,
        duration_ms=duration_ms,
        transcriber=fresh_talk_transcriber,
    )
    subtitle_source = fresh_state.subtitle_source
    timing_qa_record = fresh_state.timing_qa_record
    fresh_transcription_record = fresh_state.fresh_transcription_record
    if fresh_state.subtitle_sha256 is not None:
        artifact_hashes["subtitle_sha256"] = fresh_state.subtitle_sha256
    recut_transform = (
        {
            "schema_version": "song-recut-transform.v1",
            "method": "two_stage_seek_reencode",
            "start_ms": start_ms,
            "duration_ms": duration_ms,
            "command": accurate_command,
            "video_codec": "libx264",
            "audio_codec": "aac",
        }
        if strict_song_output
        else None
    )
    verified_output_binding = (
        {
            "schema_version": VERIFIED_SONG_OUTPUT_BINDING_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "source": dict(source_binding or {}),
            "interval": {"start_ms": start_ms, "end_ms": end_ms, "duration_ms": duration_ms},
            "post_song_anchor_start_ms": song_output_proof_binding.get("post_song_anchor_start_ms"),
            "proofs": dict(song_output_proof_binding),
            "stream_contract": dict(stream_contract or {}),
            "recut_transform": recut_transform,
            "artifacts": {
                "recut_media_path": _canonical_existing_path(media_path),
                "recut_media_sha256": artifact_hashes["video_sha256"],
                "subtitle_path": _canonical_existing_path(subtitle_path),
                "subtitle_sha256": artifact_hashes["subtitle_sha256"],
            },
        }
        if strict_song_output
        else None
    )
    manifest = {
        "schema_version": manifest_schema_version,
        "status": materialized_status,
        "reason_codes": reason_codes,
        "candidate_id": candidate_id,
        "source_video_path": str(source_video),
        "source_binding": source_binding,
        "requested_range": {"start_ms": start_ms, "end_ms": end_ms, "duration_ms": duration_ms},
        "media_path": str(media_path),
        "subtitle_path": str(subtitle_path),
        "subtitle_source": subtitle_source,
        "lyric_offset_ms": lyric_offset_ms if subtitle_source == "external_lrc_global_shift" else None,
        "command": plan["command"],
        "accurate_command": accurate_command,
        "stream_contract": stream_contract,
        "recut_transform": recut_transform,
        "song_output_proof_binding": dict(song_output_proof_binding or {}),
        "verified_output_binding": verified_output_binding,
        "dry_run_placeholder": not run_ffmpeg,
        "accurate_rerender_used": accurate_rerender_used,
        "artifact_hashes": artifact_hashes,
        "render_qa": render_qa,
        "render_qa_path": str(render_qa_path) if render_qa is not None else None,
        "subtitle_timing_qa": timing_qa_record,
        "fresh_transcription": fresh_transcription_record,
    }
    manifest_sha256 = _write_bound_materialized_manifest(manifest_path, manifest)
    return {
        "status": materialized_status,
        "reason_codes": reason_codes,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": duration_ms,
        "candidate_id": candidate_id,
        "source_video_path": str(source_video),
        "source_binding": source_binding,
        "media_path": str(media_path),
        "subtitle_path": str(subtitle_path),
        "subtitle_source": subtitle_source,
        "lyric_offset_ms": lyric_offset_ms if subtitle_source == "external_lrc_global_shift" else None,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "dry_run_placeholder": not run_ffmpeg,
        "accurate_rerender_used": accurate_rerender_used,
        "accurate_command": accurate_command,
        "stream_contract": stream_contract,
        "recut_transform": recut_transform,
        "song_output_proof_binding": dict(song_output_proof_binding or {}),
        "verified_output_binding": verified_output_binding,
        "artifact_hashes": artifact_hashes,
        "render_qa": render_qa,
        "render_qa_path": str(render_qa_path) if render_qa is not None else None,
        "subtitle_timing_qa": timing_qa_record,
        "timing_qa_path": str(timing_qa_path) if timing_qa_record is not None and "counts" in timing_qa_record else None,
        "fresh_transcription": fresh_transcription_record,
    }

def _fresh_srt_to_source_cues(srt_text: str, *, window_start_ms: int, duration_ms: int) -> list[SourceCue]:
    """Validate a fresh clip-relative transcription and lift it onto the
    source timeline.  Fail loudly on garbage — the caller falls back to the
    ASR-cue subtitle and records why."""

    from src.autoslice.jingting_chunker import parse_srt_cues

    parsed = parse_srt_cues(srt_text)
    if len(parsed) < 3:
        raise ValueError(f"fresh transcription has too few cues ({len(parsed)})")
    previous_end = 0
    lifted: list[SourceCue] = []
    for index, cue in enumerate(parsed, start=1):
        if cue.start_ms < 0 or cue.end_ms <= cue.start_ms:
            raise ValueError(f"fresh transcription cue {index} has invalid timing {cue.start_ms}-{cue.end_ms}")
        if cue.start_ms < previous_end - 1_000:
            raise ValueError(f"fresh transcription cue {index} overlaps the previous cue by >1s")
        previous_end = max(previous_end, cue.end_ms)
        # Gemini timestamps drift slightly long near the clip tail: cues that
        # START past the clip are dropped, ends are clamped — one overrunning
        # tail cue must not discard an otherwise-good transcription.
        if cue.start_ms >= duration_ms:
            continue
        if not cue.text.strip():
            continue
        lifted.append(
            SourceCue(
                cue_id=f"fresh_{index:04d}",
                source_start_ms=window_start_ms + cue.start_ms,
                source_end_ms=window_start_ms + min(cue.end_ms, duration_ms),
                text=cue.text.strip(),
                language="zh",
                kind="speech",
                confidence=1.0,
            )
        )
    if len(lifted) < 3:
        raise ValueError("fresh transcription has too few non-empty cues")
    return lifted

def _render_qa_actual_cut_error_ms(render_qa: Mapping[str, object] | None) -> float | None:
    if not isinstance(render_qa, Mapping):
        return None
    evidence = _mapping(render_qa.get("evidence"))
    value = evidence.get("actual_cut_error_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None

def _accurate_reencode_recut_command(
    *,
    source_video: Path,
    output_media: Path,
    start_ms: int,
    duration_ms: int,
    coarse_preroll_ms: int = 10_000,
    strict_song_streams: bool = False,
) -> list[str]:
    # Two-stage seek: live-captured MPEG-TS has no reliable seek index, so a
    # pure input-side -ss can land *after* the requested point (byte-position
    # estimation) and the head goes missing.  Coarse input seek well before the
    # target, then decode-and-drop precisely on the output side.
    coarse_ms = max(0, start_ms - coarse_preroll_ms)
    fine_ms = start_ms - coarse_ms
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{coarse_ms / 1000:.3f}",
        "-i",
        str(source_video),
        "-ss",
        f"{fine_ms / 1000:.3f}",
        "-t",
        f"{duration_ms / 1000:.3f}",
        *(
            [
                "-map", "0:v:0",
                "-map", "0:a:0",
                "-sn",
                "-dn",
                "-map_metadata", "-1",
                "-map_chapters", "-1",
            ]
            if strict_song_streams
            else []
        ),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_media),
    ]

def _evaluate_materialized_recut_render_qa(
    *,
    candidate_id: str,
    media_path: Path,
    render_qa_path: Path,
    requested_start_ms: int,
    requested_end_ms: int,
    enabled: bool,
) -> dict[str, object] | None:
    if not enabled:
        check = {
            "code": "ACTUAL_CUT_ERROR",
            "pass": True,
            "severity": "PASS",
            "evidence": {
                "requested_start_ms": requested_start_ms,
                "requested_end_ms": requested_end_ms,
                "actual_start_ms": requested_start_ms,
                "actual_end_ms": requested_end_ms,
                "start_error_ms": 0,
                "end_error_ms": 0,
                "actual_cut_error_ms": 0,
                "threshold_ms": 100,
                "dry_run_placeholder": True,
            },
        }
        payload = {
            "schema_version": "materialized-render-qa.v1",
            "candidate_id": candidate_id,
            "media_path": str(media_path),
            "check": check,
            "dry_run_placeholder": True,
        }
        render_qa_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return check
    metadata = _probe_rendered_timeline_metadata(
        media_path,
        requested_start_ms=requested_start_ms,
    )
    qa = evaluate_render_pts(
        RenderRequest(
            candidate_id=candidate_id,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
        ),
        metadata,
    )
    payload = {
        "schema_version": "materialized-render-qa.v1",
        "candidate_id": candidate_id,
        "media_path": str(media_path),
        "check": qa.to_manifest_check(),
    }
    render_qa_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return qa.to_manifest_check()

def _probe_rendered_timeline_metadata(
    media_path: Path,
    *,
    requested_start_ms: int,
) -> RenderedTimelineMetadata | None:
    if not media_path.is_file():
        return None
    packet_json = _ffprobe_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,duration_time",
            "-of",
            "json",
            str(media_path),
        ]
    )
    packets = packet_json.get("packets")
    if not isinstance(packets, list) or not packets:
        return None
    first_packet = packets[0] if isinstance(packets[0], Mapping) else None
    last_packet = packets[-1] if isinstance(packets[-1], Mapping) else None
    if first_packet is None or last_packet is None:
        return None
    first_pts_seconds = _float_or_none(first_packet.get("pts_time"))
    last_pts_seconds = _float_or_none(last_packet.get("pts_time"))
    last_duration_seconds = _float_or_none(last_packet.get("duration_time"))
    if first_pts_seconds is None or last_pts_seconds is None:
        return None
    if last_duration_seconds is None:
        last_duration_seconds = 0.0
    return RenderedTimelineMetadata(
        actual_start_ms=requested_start_ms + int(round(first_pts_seconds * 1000.0)),
        actual_end_ms=requested_start_ms + int(round((last_pts_seconds + last_duration_seconds) * 1000.0)),
    )

def _apply_materialized_recut_render_qa(
    evidence: ReviewEvidence,
    materialized_recut: Mapping[str, object] | None,
) -> ReviewEvidence:
    if not isinstance(materialized_recut, Mapping):
        return evidence
    render_qa = materialized_recut.get("render_qa")
    if not isinstance(render_qa, Mapping):
        return evidence
    qa_evidence = _mapping(render_qa.get("evidence"))
    actual_cut_error_ms = qa_evidence.get("actual_cut_error_ms")
    checks = tuple(evidence.checks) + (dict(render_qa),)
    metadata = {
        **dict(evidence.metadata),
        "materialized_recut": {
            "status": materialized_recut.get("status"),
            "media_path": materialized_recut.get("media_path"),
            "subtitle_path": materialized_recut.get("subtitle_path"),
            "manifest_path": materialized_recut.get("manifest_path"),
            "render_qa": dict(render_qa),
            "render_qa_path": materialized_recut.get("render_qa_path"),
        },
    }
    updates: dict[str, object] = {"checks": checks, "metadata": metadata}
    if isinstance(actual_cut_error_ms, (int, float)) and not isinstance(actual_cut_error_ms, bool):
        updates["actual_cut_error_ms"] = float(actual_cut_error_ms)
    return replace(evidence, **updates)

def _load_lyric_timeline(
    job_manifest: Mapping[str, object],
    *,
    output_dir: Path,
) -> tuple[list[tuple[int, str]], int] | None:
    """Load the proven external-LRC timeline + global shift for a song job.

    The strict song process burns lyrics from ``lrc_time + offset``, never from
    raw ASR cue timings (ASR onsets are systematically early/noisy).  Only a
    verified lyrics-alignment proof may supply this timeline.
    """

    lyrics_alignment = _mapping(job_manifest.get("lyrics_alignment"))
    if _verify_lyrics_alignment_proof(lyrics_alignment, output_dir=output_dir) is not None:
        return None
    report_path = Path(str(lyrics_alignment["alignment_report_path"]))
    if not report_path.is_absolute() and not report_path.is_file():
        report_path = output_dir / report_path
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(report, Mapping):
        return None

    timeline: list[tuple[int, str]] = []
    lyric_lines = report.get("lyric_lines")
    if isinstance(lyric_lines, list):
        for line in lyric_lines:
            if isinstance(line, Mapping) and isinstance(line.get("lrc_time_ms"), int) and str(line.get("text") or "").strip():
                timeline.append((int(line["lrc_time_ms"]), str(line["text"]).strip()))
    if not timeline:
        alignment = report.get("alignment")
        if isinstance(alignment, list):
            for entry in alignment:
                if isinstance(entry, Mapping) and isinstance(entry.get("lrc_time_ms"), int) and str(entry.get("lrc_text") or "").strip():
                    timeline.append((int(entry["lrc_time_ms"]), str(entry["lrc_text"]).strip()))
    if not timeline:
        return None
    timeline.sort(key=lambda item: item[0])

    offset_value = lyrics_alignment.get("offset_ms")
    if not isinstance(offset_value, int) or isinstance(offset_value, bool):
        offset_value = report.get("offset_ms")
    if not isinstance(offset_value, int) or isinstance(offset_value, bool):
        return None
    return timeline, offset_value

def _song_output_proof_binding(
    job_manifest: Mapping[str, object],
    *,
    output_dir: Path,
) -> dict[str, object] | None:
    """Return the exact proof artifacts that authorize a song output.

    This is producer metadata, not the final trust decision: the unattended
    runner reopens every artifact and compares the same fields independently.
    """

    alignment = _mapping(job_manifest.get("lyrics_alignment"))
    host_claim = _mapping(job_manifest.get("host_vocal_proof"))
    if _verify_lyrics_alignment_proof(alignment, output_dir=output_dir) is not None:
        return None
    report_value = alignment.get("alignment_report_path")
    host_value = host_claim.get("proof_path")
    if not isinstance(report_value, str) or not isinstance(host_value, str):
        return None
    report_path = Path(report_value)
    if not report_path.is_absolute() and not report_path.is_file():
        report_path = output_dir / report_path
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    audio_artifacts = _mapping(report.get("audio_alignment_artifacts")) if isinstance(report, Mapping) else {}
    agy_manifest_value = audio_artifacts.get("run_manifest_path")
    post_song_anchor_start_ms = report.get("post_song_talk_start_ms") if isinstance(report, Mapping) else None
    if (
        not isinstance(agy_manifest_value, str)
        or not isinstance(post_song_anchor_start_ms, int)
        or isinstance(post_song_anchor_start_ms, bool)
    ):
        return None
    try:
        return {
            "lyrics_alignment_report_path": _canonical_existing_path(report_path),
            "lyrics_alignment_report_sha256": str(alignment.get("alignment_report_sha256") or ""),
            "host_vocal_proof_path": _canonical_existing_path(host_value),
            "host_vocal_proof_sha256": str(host_claim.get("proof_sha256") or ""),
            "agy_run_manifest_path": _canonical_existing_path(agy_manifest_value),
            "agy_run_manifest_sha256": str(audio_artifacts.get("run_manifest_sha256") or ""),
            "post_song_anchor_start_ms": post_song_anchor_start_ms,
        }
    except OSError:
        return None

def _write_lyric_timeline_srt(
    timeline: Sequence[tuple[int, str]],
    offset_ms: int,
    start_ms: int,
    end_ms: int,
    output_path: Path,
    *,
    max_duration_ms: int = 6_000,
    min_duration_ms: int = 800,
    tail_pad_ms: int = 6_500,
) -> int:
    """Write clip-local lyric SRT from the external LRC global-shift model.

    Mirrors ``build_cues`` in the song-lyrics-timeline-aligner skill script:
    cue end = next lyric start capped at +6s (no line hangs through a long
    instrumental gap), minimum display 0.8s, final line padded 6.5s.
    """

    duration_ms = max(0, end_ms - start_ms)
    mapped = [(lrc_time_ms + offset_ms - start_ms, text) for lrc_time_ms, text in timeline]
    rows: list[str] = []
    index = 1
    for position, (cue_start_ms, text) in enumerate(mapped):
        if cue_start_ms < 0 or cue_start_ms >= duration_ms:
            continue
        next_start_ms = mapped[position + 1][0] if position + 1 < len(mapped) else None
        if next_start_ms is None:
            cue_end_ms = cue_start_ms + tail_pad_ms
        else:
            cue_end_ms = min(next_start_ms, cue_start_ms + max_duration_ms)
        if cue_end_ms - cue_start_ms < min_duration_ms:
            cue_end_ms = cue_start_ms + min_duration_ms
            if next_start_ms is not None and cue_end_ms > next_start_ms:
                cue_end_ms = max(cue_start_ms + 100, next_start_ms)
        cue_end_ms = min(cue_end_ms, duration_ms)
        if cue_end_ms <= cue_start_ms:
            continue
        rows.append(
            f"{index}\n{_format_srt_time(cue_start_ms)} --> {_format_srt_time(cue_end_ms)}\n{text.strip()}\n"
        )
        index += 1
    output_path.write_text("\n".join(rows).rstrip() + ("\n" if rows else ""), encoding="utf-8")
    return index - 1

BOUNDARY_CLIPPED_CUE_MAX_VISIBLE_MS = 300
BOUNDARY_CUE_START_TOLERANCE_MS = 50


def _write_source_range_srt(cues: Sequence[SourceCue], start_ms: int, end_ms: int, output_path: Path) -> None:
    rows: list[str] = []
    index = 1
    for cue in cues:
        clipped_start_ms = max(cue.source_start_ms, start_ms)
        clipped_end_ms = min(cue.source_end_ms, end_ms)
        if clipped_end_ms <= clipped_start_ms:
            continue
        # Boundary padding protects the first phoneme of the selected opening,
        # but it can also expose only 250 ms of the previous subtitle.  A flash
        # fragment is unreadable and often belongs to the prior topic; keep the
        # audio pre-roll while leaving that sliver intentionally unsubtitled.
        if (
            clipped_start_ms - start_ms <= BOUNDARY_CUE_START_TOLERANCE_MS
            and clipped_end_ms - clipped_start_ms
            <= BOUNDARY_CLIPPED_CUE_MAX_VISIBLE_MS
        ):
            continue
        relative_start_ms = clipped_start_ms - start_ms
        relative_end_ms = clipped_end_ms - start_ms
        rows.append(
            f"{index}\n{_format_srt_time(relative_start_ms)} --> {_format_srt_time(relative_end_ms)}\n{cue.text.strip()}\n"
        )
        index += 1
    output_path.write_text("\n".join(rows).rstrip() + ("\n" if rows else ""), encoding="utf-8")

def _format_srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    millis = ms % 1000
    total_seconds = ms // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

def _ffprobe_json(command: Sequence[str]) -> dict[str, object]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        return {}
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}

def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
