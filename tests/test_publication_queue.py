from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.authorized_upload as authorized_upload
from src.autoslice.publication_queue import (
    consume_ready_publication_queue,
    plan_next_publication_action,
)
from src.autoslice.publication_queue_backoff import (
    GENERIC_BASE_SECONDS,
    QUOTA_SECONDS,
    STATE_FILENAME,
)
from src.autoslice.publication_readiness import READY_FOR_SERIAL_UPLOAD


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    return repo, runtime


def _manifest(runtime: Path, name: str) -> Path:
    path = runtime / "out" / name / f"{name}.upload_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "video": {
                    "sha256": hashlib.sha256(name.encode()).hexdigest()
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path.resolve()


def _graph(*rows: dict, blockers: list[dict] | None = None) -> dict:
    return {
        "schema_version": "publication-readiness-graph.v1",
        "observational_only": True,
        "graph_blockers": blockers or [],
        "rows": list(rows),
    }


def _ready_row(candidate: str, date: str, manifest: Path) -> dict:
    return {
        "candidate_id": candidate,
        "recording_date": date,
        "lane": "talk",
        "category": READY_FOR_SERIAL_UPLOAD,
        "package_dependencies": {"upload_manifest": str(manifest)},
    }


def _attempt_fields(manifest: Path, *, attempt_id: str) -> dict[str, object]:
    document = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        "attempt_id": attempt_id,
        "artifact_id": f"artifact-{attempt_id}",
        "video_sha256": document["video"]["sha256"],
        "cover_sha256": "c" * 64,
        "manifest": str(manifest.resolve()),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "uploader": "queue-test-uploader",
    }


def _append_started_attempt(
    ledger: Path,
    manifest: Path,
    *,
    attempt_id: str = "attempt-started",
) -> dict[str, object]:
    fields = _attempt_fields(manifest, attempt_id=attempt_id)
    authorized_upload.append_ledger(
        ledger,
        {"event": "UPLOAD_ATTEMPT_STARTED", "at": "2026-09-30T12:00:00Z", **fields},
    )
    return fields


def _append_posted_attempt(
    ledger: Path,
    manifest: Path,
    *,
    attempt_id: str = "attempt-posted",
    bvid: str = "BV1POSTED123",
) -> dict[str, object]:
    fields = _attempt_fields(manifest, attempt_id=attempt_id)
    authorized_upload.append_ledger(
        ledger,
        {"event": "UPLOAD_ATTEMPT_STARTED", "at": "2026-09-30T12:00:00Z", **fields},
    )
    authorized_upload.append_ledger(
        ledger,
        {
            "event": "UPLOAD_ATTEMPT_FINISHED",
            "at": "2026-09-30T12:00:01Z",
            **fields,
            "uploader_rc": 0,
            "rc": 6,
            "bvid": bvid,
            "public_verify_status": "POSTED_UNVERIFIED",
        },
    )
    return fields


def _append_verified_attempt(
    ledger: Path,
    manifest: Path,
    *,
    attempt_id: str = "attempt-uploaded",
    bvid: str = "BV1UPLOADED1",
) -> None:
    fields = _attempt_fields(manifest, attempt_id=attempt_id)
    authorized_upload.append_ledger(
        ledger,
        {"event": "UPLOAD_ATTEMPT_STARTED", "at": "2026-09-30T12:00:00Z", **fields},
    )
    authorized_upload.append_ledger(
        ledger,
        {
            "event": "UPLOAD_ATTEMPT_FINISHED",
            "at": "2026-09-30T12:00:01Z",
            **fields,
            "uploader_rc": 0,
            "rc": 6,
            "bvid": bvid,
            "public_verify_status": "POSTED_UNVERIFIED",
        },
    )
    authorized_upload.append_ledger(
        ledger,
        {
            "event": "UPLOAD_PUBLICATION_VERIFIED",
            "at": "2026-09-30T12:00:02Z",
            **fields,
            "rc": 0,
            "bvid": bvid,
            "public_verify_status": "VERIFIED_PUBLIC",
        },
    )


def _append_publication_verified(
    ledger: Path,
    manifest: Path,
    *,
    attempt_id: str,
    bvid: str,
) -> None:
    fields = _attempt_fields(manifest, attempt_id=attempt_id)
    authorized_upload.append_ledger(
        ledger,
        {
            "event": "UPLOAD_PUBLICATION_VERIFIED",
            "at": "2026-09-30T12:00:02Z",
            **fields,
            "rc": 0,
            "bvid": bvid,
            "public_verify_status": "VERIFIED_PUBLIC",
        },
    )


