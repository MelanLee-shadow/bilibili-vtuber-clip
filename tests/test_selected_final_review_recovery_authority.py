from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.apply_subtitle_text_overrides import (
    TextCue,
    decision_output_witness_sha256,
    source_cue_witness_sha256,
)
from src.autoslice import selected_final_review_recovery_authority as recovery_authority
from src.autoslice.repository_asset_authority import build_deployed_authority_manifest
from src.autoslice.selected_final_review_recovery_authority import (
    SelectedFinalReviewRecoveryAuthorityError,
    build_authorized_selected_final_review_recovery_receipt,
    load_selected_final_review_recovery_authority,
    materialize_selected_final_review_recovery_scope,
    validate_selected_final_review_scope_predecessor,
    validate_selected_final_review_recovery_rejection,
)
from src.autoslice.selected_final_review_recovery import (
    RECOVERY_RECEIPT_FIELD,
    advance_selected_final_review_recovery_receipt,
    build_selected_final_review_recovery_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = (
    "auto_203011_328_389",
    "auto_220021_561_670",
)


def _canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _rejection(authority: dict[str, object]) -> dict[str, object]:
    expected = authority["expected_failure"]
    assert isinstance(expected, dict)
    return {
        "candidate_id": authority["candidate_id"],
        "status": "candidate_rejected",
        "rejected_status": "failed",
        "rc": 1,
        "selected_repair": True,
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_findings",
        "failure_recoverable": False,
        "rejection_reason": "subtitle_authority_unresolved_backfilled",
        "failure_recovery_fingerprint": expected["failure_recovery_fingerprint"],
        "failure_evidence": {
            "reviewed_srt_sha256": expected["reviewed_srt_sha256"],
            "surface_files": expected["surface_files"],
            "findings": [expected["finding"]],
        },
    }


def _state(authority: dict[str, object]) -> dict[str, object]:
    state: dict[str, object] = {"picks": [_rejection(authority)]}
    predecessor = authority["scope_predecessor"]
    assert isinstance(predecessor, dict)
    if predecessor["mode"] == "exact_replace":
        state["operator_processing_scope"] = copy.deepcopy(predecessor["scope"])
    return state


@pytest.mark.parametrize("candidate_id", CANDIDATES)
def test_sealed_single_cid_authority_materializes_only_v7_recovery(candidate_id: str):
    authority, seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id=candidate_id
    )
    assert seal.relative_path.endswith(f"{candidate_id}.v1.json")
    scope = materialize_selected_final_review_recovery_scope(
        authority,
        expires_at="2099-08-19T00:12:35Z",
    )
    assert scope["candidate_ids"] == [candidate_id]
    assert scope["upload_allowed"] is False
    assert scope["intent"] == "RECOVER_NAMED_SELECTED_FINAL_REVIEW_REJECTION"
    assert scope["user_authorization"]["timestamp"] == "2026-08-19T00:08:52.249Z"
    assert authority["source_ruling"]["user_event"]["line_sha256"] == (
        "e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa"
    )
    assert "provider_allowed" not in scope
    assert authority["permissions"] == {
        "upload_allowed": False,
        "provider_allowed": False,
        "allowed_operations": [
            "apply_exact_subtitle_text_override",
            "requeue_selected_final_review",
        ],
    }


@pytest.mark.parametrize("candidate_id", CANDIDATES)
def test_exact_authority_binds_current_rejection_and_standard_receipt(candidate_id: str):
    authority, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id=candidate_id
    )
    state = _state(authority)
    assert validate_selected_final_review_recovery_rejection(
        authority, state=state, state_date="2026-08-13"
    )["candidate_id"] == candidate_id
    receipt = build_authorized_selected_final_review_recovery_receipt(
        authority,
        state=state,
        state_date="2026-08-13",
        queued_row={"cid": candidate_id, "selected_repair": True, "segment_path": "/sealed.mp4"},
        current_fingerprint="sha256:" + "f" * 64,
    )
    assert receipt["candidate_id"] == candidate_id
    assert receipt["action"] == "REQUEUED_BY_OPERATOR_FINAL_REVIEW_GRANT"


def test_target_rejection_filters_unrelated_duplicate_lifecycle_row():
    authority, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id="auto_203011_328_389"
    )
    state = _state(authority)
    unrelated = {
        "candidate_id": authority["candidate_id"],
        "status": "candidate_rejected",
        "failure_stage": "chat_authority_finalization",
        "failure_recovery_fingerprint": "sha256:" + "0" * 64,
    }
    state["talk_superseded_attempts"] = [unrelated]
    assert validate_selected_final_review_recovery_rejection(
        authority, state=state, state_date="2026-08-13"
    ) == state["picks"][0]
    assert state["talk_superseded_attempts"] == [unrelated]


