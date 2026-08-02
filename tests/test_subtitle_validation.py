from pathlib import Path

from src.autoslice.subtitle_validation import validate_srt_file, validate_srt_text


def test_release_srt_validator_rejects_every_malformed_block():
    text = """1
00:00:00,000 --> 00:00:01,000
正常字幕

this block used to be silently skipped
"""

    result = validate_srt_text(text)

    assert result["status"] == "FAIL"
    assert result["block_count"] == 2
    assert {row["code"] for row in result["errors"]} >= {
        "SRT_BLOCK_TOO_SHORT"
    }


def test_release_srt_validator_rejects_short_single_character_and_overlap():
    text = """1
00:00:00,000 --> 00:00:00,240
的

2
00:00:00,200 --> 00:00:01,000
下一句
"""

    result = validate_srt_text(text)
    codes = {row["code"] for row in result["errors"]}

    assert result["status"] == "FAIL"
    assert {"SRT_CUE_TOO_SHORT", "SRT_SINGLE_CJK_CHARACTER", "SRT_CUE_OVERLAP"} <= codes


def test_release_srt_validator_accepts_single_character_interjections():
    """哎/啊/呵 are a closed interjection class, including terminal punctuation."""

    text = """1
00:00:00,000 --> 00:00:01,000
哎

2
00:00:01,000 --> 00:00:02,000
啊！

3
00:00:02,000 --> 00:00:03,000
呵
"""

    result = validate_srt_text(text)
    codes = {row["code"] for row in result["errors"]}

    assert "SRT_SINGLE_CJK_CHARACTER" not in codes
    assert result["status"] == "PASS"


def test_terminal_punctuation_does_not_hide_single_content_character():
    text = """1
00:00:00,000 --> 00:00:01,000
切，

2
00:00:01,000 --> 00:00:03,000
那就差乙乙没吃了
"""

    result = validate_srt_text(text)
    codes = [
        (row["code"], row["block"])
        for row in result["errors"]
        if row["code"] == "SRT_SINGLE_CJK_CHARACTER"
    ]

    assert codes == [("SRT_SINGLE_CJK_CHARACTER", 1)]


def test_release_srt_validator_accepts_standalone_you_answer_only():
    """\u300c\u6709\u300d\u53ef\u4ee5\u662f\u5b8c\u6574\u80af\u5b9a\u56de\u7b54\uff1b\u5176\u4ed6\u5b9e\u8bcd\u5355\u5b57\u4ecd\u4e0d\u673a\u68b0\u653e\u884c\u3002"""

    text = """1
00:00:00,000 --> 00:00:01,000
\u6709

2
00:00:02,000 --> 00:00:03,000
\u884c
"""

    result = validate_srt_text(text)
    codes = [
        (row["code"], row["block"])
        for row in result["errors"]
        if row["code"] == "SRT_SINGLE_CJK_CHARACTER"
    ]

    assert ("SRT_SINGLE_CJK_CHARACTER", 1) not in codes
    assert ("SRT_SINGLE_CJK_CHARACTER", 2) in codes


def test_release_srt_validator_accepts_consecutive_non_overlapping_cues(tmp_path: Path):
    path = tmp_path / "ok.srt"
    path.write_text(
        """1
00:00:00,000 --> 00:00:00,800
正常字幕

2
00:00:00,800 --> 00:00:01,500
第二句话
""",
        encoding="utf-8",
    )

    assert validate_srt_file(path)["status"] == "PASS"


def test_single_cjk_name_echo_is_exempt_isolated_fragment_blocks():
    """秦秦/秦 呼名回声（2026-07-26 3573 案）：单字与紧邻 cue 的 ≥2 字词共字
    即真实回声放行；孤立实词碎片仍 fail-closed。"""

    from src.autoslice.subtitle_validation import validate_srt_text

    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n秦秦\n\n"
        "2\n00:00:02,100 --> 00:00:03,000\n秦\n\n"
        "3\n00:00:03,100 --> 00:00:04,000\n她是一个非常闹腾的小朋友\n\n"
        "4\n00:00:05,000 --> 00:00:06,000\n狗\n"
    )
    result = validate_srt_text(srt)
    codes = [(error["code"], error["block"]) for error in result["errors"]]
    assert ("SRT_SINGLE_CJK_CHARACTER", 2) not in codes
    assert ("SRT_SINGLE_CJK_CHARACTER", 4) in codes


def test_merge_release_grade_cues_absorbs_slivers_and_single_chars():
    """1863 案（2026-07-27）：240ms「哦」与独立「行」是真实语音，发布校验
    拒得对——生产端贴邻合并后包直接达发布级；名回声豁免块不动。"""

    from src.autoslice.cue_split_hygiene import merge_release_grade_cues
    from src.autoslice.subtitle_validation import validate_srt_text

    srt = (
        "1\n00:00:25,190 --> 00:00:27,130\n是你在玩游戏我就走了\n\n"
        "2\n00:00:27,130 --> 00:00:27,370\n哦\n\n"
        "3\n00:00:27,370 --> 00:00:28,010\n这样吗\n\n"
        "4\n00:01:15,720 --> 00:01:17,200\n嘻，晓得吧\n\n"
        "5\n00:01:17,280 --> 00:01:18,280\n行\n\n"
        "6\n00:01:18,590 --> 00:01:22,660\n谢谢刚刚panoja的舰长\n\n"
        "7\n00:01:22,660 --> 00:01:23,380\n切，\n\n"
        "8\n00:01:23,380 --> 00:01:25,940\n那就差乙乙没吃了\n"
    )
    out, rows = merge_release_grade_cues(srt)
    assert "哦，这样吗" in out
    assert "嘻，晓得吧，行" in out
    assert "切，那就差乙乙没吃了" in out
    assert validate_srt_text(out)["status"] == "PASS"
    actions = {row["text"]: row["action"] for row in rows}
    assert actions == {
        "哦": "MERGED_INTO_NEXT",
        "行": "MERGED_INTO_PREV",
        "切，": "MERGED_INTO_NEXT",
    }

    # 名回声豁免（秦秦/秦）自动保留：校验不拒 → 合并器不动
    echo = (
        "1\n00:00:05,000 --> 00:00:07,000\n秦秦\n\n"
        "2\n00:00:07,000 --> 00:00:08,000\n秦\n"
    )
    out2, rows2 = merge_release_grade_cues(echo)
    assert rows2 == []
    assert "秦秦" in out2 and out2.count("秦") >= 3

    # 不贴邻（间隙>150ms）不并、披露
    gap = (
        "1\n00:00:05,000 --> 00:00:05,200\n呃\n\n"
        "2\n00:00:06,000 --> 00:00:08,000\n后面这句离得远\n"
    )
    out3, rows3 = merge_release_grade_cues(gap)
    assert rows3 and rows3[0]["action"] == "UNMERGEABLE_NOT_CONTIGUOUS"
