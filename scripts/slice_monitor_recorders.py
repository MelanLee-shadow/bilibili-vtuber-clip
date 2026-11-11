"""Recorder roster and per-recorder health for slice_monitor.py.

Which recorders exist, which one is in use and why another is paused is defined
once, in ``ops/recording/oci3-recording-topology.json`` (``recorders``).  The
monitor reads that and never hard-codes a host:

  paused  -> reported as a note with the recorded basis; never a problem.
  active  -> really probed.  ``container_files`` recorders (N100) must have the
             container running and, while the room is live, a recording file
             that keeps growing; while the room is offline they just wait.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ops.recording.recording_topology import CONFIG_RELATIVE, RecordingTopology, load_topology

LIVE_GRACE_SEC = 10 * 60      # recorder polls the room; allow time to attach after "live"
RECORDING_STALE_SEC = 5 * 60  # newest file untouched this long while live => not recording

# Runs on the recorder host (python3 over ssh stdin).  Read-only: docker inspect,
# directory listing, and the anonymous public room-info endpoint.
CONTAINER_FILES_PROBE_TEMPLATE = r'''
import json, os, re, subprocess, time, urllib.request
from datetime import datetime, timedelta, timezone

CONTAINER = "__CONTAINER__"
VIDEOS_ROOT = "__VIDEOS_ROOT__"
ROOM = "__ROOM__"
NOW = time.time()
out = {"ok": True, "now": NOW}

try:
    state = json.loads(subprocess.check_output(
        ["docker", "inspect", CONTAINER, "--format", "{{json .State}}"],
        text=True, stderr=subprocess.STDOUT, timeout=20))
    out["container"] = {"running": state.get("Running") is True,
                        "restarting": state.get("Restarting") is True,
                        "status": state.get("Status"), "started_at": state.get("StartedAt")}
except Exception as exc:
    out["container"] = {"running": False, "error": str(exc)[-200:]}

try:
    request = urllib.request.Request(
        "https://api.live.bilibili.com/room/v1/Room/get_info?room_id=" + ROOM,
        headers={"User-Agent": "Mozilla/5.0"})
    data = json.load(urllib.request.urlopen(request, timeout=10))["data"]
    status = data.get("live_status")
    since = None
    if status == 1 and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", str(data.get("live_time"))):
        since = datetime.strptime(data["live_time"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=8))).timestamp()
    out["live"] = {"status": status, "since_epoch": since}
except Exception as exc:
    out["live"] = {"status": None, "error": str(exc)[-200:]}

newest = None
room_dir = os.path.join(VIDEOS_ROOT, ROOM)
try:
    for date in sorted(d for d in os.listdir(room_dir) if re.fullmatch(r"\d{4}-\d\d-\d\d", d))[-2:]:
        for name in os.listdir(os.path.join(room_dir, date)):
            if not name.endswith(".flv"):
                continue
            stat = os.stat(os.path.join(room_dir, date, name))
            if newest is None or stat.st_mtime > newest["mtime"]:
                newest = {"file": date + "/" + name, "mtime": stat.st_mtime, "size": stat.st_size}
except OSError as exc:
    out["files_error"] = str(exc)[-200:]
out["newest"] = newest
print(json.dumps(out, ensure_ascii=False))
'''


def probe_script(spec: dict[str, str], room: str) -> str:
    return (CONTAINER_FILES_PROBE_TEMPLATE
            .replace("__CONTAINER__", spec["container"])
            .replace("__VIDEOS_ROOT__", spec["videos_root"])
            .replace("__ROOM__", room))


def run_container_files_probe(spec: dict[str, str], room: str) -> dict:
    argv = ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", spec["ssh_host"], "python3", "-"]
    try:
        done = subprocess.run(argv, input=probe_script(spec, room), capture_output=True,
                              text=True, timeout=90, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "fatal": f"ssh/probe error: {exc}"}
    if done.returncode != 0:
        return {"ok": False, "fatal": f"probe rc={done.returncode}: {done.stderr.strip()[-300:]}"}
    for line in reversed(done.stdout.splitlines()):
        if line.strip().startswith("{"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return {"ok": False, "fatal": f"unparseable probe output: {done.stdout[:300]}"}


def fmt_age(sec: float | None) -> str:
    if sec is None:
        return "n/a"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h{(sec % 3600) // 60}m"


def evaluate_recorders(recorders: dict[str, dict[str, str]],
                       results: dict[str, dict]) -> tuple[list[str], list[dict]]:
    """Return (notes, problems) for every recorder in the roster."""
    notes: list[str] = []
    problems: list[dict] = []
    for name, spec in recorders.items():
        if spec["state"] == "paused":
            notes.append(f"录制器 {name}: 已暂停（自 {spec['paused_since']}；依据 "
                         f"{spec['paused_basis']}），不计入告警")
            continue
        if spec["probe"] != "container_files":
            continue  # normalized_status recorders are evaluated with the host probe
        probe = results.get(name) or {"ok": False, "fatal": "no probe result"}
        host = spec["ssh_host"]
        if not probe.get("ok"):
            problems.append({"id": f"recorder_{name}_probe", "sev": "DOWN",
                             "msg": f"无法探测录制器 {name}（{host}）: {probe.get('fatal')}",
                             "fix": f"检查 `ssh {host}` 可达性与 {spec['container']} 容器；只读排查，不重启。"})
            continue
        container = probe.get("container") or {}
        newest = probe.get("newest")
        live = probe.get("live") or {}
        live_now = live.get("status") == 1
        newest_age = None if not newest else probe["now"] - newest["mtime"]
        notes.append(
            f"录制器 {name}: 容器={container.get('status')} 直播="
            f"{'是' if live_now else '否' if live.get('status') == 0 else '未知'} 最新录像="
            f"{newest['file'] if newest else '无'}（{fmt_age(newest_age)} 前更新）")
        if container.get("running") is not True or container.get("restarting"):
            problems.append({"id": f"recorder_{name}_down", "sev": "DOWN",
                             "msg": f"录制器 {name} 的容器 {spec['container']} 未在运行"
                                    f"（{container.get('status') or container.get('error')}）。",
                             "fix": f"在 {host} 上核对容器状态与日志后由行政决定恢复；监控不重启录制器。"})
            continue
        since = live.get("since_epoch")
        attached = live_now and (since is None or probe["now"] - since > LIVE_GRACE_SEC)
        if attached and (newest_age is None or newest_age > RECORDING_STALE_SEC):
            problems.append({"id": f"recorder_{name}_not_growing", "sev": "DOWN",
                             "msg": f"李豆沙在播，但录制器 {name} 的最新录像"
                                    f"{'不存在' if newest_age is None else '已 ' + fmt_age(newest_age) + ' 未增长'}。",
                             "fix": f"查 {host} 上 {spec['container']} 日志（CDN/画质回退/磁盘）；"
                                    "保留原始 FLV，监控不重启录制器。"})
    return notes, problems


def load_topology_at(project_root: str) -> RecordingTopology:
    return load_topology(Path(project_root) / CONFIG_RELATIVE)


def render_production_probe(template: str, topology: RecordingTopology, room: str, active: bool) -> str:
    """Fill the OCI3 host probe: ledger paths come from the topology ``state_dir``."""
    return (template.replace("__ROOM__", room)
            .replace("__STATUS_PATH__", str(topology.state_dir / "status.json"))
            .replace("__STATE_PATH__", str(topology.state_dir / "adapter-state.json"))
            .replace("__RECORDER_ACTIVE__", "True" if active else "False"))


def recording_view(recorders: dict[str, dict[str, str]], results: dict[str, dict]) -> dict | None:
    """Live state and newest recording of the active container recorder, if probed."""
    for name, spec in recorders.items():
        probe = results.get(name) or {}
        if spec["state"] == "active" and spec["probe"] == "container_files" and probe.get("ok"):
            newest = probe.get("newest") or {}
            return {"live": (probe.get("live") or {}).get("status") == 1,
                    "mtime": newest.get("mtime"), "date": newest.get("file", "").split("/")[0] or None,
                    "age_sec": int(probe["now"] - newest["mtime"]) if newest else None}
    return None
