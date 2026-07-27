"""Shared fact contract for selection, subtitles, titles, and covers."""

from __future__ import annotations

import hashlib
import re
from typing import Mapping

from src.autoslice.clip_context import clip_context_prompt_text, validate_clip_context


SCHEMA_VERSION = "lidousha-story-contract.v1"
_RELATION_CLAIM_RX = re.compile(r"联动|连麦|连线|当面对质|当面追问|搭档")
_NANCHO_CANONICAL_RX = re.compile(r"南町nightin|南町|大N|小N", re.IGNORECASE)
_NANCHO_SUSPECT_RX = re.compile(r"大恩(?:老师)?|大卫老师|大黄老师|邓老师")
_NANCHO_FALSE_POSITIVE_RX = re.compile(r"大恩大德|滴水之恩|涌泉相报|泉水之恩")
_SOURCE_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}")


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonicalize_relation_summary(
    text: str,
    *,
    session_relation_authority: object,
) -> str:
    """Canonicalize generated summaries, never source transcript mentions."""

    if not isinstance(session_relation_authority, Mapping) or session_relation_authority.get(
        "state"
    ) != "CONFIRMED":
        return text
    output = text
    for match in reversed(list(_NANCHO_SUSPECT_RX.finditer(output))):
        context = output[max(0, match.start() - 6) : match.end() + 6]
        if _NANCHO_FALSE_POSITIVE_RX.search(context):
            continue
        output = output[: match.start()] + "南町" + output[match.end() :]
    return output


def cover_relation_prompt(story_contract: object) -> str:
    """Disclose a relation without hallucinating an unreferenced counterpart."""

    if not isinstance(story_contract, Mapping) or story_contract.get(
        "relation_state"
    ) != "CONFIRMED":
        return ""
    participants = [
        str(row.get("display_name") or row.get("canonical_id") or "")
        for row in (story_contract.get("participants") or [])
        if isinstance(row, Mapping)
    ]
    names = " and ".join(value for value in participants if value)
    event = str(story_contract.get("selection_hook") or "").strip()
    event_line = (
        f" The source-bound clip event summary is: {event}. Use it only for pose, "
        "layout, and narrative emphasis; never use it to invent an identity."
        if event
        else ""
    )
    if story_contract.get("cover_counterpart_reference_available") is True:
        return (
            " STORY CONTRACT: this clip belongs to a confirmed live collaboration"
            + (f" between {names}." if names else ".")
            + " A hash-bound source frame verifies every depicted participant. "
            "Preserve both real on-stream character identities and their relative "
            "positions; do not add, replace, or invent another VTuber/person."
            + event_line
        )
    return (
        " STORY CONTRACT: this clip belongs to a confirmed live collaboration"
        + (f" between {names}." if names else ".")
        + " No verified counterpart character reference is supplied. Render an honest "
        "HOST-ONLY composition: do NOT invent, guess, or draw a second VTuber/person; "
        "express the relationship only with abstract conversational tension, arrows, "
        "speech-bubble shapes, or paired graphic motifs."
        + event_line
    )


