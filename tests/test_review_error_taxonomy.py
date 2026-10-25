"""Offline fault injection at the ordinary review entry points. No provider I/O."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from src.autoslice.review_errors import (
    ContractRejection, ProviderFailure, observation_error_record,
    program_error_boundary, review_error_record,
)

SITES = ("funnel", "native", "independent", "construction", "materials",
         "discovery", "secondary_provider", "secondary_persistence")


def _raise_injected(error):
    raise error


def injected_entry(site, tmp_path, monkeypatch, error):
    """Return the real entry and any durable owner ledger used by that entry."""
    fail = lambda *a, **kw: _raise_injected(error)
    if site == "funnel":
        from src.autoslice import exact_final_terminal_funnel as funnel
        from tests.test_exact_final_funnel import run_terminal, unresolved
        monkeypatch.setattr(funnel, "terminal_check", fail)
        return lambda: run_terminal([unresolved()], fail), None
    if site == "native":
        from tests.test_native_context_witness import _build, _request
        _, verifier, _, _ = _build(tmp_path, monkeypatch, observer=fail)
        return lambda: verifier(_request()), None
    if site in {"independent", "construction"}:
        from src.autoslice import native_context_witness as native
        from tests.test_native_context_witness import _request
        source = tmp_path / "source"
        source.write_bytes(b"source")
        monkeypatch.setattr(native, "build_native_context_verifier",
                            fail if site == "construction" else lambda **kw: fail)
        verifier = native.build_independent_context_witnesses(
            fallback=fail, source_media=source, output_dir=tmp_path / "out", providers=("moss",))
        return lambda: verifier.observe_independent_witnesses(_request()), None
    if site == "materials":
        from src.autoslice import targeted_relisten as materials
        from tests.test_targeted_relisten_public_contract import build_offline_rig
        rig = build_offline_rig(tmp_path, monkeypatch)
        monkeypatch.setattr(rig.verifier, "observe_range_context", fail)
        return lambda: materials.complete_target_materials(
            check=rig.check, finding=deepcopy(rig.finding), adapters={"entity_verifier": rig.verifier}), None
    if site == "discovery":
        from src.autoslice import producer_text_pipeline as pipeline
        monkeypatch.delenv("AUTOSLICE_DISABLE_FINAL_REVIEW", raising=False)
        return lambda: pipeline._run_exact_final_release_review(
            srt_text="1\n00:00:00,000 --> 00:00:01,000\n原文\n", correction_audit={},
            adapters=SimpleNamespace(review_glossary=fail), authoritative_chat=[],
            selection_hook="", clip_context={}), None
    from src.autoslice import subtitle_audio_evidence as secondary
    from src.autoslice import diarized_transcription as transport
    from src.autoslice.moss_transcription import MOSS_MODEL
    from src.autoslice.supplement_audio_budget import SupplementAudioBudget
    budget = SupplementAudioBudget(tmp_path / "source")

    def transcribe(audio, *, before_request, **kwargs):
        before_request()
        if site == "secondary_provider":
            _raise_injected(error)
        return {"provider": "moss", "model": MOSS_MODEL,
                "input_audio_sha256": hashlib.sha256(audio).hexdigest()}

    monkeypatch.setattr(transport, "transcribe_evidence", transcribe)
    if site == "secondary_persistence":
        monkeypatch.setattr(secondary, "atomic_write", fail)
    return lambda: secondary._observe_secondary(
        b"audio", media_path=tmp_path / "crop.mp3", provider="moss", duration_ms=1000,
        budget=budget, crop_start_ms=0, crop_end_ms=1000), budget


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("error_type", (TypeError, KeyError, AttributeError, ValueError, OSError, RuntimeError))
def test_program_fault_is_same_exception_with_stack_and_program_record(
    site, error_type, tmp_path, monkeypatch, caplog,
):
    error = error_type("cache_only IMMUTABLE_ARTIFACT_CONFLICT LOCAL_CONTRACT_ERROR")
    # A reason_code attribute cannot turn a programming error into a review outcome.
    error.reason_code = "MOSS_HTTP_ERROR"
    invoke, budget = injected_entry(site, tmp_path, monkeypatch, error)
    with pytest.raises(error_type) as caught:
        invoke()
    assert caught.value is error
    record = error.review_error
    assert record["status"] == record["reason_code"] == "PROGRAM_ERROR"
    assert record["exception_type"] == error_type.__name__
    assert any(frame["function"] == "_raise_injected" for frame in record["traceback_summary"])
    assert any(row.review_error is record for row in caplog.records if hasattr(row, "review_error"))
    assert "_raise_injected" in [frame.name for frame in __import__("traceback").extract_tb(error.__traceback__)]
    if budget is not None:
        assert budget.snapshot()["attempts"][0]["reason_code"] == "PROGRAM_ERROR"


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("category", ("contract", "provider"))
def test_declared_failures_keep_reason_and_block(site, category, tmp_path, monkeypatch):
    error = (ContractRejection("EXACT_SOURCE_BINDING_INVALID") if category == "contract"
             else ProviderFailure("MOSS_HTTP_ERROR", provider="moss", metadata={"http_status": 429}))
    invoke, budget = injected_entry(site, tmp_path, monkeypatch, error)
    if site in {"materials", "secondary_provider", "secondary_persistence"}:
        with pytest.raises((ContractRejection, ProviderFailure)) as caught:
            invoke()
        if site == "materials" and category == "provider":
            assert caught.value.material_recovery["reasons"] == ["MOSS_HTTP_ERROR"]
        else:
            assert caught.value is error
        if budget is not None:
            row = budget.snapshot()["attempts"][0]
            assert row["reason_code"] == ("SECONDARY_EVIDENCE_PERSIST_FAILED"
                                          if site == "secondary_persistence" else error.reason_code)
        return
    result = invoke()
    if site == "funnel":
        assert result["release_gate"] == "BLOCK"
        record = result["findings"][0]["terminal_funnel_error"]["review_error"]
    elif site == "discovery":
        assert result["release_gate"] == "BLOCK"
        assert result["reason_codes"] == [error.reason_code]
        record = result["discovery"]["review_error"]
    else:
        row = result[0] if isinstance(result, list) else result
        assert row["status"] == "UNCERTAIN"
        assert row["reason_code"] == error.reason_code
        record = row["review_error"]
    assert record["reason_code"] == error.reason_code
    assert record["status"] == ("CONTRACT_ERROR" if category == "contract" else "CALL_FAILURE")
    if category == "provider":
        assert record["provider"] == "moss"


def test_provider_adapter_preserves_legacy_reason_http_and_moss_unlocated(tmp_path, monkeypatch):
    from src.autoslice import diarized_transcription as transport
    from src.autoslice.diarized_transcription import DiarizedTranscriptionError
    error = DiarizedTranscriptionError("MOSS_HTTP_ERROR", "failure", {"http_status": 429})
    invoke, budget = injected_entry("secondary_provider", tmp_path, monkeypatch, error)
    with pytest.raises(ProviderFailure) as caught:
        invoke()
    assert caught.value.__cause__ is error
    assert caught.value.reason_code == error.reason_code
    assert budget.snapshot()["attempts"][0]["http_status"] == 429


def test_capability_check_does_not_invoke_cache_only_incompatible_owner(tmp_path, monkeypatch):
    from src.autoslice import targeted_relisten as materials
    from tests.test_targeted_relisten_public_contract import build_offline_rig
    rig = build_offline_rig(tmp_path, monkeypatch)
    finding = deepcopy(rig.finding)
    def first(**kwargs):
        raise ProviderFailure("TIMEOUT", provider="moss")
    monkeypatch.setattr(rig.verifier, "observe_range_context", first)
    with pytest.raises(ProviderFailure):
        materials.complete_target_materials(check=rig.check, finding=finding,
                                           adapters={"entity_verifier": rig.verifier})
    before = deepcopy(finding["target_material_attempts"][0])
    calls = []
    def legacy(*, check_request, range_witness_request):
        calls.append(check_request)
        pytest.fail("legacy owner cannot perform read-only recovery")
    monkeypatch.setattr(rig.verifier, "observe_range_context", legacy)
    with pytest.raises(ProviderFailure) as caught:
        materials.complete_target_materials(check=rig.check, finding=finding,
                                           adapters={"entity_verifier": rig.verifier})
    assert not calls
    assert caught.value.material_recovery["kind"] == "ORIGINAL_OWNER_RECOVERY_UNAVAILABLE"
    assert finding["target_material_attempts"][0] == before


def test_content_code_text_does_not_classify_typed_record():
    for text in ("LOCAL_CONTRACT_ERROR", "IMMUTABLE_ARTIFACT_CONFLICT", "DRIFT"):
        row = {"status": "UNCERTAIN", "reason_code": text, "reason_detail": text,
               "review_error": review_error_record(ProviderFailure(text, provider="moss"))}
        assert observation_error_record(row, "VALIDATOR_REASON")["status"] == "CALL_FAILURE"
    untyped = {"status": "UNCERTAIN", "reason_code": "MOSS_HTTP_ERROR"}
    assert observation_error_record(untyped, "REJECTED")["status"] == "CONTRACT_ERROR"


def test_failed_error_recording_does_not_replace_original_stack(caplog):
    error = TypeError("first")
    with pytest.raises(TypeError) as caught:
        with program_error_boundary("test", on_error=lambda row: _raise_injected(KeyError("ledger"))):
            _raise_injected(error)
    assert caught.value is error
    assert error.review_error["recording_error"]["exception_type"] == "KeyError"
    assert error.review_error["recording_error"]["status"] == "PROGRAM_ERROR"


@pytest.mark.parametrize("site", ("native", "independent", "construction", "discovery", "funnel"))
def test_existing_typed_contract_preserves_original_outcome(site, tmp_path, monkeypatch):
    """These same assertions run against 83a2e1d67, without the new handlers."""
    from src.autoslice import native_context_witness as native
    from src.autoslice.final_review_auditor import FinalReviewAuditError
    from src.autoslice.final_review_contract import FinalReviewContractError
    reason = "NATIVE_WITNESS_REQUEST_HASH_MISMATCH"
    if site == "discovery":
        error = FinalReviewAuditError(reason)
    elif site == "funnel":
        error = FinalReviewContractError(reason)
    else:
        error = native._RequestInvalid(reason)
    invoke, _ = injected_entry(site, tmp_path, monkeypatch, error)
    result = invoke()
    if site == "discovery":
        assert result["status"] == "AUDITOR_UNAVAILABLE"
        assert result["reason_codes"] == [reason]
        assert result["release_gate"] == "BLOCK"
    elif site == "funnel":
        result = result["findings"][0]["terminal_funnel_error"]
        assert result["status"] == "CONTRACT_ERROR"
        assert result["reason_code"] == reason
    else:
        row = result[0] if isinstance(result, list) else result
        assert row["status"] == "UNCERTAIN"
        assert row["reason_code"] == reason


@pytest.mark.parametrize("site", ("native", "independent", "construction", "secondary_provider"))
def test_existing_provider_type_keeps_code_and_failed_ledger(site, tmp_path, monkeypatch):
    from src.autoslice.diarized_transcription import DiarizedTranscriptionError
    error = DiarizedTranscriptionError("MOSS_HTTP_ERROR", "provider failure", {"http_status": 429})
    invoke, budget = injected_entry(site, tmp_path, monkeypatch, error)
    if budget is not None:
        with pytest.raises(DiarizedTranscriptionError) as caught:
            invoke()
        assert caught.value.reason_code == "MOSS_HTTP_ERROR"
        row = budget.snapshot()["attempts"][0]
        assert row["status"] == "FAILED"
        assert row["reason_code"] == "MOSS_HTTP_ERROR"
        assert row["http_status"] == 429
    else:
        result = invoke()
        row = result[0] if isinstance(result, list) else result
        assert row["status"] == "UNCERTAIN"
        assert row["reason_code"] == "MOSS_HTTP_ERROR"


def test_existing_typed_persistence_rejection_keeps_original_ledger(tmp_path, monkeypatch):
    from src.autoslice.argon_escalation_contract import ContractError
    error = ContractError("IMMUTABLE_ARTIFACT_CONFLICT")
    invoke, budget = injected_entry("secondary_persistence", tmp_path, monkeypatch, error)
    with pytest.raises(ValueError) as caught:
        invoke()
    assert str(caught.value) == "IMMUTABLE_ARTIFACT_CONFLICT"
    assert budget.snapshot()["attempts"][0]["reason_code"] == "SECONDARY_EVIDENCE_PERSIST_FAILED"


@pytest.mark.parametrize("case", ("memory_without_receipt", "invalid_channel_return"))
def test_deliberate_relisten_rejection_stays_on_its_finding(case, tmp_path, monkeypatch):
    """Real refusal branches: no model sends, erased ledger, or whole-clip abort.

    These outcome assertions also run on 83a2e1d67 to check the old contract.
    TASK-2 alone (104f24465) incorrectly aborts the memory-without-receipt case.
    """
    from src.autoslice.supplement_audio_budget import start_budget
    from tests.test_exact_final_funnel import run_terminal
    from tests.test_targeted_relisten_public_contract import build_offline_rig
    rig = build_offline_rig(tmp_path, monkeypatch)
    expected = {"memory_without_receipt": "RELISTEN_NATIVE_OWNER_MEMORY_WITHOUT_RECEIPT",
                "invalid_channel_return": "TARGET_MATERIAL_CHANNEL_RETURN_INVALID"}[case]
    if case == "memory_without_receipt":
        owner = start_budget(rig.source)
        owner.consume("moss", "offline-native", 0, 1000)
        snapshot = deepcopy(owner.snapshot())
    else:
        monkeypatch.setattr(rig.verifier, "observe_range_context", lambda **kwargs: {"bad": "shape"})
    finding = deepcopy(rig.finding)
    finding["exact_release_adjudication"] = {"status": "UNCERTAIN", "repaired": False,
                                            "reason_code": "JUDGE_REQUESTS_MORE_AUDIO"}
    result = run_terminal([finding], lambda _: pytest.fail("refused before judge"),
                          terminal_rig=rig, srt_text=rig.srt)
    assert result["release_gate"] == "BLOCK"
    assert len(result["findings"]) == 1
    row = result["findings"][0]
    assert row["cue_index"] == finding["cue_index"]
    error = row["terminal_funnel_error"]
    assert error["status"] == "CONTRACT_ERROR"
    assert error["reason_code"] == expected
    assert error["prior_evidence_retained"] is True
    assert not rig.transport.calls
    assert row["exact_release_adjudication"] == finding["exact_release_adjudication"]
    if case == "memory_without_receipt":
        assert owner.snapshot() == snapshot
        assert snapshot["pending_attempt_count"] == 1
        assert not (rig.output / "native-audio-budget.json").exists()
    else:
        attempt = row["target_material_attempts"][0]
        assert attempt["execution_status"] == "RETURNED"
        assert attempt["observations"] == {"bad": "shape"}


def _historical_argon_input(rig):
    from src.autoslice.context_assisted_audio import digest
    request = deepcopy(rig.check)
    request["argon_escalation_evidence"] = {"offline_probe": True}
    request.pop("request_sha256", None)
    request["request_sha256"] = digest(request)
    finding = deepcopy(rig.finding)
    finding["exact_release_adjudication"] = {"request": request, "status": "UNCERTAIN",
                                            "reason_code": "JUDGE_REQUESTS_MORE_AUDIO"}
    return finding


@pytest.mark.parametrize("site", ("scheduler_question", "historical_argon"))
@pytest.mark.parametrize("error_type", (TypeError, KeyError, AttributeError, ValueError, OSError, RuntimeError))
def test_funnel_auxiliary_boundaries_do_not_hide_program_errors(site, error_type, tmp_path, monkeypatch):
    from src.autoslice import argon_escalation_integration as argon
    from src.autoslice import final_review_auditor as auditor
    from tests.test_exact_final_funnel import run_terminal, unresolved
    from tests.test_targeted_relisten_public_contract import build_offline_rig
    error = error_type("RELISTEN_NATIVE_OWNER_MEMORY_WITHOUT_RECEIPT")
    fail = lambda *a, **kw: _raise_injected(error)
    if site == "scheduler_question":
        monkeypatch.setattr(auditor, "_candidate_blind_missing_proposal_scaffold", fail)
        invoke = lambda: run_terminal([unresolved()], lambda _: pytest.fail("no judge"))
    else:
        rig = build_offline_rig(tmp_path, monkeypatch)
        monkeypatch.setattr(argon, "transcript_witness", fail)
        invoke = lambda: run_terminal([_historical_argon_input(rig)],
            lambda _: pytest.fail("no judge"), terminal_rig=rig, srt_text=rig.srt)
    with pytest.raises(error_type) as caught:
        invoke()
    assert caught.value is error
    assert error.review_error["status"] == "PROGRAM_ERROR"
    assert any(frame["function"] == "_raise_injected" for frame in error.review_error["traceback_summary"])


@pytest.mark.parametrize("category", ("contract", "legacy_contract", "provider"))
def test_historical_argon_typed_rejection_retains_original_code(category, tmp_path, monkeypatch):
    from src.autoslice import argon_escalation_integration as argon
    from src.autoslice.argon_escalation_contract import ContractError
    from src.autoslice.exact_final_terminal_funnel import terminal_check
    from tests.test_targeted_relisten_public_contract import build_offline_rig
    rig = build_offline_rig(tmp_path, monkeypatch)
    reason = "ARGON_CONSUMER_ENVELOPE_DRIFT"
    error = {"contract": ContractRejection(reason), "legacy_contract": ContractError(reason),
             "provider": ProviderFailure(reason, provider="argon")}[category]
    monkeypatch.setattr(argon, "transcript_witness", lambda *a: _raise_injected(error))
    check = terminal_check(rig.srt, _historical_argon_input(rig), clip_context=rig.context, offset_ms=0)
    rejected = check["terminal_rejected_history"]
    assert len(rejected) == 1
    assert rejected[0]["reason_code"] == reason
    assert not rig.transport.calls


# Deliberate refusals in the terminal/relisten reference closure share one type.
REVIEW_CONTRACT_MODULES = (
    'answer_contract',
    'audience_text',
    'context_assisted_audio',
    'editorial_edit_contract',
    'editorial_subtitle',
    'entity_audio_verifier',
    'entity_witness_cache_probe',
    'exact_final_terminal_funnel',
    'exact_source_transcript_contract',
    'exact_source_transcript_provider',
    'exact_source_transcript_provider_policy',
    'exact_source_transcript_runtime',
    'final_review_auditor',
    'finding_lifecycle',
    'independent_context_adjudication',
    'local_asr_target_evidence',
    'media_copy_provenance',
    'native_context_witness',
    'native_foreign_witness',
    'prompt_materials',
    'review_evidence_store',
    'screen_read_witness',
    'semantic_request_history',
    'semantic_retry_progress',
    'semantic_terminal_adjudication',
    'span_judgment_history',
    'span_phoneme_resolution',
    'targeted_relisten',
    'terminal_legacy_v1',
)


def test_review_path_has_no_untyped_reason_code_raises():
    """Catch literals, concatenation, f-strings and named validator reasons."""
    import ast
    from pathlib import Path
    import re
    root = Path(__file__).resolve().parents[1] / "src" / "autoslice"
    prefix = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]*)+")

    def reason_code(node):
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and bool(prefix.match(node.value))
        if isinstance(node, ast.JoinedStr):
            return bool(node.values) and reason_code(node.values[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return reason_code(node.left)
        if isinstance(node, ast.BoolOp):
            return any(reason_code(value) for value in node.values)
        # Named validator results are part of the same refusal protocol;
        # a new indirection must not bypass the literal/f-string guard.
        return any(isinstance(child, ast.Name) and child.id in {
            "reason", "reason_code", "error", "exhaustion_error",
        } for child in ast.walk(node))

    violations = []
    for module in REVIEW_CONTRACT_MODULES:
        for node in ast.walk(ast.parse((root / (module + ".py")).read_text())):
            if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "ValueError"
                    and node.exc.args and reason_code(node.exc.args[0])):
                violations.append(f"{module}:{node.lineno}: {ast.unparse(node.exc)}")
    assert not violations, "untyped deliberate refusals: " + "; ".join(violations)


def test_funnel_catches_only_declared_outcome_types():
    import ast
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "src/autoslice/exact_final_terminal_funnel.py"
    handlers = [node for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.ExceptHandler)]
    assert len(handlers) == 3
    assert all(isinstance(node.type, ast.Tuple) and {ast.unparse(t) for t in node.type.elts}
               == {"ContractRejection", "ProviderFailure"} for node in handlers)
