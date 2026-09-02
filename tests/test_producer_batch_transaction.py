from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path
import time

import pytest

from src.autoslice.producer_batch_transaction import (
    PreparedBatchEntry,
    ProducerBatchTransactionError,
    commit_prepared_prefix,
    resume_pending_batch,
    validate_committed_batch_journal_for_deploy,
)
from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact,
    PreparedDelivery,
    ProducerDeliveryTransactionError,
    _write_create_only,
    prepare_delivery,
)
from src.autoslice.provider_slots import provider_slot
import src.autoslice.producer_delivery_transaction as delivery_transaction
import src.autoslice.producer_batch_transaction as batch_transaction
import src.autoslice.runner_state_writeback as runner_state_writeback
from src.autoslice.producer_delivery_transaction import _read_document
from src.autoslice.producer_batch_projection import (
    project_materialized_song,
    project_materialized_talk,
)
from src.autoslice.producer_batch_runner_integration import (
    dispatch_prepared_lane,
    finish_producer_date,
    maintain_selected_source_fact_recovery,
    project_prepared_results,
    reject_materialized_prepare_results,
)
import src.autoslice.producer_batch_runner_integration as batch_integration
import src.autoslice.producer_batch_projection as batch_projection
from src.autoslice.qixi_transaction_core import (
    QixiTransactionCoreError,
    current_runner_commit_lease,
    exclusive_runner_commit,
)
from src.autoslice.repository_asset_authority import _canonical_sha256
from src.autoslice.runner_state_writeback import (
    RunnerStateWritebackError,
    state_bytes,
    track_state,
    write_exact_state_under_lease,
    write_state,
)


def _seal(root: Path) -> None:
    repo = root / "repo"
    repo.mkdir()
    body = {
        "schema_version": "deployed-authority-manifest.v1", "deployed_commit": "a" * 40,
        "entries": {"docs/pipeline/80-package-delivery.md": {
            "bytes": 1, "sha256": "sha256:" + "b" * 64,
        }},
    }
    manifest = {**body, "manifest_sha256": _canonical_sha256(body)}
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n", encoding="utf-8")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")


