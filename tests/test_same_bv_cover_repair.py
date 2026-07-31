from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice import same_bv_cover_repair as cover_repair
from src.autoslice import same_bv_repair as repair
from src.autoslice import publication_reconciliation as reconciliation


BVID = "BV1s7326qEc9"
OLD_COVER = "https://archive.biliimg.com/bfs/archive/aaaaaaaaaaaaaaaa.jpg"
NEW_COVER = "https://i0.hdslb.com/bfs/archive/bbbbbbbbbbbbbbbb.png"


def _snapshot(
    *, cover: str = OLD_COVER, title: str = "白色奶龙", cid: int = 202
) -> dict:
    metadata = {
        "title": title,
        "desc": "desc",
        "tags": sorted(["李豆沙", "切片"]),
        "tid": 21,
        "copyright": 2,
        "source": "https://live.bilibili.com/",
        "cover": cover_repair.normalise_cover_url(cover),
    }
    return {
        "creator": {
            "available": True,
            "bvid": BVID,
            "aid": 101,
            "state": 0,
            "state_desc": "开放浏览",
            "metadata": metadata,
            "videos": [{"cid": cid, "filename": "old", "title": title}],
        },
        "public": {
            "available": True,
            "bvid": BVID,
            "aid": 101,
            "cid": cid,
            "state": 0,
            "metadata": {k: v for k, v in metadata.items() if k != "source"},
        },
        "section": {
            "available": True,
            "section_id": 9320779,
            "matches": [
                {"bvid": BVID, "aid": 101, "cid": cid, "title": title}
            ],
        },
    }


def _materialize(tmp_path: Path, monkeypatch):
    cover = tmp_path / "white-dragon.cover.png"
    cover.write_bytes(b"reviewed white dragon cover")
    manifest = {
        "manifest_version": 3,
        "title": "白色奶龙",
        "description": "desc",
        "tags": ["李豆沙", "切片"],
        "publish_policy": {
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
        },
        "season": {
            "season_id": 8383206,
            "section_id": 9320779,
            "season_title": "小李切片",
        },
        "cover": {
            "path": str(cover.resolve()),
            "sha256": repair.sha256_file(cover),
            "bytes": cover.stat().st_size,
        },
        "authorization": {"by": "Ivan", "quote": "修复后上传"},
        "package_attestation": {
            "final_human_review": {"sha256": "f" * 64},
            "title_cover_qc": {"sha256": "c" * 64},
        },
        "recovery_publication_authority": {"schema_version": "test"},
    }
    manifest_path = tmp_path / "authorized-upload-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    authority = {
        "candidate_id": "white-dragon",
        "bvid": BVID,
        "aid": 101,
        "cid": 202,
    }
    monkeypatch.setattr(
        repair,
        "validate_repair_publication_target",
        lambda candidate, bvid: dict(authority),
    )
    plan = cover_repair.create_plan(
        manifest_path=manifest_path,
        manifest=manifest,
        bvid=BVID,
        snapshot=_snapshot(),
    )
    plan_path = tmp_path / "cover-plan.json"
    journal = tmp_path / "cover-journal.jsonl"
    cover_repair.write_plan(plan_path, plan)
    cover_repair.initialise_journal(journal, plan_path, plan)
    return manifest, plan, plan_path, journal


class FakeAdapter:
    def __init__(self, observations, *, edit_error: Exception | None = None):
        self.observations = list(observations)
        self.last_observation = self.observations[-1]
        self.edit_error = edit_error
        self.prepare_calls = 0
        self.edit_calls = 0

    def observe(self, bvid: str, section_id: int):
        assert bvid == BVID
        assert section_id == 9320779
        if self.observations:
            self.last_observation = self.observations.pop(0)
        return self.last_observation

    def prepare_cover(self, cover_path: Path) -> str:
        assert cover_path.name == "white-dragon.cover.png"
        self.prepare_calls += 1
        return NEW_COVER

    def edit_cover_only(self, bvid, *, expected_creator, cover_url):
        assert bvid == BVID
        assert expected_creator["videos"][0]["cid"] == 202
        assert cover_url == NEW_COVER
        self.edit_calls += 1
        if self.edit_error:
            raise self.edit_error
        return {"code": 0}


def test_plan_freezes_old_new_authority_and_rejects_noncover_drift(
    tmp_path, monkeypatch
):
    manifest, plan, _plan_path, _journal = _materialize(
        tmp_path, monkeypatch
    )
    assert plan["old_cover_url"].endswith("/aaaaaaaaaaaaaaaa.jpg")
    assert plan["replacement_cover"]["sha256"] == manifest["cover"]["sha256"]
    assert plan["unchanged_cid"] == 202
    assert plan["package_attestation"]["title_cover_qc"] == {
        "sha256": "c" * 64
    }

    drifted = _snapshot(title="wrong title")
    with pytest.raises(
        cover_repair.CoverPlanInvalid,
        match="manifest target metadata differs",
    ):
        cover_repair.create_plan(
            manifest_path=Path(plan["manifest"]["path"]),
            manifest=manifest,
            bvid=BVID,
            snapshot=drifted,
        )


