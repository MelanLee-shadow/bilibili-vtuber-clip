from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from src.autoslice.repository_asset_authority import (
    DEPLOYED_AUTHORITY_MANIFEST,
    DEPLOYED_AUTHORITY_MANIFEST_SCHEMA,
    RepositoryAssetAuthorityError,
    repository_authority_expects_asset,
    require_repository_asset_authority,
)


ASSET_RELATIVE_PATH = Path("assets/lidousha/authorities/example.json")
DEPLOYED_COMMIT = "a" * 40


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write_asset(root: Path, payload: bytes, relative: Path = ASSET_RELATIVE_PATH) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_git_repository(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Authority Test")
    _git(root, "config", "user.email", "authority-test@example.invalid")


def _commit_asset(root: Path, payload: bytes) -> str:
    _write_asset(root, payload)
    _git(root, "add", ASSET_RELATIVE_PATH.as_posix())
    _git(root, "commit", "--quiet", "-m", "add authority asset")
    return _git(root, "rev-parse", "HEAD")


def _manifest_document(
    payload: bytes,
    *,
    deployed_commit: str = DEPLOYED_COMMIT,
    entry_overrides: dict[str, object] | None = None,
    document_overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "bytes": len(payload),
        "sha256": _bytes_sha256(payload),
    }
    if entry_overrides:
        entry.update(entry_overrides)
    document: dict[str, object] = {
        "schema_version": DEPLOYED_AUTHORITY_MANIFEST_SCHEMA,
        "deployed_commit": deployed_commit,
        "entries": {ASSET_RELATIVE_PATH.as_posix(): entry},
    }
    if document_overrides:
        document.update(document_overrides)
    return document


def _write_deployed_authority(
    root: Path,
    payload: bytes,
    *,
    stamp_commit: str = DEPLOYED_COMMIT,
    manifest_commit: str = DEPLOYED_COMMIT,
    entry_overrides: dict[str, object] | None = None,
    document_overrides: dict[str, object] | None = None,
    declared_hash: str | None = None,
) -> dict[str, object]:
    root.mkdir()
    _write_asset(root, payload)
    (root / "DEPLOYED_COMMIT").write_text(stamp_commit + "\n", encoding="utf-8")
    document = _manifest_document(
        payload,
        deployed_commit=manifest_commit,
        entry_overrides=entry_overrides,
        document_overrides=document_overrides,
    )
    manifest = {
        **document,
        "manifest_sha256": declared_hash or _canonical_sha256(document),
    }
    (root / DEPLOYED_AUTHORITY_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def test_accepts_exact_bytes_tracked_by_git_head(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_git_repository(root)
    payload = b'{"authority":"committed"}\n'
    commit = _commit_asset(root, payload)

    authority = require_repository_asset_authority(
        repo_root=root,
        relative_path=ASSET_RELATIVE_PATH,
        observed_bytes=payload,
    )

    assert authority.mode == "GIT_HEAD"
    assert authority.commit == commit
    assert authority.relative_path == ASSET_RELATIVE_PATH.as_posix()
    assert authority.file_sha256 == _bytes_sha256(payload)


def test_git_head_still_expects_committed_asset_after_worktree_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_git_repository(root)
    payload = b'{"authority":"committed"}\n'
    asset = _write_asset(root, payload)
    _git(root, "add", ASSET_RELATIVE_PATH.as_posix())
    _git(root, "commit", "--quiet", "-m", "add authority asset")
    asset.unlink()

    assert repository_authority_expects_asset(
        repo_root=root,
        relative_path=ASSET_RELATIVE_PATH,
    ) is True


def test_rejects_untracked_git_asset(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_git_repository(root)
    _git(root, "commit", "--quiet", "--allow-empty", "-m", "initial")
    payload = b'{"authority":"untracked"}\n'
    _write_asset(root, payload)

    with pytest.raises(RepositoryAssetAuthorityError, match="not tracked by HEAD"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=ASSET_RELATIVE_PATH,
            observed_bytes=payload,
        )


def test_rejects_self_consistent_worktree_asset_that_differs_from_head(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_git_repository(root)
    original_core = {"authority": "committed"}
    original = {
        **original_core,
        "authority_sha256": _canonical_sha256(original_core),
    }
    _commit_asset(root, json.dumps(original, sort_keys=True).encode("utf-8"))

    modified_core = {"authority": "modified"}
    modified = {
        **modified_core,
        "authority_sha256": _canonical_sha256(modified_core),
    }
    payload = json.dumps(modified, sort_keys=True).encode("utf-8")
    _write_asset(root, payload)
    assert modified["authority_sha256"] == _canonical_sha256(modified_core)

    with pytest.raises(RepositoryAssetAuthorityError, match="differs from HEAD"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=ASSET_RELATIVE_PATH,
            observed_bytes=payload,
        )


def test_rejects_observed_bytes_that_do_not_match_the_live_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_git_repository(root)
    committed = b'{"authority":"committed"}\n'
    _commit_asset(root, committed)

    with pytest.raises(RepositoryAssetAuthorityError, match="changed while binding"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=ASSET_RELATIVE_PATH,
            observed_bytes=b'{"authority":"stale-read"}\n',
        )


def test_accepts_exact_commit_bound_deployed_manifest(tmp_path: Path) -> None:
    root = tmp_path / "deployed"
    payload = b'{"authority":"deployed"}\n'
    _write_deployed_authority(root, payload)

    authority = require_repository_asset_authority(
        repo_root=root,
        relative_path=ASSET_RELATIVE_PATH,
        observed_bytes=payload,
    )

    assert authority.mode == "DEPLOYED_MANIFEST"
    assert authority.commit == DEPLOYED_COMMIT
    assert authority.relative_path == ASSET_RELATIVE_PATH.as_posix()
    assert authority.file_sha256 == _bytes_sha256(payload)


def test_deployed_manifest_still_expects_asset_after_file_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deployed"
    payload = b'{"authority":"deployed"}\n'
    _write_deployed_authority(root, payload)
    (root / ASSET_RELATIVE_PATH).unlink()

    assert repository_authority_expects_asset(
        repo_root=root,
        relative_path=ASSET_RELATIVE_PATH,
    ) is True


def test_rejects_deployed_tree_without_authority_manifest(tmp_path: Path) -> None:
    root = tmp_path / "deployed"
    root.mkdir()
    payload = b'{"authority":"deployed"}\n'
    _write_asset(root, payload)
    (root / "DEPLOYED_COMMIT").write_text(DEPLOYED_COMMIT + "\n", encoding="utf-8")

    with pytest.raises(RepositoryAssetAuthorityError, match="manifest is unavailable"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=ASSET_RELATIVE_PATH,
            observed_bytes=payload,
        )


def test_rejects_manifest_tampered_after_its_hash_was_sealed(tmp_path: Path) -> None:
    root = tmp_path / "deployed"
    payload = b'{"authority":"deployed"}\n'
    manifest = _write_deployed_authority(root, payload)
    entries = manifest["entries"]
    assert isinstance(entries, dict)
    entry = entries[ASSET_RELATIVE_PATH.as_posix()]
    assert isinstance(entry, dict)
    entry["bytes"] = len(payload) + 1
    (root / DEPLOYED_AUTHORITY_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RepositoryAssetAuthorityError, match="manifest is invalid"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=ASSET_RELATIVE_PATH,
            observed_bytes=payload,
        )


def test_rejects_asset_drift_even_when_manifest_remains_valid(tmp_path: Path) -> None:
    root = tmp_path / "deployed"
    sealed_payload = b'{"authority":"sealed"}\n'
    _write_deployed_authority(root, sealed_payload)
    drifted_payload = b'{"authority":"drifted"}\n'
    _write_asset(root, drifted_payload)

    with pytest.raises(RepositoryAssetAuthorityError, match="asset bytes drifted"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=ASSET_RELATIVE_PATH,
            observed_bytes=drifted_payload,
        )


def test_rejects_manifest_bound_to_a_different_deployed_commit(tmp_path: Path) -> None:
    root = tmp_path / "deployed"
    payload = b'{"authority":"deployed"}\n'
    _write_deployed_authority(root, payload, manifest_commit="b" * 40)

    with pytest.raises(RepositoryAssetAuthorityError, match="manifest is invalid"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=ASSET_RELATIVE_PATH,
            observed_bytes=payload,
        )


@pytest.mark.parametrize(
    "relative_path",
    [Path("../outside.json"), Path("/absolute/authority.json")],
)
def test_rejects_unsafe_repository_relative_paths(
    tmp_path: Path,
    relative_path: Path,
) -> None:
    root = tmp_path / "deployed"
    root.mkdir()

    with pytest.raises(RepositoryAssetAuthorityError, match="path is unsafe"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=relative_path,
            observed_bytes=b"outside",
        )


def test_rejects_leaf_asset_symlink_even_when_target_is_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "deployed"
    root.mkdir()
    payload = b'{"authority":"target"}\n'
    target = root / "assets" / "target.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    asset = root / ASSET_RELATIVE_PATH
    asset.parent.mkdir(parents=True)
    asset.symlink_to(target)

    with pytest.raises(RepositoryAssetAuthorityError, match="path contains a symlink"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=ASSET_RELATIVE_PATH,
            observed_bytes=payload,
        )


def test_rejects_asset_reached_through_intermediate_symlink_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deployed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = b'{"authority":"outside"}\n'
    (outside / "authority.json").write_bytes(payload)
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RepositoryAssetAuthorityError, match="path contains a symlink"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=Path("linked/authority.json"),
            observed_bytes=payload,
        )


def test_rejects_unknown_manifest_top_level_field_even_if_hash_is_recomputed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deployed"
    payload = b'{"authority":"deployed"}\n'
    _write_deployed_authority(
        root,
        payload,
        document_overrides={"unexpected": "must-fail-closed"},
    )

    with pytest.raises(RepositoryAssetAuthorityError, match="manifest is invalid"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=ASSET_RELATIVE_PATH,
            observed_bytes=payload,
        )


def test_rejects_unknown_manifest_entry_field_even_if_hash_is_recomputed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "deployed"
    payload = b'{"authority":"deployed"}\n'
    _write_deployed_authority(
        root,
        payload,
        entry_overrides={"unexpected": "must-fail-closed"},
    )

    with pytest.raises(RepositoryAssetAuthorityError, match="invalid asset entry"):
        require_repository_asset_authority(
            repo_root=root,
            relative_path=ASSET_RELATIVE_PATH,
            observed_bytes=payload,
        )
