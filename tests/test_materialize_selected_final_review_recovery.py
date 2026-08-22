from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import materialize_selected_final_review_recovery as materializer
from src.autoslice.selected_final_review_recovery import (
    RECOVERY_RECEIPT_FIELD,
    advance_selected_final_review_recovery_receipt,
    build_selected_final_review_recovery_receipt,
)
from src.autoslice.selected_final_review_recovery_authority import (
    load_selected_final_review_recovery_authority,
    materialize_selected_final_review_recovery_scope,
)


ROOT = Path(__file__).resolve().parents[1]
DEPLOYED = "9bda9309c2da7fd5bb462fca193653ddccb94e77"


class _NoProviderRuntime:
    calls = 0

    @staticmethod
    def talk_failure_recovery_fingerprint(kind: str, candidate_id: str) -> str:
        assert kind == "subtitle_authority"
        assert candidate_id in {"auto_203011_328_389", "auto_220021_561_670"}
        return "sha256:" + "f" * 64

    @classmethod
    def requeue_recoverable_talks(cls, date: str, state: dict, *, candidate_ids: tuple[str, ...]):
        cls.calls += 1
        candidate_id = candidate_ids[0]
        old = state["picks"].pop()
        queue = {
            "cid": candidate_id,
            "segment_path": "/sealed/source.mp4",
            "start_ms": 1,
            "end_ms": 2,
            "selected_repair": True,
        }
        queue[RECOVERY_RECEIPT_FIELD] = build_selected_final_review_recovery_receipt(
            old_row=old,
            queued_row=queue,
            candidate_id=candidate_id,
            grant_id=state["operator_processing_scope"]["grant_id"],
            current_fingerprint="sha256:" + "f" * 64,
        )
        state["pending_talk"] = [queue]
        return 1


def _state(authority: dict[str, object]) -> dict[str, object]:
    expected = authority["expected_failure"]
    state = {
        "picks": [
            {
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
        ],
        "pending_talk": [],
    }
    predecessor = authority["scope_predecessor"]
    assert isinstance(predecessor, dict)
    if predecessor["mode"] == "exact_replace":
        state["operator_processing_scope"] = predecessor["scope"]
    return state


def _prepared_inputs(tmp_path: Path, candidate_id: str):
    authority, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id=candidate_id
    )
    runtime_root = tmp_path / "runtime"
    state_path = runtime_root / "state" / "2026-08-13.json"
    state_path.parent.mkdir(parents=True)
    state = _state(authority)
    predecessor = authority["scope_predecessor"]
    assert isinstance(predecessor, dict)
    if predecessor["mode"] == "exact_converged_final_review_v7":
        two, _seal = load_selected_final_review_recovery_authority(
            repo_root=ROOT, candidate_id="auto_203011_328_389"
        )
        scope = materialize_selected_final_review_recovery_scope(
            two, expires_at="2099-08-19T00:08:52Z"
        )
        queue = {"cid": two["candidate_id"], "selected_repair": True, "segment_path": "/sealed.mp4"}
        receipt = build_selected_final_review_recovery_receipt(
            old_row=_state(two)["picks"][0], queued_row=queue,
            candidate_id=two["candidate_id"], grant_id=scope["grant_id"],
            current_fingerprint="sha256:" + "f" * 64,
        )
        queue[RECOVERY_RECEIPT_FIELD] = receipt
        terminal = {
            "cid": two["candidate_id"], "selected_repair": True,
            "segment_path": "/sealed.mp4", "status": "review_ready",
        }
        terminal[RECOVERY_RECEIPT_FIELD] = advance_selected_final_review_recovery_receipt(
            receipt, from_row=queue, to_row=terminal, candidate_id=two["candidate_id"],
            grant_id=scope["grant_id"], transition_kind="QUEUE_TO_PICK",
        )
        state["picks"].insert(0, terminal)
        state["operator_processing_scope"] = scope
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return authority, runtime_root, state_path


