"""Candidate-private adapter for C6's exhaustive unchanged-boundary ruling."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "docs/reviews/auto_120032_753_816-content-anchor-evidence-proposal.v1.json"
_CID = "auto_120032_753_816"
_SRT_SHA = "sha256:3cfcf2954c1d32a078e99aaff5585213b5edbbedd6db8f87d62097d06775ea77"
_INTERVAL = (753000, 816000)
_FROZEN = {
    "source_media_sha256": "sha256:0f0770a9426c48fee5457cf9639a77e5a477f41ec806f4e4789e19c336e1aae5",
    "record_sha256": "sha256:04b64d9a1e19810948fa0f6cca90d132a4464cc518cdbb26ace9ed0f0876222f",
    "candidate_media_sha256": "sha256:982e0fea91ef222302ec5cb30a965d0222164a87f4f3a3bc53e445048919de35",
    "reviewed_srt_sha256": _SRT_SHA,
    "speaker_ass_sha256": "sha256:f78a94d7154fd538de45c4bde41e5d85eb5cf97c099d6e3666666f7dd754e63e",
    "boundary_audit_sha256": "sha256:5231b6319b2bfa827ce3b404da7fcf3a7c33ea226314a0cfefde8195b1c5aec1",
    "operator_decision_ledger_sha256": "sha256:ecef045ba999ca94f3afa58a57562ce728fd004e9dc0d03cec949e0f27f737f3",
}
_ROOT_KEYS = frozenset(
    {
        "schema_version", "accepted", "candidate_id", "recording_date", "boundary_change",
        "provider_authorized", "upload_authorized", "line947_exhaustive_ruling",
        "frozen_delivery_binding", "superseded_automated_continuation_receipt",
        "activation_contract", "root_acceptance", "canonical_self_hash",
    }
)
_RULING_KEYS = frozenset(
    {"ruling_line_sha256", "named_subtitle_cue", "exact_text", "exact_title", "exact_cover_lines", "unnamed_boundary_bytes"}
)
_RECEIPT_KEYS = frozenset(
    {"status", "review_scope", "recommended_end_cue_index", "recommended_end_ms", "final_media_end_ms", "disposition"}
)
_ACTIVATION_KEYS = frozenset(
    {"requires_root_acceptance", "requires_exact_candidate_date_source_interval_and_reviewed_srt_hash", "only_supersedes_reason_codes", "must_not_change_generic_boundary_gate", "must_not_extend_or_shorten_boundary", "must_not_authorize_provider_or_upload"}
)
_ACCEPTANCE_KEYS = frozenset({"reviewed_by", "reviewed_at", "decision_basis"})
_AUTHORIZED_REASONS = frozenset({"CONTENT_ANCHOR_NOT_COVERED", "CONTENT_ANCHOR_COVERED_NOT_PROVEN"})


def _canonical(value: Mapping[str, object]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "canonical_self_hash"}
    raw = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _is_accepted_authority(raw: Mapping[str, object]) -> bool:
    binding = raw.get("frozen_delivery_binding")
    ruling = raw.get("line947_exhaustive_ruling")
    receipt = raw.get("superseded_automated_continuation_receipt")
    activation = raw.get("activation_contract")
    acceptance = raw.get("root_acceptance")
    return bool(
        set(raw) == _ROOT_KEYS
        and raw.get("canonical_self_hash") == _canonical(raw)
        and raw.get("schema_version") == "c6-line947-exhaustive-unchanged-boundary-authority-proposal.v1"
        and raw.get("accepted") is True
        and raw.get("candidate_id") == _CID
        and raw.get("recording_date") == "2026-08-14"
        and raw.get("boundary_change") is False
        and raw.get("provider_authorized") is False
        and raw.get("upload_authorized") is False
        and isinstance(binding, Mapping)
        and set(binding) == set(_FROZEN) | {"source_interval"}
        and binding.get("source_interval") == {"absolute_start_ms": 753000, "absolute_end_ms": 816000}
        and all(binding.get(key) == value for key, value in _FROZEN.items())
        and isinstance(ruling, Mapping)
        and set(ruling) == _RULING_KEYS
        and ruling.get("ruling_line_sha256") == "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa"
        and ruling.get("named_subtitle_cue") == 17
        and ruling.get("exact_text") == "哦，是昨天的视频。へぇ、なるほどね。"
        and ruling.get("exact_title") == "【李豆沙】弹幕写错《坏结果》，小李却只听过《大结果》"
        and ruling.get("exact_cover_lines") == ["弹幕错写坏结果", "小李只听过大结果"]
        and ruling.get("unnamed_boundary_bytes") == "FROZEN"
        and isinstance(receipt, Mapping)
        and set(receipt) == _RECEIPT_KEYS
        and receipt == {
            "status": "PASS", "review_scope": "source_full_window", "recommended_end_cue_index": 35,
            "recommended_end_ms": 86960, "final_media_end_ms": 87360,
            "disposition": "SUPERSEDED_FOR_THIS_CANDIDATE_ONLY_NOT_FALSIFIED",
        }
        and isinstance(activation, Mapping)
        and set(activation) == _ACTIVATION_KEYS
        and activation.get("requires_root_acceptance") is True
        and activation.get("requires_exact_candidate_date_source_interval_and_reviewed_srt_hash") is True
        and activation.get("only_supersedes_reason_codes") == sorted(_AUTHORIZED_REASONS)
        and all(activation.get(key) is True for key in _ACTIVATION_KEYS - {"requires_root_acceptance", "requires_exact_candidate_date_source_interval_and_reviewed_srt_hash", "only_supersedes_reason_codes"})
        and isinstance(acceptance, Mapping)
        and set(acceptance) == _ACCEPTANCE_KEYS
        and acceptance == {
            "reviewed_by": "Codex root",
            "reviewed_at": "2026-08-24T23:42:04Z",
            "decision_basis": "Ivan line947 exhaustively named only cue17/title/cover for C6 and authorized direct fastlane upload; unnamed boundary bytes remain frozen; automated continuation is superseded for this candidate only, not falsified.",
        }
    )


def load_accepted_authority() -> dict[str, object] | None:
    """Load only a root-accepted, self-hashed C6 proposal; never infer assent."""

    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not _is_accepted_authority(raw):
        return None
    return raw


def supersede_content_anchor_block(
    *,
    review: Mapping[str, object],
    candidate_id: str,
    recording_date: str | None,
    source_final_start_ms: int,
    source_final_end_ms: int,
    delivery_srt_sha256: str | None,
    authority: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """Return a bound final-delivery PASS only for the accepted C6 authority.

    This is deliberately narrower than a semantic projection: it cannot alter
    media endpoints and only supersedes an automated content-anchor reopening
    for the exact frozen delivery bytes.
    """

    active = dict(authority) if authority is not None else load_accepted_authority()
    if not active or not _is_accepted_authority(active):
        return None
    if not (
        candidate_id == _CID
        and recording_date == "2026-08-14"
        and (source_final_start_ms, source_final_end_ms) == _INTERVAL
        and delivery_srt_sha256 == _SRT_SHA
        and active.get("candidate_id") == _CID
        and active.get("boundary_change") is False
        and active.get("accepted") is True
    ):
        return None
    reasons = review.get("reason_codes")
    reason_set = frozenset(str(reason) for reason in reasons) if isinstance(reasons, list) else frozenset()
    if not (
        reason_set
        and reason_set <= _AUTHORIZED_REASONS
        and review.get("status") == "BLOCK"
        and review.get("review_scope") == "final_delivery"
        and all(review.get(key) is True for key in ("syntax_complete", "story_closed", "next_topic_separated", "next_topic_witness_valid"))
        and review.get("content_anchor_covered") is False
        and review.get("same_topic_continues_after_target") is False
        and review.get("needs_more_context") is False
    ):
        return None
    result = dict(review)
    result.update(
        {
            "status": "PASS",
            "content_anchor_covered": True,
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "next_topic_witness_valid": True,
            "same_topic_continues_after_target": False,
            "needs_more_context": False,
            "retry_scope": "none",
            "reason_codes": [
                "C6_LINE947_EXHAUSTIVE_UNCHANGED_BOUNDARY_SUPERSEDES_AUTOMATED_CONTENT_ANCHOR_REOPEN"
            ],
            "c6_line947_authority": {
                "canonical_self_hash": active["canonical_self_hash"],
                "boundary_change": False,
                "superseded_receipt": active["superseded_automated_continuation_receipt"],
            },
        }
    )
    return result


def apply_content_anchor_override(
    review: Mapping[str, object],
    candidate_id: str,
    recording_date: str | None,
    source_interval: tuple[int, int],
    delivery_srt_sha256: str | None,
) -> dict[str, object]:
    """Apply the C6 exception, or preserve the prior generic review verbatim."""

    override = supersede_content_anchor_block(
        review=review,
        candidate_id=candidate_id,
        recording_date=recording_date,
        source_final_start_ms=source_interval[0],
        source_final_end_ms=source_interval[1],
        delivery_srt_sha256=delivery_srt_sha256,
    )
    return override if override is not None else dict(review)
