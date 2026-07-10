#!/usr/bin/env python3
"""Unattended post-stream auto-slice runner (runs ON the free host).

Ivan's goal (2026-07-05): when a 李豆沙 stream ends, free starts the FULL
canonical pipeline by itself — no human kick-off:

    stream end (blrec live_status via API)
      → per new segment: BCUT aggregate ASR transcript (ms timeline)
      → semantic recall candidate selection (CPA, viewer-perspective, with the
        curated slice-selection metric; deterministic fallback lanes if the
        LLM is down — zero-output is loud, never silent)
      → top-N talk candidates + up to 2 songs (highest danmaku)
      → produce_slice_package per candidate (BCUT+AGY+CPA subtitles, sentence
        boundaries, pillarbox, sapphire72 burn, REAL CPA cover, 李豆沙-style
        title) / song LRC lane with the strict completeness gate
      → delivery under <repo>/lidousha/<date>/ + AUTOSLICE_SUMMARY.md with the
        selection reason (hook) and confidence per clip for human review
      → status report file (no chat/email; Mac pulls via launchd)

Upload stays OFF by design: this runner has no upload path at all and
produce_slice_package writes publish drafts with upload_enabled=false.

HARD LESSONS BAKED IN (first real run, 2026-07-06):
- **CPA health gate**: recordings can wait, garbage cannot be unshipped.  If
  the CPA chat lane is down (gpt-5.6/5.5/5.4 provider outages happen), the
  batch is DEFERRED (status=paused_cpa_down) and resumes on a later tick —
  clips are never produced with cid titles / cid-text covers.
- **Title is part of the product**: a pick whose title generation failed is
  NOT delivered; it stays pending and is retried on resume (bounded).
- **Dead segments**: blrec restart stubs (a few KB of mp4) and segments whose
  BCUT transcription fails twice are marked dead and never retried again (the
  first run retried a 2.9KB stub every 10 minutes forever).
- **Subtitle fonts**: the sapphire72 ASS names "Microsoft YaHei"; Linux needs
  a CJK fallback font installed (`apt install fonts-noto-cjk`) or every glyph
  burns as a tofu box.  Checked at startup, loud in the report if missing.
- **Songs**: semantic recall's per-segment candidate cap squeezes songs out,
  so a deterministic performance-detector supplement also feeds the song
  queue; final pick = top MAX_SONGS_PER_DATE by danmaku count.
- **Covers self-heal**: the CPA image lane fails independently of the chat
  lane (2026-07-06: gateway 400 "multiples of 16" blanked a whole batch's
  covers while titles/subtitles were fine).  A delivered clip without a cover
  gets a bounded cover-only repair pass (regenerate_lidousha_cover) on later
  ticks — never a re-produce, never silent (summary shows repair state).

RUNNER v4 (2026-07-09 external audit — "the control plane was lying"):
- **Source health first**: a dead/hung CloudDrive FUSE mount used to read as
  "no new recordings" and the heartbeat stayed green while the RECORDER's
  write path was broken.  Every tick now probes the mount (subprocess ls with
  timeout, hang-proof); failure → ALERT file + SOURCE_UNAVAILABLE heartbeat +
  do nothing.  A cron watchdog (free_mount_watchdog.sh) self-heals the mount.
- **Honest status words**: song gate BLOCK is `blocked`, never `ok`; a batch
  with zero deliveries is `no_delivery`, never `done`.  Delivered talk is
  `review_ready`, or `quarantine` when deterministic boundary red flags fired
  (produce_slice_package computes them; quarantine is delivered-but-flagged).
- **Budget = deliveries**: gate-blocked songs no longer consume the per-date
  song budget; the danmaku-sorted backlog backfills (bounded SONG_ATTEMPT_CAP).
- **Global selection + sealing**: talk picks are ranked globally by recall
  confidence (soft per-segment diversity cap) and selection only happens after
  the segment inventory is STABLE across ticks — late segments compete instead
  of arriving to spent quota.
- **Atomic state**: tmp+rename writes with .bak; corrupt state quarantines the
  file and BLOCKS the date (state_corrupt_blocked) instead of silently
  reprocessing from scratch.

Deployment (free):
    repo   /opt/bilive/autoslice/repo        (rsync of scripts/ src/ assets/)
    env    /opt/bilive/autoslice/cpa.env     (CPA_BASE_URL / CPA_API_KEY, 600)
    state  /opt/bilive/autoslice/state/<date>.json
    cron   */10 min: flock -n lock python3 scripts/free_session_autoslice.py --once
    kill   touch /opt/bilive/autoslice/DISABLED to pause everything
    deps   fonts-noto-cjk (subtitle rendering), ffmpeg, PIL, self-ssh key
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BASE = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
ROOM = os.environ.get("AUTOSLICE_ROOM", "22966160")
REC_ROOT = Path(
    os.environ.get(
        "AUTOSLICE_REC_ROOT",
        f"/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/{ROOM}",
    )
)
BLREC_PORT = int(os.environ.get("AUTOSLICE_BLREC_PORT", "22333"))
BILIVE_ENV = Path("/opt/bilive/.env")
CPA_ENV = BASE / "cpa.env"
MAX_TALK_PICKS = 5
MAX_SONGS_PER_DATE = 2  # Ivan 2026-07-05: 每场直播至多两个歌切，按弹幕最高的两个
TALK_PER_SEGMENT_CAP = 2  # diversity guard on the GLOBAL confidence ranking; slack refills
SONG_ATTEMPT_CAP = 6  # total song-lane attempts per date (delivered + blocked + failed);
                      # gate-blocked songs do NOT consume the delivery budget — the
                      # backlog backfills — so an unlucky date needs a hard attempt cap
# Delivered-to-review talk statuses.  "ok" is the pre-2026-07-09 name kept for
# old state files; new records are review_ready (clean) or quarantine (delivered
# WITH deterministic red flags — reviewable, never silently green).
DELIVERED_TALK_STATUSES = {"ok", "review_ready", "quarantine"}
PER_SEGMENT_CANDIDATES = 4
MIN_SEGMENT_BYTES = 5_000_000  # blrec restart stubs are a few KB — dead on sight
BCUT_MAX_ATTEMPTS = 2
TITLE_MAX_ATTEMPTS = 3
COVER_REPAIR_MAX_ATTEMPTS = 3  # one attempt per tick → retries spread ~10min apart
MAX_PARALLEL_PRODUCE = 3  # slices are independent; produce them concurrently (each is
                          # network-bound on AGY/CPA/gpt-image-2, so a few in flight
                          # cut wall-clock ~3x; bounded by free CPU + CPA concurrency)
PIECE_PRE_MS = 10_000
PIECE_POST_MS = 32_000
SONG_WINDOW_PRE_MS = 15_000   # window must stay SONG-dominated or the in-window
SONG_WINDOW_POST_MS = 20_000  # recall reclassifies it as talk (smoke-proven at
                              # ±60/45s and ±180/150s); 15/20s matches the
                              # validated 虫儿飞 run.
# A recall window is still only an anchor.  If it identifies a song but cannot
# prove both LRC ends, retry once with enough original-source context for the
# boundary resolver to recover missed intro/tail audio.  This fixed the 7/9
# 《芽吹くとき》case where aggregate ASR began ~18s late and ended ~27s
# early; the old 15/20 source window physically excluded the true boundaries.
SONG_PROOF_RETRY_PRE_MS = 45_000
SONG_PROOF_RETRY_POST_MS = 45_000
SONG_ANCHOR_TRIM_MIN_MS = 20_000  # only retry on the danmaku-dense core when the
                                  # trim drops ≥20s of talk padding off an end
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Per-stage CPA model chains (2026-07-10, Ivan): sol ONLY where open-ended
# judgment is load-bearing — semantic recall (editorial pick over a 30-min
# transcript) and the single brand-critical title call (high effort, short
# prompt).  Terra (the everyday 5.5 successor) carries the supporting lanes:
# song hints are non-load-bearing (known_songs fingerprint pinning + clean-line
# search are the authority; a wrong hint is discarded by the alignment gate)
# and cover art direction is a structured, fail-open pick with a deterministic
# fallback.  Every chain falls back gpt-5.5 → gpt-5.4.  gpt-5.6-luna would suit
# the structured lanes but is auth_unavailable on CPA today (providers=codex).
CPA_CMD_DEEP = "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' medium"
CPA_CMD_TITLE = "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' high"
CPA_CMD_STANDARD = "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-terra gpt-5.5 gpt-5.4' medium"
# The selector's --cpa-command is the semantic-QA JUDGE lane (request/response
# JSON contract), NOT a prompt/completion LLM template — canonical validated
# command per docs/spark/2026-06-30-future-live-e2e-runbook.md.  The selector
# does NOT run it through a shell, so the api-base must be substituted here
# (the key stays off the command line via --api-key-env).


def cpa_qa_cmd() -> str:
    # ``produce_song`` is also a supported/manual repair entry point and does
    # not pass through ``main()``, which injects cpa.env into os.environ.  Read
    # the same credential file as child_env() so the judge command and its
    # subprocess environment cannot disagree (empty --api-base used to make a
    # manual rerun die in argparse before song proof even started).
    env_file = load_env_file(CPA_ENV)
    base = (env_file.get("CPA_BASE_URL") or os.environ.get("CPA_BASE_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError(f"CPA_BASE_URL missing from environment and {CPA_ENV}")
    return (
        "python3 scripts/cpa_semantic_qa_llm.py --request {request_json} --response {response_json} "
        f"--transport direct --model gpt-5.4-mini --api-base {base} --api-key-env CPA_API_KEY"
    )


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("export ").strip()
            if "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(load_env_file(CPA_ENV))
    env.setdefault("HOME", "/root")
    return env


def cpa_healthy() -> bool:
    """One cheap real probe against the CPA chat lane, walking the same failover
    order production uses (gpt-5.6-sol, then the gpt-5.5 / gpt-5.4 fallbacks) —
    any healthy model in the chain means the lane can serve the batch.

    The whole batch is gated on this: provider outages (auth_unavailable 503)
    are a normal operating condition, and producing clips without a working
    title/reconcile LLM ships garbage.  Recordings can wait; the cron tick is
    the retry loop.
    """

    env = load_env_file(CPA_ENV)
    base = (env.get("CPA_BASE_URL") or os.environ.get("CPA_BASE_URL", "")).rstrip("/")
    key = env.get("CPA_API_KEY") or os.environ.get("CPA_API_KEY", "")
    if not base or not key:
        return False
    for model in ("gpt-5.6-sol", "gpt-5.5", "gpt-5.4"):
        body = json.dumps(
            {"model": model, "input": "回复:OK", "reasoning": {"effort": "low"}, "max_output_tokens": 2000}
        ).encode()
        req = urllib.request.Request(
            f"{base}/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — any failure means this model is down
            continue
    return False


def cjk_font_present() -> bool:
    completed = subprocess.run(["fc-list"], check=False, capture_output=True, text=True, timeout=30)
    return bool(re.search(r"CJK|WenQuan|LXGW|YaHei|PingFang", completed.stdout))


def blrec_live_status() -> bool | None:
    """True=live, False=not live, None=unknown (API down → fail-safe skip)."""
    key = load_env_file(BILIVE_ENV).get("RECORD_KEY", "")
    if not key:
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{BLREC_PORT}/api/v1/tasks/{ROOM}/data",
        headers={"x-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return int(data.get("room_info", {}).get("live_status", 0)) == 1
    except Exception as exc:  # noqa: BLE001 — any API failure means "unknown"
        log(f"blrec API unavailable: {exc}")
        return None


def state_path(date: str) -> Path:
    return BASE / "state" / f"{date}.json"


def read_state(date: str) -> dict:
    """State loader that never mistakes damage for a fresh start.

    Missing file → {} (genuinely new date).  Corrupt JSON → the damaged file is
    quarantined for forensics and the .bak (previous good write) is restored;
    with no usable .bak the date is BLOCKED (state_corrupt_blocked), because
    reprocessing 'from scratch' would re-produce and re-deliver everything
    (2026-07-09 audit: corruption must be loud, not a silent reset)."""
    path = state_path(date)
    bak = path.with_suffix(".json.bak")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        try:  # crash window between the two os.replace()s in write_state
            return json.loads(bak.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    except OSError as exc:
        return {"status": "state_corrupt_blocked", "state_error": f"unreadable: {exc}"}
    except ValueError as exc:
        quarantined = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%dT%H%M%S')}")
        try:
            path.rename(quarantined)
        except OSError:
            pass
        log(f"state for {date} is corrupt ({exc}) → kept as {quarantined.name}")
        try:
            restored = json.loads(bak.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Persist the blocked marker so EVERY subsequent read agrees — a
            # rename alone would make the next read see "new date" and happily
            # re-produce and re-deliver the whole batch.
            blocked = {"status": "state_corrupt_blocked", "state_error": f"corrupt json, no usable .bak: {exc}"}
            path.write_text(json.dumps(blocked, ensure_ascii=False, indent=2), encoding="utf-8")
            return blocked
        log(f"state for {date} restored from .bak")
        restored["state_restored_from_bak"] = True
        write_state(date, restored)
        return restored


def write_state(date: str, state: dict) -> None:
    """Atomic write (tmp + os.replace) keeping the previous version as .bak —
    a mid-write crash can no longer leave a half-written unparseable state."""
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path = state_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if path.exists():
        os.replace(path, path.with_suffix(".json.bak"))
    os.replace(tmp, path)


def write_alert(name: str, message: str) -> None:
    """Append-only alert files under reports/ — the Mac launchd pull is the
    delivery channel (Ivan's rule: alerts travel via report files, not chat)."""
    path = BASE / "reports" / f"ALERT_{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as sink:
        sink.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")


def source_health_error() -> str | None:
    """Probe the recordings mount via a subprocess `ls` so a HUNG FUSE mount
    (which blocks Python's stat() forever) times out instead of wedging the
    tick.  Returns None when healthy, else a short error string.  2026-07-09:
    the CloudDrive endpoint died and every layer above swallowed the OSError
    into 'no dates' — the control plane kept reporting green for hours."""
    try:
        completed = subprocess.run(
            ["ls", str(REC_ROOT)], check=False, capture_output=True, text=True, timeout=25
        )
    except subprocess.TimeoutExpired:
        return f"listing {REC_ROOT} timed out after 25s (hung mount?)"
    if completed.returncode != 0:
        return (completed.stderr.strip() or f"ls rc={completed.returncode}")[:300]
    return None


def list_dates() -> list[str]:
    try:
        names = [p.name for p in REC_ROOT.iterdir() if p.is_dir() and DATE_RX.match(p.name)]
    except OSError as exc:
        log(f"list_dates: recordings root unreadable: {exc}")
        return []
    return sorted(names)[-3:]


def list_segments(date: str) -> list[Path]:
    date_dir = REC_ROOT / date
    try:
        files = sorted(date_dir.glob(f"{ROOM}_*.mp4"))
    except OSError as exc:
        log(f"list_segments({date}): unreadable: {exc}")
        return []
    return [f for f in files if f.parent == date_dir]


def ffprobe_ms(path: Path) -> int:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=False, capture_output=True, text=True, timeout=600,
    )
    try:
        return int(float(completed.stdout.strip()) * 1000)
    except ValueError:
        return 0


