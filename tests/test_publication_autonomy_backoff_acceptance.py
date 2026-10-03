"""Normal-runner acceptance for local work during durable upload backoff."""

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


def test_runner_prepares_locally_then_remains_quota_backed_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    runtime = tmp_path / "runtime"
    repository.mkdir()
    runtime.mkdir()
    (runtime / "reports").mkdir()
    (runtime / "reports/upload_ledger.jsonl").write_text("", encoding="utf-8")
    blocked = _manifest(runtime, "existing-upload")
    prepare_id = "prepare-under-quota"
    recorded = record_queue_result(
        runtime_root=runtime,
        manifest_path=blocked,
        rc=8,
        status="QUOTA_BLOCKED",
        ledger_entries=[],
        now_epoch=NOW,
    )
    assert recorded["status"] == "RECORDED"
    state_path = runtime / "reports" / STATE_FILENAME
    state_before = state_path.read_bytes()
    uploader_calls: list[list[str]] = []
    heartbeats: list[str] = []
    logs: list[str] = []
    tick_number = 0

    def graph_builder(**_kwargs: object) -> dict[str, object]:
        prepared = runtime / "out" / prepare_id / f"{prepare_id}.upload_manifest.json"
        rows = [_upload_row("existing-upload", "2026-08-21", blocked)]
        rows.append(
            _upload_row(prepare_id, "2026-08-22", prepared)
            if prepared.is_file()
            else _prepare_row(prepare_id, "2026-08-22")
        )
        return _graph(rows)

    original_consumer = consume_ready_publication_queue

    def consume(**kwargs: object) -> dict[str, object]:
        nonlocal tick_number
        tick_number += 1
        return original_consumer(
            **kwargs,
            graph_builder=graph_builder,
            upload_call=lambda argv: uploader_calls.append(argv) or 0,
            manifest_prepare_call=_prepare_manifest,
            ledger_reader=lambda _path: ([], []),
            ledger_guard=lambda *_args: (_ for _ in ()).throw(
                AssertionError("empty ledger has no guard calls")
            ),
        )

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
    prepared = runtime / "out" / prepare_id / f"{prepare_id}.upload_manifest.json"
    assert prepared.is_file()
    assert state_path.read_bytes() == state_before
    assert uploader_calls == []
    assert "publication_queue=MANIFEST_PREPARED" in heartbeats[0]
    assert "publication_skipped_backoff=1" in heartbeats[0]

    assert runner.tick() == 0
    assert state_path.read_bytes() == state_before
    assert uploader_calls == []
    assert "publication_queue=RETRY_BACKOFF" in heartbeats[1]
    quota_logs = [row for row in logs if "PUBLICATION_QUEUE_QUOTA_BACKOFF" in row]
    assert quota_logs

    original_gate = evaluate_upload_backoff(
        runtime_root=runtime,
        manifest_path=blocked,
        ledger_entries=[],
        now_epoch=NOW + 3,
    )
    prepared_gate = evaluate_upload_backoff(
        runtime_root=runtime,
        manifest_path=prepared,
        ledger_entries=[],
        now_epoch=NOW + 3,
    )
    assert original_gate["status"] == "BACKOFF"
    assert prepared_gate["status"] == "BACKOFF"
    assert original_gate["scope"] == prepared_gate["scope"] == "GLOBAL_QUOTA"
    assert hashlib.sha256(state_path.read_bytes()).hexdigest() == hashlib.sha256(
        state_before
    ).hexdigest()
