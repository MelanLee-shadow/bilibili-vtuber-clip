from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice import c7b_failed_row_adoption as adoption

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / adoption.RECEIPT_RELATIVE_PATH


def _receipt() -> dict:
    return json.loads(RECEIPT.read_text(encoding="utf-8"))


def test_c7b_receipt_is_self_sealed_and_exact() -> None:
    value = _receipt()
    adoption._validate_receipt(value)
    unsigned = dict(value)
    declared = unsigned.pop("canonical_self_sha256")
    assert declared == adoption.canonical_sha256(unsigned)


def test_c7b_row_fingerprint_is_complete_and_candidate_scoped() -> None:
    row = json.loads((ROOT / "tests/fixtures/c7b_live_failed_row.complete.json").read_text(encoding="utf-8"))
    loaded, _ = adoption.validate_c7b_failed_row_adoption(
        repo_root=ROOT, state={"picks": [{} for _ in range(5)] + [row]},
        state_date=adoption.RECORDING_DATE,
    )
    assert loaded["candidate_id"] == adoption.CANDIDATE_ID


def test_c7b_live_row_drift_fails_closed() -> None:
    row = json.loads((ROOT / "tests/fixtures/c7b_live_failed_row.complete.json").read_text(encoding="utf-8"))
    row["cover_route_regeneration_attempts"] = 2
    with pytest.raises(adoption.C7bFailedRowAdoptionError, match="FINGERPRINT_DRIFT"):
        adoption.validate_c7b_failed_row_adoption(
            repo_root=ROOT, state={"picks": [{} for _ in range(5)] + [row]},
            state_date=adoption.RECORDING_DATE,
        )


def test_c7b_unrelated_failed_row_is_rejected() -> None:
    row = json.loads((ROOT / "tests/fixtures/c7b_live_failed_row.complete.json").read_text(encoding="utf-8"))
    row["candidate_id"] = "other_failed_candidate"
    with pytest.raises(adoption.C7bFailedRowAdoptionError, match="ROW_STATE_DRIFT"):
        adoption.validate_c7b_failed_row_adoption(
            repo_root=ROOT, state={"picks": [{} for _ in range(5)] + [row]},
            state_date=adoption.RECORDING_DATE,
        )


def test_c7b_complete_live_row_fixture_is_independently_bound() -> None:
    fixture = ROOT / "tests/fixtures/c7b_live_failed_row.complete.json"
    row = json.loads(fixture.read_text(encoding="utf-8"))
    receipt = _receipt()
    assert adoption.row_fingerprint(row) == (
        "sha256:5499491676681c5ed854128f03dd35e65d4c43c4bad4b30f352032f10178a854"
    )
    assert receipt["live_row_fingerprint"]["sha256"] == adoption.row_fingerprint(row)
    loaded, _ = adoption.validate_c7b_failed_row_adoption(
        repo_root=ROOT, state={"picks": [{} for _ in range(5)] + [row]},
        state_date=adoption.RECORDING_DATE,
    )
    assert loaded["live_row_fingerprint"]["sha256"].endswith("5499491676681c5ed854128f03dd35e65d4c43c4bad4b30f352032f10178a854")


@pytest.mark.parametrize("mutation", [
    lambda row: row.pop("title"),
    lambda row: row.update({"confidence": "0.82"}),
    lambda row: row.update({"cover_route_regeneration_attempts": 2}),
    lambda row: row.update({"candidate_id": "other"}),
    lambda row: row.update({"failure_tuple_extra": True}),
    lambda row: row.update({"rc": 0}),
])
def test_c7b_complete_fixture_mutations_fail_closed(mutation) -> None:
    row = json.loads((ROOT / "tests/fixtures/c7b_live_failed_row.complete.json").read_text(encoding="utf-8"))
    mutation(row)
    with pytest.raises(adoption.C7bFailedRowAdoptionError):
        adoption.validate_c7b_failed_row_adoption(
            repo_root=ROOT, state={"picks": [{} for _ in range(5)] + [row]},
            state_date=adoption.RECORDING_DATE,
        )


def test_c7b_row_selector_index_and_date_are_exact() -> None:
    row = json.loads((ROOT / "tests/fixtures/c7b_live_failed_row.complete.json").read_text(encoding="utf-8"))
    with pytest.raises(adoption.C7bFailedRowAdoptionError, match="ROW_STATE_DRIFT"):
        adoption.validate_c7b_failed_row_adoption(
            repo_root=ROOT, state={"picks": [{} for _ in range(4)] + [row, {}]},
            state_date=adoption.RECORDING_DATE,
        )
    with pytest.raises(adoption.C7bFailedRowAdoptionError, match="DATE_DRIFT"):
        adoption.validate_c7b_failed_row_adoption(
            repo_root=ROOT, state={"picks": [{} for _ in range(5)] + [row]},
            state_date="2026-08-15",
        )


def test_c7b_unrelated_failed_rows_do_not_change_the_bound_target() -> None:
    row = json.loads((ROOT / "tests/fixtures/c7b_live_failed_row.complete.json").read_text(encoding="utf-8"))
    unrelated = {"candidate_id": "other_failed", "status": "failed", "rc": 1}
    state = {"picks": [{} for _ in range(5)] + [row, unrelated]}
    loaded, _ = adoption.validate_c7b_failed_row_adoption(
        repo_root=ROOT, state=state, state_date=adoption.RECORDING_DATE,
    )
    assert loaded["candidate_id"] == adoption.CANDIDATE_ID


def test_c7b_chain_descriptor_rejects_path_scope_symlink_hardlink_and_permissions(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    target = root / "assets" / "authority.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"{}")
    assert adoption._safe_chain_bytes(repo_root=root, relative=Path("assets/authority.json"), label="TEST") == b"{}"
    target.chmod(0o666)
    with pytest.raises(adoption.C7bFailedRowAdoptionError, match="PERMISSION_INVALID"):
        adoption._safe_chain_bytes(repo_root=root, relative=Path("assets/authority.json"), label="TEST")
    target.chmod(0o644)
    hardlink = root / "assets" / "hardlink.json"
    hardlink.hardlink_to(target)
    with pytest.raises(adoption.C7bFailedRowAdoptionError, match="PERMISSION_INVALID"):
        adoption._safe_chain_bytes(repo_root=root, relative=Path("assets/hardlink.json"), label="TEST")
    target.unlink()
    target.symlink_to("hardlink.json")
    with pytest.raises(adoption.C7bFailedRowAdoptionError, match="PATH_INVALID"):
        adoption._safe_chain_bytes(repo_root=root, relative=Path("assets/authority.json"), label="TEST")
    with pytest.raises(adoption.C7bFailedRowAdoptionError, match="PATH_INVALID"):
        adoption._safe_chain_bytes(repo_root=root, relative=Path("../authority.json"), label="TEST")
