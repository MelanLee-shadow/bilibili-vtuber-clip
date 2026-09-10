from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact,
    ProducerDeliveryTransactionError,
    _read_document,
    commit_prepared_delivery,
    deployment_authority_binding,
    load_prepared_delivery,
    prepare_delivery,
)
from src.autoslice.producer_batch_projection import project_materialized_talk
from src.autoslice.producer_batch_transaction import (
    PreparedBatchEntry,
    commit_prepared_prefix,
)
from src.autoslice.producer_delivery_prepare import talk_delivery_summary
from src.autoslice.producer_prepare_result import (
    prepared_song_result,
    prepared_talk_result,
)
from src.autoslice.song_lane import song_status
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice.repository_asset_authority import _canonical_sha256
from src.autoslice.runner_state_writeback import state_bytes


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


def _talk_preparation_fixture(tmp_path: Path) -> dict[str, object]:
    """Build an eleven-artifact Talk prepare with ten stable old targets."""

    from src.autoslice import producer_delivery_prepare as prepare

    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    date = "2026-08-22"
    candidate_id = "cid-1"
    basename = "same.hook__cid-1"
    output_root = root / "out" / date
    output_root.mkdir(parents=True)
    source_payloads = {
        "video": b"new-video",
        "subtitle": b"new-subtitle",
        "uniform_host_ass": b"new-ass",
        "chat_authority": b"new-chat",
        "redelivery_baseline": b"baseline-redelivery",
        "subtitle_regression": b"new-regression",
        "filler_audit": b"new-filler",
        "clip_context": b"new-context",
        "publish": b"new-publish",
        "record": b"new-record",
        "cover": b"new-cover",
    }
    sources: dict[str, Path] = {}
    for role, payload in source_payloads.items():
        path = output_root / f"{role}.artifact"
        path.write_bytes(payload)
        sources[role] = path

    def sha256(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    record = {
        "duration_ms": 1,
        "clip_context_path": str(sources["clip_context"]),
        "burned_preview": {
            "status": "BURNED",
            "path": str(sources["video"]),
            "burned_sha256": sha256(sources["video"]),
            "ass_path": str(sources["uniform_host_ass"]),
            "ass_sha256": sha256(sources["uniform_host_ass"]),
        },
        "artifact_hashes": {
            "burned_video_sha256": sha256(sources["video"]),
            "ass_sha256": sha256(sources["uniform_host_ass"]),
        },
    }
    staging = {
        "cover_status": "AI_COVER_READY",
        "cover_path": str(sources["cover"]),
        "publish_json_path": str(sources["publish"]),
    }
    delivery_root = root / "repo" / "lidousha"
    delivery_root.mkdir(parents=True)
    suffixes = {
        "video": ".mp4",
        "subtitle": ".srt",
        "uniform_host_ass": ".final-sapphire72.ass",
        "chat_authority": ".chat-authority.json",
        "redelivery_baseline": ".redelivery-baseline.json",
        "subtitle_regression": ".subtitle-regression.json",
        "filler_audit": ".filler-audit.json",
        "clip_context": ".clip-context.json",
        "publish": ".publish.json",
        "record": ".record.json",
        "cover": ".cover.png",
    }
    old_targets = {
        role: delivery_root / date / f"{basename}{suffixes[role]}"
        for role in source_payloads
        if role != "cover"
    }
    for role, target in old_targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"old-{role}".encode("utf-8"))

    kwargs = {
        "spec": {"output_root": str(output_root), "date": date, "delivery_name": basename},
        "candidate_id": candidate_id,
        "record": record,
        "staging": staging,
        "record_path": sources["record"],
        "subtitle_path": sources["subtitle"],
        "speaker_review_srt": None,
        "speaker_ass": None,
        "speaker_manifest_path": None,
        "redelivery_baseline_audit_path": sources["redelivery_baseline"],
        "subtitle_regression_audit_path": sources["subtitle_regression"],
        "chat_authority_path": sources["chat_authority"],
        "talk_filler_audit_path": sources["filler_audit"],
        "text_manifest_path": None,
        "delivery_root": delivery_root,
    }
    handle = prepare.prepare_talk_delivery(**kwargs)
    return {
        "root": root,
        "date": date,
        "candidate_id": candidate_id,
        "basename": basename,
        "record": record,
        "staging": staging,
        "sources": sources,
        "old_targets": old_targets,
        "delivery_root": delivery_root,
        "kwargs": kwargs,
        "handle": handle,
    }


