"""Offline policy-only correction controls; no live API or credential access."""
from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import scripts.authorized_upload as au
from src.autoslice.bilibili_member_api import BiliSession
from src.autoslice.same_bv_repair import normalise_snapshot


BVID = "BV1PAHP6VEZN"


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False))


def _files(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
            for p in root.rglob("*") if p.is_file()}


def setup(tmp_path, *, desc_wrong=False):
    # Resolve the feature in the fixture so a baseline implementation yields
    # individual red controls rather than aborting all test-file collection.
    global repair
    from src.autoslice import authorized_upload_policy_repair as repair

    title = "【李豆沙】豆沙歌，《快乐手帐》"
    desc = au.description_for_title(title)
    policy = au.archive_policy.new_upload_policy()
    manifest = {"manifest_version": 3, "artifact_id": "song-test", "title": title,
                "description": desc, "tags": ["李豆沙", "虚拟主播", "直播切片"],
                "publish_policy": policy, "video": {"sha256": "a" * 64},
                "cover": {"sha256": "b" * 64}, "package_attestation": {},
                "authorization": {"by": "维护者", "quote": "按原稿修复策略字段"}}
    path = tmp_path / "song.upload_manifest.json"
    _write(path, manifest)
    ledger = tmp_path / "ledger.jsonl"

    def write_ledger():
        row = {**au.upload_recovery.manifest_ledger_binding(manifest, path),
               "event": "UPLOAD_ATTEMPT_FINISHED", "bvid": BVID, "attempt_id": "attempt-1"}
        ledger.write_text(json.dumps(row) + "\n")
        return row

    write_ledger()
    raw = {"archive": {"aid": 117495155395942, "bvid": BVID, "state": 0, "state_desc": "已通过",
                       "title": title, "tag": ",".join(manifest["tags"]),
                       "cover": "https://i0.hdslb.com/bfs/archive/cover.jpg",
                       "desc": "旧脚本写死简介" if desc_wrong else desc,
                       **policy, "tid": 21, "dynamic": "保留原动态", "no_reprint": 1,
                       "subtitle": {"open": 0, "lan": ""}, "up_close_reply": 1,
                       "desc_format_id": 0, "interactive": 0, "up_selection_reply": 0,
                       "up_close_danmu": 0},
           "videos": [{"cid": 12345, "filename": "original-file", "title": "原始分P标题"}]}
    proof = {"schema_version": "authorized-upload-public-verify.v2", "status": "PUBLIC_VERIFY_FAILED",
             "bvid": BVID, "verified_at": "2026-10-24T07:00:00+00:00",
             "expected": {**policy, "title": title, "description": desc,
                          "tags": manifest["tags"], "season_id": 8110002, "section_id": 9110002},
             "public_view": {"aid": raw["archive"]["aid"], "cid": 12345},
             "member_archive": {"aid": raw["archive"]["aid"], "bvid": BVID},
             "problems": ["public tid mismatch", "Creator archive tid mismatch"]}
    if desc_wrong:
        proof["problems"] += ["public desc mismatch", "Creator archive desc mismatch"]
    proof_path = au.public_verify_sidecar_path(path)
    _write(proof_path, proof)

    class Session(BiliSession):
        def __init__(self):
            # No production session construction, files or authentication.
            self.csrf = "FAKE-CSRF-DO-NOT-PERSIST"
            self.raw = copy.deepcopy(raw)
            self.edits = []
            self.ambiguous = False
            self.apply = True

        def archive_view(self, bvid):
            assert bvid == BVID
            return copy.deepcopy(self.raw)

        def edit_archive(self, payload):
            self.edits.append(copy.deepcopy(payload))
            if self.apply:
                for field in repair.FIELDS:
                    if field in payload:
                        value = payload[field]
                        if field == "desc" and value != self.raw["archive"]["desc"]:
                            value = self.raw["archive"]["source"] + "\n" + value
                        self.raw["archive"][field] = value
            if self.ambiguous:
                raise TimeoutError("ambiguous synthetic edit")
            return {"code": 0}

    class Adapter:
        def __init__(self):
            self.session = Session()
            self.unavailable = False
            self.after_hook = None
            self.observations = 0

        def observe(self, bvid, section):
            self.observations += 1
            if self.after_hook and self.session.edits:
                self.after_hook(self.session.raw)
            archive = self.session.raw["archive"]
            public = {"code": 0, "data": {**archive, "cid": 12345, "pic": archive["cover"]}}
            snapshot = normalise_snapshot(
                bvid=bvid, section_id=section, creator_data=self.session.raw,
                public_payload=public,
                public_tags_payload={"code": 0, "data": [{"tag_name": t} for t in manifest["tags"]]},
                section_payload={"code": 0, "data": {"id": section, "episodes": [
                    {"bvid": BVID, "aid": archive["aid"], "cid": 12345, "title": archive["title"]}]}},
            )
            if self.unavailable:
                snapshot["public"]["available"] = False
            return snapshot

    adapter = Adapter()
    args = SimpleNamespace(manifest=str(path), bvid=BVID, dry_run=False,
                           ledger=str(ledger), lock=None, cookie_json="unused",
                           wait=0, poll=0)
    loads = []

    def load(_path, **kwargs):
        loads.append(kwargs)
        return manifest, []

    dependencies = dict(load_and_verify=load, read_ledger=au.read_ledger,
                        public_verify_path=au.public_verify_sidecar_path,
                        adapter_factory=lambda _p: adapter,
                        upload_lock=lambda _p: nullcontext(), default_lock=tmp_path / "lock",
                        create_json=au._create_json_sidecar, description_for_title=au.description_for_title)
    return args, adapter, dependencies, manifest, proof_path, write_ledger


