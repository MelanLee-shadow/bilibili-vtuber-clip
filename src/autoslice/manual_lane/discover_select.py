"""`discover`, the selection side: one recording's candidates, and the day's seats.

The selection logic is the old pipeline's and is called, not rewritten: `select_semantic_session_candidates_covered`
recalls candidates from a recording's whole-recording subtitles (and its danmaku), each with the scorecard the
metric defines.  This module only (1) feeds it from a prepared recording and keeps its answer next to the inputs'
fingerprints, and (2) seats the day's candidates the way the production selection does: hard Tier, then absolute score,
then confidence (`selection_rank_key`); the candidate-level admission (`selection_scorecard_production_admission`); the
lowest confidence; the quota of the recording date (`resolve_quota_grant`, the dated authority asset); the soft per-recording
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
ADMISSION_HOLD = "ADMISSION_HOLD"
OVERLAPS_SEATED = "OVERLAPS_SEATED"
QUOTA_FULL = "QUOTA_FULL"
EXTRA_SEAT_BELOW_SCORE = "EXTRA_SEAT_BELOW_SCORE"


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


def recall_segment(prepared: Prepared, directory: Path, *, ask: Callable[[str], str], selector: Selector | None = None) -> Recall:
    """The candidates of one prepared recording (`ask` is the model call: prompt in, answer text out; it raises when the call
    fails).  A recall made from the same inputs is read back, not made again; the answer is written next to them."""
    if (kept := load_recall(directory, prepared)) is not None:
        return kept
    cues = [SourceCue(cue_id=str(c["id"]), source_start_ms=round(c["start_s"] * 1000), source_end_ms=round(c["end_s"] * 1000),
                      text=c["text"]) for c in parse_srt((directory / "bcut.srt").read_text(encoding="utf-8"))]
    xml = directory / "source.xml"
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


def _admitted(pool: Sequence[Mapping[str, Any]], done: set[str], lowest: float,
              reject: Callable[..., None]) -> list[Mapping[str, Any]]:
    """The candidates that may take a seat at all: talk, confident enough, admitted by the production admission."""
    eligible = []
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

    ranked = sorted(_admitted(pool, {str(b["cid"]) for b in built}, float(cfg["min_confidence"]), reject), key=selection_rank_key)
    taken = [(str(b["segment"]), int(b["start_ms"]), int(b["end_ms"])) for b in built]
    per_segment = Counter(str(b["segment"]) for b in built)
    seated: list[dict[str, Any]] = []

    def extra_seat_ok(entry: Mapping[str, Any]) -> bool:
        gate = grant.extra_slot_min_score          # seats past base_talk_seats exist only where a dated authority made them
        if gate is None or len(built) + len(seated) + 1 <= int(cfg["base_talk_seats"]):
            return True
        card = entry.get("selection_scorecard")
        return bool(selection_scorecard_is_valid(card) and float(card["effective_score"]) >= gate)

    def seat(entry: Mapping[str, Any]) -> None:
        seated.append(dict(entry))
        taken.append(_span(entry))
        per_segment[_span(entry)[0]] += 1

    deferred = []
    for entry in ranked:
        if _overlaps(entry, taken):
            reject(entry, OVERLAPS_SEATED, "overlaps a seated or built candidate of the same recording")
        elif len(seated) < slots and per_segment[_span(entry)[0]] < int(cfg["per_segment_soft_cap"]) and extra_seat_ok(entry):
            seat(entry)
        else:
            deferred.append(entry)
    for entry in deferred:                           # the per-recording limit is soft: slack is refilled in rank order
        if _overlaps(entry, taken):
            reject(entry, OVERLAPS_SEATED, "overlaps a seated or built candidate of the same recording")
        elif len(seated) >= slots:
            reject(entry, QUOTA_FULL, f"{grant.cap} seats on {date} ({grant.source}), {len(built)} already built")
        elif extra_seat_ok(entry):
            seat(entry)
        else:
            reject(entry, EXTRA_SEAT_BELOW_SCORE, f"seat {len(built) + len(seated) + 1} needs effective_score >= {grant.extra_slot_min_score}")
    return Seating(seated=seated, rejected=rejected, policy=policy)
