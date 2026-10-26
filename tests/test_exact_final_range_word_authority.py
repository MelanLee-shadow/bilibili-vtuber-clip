from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.autoslice.exact_final_witness_authority import (
    RANGE_WORD_PROJECTION_FIELD,
    build_range_word_projection_binding,
    convergence_witness_gate,
    range_word_projection_binding_from_adjudication,
    valid_acoustic_witness_binding,
    valid_convergence_witness_gate,
    valid_range_word_projection_binding,
)


try:
    # Reuse the real file-backed native-range fixture.  Its validator reads
    # the receipt, envelope, source and raw response instead of trusting a
    # hand-built observation dictionary.
    from test_independent_context_adjudication import _native_range_artifacts
except ImportError:  # pragma: no cover - package-style pytest collection
    from tests.test_independent_context_adjudication import _native_range_artifacts


def _json_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _current_request(tmp_path: Path, *, word_timing: bool = True) -> tuple[dict, dict]:
    from src.autoslice.final_review_auditor import build_context_adjudication_request

    source, finding, clip, _narrow, observation, _paths = _native_range_artifacts(
        tmp_path, "mai", word_timing=word_timing,
    )
    request = build_context_adjudication_request(
        source,
        {**finding, "_prior_range_acoustic_observations": [observation]},
        clip_context=clip,
        source_media_timeline_offset_ms=100,
    )
    row = request["context_only_range_acoustic_observations"][0]
    # The fixture's word span is deliberately a neutral replacement for this
    # authority test; changing the request and recomputing its own identity
    # leaves the source/range receipt candidate-blind and fully validator-bound.
    request = deepcopy(request)
    request["proposed_cue"] = "乙"
    request_payload = dict(request)
    request_payload.pop("request_sha256")
    request["request_sha256"] = _json_sha(request_payload)
    return request, row


def test_native_word_projection_passes_only_after_real_range_validation(tmp_path: Path) -> None:
    request, row = _current_request(tmp_path)
    binding = build_range_word_projection_binding(
        check_request=request,
        range_observation=row,
    )
    assert binding is not None
    assert binding["witness_protocol"] == "native_word_timed_range_context"
    assert binding["witness"]["evidence_scope"] == "RANGE_CONTEXT_ONLY"
    assert binding["projection"]["spans"] == [
        {"n": 2, "text": "乙", "start_ms": 10100, "end_ms": 10200},
    ]
    assert valid_range_word_projection_binding(
        binding, proposed="乙", window=(10_000, 14_000),
    )
    assert valid_acoustic_witness_binding(
        binding, proposed="乙", window=(10_000, 14_000),
    )

    finding = {"exact_release_adjudication": {"request": request}}
    gate = convergence_witness_gate(
        proposed="乙",
        window=(10_000, 14_000),
        findings=[finding],
        history=[],
    )
    assert gate["status"] == "PASS"
    assert gate["source"] == "FRESH_RANGE_WORD_PROJECTION"
    assert RANGE_WORD_PROJECTION_FIELD in gate
    assert "acoustic_witness_binding" not in gate
    assert valid_convergence_witness_gate(
        gate, proposed="乙", window=(10_000, 14_000),
    )


@pytest.mark.parametrize("tamper", [
    "projection_window",
    "projection_neighbor",
    "different_proposal",
    "request_drift",
    "provider_mismatch",
    "forged_digest",
])
def test_projection_rebuild_rejects_timing_or_identity_tamper(tmp_path: Path, tamper: str) -> None:
    request, row = _current_request(tmp_path)
    binding = build_range_word_projection_binding(
        check_request=request,
        range_observation=row,
    )
    assert binding is not None
    tampered = deepcopy(binding)
    if tamper == "projection_window":
        tampered["projection"]["spans"][0]["end_ms"] = 14_001
    elif tamper == "projection_neighbor":
        tampered["projection"]["spans"][0]["n"] = 3
    elif tamper == "different_proposal":
        assert not valid_range_word_projection_binding(
            tampered, proposed="别的提案", window=(10_000, 14_000),
        )
        return
    elif tamper == "request_drift":
        tampered["check_request"]["matched_start_ms"] = 10_001
        payload = dict(tampered["check_request"])
        payload.pop("request_sha256")
        tampered["check_request"]["request_sha256"] = _json_sha(payload)
    elif tamper == "provider_mismatch":
        tampered["range_observation"]["provider"] = "moss"
    else:
        tampered["binding_sha256"] = "sha256:" + _json_sha(tampered)
    # Even if a caller recomputes the outer digest, the builder must replay
    # the immutable receipt and raw provider artifacts before accepting spans.
    if tamper != "forged_digest":
        payload = dict(tampered)
        payload.pop("binding_sha256")
        tampered["binding_sha256"] = "sha256:" + _json_sha(payload)
    assert not valid_range_word_projection_binding(
        tampered, proposed="乙", window=(10_000, 14_000),
    )


