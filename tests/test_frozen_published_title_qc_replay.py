"""Historical QC is reusable only for immutable, already-public title edits."""
import copy
import hashlib
import json

import pytest

from scripts import authorized_upload as au
from scripts.run_title_cover_joint_qc import package_story_context
from src.autoslice.host_only_v4_package_binding import (
    FROZEN_HOST_ONLY_V4_AUTHORITY,
    FROZEN_HOST_ONLY_V4_SCHEMA,
    HostOnlyV4PackageBindingError,
    materialize_package_binding,
    validate_package_binding,
)
from src.autoslice.story_contract import cover_story_contract_binding


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _publication(tmp_path):
    manifest = {"title": "【李豆沙】一个旧标题", "package_attestation": {}}
    uploaded = {"status": "VERIFIED_PUBLIC", "bvid": "BV1test", "aid": 12, "cid": 34,
                "title": manifest["title"]}
    for key in ("video", "cover", "record", "subtitle", "package_audit", "review_manifest", "title_cover_qc"):
        path = tmp_path / key
        path.write_bytes(key.encode())
        entry = {"path": str(path), "sha256": au.sha256_file(path)}
        (manifest if key in {"video", "cover"} else manifest["package_attestation"])[key] = entry
        uploaded[key + "_sha256"] = entry["sha256"]
    manifest_path = tmp_path / "original.json"
    _write(manifest_path, manifest)
    proof = {"status": "VERIFIED_PUBLIC", "problems": [], "bvid": "BV1test",
             "expected": {"title": manifest["title"], "section_id": 56},
             "public_view": {"title": manifest["title"], "aid": 12, "cid": 34},
             "member_archive": {"title": manifest["title"], "aid": 12, "bvid": "BV1test"},
             "section_api": {"episode_match_count": 1}}
    proof_path = tmp_path / "public.json"
    _write(proof_path, proof)
    uploaded.update(manifest=str(manifest_path), manifest_sha256=au.sha256_file(manifest_path),
                    public_verify=str(proof_path), public_verify_sha256=au.sha256_file(proof_path))
    _write(au.uploaded_sidecar_path(manifest_path), uploaded)
    return manifest_path, manifest, uploaded, proof, proof_path


def test_original_publication_proof_replay_is_read_only(tmp_path):
    path, manifest, *_ = _publication(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    assert au._original_published_title_replay_problems(path, manifest) == []
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}


def test_m27_original_manifest_accepts_relocation_but_rejects_byte_drift(tmp_path):
    path, manifest, *_ = _publication(tmp_path)
    moved = tmp_path / "moved.json"
    moved.write_bytes(path.read_bytes())
    au.uploaded_sidecar_path(moved).write_bytes(au.uploaded_sidecar_path(path).read_bytes())
    assert au._original_published_title_replay_problems(moved, manifest) == []
    moved.write_bytes(moved.read_bytes() + b" ")
    assert "original published manifest/identity drift" in au._original_published_title_replay_problems(moved, manifest)


