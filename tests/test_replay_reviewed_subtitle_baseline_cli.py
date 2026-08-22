from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts import replay_reviewed_subtitle_baseline as cli


def test_success_matrix_has_unique_terminal_predicates() -> None:
    plan = SimpleNamespace(matrix=(
        {"predicate": "SPEAKER_ASS_BURN_REBUILD", "status": "PENDING_STAGE"},
        {"predicate": "TITLE_COVER_PRECONDITIONS", "status": "PENDING_STAGE"},
        {"predicate": "FINAL_REVIEW_AND_PACKAGE_AUDIT", "status": "PENDING_STAGE"},
        {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"},
    ))
    matrix = cli._matrix(plan, status="PASS")
    assert len({row["predicate"] for row in matrix}) == len(matrix)
    assert all(row["status"] not in {"PENDING", "PENDING_STAGE", "BLOCK"} for row in matrix)
    assert matrix[-1] == {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"}


def test_failure_matrix_keeps_independent_gates_distinct() -> None:
    plan = SimpleNamespace(matrix=(
        {"predicate": "SEALED_BASELINE", "status": "PASS"},
        {"predicate": "COVER_ROUTE_PIXEL_HOST_PARTICIPANT_PUNCH", "status": "PENDING_STAGE"},
    ))
    matrix = {row["predicate"]: row for row in cli._matrix(
        plan, status="NOT_EVALUATED",
        failures={"SOURCE_FACT_REVIEW": "REPLAY_SOURCE_FACT_FAILED", "PACKAGE_AUDIT": "REPLAY_AUDIT_FAILED"},
    )}
    assert matrix["SEALED_BASELINE"]["status"] == "PASS"
    assert matrix["COVER_ROUTE_PIXEL_HOST_PARTICIPANT_PUNCH"]["status"] == "NOT_EVALUATED"
    assert matrix["SOURCE_FACT_REVIEW"] == {
        "predicate": "SOURCE_FACT_REVIEW", "status": "FAIL", "reason_code": "REPLAY_SOURCE_FACT_FAILED",
    }
    assert matrix["PACKAGE_AUDIT"] == {
        "predicate": "PACKAGE_AUDIT", "status": "FAIL", "reason_code": "REPLAY_AUDIT_FAILED",
    }


def test_prepare_converts_finalizer_system_exit_to_per_candidate_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}\n")
    baseline = SimpleNamespace(config={"sha256": "a" * 64})
    plan = SimpleNamespace(date="2026-08-14", candidate_id="cid", record_path=record, baseline=baseline)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(cli, "stage_replay", lambda *_a, **_kw: (_ for _ in ()).throw(SystemExit(2)))
    with pytest.raises(cli._PrepareFailure) as caught:
        cli._prepare(plan, runtime=tmp_path, stage_parent=parent)
    assert caught.value.provider_attempted is False


def test_prepared_manifest_diagnostic_hash_is_file_digest_not_inner_seal(tmp_path: Path) -> None:
    manifest = tmp_path / "prepared.json"
    manifest.write_text('{"prepared_sha256":"sha256:' + "a" * 64 + '"}\n')
    manifest.chmod(0o600)
    finalization = SimpleNamespace(
        prepared_manifest=manifest, prepared_sha256="sha256:" + "a" * 64,
    )
    digest = cli._prepared_manifest_sha256(finalization)
    assert digest == "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert digest != finalization.prepared_sha256


def test_private_parent_rejects_a_symlink_ancestor(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(Exception):
        cli._private_parent(linked / "stage")


def test_readiness_graph_is_scoped_and_does_not_prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text('{"picks":[]}\n')
    monkeypatch.setattr(cli, "_safe_directory", lambda path: Path(path))
    monkeypatch.setattr(
        "src.autoslice.publication_readiness.build_readiness_graph",
        lambda **_kwargs: {"rows": [
            {"candidate_id": "one", "recording_date": "2026-08-14", "category": "READY_TO_PREPARE", "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"]},
            {"candidate_id": "two", "recording_date": "2026-08-15", "category": "NEEDS_PROVIDER", "reason_codes": ["COVER_QC_MISSING"]},
        ], "graph_blockers": []},
    )
    result = cli._readiness_graph(runtime=runtime, date="2026-08-14", candidate_ids=[])
    assert result["rows"] == [{"candidate_id": "one", "recording_date": "2026-08-14", "category": "READY_TO_PREPARE", "reason_codes": ["LOCAL_PREPARATION_AVAILABLE"]}]
    assert result["upload_allowed"] is False


def test_readiness_graph_normalizes_semantic_recall_stale_replay_to_prepare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text(json.dumps({"picks": [{
        "cid": "one", "lane": "semantic_recall", "status": "candidate_rejected",
    }]}))
    monkeypatch.setattr(
        "src.autoslice.publication_readiness.build_readiness_graph",
        lambda **_kwargs: {"rows": [{
            "candidate_id": "one", "recording_date": "2026-08-14",
            "category": "STATE_DRIFT", "reason_codes": [
                "STATE_ROW_NOT_REVIEW_READY", "PACKAGE_ARTIFACT_HASH_DRIFT", "COVER_QC_MISSING",
            ],
        }], "graph_blockers": []},
    )
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: SimpleNamespace(
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    ))
    result = cli._readiness_graph(runtime=runtime, date="2026-08-14", candidate_ids=[])
    row = result["rows"][0]
    assert row["category"] == "READY_TO_PREPARE"
    assert row["replay_readiness"] == {
        "predicate": "SEALED_REVIEWED_BASELINE_REPLAY_READY", "status": "PASS",
        "baseline_sha256": "a" * 64,
    }
    assert row["generic_observation"]["category"] == "STATE_DRIFT"


