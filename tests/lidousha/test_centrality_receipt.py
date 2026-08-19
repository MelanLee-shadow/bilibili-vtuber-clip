from __future__ import annotations

import copy

import pytest

from src.autoslice import centrality_policy as cp
from src.autoslice import centrality_receipt as cr


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _evidence(label: str, cue: int) -> dict[str, object]:
    return {
        "cue_id": cue,
        "start_ms": cue * 1_000,
        "end_ms": cue * 1_000 + 900,
        "speaker_label": label,
        "claim": f"key contribution {cue}",
    }


def _assessment(level: int) -> dict[str, object]:
    return {
        "status": cp.ASSESSMENT_SCORED,
        "speaker_status": cp.SPEAKER_VERIFIED,
        "level": level,
        "host_speech_share_band": "mixed",
        "evidence": [
            _evidence(cp.HOST_SPEAKER_LABEL, 1),
            _evidence(cp.OTHER_SPEAKER_LABEL, 2),
        ],
        "counterfactual": "Removing the host removes the decisive reaction.",
        "reason": "The host supplies the central turn.",
    }


def _build(
    level: int = 4,
    *,
    authority: str = cr.SPEAKER_AUTHORITY_CALIBRATED,
    content_rank: str | None = None,
    legacy: object = None,
) -> dict[str, object]:
    return cr.build_centrality_receipt(
        candidate_id="candidate-a",
        candidate_start_ms=1_000,
        candidate_end_ms=8_000,
        resolved_boundary_sha256=_hash("a"),
        source_media_sha256=_hash("b"),
        asr_sha256=_hash("c"),
        speaker_receipt_sha256=_hash("d"),
        cue_labels_sha256=_hash("e"),
        speaker_authority=authority,
        assessment=_assessment(level),
        content_rank_receipt_sha256=content_rank,
        legacy_v1_shadow=({"centrality": 4, "effective_score": 99.0} if legacy is None else legacy),
    )


def _current_bindings(*, content_rank: str | None = None) -> dict[str, object]:
    return {
        "candidate_id": "candidate-a",
        "start_ms": 1_000,
        "end_ms": 8_000,
        "resolved_boundary_sha256": _hash("a"),
        "source_media_sha256": _hash("b"),
        "asr_sha256": _hash("c"),
        "speaker_receipt_sha256": _hash("d"),
        "cue_labels_sha256": _hash("e"),
        "content_rank_receipt_sha256": content_rank,
    }


@pytest.mark.parametrize(
    ("level", "disposition", "automatic_pool", "manual"),
    [
        (0, cp.DISPOSITION_AUTO_INELIGIBLE, False, False),
        (1, cp.DISPOSITION_AUTO_INELIGIBLE, False, False),
        (2, cp.DISPOSITION_MANUAL_SELECTION_ONLY, False, True),
        (3, cp.DISPOSITION_AUTO_ELIGIBLE, True, False),
        (4, cp.DISPOSITION_AUTO_ELIGIBLE, True, False),
    ],
)
def test_receipt_preserves_approved_policy_table(
    level: int, disposition: str, automatic_pool: bool, manual: bool
) -> None:
    receipt = _build(level, content_rank=_hash("f"))
    assert receipt["gate"]["base_disposition"] == disposition
    assert receipt["gate"]["automatic_pool_eligible"] is automatic_pool
    assert receipt["gate"]["manual_selection_required"] is manual
    assert receipt["gate"]["upload_authorized"] is False
    assert receipt["content_rank_v2"]["centrality_rank_component"] == []


def test_provisional_speaker_can_only_produce_shadow_decision() -> None:
    receipt = _build(
        4,
        authority=cr.SPEAKER_AUTHORITY_PROVISIONAL,
        content_rank=_hash("f"),
    )
    assert receipt["gate"]["base_disposition"] == cp.DISPOSITION_AUTO_ELIGIBLE
    assert receipt["gate"]["effective_disposition"] == cr.EFFECTIVE_SHADOW_ONLY
    assert receipt["gate"]["automatic_pool_eligible"] is False
    assert receipt["gate"]["automatic_selection_authorized"] is False
    assert "SPEAKER_CALIBRATION_PROVISIONAL" in receipt["gate"]["promotion_blockers"]


def test_content_rank_receipt_is_required_for_automatic_selection() -> None:
    receipt = _build(4, content_rank=None)
    assert receipt["gate"]["automatic_pool_eligible"] is True
    assert receipt["gate"]["automatic_selection_authorized"] is False
    assert "CONTENT_RANK_V2_RECEIPT_MISSING" in receipt["gate"]["promotion_blockers"]


def test_levels_three_and_four_never_enter_rank_component() -> None:
    three = _build(3, content_rank=_hash("f"))
    four = _build(4, content_rank=_hash("f"))
    assert three["gate"] == four["gate"]
    assert three["content_rank_v2"] == four["content_rank_v2"]


def test_legacy_v1_mutation_changes_audit_hash_but_not_decision() -> None:
    low = _build(
        4,
        content_rank=_hash("f"),
        legacy={"centrality": 0, "effective_score": 1.0},
    )
    high = _build(
        4,
        content_rank=_hash("f"),
        legacy={"centrality": 4, "effective_score": 999.0},
    )
    assert low["gate"] == high["gate"]
    assert low["content_rank_v2"] == high["content_rank_v2"]
    assert low["receipt_sha256"] != high["receipt_sha256"]
    assert low["legacy_v1_shadow"]["decision_influence"] is False
    assert high["legacy_v1_shadow"]["decision_influence"] is False


def test_integrity_and_freshness_replay_passes() -> None:
    receipt = _build(4, content_rank=_hash("f"))
    assert (
        cr.validate_centrality_receipt(
            receipt, current_bindings=_current_bindings(content_rank=_hash("f"))
        )
        == receipt
    )


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("candidate_id", "candidate-b"),
        ("start_ms", 999),
        ("end_ms", 8_001),
        ("resolved_boundary_sha256", _hash("0")),
        ("source_media_sha256", _hash("1")),
        ("asr_sha256", _hash("2")),
        ("speaker_receipt_sha256", _hash("3")),
        ("cue_labels_sha256", _hash("4")),
        ("content_rank_receipt_sha256", _hash("5")),
    ],
)
def test_any_current_binding_drift_makes_receipt_stale(key: str, replacement: object) -> None:
    receipt = _build(4, content_rank=_hash("f"))
    bindings = _current_bindings(content_rank=_hash("f"))
    bindings[key] = replacement
    with pytest.raises(cr.CentralityReceiptError, match="CENTRALITY_RECEIPT_STALE"):
        cr.validate_centrality_receipt(receipt, current_bindings=bindings)


def test_tampered_payload_fails_integrity() -> None:
    receipt = _build(4, content_rank=_hash("f"))
    receipt["centrality"]["level"] = 0
    with pytest.raises(cr.CentralityReceiptError):
        cr.validate_centrality_receipt(receipt)


def test_rehashed_but_policy_inconsistent_payload_fails_replay() -> None:
    receipt = _build(4, content_rank=_hash("f"))
    tampered = copy.deepcopy(receipt)
    tampered["gate"]["upload_authorized"] = True
    payload = dict(tampered)
    payload.pop("receipt_sha256")
    tampered["receipt_sha256"] = cr._canonical_sha256(payload)
    with pytest.raises(
        cr.CentralityReceiptError,
        match="CENTRALITY_RECEIPT_POLICY_REPLAY_FAILED",
    ):
        cr.validate_centrality_receipt(tampered)
