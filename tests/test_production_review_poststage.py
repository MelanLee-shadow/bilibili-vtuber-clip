import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

import pytest

import scripts.audit_review_package as package_audit
import scripts.build_daily_review_manifest as daily_manifest
import scripts.session_autoslice as runner
from src.autoslice import final_media_review_poststage
from src.autoslice import review_package_poststage as poststage


DATE = "2026-08-21"
CID = "talk_100_200"
TERMINAL_DATE = "2026-08-20"
TERMINAL_CID = "talk_terminal"


def _setup_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: dict) -> Path:
    base = tmp_path / "runtime"
    repo = tmp_path / "repo"
    (base / "state").mkdir(parents=True)
    (base / "out" / DATE / CID / "replacement_recuts").mkdir(parents=True)
    repo.mkdir()
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n", encoding="utf-8")
    (base / "state" / f"{DATE}.json").write_text(
        json.dumps({"date": DATE, **state}), encoding="utf-8"
    )
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(
        runner,
        "read_state",
        lambda _date: json.loads((base / "state" / f"{DATE}.json").read_text()),
    )
    return base / "out" / DATE / CID / "replacement_recuts"


def _manifest(candidate_id: str = CID, *, generated_at: str = "first") -> dict:
    return {
        "schema_version": "lidousha-daily-review-manifest.v1",
        "generated_by": "build_daily_review_manifest.v1",
        "generated_at": generated_at,
        "date": DATE,
        "status": "review_ready",
        "batch_status": "review_ready",
        "candidate_id": candidate_id,
        "deployed_commit": "a" * 40,
        "run_mode": "PRODUCTION_REVIEW",
        "upload_allowed": False,
        "items": [{"id": candidate_id, "candidate_id": candidate_id, "kind": "talk"}],
    }


def _patch_builder_and_auditor(monkeypatch: pytest.MonkeyPatch, *, audit_passed: bool = True):
    builds: list[str] = []
    audits: list[Path] = []

    def build(package_root, _state_path, _deployed_commit_file, candidate_id):
        builds.append(candidate_id)
        return _manifest(candidate_id, generated_at=f"build-{len(builds)}")

    def audit(package_root):
        audits.append(Path(package_root))
        manifest = json.loads((Path(package_root) / "review_manifest.json").read_text())
        return {
            "passed": audit_passed,
            "candidate_id": manifest["candidate_id"],
            "manifest_generated_at": manifest["generated_at"],
        }

    monkeypatch.setattr(daily_manifest, "build", build)
    monkeypatch.setattr(package_audit, "audit_package", audit)
    return builds, audits


def _setup_terminal_scan_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    base = tmp_path / "runtime"
    repo = tmp_path / "repo"
    state_root = base / "state"
    package_root = base / "out" / TERMINAL_DATE / TERMINAL_CID / "replacement_recuts"
    state_root.mkdir(parents=True)
    package_root.mkdir(parents=True)
    repo.mkdir()
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n", encoding="utf-8")
    states = {
        TERMINAL_DATE: {
            "status": "review_ready_with_failures",
            "picks": [{"candidate_id": TERMINAL_CID, "status": "review_ready", "rc": 0}],
        },
        DATE: {"status": "processing", "picks": []},
    }
    for date, state in states.items():
        (state_root / f"{date}.json").write_text(
            json.dumps({"date": date, **state}), encoding="utf-8"
        )
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "AUTOSLICE_START_DATE", TERMINAL_DATE)
    monkeypatch.setattr(runner, "state_path", lambda date: state_root / f"{date}.json")
    monkeypatch.setattr(
        runner,
        "read_state",
        lambda date: json.loads((state_root / f"{date}.json").read_text()),
    )
    return base, package_root


def _terminal_manifest(*, generated_at: str = "first") -> dict:
    return {
        "schema_version": "lidousha-daily-review-manifest.v1",
        "generated_by": "build_daily_review_manifest.v1",
        "generated_at": generated_at,
        "date": TERMINAL_DATE,
        "status": "review_ready_with_failures",
        "batch_status": "review_ready_with_failures",
        "candidate_id": TERMINAL_CID,
        "deployed_commit": "a" * 40,
    }


def _final_media_job(package_root: Path, candidate_id: str = CID) -> Path:
    verification = package_root / "verification"
    verification.mkdir(parents=True, exist_ok=True)
    path = verification / f"{candidate_id}.final-media-review-job.json"
    path.write_text("{}\n", encoding="utf-8")
    return path


def _terminal_final_media_state(
    *,
    job_path: Path,
    status: str = "COMPLETE",
    content_review_status: str = "PASS",
    state_sha256: str = "c" * 64,
) -> dict[str, object]:
    return {
        "status": status,
        "state_sha256": state_sha256,
        "job_path": str(job_path.resolve()),
        "job_sha256": hashlib.sha256(job_path.read_bytes()).hexdigest(),
        "attempt_count": 1,
        "assessment": {"content_review_status": "UNASSESSED"},
        "result": (
            {"content_review_status": content_review_status}
            if content_review_status in {"PASS", "BLOCK"}
            else None
        ),
    }


def _final_media_projection(
    candidate_id: str,
    state: dict[str, object],
) -> dict[str, object]:
    result = state.get("result")
    content = (
        result.get("content_review_status")
        if isinstance(result, dict)
        else "UNASSESSED"
    )
    return {
        "schema_version": "final-media-poststage-projection.v1",
        "candidate_id": candidate_id,
        "status": "CONSUMED",
        "source": "package_local_final_media_consumer",
        "release_authority": False,
        "provider_calls": 0,
        "package_state_written": True,
        "provider_call_status": "NOT_CALLED",
        "cache_reused": True,
        "state_sha256": state["state_sha256"],
        "attempt_count": 1,
        "reason_codes": [],
        "consumer_state_status": state["status"],
        "content_review_status": content,
    }


def test_processing_and_deploy_tail_do_not_build(tmp_path, monkeypatch):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "processing",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    builds, _ = _patch_builder_and_auditor(monkeypatch)

    poststage.materialize_production_review_packages(DATE, runner)
    assert builds == []
    assert not (package_root / "review_manifest.json").exists()

    (runner.BASE / "deploy.guard").touch()
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: False)
    monkeypatch.setattr(runner, "list_dates", lambda: [DATE])
    monkeypatch.setattr(runner, "process_date", lambda _date: pytest.fail("deploy tail must not process"))
    assert runner.tick() == 0
    assert builds == []


@pytest.mark.parametrize("status", ["review_ready", "review_ready_retry_wait"])
def test_terminal_review_builds_and_audits_and_retry_wait_is_allowed(
    tmp_path, monkeypatch, status
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": status,
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    builds, audits = _patch_builder_and_auditor(monkeypatch)

    poststage.materialize_production_review_packages(DATE, runner)

    assert builds == [CID]
    assert audits == [package_root]
    assert json.loads((package_root / "review_manifest.json").read_text())["candidate_id"] == CID
    assert json.loads((package_root / "package_audit.json").read_text())["passed"] is True


def test_tick_runs_review_poststage_after_process_date(tmp_path, monkeypatch):
    _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    builds, _ = _patch_builder_and_auditor(monkeypatch)
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: False)
    monkeypatch.setattr(runner, "list_dates", lambda: [DATE])
    processed: list[str] = []
    monkeypatch.setattr(runner, "process_date", lambda date: processed.append(date))

    assert runner.tick() == 0
    assert processed == [DATE]
    assert builds == [CID]


def test_tick_does_not_touch_publication_queue_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_runtime(tmp_path, monkeypatch, {"status": "processing", "picks": []})
    monkeypatch.delenv("AUTOSLICE_AUTHORIZED_UPLOAD_QUEUE_ENABLED", raising=False)
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: False)
    monkeypatch.setattr(runner, "list_dates", lambda: [])
    monkeypatch.setattr(
        runner.publication_queue,
        "consume_ready_publication_queue",
        lambda **_kwargs: pytest.fail("publication queue is disabled by default"),
    )

    assert runner.tick() == 0


