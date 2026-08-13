"""Fail-closed lookup for candidate-scoped human-truth assets."""

from __future__ import annotations

import hashlib
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


def candidate_truth_fingerprint(
    *,
    base_fingerprint: str,
    candidate_id: str,
    asset_paths: list[Path],
    repo_root: Path,
    truth_withheld: bool,
) -> str:
    """Hash only one candidate's optional truth assets over the shared base."""

    if not asset_paths:
        if not truth_withheld:
            return base_fingerprint
        hasher = hashlib.sha256()
        hasher.update(b"talk-pipeline-fingerprint.v4\0")
        hasher.update(base_fingerprint.encode("utf-8") + b"\0human_truth=withheld\0")
        return "sha256:" + hasher.hexdigest()
    hasher = hashlib.sha256()
    hasher.update(b"talk-pipeline-fingerprint.v5\0")
    hasher.update(base_fingerprint.encode("utf-8") + b"\0")
    hasher.update(str(candidate_id).encode("utf-8") + b"\0")
    for path in sorted(asset_paths, key=lambda item: item.relative_to(repo_root).as_posix()):
        relative = path.relative_to(repo_root).as_posix()
        hasher.update(relative.encode("utf-8") + b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()
