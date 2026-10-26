"""Locate one complete short reference inside the current target audio.

This is a bounded, deterministic hint for the unaligned voice-reference lane.
It never proves source playback, speaker identity, caption ownership, or a
linear mapping.  The caller must still bind the returned object to the current
media, SRT, frame, and ordinary observer request before it can be consumed.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import array
import json
import math
import re
import wave
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse

SCHEMA_VERSION = "short-reference-playback-locator.v1"
LEGACY_ALGORITHM_VERSION = "mono-normalized-cross-correlation-v1"
CONTEXT_ALGORITHM_VERSION = "context-overlap-normalized-cross-correlation-v2"
ALGORITHM_VERSION = "cue-overlap-normalized-cross-correlation-v3"
MAX_REFERENCE_DURATION_MS = 8000
MIN_REFERENCE_DURATION_MS = 20
MIN_ABS_NCC = 0.98
MIN_SCORE_SEPARATION = 0.10
_HASH_RE = re.compile(r"(?:sha256:)?[0-9a-fA-F]{64}\Z")


class ShortReferencePlaybackLocatorError(ValueError):
    """A locator cannot be safely computed or replayed."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _normal_hash(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise ShortReferencePlaybackLocatorError(code)
    value = value.strip()
    if not _HASH_RE.fullmatch(value):
        raise ShortReferencePlaybackLocatorError(code)
    return "sha256:" + value.removeprefix("sha256:").lower()


def _sha256(path: Path) -> str:
    try:
        digest = _evidence_file_sha256(path)
    except OSError as exc:
        raise ShortReferencePlaybackLocatorError("LOCATOR_AUDIO_UNREADABLE") from exc
    return "sha256:" + digest


def _regular_file(value: str | Path, *, code: str = "LOCATOR_AUDIO_UNAVAILABLE") -> Path:
    candidate = Path(value)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ShortReferencePlaybackLocatorError(code) from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise ShortReferencePlaybackLocatorError(code)
    return resolved


