#!/usr/bin/env python3
"""Aggregate per-candidate cue speaker challenges with fail-closed gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import run_cue_aligned_speaker_shadow as cue_shadow
from src.autoslice import host_occupancy as ho


SCHEMA_VERSION = "cue-speaker-challenge-aggregate.v1"
PURPOSE = "DEVELOPMENT_DIAGNOSTIC_NOT_RELEASE_AUTHORITY"
BASELINE_CORRECT = 157
BASELINE_CLEAR = 188


class AggregateChallengeError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _sum_confusions(rows: Sequence[Mapping[str, object]]) -> dict[str, dict[str, int]]:
    result = {
        truth: {
            prediction: 0
            for prediction in (
                ho.LABEL_HOST,
                ho.LABEL_OTHER,
                ho.LABEL_UNKNOWN,
            )
        }
        for truth in (ho.LABEL_HOST, ho.LABEL_OTHER)
    }
    for row in rows:
        confusion = row.get("confusion_cues")
        if not isinstance(confusion, Mapping):
            raise AggregateChallengeError("strategy confusion matrix is missing")
        for truth in result:
            values = confusion.get(truth)
            if not isinstance(values, Mapping):
                raise AggregateChallengeError("strategy confusion matrix is malformed")
            for prediction in result[truth]:
                value = values.get(prediction)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise AggregateChallengeError("strategy confusion count is invalid")
                result[truth][prediction] += value
    return result


def _aggregate_strategy(name: str, rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    confusion = _sum_confusions(rows)
    host_total = sum(confusion[ho.LABEL_HOST].values())
    other_total = sum(confusion[ho.LABEL_OTHER].values())
    clear_total = host_total + other_total
    correct = confusion[ho.LABEL_HOST][ho.LABEL_HOST] + confusion[ho.LABEL_OTHER][ho.LABEL_OTHER]
    unknown = (
        confusion[ho.LABEL_HOST][ho.LABEL_UNKNOWN] + confusion[ho.LABEL_OTHER][ho.LABEL_UNKNOWN]
    )
    classified = clear_total - unknown
    predicted_host = (
        confusion[ho.LABEL_HOST][ho.LABEL_HOST] + confusion[ho.LABEL_OTHER][ho.LABEL_HOST]
    )
    false_host = confusion[ho.LABEL_OTHER][ho.LABEL_HOST]
    mixed_hard = 0
    for row in rows:
        value = row.get("mixed_hard_label_count")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AggregateChallengeError("mixed hard-label count is invalid")
        mixed_hard += value
    per_candidate_pass = all(row.get("passes_provisional_accuracy_gate") is True for row in rows)
    host_precision = _ratio(confusion[ho.LABEL_HOST][ho.LABEL_HOST], predicted_host)
    host_recall = _ratio(confusion[ho.LABEL_HOST][ho.LABEL_HOST], host_total)
    coverage = _ratio(classified, clear_total)
    unknown_share = _ratio(unknown, clear_total)
    verified_accuracy = _ratio(correct, classified)
    verified_correct_coverage = _ratio(correct, clear_total)
    development_gate = bool(
        per_candidate_pass
        and false_host == 0
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
        and verified_correct_coverage is not None
        and verified_correct_coverage > BASELINE_CORRECT / BASELINE_CLEAR
    )
    return {
        "strategy": name,
        "candidate_count": len(rows),
        "clear_truth_cues": clear_total,
        "confusion_cues": confusion,
        "false_host_count": false_host,
        "false_other_count": confusion[ho.LABEL_HOST][ho.LABEL_OTHER],
        "host_precision": host_precision,
        "host_recall": host_recall,
        "other_recall": _ratio(confusion[ho.LABEL_OTHER][ho.LABEL_OTHER], other_total),
        "classified_coverage": coverage,
        "clear_unknown_share": unknown_share,
        "verified_accuracy": verified_accuracy,
        "verified_correct_coverage": verified_correct_coverage,
        "mixed_hard_label_count": mixed_hard,
        "all_candidates_pass": per_candidate_pass,
        "passes_development_gate": development_gate,
    }


def aggregate_documents(
    documents: Sequence[Mapping[str, object]],
    *,
    input_bindings: Sequence[Mapping[str, str]] = (),
) -> dict[str, object]:
    if len(documents) < 2:
        raise AggregateChallengeError("aggregate requires at least two candidates")
    candidate_ids: list[str] = []
    by_strategy: dict[str, list[Mapping[str, object]]] = {
        strategy: [] for strategy in cue_shadow.STRATEGIES
    }
    for document in documents:
        if document.get("purpose") != PURPOSE or document.get("evaluation_mode") != "CUE_ALIGNED":
            raise AggregateChallengeError("input is not a cue-aligned development challenge")
        candidate_id = str(document.get("candidate_id") or "")
        if not candidate_id or candidate_id in candidate_ids:
            raise AggregateChallengeError("candidate identities are empty or duplicated")
        candidate_ids.append(candidate_id)
        strategies = document.get("strategies")
        if not isinstance(strategies, list):
            raise AggregateChallengeError("candidate strategy list is missing")
        indexed = {str(row.get("strategy")): row for row in strategies if isinstance(row, Mapping)}
        if set(indexed) != set(cue_shadow.STRATEGIES):
            raise AggregateChallengeError("candidate strategy set is inconsistent")
        for strategy in cue_shadow.STRATEGIES:
            by_strategy[strategy].append(indexed[strategy])

    strategies = [_aggregate_strategy(name, by_strategy[name]) for name in cue_shadow.STRATEGIES]
    designated = next(
        row for row in strategies if row["strategy"] == cue_shadow.DESIGNATED_STRATEGY
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "candidate_ids": candidate_ids,
        "input_bindings": [dict(binding) for binding in input_bindings],
        "designated_strategy": cue_shadow.DESIGNATED_STRATEGY,
        "strategies": strategies,
        "frozen_automatic_baseline": {
            "correct_clear_cues": BASELINE_CORRECT,
            "clear_cues": BASELINE_CLEAR,
            "correct_coverage": round(BASELINE_CORRECT / BASELINE_CLEAR, 6),
        },
        "locked_cross_session_holdouts": 0,
        "required_locked_cross_session_holdouts": 2,
        "development_gate_passes": designated["passes_development_gate"],
        "production_promotion_authorized": False,
        "promotion_blockers": [
            *(
                []
                if designated["passes_development_gate"]
                else ["DESIGNATED_STRATEGY_FAILED_DEVELOPMENT_GATE"]
            ),
            "TWO_LOCKED_CROSS_SESSION_HOLDOUTS_MISSING",
            "EXPLICIT_DEPLOYMENT_AUTHORIZATION_MISSING",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--challenge", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    documents = []
    bindings = []
    for path in args.challenge:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise AggregateChallengeError(f"challenge root is not an object: {path}")
        documents.append(payload)
        bindings.append({"path": str(path.resolve()), "sha256": _sha256(path)})
    result = aggregate_documents(documents, input_bindings=bindings)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0 if result["development_gate_passes"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
