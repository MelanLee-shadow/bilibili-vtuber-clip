"""SRT parsing / formatting and the small text helpers every step shares.

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, TypedDict, cast

# ----- SRT and text helpers ---------------------------------------------------------------
_TS_RE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


class TimeSpan(TypedDict):
    start_s: float
    end_s: float


class TimedText(TimeSpan):
    """The timed text fields shared by SRT readers and writers."""

    text: str


class SrtCue(TimedText):
    """A parsed SRT cue, after missing cue numbers have been assigned."""

    id: int


class _ParsingCue(TimedText):
    id: int | None


def _parse_ts(text: str) -> float:
    match = _TS_RE.match(text.strip())
    if not match:
        raise ValueError(f"bad SRT timestamp: {text!r}")
    h, m, s, ms = match.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000


def fmt_ts(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    h, rest = divmod(total_ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(text: str) -> list[SrtCue]:
    cues: list[_ParsingCue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        if "-->" in lines[0]:
            timing, body = lines[0], lines[1:]
            cue_id = None
        else:
            cue_id, timing, body = int(lines[0].strip()), lines[1], lines[2:]
        start, end = timing.split("-->")
        cues.append({"id": cue_id, "start_s": _parse_ts(start), "end_s": _parse_ts(end),
                     "text": " ".join(ln.strip() for ln in body)})
    for index, cue in enumerate(cues, 1):
        if cue["id"] is None:
            cue["id"] = index
    # The loop above fills every missing number before this list is returned.
    return cast(list[SrtCue], cues)


def format_srt(cues: Sequence[TimedText]) -> str:
    blocks = [f"{i}\n{fmt_ts(c['start_s'])} --> {fmt_ts(c['end_s'])}\n{c['text']}\n"
              for i, c in enumerate(cues, 1)]
    return "\n".join(blocks)


def window_cues(cues: Sequence[SrtCue], start_s: float, end_s: float) -> list[SrtCue]:
    return [cast(SrtCue, dict(c)) for c in cues
            if c["start_s"] >= start_s - 0.01 and c["end_s"] <= end_s + 0.01]


def _context_lines(cues: Sequence[SrtCue]) -> str:
    return "\n".join(f"{c['id']} {fmt_ts(c['start_s'])} {c['text']}" for c in cues)


class JSONTextError(ValueError):
    """A CPA answer that does not start with one readable JSON value.  A ValueError, so every caller that already
    treats an unreadable answer as a failed read keeps doing so."""


class JSONNotFound(JSONTextError):
    """No JSON value starts anywhere in the text (prose, an empty answer, a refusal)."""


class JSONTruncated(JSONTextError):
    """The first JSON value the text starts is not finished: the answer was cut short (TASK-35)."""


class JSONMalformed(JSONTextError):
    """The first JSON value the text starts closes again but does not parse (a missing comma, a bad escape): the
    answer is broken, not short.  Distinct from JSONTruncated because it is not asked again as a cut answer."""


def _looks_like_json(text: str, start: int) -> bool:
    """Whether the bracket at `start` begins a JSON value rather than a bracket inside prose (`[笑]`, 「[1]」)."""
    rest = text[start + 1:].lstrip()
    return bool(rest) and (rest[0] in '[{"' or rest[0] in "-0123456789"
                           or rest[:4] in ("true", "fals", "null"))


def _value_closes(text: str, start: int) -> bool:
    """True when the bracket opened at `start` closes again inside `text`, so a decode failure there is a malformed
    value rather than a cut-off one.  Strings and escapes are honoured: a bracket inside a string does not count."""
    depth = 0
    in_string = escaped = False
    for ch in text[start:]:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return True
    return False


def _json_from_text(text: str) -> Any:
    """The first JSON value of a CPA answer; surrounding prose and a code fence around it are skipped.

    TASK-35: the first value the answer starts is the answer.  When it cannot be read whole this raises — JSONTruncated
    when it was cut off, JSONMalformed when it closes but is broken — and never falls back to an object inside it.
    The 1323/1722 answers of TASK-32b (the provider said `complete: True`, the stream ended mid-string) used to come
    back as the first one or two rows of a ten-doubt batch, and the doubts left out became UNRESOLVED or stopped the
    clip.  Half an answer is not a smaller answer.
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        start = match.start()
        if not _looks_like_json(text, start):
            continue                      # a bracket inside prose, not the start of a value
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            head = f"{text[start:start + 80]!r}"
            if not _value_closes(text, start):
                raise JSONTruncated(f"the JSON value at {start} is not finished: {head}") from None
            raise JSONMalformed(f"the JSON value at {start} does not parse ({exc.msg}): {head}") from None
        return value
    raise JSONNotFound("no JSON object found in CPA response")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
