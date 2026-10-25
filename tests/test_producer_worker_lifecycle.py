from __future__ import annotations

import copy
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice.produce_dispatch import produce_batch_windowed
from src.autoslice.producer_batch_runner_integration import (
    cover_state_transition,
    dispatch_prepared_lane,
    make_talk_result_checkpoint,
)
import src.autoslice.producer_batch_transaction as producer_batch_transaction
from src.autoslice.producer_batch_transaction import ProducerBatchTransactionError
from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact,
    ProducerDeliveryTransactionError,
    _canonical_sha256,
    prepare_delivery,
)
from src.autoslice.qixi_transaction_core import current_runner_commit_lease
from src.autoslice.runner_state_writeback import (
    RunnerStateWritebackError,
    state_bytes,
    write_state,
)


DATE = "2026-10-13"


def _runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    (repo / "lidousha").mkdir()
    consumer = repo / "docs/pipeline/80-package-delivery.md"
    consumer.parent.mkdir(parents=True)
    consumer.write_bytes(b"bound consumer")
    commit = "a" * 40
    body = {
        "schema_version": "deployed-authority-manifest.v1",
        "deployed_commit": commit,
        "entries": {
            "docs/pipeline/80-package-delivery.md": {
                "bytes": len(consumer.read_bytes()),
                "sha256": "sha256:" + hashlib.sha256(consumer.read_bytes()).hexdigest(),
            }
        },
    }
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n", encoding="utf-8")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(
        json.dumps({**body, "manifest_sha256": _canonical_sha256(body)}),
        encoding="utf-8",
    )
    return root


def _state_file(root: Path, items: list[dict]) -> tuple[dict, Path]:
    state = {
        "pending_talk": copy.deepcopy(items),
        "picks": [],
        "songs": [],
    }
    state_path = root / "state" / f"{DATE}.json"
    state_path.parent.mkdir()
    state_path.write_bytes(state_bytes(state))
    return state, state_path


def _prepared_result(root: Path, candidate_id: str) -> tuple[object, dict]:
    source = root / "source" / f"{candidate_id}.bin"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(f"prepared:{candidate_id}".encode("utf-8"))
    target = root / "repo" / "lidousha" / DATE / f"{candidate_id}.mp4"
    payload_sha = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    handle = prepare_delivery(
        runtime_root=root,
        lane="talk",
        candidate_id=candidate_id,
        artifacts=[DeliveryArtifact("video", source, target, payload_sha)],
    )
    result = {
        "candidate_id": candidate_id,
        "status": "delivery_prepared_no_target",
        "prepared_summary": {
            "prepared_delivery": {
                "manifest_path": str(handle.manifest_path),
                "prepared_sha256": f"sha256:{handle.prepared_sha256}",
            }
        },
    }
    return handle, result


def _windowed(
    root: Path,
    items: list[dict],
    producer,
    *,
    max_parallel: int = 1,
    free_bytes: int = 1 << 40,
    prepare_only: bool = False,
    on_result=None,
    stop_requested_fn=None,
    talk_fingerprint=None,
    dependency_snapshot=None,
    dependency_fingerprint=None,
    log=None,
) -> list[dict]:
    fingerprint = talk_fingerprint or (lambda _candidate_id: "talk-pipeline-v1")

    def song_producer(*_args, **_kwargs):
        raise AssertionError("song producer is outside this test")

    return produce_batch_windowed(
        DATE,
        items,
        producer,
        produce_talk_fn=producer,
        produce_song_fn=song_producer,
        base=root,
        max_parallel=max_parallel,
        min_free_bytes=0,
        free_bytes_fn=lambda _path: free_bytes,
        operational_reserve_bytes=0,
        peak_bytes_per_second=1,
        log=log or (lambda _message: None),
        talk_pipeline_fingerprint=fingerprint,
        pipeline_fingerprint=lambda: "generic-pipeline-v1",
        song_pipeline_fingerprint=lambda: "song-pipeline-v1",
        song_window_pre_ms=0,
        song_window_post_ms=0,
        talk_window_pre_ms=0,
        talk_window_post_ms=0,
        stop_requested_fn=stop_requested_fn,
        capture_talk_dependency_snapshot=dependency_snapshot,
        prepared_result_dependency_fingerprint=dependency_fingerprint,
        prepare_only=prepare_only,
        on_result=on_result,
    )


def _talk_checkpoint(
    *,
    root: Path,
    state: dict,
    state_path: Path,
    items: list[dict],
    persist_state,
    title_max_attempts: int = 3,
):
    return make_talk_result_checkpoint(
        runtime_root=root,
        date=DATE,
        state_path=state_path,
        state=state,
        talk_items=items,
        rescore_blocked_items=[],
        frozen_talk_candidate_ids=set(),
        production_preimage=None,
        exact_contract_ids=set(),
        runner=SimpleNamespace(BASE=root),
        checkpoint_before=state_path.read_bytes(),
        replace_pending=lambda live, _frozen, pending: live.__setitem__(
            "pending_talk", copy.deepcopy(list(pending))
        ),
        seal_transition=lambda *_args: True,
        apply_rejection=lambda _result, **_kwargs: False,
        delivered_statuses={"ok"},
        cover_pending_status="media_ready_cover_pending",
        title_max_attempts=title_max_attempts,
        persist_state=persist_state,
        log=lambda _message: None,
    )


