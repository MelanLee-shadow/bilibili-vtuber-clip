"""Remote probe programs for slice_monitor.py.

Each template is a Python program the monitor pipes to a remote interpreter over
ssh (stdin).  They live here only to keep the monitor entrypoint small; the
monitor substitutes the ``__ROOMS__`` / ``__ROOM__`` placeholders from its own
single configuration.
"""

LEGACY_PROBE_TEMPLATE = r'''
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
    "auto_review_shadow": count_live(r"auto_review_shadow_daemon"),  # no-upload review gate
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
'''


PRODUCTION_PROBE_TEMPLATE = r'''
import json, re, shutil, subprocess, time

STATUS_PATH = "__STATUS_PATH__"
STATE_PATH = "__STATE_PATH__"
RECORDER_ACTIVE = __RECORDER_ACTIVE__
AUDIT_PATH = "/opt/bilive/autoslice/repo/ops/recording/record_health_audit.py"
ROOM = "__ROOM__"


def read_object(path):
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def process_count(needle):
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "args"], text=True, errors="replace"
        )
    except Exception:
        return 0
    return sum(1 for line in output.splitlines()
               if needle in line and "grep" not in line and " -m pytest" not in line)


def last_json_line(text):
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def recorder_health():
    if not RECORDER_ACTIVE:  # paused by the topology: no live ledger to read or audit
        return {}, None
    status = read_object(STATUS_PATH)
    if status.get("schema_version") != "recorder-neutral-status.v1":
        raise ValueError("recorder status schema mismatch")
    audit_run = subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/python3", AUDIT_PATH, "--lookback-hours", "96",
         "--status-path", STATUS_PATH, "--state-path", STATE_PATH],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    audit = last_json_line(audit_run.stdout)
    if audit is None:
        audit = {
            "schema_version": "record-health-audit.v1",
            "status": "FAIL",
            "healthy": False,
            "service_reachable": status.get("service_reachable"),
            "unresolved": [
                f"record_health_audit rc={audit_run.returncode}: "
                + (audit_run.stderr.strip()[-240:] or "no JSON output")
            ],
        }
    return status, audit


try:
    status, audit = recorder_health()
    latest = status.get("latest_source")
    if not isinstance(latest, dict):
        latest = {}
    relative = str(
        latest.get("relative_path")
        or latest.get("path")
        or latest.get("file")
        or ""
    )
    date_match = re.search(r"(?:^|/)(20\d{2}-\d{2}-\d{2})(?:/|$)", relative)
    latest_date = date_match.group(1) if date_match else None
    media = latest.get("media") if isinstance(latest.get("media"), dict) else {}
    source_mtime = latest.get("mtime_epoch")
    if isinstance(source_mtime, bool) or not isinstance(source_mtime, (int, float)):
        source_mtime = status.get("generated_at_epoch")
    source_size = latest.get("size_bytes")
    if isinstance(source_size, bool) or not isinstance(source_size, (int, float)):
        source_size = None
    try:
        free_bytes = shutil.disk_usage("/opt/bilive").free
    except OSError:
        free_bytes = None
    recording = status.get("recording") is True
    streaming = status.get("streaming") is True
    service = status.get("service_reachable") is True
    payload = {
        "ok": True,
        "now": time.time(),
        "procs": {
            "recorder": 1 if service else 0,
            "scan": process_count("session_autoslice.py"),
            "local_prepare": process_count("local_prepare"),
            "upload": process_count("src.upload.upload"),
            "auto_review_shadow": process_count("lidousha_auto_review_shadow"),
        },
        "rooms": {
            ROOM: {
                "latest_date_dir": latest_date,
                "recording": {
                    "active": recording or streaming,
                    "mtime": source_mtime,
                    "age_sec": (
                        None
                        if not isinstance(source_mtime, (int, float))
                        else max(0, int(time.time() - float(source_mtime)))
                    ),
                    "latest_file": relative or None,
                },
                "slices": {
                    "age_sec": None,
                    "count_latest_dir": None,
                    "cover_count_latest_dir": None,
                    "publish_json_latest_dir": None,
                },
                "dirty_backlog": [],
                "recorder_status": {
                    "live_status": 1 if streaming else 0,
                    "finalizing": status.get("finalizing") is True,
                    "running_status": 1 if service else 0,
                    "total_output_bytes": source_size,
                    "real_stream_format": status.get("real_stream_format"),
                    "requested_quality_number": status.get("requested_quality_number"),
                    "active_media": media,
                    "cookie_login_valid": status.get("cookie_login_valid"),
                    "cookie_health_error": status.get("cookie_health_error"),
                },
            }
        },
        "flags": {"delete_source": "never"},
        "disk": {"free": free_bytes},
        "audio": {},
        "auto_review_shadow": {},
        "record_health": audit,
    }
    print(json.dumps(payload, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({
        "ok": False,
        "fatal": f"production probe: {type(exc).__name__}: {exc}",
    }, ensure_ascii=False))
'''


JINGTING_PROBE_PY = r'''
import json, os, re, subprocess, time

ROOT = "/path/to/cloud-drive/live-streaming"
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
    "agy_count": ps_count("agy"),
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
