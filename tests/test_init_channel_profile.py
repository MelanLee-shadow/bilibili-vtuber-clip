"""init_channel_profile：确定性脚手架一次做完（骨架复制+全量 REPLACE_ME 替换）。"""

import json
import shutil
from pathlib import Path

import pytest

from scripts.init_channel_profile import init_profile

try:  # 私库：骨架由导出器现场构建；OSS：导出器已剥离，用仓内现成骨架
    from scripts.export_oss_snapshot import build_template_assets
except ModuleNotFoundError:
    build_template_assets = None

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def mini_repo(tmp_path: Path) -> Path:
    if build_template_assets is not None:
        build_template_assets(tmp_path)  # 写出 assets/_template
    else:
        shutil.copytree(
            ROOT / "assets/_template", tmp_path / "assets/_template"
        )
    profiles_dir = tmp_path / "profiles/_template"
    profiles_dir.mkdir(parents=True)
    shutil.copy2(
        ROOT / "profiles/_template/profile.json", profiles_dir / "profile.json"
    )
    return tmp_path


def test_init_materializes_profile_with_zero_replace_me(mini_repo: Path) -> None:
    result = init_profile(
        "ake",
        display_name="阿珂",
        prompt_name="Ake",
        room="424242",
        repo_root=mini_repo,
    )

    manifest = json.loads(
        (mini_repo / "profiles/ake/profile.json").read_text(encoding="utf-8")
    )
    assert manifest["profile_id"] == "ake"
    assert manifest["identity"]["display_name"] == "阿珂"
    assert manifest["identity"]["prompt_name"] == "Ake"
    assert manifest["identity"]["room_id"] == "424242"
    assert manifest["assets"]["root"] == "assets/ake"
    assert result["replace_me_files_rewritten"] >= 10

    leftovers = [
        path.name
        for path in (mini_repo / "assets/ake").rglob("*")
        if path.is_file()
        and path.suffix in {".json", ".md", ".txt"}
        and "REPLACE_ME" in path.read_text(encoding="utf-8")
    ]
    assert leftovers == []
    ledger = json.loads(
        (mini_repo / "assets/ake/speech_memory_ledger.v1.json").read_text()
    )
    assert ledger["schema_version"] == "ake-speech-memory-ledger.v1"


def test_init_refuses_to_overwrite_existing_channel(mini_repo: Path) -> None:
    init_profile("ake", repo_root=mini_repo)
    with pytest.raises(SystemExit, match="不覆盖既有频道"):
        init_profile("ake", repo_root=mini_repo)


def test_init_rejects_builtin_and_bad_ids(mini_repo: Path) -> None:
    with pytest.raises(SystemExit, match="内置"):
        init_profile("lidousha", repo_root=mini_repo)
    with pytest.raises(SystemExit, match="不合法"):
        init_profile("Bad ID!", repo_root=mini_repo)