def test_tick_runs_one_publication_queue_iteration_when_explicitly_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_runtime(tmp_path, monkeypatch, {"status": "processing", "picks": []})
    monkeypatch.setenv("AUTOSLICE_AUTHORIZED_UPLOAD_QUEUE_ENABLED", "1")
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: False)
    monkeypatch.setattr(runner, "list_dates", lambda: [])
    calls: list[dict[str, object]] = []
    heartbeats: list[str] = []
    monkeypatch.setattr(runner, "write_heartbeat", heartbeats.append)

    def consume(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {"status": "NO_READY_ACTION", "side_effect_attempted": False}

    monkeypatch.setattr(
        runner.publication_queue,
        "consume_ready_publication_queue",
        consume,
    )

    assert runner.tick() == 0
    assert calls == [
        {
            "repository_root": runner.REPO_ROOT,
            "runtime_root": runner.BASE,
            "enabled": True,
        }
    ]
    assert len(heartbeats) == 1
    assert "publication_queue=NO_READY_ACTION" in heartbeats[0]


def test_tick_surfaces_unresolved_upload_ledger_without_crashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_runtime(tmp_path, monkeypatch, {"status": "processing", "picks": []})
    monkeypatch.setenv("AUTOSLICE_AUTHORIZED_UPLOAD_QUEUE_ENABLED", "1")
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: False)
    monkeypatch.setattr(runner, "list_dates", lambda: [])
    heartbeats: list[str] = []
    logs: list[str] = []
    monkeypatch.setattr(runner, "write_heartbeat", heartbeats.append)
    monkeypatch.setattr(runner, "log", logs.append)
    monkeypatch.setattr(
        runner.publication_queue,
        "consume_ready_publication_queue",
        lambda **_kwargs: {
            "status": "BLOCKED_LEDGER",
            "reason_codes": ["UPLOAD_ATTEMPT_OUTCOME_UNRESOLVED"],
            "attempt_id": "attempt-crash-window",
            "side_effect_attempted": False,
        },
    )

    assert runner.tick() == 0
    assert len(heartbeats) == 1
    assert "publication_queue=BLOCKED_LEDGER" in heartbeats[0]
    unresolved_logs = [
        row
        for row in logs
        if "UPLOAD_ATTEMPT_OUTCOME_UNRESOLVED" in row
    ]
    assert len(unresolved_logs) == 1
    assert "attempt-crash-window" in unresolved_logs[0]


def test_tick_surfaces_publication_retry_backoff_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_runtime(tmp_path, monkeypatch, {"status": "processing", "picks": []})
    monkeypatch.setenv("AUTOSLICE_AUTHORIZED_UPLOAD_QUEUE_ENABLED", "1")
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: False)
    monkeypatch.setattr(runner, "list_dates", lambda: [])
    heartbeats: list[str] = []
    logs: list[str] = []
    monkeypatch.setattr(runner, "write_heartbeat", heartbeats.append)
    monkeypatch.setattr(runner, "log", logs.append)
    monkeypatch.setattr(
        runner.publication_queue,
        "consume_ready_publication_queue",
        lambda **_kwargs: {
            "status": "RETRY_BACKOFF",
            "backoff": {
                "reason_code": "PUBLICATION_QUEUE_RETRY_BACKOFF",
                "retry_after_seconds": 899,
                "next_attempt_at": "2033-05-18T03:48:20+00:00",
                "failure_count": 1,
            },
            "side_effect_attempted": False,
        },
    )

    assert runner.tick() == 0
    assert len(heartbeats) == 1
    assert "publication_queue=RETRY_BACKOFF" in heartbeats[0]
    retry_logs = [row for row in logs if "PUBLICATION_QUEUE_RETRY_BACKOFF" in row]
    assert len(retry_logs) == 1
    assert "2033-05-18T03:48:20+00:00" in retry_logs[0]
    assert "899" in retry_logs[0]


def test_tick_logs_successful_fallthrough_past_backed_off_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_runtime(tmp_path, monkeypatch, {"status": "processing", "picks": []})
    monkeypatch.setenv("AUTOSLICE_AUTHORIZED_UPLOAD_QUEUE_ENABLED", "1")
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: False)
    monkeypatch.setattr(runner, "list_dates", lambda: [])
    heartbeats: list[str] = []
    logs: list[str] = []
    monkeypatch.setattr(runner, "write_heartbeat", heartbeats.append)
    monkeypatch.setattr(runner, "log", logs.append)
    monkeypatch.setattr(
        runner.publication_queue,
        "consume_ready_publication_queue",
        lambda **_kwargs: {
            "status": "ACTION_COMPLETED",
            "action": {"candidate_id": "older-ready"},
            "skipped_backoff": [
                {
                    "action": {"candidate_id": "newer-backed-off"},
                    "backoff": {
                        "reason_code": "PUBLICATION_QUEUE_RETRY_BACKOFF",
                        "next_attempt_at": "2033-05-18T03:48:20+00:00",
                    },
                }
            ],
            "side_effect_attempted": True,
        },
    )

    assert runner.tick() == 0
    assert len(heartbeats) == 1
    assert "publication_queue=ACTION_COMPLETED" in heartbeats[0]
    assert "publication_skipped_backoff=1" in heartbeats[0]
    queue_logs = [row for row in logs if "newer-backed-off" in row]
    assert len(queue_logs) == 1
    assert "older-ready" in queue_logs[0]
    assert "2033-05-18T03:48:20+00:00" in queue_logs[0]


def test_tick_skips_publication_queue_after_live_mid_tick_yield(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _setup_runtime(tmp_path, monkeypatch, {"status": "processing", "picks": []})
    monkeypatch.setenv("AUTOSLICE_AUTHORIZED_UPLOAD_QUEUE_ENABLED", "1")
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "list_dates", lambda: [DATE])
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: True)
    heartbeats: list[str] = []
    monkeypatch.setattr(runner, "write_heartbeat", heartbeats.append)
    monkeypatch.setattr(
        runner.publication_queue,
        "consume_ready_publication_queue",
        lambda **_kwargs: pytest.fail("live yield must skip publication queue"),
    )

    assert runner.tick() == 0
    assert len(heartbeats) == 1
    assert "live_yield_deferred=" in heartbeats[0]
    assert "publication_queue=" not in heartbeats[0]


def test_second_tick_is_idempotent_when_canonical_audit_still_passes(tmp_path, monkeypatch):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    builds, audits = _patch_builder_and_auditor(monkeypatch)

    poststage.materialize_production_review_packages(DATE, runner)
    manifest_bytes = (package_root / "review_manifest.json").read_bytes()
    audit_bytes = (package_root / "package_audit.json").read_bytes()
    poststage.materialize_production_review_packages(DATE, runner)

    assert builds == [CID]
    assert len(audits) == 2
    assert (package_root / "review_manifest.json").read_bytes() == manifest_bytes
    assert (package_root / "package_audit.json").read_bytes() == audit_bytes


def test_materialize_reaudits_existing_package_and_rebuilds_on_payload_drift(
    tmp_path, monkeypatch
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    builds: list[str] = []
    audits: list[str] = []

    def build(package_root, _state_path, _deployed_commit_file, candidate_id):
        builds.append(candidate_id)
        (Path(package_root) / "payload.bin").write_text(
            f"payload-{len(builds)}", encoding="utf-8"
        )
        return _manifest(generated_at=f"build-{len(builds)}")

    def audit(package_root):
        payload = (Path(package_root) / "payload.bin").read_text(encoding="utf-8")
        audits.append(payload)
        return {"passed": True, "payload": payload}

    monkeypatch.setattr(daily_manifest, "build", build)
    monkeypatch.setattr(package_audit, "audit_package", audit)

    poststage.materialize_production_review_packages(DATE, runner)
    (package_root / "payload.bin").write_text("payload-drifted", encoding="utf-8")
    poststage.materialize_production_review_packages(DATE, runner)

    assert builds == [CID, CID]
    assert audits == ["payload-1", "payload-drifted", "payload-2"]
    assert (package_root / "payload.bin").read_text(encoding="utf-8") == "payload-2"


def test_terminal_backfill_precedes_unfinished_start_date_work_without_processing_terminal(
    tmp_path, monkeypatch
):
    _, package_root = _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    builds: list[str] = []
    audits: list[Path] = []

    def build(package_root, _state_path, _deployed_commit_file, candidate_id):
        builds.append(candidate_id)
        return _terminal_manifest()

    def audit(package_root):
        audits.append(Path(package_root))
        return {"passed": True}

    monkeypatch.setattr(daily_manifest, "build", build)
    monkeypatch.setattr(package_audit, "audit_package", audit)
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: False)
    monkeypatch.setattr(runner, "list_dates", lambda: [DATE])
    processed: list[str] = []
    monkeypatch.setattr(runner, "process_date", lambda date: processed.append(date))

    assert runner.tick() == 0
    assert builds == [TERMINAL_CID]
    assert audits == [package_root]
    assert processed == [DATE]
    assert (package_root / "review_manifest.json").is_file()
    assert (package_root / "package_audit.json").is_file()


def test_terminal_backfill_skips_current_package_without_full_audit_or_build(
    tmp_path, monkeypatch
):
    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = runner.BASE / "out" / TERMINAL_DATE / TERMINAL_CID / "replacement_recuts"
    current_manifest = _terminal_manifest()
    current_manifest["deployed_commit"] = "old-deployed-commit"
    package_root.joinpath("review_manifest.json").write_text(
        json.dumps(current_manifest), encoding="utf-8"
    )
    package_root.joinpath("package_audit.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8"
    )
    monkeypatch.setattr(
        daily_manifest, "build", lambda *_args: pytest.fail("current package must not rebuild")
    )
    monkeypatch.setattr(
        package_audit,
        "audit_package",
        lambda *_args: pytest.fail("terminal scan must not run full audit"),
    )

    poststage.backfill_terminal_review_packages(runner)


