"""Reselect a candidate's window from CPA's last cut (U103, `--from-counter` only), and supersede marking.

`reselect --from-counter` opens a new candidate `auto_<segment>_<start_s>_<end_s>` whose window is the cut CPA
adopted in the original candidate's boundary step (boundary/decision.json, "adopted").  No CPA call.  The
original is left untouched.

`supersede` only marks a candidate SUPERSEDED by a named successor and records why.  Its file is kept.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from .srt_text import SrtCue, parse_srt
from .state import DEFAULT_BCUT_SOURCE_ZERO_MS, ClipState, utc_now

if TYPE_CHECKING:
    from .state import ClipInfo

SUPERSEDED = "SUPERSEDED"
_CID = re.compile(r"auto_(\d{6})_(\d+)_(\d+)")
# Clip keys that describe one candidate's own window or status; the new candidate does not inherit them.
_PER_CANDIDATE_KEYS = frozenset({"status", "superseded_by", "superseded", "proposal", "selection_window",
                                 "reselected_from", "authority", "reselect", "start_cue", "end_cue",
                                 "start_ms", "end_ms", "cover_style_id", "cover_renderer"})


class ChosenCut(TypedDict):
    start_cue: int
    end_cue: int
    start_ms: int
    end_ms: int


@dataclass
class ReselectResult:
    ok: bool
    reason: str = ""
    new_cid: str = ""
    detail: str = ""
    chosen: ChosenCut | None = None


def load_cues(path: Path, *, source_zero_ms: int = DEFAULT_BCUT_SOURCE_ZERO_MS) -> list[SrtCue]:
    """Read original BCUT cues and project their times onto the source-recording clock."""
    offset = source_zero_ms / 1000
    return [SrtCue(id=cue["id"], text=cue["text"], start_s=cue["start_s"] + offset, end_s=cue["end_s"] + offset)
            for cue in parse_srt(Path(path).read_text(encoding="utf-8"))]


def reselect_from_counter(*, task_dir: Path, orig: ClipState, cues: Sequence[SrtCue],
                          authority: str, quote: str) -> ReselectResult:
    """Open a candidate from the original's last CPA cut.  Nothing is written on any stop."""
    if not (authority.strip() and quote.strip()):
        raise ValueError("reselect needs a non-empty authority and quote")
    task_dir = Path(task_dir)
    clip = orig.data["clip"]
    orig_cid = str(clip["cid"])
    match = _CID.fullmatch(orig_cid)
    if match is None:
        raise ValueError(f"candidate id {orig_cid!r} does not follow auto_<HHMMSS>_<start_s>_<end_s>")
    decision_path = task_dir / "clips" / orig_cid / "boundary" / "decision.json"
    if not decision_path.exists():
        return ReselectResult(False, "RESELECT_NO_COUNTER", detail=f"{orig_cid} has no boundary decision yet")
    adopted = json.loads(decision_path.read_text(encoding="utf-8")).get("adopted") or {}
    c_start, c_end = adopted.get("start_cue"), adopted.get("end_cue")
    by_id = {int(c["id"]): c for c in cues}
    if c_start is None or c_end is None or int(c_start) not in by_id or int(c_end) not in by_id:
        return ReselectResult(False, "RESELECT_COUNTER_INVALID", detail="the adopted cut is not in this segment")
    c_start, c_end = int(c_start), int(c_end)
    if (c_start, c_end) == (int(clip["start_cue"]), int(clip["end_cue"])):
        return ReselectResult(False, "RESELECT_UNCHANGED", detail="the adopted cut is the candidate's own cut")
    start_ms = round(float(by_id[c_start]["start_s"]) * 1000)
    end_ms = round(float(by_id[c_end]["end_s"]) * 1000)
    new_cid = f"auto_{match.group(1)}_{start_ms // 1000}_{end_ms // 1000}"
    if new_cid == orig_cid or (task_dir / "state" / f"{new_cid}.json").exists():
        return ReselectResult(False, "RESELECT_ID_EXISTS", new_cid=new_cid, detail=f"{new_cid} already exists")
    new_clip = {k: v for k, v in clip.items() if k not in _PER_CANDIDATE_KEYS}
    new_clip.update({
        "cid": new_cid, "start_cue": c_start, "end_cue": c_end, "start_ms": start_ms, "end_ms": end_ms,
        "remote_dir": f"{clip['remote_dir'].rsplit('/', 1)[0]}/{new_cid}",
        "proposal": {"start_cue": c_start, "end_cue": c_end},
        "reselected_from": orig_cid, "authority": authority,
        "reselect": {"authority": authority, "quote": quote, "original": orig_cid, "adopted_cut": [c_start, c_end]},
    })
    new = ClipState.create(task_dir / "state" / f"{new_cid}.json", clip=cast("ClipInfo", new_clip),
                           session=orig.data["session"])
    new.data["released"] = [dict(item) for item in orig.data.get("released", [])]
    new.save()
    chosen: ChosenCut = {"start_cue": c_start, "end_cue": c_end, "start_ms": start_ms, "end_ms": end_ms}
    return ReselectResult(True, "", new_cid, f"new candidate from CPA's last cut {c_start}-{c_end}", chosen)


def supersede_candidate(*, task_dir: Path, cid: str, by: str, authority: str, quote: str,
                        reason: str) -> ReselectResult:
    """Mark `cid` SUPERSEDED by `by` and record why.  The file is kept."""
    if not (authority.strip() and quote.strip() and reason.strip()):
        raise ValueError("supersede needs a non-empty authority, quote and reason")
    task_dir = Path(task_dir)
    if cid == by:
        return ReselectResult(False, "SUPERSEDE_SELF", detail="a candidate cannot supersede itself")
    target = ClipState.load(task_dir / "state" / f"{cid}.json")
    ClipState.load(task_dir / "state" / f"{by}.json")   # the successor must exist
    clip = target.data["clip"]
    if clip.get("status") == SUPERSEDED:
        return ReselectResult(False, "ALREADY_SUPERSEDED", detail=f"{cid} is already superseded by {clip.get('superseded_by')}")
    clip["status"] = SUPERSEDED
    clip["superseded_by"] = by
    clip["superseded"] = {"by": by, "authority": authority, "quote": quote, "reason": reason, "at": utc_now()}
    target.save()
    return ReselectResult(True, "", by, f"{cid} marked SUPERSEDED by {by}")
