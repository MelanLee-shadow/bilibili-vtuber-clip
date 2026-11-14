"""The review's objections to what the channel owner decided (review_owner.Dissent), kept in the clip's state
(state.data["dissent"]).  Each one is recorded in full and registered with a default disposition: the owner's writing is
kept, on the grounds of the owner-final rule (what the owner decided himself goes before every gate; a finding is
recorded and the run goes on), so an objection never stops step 8, step 9 or the seal.  A person may still replace the
default with a disposition of their own (`resolve`, dissent_resolve): keep the owner's writing under their name, or take
the review's (an owner-authorised edit).  The review record (state.review) is rebuilt whenever step 9 runs again, so
nothing here lives in it.

An objection is told apart by (kind, cue, text, proposed): the same answer applied again adds nothing, and a disposition
stays with its objection.  REVIEW.md carries every objection in full with its disposition (`write_section`), PROCESS.md
and the `run` output the same (`process_lines`).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

from .review_owner import KINDS, SECTION, Dissent, tail

if TYPE_CHECKING:
    from .state import ClipState

HOW = {"keep": "保留频道主写法", "apply": "改用复审写法"}
DEFAULT_BY = "频道主定稿规则"
DEFAULT_REASON = "频道主明确决定的内容优先于一切门，复审的异议只记录、不拦打包与封存"


class Resolution(TypedDict):
    how: str       # keep | apply (HOW)
    by: str        # who decided
    reason: str    # on what grounds
    at: str
    default: NotRequired[bool]     # True: set when the objection was registered (DEFAULT_BY), not by a person


class DissentItem(Dissent):
    n: int                          # the number `resolve-dissent --item` takes; never reused
    resolution: Resolution


def items(state: ClipState) -> list[DissentItem]:
    return state.data.get("dissent", [])


def add(state: ClipState, found: Sequence[Dissent] | None) -> None:
    """Register the objections not yet there, each with the default disposition (the caller saves the state)."""
    from .state import utc_now

    with state.lock:
        have = state.data.setdefault("dissent", [])
        for d in found or ():
            if not any((h["kind"], h["cue"], h["text"], h["proposed"]) == (d["kind"], d["cue"], d["text"], d["proposed"])
                       for h in have):
                have.append({"kind": d["kind"], "cue": d["cue"], "text": d["text"], "proposed": d["proposed"],
                             "value": d["value"], "reason": d["reason"], "n": len(have) + 1,
                             "resolution": {"how": "keep", "by": DEFAULT_BY, "reason": DEFAULT_REASON, "at": utc_now(),
                                            "default": True}})


def line(item: DissentItem) -> str:
    """One objection in full, with its disposition."""
    done = item["resolution"]
    return (f"[{item['n']}] {KINDS[item['kind']]}{tail(item)}；"
            f"处置：{HOW[done['how']]}（{done['by']}：{done['reason']}，{done['at']}）")


def lines(state: ClipState) -> list[str]:
    return [line(i) for i in items(state)]


def defaulted(state: ClipState) -> list[DissentItem]:
    """The objections still on the default disposition."""
    return [i for i in items(state) if i["resolution"].get("default")]


def process_lines(state: ClipState) -> list[str]:
    """What PROCESS.md and the `run` output say: how many objections stand on the owner-final rule, then every objection in
    full (none: nothing)."""
    kept = defaulted(state)
    return ([f"有 {len(kept)} 条异议已按{DEFAULT_BY}保留"] if kept else []) + lines(state)


def write_section(state: ClipState, path: Path) -> None:
    """REVIEW.md's section for the objections (SECTION, always the last one): every objection in full with its
    disposition, or 无 for a clip where something is the owner's and the review had none.  Written again each time."""
    found, review = items(state), state.data.get("review")
    if not found and (review is None or review.get("dissent") is None):
        return
    body = [f"- {text}" for text in lines(state)] or ["无"]
    head = path.read_text(encoding="utf-8").split("\n" + SECTION)[0].rstrip()
    path.write_text("\n".join([head, "", SECTION, *body]) + "\n", encoding="utf-8")


def resolve(state: ClipState, n: int, how: str, by: str, reason: str) -> DissentItem:
    """Record the disposition of objection `n` (the caller saves the state)."""
    from .state import StateError, utc_now

    item = next((i for i in items(state) if i["n"] == n), None)
    if item is None:
        raise StateError(f"没有第 {n} 条异议（共 {len(items(state))} 条）")
    if not item["resolution"].get("default"):
        raise StateError(f"第 {n} 条异议已有处置人的处置：{line(item)}")
    if how not in HOW or not by.strip() or not reason.strip():
        raise StateError("处置要写明做法（keep/apply）、处置人（--by）和依据（--reason）")
    item["resolution"] = {"how": how, "by": by.strip(), "reason": reason.strip(), "at": utc_now()}
    return item