def bcut_transcribe(segment: Path, date: str) -> Path | None:
    cache = BASE / "cache" / date / f"{segment.stem}.bcut.srt"
    if cache.is_file() and cache.stat().st_size > 0:
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "free_asr_client.py"), str(segment), "--srt", str(cache)],
        check=False, capture_output=True, text=True, timeout=900, cwd=str(REPO_ROOT), env=child_env(),
    )
    if completed.returncode != 0 or not cache.is_file() or cache.stat().st_size == 0:
        log(f"BCUT transcribe FAILED for {segment.name}: {completed.stderr[-200:]}")
        return None
    return cache


def find_danmaku_xml(segment: Path) -> Path | None:
    digits = re.sub(r"\D", "", segment.stem)
    for folder in (segment.parent, segment.parent / "sources"):
        try:
            for xml in folder.glob("*.xml"):
                if re.sub(r"\D", "", xml.stem) == digits and xml.stat().st_size > 0:
                    return xml
        except OSError:
            continue
    return None


def danmaku_hints(xml_path: Path | None) -> str | None:
    if xml_path is None:
        return None
    try:
        from src.autoslice.danmaku_evidence import find_danmaku_bursts, load_danmaku_xml

        items = load_danmaku_xml(xml_path)
        bursts = find_danmaku_bursts(items)
    except Exception:  # noqa: BLE001 — hints are optional enrichment
        return None
    lines = []
    for burst in bursts[:6]:
        sample = " / ".join(burst.sample_texts[:3])
        lines.append(f"{burst.start_ms // 60000:02d}:{burst.start_ms // 1000 % 60:02d} x{burst.count}: {sample}")
    return "弹幕突发时段(观众密集反应，强候选提示):\n" + "\n".join(lines) if lines else None


def danmaku_count_in(xml_path_str: str | None, start_ms: int, end_ms: int) -> int:
    if not xml_path_str:
        return 0
    try:
        from src.autoslice.danmaku_evidence import load_danmaku_xml

        items = load_danmaku_xml(Path(xml_path_str))
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for item in items if start_ms <= item.offset_ms < end_ms)


def slice_srt(src_srt: Path, start_ms: int, end_ms: int, dest: Path) -> int:
    """Cut [start_ms, end_ms) out of an SRT and rebase timestamps to 0."""
    from scripts.run_auto_review_shadow_pipeline import _parse_srt

    def ts(ms: int) -> str:
        ms = max(0, ms)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    blocks = []
    for cue in _parse_srt(src_srt):
        if cue.source_end_ms <= start_ms or cue.source_start_ms >= end_ms:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n{ts(cue.source_start_ms - start_ms)} --> {ts(cue.source_end_ms - start_ms)}\n{cue.text}"
        )
    dest.write_text("\n\n".join(blocks) + "\n" if blocks else "", encoding="utf-8")
    return len(blocks)


def safe_name(text: str, fallback: str) -> str:
    """Human-readable delivery filename from the recall hook."""
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "", (text or "").strip())
    return cleaned[:18] if cleaned else fallback


def last_json_block(text: str) -> dict:
    """Parse the LAST balanced top-level JSON object in text (produce logs end
    with a summary object that contains nested objects — a non-greedy regex
    can't match it; this walks braces from the last closing brace backwards)."""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for start in range(end, -1, -1):
            ch = text[start]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : end + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        break
                    break
        end = text.rfind("}", 0, max(0, end))
    return {}


def recall_candidates(srt_path: Path, hints: str | None) -> tuple[list, str, dict]:
    """(candidates, lane, extras) — semantic lane first, deterministic fallback.

    extras maps candidate_id → {"hook": 选片理由, "confidence": 打分} so the
    review summary can show WHY each clip was picked (Ivan 2026-07-06).
    """
    from scripts.run_auto_review_shadow_pipeline import _parse_srt
    from src.autoslice.full_session_candidate_selector import select_full_session_candidates
    from src.autoslice.llm_client import LlmCallError, LlmConfig, build_llm_call
    from src.autoslice.semantic_candidate_selector import select_semantic_session_candidates

    cues = _parse_srt(srt_path)
    if not cues:
        return [], "empty", {}
    llm = build_llm_call(
        LlmConfig(
            transport="command",
            # Talk semantic recall = deep open-ended lane → gpt-5.6-sol medium.
            command_template=f"bash {REPO_ROOT}/scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}} 'gpt-5.6-sol gpt-5.5 gpt-5.4' medium",
            timeout_seconds=600.0,
        )
    )
    try:
        candidates, diag = select_semantic_session_candidates(
            cues, llm_call=llm, max_candidates=PER_SEGMENT_CANDIDATES, danmaku_hints=hints
        )
        hooks = diag.get("hooks") or {}
        extras = {}
        for cand in candidates:
            cid = cand.anchor.candidate_id
            extras[cid] = {
                "hook": str(hooks.get(cid) or ""),
                "confidence": round(float(getattr(cand.boundary, "start_boundary_score", 0.5) or 0.5), 2),
            }
        # Deterministic song supplement: recall's candidate cap squeezes songs
        # out on song-heavy streams (first real run: 4+ songs sung, 1 caught).
        try:
            supplement = select_full_session_candidates(cues, max_candidates=8)
        except Exception:  # noqa: BLE001
            supplement = []
        for cand in supplement:
            if getattr(cand, "content_type_hint", "talk") != "song":
                continue
            if any(
                getattr(c, "content_type_hint", "talk") == "song"
                and not (cand.anchor.anchor_end_ms <= c.anchor.anchor_start_ms or cand.anchor.anchor_start_ms >= c.anchor.anchor_end_ms)
                for c in candidates
            ):
                continue  # overlaps a recall song → duplicate
            candidates.append(cand)
            extras[cand.anchor.candidate_id] = {"hook": "确定性歌检测补充(演唱段)", "confidence": 0.5}
        return candidates, "semantic_recall", extras
    except LlmCallError as exc:
        log(f"semantic recall failed ({exc}); falling back to deterministic lanes")
        fallback = select_full_session_candidates(cues, max_candidates=PER_SEGMENT_CANDIDATES)
        return fallback, "deterministic_fallback", {}


def read_publish_meta(work_dir: Path) -> dict:
    for publish in sorted(work_dir.glob("replacement_recuts/*.publish.json")):
        try:
            d = json.loads(publish.read_text(encoding="utf-8"))
            return {
                "title": d.get("title"),
                "title_source": d.get("title_source"),
                "cover_status": d.get("cover_status"),
            }
        except (OSError, ValueError):
            continue
    return {}


