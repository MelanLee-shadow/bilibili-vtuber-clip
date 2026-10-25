"""Publication identity producers, consumers, historical receipts and controls."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from scripts.run_title_cover_joint_qc import resolve_candidate_id
from src.autoslice import authorized_upload_final_media as final_media
from src.autoslice import fastlane_c2_authorized_upload as c2_upload
from src.autoslice import publication_readiness as readiness
from src.autoslice import publication_reconciliation as reconciliation
from src.autoslice import publication_registry as registry
from src.autoslice.publication_identity import publication_identity
from src.autoslice.publication_manifest_prepare import prepare_manifest_from_readiness
from src.autoslice.song_delivery import _write_song_active_record
from src.autoslice.upload_ledger_scope import content_identity, identity_problems, row_matches_target
from tests.test_publication_manifest_prepare import _repository, _row
from tests.test_publication_readiness import _package
from tests.test_upload_ledger_scope import bound_manifest


ROOT = Path(__file__).resolve().parents[1]
CONSUMERS = (
    "src/autoslice/authorized_upload_final_media.py",
    "src/autoslice/publication_registry.py",
    "src/autoslice/publication_readiness.py",
    "src/autoslice/publication_manifest_prepare.py",
    "src/autoslice/publication_reconciliation.py",
    "src/autoslice/fastlane_c2_authorized_upload.py",
    "src/autoslice/upload_ledger_scope.py",
    "scripts/run_title_cover_joint_qc.py",
)


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _entry(path):
    raw = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _record(delivery="song_130025_580", source="seededsong_120000_349590"):
    return {"candidate_id": source, "delivery_candidate_id": delivery, "source_candidate_id": source}


def test_pair_interpretation_is_absent_from_every_consumer():
    for relative in CONSUMERS:
        tree = ast.parse((ROOT / relative).read_text())
        assert not [node for node in ast.walk(tree) if isinstance(node, ast.Constant)
                    and node.value in ("delivery_candidate_id", "source_candidate_id")], relative
        assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "publication_identity" for node in ast.walk(tree)), relative


@pytest.mark.parametrize("delivery,source", [
    ("song_130025_580", "seededsong_120000_349590"),
    ("song_133029_1424", "seededsong_17_420000"),
    ("semanticsong_362280_490660", "semanticsong_362280_490660"),
])
def test_shared_identity_and_every_manual_consumer(tmp_path, monkeypatch, delivery, source):
    record = _record(delivery, source)
    identity = publication_identity(record)
    assert identity.candidate_id == delivery
    assert identity.declared_ids == {delivery}
    assert identity.selector_aliases == {source}
    assert c2_upload.candidate_id_from_record(tmp_path, record) == delivery
    assert resolve_candidate_id(record, {"items": [{"candidate_id": delivery}]}) == delivery
    assert final_media._candidate_id(record, {"candidate_id": delivery}, package_root=tmp_path, manifest={}) == (delivery, [])
    path = tmp_path / "record.json"
    _write(path, record)
    manifest = {"package_attestation": {"record": _entry(path), "package_root": str(tmp_path / "out/2026-10-21" / delivery)}}
    calls = []
    monkeypatch.setattr(registry, "upload_block_reason", lambda candidate, **kw: calls.append((candidate, kw["recording_date"])) or "held")
    assert registry.manifest_upload_block_reason(manifest) == "held"
    assert calls == [(delivery, "2026-10-21")]
    assert reconciliation._candidate_and_date(manifest) == (delivery, "2026-10-21")


@pytest.mark.parametrize("drift", [
    {"candidate_id": "unrelated"},
    {"story_contract": {"candidate_id": "unrelated"}},
])
def test_conflicting_pair_is_rejected_by_all_consumers(tmp_path, drift):
    record = {**_record(), **drift}
    with pytest.raises(ValueError):
        publication_identity(record)
    with pytest.raises(ValueError):
        c2_upload.candidate_id_from_record(tmp_path, record)
    with pytest.raises(ValueError):
        resolve_candidate_id(record, {})
    assert final_media._candidate_id(record, None, package_root=tmp_path, manifest={}) == (None, ["FINAL_MEDIA_REVIEW_CANDIDATE_ID_MISMATCH"])
    path = tmp_path / "record.json"
    _write(path, record)
    manifest = {"package_attestation": {"record": _entry(path), "package_root": str(tmp_path / "out/2026-10-21/song")}}
    assert "fail-closed" in registry.manifest_upload_block_reason(manifest)
    with pytest.raises(reconciliation.PublicationReconciliationError, match="conflict"):
        reconciliation._candidate_and_date(manifest)
    with pytest.raises(ValueError):
        content_identity(manifest)


def test_legacy_projections_preserve_their_distinct_admission_rules(tmp_path):
    record = {"candidate_id": "root", "delivery_candidate_id": "delivery", "story_contract": {"candidate_id": "story"}}
    identity = publication_identity(record)
    assert identity.candidate_id == identity.registry_candidate_id == "story"
    assert identity.record_candidate_id == "root"
    assert identity.declared_ids == {"root", "delivery", "story"}
    assert c2_upload.candidate_id_from_record(tmp_path, record) == "story"
    with pytest.raises(ValueError, match="conflict"):
        resolve_candidate_id(record, {})
    assert final_media._candidate_id(record, None, package_root=tmp_path, manifest={})[1] == ["FINAL_MEDIA_REVIEW_CANDIDATE_ID_MISMATCH"]
    assert publication_identity({"candidate_id": "arbitrary"}).candidate_id == ""
    assert c2_upload.candidate_id_from_record(tmp_path, {"candidate_id": "arbitrary"}) == ""


def test_legacy_qc_keeps_string_only_admission_and_raw_precedence(tmp_path):
    with pytest.raises(ValueError, match="unavailable"):
        resolve_candidate_id({"story_contract": {"candidate_id": 123}}, {})
    record = {"story_contract": {"candidate_id": 123}, "delivery_candidate_id": "legacy-delivery"}
    assert resolve_candidate_id(record, {}) == "legacy-delivery"
    assert c2_upload.candidate_id_from_record(tmp_path, record) == "123"
    assert publication_identity({"story_contract": {"candidate_id": " spaced "}}).candidate_id == " spaced "


@pytest.mark.parametrize("delivery,source", [
    ("song_130025_580", "seededsong_120000_349590"),
    ("song_new_recording_7", "seededsong_314_15926"),
])
def test_song_producer_persistence_reaches_readiness_and_manifest_preparation(tmp_path, delivery, source):
    runtime = tmp_path / "runtime"
    package, record_path = _package(runtime, "2026-10-21", delivery)
    record = json.loads(record_path.read_text())
    record["candidate_id"] = source
    publish_path = package / f"{delivery}.publish.json"
    publish = json.loads(publish_path.read_text())
    publish.update(candidate_id=source, schema_version="shadow-publish-draft.v1", upload_enabled=False)
    _write(publish_path, publish)
    record["publish_staging"] = {"title": publish["title"], "upload_enabled": False, "publish_json_path": str(publish_path)}
    # Enter before the ordinary producer writes the id pair. No hand-added pair
    # or converted delivery record is fed to the repaired consumers.
    written, digest = _write_song_active_record(
        {"candidate_id": source, "materialized_recut": record},
        delivery_candidate_id=delivery, title=publish["title"],
        video_sha256=record["artifact_hashes"]["burned_video_sha256"],
        summary_authority_root=package,
    )
    assert written == record_path
    assert digest == "sha256:" + _entry(written)["sha256"]
    reasons, dependencies, bound_record = readiness._inspect_package(package, delivery, "2026-10-21")
    assert bound_record == written
    assert not {"PACKAGE_RECORD_IDENTITY_DRIFT", "PACKAGE_PUBLISH_IDENTITY_DRIFT"} & reasons
    assert {"record", "publish"} <= dependencies.keys()

    repo = _repository(tmp_path)
    row_runtime, row = _row(tmp_path / "prepare", candidate_id=delivery)
    row["recording_date"] = "2026-10-21"
    paths = row["package_dependencies"]
    _write(Path(paths["record"]), json.loads(written.read_text()))
    _write(Path(paths["publish"]), publish)
    calls = []
    def call(argv):
        calls.append(argv)
        _write(Path(argv[argv.index("--out") + 1]), {"manifest_version": 3})
        return 0
    result = prepare_manifest_from_readiness(row, repository_root=repo, runtime_root=row_runtime,
        authorized_upload_call=call, manifest_loader=lambda path: (json.loads(path.read_text()), []))
    assert result["status"] == "PREPARED"
    assert result["candidate_id"] == delivery
    assert len(calls) == 1
    # The same producer output still rejects an unrelated row and publish id.
    publish["candidate_id"] = "unrelated"
    _write(publish_path, publish)
    reasons, _, _ = readiness._inspect_package(package, delivery, "2026-10-21")
    assert "PACKAGE_PUBLISH_IDENTITY_DRIFT" in reasons
    _write(Path(paths["publish"]), publish)
    assert prepare_manifest_from_readiness(row, repository_root=repo, runtime_root=row_runtime)["reason_codes"] == ["MANIFEST_PREPARATION_IDENTITY_MISMATCH"]
    reasons, _, _ = readiness._inspect_package(package, "unrelated", "2026-10-21")
    assert "PACKAGE_RECORD_IDENTITY_DRIFT" in reasons


@pytest.mark.parametrize("previous_date,target_date,expected", [
    ("2026-10-21", "2026-10-21", True),
    ("2026-10-20", "2026-10-21", False),
    ("", "2026-10-21", False),
    ("2026-10-21", "", False),
    ("", "", False),
])
@pytest.mark.parametrize("legacy", [False, True])
def test_selector_aliases_require_known_equal_dates_in_both_directions(tmp_path, previous_date, target_date, expected, legacy):
    source = "seededsong_120000_349590"
    manifest = bound_manifest(tmp_path, source, delivery_candidate_id="song_new_1", source_candidate_id=source)
    target = content_identity(manifest)
    assert not identity_problems(target)
    assert target["same_day_candidate_ids"] == [source]
    target["recording_date"] = target_date
    previous = {"candidate_ids": [source], "lineage_candidate_ids": [source],
                "recording_date": previous_date, "package_locator": "", "source_windows": []}
    if not legacy:
        previous["same_day_candidate_ids"] = [source]
    row = {"video_sha256": "old", "content_identity": previous}
    assert row_matches_target(row, "new", manifest, identity=target) is expected
    assert row_matches_target({"content_identity": target}, "new", manifest, identity=previous) is expected
    # Byte/record/source-window bindings are independent, stronger evidence.
    assert row_matches_target(row, "old", manifest, identity=target)
    assert row_matches_target({**row, "record_sha256": manifest["package_attestation"]["record"]["sha256"]}, "new", manifest, identity=target)


def test_same_day_alias_metadata_is_validated_and_does_not_weaken_source_windows(tmp_path):
    source = "seededsong_10_200"
    manifest = bound_manifest(tmp_path, source, delivery_candidate_id="song_1_2", source_candidate_id=source)
    target = content_identity(manifest)
    assert identity_problems({**target, "same_day_candidate_ids": ["unbound"]})
    previous = {**target, "package_locator": "other", "recording_date": "2026-10-12", "source_windows": [{"source_sha256": "a" * 64, "start_ms": 10, "end_ms": 200}]}
    target["candidate_ids"] = ["other"]
    target["source_windows"] = [{"source_sha256": "a" * 64, "start_ms": 100, "end_ms": 300}]
    assert row_matches_target({"content_identity": previous}, "new", manifest, identity=target)


def test_historical_frozen_selector_alias_cannot_cross_recordings():
    # Enter at the persisted ledger consumer, using the old writer's shape
    # without the new metadata. This directly detects the old matcher's bug.
    source = "seededsong_120000_349590"
    previous = {"candidate_ids": [source, "song_old_1"], "lineage_candidate_ids": [source],
                "recording_date": "2026-10-20", "source_windows": [], "package_locator": "old"}
    target = {**previous, "candidate_ids": [source, "song_new_1"],
              "recording_date": "2026-10-21", "package_locator": "new"}
    assert not row_matches_target({"content_identity": previous}, "new-video", {}, identity=target)
    assert not row_matches_target({"content_identity": target}, "old-video", {}, identity=previous)
    target["recording_date"] = previous["recording_date"]
    assert row_matches_target({"content_identity": previous}, "new-video", {}, identity=target)


@pytest.mark.parametrize("bvid,delivery", [
    ("BV1PAHP6VEZN", "song_130025_580"),
    ("BV1r7HK6rEpU", "song_133029_1424"),
])
def test_published_song_receipts_keep_their_identity(tmp_path, bvid, delivery, request):
    evidence = ROOT / "reports/song-fastlane-20261024-publication-evidence/oci3/opt/bilive/autoslice"
    rows = [json.loads(line) for line in (evidence / f"reports/upload_ledger.{bvid}.rows.jsonl").read_text().splitlines()]
    published = next(row for row in rows if row.get("public_verify_status") == "VERIFIED_PUBLIC")
    assert published["bvid"] == bvid
    frozen = published["content_identity"]
    source, = set(frozen["candidate_ids"]) - {delivery}
    receipts = list((evidence / "private-runs").rglob("*.publication_reconciliation.json"))
    receipt, = [json.loads(p.read_text()) for p in receipts if json.loads(p.read_text())["bvid"] == bvid]
    assert receipt["candidate_id"] == delivery
    manifest, = [json.loads(p.read_text()) for p in (evidence / "private-runs").rglob("*.upload_manifest.json") if json.loads(p.read_text())["title"] == receipt["title"]]
    # These saved receipts contain the frozen aliases and publication id, but
    # not the active-record bytes. Replay only those receipt-derived identity
    # fields; CI has no permission to open the private production directory.
    assert manifest["package_attestation"]["record"]["sha256"] == published["record_sha256"]
    record = _record(delivery, source)
    request.node.user_properties.append(("source_record", "identity fields projected from saved receipts"))
    assert publication_identity(record).candidate_id == delivery
    assert c2_upload.candidate_id_from_record(tmp_path, record) == delivery
    assert resolve_candidate_id(record, {}) == delivery
    assert final_media._candidate_id(record, {"candidate_id": delivery}, package_root=tmp_path, manifest={}) == (delivery, [])
    saved_registry = [json.loads(line) for line in (evidence / f"state/publication_registry.{bvid}.rows.json").read_text().splitlines()]
    assert all(row["candidate_id"] == delivery for group in saved_registry for row in group["rows"])


def test_existing_talk_publication_receipts_keep_their_identity(tmp_path):
    evidence = ROOT / "reports/p-dispatch-20261015-publication-evidence/oci3/opt/bilive/autoslice/private-runs"
    receipts = [json.loads(p.read_text()) for p in evidence.rglob("*.publication_reconciliation.json")]
    talks = [row for row in receipts if not row["candidate_id"].startswith("semanticsong_")]
    assert len(talks) >= 1
    for receipt in talks:
        candidate = receipt["candidate_id"]
        record = {"candidate_id": candidate, "story_contract": {"candidate_id": candidate}}
        assert publication_identity(record).candidate_id == candidate
        assert c2_upload.candidate_id_from_record(tmp_path, record) == candidate
        assert resolve_candidate_id(record, {}) == candidate
        assert final_media._candidate_id(record, {"candidate_id": candidate}, package_root=tmp_path, manifest={}) == (candidate, [])
