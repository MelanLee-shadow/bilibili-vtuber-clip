"""Build one hash-bound context artifact shared by downstream clip stages."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, TypeVar

from src.autoslice.chat_authority import ChatEvidence, sanitize_chat_display_text
from src.autoslice.speech_memory_ledger import load_scoped_speech_memory


SCHEMA_VERSION = "lidousha-clip-context.v1"
MAX_TRANSCRIPT_CHARS = 60_000
MAX_CHAT_ROWS = 240
MAX_PROMPT_CHARS = 18_000
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}")
_T = TypeVar("_T")


class ClipContextError(ValueError):
    pass


@dataclass(frozen=True)
class ClipContextPromptRender:
    text: str
    section_chars: Mapping[str, int]


def _canonical_json_sha256(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _temporal_sample(values: Sequence[_T], cap: int) -> list[_T]:
    if cap <= 0 or not values:
        return []
    if len(values) <= cap:
        return list(values)
    if cap == 1:
        return [values[len(values) // 2]]
    indexes = {
        round(index * (len(values) - 1) / (cap - 1))
        for index in range(cap)
    }
    return [values[index] for index in sorted(indexes)]


def select_context_chat(
    evidence: Sequence[ChatEvidence],
    *,
    cap: int = MAX_CHAT_ROWS,
) -> list[ChatEvidence]:
    """Keep source-bound high-value events before sampling ordinary danmaku."""

    ordered = sorted(
        evidence,
        key=lambda item: (
            int(item.offset_ms),
            str(item.kind),
            str(item.source_event_id or ""),
        ),
    )
    strong = [item for item in ordered if item.kind != "danmaku"]
    ordinary = [item for item in ordered if item.kind == "danmaku"]
    if len(strong) >= cap:
        selected = _temporal_sample(strong, cap)
    else:
        selected = [
            *strong,
            *_temporal_sample(ordinary, cap - len(strong)),
        ]
    return sorted(
        selected,
        key=lambda item: (
            int(item.offset_ms),
            str(item.kind),
            str(item.source_event_id or ""),
        ),
    )


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
    if len(draft_srt) > MAX_TRANSCRIPT_CHARS:
        raise ClipContextError("CLIP_CONTEXT_WHOLE_CLIP_TRANSCRIPT_TOO_LARGE")
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
    selected_chat = select_context_chat(authoritative_chat)
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
        for item in selected_chat
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
        "whole_clip_draft_srt": draft_srt,
        "whole_clip_draft_srt_sha256": "sha256:"
        + hashlib.sha256(draft_srt.encode("utf-8")).hexdigest(),
        "structured_chat": chat_rows,
        "topic_resolution": dict(topic_resolution),
        "session_topic_authorities": [dict(row) for row in session_topic_authorities],
        "speech_memory": scoped_memory,
        "retrieval_budget": {
            "whole_clip_transcript_char_cap": MAX_TRANSCRIPT_CHARS,
            "whole_clip_transcript_truncated": False,
            "structured_chat_row_cap": MAX_CHAT_ROWS,
            "structured_chat_total_rows": len(authoritative_chat),
            "structured_chat_selected_rows": len(selected_chat),
            "structured_chat_truncated": len(selected_chat) < len(authoritative_chat),
            "structured_chat_selection_policy": (
                "all_sc_gift_guard_then_temporal_danmaku_sampling"
            ),
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
    transcript = context.get("whole_clip_draft_srt")
    retrieval_budget = context.get("retrieval_budget")
    if (
        not isinstance(transcript, str)
        or not isinstance(retrieval_budget, Mapping)
        or retrieval_budget.get("whole_clip_transcript_truncated") is not False
        or len(transcript) > MAX_TRANSCRIPT_CHARS
        or context.get("whole_clip_draft_srt_sha256")
        != "sha256:" + hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    ):
        raise ClipContextError("CLIP_CONTEXT_WHOLE_CLIP_TRANSCRIPT_INVALID")
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


def _render_selected_blocks(blocks: Sequence[str], budget: int) -> str:
    if budget <= 0 or not blocks:
        return ""

    def render(indexes: set[int]) -> str:
        chunks: list[str] = []
        previous = -1
        for index in sorted(indexes):
            if index > previous + 1:
                chunks.append(
                    f"…[omitted blocks {previous + 2}-{index}]…"
                )
            chunks.append(blocks[index])
            previous = index
        if previous < len(blocks) - 1:
            chunks.append(
                f"…[omitted blocks {previous + 2}-{len(blocks)}]…"
            )
        return "\n".join(chunks)

    full = "\n".join(blocks)
    if len(full) <= budget:
        return full
    anchors = {0, len(blocks) // 2, len(blocks) - 1}
    selected: set[int] = set()
    for index in sorted(anchors):
        proposal = {*selected, index}
        if len(render(proposal)) <= budget:
            selected = proposal
    if not selected:
        raise ClipContextError("CLIP_CONTEXT_PROMPT_SECTION_TOO_LARGE")
    while True:
        missing_runs: list[tuple[int, int]] = []
        start: int | None = None
        for index in range(len(blocks)):
            if index not in selected and start is None:
                start = index
            if index in selected and start is not None:
                missing_runs.append((start, index - 1))
                start = None
        if start is not None:
            missing_runs.append((start, len(blocks) - 1))
        candidates = [
            (end - start + 1, (start + end) // 2)
            for start, end in missing_runs
        ]
        added = False
        for _span, index in sorted(candidates, reverse=True):
            proposal = {*selected, index}
            if len(render(proposal)) <= budget:
                selected = proposal
                added = True
                break
        if not added:
            break
    return render(selected)


def _json_line(label: str, value: object) -> str:
    return f"{label}: " + json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def render_clip_context_prompt(
    context: Mapping[str, object],
    *,
    max_chars: int = MAX_PROMPT_CHARS,
) -> ClipContextPromptRender:
    """Render mandatory authority plus cue-aware bounded evidence sections."""

    validate_clip_context(context)
    mandatory = "\n".join(
        [
            f"clip_context_sha256: {context.get('context_sha256')}",
            f"candidate_id: {context.get('candidate_id')}",
            f"recording_date: {context.get('recording_date')}",
            f"selection_hook: {context.get('selection_hook') or ''}",
            _json_line("source_pieces", context.get("pieces")),
            _json_line(
                "session_relation_authority",
                context.get("session_relation_authority"),
            ),
            _json_line("topic_resolution", context.get("topic_resolution")),
            _json_line(
                "session_topic_authorities",
                context.get("session_topic_authorities"),
            ),
        ]
    )
    if len(mandatory) + 96 >= max_chars:
        raise ClipContextError("CLIP_CONTEXT_PROMPT_MANDATORY_TOO_LARGE")
    available = max_chars - len(mandatory) - 32
    transcript_budget = int(available * 0.60)
    chat_budget = int(available * 0.25)
    memory_budget = available - transcript_budget - chat_budget

    transcript = str(context.get("whole_clip_draft_srt") or "")
    transcript_blocks = [
        block.strip()
        for block in re.split(r"\n\s*\n", transcript)
        if block.strip()
    ]
    transcript_section = _render_selected_blocks(
        [
            "整片初稿（用于回指、复现和后文调侃；不是文字 authority）：",
            *transcript_blocks,
        ],
        transcript_budget,
    )
    unused_transcript = max(0, transcript_budget - len(transcript_section))

    chat_rows = [
        row
        for row in (context.get("structured_chat") or [])
        if isinstance(row, Mapping)
    ]
    chat_blocks = ["结构化弹幕/SC（保留 source binding）："]
    chat_blocks.extend(
        (
            f"- {row.get('kind')} @{row.get('offset_ms')}ms"
            f" event={row.get('source_event_id')}: {row.get('text')}"
        )
        for row in chat_rows
    )
    chat_section = _render_selected_blocks(
        chat_blocks,
        chat_budget + unused_transcript // 2,
    )
    unused_chat = max(
        0, chat_budget + unused_transcript // 2 - len(chat_section)
    )

    memory = context.get("speech_memory")
    memory_rows = memory.get("entries") if isinstance(memory, Mapping) else []
    memory_blocks = [
        "以下长期记忆只提供候选，绝不直接授权改字；同音字仍须画面/弹幕/SC/同片复现/声学或人工 source truth："
    ]
    for row in memory_rows or []:
        if isinstance(row, Mapping):
            memory_blocks.append(
                "- "
                + f"id={row.get('memory_id')}; "
                + str(row.get("kind") or "memory")
                + ": surfaces="
                + "/".join(
                    str(value) for value in (row.get("surfaces") or [])
                )
                + "; candidates="
                + "/".join(
                    str(value)
                    for value in (
                        row.get("candidate_canonicals") or []
                    )
                )
            )
    memory_section = _render_selected_blocks(
        memory_blocks,
        memory_budget + unused_transcript - unused_transcript // 2 + unused_chat,
    )
    text = "\n".join(
        section
        for section in (
            mandatory,
            memory_section,
            transcript_section,
            chat_section,
        )
        if section
    )
    if len(text) > max_chars:
        raise ClipContextError("CLIP_CONTEXT_PROMPT_BUDGET_EXCEEDED")
    return ClipContextPromptRender(
        text=text,
        section_chars={
            "mandatory": len(mandatory),
            "memory": len(memory_section),
            "transcript": len(transcript_section),
            "chat": len(chat_section),
        },
    )


def clip_context_prompt_text(context: Mapping[str, object]) -> str:
    return render_clip_context_prompt(context).text


def write_clip_context(path: Path, context: Mapping[str, object]) -> None:
    validate_clip_context(context)
    path.write_text(
        json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
