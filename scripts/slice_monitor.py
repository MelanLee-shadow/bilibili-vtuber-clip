#!/usr/bin/env python3
"""
李豆沙 自动切片监控 (lidousha auto-slice monitor)

Runs locally on 维护者's Mac (launchd), SSHes into the configured production
recording host, and checks the health of the bilive auto-slice pipeline for room
22966160 (李豆沙). The production profile defaults to the current OCI3 SSH alias;
legacy topology must be selected explicitly and must name its host.

What it watches
  - bilive_record container reachable
  - official BililiveRecorder adapter status is fresh
  - whether 22966160 is currently live/recording
  - whether the auto-slice loop (`src.burn.scan`) is running
  - whether the publish loop (`src.upload.upload`) is running  <-- must be OFF
  - whether finished recordings are actually producing slices + covers
  - scan log error bursts
  - disk headroom + recorder quality/IPv4/source-retention policy

Mutation policy
  - stop a detected legacy `src.upload.upload` process by default because
    publishing is explicitly forbidden; this safety stop can be disabled
  - NEVER restart the recorder unless AUTOSLICE_MONITOR_ALLOW_RECORDER_RESTART=1
  - never revive retired scan/local-prepare/shadow daemons
Everything else -> report a diagnosis and suggested manual fix, with no change.

Alerts: report file (always) + Apple Mail (iCloud) on problem/recovery transitions.

This script never publishes anything.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

# Sibling modules are imported as ``scripts.*`` (repo convention), which needs the
# project root on sys.path when this file is run directly.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.slice_monitor_crawlers import (
    CRAWLER_RESULT_CMD,
    crawler_note,
    crawler_problems,
    parse_crawler_results,
)
from scripts.slice_monitor_probes import (
    JINGTING_PROBE_PY,
    LEGACY_PROBE_TEMPLATE,
    PRODUCTION_PROBE_TEMPLATE,
)
from scripts.slice_monitor_recorders import (
    evaluate_recorders,
    fmt_age,
    load_topology_at,
    recording_view,
    render_production_probe,
    run_container_files_probe,
)
from scripts.slice_monitor_reports import (
    atomic_write_json,
    prune_history,
    write_reports as _write_reports,
)

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


MONITOR_PROFILE = os.environ.get("AUTOSLICE_MONITOR_PROFILE", "production").strip().lower()
if MONITOR_PROFILE not in {"production", "legacy"}:
    raise RuntimeError("AUTOSLICE_MONITOR_PROFILE must be production or legacy")
if MONITOR_PROFILE == "legacy" and not os.environ.get("AUTOSLICE_MONITOR_SSH_HOST", "").strip():
    raise RuntimeError("legacy monitor profile requires AUTOSLICE_MONITOR_SSH_HOST")
_DEFAULT_SSH_HOST = "localhost"
SSH_HOST = os.environ.get("AUTOSLICE_MONITOR_SSH_HOST", _DEFAULT_SSH_HOST).strip()
if not SSH_HOST:
    raise RuntimeError("AUTOSLICE_MONITOR_SSH_HOST must not be empty")
_DEFAULT_CONTAINER = "bililive_recorder" if MONITOR_PROFILE == "production" else "bilive_record"
CONTAINER = os.environ.get("AUTOSLICE_MONITOR_CONTAINER", _DEFAULT_CONTAINER).strip()
# Rooms, recorders and the ledger directory come from the one recording topology.
TOPOLOGY = load_topology_at(PROJECT_ROOT)
RECORDERS = TOPOLOGY.recorders
PRIMARY_ROOM = os.environ.get("AUTOSLICE_MONITOR_ROOM", TOPOLOGY.room).strip()
# The recorder on the configured production host; paused by topology => no health probe.
_HOST_RECORDER = next((r for r in RECORDERS.values()
                       if r["probe"] == "normalized_status" and r["ssh_host"] == SSH_HOST), None)
HOST_RECORDER_ACTIVE = _HOST_RECORDER is None or _HOST_RECORDER["state"] == "active"
ROOMS = [PRIMARY_ROOM]
ALLOW_RECORDER_RESTART = _env_bool(
    "AUTOSLICE_MONITOR_ALLOW_RECORDER_RESTART", default=False
)
ALLOW_UPLOAD_KILL = _env_bool("AUTOSLICE_MONITOR_ALLOW_UPLOAD_KILL", default=True)


def _ssh_argv(*remote_args: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        SSH_HOST,
        *remote_args,
    ]

# State/log directory: derived from this file's location (no hard-coded checkout
# path); AUTOSLICE_MONITOR_REPORT_DIR redirects it, e.g. for a read-only trial run.
REPORT_DIR = os.path.abspath(
    os.environ.get("AUTOSLICE_MONITOR_REPORT_DIR")
    or os.path.join(PROJECT_ROOT, "reports", "slice_monitor")
)
STATE_FILE = os.path.join(REPORT_DIR, "state.json")
LATEST_JSON = os.path.join(REPORT_DIR, "latest.json")
LATEST_MD = os.path.join(REPORT_DIR, "latest.md")

EMAIL_TO = ""  # 收件人由 smtp.json 的 to 字段提供；留空禁用兜底
EMAIL_ACCOUNT = "iCloud"


EMAIL_ENABLED = False

# thresholds
RECORDING_ACTIVE_AGE = 300      # file touched within 5 min => live/recording
SLICE_GRACE_SEC = 90 * 60       # after a stream ends, allow 90 min before "no slices" is a problem
DISK_MIN_BYTES = 15 * 1024**3   # warn under 15 GiB free

CET8 = timezone(timedelta(hours=8))

# ----------------------------------------------------------------------------
# Remote probe (runs INSIDE the container via `docker exec ... python3 -`)
# ----------------------------------------------------------------------------
PROBE_PY = LEGACY_PROBE_TEMPLATE.replace("__ROOMS__", '", "'.join(ROOMS))


PRODUCTION_PROBE_PY = render_production_probe(
    PRODUCTION_PROBE_TEMPLATE, TOPOLOGY, PRIMARY_ROOM, HOST_RECORDER_ACTIVE)


def run_production_probe():
    """Read normalized recorder health on the configured production host."""
    try:
        completed = subprocess.run(
            _ssh_argv("/usr/bin/python3", "-"),
            input=PRODUCTION_PROBE_PY,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "fatal": "production ssh/probe timeout"}
    except Exception as exc:
        return {"ok": False, "fatal": f"production ssh error: {exc}"}
    if completed.returncode != 0:
        return {
            "ok": False,
            "fatal": (
                f"production probe rc={completed.returncode}: "
                f"{completed.stderr.strip()[-400:]}"
            ),
        }
    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {
        "ok": False,
        "fatal": f"unparseable production probe output: {completed.stdout[:400]}",
    }


# ----------------------------------------------------------------------------
# Remote helpers
# ----------------------------------------------------------------------------
def run_probe(last_audio_file=""):
    """Run the retired container-layout probe in explicit legacy mode only."""
    if MONITOR_PROFILE != "legacy":
        raise RuntimeError("legacy container probe requested outside legacy profile")
    probe_src = PROBE_PY.replace("__LAST_AUDIO__", last_audio_file or "")
    try:
        p = subprocess.run(
            _ssh_argv("docker", "exec", "-i", CONTAINER, "python3", "-"),
            input=probe_src, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"ok": False, "fatal": "ssh/probe timeout"}
    except Exception as e:
        return {"ok": False, "fatal": f"ssh error: {e}"}
    if p.returncode != 0:
        return {"ok": False, "fatal": f"probe rc={p.returncode}: {p.stderr.strip()[:400]}"}
    out = p.stdout.strip()
    # tolerate leading noise; take last JSON line
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"ok": False, "fatal": f"unparseable probe output: {out[:400]}"}


def run_jingting_probe():
    """Probe the retired host-side fine-transcription lane only in legacy mode."""
    if MONITOR_PROFILE != "legacy":
        return {
            "ok": True,
            "skipped": True,
            "reason": "legacy host-side jingting lane is not part of the production profile",
        }
    remote_py = JINGTING_PROBE_PY
    try:
        p = subprocess.run(
            _ssh_argv("python3", "-"),
            input=remote_py, capture_output=True, text=True, timeout=60)
    except Exception as e:
        return {"ok": False, "fatal": f"jingting probe ssh error: {e}"}
    if p.returncode != 0:
        return {"ok": False, "fatal": f"jingting probe rc={p.returncode}: {p.stderr.strip()[:300]}"}
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "fatal": f"jingting probe parse error: {p.stdout[:300]}"}


def run_autoslice_probe():
    """Health of the autoslice runner on the configured production host."""
    cmd = (
        "sudo -n cat /opt/bilive/autoslice/reports/heartbeat.txt 2>/dev/null; echo __SEP__; "
        "for f in /opt/bilive/autoslice/reports/ALERT_*.txt; do "
        "[ -f \"$f\" ] && echo \"$f|$(stat -c %Y \"$f\")|$(tail -1 \"$f\")\"; done 2>/dev/null; "
        "echo __CRAWL__; " + CRAWLER_RESULT_CMD + "; echo __DISABLED__; "
        "[ -e /opt/bilive/autoslice/DISABLED ] && echo yes; true"
    )
    try:
        p = subprocess.run(
            _ssh_argv(cmd), capture_output=True, text=True, timeout=40
        )
    except Exception as e:
        return {"fatal": f"ssh error: {e}"}
    if p.returncode != 0:
        return {"fatal": f"rc={p.returncode}: {p.stderr.strip()[:200]}"}
    head, _, alerts_raw = p.stdout.partition("__SEP__")
    alerts_raw, _, crawl_raw = alerts_raw.partition("__CRAWL__")
    crawl_raw, _, disabled_raw = crawl_raw.partition("__DISABLED__")
    heartbeat = head.strip() or None
    out = {"heartbeat": heartbeat, "age_sec": None, "alerts": [],
           "crawlers": parse_crawler_results(crawl_raw),
           "runner_disabled": disabled_raw.strip() == "yes"}
    if heartbeat:
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})", heartbeat)
        if m:
            try:
                from datetime import datetime
                out["age_sec"] = time.time() - datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z").timestamp()
            except ValueError:
                pass
    now = time.time()
    for line in alerts_raw.strip().splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        path_, mtime_, last = parts
        try:
            fresh = (now - float(mtime_)) < 6 * 3600
        except ValueError:
            fresh = False
        if fresh:  # only recent alerts; old ones would spam every report forever
            out["alerts"].append({"name": os.path.basename(path_), "last": last})
    return out


def ssh_exec(cmd_inside_container, detached=False):
    """Run a bash command inside the container over ssh.

    The inner command is shlex-quoted into a single remote string; otherwise ssh
    re-splits a command containing &&, >>, pipes or spaces and `bash -lc` gets a
    broken fragment (this silently dropped scan/local_prepare starts).
    """
    import shlex
    dflag = "-d " if detached else ""
    remote = f"docker exec {dflag}{CONTAINER} bash -lc {shlex.quote(cmd_inside_container)}"
    base = _ssh_argv(remote)
    try:
        p = subprocess.run(base, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def kill_upload():
    if MONITOR_PROFILE == "production":
        try:
            completed = subprocess.run(
                _ssh_argv("pkill", "-9", "-f", "src.upload.upload"),
                capture_output=True,
                text=True,
                timeout=40,
                check=False,
            )
        except Exception:
            return False
        return completed.returncode == 0
    rc, _out, _err = ssh_exec("pkill -9 -f 'src.upload.upload'; sleep 1; echo done")
    return rc == 0


def restart_recorder(room):
    """Restart official BililiveRecorder after two live no-growth observations."""
    if room != PRIMARY_ROOM:
        return False
    try:
        p = subprocess.run(
            _ssh_argv("docker", "restart", "bililive_recorder"),
            capture_output=True, text=True, timeout=40)
        return p.returncode == 0
    except Exception:
        return False


def evaluate(probe, state):
    """Return (verdict, problems, actions, notes). verdict in OK/WARN/DEGRADED/DOWN."""
    actions = []    # auto actions taken
    notes, problems = evaluate_recorders(RECORDERS, probe.get("recorder_probes") or {})

    if not probe.get("ok"):
        problems.append({"id": "probe", "sev": "DOWN",
                         "msg": f"无法探测 {SSH_HOST}/容器: {probe.get('fatal')}",
                         "fix": f"检查 `ssh {SSH_HOST}` 可达性、主机身份和 bilive_record 容器。"})
        return "DOWN", problems, actions, notes

    procs = probe["procs"]
    primary = probe["rooms"].get(PRIMARY_ROOM, {})
    rec = primary.get("recording", {})
    sl = primary.get("slices", {})
    dirty = primary.get("dirty_backlog", [])
    flags = probe.get("flags", {})
    api = primary.get("recorder_status") or {}
    # Authoritative live state from the normalized recorder status; fall back
    # to file freshness only when the status document is unavailable.  A paused
    # host recorder has no status: the in-use recorder (topology) answers instead.
    live_status = api.get("live_status")
    view = None if HOST_RECORDER_ACTIVE else recording_view(RECORDERS, probe.get("recorder_probes") or {})
    if view:
        live = view["live"]
        rec = {"active": live, "mtime": view["mtime"], "age_sec": view["age_sec"]}
        primary = {**primary, "latest_date_dir": view["date"]}
    elif live_status is not None:
        live = (live_status == 1) or api.get("finalizing") is True
    else:
        live = bool(rec.get("active"))
    rec_rate = api.get("rec_rate")
    rec_total = api.get("total_output_bytes")
    if rec_total is None:
        rec_total = api.get("rec_total")
    # cloud-mounted Videos doesn't update mtime live, so file-freshness can't tell the in-progress
    # recording from backlog. When the API says she's live, the latest date dir is the live session,
    # not dirty backlog — drop it from the dirty set.
    latest_dir = primary.get("latest_date_dir")
    if live and latest_dir:
        dirty = [d for d in dirty if d.get("dir") != latest_dir]

    notes.append(
        f"monitor profile={MONITOR_PROFILE} host={SSH_HOST} "
        f"recorder_restart={'enabled' if ALLOW_RECORDER_RESTART else 'disabled'} "
        f"upload_safety_stop={'enabled' if ALLOW_UPLOAD_KILL else 'disabled'}"
    )
    notes.append(f"recorder={procs['recorder']} scan={procs['scan']} "
                 f"local_prepare={procs.get('local_prepare', 0)} upload={procs['upload']} "
                 f"auto_review_shadow={procs.get('auto_review_shadow', 0)}")
    notes.append(f"李豆沙 live={'YES' if live else 'no'} "
                 f"录制最新={primary.get('latest_date_dir')} "
                 f"(age {fmt_age(rec.get('age_sec'))})")
    notes.append(f"切片最新 age={fmt_age(sl.get('age_sec'))} "
                 f"count={sl.get('count_latest_dir')} covers={sl.get('cover_count_latest_dir')} "
                 f"publish.json={sl.get('publish_json_latest_dir')}")
    record_health = probe.get("record_health")
    if isinstance(record_health, dict):
        unresolved = list(record_health.get("unresolved") or [])
        notes.append(
            "record-health "
            f"status={record_health.get('status')} "
            f"status_age={record_health.get('status_age_seconds')}s "
            f"state_age={record_health.get('adapter_state_age_seconds')}s "
            f"unresolved={len(unresolved)}"
        )
        if record_health.get("healthy") is not True:
            down = (
                record_health.get("service_reachable") is False
                or any(str(item).startswith("status stale:") for item in unresolved)
                or any("missing or not a regular file" in str(item) for item in unresolved)
            )
            problems.append({
                "id": "record_health_audit",
                "sev": "DOWN" if down else "DEGRADED",
                "msg": "OCI3 normalized recorder-health audit failed.",
                "fix": (
                    f"Inspect {SSH_HOST}:/opt/bilive/recording/status.json, "
                    "adapter-state.json and the deployed record_health_audit.py result."
                ),
                "detail": [str(item) for item in unresolved[:6]],
            })
    jt = probe.get("jingting") or {}
    if jt.get("skipped"):
        notes.append(f"jingting skipped: {jt.get('reason')}")
    elif jt.get("ok"):
        notes.append(f"jingting agy daemon={jt.get('daemon_count')} agy={jt.get('agy_count')} "
                     f"latest={jt.get('latest_date')} pending={jt.get('pending_latest')} "
                     f"done={jt.get('done_latest')} review_required={jt.get('review_required_latest', 0)} "
                     f"log_age={fmt_age(jt.get('log_age_sec'))}")
        if jt.get("pending_latest", 0) > 0 and jt.get("daemon_count", 0) == 0:
            # .jingting pendings are OLD-plane hybrid slices; with that plane
            # retired  the stale backlog must not WARN forever.
            if procs.get("scan", 0) > 0:
                problems.append({"id": "jingting_daemon_down", "sev": "WARN",
                                 "msg": "已有待精听切片，但 agy jingting daemon 未运行。",
                                 "fix": "启动宿主机精听 daemon：`/opt/bilive/app/scripts/gemini_slice_jingting.py --provider agy --daemon --room 22966160`。"})
            else:
                notes.append(f"旧管线遗留 {jt.get('pending_latest')} 个 .jingting 待精听切片（旧面已退役，仅存档不告警）")
        if jt.get("review_required_latest", 0) > 0:
            problems.append({"id": "jingting_review_required", "sev": "WARN",
                             "msg": f"{jt.get('review_required_latest')} 条精听字幕需要人工/歌词校对。",
                             "fix": "打开本地 review 包或远端 `.jingting.review-required.json`，不要把这些字幕直接 burn/upload。"})
    elif jt:
        notes.append(f"jingting probe failed: {jt.get('fatal')}")
    ars = probe.get("auto_review_shadow") or {}
    if ars:
        counts = ars.get("last_summary_counts") or {}
        gaps = ars.get("last_gap_summary") or {}
        notes.append("auto-review shadow "
                     f"age={fmt_age(ars.get('age_sec'))} status={ars.get('last_status')} "
                     f"auto_upload={counts.get('auto_upload')} block={counts.get('block')} "
                     f"gaps={gaps}")
    if api:
        active_media = api.get("active_media") or {}
        notes.append(f"recorder: live_status={live_status} run={api.get('running_status')} "
                     f"fmt={api.get('real_stream_format')} requested_qn={api.get('requested_quality_number')} "
                     f"actual={active_media.get('width')}x{active_media.get('height')} "
                     f"rec_rate={rec_rate} rec_total={rec_total}")
        width = int(active_media.get("width") or 0)
        height = int(active_media.get("height") or 0)
        if live and width and height and not (
            max(width, height) >= 1920 and min(width, height) >= 1080
        ):
            problems.append({
                "id": "recorder_quality_below_1080",
                "sev": "WARN",
                "msg": f"录播姬当前实际视频为 {width}x{height}，低于 1080p；配置优先 10000 不能证明实际拿到 1080p。",
                "fix": "检查 B 站账号/Cookie 当时可用画质和录播姬 CurrentQn；保留录制但按质量降级告警。"
            })
        if api.get("cookie_login_valid") is False:
            problems.append({
                "id": "recorder_cookie_not_logged_in",
                "sev": "WARN",
                "msg": "录播姬的 B 站 Cookie 未登录，10000 仍会优先请求但实际可能只能取得较低画质。",
                "fix": f"更新 {SSH_HOST} 上权限 0600 的录播姬 Cookie；不要把 Cookie 写进仓库或日志。"
            })
        elif api.get("cookie_health_error"):
            problems.append({
                "id": "recorder_cookie_health_unknown",
                "sev": "WARN",
                "msg": "暂时无法验证录播姬 Cookie 登录状态。",
                "fix": "检查 adapter 状态中的 cookie health 网络错误；保持实际分辨率验收。"
            })

    # ---- CRITICAL: live but capturing nothing (the 90-min gap that slipped today) ----
    # Decide via byte growth between two checks (~5 min apart);
    # instantaneous rec_rate is often 0 at the sampling instant.
    rec_seen = state.get("rec_total_seen", {})
    key = str(PRIMARY_ROOM)
    if live and rec_total is not None and key in rec_seen:
        # exactly equal => no new bytes since last check (stuck). A DROP means a new
        # segment/recording started (rec_total resets), which is NOT stuck.
        if rec_total == rec_seen[key]:
            detail = (
                f"李豆沙在播但录制无增长(上轮 {rec_seen[key]} → 本轮 {rec_total} 字节)"
            )
            if ALLOW_RECORDER_RESTART:
                ok = restart_recorder(PRIMARY_ROOM)
                actions.append((
                    "restarted_recorder",
                    detail + f"，已{'成功' if ok else '尝试'}重启官方录播姬。",
                ))
                message = "李豆沙在播但录播姬两轮无字节增长——已按显式配置尝试重启录制器。"
            else:
                message = (
                    "李豆沙在播但录播姬两轮无字节增长；监控默认不重启生产录播姬。"
                )
            problems.append({"id": "live_not_recording", "sev": "DOWN",
                             "msg": message,
                             "fix": f"检查 {SSH_HOST} 录播姬日志中的 CDN/画质回退；"
                                    "确认当前主机、录制状态和维护窗口后再人工恢复，保留原始 FLV。"})
    if rec_total is not None:
        rec_seen[key] = rec_total
    state["rec_total_seen"] = rec_seen

    # ---- publish guard (irreversible; explicitly forbidden) ----
    if procs.get("upload", 0) > 0:
        if ALLOW_UPLOAD_KILL:
            ok = kill_upload()
            actions.append(("killed_upload",
                            f"检测到 src.upload.upload 在运行(会自动投稿)，已{'成功' if ok else '尝试'}终止。"))
            message = "发布进程 upload 在运行——已执行配置允许的安全停机。"
        else:
            message = "发布进程 upload 在运行；安全停机被配置禁用，监控未作远端修改。"
        problems.append({"id": "upload_running", "sev": "DEGRADED",
                         "msg": message,
                         "fix": f"检查 {SSH_HOST} 上谁启动了 upload；没有当前发布授权时立即停止。"})

    # ---- recorder config regression ----
    # The dangerous setting is source deletion: a bad remux must never remove
    # the only original recording.
    if flags.get("delete_source") == "auto":
        problems.append({"id": "delete_source_auto", "sev": "DEGRADED",
                         "msg": "delete_source=auto 危险：remux 坏了会把原始源删掉(6/20 无声那次的元凶之一)。",
                         "fix": "改回 delete_source=never，保留原始 flv。"})

    # ---- container reachable but recorders gone ----
    if HOST_RECORDER_ACTIVE and procs.get("recorder", 0) == 0:
        problems.append({"id": "recorder_down", "sev": "DOWN",
                         "msg": "官方录播姬状态缺失、过期或不可达。",
                         "fix": f"检查 {SSH_HOST} 上 bililive_recorder 容器、"
                                "/opt/bilive/recording/status.json 与 bililive_adapter 服务。"})








    for label, key in (("scan", "scan"), ("local_prepare", "local_prepare"),
                       ("auto-review-shadow-daemon", "auto_review_shadow")):
        if procs.get(key, 0) > 0:
            problems.append({"id": f"old_plane_{key}_running", "sev": "WARN",
                             "msg": f"已退役的旧控制面进程 {label} 在容器里运行（疑被手动/旧脚本拉起）。",
                             "fix": "旧管线 2026-07-10 已退役。若无人在调试，"
                                    "进容器 pkill 对应模块；勿恢复 compose 旧 command。"})

    # ---- NEW plane health: autoslice runner heartbeat + alerts (configured host) ----
    hb = probe.get("autoslice") or {}
    if hb.get("fatal"):
        problems.append({"id": "autoslice_probe_failed", "sev": "DEGRADED",
                         "msg": f"autoslice 健康探测失败: {hb['fatal']}",
                         "fix": f"检查 ssh {SSH_HOST} 与 /opt/bilive/autoslice/reports/。"})
    if hb.get("heartbeat"):
        notes.append(f"autoslice 心跳: {hb['heartbeat'][:140]}")
    runner_off = hb.get("runner_disabled") is True
    if runner_off:
        notes.append("autoslice runner 已由 DISABLED 开关暂停（该状态下 runner 不写心跳、不推进日期；开关由行政管理）")
    if not hb.get("fatal") and not runner_off:
        if hb.get("age_sec") is None:
            problems.append({"id": "autoslice_heartbeat_missing", "sev": "DEGRADED",
                             "msg": "读不到 autoslice runner 心跳（heartbeat.txt 缺失/无时间戳）。",
                             "fix": f"查 {SSH_HOST} crontab 的 */10 tick 与 /opt/bilive/autoslice/logs/runner.log。"})
        elif hb["age_sec"] > 30 * 60:
            problems.append({"id": "autoslice_heartbeat_stale", "sev": "DEGRADED",
                             "msg": f"autoslice 心跳已 {fmt_age(int(hb['age_sec']))} 未更新（cron 每 10 分钟应一跳）。",
                             "fix": f"查 {SSH_HOST} crontab 与 runner.log；确认 DISABLED 杀开关状态符合当前运行计划。"})
    if not hb.get("fatal"):
        notes.append(crawler_note(hb.get("crawlers")))
    problems.extend(crawler_problems(hb.get("crawlers")))
    if "SOURCE_UNAVAILABLE" in (hb.get("heartbeat") or "") and not runner_off:
        problems.append({"id": "autoslice_source_unavailable", "sev": "DOWN",
                         "msg": "autoslice 心跳报 SOURCE_UNAVAILABLE——录播挂载不可读（录制写入路径可能同断）。",
                         "fix": "mount 看门狗（*/5）应自愈；看 ALERT_MOUNT_WATCHDOG/ALERT_SOURCE_UNAVAILABLE，"
                                "未自愈需人工修复挂载并重启 bilive_record。"})
    for alert in hb.get("alerts", []):
        problems.append({"id": f"autoslice_alert_{alert['name']}", "sev": "WARN",
                         "msg": f"autoslice 近 6h 告警 {alert['name']}: {alert['last'][:160]}",
                         "fix": "看 <宿主>:/opt/bilive/autoslice/reports/ 对应 ALERT 文件全文。"})

    # ---- finished stream but the runner hasn't concluded that date ----
    if (not live) and rec.get("mtime") and not runner_off:
        ended_ago = probe["now"] - rec["mtime"]
        date_states = dict(re.findall(r"(\d{4}-\d{2}-\d{2}):(\S+)", hb.get("heartbeat") or ""))
        latest_state = date_states.get(str(primary.get("latest_date_dir") or ""))
        if ended_ago > SLICE_GRACE_SEC and latest_state in (None, "new", "sealing", "processing"):
            problems.append({"id": "autoslice_not_concluded", "sev": "DEGRADED",
                             "msg": f"最近一场已结束 {fmt_age(int(ended_ago))}，autoslice 该日期状态仍为 "
                                    f"{latest_state or '未知'}（应到 review_ready/no_delivery/paused_cpa_down）。",
                             "fix": f"看 {SSH_HOST} runner.log 与 state/<date>.json；CPA 断供会显示 paused_cpa_down（属正常等待）。"})

    # ---- disk ----
    free = probe["disk"].get("free")
    if free is not None and free < DISK_MIN_BYTES:
        problems.append({"id": "disk_low", "sev": "WARN",
                         "msg": f"Videos 盘剩余 {free/1024**3:.1f} GiB 偏低。",
                         "fix": "清理已处理的旧录播/切片。"})

    # ---- audio health on the newest finished recording (the 6/20 silent-audio guard) ----
    au = probe.get("audio") or {}
    if au.get("file"):
        prof = au.get("profile")
        mv = au.get("mean_volume")
        notes.append(f"音频检查 {au['file']}: codec={au.get('codec_name')} profile={prof} "
                     f"ch={au.get('channels')} mean_vol={mv}")
        if au.get("has_audio") is False:
            problems.append({"id": "audio_missing", "sev": "DEGRADED",
                             "msg": f"最新录播 {au['file']} 没有音轨。",
                             "fix": "检查录制源/格式；必要时用官方录播音频替换。"})
        elif au.get("has_audio") is None:
            # probe couldn't read the file (e.g. still-remuxing / raw fragment) —
            # UNKNOWN, not a failure.  Note it, don't raise a false DEGRADED.
            notes.append(f"音频未能校验(探测失败，多为文件尚在合成或裸片段): {au.get('ffprobe_error','')[:60]}")
        elif prof in ("-1", "unknown"):
            problems.append({"id": "audio_bad_profile", "sev": "DEGRADED",
                             "msg": f"最新录播 {au['file']} 音频 profile={prof}(6/20 无声同款异常)。",
                             "fix": "这条流的音频封装坏了；切回另一种 stream_format 或用官方录播音频替换。"})
        elif mv is not None and mv < -50:
            problems.append({"id": "audio_silent", "sev": "DEGRADED",
                             "msg": f"最新录播 {au['file']} 疑似静音(mean_volume={mv}dB)。",
                             "fix": "实际抽听确认；多为录制源问题，考虑换源。"})
        if au["file"] != state.get("last_audio_file") and "mean_volume" in au:
            state["last_audio_file"] = au["file"]

    # ---- overall verdict ----
    sevs = [p["sev"] for p in problems]
    if "DOWN" in sevs:
        verdict = "DOWN"
    elif "DEGRADED" in sevs:
        verdict = "DEGRADED"
    elif "WARN" in sevs:
        verdict = "WARN"
    else:
        verdict = "OK"
    return verdict, problems, actions, notes


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    atomic_write_json(STATE_FILE, state)


def write_reports(probe, verdict, problems, actions, notes, ts):
    return _write_reports(REPORT_DIR, probe, verdict, problems, actions, notes, ts)


SMTP_CONFIG = os.path.expanduser("~/.config/slice_monitor/smtp.json")


def _send_email_smtp(subject, body):
    """Reliable headless path. Config json: {host,port,user,password,from,to?}.
    For iCloud: host=smtp.mail.me.com port=587, user=<appleid>, password=<app-specific pwd>."""
    if not os.path.exists(SMTP_CONFIG):
        return None  # not configured -> caller falls back
    import smtplib
    from email.message import EmailMessage
    with open(SMTP_CONFIG) as f:
        cfg = json.load(f)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("from", cfg["user"])
    msg["To"] = cfg.get("to", EMAIL_TO)
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["host"], int(cfg.get("port", 587)), timeout=30) as s:
            s.starttls()
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
        return True, "smtp ok"
    except Exception as e:
        return False, f"smtp error: {e}"


def _send_email_applemail(subject, body):
    """Fallback via Apple Mail. Background GUI automation is finicky; warm Mail up first."""
    try:
        subprocess.run(["open", "-g", "-a", "Mail"], capture_output=True, timeout=15)
    except Exception:
        pass
    osa = '''on run argv
  set theSubject to item 1 of argv
  set theBody to item 2 of argv
  set theAddr to item 3 of argv
  tell application "Mail"
    set m to make new outgoing message with properties {subject:theSubject, content:theBody, visible:false}
    tell m to make new to recipient at end of to recipients with properties {address:theAddr}
    send m
  end tell
end run'''
    try:
        p = subprocess.run(["osascript", "-", subject, body, EMAIL_TO],
                           input=osa, capture_output=True, text=True, timeout=25)
        return p.returncode == 0, ("applemail ok" if p.returncode == 0
                                   else (p.stderr.strip() or "applemail failed"))
    except Exception as e:
        return False, f"applemail error (unreliable from launchd; configure SMTP): {e}"


def send_email(subject, body):
    r = _send_email_smtp(subject, body)
    if r is not None:
        if r[0]:
            return r
        # smtp configured but failed -> still try Apple Mail as backup
        ok, info = _send_email_applemail(subject, body)
        return ok, f"{r[1]} | fallback {info}"
    return _send_email_applemail(subject, body)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ts = datetime.now(CET8)
    # manual test hook: `touch reports/slice_monitor/SEND_TEST_EMAIL` then wait for next run,
    # to verify the email channel works from the LaunchAgent (launchd) context.
    test_flag = os.path.join(REPORT_DIR, "SEND_TEST_EMAIL")
    if EMAIL_ENABLED and os.path.exists(test_flag):
        ok, info = send_email("[TEST] 李豆沙切片监控 (launchd)",
                              f"launchd 上下文邮件测试 @ {ts:%Y-%m-%d %H:%M:%S}\n收到即说明定时任务能发邮件。")
        print(f"[test-email] ok={ok} info={info}")
        try:
            os.remove(test_flag)
        except OSError:
            pass

    state = load_state()
    probe = (
        run_production_probe()
        if MONITOR_PROFILE == "production"
        else run_probe(state.get("last_audio_file", ""))
    )
    probe["recorder_probes"] = {
        name: run_container_files_probe(spec, PRIMARY_ROOM)
        for name, spec in RECORDERS.items()
        if spec["state"] == "active" and spec["probe"] == "container_files"
    }
    probe["jingting"] = run_jingting_probe()
    probe["autoslice"] = run_autoslice_probe()
    verdict, problems, actions, notes = evaluate(probe, state)
    md = write_reports(probe, verdict, problems, actions, notes, ts)

    prob_ids = sorted(p["id"] for p in problems)
    prev_ids = state.get("problem_ids", [])
    prev_verdict = state.get("verdict", "OK")

    # email policy: on NEW problem ids, on escalation, on recovery, or when auto-actions happened
    new_problem = bool(set(prob_ids) - set(prev_ids))
    recovered = prev_verdict in ("DEGRADED", "DOWN") and verdict in ("OK", "WARN")
    notify = new_problem or recovered or bool(actions) or (verdict == "DOWN")

    if EMAIL_ENABLED and notify and (problems or actions or recovered):
        subj = f"[李豆沙切片监控] {verdict}"
        if recovered and not problems:
            subj = "[李豆沙切片监控] 恢复正常 ✅"
        ok, info = send_email(subj, md)
        state["last_email_ok"] = ok
        state["last_email_info"] = info[:200]

    state.update({"verdict": verdict, "problem_ids": prob_ids,
                  "ts": ts.isoformat()})
    save_state(state)
    prune_history(REPORT_DIR, ts)

    print(f"[{ts:%Y-%m-%d %H:%M:%S}] {verdict}  problems={prob_ids}  "
          f"actions={[a[0] for a in actions]}")
    print(md)





    if "--health-exit" in sys.argv:
        sys.exit({"OK": 0, "WARN": 0, "DEGRADED": 1, "DOWN": 2}[verdict])
    sys.exit(0)


if __name__ == "__main__":
    main()
