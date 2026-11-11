"""The finished subtitles as one track (TASK-9).

final.srt and speaker.srt are two writings of one cue list: time, text (its display lines kept), and the speaker.
Every writer after step 3 (the import, step 4 labels, a review fix) edits that list and writes both files from it, so
the two never disagree (FMR A3 checks timing and text parity).  A cue is named by its final.srt number everywhere
(the review prompt, its fixes, the package's doubt rows); a doubt finds its final cues by time (`doubt_finals`),
whichever producer wrote it: step 3 rows carry the BCUT cue (or absolute time), imported rows carry their own time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from .stage_impl import _parse_ts, fmt_ts as fmt_ts
from .srt_text import TimeSpan, TimedText

if TYPE_CHECKING:
    from .srt_text import SrtCue

_LABEL = re.compile(r"^\[([^\[\]]+)\]\s+(.*)$")


class TrackCue(TimedText, total=False):
    speaker: str | None


class DoubtTime(TypedDict, total=False):
    id: str | int
    start_s: str | float | None
    end_s: str | float | None
    abs_start_ms: str | float | None
    abs_end_ms: str | float | None
    cue_id: str | int | None


def parse_blocks(text: str) -> list[TrackCue]:
    """SRT blocks with their display lines kept ("\\n" in `text`); a leading `[名字] ` on the first line becomes
    `speaker`."""
    cues: list[TrackCue] = []
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        timing = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing is None or timing + 1 >= len(lines):
            continue
        start, end = lines[timing].split("-->")
        body = [ln.strip() for ln in lines[timing + 1:]]
        speaker = None
        match = _LABEL.match(body[0])
        if match:
            speaker, body[0] = match.group(1), match.group(2)
        cues.append({"start_s": _parse_ts(start), "end_s": _parse_ts(end), "text": "\n".join(body),
                     "speaker": speaker})
    return cues


_CJK = r"[㐀-鿿，。！？、…]"


def plain_text(text: str) -> str:
    """One line of text from display lines: no space where two Chinese lines meet, a space between other words."""
    return re.sub(r"\s*\n\s*", " ", re.sub(rf"(?<={_CJK})\s*\n\s*(?={_CJK})", "", text)).strip()


def format_blocks(cues: Sequence[TrackCue], *, labels: bool) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        text = str(c["text"])
        if labels and c.get("speaker"):
            text = f"[{c['speaker']}] {text}"
        out.append(f"{i}\n{fmt_ts(float(c['start_s']))} --> {fmt_ts(float(c['end_s']))}\n{text}\n")
    return "\n".join(out)


def read_track(clip_dir: Path) -> list[TrackCue]:
    """The cue list: final.srt's cues with the speaker of speaker.srt (cue for cue when the two match, which is what
    every writer produces; otherwise the speaker cue overlapping most in time), or no speakers without speaker.srt."""
    final = parse_blocks((clip_dir / "subs/final.srt").read_text(encoding="utf-8"))
    spk_path = clip_dir / "speaker/speaker.srt"
    spk = parse_blocks(spk_path.read_text(encoding="utf-8")) if spk_path.exists() else []
    if not spk:
        return final
    if len(spk) == len(final) and all(abs(a["start_s"] - b["start_s"]) < 1e-3 and a["text"] == b["text"]
                                      for a, b in zip(final, spk, strict=True)):
        return spk
    for cue in final:
        here = (cue["start_s"], cue["end_s"])
        best = max(spk, key=lambda s: _overlap(here, (s["start_s"], s["end_s"])))
        cue["speaker"] = best["speaker"] if _overlap(here, (best["start_s"], best["end_s"])) > 0 else None
    return final


def write_track(clip_dir: Path, cues: Sequence[TrackCue], *, speakers: bool) -> None:
    """subs/final.srt (no labels) and, when `speakers`, speaker/speaker.srt (labels) from the same cues."""
    (clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (clip_dir / "subs/final.srt").write_text(format_blocks(cues, labels=False), encoding="utf-8")
    if speakers:
        (clip_dir / "speaker").mkdir(parents=True, exist_ok=True)
        (clip_dir / "speaker/speaker.srt").write_text(format_blocks(cues, labels=True), encoding="utf-8")


def doubt_span(doubt: DoubtTime, bcut_by_id: Mapping[int, TimeSpan],
               clip_start_ms: int) -> tuple[float, float] | None:
    """The doubt's time in the cut clock: its own (imported rows), its absolute time (step 3 rows), or its BCUT cue."""
    if doubt.get("start_s") is not None and doubt.get("end_s") is not None:
        return float(cast("str | float", doubt["start_s"])), float(cast("str | float", doubt["end_s"]))
    if doubt.get("abs_start_ms") is not None and doubt.get("abs_end_ms") is not None:
        return ((int(cast("str | float", doubt["abs_start_ms"])) - clip_start_ms) / 1000,
                (int(cast("str | float", doubt["abs_end_ms"])) - clip_start_ms) / 1000)
    cue = bcut_by_id.get(cast(int, doubt.get("cue_id")))
    if cue is None:
        return None
    return float(cue["start_s"]), float(cue["end_s"])


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def doubt_finals(doubts: Sequence[DoubtTime], final_cues: Sequence[TimeSpan],
                 bcut: Sequence[TimeSpan], clip_start_ms: int) -> dict[str, list[int]]:
    """{doubt id: final.srt numbers}.  Each final cue belongs to the doubt it overlaps most in time (a doubt whose BCUT
    sentence was split into two lines owns both); a doubt with no final cue (laughter only, dropped) maps to []."""
    by_id = {int(cast("SrtCue", c)["id"]): c for c in bcut if c.get("id") is not None}
    spans = {str(d["id"]): s for d in doubts if (s := doubt_span(d, by_id, clip_start_ms)) is not None}
    out: dict[str, list[int]] = {k: [] for k in spans}
    for number, cue in enumerate(final_cues, 1):
        here = (float(cue["start_s"]), float(cue["end_s"]))
        # most overlap; on a tie (an interjection inside a longer sentence) the doubt whose span fits the cue best
        best = max(spans.items(), key=lambda kv: (round(_overlap(here, kv[1]), 3),
                                                  -abs(kv[1][0] - here[0]) - abs(kv[1][1] - here[1])), default=None)
        if best is not None and _overlap(here, best[1]) > 0:
            out[best[0]].append(number)
    return out
