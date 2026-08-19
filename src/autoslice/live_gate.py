"""Live-broadcast gate: is the room live, and what is that claim based on?

Extracted from the runner (god-file decomposition) after the 
seven-hour freeze.  That incident's forensics had to be rebuilt from the
recorder's webhook journal because the runner logged exactly one bare line —
``room is LIVE — waiting for stream end`` — with no evidence attached, and the
tick that printed it exited three seconds later (journal: cron session opened
12:40:01, closed 12:40:04).  The lock was held by something else entirely.

Two lessons are encoded here:

* **Every live determination carries its evidence.**  ``recorder_status_basis``
  captures the raw fields; ``format_live_basis`` renders them next to the
  verdict, in the heartbeat and in the log.
* **Cross-source disagreement is an ALERT, never an override.**  The runner's
  hold is fail-safe by design: an idle recorder during a live room means
  *recording is broken*, which is strictly worse than a delayed batch.  See
  ``live_signal_divergence``.

Deliberately NOT a liveness signal: recording files appearing on the cloud
mount.  On a genuine 11:03Z→14:38Z session first materialised .mp4
bytes at 18:59Z — upload lag makes file growth useless as a veto, and only
weakly useful (day-directory presence) as corroboration.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

RECORDER_BASIS_FIELDS = (
    "live_status",
    "streaming",
    "recording",
    "finalizing",
    "rec_rate",
    "latest_source",
    "running_status",
    "service_reachable",
    "error",
)


def read_recorder_live_status(
    status_path: Path,
    *,
    room: str,
    max_age_seconds: float,
    log: Callable[[str], None],
) -> bool | None:
    """True=active, False=sealed, None=unknown (stale/down → fail-safe skip)."""

    try:
        if status_path.is_symlink() or not status_path.is_file():
            raise ValueError("status file missing or symlinked")
        data = json.loads(status_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != "recorder-neutral-status.v1":
            raise ValueError("status schema mismatch")
        if str(data.get("room_id")) != str(room):
            raise ValueError("status room mismatch")
        age = time.time() - float(data["generated_at_epoch"])
        if age < -300 or age > max_age_seconds:
            raise ValueError(f"status stale ({age:.0f}s)")
        if data.get("service_reachable") is not True or data.get("error"):
            raise ValueError(str(data.get("error") or "recorder service unreachable"))
        if data.get("finalizing") is True:
            return True
        if data.get("streaming") is True or data.get("recording") is True:
            return True
        live_status = data.get("live_status")
        if live_status not in (0, 1, False, True):
            raise ValueError("live status is unknown")
        return bool(live_status)
    except Exception as exc:  # noqa: BLE001 — any status failure means "unknown"
        log(f"recorder status unavailable: {exc}")
        return None


def live_hold_active(
    live: bool | None,
    *,
    rec_root: Path,
    list_dates: Callable[[], list[str]],
    log: Callable[[str], None],
    ignore_hold: bool,
) -> bool:
    """直播期间冻结处理（True/未知都冻结，fail-safe）。

    例外（维护者）：隔离回填 BASE 处理的是几天前的已关闭文件，
    直播期间跑它们数据上安全，只有资源争抢风险（由外部护栏管）。设
    ``AUTOSLICE_IGNORE_LIVE_HOLD=1`` 可豁免——但带自我防护：**只要本 BASE 的
    录像根里能看到今天（UTC 或北京日）的日期目录，豁免拒绝生效**，因此
    生产面即使误设该 env 也依然冻结；能豁免的只有只挂历史日期的隔离面。
    """

    if live is False:
        return False
    if not ignore_hold:
        return True
    moment = time.time()
    today_utc = time.strftime("%Y-%m-%d", time.gmtime(moment))
    today_cst = time.strftime("%Y-%m-%d", time.gmtime(moment + 8 * 3600))
    visible = set(list_dates())
    if visible & {today_utc, today_cst}:
        log(
            "AUTOSLICE_IGNORE_LIVE_HOLD=1 REFUSED: today's recordings are visible "
            f"in {rec_root} — live hold stays (production-shape base)"
        )
        return True
    log(
        f"live={live} but hold IGNORED (AUTOSLICE_IGNORE_LIVE_HOLD=1, isolated backfill "
        f"base over closed dates {sorted(visible)})"
    )
    return False


def recorder_status_basis(status_path: Path) -> dict:
    """Raw inputs behind the live decision — forensics only, never a decision.

    Never raises: an unreadable or non-object payload becomes ``status_error``.
    """

    basis: dict = {"status_path": str(status_path)}
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — evidence collection must not throw
        basis["status_error"] = str(exc)
        return basis
    if not isinstance(data, dict):
        basis["status_error"] = "status payload is not a JSON object"
        return basis
    for field in RECORDER_BASIS_FIELDS:
        basis[field] = data.get(field)
    try:
        basis["status_age_seconds"] = round(
            time.time() - float(data["generated_at_epoch"]), 1
        )
    except Exception:  # noqa: BLE001 — a missing stamp is itself evidence
        basis["status_age_seconds"] = None
    return basis


def format_live_basis(live: bool | None, basis: dict) -> str:
    """Compact one-line rendering of a live verdict together with its evidence."""

    fields = " ".join(
        f"{name}={basis[name]!r}"
        for name in (
            "status_error",
            "status_age_seconds",
            "today_recording_dir",
            *RECORDER_BASIS_FIELDS,
        )
        if name in basis
    )
    return f"live={live} basis[{fields}]"


def live_signal_divergence(live: bool | None, basis: dict) -> str | None:
    """'The room says live but the recorder is doing nothing' → ALERT, not veto.

    The hold STAYS: an idle recorder during a live room means recording is
    broken, and charging ahead would also put slice production on the box
    exactly when the recorder needs it.
    """

    if live is not True or basis.get("status_error"):
        return None
    if (
        basis.get("recording") is not False
        or basis.get("streaming") is not False
        or basis.get("finalizing") is not False
    ):
        return None
    rate = basis.get("rec_rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate != 0:
        return None
    return (
        f"recorder reports live_status={basis.get('live_status')!r} while "
        "recording/streaming/finalizing are all false and rec_rate=0 "
        f"(running_status={basis.get('running_status')!r}, "
        f"latest_source={basis.get('latest_source')!r}, "
        f"status_age={basis.get('status_age_seconds')}s) — the hold STAYS "
        "(fail-safe); recording is probably broken"
    )


def sustained_missing_recording_warning(
    live: bool | None,
    basis: dict,
    *,
    marker_path: Path,
    warn_after_seconds: float,
) -> str | None:
    """WARN once per hold when a live room has produced no day directory.

    Corroboration only — the verdict never changes.  The sustain window exists
    because the cloud mount lags: the day directory for an 11:03Z
    stream was not populated until 11:44Z.  State lives in one small marker so
    a four-hour broadcast cannot spam the alert file every ten minutes.
    """

    if live is not True:
        marker_path.unlink(missing_ok=True)
        return None
    if basis.get("today_recording_dir") is not False:
        marker_path.unlink(missing_ok=True)
        return None
    moment = time.time()
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        started = float(marker["missing_since_epoch"])
        warned = bool(marker.get("warned"))
    except Exception:  # noqa: BLE001 — a fresh or damaged marker restarts the window
        started, warned = moment, False
    if warned:
        return None
    if moment - started < warn_after_seconds:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps({"missing_since_epoch": started, "warned": False}),
            encoding="utf-8",
        )
        return None
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps({"missing_since_epoch": started, "warned": True}), encoding="utf-8"
    )
    return (
        f"room has been live for {int(moment - started)}s with no recording "
        "directory for today in the recordings root — the hold STAYS "
        "(fail-safe), but recording may be dropping this session"
    )


def positive_live_hold_recheck(
    status_path: Path,
    recorder_live_status: Callable[[], bool | None],
    live_hold_active_fn: Callable[[bool | None], bool],
) -> bool:
    """Positive-only live gate for use *inside* a running tick.

    The tick-start gate is deliberately conservative: unknown status freezes
    the whole tick because nothing has started yet.  Mid-tick the asymmetry
    flips — work is already in flight, and aborting every batch on a transient
    status blip would starve production — so only a POSITIVE reading yields.
    The next tick's start gate is still conservative, so exposure is bounded by
    one batch.  Routed through the caller's hold predicate so the
    ``AUTOSLICE_IGNORE_LIVE_HOLD`` backfill exemption keeps working, and
    short-circuited without a status file so non-free produce arms never pay.
    """

    try:
        if not status_path.is_file():
            return False
    except OSError:
        return False
    live = recorder_live_status()
    return live is True and live_hold_active_fn(live)


def live_determination_basis(
    live: bool | None, status_path: Path, list_dates: Callable[[], list[str]]
) -> dict:
    """Recorder evidence plus today's day-directory presence (corroboration).

    The directory probe only runs for a positive verdict — the only case it can
    corroborate — so an ordinary idle tick does not pay for a second listing of
    a slow cloud mount.
    """

    basis = recorder_status_basis(status_path)
    if live is True:
        moment = time.time()
        today = {
            time.strftime("%Y-%m-%d", time.gmtime(moment + offset))
            for offset in (0, 8 * 3600)
        }
        basis["today_recording_dir"] = bool(today & set(list_dates()))
    return basis


def live_hold_report(
    live: bool | None,
    basis: dict,
    *,
    marker_path: Path,
    warn_after_seconds: float,
) -> dict:
    """Heartbeat line, log line and every alert a live hold should raise."""

    evidence = format_live_basis(live, basis)
    alerts = [
        (name, message)
        for name, message in (
            ("LIVE_SIGNAL_DIVERGENCE", live_signal_divergence(live, basis)),
            (
                "LIVE_WITHOUT_RECORDING",
                sustained_missing_recording_warning(
                    live,
                    basis,
                    marker_path=marker_path,
                    warn_after_seconds=warn_after_seconds,
                ),
            ),
        )
        if message
    ]
    if live is None:
        return {
            "heartbeat": (
                "live=? source=ok (recorder status unavailable — fail-safe skip) "
                f"{evidence}"
            ),
            "log": f"live status unknown — fail-safe skip this tick: {evidence}",
            "alerts": alerts,
        }
    return {
        "heartbeat": f"live=True source=ok (waiting for stream end) {evidence}",
        # The tick RETURNS after this; it never sleeps and never keeps
        # runner.lock across the broadcast.  cron re-evaluates in 10 minutes.
        "log": f"room is LIVE — ending this tick, cron re-evaluates in 10min: {evidence}",
        "alerts": alerts,
    }


__all__ = [
    "RECORDER_BASIS_FIELDS",
    "format_live_basis",
    "live_determination_basis",
    "live_hold_active",
    "live_hold_report",
    "live_signal_divergence",
    "positive_live_hold_recheck",
    "read_recorder_live_status",
    "recorder_status_basis",
    "sustained_missing_recording_warning",
]
