"""Contract shared by local consumers and the standard-library SSH bootstrap."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from scripts import llm_via_runtime_cpa_ssh as bridge
from src.autoslice.deployed_code_root import deployed_code_root
from src.autoslice import llm_client, provider_slots

SHA = "a" * 40


def deployment(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "runtime"
    repo = base / "versions" / SHA / "repo"
    repo.mkdir(parents=True)
    (repo / "DEPLOYED_COMMIT").write_text(SHA + "\n")
    (base / "current").symlink_to(f"versions/{SHA}")
    (base / "repo").mkdir()
    (base / "repo" / "DEPLOYED_COMMIT").write_text("b" * 40 + "\n")
    return base, repo


def test_version_tree_wins_over_stale_repo(tmp_path: Path) -> None:
    base, repo = deployment(tmp_path)
    assert deployed_code_root(base) == repo
    assert (deployed_code_root(base) / "DEPLOYED_COMMIT").read_text() == SHA + "\n"


def test_absent_current_returns_legacy_repo(tmp_path: Path) -> None:
    base, _ = deployment(tmp_path)
    (base / "current").unlink()
    assert deployed_code_root(base) == base / "repo"
    assert (deployed_code_root(base) / "DEPLOYED_COMMIT").read_text() == "b" * 40 + "\n"


INVALID = [
    "current_directory", "current_file", "wrong_target", "absolute_target",
    "repo_suffix", "short_commit", "uppercase_commit", "broken_link",
    "versions_symlink", "version_symlink", "repo_symlink", "missing_marker",
    "marker_symlink", "marker_directory", "wrong_commit", "missing_newline", "invalid_utf8",
]


def invalidate(base: Path, repo: Path, kind: str) -> None:
    current = base / "current"
    marker = repo / "DEPLOYED_COMMIT"
    if kind.startswith("current_"):
        current.unlink()
        if kind == "current_directory":
            current.mkdir()
        else:
            current.write_text("invalid")
    elif kind in {"wrong_target", "absolute_target", "repo_suffix", "short_commit", "uppercase_commit", "broken_link"}:
        current.unlink()
        targets = {
            "wrong_target": "repo", "absolute_target": str(repo.parent),
            "repo_suffix": f"versions/{SHA}/repo", "short_commit": "versions/abc",
            "uppercase_commit": "versions/" + "A" * 40, "broken_link": "versions/" + "c" * 40,
        }
        current.symlink_to(targets[kind])
    elif kind in {"versions_symlink", "version_symlink", "repo_symlink"}:
        path = {"versions_symlink": base / "versions", "version_symlink": repo.parent,
                "repo_symlink": repo}[kind]
        moved = base / "redirected"
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
    elif kind == "missing_marker":
        marker.unlink()
    elif kind == "marker_symlink":
        moved = repo / "other-marker"
        marker.rename(moved)
        marker.symlink_to(moved)
    elif kind == "marker_directory":
        marker.unlink()
        marker.mkdir()
    elif kind == "wrong_commit":
        marker.write_text("b" * 40 + "\n")
    elif kind == "missing_newline":
        marker.write_text(SHA)
    elif kind == "invalid_utf8":
        marker.write_bytes(b"\xff")


@pytest.mark.parametrize("kind", INVALID)
@pytest.mark.parametrize("resolver", ["local", "remote_bootstrap"])
def test_invalid_current_never_falls_back(tmp_path, monkeypatch, kind, resolver):
    base, repo = deployment(tmp_path)
    invalidate(base, repo, kind)
    if resolver == "local":
        with pytest.raises(ValueError, match="deployed code root:"):
            deployed_code_root(base)
    else:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"runtime_root": str(base)})))
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setattr(llm_client, "runtime_cpa_command_environment",
                            lambda *a: pytest.fail("invalid current reached provider environment"))
        with pytest.raises(ValueError, match="deployed code root:"):
            exec(compile(bridge.REMOTE_PROGRAM, "<remote-bootstrap-fixture>", "exec"), {})


def test_invalid_current_cannot_silently_bypass_provider_permit(tmp_path, monkeypatch):
    base, repo = deployment(tmp_path)
    invalidate(base, repo, "wrong_commit")
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    monkeypatch.setattr(provider_slots, "_CANONICAL_PRODUCTION_ROOT", base)
    with pytest.raises(ValueError, match="deployed code root:"):
        provider_slots.configured_runtime_root()
