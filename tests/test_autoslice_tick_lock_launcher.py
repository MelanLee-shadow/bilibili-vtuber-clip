from __future__ import annotations

from pathlib import Path

import scripts.session_autoslice as runner


ROOT = Path(__file__).resolve().parents[1]


def test_tick_yields_remaining_dates_to_deploy_guard(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "BASE", tmp_path)
    monkeypatch.setattr(runner, "cjk_font_present", lambda: True)
    monkeypatch.setattr(runner, "source_health_error", lambda: None)
    monkeypatch.setattr(runner, "recorder_live_status", lambda: False)
    monkeypatch.setattr(runner, "live_determination_basis", lambda live: {})
    monkeypatch.setattr(runner, "live_hold_recheck", lambda: False)
    monkeypatch.setattr(runner, "list_dates", lambda: ["2099-01-01", "2099-01-02"])
    monkeypatch.setattr(runner, "read_state", lambda date: {"status": "pending"})
    logs: list[str] = []
    monkeypatch.setattr(runner, "log", logs.append)
    processed: list[str] = []

    def fake_process(date: str) -> None:
        processed.append(date)
        (tmp_path / "deploy.guard").mkdir(exist_ok=True)

    monkeypatch.setattr(runner, "process_date", fake_process)

    assert runner.tick() == 0
    assert processed == ["2099-01-01"]
    assert any("deploy guard present" in message for message in logs)
    heartbeat = (tmp_path / "reports" / "heartbeat.txt").read_text(encoding="utf-8")
    assert "deploy_yield_deferred=2099-01-02" in heartbeat

    processed.clear()
    assert runner.tick() == 0
    assert processed == []
    heartbeat = (tmp_path / "reports" / "heartbeat.txt").read_text(encoding="utf-8")
    assert "dates=(none)" in heartbeat
    assert "deploy_yield_deferred=2099-01-01 2099-01-02" in heartbeat


def test_deploy_manages_runner_cron_with_tick_lock_not_runner_lock() -> None:
    deploy = (ROOT / "scripts" / "deploy_autoslice.sh").read_text(encoding="utf-8")
    old_live_line = (
        "*/10 * * * * /usr/bin/flock -n /opt/bilive/autoslice/runner.lock "
        "/bin/bash -lc '\\''cd /opt/bilive/autoslice/repo && "
        "AUTOSLICE_SPEAKER_MODE=uniform_host /usr/bin/python3 "
        "scripts/session_autoslice.py --once'\\'' "
        ">> /opt/bilive/autoslice/logs/runner.log 2>&1"
    )
    migrated = old_live_line.replace("runner.lock", "tick.lock").replace(
        "AUTOSLICE_SPEAKER_MODE=uniform_host",
        "AUTOSLICE_BASE=/opt/bilive/autoslice AUTOSLICE_SPEAKER_MODE=uniform_host",
    )
    assert f"runner_cron='{migrated}'" in deploy
    assert '"scripts/session_autoslice.py:$runner_cron"' in deploy
    assert "grep -Fv 'scripts/session_autoslice.py'" in deploy
    assert 'grep -Fxq "$runner_cron"' in deploy
    assert "runner.lock /bin/bash -lc" not in deploy
    # This is the exact effective launcher: all provider-bearing children now
    # receive the runtime root used by the shared cross-process slot pool.
    assert "AUTOSLICE_BASE=/opt/bilive/autoslice AUTOSLICE_SPEAKER_MODE=uniform_host /usr/bin/python3" in deploy


def test_deploy_critical_sections_hold_tick_while_acquiring_runner() -> None:
    deploy = (ROOT / "scripts" / "deploy_autoslice.sh").read_text(encoding="utf-8")
    nested = (
        '/usr/bin/flock -w 7200 "$REMOTE_BASE/tick.lock" '
        '/usr/bin/flock -w 7200 "$REMOTE_BASE/runner.lock" bash -s --'
    )
    # The producer-batch pre-swap drain joins rollback, tree swap, external
    # install, and identity sealing under the same concrete tick -> runner
    # nesting rather than two released drains.
    assert deploy.count(nested) == 5
    assert "; /usr/bin/flock -w 7200 '$REMOTE_BASE/runner.lock' true" not in deploy


def test_eval_and_watchdog_share_the_outer_tick_lock() -> None:
    eval_once = (ROOT / "scripts" / "run_eval_base_once.sh").read_text(encoding="utf-8")
    watchdog = (ROOT / "scripts" / "mount_watchdog.sh").read_text(encoding="utf-8")
    assert 'flock -n "$BASE/tick.lock" env' in eval_once
    assert 'TICK_LOCK="${AUTOSLICE_WATCHDOG_TICK_LOCK:-$BASE/tick.lock}"' in watchdog
    assert "tick.lock holder(s)" in watchdog
    assert "AUTOSLICE_WATCHDOG_RUNNER_LOCK" not in watchdog