def test_terminal_backfill_skips_when_deploy_guard_exists(tmp_path, monkeypatch):
    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    (runner.BASE / "deploy.guard").touch()
    monkeypatch.setattr(
        daily_manifest, "build", lambda *_args: pytest.fail("deploy guard forbids writes")
    )
    monkeypatch.setattr(
        package_audit,
        "audit_package",
        lambda *_args: pytest.fail("deploy guard forbids auditing"),
    )

    poststage.backfill_terminal_review_packages(runner)

    package_root = runner.BASE / "out" / TERMINAL_DATE / TERMINAL_CID / "replacement_recuts"
    assert not package_root.joinpath("review_manifest.json").exists()
    assert not package_root.joinpath("package_audit.json").exists()


def test_recovery_state_is_excluded(tmp_path, monkeypatch):
    _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "run_mode": "RECOVERY_REVIEW",
            "upload_allowed": False,
            "talk_selection_contract": {"mode": "EXACT_CANDIDATE_SET_NO_BACKFILL"},
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    builds, _ = _patch_builder_and_auditor(monkeypatch)

    poststage.materialize_production_review_packages(DATE, runner)

    assert builds == []


def test_failed_audit_is_persisted_without_state_success_or_partial_pass(
    tmp_path, monkeypatch
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready_with_failures",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    builds, _ = _patch_builder_and_auditor(monkeypatch, audit_passed=False)
    state_path = runner.state_path(DATE)
    before = state_path.read_bytes()

    poststage.materialize_production_review_packages(DATE, runner)

    assert builds == [CID]
    assert state_path.read_bytes() == before
    assert json.loads((package_root / "package_audit.json").read_text())["passed"] is False
    assert (package_root / "review_manifest.json").is_file()



def test_new_package_with_explicit_final_media_job_runs_consumer_after_audit(
    tmp_path, monkeypatch
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    _final_media_job(package_root)
    builds, audits = _patch_builder_and_auditor(monkeypatch)
    calls: list[tuple[Path, str, Path]] = []

    def consume(root, candidate_id, *, runtime_root, environment=None):
        del environment
        calls.append((Path(root), candidate_id, Path(runtime_root)))
        return {
            "status": "CONSUMED",
            "consumer_state_status": "COMPLETE",
            "consumer": {"cache_reused": False},
        }

    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        consume,
    )
    logs: list[str] = []
    monkeypatch.setattr(runner, "log", logs.append)

    poststage.materialize_production_review_packages(DATE, runner)

    assert builds == [CID]
    assert audits == [package_root]
    assert calls == [(package_root, CID, runner.BASE)]
    assert logs == [f"final-media consumed for {CID}: COMPLETE"]
    assert not (
        package_root / "verification" / "mechanical-delivery-review.json"
    ).exists()


def test_current_package_with_explicit_job_runs_consumer_without_rebuild(
    tmp_path, monkeypatch
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    builds, audits = _patch_builder_and_auditor(monkeypatch)
    poststage.materialize_production_review_packages(DATE, runner)
    _final_media_job(package_root)
    calls: list[str] = []
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda _root, candidate_id, **_kwargs: (
            calls.append(candidate_id)
            or {
                "status": "CONSUMED",
                "consumer_state_status": "COMPLETE",
                "consumer": {"cache_reused": True},
            }
        ),
    )

    poststage.materialize_production_review_packages(DATE, runner)

    assert builds == [CID]
    assert len(audits) == 2
    assert calls == [CID]


def test_final_media_result_projection_is_durable_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    _final_media_job(package_root)
    _patch_builder_and_auditor(monkeypatch)
    consumer_state = {
        "status": "COMPLETE",
        "state_sha256": "a" * 64,
        "binding_sha256": "b" * 64,
        "attempt_count": 1,
        "next_attempt_at": None,
        "reason_codes": [],
    }
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda *_args, **_kwargs: {
            "status": "CONSUMED",
            "candidate_id": CID,
            "consumer_state_status": "COMPLETE",
            "consumer_content_review_status": "PASS",
            "provider_calls": 1,
            "package_state_written": True,
            "consumer": {
                "state": consumer_state,
                "cache_reused": False,
                "provider_call_status": "RESPONSE_ACCEPTED",
            },
        },
    )
    real_write_state = runner.write_state
    writes: list[str] = []

    def write_state(date: str, state: dict) -> None:
        writes.append(date)
        real_write_state(date, state)

    monkeypatch.setattr(runner, "write_state", write_state)

    poststage.materialize_production_review_packages(DATE, runner)
    poststage.materialize_production_review_packages(DATE, runner)

    date_state = json.loads(runner.state_path(DATE).read_text(encoding="utf-8"))
    projection = date_state["final_media_poststage"][CID]
    assert writes == [DATE]
    assert date_state["status"] == "review_ready"
    assert projection == {
        "schema_version": "final-media-poststage-projection.v1",
        "candidate_id": CID,
        "status": "CONSUMED",
        "source": "package_local_final_media_consumer",
        "release_authority": False,
        "provider_calls": 1,
        "package_state_written": True,
        "provider_call_status": "RESPONSE_ACCEPTED",
        "cache_reused": False,
        "state_sha256": "a" * 64,
        "binding_sha256": "b" * 64,
        "attempt_count": 1,
        "reason_codes": [],
        "consumer_state_status": "COMPLETE",
        "content_review_status": "PASS",
    }


def test_capability_retry_deadline_and_state_identity_are_projected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    _final_media_job(package_root)
    _patch_builder_and_auditor(monkeypatch)
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda *_args, **_kwargs: {
            "status": "CAPABILITY_WAIT",
            "candidate_id": CID,
            "provider_calls": 1,
            "package_state_written": False,
            "capability_bootstrap": {
                "status": "RETRY_WAIT",
                "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
                "run_id": "raw-av-sentinel-deadbeef-20260930T160000Z",
                "challenge_states": {
                    "raw_audio": {
                        "status": "RETRY_WAIT",
                        "attempt_count": 1,
                        "state_sha256": "a" * 64,
                        "next_retry_at": "2026-09-30T17:00:11Z",
                        "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
                    },
                    "continuous_source_video": {
                        "status": "RETRY_WAIT",
                        "attempt_count": 2,
                        "state_sha256": "b" * 64,
                        "next_retry_at": "2026-09-30T17:00:07+00:00",
                        "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
                    },
                },
            },
        },
    )
    real_write_state = runner.write_state
    writes: list[str] = []

    def write_state(date: str, state: dict) -> None:
        writes.append(date)
        real_write_state(date, state)

    monkeypatch.setattr(runner, "write_state", write_state)
    poststage.materialize_production_review_packages(DATE, runner)
    poststage.materialize_production_review_packages(DATE, runner)

    date_state = json.loads(runner.state_path(DATE).read_text(encoding="utf-8"))
    projection = date_state["final_media_poststage"][CID]
    assert writes == [DATE]
    assert projection["status"] == "CAPABILITY_WAIT"
    assert projection["capability_run_id"] == (
        "raw-av-sentinel-deadbeef-20260930T160000Z"
    )
    assert projection["capability_next_attempt_at"] == "2026-09-30T17:00:07Z"
    assert projection["capability_challenges"] == {
        "raw_audio": {
            "status": "RETRY_WAIT",
            "attempt_count": 1,
            "state_sha256": "a" * 64,
            "next_retry_at": "2026-09-30T17:00:11Z",
            "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
        },
        "continuous_source_video": {
            "status": "RETRY_WAIT",
            "attempt_count": 2,
            "state_sha256": "b" * 64,
            "next_retry_at": "2026-09-30T17:00:07Z",
            "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
        },
    }
    assert projection["release_authority"] is False


def test_capability_ambiguous_state_identity_projects_without_retry_deadline():
    projection = poststage._result_projection(
        CID,
        {
            "status": "CAPABILITY_STOPPED",
            "provider_calls": None,
            "package_state_written": False,
            "capability_bootstrap": {
                "status": "DISPATCH_AMBIGUOUS",
                "reason_code": "SENTINEL_DISPATCH_OUTCOME_UNKNOWN",
                "run_id": "raw-av-sentinel-ambiguous",
                "challenge_states": {
                    "raw_audio": {
                        "status": "DISPATCH_AMBIGUOUS",
                        "attempt_count": 1,
                        "state_sha256": "c" * 64,
                        "next_retry_at": None,
                        "reason_code": "SENTINEL_DISPATCH_OUTCOME_UNKNOWN",
                    }
                },
            },
        },
    )

    assert projection["capability_run_id"] == "raw-av-sentinel-ambiguous"
    assert projection["capability_challenges"]["raw_audio"] == {
        "status": "DISPATCH_AMBIGUOUS",
        "attempt_count": 1,
        "state_sha256": "c" * 64,
        "reason_code": "SENTINEL_DISPATCH_OUTCOME_UNKNOWN",
    }
    assert "capability_next_attempt_at" not in projection
    assert projection["provider_calls"] is None
    assert projection["release_authority"] is False