def _interval(value: object, *, code: str) -> list[float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
        raise ShortReferencePlaybackLocatorError(code)
    values: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ShortReferencePlaybackLocatorError(code)
        number = float(item)
        if not math.isfinite(number):
            raise ShortReferencePlaybackLocatorError(code)
        values.append(number)
    if values[0] < 0 or values[1] <= values[0]:
        raise ShortReferencePlaybackLocatorError(code)
    return values


def _round_ms(value: float) -> int | float:
    rounded = round(float(value), 3)
    return int(rounded) if rounded.is_integer() else rounded


def _read_pcm(path_value: str | Path, expected_sha: object, *, label: str) -> dict[str, object]:
    path = _regular_file(path_value, code=f"{label}_AUDIO_UNAVAILABLE")
    actual_sha = _sha256(path)
    expected = _normal_hash(expected_sha, code=f"{label}_AUDIO_HASH_INVALID")
    if actual_sha != expected:
        raise ShortReferencePlaybackLocatorError(f"{label}_AUDIO_HASH_MISMATCH")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
            compression = handle.getcomptype()
            raw = handle.readframes(frame_count)
    except (OSError, wave.Error) as exc:
        raise ShortReferencePlaybackLocatorError(f"{label}_AUDIO_WAVE_INVALID") from exc
    if (
        channels not in (1, 2)
        or sample_width != 2
        or sample_rate <= 0
        or compression != "NONE"
        or len(raw) != frame_count * channels * sample_width
    ):
        raise ShortReferencePlaybackLocatorError(f"{label}_AUDIO_FORMAT_INVALID")
    values = array.array("h")
    values.frombytes(raw)
    if values.itemsize != 2:
        raise ShortReferencePlaybackLocatorError(f"{label}_AUDIO_FORMAT_INVALID")
    if __import__("sys").byteorder != "little":
        values.byteswap()
    # Existing normal preparation emits stereo PCM.  The matcher uses one
    # deterministic mono projection and records that fact; it never treats a
    # channel correlation as playback authority.
    if channels == 1:
        mono = [float(value) / 32768.0 for value in values]
    else:
        mono = [
            (float(values[index]) + float(values[index + 1])) / (2.0 * 32768.0)
            for index in range(0, len(values), 2)
        ]
    if len(mono) != frame_count or not mono:
        raise ShortReferencePlaybackLocatorError(f"{label}_AUDIO_EMPTY")
    return {
        "path": str(path),
        "sha256": actual_sha,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "duration_ms": frame_count * 1000.0 / sample_rate,
        "samples": mono,
    }


def _ncc_scores(target: list[float], reference: list[float]) -> tuple[list[float], int]:
    """Return signed NCC for every valid target offset and the backend name."""
    target_count = len(target)
    reference_count = len(reference)
    if reference_count > target_count:
        raise ShortReferencePlaybackLocatorError("LOCATOR_REFERENCE_LONGER_THAN_TARGET")
    reference_mean = sum(reference) / reference_count
    centered_reference = [value - reference_mean for value in reference]
    reference_energy = sum(value * value for value in centered_reference)
    if reference_energy <= 1e-10:
        raise ShortReferencePlaybackLocatorError("LOCATOR_REFERENCE_SILENT")
    count = target_count - reference_count + 1
    try:
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:
        np = None
    if np is not None:
        target_array = np.asarray(target, dtype=np.float64)
        reference_array = np.asarray(centered_reference, dtype=np.float64)
        if target_count * reference_count > 2_000_000:
            # Same valid correlation as the existing long-source matcher;
            # avoid quadratic work for ordinary 48 kHz button recordings.
            nfft = 1 << (target_count + reference_count - 1).bit_length()
            numerator = np.fft.irfft(
                np.fft.rfft(target_array, nfft)
                * np.conj(np.fft.rfft(reference_array, nfft)), nfft,
            )[:count]
        else:
            numerator = np.correlate(target_array, reference_array, mode="valid")
        prefix = np.concatenate(([0.0], np.cumsum(target_array)))
        prefix_sq = np.concatenate(([0.0], np.cumsum(target_array * target_array)))
        sums = prefix[reference_count:] - prefix[:-reference_count]
        sums_sq = prefix_sq[reference_count:] - prefix_sq[:-reference_count]
        energies = sums_sq - (sums * sums / reference_count)
        denominator = np.sqrt(np.maximum(energies, 0.0) * reference_energy)
        scores = np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator > 1e-10)
        return [float(value) for value in scores], 1
    # Unit tests use tiny fixtures under the portable venv.  Keep a stdlib
    # fallback there; production runtimes with long PCM use the vectorized
    # path above (or the exact same algorithm remains available for small
    # media without a third-party dependency).
    if target_count * reference_count > 2_000_000:
        raise ShortReferencePlaybackLocatorError("LOCATOR_NUMPY_REQUIRED_FOR_LONG_PCM")
    scores: list[float] = []
    for start in range(count):
        window = target[start : start + reference_count]
        mean = sum(window) / reference_count
        centered = [value - mean for value in window]
        energy = sum(value * value for value in centered)
        if energy <= 1e-10:
            scores.append(0.0)
            continue
        numerator = sum(left * right for left, right in zip(centered, centered_reference, strict=True))
        scores.append(numerator / math.sqrt(energy * reference_energy))
    return scores, 0


def _context_payload(context: Mapping[str, object] | None) -> dict[str, object]:
    if context is None:
        return {}
    try:
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ShortReferencePlaybackLocatorError("LOCATOR_CONTEXT_INVALID") from exc
    if not isinstance(payload, dict):
        raise ShortReferencePlaybackLocatorError("LOCATOR_CONTEXT_INVALID")
    return payload


