"""Prove that a security-sensitive repository asset is committed or deployed.

Developer/WSL checkouts prove authority against the exact bytes in ``HEAD``.
The production deploy tree intentionally has no Git object database, so the
deploy script seals the small authority surface in a commit-bound manifest
after the complete managed tree has matched the local ``git archive``.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path


DEPLOYED_AUTHORITY_MANIFEST = "DEPLOYED_AUTHORITY_MANIFEST.json"
DEPLOYED_AUTHORITY_MANIFEST_SCHEMA = "deployed-authority-manifest.v1"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class RepositoryAssetAuthorityError(ValueError):
    """The asset cannot be tied to the active commit/deployment identity."""


@dataclass(frozen=True, slots=True)
class RepositoryAssetAuthority:
    mode: str
    commit: str
    relative_path: str
    file_sha256: str


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _bytes_sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _safe_relative_path(value: Path) -> str:
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise RepositoryAssetAuthorityError("repository asset path is unsafe")
    text = value.as_posix()
    if text.startswith("./") or text in {"", "."}:
        raise RepositoryAssetAuthorityError("repository asset path is not canonical")
    return text


def _resolve_regular_asset(*, root: Path, relative: str) -> Path:
    """Resolve one asset without accepting a symlink in its path chain."""

    cursor = root
    for part in Path(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise RepositoryAssetAuthorityError(
                f"repository authority asset path contains a symlink: {relative}"
            )
    try:
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RepositoryAssetAuthorityError(
            f"repository authority asset escapes or is unavailable: {relative}"
        ) from exc
    if not resolved.is_file():
        raise RepositoryAssetAuthorityError(
            f"repository authority asset is not a regular file: {relative}"
        )
    return resolved


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise RepositoryAssetAuthorityError("git authority check is unavailable") from exc


def _git_authority(
    *, root: Path, relative: str, observed: bytes
) -> RepositoryAssetAuthority | None:
    top = _run_git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    try:
        if Path(top.stdout.decode("utf-8").strip()).resolve(strict=True) != root:
            raise RepositoryAssetAuthorityError(
                "repository asset belongs to a different Git worktree"
            )
    except (UnicodeError, OSError) as exc:
        raise RepositoryAssetAuthorityError("Git worktree identity is invalid") from exc
    tracked = _run_git(root, "ls-files", "--error-unmatch", "--", relative)
    if tracked.returncode != 0:
        raise RepositoryAssetAuthorityError(
            f"repository authority asset is not tracked by HEAD: {relative}"
        )
    committed = _run_git(root, "show", f"HEAD:{relative}")
    if committed.returncode != 0 or committed.stdout != observed:
        raise RepositoryAssetAuthorityError(
            f"repository authority asset differs from HEAD: {relative}"
        )
    head = _run_git(root, "rev-parse", "HEAD")
    commit = head.stdout.decode("ascii", errors="strict").strip()
    if head.returncode != 0 or _COMMIT_RE.fullmatch(commit) is None:
        raise RepositoryAssetAuthorityError("Git HEAD identity is invalid")
    return RepositoryAssetAuthority(
        mode="GIT_HEAD",
        commit=commit,
        relative_path=relative,
        file_sha256=_bytes_sha256(observed),
    )


def _load_deployed_authority_manifest(
    *, root: Path
) -> tuple[str, Mapping[str, object]]:
    commit_path = root / "DEPLOYED_COMMIT"
    manifest_path = root / DEPLOYED_AUTHORITY_MANIFEST
    for path, label in (
        (commit_path, "deployed commit"),
        (manifest_path, "deployed authority manifest"),
    ):
        if path.is_symlink() or not path.is_file():
            raise RepositoryAssetAuthorityError(f"{label} is unavailable")
    commit = commit_path.read_text(encoding="utf-8").split(maxsplit=1)[0]
    if _COMMIT_RE.fullmatch(commit) is None:
        raise RepositoryAssetAuthorityError("deployed commit identity is invalid")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RepositoryAssetAuthorityError(
            "deployed authority manifest is unreadable"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise RepositoryAssetAuthorityError("deployed authority manifest is not an object")
    document = dict(manifest)
    declared_hash = document.pop("manifest_sha256", None)
    entries = document.get("entries")
    if (
        set(document) != {"schema_version", "deployed_commit", "entries"}
        or document.get("schema_version") != DEPLOYED_AUTHORITY_MANIFEST_SCHEMA
        or document.get("deployed_commit") != commit
        or not isinstance(entries, Mapping)
        or not entries
        or not isinstance(declared_hash, str)
        or _SHA256_RE.fullmatch(declared_hash) is None
        or _canonical_sha256(document) != declared_hash
    ):
        raise RepositoryAssetAuthorityError("deployed authority manifest is invalid")
    for entry_path, entry_value in entries.items():
        if not isinstance(entry_path, str):
            raise RepositoryAssetAuthorityError(
                "deployed authority manifest contains a non-string asset path"
            )
        if _safe_relative_path(Path(entry_path)) != entry_path:
            raise RepositoryAssetAuthorityError(
                "deployed authority manifest contains an unsafe asset path"
            )
        if not isinstance(entry_value, Mapping) or set(entry_value) != {
            "bytes",
            "sha256",
        }:
            raise RepositoryAssetAuthorityError(
                "deployed authority manifest contains an invalid asset entry"
            )
        size = entry_value.get("bytes")
        digest = entry_value.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
        ):
            raise RepositoryAssetAuthorityError(
                "deployed authority manifest contains invalid asset metadata"
            )
    return commit, entries


def _deployed_authority(
    *, root: Path, relative: str, observed: bytes
) -> RepositoryAssetAuthority:
    commit, entries = _load_deployed_authority_manifest(root=root)
    entry = entries.get(relative)
    if not isinstance(entry, Mapping) or set(entry) != {"bytes", "sha256"}:
        raise RepositoryAssetAuthorityError(
            f"asset is absent from deployed authority manifest: {relative}"
        )
    observed_sha = _bytes_sha256(observed)
    if entry.get("bytes") != len(observed) or entry.get("sha256") != observed_sha:
        raise RepositoryAssetAuthorityError(
            f"deployed authority asset bytes drifted: {relative}"
        )
    return RepositoryAssetAuthority(
        mode="DEPLOYED_MANIFEST",
        commit=commit,
        relative_path=relative,
        file_sha256=observed_sha,
    )


def _resolved_repo_root(repo_root: Path) -> Path:
    if repo_root.is_symlink():
        raise RepositoryAssetAuthorityError("repository root must not be a symlink")
    try:
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise RepositoryAssetAuthorityError("repository root is unavailable") from exc
    if not root.is_dir():
        raise RepositoryAssetAuthorityError("repository root is not a directory")
    return root


def repository_authority_expects_asset(
    *, repo_root: Path, relative_path: Path
) -> bool:
    """Return whether active HEAD/deployment authority declares this asset.

    This deliberately does not inspect the worktree file first.  A deleted
    committed authority must remain expected so callers fail closed instead of
    silently downgrading the candidate to a legacy lane.
    """

    root = _resolved_repo_root(repo_root)
    relative = _safe_relative_path(relative_path)
    top = _run_git(root, "rev-parse", "--show-toplevel")
    if top.returncode == 0:
        try:
            if Path(top.stdout.decode("utf-8").strip()).resolve(strict=True) != root:
                raise RepositoryAssetAuthorityError(
                    "repository asset belongs to a different Git worktree"
                )
        except (UnicodeError, OSError) as exc:
            raise RepositoryAssetAuthorityError("Git worktree identity is invalid") from exc
        committed = _run_git(root, "cat-file", "-e", f"HEAD:{relative}")
        return committed.returncode == 0
    _commit, entries = _load_deployed_authority_manifest(root=root)
    return relative in entries


def require_repository_asset_authority(
    *, repo_root: Path, relative_path: Path, observed_bytes: bytes
) -> RepositoryAssetAuthority:
    """Require exact HEAD bytes or an exact commit-bound deployment entry."""

    root = _resolved_repo_root(repo_root)
    relative = _safe_relative_path(relative_path)
    path = _resolve_regular_asset(root=root, relative=relative)
    if path.read_bytes() != observed_bytes:
        raise RepositoryAssetAuthorityError("repository authority asset changed while binding")
    git_authority = _git_authority(root=root, relative=relative, observed=observed_bytes)
    if git_authority is not None:
        return git_authority
    return _deployed_authority(root=root, relative=relative, observed=observed_bytes)


def build_deployed_authority_manifest(
    *,
    repo_root: Path,
    deployed_commit: str,
    relative_paths: Iterable[Path],
) -> dict[str, object]:
    """Build the deterministic deployment manifest from exact on-disk bytes."""

    if _COMMIT_RE.fullmatch(deployed_commit) is None:
        raise RepositoryAssetAuthorityError("deployed commit identity is invalid")
    root = _resolved_repo_root(repo_root)
    canonical_paths = sorted({_safe_relative_path(path) for path in relative_paths})
    if not canonical_paths:
        raise RepositoryAssetAuthorityError("authority manifest asset set is empty")
    entries: dict[str, object] = {}
    for relative in canonical_paths:
        path = _resolve_regular_asset(root=root, relative=relative)
        payload = path.read_bytes()
        entries[relative] = {
            "bytes": len(payload),
            "sha256": _bytes_sha256(payload),
        }
    document: dict[str, object] = {
        "schema_version": DEPLOYED_AUTHORITY_MANIFEST_SCHEMA,
        "deployed_commit": deployed_commit,
        "entries": entries,
    }
    document["manifest_sha256"] = _canonical_sha256(document)
    return document
