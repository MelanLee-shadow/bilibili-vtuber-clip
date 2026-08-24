#!/usr/bin/env python3
"""Materialize C10's private, source-bound subtitle repair.

The watched BV is subtitle authority for the adapted lyrics.  It is not a
reason to erase the watched-song portion of the recut: the old projection did
exactly that and was rejected.  Existing outside/live text stays intact unless
the bounded ruling or watched-screen evidence supplies a correction.
"""
from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice.jingting_chunker import parse_srt_cues


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "subtitles/auto_143025_868_1094.pipeline-diagnostic.srt"
OUTPUT = ROOT / "subtitles/auto_143025_868_1094.release.srt"
# These cue ordinals are direct audio matches to BV1wF411g7YT.  The text below
# was transcribed from its visible chat-bubble lyrics, not from the original
# song BV or an ASR guess.  Cue 13 remains the existing outer-stream line.
WATCHED_SCREEN_TEXT = {
    6: "今天进社社社社团交朋友", 7: "才不是磕上了哪对 couple",
    8: "她左手拉右手", 9: "她左手拉右手", 10: "美女里还有一只狗",
    11: "五等分分分分分白毛女友", 12: "妹妹loli爱豆巨乳全都有",
    14: "妹妹loli爱豆巨乳全都有", 15: "哥哥他不懂事", 16: "全是直播故事",
    17: "女子会尺度有点愁", 18: "看看群友", 19: "天气转暖大家都没穿得很厚",
    20: "psp&novus真是漂亮姐姐遛地走！", 21: "啊—魔法少女沾酱油",
    22: "震慑人心歌喉", 23: "熊耳朵粉挑染都好美哦", 24: "真舍不得抱一下就走",
    25: "哦—扳手和工装裤", 26: "爸爸活也能有", 27: "老师是瑰宝我心动一丝不苟",
    28: "有人说我花心见一个爱一个真像个铝铜", 29: "咋的你不好女色色即是空是你的事",
    30: "我就是喜欢喜欢漂亮姐姐关你屁事", 31: "对不起我今天喝了点酒有一点上头",
    32: "多看了一眼不好意思我真不是铝铜", 33: "Novus铁直就是我今天我不干好事",
    34: "看两眼太爽啦唱首爵士", 35: "啊—露肩和服毛绒耳朵", 36: "和我共享女朋友",
    37: "枸杞成精国风温柔", 38: "真舍不得喝口茶就走", 39: "哦—紧身旗袍生姜头",
    40: "辣到我发愁", 41: "女孩是瑰宝我心动一丝不苟", 42: "啊—贝雷帽水母头",
    43: "吃外卖员爱好我都懂", 44: "姐姐们穿得都好美哦", 45: "哦—海兔イケボ",
    46: "直击我胸口", 47: "见一个爱一个我真不是铝铜", 48: "心有铁拳拳拳拳细唱干花",
    49: "人型唢呐把你声卡都唱炸", 50: "小时候不懂事", 51: "学海豹算数字",
    52: "张口来5555835？！", 53: "今天进社社社社团交朋友", 54: "才不是磕上了哪对 couple",
    55: "她左手拉右手", 56: "她左手拉右手", 57: "美女里还有一只狗",
    58: "五等分分分分分白毛女友", 59: "妹妹loli爱豆巨乳全都有", 60: "今天进社社社社团交朋友",
    61: "我左手拉右手，再右手拉左手", 62: "旁边还站着一只狗",
}
REPLACEMENTS = {73: "谢谢你 arigatou"}
# Independently present in the recut ASR but absent from the aligned watched
# audio.  They stay as concurrent outer-stream lines rather than overwriting
# the on-screen lyric below them.
CONCURRENT_LIVE = ((83_060, 84_580, "这谁唱呢"), (87_920, 88_540, "是谁唱的"))


def stamp(ms: int) -> str:
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def main() -> None:
    cues = parse_srt_cues(SOURCE.read_text(encoding="utf-8"))
    rendered = []
    for source_ordinal, cue in enumerate(cues, start=1):
        text = WATCHED_SCREEN_TEXT.get(
            source_ordinal, REPLACEMENTS.get(source_ordinal, str(cue.text).strip())
        )
        rendered.append((cue.start_ms, cue.end_ms, text))
    for start_ms, end_ms, text in CONCURRENT_LIVE:
        rendered.append((start_ms, end_ms, text))
    rendered.sort(key=lambda row: (row[0], row[1], row[2]))
    blocks = [
        f"{ordinal}\n{stamp(start_ms)} --> {stamp(end_ms)}\n{text}"
        for ordinal, (start_ms, end_ms, text) in enumerate(rendered, start=1)
    ]
    OUTPUT.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
