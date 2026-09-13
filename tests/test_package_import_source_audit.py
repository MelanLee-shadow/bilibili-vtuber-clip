"""A relocated talk package must obtain its own audit, not import a stale one."""

import json

import pytest

from tests.test_import_external_package import (
    StubGateRunner, _build_external_package, _run, _step,
)


@pytest.mark.parametrize("old_passed", [True, False])
def test_source_audit_is_preserved_without_becoming_destination_audit(tmp_path, old_passed):
    fixture = _build_external_package(tmp_path)
    source = fixture.staging_package / "package_audit.json"
    before = json.dumps({"passed": old_passed, "root": "historical-source"}).encode()
    source.write_bytes(before)
    gate = StubGateRunner(fixture)

    receipt, code = _run(fixture, gate=gate)

    assert code == 0, receipt
    assert source.read_bytes() == before
    assert (fixture.destination_package / "package_audit.json").read_bytes() == gate.audit_stdout.encode()
    assert any("audit_review_package.py" in call[1] for call in gate.calls)
    assert "package_audit.json" not in {row["relative"] for row in _step(receipt, "COPY")["files"]}


def test_source_pass_does_not_bypass_failed_current_audit(tmp_path):
    fixture = _build_external_package(tmp_path)
    source = fixture.staging_package / "package_audit.json"
    before = b'{"passed":true,"root":"historical-source"}\n'
    source.write_bytes(before)
    state_before = fixture.state_path.read_bytes()

    receipt, code = _run(fixture, gate=StubGateRunner(fixture, audit_passed=False))

    assert code == 2
    assert _step(receipt, "AUDIT")["code"] == "PACKAGE_AUDIT_BLOCKED"
    assert fixture.state_path.read_bytes() == state_before
    assert source.read_bytes() == before
    assert not (fixture.destination_package / "package_audit.json").exists()


def test_source_audit_cannot_replace_a_different_existing_destination_audit(tmp_path):
    fixture = _build_external_package(tmp_path)
    assert _run(fixture)[1] == 0
    destination = fixture.destination_package / "package_audit.json"
    before = b'{"passed":true,"root":"existing-destination-evidence"}\n'
    destination.write_bytes(before)
    source = fixture.staging_package / "package_audit.json"
    source.write_bytes(b'{"passed":true,"root":"another-source"}\n')

    receipt, code = _run(fixture)

    assert code == 2
    assert _step(receipt, "AUDIT")["code"] == "AUDIT_REPORT_OVERWRITE_REFUSED"
    assert destination.read_bytes() == before


def test_only_root_derived_audit_is_excluded_not_nested_evidence(tmp_path):
    fixture = _build_external_package(tmp_path)
    nested = fixture.staging_package / "evidence" / "package_audit.json"
    nested.parent.mkdir()
    nested.write_bytes(b"historical nested evidence, not the destination audit")

    receipt, code = _run(fixture)

    assert code == 0, receipt
    assert (fixture.destination_package / "evidence" / "package_audit.json").read_bytes() == nested.read_bytes()


def test_source_audit_symlink_is_still_unsafe(tmp_path):
    fixture = _build_external_package(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside must remain unchanged")
    (fixture.staging_package / "package_audit.json").symlink_to(outside)

    receipt, code = _run(fixture)

    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] == "UNSAFE_PATH_SYMLINK"
    assert outside.read_bytes() == b"outside must remain unchanged"
    assert not fixture.destination_package.exists()
