"""`discover`, the selection side: one recording's candidates, and the day's seats.

The selection logic is the old pipeline's and is called, not rewritten: `select_semantic_session_candidates_covered`
recalls candidates from a recording's whole-recording subtitles (and its danmaku), each with the scorecard the
metric defines.  This module only (1) feeds it from a prepared recording and keeps its answer next to the inputs'
fingerprints, and (2) seats the day's candidates:
the basic quality gate (talk, the lowest confidence, the candidate-level admission
`selection_scorecard_production_admission`), pass or fail; past it, the content points of the channel's core draws
(`core_draws`) order the seats and every seat needs the asset's content floor; the quota of the recording date (`resolve_quota_grant`, the dated authority asset); the soft per-recording
limit that slack refills.  The numbers that are not read from those modules are the `discover` rules of lane_rules.json.
A candidate that is not seated keeps the reason.  Every import of the old pipeline is in this file (import_boundary.json).

A channel whose draws asset names stories and roles (core_draws.has_stories;) is selected for what happens
between the host and a particular person, not for the game being played: the recall is told the channel's gates and
kinds of story (`core_draws.focus`) and given what helps to read the raw recognition (`discover_aids`); every danmaku
burst is numbered and accounted for (`_account_bursts`: in a candidate, or why not); two gates pass or fail (about the
host; a viewer of the circle can follow it); the content points still order the seats, the story and role only saying
where a draw is must-cut or counts (core_draws `must_cut_when`, `points_when`); candidates of one evening on the same
matter take one seat together (`topic`, set by discover_topics, joined into one clip by discover).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from src.autoslice.danmaku_evidence import (
    BURST_HINT_AFTER_MS,
    BURST_LEAD_MS,
    DanmakuItem,
    burst_hint_lines,
    SessionDensity,
    find_session_bursts,
    laughs,
    load_danmaku_xml,
    session_density,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.selection_scorecard import (
    selection_rank_key,
    selection_scorecard_is_valid,
    selection_scorecard_production_admission,
)
from src.autoslice.talk_quota_authority import resolve_quota_grant

from . import core_draws, discover_aids, discover_voices
from .discover_media import DiscoverError, Prepared, is_date, write_json
from .gates import CLIP_MAX_SECONDS
from .lane_rules import rules
from .srt_text import parse_srt

# v2 : bursts against the session's density, each with its hint, settled by the model's word and the time, asked
# about again; a v1 recall's bursts were against its own recording and are not read back
RECALL_SCHEMA = "manual-lane.discover-recall.v2"
_SEGMENT_STAMP = re.compile(r"_\d{4}(\d{2})(\d{2})-(\d{2})-(\d{2})-\d{2}$")
_SLUG_MAX = 41                                   # stage_package.member_slug: the package slug is [a-z0-9][a-z0-9-]{0,40}
Selector = Callable[..., tuple[list[Any], dict[str, Any]]]

# why a candidate was not seated (the ledger's `reason`)
NOT_TALK = "NOT_TALK"
BELOW_MIN_CONFIDENCE = "BELOW_MIN_CONFIDENCE"
BELOW_MIN_CONTENT = "BELOW_MIN_CONTENT"
ADMISSION_HOLD = "ADMISSION_HOLD"
OVERLAPS_SEATED = "OVERLAPS_SEATED"
QUOTA_FULL = "QUOTA_FULL"
EXTRA_SEAT_BELOW_SCORE = "EXTRA_SEAT_BELOW_SCORE"
OWNER_MARK = "OWNER_MARK"                        # seated because the owner marked it
MUST_CUT = "MUST_CUT"                            # seated past the quality gate: a must-cut core draw (collab, sajiao)
NOT_ABOUT_HOST = "NOT_ABOUT_HOST"                # core_draws.gate: someone else's matter, the host only looks on
NOT_UNDERSTOOD = "NOT_UNDERSTOOD"                # core_draws.gate: a viewer of the circle who missed the stream cannot follow it
NO_PEAK = "NO_PEAK"                              # core_draws.gate: a story that needs its key moment has none
SAME_TOPIC = "SAME_TOPIC"                        # the evening's other candidate on the same matter takes the seat for both
# what became of a danmaku burst of the recording (recall.json `bursts`, the ledger)
BURST_IN_CANDIDATE, BURST_NOT_SELECTED, BURST_UNANSWERED = "IN_CANDIDATE", "NOT_SELECTED", "UNANSWERED"
BURSTS_UNACCOUNTED = "BURSTS_UNACCOUNTED"         # a recording's failure: a burst neither in a candidate nor explained, asked twice
BURST_REASK_FAILED = "BURST_REASK_FAILED"         # a recording's failure: the second question about a burst was not answered
BURST_REASK_BEFORE_MS = 180_000                  # the lines before a burst the second question gives (its cause is before it)
# what became of a burst on the day (the funnel, `burst_funnel`): not nominated (the model's word, or none), then how far the
# candidate holding it got
BURST_GATED = "GATED"                             # its candidate failed a gate (the reason is the candidate's rejection)
BURST_PAST_GATE = "PAST_GATE"                     # its candidate passed the gates and got no seat (QUOTA_FULL, ...)
BURST_SEATED = "SEATED"
BURST_MADE = "MADE"                               # its candidate is a clip that exists
SHADOWS = ("users", "laughs")                     # the two shadow counts (20-selection.md 链路 2「影子」): recorded only
OWNER_MARK_ALREADY_BUILT = "OWNER_MARK_ALREADY_BUILT"
OWNER_MARK_NOT_TALK = "OWNER_MARK_NOT_TALK"


class RecallCallError(RuntimeError):
    """The model call of a recall failed (the segment is left for the next run)."""


class Recall(TypedDict):
    schema: str
    inputs: dict[str, str | None]
    source: dict[str, str | int]
    input_pool: list[dict[str, Any]]
    diagnostics: dict[str, Any]


def default_selector() -> Selector:
    from src.autoslice.semantic_candidate_selector import select_semantic_session_candidates_covered

    return select_semantic_session_candidates_covered


def lane_cid(candidate_id: str, segment_name: str) -> str:
    """The clip id of a candidate.  The selector's id is made from the interval alone (`semantictalk_<start>_<end>`), which two
    recordings can share; the recording's start (month, day, hour, minute of its file name) is added so ids never meet
    across recordings or days, and the id still fits the package slug."""
    stamp = _SEGMENT_STAMP.search(segment_name)
    if stamp is None:
        raise DiscoverError("SEGMENT_NAME_UNRECOGNISED", f"{segment_name} does not end in _<YYYYMMDD>-<HH>-<MM>-<SS>")
    return fits_slug(f"{candidate_id}_{''.join(stamp.groups())}".lower())


def fits_slug(cid: str) -> str:
    """`cid`, when it can be a package slug."""
    if len(cid) > _SLUG_MAX or not re.fullmatch(r"[a-z0-9][a-z0-9_]*", cid):
        raise DiscoverError("CLIP_ID_UNUSABLE", f"{cid} does not fit the package slug rule")
    return cid


def _inputs(prepared: Prepared) -> dict[str, str | None]:
    return {"source_fp": prepared["source_fp"], "xml_sha256": prepared["xml"]["sha256"], "srt_sha256": prepared["srt_sha256"]}


def _source_facts(prepared: Prepared) -> dict[str, str | int]:
    return {"date": prepared["date"], "name": prepared["segment"], "path": prepared["path"], "size": prepared["size"],
            "mtime": prepared["mtime"], "directory": prepared["directory"]}


def load_recall(directory: Path, prepared: Prepared) -> Recall | None:
    """The recall kept in `directory` if it was made from exactly these inputs."""
    path = directory / "recall.json"
    if not path.exists():
        return None
    doc: Recall = json.loads(path.read_text(encoding="utf-8"))
    return doc if doc.get("schema") == RECALL_SCHEMA and doc["inputs"] == _inputs(prepared) else None


def _entry(candidate: Any, source: dict[str, str | int], diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    """One input_pool entry, in the shape of a selection result (clip_init.SelectionEntry reads it)."""
    cid = candidate.anchor.candidate_id
    return {"cid": cid, "candidate_id": cid, "source": source, "kind": candidate.content_type_hint,
            "start_ms": candidate.boundary.resolved_start_ms, "end_ms": candidate.boundary.resolved_end_ms,
            "confidence": candidate.boundary.start_boundary_score, "hook": diagnostics["hooks"].get(cid, ""),
            "selection_scorecard": diagnostics["scorecards"].get(cid), "cue_ids": [x.cue_id for x in candidate.cues],
            "text": "\n".join(x.text for x in candidate.cues)}


OWNER_MARKS_FILE = "owner_marks.json"        # profiles/<id>/: the accounts whose marks in the danmaku are clip requests
OWNER_MARK_NOT_COVERED = "OWNER_MARK_NOT_COVERED"


def owner_mark_rules() -> dict[str, Any] | None:
    """The channel's owner accounts and mark words, or None when the channel has none (the feature is off)."""
    from .channel import profile

    path = profile().manifest_path.parent / OWNER_MARKS_FILE
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    mids, words = doc.get("owner_mids"), doc.get("mark_text_contains")
    if (not isinstance(mids, list) or not mids or not all(isinstance(m, int) and m > 0 for m in mids)
            or not isinstance(words, list) or not words or not all(isinstance(w, str) and w for w in words)
            or not all(isinstance(doc.get(k), int) and doc[k] >= 0 for k in ("before_ms", "after_ms"))):
        raise DiscoverError("OWNER_MARKS_INVALID", f"{path}: owner_mids, mark_text_contains, before_ms, after_ms")
    return doc


