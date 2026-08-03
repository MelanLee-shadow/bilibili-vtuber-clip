import json

import pytest

import scripts.session_autoslice as runner
from src.autoslice.published_song_history import (
    PublishedSongHistoryError,
    published_song_match,
)
from src.autoslice.song_lane import _published_song_delivery_allowed
from src.autoslice.surface_canon import CHANNEL_PROFILE


def _snapshot(tmp_path):
    path = tmp_path / "published.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "vtuber-slice.published-songs.v1",
                "profile_id": "lidousha",
                "verified_through": "2026-07-17T00:00:00Z",
                "songs": [
                    {
                        "canonical_title": "小幸运",
                        "aliases": ["A Little Happiness"],
                        "bvid": "BV18eN967ENy",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_snapshot_matches_exact_normalized_title_and_explicit_alias(tmp_path):
    snapshot = _snapshot(tmp_path)
    ledger = tmp_path / "missing-ledger.jsonl"

    exact = published_song_match(
        f"{CHANNEL_PROFILE.talk_title_prefix}示例歌，直播间唱《小 幸 运》",
        snapshot_path=snapshot,
        ledger_path=ledger,
        song_title_prefix=f"{CHANNEL_PROFILE.talk_title_prefix}示例歌，",
    )
    alias = published_song_match(
        "A Little Happiness",
        snapshot_path=snapshot,
        ledger_path=ledger,
        song_title_prefix=f"{CHANNEL_PROFILE.talk_title_prefix}示例歌，",
    )

    assert exact["canonical_title"] == "小幸运"
    assert exact["source"] == "committed_snapshot"
    assert alias["canonical_title"] == "小幸运"
    assert published_song_match(
        "小幸運",
        snapshot_path=snapshot,
        ledger_path=ledger,
        song_title_prefix=f"{CHANNEL_PROFILE.talk_title_prefix}示例歌，",
    ) is None, "unlisted semantic/fuzzy variants must not be guessed"


def test_successful_ledger_upload_is_merged_but_failed_attempt_is_not(tmp_path):
    snapshot = _snapshot(tmp_path)
    ledger = tmp_path / "upload_ledger.jsonl"
    ledger.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in (
                {
                    "event": "UPLOAD_ATTEMPT_FINISHED",
                    "rc": 1,
                    "bvid": None,
                    "title": f"{CHANNEL_PROFILE.talk_title_prefix}示例歌，《失败的歌》",
                },
                {
                    "event": "UPLOAD_ATTEMPT_FINISHED",
                    "rc": 0,
                    "bvid": "BV1TeN967EUp",
                    "title": f"{CHANNEL_PROFILE.talk_title_prefix}示例歌，《新发布歌》｜现场版",
                    "at": "2026-07-17T01:00:00Z",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert published_song_match(
        "新发布歌",
        snapshot_path=snapshot,
        ledger_path=ledger,
        song_title_prefix=f"{CHANNEL_PROFILE.talk_title_prefix}示例歌，",
    )["source"] == "production_upload_ledger"
    assert published_song_match(
        "失败的歌",
        snapshot_path=snapshot,
        ledger_path=ledger,
        song_title_prefix=f"{CHANNEL_PROFILE.talk_title_prefix}示例歌，",
    ) is None


def test_alias_collision_fails_closed(tmp_path):
    snapshot = _snapshot(tmp_path)
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    document["songs"].append(
        {
            "canonical_title": "另一首歌",
            "aliases": ["小幸运"],
            "bvid": "BV14gK36oEjN",
        }
    )
    snapshot.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PublishedSongHistoryError, match="alias collision"):
        published_song_match(
            "小幸运",
            snapshot_path=snapshot,
            ledger_path=tmp_path / "missing.jsonl",
            song_title_prefix=f"{CHANNEL_PROFILE.talk_title_prefix}示例歌，",
        )


def test_produce_song_skips_known_visual_title_before_model_or_media_work(monkeypatch):
    monkeypatch.setattr(
        runner,
        "published_song_match",
        lambda title: {
            "canonical_title": "小幸运",
            "query": title,
            "source": "committed_snapshot",
            "bvid": "BV18eN967ENy",
        },
    )
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("known upload must skip before subprocess work"),
    )

    result = runner.produce_song(
        "2026-07-17",
        {"cid": "visual-song", "session_id": "session-1", "title_hint": "小幸运"},
    )

    assert result["status"] == "blocked"
    assert result["reason_codes"] == ["SONG_PREVIOUSLY_PUBLISHED"]
    assert result["published_song_match"]["bvid"] == "BV18eN967ENy"


def test_one_song_delivery_slot_per_live_session():
    assert runner.MAX_SONGS_PER_SESSION == 1
    state = {
        "songs": [
            {"session_id": "first", "delivered": True},
        ]
    }
    assert runner.song_delivery_budget(state, "first") == 0
    assert runner.song_delivery_budget(state, "second") == 1


def test_final_canonical_title_gate_blocks_before_delivery(monkeypatch):
    monkeypatch.setattr(
        runner,
        "published_song_match",
        lambda title: {
            "canonical_title": "嘉宾",
            "query": title,
            "source": "committed_snapshot",
            "bvid": "BV19Pjw6KEpM",
        },
    )
    result = {"decision": "AUTO_UPLOAD", "reason_codes": []}
    summary = {
        "source_context_job": {
            "song_boundary": {"song_title": "嘉宾"},
        }
    }

    assert _published_song_delivery_allowed(result, summary) is False
    assert result["decision"] == "BLOCK"
    assert result["reason_codes"] == ["SONG_PREVIOUSLY_PUBLISHED"]
    assert result["published_song_match"]["bvid"] == "BV19Pjw6KEpM"
