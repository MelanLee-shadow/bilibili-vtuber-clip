"""Ordinary V4 receipts can retain absolute producer paths after a copy.

These are synthetic hash fixtures testing only locator compatibility, not image
identity judgments, complete package audits, or production import admission.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest

from src.autoslice import package_import as pi


def _fixture(root: Path, *, absolute: bool):
    cover = root / "covers" / "final.cover.png"
    cover.parent.mkdir()
    cover.write_bytes(b"synthetic final cover fixture")
    source = "/unmounted-producer/out/candidate/replacement_recuts/covers/final.cover.png"
    digest = "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest()
    verification = {
        "schema_version": "lidousha-cover-final-host-identity-verification.v4",
        "final_cover_path": source if absolute else "covers/final.cover.png",
        "final_cover_sha256": digest,
    }
    return cover, {
        "final_cover": source,
        "final_cover_sha256": digest,
        "final_host_identity_verification": verification,
    }


@pytest.mark.parametrize("absolute", [False, True])
def test_copied_cover_resolves_without_opening_producer_path(tmp_path, absolute):
    cover, generation = _fixture(tmp_path, absolute=absolute)
    original = copy.deepcopy(generation)
    assert pi._package_internal_cover(tmp_path, generation) == cover
    assert generation == original


def test_pre_e166_absolute_v4_alias_is_not_rejected(tmp_path):
    cover, generation = _fixture(tmp_path, absolute=True)
    # This is exactly the alias the original importer used before E166.
    historical_alias = (
        tmp_path
        / Path(generation["final_cover"]).parent.name
        / Path(generation["final_cover"]).name
    )
    assert historical_alias == cover and historical_alias.is_file()
    assert pi._package_internal_cover(tmp_path, generation) == historical_alias


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("problem", ["hash", "missing", "symlink"])
def test_both_receipt_forms_still_reject_bad_local_bytes(tmp_path, absolute, problem):
    cover, generation = _fixture(tmp_path, absolute=absolute)
    if problem == "hash":
        cover.write_bytes(b"changed bytes")
    elif problem == "missing":
        cover.unlink()
    else:
        target = tmp_path / "other.png"
        target.write_bytes(cover.read_bytes())
        cover.unlink()
        cover.symlink_to(target)
    with pytest.raises(pi.PackageImportError, match="DECLARED_ARTIFACT_SHA_DRIFT"):
        pi._package_internal_cover(tmp_path, generation)


def test_relative_witness_escape_never_falls_back_to_old_alias(tmp_path):
    _, generation = _fixture(tmp_path, absolute=False)
    generation["final_host_identity_verification"]["final_cover_path"] = "../outside.png"
    with pytest.raises(pi.PackageImportError, match="DECLARED_ARTIFACT_SHA_DRIFT"):
        pi._package_internal_cover(tmp_path, generation)


@pytest.mark.parametrize(
    "locator", [None, "", "./covers/final.cover.png", "covers/../covers/final.cover.png"]
)
def test_invalid_relative_receipt_is_not_reinterpreted_as_absolute(tmp_path, locator):
    _, generation = _fixture(tmp_path, absolute=False)
    generation["final_host_identity_verification"]["final_cover_path"] = locator
    with pytest.raises(pi.PackageImportError, match="DECLARED_ARTIFACT_SHA_DRIFT"):
        pi._package_internal_cover(tmp_path, generation)


@pytest.mark.parametrize("declared", [None, "", "/"])
def test_absolute_receipt_requires_a_usable_generation_alias(tmp_path, declared):
    _, generation = _fixture(tmp_path, absolute=True)
    generation["final_cover"] = declared
    with pytest.raises(pi.PackageImportError, match="DECLARED_ARTIFACT_SHA_DRIFT"):
        pi._package_internal_cover(tmp_path, generation)


def test_canonical_flat_v4_cover_does_not_inherit_the_legacy_parent(tmp_path):
    cover, generation = _fixture(tmp_path, absolute=False)
    flat = tmp_path / "final.cover.png"
    flat.write_bytes(cover.read_bytes())
    generation["final_host_identity_verification"]["final_cover_path"] = flat.name
    assert pi._package_internal_cover(tmp_path, generation) == flat