@pytest.mark.parametrize("desc_wrong", [False, True])
def test_policy_edit_only_changed_fields_and_preserves_clone_bytes(tmp_path, desc_wrong):
    args, adapter, deps, manifest, _, _ = setup(tmp_path, desc_wrong=desc_wrong)
    before = copy.deepcopy(adapter.session.raw)
    original_payload = adapter.session.build_edit_payload(before)
    code, receipt = repair.run(args, **deps)
    assert code == 0 and receipt["status"] == "VERIFIED_POLICY_ONLY"
    assert set(receipt["changes"]) == ({"tid", "desc"} if desc_wrong else {"tid"})
    payload = adapter.session.edits[0]
    assert {key for key in payload if payload[key] != original_payload[key]} == set(receipt["changes"])
    for field in ("title", "tag", "cover", "videos", "dynamic", "subtitle", "no_reprint"):
        assert json.dumps(payload[field], ensure_ascii=False) == json.dumps(original_payload[field], ensure_ascii=False)
    assert adapter.session.raw["videos"] == before["videos"]
    if desc_wrong:
        assert payload["desc"] == au.description_for_title(manifest["title"], submission=True)
        assert adapter.session.raw["archive"]["desc"] == manifest["description"]
    intent = next(tmp_path.glob("*.policy-repair.intent.json"))
    original_intent = intent.read_bytes()
    assert "FAKE-CSRF" not in original_intent.decode()
    code, _ = repair.run(args, **deps)
    assert code == 0 and len(adapter.session.edits) == 1
    assert intent.read_bytes() == original_intent
    assert not list(tmp_path.glob("*.uploaded.json"))


def test_policy_dry_run_is_read_only_including_mutex_and_reports_field_diff(tmp_path):
    args, adapter, deps, _, _, _ = setup(tmp_path, desc_wrong=True)
    args.dry_run = True
    def no_lock(_):
        pytest.fail("dry-run acquired a writable lock")
    deps["upload_lock"] = no_lock
    before = _files(tmp_path)
    code, receipt = repair.run(args, **deps)
    assert code == 0 and receipt["status"] == "DRY_RUN"
    assert set(receipt["changes"]) == {"tid", "desc"}
    assert receipt["changes"]["tid"] == {"before": 21, "after": 27}
    assert adapter.session.edits == [] and _files(tmp_path) == before