def test_canonical_default_action_resolves_ledger_accessors_and_requires_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "canonical-missing-commit")
    monkeypatch.setattr(authorized_upload, "main", lambda _argv: 0)

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(
            _ready_row("canonical-missing-commit", "2026-08-21", manifest)
        ),
    )

    assert result["status"] == "LEDGER_BLOCKED"
    assert result["reason_codes"] == ["PUBLICATION_ACTION_OUTCOME_UNVERIFIED"]
    assert result["ledger_commit"]["status"] == "UNVERIFIED"
    assert result["action_result_status"] == "ACTION_COMPLETED"
    assert result["side_effect_attempted"] is True
    assert not (runtime / "reports" / STATE_FILENAME).exists()


def test_canonical_default_upload_completes_only_after_terminal_ledger_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "canonical-committed")
    ledger = runtime / "reports" / "upload_ledger.jsonl"

    def commit(_argv: list[str]) -> int:
        _append_verified_attempt(ledger, manifest)
        return 0

    monkeypatch.setattr(authorized_upload, "main", commit)
    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(
            _ready_row("canonical-committed", "2026-08-21", manifest)
        ),
    )

    assert result["status"] == "ACTION_COMPLETED"
    assert result["ledger_commit"]["status"] == "COMMITTED"
    assert result["ledger_commit"]["terminal_event"] == "UPLOAD_PUBLICATION_VERIFIED"
    assert result["backoff_state"]["status"] == "CLEARED"


def test_canonical_default_season_rc0_without_terminal_commit_stays_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "canonical-season")
    ledger = runtime / "reports" / "upload_ledger.jsonl"
    _append_posted_attempt(
        ledger,
        manifest,
        attempt_id="attempt-season",
        bvid="BV1POSTED123",
    )
    monkeypatch.setattr(authorized_upload, "main", lambda _argv: 0)

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: pytest.fail(
            "posted recovery must precede readiness"
        ),
    )

    assert result["status"] == "LEDGER_BLOCKED"
    assert result["reason_codes"] == ["PUBLICATION_ACTION_OUTCOME_UNVERIFIED"]
    assert result["ledger_commit"]["detail"] == (
        "successful action ledger status is posted_unverified"
    )


def test_canonical_default_season_completes_after_matching_terminal_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "canonical-season-committed")
    ledger = runtime / "reports" / "upload_ledger.jsonl"
    _append_posted_attempt(
        ledger,
        manifest,
        attempt_id="attempt-season-committed",
        bvid="BV1POSTED123",
    )

    def commit(_argv: list[str]) -> int:
        _append_publication_verified(
            ledger,
            manifest,
            attempt_id="attempt-season-committed",
            bvid="BV1POSTED123",
        )
        return 0

    monkeypatch.setattr(authorized_upload, "main", commit)
    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: pytest.fail(
            "posted recovery must precede readiness"
        ),
    )

    assert result["status"] == "ACTION_COMPLETED"
    assert result["ledger_commit"]["status"] == "COMMITTED"
    assert result["ledger_commit"]["bvid"] == "BV1POSTED123"


def test_canonical_default_rc6_started_only_is_immediately_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "canonical-rc6-started")
    ledger = runtime / "reports" / "upload_ledger.jsonl"

    def leave_started(_argv: list[str]) -> int:
        _append_started_attempt(ledger, manifest)
        return 6

    monkeypatch.setattr(authorized_upload, "main", leave_started)
    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(
            _ready_row("canonical-rc6-started", "2026-08-21", manifest)
        ),
    )

    assert result["status"] == "LEDGER_BLOCKED"
    assert result["reason_codes"] == ["UPLOAD_ATTEMPT_OUTCOME_UNRESOLVED"]
    assert result["ledger_commit"]["status"] == "UNRESOLVED"
    assert result["ledger_commit"]["attempt_id"] == "attempt-started"
    assert not (runtime / "reports" / STATE_FILENAME).exists()


def test_canonical_default_rc6_posted_is_followup_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "canonical-rc6-posted")
    ledger = runtime / "reports" / "upload_ledger.jsonl"

    def leave_posted(_argv: list[str]) -> int:
        _append_posted_attempt(ledger, manifest)
        return 6

    monkeypatch.setattr(authorized_upload, "main", leave_posted)
    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(
            _ready_row("canonical-rc6-posted", "2026-08-21", manifest)
        ),
    )

    assert result["status"] == "FOLLOWUP_PENDING"
    assert result["ledger_commit"]["status"] == "POSTED_UNVERIFIED"
    assert result["ledger_commit"]["bvid"] == "BV1POSTED123"
    assert result["backoff_state"]["status"] == "CLEARED"


def test_canonical_default_rc6_without_ledger_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "canonical-rc6-absent")
    monkeypatch.setattr(authorized_upload, "main", lambda _argv: 6)

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(
            _ready_row("canonical-rc6-absent", "2026-08-21", manifest)
        ),
    )

    assert result["status"] == "LEDGER_BLOCKED"
    assert result["reason_codes"] == ["PUBLICATION_ACTION_OUTCOME_UNVERIFIED"]
    assert result["ledger_commit"]["status"] == "UNVERIFIED"
    assert not (runtime / "reports" / STATE_FILENAME).exists()


