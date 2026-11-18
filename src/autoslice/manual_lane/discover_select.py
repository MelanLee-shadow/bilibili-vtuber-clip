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
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypedDict

from src.autoslice.danmaku_evidence import find_danmaku_bursts, load_danmaku_xml
from src.autoslice.review_evidence import SourceCue
from src.autoslice.selection_scorecard import (
    selection_rank_key,
    selection_scorecard_is_valid,
    selection_scorecard_production_admission,
)
from src.autoslice.talk_quota_authority import resolve_quota_grant

from . import core_draws
from .discover_media import DiscoverError, Prepared, is_date, write_json
from .lane_rules import rules
from .srt_text import parse_srt

RECALL_SCHEMA = "manual-lane.discover-recall.v1"
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
    cid = f"{candidate_id}_{''.join(stamp.groups())}".lower()
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
    """Danmaku an owner account sent with a mark word (「我在弹幕里标记了一处地点…我要自动流水线能切出这一块来」),
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


def recall_segment(prepared: Prepared, directory: Path, *, ask: Callable[[str], str], selector: Selector | None = None) -> Recall:
    """The candidates of one prepared recording (`ask` is the model call: prompt in, answer text out; it raises when the call
    fails).  A recall made from the same inputs is read back, not made again; the answer is written next to them.
    The owner's marks are tied to candidates on top of either (`_attach_owner_marks`)."""
    xml = directory / "source.xml"
    if (kept := load_recall(directory, prepared)) is not None:
        return _with_draws(_attach_owner_marks(kept, None, xml, directory, ask=ask, selector=selector), directory, ask)
    cues = _cues(directory)
    hints: str | None = None
    if xml.exists():
        try:
            hints = "\n".join(str(asdict(b)) for b in find_danmaku_bursts(load_danmaku_xml(xml)))
        except ValueError:                       # unparseable: the selector says so in its diagnostics, it gets no hints
            hints = None
    candidates, diagnostics = (selector or default_selector())(
        cues, llm_call=ask, max_candidates=int(rules("discover")["recall_max_candidates"]),
        danmaku_hints=hints, danmaku_xml=xml if xml.exists() else None)
    source = _source_facts(prepared)
    pool = [_entry(c, source, diagnostics) for c in candidates]
    doc = Recall(schema=RECALL_SCHEMA, inputs=_inputs(prepared), source=source, input_pool=pool, diagnostics=diagnostics)
    write_json(directory / "recall.json", doc)
    return _with_draws(_attach_owner_marks(doc, cues, xml, directory, ask=ask, selector=selector), directory, ask)


def _with_draws(doc: Recall, directory: Path, ask: Callable[[str], str]) -> Recall:
    """The core draws of every candidate in the pool (one call for the ones not yet labelled), written back next to the
    recall.  A failed call or answer raises: the recording is left for the next run, its recall is kept."""
    if core_draws.label(doc["input_pool"], ask):
        write_json(directory / "recall.json", doc)
    return doc


@dataclass(frozen=True)
class Seating:
    seated: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    policy: dict[str, Any]


def _span(entry: Mapping[str, Any]) -> tuple[str, int, int]:
    return str(entry["source"]["name"]), int(entry["start_ms"]), int(entry["end_ms"])


def _overlaps(entry: Mapping[str, Any], taken: Sequence[tuple[str, int, int]]) -> bool:
    name, start, end = _span(entry)
    return any(name == n and start < e and s < end for n, s, e in taken)


def quality(entry: Mapping[str, Any]) -> float:
    """The scorecard's effective score: only a tie-break among equal content points and the gate of a dated extra seat.
 It is not the quality gate (公开规则：基础质量…决定了能不能看: the gate passes or fails, see `_admitted`)."""
    return float(entry["selection_scorecard"]["effective_score"])


def _rank(entry: Mapping[str, Any]) -> tuple[bool, float, float, float, str]:
    """Among the candidates past the quality gate: Tier 3 (unreadable / generic) last, then the content points (core draws
    + audience interest, fitted on the channel's views), then the effective score, then confidence."""
    tier, _, confidence, cid = selection_rank_key(entry)
    return tier >= 3, -core_draws.content(entry), -quality(entry), confidence, cid


