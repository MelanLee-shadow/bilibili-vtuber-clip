"""Content identity of the exact code/assets shipped to overflow workers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


TREES = ("src", "scripts", "ops", "assets", "profiles")
MANIFEST = "OVERFLOW_RELEASE_MANIFEST.json"
MARKER = "OVERFLOW_RELEASE_SHA256"


class ReleaseMismatchError(ValueError):
    pass


def inventory(repo: Path) -> dict[str, str]:
    rows = {}
    for name in TREES:
        for path in sorted((repo / name).rglob("*")):
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise ValueError("overflow release contains a symlink")
            if path.is_file():
                rows[str(path.relative_to(repo))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("DEPLOYED_COMMIT", "DEPLOYED_AUTHORITY_MANIFEST.json"):
        path = repo / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("overflow release has no deployed authority")
        rows[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows


def seal(repo: Path) -> str:
    data = {"schema_version": "autoslice-overflow-release.v1", "files": inventory(repo)}
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    (repo / MANIFEST).write_bytes(raw)
    (repo / MARKER).write_text(digest + "\n")
    return digest


def verify(repo: Path, expected: str) -> None:
    try:
        raw = (repo / MANIFEST).read_bytes()
        marker = (repo / MARKER).read_text().strip()
    except FileNotFoundError as exc:
        raise ReleaseMismatchError("overflow release is not installed") from exc
    if hashlib.sha256(raw).hexdigest() != expected or marker != expected:
        raise ReleaseMismatchError("overflow release seal mismatch")
    data = json.loads(raw)
    if data.get("schema_version") != "autoslice-overflow-release.v1" or inventory(repo) != data.get("files"):
        raise ReleaseMismatchError("overflow code/assets drifted after deployment")