def _prepared_retry_prefix(handle) -> str:
    document = _read_document(handle)
    video = next(row for row in document["artifacts"] if row["role"] == "video")
    return Path(video["target_path"]).name.split("__", 1)[0]


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


@pytest.mark.parametrize("existing_suffix", [".mp4", ".cover.png"])
@pytest.mark.parametrize("existing_kind", ["file", "symlink", "directory"])
def test_talk_retry_preserves_old_delivery_and_projects_fresh_targets(tmp_path: Path, monkeypatch, existing_suffix: str, existing_kind: str):
    from src.autoslice import producer_delivery_prepare as prepare

    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    out = root / "out" / "2026-08-21"
    out.mkdir(parents=True)
    sources = {}
    for role in ("video", "subtitle", "record", "chat", "cover"):
        sources[role] = out / role
        sources[role].write_bytes(f"new-{role}".encode())
    monkeypatch.setattr(prepare, "_validated_burned_artifact", lambda _record: sources["video"])
    monkeypatch.setattr(prepare, "_validated_burned_ass_artifact", lambda _record: None)
    delivery = root / "lidousha" / "2026-08-21"
    delivery.mkdir(parents=True)
    basename = "a.b__cid-1"
    old = delivery / (basename + existing_suffix)
    if existing_kind == "symlink":
        old.symlink_to(sources["video"])
    elif existing_kind == "directory":
        old.mkdir()
    else:
        old.write_bytes(b"old-delivery")
    old_inode = old.lstat().st_ino
    kwargs = dict(
        spec={"output_root": str(out), "date": "2026-08-21", "delivery_name": basename},
        candidate_id="cid-1", record={}, staging={"cover_path": str(sources["cover"])},
        record_path=sources["record"], subtitle_path=sources["subtitle"],
        speaker_review_srt=None, speaker_ass=None, speaker_manifest_path=None,
        redelivery_baseline_audit_path=None, subtitle_regression_audit_path=None,
        chat_authority_path=sources["chat"], talk_filler_audit_path=None,
        text_manifest_path=None, delivery_root=root / "lidousha",
    )
    if existing_kind != "file":
        with pytest.raises(ProducerDeliveryTransactionError, match="delivery target is unsafe"):
            prepare.prepare_talk_delivery(**kwargs)
        assert old.lstat().st_ino == old_inode
        assert not (root / ".prepared-deliveries").exists()
        return
    handle = prepare.prepare_talk_delivery(**kwargs)
    assert prepare.prepare_talk_delivery(**kwargs) == handle
    summary = talk_delivery_summary(
        candidate_id="cid-1", final_end=1, record={"duration_ms": 1},
        audit={"closure_sentence": "ok", "verdict": "PASS"}, timing_qa={},
        staging={}, delivery=delivery / basename, speaker_review_srt=None, speaker_ass=None,
        speaker_manifest=None, speaker_guess=type("Guess", (), {"summary_digest": staticmethod(lambda _value: "OFF")}),
        subtitle_regression_audit=None, redelivery_baseline_audit=None,
        talk_filler_audit_path=None, prepared=handle,
    )
    video = Path(summary["delivery"])
    assert video.name.startswith("retry-") and video.name.endswith("__cid-1.mp4")
    assert summary["prepared_artifacts"]["video"]["path"] == str(video)
    assert summary["subtitle"] == str(video.with_suffix(".srt"))
    assert summary["prepared_artifacts"]["cover"]["path"] == str(video.with_suffix(".cover.png"))
    assert not video.exists()
    with exclusive_runner_commit(root) as lease:
        commit_prepared_delivery(handle=handle, lease=lease)
    assert video.read_bytes() == b"new-video"
    assert old.read_bytes() == b"old-delivery" and old.stat().st_ino == old_inode


