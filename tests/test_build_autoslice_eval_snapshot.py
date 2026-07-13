import json
from pathlib import Path
import subprocess

import pytest

from scripts.build_autoslice_eval_snapshot import build_snapshot


def _commit_fixture(repo: Path, *, include_profile: bool) -> None:
    files = {
        "scripts/free_session_autoslice.py": "# runner\n",
        "scripts/produce_slice_package.py": "# producer\n",
        "src/autoslice/source_context_executor.py": "# executor\n",
    }
    if include_profile:
        files["assets/lidousha/voiceprint_profile.v1.json"] = "{}\n"
    else:
        files["assets/lidousha/placeholder.txt"] = "missing profile\n"
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
    (repo / "scripts/free_session_autoslice.py").write_text("dirty\n", encoding="utf-8")
    output = tmp_path / "snapshot"

    manifest = build_snapshot(repo=repo, commit="HEAD", output=output)

    assert (output / "scripts/free_session_autoslice.py").read_text(encoding="utf-8") == "# runner\n"
    assert (output / "assets/lidousha/voiceprint_profile.v1.json").is_file()
    persisted = json.loads((output / "EVAL_SNAPSHOT_MANIFEST.json").read_text(encoding="utf-8"))
    assert persisted["commit"] == manifest["commit"]
    assert "assets/lidousha/voiceprint_profile.v1.json" in persisted["files"]


def test_eval_snapshot_refuses_commit_missing_runtime_asset(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _commit_fixture(repo, include_profile=False)

    with pytest.raises(RuntimeError, match="voiceprint_profile"):
        build_snapshot(repo=repo, commit="HEAD", output=tmp_path / "snapshot")
