import hashlib
import json
from pathlib import Path

import pytest

import scripts.resume_frozen_talk_package as frozen_resume
from scripts.resume_frozen_talk_package import (
    FrozenTalkResumeError,
    TRANSACTION_SCHEMA,
    _assert_upload_ledger_unchanged,
    _clone_approved_cover_generation,
    _commit_transaction,
    _recursive_path_rewrite,
    _validated_input,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_validated_input_archives_before_mutable_source_is_replaced(tmp_path):
    source = tmp_path / "delivery.srt"
    archive = tmp_path / "frozen" / "source.srt"
    source.write_bytes(b"reviewed source\n")
    plan = {
        "inputs": {
            "frozen_text_srt": {
                "path": str(source),
                "archive_path": str(archive),
                "sha256": _sha(source.read_bytes()),
            }
        }
    }

    first = _validated_input(
        plan,
        "frozen_text_srt",
        allowed_roots=(tmp_path.resolve(),),
    )
    assert first == archive.resolve()
    source.write_bytes(b"new delivered bytes\n")
    second = _validated_input(
        plan,
        "frozen_text_srt",
        allowed_roots=(tmp_path.resolve(),),
    )
    assert second == first
    assert second.read_bytes() == b"reviewed source\n"


def test_no_upload_ledger_gate_runs_before_finalization(tmp_path):
    ledger = tmp_path / "upload_ledger.jsonl"
    ledger.write_bytes(b"unchanged\n")
    plan = {
        "inputs": {
            "upload_ledger": {"sha256": _sha(ledger.read_bytes())}
        }
    }
    _assert_upload_ledger_unchanged(plan, ledger)
    ledger.write_bytes(b"concurrent publish\n")
    with pytest.raises(FrozenTalkResumeError, match="upload ledger changed"):
        _assert_upload_ledger_unchanged(plan, ledger)


def test_resume_transaction_rolls_forward_and_allows_only_declared_mutation(tmp_path):
    generation = tmp_path / "generation"
    generation.mkdir()
    immutable_blob = generation / "immutable.blob"
    mutable_blob = generation / "mutable.blob"
    immutable_blob.write_bytes(b"speaker srt")
    mutable_blob.write_bytes(b"record before cover binding")
    immutable_target = tmp_path / "delivery.speaker.srt"
    mutable_target = tmp_path / "delivery.record.json"
    transaction = generation / "resume-transaction.json"
    entries = [
        {
            "label": "speaker",
            "target": str(immutable_target),
            "intended_blob": str(immutable_blob),
            "intended_sha256": _sha(immutable_blob.read_bytes()),
            "mutable_after_commit": False,
        },
        {
            "label": "record",
            "target": str(mutable_target),
            "intended_blob": str(mutable_blob),
            "intended_sha256": _sha(mutable_blob.read_bytes()),
            "mutable_after_commit": True,
        },
    ]
    result = _commit_transaction(
        transaction_path=transaction,
        plan_sha256="a" * 64,
        recovery_code_fingerprint="sha256:" + "1" * 64,
        candidate_id="auto_test",
        date="2026-07-10",
        entries=entries,
        allowed_target_roots=(tmp_path,),
    )
    assert result["status"] == "COMMITTED"
    assert immutable_target.read_bytes() == b"speaker srt"
    assert mutable_target.read_bytes() == b"record before cover binding"

    mutable_target.write_bytes(b"record after cover binding")
    resumed = _commit_transaction(
        transaction_path=transaction,
        plan_sha256="a" * 64,
        recovery_code_fingerprint="sha256:" + "1" * 64,
        candidate_id="auto_test",
        date="2026-07-10",
        entries=[],
        allowed_target_roots=(tmp_path,),
    )
    assert resumed["status"] == "COMMITTED"
    assert mutable_target.read_bytes() == b"record after cover binding"

    with pytest.raises(FrozenTalkResumeError, match="authority drifted"):
        _commit_transaction(
            transaction_path=transaction,
            plan_sha256="a" * 64,
            recovery_code_fingerprint="sha256:" + "9" * 64,
            candidate_id="auto_test",
            date="2026-07-10",
            entries=[],
            allowed_target_roots=(tmp_path,),
        )

    immutable_target.write_bytes(b"tampered")
    with pytest.raises(FrozenTalkResumeError, match="immutable target drifted"):
        _commit_transaction(
            transaction_path=transaction,
            plan_sha256="a" * 64,
            recovery_code_fingerprint="sha256:" + "1" * 64,
            candidate_id="auto_test",
            date="2026-07-10",
            entries=[],
            allowed_target_roots=(tmp_path,),
        )


def test_resume_transaction_recovers_prepared_journal(tmp_path):
    generation = tmp_path / "generation"
    generation.mkdir()
    blob = generation / "intended.bin"
    target = tmp_path / "delivery.bin"
    blob.write_bytes(b"ready")
    transaction = generation / "resume-transaction.json"
    journal = {
        "schema_version": TRANSACTION_SCHEMA,
        "status": "PREPARED",
        "plan_sha256": "b" * 64,
        "recovery_code_fingerprint": "sha256:" + "2" * 64,
        "candidate_id": "auto_test",
        "date": "2026-07-10",
        "upload_enabled": False,
        "entries": [
            {
                "label": "delivery",
                "target": str(target),
                "intended_blob": str(blob),
                "intended_sha256": _sha(blob.read_bytes()),
                "mutable_after_commit": False,
            }
        ],
    }
    transaction.write_text(json.dumps(journal), encoding="utf-8")
    result = _commit_transaction(
        transaction_path=transaction,
        plan_sha256="b" * 64,
        recovery_code_fingerprint="sha256:" + "2" * 64,
        candidate_id="auto_test",
        date="2026-07-10",
        entries=[],
        allowed_target_roots=(tmp_path,),
    )
    assert result["status"] == "COMMITTED"
    assert target.read_bytes() == b"ready"


def test_recursive_path_rewrite_changes_only_exact_path_values():
    original = "/tmp/generation/file.srt"
    active = "/opt/bilive/autoslice/out/file.srt"
    payload = {
        "path": original,
        "command": ["tool", original, f"prefix:{original}"],
        "nested": {"path": original},
    }
    rewritten = _recursive_path_rewrite(payload, {original: active})
    assert rewritten["path"] == active
    assert rewritten["command"] == ["tool", active, f"prefix:{original}"]
    assert rewritten["nested"]["path"] == active


def test_cover_clone_recovers_each_one_sided_atomic_write(tmp_path, monkeypatch):
    source_cover = tmp_path / "source" / "final.cover.png"
    source_cover.parent.mkdir()
    source_cover.write_bytes(b"approved image bytes")
    source_manifest = source_cover.with_suffix(".cover_generation.json")
    source_manifest.write_text('{"source":true}\n', encoding="utf-8")
    source_generation = {
        "candidate_id": "auto_test",
        "title": "manual title",
        "model": "gpt-image-2",
        "final_cover": str(source_cover),
        "final_cover_sha256": "sha256:" + _sha(source_cover.read_bytes()),
    }
    target_cover = tmp_path / "target" / "final.cover.png"

    def fake_validate(*, cover, title, candidate_id):
        assert title == "manual title"
        assert candidate_id == "auto_test"
        if cover == source_cover:
            return source_generation, source_manifest
        assert cover == target_cover
        assert target_cover.is_file()
        manifest = target_cover.with_suffix(".cover_generation.json")
        assert manifest.is_file()
        return json.loads(manifest.read_text(encoding="utf-8")), manifest

    monkeypatch.setattr(
        frozen_resume, "_validate_repaired_cover_generation", fake_validate
    )

    # Crash shape 1: PNG committed, manifest absent.
    target_cover.parent.mkdir()
    target_cover.write_bytes(source_cover.read_bytes())
    _clone_approved_cover_generation(
        source_cover=source_cover,
        target_cover=target_cover,
        title="manual title",
        candidate_id="auto_test",
    )
    target_manifest = target_cover.with_suffix(".cover_generation.json")
    intended_manifest = target_manifest.read_bytes()

    # Crash shape 2: manifest committed, PNG absent.
    target_cover.unlink()
    assert target_manifest.read_bytes() == intended_manifest
    _clone_approved_cover_generation(
        source_cover=source_cover,
        target_cover=target_cover,
        title="manual title",
        candidate_id="auto_test",
    )
    assert target_cover.read_bytes() == source_cover.read_bytes()
    assert target_manifest.read_bytes() == intended_manifest
