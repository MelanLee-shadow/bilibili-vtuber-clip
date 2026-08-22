from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_manages_runner_cron_with_tick_lock_not_runner_lock() -> None:
    deploy = (ROOT / "scripts" / "deploy_free_autoslice.sh").read_text(encoding="utf-8")
    canonical = (
        "runner_cron='*/10 * * * * /usr/bin/flock -n "
        "/opt/bilive/autoslice/tick.lock env AUTOSLICE_BASE=/opt/bilive/autoslice "
        "python3 /opt/bilive/autoslice/repo/scripts/free_session_autoslice.py --once "
        ">> /opt/bilive/autoslice/logs/runner.log 2>&1'"
    )
    assert canonical in deploy
    assert '"scripts/free_session_autoslice.py:$runner_cron"' in deploy
    assert "grep -Fv 'scripts/free_session_autoslice.py'" in deploy
    assert 'grep -Fxq "$runner_cron"' in deploy
    assert "runner.lock env AUTOSLICE_BASE=/opt/bilive/autoslice python3 " not in deploy


def test_eval_and_watchdog_share_the_outer_tick_lock() -> None:
    eval_once = (ROOT / "scripts" / "run_eval_base_once.sh").read_text(encoding="utf-8")
    watchdog = (ROOT / "scripts" / "free_mount_watchdog.sh").read_text(encoding="utf-8")
    assert 'flock -n "$BASE/tick.lock" env' in eval_once
    assert 'TICK_LOCK="${AUTOSLICE_WATCHDOG_TICK_LOCK:-$BASE/tick.lock}"' in watchdog
    assert "tick.lock holder(s)" in watchdog
    assert "AUTOSLICE_WATCHDOG_RUNNER_LOCK" not in watchdog
