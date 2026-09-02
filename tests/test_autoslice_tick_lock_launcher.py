from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