@pytest.mark.parametrize(
    ("initial_attempts", "a_in_pending"),
    [(0, True), (2, False)],
)
def test_talk_title_retry_or_terminal_failure_keeps_ordered_tail_dispatching(
    tmp_path: Path, initial_attempts: int, a_in_pending: bool
):
    """Content outcomes checkpoint and advance the shared ordered prefix."""

    root = _runtime(tmp_path)
    items = [
        {"cid": "a", "title_attempts": initial_attempts, "capacity_duration_ms": 1},
        {"cid": "b", "capacity_duration_ms": 1},
        {"cid": "c", "capacity_duration_ms": 1},
    ]
    state, state_path = _state_file(root, items)
    initial_state_bytes = state_path.read_bytes()
    calls: list[str] = []
    outcome = {
        "a": {"candidate_id": "a", "status": "title_failed"},
        "b": {"candidate_id": "b", "status": "ok"},
        "c": {"candidate_id": "c", "status": "ok"},
    }

    def producer(_date, item, **_kwargs):
        calls.append(item["cid"])
        return dict(outcome[item["cid"]])

    def produce_batch(date, batch_items, produce_fn, **kwargs):
        return _windowed(
            root,
            batch_items,
            produce_fn,
            max_parallel=1,
            prepare_only=kwargs.get("prepare_only", False),
            on_result=kwargs.get("on_result"),
        )

    persisted: list[dict] = []

    def persist_state():
        persisted.append(copy.deepcopy(state))
        state_path.write_bytes(state_bytes(state))

    checkpoint, checkpoint_state = _talk_checkpoint(
        root=root,
        state=state,
        state_path=state_path,
        items=items,
        persist_state=persist_state,
    )
    before, results, entries = dispatch_prepared_lane(
        runtime_root=root,
        date=DATE,
        lane="talk",
        items=items,
        state_path=state_path,
        state=state,
        produce_batch=produce_batch,
        produce_fn=producer,
        runner=SimpleNamespace(BASE=root),
        on_result=checkpoint,
    )

    assert before == initial_state_bytes
    assert entries == []
    assert [row["candidate_id"] for row in results] == ["a", "b", "c"]
    assert calls == ["a", "b", "c"]
    assert checkpoint_state.retry == ([items[0]] if a_in_pending else [])
    assert [row["candidate_id"] for row in state["picks"]] == (
        ["b", "c"] if a_in_pending else ["a", "b", "c"]
    )
    if a_in_pending:
        assert state["pending_talk"][0]["cid"] == "a"
        assert state["pending_talk"][0]["title_attempts"] == 1
    else:
        assert state["pending_talk"] == []
        assert state["picks"][0]["status"] == "failed"
    assert len(persisted) == 3


def test_persistence_callback_failure_stops_new_dispatch_and_retains_prepared_receipt(
    tmp_path: Path,
):
    """A durable callback failure stops C while A/B evidence remains inspectable."""

    root = _runtime(tmp_path)
    items = [
        {"cid": cid, "capacity_duration_ms": 1}
        for cid in ("a", "b", "c")
    ]
    state, state_path = _state_file(root, items)
    calls: list[str] = []
    b_started = False

    def producer(_date, item, **_kwargs):
        nonlocal b_started
        cid = item["cid"]
        calls.append(cid)
        if cid == "a":
            # Ensure the two-future window has admitted B before A reaches the
            # state callback.  B's private completion must survive A's error.
            for _ in range(500):
                if b_started:
                    break
                import time

                time.sleep(0.001)
            assert b_started
            return {"candidate_id": "a", "status": "ok"}
        if cid == "b":
            b_started = True
            _handle, result = _prepared_result(root, cid)
            return result
        raise AssertionError("C must remain deferred")

    def produce_batch(date, batch_items, produce_fn, **kwargs):
        return _windowed(
            root,
            batch_items,
            produce_fn,
            max_parallel=2,
            prepare_only=kwargs.get("prepare_only", False),
            on_result=kwargs.get("on_result"),
        )

    class PersistenceFailure(RuntimeError):
        pass

    def persist_state():
        raise PersistenceFailure("state CAS failed")

    checkpoint, _checkpoint_state = _talk_checkpoint(
        root=root,
        state=state,
        state_path=state_path,
        items=items,
        persist_state=persist_state,
    )
    with pytest.raises(PersistenceFailure, match="state CAS failed"):
        dispatch_prepared_lane(
            runtime_root=root,
            date=DATE,
            lane="talk",
            items=items,
            state_path=state_path,
            state=state,
            produce_batch=produce_batch,
            produce_fn=producer,
            runner=SimpleNamespace(BASE=root),
            on_result=checkpoint,
        )

    assert set(calls) == {"a", "b"}
    assert "c" not in calls
    assert json.loads(state_path.read_bytes()) == {
        "pending_talk": items,
        "picks": [],
        "songs": [],
    }
    receipts = list(
        (root / ".prepared-deliveries" / "talk" / "b").glob(
            "*/dispatch-result-*.json"
        )
    )
    assert len(receipts) == 1