def test_target_rejection_rejects_duplicate_exact_evidence():
    authority, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id="auto_203011_328_389"
    )
    state = _state(authority)
    state["talk_superseded_attempts"] = [copy.deepcopy(state["picks"][0])]
    with pytest.raises(SelectedFinalReviewRecoveryAuthorityError, match="not unique"):
        validate_selected_final_review_recovery_rejection(
            authority, state=state, state_date="2026-08-13"
        )


def test_scope_predecessor_is_exact_for_two_and_serial_for_three():
    authority_two, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id="auto_203011_328_389"
    )
    state_two = _state(authority_two)
    scope_two = materialize_selected_final_review_recovery_scope(
        authority_two, expires_at="2099-08-19T00:08:52Z"
    )
    validate_selected_final_review_scope_predecessor(
        authority_two, repo_root=ROOT, state=state_two, scope=scope_two
    )
    state_two["operator_processing_scope"]["grant_id"] = "drift"
    with pytest.raises(SelectedFinalReviewRecoveryAuthorityError, match="predecessor"):
        validate_selected_final_review_scope_predecessor(
            authority_two, repo_root=ROOT, state=state_two, scope=scope_two
        )

    authority_three, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id="auto_220021_561_670"
    )
    scope_three = materialize_selected_final_review_recovery_scope(
        authority_three, expires_at="2099-08-19T00:08:52Z"
    )
    with pytest.raises(SelectedFinalReviewRecoveryAuthorityError, match="conflicts"):
        validate_selected_final_review_scope_predecessor(
            authority_three, repo_root=ROOT, state=_state(authority_two), scope=scope_three
        )


def _two_v7_predecessor_state(*, converged: bool) -> tuple[dict[str, object], dict[str, object]]:
    two, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id="auto_203011_328_389"
    )
    three, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id="auto_220021_561_670"
    )
    scope = materialize_selected_final_review_recovery_scope(
        two, expires_at="2099-08-19T00:08:52Z"
    )
    queue = {"cid": two["candidate_id"], "selected_repair": True, "segment_path": "/sealed.mp4"}
    receipt = build_selected_final_review_recovery_receipt(
        old_row=_rejection(two), queued_row=queue, candidate_id=str(two["candidate_id"]),
        grant_id=str(scope["grant_id"]), current_fingerprint="sha256:" + "f" * 64,
    )
    queue[RECOVERY_RECEIPT_FIELD] = receipt
    state = _state(three)
    state["operator_processing_scope"] = scope
    if not converged:
        state["pending_talk"] = [queue]
        return state, three
    terminal = {
        "cid": two["candidate_id"], "selected_repair": True, "segment_path": "/sealed.mp4",
        "status": "review_ready",
    }
    terminal[RECOVERY_RECEIPT_FIELD] = advance_selected_final_review_recovery_receipt(
        receipt, from_row=queue, to_row=terminal, candidate_id=str(two["candidate_id"]),
        grant_id=str(scope["grant_id"]), transition_kind="QUEUE_TO_PICK",
    )
    state["picks"].insert(0, terminal)
    return state, three


def test_three_accepts_only_converged_receipt_bound_two_v7_predecessor():
    state, authority_three = _two_v7_predecessor_state(converged=True)
    scope_three = materialize_selected_final_review_recovery_scope(
        authority_three, expires_at="2099-08-19T00:08:52Z"
    )
    validate_selected_final_review_scope_predecessor(
        authority_three, repo_root=ROOT, state=state, scope=scope_three,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )


