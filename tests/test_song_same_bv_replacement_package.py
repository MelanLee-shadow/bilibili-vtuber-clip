"""Offline replacement constructor controls; no model or publication calls."""
from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

import scripts.build_song_review_manifest as builder
from src.autoslice import new_bv_repair_authority as authority_module
from src.autoslice import publication_reconciliation as reconciliation
from src.autoslice.review_package_ass_audit import audit_song_burn_ass
from src.autoslice.subtitle_rendering import _write_sapphire_ass_from_srt
from tests.test_build_song_review_manifest import (
    CANDIDATE_ID, TITLE, _fixture, _build, _write_json, _sha,
)
from tests.test_publication_reconciliation import _new_bv_fixture, _entry, _reconcile_new, TAGS


def _media(path: Path, frequency: int = 440) -> None:
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=32x32:r=5:d=1",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=1",
        "-c:v", "libx264", "-threads", "1", "-c:a", "aac", "-shortest", "-y", str(path),
    ], check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _synthetic_season_routes(monkeypatch):
    monkeypatch.setenv('AUTOSLICE_SEASON_IDS', json.dumps({
        lane: {'season_id': 8110000 + index, 'section_id': 9110000 + index}
        for index, lane in enumerate(('talk', 'song', 'independent_related'), 1)}))