def test_plan_accepts_prior_full_replacement_when_only_cover_changed_since(
    tmp_path, monkeypatch
):
    manifest, _plan, _plan_path, _journal = _materialize(
        tmp_path, monkeypatch
    )
    completed_snapshot = _snapshot(
        cover="https://archive.biliimg.com/bfs/archive/cccccccccccccccc.png",
        cid=303,
    )
    completed_path = tmp_path / "prior-full-completed.json"
    completed_path.write_text(
        json.dumps({"live_snapshot": completed_snapshot}) + "\n",
        encoding="utf-8",
    )

    def predecessor_replay(path, *, authority, bvid, snapshot):
        assert path == completed_path.resolve()
        assert authority["cid"] == 202
        assert bvid == BVID
        assert cover_repair.snapshots_equivalent(snapshot, completed_snapshot)
        return {
            "schema_version": "same-bv-repair-predecessor.v1",
            "completed": {
                "path": str(path),
                "sha256": repair.sha256_file(path),
            },
            "new_cid": 303,
        }

    monkeypatch.setattr(
        repair, "_predecessor_completion_attestation", predecessor_replay
    )
    current = _snapshot(cover=OLD_COVER, cid=303)
    bridged = cover_repair.create_plan(
        manifest_path=Path(_plan["manifest"]["path"]),
        manifest=manifest,
        bvid=BVID,
        snapshot=current,
        predecessor_completed_path=completed_path,
    )
    assert bridged["unchanged_cid"] == 303
    bridge = bridged["predecessor_completion"]["cover_only_bridge"]
    assert bridge["rule"] == "ALL_FIELDS_EXCEPT_CREATOR_PUBLIC_COVER_EXACT"
    assert bridge["planning_creator_cover"].endswith(
        "/aaaaaaaaaaaaaaaa.jpg"
    )


def test_run_edits_cover_once_keeps_cid_and_fresh_verify_is_create_only(
    tmp_path, monkeypatch
):
    manifest, _plan, plan_path, journal = _materialize(tmp_path, monkeypatch)
    target = _snapshot(cover=NEW_COVER)
    adapter = FakeAdapter([_snapshot(), _snapshot(), target])
    result = cover_repair.run(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=0,
        poll_seconds=0,
    )
    assert result.state == "VERIFIED"
    assert adapter.prepare_calls == 1
    assert adapter.edit_calls == 1
    assert result.details["unchanged_cid"] == 202

    completed_path = tmp_path / "cover-completed.json"
    completed = cover_repair.verify_live(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=FakeAdapter([target]),
        out=completed_path,
    )
    assert completed["schema_version"] == cover_repair.COMPLETED_SCHEMA
    assert completed["unchanged_cid"] == 202
    original_bytes = completed_path.read_bytes()
    repeated = cover_repair.verify_live(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=FakeAdapter([target]),
        out=completed_path,
    )
    assert repeated == completed
    assert completed_path.read_bytes() == original_bytes


def test_completed_receipt_reconciles_as_unchanged_cid_publication(
    tmp_path, monkeypatch
):
    manifest, _plan, plan_path, journal = _materialize(tmp_path, monkeypatch)
    target = _snapshot(cover=NEW_COVER)
    result = cover_repair.run(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=FakeAdapter([_snapshot(), _snapshot(), target]),
        wait_seconds=0,
        poll_seconds=0,
    )
    assert result.state == "VERIFIED"
    completed_path = tmp_path / "cover-completed.json"
    cover_repair.verify_live(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=FakeAdapter([target]),
        out=completed_path,
    )
    monkeypatch.setattr(
        reconciliation,
        "_candidate_and_date",
        lambda _manifest: ("white-dragon", "2026-07-29"),
    )
    monkeypatch.setattr(
        reconciliation,
        "_commit_projection",
        lambda **kwargs: kwargs["publication"],
    )
    publication = reconciliation.reconcile_same_bv_cover_publication(
        completed_path=completed_path,
        manifest=manifest,
        manifest_path=Path(
            json.loads(completed_path.read_text(encoding="utf-8"))["manifest"][
                "path"
            ]
        ),
        autoslice_base=tmp_path / "base",
        registry_path=tmp_path / "registry.json",
        reconciled_at="2026-07-29T12:00:00+00:00",
    )
    assert publication["status"] == "VERIFIED_SAME_BV_COVER"
    assert publication["cid"] == 202
    assert publication["authority"]["sha256"] == repair.sha256_file(
        completed_path
    )

    runtime_entry = {
        "candidate_id": "white-dragon",
        "recording_date": "2026-07-29",
        "status": "published",
        "bvid": BVID,
        "publication_reconciliation": publication,
    }
    assert reconciliation.validate_runtime_registry_entry(runtime_entry)


