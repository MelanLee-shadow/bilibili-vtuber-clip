"""Which guest said each guest line of a clip with two or more named guests (the channel's request; U115: telling the guests apart is an internal goal, the
burned subtitles still only tell the host from the others).

The host's voiceprint labels a line [连线] without saying who.  The witnesses' diarizers already split the clip into
anonymous speakers (MOSS, MAI: S01, S02 ...), and the talk says who some of them are: a guest named or accused who
answers in the next lines, a guest saying who she is.  One CPA call reads the clip's lines with each guest line's group
and cites such lines; a citation is kept only when its quote stands letter for letter in the cited line and, for an
answer, a spelling of her name stands in one of the three lines before.  A group is that guest when every kept
citation in it says so; a guest line takes its MOSS group's guest (MAI's when MOSS has no group there, MOSS is the
label engine of step 4); a line whose two groups name different guests stays unnamed.  The result is kept in
title/guest_identity.json, and title_inputs labels those lines with the guest's name, so the title may write what she
said as hers and the quote checker holds it to that.

Before any of that, the channel owner's own labels: a clip whose subtitles he decided (`import-subtitles --decided-by
owner`) and whose push brought speakers.json (scripts/speaker_label_page.py apply: his label of every line, by name), or
whose lines stand in the owner tier of the channel's speaker truth store, has its guest lines named by him
(`owner_lines`; `state_owner_rows`, the same reading as the session samples).  His names stand over every citation and group; they also name the groups
they fall in, which names his unlabelled lines; the model is asked only when some guest line is left without his name
(11-24 625460: he labelled 「哦，勾到了。我要笑。」 礼墨Sumi, the lane kept only 连线, and the title had to say 队友).

Next to his labels, the voiceprint itself (`voiceprint_lines`, U467: 「我」 in a collab title must be someone the viewer can
tell; A12).  The frozen classifier of step 4 already scores every ~1.2 s chunk of a linked clip for each of its own
people (speaker/chunks-pred.json); step 4 reads only the host's probability.  When every participant of the session is
one of its classes (speaker_v8.known_participants), a guest line is that guest when, over the chunks it overlaps
(weighted by overlap), the host's probability is at most host_max, the top guest holds at least share_min of the guests'
mass, the line lasts at least min_duration_s and the chunks heard at least min_heard of it (lane_rules guest_voiceprint).
On 维护者's 11-24 names of three 11-23 clips this named 20 of the 62 guest lines the lane labelled 连线, all right;
leave-one-clip-out 26 of 27 right; looser lines put the host's words in a guest's mouth (host 0.086 and 0.100 with a
guest share of 0.97 were his).  The voiceprint's names stand over the groups and citations but never name a group
(in a three-person session one group can hold two people); his labels stand over the voiceprint; the model is asked
only when his labels and the voiceprint together leave a guest line unnamed.  A clip without chunk files (a revision
_r*) or with a guest the classifier does not know is named as before.

Over the voiceprint, step 4's arrival evidence when the clip has it (`arrival_lines`, arrival.py): the lag of a line
against a guest's own recording of the session says which guest spoke, with no classifier (on the owner's 11-23 names it
agreed on 105 of 112 one-speaker lines, and three of the other seven had the voiceprint on its side).  Where several
registered guests can be on the line's sound path (`arrival_among`: someone sits with the recording's owner), the
arrival names nobody: the voiceprint chooses among those guests only, and no other route may name the line anyone
else.

A successor (revise_run, trim_run) has none of that evidence: its step 4 takes the speakers over and runs no arrival or
voiceprint.  What its predecessor named each guest line (step 4's speaker/persons.json, moved with the lines it keeps)
comes with them as speaker/guest_names.json (`carried_lines`) and names the successor's guest lines as it did there, over
the arrival and the voiceprint, under the owner's labels: a revision for a changed word does not change who said what,
nor the guests' colours on the picture."""

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
    from .state import ClipState
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
    """The owner's labels by name of the clip ([{start_s, end_s, text, speaker}], the clip's clock; state_owner_rows),
    None when he labelled none of its lines.  A successor carries them on: revise_run (same clock), trim_run (moved with
    the cues it keeps)."""
    return state_owner_rows(ctx.state) or None