def produce_talk(date: str, item: dict, *, reuse_cover: bool = False) -> dict:
    """Run produce_slice_package for one pending talk item (plain-dict spec).

    Returns the result record; status is one of ok / title_failed / failed.
    A title_failed pick is cleaned up (no delivery with a cid title/cover) and
    retried on a later resume.  ``reuse_cover`` keeps the existing delivered
    cover (subtitle-only re-run) and skips the ~90s AI cover step.
    """
    cid = item["cid"]
    out_root = BASE / "out" / date
    delivery_name = safe_name(item.get("hook", ""), cid)
    spec = {
        "candidate_id": cid,
        "date": date,
        "output_root": str(out_root),
        "delivery_name": delivery_name,
        "given_title": None,
        "lead_pad_ms": 400,
        "semantic_start_ms": item["start_ms"],
        "semantic_end_ms": item["end_ms"],
        "pieces": [
            {
                "remote_media": item["segment_path"],
                "start_ms": max(0, item["start_ms"] - PIECE_PRE_MS),
                "end_ms": min(item["seg_dur_ms"], item["end_ms"] + PIECE_POST_MS) if item["seg_dur_ms"] else item["end_ms"] + PIECE_POST_MS,
                **({"danmaku_xml_local": item["xml"]} if item.get("xml") else {}),
            }
        ],
    }
    out_root.mkdir(parents=True, exist_ok=True)
    spec_path = out_root / f"spec_{cid}.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    log_path = BASE / "logs" / f"{date}_{cid}.log"
    log(f"producing {cid} ({(item['end_ms'] - item['start_ms']) // 1000}s) from {Path(item['segment_path']).name}")
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "produce_slice_package.py"),
           "--spec", str(spec_path), "--ssh-host", "localhost"]
    if reuse_cover:
        cmd.append("--reuse-cover")
    with open(log_path, "a", encoding="utf-8") as sink:
        completed = subprocess.run(
            cmd, check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=5400,
            cwd=str(REPO_ROOT), env=child_env(),
        )
    result = {
        "candidate_id": cid, "segment": Path(item["segment_path"]).name,
        "start_ms": item["start_ms"], "end_ms": item["end_ms"],
        "hook": item.get("hook", ""), "confidence": item.get("confidence"),
        "lane": item.get("lane", ""), "rc": completed.returncode, "log": str(log_path),
    }
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    result["summary"] = last_json_block(tail)
    result.update(read_publish_meta(out_root / cid))
    if completed.returncode != 0:
        result["status"] = "failed"
        return result
    if "llm_failed" in str(result.get("title_source") or ""):
        # No delivery with a cid title / cid-text cover — clean and retry later.
        delivered = REPO_ROOT / "lidousha" / date
        for f in delivered.glob(f"{delivery_name}.*"):
            f.unlink(missing_ok=True)
        recuts = out_root / cid / "replacement_recuts"
        if recuts.is_dir():
            import shutil

            shutil.rmtree(recuts, ignore_errors=True)
        result["status"] = "title_failed"
        return result
    # Deterministic boundary red flags (2026-07-09 audit): delivered artifacts
    # with a suspicious boundary are QUARANTINED for review, never silently green.
    red_flags = list((result.get("summary") or {}).get("red_flags") or [])
    result["red_flags"] = red_flags
    result["status"] = "quarantine" if red_flags else "review_ready"
    return result


_SRT_TS_RX = re.compile(
    r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*(\d\d):(\d\d):(\d\d),(\d\d\d)"
)


def _srt_cue_spans(srt_path: Path, lo_ms: int, hi_ms: int) -> list[tuple[int, int]]:
    """(start_ms, end_ms) cue spans overlapping [lo,hi] from a whole-segment SRT."""
    try:
        text = srt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    spans = []
    for m in _SRT_TS_RX.finditer(text):
        s = (int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])) * 1000 + int(m[4])
        e = (int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7])) * 1000 + int(m[8])
        if e >= lo_ms and s <= hi_ms:
            spans.append((s, e))
    return spans


def _song_core_span(srt_path: Path, lo_ms: int, hi_ms: int,
                    *, min_gap_ms: int = 8_000, edge_frac: float = 0.35, min_span_ms: int = 90_000,
                    tail_gap_ms: int = 16_000, tail_frac: float = 0.12) -> tuple[int, int]:
    """Trim [lo,hi] to the sung core using ASR speech gaps.  A performance is set
    off from the surrounding chatter by a ≥8s music-intro gap at the front and a
    ≥16s outro gap at the back (《屑屑》: the recall anchor bloated BOTH ends — into
    the pig-nose talk before AND the '谢谢大家' talk after, so the window kept
    classifying as talk).  LEAD trim: song starts after the last big gap in the
    leading ``edge_frac``.  TAIL trim is DELIBERATELY conservative — only a LARGE
    (≥16s, bigger than a mid-song instrumental interlude) gap in the LAST
    ``tail_frac`` of the span counts, so the song's own late breaks are never
    clipped (clipping → SONG_PARTIAL, worse than carrying a little outro).  Returns
    (lo,hi) unchanged / partially-trimmed when a trim would be degenerate."""
    cues = _srt_cue_spans(srt_path, lo_ms, hi_ms)
    if len(cues) < 4 or hi_ms - lo_ms <= min_span_ms:
        return lo_ms, hi_ms
    span = hi_ms - lo_ms
    lead_cut = lo_ms + edge_frac * span
    lead = [(s1 - e0, s1) for (s0, e0), (s1, e1) in zip(cues, cues[1:]) if e0 <= lead_cut and (s1 - e0) >= min_gap_ms]
    new_lo = max(lead, key=lambda g: g[0])[1] if lead else lo_ms
    tail_cut = hi_ms - tail_frac * span
    tail = [e0 for (s0, e0), (s1, e1) in zip(cues, cues[1:]) if s1 >= tail_cut and (s1 - e0) >= tail_gap_ms]
    new_hi = min(tail) if tail else hi_ms
    if new_lo == lo_ms and new_hi == hi_ms:
        return lo_ms, hi_ms
    if new_hi - new_lo < min_span_ms:  # a trim would over-shorten → keep the generous span
        return lo_ms, hi_ms
    return int(new_lo), int(new_hi)


def song_status(rc: int, delivered: bool) -> str:
    """Honest song-lane status words (2026-07-09 audit): the selector exiting 0
    only means the PIPELINE ran.  blocked = not a song / performance incomplete
    / no materialized artifact — never 'ok'."""
    if rc != 0:
        return "failed"
    return "review_ready" if delivered else "blocked"


def song_proof_retry_window(anchor_start_ms: int, anchor_end_ms: int, segment_duration_ms: int) -> tuple[int, int]:
    """Expand a recall anchor against the original segment for LRC proof."""
    start_ms = max(0, anchor_start_ms - SONG_PROOF_RETRY_PRE_MS)
    end_ms = anchor_end_ms + SONG_PROOF_RETRY_POST_MS
    if segment_duration_ms:
        end_ms = min(segment_duration_ms, end_ms)
    return start_ms, end_ms


def song_delivery_ok(
    selector_rc: int,
    is_song: bool,
    reason_codes,
    completion_evidence: bool | dict | None,
) -> bool:
    """Ivan's FINAL song rule (2026-07-10): at most MAX_SONGS_PER_DATE per date,
    danmaku-desc; a song is delivered when the window IS a song and the
    performance is AFFIRMATIVELY PROVEN complete.  Absence of ``SONG_PARTIAL``
    is not evidence: the 2026-07-09 ``芽吹くとき`` run had no LRC proof
    and therefore never emitted that negative code, but was still incorrectly
    delivered.  Everything else the semantic judge flags (closure, viewer
    context, boundary style, AUTO_UPLOAD/BLOCK itself) is reviewer REFERENCE,
    not a delivery gate."""
    proof_ready = (
        completion_evidence is True
        or (isinstance(completion_evidence, dict) and completion_evidence.get("ready") is True)
    )
    return (
        selector_rc == 0
        and bool(is_song)
        and proof_ready
        and "SONG_PARTIAL" not in (reason_codes or [])
    )


def fresh_song_selector_dir(out_dir: Path, tag: str) -> Path:
    """Create an empty, invocation-owned selector output directory.

    Selector attempts used to share ``song_selector{tag}``.  A failed process
    could therefore leave the runner reading a previous invocation's valid
    ``summary.json`` and artifacts.  Keep every attempt as non-destructive
    evidence under the stable tag directory, but give the current subprocess a
    new empty child so only files it writes can influence this attempt.
    """
    history_dir = out_dir / f"song_selector{tag}"
    history_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="attempt-", dir=history_dir))


def record_is_song(entry: dict) -> bool:
    """The window IS a song when the in-window recall classified it as one
    (semanticsong_* record id) OR the LRC lane pinned/aligned it.  Keep that
    upstream anchor through later selector retries; final delivery still needs
    independent positive boundary and lyric evidence and otherwise fails closed."""
    job = entry.get("source_context_job") or {}
    return (
        str(entry.get("candidate_id") or "").startswith("semanticsong")
        or job.get("content_type_hint") == "song"
        or job.get("song_candidate") is True
        or job.get("requires_full_source_song_boundary_redo") is True
        or bool(job.get("song_boundary"))
        or bool(job.get("lyrics_alignment"))
    )


def song_delivery_artifacts(record: dict) -> dict:
    """Best-known materialized artifacts for a song record, with sha256 hashes
    whenever the pipeline recorded them (hash hygiene stays; SEMANTIC gating
    does not — the delivery decision is song_delivery_ok).  A missing/blocked
    cover never blocks the video: covers are generated after the release gate
    now, and repair_covers backfills delivered clips."""
    recut = record.get("materialized_recut")
    if not isinstance(recut, dict):
        return {}
    out: dict = {}
    burned = recut.get("burned_preview")
    if isinstance(burned, dict) and burned.get("status") == "BURNED" and burned.get("path"):
        out["video_path"] = str(burned["path"])
        if burned.get("burned_sha256"):
            out["video_sha256"] = str(burned["burned_sha256"])
    recut_hashes = recut.get("artifact_hashes")
    if isinstance(recut_hashes, dict) and recut_hashes.get("burned_video_sha256"):
        out["video_sha256"] = str(recut_hashes["burned_video_sha256"])
    if recut.get("subtitle_path"):
        out["subtitle_path"] = str(recut["subtitle_path"])
        if isinstance(recut_hashes, dict) and recut_hashes.get("subtitle_sha256"):
            out["subtitle_sha256"] = str(recut_hashes["subtitle_sha256"])
    if recut.get("manifest_path"):
        out["recut_manifest_path"] = str(recut["manifest_path"])
    gate = recut.get("cover_release_gate")
    if isinstance(gate, dict):
        out["cover_release_gate_satisfied"] = gate.get("satisfied")
        out["release_gate_path"] = str(gate.get("path") or "")
        gate_hashes = gate.get("artifact_hashes")
        if isinstance(gate_hashes, dict) and gate_hashes.get("burned_video_sha256"):
            out["video_sha256"] = str(gate_hashes["burned_video_sha256"])
    staging = recut.get("publish_staging")
    if isinstance(staging, dict) and staging.get("status") == "STAGED":
        if staging.get("title") or record.get("title"):
            out["title"] = str(staging.get("title") or record.get("title") or "")
        if staging.get("cover_path"):
            out["cover_path"] = str(staging["cover_path"])
            recut_hashes = recut.get("artifact_hashes")
            if isinstance(recut_hashes, dict) and recut_hashes.get("cover_sha256"):
                out["cover_sha256"] = str(recut_hashes["cover_sha256"])
    return out


def _matches_sha256(path: Path, expected: str) -> bool:
    expected = expected.removeprefix("sha256:").lower()
    if path.is_symlink() or len(expected) != 64 or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected


