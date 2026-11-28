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
from .publication_id import lineage
from .state import ClipState, StateError, sha256_file

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

    from .config import SessionConfig
    from .state import ClipInfo


class CandidateQuality(TypedDict, total=False):
    reason: str


class SelectionScorecard(TypedDict, total=False):
    start_cue: str | int | float
    end_cue: str | int | float
    candidate_quality: CandidateQuality


class Peak(TypedDict):
    """One of the moments of a candidate the selection says most deserve to be seen (the entry's `peaks`, core_draws)."""
    quote: str      # the words, as the selection's transcript has them
    who: str        # who says them, "" when the selection cannot tell
    at_ms: int      # about when: on the recording's clock; a joined clip's on the joined clock (step 6 reads no time)
    why: str


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
    peaks: NotRequired[JsonValue]        # core_draws.label: the candidate's key moments [{quote, who, at_ms, why}], at_ms on
                                         # the recording's clock (a joined clip: on the joined clock); the title step's landing


class SelectionDocument(TypedDict):
    input_pool: NotRequired[list[SelectionEntry]]


def _entry_in(data: SelectionDocument, cid: str) -> SelectionEntry | None:
    # discover's clip id is the candidate's id plus the recording start (lane_cid: _MMDDHHMM); the recording's own
    # selection file (recall.json) names the candidate without it -- goldfish 4 had to edit the file to init a clip
    bare = re.sub(r"_\d{8}$", "", cid)
    return next((entry for entry in data.get("input_pool", []) if entry.get("cid") in (cid, bare)), None)


def selection_entry(selection_path: Path, cid: str) -> SelectionEntry:
    """The entry of `cid` in a selection file (StateError when it is not there)."""
    entry = _entry_in(cast(SelectionDocument, json.loads(Path(selection_path).read_text(encoding="utf-8"))), cid)
    if entry is None:
        raise StateError(f"candidate {cid} not found in {selection_path}")
    return entry


def clip_entry(clip: ClipInfo) -> SelectionEntry | None:
    """The entry of a clip in the selection file `init` made it from; None for a clip made without one (by hand, in
    tests).  A successor keeps the file of the clip it was made from (revise_run copies `selection_file`; a trimmed
    clip's own file names the trimmed clip), so the clip id is looked up, then the ids it was made from, nearest first
    (publication_id.lineage: `a_trim_r4` -> `a_trim` -> `a`).  A file that is gone or names none of them is an error."""
    path = clip.get("selection_file")
    if not path:
        return None
    if not Path(path).is_file():
        raise StateError(f"selection file of {clip.get('cid')} is gone: {path}")
    data = cast(SelectionDocument, json.loads(Path(path).read_text(encoding="utf-8")))
    cid = str(clip.get("cid") or "")
    for name in sorted(lineage(cid), key=len, reverse=True):
        if (entry := _entry_in(data, name)) is not None:
            return entry
    raise StateError(f"candidate {cid} (nor any clip it was made from) not found in {path}")


def owner_marked(clip: ClipInfo) -> bool:
    """The clip was seated for the channel owner's mark in the chat (discover_select: an entry with `owner_mark`, 11-15
    弹幕里标记的那一段，流水线要自己切出来), read from the selection file `init` made it from (clip_entry)."""
    entry = clip_entry(clip)
    return bool(entry and entry.get("owner_mark"))


def entry_reason(entry: SelectionEntry) -> str:
    """The selection's most detailed account of why this stretch was chosen and what must stay in it
    (`selection_scorecard.candidate_quality.reason`); "" when the entry carries none (a hand-made or compiled entry)."""
    return str((entry.get("selection_scorecard") or {}).get("candidate_quality", {}).get("reason") or "")


class SelectionNotes(TypedDict):
    hook: str
    reason: str
    peaks: list[Peak]


def selection_notes(clip: ClipInfo) -> SelectionNotes:
    """What the selection wrote down about a clip, for the title step: the hook, the detailed reason and the peaks, as
    `clip_from_entry` stored them.  A state made before the reason was stored (no `selection_reason` key, revisions of
    such a clip too) reads it from its selection file (clip_entry); it has no peaks."""
    reason = clip.get("selection_reason")
    if reason is None:
        entry = clip_entry(clip)
        reason = entry_reason(entry) if entry else ""
    return {"hook": str(clip.get("hook") or ""), "reason": str(reason or ""),
            "peaks": cast("list[Peak]", list(clip.get("peaks") or []))}


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
        "selection_reason": entry_reason(entry),
        **({"peaks": entry["peaks"]} if entry.get("peaks") else {}),
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
