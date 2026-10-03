"""Ordinary-production review-package post-stage."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


_FINAL_MEDIA_PROJECTION_SCHEMA = "final-media-poststage-projection.v1"
_FINAL_MEDIA_RESULT_STATUSES = frozenset(
    {"CONSUMED", "CAPABILITY_WAIT", "CAPABILITY_STOPPED"}
)
_FINAL_MEDIA_ALWAYS_WAKE_STATES = frozenset(
    {"WAITING_CAPABILITY", "RETRY_WAIT", "DISPATCHING", "DISPATCH_AMBIGUOUS"}
)


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


def _content_review_status_from_state(state: Mapping[str, object]) -> str | None:
    result = state.get("result")
    if isinstance(result, Mapping):
        status = result.get("content_review_status")
        if status in {"PASS", "BLOCK"}:
            return str(status)
    assessment = state.get("assessment")
    if isinstance(assessment, Mapping):
        status = assessment.get("content_review_status")
        if status == "UNASSESSED":
            return status
    return None


def _package_state_job_binding_matches(
    package_state: Mapping[str, object],
    *,
    package_root: object,
) -> bool:
    raw_path = package_state.get("job_path")
    expected_sha = package_state.get("job_sha256")
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha)
    ):
        return False
    path = Path(raw_path)
    try:
        root = Path(package_root).resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        info = path.lstat()
    except (OSError, RuntimeError, ValueError):
        return False
    if path.is_symlink() or not resolved.is_file() or info.st_nlink != 1:
        return False
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected_sha


def _projection_matches_package_state(
    projection: object,
    package_state: Mapping[str, object],
    *,
    candidate_id: str,
) -> bool:
    """Bind ordinary-runner consumption to the exact package-local state.

    The package state is the provider/dedup authority.  The date-state row is a
    bounded operations projection only.  A crash between those two durable
    writes must wake one cache-only reconciliation tick; an exact state hash
    match prevents that reconciliation from becoming a permanent polling loop.
    """

    state_sha = package_state.get("state_sha256")
    state_status = package_state.get("status")
    if (
        not isinstance(state_sha, str)
        or len(state_sha) != 64
        or any(char not in "0123456789abcdef" for char in state_sha)
        or not isinstance(state_status, str)
    ):
        return False
    if not isinstance(projection, Mapping):
        return False
    return bool(
        projection.get("schema_version") == _FINAL_MEDIA_PROJECTION_SCHEMA
        and projection.get("candidate_id") == candidate_id
        and projection.get("status") == "CONSUMED"
        and projection.get("source") == "package_local_final_media_consumer"
        and projection.get("release_authority") is False
        and projection.get("package_state_written") is True
        and projection.get("consumer_state_status") == state_status
        and projection.get("content_review_status")
        == _content_review_status_from_state(package_state)
        and projection.get("state_sha256") == state_sha
    )


def _final_media_needs_tick(
    *,
    base,
    package_root,
    candidate_id: str,
    projection: object = None,
) -> bool:
    verification = package_root / "verification"
    job_path = verification / f"{candidate_id}.final-media-review-job.json"
    state_path = verification / f"{candidate_id}.final-media-review-state.json"
    has_job = job_path.exists() or job_path.is_symlink()
    has_state = state_path.exists() or state_path.is_symlink()
    if not has_job:
        if has_state:
            return True
        try:
            from src.autoslice.production_final_media_review_admission import (
                production_final_media_review_is_required,
            )

            return production_final_media_review_is_required(
                package_root, candidate_id
            )
        except Exception:  # noqa: BLE001 - actual poststage must surface the defect
            return True
    if (
        verification.is_symlink()
        or not verification.is_dir()
        or job_path.is_symlink()
        or not job_path.is_file()
    ):
        return True
    if not has_state:
        return True
    if state_path.is_symlink() or not state_path.is_file():
        return True
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return True
    if not isinstance(state, dict):
        return True
    status = state.get("status")
    if not _package_state_job_binding_matches(
        state,
        package_root=package_root,
    ):
        return True
    projection_matches = _projection_matches_package_state(
        projection,
        state,
        candidate_id=candidate_id,
    )
    if status in _FINAL_MEDIA_ALWAYS_WAKE_STATES:
        return True
    if status == "BLOCKED_INPUT":
        if not projection_matches:
            return True
        reasons = state.get("reason_codes")
        materializable = {
            "FINAL_MEDIA_REVIEW_AUDIO_COVERAGE_INCOMPLETE",
            "FINAL_MEDIA_REVIEW_AUDIO_DURATION_MISMATCH",
            "FINAL_MEDIA_REVIEW_WINDOW_AFTER_EOF",
        }
        if isinstance(reasons, list) and materializable.intersection(reasons):
            return True
        from src.autoslice.final_media_review_model_capability_sentinel import (
            SENTINEL_RUNTIME_FILENAME,
        )
        from src.autoslice.final_media_review_raw_av import (
            RUNTIME_CAPABILITY_FILENAME,
        )

        for name in (RUNTIME_CAPABILITY_FILENAME, SENTINEL_RUNTIME_FILENAME):
            path = base / name
            if path.exists() or path.is_symlink():
                return True
        return False
    if status in {"COMPLETE", "FAILED"}:
        return not projection_matches
    return True


def _terminal_poststage_work(
    *,
    base,
    date: str,
    state: dict,
    daily_manifest,
) -> tuple[bool, list[tuple[object, str]]]:
    package_rebuild_required = False
    final_media_only: list[tuple[object, str]] = []
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
            package_rebuild_required = True
            continue
        projections = state.get("final_media_poststage")
        projection = (
            projections.get(candidate_id)
            if isinstance(projections, Mapping)
            else None
        )
        if _final_media_needs_tick(
            base=base,
            package_root=package_root,
            candidate_id=candidate_id,
            projection=projection,
        ):
            final_media_only.append((package_root, candidate_id))
    return package_rebuild_required, final_media_only


def _state_needs_materialization(
    *,
    base,
    date: str,
    state: dict,
    daily_manifest,
) -> bool:
    package_rebuild_required, final_media_only = _terminal_poststage_work(
        base=base,
        date=date,
        state=state,
        daily_manifest=daily_manifest,
    )
    return package_rebuild_required or bool(final_media_only)


def _provider_call_projection(value: object) -> int | None:
    """Keep ambiguous call counts null; zero requires affirmative evidence."""

    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError("final-media provider call count is invalid")


def _normalized_utc_timestamp(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 160:
        raise ValueError(f"{label} timestamp is invalid")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise ValueError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} timestamp has no timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _capability_observation(
    bootstrap: Mapping[str, object],
) -> dict[str, object]:
    """Project bounded sentinel identity/deadlines without becoming scheduler authority."""

    projected: dict[str, object] = {}
    run_id = bootstrap.get("run_id")
    if isinstance(run_id, str) and run_id:
        projected["capability_run_id"] = run_id[:160]
    challenge_states = bootstrap.get("challenge_states")
    if not isinstance(challenge_states, Mapping):
        return projected
    challenges: dict[str, dict[str, object]] = {}
    retry_times: list[str] = []
    for modality in ("raw_audio", "continuous_source_video"):
        raw_state = challenge_states.get(modality)
        if not isinstance(raw_state, Mapping):
            continue
        compact: dict[str, object] = {}
        status = raw_state.get("status")
        if isinstance(status, str):
            compact["status"] = status[:160]
        attempt_count = raw_state.get("attempt_count")
        if attempt_count is not None:
            if (
                isinstance(attempt_count, bool)
                or not isinstance(attempt_count, int)
                or attempt_count < 0
            ):
                raise ValueError("capability challenge attempt count is invalid")
            compact["attempt_count"] = attempt_count
        state_sha = raw_state.get("state_sha256")
        if state_sha is not None:
            if (
                not isinstance(state_sha, str)
                or len(state_sha) != 64
                or any(char not in "0123456789abcdef" for char in state_sha)
            ):
                raise ValueError("capability challenge state hash is invalid")
            compact["state_sha256"] = state_sha
        reason_code = raw_state.get("reason_code")
        if isinstance(reason_code, str):
            compact["reason_code"] = reason_code[:160]
        next_retry_at = raw_state.get("next_retry_at")
        if next_retry_at is not None:
            normalized = _normalized_utc_timestamp(
                next_retry_at,
                label=f"{modality} next_retry_at",
            )
            compact["next_retry_at"] = normalized
            if status == "RETRY_WAIT":
                retry_times.append(normalized)
        if compact:
            challenges[modality] = compact
    if challenges:
        projected["capability_challenges"] = challenges
    if retry_times:
        projected["capability_next_attempt_at"] = min(
            retry_times,
            key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
        )
    return projected


def _safe_reason_codes(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(code)[:160] for code in value if isinstance(code, str)][:32]


def _result_projection(
    candidate_id: str,
    result: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Build a bounded, non-authoritative runner-state projection."""

    if result is None:
        return None
    status = result.get("status")
    if status not in _FINAL_MEDIA_RESULT_STATUSES:
        raise ValueError("final-media poststage result status is invalid")
    projection: dict[str, object] = {
        "schema_version": _FINAL_MEDIA_PROJECTION_SCHEMA,
        "candidate_id": candidate_id,
        "status": status,
        "source": "package_local_final_media_consumer",
        "release_authority": False,
        "provider_calls": _provider_call_projection(
            result.get("provider_calls")
        ),
        "package_state_written": result.get("package_state_written") is True,
    }
    bootstrap = result.get("capability_bootstrap")
    if isinstance(bootstrap, Mapping):
        capability_status = bootstrap.get("status")
        reason_code = bootstrap.get("reason_code")
        if isinstance(capability_status, str):
            projection["capability_status"] = capability_status[:160]
        if isinstance(reason_code, str):
            projection["reason_code"] = reason_code[:160]
        projection.update(_capability_observation(bootstrap))
    consumer = result.get("consumer")
    if isinstance(consumer, Mapping):
        provider_call_status = consumer.get("provider_call_status")
        if isinstance(provider_call_status, str):
            projection["provider_call_status"] = provider_call_status[:160]
        if isinstance(consumer.get("cache_reused"), bool):
            projection["cache_reused"] = consumer["cache_reused"]
        consumer_state = consumer.get("state")
        if isinstance(consumer_state, Mapping):
            for key in ("state_sha256", "binding_sha256", "next_attempt_at"):
                value = consumer_state.get(key)
                if isinstance(value, str):
                    projection[key] = value[:160]
            attempt_count = consumer_state.get("attempt_count")
            if (
                isinstance(attempt_count, int)
                and not isinstance(attempt_count, bool)
                and attempt_count >= 0
            ):
                projection["attempt_count"] = attempt_count
            projection["reason_codes"] = _safe_reason_codes(
                consumer_state.get("reason_codes")
            )
    for source_key, projection_key in (
        ("consumer_state_status", "consumer_state_status"),
        ("consumer_content_review_status", "content_review_status"),
    ):
        value = result.get(source_key)
        if projection_key != "content_review_status" and isinstance(value, str):
            projection[projection_key] = value[:160]
        elif value in {"PASS", "BLOCK", "UNASSESSED"}:
            projection[projection_key] = value
    return projection


