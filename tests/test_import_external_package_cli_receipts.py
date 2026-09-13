"""CLI receipt output must not turn an implicit dry run into a package write."""
from pathlib import Path
import json

import pytest
from scripts import import_external_package as cli


CID = "auto_receipt_output"
DATE = "2026-08-25"


def _paths(tmp_path: Path) -> tuple[list[str], Path]:
    source = tmp_path / "source"
    source.mkdir()
    base = tmp_path / "runtime"
    package = base / "out" / DATE / CID / "replacement_recuts"
    package.mkdir(parents=True)
    return ["--source", str(source), "--date", DATE, "--candidate", CID,
            "--base", str(base)], package / f"{CID}.external-import-receipt.json"


@pytest.mark.parametrize("existing_receipt", [False, True])
def test_real_refused_dry_run_preserves_package(tmp_path: Path, existing_receipt: bool) -> None:
    args, receipt = _paths(tmp_path)
    # The real preflight refuses this incomplete source before any provider runs.
    if existing_receipt:
        receipt.write_bytes(b"previous immutable import receipt\n")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in receipt.parent.iterdir()}
    assert cli.main(args) == 2
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
             for p in receipt.parent.iterdir()}
    assert after == before


def test_successful_dry_run_only_prints_implicit_receipt(tmp_path: Path, monkeypatch, capsys) -> None:
    args, receipt = _paths(tmp_path)
    document = {"status": "DRY_RUN_OK", "upload_allowed": False}

    def result(**kwargs):
        assert kwargs["apply"] is False
        return document, 0

    monkeypatch.setattr(cli, "run_import", result)
    assert cli.main(args) == 0
    assert not receipt.exists()
    assert json.loads(capsys.readouterr().out) == document


@pytest.mark.parametrize(("status", "code"), [("DRY_RUN_OK", 0), ("REFUSED", 2)])
def test_explicit_dry_run_receipt_is_still_written(tmp_path: Path, monkeypatch, status, code) -> None:
    args, implicit = _paths(tmp_path)
    explicit = tmp_path / "requested-diagnostic.json"
    document = {"status": status, "upload_allowed": False}
    monkeypatch.setattr(cli, "run_import", lambda **kwargs: (document, code))
    assert cli.main([*args, "--receipt", str(explicit)]) == code
    assert json.loads(explicit.read_bytes()) == document
    assert not implicit.exists()


def test_apply_keeps_default_receipt_output(tmp_path: Path, monkeypatch) -> None:
    args, receipt = _paths(tmp_path)
    document = {"status": "IMPORTED", "upload_allowed": False}

    def result(**kwargs):
        assert kwargs["apply"] is True
        return document, 0

    monkeypatch.setattr(cli, "run_import", result)
    assert cli.main([*args, "--apply"]) == 0
    assert json.loads(receipt.read_bytes()) == document
