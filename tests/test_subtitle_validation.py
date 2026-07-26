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
    """哎/啊/呵 are a closed interjection class, not ASR shatter."""

    text = """1
00:00:00,000 --> 00:00:01,000
哎

2
00:00:01,000 --> 00:00:02,000
啊

3
00:00:02,000 --> 00:00:03,000
呵
"""

    result = validate_srt_text(text)
    codes = {row["code"] for row in result["errors"]}

    assert "SRT_SINGLE_CJK_CHARACTER" not in codes
    assert result["status"] == "PASS"


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