def song_completion_evidence(record: dict) -> dict:
    """Verify the positive, hash-bound proof required to deliver a song.

    This deliberately duplicates the final edge checks from the selector at
    the unattended-runner boundary.  A stale/globbed burned MP4 must not escape
    merely because an earlier selector process happened to leave it on disk.
    """
    failures: list[str] = []

    def is_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    job = record.get("source_context_job")
    if not isinstance(job, dict):
        job = {}
    boundary = job.get("song_boundary")
    if not isinstance(boundary, dict) or boundary.get("status") != "FULL_SONG_READY":
        boundary = {}
        failures.append("SONG_FULL_BOUNDARY_PROOF_MISSING")
    boundary_values = [boundary.get(key) for key in ("clip_start_ms", "first_lyric_start_ms", "last_lyric_end_ms", "clip_end_ms")]
    if boundary and not all(is_int(value) for value in boundary_values):
        failures.append("SONG_BOUNDARY_TIMELINE_MISSING")
    elif boundary and not (0 <= boundary_values[0] <= boundary_values[1] <= boundary_values[2] <= boundary_values[3]):
        failures.append("SONG_BOUNDARY_TIMELINE_INVALID")
    boundary_zero = boundary.get("nominal_lrc_zero_ms") if boundary else None
    if boundary and (
        not is_int(boundary_zero)
        or not all(is_int(value) for value in boundary_values)
        or not (boundary_values[0] <= boundary_zero <= boundary_values[1])
    ):
        failures.append("SONG_NOMINAL_LRC_ZERO_INVALID")

    alignment = job.get("lyrics_alignment")
    report: dict | None = None
    if not isinstance(alignment, dict) or alignment.get("status") != "READY":
        alignment = {}
        failures.append("SONG_LYRICS_ALIGNMENT_PROOF_MISSING")
    else:
        for field in ("provider", "model"):
            if not isinstance(alignment.get(field), str) or not str(alignment[field]).strip():
                failures.append(f"SONG_LYRICS_{field.upper()}_MISSING")
        source = alignment.get("source") or alignment.get("external_lrc")
        if not isinstance(source, str) or not source.strip():
            failures.append("SONG_EXTERNAL_LRC_SOURCE_MISSING")
        report_value = alignment.get("alignment_report_path")
        report_sha = alignment.get("alignment_report_sha256")
        if not isinstance(report_value, str) or not report_value:
            failures.append("SONG_ALIGNMENT_REPORT_MISSING")
        elif not isinstance(report_sha, str) or not _matches_sha256(Path(report_value), report_sha):
            failures.append("SONG_ALIGNMENT_REPORT_HASH_INVALID")
        else:
            try:
                loaded_report = json.loads(Path(report_value).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded_report = None
            if not isinstance(loaded_report, dict):
                failures.append("SONG_ALIGNMENT_REPORT_INVALID_JSON")
            else:
                report = loaded_report

    if report is not None:
        if report.get("schema_version") != "lyrics-alignment-report.v1":
            failures.append("SONG_ALIGNMENT_REPORT_SCHEMA_INVALID")
        if report.get("alignment_model") != "external_lrc_global_shift.v1":
            failures.append("SONG_ALIGNMENT_REPORT_MODEL_INVALID")
        if report.get("provider") != alignment.get("provider"):
            failures.append("SONG_ALIGNMENT_PROVIDER_MISMATCH")
        external_lrc = alignment.get("external_lrc")
        if not isinstance(external_lrc, str) or report.get("source_ref") != external_lrc:
            failures.append("SONG_ALIGNMENT_SOURCE_MISMATCH")
        offset_ms = alignment.get("offset_ms")
        if not is_int(offset_ms) or report.get("offset_ms") != offset_ms:
            failures.append("SONG_ALIGNMENT_OFFSET_MISMATCH")
        alignment_zero = alignment.get("nominal_lrc_zero_ms")
        report_zero = report.get("nominal_lrc_zero_ms")
        if not all(is_int(value) for value in (boundary_zero, alignment_zero, report_zero, offset_ms)):
            failures.append("SONG_NOMINAL_LRC_ZERO_INVALID")
        elif not (
            boundary_zero == alignment_zero == report_zero == offset_ms == report.get("offset_ms")
        ):
            failures.append("SONG_NOMINAL_LRC_ZERO_MISMATCH")
        if boundary.get("song_title") and report.get("song_title") != boundary.get("song_title"):
            failures.append("SONG_ALIGNMENT_TITLE_MISMATCH")
        if record.get("candidate_id") and report.get("candidate_id") != record.get("candidate_id"):
            failures.append("SONG_ALIGNMENT_CANDIDATE_MISMATCH")
        if boundary and (
            report.get("first_lyric_start_ms") != boundary.get("first_lyric_start_ms")
            or report.get("last_lyric_end_ms") != boundary.get("last_lyric_end_ms")
        ):
            failures.append("SONG_ALIGNMENT_BOUNDARY_MISMATCH")

        lyric_lines = report.get("lyric_lines")
        if not isinstance(lyric_lines, list) or len(lyric_lines) < 8:
            failures.append("SONG_ALIGNMENT_LYRIC_TIMELINE_INVALID")
            lyric_lines = []
        else:
            lyric_times = [line.get("lrc_time_ms") for line in lyric_lines if isinstance(line, dict)]
            lyric_texts_ok = all(isinstance(line, dict) and str(line.get("text") or "").strip() for line in lyric_lines)
            if (
                len(lyric_times) != len(lyric_lines)
                or not all(is_int(value) and value >= 0 for value in lyric_times)
                or lyric_times != sorted(lyric_times)
                or not lyric_texts_ok
            ):
                failures.append("SONG_ALIGNMENT_LYRIC_TIMELINE_INVALID")
        line_count = report.get("line_count")
        matched_count = report.get("matched_line_count")
        matched_ratio = report.get("matched_line_ratio")
        if (
            not is_int(line_count)
            or line_count != len(lyric_lines)
            or not is_int(matched_count)
            or matched_count < 0
            or matched_count > line_count
            or not isinstance(matched_ratio, (int, float))
            or isinstance(matched_ratio, bool)
            or float(matched_ratio) < 0.55
            or (line_count and matched_count / line_count < 0.55)
            or (line_count and float(matched_ratio) != round(matched_count / line_count, 4))
            or alignment.get("matched_line_ratio") != matched_ratio
        ):
            failures.append("SONG_ALIGNMENT_MATCH_EVIDENCE_INVALID")
        report_alignment = report.get("alignment")
        if not isinstance(report_alignment, list) or len(report_alignment) != line_count:
            failures.append("SONG_ALIGNMENT_MATCH_EVIDENCE_INVALID")
        elif sum(1 for entry in report_alignment if isinstance(entry, dict) and entry.get("matched_cue_id") is not None) != matched_count:
            failures.append("SONG_ALIGNMENT_MATCH_EVIDENCE_INVALID")

        is_audio_report = report.get("evidence_source") == "agy_audio_lrc"
        is_audio_model = str(alignment.get("model") or "").endswith("-agy-audio-lrc-global-shift-v1")
        if is_audio_report != is_audio_model:
            failures.append("SONG_ALIGNMENT_EVIDENCE_TYPE_MISMATCH")

        if is_audio_report:
            if (
                report.get("audio_alignment_provider") != "agy"
                or report.get("audio_alignment_model") != "Gemini 3.5 Flash (High)"
                or not str(alignment.get("model") or "").endswith("-agy-audio-lrc-global-shift-v1")
            ):
                failures.append("SONG_AUDIO_LRC_PROVIDER_INVALID")
            audio_artifacts = report.get("audio_alignment_artifacts")
            artifact_pairs = (
                ("source_path", "source_sha256"),
                ("lrc_path", "lrc_sha256"),
                ("prompt_path", "prompt_sha256"),
                ("raw_output_path", "raw_output_sha256"),
                ("run_manifest_path", "run_manifest_sha256"),
            )
            if not isinstance(audio_artifacts, dict):
                failures.append("SONG_AUDIO_LRC_ARTIFACTS_INVALID")
            else:
                for path_key, sha_key in artifact_pairs:
                    path_value = audio_artifacts.get(path_key)
                    sha_value = audio_artifacts.get(sha_key)
                    if (
                        not isinstance(path_value, str)
                        or not isinstance(sha_value, str)
                        or not _matches_sha256(Path(path_value), sha_value)
                    ):
                        failures.append("SONG_AUDIO_LRC_ARTIFACTS_INVALID")
                manifest_value = audio_artifacts.get("run_manifest_path")
                try:
                    audio_manifest = json.loads(Path(str(manifest_value)).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    audio_manifest = None
                if not isinstance(audio_manifest, dict) or (
                    audio_manifest.get("schema_version") != "agy-audio-lrc-run.v1"
                    or audio_manifest.get("candidate_id") != record.get("candidate_id")
                    or audio_manifest.get("provider") != "agy"
                    or audio_manifest.get("model") != "Gemini 3.5 Flash (High)"
                    or audio_manifest.get("agy_rc") != 0
                    or audio_manifest.get("provider_fallback_used") is not False
                    or audio_manifest.get("sandbox") is not True
                ):
                    failures.append("SONG_AUDIO_LRC_MANIFEST_INVALID")
                elif not isinstance(audio_manifest.get("artifacts"), dict) or any(
                    audio_manifest["artifacts"].get(manifest_key) != audio_artifacts.get(report_key)
                    for manifest_key, report_key in (
                        ("source_path", "source_path"),
                        ("source_sha256", "source_sha256"),
                        ("source_duration_ms", "source_duration_ms"),
                        ("lrc_path", "lrc_path"),
                        ("lrc_sha256", "lrc_sha256"),
                        ("prompt_path", "prompt_path"),
                        ("prompt_sha256", "prompt_sha256"),
                        ("output_path", "raw_output_path"),
                        ("output_sha256", "raw_output_sha256"),
                    )
                ):
                    failures.append("SONG_AUDIO_LRC_MANIFEST_BINDING_INVALID")
                raw_value = audio_artifacts.get("raw_output_path")
                try:
                    raw_observation = json.loads(Path(str(raw_value)).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raw_observation = None
                raw_record = raw_observation.get("record") if isinstance(raw_observation, dict) else None
                raw_rows = raw_observation.get("observations") if isinstance(raw_observation, dict) else None
                if (
                    not isinstance(raw_observation, dict)
                    or raw_observation.get("schema_version") != "agy-audio-lrc-observation.v1"
                    or not isinstance(raw_record, dict)
                    or raw_record.get("candidate_id") != record.get("candidate_id")
                    or raw_record.get("source_sha256") != audio_artifacts.get("source_sha256")
                    or raw_record.get("lrc_sha256") != audio_artifacts.get("lrc_sha256")
                    or raw_record.get("source_duration_ms") != audio_artifacts.get("source_duration_ms")
                    or not isinstance(raw_rows, list)
                ):
                    failures.append("SONG_AUDIO_LRC_RAW_OBSERVATION_INVALID")
            if isinstance(report_alignment, list) and isinstance(lyric_lines, list):
                ids: list[str] = []
                starts: list[int] = []
                residuals: list[int] = []
                audio_rows_ok = len(report_alignment) == len(lyric_lines) == matched_count == line_count
                for row, lyric in zip(report_alignment, lyric_lines):
                    if not isinstance(row, dict) or not isinstance(lyric, dict):
                        audio_rows_ok = False
                        break
                    cue_id = row.get("matched_cue_id")
                    cue_start = row.get("cue_start_ms")
                    cue_end = row.get("cue_end_ms")
                    if (
                        row.get("evidence_source") != "agy_audio_lrc"
                        or not isinstance(cue_id, str)
                        or not cue_id.startswith("agy-audio:")
                        or not is_int(cue_start)
                        or not is_int(cue_end)
                        or not 0 <= cue_start < cue_end
                        or row.get("lrc_time_ms") != lyric.get("lrc_time_ms")
                        or row.get("lrc_text") != lyric.get("text")
                    ):
                        audio_rows_ok = False
                        break
                    ids.append(cue_id)
                    starts.append(cue_start)
                    residuals.append(cue_start - lyric["lrc_time_ms"])
                if (
                    not audio_rows_ok
                    or len(set(ids)) != len(ids)
                    or starts != sorted(starts)
                    or not is_int(offset_ms)
                    or any(abs(value - offset_ms) > 1_500 for value in residuals)
                    or (starts and starts[0] != report.get("first_lyric_start_ms"))
                    or (
                        report_alignment
                        and isinstance(report_alignment[-1], dict)
                        and report_alignment[-1].get("cue_end_ms") != report.get("last_lyric_end_ms")
                    )
                ):
                    failures.append("SONG_AUDIO_LRC_OBSERVATION_INVALID")
                if isinstance(audio_artifacts, dict) and isinstance(raw_rows, list):
                    raw_rows_match = len(raw_rows) == len(report_alignment) == len(lyric_lines)
                    if raw_rows_match:
                        for index, (raw_row, proof_row, lyric) in enumerate(zip(raw_rows, report_alignment, lyric_lines)):
                            if (
                                not isinstance(raw_row, dict)
                                or not isinstance(proof_row, dict)
                                or not isinstance(lyric, dict)
                                or raw_row.get("lrc_index") != index
                                or raw_row.get("lrc_time_ms") != lyric.get("lrc_time_ms")
                                or raw_row.get("text") != lyric.get("text")
                                or raw_row.get("heard") is not True
                                or raw_row.get("live_start_ms") != proof_row.get("cue_start_ms")
                                or raw_row.get("live_end_ms") != proof_row.get("cue_end_ms")
                                or not isinstance(raw_row.get("confidence"), (int, float))
                                or isinstance(raw_row.get("confidence"), bool)
                                or round(float(raw_row["confidence"]), 4) != proof_row.get("match_ratio")
                            ):
                                raw_rows_match = False
                                break
                    if not raw_rows_match:
                        failures.append("SONG_AUDIO_LRC_RAW_REPORT_MISMATCH")

    recut = record.get("materialized_recut")
    if not isinstance(recut, dict) or recut.get("status") != "MATERIALIZED":
        recut = {}
        failures.append("SONG_MATERIALIZED_RECUT_MISSING")
    elif recut.get("subtitle_source") != "external_lrc_global_shift":
        failures.append("SONG_EXTERNAL_LRC_SUBTITLE_NOT_MATERIALIZED")
    else:
        recut_reasons = recut.get("reason_codes")
        if not isinstance(recut_reasons, list):
            recut_reasons = []
        if recut.get("accurate_rerender_used") is not True:
            failures.append("SONG_ACCURATE_RERENDER_REQUIRED")
        if "FFMPEG_ACCURATE_RECUT_FAILED" in recut_reasons:
            failures.append("SONG_ACCURATE_RERENDER_FAILED")
        render_qa = recut.get("render_qa")
        render_evidence = render_qa.get("evidence") if isinstance(render_qa, dict) else None
        actual_error = render_evidence.get("actual_cut_error_ms") if isinstance(render_evidence, dict) else None
        threshold = render_evidence.get("threshold_ms") if isinstance(render_evidence, dict) else None
        if (
            not isinstance(render_qa, dict)
            or render_qa.get("pass") is not True
            or not isinstance(actual_error, (int, float))
            or isinstance(actual_error, bool)
            or not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or actual_error > threshold
        ):
            failures.append("SONG_RENDER_QA_FAILED")

    if recut and boundary:
        if recut.get("start_ms") != boundary.get("clip_start_ms") or recut.get("end_ms") != boundary.get("clip_end_ms"):
            failures.append("SONG_RECUT_BOUNDARY_MISMATCH")
        if recut.get("lyric_offset_ms") != alignment.get("offset_ms"):
            failures.append("SONG_RECUT_LYRIC_OFFSET_MISMATCH")

    artifact_hashes = recut.get("artifact_hashes") if recut else None
    subtitle_path = recut.get("subtitle_path") if recut else None
    subtitle_sha = artifact_hashes.get("subtitle_sha256") if isinstance(artifact_hashes, dict) else None
    if not isinstance(subtitle_path, str) or not isinstance(subtitle_sha, str) or not _matches_sha256(Path(subtitle_path), subtitle_sha):
        failures.append("SONG_SUBTITLE_ARTIFACT_HASH_INVALID")

    burned = recut.get("burned_preview") if recut else None
    burned_sha = None
    if not isinstance(burned, dict) or burned.get("status") != "BURNED" or not burned.get("path"):
        failures.append("SONG_BURNED_PREVIEW_MISSING")
    else:
        burned_sha = burned.get("burned_sha256")
        if isinstance(artifact_hashes, dict):
            burned_sha = artifact_hashes.get("burned_video_sha256") or burned_sha
        if not isinstance(burned_sha, str) or not _matches_sha256(Path(str(burned["path"])), burned_sha):
            failures.append("SONG_BURNED_PREVIEW_HASH_INVALID")

    failures = list(dict.fromkeys(failures))
    return {
        "ready": not failures,
        "reason_codes": failures,
        "song_boundary_status": boundary.get("status") if isinstance(boundary, dict) else None,
        "lyrics_alignment_status": alignment.get("status") if alignment else None,
        "lyrics_provider": alignment.get("provider") if alignment else None,
        "external_lrc": (alignment.get("external_lrc") or alignment.get("source")) if alignment else None,
        "alignment_report_path": alignment.get("alignment_report_path") if alignment else None,
        "alignment_report_sha256": alignment.get("alignment_report_sha256") if alignment else None,
        "subtitle_source": recut.get("subtitle_source") if recut else None,
        "burned_preview_path": burned.get("path") if isinstance(burned, dict) else None,
        "burned_preview_sha256": burned_sha,
        "matched_line_ratio": report.get("matched_line_ratio") if report is not None else None,
        "lyric_offset_ms": alignment.get("offset_ms") if alignment else None,
    }


def verified_song_fallback_title(song_title: str | None, hook: str | None) -> str | None:
    """Build a hook-bearing fallback when semantic publish staging was advisory-blocked."""
    song_title = str(song_title or "").strip()
    if not song_title:
        return None
    hook = str(hook or "").strip()
    hook = re.split(r"[，。！？；]", hook, maxsplit=1)[0].strip()
    if hook and hook != "确定性歌检测补充(演唱段)":
        return f"【李豆沙】豆沙歌，《{song_title}》｜{hook[:16]}"
    return f"【李豆沙】豆沙歌，直播间唱《{song_title}》"


def produce_song(date: str, item: dict) -> dict:
    """Song lane (Ivan 2026-07-05): cut a tight window around the sung anchor,
    run the canonical song pipeline (netease LRC global-shift alignment + strict
    completeness gate, fail-closed) and deliver only gate-passing results.

    Anchor-bleed guard (Ivan 2026-07-06): a recall song-anchor can begin dozens
    of seconds inside the PRECEDING talk (《今天也过得很愉快》's anchor led with the
    pig-nose banter), so the window leads with talk and the in-window recall
    latches onto that talk (window_classified_song=False → AUTO_RECUT, no song
    delivered).  When the first attempt misses the song, retry ONCE on the
    danmaku-dense core of the anchor.  Self-correcting: it only fires when the
    song was missed, so a clean song window (嘉宾) runs exactly once as before."""
    seg_dur_ms = item["seg_dur_ms"]
    segment = Path(item["segment_path"])
    cid = item["cid"]
    out_dir = BASE / "out" / date / cid
    out_dir.mkdir(parents=True, exist_ok=True)

    def window_for(a0: int, a1: int) -> tuple[int, int]:
        s = max(0, a0 - SONG_WINDOW_PRE_MS)
        e = min(seg_dur_ms, a1 + SONG_WINDOW_POST_MS) if seg_dur_ms else a1 + SONG_WINDOW_POST_MS
        return s, e

    def attempt(start: int, end: int, tag: str) -> dict:
        log(f"song lane {cid}{tag}: window {start // 1000}-{end // 1000}s (danmaku x{item.get('danmaku', 0)}) from {segment.name}")
        result = {"candidate_id": cid, "segment": segment.name, "start_ms": start, "end_ms": end,
                  "danmaku": item.get("danmaku", 0), "hook": item.get("hook", ""), "preview": item.get("preview", "")[:60], "rc": -1}
        window_mp4 = out_dir / f"{cid}{tag}_source.mp4"
        if not window_mp4.is_file():
            cut = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", f"{start / 1000:.3f}", "-to", f"{end / 1000:.3f}", "-i", str(segment),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
                 str(window_mp4)],
                check=False, capture_output=True, text=True, timeout=3600,
            )
            if cut.returncode != 0 or not window_mp4.is_file():
                result["error"] = f"window cut failed: {cut.stderr[-200:]}"
                result["status"] = "failed"
                return result

        src_srt = BASE / "cache" / date / f"{segment.stem}.bcut.srt"
        window_srt = out_dir / f"{cid}{tag}_source.srt"
        if slice_srt(src_srt, start, end, window_srt) == 0:
            result["error"] = "empty window srt"
            result["status"] = "failed"
            return result

        # NOTE: segment danmaku XML is segment-relative — do not pass it to the
        # selector (it would misalign against the window-relative video); LRC is
        # the subtitle authority for songs anyway.
        selector_dir = fresh_song_selector_dir(out_dir, tag)
        log_path = BASE / "logs" / f"{date}_{cid}.log"
        with open(log_path, "a", encoding="utf-8") as sink:
            selector_env = child_env()
            selector_env["AGY_MODEL"] = os.environ.get("SONG_AGY_MODEL", "Gemini 3.5 Flash (High)")
            selector_command = [
                 sys.executable, str(REPO_ROOT / "scripts" / "run_full_session_selector_cpa_shadow.py"),
                 "--source-video", str(window_mp4), "--source-srt", str(window_srt),
                 "--output-dir", str(selector_dir), "--max-candidates", "1",
                 "--source-duration-ms", str(max(0, end - start)),
                 "--cpa-command", cpa_qa_cmd(),
                 "--semantic-recall-llm-command", CPA_CMD_DEEP,
                 "--song-hint-llm-command", CPA_CMD_STANDARD,
                 "--title-llm-command", CPA_CMD_TITLE,
                 "--cover-art-direction-llm-command", CPA_CMD_STANDARD,
                 "--lrc-provider", "auto", "--burn-preview", "--publish-staging",
            ]
            known_song_query = str(item.get("preview") or "").strip()
            # Search the quoted song title first.  Passing the entire prose
            # preview ("下播前演唱 yonige《芽吹くとき》") made LRCLIB return no
            # rows even though the exact title returns the canonical timed LRC.
            quoted_titles = re.findall(r"[《「『]([^》」』]{1,80})[》」』]", known_song_query)
            for query in [*quoted_titles[:1], known_song_query]:
                if query:
                    selector_command.extend(["--song-lrc-query", query])
            # Every invocation already came from the upstream song lane, which
            # owns this anchor.  Re-asking a nondeterministic semantic LLM to
            # decide whether the same window is a song made Japanese garbage
            # ASR randomly produce NO_FULL_SESSION_CANDIDATES.  Keep the anchor
            # for tight/core/full attempts; only the full attempt may escalate
            # to expensive audio+LRC proof.  Delivery still requires the
            # independent positive full-song proof below.
            selector_command.extend(
                [
                    "--seed-song-candidate-id",
                    f"seededsong_{max(0, anchor_start - start)}_{min(end - start, anchor_end - start)}",
                    "--seed-song-anchor-start-ms",
                    str(max(0, anchor_start - start)),
                    "--seed-song-anchor-end-ms",
                    str(min(end - start, anchor_end - start)),
                ]
            )
            if tag == "_full":
                selector_command.append("--agy-audio-lrc-align")
            completed = subprocess.run(
                selector_command,
                check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=5400,
                cwd=str(REPO_ROOT), env=selector_env,
            )
        result["rc"] = completed.returncode
        result["log"] = str(log_path)
        decision = None
        reasons: list = []
        is_song = False
        summary_record: dict = {}
        summary_path = selector_dir / "summary.json"
        if summary_path.is_file():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                for entry in summary.get("records", []) if isinstance(summary, dict) else []:
                    if not isinstance(entry, dict):
                        continue
                    summary_record = entry
                    decision = entry.get("decision_action") or decision
                    reasons = list(entry.get("reason_codes") or reasons)
                    is_song = is_song or record_is_song(entry)
            except ValueError:
                pass
        result["decision"] = decision
        completion = song_completion_evidence(summary_record)
        result["reason_codes"] = list(dict.fromkeys([*reasons, *completion["reason_codes"]]))
        result["window_classified_song"] = is_song
        result["song_completion_evidence"] = completion
        artifacts = song_delivery_artifacts(summary_record)
        if artifacts.get("title"):
            result["title"] = artifacts["title"]
        if not result.get("title"):  # pre-gate-era summaries carry no staging title
            for publish in sorted(selector_dir.glob("**/replacement_recuts/*.publish.json")):
                try:
                    result["title"] = json.loads(publish.read_text(encoding="utf-8")).get("title")
                except (OSError, ValueError):
                    pass
        # Burned video: only the summary-recorded, hash-bound artifact is
        # eligible.  Old selector debris must never inherit a newer proof.
        burned = Path(artifacts["video_path"]) if artifacts.get("video_path") else None
        if burned is not None and artifacts.get("video_sha256") and not _matches_sha256(burned, artifacts["video_sha256"]):
            log(f"song lane {cid}: burned video hash drift since materialization — refusing stale artifact")
            burned = None
        cover = Path(artifacts["cover_path"]) if artifacts.get("cover_path") else None
        cover_ok = bool(cover is not None and cover.is_file() and (
            not artifacts.get("cover_sha256") or _matches_sha256(cover, artifacts["cover_sha256"])))
        if not cover_ok:
            cover = None
        result["song_complete"] = completion["ready"] is True
        result["lyrics_alignment_ready"] = completion["lyrics_alignment_status"] == "READY"
        if result["song_complete"] and not result.get("title"):
            boundary = (summary_record.get("source_context_job") or {}).get("song_boundary") or {}
            result["title"] = verified_song_fallback_title(boundary.get("song_title"), item.get("hook"))
        if "cover_release_gate_satisfied" in artifacts:
            result["cover_release_gate_satisfied"] = artifacts["cover_release_gate_satisfied"]
        if burned is not None and burned.is_file() and song_delivery_ok(
            completed.returncode, is_song, reasons, completion
        ):
            delivery = REPO_ROOT / "lidousha" / date
            delivery.mkdir(parents=True, exist_ok=True)
            import shutil

            name = safe_name("歌切_" + (result.get("title") or item.get("hook") or ""), f"歌切_{cid}")
            shutil.copy2(burned, delivery / f"{name}.mp4")
            if cover_ok and cover is not None:
                shutil.copy2(cover, delivery / f"{name}.cover.png")
            delivered_sidecars: dict[str, str] = {}
            subtitle = Path(artifacts["subtitle_path"]) if artifacts.get("subtitle_path") else None
            if subtitle is not None and subtitle.is_file() and artifacts.get("subtitle_sha256") and _matches_sha256(
                subtitle, artifacts["subtitle_sha256"]
            ):
                delivered_srt = delivery / f"{name}.srt"
                shutil.copy2(subtitle, delivered_srt)
                delivered_sidecars["subtitle"] = str(delivered_srt)
            alignment_report = completion.get("alignment_report_path")
            alignment_sha = completion.get("alignment_report_sha256")
            if isinstance(alignment_report, str) and isinstance(alignment_sha, str):
                alignment_path = Path(alignment_report)
                if _matches_sha256(alignment_path, alignment_sha):
                    delivered_alignment = delivery / f"{name}.lyrics-alignment-report.json"
                    shutil.copy2(alignment_path, delivered_alignment)
                    delivered_sidecars["lyrics_alignment_report"] = str(delivered_alignment)
            recut_manifest = Path(artifacts["recut_manifest_path"]) if artifacts.get("recut_manifest_path") else None
            if recut_manifest is not None and recut_manifest.is_file():
                delivered_manifest = delivery / f"{name}.recut.manifest.json"
                shutil.copy2(recut_manifest, delivered_manifest)
                delivered_sidecars["recut_manifest"] = str(delivered_manifest)
            result["delivered"] = str(delivery / f"{name}.mp4")
            result["delivered_sidecars"] = delivered_sidecars
        result["status"] = song_status(completed.returncode, bool(result.get("delivered")))
        return result

    anchor_start, anchor_end = item["anchor_start_ms"], item["anchor_end_ms"]
    src_srt = BASE / "cache" / date / f"{segment.stem}.bcut.srt"
    result = attempt(*window_for(anchor_start, anchor_end), "")
    if result.get("window_classified_song") and not result.get("song_complete"):
        full_start, full_end = song_proof_retry_window(anchor_start, anchor_end, seg_dur_ms)
        if full_start < result.get("start_ms", full_start) or full_end > result.get("end_ms", full_end):
            log(
                f"song lane {cid}: song identified but positive LRC boundary proof is missing — "
                f"retrying with original-source context {full_start // 1000}-{full_end // 1000}s"
            )
            proof_retry = attempt(full_start, full_end, "_full")
            if proof_retry.get("song_complete"):
                result = {**proof_retry, "retried_full_source": True}
            else:
                result["full_source_retry"] = {
                    key: proof_retry.get(key)
                    for key in (
                        "start_ms",
                        "end_ms",
                        "rc",
                        "status",
                        "reason_codes",
                        "window_classified_song",
                        "song_complete",
                        "song_completion_evidence",
                    )
                }
    if not result.get("window_classified_song") and not result.get("delivered"):
        d0, d1 = _song_core_span(src_srt, anchor_start, anchor_end)
        if d0 >= anchor_start + SONG_ANCHOR_TRIM_MIN_MS or d1 <= anchor_end - SONG_ANCHOR_TRIM_MIN_MS:
            log(f"song lane {cid}: window classified as talk — retrying on sung core {d0 // 1000}-{d1 // 1000}s")
            retry = attempt(*window_for(d0, d1), "_core")
            if retry.get("window_classified_song") or retry.get("delivered"):
                result = {**retry, "retried_core": True}
    return result


def delivered_paths(date: str, rec: dict) -> tuple[Path, Path] | None:
    """(mp4, cover) delivery paths for a pick/song record, or None if the mp4
    was never delivered (failed/gated records have nothing to repair)."""
    if rec.get("delivered"):  # song lane records the delivered path explicitly
        mp4 = Path(rec["delivered"])
    else:
        name = safe_name(rec.get("hook", ""), rec.get("candidate_id", ""))
        mp4 = REPO_ROOT / "lidousha" / date / f"{name}.mp4"
    if not mp4.is_file():
        return None
    return mp4, mp4.with_suffix(".cover.png")


def image_lane_down(log_tail: str) -> bool:
    """CPA image-lane outage signatures (channel unrouted / gateway 5xx).  These
    are operator-side and self-resolve — they must NOT burn bounded repair
    attempts, mirroring the chat lane's paused_cpa_down patience."""
    return "可用渠道不存在" in log_tail or "HTTP 50" in log_tail


def cover_ref_for(date: str, cid: str) -> Path | None:
    """The producer's CLEAN reference frame (pre-burn).  Preferred over frame
    grabs from the delivered mp4, whose burned subtitles would leak into the
    gpt-image-2 identity reference."""
    return next(iter(sorted((BASE / "out" / date / cid).glob("**/cover_refs/*.cover-ref.png"))), None)


def cover_repair_needed(date: str, rec: dict) -> bool:
    # Delivered = passed the delivery gate (for songs: is-song + complete, see
    # song_delivery_ok) — every delivered clip deserves a cover, regardless of
    # what the ADVISORY semantic judge said (Ivan 2026-07-10).  Blocked/failed
    # records have no delivery and never get covers.
    delivered = rec.get("status") in DELIVERED_TALK_STATUSES or bool(rec.get("delivered"))
    if not delivered or not rec.get("title"):
        return False
    if rec.get("cover_repair_attempts", 0) >= COVER_REPAIR_MAX_ATTEMPTS:
        return False
    paths = delivered_paths(date, rec)
    return paths is not None and not paths[1].is_file()


def repair_covers(date: str, state: dict) -> None:
    """Phase D: delivered clips whose REAL-AI cover was blocked (CPA image lane
    hiccups: gateway 400s, provider outages) get a bounded cover-only retry —
    the mp4 is already good, nothing is re-produced.  One attempt per record
    per tick; permanently blocked covers stay loud in the review summary."""
    todo = [r for r in state.get("picks", []) + state.get("songs", []) if cover_repair_needed(date, r)]
    if not todo:
        return
    for rec in todo:
        mp4, cover = delivered_paths(date, rec)
        rec["cover_repair_attempts"] = rec.get("cover_repair_attempts", 0) + 1
        cid = rec.get("candidate_id", "?")
        log(f"cover repair {cid} (attempt {rec['cover_repair_attempts']}/{COVER_REPAIR_MAX_ATTEMPTS})")
        log_path = BASE / "logs" / f"{date}_{cid}_cover.log"
        ref = cover_ref_for(date, cid)
        src_args = ["--ref", str(ref)] if ref else ["--media", str(mp4)]
        try:
            with open(log_path, "a", encoding="utf-8") as sink:
                completed = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "scripts" / "regenerate_lidousha_cover.py"),
                     "--title", str(rec["title"]), *src_args,
                     "--candidate-id", str(cid), "--out", str(cover)],
                    check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=1200,
                    cwd=str(REPO_ROOT), env=child_env(),
                )
            rc = completed.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        if rc == 0 and cover.is_file():
            rec["cover_status"] = "REPAIRED_AI_COVER"
            log(f"cover repaired → {cover.name}")
        else:
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            except OSError:
                tail = ""
            if image_lane_down(tail):
                rec["cover_repair_attempts"] -= 1  # lane outage, not this pick's failure
                log("cover repair: CPA image lane down — deferring ALL repairs to a later tick")
                write_state(date, state)
                return
            if rec["cover_repair_attempts"] >= COVER_REPAIR_MAX_ATTEMPTS:
                rec["cover_status"] = f"{rec.get('cover_status') or 'BLOCKED_AI_COVER_REQUIRED'}(repair_failed_x{rec['cover_repair_attempts']})"
                log(f"cover repair failed {rec['cover_repair_attempts']}x — left for human review (see {log_path.name})")
        write_state(date, state)


