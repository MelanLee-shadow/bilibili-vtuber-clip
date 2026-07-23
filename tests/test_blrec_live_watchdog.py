from __future__ import annotations

import json
import signal
from copy import deepcopy

import pytest

from ops.recording import blrec_live_watchdog as watchdog


def live(live_status: int = 1, live_time: str = "2026-07-23 00:00:00") -> dict:
    return {
        "ok": True,
        "live_status": live_status,
        "live_time": live_time,
        "title": "test",
    }


def task(**overrides) -> dict:
    value = {
        "ok": True,
        "running_status": "recording",
        "recording_path": "/app/Videos/current.m4s",
        "postprocessing_path": "",
        "rec_total": 1_000,
        "rec_elapsed": 10,
        "rec_rate": 100,
        "real_stream_format": "fmp4",
        "real_quality_number": 10000,
    }
    value.update(overrides)
    return value


def probe(size: int = 1_000, path: str = "/app/Videos/current.m4s") -> dict:
    return {
        "path": path,
        "exists": True,
        "size": size,
        "mtime_ns": size,
    }


def evaluate(
    now: float,
    room_state: dict,
    *,
    live_info: dict | None = None,
    task_status: dict | None = None,
    pids: list[int] | None = None,
    current_probe: dict | None = None,
) -> watchdog.Decision:
    return watchdog.evaluate_room(
        now,
        live_info if live_info is not None else live(),
        task_status if task_status is not None else task(),
        pids if pids is not None else [123],
        room_state,
        current_probe if current_probe is not None else probe(),
    )


def test_growing_recording_is_never_restarted() -> None:
    state = watchdog.fresh_room_state()
    first = evaluate(0, state)
    second = evaluate(
        60,
        state,
        task_status=task(rec_total=2_000, rec_elapsed=70),
        current_probe=probe(size=2_000),
    )

    assert first.reason == "recording_progress"
    assert second.reason == "recording_progress"
    assert second.action == "none"
    assert second.progress is True


def test_stall_requires_four_minutes_then_reacquires_same_fmp4_profile() -> None:
    state = watchdog.fresh_room_state()
    evaluate(0, state)
    pending = evaluate(60, state)
    still_pending = evaluate(299, state)
    recovery = evaluate(300, state)

    assert pending.reason == "live_without_progress_grace"
    assert still_pending.action == "none"
    assert recovery.action == "restart_blrec"
    assert recovery.reason == "live_recording_stalled_same_profile_reacquire"
    assert recovery.same_profile_reacquire is True
    assert watchdog.PROFILES[recovery.profile_index]["name"] == "fmp4-original"


def test_second_stall_advances_from_fmp4_to_flv() -> None:
    state = watchdog.fresh_room_state()
    state["session_active"] = True
    state["session_live_time"] = "2026-07-23 00:00:00"
    state["no_progress_since"] = 0
    state["last_observation"] = watchdog.make_observation(task(), probe())
    watchdog.commit_recovery(
        300,
        state,
        0,
        123,
        same_profile_reacquire=True,
    )
    state["no_progress_since"] = 300
    state["last_observation"] = watchdog.make_observation(task(), probe())

    recovery = evaluate(600, state)

    assert recovery.reason == "live_recording_stalled_fallback_profile"
    assert recovery.action == "restart_blrec"
    assert recovery.same_profile_reacquire is False
    assert watchdog.PROFILES[recovery.profile_index]["name"] == "flv-original"


def test_wall_clock_elapsed_without_bytes_is_not_progress() -> None:
    state = watchdog.fresh_room_state()
    evaluate(0, state)
    evaluate(
        60,
        state,
        task_status=task(rec_elapsed=70),
        current_probe=probe(size=1_000),
    )
    recovery = evaluate(
        300,
        state,
        task_status=task(rec_elapsed=310),
        current_probe=probe(size=1_000),
    )

    assert recovery.reason == "live_recording_stalled_same_profile_reacquire"
    assert recovery.action == "restart_blrec"


def test_remux_is_fail_closed_even_when_overdue() -> None:
    state = watchdog.fresh_room_state()
    remuxing = task(
        running_status="remuxing",
        recording_path="",
        postprocessing_path="/app/Videos/completed.mp4.tmp",
    )
    first = evaluate(0, state, task_status=remuxing)
    overdue = evaluate(3_600, state, task_status=remuxing)

    assert first.reason == "postprocessing_active"
    assert overdue.reason == "postprocessing_overdue_fail_closed"
    assert overdue.action == "none"