def test_ambiguous_provider_call_count_projects_as_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    _final_media_job(package_root)
    _patch_builder_and_auditor(monkeypatch)
    consumer_state = {
        "status": "DISPATCHING",
        "state_sha256": "c" * 64,
        "binding_sha256": "d" * 64,
        "attempt_count": 1,
        "next_attempt_at": None,
        "reason_codes": [],
        "assessment": {"content_review_status": "UNASSESSED"},
        "result": None,
    }
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda *_args, **_kwargs: {
            "status": "CONSUMED",
            "candidate_id": CID,
            "consumer_state_status": "DISPATCHING",
            "consumer_content_review_status": "UNASSESSED",
            "provider_calls": None,
            "package_state_written": True,
            "consumer": {
                "state": consumer_state,
                "cache_reused": True,
                "provider_called": None,
                "provider_call_status": "AMBIGUOUS_PREVIOUS_DISPATCH",
            },
        },
    )
    real_write_state = runner.write_state
    writes: list[str] = []

    def write_state(date: str, state: dict) -> None:
        writes.append(date)
        real_write_state(date, state)

    monkeypatch.setattr(runner, "write_state", write_state)
    poststage.materialize_production_review_packages(DATE, runner)
    poststage.materialize_production_review_packages(DATE, runner)

    date_state = json.loads(runner.state_path(DATE).read_text(encoding="utf-8"))
    projection = date_state["final_media_poststage"][CID]
    assert writes == [DATE]
    assert projection["provider_calls"] is None
    assert projection["provider_call_status"] == "AMBIGUOUS_PREVIOUS_DISPATCH"
    assert projection["consumer_state_status"] == "DISPATCHING"
    assert projection["state_sha256"] == "c" * 64
    assert projection["release_authority"] is False


def test_final_media_error_projection_is_replaced_after_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    _final_media_job(package_root)
    _patch_builder_and_auditor(monkeypatch)

    def fail(*_args, **_kwargs):
        raise final_media_review_poststage.FinalMediaReviewPoststageError(
            "FINAL_MEDIA_REVIEW_SYNTHETIC_FAILURE",
            "synthetic consumer failure",
        )

    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        fail,
    )
    poststage.materialize_production_review_packages(DATE, runner)
    failed_state = json.loads(runner.state_path(DATE).read_text(encoding="utf-8"))
    failure = failed_state["final_media_poststage"][CID]
    assert failure["status"] == "ERROR"
    assert failure["reason_code"] == "FINAL_MEDIA_REVIEW_SYNTHETIC_FAILURE"
    assert failure["provider_calls"] is None
    assert failure["provider_call_status"] == "UNKNOWN"
    assert failure["package_state_written"] is None
    assert "detail" not in failure
    assert failure["release_authority"] is False

    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda *_args, **_kwargs: {
            "status": "CONSUMED",
            "candidate_id": CID,
            "consumer_state_status": "BLOCKED_INPUT",
            "consumer_content_review_status": "UNASSESSED",
            "provider_calls": 0,
            "package_state_written": True,
            "consumer": {
                "state": {
                    "status": "BLOCKED_INPUT",
                    "attempt_count": 0,
                    "reason_codes": [
                        "FINAL_MEDIA_REVIEW_VISUAL_INPUT_NOT_CONTINUOUS"
                    ],
                },
                "cache_reused": False,
                "provider_call_status": "NOT_CALLED",
            },
        },
    )
    poststage.materialize_production_review_packages(DATE, runner)

    recovered_state = json.loads(runner.state_path(DATE).read_text(encoding="utf-8"))
    recovered = recovered_state["final_media_poststage"][CID]
    assert recovered["status"] == "CONSUMED"
    assert recovered["consumer_state_status"] == "BLOCKED_INPUT"
    assert recovered["content_review_status"] == "UNASSESSED"
    assert recovered["reason_codes"] == [
        "FINAL_MEDIA_REVIEW_VISUAL_INPUT_NOT_CONTINUOUS"
    ]
    assert "detail" not in recovered
    assert recovered["release_authority"] is False


def test_failed_audit_does_not_schedule_explicit_final_media_job(
    tmp_path, monkeypatch
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    _final_media_job(package_root)
    builds, _audits = _patch_builder_and_auditor(
        monkeypatch, audit_passed=False
    )
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda *_args, **_kwargs: pytest.fail(
            "failed canonical audit must stop final-media scheduling"
        ),
    )

    poststage.materialize_production_review_packages(DATE, runner)

    assert builds == [CID]


def test_package_without_final_media_job_preserves_original_poststage(
    tmp_path, monkeypatch
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    builds, audits = _patch_builder_and_auditor(monkeypatch)
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda *_args, **_kwargs: pytest.fail(
            "ordinary package without job must not enter final-media consumer"
        ),
    )

    poststage.materialize_production_review_packages(DATE, runner)

    assert builds == [CID]
    assert audits == [package_root]


def test_capability_wait_logs_without_state_or_mechanical_receipt(
    tmp_path, monkeypatch
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    _final_media_job(package_root)
    _patch_builder_and_auditor(monkeypatch)
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda *_args, **_kwargs: {
            "status": "CAPABILITY_WAIT",
            "capability_bootstrap": {
                "status": "RETRY_WAIT",
                "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
            },
        },
    )
    logs: list[str] = []
    monkeypatch.setattr(runner, "log", logs.append)

    poststage.materialize_production_review_packages(DATE, runner)

    assert logs == [
        f"final-media capability_wait for {CID}: "
        "SENTINEL_PROVIDER_RATE_LIMITED"
    ]
    verification = package_root / "verification"
    assert not (
        verification / f"{CID}.final-media-review-state.json"
    ).exists()
    assert not (verification / "mechanical-delivery-review.json").exists()



