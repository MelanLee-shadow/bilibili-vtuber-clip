
"""OSS-only synthetic fixture helper for producer pipeline tests."""


def _operator_text_owned_spec() -> dict:
    baseline_sha = "ab" * 32
    pipeline_sha = "cd" * 32
    diagnostic_sha = "fa" * 32
    ledger_sha = "de" * 32
    baseline = {
        "schema_version": "subtitle-redelivery-baseline.v2",
        "mode": "preserve_text_outside_source_truth",
        "exact_interval_replay": True,
        "path": "/tmp/synthetic.reviewed.srt",
        "sha256": baseline_sha,
        "authority": "OSS synthetic reviewed truth",
        "source_recording_basename": "synthetic-recording.mp4",
        "source_sha256": "12" * 32,
        "absolute_source_start_ms": 1323000,
        "absolute_source_end_ms": 1603000,
        "operator_text_full_ownership": {
            "schema_version": "operator-reviewed-text-full-ownership-pin.v2",
            "baseline_sha256": baseline_sha,
            "pipeline_srt_sha256": pipeline_sha,
            "decision_ledger_sha256": ledger_sha,
            "diagnostic_diff_sha256": diagnostic_sha,
            "operator_authority": {
                "kind": "REVIEWER_OPERATOR",
                "evidence_ref": "review: synthetic exhaustive ruling",
            },
            "cue_count": 5,
            "changed_cue_count": 2,
            "operator_exact_text_cue_count": 2,
            "operator_unchanged_freeze_cue_count": 3,
            "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
        },
        "operator_truth_lanes": {
            "schema_version": "operator-reviewed-subtitle-truth-lanes.v1",
            "release_truth": {"srt_sha256": baseline_sha},
            "pipeline_diagnostic": {
                "path": "/tmp/synthetic.pipeline.srt",
                "sha256": pipeline_sha,
            },
            "decision_ledger": {
                "path": "/tmp/synthetic.decisions.json",
                "sha256": ledger_sha,
            },
            "diff_receipt": {
                "path": "/tmp/synthetic.diff.json",
                "sha256": diagnostic_sha,
            },
        },
    }
    return {"subtitle_redelivery_baseline": baseline}
