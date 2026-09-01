from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import evaluate_host_occupancy_challenge as challenge
from scripts import run_cue_aligned_speaker_shadow as cue_shadow
from src.autoslice import host_occupancy as ho


def _override(path: Path) -> Path:
    payload = {
        "schema_version": 1,
        "candidate_id": "candidate-a",
        "source_media_sha256": "a" * 64,
        "source_srt_sha256": "b" * 64,
        "reviewed_speaker_baseline": {
            "cue_count": 3,
            "automatic_input": {"sha256": "b" * 64},
            "machine_cues": [{"source_cue": 99, "speaker": "李豆沙"}],
        },
        "overrides": [
            {
                "source_cue": 1,
                "expect": {
                    "start": "00:00:00,000",
                    "end": "00:00:02,000",
                    "text": "host",
                },
                "segments": [
                    {
                        "speaker": "李豆沙",
                        "start": "00:00:00,000",
                        "end": "00:00:02,000",
                        "text": "host",
                    }
                ],
            },
            {
                "source_cue": 2,
                "expect": {
                    "start": "00:00:02,000",
                    "end": "00:00:04,000",
                    "text": "guest",
                },
                "segments": [
                    {
                        "speaker": "连线",
                        "start": "00:00:02,000",
                        "end": "00:00:04,000",
                        "text": "guest",
                    }
                ],
            },
            {
                "source_cue": 3,
                "expect": {
                    "start": "00:00:04,000",
                    "end": "00:00:05,000",
                    "text": "mixed hostmixed guest",
                },
                "segments": [
                    {
                        "speaker": "李豆沙",
                        "start": "00:00:04,000",
                        "end": "00:00:04,500",
                        "text": "mixed host",
                    },
                    {
                        "speaker": "连线",
                        "start": "00:00:04,500",
                        "end": "00:00:05,000",
                        "text": "mixed guest",
                    },
                ],
            },
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _report() -> dict[str, object]:
    # The guest windows have one accidental high prototype.  max-v1 therefore
    # creates false-host, while two-prototype consensus abstains.
    scores = [
        {"p1": 0.80, "p2": 0.75, "p3": 0.70},
        {"p1": 0.78, "p2": 0.72, "p3": 0.68},
        {"p1": 0.70, "p2": 0.20, "p3": 0.22},
        {"p1": 0.71, "p2": 0.21, "p3": 0.23},
    ]
    return {
        "schema_version": ho.SCHEMA_VERSION,
        "estimator_version": ho.ESTIMATOR_VERSION,
        "threshold_version": ho.THRESHOLD_VERSION,
        "candidate_id": "candidate-a",
        "windows": [
            {
                "start_ms": index * 1_000,
                "end_ms": index * 1_000 + 2_000,
                "label": ho.LABEL_UNKNOWN,
                "score": max(values.values()),
                "best_prototype": "p1",
                "prototype_scores": values,
            }
            for index, values in enumerate(scores)
        ],
    }


def _cue_report(*, mixed_scores: dict[str, float] | None = None) -> dict[str, object]:
    rows = [
        (1, 0, 2_000, "host", {"p1": 0.80, "p2": 0.75, "p3": 0.70}),
        (2, 2_000, 4_000, "guest", {"p1": 0.20, "p2": 0.21, "p3": 0.22}),
        (
            3,
            4_000,
            5_000,
            "mixed hostmixed guest",
            mixed_scores or {"p1": 0.40, "p2": 0.41, "p3": 0.42},
        ),
    ]
    units = []
    for source_cue, start_ms, end_ms, content_text, scores in rows:
        raw_predictions = {
            strategy: cue_shadow.classify_prototype_scores(scores, strategy=strategy)
            for strategy in cue_shadow.STATIC_STRATEGIES
        }
        subwindows = [
            {
                "start_ms": window_start,
                "end_ms": window_end,
                "prototype_scores": scores,
                "raw_predictions": dict(raw_predictions),
            }
            for window_start, window_end in cue_shadow.planned_subwindows(start_ms, end_ms)
        ]
        predictions = {}
        reasons = {}
        for strategy in cue_shadow.STATIC_STRATEGIES:
            prediction, reason = cue_shadow._primary_prediction(
                whole_label=raw_predictions[strategy],
                subwindow_labels=[window["raw_predictions"][strategy] for window in subwindows],
                duration_ms=end_ms - start_ms,
                base_abstention_reason=None,
                allow_short_consensus=strategy
                in {cue_shadow.STRATEGY_TWO_VOTE, cue_shadow.STRATEGY_ALL_VOTE},
            )
            predictions[strategy] = prediction
            reasons[strategy] = reason
        session_scores = (
            {"10": 0.80, "11": 0.78, "12": 0.76}
            if source_cue == 1
            else {"10": 0.20, "11": 0.22, "12": 0.24}
            if source_cue == 2
            else {"10": 0.40, "11": 0.46, "12": 0.44}
        )
        session_prediction = cue_shadow.classify_session_anchor_scores(session_scores)
        predictions[cue_shadow.STRATEGY_SESSION_ANCHOR] = session_prediction
        reasons[cue_shadow.STRATEGY_SESSION_ANCHOR] = (
            None if session_prediction != ho.LABEL_UNKNOWN else "SESSION_SCORE_AMBIGUITY"
        )
        units.append(
            {
                "source_cue": source_cue,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_stratum": (
                    "SHORT" if end_ms - start_ms < cue_shadow.MIN_CUE_MS else "STANDARD"
                ),
                "content_text_sha256": "sha256:"
                + hashlib.sha256(content_text.encode()).hexdigest(),
                "energy_activity_ratio": 1.0,
                "analysis_status": "SCORED_EVIDENCE",
                "prototype_scores": scores,
                "raw_predictions": raw_predictions,
                "subwindows": subwindows,
                "predictions": predictions,
                "primary_abstention_reasons": reasons,
                "session_anchor_status": "READY",
                "session_anchor_scores": session_scores,
            }
        )
    payload = {
        "schema_version": cue_shadow.SCHEMA_VERSION,
        "purpose": cue_shadow.PURPOSE,
        "candidate_id": "candidate-a",
        "bindings": {
            "media_sha256": "sha256:" + "a" * 64,
            "srt_sha256": "sha256:" + "b" * 64,
            "decoded_pcm_duration_ms": 5_000,
        },
        "provisional_calibration": True,
        "cue_count": 3,
        "units": units,
    }
    payload["deterministic_payload_sha256"] = cue_shadow._canonical_sha256(payload)
    return payload


def test_reviewed_truth_excludes_machine_only_cues(tmp_path: Path) -> None:
    truth, summary = challenge.load_reviewed_truth(_override(tmp_path / "truth.json"))
    assert len(truth) == 4
    assert summary["reviewed_override_rows"] == 3
    assert summary["mixed_override_cues"] == 1
    assert summary["machine_only_cues_excluded"] == 1


def test_parser_and_main_preserve_the_evaluator_contract(tmp_path: Path) -> None:
    parser = challenge.build_parser()
    parsed = parser.parse_args(
        ["--occupancy-report", "report.json", "--speaker-override", "truth.json", "--output", "out.json"]
    )
    assert (parsed.occupancy_report, parsed.speaker_override, parsed.output) == (
        Path("report.json"), Path("truth.json"), Path("out.json")
    )
    truth_path = _override(tmp_path / "truth.json")
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_report()), encoding="utf-8")
    output = tmp_path / "out.json"
    assert challenge.main(
        [
            "--occupancy-report", str(report_path),
            "--speaker-override", str(truth_path),
            "--output", str(output),
        ]
    ) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["candidate_id"] == "candidate-a"


