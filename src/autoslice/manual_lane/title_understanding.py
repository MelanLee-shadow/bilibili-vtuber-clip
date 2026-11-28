"""Step 6, before the title is written: work out what the clip is about.

The channel owner found the biggest title error was a title not built on the clip's main line (a passing remark taken
as the subject, a reference turned around).  So the title prompt is given a checked reading of the clip first:

- two independent readings of the whole final subtitles (the same prompt, `run` 1 and 2, asked at once), each with
  the main line, the landings (the few moments that could carry the title), who/what the easily confused references
  mean, the side topics, and how the host figures in the main line (`host_role`, one of ROLES);
- one merge that checks both readings against the subtitles, anchored on what the selection wrote when the clip was
  chosen (clip_init.selection_notes: the hook, the detailed reason, the peaks), and settles where they differ.

The landings are a group, not one punchline (标题没落在最关键的那一下，就是流水线的问题):
on 11-23/24 the one punchline step 6 settled left every other good moment under "side topics", which the title prompt
forbade, and the moment the selection had pointed at was never written (0 of 8 to 0 of 48 candidates).  Each moment the
selection pointed at is a landing (`picked`), then the readers' own most unusual lines; side topics are only what has
nothing to do with the main line.  All three prompts (reading, merge, title) see the selection's notes.

A clip whose main line is other people's business, the host only chiming in (the merge says BYSTANDER and at least
one reading said so too), is not the channel's clip: step 6 stops it as NOT_ABOUT_HOST before any title is written
(`not_about_host`).  Selection cannot see who speaks (no speaker labels before step 4), so this is the first place the
lane can tell.  selection is about the host herself; reaction clips (her reactions to someone else's video are the main
line) and collab clips where she is one of the parties are not this case.  The reading rests on step 4's labels: the
note carries her share of the lines, and when no line at all is labelled hers the stop is NO_HOST_LINES (step 4 is to
be looked at), not a verdict on the content.

No audio is read and no recognition is run; the selection's notes only say which event is the main line and which
moments it chose, every fact is the subtitles'.  The result goes into title/candidates.json (`understanding`), a PROCESS line, and the title prompt
(assets/manual_lane/prompts/title_mainline.md).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict, cast

from . import channel, channel_facts, parallel, title_authority
from .clip_init import SelectionNotes, selection_notes
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
LANDINGS_MAX = 4
LEAD, PARTY, BYSTANDER = "主角", "参与", "旁观"     # how the host figures in the main line (the prompts' words)
ROLES = (LEAD, PARTY, BYSTANDER)
NOT_ABOUT_HOST = "NOT_ABOUT_HOST"
NO_HOST_LINES = "NO_HOST_LINES"     # the same stop when step 4 labelled none of the lines hers


class Landing(TypedDict):
    """One moment of the clip that could carry the title."""
    quote: str                    # the words as the subtitles have them ("" for the owner's own statement of the draw)
    who: str                      # who says them, as the speaker label has it ("" without labels)
    cues: list[int]
    why: str                      # what is out of the ordinary in it
    picked: NotRequired[bool]     # the selection pointed at this moment
    owner: NotRequired[bool]      # the channel owner said this is the clip's draw (title_verdicts.retitle)


class ReadingBrief(TypedDict):
    main_line: str
    landings: list[Landing]
    punchline: str          # the landings as one line (landings_line), for the consumers that take one string
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
    readings: list[ReadingBrief]      # each reading's main_line and landings
    merged: Merged
    hook: str                         # the selection reason as written (may be empty)
    selection: SelectionNotes         # what the selection wrote of the clip, as the prompts carried it


def persona_basics(text: str | None = None) -> str:
    """The channel's basic facts as the title reads them (channel_facts.basics; `PERSONA` is this module's path so a
    test can point it elsewhere)."""
    return channel_facts.basics(PERSONA.read_text(encoding="utf-8") if text is None else text)


def _filled(value: Any, key: str) -> bool:
    return isinstance(value, dict) and isinstance(value.get(key), str) and bool(value[key].strip())


def _landing_ok(value: Any) -> bool:
    return (_filled(value, "quote") and isinstance(value.get("who"), str) and isinstance(value.get("why"), str)
            and isinstance(value.get("cues"), list)
            and all(isinstance(c, int) and not isinstance(c, bool) for c in value["cues"]))


def reading_problem(value: Any) -> str:
    """Why a reading is not usable, or "": an object whose main_line is a non-empty string, whose landings are one or
    more objects with a quote (non-empty), who, cues (integers) and why, and whose host_role is one of ROLES.  More than
    LANDINGS_MAX landings is not a shape error: they come in priority order and `_capped` keeps the first ones."""
    if not isinstance(value, dict):
        return "not an object"
    if not _filled(value, "main_line"):
        return "main_line missing"
    landings = value.get("landings")
    if not isinstance(landings, list) or not landings or not all(map(_landing_ok, landings)):
        return "landings must be one or more objects with quote, who, cues (integers) and why"
    return "" if value.get("host_role") in ROLES else f"host_role must be one of {'/'.join(ROLES)}"


def _capped(answer: ReadingBrief | Merged) -> list[str]:
    """Keep the first LANDINGS_MAX landings of a reading or the merge (the prompts list them most important first);
    returns one line per landing left out, for the merge's conflicts and PROCESS.  11-23 1493180: the selection had
    marked six beats, the merge listed all six as the prompt said, and ten answers in a row were rejected as a shape
    error, stopping the clip."""
    dropped = answer["landings"][LANDINGS_MAX:]
    answer["landings"] = answer["landings"][:LANDINGS_MAX]
    return [f"落点超过 {LANDINGS_MAX} 处，按先后没列：{landing_text(x)}" for x in dropped]


def landing_text(landing: Landing) -> str:
    """One landing as the prompts and PROCESS show it."""
    if not landing["quote"]:
        return landing["why"] + ("〔频道主说的看点〕" if landing.get("owner") else "")
    who = f"[{landing['who']}] " if landing["who"] else ""
    cues = f"（第 {'、'.join(map(str, landing['cues']))} 句）" if landing["cues"] else ""
    return (f"{who}“{landing['quote']}”{cues}" + (f"——{landing['why']}" if landing["why"] else "")
            + ("〔选片点出〕" if landing.get("picked") else ""))


def landings_line(landings: Sequence[Landing]) -> str:
    return "；".join(f"{n}. {landing_text(x)}" for n, x in enumerate(landings, 1))


def owner_landing(draw: str) -> list[Landing]:
    """The landings of a clip whose draw the channel owner stated himself: that one, in his words."""
    return [{"quote": "", "who": "", "cues": [], "why": draw, "owner": True}]


def selection_block(notes: SelectionNotes) -> str:
    """What the selection wrote of the clip, as the reading, merge and title prompts carry it.  It says which moments
    were chosen; every fact of a title is still the subtitles'."""
    if not (notes["hook"].strip() or notes["reason"].strip() or notes["peaks"]):
        return "选片时写下的“为什么选这段”：" + NO_HOOK
    lines = ["选片时写下的“为什么选这段”（只用来确定选的是哪几下；人名和原话是转写稿的写法，会有错字和夸张，事实以字幕为准）：",
             f"- 一句话：{notes['hook'].strip() or NO_HOOK}"]
    if notes["reason"].strip():
        lines.append(f"- 看点说明：{notes['reason'].strip()}")
    if notes["peaks"]:
        lines.append("- 选片点出的那几下（最该被看到的时刻）：")
        lines += [f"  {n}. " + (f"[{p['who']}] " if p.get("who") else "") + f"“{p['quote']}”——{p['why']}"
                  for n, p in enumerate(notes["peaks"], 1)]
    else:
        lines.append("- 选片没有单独列出“最该被看到的那几下”：上面两段里点到的每一拍（写着“必须保留”“落点”的）都算选片点出的。")
    return "\n".join(lines)


