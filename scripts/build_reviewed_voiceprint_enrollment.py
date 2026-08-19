#!/usr/bin/env python3
"""Extract a hash-bound reviewed HOST enrollment bank for shadow experiments.

The builder never edits the production voiceprint profile.  It accepts only
full-cue, single-speaker HOST rows from an 维护者-reviewed speaker override;
mixed rows are excluded even when they contain a HOST subsegment because the
historical split timestamps may be approximate.  The result is development
evidence, not release or deployment authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.apply_subtitle_text_overrides import parse_srt
from src.autoslice import host_occupancy as ho
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.subtitle_validation import validate_srt_text


SCHEMA_VERSION = "reviewed-voiceprint-enrollment-shadow.v1"
PURPOSE = "DEVELOPMENT_SHADOW_ONLY_NOT_PRODUCTION_PROFILE_AUTHORITY"
SELECTION_POLICY = "FULL_CUE_SINGLE_SPEAKER_REVIEWED_HOST_ONLY"
DEFAULT_MINIMUM_CLIP_MS = 700
MINIMUM_DEVELOPMENT_CLIPS = 3
RECOMMENDED_CLIP_RANGE = (6, 12)
RECOMMENDED_TOTAL_DURATION_MS = (60_000, 120_000)
PCM_DURATION_ROUNDING_TOLERANCE_MS = 1

_PROFILE = load_channel_profile(REPO_ROOT)
_HOST_NAMES = frozenset({_PROFILE.host_speaker_label, _PROFILE.display_name})


class ReviewedEnrollmentError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _normalized_sha256(value: object, *, label: str) -> str:
    normalized = str(value or "").lower().removeprefix("sha256:")
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ReviewedEnrollmentError(f"{label} sha256 is malformed")
    return "sha256:" + normalized


def _clock_ms(value: object) -> int:
    text = str(value or "")
    try:
        hours, minutes, remainder = text.split(":")
        seconds, milliseconds = remainder.replace(".", ",").split(",")
        result = (
            int(hours) * 3_600_000
            + int(minutes) * 60_000
            + int(seconds) * 1_000
            + int(milliseconds)
        )
    except (TypeError, ValueError) as exc:
        raise ReviewedEnrollmentError(f"invalid reviewed segment timestamp: {text!r}") from exc
    if result < 0:
        raise ReviewedEnrollmentError("reviewed segment timestamp is negative")
    return result


def _duration_ms(path: Path) -> int:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        raise ReviewedEnrollmentError(f"ffprobe failed for {path}: {completed.stderr[-300:]}")
    try:
        duration = int(float(completed.stdout.strip()) * 1000)
    except ValueError as exc:
        raise ReviewedEnrollmentError(f"invalid ffprobe duration for {path}") from exc
    if duration <= 0:
        raise ReviewedEnrollmentError(f"media has no positive duration: {path}")
    return duration


def _select_reviewed_host_rows(
    payload: Mapping[str, object], *, minimum_clip_ms: int
) -> tuple[list[dict[str, object]], dict[str, int]]:
    rows = payload.get("overrides")
    if not isinstance(rows, list) or not rows:
        raise ReviewedEnrollmentError("speaker override has no reviewed rows")
    selected: list[dict[str, object]] = []
    excluded = {
        "mixed_or_multisegment": 0,
        "single_non_host": 0,
        "below_minimum_duration": 0,
    }
    seen_cues: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReviewedEnrollmentError("speaker override row is malformed")
        source_cue = row.get("source_cue")
        if (
            isinstance(source_cue, bool)
            or not isinstance(source_cue, int)
            or source_cue <= 0
            or source_cue in seen_cues
        ):
            raise ReviewedEnrollmentError("reviewed source cue is invalid or duplicated")
        seen_cues.add(source_cue)
        authority = str(row.get("authority") or "").strip()
        if not authority:
            raise ReviewedEnrollmentError("reviewed row has no explicit authority")
        expect = row.get("expect")
        segments = row.get("segments")
        if not isinstance(expect, Mapping) or not isinstance(segments, list) or not segments:
            raise ReviewedEnrollmentError("reviewed row lacks cue binding or segments")
        expected_start_ms = _clock_ms(expect.get("start"))
        expected_end_ms = _clock_ms(expect.get("end"))
        expected_text = expect.get("text")
        if (
            expected_end_ms <= expected_start_ms
            or not isinstance(expected_text, str)
            or not expected_text.strip()
        ):
            raise ReviewedEnrollmentError("reviewed cue binding is malformed")
        if len(segments) != 1:
            excluded["mixed_or_multisegment"] += 1
            continue
        segment = segments[0]
        if not isinstance(segment, Mapping):
            raise ReviewedEnrollmentError("reviewed speaker segment is malformed")
        speaker = str(segment.get("speaker") or "").strip()
        if speaker not in _HOST_NAMES:
            excluded["single_non_host"] += 1
            continue
        start_ms = _clock_ms(segment.get("start"))
        end_ms = _clock_ms(segment.get("end"))
        if start_ms != expected_start_ms or end_ms != expected_end_ms:
            raise ReviewedEnrollmentError(
                "single-speaker enrollment segment must match its full cue binding"
            )
        duration_ms = end_ms - start_ms
        if duration_ms < minimum_clip_ms:
            excluded["below_minimum_duration"] += 1
            continue
        reviewed_text = segment.get("text")
        if not isinstance(reviewed_text, str) or not reviewed_text.strip():
            raise ReviewedEnrollmentError("reviewed HOST segment text is missing")
        selected.append(
            {
                "source_cue": source_cue,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": duration_ms,
                "authority": authority,
                "speaker": speaker,
                # The pristine worksheet binding and 维护者's corrected text may
                # differ (cue 5 in the 7/22 truth is a known example).  Text is
                # not an acoustic-boundary gate, so bind both without changing
                # the delivery baseline validator.
                "binding_text_sha256": "sha256:"
                + hashlib.sha256(expected_text.encode("utf-8")).hexdigest(),
                "reviewed_text_sha256": "sha256:"
                + hashlib.sha256(reviewed_text.encode("utf-8")).hexdigest(),
            }
        )
    selected.sort(key=lambda item: (int(item["start_ms"]), int(item["source_cue"])))
    return selected, excluded


def _energy_activity_ratio(samples: Sequence[int]) -> float:
    frame_length = ho.VAD_FRAME_MS * ho.SAMPLE_RATE_HZ // 1000
    flags = ho.speech_frame_flags(ho.frame_rms(samples, frame_length=frame_length))
    return sum(1 for flag in flags if flag) / len(flags) if flags else 0.0


def _validate_reviewed_grid(payload: Mapping[str, object], *, text_final_srt_path: Path) -> int:
    cues = parse_srt(text_final_srt_path)
    by_source_cue = {cue.source_index: cue for cue in cues}
    if len(by_source_cue) != len(cues):
        raise ReviewedEnrollmentError("text-final SRT cue identities are duplicated")
    rows = payload.get("overrides")
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, Mapping)
        source_cue = row.get("source_cue")
        expect = row.get("expect")
        assert isinstance(source_cue, int) and not isinstance(source_cue, bool)
        assert isinstance(expect, Mapping)
        cue = by_source_cue.get(source_cue)
        if cue is None:
            raise ReviewedEnrollmentError(f"text-final SRT misses reviewed source cue {source_cue}")
        if _clock_ms(cue.start) != _clock_ms(expect.get("start")) or _clock_ms(
            cue.end
        ) != _clock_ms(expect.get("end")):
            raise ReviewedEnrollmentError(
                f"text-final SRT grid drifted at reviewed source cue {source_cue}"
            )
    return len(cues)


def build_reviewed_enrollment(
    *,
    source_session_id: str,
    media_path: Path,
    text_final_srt_path: Path,
    override_path: Path,
    work_dir: Path,
    minimum_clip_ms: int = DEFAULT_MINIMUM_CLIP_MS,
) -> dict[str, object]:
    """Build deterministic reviewed clips without touching a production profile."""

    source_session_id = source_session_id.strip()
    if not source_session_id:
        raise ReviewedEnrollmentError("source session id is required")
    if minimum_clip_ms < 300 or minimum_clip_ms > 4_000:
        raise ReviewedEnrollmentError("minimum clip duration must be 300--4000 ms")
    if not media_path.is_file() or not text_final_srt_path.is_file() or not override_path.is_file():
        raise ReviewedEnrollmentError("source media, text-final SRT, and override must be files")
    media_path = media_path.resolve()
    text_final_srt_path = text_final_srt_path.resolve()
    override_path = override_path.resolve()
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    media_stat = media_path.stat()
    text_final_srt_stat = text_final_srt_path.stat()
    override_stat = override_path.stat()
    media_sha256 = _sha256(media_path)
    text_final_srt_sha256 = _sha256(text_final_srt_path)
    override_sha256 = _sha256(override_path)
    payload = json.loads(override_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ReviewedEnrollmentError("speaker override root must be an object")
    if payload.get("schema_version") != 1:
        raise ReviewedEnrollmentError("speaker override schema is unsupported")
    candidate_id = str(payload.get("candidate_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    if not candidate_id or "reviewer_reviewed" not in status:
        raise ReviewedEnrollmentError("override is not an 维护者-reviewed candidate truth")
    expected_media_sha256 = _normalized_sha256(
        payload.get("source_media_sha256"), label="override source media"
    )
    if media_sha256 != expected_media_sha256:
        raise ReviewedEnrollmentError("source media does not match reviewed override")
    expected_text_final_srt_sha256 = _normalized_sha256(
        payload.get("text_final_srt_sha256"), label="override text-final SRT"
    )
    if text_final_srt_sha256 != expected_text_final_srt_sha256:
        raise ReviewedEnrollmentError("text-final SRT does not match reviewed override")

    selected, excluded = _select_reviewed_host_rows(payload, minimum_clip_ms=minimum_clip_ms)
    if len(selected) < MINIMUM_DEVELOPMENT_CLIPS:
        raise ReviewedEnrollmentError("too few clear reviewed HOST clips for a development bank")

    container_duration_ms = _duration_ms(media_path)
    pcm_path = ho.extract_span_wav(
        media_path,
        start_ms=0,
        end_ms=container_duration_ms,
        output_path=work_dir / "source-16k-mono.wav",
    )
    samples = ho.read_wav_samples(pcm_path)
    decoded_duration_ms = len(samples) * 1000 // ho.SAMPLE_RATE_HZ
    if decoded_duration_ms <= 0:
        raise ReviewedEnrollmentError("decoded source PCM is empty")
    srt_verdict = validate_srt_text(
        text_final_srt_path.read_text(encoding="utf-8"),
        media_duration_ms=decoded_duration_ms,
        min_cue_ms=1,
        media_tail_tolerance_ms=PCM_DURATION_ROUNDING_TOLERANCE_MS,
    )
    if srt_verdict["status"] != "PASS":
        codes = [str(row.get("code")) for row in srt_verdict["errors"]]
        raise ReviewedEnrollmentError(
            "text-final SRT failed strict validation: " + ",".join(codes[:8])
        )
    text_final_cue_count = _validate_reviewed_grid(payload, text_final_srt_path=text_final_srt_path)

    clips: list[dict[str, object]] = []
    for row in selected:
        start_ms = int(row["start_ms"])
        end_ms = int(row["end_ms"])
        if end_ms > decoded_duration_ms:
            raise ReviewedEnrollmentError("reviewed HOST clip exceeds decoded media")
        clip_samples = ho._slice(
            samples,
            span_start_ms=0,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if not clip_samples:
            raise ReviewedEnrollmentError("reviewed HOST clip decoded to no samples")
        source_cue = int(row["source_cue"])
        clip_path = ho.write_window_wav(
            clip_samples,
            work_dir / "clips" / f"{candidate_id}-cue-{source_cue:04d}.wav",
        )
        ratio = _energy_activity_ratio(clip_samples)
        if not math.isfinite(ratio):
            raise ReviewedEnrollmentError("clip energy activity ratio is not finite")
        clips.append(
            {
                "id": f"{source_session_id}:{candidate_id}:cue-{source_cue}",
                "source_cue": source_cue,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": len(clip_samples) * 1000 // ho.SAMPLE_RATE_HZ,
                "relative_path": str(clip_path.relative_to(work_dir)),
                "sha256": _sha256(clip_path),
                "audio_format": "PCM_S16LE_MONO_16000HZ",
                "energy_activity_ratio": round(ratio, 6),
                "energy_activity_is_not_speech_or_bgm_classification": True,
                "authority": row["authority"],
                "speaker": row["speaker"],
                "binding_text_sha256": row["binding_text_sha256"],
                "reviewed_text_sha256": row["reviewed_text_sha256"],
                "text_changed": (row["binding_text_sha256"] != row["reviewed_text_sha256"]),
            }
        )

    current_media_stat = media_path.stat()
    current_text_final_srt_stat = text_final_srt_path.stat()
    current_override_stat = override_path.stat()
    if (current_media_stat.st_size, current_media_stat.st_mtime_ns) != (
        media_stat.st_size,
        media_stat.st_mtime_ns,
    ) or _sha256(media_path) != media_sha256:
        raise ReviewedEnrollmentError("source media drifted during extraction")
    if (
        current_text_final_srt_stat.st_size,
        current_text_final_srt_stat.st_mtime_ns,
    ) != (text_final_srt_stat.st_size, text_final_srt_stat.st_mtime_ns) or _sha256(
        text_final_srt_path
    ) != text_final_srt_sha256:
        raise ReviewedEnrollmentError("text-final SRT drifted during extraction")
    if (current_override_stat.st_size, current_override_stat.st_mtime_ns) != (
        override_stat.st_size,
        override_stat.st_mtime_ns,
    ) or _sha256(override_path) != override_sha256:
        raise ReviewedEnrollmentError("reviewed override drifted during extraction")

    total_duration_ms = sum(int(clip["duration_ms"]) for clip in clips)
    blockers: list[str] = []
    if len(clips) < RECOMMENDED_CLIP_RANGE[0]:
        blockers.append("RECOMMENDED_CLIP_COUNT_NOT_MET")
    if total_duration_ms < RECOMMENDED_TOTAL_DURATION_MS[0]:
        blockers.append("RECOMMENDED_TOTAL_DURATION_NOT_MET")
    blockers.extend(
        [
            "LOCKED_CROSS_SESSION_HOLDOUTS_NOT_EVALUATED",
            "PRODUCTION_PROFILE_UPDATE_NOT_AUTHORIZED",
        ]
    )
    deterministic_payload = {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "source_session_id": source_session_id,
        "candidate_id": candidate_id,
        "bindings": {
            "enrollment_root_path": str(work_dir),
            "source_media_path": str(media_path),
            "source_media_sha256": media_sha256,
            "source_media_size_bytes": media_stat.st_size,
            "container_duration_ms": container_duration_ms,
            "decoded_pcm_sha256": _sha256(pcm_path),
            "decoded_pcm_duration_ms": decoded_duration_ms,
            "text_final_srt_path": str(text_final_srt_path),
            "text_final_srt_sha256": text_final_srt_sha256,
            "text_final_srt_cue_count": text_final_cue_count,
            "pcm_duration_rounding_tolerance_ms": (PCM_DURATION_ROUNDING_TOLERANCE_MS),
            "reviewed_override_path": str(override_path),
            "reviewed_override_sha256": override_sha256,
            "builder_sha256": _sha256(Path(__file__).resolve()),
        },
        "selection": {
            "policy": SELECTION_POLICY,
            "minimum_clip_ms": minimum_clip_ms,
            "reviewed_override_rows": len(payload["overrides"]),
            "selected_clip_count": len(clips),
            "excluded_rows": excluded,
        },
        "clips": clips,
        "summary": {
            "prototype_count": len(clips),
            "total_duration_ms": total_duration_ms,
            "recommended_clip_range": list(RECOMMENDED_CLIP_RANGE),
            "recommended_total_duration_ms": list(RECOMMENDED_TOTAL_DURATION_MS),
            "development_shadow_ready": True,
            "production_enrollment_ready": False,
            "promotion_blockers": blockers,
        },
    }
    return {
        **deterministic_payload,
        "deterministic_payload_sha256": _canonical_sha256(deterministic_payload),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-session-id", required=True)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--text-final-srt", type=Path, required=True)
    parser.add_argument("--override", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-clip-ms", type=int, default=DEFAULT_MINIMUM_CLIP_MS)
    args = parser.parse_args(argv)
    result = build_reviewed_enrollment(
        source_session_id=args.source_session_id,
        media_path=args.media,
        text_final_srt_path=args.text_final_srt,
        override_path=args.override,
        work_dir=args.work_dir,
        minimum_clip_ms=args.minimum_clip_ms,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
