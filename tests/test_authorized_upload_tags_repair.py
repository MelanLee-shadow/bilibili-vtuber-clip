"""Offline transaction controls; not evidence of live Bilibili acceptance."""
import copy
import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from src.autoslice import authorized_upload_tags_repair as tags_repair
from src.autoslice.same_bv_repair import normalise_creator_snapshot


def setup(tmp_path):
    tags = ["李豆沙", "虚拟主播", "虚拟UP主", "直播切片"]
    manifest = {"title": "月饼", "tags": tags, "season": {"lane": "talk", "season_title": "小李切片"},
                "package_attestation": {"subtitle": {"sha256": "b" * 64}}}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    published = {"bvid": "BV1test", "aid": 2, "cid": 3, "status": "VERIFIED_PUBLIC",
                 "manifest_sha256": sha}
    proof_path = tmp_path / "public_verify.json"
    proof_path.write_text(json.dumps({"status": "VERIFIED_PUBLIC", "bvid": "BV1test",
                                     "expected": {"section_id": 7}}))
    published.update(public_verify=str(proof_path),
                     public_verify_sha256=hashlib.sha256(proof_path.read_bytes()).hexdigest())
    (tmp_path / "manifest.json.uploaded.json").write_text(json.dumps(published))
    input_path, result_path = tmp_path / "INPUT.json", tmp_path / "TAGS.json"
    input_path.write_text(json.dumps({"bvid": "BV1test", "manifest_sha256": sha,
                                     "srt_sha256": "b" * 64, "title": "月饼"}))
    result_path.write_text(json.dumps({"status": "OK", "final_tags": [*tags, "月饼"]}))
    raw = {"archive": {"bvid": "BV1test", "aid": 2, "state": 0,
                       "title": "月饼", "tag": ",".join(tags), "desc": "描述", "cover": "cover"},
           "videos": [{"cid": 3, "filename": "original", "title": "P1"}]}

    class Adapter:
        def __init__(self):
            self.session = self
            self.raw = copy.deepcopy(raw)
            self.edits = 0
            self.ambiguous = False

        def archive_view(self, _bvid):
            return copy.deepcopy(self.raw)

        def build_edit_payload(self, current, *, tag):
            payload = copy.deepcopy(current)
            payload["archive"]["tag"] = tag
            return payload

        def edit_archive(self, payload):
            self.edits += 1
            self.raw = payload
            if self.ambiguous:
                raise TimeoutError()
            return {"code": 0}

        def observe(self, bvid, section_id):
            creator = normalise_creator_snapshot(bvid=bvid, creator_data=self.raw)
            return {"creator": creator,
                    "public": {"available": True, "aid": 2, "cid": 3, "state": 0,
                               "metadata": copy.deepcopy(creator["metadata"])},
                    "section": {"available": True, "section_id": section_id,
                                "matches": [{"aid": 2, "cid": 3, "title": "月饼"}]}}

    adapter = Adapter()
    args = SimpleNamespace(manifest=str(path), bvid="BV1test", tag_input=str(input_path),
                           tag_result=str(result_path), receipt=str(tmp_path / "receipt.json"),
                           quote="补齐标签", authorized_by="维护者", lock=None,
                           cookie_json="unused", execute=True, wait=0, poll=0)
    dependencies = dict(load_and_verify=lambda *_a, **_k: (manifest, []),
                        adapter_factory=lambda _p: adapter, upload_lock=lambda _p: nullcontext(),
                        default_lock=tmp_path / "lock")
    return args, adapter, dependencies


def test_tag_edit_preserves_video_and_metadata_and_resume_never_edits(tmp_path):
    args, adapter, dependencies = setup(tmp_path)
    code, receipt = tags_repair.run(args, **dependencies)
    assert code == 0 and receipt["status"] == "VERIFIED_TAGS_ONLY"
    assert adapter.edits == 1
    assert tags_repair.stable_without_tags(receipt["before"]) == tags_repair.stable_without_tags(receipt["after"])
    assert adapter.raw["videos"] == [{"cid": 3, "filename": "original", "title": "P1"}]
    code, _ = tags_repair.run(args, **dependencies)
    assert code == 0 and adapter.edits == 1


def test_ambiguous_tag_edit_only_observes_the_existing_intent(tmp_path):
    args, adapter, dependencies = setup(tmp_path)
    adapter.ambiguous = True
    code, receipt = tags_repair.run(args, **dependencies)
    assert code == 6 and receipt["status"] == "EDIT_OUTCOME_UNKNOWN"
    code, receipt = tags_repair.run(args, **dependencies)
    assert code == 0 and receipt["status"] == "VERIFIED_TAGS_ONLY"
    assert adapter.edits == 1


def test_tag_repair_rejects_source_binding_drift_before_edit(tmp_path):
    args, adapter, dependencies = setup(tmp_path)
    payload = json.loads((tmp_path / "INPUT.json").read_text())
    payload["srt_sha256"] = "c" * 64
    (tmp_path / "INPUT.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="PROPOSAL_BINDING"):
        tags_repair.run(args, **dependencies)
    assert adapter.edits == 0


def test_tag_repair_rejects_non_tag_change_after_plan(tmp_path):
    args, adapter, dependencies = setup(tmp_path)
    args.execute = False
    assert tags_repair.run(args, **dependencies)[0] == 0
    adapter.raw["archive"]["desc"] = "用户已修改"
    args.execute = True
    with pytest.raises(ValueError, match="NON_TAG_DRIFT"):
        tags_repair.run(args, **dependencies)
    assert adapter.edits == 0


def test_corrupt_receipt_cannot_authorize_another_edit(tmp_path):
    args, adapter, dependencies = setup(tmp_path)
    args.execute = False
    tags_repair.run(args, **dependencies)
    path = tmp_path / "receipt.json"
    receipt = json.loads(path.read_text())
    receipt["before"]["creator"]["metadata"]["desc"] = "伪造"
    path.write_text(json.dumps(receipt))
    args.execute = True
    with pytest.raises(ValueError, match="RECEIPT_BINDING"):
        tags_repair.run(args, **dependencies)
    assert adapter.edits == 0
