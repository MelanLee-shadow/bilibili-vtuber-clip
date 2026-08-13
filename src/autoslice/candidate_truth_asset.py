"""Fail-closed lookup for candidate-scoped human-truth assets."""

from __future__ import annotations

import re
from pathlib import Path


_SAFE_CANDIDATE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,96}")


def resolve_candidate_truth_asset_path(
    *,
    root: Path,
    candidate_id: str,
    suffix: str,
    truth_is_available: bool,
    label: str,
) -> Path | None:
    """Return one canonical candidate asset without following indirection."""

    normalized_id = str(candidate_id or "")
    if _SAFE_CANDIDATE_ID_RE.fullmatch(normalized_id) is None:
        raise ValueError(f"unsafe candidate id for {label}")
    if not truth_is_available:
        return None
    path = root / f"{normalized_id}{suffix}"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"candidate {label} must be a regular non-symlink file")
    if path.resolve().parent != root.resolve():
        raise ValueError(f"candidate {label} escapes its canonical asset root")
    return path
