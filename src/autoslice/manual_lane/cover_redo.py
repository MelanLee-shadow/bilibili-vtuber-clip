"""Step 6 cover: when no candidate is fit, make the routes again before the look-and-choose fallback (70-cover).

A candidate vetoed for its MATERIAL (the judge's identity, source-faithful or no-fabrication axis, no host face measured in
the picture, a face cut off) is not a candidate the fallback may wave through: the picture itself is wrong, and another frame
or another generation is what can make it right.  So when no candidate is eligible, the routes that failed on their material
are made again, within the retry budget the title step already has for a cover text that does not fit (`RetryBudget`, one
budget, `step6_cover.COVER_RETRIES`):

- a route that found no face is made again on another of the frames the frame step already looked at;
- a drawn route (sticker, scene) is generated again with the reviewer's own words about the last picture in its prompt;
- every route is made in a work directory of its own, so a redo never reuses the picture it is redoing.

Only after the budget is spent does the fallback choose, and only among candidates not vetoed on their material; when every
candidate is, the one with the fewest vetoes is taken and the record carries a loud, non-blocking flag for the finished-media
review (`material_flag`).  Each round's candidates and the reasons for the next round are kept (`RedoLog.rounds`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .cover_pick import candidate_record

if TYPE_CHECKING:
    from .step6_cover_types import CoverCandidate

# the judge's axes whose "no" says the picture is not the person / event of the source frame
JUDGE_AXES = ("identity", "source_faithful", "no_fabrication")
# the routes' own recorded failures that say no usable host face came out of the picture (cover_candidates.py)
NO_FACE = frozenset({"HOST_FACE_NOT_FOUND", "HOST_FACE_NOT_IN_FRAME", "HOST_FACE_NOT_ON_HER_FIGURE",
                     "STICKER_HOST_FACE_UNAVAILABLE", "PEOPLE_FACE_OUTSIDE_MASK", "PEOPLE_LAYERS_UNAVAILABLE"})
MATERIAL = frozenset({"HOST_MISSING", *(f"CPA_VETO:{axis}" for axis in JUDGE_AXES)})
DRAWN = ("ai_sticker", "ai_scene")             # the routes whose prompt can carry the reviewer's words
NOT_REDONE = ("emote",)                        # an official picture: another frame or another draw changes nothing
FEEDBACK = ("A previous attempt at this picture was rejected by the reviewer for the reasons quoted below, verbatim. "
            "Draw it again so that none of them applies, staying faithful to the reference image: ")
REASON_MAX_CHARS = 600


@dataclass
class RetryBudget:
    """The one retry budget of the cover step: a shortened cover text and a redo of the routes both spend it."""

    limit: int
    used: int = 0

    def left(self) -> int:
        return self.limit - self.used

    def spend(self) -> int:
        self.used += 1
        return self.used


def vetoes_of(row: Mapping[str, object]) -> list[str]:
    return list(cast("Mapping[str, list[str]]", row["score"]).get("vetoes") or [])


def is_material(veto: str) -> bool:
    return veto in MATERIAL or any(part in NO_FACE for part in veto.split(":"))


def material_vetoes(row: Mapping[str, object]) -> list[str]:
    return [v for v in vetoes_of(row) if is_material(v)]


def faceless(row: Mapping[str, object]) -> bool:
    return any(part in NO_FACE for v in vetoes_of(row) for part in v.split(":"))


def rejection_text(row: Mapping[str, object], *, codes: bool = True) -> str:
    """The reviewer's own words about a candidate's material vetoes (axis: reason); where it had none, the veto code
    (`codes`) or nothing."""
    verdict = cast("Mapping[str, object]", row.get("verdict") or {})
    parts = []
    for veto in material_vetoes(row):
        axis = veto.removeprefix("CPA_VETO:") if veto.startswith("CPA_VETO:") else ""
        detail = verdict.get(axis)
        reason = str(detail.get("reason") or "").strip() if isinstance(detail, Mapping) else ""
        if reason or codes:
            parts.append((f"{axis}: {reason}" if reason else veto)[:REASON_MAX_CHARS])
    return " | ".join(parts)


def retry_brief(brief: Mapping[str, object], rejected: str) -> dict[str, object]:
    """The brief of a drawn route's redo: the last picture's rejection stands in the action the prompt is built from."""
    return dict(brief, action=f"{brief.get('action') or ''}. {FEEDBACK}{rejected}")


def alternate_frame(times: Sequence[float], used: Sequence[float], checked: Sequence[Mapping[str, object]]) -> float | None:
    """The first of the frames the frame step looked at (nearest the cover moment first) not used yet and not already judged
    to hold an unrelated page, or None."""
    bad = [float(cast(float, c["t"])) for c in checked if c.get("verdict") in ("NOT_CLEAN", "ERROR") and c.get("t") is not None]
    return next((t for t in times if not any(abs(t - u) < 0.01 for u in [*used, *bad])), None)


def redo_routes(current: Sequence[CoverCandidate], everything: Sequence[CoverCandidate], routes: Sequence[str]
                ) -> dict[str, CoverCandidate]:
    """route -> its row, for the routes to make again: nothing at all is eligible (anywhere) and the route's latest row is
    vetoed on its material.  `routes` is the stage's order."""
    if any(r["score"]["status"] == "ELIGIBLE" for r in everything):
        return {}
    latest = {r["route"]: r for r in current}
    return {route: latest[route] for route in routes
            if route in latest and route not in NOT_REDONE and material_vetoes(latest[route])}


