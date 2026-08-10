from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice import visual_song_discovery
from src.autoslice.visual_song_discovery import (
    DEFAULT_MODEL,
    VISUAL_SONG_SCHEMA_VERSION,
    VisualSongCandidate,
    VisualSongConfig,
    discover_visual_songs,
    parse_visual_song_response,
    quick_media_fingerprint,
    union_visual_song_candidates,
)


def _response(*songs: dict) -> str:
    return json.dumps(
        {"schema_version": VISUAL_SONG_SCHEMA_VERSION, "songs": list(songs)},
        ensure_ascii=False,
    )


def _song(title: str = "晴る", start: int = 90_000, end: int = 270_000, index: int = 10) -> dict:
    return {
        "song_title": title,
        "start_ms": start,
        "end_ms": end,
        "evidence": {
            "list_index": index,
            "frames": ["contact_001.jpg @ 00:01:30"],
            "reason": "new numbered row",
        },
        "confidence": 0.96,
    }


def test_parse_visual_song_response_preserves_multilingual_title_and_strict_interval():
    parsed = parse_visual_song_response(_response(_song()), duration_ms=300_000)

    assert len(parsed) == 1
    assert parsed[0].song_title == "晴る"
    assert parsed[0].list_index == 10
    assert parsed[0].start_ms == 90_000
    assert parsed[0].confidence == 0.96


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(extra="not allowed"),
        lambda row: row.update(start_ms=True),
        lambda row: row.update(end_ms=400_000),
        lambda row: row.update(confidence=1.1),
        lambda row: row.update(evidence={}),
    ],
)
def test_parse_visual_song_response_rejects_loose_or_unbound_rows(mutation):
    row = _song()
    mutation(row)
    with pytest.raises(ValueError):
        parse_visual_song_response(_response(row), duration_ms=300_000)


