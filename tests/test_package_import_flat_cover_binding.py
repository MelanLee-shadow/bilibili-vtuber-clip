"""Flat, same-stem covers retain historical absolute V4 witness paths.

Synthetic package/relocation fixtures exercise the actual state-bind consumer;
no model, image-identity judgment, package audit, or publication is simulated.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.autoslice import package_import as pi
from tests.test_import_external_package import (
    CANDIDATE,
    SRC_PACKAGE,
    SRC_REPO,
    SRC_WORKSPACE,
    STEM,
    _build_external_package,
    _write_json,
)


def _relocated(tmp_path: Path, *, flat: bool = True, canonical_flat: bool = False):
    fx = _build_external_package(tmp_path)
    if flat:
        publish_path = fx.staging_package / f"{CANDIDATE}.recut.publish.json"
        record_path = fx.staging_package / f"{CANDIDATE}.record.json"
        publish = json.loads(publish_path.read_text())
        record = json.loads(record_path.read_text())
        generation = publish["cover_generation"]
        old = fx.staging_package / "covers/final-cover.png"
        name = f"{STEM}.cover.png"
        # Both aliases carry the SAME frozen image, exactly as the real intake.
        (fx.staging_package / name).write_bytes(old.read_bytes())
        alias = fx.staging_package / "replacement_recuts" / name
        alias.parent.mkdir()
        alias.write_bytes(old.read_bytes())
        generation["final_cover"] = f"{SRC_PACKAGE}/{name}"
        generation["final_host_identity_verification"] = {
            "schema_version": "lidousha-cover-final-host-identity-verification.v4",
            "final_cover_path": f"{SRC_PACKAGE}/{name}",
            "final_cover_sha256": generation["final_cover_sha256"],
        }
        publish["cover_path"] = generation["final_cover"]
        record["publish_staging"]["cover_generation"] = copy.deepcopy(generation)
        record["publish_staging"]["cover_path"] = generation["final_cover"]
        record["artifact_hashes"]["publish_draft_sha256"] = "sha256:" + _write_json(
            publish_path, publish
        )
        _write_json(record_path, record)
    if canonical_flat:
        publish_path = fx.staging_package / f"{CANDIDATE}.recut.publish.json"
        record_path = fx.staging_package / f"{CANDIDATE}.record.json"
        publish = json.loads(publish_path.read_text())
        record = json.loads(record_path.read_text())
        generation = publish["cover_generation"]
        name = f"{STEM}.cover.png"
        (fx.staging_package / name).write_bytes(
            (fx.staging_package / "covers/final-cover.png").read_bytes()
        )
        generation["final_host_identity_verification"] = {
            "schema_version": "lidousha-cover-final-host-identity-verification.v4",
            "final_cover_path": name,
            "final_cover_sha256": generation["final_cover_sha256"],
        }
        record["publish_staging"]["cover_generation"] = copy.deepcopy(generation)
        record["artifact_hashes"]["publish_draft_sha256"] = "sha256:" + _write_json(
            publish_path, publish
        )
        _write_json(record_path, record)
    plan = pi.plan_import(
        source_package_dir=fx.staging_package,
        destination_package_root=fx.destination_package,
        destination_repo_root=fx.repo_root,
        candidate_id=CANDIDATE,
        source_repo_root=SRC_REPO,
        source_workspace_root=SRC_WORKSPACE,
    )
    pi.execute_copy(plan, apply=True)
    pi.relocate_package(
        package_root=fx.destination_package, candidate_id=CANDIDATE, roots=plan.roots, apply=True
    )
    pi.materialize_same_stem_cover(
        package_root=fx.destination_package,
        documents=pi.read_package_documents(fx.destination_package, CANDIDATE),
        apply=True,
    )
    return fx


def test_flat_same_stem_declared_cover_binds_without_rewriting_v4_witness(tmp_path):
    fx = _relocated(tmp_path)
    before = {p: p.read_bytes() for p in fx.destination_package.rglob("*") if p.is_file()}
    bound = pi.read_bound_package(package_root=fx.destination_package, candidate_id=CANDIDATE)
    assert bound.cover_path == fx.destination_package / f"{STEM}.cover.png"
    assert bound.cover_path == bound.same_stem_cover_path
    assert (
        bound.publish["cover_generation"]["final_host_identity_verification"]["final_cover_path"]
        == f"{SRC_PACKAGE}/{STEM}.cover.png"
    )
    assert {p: p.read_bytes() for p in before} == before


def test_existing_nested_cover_binding_is_unchanged(tmp_path):
    fx = _relocated(tmp_path, flat=False)
    bound = pi.read_bound_package(package_root=fx.destination_package, candidate_id=CANDIDATE)
    assert bound.cover_path == fx.destination_package / "covers/final-cover.png"
    assert bound.cover_path.read_bytes() == bound.same_stem_cover_path.read_bytes()


@pytest.mark.parametrize(
    "fault",
    ["flat_missing", "flat_drift", "flat_symlink", "historical_alias_drift", "journal_drift"],
)
def test_same_stem_compatibility_does_not_waive_identity_checks(tmp_path, fault):
    fx = _relocated(tmp_path)
    flat = fx.destination_package / f"{STEM}.cover.png"
    alias = fx.destination_package / "replacement_recuts" / flat.name
    if fault == "flat_missing":
        flat.unlink()
    elif fault == "flat_drift":
        flat.write_bytes(b"a different cover")
    elif fault == "flat_symlink":
        flat.unlink()
        flat.symlink_to(alias)
    elif fault == "historical_alias_drift":
        alias.write_bytes(b"invalid frozen-witness alias")
    else:
        p = fx.destination_package / f"{CANDIDATE}.record.json"
        p.write_bytes(p.read_bytes() + b"\n")
    with pytest.raises(pi.PackageImportError) as caught:
        pi.read_bound_package(package_root=fx.destination_package, candidate_id=CANDIDATE)
    expected_codes = {
        "flat_missing": "SOURCE_FILE_MISSING",
        "flat_drift": "SAME_STEM_COVER_DRIFT",
        "flat_symlink": "UNSAFE_PATH_SYMLINK",
        "historical_alias_drift": "DECLARED_ARTIFACT_SHA_DRIFT",
        "journal_drift": "RELOCATION_POSTIMAGE_DRIFT",
    }
    assert caught.value.as_dict()["code"] == expected_codes[fault]


def test_canonical_flat_v4_binds_while_proving_declared_nested_generation_alias(tmp_path):
    fx = _relocated(tmp_path, flat=False, canonical_flat=True)
    before = {p: p.read_bytes() for p in fx.destination_package.rglob("*") if p.is_file()}
    bound = pi.read_bound_package(package_root=fx.destination_package, candidate_id=CANDIDATE)
    assert bound.cover_path == bound.same_stem_cover_path
    assert bound.publish["cover_generation"]["final_cover"] == str(
        fx.destination_package / "covers/final-cover.png"
    )
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize(
    "fault,code",
    [
        ("missing", "SOURCE_FILE_MISSING"),
        ("drift", "DECLARED_ARTIFACT_SHA_DRIFT"),
        ("symlink", "UNSAFE_PATH_SYMLINK"),
    ],
)
def test_canonical_flat_v4_does_not_hide_a_broken_declared_alias(tmp_path, fault, code):
    fx = _relocated(tmp_path, flat=False, canonical_flat=True)
    alias = fx.destination_package / "covers/final-cover.png"
    if fault == "missing":
        alias.unlink()
    elif fault == "drift":
        alias.write_bytes(b"wrong cover")
    else:
        alias.unlink()
        alias.symlink_to(fx.destination_package / f"{STEM}.cover.png")
    with pytest.raises(pi.PackageImportError) as caught:
        pi.read_bound_package(package_root=fx.destination_package, candidate_id=CANDIDATE)
    assert caught.value.as_dict()["code"] == code