def test_public_title_compatibility_requires_explicit_title_scope_and_original_proof(tmp_path, monkeypatch):
    path, manifest, uploaded, *_ = _publication(tmp_path)
    manifest.update(manifest_version=3, schema_version="authorized-upload-manifest.v3",
                    authorization={"by": "维护者", "quote": "qualified clips authorized"},
                    title="【李豆沙】这是一个足够长度的测试标题", tags=["李豆沙", "虚拟主播", "直播切片"],
                    description=au.DEFAULT_DESCRIPTION,
                    publish_policy={"tid": au.EXPECTED_TID, "copyright": au.EXPECTED_COPYRIGHT,
                                    "source": au.EXPECTED_SOURCE})
    _write(path, manifest)
    observed = []
    monkeypatch.setattr(au, "_validate_v3_package_attestation",
                        lambda _m, **kw: observed.append(kw["frozen_published_title_replay"]) or [])
    # Use the unchanged valid proof fixture, avoiding unrelated title-policy
    # evaluation. Proof validity itself is exercised above against real bytes.
    monkeypatch.setattr(au, "upload_manifest_title_policy_violations", lambda *_a: [])
    monkeypatch.setattr(au, "_original_published_title_replay_problems", lambda *_a: [])
    au.load_and_verify(path)
    au.load_and_verify(path, frozen_plan_resume=True)
    _, accepted = au.load_and_verify(path, frozen_plan_resume=True, published_title_only_replay=True)
    _, rejected = au.load_and_verify(path, published_title_only_replay=True)
    assert accepted == []
    assert rejected and observed == [False, False, True, False]
    monkeypatch.setattr(au, "_original_published_title_replay_problems", lambda *_a: ["original proof drift"])
    _, rejected = au.load_and_verify(path, frozen_plan_resume=True, published_title_only_replay=True)
    assert "original proof drift" in rejected and observed[-1] is False


@pytest.mark.parametrize("drift", ["manifest", "video", "cover", "record", "subtitle",
                                  "title_cover_qc", "unpublished", "proof_hash", "cid", "section"])
def test_historical_qc_cannot_cover_drift_or_unpublished_package(tmp_path, drift):
    path, manifest, uploaded, proof, proof_path = _publication(tmp_path)
    if drift == "manifest":
        path.write_bytes(path.read_bytes() + b" ")
    elif drift in {"video", "cover", "record", "subtitle", "title_cover_qc"}:
        (tmp_path / drift).write_bytes(b"changed")
    elif drift == "unpublished":
        uploaded["status"] = "POSTED_UNVERIFIED"
        _write(au.uploaded_sidecar_path(path), uploaded)
    elif drift == "proof_hash":
        proof_path.write_bytes(proof_path.read_bytes() + b" ")
    else:
        if drift == "cid":
            proof["public_view"]["cid"] = 35
        else:
            proof["section_api"]["episode_match_count"] = 2
        _write(proof_path, proof)
        uploaded["public_verify_sha256"] = au.sha256_file(proof_path)
        _write(au.uploaded_sidecar_path(path), uploaded)
    assert au._original_published_title_replay_problems(path, manifest)


def _story():
    hook = "她把朋友提到的纯爱小说听成另一部书，聊了半天才发现认错。"
    return {"schema_version": "lidousha-story-contract.v1", "candidate_id": "candidate",
            "selection_hook": hook,
            "selection_hook_sha256": "sha256:" + hashlib.sha256(hook.encode()).hexdigest(),
            "relation_state": "UNKNOWN", "participants": [],
            "cover_counterpart_reference_available": False, "cover_reference_authority": None,
            "source_media_sha256s": ["sha256:" + "1" * 64], "clip_context_binding": {"source": "original"},
            "cover_fallback_mode": "HOST_ONLY_GENERIC", "boundary_semantic_review": None,
            "human_boundary_authority": None,
            "source_fact_review": {"original_title": "old", "final_title": "old",
                                   "original_selection_hook": "old", "passes": 1,
                                   "receipt_sha256": "old", "source_media_sha256": "same"}}


def test_only_known_title_review_metadata_can_differ_in_frozen_story():
    story = _story()
    binding = cover_story_contract_binding(story)
    binding["source_fact_review"] = dict(story["source_fact_review"], final_title="new", passes=2)
    kwargs = {"record": {"story_contract": story}, "generation": {"story_contract": binding},
              "candidate_id": "candidate"}
    with pytest.raises(ValueError, match="package story differs"):
        package_story_context(**kwargs)
    assert package_story_context(**kwargs, frozen_published_replay=True)["selection_hook"] == story["selection_hook"]
    binding["source_fact_review"]["source_media_sha256"] = "different"
    with pytest.raises(ValueError, match="package story differs"):
        package_story_context(**kwargs, frozen_published_replay=True)
    binding["source_fact_review"] = copy.deepcopy(story["source_fact_review"])
    binding["participants"] = ["invented participant"]
    with pytest.raises(ValueError, match="package story differs"):
        package_story_context(**kwargs, frozen_published_replay=True)


