"""Manifest-bound bridge from Qixi package receipts to terminal owner auditing."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.qixi_corrected_package_finalization import (
    QixiCorrectedPackageError,
    validate_manifest_bound_applied_receipt,
)
from src.autoslice.qixi_cover_successor_finalization import (
    QixiCoverSuccessorError,
    validate_applied_receipt as validate_cover_successor_receipt,
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
        successor_name = item.get("qixi_cover_successor_finalization")
        successor_sha = item.get("qixi_cover_successor_finalization_sha256")
        if isinstance(successor_name, str) or successor_sha is not None:
            if not (isinstance(successor_name, str) and isinstance(successor_sha, str)):
                return None
            successor_path = package_root / successor_name
            if successor_path.is_symlink() or not successor_path.is_file():
                return None
            import hashlib, json
            payload = successor_path.read_bytes()
            if "sha256:" + hashlib.sha256(payload).hexdigest() != successor_sha:
                return None
            receipt = json.loads(payload.decode("utf-8"))
            validate_cover_successor_receipt(receipt, package_root=package_root)
            return validate_projection_assets(repo_root)
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
        QixiCoverSuccessorError,
        TerminalProjectionError,
    ):
        return None
