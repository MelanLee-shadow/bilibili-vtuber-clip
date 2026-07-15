"""Load clip-local danmaku, superchat, gift, and independent ASR evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .chat_authority import ChatEvidence, load_chat_jsonl, recording_start_epoch_ms
from .danmaku_evidence import load_danmaku_xml


DANMAKU_PRE_CONTEXT_MS = 30_000

SC_PRE_CONTEXT_MS = 900_000  # include SCs up to 15min before the clip: she CLEARS THE

GIFT_PRE_CONTEXT_MS = 120_000  # thanks for a gift usually follow within a couple


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

    xml_value = piece.get("danmaku_xml_local")
    xml_path = Path(str(xml_value)) if xml_value else None
    jsonl_value = piece.get("chat_jsonl_local") or piece.get("superchat_jsonl_local")
    if not jsonl_value:
        jsonl_value = str(Path(piece["remote_media"]).with_suffix(".jsonl"))
    jsonl_path = Path(str(jsonl_value))
    segment_zero = recording_start_epoch_ms(piece["remote_media"])
    jsonl_items = load_chat_jsonl(jsonl_path, recording_start_ms=segment_zero)

    evidence: list[ChatEvidence] = []
    xml_healthy = bool(xml_path and xml_path.is_file() and xml_path.stat().st_size > 0)
    if xml_healthy and xml_path is not None:
        evidence.extend(
            ChatEvidence(
                "danmaku",
                item.offset_ms,
                item.text,
                source=str(xml_path),
                source_sha256=hashlib.sha256(xml_path.read_bytes()).hexdigest(),
            )
            for item in load_danmaku_xml(xml_path)
        )
    else:
        evidence.extend(item for item in jsonl_items if item.kind == "danmaku")
    evidence.extend(item for item in jsonl_items if item.kind == "superchat")
    evidence.extend(item for item in jsonl_items if item.kind == "gift")
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