def test_recoverable_failure_is_persisted_while_independent_tail_finishes(
    tmp_path: Path,
):
    """A recoverable A outcome must checkpoint before independent B advances."""

    root = _runtime(tmp_path)
    items = [
        {"cid": "a", "segment_path": "/record/a.mp4", "capacity_duration_ms": 1},
        {"cid": "b", "segment_path": "/record/b.mp4", "capacity_duration_ms": 1},
    ]
    state, state_path = _state_file(root, items)
    a_started = threading.Event()
    b_started = threading.Event()
    calls: list[str] = []
    persisted: list[dict] = []

    def producer(_date, item, **_kwargs):
        cid = item["cid"]
        calls.append(cid)
        if cid == "a":
            a_started.set()
            assert b_started.wait(timeout=5)
            return {
                "candidate_id": cid,
                "status": "failed",
                "failure_recoverable": True,
            }
        b_started.set()
        assert a_started.is_set()
        return {"candidate_id": cid, "status": "ok"}

    def produce_batch(date, batch_items, produce_fn, **kwargs):
        return _windowed(
            root,
            batch_items,
            produce_fn,
            max_parallel=2,
            prepare_only=kwargs.get("prepare_only", False),
            on_result=kwargs.get("on_result"),
        )

    def persist_state():
        persisted.append(copy.deepcopy(state))
        state_path.write_bytes(state_bytes(state))

    checkpoint, checkpoint_state = _talk_checkpoint(
        root=root,
        state=state,
        state_path=state_path,
        items=items,
        persist_state=persist_state,
    )
    before, results, entries = dispatch_prepared_lane(
        runtime_root=root,
        date=DATE,
        lane="talk",
        items=items,
        state_path=state_path,
        state=state,
        produce_batch=produce_batch,
        produce_fn=producer,
        runner=SimpleNamespace(BASE=root),
        on_result=checkpoint,
    )

    assert before == state_bytes({"pending_talk": items, "picks": [], "songs": []})
    assert entries == []
    assert calls == ["a", "b"] or calls == ["b", "a"]
    assert [row["candidate_id"] for row in results] == ["a", "b"]
    assert checkpoint_state.recoverable_failure is True
    assert persisted[0]["picks"] == [
        {"candidate_id": "a", "status": "failed", "failure_recoverable": True}
    ]
    assert persisted[0]["pending_talk"] == [items[1]]
    assert json.loads(state_path.read_bytes())["picks"] == [
        {"candidate_id": "a", "status": "failed", "failure_recoverable": True},
        {"candidate_id": "b", "status": "ok", "bundle_lifecycle": "CURRENT", "bundle_compliance": "COMPLIANT"},
    ]


def test_interrupted_ordered_callback_recovers_a_and_b_without_calls_or_active_scratch(
    tmp_path: Path,
):
    """A blocked head and completed tail both replay from exact receipts."""

    root = _runtime(tmp_path)
    source_paths = {}
    items = []
    for cid in ("a", "b"):
        source = root / "recordings" / f"{cid}.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"source-{cid}".encode("utf-8"))
        source_paths[cid] = source
        items.append(
            {
                "cid": cid,
                "segment_path": str(source),
                "capacity_duration_ms": 1,
            }
        )
    state, state_path = _state_file(root, items)
    b_completed = threading.Event()
    first_calls: list[str] = []
    callback_order: list[str] = []

    def first_producer(_date, item, **_kwargs):
        cid = item["cid"]
        first_calls.append(cid)
        if cid == "a":
            assert b_completed.wait(timeout=5), "B must complete before A is released"
        handle, result = _prepared_result(root, cid)
        if cid == "b":
            b_completed.set()
        return result

    def produce_batch(date, batch_items, produce_fn, **kwargs):
        return _windowed(
            root,
            batch_items,
            produce_fn,
            max_parallel=2,
            prepare_only=kwargs.get("prepare_only", False),
            on_result=kwargs.get("on_result"),
        )

    class CallbackFailure(RuntimeError):
        pass

    def fail_at_head(index, item, result):
        callback_order.append(item["cid"])
        if item["cid"] == "a":
            raise CallbackFailure("A state callback interrupted")
        return True

    with pytest.raises(CallbackFailure, match="A state callback interrupted"):
        dispatch_prepared_lane(
            runtime_root=root,
            date=DATE,
            lane="talk",
            items=items,
            state_path=state_path,
            state=state,
            produce_batch=produce_batch,
            produce_fn=first_producer,
            runner=SimpleNamespace(BASE=root),
            on_result=fail_at_head,
        )

    assert set(first_calls) == {"a", "b"}
    assert callback_order == ["a"]
    first_receipts = {
        cid: next(
            (root / ".prepared-deliveries" / "talk" / cid).glob(
                "*/dispatch-result-*.json"
            )
        )
        for cid in ("a", "b")
    }
    assert all(path.exists() for path in first_receipts.values())

    second_calls: list[str] = []

    def must_not_run(_date, item, **_kwargs):
        second_calls.append(item["cid"])
        raise AssertionError("exact prepared completion should be reused")

    recovered = dispatch_prepared_lane(
        runtime_root=root,
        date=DATE,
        lane="talk",
        items=items,
        state_path=state_path,
        state=state,
        produce_batch=produce_batch,
        produce_fn=must_not_run,
        runner=SimpleNamespace(BASE=root),
        on_result=lambda index, item, result: True,
    )

    _before, results, _entries = recovered
    assert second_calls == []
    assert [row["candidate_id"] for row in results] == ["a", "b"]
    assert [
        row["prepared_summary"]["prepared_delivery"]["manifest_path"]
        for row in results
    ] == [
        str(
            next(
                (root / ".prepared-deliveries" / "talk" / cid).glob(
                    "*/prepared.json"
                )
            )
        )
        for cid in ("a", "b")
    ]
    registry_path = root / "runtime-scratch" / "registry.v1.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert all(row.get("state") != "active" for row in registry["entries"])
    assert all(path.exists() for path in first_receipts.values())
    assert all(path.read_bytes() for path in source_paths.values())