def test_no_native_word_timing_or_phonetic_mismatch_is_not_promoted(tmp_path: Path) -> None:
    request, row = _current_request(tmp_path, word_timing=False)
    assert build_range_word_projection_binding(
        check_request=request,
        range_observation=row,
    ) is None

    request, row = _current_request(tmp_path, word_timing=True)
    row = deepcopy(row)
    row["native_word_overlap_evidence"]["table"]["rows"][0]["cue_spans"][1]["text"] = "甲"
    # The supplied summary is ignored; the strict validator rebuilds it from
    # the file-bound raw response and consequently cannot accept this fake.
    binding = build_range_word_projection_binding(
        check_request=request,
        range_observation=row,
    )
    assert binding is not None
    assert binding["projection"]["text"] == "乙"


def test_explicit_current_audio_request_keeps_range_projection_blocked(tmp_path: Path) -> None:
    request, _row = _current_request(tmp_path)
    finding = {
        "exact_release_adjudication": {
            "request": request,
            "witness_judge": {
                "judge": {
                    "schema_version": "acoustic-witness-adjudication.v1",
                    "choice": "PROPOSED",
                    "needs_audio_requested": True,
                },
            },
        },
    }
    # The receipt is valid, but an explicit current needs-audio request still
    # owns the decision; range timing cannot silently close that CPA cycle.
    finding["exact_release_adjudication"]["request"] = request
    assert convergence_witness_gate(
        proposed="乙",
        window=(10_000, 14_000),
        findings=[finding],
        history=[],
    )["status"] == "BLOCK"


def _range_judge(request: dict, *, contract: str) -> dict:
    judge = {
        "schema_version": "acoustic-witness-adjudication.v1",
        "status": "JUDGED",
        "choice": "PROPOSED",
        "needs_audio": False,
        "needs_audio_requested": False,
        "needs_audio_request_unresolved": False,
        "decision_contract": contract,
        "choice_set": ["CURRENT", "PROPOSED", "NEITHER"],
        "check_request_sha256": request["request_sha256"],
        "reason": "The bound native range words support the selected proposal.",
        "candidate_pinyin_similarity": {"current": 0.0, "proposed": 1.0},
        "prompt_sha256": "sha256:" + "1" * 64,
        "completion_sha256": "sha256:" + "2" * 64,
    }
    if contract == "current-proposed-candidate-id-neither.v1":
        judge.update(candidate_id="PROPOSAL", selected_candidate_text="乙")
    return judge


@pytest.mark.parametrize(
    "contract",
    ["current-proposed-neither.v1", "current-proposed-candidate-id-neither.v1"],
)
def test_range_projection_accepts_each_supported_proposed_judge_contract(
    tmp_path: Path, contract: str,
) -> None:
    request, row = _current_request(tmp_path)
    adjudication = {
        "request": request,
        "witness_judge": {"judge": _range_judge(request, contract=contract)},
        "context_only_range_acoustic_observations": [row],
    }
    binding = range_word_projection_binding_from_adjudication(
        adjudication, proposed="乙", window=(10_000, 14_000),
    )
    assert binding is not None
    assert valid_range_word_projection_binding(
        binding, proposed="乙", window=(10_000, 14_000),
    )


@pytest.mark.parametrize(
    "mutation",
    ["candidate_id", "selected_candidate_text", "unknown_contract"],
)
def test_range_projection_rejects_candidate_identity_or_contract_drift(
    tmp_path: Path, mutation: str,
) -> None:
    request, row = _current_request(tmp_path)
    judge = _range_judge(
        request, contract="current-proposed-candidate-id-neither.v1",
    )
    if mutation == "candidate_id":
        judge["candidate_id"] = "CURRENT"
    elif mutation == "selected_candidate_text":
        judge["selected_candidate_text"] = "别的提案"
    else:
        judge["decision_contract"] = "invented-contract.v1"
    adjudication = {
        "request": request,
        "witness_judge": {"judge": judge},
        "context_only_range_acoustic_observations": [row],
    }
    assert range_word_projection_binding_from_adjudication(
        adjudication, proposed="乙", window=(10_000, 14_000),
    ) is None
