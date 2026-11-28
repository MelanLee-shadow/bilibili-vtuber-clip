"""Is every quotation in a title what that person said in the final subtitles (verbatim, or cut down from it).

Split out of title_cover_quotes so the lane runs the same check without loading the title policy and the LLM client
that the rest of that module uses; title_cover_quotes re-exports the names for its existing importers."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping
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


def _take(rows: list[dict[Any, Any]], needle: str) -> tuple[list[str], int, bool]:
    """The letters of `needle` taken greedily, in order, from `rows`: the cues used, how many letters were found, and
    whether a negation was left out of a clause a later letter was taken from (_drops_negation)."""
    used: list[str] = []
    at, flipped = 0, False
    for row in rows:
        before = at
        for clause in filter(None, (canonical(c) for c in CLAUSE.split(row["text"]))):
            taken = set()
            for i, letter in enumerate(clause):
                if at < len(needle) and letter == needle[at]:
                    at += 1
                    taken.add(i)
            flipped = flipped or _drops_negation(clause, taken)
        if at > before:
            used.append(row["cue"])
    return used, at, flipped


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
            # the text the quotation stands in (the stutters taken out when only that holds it) may not be cut past a
            # negation: 「我没有把自己变绿」 -> 「把自己变绿」
            read = window if needle in original else [{"cue": window[0]["cue"], "text": _destutter(" ".join(r["text"] for r in window))}]
            if (needle in original or needle in destuttered) and not _take(read, needle)[2]:
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


CONDENSED_WINDOW = 6     # neighbouring cues one speaker's condensed quotation may be drawn from
CLAUSE = re.compile(r"[\s，。！？、；：,.!?;:…—~～]+")
NEGATIONS = "不没别未无非勿莫"
NOT_NEGATIONS = ("非常", "不过", "不管", "无论", "不论")   # words with a negation letter that negate nothing


def _drops_negation(clause: str, taken: set[int]) -> bool:
    """A negation letter of `clause` left out while a letter after it in the same clause is kept (「我不是说你是猪」 ->
    「我是猪」, 「我没有把自己变绿」 -> 「把自己变绿」): the cut says the opposite.  A clause left out whole, or cut after its
    last kept letter, says nothing it did not."""
    def in_word(i: int) -> bool:
        return any(clause.startswith(w, j) for w in NOT_NEGATIONS for j in range(max(0, i - len(w) + 1), i + 1))

    if not taken:
        return False
    last = max(taken)
    return any(letter in NEGATIONS and i not in taken and not in_word(i) for i, letter in enumerate(clause[:last]))


def find_condensed(quote: str, srt: str, *, speaker: str = "") -> dict[Any, Any] | None:
    """A quotation cut down from what one person said: its letters all stand, in order, in that speaker's lines within
    CONDENSED_WINDOW neighbouring cues; letters and whole lines between them may be left out, none is added or changed
    (the channel's request; a quoted line is still that person's words), and no
    negation is left out of a clause the quotation keeps a later letter of (_drops_negation; letters are taken
    greedily, so the earliest reading is the one checked).  The smallest window wins.  `speaker` limits the search to
    that speaker's lines."""
    needle = canonical(quote)
    if not needle:
        return None
    cues = final_cues(srt)
    for size in range(1, CONDENSED_WINDOW + 1):
        for start in range(len(cues) - size + 1):
            window = cues[start : start + size]
            for who in dict.fromkeys(row["speaker"] for row in window):
                if speaker and who != speaker:
                    continue
                used, at, flipped = _take([r for r in window if r["speaker"] == who], needle)
                if at == len(needle) and not flipped:
                    first = next(r for r in window if r["cue"] == used[0])
                    return {
                        "cues": used,
                        "time": first["time"],
                        "speaker": who,
                        "speaker_status": "VERIFIED_SUBTITLE" if who else "UNKNOWN",
                        "quote": quote,
                        "match": "CONDENSED",
                    }
    return None


HOST_NAME = "李豆沙"
TEAMMATE = "队友"      # skill section 4: an unidentified guest, or one of the guests


def _written_names(srt: str, people: Collection[str]) -> dict[str, str]:
    """How a title writes the people of the subtitles and of the session (name -> speaker label): each name, and a
    guest's short name (the leading Chinese part of 「礼墨Sumi」: skill section 4 writes 礼墨, 安晚 in the body), with the
    host and kmx."""
    out = {HOST_NAME: HOST_NAME, "kmx": "kmx", "KMX": "kmx"}
    for label in dict.fromkeys([*(row["speaker"] for row in final_cues(srt) if row["speaker"]), *people]):
        out.setdefault(label, label)
        short = re.match(r"[\u4e00-\u9fff]{2,}", label)
        if short and short.group() != label:
            out.setdefault(short.group(), label)
    return out


def _speakers_named(body: str, phrase: str, srt: str, unidentified: Collection[str],
                    people: Collection[str]) -> list[str] | None:
    """Who the title says says `phrase`: the speaker labels the names written right before it stand for (「礼墨“…”」,
    「礼墨安晚“…”」, 「李豆沙说：“…”」); None when no name stands there.  Two names or 队友 let the line be an
    unidentified guest's (`unidentified` labels: skill section 4, when the guests cannot be told apart); one name does
    not -- that is a guess at who said it."""
    names = _written_names(srt, people)
    one = "|".join(re.escape(n) for n in sorted([*names, TEAMMATE], key=len, reverse=True))
    found = re.search(rf"((?:{one})(?:[、和与跟]?(?:{one}))*)(?:说|回|喊|来|一句|当场说|表示)*[：:\s]*[“\"「『‘]?"
                      + re.escape(phrase), body)
    if not found:
        return None
    written = re.findall(one, found.group(1))
    labels = [who for who in dict.fromkeys(row["speaker"] for row in final_cues(srt)) if who]
    allowed = [names[n] for n in written if n != TEAMMATE]
    if TEAMMATE in written:
        allowed += [who for who in labels if who != HOST_NAME]
    if len(written) > 1 or TEAMMATE in written:
        allowed += list(unidentified)
    return list(dict.fromkeys(allowed))


def check_title_quotes(title: str, final_srt: str, *, declared_quote: object = None,
                       unidentified: Collection[str] = (), people: Collection[str] = ()) -> dict[Any, Any]:
    # A title needs no quote; every quotation mark it uses must be backed by what that person said (verbatim, or
    # condensed: find_condensed), by the person the names right before it say (_speakers_named; `unidentified`: the
    # speaker labels of guests nobody could tell apart; `people`: the session's names, which a line may be labelled with).
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
        named = _speakers_named(body, phrase, final_srt, unidentified, people)
        # a name the subtitles give no speaker for (队友 on a clip without guest labels) verifies nobody: "\0" matches none
        attributed = (named or ["\0"]) if named is not None else ([speaker] if speaker else [""])
        match = next(filter(None, (find_quote(phrase, final_srt, speaker=who) or find_condensed(phrase, final_srt, speaker=who)
                                   for who in attributed)), None)
        matches.append(match)
        if any(attributed) and match is None and (find_quote(phrase, final_srt) or find_condensed(phrase, final_srt)) is not None:
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
