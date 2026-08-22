"""Resolve immutable chat/context inputs for reviewed-baseline replay.

This intentionally does not repair the current candidate sidecars.  It may
recover their record-bound historical bytes only from the deployed profile's
portable delivery mirror, using the byte-identical record as the join key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RecordBoundFinalizerAuthority:
    chat: object
    clip_context: object
    source: str


def _same_sha(left: object, right: object) -> bool:
    return (
        isinstance(left, str) and isinstance(right, str)
        and _SHA256.fullmatch(left) is not None and _SHA256.fullmatch(right) is not None
        and left.removeprefix("sha256:") == right.removeprefix("sha256:")
    )


def _binding_or_none(path: object, *, label: str, regular_binding: Callable[..., object]) -> object | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        return regular_binding(Path(path), label=label)
    except Exception:
        return None


def _artifact_hashes(record: Mapping[str, object], *, error: Callable[[str], Exception]) -> tuple[str, str]:
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, Mapping):
        raise error("REPLAY_FINALIZER_RECORD_HASHES_INVALID")
    chat = hashes.get("chat_authority_audit_sha256")
    clip = hashes.get("clip_context_file_sha256")
    if not isinstance(chat, str) or not isinstance(clip, str):
        raise error("REPLAY_FINALIZER_RECORD_HASHES_INVALID")
    return chat, clip


def _validate_clip(
    binding: object, *, plan: Any, record: Mapping[str, object], source_media_sha256: str,
    load_json: Callable[..., dict[str, Any]], error: Callable[[str], Exception],
) -> None:
    from src.autoslice.clip_context import ClipContextError, validate_clip_context

    try:
        document = load_json(binding, label="CLIP_CONTEXT")
        validated = validate_clip_context(
            document, candidate_id=plan.candidate_id, recording_date=plan.date,
            source_media_sha256s=(source_media_sha256,),
        )
    except (ClipContextError, ValueError) as exc:
        raise error("REPLAY_FINALIZER_CLIP_CONTEXT_INVALID") from exc
    if not _same_sha(validated.get("context_sha256"), record.get("clip_context_payload_sha256")):
        raise error("REPLAY_FINALIZER_CLIP_CONTEXT_PAYLOAD_DRIFT")
    story = record.get("story_contract")
    binding_record = story.get("clip_context_binding") if isinstance(story, Mapping) else None
    if (
        not isinstance(binding_record, Mapping)
        or not _same_sha(binding_record.get("context_sha256"), validated.get("context_sha256"))
    ):
        raise error("REPLAY_FINALIZER_CLIP_CONTEXT_RECORD_CLOSURE_DRIFT")


def _validate_chat(
    binding: object, *, record: Mapping[str, object], load_json: Callable[..., dict[str, Any]],
    error: Callable[[str], Exception],
) -> None:
    document = load_json(binding, label="CHAT_AUTHORITY")
    hashes = record.get("artifact_hashes")
    review = document.get("final_review_audit") if isinstance(document, Mapping) else None
    structured = document.get("structured_chat_binding_audit") if isinstance(document, Mapping) else None
    if not isinstance(hashes, Mapping) or not isinstance(review, Mapping) or not isinstance(structured, Mapping):
        raise error("REPLAY_FINALIZER_CHAT_AUTHORITY_INVALID")
    subtitle = hashes.get("subtitle_sha256")
    if (
        document.get("schema_version") != "chat-authority-audit.v2"
        # ``NO_MATCH`` is the canonical terminal result when the chat proposal
        # engine found no correction to make; its final-artifact closure is
        # still just as binding as an applied verified correction.
        or document.get("status") not in {"NO_MATCH", "APPLIED_AND_VERIFIED"}
        or document.get("final_status") != "FINAL_ARTIFACTS_VERIFIED"
        or not _same_sha(document.get("final_output_srt_sha256"), subtitle)
        or not _same_sha(document.get("final_text_srt_sha256"), subtitle)
        or not _same_sha(document.get("final_speaker_srt_sha256"), hashes.get("speaker_review_srt_sha256"))
        or not _same_sha(document.get("speaker_ass_sha256"), hashes.get("ass_sha256"))
        or review.get("schema_version") != "final-review-audit.v2"
        or review.get("status") != "CLEAN"
        or not _same_sha(review.get("reviewed_srt_sha256"), subtitle)
        or structured.get("schema_version") != "structured-chat-binding-audit.v1"
        or structured.get("status") != "PASS"
    ):
        raise error("REPLAY_FINALIZER_CHAT_AUTHORITY_RECORD_CLOSURE_DRIFT")


def reconstruct_structured_chat(
    clip_context: Mapping[str, object], *, candidate_id: str, recording_date: str,
    source_media_sha256: str, error: Callable[[str], Exception],
) -> tuple[object, ...]:
    """Rebuild exact-final chat from one sealed, complete clip context."""

    from src.autoslice.chat_evidence import ChatEvidence
    from src.autoslice.clip_context import ClipContextError, validate_clip_context

    try:
        validated = validate_clip_context(
            clip_context, candidate_id=candidate_id, recording_date=recording_date,
            source_media_sha256s=(source_media_sha256,),
        )
    except ClipContextError as exc:
        raise error("REPLAY_CLIP_CONTEXT_BINDING_INVALID") from exc
    rows = validated.get("structured_chat")
    budget = validated.get("retrieval_budget")
    if (
        not isinstance(rows, list) or not isinstance(budget, Mapping)
        or budget.get("structured_chat_truncated") is not False
        or budget.get("structured_chat_selected_rows") != len(rows)
        or budget.get("structured_chat_total_rows") != len(rows)
        or not isinstance(budget.get("structured_chat_row_cap"), int)
        or budget["structured_chat_row_cap"] < len(rows)
        or budget.get("structured_chat_selection_policy")
        != "all_sc_gift_guard_then_temporal_danmaku_sampling"
    ):
        raise error("REPLAY_STRUCTURED_CHAT_BUDGET_INVALID")
    expected = {
        "kind", "offset_ms", "text", "sender", "source", "source_sha256",
        "source_event_id",
    }
    result = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != expected:
            raise error("REPLAY_STRUCTURED_CHAT_ROW_SHAPE_INVALID")
        kind = row["kind"]
        offset = row["offset_ms"]
        values = (row["text"], row["sender"], row["source"], row["source_sha256"], row["source_event_id"])
        if (
            kind not in {"danmaku", "superchat", "gift", "guard"}
            or isinstance(offset, bool) or not isinstance(offset, int)
            or any(not isinstance(value, str) for value in values)
            or not row["source"]
            or _SHA256.fullmatch(row["source_sha256"]) is None
        ):
            raise error("REPLAY_STRUCTURED_CHAT_ROW_VALUE_INVALID")
        # XML-backed danmaku has no independent event id.  Non-danmaku rows
        # must remain individually attributable.
        if not row["source_event_id"] and kind != "danmaku":
            raise error("REPLAY_STRUCTURED_CHAT_EVENT_ID_INVALID")
        result.append(ChatEvidence(
            str(kind), offset, str(row["text"]), str(row["sender"]),
            str(row["source"]), str(row["source_sha256"]), str(row["source_event_id"]),
        ))
    return tuple(result)


def _portable_date_root(
    *, runtime_authority_root: Path, date: str, safe_directory: Callable[[Path], Path],
    error: Callable[[str], Exception],
) -> Path:
    from src.autoslice.channel_profile import load_channel_profile

    runtime = safe_directory(runtime_authority_root)
    repo = safe_directory(runtime / "repo")
    try:
        root = Path(load_channel_profile(repo).delivery_root_for(repo)).absolute()
    except Exception as exc:
        raise error("REPLAY_PORTABLE_AUTHORITY_PROFILE_INVALID") from exc
    if not root.is_relative_to(repo):
        raise error("REPLAY_PORTABLE_AUTHORITY_OUTSIDE_REPO")
    return safe_directory(root / date)


def _portable_authority(
    *, plan: Any, record_binding: object, record: Mapping[str, object],
    runtime_authority_root: Path, chat_sha256: str, clip_sha256: str,
    regular_binding: Callable[..., object], safe_directory: Callable[[Path], Path],
    load_json: Callable[..., dict[str, Any]], error: Callable[[str], Exception],
) -> RecordBoundFinalizerAuthority:
    root = _portable_date_root(
        runtime_authority_root=runtime_authority_root, date=plan.date,
        safe_directory=safe_directory, error=error,
    )
    matches: list[tuple[object, dict[str, Any]]] = []
    for path in sorted(root.glob("*.record.json")):
        try:
            binding = regular_binding(path, label="PORTABLE_RECORD")
        except Exception as exc:
            raise error("REPLAY_PORTABLE_AUTHORITY_UNSAFE") from exc
        if not _same_sha(getattr(binding, "sha256", None), getattr(record_binding, "sha256", None)):
            continue
        mirror = load_json(binding, label="PORTABLE_RECORD")
        story = mirror.get("story_contract")
        hashes = mirror.get("artifact_hashes")
        if (
            not isinstance(story, Mapping) or story.get("candidate_id") != plan.candidate_id
            or not isinstance(hashes, Mapping)
            or not _same_sha(hashes.get("chat_authority_audit_sha256"), chat_sha256)
            or not _same_sha(hashes.get("clip_context_file_sha256"), clip_sha256)
            or mirror.get("chat_authority_audit_path") != str(plan.package_root / f"{plan.candidate_id}.chat-authority.json")
            or mirror.get("clip_context_path") != str(plan.package_root / f"{plan.candidate_id}.clip-context.json")
        ):
            raise error("REPLAY_PORTABLE_AUTHORITY_RECORD_IDENTITY_DRIFT")
        matches.append((binding, mirror))
    if len(matches) != 1:
        raise error("REPLAY_PORTABLE_AUTHORITY_RECORD_AMBIGUOUS")
    mirror_binding, mirror = matches[0]
    stem = Path(getattr(mirror_binding, "path")).name.removesuffix(".record.json")
    chat = regular_binding(root / f"{stem}.chat-authority.json", label="PORTABLE_CHAT_AUTHORITY")
    clip = regular_binding(root / f"{stem}.clip-context.json", label="PORTABLE_CLIP_CONTEXT")
    if not _same_sha(getattr(chat, "sha256", None), chat_sha256):
        raise error("REPLAY_FINALIZER_CHAT_AUTHORITY_DRIFT")
    if not _same_sha(getattr(clip, "sha256", None), clip_sha256):
        raise error("REPLAY_FINALIZER_CLIP_CONTEXT_DRIFT")
    publish = regular_binding(root / f"{stem}.publish.json", label="PORTABLE_PUBLISH")
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, Mapping) or not _same_sha(
        getattr(publish, "sha256", None), hashes.get("publish_draft_sha256")
    ):
        raise error("REPLAY_PORTABLE_AUTHORITY_PUBLISH_IDENTITY_DRIFT")
    publish_document = load_json(publish, label="PORTABLE_PUBLISH")
    publish_hashes = publish_document.get("artifact_hashes")
    if (
        not isinstance(publish_hashes, Mapping)
        or not _same_sha(publish_hashes.get("chat_authority_audit_sha256"), chat_sha256)
        or not _same_sha(publish_hashes.get("clip_context_file_sha256"), clip_sha256)
    ):
        raise error("REPLAY_PORTABLE_AUTHORITY_PUBLISH_CLOSURE_DRIFT")
    return RecordBoundFinalizerAuthority(chat, clip, "portable-record-mirror")


def resolve_record_bound_finalizer_authority(
    *, plan: Any, record_binding: object, record: Mapping[str, object],
    runtime_authority_root: Path, source_media_sha256: str,
    regular_binding: Callable[..., object], safe_directory: Callable[[Path], Path],
    load_json: Callable[..., dict[str, Any]], error: Callable[[str], Exception],
) -> RecordBoundFinalizerAuthority:
    """Return the only sidecar pair the record can authorize for replay."""

    chat_sha256, clip_sha256 = _artifact_hashes(record, error=error)
    direct_chat = _binding_or_none(record.get("chat_authority_audit_path"), label="CHAT_AUTHORITY", regular_binding=regular_binding)
    direct_clip = _binding_or_none(record.get("clip_context_path"), label="CLIP_CONTEXT", regular_binding=regular_binding)
    if (
        direct_chat is not None and direct_clip is not None
        and _same_sha(getattr(direct_chat, "sha256", None), chat_sha256)
        and _same_sha(getattr(direct_clip, "sha256", None), clip_sha256)
    ):
        authority = RecordBoundFinalizerAuthority(direct_chat, direct_clip, "current-candidate")
    else:
        authority = _portable_authority(
            plan=plan, record_binding=record_binding, record=record,
            runtime_authority_root=runtime_authority_root, chat_sha256=chat_sha256,
            clip_sha256=clip_sha256, regular_binding=regular_binding,
            safe_directory=safe_directory, load_json=load_json, error=error,
        )
    _validate_chat(authority.chat, record=record, load_json=load_json, error=error)
    _validate_clip(
        authority.clip_context, plan=plan, record=record,
        source_media_sha256=source_media_sha256, load_json=load_json, error=error,
    )
    return authority
