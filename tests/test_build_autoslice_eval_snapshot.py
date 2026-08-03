import json
from pathlib import Path
import subprocess

import pytest

from scripts.build_autoslice_eval_snapshot import build_snapshot


REPO_ROOT = Path(__file__).resolve().parents[1]


def _commit_fixture(repo: Path, *, include_profile: bool) -> None:
    profile_document = json.loads(
        (REPO_ROOT / "profiles/lidousha/profile.json").read_text(encoding="utf-8")
    )
    files = {
        "scripts/session_autoslice.py": "# runner\n",
        "scripts/produce_slice_package.py": "# producer\n",
        "scripts/regenerate_channel_cover.py": "# cover tool\n",
        "src/autoslice/source_context_executor.py": "# executor\n",
        "profiles/lidousha/profile.json": (
            json.dumps(profile_document, ensure_ascii=False, indent=2) + "\n"
        ),
    }
    for relative in profile_document["assets"]["files"].values():
        if relative == "voiceprint_profile.v1.json" and not include_profile:
            continue
        files[f"assets/lidousha/{relative}"] = "{}\n"
    for relative in profile_document["assets"]["directories"].values():
        files[f"assets/lidousha/{relative}/.keep"] = "tracked directory\n"
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
        cwd=repo,
        check=True,
    )


def test_eval_snapshot_is_commit_exact_and_includes_required_assets(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _commit_fixture(repo, include_profile=True)
    # Dirty bytes must not leak into a commit-pinned blind runtime.
    (repo / "scripts/session_autoslice.py").write_text("dirty\n", encoding="utf-8")
    output = tmp_path / "snapshot"

    manifest = build_snapshot(repo=repo, commit="HEAD", output=output)

    assert (output / "scripts/session_autoslice.py").read_text(encoding="utf-8") == "# runner\n"
    assert (output / "profiles/lidousha/profile.json").is_file()
    assert (output / "assets/lidousha/voiceprint_profile.v1.json").is_file()
    persisted = json.loads((output / "EVAL_SNAPSHOT_MANIFEST.json").read_text(encoding="utf-8"))
    assert persisted["commit"] == manifest["commit"]
    assert persisted["profile_id"] == "lidousha"
    assert "profiles/lidousha/profile.json" in persisted["files"]
    assert "assets/lidousha/voiceprint_profile.v1.json" in persisted["files"]


def test_eval_snapshot_refuses_commit_missing_runtime_asset(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _commit_fixture(repo, include_profile=False)

    with pytest.raises(RuntimeError, match="voiceprint_profile"):
        build_snapshot(repo=repo, commit="HEAD", output=tmp_path / "snapshot")


def test_eval_snapshot_handles_an_alias_to_the_temp_parent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _commit_fixture(repo, include_profile=True)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    output = alias_parent / "snapshot"

    manifest = build_snapshot(repo=repo, commit="HEAD", output=output)

    assert manifest["profile_id"] == "lidousha"
    assert (output / "profiles/lidousha/profile.json").is_file()
