"""Runner-owned consumption of an explicitly configured final-media job.

This module is deliberately narrower than mechanical delivery review.  It does
not create a final-media job, a package audit, a mechanical receipt, an upload
manifest, or any publication authority.  The ordinary production post-stage
calls it only when the package already contains the canonical candidate-bound
job sidecar.

The existing materializer, runtime sentinel bootstrap, raw-AV binder and
durable consumer remain the authorities for their respective transitions.
Capability wait/failure states are returned before a package review state is
created; ordinary packages without the sidecar are unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from src.autoslice.final_media_review_capability_autobootstrap import (
    CAPABILITY_BOOTSTRAP_STOP_STATUSES,
    CAPABILITY_BOOTSTRAP_WAIT_STATUSES,
    FinalMediaReviewCapabilityAutobootstrapError,
    ensure_runtime_raw_av_capability,
)
from src.autoslice.final_media_review_inputs import (
    FinalMediaReviewInputError,
    consume_review_job,
)
from src.autoslice.final_media_review_materialization import (
    FinalMediaReviewMaterializationError,
    refresh_review_job_contract,
    resolve_or_materialize_review_job,
)
from src.autoslice.final_media_review_raw_av import (
    FinalMediaReviewRawAvError,
    bind_review_job_to_runtime_capability,
)
from src.autoslice.production_final_media_review_admission import (
    ProductionFinalMediaReviewAdmissionError,
    bootstrap_production_final_media_review_job,
)


def _known_provider_calls(value: object) -> int | None:
    """Return a proven count, preserving unknown rather than inventing zero."""

    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise _error(
        "FINAL_MEDIA_REVIEW_POSTSTAGE_PROVIDER_COUNT_INVALID",
        "provider call count must be a nonnegative integer or null",
    )


def _capability_provider_calls(
    capability_bootstrap: Mapping[str, object] | None,
) -> int | None:
    if capability_bootstrap is None:
        return 0
    if capability_bootstrap.get("status") == "DISPATCH_AMBIGUOUS":
        return None
    return _known_provider_calls(capability_bootstrap.get("provider_calls"))


def _combined_provider_calls(
    capability_bootstrap: Mapping[str, object] | None,
    outcome: Mapping[str, object],
) -> int | None:
    bootstrap_calls = _capability_provider_calls(capability_bootstrap)
    call_status = outcome.get("provider_call_status")
    provider_called = outcome.get("provider_called")
    if (
        bootstrap_calls is None
        or call_status in {"AMBIGUOUS", "AMBIGUOUS_PREVIOUS_DISPATCH"}
        or provider_called is None
    ):
        return None
    if not isinstance(provider_called, bool):
        raise _error(
            "FINAL_MEDIA_REVIEW_POSTSTAGE_PROVIDER_COUNT_INVALID",
            "consumer provider_called must be true, false, or null",
        )
    return bootstrap_calls + int(provider_called)


def _content_review_status(state: object) -> str | None:
    """Project the actual perceptual result, not the pre-dispatch assessment.

    ``assessment.content_review_status`` is intentionally ``UNASSESSED`` for
    every job, including a completed one.  A terminal PASS/BLOCK therefore
    lives only in ``state.result``.  Non-terminal states retain the assessment
    value so callers can still distinguish an unresolved review.
    """

    if not isinstance(state, Mapping):
        return None
    result = state.get("result")
    if isinstance(result, Mapping):
        status = result.get("content_review_status")
        if status in {"PASS", "BLOCK"}:
            return str(status)
    assessment = state.get("assessment")
    if isinstance(assessment, Mapping):
        status = assessment.get("content_review_status")
        return str(status) if isinstance(status, str) else None
    return None


class FinalMediaReviewPoststageError(ValueError):
    """A typed package-local scheduling or consumer failure."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def _error(reason_code: str, detail: str) -> FinalMediaReviewPoststageError:
    return FinalMediaReviewPoststageError(reason_code, detail)


