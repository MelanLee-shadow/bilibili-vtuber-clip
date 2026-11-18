"""ops/recording/oci3_lane_spool_copy.sh: while the stream is live only chat files are copied; the videos reach the lane
by the mover's handover (one copy on the host, 维护者), or by this copy once the stream is over and a video
was not handed over within HANDOVER_MIN."""
from __future__ import annotations

import grp
import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "recording" / "oci3_lane_spool_copy.sh"
DAY = "2026-11-18"

gnu = shutil.which("rsync") and b"GNU" in subprocess.run(["find", "--version"], capture_output=True).stdout
pytestmark = pytest.mark.skipif(not gnu, reason="needs GNU find/stat/df and rsync (the media host)")


def _file(path: Path, minutes_ago: float, data: bytes = b"x" * 64) -> None:
    path.write_bytes(data)
    when = time.time() - minutes_ago * 60
    os.utime(path, (when, when))


def _run(tmp_path: Path) -> tuple[Path, Path]:
    owner = f"{pwd.getpwuid(os.getuid()).pw_name}:{grp.getgrgid(os.getgid()).gr_name}"
    spool, lane = tmp_path / "spool", tmp_path / "lane"
    subprocess.run(["bash", str(SCRIPT), str(spool), str(lane), owner], check=True, capture_output=True,
                   env={**os.environ, "RESERVE_KB": "1"})
    return spool / DAY, lane / DAY


def _spool_day(tmp_path: Path) -> Path:
    day = tmp_path / "spool" / DAY
    day.mkdir(parents=True)
    return day


def test_while_live_only_chat_is_copied(tmp_path):
    day = _spool_day(tmp_path)
    _file(day / "a.flv", 120)
    _file(day / "a.xml", 120)
    _file(day / "b.flv", 0)                       # being recorded
    _, lane = _run(tmp_path)
    assert (lane / "a.xml").is_file()
    assert not (lane / "a.flv").exists() and not (lane / "b.flv").exists()
    assert (lane / ".copy_pending").is_file()


def test_after_the_stream_a_video_not_handed_over_in_time_is_copied(tmp_path):
    day = _spool_day(tmp_path)
    _file(day / "a.flv", 120)
    _file(day / "b.flv", 30)                      # stream over, still within the handover wait
    _, lane = _run(tmp_path)
    assert (lane / "a.flv").read_bytes() == (day / "a.flv").read_bytes()
    assert not (lane / "b.flv").exists()
    assert (lane / ".copy_pending").is_file()


def test_a_handed_over_video_is_not_copied_again(tmp_path):
    day = _spool_day(tmp_path)
    _file(day / "a.xml", 120)
    _file(day / "b.flv", 0)                       # being recorded
    _, lane = _run(tmp_path)
    _file(lane / "a.flv", 120)                    # renamed in by the mover; the spool has no a.flv any more
    _run(tmp_path)
    assert sorted(p.name for p in lane.iterdir()) == [".copy_pending", ".source", "a.flv", "a.xml"]
