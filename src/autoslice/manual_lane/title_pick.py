"""Step 6, after a round's candidates are checked: put them in the channel owner's order of taste.

The model's own `recommended` is often not the candidate the owner accepts.  So each round, once the deterministic checks
are done, one more call (purpose `title-pick`, the judge tier) is shown what he accepted and rejected clip by clip
(title_verdicts.corpus, the clip being written left out) and asked to put the passing candidates in his order, with the
grade he would give each and why (prompts/title_pick.md).  That order, not `recommended`, decides which candidate goes to
the fact judge first and which one is finally kept.

Before the order is used, candidates that end on the editor's own remark (an owner nickname plus words the clip never
said) go to the back (`editor_tail`): the owner rejects that tail however the rest reads.  The filter is deterministic.

The ranking is advice, not a gate on the step: when it cannot be had (the call fails, or the answer never fits the
contract) the round falls back to the model's own order, and says so.  A round with a single passing candidate has
nothing to rank and is not asked.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from .runner import Ctx
from .srt_text import _json_from_text
from .step_io import _ask_cpa, _cpa_ok, _log, _render

if TYPE_CHECKING:
    from .state import CpaEvent

PICK_PURPOSE = "title-pick"
# How the owner is called in the title skill's section 4 table of forms of address (the 称呼表): a title that ends on one
# of these followed by words of the editor's own is the failure the owner rejects.
EDITOR_NICKNAMES = ("熊猫头", "李姐", "小李")
CLAUSE_BREAKS = re.compile("[，？！]")
COVERAGE_MIN = 0.5            # below this share of the last clause's characters found in the subtitles, it is the editor's
BAD_GRADE = "差"              # predicted 差 exactly, or any grade containing 尤其差, is a rejection
WORST_GRADE = "尤其差"


class PickItem(TypedDict):
    """What is recorded on one ranked candidate."""
    rank: int                 # 1 = first of the round's ranked candidates (the filtered ones after the others)
    grade: str                # the grade the owner is predicted to give
    why: str
    editor_tail: bool


class PickCall(TypedDict):
    """What is recorded on a round that asked for a ranking."""
    call_id: str
    fell_back: bool           # True: the ranking could not be had and the round used the model's own order
    reason: str               # why it fell back ("" when it did not)
    first: str                # the title ranked first ("" on a fall back)
    first_grade: str
    filtered: int             # how many candidates were put at the back as editor_tail


@dataclass(frozen=True)
class PickBasis:
    """What the ranking prompt is written from: the clip's subtitles (for the editor-tail filter) and main line, and the
    owner's verdicts on the other clips."""
    subtitles: str
    main_line: str
    punchline: str
    corpus: str
    circle: str = ""          # circle_context.block: background of the references the main line needs ("" when none)


@dataclass(frozen=True)
class RoundPick:
    order: list[int]          # every candidate of the round: ranked passing ones, then the others in the model's order
    items: dict[int, PickItem] = field(default_factory=dict)
    call: PickCall | None = None        # None: no ranking was asked (fewer than two passing candidates)
    top_is_bad: bool = False            # the first-ranked candidate is predicted to be rejected


def _keep(text: str) -> str:
    """Han characters, letters and digits only, lower-cased: what two texts are compared on."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def last_clause(body: str) -> str:
    parts = [p for p in CLAUSE_BREAKS.split(body.strip()) if p.strip()]
    return parts[-1] if parts else body


def editor_tail(body: str, subtitles: str) -> bool:
    """The title body's last clause names the owner by a nickname and is mostly not the clip's words: a character counts as
    the clip's when it lies in a pair of neighbouring characters found in the subtitles; under COVERAGE_MIN of the clause
    covered it is the editor's own remark.  A clause under two characters is never filtered."""
    clause = last_clause(body)
    if not any(n in clause for n in EDITOR_NICKNAMES):
        return False
    chars, said = _keep(clause), _keep(subtitles)
    if len(chars) < 2:
        return False
    pairs = {said[i:i + 2] for i in range(len(said) - 1)}
    covered = sum(1 for i in range(len(chars))
                  if (i >= 1 and chars[i - 1:i + 1] in pairs) or (i + 1 < len(chars) and chars[i:i + 2] in pairs))
    return covered / len(chars) < COVERAGE_MIN


def is_bad_grade(grade: str) -> bool:
    text = grade.strip()
    return WORST_GRADE in text or text == BAD_GRADE