def _case(tmp_path: Path, monkeypatch, *, bvid="BV1FBGw6uE6i", completion_ready=True, long_observation=False):
    fx = _fixture(tmp_path, burn_ass=True)
    # A speech cue uses the actual renderer; preserve a legacy prefixed ASS as
    # the first publication, then import the canonical display projection.
    srt = fx["artifact_paths"]["subtitle"]
    # Build the ordinary fixture before freezing the publication. The test's
    # cover witness is synthetic and is not a claim of cover/content acceptance.
    manifest = _build(fx, monkeypatch)
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] 谢谢大家\n\n2\n00:00:01,000 --> 00:00:02,000\n不能停止我对你的爱\n")
    root = fx["package"]
    item = manifest["items"][0]
    (root / item["subtitle_srt"]).write_bytes(srt.read_bytes())
    ass = root / item["ass_path"]
    _write_sapphire_ass_from_srt(root / item["subtitle_srt"], ass)
    replacement_ass = tmp_path / "replacement.ass"
    replacement_ass.write_bytes(ass.read_bytes())
    lines = ass.read_text().splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("Dialogue:") and line.split(",", 9)[3] == "LDS":
            parts = line.split(",", 9); parts[9] = "[李豆沙] " + parts[9]; lines[i] = ",".join(parts)
    ass.write_text("".join(lines))
    _media(root / item["video"])
    replacement_video = tmp_path / "replacement.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(root/item["video"]),
                    "-map", "0", "-c", "copy", "-metadata", "comment=replacement", str(replacement_video)],
                   check=True, capture_output=True)
    record_path = root / item["record"]
    record = json.loads(record_path.read_text())
    record["burned_preview"] = {"status": "BURNED", "path": item["video"], "ass_path": item["ass_path"],
                                "burned_sha256": _sha(root/item["video"])}
    record["artifact_hashes"].update(burned_video_sha256=_sha(root/item["video"]), ass_sha256=_sha(ass),
                                     subtitle_sha256=_sha(root/item["subtitle_srt"]))
    record["upload_tags"] = {"engine": "test", "status": "OK", "final_tags": TAGS}
    _write_json(record_path, record)
    publish_path=root/item["publish_json"]
    publish=json.loads(publish_path.read_text())
    publish["artifact_hashes"].update(record["artifact_hashes"])
    _write_json(publish_path,publish)
    recut_path = root / item["recut_manifest"]
    recut = json.loads(recut_path.read_text()); output = recut["verified_output_binding"]
    output["interval"] = {"start_ms": 0, "end_ms": 1000, "duration_ms": 1000}
    output["artifacts"].update(burned_media_sha256=_sha(root/item["video"]),
        ass_sha256=_sha(ass), subtitle_sha256=_sha(root/item["subtitle_srt"]))
    _write_json(recut_path, recut)
    for role, key in (("video","video"),("active_record","record"),("recut_manifest","recut_manifest"),("subtitle","subtitle_srt"),("publish","publish_json")):
        item["sha256"][role] = _sha(root/item[key])[7:]
    item["ass_sha256"] = _sha(ass)
    if long_observation:
        manifest["cover_observation"] = {"detail": "主播白发及熊猫耳被画面上边缘截断。" * 80}
    # Freeze a structurally valid historical first-publication Song envelope.
    # The fixture's content witnesses are synthetic; candidate audit remains real.
    delivery_path = root / item["delivery_manifest"]
    delivery = json.loads(delivery_path.read_text())
    for role, entry in delivery["artifacts"].items():
        current = root / item[{"active_record": "record", "subtitle": "subtitle_srt", "publish": "publish_json"}.get(role, role)]
        entry.update(sha256=_sha(current), source_sha256=_sha(current))
    _write_json(delivery_path, delivery)
    authority = manifest["delivery_authority"]
    authority["manifest_sha256"] = _sha(delivery_path)
    authority["song_completion_evidence"].update(
        ready=completion_ready, burned_preview_sha256=_sha(root/item["video"]), recut_manifest_sha256=_sha(recut_path))
    _write_json(root/"review_manifest.json",manifest)
    import scripts.audit_review_package as audit_module
    historical_audit = {
        "schema_version": audit_module.AUDIT_SCHEMA_VERSION,
        "policy_epoch": audit_module.AUDIT_POLICY_EPOCH,
        "passed": True, "blocking_issue_count": 0, "issue_count": 0, "issues": [], "root": str(root),
        "audited_inputs": [{"path": p.relative_to(root).as_posix(), "sha256": _sha(p)[7:]}
                           for p in sorted(root.rglob("*")) if p.is_file()],
    }
    _write_json(root/"package_audit.json", historical_audit)
    pub_dir = tmp_path/"publication"; pub_dir.mkdir()
    pub = _new_bv_fixture(pub_dir,candidate=CANDIDATE_ID,date="2026-07-25",title=TITLE,bvid=bvid)
    pm = pub["manifest"]
    from scripts import authorized_upload as au
    # Reuse the publication fixture's actual three-field policy before freezing
    # its authority; the generic reconciliation fixture omits this upload field.
    pm["publish_policy"] = au.archive_policy.checked_policy({
        key: pub["public"]["expected"][key] for key in au.archive_policy.FIELDS
    })
    pm["description"] = au.description_for_title(TITLE)
    pm["season"] = au.season_block_for(TITLE, "song")
    pub["public"]["expected"]["description"] = pm["description"]
    for surface in ("public_view", "member_archive"):
        pub["public"][surface]["desc"] = pm["description"]
    _write_json(pub["public_path"], pub["public"])
    pub["uploaded"]["public_verify_sha256"] = _sha(pub["public_path"])[7:]
    pm["video"] = _entry(root/item["video"]); pm["cover"] = _entry(root/item["cover"])
    pm["package_attestation"] = {"package_root":str(root), "record":_entry(record_path),
        "subtitle":_entry(root/item["subtitle_srt"]), "review_manifest":_entry(root/"review_manifest.json")}
    _write_json(pub["manifest_path"],pm)
    pub["uploaded"].update(manifest_sha256=_entry(pub["manifest_path"])["sha256"],
        record_sha256=_entry(record_path)["sha256"],video_sha256=_entry(root/item["video"])["sha256"],
        cover_sha256=_entry(root/item["cover"])["sha256"])
    _write_json(pub["uploaded_path"],pub["uploaded"])
    _reconcile_new(pub)
    authority = authority_module.build_new_bv_repair_authority(
        reconciliation.authority_sidecar_path(pub["manifest_path"]),candidate_id=CANDIDATE_ID,title=TITLE)
    authority_path=tmp_path/"authority.json"; _write_json(authority_path,authority)
    new_lines=replacement_ass.read_text().splitlines(keepends=True)
    changed=[i for i,(a,b) in enumerate(zip(lines,new_lines),1) if a!=b]
    comparison=tmp_path/"comparison.json"
    _write_json(comparison,{"status":"PASS","srt_path":str(root/item["subtitle_srt"]),"srt_sha256":_sha(root/item["subtitle_srt"]),
        "original_ass_path":str(ass),"original_ass_sha256":_sha(ass),"new_ass_path":str(replacement_ass),"new_ass_sha256":_sha(replacement_ass),
        "changed_lines":changed,"speech_cues":len(changed),"speech_style":"LDS",
        "lyric_events_identical":sum(x.startswith("Dialogue:") and x.split(",",9)[3]=="Default" for x in new_lines)})
    burn=tmp_path/"burn.json"
    _write_json(burn,{"status":"PASS","full_decode_rc":0,"full_decode_stderr":"","branding_intro":None,
        "interval":output["interval"],"target":str(replacement_video),"target_bytes":replacement_video.stat().st_size,
        "target_sha256":_sha(replacement_video),"ass_sha256":_sha(replacement_ass),"original_published_sha256":_sha(root/item["video"]),
        "audio_packet_hashes":{str(p):builder._audio_packet_sha256(p) for p in (root/item["video"],replacement_video)}})
    return dict(package_root=tmp_path/"replacement-package", original_package_root=root,
        replacement_video=replacement_video,replacement_ass=replacement_ass,ass_comparison_path=comparison,
        burn_validation_path=burn,first_publication_authority_path=authority_path)