def state_owner_rows(state: ClipState) -> list[dict[str, Any]]:
    """The one reading of "the lines the owner labelled by name" (step 4's interjection lock, the guest names here, the
    session's voice samples and the session labelling page, session_voices.owner_samples): the import's speakers.json
    when the import is his (`--decided-by owner`), then the rows of the clip's family (its revisions share its clock,
    session_voices.family) in the newest owner-tier set of its day in the channel's speaker truth store that no import
    row overlaps."""
    from .session_voices import family, truth_owner_rows
    from .step_import import decided_by_owner

    rec = state.data.get("imported")
    path = ((rec or {}).get("files") or {}).get(OWNER_FILE)
    rows = (cast("list[dict[str, Any]]", json.loads(Path(path).read_text(encoding="utf-8")))
            if path and decided_by_owner(rec) else [])
    stored = truth_owner_rows(str(state.data["session"]["recording_date"])).get(family(str(state.data["clip"]["cid"])), [])
    return rows + [{"start_s": int(cast("int", r["start_ms"])) / 1000, "end_s": int(cast("int", r["end_ms"])) / 1000,
                    "text": str(r.get("text") or ""), "speaker": r["speaker"]}
                   for r in stored
                   if not any(min(float(x["end_s"]), int(cast("int", r["end_ms"])) / 1000)
                              > max(float(x["start_s"]), int(cast("int", r["start_ms"])) / 1000) for x in rows)]


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


CARRIED_FILE = "guest_names.json"       # the import's copy of a successor's push speaker/guest_names.json (step_import.IMPORT_FILES)


