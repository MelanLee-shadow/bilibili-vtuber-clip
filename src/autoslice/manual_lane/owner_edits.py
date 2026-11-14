"""Which sentences of an owner-decided push the channel owner actually edited (his authority covers only those).

The push may bring `subs/baseline.srt`: the subtitles as the owner saw them when he edited.  `edited_cues` aligns the
decided cues with it by time overlap (the closer one on a tie); a cue is unedited only when its best-overlapping baseline cue has the same words
(whitespace and line breaks ignored), the same start and end (within TIME_TOL_S) and, when both name a speaker, the same
speaker.  Everything else is edited: new words, another speaker, a split or a join, a moved time, no baseline cue there.
Without a baseline nothing can be compared, so the whole push counts as edited (`pushed_edits` returns None).
`track_numbers` carries the edited import cues over to the final track's cue numbers (normalisation may split cues).
"Edited by the owner" is a property of each sentence and travels with it: a successor clip made from a finished clip's subtitles
(`trim`, `revise`) is handed the predecessor's answer for each sentence it keeps (`owner_numbers`, `carried`), not a fresh
"nothing to compare, all edited".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from . import subtitle_track

TIME_TOL_S = 0.05


def _flat(text: str) -> str:
    return "".join(str(text).split())


def overlap(a: subtitle_track.TrackCue, b: subtitle_track.TrackCue) -> float:
    return min(float(a["end_s"]), float(b["end_s"])) - max(float(a["start_s"]), float(b["start_s"]))


def best_match(cue: subtitle_track.TrackCue, others: Sequence[subtitle_track.TrackCue]) -> int | None:
    """The index of the cue of `others` that `cue` overlaps most in time (on a tie, as with an interjection inside a longer
    sentence, the one whose start and end are closer); None when it overlaps none."""
    found = [(round(overlap(cue, o), 3), -abs(float(cue["start_s"]) - float(o["start_s"]))
              - abs(float(cue["end_s"]) - float(o["end_s"])), -i, i) for i, o in enumerate(others) if overlap(cue, o) > 0]
    return max(found)[3] if found else None


def same_span(a: subtitle_track.TrackCue, b: subtitle_track.TrackCue, tolerance: float) -> bool:
    return (abs(float(a["start_s"]) - float(b["start_s"])) <= tolerance
            and abs(float(a["end_s"]) - float(b["end_s"])) <= tolerance)


def _unchanged(cue: subtitle_track.TrackCue, baseline: Sequence[subtitle_track.TrackCue]) -> bool:
    at = best_match(cue, baseline)
    if at is None:
        return False
    best = baseline[at]
    moved_speaker = bool(cue.get("speaker") and best.get("speaker") and cue["speaker"] != best["speaker"])
    return same_span(cue, best, TIME_TOL_S) and not moved_speaker and _flat(cue["text"]) == _flat(best["text"])


def edited_cues(final: Sequence[subtitle_track.TrackCue], baseline: Sequence[subtitle_track.TrackCue]) -> list[int]:
    """The 1-based numbers of the decided cues the owner edited, against the baseline he edited from."""
    return [n for n, cue in enumerate(final, 1) if not _unchanged(cue, baseline)]


def pushed_cues(final: Path, speaker: Path | None) -> list[subtitle_track.TrackCue]:
    """The cues step 3 reads from a push: the speaker file when there is one, else the final file."""
    path = speaker if speaker is not None and speaker.is_file() else final
    return subtitle_track.parse_blocks(path.read_text(encoding="utf-8"))


def pushed_edits(final: Path, speaker: Path | None, baseline: Path) -> list[int] | None:
    """`edited_cues` of a push against its baseline file; None (everything counts as edited) when it has no baseline."""
    if not baseline.is_file():
        return None
    return edited_cues(pushed_cues(final, speaker), subtitle_track.parse_blocks(baseline.read_text(encoding="utf-8")))


def track_numbers(track: Sequence[subtitle_track.TrackCue], pushed: Sequence[subtitle_track.TrackCue],
                  edited: Sequence[int]) -> set[int]:
    """The final track's cue numbers that are the owner's: a cue overlapping an edited import cue, or overlapping no
    import cue at all (nothing shows it is his to leave alone)."""
    marked = set(edited)
    numbers: set[int] = set()
    for n, cue in enumerate(track, 1):
        touching = [i for i, p in enumerate(pushed, 1) if overlap(cue, p) > 0]
        if not touching or marked.intersection(touching):
            numbers.add(n)
    return numbers


def owner_numbers(base: Path, rec: Mapping[str, object] | None, track: Sequence[subtitle_track.TrackCue]) -> set[int] | None:
    """The cue numbers of `track` the owner edited, from the import record `rec` of the clip whose import files are in `base`;
    None when the import cannot tell (no record, or no baseline: every cue counts)."""
    edited = rec.get("owner_edited") if rec else None
    if not isinstance(edited, list):
        return None
    return track_numbers(track, pushed_cues(base / "final.srt", base / "speaker.srt"), edited)


def carried(origin: Sequence[int | None], owner: set[int] | None) -> list[int] | None:
    """`owner_edited` of a successor's push whose cue j (from 1) is cue `origin[j-1]` of its predecessor's track (None: no such
    cue, nothing shows it is not the owner's), given the predecessor's `owner_numbers`.  None when the predecessor cannot tell."""
    if owner is None:
        return None
    return [j for j, n in enumerate(origin, 1) if n is None or n in owner]
