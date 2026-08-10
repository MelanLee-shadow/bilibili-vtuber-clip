"""Hash-bound proof that a song candidate contains the selected host's live vocal.

The lyric/LRC alignment gate proves *which recording is audible*.  It does not
prove that the configured host is the person singing it: a background track aligns to the
same LRC just as well.  This module adds an independent, fail-closed CAM++
speaker-verification gate over seven lyric-spanning checkpoints.

Importing the module intentionally needs only the Python standard library.
``modelscope`` (and its transitive numerical dependencies) is imported lazily
by the CLI after all hash bindings have been checked.  This lets the ordinary
autoslice process verify proof claims without loading an ML runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.autoslice.channel_profile import load_channel_profile


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)

PROOF_SCHEMA_VERSION = "host-vocal-proof.v3"
PROFILE_SCHEMA_VERSION = "lidousha-voiceprint-profile.v1"
GENERIC_PROFILE_SCHEMA_VERSION = "host-voiceprint-profile.v1"
SUPPORTED_PROFILE_SCHEMA_VERSIONS = {
    PROFILE_SCHEMA_VERSION,
    GENERIC_PROFILE_SCHEMA_VERSION,
}

CHECKPOINT_FRACTIONS = (0.08, 0.22, 0.36, 0.50, 0.64, 0.78, 0.92)
CHECKPOINT_BUCKETS = ("head", "head", "middle", "middle", "middle", "tail", "tail")
CHECKPOINT_WINDOW_MS = 4_000
MIN_LYRIC_CUE_MS = 2_500
SESSION_HOST_ANCHOR_WINDOW_MS = 8_000
SESSION_HOST_ANCHOR_SEARCH_STEP_MS = 4_000
SESSION_HOST_ANCHOR_SEARCH_SCOPE = "post_song_speech_to_source_end_excluding_lyric_span"
MIN_SESSION_HOST_ANCHOR_MS = 3_000
MIN_SESSION_ENROLL_MEDIAN = 0.50
MIN_SESSION_LYRIC_SCORE = 0.22
# The pinned CAM++ model's own ``yesOrno_thr`` is 0.31.  Speech enrollment and
# singing occupy different acoustic domains, so a checkpoint may establish
# identity either directly against the enrollment set or through a verified
# same-session host-speech anchor.  Requiring five lyric-spanning positives
# (plus head/middle/tail coverage), together with the canonical AGY assertions
# above, still fails closed on background/playback vocals.
MIN_CHECKPOINT_MEDIAN = 0.31
MIN_PASSED_CHECKPOINTS = 5
REQUIRED_BUCKETS = ("head", "middle", "tail")
EXPECTED_REFERENCE_COUNT = 3
CHECKPOINT_REQUIRED_LYRIC_ROLE = "SINGING_THIS_LYRIC"
READY_SINGING_ASSERTIONS = {
    "lyric_vocal_subject": CHANNEL_PROFILE.decision("lyric_vocal_subject"),
    "lidousha_role": CHECKPOINT_REQUIRED_LYRIC_ROLE,
    "same_live_vocal_source_as_lidousha": True,
    "other_singer_or_harmony_audible": False,
    "recorded_or_playback_vocal_audible": False,
}
READY_SPOKEN_ASSERTIONS = {
    **READY_SINGING_ASSERTIONS,
    "lidousha_role": "PERFORMING_THIS_LYRIC_SPOKEN",
}

READY_STATUS = "READY"
BLOCKED_STATUS = "BLOCKED"
READY_DECISION = CHANNEL_PROFILE.decision("host_vocal_present")
BLOCKED_DECISION = CHANNEL_PROFILE.decision("host_vocal_absent")


class HostVocalProofError(ValueError):
    """Raised when an input or generated proof violates a trust invariant."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_directory(path: Path) -> str:
    """Hash a model tree by relative path and file content.

    The tree digest is independent of absolute deployment location.  Symlinked
    files are hashed by their resolved content; broken links and empty model
    directories are rejected.
    """

    root = path.resolve(strict=True)
    if not root.is_dir():
        raise HostVocalProofError(f"model path is not a directory: {path}")
    files = sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix())
    if not files:
        raise HostVocalProofError(f"model directory contains no files: {path}")
    digest = hashlib.sha256()
    for item in files:
        relative = item.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        file_digest = bytes.fromhex(_sha256_file(item))
        digest.update(len(file_digest).to_bytes(8, "big"))
        digest.update(file_digest)
    return digest.hexdigest()


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostVocalProofError(f"cannot read {label} JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise HostVocalProofError(f"{label} must be a JSON object")
    return payload


def _normalize_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise HostVocalProofError(f"{label} sha256 is missing")
    normalized = value.lower().removeprefix("sha256:")
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise HostVocalProofError(f"{label} sha256 is malformed")
    return normalized


def _require_string(mapping: Mapping[str, object], key: str, *, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HostVocalProofError(f"{label}.{key} is missing")
    return value


def _require_int(mapping: Mapping[str, object], key: str, *, label: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostVocalProofError(f"{label}.{key} must be an integer")
    return value


def _require_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostVocalProofError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise HostVocalProofError(f"{label} must be finite")
    return number


def _require_similarity_score(value: object, *, label: str) -> float:
    number = _require_number(value, label=label)
    if not -1.0 <= number <= 1.0:
        raise HostVocalProofError(f"{label} must be within [-1, 1]")
    return number


def _canonical_policy() -> dict[str, object]:
    return {
        "checkpoint_fractions": list(CHECKPOINT_FRACTIONS),
        "checkpoint_buckets": list(CHECKPOINT_BUCKETS),
        "checkpoint_window_ms": CHECKPOINT_WINDOW_MS,
        "minimum_lyric_cue_ms": MIN_LYRIC_CUE_MS,
        "session_host_anchor_window_ms": SESSION_HOST_ANCHOR_WINDOW_MS,
        "session_host_anchor_search_step_ms": SESSION_HOST_ANCHOR_SEARCH_STEP_MS,
        "session_host_anchor_search_scope": SESSION_HOST_ANCHOR_SEARCH_SCOPE,
        "minimum_session_host_anchor_ms": MIN_SESSION_HOST_ANCHOR_MS,
        "minimum_session_enroll_median": MIN_SESSION_ENROLL_MEDIAN,
        "minimum_session_lyric_score": MIN_SESSION_LYRIC_SCORE,
        "minimum_checkpoint_median": MIN_CHECKPOINT_MEDIAN,
        "checkpoint_decision_rule": "direct_enrollment_or_verified_session_bridge",
        "minimum_passed_checkpoints": MIN_PASSED_CHECKPOINTS,
        "required_buckets": list(REQUIRED_BUCKETS),
        "checkpoint_required_lyric_role": CHECKPOINT_REQUIRED_LYRIC_ROLE,
    }


def _legacy_reference_profile_policy() -> dict[str, object]:
    """Keep the enrollment/talk profile hash stable while song proof evolves."""

    policy = _canonical_policy()
    policy.pop("session_host_anchor_search_step_ms")
    policy.pop("session_host_anchor_search_scope")
    policy["minimum_session_lyric_score"] = 0.31
    return policy


def _validate_profile(profile: Mapping[str, object]) -> tuple[dict[str, object], list[dict[str, str]]]:
    if profile.get("schema_version") not in SUPPORTED_PROFILE_SCHEMA_VERSIONS:
        raise HostVocalProofError(
            "reference profile schema_version must be one of "
            + ", ".join(sorted(SUPPORTED_PROFILE_SCHEMA_VERSIONS))
        )
    _require_string(profile, "profile_id", label="reference_profile")
    if _require_string(profile, "subject", label="reference_profile") != CHANNEL_PROFILE.display_name:
        raise HostVocalProofError(
            "reference_profile.subject does not match the selected channel profile"
        )
    policy = profile.get("policy")
    if not isinstance(policy, Mapping) or dict(policy) not in (
        _canonical_policy(),
        _legacy_reference_profile_policy(),
    ):
        raise HostVocalProofError("reference_profile.policy does not match the compiled fail-closed policy")

    model = profile.get("model")
    if not isinstance(model, Mapping):
        raise HostVocalProofError("reference_profile.model is missing")
    model_info = {
        "model_id": _require_string(model, "model_id", label="reference_profile.model"),
        "tree_sha256": _normalize_sha256(model.get("tree_sha256"), label="reference_profile.model.tree"),
    }

    raw_references = profile.get("references")
    if not isinstance(raw_references, list) or len(raw_references) != EXPECTED_REFERENCE_COUNT:
        raise HostVocalProofError(f"reference_profile.references must contain exactly {EXPECTED_REFERENCE_COUNT} enrollments")
    references: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    for index, raw_reference in enumerate(raw_references):
        if not isinstance(raw_reference, Mapping):
            raise HostVocalProofError(f"reference_profile.references[{index}] must be an object")
        reference_id = _require_string(raw_reference, "id", label=f"reference_profile.references[{index}]")
        filename = _require_string(raw_reference, "filename", label=f"reference_profile.references[{index}]")
        file_path = Path(filename)
        if file_path.is_absolute() or ".." in file_path.parts or filename in {".", ""}:
            raise HostVocalProofError(f"reference_profile.references[{index}].filename must be a safe relative path")
        if reference_id in seen_ids or filename in seen_files:
            raise HostVocalProofError("reference_profile references must have unique ids and filenames")
        seen_ids.add(reference_id)
        seen_files.add(filename)
        references.append(
            {
                "id": reference_id,
                "filename": filename,
                "sha256": _normalize_sha256(raw_reference.get("sha256"), label=f"reference_profile.references[{index}]"),
            }
        )
    return model_info, references


def _checkpoint_position(first_ms: int, last_ms: int, fraction: float) -> tuple[int, int, int]:
    center_ms = int(round(first_ms + (last_ms - first_ms) * fraction))
    start_ms = max(0, center_ms - CHECKPOINT_WINDOW_MS // 2)
    return center_ms, start_ms, start_ms + CHECKPOINT_WINDOW_MS


def _selected_lyric_rows(alignment: Mapping[str, object]) -> list[dict[str, object]]:
    """Select seven distinct, actually aligned lyric rows across the song.

    Sampling arbitrary wall-clock fractions can land on an instrumental gap or
    on unrelated speech over a playing record.  The checkpoints must instead
    be centred on concrete lyric observations from the bound alignment report.
    This still is not source separation, but it makes the identity evidence
    coincide with seven specific sung-line intervals rather than merely with
    the overall LRC span.
    """

    raw_alignment = alignment.get("alignment")
    raw_lyrics = alignment.get("lyric_lines")
    if not isinstance(raw_alignment, list):
        raise HostVocalProofError("lyrics alignment report has no alignment rows")
    lyrics = raw_lyrics if isinstance(raw_lyrics, list) else []
    first_lyric_ms = _require_int(alignment, "first_lyric_start_ms", label="lyrics_alignment_report")
    last_lyric_ms = _require_int(alignment, "last_lyric_end_ms", label="lyrics_alignment_report")
    matched: list[dict[str, object]] = []
    previous_start = -1
    seen_cue_ids: set[str] = set()
    for alignment_index, raw_row in enumerate(raw_alignment):
        if not isinstance(raw_row, Mapping) or raw_row.get("matched_cue_id") is None:
            continue
        start_ms = _require_int(raw_row, "cue_start_ms", label=f"alignment[{alignment_index}]")
        end_ms = _require_int(raw_row, "cue_end_ms", label=f"alignment[{alignment_index}]")
        lrc_time_ms = _require_int(raw_row, "lrc_time_ms", label=f"alignment[{alignment_index}]")
        text_value = raw_row.get("lrc_text")
        if not isinstance(text_value, str) or not text_value.strip():
            lyric = lyrics[alignment_index] if alignment_index < len(lyrics) else None
            text_value = lyric.get("text") if isinstance(lyric, Mapping) else None
        if not isinstance(text_value, str) or not text_value.strip():
            raise HostVocalProofError(f"alignment[{alignment_index}] lyric text is missing")
        cue_id = raw_row.get("matched_cue_id")
        if not isinstance(cue_id, str) or not cue_id.strip():
            raise HostVocalProofError(f"alignment[{alignment_index}] matched_cue_id is invalid")
        if (
            start_ms < first_lyric_ms
            or end_ms > last_lyric_ms
            or end_ms <= start_ms
            or start_ms <= previous_start
        ):
            raise HostVocalProofError("matched lyric alignment rows must be positive and monotonic")
        previous_start = start_ms
        if cue_id in seen_cue_ids:
            raise HostVocalProofError("matched lyric alignment rows reuse a cue id")
        seen_cue_ids.add(cue_id)
        assertions = {key: raw_row.get(key) for key in READY_SINGING_ASSERTIONS}
        if assertions == READY_SPOKEN_ASSERTIONS:
            # A short canonical spoken passage can keep the song READY, but a
            # speaker-similarity hit on speech must never count as one of the
            # five required *singing* checkpoints.
            continue
        if assertions != READY_SINGING_ASSERTIONS:
            raise HostVocalProofError(
                f"alignment[{alignment_index}] is not a structurally READY host lyric row"
            )
        if end_ms - start_ms < MIN_LYRIC_CUE_MS:
            continue
        matched.append(
            {
                "alignment_index": alignment_index,
                "matched_cue_id": cue_id,
                "lrc_time_ms": lrc_time_ms,
                "lrc_text": text_value,
                "cue_start_ms": start_ms,
                "cue_end_ms": end_ms,
                **assertions,
            }
        )
    if len(matched) < len(CHECKPOINT_FRACTIONS):
        raise HostVocalProofError(
            f"lyrics alignment report needs at least {len(CHECKPOINT_FRACTIONS)} matched lyric rows for host-vocal proof"
        )
    selected = [matched[min(len(matched) - 1, int(fraction * len(matched)))] for fraction in CHECKPOINT_FRACTIONS]
    if len({int(row["alignment_index"]) for row in selected}) != len(CHECKPOINT_FRACTIONS):
        raise HostVocalProofError("host-vocal lyric checkpoint selection was not distinct")
    for previous, current in zip(selected, selected[1:]):
        if int(previous["cue_end_ms"]) > int(current["cue_start_ms"]):
            raise HostVocalProofError("selected host-vocal lyric checkpoints overlap")
    return selected


def _checkpoint_position_for_row(row: Mapping[str, object]) -> tuple[int, int, int]:
    start = _require_int(row, "cue_start_ms", label="selected lyric row")
    end = _require_int(row, "cue_end_ms", label="selected lyric row")
    center_ms = (start + end) // 2
    window_ms = min(CHECKPOINT_WINDOW_MS, end - start)
    sample_start_ms = max(start, center_ms - window_ms // 2)
    sample_end_ms = sample_start_ms + window_ms
    if sample_end_ms > end:
        sample_end_ms = end
        sample_start_ms = sample_end_ms - window_ms
    return center_ms, sample_start_ms, sample_end_ms


def _session_host_anchor_position(alignment: Mapping[str, object]) -> tuple[int, int]:
    post_talk_ms = _require_int(alignment, "post_song_talk_start_ms", label="lyrics_alignment_report")
    artifacts = alignment.get("audio_alignment_artifacts")
    if not isinstance(artifacts, Mapping):
        raise HostVocalProofError("lyrics alignment report audio artifacts are missing")
    source_duration_ms = _require_int(artifacts, "source_duration_ms", label="audio_alignment_artifacts")
    last_lyric_ms = _require_int(alignment, "last_lyric_end_ms", label="lyrics_alignment_report")
    if post_talk_ms < last_lyric_ms or post_talk_ms >= source_duration_ms:
        raise HostVocalProofError("post-song host speech anchor is outside the source")
    duration_ms = min(SESSION_HOST_ANCHOR_WINDOW_MS, source_duration_ms - post_talk_ms)
    if duration_ms < MIN_SESSION_HOST_ANCHOR_MS:
        raise HostVocalProofError("post-song host speech anchor is too short")
    return post_talk_ms, post_talk_ms + duration_ms


def _session_host_anchor_positions(
    alignment: Mapping[str, object],
) -> tuple[tuple[int, int], ...]:
    """Search every post-song window, not a fixed horizon after the song.

    The anchor exists to supply one verified sample of the host *speaking* in
    this session, so the singing checkpoints can be judged against a
    same-domain reference instead of only against spoken enrollment.  The
    previous search stopped a fixed 48s after ``post_song_talk_start_ms``,
    which silently assumed that offset lands on host speech.  In a medley or
    3D-live setlist it can instead land inside the *next* song, and the real
    post-show talk then sits far beyond any fixed horizon: the search exhausts
    itself on singing, no window clears ``MIN_SESSION_ENROLL_MEDIAN``, the
    bridge stays closed, and a genuine host performance is rejected.  Scanning
    to the end of the source removes that assumption without moving a single
    threshold - every returned window still has to clear 0.50 against
    enrollment on its own.

    Windows overlapping the proven lyric span are excluded.  An anchor drawn
    from the song under proof would verify the performance with the
    performance and hand every remaining checkpoint a free bridge, which is
    precisely the playback/background-vocal hole this gate exists to close.
    The span comes from the hash-bound alignment report, not from a second
    boundary source.
    """

    first_start_ms, first_end_ms = _session_host_anchor_position(alignment)
    artifacts = alignment.get("audio_alignment_artifacts")
    assert isinstance(artifacts, Mapping)
    source_duration_ms = _require_int(
        artifacts, "source_duration_ms", label="audio_alignment_artifacts"
    )
    lyric_start_ms = _require_int(
        alignment, "first_lyric_start_ms", label="lyrics_alignment_report"
    )
    lyric_end_ms = _require_int(
        alignment, "last_lyric_end_ms", label="lyrics_alignment_report"
    )
    latest_start_ms = source_duration_ms - MIN_SESSION_HOST_ANCHOR_MS
    positions: list[tuple[int, int]] = []
    start_ms = first_start_ms
    while start_ms <= latest_start_ms:
        end_ms = min(start_ms + SESSION_HOST_ANCHOR_WINDOW_MS, source_duration_ms)
        overlaps_lyrics = start_ms < lyric_end_ms and end_ms > lyric_start_ms
        if end_ms - start_ms >= MIN_SESSION_HOST_ANCHOR_MS and not overlaps_lyrics:
            positions.append((start_ms, end_ms))
        start_ms += SESSION_HOST_ANCHOR_SEARCH_STEP_MS
    return tuple(positions) or ((first_start_ms, first_end_ms),)


def _checkpoint_passed(*, session_anchor_ready: bool, median_score: float, session_anchor_score: float) -> bool:
    """Apply the singing-domain identity rule used by generation and verification.

    Direct enrollment remains a valid lane.  When it is weak because the
    enrollment references are spoken, a verified same-session speech sample
    may bridge to the singing checkpoint instead of imposing two redundant
    hard gates that systematically reject the host's singing voice.
    """

    direct_match = median_score >= MIN_CHECKPOINT_MEDIAN
    session_bridge_match = session_anchor_ready and session_anchor_score >= MIN_SESSION_LYRIC_SCORE
    return direct_match or session_bridge_match


def _validate_session_anchor_search(
    raw_search: object,
    *,
    raw_selected_anchor: Mapping[str, object],
    positions: tuple[tuple[int, int], ...],
    selected_start_ms: int,
    selected_end_ms: int,
) -> None:
    if raw_search is None:
        if (selected_start_ms, selected_end_ms) != positions[0]:
            raise HostVocalProofError(
                "shifted session host anchor lacks its search record"
            )
        return
    raw_candidates = (
        raw_search.get("candidates") if isinstance(raw_search, Mapping) else None
    )
    if (
        not isinstance(raw_search, Mapping)
        or raw_search.get("strategy")
        != "first_verified_enrollment_window_after_song"
        or not isinstance(raw_candidates, list)
        or not raw_candidates
    ):
        raise HostVocalProofError("session host anchor search record is invalid")
    candidate_medians: list[float] = []
    for candidate_index, candidate in enumerate(raw_candidates):
        if not isinstance(candidate, Mapping) or candidate_index >= len(positions):
            raise HostVocalProofError(
                "session host anchor search candidates are invalid"
            )
        expected_start_ms, expected_end_ms = positions[candidate_index]
        candidate_start_ms = _require_int(
            candidate,
            "start_ms",
            label=f"session_host_anchor_search.candidates[{candidate_index}]",
        )
        candidate_end_ms = _require_int(
            candidate,
            "end_ms",
            label=f"session_host_anchor_search.candidates[{candidate_index}]",
        )
        candidate_median = _require_similarity_score(
            candidate.get("enroll_median_score"),
            label=(
                "session_host_anchor_search.candidates"
                f"[{candidate_index}].enroll_median_score"
            ),
        )
        candidate_passed = candidate_median >= MIN_SESSION_ENROLL_MEDIAN
        if (
            (candidate_start_ms, candidate_end_ms)
            != (expected_start_ms, expected_end_ms)
            or candidate.get("window_ms")
            != candidate_end_ms - candidate_start_ms
            or candidate.get("passed") is not candidate_passed
        ):
            raise HostVocalProofError(
                "session host anchor search candidates are invalid"
            )
        candidate_medians.append(candidate_median)
    selected_index = positions.index((selected_start_ms, selected_end_ms))
    if (
        selected_index >= len(raw_candidates)
        or raw_selected_anchor != raw_candidates[selected_index]
    ):
        raise HostVocalProofError(
            "session host anchor search does not bind the selected candidate"
        )
    if raw_selected_anchor.get("passed") is True:
        if (
            len(raw_candidates) != selected_index + 1
            or any(
                candidate.get("passed") is True
                for candidate in raw_candidates[:selected_index]
            )
        ):
            raise HostVocalProofError(
                "session host anchor search did not select the first verified window"
            )
    elif (
        len(raw_candidates) != len(positions)
        or candidate_medians[selected_index] != max(candidate_medians)
    ):
        raise HostVocalProofError(
            "session host anchor search did not retain the best failed window"
        )


def _decision_from_checkpoints(checkpoints: Sequence[Mapping[str, object]]) -> tuple[str, str, dict[str, object]]:
    passed_by_bucket = {bucket: 0 for bucket in REQUIRED_BUCKETS}
    passed_count = 0
    for checkpoint in checkpoints:
        bucket = str(checkpoint["bucket"])
        passed = bool(checkpoint["passed"])
        if passed:
            passed_count += 1
            if bucket in passed_by_bucket:
                passed_by_bucket[bucket] += 1
    bucket_coverage = {bucket: passed_by_bucket[bucket] >= 1 for bucket in REQUIRED_BUCKETS}
    ready = passed_count >= MIN_PASSED_CHECKPOINTS and all(bucket_coverage.values())
    distribution = {
        "passed_count": passed_count,
        "total_count": len(checkpoints),
        "passed_by_bucket": passed_by_bucket,
        "bucket_coverage": bucket_coverage,
        "minimum_passed_checkpoints": MIN_PASSED_CHECKPOINTS,
    }
    return (
        READY_STATUS if ready else BLOCKED_STATUS,
        READY_DECISION if ready else BLOCKED_DECISION,
        distribution,
    )


def _extract_checkpoint(source_media: Path, *, start_ms: int, expected_duration_ms: int, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(source_media),
        "-t",
        f"{expected_duration_ms / 1000:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise HostVocalProofError("ffmpeg is required to extract host-vocal checkpoints") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-800:]
        raise HostVocalProofError(f"ffmpeg checkpoint extraction failed: {detail}") from exc

    try:
        with wave.open(str(output_path), "rb") as handle:
            duration_ms = round(handle.getnframes() * 1000 / handle.getframerate())
            valid_format = handle.getnchannels() == 1 and handle.getframerate() == 16_000 and handle.getsampwidth() == 2
    except (OSError, EOFError, wave.Error) as exc:
        raise HostVocalProofError(f"invalid extracted checkpoint WAV {output_path}: {exc}") from exc
    if not valid_format or duration_ms < expected_duration_ms - 100:
        raise HostVocalProofError(
            f"checkpoint WAV must be mono PCM16 16kHz and at least {expected_duration_ms - 100}ms: {output_path}"
        )


def _extract_score(result: object) -> float:
    value: object = result
    if isinstance(result, Mapping):
        for key in ("score", "similarity", "scores"):
            if key in result:
                value = result[key]
                break
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 1:
            raise HostVocalProofError("CAM++ returned an ambiguous score sequence")
        value = value[0]
    return _require_similarity_score(value, label="CAM++ score")


def _load_campplus_pipeline(model_dir: Path):
    """Load ModelScope only in the dedicated ML CLI process."""

    try:
        from modelscope.pipelines import pipeline  # type: ignore[import-not-found]
        from modelscope.utils.constant import Tasks  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - exercised on the production ML runtime
        raise HostVocalProofError(f"cannot import ModelScope CAM++ runtime: {type(exc).__name__}: {exc}") from exc
    try:
        return pipeline(task=Tasks.speaker_verification, model=str(model_dir))
    except Exception as exc:  # pragma: no cover - exercised on the production ML runtime
        raise HostVocalProofError(f"cannot load CAM++ model: {type(exc).__name__}: {exc}") from exc


def generate_host_vocal_proof(
    *,
    source_media_path: Path,
    candidate_id: str,
    alignment_report_path: Path,
    profile_path: Path,
    reference_dir: Path,
    model_dir: Path,
    output_path: Path,
) -> dict[str, str]:
    """Generate a proof artifact and return its small hash-bound claim."""

    source_media_path = source_media_path.resolve(strict=True)
    alignment_report_path = alignment_report_path.resolve(strict=True)
    profile_path = profile_path.resolve(strict=True)
    reference_dir = reference_dir.resolve(strict=True)
    model_dir = model_dir.resolve(strict=True)
    output_path = output_path.resolve()
    if not source_media_path.is_file():
        raise HostVocalProofError(f"source media is not a file: {source_media_path}")
    if not alignment_report_path.is_file():
        raise HostVocalProofError(f"lyrics alignment report is not a file: {alignment_report_path}")
    if not profile_path.is_file():
        raise HostVocalProofError(f"reference profile is not a file: {profile_path}")
    if not reference_dir.is_dir():
        raise HostVocalProofError(f"reference directory is not a directory: {reference_dir}")
    if not candidate_id.strip():
        raise HostVocalProofError("candidate-id must not be empty")

    alignment = _load_json_object(alignment_report_path, label="lyrics alignment report")
    if alignment.get("candidate_id") != candidate_id:
        raise HostVocalProofError("lyrics alignment report candidate_id does not match --candidate-id")
    first_ms = _require_int(alignment, "first_lyric_start_ms", label="lyrics_alignment_report")
    last_ms = _require_int(alignment, "last_lyric_end_ms", label="lyrics_alignment_report")
    if first_ms < 0 or last_ms <= first_ms:
        raise HostVocalProofError("lyrics alignment report has an invalid first/last lyric span")
    selected_lyrics = _selected_lyric_rows(alignment)
    session_anchor_positions = _session_host_anchor_positions(alignment)

    profile = _load_json_object(profile_path, label="reference profile")
    model_info, expected_references = _validate_profile(profile)
    actual_model_sha = _sha256_directory(model_dir)
    if actual_model_sha != model_info["tree_sha256"]:
        raise HostVocalProofError("CAM++ model directory sha256 does not match reference profile")

    references: list[dict[str, str]] = []
    for expected in expected_references:
        reference_path = (reference_dir / expected["filename"]).resolve(strict=True)
        try:
            reference_path.relative_to(reference_dir)
        except ValueError as exc:
            raise HostVocalProofError(f"reference escapes --reference-dir: {expected['filename']}") from exc
        if not reference_path.is_file():
            raise HostVocalProofError(f"reference is not a file: {reference_path}")
        actual_sha = _sha256_file(reference_path)
        if actual_sha != expected["sha256"]:
            raise HostVocalProofError(f"reference sha256 mismatch: {expected['id']}")
        references.append({**expected, "path": str(reference_path)})

    source_sha = _sha256_file(source_media_path)
    audio_artifacts = alignment.get("audio_alignment_artifacts")
    if not isinstance(audio_artifacts, Mapping):
        raise HostVocalProofError("lyrics alignment report audio artifacts are missing")
    aligned_source_sha = _normalize_sha256(audio_artifacts.get("source_sha256"), label="audio alignment source")
    if aligned_source_sha != source_sha:
        raise HostVocalProofError("audio alignment and host-vocal proof source sha256 mismatch")
    alignment_sha = _sha256_file(alignment_report_path)
    profile_sha = _sha256_file(profile_path)
    checkpoint_dir = output_path.parent / f"{output_path.stem}.checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    speaker_verifier = _load_campplus_pipeline(model_dir)
    session_anchor_candidates: list[dict[str, object]] = []
    selected_session_anchor: dict[str, object] | None = None
    for anchor_index, (session_anchor_start_ms, session_anchor_end_ms) in enumerate(
        session_anchor_positions
    ):
        session_anchor_path = (
            checkpoint_dir / f"session-host-anchor-{anchor_index + 1:02d}.wav"
        ).resolve()
        _extract_checkpoint(
            source_media_path,
            start_ms=session_anchor_start_ms,
            expected_duration_ms=session_anchor_end_ms - session_anchor_start_ms,
            output_path=session_anchor_path,
        )
        session_reference_scores: list[dict[str, object]] = []
        for reference in references:
            try:
                raw_result = speaker_verifier(
                    [reference["path"], str(session_anchor_path)]
                )
            except Exception as exc:  # pragma: no cover - production ML runtime
                raise HostVocalProofError(
                    f"CAM++ session-host anchor inference failed: {type(exc).__name__}: {exc}"
                ) from exc
            session_reference_scores.append(
                {
                    "reference_id": reference["id"],
                    "score": round(_extract_score(raw_result), 8),
                }
            )
        session_enroll_median = round(
            float(statistics.median(row["score"] for row in session_reference_scores)),
            8,
        )
        candidate: dict[str, object] = {
            "start_ms": session_anchor_start_ms,
            "end_ms": session_anchor_end_ms,
            "window_ms": session_anchor_end_ms - session_anchor_start_ms,
            "sample_path": str(session_anchor_path),
            "sample_sha256": _sha256_file(session_anchor_path),
            "reference_scores": session_reference_scores,
            "enroll_median_score": session_enroll_median,
            "passed": session_enroll_median >= MIN_SESSION_ENROLL_MEDIAN,
        }
        session_anchor_candidates.append(candidate)
        if candidate["passed"] is True:
            selected_session_anchor = candidate
            break
    if selected_session_anchor is None:
        selected_session_anchor = max(
            session_anchor_candidates,
            key=lambda candidate: float(candidate["enroll_median_score"]),
        )
    session_host_anchor = dict(selected_session_anchor)
    session_anchor_path = Path(str(session_host_anchor["sample_path"]))
    session_anchor_ready = session_host_anchor["passed"] is True
    checkpoints: list[dict[str, object]] = []
    for index, (fraction, bucket, lyric_row) in enumerate(
        zip(CHECKPOINT_FRACTIONS, CHECKPOINT_BUCKETS, selected_lyrics, strict=True)
    ):
        center_ms, start_ms, end_ms = _checkpoint_position_for_row(lyric_row)
        checkpoint_path = (checkpoint_dir / f"checkpoint-{index + 1:02d}.wav").resolve()
        _extract_checkpoint(
            source_media_path,
            start_ms=start_ms,
            expected_duration_ms=end_ms - start_ms,
            output_path=checkpoint_path,
        )
        scores: list[dict[str, object]] = []
        for reference in references:
            try:
                raw_result = speaker_verifier([reference["path"], str(checkpoint_path)])
            except Exception as exc:  # pragma: no cover - exercised on the production ML runtime
                raise HostVocalProofError(
                    f"CAM++ inference failed for checkpoint {index + 1}/{reference['id']}: {type(exc).__name__}: {exc}"
                ) from exc
            score = round(_extract_score(raw_result), 8)
            scores.append({"reference_id": reference["id"], "score": score})
        median_score = round(float(statistics.median(row["score"] for row in scores)), 8)
        try:
            session_anchor_score = round(
                _extract_score(speaker_verifier([str(session_anchor_path), str(checkpoint_path)])), 8
            )
        except Exception as exc:  # pragma: no cover - production ML runtime
            raise HostVocalProofError(
                f"CAM++ session-anchor inference failed for checkpoint {index + 1}: {type(exc).__name__}: {exc}"
            ) from exc
        checkpoints.append(
            {
                "index": index,
                "fraction": fraction,
                "bucket": bucket,
                "alignment_index": lyric_row["alignment_index"],
                "matched_cue_id": lyric_row["matched_cue_id"],
                "lrc_time_ms": lyric_row["lrc_time_ms"],
                "lrc_text": lyric_row["lrc_text"],
                "lyric_cue_start_ms": lyric_row["cue_start_ms"],
                "lyric_cue_end_ms": lyric_row["cue_end_ms"],
                "lyric_vocal_subject": lyric_row["lyric_vocal_subject"],
                "lidousha_role": lyric_row["lidousha_role"],
                "same_live_vocal_source_as_lidousha": lyric_row[
                    "same_live_vocal_source_as_lidousha"
                ],
                "other_singer_or_harmony_audible": lyric_row[
                    "other_singer_or_harmony_audible"
                ],
                "recorded_or_playback_vocal_audible": lyric_row[
                    "recorded_or_playback_vocal_audible"
                ],
                "center_ms": center_ms,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "window_ms": end_ms - start_ms,
                "sample_path": str(checkpoint_path),
                "sample_sha256": _sha256_file(checkpoint_path),
                "scores": scores,
                "median_score": median_score,
                "session_anchor_score": session_anchor_score,
                "passed": _checkpoint_passed(
                    session_anchor_ready=session_anchor_ready,
                    median_score=median_score,
                    session_anchor_score=session_anchor_score,
                ),
            }
        )

    checkpoint_pcm_hashes = [str(checkpoint["sample_sha256"]) for checkpoint in checkpoints]
    if len(set(checkpoint_pcm_hashes)) != len(checkpoint_pcm_hashes):
        raise HostVocalProofError("host-vocal checkpoints must contain seven distinct PCM samples")

    status, decision, distribution = _decision_from_checkpoints(checkpoints)
    proof: dict[str, object] = {
        "schema_version": PROOF_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "status": status,
        "decision": decision,
        "source_media": {"path": str(source_media_path), "sha256": source_sha},
        "lyrics_alignment_report": {
            "path": str(alignment_report_path),
            "sha256": alignment_sha,
            "first_lyric_start_ms": first_ms,
            "last_lyric_end_ms": last_ms,
        },
        "reference_profile": {
            "path": str(profile_path),
            "sha256": profile_sha,
            "schema_version": profile["schema_version"],
            "profile_id": profile["profile_id"],
        },
        "speaker_model": {
            "path": str(model_dir),
            "model_id": model_info["model_id"],
            "tree_sha256": actual_model_sha,
        },
        "references": references,
        "session_host_anchor": session_host_anchor,
        "session_host_anchor_search": {
            "strategy": "first_verified_enrollment_window_after_song",
            "candidates": session_anchor_candidates,
        },
        "policy": _canonical_policy(),
        "checkpoints": checkpoints,
        "distribution": distribution,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output_path.parent, prefix=f".{output_path.name}.", delete=False
    ) as handle:
        temporary_path = Path(handle.name)
        json.dump(proof, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary_path.replace(output_path)
    proof_sha = _sha256_file(output_path)
    return {"status": status, "proof_path": str(output_path), "proof_sha256": proof_sha, "decision": decision}


def verify_host_vocal_proof_claim(
    claim: Mapping[str, object] | None,
    expected_candidate_id: str,
    source_media_path: Path,
    alignment_report_path: Path,
    profile_path: Path,
) -> str | None:
    """Verify a small host-vocal claim without importing the ML runtime.

    Returns ``None`` for an internally consistent READY *or BLOCKED* proof.
    Callers must separately require ``claim["status"] == "READY"`` before
    accepting a song.  Any missing/tampered artifact returns a readable error.
    """

    try:
        _verify_host_vocal_proof_claim(
            claim,
            expected_candidate_id=expected_candidate_id,
            source_media_path=source_media_path,
            alignment_report_path=alignment_report_path,
            profile_path=profile_path,
        )
    except (HostVocalProofError, OSError) as exc:
        return str(exc)
    return None


def _verify_host_vocal_proof_claim(
    claim: Mapping[str, object] | None,
    *,
    expected_candidate_id: str,
    source_media_path: Path,
    alignment_report_path: Path,
    profile_path: Path,
) -> None:
    if not isinstance(claim, Mapping):
        raise HostVocalProofError("host vocal proof claim is missing")
    claimed_status = _require_string(claim, "status", label="host_vocal_proof")
    claimed_decision = _require_string(claim, "decision", label="host_vocal_proof")
    if claimed_status not in {READY_STATUS, BLOCKED_STATUS}:
        raise HostVocalProofError("host_vocal_proof.status is invalid")
    proof_path_value = _require_string(claim, "proof_path", label="host_vocal_proof")
    proof_path = Path(proof_path_value).resolve(strict=True)
    if not proof_path.is_file():
        raise HostVocalProofError(f"host vocal proof does not exist: {proof_path}")
    claimed_sha = _normalize_sha256(claim.get("proof_sha256"), label="host_vocal_proof.proof")
    if _sha256_file(proof_path) != claimed_sha:
        raise HostVocalProofError("host vocal proof sha256 mismatch")

    proof = _load_json_object(proof_path, label="host vocal proof")
    if proof.get("schema_version") != PROOF_SCHEMA_VERSION:
        raise HostVocalProofError(f"host vocal proof schema_version must be {PROOF_SCHEMA_VERSION}")
    if proof.get("candidate_id") != expected_candidate_id:
        raise HostVocalProofError("host vocal proof candidate_id mismatch")
    if proof.get("status") != claimed_status or proof.get("decision") != claimed_decision:
        raise HostVocalProofError("host vocal proof claim status/decision does not match its artifact")

    source_media_path = source_media_path.resolve(strict=True)
    alignment_report_path = alignment_report_path.resolve(strict=True)
    profile_path = profile_path.resolve(strict=True)
    bindings = (
        ("source_media", source_media_path, "sha256"),
        ("lyrics_alignment_report", alignment_report_path, "sha256"),
        ("reference_profile", profile_path, "sha256"),
    )
    for field, expected_path, sha_field in bindings:
        binding = proof.get(field)
        if not isinstance(binding, Mapping):
            raise HostVocalProofError(f"host vocal proof {field} binding is missing")
        bound_path = Path(_require_string(binding, "path", label=field)).resolve(strict=True)
        if bound_path != expected_path:
            raise HostVocalProofError(f"host vocal proof {field} path mismatch")
        declared_sha = _normalize_sha256(binding.get(sha_field), label=f"host vocal proof {field}")
        if _sha256_file(expected_path) != declared_sha:
            raise HostVocalProofError(f"host vocal proof {field} sha256 mismatch")

    profile = _load_json_object(profile_path, label="reference profile")
    model_info, expected_references = _validate_profile(profile)
    reference_binding = proof.get("reference_profile")
    if (
        not isinstance(reference_binding, Mapping)
        or reference_binding.get("schema_version") != profile.get("schema_version")
    ):
        raise HostVocalProofError("host vocal proof reference profile schema mismatch")
    if proof.get("policy") != _canonical_policy():
        raise HostVocalProofError("host vocal proof policy does not match the compiled fail-closed policy")

    model_binding = proof.get("speaker_model")
    if not isinstance(model_binding, Mapping):
        raise HostVocalProofError("host vocal proof speaker_model binding is missing")
    model_path = Path(_require_string(model_binding, "path", label="speaker_model")).resolve(strict=True)
    if model_binding.get("model_id") != model_info["model_id"]:
        raise HostVocalProofError("host vocal proof speaker_model model_id mismatch")
    declared_model_sha = _normalize_sha256(model_binding.get("tree_sha256"), label="speaker_model.tree")
    if declared_model_sha != model_info["tree_sha256"] or _sha256_directory(model_path) != declared_model_sha:
        raise HostVocalProofError("host vocal proof speaker_model sha256 mismatch")

    raw_references = proof.get("references")
    if not isinstance(raw_references, list) or len(raw_references) != EXPECTED_REFERENCE_COUNT:
        raise HostVocalProofError(f"host vocal proof must contain exactly {EXPECTED_REFERENCE_COUNT} references")
    expected_reference_ids = [reference["id"] for reference in expected_references]
    for index, (raw_reference, expected) in enumerate(zip(raw_references, expected_references, strict=True)):
        if not isinstance(raw_reference, Mapping):
            raise HostVocalProofError(f"host vocal proof references[{index}] must be an object")
        if raw_reference.get("id") != expected["id"] or raw_reference.get("filename") != expected["filename"]:
            raise HostVocalProofError(f"host vocal proof reference metadata mismatch at index {index}")
        declared_reference_sha = _normalize_sha256(raw_reference.get("sha256"), label=f"references[{index}]")
        if declared_reference_sha != expected["sha256"]:
            raise HostVocalProofError(f"host vocal proof reference profile hash mismatch: {expected['id']}")
        reference_path = Path(_require_string(raw_reference, "path", label=f"references[{index}]")).resolve(strict=True)
        if reference_path.name != Path(expected["filename"]).name or _sha256_file(reference_path) != declared_reference_sha:
            raise HostVocalProofError(f"host vocal proof reference sha256 mismatch: {expected['id']}")

    alignment_binding = proof.get("lyrics_alignment_report")
    if not isinstance(alignment_binding, Mapping):
        raise HostVocalProofError("host vocal proof lyrics_alignment_report binding is missing")
    alignment = _load_json_object(alignment_report_path, label="lyrics alignment report")
    if alignment.get("candidate_id") != expected_candidate_id:
        raise HostVocalProofError("lyrics alignment report candidate_id mismatch")
    first_ms = _require_int(alignment, "first_lyric_start_ms", label="lyrics_alignment_report")
    last_ms = _require_int(alignment, "last_lyric_end_ms", label="lyrics_alignment_report")
    if first_ms < 0 or last_ms <= first_ms:
        raise HostVocalProofError("lyrics alignment report has an invalid first/last lyric span")
    selected_lyrics = _selected_lyric_rows(alignment)
    session_anchor_positions = _session_host_anchor_positions(alignment)
    audio_artifacts = alignment.get("audio_alignment_artifacts")
    if not isinstance(audio_artifacts, Mapping):
        raise HostVocalProofError("lyrics alignment report audio artifacts are missing")
    aligned_source_sha = _normalize_sha256(audio_artifacts.get("source_sha256"), label="audio alignment source")
    source_binding = proof.get("source_media")
    if not isinstance(source_binding, Mapping):
        raise HostVocalProofError("host vocal proof source_media binding is missing")
    if aligned_source_sha != _normalize_sha256(source_binding.get("sha256"), label="host vocal source"):
        raise HostVocalProofError("audio alignment and host-vocal proof source sha256 mismatch")
    if alignment_binding.get("first_lyric_start_ms") != first_ms or alignment_binding.get("last_lyric_end_ms") != last_ms:
        raise HostVocalProofError("host vocal proof lyric span does not match alignment report")

    raw_checkpoints = proof.get("checkpoints")
    if not isinstance(raw_checkpoints, list) or len(raw_checkpoints) != len(CHECKPOINT_FRACTIONS):
        raise HostVocalProofError(f"host vocal proof must contain exactly {len(CHECKPOINT_FRACTIONS)} checkpoints")
    checked_checkpoints: list[dict[str, object]] = []
    checked_sample_hashes: set[str] = set()
    checkpoint_root = proof_path.parent / f"{proof_path.stem}.checkpoints"
    raw_session_anchor = proof.get("session_host_anchor")
    if not isinstance(raw_session_anchor, Mapping):
        raise HostVocalProofError("host vocal proof session_host_anchor is missing")
    session_anchor_start_ms = _require_int(
        raw_session_anchor, "start_ms", label="session_host_anchor"
    )
    session_anchor_end_ms = _require_int(
        raw_session_anchor, "end_ms", label="session_host_anchor"
    )
    if (
        (session_anchor_start_ms, session_anchor_end_ms)
        not in session_anchor_positions
        or raw_session_anchor.get("window_ms") != session_anchor_end_ms - session_anchor_start_ms
    ):
        raise HostVocalProofError("session host anchor timing mismatch")
    _validate_session_anchor_search(
        proof.get("session_host_anchor_search"),
        raw_selected_anchor=raw_session_anchor,
        positions=session_anchor_positions,
        selected_start_ms=session_anchor_start_ms,
        selected_end_ms=session_anchor_end_ms,
    )
    session_sample_path = Path(
        _require_string(raw_session_anchor, "sample_path", label="session_host_anchor")
    ).resolve(strict=True)
    try:
        session_sample_path.relative_to(checkpoint_root.resolve())
    except ValueError as exc:
        raise HostVocalProofError("session host anchor sample escapes proof checkpoint directory") from exc
    session_sample_sha = _normalize_sha256(
        raw_session_anchor.get("sample_sha256"), label="session_host_anchor.sample"
    )
    if _sha256_file(session_sample_path) != session_sample_sha:
        raise HostVocalProofError("session host anchor sample sha256 mismatch")
    raw_session_scores = raw_session_anchor.get("reference_scores")
    if not isinstance(raw_session_scores, list) or len(raw_session_scores) != EXPECTED_REFERENCE_COUNT:
        raise HostVocalProofError("session host anchor reference scores are invalid")
    session_scores: list[float] = []
    for score_index, raw_score in enumerate(raw_session_scores):
        if not isinstance(raw_score, Mapping) or raw_score.get("reference_id") != expected_reference_ids[score_index]:
            raise HostVocalProofError("session host anchor reference order mismatch")
        session_scores.append(
            _require_similarity_score(
                raw_score.get("score"), label=f"session_host_anchor.reference_scores[{score_index}]"
            )
        )
    session_enroll_median = round(float(statistics.median(session_scores)), 8)
    if _require_similarity_score(
        raw_session_anchor.get("enroll_median_score"), label="session_host_anchor.enroll_median_score"
    ) != session_enroll_median:
        raise HostVocalProofError("session host anchor median score mismatch")
    session_anchor_ready = session_enroll_median >= MIN_SESSION_ENROLL_MEDIAN
    if raw_session_anchor.get("passed") is not session_anchor_ready:
        raise HostVocalProofError("session host anchor threshold decision mismatch")
    for index, (raw_checkpoint, fraction, bucket, lyric_row) in enumerate(
        zip(raw_checkpoints, CHECKPOINT_FRACTIONS, CHECKPOINT_BUCKETS, selected_lyrics, strict=True)
    ):
        if not isinstance(raw_checkpoint, Mapping):
            raise HostVocalProofError(f"checkpoint[{index}] must be an object")
        if raw_checkpoint.get("index") != index or raw_checkpoint.get("fraction") != fraction or raw_checkpoint.get("bucket") != bucket:
            raise HostVocalProofError(f"checkpoint[{index}] identity/fraction/bucket mismatch")
        if (
            raw_checkpoint.get("alignment_index") != lyric_row["alignment_index"]
            or raw_checkpoint.get("matched_cue_id") != lyric_row["matched_cue_id"]
            or raw_checkpoint.get("lrc_time_ms") != lyric_row["lrc_time_ms"]
            or raw_checkpoint.get("lrc_text") != lyric_row["lrc_text"]
            or raw_checkpoint.get("lyric_cue_start_ms") != lyric_row["cue_start_ms"]
            or raw_checkpoint.get("lyric_cue_end_ms") != lyric_row["cue_end_ms"]
            or raw_checkpoint.get("lyric_vocal_subject") != lyric_row["lyric_vocal_subject"]
            or raw_checkpoint.get("lidousha_role") != lyric_row["lidousha_role"]
            or raw_checkpoint.get("same_live_vocal_source_as_lidousha")
            is not lyric_row["same_live_vocal_source_as_lidousha"]
            or raw_checkpoint.get("other_singer_or_harmony_audible")
            is not lyric_row["other_singer_or_harmony_audible"]
            or raw_checkpoint.get("recorded_or_playback_vocal_audible")
            is not lyric_row["recorded_or_playback_vocal_audible"]
        ):
            raise HostVocalProofError(f"checkpoint[{index}] is not bound to its selected lyric row")
        center_ms, start_ms, end_ms = _checkpoint_position_for_row(lyric_row)
        if (
            raw_checkpoint.get("center_ms") != center_ms
            or raw_checkpoint.get("start_ms") != start_ms
            or raw_checkpoint.get("end_ms") != end_ms
            or raw_checkpoint.get("window_ms") != end_ms - start_ms
        ):
            raise HostVocalProofError(f"checkpoint[{index}] timing does not match lyric span")
        sample_path = Path(_require_string(raw_checkpoint, "sample_path", label=f"checkpoint[{index}]")).resolve(strict=True)
        try:
            sample_path.relative_to(checkpoint_root.resolve())
        except ValueError as exc:
            raise HostVocalProofError(f"checkpoint[{index}] sample path escapes proof checkpoint directory") from exc
        sample_sha = _normalize_sha256(raw_checkpoint.get("sample_sha256"), label=f"checkpoint[{index}].sample")
        if _sha256_file(sample_path) != sample_sha:
            raise HostVocalProofError(f"checkpoint[{index}] sample sha256 mismatch")
        if sample_sha in checked_sample_hashes:
            raise HostVocalProofError("host-vocal checkpoints reuse decoded PCM")
        checked_sample_hashes.add(sample_sha)
        raw_scores = raw_checkpoint.get("scores")
        if not isinstance(raw_scores, list) or len(raw_scores) != EXPECTED_REFERENCE_COUNT:
            raise HostVocalProofError(f"checkpoint[{index}] must contain exactly {EXPECTED_REFERENCE_COUNT} scores")
        scores: list[float] = []
        for score_index, raw_score in enumerate(raw_scores):
            if not isinstance(raw_score, Mapping) or raw_score.get("reference_id") != expected_reference_ids[score_index]:
                raise HostVocalProofError(f"checkpoint[{index}] score reference order mismatch")
            scores.append(
                _require_similarity_score(
                    raw_score.get("score"), label=f"checkpoint[{index}].scores[{score_index}].score"
                )
            )
        median_score = round(float(statistics.median(scores)), 8)
        if _require_similarity_score(
            raw_checkpoint.get("median_score"), label=f"checkpoint[{index}].median_score"
        ) != median_score:
            raise HostVocalProofError(f"checkpoint[{index}] median score was not recomputed honestly")
        session_anchor_score = _require_similarity_score(
            raw_checkpoint.get("session_anchor_score"), label=f"checkpoint[{index}].session_anchor_score"
        )
        passed = _checkpoint_passed(
            session_anchor_ready=session_anchor_ready,
            median_score=median_score,
            session_anchor_score=session_anchor_score,
        )
        if raw_checkpoint.get("passed") is not passed:
            raise HostVocalProofError(f"checkpoint[{index}] threshold decision mismatch")
        checked_checkpoints.append({"bucket": bucket, "passed": passed})

    expected_status, expected_decision, expected_distribution = _decision_from_checkpoints(checked_checkpoints)
    if proof.get("distribution") != expected_distribution:
        raise HostVocalProofError("host vocal proof checkpoint distribution mismatch")
    if proof.get("status") != expected_status or proof.get("decision") != expected_decision:
        raise HostVocalProofError("host vocal proof aggregate decision mismatch")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a hash-bound "
            f"{CHANNEL_PROFILE.display_name} host-vocal proof"
        )
    )
    parser.add_argument("--source-media", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--lyrics-alignment-report", type=Path, required=True)
    parser.add_argument("--reference-profile", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        claim = generate_host_vocal_proof(
            source_media_path=args.source_media,
            candidate_id=args.candidate_id,
            alignment_report_path=args.lyrics_alignment_report,
            profile_path=args.reference_profile,
            reference_dir=args.reference_dir,
            model_dir=args.model_dir,
            output_path=args.output,
        )
    except (HostVocalProofError, OSError) as exc:
        print(f"host-vocal proof failed closed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(claim, ensure_ascii=False, sort_keys=True))
    return 0 if claim["status"] == READY_STATUS else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