def fallback_pool(rows: Sequence[CoverCandidate]) -> tuple[list[CoverCandidate], list[CoverCandidate]]:
    """(rendered candidates not vetoed on their material, every rendered candidate)."""
    rendered = [r for r in rows if r["status"] == "RENDERED"]
    return [r for r in rendered if not material_vetoes(r)], rendered


def fewest_vetoes(rows: Sequence[CoverCandidate]) -> CoverCandidate:
    """The candidate with the fewest vetoes; the later one (a redo with the reviewer's words) on a tie."""
    return min(reversed(rows), key=lambda r: len(vetoes_of(r)))


def material_flag(row: CoverCandidate) -> dict[str, object]:
    """The loud, non-blocking record of a cover chosen although its material was vetoed."""
    return {"id": row["id"], "route": row["route"], "axes": material_vetoes(row), "judge_words": rejection_text(row),
            "all_vetoes": vetoes_of(row)}


def flag_line(flag: Mapping[str, object]) -> str:
    return (f"【封面素材被否决·非阻塞·须成品审查与上线前核对】所有候选都被素材类否决，取否决项最少的 {flag['id']}："
            f"{'、'.join(cast('list[str]', flag['axes']))}；判官原话：{flag['judge_words']}")


@dataclass
class RedoLog:
    """Every row of every round (a row made again is renamed `<id>-r<round it came from>` with its pictures) and the rounds'
    records."""

    rows: list[CoverCandidate] = field(default_factory=list)
    raw: list[CoverCandidate] = field(default_factory=list)
    rounds: list[dict[str, object]] = field(default_factory=list)
    frames: list[float] = field(default_factory=list)
    superseded: set[str] = field(default_factory=set)
    made: dict[str, tuple[int, float]] = field(default_factory=dict)      # row id -> (round, frame time it was made on)

    def current(self) -> list[CoverCandidate]:
        return [r for r in self.rows if r["id"] not in self.superseded]

    def add_round(self, number: int, frame_t: float, routes: Sequence[str], rows: Sequence[CoverCandidate],
                  raw: Sequence[CoverCandidate], **why: object) -> None:
        self.rows.extend(rows)
        self.raw.extend(raw)
        self.made.update({r["id"]: (number, frame_t) for r in rows})
        if frame_t not in self.frames:
            self.frames.append(frame_t)
        self.rounds.append({"round": number, "frame_t": frame_t, "routes": list(routes), **why,
                            "candidates": [candidate_record(r) for r in rows]})

    def supersede(self, cand_dir: Path, routes: Sequence[str]) -> list[str]:
        """Rename the live rows of `routes` that a new round replaces, and their pictures in `cand_dir`, to
        `<id>-r<round>`; returns the new ids."""
        renamed = [(r["id"], f"{r['id']}-r{self.made[r['id']][0]}") for r in self.rows
                   if r["route"] in routes and r["id"] not in self.superseded]
        for old, new in renamed:
            for path in cand_dir.glob(f"{old}.*"):
                path.rename(cand_dir / (new + path.name[len(old):]))
            self.rows = [cast("CoverCandidate", dict(r, id=new, final_path=f"{new}.jpg")) if r["id"] == old else r for r in self.rows]
            self.raw = [cast("CoverCandidate", dict(r, id=new)) if r["id"] == old else r for r in self.raw]
            self.made[new] = self.made[old]
            self.superseded.add(new)
        return [new for _old, new in renamed]
