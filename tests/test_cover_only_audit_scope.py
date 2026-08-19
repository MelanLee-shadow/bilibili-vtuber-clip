from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import audit_review_package as package_audit
from src.autoslice import cover_only_audit_scope as scope_mod
from src.autoslice import review_package_source_fact_audit as source_fact_audit
from src.autoslice.surface_canon import CHANNEL_PROFILE


CANDIDATE = "auto_192000_909_1014"
TITLE = CHANNEL_PROFILE.talk_title_prefix + "观众想让新3D永久保留“白色浣熊”表情，小主拒绝花钱"


def _sha(path: Path) -> str:
    return scope_mod._sha256(path, prefixed=True)


def _scope_fixture(tmp_path: Path, monkeypatch):
    root = tmp_path / "package"
    root.mkdir()
    stem = "white-dragon"
    video = root / f"{stem}.mp4"
    subtitle = root / f"{stem}.srt"
    cover = root / f"{stem}.cover.png"
    record_path = root / f"{stem}.record.json"
    publish_path = root / f"{stem}.publish.json"
    video.write_bytes(b"reviewed-video")
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n白色浣熊\n",
        encoding="utf-8",
    )
    cover.write_bytes(b"new-reviewed-cover")
    authority = {
        "candidate_id": CANDIDATE,
        "bvid": "BV1s7326qEc9",
        "aid": 101,
        "cid": 202,
        "authority_sha256": "sha256:" + "a" * 64,
    }
    record = {
        "story_contract": {"candidate_id": CANDIDATE},
        "publish_staging": {"title": TITLE},
        "recovery_publication_authority": authority,
        "upload_tags": {"final_tags": ["主播", "切片"]},
        "artifact_hashes": {
            "video_sha256": _sha(video),
            "subtitle_sha256": _sha(subtitle),
            "cover_sha256": _sha(cover),
        },
    }
    record_path.write_text(json.dumps(record), encoding="utf-8")
    publish_path.write_text(json.dumps({"title": TITLE}), encoding="utf-8")

    predecessor_plan_path = tmp_path / "predecessor-plan.json"
    predecessor_plan_path.write_text("{}\n", encoding="utf-8")
    prior_review_path = tmp_path / "prior-review.json"
    prior_review_path.write_text("{}\n", encoding="utf-8")
    completed_path = tmp_path / "predecessor-completed.json"
    completed_path.write_text("{}\n", encoding="utf-8")
    plan = {
        "plan_id": "prior-plan",
        "bvid": authority["bvid"],
        "recovery_publication_authority": authority,
        "target_metadata": {
            "title": TITLE,
            "desc": "desc",
            # Public API readback may reorder a semantically identical tag set.
            "tags": ["切片", "主播"],
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
        },
        "replacement": {
            "video": {
                "sha256": scope_mod._sha256(video),
                "bytes": video.stat().st_size,
            }
        },
        "package_attestation": {
            "final_human_review": {
                "path": str(prior_review_path),
                "sha256": scope_mod._sha256(prior_review_path),
                "bytes": prior_review_path.stat().st_size,
            }
        },
    }
    completed = {
        "candidate_id": CANDIDATE,
        "bvid": authority["bvid"],
        "aid": authority["aid"],
        "new_cid": 303,
        "plan": {"path": str(predecessor_plan_path)},
    }
    predecessor = {
        "verified_journal_row": {
            "journal_path": str(tmp_path / "journal.jsonl"),
            "seq": 4,
            "row_sha256": "b" * 64,
        }
    }
    review_item = {
        "candidate_id": CANDIDATE,
        "reviewed_title": TITLE,
        "publication_target": {
            **authority,
            "final_title": TITLE,
        },
        "artifacts": {
            "video": {"sha256": _sha(video)},
            "subtitle": {"sha256": _sha(subtitle)},
        },
    }
    receipt = {
        "reviewed_at": "2026-07-30T08:49:20+00:00",
        "reviewed_by": "Codex root",
        "status": "ACCEPTED_FOR_SAME_BV",
        "scope": "same_bv_repair",
    }
    monkeypatch.setattr(
        scope_mod,
        "_replay_predecessor",
        lambda _path: (completed, plan, predecessor),
    )
    monkeypatch.setattr(
        scope_mod,
        "_prior_review",
        lambda _plan, *, candidate_id: (receipt, review_item),
    )
    monkeypatch.setattr(
        scope_mod,
        "_canonical_authority",
        lambda raw, *, candidate_id, title: dict(raw),
    )
    return root, stem, completed_path


def test_scope_replays_exact_noncover_bytes_and_new_cover(tmp_path, monkeypatch):
    root, stem, completed_path = _scope_fixture(tmp_path, monkeypatch)
    scope = scope_mod.create_scope(
        package_root=root,
        candidate_id=CANDIDATE,
        predecessor_completed_path=completed_path,
        authorized_by="维护者",
        authorization_quote="其他没什么问题，只要求替换封面。",
        created_at="2026-07-31T00:00:00+00:00",
    )
    item = {
        "candidate_id": CANDIDATE,
        "stem": stem,
        "title": TITLE,
        "video": f"{stem}.mp4",
        "subtitle_srt": f"{stem}.srt",
        "cover": f"{stem}.cover.png",
        "record": f"{stem}.record.json",
        "publish_json": f"{stem}.publish.json",
    }
    assert scope_mod.validate_scope(scope, package_root=root, item=item) == scope
    assert scope["publication_target"]["current_cid"] == 303
    assert scope["reused_gate"] == "SOURCE_FACT_REVIEW_MISSING_ONLY"
    assert scope["frozen_noncover"]["tags"] == ["主播", "切片"]