def test_real_runner_poststage_stops_without_sentinel_capability(
    tmp_path, monkeypatch
):
    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    builds, audits = _patch_builder_and_auditor(monkeypatch)
    verification = package_root / "verification"
    verification.mkdir()
    video = package_root / "synthetic-final.mp4"
    video.write_bytes(b"synthetic exact final video bytes")
    audio = verification / "synthetic-full-final-media.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\0\0" * 32_000)

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    manifest = {
        "schema": "synthetic-review-assets.v1",
        "source_video": {
            "path": str(video.resolve()),
            "sha256": sha(video),
            "bytes": video.stat().st_size,
        },
        "artifacts": [
            {
                "path": str(audio.resolve()),
                "sha256": sha(audio),
                "bytes": audio.stat().st_size,
                "kind": "exact_full_audio_wav",
                "window": "full-final-media",
                "range_seconds": [0.0, 2.0],
                "actual_duration_us": 2_000_000,
                "sample_rate": 16_000,
                "sample_frames": 32_000,
                "source_video_sha256": sha(video),
            }
        ],
    }
    manifest_path = verification / "synthetic-review-assets.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    job_path = verification / f"{CID}.final-media-review-job.json"
    job = {
        "schema_version": "final-media-review-input-job.v1",
        "candidate_id": CID,
        "asset_manifest": {
            "path": str(manifest_path.resolve()),
            "sha256": sha(manifest_path),
        },
        "media_clock": {
            "source_video_sha256": sha(video),
            "source_video_bytes": video.stat().st_size,
            "duration_us": 2_000_000,
            "first_video_pts_us": 0,
            "last_video_pts_us": 1_960_000,
            "video_frame_count": 50,
        },
        "requirements": {
            "continuous_audio_required": True,
            "continuous_visual_required": True,
            "content_review_required": True,
            "exact_media_clock_evidence_required": False,
            "provider_accepts_bound_source_video": False,
            "provider_accepts_bound_audio": False,
        },
        "review_plan": {
            "authority": "SYNTHETIC_RUNNER_POSTSTAGE_TEST_ONLY",
            "review_points": [
                {
                    "point_id": "whole-final-media",
                    "final_video_start_ms": 0,
                    "final_video_end_ms": 2_000,
                    "expectation": "inspect exact final audio and video",
                }
            ],
        },
    }
    job_path.write_text(
        json.dumps(job, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logs: list[str] = []
    monkeypatch.setattr(runner, "log", logs.append)

    poststage.materialize_production_review_packages(DATE, runner)

    state_path = verification / f"{CID}.final-media-review-state.json"
    projection = json.loads(runner.state_path(DATE).read_text())[
        "final_media_poststage"
    ][CID]
    assert builds == [CID]
    assert audits == [package_root]
    assert projection == {
        "schema_version": "final-media-poststage-projection.v1",
        "candidate_id": CID,
        "status": "CAPABILITY_STOPPED",
        "source": "package_local_final_media_consumer",
        "release_authority": False,
        "provider_calls": 0,
        "package_state_written": False,
        "capability_status": "SENTINEL_RUNTIME_ABSENT",
    }
    assert not state_path.exists()
    assert logs == [
        f"final-media capability_stopped for {CID}: typed runtime status"
    ]
    assert not (runner.BASE / "model-capability-sentinel-runs").exists()
    assert not (verification / "mechanical-delivery-review.json").exists()


def _write_terminal_current_package(package_root: Path) -> None:
    current_manifest = _terminal_manifest()
    current_manifest["deployed_commit"] = "old-deployed-commit"
    package_root.joinpath("review_manifest.json").write_text(
        json.dumps(current_manifest), encoding="utf-8"
    )
    package_root.joinpath("package_audit.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8"
    )


def test_normal_tick_restarts_terminal_final_media_wait_then_dedupes_complete(
    tmp_path, monkeypatch
):
    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = (
        runner.BASE
        / "out"
        / TERMINAL_DATE
        / TERMINAL_CID
        / "replacement_recuts"
    )
    _write_terminal_current_package(package_root)
    job_path = _final_media_job(package_root, TERMINAL_CID)
    state_path = (
        package_root
        / "verification"
        / f"{TERMINAL_CID}.final-media-review-state.json"
    )
    package_state = _terminal_final_media_state(
        job_path=job_path,
        state_sha256="3" * 64,
    )
    calls: list[str] = []

    def consume(_root, candidate_id, **_kwargs):
        calls.append(candidate_id)
        if len(calls) == 1:
            return {
                "status": "CAPABILITY_WAIT",
                "candidate_id": candidate_id,
                "job_path": str(job_path),
                "job_sha256": hashlib.sha256(job_path.read_bytes()).hexdigest(),
                "provider_calls": 0,
                "package_state_written": False,
                "capability_bootstrap": {
                    "status": "PREPARED",
                    "provider_calls": 0,
                    "reason_code": "SENTINEL_PROVIDER_CAPACITY_UNAVAILABLE",
                    "challenge_states": {},
                },
            }
        state_path.write_text(json.dumps(package_state) + "\n", encoding="utf-8")
        return {
            "status": "CONSUMED",
            "candidate_id": candidate_id,
            "job_path": str(job_path),
            "job_sha256": hashlib.sha256(job_path.read_bytes()).hexdigest(),
            "provider_calls": 0,
            "package_state_written": True,
            "consumer_state_status": "COMPLETE",
            "consumer_content_review_status": "PASS",
            "consumer": {
                "cache_reused": True,
                "provider_called": False,
                "provider_call_status": "NOT_CALLED",
                "state": package_state,
            },
        }

    monkeypatch.setattr(
        daily_manifest,
        "build",
        lambda *_args: pytest.fail("current package must not rebuild"),
    )
    monkeypatch.setattr(
        package_audit,
        "audit_package",
        lambda *_args: pytest.fail("current package must not reaudit"),
    )
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        consume,
    )
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(
        runner,
        "_live_hold_active",
        lambda _live, **_kwargs: False,
    )
    monkeypatch.setattr(runner, "list_dates", lambda: [])
    monkeypatch.setattr(
        runner,
        "process_date",
        lambda _date: pytest.fail("empty active date set must not process"),
    )
    monkeypatch.setattr(
        runner.runner_publication_queue,
        "consume_tick",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(runner, "write_heartbeat", lambda _body: None)
    monkeypatch.setattr(runner, "log", lambda _body: None)

    assert runner.tick() == 0
    first = json.loads(runner.state_path(TERMINAL_DATE).read_text())[
        "final_media_poststage"
    ][TERMINAL_CID]
    assert first["status"] == "CAPABILITY_WAIT"
    assert first["capability_status"] == "PREPARED"
    assert first["provider_calls"] == 0
    assert first["package_state_written"] is False
    assert calls == [TERMINAL_CID]
    assert not state_path.exists()

    assert runner.tick() == 0
    second = json.loads(runner.state_path(TERMINAL_DATE).read_text())[
        "final_media_poststage"
    ][TERMINAL_CID]
    assert second == _final_media_projection(TERMINAL_CID, package_state)
    assert calls == [TERMINAL_CID, TERMINAL_CID]
    assert json.loads(state_path.read_text()) == package_state

    assert runner.tick() == 0
    third = json.loads(runner.state_path(TERMINAL_DATE).read_text())[
        "final_media_poststage"
    ][TERMINAL_CID]
    assert third == second
    assert calls == [TERMINAL_CID, TERMINAL_CID]


def test_runner_tick_process_restart_recovers_terminal_final_media_and_dedupes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, package_root = _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    _write_terminal_current_package(package_root)
    job_path = _final_media_job(package_root, TERMINAL_CID)
    package_state_path = (
        package_root
        / "verification"
        / f"{TERMINAL_CID}.final-media-review-state.json"
    )
    phase_ledger = tmp_path / "runner-process-consumer-calls.jsonl"
    worker = tmp_path / "runner-tick-process-worker.py"
    worker.write_text(
        r'''from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

import scripts.audit_review_package as package_audit
import scripts.build_daily_review_manifest as daily_manifest
import scripts.session_autoslice as runner
from src.autoslice import final_media_review_poststage

base = Path(sys.argv[1])
repository = Path(sys.argv[2])
package_root = Path(sys.argv[3])
job_path = Path(sys.argv[4])
package_state_path = Path(sys.argv[5])
phase = sys.argv[6]
ledger = Path(sys.argv[7])
candidate_id = "talk_terminal"

runner.BASE = base
runner.REPO_ROOT = repository
runner.AUTOSLICE_START_DATE = "2026-08-20"
runner.cjk_font_present = lambda: True
runner.source_health_error = lambda: None
runner.recorder_live_status = lambda: False
runner.live_determination_basis = lambda _live: {}
runner._live_hold_active = lambda _live, **_kwargs: False
runner.list_dates = lambda: []
runner.process_date = lambda _date: (_ for _ in ()).throw(
    AssertionError("empty active date set must not process")
)
runner.runner_publication_queue.consume_tick = lambda *_args, **_kwargs: ""
runner.write_heartbeat = lambda _body: None
runner.log = lambda _body: None

daily_manifest.build = lambda *_args: (_ for _ in ()).throw(
    AssertionError("current terminal package must not rebuild")
)
package_audit.audit_package = lambda *_args: (_ for _ in ()).throw(
    AssertionError("current terminal package must not reaudit")
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def consume(_root, observed_candidate_id, **_kwargs):
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "candidate_id": observed_candidate_id,
                    "phase": phase,
                    "pid": os.getpid(),
                },
                sort_keys=True,
            )
            + "\n"
        )
    if phase == "wait":
        return {
            "status": "CAPABILITY_WAIT",
            "candidate_id": observed_candidate_id,
            "job_path": str(job_path),
            "job_sha256": sha(job_path),
            "provider_calls": 0,
            "package_state_written": False,
            "capability_bootstrap": {
                "status": "PREPARED",
                "provider_calls": 0,
                "reason_code": "SENTINEL_PROVIDER_CAPACITY_UNAVAILABLE",
                "challenge_states": {},
            },
        }
    if phase != "complete":
        raise AssertionError(f"consumer replayed during {phase}")
    package_state = {
        "status": "COMPLETE",
        "state_sha256": "4" * 64,
        "job_path": str(job_path.resolve()),
        "job_sha256": sha(job_path),
        "attempt_count": 1,
        "assessment": {"content_review_status": "UNASSESSED"},
        "result": {"content_review_status": "PASS"},
    }
    package_state_path.write_text(
        json.dumps(package_state, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "CONSUMED",
        "candidate_id": observed_candidate_id,
        "job_path": str(job_path),
        "job_sha256": sha(job_path),
        "provider_calls": 0,
        "package_state_written": True,
        "consumer_state_status": "COMPLETE",
        "consumer_content_review_status": "PASS",
        "consumer": {
            "cache_reused": True,
            "provider_called": False,
            "provider_call_status": "NOT_CALLED",
            "state": package_state,
        },
    }


final_media_review_poststage.consume_configured_final_media_review = consume
return_code = runner.tick()
date_state = json.loads((base / "state/2026-08-20.json").read_text())
print(
    json.dumps(
        {
            "date_state": date_state,
            "phase": phase,
            "pid": os.getpid(),
            "return_code": return_code,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
)
''',
        encoding="utf-8",
    )
    repository_root = Path(__file__).resolve().parents[1]
    process_environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(repository_root),
    }

    def run_process(phase: str) -> dict[str, object]:
        completed = subprocess.run(
            [
                sys.executable,
                str(worker),
                str(base),
                str(runner.REPO_ROOT),
                str(package_root),
                str(job_path),
                str(package_state_path),
                phase,
                str(phase_ledger),
            ],
            cwd=repository_root,
            env=process_environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == ""
        value = json.loads(completed.stdout)
        assert value["return_code"] == 0
        assert value["pid"] != os.getpid()
        return value

    first = run_process("wait")
    first_projection = first["date_state"]["final_media_poststage"][TERMINAL_CID]
    assert first_projection["status"] == "CAPABILITY_WAIT"
    assert first_projection["capability_status"] == "PREPARED"
    assert first_projection["provider_calls"] == 0
    assert first_projection["package_state_written"] is False
    assert not package_state_path.exists()

    second = run_process("complete")
    second_projection = second["date_state"]["final_media_poststage"][TERMINAL_CID]
    persisted_package_state = json.loads(package_state_path.read_text(encoding="utf-8"))
    assert second_projection == _final_media_projection(
        TERMINAL_CID,
        persisted_package_state,
    )
    assert persisted_package_state["status"] == "COMPLETE"
    assert persisted_package_state["result"]["content_review_status"] == "PASS"
    second_state_bytes = runner.state_path(TERMINAL_DATE).read_bytes()
    package_state_bytes = package_state_path.read_bytes()

    third = run_process("forbidden-terminal")
    third_projection = third["date_state"]["final_media_poststage"][TERMINAL_CID]
    assert third_projection == second_projection
    assert runner.state_path(TERMINAL_DATE).read_bytes() == second_state_bytes
    assert package_state_path.read_bytes() == package_state_bytes

    calls = [
        json.loads(line)
        for line in phase_ledger.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["phase"] for row in calls] == ["wait", "complete"]
    assert all(row["candidate_id"] == TERMINAL_CID for row in calls)
    assert all(row["pid"] != os.getpid() for row in calls)
    assert [row["phase"] for row in (first, second, third)] == [
        "wait",
        "complete",
        "forbidden-terminal",
    ]


def test_terminal_backfill_wakes_current_package_with_unconsumed_job_without_rebuild(
    tmp_path, monkeypatch
):
    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = (
        runner.BASE
        / "out"
        / TERMINAL_DATE
        / TERMINAL_CID
        / "replacement_recuts"
    )
    _write_terminal_current_package(package_root)
    _final_media_job(package_root, TERMINAL_CID)
    monkeypatch.setattr(
        daily_manifest,
        "build",
        lambda *_args: pytest.fail("current package must not rebuild"),
    )
    audits: list[Path] = []

    def audit(root):
        audits.append(Path(root))
        return {"passed": True}

    monkeypatch.setattr(package_audit, "audit_package", audit)
    calls: list[str] = []
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda _root, candidate_id, **_kwargs: (
            calls.append(candidate_id)
            or {
                "status": "CONSUMED",
                "consumer_state_status": "COMPLETE",
                "consumer": {"cache_reused": True},
            }
        ),
    )

    poststage.backfill_terminal_review_packages(runner)

    assert audits == []
    assert calls == [TERMINAL_CID]


def test_terminal_backfill_reconciles_completed_final_media_projection_once(
    tmp_path, monkeypatch
):
    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = (
        runner.BASE
        / "out"
        / TERMINAL_DATE
        / TERMINAL_CID
        / "replacement_recuts"
    )
    _write_terminal_current_package(package_root)
    _final_media_job(package_root, TERMINAL_CID)
    state_path = (
        package_root
        / "verification"
        / f"{TERMINAL_CID}.final-media-review-state.json"
    )
    package_state = _terminal_final_media_state(
        job_path=state_path.with_name(
            f"{TERMINAL_CID}.final-media-review-job.json"
        )
    )
    state_path.write_text(
        json.dumps(package_state) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        daily_manifest,
        "build",
        lambda *_args: pytest.fail("completed final-media package must not rebuild"),
    )
    monkeypatch.setattr(
        package_audit,
        "audit_package",
        lambda *_args: pytest.fail("completed final-media package must not reaudit"),
    )
    calls: list[str] = []

    def consume(_root, candidate_id, **_kwargs):
        calls.append(candidate_id)
        return {
            "status": "CONSUMED",
            "candidate_id": candidate_id,
            "consumer_state_status": "COMPLETE",
            "consumer_content_review_status": "PASS",
            "provider_calls": 0,
            "package_state_written": True,
            "consumer": {
                "state": package_state,
                "cache_reused": True,
                "provider_called": False,
                "provider_call_status": "NOT_CALLED",
            },
        }

    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        consume,
    )

    poststage.backfill_terminal_review_packages(runner)
    poststage.backfill_terminal_review_packages(runner)

    persisted = json.loads(runner.state_path(TERMINAL_DATE).read_text())
    assert calls == [TERMINAL_CID]
    assert persisted["final_media_poststage"][TERMINAL_CID] == (
        _final_media_projection(TERMINAL_CID, package_state)
    )


