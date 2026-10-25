"""Ordinary cleanup versus actual private-producer recovery, fixture-only."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice import producer_batch_transaction as batch
from src.autoslice import producer_delivery_transaction as delivery
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice.repository_asset_authority import _canonical_sha256

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
planner = importlib.import_module("cleanup_preflight_scan")
DATE, CID = "2026-10-13", "auto_100000_1_2"


def fixture(tmp_path, *, lane="talk", status="failed", checkpoint=False, optional_missing=False):
    root = tmp_path / "runtime"
    repo = root / "repo"
    repo.mkdir(parents=True)
    body = {"schema_version": "deployed-authority-manifest.v1", "deployed_commit": "a" * 40,
            "entries": {"docs/pipeline/80-package-delivery.md": {"bytes": 1, "sha256": "sha256:" + "b" * 64}}}
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(json.dumps({**body, "manifest_sha256": _canonical_sha256(body)}))
    (root / "state").mkdir()
    state_path = root / "state" / (DATE + ".json")
    state_path.write_text(json.dumps({"picks": [{"candidate_id": CID, "status": status}], "pending_talk": [], "pending_song": []}))
    out = root / "out" / DATE / CID
    out.mkdir(parents=True)
    source = out / "working.wav"
    source.write_bytes(b"source-original")
    unused = out / "padded_9.mp4"
    unused.write_bytes(b"unreferenced-rebuildable-intermediate")
    target = root / "delivery" / (CID + ".mp4")
    handle = delivery.prepare_delivery(runtime_root=root, lane=lane, candidate_id=CID,
        artifacts=[delivery.DeliveryArtifact("video", source, target, "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest())])
    result = {"status": "delivery_prepared_no_target", "prepared_delivery": {
        "manifest_path": str(handle.manifest_path), "prepared_sha256": "sha256:" + handle.prepared_sha256}}
    case = {"root": root, "source": source, "unused": unused, "target": target, "handle": handle,
            "state_path": state_path, "binding": {"date": DATE, "lane": lane, "candidate_id": CID}, "result": result}
    if checkpoint:
        if not hasattr(batch, "checkpoint_prepared_result"):
            pytest.skip("completion consumer is supplied by the integrating C/A patch")
        dependency = out / "piece_0.mp4"
        dependency.write_bytes(b"checkpoint-only-source")
        inputs = {dependency: True}
        if optional_missing:
            case["optional"] = out / "optional-text.txt"
            inputs[case["optional"]] = False
        batch.checkpoint_prepared_result(entry=batch.PreparedBatchEntry(lane, CID, handle),
            binding=case["binding"], result=result,
            source_dependencies=batch.bind_prepared_result_inputs(inputs))
        case.update(dependency=dependency, checkpoint=next(handle.manifest_path.parent.glob("dispatch-result-*.json")))
    return case


def plan(case, monkeypatch, *, plan_path=None):
    root = case["root"]
    plan_path = plan_path or root.parent / "plan.json"
    monkeypatch.setattr(planner, "quiet_window", lambda _: [])
    monkeypatch.setattr(sys, "argv", ["cleanup_preflight_scan.py", "--base", str(root), "--json", str(plan_path)])
    rc = planner.main()
    return rc, json.loads(plan_path.read_text()) if rc == 0 else None


@pytest.mark.parametrize("lane", ["talk", "song"])
@pytest.mark.parametrize("status", ["failed", "blocked"])
def test_terminal_without_pending_still_protects_real_prepared_source(tmp_path, monkeypatch, lane, status):
    case = fixture(tmp_path, lane=lane, status=status)
    with exclusive_runner_commit(case["root"]) as lease:
        delivery.preflight_prepared_delivery(handle=case["handle"], lease=lease)
    rc, planned = plan(case, monkeypatch)
    assert rc == 0 and [row["path"] for row in planned] == [str(case["unused"])]
    with exclusive_runner_commit(case["root"]) as lease:
        delivery.commit_prepared_delivery(handle=case["handle"], lease=lease)
    assert case["target"].read_bytes() == b"source-original"


def test_private_copy_cannot_replace_missing_original_in_real_consumer(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    staged = Path(delivery._read_document(case["handle"])["artifacts"][0]["staged_path"])
    case["source"].unlink()  # Only this newly created fixture is deleted.
    assert staged.read_bytes() == b"source-original"
    with exclusive_runner_commit(case["root"]) as lease:
        with pytest.raises(delivery.ProducerDeliveryTransactionError):
            delivery.preflight_prepared_delivery(handle=case["handle"], lease=lease)
    assert plan(case, monkeypatch)[0] == 2


@pytest.mark.parametrize("lane", ["talk", "song"])
def test_checkpoint_only_input_is_protected_and_real_replay_survives(tmp_path, monkeypatch, lane):
    case = fixture(tmp_path, lane=lane, checkpoint=True)
    assert batch.load_prepared_result_checkpoint(runtime_root=case["root"], lane=lane, candidate_id=CID, binding=case["binding"]) == case["result"]
    rc, planned = plan(case, monkeypatch)
    assert rc == 0 and [row["path"] for row in planned] == [str(case["unused"])]
    assert batch.load_prepared_result_checkpoint(runtime_root=case["root"], lane=lane, candidate_id=CID, binding=case["binding"]) == case["result"]
    case["dependency"].unlink()
    with pytest.raises(batch.ProducerBatchTransactionError, match="dependencies drift"):
        batch.load_prepared_result_checkpoint(runtime_root=case["root"], lane=lane, candidate_id=CID, binding=case["binding"])
    assert plan(case, monkeypatch)[0] == 2


def test_v1_checkpoint_remains_protected_by_current_consumer(tmp_path, monkeypatch):
    case = fixture(tmp_path, checkpoint=True)
    body = json.loads(case["checkpoint"].read_text())
    body["schema_version"] = "producer-result-checkpoint.v1"
    body.pop("consumer_authority_manifest_b64", None)
    body.pop("deployed_authority_manifest_b64", None)
    body.pop("reprepared_from", None)
    body.pop("checkpoint_sha256")
    body["checkpoint_sha256"] = "sha256:" + hashlib.sha256(delivery._canonical_bytes(body)).hexdigest()
    case["checkpoint"].write_bytes(delivery._canonical_bytes(body))
    assert batch.load_prepared_result_checkpoint(runtime_root=case["root"], lane="talk", candidate_id=CID, binding=case["binding"])
    rc, planned = plan(case, monkeypatch)
    assert rc == 0 and [row["path"] for row in planned] == [str(case["unused"])]


def test_v2_scan_only_uses_readonly_leaf_and_blocks_stale_leaf_without_reprepare(tmp_path, monkeypatch):
    case = fixture(tmp_path, checkpoint=True)
    if not hasattr(batch, "_load_result_checkpoint"):
        pytest.skip("v2 read-only leaf is supplied by A's equivalent-deploy patch")
    monkeypatch.setattr(batch, "load_prepared_result_checkpoint", lambda **_: pytest.fail("cleanup called mutating loader"))
    monkeypatch.setattr(batch, "_reprepare_equivalent_result", lambda *_, **__: pytest.fail("cleanup reprepared a handle"))
    before = {str(p): p.read_bytes() for p in case["root"].rglob("*") if p.is_file()}
    assert plan(case, monkeypatch)[0] == 0
    assert {str(p): p.read_bytes() for p in case["root"].rglob("*") if p.is_file()} == before
    (case["root"] / "repo" / "DEPLOYED_COMMIT").write_text("c" * 40 + "\n")
    assert plan(case, monkeypatch)[0] == 2


def test_valid_reprepare_chain_is_protected_then_released_by_journaled_leaf(tmp_path, monkeypatch):
    case = fixture(tmp_path, checkpoint=True)
    if getattr(batch, "PRODUCER_RESULT_CHECKPOINT_SCHEMA", None) != "producer-result-checkpoint.v2":
        pytest.skip("equivalent-deploy lineage is supplied by A's patch")
    old_body = json.loads(case["checkpoint"].read_text())
    repo = case["root"] / "repo"
    manifest = json.loads((repo / "DEPLOYED_AUTHORITY_MANIFEST.json").read_text())
    manifest["deployed_commit"] = "c" * 40
    manifest.pop("manifest_sha256")
    manifest["manifest_sha256"] = _canonical_sha256(manifest)
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(json.dumps(manifest))
    (repo / "DEPLOYED_COMMIT").write_text("c" * 40 + "\n")
    fresh = delivery.prepare_delivery(runtime_root=case["root"], lane="talk", candidate_id=CID,
        artifacts=[delivery.DeliveryArtifact("video", case["source"], case["target"], "sha256:" + hashlib.sha256(case["source"].read_bytes()).hexdigest())])
    fresh_result = {"status": "delivery_prepared_no_target", "prepared_delivery": {
        "manifest_path": str(fresh.manifest_path), "prepared_sha256": "sha256:" + fresh.prepared_sha256}}
    batch.checkpoint_prepared_result(entry=batch.PreparedBatchEntry("talk", CID, fresh), binding=case["binding"],
        result=fresh_result, source_dependencies=old_body["source_dependencies"], reprepared_from={
            "manifest_path": str(case["handle"].manifest_path), "prepared_sha256": "sha256:" + case["handle"].prepared_sha256,
            "checkpoint_sha256": old_body["checkpoint_sha256"]})
    assert batch.load_prepared_result_checkpoint(runtime_root=case["root"], lane="talk", candidate_id=CID, binding=case["binding"]) == fresh_result
    assert plan(case, monkeypatch)[0] == 0
    with exclusive_runner_commit(case["root"]) as lease:
        batch.commit_prepared_prefix(runtime_root=case["root"], date=DATE, state_path=case["state_path"],
            state_before=case["state_path"].read_bytes(), after_state={"picks": [{"candidate_id": CID, "status": "blocked"}]},
            entries=[batch.PreparedBatchEntry("talk", CID, fresh)], lease=lease)
    rc, planned = plan(case, monkeypatch)
    assert rc == 0 and {row["path"] for row in planned} == {str(case[k]) for k in ("source", "unused", "dependency")}


@pytest.mark.parametrize("kind", ["fork", "predecessor_drift", "missing_predecessor"])
def test_invalid_checkpoint_lineage_cannot_retire_originals(tmp_path, monkeypatch, kind):
    case = fixture(tmp_path, checkpoint=True)
    if getattr(batch, "PRODUCER_RESULT_CHECKPOINT_SCHEMA", None) != "producer-result-checkpoint.v2":
        pytest.skip("lineage is supplied by A's equivalent-deploy patch")
    body = json.loads(case["checkpoint"].read_text())
    previous = {"manifest_path": str(case["handle"].manifest_path),
                "prepared_sha256": "sha256:" + case["handle"].prepared_sha256,
                "checkpoint_sha256": body["checkpoint_sha256"]}
    if kind == "predecessor_drift":
        previous["checkpoint_sha256"] = "sha256:" + "d" * 64
    elif kind == "missing_predecessor":
        previous["manifest_path"] = str(case["handle"].manifest_path.parent.parent / ("d" * 64) / "prepared.json")
    for index in range(2 if kind == "fork" else 1):
        fresh = delivery.prepare_delivery(runtime_root=case["root"], lane="talk", candidate_id=CID,
            artifacts=[delivery.DeliveryArtifact("video", case["source"], case["target"].with_name(f"successor-{index}.mp4"),
                "sha256:" + hashlib.sha256(case["source"].read_bytes()).hexdigest())])
        result = {"status": "delivery_prepared_no_target", "prepared_delivery": {
            "manifest_path": str(fresh.manifest_path), "prepared_sha256": "sha256:" + fresh.prepared_sha256}}
        batch.checkpoint_prepared_result(entry=batch.PreparedBatchEntry("talk", CID, fresh), binding=case["binding"],
            result=result, source_dependencies=body["source_dependencies"], reprepared_from=previous)
    assert plan(case, monkeypatch)[0] == 2
    assert case["source"].read_bytes() == b"source-original"


@pytest.mark.parametrize("appears_during_scan", [False, True])
def test_consumer_declared_optional_absence_is_preserved_and_observed(tmp_path, monkeypatch, appears_during_scan):
    case = fixture(tmp_path, checkpoint=True, optional_missing=True)
    if appears_during_scan:
        if not hasattr(batch, "_load_result_checkpoint"):
            pytest.skip("read-only leaf supplied by A")
        original = batch._load_result_checkpoint
        def observe_then_create(*args, **kwargs):
            result = original(*args, **kwargs)
            case["optional"].write_text("new input after read")
            return result
        monkeypatch.setattr(batch, "_load_result_checkpoint", observe_then_create)
    assert plan(case, monkeypatch)[0] == (2 if appears_during_scan else 0)


@pytest.mark.parametrize("failure", ["invalid_json", "tampered", "unreadable", "symlink", "missing_handle", "source_drift", "dependency_drift"])
def test_bad_checkpoint_blocks_before_planning_and_does_not_refresh_old_plan(tmp_path, monkeypatch, failure):
    case = fixture(tmp_path, checkpoint=True)
    path = case["checkpoint"]
    if failure == "invalid_json":
        path.write_text("{")
    elif failure == "tampered":
        body = json.loads(path.read_text())
        body["source_dependencies"] = {}
        path.write_text(json.dumps(body))
    elif failure == "unreadable":
        real_open = os.open
        def denied(name, *args, **kwargs):
            if os.fspath(name) == str(path):
                raise PermissionError("synthetic private checkpoint read denial")
            return real_open(name, *args, **kwargs)
        monkeypatch.setattr(os, "open", denied)
    elif failure == "symlink":
        retained = path.with_suffix(".retained")
        path.rename(retained)
        path.symlink_to(retained)
    elif failure == "missing_handle":
        case["handle"].manifest_path.unlink()
    elif failure == "source_drift":
        case["source"].write_bytes(b"different-bytes")
    else:
        case["dependency"].write_bytes(b"different-checkpoint-source")
    old_plan = tmp_path / "old-plan.json"
    old_plan.write_bytes(b"old-plan-not-refreshed")
    monkeypatch.setattr(planner, "candidate_states", lambda _: pytest.fail("bad checkpoint reached candidate planning"))
    assert plan(case, monkeypatch, plan_path=old_plan)[0] == 2
    assert old_plan.read_bytes() == b"old-plan-not-refreshed"


@pytest.mark.parametrize("store", sorted(planner.RECOVERY_STORES))
def test_store_symlink_is_not_optional_absence(tmp_path, monkeypatch, store):
    case = fixture(tmp_path)
    path = case["root"] / store
    outside = tmp_path / "outside"
    outside.mkdir()
    if path.exists():
        path.rename(tmp_path / "retained-store")
    path.symlink_to(outside, target_is_directory=True)
    assert plan(case, monkeypatch)[0] == 2


def test_referenced_missing_checkpoint_is_not_an_empty_optional_store(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    missing = case["handle"].manifest_path.parent / ("dispatch-result-" + "a" * 64 + ".json")
    (case["root"] / "state" / "checkpoint-pointer.json").write_text(json.dumps({"checkpoint": str(missing)}))
    assert plan(case, monkeypatch)[0] == 2


@pytest.mark.parametrize("kind", ["manifest_drift", "source_drift"])
def test_mutation_after_consumer_read_is_in_final_scan_closure(tmp_path, monkeypatch, kind):
    case = fixture(tmp_path)
    checked = delivery._checked_artifacts
    def drift(*args, **kwargs):
        result = checked(*args, **kwargs)
        target = case["source"] if kind == "source_drift" else case["handle"].manifest_path
        target.write_bytes(target.read_bytes() + b" ")
        return result
    monkeypatch.setattr(delivery, "_checked_artifacts", drift)
    assert plan(case, monkeypatch)[0] == 2


@pytest.mark.parametrize("journal_kind", ["delivery", "batch"])
def test_valid_completed_history_releases_source_and_does_not_need_it_again(tmp_path, monkeypatch, journal_kind):
    case = fixture(tmp_path)
    with exclusive_runner_commit(case["root"]) as lease:
        if journal_kind == "delivery":
            delivery.commit_prepared_delivery(handle=case["handle"], lease=lease)
        else:
            batch.commit_prepared_prefix(runtime_root=case["root"], date=DATE, state_path=case["state_path"],
                state_before=case["state_path"].read_bytes(), after_state={"picks": [{"candidate_id": CID, "status": "blocked"}]},
                entries=[batch.PreparedBatchEntry("talk", CID, case["handle"])], lease=lease)
    rc, planned = plan(case, monkeypatch)
    assert rc == 0 and {row["path"] for row in planned} == {str(case["source"]), str(case["unused"])}
    case["source"].unlink()
    assert plan(case, monkeypatch)[0] == 0
    with exclusive_runner_commit(case["root"]) as lease:
        assert batch.resume_pending_batch(runtime_root=case["root"], date=DATE, lease=lease) is None
    assert case["target"].read_bytes() == b"source-original"


def test_completed_checkpoint_is_history_after_batch_commit(tmp_path, monkeypatch):
    case = fixture(tmp_path, checkpoint=True)
    with exclusive_runner_commit(case["root"]) as lease:
        batch.commit_prepared_prefix(runtime_root=case["root"], date=DATE, state_path=case["state_path"],
            state_before=case["state_path"].read_bytes(), after_state={"picks": [{"candidate_id": CID, "status": "failed"}]},
            entries=[batch.PreparedBatchEntry("talk", CID, case["handle"])], lease=lease)
    rc, planned = plan(case, monkeypatch)
    assert rc == 0 and {row["path"] for row in planned} == {str(case[k]) for k in ("source", "unused", "dependency")}
    # This loader no longer resumes consumed staged bytes. The ordinary
    # runner's journal lane has finished and correctly returns no pending work.
    with pytest.raises(delivery.ProducerDeliveryTransactionError):
        batch.load_prepared_result_checkpoint(runtime_root=case["root"], lane="talk", candidate_id=CID, binding=case["binding"])
    with exclusive_runner_commit(case["root"]) as lease:
        assert batch.resume_pending_batch(runtime_root=case["root"], date=DATE, lease=lease) is None


@pytest.mark.parametrize("failure", ["tampered_journal", "missing_handle", "foreign_target"])
def test_status_alone_cannot_release_prepared_inputs(tmp_path, monkeypatch, failure):
    case = fixture(tmp_path)
    with exclusive_runner_commit(case["root"]) as lease:
        receipt = delivery.commit_prepared_delivery(handle=case["handle"], lease=lease)
    journal = Path(receipt["journal_path"])
    if failure == "tampered_journal":
        body = json.loads(journal.read_text())
        body["installed_roles"] = []
        journal.write_text(json.dumps(body))
    elif failure == "missing_handle":
        case["handle"].manifest_path.unlink()
    else:
        retained = case["target"].with_suffix(".retained")
        case["target"].rename(retained)
        case["target"].write_bytes(retained.read_bytes())
    assert plan(case, monkeypatch)[0] == 2


@pytest.mark.parametrize("checkpoint", [False, True])
def test_pending_batch_protects_sources_and_actual_resume_completes(tmp_path, monkeypatch, checkpoint):
    case = fixture(tmp_path, checkpoint=checkpoint)
    state_writer = batch.write_exact_state_under_lease
    def interrupted(*args, **kwargs):
        raise batch.RunnerStateWritebackError("fixture crash before state write")
    monkeypatch.setattr(batch, "write_exact_state_under_lease", interrupted)
    with exclusive_runner_commit(case["root"]) as lease:
        with pytest.raises(batch.ProducerBatchTransactionError, match="fixture crash before state write"):
            batch.commit_prepared_prefix(runtime_root=case["root"], date=DATE, state_path=case["state_path"],
                state_before=case["state_path"].read_bytes(), after_state={"picks": [{"candidate_id": CID, "status": "blocked"}]},
                entries=[batch.PreparedBatchEntry("talk", CID, case["handle"])], lease=lease)
    monkeypatch.setattr(batch, "write_exact_state_under_lease", state_writer)
    rc, planned = plan(case, monkeypatch)
    assert rc == 0 and str(case["source"]) not in {row["path"] for row in planned}
    with exclusive_runner_commit(case["root"]) as lease:
        assert batch.resume_pending_batch(runtime_root=case["root"], date=DATE, lease=lease)
    assert json.loads(case["state_path"].read_text())["picks"][0]["status"] == "blocked"


@pytest.mark.parametrize("authority", ["state", "recovery"])
def test_existing_state_and_recovery_references_still_win_over_completed_history(tmp_path, monkeypatch, authority):
    case = fixture(tmp_path)
    with exclusive_runner_commit(case["root"]) as lease:
        delivery.commit_prepared_delivery(handle=case["handle"], lease=lease)
    pointer = case["root"] / authority / "source-pointer.json"
    pointer.parent.mkdir(exist_ok=True)
    pointer.write_text(json.dumps({"source": str(case["source"])}))
    rc, planned = plan(case, monkeypatch)
    assert rc == 0 and [row["path"] for row in planned] == [str(case["unused"])]


def test_native_cli_protects_prepared_source_without_deleting(tmp_path):
    case = fixture(tmp_path)
    output = tmp_path / "native-plan.json"
    result = subprocess.run([sys.executable, str(ROOT / "scripts/cleanup_preflight_scan.py"),
        "--base", str(case["root"]), "--json", str(output), "--ignore-quiet-window"],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [row["path"] for row in json.loads(output.read_text())] == [str(case["unused"])]
    assert case["source"].read_bytes() == b"source-original" and not case["target"].exists()
