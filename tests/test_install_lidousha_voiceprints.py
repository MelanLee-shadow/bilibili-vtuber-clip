import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts import install_lidousha_voiceprints as installer


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path):
    reference_source = tmp_path / "reference-source"
    reference_source.mkdir()
    references = []
    for index in range(3):
        path = reference_source / f"enroll_{index}.wav"
        path.write_bytes(f"reference-{index}".encode())
        references.append({"id": f"ref-{index}", "filename": path.name, "sha256": _sha(path)})
    model_source = tmp_path / "model-source"
    model_source.mkdir()
    (model_source / "configuration.json").write_bytes(b"config")
    (model_source / "model.bin").write_bytes(b"weights")
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-voiceprint-profile.v1",
                "configuration_status": "READY",
                "references": references,
                "model": {"tree_sha256": installer.sha256_directory(model_source)},
            }
        ),
        encoding="utf-8",
    )
    return profile, reference_source, model_source


def _run(monkeypatch, profile, reference_source, reference_target, model_source, model_target):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "install_lidousha_voiceprints.py",
            "--profile",
            str(profile),
            "--source-dir",
            str(reference_source),
            "--target-dir",
            str(reference_target),
            "--model-source-dir",
            str(model_source),
            "--model-target-dir",
            str(model_target),
        ],
    )
    return installer.main()


def test_installs_private_references_and_durable_model_idempotently(tmp_path, monkeypatch):
    profile, reference_source, model_source = _fixture(tmp_path)
    reference_target = tmp_path / "references"
    model_target = tmp_path / "models" / "campplus"

    assert _run(monkeypatch, profile, reference_source, reference_target, model_source, model_target) == 0
    first_hash = installer.sha256_directory(model_target)
    assert oct(reference_target.stat().st_mode & 0o777) == "0o700"
    assert all(oct(path.stat().st_mode & 0o777) == "0o600" for path in reference_target.iterdir())

    assert _run(monkeypatch, profile, reference_source, reference_target, model_source, model_target) == 0
    assert installer.sha256_directory(model_target) == first_hash


def test_existing_hash_mismatched_model_target_fails_closed(tmp_path, monkeypatch):
    profile, reference_source, model_source = _fixture(tmp_path)
    reference_target = tmp_path / "references"
    model_target = tmp_path / "models" / "campplus"
    model_target.mkdir(parents=True)
    (model_target / "model.bin").write_bytes(b"wrong")

    with pytest.raises(SystemExit, match="existing model target is hash-mismatched"):
        _run(monkeypatch, profile, reference_source, reference_target, model_source, model_target)