def test_canonical_default_rc6_with_terminal_commit_uses_ledger_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "canonical-rc6-committed")
    ledger = runtime / "reports" / "upload_ledger.jsonl"

    def commit_but_return_followup(_argv: list[str]) -> int:
        _append_verified_attempt(ledger, manifest)
        return 6

    monkeypatch.setattr(authorized_upload, "main", commit_but_return_followup)
    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(
            _ready_row("canonical-rc6-committed", "2026-08-21", manifest)
        ),
    )

    assert result["status"] == "ACTION_COMPLETED"
    assert result["ledger_commit"]["status"] == "COMMITTED"
    assert result["backoff_state"]["status"] == "CLEARED"


def test_disabled_queue_does_not_read_graph_or_ledger(tmp_path: Path) -> None:
    repo, runtime = _roots(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled queue must not inspect or mutate")

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=False,
        graph_builder=forbidden,
        upload_call=forbidden,
        ledger_reader=forbidden,
        ledger_guard=forbidden,
    )

    assert result["status"] == "DISABLED"
    assert result["side_effect_attempted"] is False


@pytest.mark.parametrize(
    ("marker", "reason"),
    [
        ("DISABLED", "PRODUCTION_DISABLED"),
        ("deploy.guard", "DEPLOYMENT_IN_PROGRESS"),
        ("AUTO_UPLOAD", "LEGACY_AUTO_UPLOAD_MARKER_PRESENT"),
    ],
)
def test_runtime_markers_block_before_graph_or_upload(
    tmp_path: Path,
    marker: str,
    reason: str,
) -> None:
    repo, runtime = _roots(tmp_path)
    (runtime / marker).touch()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("runtime marker must stop queue")

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=forbidden,
        upload_call=forbidden,
    )

    assert result["status"] == "BLOCKED_RUNTIME"
    assert result["reason_codes"] == [reason]
    assert result["side_effect_attempted"] is False


def test_posted_unverified_recovery_precedes_new_ready_upload(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "posted")
    calls: list[list[str]] = []

    def graph_forbidden(**_kwargs):
        raise AssertionError("ledger recovery must precede readiness graph")

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=graph_forbidden,
        upload_call=lambda argv: calls.append(argv) or 0,
        ledger_reader=lambda _path: ([{"video_sha256": "video-sha"}], []),
        ledger_guard=lambda _path, _sha: (
            "posted_unverified",
            {
                "manifest": str(manifest),
                "bvid": "BV1POSTED123",
                "attempt_id": "attempt-1",
            },
            [],
        ),
    )

    assert result["status"] == "ACTION_COMPLETED"
    assert result["action"]["action"] == "SEASON_ADD"
    assert calls == [
        [
            "season-add",
            "--manifest",
            str(manifest),
            "--bvid",
            "BV1POSTED123",
            "--ledger",
            str(runtime / "reports/upload_ledger.jsonl"),
        ]
    ]


def test_unresolved_upload_intent_blocks_all_actions(tmp_path: Path) -> None:
    repo, runtime = _roots(tmp_path)
    called = False

    def upload(_argv: list[str]) -> int:
        nonlocal called
        called = True
        return 0

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: pytest.fail("ledger ambiguity is global"),
        upload_call=upload,
        ledger_reader=lambda _path: ([{"video_sha256": "video-sha"}], []),
        ledger_guard=lambda _path, _sha: (
            "unresolved",
            {"attempt_id": "ambiguous"},
            [],
        ),
    )

    assert result["status"] == "BLOCKED_LEDGER"
    assert result["reason_codes"] == ["UPLOAD_ATTEMPT_OUTCOME_UNRESOLVED"]
    assert result["side_effect_attempted"] is False
    assert called is False


@pytest.mark.parametrize(
    "ledger_order",
    [("A", "B"), ("B", "A")],
)
def test_unresolved_hash_globally_blocks_posted_recovery_in_any_order(
    tmp_path: Path,
    ledger_order: tuple[str, str],
) -> None:
    repo, runtime = _roots(tmp_path)
    posted_manifest = _manifest(runtime, "posted-A")
    visited: list[str] = []
    callbacks = {"graph": 0, "upload": 0, "prepare": 0}
    states = {
        "A": (
            "posted_unverified",
            {
                "manifest": str(posted_manifest),
                "bvid": "BV1POSTEDA",
                "attempt_id": "attempt-A",
            },
            [],
        ),
        "B": ("unresolved", {"attempt_id": "attempt-B"}, []),
    }

    def graph_builder(**_kwargs):
        callbacks["graph"] += 1
        return _graph()

    def upload_call(_argv: list[str]) -> int:
        callbacks["upload"] += 1
        return 0

    def prepare_call(*_args, **_kwargs):
        callbacks["prepare"] += 1
        return {"status": "PREPARED"}

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=graph_builder,
        upload_call=upload_call,
        manifest_prepare_call=prepare_call,
        ledger_reader=lambda _path: (
            [{"video_sha256": value} for value in ledger_order],
            [],
        ),
        ledger_guard=lambda _path, value: (
            visited.append(value) or states[value]
        ),
    )

    assert visited == list(ledger_order)
    assert result["status"] == "BLOCKED_LEDGER"
    assert result["reason_codes"] == ["UPLOAD_ATTEMPT_OUTCOME_UNRESOLVED"]
    assert result["attempt_id"] == "attempt-B"
    assert result["side_effect_attempted"] is False
    assert callbacks == {"graph": 0, "upload": 0, "prepare": 0}


