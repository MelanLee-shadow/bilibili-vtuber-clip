"""A selection entry becomes a clip's state: the one definition `init` and `discover` share.

The entry is a candidate as the selection step writes it (`input_pool` of a selection result): its recording is named by
`source.path` (or, in the older hand-made shape, `segment_path`), its cues by `selection_scorecard.start_cue/end_cue`
(or, failing those, the first and last of `cue_ids`).  The clip's recording is `<stem of that path>.flv`; its BCUT is the
whole-recording subtitle file (`bcut_srt`, the state key is still called `mac_bcut_srt`), with `bcut_source_zero_ms` the
recording time of the file's zero.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from .layout import layout
from .state import ClipState, StateError, sha256_file

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

    from .config import SessionConfig
    from .state import ClipInfo


class SelectionScorecard(TypedDict, total=False):
    start_cue: str | int | float
    end_cue: str | int | float


class EntrySource(TypedDict, total=False):
    path: str


class SelectionEntry(TypedDict):
    cid: str
    start_ms: str | int | float
    end_ms: str | int | float
    segment_path: NotRequired[str]
    source: NotRequired[EntrySource]
    cue_ids: NotRequired[list[str | int]]
    selection_scorecard: NotRequired[SelectionScorecard]
    hook: NotRequired[JsonValue]
    owner_mark: NotRequired[JsonValue]   # discover_select: the owner's mark in the chat this candidate was seated for


class SelectionDocument(TypedDict):
    input_pool: NotRequired[list[SelectionEntry]]


def selection_entry(selection_path: Path, cid: str) -> SelectionEntry:
    """The entry of `cid` in a selection file (StateError when it is not there)."""
    data = cast(SelectionDocument, json.loads(Path(selection_path).read_text(encoding="utf-8")))
    # discover's clip id is the candidate's id plus the recording start (lane_cid: _MMDDHHMM); the recording's own
    # selection file (recall.json) names the candidate without it -- goldfish 4 had to edit the file to init a clip
    bare = re.sub(r"_\d{8}$", "", cid)
    for entry in data.get("input_pool", []):
        if entry.get("cid") in (cid, bare):
            return entry
    raise StateError(f"candidate {cid} not found in {selection_path}")


def owner_marked(clip: ClipInfo) -> bool:
    """The clip was seated for the channel owner's mark in the chat (discover_select: an entry with `owner_mark`, 11-15
    「我要自动流水线能切出这一块来」), read from the selection file `init` made it from.  A clip made without one (by
    hand, in tests) is not marked; a selection file that is gone or no longer names the clip is an error."""
    path = clip.get("selection_file")
    if not path:
        return False
    return bool(selection_entry(Path(path), str(clip.get("cid") or "")).get("owner_mark"))


def entry_source_name(entry: SelectionEntry) -> str:
    """The recording file a candidate was selected from: `<stem>.flv` of its source path."""
    path = entry.get("segment_path") or entry.get("source", {}).get("path")
    if not path:
        raise StateError(f"candidate {entry['cid']} names no recording (source.path or segment_path)")
    return Path(path).stem + ".flv"


def entry_cues(entry: SelectionEntry) -> tuple[int, int]:
    """The candidate's first and last cue number."""
    card = entry.get("selection_scorecard") or {}
    if "start_cue" in card and "end_cue" in card:
        return int(card["start_cue"]), int(card["end_cue"])
    ids = entry.get("cue_ids") or []
    if not ids:
        raise StateError(f"candidate {entry['cid']} names no cues (selection_scorecard.start_cue/end_cue or cue_ids)")
    return int(ids[0]), int(ids[-1])


def clip_from_entry(entry: SelectionEntry, *, cid: str, recording_date: str, bcut_srt: Path, bcut_source_zero_ms: int,
                    remote_root: str | None, source_dir: str | None, selection_file: Path) -> ClipInfo:
    """The clip block of the state file for `entry`.  The clip's work directory on the media host is `<remote_root>/<cid>`
    (default root: the session's directory `<work_root>/<recording date>`)."""
    start_cue, end_cue = entry_cues(entry)
    clip = {
        "cid": cid,
        "recording_date": recording_date,
        "source_name": entry_source_name(entry),
        "start_ms": int(entry["start_ms"]),
        "end_ms": int(entry["end_ms"]),
        "start_cue": start_cue,
        "end_cue": end_cue,
        "proposal": {"start_cue": start_cue, "end_cue": end_cue},
        "hook": str(entry.get("hook", "")),
        "mac_bcut_srt": str(bcut_srt),
        "bcut_source_zero_ms": bcut_source_zero_ms,
        "remote_dir": f"{remote_root or layout().session_root(recording_date)}/{cid}",
        **({"source_dir": source_dir} if source_dir else {}),
        "selection_file": str(selection_file),
        "selection_sha256": sha256_file(selection_file),
    }
    return cast("ClipInfo", clip)


def create_clip_state(task: Path, clip: ClipInfo, session: SessionConfig) -> ClipState:
    """`state/<cid>.json` of the task for `clip` and the session (an existing state file is never overwritten)."""
    state = ClipState.create(task / "state" / f"{clip['cid']}.json", clip=clip, session=session.as_session_block())
    state.data["released"] = [dict(item) for item in session.released]
    state.save()
    return state
