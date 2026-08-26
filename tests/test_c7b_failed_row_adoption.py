from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.autoslice import c7b_failed_row_adoption as adoption

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / adoption.RECEIPT_RELATIVE_PATH


def _receipt() -> dict:
    return json.loads(RECEIPT.read_text(encoding="utf-8"))


def _row() -> dict[str, object]:
    receipt = _receipt()
    failure = dict(receipt["failure_tuple"])
    generation = dict(receipt["attempt_generation"])
    return {
        "candidate_id": adoption.CANDIDATE_ID,
        **failure,
        "cover_route_regeneration_attempts": generation["cover_route_regeneration_attempts"],
        "cover_route_regeneration_fingerprint": generation["cover_route_regeneration_fingerprint"],
        "cover_generation": {"sealed": True},
    }


def test_c7b_receipt_is_self_sealed_and_exact() -> None:
    value = _receipt()
    adoption._validate_receipt(value)
    unsigned = dict(value)
    declared = unsigned.pop("canonical_self_sha256")
    assert declared == adoption.canonical_sha256(unsigned)


def test_c7b_row_fingerprint_is_complete_and_candidate_scoped(monkeypatch) -> None:
    row = _row()
    receipt = _receipt()
    receipt["live_row_fingerprint"] = dict(receipt["live_row_fingerprint"])
    receipt["live_row_fingerprint"]["sha256"] = adoption.row_fingerprint(row)
    monkeypatch.setattr(adoption, "load_c7b_failed_row_adoption_receipt", lambda *, repo_root: (receipt, None))
    state = {"picks": [{} for _ in range(5)] + [row]}
    loaded, _ = adoption.validate_c7b_failed_row_adoption(
        repo_root=ROOT, state=state, state_date=adoption.RECORDING_DATE,
    )
    assert loaded["candidate_id"] == adoption.CANDIDATE_ID


def test_c7b_live_row_drift_fails_closed(monkeypatch) -> None:
    row = _row()
    receipt = _receipt()
    receipt["live_row_fingerprint"] = dict(receipt["live_row_fingerprint"])
    receipt["live_row_fingerprint"]["sha256"] = adoption.row_fingerprint(row)
    monkeypatch.setattr(adoption, "load_c7b_failed_row_adoption_receipt", lambda *, repo_root: (receipt, None))
    row["cover_route_regeneration_attempts"] = 2
    state = {"picks": [{} for _ in range(5)] + [row]}
    with pytest.raises(adoption.C7bFailedRowAdoptionError, match="FINGERPRINT_DRIFT"):
        adoption.validate_c7b_failed_row_adoption(
            repo_root=ROOT, state=state, state_date=adoption.RECORDING_DATE,
        )


def test_c7b_unrelated_failed_row_is_rejected(monkeypatch) -> None:
    receipt = _receipt()
    row = _row()
    row["candidate_id"] = "other_failed_candidate"
    receipt["live_row_fingerprint"] = dict(receipt["live_row_fingerprint"])
    receipt["live_row_fingerprint"]["sha256"] = adoption.row_fingerprint(row)
    monkeypatch.setattr(adoption, "load_c7b_failed_row_adoption_receipt", lambda *, repo_root: (receipt, None))
    state = {"picks": [{} for _ in range(5)] + [row]}
    with pytest.raises(adoption.C7bFailedRowAdoptionError, match="ROW_STATE_DRIFT"):
        adoption.validate_c7b_failed_row_adoption(
            repo_root=ROOT, state=state, state_date=adoption.RECORDING_DATE,
        )
