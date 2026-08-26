"""The single sealed C7b failed-row adoption exception.

This module is deliberately narrower than every generic revive/import path.  It
only waives the source-state class mismatch for the one C7b row; all replay,
artifact, QC, and state-last CAS gates remain owned by the normal replay lane.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthority,
    require_repository_asset_authority,
)

SCHEMA = "c7b-failed-row-adoption-receipt.v1"
CANDIDATE_ID = "auto_130040_201_255"
RECORDING_DATE = "2026-08-14"
RECEIPT_RELATIVE_PATH = Path(
    "assets/lidousha/fastlane_c7b_private/"
    "auto_130040_201_255.failed-row-adoption-receipt.v1.json"
)

class C7bFailedRowAdoptionError(ValueError):
    """The exact C7b failed-row adoption receipt cannot be consumed."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def row_fingerprint(row: Mapping[str, object]) -> str:
    """Fingerprint the complete row, not a selected failure subset."""
    return canonical_sha256(row)


def _sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or len(value.removeprefix("sha256:")) != 64:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_INVALID")
    raw = value.removeprefix("sha256:")
    if any(char not in "0123456789abcdef" for char in raw):
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_INVALID")
    return "sha256:" + raw


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_INVALID")
    return value


def _regular_json(path: Path, *, label: str) -> tuple[dict[str, object], bytes]:
    if path.is_symlink() or not path.is_file():
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_UNAVAILABLE")
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_INVALID") from exc
    if not isinstance(value, dict):
        raise C7bFailedRowAdoptionError(f"C7B_ADOPTION_{label}_INVALID")
    return value, payload


def load_c7b_failed_row_adoption_receipt(
    *, repo_root: Path,
) -> tuple[dict[str, object], RepositoryAssetAuthority]:
    """Load and prove the checked-in receipt is current repository authority."""
    path = repo_root / RECEIPT_RELATIVE_PATH
    value, payload = _regular_json(path, label="RECEIPT")
    try:
        seal = require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=RECEIPT_RELATIVE_PATH,
            observed_bytes=payload,
        )
    except Exception as exc:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_UNSEALED") from exc
    unsigned = dict(value)
    declared = unsigned.pop("canonical_self_sha256", None)
    if declared != canonical_sha256(unsigned):
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_SELF_SEAL_DRIFT")
    _validate_receipt(value)
    return value, seal