@pytest.mark.parametrize("bvid", ["BV1FBGw6uE6i", "BV1BJGc6aEWf"])
def test_replacement_consumes_real_publication_and_song_ass_audit(tmp_path,monkeypatch,bvid):
    case=_case(tmp_path,monkeypatch,bvid=bvid)
    before={p.relative_to(case["original_package_root"]):p.read_bytes() for p in case["original_package_root"].rglob("*") if p.is_file()}
    old_root=case["original_package_root"]
    old_item=json.loads((old_root/"review_manifest.json").read_text())["items"][0]
    old_passed,old_issues=audit_song_burn_ass(root=old_root,item=old_item,
        subtitle_path=old_root/old_item["subtitle_srt"],max_visual_lines=2,max_visual_line_chars=28)
    assert not old_passed
    assert "SUBTITLE_ASS_SPEAKER_LABEL_VISIBLE" in {issue.code for issue in old_issues}
    manifest=builder.build_same_bv_replacement(**case)
    item=manifest["items"][0];root=case["package_root"]
    passed,issues=audit_song_burn_ass(root=root,item=item,subtitle_path=root/item["subtitle_srt"],max_visual_lines=2,max_visual_line_chars=28)
    assert passed,issues
    # The shared cover fixture has placeholder image bytes. Keep its existing
    # cover validation stub local to this test; Song byte/authority audit runs.
    import scripts.audit_review_package as audit
    monkeypatch.setattr(audit, "_audit_finished_item_cover", lambda **_kwargs: None)
    current = audit.audit_package(root)
    assert current["passed"], current["issues"]
    assert manifest["upload_allowed"] is False
    assert item["recovery_publication_authority"]["bvid"]==bvid
    for binding in item["same_bv_replacement"]["evidence"].values():
        assert _sha(root/binding["path"])==binding["sha256"]
    assert before=={p.relative_to(case["original_package_root"]):p.read_bytes() for p in case["original_package_root"].rglob("*") if p.is_file()}


