"""Every chat event of one live session, on one clock.

The recorder splits a live session into files of about half an hour, and each file's XML clock starts at that file's own
`BililiveRecorderRecordInfo/start_time`.  Splitting is a storage detail: whatever looks for chat evidence by live time
(ordinary comments, SC, gifts, guards) looks on the session timeline, not inside one file.  `load` returns the events of
all the session's segments on the clock of one anchor segment.  A segment that cannot be listed, read or placed on the
clock is returned as unknown; it is never guessed away and never stops the clip.

Which segments are one session is decided by time, never by file name (the recorder's name template has changed inside
one live): a segment spans [its start_time, start_time + its last event], and the session is the chain of segments that
touch the anchor's span or lie within SESSION_GAP_S of the span gathered so far.  Segments recorded at the same time by
two recorders overlap and so join.  After joining, one event seen in two segments is kept once.

Event identity across segments.  An ordinary comment is the same one when its `p_rest` (which carries the server's send
time in ms and the sender's hash) and `raw` text are equal.  An SC, gift or guard node carries, besides the recorder's
relative `ts`, the sender (`user`, `uid`) and in its `raw` JSON the server's own epoch second (`timestamp` for a gift,
`start_time` for an SC and a guard; read from OCI3 recordings).  recorder_events does not return that second, so it is
read here from the XML.  Two events are the same when type, sender and content (SC text, gift name and count, guard
level and count) are equal and their server seconds are equal; if either side lacks a server second, when they are at
most DUPLICATE_WINDOW_S apart on the common clock.  Only events of different segments are merged: two equal events inside
one segment were really sent twice.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import re
from typing import Protocol, TypedDict
import xml.etree.ElementTree as ET

from .recorder_events import Danmaku, recorder_events

_ORDINARY = re.compile(r'<d p="([^"]*)"[^>]*>([^<]*)</d>')
_UNREADABLE = (RuntimeError, OSError, ValueError, KeyError, ET.ParseError)
# Recorder reconnects and sparse chat leave gaps of minutes inside one live; two lives of one day are hours apart.
SESSION_GAP_S = 1200.0
DUPLICATE_WINDOW_S = 2.0
_CACHE_VERSION = "session-span-dedupe-1"


class UnknownSegment(TypedDict):
    segment: str | None
    reason: str


class SegmentSource(Protocol):
    def fetch(self, remote_path: str) -> bytes: ...
    def names(self, remote_dir: str) -> list[str]: ...


_Row = tuple[Danmaku, float | None]    # an event with its server second (None when it has none)
_Content = tuple[str | int | None, ...]


def segment_start_s(payload: bytes) -> float:
    """Epoch seconds of the recording's t=0, from the recorder's own metadata."""
    info = ET.fromstring(payload).find("./BililiveRecorderRecordInfo")
    value = None if info is None else info.get("start_time")
    if not value:
        raise ValueError("segment has no recorder start_time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("segment start_time has no time zone")
    return parsed.timestamp()


def segment_rows(payload: bytes, shift: float = 0.0) -> list[Danmaku]:
    """Ordinary comments (`text`, `p_rest`, `raw`) then typed recorder events of one segment, moved by `shift`."""
    rows: list[Danmaku] = []
    for match in _ORDINARY.finditer(payload.decode("utf-8", errors="replace")):
        sent, _, rest = match.group(1).partition(",")
        rows.append({"t_s": float(sent) + shift, "text": html.unescape(match.group(2)).strip(),
                     "p_rest": rest, "raw": match.group(2)})
    rows.extend({**event, "t_s": event["t_s"] + shift} for event in recorder_events(payload))
    return rows


def _server_second(node: ET.Element) -> float | None:
    """The server's epoch second of an SC / gift / guard node, from its `raw` JSON; None when it is not there."""
    try:
        raw = json.loads(node.get("raw") or "")
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    for key in ("timestamp", "start_time"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _segment_events(payload: bytes, shift: float = 0.0) -> list[_Row]:
    """`segment_rows` with each typed row's server second (None for ordinary comments and unstamped nodes)."""
    rows = segment_rows(payload, shift)
    typed = [row for row in rows if "type" in row]
    nodes = [n for n in ET.fromstring(payload).iter() if n.tag in {"sc", "gift", "guard"}]
    if len(typed) != len(nodes):
        raise ValueError("recorder events do not line up with the segment's SC/gift/guard nodes")
    stamps = {id(row): _server_second(node) for row, node in zip(typed, nodes)}
    return [(row, stamps.get(id(row))) for row in rows]


def _content(row: Danmaku) -> _Content:
    if "type" not in row:
        return ("d", row["p_rest"], row["raw"])
    return (row["type"], row["sender"], row["text"], row.get("gift_name"), row.get("count"), row.get("guard_level"))


def _merge(segments: list[list[_Row]]) -> list[Danmaku]:
    """Events of all segments by time, each event kept once (the earlier one).  `segments[0]` is the anchor."""
    tagged = [(row, srv, index) for index, events in enumerate(segments) for row, srv in events]
    tagged.sort(key=lambda item: item[0]["t_s"])
    kept: dict[_Content, list[tuple[int, float, float | None]]] = {}
    merged: list[Danmaku] = []
    for row, srv, index in tagged:
        seen = kept.setdefault(_content(row), [])
        if "type" in row:
            same = any(i != index and (srv == s if srv is not None and s is not None
                                       else abs(row["t_s"] - t) <= DUPLICATE_WINDOW_S) for i, t, s in seen)
        else:
            same = any(i != index for i, _, _ in seen)
        seen.append((index, row["t_s"], srv))
        if not same:
            merged.append(row)
    return merged


def _session_members(anchor_span: tuple[float, float], spans: dict[str, tuple[float, float]]) -> set[str]:
    """Names of the segments chained to the anchor: each within SESSION_GAP_S of the span gathered so far."""
    low, high = anchor_span
    joined: set[str] = set()
    grew = True
    while grew:
        grew = False
        for name, (start, end) in spans.items():
            if name not in joined and max(start - high, low - end, 0.0) <= SESSION_GAP_S:
                joined.add(name)
                low, high = min(low, start), max(high, end)
                grew = True
    return joined


def _span(start: float, events: list[_Row]) -> tuple[float, float]:
    """The segment's own seconds (start .. start + last event) as epoch seconds; `events` are unshifted."""
    return start, start + max([0.0] + [row["t_s"] for row, _ in events])


def _unknown(segment: str | None, exc: Exception) -> UnknownSegment:
    return {"segment": segment, "reason": f"{type(exc).__name__}: {exc}"}


def load(danmaku: SegmentSource, directory: str, anchor_stem: str, anchor_xml: bytes,
         cache: Path | None = None) -> tuple[list[Danmaku], list[UnknownSegment]]:
    """(events of the whole session on the anchor's clock, unknown segments).  `directory` holds the session's segment
    XMLs.  An anchor that is not one of them (a compilation's joined XML) has no other segments to be placed against.
    The parsed session is cached only when nothing is unknown, so a failed segment is read again at the next call."""
    own = segment_rows(anchor_xml)
    try:
        names = sorted(danmaku.names(directory))
    except _UNREADABLE as exc:
        return own, [_unknown(None, exc)]
    others = [n for n in names if Path(n).stem != anchor_stem]
    if len(others) == len(names):
        return own, []
    key = hashlib.sha256(json.dumps([_CACHE_VERSION, hashlib.sha256(anchor_xml).hexdigest(), names]).encode()).hexdigest()
    if cache is not None and cache.is_file():
        stored = json.loads(cache.read_text(encoding="utf-8"))
        if stored["key"] == key:
            return stored["rows"], []
    try:
        here = segment_start_s(anchor_xml)
        anchor_events = _segment_events(anchor_xml)
    except _UNREADABLE as exc:
        return own, [_unknown(None, exc)]
    unknown: list[UnknownSegment] = []
    read: dict[str, tuple[float, list[_Row]]] = {}
    for name in others:
        try:
            payload = danmaku.fetch(f"{directory}/{name}")
            start = segment_start_s(payload)
            read[name] = (start, _segment_events(payload))
        except _UNREADABLE as exc:
            unknown.append(_unknown(Path(name).stem, exc))
    members = _session_members(_span(here, anchor_events),
                               {name: _span(start, events) for name, (start, events) in read.items()})
    if not members:
        rows = own
    else:
        shifted = [anchor_events] + [[({**row, "t_s": row["t_s"] + start - here}, srv) for row, srv in events]
                                     for name, (start, events) in read.items() if name in members]
        rows = _merge(shifted)
    if cache is not None and not unknown:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    return rows, unknown
