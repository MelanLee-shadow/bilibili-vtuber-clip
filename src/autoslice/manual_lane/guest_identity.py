"""Which guest said each guest line of a clip with two or more named guests (公开规则11-23: 「声纹听不出来，但是所有声音
证据很可能能确定到一个具体的人吧？光声纹定不下时综合所有证据」; U115: telling the guests apart is an internal goal, the
burned subtitles still only tell the host from the others).

The host's voiceprint labels a line [连线] without saying who. The witnesses' diarizers already split the clip into
anonymous speakers (MOSS, MAI: S01, S02 ...), and the talk says who some of them are: a guest named or accused who
answers in the next lines, a guest saying who she is. One CPA call reads the clip's lines with each guest line's group
and cites such lines; a citation is kept only when its quote stands letter for letter in the cited line and, for an
answer, a spelling of her name stands in one of the three lines before. A group is that guest when every kept
citation in it says so; a guest line takes its MOSS group's guest (MAI's when MOSS has no group there, MOSS is the
label engine of step 4); a line whose two groups name different guests stays unnamed. The result is kept in
title/guest_identity.json, and title_inputs labels those lines with the guest's name, so the title may write what she
said as hers and the quote checker holds it to that.

Before any of that, the channel owner's own labels: a clip whose subtitles he decided (`import-subtitles --decided-by
owner`) and whose push brought speakers.json (scripts/speaker_label_page.py apply: his label of every line, by name) has
its guest lines named by him (`owner_lines`). His names stand over every citation and group; they also name the groups
they fall in, which names his unlabelled lines; the model is asked only when some guest line is left without his name
(11-24 625460: he labelled 「哦，勾到了。我要笑。」 礼墨Sumi, the lane kept only 连线, and the title had to say 队友)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from . import text_overlap
from .speaker_v8 import GUEST_LABEL, HOST

if TYPE_CHECKING:
    from .runner import Ctx
    from .subtitle_track import TrackCue

ENGINES = ("moss", "mai")          # MOSS first: it is the label engine of step 4
KINDS = ("被点名后接话", "自称", "说自己的事")
BEFORE = 3                          # lines before an answer where her name must stand
PATH = "title/guest_identity.json"
GROUP_LETTERS = "甲乙丙丁戊己庚辛"


def _groups(ctx: Ctx, track: Sequence[TrackCue]) -> dict[str, list[str | None]]:
    """Per engine, the anonymous speaker with most overlap on each cue (None where the engine has no segment)."""
    out: dict[str, list[str | None]] = {}
    for engine in ENGINES:
        path = ctx.clip_dir / f"witness/{engine}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        segs = [s for s in doc.get("segments") or [] if s.get("speaker")] if doc.get("status") == "OK" else []
        if not segs:
            continue
        rows: list[str | None] = []
        for cue in track:
            a, b = float(cue["start_s"]) * 1000, float(cue["end_s"]) * 1000
            overlap: dict[str, float] = {}
            for seg in segs:
                ov = min(float(seg["end_ms"]), b) - max(float(seg["start_ms"]), a)
                if ov > 0:
                    overlap[str(seg["speaker"])] = overlap.get(str(seg["speaker"]), 0.0) + ov
            rows.append(max(overlap, key=overlap.__getitem__) if overlap else None)
        out[engine] = rows
    return out


def needed(ctx: Ctx, track: Sequence[TrackCue]) -> list[str]:
    """The guests to tell apart: two or more named guests and a guest line; else []."""
    from .config import participant_names

    guests = [n for n in participant_names(ctx.session) if n != HOST]
    return guests if len(guests) >= 2 and any(c.get("speaker") == GUEST_LABEL for c in track) else []


def kept_anchors(answer: Any, track: Sequence[TrackCue], guests: Sequence[str],
                 spellings: Mapping[str, Sequence[str]]) -> list[dict[str, Any]]:
    """The answer's citations that hold: a guest line, a known guest, a quote that stands in that line, and for an
    answer, one of her spellings in the BEFORE lines before it."""
    from .subtitle_track import plain_text

    texts = [text_overlap.key(plain_text(str(c["text"]))) for c in track]
    out = []
    for item in (answer.get("anchors") or []) if isinstance(answer, dict) else []:
        if not isinstance(item, dict):
            continue
        name, kind, quote = str(item.get("name") or ""), str(item.get("kind") or ""), text_overlap.key(str(item.get("quote") or ""))
        try:
            line = int(item.get("line"))
        except (TypeError, ValueError):
            continue
        if name not in guests or kind not in KINDS or not quote or not 1 <= line <= len(track):
            continue
        if track[line - 1].get("speaker") != GUEST_LABEL or quote not in texts[line - 1]:
            continue
        if kind == "被点名后接话":
            words = [text_overlap.key(s) for s in [name, *spellings.get(name, [])] if text_overlap.key(s)]
            if not any(w in t for t in texts[max(0, line - 1 - BEFORE):line - 1] for w in words):
                continue
        out.append({"line": line, "name": name, "kind": kind, "quote": str(item.get("quote"))})
    return out


def resolve(track: Sequence[TrackCue], groups: Mapping[str, Sequence[str | None]],
            anchors: Sequence[Mapping[str, Any]]) -> tuple[dict[int, str], dict[str, dict[str, str]]]:
    """({line: guest}, {engine: {group: guest}}): a group is the guest all its kept citations name; a cited line is its
    guest; another guest line takes its MOSS group's guest, MAI's where MOSS has none, nobody where they differ."""
    names: dict[str, dict[str, str]] = {}
    for engine, rows in groups.items():
        votes: dict[str, set[str]] = {}
        for a in anchors:
            group = rows[int(a["line"]) - 1]
            if group is not None:
                votes.setdefault(group, set()).add(str(a["name"]))
        names[engine] = {g: next(iter(v)) for g, v in votes.items() if len(v) == 1}
    lines: dict[int, str] = {int(a["line"]): str(a["name"]) for a in anchors}
    for i, cue in enumerate(track, 1):
        if cue.get("speaker") != GUEST_LABEL or i in lines:
            continue
        said = [names[e].get(str(groups[e][i - 1])) for e in ENGINES if e in groups and groups[e][i - 1] is not None]
        heard = [n for n in said if n]
        first = said[0] if said else None
        if first and all(n == first for n in heard):
            lines[i] = first
        elif not first and heard and len(set(heard)) == 1:
            lines[i] = heard[0]
    return lines, names


OWNER_FILE = "speakers.json"       # the import's copy of the push's speaker/speakers.json (step_import.IMPORT_FILES)


def owner_rows(ctx: Ctx) -> list[dict[str, Any]] | None:
    """The owner's labels by name ([{start_s, end_s, text, speaker}], the clip's clock) when the clip's import is his
    (`--decided-by owner`) and brought them; None otherwise.  A successor carries them on: revise_run (same clock),
    trim_run (moved with the cues it keeps)."""
    from .step_import import imported, owner_decided

    path = ((imported(ctx) or {}).get("files") or {}).get(OWNER_FILE)
    if not path or not owner_decided(ctx):
        return None
    return cast("list[dict[str, Any]]", json.loads(Path(path).read_text(encoding="utf-8")))


def names_by_overlap(cues: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> list[str | None]:
    """For each cue, the speaker of the owner's row that overlaps it most (None where none does)."""
    out: list[str | None] = []
    for cue in cues:
        a, b = float(cue["start_s"]), float(cue["end_s"])
        best, most = None, 0.0
        for row in rows:
            ov = min(float(row["end_s"]), b) - max(float(row["start_s"]), a)
            if ov > most:
                best, most = row, ov
        out.append(str(best["speaker"]) if best is not None and best.get("speaker") else None)
    return out


