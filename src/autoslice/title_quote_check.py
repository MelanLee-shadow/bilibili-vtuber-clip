"""Does every quotation in a title appear in the final subtitles, said by the right speaker.

Split out of title_cover_quotes so the lane runs the same check without loading the title policy and the LLM client
that the rest of that module uses; title_cover_quotes re-exports the names for its existing importers."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from .channel_envelope import talk_title_body

PUNCT = re.compile(r"""[\s，。！？、；：,.!?;:“”"'‘’「」『』（）()《》〈〉…—\-~～·]""")


QUOTES = re.compile(r'“([^”]+)”|"([^"]+)"|「([^」]+)」|『([^』]+)』|‘([^’]+)’')


def canonical(text: str) -> str:
    return PUNCT.sub("", text)


def final_cues(srt: str) -> list[dict[Any, Any]]:
    rows = []
    for block in re.split(r"\n\s*\n", srt.replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if len(lines) < 3 or " --> " not in lines[1]:
            continue
        text = " ".join(lines[2:])
        speaker = ""
        match = re.match(r"\[([^\]]+)\]\s*(.*)", text)
        if match:
            speaker, text = match.groups()
        rows.append(
            {
                "cue": lines[0],
                "time": lines[1].split(" --> ")[0],
                "timing": lines[1],
                "speaker": speaker,
                "text": text,
            }
        )
    return rows


def _destutter(text: str) -> str:
    # Only explicitly separated repetitions; do not collapse 人人 or 好好学习.
    return re.sub(r"([^\s，,、。]{1,4})(?:[\s，,、]+\1)+", r"\1", text)


def find_quote(quote: str, srt: str, *, speaker: str = "") -> dict[Any, Any] | None:
    needle = canonical(quote)
    if not needle:
        return None
    cues = final_cues(srt)
    for size in range(1, 5):
        for start in range(len(cues) - size + 1):
            window = cues[start : start + size]
            speakers = {row["speaker"] for row in window}
            if len(speakers - {""}) > 1:
                continue
            if speaker and speakers != {speaker}:
                continue
            original = "".join(canonical(row["text"]) for row in window)
            destuttered = canonical(_destutter(" ".join(row["text"] for row in window)))
            if needle in original or needle in destuttered:
                known = next(iter(speakers)) if len(speakers) == 1 else ""
                return {
                    "cues": [r["cue"] for r in window],
                    "time": window[0]["time"],
                    "speaker": known,
                    "speaker_status": "VERIFIED_SUBTITLE" if known else "UNKNOWN",
                    "quote": quote,
                    "match": "VERBATIM" if needle in original else "STUTTER_REMOVED",
                }
    return None


def check_title_quotes(title: str, final_srt: str, *, declared_quote: object = None) -> dict[Any, Any]:
    # A title needs no verbatim quote; every quotation mark it uses must be backed.
    phrases = [next(v for v in match if v) for match in QUOTES.findall(title)]
    errors = []
    speaker = ""
    if isinstance(declared_quote, Mapping):
        speaker = str(declared_quote.get("speaker") or "").strip()
        declared_quote = declared_quote.get("text")
    if isinstance(declared_quote, str) and declared_quote.strip():
        # A declared quote is a claim about this title, so it must appear in it.
        if canonical(declared_quote) not in canonical(title):
            errors.append("TITLE_DECLARED_QUOTE_MISSING")
        elif declared_quote not in phrases:
            phrases.append(declared_quote)
    if not final_cues(final_srt):
        errors.append("TITLE_FINAL_SUBTITLES_REQUIRED")
    for left, right in (("“", "”"), ("「", "」"), ("『", "』"), ("‘", "’")):
        if title.count(left) != title.count(right):
            errors.append("TITLE_QUOTE_UNBALANCED")
    if title.count('"') % 2:
        errors.append("TITLE_QUOTE_UNBALANCED")
    matches = []
    body = talk_title_body(title)
    for phrase in phrases:
        # A declared speaker is a claim, never authority. Explicit attribution
        # beside a quote also requires the final subtitle to identify that speaker.
        attributed = speaker
        for label in ("李豆沙", "kmx", "KMX"):
            if re.search(
                re.escape(label)
                + r"(?:说|回|喊|来|一句|当场说|表示)*[：:\s]*[“\"「『‘]?"
                + re.escape(phrase),
                body,
            ):
                attributed = label.lower() if label.upper() == "KMX" else label
        match = find_quote(phrase, final_srt, speaker=attributed)
        matches.append(match)
        if attributed and match is None and find_quote(phrase, final_srt) is not None:
            errors.append("TITLE_QUOTE_SPEAKER_UNVERIFIED")
    if any(match is None for match in matches):
        errors.append("TITLE_QUOTE_NOT_IN_FINAL_SUBTITLES")
    return {
        "schema_version": "lidousha-title-quote-check.v1",
        "status": "PASS" if not errors else "FAIL",
        "reason_codes": sorted(set(errors)),
        "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
        "final_srt_sha256": hashlib.sha256(final_srt.encode()).hexdigest(),
        "matches": matches,
        "audio_calls": 0,
    }