def test_prepared_result_recovers_before_capacity_admission_without_provider_call(
    tmp_path: Path,
):
    root = _runtime(tmp_path)
    item = {"cid": "recover", "capacity_duration_ms": 1}
    calls: list[str] = []

    def first_producer(_date, row, **_kwargs):
        calls.append(row["cid"])
        _handle, result = _prepared_result(root, row["cid"])
        return result

    first = _windowed(root, [item], first_producer, prepare_only=True)

    def must_not_run(_date, _row, **_kwargs):
        raise AssertionError("prepared completion should be reused before producer")

    second = _windowed(
        root,
        [item],
        must_not_run,
        prepare_only=True,
        free_bytes=0,
    )

    assert calls == ["recover"]
    assert second == first
    assert second[0]["prepared_summary"]["prepared_delivery"] == first[0][
        "prepared_summary"
    ]["prepared_delivery"]


@pytest.mark.parametrize("fails", [False, True])
def test_worker_retains_dependency_input_captured_before_provider(tmp_path, fails):
    root = _runtime(tmp_path)
    item = {"cid": "snapshot", "capacity_duration_ms": 1}
    current = {"stages": {"text": "input-before-provider"}}

    def producer(_date, row, **_kwargs):
        assert row["talk_dependency_snapshot"] == current
        current["stages"]["text"] = "changed-by-provider"
        if fails:
            raise RuntimeError("provider failed")
        return {"candidate_id": row["cid"], "status": "blocked"}

    result = _windowed(root, [item], producer, dependency_snapshot=lambda *_args: copy.deepcopy(current))
    assert result[0]["talk_dependency_snapshot"] == {"stages": {"text": "input-before-provider"}}
    assert "talk_dependency_snapshot" not in item


def test_retained_worker_does_not_recapture_or_replace_original_dependency_proof(tmp_path):
    root = _runtime(tmp_path)
    item = {"cid": "original-snapshot", "capacity_duration_ms": 1}
    snapshot = {"stages": {"text": "original-input"}}
    def producer(_date, row, **_kwargs):
        return _prepared_result(root, row["cid"])[1]
    first = _windowed(root, [item], producer, prepare_only=True,
                      dependency_snapshot=lambda *_args: copy.deepcopy(snapshot),
                      dependency_fingerprint=lambda *_args: "stable-consumer-input")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("retained result must preserve its original proof")
    second = _windowed(root, [item], forbidden, prepare_only=True, free_bytes=0,
                       dependency_snapshot=forbidden,
                       dependency_fingerprint=lambda *_args: "stable-consumer-input")
    assert second == first
    assert second[0]["talk_dependency_snapshot"] == snapshot


