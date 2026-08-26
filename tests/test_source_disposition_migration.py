from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from ops.recording.source_disposition_migration import (
    MIGRATION_SCHEMA_VERSION,
    REASON,
    MigrationError,
    canonical_sha256,
    migrate,
    snapshot_diff,
    transform_state,
    validate_receipt,
)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _fp(path: Path) -> dict[str, int | str]:
    info = path.lstat()
    return {
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _row() -> dict:
    row = {
        "schema_version": "recording-connection-stub.v1",
        "status": "IGNORED_CONNECTION_STUB",
        "reason_code": "RECORDER_CONNECTION_STUB_NO_DECODABLE_VIDEO",
        "source_relative_path": "2026-08-18/22966160_20260818-19-00-28.flv",
        "source": {"path": "old-source", "sha256": "0" * 64},
        "xml": {"path": "old-xml", "sha256": "1" * 64},
        "webhook": {"opening_event_id": "open-old", "closing_event_id": "close-old"},
        "decode": {"video_codec": "h264", "decoded_video_frames": 0},
        "session": {
            "session_id": "session-old",
            "successor_webhook": {
                "opening_event_id": "open-successor",
                "closing_event_id": "close-successor",
            },
            "successor_finalized_ledger": {
                "target": "/old/2026-08-18/old.mp4",
                "target_sha256": "2" * 64,
                "source_size": 1150361870,
                "finalized_at": "2026-08-18T19:00:35Z",
            },
        },
        "source_action": {"finalized": False},
    }
    row["canonical_integrity"] = {
        "algorithm": "sha256",
        "canonical_json_sha256": canonical_sha256(
            {key: value for key, value in row.items() if key != "canonical_integrity"}
        ),
    }
    return row


def _fixture(tmp_path: Path) -> tuple[dict, Path, Path]:
    record_root = tmp_path / "recording"
    replacement_paths = {
        "source": "2026-08-18/22966160_20260818-19-00-30.flv",
        "xml": "2026-08-18/22966160_20260818-19-00-30.xml",
        "successor_source": "2026-08-18/22966160_20260818-19-00-32.flv",
        "successor_mp4": "2026-08-18/22966160_20260818-19-00-32.mp4",
    }
    contents = {
        "source": b"replacement-source-0e1ca21d",
        "xml": b"<record_info roomid='22966160'/>",
        "successor_source": b"replacement-successor-3fd58f",
        "successor_mp4": b"replacement-mp4-3fd58f",
    }
    replacement = {}
    for role, relative in replacement_paths.items():
        path = record_root / relative
        _write(path, contents[role])
        fp = _fp(path)
        replacement[role] = {"relative_path": relative, **fp}
        if role == "successor_mp4":
            replacement[role]["media_probe"] = {
                "size_bytes": fp["size_bytes"],
                "duration_seconds": 42.5,
                "video_codec": "h264",
                "audio_codec": "aac",
                "width": 1920,
                "height": 1080,
            }

    row = _row()
    state = {
        "schema_version": "bililive-recorder-adapter-state.v1",
        "managed_since_epoch": 1.0,
        "finalized": {f"2026-08-18/final-{i}.flv": {"target": f"final-{i}.mp4"} for i in range(6)},
        "source_dispositions": {row["source_relative_path"]: row},
        "webhook_event_ids": {
            event_id: {"event_type": "fixture"}
            for event_id in ("open-old", "close-old", "open-successor", "close-successor")
        },
    }
    state_path = tmp_path / "old-state.json"
    state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    old_target = deepcopy(row["session"]["successor_finalized_ledger"])
    new_target = {
        "target": replacement["successor_mp4"]["relative_path"],
        "target_sha256": replacement["successor_mp4"]["sha256"],
        "size_bytes": replacement["successor_mp4"]["size_bytes"],
        "media_probe": replacement["successor_mp4"]["media_probe"],
    }
    request = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "tool_commit": "981bc4ab",
        "reason": REASON,
        "operator_authority": {"operator": "fixture-operator", "ticket": "OCI3-1"},
        "old_state_snapshot": {"path": str(state_path), "sha256": hashlib.sha256(state_path.read_bytes()).hexdigest()},
        "old_disposition_relative": row["source_relative_path"],
        "old_row_canonical_sha256": row["canonical_integrity"]["canonical_json_sha256"],
        "old_webhook_event_ids": ["open-old", "close-old", "open-successor", "close-successor"],
        "replacement": replacement,
        "old_finalized_target_evidence": old_target,
        "new_finalized_target_evidence": new_target,
    }
    return request, record_root, state_path