def test_terminal_backfill_reconciles_stale_terminal_projection(
    tmp_path, monkeypatch
):
    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = (
        runner.BASE
        / "out"
        / TERMINAL_DATE
        / TERMINAL_CID
        / "replacement_recuts"
    )
    _write_terminal_current_package(package_root)
    _final_media_job(package_root, TERMINAL_CID)
    state_path = (
        package_root
        / "verification"
        / f"{TERMINAL_CID}.final-media-review-state.json"
    )
    package_state = _terminal_final_media_state(
        job_path=state_path.with_name(
            f"{TERMINAL_CID}.final-media-review-job.json"
        ),
        state_sha256="d" * 64,
    )
    state_path.write_text(json.dumps(package_state) + "\n", encoding="utf-8")
    date_path = runner.state_path(TERMINAL_DATE)
    date_state = json.loads(date_path.read_text())
    stale = _final_media_projection(TERMINAL_CID, package_state)
    stale["state_sha256"] = "e" * 64
    date_state["final_media_poststage"] = {TERMINAL_CID: stale}
    date_path.write_text(json.dumps(date_state), encoding="utf-8")
    monkeypatch.setattr(
        daily_manifest,
        "build",
        lambda *_args: pytest.fail("terminal projection repair must not rebuild"),
    )
    monkeypatch.setattr(
        package_audit,
        "audit_package",
        lambda *_args: pytest.fail("terminal projection repair must not reaudit"),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda _root, candidate_id, **_kwargs: (
            calls.append(candidate_id)
            or {
                "status": "CONSUMED",
                "candidate_id": candidate_id,
                "consumer_state_status": "COMPLETE",
                "consumer_content_review_status": "PASS",
                "provider_calls": 0,
                "package_state_written": True,
                "consumer": {
                    "state": package_state,
                    "cache_reused": True,
                    "provider_called": False,
                    "provider_call_status": "NOT_CALLED",
                },
            }
        ),
    )

    poststage.backfill_terminal_review_packages(runner)

    persisted = json.loads(date_path.read_text())
    assert calls == [TERMINAL_CID]
    assert persisted["final_media_poststage"][TERMINAL_CID]["state_sha256"] == (
        "d" * 64
    )


def test_terminal_backfill_reconciles_changed_active_job_without_provider_replay(
    tmp_path, monkeypatch
):
    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = (
        runner.BASE
        / "out"
        / TERMINAL_DATE
        / TERMINAL_CID
        / "replacement_recuts"
    )
    _write_terminal_current_package(package_root)
    job_path = _final_media_job(package_root, TERMINAL_CID)
    state_path = (
        package_root
        / "verification"
        / f"{TERMINAL_CID}.final-media-review-state.json"
    )
    old_state = _terminal_final_media_state(job_path=job_path)
    state_path.write_text(json.dumps(old_state) + "\n", encoding="utf-8")
    date_path = runner.state_path(TERMINAL_DATE)
    date_state = json.loads(date_path.read_text())
    date_state["final_media_poststage"] = {
        TERMINAL_CID: _final_media_projection(TERMINAL_CID, old_state)
    }
    date_path.write_text(json.dumps(date_state), encoding="utf-8")
    job_path.write_text('{"successor":true}\n', encoding="utf-8")
    successor_state = _terminal_final_media_state(
        job_path=job_path,
        state_sha256="2" * 64,
    )
    monkeypatch.setattr(
        daily_manifest,
        "build",
        lambda *_args: pytest.fail("job-binding reconciliation must not rebuild"),
    )
    monkeypatch.setattr(
        package_audit,
        "audit_package",
        lambda *_args: pytest.fail("job-binding reconciliation must not reaudit"),
    )
    calls: list[str] = []

    def consume(_root, candidate_id, **_kwargs):
        calls.append(candidate_id)
        state_path.write_text(json.dumps(successor_state) + "\n", encoding="utf-8")
        return {
            "status": "CONSUMED",
            "candidate_id": candidate_id,
            "consumer_state_status": "COMPLETE",
            "consumer_content_review_status": "PASS",
            "provider_calls": 0,
            "package_state_written": True,
            "consumer": {
                "state": successor_state,
                "cache_reused": True,
                "provider_called": False,
                "provider_call_status": "NOT_CALLED",
            },
        }

    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        consume,
    )

    poststage.backfill_terminal_review_packages(runner)
    poststage.backfill_terminal_review_packages(runner)

    persisted = json.loads(date_path.read_text())
    assert calls == [TERMINAL_CID]
    assert persisted["final_media_poststage"][TERMINAL_CID]["state_sha256"] == (
        "2" * 64
    )


