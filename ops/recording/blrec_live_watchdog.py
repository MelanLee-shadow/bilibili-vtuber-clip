#!/usr/bin/env python3
"""Stateful, progress-aware watchdog for the production blrec process.

This script is invoked once per minute from the host cron. It deliberately
waits longer than blrec's own URL/quality retries, never restarts while a file
is growing or postprocessing is active, and rate-limits process replacement.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


ROOMS = [
    {"room_id": "22966160", "config": "/app/settings.toml", "port": 2233},
]

PROFILES = (
    {"name": "fmp4-original", "stream_format": "fmp4", "quality_number": 10000},
    {"name": "flv-original", "stream_format": "flv", "quality_number": 10000},
    {"name": "flv-250", "stream_format": "flv", "quality_number": 250},
)

LOG_DIR = Path("/app/logs/runtime")
RECORD_LOG_DIR = Path("/app/logs/record")
EVENT_DIR = Path("/app/Videos/record_health/watchdog")
STATE_PATH = LOG_DIR / "blrec_live_watchdog_state.v2.json"
LOCK_PATH = LOG_DIR / "blrec_live_watchdog.lock"

STATE_SCHEMA_VERSION = 2
STARTUP_GRACE_SECONDS = 180
STALL_SECONDS = 240
STABLE_BUDGET_RESET_SECONDS = 600
RESTART_WINDOW_SECONDS = 1800
MAX_RESTARTS_PER_WINDOW = 3
RESTART_BACKOFF_SECONDS = (0, 120, 300)
API_READY_TIMEOUT_SECONDS = 45
TERM_GRACE_SECONDS = 45
FINALIZATION_ALERT_SECONDS = 1800


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    profile_index: int
    progress: bool = False
    stall_age_seconds: Optional[int] = None
    retry_after_seconds: Optional[int] = None
    same_profile_reacquire: bool = False


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fresh_state() -> dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "rooms": {}}


def fresh_room_state() -> dict[str, Any]:
    return {
        "session_active": False,
        "session_live_time": None,
        "profile_index": 0,
        "same_profile_reacquire_used": False,
        "managed_pid": None,
        "restore_primary_pending": False,
        "starting_since": None,
        "no_progress_since": None,
        "healthy_since": None,
        "api_unready_since": None,
        "finalizing_since": None,
        "restart_times": [],
        "last_observation": {},
    }


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_PATH.read_text())
    except Exception:
        return fresh_state()
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        return fresh_state()
    if not isinstance(state.get("rooms"), dict):
        return fresh_state()
    return state


def save_state(state: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp_path, STATE_PATH)


def append_event(event: dict[str, Any]) -> None:
    event = dict(event)
    day_dir = EVENT_DIR / datetime.now().strftime("%Y-%m-%d")
    try:
        day_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with (day_dir / "events.jsonl").open("a", encoding="utf-8") as file:
            file.write(text + "\n")
    except OSError as exc:
        event["event_store_error"] = type(exc).__name__
    text = json.dumps(event, ensure_ascii=False, sort_keys=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "blrec-live-watchdog.log").open(
        "a", encoding="utf-8"
    ) as file:
        file.write(text + "\n")


def request_json(
    url: str,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 10,
) -> Any:
    req = urllib.request.Request(
        url,
        headers=headers or {"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def get_live_info(room_id: str) -> dict[str, Any]:
    url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
    try:
        data = request_json(url)
        room = data.get("data") or {}
        return {
            "ok": data.get("code") == 0,
            "code": data.get("code"),
            "message": data.get("message"),
            "title": room.get("title"),
            "live_status": room.get("live_status"),
            "live_time": room.get("live_time"),
        }
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def record_key() -> str:
    key = os.environ.get("RECORD_KEY", "")
    if not key:
        raise RuntimeError("RECORD_KEY is not present in the container environment")
    return key


def get_task_status(room: dict[str, Any], key: str) -> dict[str, Any]:
    url = f"http://127.0.0.1:{room['port']}/api/v1/tasks/data"
    headers = {"X-API-Key": key}
    try:
        data = request_json(url, headers=headers, timeout=5)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}

    tasks = data if isinstance(data, list) else []
    for task in tasks:
        info = task.get("room_info") or {}
        if str(info.get("room_id")) != room["room_id"]:
            continue
        status = task.get("task_status") or {}
        return {
            "ok": True,
            "title": info.get("title"),
            "live_status": info.get("live_status"),
            "monitor_enabled": status.get("monitor_enabled"),
            "recorder_enabled": status.get("recorder_enabled"),
            "running_status": status.get("running_status"),
            "recording_path": status.get("recording_path") or "",
            "postprocessing_path": status.get("postprocessing_path") or "",
            "postprocessor_status": status.get("postprocessor_status"),
            "rec_elapsed": status.get("rec_elapsed"),
            "rec_total": status.get("rec_total"),
            "rec_rate": status.get("rec_rate"),
            "dl_total": status.get("dl_total"),
            "dl_rate": status.get("dl_rate"),
            "danmu_total": status.get("danmu_total"),
            "real_stream_format": status.get("real_stream_format"),
            "real_quality_number": status.get("real_quality_number"),
        }
    return {"ok": False, "error": "task_not_found"}


def blrec_pids_for_port(port: int) -> list[int]:
    pids: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            parts = [
                part
                for part in (proc / "cmdline")
                .read_bytes()
                .decode(errors="ignore")
                .split("\0")
                if part
            ]
        except Exception:
            continue
        if not parts or not any("blrec" in part for part in parts):
            continue
        if "--port" not in parts:
            continue
        try:
            configured_port = int(parts[parts.index("--port") + 1])
        except (IndexError, ValueError):
            continue
        if configured_port == port:
            pids.append(int(proc.name))
    return sorted(pids)


def media_finalizer_pids() -> list[int]:
    """Return ffmpeg processes that have an argument under the recording tree."""
    pids: list[int] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            parts = [
                part
                for part in (proc / "cmdline")
                .read_bytes()
                .decode(errors="ignore")
                .split("\0")
                if part
            ]
        except Exception:
            continue
        if not parts or not any(Path(part).name == "ffmpeg" for part in parts):
            continue
        if any(part.startswith("/app/Videos/") for part in parts):
            pids.append(int(proc.name))
    return sorted(pids)


def recorder_writer_pids(room: dict[str, Any]) -> dict[str, list[int]]:
    return {
        "blrec": blrec_pids_for_port(int(room["port"])),
        "finalizers": media_finalizer_pids(),
    }


def path_probe(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        stat = Path(path).stat()
    except OSError:
        return {"path": path, "exists": False}
    return {
        "path": path,
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def probe_for_task(
    task: dict[str, Any],
    room_state: dict[str, Any],
) -> dict[str, Any]:
    path = (
        task.get("recording_path")
        or task.get("postprocessing_path")
        or (room_state.get("last_observation") or {}).get("path")
        or ""
    )
    return path_probe(path)


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def recording_made_progress(
    task: dict[str, Any],
    probe: dict[str, Any],
    previous: dict[str, Any],
) -> bool:
    if not previous:
        return True
    if _number(task.get("rec_total")) > _number(previous.get("rec_total")):
        return True
    if probe.get("path") and probe.get("path") != previous.get("path"):
        return True
    if (
        probe.get("exists")
        and probe.get("path") == previous.get("path")
        and _number(probe.get("size")) > _number(previous.get("size"))
    ):
        return True
    return False


def make_observation(
    task: dict[str, Any],
    probe: dict[str, Any],
) -> dict[str, Any]:
    return {
        "path": probe.get("path") or "",
        "exists": bool(probe.get("exists")),
        "size": probe.get("size"),
        "mtime_ns": probe.get("mtime_ns"),
        "rec_total": task.get("rec_total"),
        "rec_elapsed": task.get("rec_elapsed"),
        "running_status": task.get("running_status"),
    }


def reset_live_session(room_state: dict[str, Any]) -> None:
    room_state.update(
        {
            "session_active": False,
            "session_live_time": None,
            "same_profile_reacquire_used": False,
            "starting_since": None,
            "no_progress_since": None,
            "healthy_since": None,
            "api_unready_since": None,
            "finalizing_since": None,
            "last_observation": {},
        }
    )


def _prune_restart_times(now: float, room_state: dict[str, Any]) -> list[float]:
    restart_times = [
        float(timestamp)
        for timestamp in room_state.get("restart_times", [])
        if now - float(timestamp) < RESTART_WINDOW_SECONDS
    ]
    room_state["restart_times"] = restart_times
    return restart_times


def gated_recovery(
    now: float,
    room_state: dict[str, Any],
    *,
    action: str,
    reason: str,
    profile_index: int,
    progress: bool = False,
    stall_age_seconds: Optional[int] = None,
    same_profile_reacquire: bool = False,
) -> Decision:
    restart_times = _prune_restart_times(now, room_state)
    if len(restart_times) >= MAX_RESTARTS_PER_WINDOW:
        retry_after = max(
            1,
            int(RESTART_WINDOW_SECONDS - (now - restart_times[0])),
        )
        return Decision(
            action="none",
            reason="restart_circuit_open",
            profile_index=int(room_state.get("profile_index") or 0),
            progress=progress,
            stall_age_seconds=stall_age_seconds,
            retry_after_seconds=retry_after,
        )

    backoff_index = min(len(restart_times), len(RESTART_BACKOFF_SECONDS) - 1)
    required_backoff = RESTART_BACKOFF_SECONDS[backoff_index]
    if restart_times and now - restart_times[-1] < required_backoff:
        retry_after = max(1, int(required_backoff - (now - restart_times[-1])))
        return Decision(
            action="none",
            reason="restart_backoff",
            profile_index=int(room_state.get("profile_index") or 0),
            progress=progress,
            stall_age_seconds=stall_age_seconds,
            retry_after_seconds=retry_after,
        )

    return Decision(
        action=action,
        reason=reason,
        profile_index=profile_index,
        progress=progress,
        stall_age_seconds=stall_age_seconds,
        same_profile_reacquire=same_profile_reacquire,
    )


def source_recovery_decision(
    now: float,
    room_state: dict[str, Any],
    *,
    current_profile: int,
    reason: str,
    stall_age_seconds: int,
) -> Decision:
    """Reacquire once at the current profile before degrading.

    The last profile never wraps back to the preferred profile mid-live. Once
    its clean reacquire is exhausted, the still-running recorder may continue
    its own bounded source work, but the supervisor opens the circuit.
    """
    if not room_state.get("same_profile_reacquire_used"):
        return gated_recovery(
            now,
            room_state,
            action="restart_blrec",
            reason=f"{reason}_same_profile_reacquire",
            profile_index=current_profile,
            stall_age_seconds=stall_age_seconds,
            same_profile_reacquire=True,
        )
    if current_profile + 1 < len(PROFILES):
        return gated_recovery(
            now,
            room_state,
            action="restart_blrec",
            reason=f"{reason}_fallback_profile",
            profile_index=current_profile + 1,
            stall_age_seconds=stall_age_seconds,
        )
    return Decision(
        action="none",
        reason="source_recovery_exhausted_open_circuit",
        profile_index=current_profile,
        stall_age_seconds=stall_age_seconds,
    )


def evaluate_room(
    now: float,
    live_info: dict[str, Any],
    task: dict[str, Any],
    pids: list[int],
    room_state: dict[str, Any],
    probe: dict[str, Any],
) -> Decision:
    live_status = live_info.get("live_status") if live_info.get("ok") else None
    current_profile = int(room_state.get("profile_index") or 0)

    if live_status == 1:
        live_time = live_info.get("live_time")
        new_session = not room_state.get("session_active")
        changed_session = (
            live_time
            and room_state.get("session_live_time") not in (None, live_time)
        )
        if new_session or changed_session:
            reset_live_session(room_state)
            room_state["restart_times"] = []
        room_state["session_active"] = True
        room_state["session_live_time"] = live_time or room_state.get(
            "session_live_time"
        )
    elif live_status == 0 and room_state.get("session_active"):
        if current_profile != 0:
            room_state["restore_primary_pending"] = True
        reset_live_session(room_state)

    if not pids:
        target_profile = (
            0
            if live_status != 1 and room_state.get("restore_primary_pending")
            else current_profile
        )
        action = "start_blrec"
        reason = "blrec_process_missing_live" if live_status == 1 else "blrec_process_missing"
        return gated_recovery(
            now,
            room_state,
            action=action,
            reason=reason,
            profile_index=target_profile,
        )

    if live_status is None:
        return Decision(
            action="none",
            reason="public_live_status_unknown_fail_closed",
            profile_index=current_profile,
        )

    if live_status == 0:
        if task.get("ok"):
            postprocessing_active = bool(task.get("postprocessing_path")) or task.get(
                "running_status"
            ) in {"remuxing", "injecting"}
            if postprocessing_active:
                return Decision(
                    action="none",
                    reason="postprocessing_after_live_fail_closed",
                    profile_index=current_profile,
                )
            if room_state.get("restore_primary_pending"):
                return gated_recovery(
                    now,
                    room_state,
                    action="restart_blrec",
                    reason="restore_primary_after_live",
                    profile_index=0,
                )
            room_state["api_unready_since"] = None
            return Decision(
                action="none",
                reason="not_live",
                profile_index=current_profile,
            )
        unready_since = room_state.get("api_unready_since")
        if unready_since is None:
            room_state["api_unready_since"] = now
            return Decision(
                action="none",
                reason="offline_api_unready_grace",
                profile_index=current_profile,
            )
        age = int(now - float(unready_since))
        if age < STARTUP_GRACE_SECONDS:
            return Decision(
                action="none",
                reason="offline_api_unready_grace",
                profile_index=current_profile,
                stall_age_seconds=age,
            )
        return gated_recovery(
            now,
            room_state,
            action="restart_blrec",
            reason="offline_api_unready",
            profile_index=0,
            stall_age_seconds=age,
        )

    previous = room_state.get("last_observation") or {}
    if not task.get("ok"):
        if (
            previous
            and probe.get("exists")
            and probe.get("path") == previous.get("path")
            and _number(probe.get("size")) > _number(previous.get("size"))
        ):
            room_state["last_observation"] = {
                **previous,
                **probe,
            }
            room_state["api_unready_since"] = None
            return Decision(
                action="none",
                reason="media_growing_while_api_unready",
                profile_index=current_profile,
                progress=True,
            )
        unready_since = room_state.get("api_unready_since")
        if unready_since is None:
            room_state["api_unready_since"] = now
            return Decision(
                action="none",
                reason="live_api_unready_grace",
                profile_index=current_profile,
            )
        age = int(now - float(unready_since))
        start_age = (
            now - float(room_state["starting_since"])
            if room_state.get("starting_since") is not None
            else STARTUP_GRACE_SECONDS
        )
        if age < STARTUP_GRACE_SECONDS or start_age < STARTUP_GRACE_SECONDS:
            return Decision(
                action="none",
                reason="live_api_unready_grace",
                profile_index=current_profile,
                stall_age_seconds=age,
            )
        return gated_recovery(
            now,
            room_state,
            action="restart_blrec",
            reason="live_api_unready_same_profile_restart",
            profile_index=current_profile,
            stall_age_seconds=age,
        )

    room_state["api_unready_since"] = None
    running_status = task.get("running_status")
    postprocessing_active = bool(task.get("postprocessing_path")) or running_status in {
        "remuxing",
        "injecting",
    }
    if postprocessing_active:
        if room_state.get("finalizing_since") is None:
            room_state["finalizing_since"] = now
        room_state["no_progress_since"] = None
        room_state["healthy_since"] = None
        room_state["last_observation"] = make_observation(task, probe)
        finalizing_age = int(now - float(room_state["finalizing_since"]))
        reason = (
            "postprocessing_active"
            if finalizing_age < FINALIZATION_ALERT_SECONDS
            else "postprocessing_overdue_fail_closed"
        )
        return Decision(
            action="none",
            reason=reason,
            profile_index=current_profile,
            stall_age_seconds=finalizing_age,
        )
    room_state["finalizing_since"] = None

    recording = running_status == "recording" and bool(task.get("recording_path"))
    progress = recording and recording_made_progress(task, probe, previous)
    room_state["last_observation"] = make_observation(task, probe)
    if progress:
        room_state["no_progress_since"] = None
        if room_state.get("healthy_since") is None:
            room_state["healthy_since"] = now
        if now - float(room_state["healthy_since"]) >= STABLE_BUDGET_RESET_SECONDS:
            room_state["restart_times"] = []
            room_state["same_profile_reacquire_used"] = False
        return Decision(
            action="none",
            reason="recording_progress",
            profile_index=current_profile,
            progress=True,
        )

    room_state["healthy_since"] = None
    no_progress_since = room_state.get("no_progress_since")
    if no_progress_since is None:
        room_state["no_progress_since"] = now
        return Decision(
            action="none",
            reason="live_without_progress_grace",
            profile_index=current_profile,
        )

    stall_age = int(now - float(no_progress_since))
    start_age = (
        now - float(room_state["starting_since"])
        if room_state.get("starting_since") is not None
        else STARTUP_GRACE_SECONDS
    )
    if stall_age < STALL_SECONDS or start_age < STARTUP_GRACE_SECONDS:
        return Decision(
            action="none",
            reason="live_without_progress_grace",
            profile_index=current_profile,
            stall_age_seconds=stall_age,
        )

    return source_recovery_decision(
        now,
        room_state,
        current_profile=current_profile,
        reason="live_recording_stalled",
        stall_age_seconds=stall_age,
    )


def render_profile_config(base_text: str, profile: dict[str, Any]) -> str:
    rendered = base_text
    replacements = {
        "stream_format": f'"{profile["stream_format"]}"',
        "quality_number": str(profile["quality_number"]),
    }
    for key, value in replacements.items():
        rendered, count = re.subn(
            rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=.*$",
            f"{key} = {value}",
            rendered,
        )
        if count != 1:
            raise RuntimeError(
                f"expected exactly one {key} in recorder config, found {count}"
            )
    return rendered


def write_runtime_config(
    room: dict[str, Any],
    profile_index: int,
) -> Path:
    profile = PROFILES[profile_index]
    base_text = Path(room["config"]).read_text()
    rendered = render_profile_config(base_text, profile)
    path = Path(
        f"/tmp/blrec-watchdog-room-{room['room_id']}-{profile['name']}.toml"
    )
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(rendered)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    return path


def mark_source_incomplete(
    room: dict[str, Any],
    path: str,
    *,
    reason: str,
    live_info: dict[str, Any],
    profile_index: int,
) -> Optional[str]:
    if not path:
        return None
    source_path = Path(path)
    if not source_path.exists():
        return None
    marker_path = Path(f"{source_path}.incomplete.json")
    marker = {
        "schema_version": 1,
        "marked_at": now_iso(),
        "room_id": room["room_id"],
        "live_time": live_info.get("live_time"),
        "reason": reason,
        "profile": PROFILES[profile_index]["name"],
        "source_path": str(source_path),
        "source_size": source_path.stat().st_size,
    }
    tmp_path = marker_path.with_suffix(f"{marker_path.suffix}.tmp")
    tmp_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp_path, marker_path)
    return str(marker_path)


def stop_blrec(room: dict[str, Any]) -> dict[str, Any]:
    pids = blrec_pids_for_port(int(room["port"]))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + TERM_GRACE_SECONDS
    while time.time() < deadline:
        writers = recorder_writer_pids(room)
        if not writers["blrec"] and not writers["finalizers"]:
            return {
                "stopped_pids": pids,
                "remaining_pids": [],
                "finalizer_pids": [],
                "safe_to_start": True,
            }
        time.sleep(1)
    writers = recorder_writer_pids(room)
    return {
        "stopped_pids": pids,
        "remaining_pids": writers["blrec"],
        "finalizer_pids": writers["finalizers"],
        "safe_to_start": not writers["blrec"] and not writers["finalizers"],
    }


def start_blrec(
    room: dict[str, Any],
    key: str,
    profile_index: int,
) -> int:
    writers = recorder_writer_pids(room)
    if writers["blrec"] or writers["finalizers"]:
        raise RuntimeError("recorder or media finalizer is still active")
    RECORD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["python", "/app/patch_blrec.py"], check=True)
    config_path = write_runtime_config(room, profile_index)
    profile_name = PROFILES[profile_index]["name"]
    log_path = (
        RECORD_LOG_DIR
        / f"blrec-watchdog-room{room['room_id']}-{profile_name}-"
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    )
    log_file = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            [
                "blrec",
                "-c",
                str(config_path),
                "--open",
                "--host",
                "0.0.0.0",
                "--port",
                str(room["port"]),
                "--api-key",
                key,
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
    finally:
        log_file.close()
    return proc.pid


def enable_recorder(room: dict[str, Any], key: str) -> dict[str, Any]:
    url = (
        f"http://127.0.0.1:{room['port']}/api/v1/tasks/"
        f"{room['room_id']}/recorder/enable"
    )
    req = urllib.request.Request(
        url,
        headers={"X-API-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            response.read()
        return {"ok": True, "status": response.status}
    except urllib.error.HTTPError as exc:
        exc.read()
        return {"ok": False, "status": exc.code}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def wait_until_ready_and_enable(
    room: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    deadline = time.time() + API_READY_TIMEOUT_SECONDS
    last_task: dict[str, Any] = {"ok": False, "error": "not_checked"}
    while time.time() < deadline:
        last_task = get_task_status(room, key)
        if last_task.get("ok"):
            return {
                "api_ready": True,
                "enable": enable_recorder(room, key),
            }
        time.sleep(2)
    return {
        "api_ready": False,
        "error": last_task.get("error") or "api_ready_timeout",
    }


def commit_recovery(
    now: float,
    room_state: dict[str, Any],
    profile_index: int,
    pid: Optional[int],
    *,
    same_profile_reacquire: bool = False,
) -> None:
    previous_profile = int(room_state.get("profile_index") or 0)
    restart_times = _prune_restart_times(now, room_state)
    restart_times.append(now)
    room_state.update(
        {
            "profile_index": profile_index,
            "same_profile_reacquire_used": (
                True
                if same_profile_reacquire
                else (
                    False
                    if profile_index != previous_profile
                    else room_state.get("same_profile_reacquire_used", False)
                )
            ),
            "managed_pid": pid,
            "restore_primary_pending": (
                False
                if profile_index == 0
                else room_state.get("restore_primary_pending", False)
            ),
            "starting_since": now,
            "no_progress_since": None,
            "healthy_since": None,
            "api_unready_since": None,
            "finalizing_since": None,
            "restart_times": restart_times,
            "last_observation": {},
        }
    )


def event_from_observation(
    room: dict[str, Any],
    live_info: dict[str, Any],
    task: dict[str, Any],
    pids: list[int],
    decision: Decision,
    room_state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "time": now_iso(),
        "room_id": room["room_id"],
        "title": live_info.get("title") or task.get("title"),
        "live_status": live_info.get("live_status"),
        "live_time": live_info.get("live_time"),
        "task_ok": bool(task.get("ok")),
        "task_running_status": task.get("running_status"),
        "recording_path_present": bool(task.get("recording_path")),
        "postprocessing_active": bool(task.get("postprocessing_path")),
        "rec_total": task.get("rec_total"),
        "rec_rate": task.get("rec_rate"),
        "real_stream_format": task.get("real_stream_format"),
        "real_quality_number": task.get("real_quality_number"),
        "pids": pids,
        "profile": PROFILES[decision.profile_index]["name"],
        "progress": decision.progress,
        "stall_age_seconds": decision.stall_age_seconds,
        "retry_after_seconds": decision.retry_after_seconds,
        "same_profile_reacquire": decision.same_profile_reacquire,
        "restart_count_window": len(room_state.get("restart_times", [])),
        "reason": decision.reason,
        "action": decision.action,
    }


def run_once() -> int:
    key = record_key()
    state = load_state()
    summaries = []
    for room in ROOMS:
        room_id = room["room_id"]
        room_state = state["rooms"].setdefault(room_id, fresh_room_state())
        now = time.time()
        live_info = get_live_info(room_id)
        task = get_task_status(room, key)
        pids = blrec_pids_for_port(int(room["port"]))
        probe = probe_for_task(task, room_state)
        decision = evaluate_room(
            now,
            live_info,
            task,
            pids,
            room_state,
            probe,
        )
        event = event_from_observation(
            room,
            live_info,
            task,
            pids,
            decision,
            room_state,
        )
        if decision.action in {"start_blrec", "restart_blrec"}:
            stop_result: dict[str, Any] = {
                "stopped_pids": [],
                "remaining_pids": [],
                "finalizer_pids": [],
                "safe_to_start": True,
            }
            incomplete_marker: Optional[str] = None
            try:
                if decision.action == "restart_blrec":
                    if (
                        live_info.get("live_status") == 1
                        and decision.reason.startswith(
                            ("live_recording_stalled", "live_api_unready")
                        )
                    ):
                        incomplete_marker = mark_source_incomplete(
                            room,
                            task.get("recording_path")
                            or probe.get("path")
                            or "",
                            reason=decision.reason,
                            live_info=live_info,
                            profile_index=int(room_state.get("profile_index") or 0),
                        )
                    stop_result = stop_blrec(room)
                    if not stop_result["safe_to_start"]:
                        raise RuntimeError("recorder writers did not quiesce")
                pid = start_blrec(room, key, decision.profile_index)
                readiness = wait_until_ready_and_enable(room, key)
            except Exception as exc:
                applied_profile = int(room_state.get("profile_index") or 0)
                commit_recovery(
                    now,
                    room_state,
                    applied_profile,
                    None,
                    same_profile_reacquire=False,
                )
                event.update(
                    {
                        "stop_result": stop_result,
                        "incomplete_marker": incomplete_marker,
                        "recovery_result": {
                            "ok": False,
                            "error": type(exc).__name__,
                        },
                        "restart_count_window": len(room_state["restart_times"]),
                    }
                )
            else:
                commit_recovery(
                    now,
                    room_state,
                    decision.profile_index,
                    pid,
                    same_profile_reacquire=decision.same_profile_reacquire,
                )
                readiness_ok = bool(readiness.get("api_ready")) and bool(
                    (readiness.get("enable") or {}).get("ok")
                )
                event.update(
                    {
                        "started_pid": pid,
                        "stop_result": stop_result,
                        "incomplete_marker": incomplete_marker,
                        "readiness": readiness,
                        "recovery_result": {
                            "ok": readiness_ok,
                            "error": None if readiness_ok else "api_or_enable_not_ready",
                        },
                        "restart_count_window": len(room_state["restart_times"]),
                    }
                )
        append_event(event)
        summaries.append(
            {
                key: event.get(key)
                for key in (
                    "room_id",
                    "title",
                    "live_status",
                    "task_running_status",
                    "profile",
                    "progress",
                    "reason",
                    "action",
                )
            }
        )
    save_state(state)
    print(json.dumps({"time": now_iso(), "summary": summaries}, ensure_ascii=False))
    return 0


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(
            json.dumps(
                {"time": now_iso(), "summary": [], "reason": "watchdog_already_running"},
                ensure_ascii=False,
            )
        )
        return 0
    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
