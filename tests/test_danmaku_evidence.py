import pytest

from scripts.gemini_slice_jingting import agy_prompt
from src.autoslice.danmaku_evidence import (
    DanmakuItem,
    danmaku_in_window,
    find_danmaku_bursts,
    format_danmaku_lines,
    parse_blrec_danmaku_xml,
)
from src.autoslice.semantic_candidate_selector import build_semantic_recall_prompt


def _xml(entries: list[tuple[float, str]]) -> str:
    body = "\n".join(
        f'<d p="{offset:.3f},1,25,16777215,1782961247649,0,ea71f718,578340245" uid="0" user="塔***">{text}</d>'
        for offset, text in entries
    )
    return f"""<?xml version='1.0' encoding='utf-8'?>
<i>
    <metadata><room_id>123456</room_id></metadata>
    {body}
</i>"""


def test_parse_blrec_xml_extracts_offsets_and_text():
    items = parse_blrec_danmaku_xml(_xml([(0.0, "这个墨镜是怎么设计的"), (4.487, "我朝"), (4.612, "会赢吗家人们")]))
    assert [(item.offset_ms, item.text) for item in items] == [
        (0, "这个墨镜是怎么设计的"),
        (4487, "我朝"),
        (4612, "会赢吗家人们"),
    ]


def test_parse_rejects_broken_xml_and_skips_empty_text():
    with pytest.raises(ValueError):
        parse_blrec_danmaku_xml("not xml at all <d")
    items = parse_blrec_danmaku_xml(_xml([(1.0, ""), (2.0, "有内容")]))
    assert [item.text for item in items] == ["有内容"]


def test_bursts_found_relative_to_baseline():
    # Baseline chatter: 1 danmaku per 30s bucket; burst: 12 in one bucket.
    entries = [(float(t), f"平常{t}") for t in range(0, 1800, 30)]
    entries += [(1380.0 + i, f"突发{i}") for i in range(12)]
    bursts = find_danmaku_bursts(parse_blrec_danmaku_xml(_xml(entries)))
    assert bursts
    top = bursts[0]
    assert top.start_ms == 1_380_000
    assert top.count >= 12
    assert any("突发" in text for text in top.sample_texts)


def test_no_bursts_on_flat_traffic():
    entries = [(float(t), "平常") for t in range(0, 1800, 30)]
    assert find_danmaku_bursts(parse_blrec_danmaku_xml(_xml(entries))) == []


def test_window_extraction_and_line_formatting():
    items = [DanmakuItem(600_000, "这是ai吗"), DanmakuItem(620_000, "灰喜鹊拟人"), DanmakuItem(700_000, "窗外")]
    in_window = danmaku_in_window(items, 600_000, 690_000)
    assert [item.text for item in in_window] == ["这是ai吗", "灰喜鹊拟人"]
    lines = format_danmaku_lines(in_window, base_ms=600_000)
    assert lines == ["00:00 这是ai吗", "00:20 灰喜鹊拟人"]


def test_semantic_recall_prompt_includes_danmaku_hints():
    from src.autoslice.review_evidence import SourceCue

    cues = [
        SourceCue(cue_id="u_1", source_start_ms=0, source_end_ms=4_000, text="第一句", language="zh", kind="speech", confidence=1.0)
    ]
    hints = "23:00-24:00 (弹幕x12): 反沙 / 绕口令"
    prompt = build_semantic_recall_prompt(cues, max_candidates=3, danmaku_hints=hints)
    assert "弹幕突发区" in prompt
    assert "23:00-24:00 (弹幕x12)" in prompt
    # Without hints the block is absent.
    assert "弹幕突发区" not in build_semantic_recall_prompt(cues, max_candidates=3)


def test_agy_prompt_includes_danmaku_lines_and_visual_read_instruction():
    prompt = agy_prompt("1\n00:00:01,000 --> 00:00:02,000\n你好\n", danmaku_lines=["00:05 灰喜鹊拟人", "00:12 一眼AI"])
    assert "given to you VERBATIM" in prompt  # exact platform text, not re-OCR'd
    assert "not by itself proof" in prompt
    assert "PROPER-NAME CONFLICT RULE" in prompt
    assert "listen to the actual" in prompt
    assert "TEMPORAL PAIRING RULE" in prompt
    assert "00:05 灰喜鹊拟人" in prompt
    assert "READ the on-screen text" in prompt
    # Without danmaku the visual-read instruction still stands, hint block absent.
    bare = agy_prompt("1\n00:00:01,000 --> 00:00:02,000\n你好\n")
    assert "READ the on-screen text" in bare
    assert "given to you VERBATIM" not in bare
