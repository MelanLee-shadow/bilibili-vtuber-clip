"""Candidate-local packaging does not certify recovery of a paused batch.

Importer orchestration uses the existing synthetic fixture and explicit stub
quality gates. Manifest tests exercise the real guard up to cover resolution;
none of these tests supplies model/audio truth or a publishable package.
"""

from __future__ import annotations

import copy
import json

import pytest

from scripts import build_daily_review_manifest as daily
from src.autoslice import package_import as pi
from src.autoslice.publication_reconciliation import project_publication_closure
from tests.test_import_external_package import (
    CANDIDATE,
    _build_external_package,
    _run,
    _write_json,
)


def _paused(fixture):
    state = json.loads(fixture.state_path.read_text())
    state.update(
        status="paused_cpa_down",
        provider_error="historical outage",
        next_retry_at_epoch=100,
        source_integrity={"can_select": False},
    )
    state["picks"] = [
        {"candidate_id": CANDIDATE, "status": "review_ready", "rc": 0},
        {"candidate_id": "other-failed", "status": "failed", "rc": 2},
    ]
    state["pending_talk"] = [{"candidate_id": "other-talk", "status": "pending"}]
    state["pending_song"] = [{"candidate_id": "other-song", "status": "pending"}]
    _write_json(fixture.state_path, state)
    return state


def test_paused_ready_import_preserves_the_batch_and_every_unrelated_item(tmp_path):
    fixture = _build_external_package(tmp_path)
    before = _paused(fixture)
    receipt, code = _run(fixture, allow_new_pick=False)
    assert code == 0, receipt
    assert receipt["status"] == "REVIEW_READY"
    after = json.loads(fixture.state_path.read_text())
    assert after["status"] == "paused_cpa_down"
    for key in before:
        if key != "picks":
            assert after[key] == before[key], key
    assert after["picks"][1:] == before["picks"][1:]
    assert after["picks"][0]["external_package_import"]["created_pick_row"] is False


def test_paused_ready_dry_run_does_not_write_state_or_package(tmp_path):
    fixture = _build_external_package(tmp_path)
    _paused(fixture)
    before = fixture.state_path.read_bytes()
    receipt, code = _run(fixture, apply=False, allow_new_pick=False)
    assert code == 0, receipt
    assert receipt["status"] == "DRY_RUN_OK"
    assert fixture.state_path.read_bytes() == before
    assert not fixture.destination_package.exists()


class ReachedUnmodifiedCoverGate(Exception):
    pass


def test_manifest_uses_same_candidate_local_admission_without_relabeling_batch(
    tmp_path, monkeypatch
):
    fixture = _build_external_package(tmp_path)
    _paused(fixture)
    before = fixture.state_path.read_bytes()

    def stop_at_cover(**_):
        raise ReachedUnmodifiedCoverGate()

    monkeypatch.setattr(daily, "_resolve_final_cover", stop_at_cover)
    with pytest.raises(ReachedUnmodifiedCoverGate):
        daily.build(
            fixture.staging_package, fixture.state_path, fixture.deployed_commit_file, CANDIDATE
        )
    assert fixture.state_path.read_bytes() == before


@pytest.mark.parametrize(
    "fault",
    [
        "processing",
        "paused_runtime_invalid",
        "source_incomplete",
        "unknown_status",
        "absent",
        "failed",
        "candidate_rejected",
        "nonzero_rc",
        "bool_rc",
        "duplicate",
        "pending_talk",
        "pending_song",
        "songs",
        "malformed_queue",
    ],
)
def test_pause_exception_does_not_admit_other_states_or_queued_candidates(tmp_path, fault):
    fixture = _build_external_package(tmp_path)
    state = _paused(fixture)
    if fault in {"processing", "paused_runtime_invalid", "source_incomplete", "unknown_status"}:
        state["status"] = fault
    elif fault == "absent":
        state["picks"].pop(0)
    elif fault in {"failed", "candidate_rejected"}:
        state["picks"][0]["status"] = fault
    elif fault == "nonzero_rc":
        state["picks"][0]["rc"] = 2
    elif fault == "bool_rc":
        state["picks"][0]["rc"] = False
    elif fault == "duplicate":
        state["picks"].append(copy.deepcopy(state["picks"][0]))
    elif fault == "malformed_queue":
        state["pending_talk"] = {"candidate_id": CANDIDATE}
    else:
        state[fault].append({"candidate_id": CANDIDATE, "status": "pending"})
    _write_json(fixture.state_path, state)
    frozen = fixture.state_path.read_bytes()
    receipt, code = _run(fixture, apply=False, allow_new_pick=True)
    assert code == 2
    assert receipt["steps"][-1]["code"] == "BATCH_STATUS_NOT_REVIEWABLE"
    assert fixture.state_path.read_bytes() == frozen
    assert not fixture.destination_package.exists()
    with pytest.raises(daily.DailyManifestError, match="batch status not reviewable"):
        daily.build(
            fixture.staging_package, fixture.state_path, fixture.deployed_commit_file, CANDIDATE
        )


def test_paused_wrong_date_still_uses_existing_exact_date_guard(tmp_path):
    fixture = _build_external_package(tmp_path)
    state = _paused(fixture)
    with pytest.raises(pi.PackageImportError, match="STATE_DATE_MISMATCH"):
        pi.check_state_preconditions(
            state,
            candidate_id=CANDIDATE,
            date="2026-08-08",
            allow_new_pick=False,
            project_closure=project_publication_closure,
        )
