"""Step 6 cover: scoring on the measured face, ranking all routes together, and the thumbnail-checked choice.

All routes' candidates are compared at once.  The 25 % face floor reads the face box MEASURED on the picture the host comes
from (cover_candidates.py: one vision reading of the source picture, carried through the layout's own geometry), never a
number written into a spec and never the fixed default box.  A candidate whose face could not be measured is degraded, not
vetoed: it keeps the judge's own reading of the finished picture (a vision reading, marked `judge`) for the score and
ranks behind every candidate whose face was measured.

Ranking (best first): not repeating this session's look, total score, the realism axis C, then the face height up to
the target; a candidate without a measured face is left out while a measured one is eligible.  The first candidate whose 180 px thumbnail passes is chosen; when none passes, the best by that
ranking is chosen and the thumbnail verdicts are recorded (non-blocking)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, cast

from .cover_thumb import ThumbVerdict
from .step6_cover_types import CoverCandidate, CoverVerdict

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

FRAME_W, FRAME_H = 1920, 1080
# the measured facts the remote tool leaves on a row, carried into the scored row and choose.json unchanged
ROW_FACTS = ("punch_ink_h", "punch_floor_px", "punch_below_floor", "text_plan", "crop_plan", "render_s", "phases_s", "background")


def valid_box(box: object) -> bool:
    return (isinstance(box, list) and len(box) == 4 and all(isinstance(v, int) and not isinstance(v, bool) for v in box)
            and 0 <= box[0] < box[2] <= FRAME_W and 0 <= box[1] < box[3] <= FRAME_H)


def face_view(row: Mapping[str, object]) -> dict[str, object]:
    """The face a row is scored by: {face_status, face_source, face_px, face_frac, verdict}.  `verdict` is the row's
    judge verdict with its `face` replaced by the measured box (the judge's own reading kept as `face_judge`)."""
    verdict = row.get("verdict")
    verdict = dict(cast("Mapping[str, JsonValue]", verdict)) if isinstance(verdict, Mapping) else None
    measured, judged = row.get("face_measured"), (verdict or {}).get("face")
    if valid_box(measured):
        status, source, face = "MEASURED", "source_picture", cast("list[int]", measured)
    else:
        status, source, face = "UNMEASURED", ("judge" if valid_box(judged) else None), (judged if valid_box(judged) else None)
    if verdict is not None and status == "MEASURED":
        verdict["face_judge"] = judged
        verdict["face"] = list(cast("list[int]", face))
    px = cast("list[int]", face)[3] - cast("list[int]", face)[1] if face else None
    return {"face_status": status, "face_source": source, "face_px": px,
            "face_frac": round(px / FRAME_H, 3) if px else None, "verdict": cast("CoverVerdict | None", verdict)}


def rank(rows: Sequence[CoverCandidate], *, repeats: Callable[[CoverCandidate], bool], target_px: int
         ) -> list[CoverCandidate]:
    """The eligible candidates of every route, best first (see the module doc)."""
    def key(row: CoverCandidate) -> tuple[bool, bool, int, int, int]:
        score = row["score"]
        return (not repeats(row), row.get("face_status") == "MEASURED", score["total"], score["C"],
                min(cast("int", row.get("face_px") or 0), target_px))
    eligible = [r for r in rows if r["score"]["status"] == "ELIGIBLE"]
    # a candidate whose host face could not be measured (no face in the picture to lay out or to judge) is not chosen
    # while any candidate with a measured face is eligible
    pool = [r for r in eligible if r.get("face_status") == "MEASURED"] or eligible
    return sorted(pool, key=key, reverse=True)


def choose_checked(ranked: Sequence[CoverCandidate], check: Callable[[str], ThumbVerdict]
                   ) -> tuple[str, str, dict[str, ThumbVerdict]]:
    """(chosen id, why, thumbnail verdicts tried).  The first candidate whose thumbnail is not a FAIL; when every one
    fails, the best-ranked, with the verdicts left in the record."""
    tried: dict[str, ThumbVerdict] = {}
    for position, row in enumerate(ranked):
        tried[row["id"]] = check(row["id"])
        if tried[row["id"]]["status"] != "FAIL":
            lead = (f"排名第 {position + 1} 的 {row['id']}" if position else f"总分最高的 {row['id']}")
            skipped = (f"；前 {position} 名缩略图未过（{'、'.join(list(tried)[:-1])}）" if position else "")
            return row["id"], f"{lead}（{row['score']['total']} 分）缩略图{tried[row['id']]['status']}{skipped}", tried
    best = ranked[0]
    return best["id"], (f"全部 {len(ranked)} 个合格候选的缩略图都没过（非阻塞），取排名最高的 {best['id']}"
                        f"（{best['score']['total']} 分）"), tried


def candidate_record(row: CoverCandidate) -> dict[str, object]:
    """What choose.json keeps per candidate: route, the measured face and its share of the height, the punchline's
    size, every score term and veto, the thumbnail verdict, and why a route made nothing."""
    record: dict[str, object] = {"id": row["id"], "route": row["route"], "status": row["status"],
                                 "face_status": row.get("face_status"), "face_source": row.get("face_source"),
                                 "face_measured": row.get("face_measured"), "face_px": row.get("face_px"),
                                 "face_frac": row.get("face_frac"), "punch_px": row.get("punch_font_size"),
                                 "score": row["score"], "thumb": row.get("thumb"), "repeat": row.get("repeat"),
                                 "failure": row.get("failure")}
    record.update({k: row[k] for k in ROW_FACTS if k in row})      # type: ignore[literal-required]
    return record


def background_process_lines(rows: Sequence[CoverCandidate]) -> list[str]:
    """The PROCESS line for the pastel candidates' backgrounds (cover_background.py's record on each row): which group
    each got and the numbers it was judged by; no line when no candidate used a pastel background."""
    parts = []
    for row in rows:
        bg = cast("Mapping[str, JsonValue] | None", row.get("background"))
        if bg:
            chosen = cast("Mapping[str, JsonValue]", bg["chosen"])
            parts.append(f"{row['id']} {bg['id']}（{bg['hue']}·{bg['tone']}；字对比 {chosen['text_distance']}、描边 "
                         f"{chosen['stroke_distance']}，人物融底 {cast(float, chosen['layer_fused_frac']):.0%}；{bg['tier']}）")
    if not parts:
        return []
    return ["封面底色（lane_rules cover.backgrounds：人物不融底、字读得清、近期与同场用过的靠后、各路线错开色系，余下按内容哈希取）："
            + "；".join(parts)]
