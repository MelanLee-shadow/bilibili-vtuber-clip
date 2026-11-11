"""换源授权凭“发布登记＋首发回执”认稿件，不再读本地首发包。

首发包（视频、封面、record）全部删除后，只要首发回执、四份小证据和发布登记在，
authority 仍能建出并重放；登记或回执任何一项不对都拒绝。
"""

from __future__ import annotations

import copy
import json

import pytest

from src.autoslice import new_bv_repair_authority as authorities
from src.autoslice.publication_reconciliation import authority_sidecar_path
from tests.test_publication_reconciliation import (
    BVID, CANDIDATE, DATE, TITLE, _new_bv_fixture, _reconcile_new,
)

_PACKAGE_FILES = ("clip.mp4", "clip.cover.png", "clip.record.json")


@pytest.fixture
def closure(tmp_path):
    fixture = _new_bv_fixture(tmp_path)
    _reconcile_new(fixture)
    receipt = authority_sidecar_path(fixture["manifest_path"])
    registry = json.loads(fixture["registry_path"].read_text(encoding="utf-8"))
    (row,) = registry["entries"]
    assert row["publication_reconciliation"]["authority"]["path"] == str(receipt.resolve())
    package = fixture["manifest_path"].parent
    for name in _PACKAGE_FILES:
        (package / name).unlink()
    return fixture, receipt, registry


_REGISTRY_FILES = iter(range(10**6))


def _registry_file(tmp_path, registry):
    """Write a (possibly damaged) registry to its own temp file; the witness reads it by explicit path."""
    path = tmp_path / f"registry-{next(_REGISTRY_FILES)}.json"
    path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    return path


def _build(receipt, registry_path):
    return authorities.build_new_bv_repair_authority(
        receipt, candidate_id=CANDIDATE, title=TITLE, registry_path=registry_path)


def _validate(authority, registry_path, **kw):
    return authorities.validate_new_bv_repair_authority(
        authority, candidate_id=CANDIDATE, registry_path=registry_path, **kw)


def test_authority_builds_and_replays_after_the_first_package_is_deleted(closure, tmp_path):
    fixture, receipt, registry = closure
    assert not (fixture["manifest_path"].parent / "clip.record.json").exists()
    path = fixture["registry_path"]
    authority = _build(receipt, path)
    assert authority["bvid"] == BVID and authority["recording_date"] == DATE
    assert authority["candidate_id"] == CANDIDATE
    validated = _validate(authority, path)
    assert validated == authority
    assert authorities.replay_first_publication_manifest(
        validated, registry_path=path) == fixture["manifest"]


def _row(registry):
    return registry["entries"][0]


def _no_row(registry):
    registry["entries"] = []


def _two_rows(registry):
    twin = copy.deepcopy(_row(registry))
    twin["candidate_id"] = "other_candidate"
    registry["entries"].append(twin)


def _not_published(registry):
    _row(registry)["status"] = "released_for_upload"


def _other_candidate(registry):
    _row(registry)["candidate_id"] = "other_candidate"


def _other_date(registry):
    _row(registry)["recording_date"] = "2026-07-30"


def _other_receipt_sha(registry):
    _row(registry)["publication_reconciliation"]["authority"]["sha256"] = "0" * 64


def _other_receipt_path(registry):
    _row(registry)["publication_reconciliation"]["authority"]["path"] += ".other"


def _other_receipt_bytes(registry):
    _row(registry)["publication_reconciliation"]["authority"]["bytes"] += 1


def _no_binding(registry):
    del _row(registry)["publication_reconciliation"]["authority"]


@pytest.mark.parametrize("damage, detail", [
    (_no_row, "没有这条稿件"),
    (_two_rows, "more than one row"),
    (_not_published, "not published"),
    (_other_candidate, "candidate_id differs"),
    (_other_date, "recording_date differs"),
    (_other_receipt_sha, "does not bind this publication receipt"),
    (_other_receipt_path, "does not bind this publication receipt"),
    (_other_receipt_bytes, "does not bind this publication receipt"),
    (_no_binding, "does not bind this publication receipt"),
])
def test_registry_that_does_not_witness_the_receipt_is_refused(closure, tmp_path, damage, detail):
    _, receipt, registry = closure
    damage(registry)
    with pytest.raises(authorities.NewBvRepairAuthorityError, match=detail):
        _build(receipt, _registry_file(tmp_path, registry))


@pytest.mark.parametrize("damage, detail", [
    (_no_row, "没有这条稿件"),
    (_other_receipt_sha, "does not bind this publication receipt"),
    (_other_candidate, "candidate_id differs"),
])
def test_registry_damage_after_build_is_refused_on_replay_and_manifest(closure, tmp_path, damage, detail):
    fixture, receipt, registry = closure
    authority = _build(receipt, fixture["registry_path"])
    damage(registry)
    damaged = _registry_file(tmp_path, registry)
    with pytest.raises(authorities.NewBvRepairAuthorityError, match=detail):
        _validate(authority, damaged)
    with pytest.raises(authorities.NewBvRepairAuthorityError, match=detail):
        authorities.replay_first_publication_manifest(authority, registry_path=damaged)
    # the shape-only form (maker host without the evidence) never consults the registry
    _validate(authority, damaged, replay_evidence=False)


def test_unreadable_registry_is_refused(closure, tmp_path):
    _, receipt, _ = closure
    bad_schema = _registry_file(tmp_path, {"schema_version": "nope", "entries": []})
    for path in (tmp_path / "missing-registry.json", bad_schema):  # OSError, ValueError
        with pytest.raises(authorities.NewBvRepairAuthorityError, match="publication registry unreadable"):
            _build(receipt, path)


def test_receipt_drift_is_refused(closure):
    fixture, receipt, _ = closure
    path = fixture["registry_path"]
    authority = _build(receipt, path)
    doc = json.loads(receipt.read_text(encoding="utf-8"))
    doc["created_at"] = "2030-01-01T00:00:00+00:00"
    receipt.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(authorities.NewBvRepairAuthorityError, match="publication receipt drifted"):
        _validate(authority, path)


@pytest.mark.parametrize("key", ["manifest", "public_verify", "season_verify", "uploaded"])
def test_evidence_drift_is_refused(closure, key):
    fixture, receipt, _ = closure
    authority = _build(receipt, fixture["registry_path"])
    path = fixture[{"manifest": "manifest_path", "public_verify": "public_path",
                    "season_verify": "season_path", "uploaded": "uploaded_path"}[key]]
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(Exception, match="hash/bytes drifted"):
        _validate(authority, fixture["registry_path"])


def test_a_manuscript_repaired_since_is_still_witnessed_by_its_row(closure, tmp_path):
    """After a same-BV repair the row binds the completed repair, not the first receipt: the registry still says
    which manuscript the BVID is (a later repair chains from the completed one)."""
    _fixture, receipt, registry = closure
    projection = _row(registry)["publication_reconciliation"]
    projection["status"] = "VERIFIED_SAME_BV"
    projection["authority"] = {"path": str(receipt) + ".same-bv-repair-completed.json", "sha256": "1" * 64, "bytes": 9}
    path = _registry_file(tmp_path, registry)
    authority = _build(receipt, path)
    assert _validate(authority, path) == authority


def test_a_repaired_row_of_another_manuscript_is_still_refused(closure, tmp_path):
    _fixture, receipt, registry = closure
    _row(registry)["publication_reconciliation"]["status"] = "VERIFIED_SAME_BV"
    _other_candidate(registry)
    with pytest.raises(authorities.NewBvRepairAuthorityError, match="candidate_id differs"):
        _build(receipt, _registry_file(tmp_path, registry))