def test_api_startup_delay_does_not_trigger_restart() -> None:
    state = watchdog.fresh_room_state()
    state["starting_since"] = 100
    unavailable = {"ok": False, "error": "ConnectionRefusedError"}

    first = evaluate(120, state, task_status=unavailable, current_probe={})
    second = evaluate(250, state, task_status=unavailable, current_probe={})

    assert first.reason == "live_api_unready_grace"
    assert second.reason == "live_api_unready_grace"
    assert second.action == "none"


def test_file_growth_protects_recording_when_api_is_unavailable() -> None:
    state = watchdog.fresh_room_state()
    evaluate(0, state)
    unavailable = {"ok": False, "error": "TimeoutError"}

    decision = evaluate(
        120,
        state,
        task_status=unavailable,
        current_probe=probe(size=2_000),
    )

    assert decision.reason == "media_growing_while_api_unready"
    assert decision.progress is True
    assert decision.action == "none"


def test_missing_process_is_started_without_a_kill() -> None:
    state = watchdog.fresh_room_state()
    decision = evaluate(0, state, pids=[])

    assert decision.action == "start_blrec"
    assert decision.profile_index == 0


def test_offline_api_failure_gets_a_grace_period_then_recovers_monitor() -> None:
    state = watchdog.fresh_room_state()
    unavailable = {"ok": False, "error": "ConnectionRefusedError"}
    offline = live(0, "")

    first = evaluate(
        0,
        state,
        live_info=offline,
        task_status=unavailable,
        current_probe={},
    )
    recovery = evaluate(
        watchdog.STARTUP_GRACE_SECONDS,
        state,
        live_info=offline,
        task_status=unavailable,
        current_probe={},
    )

    assert first.reason == "offline_api_unready_grace"
    assert recovery.reason == "offline_api_unready"
    assert recovery.action == "restart_blrec"


def test_restart_budget_opens_circuit() -> None:
    state = watchdog.fresh_room_state()
    state["session_active"] = True
    state["session_live_time"] = "2026-07-23 00:00:00"
    state["restart_times"] = [100, 200, 300]
    state["no_progress_since"] = 0
    state["last_observation"] = watchdog.make_observation(task(), probe())

    decision = evaluate(400, state)

    assert decision.action == "none"
    assert decision.reason == "restart_circuit_open"
    assert decision.retry_after_seconds == 1_500


def test_live_end_restores_primary_after_postprocessing_is_clear() -> None:
    state = watchdog.fresh_room_state()
    state.update(
        {
            "session_active": True,
            "session_live_time": "old",
            "profile_index": 2,
            "no_progress_since": 100,
            "last_observation": {"path": "old"},
        }
    )

    decision = evaluate(
        200,
        state,
        live_info=live(0, ""),
        task_status=task(
            running_status="waiting",
            recording_path="",
            rec_total=0,
            rec_elapsed=0,
        ),
        current_probe={},
    )

    assert decision.reason == "restore_primary_after_live"
    assert decision.action == "restart_blrec"
    assert decision.profile_index == 0
    assert state["profile_index"] == 2
    assert state["restore_primary_pending"] is True
    assert state["no_progress_since"] is None
    assert state["last_observation"] == {}


def test_live_end_waits_for_remux_before_restoring_primary() -> None:
    state = watchdog.fresh_room_state()
    state.update(
        {
            "session_active": True,
            "session_live_time": "old",
            "profile_index": 1,
        }
    )
    remuxing = task(
        running_status="remuxing",
        recording_path="",
        postprocessing_path="/app/Videos/completed.mp4.tmp",
    )

    decision = evaluate(
        200,
        state,
        live_info=live(0, ""),
        task_status=remuxing,
        current_probe=probe(path="/app/Videos/completed.mp4.tmp"),
    )

    assert decision.reason == "postprocessing_after_live_fail_closed"
    assert decision.action == "none"
    assert state["restore_primary_pending"] is True