def answer_problem(value: object, n: int) -> str:
    """Why the ranking answer is not usable, or "": an object with `order` (a list; what is missing, repeated or out of
    range in it is mended by `normalized_order`) and `predicted`, one object per candidate with its whole `index`, a
    `grade` and a `why`."""
    if not isinstance(value, dict):
        return "not an object"
    if not isinstance(value.get("order"), list):
        return "order is not a list"
    predicted = value.get("predicted")
    if not isinstance(predicted, list) or len(predicted) != n:
        return f"predicted is not a list of {n}"
    seen: set[int] = set()
    for item in predicted:
        if not isinstance(item, dict):
            return "a predicted item is not an object"
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < n or index in seen:
            return f"bad or repeated index {index!r}"
        seen.add(index)
        if not isinstance(item.get("grade"), str) or not item["grade"].strip():
            return f"grade missing (index {index})"
        if not isinstance(item.get("why"), str):
            return f"why is not a string (index {index})"
    return ""


def normalized_order(order: Sequence[object], n: int) -> list[int]:
    """The order with repeats and out-of-range entries dropped and what is missing put at the end."""
    out: list[int] = []
    for x in order:
        if isinstance(x, int) and not isinstance(x, bool) and 0 <= x < n and x not in out:
            out.append(x)
    return out + [i for i in range(n) if i not in out]


def _contract(n: int) -> Callable[[object], str]:
    def accepts(value: object) -> str:
        found = answer_problem(value, n)
        return f"TITLE_PICK_SHAPE ({found})" if found else ""
    return accepts


def _failure(record: CpaEvent) -> str:
    return str(record.get("reason") or record.get("stderr_tail") or "no answer").strip().splitlines()[0][:200]


def rank_round(ctx: Ctx, call_id: str, basis: PickBasis, *, titles: Sequence[str], bodies: Sequence[str],
               passing: Sequence[bool], model_order: Sequence[int], cpa_calls: list[CpaEvent]) -> RoundPick:
    """Order one round's candidates.  `titles`/`bodies` are each candidate's whole title and its body (after the prefix),
    `passing` whether it passed the deterministic checks, `model_order` the model's own order (its recommendation first).
    Only passing candidates are ranked, numbered in the order the model wrote them; the rest follow in the model's order."""
    ranked_ids = [i for i in range(len(titles)) if passing[i]]
    model_ranked = [i for i in model_order if passing[i]]
    rest = [i for i in model_order if not passing[i]]
    if len(ranked_ids) < 2:
        return RoundPick(order=[*model_ranked, *rest])
    tails = [editor_tail(bodies[i], basis.subtitles) for i in ranked_ids]
    if all(tails):
        tails = [False] * len(ranked_ids)          # nothing left to choose from: none is filtered
    n = len(ranked_ids)
    prompt = _render("title_pick.md", corpus=basis.corpus, main_line=basis.main_line, punchline=basis.punchline,
                     circle_context=f"\n{basis.circle}" if basis.circle else "",
                     candidates="\n".join(f"{k} {titles[i]}" for k, i in enumerate(ranked_ids)))
    record = _ask_cpa(ctx, call_id, prompt, purpose=PICK_PURPOSE, accepts=_contract(n))
    cpa_calls.append(_log(record))
    answer: object = None
    reason = ""
    if not _cpa_ok(record):
        reason = _failure(record)
    else:
        try:
            answer = _json_from_text(record["response_text"])
        except ValueError as exc:
            reason = f"TITLE_PICK_UNPARSEABLE ({exc})"
        else:
            reason = answer_problem(answer, n)
    if reason or not isinstance(answer, dict):
        return RoundPick(order=[*model_ranked, *rest],
                         call=PickCall(call_id=call_id, fell_back=True, reason=reason or "no answer", first="",
                                       first_grade="", filtered=0))
    order = normalized_order(answer["order"], n)
    order = [k for k in order if not tails[k]] + [k for k in order if tails[k]]
    predicted = {p["index"]: p for p in answer["predicted"]}
    items = {ranked_ids[k]: PickItem(rank=place, grade=str(predicted[k]["grade"]).strip(), why=str(predicted[k]["why"]),
                                     editor_tail=tails[k])
             for place, k in enumerate(order, 1)}
    first = ranked_ids[order[0]]
    return RoundPick(order=[*(ranked_ids[k] for k in order), *rest], items=items,
                     call=PickCall(call_id=call_id, fell_back=False, reason="", first=titles[first],
                                   first_grade=items[first]["grade"], filtered=sum(tails)),
                     top_is_bad=is_bad_grade(items[first]["grade"]))


def process_line(call: PickCall, round_no: int) -> str:
    """The PROCESS line of one round that asked for a ranking."""
    if call["fell_back"]:
        return f"按口味排序未做成（{call['reason']}），本轮按模型自己的推荐（第 {round_no + 1} 轮）"
    return (f"按频道主口味排序：第一 {call['first']}（预测 {call['first_grade']}）；"
            f"筛掉自拟评语收尾 {call['filtered']} 条（第 {round_no + 1} 轮）")


def feedback_line(item: PickItem | None) -> str:
    """The line after a candidate in the rewrite feedback: what the owner is predicted to say of it."""
    return f"按频道主的口味预测：{item['grade']} {item['why']}" if item else ""