def test_v4_replay_retains_generation_and_checks_original_pixels_and_verdict(tmp_path):
    from src.autoslice.cover_host_identity_gate import HOST_ONLY_AUTHORITY, HOST_ONLY_SCHEMA_VERSION

    root = tmp_path / "package"
    root.mkdir()
    cover, reference, comparison = (root / n for n in ("cover.png", "ref.png", "compare.png"))
    for p in (cover, reference, comparison):
        p.write_bytes(p.name.encode())
    def digest(p):
        return "sha256:" + au.sha256_file(p)
    verdict = {key: True for key in ("source_lidousha_located", "primary_subject_is_lidousha",
               "primary_subject_is_visually_dominant", "primary_subject_face_is_large_and_clear",
               "primary_subject_carries_story_reaction", "thumbnail_has_clear_click_hook")}
    verdict.update({key: False for key in ("primary_subject_matches_other_source_participant",
                   "excessive_dead_space", "meaningless_dominant_decoration", "other_recognizable_people_or_avatars_visible")})
    verdict.update(identity_conflicts=[], composition_conflicts=[], other_recognizable_people_or_avatars=[],
                   primary_subject_identity="same host", reason="original pixel comparison")
    receipt = {"schema_version": HOST_ONLY_SCHEMA_VERSION, "authority": HOST_ONLY_AUTHORITY,
               "status": "PASS", "host_only_required": True,
               "final_cover_sha256": digest(cover), "reference_path": str(reference),
               "reference_sha256": digest(reference), "comparison_path": str(comparison),
               "comparison_sha256": digest(comparison), "verdict": verdict,
               "witness": {"provider": "cpa", "model": "original-model", "status": "OBSERVED",
                           "image_sha256": au.sha256_file(comparison), "answer": json.dumps(verdict)}}
    generation = {"final_host_identity_verification": receipt}
    item = {"candidate_id": "candidate", "cover": cover.name}
    binding = materialize_package_binding(root=root, item=item, generation=generation)
    item["host_only_v4_binding"] = binding
    receipt.update(schema_version=FROZEN_HOST_ONLY_V4_SCHEMA, authority=FROZEN_HOST_ONLY_V4_AUTHORITY)
    binding.update(receipt_schema_version=FROZEN_HOST_ONLY_V4_SCHEMA, receipt_authority=FROZEN_HOST_ONLY_V4_AUTHORITY)
    receipt_path = root / binding["receipt_path"]
    _write(receipt_path, receipt)
    binding["receipt_sha256"] = digest(receipt_path)
    before = copy.deepcopy(generation)
    with pytest.raises(HostOnlyV4PackageBindingError, match="no portable identity"):
        validate_package_binding(root=root, item=item, generation=generation)
    validate_package_binding(root=root, item=item, generation=generation, frozen_published_v4=True)
    assert generation == before
    (root / binding["comparison_path"]).write_bytes(b"wrong pixels")
    with pytest.raises(HostOnlyV4PackageBindingError, match="does not match package bytes"):
        validate_package_binding(root=root, item=item, generation=generation, frozen_published_v4=True)
    (root / binding["comparison_path"]).write_bytes(comparison.read_bytes())
    receipt["verdict"]["primary_subject_is_lidousha"] = False
    receipt["witness"]["answer"] = json.dumps(receipt["verdict"])
    _write(receipt_path, receipt)
    binding["receipt_sha256"] = digest(receipt_path)
    with pytest.raises(HostOnlyV4PackageBindingError, match="original CPA verdict is invalid"):
        validate_package_binding(root=root, item=item, generation=generation, frozen_published_v4=True)
