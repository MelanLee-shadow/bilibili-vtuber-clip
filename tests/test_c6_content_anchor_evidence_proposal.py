"""C6's root-review proposal is inert until accepted, and byte-bound after."""

import hashlib
import json
from pathlib import Path

from src.autoslice.c6_exhaustive_boundary_authority import (
    load_accepted_authority,
    supersede_content_anchor_block,
)


PROPOSAL = Path("docs/reviews/auto_120032_753_816-content-anchor-evidence-proposal.v1.json")
SRT = Path("assets/lidousha/reviewed_subtitle_baselines/auto_120032_753_816.reviewed.srt")


def _seal(document: dict[str, object]) -> dict[str, object]:
    unsigned = dict(document)
    unsigned.pop("canonical_self_hash", None)
    document["canonical_self_hash"] = "sha256:" + hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return document


def _blocked_review() -> dict[str, object]:
    return {
        "status": "BLOCK",
        "review_scope": "final_delivery",
        "reason_codes": ["CONTENT_ANCHOR_NOT_COVERED"],
        "content_anchor_covered": False,
        "syntax_complete": True,
        "story_closed": True,
        "next_topic_separated": True,
        "next_topic_witness_valid": True,
        "same_topic_continues_after_target": False,
        "needs_more_context": False,
    }


def test_c6_proposal_is_root_accepted_and_hash_sealed() -> None:
    proposal = json.loads(PROPOSAL.read_text(encoding="utf-8"))

    assert proposal["accepted"] is True
    assert proposal["canonical_self_hash"] == _seal(dict(proposal))["canonical_self_hash"]
    assert proposal["boundary_change"] is False
    assert proposal["provider_authorized"] is False
    assert proposal["upload_authorized"] is False
    assert proposal["frozen_delivery_binding"]["reviewed_srt_sha256"] == (
        "sha256:" + hashlib.sha256(SRT.read_bytes()).hexdigest()
    )
    assert proposal["line947_exhaustive_ruling"]["named_subtitle_cue"] == 17
    assert proposal["line947_exhaustive_ruling"]["unnamed_boundary_bytes"] == "FROZEN"
    assert proposal["superseded_automated_continuation_receipt"]["disposition"] == (
        "SUPERSEDED_FOR_THIS_CANDIDATE_ONLY_NOT_FALSIFIED"
    )
    assert proposal["root_acceptance"] == {
        "reviewed_by": "Codex root",
        "reviewed_at": "2026-08-24T23:42:04Z",
        "decision_basis": "Ivan line947 exhaustively named only cue17/title/cover for C6 and authorized direct fastlane upload; unnamed boundary bytes remain frozen; automated continuation is superseded for this candidate only, not falsified.",
    }
    assert load_accepted_authority() is not None
    adapter_source = Path("src/autoslice/c6_exhaustive_boundary_authority.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess" not in adapter_source
    assert "requests" not in adapter_source


def test_c6_accepted_authority_supersedes_only_the_named_automated_reopen() -> None:
    authority = _seal(json.loads(PROPOSAL.read_text(encoding="utf-8")))
    authority["accepted"] = True
    _seal(authority)

    review = supersede_content_anchor_block(
        review=_blocked_review(),
        candidate_id="auto_120032_753_816",
        recording_date="2026-08-14",
        source_final_start_ms=753000,
        source_final_end_ms=816000,
        delivery_srt_sha256="sha256:" + hashlib.sha256(SRT.read_bytes()).hexdigest(),
        authority=authority,
    )

    assert review is not None
    assert review["status"] == "PASS"
    assert review["c6_line947_authority"]["boundary_change"] is False
    assert review["reason_codes"] == [
        "C6_LINE947_EXHAUSTIVE_UNCHANGED_BOUNDARY_SUPERSEDES_AUTOMATED_CONTENT_ANCHOR_REOPEN"
    ]


def test_c6_near_miss_hash_or_interval_cannot_supersede_gate() -> None:
    authority = _seal(json.loads(PROPOSAL.read_text(encoding="utf-8")))
    authority["accepted"] = True
    _seal(authority)

    assert supersede_content_anchor_block(
        review=_blocked_review(), candidate_id="auto_120032_753_816", recording_date="2026-08-14",
        source_final_start_ms=753000, source_final_end_ms=816001,
        delivery_srt_sha256="sha256:" + hashlib.sha256(SRT.read_bytes()).hexdigest(), authority=authority,
    ) is None


def test_c6_same_candidate_with_wrong_recording_date_cannot_supersede_gate() -> None:
    authority = _seal(json.loads(PROPOSAL.read_text(encoding="utf-8")))
    authority["accepted"] = True
    _seal(authority)

    assert supersede_content_anchor_block(
        review=_blocked_review(),
        candidate_id="auto_120032_753_816",
        recording_date="2026-08-15",
        source_final_start_ms=753000,
        source_final_end_ms=816000,
        delivery_srt_sha256="sha256:" + hashlib.sha256(SRT.read_bytes()).hexdigest(),
        authority=authority,
    ) is None


def test_c6_combined_or_incompatible_failure_cannot_be_swallowed() -> None:
    authority = _seal(json.loads(PROPOSAL.read_text(encoding="utf-8")))
    authority["accepted"] = True
    _seal(authority)
    inputs = {
        "candidate_id": "auto_120032_753_816",
        "recording_date": "2026-08-14",
        "source_final_start_ms": 753000,
        "source_final_end_ms": 816000,
        "delivery_srt_sha256": "sha256:" + hashlib.sha256(SRT.read_bytes()).hexdigest(),
        "authority": authority,
    }
    combined = _blocked_review()
    combined["reason_codes"].append("PROVIDER_ERROR")
    assert supersede_content_anchor_block(review=combined, **inputs) is None
    incompatible = _blocked_review()
    incompatible["story_closed"] = False
    assert supersede_content_anchor_block(review=incompatible, **inputs) is None
    extra_field = _seal(dict(authority))
    extra_field["unreviewed_expansion"] = True
    _seal(extra_field)
    assert supersede_content_anchor_block(review=_blocked_review(), authority=extra_field, **{
        key: value for key, value in inputs.items() if key != "authority"
    }) is None
    authority["frozen_delivery_binding"]["candidate_media_sha256"] = "sha256:" + "0" * 64
    _seal(authority)
    assert supersede_content_anchor_block(
        review=_blocked_review(), candidate_id="auto_120032_753_816", recording_date="2026-08-14",
        source_final_start_ms=753000, source_final_end_ms=816000,
        delivery_srt_sha256="sha256:" + hashlib.sha256(SRT.read_bytes()).hexdigest(), authority=authority,
    ) is None
