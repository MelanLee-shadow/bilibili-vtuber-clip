"""Prepared Song packages bind delivery identity to one sealed selector row."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.build_song_review_manifest as builder
from tests.test_build_song_review_manifest import (
    BASENAME,
    CANDIDATE_ID,
    SOURCE_CANDIDATE_ID,
    _fixture,
    _prepared_fixture,
    _sha,
    _write_json,
)


@pytest.fixture
def cover_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reuse the package fixture's cover evidence; identity checks stay real.
    monkeypatch.setattr(builder, "validate_cover_route_decision", lambda *_a, **_k: True)
    monkeypatch.setattr(
        builder, "validate_rendered_text_pixel_evidence", lambda *_a, **_k: True
    )


def _build(prepared: dict, fx: dict, *, candidate_id: str = CANDIDATE_ID) -> dict:
    def completion(record: dict) -> dict:
        assert record["candidate_id"] == fx.get("selector_candidate_id", SOURCE_CANDIDATE_ID)
        return fx["completion"]

    return builder.build_prepared(
        prepared["package"],
        prepared_manifest_path=prepared["handle"].manifest_path,
        prepared_manifest_sha256="sha256:" + prepared["handle"].prepared_sha256,
        selector_summary_path=prepared["summary"],
        selector_summary_sha256=_sha(prepared["summary"]),
        candidate_id=candidate_id,
        recording_date="2026-07-25",
        runtime_root=prepared["runtime"],
        completion_verifier=completion,
    )


@pytest.mark.parametrize(
    "selector_candidate_id", [SOURCE_CANDIDATE_ID, CANDIDATE_ID], ids=["seeded", "same-id"]
)
def test_prepared_package_keeps_delivery_id_and_bound_selector_row(
    tmp_path: Path, cover_validation: None, selector_candidate_id: str
) -> None:
    fx = _fixture(tmp_path, source_candidate_id=selector_candidate_id)
    fx["selector_candidate_id"] = selector_candidate_id
    prepared = _prepared_fixture(fx, tmp_path, selector_candidate_id=selector_candidate_id)

    manifest = _build(prepared, fx)

    assert manifest["candidate_id"] == CANDIDATE_ID
    assert manifest["cover_route_attestations"][0]["candidate_id"] == CANDIDATE_ID
    assert manifest["upload_allowed"] is False
    assert manifest["prepared_authority"]["target_materialized"] is False
    item = manifest["items"][0]
    assert item["candidate_id"] == item["id"] == CANDIDATE_ID
    assert item["stem"] == BASENAME
    package = prepared["package"]
    active = json.loads((package / item["record"]).read_text(encoding="utf-8"))
    assert active["delivery_candidate_id"] == CANDIDATE_ID
    assert active["source_candidate_id"] == selector_candidate_id
    publish = json.loads((package / item["publish_json"]).read_text(encoding="utf-8"))
    assert publish["candidate_id"] == selector_candidate_id
    summary = json.loads((package / item["selector_summary"]).read_text(encoding="utf-8"))
    assert summary["records"][0]["candidate_id"] == selector_candidate_id
    delivery = json.loads((package / item["delivery_manifest"]).read_text(encoding="utf-8"))
    assert delivery["candidate_id"] == CANDIDATE_ID
    assert _sha(package / item["record"]) == "sha256:" + item["sha256"]["active_record"]
    assert _sha(package / item["publish_json"]) == "sha256:" + item["sha256"]["publish"]
    assert not (prepared["runtime"] / "lidousha").exists()


@pytest.mark.parametrize(
    ("defect", "message"),
    [
        ("missing-source", "active Song record.*source_candidate_id"),
        ("wrong-delivery", "active Song record.*delivery candidate"),
    ],
)
def test_prepared_package_refuses_unbound_active_identity(
    tmp_path: Path, cover_validation: None, defect: str, message: str
) -> None:
    fx = _fixture(tmp_path)
    active_path = fx["source_paths"]["active_record"]
    active = json.loads(active_path.read_text(encoding="utf-8"))
    if defect == "missing-source":
        del active["source_candidate_id"]
    else:
        active["delivery_candidate_id"] = "song_other_1"
    # Prepare after the identity defect: all transaction hashes remain valid.
    _write_json(active_path, active)
    prepared = _prepared_fixture(fx, tmp_path)

    with pytest.raises(builder.SongReviewManifestError, match=message):
        _build(prepared, fx)
    assert not prepared["package"].exists()


@pytest.mark.parametrize("row_count", [0, 2])
def test_prepared_package_requires_exactly_one_bound_selector_row(
    tmp_path: Path, cover_validation: None, row_count: int
) -> None:
    fx = _fixture(tmp_path)
    prepared = _prepared_fixture(fx, tmp_path)
    summary = json.loads(prepared["summary"].read_text(encoding="utf-8"))
    source_row = summary["records"][0]
    # A complete delivery-id decoy must never substitute for the bound source row.
    decoy = json.loads(json.dumps(source_row))
    decoy["candidate_id"] = CANDIDATE_ID
    summary["records"] = [source_row] * row_count + [decoy]
    _write_json(prepared["summary"], summary)

    with pytest.raises(builder.SongReviewManifestError, match="exactly one matching candidate row"):
        _build(prepared, fx)
    assert not prepared["package"].exists()


def test_prepared_package_refuses_materialized_delivery_id_in_selector_row(
    tmp_path: Path, cover_validation: None
) -> None:
    fx = _fixture(tmp_path)
    prepared = _prepared_fixture(fx, tmp_path)
    summary = json.loads(prepared["summary"].read_text(encoding="utf-8"))
    summary["records"][0]["materialized_recut"]["candidate_id"] = CANDIDATE_ID
    _write_json(prepared["summary"], summary)

    with pytest.raises(builder.SongReviewManifestError, match="ordinary completed STAGED"):
        _build(prepared, fx)
    assert not prepared["package"].exists()


@pytest.mark.parametrize("publish_surface", ["selector", "prepared"])
def test_prepared_package_refuses_publish_delivery_id_instead_of_selector_id(
    tmp_path: Path, cover_validation: None, publish_surface: str
) -> None:
    fx = _fixture(tmp_path)
    if publish_surface == "prepared":
        publish_path = fx["source_paths"]["publish"]
        publish = json.loads(publish_path.read_text(encoding="utf-8"))
        publish["candidate_id"] = CANDIDATE_ID
        _write_json(publish_path, publish)
    prepared = _prepared_fixture(fx, tmp_path)
    if publish_surface == "selector":
        summary = json.loads(prepared["summary"].read_text(encoding="utf-8"))
        publish_path = Path(summary["records"][0]["materialized_recut"]["publish_staging"]["publish_json_path"])
        publish = json.loads(publish_path.read_text(encoding="utf-8"))
        publish["candidate_id"] = CANDIDATE_ID
        _write_json(publish_path, publish)

    message = (
        "bound no-upload draft" if publish_surface == "selector"
        else "Song title/publish/final-cover authority"
    )
    with pytest.raises(builder.SongReviewManifestError, match=message):
        _build(prepared, fx)
    assert not prepared["package"].exists()


def test_prepared_package_refuses_wrong_handle_delivery_id(
    tmp_path: Path, cover_validation: None
) -> None:
    fx = _fixture(tmp_path)
    prepared = _prepared_fixture(fx, tmp_path)

    with pytest.raises(builder.SongReviewManifestError, match="handle candidate"):
        _build(prepared, fx, candidate_id="song_other_1")
    assert not prepared["package"].exists()