def _safe_package_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise _error(
            "FINAL_MEDIA_REVIEW_POSTSTAGE_PATH_INVALID",
            "package root must be an absolute non-symlink directory",
        )
    try:
        root = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_POSTSTAGE_PATH_INVALID",
            "package root is unavailable",
        ) from exc
    if not root.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_POSTSTAGE_PATH_INVALID",
            "package root is not a directory",
        )
    return root


def _contained_regular(path: Path, *, root: Path, label: str) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _error(
            "FINAL_MEDIA_REVIEW_POSTSTAGE_PATH_INVALID",
            f"{label} is unavailable or escapes package root",
        ) from exc
    if path.is_symlink() or not resolved.is_file() or info.st_nlink != 1:
        raise _error(
            "FINAL_MEDIA_REVIEW_POSTSTAGE_PATH_INVALID",
            f"{label} must be a single-link regular file",
        )
    return resolved


def _current_review_contract(
    candidate_id: str,
) -> tuple[str, list[dict[str, object]]] | None:
    """Return the current committed contract for a configured candidate."""

    from src.autoslice import final_human_review

    try:
        contract_sha256, contracts = final_human_review._review_contracts()
    except final_human_review.FinalHumanReviewError as exc:
        raise _error(exc.reason_code, exc.detail) from exc
    points = contracts.get(candidate_id)
    return (contract_sha256, points) if points is not None else None


