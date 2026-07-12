import hashlib
import json
from pathlib import Path

import pytest

from scripts import free_session_autoslice as runner
from scripts import produce_slice_package as producer
from scripts import score_blind_subtitle as scorer


def test_withheld_mode_hides_candidate_truth_and_changes_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    override = tmp_path / "assets/lidousha/subtitle_text_overrides/auto_blind.text.v1.json"
    regression = tmp_path / "assets/lidousha/subtitle_regressions/auto_blind.subtitle-regression.v1.json"
    override.parent.mkdir(parents=True)
    regression.parent.mkdir(parents=True)
    override.write_text("{}\n", encoding="utf-8")
    regression.write_text("{}\n", encoding="utf-8")

    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "delivery")
    delivery_fingerprint = runner.talk_pipeline_fingerprint("auto_blind")
    assert runner.candidate_text_override_path("auto_blind") == override
    assert runner.candidate_subtitle_regression_path("auto_blind") == regression

    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")
    assert runner.candidate_text_override_path("auto_blind") is None
    assert runner.candidate_subtitle_regression_path("auto_blind") is None
    assert runner.talk_pipeline_fingerprint("auto_blind") != delivery_fingerprint


def test_withheld_mode_disables_reviewed_timely_terms_without_machine_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(runner, "BASE", tmp_path / "runtime")
    monkeypatch.setattr(runner, "CPA_ENV", tmp_path / "missing.env")
    reviewed = tmp_path / "repo/assets/lidousha/timely_terms.json"
    reviewed.parent.mkdir(parents=True)
    reviewed.write_text('{"reviewed":"梦限大"}\n', encoding="utf-8")
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")
    monkeypatch.delenv("AUTOSLICE_BLIND_TIMELY_TERMS", raising=False)

    env = runner.child_env_for_date("2026-07-10")

    assert env["LIDOUSHA_DISABLE_TIMELY_TERMS"] == "1"
    assert "LIDOUSHA_TIMELY_TERMS" not in env


def test_withheld_mode_accepts_explicit_machine_only_crawler_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CPA_ENV", tmp_path / "missing.env")
    machine_snapshot = tmp_path / "machine-only.json"
    machine_snapshot.write_text('{"machine":true}\n', encoding="utf-8")
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")
    monkeypatch.setenv("AUTOSLICE_BLIND_TIMELY_TERMS", str(machine_snapshot))

    env = runner.child_env_for_date("2026-07-10")

    assert env["LIDOUSHA_TIMELY_TERMS"] == str(machine_snapshot.resolve())
    assert "LIDOUSHA_DISABLE_TIMELY_TERMS" not in env


def test_blind_generator_fails_closed_if_truth_path_leaks_into_spec(tmp_path, monkeypatch):
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "candidate_id": "auto_blind",
                "human_truth_mode": "withheld",
                "subtitle_text_overrides": "/forbidden/human-truth.json",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")

    with pytest.raises(ValueError, match="refuses human-truth inputs"):
        producer.main(["--spec", str(spec), "--ssh-host", "localhost"])


def test_post_hoc_scorer_reads_truth_only_after_blind_outputs_exist(tmp_path):
    candidate_id = "auto_blind"
    text_srt = tmp_path / "final-text.srt"
    speaker_srt = tmp_path / "final-speaker.srt"
    truth = tmp_path / "truth.json"
    output = tmp_path / "audit.json"
    text_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n梦限大\n", encoding="utf-8")
    speaker_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] 梦限大\n", encoding="utf-8")
    truth.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-subtitle-regression.v1",
                "candidate_id": candidate_id,
                "required_payload_substrings": ["梦限大"],
                "forbidden_payload_substrings": ["mujica"],
                "forbidden_exact_cues": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert scorer.main(
        [
            "--candidate-id",
            candidate_id,
            "--final-text-srt",
            str(text_srt),
            "--final-speaker-srt",
            str(speaker_srt),
            "--truth-asset",
            str(truth),
            "--output",
            str(output),
        ]
    ) == 0
    audit = json.loads(output.read_text(encoding="utf-8"))
    assert audit["status"] == "PASS"
    assert audit["evaluation_mode"] == "post_hoc_withheld_truth"
    assert audit["final_text_srt_sha256"] == hashlib.sha256(text_srt.read_bytes()).hexdigest()