def test_required_subtitle_content_changes_retry_prefix_but_same_bytes_new_inode_keeps_it(
    tmp_path: Path,
):
    fixture = _talk_preparation_fixture(tmp_path)
    handle_before = fixture["handle"]
    prefix_before = _prepared_retry_prefix(handle_before)
    old_snapshots = {
        role: (target.stat().st_ino, target.read_bytes())
        for role, target in fixture["old_targets"].items()
    }
    required = fixture["sources"]["subtitle"]
    required.write_bytes(b"changed-subtitle")

    from src.autoslice import producer_delivery_prepare as prepare

    handle_changed = prepare.prepare_talk_delivery(**fixture["kwargs"])
    prefix_changed = _prepared_retry_prefix(handle_changed)
    assert prefix_changed.startswith("retry-")
    assert prefix_changed != prefix_before
    changed_row = next(
        row for row in _read_document(handle_changed)["artifacts"]
        if row["role"] == "subtitle"
    )
    expected_changed_sha = "sha256:" + hashlib.sha256(required.read_bytes()).hexdigest()
    assert changed_row["source_sha256"] == expected_changed_sha
    assert changed_row["staged_sha256"] == expected_changed_sha

    changed_payload = required.read_bytes()
    previous_inode = required.stat().st_ino
    replacement = required.with_name(".subtitle.replacement")
    replacement.write_bytes(changed_payload)
    replacement.chmod(required.stat().st_mode & 0o777)
    assert replacement.stat().st_ino != previous_inode
    os.replace(replacement, required)
    assert required.stat().st_ino != previous_inode

    handle_replaced = prepare.prepare_talk_delivery(**fixture["kwargs"])
    assert _prepared_retry_prefix(handle_replaced) == prefix_changed
    assert handle_replaced.manifest_path != handle_changed.manifest_path
    replaced_row = next(
        row for row in _read_document(handle_replaced)["artifacts"]
        if row["role"] == "subtitle"
    )
    assert replaced_row["source_sha256"] == expected_changed_sha
    assert replaced_row["source_inode"] != changed_row["source_inode"]
    assert {
        role: (target.stat().st_ino, target.read_bytes())
        for role, target in fixture["old_targets"].items()
    } == old_snapshots