def _locate_short_reference(
    *,
    reference_audio_path: str | Path,
    reference_audio_sha256: str,
    target_audio_path: str | Path,
    target_audio_sha256: str,
    target_audio_window_ms: Sequence[int | float],
    target_interval_ms: Sequence[int | float],
    reference_source_path: str | Path,
    reference_source_sha256: str,
    source_uri: str,
    binding_context: Mapping[str, object] | None = None,
    algorithm_version: str,
) -> dict[str, object]:
    """Find a unique complete short reference in a current target window.

    A successful result is only a bounded EVIDENCE_ONLY hint.  Every byte and
    interval used by the calculation is included, and ``validate_locator``
    recomputes the score before a consumer accepts it.
    """
    window = _interval(target_audio_window_ms, code="LOCATOR_TARGET_WINDOW_INVALID")
    interval = _interval(target_interval_ms, code="LOCATOR_TARGET_INTERVAL_INVALID")
    if not window[0] <= interval[0] < interval[1] <= window[1]:
        raise ShortReferencePlaybackLocatorError("LOCATOR_TARGET_INTERVAL_INVALID")
    parsed_uri = urlparse(source_uri.strip() if isinstance(source_uri, str) else "")
    if parsed_uri.scheme not in {"http", "https"} or not parsed_uri.netloc:
        raise ShortReferencePlaybackLocatorError("LOCATOR_SOURCE_URI_INVALID")
    reference_source = _regular_file(reference_source_path, code="LOCATOR_SOURCE_MEDIA_UNAVAILABLE")
    source_sha = _sha256(reference_source)
    if source_sha != _normal_hash(reference_source_sha256, code="LOCATOR_SOURCE_MEDIA_HASH_INVALID"):
        raise ShortReferencePlaybackLocatorError("LOCATOR_SOURCE_MEDIA_HASH_MISMATCH")
    reference = _read_pcm(reference_audio_path, reference_audio_sha256, label="REFERENCE")
    target = _read_pcm(target_audio_path, target_audio_sha256, label="TARGET")
    if reference["sample_rate_hz"] != target["sample_rate_hz"]:
        raise ShortReferencePlaybackLocatorError("LOCATOR_SAMPLE_RATE_MISMATCH")
    reference_duration_ms = float(reference["duration_ms"])
    if not MIN_REFERENCE_DURATION_MS <= reference_duration_ms < MAX_REFERENCE_DURATION_MS:
        raise ShortReferencePlaybackLocatorError("LOCATOR_REFERENCE_DURATION_UNSUPPORTED")
    target_duration_ms = float(target["duration_ms"])
    if abs(target_duration_ms - (window[1] - window[0])) > 20.0:
        raise ShortReferencePlaybackLocatorError("LOCATOR_TARGET_WINDOW_DURATION_MISMATCH")
    reference_samples = reference["samples"]
    target_samples = target["samples"]
    assert isinstance(reference_samples, list) and isinstance(target_samples, list)
    interval_start = round((interval[0] - window[0]) * int(target["sample_rate_hz"]) / 1000.0)
    interval_end = round((interval[1] - window[0]) * int(target["sample_rate_hz"]) / 1000.0)
    legacy = algorithm_version == LEGACY_ALGORITHM_VERSION
    if algorithm_version not in (LEGACY_ALGORITHM_VERSION, CONTEXT_ALGORITHM_VERSION, ALGORITHM_VERSION):
        raise ShortReferencePlaybackLocatorError("LOCATOR_ALGORITHM_UNSUPPORTED")
    search_start, search_end = ((interval_start, interval_end) if legacy else (0, len(target_samples)))
    if search_end - search_start < len(reference_samples):
        raise ShortReferencePlaybackLocatorError("LOCATOR_REFERENCE_DOES_NOT_FIT_TARGET")
    reference_rms = math.sqrt(sum(value * value for value in reference_samples) / len(reference_samples))
    target_rms = math.sqrt(sum(value * value for value in target_samples) / len(target_samples))
    if reference_rms < 0.005 or target_rms < 0.005:
        raise ShortReferencePlaybackLocatorError("LOCATOR_SILENT_AUDIO")
    scores, backend = _ncc_scores(target_samples[search_start:search_end], reference_samples)
    if not scores:
        raise ShortReferencePlaybackLocatorError("LOCATOR_NO_CANDIDATE")
    ranked = sorted(range(len(scores)), key=lambda index: abs(scores[index]), reverse=True)
    global_best_index = ranked[0]
    if algorithm_version == ALGORITHM_VERSION:
        # A replay in a neighbouring cue is not an alternative origin for
        # this cue. Only complete-reference matches intersecting this cue
        # compete; more than one such match remains ambiguous.
        ranked = [index for index in ranked
                  if index < interval_end and index + len(reference_samples) > interval_start]
        if not ranked:
            raise ShortReferencePlaybackLocatorError("LOCATOR_NO_CANDIDATE")
    best_index = ranked[0]
    best_score = float(scores[best_index])
    best_abs = abs(best_score)
    reference_count = len(reference_samples)
    runner_up_abs = 0.0
    for index in ranked[1:]:
        if index >= best_index + reference_count or index + reference_count <= best_index:
            runner_up_abs = abs(float(scores[index]))
            break
    separation = best_abs - runner_up_abs
    if best_abs < MIN_ABS_NCC:
        if algorithm_version == ALGORITHM_VERSION and abs(scores[global_best_index]) >= MIN_ABS_NCC:
            raise ShortReferencePlaybackLocatorError("LOCATOR_MATCH_DOES_NOT_OVERLAP_TARGET")
        raise ShortReferencePlaybackLocatorError("LOCATOR_WEAK_MATCH")
    if separation < MIN_SCORE_SEPARATION:
        raise ShortReferencePlaybackLocatorError("LOCATOR_REPEATED_OR_AMBIGUOUS_MATCH")
    rate = int(reference["sample_rate_hz"])
    offset_ms = (search_start + best_index) * 1000.0 / rate
    matched = [window[0] + offset_ms, window[0] + offset_ms + reference_duration_ms]
    overlap = [max(interval[0], matched[0]), min(interval[1], matched[1])]
    if overlap[1] <= overlap[0]:
        raise ShortReferencePlaybackLocatorError("LOCATOR_MATCH_DOES_NOT_OVERLAP_TARGET")
    locator: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "LOCATED",
        "authority": "EVIDENCE_ONLY",
        "algorithm": {
            "name": algorithm_version,
            "backend": "numpy" if backend else "stdlib",
            "sample_rate_hz": rate,
            "mono_projection": "mean_channels_v1",
            "reference_complete": True,
            "max_reference_duration_ms": MAX_REFERENCE_DURATION_MS,
            "min_abs_ncc": MIN_ABS_NCC,
            "min_score_separation": MIN_SCORE_SEPARATION,
        },
        "reference_source_media": {
            "path": str(reference_source),
            "sha256": source_sha,
            "source_uri": source_uri.strip(),
        },
        "reference_audio": {
            "path": reference["path"],
            "sha256": reference["sha256"],
            "duration_ms": _round_ms(reference_duration_ms),
            "sample_rate_hz": rate,
            "channels": reference["channels"],
        },
        "target_audio": {
            "path": target["path"],
            "sha256": target["sha256"],
            "window_ms": [_round_ms(value) for value in window],
            "interval_ms": [_round_ms(value) for value in interval],
            "sample_rate_hz": rate,
            "channels": target["channels"],
        },
        "matched_target_interval_ms": [_round_ms(value) for value in matched],
        "matched_target_offset_ms": [_round_ms(offset_ms), _round_ms(offset_ms + reference_duration_ms)],
        "ncc": round(best_score, 9),
        "abs_ncc": round(best_abs, 9),
        "runner_up_abs_ncc": round(runner_up_abs, 9),
        "score_separation": round(separation, 9),
        "time_alignment_performed": False,
        "source_subtraction_authorized": False,
        "caption_dedup_authority": False,
        "source_role_authority": False,
        "mutation_authorized": False,
        "display_mutation_authorized": False,
        "publication_authorized": False,
        "locator_scope": ("TARGET_PARTIAL_COMPLETE_REFERENCE_ONLY" if legacy else
                          "CONTEXT_COMPLETE_REFERENCE_OVERLAPS_TARGET" if algorithm_version == CONTEXT_ALGORITHM_VERSION else
                          "CUE_OVERLAP_COMPLETE_REFERENCE_IN_CONTEXT"),
        "binding_context": _context_payload(binding_context),
    }
    if not legacy:
        locator["target_overlap_interval_ms"] = [_round_ms(value) for value in overlap]
    return locator