def test_strategy_report_separates_false_host_from_abstention(tmp_path: Path) -> None:
    truth, summary = challenge.load_reviewed_truth(_override(tmp_path / "truth.json"))
    result = challenge.evaluate_report(
        _report(),
        report_sha256="sha256:" + "1" * 64,
        truth=truth,
        truth_summary=summary,
    )
    by_name = {row["strategy"]: row for row in result["strategies"]}
    assert by_name[challenge.STRATEGY_MAX]["false_host_rate"] > 0
    assert by_name[challenge.STRATEGY_TWO_VOTE]["false_host_rate"] == 0
    assert (
        by_name[challenge.STRATEGY_TWO_VOTE]["clear_unknown_share"]
        > by_name[challenge.STRATEGY_MAX]["clear_unknown_share"]
    )
    assert result["purpose"] == "DEVELOPMENT_DIAGNOSTIC_NOT_RELEASE_AUTHORITY"


def test_old_v1_receipt_without_per_prototype_scores_is_rejected(
    tmp_path: Path,
) -> None:
    truth, summary = challenge.load_reviewed_truth(_override(tmp_path / "truth.json"))
    report = _report()
    report["windows"][0].pop("prototype_scores")
    with pytest.raises(
        challenge.ChallengeEvaluationError,
        match="lacks prototype_scores",
    ):
        challenge.evaluate_report(
            report,
            report_sha256="sha256:" + "1" * 64,
            truth=truth,
            truth_summary=summary,
        )


def test_candidate_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    truth, summary = challenge.load_reviewed_truth(_override(tmp_path / "truth.json"))
    report = _report()
    report["candidate_id"] = "candidate-b"
    with pytest.raises(challenge.ChallengeEvaluationError, match="candidate mismatch"):
        challenge.evaluate_report(
            report,
            report_sha256="sha256:" + "1" * 64,
            truth=truth,
            truth_summary=summary,
        )