@pytest.mark.parametrize("applied", [False, True])
def test_policy_ambiguous_intent_is_observation_only_on_reentry(tmp_path, applied):
    args, adapter, deps, _, _, _ = setup(tmp_path)
    adapter.session.ambiguous = True
    adapter.session.apply = applied
    code, receipt = repair.run(args, **deps)
    assert code == (0 if applied else 6)
    assert receipt["edit_status"] == "EDIT_OUTCOME_UNKNOWN"
    code, _ = repair.run(args, **deps)
    assert code == (0 if applied else 6)
    assert len(adapter.session.edits) == 1


def test_policy_crash_after_intent_before_edit_never_dispatches_on_resume(tmp_path):
    args, adapter, deps, _, _, _ = setup(tmp_path)
    create = deps["create_json"]
    def crash(path, value):
        create(path, value)
        raise OSError("synthetic crash after durable intent")
    deps["create_json"] = crash
    with pytest.raises(OSError):
        repair.run(args, **deps)
    deps["create_json"] = create
    assert repair.run(args, **deps)[0] == 6
    assert not adapter.session.edits


@pytest.mark.parametrize("problem", ["public title mismatch", "public tags mismatch",
                                    "Creator archive tags mismatch", "public state is -4, expected 0",
                                    "exact section episode title mismatch", "cover mismatch"])
def test_policy_rejects_any_non_policy_public_problem(tmp_path, problem):
    args, adapter, deps, _, proof_path, _ = setup(tmp_path)
    proof = json.loads(proof_path.read_text())
    proof["problems"].append(problem)
    _write(proof_path, proof)
    before = _files(tmp_path)
    with pytest.raises(repair.PolicyRepairError, match="NON_POLICY_PROBLEM"):
        repair.run(args, **deps)
    assert not adapter.session.edits and _files(tmp_path) == before


@pytest.mark.parametrize("mutation", ["missing-bv", "other-bv", "other-manifest", "other-attempt",
                                     "uploaded", "same-bv", "proof-bv", "request-bv", "bad-manifest"])
def test_policy_admission_negative_controls(tmp_path, mutation):
    args, adapter, deps, manifest, proof_path, write_ledger = setup(tmp_path)
    if mutation in {"missing-bv", "other-bv", "other-manifest", "other-attempt"}:
        row = json.loads((tmp_path / "ledger.jsonl").read_text())
        if mutation == "missing-bv":
            row.pop("bvid")
        elif mutation == "other-bv":
            row["bvid"] = "BV1Tst46ABcd"
        elif mutation == "other-manifest":
            row["manifest_sha256"] = "f" * 64
        else:
            row["attempt_id"] = ""
        (tmp_path / "ledger.jsonl").write_text(json.dumps(row) + "\n")
    elif mutation == "uploaded":
        _write(au.uploaded_sidecar_path(tmp_path / "song.upload_manifest.json"), {"status": "VERIFIED_PUBLIC"})
    elif mutation == "same-bv":
        manifest["recovery_publication_authority"] = {"bvid": BVID}
        _write(tmp_path / "song.upload_manifest.json", manifest)
        write_ledger()
    elif mutation == "proof-bv":
        proof = json.loads(proof_path.read_text()); proof["bvid"] = "BV1Tst46ABcd"
        _write(proof_path, proof)
    elif mutation == "request-bv":
        args.bvid = "BV1Tst46ABcd"
    else:
        deps["load_and_verify"] = lambda *_a, **_k: (manifest, ["invalid package"])
    before = _files(tmp_path)
    with pytest.raises(repair.PolicyRepairError):
        repair.run(args, **deps)
    assert not adapter.session.edits and _files(tmp_path) == before


def test_policy_uses_latest_public_verify_and_rejects_newer_bad_observation(tmp_path):
    args, adapter, deps, _, proof_path, _ = setup(tmp_path)
    proof = json.loads(proof_path.read_text())
    proof["verified_at"] = "2026-10-24T08:00:00+00:00"
    proof["problems"].append("public title mismatch")
    _write(proof_path.with_name(proof_path.stem + ".observation-newer.json"), proof)
    with pytest.raises(repair.PolicyRepairError, match="NON_POLICY_PROBLEM"):
        repair.run(args, **deps)
    assert not adapter.session.edits


