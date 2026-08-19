"""Durable per-recording-segment media orientation and scene classification.

One recorder session can contain materially different scenes after a title or
layout change.  The segment is therefore the authority boundary: the receipt
below binds ffprobe dimensions and recorder title/context inputs to the exact
segment stat signature, so both selection quota policy and speaker routing
consume one persisted fact surface.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Mapping


SCHEMA_VERSION = "segment-scene-context.v1"
ORIENTATIONS = frozenset({"landscape", "portrait", "square", "unknown"})
SCENE_KINDS = frozenset({"event", "talk"})

_SEGMENT_DATE_RX = re.compile(r"_(\d{8})-\d{2}-\d{2}-\d{2}$")
_EVENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("3d_live", re.compile(r"(?i)(?:3\s*d\s*(?:live|直播|回)|3d(?:live|回))")),
    ("birthday", re.compile(r"(?:生日(?:回|会|直播)|生诞(?:祭|回|直播))")),
    ("anniversary", re.compile(r"(?:周年(?:回|纪念|直播|庆)|纪念回)")),
)
_META_MAX_BYTES = 1 * 1024 * 1024
_XML_HEAD_MAX_BYTES = 256 * 1024


class SegmentSceneContextError(ValueError):
    pass


def _sha256_json(document: object) -> str:
    payload = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _path_signature(path: Path) -> dict[str, object]:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return {"status": "MISSING", "basename": path.name}
    except OSError as exc:
        return {
            "status": "UNREADABLE",
            "basename": path.name,
            "error_type": type(exc).__name__,
        }
    return {
        "status": "PRESENT",
        "basename": path.name,
        "size_bytes": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
    }


def _recording_date(segment: Path) -> str | None:
    matched = _SEGMENT_DATE_RX.search(segment.stem)
    if matched is None:
        return None
    raw = matched.group(1)
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def probe_video_dimensions(segment: Path) -> dict[str, object]:
    """Return a typed first-video-stream probe without raising to callers."""

    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(segment),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "UNKNOWN",
            "width": None,
            "height": None,
            "orientation": "unknown",
            "reason_code": f"FFPROBE_{type(exc).__name__.upper()}",
        }
    try:
        payload = json.loads(completed.stdout)
        stream = payload["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        if completed.returncode != 0 or width <= 0 or height <= 0:
            raise ValueError("non-positive dimensions")
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "status": "UNKNOWN",
            "width": None,
            "height": None,
            "orientation": "unknown",
            "reason_code": "FFPROBE_DIMENSIONS_UNAVAILABLE",
        }
    orientation = (
        "landscape" if width > height else "portrait" if height > width else "square"
    )
    return {
        "status": "PASS",
        "width": width,
        "height": height,
        "orientation": orientation,
        "reason_code": "VIDEO_DIMENSIONS_PROBED",
    }


def _normalized_media_probe(payload: Mapping[str, object]) -> dict[str, object]:
    width, height = payload.get("width"), payload.get("height")
    if (
        payload.get("status") == "PASS"
        and isinstance(width, int)
        and not isinstance(width, bool)
        and width > 0
        and isinstance(height, int)
        and not isinstance(height, bool)
        and height > 0
    ):
        orientation = (
            "landscape" if width > height else "portrait" if height > width else "square"
        )
        return {
            "status": "PASS",
            "width": width,
            "height": height,
            "orientation": orientation,
            "reason_code": str(payload.get("reason_code") or "VIDEO_DIMENSIONS_PROBED"),
        }
    return {
        "status": "UNKNOWN",
        "width": None,
        "height": None,
        "orientation": "unknown",
        "reason_code": str(payload.get("reason_code") or "DIMENSIONS_UNKNOWN"),
    }


def _meta_context_texts(path: Path) -> tuple[str, list[dict[str, str]]]:
    if not path.is_file() or path.is_symlink():
        return "MISSING", []
    try:
        if path.stat().st_size > _META_MAX_BYTES:
            return "UNREADABLE", []
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "UNREADABLE", []
    if not isinstance(payload, Mapping):
        return "UNREADABLE", []
    rows: list[dict[str, str]] = []
    containers = [("meta", payload)]
    for key in ("description", "recorder"):
        child = payload.get(key)
        if isinstance(child, Mapping):
            containers.append((f"meta.{key}", child))
    for prefix, container in containers:
        for key in ("title", "Title", "room_title", "RoomTitle"):
            value = str(container.get(key) or "").strip()
            if value:
                rows.append({"source": f"{prefix}.{key}", "text": value[:500]})
    return "READABLE", rows


def _xml_context_texts(path: Path | None) -> tuple[str, list[dict[str, str]]]:
    if path is None or not path.is_file() or path.is_symlink():
        return "MISSING", []
    try:
        head = path.read_bytes()[:_XML_HEAD_MAX_BYTES].decode("utf-8", "replace")
    except OSError:
        return "UNREADABLE", []
    rows: list[dict[str, str]] = []
    for source, pattern in (
        ("xml.metadata.room_title", re.compile(r"<room_title>(.*?)</room_title>", re.S)),
        (
            "xml.BililiveRecorderRecordInfo.title",
            re.compile(r"<BililiveRecorderRecordInfo\b[^>]*\btitle=([\"'])(.*?)\1", re.S),
        ),
    ):
        matched = pattern.search(head)
        if matched is None:
            continue
        raw = matched.group(2) if matched.lastindex == 2 else matched.group(1)
        value = html.unescape(raw).strip()
        if value:
            rows.append({"source": source, "text": value[:500]})
    return "READABLE", rows


def _event_context(
    meta_path: Path, xml_path: Path | None
) -> dict[str, object]:
    meta_status, meta_rows = _meta_context_texts(meta_path)
    xml_status, xml_rows = _xml_context_texts(xml_path)
    rows = [*meta_rows, *xml_rows]
    matches: list[dict[str, str]] = []
    for row in rows:
        for keyword_class, pattern in _EVENT_PATTERNS:
            matched = pattern.search(row["text"])
            if matched is not None:
                matches.append(
                    {
                        "keyword_class": keyword_class,
                        "surface": matched.group(0),
                        "source": row["source"],
                    }
                )
    if matches:
        status = "MATCH"
    elif rows or "READABLE" in {meta_status, xml_status}:
        status = "NO_MATCH"
    elif "UNREADABLE" in {meta_status, xml_status}:
        status = "UNREADABLE"
    else:
        status = "MISSING"
    return {
        "status": status,
        "matched_keywords": matches,
        "context_sources": [row["source"] for row in rows],
        "context_text_sha256": _sha256_json(rows),
        "input_status": {"meta": meta_status, "xml": xml_status},
    }


def validate_segment_scene_context(
    payload: object,
    *,
    expected_segment: str | None = None,
    expected_source_path: str | Path | None = None,
) -> dict[str, object]:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != SCHEMA_VERSION:
        raise SegmentSceneContextError("unsupported segment scene context schema")
    segment = str(payload.get("source_segment") or "")
    if not segment or Path(segment).name != segment:
        raise SegmentSceneContextError("segment scene context source is invalid")
    if expected_segment is not None and Path(expected_segment).name != segment:
        raise SegmentSceneContextError("segment scene context source binding mismatch")
    probe = payload.get("media_probe")
    event = payload.get("event_context")
    signatures = payload.get("input_signatures")
    if (
        not isinstance(probe, Mapping)
        or not isinstance(event, Mapping)
        or not isinstance(signatures, Mapping)
    ):
        raise SegmentSceneContextError("segment scene context evidence is incomplete")
    orientation = str(probe.get("orientation") or "")
    if orientation not in ORIENTATIONS or probe.get("status") not in {"PASS", "UNKNOWN"}:
        raise SegmentSceneContextError("segment orientation receipt is invalid")
    if probe.get("status") == "PASS":
        width, height = probe.get("width"), probe.get("height")
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (width, height)):
            raise SegmentSceneContextError("segment dimensions are invalid")
        measured = (
            "landscape" if width > height else "portrait" if height > width else "square"
        )
        if orientation != measured:
            raise SegmentSceneContextError("segment orientation contradicts dimensions")
    elif orientation != "unknown" or probe.get("width") is not None or probe.get("height") is not None:
        raise SegmentSceneContextError("unknown segment probe carries dimensions")
    if event.get("status") not in {"MATCH", "NO_MATCH", "MISSING", "UNREADABLE"}:
        raise SegmentSceneContextError("segment event context status is invalid")
    if event.get("status") == "MATCH" and not event.get("matched_keywords"):
        raise SegmentSceneContextError("segment event match lacks keyword evidence")
    scene_kind = str(payload.get("scene_kind") or "")
    if scene_kind not in SCENE_KINDS:
        raise SegmentSceneContextError("segment scene kind is invalid")
    may_be_event = orientation == "landscape" and event.get("status") == "MATCH"
    if (scene_kind == "event") != may_be_event:
        raise SegmentSceneContextError("segment event decision contradicts its evidence")
    fingerprint = str(payload.get("input_fingerprint_sha256") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
        raise SegmentSceneContextError("segment input fingerprint is invalid")
    if fingerprint != _sha256_json(signatures):
        raise SegmentSceneContextError("segment input fingerprint binding mismatch")
    if expected_source_path is not None and signatures.get("segment") != _path_signature(
        Path(expected_source_path)
    ):
        raise SegmentSceneContextError("segment source stat binding mismatch")
    return dict(payload)


def resolve_segment_scene_context(
    segment: Path,
    *,
    xml_path: Path | None = None,
    cached: object = None,
    dimension_probe: Callable[[Path], Mapping[str, object]] = probe_video_dimensions,
) -> dict[str, object]:
    """Build or reuse one stat-bound scene receipt.

    F13(维护者,裁定失落案重申):「我记得我当时说过 88 这个 3D live 场
    放宽到 15 个,然后当天的杂谈场认为是独立的,自然有 5 个」。事件放宽必须同时
    看到事件标题/语境和横屏；任何缺失、不可读、unknown 或竖屏都退回 talk。
    """

    segment = Path(segment)
    meta_path = segment.with_suffix(".meta.json")
    signatures = {
        "segment": _path_signature(segment),
        "meta": _path_signature(meta_path),
        "xml": _path_signature(xml_path) if xml_path is not None else {"status": "MISSING"},
    }
    input_fingerprint = _sha256_json(signatures)
    try:
        prior = validate_segment_scene_context(cached, expected_segment=segment.name)
    except SegmentSceneContextError:
        prior = None
    if (
        prior is not None
        and prior.get("input_fingerprint_sha256") == input_fingerprint
        # A successful dimension fact is durable.  UNKNOWN remains the
        # fail-closed decision for this tick, but is deliberately retried on
        # later ticks so a transient ffprobe/runtime outage cannot freeze the
        # segment into TALK forever without any source-stat change.
        and prior["media_probe"].get("status") == "PASS"
    ):
        return prior

    try:
        probe = _normalized_media_probe(dict(dimension_probe(segment)))
    except Exception as exc:  # injected probes obey the same fail-closed contract
        probe = {
            "status": "UNKNOWN",
            "width": None,
            "height": None,
            "orientation": "unknown",
            "reason_code": f"DIMENSION_PROBE_{type(exc).__name__.upper()}",
        }
    event = _event_context(meta_path, xml_path)
    orientation = str(probe["orientation"])
    is_event = orientation == "landscape" and event["status"] == "MATCH"
    if orientation == "portrait":
        reason_code = "PORTRAIT_SEGMENT_NEVER_EVENT"
    elif orientation != "landscape":
        reason_code = "ORIENTATION_NOT_LANDSCAPE_FAIL_CLOSED"
    elif event["status"] != "MATCH":
        reason_code = "EVENT_CONTEXT_NOT_PROVEN_FAIL_CLOSED"
    else:
        reason_code = "LANDSCAPE_EVENT_CONTEXT_CONFIRMED"
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "source_segment": segment.name,
        "recording_date": _recording_date(segment),
        "input_signatures": signatures,
        "input_fingerprint_sha256": input_fingerprint,
        "media_probe": probe,
        "event_context": event,
        "scene_kind": "event" if is_event else "talk",
        "decision_reason_code": reason_code,
    }
    return validate_segment_scene_context(receipt, expected_segment=segment.name)