@pytest.mark.parametrize("drift", [None, "consumer", "unsealed_bytes", "native_guard"])
def test_worker_equivalent_deploy_reprepares_under_current_guards(tmp_path, monkeypatch, drift):
    import src.autoslice.producer_completed_prepare_revalidation as native
    root = _runtime(tmp_path)
    item = {"cid": "equivalent", "capacity_duration_ms": 1}
    def producer(_date, row, **_kwargs):
        return _prepared_result(root, row["cid"])[1]
    def fingerprint(*_args):
        return "sha256:" + "1" * 64
    first = _windowed(root, [item], producer, prepare_only=True, dependency_fingerprint=fingerprint)
    old_path = Path(first[0]["prepared_summary"]["prepared_delivery"]["manifest_path"])
    original = old_path.read_bytes()
    original_checkpoint = next(old_path.parent.glob("dispatch-result-*.json")).read_bytes()
    seal_path = root / "repo/DEPLOYED_AUTHORITY_MANIFEST.json"
    seal = json.loads(seal_path.read_bytes())
    seal.pop("manifest_sha256")
    for generation in ("b", "c"):
        seal["deployed_commit"] = generation * 40
        (root / "repo/DEPLOYED_COMMIT").write_text(generation * 40 + "\n")
        consumer = root / "repo/docs/pipeline/80-package-delivery.md"
        if drift in {"consumer", "unsealed_bytes"}:
            consumer.write_bytes(b"current consumer changed")
        if drift == "consumer":
            seal["entries"]["docs/pipeline/80-package-delivery.md"] = {
                "bytes": len(consumer.read_bytes()),
                "sha256": "sha256:" + hashlib.sha256(consumer.read_bytes()).hexdigest(),
            }
        seal_path.write_text(json.dumps({**seal, "manifest_sha256": _canonical_sha256(seal)}))
        checks = []
        def validate(handle, result):
            checks.append(handle)
            assert result["candidate_id"] == "equivalent"
            if drift == "native_guard":
                raise RuntimeError("current native gate BLOCK")
        monkeypatch.setattr(native, "validate_completed_prepare_artifacts", validate)
        def forbidden(*_args, **_kwargs):
            raise AssertionError("completed package must not rerun producer")
        if drift is None:
            second = _windowed(root, [item], forbidden, prepare_only=True, free_bytes=0, dependency_fingerprint=fingerprint)
            new_path = Path(second[0]["prepared_summary"]["prepared_delivery"]["manifest_path"])
            assert new_path != old_path
            assert checks and json.loads(new_path.read_bytes())["deployed_authority"]["commit"] == generation * 40
            assert _windowed(root, [item], forbidden, prepare_only=True, free_bytes=0, dependency_fingerprint=fingerprint) == second
        else:
            with pytest.raises((ProducerBatchTransactionError, ProducerDeliveryTransactionError)):
                _windowed(root, [item], forbidden, prepare_only=True, free_bytes=0, dependency_fingerprint=fingerprint)
            assert len(list((root / ".prepared-deliveries").glob("*/*/*/dispatch-result-*.json"))) == 1
            break
        assert old_path.read_bytes() == original
        assert next(old_path.parent.glob("dispatch-result-*.json")).read_bytes() == original_checkpoint


def test_legacy_result_can_resume_current_deploy_but_cannot_invent_equivalence(tmp_path):
    root = _runtime(tmp_path)
    item = {"cid": "legacy-result", "capacity_duration_ms": 1}
    def producer(_date, row, **_kwargs):
        return _prepared_result(root, row["cid"])[1]
    first = _windowed(root, [item], producer, prepare_only=True)
    path = next((root / ".prepared-deliveries").glob("*/*/*/dispatch-result-*.json"))
    body = json.loads(path.read_bytes())
    body.pop("checkpoint_sha256")
    body.pop("deployed_authority_manifest_b64")
    body.pop("reprepared_from")
    body["schema_version"] = "producer-result-checkpoint.v1"
    body["checkpoint_sha256"] = producer_batch_transaction._sha(producer_batch_transaction._canonical_bytes(body))
    path.write_bytes(producer_batch_transaction._canonical_bytes(body))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy completion must never invent an old consumer manifest")
    assert _windowed(root, [item], forbidden, prepare_only=True, free_bytes=0) == first
    manifest = root / "repo/DEPLOYED_AUTHORITY_MANIFEST.json"
    seal = json.loads(manifest.read_bytes())
    seal.pop("manifest_sha256")
    seal["deployed_commit"] = "b" * 40
    (root / "repo/DEPLOYED_COMMIT").write_text("b" * 40 + "\n")
    manifest.write_text(json.dumps({**seal, "manifest_sha256": _canonical_sha256(seal)}))
    with pytest.raises(ProducerBatchTransactionError, match="equivalence evidence missing"):
        _windowed(root, [item], forbidden, prepare_only=True, free_bytes=0)


def test_completed_prepare_native_consumer_rejects_bare_media_handle(tmp_path):
    from src.autoslice.producer_completed_prepare_revalidation import validate_completed_prepare_artifacts
    root = _runtime(tmp_path)
    handle, result = _prepared_result(root, "incomplete-native-proof")
    with pytest.raises(ProducerDeliveryTransactionError, match="lacks native record"):
        validate_completed_prepare_artifacts(handle, result)