def owner_marks(xml: Path, marks_rules: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Danmaku an owner account sent with a mark word (弹幕里标记的那一段，流水线要自己切出来),
    oldest first.  BililiveRecorder writes the sender's uid as the 7th field of `p`."""
    if marks_rules is None or not xml.exists():
        return []
    from xml.etree import ElementTree

    mids = {int(m) for m in marks_rules["owner_mids"]}
    try:
        root = ElementTree.fromstring(xml.read_bytes())
    except ElementTree.ParseError:                  # the recall gets no hints from it either (recall_segment)
        return []
    marks = []
    for node in root.iter("d"):
        fields, text = (node.get("p") or "").split(","), (node.text or "").strip()
        try:
            at_ms, mid = round(float(fields[0]) * 1000), int(fields[6])
        except (IndexError, ValueError):
            continue
        if mid in mids and any(word in text for word in marks_rules["mark_text_contains"]):
            marks.append({"at_ms": at_ms, "text": text, "mid": mid})
    return sorted(marks, key=lambda m: m["at_ms"])


def _marked(entry: Mapping[str, Any], at_ms: int, after_ms: int) -> bool:
    """A mark is sent while the moment is on or soon after it: the candidate holds it, or ended at most `after_ms` before."""
    return int(entry["start_ms"]) <= at_ms <= int(entry["end_ms"]) + after_ms


def _attach_owner_marks(doc: Recall, cues: Sequence[SourceCue] | None, xml: Path, directory: Path, *,
                        ask: Callable[[str], str], selector: Selector | None) -> Recall:
    """Tie every owner mark of the recording to a candidate of its pool.  A mark no recalled candidate holds gets a recall of
    its own over the cues around it (`before_ms` before, `after_ms` after); one still not held is kept as not covered,
    and seating reports it.  Written back next to the recall; read back while the marks are the same."""
    marks_rules = owner_mark_rules()
    marks = owner_marks(xml, marks_rules)
    if doc.get("owner_marks", {}).get("marks") == marks:
        return doc
    pool = list(doc["input_pool"])
    for entry in pool:
        entry.pop("owner_mark", None)
    tied = []
    for mark in marks:
        holders = [e for e in pool if _marked(e, mark["at_ms"], int(marks_rules["after_ms"]))] if marks_rules else []
        if not holders and marks_rules:
            if cues is None:
                cues = _cues(directory)
            lo, hi = mark["at_ms"] - int(marks_rules["before_ms"]), mark["at_ms"] + int(marks_rules["after_ms"])
            near = [c for c in cues if c.source_end_ms > lo and c.source_start_ms < hi]
            if near:
                found, diagnostics = (selector or default_selector())(
                    near, llm_call=ask, max_candidates=3, danmaku_hints=None, danmaku_xml=xml if xml.exists() else None)
                known = {e["cid"] for e in pool}
                for candidate in found:
                    entry = _entry(candidate, doc["source"], diagnostics)
                    if entry["cid"] not in known and _marked(entry, mark["at_ms"], int(marks_rules["after_ms"])):
                        pool.append(entry)
                        holders.append(entry)
        if holders:                                  # the one holding the mark itself before one that ended just before it
            best = min(holders, key=lambda e: (not int(e["start_ms"]) <= mark["at_ms"] <= int(e["end_ms"]),
                                                abs(int(e["end_ms"]) - mark["at_ms"])))
            best["owner_mark"] = dict(mark)
        tied.append({**mark, "candidate_id": best["candidate_id"] if holders else None})
    doc = Recall(**{**doc, "input_pool": pool})
    doc["owner_marks"] = {"marks": marks, "tied": tied}  # type: ignore[typeddict-unknown-key]
    write_json(directory / "recall.json", doc)
    return doc


def _cues(directory: Path) -> list[SourceCue]:
    return [SourceCue(cue_id=str(c["id"]), source_start_ms=round(c["start_s"] * 1000), source_end_ms=round(c["end_s"] * 1000),
                      text=c["text"]) for c in parse_srt((directory / "bcut.srt").read_text(encoding="utf-8"))]


def counted(item: DanmakuItem) -> bool:
    """A danmaku that counts toward a burst: typed by a viewer (not a platform 口令) and not a song call or name chant
    (the channel's request).  Gifts and entries are not danmaku in the recorder's file at all."""
    from src.autoslice.semantic_candidate_selector import chat_is_cheer

    return not item.machine and not chat_is_cheer(item.text)


def _danmaku(directory: Path) -> list[DanmakuItem] | None:
    """The recording's danmaku, or None when it has no readable file (the selector says so in its diagnostics)."""
    xml = directory / "source.xml"
    if not xml.exists():
        return None
    try:
        return load_danmaku_xml(xml)
    except ValueError:
        return None


def session_danmaku_density(directories: Sequence[Path]) -> SessionDensity | None:
    """The day's own danmaku density over every prepared recording of it (danmaku_evidence.session_density): bursts are
    the densest half minutes of this broadcast, not of each half-hour file, and not a number shared by every broadcast."""
    return session_density([items for d in directories if (items := _danmaku(d)) is not None], counted)


def _shadow_counted(kind: str) -> Callable[[DanmakuItem], bool]:
    return counted if kind == "users" else lambda item: counted(item) and laughs(item)


def session_shadow_density(directories: Sequence[Path]) -> dict[str, SessionDensity | None]:
    """The day's density by the two shadow counts (`SHADOWS`): the viewers who sent a counted danmaku in the half minute,
    and the counted danmaku that laugh (danmaku_evidence.LAUGH) -- the same threshold rule as the bursts'."""
    recordings = [items for d in directories if (items := _danmaku(d)) is not None]
    return {kind: session_density(recordings, _shadow_counted(kind), per_user=kind == "users") for kind in SHADOWS}


def shadow_bursts(directory: Path, densities: Mapping[str, SessionDensity | None], bursts: Sequence[Mapping[str, Any]],
                  entries: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The bursts the shadow counts find in one recording, written down to be weighed on later broadcasts and nothing
    else: the model is not told of them and the seating does not see them.  Each: its time and count, whether a burst of
    the recording overlaps it (`main`), and the candidate that holds it by the time (`candidate_id`, `_holds`)."""
    items = _danmaku(directory)
    found: dict[str, list[dict[str, Any]]] = {}
    for kind in SHADOWS:
        density = densities.get(kind)
        found[kind] = [] if items is None or density is None else [
            {"start_ms": b.start_ms, "end_ms": b.end_ms, "count": b.count, "threshold": density.threshold,
             "main": any(b.start_ms < int(m["end_ms"]) and int(m["start_ms"]) < b.end_ms for m in bursts),
             "candidate_id": next((e["candidate_id"] for e in entries
                                   if _holds(e, {"start_ms": b.start_ms, "end_ms": b.end_ms})), None)}
            for b in find_session_bursts(items, threshold=density.threshold, counted=_shadow_counted(kind),
                                         per_user=kind == "users")]
    return found


def recall_segment(prepared: Prepared, directory: Path, *, ask: core_draws.Ask, density: SessionDensity | None,
                   selector: Selector | None = None, hear: discover_voices.Hear | None = None) -> Recall:
    """The candidates of one prepared recording (`ask` is the model call: prompt in, answer text out; it raises when the call
    fails).  A recall made from the same inputs is read back, not made again; the answer is written next to them.
    `density` is the session's danmaku density (`session_danmaku_density`, None when no recording of the day has
    danmaku): every half minute of this recording at its threshold is a burst the recall must account for
    (`_settle_bursts`); one it leaves unaccounted is asked about again (`_reask_bursts`).  The owner's marks
    are tied to candidates on top of either (`_attach_owner_marks`).  `hear` reads which of the candidates' lines sound
    like the host for the labelling call of a channel with stories (discover_voices)."""
    xml = directory / "source.xml"
    if (kept := load_recall(directory, prepared)) is not None:
        doc = _reask_bursts(_settle_bursts(kept, directory), None, directory, ask=ask, selector=selector)
        return _with_draws(_settle_bursts(_attach_owner_marks(doc, None, xml, directory, ask=ask, selector=selector), directory),
                           prepared, directory, ask, hear)
    cues = _cues(directory)
    hints: str | None = None
    numbered: list[tuple[str, Any, str]] = []
    items = _danmaku(directory)
    if items is not None and density is not None:
        numbered = burst_hint_lines(items, find_session_bursts(items, threshold=density.threshold, counted=counted),
                                    baseline=density.baseline)
        hints = "\n".join(line for _, _, line in numbered) or None
    candidates, diagnostics = (selector or default_selector())(
        cues, llm_call=ask, max_candidates=int(rules("discover")["recall_max_candidates"]),
        danmaku_hints=hints, danmaku_xml=xml if xml.exists() else None, **_focus(cues, items))
    source = _source_facts(prepared)
    pool = [_entry(c, source, diagnostics) for c in candidates]
    doc = Recall(schema=RECALL_SCHEMA, inputs=_inputs(prepared), source=source, input_pool=pool, diagnostics=diagnostics)
    doc["bursts"] = [{"id": burst_id, "start_ms": burst.start_ms, "end_ms": burst.end_ms, "count": burst.count,  # type: ignore[typeddict-unknown-key]
                      "baseline": density.baseline, "threshold": density.threshold, "hint": line}  # type: ignore[union-attr]
                     for burst_id, burst, line in numbered]
    doc = _reask_bursts(_settle_bursts(doc, directory), cues, directory, ask=ask, selector=selector)
    return _with_draws(_settle_bursts(_attach_owner_marks(doc, cues, xml, directory, ask=ask, selector=selector), directory),
                       prepared, directory, ask, hear)


def _focus(cues: Sequence[SourceCue], items: Sequence[DanmakuItem] | None) -> dict[str, Any]:
    """What a channel with stories also gives every recall of a recording: its gates and kinds of story, and what helps to
    read the raw recognition (discover_aids)."""
    if not core_draws.has_stories():
        return {}
    return {"selection_focus": core_draws.focus(),
            "reading_aids": discover_aids.reading_aids("\n".join(c.text for c in cues), [i.text for i in items or []])}


def _holds(entry: Mapping[str, Any], burst: Mapping[str, Any]) -> bool:
    """Whether a candidate holds a burst: it overlaps the burst, or ends at most `BURST_LEAD_MS` before it -- the danmaku
    that explain a joke or laugh at it come 5 to 60 seconds after the line (the task brief of)."""
    return int(entry["start_ms"]) < int(burst["end_ms"]) and int(burst["start_ms"]) - BURST_LEAD_MS < int(entry["end_ms"])


def _answers(doc: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The model's word on each burst: the recall's, then the second question's (`burst_reask`)."""
    return {**(doc["diagnostics"].get("burst_answers") or {}), **(doc.get("burst_reask", {}).get("answers") or {})}


def _settle_bursts(doc: Recall, directory: Path) -> Recall:
    """What became of every danmaku burst of the recording (the hand-made candidate table's rule 「每个爆发窗口要么入选、
    要么写明不选理由」, now the automatic selection's too).  The model's word decides, the time checks it: a burst it says is in
    none of its candidates (`selected` false) is `NOT_SELECTED` with its reason, whatever candidate is near it in time; one
    it says is in a candidate, or does not answer about, is `IN_CANDIDATE` when a candidate of the pool holds it
    (`_holds`), else `UNANSWERED` -- a claimed candidate that is not there is no reason for leaving the burst.  Every
    candidate holding an `IN_CANDIDATE` burst is marked with it (`bursts`: the seating gives it the asset's
    `burst_points`).  `reask` says what became of its second question (`_reask_bursts`).  Written back next to the recall."""
    bursts = doc.get("bursts") or []
    answers = _answers(doc)
    reask = doc.get("burst_reask") or {}
    for burst in bursts:
        answer = answers.get(burst["id"])
        declined = answer is not None and not answer["selected"]
        holder = None if declined else next((e for e in doc["input_pool"] if _holds(e, burst)), None)
        burst.update(disposition=BURST_NOT_SELECTED if declined else BURST_IN_CANDIDATE if holder else BURST_UNANSWERED,
                     candidate_id=holder["candidate_id"] if holder else None,
                     selected=None if answer is None else bool(answer["selected"]),
                     reason=str(answer["reason"]) if answer else "",
                     reask=next((state for state, key in (("NO_LINES", "no_lines"), ("ASKED", "asked"), ("FAILED", "failed"))
                                 if burst["id"] in (reask.get(key) or ())), None))
    for entry in doc["input_pool"]:
        entry.pop("bursts", None)
        if held := [b["id"] for b in bursts if b["disposition"] == BURST_IN_CANDIDATE and _holds(entry, b)]:
            entry["bursts"] = held
    write_json(directory / "recall.json", doc)
    return doc


def _reask_bursts(doc: Recall, cues: Sequence[SourceCue] | None, directory: Path, *, ask: Callable[[str], str],
                  selector: Selector | None) -> Recall:
    """Ask again about every burst the recall left unaccounted (`UNANSWERED`): a recall over the lines from
    `BURST_REASK_BEFORE_MS` before it to a minute after, with that burst alone in its hints.  A candidate it finds that
    holds the burst joins the pool; its word on the burst is kept (`burst_reask`).  A burst is asked once (`asked`); one
    without any line around it cannot be asked (`no_lines`); one whose question the model did not answer (the call failed
    or its answer had no candidates) is left for the next round (`failed`, the recording's `BURST_REASK_FAILED`) -- the
    recall's candidates stay in the pool either way.  One still unaccounted after it is the recording's failure
    (`BURSTS_UNACCOUNTED`, discover)."""
    from src.autoslice.semantic_candidate_selector import LlmCallError   # the recall's answer that cannot be read

    kept = doc.get("burst_reask") or {}
    asked, no_lines = list(kept.get("asked") or []), list(kept.get("no_lines") or [])
    todo = [b for b in doc.get("bursts") or [] if b["disposition"] == BURST_UNANSWERED and b["id"] not in asked]
    if not todo:
        return doc
    if cues is None:
        cues = _cues(directory)
    focus = _focus(cues, _danmaku(directory))
    xml = directory / "source.xml"
    pool, known = list(doc["input_pool"]), {e["cid"] for e in doc["input_pool"]}
    answers, failed = dict(kept.get("answers") or {}), {}
    for burst in todo:
        lo, hi = int(burst["start_ms"]) - BURST_REASK_BEFORE_MS, int(burst["end_ms"]) + BURST_HINT_AFTER_MS
        near = [c for c in cues if c.source_end_ms > lo and c.source_start_ms < hi]
        if not near:
            asked.append(burst["id"])
            no_lines.append(burst["id"])
            continue
        try:
            found, diagnostics = (selector or default_selector())(
                near, llm_call=ask, max_candidates=3, danmaku_hints=str(burst["hint"]),
                danmaku_xml=xml if xml.exists() else None, **focus)
        except (RecallCallError, LlmCallError) as exc:   # this burst's question, not the recall: its candidates stay
            failed[burst["id"]] = f"{type(exc).__name__}: {str(exc)[:300]}"
            continue
        asked.append(burst["id"])
        for candidate in found:
            entry = _entry(candidate, doc["source"], diagnostics)
            if entry["cid"] not in known and _holds(entry, burst):
                pool.append(entry)
                known.add(entry["cid"])
        if burst["id"] in (diagnostics.get("burst_answers") or {}):
            answers[burst["id"]] = diagnostics["burst_answers"][burst["id"]]
    doc = Recall(**{**doc, "input_pool": pool})
    doc["burst_reask"] = {"asked": asked, "answers": answers, "no_lines": no_lines, "failed": failed}  # type: ignore[typeddict-unknown-key]
    return _settle_bursts(doc, directory)


def _with_draws(doc: Recall, prepared: Prepared, directory: Path, ask: core_draws.Ask,
                hear: discover_voices.Hear | None) -> Recall:
    """The core draws of every candidate in the pool (one call for the ones not yet labelled), written back next to the
    recall.  A failed call or answer raises: the recording is left for the next run, its recall is kept.  A channel with
    stories has its candidates' lines marked by the host's voiceprint first, when `hear` is given (what it heard, or that
    the channel has no voiceprint workspace, is kept in recall.json `voiceprint`; a reading that fails raises like a
    failed call, discover_voices)."""
    if core_draws.pending(doc["input_pool"]):
        voices = None
        cues = None
        if core_draws.has_stories():
            timed = _cues(directory)
            cues = {c.cue_id: (c.source_start_ms, c.text) for c in timed}
            if hear is not None:
                wanted = {str(c) for e in doc["input_pool"] if e.get("kind") == "talk" for c in e["cue_ids"]}
                heard = hear(prepared, directory, {c.cue_id: (c.source_start_ms, c.source_end_ms) for c in timed if c.cue_id in wanted})
                voices = heard["host_prob"]
                doc["voiceprint"] = {"note": heard["note"], "lines": len(voices or {})}  # type: ignore[typeddict-unknown-key]
        core_draws.label(doc["input_pool"], ask, cues, voices)
        write_json(directory / "recall.json", doc)
    return doc


@dataclass(frozen=True)
class Seating:
    seated: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    policy: dict[str, Any]


def _span(entry: Mapping[str, Any]) -> tuple[str, int, int]:
    return str(entry["source"]["name"]), int(entry["start_ms"]), int(entry["end_ms"])


def _spans(entry: Mapping[str, Any]) -> list[tuple[str, int, int]]:
    """The stretches of recording a seated candidate uses: its own, or those of every candidate it is joined with."""
    return [_span(member) for member in entry.get("topic_members") or [entry]]


def _length_ms(entry: Mapping[str, Any]) -> int:
    return sum(end - start for _, start, end in _spans(entry))


def _with_lead_in(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """A candidate given the lines before it (`lead_in`, discover_lead_in) is made as one clip with them, in broadcast
    order, like one matter's candidates (`topic_members`; discover joins them)."""
    if not entry.get("lead_in"):
        return entry
    return dict(entry, topic_members=sorted([*entry["lead_in"], entry], key=_span),
                topic_theme=entry.get("topic_theme") or str(entry.get("hook", "")),
                topic_why=entry.get("topic_why") or str(entry["lead_in"][0].get("hook", "")))


def _overlaps(entry: Mapping[str, Any], taken: Sequence[tuple[str, int, int]]) -> bool:
    return any(name == n and start < e and s < end for name, start, end in _spans(entry) for n, s, e in taken)


def _fold_topics(marked: list[Mapping[str, Any]], ranked: list[Mapping[str, Any]], reject: Callable[..., None],
                 unleading: set[str]) -> None:
    """One seat for the evening's candidates on the same matter (维护者, of two clips on who is whose pet made
    twenty minutes apart: 同话题的两条应合并成一条).  `topic` is set by discover_topics on candidates
    that go on from one another's conclusion.  The first of a topic in seating order (an owner's mark, else a must-cut
    draw that takes no seat, else one that takes a seat, else the best rank) stands for all of them and carries them in
    broadcast order (`topic_members`): discover joins them into one clip.  The others give up their own seat; a marked
    one keeps its own.  A clip is at most `gates.CLIP_MAX_SECONDS` long: members are taken in seating order while the
    joined clip stays within it, and one that no longer fits is not made at all (the matter already has its clip).
    A candidate in `unleading` led its topic in an earlier pass and was not seated (seat_day): it is left out of the
    topic, which the next of its members leads."""
    by_topic: dict[str, list[Mapping[str, Any]]] = {}
    cut = [e for e in ranked if core_draws.must_cut(e)]
    for entry in [*marked, *(e for e in cut if not core_draws.takes_seat(e)), *(e for e in cut if core_draws.takes_seat(e)),
                  *(e for e in ranked if not core_draws.must_cut(e))]:
        if entry.get("topic") and str(entry["cid"]) not in unleading:
            by_topic.setdefault(str(entry["topic"]), []).append(entry)
    for members in by_topic.values():
        lead, folded = members[0], [m for m in members[1:] if not any(m is e for e in marked)]
        taken, length_ms = [lead], _length_ms(lead)
        for member in folded:
            ranked[:] = [e for e in ranked if e is not member]
            mine = _length_ms(member)
            if length_ms + mine <= CLIP_MAX_SECONDS * 1000:
                taken.append(member)
                length_ms += mine
                reject(member, SAME_TOPIC, f"same matter as {lead['cid']}: made as one clip with it")
            else:
                reject(member, SAME_TOPIC, f"same matter as {lead['cid']}: not made (joined with it the clip would be "
                                           f"{(length_ms + mine) // 1000} s, over {CLIP_MAX_SECONDS:.0f} s)")
        if len(taken) > 1:
            home = marked if any(lead is e for e in marked) else ranked
            pieces = [piece for e in taken for piece in e.get("topic_members") or [e]]     # a lead-in goes with its candidate
            home[next(i for i, e in enumerate(home) if e is lead)] = dict(lead, topic_members=sorted(pieces, key=_span))


def quality(entry: Mapping[str, Any]) -> float:
    """The scorecard's effective score: only a tie-break among equal content points and the gate of a dated extra seat.
    It is not the quality gate (the channel's request: the gate passes or fails, see `_admitted`)."""
    return float(entry["selection_scorecard"]["effective_score"])


def _rank(entry: Mapping[str, Any]) -> tuple[bool, float, float, float, str]:
    """Among the candidates past the gates: Tier 3 (unreadable / generic) last, then the content points (core draws
    + audience interest, fitted on the channel's views) plus the burst points of one holding a danmaku burst
    (core_draws.burst_points), then the effective score, then confidence."""
    tier, _, confidence, cid = selection_rank_key(entry)
    return tier >= 3, -(core_draws.content(entry) + core_draws.burst_points(entry)), -quality(entry), confidence, cid


def _admitted(pool: Sequence[Mapping[str, Any]], done: set[str], lowest: float,
              reject: Callable[..., None]) -> list[Mapping[str, Any]]:
    """The candidates that may take a seat at all.  The basic quality gate decides only whether a clip can be watched --
    talk, confident enough, admitted by the production admission (watchable, distinctive, producible, with cue evidence)
    -- and passes or fails; it gives no points (the channel's request).  A seat of the day's quota then needs the content floor of the channel's draws asset: the cap is a
    ceiling, not a number to fill (the channel's request).  A must-cut draw (collab, sajiao:
    「撒娇是我想要的片段，这个一定要放」) needs only the basic gate."""
    eligible, least = [], core_draws.floor()
    for entry in pool:
        if entry["cid"] in done:
            continue
        confidence = entry.get("confidence")
        if entry.get("kind") != "talk":
            reject(entry, NOT_TALK, f"kind={entry.get('kind')}: the song lane is not discover's")
        elif not isinstance(confidence, (int, float)) or confidence < lowest:
            reject(entry, BELOW_MIN_CONFIDENCE, f"confidence={confidence}, lowest {lowest}")
        elif (admission := selection_scorecard_production_admission(entry.get("selection_scorecard"))).get("status") != "ADMIT":
            reject(entry, ADMISSION_HOLD, f"{admission.get('reason_code')}: {admission.get('reason', '')}"[:200])
        elif (closed := core_draws.gate(entry)) is not None:      # a channel with stories: its two gates, and the key moment
            reject(entry, *closed)
        elif least is not None and core_draws.content(entry) < least and not core_draws.must_cut(entry):
            reject(entry, BELOW_MIN_CONTENT, f"content points {core_draws.content(entry)}, lowest {least} "
                                             f"(effective_score {quality(entry)} does not count)")
        else:
            eligible.append(entry)
    return eligible


def _seated_cids(seating: Seating) -> set[str]:
    return {str(m["cid"]) for e in seating.seated for m in [e, *(e.get("topic_members") or [])]}


def burst_swaps(date: str, pool: Sequence[Mapping[str, Any]], built: Sequence[Mapping[str, Any]],
                seating: Seating) -> dict[str, list[str]]:
    """What the burst points changed (`core_draws.burst_points`): the same day seated again with no candidate holding a
    burst.  `in` took a seat only with them, `out` gave its seat up to one of those."""
    if not any(e.get("bursts") for e in pool):
        return {"in": [], "out": []}
    plain = seat_day(date, [{k: v for k, v in e.items() if k != "bursts"} for e in pool], built)
    now, then = _seated_cids(seating), _seated_cids(plain)
    return {"in": sorted(now - then), "out": sorted(then - now)}


def burst_funnel(segments: Sequence[Mapping[str, Any]], pool: Sequence[Mapping[str, Any]], seating: Seating,
                 made: set[str]) -> list[dict[str, Any]]:
    """How far every danmaku burst of the day got: found, nominated (`IN_CANDIDATE`; else the model's word
    `NOT_SELECTED` and its reason, or `UNANSWERED`), past the gates, seated, made (`made`: the cids of the clips that exist,
    and of the candidates joined into them).  A burst several candidates hold goes as far as the furthest of them."""
    seated = _seated_cids(seating)
    why = {str(r["cid"]): r for r in seating.rejected}

    def reach(entry: Mapping[str, Any]) -> tuple[int, str, str]:
        cid = str(entry["cid"])
        if cid in made:
            return 4, BURST_MADE, ""
        if cid in seated:
            return 3, BURST_SEATED, ""
        rejection = why.get(cid, {})
        past = rejection.get("reason") in (QUOTA_FULL, OVERLAPS_SEATED, SAME_TOPIC, EXTRA_SEAT_BELOW_SCORE)
        return (2 if past else 1), BURST_PAST_GATE if past else BURST_GATED, str(rejection.get("reason", ""))

    rows = []
    for rec in segments:
        for burst in rec.get("bursts") or []:
            row = {"segment": rec["name"], "id": burst["id"], "start_ms": burst["start_ms"], "end_ms": burst["end_ms"],
                   "count": burst["count"], "stage": burst["disposition"], "cid": None, "why": burst.get("reason", "")}
            holders = [e for e in pool if _span(e)[0] == rec["name"] and burst["id"] in (e.get("bursts") or [])]
            if burst["disposition"] == BURST_IN_CANDIDATE and holders:
                _, stage, reason = max(reach(e) for e in holders)
                best = max(holders, key=reach)
                row.update(stage=stage, cid=best["cid"], why=reason)
            rows.append(row)
    return rows


def seat_day(date: str, pool: Sequence[Mapping[str, Any]], built: Sequence[Mapping[str, Any]]) -> Seating:
    """Which of the day's candidates (`pool`: the entries of every recording, each with its `cid`) take the seats still free
    on `date`.  `built` are the clips already made for the date (cid, segment, start_ms, end_ms): they keep their seats,
    are never changed, and the candidates they came from are not seated again.

    The members of a topic give up their seats to the one leading it (_fold_topics) only when it is seated: a lead that
    is not (the seats are full, or it overlaps a seated clip) is left out of its topic and the day is seated again, the
    next member leading -- a must-cut member is never lost behind a lead that did not get in."""
    unleading: set[str] = set()
    while True:
        seating = _seat_once(date, pool, built, unleading)
        seated = {str(e["cid"]) for e in seating.seated}
        failed = {str(e["cid"]) for e in pool if e.get("topic") and str(e["cid"]) not in seated | unleading
                  and any(r["cid"] == e["cid"] and r["reason"] != SAME_TOPIC for r in seating.rejected)
                  and any(r["reason"] == SAME_TOPIC and r["detail"].startswith(f"same matter as {e['cid']}:") for r in seating.rejected)}
        if not failed:
            return seating
        unleading |= failed


def _seat_once(date: str, pool: Sequence[Mapping[str, Any]], built: Sequence[Mapping[str, Any]], unleading: set[str]) -> Seating:
    pool = [_with_lead_in(e) for e in pool]
    if not is_date(date):
        raise DiscoverError("BAD_DATE", f"{date!r} is not YYYY-MM-DD")
    cfg = rules("discover")
    grant = resolve_quota_grant(date, "talk", default_cap=int(cfg["base_talk_seats"]))
    slots = max(0, grant.cap - len(built))
    policy = {"scope": "talk", "cap": grant.cap, "extra_slot_min_score": grant.extra_slot_min_score,
              "policy_source": grant.source, "built": len(built), "free_seats": slots}
    rejected: list[dict[str, Any]] = []

    def reject(entry: Mapping[str, Any], reason: str, detail: str = "") -> None:
        name, start, end = _span(entry)
        rejected.append({"cid": entry["cid"], "candidate_id": entry["candidate_id"], "segment": name, "start_ms": start,
                         "end_ms": end, "reason": reason, "detail": detail, "hook": str(entry.get("hook", ""))[:80]})

    # a built clip joined from several candidates (`covers`) holds every one of them: none is seated again
    done = {str(b["cid"]) for b in built} | {str(c["cid"]) for b in built for c in b.get("covers", [])}
    taken = [(str(c["segment"]), int(c["start_ms"]), int(c["end_ms"])) for b in built for c in b.get("covers") or [b]]
    per_segment = Counter(str(b["segment"]) for b in built)
    seated: list[dict[str, Any]] = []
    # The owner's marks first (弹幕里标记的那一段，流水线要自己切出来): the marked candidate is made whatever its score,
    # confidence or the day's quota; it does not take one of the quota's seats.  A clip already built over it is enough.
    marked: list[Mapping[str, Any]] = [e for e in pool if e.get("owner_mark") and str(e["cid"]) not in done]
    ranked = sorted(_admitted([e for e in pool if not any(e is m for m in marked)], done, float(cfg["min_confidence"]), reject),
                    key=_rank)
    _fold_topics(marked, ranked, reject, unleading)
    for entry in marked:
        if _overlaps(entry, taken):
            reject(entry, OWNER_MARK_ALREADY_BUILT, "a built or seated clip of the same recording already covers the mark")
        elif entry.get("kind") != "talk":
            reject(entry, OWNER_MARK_NOT_TALK, f"kind={entry.get('kind')}: marked, but the song lane is not discover's")
        else:
            seated.append(dict(entry, seat_reason=OWNER_MARK))
            taken.extend(_spans(entry))
    policy["owner_marked"] = len(seated)



    seated_cut = [e for e in ranked if core_draws.must_cut(e)]
    for entry in seated_cut:
        ranked.remove(entry)
    for entry in [e for e in seated_cut if not core_draws.takes_seat(e)]:
        if _overlaps(entry, taken):
            reject(entry, OVERLAPS_SEATED, "overlaps a seated or built candidate of the same recording")
        else:
            seated.append(dict(entry, seat_reason=MUST_CUT, must_cut=core_draws.must_cut(entry)))
            taken.extend(_spans(entry))
    owner_seats = len(seated)                       # marks and seat-free must-cut draws take no seat of the day's quota
    policy["must_cut"] = owner_seats - policy["owner_marked"]
    for entry in [e for e in seated_cut if core_draws.takes_seat(e)]:      # already in rank order: content points first
        if _overlaps(entry, taken):
            reject(entry, OVERLAPS_SEATED, "overlaps a seated or built candidate of the same recording")
        elif len(seated) - owner_seats >= slots:
            reject(entry, QUOTA_FULL, f"must-cut {core_draws.must_cut(entry)} takes a seat: {grant.cap} seats on {date} "
                                      f"({grant.source}), {len(built)} already built")
        else:
            seated.append(dict(entry, seat_reason=MUST_CUT, must_cut=core_draws.must_cut(entry)))
            taken.extend(_spans(entry))
            per_segment[_span(entry)[0]] += 1
    policy["must_cut_seated"] = len(seated) - owner_seats

    def extra_seat_ok(entry: Mapping[str, Any]) -> bool:
        gate = grant.extra_slot_min_score          # seats past base_talk_seats exist only where a dated authority made them
        if gate is None or len(built) + len(seated) - owner_seats + 1 <= int(cfg["base_talk_seats"]):
            return True
        return bool(selection_scorecard_is_valid(entry.get("selection_scorecard")) and quality(entry) >= gate)

    def seat(entry: Mapping[str, Any]) -> None:
        seated.append(dict(entry))
        taken.extend(_spans(entry))
        per_segment[_span(entry)[0]] += 1

    deferred = []
    for entry in ranked:
        if _overlaps(entry, taken):
            reject(entry, OVERLAPS_SEATED, "overlaps a seated or built candidate of the same recording")
        elif len(seated) - owner_seats < slots and per_segment[_span(entry)[0]] < int(cfg["per_segment_soft_cap"]) and extra_seat_ok(entry):
            seat(entry)
        else:
            deferred.append(entry)
    for entry in deferred:                           # the per-recording limit is soft: slack is refilled in rank order
        if _overlaps(entry, taken):
            reject(entry, OVERLAPS_SEATED, "overlaps a seated or built candidate of the same recording")
        elif len(seated) - owner_seats >= slots:
            reject(entry, QUOTA_FULL, f"{grant.cap} seats on {date} ({grant.source}), {len(built)} already built")
        elif extra_seat_ok(entry):
            seat(entry)
        else:
            reject(entry, EXTRA_SEAT_BELOW_SCORE, f"seat {len(built) + len(seated) - owner_seats + 1} needs score >= {grant.extra_slot_min_score}")
    return Seating(seated=seated, rejected=rejected, policy=policy)