def carried_rows(persons: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The guest lines a predecessor named (its step 4 speaker/persons.json rows), as [{start_s, end_s, text, speaker}]
    with the name for speaker: what a successor's push carries as speaker/guest_names.json."""
    return [{"start_s": r["start_s"], "end_s": r["end_s"], "text": r["text"], "speaker": r["person"]}
            for r in persons if r["speaker"] == GUEST_LABEL and r["person"]]


def carried_lines(ctx: Ctx, track: Sequence[TrackCue], guests: Sequence[str]) -> dict[int, str]:
    """{line: guest} for a successor's guest lines, by the predecessor's names its import brought (CARRIED_FILE): a guest
    line takes the name of the carried line that overlaps it most, when that name is one of the session's guests."""
    from .step_import import imported

    path = ((imported(ctx) or {}).get("files") or {}).get(CARRIED_FILE)
    if not path:
        return {}
    names = names_by_overlap(track, json.loads(Path(path).read_text(encoding="utf-8")))
    return {i: str(n) for i, (cue, n) in enumerate(zip(track, names, strict=True), 1)
            if cue.get("speaker") == GUEST_LABEL and n in guests}


VOICE_UNITS = "speaker/chunks-units.json"      # step 3/4's ~1.2 s chunks of the clip (step4_speaker.voice_chunks)
VOICE_PREDS = "speaker/chunks-pred.json"       # the frozen classifier's probabilities of every class for each chunk


def _voice_files(ctx: Ctx) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """(units, predictions doc), or None where the clip has no chunk run (a revision _r* clip, a one-participant clip)
    or the files cannot be read."""
    try:
        pred = json.loads((ctx.clip_dir / VOICE_PREDS).read_text(encoding="utf-8"))
        units = json.loads((ctx.clip_dir / VOICE_UNITS).read_text(encoding="utf-8"))["units"]
    except (OSError, ValueError):
        return None
    return cast("list[dict[str, Any]]", units), cast("dict[str, Any]", pred)


def voiceprint_lines(ctx: Ctx, track: Sequence[TrackCue], guests: Sequence[str],
                     among: Mapping[int, Sequence[str]] | None = None) -> dict[int, str]:
    """{line: guest} for the guest lines the voiceprint's own guest classes name with confidence (see the module
    docstring; the numbers are lane_rules guest_voiceprint).  {} when the clip has no chunk files or some participant
    of the session is no class of the classifier.  `among` (arrival_among) gives the only guests a line can be: the
    choice and the share are taken among them."""
    from .lane_rules import rules
    from .speaker_v8 import known_participants

    got = _voice_files(ctx)
    if got is None:
        return {}
    units, pred = got
    classes = [str(c) for c in pred.get("classes") or []]
    if not known_participants(ctx.session, classes):
        return {}
    cfg = cast("Mapping[str, float]", rules("guest_voiceprint"))
    host_max, share_min = float(cfg["host_max"]), float(cfg["share_min"])
    min_ms, min_heard = float(cfg["min_duration_s"]) * 1000, float(cfg["min_heard"])
    probs = {str(r["id"]): r["probs"] for r in pred.get("predictions") or [] if r.get("ok") and r.get("probs")}
    out: dict[int, str] = {}
    for i, cue in enumerate(track, 1):
        a, b = float(cue["start_s"]) * 1000, float(cue["end_s"]) * 1000
        if cue.get("speaker") != GUEST_LABEL or b - a < min_ms:
            continue
        heard, mass = 0.0, dict.fromkeys(classes, 0.0)
        for unit in units:
            p = probs.get(str(unit["id"]))
            ov = min(b, float(unit["e"])) - max(a, float(unit["s"]))
            if p is None or ov <= 0:
                continue
            heard += ov
            for name in classes:
                mass[name] += ov * float(p.get(name, 0.0))
        others = list((among or {}).get(i) or [n for n in classes if n != HOST])
        guest_mass = sum(mass[n] for n in others)
        if heard < min_heard * (b - a) or guest_mass <= 0:
            continue
        top = max(others, key=mass.__getitem__)
        if top in guests and mass[HOST] / heard <= host_max and mass[top] / guest_mass >= share_min:
            out[i] = top
    return out


def arrival_lines(ctx: Ctx, track: Sequence[TrackCue], guests: Sequence[str]) -> dict[int, str]:
    """{line: guest} for the guest lines step 4's arrival evidence gives to one named guest (arrival.line_persons: the
    lag of the line against a guest's own recording of the session is that guest's or the other's).  It is physical,
    so it stands over the voiceprint's names; {} when the clip has no arrival evidence."""
    from . import arrival

    evidence = arrival.load(ctx.clip_dir)
    if evidence is None:
        return {}
    named = arrival.line_persons(track, evidence["segments"], evidence["persons"], evidence.get("letters") or {})
    return {i: who for i, who in named.items() if who in guests and track[i - 1].get("speaker") == GUEST_LABEL}


def arrival_among(ctx: Ctx, track: Sequence[TrackCue]) -> dict[int, list[str]]:
    """{line: the guests it can be} for the guest lines on a sound path several registered guests share (arrival.py:
    the arrival tells the host from the path, not its people from each other); {} without such a path."""
    from . import arrival

    evidence = arrival.load(ctx.clip_dir)
    among = (evidence or {}).get("among")
    if evidence is None or not among:
        return {}
    labels = arrival.line_persons(track, evidence["segments"], evidence["persons"], evidence.get("letters") or {})
    return {i: list(among[label]) for i, label in labels.items()
            if label in among and track[i - 1].get("speaker") == GUEST_LABEL}


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
    prompt and returns the parsed answer (None: the call failed; only the owner's and the voiceprint's names are
    returned and nothing is kept)."""
    from .step_io import _render

    guests = needed(ctx, track)
    if not guests:
        return {}
    owner = owner_lines(ctx, track, guests)
    arrived = arrival_lines(ctx, track, guests)
    among = arrival_among(ctx, track)
    # the arrival's name over the voiceprint's, a successor's names from its predecessor over both
    voice = {**voiceprint_lines(ctx, track, guests, among), **arrived, **carried_lines(ctx, track, guests)}
    preds_path = ctx.clip_dir / VOICE_PREDS
    voice_file = hashlib.sha256(preds_path.read_bytes()).hexdigest() if preds_path.exists() else None
    groups = _groups(ctx, track)
    lines_text = _prompt_lines(track, groups)
    key = hashlib.sha256(json.dumps([lines_text, guests, groups, sorted(owner.items()), voice_file, sorted(voice.items()),
                                     *([sorted(among.items())] if among else [])],      # absent: the keys kept before it
                                    ensure_ascii=False).encode()).hexdigest()
    path = ctx.clip_dir / PATH
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("key") == key:
            return {int(k): v for k, v in saved["lines"].items()}
    marks = [{"line": i, "name": n} for i, n in sorted(owner.items())]      # his names, as citations that always hold
    if all(i in owner or i in voice for i, c in enumerate(track, 1) if c.get("speaker") == GUEST_LABEL):
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
            # title_inputs reads the record, not this return: it must hold these names, not an older track's, and its
            # key must not match, so that the next run asks again
            failed = {**voice, **owner}
            _write_record(path, {"key": None, "failed": True, "guests": guests, "owner": _str_keys(owner),
                                 "voice": _str_keys(voice), "lines": _str_keys(failed)})
            return failed
        anchors = kept_anchors(answer, track, guests, known)
    lines, names = resolve(track, groups, [*marks, *anchors])
    lines = {i: n for i, n in lines.items() if n in among.get(i, guests)}      # a shared path's line is one of its people
    lines.update(voice)          # a confident voiceprint stands over a group's name, never names a group itself
    lines.update({int(a["line"]): str(a["name"]) for a in anchors if a.get("kind") == "自称"})   # her own words name her
    lines.update(owner)
    _write_record(path, {"key": key, "guests": guests, "owner": _str_keys(owner), "voice": _str_keys(voice),
                         "arrival": _str_keys(arrived), "arrival_among": {str(i): v for i, v in sorted(among.items())},
                         "answer": answer, "anchors": anchors, "groups": names, "lines": _str_keys(lines)})
    return lines


def _str_keys(lines: Mapping[int, str]) -> dict[str, str]:
    return {str(k): v for k, v in sorted(lines.items())}


def _write_record(path: Path, record: Mapping[str, Any]) -> None:
    """Written whole or not at all: title_inputs and the PROCESS note read it while another run may be writing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def saved_lines(ctx: Ctx) -> dict[int, str]:
    """The kept answer of `identify` (title_inputs: every reader of the title sees the same names)."""
    path = ctx.clip_dir / PATH
    if not path.exists():
        return {}
    return {int(k): str(v) for k, v in cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))["lines"].items()}


