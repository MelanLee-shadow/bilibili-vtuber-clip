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


# --------------------------------------------------------------------------
# 提示名额（`DANMAKU_HINT_MAX_BURSTS`）——维护者「6 要放宽」
#
# 旧实现是 `find_danmaku_bursts(items)`（被调方默认 max_bursts=8）再 `bursts[:6]`：
# **两道帽子，小的那道在调用方、大的那道藏在被调方**。8/7 那场 660000-690000 的
# 爆发排第 7，被 `[:6]` 丢掉，选题模型连提示都没看到。下面三件事各钉一条：
# 超过旧上限能进提示、仍然有上限、8/7 真实弹幕的那条爆发必须在提示里。
# --------------------------------------------------------------------------


def _bursty_session_xml(counts: list[int]) -> str:
    """奇数桶放强度递减的爆发、偶数桶放 1 条基线闲聊。

    非空桶中位数恒为 1 → 判定阈值 = max(6, 1×2.0) = 6，所以 `counts` 里每个
    ≥6 的值都会成为一条独立爆发（奇数桶互不相邻，不会被合并）。检测算术未被触碰，
    只是给它一个爆发数可控的输入。
    """

    entries: list[tuple[float, str]] = [(float(bucket * 30), "闲聊") for bucket in range(0, 60, 2)]
    for index, count in enumerate(counts):
        bucket = 1 + index * 2
        entries += [(bucket * 30 + i * 0.01, f"爆发{count}号") for i in range(count)]
    return _xml(sorted(entries))


def _hint_lines(hints: str | None) -> list[str]:
    assert hints is not None
    header, _, body = hints.partition("\n")
    assert "弹幕突发时段" in header
    return body.split("\n")


def test_hints_carry_bursts_past_the_old_top6_and_past_the_callee_default(tmp_path):
    """名额放宽必须同时穿透两道帽子，只改调用方的切片是半修。"""

    from src.autoslice.danmaku_evidence import DANMAKU_HINT_MAX_BURSTS
    from src.autoslice.talk_lane import danmaku_hints

    # 12 条强度互不相同的爆发（30..19），排名因此完全确定。
    counts = list(range(30, 18, -1))
    xml_path = tmp_path / "twelve.xml"
    xml_path.write_text(_bursty_session_xml(counts), encoding="utf-8")

    lines = _hint_lines(danmaku_hints(xml_path))
    assert len(lines) == 12  # 12 < 名额上限，所以一条都不该丢
    # 第 7 名（x24）：旧的 `[:6]` 正是在这里把 8/7 那条爆发丢掉的。
    assert any("x24:" in line for line in lines)
    # 第 9..12 名（x22..x19）：**只有显式传 max_bursts 才拿得到**——被调方默认 8
    # 会在这里截断，所以这几条是「名额真的穿透到了检测层」的唯一证据。
    for count in (22, 21, 20, 19):
        assert any(f"x{count}:" in line for line in lines), f"x{count} 被被调方默认 max_bursts=8 截掉了"
    assert DANMAKU_HINT_MAX_BURSTS >= 12


def test_hints_stay_capped_and_drop_only_the_weakest_bursts(tmp_path):
    """放宽不等于无界：超额时保留最强的 N 条，丢最弱的。"""

    from src.autoslice.danmaku_evidence import DANMAKU_HINT_MAX_BURSTS
    from src.autoslice.talk_lane import danmaku_hints

    counts = list(range(40, 14, -1))  # 26 条，超过名额
    xml_path = tmp_path / "flood.xml"
    xml_path.write_text(_bursty_session_xml(counts), encoding="utf-8")

    lines = _hint_lines(danmaku_hints(xml_path))
    assert len(lines) == DANMAKU_HINT_MAX_BURSTS
    assert len(lines) < len(counts)  # 有上限，不是把全部爆发倒进 prompt
    kept = set(counts[:DANMAKU_HINT_MAX_BURSTS])
    dropped = set(counts[DANMAKU_HINT_MAX_BURSTS:])
    assert all(any(f"x{count}:" in line for line in lines) for count in kept)
    assert not any(f"x{count}:" in line for count in dropped for line in lines)