@pytest.mark.parametrize(
    "ledger_order",
    [("A", "B"), ("B", "A")],
)
def test_guard_problem_globally_blocks_posted_recovery_in_any_order(
    tmp_path: Path,
    ledger_order: tuple[str, str],
) -> None:
    repo, runtime = _roots(tmp_path)
    posted_manifest = _manifest(runtime, "posted-A")
    visited: list[str] = []
    callbacks = {"graph": 0, "upload": 0, "prepare": 0}
    states = {
        "A": (
            "posted_unverified",
            {
                "manifest": str(posted_manifest),
                "bvid": "BV1POSTEDA",
                "attempt_id": "attempt-A",
            },
            [],
        ),
        "B": (None, None, ["guard-B-invalid"]),
    }

    def graph_builder(**_kwargs):
        callbacks["graph"] += 1
        return _graph()

    def upload_call(_argv: list[str]) -> int:
        callbacks["upload"] += 1
        return 0

    def prepare_call(*_args, **_kwargs):
        callbacks["prepare"] += 1
        return {"status": "PREPARED"}

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=graph_builder,
        upload_call=upload_call,
        manifest_prepare_call=prepare_call,
        ledger_reader=lambda _path: (
            [{"video_sha256": value} for value in ledger_order],
            [],
        ),
        ledger_guard=lambda _path, value: (
            visited.append(value) or states[value]
        ),
    )

    assert visited == list(ledger_order)
    assert result["status"] == "BLOCKED_LEDGER"
    assert result["reason_codes"] == ["UPLOAD_LEDGER_INVALID"]
    assert result["detail"] == ["guard-B-invalid"]
    assert result["side_effect_attempted"] is False
    assert callbacks == {"graph": 0, "upload": 0, "prepare": 0}


@pytest.mark.parametrize(
    "ledger_order",
    [("A", "B"), ("B", "A")],
)
def test_two_posted_recoveries_preserve_first_ledger_hash_priority(
    tmp_path: Path,
    ledger_order: tuple[str, str],
) -> None:
    repo, runtime = _roots(tmp_path)
    manifests = {value: _manifest(runtime, f"posted-{value}") for value in ("A", "B")}
    visited: list[str] = []
    upload_calls: list[list[str]] = []
    callbacks = {"graph": 0, "prepare": 0}

    def graph_builder(**_kwargs):
        callbacks["graph"] += 1
        return _graph()

    def prepare_call(*_args, **_kwargs):
        callbacks["prepare"] += 1
        return {"status": "PREPARED"}

    def guard(_path: Path, value: str):
        visited.append(value)
        return (
            "posted_unverified",
            {
                "manifest": str(manifests[value]),
                "bvid": f"BV1POSTED{value}",
                "attempt_id": f"attempt-{value}",
            },
            [],
        )

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=graph_builder,
        upload_call=lambda argv: upload_calls.append(argv) or 0,
        manifest_prepare_call=prepare_call,
        ledger_reader=lambda _path: (
            [{"video_sha256": value} for value in ledger_order],
            [],
        ),
        ledger_guard=guard,
    )

    selected = ledger_order[0]
    assert visited == list(ledger_order)
    assert result["status"] == "ACTION_COMPLETED"
    assert result["action"]["action"] == "SEASON_ADD"
    assert result["action"]["video_sha256"] == selected
    assert result["action"]["attempt_id"] == f"attempt-{selected}"
    assert upload_calls == [
        [
            "season-add",
            "--manifest",
            str(manifests[selected]),
            "--bvid",
            f"BV1POSTED{selected}",
            "--ledger",
            str(runtime / "reports/upload_ledger.jsonl"),
        ]
    ]
    assert callbacks == {"graph": 0, "prepare": 0}