@pytest.mark.parametrize("damage", ["audio", "ass-extra-line", "authority-bv", "existing-root"])
def test_replacement_refuses_unbound_changes_before_output(tmp_path,monkeypatch,damage):
    case=_case(tmp_path,monkeypatch)
    if damage=="audio":
        _media(case["replacement_video"],frequency=880)
        burn=json.loads(case["burn_validation_path"].read_text())
        burn.update(target_sha256=_sha(case["replacement_video"]),target_bytes=case["replacement_video"].stat().st_size)
        burn["audio_packet_hashes"][str(case["replacement_video"])]=builder._audio_packet_sha256(case["replacement_video"])
        _write_json(case["burn_validation_path"],burn)
    elif damage=="ass-extra-line":
        p=case["replacement_ass"];p.write_text(p.read_text().replace("不能停止","可以停止"))
        # Even a rehashed comparison cannot authorize an unlisted line change.
        d=json.loads(case["ass_comparison_path"].read_text());d["new_ass_sha256"]=_sha(p);_write_json(case["ass_comparison_path"],d)
    elif damage=="authority-bv":
        d=json.loads(case["first_publication_authority_path"].read_text());d["bvid"]="BV0000000000";d.pop("authority_sha256");d["authority_sha256"]=authority_module._digest(d)
        _write_json(case["first_publication_authority_path"],d)
    else:
        case["package_root"].mkdir();(case["package_root"]/"keep").write_text("unchanged")
    reason = {
        "audio": "audio packet hash differs", "ass-extra-line": "ASS differences",
        "authority-bv": "first-publication authority invalid", "existing-root": "already exists",
    }[damage]
    with pytest.raises(builder.SongReviewManifestError, match=reason):
        builder.build_same_bv_replacement(**case)
    if damage=="existing-root": assert (case["package_root"]/"keep").read_text()=="unchanged"
    else: assert not case["package_root"].exists()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("offline replacement test attempted network access")
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.mark.parametrize("bvid", ["BV1FBGw6uE6i", "BV1BJGc6aEWf"])
def test_replacement_mechanical_cli_and_saved_replay(tmp_path, monkeypatch, bvid):
    from scripts import audit_review_package as audit
    from scripts import build_final_human_review as final_builder
    from src.autoslice import mechanical_delivery_review as mechanical
    case = _case(tmp_path, monkeypatch, bvid=bvid)
    builder.build_same_bv_replacement(**case)
    root = case["package_root"]
    monkeypatch.setattr(audit, "_audit_finished_item_cover", lambda **_kwargs: None)
    verdict = audit.audit_package(root)
    assert verdict["passed"], verdict["issues"]
    _write_json(root/"package_audit.json", verdict)
    out = root / "verification/mechanical-delivery-review.json"
    assert final_builder.main([str(root), "--package-audit", str(root/"package_audit.json"),
                               "--mechanical", "--out", str(out)]) == 0
    receipt = json.loads(out.read_text())
    assert receipt["fresh_human_full_playback_claimed"] is False
    assert receipt["new_upload_authorized"] is False
    assert receipt["bindings"]["items"][0]["publication_target"]["bvid"] == bvid
    assert mechanical.validate_mechanical_receipt(receipt, root, root/"package_audit.json") == receipt
    assert mechanical.validate_saved_mechanical_receipt(receipt, root) == receipt
    assert "story_contract" not in json.loads((root/json.loads((root/"review_manifest.json").read_text())["items"][0]["record"]).read_text())


def test_talk_closure_still_requires_story_contract(tmp_path):
    from src.autoslice import final_human_review as final_review
    root = tmp_path / "talk"; root.mkdir()
    (root/"record.json").write_text(json.dumps({"publish_staging": {"title": TITLE}}))
    manifest = {"status": "finished_review_package_no_upload_pending_human_review", "upload_allowed": False,
                "items": [{"candidate_id": CANDIDATE_ID, "title": TITLE, "record": "record.json",
                           "video": "clip.mp4", "subtitle_srt": "clip.srt", "cover": "cover.png"}]}
    with pytest.raises(final_review.FinalHumanReviewError, match="FINAL_HUMAN_REVIEW_RECORD_IDENTITY_INVALID"):
        final_review._manifest_items(manifest, package_root=root)


@pytest.mark.parametrize("damage", ["source-completion", "current-record", "replacement-evidence"])
def test_song_mechanical_closure_rejects_proof_drift(tmp_path, monkeypatch, damage):
    from src.autoslice import final_human_review as final_review
    # A negative completion is frozen before the real publication-authority
    # constructor, so only the original Song gate can reject this control.
    case = _case(tmp_path, monkeypatch, completion_ready=damage != "source-completion")
    manifest = builder.build_same_bv_replacement(**case)
    root = case["package_root"]; item = manifest["items"][0]
    if damage == "current-record":
        p = root / item["record"]; d = json.loads(p.read_text()); d["delivery_candidate_id"] = "different_song"
        _write_json(p, d)
    elif damage == "replacement-evidence":
        p = root / item["same_bv_replacement"]["evidence"]["burn-validation.json"]["path"]
        d = json.loads(p.read_text()); d["status"] = "FAIL"; _write_json(p, d)
    with pytest.raises(final_review.FinalHumanReviewError, match="FINAL_HUMAN_REVIEW_SONG_CONTRACT_INVALID"):
        final_review._manifest_items(manifest, package_root=root)