def build_story_contract(
    *,
    candidate_id: str,
    selection_hook: str,
    transcript_text: str,
    selection_scorecard: object,
    session_relation_authority: object,
    cover_reference_authority: object = None,
    source_media_sha256s: object = None,
    clip_context: object = None,
    recording_date: str | None = None,
    boundary_semantic_review: object = None,
    human_boundary_authority: str | None = None,
) -> dict[str, object]:
    relation = (
        dict(session_relation_authority)
        if isinstance(session_relation_authority, Mapping)
        else None
    )
    relation_state = str((relation or {}).get("state") or "UNKNOWN")
    participants = list((relation or {}).get("participants") or [])
    entity_required = bool(
        _NANCHO_CANONICAL_RX.search(selection_hook)
        or _NANCHO_SUSPECT_RX.search(selection_hook)
    )
    cover_reference = (
        dict(cover_reference_authority)
        if isinstance(cover_reference_authority, Mapping)
        else None
    )
    referenced_participants = set(
        str(value)
        for value in ((cover_reference or {}).get("visible_participant_ids") or [])
    )
    participant_ids = {
        str(row.get("canonical_id") or "")
        for row in participants
        if isinstance(row, Mapping)
    }
    counterpart_reference_available = bool(
        cover_reference
        and len(referenced_participants) >= 2
        and referenced_participants <= participant_ids
    )
    verified_source_media_sha256s = sorted(
        {
            str(value)
            for value in (
                source_media_sha256s
                if isinstance(source_media_sha256s, (list, tuple, set))
                else []
            )
            if _SOURCE_SHA256_RX.fullmatch(str(value)) is not None
        }
    )
    valid_clip_context = None
    if isinstance(clip_context, Mapping):
        valid_clip_context = validate_clip_context(
            clip_context,
            candidate_id=candidate_id,
            recording_date=recording_date,
            source_media_sha256s=verified_source_media_sha256s,
        )
    speech_memory = (
        valid_clip_context.get("speech_memory")
        if isinstance(valid_clip_context, Mapping)
        else None
    )
    clip_context_binding = (
        {
            "schema_version": valid_clip_context.get("schema_version"),
            "context_sha256": valid_clip_context.get("context_sha256"),
            "whole_clip_draft_srt_sha256": valid_clip_context.get(
                "whole_clip_draft_srt_sha256"
            ),
            "speech_memory_ledger_sha256": (
                speech_memory.get("ledger_sha256")
                if isinstance(speech_memory, Mapping)
                else None
            ),
            "mutation_authorized": False,
        }
        if valid_clip_context is not None
        else None
    )
    boundary_review = (
        dict(boundary_semantic_review)
        if isinstance(boundary_semantic_review, Mapping)
        else None
    )
    if boundary_review is not None and str(
        boundary_review.get("candidate_id") or candidate_id
    ) != candidate_id:
        boundary_review = {
            "schema_version": boundary_review.get("schema_version"),
            "status": "BLOCK",
            "reason_codes": ["BOUNDARY_REVIEW_CANDIDATE_MISMATCH"],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "selection_hook": selection_hook,
        "selection_hook_sha256": _sha256_text(selection_hook),
        "transcript_sha256": _sha256_text(transcript_text),
        "selection_scorecard": (
            dict(selection_scorecard)
            if isinstance(selection_scorecard, Mapping)
            else None
        ),
        "session_relation_authority": relation,
        "relation_state": relation_state,
        "participants": participants,
        "required_entity_ids": ["nancho"] if entity_required else [],
        "nancho_accepted_surfaces": ["南町", "大N", "小N", "南町nightin"],
        "relation_claim_allowed": relation_state == "CONFIRMED",
        "cover_counterpart_reference_available": counterpart_reference_available,
        "cover_reference_authority": cover_reference,
        "source_media_sha256s": verified_source_media_sha256s,
        "clip_context_binding": clip_context_binding,
        "clip_context_prompt": (
            clip_context_prompt_text(valid_clip_context)
            if valid_clip_context is not None
            else ""
        ),
        "boundary_semantic_review": boundary_review,
        "human_boundary_authority": (
            str(human_boundary_authority).strip()
            if str(human_boundary_authority or "").strip()
            else None
        ),
        "cover_fallback_mode": (
            "VERIFIED_DUAL_STREAM_FRAME"
            if counterpart_reference_available
            else (
                "HOST_ONLY_RELATION_EXPLICIT"
                if relation_state == "CONFIRMED"
                else "HOST_ONLY_GENERIC"
            )
        ),
    }


def audit_story_artifact(
    text: str,
    *,
    story_contract: Mapping[str, object],
    artifact_kind: str,
) -> dict[str, object]:
    violations: list[dict[str, object]] = []
    relation_claim = bool(_RELATION_CLAIM_RX.search(text))
    # 关系声明门只管「生成物」（hook/标题/封面文案）的捏造风险；字幕是
    # 她口播的逐字实录（2026-07-27 1533 案：「看看联动这边」），源语保真
    # 高于关系权威，且 uniform_host 裁定说话人不确定绝不拒发。字幕里的
    # 声明词只披露不拦截。
    if (
        relation_claim
        and story_contract.get("relation_claim_allowed") is not True
        and artifact_kind != "subtitle"
    ):
        violations.append(
            {
                "reason_code": "UNCONFIRMED_RELATION_CLAIM",
                "artifact_kind": artifact_kind,
            }
        )
    for match in _NANCHO_SUSPECT_RX.finditer(text):
        context = text[max(0, match.start() - 6) : match.end() + 6]
        if _NANCHO_FALSE_POSITIVE_RX.search(context):
            continue
        violations.append(
            {
                "reason_code": "NANCHO_ALIAS_UNRESOLVED",
                "artifact_kind": artifact_kind,
                "surface": match.group(0),
                "start": match.start(),
                "end": match.end(),
            }
        )
    if (
        artifact_kind == "title"
        and "nancho" in (story_contract.get("required_entity_ids") or [])
        and not _NANCHO_CANONICAL_RX.search(text)
    ):
        violations.append(
            {
                "reason_code": "REQUIRED_NANCHO_ENTITY_MISSING_FROM_TITLE",
                "artifact_kind": artifact_kind,
            }
        )
    return {
        "schema_version": "story-artifact-audit.v1",
        "artifact_kind": artifact_kind,
        "status": "PASS" if not violations else "FAIL",
        "text_sha256": _sha256_text(text),
        "violations": violations,
    }
