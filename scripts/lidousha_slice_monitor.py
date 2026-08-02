#!/usr/bin/env python3
"""
李豆沙 自动切片监控 (lidousha auto-slice monitor)

Runs locally on Ivan's Mac (cron), SSHes into the `free` host, and checks the
health of the bilive auto-slice pipeline for room 22966160 (李豆沙). A former
secondary test-room probe was removed with the recorder migration.

What it watches
  - bilive_record container reachable
  - official BililiveRecorder adapter status is fresh
  - whether 22966160 is currently live/recording
  - whether the auto-slice loop (`src.burn.scan`) is running
  - whether the publish loop (`src.upload.upload`) is running  <-- must be OFF
  - whether finished recordings are actually producing slices + covers
  - scan log error bursts
  - disk headroom + recorder quality/IPv4/source-retention policy

Safe auto-rescue ("能自动救的就救")
  - kill any running `src.upload.upload`  (publishing is explicitly forbidden)
  - restart a CRASHED scan loop that the monitor had previously blessed
  - cold-start scan when 22966160 is live AND no dirty backlog would be reswept
Everything else -> alert with a diagnosis + suggested manual fix, no auto-change.

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

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
SSH_HOST = os.environ.get("AUTOSLICE_MONITOR_SSH_HOST", "free")
CONTAINER = os.environ.get("AUTOSLICE_MONITOR_CONTAINER", "bilive_record")
PRIMARY_ROOM = os.environ.get("AUTOSLICE_MONITOR_ROOM", "22966160")  # 参考部署房间号
ROOMS = [PRIMARY_ROOM]

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                          "reports", "slice_monitor")
REPORT_DIR = os.path.abspath(REPORT_DIR)
STATE_FILE = os.path.join(REPORT_DIR, "state.json")
LATEST_JSON = os.path.join(REPORT_DIR, "latest.json")
LATEST_MD = os.path.join(REPORT_DIR, "latest.md")
HISTORY_DIR = os.path.join(REPORT_DIR, "history")

EMAIL_TO = "hfnkzjbsbm@privaterelay.appleid.com"
EMAIL_ACCOUNT = "iCloud"
# Ivan dropped the email channel (2026-06-22): report file is the only alert channel.
# Flip back to True (and configure ~/.config/lidousha_monitor/smtp.json) to re-enable email.
EMAIL_ENABLED = False

# thresholds
RECORDING_ACTIVE_AGE = 300      # file touched within 5 min => live/recording
SLICE_GRACE_SEC = 90 * 60       # after a stream ends, allow 90 min before "no slices" is a problem
DISK_MIN_BYTES = 15 * 1024**3   # warn under 15 GiB free

CET8 = timezone(timedelta(hours=8))

# ----------------------------------------------------------------------------
# Remote probe (runs INSIDE the container via `docker exec ... python3 -`)
# ----------------------------------------------------------------------------
PROBE_PY = r'''
import os, re, json, time, glob, sqlite3, shutil, subprocess
import urllib.request

ROOMS = ["__ROOMS__"]
VIDEOS = "/app/Videos"
NOW = time.time()

# BililiveRecorder's host-side adapter atomically publishes this normalized
# document and compose mounts it read-only into the tooling container.
RECORDER_STATUS = "/app/recording-state/status.json"
RECORDER_STATUS_MAX_AGE = 180

def recorder_status(room):
    if room != ROOMS[0]:
        return None
    try:
        with open(RECORDER_STATUS, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("schema_version") != "recorder-neutral-status.v1":
            raise ValueError("schema mismatch")
        if str(d.get("room_id")) != room:
            raise ValueError("room mismatch")
        age = NOW - float(d["generated_at_epoch"])
        if age < -300 or age > RECORDER_STATUS_MAX_AGE:
            raise ValueError(f"stale status ({age:.0f}s)")
        if d.get("service_reachable") is not True or d.get("error"):
            raise ValueError(str(d.get("error") or "recorder unreachable"))
        return {
            "backend": d.get("backend"),
            "live_status": d.get("live_status"),
            "running_status": d.get("running_status"),
            "streaming": d.get("streaming"),
            "recording": d.get("recording"),
            "danmaku_connected": d.get("danmaku_connected"),
            "rec_total": d.get("rec_total"),
            "total_input_bytes": d.get("total_input_bytes"),
            "total_output_bytes": d.get("total_output_bytes"),
            "rec_rate": d.get("rec_rate"),
            "real_stream_format": d.get("real_stream_format"),
            "requested_quality_priority": d.get("requested_quality_priority"),
            "requested_quality_number": d.get("requested_quality_number"),
            "actual_quality_number": d.get("actual_quality_number"),
            "active_media": (d.get("latest_source") or {}).get("media"),
            "latest_source_size": (d.get("latest_source") or {}).get("size_bytes"),
            "cookie_configured": (d.get("bilibili_cookie") or {}).get("configured"),
            "cookie_login_valid": (d.get("bilibili_cookie") or {}).get("login_valid"),
            "cookie_health_error": (d.get("bilibili_cookie") or {}).get("error"),
            "recording_path": d.get("recording_path"),
            "finalizing": d.get("finalizing"),
            "status_age_sec": int(age),
        }
    except Exception as e:
        return {"error": str(e)[:120]}

def ps_args():
    try:
        out = subprocess.check_output(["ps", "-eo", "args"], text=True, errors="replace")
        return out.splitlines()
    except Exception as e:
        return []

LINES = ps_args()
def count(pat):
    rx = re.compile(pat)
    return sum(1 for l in LINES if rx.search(l) and "grep" not in l)

def count_live(pat):
    rx = re.compile(pat)
    return sum(1 for l in LINES if rx.search(l) and "grep" not in l and "<defunct>" not in l)

primary_recorder_status = recorder_status(ROOMS[0])
procs = {
    "recorder": int(bool(primary_recorder_status and not primary_recorder_status.get("error"))),
    "scan": count_live(r"src\.burn\.scan"),
    "local_prepare": count_live(r"src\.upload\.local_prepare"),  # no-publish title/cover staging
    "upload": count_live(r"src\.upload\.upload"),                # OLD publishing daemon (must be OFF)
    "auto_review_shadow": count_live(r"lidousha_auto_review_shadow_daemon"),  # no-upload review gate
}

ORIG_RX = lambda room: re.compile(r"^%s_\d{8}-\d\d-\d\d-\d\d\.(mp4|flv|m4s)$" % room)
# The FINISHED, remuxed recording (dashed date + trailing dash) — ffprobe reads
# its moov in a few seconds. Historical raw `.m4s` fragments can be 1GB+ fMP4
# and ffprobe may time out over the slow mount → false audio_missing. Audio
# health must be checked on a finalized readable file, not an active fragment.
FINISHED_MP4_RX = lambda room: re.compile(r"^%s_\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d-\.mp4$" % room)
SLICE_RX = lambda room: re.compile(r"\d+s_.*%s.*\.(flv|mp4)$" % room)

def date_dirs(room):
    base = os.path.join(VIDEOS, room)
    if not os.path.isdir(base):
        return []
    ds = [d for d in os.listdir(base) if re.match(r"\d{4}-\d{2}-\d{2}$", d)]
    return sorted(ds)

rooms = {}
for room in ROOMS:
    base = os.path.join(VIDEOS, room)
    dds = date_dirs(room)
    latest_dir = dds[-1] if dds else None
    info = {"exists": os.path.isdir(base), "latest_date_dir": latest_dir,
            "all_date_dirs": dds}
    # recording freshness (newest original anywhere in latest dir)
    rec_file, rec_mtime = None, 0
    if latest_dir:
        for f in glob.glob(os.path.join(base, latest_dir, "*")):
            bn = os.path.basename(f)
            if ORIG_RX(room).match(bn) or bn.endswith(".m4s") or bn.endswith(".part"):
                try:
                    m = os.path.getmtime(f)
                except OSError:
                    continue
                if m > rec_mtime:
                    rec_mtime, rec_file = m, bn
    info["recording"] = {
        "latest_file": rec_file,
        "mtime": rec_mtime,
        "age_sec": int(NOW - rec_mtime) if rec_mtime else None,
        "active": bool(rec_mtime and (NOW - rec_mtime) < 300),
    }
    # slice output in latest dir
    sl_file, sl_mtime, sl_count, cover_count, pub_count = None, 0, 0, 0, 0
    if latest_dir:
        for f in glob.glob(os.path.join(base, latest_dir, "*")):
            bn = os.path.basename(f)
            if SLICE_RX(room).search(bn):
                sl_count += 1
                try:
                    m = os.path.getmtime(f)
                except OSError:
                    m = 0
                if m > sl_mtime:
                    sl_mtime, sl_file = m, bn
            if bn.endswith(".cover.png") or bn.endswith(".cover.jpg"):
                cover_count += 1
            if bn.endswith(".publish.json"):
                pub_count += 1
    info["slices"] = {
        "latest_file": sl_file,
        "mtime": sl_mtime,
        "age_sec": int(NOW - sl_mtime) if sl_mtime else None,
        "count_latest_dir": sl_count,
        "cover_count_latest_dir": cover_count,
        "publish_json_latest_dir": pub_count,
    }
    # dirty backlog: date dirs (other than the live one) that still hold top-level originals
    dirty = []
    for d in dds:
        if latest_dir and d == latest_dir and info["recording"]["active"]:
            continue  # the live dir is allowed to have in-progress originals
        cnt = 0
        for f in glob.glob(os.path.join(base, d, "*")):
            if ORIG_RX(room).match(os.path.basename(f)):
                cnt += 1
        if cnt:
            dirty.append({"dir": d, "originals": cnt})
    info["dirty_backlog"] = dirty
    info["recorder_status"] = (
        primary_recorder_status if room == ROOMS[0] else recorder_status(room)
    )
    rooms[room] = info

# ---- audio health on the newest FINISHED recording of the primary room ----
# Catches the 6/20 failure mode (AAC profile -1 / silent) at the source. ffprobe (cheap,
# no decode) runs every tick; the volumedetect decode runs only when the file is new.
AUDIO_LAST = "__LAST_AUDIO__"
audio = {}
proom = ROOMS[0]
pinfo = rooms.get(proom, {})
active_bn = os.path.basename(((pinfo.get("recorder_status") or {}).get("recording_path") or ""))
ld = pinfo.get("latest_date_dir")
# Prefer the finished remuxed .mp4 (ffprobe-readable); only fall back to a raw
# ORIG_RX match (.m4s) if no remuxed file exists yet.
cand, cand_m, cand_finished = None, 0, False
if ld:
    for f in glob.glob(os.path.join(VIDEOS, proom, ld, "*")):
        bn = os.path.basename(f)
        finished = bool(FINISHED_MP4_RX(proom).match(bn))
        if (finished or ORIG_RX(proom).match(bn)) and bn != active_bn:
            try:
                m = os.path.getmtime(f)
            except OSError:
                continue
            # a finished .mp4 always wins over a raw fragment; else newest wins
            if (finished and not cand_finished) or (finished == cand_finished and m > cand_m):
                cand_m, cand, cand_finished = m, f, finished
if cand:
    audio = {"file": os.path.basename(cand)}
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,profile,sample_rate,channels", "-of",
             "default=noprint_wrappers=1", cand],
            text=True, stderr=subprocess.STDOUT, timeout=30)
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                audio[k] = v
    except Exception as e:
        audio["ffprobe_error"] = str(e)[:80]
    # A probe timeout/error is UNKNOWN, not "audio missing" — the raw .m4s times
    # out even when the audio is fine.  Only an explicit no-audio-stream result
    # (ffprobe succeeded, no codec_name) counts as missing.
    audio["has_audio"] = None if "ffprobe_error" in audio else ("codec_name" in audio)
    if audio["file"] != AUDIO_LAST:  # expensive silence check only on a new file
        try:
            vd = subprocess.run(
                ["ffmpeg", "-y", "-t", "12", "-i", cand, "-af", "volumedetect", "-f", "null", "-"],
                capture_output=True, text=True, timeout=45).stderr
            mm = re.search(r"mean_volume:\s*(-?\d+\.?\d*) dB", vd)
            audio["mean_volume"] = float(mm.group(1)) if mm else None
        except Exception as e:
            audio["mean_volume_error"] = str(e)[:80]

# scan log (newest)
scan_log = {"file": None, "mtime": None, "recent_errors": [], "error_count": 0}
logs = sorted(glob.glob("/app/logs/scan/scan-*.log"), key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
if logs:
    f = logs[-1]
    scan_log["file"] = os.path.basename(f)
    scan_log["mtime"] = os.path.getmtime(f)
    try:
        with open(f, "r", errors="replace") as fh:
            tail = fh.readlines()[-400:]
        errs = [l.strip() for l in tail if "[ERROR]" in l or " ERROR " in l]
        scan_log["error_count"] = len(errs)
        scan_log["recent_errors"] = errs[-6:]
    except Exception:
        pass
# also newest runtime scan log mtime (process heartbeat)
rt = sorted(glob.glob("/app/logs/runtime/scan-*.log"), key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
scan_log["runtime_log_mtime"] = os.path.getmtime(rt[-1]) if rt else None

# upload queue
uq = {"count": None, "locked": None}
try:
    db = sqlite3.connect("/app/src/db/data.db")
    cur = db.cursor()
    cur.execute("select count(*), sum(case when locked!=0 then 1 else 0 end) from upload_queue")
    row = cur.fetchone()
    uq = {"count": row[0], "locked": row[1] or 0}
    db.close()
except Exception as e:
    uq["error"] = str(e)

# disk
try:
    du = shutil.disk_usage(VIDEOS)
    disk = {"free": du.free, "total": du.total}
except Exception as e:
    disk = {"error": str(e)}

# Recorder-policy projection from the normalized adapter status.
flags = {
    "stream_format": (primary_recorder_status or {}).get("real_stream_format"),
    "delete_source": "never",
    "quality_priority": (primary_recorder_status or {}).get("requested_quality_priority"),
    "address_family": "ipv4",
}

# no-upload auto-review shadow state
auto_review_shadow = {"state_file": "/app/reports/auto_review_shadow/lidousha_auto_review_shadow_state.json"}
try:
    st = os.stat(auto_review_shadow["state_file"])
    auto_review_shadow["mtime"] = st.st_mtime
    auto_review_shadow["age_sec"] = int(NOW - st.st_mtime)
    with open(auto_review_shadow["state_file"], "r", encoding="utf-8") as fh:
        ars = json.load(fh)
    last = ars.get("last_result") or {}
    auto_review_shadow["last_status"] = last.get("status")
    auto_review_shadow["last_output_dir"] = last.get("output_dir")
    auto_review_shadow["last_summary_counts"] = last.get("summary_counts")
    auto_review_shadow["last_gap_summary"] = last.get("gap_summary")
    auto_review_shadow["last_validations"] = last.get("validations")
except FileNotFoundError:
    auto_review_shadow["missing"] = True
except Exception as e:
    auto_review_shadow["error"] = str(e)[:160]

print(json.dumps({
    "ok": True, "now": NOW, "procs": procs, "rooms": rooms,
    "scan_log": scan_log, "upload_queue": uq, "disk": disk, "flags": flags,
    "audio": audio,
    "auto_review_shadow": auto_review_shadow,
}))
'''.replace("__ROOMS__", '", "'.join(ROOMS))


# ----------------------------------------------------------------------------
# Remote helpers
# ----------------------------------------------------------------------------
def run_probe(last_audio_file=""):
    probe_src = PROBE_PY.replace("__LAST_AUDIO__", last_audio_file or "")
    try:
        p = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", SSH_HOST,
             "docker", "exec", "-i", CONTAINER, "python3", "-"],
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
    """Probe the host-side Antigravity fine-transcription daemon.

    This runs on the free host, not inside the bilive container, because agy is
    authenticated under root on the host.
    """
    remote_py = r'''
import json, os, re, subprocess, time

ROOT = "/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming"
ROOM = "22966160"
LOG = "/opt/bilive/logs/jingting-22966160.log"
SLICE_RX = re.compile(r"\d+s_.*_%s_.*\.(flv|mp4)$" % ROOM)
SKIP = {
    ".jingting_jobs", "sources", "burned_final", "final_release",
    "replacement_recuts", "bad_dash_merge_20260619-064753",
    "bad_flv_and_corrupt_source_20260618-230407",
}

def find_srt(path):
    stem = path.rsplit(".", 1)[0]
    base = os.path.basename(stem)
    for cand in (
        stem + ".srt",
        os.path.join(os.path.dirname(path), "subtitles", base + ".srt"),
        os.path.join(os.path.dirname(path), "subtitles", base + ".coarse.srt"),
    ):
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            return cand
    return None


def ps_count(needle):
    try:
        out = subprocess.check_output(["ps", "-eo", "args"], text=True, errors="replace")
    except Exception:
        return 0
    return sum(1 for line in out.splitlines() if needle in line and "grep" not in line)

room_root = os.path.join(ROOT, ROOM)
dates = sorted(
    d for d in os.listdir(room_root)
    if re.match(r"\d{4}-\d{2}-\d{2}$", d) and os.path.isdir(os.path.join(room_root, d))
) if os.path.isdir(room_root) else []
latest = dates[-1] if dates else None
pending = []
done = []
review_required = []
if latest:
    base = os.path.join(room_root, latest)
    for cur, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
        if any(part in SKIP for part in cur.split(os.sep)):
            continue
        for name in files:
            path = os.path.join(cur, name)
            if name.endswith(".jingting.done"):
                done.append(path)
            if name.endswith(".jingting.review-required.json"):
                review_required.append(path)
            if SLICE_RX.search(name) and find_srt(path):
                marker = path.rsplit(".", 1)[0] + ".jingting.done"
                if not os.path.exists(marker):
                    pending.append(path)

log_mtime = os.path.getmtime(LOG) if os.path.exists(LOG) else None
print(json.dumps({
    "ok": True,
    "daemon_count": ps_count("gemini_slice_jingting.py --provider agy --daemon"),
    "agy_count": ps_count("/root/.local/bin/agy"),
    "latest_date": latest,
    "pending_latest": len(pending),
    "done_latest": len(done),
    "review_required_latest": len(review_required),
    "latest_review_required": os.path.basename(review_required[0]) if review_required else "",
    "latest_pending": os.path.basename(pending[0]) if pending else "",
    "log": LOG if os.path.exists(LOG) else "",
    "log_age_sec": int(time.time() - log_mtime) if log_mtime else None,
}))
'''
    try:
        p = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", SSH_HOST,
             "python3", "-"],
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
    """Health of the NEW control plane: the autoslice runner on the free host
    (heartbeat freshness, SOURCE_UNAVAILABLE, recent ALERT_* files)."""
    cmd = (
        "cat /opt/bilive/autoslice/reports/heartbeat.txt 2>/dev/null; echo __SEP__; "
        "for f in /opt/bilive/autoslice/reports/ALERT_*.txt; do "
        "[ -f \"$f\" ] && echo \"$f|$(stat -c %Y \"$f\")|$(tail -1 \"$f\")\"; done 2>/dev/null; true"
    )
    try:
        p = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", SSH_HOST, cmd],
                           capture_output=True, text=True, timeout=40)
    except Exception as e:
        return {"fatal": f"ssh error: {e}"}
    if p.returncode != 0:
        return {"fatal": f"rc={p.returncode}: {p.stderr.strip()[:200]}"}
    head, _, alerts_raw = p.stdout.partition("__SEP__")
    heartbeat = head.strip() or None
    out = {"heartbeat": heartbeat, "age_sec": None, "alerts": []}
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
    base = ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes", SSH_HOST, remote]
    try:
        p = subprocess.run(base, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def kill_upload():
    rc, out, err = ssh_exec("pkill -9 -f 'src.upload.upload'; sleep 1; echo done")
    return rc == 0


def restart_recorder(room):
    """Restart official BililiveRecorder after two live no-growth observations."""
    if room != PRIMARY_ROOM:
        return False
    try:
        p = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=15",
                "-o",
                "BatchMode=yes",
                SSH_HOST,
                "docker",
                "restart",
                "bililive_recorder",
            ],
            capture_output=True, text=True, timeout=40)
        return p.returncode == 0
    except Exception:
        return False


def fmt_age(sec):
    if sec is None:
        return "n/a"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h{(sec % 3600) // 60}m"


def evaluate(probe, state):
    """Return (verdict, problems, actions, notes). verdict in OK/WARN/DEGRADED/DOWN."""
    problems = []   # each: {id, sev, msg, fix}
    actions = []    # auto actions taken
    notes = []

    if not probe.get("ok"):
        problems.append({"id": "probe", "sev": "DOWN",
                         "msg": f"无法探测 free/容器: {probe.get('fatal')}",
                         "fix": "检查 `ssh free` 可达性与 `docker ps` 里 bilive_record 是否在运行。"})
        return "DOWN", problems, actions, notes

    procs = probe["procs"]
    primary = probe["rooms"].get(PRIMARY_ROOM, {})
    rec = primary.get("recording", {})
    sl = primary.get("slices", {})
    dirty = primary.get("dirty_backlog", [])
    flags = probe.get("flags", {})
    api = primary.get("recorder_status") or {}
    # Authoritative live state from the normalized recorder status; fall back
    # to file freshness only when the status document is unavailable.
    live_status = api.get("live_status")
    if live_status is not None:
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

    notes.append(f"recorder={procs['recorder']} scan={procs['scan']} "
                 f"local_prepare={procs.get('local_prepare', 0)} upload={procs['upload']} "
                 f"auto_review_shadow={procs.get('auto_review_shadow', 0)}")
    notes.append(f"李豆沙 live={'YES' if live else 'no'} "
                 f"录制最新={primary.get('latest_date_dir')} "
                 f"(age {fmt_age(rec.get('age_sec'))})")
    notes.append(f"切片最新 age={fmt_age(sl.get('age_sec'))} "
                 f"count={sl.get('count_latest_dir')} covers={sl.get('cover_count_latest_dir')} "
                 f"publish.json={sl.get('publish_json_latest_dir')}")
    jt = probe.get("jingting") or {}
    if jt.get("ok"):
        notes.append(f"jingting agy daemon={jt.get('daemon_count')} agy={jt.get('agy_count')} "
                     f"latest={jt.get('latest_date')} pending={jt.get('pending_latest')} "
                     f"done={jt.get('done_latest')} review_required={jt.get('review_required_latest', 0)} "
                     f"log_age={fmt_age(jt.get('log_age_sec'))}")
        if jt.get("pending_latest", 0) > 0 and jt.get("daemon_count", 0) == 0:
            # .jingting pendings are OLD-plane hybrid slices; with that plane
            # retired (2026-07-10) the stale backlog must not WARN forever.
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
                "fix": "更新 free 上权限 0600 的录播姬 Cookie；不要把 Cookie 写进仓库或日志。"
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
            ok = restart_recorder(PRIMARY_ROOM)
            actions.append(("restarted_recorder",
                            f"李豆沙在播但录制无增长(上轮 {rec_seen[key]} → 本轮 {rec_total} 字节)，"
                            f"已{'成功' if ok else '尝试'}重启官方录播姬。"))
            problems.append({"id": "live_not_recording", "sev": "DOWN",
                             "msg": "李豆沙在播但录播姬两轮无字节增长——已自动重启录制器。",
                             "fix": "检查录播姬日志中的 CDN/画质回退；生产配置已强制 IPv4，"
                                    "若仍反复发生则保留原始 FLV 并人工检查源端可用性。"})
    if rec_total is not None:
        rec_seen[key] = rec_total
    state["rec_total_seen"] = rec_seen

    # ---- publish guard (irreversible; explicitly forbidden) ----
    if procs.get("upload", 0) > 0:
        ok = kill_upload()
        actions.append(("killed_upload",
                        f"检测到 src.upload.upload 在运行(会自动投稿)，已{'成功' if ok else '尝试'}终止。"))
        problems.append({"id": "upload_running", "sev": "DEGRADED",
                         "msg": "发布进程 upload 在运行——已自动杀掉以防投稿。",
                         "fix": "确认没有人/脚本启动 upload.sh；只跑 scan。"})

    # ---- recorder config regression ----
    # The dangerous setting is source deletion: a bad remux must never remove
    # the only original recording.
    if flags.get("delete_source") == "auto":
        problems.append({"id": "delete_source_auto", "sev": "DEGRADED",
                         "msg": "delete_source=auto 危险：remux 坏了会把原始源删掉(6/20 无声那次的元凶之一)。",
                         "fix": "改回 delete_source=never，保留原始 flv。"})

    # ---- container reachable but recorders gone ----
    if procs.get("recorder", 0) == 0:
        problems.append({"id": "recorder_down", "sev": "DOWN",
                         "msg": "官方录播姬状态缺失、过期或不可达。",
                         "fix": "检查 free 上 bililive_recorder 容器、"
                                "/opt/bilive/recording/status.json 与 bililive_adapter 服务。"})

    # ---- OLD control plane RETIRED (2026-07-10, Ivan) ----
    # BililiveRecorder records; the autoslice runner (free cron, /opt/bilive/autoslice)
    # slices.  scan/local_prepare/shadow-daemon must NOT run: they double-
    # produce, burn AI-cover money on full segments, and their full-tree FUSE
    # rescans destabilized the CloudDrive mount (the 7/9 outage).  This monitor
    # used to AUTO-RESURRECT them (slice_blessed crash-recovery) — that logic is
    # deliberately gone; a running copy is reported, never restarted.
    for label, key in (("scan", "scan"), ("local_prepare", "local_prepare"),
                       ("auto-review-shadow-daemon", "auto_review_shadow")):
        if procs.get(key, 0) > 0:
            problems.append({"id": f"old_plane_{key}_running", "sev": "WARN",
                             "msg": f"已退役的旧控制面进程 {label} 在容器里运行（疑被手动/旧脚本拉起）。",
                             "fix": "旧管线 2026-07-10 已退役（HANDOFF 有据）。若无人在调试，"
                                    "进容器 pkill 对应模块；勿恢复 compose 旧 command。"})

    # ---- NEW plane health: autoslice runner heartbeat + alerts (free host) ----
    hb = probe.get("autoslice") or {}
    if hb.get("fatal"):
        problems.append({"id": "autoslice_probe_failed", "sev": "DEGRADED",
                         "msg": f"autoslice 健康探测失败: {hb['fatal']}",
                         "fix": "检查 ssh free 与 /opt/bilive/autoslice/reports/。"})
    if hb.get("heartbeat"):
        notes.append(f"autoslice 心跳: {hb['heartbeat'][:140]}")
    if not hb.get("fatal"):
        if hb.get("age_sec") is None:
            problems.append({"id": "autoslice_heartbeat_missing", "sev": "DEGRADED",
                             "msg": "读不到 autoslice runner 心跳（heartbeat.txt 缺失/无时间戳）。",
                             "fix": "查 free crontab 的 */10 tick 与 /opt/bilive/autoslice/logs/runner.log。"})
        elif hb["age_sec"] > 30 * 60:
            problems.append({"id": "autoslice_heartbeat_stale", "sev": "DEGRADED",
                             "msg": f"autoslice 心跳已 {fmt_age(int(hb['age_sec']))} 未更新（cron 每 10 分钟应一跳）。",
                             "fix": "查 free crontab 与 runner.log；确认 DISABLED 杀开关没被误留。"})
    if "SOURCE_UNAVAILABLE" in (hb.get("heartbeat") or ""):
        problems.append({"id": "autoslice_source_unavailable", "sev": "DOWN",
                         "msg": "autoslice 心跳报 SOURCE_UNAVAILABLE——录播挂载不可读（录制写入路径可能同断）。",
                         "fix": "mount 看门狗（*/5）应自愈；看 ALERT_MOUNT_WATCHDOG/ALERT_SOURCE_UNAVAILABLE，"
                                "未自愈按 HANDOFF 2026-07-09 手动修挂载并重启 bilive_record。"})
    for alert in hb.get("alerts", []):
        problems.append({"id": f"autoslice_alert_{alert['name']}", "sev": "WARN",
                         "msg": f"autoslice 近 6h 告警 {alert['name']}: {alert['last'][:160]}",
                         "fix": "看 free:/opt/bilive/autoslice/reports/ 对应 ALERT 文件全文。"})

    # ---- finished stream but the runner hasn't concluded that date ----
    if (not live) and rec.get("mtime"):
        ended_ago = probe["now"] - rec["mtime"]
        date_states = dict(re.findall(r"(\d{4}-\d{2}-\d{2}):(\S+)", hb.get("heartbeat") or ""))
        latest_state = date_states.get(str(primary.get("latest_date_dir") or ""))
        if ended_ago > SLICE_GRACE_SEC and latest_state in (None, "new", "sealing", "processing"):
            problems.append({"id": "autoslice_not_concluded", "sev": "DEGRADED",
                             "msg": f"最近一场已结束 {fmt_age(int(ended_ago))}，autoslice 该日期状态仍为 "
                                    f"{latest_state or '未知'}（应到 review_ready/no_delivery/paused_cpa_down）。",
                             "fix": "看 free runner.log 与 state/<date>.json；CPA 断供会显示 paused_cpa_down（属正常等待）。"})

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
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def write_reports(probe, verdict, problems, actions, notes, ts):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    payload = {"ts": ts.isoformat(), "verdict": verdict, "problems": problems,
               "actions": [{"kind": a[0], "msg": a[1]} for a in actions],
               "notes": notes, "probe": probe}
    with open(LATEST_JSON, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    hist = os.path.join(HISTORY_DIR, ts.strftime("%Y%m%d-%H%M%S") + f"-{verdict}.json")
    with open(hist, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    icon = {"OK": "✅", "WARN": "🟡", "DEGRADED": "🟠", "DOWN": "🔴"}[verdict]
    md = [f"# 李豆沙 自动切片监控 — {icon} {verdict}",
          "", f"生成: {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}", ""]
    md.append("## 状态")
    for n in notes:
        md.append(f"- {n}")
    if actions:
        md += ["", "## 监控自动处理"]
        for _, m in actions:
            md.append(f"- ✅ {m}")
    if problems:
        md += ["", "## 问题 & 建议"]
        for p in problems:
            md.append(f"- **[{p['sev']}] {p['msg']}**")
            md.append(f"  - 建议: {p['fix']}")
            for d in p.get("detail", [])[:6]:
                md.append(f"    - `{d}`")
    else:
        md += ["", "_无问题。_"]
    with open(LATEST_MD, "w") as f:
        f.write("\n".join(md) + "\n")
    return "\n".join(md)


SMTP_CONFIG = os.path.expanduser("~/.config/lidousha_monitor/smtp.json")


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
    probe = run_probe(state.get("last_audio_file", ""))
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

    print(f"[{ts:%Y-%m-%d %H:%M:%S}] {verdict}  problems={prob_ids}  "
          f"actions={[a[0] for a in actions]}")
    print(md)
    # A SUCCESSFUL monitor run exits 0 — the health verdict is surfaced via the
    # report file + email (Ivan 的约定：告警只走报告文件), NOT the process exit
    # code.  Under launchd, exit 1 on a monitored-DEGRADED made `launchctl list`
    # show a permanent "1" that reads as the monitor itself failing.  Opt into
    # the old verdict-as-exit-code behavior with --health-exit (manual/cron).
    if "--health-exit" in sys.argv:
        sys.exit({"OK": 0, "WARN": 0, "DEGRADED": 1, "DOWN": 2}[verdict])
    sys.exit(0)


if __name__ == "__main__":
    main()