def test_readiness_graph_normalizes_only_selection_terminal_for_sharded_talk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text(json.dumps({"picks": [{
        "cid": "one", "lane": "semantic_recall_sharded", "status": "candidate_rejected",
    }]}))
    monkeypatch.setattr(
        "src.autoslice.publication_readiness.build_readiness_graph",
        lambda **_kwargs: {"rows": [{
            "candidate_id": "one", "recording_date": "2026-08-14",
            "category": "NEEDS_IVAN_TRUTH", "reason_codes": ["SELECTION_SUPPORT_TERMINAL_BLOCKED"],
        }], "graph_blockers": []},
    )
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: SimpleNamespace(
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    ))
    row = cli._readiness_graph(runtime=runtime, date="2026-08-14", candidate_ids=[])["rows"][0]
    assert row["category"] == "READY_TO_PREPARE"
    assert row["generic_observation"] == {
        "category": "NEEDS_IVAN_TRUTH", "reason_codes": ["SELECTION_SUPPORT_TERMINAL_BLOCKED"],
    }


@pytest.mark.parametrize(
    ("lane", "category", "reasons"),
    [
        ("semantic_recall", "NEEDS_IVAN_TRUTH", ["HUMAN_TRUTH_MISSING"]),
        ("talk", "NEEDS_IVAN_TRUTH", ["HOLD_PENDING_REVIEW"]),
        ("song", "STATE_DRIFT", ["STATE_ROW_NOT_REVIEW_READY"]),
    ],
)
def test_readiness_graph_never_normalizes_unowned_reason_or_song_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lane: str, category: str, reasons: list[str],
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text(json.dumps({"picks": [{
        "cid": "one", "lane": lane, "status": "candidate_rejected",
    }]}))
    monkeypatch.setattr(
        "src.autoslice.publication_readiness.build_readiness_graph",
        lambda **_kwargs: {"rows": [{
            "candidate_id": "one", "recording_date": "2026-08-14",
            "category": category, "reason_codes": reasons,
        }], "graph_blockers": []},
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli, "build_replay_plan",
        lambda **kwargs: calls.append(kwargs) or pytest.fail("unowned row must not build a replay plan"),
    )
    row = cli._readiness_graph(runtime=runtime, date="2026-08-14", candidate_ids=[])["rows"][0]
    assert row["category"] == category
    assert "generic_observation" not in row
    assert calls == []


