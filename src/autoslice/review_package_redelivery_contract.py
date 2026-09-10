"""Check a portable redelivery against its registered subtitle time domain."""
from __future__ import annotations

from collections.abc import Callable
import hashlib
from pathlib import Path
from typing import Any

from src.autoslice.c7b_failed_row_adoption import _delivery_local_incident_repair_valid
from src.autoslice.redelivery_time_domain import (
    DELIVERY_LOCAL,
    RedeliveryTimeDomainError,
    operator_v3_time_domain,
)
from src.autoslice.reviewed_subtitle_baseline_registry import (
    ReviewedSubtitleBaselineRegistryError,
    load_candidate_reviewed_subtitle_baseline,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_ROOT = _REPO_ROOT / "assets/lidousha/reviewed_subtitle_baselines"


def _audit_redelivery_time_domain(
    *,
    issue_adder: Callable[..., None],
    issues: list[dict[str, Any]],
    stem: str,
    record_path: Path | None,
    subtitle_path: Path | None,
    record: dict[str, Any],
) -> None:
    redelivery = record.get("redelivery_baseline")
    if not isinstance(redelivery, dict):
        return
    try:
        baseline = load_candidate_reviewed_subtitle_baseline(
            _BASELINE_ROOT,
            stem,
            repo_root=_REPO_ROOT,
        )
        if baseline is None:
            raise ReviewedSubtitleBaselineRegistryError("baseline missing")
        time_domain = operator_v3_time_domain(baseline.config)
    except (ReviewedSubtitleBaselineRegistryError, RedeliveryTimeDomainError):
        issue_adder(
            issues,
            "REDELIVERY_BASELINE_TIME_DOMAIN_AUTHORITY_INVALID",
            stem=stem,
            path=record_path,
        )
        return
    if time_domain != DELIVERY_LOCAL:
        return
    expected_interval = {
        "absolute_source_start_ms": baseline.config.get(
            "absolute_source_start_ms"
        ),
        "absolute_source_end_ms": baseline.config.get(
            "absolute_source_end_ms"
        ),
    }
    if redelivery.get("current_source_interval") != expected_interval:
        issue_adder(
            issues,
            "REDELIVERY_BASELINE_DELIVERY_INTERVAL_MISMATCH",
            stem=stem,
            path=record_path,
        )
    expected_sha = str(baseline.config.get("sha256") or "")
    try:
        actual_sha = (
            hashlib.sha256(subtitle_path.read_bytes()).hexdigest()
            if subtitle_path is not None
            else ""
        )
    except OSError:
        actual_sha = ""
    incident_repair_valid = False
    if actual_sha and actual_sha != expected_sha and subtitle_path is not None:
        try:
            incident_repair_valid = _delivery_local_incident_repair_valid(
                redelivery, Path(str(baseline.config["path"])).read_text(),
                subtitle_path.read_text(),
            )
        except (OSError, KeyError, TypeError, ValueError):
            pass
    if actual_sha != expected_sha and not incident_repair_valid:
        issue_adder(
            issues,
            "REDELIVERY_BASELINE_DELIVERY_LOCAL_SUBTITLE_MISMATCH",
            stem=stem,
            path=subtitle_path or record_path,
        )