def test_newest_ready_manifest_is_the_only_upload_action(tmp_path: Path) -> None:
    repo, runtime = _roots(tmp_path)
    older = _manifest(runtime, "older")
    newer_first = _manifest(runtime, "newer-first")
    newer_second = _manifest(runtime, "newer-second")
    graph = _graph(
        _ready_row("older", "2026-08-20", older),
        _ready_row("newer-first", "2026-08-21", newer_first),
        _ready_row("newer-second", "2026-08-21", newer_second),
    )
    calls: list[list[str]] = []

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=lambda argv: calls.append(argv) or 0,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
    )

    assert result["status"] == "ACTION_COMPLETED"
    assert result["action"]["candidate_id"] == "newer-first"
    assert len(calls) == 1
    assert calls[0] == [
        "upload",
        "--manifest",
        str(newer_first),
        "--ledger",
        str(runtime / "reports/upload_ledger.jsonl"),
        "--lock",
        str(runtime / "upload.lock"),
    ]


def test_graph_blocker_and_unsafe_manifest_never_call_upload(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    outside = tmp_path / "outside.upload_manifest.json"
    outside.write_text("{}")

    def forbidden(_argv: list[str]) -> int:
        raise AssertionError("blocked plan must not call upload")

    blocked = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(
            blockers=[{"code": "STATE_FILE_INVALID"}]
        ),
        upload_call=forbidden,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
    )
    assert blocked["status"] == "BLOCKED_GRAPH"
    assert blocked["side_effect_attempted"] is False

    unsafe = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(
            _ready_row("outside", "2026-08-21", outside)
        ),
        upload_call=forbidden,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
    )
    assert unsafe["status"] == "BLOCKED_GRAPH"
    assert unsafe["reason_codes"] == ["READY_UPLOAD_MANIFEST_INVALID"]


def test_ready_to_prepare_is_one_local_manifest_action(tmp_path: Path) -> None:
    repo, runtime = _roots(tmp_path)
    row = {
        "candidate_id": "prepare-only",
        "recording_date": "2026-08-21",
        "lane": "talk",
        "category": "READY_TO_PREPARE",
        "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"],
        "package_dependencies": {"record": "synthetic"},
    }
    calls: list[dict[str, object]] = []

    def prepare(readiness_row, **kwargs):
        calls.append({"row": dict(readiness_row), **kwargs})
        return {"status": "PREPARED", "manifest": "synthetic-manifest"}

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(row),
        upload_call=lambda _argv: pytest.fail("preparation tick must not upload"),
        manifest_prepare_call=prepare,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
    )

    assert result["status"] == "MANIFEST_PREPARED"
    assert result["side_effect_attempted"] is True
    assert result["external_side_effect_attempted"] is False
    assert len(calls) == 1
    assert calls[0]["row"] == row
    assert calls[0]["repository_root"] == repo
    assert calls[0]["runtime_root"] == runtime


def test_ready_upload_precedes_manifest_preparation(tmp_path: Path) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "upload-first")
    prepare_row = {
        "candidate_id": "prepare-later",
        "recording_date": "2026-08-22",
        "lane": "talk",
        "category": "READY_TO_PREPARE",
        "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"],
        "package_dependencies": {},
    }
    calls: list[list[str]] = []
    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(
            prepare_row,
            _ready_row("upload-first", "2026-08-21", manifest),
        ),
        upload_call=lambda argv: calls.append(argv) or 0,
        manifest_prepare_call=lambda *_args, **_kwargs: pytest.fail(
            "ready upload has priority over local preparation"
        ),
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
    )
    assert result["status"] == "ACTION_COMPLETED"
    assert result["action"]["candidate_id"] == "upload-first"
    assert len(calls) == 1


def test_no_ready_action_is_provider_free(tmp_path: Path) -> None:
    repo, runtime = _roots(tmp_path)
    result = plan_next_publication_action(
        repository_root=repo,
        runtime_root=runtime,
        graph_builder=lambda **_kwargs: _graph(
            {
                "candidate_id": "provider-blocked",
                "recording_date": "2026-08-21",
                "category": "NEEDS_PROVIDER",
                "package_dependencies": {},
            }
        ),
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
    )
    assert result["status"] == "NO_READY_ACTION"


@pytest.mark.parametrize(
    ("rc", "status"),
    [
        (2, "VALIDATION_BLOCKED"),
        (3, "DUPLICATE_ALREADY_UPLOADED"),
        (5, "LEDGER_BLOCKED"),
        (6, "FOLLOWUP_PENDING"),
        (8, "QUOTA_BLOCKED"),
        (17, "ACTION_FAILED"),
    ],
)
def test_upload_return_code_is_preserved(
    tmp_path: Path,
    rc: int,
    status: str,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "candidate")
    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: _graph(
            _ready_row("candidate", "2026-08-21", manifest)
        ),
        upload_call=lambda _argv: rc,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
    )
    assert result["status"] == status
    assert result["rc"] == rc
    assert result["side_effect_attempted"] is True


