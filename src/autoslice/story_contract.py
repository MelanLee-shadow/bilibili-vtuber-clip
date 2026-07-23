"""Shared fact contract for selection, subtitles, titles, and covers."""

from __future__ import annotations

import hashlib
import re
from typing import Mapping


SCHEMA_VERSION = "lidousha-story-contract.v1"
_RELATION_CLAIM_RX = re.compile(r"联动|连麦|连线|当面对质|当面追问|搭档")
_NANCHO_CANONICAL_RX = re.compile(r"南町nightin|南町|大N|小N", re.IGNORECASE)
_NANCHO_SUSPECT_RX = re.compile(r"大恩(?:老师)?|大卫老师|大黄老师|邓老师")
_NANCHO_FALSE_POSITIVE_RX = re.compile(r"大恩大德|滴水之恩|涌泉相报|泉水之恩")


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
    return (
        " STORY CONTRACT: this clip belongs to a confirmed live collaboration"
        + (f" between {names}." if names else ".")
        + " No verified counterpart character reference is supplied. Render an honest "
        "HOST-ONLY composition: do NOT invent, guess, or draw a second VTuber/person; "
        "express the relationship only with abstract conversational tension, arrows, "
        "speech-bubble shapes, or paired graphic motifs."
    )


def build_story_contract(
    *,
    candidate_id: str,
    selection_hook: str,
    transcript_text: str,
    selection_scorecard: object,
    session_relation_authority: object,
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
        "cover_counterpart_reference_available": False,
        "cover_fallback_mode": (
            "HOST_ONLY_RELATION_EXPLICIT" if relation_state == "CONFIRMED" else "HOST_ONLY_GENERIC"
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
    if relation_claim and story_contract.get("relation_claim_allowed") is not True:
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
