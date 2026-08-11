"""Hash-bound receipt for the approved centrality eligibility policy.

This module is deliberately orchestration-neutral: it does not call an LLM,
rank candidates, produce media, or upload anything.  It seals an already
validated semantic assessment to exact candidate/speaker/content inputs and
can revalidate those bytes at a future production choke point.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

from src.autoslice import centrality_policy as cp
from src.autoslice.channel_profile import load_channel_profile


_PROFILE = load_channel_profile(Path(__file__).resolve().parents[2])
SCHEMA_VERSION = f"{_PROFILE.profile_id}-centrality-selection-receipt.v1"
PURPOSE = "SELECTION_ELIGIBILITY_ONLY_NOT_UPLOAD_AUTHORITY"

SPEAKER_AUTHORITY_PROVISIONAL = "PROVISIONAL_MACHINE"
SPEAKER_AUTHORITY_CALIBRATED = "CALIBRATED_MACHINE"
SPEAKER_AUTHORITY_HUMAN = "HUMAN_REVIEWED"
SPEAKER_AUTHORITIES = frozenset(
    {
        SPEAKER_AUTHORITY_PROVISIONAL,
        SPEAKER_AUTHORITY_CALIBRATED,
        SPEAKER_AUTHORITY_HUMAN,
    }
)
TRUSTED_SPEAKER_AUTHORITIES = frozenset({SPEAKER_AUTHORITY_CALIBRATED, SPEAKER_AUTHORITY_HUMAN})

EFFECTIVE_SHADOW_ONLY = "SHADOW_ONLY_SPEAKER_CALIBRATION_REQUIRED"
_SHA256_RE = re.compile(r"(?:sha256:)?[0-9a-fA-F]{64}")


class CentralityReceiptError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CentralityReceiptError("CENTRALITY_RECEIPT_NON_CANONICAL_VALUE") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _sha256(value: object, *, reason_code: str) -> str:
    text = str(value or "")
    if not _SHA256_RE.fullmatch(text):
        raise CentralityReceiptError(reason_code)
    return "sha256:" + text.lower().removeprefix("sha256:")


def _normalize_candidate(
    *,
    candidate_id: str,
    candidate_start_ms: int,
    candidate_end_ms: int,
    resolved_boundary_sha256: str,
    source_media_sha256: str,
    asr_sha256: str,
) -> dict[str, object]:
    candidate_id = candidate_id.strip()
    if (
        not candidate_id
        or isinstance(candidate_start_ms, bool)
        or not isinstance(candidate_start_ms, int)
        or isinstance(candidate_end_ms, bool)
        or not isinstance(candidate_end_ms, int)
        or candidate_start_ms < 0
        or candidate_end_ms <= candidate_start_ms
    ):
        raise CentralityReceiptError("CENTRALITY_RECEIPT_CANDIDATE_INVALID")
    return {
        "candidate_id": candidate_id,
        "start_ms": candidate_start_ms,
        "end_ms": candidate_end_ms,
        "resolved_boundary_sha256": _sha256(
            resolved_boundary_sha256,
            reason_code="CENTRALITY_RECEIPT_BOUNDARY_SHA256_INVALID",
        ),
        "source_media_sha256": _sha256(
            source_media_sha256,
            reason_code="CENTRALITY_RECEIPT_MEDIA_SHA256_INVALID",
        ),
        "asr_sha256": _sha256(
            asr_sha256,
            reason_code="CENTRALITY_RECEIPT_ASR_SHA256_INVALID",
        ),
    }


def _normalize_legacy_shadow(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise CentralityReceiptError("CENTRALITY_RECEIPT_LEGACY_SHADOW_INVALID")
    try:
        copied = json.loads(
            json.dumps(
                raw,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise CentralityReceiptError("CENTRALITY_RECEIPT_LEGACY_SHADOW_INVALID") from exc
    assert isinstance(copied, dict)
    copied["decision_influence"] = False
    return copied


def _effective_gate(
    *,
    assessment: Mapping[str, object],
    speaker_authority: str,
    content_rank_receipt_sha256: str | None,
) -> dict[str, object]:
    base_gate = assessment.get("gate")
    if not isinstance(base_gate, Mapping):
        raise CentralityReceiptError("CENTRALITY_RECEIPT_BASE_GATE_MISSING")
    base_disposition = str(base_gate.get("disposition") or "")
    trusted_speaker = speaker_authority in TRUSTED_SPEAKER_AUTHORITIES
    blockers: list[str] = []
    if not trusted_speaker and base_disposition not in {
        cp.DISPOSITION_NOT_EVALUATED,
        cp.DISPOSITION_HUMAN_REVIEW_REQUIRED,
    }:
        blockers.append("SPEAKER_CALIBRATION_PROVISIONAL")
        effective_disposition = EFFECTIVE_SHADOW_ONLY
    else:
        effective_disposition = base_disposition
    if base_disposition == cp.DISPOSITION_AUTO_ELIGIBLE and content_rank_receipt_sha256 is None:
        blockers.append("CONTENT_RANK_V2_RECEIPT_MISSING")
    automatic_pool_eligible = bool(
        base_disposition == cp.DISPOSITION_AUTO_ELIGIBLE and trusted_speaker
    )
    automatic_selection_authorized = bool(
        automatic_pool_eligible and content_rank_receipt_sha256 is not None
    )
    return {
        "base_disposition": base_disposition,
        "effective_disposition": effective_disposition,
        "automatic_pool_eligible": automatic_pool_eligible,
        "automatic_selection_authorized": automatic_selection_authorized,
        "manual_selection_required": bool(base_gate.get("manual_selection_required")),
        "human_review_required": bool(base_gate.get("human_review_required")),
        "reason_codes": list(base_gate.get("reason_codes") or []),
        "promotion_blockers": blockers,
        "upload_authorized": False,
    }


def build_centrality_receipt(
    *,
    candidate_id: str,
    candidate_start_ms: int,
    candidate_end_ms: int,
    resolved_boundary_sha256: str,
    source_media_sha256: str,
    asr_sha256: str,
    speaker_receipt_sha256: str,
    cue_labels_sha256: str,
    speaker_authority: str,
    assessment: object,
    content_rank_receipt_sha256: str | None,
    legacy_v1_shadow: object,
) -> dict[str, object]:
    """Seal one assessment to exact inputs; never authorize upload."""

    if speaker_authority not in SPEAKER_AUTHORITIES:
        raise CentralityReceiptError("CENTRALITY_RECEIPT_SPEAKER_AUTHORITY_INVALID")
    candidate = _normalize_candidate(
        candidate_id=candidate_id,
        candidate_start_ms=candidate_start_ms,
        candidate_end_ms=candidate_end_ms,
        resolved_boundary_sha256=resolved_boundary_sha256,
        source_media_sha256=source_media_sha256,
        asr_sha256=asr_sha256,
    )
    try:
        normalized_assessment = cp.normalize_centrality_assessment(assessment)
    except cp.CentralityPolicyError as exc:
        raise CentralityReceiptError(exc.reason_code) from exc
    speaker = {
        "receipt_sha256": _sha256(
            speaker_receipt_sha256,
            reason_code="CENTRALITY_RECEIPT_SPEAKER_SHA256_INVALID",
        ),
        "cue_labels_sha256": _sha256(
            cue_labels_sha256,
            reason_code="CENTRALITY_RECEIPT_CUE_LABELS_SHA256_INVALID",
        ),
        "authority": speaker_authority,
    }
    normalized_content_receipt = (
        _sha256(
            content_rank_receipt_sha256,
            reason_code="CENTRALITY_RECEIPT_CONTENT_RANK_SHA256_INVALID",
        )
        if content_rank_receipt_sha256 is not None
        else None
    )
    legacy_shadow = _normalize_legacy_shadow(legacy_v1_shadow)
    gate = _effective_gate(
        assessment=normalized_assessment,
        speaker_authority=speaker_authority,
        content_rank_receipt_sha256=normalized_content_receipt,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "decision_policy_version": cp.DECISION_POLICY_VERSION,
        "rubric_version": cp.RUBRIC_VERSION,
        "candidate": candidate,
        "speaker": speaker,
        "centrality": normalized_assessment,
        "content_rank_v2": {
            "receipt_sha256": normalized_content_receipt,
            "centrality_rank_component": [],
        },
        "gate": gate,
        "manual_override": None,
        "legacy_v1_shadow": legacy_shadow,
    }
    return {**payload, "receipt_sha256": _canonical_sha256(payload)}


def validate_centrality_receipt(
    receipt: object,
    *,
    current_bindings: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Recompute policy and optionally compare every freshness binding."""

    if not isinstance(receipt, Mapping):
        raise CentralityReceiptError("CENTRALITY_RECEIPT_INVALID")
    payload = dict(receipt)
    declared_hash = payload.pop("receipt_sha256", None)
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("purpose") != PURPOSE
        or receipt.get("decision_policy_version") != cp.DECISION_POLICY_VERSION
        or receipt.get("rubric_version") != cp.RUBRIC_VERSION
        or _sha256(
            declared_hash,
            reason_code="CENTRALITY_RECEIPT_SELF_SHA256_INVALID",
        )
        != _canonical_sha256(payload)
    ):
        raise CentralityReceiptError("CENTRALITY_RECEIPT_INTEGRITY_FAILED")
    candidate = receipt.get("candidate")
    speaker = receipt.get("speaker")
    centrality = receipt.get("centrality")
    content_rank = receipt.get("content_rank_v2")
    legacy = receipt.get("legacy_v1_shadow")
    if (
        not isinstance(candidate, Mapping)
        or not isinstance(speaker, Mapping)
        or not isinstance(centrality, Mapping)
        or not isinstance(content_rank, Mapping)
        or not isinstance(legacy, Mapping)
        or legacy.get("decision_influence") is not False
        or receipt.get("manual_override") is not None
    ):
        raise CentralityReceiptError("CENTRALITY_RECEIPT_STRUCTURE_INVALID")
    rebuilt = build_centrality_receipt(
        candidate_id=str(candidate.get("candidate_id") or ""),
        candidate_start_ms=candidate.get("start_ms"),  # type: ignore[arg-type]
        candidate_end_ms=candidate.get("end_ms"),  # type: ignore[arg-type]
        resolved_boundary_sha256=str(candidate.get("resolved_boundary_sha256") or ""),
        source_media_sha256=str(candidate.get("source_media_sha256") or ""),
        asr_sha256=str(candidate.get("asr_sha256") or ""),
        speaker_receipt_sha256=str(speaker.get("receipt_sha256") or ""),
        cue_labels_sha256=str(speaker.get("cue_labels_sha256") or ""),
        speaker_authority=str(speaker.get("authority") or ""),
        assessment=centrality,
        content_rank_receipt_sha256=(
            str(content_rank.get("receipt_sha256"))
            if content_rank.get("receipt_sha256") is not None
            else None
        ),
        legacy_v1_shadow=legacy,
    )
    if rebuilt != dict(receipt):
        raise CentralityReceiptError("CENTRALITY_RECEIPT_POLICY_REPLAY_FAILED")
    if current_bindings is not None:
        expected = {
            "candidate_id": candidate.get("candidate_id"),
            "start_ms": candidate.get("start_ms"),
            "end_ms": candidate.get("end_ms"),
            "resolved_boundary_sha256": candidate.get("resolved_boundary_sha256"),
            "source_media_sha256": candidate.get("source_media_sha256"),
            "asr_sha256": candidate.get("asr_sha256"),
            "speaker_receipt_sha256": speaker.get("receipt_sha256"),
            "cue_labels_sha256": speaker.get("cue_labels_sha256"),
            "content_rank_receipt_sha256": content_rank.get("receipt_sha256"),
        }
        actual = dict(current_bindings)
        for key in (
            "resolved_boundary_sha256",
            "source_media_sha256",
            "asr_sha256",
            "speaker_receipt_sha256",
            "cue_labels_sha256",
        ):
            actual[key] = _sha256(
                actual.get(key),
                reason_code="CENTRALITY_RECEIPT_CURRENT_BINDING_INVALID",
            )
        if actual.get("content_rank_receipt_sha256") is not None:
            actual["content_rank_receipt_sha256"] = _sha256(
                actual.get("content_rank_receipt_sha256"),
                reason_code="CENTRALITY_RECEIPT_CURRENT_BINDING_INVALID",
            )
        if actual != expected:
            raise CentralityReceiptError("CENTRALITY_RECEIPT_STALE")
    return rebuilt