@pytest.mark.parametrize("bvid", ["BV1FBGw6uE6i", "BV1BJGc6aEWf"])
def test_replacement_authorized_cli_chain_is_offline(tmp_path, monkeypatch, capsys, bvid):
    from scripts import audit_review_package as audit
    from scripts import authorized_upload as au
    from scripts import build_final_human_review as final_builder
    case = _case(tmp_path, monkeypatch, bvid=bvid)
    manifest = builder.build_same_bv_replacement(**case)
    root = case["package_root"]; item = manifest["items"][0]
    monkeypatch.setattr(audit, "_audit_finished_item_cover", lambda **_kwargs: None)
    verdict = audit.audit_package(root)
    assert verdict["passed"], verdict["issues"]
    audit_path = root / "package_audit.json"; _write_json(audit_path, verdict)
    receipt_path = root / "verification/mechanical-delivery-review.json"
    assert final_builder.main([str(root), "--package-audit", str(audit_path), "--mechanical",
                               "--out", str(receipt_path)]) == 0
    upload_path = tmp_path / "authorized-manifest.json"
    assert au.main(["make-manifest", "--video", str(root/item["video"]),
                    "--cover", str(root/item["cover"]), "--package-audit", str(audit_path),
                    "--title", TITLE, "--quote", "离线 fixture，不授权实际上传",
                    "--final-human-review", str(receipt_path), "--out", str(upload_path)]) == 0
    authorized = json.loads(upload_path.read_text())
    assert authorized["recovery_publication_authority"]["bvid"] == bvid
    assert authorized["season"]["lane"] == "song"
    inherited = item["same_bv_replacement"]["first_publication_metadata"]
    assert authorized["title"] == inherited["title"]
    assert authorized["description"] == inherited["description"]
    assert authorized["tags"] == inherited["tags"]
    assert au.main(["verify", "--manifest", str(upload_path)]) == 0
    authority = authorized["recovery_publication_authority"]
    observations = []
    class ReadOnlyAdapter:
        def observe(self, observed_bvid, section_id):
            assert observed_bvid == bvid
            assert section_id == authorized["season"]["section_id"]
            observations.append((observed_bvid, section_id))
            metadata = {"title": TITLE, "desc": authorized["description"], "tags": authorized["tags"],
                        **authorized["publish_policy"], "cover": "https://example.invalid/old-cover.png"}
            return {
                "creator": {"available": True, "bvid": bvid, "aid": authority["aid"], "state": 0,
                            "state_desc": "开放浏览", "metadata": metadata,
                            "videos": [{"cid": authority["cid"], "filename": "old-file", "title": TITLE}]},
                "public": {"available": True, "bvid": bvid, "aid": authority["aid"], "cid": authority["cid"],
                           "state": 0, "metadata": {k:v for k,v in metadata.items() if k != "source"}},
                "section": {"available": True, "section_id": section_id,
                            "matches": [{"bvid": bvid, "aid": authority["aid"], "cid": authority["cid"], "title": TITLE}]},
            }
    monkeypatch.setattr(au, "_same_bv_adapter", lambda *_args: ReadOnlyAdapter())
    plan_path = tmp_path / "repair-plan.json"
    journal = tmp_path / "repair-journal.jsonl"
    lock = tmp_path / "repair.lock"
    capsys.readouterr()
    assert au.main(["repair-plan", "--manifest", str(upload_path), "--bvid", bvid,
                    "--out", str(plan_path), "--journal", str(journal),
                    "--lock", str(lock), "--dry-run"]) == 0
    # The ordinary dry CLI prints a plan and deliberately does not persist it.
    plan = json.loads(capsys.readouterr().out)
    assert not plan_path.exists()
    assert not journal.exists()
    assert observations
    assert plan["bvid"] == bvid
    # Freeze this exact plan with the native local writers. Later commands must
    # reach their PLANNED state, without a Talk rejection or any remote mutation.
    au.write_same_bv_repair_plan(plan_path, plan)
    au.initialise_same_bv_repair_journal(journal, plan_path, plan)
    before = journal.read_bytes()
    assert au.main(["repair-status", "--plan", str(plan_path), "--journal", str(journal)]) == 6
    assert json.loads(capsys.readouterr().out)["state"] == "PLANNED"
    assert au.main(["repair-run", "--plan", str(plan_path), "--journal", str(journal), "--dry-run"]) == 6
    assert json.loads(capsys.readouterr().out)["state"] == "PLANNED"
    completed = tmp_path / "completed.json"
    assert au.main(["repair-verify-live", "--plan", str(plan_path), "--journal", str(journal),
                    "--lock", str(lock), "--out", str(completed)]) == 6
    assert "repair is PLANNED" in capsys.readouterr().err
    assert journal.read_bytes() == before
    assert not completed.exists()
    assert len(observations) == 1


