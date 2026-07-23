"""Build one hash-bound context artifact shared by downstream clip stages."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.chat_authority import ChatEvidence, sanitize_chat_display_text
from src.autoslice.speech_memory_ledger import load_scoped_speech_memory


SCHEMA_VERSION = "lidousha-clip-context.v1"
MAX_TRANSCRIPT_CHARS = 60_000
MAX_CHAT_ROWS = 240
MAX_PROMPT_CHARS = 18_000
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}")


class ClipContextError(ValueError):
    pass


def _canonical_json_sha256(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _bounded_text(value: str, cap: int) -> tuple[str, bool]:
    if len(value) <= cap:
        return value, False
    half = max(1, cap // 2)
    return value[:half] + "\n…[context middle omitted by explicit budget]…\n" + value[-half:], True


def build_clip_context(
    *,
    candidate_id: str,
    spec: Mapping[str, object],
    draft_srt: str,
    authoritative_chat: Sequence[ChatEvidence],
    topic_resolution: Mapping[str, object],
    session_topic_authorities: Sequence[Mapping[str, object]],
    speech_memory_ledger_path: Path,
) -> dict[str, object]:
    recording_date = str(spec.get("date") or "")
    transcript, transcript_truncated = _bounded_text(
        draft_srt, MAX_TRANSCRIPT_CHARS
    )
    scoped_memory = load_scoped_speech_memory(
        speech_memory_ledger_path,
        speaker_id="lidousha",
        channel_id="lidousha",
        recording_date=recording_date,
        candidate_id=candidate_id,
        relation_id=(
            str(relation.get("relation_id") or "")
            if isinstance(
                (relation := spec.get("session_relation_authority")), Mapping
            )
            else None
        ),
    )
    chat_rows = [
        {
            "kind": item.kind,
            "offset_ms": item.offset_ms,
            "text": sanitize_chat_display_text(item.text),
            "sender": item.sender,
            "source": item.source,
            "source_sha256": item.source_sha256,
            "source_event_id": item.source_event_id,
        }
        for item in authoritative_chat[:MAX_CHAT_ROWS]
    ]
    pieces = [
        {
            "start_ms": int(piece["start_ms"]),
            "end_ms": int(piece["end_ms"]),
            "source_media_sha256": piece.get("source_media_sha256"),
            "recording_basename": Path(str(piece.get("remote_media") or "")).name,
        }
        for piece in (spec.get("pieces") or [])
        if isinstance(piece, Mapping)
    ]
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "selection_hook": str(spec.get("selection_hook") or ""),
        "pieces": pieces,
        "session_relation_authority": spec.get("session_relation_authority"),
        "whole_clip_draft_srt": transcript,
        "whole_clip_draft_srt_sha256": "sha256:"
        + hashlib.sha256(draft_srt.encode("utf-8")).hexdigest(),
        "structured_chat": chat_rows,
        "topic_resolution": dict(topic_resolution),
        "session_topic_authorities": [dict(row) for row in session_topic_authorities],
        "speech_memory": scoped_memory,
        "retrieval_budget": {
            "whole_clip_transcript_char_cap": MAX_TRANSCRIPT_CHARS,
            "whole_clip_transcript_truncated": transcript_truncated,
            "structured_chat_row_cap": MAX_CHAT_ROWS,
            "structured_chat_total_rows": len(authoritative_chat),
            "structured_chat_truncated": len(authoritative_chat) > MAX_CHAT_ROWS,
        },
        "mutation_authorized": False,
    }
    payload["context_sha256"] = _canonical_json_sha256(payload)
    return payload


def validate_clip_context(
    context: Mapping[str, object],
    *,
    candidate_id: str | None = None,
    recording_date: str | None = None,
    source_media_sha256s: Sequence[str] | None = None,
) -> dict[str, object]:
    """Recompute the payload digest and check its execution-scope bindings."""

    if context.get("schema_version") != SCHEMA_VERSION:
        raise ClipContextError("CLIP_CONTEXT_SCHEMA_INVALID")
    payload = dict(context)
    declared = str(payload.pop("context_sha256", ""))
    if _SHA256_RX.fullmatch(declared) is None or _canonical_json_sha256(payload) != declared:
        raise ClipContextError("CLIP_CONTEXT_DIGEST_MISMATCH")
    if candidate_id is not None and context.get("candidate_id") != candidate_id:
        raise ClipContextError("CLIP_CONTEXT_CANDIDATE_MISMATCH")
    if recording_date is not None and context.get("recording_date") != recording_date:
        raise ClipContextError("CLIP_CONTEXT_DATE_MISMATCH")
    if context.get("mutation_authorized") is not False:
        raise ClipContextError("CLIP_CONTEXT_MUTATION_AUTHORITY_INVALID")
    pieces = context.get("pieces")
    if not isinstance(pieces, list) or not pieces:
        raise ClipContextError("CLIP_CONTEXT_PIECES_INVALID")
    declared_sources = {
        str(piece.get("source_media_sha256") or "")
        for piece in pieces
        if isinstance(piece, Mapping)
    }
    if not declared_sources or any(
        _SHA256_RX.fullmatch(value) is None for value in declared_sources
    ):
        raise ClipContextError("CLIP_CONTEXT_SOURCE_BINDING_INVALID")
    if source_media_sha256s is not None and declared_sources != set(source_media_sha256s):
        raise ClipContextError("CLIP_CONTEXT_SOURCE_BINDING_MISMATCH")
    return dict(context)


def clip_context_prompt_text(context: Mapping[str, object]) -> str:
    """Compact reviewer context; every line remains tied to context_sha256."""

    memory = context.get("speech_memory")
    memory_rows = memory.get("entries") if isinstance(memory, Mapping) else []
    chunks = [
        f"clip_context_sha256: {context.get('context_sha256')}",
        f"selection_hook: {context.get('selection_hook') or ''}",
        "以下长期记忆只提供候选，绝不直接授权改字；同音字仍须画面/弹幕/SC/同片复现/声学或人工 source truth：",
    ]
    for row in memory_rows or []:
        if not isinstance(row, Mapping):
            continue
        chunks.append(
            "- "
            + f"id={row.get('memory_id')}; "
            + str(row.get("kind") or "memory")
            + ": surfaces="
            + "/".join(str(value) for value in (row.get("surfaces") or []))
            + "; candidates="
            + "/".join(
                str(value) for value in (row.get("candidate_canonicals") or [])
            )
        )
    chunks.append("整片初稿（用于回指、复现和后文调侃；不是文字 authority）：")
    chunks.append(str(context.get("whole_clip_draft_srt") or ""))
    chat_rows = context.get("structured_chat") or []
    if chat_rows:
        chunks.append("结构化弹幕/SC（保留 source binding）：")
        for row in chat_rows:
            if isinstance(row, Mapping):
                chunks.append(
                    f"- {row.get('kind')} @{row.get('offset_ms')}ms: {row.get('text')}"
                )
    bounded, _ = _bounded_text("\n".join(chunks), MAX_PROMPT_CHARS)
    return bounded


def write_clip_context(path: Path, context: Mapping[str, object]) -> None:
    validate_clip_context(context)
    path.write_text(
        json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