@pytest.mark.parametrize("drift", [None, "video", "subtitle", "publish", "tags"])
def test_song_native_revalidation_binds_completion_to_sealed_package(tmp_path, monkeypatch, drift):
    from src.autoslice.producer_completed_prepare_revalidation import validate_completed_prepare_artifacts
    from src.autoslice import producer_batch_projection, song_completion, song_delivery, song_lane

    root = _runtime(tmp_path)
    sources = root / "source"
    sources.mkdir()
    cid = "song-current"
    original = {}
    for role in ("video", "subtitle", "lyrics_alignment_report", "host_vocal_proof", "recut_manifest"):
        path = sources / role
        path.write_bytes(role.encode())
        original[role] = path
    def digest(path):
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    video_sha = digest(original["video"])
    record = {"candidate_id": "semanticsong_source"}
    active = {
        "delivery_candidate_id": cid, "source_candidate_id": record["candidate_id"],
        "publish_staging": {"title": "song title", "upload_enabled": False},
        "artifact_hashes": {"burned_video_sha256": video_sha},
        "upload_tags": {"engine": "native", "status": "OK_NO_LLM", "final_tags": ["song"], "final_tag_line": "song"},
    }
    publish = {
        "schema_version": "shadow-publish-draft.v1", "candidate_id": record["candidate_id"],
        "title": "wrong title" if drift == "publish" else "song title", "upload_enabled": False,
        "artifact_hashes": {"burned_video_sha256": video_sha},
    }
    if drift == "tags":
        active["upload_tags"]["final_tag_line"] = "unbound"
    for role, value in (("active_record", active), ("publish", publish)):
        path = sources / role
        path.write_text(json.dumps(value))
        original[role] = path
    summary = sources / "summary.json"
    summary.write_text(json.dumps({"records": [record]}))
    sealed = dict(original)
    if drift in {"video", "subtitle"}:
        different = sources / ("different-" + drift)
        different.write_bytes(b"other valid package bytes")
        sealed[drift] = different
    handle = prepare_delivery(
        runtime_root=root, lane="song", candidate_id=cid,
        artifacts=[DeliveryArtifact(role, path, root / "repo" / (cid + "-" + role), digest(path))
                   for role, path in sealed.items()],
    )
    artifacts = {"video_path": str(original["video"]), "video_sha256": video_sha,
                 "subtitle_path": str(original["subtitle"]), "subtitle_sha256": digest(original["subtitle"]),
                 "recut_manifest_path": str(original["recut_manifest"]), "recut_manifest_sha256": digest(original["recut_manifest"])}
    completion = {"alignment_report_path": str(original["lyrics_alignment_report"]),
                  "alignment_report_sha256": digest(original["lyrics_alignment_report"]),
                  "host_vocal_proof_path": str(original["host_vocal_proof"]),
                  "host_vocal_proof_sha256": digest(original["host_vocal_proof"])}
    monkeypatch.setattr(song_completion, "song_completion_evidence", lambda _record: completion)
    monkeypatch.setattr(song_delivery, "song_delivery_artifacts", lambda _record: artifacts)
    monkeypatch.setattr(song_lane, "song_delivery_ok", lambda *_args: True)
    projected = []
    monkeypatch.setattr(producer_batch_projection, "project_materialized_song", lambda *_args, **_kwargs: projected.append(True))
    result = {"selector_summary_path": str(summary), "selector_summary_sha256": digest(summary),
              "selector_record_candidate_id": record["candidate_id"]}
    if drift is None:
        validate_completed_prepare_artifacts(handle, result)
        assert projected == [True]
    else:
        with pytest.raises(RuntimeError, match="sealed|binding|tags"):
            validate_completed_prepare_artifacts(handle, result)
        assert projected == []


@pytest.mark.parametrize("drift", ["item_content", "pipeline_version", "source_stat"])
def test_prepared_result_checkpoint_drift_never_reuses_old_completion(
    tmp_path: Path, drift: str
):
    root = _runtime(tmp_path)
    source = root / "recording.mp4"
    source.write_bytes(b"source-v1")
    item = {
        "cid": f"drift-{drift}",
        "segment_path": str(source),
        "title_hint": "before",
        "capacity_duration_ms": 1,
    }
    version = ["pipeline-v1"]
    first_calls: list[str] = []

    def first_producer(_date, row, **_kwargs):
        first_calls.append(row["cid"])
        _handle, result = _prepared_result(root, row["cid"])
        return result

    _windowed(
        root,
        [item],
        first_producer,
        prepare_only=True,
        talk_fingerprint=lambda _cid: version[0],
    )
    if drift == "item_content":
        item["title_hint"] = "after"
    elif drift == "pipeline_version":
        version[0] = "pipeline-v2"
    else:
        source.write_bytes(b"source-v2-with-different-stat")

    second_calls: list[str] = []

    def second_producer(_date, row, **_kwargs):
        second_calls.append(row["cid"])
        return {"candidate_id": row["cid"], "status": "ok"}

    result = _windowed(
        root,
        [item],
        second_producer,
        prepare_only=True,
        talk_fingerprint=lambda _cid: version[0],
    )

    assert first_calls == [item["cid"]]
    assert second_calls == [item["cid"]]
    assert result == [{"candidate_id": item["cid"], "status": "ok"}]


def test_dependency_fingerprint_change_during_prepare_blocks_checkpoint(tmp_path: Path):
    root = _runtime(tmp_path)
    item = {"cid": "changed-during-prepare", "capacity_duration_ms": 1}
    version = ["stage-v1"]

    def producer(_date, row, **_kwargs):
        _handle, result = _prepared_result(root, row["cid"])
        version[0] = "stage-v2"
        return result

    with pytest.raises(ProducerBatchTransactionError, match="changed during preparation"):
        _windowed(
            root, [item], producer, prepare_only=True,
            talk_fingerprint=lambda _cid: version[0],
        )
    assert not list((root / ".prepared-deliveries").glob("*/*/*/dispatch-result-*.json"))