def test_scope_rejects_title_drift_and_present_receipt(tmp_path, monkeypatch):
    root, _stem, completed_path = _scope_fixture(tmp_path, monkeypatch)
    publish_path = root / "white-dragon.publish.json"
    publish_path.write_text(json.dumps({"title": "CPA的新标题"}), encoding="utf-8")
    with pytest.raises(scope_mod.CoverOnlyAuditScopeError, match="title differs"):
        scope_mod.create_scope(
            package_root=root,
            candidate_id=CANDIDATE,
            predecessor_completed_path=completed_path,
            authorized_by="维护者",
            authorization_quote="只换封面",
        )
    publish_path.write_text(
        json.dumps({"title": TITLE, "source_fact_review": {"status": "bad"}}),
        encoding="utf-8",
    )
    with pytest.raises(scope_mod.CoverOnlyAuditScopeError, match="present receipt"):
        scope_mod.create_scope(
            package_root=root,
            candidate_id=CANDIDATE,
            predecessor_completed_path=completed_path,
            authorized_by="维护者",
            authorization_quote="只换封面",
        )


def test_scope_rejects_tag_set_drift(tmp_path, monkeypatch):
    root, _stem, completed_path = _scope_fixture(tmp_path, monkeypatch)
    record_path = root / "white-dragon.record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["upload_tags"]["final_tags"] = ["主播", "错误标签"]
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(
        scope_mod.CoverOnlyAuditScopeError,
        match="tags differ",
    ):
        scope_mod.create_scope(
            package_root=root,
            candidate_id=CANDIDATE,
            predecessor_completed_path=completed_path,
            authorized_by="维护者",
            authorization_quote="只换封面",
        )


def test_canonical_source_fact_missing_accepts_only_valid_scope(tmp_path, monkeypatch):
    scope_path = tmp_path / "scope.json"
    scope_path.write_text('{"fixture": true}\n', encoding="utf-8")
    publish_path = tmp_path / "clip.publish.json"
    publish_path.write_text("{}\n", encoding="utf-8")
    issues: list[dict] = []
    monkeypatch.setattr(
        source_fact_audit,
        "validate_cover_only_audit_scope",
        lambda *_args, **_kwargs: {},
    )
    package_audit._audit_item_story_contract(
        root=tmp_path,
        manifest={},
        item={
            "candidate_id": CANDIDATE,
            "stem": "clip",
            "cover_only_audit_scope": scope_path.name,
        },
        issues=issues,
        stem="clip",
        subtitle_path=None,
        chat_authority={},
        publish_path=publish_path,
        title_txt_path=None,
        publish_title=TITLE,
        title_txt="",
        record_path=tmp_path / "clip.record.json",
        record={"publish_staging": {}},
        story_contract={"schema_version": "fixture"},
        story_contract_required=True,
        is_song=False,
    )
    assert "SOURCE_FACT_REVIEW_MISSING" not in {row["code"] for row in issues}


def test_canonical_source_fact_scope_cannot_read_external_absolute_path(tmp_path, monkeypatch):
    package_root = tmp_path / "package"
    package_root.mkdir()
    external_scope = tmp_path / "external-scope.json"
    external_scope.write_text('{"fixture": true}\n', encoding="utf-8")
    publish_path = package_root / "clip.publish.json"
    publish_path.write_text("{}\n", encoding="utf-8")
    issues: list[dict] = []
    monkeypatch.setattr(
        source_fact_audit,
        "validate_cover_only_audit_scope",
        lambda *_args, **_kwargs: pytest.fail("external scope must not be validated"),
    )

    package_audit._audit_item_story_contract(
        root=package_root,
        manifest={},
        item={
            "candidate_id": CANDIDATE,
            "stem": "clip",
            "cover_only_audit_scope": str(external_scope),
        },
        issues=issues,
        stem="clip",
        subtitle_path=None,
        chat_authority={},
        publish_path=publish_path,
        title_txt_path=None,
        publish_title=TITLE,
        title_txt="",
        record_path=package_root / "clip.record.json",
        record={"publish_staging": {}},
        story_contract={"schema_version": "fixture"},
        story_contract_required=True,
        is_song=False,
    )

    assert "COVER_ONLY_AUDIT_SCOPE_INVALID" in {row["code"] for row in issues}


def test_canonical_scope_cannot_override_present_invalid_receipt(tmp_path, monkeypatch):
    receipt = {"status": "bad"}
    publish_path = tmp_path / "clip.publish.json"
    publish_path.write_text(json.dumps({"source_fact_review": receipt}), encoding="utf-8")
    issues: list[dict] = []
    scope_called = False

    def should_not_call(*_args, **_kwargs):
        nonlocal scope_called
        scope_called = True

    monkeypatch.setattr(source_fact_audit, "validate_cover_only_audit_scope", should_not_call)
    monkeypatch.setattr(source_fact_audit, "validate_source_fact_review", lambda *_a, **_k: False)
    package_audit._audit_item_story_contract(
        root=tmp_path,
        manifest={},
        item={"candidate_id": CANDIDATE, "stem": "clip"},
        issues=issues,
        stem="clip",
        subtitle_path=None,
        chat_authority={},
        publish_path=publish_path,
        title_txt_path=None,
        publish_title=TITLE,
        title_txt="",
        record_path=tmp_path / "clip.record.json",
        record={"publish_staging": {"source_fact_review": receipt}},
        story_contract={
            "schema_version": "fixture",
            "source_fact_review": receipt,
        },
        story_contract_required=True,
        is_song=False,
    )
    assert scope_called is False
    assert "SOURCE_FACT_REVIEW_INVALID" in {row["code"] for row in issues}
