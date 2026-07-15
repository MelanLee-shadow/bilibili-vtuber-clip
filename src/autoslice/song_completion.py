"""Song delivery completion evidence + AV-stream contract checks.

Extracted verbatim from scripts/free_session_autoslice.py (2026-07-15 屎山
治理第二刀): the 843-line pure `song_completion_evidence` proof function plus
its exclusively-used stream-contract helpers. All callers live in the runner
namespace and reach this via the runner's thin `song_completion_evidence`
wrapper, which injects `_has_exact_av_streams` so tests that patch
`runner._has_exact_av_streams` still steer the real proof path.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from src.autoslice.verified_io import (
    _matches_sha256,
    _canonical_existing_path,
    _normalized_sha256,
)
from src.autoslice.host_vocal_proof import verify_host_vocal_proof_claim
from src.autoslice.song_repair import (
    AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION,
    LYRIC_VOCAL_ASSERTION_KEYS,
    derive_live_arrangement_completeness,
    load_audio_lrc_json_artifact,
    live_performance_failure_reason_codes,
    validate_audio_lrc_canonical_projection,
    validate_audio_lrc_execution_metadata,
    validate_live_performance_observation,
)

MATERIALIZED_RECUT_SCHEMA_VERSION = "materialized-recut.v2"
VERIFIED_SONG_OUTPUT_BINDING_SCHEMA_VERSION = "verified-song-output-binding.v1"
SONG_STREAM_CONTRACT_SCHEMA_VERSION = "song-av-stream-contract.v1"


def _expected_song_stream_contract() -> dict[str, object]:
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


def _expected_song_recut_command(
    source_path: str,
    output_path: str,
    start_ms: int,
    duration_ms: int,
    *,
    coarse_preroll_ms: int = 10_000,
) -> list[str]:
    coarse_ms = max(0, start_ms - coarse_preroll_ms)
    fine_ms = start_ms - coarse_ms
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{coarse_ms / 1000:.3f}", "-i", source_path,
        "-ss", f"{fine_ms / 1000:.3f}", "-t", f"{duration_ms / 1000:.3f}",
        "-map", "0:v:0", "-map", "0:a:0",
        "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", output_path,
    ]


def _burn_command_obeys_song_stream_contract(command: object, *, input_path: str, output_path: str) -> bool:
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        return False
    try:
        input_index = command.index("-i")
    except ValueError:
        return False
    maps = [command[index + 1] for index, part in enumerate(command[:-1]) if part == "-map"]
    required_pairs = (("-map_metadata", "-1"), ("-map_chapters", "-1"))
    return (
        input_index + 1 < len(command)
        and _canonical_existing_path(command[input_index + 1]) == input_path
        and _canonical_existing_path(command[-1]) == output_path
        and len(maps) == 2
        and maps[0] in {"0:v:0", "[v]"}
        and maps[1] == "0:a:0"
        and not any("?" in item for item in maps)
        and "-sn" in command
        and "-dn" in command
        and all(any(command[index:index + 2] == [flag, value] for index in range(len(command) - 1)) for flag, value in required_pairs)
    )


def _has_exact_av_streams(path: Path) -> bool:
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "stream=index,codec_type",
                "-of", "json", str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return False
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list) or len(streams) != 2:
        return False
    types = [stream.get("codec_type") for stream in streams if isinstance(stream, dict)]
    return sorted(types) == ["audio", "video"]


def song_completion_evidence(
    record: dict, *, has_exact_av_streams=None, host_vocal_profile=None
) -> dict:
    """Verify the positive, hash-bound proof required to deliver a song.

    This deliberately duplicates the final edge checks from the selector at
    the unattended-runner boundary.  A stale/globbed burned MP4 must not escape
    merely because an earlier selector process happened to leave it on disk.

    ``has_exact_av_streams`` is injected by the runner wrapper (defaulting to
    this module's own) so a test that monkeypatches ``runner._has_exact_av_streams``
    still reaches the real proof path after this function moved out of the runner.
    """
    if has_exact_av_streams is None:
        has_exact_av_streams = _has_exact_av_streams
    failures: list[str] = []
    live_performance_status: str | None = None
    live_performance_mode: str | None = None
    live_performance_confidence: float | None = None
    live_performance_semantic_ready = False
    audio_artifacts: dict = {}
    audio_manifest: dict | None = None
    host_proof: dict | None = None

    def is_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    job = record.get("source_context_job")
    if not isinstance(job, dict):
        job = {}
    boundary = job.get("song_boundary")
    if not isinstance(boundary, dict) or boundary.get("status") != "FULL_SONG_READY":
        boundary = {}
        failures.append("SONG_FULL_BOUNDARY_PROOF_MISSING")
    boundary_values = [boundary.get(key) for key in ("clip_start_ms", "first_lyric_start_ms", "last_lyric_end_ms", "clip_end_ms")]
    if boundary and not all(is_int(value) for value in boundary_values):
        failures.append("SONG_BOUNDARY_TIMELINE_MISSING")
    elif boundary and not (0 <= boundary_values[0] <= boundary_values[1] <= boundary_values[2] <= boundary_values[3]):
        failures.append("SONG_BOUNDARY_TIMELINE_INVALID")
    boundary_zero = boundary.get("nominal_lrc_zero_ms") if boundary else None
    if boundary and (
        not is_int(boundary_zero)
        or not all(is_int(value) for value in boundary_values)
        or not (boundary_values[0] <= boundary_zero <= boundary_values[1])
    ):
        failures.append("SONG_NOMINAL_LRC_ZERO_INVALID")

    alignment = job.get("lyrics_alignment")
    report: dict | None = None
    if not isinstance(alignment, dict) or alignment.get("status") != "READY":
        alignment = {}
        failures.append("SONG_LYRICS_ALIGNMENT_PROOF_MISSING")
    else:
        for field in ("provider", "model"):
            if not isinstance(alignment.get(field), str) or not str(alignment[field]).strip():
                failures.append(f"SONG_LYRICS_{field.upper()}_MISSING")
        source = alignment.get("source") or alignment.get("external_lrc")
        if not isinstance(source, str) or not source.strip():
            failures.append("SONG_EXTERNAL_LRC_SOURCE_MISSING")
        report_value = alignment.get("alignment_report_path")
        report_sha = alignment.get("alignment_report_sha256")
        if not isinstance(report_value, str) or not report_value:
            failures.append("SONG_ALIGNMENT_REPORT_MISSING")
        elif not isinstance(report_sha, str) or not _matches_sha256(Path(report_value), report_sha):
            failures.append("SONG_ALIGNMENT_REPORT_HASH_INVALID")
        else:
            try:
                loaded_report = json.loads(Path(report_value).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded_report = None
            if not isinstance(loaded_report, dict):
                failures.append("SONG_ALIGNMENT_REPORT_INVALID_JSON")
            else:
                report = loaded_report

    if report is not None:
        if report.get("schema_version") != "lyrics-alignment-report.v1":
            failures.append("SONG_ALIGNMENT_REPORT_SCHEMA_INVALID")
        if report.get("alignment_model") != "external_lrc_global_shift.v1":
            failures.append("SONG_ALIGNMENT_REPORT_MODEL_INVALID")
        if report.get("provider") != alignment.get("provider"):
            failures.append("SONG_ALIGNMENT_PROVIDER_MISMATCH")
        external_lrc = alignment.get("external_lrc")
        if not isinstance(external_lrc, str) or report.get("source_ref") != external_lrc:
            failures.append("SONG_ALIGNMENT_SOURCE_MISMATCH")
        offset_ms = alignment.get("offset_ms")
        if not is_int(offset_ms) or report.get("offset_ms") != offset_ms:
            failures.append("SONG_ALIGNMENT_OFFSET_MISMATCH")
        alignment_zero = alignment.get("nominal_lrc_zero_ms")
        report_zero = report.get("nominal_lrc_zero_ms")
        if not all(is_int(value) for value in (boundary_zero, alignment_zero, report_zero, offset_ms)):
            failures.append("SONG_NOMINAL_LRC_ZERO_INVALID")
        elif not (
            boundary_zero == alignment_zero == report_zero == offset_ms == report.get("offset_ms")
        ):
            failures.append("SONG_NOMINAL_LRC_ZERO_MISMATCH")
        if boundary.get("song_title") and report.get("song_title") != boundary.get("song_title"):
            failures.append("SONG_ALIGNMENT_TITLE_MISMATCH")
        if record.get("candidate_id") and report.get("candidate_id") != record.get("candidate_id"):
            failures.append("SONG_ALIGNMENT_CANDIDATE_MISMATCH")
        if boundary and (
            report.get("first_lyric_start_ms") != boundary.get("first_lyric_start_ms")
            or report.get("last_lyric_end_ms") != boundary.get("last_lyric_end_ms")
        ):
            failures.append("SONG_ALIGNMENT_BOUNDARY_MISMATCH")

        lyric_lines = report.get("lyric_lines")
        if not isinstance(lyric_lines, list) or len(lyric_lines) < 8:
            failures.append("SONG_ALIGNMENT_LYRIC_TIMELINE_INVALID")
            lyric_lines = []
        else:
            lyric_times = [line.get("lrc_time_ms") for line in lyric_lines if isinstance(line, dict)]
            lyric_texts_ok = all(isinstance(line, dict) and str(line.get("text") or "").strip() for line in lyric_lines)
            if (
                len(lyric_times) != len(lyric_lines)
                or not all(is_int(value) and value >= 0 for value in lyric_times)
                or lyric_times != sorted(lyric_times)
                or not lyric_texts_ok
            ):
                failures.append("SONG_ALIGNMENT_LYRIC_TIMELINE_INVALID")
        line_count = report.get("line_count")
        matched_count = report.get("matched_line_count")
        matched_ratio = report.get("matched_line_ratio")
        if (
            not is_int(line_count)
            or line_count != len(lyric_lines)
            or not is_int(matched_count)
            or matched_count < 0
            or matched_count > line_count
            or not isinstance(matched_ratio, (int, float))
            or isinstance(matched_ratio, bool)
            or float(matched_ratio) < 0.55
            or (line_count and matched_count / line_count < 0.55)
            or (line_count and float(matched_ratio) != round(matched_count / line_count, 4))
            or alignment.get("matched_line_ratio") != matched_ratio
        ):
            failures.append("SONG_ALIGNMENT_MATCH_EVIDENCE_INVALID")
        report_alignment = report.get("alignment")
        if not isinstance(report_alignment, list) or len(report_alignment) != line_count:
            failures.append("SONG_ALIGNMENT_MATCH_EVIDENCE_INVALID")
        elif sum(1 for entry in report_alignment if isinstance(entry, dict) and entry.get("matched_cue_id") is not None) != matched_count:
            failures.append("SONG_ALIGNMENT_MATCH_EVIDENCE_INVALID")

        is_audio_report = report.get("evidence_source") == "agy_audio_lrc"
        is_audio_model = str(alignment.get("model") or "").endswith("-agy-audio-lrc-global-shift-v1")
        if is_audio_report != is_audio_model:
            failures.append("SONG_ALIGNMENT_EVIDENCE_TYPE_MISMATCH")

        if not is_audio_report:
            failures.append("SONG_LIVE_PERFORMANCE_UNPROVEN")

        if is_audio_report:
            live_performance = report.get("live_performance")
            performance_error = validate_live_performance_observation(
                live_performance,
                first_lyric_start_ms=report.get("first_lyric_start_ms") if is_int(report.get("first_lyric_start_ms")) else -1,
                last_lyric_end_ms=report.get("last_lyric_end_ms") if is_int(report.get("last_lyric_end_ms")) else -1,
                observations=report_alignment,
                require_ready=True,
            )
            if performance_error is not None:
                failures.extend(live_performance_failure_reason_codes(live_performance))
            else:
                live_performance_semantic_ready = True
            report_provider = report.get("audio_alignment_provider")
            report_execution_error = validate_audio_lrc_execution_metadata(
                provider=report_provider,
                model=report.get("audio_alignment_model"),
                agy_rc=report.get("audio_alignment_agy_rc", 0 if report_provider == "agy" else None),
                provider_fallback_used=report.get(
                    "audio_alignment_provider_fallback_used",
                    False if report_provider == "agy" else None,
                ),
                agy_failure_category=report.get("audio_alignment_agy_failure_category"),
                sandbox=True if report_provider == "agy" else False,
            )
            if (
                report_execution_error is not None
                or not str(alignment.get("model") or "").endswith("-agy-audio-lrc-global-shift-v1")
            ):
                failures.append("SONG_AUDIO_LRC_PROVIDER_INVALID")
            audio_artifacts = report.get("audio_alignment_artifacts")
            raw_rows: list | None = None
            artifact_pairs = (
                ("source_path", "source_sha256"),
                ("lrc_path", "lrc_sha256"),
                ("prompt_path", "prompt_sha256"),
                ("raw_output_path", "raw_output_sha256"),
                ("run_manifest_path", "run_manifest_sha256"),
            )
            if not isinstance(audio_artifacts, dict):
                failures.append("SONG_AUDIO_LRC_ARTIFACTS_INVALID")
            else:
                for path_key, sha_key in artifact_pairs:
                    path_value = audio_artifacts.get(path_key)
                    sha_value = audio_artifacts.get(sha_key)
                    if (
                        not isinstance(path_value, str)
                        or not isinstance(sha_value, str)
                        or not _matches_sha256(Path(path_value), sha_value)
                    ):
                        failures.append("SONG_AUDIO_LRC_ARTIFACTS_INVALID")
                manifest_value = audio_artifacts.get("run_manifest_path")
                try:
                    audio_manifest = json.loads(Path(str(manifest_value)).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    audio_manifest = None
                manifest_execution_error = (
                    validate_audio_lrc_execution_metadata(
                        provider=audio_manifest.get("provider"),
                        model=audio_manifest.get("model"),
                        agy_rc=audio_manifest.get("agy_rc"),
                        provider_fallback_used=audio_manifest.get("provider_fallback_used"),
                        agy_failure_category=audio_manifest.get("agy_failure_category"),
                        sandbox=audio_manifest.get("sandbox"),
                    )
                    if isinstance(audio_manifest, dict)
                    else "manifest is not a mapping"
                )
                if not isinstance(audio_manifest, dict) or (
                    audio_manifest.get("schema_version") not in {
                        "agy-audio-lrc-run.v1",
                        "agy-audio-lrc-run.v2",
                        "agy-audio-lrc-run.v3",
                    }
                    or audio_manifest.get("candidate_id") != record.get("candidate_id")
                    or (
                        audio_manifest.get("provider") == "gemini_api"
                        and audio_manifest.get("schema_version") != "agy-audio-lrc-run.v3"
                    )
                    or audio_manifest.get("provider") != report_provider
                    or audio_manifest.get("model") != report.get("audio_alignment_model")
                    or audio_manifest.get("agy_rc")
                    != report.get("audio_alignment_agy_rc", 0 if report_provider == "agy" else None)
                    or audio_manifest.get("provider_fallback_used")
                    is not report.get(
                        "audio_alignment_provider_fallback_used",
                        False if report_provider == "agy" else None,
                    )
                    or audio_manifest.get("agy_failure_category")
                    != report.get("audio_alignment_agy_failure_category")
                    or manifest_execution_error is not None
                ):
                    failures.append("SONG_AUDIO_LRC_MANIFEST_INVALID")
                elif not isinstance(audio_manifest.get("artifacts"), dict) or any(
                    audio_manifest["artifacts"].get(manifest_key) != audio_artifacts.get(report_key)
                    for manifest_key, report_key in (
                        ("source_origin_path", "source_origin_path"),
                        ("source_path", "source_path"),
                        ("source_sha256", "source_sha256"),
                        ("source_duration_ms", "source_duration_ms"),
                        ("lrc_path", "lrc_path"),
                        ("lrc_sha256", "lrc_sha256"),
                        ("prompt_path", "prompt_path"),
                        ("prompt_sha256", "prompt_sha256"),
                        ("output_path", "raw_output_path"),
                        ("output_sha256", "raw_output_sha256"),
                    )
                ):
                    failures.append("SONG_AUDIO_LRC_MANIFEST_BINDING_INVALID")
                if isinstance(audio_manifest, dict) and audio_manifest.get("provider") == "gemini_api":
                    manifest_artifacts = audio_manifest.get("artifacts")
                    api_audio_path = audio_artifacts.get("api_audio_path")
                    api_audio_sha = audio_artifacts.get("api_audio_sha256")
                    api_audio_duration_ms = audio_artifacts.get("api_audio_duration_ms")
                    source_duration_for_api = audio_artifacts.get("source_duration_ms")
                    configured_key_count = audio_manifest.get("configured_key_count")
                    accepted_key_ordinal = audio_manifest.get("accepted_key_ordinal")
                    if (
                        audio_manifest.get("direct_audio_input") is not True
                        or not isinstance(api_audio_path, str)
                        or not isinstance(api_audio_sha, str)
                        or not _matches_sha256(Path(api_audio_path), api_audio_sha)
                        or not is_int(api_audio_duration_ms)
                        or not is_int(source_duration_for_api)
                        or abs(api_audio_duration_ms - source_duration_for_api) > 1_000
                        or not isinstance(manifest_artifacts, dict)
                        or manifest_artifacts.get("api_audio_path") != api_audio_path
                        or manifest_artifacts.get("api_audio_sha256") != api_audio_sha
                        or manifest_artifacts.get("api_audio_duration_ms") != api_audio_duration_ms
                        or not is_int(configured_key_count)
                        or not 1 <= configured_key_count <= 3
                        or not is_int(accepted_key_ordinal)
                        or not 1 <= accepted_key_ordinal <= configured_key_count
                    ):
                        failures.append("SONG_AUDIO_LRC_API_AUDIO_BINDING_INVALID")
                raw_value = audio_artifacts.get("raw_output_path")
                try:
                    raw_observation = json.loads(Path(str(raw_value)).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raw_observation = None
                raw_record = raw_observation.get("record") if isinstance(raw_observation, dict) else None
                raw_rows = raw_observation.get("observations") if isinstance(raw_observation, dict) else None
                if (
                    not isinstance(raw_observation, dict)
                    or raw_observation.get("schema_version") != AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION
                    or not isinstance(raw_record, dict)
                    or raw_record.get("candidate_id") != record.get("candidate_id")
                    or raw_record.get("source_sha256") != audio_artifacts.get("source_sha256")
                    or raw_record.get("lrc_sha256") != audio_artifacts.get("lrc_sha256")
                    or raw_record.get("source_duration_ms") != audio_artifacts.get("source_duration_ms")
                    or not isinstance(raw_rows, list)
                ):
                    failures.append("SONG_AUDIO_LRC_RAW_OBSERVATION_INVALID")
                elif raw_observation.get("live_performance") != report.get("live_performance"):
                    failures.append("SONG_LIVE_PERFORMANCE_BINDING_INVALID")
                elif raw_observation.get("live_arrangement") != report.get("live_arrangement_observation"):
                    failures.append("SONG_LIVE_ARRANGEMENT_BINDING_INVALID")
                else:
                    try:
                        derived_arrangement = derive_live_arrangement_completeness(
                            observations=raw_rows,
                            live_arrangement=raw_observation.get("live_arrangement"),
                            post_song_talk_start_ms=raw_observation.get("post_song_talk_start_ms"),
                            source_duration_ms=int(audio_artifacts.get("source_duration_ms")),
                        )
                    except (TypeError, ValueError):
                        failures.append("SONG_LIVE_ARRANGEMENT_INVALID")
                    else:
                        if (
                            report.get("arrangement_completeness") != derived_arrangement
                            or alignment.get("completion_basis") != derived_arrangement.get("classification")
                        ):
                            failures.append("SONG_LIVE_ARRANGEMENT_BINDING_INVALID")
                        canonical_lyrics = report.get("canonical_lyric_lines")
                        if (
                            report.get("canonical_line_count") != len(raw_rows)
                            or not isinstance(canonical_lyrics, list)
                            or len(canonical_lyrics) != len(raw_rows)
                            or any(
                                not isinstance(raw_row, dict)
                                or not isinstance(lyric, dict)
                                or lyric.get("lrc_index") != raw_row.get("lrc_index")
                                or lyric.get("lrc_time_ms") != raw_row.get("lrc_time_ms")
                                or lyric.get("text") != raw_row.get("text")
                                for raw_row, lyric in zip(raw_rows, canonical_lyrics)
                            )
                        ):
                            failures.append("SONG_LIVE_ARRANGEMENT_BINDING_INVALID")
                if isinstance(audio_manifest, dict) and audio_manifest.get("schema_version") in {
                    "agy-audio-lrc-run.v2",
                    "agy-audio-lrc-run.v3",
                }:
                    provider_raw_value = audio_artifacts.get("provider_raw_output_path")
                    provider_raw_sha = audio_artifacts.get("provider_raw_output_sha256")
                    manifest_artifacts = audio_manifest.get("artifacts")
                    if (
                        not isinstance(provider_raw_value, str)
                        or not isinstance(provider_raw_sha, str)
                        or not _matches_sha256(Path(provider_raw_value), provider_raw_sha)
                        or audio_artifacts.get("canonicalized_output_path") != raw_value
                        or audio_artifacts.get("canonicalized_output_sha256")
                        != audio_artifacts.get("raw_output_sha256")
                        or not isinstance(manifest_artifacts, dict)
                        or manifest_artifacts.get("provider_raw_output_path") != provider_raw_value
                        or manifest_artifacts.get("provider_raw_output_sha256") != provider_raw_sha
                        or audio_manifest.get("canonicalization")
                        != {
                            "strategy": "canonical-lrc-by-exact-index.v1",
                            "row_identity": "strict_zero_based_lrc_index",
                            "restored_fields": ["lrc_time_ms", "text"],
                            "row_count": (
                                int(report.get("canonical_line_count"))
                                if is_int(report.get("canonical_line_count"))
                                else -1
                            ),
                            "canonical_lrc_sha256": audio_artifacts.get("lrc_sha256"),
                            "provider_raw_output_sha256": provider_raw_sha,
                            "canonicalized_output_sha256": audio_artifacts.get("canonicalized_output_sha256"),
                        }
                    ):
                        failures.append("SONG_AUDIO_LRC_CANONICALIZATION_INVALID")
                    else:
                        try:
                            provider_raw_observation = load_audio_lrc_json_artifact(
                                Path(provider_raw_value),
                                "provider raw audio alignment",
                            )
                            validate_audio_lrc_canonical_projection(
                                provider_payload=provider_raw_observation,
                                canonical_payload=raw_observation,
                                lrc_path=Path(str(audio_artifacts.get("lrc_path"))),
                            )
                        except ValueError:
                            provider_raw_observation = None
                        if provider_raw_observation is None:
                            failures.append("SONG_AUDIO_LRC_PROVIDER_CANONICAL_MISMATCH")
            if isinstance(report_alignment, list) and isinstance(lyric_lines, list):
                ids: list[str] = []
                starts: list[int] = []
                residuals: list[int] = []
                raw_heard_rows = (
                    [row for row in raw_rows if isinstance(row, dict) and row.get("heard") is True]
                    if isinstance(raw_rows, list)
                    else []
                )
                audio_rows_ok = (
                    isinstance(raw_rows, list)
                    and len(raw_heard_rows) == len(report_alignment) == len(lyric_lines) == matched_count == line_count
                )
                expected_raw_sha = str(
                    audio_artifacts.get("raw_output_sha256") if isinstance(audio_artifacts, dict) else ""
                ).lower().removeprefix("sha256:")
                for index, (row, lyric) in enumerate(zip(report_alignment, lyric_lines)):
                    raw_row = raw_heard_rows[index] if index < len(raw_heard_rows) else None
                    if not isinstance(row, dict) or not isinstance(lyric, dict) or not isinstance(raw_row, dict):
                        audio_rows_ok = False
                        break
                    cue_id = row.get("matched_cue_id")
                    cue_start = row.get("cue_start_ms")
                    cue_end = row.get("cue_end_ms")
                    if (
                        set(raw_row) != {
                            "lrc_index",
                            "lrc_time_ms",
                            "text",
                            "heard",
                            "live_start_ms",
                            "live_end_ms",
                            "confidence",
                            *LYRIC_VOCAL_ASSERTION_KEYS,
                        }
                        or not LYRIC_VOCAL_ASSERTION_KEYS.issubset(row)
                        or not is_int(raw_row.get("lrc_index"))
                        or row.get("canonical_lrc_index") != raw_row.get("lrc_index")
                        or ("lrc_index" in lyric and lyric.get("lrc_index") != raw_row.get("lrc_index"))
                        or raw_row.get("lrc_time_ms") != lyric.get("lrc_time_ms")
                        or raw_row.get("text") != lyric.get("text")
                        or raw_row.get("heard") is not True
                        or raw_row.get("live_start_ms") != cue_start
                        or raw_row.get("live_end_ms") != cue_end
                        or isinstance(raw_row.get("confidence"), bool)
                        or not isinstance(raw_row.get("confidence"), (int, float))
                        or row.get("match_ratio") != round(float(raw_row.get("confidence") or 0.0), 4)
                        or any(row.get(key) != raw_row.get(key) for key in LYRIC_VOCAL_ASSERTION_KEYS)
                        or row.get("evidence_source") != "agy_audio_lrc"
                        or not isinstance(cue_id, str)
                        or cue_id != f"agy-audio:{expected_raw_sha[:12]}:line-{raw_row.get('lrc_index')}"
                        or not is_int(cue_start)
                        or not is_int(cue_end)
                        or not 0 <= cue_start < cue_end
                        or row.get("lrc_time_ms") != lyric.get("lrc_time_ms")
                        or row.get("lrc_text") != lyric.get("text")
                    ):
                        audio_rows_ok = False
                        break
                    ids.append(cue_id)
                    starts.append(cue_start)
                    residuals.append(cue_start - lyric["lrc_time_ms"])
                if (
                    not audio_rows_ok
                    or len(set(ids)) != len(ids)
                    or starts != sorted(starts)
                    or not is_int(offset_ms)
                    or any(abs(value - offset_ms) > 1_500 for value in residuals)
                    or (starts and starts[0] != report.get("first_lyric_start_ms"))
                    or (
                        report_alignment
                        and isinstance(report_alignment[-1], dict)
                        and report_alignment[-1].get("cue_end_ms") != report.get("last_lyric_end_ms")
                    )
                ):
                    failures.append("SONG_AUDIO_LRC_OBSERVATION_INVALID")
                if isinstance(audio_artifacts, dict) and isinstance(raw_rows, list):
                    raw_rows_match = len(raw_heard_rows) == len(report_alignment) == len(lyric_lines)
                    if raw_rows_match:
                        for index, (raw_row, proof_row, lyric) in enumerate(zip(raw_heard_rows, report_alignment, lyric_lines)):
                            if (
                                not isinstance(raw_row, dict)
                                or not isinstance(proof_row, dict)
                                or not isinstance(lyric, dict)
                                or not is_int(raw_row.get("lrc_index"))
                                or proof_row.get("canonical_lrc_index") != raw_row.get("lrc_index")
                                or ("lrc_index" in lyric and lyric.get("lrc_index") != raw_row.get("lrc_index"))
                                or raw_row.get("lrc_time_ms") != lyric.get("lrc_time_ms")
                                or raw_row.get("text") != lyric.get("text")
                                or raw_row.get("heard") is not True
                                or raw_row.get("live_start_ms") != proof_row.get("cue_start_ms")
                                or raw_row.get("live_end_ms") != proof_row.get("cue_end_ms")
                                or not isinstance(raw_row.get("confidence"), (int, float))
                                or isinstance(raw_row.get("confidence"), bool)
                                or round(float(raw_row["confidence"]), 4) != proof_row.get("match_ratio")
                            ):
                                raw_rows_match = False
                                break
                    if not raw_rows_match:
                        failures.append("SONG_AUDIO_LRC_RAW_REPORT_MISMATCH")

            # A schema-valid dict is not a READY proof until every bound AGY
            # artifact, manifest, raw-v5 row, and report projection above has
            # survived validation.  Keep state evidence non-contradictory.
            if live_performance_semantic_ready and not failures and isinstance(live_performance, dict):
                live_performance_status = "READY"
                live_performance_mode = str(live_performance.get("mode"))
                live_performance_confidence = float(live_performance.get("confidence"))

    host_vocal_claim = job.get("host_vocal_proof")
    host_vocal_status = host_vocal_claim.get("status") if isinstance(host_vocal_claim, dict) else None
    host_vocal_decision = host_vocal_claim.get("decision") if isinstance(host_vocal_claim, dict) else None
    host_vocal_proof_path = host_vocal_claim.get("proof_path") if isinstance(host_vocal_claim, dict) else None
    host_vocal_proof_sha = host_vocal_claim.get("proof_sha256") if isinstance(host_vocal_claim, dict) else None
    host_vocal_verified = False
    if not isinstance(host_vocal_claim, dict):
        failures.append("SONG_HOST_VOCAL_UNPROVEN")
    elif not isinstance(host_vocal_proof_path, str) or not isinstance(host_vocal_proof_sha, str):
        reason = host_vocal_claim.get("reason_code")
        failures.append(
            reason
            if reason in {"SONG_HOST_VOCAL_VERIFIER_UNAVAILABLE", "SONG_HOST_VOCAL_UNPROVEN"}
            else "SONG_HOST_VOCAL_UNPROVEN"
        )
    elif not _matches_sha256(Path(host_vocal_proof_path), host_vocal_proof_sha):
        failures.append("SONG_HOST_VOCAL_PROOF_INVALID")
    else:
        try:
            host_proof = json.loads(Path(host_vocal_proof_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            host_proof = None
        source_binding = host_proof.get("source_media") if isinstance(host_proof, dict) else None
        source_value = host_vocal_claim.get("source_media_path")
        if not isinstance(source_value, str) and isinstance(source_binding, dict):
            source_value = source_binding.get("path")
        alignment_value = alignment.get("alignment_report_path") if alignment else None
        if not isinstance(source_value, str) or not isinstance(alignment_value, str):
            failures.append("SONG_HOST_VOCAL_PROOF_INVALID")
        else:
            host_error = verify_host_vocal_proof_claim(
                host_vocal_claim,
                str(record.get("candidate_id") or ""),
                Path(source_value),
                Path(alignment_value),
                host_vocal_profile,
            )
            if host_error is not None:
                failures.append("SONG_HOST_VOCAL_PROOF_INVALID")
            elif host_vocal_status != "READY" or host_vocal_decision != "LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS":
                failures.append(
                    "SONG_NOT_LIDOUSHA_SINGING"
                    if host_vocal_status == "BLOCKED" and host_vocal_decision == "NO_LIDOUSHA_VOCAL_DETECTED"
                    else "SONG_HOST_VOCAL_UNPROVEN"
                )
            else:
                host_vocal_verified = True

    recut = record.get("materialized_recut")
    if not isinstance(recut, dict) or recut.get("status") != "MATERIALIZED":
        recut = {}
        failures.append("SONG_MATERIALIZED_RECUT_MISSING")
    elif recut.get("subtitle_source") != "external_lrc_global_shift":
        failures.append("SONG_EXTERNAL_LRC_SUBTITLE_NOT_MATERIALIZED")
    else:
        recut_reasons = recut.get("reason_codes")
        if not isinstance(recut_reasons, list):
            recut_reasons = []
        if recut.get("accurate_rerender_used") is not True:
            failures.append("SONG_ACCURATE_RERENDER_REQUIRED")
        if "FFMPEG_ACCURATE_RECUT_FAILED" in recut_reasons:
            failures.append("SONG_ACCURATE_RERENDER_FAILED")
        render_qa = recut.get("render_qa")
        render_evidence = render_qa.get("evidence") if isinstance(render_qa, dict) else None
        actual_error = render_evidence.get("actual_cut_error_ms") if isinstance(render_evidence, dict) else None
        threshold = render_evidence.get("threshold_ms") if isinstance(render_evidence, dict) else None
        if (
            not isinstance(render_qa, dict)
            or render_qa.get("pass") is not True
            or not isinstance(actual_error, (int, float))
            or isinstance(actual_error, bool)
            or not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or actual_error > threshold
        ):
            failures.append("SONG_RENDER_QA_FAILED")

    if recut and boundary:
        if recut.get("start_ms") != boundary.get("clip_start_ms") or recut.get("end_ms") != boundary.get("clip_end_ms"):
            failures.append("SONG_RECUT_BOUNDARY_MISMATCH")
        if recut.get("lyric_offset_ms") != alignment.get("offset_ms"):
            failures.append("SONG_RECUT_LYRIC_OFFSET_MISMATCH")

    artifact_hashes = recut.get("artifact_hashes") if recut else None
    subtitle_path = recut.get("subtitle_path") if recut else None
    subtitle_sha = artifact_hashes.get("subtitle_sha256") if isinstance(artifact_hashes, dict) else None
    if not isinstance(subtitle_path, str) or not isinstance(subtitle_sha, str) or not _matches_sha256(Path(subtitle_path), subtitle_sha):
        failures.append("SONG_SUBTITLE_ARTIFACT_HASH_INVALID")

    burned = recut.get("burned_preview") if recut else None
    burned_sha = None
    if not isinstance(burned, dict) or burned.get("status") != "BURNED" or not burned.get("path"):
        failures.append("SONG_BURNED_PREVIEW_MISSING")
    else:
        burned_sha = burned.get("burned_sha256")
        if isinstance(artifact_hashes, dict):
            burned_sha = artifact_hashes.get("burned_video_sha256") or burned_sha
        if not isinstance(burned_sha, str) or not _matches_sha256(Path(str(burned["path"])), burned_sha):
            failures.append("SONG_BURNED_PREVIEW_HASH_INVALID")

    # The proofs above establish what happened in one exact source.  This
    # second edge establishes that the delivered bytes were produced from that
    # same source/candidate/interval with the fixed A/V stream contract.
    recut_manifest: dict | None = None
    manifest_value = recut.get("manifest_path") if recut else None
    manifest_sha = recut.get("manifest_sha256") if recut else None
    if (
        not isinstance(manifest_value, str)
        or not isinstance(manifest_sha, str)
        or not _matches_sha256(Path(manifest_value), manifest_sha)
    ):
        failures.append("SONG_RECUT_MANIFEST_HASH_INVALID")
    else:
        try:
            loaded_manifest = json.loads(Path(manifest_value).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded_manifest = None
        if not isinstance(loaded_manifest, dict):
            failures.append("SONG_RECUT_MANIFEST_CONTENT_INVALID")
        else:
            recut_manifest = loaded_manifest

    output_binding = recut.get("verified_output_binding") if recut else None
    manifest_binding = recut_manifest.get("verified_output_binding") if isinstance(recut_manifest, dict) else None
    if (
        not isinstance(output_binding, dict)
        or output_binding.get("schema_version") != VERIFIED_SONG_OUTPUT_BINDING_SCHEMA_VERSION
        or manifest_binding != output_binding
        or not isinstance(recut_manifest, dict)
        or recut_manifest.get("schema_version") != MATERIALIZED_RECUT_SCHEMA_VERSION
        or recut_manifest.get("status") != "MATERIALIZED"
        or recut_manifest.get("candidate_id") != record.get("candidate_id")
        or recut.get("candidate_id") != record.get("candidate_id")
        or output_binding.get("candidate_id") != record.get("candidate_id")
        or recut_manifest.get("source_video_path") != recut.get("source_video_path")
        or recut_manifest.get("source_binding") != recut.get("source_binding")
        or recut_manifest.get("requested_range") != {
            "start_ms": recut.get("start_ms"),
            "end_ms": recut.get("end_ms"),
            "duration_ms": recut.get("duration_ms"),
        }
        or recut_manifest.get("media_path") != recut.get("media_path")
        or recut_manifest.get("subtitle_path") != recut.get("subtitle_path")
        or recut_manifest.get("subtitle_source") != recut.get("subtitle_source")
        or recut_manifest.get("lyric_offset_ms") != recut.get("lyric_offset_ms")
        or recut_manifest.get("accurate_command") != recut.get("accurate_command")
        or recut_manifest.get("stream_contract") != recut.get("stream_contract")
        or recut_manifest.get("recut_transform") != recut.get("recut_transform")
        or recut_manifest.get("song_output_proof_binding") != recut.get("song_output_proof_binding")
        or not isinstance(recut_manifest.get("artifact_hashes"), dict)
        or not isinstance(recut.get("artifact_hashes"), dict)
        or any(
            recut_manifest["artifact_hashes"].get(key) != recut["artifact_hashes"].get(key)
            for key in ("video_sha256", "subtitle_sha256", "burned_video_sha256", "ass_sha256")
        )
        or recut_manifest.get("burned_preview") != recut.get("burned_preview")
    ):
        failures.append("SONG_RECUT_MANIFEST_CONTENT_INVALID")

    source_binding = output_binding.get("source") if isinstance(output_binding, dict) else None
    source_path = source_binding.get("canonical_path") if isinstance(source_binding, dict) else None
    source_sha = source_binding.get("sha256") if isinstance(source_binding, dict) else None
    source_canonical = _canonical_existing_path(source_path)
    host_source = host_proof.get("source_media") if isinstance(host_proof, dict) else None
    manifest_artifacts = audio_manifest.get("artifacts") if isinstance(audio_manifest, dict) else None
    source_path_claims = [
        recut.get("source_video_path") if recut else None,
        (recut.get("source_binding") or {}).get("canonical_path") if isinstance(recut.get("source_binding") if recut else None, dict) else None,
        recut_manifest.get("source_video_path") if isinstance(recut_manifest, dict) else None,
        (recut_manifest.get("source_binding") or {}).get("canonical_path") if isinstance(recut_manifest.get("source_binding") if isinstance(recut_manifest, dict) else None, dict) else None,
        host_source.get("path") if isinstance(host_source, dict) else None,
        alignment.get("source_media_path") if isinstance(alignment, dict) else None,
        report.get("source_media_path") if isinstance(report, dict) else None,
        audio_artifacts.get("source_origin_path") if isinstance(audio_artifacts, dict) else None,
        manifest_artifacts.get("source_origin_path") if isinstance(manifest_artifacts, dict) else None,
    ]
    source_sha_claims = [
        (recut.get("source_binding") or {}).get("sha256") if isinstance(recut.get("source_binding") if recut else None, dict) else None,
        (recut_manifest.get("source_binding") or {}).get("sha256") if isinstance(recut_manifest.get("source_binding") if isinstance(recut_manifest, dict) else None, dict) else None,
        host_source.get("sha256") if isinstance(host_source, dict) else None,
        alignment.get("source_media_sha256") if isinstance(alignment, dict) else None,
        report.get("source_media_sha256") if isinstance(report, dict) else None,
        audio_artifacts.get("source_sha256") if isinstance(audio_artifacts, dict) else None,
        manifest_artifacts.get("source_sha256") if isinstance(manifest_artifacts, dict) else None,
    ]
    if (
        source_canonical is None
        or _normalized_sha256(source_sha) is None
        or not _matches_sha256(Path(source_canonical), str(source_sha))
        or any(_canonical_existing_path(value) != source_canonical for value in source_path_claims)
        or any(_normalized_sha256(value) != _normalized_sha256(source_sha) for value in source_sha_claims)
    ):
        failures.append("SONG_RECUT_SOURCE_BINDING_INVALID")

    proofs = output_binding.get("proofs") if isinstance(output_binding, dict) else None
    expected_proofs = (
        (
            "lyrics_alignment_report_path", "lyrics_alignment_report_sha256",
            alignment.get("alignment_report_path") if isinstance(alignment, dict) else None,
            alignment.get("alignment_report_sha256") if isinstance(alignment, dict) else None,
        ),
        (
            "host_vocal_proof_path", "host_vocal_proof_sha256",
            host_vocal_proof_path, host_vocal_proof_sha,
        ),
        (
            "agy_run_manifest_path", "agy_run_manifest_sha256",
            audio_artifacts.get("run_manifest_path") if isinstance(audio_artifacts, dict) else None,
            audio_artifacts.get("run_manifest_sha256") if isinstance(audio_artifacts, dict) else None,
        ),
    )
    proof_binding_ok = isinstance(proofs, dict)
    if proof_binding_ok:
        for path_key, sha_key, expected_path, expected_sha in expected_proofs:
            if (
                _canonical_existing_path(proofs.get(path_key)) != _canonical_existing_path(expected_path)
                or _normalized_sha256(proofs.get(sha_key)) != _normalized_sha256(expected_sha)
            ):
                proof_binding_ok = False
                break
    if (
        not proof_binding_ok
        or recut.get("song_output_proof_binding") != proofs
        or (recut_manifest.get("song_output_proof_binding") if isinstance(recut_manifest, dict) else None) != proofs
    ):
        failures.append("SONG_RECUT_PROOF_BINDING_INVALID")

    start_ms = recut.get("start_ms") if recut else None
    end_ms = recut.get("end_ms") if recut else None
    duration_ms = recut.get("duration_ms") if recut else None
    expected_interval = {"start_ms": start_ms, "end_ms": end_ms, "duration_ms": duration_ms}
    report_post_anchor = report.get("post_song_talk_start_ms") if isinstance(report, dict) else None
    host_anchor = host_proof.get("session_host_anchor") if isinstance(host_proof, dict) else None
    host_anchor_start = host_anchor.get("start_ms") if isinstance(host_anchor, dict) else None
    if (
        not all(is_int(value) for value in (start_ms, end_ms, duration_ms))
        or duration_ms != end_ms - start_ms
        or (output_binding.get("interval") if isinstance(output_binding, dict) else None) != expected_interval
        or (recut_manifest.get("requested_range") if isinstance(recut_manifest, dict) else None) != expected_interval
        or not is_int(report_post_anchor)
        or not is_int(host_anchor_start)
        or report_post_anchor != host_anchor_start
        or (output_binding.get("post_song_anchor_start_ms") if isinstance(output_binding, dict) else None) != report_post_anchor
        or (proofs.get("post_song_anchor_start_ms") if isinstance(proofs, dict) else None) != report_post_anchor
    ):
        failures.append("SONG_RECUT_INTERVAL_BINDING_INVALID")
    elif end_ms > report_post_anchor:
        failures.append("SONG_OUTPUT_OVERLAPS_POST_SONG_HOST_ANCHOR")

    expected_stream_contract = _expected_song_stream_contract()
    recut_media_path = _canonical_existing_path(recut.get("media_path") if recut else None)
    burned_path = _canonical_existing_path(burned.get("path") if isinstance(burned, dict) else None)
    accurate_command = recut.get("accurate_command") if recut else None
    expected_accurate_command = (
        _expected_song_recut_command(source_canonical, recut_media_path, start_ms, duration_ms)
        if source_canonical is not None
        and recut_media_path is not None
        and all(is_int(value) for value in (start_ms, duration_ms))
        else None
    )
    burn_command = burned.get("command") if isinstance(burned, dict) else None
    transform = output_binding.get("recut_transform") if isinstance(output_binding, dict) else None
    burn_transform = output_binding.get("burn_transform") if isinstance(output_binding, dict) else None
    if (
        recut.get("stream_contract") != expected_stream_contract
        or (recut_manifest.get("stream_contract") if isinstance(recut_manifest, dict) else None) != expected_stream_contract
        or (output_binding.get("stream_contract") if isinstance(output_binding, dict) else None) != expected_stream_contract
        or accurate_command != expected_accurate_command
        or not isinstance(transform, dict)
        or transform.get("schema_version") != "song-recut-transform.v1"
        or transform.get("command") != expected_accurate_command
        or not isinstance(burn_transform, dict)
        or burn_transform.get("schema_version") != "song-subtitle-burn-transform.v1"
        or burn_transform.get("command") != burn_command
        or (burned.get("stream_contract") if isinstance(burned, dict) else None) != expected_stream_contract
        or not _burn_command_obeys_song_stream_contract(
            burn_command,
            input_path=recut_media_path or "",
            output_path=burned_path or "",
        )
        or recut_media_path is None
        or burned_path is None
        or not has_exact_av_streams(Path(recut_media_path))
        or not has_exact_av_streams(Path(burned_path))
    ):
        failures.append("SONG_RECUT_STREAM_CONTRACT_INVALID")

    binding_artifacts = output_binding.get("artifacts") if isinstance(output_binding, dict) else None
    ass_path = burned.get("ass_path") if isinstance(burned, dict) else None
    ass_sha = artifact_hashes.get("ass_sha256") if isinstance(artifact_hashes, dict) else None
    video_sha = artifact_hashes.get("video_sha256") if isinstance(artifact_hashes, dict) else None
    relevant_manifest_hashes = recut_manifest.get("artifact_hashes") if isinstance(recut_manifest, dict) else None
    expected_artifacts = {
        "recut_media_path": recut_media_path,
        "recut_media_sha256": video_sha,
        "subtitle_path": _canonical_existing_path(subtitle_path),
        "subtitle_sha256": subtitle_sha,
        "burned_media_path": burned_path,
        "burned_media_sha256": burned_sha,
        "ass_path": _canonical_existing_path(ass_path),
        "ass_sha256": ass_sha,
    }
    if (
        binding_artifacts != expected_artifacts
        or not isinstance(video_sha, str)
        or recut_media_path is None
        or not _matches_sha256(Path(recut_media_path), video_sha)
        or not isinstance(ass_sha, str)
        or expected_artifacts["ass_path"] is None
        or not _matches_sha256(Path(str(expected_artifacts["ass_path"])), ass_sha)
        or not isinstance(relevant_manifest_hashes, dict)
        or any(relevant_manifest_hashes.get(key) != artifact_hashes.get(key) for key in (
            "video_sha256", "subtitle_sha256", "burned_video_sha256", "ass_sha256"
        ))
        or (recut_manifest.get("burned_preview") if isinstance(recut_manifest, dict) else None) != burned
    ):
        failures.append("SONG_RECUT_ARTIFACT_BINDING_INVALID")

    joint_singing_decision = (
        "VERIFIED_LIDOUSHA_SINGING"
        if live_performance_status == "READY"
        and live_performance_mode == "LIVE_STREAMER_SINGING"
        and host_vocal_verified
        else None
    )
    failures = list(dict.fromkeys(failures))
    return {
        "ready": not failures,
        "reason_codes": failures,
        "song_boundary_status": boundary.get("status") if isinstance(boundary, dict) else None,
        "lyrics_alignment_status": alignment.get("status") if alignment else None,
        "host_vocal_status": host_vocal_status,
        "host_vocal_decision": host_vocal_decision,
        "host_vocal_proof_path": host_vocal_proof_path,
        "host_vocal_proof_sha256": host_vocal_proof_sha,
        "live_performance_status": live_performance_status,
        "live_performance_mode": live_performance_mode,
        "live_performance_confidence": live_performance_confidence,
        "joint_singing_decision": joint_singing_decision,
        "lyrics_provider": alignment.get("provider") if alignment else None,
        "external_lrc": (alignment.get("external_lrc") or alignment.get("source")) if alignment else None,
        "alignment_report_path": alignment.get("alignment_report_path") if alignment else None,
        "alignment_report_sha256": alignment.get("alignment_report_sha256") if alignment else None,
        "subtitle_source": recut.get("subtitle_source") if recut else None,
        "burned_preview_path": burned.get("path") if isinstance(burned, dict) else None,
        "burned_preview_sha256": burned_sha,
        "recut_manifest_path": manifest_value if isinstance(manifest_value, str) else None,
        "recut_manifest_sha256": manifest_sha if isinstance(manifest_sha, str) else None,
        "recut_source_path": source_canonical,
        "recut_source_sha256": source_sha if isinstance(source_sha, str) else None,
        "matched_line_ratio": report.get("matched_line_ratio") if report is not None else None,
        "lyric_offset_ms": alignment.get("offset_ms") if alignment else None,
    }


def verified_song_fallback_title(song_title: str | None, hook: str | None) -> str | None:
    """Build a hook-bearing fallback when semantic publish staging was advisory-blocked."""
    song_title = str(song_title or "").strip()
    if not song_title:
        return None
    hook = str(hook or "").strip()
    hook = re.split(r"[，。！？；]", hook, maxsplit=1)[0].strip()
    # Recall hooks often repeat a slightly different ASR spelling of the song
    # inside 《》.  Truncating that text at 16 characters produced broken titles
    # such as "《和你迎着台风去看".  The LRC-verified canonical title already
    # owns the name; keep only the hook phrase before a repeated quote.
    hook = re.split(r"[《「『]", hook, maxsplit=1)[0].rstrip("：:｜|、 ")
    if hook and hook != "确定性歌检测补充(演唱段)":
        return f"【李豆沙】豆沙歌，《{song_title}》｜{hook[:16]}"
    return f"【李豆沙】豆沙歌，直播间唱《{song_title}》"
