import json
import os
from pathlib import Path

import pytest

import scripts.apply_subtitle_correction as correction

from scripts.apply_subtitle_correction import (
    DeliveryCopyError,
    _copy_delivery_file,
    _preflight_existing_delivery_copies,
    _preflight_delivery_targets,
    _project_existing_text_onto_timing,
    _project_reviewed_text_onto_timing,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_reviewed_text_is_projected_onto_authoritative_timing_with_drop(
    tmp_path: Path,
) -> None:
    text_source = _write(
        tmp_path / "automatic.srt",
        """1
00:00:01,000 --> 00:00:01,500
原文一

2
00:00:02,000 --> 00:00:02,500
背景日语

3
00:00:03,000 --> 00:00:03,400
旧专名
""",
    )
    decision_output = _write(
        tmp_path / "decision.srt",
        """1
00:00:01,000 --> 00:00:01,500
原文一

2
00:00:03,000 --> 00:00:03,400
正确专名
""",
    )
    timing_source = _write(
        tmp_path / "timing.srt",
        """1
00:00:00,800 --> 00:00:01,900
旧文本不构成文字 authority

2
00:00:02,000 --> 00:00:02,900
旧文本不构成文字 authority

3
00:00:03,000 --> 00:00:04,800
旧文本不构成文字 authority
""",
    )
    override = tmp_path / "override.json"
    override.write_text(
        json.dumps(
            {
                "overrides": [
                    {"source_cue": 2, "action": "drop"},
                    {"source_cue": 3, "action": "replace"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    output = _project_reviewed_text_onto_timing(
        text_source=text_source,
        decision_output=decision_output,
        timing_source=timing_source,
        text_override=override,
    )

    assert "00:00:00,800 --> 00:00:01,900\n原文一" in output
    assert "00:00:03,000 --> 00:00:04,800\n正确专名" in output
    assert "背景日语" not in output
    assert "00:00:02,000 --> 00:00:02,900" not in output


def test_refresh_only_keeps_text_but_replaces_every_timing_boundary() -> None:
    text_srt = """1
00:00:01,000 --> 00:00:01,200
完整语音

2
00:00:02,000 --> 00:00:02,300
第二句
"""
    timing_srt = """1
00:00:00,800 --> 00:00:01,900
旧文本

2
00:00:02,000 --> 00:00:03,800
旧文本
"""

    output = _project_existing_text_onto_timing(
        text_srt=text_srt,
        timing_srt=timing_srt,
    )

    assert "00:00:00,800 --> 00:00:01,900\n完整语音" in output
    assert "00:00:02,000 --> 00:00:03,800\n第二句" in output


def test_delivery_copy_skips_same_regular_video_and_sidecar(tmp_path: Path) -> None:
    video = _write(tmp_path / "recut.burned.mp4", "video")
    sidecar = _write(tmp_path / "recut.srt", "subtitle")

    assert _copy_delivery_file(video, video) == "ALREADY_DELIVERED"
    assert _copy_delivery_file(sidecar, sidecar) == "ALREADY_DELIVERED"
    hardlinked_video = tmp_path / "delivery-hardlink.mp4"
    os.link(video, hardlinked_video)
    assert _copy_delivery_file(video, hardlinked_video) == "ALREADY_DELIVERED"
    assert hardlinked_video.read_text(encoding="utf-8") == "video"
    _preflight_existing_delivery_copies([(video, hardlinked_video)])


def test_delivery_copy_copies_distinct_regular_target(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.mp4", "fresh video")
    destination = _write(tmp_path / "delivery.mp4", "old video")

    assert _copy_delivery_file(source, destination) == "COPIED"
    assert destination.read_text(encoding="utf-8") == "fresh video"


def test_delivery_preflight_and_copy_reject_symlink_and_identity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "source.srt", "text")
    destination = tmp_path / "delivery.srt"
    destination.symlink_to(source)
    with pytest.raises(DeliveryCopyError, match="symlink"):
        _preflight_delivery_targets([destination])
    with pytest.raises(DeliveryCopyError, match="symlink"):
        _copy_delivery_file(source, destination)

    distinct = _write(tmp_path / "distinct.srt", "different")
    monkeypatch.setattr(os.path, "samefile", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(DeliveryCopyError, match="cannot determine"):
        _copy_delivery_file(source, distinct)
    with pytest.raises(DeliveryCopyError, match="cannot determine"):
        _preflight_existing_delivery_copies([(source, distinct)])


def test_main_skips_same_burned_video_and_same_srt_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "auto_same_delivery"
    date = "2026-08-20"
    recut_dir = tmp_path / "out" / date / cid / "replacement_recuts"
    recut_dir.mkdir(parents=True)
    delivery = recut_dir / "recut.burned-final-sapphire72.mp4"
    subtitle = delivery.with_suffix(".srt")
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n",
        encoding="utf-8",
    )
    (recut_dir / f"{cid}.record.json").write_text(
        json.dumps(
            {
                "subtitle_path": str(subtitle),
                "media_path": str(recut_dir / "source.mp4"),
            }
        ),
        encoding="utf-8",
    )

    def fake_burn(*_args: object, **_kwargs: object) -> dict[str, object]:
        delivery.write_text("burned", encoding="utf-8")
        return {"burned_preview": {"path": str(delivery), "status": "BURNED"}}

    monkeypatch.setattr(correction, "require_branding_intro", lambda _root: object())
    monkeypatch.setattr(correction, "_burn_preview_subtitles", fake_burn)
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "uniform_host")

    assert correction.main(
        [
            "--cid",
            cid,
            "--date",
            date,
            "--delivery",
            str(delivery),
            "--replace",
            "旧字幕=新字幕",
            "--out-base",
            str(tmp_path),
        ]
    ) == 0
    assert delivery.read_text(encoding="utf-8") == "burned"
    assert "新字幕" in subtitle.read_text(encoding="utf-8")