@pytest.mark.parametrize("candidate_id", ("auto_203011_328_389", "auto_220021_561_670"))
def test_prepare_and_apply_are_provider_free_exact_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_id: str
):
    authority, runtime_root, state_path = _prepared_inputs(tmp_path, candidate_id)
    monkeypatch.setattr(materializer, "_read_deployed_commit", lambda _root: DEPLOYED)
    _NoProviderRuntime.calls = 0
    prepared = materializer.prepare_recovery(
        repo_root=ROOT,
        runtime_root=runtime_root,
        state_path=state_path,
        candidate_id=candidate_id,
        expected_deployed_commit=DEPLOYED,
        expires_at="2099-08-19T00:08:52Z",
        runtime=_NoProviderRuntime,
    )
    assert _NoProviderRuntime.calls == 1
    assert prepared.authority == authority
    assert state_path.read_bytes() == prepared.before_bytes
    applied = materializer.apply_recovery(
        repo_root=ROOT,
        runtime_root=runtime_root,
        state_path=state_path,
        candidate_id=candidate_id,
        expected_deployed_commit=DEPLOYED,
        expires_at="2099-08-19T00:08:52Z",
        runtime=_NoProviderRuntime,
    )
    assert _NoProviderRuntime.calls == 2
    assert json.loads(state_path.read_text()) == applied.after_state
    sidecar = json.loads(applied.sidecar.read_text())
    assert sidecar["provider_called"] is False
    assert sidecar["upload_allowed"] is False
    assert sidecar["status"] == "PREPARED"
    assert sidecar["intent"] == "MATERIALIZE_SELECTED_FINAL_REVIEW_RECOVERY"
    assert sidecar["recovery_receipt_sha256"] == applied.receipt["receipt_sha256"]
    resumed = materializer.apply_recovery(
        repo_root=ROOT,
        runtime_root=runtime_root,
        state_path=state_path,
        candidate_id=candidate_id,
        expected_deployed_commit=DEPLOYED,
        expires_at="2099-08-19T00:08:52Z",
        runtime=_NoProviderRuntime,
    )
    assert resumed.before_bytes is None
    assert resumed.state_before_sha256 == sidecar["state_before_sha256"]


def test_preflight_rejects_deployed_state_and_candidate_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _authority, runtime_root, state_path = _prepared_inputs(tmp_path, "auto_203011_328_389")
    monkeypatch.setattr(materializer, "_read_deployed_commit", lambda _root: "0" * 40)
    with pytest.raises(materializer.SelectedFinalReviewRecoveryMaterializationError, match="DEPLOYED_COMMIT_DRIFT"):
        materializer.prepare_recovery(
            repo_root=ROOT, runtime_root=runtime_root, state_path=state_path,
            candidate_id="auto_203011_328_389", expected_deployed_commit=DEPLOYED,
            expires_at="2099-08-19T00:08:52Z", runtime=_NoProviderRuntime,
        )


def test_near_live_unrelated_rejection_is_preserved_and_exact_scope_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    authority, runtime_root, state_path = _prepared_inputs(tmp_path, "auto_203011_328_389")
    state = json.loads(state_path.read_text())
    unrelated = {
        "candidate_id": authority["candidate_id"],
        "status": "candidate_rejected",
        "failure_stage": "chat_authority_finalization",
        "failure_recovery_fingerprint": "sha256:" + "0" * 64,
    }
    state["talk_superseded_attempts"] = [unrelated]
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(materializer, "_read_deployed_commit", lambda _root: DEPLOYED)
    applied = materializer.apply_recovery(
        repo_root=ROOT, runtime_root=runtime_root, state_path=state_path,
        candidate_id="auto_203011_328_389", expected_deployed_commit=DEPLOYED,
        expires_at="2099-08-19T00:08:52Z", runtime=_NoProviderRuntime,
    )
    assert applied.after_state["talk_superseded_attempts"] == [unrelated]
    assert applied.after_state["operator_processing_scope"]["schema_version"] == "operator-processing-scope-grant.v7"


