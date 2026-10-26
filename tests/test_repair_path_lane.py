"""Offline lane regressions; synthetic bytes are not publication acceptance."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.autoslice import new_bv_repair_authority as normal
from src.autoslice import original_patch_package as original
from src.autoslice import recovery_title_authority as recovery
from src.autoslice import same_bv_repair as repair
from src.autoslice import same_bv_stage_repair as stage
from src.autoslice.publication_reconciliation import authority_sidecar_path
from src.autoslice.title_policy import publish_title_lane, publish_title_policy_violations
from tests.test_publication_reconciliation import (
    CANDIDATE, _new_bv_fixture, _reconcile_new, _sha, _write_json,
)
from tests.test_same_bv_stage_repair import metadata_fixture

TALK = "【李豆沙】请求抱抱，怎么还要加一句咬一下？"
SONG = "【李豆沙】豆沙歌，《失眠飞行》"
NEW_SONG = "【李豆沙】豆沙歌，《晴天》"


def published(tmp_path, title):
    fixture = _new_bv_fixture(tmp_path, title=title)
    _reconcile_new(fixture)
    source = authority_sidecar_path(fixture["manifest_path"])
    authority = normal.build_new_bv_repair_authority(
        source, candidate_id=CANDIDATE, title=title,
    )
    return fixture, source, authority


@pytest.mark.parametrize("title", [TALK, SONG, NEW_SONG])
def test_first_publication_to_same_bv_plan_keeps_the_title_lane(tmp_path, monkeypatch, title):
    fixture, source, authority = published(tmp_path, title)
    source_bytes = source.read_bytes()
    assert recovery.validate_recovery_publication_authority(
        authority, candidate_id=CANDIDATE, expected_final_title=title,
    ) == authority
    assert recovery.expected_recovery_publish_title(authority) == title
    manifest = copy.deepcopy(fixture["manifest"])
    manifest.update(
        recovery_publication_authority=authority,
        publish_policy={"tid": 21, "copyright": 2, "source": "https://live.bilibili.com/"},
        authorization={"by": "test", "quote": "offline plan only"},
    )
    manifest["season"].update(season_id=7, section_id=8)
    for kind in ("video", "cover"):
        manifest[kind]["bytes"] = Path(manifest[kind]["path"]).stat().st_size
    path = tmp_path / "synthetic-successor.json"
    _write_json(path, manifest)
    aid, cid, bvid = authority["aid"], authority["cid"], authority["bvid"]
    metadata = {
        "title": title, "desc": manifest["description"], "tags": sorted(manifest["tags"]),
        **manifest["publish_policy"], "cover": "https://example.invalid/cover.png",
    }
    snapshot = {
        "creator": {"available": True, "bvid": bvid, "aid": aid, "state": 0,
                    "metadata": metadata, "videos": [{"cid": cid, "filename": "old", "title": title}]},
        "public": {"available": True, "bvid": bvid, "aid": aid, "cid": cid, "state": 0,
                   "metadata": {k: v for k, v in metadata.items() if k != "source"}},
        "section": {"available": True, "section_id": 8,
                    "matches": [{"bvid": bvid, "aid": aid, "cid": cid, "title": title}]},
    }
    # Isolate planning from content acceptance. Production still replays this gate;
    # these explicitly synthetic fixtures do not manufacture a review receipt.
    monkeypatch.setattr(repair, "_final_human_review_attestation", lambda manifest: {})
    plan = repair.create_plan(manifest_path=path, manifest=manifest, bvid=bvid, snapshot=snapshot)
    repair.validate_plan(plan, manifest=manifest)
    assert plan["target_metadata"]["title"] == title
    assert plan["before"] == snapshot
    assert plan["season"] == {k: manifest["season"][k] for k in ("season_id", "section_id", "season_title")}
    assert source.read_bytes() == source_bytes
    assert not list(tmp_path.glob("*.jsonl"))


@pytest.mark.parametrize("published_title,successor", [(TALK, SONG), (SONG, TALK)])
def test_bound_publication_lane_cannot_be_switched_by_successor(tmp_path, published_title, successor):
    _, source, _ = published(tmp_path, published_title)
    with pytest.raises(normal.NewBvRepairAuthorityError, match="successor title invalid"):
        normal.build_new_bv_repair_authority(source, candidate_id=CANDIDATE, title=successor)


@pytest.mark.parametrize("title,lane,code", [
    (TALK, "song", "song_catalog_title_not_exact"),
    (SONG, "talk", "talk_title_uses_song_prefix"),
    (SONG + "，多余钩子", "song", "song_catalog_title_not_exact"),
])
def test_lane_specific_title_rejections_stay_strict(title, lane, code):
    assert code in publish_title_policy_violations(title, lane=lane)


@pytest.mark.parametrize("damage", ["source_bytes", "observed_title", "candidate", "final_title"])
def test_song_authority_still_replays_hash_and_identity_guards(tmp_path, damage):
    _, source, authority = published(tmp_path, SONG)
    if damage == "source_bytes":
        source.write_bytes(source.read_bytes() + b" ")
    else:
        key, value = {
            "observed_title": ("observed_public_title", TALK),
            "candidate": ("candidate_id", "another_candidate"),
            "final_title": ("title", NEW_SONG),
        }[damage]
        authority[key] = value
        authority.pop("authority_sha256")
        authority["authority_sha256"] = normal._digest(authority)
    with pytest.raises(ValueError):
        normal.validate_new_bv_repair_authority(
            authority, candidate_id=CANDIDATE, expected_final_title=SONG,
        )


@pytest.mark.parametrize("title", [TALK, SONG, NEW_SONG])
def test_public_receipt_and_registry_recovery_share_the_title_lane(tmp_path, monkeypatch, title):
    fixture = _new_bv_fixture(tmp_path, title=title)
    evidence = fixture["public_path"]
    authority = recovery.build_recovery_title_authority(
        candidate_id=CANDIDATE, evidence_path=evidence,
        expected_evidence_sha256="sha256:" + _sha(evidence), repo_root=tmp_path,
    )
    assert recovery.validate_recovery_title_authority(
        authority, candidate_id=CANDIDATE, expected_title=title, repo_root=tmp_path,
    ) == authority
    entry = {
        "candidate_id": CANDIDATE, "title_mode": "verified_public_exact",
        "observed_public_title": title, "required_given_end_ms": 1000,
        "boundary_end_mode": "exact_source_pin", "bvid": authority["bvid"],
        "aid": authority["aid"], "cid": authority["cid"],
        "source_public_verify_repo_path": evidence.relative_to(tmp_path).as_posix(),
        "source_public_verify_sha256": "sha256:" + _sha(evidence),
        "source_public_verify_schema_version": "authorized-upload-public-verify.v2",
        "source_public_verify_status": "VERIFIED_PUBLIC",
    }
    assert recovery._validated_publication_entry(entry, repo_root=tmp_path) == entry
    monkeypatch.setattr(recovery, "manual_title_override", lambda candidate_id: title)
    manual = dict(entry, title_mode="reviewer_manual_override")
    assert recovery._validated_publication_entry(manual, repo_root=tmp_path) == manual
    assert recovery.expected_recovery_publish_title(manual) == title
    assert recovery.validate_recovery_title_authority(
        authority, candidate_id=CANDIDATE, expected_title=title, repo_root=tmp_path,
    ) == authority


@pytest.mark.parametrize("title", [SONG + "，钩子", "【李豆沙】豆沙歌，《未闭合"])
def test_song_recovery_does_not_canonicalize_away_invalid_final_titles(tmp_path, title):
    fixture = _new_bv_fixture(tmp_path, title=title)
    with pytest.raises(recovery.RecoveryTitleAuthorityError, match="POLICY_INVALID"):
        recovery.build_recovery_title_authority(
            candidate_id=CANDIDATE, evidence_path=fixture["public_path"],
            expected_evidence_sha256="sha256:" + _sha(fixture["public_path"]), repo_root=tmp_path,
        )


@pytest.mark.parametrize("title", [TALK, SONG, NEW_SONG, SONG + "，钩子"])
def test_original_sealed_target_title_uses_shared_lane(tmp_path, monkeypatch, title):
    reference = tmp_path / "operator.json"
    reference.write_text("{}")
    target = {
        "schema_version": original.TARGET_SCHEMA, "candidate_id": CANDIDATE,
        "recording_date": "2026-07-29", "bvid": "BV1FBGw6uE6i", "aid": 1001, "cid": 2002,
        "old_title": title, "final_title": title, "public_snapshot_sha256": "0" * 64,
        "authorization_ref": {"path": reference.name, "sha256": _sha(reference)},
    }
    # Unit-test only the sealed-target consumer; existing binding tests cover seals.
    monkeypatch.setattr(original, "sealed_asset", lambda *args: (target, "test-seal"))
    monkeypatch.setattr(original, "require_repository_asset_authority", lambda **kwargs: None)
    if publish_title_policy_violations(title, lane=publish_title_lane(title)):
        with pytest.raises(original.OriginalPackageError, match="target title policy"):
            original.publication_authority(CANDIDATE, repo_root=tmp_path)
    else:
        authority = original.publication_authority(CANDIDATE, repo_root=tmp_path)
        assert authority["title"] == title
        assert authority["same_bv_only"] is True


def metadata_stage(tmp_path, *, published_title, title):
    path, request, live = metadata_fixture(tmp_path, title=title)
    historical_path = Path(request["publication_record"]["path"])
    historical = stage.replay(request["publication_record"])
    historical["clips"][0]["title"] = published_title
    _write_json(historical_path, historical)
    request["publication_record"] = stage.binding(historical_path)
    _write_json(path, request)
    return path, request, live


@pytest.mark.parametrize("title", [SONG, NEW_SONG, SONG + "，钩子"])
def test_root_bound_title_cover_stage_uses_shared_lane(tmp_path, title):
    path, _, live = metadata_stage(tmp_path, published_title=SONG, title=title)
    if title.endswith("钩子"):
        with pytest.raises(repair.PlanInvalid, match="current title policy"):
            stage.create_plan(request_path=path, snapshot=live)
    else:
        plan = stage.create_plan(request_path=path, snapshot=live)
        stage.validate_plan(plan, manifest=json.loads(path.read_text()))
        assert plan["target_metadata"]["title"] == title
        assert plan["final_acceptance"] is False
        assert plan["subtitles_complete"] is False


@pytest.mark.parametrize("published_title,successor", [(TALK, SONG), (SONG, TALK)])
def test_bound_stage_lane_cannot_be_switched_by_approved_successor(tmp_path, published_title, successor):
    path, request, live = metadata_stage(tmp_path, published_title=published_title, title=successor)
    original = Path(request["publication_record"]["path"]).read_bytes()
    # The fixture binds the new title throughout root/CPA/quote evidence, so
    # rejection must come from the original publication's lane, not hash drift.
    with pytest.raises(repair.PlanInvalid, match="stage title violates current title policy"):
        stage.create_plan(request_path=path, snapshot=live)
    assert Path(request["publication_record"]["path"]).read_bytes() == original
    assert not list(tmp_path.glob("*.jsonl"))


@pytest.mark.parametrize("published_title", [None, "", "   "])
def test_stage_lane_requires_a_bound_original_title(tmp_path, published_title):
    path, _, live = metadata_stage(tmp_path, published_title=published_title, title=TALK)
    with pytest.raises(repair.PlanInvalid, match="historical publication title missing"):
        stage.create_plan(request_path=path, snapshot=live)
