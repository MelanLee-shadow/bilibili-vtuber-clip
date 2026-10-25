"""Exercise ordinary recovery with dependency changes and durable counters."""
import copy
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from scripts import session_autoslice as runner
from src.autoslice import delivery_recovery as recovery
from src.autoslice.infra_retry_policy import talk_infra_retry_delay_seconds
from src.autoslice.runner_pipeline_fingerprints import (
    TALK_DEPENDENCY_FIELD, TALK_STAGE_ROOTS, changed_dependency_stages,
    dependency_fingerprint,
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    for modules in TALK_STAGE_ROOTS.values():
        for name in modules:
            module = name.split(":")[0]
            path = repo / "src/autoslice" / f"{module}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def _resolve_automatic_title():\n    return 'original'\n")
    for name in ("candidate_text_override_path", "candidate_subtitle_regression_path",
                 "candidate_speaker_override_path", "candidate_entity_projection_path",
                 "candidate_public_text_surface_authority_path", "candidate_reviewed_subtitle_baseline"):
        monkeypatch.setattr(runner, name, lambda _cid: None)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "d" * 64)
    monkeypatch.setattr(runner, "talk_failure_recovery_fingerprint", lambda *_: "sha256:" + "f" * 64)
    rec = tmp_path / "recordings"
    date = "2026-08-13"
    source = rec / date / "real-input-name.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"unit fixture media, not a real producer acceptance")
    monkeypatch.setattr(runner, "REC_ROOT", rec)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _: None)
    monkeypatch.setattr(runner, "resolve_structured_chat_binding", lambda *_, **__: {
        "structured_chat_required": False, "chat_binding_status": "NOT_REGISTERED"})
    record = dict(candidate_id="candidate", status="failed", segment=source.name,
                  start_ms=100_000, end_ms=200_000, failure_kind="subtitle_authority",
                  failure_stage="final_review_findings", failure_recoverable=False,
                  pipeline_fingerprint="sha256:" + "1" * 64,
                  failure_recovery_fingerprint="sha256:" + "2" * 64,
                  talk_repair_retry_count=2, talk_transient_retry_count=3)
    record[TALK_DEPENDENCY_FIELD] = runner.capture_talk_dependency_snapshot(date, record)
    return repo, source, date, {"picks": [record], "pending_talk": []}


def test_ordinary_recovery_ignores_unrelated_deploy(sandbox):
    repo, source, date, state = sandbox
    (repo / "src/autoslice/reporting.py").write_text("REPORTING_VERSION = 2\n")
    before = copy.deepcopy(state)
    assert runner.requeue_recoverable_talks(date, state) == 0
    assert state == before


def test_relevant_text_consumer_change_requeues_only_failed_candidate(sandbox):
    repo, source, date, state = sandbox
    good = dict(candidate_id="ready", status="review_ready", bundle_lifecycle="CURRENT")
    state["picks"].append(good)
    (repo / "src/autoslice/producer_text_pipeline.py").write_text("TEXT_POLICY = 2\n")
    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["picks"] == [good]
    queued = state["pending_talk"][0]
    assert queued["retry_changed_stages"] == ["text"]
    assert queued["talk_repair_retry_count"] == 3
    assert queued["talk_transient_retry_count"] == 3
    assert "review_status" not in queued


def test_source_and_body_changes_invalidate_old_review(sandbox):
    repo, source, date, state = sandbox
    old = state["picks"][0][TALK_DEPENDENCY_FIELD]
    source.write_bytes(source.read_bytes() + b"source changed")
    new = runner.capture_talk_dependency_snapshot(date, state["picks"][0])
    assert set(changed_dependency_stages(old, new)) == set(TALK_STAGE_ROOTS)
    state["picks"][0][TALK_DEPENDENCY_FIELD] = new
    state["picks"][0]["filler_proposal_srt_sha256"] = "sha256:" + "3" * 64
    newer = runner.capture_talk_dependency_snapshot(date, state["picks"][0])
    assert set(changed_dependency_stages(new, newer)) == {"boundary", "text", "title", "cover", "package"}
    state["picks"][0]["merge_gap_removals"] = [{"start_ms": 120_000, "end_ms": 130_000}]
    assert set(changed_dependency_stages(newer, runner.capture_talk_dependency_snapshot(date, state["picks"][0]))) == set(TALK_STAGE_ROOTS)