def test_observed_positive_dry_run_is_source_bound_and_no_write(tmp_path):
    request, record_root, state_path = _fixture(tmp_path)
    before = state_path.read_bytes()
    receipt_path = tmp_path / "receipt.json"
    receipt = migrate(request, record_root=record_root, receipt_path=receipt_path, write=False)
    assert not receipt_path.exists()
    assert receipt["identity_proven"] is False
    assert receipt["source_substitution"] is True
    assert receipt["ordinary_rebind"] is False
    assert receipt["publication_authority"] is None
    assert receipt["old_state_snapshot"]["raw_sha256"] == request["old_state_snapshot"]["sha256"]
    assert state_path.read_bytes() == before
    validate_receipt(receipt)


def test_write_is_create_only_and_identical_rerun_is_idempotent(tmp_path):
    request, record_root, _ = _fixture(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    first = migrate(request, record_root=record_root, receipt_path=receipt_path, write=True)
    bytes_after_first = receipt_path.read_bytes()
    second = migrate(request, record_root=record_root, receipt_path=receipt_path, write=True)
    assert receipt_path.read_bytes() == bytes_after_first
    assert second["mapping"] == first["mapping"]


def test_different_mapping_rejected_without_overwrite(tmp_path):
    request, record_root, _ = _fixture(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    migrate(request, record_root=record_root, receipt_path=receipt_path, write=True)
    changed = deepcopy(request)
    changed["replacement"]["source"]["relative_path"] = "2026-08-18/other.flv"
    with pytest.raises(MigrationError, match="different mapping|missing"):
        migrate(changed, record_root=record_root, receipt_path=receipt_path, write=True)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: r.update(reason="IDENTITY_REBIND"), "reason"),
        (lambda r: r["old_state_snapshot"].update(sha256="f" * 64), "state snapshot SHA"),
        (lambda r: r.update(old_row_canonical_sha256="f" * 64), "row canonical SHA"),
        (lambda r: r.update(old_webhook_event_ids=["wrong"]), "webhook event IDs"),
        (lambda r: r["replacement"]["successor_mp4"].update(sha256="f" * 64), "SHA-256"),
        (lambda r: r["new_finalized_target_evidence"].update(target_sha256="f" * 64), "target SHA"),
    ],
)
def test_contract_and_hash_mismatch_cases(tmp_path, change, message):
    request, record_root, _ = _fixture(tmp_path)
    changed = deepcopy(request)
    change(changed)
    with pytest.raises(MigrationError, match=message):
        migrate(changed, record_root=record_root, receipt_path=None, write=False)


def test_old_state_drift_is_rejected(tmp_path):
    request, record_root, state_path = _fixture(tmp_path)
    state_path.write_text(state_path.read_text(encoding="utf-8").replace('"managed_since_epoch": 1.0', '"managed_since_epoch": 2.0'), encoding="utf-8")
    with pytest.raises(MigrationError, match="state snapshot SHA-256 drifted"):
        migrate(request, record_root=record_root, receipt_path=None, write=False)


def test_glob_duplicate_and_ambiguous_paths_rejected(tmp_path):
    request, record_root, _ = _fixture(tmp_path)
    globbed = deepcopy(request)
    globbed["replacement"]["xml"]["relative_path"] = "2026-08-18/*.xml"
    with pytest.raises(MigrationError, match="glob"):
        migrate(globbed, record_root=record_root, receipt_path=None, write=False)
    duplicate = deepcopy(request)
    duplicate["replacement"]["xml"] = deepcopy(duplicate["replacement"]["source"])
    with pytest.raises(MigrationError, match="duplicate"):
        migrate(duplicate, record_root=record_root, receipt_path=None, write=False)