def named_rows(clip_dir: Path) -> list[dict[str, Any]]:
    """The guest lines step 4 named (speaker/persons.json rows with a person); [] when it named none."""
    from .step4_speaker import PERSONS

    path = clip_dir / PERSONS
    if not path.exists():
        return []
    return [r for r in json.loads(path.read_text(encoding="utf-8")) if r.get("person") and r.get("speaker") == GUEST_LABEL]


def person_at(rows: Sequence[Mapping[str, Any]], start_s: float, end_s: float) -> str | None:
    """The person of the named guest lines `rows` that overlap start_s..end_s most (overlap summed by person; a line
    the review rewrote keeps the name of the line it was), or None when none overlaps.  The one reading of "who says
    this guest line": the burn colours a line by it (step5_burn) and the review is shown it (stage_package), so the
    review sees the person the picture shows."""
    overlap: dict[str, float] = {}
    for r in rows:
        overlap[str(r["person"])] = overlap.get(str(r["person"]), 0.0) + max(
            0.0, min(float(r["end_s"]), end_s) - max(float(r["start_s"]), start_s))
    person = max(overlap, key=overlap.__getitem__, default=None)
    return person if person is not None and overlap[person] > 0 else None


def speaker_names(clip_dir: Path, track: Sequence[TrackCue]) -> list[str | None]:
    """Each cue's speaker by name: the host's label for her lines, the guest person_at names for a guest line (None
    when nobody is named there), None for a cue without a speaker."""
    rows = named_rows(clip_dir)
    return [HOST if c.get("speaker") == HOST else person_at(rows, float(c["start_s"]), float(c["end_s"]))
            if c.get("speaker") == GUEST_LABEL else None for c in track]