def write_reports(date: str, state: dict) -> None:
    delivery = REPO_ROOT / "lidousha" / date
    delivery.mkdir(parents=True, exist_ok=True)

    def fmt_dur(pick: dict) -> str:
        secs = max(0, (pick.get("end_ms", 0) - pick.get("start_ms", 0)) // 1000)
        return f"{secs // 60}:{secs % 60:02d}"

    picks = state.get("picks", [])
    songs = state.get("songs", [])
    delivered_talk = sum(1 for p in picks if p.get("status") in DELIVERED_TALK_STATUSES)
    quarantined = sum(1 for p in picks if p.get("status") == "quarantine")
    delivered_songs = sum(1 for s in songs if s.get("delivered"))
    blocked_songs = sum(1 for s in songs if s.get("status") == "blocked")
    lines = [
        f"# {date} 无人值守自动切片批次",
        "",
        f"- 状态: **{state.get('status')}**  (runner v4; 上传永远关闭，全部成品仅供人工审查)",
        f"- 交付实况: 谈话 **{delivered_talk} 交付**（其中 {quarantined} 条 ⚠quarantine 需人工看边界）/ "
        f"歌 **{delivered_songs} 交付** · {blocked_songs} 被完整性门拦截 · 共尝试 {len(songs)}",
        f"- 段: 完成 {len(state.get('segments_done', []))} / 死段 {len(state.get('segments_dead', {}))} / 待产出 talk {len(state.get('pending_talk', []))} + song {len(state.get('pending_song', []))}",
        "",
        "## 谈话成品（审查要点：标题、选片理由、边界收束）",
        "",
        "| 成品 | 时长 | 标题 | 选片理由(hook) | 信心 | 收束句 | 边界 | 封面 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for pick in state.get("picks", []):
        s = pick.get("summary") or {}
        if pick.get("status") in ("ok", "review_ready"):
            status_mark = ""
        elif pick.get("status") == "quarantine":
            status_mark = f" ⚠quarantine[{','.join(pick.get('red_flags') or [])}]"
        else:
            status_mark = f" ⚠{pick.get('status')}"
        lines.append(
            f"| `{safe_name(pick.get('hook',''), pick.get('candidate_id','?'))}`{status_mark} "
            f"| {fmt_dur(pick)} "
            f"| {pick.get('title') or '(未生成)'} "
            f"| {pick.get('hook') or '(兜底lane无理由)'} "
            f"| {pick.get('confidence') if pick.get('confidence') is not None else '—'} "
            f"| {s.get('closure_sentence') or '?'} "
            f"| {s.get('boundary_verdict') or '?'} "
            f"| {pick.get('cover_status') or s.get('cover_status') or '?'} |"
        )
    lines += ["", f"## 歌切（至多 {MAX_SONGS_PER_DATE} 个、按弹幕量排序；没唱完整的歌不切(SONG_PARTIAL 不交付)；语义判定仅作参考不拦交付；被拦不占配额、备份自动回填）", ""]
    if songs:
        lines += ["| 歌 | 弹幕 | 门判定 | 原因码 | 标题 | 交付 |", "|---|---|---|---|---|---|"]
        for song in songs:
            lines.append(
                f"| `{song.get('candidate_id')}` | x{song.get('danmaku', 0)} "
                f"| {song.get('decision') or '?'} | {','.join(song.get('reason_codes') or []) or '—'} "
                f"| {song.get('title') or '—'} "
                f"| {'✓ ' + Path(song['delivered']).name if song.get('delivered') else '未过门不交付'} |"
            )
    else:
        lines.append("(本场未检出/未产出歌切)")
    backlog = state.get("song_backlog", [])
    if backlog:
        def fmt_backlog(b) -> str:
            if not isinstance(b, dict):
                return str(b)  # legacy pre-v4 string entries
            return (
                f"{Path(b['segment_path']).name} {b['anchor_start_ms'] // 1000}-{b['anchor_end_ms'] // 1000}s "
                f"弹幕x{b.get('danmaku', 0)}: {b.get('hook') or b.get('preview', '')[:40]}"
            )
        lines += ["", "## 歌切候选备份（按弹幕排序；门拦截后自动回填的来源）", ""] + [f"- {fmt_backlog(b)}" for b in backlog]
    not_selected = state.get("not_selected", [])
    if not_selected:
        lines += ["", "## 落选谈话候选（供复核选片是否漏才）", ""] + [f"- {n}" for n in not_selected]
    dead = state.get("segments_dead", {})
    if dead:
        lines += ["", "## 死段（不再重试）", ""] + [f"- {k}: {v}" for k, v in dead.items()]
    if state.get("status") == "paused_cpa_down":
        lines += ["", "> ⚠ CPA 链路不可用，批次已暂停；cron 每 10 分钟自动重试，恢复后从断点续产。"]
    if state.get("status") == "no_delivery":
        lines += ["", "> ⚠ 本场 0 条交付（候选被门拦截/失败/耗尽）。这不是成功状态，需人工过目落选与拦截原因。"]
    (delivery / "AUTOSLICE_SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = BASE / "reports" / "latest.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f"# autoslice runner 最新状态\n\n- 时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        f"- 日期: {date}  状态: {state.get('status')}\n"
        f"- 谈话: {delivered_talk} 交付(含 {quarantined} quarantine) / {len(picks)} 尝试 (pending {len(state.get('pending_talk', []))})\n"
        f"- 歌切: {delivered_songs} 交付 / {blocked_songs} 门拦 / {len(songs)} 尝试 (pending {len(state.get('pending_song', []))})\n"
        f"- 交付: {REPO_ROOT}/lidousha/{date}/ (Mac launchd 拉取)\n",
        encoding="utf-8",
    )


def discover_segments(date: str, state: dict) -> None:
    """Phase A: transcribe + recall new segments into pending queues."""
    done = set(state.setdefault("segments_done", []))
    dead = state.setdefault("segments_dead", {})
    attempts = state.setdefault("bcut_attempts", {})
    pending_talk = state.setdefault("pending_talk", [])
    pending_song = state.setdefault("pending_song", [])

    for segment in list_segments(date):
        stem = segment.stem
        if stem in done or stem in dead:
            continue
        try:
            size = segment.stat().st_size
        except OSError:
            continue
        if size < MIN_SEGMENT_BYTES:
            dead[stem] = f"stub_too_small({size}B)"
            log(f"segment {segment.name}: dead stub ({size}B), skipping forever")
            continue
        srt = bcut_transcribe(segment, date)
        if srt is None:
            attempts[stem] = attempts.get(stem, 0) + 1
            if attempts[stem] >= BCUT_MAX_ATTEMPTS:
                dead[stem] = f"bcut_failed_x{attempts[stem]}"
                log(f"segment {segment.name}: BCUT failed {attempts[stem]}x → dead")
            continue
        xml = find_danmaku_xml(segment)
        candidates, lane, extras = recall_candidates(srt, danmaku_hints(xml))
        seg_dur = ffprobe_ms(segment)
        log(f"{segment.name}: {len(candidates)} candidate(s) via {lane}")
        seg_tag = re.sub(r"\D", "", stem)[-6:]
        for cand in candidates:
            meta = extras.get(cand.anchor.candidate_id, {})
            base_item = {
                "segment_path": str(segment),
                "seg_dur_ms": seg_dur,
                "xml": str(xml) if xml else None,
                "hook": meta.get("hook", ""),
                "confidence": meta.get("confidence"),
                "lane": lane,
                "preview": cand.text_preview[:80],
            }
            if getattr(cand, "content_type_hint", "talk") == "song":
                a0, a1 = int(cand.anchor.anchor_start_ms), int(cand.anchor.anchor_end_ms)
                pending_song.append({
                    **base_item,
                    "cid": f"song_{seg_tag}_{a0 // 1000}",
                    "anchor_start_ms": a0,
                    "anchor_end_ms": a1,
                    "danmaku": danmaku_count_in(str(xml) if xml else None, a0, a1),
                })
            else:
                b = cand.boundary
                s0 = max(0, int(b.resolved_start_ms))
                s1 = min(seg_dur, int(b.resolved_end_ms)) if seg_dur else int(b.resolved_end_ms)
                pending_talk.append({
                    **base_item,
                    "cid": f"auto_{seg_tag}_{s0 // 1000}_{s1 // 1000}",
                    "start_ms": s0,
                    "end_ms": s1,
                })
        done.add(stem)
    state["segments_done"] = sorted(done)


def session_sealed(date: str, state: dict) -> bool:
    """The date's recordings are STABLE: same segment inventory (names+sizes)
    as the previous tick, with at least one segment.  Selecting before seal
    hands the early segments the whole quota (2026-07-09 audit: a conf=0.94
    late-arriving candidate lost to five earlier 0.85-0.90 ones).  Costs one
    extra tick (~10 min) of latency after stream end; also absorbs the
    recorder's final flush.  Mutates state['seg_snapshot'] for the next tick."""
    snapshot: dict[str, int] = {}
    for segment in list_segments(date):
        try:
            snapshot[segment.stem] = segment.stat().st_size
        except OSError:
            return False  # flaky source read — never seal on a lie
    prev = state.get("seg_snapshot")
    state["seg_snapshot"] = snapshot
    return bool(snapshot) and prev == snapshot


def song_delivery_budget(state: dict) -> int:
    """Remaining song DELIVERIES wanted.  Only delivered songs consume the
    per-date budget — a gate-BLOCKED attempt must not eat a slot (2026-07-09
    audit: two BLOCKs consumed both slots and the date still read 'done')."""
    delivered = sum(1 for s in state.get("songs", []) if s.get("delivered"))
    return max(0, MAX_SONGS_PER_DATE - delivered)


def refill_songs(state: dict) -> None:
    """Top up pending_song from the structured backlog, danmaku-desc, honoring
    both the delivery budget and the hard per-date attempt cap.  Legacy string
    backlog entries (pre-v4 states) stay for the report but cannot backfill."""
    backlog = state.setdefault("song_backlog", [])
    pool = state.get("pending_song", []) + [b for b in backlog if isinstance(b, dict)]
    legacy = [b for b in backlog if not isinstance(b, dict)]
    pool.sort(key=lambda x: (-(x.get("danmaku") or 0), -(x["anchor_end_ms"] - x["anchor_start_ms"])))
    attempts_left = max(0, SONG_ATTEMPT_CAP - len(state.get("songs", [])))
    allowed = min(song_delivery_budget(state), attempts_left)
    state["pending_song"] = pool[:allowed]
    state["song_backlog"] = pool[allowed:] + legacy


def prioritize(state: dict) -> None:
    """Phase B: GLOBAL talk ranking by recall confidence (the metric asset is
    embedded in the recall prompt, so confidence carries its hard tiers), with
    a soft per-segment diversity cap that yields when slots would go unfilled.
    Replaces the segment round-robin that let five early candidates claim the
    whole quota regardless of score.  Songs: top danmaku, budget = deliveries."""
    pending_talk = state.get("pending_talk", [])
    produced = sum(1 for p in state.get("picks", []) if p.get("status") in DELIVERED_TALK_STATUSES)
    slots = max(0, MAX_TALK_PICKS - produced)
    ranked = sorted(pending_talk, key=lambda x: -(x.get("confidence") or 0.0))
    keep: list[dict] = []
    deferred: list[dict] = []
    per_seg: dict[str, int] = {}
    for item in ranked:
        seg = item["segment_path"]
        if len(keep) < slots and per_seg.get(seg, 0) < TALK_PER_SEGMENT_CAP:
            keep.append(item)
            per_seg[seg] = per_seg.get(seg, 0) + 1
        else:
            deferred.append(item)
    # The diversity cap is SOFT: refill unused slots from the deferred list
    # (still confidence-ordered) rather than deliver fewer than `slots` picks.
    for item in list(deferred):
        if len(keep) >= slots:
            break
        keep.append(item)
        deferred.remove(item)
    state["pending_talk"] = keep
    for item in deferred:
        state.setdefault("not_selected", []).append(
            f"{Path(item['segment_path']).name} {item['start_ms'] // 1000}-{item['end_ms'] // 1000}s "
            f"conf={item.get('confidence')} hook={item.get('hook', '')[:40]} "
            f"(落选:全场按信心分全局排序取{MAX_TALK_PICKS}席,同段软上限{TALK_PER_SEGMENT_CAP})"
        )
    refill_songs(state)


def produce_batch(date: str, items: list[dict], produce_fn) -> list[dict]:
    """Produce ``items`` CONCURRENTLY (bounded by MAX_PARALLEL_PRODUCE), preserving
    input order.  Each slice is an independent subprocess (produce_slice_package /
    the song selector), so threads just wait on those; a crash in one becomes a
    failed result and never kills the batch.  ``produce_fn`` is produce_talk /
    produce_song, called as fn(date, item)."""
    from concurrent.futures import ThreadPoolExecutor

    def _one(item: dict) -> dict:
        try:
            return produce_fn(date, item)
        except Exception as exc:  # noqa: BLE001 — one bad slice must not kill the batch
            log(f"produce crashed for {item.get('cid')}: {exc}")
            return {"candidate_id": item.get("cid"), "rc": -1, "status": "failed", "error": str(exc),
                    **{k: item[k] for k in ("hook", "confidence", "danmaku") if k in item}}

    if not items:
        return []
    workers = min(MAX_PARALLEL_PRODUCE, len(items))
    log(f"producing {len(items)} slice(s), up to {workers} in parallel")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_one, items))


