"""Lines the jianying segmentation adds to or cuts out of BCUT's (TASK-28; COACH-ADVICE e05 §3, §6; lane_rules orphans,
split).

- Orphans (§3): a jianying segment that overlaps no BCUT cue (evidence_attach: an orphan, attached whole to the nearest
  cue) becomes its own line, with jianying's text and time, when it has at least `min_content_chars` content characters
  and does not repeat a cue within `neighbour_s` seconds (`repeat_coverage` of its words in that cue).  Otherwise it is
  not subtitled.  Either way it is detached from the nearest cue, so CPA is no longer asked to merge it there.
- Splits (§6): a BCUT cue whose words jianying wrote as two or more segments is cut at jianying's boundaries when every
  piece keeps at least `min_content_chars` content characters.  The plan is the jianying pieces with their times; the
  cut is made on the cue's final text (after CPA) by the same aligner as the evidence slices (evidence_attach._cut_points),
  so a decided text is never replaced, only divided.

plan() is called at detection; apply_splits() and orphan_cues() by the subtitle writer (step3_adjudicate._final_cues).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict, TypeVar, cast

from . import evidence_attach
from .content_compare import content_chars, overlap_share
from .lane_rules import rules
from .srt_text import SrtCue, TimedText

if TYPE_CHECKING:
    from .lane_rules import OrphanRules, SplitRules

ORPHAN_ID_BASE = 100000          # orphan lines get ids above every cue id (v8 chunks, PROCESS)

_Cue = TypeVar("_Cue", bound=TimedText)


class DroppedLine(TimedText):
    why: str


@dataclass
class LinePlan:
    orphan_lines: list[SrtCue] = field(default_factory=list)
    orphan_dropped: list[DroppedLine] = field(default_factory=list)
    splits: dict[int, list[TimedText]] = field(default_factory=dict)

    def process_lines(self) -> list[str]:
        out = [f"剪映孤立段（与任何 BCUT 句都不重叠，e05 §3）：单独成句 {len(self.orphan_lines)} 段"
               + ("（" + "；".join(f"{o['start_s']:.2f}s「{o['text']}」" for o in self.orphan_lines) + "）"
                  if self.orphan_lines else "")
               + f"，不出字幕 {len(self.orphan_dropped)} 段"
               + ("（" + "；".join(f"{o['start_s']:.2f}s「{o['text']}」{o['why']}" for o in self.orphan_dropped) + "）"
                  if self.orphan_dropped else "")]
        out.append(f"剪映分段切句计划（e05 §6）：{len(self.splits)} 句"
                   + (f"：cue {sorted(self.splits)}" if self.splits else ""))
        return out


class SplitProposal(TypedDict):
    cue_id: int
    source_text: str
    start_s: float
    end_s: float
    parts: list[TimedText]


def _gap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, max(a0, b0) - min(a1, b1))


def plan(cues: Sequence[TimedText], att: evidence_attach.AsrAttachment,
         cfg_orphans: OrphanRules | None = None, cfg_split: SplitRules | None = None) -> LinePlan:
    """The orphan lines and the split plan of one clip; detaches every orphan from the cue it was attached to."""
    co = cast("OrphanRules", dict(rules("orphans") if cfg_orphans is None else cfg_orphans))
    cs = cast("SplitRules", dict(rules("split") if cfg_split is None else cfg_split))
    out = LinePlan()
    if co.get("enabled"):
        min_chars, near, cover = int(co["min_content_chars"]), float(co["neighbour_s"]), float(co["repeat_coverage"])
        for k, o in enumerate(att.orphans):
            seg = o["segment"]
            s0, s1, text = float(seg["start_s"]), float(seg["end_s"]), str(seg["text"]).strip()
            close = [c for c in cues if _gap(s0, s1, float(c["start_s"]), float(c["end_s"])) <= near]
            if content_chars(text) < min_chars:
                out.orphan_dropped.append({"start_s": s0, "end_s": s1, "text": text, "why": f"实字不足 {min_chars} 个"})
            elif any(overlap_share(text, str(c["text"])) >= cover for c in close):
                out.orphan_dropped.append({"start_s": s0, "end_s": s1, "text": text,
                                           "why": f"与 {near:g} 秒内的句子重复"})
            else:
                out.orphan_lines.append({"id": ORPHAN_ID_BASE + k, "start_s": s0, "end_s": s1, "text": text})
            att.detach(o)
    if cs.get("enabled"):
        min_chars = int(cs["min_content_chars"])
        for cid, segs in att.by_cue.items():
            pieces = sorted((s for s in segs if s["text"].strip()), key=lambda s: s["start_s"])
            if len({s["seg"] for s in pieces}) >= 2 and all(content_chars(s["text"]) >= min_chars for s in pieces):
                out.splits[cid] = [{"start_s": float(s["start_s"]), "end_s": float(s["end_s"]), "text": s["text"]}
                                   for s in pieces]
    return out


def apply_split(cue: _Cue, text: str, spec: Sequence[TimedText],
                min_chars: int | None = None) -> list[_Cue] | None:
    """The cue's final text cut at the jianying boundaries of `spec`: [dict(cue, start_s, end_s, text)] per piece, or
    None when a piece would keep fewer than `min_chars` content characters (the cue stays whole)."""
    min_chars = int(rules("split")["min_content_chars"]) if min_chars is None else min_chars
    a, b = float(cue["start_s"]), float(cue["end_s"])
    times = [(max(a, float(p["start_s"])), min(b, float(p["end_s"]))) for p in spec]
    cuts = evidence_attach._cut_points(text, [str(p["text"]) for p in spec], times, a, b)
    edges = [0, *cuts, len(text)]
    pieces = [evidence_attach._strip_edges(text[edges[j]:edges[j + 1]]) for j in range(len(spec))]
    if any(content_chars(p) < min_chars for p in pieces):
        return None
    bounds = [a] + [min(b, max(a, (times[j][1] + times[j + 1][0]) / 2)) for j in range(len(spec) - 1)] + [b]
    if any(y <= x for x, y in zip(bounds, bounds[1:], strict=False)):
        return None
    return [cast(_Cue, dict(cue, start_s=round(bounds[j], 3), end_s=round(bounds[j + 1], 3), text=pieces[j]))
            for j in range(len(spec))]


def effective_split_proposals(cues: Sequence[SrtCue], lines: LinePlan) -> list[SplitProposal]:
    """Project the proposed cuts that the ordinary writer can apply to each current BCUT cue."""
    proposals: list[SplitProposal] = []
    for cue in cues:
        spec = lines.splits.get(cue["id"])
        parts = apply_split(cue, cue["text"], spec) if spec else None
        if parts:
            proposals.append({"cue_id": cue["id"], "source_text": cue["text"],
                              "start_s": cue["start_s"], "end_s": cue["end_s"],
                              "parts": [{"start_s": part["start_s"], "end_s": part["end_s"],
                                         "text": part["text"]} for part in parts]})
    return proposals


def chunk_cues(lines: LinePlan) -> list[SrtCue]:
    """The orphan lines as cues for the v8 chunk pass (so step 4 finds a voiceprint for them)."""
    return [{"id": o["id"], "start_s": o["start_s"], "end_s": o["end_s"], "text": o["text"]} for o in lines.orphan_lines]