def test_symlink_and_hardlink_are_rejected(tmp_path):
    request, record_root, _ = _fixture(tmp_path)
    source = record_root / request["replacement"]["source"]["relative_path"]
    symlink = record_root / "2026-08-18/symlink.flv"
    symlink.symlink_to(source)
    changed = deepcopy(request)
    changed["replacement"]["source"]["relative_path"] = "2026-08-18/symlink.flv"
    with pytest.raises(MigrationError, match="symlink"):
        migrate(changed, record_root=record_root, receipt_path=None, write=False)
    hardlink = record_root / "2026-08-18/hardlink.flv"
    os.link(source, hardlink)
    changed = deepcopy(request)
    changed["replacement"]["source"]["relative_path"] = "2026-08-18/hardlink.flv"
    with pytest.raises(MigrationError, match="hardlink|fingerprint"):
        migrate(changed, record_root=record_root, receipt_path=None, write=False)


def test_no_input_mutation_and_no_publication_effects(tmp_path):
    request, record_root, state_path = _fixture(tmp_path)
    original = json.dumps(request, sort_keys=True)
    original_state = state_path.read_bytes()
    receipt = migrate(request, record_root=record_root, receipt_path=None, write=False)
    assert json.dumps(request, sort_keys=True) == original
    assert state_path.read_bytes() == original_state
    assert receipt["publication_effects"] == []
    assert receipt["publication_authority"] is None


def test_formal_safe_transform_preserves_finalized_and_original_disposition(tmp_path):
    request, record_root, state_path = _fixture(tmp_path)
    original_state_bytes = state_path.read_bytes()
    receipt_path = tmp_path / "receipt.json"
    migrate(request, record_root=record_root, receipt_path=receipt_path, write=True)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    output = tmp_path / "transformed-state.json"
    transformed = transform_state(receipt, output_path=output, write=True)
    original = json.loads(state_path.read_text(encoding="utf-8"))
    assert transformed["finalized"] == original["finalized"]
    assert transformed["source_dispositions"] == original["source_dispositions"]
    assert transformed["source_dispositions"][request["old_disposition_relative"]] == receipt["old_disposition"]["row_snapshot"]
    assert transformed["source_disposition_migrations"][0]["ordinary_rebind"] is False
    assert transformed["source_disposition_migrations"][0]["adapter_consumption"] == "NOT_CONSUMED_BY_ADAPTER"
    assert state_path.read_bytes() == original_state_bytes
    assert snapshot_diff(state_path, output)["top_level_added"] == ["source_disposition_migrations"]


def test_secure_json_and_output_paths_reject_symlink_traversal(tmp_path):
    request, record_root, state_path = _fixture(tmp_path)
    linked_state = tmp_path / "state-link.json"
    linked_state.symlink_to(state_path)
    changed = deepcopy(request)
    changed["old_state_snapshot"]["path"] = str(linked_state)
    with pytest.raises(MigrationError, match="unsafe|missing"):
        migrate(changed, record_root=record_root, receipt_path=None, write=False)

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "receipt-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(MigrationError, match="unsafe|missing"):
        migrate(request, record_root=record_root, receipt_path=linked_parent / "receipt.json", write=True)
    assert not (outside / "receipt.json").exists()

    target = tmp_path / "receipt-target.json"
    target.write_text("{}", encoding="utf-8")
    linked_receipt = tmp_path / "receipt-link.json"
    linked_receipt.symlink_to(target)
    with pytest.raises(MigrationError, match="unsafe|missing"):
        migrate(request, record_root=record_root, receipt_path=linked_receipt, write=True)
    assert target.read_text(encoding="utf-8") == "{}"


def test_transform_is_dry_run_by_default(tmp_path):
    request, record_root, _ = _fixture(tmp_path)
    receipt = migrate(request, record_root=record_root, receipt_path=None, write=False)
    output = tmp_path / "new-state.json"
    transform_state(receipt, output_path=output, write=False)
    assert not output.exists()
