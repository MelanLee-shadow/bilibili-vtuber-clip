"""Frozen geometry replay after ordinary, validated text retirement.

The fixture uses the real typed repair and registration writer. Source-owner
receipt validation uses the existing R6 scoped fixture seam; it does not
replace the real original-material source-owner/geometry field replays.
"""
from copy import deepcopy

import pytest

from src.autoslice import editorial_subtitle as editorial
from src.autoslice import exact_final_cpa_history as history
from src.autoslice import producer_package_finalization as finalization
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.context_assisted_audio import digest
from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal
from tests.test_editorial_subtitle import SRT, h, run


DURATION = 223_770
OFFSET = 5000
POST_FREEZE = "POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER"


def _case(tmp_path, monkeypatch, *, kind, source_owner=False, retained=False):
    output, repairs, _, calls = run(tmp_path, monkeypatch)
    assert len(repairs) == len(calls) == 1
    repair = deepcopy(repairs[0])
    start = OFFSET + repair["matched_start_ms"]
    end = OFFSET + repair["matched_end_ms"]
    row = {
        "matched_start_ms": start, "matched_end_ms": end,
        "before": ["原始历史字幕"], "after": [repair["before"]],
    }
    key = "entity_repairs"
    if kind == "exact_read":
        key = "applied"
        row.update(exact_text=repair["before"], owner_eligible=True)
    # Empty predecessors retain the actual list ordinal through cold replay.
    authority = {key: ([{}, {}] if kind == "exact_read" else []) + [row]}
    spec = {
        "pieces": [{"start_ms": 0, "end_ms": DURATION}],
        "semantic_start_ms": 5000, "semantic_end_ms": 159_140,
    }
    pipeline.freeze_required_boundary_owner_contract(
        spec=deepcopy(spec), durations=[DURATION],
        chat_authority_audit=authority, required_boundary_owners=[],
    )
    frozen = deepcopy(authority["frozen_boundary_owner_contract"])
    assert frozen["required_owner_count"] == 1
    if source_owner:
        conflict = {
            "original_applied": deepcopy(authority["applied"]),
            "evidence": {
                "prior_chat_authority": {
                    "applied_index": 2, "delivery_start_ms": OFFSET,
                },
            },
        }
        receipt = repair["editorial_authority"]
        receipt["source_owner_reconciliation"] = True
        receipt["request"]["chat_source_conflict"] = deepcopy(conflict)
        repair["judge"]["authority_sha256"] = digest(receipt)
        real_validator = editorial.validate_authority

        def completed_source_receipt(value, **kwargs):
            if value.get("source_owner_reconciliation") is not True:
                return real_validator(value, **kwargs)
            if value["request"].get("chat_source_conflict") != conflict:
                raise ValueError("SOURCE_OWNER_FIXTURE_SCOPE_DRIFT")
            ordinary = deepcopy(value)
            ordinary.pop("source_owner_reconciliation")
            ordinary["request"].pop("chat_source_conflict")
            return real_validator(ordinary, **kwargs)

        monkeypatch.setattr(editorial, "validate_authority", completed_source_receipt)
    editorial.validate_repair(repair)
    authority["exact_final_cpa_self_heal"] = {
        "schema_version": "exact-final-cpa-self-heal-audit.v1",
        "status": "REVIEW_PENDING",
        "passes": [{
            "schema_version": "exact-final-cpa-self-heal-pass.v1",
            "pass_index": 1, "input_srt_sha256": h(SRT),
            "output_srt_sha256": h(output), "repairs": [repair],
        }],
    }
    finalization._register_exact_final_cpa_repairs(
        authority, [repair], delivery_start_ms=OFFSET,
    )
    _validate_exact_final_cpa_self_heal(
        authority, expected_srt_sha256=h(output), allow_pending=True,
    )
    successor_index = len(authority["entity_repairs"]) - 1
    pipeline._continuation_validate_exact_final_owner(
        authority["entity_repairs"][successor_index],
        row_index=successor_index, chat_authority=authority,
    )
    if retained:
        completed = {
            **deepcopy(authority["exact_final_cpa_self_heal"]),
            "status": "PASS", "final_srt_sha256": h(output),
        }
        _validate_exact_final_cpa_self_heal(
            {"exact_final_cpa_self_heal": completed},
            expected_srt_sha256=h(output), allow_pending=False,
        )
        # This unit projects geometry from a validated completed run; a new
        # current run has no repair yet. Full cross-run text replay is tested
        # in test_source_clock_retained_runs and the actual Xiada field.
        authority["exact_final_cpa_self_heal"] = history.build_run_audit(
            [], history.retained_runs(completed),
        )
        assert history.repair_history(authority["exact_final_cpa_self_heal"])
    authority["final_review_audit"] = {"status": "FLAGGED", "release_gate": "BLOCK"}
    return authority, spec, frozen, key, (2 if kind == "exact_read" else 0), calls