def test_title_input_change_preserves_source_and_speaker_proof(sandbox):
    repo, source, date, state = sandbox
    row = state["picks"][0]
    old = row[TALK_DEPENDENCY_FIELD]
    row["given_title"] = "source-backed new title"
    assert set(changed_dependency_stages(old, runner.capture_talk_dependency_snapshot(date, row))) == {"title", "cover", "package"}


def test_legacy_provenance_adopts_baseline_without_whole_queue_replay(sandbox):
    repo, source, date, state = sandbox
    row = state["picks"][0]
    del row[TALK_DEPENDENCY_FIELD]
    assert runner.requeue_recoverable_talks(date, state) == 0
    assert row["recovery_dependency_status"] == "LEGACY_BASELINE_ADOPTED_NO_REPLAY"
    assert row["failure_recovery_fingerprint"] == "sha256:" + "2" * 64
    assert (row["talk_repair_retry_count"], row["talk_transient_retry_count"]) == (2, 3)
    restored = json.loads(json.dumps(state))
    (repo / "src/autoslice/reporting.py").write_text("UNRELATED_DEPLOY = 3\n")
    assert runner.requeue_recoverable_talks(date, restored) == 0


def test_relevant_deploy_does_not_erase_infra_backoff(sandbox):
    repo, source, date, state = sandbox
    row = state["picks"][0]
    row.update(failure_kind="provider_transient", failure_stage="final_review_discovery",
               failure_recoverable=True, next_retry_at_epoch=0)
    state["talk_superseded_attempts"] = [dict(candidate_id="candidate", talk_repair_retry_count=4,
                                            talk_transient_retry_count=5)]
    (repo / "src/autoslice/producer_text_pipeline.py").write_text("TEXT_POLICY = 3\n")
    assert runner.requeue_recoverable_talks(date, state) == 1
    queued = state["pending_talk"][0]
    assert queued["talk_repair_retry_count"] == 5
    assert queued["talk_transient_retry_count"] == 6
    assert talk_infra_retry_delay_seconds(queued) == runner.SONG_INFRA_RETRY_MAX_SECONDS


def test_unknown_error_does_not_wake_for_a_deploy(sandbox):
    repo, source, date, state = sandbox
    row = state["picks"][0]
    row.update(failure_kind="producer_error", failure_stage="unknown", failure_recoverable=True)
    (repo / "src/autoslice/producer_text_pipeline.py").write_text("TEXT_POLICY = 4\n")
    assert runner.requeue_recoverable_talks(date, state) == 0
    assert row["recovery_dependency_status"] == "UNCLASSIFIED_FAILURE_REQUIRES_DIAGNOSIS"


def test_invalid_snapshot_is_diagnostic_and_does_not_block_another_pick(sandbox):
    repo, source, date, state = sandbox
    other = copy.deepcopy(state["picks"][0])
    other["candidate_id"] = "other"
    other[TALK_DEPENDENCY_FIELD] = runner.capture_talk_dependency_snapshot(date, other)
    state["picks"].append(other)
    state["picks"][0][TALK_DEPENDENCY_FIELD]["candidate_id"] = "foreign"
    (repo / "src/autoslice/producer_text_pipeline.py").write_text("TEXT_POLICY = 5\n")
    assert runner.requeue_recoverable_talks(date, state) == 1
    assert "identity is invalid" in state["picks"][0]["recovery_dependency_error"]
    assert state["pending_talk"][0]["cid"] == "other"