def consume_configured_final_media_review(
    package_root: str | Path,
    candidate_id: str,
    *,
    runtime_root: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Consume one existing candidate-bound job through the normal state machine."""

    root = _safe_package_root(package_root)
    verification = root / "verification"
    job_relative = f"verification/{candidate_id}.final-media-review-job.json"
    state_relative = f"verification/{candidate_id}.final-media-review-state.json"
    job_path = root / job_relative
    state_path = root / state_relative
    if not job_path.exists() and not job_path.is_symlink():
        if state_path.exists() or state_path.is_symlink():
            raise _error(
                "FINAL_MEDIA_REVIEW_POSTSTAGE_ORPHAN_STATE",
                "final-media state exists without its canonical job",
            )
        return {
            "status": "NOT_CONFIGURED",
            "candidate_id": candidate_id,
            "provider_calls": 0,
            "package_state_written": False,
        }
    if verification.is_symlink() or not verification.is_dir():
        raise _error(
            "FINAL_MEDIA_REVIEW_POSTSTAGE_PATH_INVALID",
            "verification directory is missing or unsafe",
        )
    safe_job = _contained_regular(job_path, root=root, label="final-media job")
    try:
        materialized = resolve_or_materialize_review_job(
            safe_job, allowed_root=root
        )
        active_job = _contained_regular(
            Path(str(materialized["active_job_path"])),
            root=root,
            label="active final-media job",
        )
        current_contract = _current_review_contract(candidate_id)
        contract_refresh: dict[str, object] | None = None
        if current_contract is not None:
            contract_sha256, review_points = current_contract
            contract_refresh = refresh_review_job_contract(
                active_job,
                allowed_root=root,
                review_contract_sha256=contract_sha256,
                review_points=review_points,
            )
            active_job = _contained_regular(
                Path(str(contract_refresh["active_job_path"])),
                root=root,
                label="contract-current final-media job",
            )
        selected_runtime = (
            Path(runtime_root).expanduser().absolute()
            if runtime_root is not None
            else None
        )
        capability_bootstrap: dict[str, object] | None = None
        capability_binding: dict[str, object] | None = None
        if selected_runtime is not None:
            capability_bootstrap = ensure_runtime_raw_av_capability(
                selected_runtime,
                environment=environment,
            )
            bootstrap_status = capability_bootstrap.get("status")
            if bootstrap_status in CAPABILITY_BOOTSTRAP_WAIT_STATUSES:
                return {
                    "status": "CAPABILITY_WAIT",
                    "candidate_id": candidate_id,
                    "job_path": str(safe_job),
                    "active_job_path": str(active_job),
                    "state_path": str(state_path),
                    "materialization": materialized,
                    "contract_refresh": contract_refresh,
                    "capability_bootstrap": capability_bootstrap,
                    "capability_binding": None,
                    "provider_calls": _capability_provider_calls(
                        capability_bootstrap
                    ),
                    "package_state_written": False,
                }
            if bootstrap_status in CAPABILITY_BOOTSTRAP_STOP_STATUSES:
                return {
                    "status": "CAPABILITY_STOPPED",
                    "candidate_id": candidate_id,
                    "job_path": str(safe_job),
                    "active_job_path": str(active_job),
                    "state_path": str(state_path),
                    "materialization": materialized,
                    "contract_refresh": contract_refresh,
                    "capability_bootstrap": capability_bootstrap,
                    "capability_binding": None,
                    "provider_calls": _capability_provider_calls(
                        capability_bootstrap
                    ),
                    "package_state_written": False,
                }
            capability_binding = bind_review_job_to_runtime_capability(
                active_job,
                allowed_root=root,
                runtime_root=selected_runtime,
            )
            active_job = _contained_regular(
                Path(str(capability_binding["active_job_path"])),
                root=root,
                label="capability-bound final-media job",
            )
        outcome = consume_review_job(
            active_job,
            state_path,
            runtime_root=selected_runtime,
            allowed_root=root,
            environment=environment,
        )
    except (
        FinalMediaReviewCapabilityAutobootstrapError,
        FinalMediaReviewInputError,
        FinalMediaReviewMaterializationError,
        FinalMediaReviewRawAvError,
        KeyError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        reason_code = getattr(
            exc, "reason_code", "FINAL_MEDIA_REVIEW_POSTSTAGE_CONSUMER_FAILED"
        )
        detail = getattr(exc, "detail", str(exc))
        raise _error(str(reason_code), str(detail)) from exc
    state = outcome.get("state")
    return {
        "status": "CONSUMED",
        "candidate_id": candidate_id,
        "job_path": str(safe_job),
        "active_job_path": str(active_job),
        "state_path": str(state_path),
        "materialization": materialized,
        "contract_refresh": contract_refresh,
        "capability_bootstrap": capability_bootstrap,
        "capability_binding": capability_binding,
        "consumer": outcome,
        "consumer_state_status": (
            state.get("status") if isinstance(state, Mapping) else None
        ),
        "consumer_content_review_status": _content_review_status(state),
        "provider_calls": _combined_provider_calls(
            capability_bootstrap,
            outcome,
        ),
        "package_state_written": state_path.is_file() and not state_path.is_symlink(),
    }


def bootstrap_and_consume_production_final_media_review(
    package_root: str | Path,
    candidate_id: str,
    *,
    runtime_root: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Bootstrap an admitted daily package, then run the normal consumer."""

    root = _safe_package_root(package_root)
    verification = root / "verification"
    job_path = verification / f"{candidate_id}.final-media-review-job.json"
    state_path = verification / f"{candidate_id}.final-media-review-state.json"
    job_bootstrap: dict[str, object] | None = None
    if not job_path.exists() and not job_path.is_symlink():
        if state_path.exists() or state_path.is_symlink():
            return consume_configured_final_media_review(
                root,
                candidate_id,
                runtime_root=runtime_root,
                environment=environment,
            )
        try:
            job_bootstrap = bootstrap_production_final_media_review_job(
                root, candidate_id
            )
        except ProductionFinalMediaReviewAdmissionError as exc:
            raise _error(exc.reason_code, exc.detail) from exc
        if job_bootstrap.get("status") == "NOT_REQUIRED":
            return {
                "status": "NOT_CONFIGURED",
                "candidate_id": candidate_id,
                "job_bootstrap": job_bootstrap,
                "provider_calls": 0,
                "package_state_written": False,
            }
    result = consume_configured_final_media_review(
        root,
        candidate_id,
        runtime_root=runtime_root,
        environment=environment,
    )
    return {**result, "job_bootstrap": job_bootstrap}


__all__ = [
    "FinalMediaReviewPoststageError",
    "bootstrap_and_consume_production_final_media_review",
    "consume_configured_final_media_review",
]
