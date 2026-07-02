from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_lidousha_review_package import audit_package


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _minimal_package(tmp_path: Path) -> Path:
    root = tmp_path / "pkg"
    (root / "subtitles").mkdir(parents=True)
    (root / "ass").mkdir()
    (root / "publish").mkdir()
    (root / "evidence").mkdir()
    (root / "covers").mkdir()

    stem = "447s_hybrid_22966160_2026-06-29-22-05-02"
    _write(
        root / "subtitles" / f"{stem}.normalized.zh.srt",
        """1
00:00:00,000 --> 00:00:10,000
却说不出你欣赏我哪一
种表情

2
00:00:52,560 --> 00:01:22,540
词曲 陈绮贞
""",
    )
    _write(
        root / "ass" / f"{stem}.sapphire48.ass",
        r"""[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:10.00,Default,,0,0,0,,却说不出你欣赏我哪一\N种表情
Dialogue: 0,0:00:52.56,0:01:22.54,Default,,0,0,0,,词曲 陈绮贞
""",
    )
    _write(root / "publish" / f"{stem}.title.txt", "【李豆沙】唱着唱着突然卡住：像在KTV录的？\n")
    _write(root / "publish" / f"{stem}.publish.json", json.dumps({"title": "唱着唱着突然卡住：像在KTV录的？"}, ensure_ascii=False))
    _write(
        root / "evidence" / f"{stem}.evidence.json",
        json.dumps(
            {
                "summary": "后半段是直播/伴奏事故，包含歌词：却说不出你欣赏我哪一种表情。",
                "transcript_excerpt": "却说不出你欣赏我哪一种表情 词曲 陈绮贞 怎么还卡了一下",
            },
            ensure_ascii=False,
        ),
    )
    _write(root / "covers" / f"{stem}.cover.png", "fake")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": stem,
                        "title": "【李豆沙】唱着唱着突然卡住：像在KTV录的？",
                        "source_srt": f"/app/Videos/.../{stem}.jingting.srt",
                        "subtitle_srt": str(root / "subtitles" / f"{stem}.normalized.zh.srt"),
                        "ass_path": str(root / "ass" / f"{stem}.sapphire48.ass"),
                        "publish_json": str(root / "publish" / f"{stem}.publish.json"),
                        "title_txt": str(root / "publish" / f"{stem}.title.txt"),
                        "cover": str(root / "covers" / f"{stem}.cover.png"),
                        "evidence_json": str(root / "evidence" / f"{stem}.evidence.json"),
                        "cover_generation": "deterministic title + burned-frame cover",
                        "cover_regenerated_from_burn_frame": True,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return root


def test_audit_blocks_song_package_without_lyric_alignment_ai_cover_and_title_sync(tmp_path: Path):
    root = _minimal_package(tmp_path)

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "SONG_LYRIC_SOURCE_MISSING" in codes
    assert "SONG_ALIGNMENT_REPORT_MISSING" in codes
    assert "SONG_TITLE_FORMAT_INVALID" in codes
    assert "PUBLISH_TITLE_TXT_MISMATCH" in codes
    assert "AI_COVER_EVIDENCE_MISSING" in codes
    assert "COVER_FALLBACK_NOT_FINISHED" in codes
    assert "SUBTITLE_LONG_STATIC_CUE" in codes


def test_audit_flags_ass_visual_line_count_and_length(tmp_path: Path):
    root = tmp_path / "pkg"
    stem = "138s_semantic_22966160_2026-06-29-22-35-01"
    _write(
        root / "ass" / f"{stem}.sapphire48.ass",
        r"""[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:20.00,Default,,0,0,0,,剩女是一个组织对剩女而且这一整行明显超过十八个字\\N是一个组织我于是准备\\N把你挂在这你刚好可以\\N跟这个白色背景融为一\\N体你就挂在这吧你作为\\N一副挂画就挂在这直播\\N间你的这个百合厨的属\\N性也非常符合本质
""",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished",
                "items": [
                    {
                        "stem": stem,
                        "title": "【李豆沙】测试",
                        "ass_path": str(root / "ass" / f"{stem}.sapphire48.ass"),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert "SUBTITLE_ASS_TOO_MANY_VISUAL_LINES" in codes
    assert "SUBTITLE_ASS_LINE_TOO_LONG" in codes


def test_audit_does_not_flag_ai_cover_dict_when_fallback_used_false(tmp_path: Path):
    root = tmp_path / "pkg"
    stem = "447s_travel_meaning_redone_20260629"
    srt = _write(
        root / "subtitles" / f"{stem}.srt",
        "1\n00:00:00,000 --> 00:00:06,000\n却说不出你欣赏我哪一种表情\n",
    )
    ass = _write(
        root / "ass" / f"{stem}.ass",
        "[Events]\nDialogue: 0,0:00:00.00,0:00:06.00,Default,,0,0,0,,却说不出你欣赏我哪一种表情\n",
    )
    title = "【李豆沙】豆沙歌，《旅行的意义》唱到伴奏卡住像在KTV录的"
    publish = _write(root / "publish" / f"{stem}.publish.json", json.dumps({"title": title}, ensure_ascii=False))
    title_txt = _write(root / "publish" / f"{stem}.title.txt", title + "\n")
    evidence = _write(
        root / "evidence" / f"{stem}.evidence.json",
        json.dumps(
            {
                "classification": "song",
                "lyrics_alignment": {"lyric_source_url": "https://example.test/lrc"},
            },
            ensure_ascii=False,
        ),
    )
    ai_bg = root / "covers_ai_original" / f"{stem}.ai-bg.png"
    ai_bg.parent.mkdir(parents=True, exist_ok=True)
    ai_bg.write_bytes(b"fake-ai-bg")
    cover = root / "covers" / f"{stem}.cover.png"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"fake-cover")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "stem": stem,
                        "classification": "song",
                        "source_srt": str(srt),
                        "subtitle_srt": str(srt),
                        "ass_path": str(ass),
                        "publish_json": str(publish),
                        "title_txt": str(title_txt),
                        "cover": str(cover),
                        "evidence_json": str(evidence),
                        "lyrics_alignment_report": str(evidence),
                        "ai_cover_generated": True,
                        "source_ai_background": str(ai_bg),
                        "cover_generation": {
                            "workflow": "cpa-openai-compatible-image-edit-cover",
                            "method": "images.edit",
                            "model": "gpt-image-2",
                            "image_gen_model": "cpa",
                            "fallback_used": False,
                            "ai_background": str(ai_bg),
                            "final_cover": str(cover),
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


def test_audit_blocks_package_with_extended_invalid_review_draft_status(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "invalid_review_draft_song_boundary_subtitle_cover_failed",
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert "PACKAGE_MARKED_INVALID_REVIEW_DRAFT" in codes
    assert result["passed"] is False
    assert result["blocking_issue_count"] >= 1


def test_audit_blocks_package_with_invalid_redo_required_marker(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps({"status": "finished", "items": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "INVALID_REDO_REQUIRED.json").write_text(
        json.dumps(
            {
                "status": "invalid_review_draft_song_boundary_subtitle_cover_failed",
                "reason": "Ivan review: source clip cuts the song",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    codes = {issue["code"] for issue in result["issues"]}
    assert "PACKAGE_MARKED_INVALID_REDO_REQUIRED" in codes
    assert result["passed"] is False
