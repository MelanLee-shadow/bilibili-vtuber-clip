"""Step 6, before the title is written: work out what the clip is about.

The channel owner found the biggest title error was a title not built on the clip's main line (a passing remark taken
as the subject, a reference turned around).  So the title prompt is given a checked reading of the clip first:

- two independent readings of the whole final subtitles (the same prompt, `run` 1 and 2, asked at once), each with
  the main line, the punchline, who/what the easily confused references mean, and the side topics;
- one merge that checks both readings against the subtitles, anchored on the selection reason written when the clip
  was chosen (`clip.hook`; empty for a compilation or a trimmed successor), and settles where they differ.

No audio is read and no recognition is run; the selection reason only picks which event is the main line, every fact
is the subtitles'.  The result goes into title/candidates.json (`understanding`), a PROCESS line, and the title prompt
(assets/manual_lane/prompts/title_mainline.md).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict, cast

from . import channel, channel_facts, parallel
from .runner import Ctx, StepOutcome
from .srt_text import _json_from_text
from .step_io import _ask_cpa, _cpa_ok, _fail, _log, _render

if TYPE_CHECKING:
    from .state import CpaEvent

PERSONA = channel.asset("persona.md")
COVER_SECTION = channel_facts.COVER_SECTION
READ_RUNS = (1, 2)
READ_PURPOSE = "title-read"
MERGE_PURPOSE = "title-merge"
SHAPE_REASON = "TITLE_UNDERSTANDING_SHAPE"
NO_HOOK = "（无）"


class ReadingBrief(TypedDict):
    main_line: str
    punchline: str


class Referent(TypedDict):
    term: str
    means: str


class Unexplained(TypedDict):
    """A circle reference the main line or the punchline needs and neither the subtitles nor persona.md explain."""
    term: str
    cues: list[int]
    why: str


class Merged(ReadingBrief):
    """The merge's whole answer; the keys the title prompt is built from are required (merge_problem)."""
    who_is_who: list[Referent]
    side_topics: list[str]
    agree: NotRequired[object]        # whether the two readings agree (true/false)
    hook_holds: NotRequired[object]   # whether the selection reason holds in the subtitles (true/false/null)
    conflicts: NotRequired[list[str]]
    unexplained: NotRequired[list[Unexplained]]   # missing = none (answers cached before the field existed)


class Understanding(TypedDict):
    readings: list[ReadingBrief]      # each reading's main_line and punchline
    merged: Merged
    hook: str                         # the selection reason as written (may be empty)


def persona_basics(text: str | None = None) -> str:
    """The channel's basic facts as the title reads them (channel_facts.basics; `PERSONA` is this module's path so a
    test can point it elsewhere)."""
    return channel_facts.basics(PERSONA.read_text(encoding="utf-8") if text is None else text)


def _filled(value: Any, key: str) -> bool:
    return isinstance(value, dict) and isinstance(value.get(key), str) and bool(value[key].strip())


def reading_problem(value: Any) -> str:
    """Why a reading is not usable, or "": an object whose main_line and punchline are non-empty strings."""
    if not isinstance(value, dict):
        return "not an object"
    return next((f"{key} missing" for key in ("main_line", "punchline") if not _filled(value, key)), "")


def merge_problem(value: Any) -> str:
    """Why the merge is not usable, or "": a reading, plus who_is_who (objects with term and means) and side_topics
    (strings) the title prompt is written from.  `unexplained` may be missing; when present it is a list of objects
    with a term (non-empty string), cues (integers) and why (string)."""
    problem = reading_problem(value)
    if problem:
        return problem
    who = value.get("who_is_who")
    if (not isinstance(who, list) or not all(isinstance(x, dict) and isinstance(x.get("term"), str)
                                             and isinstance(x.get("means"), str) for x in who)):
        return "who_is_who must be a list of objects with term and means"
    side = value.get("side_topics")
    if not isinstance(side, list) or not all(isinstance(x, str) for x in side):
        return "side_topics must be a list of strings"
    unexplained = value.get("unexplained", [])
    if (not isinstance(unexplained, list) or not all(
            isinstance(x, dict) and _filled(x, "term") and isinstance(x.get("why"), str)
            and isinstance(x.get("cues"), list)
            and all(isinstance(c, int) and not isinstance(c, bool) for c in x["cues"]) for x in unexplained)):
        return "unexplained must be a list of objects with term, cues (integers) and why"
    return ""


