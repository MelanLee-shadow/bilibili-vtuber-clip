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


def _other_profile_document() -> dict:
    document = _default_document()
    document["profile_id"] = "other_host"
    document["identity"] = {
        "display_name": "另一位主播",
        "prompt_name": "Another Host",
        "short_name": "小主",
        "self_reference_aliases": ["另一位主播", "小主"],
        "speaker_identity_aliases": ["另一位主播", "another_host"],
        "room_id": "123456",
        "output_directory": "other_host",
        "host_speaker_label": "主播",
        "guest_speaker_label": "嘉宾",
    }
    document["runtime"]["voiceprint_reference_subdirectory"] = "other_host"
    document["titles"] = {
        "talk_prefix": "【另一位主播】",
        "song_prefix": "【另一位主播】歌切，",
        "song_plain_template": "【另一位主播】歌切，《{song_title}》",
    }
    document["text_normalization"] = {"canonical_surfaces": []}
    document["decisions"] = {
        "host_vocal_present": "HOST_VOCAL_PRESENT",
        "host_vocal_absent": "NO_HOST_VOCAL_DETECTED",
        "verified_host_singing": "VERIFIED_HOST_SINGING",
        "host_not_singing_reason": "SONG_NOT_HOST_SINGING",
        "lyric_vocal_subject": "HOST",
    }
    return document


def test_default_lidousha_profile_freezes_the_pre_profile_runtime_contract():
    profile = load_channel_profile(REPO_ROOT, environ={})

    assert profile.profile_id == "lidousha"
    assert profile.display_name == "李豆沙"
    assert profile.prompt_name == "Li Dousha"
    assert profile.short_name == "小李"
    assert profile.self_reference_aliases == ("李豆沙", "小李", "豆沙")
    assert profile.speaker_identity_aliases == ("李豆沙", "shadow")
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
    assert profile.asset_file("manual_archive_metadata") == (
        REPO_ROOT / "assets/lidousha/manual_archive_metadata.v1.json"
    )
    assert profile.asset_file("psplive_roster") == (
        REPO_ROOT / "assets/lidousha/psplive_roster.v1.md"
    )
    assert profile.asset_file("psplive_roster_sources") == (
        REPO_ROOT / "assets/lidousha/psplive_roster_sources.v1.json"
    )
    assert profile.asset_file("timely_term_seeds") == (
        REPO_ROOT / "assets/lidousha/timely_term_seeds.json"
    )
    assert profile.asset_file("timely_term_sources") == (
        REPO_ROOT / "assets/lidousha/timely_term_sources.json"
    )
    assert profile.asset_file("title_policy") == (
        REPO_ROOT / "assets/lidousha/title_policy.json"
    )
    assert profile.asset_file("upload_tag_policy") == (
        REPO_ROOT / "assets/lidousha/upload_tag_policy.json"
    )
    assert profile.asset_file("subtitle_truth_ledger") == (
        REPO_ROOT / "assets/lidousha/subtitle_truth_ledger.v1.json"
    )
    assert profile.asset_directory("fonts") == REPO_ROOT / "assets/lidousha/fonts"
    assert profile.voiceprint_reference_subdirectory == "lidousha"
    assert profile.song_title_prefix == "【李豆沙】豆沙歌，"
    assert profile.talk_title_prefix == "【李豆沙】"
    assert [(rule.surface, rule.canonical) for rule in profile.canonical_surface_rules] == [
        ("哇哭哇哭", "wakuwaku"),
        ("哇库哇库", "wakuwaku"),
        ("直女", "侄女"),
        ("难崩小视频", "难绷小视频"),
        ("gala game", "Galgame"),
        ("嘎啦 game", "Galgame"),
        ("嘎啦game", "Galgame"),
        ("kimo熊", "kmx"),
        ("kimo 熊", "kmx"),
        ("Kimo熊", "kmx"),
        ("kimoxiong", "kmx"),
        ("基默熊", "kmx"),
        ("李豆莎", "李豆沙"),
        ("苏马奶", "十麻乃"),
    ]
    # Ivan 2026-07-14/19 铁律：歌切标题固定目录式，hook 一律被忽略。
    assert profile.format_song_title("芽吹くとき", hook="下播前的温柔哄睡小歌") == (
        "【李豆沙】豆沙歌，《芽吹くとき》"
    )
    assert profile.format_song_title("芽吹くとき") == "【李豆沙】豆沙歌，《芽吹くとき》"
    assert profile.decision("host_vocal_present") == (
        "LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS"
    )
    assert profile.decision("host_vocal_absent") == "NO_LIDOUSHA_VOCAL_DETECTED"
    assert profile.decision("verified_host_singing") == "VERIFIED_LIDOUSHA_SINGING"
    assert profile.decision("host_not_singing_reason") == "SONG_NOT_LIDOUSHA_SINGING"
    assert profile.tool("cover_regenerator") == REPO_ROOT / "scripts/regenerate_lidousha_cover.py"
    assert not profile.missing_runtime_paths()

    manual_metadata = json.loads(
        profile.asset_file("manual_archive_metadata").read_text(encoding="utf-8")
    )
    manual_archive = manual_metadata["archives"]["BV1pbNR66E2r"]
    assert manual_archive["tag_authority"] == "manual"
    assert manual_archive["preserve_on_metadata_edits"] is True
    assert manual_archive["tags"] == [
        "李豆沙",
        "虚拟UP主",
        "周次",
        "恋死",
        "非人少女",
        "百合",
        "恋人不行",
        "终将",
    ]


