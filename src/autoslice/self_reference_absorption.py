"""Deterministic phonetic absorption for host self-reference name slots.

Static glossary membership must not make one real person outrank another.  This
module handles a narrower, evidence-bearing case: Chinese grammar places a name
in the host self-reference slot (找X来、因为X、用X的方式、同意X把…), and that
surface is phonetically close to 李豆沙.  Only then is the surface absorbed.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Any

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.subtitle_fidelity import _toneless_syllables


_CJK_NAME = r"[\u3400-\u9fff]{2,4}"
_SELF_REFERENCE_SLOT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        rf"找(?P<surface>{_CJK_NAME})来",
        rf"因为(?P<surface>{_CJK_NAME})(?:$|[，,。！？\s]|切|是|把)",
        rf"用(?P<surface>{_CJK_NAME})的方式",
        rf"同意(?P<surface>{_CJK_NAME})把",
        rf"给了(?P<surface>{_CJK_NAME})一个",
        rf"(?P<surface>{_CJK_NAME})一直是",
        rf"(?P<surface>{_CJK_NAME})是什么",
    )
)
_CANONICALS = frozenset({"李豆沙", "小李", "豆沙"})
_EXCLUDED = frozenset({"礼墨", "李姐", "老师", "官方"})
_MIN_LIDOUSHA_PHONETIC_RATIO = 0.58


def _phonetic_text(value: str) -> str:
    syllables = _toneless_syllables(value)
    return " ".join(syllables)


def lidousha_phonetic_ratio(value: str) -> float:
    candidate = _phonetic_text(value)
    target = _phonetic_text("李豆沙")
    if not candidate or not target:
        return 0.0
    return SequenceMatcher(None, candidate, target, autojunk=False).ratio()


def absorb_host_self_references(srt_text: str) -> tuple[str, dict[str, Any]]:
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    repairs: list[dict[str, Any]] = []
    for cue_offset, (cue, text) in enumerate(zip(cues, texts)):
        replacements: list[tuple[int, int, str, float, str]] = []
        for pattern in _SELF_REFERENCE_SLOT_PATTERNS:
            for match in pattern.finditer(text):
                surface = match.group("surface")
                if surface in _CANONICALS or surface in _EXCLUDED:
                    continue
                ratio = lidousha_phonetic_ratio(surface)
                if ratio < _MIN_LIDOUSHA_PHONETIC_RATIO:
                    continue
                start, end = match.span("surface")
                replacements.append((start, end, surface, ratio, pattern.pattern))
        if not replacements:
            continue
        # Replace right-to-left; overlapping grammar matches collapse to one.
        selected: list[tuple[int, int, str, float, str]] = []
        for row in sorted(replacements, key=lambda item: (item[0], item[1])):
            if selected and row[0] < selected[-1][1]:
                if row[3] > selected[-1][3]:
                    selected[-1] = row
                continue
            selected.append(row)
        after = text
        for start, end, surface, ratio, grammar in reversed(selected):
            after = after[:start] + "李豆沙" + after[end:]
            repairs.append(
                {
                    "cue_index": cue_offset + 1,
                    "matched_start_ms": cue.start_ms,
                    "matched_end_ms": cue.end_ms,
                    "before": surface,
                    "after": "李豆沙",
                    "phonetic_ratio": round(ratio, 4),
                    "phonetic_candidate": _phonetic_text(surface),
                    "phonetic_target": _phonetic_text("李豆沙"),
                    "grammar_pattern": grammar,
                    "authority": "HOST_SELF_REFERENCE_GRAMMAR_PLUS_PHONETIC_ABSORPTION",
                }
            )
        texts[cue_offset] = after
    if not repairs:
        return srt_text, {
            "schema_version": "self-reference-absorption-audit.v1",
            "status": "NO_MATCH",
            "repairs": [],
        }
    output = "\n".join(
        f"{index}\n{_ms(cue.start_ms)} --> {_ms(cue.end_ms)}\n{text}\n"
        for index, (cue, text) in enumerate(zip(cues, texts), start=1)
    )
    return output, {
        "schema_version": "self-reference-absorption-audit.v1",
        "status": "APPLIED",
        "repairs": repairs,
    }


def _ms(value_ms: int) -> str:
    hours, rem = divmod(int(value_ms), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
