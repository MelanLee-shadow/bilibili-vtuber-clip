"""Producer composition keeps the native owner's relisten ledger reader.

Rule (41 同一物理疑点的实际补听与语境收口): audio attachment/dispatch, raw
response and usable observation are separately accounted; a real relisten is
proven by the existing owner ledger, and a no-gain terminal needs that same
ledger. The producer's human/CPA composition must expose the audio chain's
read-only ``relisten_budget_state`` to ``semantic_audio_recovery``.

All media is synthetic and every model reply is a protocol stub at the normal
send boundary (see ``test_targeted_relisten_public_contract``); no provider is
reachable from this module.
"""
from __future__ import annotations

from pathlib import Path

from src.autoslice import entity_audio_verifier as audio
from src.autoslice import native_context_witness as native
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from tests import test_targeted_relisten_public_contract as relisten


def _adapters() -> pipeline.TextPipelineAdapters:
    def unused(*args, **kwargs):
        return None

    return pipeline.TextPipelineAdapters(
        build_aggregate_transcriber=unused,
        build_agy_transcriber=unused,
        load_term_boundary_surfaces=unused,
        profile_asset_file=lambda _name: Path("/__vtuber_slice_missing_asset__"),
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=unused,
        topic_graph_expected_sha256=lambda: "",
    )


def _production_verifier(tmp_path, monkeypatch):
    rig = relisten.build_offline_rig(tmp_path, monkeypatch)
    real_independent = native.build_independent_context_witnesses

    def offline_independent(**kwargs):
        # Same wrapper/owner ledger as production; only the network-backed
        # native ASR providers are absent from this offline fixture.
        return real_independent(**{**kwargs, "providers": ()})

    def no_cpa(_prompt):
        raise AssertionError("OFFLINE_UNEXPECTED_CPA_CALL")

    monkeypatch.setattr(audio, "build_local_audio_entity_verifier", lambda **_: rig.local)
    monkeypatch.setattr(native, "build_independent_context_witnesses", offline_independent)
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: no_cpa)
    context = pipeline._build_entity_verification_context(
        spec={"date": "2026-10-19"}, padded=rig.source, padded_dur=10000,
        host="localhost", text_override_path=None, cid="synthetic-protocol-case",
        out_root=rig.output, srt_text=rig.srt, authoritative_chat=[],
        adapters=_adapters(),
    )
    rig.verifier = context.verify_confusable_entity
    return rig


def test_production_composition_proves_real_relisten_dispatch(tmp_path, monkeypatch):
    rig = _production_verifier(tmp_path, monkeypatch)
    assert callable(getattr(rig.verifier, "relisten_budget_state", None))
    witness = rig.verifier(build_witness_request(rig.check))
    assert witness["status"] == "OBSERVED", witness
    finding = relisten.pending_finding(rig, rig.check, witness)

    _, audit, _ = relisten.acquire_range(rig, finding, rig.check, relisten.observe_plan())

    assert audit["status"] == "OBSERVED", audit.get("reason_code")
    assert audit["reason_code"] == "BOUND_NEW_RANGE_CONTEXT"
    assert audit["execution_status"] == "COMPLETED_USABLE_OBSERVATION"
    assert audit["budget_before"] is not None and audit["budget_after"] is not None
    dispatched = audit["provider_dispatches"]
    assert dispatched and all(row["status"] == "OBSERVED" for row in dispatched)
    assert audit["provider_calls_this_attempt"] == len(dispatched)
    assert len(rig.transport.calls) == 2


def test_production_composition_reaches_no_gain_terminal_ledger(tmp_path, monkeypatch):
    rig = _production_verifier(tmp_path, monkeypatch)
    witness, check, finding, _ = relisten.three_observations(rig)

    _, audit, _ = relisten.acquire_range(rig, finding, check, relisten.no_gain_plan)

    assert audit["status"] == "CONTEXT_REVIEW", audit.get("reason_code")
    assert audit["reason_code"] == "SEMANTIC_RANGE_NO_NEW_INFORMATION"
    relisten.policy.validate_no_gain(audit["no_gain_proof"], audit["terminal_check_request"])
    assert audit["provider_calls_this_attempt"] == 0


def test_relisten_dispatch_proof_reads_branch_qualified_v3_account(tmp_path, monkeypatch):
    """41 实际补听证明 on a reconciled (v3) owner account.

    A v3 account composes immutable branches whose attempt ids are
    branch-local (identity = branch receipt hash + attempt_id). A real
    relisten reservation on the active branch can reuse an id an original
    branch already holds (OCI3 e9c50824 sister: active 50..55 vs incoming
    27..59); it is still this call's dispatch and must be proven as such.
    """
    import hashlib
    import json

    from src.autoslice.native_audio_budget_receipt import (
        _require_budget_history_extension, persist_native_audio_budget_receipt)
    from src.autoslice.semantic_audio_recovery import _dispatched_since
    from src.autoslice.supplement_audio_budget import get_budget
    from tests.test_native_audio_history_reconciliation import _auto, _fork

    state = _fork(tmp_path, monkeypatch)
    source, _, canonical, _, _ = state
    _auto(state)
    before = json.loads(canonical.budget_receipt.read_bytes())
    composed = before["budget"]["attempts"]
    assert len({r["attempt_id"] for r in composed}) < len(composed)  # Ids repeat across branches.
    token = get_budget(source).consume("moss", "moss-transcribe-diarize-pro", 15000, 16000)
    assert token in {r["attempt_id"] for r in composed}  # The collision under test.
    get_budget(source).finish_attempt(token, status="OBSERVED")
    persist_native_audio_budget_receipt(
        source_media=source, source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=canonical.budget_receipt)
    after = json.loads(canonical.budget_receipt.read_bytes())
    _require_budget_history_extension(before, after)

    dispatched = _dispatched_since(before, after)

    branch = after["budget"]["account_history"]["canonical"]["receipt_sha256"]
    assert [(r["attempt_id"], r["provider"], r["start_ms"], r["status"], r["history_branch_sha256"])
            for r in dispatched] == [(token, "moss", 15000, "OBSERVED", branch)]
    assert all(row in after["budget"]["attempts"] for row in dispatched)  # Same composed identity.
    assert _dispatched_since(after, after) == []  # Nothing new, nothing claimed.


def test_production_composition_names_the_candidates_own_producer_media(tmp_path, monkeypatch):
    """41:1631-1634 (F5): the composition exposes the media its native
    witnesses read, so a native receipt on it can be proven by its own
    producer provenance (re-validated by the projection, never trusted)."""
    rig = _production_verifier(tmp_path, monkeypatch)
    assert rig.verifier.relisten_own_source_media == str(rig.source)
