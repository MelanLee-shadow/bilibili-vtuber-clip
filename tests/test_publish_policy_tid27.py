"""New-upload policy and proof-bound old archive policy; all inputs are local fixtures."""

from __future__ import annotations
import copy
import hashlib
import json
import pytest

from src.autoslice import publish_policy as policy
from src.autoslice import recovery_title_authority, new_bv_repair_authority
from src.autoslice.authorized_upload_recovery_cli import (
    UploadRecoveryError,
    assert_posted_policy_ledger,
    manifest_ledger_binding,
)


def proof(tid=21):
    return {
        "bvid": "BV1Mug46EEQz",
        "member_archive": {
            "bvid": "BV1Mug46EEQz",
            "aid": 42,
            "tid": tid,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
        },
        "public_view": {"aid": 42, "cid": 101, "tid": tid, "copyright": 2},
    }


def authority(path, raw):
    return {
        "schema_version": "recovery-publication-authority-fixture",
        "candidate_id": "fixture",
        "bvid": "BV1Mug46EEQz",
        "aid": 42,
        "cid": 101,
        "source_public_verify_repo_path": path.name,
        "source_public_verify_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize("tid", [21, 27, 171])
def test_same_bv_policy_is_read_from_real_hash_bound_source_bytes(tmp_path, monkeypatch, tid):
    path = tmp_path / "public-proof.json"
    raw = json.dumps(proof(tid)).encode()
    path.write_bytes(raw)
    envelope = authority(path, raw)
    monkeypatch.setattr(
        recovery_title_authority,
        "validate_recovery_publication_authority",
        lambda value, **_: value,
    )
    manifest = {"title": "fixture", "recovery_publication_authority": envelope}
    actual = policy.manifest_policy(manifest, repo_root=tmp_path)
    assert actual == {"tid": tid, "copyright": 2, "source": "https://live.bilibili.com/"}
    manifest["publish_policy"] = actual
    assert policy.policy_problems(manifest, ordinary_upload=True) == [
        "EXISTING_BV_POLICY_NOT_NEW_UPLOAD"
    ]


@pytest.mark.parametrize("field", ["tid", "copyright", "source", "aid", "bvid"])
def test_publication_policy_proof_missing_or_wrong_identity_refuses(tmp_path, monkeypatch, field):
    data = proof()
    data["member_archive"].pop(field)
    raw = json.dumps(data).encode()
    path = tmp_path / "public-proof.json"
    path.write_bytes(raw)
    monkeypatch.setattr(
        recovery_title_authority,
        "validate_recovery_publication_authority",
        lambda value, **_: value,
    )
    with pytest.raises(ValueError, match="POLICY"):
        policy.manifest_policy(
            {"title": "fixture", "recovery_publication_authority": authority(path, raw)},
            repo_root=tmp_path,
        )


def test_publication_policy_sha_and_public_creator_category_must_agree(tmp_path, monkeypatch):
    path = tmp_path / "public-proof.json"
    raw = json.dumps(proof()).encode()
    path.write_bytes(raw)
    envelope = authority(path, raw)
    monkeypatch.setattr(
        recovery_title_authority,
        "validate_recovery_publication_authority",
        lambda value, **_: value,
    )
    manifest = {"title": "fixture", "recovery_publication_authority": envelope}
    path.write_bytes(raw + b"\n")
    with pytest.raises(ValueError, match="HASH_DRIFT"):
        policy.manifest_policy(manifest, repo_root=tmp_path)
    data = proof()
    data["public_view"]["tid"] = 27
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    manifest["recovery_publication_authority"] = authority(path, raw)
    with pytest.raises(ValueError, match="DISAGREE"):
        policy.manifest_policy(manifest, repo_root=tmp_path)


def test_new_bv_authority_replays_original_policy_without_current_default(monkeypatch):
    envelope = {
        "schema_version": "new-bv-same-bv-publication-authority.v1",
        "candidate_id": "fixture",
    }
    monkeypatch.setattr(
        recovery_title_authority,
        "validate_recovery_publication_authority",
        lambda value, **_: value,
    )
    original = {"tid": 171, "copyright": 1, "source": ""}
    monkeypatch.setattr(
        new_bv_repair_authority,
        "replay_first_publication_manifest",
        lambda value: {"publish_policy": original},
    )
    assert (
        policy.manifest_policy({"title": "fixture", "recovery_publication_authority": envelope})
        == original
    )
    assert policy.manifest_policy({"title": "new"}) == {
        "tid": 27,
        "copyright": 2,
        "source": "https://live.bilibili.com/",
    }


def test_historical_policy_requires_exact_posted_or_unknown_ledger_bytes(tmp_path):
    path = tmp_path / "manifest.json"
    manifest = {
        "manifest_version": 3,
        "publish_policy": {"tid": 21, "copyright": 2, "source": policy.SOURCE},
    }
    path.write_text(json.dumps(manifest))
    start = {
        "event": "UPLOAD_ATTEMPT_STARTED",
        "attempt_id": "fixture",
        **manifest_ledger_binding(manifest, path),
    }

    def read(_):
        return [start], []

    assert_posted_policy_ledger(manifest, path, tmp_path / "ledger", read_ledger=read)
    assert policy.policy_problems(manifest, proven_historical=True) == []
    assert policy.policy_problems(manifest, ordinary_upload=True)
    path.write_text(json.dumps({**manifest, "drift": True}))
    with pytest.raises(UploadRecoveryError, match="no exact"):
        assert_posted_policy_ledger(manifest, path, tmp_path / "ledger", read_ledger=read)


def test_known_not_dispatched_attempt_cannot_adopt_historical_policy(tmp_path):
    path = tmp_path / "manifest.json"
    manifest = {"manifest_version": 3}
    path.write_text(json.dumps(manifest))
    start = {
        "event": "UPLOAD_ATTEMPT_STARTED",
        "attempt_id": "fixture",
        **manifest_ledger_binding(manifest, path),
    }
    terminal = {
        "event": "UPLOAD_ATTEMPT_FINISHED",
        "attempt_id": "fixture",
        "rc": 4,
        "submission_outcome": "NOT_DISPATCHED",
    }
    with pytest.raises(UploadRecoveryError, match="no exact"):
        assert_posted_policy_ledger(
            manifest, path, tmp_path / "ledger", read_ledger=lambda _: ([start, terminal], [])
        )


@pytest.mark.parametrize("field,changed", [("tid", 27), ("copyright", 1), ("source", "changed")])
def test_same_bv_plan_policy_invariant_cannot_be_overridden(field, changed):
    original = {"tid": 21, "copyright": 2, "source": policy.SOURCE}
    before = {
        "creator": {"metadata": original},
        "public": {"metadata": {"tid": 21, "copyright": 2}},
    }
    assert policy.preserved_policy_problems(original, before) == []
    target = copy.deepcopy(original)
    target[field] = changed
    assert (
        "SAME_BV_" + field.upper() + "_MUST_PRESERVE_ORIGINAL"
        in policy.preserved_policy_problems(target, before)
    )


def test_hash_bound_public_policy_still_requires_original_cid(tmp_path, monkeypatch):
    data = proof()
    data["public_view"]["cid"] = 202
    raw = json.dumps(data).encode()
    path = tmp_path / "wrong-cid.json"
    path.write_bytes(raw)
    monkeypatch.setattr(
        recovery_title_authority,
        "validate_recovery_publication_authority",
        lambda value, **_: value,
    )
    with pytest.raises(ValueError, match="IDENTITY_DRIFT"):
        policy.manifest_policy(
            {"title": "fixture", "recovery_publication_authority": authority(path, raw)},
            repo_root=tmp_path,
        )