def test_quick_media_fingerprint_samples_middle_and_tail(tmp_path):
    media = tmp_path / "stream.mp4"
    media.write_bytes(b"a" * (3 * 1024 * 1024))
    before = quick_media_fingerprint(media, sample_bytes=1024)
    with media.open("r+b") as target:
        target.seek(len(media.read_bytes()) // 2)
        target.write(b"changed-middle")

    assert quick_media_fingerprint(media, sample_bytes=1024) != before


def test_discover_visual_songs_caches_success_and_calls_agy_once(tmp_path):
    media = tmp_path / "stream.mp4"
    media.write_bytes(b"real-enough-media-bytes" * 100)
    agy = tmp_path / "agy"
    agy.write_text("stub", encoding="utf-8")
    cache = tmp_path / "cache"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if Path(command[0]).name == "ffmpeg":
            frame = Path(command[-1].replace("%05d", "00001"))
            frame.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (520, 620), "#6ba5d2").save(frame)
        else:
            Path(kwargs["cwd"], "visual_songs.json").write_text(
                _response(_song()), encoding="utf-8"
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    config = VisualSongConfig(sample_every_seconds=30, timeout_seconds=60)
    first = discover_visual_songs(
        media,
        cache,
        duration_ms=300_000,
        config=config,
        agy_bin=agy,
        command_runner=fake_run,
    )
    second = discover_visual_songs(
        media,
        cache,
        duration_ms=300_000,
        config=config,
        agy_bin=agy,
        command_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache miss")),
    )

    assert first.status == "READY" and not first.cache_hit
    assert second.status == "READY" and second.cache_hit
    assert [Path(command[0]).name for command in calls].count("agy") == 1
    agy_command = next(command for command in calls if Path(command[0]).name == "agy")
    assert agy_command[agy_command.index("--model") + 1] == DEFAULT_MODEL
    assert "--sandbox" in agy_command
    assert "--dangerously-skip-permissions" in agy_command
    add_dir = agy_command[agy_command.index("--add-dir") + 1]
    assert Path(add_dir).name.startswith("visual_song_")
    assert "Do not inspect any other file" in agy_command[agy_command.index("-p") + 1]
    assert first.content_fingerprint == second.content_fingerprint
    assert Path(first.cache_path).is_file()


def _sheet_writing_runner(calls: list[list[str]], *, agy_writes: bool = True):
    """ffmpeg produces a real frame; agy either answers or is never reached."""

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if Path(command[0]).name == "ffmpeg":
            frame = Path(command[-1].replace("%05d", "00001"))
            frame.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (520, 620), "#6ba5d2").save(frame)
        elif agy_writes:
            Path(kwargs["cwd"], "visual_songs.json").write_text(
                _response(_song()), encoding="utf-8"
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    return fake_run


def test_discover_visual_songs_falls_back_to_gemini_when_agy_absent(
    tmp_path, monkeypatch
):
    """AGY 缺席不再让整条视觉召回 lane 死掉（2026-08-10 统一客户端接入）。

    这条 lane 在此之前是零兜底的：没有 agy 二进制就直接 FAILED，而按 Ivan
    的长期指示 wsl 产线上本来就不装 AGY。摘掉 agy_gemini_client 的 Gemini
    腿，这个用例必红。
    """

    media = tmp_path / "stream.mp4"
    media.write_bytes(b"real-enough-media-bytes" * 100)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-one")
    seen: dict[str, object] = {}

    def fake_generate_content(*, prompt, key, model, inline_parts=None, **_kwargs):
        seen["key"] = key
        seen["model"] = model
        seen["images"] = len(inline_parts or [])
        seen["prompt"] = prompt
        return _response(_song())

    monkeypatch.setattr(
        visual_song_discovery.agy_gemini_client,
        "generate_content",
        fake_generate_content,
    )

    calls: list[list[str]] = []
    result = discover_visual_songs(
        media,
        tmp_path / "cache",
        duration_ms=300_000,
        config=VisualSongConfig(sample_every_seconds=30, timeout_seconds=60),
        agy_bin=tmp_path / "missing-agy",
        command_runner=_sheet_writing_runner(calls, agy_writes=False),
    )

    assert result.status == "READY"
    assert [candidate.song_title for candidate in result.candidates] == ["晴る"]
    # AGY was never invoked; only ffmpeg ran as a subprocess.
    assert {Path(command[0]).name for command in calls} == {"ffmpeg"}
    assert seen["key"] == "free-key-one"
    assert seen["images"] >= 1
    assert "contact sheets are attached" in str(seen["prompt"])


def test_discover_visual_songs_failure_isolated_when_no_provider_can_run(tmp_path):
    """Neither AGY nor a Gemini key: the optional lane still fails isolated."""

    media = tmp_path / "stream.mp4"
    media.write_bytes(b"video")

    result = discover_visual_songs(
        media,
        tmp_path / "cache",
        duration_ms=30_000,
        agy_bin=tmp_path / "missing-agy",
    )

    assert result.status == "FAILED"
    assert result.candidates == ()
    assert result.error


def test_discover_visual_songs_failure_isolated_for_unexpected_runner_error(tmp_path):
    media = tmp_path / "stream.mp4"
    media.write_bytes(b"video")
    agy = tmp_path / "agy"
    agy.write_text("stub", encoding="utf-8")

    result = discover_visual_songs(
        media,
        tmp_path / "cache",
        duration_ms=30_000,
        agy_bin=agy,
        command_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("decoder bug")),
    )

    assert result.status == "FAILED"
    assert result.candidates == ()
    assert "AssertionError: decoder bug" in str(result.error)


def test_discover_visual_songs_failure_isolated_for_invalid_optional_config(tmp_path):
    media = tmp_path / "stream.mp4"
    media.write_bytes(b"video")

    result = discover_visual_songs(
        media,
        tmp_path / "cache",
        duration_ms=30_000,
        config=VisualSongConfig(sample_every_seconds=1),
    )

    assert result.status == "FAILED"
    assert "sample_every_seconds must be at least 5" in str(result.error)


def test_union_attaches_title_hint_to_overlap_and_appends_unmatched_visual_song():
    recalled = [
        {
            "cid": "song_asr_1",
            "anchor_start_ms": 100_000,
            "anchor_end_ms": 250_000,
            "hook": "ASR heard a performance",
        }
    ]
    visual = [
        VisualSongCandidate("晴る", 90_000, 270_000, {"list_index": 10}, 0.98),
        VisualSongCandidate("太阳系disco", 400_000, 590_000, {"list_index": 15}, 0.97),
    ]

    combined = union_visual_song_candidates(recalled, visual, segment_tag="221957")

    assert len(combined) == 2
    assert combined[0]["cid"] == "song_asr_1"
    assert combined[0]["title_hint"] == "晴る"
    assert combined[0]["visual_song_matched_to_asr"] is True
    assert combined[1]["cid"].startswith("songvis_221957_400_")
    assert combined[1]["title_hint"] == "太阳系disco"
    assert combined[1]["lane"] == "visual_song_inventory"
