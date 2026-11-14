"""The channel's basic facts are read in one place by every step that judges her words (owner)."""

from __future__ import annotations

from src.autoslice.manual_lane import channel_facts, title_understanding

TEXT = ("# 标题行\n\n- 身份：主播。\n- 封面形象规则：不加帽子。\n- 粉丝文化：观众叫某某。\n"
        "## 封面表情/角色映射\n- 默认表情：软萌。\n\n## 切片中积累的已审定特质\n- 形态：两种。 <!-- pf:pf-abc@r1 -->\n")


def test_the_title_reads_the_whole_file_without_the_cover_expression_section():
    basics = channel_facts.basics(TEXT)
    assert "默认表情" not in basics and "封面形象规则" in basics and "形态：两种。 <!-- pf:pf-abc@r1 -->" in basics
    assert title_understanding.persona_basics(TEXT) == basics


def test_a_reader_of_her_speech_gets_no_cover_rule_and_no_ledger_marker():
    spoken = channel_facts.spoken(TEXT)
    assert spoken.splitlines() == ["- 身份：主播。", "- 粉丝文化：观众叫某某。", "## 切片中积累的已审定特质", "- 形态：两种。"]
    assert channel_facts.fans(TEXT) == "粉丝文化：观众叫某某。" and channel_facts.fans("- 身份：主播。\n") == ""


def test_the_subtitle_agent_reads_the_same_file_as_the_title():
    block = channel_facts.for_subtitles()
    assert block.startswith(channel_facts.SUBTITLE_NOTE) and "不是语音证据" in block
    for line in channel_facts.spoken().splitlines():
        assert line in block and line.split(" <!--")[0] in title_understanding.persona_basics()
