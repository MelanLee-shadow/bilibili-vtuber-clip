import hashlib
import json
import shutil
import stat
from pathlib import Path

import pytest

from src.autoslice.prepared_review_recovery import historical_prepared_review
from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact, ProducerDeliveryTransactionError, prepare_delivery,
    preflight_prepared_delivery,
)
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice.repository_asset_authority import _canonical_sha256


def _sha(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


def _deployment(repo, commit, *, policy=b"unchanged authority"):
    (repo / "assets").mkdir(parents=True)
    (repo / "assets/policy.txt").write_bytes(policy)
    release = repo / "src/autoslice/overflow_release.py"
    release.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).parents[1] / "src/autoslice/overflow_release.py", release)
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n")
    body = {"schema_version": "deployed-authority-manifest.v1", "deployed_commit": commit,
            "entries": {"assets/policy.txt": {"bytes": len(policy), "sha256": _sha(repo / "assets/policy.txt")}}}
    _json(repo / "DEPLOYED_AUTHORITY_MANIFEST.json", {**body, "manifest_sha256": _canonical_sha256(body)})
    entries = {}
    for path in sorted(repo.rglob("*")):
        if path.name.startswith("DEPLOYED_"):
            continue
        row = {"type": "dir" if path.is_dir() else "file", "mode": stat.S_IMODE(path.stat().st_mode)}
        if path.is_file():
            row.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        entries[str(path.relative_to(repo))] = row
    digest = hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    _json(repo / "DEPLOYED_MANIFEST.json", {"schema_version": "oci3-shadow-autoslice-repo-manifest.v1",
                                          "commit": commit, "entries": entries, "tree_sha256": digest})


