"""`resolve-dissent`: a person's disposition of one review objection to what the channel owner decided (dissent_register),
replacing the default the objection was registered with (keep, on the owner-final rule); optional, the clip is packaged and
sealed without it.

`--keep`: the owner's writing stands, now under the named person.  `--apply`: the review's proposal is taken as an owner-authorised edit of the
import (import/final.srt and import/speaker.srt: the push the owner's decision lives in), the cue becomes one the owner
edited (imported["owner_edited"]), and step 3 and everything after it are produced again from the import
(step_import.forget_steps, the same redo as importing again); the other objections keep their dispositions.  Only a text or a
speaker proposal can be applied, and only to a final cue that is one import cue exactly (a split or joined one cannot be
mapped back: edit the push and import it again).  The disposition goes to the state and to PROCESS.md.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TypedDict

from . import dissent_register, owner_edits, subtitle_track
from .state import ClipState, StateError
from .step_import import IMPORT_DIR, forget_steps

class Resolved(TypedDict):
    cid: str
    item: int
    how: str
    redo: str                 # from_step_3 (apply) | none (keep)
    on_default: list[int]     # the objections still on the default disposition


MAP_TOL_S = 0.25      # the final cue and the import cue it came from may differ this much at either end (overlap shifts)


def _import_index(clip_dir: Path, item: dissent_register.DissentItem, pushed: list[subtitle_track.TrackCue]) -> int:
    """The 0-based import cue that is exactly the final cue of the objection."""
    track = subtitle_track.read_track(clip_dir)
    n = item["cue"]
    if n is None or not 1 <= n <= len(track) or subtitle_track.plain_text(track[n - 1]["text"]) != item["text"]:
        raise StateError(f"第 {n} 句现在已不是异议记录的文字，不能套用；先看 REVIEW.md 与现行字幕")
    at = owner_edits.best_match(track[n - 1], pushed)
    if at is None or owner_edits.best_match(pushed[at], track) != n - 1 or not owner_edits.same_span(
            track[n - 1], pushed[at], MAP_TOL_S):
        raise StateError(f"最终字幕第 {n} 句与推送的字幕不是一句对一句（被拆分、合并或挪动过），无法自动改写；"
                         f"请改推送文件后重新 import-subtitles")
    return at


def _edit_import(clip_dir: Path, item: dissent_register.DissentItem, pushed: list[subtitle_track.TrackCue],
                 index: int) -> None:
    """Write the proposal into the import files (the cue at `index`): the text into the final and speaker files, a
    speaker label into the speaker file."""
    base = clip_dir / IMPORT_DIR
    final, speaker = base / "final.srt", base / "speaker.srt"
    if item["kind"] == "speaker" and not speaker.is_file():
        raise StateError("推送里没有说话人文件，无法改说话人")
    for path, labels in ((final, False), (speaker, True)):
        if not path.is_file() or (item["kind"] == "speaker" and path is final):
            continue
        cues = subtitle_track.parse_blocks(path.read_text(encoding="utf-8"))
        if len(cues) != len(pushed):
            if path is final and speaker.is_file():
                continue          # step 3 reads the speaker file's cues
            raise StateError(f"{path} 的句数与推送的字幕不一致")
        if item["kind"] == "speaker":
            cues[index]["speaker"] = item["value"]
        else:
            cues[index]["text"] = item["value"]
        path.write_text(subtitle_track.format_blocks(cues, labels=labels), encoding="utf-8")


def _process(clip_dir: Path, item: dissent_register.DissentItem) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with (clip_dir / "PROCESS.md").open("a", encoding="utf-8") as fh:
        fh.write(f"\n## 异议处置 · {stamp}\n\n- {dissent_register.line(item)}\n")


def resolve_dissent(task: Path, cid: str, *, item: int, how: str, by: str, reason: str) -> Resolved:
    state = ClipState.load(task / "state" / f"{cid}.json")
    clip_dir = task / "clips" / cid
    done = dissent_register.resolve(state, item, how, by, reason)     # in memory: nothing is saved before the edit is made
    if how == "apply":
        rec = state.data.get("imported")
        if done["kind"] not in ("text", "speaker") or rec is None:
            raise StateError(f"第 {item} 条是「{done['kind']}」类异议（或片没有导入记录），只能 --keep")
        pushed = owner_edits.pushed_cues(clip_dir / IMPORT_DIR / "final.srt", clip_dir / IMPORT_DIR / "speaker.srt")
        index = _import_index(clip_dir, done, pushed)
        _edit_import(clip_dir, done, pushed, index)
        if rec.get("owner_edited") is not None:
            rec["owner_edited"] = sorted({*(rec.get("owner_edited") or []), index + 1})
        forget_steps(state)
    state.save()
    _process(clip_dir, done)
    return {"cid": cid, "item": item, "how": how, "redo": "from_step_3" if how == "apply" else "none",
            "on_default": [i["n"] for i in dissent_register.defaulted(state)]}
