"""`discover`: a candidate that a viewer of the circle cannot follow because its cause was said before it -- perhaps in
the previous recording -- is given what comes before it.

Every recording (half an hour) is selected by itself.  On the host's stance on an idol's love affair (her
「我不想知道他的私事」 near the end of the recording of 22:30) went on into the recording of 23:00; its candidate
there, a relation/yuri matter among the day's highest content points, was labelled not followable in three full
replays of three (「开头讨论的“她”具体是谁…片内没有补出」) and not made.  The matter began about five minutes before
it (「如果你知道李豆沙的前推…而是直接告诉你她怀孕了」, 22:57); given only the three minutes before it, the lead-in
call still answered not followable (「片前始终用“他”指代舞台人物，没有明确交代是谁」).  The pull-back of the recall
(semantic_candidate_selector._pull_back_dangling_openings) moves a start only inside its own recording, and only where
the first line dangles by rule.

After every recording of a day is selected and labelled, one call (prompts/discover_lead_in.md) is given, for each
talk candidate labelled not followable (core_draws `understood` false), the lines before it as far back as the clip,
begun there, would still be at most gates.CLIP_MAX_SECONDS long (`_lookback_ms`) -- the previous recording's last lines
too, when the candidate begins less than that into its own and the previous recording's speech runs up to its start --
and the candidate's own lines, and says from which of the earlier lines
its matter begins and whether, with them, a viewer of the circle can follow it.  A candidate that can be followed so
carries `lead_in`: the stretches before it (one per recording) its clip begins with; its label then says it is
followable and keeps what was missing (`missing_before`).  Seating joins them like one matter's candidates
(discover_select `topic_members`) and discover makes the clip with the same-session compilation.  A lead-in that
would make the clip longer than gates.CLIP_MAX_SECONDS is not given (the candidate stays not followable, and the
record says why).  The answer is kept in `<day>/lead_ins.json` and read back while the prompt is the same.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from . import channel
from .discover_media import write_json
from .gates import CLIP_MAX_SECONDS

SCHEMA = "manual-lane.discover-lead-ins.v1"
FILE = "lead_ins.json"
PURPOSE = "discover-lead-in"
CONTIGUOUS_GAP_MS = 120_000            # the previous recording's last line ends at most this long before this one begins
_STAMP = re.compile(r"_(\d{8})-(\d{2})-(\d{2})-(\d{2})$")


class SourceCue(Protocol):
    """A subtitle line of a recording (discover_select._cues reads them; the old pipeline's type stays behind that boundary)."""
    cue_id: str
    source_start_ms: int
    source_end_ms: int
    text: str


class LeadInError(ValueError):
    """The answer is not usable (the day is seated without lead-ins, and the ledger says so)."""


def _begins(name: str) -> dt.datetime | None:
    stamp = _STAMP.search(name)
    return None if stamp is None else dt.datetime.strptime("".join(stamp.groups()), "%Y%m%d%H%M%S")


def _lookback_ms(entry: Mapping[str, Any]) -> int:
    """How far before the candidate its matter may begin: a clip begun there is at most CLIP_MAX_SECONDS long."""
    return max(0, round(CLIP_MAX_SECONDS * 1000) - (int(entry["end_ms"]) - int(entry["start_ms"])))


def _before(entry: Mapping[str, Any], recordings: Sequence[tuple[str, Sequence[SourceCue]]]) -> list[tuple[str, SourceCue]]:
    """The lines of the `_lookback_ms` before the candidate, oldest first, each with its recording's name: its own
    recording's, and the previous recording's last ones when the window reaches back past the start of its own and the
    previous recording's speech runs up to it."""
    names = [name for name, _ in recordings]
    own = str(entry["source"]["name"])
    at = names.index(own)
    start = int(entry["start_ms"])
    lookback = _lookback_ms(entry)
    out = [(own, c) for c in recordings[at][1] if start - lookback <= c.source_start_ms < start]
    if start < lookback and at > 0:
        prev, cues = recordings[at - 1]
        mine, theirs = _begins(own), _begins(prev)
        if mine is not None and theirs is not None and cues:
            offset_ms = round((mine - theirs).total_seconds() * 1000)      # the previous recording's clock, at this one's 0
            if offset_ms - cues[-1].source_end_ms <= CONTIGUOUS_GAP_MS:
                floor = offset_ms - (lookback - start)
                out = [(prev, c) for c in cues if c.source_start_ms >= floor] + out
    return out


def _todo(pool: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted((e for e in pool if e.get("kind") == "talk" and (e.get("core_draws") or {}).get("understood") is False),
                  key=lambda e: (str(e["source"]["name"]), int(e["start_ms"])))


def prompt(items: Sequence[tuple[Mapping[str, Any], Sequence[tuple[str, SourceCue]], Sequence[SourceCue]]]) -> str:
    blocks = []
    for number, (entry, before, own) in enumerate(items, start=1):
        earlier = "\n".join(f"#P{n} {' '.join(c.text.split())}" for n, (_, c) in enumerate(before, start=1))
        inside = "\n".join(f"#{c.cue_id} {' '.join(c.text.split())}" for c in own)
        blocks.append(f"候选 {number}\n选片理由：{entry.get('hook', '')}\n看不懂的地方：{entry['core_draws'].get('missing', '')}\n"
                      f"片前的字幕（紧挨着片子开头之前，按时间先后）：\n{earlier}\n片内的字幕：\n{inside}")
    return channel.prompt("discover_lead_in.md").replace("{candidates}", "\n\n".join(blocks))


def parse(raw: str, before: Sequence[Sequence[tuple[str, SourceCue]]]) -> list[dict[str, Any]]:
    """One answer per candidate, in order: {from: index into its earlier lines or None, understood, missing}."""
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        doc = json.loads(match.group(0) if match else raw)
    except json.JSONDecodeError as exc:
        raise LeadInError(f"not JSON: {exc}") from exc
    rows = doc.get("leads") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or len(rows) != len(before):
        raise LeadInError(f"`leads` must answer each of the {len(before)} candidates")
    out: list[dict[str, Any] | None] = [None] * len(before)
    for row in rows:
        number = row.get("id") if isinstance(row, dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= len(before) or out[number - 1]:
            raise LeadInError(f"bad lead row {str(row)[:200]}")
        start = row.get("from")
        index = None
        if start is not None:
            found = re.fullmatch(r"#?P(\d+)", str(start).strip())
            if found is None or not 1 <= int(found.group(1)) <= len(before[number - 1]):
                raise LeadInError(f"bad lead row {str(row)[:200]}")
            index = int(found.group(1)) - 1
        if not isinstance(row.get("understood"), bool):
            raise LeadInError(f"bad lead row {str(row)[:200]}")
        out[number - 1] = {"from": index, "understood": row["understood"] and index is not None,
                           "missing": str(row.get("missing") or "").strip()[:200], "why": str(row.get("why") or "").strip()[:200]}
    return [r for r in out if r is not None]


def _stretches(entry: Mapping[str, Any], lines: Sequence[tuple[str, SourceCue]], sources: Mapping[str, Mapping[str, Any]],
               why: str) -> list[dict[str, Any]]:
    """The lead-in of `entry` from `lines` (its earlier lines from the answer's one on): one stretch per recording, each in
    the shape of a candidate the compilation can cut (discover._build_joined)."""
    by_name: dict[str, list[SourceCue]] = {}
    for name, cue in lines:
        by_name.setdefault(name, []).append(cue)
    out = []
    for n, (name, cues) in enumerate(by_name.items(), start=1):
        out.append({"cid": f"{entry['cid']}_lead{n}", "candidate_id": f"{entry['candidate_id']}_lead{n}", "kind": "talk",
                    "source": dict(sources[name]), "start_ms": cues[0].source_start_ms, "end_ms": cues[-1].source_end_ms,
                    "cue_ids": [c.cue_id for c in cues], "hook": f"前因：{why}", "peaks": []})
    return out


def assign(pool: list[dict[str, Any]], day_dir: Path, ask: Callable[[str], str],
           cues_of: Callable[[Path], list[SourceCue]]) -> dict[str, Any]:
    """Set `lead_in` on the candidates of `pool` that a viewer of the circle can follow with the lines before them, mark
    their labels followable, and return the record kept in `<day_dir>/lead_ins.json`.  `cues_of(directory)` reads a
    recording's subtitles.  With no candidate labelled not followable nothing is asked.  A failed call raises what `ask`
    raises; an unusable answer raises LeadInError."""
    for entry in pool:
        entry.pop("lead_in", None)
    todo = _todo(pool)
    if not todo:
        return {"schema": SCHEMA, "leads": []}
    recordings, sources = [], {}
    for directory in sorted(p for p in day_dir.iterdir() if (p / "bcut.srt").exists() and (p / "recall.json").exists()):
        recordings.append((directory.name, cues_of(directory)))
        sources[directory.name] = json.loads((directory / "recall.json").read_text(encoding="utf-8"))["source"]
    items = []
    for entry in todo:
        own = [c for c in dict(recordings)[str(entry["source"]["name"])] if c.cue_id in {str(x) for x in entry["cue_ids"]}]
        items.append((entry, _before(entry, recordings), own))
    asked = [item for item in items if item[1]]
    leads = [{"cid": e["cid"], "applied": False, "note": "片前没有字幕可接"} for e, before, _ in items if not before]
    if asked:
        text = prompt(asked)
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        path = day_dir / FILE
        kept = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if kept is not None and kept.get("schema") == SCHEMA and kept.get("prompt_sha256") == fingerprint:
            answers = kept["answers"]
        else:
            answers = parse(ask(text), [before for _, before, _ in asked])
            write_json(path, {"schema": SCHEMA, "prompt_sha256": fingerprint, "candidates": [e["cid"] for e, _, _ in asked],
                              "answers": answers})
        for (entry, before, _), answer in zip(asked, answers, strict=True):
            label = entry["core_draws"]
            if not answer["understood"]:
                leads.append({"cid": entry["cid"], "applied": False, "note": f"往前接了也看不懂：{answer['missing']}"})
                continue
            stretches = _stretches(entry, before[answer["from"]:], sources, answer["why"])
            length_ms = sum(s["end_ms"] - s["start_ms"] for s in stretches) + int(entry["end_ms"]) - int(entry["start_ms"])
            if length_ms > CLIP_MAX_SECONDS * 1000:
                leads.append({"cid": entry["cid"], "applied": False,
                              "note": f"接上前因片长 {length_ms // 1000} 秒，超过 {CLIP_MAX_SECONDS:.0f} 秒"})
                continue
            entry["lead_in"] = stretches
            entry["core_draws"] = dict(label, understood=True, missing="", missing_before=label.get("missing", ""))
            leads.append({"cid": entry["cid"], "applied": True, "from": [s["cid"] for s in stretches],
                          "note": answer["why"]})
    return {"schema": SCHEMA, "leads": leads}