def test_corrupt_snapshot_cannot_masquerade_as_a_legacy_record(sandbox):
    repo, source, date, state = sandbox
    state["picks"][0][TALK_DEPENDENCY_FIELD] = "corrupt"
    assert runner.requeue_recoverable_talks(date, state) == 0
    assert "type is invalid" in state["picks"][0]["recovery_dependency_error"]
    assert state["picks"][0][TALK_DEPENDENCY_FIELD] == "corrupt"


def test_imported_consumer_tracks_used_function_but_not_unrelated_helper(tmp_path):
    root = tmp_path
    mod = root / "src/autoslice"
    mod.mkdir(parents=True)
    consumer = mod / "consumer.py"
    consumer.write_text("from src.autoslice.helper import evaluate\ndef consume():\n    return evaluate()\n")
    helper = mod / "helper.py"
    helper.write_text("def evaluate():\n    return 1\ndef unrelated():\n    return 1\n")
    def fingerprint():
        return dependency_fingerprint(repo_root=root, roots=[consumer], config={})
    old = fingerprint()
    helper.write_text("def evaluate():\n    return 1\ndef unrelated():\n    return 2\n")
    assert fingerprint() == old
    previous_stat = helper.stat()
    helper.write_text("def evaluate():\n    return 2\ndef unrelated():\n    return 2\n")
    os.utime(helper, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
    assert fingerprint() != old


def test_conditional_local_provider_imports_are_proof_dependencies(tmp_path):
    mod = tmp_path / "src/autoslice"
    mod.mkdir(parents=True)
    consumer = mod / "consumer.py"
    consumer.write_text("def consume(provider):\n    if provider == 'first':\n        from src.autoslice.first import call\n    else:\n        from src.autoslice.second import call\n    return call()\n")
    providers = [mod / "first.py", mod / "second.py"]
    for path in providers:
        path.write_text("def call():\n    return 1\n")
    def fingerprint():
        return dependency_fingerprint(repo_root=tmp_path, roots=[consumer], config={})
    old = fingerprint()
    for path in providers:
        path.write_text("def call():\n    return 2\n")
        assert fingerprint() != old
        path.write_text("def call():\n    return 1\n")


def test_prepared_result_key_tracks_profile_and_transport_not_counters(sandbox, monkeypatch):
    repo, source, date, state = sandbox
    row = state["picks"][0]
    original = runner.prepared_result_dependency_fingerprint(date, row, "talk")
    row.update(talk_transient_retry_count=99, pipeline_fingerprint="sha256:" + "9" * 64)
    assert runner.prepared_result_dependency_fingerprint(date, row, "talk") == original
    monkeypatch.setattr(runner, "CHANNEL_PROFILE", replace(runner.CHANNEL_PROFILE, talk_title_prefix="new title prefix"))
    updated = runner.capture_talk_dependency_snapshot(date, row)
    assert changed_dependency_stages(row[TALK_DEPENDENCY_FIELD], updated) == ("title",)
    assert runner.prepared_result_dependency_fingerprint(date, row, "talk") != original
    old = updated
    bridge = repo / "scripts/llm_via_cpa.sh"
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text("#!/bin/sh\nexit 1\n")
    assert set(changed_dependency_stages(old, runner.capture_talk_dependency_snapshot(date, row))) == {"boundary", "text", "title", "cover"}
    with pytest.raises(ValueError, match="lane"):
        runner.prepared_result_dependency_fingerprint(date, row, "unknown")


def test_song_proof_key_ignores_retry_caps_but_tracks_window_policy(sandbox, monkeypatch):
    original = runner.song_pipeline_fingerprint()
    monkeypatch.setattr(runner, "SONG_INFRA_RETRY_CAP", runner.SONG_INFRA_RETRY_CAP + 1)
    monkeypatch.setattr(runner, "SONG_LIFETIME_ATTEMPT_CAP", runner.SONG_LIFETIME_ATTEMPT_CAP + 1)
    assert runner.song_pipeline_fingerprint() == original
    monkeypatch.setattr(runner, "SONG_WINDOW_PRE_MS", runner.SONG_WINDOW_PRE_MS + 1000)
    assert runner.song_pipeline_fingerprint() != original
