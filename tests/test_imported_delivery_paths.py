"""Maintenance resolves the native imported package, not a retained legacy copy."""
import copy
import json
from pathlib import Path

import pytest

from scripts import session_autoslice as runner
from src.autoslice.cover_maintenance import delivered_paths
from tests.test_import_external_package import CANDIDATE, DATE, STEM, _build_external_package, _run


def _imported(tmp_path, monkeypatch):
    fixture = _build_external_package(tmp_path)
    receipt, rc = _run(fixture)
    assert rc == 0, receipt
    row = json.loads(fixture.state_path.read_bytes())["picks"][0]
    monkeypatch.setattr(runner, "BASE", fixture.base)
    monkeypatch.setattr(runner, "REPO_ROOT", fixture.repo_root)
    legacy = fixture.repo_root / "lidousha" / DATE / f"old__{CANDIDATE}.mp4"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"superseded video")
    legacy.with_suffix(".cover.png").write_bytes(b"retained cover")
    row["delivered"] = str(legacy)
    expected = (fixture.destination_package / f"{STEM}.mp4",
                fixture.destination_package / f"{STEM}.cover.png")
    return fixture, row, legacy, expected


@pytest.mark.parametrize("retain_legacy", [True, False])
def test_verified_import_precedes_legacy_delivery(tmp_path, monkeypatch, retain_legacy):
    fixture, row, legacy, expected = _imported(tmp_path, monkeypatch)
    if not retain_legacy:
        row.pop("delivered")
    before = copy.deepcopy(row)
    state = fixture.state_path.read_bytes()
    assert delivered_paths(DATE, row) == expected
    assert row == before and fixture.state_path.read_bytes() == state
    assert legacy.read_bytes() == b"superseded video"


@pytest.mark.parametrize("drift", ["status", "schema", "null", "journal_hash", "journal_bytes",
    "video_bytes", "cover_bytes", "record_bytes", "state_video_hash", "state_title",
    "media_path", "alias_path", "candidate", "parent_symlink"])
def test_invalid_import_never_falls_back_to_old_delivery(tmp_path, monkeypatch, drift):
    fixture, row, legacy, expected = _imported(tmp_path, monkeypatch)
    binding = row["external_package_import"]
    if drift == "status":
        binding["status"] = "PREPARED"
    elif drift == "schema":
        binding["schema_version"] = "unverified"
    elif drift == "null":
        row["external_package_import"] = None
    elif drift == "journal_hash":
        binding["package_relocation_journal_sha256"] = "sha256:" + "0" * 64
    elif drift == "journal_bytes":
        Path(binding["package_relocation_journal_path"]).write_text("{}")
    elif drift in {"video_bytes", "cover_bytes"}:
        expected[0 if drift == "video_bytes" else 1].write_bytes(b"drift")
    elif drift == "record_bytes":
        Path(binding["record_path"]).write_text("{}")
    elif drift == "state_video_hash":
        row["video_sha256"] = "sha256:" + "0" * 64
    elif drift == "state_title":
        row["title"] = "Different title"
    elif drift == "media_path":
        binding["burned_video_path"] = str(legacy)
    elif drift == "alias_path":
        binding["same_stem_cover_path"] = str(legacy.with_suffix(".cover.png"))
    elif drift == "candidate":
        row["candidate_id"] = "../wrong"
    else:
        target = fixture.destination_package.with_name("hidden-original")
        fixture.destination_package.rename(target)
        fixture.destination_package.symlink_to(target, target_is_directory=True)
    before = copy.deepcopy(row)
    assert delivered_paths(DATE, row) is None
    assert row == before and legacy.read_bytes() == b"superseded video"


def test_legacy_delivery_without_import_binding_keeps_existing_rule(tmp_path, monkeypatch):
    _fixture, row, legacy, _expected = _imported(tmp_path, monkeypatch)
    row.pop("external_package_import")
    assert delivered_paths(DATE, row) == (legacy, legacy.with_suffix(".cover.png"))
