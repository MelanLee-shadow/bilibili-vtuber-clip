"""Physical deployed-code binding for final-media processes; state stays canonical."""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from src.autoslice.runtime_code_lease import (
    VERSION_ENV, immutable_version, repository_runtime_root,
)

SCHEMA = "final-media-physical-code-binding.v1"
RUNNER = "scripts/run_final_media_review_model_capability_sentinel.py"
ADAPTER = "scripts/final_media_review_gemini_api_adapter.py"
HELPER = "src/autoslice/gemini_file_api.py"
SOURCE_FILES = (RUNNER, ADAPTER, HELPER,
    "src/autoslice/final_media_review_code_binding.py",
    "src/autoslice/final_media_review_gemini_api_runtime.py",
    "src/autoslice/final_media_review_model_capability_sentinel.py",
    "src/autoslice/final_media_review_capability_autobootstrap.py",
    "src/autoslice/final_media_review_raw_av.py",
    "src/autoslice/runtime_code_lease.py",
)


def _need(ok, reason):
    if not ok:
        raise ValueError("FINAL_MEDIA_CODE_" + reason)


def _file(path):
    _need(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)),
          "PHYSICAL_PATH_REQUIRED")
    info = path.stat()
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & 0o022,
          "SOURCE_UNSAFE")
    return {"sha256": _evidence_file_sha256(path), "bytes": info.st_size}


def _helper_pin(repo):
    actual = _file(repo / HELPER)["sha256"]
    parsed = ast.parse((repo / ADAPTER).read_bytes())
    pins = [node.value.value for node in parsed.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            and any(isinstance(t, ast.Name) and t.id == "GEMINI_FILE_HELPER_SHA256"
                    for t in node.targets)]
    _need(pins == [actual], "HELPER_PIN_DRIFT")


def build_code_binding(root, repo):
    root, repo = Path(root), Path(repo)
    _need(repo.is_absolute() and repo == repo.resolve(), "PHYSICAL_PATH_REQUIRED")
    _need(repository_runtime_root(repo) == root, "RUNTIME_ROOT_DRIFT")
    commit_file = _file(repo / "DEPLOYED_COMMIT")
    commit = (repo / "DEPLOYED_COMMIT").read_text().strip()
    _need(re.fullmatch("[0-9a-f]{40}", commit) is not None, "VERSION_INVALID")
    versioned = immutable_version(repo)
    _need(versioned or repo == root / "repo", "LAYOUT_INVALID")
    manifest_file = _file(repo / "DEPLOYED_MANIFEST.json")
    manifest = json.loads((repo / "DEPLOYED_MANIFEST.json").read_bytes())
    entries = manifest.get("entries")
    _need(manifest.get("schema_version") == "oci3-shadow-autoslice-repo-manifest.v1"
          and manifest.get("commit") == commit and isinstance(entries, dict), "MANIFEST_IDENTITY_DRIFT")
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    _need(hashlib.sha256(canonical).hexdigest() == manifest.get("tree_sha256"), "MANIFEST_DIGEST_DRIFT")
    sources = {}
    for relative in SOURCE_FILES:
        path = repo / relative
        actual = _file(path)
        entry = entries.get(relative) or {}
        _need(entry.get("type") == "file" and entry.get("sha256") == actual["sha256"]
              and entry.get("mode") == stat.S_IMODE(path.stat().st_mode), "SOURCE_MANIFEST_DRIFT")
        sources[relative] = actual
    _helper_pin(repo)
    return {"schema_version": SCHEMA, "repo": repo.relative_to(root).as_posix(),
            "commit": commit, "layout": "immutable_version" if versioned else "legacy_leased",
            "deployed_commit": commit_file, "deployed_manifest": manifest_file,
            "source_files": sources}


def select_code_repo(root, environment, *, executing_repo):
    """Use the code already captured by the CLI/import lease, never current again."""
    root, executing_repo = Path(root), Path(executing_repo).resolve()
    pinned = environment.get(VERSION_ENV)
    if (executing_repo / "DEPLOYED_COMMIT").exists():
        if pinned:
            _need(Path(pinned).is_absolute() and Path(pinned) == executing_repo,
                  "EXECUTING_VERSION_DRIFT")
        binding = build_code_binding(root, executing_repo)
        return executing_repo, binding
    # Undeployed development/fixture roots retain their existing local route.
    # A development process cannot provision a real deployed legacy/current tree.
    _need(not pinned and not (root / "current").exists()
          and not (root / "repo/DEPLOYED_COMMIT").exists(), "EXECUTING_VERSION_REQUIRED")
    repo = root / "repo"
    _helper_pin(repo)
    return repo, None


def validate_code_binding(root, value, *, adapter, runner=None):
    if value is None:
        _need(not str(adapter["path"]).startswith("versions/"), "BINDING_MISSING")
        return None
    _need(isinstance(value, dict) and value.get("schema_version") == SCHEMA, "BINDING_INVALID")
    relative = value.get("repo")
    _need(isinstance(relative, str) and relative and not Path(relative).is_absolute()
          and ".." not in Path(relative).parts, "BINDING_INVALID")
    repo = Path(root) / relative
    rebuilt = build_code_binding(Path(root), repo)
    _need(value == rebuilt, "VERSION_DRIFT")
    _need(adapter["path"] == relative + "/" + ADAPTER
          and adapter["sha256"] == value["source_files"][ADAPTER]["sha256"], "ADAPTER_VERSION_DRIFT")
    if runner is not None:
        _need(runner["path"] == relative + "/" + RUNNER
              and runner["sha256"] == value["source_files"][RUNNER]["sha256"], "RUNNER_VERSION_DRIFT")
    return repo


def child_environment(environment, *, root, code_binding):
    """A child executes the captured transport version, even after current moves."""
    selected = dict(environment)
    if code_binding is None:
        return selected
    repo = Path(root) / code_binding["repo"]
    _need(build_code_binding(Path(root), repo) == code_binding, "VERSION_DRIFT")
    if code_binding["layout"] == "immutable_version":
        selected[VERSION_ENV] = str(repo)
    else:
        selected.pop(VERSION_ENV, None)
    return selected