def test_generic_failure_persists_backoff_across_queue_invocations(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "retry-candidate")
    graph = _graph(
        _ready_row("retry-candidate", "2026-08-21", manifest)
    )
    calls: list[list[str]] = []
    return_codes = iter([17, 0])

    def upload(argv: list[str]) -> int:
        calls.append(argv)
        return next(return_codes)

    failed = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=upload,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        now_epoch=2_000_000_000.0,
    )
    assert failed["status"] == "ACTION_FAILED"
    assert failed["backoff_state"]["status"] == "RECORDED"
    assert len(calls) == 1

    waiting = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=lambda _argv: pytest.fail("backoff must suppress uploader"),
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        now_epoch=2_000_000_001.0,
    )
    assert waiting["status"] == "RETRY_BACKOFF"
    assert waiting["backoff"]["retry_after_seconds"] == GENERIC_BASE_SECONDS - 1
    assert waiting["side_effect_attempted"] is False
    assert len(calls) == 1

    retried = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=upload,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        now_epoch=2_000_000_000.0 + GENERIC_BASE_SECONDS + 1,
    )
    assert retried["status"] == "ACTION_COMPLETED"
    assert len(calls) == 2
    assert not (runtime / "reports" / STATE_FILENAME).exists()


def test_quota_result_blocks_a_different_ready_manifest_globally(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    first = _manifest(runtime, "quota-first")
    second = _manifest(runtime, "quota-second")
    first_graph = _graph(
        _ready_row("quota-first", "2026-08-21", first)
    )
    quota = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: first_graph,
        upload_call=lambda _argv: 8,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        now_epoch=2_000_000_000.0,
    )
    assert quota["status"] == "QUOTA_BLOCKED"
    assert quota["backoff_state"]["scope"] == "GLOBAL_QUOTA"

    second_graph = _graph(
        _ready_row("quota-second", "2026-08-22", second)
    )
    waiting = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: second_graph,
        upload_call=lambda _argv: pytest.fail("global quota backoff must suppress uploader"),
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        now_epoch=2_000_000_060.0,
    )
    assert waiting["status"] == "RETRY_BACKOFF"
    assert waiting["backoff"]["scope"] == "GLOBAL_QUOTA"
    assert waiting["backoff"]["retry_after_seconds"] == QUOTA_SECONDS - 60


def test_followup_pending_is_never_delayed_by_retry_backoff(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    manifest = _manifest(runtime, "posted")
    graph = _graph(_ready_row("posted", "2026-08-21", manifest))

    posted = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=lambda _argv: 6,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        now_epoch=2_000_000_000.0,
    )

    assert posted["status"] == "FOLLOWUP_PENDING"
    assert posted["backoff_state"]["status"] == "CLEARED"
    assert not (runtime / "reports" / STATE_FILENAME).exists()


def test_manifest_local_backoff_falls_through_to_next_ready_candidate(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    older = _manifest(runtime, "older-ready")
    newer = _manifest(runtime, "newer-backed-off")
    graph = _graph(
        _ready_row("older-ready", "2026-08-20", older),
        _ready_row("newer-backed-off", "2026-08-21", newer),
    )
    calls: list[list[str]] = []

    def backoff_evaluator(*, manifest_path, **_kwargs):
        if Path(manifest_path) == newer:
            return {
                "status": "BACKOFF",
                "scope": "MANIFEST",
                "reason_code": "PUBLICATION_QUEUE_RETRY_BACKOFF",
                "next_attempt_at": "2033-05-18T03:48:20+00:00",
                "next_attempt_epoch": 2_000_000_900.0,
                "retry_after_seconds": 899,
            }
        return {"status": "CLEAR"}

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=lambda argv: calls.append(argv) or 0,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        backoff_evaluator=backoff_evaluator,
        backoff_recorder=lambda **_kwargs: {"status": "CLEARED"},
        now_epoch=2_000_000_001.0,
    )

    assert result["status"] == "ACTION_COMPLETED"
    assert result["action"]["candidate_id"] == "older-ready"
    assert len(calls) == 1
    assert calls[0][calls[0].index("--manifest") + 1] == str(older)
    assert [
        row["action"]["candidate_id"] for row in result["skipped_backoff"]
    ] == ["newer-backed-off"]
    assert result["skipped_backoff"][0]["backoff"]["scope"] == "MANIFEST"


def test_global_quota_backoff_never_falls_through_to_older_candidate(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    older = _manifest(runtime, "older-ready")
    newer = _manifest(runtime, "newer-quota")
    graph = _graph(
        _ready_row("older-ready", "2026-08-20", older),
        _ready_row("newer-quota", "2026-08-21", newer),
    )
    inspected: list[Path] = []

    def backoff_evaluator(*, manifest_path, **_kwargs):
        inspected.append(Path(manifest_path))
        return {
            "status": "BACKOFF",
            "scope": "GLOBAL_QUOTA",
            "reason_code": "PUBLICATION_QUEUE_QUOTA_BACKOFF",
            "next_attempt_at": "2033-05-19T03:33:20+00:00",
            "next_attempt_epoch": 2_000_087_000.0,
            "retry_after_seconds": 86_400,
        }

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=lambda _argv: pytest.fail("global quota must suppress uploader"),
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        backoff_evaluator=backoff_evaluator,
        now_epoch=2_000_000_600.0,
    )

    assert result["status"] == "RETRY_BACKOFF"
    assert result["action"]["candidate_id"] == "newer-quota"
    assert result["backoff"]["scope"] == "GLOBAL_QUOTA"
    assert inspected == [newer]
    assert result["side_effect_attempted"] is False