def test_cue_aligned_evaluation_keeps_mixed_truth_out_of_clear_metrics(
    tmp_path: Path,
) -> None:
    truth, summary = challenge.load_reviewed_truth(_override(tmp_path / "truth.json"))
    result = challenge.evaluate_report(
        _cue_report(),
        report_sha256="sha256:" + "2" * 64,
        truth=truth,
        truth_summary=summary,
    )
    assert result["evaluation_mode"] == "CUE_ALIGNED"
    for row in result["strategies"]:
        assert row["clear_truth_cues"] == 2
        assert row["mixed_truth_cues"] == 1
        assert row["mixed_hard_label_count"] == 0
        assert row["false_host_count"] == 0
        assert row["host_recall"] == 1.0
        assert row["passes_provisional_accuracy_gate"] is True


def test_cue_aligned_mixed_hard_label_is_a_separate_gate_failure(
    tmp_path: Path,
) -> None:
    truth, summary = challenge.load_reviewed_truth(_override(tmp_path / "truth.json"))
    result = challenge.evaluate_report(
        _cue_report(mixed_scores={"p1": 0.8, "p2": 0.75, "p3": 0.7}),
        report_sha256="sha256:" + "3" * 64,
        truth=truth,
        truth_summary=summary,
    )
    by_name = {row["strategy"]: row for row in result["strategies"]}
    assert by_name[cue_shadow.STRATEGY_MAX]["mixed_hard_label_count"] == 0
    assert by_name[cue_shadow.STRATEGY_MEDIAN]["mixed_hard_label_count"] == 0
    assert by_name[cue_shadow.STRATEGY_TWO_VOTE]["mixed_hard_label_count"] == 1
    assert by_name[cue_shadow.STRATEGY_ALL_VOTE]["mixed_hard_label_count"] == 1
    assert by_name[cue_shadow.STRATEGY_TWO_VOTE]["passes_provisional_accuracy_gate"] is False
    assert by_name[cue_shadow.STRATEGY_ALL_VOTE]["passes_provisional_accuracy_gate"] is False


def test_reviewed_cross_session_bank_is_reported_as_oracle_signal_only(
    tmp_path: Path,
) -> None:
    truth, summary = challenge.load_reviewed_truth(_override(tmp_path / "truth.json"))
    report = _cue_report()
    report["enrollment"] = {"prototype_count": 3}
    report["reviewed_development_enrollment"] = {
        "prototype_count": 4,
        "total_duration_ms": 4_860,
        "source_session_id": "source-session",
        "target_session_id": "target-session",
        "production_profile_unchanged": True,
        "promotion_authority": False,
    }
    reviewed_scores = {
        1: {"r1": 0.80, "r2": 0.75, "r3": 0.70, "r4": 0.65},
        2: {"r1": 0.60, "r2": 0.20, "r3": 0.25, "r4": 0.30},
        3: {"r1": 0.45, "r2": 0.40, "r3": 0.35, "r4": 0.30},
    }
    for unit in report["units"]:
        unit["reviewed_development_enrollment_scores"] = reviewed_scores[unit["source_cue"]]
    deterministic_payload = {
        key: value for key, value in report.items() if key != "deterministic_payload_sha256"
    }
    report["deterministic_payload_sha256"] = cue_shadow._canonical_sha256(deterministic_payload)
    result = challenge.evaluate_report(
        report,
        report_sha256="sha256:" + "5" * 64,
        truth=truth,
        truth_summary=summary,
    )
    diagnostic = result["reviewed_development_enrollment_signal"]
    assert diagnostic["production_promotion_authorized"] is False
    assert diagnostic["thresholds_selected_after_truth_are_oracle_only"] is True
    by_name = {row["metric"]: row for row in diagnostic["metrics"]}
    median = by_name["reviewed_bank_median"]["overall"]
    assert median["zero_false_host_oracle"]["host_recall_upper_bound"] == 1.0
    maximum = by_name["reviewed_bank_max"]["overall"]
    assert maximum["frozen_provisional_host_threshold"]["false_host_count"] == 1


def test_cue_aligned_persisted_prediction_drift_is_rejected(tmp_path: Path) -> None:
    truth, summary = challenge.load_reviewed_truth(_override(tmp_path / "truth.json"))
    report = _cue_report()
    report["units"][0]["predictions"][cue_shadow.STRATEGY_MAX] = ho.LABEL_OTHER
    deterministic_payload = {
        key: value for key, value in report.items() if key != "deterministic_payload_sha256"
    }
    report["deterministic_payload_sha256"] = cue_shadow._canonical_sha256(deterministic_payload)
    with pytest.raises(challenge.ChallengeEvaluationError, match="prediction drift"):
        challenge.evaluate_report(
            report,
            report_sha256="sha256:" + "4" * 64,
            truth=truth,
            truth_summary=summary,
        )