def _error_projection(candidate_id: str, exc: Exception) -> dict[str, object]:
    reason_code = getattr(exc, "reason_code", type(exc).__name__)
    return {
        "schema_version": _FINAL_MEDIA_PROJECTION_SCHEMA,
        "candidate_id": candidate_id,
        "status": "ERROR",
        "source": "package_local_final_media_consumer",
        "release_authority": False,
        # The exception boundary cannot prove whether a provider call or state
        # write happened.  Preserve that uncertainty instead of claiming zero.
        "provider_calls": None,
        "provider_call_status": "UNKNOWN",
        "package_state_written": None,
        "reason_code": str(reason_code)[:160],
        "error_type": type(exc).__name__[:160],
    }


def _persist_projection(
    *,
    date: str,
    candidate_id: str,
    projection: Mapping[str, object] | None,
    runner: object,
) -> bool:
    """Persist one consumer summary in the existing date state, idempotently."""

    state = runner.read_state(date)
    if not isinstance(state, dict):
        raise ValueError("final-media projection requires an object date state")
    raw = state.get("final_media_poststage")
    if raw is None:
        projections: dict[str, object] = {}
    elif isinstance(raw, Mapping):
        projections = dict(raw)
    else:
        raise ValueError("final_media_poststage state field is invalid")
    previous = projections.get(candidate_id)
    if projection is None:
        projections.pop(candidate_id, None)
    else:
        projections[candidate_id] = dict(projection)
    if previous == projections.get(candidate_id) and (
        projection is not None or candidate_id not in projections
    ):
        return False
    if projections:
        state["final_media_poststage"] = projections
    else:
        state.pop("final_media_poststage", None)
    runner.write_state(date, state)
    return True