def test_terminal_backfill_skips_matching_completed_projection(
    tmp_path, monkeypatch
):
    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = (
        runner.BASE
        / "out"
        / TERMINAL_DATE
        / TERMINAL_CID
        / "replacement_recuts"
    )
    _write_terminal_current_package(package_root)
    _final_media_job(package_root, TERMINAL_CID)
    state_path = (
        package_root
        / "verification"
        / f"{TERMINAL_CID}.final-media-review-state.json"
    )
    package_state = _terminal_final_media_state(
        job_path=state_path.with_name(
            f"{TERMINAL_CID}.final-media-review-job.json"
        )
    )
    state_path.write_text(json.dumps(package_state) + "\n", encoding="utf-8")
    date_path = runner.state_path(TERMINAL_DATE)
    date_state = json.loads(date_path.read_text())
    date_state["final_media_poststage"] = {
        TERMINAL_CID: _final_media_projection(TERMINAL_CID, package_state)
    }
    date_path.write_text(json.dumps(date_state), encoding="utf-8")
    monkeypatch.setattr(
        daily_manifest,
        "build",
        lambda *_args: pytest.fail("matching terminal projection must not rebuild"),
    )
    monkeypatch.setattr(
        package_audit,
        "audit_package",
        lambda *_args: pytest.fail("matching terminal projection must not reaudit"),
    )
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda *_args, **_kwargs: pytest.fail(
            "matching terminal projection must not reschedule consumer"
        ),
    )

    poststage.backfill_terminal_review_packages(runner)


def test_terminal_blocked_input_without_projection_is_consumed_once(
    tmp_path, monkeypatch
):
    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = (
        runner.BASE
        / "out"
        / TERMINAL_DATE
        / TERMINAL_CID
        / "replacement_recuts"
    )
    _write_terminal_current_package(package_root)
    _final_media_job(package_root, TERMINAL_CID)
    state_path = (
        package_root
        / "verification"
        / f"{TERMINAL_CID}.final-media-review-state.json"
    )
    package_state = _terminal_final_media_state(
        job_path=state_path.with_name(
            f"{TERMINAL_CID}.final-media-review-job.json"
        ),
        status="BLOCKED_INPUT",
        content_review_status="UNASSESSED",
        state_sha256="f" * 64,
    )
    package_state["reason_codes"] = [
        "FINAL_MEDIA_REVIEW_VISUAL_INPUT_NOT_CONTINUOUS"
    ]
    state_path.write_text(json.dumps(package_state) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        daily_manifest,
        "build",
        lambda *_args: pytest.fail("blocked projection repair must not rebuild"),
    )
    monkeypatch.setattr(
        package_audit,
        "audit_package",
        lambda *_args: pytest.fail("blocked projection repair must not reaudit"),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda _root, candidate_id, **_kwargs: (
            calls.append(candidate_id)
            or {
                "status": "CONSUMED",
                "candidate_id": candidate_id,
                "consumer_state_status": "BLOCKED_INPUT",
                "consumer_content_review_status": "UNASSESSED",
                "provider_calls": 0,
                "package_state_written": True,
                "consumer": {
                    "state": package_state,
                    "cache_reused": True,
                    "provider_called": False,
                    "provider_call_status": "NOT_CALLED",
                },
            }
        ),
    )

    poststage.backfill_terminal_review_packages(runner)
    poststage.backfill_terminal_review_packages(runner)

    assert calls == [TERMINAL_CID]


def test_terminal_blocked_input_wakes_when_runtime_sentinel_appears(
    tmp_path, monkeypatch
):
    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = (
        runner.BASE
        / "out"
        / TERMINAL_DATE
        / TERMINAL_CID
        / "replacement_recuts"
    )
    _write_terminal_current_package(package_root)
    _final_media_job(package_root, TERMINAL_CID)
    state_path = (
        package_root
        / "verification"
        / f"{TERMINAL_CID}.final-media-review-state.json"
    )
    package_state = _terminal_final_media_state(
        job_path=state_path.with_name(
            f"{TERMINAL_CID}.final-media-review-job.json"
        ),
        status="BLOCKED_INPUT",
        content_review_status="UNASSESSED",
        state_sha256="1" * 64,
    )
    package_state["reason_codes"] = [
        "FINAL_MEDIA_REVIEW_VISUAL_INPUT_NOT_CONTINUOUS"
    ]
    state_path.write_text(
        json.dumps(package_state) + "\n",
        encoding="utf-8",
    )
    date_path = runner.state_path(TERMINAL_DATE)
    date_state = json.loads(date_path.read_text())
    date_state["final_media_poststage"] = {
        TERMINAL_CID: _final_media_projection(TERMINAL_CID, package_state)
    }
    date_path.write_text(json.dumps(date_state), encoding="utf-8")
    (runner.BASE / "final-media-review-model-capability-sentinel-runtime.json").write_text(
        "{}\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        daily_manifest,
        "build",
        lambda *_args: pytest.fail("current package must not rebuild"),
    )
    monkeypatch.setattr(package_audit, "audit_package", lambda _root: {"passed": True})
    calls: list[str] = []
    monkeypatch.setattr(
        final_media_review_poststage,
        "consume_configured_final_media_review",
        lambda _root, candidate_id, **_kwargs: (
            calls.append(candidate_id)
            or {
                "status": "CAPABILITY_WAIT",
                "capability_bootstrap": {
                    "status": "RETRY_WAIT",
                    "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
                },
            }
        ),
    )

    poststage.backfill_terminal_review_packages(runner)

    assert calls == [TERMINAL_CID]