def _hooks_block() -> str:
    rules = title_authority.hook_rules()
    return ("编辑方法里讲“落点怎么找”的一节（原文；讲文件、截帧和封面的部分这里用不到）：\n" + rules) if rules else ""


def merge_problem(value: Any) -> str:
    """Why the merge is not usable, or "": a reading (main line, landings, host role), plus who_is_who (objects with
    term and means) and side_topics (strings) the title prompt is written from.  `unexplained` may be missing; when present it is a list of objects
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
    notes = selection_notes(ctx.state.data["clip"])
    try:
        selection, hooks = selection_block(notes), _hooks_block()
    except title_authority.AuthorityError as exc:
        return _fail("TITLE_AUTHORITY_MISSING", str(exc), cpa=cpa_calls)
    read_prompt = _render("title_understand.md", persona=persona, tiers=tiers, selection=selection, hooks=hooks,
                          landings_max=str(LANDINGS_MAX), context=lines)
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
        _capped(readings[-1])
    merge_prompt = _render("title_understand_merge.md", persona=persona, tiers=tiers, selection=selection, hooks=hooks,
                           landings_max=str(LANDINGS_MAX),
                           reading_a=json.dumps(readings[0], ensure_ascii=False, indent=1),
                           reading_b=json.dumps(readings[1], ensure_ascii=False, indent=1), context=lines)
    merged = _answer(_ask_cpa(ctx, "title-merge", merge_prompt, purpose=MERGE_PURPOSE,
                              accepts=_contract(merge_problem)), cpa_calls)
    if isinstance(merged, StepOutcome):
        return merged
    done = cast(Merged, merged)
    if dropped := _capped(done):
        done["conflicts"] = [*(done.get("conflicts") or []), *dropped]
    done["punchline"] = landings_line(done["landings"])
    return {"readings": [{"main_line": r["main_line"], "landings": r["landings"],
                          "punchline": landings_line(r["landings"]), "host_role": r["host_role"]} for r in readings],
            "merged": done, "hook": notes["hook"], "selection": notes}


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
    return _render("title_mainline.md", main_line=merged["main_line"],
                   landings="\n".join(f"  {n}. {landing_text(x)}" for n, x in enumerate(merged["landings"], 1)),
                   selection=selection_block(found["selection"]),
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
