"""Repository-sealed authority for C3's line947 speaker-only recovery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.repository_asset_authority import require_repository_asset_authority
from src.autoslice.speaker_common import HOST_SPEAKER, SpeakerFinalizationError

CANDIDATE_ID = "auto_220021_561_670"
SCHEMA = "fastlane-c3-line947-speaker-authority.v2"
ASSET = Path("assets/lidousha/fastlane_c3_speaker_authorities/auto_220021_561_670.line947.v2.json")
RAW_EVENT_SHA256 = "e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa"
MESSAGE_CONTENT_SHA256 = "0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b"
DURABLE_RULING_SHA256 = "29bc6e523645ecfbd9dbf15a5bea912dd741a6dced0b54ca41d4f906cebfde49"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_c3_line947_authority(*, repo_root: Path) -> dict[str, object]:
    path = repo_root / ASSET
    payload = path.read_bytes()
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpeakerFinalizationError("C3 sealed authority is unreadable") from exc
    if not isinstance(document, dict):
        raise SpeakerFinalizationError("C3 sealed authority is not an object")
    try:
        require_repository_asset_authority(repo_root=repo_root, relative_path=ASSET, observed_bytes=payload)
    except ValueError as exc:
        raise SpeakerFinalizationError("C3 sealed authority is not repository-bound") from exc
    return document


def _validate_event_binding(
    event: Mapping[str, object],
    *,
    raw_event_line: bytes,
    expected_session_id: str,
    expected_uuid: str,
    expected_timestamp: str,
    expected_raw_sha256: str,
    expected_content_sha256: str,
    expected_content_with_newline_sha256: str,
) -> None:
    """Check all three event byte layers without trusting extracted metadata."""

    if set(event) != {
        "session_id",
        "uuid",
        "timestamp",
        "raw_event_line_sha256",
        "message_content_sha256",
        "jq_message_content_with_newline_sha256",
    }:
        raise SpeakerFinalizationError("C3 raw event binding schema drift")
    if (
        event.get("session_id"),
        event.get("uuid"),
        event.get("timestamp"),
        event.get("raw_event_line_sha256"),
        event.get("message_content_sha256"),
        event.get("jq_message_content_with_newline_sha256"),
    ) != (
        expected_session_id,
        expected_uuid,
        expected_timestamp,
        expected_raw_sha256,
        expected_content_sha256,
        expected_content_with_newline_sha256,
    ):
        raise SpeakerFinalizationError("C3 raw event binding drift")
    if _sha256_bytes(raw_event_line) != expected_raw_sha256:
        raise SpeakerFinalizationError("C3 raw event line drift")
    try:
        parsed = json.loads(raw_event_line)
        content = parsed["message"]["content"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SpeakerFinalizationError("C3 raw event is not the expected user message") from exc
    if (
        not isinstance(content, str)
        or parsed.get("type") != "user"
        or parsed.get("sessionId") != expected_session_id
        or parsed.get("uuid") != expected_uuid
        or parsed.get("timestamp") != expected_timestamp
        or _sha256_bytes(content.encode("utf-8")) != expected_content_sha256
        or _sha256_bytes((content + "\n").encode("utf-8"))
        != expected_content_with_newline_sha256
    ):
        raise SpeakerFinalizationError("C3 message content layer drift")


def validate_c3_line947_authority(document: Mapping[str, object], *, raw_event_line: bytes, durable_ruling: Path, text_srt_sha256: str, delivery_media_sha256: str, predecessor_ass_sha256: str, predecessor_record_sha256: str, trim_provenance_sha256: str, cues: Sequence[object]) -> None:
    """Reject any C3 authority, ruling-layer, delivery, or cue-grid drift."""

    fields = {"schema_version", "candidate_id", "authority", "user_event", "durable_ruling", "delivery", "omission_policy", "cues", "subtitle_text_authorized", "upload_authorized"}
    if set(document) != fields or document.get("schema_version") != SCHEMA:
        raise SpeakerFinalizationError("C3 authority schema drift")
    if document.get("candidate_id") != CANDIDATE_ID or document.get("authority") != "Ivan/Claude line947 exhaustive C3 ruling":
        raise SpeakerFinalizationError("C3 authority candidate/owner mismatch")
    if document.get("subtitle_text_authorized") is not False or document.get("upload_authorized") is not False:
        raise SpeakerFinalizationError("C3 authority scope escalation")
    event = document.get("user_event")
    if not isinstance(event, Mapping):
        raise SpeakerFinalizationError("C3 raw event binding schema drift")
    _validate_event_binding(
        event,
        raw_event_line=raw_event_line,
        expected_session_id="0df2296b-500a-4681-ab5e-6fb46dc39579",
        expected_uuid="555195ed-ec18-418d-a311-558f7e54291f",
        expected_timestamp="2026-08-19T00:08:52.249Z",
        expected_raw_sha256=RAW_EVENT_SHA256,
        expected_content_sha256=MESSAGE_CONTENT_SHA256,
        expected_content_with_newline_sha256="de4349ef57a61fd77418809e8531bda37c59c73b0b502f331cb5d88456e333d4",
    )
    durable = document.get("durable_ruling")
    ruling_relative = Path("docs/reviews/2026-08-19-ivan-review-batch-rulings.md")
    try:
        require_repository_asset_authority(
            repo_root=durable_ruling.parents[2],
            relative_path=ruling_relative,
            observed_bytes=durable_ruling.read_bytes(),
        )
    except (OSError, ValueError) as exc:
        raise SpeakerFinalizationError("C3 durable ruling is not repository-bound") from exc
    if not isinstance(durable, Mapping) or durable != {"path": ruling_relative.as_posix(), "sha256": DURABLE_RULING_SHA256} or _sha256_bytes(durable_ruling.read_bytes()) != DURABLE_RULING_SHA256:
        raise SpeakerFinalizationError("C3 durable ruling layer drift")
    delivery = document.get("delivery")
    if not isinstance(delivery, Mapping) or set(delivery) != {"source_recording", "text_srt_sha256", "delivery_media_sha256", "predecessor_uniform_ass_sha256", "predecessor_record_sha256", "trim_provenance_sha256"}:
        raise SpeakerFinalizationError("C3 delivery binding schema drift")
    if delivery.get("source_recording") != {"basename": "22966160_20260813-22-00-21.mp4", "sha256": "e9ce6186f9d1f6e9bad8b3801054a63ef9833f4943878e1e721b5a338aae18da", "absolute_start_ms": 561650, "absolute_end_ms": 670690}:
        raise SpeakerFinalizationError("C3 source recording binding drift")
    if (delivery.get("text_srt_sha256"), delivery.get("delivery_media_sha256"), delivery.get("predecessor_uniform_ass_sha256"), delivery.get("predecessor_record_sha256"), delivery.get("trim_provenance_sha256")) != (text_srt_sha256, delivery_media_sha256, predecessor_ass_sha256, predecessor_record_sha256, trim_provenance_sha256):
        raise SpeakerFinalizationError("C3 delivery artifact binding drift")
    if document.get("omission_policy") != {"watched_video_and_end_theme_cues": "NONE", "predicate": "no watched-video speech, soundtrack, or end-theme lyrics may appear"}:
        raise SpeakerFinalizationError("C3 omission policy drift")
    rows = document.get("cues")
    if not isinstance(rows, list) or len(rows) != len(cues) or len(rows) != 11:
        raise SpeakerFinalizationError("C3 cue grid drift")
    for position, (row, cue) in enumerate(zip(rows, cues, strict=True), start=1):
        if not isinstance(row, Mapping) or set(row) != {"cue", "start", "end", "text", "speaker"} or row.get("cue") != position or row.get("speaker") != HOST_SPEAKER:
            raise SpeakerFinalizationError("C3 cue speaker/index drift")
        if any(row.get(field) != getattr(cue, field, None) for field in ("start", "end", "text")):
            raise SpeakerFinalizationError("C3 cue text/timing drift")
