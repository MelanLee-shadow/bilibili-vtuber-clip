import hashlib
import json
from pathlib import Path

import pytest

import scripts.rollback_reviewed_cover_invalidation as rollback
from scripts.repair_reviewed_covers import _invalidate_document
from src.autoslice.cover_maintenance import _json_file_bytes


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _pretty(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _fixture(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    base = tmp_path / "base"
    date = "2026-08-08"
    candidate_id = "auto_test"
    bvid = "BV1test"
    cover_sha = "c" * 64
    package = base / "out" / date / candidate_id / "replacement_recuts"
    delivery = root / "lidousha" / date
    plans = root / "assets" / "lidousha" / "cover_repair_plans"
    state_root = base / "state"
    plans.mkdir(parents=True)
    package.mkdir(parents=True)
    delivery.mkdir(parents=True)
    state_root.mkdir(parents=True)
    (base / "DISABLED").touch()

    monkeypatch.setattr(rollback, "ROOT", root)
    monkeypatch.setattr(rollback, "BASE", base)

    publish_document = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": candidate_id,
        "title": "【李豆沙】原题",
        "cover_text": "第一行\n第二行",
        "cover_status": "AI_COVER_READY",
        "cover_path": "/delivery/cover.png",
        "cover_generation": {"cover_text": "第一行\n第二行"},
        "artifact_hashes": {"cover_sha256": "sha256:" + cover_sha},
        "upload_enabled": False,
        "reason_codes": [],
    }
    publish_raw = _json_file_bytes(publish_document)
    publish_sha = _sha(publish_raw)
    record_document = {
        "story_contract": {"candidate_id": candidate_id},
        "artifact_hashes": {
            "cover_sha256": "sha256:" + cover_sha,
            "publish_draft_sha256": "sha256:" + publish_sha,
        },
        "publish_staging": publish_document,
    }
    record_raw = _json_file_bytes(record_document)

    record_preimage = package / f"{candidate_id}.burned.record.json"
    publish_preimage = package / f"{candidate_id}.publish.backup.json"
    _write(record_preimage, record_raw)
    _write(publish_preimage, publish_raw)

    record_targets = [
        delivery / "clip.record.json",
        package / f"{candidate_id}.record.json",
    ]
    publish_target = package / f"{candidate_id}.publish.json"
    invalidated_record_raw = _json_file_bytes(
        _invalidate_document(record_document, title="【李豆沙】原题", cover_text="第一行\n第二行")
    )
    invalidated_publish_raw = _json_file_bytes(
        _invalidate_document(publish_document, title="【李豆沙】原题", cover_text="第一行\n第二行")
    )
    for target in record_targets:
        _write(target, invalidated_record_raw)
    _write(publish_target, invalidated_publish_raw)

    original_publish_sha = "d" * 64
    upload_ledger_path = base / "reports" / "upload_ledger.jsonl"
    upload_ledger_raw = b'{"event":"historical"}\n'
    _write(upload_ledger_path, upload_ledger_raw)
    original_plan = {
        "schema_version": "reviewed-cover-repair-plan.v1",
        "date": date,
        "upload_enabled": False,
        "upload_ledger": {
            "path": str(upload_ledger_path),
            "sha256": _sha(upload_ledger_raw),
        },
        "invalidations": [
            {
                "candidate_id": candidate_id,
                "title": "【李豆沙】原题",
                "expected_cover_text": "第一行\n第二行",
                "documents": [
                    {"path": str(record_targets[0]), "sha256": _sha(record_raw)},
                    {"path": str(record_targets[1]), "sha256": _sha(record_raw)},
                    {"path": str(publish_target), "sha256": original_publish_sha},
                ],
            }
        ],
    }
    original_plan_path = plans / "original.json"
    original_plan_raw = _pretty(original_plan)
    _write(original_plan_path, original_plan_raw)

    transaction_root = base / "out" / date / "reviewed_cover_repairs" / _sha(original_plan_raw)[:16]
    transaction_path = transaction_root / "invalidation-transaction.json"
    transaction = {
        "schema_version": rollback.INVALIDATION_SCHEMA,
        "status": "COMMITTED",
        "date": date,
        "plan_sha256": _sha(original_plan_raw),
        "upload_enabled": False,
        "entries": [
            {
                "candidate_id": candidate_id,
                "target": str(record_targets[0]),
                "intended_sha256": _sha(invalidated_record_raw),
            },
            {
                "candidate_id": candidate_id,
                "target": str(record_targets[1]),
                "intended_sha256": _sha(invalidated_record_raw),
            },
            {
                "candidate_id": candidate_id,
                "target": str(publish_target),
                "intended_sha256": _sha(invalidated_publish_raw),
            },
        ],
    }
    transaction_raw = _pretty(transaction)
    _write(transaction_path, transaction_raw)

    preimage_record = {
        "candidate_id": candidate_id,
        "status": "published",
        "cover_status": "AI_COVER_READY",
        "cover_sha256": "sha256:" + cover_sha,
        "cover_generation": {"cover_text": "第一行\n第二行"},
    }
    current_record = {
        "candidate_id": candidate_id,
        "status": "published",
        "cover_status": "BLOCKED_AI_COVER_REQUIRED",
        "reviewed_cover_repair": {"status": "INVALIDATED"},
    }
    neighbor = {"candidate_id": "auto_neighbor", "marker": "preserve me"}
    current_state = {"date": date, "picks": [current_record, neighbor], "songs": []}
    backup_state = {"date": date, "picks": [preimage_record], "songs": []}
    state_path = state_root / f"{date}.json"
    backup_path = state_root / f"{date}.json.bak"
    current_state_raw = _pretty(current_state)
    backup_state_raw = _pretty(backup_state)
    _write(state_path, current_state_raw)
    _write(backup_path, backup_state_raw)

    registry_path = root / "assets" / "lidousha" / "publication_registry.v1.json"
    registry = {
        "entries": [
            {
                "candidate_id": candidate_id,
                "recording_date": date,
                "status": "published",
                "bvid": bvid,
            }
        ]
    }
    registry_raw = _pretty(registry)
    _write(registry_path, registry_raw)

    plan = {
        "schema_version": rollback.PLAN_SCHEMA,
        "purpose": rollback.PURPOSE,
        "date": date,
        "candidate_id": candidate_id,
        "upload_enabled": False,
        "public_edit_enabled": False,
        "publication_registry": {
            "path": str(registry_path),
            "sha256": _sha(registry_raw),
            "bvid": bvid,
        },
        "invalidation_plan": {
            "path": str(original_plan_path),
            "sha256": _sha(original_plan_raw),
        },
        "invalidation_transaction": {
            "path": str(transaction_path),
            "sha256": _sha(transaction_raw),
        },
        "state": {
            "path": str(state_path),
            "expected_current_file_sha256": _sha(current_state_raw),
            "expected_current_record_sha256": rollback._canonical_record_sha256(current_record),
            "preimage_path": str(backup_path),
            "preimage_file_sha256": _sha(backup_state_raw),
            "preimage_record_sha256": rollback._canonical_record_sha256(preimage_record),
        },
        "documents": [
            {
                "kind": "record",
                "target": str(record_targets[0]),
                "expected_invalidated_sha256": _sha(invalidated_record_raw),
                "original_expected_sha256": _sha(record_raw),
                "preimage_path": str(record_preimage),
                "recovery_preimage_sha256": _sha(record_raw),
                "preimage_kind": "EXACT_ORIGINAL_PREIMAGE",
            },
            {
                "kind": "record",
                "target": str(record_targets[1]),
                "expected_invalidated_sha256": _sha(invalidated_record_raw),
                "original_expected_sha256": _sha(record_raw),
                "preimage_path": str(record_preimage),
                "recovery_preimage_sha256": _sha(record_raw),
                "preimage_kind": "EXACT_ORIGINAL_PREIMAGE",
            },
            {
                "kind": "publish",
                "target": str(publish_target),
                "expected_invalidated_sha256": _sha(invalidated_publish_raw),
                "original_expected_sha256": original_publish_sha,
                "preimage_path": str(publish_preimage),
                "recovery_preimage_sha256": publish_sha,
                "preimage_kind": "SANCTIONED_ACTIVE_SIBLING",
                "original_preimage_unavailable_reason": "original raw bytes unavailable; sibling reproduces invalidation exactly",
            },
        ],
    }
    plan_path = plans / "rollback.json"
    _write(plan_path, _pretty(plan))
    return {
        "plan": plan,
        "plan_path": plan_path,
        "state_path": state_path,
        "record_targets": record_targets,
        "publish_target": publish_target,
        "record_raw": record_raw,
        "publish_raw": publish_raw,
        "invalidated_record_raw": invalidated_record_raw,
        "invalidated_publish_raw": invalidated_publish_raw,
        "transaction_root": transaction_root,
        "transaction_path": transaction_path,
        "transaction_raw": transaction_raw,
        "candidate_id": candidate_id,
        "neighbor": neighbor,
        "registry_path": registry_path,
        "backup_path": backup_path,
        "backup_state_raw": backup_state_raw,
    }


def test_validate_only_is_non_mutating_and_apply_restores_single_candidate(
    tmp_path, monkeypatch
):
    fixture = _fixture(tmp_path, monkeypatch)

    checked = rollback.run(fixture["plan_path"], apply=False)

    assert checked["status"] == "VALIDATED_ONLY"
    assert fixture["record_targets"][0].read_bytes() == fixture["invalidated_record_raw"]
    assert fixture["publish_target"].read_bytes() == fixture["invalidated_publish_raw"]
    assert not list(fixture["transaction_root"].glob("rollback-*"))

    committed = rollback.run(fixture["plan_path"], apply=True)

    assert committed["status"] == "ROLLED_BACK_PUBLISHED_CANDIDATE_PREFLIGHT_GAP"
    assert all(path.read_bytes() == fixture["record_raw"] for path in fixture["record_targets"])
    assert fixture["publish_target"].read_bytes() == fixture["publish_raw"]
    state = json.loads(fixture["state_path"].read_text())
    restored = next(row for row in state["picks"] if row["candidate_id"] == fixture["candidate_id"])
    neighbor = next(row for row in state["picks"] if row["candidate_id"] == "auto_neighbor")
    assert restored["cover_status"] == "AI_COVER_READY"
    assert restored["cover_generation"] == {"cover_text": "第一行\n第二行"}
    assert neighbor == fixture["neighbor"]
    assert fixture["backup_path"].read_bytes() == fixture["backup_state_raw"]
    assert fixture["transaction_path"].read_bytes() == fixture["transaction_raw"]

    repeated = rollback.run(fixture["plan_path"], apply=True)
    assert repeated["status"] == "ROLLED_BACK_PUBLISHED_CANDIDATE_PREFLIGHT_GAP"
    assert repeated["rollback_plan_sha256"] == committed["rollback_plan_sha256"]


def test_prepared_rollback_resumes_after_a_document_commit_crash(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, monkeypatch)
    atomic_write = rollback._atomic_write
    target_writes = 0

    def fail_during_second_target(path, payload):
        nonlocal target_writes
        if path in {*fixture["record_targets"], fixture["publish_target"], fixture["state_path"]}:
            target_writes += 1
            if target_writes == 2:
                raise RuntimeError("injected crash")
        atomic_write(path, payload)

    monkeypatch.setattr(rollback, "_atomic_write", fail_during_second_target)
    with pytest.raises(RuntimeError, match="injected crash"):
        rollback.run(fixture["plan_path"], apply=True)

    journal = next(fixture["transaction_root"].glob("rollback-*/rollback-transaction.json"))
    assert json.loads(journal.read_text())["status"] == "PREPARED"
    assert fixture["record_targets"][0].read_bytes() == fixture["record_raw"]
    assert fixture["record_targets"][1].read_bytes() == fixture["invalidated_record_raw"]

    monkeypatch.setattr(rollback, "_atomic_write", atomic_write)
    recovered = rollback.run(fixture["plan_path"], apply=True)

    assert recovered["status"] == "ROLLED_BACK_PUBLISHED_CANDIDATE_PREFLIGHT_GAP"
    assert fixture["transaction_path"].read_bytes() == fixture["transaction_raw"]
    assert fixture["backup_path"].read_bytes() == fixture["backup_state_raw"]


def test_target_drift_fails_before_recovery_namespace_or_state_write(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, monkeypatch)
    before_state = fixture["state_path"].read_bytes()
    fixture["record_targets"][0].write_bytes(b"unexpected drift")

    with pytest.raises(rollback.CoverInvalidationRollbackError, match="target drifted"):
        rollback.run(fixture["plan_path"], apply=True)

    assert fixture["state_path"].read_bytes() == before_state
    assert not list(fixture["transaction_root"].glob("rollback-*"))


def test_registry_must_still_identify_the_exact_published_bvid(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, monkeypatch)
    registry = json.loads(fixture["registry_path"].read_text())
    registry["entries"][0]["status"] = "released_for_upload"
    registry_raw = _pretty(registry)
    fixture["registry_path"].write_bytes(registry_raw)
    plan = fixture["plan"]
    plan["publication_registry"]["sha256"] = _sha(registry_raw)
    fixture["plan_path"].write_bytes(_pretty(plan))

    with pytest.raises(
        rollback.CoverInvalidationRollbackError,
        match="published registry authority drifted",
    ):
        rollback.run(fixture["plan_path"], apply=True)

    assert not list(fixture["transaction_root"].glob("rollback-*"))