def _fixture(tmp_path: Path):
    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    (root / "state").mkdir()
    state_path = root / "state" / "2026-08-22.json"
    before_state = {"pending_talk": [{"cid": "cid-1"}], "picks": []}
    before = json.dumps(before_state, ensure_ascii=False, indent=2).encode("utf-8")
    state_path.write_bytes(before)
    source = root / "out" / "cid-1" / "clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"prepared-video")
    delivery_root = root / "repo" / "lidousha"
    delivery_root.mkdir(parents=True)
    target = delivery_root / "2026-08-22" / "hook__cid-1.mp4"
    prepared = prepare_delivery(
        runtime_root=root, lane="talk", candidate_id="cid-1",
        artifacts=[DeliveryArtifact("video", source, target, "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest())],
    )
    after = {"pending_talk": [], "picks": [{"candidate_id": "cid-1", "status": "review_ready", "delivered": str(target)}]}
    entry = PreparedBatchEntry("talk", "cid-1", prepared)
    return root, state_path, before, target, after, entry


def _private_prepare_worker(
    root_raw: str, date: str, candidate_id: str, release, active, maximum, guard, results,
) -> None:
    """Spawn-safe mock provider prepare worker for the batch golden canary."""

    root = Path(root_raw)
    try:
        with provider_slot(root, timeout_seconds=10, poll_seconds=0.01):
            with guard:
                active.value += 1
                maximum.value = max(maximum.value, active.value)
            release.wait(15)
            with guard:
                active.value -= 1
        source = root / "out" / date / candidate_id / "clip.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        payload = f"prepared:{candidate_id}".encode("utf-8")
        source.write_bytes(payload)
        target = root / "repo" / "lidousha" / date / f"same.hook__{candidate_id}.mp4"
        handle = prepare_delivery(
            runtime_root=root, lane="talk", candidate_id=candidate_id,
            artifacts=[DeliveryArtifact(
                "video", source, target,
                "sha256:" + hashlib.sha256(payload).hexdigest(),
            )],
        )
        results.put(("ok", candidate_id, str(handle.manifest_path), handle.prepared_sha256))
    except BaseException as exc:  # pragma: no cover - subprocess propagation
        results.put(("error", candidate_id, type(exc).__name__, str(exc)))


def test_batch_commits_target_then_exact_state_under_one_lease(tmp_path: Path):
    root, state_path, before, target, after, entry = _fixture(tmp_path)
    with exclusive_runner_commit(root) as lease:
        receipt = commit_prepared_prefix(
            runtime_root=root, date="2026-08-22", state_path=state_path,
            state_before=before, after_state=after, entries=[entry], lease=lease,
        )
    assert target.read_bytes() == b"prepared-video"
    assert json.loads(state_path.read_text(encoding="utf-8")) == after
    journal = json.loads(Path(receipt["journal_path"]).read_text(encoding="utf-8"))
    assert journal["status"] == "COMMITTED"
    assert journal["installed_artifacts"] == journal["artifact_inventory"]


def test_five_private_prepares_share_provider_cap_then_commit_same_hook_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Five candidates keep all shared bytes untouched until one short commit."""

    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    (root / "repo" / "lidousha").mkdir(parents=True)
    state_dir = root / "state"
    state_dir.mkdir()
    date = "2026-08-22"
    candidate_ids = [f"cid-{index}" for index in range(5)]
    state_path = state_dir / f"{date}.json"
    before_state = {"pending_talk": [{"cid": candidate_id} for candidate_id in candidate_ids], "picks": []}
    before = state_bytes(before_state)
    state_path.write_bytes(before)
    delivery_date = root / "repo" / "lidousha" / date
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "2")
    context = multiprocessing.get_context("spawn")
    release = context.Event()
    active = context.Value("i", 0)
    maximum = context.Value("i", 0)
    guard = context.Lock()
    results = context.Queue()
    workers = [
        context.Process(
            target=_private_prepare_worker,
            args=(str(root), date, candidate_id, release, active, maximum, guard, results),
        )
        for candidate_id in candidate_ids
    ]
    for worker in workers:
        worker.start()
    try:
        deadline = time.monotonic() + 10
        while active.value < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert active.value == 2
        # Provider work is outside the mutation mutex even while three more
        # candidate processes wait for the same cross-process pool.
        with exclusive_runner_commit(root):
            pass
        assert maximum.value <= 2
        assert state_path.read_bytes() == before
        assert not delivery_date.exists()
        assert not (root / ".producer-batch-journal").exists()

        release.set()
        prepared: dict[str, PreparedDelivery] = {}
        for _ in candidate_ids:
            message = results.get(timeout=20)
            assert message[0] == "ok", message
            _status, candidate_id, manifest_path, digest = message
            prepared[candidate_id] = PreparedDelivery(
                root, "talk", candidate_id, digest, Path(manifest_path),
            )
        for worker in workers:
            worker.join(timeout=20)
            assert worker.exitcode == 0
    finally:
        release.set()
        for worker in workers:
            if worker.is_alive():
                worker.join(timeout=5)

    entries = [PreparedBatchEntry("talk", candidate_id, prepared[candidate_id]) for candidate_id in candidate_ids]
    after = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": candidate_id, "status": "review_ready",
                "delivered": str(root / "repo" / "lidousha" / date / f"same.hook__{candidate_id}.mp4"),
            }
            for candidate_id in candidate_ids
        ],
    }
    with exclusive_runner_commit(root) as lease:
        receipt = commit_prepared_prefix(
            runtime_root=root, date=date, state_path=state_path,
            state_before=before, after_state=after, entries=entries, lease=lease,
        )

    assert maximum.value == 2
    assert json.loads(state_path.read_text(encoding="utf-8")) == after
    assert len({row["delivered"] for row in after["picks"]}) == 5
    assert all(Path(row["delivered"]).is_file() for row in after["picks"])
    assert json.loads(Path(receipt["journal_path"]).read_text(encoding="utf-8"))["status"] == "COMMITTED"


def test_batch_state_drift_creates_no_formal_journal_or_target(tmp_path: Path):
    root, state_path, before, target, after, entry = _fixture(tmp_path)
    state_path.write_text("{}", encoding="utf-8")
    with exclusive_runner_commit(root) as lease:
        with pytest.raises(ProducerBatchTransactionError, match="state preimage drifts"):
            commit_prepared_prefix(
                runtime_root=root, date="2026-08-22", state_path=state_path,
                state_before=before, after_state=after, entries=[entry], lease=lease,
            )
    assert not target.exists()
    assert not (root / ".producer-batch-journal").exists()


def test_committed_receipt_does_not_block_later_noop_resume(tmp_path: Path):
    root, state_path, before, _target, after, entry = _fixture(tmp_path)
    with exclusive_runner_commit(root) as lease:
        commit_prepared_prefix(
            runtime_root=root, date="2026-08-22", state_path=state_path,
            state_before=before, after_state=after, entries=[entry], lease=lease,
        )
    with exclusive_runner_commit(root) as lease:
        assert resume_pending_batch(runtime_root=root, date="2026-08-22", lease=lease) is None


def test_batch_journal_namespace_mode_drift_blocks_recovery_before_prepare(tmp_path: Path):
    root, _state_path, _before, _target, _after, _entry = _fixture(tmp_path)
    journal_root = root / ".producer-batch-journal"
    journal_root.mkdir()
    journal_root.chmod(0o755)

    with exclusive_runner_commit(root) as lease:
        with pytest.raises(ProducerBatchTransactionError, match="namespace is unsafe"):
            resume_pending_batch(runtime_root=root, date="2026-08-22", lease=lease)


def _talk_projection_fixture(tmp_path: Path):
    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    date = "2026-08-22"
    candidate_id = "cid-1"
    source_root = root / "out" / date / candidate_id
    source_root.mkdir(parents=True)
    delivery = root / "repo" / "lidousha" / date / "hook__cid-1"
    sources = {
        "video": ("clip.mp4", ".mp4", b"video"),
        "subtitle": ("clip.srt", ".srt", b"subtitle"),
        "cover": ("clip.cover.png", ".cover.png", b"cover"),
        "record": ("clip.record.json", ".record.json", b"record"),
        "publish": ("clip.publish.json", ".publish.json", b"publish"),
    }
    artifacts = []
    for role, (source_name, suffix, payload) in sources.items():
        source = source_root / source_name
        source.write_bytes(payload)
        artifacts.append(DeliveryArtifact(
            role, source, Path(str(delivery) + suffix),
            "sha256:" + hashlib.sha256(payload).hexdigest(),
        ))
    handle = prepare_delivery(
        runtime_root=root, lane="talk", candidate_id=candidate_id, artifacts=artifacts,
    )
    document = _read_document(handle)
    bindings = {
        row["role"]: {"path": row["target_path"], "sha256": row["staged_sha256"]}
        for row in document["artifacts"]
    }
    result = {
        "candidate_id": candidate_id, "rc": 0, "status": "delivery_prepared_no_target",
        "cover_status": "AI_COVER_READY",
        "video_sha256": bindings["video"]["sha256"],
        "subtitle_sha256": bindings["subtitle"]["sha256"],
        "cover_sha256": bindings["cover"]["sha256"],
        "prepared_summary": {
            "delivery": bindings["video"]["path"],
            "subtitle": bindings["subtitle"]["path"],
            "cover_status": "AI_COVER_READY", "red_flags": [], "boundary_repairs": [],
            "prepared_delivery": {
                "manifest_path": str(handle.manifest_path),
                "prepared_sha256": f"sha256:{handle.prepared_sha256}",
            },
            "prepared_artifacts": bindings,
        },
    }
    item = {"cid": candidate_id}
    return root, date, result, item


def test_talk_projection_binds_ai_cover_to_sealed_prepared_hashes(tmp_path: Path):
    root, date, result, item = _talk_projection_fixture(tmp_path)
    seen = {}

    def finalize(_result, *_args, prepared_cover_ready=None, **_kwargs):
        seen["ready"] = prepared_cover_ready

    project_materialized_talk(
        result, item=item, date=date, work_root=root / "out" / date,
        runner=type("Runner", (), {"BASE": root})(), finalize=finalize,
    )

    assert seen["ready"] is True
    assert result["delivered"].endswith("hook__cid-1.mp4")
    assert "prepared_delivery" not in result["summary"]


def test_talk_projection_rejects_same_role_set_with_wrong_result_hash(tmp_path: Path):
    root, date, result, item = _talk_projection_fixture(tmp_path)
    result["cover_sha256"] = "sha256:" + "0" * 64
    seen = {}

    project_materialized_talk(
        result, item=item, date=date, work_root=root / "out" / date,
        runner=type("Runner", (), {"BASE": root})(),
        finalize=lambda *_args, prepared_cover_ready=None, **_kwargs: seen.setdefault("ready", prepared_cover_ready),
    )

    assert seen["ready"] is False


def test_talk_projection_uses_same_sealed_binding_for_reused_cover(tmp_path: Path, monkeypatch):
    root, date, result, item = _talk_projection_fixture(tmp_path)
    result["cover_status"] = "REUSED_COVER"
    result["prepared_summary"]["cover_status"] = "REUSED_COVER"
    monkeypatch.setattr(batch_projection, "accepted_result_cover_status", lambda *_args, **_kwargs: "REUSED_COVER")
    seen = {}

    project_materialized_talk(
        result, item=item, date=date, work_root=root / "out" / date,
        runner=type("Runner", (), {"BASE": root})(),
        finalize=lambda *_args, prepared_cover_ready=None, **_kwargs: seen.setdefault("ready", prepared_cover_ready),
    )

    assert seen["ready"] is True


def test_exact_tracked_state_write_keeps_live_mapping_after_commit(tmp_path: Path):
    root, state_path, before, _target, _after, _entry = _fixture(tmp_path)
    tracked = track_state(state_path, json.loads(before.decode("utf-8")))
    tracked["picks"] = [{"candidate_id": "cid-1", "status": "review_ready"}]
    expected = dict(tracked)

    with exclusive_runner_commit(root) as lease:
        write_exact_state_under_lease(
            state_path, runtime_root=root, lease=lease,
            expected_before=before, after=tracked,
        )

    assert dict(tracked) == expected
    assert json.loads(state_path.read_text(encoding="utf-8")) == expected


def test_create_only_document_short_write_never_leaves_partial_authority(tmp_path: Path, monkeypatch):
    path = tmp_path / "journal.json"
    real_write = delivery_transaction.os.write
    calls = 0

    def short_then_error(descriptor, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return min(3, len(payload))
        raise OSError("ENOSPC")

    monkeypatch.setattr(delivery_transaction.os, "write", short_then_error)
    with pytest.raises(ProducerDeliveryTransactionError, match="write failed"):
        _write_create_only(path, b"sealed-journal")
    monkeypatch.setattr(delivery_transaction.os, "write", real_write)

    assert not path.exists()


@pytest.mark.parametrize("fault", ["zero", "partial_then_enospc"])
def test_state_write_short_or_zero_temp_never_promotes_partial_authority(
    tmp_path: Path, monkeypatch, fault: str,
):
    root, state_path, before, _target, _after, _entry = _fixture(tmp_path)
    real_write = runner_state_writeback.os.write
    calls = 0

    def faulting_write(descriptor, payload):
        nonlocal calls
        calls += 1
        if fault == "zero":
            return 0
        if calls == 1:
            return min(3, len(payload))
        raise OSError("ENOSPC")

    monkeypatch.setattr(runner_state_writeback.os, "write", faulting_write)
    with pytest.raises(RunnerStateWritebackError, match="TEMP_WRITE_FAILED"):
        runner_state_writeback._atomic_write_bytes(
            state_path, b'{"picks":["new"]}', runtime_root=root,
        )
    monkeypatch.setattr(runner_state_writeback.os, "write", real_write)

    assert state_path.read_bytes() == before
    assert not list(state_path.parent.glob(f".{state_path.name}.*"))


def test_batch_recovery_rolls_forward_after_crash_between_artifact_checkpoints(
    tmp_path: Path, monkeypatch
):
    root, state_path, before, target, after, _entry = _fixture(tmp_path)
    source = root / "out" / "cid-1" / "clip.srt"
    source.write_bytes(b"prepared-subtitle")
    second_target = target.with_suffix(".srt")
    prepared = prepare_delivery(
        runtime_root=root, lane="talk", candidate_id="cid-1",
        artifacts=[
            DeliveryArtifact(
                "video", root / "out" / "cid-1" / "clip.mp4", target,
                "sha256:" + hashlib.sha256(b"prepared-video").hexdigest(),
            ),
            DeliveryArtifact(
                "subtitle", source, second_target,
                "sha256:" + hashlib.sha256(b"prepared-subtitle").hexdigest(),
            ),
        ],
    )
    entry = PreparedBatchEntry("talk", "cid-1", prepared)
    real_replace = batch_transaction._write_replace
    calls = 0

    def fail_second_checkpoint(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash after second target rename")
        return real_replace(path, payload)

    monkeypatch.setattr(batch_transaction, "_write_replace", fail_second_checkpoint)
    with exclusive_runner_commit(root) as lease:
        with pytest.raises(OSError, match="simulated crash"):
            commit_prepared_prefix(
                runtime_root=root, date="2026-08-22", state_path=state_path,
                state_before=before, after_state=after, entries=[entry], lease=lease,
            )
    assert target.exists() and second_target.exists()
    assert state_path.read_bytes() == before
    pending_path = next((root / ".producer-batch-journal").glob("*.json"))
    with pytest.raises(ProducerBatchTransactionError, match="pending"):
        validate_committed_batch_journal_for_deploy(runtime_root=root, path=pending_path)

    monkeypatch.setattr(batch_transaction, "_write_replace", real_replace)
    with exclusive_runner_commit(root) as lease:
        resumed = resume_pending_batch(runtime_root=root, date="2026-08-22", lease=lease)

    assert resumed is not None
    assert json.loads(state_path.read_text(encoding="utf-8")) == after


def _song_projection_fixture(tmp_path: Path, *, include_cover: bool = True):
    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    candidate_id = "song-1"
    source_root = root / "out" / candidate_id
    source_root.mkdir(parents=True)
    delivery = root / "repo" / "lidousha" / "2026-08-22" / "song__song-1"
    generation = {"status": "AI_COVER_READY", "final_cover": "private-cover"}
    payloads = {
        "video": b"video",
        "delivery_manifest": b"manifest",
        "active_record": json.dumps({
            "materialized_recut": {"publish_staging": {"cover_generation": generation}},
        }).encode("utf-8"),
    }
    if include_cover:
        payloads["cover"] = b"cover"
    suffixes = {"video": ".mp4", "delivery_manifest": ".delivery.manifest.json", "cover": ".cover.png", "active_record": ".record.json"}
    artifacts = []
    for role, payload in payloads.items():
        source = source_root / role
        source.write_bytes(payload)
        artifacts.append(DeliveryArtifact(
            role, source, Path(str(delivery) + suffixes[role]),
            "sha256:" + hashlib.sha256(payload).hexdigest(),
        ))
    prepared = prepare_delivery(runtime_root=root, lane="song", candidate_id=candidate_id, artifacts=artifacts)
    document = _read_document(prepared)
    intended = {
        row["role"]: {"path": row["target_path"], "sha256": row["staged_sha256"]}
        for row in document["artifacts"]
    }
    result = {
        "candidate_id": candidate_id, "rc": 0, "status": "delivery_prepared_no_target",
        "intended_delivery": intended["video"],
        "intended_delivery_manifest": intended["delivery_manifest"],
        "intended_delivery_sidecars": {role: value for role, value in intended.items() if role != "video"},
        "intended_cover_status": "AI_COVER_READY" if include_cover else "BLOCKED_AI_COVER_REQUIRED",
        "prepared_delivery": {
            "manifest_path": str(prepared.manifest_path),
            "prepared_sha256": f"sha256:{prepared.prepared_sha256}",
        },
    }
    if include_cover:
        result["intended_cover"] = intended["cover"]
        result["prepared_cover_generation"] = generation
    return root, candidate_id, result, intended


def test_song_projection_matches_the_sealed_prepared_target_map(tmp_path: Path):
    root, candidate_id, result, intended = _song_projection_fixture(tmp_path)
    project_materialized_song(
        result, runtime_root=root, candidate_id=candidate_id,
        song_status=lambda rc, delivered: "review_ready" if rc == 0 and delivered else "failed",
    )

    assert result["delivered"] == intended["video"]["path"]
    assert result["delivery_manifest_path"] == intended["delivery_manifest"]["path"]
    assert result["delivered_sidecars"]["cover"] == intended["cover"]["path"]
    assert result["cover_path"] == intended["cover"]["path"]
    assert result["status"] == "review_ready"


def test_song_projection_rejects_result_path_drift_from_prepared_manifest(tmp_path: Path):
    root, candidate_id, result, _intended = _song_projection_fixture(tmp_path)
    result["intended_delivery"]["path"] = str(root / "repo" / "lidousha" / "wrong.mp4")

    with pytest.raises(ValueError, match="projection drifts"):
        project_materialized_song(
            result, runtime_root=root, candidate_id=candidate_id,
            song_status=lambda _rc, _delivered: "review_ready",
        )


@pytest.mark.parametrize(
    ("include_cover", "status"),
    [(True, "BLOCKED_AI_COVER_REQUIRED"), (False, "AI_COVER_READY")],
)
def test_song_projection_rejects_cover_status_opposite_to_sealed_cover(
    tmp_path: Path, include_cover: bool, status: str,
):
    root, candidate_id, result, _intended = _song_projection_fixture(
        tmp_path, include_cover=include_cover,
    )
    result["intended_cover_status"] = status
    with pytest.raises(ValueError, match="projection drifts"):
        project_materialized_song(
            result, runtime_root=root, candidate_id=candidate_id,
            song_status=lambda _rc, _delivered: "review_ready",
        )


def test_prepare_protocol_rejects_unowned_materialized_success_before_state_commit(tmp_path: Path):
    root, state_path, before, target, _after, _entry = _fixture(tmp_path)
    with pytest.raises(ValueError, match="materialized success"):
        reject_materialized_prepare_results(
            runtime_root=root, lane="talk", items=[{"cid": "cid-1"}],
            results=[{
                "candidate_id": "cid-1", "rc": 0, "status": "review_ready",
                "delivered": str(target),
            }],
        )
    assert state_path.read_bytes() == before
    assert not target.exists()
    assert not (root / ".producer-batch-journal").exists()


@pytest.mark.parametrize("status", ["review_ready", "media_ready_cover_pending"])
def test_prepare_protocol_rejects_status_only_positive_success(tmp_path: Path, status: str):
    root = tmp_path / "runtime"
    root.mkdir()

    with pytest.raises(ValueError, match="materialized success"):
        reject_materialized_prepare_results(
            runtime_root=root,
            lane="song",
            items=[{"cid": "song-positive"}],
            results=[{
                "candidate_id": "song-positive",
                "rc": 0,
                "status": status,
            }],
        )

    assert not (root / ".producer-batch-journal").exists()


def test_prepare_protocol_accepts_rc0_terminal_song_rejection_without_target(tmp_path: Path):
    root = tmp_path / "runtime"
    root.mkdir()
    result = {
        "candidate_id": "songvis_225221_120_a5f25f0c",
        "rc": 0,
        "status": "candidate_rejected",
        "decision": "REJECT",
        "reason_codes": ["SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS"],
    }

    reject_materialized_prepare_results(
        runtime_root=root,
        lane="song",
        items=[{"cid": result["candidate_id"]}],
        results=[result],
    )

    assert result["status"] == "candidate_rejected"
    assert result["decision"] == "REJECT"
    assert result["reason_codes"] == ["SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS"]
    assert not (root / ".producer-batch-journal").exists()
    assert "delivered" not in result
    assert "delivery_manifest_path" not in result
    assert "delivered_sidecars" not in result


def test_mixed_failure_then_prepared_result_keeps_later_prepared_entry(tmp_path: Path, monkeypatch):
    root, date, result, item = _talk_projection_fixture(tmp_path)
    monkeypatch.setattr(batch_integration, "project_materialized_talk", lambda *_args, **_kwargs: None)
    entries = project_prepared_results(
        runtime_root=root, date=date, lane="talk",
        items=[{"cid": "failed-first"}, item],
        results=[
            {"candidate_id": "failed-first", "rc": -1, "status": "title_failed"},
            result,
        ],
        runner=type("Runner", (), {"BASE": root})(),
    )

    assert [(entry.lane, entry.candidate_id) for entry in entries] == [("talk", "cid-1")]


def test_dispatch_rejects_external_state_change_before_provider_prepare(tmp_path: Path):
    """The exact CAS cannot be built from a stale in-memory runner mapping."""

    root = tmp_path / "runtime"
    root.mkdir()
    state_dir = root / "state"
    state_dir.mkdir()
    state_path = state_dir / "2026-08-22.json"
    local = {"pending_talk": [{"cid": "cid-1"}], "picks": []}
    external = {"pending_talk": [], "picks": [{"candidate_id": "external"}]}
    state_path.write_bytes(state_bytes(external))
    provider_calls = 0

    def should_not_prepare(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("provider preparation must not begin after CAS drift")

    with pytest.raises(RunnerStateWritebackError, match="DISPATCH_PREIMAGE_DRIFT"):
        dispatch_prepared_lane(
            runtime_root=root, date="2026-08-22", lane="talk",
            items=[{"cid": "cid-1"}], state_path=state_path, state=local,
            produce_batch=should_not_prepare, produce_fn=object(), runner=object(),
        )

    assert provider_calls == 0
    assert state_path.read_bytes() == state_bytes(external)
    assert not (root / ".producer-batch-journal").exists()
    assert not (root / "repo" / "lidousha" / "2026-08-22" / "hook__cid-1.mp4").exists()


def test_ordinary_state_writer_reuses_live_canonical_lease(tmp_path: Path):
    root, state_path, _before, _target, _after, _entry = _fixture(tmp_path)
    live = track_state(state_path, json.loads(state_path.read_text(encoding="utf-8")))
    live["picks"] = [{"candidate_id": "manual", "status": "review_ready"}]

    with exclusive_runner_commit(root):
        write_state(
            state_path, live, runtime_root=root, updated_at="2026-08-22T00:00:00Z",
            log=lambda _message: None,
        )

    assert live["picks"] == [{"candidate_id": "manual", "status": "review_ready"}]
    assert json.loads(state_path.read_text(encoding="utf-8"))["picks"] == live["picks"]


def test_legacy_cover_repair_is_serialized_with_its_state_bind(tmp_path: Path):
    root, state_path, _before, _target, _after, _entry = _fixture(tmp_path)
    state = track_state(state_path, json.loads(state_path.read_text(encoding="utf-8")))
    state["status"] = "review_ready"
    observed: list[str] = []

    def repair(_date, live, **_kwargs):
        live["cover_repaired"] = True
        write_state(
            state_path, live, runtime_root=root, updated_at="2026-08-22T00:00:00Z",
            log=lambda _message: None,
        )
        observed.append("repair-and-bind")

    finish_producer_date(
        runtime_root=root, date="2026-08-22", state=state, mutate_songs=True,
        repair_covers=repair, automatic_maintenance=True, candidate_ids=None,
        project_terminal=lambda _state, **_kwargs: {
            "delivered_talk": [], "picks": [], "repaired": [],
            "delivered_songs": [], "blocked_songs": [], "rejected_songs": [],
            "songs": [], "failures": [],
        },
        persist_state=lambda: None, write_reports=lambda *_args: None,
        log=lambda _message: None, capture_candidates=[], routing_claim=None,
        queue_collab=lambda *_args, **_kwargs: None,
    )

    assert observed == ["repair-and-bind"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["cover_repaired"] is True


def test_deterministic_delivery_recovery_runs_under_canonical_lease(tmp_path: Path):
    root = tmp_path / "runtime"
    root.mkdir()
    observed: list[str] = []

    def recover(*_args, **_kwargs):
        assert current_runner_commit_lease(runtime_root=root) is not None
        with pytest.raises(QixiTransactionCoreError, match="nesting"):
            with exclusive_runner_commit(root):
                pass
        observed.append("serialized")
        return (0, 0, 0, 0, False)

    assert maintain_selected_source_fact_recovery(
        runtime_root=root, maintain=recover, date="2026-08-22", state={},
        automatic_maintenance=True, candidate_ids=None, persist=lambda: None,
        readback=lambda: {}, log=lambda _message: None,
        song_pipeline_fingerprint=lambda: "sha256:test",
    ) == (0, 0, 0, 0, False)
    assert observed == ["serialized"]


def test_deploy_validator_rejects_rehashed_fake_committed_inventory(tmp_path: Path):
    root, state_path, before, _target, after, entry = _fixture(tmp_path)
    with exclusive_runner_commit(root) as lease:
        receipt = commit_prepared_prefix(
            runtime_root=root, date="2026-08-22", state_path=state_path,
            state_before=before, after_state=after, entries=[entry], lease=lease,
        )
    journal_path = Path(receipt["journal_path"])
    document = json.loads(journal_path.read_text(encoding="utf-8"))
    document["artifact_inventory"] = ["forged"]
    document["installed_artifacts"] = ["forged"]
    unsigned = dict(document)
    unsigned.pop("journal_sha256")
    document["journal_sha256"] = "sha256:" + hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    ).hexdigest()
    journal_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    journal_path.chmod(0o600)

    with pytest.raises(ProducerBatchTransactionError, match="committed inventory drifts"):
        validate_committed_batch_journal_for_deploy(runtime_root=root, path=journal_path)