def test_three_rejects_active_two_v7_fake_receipt_and_other_scope():
    active, authority_three = _two_v7_predecessor_state(converged=False)
    scope_three = materialize_selected_final_review_recovery_scope(
        authority_three, expires_at="2099-08-19T00:08:52Z"
    )
    with pytest.raises(SelectedFinalReviewRecoveryAuthorityError, match="conflicts"):
        validate_selected_final_review_scope_predecessor(
            authority_three, repo_root=ROOT, state=active, scope=scope_three,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
    forged, _authority_three = _two_v7_predecessor_state(converged=True)
    forged["picks"][0][RECOVERY_RECEIPT_FIELD]["receipt_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(SelectedFinalReviewRecoveryAuthorityError, match="conflicts"):
        validate_selected_final_review_scope_predecessor(
            authority_three, repo_root=ROOT, state=forged, scope=scope_three,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
    two, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id="auto_203011_328_389"
    )
    other = _state(authority_three)
    other["operator_processing_scope"] = two["scope_predecessor"]["scope"]
    with pytest.raises(SelectedFinalReviewRecoveryAuthorityError, match="conflicts"):
        validate_selected_final_review_scope_predecessor(
            authority_three, repo_root=ROOT, state=other, scope=scope_three,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("candidate_id", CANDIDATES)
def test_authority_rejects_wrong_state_cue_and_permissions(candidate_id: str):
    authority, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id=candidate_id
    )
    state = _state(authority)
    with pytest.raises(SelectedFinalReviewRecoveryAuthorityError, match="state date"):
        validate_selected_final_review_recovery_rejection(
            authority, state=state, state_date="2026-08-14"
        )
    bad_state = copy.deepcopy(state)
    bad_state["picks"][0]["failure_evidence"]["findings"][0]["cue_index"] = 99
    with pytest.raises(SelectedFinalReviewRecoveryAuthorityError, match="evidence"):
        validate_selected_final_review_recovery_rejection(
            authority, state=bad_state, state_date="2026-08-13"
        )
    bad_authority = copy.deepcopy(authority)
    bad_authority["permissions"]["provider_allowed"] = True
    body = {key: value for key, value in bad_authority.items() if key != "authority_sha256"}
    bad_authority["authority_sha256"] = _canonical_sha256(body)
    with pytest.raises(SelectedFinalReviewRecoveryAuthorityError, match="permissions"):
        materialize_selected_final_review_recovery_scope(
            bad_authority,
            expires_at="2099-08-19T00:12:35Z",
        )


@pytest.mark.parametrize("candidate_id", CANDIDATES)
def test_loader_rejects_tampered_override_hash(
    candidate_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    authority_relative = (
        Path("assets/lidousha/selected_final_review_recovery_authorities")
        / f"{candidate_id}.v1.json"
    )
    authority_path = tmp_path / authority_relative
    authority_path.parent.mkdir(parents=True)
    authority = json.loads((ROOT / authority_relative).read_text())
    authority["text_override"]["sha256"] = "0" * 64
    body = {key: value for key, value in authority.items() if key != "authority_sha256"}
    authority["authority_sha256"] = _canonical_sha256(body)
    authority_path.write_text(json.dumps(authority), encoding="utf-8")
    override_relative = Path(authority["text_override"]["relative_path"])
    override_path = tmp_path / override_relative
    override_path.parent.mkdir(parents=True)
    override_path.write_bytes((ROOT / override_relative).read_bytes())
    monkeypatch.setattr(
        recovery_authority,
        "require_repository_asset_authority",
        lambda **_kwargs: object(),
    )
    with pytest.raises(SelectedFinalReviewRecoveryAuthorityError, match="bytes drifted"):
        load_selected_final_review_recovery_authority(
            repo_root=tmp_path, candidate_id=candidate_id
        )


@pytest.mark.parametrize(
    ("candidate_id", "cue_index", "before", "after"),
    [
        ("auto_203011_328_389", 4, "谢谢沧老师", "先方とのお打合わせ"),
        ("auto_220021_561_670", 6, "S for high gal", "不好意思"),
    ],
)
def test_text_authority_is_exact_one_cue_no_upload(
    candidate_id: str, cue_index: int, before: str, after: str
):
    document = json.loads(
        (ROOT / "assets/lidousha/subtitle_text_overrides" / f"{candidate_id}.text.v1.json").read_text()
    )
    cues = [TextCue(index, "00:00:00,000", "00:00:00,100", "irrelevant") for index in range(1, cue_index)]
    expected = document["overrides"][0]["expect"]
    cues.append(TextCue(cue_index, expected["start"], expected["end"], before))
    assert document["upload"] is False
    assert document["overrides"] == [
        {
            "source_cue": cue_index,
            "action": "replace",
            "expect": expected,
            "text": after,
            "authority": document["overrides"][0]["authority"],
            "reason": document["overrides"][0]["reason"],
        }
    ]
    assert source_cue_witness_sha256(cues, document) == document["source_cue_witness_sha256"]
    assert decision_output_witness_sha256(cues, document) == document["decision_output_witness_sha256"]


def test_registry_is_replayed_sealed_current_asset_and_drift_changes_manifest_entry():
    registry = ROOT / "assets/lidousha/publication_registry.v1.json"
    manifest = build_deployed_authority_manifest(
        repo_root=ROOT,
        deployed_commit="9bda9309c2da7fd5bb462fca193653ddccb94e77",
        relative_paths=[registry.relative_to(ROOT)],
    )
    entry = manifest["entries"][registry.relative_to(ROOT).as_posix()]
    assert entry == {
        "bytes": len(registry.read_bytes()),
        "sha256": "sha256:300a0d1c61bc8280f57fc6a0c1a4faa6ddaec84756032899eeccaa26127a939f",
    }
    drifted = json.loads(registry.read_text())
    next(row for row in drifted["entries"] if row["candidate_id"] == "auto_123655_771_844")["bvid"] = "BV1drifted"
    assert hashlib.sha256(
        (json.dumps(drifted, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    ).hexdigest() != entry["sha256"].removeprefix("sha256:")
