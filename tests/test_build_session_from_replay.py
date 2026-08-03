"""从官方回放造 session 源：相对轴归零、epoch=墙钟+偏移、零伪造字段。"""

import json
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from scripts.build_session_from_replay import build

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe required",
)


def _tiny_media(path: Path, seconds: float = 1.0) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=16000:cl=mono:d={seconds}",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


def test_builder_rebases_axis_and_fabricates_nothing(tmp_path: Path) -> None:
    replay_mp4 = tmp_path / "replay.mp4"
    _tiny_media(replay_mp4, 2.0)
    segment = tmp_path / "23222837_20260802-17-00-20.mp4"
    _tiny_media(segment, 1.0)
    replay_xml = tmp_path / "replay.xml"
    replay_xml.write_text(
        "<i>"
        '<d p="10.500,1,25,16777215,1754000000,0,abc,0">窗前的弹幕</d>'
        '<d p="3600.000,1,25,16777215,1754000001,0,def,0">窗内第一条</d>'
        '<d p="3720.250,1,25,16777215,1754000002,0,ghi,0">窗内第二条</d>'
        '<d p="5400.000,1,25,16777215,1754000003,0,jkl,0">窗后的弹幕</d>'
        "</i>",
        encoding="utf-8",
    )

    result = build(
        Namespace(
            segment=segment,
            replay_mp4=replay_mp4,
            replay_xml=replay_xml,
            bvid="BV1TEST",
            room="23222837",
            window_start=3600.0,
            window_end=5400.0,
            t0_wall="2026-08-02T17:00:20+08:00",
            uncertainty_s=10,
            anchor_note="synthetic",
            note="test window",
        )
    )

    assert result["danmaku_in_window"] == 2
    stem = "23222837_20260802-17-00-20"

    rows = [
        json.loads(line)
        for line in (tmp_path / f"{stem}.jsonl").read_text().splitlines()
    ]
    assert [row["info"][1] for row in rows] == ["窗内第一条", "窗内第二条"]
    from datetime import datetime

    t0_ms = int(
        datetime.fromisoformat("2026-08-02T17:00:20+08:00").timestamp() * 1000
    )
    assert rows[0]["info"][0][4] == t0_ms
    assert rows[1]["info"][0][4] == t0_ms + 120_250
    # 不伪造发送者：uid 槽为 0、uname 槽为空串
    assert rows[0]["info"][0][7] == ""
    assert rows[0]["info"][2] == []

    xml_text = (tmp_path / f"{stem}.xml").read_text(encoding="utf-8")
    assert 'p="0.000,' in xml_text and 'p="120.250,' in xml_text
    assert "窗前的弹幕" not in xml_text and "窗后的弹幕" not in xml_text
    assert "official-replay:BV1TEST" in xml_text

    meta = json.loads((tmp_path / f"{stem}.meta.json").read_text())
    assert meta["description"]["RecordStartTime"] == "2026-08-02T17:00:20+08:00"

    prov = json.loads((tmp_path / f"{stem}.replay-provenance.json").read_text())
    assert prov["chat"]["events_in_window"] == 2
    assert prov["chat"]["fabricated_fields"] == "none"
    assert prov["wall_clock_anchor"]["uncertainty_s"] == 10
