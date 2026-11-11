"""Regression coverage for historical source-owner registration replay.

The ordinary typed repair is produced by the existing editorial fixture.  As
in test_chat_authority_reconciliation's successor-chain test, only the already
completed source-owner receipt boundary is isolated: after checking its exact
fixture scope, it delegates to the real ordinary authority validator.  Repair
validation, registration, historical lookup and owner comparison remain real.
This module does not stand in for the separate Xiada XML/audio receipt replay.
"""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any

import pytest

from src.autoslice import editorial_subtitle as editorial
from src.autoslice import producer_package_finalization as finalization
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.canonical_json_hash import canonical_sha256
from src.autoslice.context_assisted_audio import digest
from tests.test_editorial_subtitle import SRT, h, run


OFFSET = 5000


@dataclass
class RegisteredCase:
    authority: dict[str, Any]
    output: str
    prompts: list[str]


def _snapshot(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _register(repair: dict[str, Any], output: str, applied: list[dict[str, Any]]) -> dict[str, Any]:
    authority = {
        "applied": deepcopy(applied),
        "exact_final_cpa_self_heal": {
            "schema_version": "exact-final-cpa-self-heal-audit.v1",
            "status": "REVIEW_PENDING",
            "passes": [{
                "schema_version": "exact-final-cpa-self-heal-pass.v1",
                "pass_index": 1,
                "input_srt_sha256": h(SRT),
                "output_srt_sha256": h(output),
                "repairs": [repair],
            }],
        },
    }
    finalization._register_exact_final_cpa_repairs(
        authority, [repair], delivery_start_ms=OFFSET,
    )
    # Exercise the cold JSON shape, with no shared Python container aliases.
    return json.loads(json.dumps(authority, ensure_ascii=False))


def _validate_owner(authority: dict[str, Any]) -> None:
    pipeline._continuation_validate_exact_final_owner(
        authority["entity_repairs"][0], row_index=0,
        chat_authority=authority,
    )


@pytest.fixture
def source_owner_case(tmp_path, monkeypatch):
    output, repairs, _, prompts = run(tmp_path, monkeypatch)
    assert len(repairs) == 1 and len(prompts) == 1
    repair = deepcopy(repairs[0])
    editorial.validate_repair(repair)
    original_applied = [{
        "mode": "exact_span",
        "matched_start_ms": OFFSET + repair["matched_start_ms"],
        "matched_end_ms": OFFSET + repair["matched_end_ms"],
        "exact_text": repair["before"],
    }]
    conflict = {
        "original_applied": deepcopy(original_applied),
        "evidence": {
            "prior_chat_authority": {
                "applied_index": 0, "delivery_start_ms": OFFSET,
            },
        },
    }
    receipt = repair["editorial_authority"]
    receipt["source_owner_reconciliation"] = True
    receipt["request"]["chat_source_conflict"] = deepcopy(conflict)
    repair["judge"]["authority_sha256"] = digest(receipt)

    real_validate_authority = editorial.validate_authority

    def validate_completed_source_receipt(value, **kwargs):
        if value.get("source_owner_reconciliation") is not True:
            return real_validate_authority(value, **kwargs)
        # This is an explicit upstream fixture boundary, not a permissive
        # validate_repair stub.  It accepts only the recorded fixture scope.
        if value["request"].get("chat_source_conflict") != conflict:
            raise ValueError("SOURCE_OWNER_FIXTURE_SCOPE_DRIFT")
        ordinary = deepcopy(value)
        ordinary.pop("source_owner_reconciliation")
        ordinary["request"].pop("chat_source_conflict")
        return real_validate_authority(ordinary, **kwargs)

    monkeypatch.setattr(editorial, "validate_authority", validate_completed_source_receipt)
    editorial.validate_repair(repair)
    authority = _register(repair, output, original_applied)
    assert authority["entity_repairs"][0]["superseded_applied_indexes"] == [0]
    assert authority["applied"][0]["reconciliation"]["schema_version"] == (
        "chat-source-owner-exact-read-supersession.v1"
    )
    assert "reconciliation" not in receipt["request"]["chat_source_conflict"]["original_applied"][0]
    return RegisteredCase(authority, output, prompts)


def test_historical_source_owner_replay_preserves_original_inputs(source_owner_case):
    case = source_owner_case
    before = _snapshot(case.authority)
    calls_before = list(case.prompts)
    # A second cold consume also catches accidental in-place retirement of
    # the original request's applied rows during the first local replay.
    _validate_owner(case.authority)
    assert _snapshot(case.authority) == before
    _validate_owner(case.authority)
    assert _snapshot(case.authority) == before
    assert case.prompts == calls_before


def test_live_registration_still_rejects_changed_applied_owner(source_owner_case):
    case = source_owner_case
    repair = case.authority["exact_final_cpa_self_heal"]["passes"][0]["repairs"][0]
    original = repair["editorial_authority"]["request"]["chat_source_conflict"]["original_applied"]
    live = {"applied": deepcopy(original)}
    live["applied"][0]["exact_text"] = "另一个实际 owner"
    repair_before = _snapshot(repair)
    with pytest.raises(ValueError, match="^CHAT_RECONCILIATION_LIVE_OWNER_DRIFT$"):
        finalization._register_exact_final_cpa_repairs(
            live, [repair], delivery_start_ms=OFFSET,
        )
    assert _snapshot(repair) == repair_before


def test_corrupt_original_receipt_is_rejected_after_outer_rebinding(source_owner_case):
    authority = deepcopy(source_owner_case.authority)
    repair = authority["exact_final_cpa_self_heal"]["passes"][0]["repairs"][0]
    repair["editorial_authority"]["raw_completion"] += "tamper"
    # Keep the outer lookup unique and consistent so this must reach the
    # real repair validator, rather than merely miss the historical digest.
    changed_digest = "sha256:" + canonical_sha256(repair, allow_nan=True)
    authority["entity_repairs"][0]["exact_final_repair_sha256"] = changed_digest
    authority["exact_final_cpa_surface_registrations"][0]["exact_final_repair_sha256"] = changed_digest
    before = _snapshot(authority)
    with pytest.raises(pipeline.CurrentTextContinuationError, match="^CURRENT_EDITORIAL_OWNER_INVALID$"):
        _validate_owner(authority)
    assert _snapshot(authority) == before


def test_duplicate_historical_registration_remains_ambiguous(source_owner_case):
    authority = deepcopy(source_owner_case.authority)
    registrations = authority["exact_final_cpa_surface_registrations"]
    registrations.append(deepcopy(registrations[0]))
    before = _snapshot(authority)
    with pytest.raises(pipeline.CurrentTextContinuationError, match="^CURRENT_EXACT_FINAL_OWNER_UNBOUND$"):
        _validate_owner(authority)
    assert _snapshot(authority) == before


def test_ordinary_editorial_owner_path_stays_unchanged(tmp_path, monkeypatch):
    output, repairs, _, prompts = run(tmp_path, monkeypatch)
    assert len(repairs) == 1 and len(prompts) == 1
    assert repairs[0]["editorial_authority"].get("source_owner_reconciliation") is not True
    authority = _register(repairs[0], output, [])
    before = _snapshot(authority)
    _validate_owner(authority)
    assert _snapshot(authority) == before
    assert len(prompts) == 1