def _admitted(pool: Sequence[Mapping[str, Any]], done: set[str], lowest: float,
              reject: Callable[..., None]) -> list[Mapping[str, Any]]:
    """The candidates that may take a seat at all. The basic quality gate decides only whether a clip can be watched --
 talk, confident enough, admitted by the production admission (watchable, distinctive, producible, with cue evidence)
 -- and passes or fails; it gives no points (公开规则：基础质量一定得先过关之后，我们再看看点好不好看。看点是一个
 单独的分吧). A seat of the day's quota then needs the content floor of the channel's draws asset: the cap is a
 ceiling, not a number to fill (公开规则：只有得高分的才去做成切片). A must-cut draw (collab, sajiao:
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
        elif least is not None and core_draws.content(entry) < least and not core_draws.must_cut(entry):
            reject(entry, BELOW_MIN_CONTENT, f"content points {core_draws.content(entry)}, lowest {least} "
                                             f"(effective_score {quality(entry)} does not count)")
        else:
            eligible.append(entry)
    return eligible


def seat_day(date: str, pool: Sequence[Mapping[str, Any]], built: Sequence[Mapping[str, Any]]) -> Seating:
    """Which of the day's candidates (`pool`: the entries of every recording, each with its `cid`) take the seats still free
    on `date`.  `built` are the clips already made for the date (cid, segment, start_ms, end_ms): they keep their seats,
    are never changed, and the candidates they came from are not seated again."""
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

    done = {str(b["cid"]) for b in built}
    taken = [(str(b["segment"]), int(b["start_ms"]), int(b["end_ms"])) for b in built]
    per_segment = Counter(str(b["segment"]) for b in built)
    seated: list[dict[str, Any]] = []
    # The owner's marks first (「我要自动流水线能切出这一块来」): the marked candidate is made whatever its score,
    # confidence or the day's quota; it does not take one of the quota's seats.  A clip already built over it is enough.
    marked = [e for e in pool if e.get("owner_mark") and str(e["cid"]) not in done]
    for entry in marked:
        if _overlaps(entry, taken):
            reject(entry, OWNER_MARK_ALREADY_BUILT, "a built or seated clip of the same recording already covers the mark")
        elif entry.get("kind") != "talk":
            reject(entry, OWNER_MARK_NOT_TALK, f"kind={entry.get('kind')}: marked, but the song lane is not discover's")
        else:
            seated.append(dict(entry, seat_reason=OWNER_MARK))
            taken.append(_span(entry))
    policy["owner_marked"] = len(seated)
    ranked = sorted(_admitted([e for e in pool if e not in marked], done, float(cfg["min_confidence"]), reject), key=_rank)



    seated_cut = [e for e in ranked if core_draws.must_cut(e)]
    for entry in seated_cut:
        ranked.remove(entry)
    for entry in [e for e in seated_cut if not core_draws.takes_seat(e)]:
        if _overlaps(entry, taken):
            reject(entry, OVERLAPS_SEATED, "overlaps a seated or built candidate of the same recording")
        else:
            seated.append(dict(entry, seat_reason=MUST_CUT, must_cut=core_draws.must_cut(entry)))
            taken.append(_span(entry))
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
            taken.append(_span(entry))
            per_segment[_span(entry)[0]] += 1
    policy["must_cut_seated"] = len(seated) - owner_seats

    def extra_seat_ok(entry: Mapping[str, Any]) -> bool:
        gate = grant.extra_slot_min_score          # seats past base_talk_seats exist only where a dated authority made them
        if gate is None or len(built) + len(seated) - owner_seats + 1 <= int(cfg["base_talk_seats"]):
            return True
        return bool(selection_scorecard_is_valid(entry.get("selection_scorecard")) and quality(entry) >= gate)

    def seat(entry: Mapping[str, Any]) -> None:
        seated.append(dict(entry))
        taken.append(_span(entry))
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