@pytest.mark.parametrize("tamper", ["receipt", "handle"])
def test_tampered_prepared_receipt_or_handle_is_rejected_before_producer(
    tmp_path: Path, tamper: str
):
    root = _runtime(tmp_path)
    item = {"cid": f"tampered-{tamper}", "capacity_duration_ms": 1}
    handle_box: list[object] = []

    def producer(_date, row, **_kwargs):
        handle, result = _prepared_result(root, row["cid"])
        handle_box.append(handle)
        return result

    _windowed(root, [item], producer, prepare_only=True)
    handle = handle_box[0]
    candidate_root = root / ".prepared-deliveries" / "talk" / item["cid"]
    receipt = next(candidate_root.glob("*/dispatch-result-*.json"))
    if tamper == "receipt":
        document = json.loads(receipt.read_text(encoding="utf-8"))
        document["result"]["status"] = "ok"
        receipt.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    else:
        manifest = handle.manifest_path
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["lane"] = "song"
        manifest.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")

    calls = 0

    def must_not_run(_date, _row, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("tampered checkpoint must fail before producer")

    with pytest.raises((ProducerBatchTransactionError, ProducerDeliveryTransactionError)):
        _windowed(
            root,
            [item],
            must_not_run,
            prepare_only=True,
            free_bytes=1 << 40,
        )
    assert calls == 0


def test_system_exit_zero_becomes_failed_result_and_does_not_kill_ordered_tail(
    tmp_path: Path,
):
    root = _runtime(tmp_path)
    items = [{"cid": cid, "capacity_duration_ms": 1} for cid in ("a", "b")]
    calls: list[str] = []

    def producer(_date, row, **_kwargs):
        calls.append(row["cid"])
        if row["cid"] == "a":
            raise SystemExit(0)
        return {"candidate_id": "b", "status": "ok"}

    results = _windowed(root, items, producer, max_parallel=1)

    assert calls == ["a", "b"]
    assert [row["candidate_id"] for row in results] == ["a", "b"]
    assert results[0]["status"] == "failed"
    assert results[0]["reason_codes"] == ["PRODUCE_EXIT_WITHOUT_RESULT"]
    assert results[1]["status"] == "ok"
    assert results[0]["status"] != "READY"


def test_stop_at_start_preserves_disabled_marker_and_leaves_entire_tail(
    tmp_path: Path,
):
    root = _runtime(tmp_path)
    disabled = root / "DISABLED"
    disabled.write_bytes(b"")
    marker_before = (disabled.read_bytes(), disabled.stat().st_mode)
    items = [{"cid": cid, "capacity_duration_ms": 1} for cid in ("a", "b", "c")]
    calls: list[str] = []

    def producer(_date, row, **_kwargs):
        calls.append(row["cid"])
        return {"candidate_id": row["cid"], "status": "ok"}

    results = _windowed(
        root,
        items,
        producer,
        stop_requested_fn=lambda: True,
    )

    assert results == []
    assert calls == []
    assert items == [
        {"cid": cid, "capacity_duration_ms": 1} for cid in ("a", "b", "c")
    ]
    assert (disabled.read_bytes(), disabled.stat().st_mode) == marker_before


def test_stop_midbatch_leaves_tail_and_preserves_disabled_marker(
    tmp_path: Path,
):
    root = _runtime(tmp_path)
    disabled = root / "DISABLED"
    disabled.write_bytes(b"")
    marker_before = (disabled.read_bytes(), disabled.stat().st_mode)
    items = [{"cid": cid, "capacity_duration_ms": 1} for cid in ("a", "b", "c")]
    calls: list[str] = []
    checks = iter((False, True))

    def producer(_date, row, **_kwargs):
        calls.append(row["cid"])
        return {"candidate_id": row["cid"], "status": "ok"}

    results = _windowed(
        root,
        items,
        producer,
        stop_requested_fn=lambda: next(checks),
    )

    assert calls == ["a"]
    assert results == [{"candidate_id": "a", "status": "ok"}]
    assert [row["cid"] for row in items[1:]] == ["b", "c"]
    assert (disabled.read_bytes(), disabled.stat().st_mode) == marker_before


def test_cover_state_transition_rejects_candidate_drift_and_then_uses_live_lease(
    tmp_path: Path,
):
    root = _runtime(tmp_path)
    state_path = root / "state" / f"{DATE}.json"
    state_path.parent.mkdir()
    target_before = {
        "candidate_id": "target",
        "status": "cover_pending",
        "cover_generation": 1,
    }
    neighbor_before = {"candidate_id": "neighbor", "status": "queued"}
    initial = {"picks": [target_before, neighbor_before], "songs": []}
    state_path.write_bytes(state_bytes(initial))

    target_external = dict(target_before, cover_generation=2)
    neighbor_external = dict(neighbor_before, status="updated_out_of_band")
    external = {"picks": [target_external, neighbor_external], "songs": []}
    state_path.write_bytes(state_bytes(external))
    stale_state = copy.deepcopy(initial)
    transition_calls: list[str] = []

    with pytest.raises(
        RunnerStateWritebackError, match="COVER_STATE_TRANSITION_CANDIDATE_DRIFT"
    ):
        cover_state_transition(
            runtime_root=root,
            date=DATE,
            state=stale_state,
            candidate_id="target",
            expected_record=target_before,
            transition=lambda _live: transition_calls.append("must-not-run"),
            persist_state=lambda: transition_calls.append("must-not-persist"),
        )

    assert transition_calls == []
    assert json.loads(state_path.read_bytes()) == external

    live_state = copy.deepcopy(external)
    observed_leases = []

    def transition(live):
        observed_leases.append(current_runner_commit_lease(runtime_root=root))
        live["cover_generation"] = 3

    def persist():
        lease = current_runner_commit_lease(runtime_root=root)
        assert lease is not None
        write_state(
            state_path,
            live_state,
            runtime_root=root,
            updated_at="2026-10-13T00:00:00Z",
            log=lambda _message: None,
            lease=lease,
        )

    result = cover_state_transition(
        runtime_root=root,
        date=DATE,
        state=live_state,
        candidate_id="target",
        expected_record=target_external,
        transition=transition,
        persist_state=persist,
    )

    assert observed_leases and observed_leases[0] is not None
    assert result["cover_generation"] == 3
    written = json.loads(state_path.read_bytes())
    assert written["picks"][0]["cover_generation"] == 3
    assert written["picks"][1] == neighbor_external


def test_prepared_checkpoint_io_error_stops_before_dispatching_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _runtime(tmp_path)
    items = [{"cid": cid, "capacity_duration_ms": 1} for cid in ("a", "b", "c")]
    calls: list[str] = []

    def producer(_date, row, **_kwargs):
        calls.append(row["cid"])
        if row["cid"] != "a":
            raise AssertionError("checkpoint IO failure must leave the tail undispatched")
        _handle, result = _prepared_result(root, "a")
        return result

    def fail_fsync(_directory: Path) -> None:
        raise OSError("simulated checkpoint fsync failure")

    monkeypatch.setattr(producer_batch_transaction, "_fsync_directory", fail_fsync)

    with pytest.raises(
        ProducerBatchTransactionError, match="could not be persisted"
    ):
        _windowed(root, items, producer, prepare_only=True, max_parallel=1)

    assert calls == ["a"]


def test_completed_spec_source_drift_rejects_recovery_without_new_producer_call(
    tmp_path: Path,
):
    root = _runtime(tmp_path)
    second_source = root / "recordings" / "second.mp4"
    second_source.parent.mkdir(parents=True)
    second_source.write_bytes(b"second-source-v1")
    items = [{"cid": "A", "capacity_duration_ms": 1}]
    producer_calls: list[str] = []

    def producer(_date, row, **_kwargs):
        producer_calls.append(row["cid"])
        spec_path = root / "out" / DATE / "spec_A.json"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(
            json.dumps(
                {
                    "pieces": [
                        {
                            "remote_media": str(second_source),
                            "start_ms": 0,
                            "end_ms": 1,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        _handle, result = _prepared_result(root, "A")
        return result

    class CallbackBlocked(RuntimeError):
        pass

    def interrupt_callback(_index: int, _item: dict, _result: dict) -> None:
        raise CallbackBlocked("interrupt after prepared result checkpoint")

    with pytest.raises(CallbackBlocked):
        _windowed(
            root,
            items,
            producer,
            prepare_only=True,
            max_parallel=1,
            on_result=interrupt_callback,
        )
    assert producer_calls == ["A"]

    second_source.write_bytes(b"second-source-drifted")
    recovery_calls: list[str] = []

    def must_not_run(_date, row, **_kwargs):
        recovery_calls.append(row["cid"])
        raise AssertionError("source-drifted completion must fail before producer")

    with pytest.raises(
        ProducerBatchTransactionError, match="source dependencies drift"
    ):
        _windowed(
            root,
            items,
            must_not_run,
            prepare_only=True,
            max_parallel=1,
            free_bytes=1 << 40,
        )

    assert recovery_calls == []
    assert producer_calls == ["A"]


def test_checkpoint_blocked_outcome_logs_prefix_and_deferred_candidate_ids(
    tmp_path: Path,
):
    root = _runtime(tmp_path)
    items = [{"cid": cid, "capacity_duration_ms": 1} for cid in ("a", "b", "c")]
    producer_calls: list[str] = []
    logs: list[str] = []

    def producer(_date, row, **_kwargs):
        producer_calls.append(row["cid"])
        return {"candidate_id": row["cid"], "status": "ok"}

    def block_callback(_index: int, _item: dict, _result: dict) -> None:
        raise RuntimeError("state checkpoint unavailable")

    with pytest.raises(RuntimeError, match="state checkpoint unavailable"):
        _windowed(
            root,
            items,
            producer,
            max_parallel=1,
            on_result=block_callback,
            log=logs.append,
        )

    outcome = next(
        json.loads(message)
        for message in logs
        if message.startswith("{")
        and json.loads(message).get("schema_version")
        == "producer-dispatch-outcome.v1"
    )
    assert outcome["status"] == "CHECKPOINT_BLOCKED"
    assert outcome["checkpointed_count"] == 0
    assert outcome["uncheckpointed_candidate_ids"] == ["a"]
    assert outcome["deferred_candidate_ids"] == ["b", "c"]
    assert producer_calls == ["a"]
