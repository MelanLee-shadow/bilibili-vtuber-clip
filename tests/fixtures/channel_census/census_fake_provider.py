"""No network, no secrets: deterministic providers for the public census CLI."""
import json
import os
from pathlib import Path

from src.autoslice import channel_census as cc


def record(event):
    with Path(os.environ["CENSUS_TEST_EVENTS"]).open("a") as stream:
        stream.write(event + "\n")


_build = cc.build_census_windows


def counted_build(*args, **kwargs):
    record("build")
    return _build(*args, **kwargs)


cc.build_census_windows = counted_build


def transcribe(recording, media_path):
    assert media_path.read_bytes().startswith(b"RIFF")
    record("transcribe")
    raw = json.loads(Path(os.environ["CENSUS_TEST_RECORDING"]).read_text())
    return tuple(cc.CensusTranscriptCue(**row) for row in raw["cues"])


def label(window, *, prompt):
    record("label:" + window.window_id)
    assert len(cc.census_window_text(window)) <= 2500
    assert "主播：测试主播" in prompt
    raw = {
        "window_id": window.window_id,
        "host_activity": "FREE_TALK",
        "secondary_activities": [],
        "topic": "解题与互动",
        "grades": dict.fromkeys(cc.CENSUS_WINDOW_DIMENSIONS, 2),
        "audience_interest_hits": ["math"],
        "persons": ["朋友"],
        "address_terms": [],
        "trigger_kinds": ["VIEWER_CHAT"],
        "quoted_or_roleplay": False,
        "summary": "主播回应问题并解开误会",
    }
    if stop := os.environ.get("CENSUS_TEST_STOP"):
        Path(stop).touch()
    return json.dumps(raw, ensure_ascii=False)