def _validate_receipt(value: Mapping[str, object]) -> None:
    required = {
        "schema_version", "candidate_id", "recording_date", "row_selector",
        "live_row_fingerprint", "failure_tuple", "attempt_generation",
        "source_collection", "predecessor", "ivan_ruling", "target",
        "authority_chain", "permissions", "canonical_self_sha256",
    }
    if set(value) != required or value.get("schema_version") != SCHEMA:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_SCHEMA_INVALID")
    if value.get("candidate_id") != CANDIDATE_ID or value.get("recording_date") != RECORDING_DATE:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RECEIPT_IDENTITY_DRIFT")
    selector = _mapping(value.get("row_selector"), label="ROW_SELECTOR")
    if dict(selector) != {"collection": "picks", "index": 5, "candidate_id": CANDIDATE_ID}:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_SELECTOR_INVALID")
    fingerprint = _mapping(value.get("live_row_fingerprint"), label="ROW_FINGERPRINT")
    if set(fingerprint) != {"algorithm", "canonicalization", "sha256"} or fingerprint.get("algorithm") != "sha256" or fingerprint.get("canonicalization") != "json_sort_keys_utf8_no_whitespace":
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_FINGERPRINT_INVALID")
    _sha(fingerprint.get("sha256"), label="ROW_FINGERPRINT")
    failure = _mapping(value.get("failure_tuple"), label="FAILURE_TUPLE")
    expected_failure = {
        "status": "failed", "rc": 1, "failure_kind": "content_boundary",
        "failure_stage": "final_review_boundary_semantic", "failure_recoverable": False,
        "failure_fingerprint": "sha256:ba8e802ba67bd284207fd706a758924afc122577398b038eff6fbb1197dfa606",
        "failure_recovery_fingerprint": "sha256:270cd93e86e9612d4d3e3060b3cd300d3ce2c6f3a82b66f3fa99d64694a16caf",
    }
    if dict(failure) != expected_failure:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_FAILURE_TUPLE_INVALID")
    attempts = _mapping(value.get("attempt_generation"), label="ATTEMPT_GENERATION")
    expected_attempts = {
        "cover_route_regeneration_attempts": 1,
        "cover_route_regeneration_fingerprint": "sha256:4d7a8d95549b0385d3ed8917b684b1d42087bfb2dde0318f3eaea663e2e83918",
        "cover_generation_sha256": "sha256:" + "0" * 64,
    }
    # The full generation object is already covered by the complete row hash;
    # the receipt stores its digest as an explicit anti-omission witness.
    if set(attempts) != set(expected_attempts) or attempts.get("cover_route_regeneration_attempts") != 1 or attempts.get("cover_route_regeneration_fingerprint") != expected_attempts["cover_route_regeneration_fingerprint"] or attempts.get("cover_generation_sha256") != "sha256:7ef2ae295d7ab612bf5ee0f74af3571bab880dc31f6b8e9a9fe3398197aead60":
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ATTEMPT_GENERATION_INVALID")
    _sha(attempts.get("cover_generation_sha256"), label="COVER_GENERATION")
    source = _mapping(value.get("source_collection"), label="SOURCE_COLLECTION")
    if dict(source) != {
        "basename": "22966160_20260814-13-00-40.mp4",
        "sha256": "sha256:660f609ca46cf9b6a5d7618df290ffd8cf32e54677813be343067719d8616b54",
        "absolute_interval_ms": [191190, 303140],
    }:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_SOURCE_COLLECTION_INVALID")
    predecessor = _mapping(value.get("predecessor"), label="PREDECESSOR")
    expected_predecessor = {
        "record_sha256": "sha256:b875d8ddedaa47971249e3af057b218f45e62fb002ddcf6c9733cd38d5bfd8f5",
        "video_sha256": "sha256:09c42e6cab8b35891f7b307dcfd4e23323073f30915ffc36bcbe2f33acdb1798",
        "srt_sha256": "sha256:810a0f17e658184029543910dc8e1c22e1166f03edf7ca90afb82943b761bd94",
        "source_media_sha256": "sha256:660f609ca46cf9b6a5d7618df290ffd8cf32e54677813be343067719d8616b54",
        "padded_source_sha256": "sha256:5b06a7bb19e22c0a8c368c83ee83170ff068b04d32fd07cf246859e8f09014f0",
    }
    if dict(predecessor) != expected_predecessor:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_PREDECESSOR_INVALID")
    ruling = _mapping(value.get("ivan_ruling"), label="IVAN_RULING")
    if dict(ruling) != {
        "path": "docs/reviews/2026-08-19-ivan-review-batch-rulings.md",
        "ref": "line947-row7b",
    }:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_RULING_INVALID")
    target = _mapping(value.get("target"), label="TARGET")
    if dict(target) != {
        "reviewed_srt_sha256": "sha256:e07e2e23d2eacebbeb95a4700c7fa4005fb4342a432c28d0adde1d4e68e98457",
        "title": "李姐也是脑控大师，但即使被脑控仍然信不了李1是怎么回事呢",
        "changed_cue_ordinals": [10, 18, 19, 20],
        "frozen_cue_count": 32,
        "operator_drop_count": 0,
    }:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_TARGET_INVALID")
    chain = _mapping(value.get("authority_chain"), label="AUTHORITY_CHAIN")
    expected_chain = {
        "private_authority_sha256": "sha256:bf46c6d6ae743bfb7488c3387b79e5eb187182915284ecb3856569f9552b7d77",
        "freeze_closure_sha256": "sha256:916eb1f10ff4e8a30db0b4b0164583c4de55fa4d9a785f2b2e905c060b9398e6",
        "decision_ledger_sha256": "sha256:f91c50c410f265b472e32f4f7f57696876e75b0c82d2e527b62292cfa3ff0aca",
        "truth_diff_sha256": "sha256:ea5b986c58dcd8ae70b6b947c41943ba563419623b1ae08b89f8a9d52a8f7e09",
        "source_reconciliation_sha256": "sha256:c6c07f47c007736c8c85ec5691c6c63eb503ab2bea4188e2dfbc6cc1cee75a97",
    }
    if dict(chain) != expected_chain:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_AUTHORITY_CHAIN_INVALID")
    permissions = _mapping(value.get("permissions"), label="PERMISSIONS")
    if dict(permissions) != {"provider_allowed": False, "upload_allowed": False, "state_write_allowed": False, "source_state_class_waiver": True}:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_PERMISSIONS_INVALID")


def validate_c7b_failed_row_adoption(
    *, repo_root: Path, state: Mapping[str, object], state_date: str,
) -> tuple[dict[str, object], RepositoryAssetAuthority]:
    """Validate the one live row before the replay after-image is projected."""
    receipt, seal = load_c7b_failed_row_adoption_receipt(repo_root=repo_root)
    if state_date != RECORDING_DATE:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_STATE_DATE_DRIFT")
    picks = state.get("picks")
    if not isinstance(picks, list) or len(picks) <= 5:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_PICKS_INVALID")
    row = picks[5]
    if not isinstance(row, Mapping):
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_INVALID")
    selector = receipt["row_selector"]
    assert isinstance(selector, Mapping)
    if row.get("candidate_id") != CANDIDATE_ID or row.get("status") != "failed":
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_STATE_DRIFT")
    if row_fingerprint(row) != receipt["live_row_fingerprint"]["sha256"]:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_FINGERPRINT_DRIFT")
    failure = receipt["failure_tuple"]
    assert isinstance(failure, Mapping)
    if any(row.get(key) != value for key, value in failure.items()):
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_FAILURE_DRIFT")
    generation = receipt["attempt_generation"]
    assert isinstance(generation, Mapping)
    if row.get("cover_route_regeneration_attempts") != generation["cover_route_regeneration_attempts"] or row.get("cover_route_regeneration_fingerprint") != generation["cover_route_regeneration_fingerprint"]:
        raise C7bFailedRowAdoptionError("C7B_ADOPTION_ROW_ATTEMPT_DRIFT")
    return receipt, seal
