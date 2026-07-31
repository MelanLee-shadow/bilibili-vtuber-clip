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


def test_cover_scope_tag_binding_ignores_order_but_not_set_drift():
    assert cover_repair._same_tag_set(
        ["李豆沙", "虚拟主播", "切片"],
        ["切片", "李豆沙", "虚拟主播"],
    )
    assert not cover_repair._same_tag_set(
        ["李豆沙", "虚拟主播", "切片"],
        ["李豆沙", "虚拟主播", "错误标签"],
    )
    assert not cover_repair._same_tag_set(
        ["李豆沙", "李豆沙"],
        ["李豆沙"],
    )


def test_creator_review_state_after_cover_edit_is_pending_not_drift():
    before = _snapshot()
    current = _snapshot()
    current["creator"]["state"] = -6
    current["creator"]["state_desc"] = "修改内容待审核…"
    current["creator"]["metadata"]["cover"] = NEW_COVER
    projection, problems = cover_repair._transition_projection(
        current,
        {"before": before},
        NEW_COVER,
    )
    assert projection == "pending"
    assert problems == []


def test_unexpected_creator_state_after_cover_edit_is_drift():
    before = _snapshot()
    current = _snapshot()
    current["creator"]["state"] = -5
    current["creator"]["state_desc"] = "退回"
    current["creator"]["metadata"]["cover"] = NEW_COVER
    projection, problems = cover_repair._transition_projection(
        current,
        {"before": before},
        NEW_COVER,
    )
    assert projection == "drift"
    assert problems == ["Creator changed outside cover"]


def test_exact_historical_review_state_false_block_can_resume_polling():
    before = _snapshot()
    pending = _snapshot()
    pending["creator"]["state"] = -6
    pending["creator"]["state_desc"] = "修改内容待审核…"
    pending["creator"]["metadata"]["cover"] = NEW_COVER
    rows = [
        {"state": "EDIT_AMBIGUOUS", "details": {}},
        {
            "state": "BLOCKED_DRIFT",
            "details": {
                "reason": "Creator changed outside cover",
                "live_snapshot": pending,
            },
        },
    ]
    assert cover_repair._recoverable_creator_review_block(
        rows,
        {"before": before},
        NEW_COVER,
    )
    rows[-1]["details"]["reason"] = (
        "Creator changed outside cover; exact section changed"
    )
    assert not cover_repair._recoverable_creator_review_block(
        rows,
        {"before": before},
        NEW_COVER,
    )


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


def test_plan_survives_a_disk_round_trip_and_revalidates(tmp_path, monkeypatch):
    """写盘 → 读回 → 重校验，是崩溃后 run/status/verify-live 的重入路径。

    `create_plan` 末尾会自校验，所以 `validate_plan` 在正向上一直有间接覆盖；
    真正没被直接测过的是**从磁盘读回**这一段——而它恰恰是 CLI 唯一的重入口
    （`_load_cover_repair_manifest` = `load_plan` + `validate_plan`）。
    cover-only lane 至今零生产执行，重入路径尤其不能只靠"应该没问题"。
    """

    manifest, plan, plan_path, _journal = _materialize(tmp_path, monkeypatch)

    reloaded = cover_repair.load_plan(plan_path)
    assert reloaded == plan, "读回的 plan 必须与写入的逐字段相同"
    # 不抛即通过：重入时的再校验必须接受自己刚冻结的 plan
    cover_repair.validate_plan(reloaded, manifest=manifest)


def test_tampered_plan_on_disk_is_rejected_on_reentry(tmp_path, monkeypatch):
    """磁盘上的 plan 被改过就必须在重入时拒绝，不能带着漂移继续执行。"""

    manifest, _plan, plan_path, _journal = _materialize(tmp_path, monkeypatch)

    for field, value in (
        ("bvid", "BV1tampered000"),
        ("unchanged_cid", 999999999),
        ("old_cover_url", "http://i0.hdslb.com/bfs/archive/tampered.png"),
    ):
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        payload[field] = value
        plan_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        with pytest.raises(cover_repair.CoverRepairError):
            cover_repair.validate_plan(
                cover_repair.load_plan(plan_path), manifest=manifest
            )