def test_conflicting_active_scope_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    authority, runtime_root, state_path = _prepared_inputs(tmp_path, "auto_220021_561_670")
    two, _seal = load_selected_final_review_recovery_authority(
        repo_root=ROOT, candidate_id="auto_203011_328_389"
    )
    state = json.loads(state_path.read_text())
    state["operator_processing_scope"] = two["scope_predecessor"]["scope"]
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(materializer, "_read_deployed_commit", lambda _root: DEPLOYED)
    with pytest.raises(materializer.SelectedFinalReviewRecoveryMaterializationError, match="SCOPE_PREDECESSOR_CONFLICT"):
        materializer.prepare_recovery(
            repo_root=ROOT, runtime_root=runtime_root, state_path=state_path,
            candidate_id=authority["candidate_id"], expected_deployed_commit=DEPLOYED,
            expires_at="2099-08-19T00:08:52Z", runtime=_NoProviderRuntime,
        )


def test_prepared_sidecar_resumes_after_state_cas_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _authority, runtime_root, state_path = _prepared_inputs(tmp_path, "auto_203011_328_389")
    monkeypatch.setattr(materializer, "_read_deployed_commit", lambda _root: DEPLOYED)
    original = materializer.write_exact_state_under_lease
    monkeypatch.setattr(materializer, "write_exact_state_under_lease", lambda **_kwargs: (_ for _ in ()).throw(OSError("injected")))
    values = {
        "repo_root": ROOT, "runtime_root": runtime_root, "state_path": state_path,
        "candidate_id": "auto_203011_328_389", "expected_deployed_commit": DEPLOYED,
        "expires_at": "2099-08-19T00:08:52Z", "runtime": _NoProviderRuntime,
    }
    with pytest.raises(materializer.SelectedFinalReviewRecoveryMaterializationError, match="STATE_CAS_FAILED"):
        materializer.apply_recovery(**values)
    sidecar = next((runtime_root / "state").glob("*.selected-final-review-recovery.json"))
    assert json.loads(sidecar.read_text())["status"] == "PREPARED"
    monkeypatch.setattr(materializer, "write_exact_state_under_lease", original)
    applied = materializer.apply_recovery(**values)
    assert json.loads(state_path.read_text()) == applied.after_state


def test_mismatched_prepared_sidecar_refuses_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _authority, runtime_root, state_path = _prepared_inputs(tmp_path, "auto_203011_328_389")
    monkeypatch.setattr(materializer, "_read_deployed_commit", lambda _root: DEPLOYED)
    prepared = materializer.prepare_recovery(
        repo_root=ROOT, runtime_root=runtime_root, state_path=state_path,
        candidate_id="auto_203011_328_389", expected_deployed_commit=DEPLOYED,
        expires_at="2099-08-19T00:08:52Z", runtime=_NoProviderRuntime,
    )
    document = materializer._sidecar_document(prepared)
    document["authority_sha256"] = "sha256:" + "0" * 64
    materializer._create_sidecar(prepared.sidecar, document)
    with pytest.raises(materializer.SelectedFinalReviewRecoveryMaterializationError, match="SIDECAR_MISMATCH"):
        materializer.apply_recovery(
            repo_root=ROOT, runtime_root=runtime_root, state_path=state_path,
            candidate_id="auto_203011_328_389", expected_deployed_commit=DEPLOYED,
            expires_at="2099-08-19T00:08:52Z", runtime=_NoProviderRuntime,
        )
    monkeypatch.setattr(materializer, "_read_deployed_commit", lambda _root: DEPLOYED)
    state = json.loads(state_path.read_text())
    state["picks"].append(dict(state["picks"][0]))
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(materializer.SelectedFinalReviewRecoveryMaterializationError, match="REJECTION_DRIFT"):
        materializer.prepare_recovery(
            repo_root=ROOT, runtime_root=runtime_root, state_path=state_path,
            candidate_id="auto_203011_328_389", expected_deployed_commit=DEPLOYED,
            expires_at="2099-08-19T00:08:52Z", runtime=_NoProviderRuntime,
        )