def test_stable_progress_resets_only_budget_not_active_profile() -> None:
    state = watchdog.fresh_room_state()
    state["session_active"] = True
    state["session_live_time"] = "2026-07-23 00:00:00"
    state["profile_index"] = 2
    state["same_profile_reacquire_used"] = True
    state["restart_times"] = [1, 2, 3]

    evaluate(100, state)
    evaluate(
        701,
        state,
        task_status=task(rec_total=2_000, rec_elapsed=611),
        current_probe=probe(size=2_000),
    )

    assert state["restart_times"] == []
    assert state["profile_index"] == 2
    assert state["same_profile_reacquire_used"] is False


def test_last_profile_never_wraps_to_primary_mid_live() -> None:
    state = watchdog.fresh_room_state()
    state.update(
        {
            "session_active": True,
            "session_live_time": "2026-07-23 00:00:00",
            "profile_index": len(watchdog.PROFILES) - 1,
            "same_profile_reacquire_used": True,
            "no_progress_since": 0,
            "last_observation": watchdog.make_observation(task(), probe()),
        }
    )

    decision = evaluate(300, state)

    assert decision.action == "none"
    assert decision.reason == "source_recovery_exhausted_open_circuit"
    assert decision.profile_index == len(watchdog.PROFILES) - 1


def test_profile_config_rewrites_only_format_and_quality() -> None:
    base = """[[tasks]]
room_id = 22966160

[header]
cookie = "secret-value"

[recorder]
stream_format = "fmp4"
recording_mode = "standard"
quality_number = 10000
"""
    profile = deepcopy(watchdog.PROFILES[2])

    rendered = watchdog.render_profile_config(base, profile)

    assert 'stream_format = "flv"' in rendered
    assert "quality_number = 250" in rendered
    assert 'cookie = "secret-value"' in rendered
    assert 'recording_mode = "standard"' in rendered


def test_event_log_survives_unavailable_video_event_store(
    tmp_path, monkeypatch
) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    local_logs = tmp_path / "logs"
    monkeypatch.setattr(watchdog, "EVENT_DIR", blocked)
    monkeypatch.setattr(watchdog, "LOG_DIR", local_logs)

    watchdog.append_event({"room_id": "22966160", "action": "none"})

    event = json.loads(
        (local_logs / "blrec-live-watchdog.log").read_text().splitlines()[-1]
    )
    assert event["event_store_error"] in {"FileExistsError", "NotADirectoryError"}


def test_stop_never_hard_kills_or_starts_over_a_live_writer(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(watchdog, "blrec_pids_for_port", lambda port: [123])
    monkeypatch.setattr(
        watchdog,
        "recorder_writer_pids",
        lambda room: {"blrec": [123], "finalizers": [456]},
    )
    times = iter([0.0, 46.0])
    monkeypatch.setattr(watchdog.time, "time", lambda: next(times))
    monkeypatch.setattr(
        watchdog.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    result = watchdog.stop_blrec(watchdog.ROOMS[0])

    assert signals == [(123, signal.SIGTERM)]
    assert result["safe_to_start"] is False
    assert result["remaining_pids"] == [123]
    assert result["finalizer_pids"] == [456]


def test_start_refuses_when_a_media_finalizer_is_still_active(monkeypatch) -> None:
    monkeypatch.setattr(
        watchdog,
        "recorder_writer_pids",
        lambda room: {"blrec": [], "finalizers": [456]},
    )

    with pytest.raises(RuntimeError, match="still active"):
        watchdog.start_blrec(watchdog.ROOMS[0], "not-a-real-key", 0)


def test_recovery_marks_active_source_incomplete(tmp_path, monkeypatch) -> None:
    source = tmp_path / "active.m4s"
    source.write_bytes(b"recorded")
    monkeypatch.setattr(watchdog, "now_iso", lambda: "2026-07-23T01:02:03")

    marker_path = watchdog.mark_source_incomplete(
        watchdog.ROOMS[0],
        str(source),
        reason="live_recording_stalled_same_profile_reacquire",
        live_info=live(),
        profile_index=0,
    )

    assert marker_path == f"{source}.incomplete.json"
    marker = json.loads((tmp_path / "active.m4s.incomplete.json").read_text())
    assert marker["source_size"] == len(b"recorded")
    assert marker["profile"] == "fmp4-original"
    assert marker["reason"] == "live_recording_stalled_same_profile_reacquire"
