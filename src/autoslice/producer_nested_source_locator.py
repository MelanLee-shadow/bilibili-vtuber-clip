"""Candidate-only discovery for the outer watched-player source.

The caption observer deliberately records only scene ownership and visible
captions.  This module is the next, separate evidence step: one already bound
outer frame may expose a page title or a public Bilibili URL.  Those hints are
looked up through injected, bounded transports and returned as an
``EVIDENCE_ONLY`` candidate.  They never authorize source-role attribution,
subtitle deletion, or a text repair.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from copy import deepcopy
from difflib import SequenceMatcher
import hashlib
import html
import inspect
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
import datetime as dt
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from src.autoslice.package_import import atomic_write_json, exclusive_lock

SCHEMA_VERSION = "nested-media-source-locator.v1"
LOCATOR_PROMPT = """Inspect this exact unburned frame from the OUTER live stream.
Look only at the outermost watched-player page chrome that is visibly outside
the video pixels: a browser/page title, public video URL, or player duration.
Ignore subtitles, captions, chat, banners, game UI, watermarks, and all text
inside the watched video, including any deeper embedded player. Do not use
audio, a transcript, prior answers, or prior knowledge. Copy visible titles
character by character, including Chinese names and Latin letters; never
autocorrect a character, name, spelling, or capitalization from context or
common knowledge. Preserve uncertainty: when a full title is not legible,
return only clearly readable continuous phrases and leave ambiguous text out
instead of guessing. If the outer page chrome is not legible, return empty
arrays. This is only a source lookup hint; do not identify a speaker or
propose a subtitle edit.
Return JSON only:
{"player_visible":true,"title_candidates":[],"url_candidates":[],"duration_candidates":[]}
"""

_BVID_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
_DURATION_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")
_LEADING_AUTHOR_RE = re.compile(
    r"^\s*(?:【[^】]{1,80}】|\[[^\]]{1,80}\]|\([^)]{1,80}\)|（[^）]{1,80}）)\s*"
)
_TITLE_CLAUSE_RE = re.compile(r"[，,。.!！?？；;：:\n]+")
_MAX_CANDIDATES = 12
_MAX_SEARCH_RESULTS = 20
_MIN_SEARCH_QUERY_CHARS = 8
_CAPTION_DISCOVERY_SCHEMA = "nested-media-source-caption-candidate-discovery.v1"
_MAX_CAPTION_METADATA_CANDIDATES = 3


class NestedSourceLocatorError(ValueError):
    """Raised only for malformed bound evidence or callback output."""


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _bare_sha256(value: object) -> str | None:
    # Normal scene/descriptor producers use both bare and sha256: digests.
    # The checked file bytes, rather than that presentation prefix, bind them.
    return value.removeprefix("sha256:") if isinstance(value, str) else None


def _sha_file(path: Path, *, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise NestedSourceLocatorError(f"{label}_UNSAFE")
    before = path.stat()
    from .evidence_identity import EvidenceFileChangedError
    try:
        digest = _evidence_file_sha256(path)
    except EvidenceFileChangedError as exc:
        raise NestedSourceLocatorError(f"{label}_CHANGED") from exc
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise NestedSourceLocatorError(f"{label}_CHANGED")
    return digest


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _canonical_sha(value: object) -> str:
    return _sha_bytes(_canonical_bytes(value))


def _safe_reason(prefix: str, exc: BaseException) -> str:
    return f"{prefix}_{type(exc).__name__.upper()}"


def _normal_text(value: object) -> str:
    text = html.unescape(_HTML_TAG_RE.sub(" ", str(value or "")))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _fold_text(value: object) -> str:
    return _normal_text(value).casefold()


def _public_url(value: object) -> str | None:
    text = _normal_text(value)
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").casefold()
    if not (
        host == "bilibili.com"
        or host.endswith(".bilibili.com")
        or host == "b23.tv"
        or host.endswith(".b23.tv")
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _duration_seconds(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = _normal_text(value)
        match = _DURATION_RE.fullmatch(text)
        if match:
            hours, minutes, secs = match.groups()
            seconds = (int(hours or 0) * 3600) + int(minutes) * 60 + int(secs)
        else:
            try:
                seconds = float(text)
            except (TypeError, ValueError):
                return None
    if seconds <= 0 or seconds > 24 * 3600:
        return None
    if isinstance(seconds, int):
        return seconds
    return int(seconds) if seconds.is_integer() else round(seconds, 3)


def _bvid_from_url(value: object) -> str | None:
    url = _public_url(value)
    if not url:
        return None
    match = _BVID_RE.search(url)
    return match.group(0) if match else None


def _bounded_strings(value: object, *, public_url: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:_MAX_CANDIDATES]:
        text = _public_url(item) if public_url else _normal_text(item)
        if text and len(text) <= 500 and text not in result:
            result.append(text)
    return result


def _search_query_from_title(value: object) -> str | None:
    """Choose the first specific continuous clause from a visible page title.

    The leading author label is presentation metadata, while a short clause
    such as a generic action word is too broad for public search.  The full
    title remains the matcher input after this query is sent.
    """

    text = _normal_text(value)
    text = _LEADING_AUTHOR_RE.sub("", text, count=1)
    for clause in _TITLE_CLAUSE_RE.split(text):
        phrase = clause.strip(" \t…·-—")
        key = "".join(
            char
            for char in _fold_text(phrase)
            if unicodedata.category(char)[:1] in {"L", "N"}
        )
        if len(key) >= _MIN_SEARCH_QUERY_CHARS:
            return phrase
    return None


def _title_match_key(value: object) -> str:
    """Canonical title text for the existing strict candidate comparison."""

    folded = _fold_text(value)
    return "".join(
        char for char in folded if unicodedata.category(char)[:1] in {"L", "N"}
    )


def _parse_visual(receipt: Mapping[str, object]) -> dict[str, object]:
    if receipt.get("status") != "OBSERVED":
        raise NestedSourceLocatorError("VISUAL_OBSERVER_UNAVAILABLE")
    answer = receipt.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise NestedSourceLocatorError("VISUAL_RESPONSE_EMPTY")
    answer = answer.strip()
    if answer.startswith("```json") and answer.endswith("```"):
        answer = answer[7:].strip()
        if answer.endswith("```"):
            answer = answer[:-3].strip()
    try:
        payload = json.loads(answer)
    except (TypeError, ValueError) as exc:
        raise NestedSourceLocatorError("VISUAL_RESPONSE_INVALID") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("player_visible"), bool):
        raise NestedSourceLocatorError("VISUAL_RESPONSE_INVALID")
    return {
        "player_visible": payload["player_visible"],
        "title_candidates": _bounded_strings(payload.get("title_candidates")),
        "url_candidates": _bounded_strings(payload.get("url_candidates"), public_url=True),
        "duration_candidates": _bounded_strings(payload.get("duration_candidates")),
    }


def _safe_visual_receipt(receipt: Mapping[str, object]) -> dict[str, object]:
    allowed = {
        "schema_version",
        "status",
        "reason_code",
        "media_path",
        "frame_ms",
        "image_path",
        "frame_sha256",
        "image_sha256",
        "prompt_sha256",
        "response_sha256",
        "model",
        "provider",
        "answer",
    }
    safe = {key: receipt[key] for key in allowed if key in receipt}
    return safe


def _resolve_evidence_path(raw: object, evidence_root: Path) -> Path:
    if not isinstance(raw, str) or not raw:
        raise NestedSourceLocatorError("CAPTION_EVIDENCE_PATH_MISSING")
    path = Path(raw)
    return path if path.is_absolute() else evidence_root / path


def _resolve_frame_path(raw: object, evidence_root: Path) -> Path:
    if not isinstance(raw, str) or not raw:
        raise NestedSourceLocatorError("SOURCE_FRAME_PATH_MISSING")
    path = Path(raw)
    return path if path.is_absolute() else evidence_root / path


def _explicit_source_frame(
    raw: object,
    *,
    candidate_id: str,
    source_media_sha: str,
    subtitle_sha: str,
    source_start_ms: int,
    source_end_ms: int,
    evidence_root: Path,
) -> dict[str, object] | None:
    """Validate a caller-provided current frame for source-hint discovery.

    A normal capture caller may have a better unburned frame than the frames
    retained by the caption observer (for example, a frame with readable
    outer-player chrome).  That frame must carry the same media, SRT, and
    interval bindings.  Once the field is present, malformed or stale input
    fails closed; silently falling back to an older caption frame would make
    the visual request appear current while inspecting the wrong pixels.
    """

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise NestedSourceLocatorError("SOURCE_FRAME_BINDING_INVALID")
    if raw.get("candidate_id") not in (None, candidate_id):
        raise NestedSourceLocatorError("SOURCE_FRAME_CANDIDATE_DRIFT")
    if _bare_sha256(raw.get("source_media_sha256")) != source_media_sha:
        raise NestedSourceLocatorError("SOURCE_FRAME_MEDIA_DRIFT")
    if _bare_sha256(raw.get("final_srt_sha256")) != subtitle_sha:
        raise NestedSourceLocatorError("SOURCE_FRAME_SRT_DRIFT")
    if raw.get("source_interval_ms") != [source_start_ms, source_end_ms]:
        raise NestedSourceLocatorError("SOURCE_FRAME_INTERVAL_DRIFT")
    if raw.get("source_role") not in (None, "WATCHED_MEDIA"):
        raise NestedSourceLocatorError("SOURCE_FRAME_ROLE_INVALID")
    frame_ms = raw.get("frame_ms")
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, int):
        raise NestedSourceLocatorError("SOURCE_FRAME_TIME_INVALID")
    if not source_start_ms <= frame_ms <= source_end_ms:
        raise NestedSourceLocatorError("SOURCE_FRAME_TIME_OUTSIDE_INTERVAL")
    frame_path = _resolve_frame_path(raw.get("frame_path"), evidence_root)
    frame_sha = _sha_file(frame_path, label="SOURCE_FRAME")
    if _bare_sha256(raw.get("frame_sha256")) != frame_sha:
        raise NestedSourceLocatorError("SOURCE_FRAME_HASH_DRIFT")

    capture_path_raw = raw.get("capture_evidence_path")
    capture_sha_declared = raw.get("capture_evidence_sha256")
    if (capture_path_raw is None) != (capture_sha_declared is None):
        raise NestedSourceLocatorError("SOURCE_FRAME_CAPTURE_BINDING_INVALID")
    capture: dict[str, object] = {}
    if capture_path_raw is not None:
        capture_path = _resolve_evidence_path(capture_path_raw, evidence_root)
        capture_sha = _sha_file(capture_path, label="SOURCE_FRAME_CAPTURE")
        if _bare_sha256(capture_sha_declared) != capture_sha:
            raise NestedSourceLocatorError("SOURCE_FRAME_CAPTURE_DRIFT")
        capture = {
            "capture_evidence_path": str(capture_path.absolute()),
            "capture_evidence_sha256": capture_sha,
        }
    frame: dict[str, object] = {
        "frame_id": _normal_text(raw.get("frame_id")),
        "frame_ms": frame_ms,
        "frame_path": str(frame_path.absolute()),
        "frame_sha256": frame_sha,
        "observation_id": _normal_text(raw.get("observation_id")),
        "source_role": "WATCHED_MEDIA",
    }
    frame.update(capture)
    return frame


def _load_bound_frame(
    *,
    spec: Mapping[str, object],
    candidate_id: str,
    source_media: Path,
    source_start_ms: int,
    source_end_ms: int,
    subtitle_path: Path,
    evidence_root: Path,
) -> dict[str, object]:
    watched = spec.get("watched_media") is True
    dedup = spec.get("nested_media_caption_dedup")
    if not watched or not isinstance(dedup, Mapping):
        raise NestedSourceLocatorError("SCENE_NOT_WATCHED_MEDIA")
    source_sha = _sha_file(source_media, label="SOURCE_MEDIA")
    subtitle_sha = _sha_file(subtitle_path, label="FINAL_SRT")
    evidence_path = _resolve_evidence_path(dedup.get("evidence_path"), evidence_root)
    evidence_sha = _sha_file(evidence_path, label="CAPTION_EVIDENCE")
    declared_evidence_sha = dedup.get("evidence_sha256")
    if declared_evidence_sha and _bare_sha256(declared_evidence_sha) != evidence_sha:
        raise NestedSourceLocatorError("CAPTION_EVIDENCE_DRIFT")
    try:
        document = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NestedSourceLocatorError("CAPTION_EVIDENCE_INVALID") from exc
    if not isinstance(document, Mapping):
        raise NestedSourceLocatorError("CAPTION_EVIDENCE_INVALID")
    if (
        document.get("candidate_id") != candidate_id
        or _bare_sha256(document.get("source_media_sha256")) != source_sha
        or _bare_sha256(document.get("input_transcript_sha256")) != subtitle_sha
        or document.get("source_interval_ms") != [source_start_ms, source_end_ms]
        or document.get("source_role") != "WATCHED_MEDIA"
    ):
        raise NestedSourceLocatorError("CAPTION_EVIDENCE_BINDING_DRIFT")
    rows = document.get("observations")
    if not isinstance(rows, list):
        raise NestedSourceLocatorError("CAPTION_EVIDENCE_INVALID")
    explicit_frame = _explicit_source_frame(
        spec.get("nested_media_source_frame"),
        candidate_id=candidate_id,
        source_media_sha=source_sha,
        subtitle_sha=subtitle_sha,
        source_start_ms=source_start_ms,
        source_end_ms=source_end_ms,
        evidence_root=evidence_root,
    )
    if explicit_frame is not None:
        return {
            "candidate_id": candidate_id,
            "source_media": str(source_media.absolute()),
            "source_media_sha256": source_sha,
            "final_srt": str(subtitle_path.absolute()),
            "final_srt_sha256": subtitle_sha,
            "source_interval_ms": [source_start_ms, source_end_ms],
            "caption_evidence_path": str(evidence_path.absolute()),
            "caption_evidence_sha256": evidence_sha,
            "frame_selection": "EXPLICIT_CURRENT_SOURCE_FRAME",
            "frame": explicit_frame,
        }
    bound: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        frame_raw = row.get("frame_path")
        frame_ms = row.get("frame_ms")
        if not isinstance(frame_raw, str) or not isinstance(frame_ms, int):
            continue
        frame_path = Path(frame_raw)
        if not frame_path.is_absolute():
            frame_path = evidence_path.parent / frame_path
        try:
            frame_sha = _sha_file(frame_path, label="CAPTION_FRAME")
        except NestedSourceLocatorError:
            continue
        if _bare_sha256(row.get("frame_sha256")) != frame_sha or not source_start_ms <= frame_ms <= source_end_ms:
            continue
        bound.append(
            {
                "frame_id": _normal_text(row.get("frame_id")),
                "frame_ms": frame_ms,
                "frame_path": str(frame_path.absolute()),
                "frame_sha256": frame_sha,
                "observation_id": _normal_text(row.get("observation_id")),
            }
        )
    if not bound:
        raise NestedSourceLocatorError("CAPTION_FRAME_MISSING")
    bound.sort(key=lambda row: (int(row["frame_ms"]), str(row["frame_id"])))
    return {
        "candidate_id": candidate_id,
        "source_media": str(source_media.absolute()),
        "source_media_sha256": source_sha,
        "final_srt": str(subtitle_path.absolute()),
        "final_srt_sha256": subtitle_sha,
        "source_interval_ms": [source_start_ms, source_end_ms],
        "caption_evidence_path": str(evidence_path.absolute()),
        "caption_evidence_sha256": evidence_sha,
        "frame_selection": "CAPTION_OBSERVATION_EARLIEST",
        "frame": bound[0],
        "frames": bound,
    }


def _extract_search_rows(raw: object) -> list[Mapping[str, object]]:
    if isinstance(raw, Mapping):
        for key in ("rows", "data", "result", "results"):
            if key in raw:
                return _extract_search_rows(raw[key])
        return [raw]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return [row for row in raw if isinstance(row, Mapping)]
    return []


def _transport_field(raw: object, key: str) -> object | None:
    return raw.get(key) if isinstance(raw, Mapping) else None


def _safe_transport_payload(raw: object) -> object | None:
    """Keep callback response evidence while excluding transport internals."""

    if isinstance(raw, Mapping):
        raw_payload = raw.get("_source_locator_raw_response")
        if isinstance(raw_payload, (Mapping, list)):
            raw = raw_payload
        if isinstance(raw, list):
            if len(raw) > _MAX_SEARCH_RESULTS:
                raise NestedSourceLocatorError("SOURCE_RESPONSE_EXCEEDS_BOUND")
            return list(raw)
        return {
            str(key): value
            for key, value in raw.items()
            if not str(key).startswith("_source_locator_")
        }
    if isinstance(raw, list):
        if len(raw) > _MAX_SEARCH_RESULTS:
            raise NestedSourceLocatorError("SOURCE_RESPONSE_EXCEEDS_BOUND")
        return list(raw)
    return None


def _capture_transport_response(raw: object) -> tuple[object, str]:
    """Bind the complete bounded payload, never a truncated candidate list."""
    payload = _safe_transport_payload(raw)
    if payload is None:
        raise NestedSourceLocatorError("SOURCE_RESPONSE_EVIDENCE_UNAVAILABLE")
    digest = _canonical_sha(payload)
    declared = _transport_field(raw, "_source_locator_response_sha256")
    if declared is not None and declared != digest:
        raise NestedSourceLocatorError("SOURCE_RESPONSE_HASH_MISMATCH")
    return payload, digest


def _normal_candidate(row: Mapping[str, object]) -> dict[str, object] | None:
    bvid = _normal_text(row.get("bvid") or row.get("BV") or row.get("id"))
    if not _BVID_RE.fullmatch(bvid):
        bvid = _bvid_from_url(row.get("arcurl") or row.get("url"))
    title = _normal_text(row.get("title") or row.get("name"))
    if not bvid or not title:
        return None
    url = _public_url(row.get("arcurl") or row.get("url"))
    result: dict[str, object] = {"bvid": bvid, "title": title}
    if url:
        result["url"] = url
    duration = _duration_seconds(row.get("duration") or row.get("length"))
    if duration is not None:
        result["duration_seconds"] = duration
    return result


def _metadata_view(raw: Mapping[str, object]) -> Mapping[str, object]:
    data = raw.get("data")
    return data if isinstance(data, Mapping) else raw


def _normal_metadata(raw: Mapping[str, object], bvid: str) -> dict[str, object] | None:
    data = _metadata_view(raw)
    actual_bvid = _normal_text(data.get("bvid") or data.get("BV") or bvid)
    title = _normal_text(data.get("title") or data.get("name"))
    cid_raw = data.get("cid")
    if cid_raw is None and isinstance(data.get("pages"), list) and data["pages"]:
        first = data["pages"][0]
        if isinstance(first, Mapping):
            cid_raw = first.get("cid")
    try:
        cid = int(cid_raw)
    except (TypeError, ValueError):
        return None
    duration = _duration_seconds(data.get("duration") or data.get("length"))
    if (
        actual_bvid != bvid
        or not title
        or cid <= 0
        or duration is None
    ):
        return None
    aid = data.get("aid")
    try:
        aid_value = int(aid) if aid is not None else None
    except (TypeError, ValueError):
        aid_value = None
    result: dict[str, object] = {
        "bvid": bvid,
        "cid": cid,
        "title": title,
        "duration_seconds": duration,
    }
    if aid_value is not None and aid_value > 0:
        result["aid"] = aid_value
    return result


def _caption_observation_for_frame(*, binding: Mapping[str, object]) -> dict[str, object]:
    """Read the one hash-bound watched-media caption for the selected frame."""

    evidence_path = Path(str(binding["caption_evidence_path"]))
    try:
        document = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NestedSourceLocatorError("CAPTION_EVIDENCE_INVALID") from exc
    rows = document.get("observations") if isinstance(document, Mapping) else None
    frame = binding.get("frame")
    if not isinstance(rows, list) or not isinstance(frame, Mapping):
        raise NestedSourceLocatorError("CAPTION_OBSERVATION_MISSING")
    # The scene descriptor may carry the current frame's own observation ID,
    # while the caption evidence document names that same physical frame with
    # its caption-observation ID.  Bind by the frame's measured time and
    # bytes; retain the evidence document's ID in the returned caption fact.
    frame_sha = _bare_sha256(frame.get("frame_sha256"))
    if frame_sha is None:
        raise NestedSourceLocatorError("CAPTION_OBSERVATION_FRAME_INVALID")
    matches = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        caption_present = row.get("caption_present")
        if (
            row.get("frame_ms") == frame.get("frame_ms")
            and _bare_sha256(row.get("frame_sha256")) == frame_sha
            and row.get("role") == "WATCHED_MEDIA_CAPTION"
            and (caption_present is None or caption_present is True)
            and row.get("pipeline_burned_subtitle") is False
        ):
            matches.append(row)
    if len(matches) != 1:
        raise NestedSourceLocatorError("CAPTION_OBSERVATION_BINDING_INVALID")
    row = matches[0]
    visible_text = _normal_text(row.get("visible_text"))
    if not visible_text or len(visible_text) > 500:
        raise NestedSourceLocatorError("CAPTION_OBSERVATION_TEXT_INVALID")
    return {
        "observation_id": str(row["observation_id"]),
        "frame_ms": int(row["frame_ms"]),
        "frame_sha256": frame_sha,
        "frame_path": str(frame["frame_path"]),
        "role": "WATCHED_MEDIA_CAPTION",
        "visible_text": visible_text,
        "visible_text_sha256": _canonical_sha({"visible_text": visible_text}),
    }


def _caption_search_query(value: object) -> str | None:
    """Prefer one visible Latin name clue over surrounding Chinese narration."""

    text = _normal_text(value)
    key = "".join(
        char
        for char in _fold_text(text)
        if unicodedata.category(char)[:1] in {"L", "N"}
    )
    if len(key) < _MIN_SEARCH_QUERY_CHARS:
        return None
    # Mixed-script captions often surround a channel/game alias with generic
    # speech. Searching that whole sentence lets unrelated narration outrank
    # the alias. Keep the complete caption in the receipt; this is only a lookup
    # hint, never an identity or speaker assertion. Multiple clues stay intact.
    clues = re.findall(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]{3,31}(?![A-Za-z0-9_])", text)
    if re.search(r"[\u3400-\u9fff]", text) and len(clues) == 1:
        return clues[0]
    return text


def _caption_fallback_transport_available(identity: Mapping[str, object] | None) -> bool:
    """Keep automatic caption discovery off for a known dead transport."""

    if identity is None:
        return True
    blocked = ("UNAVAILABLE", "FAILED", "ERROR", "INVALID", "MISSING", "OFFLINE", "UNBOUND")
    for key in ("status", "cache_status", "provider_status", "config_status"):
        value = identity.get(key)
        if isinstance(value, str) and any(token in value.upper() for token in blocked):
            return False
    return True


def discover_nested_source_caption_candidates(
    *,
    spec: Mapping[str, object],
    candidate_id: str,
    source_media: Path,
    source_start_ms: int,
    source_end_ms: int,
    subtitle_path: Path,
    evidence_root: Path,
    search_candidates: Callable[[str], object] | None = None,
    fetch_metadata: Callable[[str], Mapping[str, object]] | None = None,
    search_identity: Mapping[str, object] | None = None,
    metadata_identity: Mapping[str, object] | None = None,
    chat_authority_audit: dict[str, object] | None = None,
    source_frame: Mapping[str, object] | None = None,
    cached_source_probe: Callable[[Sequence[Mapping[str, object]]], Sequence[Mapping[str, object]]] | None = None,
    source_cache_root: Path | None = None,
) -> dict[str, object]:
    """Discover public candidates from one bound caption without source authority.

    This seam deliberately skips vision.  It consumes only the current
    ``WATCHED_MEDIA_CAPTION`` observation selected by ``_load_bound_frame`` and
    returns evidence for later source mapping; it can never return the normal
    ``PUBLIC_SOURCE_CANDIDATE_BOUND`` role contract.
    """

    binding: dict[str, object] | None = None
    request_sha256: str | None = None
    result: dict[str, object] | None = None

    def base(reason_code: str) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": _CAPTION_DISCOVERY_SCHEMA,
            "candidate_id": candidate_id,
            "status": "CANDIDATE_DISCOVERY_ONLY",
            "action": "KEEP",
            "reason_code": reason_code,
            "mutation_authorized": False,
            "source_role_authority": False,
            "candidate_discovery_only": True,
        }
        if binding is not None:
            for key in (
                "source_media_sha256",
                "final_srt_sha256",
                "source_interval_ms",
                "caption_evidence_path",
                "caption_evidence_sha256",
                "frame",
            ):
                if key in binding:
                    value[key] = binding[key]
        if request_sha256:
            value["request_sha256"] = request_sha256
        return value

    try:
        effective_spec = spec
        if source_frame is not None:
            effective_spec = {
                **spec,
                "nested_media_source_frame": {
                    **dict(source_frame),
                    "candidate_id": candidate_id,
                    "source_media_sha256": _sha_file(source_media, label="SOURCE_MEDIA"),
                    "final_srt_sha256": _sha_file(subtitle_path, label="FINAL_SRT"),
                    "source_interval_ms": [source_start_ms, source_end_ms],
                    "source_role": "WATCHED_MEDIA",
                },
            }
        binding = _load_bound_frame(
            spec=effective_spec,
            candidate_id=candidate_id,
            source_media=source_media,
            source_start_ms=source_start_ms,
            source_end_ms=source_end_ms,
            subtitle_path=subtitle_path,
            evidence_root=evidence_root,
        )
        caption = _caption_observation_for_frame(binding=binding)
        query = _caption_search_query(caption["visible_text"])
        request = {
            "schema_version": _CAPTION_DISCOVERY_SCHEMA,
            "response_evidence_contract": "complete-bounded-canonical-v2",
            "candidate_id": candidate_id,
            "source_media": binding["source_media"],
            "source_media_sha256": binding["source_media_sha256"],
            "final_srt": binding["final_srt"],
            "final_srt_sha256": binding["final_srt_sha256"],
            "source_interval_ms": binding["source_interval_ms"],
            "caption_evidence_path": binding["caption_evidence_path"],
            "caption_evidence_sha256": binding["caption_evidence_sha256"],
            "frame": binding["frame"],
            "caption_observation": caption,
            "query": query,
            "query_source": "WATCHED_MEDIA_CAPTION",
            "visual_identity": {"status": "NOT_USED_CAPTION_DISCOVERY"},
            "search_identity": dict(search_identity or {}),
            "metadata_identity": dict(metadata_identity or {}),
        }
        cache_aware = cached_source_probe is not None or source_cache_root is not None
        if cache_aware:
            request["source_cache_root"] = (
                str(source_cache_root.resolve()) if source_cache_root is not None else None
            )
            request["cache_probe_contract"] = (
                "existing-source-cache-v1"
            )
        base_request = dict(request)
        base_request_sha256 = _canonical_sha(base_request)
        request_sha256 = base_request_sha256
        cache_dir = evidence_root / "nested-source-locator" / candidate_id
        cache_path = cache_dir / f"{base_request_sha256}.json"
        cache_dir.mkdir(parents=True, exist_ok=True)

        def replay_is_valid(value: Mapping[str, object]) -> bool:
            discovery = value.get("caption_candidate_discovery")
            if not isinstance(discovery, Mapping):
                return True
            search_payload = discovery.get("search_response")
            search_sha = discovery.get("search_response_sha256")
            if (search_payload is None) != (search_sha is None):
                return False
            if search_payload is not None and search_sha != _canonical_sha(search_payload):
                return False
            metadata_rows = discovery.get("metadata_candidates", [])
            if not isinstance(metadata_rows, list):
                return False
            for row in metadata_rows:
                if not isinstance(row, Mapping):
                    return False
                payload = row.get("response")
                digest = row.get("response_sha256")
                if (payload is None) != (digest is None):
                    return False
                if payload is not None and digest != _canonical_sha(payload):
                    return False
            return True

        with exclusive_lock(cache_dir / "locator.lock", label="caption source candidate discovery"):
            if cache_path.exists():
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                cached_request = cached.get("request") if isinstance(cached, Mapping) else None
                request_matches = cached_request == base_request
                if cache_aware and isinstance(cached_request, Mapping):
                    request_matches = all(
                        cached_request.get(key) == value
                        for key, value in base_request.items()
                    )
                if not isinstance(cached, Mapping) or not request_matches:
                    result = base("CAPTION_DISCOVERY_CACHE_BINDING_DRIFT")
                    return result
                if cached.get("status") == "COMPLETE" and isinstance(cached.get("result"), Mapping):
                    replay = dict(cached["result"])
                    if not replay_is_valid(replay):
                        result = base("CAPTION_DISCOVERY_CACHE_RESPONSE_HASH_DRIFT")
                        return result
                    replay["cache_replayed"] = True
                    return replay
                result = base("CAPTION_DISCOVERY_CACHE_INVALID")
                return result

            claim = {
                "schema_version": _CAPTION_DISCOVERY_SCHEMA,
                "status": "DISPATCHED",
                "request": request,
            }
            atomic_write_json(cache_path, claim)
            result = base("CANDIDATE_DISCOVERY_ONLY")
            receipt_request: Mapping[str, object] = base_request
            result.update(
                {
                    "caption_observation": caption,
                    "query": query,
                    "query_source": "WATCHED_MEDIA_CAPTION",
                    "source_match_scope": "OBSERVED_FRAME_ONLY",
                    "visual_provider_calls": 0,
                }
            )
            if query is None:
                result["reason_code"] = "CAPTION_QUERY_NOT_SPECIFIC"
                atomic_write_json(
                    cache_path,
                    {
                        **claim,
                        "status": "COMPLETE",
                        "request": receipt_request,
                        "request_sha256": request_sha256,
                        "result": result,
                    },
                )
                return result
            if search_candidates is None:
                result["reason_code"] = "CAPTION_CANDIDATE_SEARCH_UNAVAILABLE"
                atomic_write_json(
                    cache_path,
                    {
                        **claim,
                        "status": "COMPLETE",
                        "request": receipt_request,
                        "request_sha256": request_sha256,
                        "result": result,
                    },
                )
                return result

            try:
                raw_rows = search_candidates(query)
                response_payload, response_sha = _capture_transport_response(raw_rows)
                normalized: list[dict[str, object]] = []
                seen: set[str] = set()
                for row in _extract_search_rows(raw_rows):
                    candidate = _normal_candidate(row)
                    if candidate is None or str(candidate["bvid"]) in seen:
                        continue
                    seen.add(str(candidate["bvid"]))
                    normalized.append(candidate)
                search_candidate_order = [
                    {
                        "search_index": index,
                        "bvid": candidate["bvid"],
                        "sort_basis": "SOURCE_SEARCH_RESPONSE_ORDER",
                    }
                    for index, candidate in enumerate(normalized[:_MAX_SEARCH_RESULTS])
                ]
                cache_probe_rows: list[Mapping[str, object]] = []
                cache_probe_error: str | None = None
                if cached_source_probe is not None:
                    try:
                        raw_probe_rows = cached_source_probe(normalized[:_MAX_SEARCH_RESULTS])
                        if not isinstance(raw_probe_rows, Sequence) or isinstance(
                            raw_probe_rows, (str, bytes)
                        ):
                            raise NestedSourceLocatorError("SOURCE_CACHE_PROBE_INVALID")
                        cache_probe_rows = [
                            row for row in raw_probe_rows if isinstance(row, Mapping)
                        ]
                        if len(cache_probe_rows) != len(search_candidate_order):
                            raise NestedSourceLocatorError("SOURCE_CACHE_PROBE_ORDER_INVALID")
                        for index, row in enumerate(cache_probe_rows):
                            if (
                                row.get("search_index") != index
                                or row.get("bvid") != normalized[index]["bvid"]
                                or row.get("sort_basis") != "SOURCE_SEARCH_RESPONSE_ORDER"
                            ):
                                raise NestedSourceLocatorError("SOURCE_CACHE_PROBE_BINDING_INVALID")
                    except Exception as exc:
                        cache_probe_error = type(exc).__name__
                        cache_probe_rows = [
                            {
                                "search_index": index,
                                "bvid": candidate["bvid"],
                                "sort_basis": "SOURCE_SEARCH_RESPONSE_ORDER",
                                "status": "CACHE_UNAVAILABLE",
                                "source_role_authority": False,
                                "uniqueness_claim": False,
                                "reason_code": "SOURCE_CACHE_PROBE_UNAVAILABLE",
                            }
                            for index, candidate in enumerate(normalized[:_MAX_SEARCH_RESULTS])
                        ]

                selected: list[tuple[int, dict[str, object], str, Mapping[str, object] | None]] = []
                selected_indexes: set[int] = set()
                for row in cache_probe_rows:
                    if row.get("status") != "CACHE_VALIDATED":
                        continue
                    index = row.get("search_index")
                    if not isinstance(index, int) or index < 0 or index >= len(normalized):
                        continue
                    if index in selected_indexes:
                        continue
                    selected.append((index, normalized[index], "VERIFIED_CACHE_FIRST", row))
                    selected_indexes.add(index)
                for index, candidate in enumerate(normalized):
                    if index in selected_indexes:
                        continue
                    selected.append((index, candidate, "SEARCH_RESPONSE_ORDER_FILL", None))
                    if len(selected) >= _MAX_CAPTION_METADATA_CANDIDATES:
                        break
                selected = selected[:_MAX_CAPTION_METADATA_CANDIDATES]

                metadata_rows: list[dict[str, object]] = []
                if fetch_metadata is not None:
                    for search_index, candidate, selection_reason, probe in selected:
                        bvid = str(candidate["bvid"])
                        metadata_row: dict[str, object] = {"bvid": bvid}
                        if cache_aware:
                            metadata_row.update(
                                {
                                    "search_index": search_index,
                                    "selection_reason": selection_reason,
                                }
                            )
                            if probe is not None:
                                metadata_row["cache_validation"] = dict(probe)
                        try:
                            metadata_raw = fetch_metadata(bvid)
                            metadata_payload, metadata_sha = _capture_transport_response(metadata_raw)
                            metadata = _normal_metadata(metadata_raw, bvid)
                            metadata_row.update(
                                {
                                    "status": "METADATA_BOUND" if metadata is not None else "METADATA_INVALID",
                                    "metadata": metadata,
                                    "response": metadata_payload,
                                    "response_sha256": metadata_sha,
                                }
                            )
                        except Exception as exc:
                            metadata_row.update(
                                {
                                    "status": "METADATA_FAILED",
                                    "error_type": type(exc).__name__,
                                }
                            )
                        metadata_rows.append(metadata_row)
                receipt_request = dict(base_request)
                metadata_selection: list[dict[str, object]] = []
                if cache_aware:
                    receipt_request["search_candidate_order"] = search_candidate_order
                    receipt_request["cache_candidate_probes"] = [dict(row) for row in cache_probe_rows]
                    metadata_selection = [
                        {
                            "search_index": search_index,
                            "bvid": candidate["bvid"],
                            "selection_reason": selection_reason,
                            "cache_status": probe.get("status") if probe is not None else "CACHE_NOT_SELECTED",
                        }
                        for search_index, candidate, selection_reason, probe in selected
                    ]
                    receipt_request["metadata_selection"] = metadata_selection
                    if cache_probe_error is not None:
                        receipt_request["cache_probe_error_type"] = cache_probe_error
                request_sha256 = _canonical_sha(receipt_request)
                result["request_sha256"] = request_sha256
                discovery_result: dict[str, object] = {
                    "caption_observation_id": caption["observation_id"],
                    "caption_frame_ms": caption["frame_ms"],
                    "caption_frame_sha256": caption["frame_sha256"],
                    "caption_text_sha256": caption["visible_text_sha256"],
                    "search_query": query,
                    "search_response": response_payload,
                    "search_response_sha256": response_sha,
                    "search_candidates": normalized[:_MAX_SEARCH_RESULTS],
                    "metadata_candidates": metadata_rows,
                    "metadata_candidate_limit": _MAX_CAPTION_METADATA_CANDIDATES,
                    "uniqueness_claim": False,
                    "source_role_authority": False,
                }
                if cache_aware:
                    discovery_result.update(
                        {
                            "search_candidate_order": search_candidate_order,
                            "cache_candidate_probes": [dict(row) for row in cache_probe_rows],
                            "cache_probe_error_type": cache_probe_error,
                            "metadata_selection": metadata_selection,
                        }
                    )
                result["caption_candidate_discovery"] = discovery_result
            except Exception as exc:
                result["reason_code"] = _safe_reason("CAPTION_CANDIDATE_SEARCH_FAILED", exc)
                result["candidate_discovery_error_type"] = type(exc).__name__
            atomic_write_json(
                cache_path,
                {
                    **claim,
                    "status": "COMPLETE",
                    "request": receipt_request,
                    "request_sha256": request_sha256,
                    "result": result,
                },
            )
            return result
    except Exception as exc:
        result = base(_safe_reason("CAPTION_CANDIDATE_DISCOVERY_FAILED", exc))
        if chat_authority_audit is not None:
            chat_authority_audit["nested_media_source_locator"] = result
        return result
    finally:
        if chat_authority_audit is not None and result is not None:
            chat_authority_audit["nested_media_source_locator"] = result


def _candidate_matches(
    candidates: Iterable[dict[str, object]], title_clues: list[str]
) -> list[dict[str, object]]:
    folded_clues = [
        _title_match_key(value) for value in title_clues if _title_match_key(value)
    ]
    dedup: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        bvid = str(candidate["bvid"])
        title = _title_match_key(candidate["title"])
        if not folded_clues:
            continue
        matched = False
        for clue in folded_clues:
            if clue == title:
                matched = True
                break
            shorter, longer = sorted((clue, title), key=len)
            if len(shorter) < 8 or len(shorter) / len(longer) < 0.65:
                continue
            if SequenceMatcher(None, clue, title).ratio() >= 0.84:
                matched = True
                break
        if not matched:
            continue
        dedup[bvid] = candidate
    return list(dedup.values())


def _base_result(
    *,
    binding: Mapping[str, object] | None,
    candidate_id: str,
    status: str,
    reason_code: str,
    request_sha256: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "status": status,
        "action": "KEEP",
        "reason_code": reason_code,
        "mutation_authorized": False,
        "source_role_authority": False,
    }
    if binding:
        for key in (
            "source_media_sha256",
            "final_srt_sha256",
            "source_interval_ms",
            "caption_evidence_path",
            "caption_evidence_sha256",
        ):
            if key in binding:
                result[key] = binding[key]
    if request_sha256:
        result["request_sha256"] = request_sha256
    return result


def _locate_nested_source_frame(
    *,
    spec: Mapping[str, object],
    candidate_id: str,
    source_media: Path,
    source_start_ms: int,
    source_end_ms: int,
    subtitle_path: Path,
    evidence_root: Path,
    visual_probe: Callable[[Path, int, str], Mapping[str, object]] | None = None,
    search_candidates: Callable[[str], object] | None = None,
    fetch_metadata: Callable[[str], Mapping[str, object]] | None = None,
    visual_identity: Mapping[str, object] | None = None,
    search_identity: Mapping[str, object] | None = None,
    metadata_identity: Mapping[str, object] | None = None,
    chat_authority_audit: dict[str, object] | None = None,
) -> dict[str, object]:
    """Discover one public source candidate from an already bound watched frame.

    The three transports are injected deliberately.  Runtime code can reuse
    its existing bounded Bili search and known-BVID metadata client, while
    tests and offline callers cannot accidentally open a new network route.
    """

    binding: dict[str, object] | None = None
    request_sha256: str | None = None
    result: dict[str, object] | None = None
    try:
        binding = _load_bound_frame(
            spec=spec,
            candidate_id=candidate_id,
            source_media=source_media,
            source_start_ms=source_start_ms,
            source_end_ms=source_end_ms,
            subtitle_path=subtitle_path,
            evidence_root=evidence_root,
        )
        frame = binding["frame"]
        question_sha = _canonical_sha({"prompt": LOCATOR_PROMPT})
        request = {
            "schema_version": SCHEMA_VERSION,
            "response_evidence_contract": "complete-bounded-canonical-v2",
            "candidate_id": candidate_id,
            "source_media": binding["source_media"],
            "source_media_sha256": binding["source_media_sha256"],
            "final_srt": binding["final_srt"],
            "final_srt_sha256": binding["final_srt_sha256"],
            "source_interval_ms": binding["source_interval_ms"],
            "caption_evidence_path": binding["caption_evidence_path"],
            "caption_evidence_sha256": binding["caption_evidence_sha256"],
            "frame": frame,
            "question_sha256": question_sha,
            "visual_identity": dict(visual_identity or {}),
            "search_identity": dict(search_identity or {}),
            "metadata_identity": dict(metadata_identity or {}),
        }
        request_sha256 = _canonical_sha(request)
        cache_dir = evidence_root / "nested-source-locator" / candidate_id
        cache_path = cache_dir / f"{request_sha256}.json"
        cache_dir.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(cache_dir / "locator.lock", label="nested source locator"):
            if cache_path.exists():
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(cached, Mapping) or cached.get("request") != request:
                    result = _base_result(
                        binding=binding,
                        candidate_id=candidate_id,
                        status="UNKNOWN",
                        reason_code="LOCATOR_CACHE_BINDING_DRIFT",
                        request_sha256=request_sha256,
                    )
                    return result
                if cached.get("status") == "DISPATCHED":
                    result = _base_result(
                        binding=binding,
                        candidate_id=candidate_id,
                        status="UNKNOWN",
                        reason_code="LOCATOR_AMBIGUOUS_OR_FAILED_DISPATCH",
                        request_sha256=request_sha256,
                    )
                    return result
                if cached.get("status") == "COMPLETE" and isinstance(cached.get("result"), Mapping):
                    replay = dict(cached["result"])
                    for stage in ("search", "metadata"):
                        payload = replay.get(f"{stage}_response")
                        digest = replay.get(f"{stage}_response_sha256")
                        if (payload is not None or digest is not None) and (
                            payload is None or digest != _canonical_sha(payload)
                        ):
                            result = _base_result(
                                binding=binding,
                                candidate_id=candidate_id,
                                status="UNKNOWN",
                                reason_code="LOCATOR_CACHE_RESPONSE_HASH_DRIFT",
                                request_sha256=request_sha256,
                            )
                            return result
                    replay["cache_replayed"] = True
                    result = replay
                    return replay
                result = _base_result(
                    binding=binding,
                    candidate_id=candidate_id,
                    status="UNKNOWN",
                    reason_code="LOCATOR_CACHE_INVALID",
                    request_sha256=request_sha256,
                )
                return result
            claim = {"schema_version": SCHEMA_VERSION, "status": "DISPATCHED", "request": request}
            atomic_write_json(cache_path, claim)

            if visual_probe is None:
                result = _base_result(
                    binding=binding,
                    candidate_id=candidate_id,
                    status="UNKNOWN",
                    reason_code="VISUAL_PROBE_UNAVAILABLE",
                    request_sha256=request_sha256,
                )
                atomic_write_json(cache_path, {**claim, "status": "COMPLETE", "result": result})
                return result
            try:
                receipt = visual_probe(
                    source_media,
                    int(frame["frame_ms"]),
                    LOCATOR_PROMPT,
                )
                if not isinstance(receipt, Mapping):
                    raise NestedSourceLocatorError("VISUAL_RECEIPT_INVALID")
                safe_receipt = _safe_visual_receipt(receipt)
                if receipt.get("status") != "OBSERVED":
                    result = _base_result(
                        binding=binding,
                        candidate_id=candidate_id,
                        status="UNKNOWN",
                        reason_code="VISUAL_PROBE_UNAVAILABLE",
                        request_sha256=request_sha256,
                    )
                    result["visual_receipt"] = safe_receipt
                    atomic_write_json(
                        cache_path, {**claim, "status": "COMPLETE", "result": result}
                    )
                    return result
                expected_frame_sha = frame["frame_sha256"]
                observed_frame_sha = receipt.get("frame_sha256") or receipt.get("image_sha256")
                if observed_frame_sha is not None and observed_frame_sha != expected_frame_sha:
                    raise NestedSourceLocatorError("VISUAL_FRAME_BINDING_MISMATCH")
                if receipt.get("frame_ms") is not None and receipt.get("frame_ms") != frame["frame_ms"]:
                    raise NestedSourceLocatorError("VISUAL_FRAME_TIME_MISMATCH")
                visual = _parse_visual(receipt)
                result = _base_result(
                    binding=binding,
                    candidate_id=candidate_id,
                    status="UNKNOWN",
                    reason_code="NO_OUTER_SOURCE_HINT",
                    request_sha256=request_sha256,
                )
                result.update(
                    {
                        "visual_observation": visual,
                        "visual_receipt": safe_receipt,
                        "frame_path": frame["frame_path"],
                        "frame_ms": frame["frame_ms"],
                        "frame_sha256": frame["frame_sha256"],
                        "frame_selection": binding["frame_selection"],
                    }
                )
                if not visual["player_visible"]:
                    result["reason_code"] = "OUTER_PLAYER_NOT_VISIBLE"
                else:
                    title_clues = list(visual["title_candidates"])
                    direct_bvids = sorted(
                        {
                            found
                            for url in visual["url_candidates"]
                            if (found := _bvid_from_url(url)) is not None
                        }
                    )
                    candidates: list[dict[str, object]] = []
                    if direct_bvids:
                        candidates = [{"bvid": value, "title": ""} for value in direct_bvids]
                    elif title_clues and search_candidates is not None:
                        search_query = _search_query_from_title(title_clues[0])
                        if search_query is None:
                            result["reason_code"] = "SOURCE_SEARCH_QUERY_NOT_SPECIFIC"
                        else:
                            result["search_query"] = search_query
                            try:
                                raw_rows = search_candidates(search_query)
                                response_payload, response_sha = _capture_transport_response(raw_rows)
                                normalized_rows = [
                                    row
                                    for row in (_normal_candidate(item) for item in _extract_search_rows(raw_rows))
                                    if row is not None
                                ]
                                candidates = _candidate_matches(normalized_rows, title_clues)
                                if not candidates:
                                    result["reason_code"] = "SOURCE_SEARCH_NO_TITLE_MATCH"
                                result["search_candidates"] = candidates[:_MAX_CANDIDATES]
                                result["search_results_sha256"] = _canonical_sha(normalized_rows)
                                result["search_response_sha256"] = response_sha
                                result["search_response"] = response_payload
                            except Exception as exc:
                                result["reason_code"] = _safe_reason("SOURCE_SEARCH_FAILED", exc)
                                candidates = []
                    elif title_clues:
                        result["reason_code"] = "SOURCE_SEARCH_UNAVAILABLE"
                    else:
                        result["reason_code"] = "OUTER_SOURCE_HINT_EMPTY"
                    if len(candidates) == 1 and fetch_metadata is not None:
                        selected = candidates[0]
                        bvid = str(selected["bvid"])
                        try:
                            metadata_raw = fetch_metadata(bvid)
                            if not isinstance(metadata_raw, Mapping):
                                raise NestedSourceLocatorError("SOURCE_METADATA_INVALID")
                            response_payload, response_sha = _capture_transport_response(metadata_raw)
                            metadata = _normal_metadata(metadata_raw, bvid)
                            result["metadata_sha256"] = _canonical_sha(metadata_raw)
                            result["metadata_response_sha256"] = response_sha
                            result["metadata_response"] = response_payload
                            if metadata is None:
                                raise NestedSourceLocatorError("SOURCE_METADATA_BINDING_MISMATCH")
                            if (
                                selected.get("title")
                                and _title_match_key(selected["title"])
                                != _title_match_key(metadata["title"])
                            ):
                                raise NestedSourceLocatorError("SOURCE_TITLE_BINDING_MISMATCH")
                            result.update(
                                {
                                    "status": "EVIDENCE_ONLY",
                                    "reason_code": "PUBLIC_SOURCE_CANDIDATE_BOUND",
                                    "candidate": metadata,
                                    "candidate_match": selected,
                                }
                            )
                        except Exception as exc:
                            result["reason_code"] = _safe_reason("SOURCE_METADATA_FAILED", exc)
                    elif len(candidates) > 1:
                        result["reason_code"] = "AMBIGUOUS_SOURCE_CANDIDATES"
                    elif len(candidates) == 1:
                        result["reason_code"] = "SOURCE_METADATA_UNAVAILABLE"
                atomic_write_json(cache_path, {**claim, "status": "COMPLETE", "result": result})
                return result
            except Exception as exc:
                result = _base_result(
                    binding=binding,
                    candidate_id=candidate_id,
                    status="UNKNOWN",
                    reason_code=_safe_reason("SOURCE_LOCATOR_FAILED", exc),
                    request_sha256=request_sha256,
                )
                result["visual_receipt"] = (
                    _safe_visual_receipt(receipt) if "receipt" in locals() and isinstance(receipt, Mapping) else {}
                )
                atomic_write_json(cache_path, {**claim, "status": "COMPLETE", "result": result})
                return result
    except Exception as exc:
        result = _base_result(
            binding=binding,
            candidate_id=candidate_id,
            status="UNKNOWN",
            reason_code=_safe_reason("SOURCE_LOCATOR_INPUT", exc),
            request_sha256=request_sha256,
        )
        if chat_authority_audit is not None:
            chat_authority_audit["nested_media_source_locator"] = result
        return result
    finally:
        if chat_authority_audit is not None and binding is not None:
            chat_authority_audit["nested_media_source_locator"] = result or {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "status": "UNKNOWN",
                "action": "KEEP",
                "mutation_authorized": False,
                "source_role_authority": False,
                "reason_code": "SOURCE_LOCATOR_NO_RESULT",
            }


def locate_nested_source(
    *,
    spec: Mapping[str, object],
    candidate_id: str,
    source_media: Path,
    source_start_ms: int,
    source_end_ms: int,
    subtitle_path: Path,
    evidence_root: Path,
    visual_probe: Callable[[Path, int, str], Mapping[str, object]] | None = None,
    search_candidates: Callable[[str], object] | None = None,
    fetch_metadata: Callable[[str], Mapping[str, object]] | None = None,
    visual_identity: Mapping[str, object] | None = None,
    search_identity: Mapping[str, object] | None = None,
    metadata_identity: Mapping[str, object] | None = None,
    chat_authority_audit: dict[str, object] | None = None,
    caption_candidate_discovery: bool = False,
    cached_source_probe: Callable[[Sequence[Mapping[str, object]]], Sequence[Mapping[str, object]]] | None = None,
    source_cache_root: Path | None = None,
) -> dict[str, object]:
    """Use later current frames when the first exposes no outer page chrome.

    Each different frame retains its original single-frame request/cache. A
    caller-selected frame remains exact; input drift, provider failures and
    ambiguous search results never trigger an alternative-frame bypass.
    A match identifies only the observed source, not the whole clip's roles.
    """
    if caption_candidate_discovery:
        return discover_nested_source_caption_candidates(
            spec=spec,
            candidate_id=candidate_id,
            source_media=source_media,
            source_start_ms=source_start_ms,
            source_end_ms=source_end_ms,
            subtitle_path=subtitle_path,
            evidence_root=evidence_root,
            search_candidates=search_candidates,
            fetch_metadata=fetch_metadata,
            search_identity=search_identity,
            metadata_identity=metadata_identity,
            chat_authority_audit=chat_authority_audit,
            cached_source_probe=cached_source_probe,
            source_cache_root=source_cache_root,
        )
    kwargs = {
        "candidate_id": candidate_id, "source_media": source_media,
        "source_start_ms": source_start_ms, "source_end_ms": source_end_ms,
        "subtitle_path": subtitle_path, "evidence_root": evidence_root,
        "visual_probe": visual_probe, "search_candidates": search_candidates,
        "fetch_metadata": fetch_metadata, "visual_identity": visual_identity,
        "search_identity": search_identity, "metadata_identity": metadata_identity,
    }
    result = _locate_nested_source_frame(spec=spec, **kwargs)
    empty_chrome = {"NO_OUTER_SOURCE_HINT", "OUTER_PLAYER_NOT_VISIBLE", "OUTER_SOURCE_HINT_EMPTY"}
    attempts = [{k: result[k] for k in ("request_sha256", "frame_ms", "frame_sha256", "reason_code") if k in result}]
    if spec.get("nested_media_source_frame") is None and result.get("reason_code") in empty_chrome:
        # The first dispatch already validated this exact current document.
        # Revalidate before reading subsequent frame bindings; never recover
        # from an invalid or unavailable first request by choosing new pixels.
        try:
            binding = _load_bound_frame(
                spec=spec, candidate_id=candidate_id, source_media=source_media,
                source_start_ms=source_start_ms, source_end_ms=source_end_ms,
                subtitle_path=subtitle_path, evidence_root=evidence_root,
            )
        except Exception as exc:
            result = _base_result(
                binding=None, candidate_id=candidate_id, status="UNKNOWN",
                reason_code=_safe_reason("SOURCE_LOCATOR_INPUT", exc),
            )
            if chat_authority_audit is not None:
                chat_authority_audit["nested_media_source_locator"] = result
            return result
        seen = {binding["frame"]["frame_sha256"]}
        for frame in binding.get("frames", [])[1:]:
            if frame["frame_sha256"] in seen:
                continue
            seen.add(frame["frame_sha256"])
            current = {
                **frame, "candidate_id": candidate_id,
                "source_media_sha256": binding["source_media_sha256"],
                "final_srt_sha256": binding["final_srt_sha256"],
                "source_interval_ms": binding["source_interval_ms"],
                "source_role": "WATCHED_MEDIA",
            }
            result = _locate_nested_source_frame(
                spec={**spec, "nested_media_source_frame": current}, **kwargs,
            )
            attempts.append({k: result[k] for k in ("request_sha256", "frame_ms", "frame_sha256", "reason_code") if k in result})
            if result.get("reason_code") not in empty_chrome:
                break
    if len(attempts) > 1:
        result = {**result, "frame_selection": "CURRENT_BOUND_FRAMES_AFTER_EMPTY_CHROME",
                  "frame_attempts": attempts, "source_match_scope": "OBSERVED_FRAME_ONLY"}
    primary_result = result
    if (
        spec.get("nested_media_source_frame") is None
        and primary_result.get("reason_code") in empty_chrome
        and search_candidates is not None
        and fetch_metadata is not None
        and _caption_fallback_transport_available(search_identity)
        and _caption_fallback_transport_available(metadata_identity)
    ):
        try:
            binding = _load_bound_frame(
                spec=spec, candidate_id=candidate_id, source_media=source_media,
                source_start_ms=source_start_ms, source_end_ms=source_end_ms,
                subtitle_path=subtitle_path, evidence_root=evidence_root,
            )
            frames = binding.get("frames")
            if not isinstance(frames, list):
                frames = [binding["frame"]]
            chosen_frame: Mapping[str, object] | None = None
            for frame in reversed(frames):
                if not isinstance(frame, Mapping):
                    continue
                selected = {
                    **dict(frame),
                    "candidate_id": candidate_id,
                    "source_media_sha256": binding["source_media_sha256"],
                    "final_srt_sha256": binding["final_srt_sha256"],
                    "source_interval_ms": binding["source_interval_ms"],
                    "source_role": "WATCHED_MEDIA",
                }
                try:
                    caption = _caption_observation_for_frame(
                        binding={**binding, "frame": selected}
                    )
                except NestedSourceLocatorError:
                    continue
                if _caption_search_query(caption["visible_text"]) is not None:
                    chosen_frame = selected
                    break
            if chosen_frame is not None:
                discovery = discover_nested_source_caption_candidates(
                    spec=spec,
                    candidate_id=candidate_id,
                    source_media=source_media,
                    source_start_ms=source_start_ms,
                    source_end_ms=source_end_ms,
                    subtitle_path=subtitle_path,
                    evidence_root=evidence_root,
                    search_candidates=search_candidates,
                    fetch_metadata=fetch_metadata,
                    search_identity=search_identity,
                    metadata_identity=metadata_identity,
                    source_frame=chosen_frame,
                    cached_source_probe=cached_source_probe,
                    source_cache_root=source_cache_root,
                )
                result = {
                    **primary_result,
                    "primary_locator_result": deepcopy(primary_result),
                    "caption_candidate_discovery": discovery,
                }
        except (NestedSourceLocatorError, OSError, ValueError):
            # A malformed/failed caption observation leaves the normal UNKNOWN
            # locator intact; the fallback must never hide its primary reason.
            result = primary_result
    if chat_authority_audit is not None:
        chat_authority_audit["nested_media_source_locator"] = result
    return result


def build_nested_source_locator(
    *,
    visual_probe: Callable[[Path, int, str], Mapping[str, object]] | None,
    search_candidates: Callable[[str], object] | None,
    fetch_metadata: Callable[[str], Mapping[str, object]] | None,
    visual_identity: Mapping[str, object] | None = None,
    search_identity: Mapping[str, object] | None = None,
    metadata_identity: Mapping[str, object] | None = None,
    cached_source_probe: Callable[[Sequence[Mapping[str, object]]], Sequence[Mapping[str, object]]] | None = None,
    source_cache_root: Path | None = None,
) -> Callable[..., dict[str, object]]:
    """Bind existing runtime transports without creating a new HTTP client."""

    def locate(**kwargs: object) -> dict[str, object]:
        return locate_nested_source(
            **kwargs,
            visual_probe=visual_probe,
            search_candidates=search_candidates,
            fetch_metadata=fetch_metadata,
            visual_identity=visual_identity,
            search_identity=search_identity,
            metadata_identity=metadata_identity,
            cached_source_probe=cached_source_probe,
            source_cache_root=source_cache_root,
        )

    return locate


def build_runtime_nested_source_locator(
    runtime_root: Path | None = None,
    *,
    repo_root: Path | None = None,
    community_config_path: Path | None = None,
    cache_root: Path | None = None,
    source_cache_root: Path | None = None,
    offline: bool = False,
) -> Callable[..., dict[str, object]]:
    """Bind the ordinary CPA frame and bounded public Bili transports.

    The factory only wires existing clients.  It does not download a source
    video or create a source-role receipt.  ``offline=True`` is useful for
    local seam preflights: visual calls return unavailable without touching
    CPA, and Bili callbacks can consume only the existing bounded cache.
    ``source_cache_root`` enables the additive read-only cache probe.  When it
    is omitted, the ordinary runtime source-cache root
    ``runtime/cache/nested-sources`` is used only if it already contains
    ``BV*/<cid>/source.json`` entries.  This keeps the old v12 request shape
    for an empty cache while letting the normal acoustic and locator builders
    share the same existing source-cache entries.
    """

    from src.autoslice import cpa_frame_witness
    from src.autoslice.producer_final_review_transport import _resolved_runtime_root

    frame_vision_probe = cpa_frame_witness.frame_vision_probe
    frame_parameter = inspect.signature(frame_vision_probe).parameters.get("model")
    frame_model = frame_parameter.default if frame_parameter is not None else None
    if not isinstance(frame_model, str) or not frame_model.strip():
        frame_model = None
    runtime = _resolved_runtime_root(runtime_root)
    repo = Path(repo_root).absolute() if repo_root is not None else None
    resolved_source_cache_root: Path | None = None

    def has_source_cache_entries(root: Path) -> bool:
        try:
            return any(root.glob("BV*/**/source.json"))
        except OSError:
            return False

    if source_cache_root is not None:
        resolved_source_cache_root = Path(source_cache_root).absolute()
    else:
        # ``cache_root`` historically names the bounded HTTP cache.  Prefer
        # the ordinary runtime source-cache root when it already contains
        # source entries, then retain compatibility with callers that passed
        # a populated source cache through the older ``cache_root`` seam.
        default_source_cache_root = (
            runtime / "cache" / "nested-sources" if runtime is not None else None
        )
        if default_source_cache_root is not None and has_source_cache_entries(
            default_source_cache_root
        ):
            resolved_source_cache_root = default_source_cache_root.absolute()
        elif cache_root is not None and has_source_cache_entries(Path(cache_root)):
            resolved_source_cache_root = Path(cache_root).absolute()

    cached_source_probe = None
    if resolved_source_cache_root is not None:
        from src.autoslice.nested_source_cache import (
            probe_cached_caption_source_candidates,
        )

        def cached_source_probe(
            candidates: Sequence[Mapping[str, object]],
        ) -> Sequence[Mapping[str, object]]:
            return probe_cached_caption_source_candidates(
                cache_root=resolved_source_cache_root,
                candidates=candidates,
            )

    if runtime is None:
        return build_nested_source_locator(
            visual_probe=None,
            search_candidates=None,
            fetch_metadata=None,
            visual_identity={"status": "RUNTIME_UNBOUND"},
            search_identity={"status": "RUNTIME_UNBOUND"},
            metadata_identity={"status": "RUNTIME_UNBOUND"},
            cached_source_probe=cached_source_probe,
            source_cache_root=resolved_source_cache_root,
        )

    visual_identity = {
        "transport": "src.autoslice.cpa_frame_witness.frame_vision_probe",
        "model": frame_model,
        "model_source": "frame_vision_probe_default",
        "frame_encoder": {
            "helper": "cpa_frame_witness._extract_frame_jpeg",
            "timestamp_format": "milliseconds_to_seconds_3dp",
            "max_width": 1280,
            "quality": 4,
        },
        "runtime_root": str(runtime),
        "offline": offline,
    }

    def visual_probe(media: Path, ms: int, question: str) -> Mapping[str, object]:
        if offline:
            return {
                "status": "UNAVAILABLE",
                "reason_code": "RUNTIME_OFFLINE",
                "frame_ms": int(ms),
            }
        if frame_model is None:
            return {
                "status": "UNAVAILABLE",
                "reason_code": "RUNTIME_VISUAL_MODEL_UNBOUND",
                "frame_ms": int(ms),
            }
        try:
            from src.autoslice.llm_client import runtime_cpa_command_environment

            env = runtime_cpa_command_environment(runtime)
            return frame_vision_probe(
                media,
                ms,
                question,
                api_base=env.get("CPA_BASE_URL", ""),
                api_key=env.get("CPA_API_KEY", ""),
            )
        except Exception as exc:
            return {
                "status": "UNAVAILABLE",
                "reason_code": f"RUNTIME_BINDING_{type(exc).__name__.upper()}",
            }

    if community_config_path is None and repo is not None:
        try:
            from src.autoslice.channel_profile import load_channel_profile

            community_config_path = load_channel_profile(repo).asset_file(
                "community_name_sources", repo_root=repo
            )
        except Exception:
            community_config_path = None
    try:
        from src.autoslice.community_comment_discovery import fetch_wbi_mixin_key
        from src.autoslice.community_name_crawler import _search_rows, load_config
        from src.autoslice.timely_term_crawler import BoundedHttpClient, HttpCache

        if community_config_path is None:
            raise NestedSourceLocatorError("COMMUNITY_SEARCH_CONFIG_MISSING")
        config = load_config(Path(community_config_path))
        cache = HttpCache(
            cache_root or runtime / "cache" / "nested-source-locator",
            max_body_bytes=512 * 1024,
            max_entries=32,
            max_total_bytes=16 * 1024 * 1024,
        )
        client = BoundedHttpClient(
            allowed_hosts=frozenset(str(item).lower() for item in config["source_hosts"]),
            max_requests=3,
            max_response_bytes=512 * 1024,
            timeout_seconds=15.0,
            cache=cache,
            cache_ttl=dt.timedelta(hours=18),
            stale_if_error=dt.timedelta(days=7),
            offline=offline,
        )
        mixin_key: str | None = None

        # Search remains the existing bounded anonymous client.  The public
        # view endpoint may reject that client with HTTP 412, so metadata uses
        # the already established read-only login session from the normal
        # uploader path.  Do not fall back to the anonymous client here: a
        # candidate without a verified view response must remain unresolved.
        metadata_http: Callable[[str], Mapping[str, object]] | None = None
        metadata_identity: dict[str, object] = {
            "transport": "scripts.authorized_upload._build_season_http",
            "endpoint": "https://api.bilibili.com/x/web-interface/view",
            "cookie_source": "authorized_upload.DEFAULT_COOKIE_JSON",
            "cookie_origin": "api.bilibili.com",
            "offline": offline,
        }
        if not offline:
            try:
                from scripts import authorized_upload

                import urllib.parse

                view_api = str(authorized_upload.VIEW_API)
                parsed_view = urlsplit(view_api)
                if parsed_view.scheme != "https" or parsed_view.hostname != "api.bilibili.com":
                    raise NestedSourceLocatorError("SOURCE_METADATA_ORIGIN_INVALID")
                season_http, _csrf = authorized_upload._build_season_http(
                    authorized_upload.DEFAULT_COOKIE_JSON
                )

                def metadata_reader(bvid: str) -> Mapping[str, object]:
                    url = view_api.format(bvid=urllib.parse.quote(bvid, safe=""))
                    payload = season_http(url)
                    if not isinstance(payload, Mapping):
                        raise NestedSourceLocatorError("SOURCE_METADATA_RESPONSE_INVALID")
                    if payload.get("code") != 0:
                        raise NestedSourceLocatorError("SOURCE_METADATA_RESPONSE_REJECTED")
                    # The uploader callback returns JSON only.  Persist the
                    # exact public payload hash without carrying cookies or
                    # request headers into locator evidence.
                    return {
                        **dict(payload),
                        "_source_locator_response_sha256": _canonical_sha(payload),
                        "_source_locator_raw_response": dict(payload),
                    }

                metadata_http = metadata_reader
            except Exception as exc:
                metadata_identity["status"] = (
                    f"UNAVAILABLE_{type(exc).__name__.upper()}"
                )
        else:
            metadata_identity["status"] = "OFFLINE"

        def search_candidates(query: str) -> Mapping[str, object]:
            nonlocal mixin_key
            if mixin_key is None:
                mixin_key, fresh = fetch_wbi_mixin_key(
                    client, endpoint=str(config["wbi_nav_endpoint"])
                )
                if not fresh:
                    raise NestedSourceLocatorError("SOURCE_SEARCH_WBI_STALE")
            rows, fresh = _search_rows(
                client,
                endpoint=str(config["search_endpoint"]),
                query=query,
                order="totalrank",
                page=1,
                max_results=min(int(config["max_results"]), 20),
                mixin_key=mixin_key,
                signed_at=dt.datetime.now(dt.timezone.utc).replace(microsecond=0),
            )
            if not fresh:
                raise NestedSourceLocatorError("SOURCE_SEARCH_RESPONSE_STALE")
            return {
                "rows": rows,
                "_source_locator_response_sha256": _canonical_sha(rows),
                "_source_locator_raw_response": rows,
            }

        return build_nested_source_locator(
            visual_probe=visual_probe,
            search_candidates=search_candidates,
            fetch_metadata=metadata_http,
            visual_identity=visual_identity,
            search_identity={
                "transport": "community_name_crawler.BoundedHttpClient",
                "endpoint": str(config["search_endpoint"]),
                "max_requests": 3,
                "query_strategy": "first-specific-clause-v1",
                "query_min_chars": _MIN_SEARCH_QUERY_CHARS,
                "offline": offline,
            },
            metadata_identity=metadata_identity,
            cached_source_probe=cached_source_probe,
            source_cache_root=resolved_source_cache_root,
        )
    except Exception as exc:
        # Missing profile/config/cache setup leaves the ordinary producer
        # usable; it simply records UNKNOWN after the visual seam.
        return build_nested_source_locator(
            visual_probe=visual_probe,
            search_candidates=None,
            fetch_metadata=None,
            visual_identity=visual_identity,
            search_identity={"status": f"UNAVAILABLE_{type(exc).__name__.upper()}"},
            metadata_identity={"status": "UNAVAILABLE"},
            cached_source_probe=cached_source_probe,
            source_cache_root=resolved_source_cache_root,
        )
