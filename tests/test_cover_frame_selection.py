"""表现力选帧（截图直出封面试点，2026-07-21）。

合成片：灰底静止，t∈[0.6,1.6] 有一段"片头式"诱饵运动，t∈[5,7] 白块横移 +
音量放大 + 情绪字幕——选帧必须跳过诱饵、命中演出段；裁切器输出 1920x1080。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice.cover_frame_selection import (
    DEFAULT_SKIP_HEAD_MS,
    _parse_srt_emotion_spans,
    extract_zoomed_cover_frame,
    select_expressive_cover_frame,
)


def _write_synthetic_performance_clip(tmp_path: Path) -> Path:
    media = tmp_path / "clip.mp4"
    filter_complex = (
        # 诱饵运动（片头段 0.6-1.6s，必须被 skip_head 排除）
        "[0][1]overlay=x='if(between(t,0.6,1.6),100+(t-0.6)*80,-200)':y=40[decoy];"
        # 演出运动（5-7s，白块在遮罩外区域横移）
        "[decoy][2]overlay=x='if(between(t,5,7),110+(t-5)*70,-200)':y=70[v];"
        "[3]volume=volume='if(between(t,5,7),1.0,0.05)':eval=frame[a]"
    )
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=gray:s=320x180:d=9",
            "-f", "lavfi", "-i", "color=c=white:s=50x50:d=9",
            "-f", "lavfi", "-i", "color=c=black:s=50x50:d=9",
            "-f", "lavfi", "-i", "sine=frequency=600:duration=9",
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-pix_fmt", "yuv420p", "-shortest", str(media),
        ],
        check=True, capture_output=True,
    )
    return media


def test_selects_performance_window_not_intro_decoy(tmp_path):
    media = _write_synthetic_performance_clip(tmp_path)
    srt = tmp_path / "clip.srt"
    srt.write_text(
        "1\n00:00:05,200 --> 00:00:06,800\n啊啊啊笑死我了！\n\n"
        "2\n00:00:08,000 --> 00:00:08,600\n平静收尾\n",
        encoding="utf-8",
    )
    selection = select_expressive_cover_frame(
        media, workdir=tmp_path / "work", skip_head_ms=3_000, skip_tail_ms=800, srt_path=srt
    )
    assert selection["status"] == "SELECTED"
    # 命中演出段（5-7s），而不是 0.6-1.6s 的片头诱饵。
    assert 4_500 <= selection["best_ms"] <= 7_500, selection
    assert all(c["ms"] >= 3_000 for c in selection["candidates"])
    # 动作热区被找到（白块路径在遮罩外）。
    assert selection["motion_bbox_frac"] is not None
    assert selection["anchor_x_frac"] is not None


def test_skip_head_default_covers_branding_intro():
    # talk 成片片头 ~4.3s，默认跳过窗必须盖过它。
    assert DEFAULT_SKIP_HEAD_MS >= 4_500


def test_extract_zoomed_cover_frame_outputs_hd_canvas(tmp_path):
    media = _write_synthetic_performance_clip(tmp_path)
    out = tmp_path / "cover-base.png"
    evidence = extract_zoomed_cover_frame(
        media, 5_500, out, zoom=1.32, anchor_x_frac=0.6, head_top_frac=0.1
    )
    assert out.is_file()
    assert Image.open(out).size == (1920, 1080)
    assert evidence["zoom"] == pytest.approx(1.32)
    x0, y0, x1, y1 = evidence["crop_box"]
    assert 0 <= x0 < x1 and 0 <= y0 < y1


def test_srt_emotion_span_parser(tmp_path):
    srt = tmp_path / "x.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n普通说话内容\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n哈哈哈哈太好笑了！\n",
        encoding="utf-8",
    )
    spans = _parse_srt_emotion_spans(srt)
    assert spans == [(3_000, 4_000)]
