"""Ordinary-production review-package post-stage."""

from __future__ import annotations

import json


def _poststage_files_are_current(
    *,
    package_root,
    date: str,
    candidate_id: str,
    batch_status: str,
) -> bool:
    manifest_path = package_root / "review_manifest.json"
    audit_path = package_root / "package_audit.json"
    if not manifest_path.is_file() or not audit_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, IndexError):
        return False
    return (
        isinstance(manifest, dict)
        and manifest.get("candidate_id") == candidate_id
        and manifest.get("date") == date
        and manifest.get("batch_status") == batch_status
        and isinstance(audit, dict)
        and audit.get("passed") is True
    )


def _state_needs_materialization(
    *,
    base,
    date: str,
    state: dict,
    daily_manifest,
) -> bool:
    for pick in state.get("picks") or []:
        if not isinstance(pick, dict) or pick.get("status") != "review_ready" or pick.get("rc") != 0:
            continue
        try:
            candidate_id = daily_manifest._safe_component(
                pick.get("candidate_id"), label="candidate_id"
            )
        except Exception:  # noqa: BLE001 - malformed picks remain untouched
            continue
        package_root = base / "out" / date / candidate_id / "replacement_recuts"
        if package_root.is_symlink() or not package_root.is_dir():
            continue
        if not _poststage_files_are_current(
            package_root=package_root,
            date=date,
            candidate_id=candidate_id,
            batch_status=state["status"],
        ):
            return True
    return False


def backfill_terminal_review_packages(runner: object) -> None:
    """Backfill aged-out terminal production packages before the work queue."""

    start_date = runner.AUTOSLICE_START_DATE
    if start_date is None or (runner.BASE / "deploy.guard").exists():
        return
    state_dir = runner.BASE / "state"
    try:
        state_files = sorted(state_dir.glob("*.json"))
    except OSError:
        return
    from scripts import build_daily_review_manifest as daily_manifest

    for state_file in state_files:
        date = state_file.stem
        if date < start_date or runner.DATE_RX.fullmatch(date) is None:
            continue
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if (
            not isinstance(state, dict)
            or state.get("run_mode", "PRODUCTION") != "PRODUCTION"
            or state.get("talk_selection_contract") is not None
            or state.get("status") not in daily_manifest.REVIEWABLE_BATCH_STATUSES
            or state.get("status") not in runner.START_DATE_TERMINAL_STATUSES
            or state.get("pending_talk")
            or state.get("pending_song")
            or not _state_needs_materialization(
                base=runner.BASE,
                date=date,
                state=state,
                daily_manifest=daily_manifest,
            )
        ):
            continue
        if (runner.BASE / "deploy.guard").exists():
            return
        materialize_production_review_packages(date, runner)


def materialize_production_review_packages(
    date: str,
    runner: object,
) -> None:
    """Build/audit ordinary Talk review packages from persisted state."""

    base = runner.BASE
    repo_root = runner.REPO_ROOT
    read_state = runner.read_state
    state_path = runner.state_path
    atomic_write_json = runner._atomic_write_json_file
    log = runner.log
    state = read_state(date)
    if (
        not isinstance(state, dict)
        or state.get("run_mode", "PRODUCTION") != "PRODUCTION"
        or state.get("talk_selection_contract") is not None
    ):
        return
    from scripts import audit_review_package as package_audit
    from scripts import build_daily_review_manifest as daily_manifest

    if state.get("status") not in daily_manifest.REVIEWABLE_BATCH_STATUSES:
        return

    state_file = state_path(date)
    deployed_commit_file = repo_root / "DEPLOYED_COMMIT"
    for pick in state.get("picks") or []:
        if not isinstance(pick, dict) or pick.get("status") != "review_ready" or pick.get("rc") != 0:
            continue
        candidate_id = str(pick.get("candidate_id") or "")
        if not candidate_id:
            continue
        try:
            candidate_id = daily_manifest._safe_component(candidate_id, label="candidate_id")
            package_root = base / "out" / date / candidate_id / "replacement_recuts"
            if package_root.is_symlink() or not package_root.is_dir():
                continue
            manifest_path = package_root / "review_manifest.json"
            audit_path = package_root / "package_audit.json"
            if manifest_path.is_file() and audit_path.is_file():
                existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                existing_audit = json.loads(audit_path.read_text(encoding="utf-8"))
                deployed_commit = deployed_commit_file.read_text(encoding="utf-8").split()[0]
                if (
                    isinstance(existing_manifest, dict)
                    and existing_manifest.get("candidate_id") == candidate_id
                    and existing_manifest.get("date") == date
                    and existing_manifest.get("batch_status") == state.get("status")
                    and existing_manifest.get("deployed_commit") == deployed_commit
                ):
                    current_audit = package_audit.audit_package(package_root)
                    if (
                        isinstance(existing_audit, dict)
                        and isinstance(current_audit, dict)
                        and current_audit.get("passed") is True
                        and current_audit == existing_audit
                    ):
                        continue
            manifest = daily_manifest.build(
                package_root, state_file, deployed_commit_file, candidate_id
            )
            if not isinstance(manifest, dict):
                raise ValueError("daily review builder did not return an object")
            atomic_write_json(manifest_path, manifest)
            audit = package_audit.audit_package(package_root)
            if not isinstance(audit, dict):
                raise ValueError("package auditor did not return an object")
            atomic_write_json(audit_path, audit)
            if (
                json.loads(manifest_path.read_text(encoding="utf-8")) != manifest
                or json.loads(audit_path.read_text(encoding="utf-8")) != audit
            ):
                raise ValueError("review package attestation readback drift")
            if audit.get("passed") is not True:
                log(
                    f"{date}: review package audit failed for {candidate_id}: "
                    f"{audit.get('blocking_issue_count', audit.get('issue_count', '?'))} blocking issue(s)"
                )
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not block dates
            log(f"{date}: production review package {candidate_id} failed: {exc}")
