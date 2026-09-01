from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact,
    ProducerDeliveryTransactionError,
    commit_prepared_delivery,
    deployment_authority_binding,
    load_prepared_delivery,
    prepare_delivery,
)
from src.autoslice.producer_delivery_prepare import talk_delivery_summary
from src.autoslice.producer_prepare_result import (
    prepared_song_result,
    prepared_talk_result,
)
from src.autoslice.song_lane import song_status
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice.repository_asset_authority import _canonical_sha256


def _seal(root: Path, commit: str = "a" * 40) -> None:
    repo = root / "repo"
    repo.mkdir(exist_ok=True)
    body = {
        "schema_version": "deployed-authority-manifest.v1",
        "deployed_commit": commit,
        "entries": {"docs/pipeline/80-package-delivery.md": {"bytes": 1, "sha256": "sha256:" + "b" * 64}},
    }
    manifest = dict(body)
    manifest["manifest_sha256"] = _canonical_sha256(body)
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n", encoding="utf-8")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")


def _prepared(tmp_path: Path):
    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    source = root / "out" / "candidate" / "video.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"candidate-private-video")
    delivery_root = root / "lidousha"
    delivery_root.mkdir()
    target = delivery_root / "2026-08-22" / "hook__cid-1.mp4"
    prepared = prepare_delivery(
        runtime_root=root,
        lane="talk",
        candidate_id="cid-1",
        artifacts=[DeliveryArtifact("video", source, target, "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest())],
    )
    return root, source, target, prepared


def test_prepare_does_not_create_public_date_and_preimage_drift_writes_no_journal(tmp_path: Path):
    root, source, target, prepared = _prepared(tmp_path)
    assert not target.parent.exists()
    source.write_bytes(b"drift")
    with exclusive_runner_commit(root) as lease:
        with pytest.raises(ProducerDeliveryTransactionError, match="source preimage drifts"):
            commit_prepared_delivery(handle=prepared, lease=lease)
    assert not target.exists()
    assert not (root / ".delivery-journal").exists()


def test_deployed_authority_drift_writes_no_journal_or_target(tmp_path: Path):
    root, _source, target, prepared = _prepared(tmp_path)
    _seal(root, "c" * 40)
    with exclusive_runner_commit(root) as lease:
        with pytest.raises(ProducerDeliveryTransactionError, match="deployed authority preimage drifts"):
            commit_prepared_delivery(handle=prepared, lease=lease)
    assert not target.exists()
    assert not (root / ".delivery-journal").exists()


def test_crash_after_owned_rename_replays_only_the_journal_owned_inode(tmp_path: Path, monkeypatch):
    root, _source, target, prepared = _prepared(tmp_path)
    import src.autoslice.producer_delivery_transaction as transaction

    original = transaction._write_replace
    calls = 0

    def crash_after_rename(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("crash canary")
        return original(*args, **kwargs)

    monkeypatch.setattr(transaction, "_write_replace", crash_after_rename)
    with exclusive_runner_commit(root) as lease:
        with pytest.raises(RuntimeError, match="crash canary"):
            commit_prepared_delivery(handle=prepared, lease=lease)
    assert target.exists()
    monkeypatch.setattr(transaction, "_write_replace", original)
    with exclusive_runner_commit(root) as lease:
        receipt = commit_prepared_delivery(handle=prepared, lease=lease)
    assert receipt["artifacts"] == [{"role": "video", "path": str(target), "sha256": "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()}]


def test_same_bytes_unowned_target_is_not_accepted_without_its_journal(tmp_path: Path):
    root, _source, target, prepared = _prepared(tmp_path)
    target.parent.mkdir()
    target.write_bytes(b"candidate-private-video")
    with exclusive_runner_commit(root) as lease:
        with pytest.raises(ProducerDeliveryTransactionError, match="delivery target is no longer absent"):
            commit_prepared_delivery(handle=prepared, lease=lease)
    assert not (root / ".delivery-journal").exists()


def test_talk_summary_appends_suffix_without_replacing_a_dotted_hook(tmp_path: Path):
    base = tmp_path / "delivery" / "a.b__cid-1"
    summary = talk_delivery_summary(
        candidate_id="cid-1", final_end=1, record={"duration_ms": 1},
        audit={"closure_sentence": "ok", "verdict": "PASS"}, timing_qa={},
        staging={}, delivery=base, speaker_review_srt=None, speaker_ass=None,
        speaker_manifest=None, speaker_guess=type("Guess", (), {"summary_digest": staticmethod(lambda _value: "OFF")}),
        subtitle_regression_audit=None, redelivery_baseline_audit=None,
        talk_filler_audit_path=None,
    )
    assert summary["delivery"] == str(base) + ".mp4"
    assert summary["subtitle"] == str(base) + ".srt"


def test_same_path_and_bytes_with_replaced_source_inode_gets_a_new_handle(tmp_path: Path):
    root, source, _target, original = _prepared(tmp_path)
    payload = source.read_bytes()
    source_inode = source.stat().st_ino
    replacement = source.with_name(f".{source.name}.replacement")
    replacement.write_bytes(payload)
    replacement.chmod(source.stat().st_mode & 0o777)
    assert replacement.stat().st_ino != source_inode
    os.replace(replacement, source)
    replacement = prepare_delivery(
        runtime_root=root,
        lane="talk",
        candidate_id="cid-1",
        artifacts=[DeliveryArtifact("video", source, root / "lidousha" / "2026-08-22" / "hook__cid-1.mp4", "sha256:" + hashlib.sha256(payload).hexdigest())],
    )
    assert replacement.manifest_path != original.manifest_path


def test_target_parent_symlink_before_commit_writes_no_journal_or_target(tmp_path: Path):
    root, _source, target, prepared = _prepared(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target.parent.symlink_to(outside, target_is_directory=True)
    with exclusive_runner_commit(root) as lease:
        with pytest.raises(ProducerDeliveryTransactionError, match="target parent is unsafe"):
            commit_prepared_delivery(handle=prepared, lease=lease)
    assert not target.exists()
    assert not (root / ".delivery-journal").exists()


def test_replaced_same_bytes_staged_inode_writes_no_journal_or_target(tmp_path: Path):
    root, _source, target, prepared = _prepared(tmp_path)
    document = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    staged = Path(document["artifacts"][0]["staged_path"])
    payload = staged.read_bytes()
    staged_inode = staged.stat().st_ino
    replacement = staged.with_name(f".{staged.name}.replacement")
    replacement.write_bytes(payload)
    replacement.chmod(0o600)
    assert replacement.stat().st_ino != staged_inode
    os.replace(replacement, staged)
    with exclusive_runner_commit(root) as lease:
        with pytest.raises(ProducerDeliveryTransactionError, match="staged preimage drifts"):
            commit_prepared_delivery(handle=prepared, lease=lease)
    assert not target.exists()
    assert not (root / ".delivery-journal").exists()


def test_prepare_rejects_source_with_symlink_ancestor(tmp_path: Path):
    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    real = root / "real"
    real.mkdir()
    (real / "video.mp4").write_bytes(b"bytes")
    (root / "linked").symlink_to(real, target_is_directory=True)
    (root / "lidousha").mkdir()
    with pytest.raises(ProducerDeliveryTransactionError, match="ancestor unsafe"):
        prepare_delivery(
            runtime_root=root,
            lane="talk",
            candidate_id="cid-1",
            artifacts=[DeliveryArtifact("video", root / "linked" / "video.mp4", root / "lidousha" / "2026-08-22" / "hook__cid-1.mp4", "sha256:" + hashlib.sha256(b"bytes").hexdigest())],
        )


def test_prepared_manifest_rejects_recomputed_wrong_entry_type(tmp_path: Path):
    root, _source, _target, prepared = _prepared(tmp_path)
    document = json.loads(prepared.manifest_path.read_text(encoding="utf-8"))
    document["artifacts"][0]["source_inode"] = "not-an-inode"
    unsigned = dict(document)
    unsigned.pop("prepared_sha256")
    unsigned_bytes = (
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    document["prepared_sha256"] = "sha256:" + hashlib.sha256(unsigned_bytes).hexdigest()
    prepared.manifest_path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(ProducerDeliveryTransactionError, match="artifact types drift"):
        load_prepared_delivery(runtime_root=root, manifest_path=prepared.manifest_path)


def test_prepared_projection_is_not_a_delivery_or_review_ready_state():
    talk = prepared_talk_result({"rc": 0, "summary": {"delivery": "/not-installed.mp4"}})
    song = prepared_song_result(
        {"rc": 0},
        {"prepared_delivery": {"upload_enabled": False}, "intended_delivery": {"path": "/not-installed.mp4"}},
        prepare_only=True,
    )
    assert talk["status"] == song["status"] == "delivery_prepared_no_target"
    assert "delivered" not in talk and "delivered" not in song
    assert "summary" not in talk and talk["prepared_summary"]["delivery"] == "/not-installed.mp4"
    assert song_status(0, bool(song.get("delivered"))) == "blocked"


def test_journal_rejects_unknown_schema_key_even_with_recomputed_digest(tmp_path: Path):
    root, _source, _target, prepared = _prepared(tmp_path)
    import src.autoslice.producer_delivery_transaction as transaction

    document = transaction._read_document(prepared)
    journal = transaction._journal_payload(handle=prepared, artifacts=document["artifacts"])
    journal["untrusted"] = "injected"
    unsigned = dict(journal)
    unsigned.pop("journal_sha256")
    journal["journal_sha256"] = "sha256:" + hashlib.sha256(
        transaction._canonical_bytes(unsigned)
    ).hexdigest()
    journal_root = transaction._private_directory(root, ".delivery-journal")
    path = journal_root / f"talk-cid-1-{prepared.prepared_sha256}.json"
    path.write_bytes(transaction._canonical_bytes(journal))
    path.chmod(0o600)
    with pytest.raises(ProducerDeliveryTransactionError, match="journal authority drifts"):
        transaction._read_journal(path, handle=prepared, artifacts=document["artifacts"])


def test_journal_rejects_nonprivate_mode_before_replay(tmp_path: Path):
    root, _source, _target, prepared = _prepared(tmp_path)
    import src.autoslice.producer_delivery_transaction as transaction

    document = transaction._read_document(prepared)
    journal = transaction._journal_payload(handle=prepared, artifacts=document["artifacts"])
    journal_root = transaction._private_directory(root, ".delivery-journal")
    path = journal_root / f"talk-cid-1-{prepared.prepared_sha256}.json"
    path.write_bytes(transaction._canonical_bytes(journal))
    path.chmod(0o644)
    with pytest.raises(ProducerDeliveryTransactionError, match="journal mode is unsafe"):
        transaction._read_journal(path, handle=prepared, artifacts=document["artifacts"])


def test_journal_rejects_nonprefix_checkpoint_even_with_recomputed_digest(tmp_path: Path):
    root, _source, _target, prepared = _prepared(tmp_path)
    import src.autoslice.producer_delivery_transaction as transaction

    document = transaction._read_document(prepared)
    journal = transaction._journal_payload(
        handle=prepared,
        artifacts=document["artifacts"],
        status="INSTALLING",
        installed_roles=["unowned-role"],
    )
    journal_root = transaction._private_directory(root, ".delivery-journal")
    path = journal_root / f"talk-cid-1-{prepared.prepared_sha256}.json"
    path.write_bytes(transaction._canonical_bytes(journal))
    path.chmod(0o600)
    with pytest.raises(ProducerDeliveryTransactionError, match="journal checkpoints drift"):
        transaction._read_journal(path, handle=prepared, artifacts=document["artifacts"])


def test_deployed_authority_rejects_invalid_entry_even_with_recomputed_manifest_hash(tmp_path: Path):
    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    manifest_path = root / "repo" / "DEPLOYED_AUTHORITY_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"]["docs/pipeline/80-package-delivery.md"]["bytes"] = True
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256")
    manifest["manifest_sha256"] = _canonical_sha256(unsigned)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ProducerDeliveryTransactionError, match="manifest entries are invalid"):
        deployment_authority_binding(root)