def test_policy_post_edit_non_policy_drift_fails_without_second_edit(tmp_path):
    args, adapter, deps, _, _, _ = setup(tmp_path)
    adapter.after_hook = lambda raw: raw["archive"].update(cover="other-cover")
    code, receipt = repair.run(args, **deps)
    assert code == 2 and receipt["status"] == "BLOCKED_NON_POLICY_DRIFT"
    assert repair.run(args, **deps)[0] == 2
    assert len(adapter.session.edits) == 1


def test_policy_existing_intent_binding_drift_cannot_authorize_edit(tmp_path):
    args, adapter, deps, _, _, _ = setup(tmp_path)
    adapter.session.apply = False
    assert repair.run(args, **deps)[0] == 6
    path = next(tmp_path.glob("*.policy-repair.intent.json"))
    intent = json.loads(path.read_text()); intent["binding"]["bvid"] = "BV1Tst46ABcd"
    _write(path, intent)
    with pytest.raises(repair.PolicyRepairError, match="INTENT_BINDING_DRIFT"):
        repair.run(args, **deps)
    assert len(adapter.session.edits) == 1


def test_policy_cli_dispatches_with_existing_lock_and_ledger_defaults(tmp_path, monkeypatch, capsys):
    args, adapter, deps, _, _, _ = setup(tmp_path)
    seen_locks = []
    monkeypatch.setattr(au, "DEFAULT_LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(au, "DEFAULT_UPLOAD_LOCK", tmp_path / "default-lock")
    monkeypatch.setattr(au, "load_and_verify", deps["load_and_verify"])
    monkeypatch.setattr(au, "_same_bv_cover_adapter", deps["adapter_factory"])
    monkeypatch.setattr(au, "exclusive_upload_lock", lambda path: seen_locks.append(path) or nullcontext())
    assert au.main(["policy-repair", "--manifest", args.manifest, "--bvid", BVID,
                    "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "DRY_RUN" and not seen_locks
    assert au.main(["policy-repair", "--manifest", args.manifest, "--bvid", BVID,
                    "--lock", str(tmp_path / "custom-lock"), "--wait", "0"]) == 0
    assert seen_locks == [tmp_path / "custom-lock"]


@pytest.mark.parametrize("field,value", [("copyright", 1), ("source", "https://old.invalid/")])
def test_policy_other_allowed_fields_follow_manifest_policy(tmp_path, field, value):
    args, adapter, deps, _, proof_path, _ = setup(tmp_path)
    adapter.session.raw["archive"][field] = value
    proof = json.loads(proof_path.read_text())
    proof["problems"].append(f"Creator archive {field} mismatch")
    _write(proof_path, proof)
    code, receipt = repair.run(args, **deps)
    assert code == 0 and set(receipt["changes"]) == {"tid", field}


def test_policy_resume_waits_for_unavailable_public_surface_without_edit(tmp_path):
    args, adapter, deps, _, _, _ = setup(tmp_path)
    adapter.session.apply = False
    assert repair.run(args, **deps)[0] == 6
    adapter.unavailable = True
    code, receipt = repair.run(args, **deps)
    assert code == 6 and receipt["status"] == "PUBLIC_PENDING"
    assert len(adapter.session.edits) == 1


def test_policy_waits_for_both_surfaces_after_transient_review_state(tmp_path):
    args, adapter, deps, _, _, _ = setup(tmp_path)
    args.wait, args.poll = 2, 1
    ticks = [0]
    def after(raw):
        raw["archive"]["state"] = -4 if ticks[0] == 0 else 0
    adapter.after_hook = after
    deps.update(clock=lambda: ticks[0], sleeper=lambda _s: ticks.__setitem__(0, ticks[0] + 1))
    code, receipt = repair.run(args, **deps)
    assert code == 0 and receipt["status"] == "VERIFIED_POLICY_ONLY"
    assert ticks[0] == 1 and len(adapter.session.edits) == 1
