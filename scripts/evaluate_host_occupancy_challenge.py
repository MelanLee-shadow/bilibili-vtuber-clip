#!/usr/bin/env python3
"""Evaluate host-occupancy window receipts against reviewed speaker truth.

This evaluator is deliberately diagnostic.  It compares several frozen score
aggregation strategies but does not choose or deploy one, and it never treats
machine-only cue labels as human truth.  A development set and a locked
cross-session holdout must remain separate before thresholds can be promoted
from provisional status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice import host_occupancy as ho
from src.autoslice.channel_profile import load_channel_profile
from scripts import run_cue_aligned_speaker_shadow as cue_shadow


SCHEMA_VERSION = "host-occupancy-challenge-evaluation.v1"
TRUTH_MIN_CELL_COVERAGE = 0.85
STRATEGY_MAX = "prototype_max_v1"
STRATEGY_MEDIAN = "prototype_median_shadow"
STRATEGY_TWO_VOTE = "two_prototype_consensus_shadow"
STRATEGIES = (STRATEGY_MAX, STRATEGY_MEDIAN, STRATEGY_TWO_VOTE)

_PROFILE = load_channel_profile(Path(__file__).resolve().parents[1])
_HOST_NAMES = frozenset({_PROFILE.host_speaker_label, _PROFILE.display_name})
_GUEST_NAMES = frozenset({_PROFILE.guest_speaker_label})
_TRUTH_SPEAKER_NAMES = _HOST_NAMES | _GUEST_NAMES


class ChallengeEvaluationError(ValueError):
    pass


@dataclass(frozen=True)
class TruthSegment:
    start_ms: int
    end_ms: int
    label: str
    source_cue: int


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_sha256(value: object, *, label: str) -> str:
    normalized = str(value or "").lower().removeprefix("sha256:")
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ChallengeEvaluationError(f"{label} sha256 is malformed")
    return "sha256:" + normalized


def _parse_timestamp(value: object) -> int:
    text = str(value or "")
    try:
        hours, minutes, remainder = text.split(":")
        seconds, milliseconds = remainder.split(",")
        parsed = (
            int(hours) * 3_600_000
            + int(minutes) * 60_000
            + int(seconds) * 1_000
            + int(milliseconds)
        )
    except (TypeError, ValueError) as exc:
        raise ChallengeEvaluationError(f"invalid timestamp: {text!r}") from exc
    if parsed < 0:
        raise ChallengeEvaluationError(f"negative timestamp: {text!r}")
    return parsed


def load_reviewed_truth(path: Path) -> tuple[list[TruthSegment], dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ChallengeEvaluationError("speaker override root must be an object")
    rows = payload.get("overrides")
    if not isinstance(rows, list):
        raise ChallengeEvaluationError("speaker override has no overrides list")

    segments: list[TruthSegment] = []
    mixed_cues: set[int] = set()
    reviewed_cue_bindings: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ChallengeEvaluationError("speaker override row must be an object")
        source_cue = row.get("source_cue")
        raw_segments = row.get("segments")
        if (
            isinstance(source_cue, bool)
            or not isinstance(source_cue, int)
            or not isinstance(raw_segments, list)
            or not raw_segments
        ):
            raise ChallengeEvaluationError("speaker override row is malformed")
        expect = row.get("expect")
        if not isinstance(expect, Mapping):
            raise ChallengeEvaluationError("speaker override row has no cue binding")
        expected_start_ms = _parse_timestamp(expect.get("start"))
        expected_end_ms = _parse_timestamp(expect.get("end"))
        expected_text = expect.get("text")
        if (
            expected_end_ms <= expected_start_ms
            or not isinstance(expected_text, str)
            or not expected_text.strip()
        ):
            raise ChallengeEvaluationError("speaker override cue binding is malformed")
        reviewed_cue_bindings.append(
            {
                "source_cue": source_cue,
                "start_ms": expected_start_ms,
                "end_ms": expected_end_ms,
                "content_text_sha256": "sha256:"
                + hashlib.sha256(expected_text.encode("utf-8")).hexdigest(),
            }
        )
        cue_labels: set[str] = set()
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, Mapping):
                raise ChallengeEvaluationError("speaker truth segment must be an object")
            speaker = str(raw_segment.get("speaker") or "").strip()
            if speaker not in _TRUTH_SPEAKER_NAMES:
                raise ChallengeEvaluationError(
                    f"speaker truth segment has an unsupported speaker: {speaker!r}"
                )
            label = ho.LABEL_HOST if speaker in _HOST_NAMES else ho.LABEL_OTHER
            start_ms = _parse_timestamp(raw_segment.get("start"))
            end_ms = _parse_timestamp(raw_segment.get("end"))
            if end_ms <= start_ms:
                raise ChallengeEvaluationError("speaker truth segment is not positive")
            cue_labels.add(label)
            segments.append(
                TruthSegment(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    label=label,
                    source_cue=source_cue,
                )
            )
        if len(cue_labels) > 1:
            mixed_cues.add(source_cue)

    segments.sort(key=lambda item: (item.start_ms, item.end_ms, item.source_cue))
    baseline = payload.get("reviewed_speaker_baseline")
    source_media_sha256 = _normalized_sha256(
        payload.get("source_media_sha256"), label="truth source media"
    )
    source_srt_sha256 = _normalized_sha256(
        payload.get("source_srt_sha256"), label="truth source SRT"
    )
    automatic_input = baseline.get("automatic_input") if isinstance(baseline, Mapping) else None
    if isinstance(automatic_input, Mapping):
        automatic_sha256 = _normalized_sha256(
            automatic_input.get("sha256"), label="reviewed automatic input"
        )
        if automatic_sha256 != source_srt_sha256:
            raise ChallengeEvaluationError(
                "reviewed automatic input does not match truth source SRT"
            )
    machine_count = (
        len(baseline.get("machine_cues", []))
        if isinstance(baseline, Mapping) and isinstance(baseline.get("machine_cues"), list)
        else 0
    )
    return segments, {
        "candidate_id": str(payload.get("candidate_id") or path.stem),
        "override_sha256": _sha256(path),
        "reviewed_override_rows": len(rows),
        "reviewed_truth_segments": len(segments),
        "mixed_override_cues": len(mixed_cues),
        "machine_only_cues_excluded": machine_count,
        "source_media_sha256": source_media_sha256,
        "source_srt_sha256": source_srt_sha256,
        "reviewed_cue_count": (
            int(baseline["cue_count"])
            if isinstance(baseline, Mapping)
            and isinstance(baseline.get("cue_count"), int)
            and not isinstance(baseline.get("cue_count"), bool)
            else None
        ),
        "reviewed_cue_bindings": reviewed_cue_bindings,
    }


def _window_observations(
    report: Mapping[str, object], *, strategy: str
) -> list[ho.WindowObservation]:
    rows = report.get("windows")
    if not isinstance(rows, list) or not rows:
        raise ChallengeEvaluationError("occupancy report has no windows")
    observations: list[ho.WindowObservation] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ChallengeEvaluationError("occupancy window row is malformed")
        start_ms = row.get("start_ms")
        end_ms = row.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
        ):
            raise ChallengeEvaluationError("occupancy window bounds are malformed")
        scores = row.get("prototype_scores")
        if row.get("label") == ho.LABEL_NON_SPEECH and not scores:
            observations.append(
                ho.WindowObservation(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    label=ho.LABEL_NON_SPEECH,
                )
            )
            continue
        if not isinstance(scores, Mapping) or not scores:
            raise ChallengeEvaluationError(
                "occupancy receipt lacks prototype_scores; rerun with v2 estimator"
            )
        values = [float(value) for value in scores.values()]
        if strategy == STRATEGY_MAX:
            label = ho.classify_score(max(values))
        elif strategy == STRATEGY_MEDIAN:
            label = ho.classify_score(float(statistics.median(values)))
        elif strategy == STRATEGY_TWO_VOTE:
            host_votes = sum(value >= ho.HOST_SIMILARITY_MIN for value in values)
            if host_votes >= 2:
                label = ho.LABEL_HOST
            elif all(value <= ho.OTHER_SIMILARITY_MAX for value in values):
                label = ho.LABEL_OTHER
            else:
                label = ho.LABEL_UNKNOWN
        else:
            raise ChallengeEvaluationError(f"unknown strategy: {strategy}")
        observations.append(
            ho.WindowObservation(
                start_ms=start_ms,
                end_ms=end_ms,
                label=label,
                score=max(values),
                prototype_scores={str(key): float(value) for key, value in scores.items()},
            )
        )
    return ho.smooth_labels(observations)


def _truth_for_cell(
    start_ms: int, end_ms: int, truth: Sequence[TruthSegment]
) -> tuple[str | None, int]:
    duration = end_ms - start_ms
    overlaps = {ho.LABEL_HOST: 0, ho.LABEL_OTHER: 0}
    for segment in truth:
        covered = min(end_ms, segment.end_ms) - max(start_ms, segment.start_ms)
        if covered > 0:
            overlaps[segment.label] += covered
    winner, covered_ms = max(overlaps.items(), key=lambda item: item[1])
    if duration <= 0 or covered_ms / duration < TRUTH_MIN_CELL_COVERAGE:
        return None, max(overlaps.values())
    other = ho.LABEL_OTHER if winner == ho.LABEL_HOST else ho.LABEL_HOST
    if overlaps[other] > duration * (1.0 - TRUTH_MIN_CELL_COVERAGE):
        return None, covered_ms
    return winner, covered_ms


def evaluate_strategy(
    report: Mapping[str, object],
    truth: Sequence[TruthSegment],
    *,
    strategy: str,
) -> dict[str, object]:
    observations = _window_observations(report, strategy=strategy)
    confusion = {
        truth_label: {
            predicted: 0 for predicted in (ho.LABEL_HOST, ho.LABEL_OTHER, ho.LABEL_UNKNOWN)
        }
        for truth_label in (ho.LABEL_HOST, ho.LABEL_OTHER)
    }
    excluded_ms = 0
    for start_ms, end_ms, predicted in ho.observation_cells(observations):
        if predicted == ho.LABEL_NON_SPEECH:
            predicted = ho.LABEL_UNKNOWN
        truth_label, _ = _truth_for_cell(start_ms, end_ms, truth)
        duration = end_ms - start_ms
        if truth_label is None:
            excluded_ms += duration
            continue
        confusion[truth_label][predicted] += duration

    host_total = sum(confusion[ho.LABEL_HOST].values())
    other_total = sum(confusion[ho.LABEL_OTHER].values())
    total = host_total + other_total
    correct = confusion[ho.LABEL_HOST][ho.LABEL_HOST] + confusion[ho.LABEL_OTHER][ho.LABEL_OTHER]
    classified = total - (
        confusion[ho.LABEL_HOST][ho.LABEL_UNKNOWN] + confusion[ho.LABEL_OTHER][ho.LABEL_UNKNOWN]
    )

    def ratio(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 6) if denominator else None

    false_host_rate = ratio(confusion[ho.LABEL_OTHER][ho.LABEL_HOST], other_total)
    false_other_rate = ratio(confusion[ho.LABEL_HOST][ho.LABEL_OTHER], host_total)
    host_recall = ratio(confusion[ho.LABEL_HOST][ho.LABEL_HOST], host_total)
    classified_coverage = ratio(classified, total)
    unknown_share = ratio(total - classified, total)
    passes_gate = bool(
        false_host_rate == 0.0
        and host_recall is not None
        and host_recall >= 0.85
        and classified_coverage is not None
        and classified_coverage >= 0.85
        and unknown_share is not None
        and unknown_share <= 0.15
    )
    return {
        "strategy": strategy,
        "clear_truth_ms": total,
        "excluded_mixed_or_uncovered_ms": excluded_ms,
        "truth_host_ms": host_total,
        "truth_other_ms": other_total,
        "confusion_ms": confusion,
        "accuracy_including_abstain": ratio(correct, total),
        "classified_coverage": classified_coverage,
        "clear_unknown_share": unknown_share,
        "host_recall": host_recall,
        "other_recall": ratio(confusion[ho.LABEL_OTHER][ho.LABEL_OTHER], other_total),
        "false_host_rate": false_host_rate,
        "false_other_rate": false_other_rate,
        "passes_provisional_accuracy_gate": passes_gate,
    }


def _cue_truth(
    truth: Sequence[TruthSegment],
) -> dict[int, str]:
    labels: dict[int, set[str]] = defaultdict(set)
    for segment in truth:
        labels[segment.source_cue].add(segment.label)
    return {
        source_cue: (next(iter(values)) if len(values) == 1 else "MIXED_OR_OVERLAP")
        for source_cue, values in labels.items()
    }


def _finite_scores(raw: object, *, expected_count: int, label: str) -> list[float]:
    if not isinstance(raw, Mapping) or len(raw) != expected_count:
        raise ChallengeEvaluationError(f"{label} score bank is missing or incomplete")
    values: list[float] = []
    for value in raw.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ChallengeEvaluationError(f"{label} score is malformed")
        number = float(value)
        if not math.isfinite(number):
            raise ChallengeEvaluationError(f"{label} score is not finite")
        values.append(number)
    return sorted(values)


def _score_distribution(values: Sequence[float]) -> dict[str, object] | None:
    if not values:
        return None
    return {
        "count": len(values),
        "min": round(min(values), 5),
        "median": round(float(statistics.median(values)), 5),
        "mean": round(float(statistics.fmean(values)), 5),
        "max": round(max(values), 5),
    }


def _separability_summary(
    rows: Sequence[tuple[str, str, float]],
) -> dict[str, object]:
    host = [value for truth, _stratum, value in rows if truth == ho.LABEL_HOST]
    other = [value for truth, _stratum, value in rows if truth == ho.LABEL_OTHER]
    if not host or not other:
        return {
            "truth_host_count": len(host),
            "truth_other_count": len(other),
            "host_distribution": _score_distribution(host),
            "other_distribution": _score_distribution(other),
            "zero_false_host_oracle": None,
            "auc": None,
        }
    max_other = max(other)
    oracle_hits = sum(value > max_other for value in host)
    frozen_false_host = sum(value >= ho.HOST_SIMILARITY_MIN for value in other)
    frozen_host_hits = sum(value >= ho.HOST_SIMILARITY_MIN for value in host)
    pairwise_wins = sum(
        1.0 if host_value > other_value else 0.5 if host_value == other_value else 0.0
        for host_value in host
        for other_value in other
    )
    return {
        "truth_host_count": len(host),
        "truth_other_count": len(other),
        "host_distribution": _score_distribution(host),
        "other_distribution": _score_distribution(other),
        "zero_false_host_oracle": {
            "comparison_rule": "score > max_observed_other_score",
            "max_observed_other_score": round(max_other, 5),
            "host_hits": oracle_hits,
            "host_recall_upper_bound": round(oracle_hits / len(host), 6),
            "meets_required_0_85_recall": oracle_hits / len(host) >= 0.85,
            "threshold_selected_after_truth": True,
            "promotion_authority": False,
        },
        "frozen_provisional_host_threshold": {
            "threshold": ho.HOST_SIMILARITY_MIN,
            "false_host_count": frozen_false_host,
            "host_recall": round(frozen_host_hits / len(host), 6),
        },
        "auc": round(pairwise_wins / (len(host) * len(other)), 6),
    }


def evaluate_reviewed_enrollment_signal(
    report: Mapping[str, object], truth: Sequence[TruthSegment]
) -> dict[str, object] | None:
    """Measure cross-session score separation without defining a selector.

    The zero-false-host row is an oracle upper bound chosen after seeing this
    development truth.  It answers whether the bank contains enough signal to
    justify further work; it is never a calibrated threshold or promotion gate.
    """

    receipt = report.get("reviewed_development_enrollment")
    if receipt is None:
        return None
    if not isinstance(receipt, Mapping):
        raise ChallengeEvaluationError("reviewed development enrollment receipt is malformed")
    prototype_count = receipt.get("prototype_count")
    if (
        isinstance(prototype_count, bool)
        or not isinstance(prototype_count, int)
        or prototype_count < 3
        or receipt.get("production_profile_unchanged") is not True
        or receipt.get("promotion_authority") is not False
        or not str(receipt.get("source_session_id") or "")
        or receipt.get("source_session_id") == receipt.get("target_session_id")
    ):
        raise ChallengeEvaluationError(
            "reviewed development enrollment receipt authority is invalid"
        )
    base_enrollment = report.get("enrollment")
    base_count = (
        base_enrollment.get("prototype_count") if isinstance(base_enrollment, Mapping) else None
    )
    if isinstance(base_count, bool) or not isinstance(base_count, int) or base_count < 1:
        raise ChallengeEvaluationError("base enrollment prototype count is invalid")
    units = report.get("units")
    if not isinstance(units, list):
        raise ChallengeEvaluationError("cue report units are missing")
    by_source_cue = {unit.get("source_cue"): unit for unit in units if isinstance(unit, Mapping)}
    truth_by_cue = _cue_truth(truth)
    metric_rows: dict[str, list[tuple[str, str, float]]] = {
        "reviewed_bank_max": [],
        "reviewed_bank_median": [],
        "reviewed_bank_second_highest": [],
        "dual_bank_min_of_medians": [],
    }
    for source_cue, truth_label in sorted(truth_by_cue.items()):
        if truth_label not in {ho.LABEL_HOST, ho.LABEL_OTHER}:
            continue
        unit = by_source_cue.get(source_cue)
        if not isinstance(unit, Mapping):
            raise ChallengeEvaluationError(f"cue report misses reviewed truth cue {source_cue}")
        stratum = str(unit.get("duration_stratum") or "")
        if stratum not in {"SHORT", "STANDARD", "LONG"}:
            raise ChallengeEvaluationError("cue duration stratum is invalid")
        reviewed_scores = _finite_scores(
            unit.get("reviewed_development_enrollment_scores"),
            expected_count=prototype_count,
            label="reviewed development enrollment",
        )
        base_scores = _finite_scores(
            unit.get("prototype_scores"),
            expected_count=base_count,
            label="base enrollment",
        )
        reviewed_median = float(statistics.median(reviewed_scores))
        base_median = float(statistics.median(base_scores))
        values = {
            "reviewed_bank_max": reviewed_scores[-1],
            "reviewed_bank_median": reviewed_median,
            "reviewed_bank_second_highest": reviewed_scores[-2],
            "dual_bank_min_of_medians": min(reviewed_median, base_median),
        }
        for name, value in values.items():
            metric_rows[name].append((truth_label, stratum, value))

    metrics: list[dict[str, object]] = []
    for name, rows in metric_rows.items():
        metrics.append(
            {
                "metric": name,
                "overall": _separability_summary(rows),
                "by_duration_stratum": {
                    stratum: _separability_summary([row for row in rows if row[1] == stratum])
                    for stratum in ("SHORT", "STANDARD", "LONG")
                },
            }
        )
    return {
        "purpose": ("DEVELOPMENT_TRUTH_SEPARABILITY_DIAGNOSTIC_NOT_THRESHOLD_AUTHORITY"),
        "source_session_id": receipt.get("source_session_id"),
        "target_session_id": receipt.get("target_session_id"),
        "prototype_count": prototype_count,
        "total_duration_ms": receipt.get("total_duration_ms"),
        "metrics": metrics,
        "thresholds_selected_after_truth_are_oracle_only": True,
        "production_promotion_authorized": False,
    }


def _cue_prediction(
    unit: Mapping[str, object], *, strategy: str, decoded_duration_ms: int
) -> tuple[str, str | None]:
    status = str(unit.get("analysis_status") or "")
    if status not in {"SCORED_EVIDENCE", "UNSCORABLE"}:
        raise ChallengeEvaluationError("cue analysis status is invalid")
    try:
        prediction, reason = cue_shadow.replay_unit_prediction(
            unit,
            strategy=strategy,
            decoded_duration_ms=decoded_duration_ms,
        )
    except (TypeError, ValueError, cue_shadow.CueAlignedShadowError) as exc:
        raise ChallengeEvaluationError(f"invalid cue evidence: {exc}") from exc
    return prediction, reason


def evaluate_cue_strategy(
    report: Mapping[str, object],
    truth: Sequence[TruthSegment],
    *,
    strategy: str,
) -> dict[str, object]:
    """Score cue-aligned receipts without treating mixed rows as clear truth."""

    units = report.get("units")
    if not isinstance(units, list) or not units:
        raise ChallengeEvaluationError("cue-aligned report has no units")
    by_source_cue: dict[int, Mapping[str, object]] = {}
    for unit in units:
        if not isinstance(unit, Mapping):
            raise ChallengeEvaluationError("cue unit is malformed")
        source_cue = unit.get("source_cue")
        if isinstance(source_cue, bool) or not isinstance(source_cue, int):
            raise ChallengeEvaluationError("cue unit source_cue is malformed")
        if source_cue in by_source_cue:
            raise ChallengeEvaluationError(f"duplicate cue unit: {source_cue}")
        by_source_cue[source_cue] = unit

    bindings = report.get("bindings")
    decoded_duration_ms = (
        bindings.get("decoded_pcm_duration_ms") if isinstance(bindings, Mapping) else None
    )
    if (
        isinstance(decoded_duration_ms, bool)
        or not isinstance(decoded_duration_ms, int)
        or decoded_duration_ms <= 0
    ):
        raise ChallengeEvaluationError("cue report decoded PCM duration is invalid")
    truth_by_cue = _cue_truth(truth)
    missing = sorted(set(truth_by_cue) - set(by_source_cue))
    if missing:
        raise ChallengeEvaluationError(f"cue report misses reviewed truth cues: {missing[:8]}")
    confusion = {
        label: {prediction: 0 for prediction in (ho.LABEL_HOST, ho.LABEL_OTHER, ho.LABEL_UNKNOWN)}
        for label in (ho.LABEL_HOST, ho.LABEL_OTHER)
    }
    mixed_predictions = {
        ho.LABEL_HOST: 0,
        ho.LABEL_OTHER: 0,
        ho.LABEL_UNKNOWN: 0,
    }
    abstention_reasons: dict[str, int] = defaultdict(int)
    stratum_confusion: dict[str, dict[str, dict[str, int]]] = {
        stratum: {
            label: {
                prediction: 0 for prediction in (ho.LABEL_HOST, ho.LABEL_OTHER, ho.LABEL_UNKNOWN)
            }
            for label in (ho.LABEL_HOST, ho.LABEL_OTHER)
        }
        for stratum in ("SHORT", "STANDARD", "LONG")
    }
    for source_cue, truth_label in sorted(truth_by_cue.items()):
        unit = by_source_cue[source_cue]
        prediction, reason = _cue_prediction(
            unit, strategy=strategy, decoded_duration_ms=decoded_duration_ms
        )
        if prediction == ho.LABEL_UNKNOWN:
            abstention_reasons[str(reason or "SCORE_AMBIGUITY")] += 1
        if truth_label == "MIXED_OR_OVERLAP":
            mixed_predictions[prediction] += 1
        else:
            confusion[truth_label][prediction] += 1
            stratum = str(unit.get("duration_stratum") or "")
            if stratum not in stratum_confusion:
                raise ChallengeEvaluationError("cue duration stratum is invalid")
            stratum_confusion[stratum][truth_label][prediction] += 1

    host_total = sum(confusion[ho.LABEL_HOST].values())
    other_total = sum(confusion[ho.LABEL_OTHER].values())
    clear_total = host_total + other_total
    unknown = (
        confusion[ho.LABEL_HOST][ho.LABEL_UNKNOWN] + confusion[ho.LABEL_OTHER][ho.LABEL_UNKNOWN]
    )
    classified = clear_total - unknown
    correct = confusion[ho.LABEL_HOST][ho.LABEL_HOST] + confusion[ho.LABEL_OTHER][ho.LABEL_OTHER]
    predicted_host = (
        confusion[ho.LABEL_HOST][ho.LABEL_HOST] + confusion[ho.LABEL_OTHER][ho.LABEL_HOST]
    )

    def ratio(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 6) if denominator else None

    false_host_count = confusion[ho.LABEL_OTHER][ho.LABEL_HOST]
    host_recall = ratio(confusion[ho.LABEL_HOST][ho.LABEL_HOST], host_total)
    host_precision = ratio(confusion[ho.LABEL_HOST][ho.LABEL_HOST], predicted_host)
    coverage = ratio(classified, clear_total)
    unknown_share = ratio(unknown, clear_total)
    verified_accuracy = ratio(correct, classified)
    mixed_hard = mixed_predictions[ho.LABEL_HOST] + mixed_predictions[ho.LABEL_OTHER]
    passes_gate = bool(
        false_host_count == 0
        and host_precision == 1.0
        and host_recall is not None
        and host_recall >= 0.85
        and coverage is not None
        and coverage >= 0.85
        and unknown_share is not None
        and unknown_share <= 0.15
        and verified_accuracy is not None
        and verified_accuracy >= 0.95
        and mixed_hard == 0
    )
    return {
        "strategy": strategy,
        "clear_truth_cues": clear_total,
        "mixed_truth_cues": sum(mixed_predictions.values()),
        "confusion_cues": confusion,
        "mixed_predictions": mixed_predictions,
        "abstention_reasons": dict(sorted(abstention_reasons.items())),
        "stratum_confusion_cues": stratum_confusion,
        "false_host_count": false_host_count,
        "false_other_count": confusion[ho.LABEL_HOST][ho.LABEL_OTHER],
        "host_precision": host_precision,
        "host_recall": host_recall,
        "other_recall": ratio(confusion[ho.LABEL_OTHER][ho.LABEL_OTHER], other_total),
        "classified_coverage": coverage,
        "clear_unknown_share": unknown_share,
        "verified_accuracy": verified_accuracy,
        "mixed_hard_label_count": mixed_hard,
        "passes_provisional_accuracy_gate": passes_gate,
    }


def evaluate_cue_report(
    report: Mapping[str, object],
    *,
    report_sha256: str,
    truth: Sequence[TruthSegment],
    truth_summary: Mapping[str, object],
) -> dict[str, object]:
    candidate_id = str(report.get("candidate_id") or "")
    expected = str(truth_summary.get("candidate_id") or "")
    if not candidate_id or candidate_id != expected:
        raise ChallengeEvaluationError(
            f"candidate mismatch: report={candidate_id!r} truth={expected!r}"
        )
    if report.get("purpose") != cue_shadow.PURPOSE:
        raise ChallengeEvaluationError("cue report does not declare shadow-only purpose")
    if report.get("schema_version") != cue_shadow.SCHEMA_VERSION:
        raise ChallengeEvaluationError("cue report schema version is invalid")
    if report.get("provisional_calibration") is not True:
        raise ChallengeEvaluationError("cue report must remain provisional")
    bindings = report.get("bindings")
    if not isinstance(bindings, Mapping):
        raise ChallengeEvaluationError("cue report bindings are missing")
    if _normalized_sha256(
        bindings.get("media_sha256"), label="cue report media"
    ) != truth_summary.get("source_media_sha256"):
        raise ChallengeEvaluationError("cue report media does not match reviewed truth")
    if _normalized_sha256(bindings.get("srt_sha256"), label="cue report SRT") != truth_summary.get(
        "source_srt_sha256"
    ):
        raise ChallengeEvaluationError("cue report SRT does not match reviewed truth")
    reviewed_cue_count = truth_summary.get("reviewed_cue_count")
    if reviewed_cue_count is not None and report.get("cue_count") != reviewed_cue_count:
        raise ChallengeEvaluationError("cue report count does not match reviewed baseline")
    units = report.get("units")
    if not isinstance(units, list):
        raise ChallengeEvaluationError("cue report units are missing")
    units_by_cue = {unit.get("source_cue"): unit for unit in units if isinstance(unit, Mapping)}
    if len(units_by_cue) != len(units):
        raise ChallengeEvaluationError("cue report unit identities are invalid")
    reviewed_bindings = truth_summary.get("reviewed_cue_bindings")
    if not isinstance(reviewed_bindings, list):
        raise ChallengeEvaluationError("reviewed truth cue bindings are missing")
    for expected_binding in reviewed_bindings:
        if not isinstance(expected_binding, Mapping):
            raise ChallengeEvaluationError("reviewed truth cue binding is malformed")
        unit = units_by_cue.get(expected_binding.get("source_cue"))
        if not isinstance(unit, Mapping) or any(
            unit.get(field) != expected_binding.get(field)
            for field in ("start_ms", "end_ms", "content_text_sha256")
        ):
            raise ChallengeEvaluationError(
                f"cue report grid/text mismatch at source cue {expected_binding.get('source_cue')}"
            )
    deterministic_payload = {
        key: value
        for key, value in report.items()
        if key
        not in {
            "deterministic_payload_sha256",
            "model_load_seconds",
            "scoring_seconds",
            "wall_seconds",
        }
    }
    if report.get("deterministic_payload_sha256") != cue_shadow._canonical_sha256(
        deterministic_payload
    ):
        raise ChallengeEvaluationError("cue report deterministic payload hash mismatch")
    if any(
        not isinstance(unit.get("predictions"), Mapping)
        or set(unit["predictions"]) != set(cue_shadow.STRATEGIES)
        for unit in units
    ):
        raise ChallengeEvaluationError("cue report strategy set is incomplete")
    strategies = cue_shadow.STRATEGIES
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": "DEVELOPMENT_DIAGNOSTIC_NOT_RELEASE_AUTHORITY",
        "evaluation_mode": "CUE_ALIGNED",
        "candidate_id": candidate_id,
        "speaker_report_sha256": report_sha256,
        "speaker_report_schema_version": report.get("schema_version"),
        "truth": dict(truth_summary),
        "strategies": [
            evaluate_cue_strategy(report, truth, strategy=strategy) for strategy in strategies
        ],
        "reviewed_development_enrollment_signal": (
            evaluate_reviewed_enrollment_signal(report, truth)
        ),
        "promotion_gate": {
            "false_host_count": 0,
            "host_precision": 1.0,
            "minimum_host_recall": 0.85,
            "minimum_classified_coverage": 0.85,
            "maximum_clear_unknown_share": 0.15,
            "minimum_verified_accuracy": 0.95,
            "mixed_hard_label_count": 0,
            "additional_requirement": "two locked cross-session holdouts",
        },
    }


def evaluate_report(
    report: Mapping[str, object],
    *,
    report_sha256: str,
    truth: Sequence[TruthSegment],
    truth_summary: Mapping[str, object],
) -> dict[str, object]:
    if isinstance(report.get("units"), list):
        return evaluate_cue_report(
            report,
            report_sha256=report_sha256,
            truth=truth,
            truth_summary=truth_summary,
        )
    candidate_id = str(report.get("candidate_id") or "")
    expected = str(truth_summary.get("candidate_id") or "")
    if not candidate_id or candidate_id != expected:
        raise ChallengeEvaluationError(
            f"candidate mismatch: report={candidate_id!r} truth={expected!r}"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": "DEVELOPMENT_DIAGNOSTIC_NOT_RELEASE_AUTHORITY",
        "candidate_id": candidate_id,
        "occupancy_report_sha256": report_sha256,
        "occupancy_schema_version": report.get("schema_version"),
        "occupancy_estimator_version": report.get("estimator_version"),
        "occupancy_threshold_version": report.get("threshold_version"),
        "truth": dict(truth_summary),
        "truth_min_cell_coverage": TRUTH_MIN_CELL_COVERAGE,
        "strategies": [
            evaluate_strategy(report, truth, strategy=strategy) for strategy in STRATEGIES
        ],
        "promotion_gate": {
            "false_host_rate": 0.0,
            "minimum_host_recall": 0.85,
            "minimum_classified_coverage": 0.85,
            "maximum_clear_unknown_share": 0.15,
            "additional_requirement": "two locked cross-session holdouts",
        },
    }


def _select_report(payload: object, candidate_id: str) -> Mapping[str, object]:
    if isinstance(payload, Mapping) and isinstance(payload.get("reports"), list):
        rows = payload["reports"]
    else:
        rows = payload if isinstance(payload, list) else [payload]
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping) and str(row.get("candidate_id") or "") == candidate_id
    ]
    if len(matches) != 1:
        raise ChallengeEvaluationError(
            f"expected one occupancy report for {candidate_id}, found {len(matches)}"
        )
    return matches[0]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--occupancy-report", type=Path, required=True)
    parser.add_argument("--speaker-override", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    truth, truth_summary = load_reviewed_truth(args.speaker_override)
    report_payload = json.loads(args.occupancy_report.read_text(encoding="utf-8"))
    candidate_id = str(truth_summary["candidate_id"])
    report = _select_report(report_payload, candidate_id)
    result = evaluate_report(
        report,
        report_sha256=_sha256(args.occupancy_report),
        truth=truth,
        truth_summary=truth_summary,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
