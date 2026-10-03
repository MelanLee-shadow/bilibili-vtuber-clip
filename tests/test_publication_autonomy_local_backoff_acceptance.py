"""Normal-runner acceptance for ready-first local-backoff fallthrough."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.session_autoslice as runner
from src.autoslice.publication_queue import consume_ready_publication_queue
from src.autoslice.publication_queue_backoff import (
    STATE_FILENAME,
    evaluate_upload_backoff,
    record_queue_result,
)
from src.autoslice.publication_readiness import (
    READY_FOR_SERIAL_UPLOAD,
    READY_TO_PREPARE,
)

NOW = 2_000_000_000.0


def _manifest(runtime: Path, candidate_id: str) -> Path:
    path = runtime / "out" / candidate_id / f"{candidate_id}.upload_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "candidate_id": candidate_id,
                "video": {
                    "sha256": hashlib.sha256(candidate_id.encode()).hexdigest()
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def _upload_row(candidate_id: str, date: str, manifest: Path) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "recording_date": date,
        "lane": "talk",
        "category": READY_FOR_SERIAL_UPLOAD,
        "package_dependencies": {"upload_manifest": str(manifest)},
    }


def _prepare_row(candidate_id: str, date: str) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "recording_date": date,
        "lane": "talk",
        "category": READY_TO_PREPARE,
        "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"],
        "package_dependencies": {"record": "synthetic"},
    }


def _graph(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "publication-readiness-graph.v1",
        "observational_only": True,
        "graph_blockers": [],
        "rows": rows,
    }


def _prepare_manifest(
    row: dict[str, object],
    *,
    runtime_root: Path,
    **_kwargs: object,
) -> dict[str, object]:
    candidate_id = str(row["candidate_id"])
    manifest = _manifest(runtime_root, candidate_id)
    return {"status": "PREPARED", "manifest": str(manifest)}


def test_runner_prepares_then_uploads_next_ready_around_local_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    runtime = tmp_path / "runtime"
    repository.mkdir()
    runtime.mkdir()
    (runtime / "reports").mkdir()
    (runtime / "reports/upload_ledger.jsonl").write_text("", encoding="utf-8")
    blocked_id = "newer-backed-off"
    next_id = "next-ready"
    blocked = _manifest(runtime, blocked_id)
    recorded = record_queue_result(
        runtime_root=runtime,
        manifest_path=blocked,
        rc=17,
        status="ACTION_FAILED",
        ledger_entries=[],
        now_epoch=NOW,
    )
    assert recorded["status"] == "RECORDED"
    state_path = runtime / "reports" / STATE_FILENAME
    state_before = state_path.read_bytes()
    heartbeats: list[str] = []
    logs: list[str] = []
    uploader_calls: list[list[str]] = []
    queue_results: list[dict[str, object]] = []
    published: set[str] = set()

    def graph_builder(**_kwargs: object) -> dict[str, object]:
        rows: list[dict[str, object]] = [
            _upload_row(blocked_id, "2026-08-21", blocked)
        ]
        next_manifest = runtime / "out" / next_id / f"{next_id}.upload_manifest.json"
        if next_id not in published:
            rows.append(
                _upload_row(next_id, "2026-08-20", next_manifest)
                if next_manifest.is_file()
                else _prepare_row(next_id, "2026-08-20")
            )
        return _graph(rows)

    original_consumer = consume_ready_publication_queue

    def upload(argv: list[str]) -> int:
        uploader_calls.append(argv)
        manifest = Path(argv[argv.index("--manifest") + 1])
        published.add(str(json.loads(manifest.read_text())["candidate_id"]))
        return 0

    def consume(**kwargs: object) -> dict[str, object]:
        result = original_consumer(
            **kwargs,
            graph_builder=graph_builder,
            upload_call=upload,
            manifest_prepare_call=_prepare_manifest,
            ledger_reader=lambda _path: ([], []),
            ledger_guard=lambda *_args: (_ for _ in ()).throw(
                AssertionError("empty ledger has no guard calls")
            ),
        )
        queue_results.append(result)
        return result

    monkeypatch.setattr(runner, "BASE", runtime)
    monkeypatch.setattr(runner, "REPO_ROOT", repository)
    monkeypatch.setenv("AUTOSLICE_AUTHORIZED_UPLOAD_QUEUE_ENABLED", "1")
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda _live: {})
    monkeypatch.setattr(runner, "_live_hold_active", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(runner, "list_dates", lambda: [])
    monkeypatch.setattr(
        runner.review_package_poststage,
        "backfill_terminal_review_packages",
        lambda _runner: None,
    )
    monkeypatch.setattr(
        runner.publication_queue,
        "consume_ready_publication_queue",
        consume,
    )
    monkeypatch.setattr(runner, "write_heartbeat", heartbeats.append)
    monkeypatch.setattr(runner, "log", logs.append)

    assert runner.tick() == 0
    prepared = runtime / "out" / next_id / f"{next_id}.upload_manifest.json"
    assert prepared.is_file()
    assert uploader_calls == []
    assert state_path.read_bytes() == state_before
    assert "publication_queue=MANIFEST_PREPARED" in heartbeats[0]

    assert runner.tick() == 0
    assert len(uploader_calls) == 1
    assert Path(uploader_calls[0][uploader_calls[0].index("--manifest") + 1]) == prepared
    assert state_path.read_bytes() == state_before
    assert "publication_queue=ACTION_COMPLETED" in heartbeats[1]
    assert "publication_skipped_backoff=1" in heartbeats[1]

    assert runner.tick() == 0
    assert len(uploader_calls) == 1
    assert state_path.read_bytes() == state_before
    assert "publication_queue=RETRY_BACKOFF" in heartbeats[2]
    assert [row["status"] for row in queue_results] == [
        "MANIFEST_PREPARED",
        "ACTION_COMPLETED",
        "RETRY_BACKOFF",
    ]
    assert published == {next_id}
    blocked_gate = evaluate_upload_backoff(
        runtime_root=runtime,
        manifest_path=blocked,
        ledger_entries=[],
    )
    assert blocked_gate["status"] == "BACKOFF"
    assert blocked_gate["scope"] == "MANIFEST"
    assert hashlib.sha256(state_path.read_bytes()).hexdigest() == hashlib.sha256(
        state_before
    ).hexdigest()