def locate_short_reference(
    *, reference_audio_path: str | Path, reference_audio_sha256: str,
    target_audio_path: str | Path, target_audio_sha256: str,
    target_audio_window_ms: Sequence[int | float], target_interval_ms: Sequence[int | float],
    reference_source_path: str | Path, reference_source_sha256: str,
    source_uri: str, binding_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Locate a complete reference in existing context, intersecting the cue.

    Caption segmentation need not contain an entire button recording. The
    full reference must still fit the supplied PCM context and match uniquely;
    the listener judges only the bound cue, never the surrounding match.
    """
    return _locate_short_reference(
        reference_audio_path=reference_audio_path, reference_audio_sha256=reference_audio_sha256,
        target_audio_path=target_audio_path, target_audio_sha256=target_audio_sha256,
        target_audio_window_ms=target_audio_window_ms, target_interval_ms=target_interval_ms,
        reference_source_path=reference_source_path, reference_source_sha256=reference_source_sha256,
        source_uri=source_uri, binding_context=binding_context, algorithm_version=ALGORITHM_VERSION,
    )


def validate_locator(
    locator: Mapping[str, object],
    *,
    reference_audio_path: str | Path,
    reference_audio_sha256: str,
    target_audio_path: str | Path,
    target_audio_sha256: str,
    target_audio_window_ms: Sequence[int | float],
    target_interval_ms: Sequence[int | float],
    reference_source_path: str | Path,
    reference_source_sha256: str,
    source_uri: str,
    binding_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Recompute and compare a persisted hint; caller input cannot supply scores."""
    if not isinstance(locator, Mapping) or locator.get("schema_version") != SCHEMA_VERSION:
        raise ShortReferencePlaybackLocatorError("LOCATOR_INVALID")
    algorithm = locator.get("algorithm")
    if not isinstance(algorithm, Mapping):
        raise ShortReferencePlaybackLocatorError("LOCATOR_ALGORITHM_UNSUPPORTED")
    expected = _locate_short_reference(
        reference_audio_path=reference_audio_path,
        reference_audio_sha256=reference_audio_sha256,
        target_audio_path=target_audio_path,
        target_audio_sha256=target_audio_sha256,
        target_audio_window_ms=target_audio_window_ms,
        target_interval_ms=target_interval_ms,
        reference_source_path=reference_source_path,
        reference_source_sha256=reference_source_sha256,
        source_uri=source_uri,
        binding_context=binding_context,
        algorithm_version=algorithm.get("name"),
    )
    if dict(locator) != expected:
        raise ShortReferencePlaybackLocatorError("LOCATOR_RESULT_DRIFT")
    return expected


__all__ = [
    "ALGORITHM_VERSION",
    "MAX_REFERENCE_DURATION_MS",
    "MIN_ABS_NCC",
    "MIN_SCORE_SEPARATION",
    "SCHEMA_VERSION",
    "ShortReferencePlaybackLocatorError",
    "locate_short_reference",
    "validate_locator",
]
