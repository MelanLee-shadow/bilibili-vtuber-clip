"""Manifest-bound bridge from Qixi package receipts to terminal owner auditing."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.qixi_corrected_package_finalization import (
    QixiCorrectedPackageError,
    validate_manifest_bound_applied_receipt,
)
from src.autoslice.qixi_terminal_subtitle_projection import (
    TerminalProjectionError,
    validate_projection_assets,
)


def manifest_bound_terminal_projection_authority(
    *,
    package_root: Path,
    item: Mapping[str, object],
    qixi_repo_root: Path | None,
) -> dict[str, Any] | None:
    """Return terminal authority only after the typed package receipt replays."""

    repo_root = qixi_repo_root or Path(__file__).resolve().parents[2]
    candidate_id = str(item.get("candidate_id") or "")
    if not candidate_id:
        return None
    try:
        if not validate_manifest_bound_applied_receipt(
            item,
            candidate_id=candidate_id,
            package_root=package_root,
            repo_root=qixi_repo_root,
        ):
            return None
        return validate_projection_assets(repo_root)
    except (
        OSError,
        UnicodeError,
        ValueError,
        QixiCorrectedPackageError,
        TerminalProjectionError,
    ):
        return None