def _consume_and_project_final_media(
    *,
    date: str,
    package_root,
    candidate_id: str,
    runner: object,
) -> dict[str, object] | None:
    try:
        result = _consume_final_media_if_configured(
            package_root=package_root,
            candidate_id=candidate_id,
            runner=runner,
        )
    except Exception as exc:
        _persist_projection(
            date=date,
            candidate_id=candidate_id,
            projection=_error_projection(candidate_id, exc),
            runner=runner,
        )
        raise
    _persist_projection(
        date=date,
        candidate_id=candidate_id,
        projection=_result_projection(candidate_id, result),
        runner=runner,
    )
    return result


def _consume_final_media_if_configured(
    *,
    package_root,
    candidate_id: str,
    runner: object,
) -> dict[str, object] | None:
    from src.autoslice.final_media_review_poststage import (
        bootstrap_and_consume_production_final_media_review,
    )

    result = bootstrap_and_consume_production_final_media_review(
        package_root,
        candidate_id,
        runtime_root=runner.BASE,
    )
    status = result.get("status")
    if status == "NOT_CONFIGURED":
        return None
    if status in {"CAPABILITY_WAIT", "CAPABILITY_STOPPED"}:
        bootstrap = result.get("capability_bootstrap")
        reason = (
            bootstrap.get("reason_code")
            if isinstance(bootstrap, dict)
            else None
        )
        runner.log(
            f"final-media {status.lower()} for {candidate_id}: "
            f"{reason or 'typed runtime status'}"
        )
    elif status == "CONSUMED":
        consumer = result.get("consumer")
        cache_reused = (
            consumer.get("cache_reused")
            if isinstance(consumer, dict)
            else None
        )
        if cache_reused is not True:
            runner.log(
                f"final-media consumed for {candidate_id}: "
                f"{result.get('consumer_state_status')}"
            )
    return result


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
        ):
            continue
        package_rebuild_required, final_media_only = _terminal_poststage_work(
            base=runner.BASE,
            date=date,
            state=state,
            daily_manifest=daily_manifest,
        )
        if not package_rebuild_required and not final_media_only:
            continue
        if (runner.BASE / "deploy.guard").exists():
            return
        if package_rebuild_required:
            materialize_production_review_packages(date, runner)
            continue
        for package_root, candidate_id in final_media_only:
            try:
                _consume_and_project_final_media(
                    date=date,
                    package_root=package_root,
                    candidate_id=candidate_id,
                    runner=runner,
                )
            except Exception as exc:  # noqa: BLE001 - preserve other terminal packages
                runner.log(
                    f"{date}: final-media poststage {candidate_id} failed: {exc}"
                )


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
                        _consume_and_project_final_media(
                            date=date,
                            package_root=package_root,
                            candidate_id=candidate_id,
                            runner=runner,
                        )
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
            else:
                _consume_and_project_final_media(
                    date=date,
                    package_root=package_root,
                    candidate_id=candidate_id,
                    runner=runner,
                )
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not block dates
            log(f"{date}: production review package {candidate_id} failed: {exc}")
