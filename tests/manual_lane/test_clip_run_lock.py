"""Clip run lock: live contention ends with CLIP_RUN_LOCK_BUSY and is skipped-busy by the round runner
without burning retry budget or stuck/failed counts; an unheld lock with leftover metadata from a
terminated run is taken over visibly (stderr + round `took_over`, not an error); a held flock whose
recorded owner is dead remains distinguishable as CLIP_RUN_LOCK_STALE."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import SESSION

from src.autoslice.manual_lane import auto_run as ar
from src.autoslice.manual_lane import cli, multi_run
from src.autoslice.manual_lane.runner import Step, StepOutcome
from src.autoslice.manual_lane.state import (
    CLIP_RUN_LOCK_BUSY,
    CLIP_RUN_LOCK_STALE,
    ClipRunBusyError,
    ClipRunLockStatus,
    ClipRunStaleLockError,
    ClipState,
    StateError,
    clip_run_lock,
    clip_run_lock_path,
    inspect_clip_run_lock,
)

REPO = Path(__file__).resolve().parents[2]
WAIT_S = 10.0


def _make_clip(task: Path, cid: str, steps: dict | None = None) -> ClipState:
    info = {
        "cid": cid,
        "recording_date": "2026-11-09",
        "source_name": "s.flv",
        "start_ms": 0,
        "end_ms": 1000,
        "start_cue": 1,
        "end_cue": 2,
        "hook": "",
        "mac_bcut_srt": "x.srt",
        "remote_dir": str(task / "media" / cid),
    }
    state = ClipState.create(task / "state" / f"{cid}.json", clip=info, session=SESSION)
    if steps:
        state.data["steps"] = steps
        state.save()
    return state


def _hold_live_lock(state_path: Path) -> subprocess.Popen[str]:
    code = (
        "import pathlib, sys\n"
        "from src.autoslice.manual_lane.state import clip_run_lock\n"
        "with clip_run_lock(pathlib.Path(sys.argv[1])):\n"
        "    print('held', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(state_path)],
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    return proc


def _dead_pid() -> int:
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait(timeout=WAIT_S)
    return done.pid


def _create_crashed_unheld_lock(state_path: Path) -> int:
    proc = _hold_live_lock(state_path)
    pid = proc.pid
    proc.kill()
    proc.wait(timeout=WAIT_S)
    return pid


def _hold_stale_flock_with_dead_owner(state_path: Path, dead_pid: int) -> subprocess.Popen[str]:
    lock_path = clip_run_lock_path(state_path)
    code = (
        "import fcntl, json, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "os.ftruncate(fd, 0)\n"
        "os.lseek(fd, 0, os.SEEK_SET)\n"
        "os.write(fd, (json.dumps({'pid': int(sys.argv[2]), 'started_at': '2026-11-01T00:00:00Z'}) + '\\n').encode())\n"
        "print('held', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code, str(lock_path), str(dead_pid)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
    return holder


def _fake_one_step_lane(monkeypatch: pytest.MonkeyPatch, on_step=None) -> None:
    def run_review(ctx):
        if on_step is not None:
            on_step(ctx)
        product = ctx.clip_dir / "review.txt"
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text("ok", encoding="utf-8")
        return StepOutcome(True, products={"review": str(product)}, note="ok")

    monkeypatch.setattr(cli, "STEPS", (Step(9, "review", "步骤 review", run_review),))
    monkeypatch.setattr(cli, "DEPENDS", {9: ()})
    monkeypatch.setattr(cli, "GATES", {})
    monkeypatch.setattr(cli, "SPEAKER_STEP", 4)


def test_live_lock_raises_distinct_busy_state_code_and_clears_on_clean_exit(tmp_path):
    task = tmp_path / "task"
    state_path = _make_clip(task, "c").path
    assert inspect_clip_run_lock(state_path).state == "FREE"
    holder = _hold_live_lock(state_path)
    try:
        status = inspect_clip_run_lock(state_path)
        assert status.state == "BUSY" and status.code == CLIP_RUN_LOCK_BUSY and status.owner_pid == holder.pid
        with pytest.raises(ClipRunBusyError) as exc_info:
            with clip_run_lock(state_path):
                pytest.fail("live lock must not be granted to a second process")
        exc = exc_info.value
        assert isinstance(exc, StateError)
        assert exc.code == CLIP_RUN_LOCK_BUSY
        assert exc.owner_pid == holder.pid
        assert CLIP_RUN_LOCK_BUSY in str(exc)
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=WAIT_S)

    assert inspect_clip_run_lock(state_path).state == "FREE"
    with clip_run_lock(state_path) as takeover:
        assert takeover is None
    assert clip_run_lock_path(state_path).read_bytes() == b""


def test_unheld_lock_with_dead_owner_metadata_is_taken_over_and_logged(tmp_path, capsys):
    task = tmp_path / "task"
    state_path = _make_clip(task, "crashed").path
    dead_pid = _create_crashed_unheld_lock(state_path)

    status = inspect_clip_run_lock(state_path)
    assert status.state == "FREE" and status.code is None
    assert status.leftover is True and status.owner_pid == dead_pid

    with clip_run_lock(state_path) as takeover:
        assert takeover is not None
        assert takeover.previous_pid == dead_pid
        assert takeover.lock_path == clip_run_lock_path(state_path)
        # owner metadata overwritten with current live process pid while held
        owner = json.loads(clip_run_lock_path(state_path).read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()

    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(err_lines) == 1 and str(dead_pid) in err_lines[0]
    assert clip_run_lock_path(state_path).read_bytes() == b""
    assert inspect_clip_run_lock(state_path).leftover is False


@pytest.mark.parametrize("raw", [b"not-valid-json\n", b'{"started_at": "2026-11-01T00:00:00Z"}\n'])
def test_unheld_lock_with_corrupt_or_missing_pid_metadata_is_taken_over_gracefully(tmp_path, capsys, raw):
    task = tmp_path / "task"
    state_path = _make_clip(task, "corrupt").path
    clip_run_lock_path(state_path).write_bytes(raw)

    status = inspect_clip_run_lock(state_path)
    assert status.state == "FREE" and status.code is None and status.leftover is True and status.owner_pid is None

    with clip_run_lock(state_path) as takeover:
        assert takeover is not None
        assert takeover.previous_pid is None
        owner = json.loads(clip_run_lock_path(state_path).read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()

    err_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(err_lines) == 1 and "pid=unknown" in err_lines[0]
    assert clip_run_lock_path(state_path).read_bytes() == b""


def test_held_flock_with_dead_owner_pid_is_classified_as_stale_not_busy(tmp_path):
    task = tmp_path / "task"
    state_path = _make_clip(task, "orphan").path
    dead_pid = _dead_pid()
    holder = _hold_stale_flock_with_dead_owner(state_path, dead_pid)
    try:
        status = inspect_clip_run_lock(state_path)
        assert status.state == "STALE" and status.code == CLIP_RUN_LOCK_STALE and status.owner_pid == dead_pid
        with pytest.raises(ClipRunStaleLockError) as exc_info:
            with clip_run_lock(state_path):
                pytest.fail("dead-owner held flock must not be acquired")
        assert not isinstance(exc_info.value, ClipRunBusyError)
        assert exc_info.value.code == CLIP_RUN_LOCK_STALE and exc_info.value.owner_pid == dead_pid
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=WAIT_S)


def test_direct_cli_lock_callers_report_distinct_busy_and_stale_state_codes(tmp_path, capsys):
    task = tmp_path / "task"
    live_state = _make_clip(task, "live").path
    stale_state = _make_clip(task, "stale").path
    cover_src = tmp_path / "cover_src"
    cover_src.mkdir()

    holder = _hold_live_lock(live_state)
    try:
        commands = [
            ["run", "--task-dir", str(task), "--cid", "live"],
            ["resolve-dissent", "--task-dir", str(task), "--cid", "live", "--item", "1", "--keep", "--by", "owner", "--reason", "r"],
            ["revise", "--task-dir", str(task), "--cid", "live", "--reason", "r"],
            ["redo", "--task-dir", str(task), "--cid", "live", "--from", "6"],
            ["import-cover", "--task-dir", str(task), "--cid", "live", "--source-dir", str(cover_src), "--decided-by", "owner", "--authority", "a"],
        ]
        for argv in commands:
            assert cli.main(argv) == 2
            out = capsys.readouterr().out
            assert CLIP_RUN_LOCK_BUSY in out and CLIP_RUN_LOCK_STALE not in out, (argv, out)
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=WAIT_S)

    dead_pid = _dead_pid()
    stale_holder = _hold_stale_flock_with_dead_owner(stale_state, dead_pid)
    try:
        assert cli.main(["run", "--task-dir", str(task), "--cid", "stale"]) == 2
        out = capsys.readouterr().out
        assert CLIP_RUN_LOCK_STALE in out and str(dead_pid) in out and CLIP_RUN_LOCK_BUSY not in out
    finally:
        assert stale_holder.stdin is not None
        stale_holder.stdin.close()
        stale_holder.wait(timeout=WAIT_S)


def test_auto_round_records_live_locked_clip_as_skipped_busy_without_burning_budget_or_stuck_count(tmp_path):
    task = tmp_path / "task"
    busy_state = _make_clip(task, "busy", {"subtitles": {"status": "STOPPED", "reason": "ASR_MISSING_SOURCE"}}).path
    _make_clip(task, "free")
    # seed one prior stop in the ledger so we verify busy rounds neither add runs nor push it to stuck
    ar.ledger_path(task).parent.mkdir(parents=True, exist_ok=True)
    ar.ledger_path(task).write_text(
        json.dumps({
            "schema": ar.LEDGER_SCHEMA,
            "clips": {
                "busy": {
                    "runs": 1,
                    "stopped": {"step": "subtitles", "reason": "ASR_MISSING_SOURCE", "times": 1, "version": ""},
                }
            },
        }),
        encoding="utf-8",
    )

    holder = _hold_live_lock(busy_state)
    try:
        seen_runs: list[list[str]] = []

        def run_clips(cids: list[str]) -> int:
            seen_runs.append(list(cids))
            for cid in cids:
                st = ClipState.load(task / "state" / f"{cid}.json")
                st.data["steps"]["review"] = {"status": "DONE"}
                st.save()
            return 0

        for round_idx in range(5):
            result = ar.auto_round(task, discover_days=lambda: {"days": []}, run_clips=run_clips, upload=False)
            assert result["status"] == "ROUND_DONE"
            assert result["run_exit_code"] == 0
            assert result["skipped_busy"] == [{"cid": "busy", "code": CLIP_RUN_LOCK_BUSY, "owner_pid": holder.pid}]
            assert result["took_over"] == []
            assert result["stuck"] == []
            assert result["ran"] == (["free"] if round_idx == 0 else [])
            assert result["finished"] == (["free"] if round_idx == 0 else [])

        assert seen_runs == [["free"]]
        ledger = json.loads(ar.ledger_path(task).read_text(encoding="utf-8"))
        assert ledger["clips"]["busy"]["runs"] == 1
        assert ledger["clips"]["busy"]["stopped"]["times"] == 1
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=WAIT_S)


def test_auto_round_takes_over_unheld_dead_owner_lock_records_took_over_and_finishes(tmp_path, monkeypatch, capsys):
    task = tmp_path / "task"
    crashed_state = _make_clip(task, "crashed").path
    dead_pid = _create_crashed_unheld_lock(crashed_state)
    _fake_one_step_lane(monkeypatch)
    monkeypatch.setattr(cli, "_adapters_for", lambda on_media_host: lambda remote_dir: argparse.Namespace(remote=None))

    from src.autoslice.manual_lane import discover as discover_mod
    from src.autoslice.manual_lane import song_day

    monkeypatch.setattr(discover_mod, "discover", lambda **kw: argparse.Namespace(days=[], deferred=[], failures=[]))
    monkeypatch.setattr(song_day, "enabled", lambda: False)

    rc = cli.cmd_auto(argparse.Namespace(task_dir=str(task), on_media_host=True, upload=False))
    captured = capsys.readouterr()
    assert rc == 0
    summary = json.loads(captured.out.splitlines()[-1])
    assert summary["run_exit_code"] == 0
    assert summary["ran"] == ["crashed"]
    assert summary["finished"] == ["crashed"]
    assert summary["skipped_busy"] == []
    assert summary["stuck"] == []
    assert summary["took_over"] == [{"cid": "crashed", "previous_pid": dead_pid}]
    assert str(dead_pid) in captured.err

    ledger = json.loads(ar.ledger_path(task).read_text(encoding="utf-8"))
    assert ledger["clips"]["crashed"]["runs"] == 1
    assert "stopped" not in ledger["clips"]["crashed"]
    assert clip_run_lock_path(crashed_state).read_bytes() == b""

    # Next round sees it finished with no repeated takeover
    rc2 = cli.cmd_auto(argparse.Namespace(task_dir=str(task), on_media_host=True, upload=False))
    assert rc2 == 0
    summary2 = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary2["ran"] == [] and summary2["took_over"] == [] and summary2["run_exit_code"] == 0


def test_cmd_auto_handles_in_flight_live_lock_race_as_skipped_busy_and_surfaces_held_stale_lock(tmp_path, monkeypatch, capsys):
    task = tmp_path / "task"
    raced_state = _make_clip(task, "raced", {"subtitles": {"status": "STOPPED", "reason": "ASR_MISSING_SOURCE"}}).path
    _make_clip(task, "free")
    _fake_one_step_lane(monkeypatch)
    monkeypatch.setattr(cli, "_adapters_for", lambda on_media_host: lambda remote_dir: argparse.Namespace(remote=None))

    from src.autoslice.manual_lane import discover as discover_mod
    from src.autoslice.manual_lane import song_day

    monkeypatch.setattr(discover_mod, "discover", lambda **kw: argparse.Namespace(days=[], deferred=[], failures=[]))
    monkeypatch.setattr(song_day, "enabled", lambda: False)

    # Simulate a TOCTOU race: pre-dispatch inspect sees FREE, then a live process holds the lock
    # when cmd_auto's run_many actually calls _run_clip_cid.
    holder = _hold_live_lock(raced_state)
    try:
        real_inspect = ar.inspect_clip_run_lock

        def fake_inspect(p: Path) -> ClipRunLockStatus:
            if Path(p) == raced_state:
                return ClipRunLockStatus(state="FREE", code=None, lock_path=clip_run_lock_path(p))
            return real_inspect(p)

        monkeypatch.setattr(ar, "inspect_clip_run_lock", fake_inspect)
        rc = cli.cmd_auto(argparse.Namespace(task_dir=str(task), on_media_host=True, upload=False))
        assert rc == 0
        out_lines = capsys.readouterr().out.splitlines()
        summary = json.loads(out_lines[-1])
        assert summary["run_exit_code"] == 0
        assert summary["ran"] == ["free"]
        assert summary["finished"] == ["free"]
        assert summary["skipped_busy"] == [{"cid": "raced", "code": CLIP_RUN_LOCK_BUSY, "owner_pid": holder.pid}]
        assert summary["took_over"] == []
        assert summary["stuck"] == []

        raced_log = next((task / "run-logs").glob("raced-*.log")).read_text(encoding="utf-8")
        raced_doc = json.loads(raced_log.splitlines()[-1])
        assert raced_doc["status"] == "SKIPPED_BUSY"
        assert raced_doc["state_code"] == CLIP_RUN_LOCK_BUSY
        assert raced_doc["owner_pid"] == holder.pid

        ledger = json.loads(ar.ledger_path(task).read_text(encoding="utf-8"))
        assert "raced" not in ledger["clips"]
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=WAIT_S)

    # Now add a clip whose flock is STILL held while its recorded owner pid is dead:
    # cmd_auto must NOT skip it as busy and must surface CLIP_RUN_LOCK_STALE with code 2.
    stale_state = _make_clip(task, "stale").path
    dead_pid = _dead_pid()
    stale_holder = _hold_stale_flock_with_dead_owner(stale_state, dead_pid)
    try:
        rc = cli.cmd_auto(argparse.Namespace(task_dir=str(task), on_media_host=True, upload=False))
        assert rc == 0
        summary2 = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert summary2["run_exit_code"] == 2
        assert summary2["skipped_busy"] == []
        assert summary2["took_over"] == []
        assert "stale" in summary2["ran"]
        stale_log = next((task / "run-logs").glob("stale-*.log")).read_text(encoding="utf-8")
        assert CLIP_RUN_LOCK_STALE in stale_log and str(dead_pid) in stale_log
        assert CLIP_RUN_LOCK_BUSY not in stale_log
    finally:
        assert stale_holder.stdin is not None
        stale_holder.stdin.close()
        stale_holder.wait(timeout=WAIT_S)


def test_run_many_without_on_busy_keeps_exit_code_2_with_busy_state_code(tmp_path):
    task = tmp_path / "task"
    state_path = _make_clip(task, "busy").path
    holder = _hold_live_lock(state_path)
    try:
        def run_one(cid: str, _progress) -> int:
            with clip_run_lock(task / "state" / f"{cid}.json"):
                return 0

        code = multi_run.run_many(["busy"], run_one, log_dir=tmp_path / "logs", errors=(StateError,))
        assert code == 2
        log_text = next((tmp_path / "logs").glob("busy-*.log")).read_text(encoding="utf-8")
        assert CLIP_RUN_LOCK_BUSY in log_text
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=WAIT_S)


def test_cmd_auto_records_skipped_busy_when_trim_successor_is_live_locked(tmp_path, monkeypatch, capsys):
    task = tmp_path / "task"
    _make_clip(task, "orig")
    succ_state = _make_clip(task, "orig_trim").path

    def speaker_step(ctx):
        product = ctx.clip_dir / "speaker.txt"
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text("ok", encoding="utf-8")
        return StepOutcome(True, products={"speaker": str(product)}, note="ok")

    monkeypatch.setattr(cli, "STEPS", (Step(4, "speaker", "步骤 speaker", speaker_step),))
    monkeypatch.setattr(cli, "DEPENDS", {4: ()})
    monkeypatch.setattr(cli, "GATES", {})
    monkeypatch.setattr(cli, "SPEAKER_STEP", 4)
    monkeypatch.setattr(cli, "_adapters_for", lambda on_media_host: lambda remote_dir: argparse.Namespace(remote=None))
    monkeypatch.setattr(cli, "trim_clip", lambda **kw: {"status": "TRIMMED", "successor": "orig_trim", "passed": [(1, 2)]})

    from src.autoslice.manual_lane import discover as discover_mod
    from src.autoslice.manual_lane import song_day

    monkeypatch.setattr(discover_mod, "discover", lambda **kw: argparse.Namespace(days=[], deferred=[], failures=[]))
    monkeypatch.setattr(song_day, "enabled", lambda: False)

    holder = _hold_live_lock(succ_state)
    try:
        rc = cli.cmd_auto(argparse.Namespace(task_dir=str(task), on_media_host=True, upload=False))
        assert rc == 0
        summary = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert summary["run_exit_code"] == 0
        assert summary["ran"] == []
        assert summary["skipped_busy"] == [
            {"cid": "orig", "code": CLIP_RUN_LOCK_BUSY, "owner_pid": holder.pid},
            {"cid": "orig_trim", "code": CLIP_RUN_LOCK_BUSY, "owner_pid": holder.pid},
        ]
        assert summary["stuck"] == []
        ledger = json.loads(ar.ledger_path(task).read_text(encoding="utf-8"))
        assert "orig" not in ledger["clips"] and "orig_trim" not in ledger["clips"]
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=WAIT_S)
