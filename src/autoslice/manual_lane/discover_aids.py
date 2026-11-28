"""`discover`: what helps the selection read a recording's raw recognition and its danmaku.

The selection reads the whole-recording recognition before any step has corrected a name.  On the two moments
other clippers cut and the lane did not nominate were both unreadable for that reason: the colleague 南町 stood in the
transcript as 「难听难听」, and on 11-24 「李豆沙不需要了」 as 「理论上不需要了」.  The tables that know better were read only
by the subtitle step.  This module gives the selection prompt three kinds of line, all from the channel's own assets:

- the people of the channel's knowledge graph the transcript or the danmaku name, with their other names;
- for those people, a spelling in the transcript that sounds exactly like their name (same pinyin, other characters).
  Only for people something already names: 亦枝 and 一直 sound the same too, and a hint for everyone in the graph would
  bury the transcript in false leads;
- the channel's known mishearings (mishearing_pairs.json) whose misheard form stands in the transcript.

They are aids for reading, never corrections: the subtitles are settled by step 3.  A channel without a knowledge graph
or a mishearing table simply has fewer lines; none at all gives no block.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from src.autoslice.acoustic_pinyin import text_pinyin_tokens
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.combined_knowledge_graph import load_layers

from . import graph_context, mishearing
from .layout import REPO_ROOT

HEAD = ("读这份字幕和弹幕时可以参考（字幕是机器转写，人名常被写成同音字；下面只用来读懂在说谁，不改字幕，"
        "每条都要按上下文判断，不是替换规则）：")
_HAN = re.compile(r"[一-鿿]+")
SAME_SOUND_MIN_CHARS = 2
SAME_SOUND_EXAMPLES = 3
LATIN_MIN_LETTERS = 3


def _written(surface: str, text: str) -> bool:
    """A name stands in the text: two characters or more as they are; a Latin name of three letters or more as a whole
    word in any case.  (Not graph_context.mentions: that follows the subtitle step's detection table, which leaves out
    exactly the short colleague names — 南町 — the selection needs.)"""
    if not surface.isascii():
        return len(surface) >= 2 and surface in text
    return (sum(ch.isalpha() for ch in surface) >= LATIN_MIN_LETTERS
            and re.search(rf"(?<![A-Za-z0-9]){re.escape(surface)}(?![A-Za-z0-9])", text, re.ASCII | re.IGNORECASE) is not None)


def same_sound(transcript: str, names: Sequence[str]) -> dict[str, list[str]]:
    """For each name written in Chinese characters: the other spellings in the transcript that have exactly its pinyin
    (南町 → 难听), most frequent first.  A name the pinyin library cannot read gives nothing."""
    runs = [(run.group(0), text_pinyin_tokens(run.group(0)) or []) for run in _HAN.finditer(transcript)]
    runs = [(text, pinyin) for text, pinyin in runs if len(pinyin) == len(text)]
    found: dict[str, list[str]] = {}
    for name in dict.fromkeys(names):
        want = (text_pinyin_tokens(name) or []) if _HAN.fullmatch(name) and len(name) >= SAME_SOUND_MIN_CHARS else []
        if len(want) != len(name):
            continue
        seen: dict[str, int] = {}
        for text, pinyin in runs:
            for at in range(len(pinyin) - len(want) + 1):
                spelling = text[at:at + len(want)]
                if pinyin[at:at + len(want)] == want and spelling != name:
                    seen[spelling] = seen.get(spelling, 0) + 1
        if seen:
            found[name] = sorted(seen, key=lambda spelling: -seen[spelling])[:SAME_SOUND_EXAMPLES]
    return found


def reading_aids(transcript: str, danmaku: Sequence[str]) -> str | None:
    """The block for the selection prompt, or None when the channel's assets have nothing to say about this recording."""
    graph = load_layers([graph_context.SHARED_ROOT, load_channel_profile(REPO_ROOT).asset_root])
    text = "\n".join([transcript, *danmaku])
    people = [n for n in graph.nodes.values() if n.kind == "person" and n.status == "accepted"
              and any(_written(surface, text) for surface in n.surfaces())]
    other_names = {s.casefold() for n in people for s in n.surfaces() if s != n.names["display"]}
    lines = []
    for node in people:
        display = node.names["display"]
        if display.casefold() in other_names:    # a node that is only another spelling of someone listed under their name
            continue
        others = [s for s in dict.fromkeys(node.surfaces()) if s != display]
        line = f"- {display}" + (f"（也写作／也叫：{'、'.join(others)}）" if others else "")
        for name, spellings in same_sound(transcript, [s for s in node.names.values() if not s.isascii()]).items():
            line += f"；字幕里的「{'」「'.join(spellings)}」和「{name}」同音，可能就是在说{display}"
        lines.append(line)
    table = mishearing.OUTPUT
    if table.exists():
        heard = dict.fromkeys((row["misheard"], row["standard"]) for row in mishearing.load_pairs(table)
                              if len(row["misheard"]) >= 2 and mishearing.surface_matches(row, transcript))
        lines += [f"- 字幕里的「{misheard}」有时是「{standard}」被听错（这个词本身也常是原话，按上下文判断）" for misheard, standard in heard]
    return "\n".join([HEAD, *lines]) if lines else None