def _fixture(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSLICE_RUNTIME_VERSION_ROOT", raising=False)
    runtime = tmp_path / "runtime"
    old = runtime / "repo"
    _deployment(old, "2" * 40)
    source = runtime / "source.mp4"
    source.write_bytes(b"original media")
    handle = prepare_delivery(runtime_root=runtime, lane="talk", candidate_id="retained-talk",
                              artifacts=[DeliveryArtifact("video", source, runtime / "delivery/talk.mp4", _sha(source))])
    current = runtime / "versions" / ("3" * 40) / "repo"
    _deployment(current, "3" * 40)
    (runtime / "current").mkdir()
    (runtime / "current/repo").symlink_to(current)
    return runtime, old, current, handle


def test_strict_legacy_tree_is_reviewable_but_not_current_materialization(tmp_path, monkeypatch):
    runtime, old, current, handle = _fixture(tmp_path, monkeypatch)
    watched = [p for root in [old, current, handle.manifest_path.parent]
               for p in root.rglob("*") if p.is_file()]
    before = {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in watched}
    with exclusive_runner_commit(runtime) as lease:
        reviewed = historical_prepared_review(handle, lease)
        assert reviewed.document["deployed_authority"]["commit"] == "2" * 40
        assert reviewed.document["lane"] == "talk" and len(reviewed.checked) == 1
        with pytest.raises(ProducerDeliveryTransactionError, match="authority preimage"):
            preflight_prepared_delivery(handle=handle, lease=lease)
    assert before == {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in watched}
    assert not (runtime / "versions" / ("2" * 40)).exists()


def test_only_complete_current_package_review_can_replay_evolved_policy(tmp_path, monkeypatch):
    runtime, old, current, handle = _fixture(tmp_path, monkeypatch)
    shutil.rmtree(current)
    _deployment(current, "3" * 40, policy=b"new current policy for independent audit")
    with exclusive_runner_commit(runtime) as lease:
        with pytest.raises(ProducerDeliveryTransactionError, match="authority assets changed"):
            historical_prepared_review(handle, lease)
        reviewed = historical_prepared_review(handle, lease, current_package_audit=True)
        assert reviewed.document["deployed_authority"]["commit"] == "2" * 40
        with pytest.raises(ProducerDeliveryTransactionError, match="authority preimage"):
            preflight_prepared_delivery(handle=handle, lease=lease)
        (current / "assets/policy.txt").write_bytes(b"unsealed current policy")
        with pytest.raises(ProducerDeliveryTransactionError, match="asset bytes drift"):
            historical_prepared_review(handle, lease, current_package_audit=True)


@pytest.mark.parametrize("damage", ["commit", "seal", "old_asset", "current_asset",
                                  "deployment_manifest", "present_broken_version",
                                  "legacy_symlink", "unmanifested_code"])
def test_legacy_review_never_borrows_a_mismatched_or_corrupt_tree(tmp_path, monkeypatch, damage):
    runtime, old, current, handle = _fixture(tmp_path, monkeypatch)
    if damage == "commit":
        (old / "DEPLOYED_COMMIT").write_text("4" * 40 + "\n")
    elif damage == "seal":
        (old / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text("{}")
    elif damage in {"old_asset", "current_asset"}:
        ((old if damage == "old_asset" else current) / "assets/policy.txt").write_text("wrong asset")
    elif damage == "deployment_manifest":
        manifest = json.loads((old / "DEPLOYED_MANIFEST.json").read_bytes())
        manifest["tree_sha256"] = "f" * 64
        _json(old / "DEPLOYED_MANIFEST.json", manifest)
    elif damage == "present_broken_version":
        (runtime / "versions" / ("2" * 40) / "repo").mkdir(parents=True)
    elif damage == "legacy_symlink":
        moved = runtime / "old-repo"
        old.rename(moved)
        old.symlink_to(moved)
    else:
        (old / "src/unmanifested.py").write_text("pass\n")
    with exclusive_runner_commit(runtime) as lease:
        with pytest.raises(ProducerDeliveryTransactionError):
            historical_prepared_review(handle, lease, current_package_audit=True)



def _worker_fixture(tmp_path, monkeypatch):
    from src.autoslice.overflow_release import seal
    runtime, old, current, handle = _fixture(tmp_path, monkeypatch)
    release = seal(old)
    worker_repo = runtime / "versions" / release / "repo"
    worker_repo.parent.mkdir()
    old.rename(worker_repo)
    old.symlink_to(current)
    return runtime, worker_repo, current, handle


def test_retained_worker_release_is_review_only_without_a_commit_alias(tmp_path, monkeypatch):
    runtime, worker_repo, current, handle = _worker_fixture(tmp_path, monkeypatch)
    with exclusive_runner_commit(runtime) as lease:
        result = historical_prepared_review(handle, lease)
        assert result.document["deployed_authority"]["commit"] == "2" * 40
        with pytest.raises(ProducerDeliveryTransactionError, match="deployed authority preimage"):
            preflight_prepared_delivery(handle=handle, lease=lease)
    assert not (runtime / "versions" / ("2" * 40)).exists()
    assert (runtime / "repo").is_symlink()
    assert worker_repo.is_dir()


@pytest.mark.parametrize("damage", ["code", "old_asset", "current_asset", "marker",
                                    "duplicate", "present_broken_version", "symlink"])
def test_retained_worker_release_rejects_drift_or_ambiguous_locations(tmp_path, monkeypatch, damage):
    runtime, worker_repo, current, handle = _worker_fixture(tmp_path, monkeypatch)
    if damage == "code":
        (worker_repo / "src/unmanifested.py").write_text("pass")
    elif damage in {"old_asset", "current_asset"}:
        ((worker_repo if damage == "old_asset" else current) / "assets/policy.txt").write_bytes(b"drift")
    elif damage == "marker":
        (worker_repo / "OVERFLOW_RELEASE_SHA256").write_text("f" * 64)
    elif damage == "duplicate":
        shutil.copytree(worker_repo.parent, runtime / "versions" / ("f" * 64))
    elif damage == "present_broken_version":
        (runtime / "versions" / ("2" * 40) / "repo").mkdir(parents=True)
    else:
        moved = runtime / "moved-worker-repo"
        worker_repo.rename(moved)
        worker_repo.symlink_to(moved)
    with exclusive_runner_commit(runtime) as lease:
        with pytest.raises(ProducerDeliveryTransactionError):
            historical_prepared_review(handle, lease)
