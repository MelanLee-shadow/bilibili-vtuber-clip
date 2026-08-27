from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.autoslice.fastlane_c3_speaker_authority import load_c3_line947_authority
from src.autoslice.fastlane_c3_terminal_source_fact_preservation import load_authority as load_source_fact_authority
from src.autoslice.c3_boundary_reclosure_authority import (
    C3BoundaryReclosureError,
    NEW_GRID_SHA256,
    derive_current_boundary_review,
    validate_authority_document,
    validate_derived_boundary_receipt,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.review_package_boundary_validators import c3_derived_boundary_receipt_is_valid

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_PATH = ROOT / "assets/lidousha/fastlane_c3_boundary_reclosure_authority/auto_220021_561_670.v1.json"
SOURCE = Path("/private/tmp/c3-real-followup-authority")
CID = "auto_220021_561_670"


def _authority() -> dict:
    return json.loads(AUTHORITY_PATH.read_text(encoding="utf-8"))


def _inputs() -> tuple[dict, list[object], dict, dict]:
    if not SOURCE.is_dir():
        pytest.skip("local sealed C3 v3 source package is unavailable")
    record = json.loads((SOURCE / "c3-final-grid-record.json").read_text(encoding="utf-8"))
    chat = json.loads((SOURCE / f"{CID}.recut.burned-successor-v2.chat-authority.json").read_text(encoding="utf-8"))
    cues = parse_srt_cues((SOURCE / f"{CID}.recut.burned-successor-v2.reviewed-baseline.srt").read_text(encoding="utf-8"))
    return (
        chat["final_review_audit"]["boundary_semantic_review"], cues,
        record["boundary_audit"]["required_boundary_owner_verification"],
        record["boundary_audit"]["delivery_coverage_verification"],
    )


def test_exact_carry_authorities_are_repository_bound() -> None:
    assert load_source_fact_authority(repo_root=ROOT)["schema_version"] == (
        "fastlane-c3-terminal-source-fact-preservation-authority.v1"
    )
    assert load_c3_line947_authority(repo_root=ROOT)["schema_version"] == (
        "fastlane-c3-line947-speaker-authority.v2"
    )


def test_authority_exact_provenance_and_self_seal() -> None:
    authority = validate_authority_document(_authority())
    assert authority["authorization"]["session_id"] == "session-896490c8-ae47-4531-a7d1-a0ab2d81999e"
    assert authority["authorization"]["user_message_id"] == "f08d8ae-28c9-44f0-8648-21006686a8db"
    assert authority["authorization"]["response_sha256"] == "sha256:f68d837ed3c1efaae2e69f4322ecde2f3e5e0309cb055a2238e6d774b2d98546"
    assert authority["boundary"]["new_grid_sha256"] == NEW_GRID_SHA256


@pytest.mark.parametrize("path", ("authorization", "boundary", "v3_manifest", "final_assets"))
def test_authority_provenance_drift_rejects(path: str) -> None:
    authority = _authority()
    authority[path] = deepcopy(authority[path])
    key = next(iter(authority[path]))
    authority[path][key] = "drift"
    with pytest.raises(C3BoundaryReclosureError, match="DRIFT"):
        validate_authority_document(authority)


def test_current_boundary_is_derived_and_independently_validated() -> None:
    stale, cues, owner, coverage = _inputs()
    authority = validate_authority_document(_authority())
    current = derive_current_boundary_review(
        stale_review=stale, cues=cues, owner_verification=owner,
        coverage_verification=coverage, authority=authority,
    )
    receipt = current["c3_derived_boundary_reclosure_receipt"]
    assert current["cue_grid_sha256"] == NEW_GRID_SHA256
    assert current["recommended_end_cue_index"] == 11
    assert current["final_endpoint_binding"]["final_closure_cue_index"] == 11
    assert validate_derived_boundary_receipt(
        receipt, review=current, cues=cues, authority=authority,
        owner_verification=owner, coverage_verification=coverage,
    )
    assert c3_derived_boundary_receipt_is_valid(
        receipt, review=current, cues=cues, authority=authority,
        owner_verification=owner, coverage_verification=coverage,
    )


def test_receipt_rejects_grid_endpoint_or_permission_drift() -> None:
    stale, cues, owner, coverage = _inputs()
    authority = validate_authority_document(_authority())
    current = derive_current_boundary_review(
        stale_review=stale, cues=cues, owner_verification=owner,
        coverage_verification=coverage, authority=authority,
    )
    for field, value in (("endpoint_ms", 108941), ("cue_count", 12)):
        drift = deepcopy(current["c3_derived_boundary_reclosure_receipt"])
        drift["current_review"][field] = value
        assert not validate_derived_boundary_receipt(
            drift, review=current, cues=cues, authority=authority,
            owner_verification=owner, coverage_verification=coverage,
        )
    denied = deepcopy(current)
    denied["c3_derived_boundary_reclosure_receipt"]["permissions"]["provider"] = True
    assert not validate_derived_boundary_receipt(
        denied["c3_derived_boundary_reclosure_receipt"], review=current, cues=cues,
        authority=authority, owner_verification=owner, coverage_verification=coverage,
    )