def test_all_manifest_local_backoffs_report_earliest_wakeup(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    older = _manifest(runtime, "older-backed-off")
    newer = _manifest(runtime, "newer-backed-off")
    graph = _graph(
        _ready_row("older-backed-off", "2026-08-20", older),
        _ready_row("newer-backed-off", "2026-08-21", newer),
    )

    def backoff_evaluator(*, manifest_path, **_kwargs):
        current = Path(manifest_path)
        next_epoch = 2_000_000_500.0 if current == older else 2_000_000_900.0
        return {
            "status": "BACKOFF",
            "scope": "MANIFEST",
            "reason_code": "PUBLICATION_QUEUE_RETRY_BACKOFF",
            "next_attempt_at": str(next_epoch),
            "next_attempt_epoch": next_epoch,
            "retry_after_seconds": int(next_epoch - 2_000_000_000.0),
        }

    result = plan_next_publication_action(
        repository_root=repo,
        runtime_root=runtime,
        graph_builder=lambda **_kwargs: graph,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        backoff_evaluator=backoff_evaluator,
        now_epoch=2_000_000_000.0,
    )

    assert result["status"] == "RETRY_BACKOFF"
    assert result["action"]["candidate_id"] == "older-backed-off"
    assert result["backoff"]["next_attempt_epoch"] == 2_000_000_500.0
    assert [
        row["action"]["candidate_id"]
        for row in result["backed_off_candidates"]
    ] == ["newer-backed-off", "older-backed-off"]


def test_invalid_backoff_state_blocks_queue_instead_of_falling_through(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    older = _manifest(runtime, "older-ready")
    newer = _manifest(runtime, "newer-invalid")
    graph = _graph(
        _ready_row("older-ready", "2026-08-20", older),
        _ready_row("newer-invalid", "2026-08-21", newer),
    )

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=lambda _argv: pytest.fail("invalid backoff state must stop queue"),
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        backoff_evaluator=lambda **_kwargs: {
            "status": "INVALID",
            "reason_code": "PUBLICATION_QUEUE_BACKOFF_STATE_INVALID",
            "detail": "synthetic tamper",
        },
    )

    assert result["status"] == "BLOCKED_BACKOFF_STATE"
    assert result["candidate_id"] == "newer-invalid"
    assert result["side_effect_attempted"] is False


def test_real_local_backoff_sidecar_falls_through_and_is_preserved(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    older = _manifest(runtime, "older-real")
    newer = _manifest(runtime, "newer-real")
    newer_only = _graph(
        _ready_row("newer-real", "2026-08-21", newer)
    )
    failed = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: newer_only,
        upload_call=lambda _argv: 17,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        now_epoch=2_000_000_000.0,
    )
    assert failed["status"] == "ACTION_FAILED"
    state_path = runtime / "reports" / STATE_FILENAME
    assert state_path.is_file()
    state_before = state_path.read_bytes()

    both = _graph(
        _ready_row("older-real", "2026-08-20", older),
        _ready_row("newer-real", "2026-08-21", newer),
    )
    calls: list[list[str]] = []
    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: both,
        upload_call=lambda argv: calls.append(argv) or 0,
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        now_epoch=2_000_000_001.0,
    )

    assert result["status"] == "ACTION_COMPLETED"
    assert result["action"]["candidate_id"] == "older-real"
    assert result["backoff_state"]["status"] == "PRESERVED"
    assert result["backoff_state"]["manifest_path"] == str(newer)
    assert state_path.read_bytes() == state_before
    assert len(calls) == 1
    assert [
        row["action"]["candidate_id"] for row in result["skipped_backoff"]
    ] == ["newer-real"]


