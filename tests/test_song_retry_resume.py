"""Same-pipeline song infrastructure retries resume proof, not selection.

External media/provider calls are synthetic; real requeue and produce_song
consumers, reason classification and delivery gates remain in use.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.session_autoslice as runner
from src.autoslice.exact_talk_recovery_scope import maintain_delivery_recovery_scope

DATE = "2099-01-02"
CID = "song_resume_fixture"
FINGERPRINT = "sha256:" + "1" * 64
PROVIDER_CODES = (
    "AGY_AND_GEMINI_API_FAILED", "AGY_QUOTA_EXHAUSTED", "AGY_TIMEOUT",
    "AGY_EMPTY_OUTPUT", "AGY_FAILED_RC", "CPA_UPSTREAM_TIMEOUT",
)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    base = tmp_path / "autoslice"
    repo = tmp_path / "repo"
    rec_root = tmp_path / "recordings"
    (base / "logs").mkdir(parents=True)
    repo.mkdir()
    (rec_root / DATE).mkdir(parents=True)
    segment = rec_root / DATE / "22966160_20990102-12-00-00.mp4"
    segment.write_bytes(b"synthetic-source-not-real-media")
    clock = [10_000.0]
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: FINGERPRINT)
    monkeypatch.setattr(runner, "song_pipeline_fingerprint", lambda: FINGERPRINT)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)
    monkeypatch.setattr(runner, "child_env", lambda: {})
    monkeypatch.setattr(runner, "cpa_qa_cmd", lambda: "synthetic-judge-not-executed")
    monkeypatch.setattr(runner.time, "time", lambda: clock[0])
    return SimpleNamespace(base=base, segment=segment, clock=clock)


def _record(runtime, code="AGY_AND_GEMINI_API_FAILED", *, resumed=False):
    full_start, full_end = runner.song_proof_retry_window(200_000, 350_000, 900_000)
    record = {
        "candidate_id": CID, "segment": runtime.segment.name,
        "start_ms": 185_000, "end_ms": 370_000,
        "anchor_start_ms": 200_000, "anchor_end_ms": 350_000,
        "status": "blocked", "rc": 0, "window_classified_song": True,
        "song_complete": False, "reason_codes": [code, "SONG_FULL_BOUNDARY_PROOF_MISSING"],
        "transient_failure_code": code, "transient_retry_count": 2,
        "song_pipeline_fingerprint": FINGERPRINT, "pipeline_fingerprint": FINGERPRINT,
        "next_retry_at_epoch": 9_999, "session_id": "fixture-session",
    }
    if resumed:
        record.update(start_ms=full_start, end_ms=full_end,
                      retried_full_source=True, resumed_full_source_after_transient=True)
    else:
        record.update(full_source_authoritative_block=True, full_source_retry={
            "start_ms": full_start, "end_ms": full_end, "rc": 0,
            "status": "blocked", "window_classified_song": True, "song_complete": False,
            "reason_codes": [code], "transient_failure_code": code,
        })
    return record


@pytest.mark.parametrize("code", PROVIDER_CODES)
@pytest.mark.parametrize("resumed", [False, True])
def test_due_same_pipeline_provider_retry_resumes_full_stage(runtime, code, resumed):
    record = _record(runtime, code, resumed=resumed)
    state = {"songs": [record], "pending_song": []}
    assert runner.requeue_recoverable_songs(DATE, state) == 1
    item = state["pending_song"][0]
    assert item["resume_full_source"] is True
    assert item["transient_retry_count"] == 3
    assert (item["anchor_start_ms"], item["anchor_end_ms"]) == (200_000, 350_000)
    assert item["retry_reason"] == "transient_infrastructure_failure"
    assert "delivered" not in item


@pytest.mark.parametrize("change", [
    "no_full_attempt", "not_authoritative", "not_song", "full_positive",
    "different_failure", "missing_failure", "new_pipeline", "partial_resumed_flags",
])
def test_ineligible_resume_still_uses_normal_retry_path(runtime, change):
    record = _record(runtime)
    if change == "no_full_attempt":
        record.pop("full_source_retry")
    elif change == "not_authoritative":
        record["full_source_authoritative_block"] = False
    elif change == "not_song":
        record["full_source_retry"]["window_classified_song"] = False
    elif change == "full_positive":
        record["full_source_retry"]["song_complete"] = True
    elif change == "different_failure":
        record["full_source_retry"]["transient_failure_code"] = "AGY_TIMEOUT"
    elif change == "missing_failure":
        record["full_source_retry"].pop("transient_failure_code")
    elif change == "new_pipeline":
        record["song_pipeline_fingerprint"] = "sha256:" + "2" * 64
    else:
        record = _record(runtime, resumed=True)
        record.pop("resumed_full_source_after_transient")
    state = {"songs": [record], "pending_song": []}
    assert runner.requeue_recoverable_songs(DATE, state) == 1
    assert state["pending_song"][0]["resume_full_source"] is False


def test_future_backoff_and_existing_pending_are_not_bypassed(runtime):
    record = _record(runtime)
    record["next_retry_at_epoch"] = runtime.clock[0] + 1
    state = {"songs": [record], "pending_song": []}
    assert runner.requeue_recoverable_songs(DATE, state) == 0
    record["next_retry_at_epoch"] = runtime.clock[0] - 1
    state["pending_song"] = [{"cid": CID}]
    assert runner.requeue_recoverable_songs(DATE, state) == 0
    assert len(state["pending_song"]) == 1


@pytest.mark.parametrize("code", ["SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS",
                                  "SONG_AUDIO_LRC_ALIGNMENT_INVALID"])
def test_full_source_deterministic_negative_remains_terminal(runtime, code):
    record = _record(runtime, code)
    record.pop("transient_failure_code")
    record["song_pipeline_fingerprint"] = "sha256:" + "2" * 64
    state = {"songs": [record], "pending_song": []}
    assert runner.requeue_recoverable_songs(DATE, state) == 0
    assert record["status"] == "candidate_rejected"
    assert record["song_terminal_disposition"]["retryable"] is False
    assert state["pending_song"] == []


def _selector_fixture(runtime, monkeypatch):
    out = runtime.base / "out" / DATE / CID
    out.mkdir(parents=True)
    full = runner.song_proof_retry_window(200_000, 350_000, 900_000)
    tight = (max(0, 200_000 - runner.SONG_WINDOW_PRE_MS),
             min(900_000, 350_000 + runner.SONG_WINDOW_POST_MS))
    for tag, window in (("", tight), ("_full", full)):
        runner.song_window_media_path(out, CID, tag, *window).write_bytes(b"cached-fixture")

    def slice_srt(_source, _start, _end, dest):
        dest.write_text("1\n00:00:00,200 --> 00:00:01,200\n合成歌词\n", encoding="utf8")
        return 1

    monkeypatch.setattr(runner, "slice_srt", slice_srt)
    calls = []

    def fake_selector(command, **_kwargs):
        # A media miss or unexpected executable cannot silently turn into a pass.
        assert "--output-dir" in command
        directory = Path(command[command.index("--output-dir") + 1])
        inner = command[command.index("--seed-song-candidate-id") + 1]
        is_full = "--agy-audio-lrc-align" in command
        code = "AGY_AND_GEMINI_API_FAILED" if is_full else "SONG_FULL_BOUNDARY_PROOF_MISSING"
        summary = {"records": [{
            "candidate_id": inner, "candidate_dir": str(directory / inner),
            "decision_action": "BLOCK", "reason_codes": [code],
            "source_context_job": {"content_type_hint": "song", "song_candidate": True,
                "requires_full_source_song_boundary_redo": True,
                "song_repair_gate": {"status": "BLOCKED", "reason_codes": [code]}},
        }]}
        (directory / "summary.json").write_text(json.dumps(summary), encoding="utf8")
        calls.append({"full": is_full, "directory": directory, "inner": inner,
                      "command": command})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", fake_selector)
    return calls


def test_producer_preserves_distinct_full_summary_binding(runtime, monkeypatch):
    calls = _selector_fixture(runtime, monkeypatch)
    result = runner.produce_song(DATE, {
        "cid": CID, "segment_path": str(runtime.segment), "seg_dur_ms": 900_000,
        "anchor_start_ms": 200_000, "anchor_end_ms": 350_000,
    })
    assert [c["full"] for c in calls] == [False, True]
    full = result["full_source_retry"]
    assert full["selector_summary_path"] != result["selector_summary_path"]
    for record, call in ((result, calls[0]), (full, calls[1])):
        p = call["directory"] / "summary.json"
        assert record["selector_summary_path"] == str(p.resolve())
        assert record["selector_summary_sha256"] == "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
        assert record["selector_record_candidate_id"] == call["inner"]
    assert result["decision"] == "BLOCK" and result["song_complete"] is False
    assert "delivered" not in result and "song_delivery_recovery_authority" not in result


def test_requeue_to_producer_resumes_full_on_two_successive_retries(runtime, monkeypatch):
    calls = _selector_fixture(runtime, monkeypatch)
    result = runner.produce_song(DATE, {
        "cid": CID, "segment_path": str(runtime.segment), "seg_dur_ms": 900_000,
        "anchor_start_ms": 200_000, "anchor_end_ms": 350_000,
    })
    assert [c["full"] for c in calls] == [False, True]
    for _ in range(2):
        runtime.clock[0] = result["next_retry_at_epoch"] + 1
        state = {"songs": [result], "pending_song": []}
        assert runner.requeue_recoverable_songs(DATE, state) == 1
        before = len(calls)
        result = runner.produce_song(DATE, state["pending_song"][0])
        assert len(calls) == before + 1
        assert calls[-1]["full"] is True
        assert result["resumed_full_source_after_transient"] is True
        assert result["song_complete"] is False
        assert result["status"] == "blocked"
        assert "delivered" not in result
    assert [c["full"] for c in calls] == [False, True, True, True]


def test_talk_only_scope_does_not_call_song_recovery(runtime, monkeypatch):
    state = {"picks": [], "songs": [_record(runtime)], "pending_song": [],
             "song_backlog": [{"cid": "another_song"}], "song_selection_backlog": [],
             "song_superseded_attempts": []}
    before = deepcopy(state)
    monkeypatch.setattr(runner, "requeue_recoverable_talks", lambda *_args, **_kw: 0)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Talk-only authority cannot start Song recovery")

    monkeypatch.setattr(runner, "requeue_recoverable_songs", forbidden)
    monkeypatch.setattr(runner, "recover_bound_song_deliveries", forbidden)
    result = maintain_delivery_recovery_scope(DATE, state, automatic_maintenance=True,
                                             talk_candidate_ids=set())
    assert result[0] == result[3] == 0
    for key in ("songs", "pending_song", "song_backlog", "song_selection_backlog", "song_superseded_attempts"):
        assert state[key] == before[key]