def _contract(problem: Callable[[Any], str]) -> Callable[[Any], str]:
    def accepts(value: Any) -> str:
        found = problem(value)
        return f"{SHAPE_REASON} ({found})" if found else ""
    return accepts


def _answer(record: CpaEvent, cpa_calls: list[CpaEvent]) -> object:
    cpa_calls.append(_log(record))
    if not _cpa_ok(record):
        # an answer that stayed outside the contract after the tries is unreadable; any other failure is a failed call
        reason = "CPA_RESPONSE_UNPARSEABLE" if record.get("reason") == SHAPE_REASON else "CPA_CALL_FAILED"
        return _fail(reason, record.get("stderr_tail", ""), cpa=cpa_calls)
    try:
        return cast(object, _json_from_text(record["response_text"]))   # the contract (accepts) made it an object
    except ValueError:
        return _fail("CPA_RESPONSE_UNPARSEABLE", record["file"], cpa=[*cpa_calls[:-1], record])


def understand(ctx: Ctx, lines: str, cpa_calls: list[CpaEvent]) -> Understanding | StepOutcome:
    """Two readings of the clip and their merge.  `lines` is the numbered subtitles with speakers the title prompt
    carries.  Returns the result, or the failed outcome (CPA_CALL_FAILED, CPA_RESPONSE_UNPARSEABLE,
    TITLE_AUTHORITY_MISSING when persona.md cannot be read)."""
    try:
        persona = persona_basics()
    except OSError as exc:
        return _fail("TITLE_AUTHORITY_MISSING", f"persona.md unreadable: {PERSONA}: {exc}", cpa=cpa_calls)
    hook = str(ctx.state.data["clip"].get("hook") or "")
    read_prompt = _render("title_understand.md", persona=persona, context=lines)
    records = parallel.run(
        READ_RUNS, lambda run: _ask_cpa(ctx, f"title-read-{run}", read_prompt, run=run, purpose=READ_PURPOSE,
                                        accepts=_contract(reading_problem)),
        provider="cpa")
    readings: list[ReadingBrief] = []
    for record in records:
        reading = _answer(record, cpa_calls)
        if isinstance(reading, StepOutcome):
            return reading
        readings.append(cast(ReadingBrief, reading))
    merge_prompt = _render("title_understand_merge.md", persona=persona, hook=hook.strip() or NO_HOOK,
                           reading_a=json.dumps(readings[0], ensure_ascii=False, indent=1),
                           reading_b=json.dumps(readings[1], ensure_ascii=False, indent=1), context=lines)
    merged = _answer(_ask_cpa(ctx, "title-merge", merge_prompt, purpose=MERGE_PURPOSE,
                              accepts=_contract(merge_problem)), cpa_calls)
    if isinstance(merged, StepOutcome):
        return merged
    return {"readings": [{"main_line": r["main_line"], "punchline": r["punchline"]} for r in readings],
            "merged": cast(Merged, merged), "hook": hook}


def _joined(items: Sequence[str]) -> str:
    return "；".join(items) or "无"


def mainline_block(found: Understanding, canonical_names: str) -> str:
    """title_mainline.md filled: the block that ends the method text of every title round."""
    merged = found["merged"]
    return _render("title_mainline.md", main_line=merged["main_line"], punchline=merged["punchline"],
                   who_is_who=_joined([f"{x['term']}＝{x['means']}" for x in merged["who_is_who"]]),
                   side_topics=_joined(merged["side_topics"]),
                   canonical_names=canonical_names).rstrip("\n")


def _verdict(value: Any, yes: str, no: str) -> str:
    return yes if value is True else no if value is False else "未判定"


def process_line(found: Understanding) -> str:
    merged = found["merged"]
    hook = _verdict(merged.get("hook_holds"), "成立", "不成立") if found["hook"].strip() else "无"
    return (f"本片主线：{merged['main_line'].strip()}"
            f"（两份阅读{_verdict(merged.get('agree'), '一致', '不一致')}；选片理由{hook}）")