def test_readiness_graph_keeps_stale_row_blocked_without_sealed_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text(json.dumps({"picks": [{
        "cid": "one", "lane": "semantic_recall", "status": "candidate_rejected",
    }]}))
    monkeypatch.setattr(
        "src.autoslice.publication_readiness.build_readiness_graph",
        lambda **_kwargs: {"rows": [{
            "candidate_id": "one", "recording_date": "2026-08-14",
            "category": "STATE_DRIFT", "reason_codes": ["STATE_ROW_NOT_REVIEW_READY"],
        }], "graph_blockers": []},
    )
    monkeypatch.setattr(
        cli, "build_replay_plan",
        lambda **_kwargs: (_ for _ in ()).throw(cli.ReviewedBaselineReplayError("REPLAY_BASELINE_MISSING")),
    )
    row = cli._readiness_graph(runtime=runtime, date="2026-08-14", candidate_ids=[])["rows"][0]
    assert row["category"] == "NEEDS_IVAN_TRUTH"
    assert row["replay_readiness"] == {
        "predicate": "SEALED_REVIEWED_BASELINE_REPLAY_READY", "status": "FAIL",
        "reason_code": "REPLAY_BASELINE_MISSING",
    }


def test_full_package_prepare_overlaps_and_apply_rebinds_state_serially(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The future boundary includes package preparation, not merely synthesize."""

    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text('{"generation":0}\n')
    (runtime / "DISABLED").write_text("disabled\n")
    stage_parent = tmp_path / "stages"
    stage_parent.mkdir(mode=0o700)
    plans = [
        SimpleNamespace(date="2026-08-14", candidate_id=cid, matrix=(), baseline=SimpleNamespace(config={"sha256": "a" * 64}))
        for cid in ("one", "two")
    ]
    monkeypatch.setattr(cli, "build_replay_plan", lambda **kwargs: next(
        item for item in plans if item.candidate_id == kwargs["candidate_id"]
    ))
    active = 0
    maximum = 0
    barrier = threading.Barrier(2)
    state_generations: list[int] = []
    commits: list[tuple[str, int]] = []

    def fake_prepare(plan, *, runtime, stage_parent, state_path):
        nonlocal active, maximum
        stage = stage_parent / plan.candidate_id
        stage.mkdir()
        active += 1
        maximum = max(maximum, active)
        barrier.wait(timeout=2)
        active -= 1
        return stage, SimpleNamespace(prepared_sha256="sha256:" + "b" * 64), SimpleNamespace(
            after=object(), projection=object(),
        ), None, ()

    def fake_rebind(plan, *, runtime_root, state_path, finalization, after, projection):
        assert projection is not None
        generation = json.loads(state_path.read_text())["generation"]
        state_generations.append(generation)
        return SimpleNamespace(candidate_id=plan.candidate_id, generation=generation)

    def fake_commit(*, runtime_root, after):
        commits.append((after.candidate_id, after.generation))
        path = runtime_root / "state" / "2026-08-14.json"
        path.write_text(json.dumps({"generation": after.generation + 1}) + "\n")
        journal = runtime_root / f"{after.candidate_id}.journal"
        journal.write_text("journal\n")
        return journal

    monkeypatch.setattr(cli, "_prepare", fake_prepare)
    monkeypatch.setattr(cli, "rebind_replay_after_image_state", fake_rebind)
    monkeypatch.setattr(cli, "commit_prepared_after_image", fake_commit)
    monkeypatch.setattr(cli, "cleanup_prepared_after_image_stage", lambda **_kwargs: None)
    monkeypatch.setattr(cli, "stream_binding", lambda *_args, **_kwargs: SimpleNamespace(sha256="sha256:" + "c" * 64))
    assert cli.main([
        "--apply", "--runtime-root", str(runtime), "--date", "2026-08-14",
        "--candidate-id", "one", "--candidate-id", "two",
        "--private-stage-parent", str(stage_parent),
    ]) == 0
    assert maximum == 2
    assert state_generations == [0, 1]
    assert commits == [("one", 0), ("two", 1)]
    assert json.loads(capsys.readouterr().out)["candidates"][1]["status"] == "COMMITTED"


def test_committed_cleanup_oserror_cannot_mask_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text('{"generation":0}\n')
    (runtime / "DISABLED").write_text("disabled\n")
    stage_parent = tmp_path / "stages"
    stage_parent.mkdir(mode=0o700)
    plan = SimpleNamespace(
        date="2026-08-14", candidate_id="one", matrix=(),
        baseline=SimpleNamespace(config={"sha256": "a" * 64}),
    )
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: plan)
    stage = stage_parent / "one"
    stage.mkdir()
    monkeypatch.setattr(
        cli, "_prepare",
        lambda *_args, **_kwargs: (stage, SimpleNamespace(prepared_manifest=stage / "prepared.json"), SimpleNamespace(
            after=object(), projection=object(),
        ), None, ()),
    )
    monkeypatch.setattr(cli, "rebind_replay_after_image_state", lambda *_args, **_kwargs: SimpleNamespace())
    journal = runtime / "journal.json"
    journal.write_text("journal\n")
    monkeypatch.setattr(cli, "commit_prepared_after_image", lambda **_kwargs: journal)
    monkeypatch.setattr(cli, "cleanup_prepared_after_image_stage", lambda **_kwargs: None)
    monkeypatch.setattr(cli, "_cleanup_private_stage", lambda **_kwargs: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(cli, "stream_binding", lambda *_args, **_kwargs: SimpleNamespace(sha256="sha256:" + "c" * 64))
    assert cli.main([
        "--apply", "--runtime-root", str(runtime), "--date", "2026-08-14",
        "--candidate-id", "one", "--private-stage-parent", str(stage_parent),
    ]) == 2
    item = json.loads(capsys.readouterr().out)["candidates"][0]
    assert item["status"] == "COMMITTED_CLEANUP_UNCONFIRMED"
    assert item["journal_sha256"] == "sha256:" + "c" * 64
    assert item["predicate_matrix"][-1] == {"predicate": "PRIVATE_STAGE_CLEANUP", "status": "FAIL"}


def test_rebind_failure_cleans_uncharted_transaction_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / "repo").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "state" / "2026-08-14.json").write_text('{"generation":0}\n')
    (runtime / "DISABLED").write_text("disabled\n")
    stage_parent = tmp_path / "stages"
    stage_parent.mkdir(mode=0o700)
    stage = stage_parent / "one"
    stage.mkdir()
    plan = SimpleNamespace(date="2026-08-14", candidate_id="one", matrix=(),
                           baseline=SimpleNamespace(config={"sha256": "a" * 64}))
    prepared_after = SimpleNamespace(after=object(), projection=object())
    monkeypatch.setattr(cli, "build_replay_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: (
        stage, SimpleNamespace(prepared_manifest=stage / "prepared.json"), prepared_after, None, (),
    ))
    monkeypatch.setattr(
        cli, "rebind_replay_after_image_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("state drift")),
    )
    cleaned: list[object] = []
    monkeypatch.setattr(
        cli, "cleanup_prepared_after_image_stage",
        lambda **kwargs: cleaned.append(kwargs["after"]),
    )
    monkeypatch.setattr(cli, "commit_prepared_after_image", lambda **_kwargs: pytest.fail("must not commit"))
    monkeypatch.setattr(cli, "_cleanup_private_stage", lambda **_kwargs: None)
    monkeypatch.setattr(cli, "_sanitized_failure_receipt", lambda **_kwargs: "sha256:" + "d" * 64)
    assert cli.main([
        "--apply", "--runtime-root", str(runtime), "--date", "2026-08-14",
        "--candidate-id", "one", "--private-stage-parent", str(stage_parent),
    ]) == 2
    assert cleaned == [prepared_after.after]
    assert json.loads(capsys.readouterr().out)["candidates"][0]["status"] == "BLOCKED"