def owner_lines(ctx: Ctx, track: Sequence[TrackCue], guests: Sequence[str]) -> dict[int, str]:
    """{line: guest} for the guest lines the channel owner named himself (see the module docstring): a guest line takes the
    name of his line that overlaps it most; a name that is no guest (the host, 未知) names nothing.  {} when the import
    is not his or brought no speakers.json."""
    rows = owner_rows(ctx)
    if not rows:
        return {}
    names = names_by_overlap(track, rows)
    return {i: str(n) for i, (cue, n) in enumerate(zip(track, names, strict=True), 1)
            if cue.get("speaker") == GUEST_LABEL and n in guests}


def _prompt_lines(track: Sequence[TrackCue], groups: Mapping[str, Sequence[str | None]]) -> str:
    from .subtitle_track import plain_text

    rows = groups.get("moss") or groups.get("mai") or []
    letters: dict[str, str] = {}
    out = []
    for i, cue in enumerate(track, 1):
        spk = cue.get("speaker")
        label = f"[{spk}]" if spk else ""
        if spk == GUEST_LABEL and rows and rows[i - 1] is not None:
            letter = letters.setdefault(str(rows[i - 1]), GROUP_LETTERS[len(letters) % len(GROUP_LETTERS)])
            label = f"[{spk}·{letter}]"
        out.append(f"{i} {label} {plain_text(str(cue['text']))}")
    return "\n".join(out)


def identify(ctx: Ctx, track: Sequence[TrackCue], ask: Callable[[str, str], dict[str, Any] | None]) -> dict[int, str]:
    """{line: guest} for the clip's guest lines, kept in PATH; {} when there is nothing to tell apart.  `ask` sends the
    prompt and returns the parsed answer (None: the call failed, nothing is named and nothing is kept)."""
    from .step_io import _render

    guests = needed(ctx, track)
    if not guests:
        return {}
    owner = owner_lines(ctx, track, guests)
    groups = _groups(ctx, track)
    lines_text = _prompt_lines(track, groups)
    key = hashlib.sha256(json.dumps([lines_text, guests, groups, sorted(owner.items())], ensure_ascii=False).encode()).hexdigest()
    path = ctx.clip_dir / PATH
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("key") == key:
            return {int(k): v for k, v in saved["lines"].items()}
    marks = [{"line": i, "name": n} for i, n in sorted(owner.items())]      # his names, as citations that always hold
    if owner and all(i in owner for i, c in enumerate(track, 1) if c.get("speaker") == GUEST_LABEL):
        answer: dict[str, Any] | None = None
        anchors: list[dict[str, Any]] = []
    else:
        from .collab_guests import streamers

        try:
            known = streamers(ctx)
        except Exception:  # noqa: BLE001 - without the graph an answer is checked against the formal name alone
            known = {}
        prompt = _render("guest_identity.md", host=HOST, guests="、".join(guests), lines=lines_text)
        answer = ask("guest-identity", prompt)
        if answer is None:
            return owner
        anchors = kept_anchors(answer, track, guests, known)
    lines, names = resolve(track, groups, [*marks, *anchors])
    lines.update(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": key, "guests": guests, "owner": {str(k): v for k, v in sorted(owner.items())},
                                "answer": answer, "anchors": anchors, "groups": names,
                                "lines": {str(k): v for k, v in sorted(lines.items())}},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return lines


def saved_lines(ctx: Ctx) -> dict[int, str]:
    """The kept answer of `identify` (title_inputs: every reader of the title sees the same names)."""
    path = ctx.clip_dir / PATH
    if not path.exists():
        return {}
    return {int(k): str(v) for k, v in cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))["lines"].items()}
