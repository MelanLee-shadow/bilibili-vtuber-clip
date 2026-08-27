"""C3-only regression tests for the sealed fastlane source-fact bridge."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.autoslice import fastlane_c3_source_fact_supersession as c3


ROOT = Path(__file__).resolve().parents[1]


class _Plan:
    candidate_id = c3.CANDIDATE_ID
    date = c3.RECORDING_DATE


def _canonical(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _validate_compact_fixture(value: dict) -> None:
    assert value["schema_version"] == "fastlane-c3-source-fact-supersession-fixture.v1"
    authority = value["authority"]
    assert _canonical(authority) == value["authority_sha256"], "authority_seal"
    receipt = deepcopy(value["outer_receipt"])
    receipt_seal = receipt.pop("receipt_sha256")
    assert _canonical(receipt) == receipt_seal, "outer_receipt_seal"
    assert authority["provider_call_required"] is False
    assert authority["subtitle_text_mutation_authorized"] is False
    assert authority["state_mutation_authorized"] is False
    assert authority["deploy_authorized"] is False
    assert authority["upload_authorized"] is False
    inputs = value["inputs"]
    assert authority["title_sha256"] == _text(inputs["title"])
    assert authority["selection_hook_sha256"] == _text(inputs["selection_hook"])
    assert authority["final_transcript_sha256"] == _text(inputs["final_transcript"])
    assert authority["clip_context_prompt_sha256"] == _text(inputs["clip_context_prompt"]), "clip_context_prompt"
    assert authority["selection_scorecard_sha256"] == _canonical(inputs["selection_scorecard"])
    assert authority["speaker_evidence_sha256"] == _canonical(inputs["speaker_evidence"])



def test_c3_authority_exactly_pins_claude_chain_and_no_mutation() -> None:
    value = json.loads((ROOT / c3.ASSET).read_text())
    body = dict(value); seal = body.pop("authority_sha256")
    assert _canonical(body) == seal
    authority = value["authority"]
    assert authority["claude_line947"] == c3.LINE947
    assert authority["claude_reinforcements"] == list(c3.REINFORCEMENTS)
    assert authority["outer_receipt_self_seal"] == c3.OUTER_RECEIPT_SHA256
    assert authority["historical_keep_self_seal"] == c3.HISTORICAL_RECEIPT_SHA256
    assert {key for key, value in authority.items() if value is False} >= {
        "provider_call_required", "subtitle_text_mutation_authorized",
        "state_mutation_authorized", "deploy_authorized", "upload_authorized",
    }


def test_public_mint_requires_exact_replay_plan() -> None:
    with pytest.raises(c3.C3SourceFactSupersessionError, match="REPLAY_PLAN"):
        c3.build_c3_source_fact_supersession(replay_plan=None)
    for field, value in (("candidate_id", "other"), ("date", "2026-08-12")):
        plan = _Plan()
        setattr(plan, field, value)
        assert getattr(plan, field) != getattr(_Plan(), field)


def test_compact_fixture_positive_variant_replays_all_bound_inputs() -> None:
    value = json.loads(
        (ROOT / "tests/fixtures/fastlane_c3_source_fact_supersession.v1.json").read_text()
    )
    aligned = deepcopy(value)
    aligned["authority"]["clip_context_prompt_sha256"] = _text(
        aligned["inputs"]["clip_context_prompt"]
    )
    aligned["authority_sha256"] = _canonical(aligned["authority"])
    _validate_compact_fixture(aligned)


def test_compact_fixture_rejects_prompt_drift_and_seal_tamper() -> None:
    value = json.loads(
        (ROOT / "tests/fixtures/fastlane_c3_source_fact_supersession.v1.json").read_text()
    )
    with pytest.raises(AssertionError, match="clip_context_prompt"):
        _validate_compact_fixture(value)
    tampered = deepcopy(value)
    tampered["authority"]["upload_authorized"] = True
    with pytest.raises(AssertionError, match="authority_seal"):
        _validate_compact_fixture(tampered)