def test_committed_profile_can_drive_a_different_channel_without_code_changes(tmp_path):
    repo = tmp_path / "repo"
    manifest_dir = repo / "profiles/other_host"
    asset_root = repo / "profiles/other_host/assets"
    manifest_dir.mkdir(parents=True)
    document = _other_profile_document()
    document["assets"]["root"] = "profiles/other_host/assets"
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
        "【另一位主播】歌切，《测试歌》"
    )
    assert profile.decision("host_vocal_present") == "HOST_VOCAL_PRESENT"
    assert profile.canonical_surface_rules == ()
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


def test_neutral_profile_template_is_config_valid_but_runtime_incomplete():
    manifest = REPO_ROOT / "profiles/_template/profile.json"

    config_only = subprocess.run(
        [
            sys.executable,
            "scripts/validate_channel_profile.py",
            "--manifest",
            str(manifest),
            "--config-only",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    strict = subprocess.run(
        [
            sys.executable,
            "scripts/validate_channel_profile.py",
            "--manifest",
            str(manifest),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert config_only.returncode == 0, config_only.stderr
    assert json.loads(config_only.stdout)["status"] == "READY"
    assert strict.returncode == 2
    blocked = json.loads(strict.stdout)
    assert blocked["status"] == "BLOCKED"
    assert str(REPO_ROOT / "assets/replace_me/glossary.txt") in blocked[
        "missing_runtime_paths"
    ]
    assert str(REPO_ROOT / "scripts/regenerate_channel_cover.py") in blocked[
        "missing_runtime_paths"
    ]


def test_runner_applies_selected_profile_before_building_runtime_paths(tmp_path):
    document = _other_profile_document()
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


def test_standalone_producer_applies_selected_profile_to_delivery_and_assets(tmp_path):
    document = _other_profile_document()
    manifest = tmp_path / "other-profile.json"
    manifest.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["AUTOSLICE_PROFILE_MANIFEST"] = str(manifest)
    env.pop("AUTOSLICE_PROFILE", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; import scripts.produce_slice_package as p; "
                "print(json.dumps({'profile': p.CHANNEL_PROFILE.profile_id, "
                "'host': p.CHANNEL_PROFILE.display_name, "
                "'delivery': str(p.profile_delivery_root()), "
                "'confusables': str(p.profile_asset_file('entity_confusables')), "
                "'references': str(p.profile_voiceprint_reference_dir())}, "
                "ensure_ascii=False))"
            ),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "profile": "other_host",
        "host": "另一位主播",
        "delivery": str(REPO_ROOT / "other_host"),
        "confusables": str(REPO_ROOT / "assets/lidousha/entity_confusables.json"),
        "references": "/opt/bilive/autoslice/voiceprints/other_host",
    }


def test_selected_profile_drives_song_identity_prompt_and_decisions(tmp_path):
    manifest = tmp_path / "other-profile.json"
    manifest.write_text(
        json.dumps(_other_profile_document(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["AUTOSLICE_PROFILE_MANIFEST"] = str(manifest)
    env.pop("AUTOSLICE_PROFILE", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from src.autoslice.agy_lrc_alignment import _prompt; "
                "from src.autoslice.host_vocal_proof import READY_DECISION, BLOCKED_DECISION; "
                "from src.autoslice.song_repair import live_performance_failure_reason_codes; "
                "from src.autoslice.review_evidence import SourceCue; "
                "from src.autoslice.semantic_candidate_selector import build_semantic_recall_prompt; "
                "from src.autoslice.chat_authority import canonicalize_hard_surfaces; "
                "from src.autoslice.speaker_session_router import REQUEST_SCHEMA_VERSION; "
                "from src.autoslice.speaker_routing_session import SPEAKER_ROUTING_SESSION_AUTHORITY_SCHEMA; "
                "from scripts.apply_speaker_turn_overrides import HOST_SPEAKER, GUEST_SPEAKER; "
                "from scripts.gemini_slice_jingting import agy_prompt; "
                "from scripts.run_auto_review_shadow_pipeline import _ensure_lidousha_prefix, _lidousha_cover_text; "
                "song_prompt = _prompt(candidate_id='c', attempt_id='a', "
                "source_sha256='1'*64, lrc_sha256='2'*64, duration_ms=90000); "
                "semantic_prompt = build_semantic_recall_prompt([SourceCue('c', 0, 1000, '测试')], max_candidates=1); "
                "jingting_prompt = agy_prompt('draft'); "
                "print(json.dumps({'song_has_name': 'Another Host' in song_prompt, "
                "'song_has_subject': '`HOST`' in song_prompt, "
                "'semantic_has_name': '另一位主播' in semantic_prompt, "
                "'jingting_has_name': 'Another Host' in jingting_prompt, "
                "'talk_title': _ensure_lidousha_prefix('测试标题'), "
                "'cover_text': _lidousha_cover_text('【另一位主播】歌切，《测试歌》｜钩子'), "
                "'surface_text': canonicalize_hard_surfaces('直女哇库哇库'), "
                "'speaker_labels': [HOST_SPEAKER, GUEST_SPEAKER], "
                "'routing_schema': REQUEST_SCHEMA_VERSION, "
                "'session_schema': SPEAKER_ROUTING_SESSION_AUTHORITY_SCHEMA, "
                "'ready': READY_DECISION, "
                "'blocked': BLOCKED_DECISION, "
                "'reason': live_performance_failure_reason_codes({'mode': 'OTHER_SINGER'})}, "
                "ensure_ascii=False))"
            ),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "song_has_name": True,
        "song_has_subject": True,
        "semantic_has_name": True,
        "jingting_has_name": True,
        "talk_title": "【另一位主播】测试标题",
        "cover_text": "《测试歌》｜钩子",
        "surface_text": "直女哇库哇库",
        "speaker_labels": ["主播", "嘉宾"],
        "routing_schema": "other_host-speaker-routing-request.v3",
        "session_schema": "other_host-speaker-routing-session-authority.v1",
        "ready": "HOST_VOCAL_PRESENT",
        "blocked": "NO_HOST_VOCAL_DETECTED",
        "reason": ["SONG_NOT_HOST_SINGING"],
    }