def test_all_local_upload_backoffs_allow_one_local_manifest_preparation(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    blocked = _manifest(runtime, "blocked-upload")
    prepare_row = {
        "candidate_id": "prepare-next",
        "recording_date": "2026-08-22",
        "lane": "talk",
        "category": "READY_TO_PREPARE",
        "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"],
        "package_dependencies": {"record": "synthetic"},
    }
    graph = _graph(
        _ready_row("blocked-upload", "2026-08-21", blocked),
        prepare_row,
    )
    prepared: list[dict[str, object]] = []

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=lambda _argv: pytest.fail("backed-off upload must not run"),
        manifest_prepare_call=lambda row, **kwargs: (
            prepared.append({"row": dict(row), **kwargs})
            or {"status": "PREPARED", "manifest": "synthetic"}
        ),
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        backoff_evaluator=lambda **_kwargs: {
            "status": "BACKOFF",
            "scope": "MANIFEST",
            "reason_code": "PUBLICATION_QUEUE_RETRY_BACKOFF",
            "next_attempt_at": "2033-05-18T03:48:20+00:00",
            "next_attempt_epoch": 2_000_000_900.0,
            "retry_after_seconds": 899,
        },
    )

    assert result["status"] == "MANIFEST_PREPARED"
    assert result["action"]["candidate_id"] == "prepare-next"
    assert result["external_side_effect_attempted"] is False
    assert len(prepared) == 1
    assert prepared[0]["row"] == prepare_row
    assert [
        row["action"]["candidate_id"] for row in result["skipped_backoff"]
    ] == ["blocked-upload"]


def test_global_quota_allows_local_prepare_but_never_uploads(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    blocked = _manifest(runtime, "quota-upload")
    prepare_row = {
        "candidate_id": "prepare-under-quota",
        "recording_date": "2026-08-22",
        "lane": "talk",
        "category": "READY_TO_PREPARE",
        "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"],
        "package_dependencies": {"record": "synthetic"},
    }
    graph = _graph(
        _ready_row("quota-upload", "2026-08-21", blocked),
        prepare_row,
    )

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=lambda _argv: pytest.fail("quota must suppress uploader"),
        manifest_prepare_call=lambda _row, **_kwargs: {
            "status": "PREPARED",
            "manifest": "synthetic",
        },
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        backoff_evaluator=lambda **_kwargs: {
            "status": "BACKOFF",
            "scope": "GLOBAL_QUOTA",
            "reason_code": "PUBLICATION_QUEUE_QUOTA_BACKOFF",
            "next_attempt_at": "2033-05-19T03:33:20+00:00",
            "next_attempt_epoch": 2_000_087_000.0,
            "retry_after_seconds": 86_400,
        },
    )

    assert result["status"] == "MANIFEST_PREPARED"
    assert result["action"]["candidate_id"] == "prepare-under-quota"
    assert result["deferred_upload_backoff"]["scope"] == "GLOBAL_QUOTA"
    assert result["external_side_effect_attempted"] is False
    assert [
        row["action"]["candidate_id"] for row in result["skipped_backoff"]
    ] == ["quota-upload"]


def test_actionable_upload_keeps_priority_over_local_preparation(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    ready = _manifest(runtime, "ready-upload")
    prepare_row = {
        "candidate_id": "prepare-later",
        "recording_date": "2026-08-22",
        "lane": "talk",
        "category": "READY_TO_PREPARE",
        "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"],
        "package_dependencies": {"record": "synthetic"},
    }
    graph = _graph(
        _ready_row("ready-upload", "2026-08-21", ready),
        prepare_row,
    )
    calls: list[list[str]] = []

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=lambda argv: calls.append(argv) or 0,
        manifest_prepare_call=lambda *_args, **_kwargs: pytest.fail(
            "actionable upload must retain priority"
        ),
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        backoff_evaluator=lambda **_kwargs: {"status": "CLEAR"},
        backoff_recorder=lambda **_kwargs: {"status": "CLEARED"},
    )

    assert result["status"] == "ACTION_COMPLETED"
    assert result["action"]["candidate_id"] == "ready-upload"
    assert len(calls) == 1


def test_invalid_backoff_state_still_blocks_local_preparation(
    tmp_path: Path,
) -> None:
    repo, runtime = _roots(tmp_path)
    ready = _manifest(runtime, "invalid-upload")
    prepare_row = {
        "candidate_id": "must-not-prepare",
        "recording_date": "2026-08-22",
        "lane": "talk",
        "category": "READY_TO_PREPARE",
        "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"],
        "package_dependencies": {"record": "synthetic"},
    }
    graph = _graph(
        _ready_row("invalid-upload", "2026-08-21", ready),
        prepare_row,
    )

    result = consume_ready_publication_queue(
        repository_root=repo,
        runtime_root=runtime,
        enabled=True,
        graph_builder=lambda **_kwargs: graph,
        upload_call=lambda _argv: pytest.fail("invalid state must stop queue"),
        manifest_prepare_call=lambda *_args, **_kwargs: pytest.fail(
            "invalid state must block local preparation"
        ),
        ledger_reader=lambda _path: ([], []),
        ledger_guard=lambda *_args: pytest.fail("empty ledger has no guard calls"),
        backoff_evaluator=lambda **_kwargs: {
            "status": "INVALID",
            "reason_code": "PUBLICATION_QUEUE_BACKOFF_STATE_INVALID",
            "detail": "synthetic tamper",
        },
    )

    assert result["status"] == "BLOCKED_BACKOFF_STATE"
    assert result["side_effect_attempted"] is False
