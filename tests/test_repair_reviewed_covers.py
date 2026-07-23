import copy
import hashlib
from pathlib import Path

import pytest

import scripts.repair_reviewed_covers as reviewed


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_invalidate_document_preserves_media_authority_and_resolves_manual_title():
    source = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": "auto_test",
        "title": "【李豆沙】错误标题",
        "title_source": "llm+lidousha_style_asset",
        "cover_status": "AI_COVER_READY",
        "cover_path": "/delivery/old.cover.png",
        "cover_generation": {"title": "【李豆沙】错误标题"},
        "cover_repair_binding": {"path": "/old.binding.json"},
        "artifact_hashes": {
            "burned_video_sha256": "sha256:" + "1" * 64,
            "subtitle_sha256": "sha256:" + "2" * 64,
            "cover_sha256": "sha256:" + "3" * 64,
        },
        "upload_enabled": False,
        "reason_codes": [],
    }
    original = copy.deepcopy(source)

    updated = reviewed._invalidate_document(
        source,
        title="【李豆沙】去彩排前连问三遍：你们还要来找我玩，好不好？",
    )

    assert source == original
    assert updated["title"] == "【李豆沙】去彩排前连问三遍：你们还要来找我玩，好不好？"
    assert updated["title_source"] == "job_title"
    assert updated["title_authority_status"] == "RESOLVED_MANUAL"
    assert updated["cover_text"] == "去彩排前连问三遍\n你们还要来找我玩，好不好？"
    assert updated["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert updated["upload_enabled"] is False
    assert "cover_path" not in updated
    assert "cover_generation" not in updated
    assert "cover_repair_binding" not in updated
    assert "cover_sha256" not in updated["artifact_hashes"]
    assert updated["artifact_hashes"]["burned_video_sha256"] == original["artifact_hashes"]["burned_video_sha256"]
    assert updated["artifact_hashes"]["subtitle_sha256"] == original["artifact_hashes"]["subtitle_sha256"]


def test_invalidate_document_accepts_hash_bound_short_cover_copy():
    source = {
        "schema_version": "shadow-publish-draft.v1",
        "artifact_hashes": {"burned_video_sha256": "sha256:" + "1" * 64},
        "reason_codes": [],
    }

    updated = reviewed._invalidate_document(
        source,
        title="【李豆沙】完整归档标题保留上下文",
        cover_text="短梗字\n保留问号？",
    )

    assert updated["title"] == "【李豆沙】完整归档标题保留上下文"
    assert updated["cover_text"] == "短梗字\n保留问号？"


def test_invalidate_state_record_persists_reviewed_cover_diversity_slot():
    record = {
        "candidate_id": "auto_test",
        "title": "【李豆沙】旧标题",
        "cover_status": "AI_COVER_READY",
    }

    reviewed._invalidate_state_record(
        record,
        row={
            "title": "【李豆沙】新标题",
            "cover_diversity_slot": 4,
        },
        plan_sha256="a" * 64,
    )

    assert record["title"] == "【李豆沙】新标题"
    assert record["cover_diversity_slot"] == 4
    assert record["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"


def _transaction_fixture(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    base = tmp_path / "base"
    date = "2026-07-10"
    candidate = "auto_test"
    delivery = root / "lidousha" / date
    active = base / "out" / date / candidate
    delivery.mkdir(parents=True)
    active.mkdir(parents=True)
    target = delivery / "clip.record.json"
    target.write_bytes(b"old\n")
    blob = active / "transaction" / "intended.bin"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"new\n")
    journal_path = active / "transaction" / "journal.json"
    plan = {
        "date": date,
        "invalidations": [
            {
                "candidate_id": candidate,
                "documents": [{"path": str(target)}],
            }
        ],
    }
    journal = {
        "schema_version": reviewed.TRANSACTION_SCHEMA,
        "status": "PREPARED",
        "date": date,
        "plan_sha256": "a" * 64,
        "code_fingerprint": "sha256:" + "b" * 64,
        "upload_enabled": False,
        "entries": [
            {
                "target": str(target),
                "intended_blob": str(blob),
                "intended_sha256": _sha(blob.read_bytes()),
            }
        ],
    }
    monkeypatch.setattr(reviewed, "ROOT", root)
    monkeypatch.setattr(reviewed, "BASE", base)
    return plan, journal, journal_path, target, blob


def test_invalidation_transaction_rolls_forward_and_is_idempotent(tmp_path, monkeypatch):
    plan, journal, journal_path, target, _blob = _transaction_fixture(tmp_path, monkeypatch)
    reviewed._atomic_write_json_file(journal_path, journal)

    committed = reviewed._commit_transaction(
        journal_path=journal_path,
        journal=journal,
        plan=plan,
        plan_sha256="a" * 64,
        code_fingerprint="sha256:" + "b" * 64,
        state_is_bound=False,
    )
    assert committed["status"] == "COMMITTED"
    assert target.read_bytes() == b"new\n"
    reviewed._commit_transaction(
        journal_path=journal_path,
        journal=committed,
        plan=plan,
        plan_sha256="a" * 64,
        code_fingerprint="sha256:" + "b" * 64,
        state_is_bound=False,
    )

    target.write_bytes(b"new bound cover document\n")
    reviewed._commit_transaction(
        journal_path=journal_path,
        journal=committed,
        plan=plan,
        plan_sha256="a" * 64,
        code_fingerprint="sha256:" + "b" * 64,
        state_is_bound=True,
    )
    with pytest.raises(reviewed.ReviewedCoverRepairError, match="drifted before state"):
        reviewed._commit_transaction(
            journal_path=journal_path,
            journal=committed,
            plan=plan,
            plan_sha256="a" * 64,
            code_fingerprint="sha256:" + "b" * 64,
            state_is_bound=False,
        )


def test_invalidation_transaction_rejects_blob_drift_and_target_escape(tmp_path, monkeypatch):
    plan, journal, journal_path, target, blob = _transaction_fixture(tmp_path, monkeypatch)
    blob.write_bytes(b"tampered\n")
    with pytest.raises(reviewed.ReviewedCoverRepairError, match="invalid invalidation"):
        reviewed._commit_transaction(
            journal_path=journal_path,
            journal=journal,
            plan=plan,
            plan_sha256="a" * 64,
            code_fingerprint="sha256:" + "b" * 64,
            state_is_bound=False,
        )

    blob.write_bytes(b"new\n")
    escaped = tmp_path / "escaped.json"
    escaped.write_bytes(b"old\n")
    plan["invalidations"][0]["documents"][0]["path"] = str(escaped)
    journal["entries"][0]["target"] = str(escaped)
    with pytest.raises(reviewed.ReviewedCoverRepairError, match="escapes active roots"):
        reviewed._commit_transaction(
            journal_path=journal_path,
            journal=journal,
            plan=plan,
            plan_sha256="a" * 64,
            code_fingerprint="sha256:" + "b" * 64,
            state_is_bound=False,
        )


@pytest.mark.parametrize(
    "name",
    [
        "2026-07-10.reviewed.v1.json",
        "2026-07-22.background-diversity-and-title.v1.json",
        "2026-07-22.full-replay-rerun-reviewed.v1.json",
        "2026-07-22.full-replay-title-scale.v1.json",
    ],
)
def test_checked_in_historical_cover_plans_cannot_be_executed(name):
    path = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/cover_repair_plans/history"
        / name
    )

    with pytest.raises(
        reviewed.ReviewedCoverRepairError,
        match="historical evidence only",
    ):
        reviewed.load_plan(path)
