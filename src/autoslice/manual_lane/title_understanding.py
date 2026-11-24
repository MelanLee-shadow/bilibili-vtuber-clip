"""Step 6, before the title is written: work out what the clip is about.

The channel owner found the biggest title error was a title not built on the clip's main line (a passing remark taken
as the subject, a reference turned around).  So the title prompt is given a checked reading of the clip first:

- two independent readings of the whole final subtitles (the same prompt, `run` 1 and 2, asked at once), each with
  the main line, the punchline, who/what the easily confused references mean, the side topics, and how the host figures
  in the main line (`host_role`, one of ROLES);
- one merge that checks both readings against the subtitles, anchored on the selection reason written when the clip
  was chosen (`clip.hook`; empty for a compilation or a trimmed successor), and settles where they differ.

A clip whose main line is other people's business, the host only chiming in (the merge says BYSTANDER and at least
one reading said so too), is not the channel's clip: step 6 stops it as NOT_ABOUT_HOST before any title is written
(`not_about_host`).  Selection cannot see who speaks (no speaker labels before step 4), so this is the first place the
lane can tell.  Owner: selection is 「围绕李豆沙」; reaction clips (her reactions to someone else's video are the main
line) and collab clips where she is one of the parties are not this case.  The reading rests on step 4's labels: the
note carries her share of the lines, and when no line at all is labelled hers the stop is NO_HOST_LINES (step 4 is to
be looked at), not a verdict on the content.

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
LEAD, PARTY, BYSTANDER = "主角", "参与", "旁观"     # how the host figures in the main line (the prompts' words)
ROLES = (LEAD, PARTY, BYSTANDER)
NOT_ABOUT_HOST = "NOT_ABOUT_HOST"
NO_HOST_LINES = "NO_HOST_LINES"     # the same stop when step 4 labelled none of the lines hers


class ReadingBrief(TypedDict):
    main_line: str
    punchline: str
    host_role: str          # one of ROLES


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
    host_role_why: NotRequired[str]   # the merge's one sentence behind host_role, with cue numbers


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
    """Why a reading is not usable, or "": an object whose main_line and punchline are non-empty strings and whose
    host_role is one of ROLES."""
    if not isinstance(value, dict):
        return "not an object"
    missing = next((f"{key} missing" for key in ("main_line", "punchline") if not _filled(value, key)), "")
    if missing:
        return missing
    return "" if value.get("host_role") in ROLES else f"host_role must be one of {'/'.join(ROLES)}"


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
    try:
        tiers = channel_facts.core_attractions()
    except (OSError, KeyError) as exc:
        return _fail("TITLE_AUTHORITY_MISSING", f"selection tiers unreadable: {channel_facts.METRIC}: {exc!r}",
                     cpa=cpa_calls)
    hook = str(ctx.state.data["clip"].get("hook") or "")
    read_prompt = _render("title_understand.md", persona=persona, tiers=tiers, context=lines)
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
    merge_prompt = _render("title_understand_merge.md", persona=persona, tiers=tiers, hook=hook.strip() or NO_HOOK,
                           reading_a=json.dumps(readings[0], ensure_ascii=False, indent=1),
                           reading_b=json.dumps(readings[1], ensure_ascii=False, indent=1), context=lines)
    merged = _answer(_ask_cpa(ctx, "title-merge", merge_prompt, purpose=MERGE_PURPOSE,
                              accepts=_contract(merge_problem)), cpa_calls)
    if isinstance(merged, StepOutcome):
        return merged
    return {"readings": [{"main_line": r["main_line"], "punchline": r["punchline"], "host_role": r["host_role"]}
                         for r in readings],
            "merged": cast(Merged, merged), "hook": hook}


def host_lines(speakers: Sequence[str | None]) -> tuple[int, int]:
    """(lines labelled the host's, lines) for the speaker label of each line; lines with no label at all are a clip
    without speakers (a solo clip), all hers."""
    if not any(speakers):
        return len(speakers), len(speakers)
    return sum(s == channel.HOST_LABEL for s in speakers), len(speakers)


def not_about_host(found: Understanding, speakers: Sequence[str | None]) -> tuple[str, str] | None:
    """(stop reason, note) when the clip is not about the host, else None: the merge settled on BYSTANDER (the main
    line is between others, the host only chimes in) and at least one reading said so too; a merge that overrules two
    readings agreeing she is in the main line does not stop the clip.  `speakers` is each subtitle line's speaker label:
    her share goes into the note, and with no line labelled hers the reason is NO_HOST_LINES.  The note is also the
    stop's PROCESS line."""
    merged = found["merged"]
    roles = [r["host_role"] for r in found["readings"]]
    if merged["host_role"] != BYSTANDER or BYSTANDER not in roles:
        return None
    mine, total = host_lines(speakers)
    why = str(merged.get("host_role_why") or "").strip().rstrip("。；")
    if mine == 0:
        return NO_HOST_LINES, (f"第 4 步没有把任何一句标成{channel.HOST}（0/{total} 句），读片按标签判她「{BYSTANDER}」；"
                               f"在她自己的直播里一句都不是她，先查第 4 步的说话人，不按内容下结论。第 6 步不写标题，这条不发")
    return NOT_ABOUT_HOST, (f"本片主线不围绕{channel.HOST}：她在主线里是「{BYSTANDER}」（两份阅读：{'、'.join(roles)}；"
                            f"合并：{BYSTANDER}；她的句子 {mine}/{total}）" + (f"——{why}" if why else "")
                            + f"；本片主线：{merged['main_line'].strip().rstrip('。')}。第 6 步不写标题，这条不发")


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