def test_song_mechanical_cli_rejects_symlinked_default_parent(tmp_path, monkeypatch):
    from scripts import audit_review_package as audit
    from scripts import build_final_human_review as final_builder
    case = _case(tmp_path, monkeypatch)
    builder.build_same_bv_replacement(**case)
    root = case["package_root"]
    monkeypatch.setattr(audit, "_audit_finished_item_cover", lambda **_kwargs: None)
    verdict = audit.audit_package(root); assert verdict["passed"]
    _write_json(root/"package_audit.json", verdict)
    outside = tmp_path / "outside"; outside.mkdir()
    (root/"verification").symlink_to(outside, target_is_directory=True)
    assert final_builder.main([str(root), "--package-audit", str(root/"package_audit.json"), "--mechanical"]) == 2
    assert not list(outside.iterdir())


def test_replacement_preserves_long_observation_without_treating_it_as_a_path(tmp_path, monkeypatch):
    from scripts import audit_review_package as audit
    case = _case(tmp_path, monkeypatch, long_observation=True)
    manifest = builder.build_same_bv_replacement(**case)
    root = case["package_root"]
    original = json.loads((case["original_package_root"]/"review_manifest.json").read_text())
    assert manifest["cover_observation"] == original["cover_observation"]
    assert builder.validate_same_bv_replacement_package(root) == manifest
    monkeypatch.setattr(audit, "_audit_finished_item_cover", lambda **_kwargs: None)
    verdict = audit.audit_package(root)
    assert verdict["passed"], verdict["issues"]


@pytest.mark.parametrize("damage", ["record-projection", "source-authority", "current-audit"])
def test_authorized_song_gate_rejects_rehashed_replacement_drift(tmp_path, monkeypatch, damage):
    from scripts import audit_review_package as audit
    from scripts import authorized_upload as au
    case = _case(tmp_path, monkeypatch)
    review = builder.build_same_bv_replacement(**case)
    root = case["package_root"]; item = review["items"][0]
    monkeypatch.setattr(audit, "_audit_finished_item_cover", lambda **_kwargs: None)
    verdict = audit.audit_package(root)
    assert verdict["passed"], verdict["issues"]
    record_path = root / item["record"]
    record = json.loads(record_path.read_text())
    assert au._strict_verified_song_package(root=root, review=review, review_item=item, record=record, audit=verdict)
    if damage == "record-projection":
        record["artifact_hashes"]["burned_video_sha256"] = "sha256:" + "0" * 64
        _write_json(record_path, record)
    elif damage == "source-authority":
        review["source_delivery_authority"]["song_completion_evidence"]["ready"] = False
        _write_json(root / "review_manifest.json", review)
    else:
        verdict["audited_inputs"] = [row for row in verdict["audited_inputs"] if row.get("path") != item["video"]]
    # A matching current SHA cannot launder a forbidden projection change.
    for row in verdict["audited_inputs"]:
        path = root / row["path"]
        if path.is_file():
            row["sha256"] = _sha(path)
    assert not au._strict_verified_song_package(root=root, review=review, review_item=item, record=record, audit=verdict)