def test_normal_runner_auto_bootstraps_duration_job_then_stops_without_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from src.autoslice import final_human_review as human_review
    from src.autoslice import production_final_media_review_admission as admission
    from src.autoslice.final_media_duration_binding import (
        final_burn_duration_binding,
    )
    from src.autoslice.final_media_review_materialization import (
        resolve_or_materialize_review_job as real_resolver,
    )

    package_root = _setup_runtime(
        tmp_path,
        monkeypatch,
        {
            "status": "review_ready",
            "picks": [{"candidate_id": CID, "status": "review_ready", "rc": 0}],
        },
    )
    video = package_root / "final.mp4"
    subtitle = package_root / "final.srt"
    cover = package_root / "final.cover.png"
    record_path = package_root / "final.record.json"
    video.write_bytes(b"synthetic final media bytes")
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n",
        encoding="utf-8",
    )
    cover.write_bytes(b"synthetic cover bytes")
    record = {
        "candidate_id": CID,
        "burned_preview": {
            "verification": {"duration_ms": 2_000},
            "branding_intro": {
                "verification": {"duration_ms": 19_546}
            },
        },
    }
    record_path.write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    manifest = {
        "schema_version": "lidousha-daily-review-manifest.v1",
        "generated_by": "build_daily_review_manifest.v1",
        "generated_at": "synthetic",
        "date": DATE,
        "status": "review_ready",
        "batch_status": "review_ready",
        "candidate_id": CID,
        "deployed_commit": "a" * 40,
        "run_mode": "PRODUCTION_REVIEW",
        "upload_allowed": False,
        "items": [
            {
                "candidate_id": CID,
                "kind": "talk",
                "classification": "talk",
                "video": video.name,
                "subtitle_srt": subtitle.name,
                "cover": cover.name,
                "record": record_path.name,
                "final_media_duration_binding": final_burn_duration_binding(
                    record["burned_preview"]
                ),
                "sha256": {
                    "video": sha(video),
                    "subtitle_srt": sha(subtitle),
                    "cover": sha(cover),
                    "evidence_json": sha(record_path),
                },
            }
        ],
    }
    builds: list[str] = []
    audits: list[Path] = []

    def build(_root, _state, _commit, candidate_id):
        builds.append(candidate_id)
        return manifest

    def audit(root):
        audits.append(Path(root))
        return {"passed": True, "candidate_id": CID}

    monkeypatch.setattr(daily_manifest, "build", build)
    monkeypatch.setattr(package_audit, "audit_package", audit)
    contract = tmp_path / "final-media-review-contracts.json"
    contract.write_text(
        json.dumps(
            {
                "schema_version": human_review.REVIEW_CONTRACT_SCHEMA,
                "authority": "SYNTHETIC_RUNNER_ADMISSION_TEST_ONLY",
                "contracts": [
                    {
                        "candidate_id": CID,
                        "subtitle_review_points": [
                            {
                                "point_id": "whole-final-media",
                                "final_video_start_ms": 0,
                                "final_video_end_ms": 2_000,
                                "expectation": (
                                    "inspect exact final audio and continuous video"
                                ),
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        human_review, "FINAL_MEDIA_REVIEW_CONTRACT_PATH", contract
    )

    def clock(_video: Path) -> dict[str, object]:
        return {
            "duration_us": 2_000_000,
            "first_video_pts_us": 0,
            "last_video_pts_us": 1_960_000,
            "video_frame_count": 50,
            "format_duration_seconds": "2.000000",
            "stream_start_seconds": "0.000000",
            "stream_duration_seconds": "2.000000",
        }

    monkeypatch.setattr(
        final_media_review_poststage,
        "bootstrap_production_final_media_review_job",
        lambda root, candidate_id: admission.bootstrap_production_final_media_review_job(
            root, candidate_id, probe=clock
        ),
    )

    def extract(_source: Path, output: Path, *, duration_us: int) -> None:
        frames = round(duration_us * 16_000 / 1_000_000)
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16_000)
            handle.writeframes(b"\0\0" * frames)

    monkeypatch.setattr(
        final_media_review_poststage,
        "resolve_or_materialize_review_job",
        lambda job, *, allowed_root: real_resolver(
            job,
            allowed_root=allowed_root,
            extract_full_audio=extract,
        ),
    )
    logs: list[str] = []
    monkeypatch.setattr(runner, "log", logs.append)

    poststage.materialize_production_review_packages(DATE, runner)

    verification = package_root / "verification"
    job_path = verification / f"{CID}.final-media-review-job.json"
    bootstrap_path = verification / f"{CID}.final-media-review-bootstrap.json"
    state_path = verification / f"{CID}.final-media-review-state.json"
    assert builds == [CID]
    assert audits == [package_root]
    assert job_path.is_file() and bootstrap_path.is_file()
    projection = json.loads(runner.state_path(DATE).read_text())[
        "final_media_poststage"
    ][CID]
    assert projection == {
        "schema_version": "final-media-poststage-projection.v1",
        "candidate_id": CID,
        "status": "CAPABILITY_STOPPED",
        "source": "package_local_final_media_consumer",
        "release_authority": False,
        "provider_calls": 0,
        "package_state_written": False,
        "capability_status": "SENTINEL_RUNTIME_ABSENT",
    }
    assert not state_path.exists()
    assert logs == [
        f"final-media capability_stopped for {CID}: typed runtime status"
    ]
    assert not (runner.BASE / "model-capability-sentinel-runs").exists()
    assert not (verification / "mechanical-delivery-review.json").exists()


def _write_terminal_admission_package(
    package_root: Path,
    *,
    candidate_id: str = TERMINAL_CID,
) -> None:
    from src.autoslice.final_media_duration_binding import (
        final_burn_duration_binding,
    )

    video = package_root / "terminal-final.mp4"
    subtitle = package_root / "terminal-final.srt"
    cover = package_root / "terminal-final.cover.png"
    record_path = package_root / "terminal-final.record.json"
    video.write_bytes(b"terminal exact final media")
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n终态字幕\n",
        encoding="utf-8",
    )
    cover.write_bytes(b"terminal exact cover")
    record = {
        "candidate_id": candidate_id,
        "burned_preview": {
            "verification": {"duration_ms": 2_000},
            "branding_intro": {
                "verification": {"duration_ms": 19_546}
            },
        },
    }
    record_path.write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    manifest = {
        "schema_version": "lidousha-daily-review-manifest.v1",
        "generated_by": "build_daily_review_manifest.v1",
        "generated_at": "terminal-synthetic",
        "date": TERMINAL_DATE,
        "status": "review_ready",
        "batch_status": "review_ready_with_failures",
        "candidate_id": candidate_id,
        "deployed_commit": "old-deployed-commit",
        "run_mode": "PRODUCTION_REVIEW",
        "upload_allowed": False,
        "items": [
            {
                "candidate_id": candidate_id,
                "kind": "talk",
                "classification": "talk",
                "video": video.name,
                "subtitle_srt": subtitle.name,
                "cover": cover.name,
                "record": record_path.name,
                "final_media_duration_binding": final_burn_duration_binding(
                    record["burned_preview"]
                ),
                "sha256": {
                    "video": sha(video),
                    "subtitle_srt": sha(subtitle),
                    "cover": sha(cover),
                    "evidence_json": sha(record_path),
                },
            }
        ],
    }
    (package_root / "review_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (package_root / "package_audit.json").write_text(
        json.dumps({"passed": True}) + "\n", encoding="utf-8"
    )


def _write_terminal_contract(
    path: Path,
    *,
    full_media: bool,
) -> None:
    from src.autoslice import final_human_review as human_review

    path.write_text(
        json.dumps(
            {
                "schema_version": human_review.REVIEW_CONTRACT_SCHEMA,
                "authority": "SYNTHETIC_TERMINAL_ADMISSION_TEST_ONLY",
                "contracts": [
                    {
                        "candidate_id": TERMINAL_CID,
                        "subtitle_review_points": [
                            {
                                "point_id": (
                                    "whole-final-media"
                                    if full_media
                                    else "local-window"
                                ),
                                "final_video_start_ms": (
                                    0 if full_media else 500
                                ),
                                "final_video_end_ms": (
                                    2_000 if full_media else 1_500
                                ),
                                "expectation": "inspect exact terminal media",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_terminal_backfill_wakes_required_daily_anomaly_without_existing_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from src.autoslice import final_human_review as human_review

    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = (
        runner.BASE
        / "out"
        / TERMINAL_DATE
        / TERMINAL_CID
        / "replacement_recuts"
    )
    _write_terminal_admission_package(package_root)
    contract = tmp_path / "terminal-contract.json"
    _write_terminal_contract(contract, full_media=True)
    monkeypatch.setattr(
        human_review, "FINAL_MEDIA_REVIEW_CONTRACT_PATH", contract
    )
    monkeypatch.setattr(
        daily_manifest,
        "build",
        lambda *_args: pytest.fail("final-media-only wake must not rebuild"),
    )
    monkeypatch.setattr(
        package_audit,
        "audit_package",
        lambda *_args: pytest.fail("final-media-only wake must not reaudit"),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        final_media_review_poststage,
        "bootstrap_and_consume_production_final_media_review",
        lambda _root, candidate_id, **_kwargs: (
            calls.append(candidate_id)
            or {
                "status": "CAPABILITY_WAIT",
                "capability_bootstrap": {
                    "status": "RETRY_WAIT",
                    "reason_code": "SENTINEL_PROVIDER_RATE_LIMITED",
                },
            }
        ),
    )

    poststage.backfill_terminal_review_packages(runner)

    assert calls == [TERMINAL_CID]
    assert not (
        package_root
        / "verification"
        / f"{TERMINAL_CID}.final-media-review-job.json"
    ).exists()


def test_terminal_backfill_does_not_wake_local_only_review_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from src.autoslice import final_human_review as human_review

    _setup_terminal_scan_runtime(tmp_path, monkeypatch)
    package_root = (
        runner.BASE
        / "out"
        / TERMINAL_DATE
        / TERMINAL_CID
        / "replacement_recuts"
    )
    _write_terminal_admission_package(package_root)
    contract = tmp_path / "terminal-local-contract.json"
    _write_terminal_contract(contract, full_media=False)
    monkeypatch.setattr(
        human_review, "FINAL_MEDIA_REVIEW_CONTRACT_PATH", contract
    )
    monkeypatch.setattr(
        daily_manifest,
        "build",
        lambda *_args: pytest.fail("local-only contract must not rebuild"),
    )
    monkeypatch.setattr(
        package_audit,
        "audit_package",
        lambda *_args: pytest.fail("local-only contract must not reaudit"),
    )
    monkeypatch.setattr(
        final_media_review_poststage,
        "bootstrap_and_consume_production_final_media_review",
        lambda *_args, **_kwargs: pytest.fail(
            "local-only contract must not schedule final-media consumer"
        ),
    )

    poststage.backfill_terminal_review_packages(runner)
