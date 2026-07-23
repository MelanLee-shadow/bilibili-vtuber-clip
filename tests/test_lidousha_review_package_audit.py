from __future__ import annotations

import json
from pathlib import Path

from scripts.audit_lidousha_review_package import audit_package
from src.autoslice.selection_scorecard import normalize_selection_scorecard
from src.autoslice.story_contract import build_story_contract


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


def test_audit_blocks_known_too_small_talk_cover_title(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    record = root / "talk.record.json"
    record.write_text(
        json.dumps(
            {
                "cover_generation": {
                    "method": "images.edit",
                    "model": "gpt-image-2",
                    "fallback_used": False,
                    "font_size": 91,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished_review_package_no_upload_pending_human_review",
                "items": [
                    {
                        "stem": "talk",
                        "title": "【李豆沙】南町当面追问",
                        "record": record.name,
                        "ai_cover_generated": True,
                        "cover_generation": {
                            "method": "images.edit",
                            "model": "gpt-image-2",
                            "fallback_used": False,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {
        "COVER_TITLE_TOO_SMALL"
    }


def test_audit_accepts_explicit_bounded_sapphire72_visual_contract(tmp_path: Path):
    root = tmp_path / "pkg"
    stem = "autoslice-talk"
    line = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯"
    assert 18 < len(line) <= 28
    srt = _write(
        root / f"{stem}.srt",
        f"1\n00:00:00,000 --> 00:00:06,000\n{line}\n",
    )
    ass = _write(
        root / f"{stem}.ass",
        "[Events]\n"
        f"Dialogue: 0,0:00:00.00,0:00:06.00,Default,,0,0,0,,{line}\n",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished_review_package_no_upload_pending_human_review",
                "subtitle_visual_contract": {
                    "profile": "autoslice-sapphire72",
                    "max_visual_lines": 2,
                    "max_chars_per_line": 28,
                },
                "items": [
                    {
                        "stem": stem,
                        "title": "【李豆沙】测试",
                        "subtitle_srt": str(srt),
                        "ass_path": str(ass),
                        "ai_cover_generated": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is True
    assert result["issues"] == []


def test_audit_rejects_visual_contract_looser_than_renderer(tmp_path: Path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished_review_package_no_upload_pending_human_review",
                "subtitle_visual_contract": {
                    "max_visual_lines": 3,
                    "max_chars_per_line": 40,
                },
                "items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = audit_package(root)

    assert result["passed"] is False
    assert {issue["code"] for issue in result["issues"]} == {
        "SUBTITLE_VISUAL_CONTRACT_INVALID"
    }


def test_story_contract_package_rejects_subtitle_drift_and_unresolved_nancho_alias(
    tmp_path: Path,
):
    root = tmp_path / "pkg"
    root.mkdir()
    stem = "nancho-collab"
    transcript = "南町nightin说非常亚撒西"
    srt = _write(
        root / f"{stem}.srt",
        f"1\n00:00:00,000 --> 00:00:04,000\n{transcript}\n",
    )
    scorecard = normalize_selection_scorecard(
        {
            "tier": 1,
            "tier_basis": "relationship_chain",
            "tier_reason": "联动关系与反转",
            "tier_evidence_cues": [1, 2],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 3,
                "audience_salience": 4,
                "relationship_interaction": 4,
                "persona_reversal": 3,
                "comedic_payoff": 3,
                "self_contained": 4,
            },
            "uncertainty_penalty": 0,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert scorecard is not None
    contract = build_story_contract(
        candidate_id=stem,
        selection_hook="南町当面追问李豆沙最喜欢谁",
        transcript_text=transcript,
        selection_scorecard=scorecard,
        session_relation_authority={
            "state": "CONFIRMED",
            "participants": ["lidousha", "nancho"],
        },
    )
    record = root / f"{stem}.record.json"
    cover_text = "南町当面追问最最最最喜欢"
    cover_binding = {
        "schema_version": contract["schema_version"],
        "relation_state": contract["relation_state"],
        "participants": contract["participants"],
        "cover_fallback_mode": contract["cover_fallback_mode"],
    }
    record.write_text(
        json.dumps(
            {
                "story_contract": contract,
                "publish_staging": {
                    "cover_text": cover_text,
                    "cover_generation": {
                        "cover_text": cover_text,
                        "rendered_lines": ["南町当面追问", "最最最最喜欢"],
                        "story_contract": cover_binding,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "status": "finished_review_package_no_upload_pending_human_review",
                "story_contract_required": True,
                "run_mode": "RECOVERY_REVIEW",
                "upload_allowed": False,
                "items": [
                    {
                        "stem": stem,
                        "title": "【李豆沙】南町当面追问最最最最喜欢",
                        "subtitle_srt": srt.name,
                        "record": record.name,
                        "ai_cover_generated": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert audit_package(root)["passed"] is True

    srt.write_text(
        "1\n00:00:00,000 --> 00:00:04,000\n大恩老师说非常亚撒西\n",
        encoding="utf-8",
    )
    result = audit_package(root)
    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "STORY_CONTRACT_SUBTITLE_HASH_DRIFT" in codes
    assert "NANCHO_ALIAS_UNRESOLVED" in codes


def test_story_contract_is_mandatory_by_date_and_cover_alias_cannot_drift(
    tmp_path: Path,
):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-22",
                "status": "finished_review_package_no_upload_pending_human_review",
                "items": [{"stem": "legacy-without-contract"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = audit_package(root)
    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "REVIEW_PACKAGE_UPLOAD_POLICY_INVALID" in codes
    assert "REVIEW_PACKAGE_RUN_MODE_INVALID" in codes
    assert "STORY_CONTRACT_RECORD_MISSING" in codes

    transcript = "南町nightin说非常亚撒西"
    srt = _write(
        root / "cover-drift.srt",
        f"1\n00:00:00,000 --> 00:00:04,000\n{transcript}\n",
    )
    scorecard = normalize_selection_scorecard(
        {
            "tier": 1,
            "tier_basis": "relationship_chain",
            "tier_reason": "联动关系与反转",
            "tier_evidence_cues": [1],
            "dimensions": {
                "lidousha_centrality": 4,
                "stance_intensity": 3,
                "audience_salience": 4,
                "relationship_interaction": 4,
                "persona_reversal": 3,
                "comedic_payoff": 3,
                "self_contained": 4,
            },
            "uncertainty_penalty": 0,
            "fatigue_penalty": 0,
        },
        start_cue=1,
        end_cue=1,
    )
    contract = build_story_contract(
        candidate_id="cover-drift",
        selection_hook="南町当面追问李豆沙",
        transcript_text=transcript,
        selection_scorecard=scorecard,
        session_relation_authority={
            "state": "CONFIRMED",
            "participants": ["lidousha", "nancho"],
        },
    )
    binding = {
        key: contract[key]
        for key in (
            "schema_version",
            "relation_state",
            "participants",
            "cover_fallback_mode",
        )
    }
    record = root / "cover-drift.record.json"
    record.write_text(
        json.dumps(
            {
                "story_contract": contract,
                "publish_staging": {
                    "cover_text": "大恩当面追问",
                    "cover_generation": {
                        "cover_text": "大恩当面追问",
                        "rendered_lines": ["大恩当面追问"],
                        "story_contract": binding,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "date": "2026-07-22",
                "run_mode": "RECOVERY_REVIEW",
                "upload_allowed": False,
                "items": [
                    {
                        "stem": "cover-drift",
                        "title": "【李豆沙】南町当面追问",
                        "subtitle_srt": srt.name,
                        "record": record.name,
                        "ai_cover_generated": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = audit_package(root)
    codes = {issue["code"] for issue in result["issues"]}
    assert result["passed"] is False
    assert "NANCHO_ALIAS_UNRESOLVED" in codes


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
