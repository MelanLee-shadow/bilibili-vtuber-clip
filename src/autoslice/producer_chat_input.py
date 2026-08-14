"""Load clip-local danmaku, superchat, gift, and independent ASR evidence."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from .chat_authority import ChatEvidence, load_chat_jsonl, recording_start_epoch_ms
from .danmaku_evidence import load_danmaku_xml
from .isolated_source_read import IsolatedSourceReadError, read_source_bytes_isolated


DANMAKU_PRE_CONTEXT_MS = 30_000

SC_PRE_CONTEXT_MS = 900_000  # include SCs up to 15min before the clip: she CLEARS THE

GIFT_PRE_CONTEXT_MS = 120_000  # thanks for a gift usually follow within a couple

# GUARD_BUY thanks can be delayed while the streamer clears queued SCs.  The
# July 22 verified panoja event is acknowledged 232,140ms later; keep this in
# lockstep with chat_proposals.GUARD_THANK_WINDOW_AFTER_MS.
GUARD_PRE_CONTEXT_MS = 300_000


class StructuredChatEvidenceError(RuntimeError):
    """A declared structured-chat binding is missing, drifted, or unreadable."""

    def __init__(
        self,
        reason_code: str,
        *,
        evidence: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.evidence = dict(evidence or {})


_CHAT_SHA256_RX = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


def _declared_chat_sha256(value: object) -> str | None:
    match = _CHAT_SHA256_RX.fullmatch(str(value or "").strip().lower())
    return match.group(1) if match is not None else None


def _structured_chat_parent_unavailable(path: Path) -> bool:
    try:
        return not path.parent.is_dir()
    except OSError:
        return True


def _isolated_source_payload(
    path: Path,
    *,
    reason_prefix: str,
) -> tuple[bytes, str]:
    try:
        isolated = read_source_bytes_isolated(path)
    except IsolatedSourceReadError as exc:
        raise StructuredChatEvidenceError(
            f"{reason_prefix}_{exc.reason_code}",
            evidence={"source_path": str(path), "isolated_read": exc.evidence},
        ) from exc
    payload = isolated.payload
    digest = hashlib.sha256(payload).hexdigest()
    expected_path = os.path.abspath(os.fspath(path))
    if (
        isolated.source_binding.get("path") != expected_path
        or isolated.source_binding.get("sha256") != f"sha256:{digest}"
    ):
        raise StructuredChatEvidenceError(
            f"{reason_prefix}_SOURCE_BINDING_INVALID",
            evidence={
                "source_path": str(path),
                "source_binding": isolated.source_binding,
            },
        )
    return payload, digest


def _isolated_failure_reason(error: StructuredChatEvidenceError) -> str:
    isolated_read = error.evidence.get("isolated_read")
    return str(isolated_read.get("reason_code")) if isinstance(isolated_read, Mapping) else ""


def _validated_chat_integer(
    piece: dict,
    field: str,
    *,
    required: bool,
    positive: bool = False,
) -> int | None:
    value = piece.get(field)
    if value is None:
        if required:
            raise StructuredChatEvidenceError(f"STRUCTURED_CHAT_BINDING_{field.upper()}_MISSING")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or (positive and value <= 0):
        raise StructuredChatEvidenceError(f"STRUCTURED_CHAT_BINDING_{field.upper()}_INVALID")
    return value


def _load_superchats(jsonl_path: Path) -> list[tuple[int, str, str]]:
    """(video_relative_ms, sender_uname, message) for SUPER_CHAT events in a blrec
    .jsonl event log.  SUPER_CHAT carries the EXACT full sender uname (unlike gift
    events, whose uname is masked to 小***), so both the name and the on-screen
    text she reads/thanks are recoverable ground truth.  This compatibility
    helper retains earliest-event fallback for filename-less fixtures;
    production ``_piece_chat_evidence`` uses the segment filename as t=0.
    De-duplicates the CN/JPN twin events by message text."""
    return [
        (item.offset_ms, item.sender, item.text)
        for item in load_chat_jsonl(jsonl_path)
        if item.kind == "superchat"
    ]


def _piece_chat_evidence(piece: dict) -> list[ChatEvidence]:
    """Load ordinary danmaku and SC independently from their healthy source.

    A zero-byte XML no longer disables the sibling JSONL.  When XML is healthy
    it remains the ordinary-danmaku timeline authority; JSONL still supplies
    exact SC text and is the fallback for ordinary messages.
    """

    required_value = piece.get("structured_chat_required")
    if required_value is not None and not isinstance(required_value, bool):
        raise StructuredChatEvidenceError("STRUCTURED_CHAT_BINDING_REQUIRED_FLAG_INVALID")
    structured_chat_required = required_value is True

    xml_value = piece.get("danmaku_xml_local")
    xml_path = Path(str(xml_value)) if xml_value else None
    declared_jsonl_value = piece.get("chat_jsonl_local") or piece.get("superchat_jsonl_local")
    explicit_jsonl = bool(declared_jsonl_value)
    if not declared_jsonl_value:
        if structured_chat_required:
            raise StructuredChatEvidenceError("STRUCTURED_CHAT_BINDING_PATH_MISSING")
        declared_jsonl_value = str(Path(piece["remote_media"]).with_suffix(".jsonl"))
    jsonl_path = Path(str(declared_jsonl_value))

    try:
        jsonl_payload, actual_sha256 = _isolated_source_payload(
            jsonl_path,
            reason_prefix="STRUCTURED_CHAT_BINDING",
        )
    except StructuredChatEvidenceError as exc:
        isolated_reason = _isolated_failure_reason(exc)
        if isolated_reason == "SOURCE_MISSING":
            if explicit_jsonl or structured_chat_required:
                if _structured_chat_parent_unavailable(jsonl_path):
                    raise StructuredChatEvidenceError(
                        f"SOURCE_RECORDING_ROOT_UNAVAILABLE: structured chat {jsonl_path}",
                        evidence=exc.evidence,
                    ) from exc
                raise StructuredChatEvidenceError(
                    "STRUCTURED_CHAT_BINDING_PATH_MISSING",
                    evidence=exc.evidence,
                ) from exc
            jsonl_payload = b""
            actual_sha256 = hashlib.sha256(jsonl_payload).hexdigest()
        elif isolated_reason == "SOURCE_NOT_REGULAR":
            raise StructuredChatEvidenceError(
                "STRUCTURED_CHAT_BINDING_PATH_NOT_REGULAR",
                evidence=exc.evidence,
            ) from exc
        elif isolated_reason == "SOURCE_UNREADABLE":
            raise StructuredChatEvidenceError(
                "STRUCTURED_CHAT_BINDING_PATH_UNREADABLE",
                evidence=exc.evidence,
            ) from exc
        else:
            raise

    if not jsonl_payload:
        if explicit_jsonl or structured_chat_required:
            raise StructuredChatEvidenceError("STRUCTURED_CHAT_BINDING_PATH_EMPTY")
        jsonl_items: list[ChatEvidence] = []
    else:
        expected_sha256 = _declared_chat_sha256(piece.get("chat_jsonl_sha256"))
        if piece.get("chat_jsonl_sha256") is not None and expected_sha256 is None:
            raise StructuredChatEvidenceError("STRUCTURED_CHAT_BINDING_SHA256_INVALID")
        if structured_chat_required and expected_sha256 is None:
            raise StructuredChatEvidenceError("STRUCTURED_CHAT_BINDING_SHA256_MISSING")
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise StructuredChatEvidenceError("STRUCTURED_CHAT_BINDING_SHA256_MISMATCH")

        declared_origin = _validated_chat_integer(
            piece,
            "chat_origin_epoch_ms",
            required=structured_chat_required,
            positive=True,
        )
        origin_epoch_ms = (
            declared_origin if declared_origin is not None else recording_start_epoch_ms(jsonl_path)
        )
        if structured_chat_required and origin_epoch_ms is None:
            raise StructuredChatEvidenceError(
                "STRUCTURED_CHAT_BINDING_CHAT_ORIGIN_EPOCH_MS_MISSING"
            )
        timeline_offset_ms = _validated_chat_integer(
            piece,
            "chat_timeline_offset_ms",
            required=structured_chat_required,
        )
        if timeline_offset_ms is None:
            timeline_offset_ms = 0
        try:
            loaded_items = load_chat_jsonl(
                jsonl_path,
                recording_start_ms=origin_epoch_ms,
                source_bytes=jsonl_payload,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise StructuredChatEvidenceError("STRUCTURED_CHAT_BINDING_PARSE_FAILED") from exc
        jsonl_items = [
            replace(
                item,
                offset_ms=item.offset_ms + timeline_offset_ms,
            )
            for item in loaded_items
        ]
        if (explicit_jsonl or structured_chat_required) and not jsonl_items:
            raise StructuredChatEvidenceError("STRUCTURED_CHAT_BINDING_NO_PARSEABLE_EVENTS")

    evidence: list[ChatEvidence] = []
    xml_payload: bytes | None = None
    xml_sha256 = ""
    if xml_path is not None:
        try:
            xml_payload, xml_sha256 = _isolated_source_payload(
                xml_path,
                reason_prefix="DANMAKU_XML",
            )
        except StructuredChatEvidenceError as exc:
            if _isolated_failure_reason(exc) != "SOURCE_MISSING":
                raise
    if xml_payload:
        try:
            xml_items = load_danmaku_xml(xml_path, source_bytes=xml_payload)
        except (TypeError, ValueError) as exc:
            raise StructuredChatEvidenceError(
                "DANMAKU_XML_PARSE_FAILED",
                evidence={"source_path": str(xml_path)},
            ) from exc
        evidence.extend(
            ChatEvidence(
                "danmaku",
                item.offset_ms,
                item.text,
                source=str(xml_path),
                source_sha256=xml_sha256,
            )
            for item in xml_items
        )
    else:
        evidence.extend(item for item in jsonl_items if item.kind == "danmaku")
    evidence.extend(item for item in jsonl_items if item.kind in {"superchat", "gift", "guard"})
    return evidence


def _load_independent_chat_support_srts(media_path: Path) -> list[str]:
    """Load only transcripts that never saw chat or rendered video text.

    ``.agy_refined.srt`` is deliberately excluded: that pass receives the
    structured danmaku context and source frames, so using it to prove that the
    streamer read the same message would be circular.
    """

    raw_audio_asr = media_path.with_suffix(".asr_draft.srt")
    if not raw_audio_asr.is_file():
        return []
    return [raw_audio_asr.read_text(encoding="utf-8", errors="replace")]