def test_talk_prepare_projection_batch_commit_roundtrip_keeps_old_targets(
    tmp_path: Path,
):
    fixture = _talk_preparation_fixture(tmp_path)
    handle = fixture["handle"]
    document = _read_document(handle)
    sealed = {
        row["role"]: {"path": row["target_path"], "sha256": row["staged_sha256"]}
        for row in document["artifacts"]
    }
    assert len(sealed) == 11
    assert len(fixture["old_targets"]) == 10

    summary = talk_delivery_summary(
        candidate_id=fixture["candidate_id"], final_end=1,
        record=fixture["record"], audit={"closure_sentence": "ok", "verdict": "PASS"},
        timing_qa={}, staging=fixture["staging"],
        delivery=fixture["delivery_root"] / fixture["date"] / fixture["basename"],
        speaker_review_srt=None, speaker_ass=None, speaker_manifest=None,
        speaker_guess=type("Guess", (), {"summary_digest": staticmethod(lambda _value: "OFF")}),
        subtitle_regression_audit={}, redelivery_baseline_audit={},
        talk_filler_audit_path=fixture["sources"]["filler_audit"], prepared=handle,
    )
    base_result = {
        "candidate_id": fixture["candidate_id"],
        "rc": 0,
        "status": "delivery_prepared_no_target",
        "cover_status": "AI_COVER_READY",
        "video_sha256": sealed["video"]["sha256"],
        "subtitle_sha256": sealed["subtitle"]["sha256"],
        "cover_sha256": sealed["cover"]["sha256"],
        "prepared_summary": summary,
    }

    wrong_path = deepcopy(base_result)
    wrong_path["prepared_summary"]["delivery"] = str(fixture["old_targets"]["video"])
    wrong_path["prepared_summary"]["subtitle"] = str(fixture["old_targets"]["subtitle"])
    wrong_seen: list[bool] = []

    def finalize_projection(seen: list[bool]):
        def finalize(projected, *_args, prepared_cover_ready=None, **_kwargs):
            ready = bool(prepared_cover_ready)
            seen.append(ready)
            projected["status"] = "review_ready" if ready else "blocked"
            projected["cover_path"] = (
                str(Path(projected["delivered"]).with_suffix(".cover.png"))
                if ready else None
            )

        return finalize

    project_materialized_talk(
        wrong_path, item={"cid": fixture["candidate_id"]}, date=fixture["date"],
        work_root=fixture["root"] / "out" / fixture["date"],
        runner=type("Runner", (), {"BASE": fixture["root"]})(),
        finalize=finalize_projection(wrong_seen),
    )
    assert wrong_seen == [False]
    assert wrong_path["status"] != "review_ready"

    result = deepcopy(base_result)
    ready_seen: list[bool] = []
    project_materialized_talk(
        result, item={"cid": fixture["candidate_id"]}, date=fixture["date"],
        work_root=fixture["root"] / "out" / fixture["date"],
        runner=type("Runner", (), {"BASE": fixture["root"]})(),
        finalize=finalize_projection(ready_seen),
    )
    assert ready_seen == [True]
    assert result["delivered"] == sealed["video"]["path"]
    assert result["delivered_subtitle"] == sealed["subtitle"]["path"]
    assert result["status"] == "review_ready"
    assert result["cover_path"] == sealed["cover"]["path"]

    state_dir = fixture["root"] / "state"
    state_dir.mkdir()
    state_path = state_dir / f"{fixture['date']}.json"
    before_state = {"pending_talk": [{"cid": fixture["candidate_id"]}], "picks": []}
    before = state_bytes(before_state)
    state_path.write_bytes(before)
    after_state = {"pending_talk": [], "picks": [deepcopy(result)]}
    old_snapshots = {
        role: (target.stat().st_ino, target.read_bytes())
        for role, target in fixture["old_targets"].items()
    }
    with exclusive_runner_commit(fixture["root"]) as lease:
        receipt = commit_prepared_prefix(
            runtime_root=fixture["root"], date=fixture["date"], state_path=state_path,
            state_before=before, after_state=after_state,
            entries=[PreparedBatchEntry("talk", fixture["candidate_id"], handle)], lease=lease,
        )

    assert len(receipt["installed_artifacts"]) == 11
    reloaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert reloaded == after_state
    projected = reloaded["picks"][0]
    assert projected["status"] == "review_ready"
    assert projected["delivered"] == sealed["video"]["path"]
    assert projected["delivered_subtitle"] == sealed["subtitle"]["path"]
    assert projected["cover_path"] == sealed["cover"]["path"]
    assert projected["video_sha256"] == sealed["video"]["sha256"]
    assert projected["subtitle_sha256"] == sealed["subtitle"]["sha256"]
    assert projected["cover_sha256"] == sealed["cover"]["sha256"]
    for role, binding in sealed.items():
        target = Path(binding["path"])
        assert target.is_file()
        assert target.read_bytes() == fixture["sources"][role].read_bytes()
    assert {
        role: (target.stat().st_ino, target.read_bytes())
        for role, target in fixture["old_targets"].items()
    } == old_snapshots
    assert load_prepared_delivery(
        runtime_root=fixture["root"], manifest_path=handle.manifest_path,
        allow_materialized=True,
    ) == handle


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
