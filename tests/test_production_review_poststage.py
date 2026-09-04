import json
from pathlib import Path

import pytest

import scripts.audit_review_package as package_audit
import scripts.build_daily_review_manifest as daily_manifest
import scripts.session_autoslice as runner
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
