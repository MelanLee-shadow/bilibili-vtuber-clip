"""Git-independent raw manifest history and the asset-only extension relation."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import TypeAlias, cast

Json: TypeAlias = "None | bool | int | float | str | list[Json] | dict[str, Json]"
JsonMap: TypeAlias = dict[str, Json]

HISTORY = Path("profiles/manifest_history")
HISTORY_SCHEMA = "autoslice-profile-manifest-history.v1"
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _object(pairs: list[tuple[str, Json]]) -> JsonMap:
    result: JsonMap = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest/history key: {key}")
        result[key] = value
    return result


def _document(raw: bytes) -> JsonMap:
    value: object = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise ValueError("manifest/history must be an object")
    return cast(JsonMap, value)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _archive_bytes(repo: Path, digest: str) -> bytes:
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError("invalid manifest archive sha256")
    path = repo / HISTORY / f"{digest}.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"manifest archive unavailable: {digest}")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"manifest archive bytes differ from sha256: {digest}")
    _document(raw)
    return raw


@dataclass(frozen=True)
class ProfileBindingExtension:
    old_sha256: str
    new_sha256: str
    added_files: tuple[str, ...]
    added_directories: tuple[str, ...]

    def as_dict(self) -> JsonMap:
        return {"old_sha256": self.old_sha256, "new_sha256": self.new_sha256,
                "added_asset_keys": {"files": list(self.added_files),
                                     "directories": list(self.added_directories)}}


def manifest_extension(
    repo: Path, *, old_sha256: str, current_bytes: bytes, profile_id: str,
) -> ProfileBindingExtension | None:
    """Only strict additions to files/directories are compatible; no re-signing."""
    new_sha256 = hashlib.sha256(current_bytes).hexdigest()
    if old_sha256 == new_sha256:
        return None
    old = _document(_archive_bytes(repo, old_sha256))
    new = _document(current_bytes)
    if any(not isinstance(value, str) or value.strip() != profile_id
           for value in (old.get("profile_id"), new.get("profile_id"))):
        raise ValueError("manifest profile id differs from task profile_binding")
    additions: dict[str, tuple[str, ...]] = {}
    for name in ("files", "directories"):
        old_assets, new_assets = old.get("assets"), new.get("assets")
        if not isinstance(old_assets, dict) or not isinstance(new_assets, dict):
            raise ValueError("manifest assets must be objects")
        previous, current = old_assets.get(name), new_assets.get(name)
        if not isinstance(previous, dict) or not isinstance(current, dict):
            raise ValueError(f"manifest assets.{name} must be objects")
        if any(key not in current or _canonical(current[key]) != _canonical(value)
               for key, value in previous.items()):
            raise ValueError(f"manifest assets.{name} removed or remapped an existing key")
        additions[name] = tuple(sorted(set(current) - set(previous)))
        # The normal profile parser strips keys. A raw addition must never
        # shadow an existing effective mapping (or another new mapping).
        effective = {key.strip() for key in previous}
        for key in additions[name]:
            normalized = key.strip()
            if normalized in effective:
                raise ValueError(f"manifest assets.{name} added key shadows an existing mapping")
            effective.add(normalized)
        # Compare the entire remainder, including all other assets metadata.
        new_assets[name] = previous
    if _canonical(old) != _canonical(new) or not any(additions.values()):
        raise ValueError("manifest differs beyond a strict asset-key extension")
    return ProfileBindingExtension(old_sha256, new_sha256, additions["files"], additions["directories"])


def _index(repo: Path) -> dict[str, list[str]]:
    document = _document((repo / HISTORY / "index.json").read_bytes())
    if set(document) != {"schema_version", "manifests"} or document["schema_version"] != HISTORY_SCHEMA:
        raise ValueError("invalid manifest history index schema")
    manifests = document["manifests"]
    if not isinstance(manifests, dict):
        raise ValueError("manifest history manifests must be an object")
    result: dict[str, list[str]] = {}
    for locator, entries in manifests.items():
        if (not isinstance(locator, str) or not locator or '\x00' in locator or '\\' in locator
                or ".." in Path(locator).parts
                or Path(locator).as_posix() != locator):
            raise ValueError("invalid manifest history locator")
        if (not isinstance(entries, list) or not entries
                or any(not isinstance(item, str) or _DIGEST.fullmatch(item) is None for item in entries)):
            raise ValueError("manifest history must contain raw sha256 entries")
        history = cast(list[str], entries)
        for digest in history:
            _archive_bytes(repo, digest)
        result[locator] = history
    return result


def _manifest_locator(repo: Path, manifest: Path) -> str:
    resolved = manifest.resolve()
    try:
        return resolved.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def validate_manifest_history(repo: Path, manifest: Path) -> None:
    """The accepted current version is already archived, preserving every predecessor."""
    locator = _manifest_locator(repo, manifest)
    history = _index(repo).get(locator, [])
    current = hashlib.sha256(manifest.read_bytes()).hexdigest()
    if not history or history[-1] != current:
        raise ValueError("manifest history not updated; archive the new version while retaining previous bytes")


def archive_manifest(repo: Path, manifest: Path) -> str:
    """Register a new version only after verifying all previously registered raw bytes."""
    locator = _manifest_locator(repo, manifest)
    index = repo / HISTORY / "index.json"
    manifests = _index(repo) if index.exists() else {}
    raw = manifest.read_bytes()
    _document(raw)
    digest = hashlib.sha256(raw).hexdigest()
    target = repo / HISTORY / f"{digest}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        _archive_bytes(repo, digest)
    else:
        with target.open("xb") as output:
            output.write(raw)
    history = manifests.setdefault(locator, [])
    if not history or history[-1] != digest:
        history.append(digest)
    temporary = index.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"schema_version": HISTORY_SCHEMA, "manifests": manifests},
                                    indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(index)
    return digest