def _project(authority, spec):
    evidence = deepcopy(authority)
    pipeline._continuation_retain_superseded_boundary_geometry(evidence)
    # The ordinary caller keeps final-surface additions out of the frozen
    # source-boundary question. The original ledger remains untouched.
    evidence["entity_repairs"] = [
        row for row in evidence.get("entity_repairs", [])
        if not (
            row.get("boundary_required") is False
            and row.get("boundary_owner_rejection") == POST_FREEZE
        )
    ]
    pipeline.freeze_required_boundary_owner_contract(
        spec=deepcopy(spec), durations=[DURATION],
        chat_authority_audit=evidence, required_boundary_owners=[],
    )
    return evidence


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("kind,source_owner", [
    ("entity_repair", False), ("exact_read", False), ("exact_read", True),
])
def test_registered_retirement_preserves_frozen_owner(
    tmp_path, monkeypatch, kind, source_owner, retained,
):
    authority, spec, frozen, key, index, calls = _case(
        tmp_path, monkeypatch, kind=kind,
        source_owner=source_owner, retained=retained,
    )
    original = deepcopy(authority)
    original_calls = list(calls)
    evidence = _project(authority, spec)
    result, extension = pipeline._continuation_boundary_owner_contract(
        frozen, evidence["frozen_boundary_owner_contract"], padded_dur=DURATION,
    )
    assert result == frozen and extension is None
    assert authority == original and calls == original_calls
    assert "reconciliation" in authority[key][index]
    assert evidence[key][index]["boundary_owner_id"] == frozen["owners"][0]["owner_id"]
    assert evidence["final_review_audit"]["release_gate"] == "BLOCK"
    if kind == "exact_read":
        assert evidence["applied"][:2] == original["applied"][:2]
        assert frozen["owners"][0]["owner_id"] == "exact_read:3:6000:7000"


@pytest.mark.parametrize("damage", [
    "schema", "status", "timing_false", "timing_integer", "repair_digest",
    "owner_id", "start", "end", "required_false", "required_integer",
])
@pytest.mark.parametrize("kind", ["entity_repair", "exact_read"])
def test_retained_geometry_does_not_bypass_original_owner_constraints(
    tmp_path, monkeypatch, kind, damage,
):
    authority, spec, frozen, key, index, _ = _case(
        tmp_path, monkeypatch, kind=kind, retained=True,
    )
    row = authority[key][index]
    if damage == "schema":
        row["reconciliation"]["schema_version"] = "unrelated-retirement.v1"
    elif damage == "status":
        row["reconciliation"]["status"] = "PENDING"
    elif damage.startswith("timing"):
        row["reconciliation"]["timing_immutable"] = (
            False if damage == "timing_false" else 1
        )
    elif damage == "repair_digest":
        row["reconciliation"]["exact_final_repair_sha256"] = "sha256:" + "0" * 64
    elif damage == "owner_id":
        row["boundary_owner_id"] = "another-owner"
    elif damage in {"start", "end"}:
        row["matched_" + damage + "_ms"] += 1
    else:
        row["boundary_required"] = False if damage == "required_false" else 1
    original = deepcopy(authority)
    evidence = _project(authority, spec)
    assert authority == original
    assert "reconciliation" in evidence[key][index]
    with pytest.raises(pipeline.CurrentTextContinuationError, match="OWNER_SET_DRIFT"):
        pipeline._continuation_boundary_owner_contract(
            frozen, evidence["frozen_boundary_owner_contract"], padded_dur=DURATION,
        )


@pytest.mark.parametrize("eligible", [False, None, 1])
def test_exact_read_still_requires_original_owner_eligibility(
    tmp_path, monkeypatch, eligible,
):
    authority, spec, frozen, key, index, _ = _case(
        tmp_path, monkeypatch, kind="exact_read", source_owner=True,
    )
    authority[key][index]["owner_eligible"] = eligible
    evidence = _project(authority, spec)
    assert "reconciliation" in evidence[key][index]
    with pytest.raises(pipeline.CurrentTextContinuationError, match="OWNER_SET_DRIFT"):
        pipeline._continuation_boundary_owner_contract(
            frozen, evidence["frozen_boundary_owner_contract"], padded_dur=DURATION,
        )


def test_history_never_admits_an_unfrozen_nonrequired_row(tmp_path, monkeypatch):
    authority, spec, frozen, key, index, _ = _case(
        tmp_path, monkeypatch, kind="entity_repair", retained=True,
    )
    extra = deepcopy(authority[key][index])
    extra.update(
        boundary_required=False, boundary_owner_id=None,
        matched_start_ms=18_060, matched_end_ms=19_220,
    )
    authority[key].append(extra)
    original = deepcopy(authority)
    evidence = _project(authority, spec)
    assert evidence[key][-1]["reconciliation"] == extra["reconciliation"]
    actual, extension = pipeline._continuation_boundary_owner_contract(
        frozen, evidence["frozen_boundary_owner_contract"], padded_dur=DURATION,
    )
    assert actual == frozen and extension is None and authority == original


def test_corrupt_retained_envelope_is_rejected_before_geometry_projection(tmp_path, monkeypatch):
    authority, _, _, _, _, _ = _case(
        tmp_path, monkeypatch, kind="entity_repair", retained=True,
    )
    authority["exact_final_cpa_self_heal"][history.HISTORY_KEY][0]["audit_sha256"] = (
        "sha256:" + "0" * 64
    )
    original = deepcopy(authority)
    with pytest.raises(ValueError, match="EXACT_FINAL_CPA_HISTORY_INVALID"):
        pipeline._continuation_retain_superseded_boundary_geometry(authority)
    assert authority == original


def test_no_self_heal_remains_an_unchanged_geometry_noop():
    authority = {"frozen_boundary_owner_contract": {"owners": []}, "applied": []}
    original = deepcopy(authority)
    pipeline._continuation_retain_superseded_boundary_geometry(authority)
    assert authority == original