def process_date(date: str) -> None:
    state = read_state(date)
    if state.get("status") == "state_corrupt_blocked":
        write_alert("STATE_CORRUPT", f"{date}: {state.get('state_error', 'state file corrupt')} — date BLOCKED, needs human")
        log(f"{date}: state corrupt — blocked, not reprocessing (would re-deliver everything)")
        return
    has_new = any(
        s.stem not in set(state.get("segments_done", [])) and s.stem not in state.get("segments_dead", {})
        for s in list_segments(date)
    )
    has_pending = bool(state.get("pending_talk") or state.get("pending_song"))
    needs_cover = any(cover_repair_needed(date, r) for r in state.get("picks", []) + state.get("songs", []))
    if not has_new and not has_pending and not needs_cover:
        return
    # CPA gate: recall, reconcile, titles and covers all need the chat lane.
    # Recordings can wait — never produce garbage during a provider outage.
    if not cpa_healthy():
        state["status"] = "paused_cpa_down"
        write_state(date, state)
        log(f"{date}: CPA chat lane down — batch deferred to a later tick")
        return
    log(f"processing {date}: new={has_new} pending={has_pending}")
    state.setdefault("picks", [])
    state.setdefault("songs", [])
    state["status"] = "processing"
    write_state(date, state)

    discover_segments(date, state)
    # Session sealing: transcription/recall above runs as segments appear, but
    # SELECTION waits until the inventory is stable so every candidate of the
    # session competes for the quota (late segments used to arrive after the
    # slots were spent).  Cover repairs for already-delivered clips still run.
    if (has_new or has_pending) and not session_sealed(date, state):
        state["status"] = "sealing"
        write_state(date, state)
        log(f"{date}: segment inventory not stable yet — selection deferred to next tick (sealing)")
        return
    prioritize(state)
    write_state(date, state)

    # Phase C: produce talk picks CONCURRENTLY (they're independent; each is
    # network-bound on AGY/CPA/gpt-image-2).  title_failed picks (CPA title lane
    # flaky) stay pending and retry on a later tick — the succeeded ones are kept,
    # not re-done.  CPA was gated at entry; a mid-batch outage just fails a slice.
    if state["pending_talk"]:
        talk_items = list(state["pending_talk"])
        results = produce_batch(date, talk_items, produce_talk)
        retry: list[dict] = []
        for item, result in zip(talk_items, results):
            if result.get("status") == "title_failed":
                item["title_attempts"] = item.get("title_attempts", 0) + 1
                log(f"{item['cid']}: title generation failed (attempt {item['title_attempts']})")
                if item["title_attempts"] < TITLE_MAX_ATTEMPTS:
                    retry.append(item)
                    continue
                result["status"] = "failed"
                result["error"] = "title generation failed 3x"
            state["picks"].append(result)
        state["pending_talk"] = retry
        write_state(date, state)
        if retry:  # some title lanes flaky → back off, resume the rest next tick
            state["status"] = "paused_cpa_down"
            write_state(date, state)
            write_reports(date, state)
            log(f"{date}: {len(retry)} title(s) failed — will retry on a later tick")
            return

    # Song lane with bounded backfill: a gate-BLOCKED song frees its slot for
    # the next backlog song (danmaku-desc) until the delivery budget is met,
    # the backlog runs dry, or SONG_ATTEMPT_CAP is hit.
    while state["pending_song"]:
        song_items = list(state["pending_song"])
        state["songs"].extend(produce_batch(date, song_items, produce_song))
        state["pending_song"] = []
        refill_songs(state)
        write_state(date, state)

    repair_covers(date, state)

    picks, songs = state["picks"], state["songs"]
    delivered_talk = [p for p in picks if p.get("status") in DELIVERED_TALK_STATUSES]
    quarantined = [p for p in picks if p.get("status") == "quarantine"]
    delivered_songs = [s for s in songs if s.get("delivered")]
    blocked_songs = [s for s in songs if s.get("status") == "blocked"]
    failures = [r for r in picks + songs if r.get("status") == "failed"]
    # Honest batch vocabulary (2026-07-09 audit: BLOCK+0 deliveries read 'done /
    # 0 failures').  A batch is review_ready only when something REACHED review.
    if delivered_talk or delivered_songs:
        state["status"] = "review_ready_with_failures" if failures else "review_ready"
    else:
        state["status"] = "no_delivery"
    write_state(date, state)
    write_reports(date, state)
    log(
        f"{date} batch finished [{state['status']}]: talk {len(delivered_talk)}/{len(picks)} delivered"
        f" ({len(quarantined)} quarantined), song {len(delivered_songs)} delivered"
        f" / {len(blocked_songs)} gate-blocked / {len(songs)} attempted, {len(failures)} failure(s)"
    )


