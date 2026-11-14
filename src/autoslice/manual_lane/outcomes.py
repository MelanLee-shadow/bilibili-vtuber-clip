"""What became of each sentence the pipeline wrote, once the channel owner has decided the subtitles.

Owner a sentence the pipeline was unsure of and got right deserves a high reward; the pipeline should learn
from how its uncertain sentences turned out.  The hold rule is calibrated on such outcomes (doubt_policy.is_hold).  Until
now the outcomes were collected by hand; here every owner-decided import writes them down by itself.

When a push decided by the owner brings the subtitles as he saw them (`subs/baseline.srt`), each of those sentences is
one row of <task>/calibration/outcomes.jsonl: what the pipeline had written, what it knew when it wrote it (the doubt
row: confidence, how many runs agreed, the state), what the owner left there, and whether he changed the words.  A push
without a baseline says nothing about what he saw, and records nothing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import owner_edits, subtitle_track
from .state import ClipState, utc_now

FILE = "calibration/outcomes.jsonl"            # under the lane's task directory
KNOWN = ("state", "confidence", "runs_same", "affects_meaning")


def _letters(text: object) -> str:
    return "".join(ch for ch in str(text) if ch.isalnum()).lower()


def _doubt_of(text: str, doubts: list[dict[str, Any]]) -> dict[str, Any]:
    """The doubt row this sentence was written from: the one whose decided text holds it (or is held by it)."""
    want = _letters(text)
    for row in doubts:
        got = _letters(row.get("final_text") or row.get("current") or "")
        if want and got and (want in got or got in want):
            return row
    return {}


def record(state: ClipState, clip_dir: Path, push: Path) -> int:
    """Append this clip's outcomes; returns how many sentences were written (0 without a baseline)."""
    baseline, final = push / "subs/baseline.srt", push / "subs/final.srt"
    if not baseline.is_file() or not final.is_file():
        return 0
    seen = subtitle_track.parse_blocks(baseline.read_text(encoding="utf-8"))
    speaker = push / "speaker/speaker.srt"
    decided = owner_edits.pushed_cues(final, speaker if speaker.is_file() else None)
    try:
        doubts = json.loads((clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doubts = []
    rows = []
    for n, cue in enumerate(seen, 1):
        at = owner_edits.best_match(cue, decided)
        owner = str(decided[at]["text"]) if at is not None else ""
        doubt = _doubt_of(str(cue["text"]), doubts if isinstance(doubts, list) else [])
        rows.append({"cid": state.data["clip"]["cid"], "recorded_at": utc_now(), "n": n,
                     "start_s": float(cue["start_s"]), "end_s": float(cue["end_s"]), "pipeline": str(cue["text"]),
                     "owner": owner, "text_changed": _letters(cue["text"]) != _letters(owner),
                     **{key: doubt.get(key) for key in KNOWN}})
    path = clip_dir.parent.parent / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    return len(rows)
