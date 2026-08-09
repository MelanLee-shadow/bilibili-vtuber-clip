"""Fail-closed path resolution for portable review-package evidence."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def portable_item_artifact_path(
    root: Path, item: Mapping[str, Any], *keys: str
) -> Path | None:
    """Return one package-relative regular file with no symlink component."""

    value = next((item.get(key) for key in keys if item.get(key)), None)
    if not isinstance(value, str) or not value:
        return None
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        return None
    resolved_root = root.resolve()
    cursor = root
    for part in raw.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return None
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _legacy_resolve(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        if path.exists():
            return path
        candidates = list(root.rglob(path.name))
        return candidates[0] if candidates else path
    return root / path


def resolve_review_item_evidence(
    *,
    root: Path,
    item: Mapping[str, Any],
    portable_required: bool,
    stem: str,
    manifest_path: Path,
) -> tuple[Path | None, Path | None, Path | None, list[dict[str, str]]]:
    """Resolve record/chat/context together and emit portable path failures."""

    keys = {
        "record": ("record", "record_json"),
        "chat_authority": ("chat_authority",),
        "clip_context": ("clip_context_json", "clip_context"),
    }
    if not portable_required:
        return (
            _legacy_resolve(root, next((item.get(k) for k in keys["record"] if item.get(k)), None)),
            _legacy_resolve(root, item.get("chat_authority")),
            _legacy_resolve(root, next((item.get(k) for k in keys["clip_context"] if item.get(k)), None)),
            [],
        )
    paths = {
        name: portable_item_artifact_path(root, item, *aliases)
        for name, aliases in keys.items()
    }
    codes = {
        "record": "RECORD_PATH_MISSING_OR_NONPORTABLE",
        "chat_authority": "CHAT_AUTHORITY_PATH_MISSING_OR_NONPORTABLE",
        "clip_context": "CLIP_CONTEXT_PATH_MISSING_OR_NONPORTABLE",
    }
    issues = [
        {
            "code": codes[name],
            "severity": "BLOCK",
            "stem": stem,
            "path": str(manifest_path),
            "detail": (
                f"item.{name} must name a package-relative regular file "
                "without traversal or symlink components"
            ),
        }
        for name, path in paths.items()
        if path is None
    ]
    return paths["record"], paths["chat_authority"], paths["clip_context"], issues