def write_heartbeat(body: str) -> None:
    heartbeat = BASE / "reports" / "heartbeat.txt"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {body}\n", encoding="utf-8")


def tick() -> int:
    if (BASE / "DISABLED").exists():
        log("DISABLED flag present — runner paused")
        return 0
    if not cjk_font_present():
        log("WARNING: no CJK font on this host — subtitles would burn as tofu boxes; run: apt install fonts-noto-cjk")
    # Source health FIRST (2026-07-09: a dead CloudDrive mount read as 'no new
    # recordings' and the heartbeat stayed green for hours while the recorder's
    # write path was broken).  A sick source is loud in the heartbeat AND in an
    # alert file, and the tick does nothing else — fail-closed.
    source_err = source_health_error()
    if source_err:
        write_alert("SOURCE_UNAVAILABLE", source_err)
        write_heartbeat(f"SOURCE_UNAVAILABLE({source_err}) dates=(skipped)")
        log(f"recordings source UNAVAILABLE: {source_err} — tick aborted, alert written")
        return 0
    live = blrec_live_status()
    if live is None:
        write_heartbeat("live=? source=ok (blrec API unavailable — fail-safe skip)")
        log("live status unknown — fail-safe skip this tick")
        return 0
    if live:
        write_heartbeat("live=True source=ok (waiting for stream end)")
        log("room is LIVE — waiting for stream end")
        return 0
    checked = []
    for date in list_dates():
        state = read_state(date)
        if state.get("status") == "manual_preclaim":
            checked.append(f"{date}:manual_preclaim")
            continue
        checked.append(f"{date}:{state.get('status', 'new')}")
        process_date(date)
    write_heartbeat(f"live={live} source=ok dates={' '.join(checked) or '(none)'}")
    log(f"tick done: live={live} dates={' '.join(checked) or '(none)'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="single tick (cron entry point)")
    parser.add_argument("--preclaim", nargs="+", metavar="DATE", help="mark dates as manually handled; runner will never touch them")
    parser.add_argument("--smoke-segment", type=Path, help="end-to-end smoke: recall+produce ONE talk candidate from this segment into the smoke area")
    args = parser.parse_args(argv)
    BASE.mkdir(parents=True, exist_ok=True)
    # Inject CPA credentials into OUR process too: the semantic-recall llm_call
    # runs llm_via_cpa.sh from this process (not via child_env()), and without
    # this the recall lane silently degrades to the deterministic fallback.
    os.environ.update(load_env_file(CPA_ENV))
    if args.preclaim:
        for date in args.preclaim:
            write_state(date, {"status": "manual_preclaim", "segments_done": [s.stem for s in list_segments(date)]})
            log(f"preclaimed {date}")
        return 0
    if args.smoke_segment:
        segment = args.smoke_segment
        date = "smoke"
        srt = bcut_transcribe(segment, date)
        if srt is None:
            return 1
        xml = find_danmaku_xml(segment)
        candidates, lane, extras = recall_candidates(srt, danmaku_hints(xml))
        talk = [c for c in candidates if getattr(c, "content_type_hint", "talk") != "song"]
        log(f"smoke: {len(candidates)} candidates via {lane}; producing first talk candidate")
        if not talk:
            log("smoke: no talk candidate found")
            return 1
        cand = talk[0]
        meta = extras.get(cand.anchor.candidate_id, {})
        seg_tag = re.sub(r"\D", "", segment.stem)[-6:]
        item = {
            "cid": f"auto_{seg_tag}_{int(cand.boundary.resolved_start_ms) // 1000}_{int(cand.boundary.resolved_end_ms) // 1000}",
            "segment_path": str(segment),
            "seg_dur_ms": ffprobe_ms(segment),
            "start_ms": max(0, int(cand.boundary.resolved_start_ms)),
            "end_ms": int(cand.boundary.resolved_end_ms),
            "xml": str(xml) if xml else None,
            "hook": meta.get("hook", ""),
            "confidence": meta.get("confidence"),
            "lane": lane,
        }
        result = produce_talk(date, item)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("status") in DELIVERED_TALK_STATUSES else 1
    if args.once:
        return tick()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
