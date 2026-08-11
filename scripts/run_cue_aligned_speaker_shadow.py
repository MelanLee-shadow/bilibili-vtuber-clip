#!/usr/bin/env python3
"""Run hash-bound, cue-aligned CAM++ speaker scoring in shadow mode.

This is a development diagnostic, not a production selector.  It deliberately
keeps the current cross-session thresholds frozen while changing only the
analysis unit from overlapping fixed windows to ASR cue intervals.  Every
valid interval is scored for calibration evidence, while short, low-energy,
mixed, and inconsistent long cues abstain from the primary label.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.apply_subtitle_text_overrides import parse_srt
from scripts import build_reviewed_voiceprint_enrollment as reviewed_enrollment
from src.autoslice import host_occupancy as ho
from src.autoslice.campp_embed_once import (
    _build_embedding_similarity,
    _campp_runtime_fingerprint,
)
from src.autoslice.host_vocal_proof import (
    _load_campplus_pipeline,
    _sha256_directory,
    _validate_profile,
)
from src.autoslice.speaker_overlap_evidence import (
    MAX_SUBCUE_WINDOWS,
    MIN_SUBCUE_WINDOW_MS,
    plan_subcue_windows,
)
from src.autoslice.subtitle_validation import validate_srt_text


SCHEMA_VERSION = "cue-aligned-speaker-shadow.v1"
PURPOSE = "DEVELOPMENT_SHADOW_ONLY_NOT_SELECTION_OR_RELEASE_AUTHORITY"
MIN_CUE_MS = 1_500
MAX_CUE_MS = 4_000
MIN_HARD_LABEL_CUE_MS = 300
MIN_SPEECH_FRAME_RATIO = 0.50
MAX_LONG_CONSENSUS_WINDOWS = 8
_SPEAKER_PREFIX_RE = re.compile(r"^\s*\[[^\]\r\n]+\]\s*")

STRATEGY_MAX = "prototype_max_v1"
STRATEGY_MEDIAN = "prototype_median_shadow"
STRATEGY_TWO_VOTE = "two_prototype_consensus_shadow"
STRATEGY_ALL_VOTE = "all_prototype_consensus_shadow"
STRATEGY_SESSION_ANCHOR = "same_session_anchor_consensus_shadow"
STATIC_STRATEGIES = (
    STRATEGY_MAX,
    STRATEGY_MEDIAN,
    STRATEGY_TWO_VOTE,
    STRATEGY_ALL_VOTE,
)
STRATEGIES = STATIC_STRATEGIES + (STRATEGY_SESSION_ANCHOR,)
DESIGNATED_STRATEGY = STRATEGY_TWO_VOTE

ANCHOR_STATIC_MEDIAN_MIN = 0.60
ANCHOR_STATIC_MIN_PROTOTYPE = 0.55
ANCHOR_MIN_COUNT = 3
ANCHOR_MAX_COUNT = 4
ANCHOR_MIN_PAIRWISE_SIMILARITY = 0.55
SESSION_HOST_SIMILARITY_MIN = 0.68
SESSION_OTHER_SIMILARITY_MAX = 0.45


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


CONFIG = {
    "minimum_primary_cue_ms": MIN_CUE_MS,
    "minimum_hard_label_cue_ms": MIN_HARD_LABEL_CUE_MS,
    "long_cue_consensus_after_ms": MAX_CUE_MS,
    "minimum_energy_activity_ratio": MIN_SPEECH_FRAME_RATIO,
    "subcue_minimum_window_ms": MIN_SUBCUE_WINDOW_MS,
    "subcue_maximum_windows": MAX_SUBCUE_WINDOWS,
    "long_consensus_maximum_windows": MAX_LONG_CONSENSUS_WINDOWS,
    "host_similarity_min": ho.HOST_SIMILARITY_MIN,
    "other_similarity_max": ho.OTHER_SIMILARITY_MAX,
    "threshold_version": ho.THRESHOLD_VERSION,
    "threshold_calibration_status": "PROVISIONAL_CROSS_DOMAIN",
    "strategies": list(STRATEGIES),
    "designated_strategy": DESIGNATED_STRATEGY,
    "strategy_authority": {
        STRATEGY_MAX: "LEGACY_DIAGNOSTIC_ONLY",
        STRATEGY_MEDIAN: "SHADOW_CANDIDATE",
        STRATEGY_TWO_VOTE: "DESIGNATED_SHADOW_CANDIDATE",
        STRATEGY_ALL_VOTE: "SHADOW_CANDIDATE",
        STRATEGY_SESSION_ANCHOR: "DEVELOPMENT_TUNED_SHADOW_ONLY",
    },
    "bgm_assessed": False,
    "session_anchor": {
        "calibration_status": "DEVELOPMENT_TUNED_ON_2026_08_08_NOT_HOLDOUT",
        "static_median_min": ANCHOR_STATIC_MEDIAN_MIN,
        "static_min_prototype": ANCHOR_STATIC_MIN_PROTOTYPE,
        "minimum_anchor_count": ANCHOR_MIN_COUNT,
        "maximum_anchor_count": ANCHOR_MAX_COUNT,
        "minimum_pairwise_similarity": ANCHOR_MIN_PAIRWISE_SIMILARITY,
        "session_host_similarity_min": SESSION_HOST_SIMILARITY_MIN,
        "session_other_similarity_max": SESSION_OTHER_SIMILARITY_MAX,
    },
}
CONFIG_SHA256 = _canonical_sha256(CONFIG)


class CueAlignedShadowError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _normalized_sha256(value: object, *, label: str) -> str:
    normalized = str(value or "").lower().removeprefix("sha256:")
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise CueAlignedShadowError(f"{label} sha256 is malformed")
    return "sha256:" + normalized


def load_reviewed_development_enrollment(
    manifest_path: Path,
    *,
    target_session_id: str,
    target_candidate_id: str,
    target_media_sha256: str,
) -> tuple[dict[str, Path], dict[str, object]]:
    """Load an immutable cross-session bank without changing the v1 profile."""

    target_session_id = target_session_id.strip()
    if not target_session_id:
        raise CueAlignedShadowError(
            "target session id is required for reviewed development enrollment"
        )
    if not manifest_path.is_file():
        raise CueAlignedShadowError(f"reviewed development enrollment is missing: {manifest_path}")
    manifest_path = manifest_path.resolve()
    manifest_sha256 = _sha256(manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise CueAlignedShadowError("reviewed enrollment root must be an object")
    if (
        payload.get("schema_version") != reviewed_enrollment.SCHEMA_VERSION
        or payload.get("purpose") != reviewed_enrollment.PURPOSE
    ):
        raise CueAlignedShadowError("reviewed enrollment schema or purpose is invalid")
    deterministic_hash = _normalized_sha256(
        payload.get("deterministic_payload_sha256"),
        label="reviewed enrollment deterministic payload",
    )
    deterministic_payload = dict(payload)
    deterministic_payload.pop("deterministic_payload_sha256", None)
    if _canonical_sha256(deterministic_payload) != deterministic_hash:
        raise CueAlignedShadowError("reviewed enrollment deterministic payload drifted")
    source_session_id = str(payload.get("source_session_id") or "").strip()
    if not source_session_id or source_session_id == target_session_id:
        raise CueAlignedShadowError("reviewed enrollment must come from a different named session")
    source_candidate_id = str(payload.get("candidate_id") or "").strip()
    if not target_candidate_id.strip() or source_candidate_id == target_candidate_id:
        raise CueAlignedShadowError("reviewed enrollment must come from a different candidate")
    summary = payload.get("summary")
    if (
        not isinstance(summary, Mapping)
        or summary.get("development_shadow_ready") is not True
        or summary.get("production_enrollment_ready") is not False
    ):
        raise CueAlignedShadowError("reviewed enrollment authority flags are invalid")
    bindings = payload.get("bindings")
    if not isinstance(bindings, Mapping):
        raise CueAlignedShadowError("reviewed enrollment bindings are missing")
    root_text = str(bindings.get("enrollment_root_path") or "")
    root = Path(root_text).resolve() if root_text else None
    if root is None or not root.is_dir():
        raise CueAlignedShadowError("reviewed enrollment root directory is missing")
    for path_key, hash_key, label in (
        ("source_media_path", "source_media_sha256", "reviewed source media"),
        (
            "text_final_srt_path",
            "text_final_srt_sha256",
            "reviewed text-final SRT",
        ),
        (
            "reviewed_override_path",
            "reviewed_override_sha256",
            "reviewed speaker override",
        ),
    ):
        source_path = Path(str(bindings.get(path_key) or ""))
        expected_sha256 = _normalized_sha256(bindings.get(hash_key), label=label)
        if not source_path.is_file() or _sha256(source_path) != expected_sha256:
            raise CueAlignedShadowError(f"{label} is missing or drifted")
        if path_key == "source_media_path" and expected_sha256 == _normalized_sha256(
            target_media_sha256, label="target candidate media"
        ):
            raise CueAlignedShadowError("reviewed enrollment source media equals target media")
    raw_clips = payload.get("clips")
    if (
        not isinstance(raw_clips, list)
        or len(raw_clips) < reviewed_enrollment.MINIMUM_DEVELOPMENT_CLIPS
    ):
        raise CueAlignedShadowError("reviewed enrollment has too few clips")
    prototypes: dict[str, Path] = {}
    clips: list[dict[str, object]] = []
    for raw_clip in raw_clips:
        if not isinstance(raw_clip, Mapping):
            raise CueAlignedShadowError("reviewed enrollment clip is malformed")
        clip_id = str(raw_clip.get("id") or "").strip()
        relative_path = Path(str(raw_clip.get("relative_path") or ""))
        if not clip_id or clip_id in prototypes or relative_path.is_absolute():
            raise CueAlignedShadowError("reviewed enrollment clip identity or path is invalid")
        clip_path = (root / relative_path).resolve()
        try:
            clip_path.relative_to(root)
        except ValueError as exc:
            raise CueAlignedShadowError("reviewed enrollment clip escapes its bound root") from exc
        expected_sha256 = _normalized_sha256(
            raw_clip.get("sha256"), label=f"reviewed enrollment clip {clip_id}"
        )
        if not clip_path.is_file() or _sha256(clip_path) != expected_sha256:
            raise CueAlignedShadowError(
                f"reviewed enrollment clip is missing or drifted: {clip_id}"
            )
        with wave.open(str(clip_path), "rb") as handle:
            if (
                handle.getnchannels() != 1
                or handle.getsampwidth() != 2
                or handle.getframerate() != ho.SAMPLE_RATE_HZ
            ):
                raise CueAlignedShadowError(
                    f"reviewed enrollment clip format is invalid: {clip_id}"
                )
            duration_ms = handle.getnframes() * 1000 // handle.getframerate()
        stored_duration_ms = raw_clip.get("duration_ms")
        if (
            isinstance(stored_duration_ms, bool)
            or not isinstance(stored_duration_ms, int)
            or stored_duration_ms != duration_ms
        ):
            raise CueAlignedShadowError(f"reviewed enrollment clip duration drifted: {clip_id}")
        prototypes[clip_id] = clip_path
        clips.append(
            {
                "id": clip_id,
                "relative_path": str(relative_path),
                "sha256": expected_sha256,
                "duration_ms": duration_ms,
            }
        )
    return prototypes, {
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "deterministic_payload_sha256": deterministic_hash,
        "source_session_id": source_session_id,
        "target_session_id": target_session_id,
        "candidate_id": source_candidate_id,
        "selection_policy": (
            payload.get("selection", {}).get("policy")
            if isinstance(payload.get("selection"), Mapping)
            else None
        ),
        "prototype_count": len(clips),
        "total_duration_ms": sum(int(clip["duration_ms"]) for clip in clips),
        "clips": clips,
        "production_profile_unchanged": True,
        "promotion_authority": False,
    }


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
        raise CueAlignedShadowError(f"ffprobe failed for {path}: {completed.stderr[-300:]}")
    try:
        duration = int(float(completed.stdout.strip()) * 1000)
    except ValueError as exc:
        raise CueAlignedShadowError(f"invalid ffprobe duration for {path}") from exc
    if duration <= 0:
        raise CueAlignedShadowError(f"media has no positive duration: {path}")
    return duration


def _clock_ms(value: str) -> int:
    try:
        hours, minutes, remainder = value.split(":")
        seconds, milliseconds = remainder.replace(".", ",").split(",")
        return (
            int(hours) * 3_600_000
            + int(minutes) * 60_000
            + int(seconds) * 1_000
            + int(milliseconds)
        )
    except (TypeError, ValueError) as exc:
        raise CueAlignedShadowError(f"invalid SRT timestamp: {value!r}") from exc


def classify_prototype_scores(scores: Mapping[str, float], *, strategy: str) -> str:
    """Apply frozen thresholds with increasingly conservative aggregation."""

    values = [float(value) for _, value in sorted(scores.items())]
    if not values or any(not math.isfinite(value) for value in values):
        raise CueAlignedShadowError("prototype scores must be finite and non-empty")
    if strategy == STRATEGY_MAX:
        return ho.classify_score(max(values))
    if strategy == STRATEGY_MEDIAN:
        ordered = sorted(values)
        middle = len(ordered) // 2
        median = (
            ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
        )
        return ho.classify_score(median)
    if strategy == STRATEGY_TWO_VOTE:
        if sum(value >= ho.HOST_SIMILARITY_MIN for value in values) >= 2:
            return ho.LABEL_HOST
        if all(value <= ho.OTHER_SIMILARITY_MAX for value in values):
            return ho.LABEL_OTHER
        return ho.LABEL_UNKNOWN
    if strategy == STRATEGY_ALL_VOTE:
        if all(value >= ho.HOST_SIMILARITY_MIN for value in values):
            return ho.LABEL_HOST
        if all(value <= ho.OTHER_SIMILARITY_MAX for value in values):
            return ho.LABEL_OTHER
        return ho.LABEL_UNKNOWN
    raise CueAlignedShadowError(f"unknown cue strategy: {strategy}")


def classify_session_anchor_scores(scores: Mapping[str, float]) -> str:
    values = [float(value) for _, value in sorted(scores.items())]
    if len(values) < 2 or any(not math.isfinite(value) for value in values):
        raise CueAlignedShadowError("session anchor scores are invalid")
    required_host_votes = max(2, math.ceil(len(values) / 2))
    if sum(value >= SESSION_HOST_SIMILARITY_MIN for value in values) >= required_host_votes:
        return ho.LABEL_HOST
    if all(value <= SESSION_OTHER_SIMILARITY_MAX for value in values):
        return ho.LABEL_OTHER
    return ho.LABEL_UNKNOWN


def _abstention_reason(
    *, start_ms: int, end_ms: int, media_duration_ms: int, speech_ratio: float
) -> str | None:
    duration_ms = end_ms - start_ms
    if start_ms < 0 or end_ms > media_duration_ms or duration_ms <= 0:
        return "CUE_OUTSIDE_MEDIA"
    if duration_ms < MIN_HARD_LABEL_CUE_MS:
        return "CUE_TOO_SHORT_FOR_HARD_LABEL"
    if speech_ratio < MIN_SPEECH_FRAME_RATIO:
        return "INSUFFICIENT_ENERGY_ACTIVITY"
    return None


def plan_long_consensus_windows(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Split long cues into the fewest <=4 s windows, bounded at eight."""

    duration_ms = end_ms - start_ms
    if duration_ms <= MAX_CUE_MS:
        return []
    count = math.ceil(duration_ms / MAX_CUE_MS)
    if count > MAX_LONG_CONSENSUS_WINDOWS:
        return []
    edges = [start_ms + duration_ms * step // count for step in range(count + 1)]
    return [(edges[index], edges[index + 1]) for index in range(count)]


def planned_subwindows(start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    if end_ms - start_ms > MAX_CUE_MS:
        return plan_long_consensus_windows(start_ms, end_ms)
    return plan_subcue_windows(start_ms, end_ms)


def _score_summary(
    scores: Mapping[str, float],
) -> dict[str, float | int] | None:
    if not scores:
        return None
    values = sorted(float(value) for value in scores.values())
    middle = len(values) // 2
    median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
    return {
        "min": round(values[0], 5),
        "median": round(median, 5),
        "max": round(values[-1], 5),
        "second_highest": round(values[-2] if len(values) > 1 else values[-1], 5),
        "spread": round(values[-1] - values[0], 5),
        "host_vote_count": sum(value >= ho.HOST_SIMILARITY_MIN for value in values),
        "all_other_votes": int(all(value <= ho.OTHER_SIMILARITY_MAX for value in values)),
    }


def _primary_prediction(
    *,
    whole_label: str,
    subwindow_labels: Sequence[str],
    duration_ms: int,
    base_abstention_reason: str | None,
    allow_short_consensus: bool = False,
) -> tuple[str, str | None]:
    """Fuse whole-cue and subwindows; disagreements only lower confidence."""

    if base_abstention_reason is not None:
        return ho.LABEL_UNKNOWN, base_abstention_reason
    if duration_ms < MIN_CUE_MS:
        if not allow_short_consensus:
            return ho.LABEL_UNKNOWN, "SHORT_CUE_REQUIRES_PROTOTYPE_CONSENSUS"
        if whole_label not in {ho.LABEL_HOST, ho.LABEL_OTHER}:
            return ho.LABEL_UNKNOWN, "SHORT_CUE_CONSENSUS_FAILED"
    known = {label for label in subwindow_labels if label in {ho.LABEL_HOST, ho.LABEL_OTHER}}
    if len(known) > 1:
        return ho.LABEL_UNKNOWN, "SUBCUE_SPEAKER_CONFLICT"
    if whole_label in {ho.LABEL_HOST, ho.LABEL_OTHER} and known and whole_label not in known:
        return ho.LABEL_UNKNOWN, "WHOLE_SUBCUE_DISAGREEMENT"
    if duration_ms > MAX_CUE_MS:
        if (
            whole_label in {ho.LABEL_HOST, ho.LABEL_OTHER}
            and subwindow_labels
            and all(label == whole_label for label in subwindow_labels)
        ):
            return whole_label, None
        return ho.LABEL_UNKNOWN, "LONG_CUE_CONSENSUS_FAILED"
    return whole_label, None if whole_label != ho.LABEL_UNKNOWN else "SCORE_AMBIGUITY"


def replay_unit_prediction(
    unit: Mapping[str, object],
    *,
    strategy: str,
    decoded_duration_ms: int,
) -> tuple[str, str | None]:
    """Recompute one persisted primary prediction from its bound evidence."""

    start_ms = unit.get("start_ms")
    end_ms = unit.get("end_ms")
    energy_ratio = unit.get("energy_activity_ratio")
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or isinstance(energy_ratio, bool)
        or not isinstance(energy_ratio, (int, float))
        or not math.isfinite(float(energy_ratio))
    ):
        raise CueAlignedShadowError("cue replay fields are malformed")
    base_reason = _abstention_reason(
        start_ms=start_ms,
        end_ms=end_ms,
        media_duration_ms=decoded_duration_ms,
        speech_ratio=float(energy_ratio),
    )
    if strategy == STRATEGY_SESSION_ANCHOR:
        session_scores = unit.get("session_anchor_scores")
        session_status = str(unit.get("session_anchor_status") or "")
        if session_status != "READY":
            prediction, reason = (
                ho.LABEL_UNKNOWN,
                "SESSION_ANCHOR_BANK_UNAVAILABLE",
            )
        elif not isinstance(session_scores, Mapping) or not session_scores:
            raise CueAlignedShadowError("ready session-anchor cue lacks scores")
        elif base_reason is not None:
            prediction, reason = ho.LABEL_UNKNOWN, base_reason
        else:
            prediction = classify_session_anchor_scores(
                {str(key): float(value) for key, value in session_scores.items()}
            )
            reason = None if prediction != ho.LABEL_UNKNOWN else "SESSION_SCORE_AMBIGUITY"
        persisted = unit.get("predictions")
        persisted_reasons = unit.get("primary_abstention_reasons")
        if not isinstance(persisted, Mapping) or persisted.get(strategy) != prediction:
            raise CueAlignedShadowError("persisted session-anchor prediction drifted")
        if not isinstance(persisted_reasons, Mapping) or persisted_reasons.get(strategy) != reason:
            raise CueAlignedShadowError("persisted session-anchor abstention reason drifted")
        return prediction, reason

    scores = unit.get("prototype_scores")
    if not isinstance(scores, Mapping) or not scores:
        if base_reason != "CUE_OUTSIDE_MEDIA":
            raise CueAlignedShadowError("in-bounds cue lacks prototype scores")
        return ho.LABEL_UNKNOWN, base_reason
    normalized_scores = {str(key): float(value) for key, value in scores.items()}
    whole_label = classify_prototype_scores(normalized_scores, strategy=strategy)
    raw_predictions = unit.get("raw_predictions")
    if not isinstance(raw_predictions, Mapping) or raw_predictions.get(strategy) != whole_label:
        raise CueAlignedShadowError("persisted raw cue prediction drifted")

    raw_subwindows = unit.get("subwindows")
    if not isinstance(raw_subwindows, list):
        raise CueAlignedShadowError("cue subwindows are malformed")
    subwindow_labels: list[str] = []
    expected_windows = planned_subwindows(start_ms, end_ms)
    actual_windows: list[tuple[int, int]] = []
    for raw_window in raw_subwindows:
        if not isinstance(raw_window, Mapping):
            raise CueAlignedShadowError("cue subwindow row is malformed")
        raw_scores = raw_window.get("prototype_scores")
        if not isinstance(raw_scores, Mapping) or not raw_scores:
            raise CueAlignedShadowError("cue subwindow lacks prototype scores")
        window_start = raw_window.get("start_ms")
        window_end = raw_window.get("end_ms")
        if (
            isinstance(window_start, bool)
            or not isinstance(window_start, int)
            or isinstance(window_end, bool)
            or not isinstance(window_end, int)
        ):
            raise CueAlignedShadowError("cue subwindow bounds are malformed")
        actual_windows.append((window_start, window_end))
        label = classify_prototype_scores(
            {str(key): float(value) for key, value in raw_scores.items()},
            strategy=strategy,
        )
        persisted_raw = raw_window.get("raw_predictions")
        if not isinstance(persisted_raw, Mapping) or persisted_raw.get(strategy) != label:
            raise CueAlignedShadowError("persisted subwindow prediction drifted")
        subwindow_labels.append(label)
    if actual_windows != expected_windows:
        raise CueAlignedShadowError("persisted subwindow plan drifted")
    prediction, reason = _primary_prediction(
        whole_label=whole_label,
        subwindow_labels=subwindow_labels,
        duration_ms=end_ms - start_ms,
        base_abstention_reason=base_reason,
        allow_short_consensus=strategy in {STRATEGY_TWO_VOTE, STRATEGY_ALL_VOTE},
    )
    persisted = unit.get("predictions")
    persisted_reasons = unit.get("primary_abstention_reasons")
    if not isinstance(persisted, Mapping) or persisted.get(strategy) != prediction:
        raise CueAlignedShadowError("persisted primary prediction drifted")
    if not isinstance(persisted_reasons, Mapping) or persisted_reasons.get(strategy) != reason:
        raise CueAlignedShadowError("persisted primary abstention reason drifted")
    return prediction, reason


def _score_cue_units(
    *,
    cues: Sequence[object],
    samples: Sequence[int],
    frame_flags: Sequence[bool],
    decoded_duration_ms: int,
    prototypes: Mapping[str, Path],
    development_prototypes: Mapping[str, Path],
    similarity: Callable[[Path, Path], float],
    work_dir: Path,
) -> list[dict[str, object]]:
    """Score immutable cue and subcue audio into replayable evidence rows."""

    units: list[dict[str, object]] = []
    for cue in cues:
        start_ms = _clock_ms(cue.start)
        end_ms = _clock_ms(cue.end)
        first_frame = max(0, start_ms // ho.VAD_FRAME_MS)
        last_frame = max(first_frame, end_ms // ho.VAD_FRAME_MS)
        cue_flags = frame_flags[first_frame:last_frame]
        speech_ratio = sum(1 for flag in cue_flags if flag) / len(cue_flags) if cue_flags else 0.0
        base_reason = _abstention_reason(
            start_ms=start_ms,
            end_ms=end_ms,
            media_duration_ms=decoded_duration_ms,
            speech_ratio=speech_ratio,
        )
        scores: dict[str, float] = {}
        development_scores: dict[str, float] = {}
        audio_sha256 = ""
        subwindows: list[dict[str, object]] = []
        if 0 <= start_ms < end_ms <= decoded_duration_ms:
            cue_samples = ho._slice(
                samples,
                span_start_ms=0,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            cue_path = ho.write_window_wav(
                cue_samples,
                work_dir / "cue-wavs" / f"cue-{cue.source_index:04d}.wav",
            )
            audio_sha256 = _sha256(cue_path)
            scores = {
                name: round(float(similarity(cue_path, prototype)), 5)
                for name, prototype in sorted(prototypes.items())
            }
            development_scores = {
                name: round(float(similarity(cue_path, prototype)), 5)
                for name, prototype in sorted(development_prototypes.items())
            }
            for window_index, (window_start, window_end) in enumerate(
                planned_subwindows(start_ms, end_ms), start=1
            ):
                subcue_samples = ho._slice(
                    samples,
                    span_start_ms=0,
                    start_ms=window_start,
                    end_ms=window_end,
                )
                subcue_path = ho.write_window_wav(
                    subcue_samples,
                    work_dir
                    / "subcue-wavs"
                    / f"cue-{cue.source_index:04d}-w{window_index:02d}.wav",
                )
                subcue_scores = {
                    name: round(float(similarity(subcue_path, prototype)), 5)
                    for name, prototype in sorted(prototypes.items())
                }
                subwindows.append(
                    {
                        "window": window_index,
                        "start_ms": window_start,
                        "end_ms": window_end,
                        "audio_sha256": _sha256(subcue_path),
                        "prototype_scores": subcue_scores,
                        "raw_predictions": {
                            strategy: classify_prototype_scores(subcue_scores, strategy=strategy)
                            for strategy in STATIC_STRATEGIES
                        },
                    }
                )
        raw_predictions = {
            strategy: (
                classify_prototype_scores(scores, strategy=strategy) if scores else ho.LABEL_UNKNOWN
            )
            for strategy in STATIC_STRATEGIES
        }
        predictions: dict[str, str] = {}
        abstention_reasons: dict[str, str | None] = {}
        for strategy in STATIC_STRATEGIES:
            prediction, reason = _primary_prediction(
                whole_label=raw_predictions[strategy],
                subwindow_labels=[
                    str(window["raw_predictions"][strategy]) for window in subwindows
                ],
                duration_ms=end_ms - start_ms,
                base_abstention_reason=base_reason,
                allow_short_consensus=strategy in {STRATEGY_TWO_VOTE, STRATEGY_ALL_VOTE},
            )
            predictions[strategy] = prediction
            abstention_reasons[strategy] = reason
        quality_flags: list[str] = []
        if end_ms - start_ms < MIN_CUE_MS:
            quality_flags.append("SHORT_CUE")
        elif end_ms - start_ms > MAX_CUE_MS:
            quality_flags.append("LONG_CUE")
        if speech_ratio < MIN_SPEECH_FRAME_RATIO:
            quality_flags.append("LOW_ENERGY_ACTIVITY")
        if any(reason == "SUBCUE_SPEAKER_CONFLICT" for reason in abstention_reasons.values()):
            quality_flags.append("SUBCUE_SPEAKER_CONFLICT")
        units.append(
            {
                "source_cue": cue.source_index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": end_ms - start_ms,
                "duration_stratum": (
                    "SHORT"
                    if end_ms - start_ms < MIN_CUE_MS
                    else "LONG"
                    if end_ms - start_ms > MAX_CUE_MS
                    else "STANDARD"
                ),
                "text_sha256": "sha256:" + hashlib.sha256(cue.text.encode("utf-8")).hexdigest(),
                "content_text_sha256": "sha256:"
                + hashlib.sha256(_SPEAKER_PREFIX_RE.sub("", cue.text).encode("utf-8")).hexdigest(),
                "energy_activity_ratio": round(speech_ratio, 6),
                "energy_activity_is_not_speech_or_bgm_classification": True,
                "bgm_assessed": False,
                "analysis_status": "SCORED_EVIDENCE" if scores else "UNSCORABLE",
                "primary_abstention_reasons": abstention_reasons,
                "audio_sha256": audio_sha256,
                "prototype_scores": scores,
                "score_summary": _score_summary(scores),
                "reviewed_development_enrollment_scores": development_scores,
                "reviewed_development_enrollment_score_summary": _score_summary(development_scores),
                "quality_flags": quality_flags,
                "raw_predictions": raw_predictions,
                "subwindows": subwindows,
                "predictions": predictions,
            }
        )
    return units


def _attach_session_anchor_predictions(
    *,
    units: Sequence[dict[str, object]],
    profile: Mapping[str, object],
    similarity: Callable[[Path, Path], float],
    work_dir: Path,
    decoded_duration_ms: int,
) -> dict[str, object]:
    """Attach same-session diagnostics without granting promotion authority."""

    talk_policy = profile.get("talk_speaker_policy")
    if not isinstance(talk_policy, Mapping):
        raise CueAlignedShadowError("voiceprint profile has no talk speaker policy")
    try:
        bound_session_host_min = float(talk_policy["host_session_seed_min"])
        bound_session_other_max = float(talk_policy["guest_session_similarity_max"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CueAlignedShadowError("voiceprint talk speaker policy is malformed") from exc
    if (
        bound_session_host_min != SESSION_HOST_SIMILARITY_MIN
        or bound_session_other_max != SESSION_OTHER_SIMILARITY_MAX
    ):
        raise CueAlignedShadowError("session-anchor thresholds do not match the bound talk policy")

    cue_paths_by_source = {
        int(path.stem.removeprefix("cue-")): path
        for path in (work_dir / "cue-wavs").glob("cue-*.wav")
    }
    anchor_candidates: list[dict[str, object]] = []
    for unit in units:
        summary = unit.get("score_summary")
        if (
            isinstance(summary, Mapping)
            and int(unit["duration_ms"]) >= MIN_CUE_MS
            and float(summary["median"]) >= ANCHOR_STATIC_MEDIAN_MIN
            and float(summary["min"]) >= ANCHOR_STATIC_MIN_PROTOTYPE
            and int(unit["source_cue"]) in cue_paths_by_source
        ):
            anchor_candidates.append(unit)
    anchor_candidates.sort(
        key=lambda unit: (
            -float(unit["score_summary"]["median"]),
            int(unit["source_cue"]),
        )
    )
    selected_anchors = anchor_candidates[:ANCHOR_MAX_COUNT]
    pairwise_rows: list[dict[str, object]] = []
    for left_index, left in enumerate(selected_anchors):
        for right in selected_anchors[left_index + 1 :]:
            score = float(
                similarity(
                    cue_paths_by_source[int(left["source_cue"])],
                    cue_paths_by_source[int(right["source_cue"])],
                )
            )
            pairwise_rows.append(
                {
                    "left_source_cue": left["source_cue"],
                    "right_source_cue": right["source_cue"],
                    "similarity": round(score, 5),
                }
            )
    anchor_bank_ready = bool(
        len(selected_anchors) >= ANCHOR_MIN_COUNT
        and pairwise_rows
        and min(float(row["similarity"]) for row in pairwise_rows) >= ANCHOR_MIN_PAIRWISE_SIMILARITY
    )
    anchor_paths = {
        str(unit["source_cue"]): cue_paths_by_source[int(unit["source_cue"])]
        for unit in selected_anchors
    }
    for unit in units:
        source_cue = int(unit["source_cue"])
        target = cue_paths_by_source.get(source_cue)
        session_scores: dict[str, float] = {}
        if anchor_bank_ready and target is not None:
            session_scores = {
                anchor_id: round(float(similarity(target, anchor_path)), 5)
                for anchor_id, anchor_path in sorted(anchor_paths.items())
                if int(anchor_id) != source_cue
            }
        unit["session_anchor_status"] = "READY" if anchor_bank_ready else "UNAVAILABLE"
        unit["session_anchor_scores"] = session_scores
        unit["session_anchor_score_summary"] = _score_summary(session_scores)
        base_reason = _abstention_reason(
            start_ms=int(unit["start_ms"]),
            end_ms=int(unit["end_ms"]),
            media_duration_ms=decoded_duration_ms,
            speech_ratio=float(unit["energy_activity_ratio"]),
        )
        if not anchor_bank_ready:
            session_prediction = ho.LABEL_UNKNOWN
            session_reason = "SESSION_ANCHOR_BANK_UNAVAILABLE"
        elif base_reason is not None:
            session_prediction = ho.LABEL_UNKNOWN
            session_reason = base_reason
        else:
            session_prediction = classify_session_anchor_scores(session_scores)
            session_reason = (
                None if session_prediction != ho.LABEL_UNKNOWN else "SESSION_SCORE_AMBIGUITY"
            )
        unit["predictions"][STRATEGY_SESSION_ANCHOR] = session_prediction
        unit["primary_abstention_reasons"][STRATEGY_SESSION_ANCHOR] = session_reason

    return {
        "status": "READY" if anchor_bank_ready else "UNAVAILABLE",
        "candidate_source_cues": [unit["source_cue"] for unit in anchor_candidates],
        "selected_source_cues": [unit["source_cue"] for unit in selected_anchors],
        "pairwise": pairwise_rows,
        "reason_codes": (
            [] if anchor_bank_ready else ["INSUFFICIENT_OR_INCOHERENT_STATIC_HOST_ANCHORS"]
        ),
    }


def run_shadow(
    *,
    candidate_id: str,
    media_path: Path,
    srt_path: Path,
    profile_path: Path,
    reference_dir: Path,
    model_dir: Path,
    work_dir: Path,
    development_enrollment_manifest_path: Path | None = None,
    target_session_id: str = "",
) -> dict[str, object]:
    """Score every cue once and emit a replayable diagnostic receipt."""

    for label, path in {
        "media": media_path,
        "SRT": srt_path,
        "profile": profile_path,
    }.items():
        if not path.is_file():
            raise CueAlignedShadowError(f"{label} is missing: {path}")
    if not model_dir.is_dir() or not reference_dir.is_dir():
        raise CueAlignedShadowError("model and reference directories must exist")

    media_path = media_path.resolve()
    srt_path = srt_path.resolve()
    profile_path = profile_path.resolve()
    reference_dir = reference_dir.resolve()
    model_dir = model_dir.resolve()
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    media_sha256 = _sha256(media_path)
    srt_sha256 = _sha256(srt_path)
    profile_sha256 = _sha256(profile_path)
    media_stat = media_path.stat()
    srt_stat = srt_path.stat()
    container_duration_ms = _duration_ms(media_path)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(profile, Mapping):
        raise CueAlignedShadowError("voiceprint profile root must be an object")
    model_info, expected_references = _validate_profile(profile)
    prototypes, enrollment = ho.enrollment_prototypes(profile_path, reference_dir)
    development_prototypes: dict[str, Path] = {}
    development_enrollment: dict[str, object] | None = None
    if development_enrollment_manifest_path is not None:
        development_prototypes, development_enrollment = load_reviewed_development_enrollment(
            development_enrollment_manifest_path,
            target_session_id=target_session_id,
            target_candidate_id=candidate_id,
            target_media_sha256=media_sha256,
        )
    model_tree_sha256 = _sha256_directory(model_dir)
    if model_tree_sha256 != model_info["tree_sha256"]:
        raise CueAlignedShadowError("CAM++ model tree does not match profile")

    load_started = time.monotonic()
    verifier = _load_campplus_pipeline(model_dir)
    runtime_fingerprint = _campp_runtime_fingerprint(verifier)
    similarity = _build_embedding_similarity(
        verifier=verifier,
        model_hash=model_tree_sha256,
        work_dir=work_dir / "embedding-cache",
    )
    model_load_seconds = time.monotonic() - load_started

    pcm_path = ho.extract_span_wav(
        media_path,
        start_ms=0,
        end_ms=container_duration_ms,
        output_path=work_dir / "media-16k-mono.wav",
    )
    samples = ho.read_wav_samples(pcm_path)
    decoded_duration_ms = len(samples) * 1000 // ho.SAMPLE_RATE_HZ
    if decoded_duration_ms <= 0:
        raise CueAlignedShadowError("decoded PCM has no positive duration")
    srt_text = srt_path.read_text(encoding="utf-8")
    srt_verdict = validate_srt_text(
        srt_text,
        media_duration_ms=decoded_duration_ms,
        min_cue_ms=1,
        media_tail_tolerance_ms=0,
    )
    if srt_verdict["status"] != "PASS":
        codes = [str(row.get("code")) for row in srt_verdict["errors"]]
        raise CueAlignedShadowError("SRT failed strict timeline validation: " + ",".join(codes[:8]))
    cues = parse_srt(srt_path)
    if len(cues) != srt_verdict["cue_count"]:
        raise CueAlignedShadowError("strict SRT parser and cue parser disagree")
    frame_length = ho.VAD_FRAME_MS * ho.SAMPLE_RATE_HZ // 1000
    frame_flags = ho.speech_frame_flags(ho.frame_rms(samples, frame_length=frame_length))

    scoring_started = time.monotonic()
    units = _score_cue_units(
        cues=cues,
        samples=samples,
        frame_flags=frame_flags,
        decoded_duration_ms=decoded_duration_ms,
        prototypes=prototypes,
        development_prototypes=development_prototypes,
        similarity=similarity,
        work_dir=work_dir,
    )

    session_anchor_receipt = _attach_session_anchor_predictions(
        units=units,
        profile=profile,
        similarity=similarity,
        work_dir=work_dir,
        decoded_duration_ms=decoded_duration_ms,
    )

    current_media_stat = media_path.stat()
    current_srt_stat = srt_path.stat()
    if (current_media_stat.st_size, current_media_stat.st_mtime_ns) != (
        media_stat.st_size,
        media_stat.st_mtime_ns,
    ) or _sha256(media_path) != media_sha256:
        raise CueAlignedShadowError("media drifted during cue-aligned analysis")
    if (current_srt_stat.st_size, current_srt_stat.st_mtime_ns) != (
        srt_stat.st_size,
        srt_stat.st_mtime_ns,
    ) or _sha256(srt_path) != srt_sha256:
        raise CueAlignedShadowError("SRT drifted during cue-aligned analysis")
    if _sha256(profile_path) != profile_sha256:
        raise CueAlignedShadowError("profile drifted during cue-aligned analysis")
    if _sha256_directory(model_dir) != model_tree_sha256:
        raise CueAlignedShadowError("model tree drifted during cue-aligned analysis")

    ending_prototypes, ending_enrollment = ho.enrollment_prototypes(profile_path, reference_dir)
    if ending_enrollment != enrollment or ending_prototypes != prototypes:
        raise CueAlignedShadowError("enrollment references drifted during analysis")
    for reference in expected_references:
        if (
            _sha256(reference_dir / reference["filename"]).removeprefix("sha256:")
            != reference["sha256"]
        ):
            raise CueAlignedShadowError(f"enrollment reference drifted: {reference['id']}")
    if development_enrollment_manifest_path is not None:
        ending_development_prototypes, ending_development_enrollment = (
            load_reviewed_development_enrollment(
                development_enrollment_manifest_path,
                target_session_id=target_session_id,
                target_candidate_id=candidate_id,
                target_media_sha256=media_sha256,
            )
        )
        if (
            ending_development_prototypes != development_prototypes
            or ending_development_enrollment != development_enrollment
        ):
            raise CueAlignedShadowError("reviewed development enrollment drifted during analysis")

    counts_by_strategy = {
        strategy: {
            label: sum(unit["predictions"][strategy] == label for unit in units)
            for label in (ho.LABEL_HOST, ho.LABEL_OTHER, ho.LABEL_UNKNOWN)
        }
        for strategy in STRATEGIES
    }
    deterministic_payload = {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "candidate_id": candidate_id,
        "bindings": {
            "media_path": str(media_path),
            "media_sha256": media_sha256,
            "container_duration_ms": container_duration_ms,
            "decoded_pcm_duration_ms": decoded_duration_ms,
            "decoded_pcm_sha256": _sha256(pcm_path),
            "srt_path": str(srt_path),
            "srt_sha256": srt_sha256,
            "profile_path": str(profile_path),
            "profile_sha256": profile_sha256,
            "reference_dir": str(reference_dir),
            "model_dir": str(model_dir),
            "model_tree_sha256": "sha256:" + model_tree_sha256,
            "campp_runtime_fingerprint": "sha256:" + runtime_fingerprint,
            "runner_sha256": _sha256(Path(__file__).resolve()),
            "reviewed_development_enrollment_manifest_path": (
                str(development_enrollment_manifest_path.resolve())
                if development_enrollment_manifest_path is not None
                else None
            ),
            "reviewed_development_enrollment_manifest_sha256": (
                development_enrollment["manifest_sha256"]
                if development_enrollment is not None
                else None
            ),
        },
        "config": dict(CONFIG),
        "config_sha256": CONFIG_SHA256,
        "provisional_calibration": True,
        "enrollment": enrollment,
        "reviewed_development_enrollment": development_enrollment,
        "cue_count": len(units),
        "scored_cue_count": sum(unit["analysis_status"] == "SCORED_EVIDENCE" for unit in units),
        "reviewed_development_enrollment_scored_cue_count": sum(
            bool(unit["reviewed_development_enrollment_scores"]) for unit in units
        ),
        "srt_validation": srt_verdict,
        "session_anchor": session_anchor_receipt,
        "summary": {
            "counts_by_strategy": counts_by_strategy,
            "short_cue_count": sum(unit["duration_stratum"] == "SHORT" for unit in units),
            "standard_cue_count": sum(unit["duration_stratum"] == "STANDARD" for unit in units),
            "long_cue_count": sum(unit["duration_stratum"] == "LONG" for unit in units),
            "low_energy_activity_count": sum(
                "LOW_ENERGY_ACTIVITY" in unit["quality_flags"] for unit in units
            ),
            "subcue_conflict_count": sum(
                "SUBCUE_SPEAKER_CONFLICT" in unit["quality_flags"] for unit in units
            ),
        },
        "units": units,
    }
    return {
        **deterministic_payload,
        "deterministic_payload_sha256": _canonical_sha256(deterministic_payload),
        "model_load_seconds": round(model_load_seconds, 3),
        "scoring_seconds": round(time.monotonic() - scoring_started, 3),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--development-enrollment-manifest", type=Path)
    parser.add_argument("--target-session-id", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    result = run_shadow(
        candidate_id=args.candidate_id,
        media_path=args.media,
        srt_path=args.srt,
        profile_path=args.profile,
        reference_dir=args.reference_dir,
        model_dir=args.model_dir,
        work_dir=args.work_dir,
        development_enrollment_manifest_path=(args.development_enrollment_manifest),
        target_session_id=args.target_session_id,
    )
    result["wall_seconds"] = round(time.monotonic() - started, 3)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