def test_edit_intent_is_never_retried_after_ambiguous_response(
    tmp_path, monkeypatch
):
    manifest, _plan, plan_path, journal = _materialize(tmp_path, monkeypatch)
    adapter = FakeAdapter(
        [_snapshot(), _snapshot(), _snapshot()],
        edit_error=TimeoutError("lost response"),
    )
    first = cover_repair.run(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=0,
        poll_seconds=0,
    )
    assert first.state == "EDIT_AMBIGUOUS"
    assert adapter.edit_calls == 1

    second = cover_repair.run(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=0,
        poll_seconds=0,
    )
    assert second.state == "EDIT_AMBIGUOUS"
    assert adapter.edit_calls == 1


def test_live_drift_before_asset_upload_blocks_without_mutation(
    tmp_path, monkeypatch
):
    manifest, _plan, plan_path, journal = _materialize(tmp_path, monkeypatch)
    adapter = FakeAdapter([_snapshot(title="external edit")])
    result = cover_repair.run(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=0,
        poll_seconds=0,
    )
    assert result.state == "BLOCKED_DRIFT"
    assert adapter.prepare_calls == 0
    assert adapter.edit_calls == 0


def test_fresh_verify_rejects_noncover_drift(tmp_path, monkeypatch):
    manifest, _plan, plan_path, journal = _materialize(tmp_path, monkeypatch)
    target = _snapshot(cover=NEW_COVER)
    result = cover_repair.run(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=FakeAdapter([_snapshot(), _snapshot(), target]),
        wait_seconds=0,
        poll_seconds=0,
    )
    assert result.state == "VERIFIED"
    with pytest.raises(cover_repair.CoverRepairError, match="fresh verification"):
        cover_repair.verify_live(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=FakeAdapter([_snapshot(cover=NEW_COVER, title="drift")]),
            out=tmp_path / "must-not-exist.json",
        )
    assert not (tmp_path / "must-not-exist.json").exists()


def test_journal_tamper_is_rejected(tmp_path, monkeypatch):
    manifest, _plan, plan_path, journal = _materialize(tmp_path, monkeypatch)
    rows = journal.read_text(encoding="utf-8").splitlines()
    row = json.loads(rows[0])
    row["details"]["remote_mutation"] = "tampered"
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(cover_repair.CoverJournalCorrupt, match="hash mismatch"):
        cover_repair.status(
            plan_path=plan_path, journal=journal, manifest=manifest
        )


def test_journal_append_completes_short_writes(tmp_path, monkeypatch):
    manifest, plan, plan_path, old_journal = _materialize(
        tmp_path, monkeypatch
    )
    old_journal.unlink()
    real_write = cover_repair.os.write

    def short_write(fd, data):
        return real_write(fd, bytes(data[:5]))

    monkeypatch.setattr(cover_repair.os, "write", short_write)
    cover_repair.initialise_journal(old_journal, plan_path, plan)
    result = cover_repair.status(
        plan_path=plan_path, journal=old_journal, manifest=manifest
    )
    assert result.state == "PLANNED"


class FakeSession:
    def __init__(self, view_data):
        self.view_data = view_data
        self.build_calls = []
        self.edit_calls = []

    def archive_view(self, bvid):
        assert bvid == BVID
        return self.view_data

    def build_edit_payload(self, view_data, **kwargs):
        self.build_calls.append((view_data, kwargs))
        return {"aid": 101, "videos": view_data["videos"], **kwargs}

    def edit_archive(self, payload):
        self.edit_calls.append(payload)
        return {"code": 0}


def test_production_adapter_rechecks_creator_and_changes_only_cover():
    raw = {
        "archive": {
            "bvid": BVID,
            "aid": 101,
            "state": 0,
            "state_desc": "开放浏览",
            "title": "白色奶龙",
            "desc": "desc",
            "tag": "李豆沙,切片",
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
            "cover": OLD_COVER,
        },
        "videos": [{"cid": 202, "filename": "old", "title": "白色奶龙"}],
    }
    session = FakeSession(raw)
    adapter = repair.BilibiliRepairAdapter(
        session=session,
        http=lambda _url: {},
        view_url="{bvid}",
        tags_url="{bvid}",
        section_url="{section_id}",
    )
    expected = repair.normalise_creator_snapshot(
        bvid=BVID, creator_data=raw
    )
    response = adapter.edit_cover_only(
        BVID, expected_creator=expected, cover_url=NEW_COVER
    )
    assert response == {"code": 0}
    assert session.build_calls[0][1] == {"cover_url": NEW_COVER}
    assert session.edit_calls[0]["videos"] == raw["videos"]

    drifted = dict(expected)
    drifted["videos"] = [{"cid": 999, "filename": "other", "title": "x"}]
    with pytest.raises(repair.RemoteMutationError, match="drifted"):
        adapter.edit_cover_only(
            BVID, expected_creator=drifted, cover_url=NEW_COVER
        )
    assert len(session.edit_calls) == 1
