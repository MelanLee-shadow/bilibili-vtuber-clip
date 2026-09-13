"""Ordinary producer records explicitly carry a null opt-in replay baseline."""

import json

import pytest

from tests.test_import_external_package import (
    CANDIDATE, SRC_PACKAGE, StubGateRunner, _build_external_package, _run, _step, _write_json,
)


def _ordinary(tmp_path):
    fixture = _build_external_package(tmp_path)
    p = fixture.staging_package / f"{CANDIDATE}.record.json"
    record = json.loads(p.read_bytes())
    record["redelivery_baseline"] = None
    record["redelivery_baseline_audit_path"] = None
    _write_json(p, record)
    return fixture, p, record


def test_explicit_ordinary_null_baseline_is_not_fabricated(tmp_path):
    fixture, p, record = _ordinary(tmp_path)
    before = p.read_bytes()
    receipt, code = _run(fixture)
    assert code == 0, receipt
    after = json.loads((fixture.destination_package / p.name).read_bytes())
    assert after["redelivery_baseline"] is None
    assert after["redelivery_baseline_audit_path"] is None
    assert p.read_bytes() == before
    state = json.loads(fixture.state_path.read_bytes())
    assert state["picks"][0]["summary"]["redelivery_baseline_status"] is None
    assert _step(receipt, "AUDIT")["status"] == "PASS"


def test_ordinary_no_baseline_still_runs_current_audit_and_rolls_back(tmp_path):
    fixture, _p, _record = _ordinary(tmp_path)
    state_before = fixture.state_path.read_bytes()
    receipt, code = _run(fixture, gate=StubGateRunner(fixture, audit_passed=False))
    assert code == 2
    assert _step(receipt, "AUDIT")["code"] == "PACKAGE_AUDIT_BLOCKED"
    assert fixture.state_path.read_bytes() == state_before


@pytest.mark.parametrize("change", ["missing_baseline", "missing_path", "declared_path", "string", "list", "bool"])
def test_incomplete_or_malformed_baseline_still_refused(tmp_path, change):
    fixture, p, record = _ordinary(tmp_path)
    if change == "missing_baseline":
        record.pop("redelivery_baseline")
    elif change == "missing_path":
        record.pop("redelivery_baseline_audit_path")
    elif change == "declared_path":
        record["redelivery_baseline_audit_path"] = f"{SRC_PACKAGE}/{CANDIDATE}.redelivery-baseline.json"
    else:
        record["redelivery_baseline"] = {"string": "bad", "list": [], "bool": False}[change]
    _write_json(p, record)
    receipt, code = _run(fixture)
    assert code == 2
    assert _step(receipt, "STATE_BIND")["code"] == "PACKAGE_DOCUMENT_INVALID"
