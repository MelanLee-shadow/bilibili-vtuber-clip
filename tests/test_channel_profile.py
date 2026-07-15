import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.autoslice.channel_profile import (
    CHANNEL_PROFILE_SCHEMA_VERSION,
    ChannelProfileError,
    load_channel_profile,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _default_document() -> dict:
    return json.loads((REPO_ROOT / "profiles/lidousha/profile.json").read_text(encoding="utf-8"))


def test_default_lidousha_profile_freezes_the_pre_profile_runtime_contract():
    profile = load_channel_profile(REPO_ROOT, environ={})

    assert profile.profile_id == "lidousha"
    assert profile.display_name == "李豆沙"
    assert profile.room_id == "22966160"
    assert profile.delivery_root == REPO_ROOT / "lidousha"
    assert profile.asset_root == REPO_ROOT / "assets/lidousha"
    assert profile.asset_file("voiceprint_profile") == (
        REPO_ROOT / "assets/lidousha/voiceprint_profile.v1.json"
    )
    assert profile.asset_file("branding_intro_manifest") == (
        REPO_ROOT / "assets/lidousha/intro/branding_intro.v1.json"
    )
    assert profile.asset_file("known_songs") == REPO_ROOT / "assets/lidousha/known_songs.json"
    assert profile.asset_directory("fonts") == REPO_ROOT / "assets/lidousha/fonts"
    assert profile.voiceprint_reference_subdirectory == "lidousha"
    assert profile.song_title_prefix == "【李豆沙】豆沙歌，"
    assert profile.format_song_title("芽吹くとき", hook="下播前的温柔哄睡小歌") == (
        "【李豆沙】豆沙歌，《芽吹くとき》｜下播前的温柔哄睡小歌"
    )
    assert profile.format_song_title("芽吹くとき") == "【李豆沙】豆沙歌，直播间唱《芽吹くとき》"
    assert profile.decision("host_vocal_present") == (
        "LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS"
    )
    assert profile.decision("host_vocal_absent") == "NO_LIDOUSHA_VOCAL_DETECTED"
    assert profile.decision("verified_host_singing") == "VERIFIED_LIDOUSHA_SINGING"
    assert profile.decision("host_not_singing_reason") == "SONG_NOT_LIDOUSHA_SINGING"
    assert profile.tool("cover_regenerator") == REPO_ROOT / "scripts/regenerate_lidousha_cover.py"
    assert not profile.missing_runtime_paths()


def test_committed_profile_can_drive_a_different_channel_without_code_changes(tmp_path):
    repo = tmp_path / "repo"
    manifest_dir = repo / "profiles/other_host"
    asset_root = repo / "profiles/other_host/assets"
    manifest_dir.mkdir(parents=True)
    document = _default_document()
    document["profile_id"] = "other_host"
    document["identity"] = {
        "display_name": "另一位主播",
        "room_id": "123456",
        "output_directory": "other_host",
        "host_speaker_label": "主播",
        "guest_speaker_label": "嘉宾",
    }
    document["assets"]["root"] = "profiles/other_host/assets"
    document["runtime"]["voiceprint_reference_subdirectory"] = "other_host"
    document["titles"] = {
        "song_prefix": "【另一位主播】歌切，",
        "song_hook_template": "【另一位主播】歌切，《{song_title}》｜{hook}",
        "song_plain_template": "【另一位主播】歌切，直播间唱《{song_title}》",
    }
    document["decisions"] = {
        "host_vocal_present": "HOST_VOCAL_PRESENT",
        "host_vocal_absent": "NO_HOST_VOCAL_DETECTED",
        "verified_host_singing": "VERIFIED_HOST_SINGING",
        "host_not_singing_reason": "SONG_NOT_HOST_SINGING",
        "lyric_vocal_subject": "HOST",
    }
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/regenerate_lidousha_cover.py").write_text("# placeholder\n")
    asset_root.mkdir(parents=True)
    for relative in document["assets"]["files"].values():
        path = asset_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
    for relative in document["assets"]["directories"].values():
        (asset_root / relative).mkdir(parents=True, exist_ok=True)
    (manifest_dir / "profile.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    profile = load_channel_profile(repo, environ={"AUTOSLICE_PROFILE": "other_host"})

    assert profile.display_name == "另一位主播"
    assert profile.room_id == "123456"
    assert profile.delivery_root == repo / "other_host"
    assert profile.asset_file("known_songs") == asset_root / "known_songs.json"
    assert profile.format_song_title("测试歌", hook="测试钩子") == (
        "【另一位主播】歌切，《测试歌》｜测试钩子"
    )
    assert profile.decision("host_vocal_present") == "HOST_VOCAL_PRESENT"
    assert not profile.missing_runtime_paths()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc.update({"unknown": True}), "unknown keys"),
        (lambda doc: doc["assets"].update({"root": "../escape"}), "safe repository-relative"),
        (
            lambda doc: doc["decisions"].update({"host_vocal_present": "not-a-token"}),
            "uppercase protocol token",
        ),
    ],
)
def test_profile_validation_fails_closed(tmp_path, mutate, message):
    document = _default_document()
    mutate(document)
    manifest = tmp_path / "profile.json"
    manifest.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ChannelProfileError, match=message):
        load_channel_profile(REPO_ROOT, manifest_path=manifest, environ={})


def test_profile_schema_constant_matches_default_manifest():
    assert _default_document()["schema_version"] == CHANNEL_PROFILE_SCHEMA_VERSION


def test_runner_applies_selected_profile_before_building_runtime_paths(tmp_path):
    document = _default_document()
    document["profile_id"] = "other_host"
    document["identity"] = {
        "display_name": "另一位主播",
        "room_id": "123456",
        "output_directory": "other_host",
        "host_speaker_label": "主播",
        "guest_speaker_label": "嘉宾",
    }
    document["runtime"]["voiceprint_reference_subdirectory"] = "other_host"
    document["titles"] = {
        "song_prefix": "【另一位主播】歌切，",
        "song_hook_template": "【另一位主播】歌切，《{song_title}》｜{hook}",
        "song_plain_template": "【另一位主播】歌切，直播间唱《{song_title}》",
    }
    manifest = tmp_path / "other-profile.json"
    manifest.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["AUTOSLICE_PROFILE_MANIFEST"] = str(manifest)
    for key in (
        "AUTOSLICE_PROFILE",
        "AUTOSLICE_ROOM",
        "AUTOSLICE_REC_ROOT",
        "AUTOSLICE_HOST_VOCAL_PROFILE",
        "AUTOSLICE_HOST_VOCAL_REFERENCE_DIR",
    ):
        env.pop(key, None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; import scripts.free_session_autoslice as r; "
                "print(json.dumps({'profile': r.PROFILE_ID, 'room': r.ROOM, "
                "'recordings': str(r.REC_ROOT), 'delivery': str(r.profile_delivery_root()), "
                "'references': str(r.HOST_VOCAL_REFERENCE_DIR)}, ensure_ascii=False))"
            ),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "profile": "other_host",
        "room": "123456",
        "recordings": (
            "/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/123456"
        ),
        "delivery": str(REPO_ROOT / "other_host"),
        "references": "/opt/bilive/autoslice/voiceprints/other_host",
    }