def test_hints_are_rendered_in_timeline_order(tmp_path):
    """提示按时间顺序呈现，好让模型跟 `#编号 [开始-结束]` 的字幕时间轴单调对位。

    强度信号没有丢：它就在每行的 `x{count}` 里。
    """

    from src.autoslice.talk_lane import danmaku_hints

    xml_path = tmp_path / "ordered.xml"
    xml_path.write_text(_bursty_session_xml([9, 30, 12, 21]), encoding="utf-8")

    lines = _hint_lines(danmaku_hints(xml_path))
    stamps = [line.split(" ", 1)[0] for line in lines]
    assert stamps == sorted(stamps)
    # 时间序 = 桶 1/3/5/7 → 00:30 / 01:30 / 02:30 / 03:30，强度序会是 30/21/12/9。
    assert stamps == ["00:30", "01:30", "02:30", "03:30"]
    assert [line.split("x", 1)[1].split(":", 1)[0] for line in lines] == ["9", "30", "12", "21"]


# 8/7 那场 `22966160_20260807-22-37-50.xml` 的真实 30 秒桶直方图（全场 602 条弹幕）。
# 与 `tests/test_boundary_payoff_extension.py::REAL_DANMAKU_BUCKETS` 是同一个文件、
# 同一份实测（从 free 只读重放核对过，逐桶一致）。
# `find_danmaku_bursts` 只看桶计数，所以按直方图重放与用原始 XML 完全等价：
# 阈值同为 max(6, 2 × 非空桶中位数 9) = 18，检测出 8 个窗口，
# 660000-690000（x22）按 count 排第 7 —— 正是旧 `[:6]` 丢掉的那一条。
REAL_20260807_BUCKETS: tuple[tuple[int, int], ...] = (
    (0, 7), (1, 3), (2, 1), (3, 11), (4, 4), (5, 1), (6, 3), (7, 12), (8, 4), (9, 3),
    (10, 22), (11, 17), (12, 14), (13, 18), (14, 3), (15, 19), (16, 18), (17, 7),
    (18, 5), (19, 8), (20, 24), (21, 11), (22, 22), (23, 15), (24, 10), (25, 6),
    (26, 4), (27, 6), (28, 6), (29, 2), (30, 9), (31, 11), (32, 6), (33, 25),
    (34, 12), (35, 11), (36, 6), (37, 13), (38, 9), (39, 12), (41, 4), (42, 4),
    (43, 1), (44, 21), (45, 21), (46, 16), (47, 9), (48, 11), (49, 11), (50, 9),
    (51, 17), (52, 29), (53, 15), (54, 10), (55, 7), (56, 2), (57, 4), (58, 8),
    (59, 3),
)


def test_20260807_payoff_burst_reaches_the_selection_prompt(tmp_path):
    """回归：8/7 真实弹幕里 660000-690000 那条爆发必须出现在选题 prompt 里。

    它就是 `auto_223750_578_654` 被切在包袱之前的实证成因
    （内部盲评真值文档留存）。
    断言的是「在不在」而不是「排第几」：排第 7 依赖两条 x22 之间的稳定排序平票，
    钉排名会让这条测试变脆，而漏没漏掉只跟在不在有关。
    """

    from src.autoslice.review_evidence import SourceCue
    from src.autoslice.talk_lane import danmaku_hints

    entries = [
        (bucket * 30 + index * 0.01, f"弹幕{bucket}_{index}")
        for bucket, count in REAL_20260807_BUCKETS
        for index in range(count)
    ]
    assert len(entries) == 602
    xml_path = tmp_path / "22966160_20260807-22-37-50.xml"
    xml_path.write_text(_xml(entries), encoding="utf-8")

    hints = danmaku_hints(xml_path)
    lines = _hint_lines(hints)
    assert len(lines) == 8  # 这场真实弹幕一共 8 个窗口，现在一条都不丢
    assert any(line.startswith("11:00 x22:") for line in lines), "660000-690000 那条爆发又被截掉了"
    # 而且要真的落进喂给选题模型的 prompt，不只是留在字符串里。
    cues = (
        SourceCue(
            cue_id="u_1", source_start_ms=578_030, source_end_ms=582_000,
            text="我以为都能吃掉", language="zh", kind="speech", confidence=1.0,
        ),
    )
    prompt = build_semantic_recall_prompt(cues, max_candidates=4, danmaku_hints=hints)
    assert "11:00 x22:" in prompt
