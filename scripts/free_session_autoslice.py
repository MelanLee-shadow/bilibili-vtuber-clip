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
  the CPA chat lane is down (gpt-5.5/5.4 provider outages happen), the batch
  is DEFERRED (status=paused_cpa_down) and resumes on a later tick — clips are
  never produced with cid titles / cid-text covers.
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
import json
import os
import re
import subprocess
import sys
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
PER_SEGMENT_CANDIDATES = 4
MIN_SEGMENT_BYTES = 5_000_000  # blrec restart stubs are a few KB — dead on sight
BCUT_MAX_ATTEMPTS = 2
TITLE_MAX_ATTEMPTS = 3
PIECE_PRE_MS = 10_000
PIECE_POST_MS = 32_000
SONG_WINDOW_PRE_MS = 15_000   # window must stay SONG-dominated or the in-window
SONG_WINDOW_POST_MS = 20_000  # recall reclassifies it as talk (smoke-proven at
                              # ±60/45s and ±180/150s); 15/20s matches the
                              # validated 虫儿飞 run.
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CPA_CMD = "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}"
# The selector's --cpa-command is the semantic-QA JUDGE lane (request/response
# JSON contract), NOT a prompt/completion LLM template — canonical validated
# command per docs/spark/2026-06-30-future-live-e2e-runbook.md.  The selector
# does NOT run it through a shell, so the api-base must be substituted here
# (the key stays off the command line via --api-key-env).


def cpa_qa_cmd() -> str:
    base = os.environ.get("CPA_BASE_URL", "").rstrip("/")
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
    """One cheap real probe against the CPA chat lane (gpt-5.5, then gpt-5.4).

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
    for model in ("gpt-5.5", "gpt-5.4"):
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
    try:
        return json.loads(state_path(date).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(date: str, state: dict) -> None:
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state_path(date).parent.mkdir(parents=True, exist_ok=True)
    state_path(date).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def list_dates() -> list[str]:
    try:
        names = [p.name for p in REC_ROOT.iterdir() if p.is_dir() and DATE_RX.match(p.name)]
    except OSError:
        return []
    return sorted(names)[-3:]


def list_segments(date: str) -> list[Path]:
    date_dir = REC_ROOT / date
    try:
        files = sorted(date_dir.glob(f"{ROOM}_*.mp4"))
    except OSError:
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
            command_template=f"bash {REPO_ROOT}/scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}}",
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


def produce_talk(date: str, item: dict) -> dict:
    """Run produce_slice_package for one pending talk item (plain-dict spec).

    Returns the result record; status is one of ok / title_failed / failed.
    A title_failed pick is cleaned up (no delivery with a cid title/cover) and
    retried on a later resume.
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
    with open(log_path, "a", encoding="utf-8") as sink:
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "produce_slice_package.py"),
             "--spec", str(spec_path), "--ssh-host", "localhost"],
            check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=5400,
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
    result["status"] = "ok"
    return result


def produce_song(date: str, item: dict) -> dict:
    """Song lane (Ivan 2026-07-05): cut a tight window around the sung anchor,
    run the canonical song pipeline (netease LRC global-shift alignment + strict
    completeness gate, fail-closed) and deliver only gate-passing results."""
    anchor_start = item["anchor_start_ms"]
    anchor_end = item["anchor_end_ms"]
    seg_dur_ms = item["seg_dur_ms"]
    segment = Path(item["segment_path"])
    start = max(0, anchor_start - SONG_WINDOW_PRE_MS)
    end = min(seg_dur_ms, anchor_end + SONG_WINDOW_POST_MS) if seg_dur_ms else anchor_end + SONG_WINDOW_POST_MS
    cid = item["cid"]
    out_dir = BASE / "out" / date / cid
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"song lane {cid}: window {start // 1000}-{end // 1000}s (danmaku x{item.get('danmaku', 0)}) from {segment.name}")

    result = {"candidate_id": cid, "segment": segment.name, "start_ms": start, "end_ms": end,
              "danmaku": item.get("danmaku", 0), "hook": item.get("hook", ""), "preview": item.get("preview", "")[:60], "rc": -1}
    window_mp4 = out_dir / f"{cid}_source.mp4"
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
    window_srt = out_dir / f"{cid}_source.srt"
    if slice_srt(src_srt, start, end, window_srt) == 0:
        result["error"] = "empty window srt"
        result["status"] = "failed"
        return result

    # NOTE: segment danmaku XML is segment-relative — do not pass it here (the
    # selector would misalign it against the window-relative video); LRC is the
    # subtitle authority for songs anyway.
    log_path = BASE / "logs" / f"{date}_{cid}.log"
    with open(log_path, "a", encoding="utf-8") as sink:
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "run_full_session_selector_cpa_shadow.py"),
             "--source-video", str(window_mp4), "--source-srt", str(window_srt),
             "--output-dir", str(out_dir / "song_selector"), "--max-candidates", "1",
             "--cpa-command", cpa_qa_cmd(),
             "--semantic-recall-llm-command", CPA_CMD,
             "--song-hint-llm-command", CPA_CMD,
             "--title-llm-command", CPA_CMD,
             "--cover-art-direction-llm-command", CPA_CMD,
             "--lrc-provider", "netease", "--burn-preview", "--publish-staging"],
            check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=5400,
            cwd=str(REPO_ROOT), env=child_env(),
        )
    result["rc"] = completed.returncode
    result["log"] = str(log_path)
    summary_path = out_dir / "song_selector" / "summary.json"
    decision = None
    reasons: list = []
    is_song = False
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            for entry in summary.get("records", []) if isinstance(summary, dict) else []:
                decision = entry.get("decision_action") or decision
                reasons = list(entry.get("reason_codes") or reasons)
                job = entry.get("source_context_job") or {}
                is_song = is_song or bool(job.get("song_boundary")) or bool(job.get("lyrics_alignment"))
        except ValueError:
            pass
    result["decision"] = decision
    result["reason_codes"] = reasons
    result["window_classified_song"] = is_song
    for publish in sorted((out_dir / "song_selector").glob("**/replacement_recuts/*.publish.json")):
        try:
            result["title"] = json.loads(publish.read_text(encoding="utf-8")).get("title")
        except (OSError, ValueError):
            pass
    burned = sorted((out_dir / "song_selector").glob("**/replacement_recuts/*.burned-final-sapphire72.mp4"))
    covers = sorted((out_dir / "song_selector").glob("**/covers/*.cover.png"))
    if decision == "AUTO_UPLOAD" and burned:
        delivery = REPO_ROOT / "lidousha" / date
        delivery.mkdir(parents=True, exist_ok=True)
        import shutil

        name = safe_name("歌切_" + (result.get("title") or item.get("hook") or ""), f"歌切_{cid}")
        shutil.copy2(burned[0], delivery / f"{name}.mp4")
        if covers:
            shutil.copy2(covers[-1], delivery / f"{name}.cover.png")
        result["delivered"] = str(delivery / f"{name}.mp4")
    result["status"] = "ok" if completed.returncode == 0 else "failed"
    return result


def write_reports(date: str, state: dict) -> None:
    delivery = REPO_ROOT / "lidousha" / date
    delivery.mkdir(parents=True, exist_ok=True)

    def fmt_dur(pick: dict) -> str:
        secs = max(0, (pick.get("end_ms", 0) - pick.get("start_ms", 0)) // 1000)
        return f"{secs // 60}:{secs % 60:02d}"

    lines = [
        f"# {date} 无人值守自动切片批次",
        "",
        f"- 状态: **{state.get('status')}**  (runner v3; 上传永远关闭，全部成品仅供人工审查)",
        f"- 段: 完成 {len(state.get('segments_done', []))} / 死段 {len(state.get('segments_dead', {}))} / 待产出 talk {len(state.get('pending_talk', []))} + song {len(state.get('pending_song', []))}",
        "",
        "## 谈话成品（审查要点：标题、选片理由、边界收束）",
        "",
        "| 成品 | 时长 | 标题 | 选片理由(hook) | 信心 | 收束句 | 边界 | 封面 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for pick in state.get("picks", []):
        s = pick.get("summary") or {}
        status_mark = "" if pick.get("status") == "ok" else f" ⚠{pick.get('status')}"
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
    songs = state.get("songs", [])
    lines += ["", f"## 歌切（每场至多 {MAX_SONGS_PER_DATE}，弹幕最高优先；LRC 完整性门 fail-closed）", ""]
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
        lines += ["", "## 歌切候选备份（超每场上限，未产出）", ""] + [f"- {b}" for b in backlog]
    not_selected = state.get("not_selected", [])
    if not_selected:
        lines += ["", "## 落选谈话候选（供复核选片是否漏才）", ""] + [f"- {n}" for n in not_selected]
    dead = state.get("segments_dead", {})
    if dead:
        lines += ["", "## 死段（不再重试）", ""] + [f"- {k}: {v}" for k, v in dead.items()]
    if state.get("status") == "paused_cpa_down":
        lines += ["", "> ⚠ CPA 链路不可用，批次已暂停；cron 每 10 分钟自动重试，恢复后从断点续产。"]
    (delivery / "AUTOSLICE_SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = BASE / "reports" / "latest.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    ok_picks = sum(1 for p in state.get("picks", []) if p.get("status") == "ok")
    report.write_text(
        f"# autoslice runner 最新状态\n\n- 时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        f"- 日期: {date}  状态: {state.get('status')}\n"
        f"- 谈话: {ok_picks} 成品 / {len(state.get('picks', []))} 尝试 (pending {len(state.get('pending_talk', []))})\n"
        f"- 歌切: {sum(1 for s in state.get('songs', []) if s.get('delivered'))} 交付 / {len(state.get('songs', []))} 尝试 (pending {len(state.get('pending_song', []))})\n"
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


def prioritize(state: dict) -> None:
    """Phase B: cap talk picks (round-robin across segments) and songs (top
    danmaku).  Overflow is recorded with reasons so review can catch misses."""
    pending_talk = state.get("pending_talk", [])
    produced_ok = sum(1 for p in state.get("picks", []) if p.get("status") == "ok")
    slots = max(0, MAX_TALK_PICKS - produced_ok)
    by_segment: dict[str, list] = {}
    for item in pending_talk:
        by_segment.setdefault(item["segment_path"], []).append(item)
    ordered: list[dict] = []
    while any(by_segment.values()):
        for seg in sorted(by_segment):
            if by_segment[seg]:
                ordered.append(by_segment[seg].pop(0))
    keep, drop = ordered[:slots], ordered[slots:]
    state["pending_talk"] = keep
    for item in drop:
        state.setdefault("not_selected", []).append(
            f"{Path(item['segment_path']).name} {item['start_ms'] // 1000}-{item['end_ms'] // 1000}s "
            f"conf={item.get('confidence')} hook={item.get('hook', '')[:40]} (超每场{MAX_TALK_PICKS}条上限)"
        )

    pending_song = state.get("pending_song", [])
    pending_song.sort(key=lambda x: (-(x.get("danmaku") or 0), -(x["anchor_end_ms"] - x["anchor_start_ms"])))
    produced_songs = len(state.get("songs", []))
    budget = max(0, MAX_SONGS_PER_DATE - produced_songs)
    keep_s, drop_s = pending_song[:budget], pending_song[budget:]
    state["pending_song"] = keep_s
    for item in drop_s:
        state.setdefault("song_backlog", []).append(
            f"{Path(item['segment_path']).name} {item['anchor_start_ms'] // 1000}-{item['anchor_end_ms'] // 1000}s "
            f"弹幕x{item.get('danmaku', 0)}: {item.get('hook') or item.get('preview', '')[:40]} (超出每场{MAX_SONGS_PER_DATE}个上限)"
        )


def process_date(date: str) -> None:
    state = read_state(date)
    has_new = any(
        s.stem not in set(state.get("segments_done", [])) and s.stem not in state.get("segments_dead", {})
        for s in list_segments(date)
    )
    has_pending = bool(state.get("pending_talk") or state.get("pending_song"))
    if not has_new and not has_pending:
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
    prioritize(state)
    write_state(date, state)

    # Phase C: produce with mid-batch CPA gate + bounded title retries.
    while state["pending_talk"]:
        if not cpa_healthy():
            state["status"] = "paused_cpa_down"
            write_state(date, state)
            write_reports(date, state)
            log(f"{date}: CPA went down mid-batch — pausing, will resume")
            return
        item = state["pending_talk"][0]
        try:
            result = produce_talk(date, item)
        except Exception as exc:  # noqa: BLE001 — one bad candidate must not kill the batch
            log(f"produce crashed for {item['cid']}: {exc}")
            result = {**{k: item.get(k) for k in ("cid", "hook", "confidence")},
                      "candidate_id": item["cid"], "rc": -1, "status": "failed", "error": str(exc)}
        if result.get("status") == "title_failed":
            item["title_attempts"] = item.get("title_attempts", 0) + 1
            log(f"{item['cid']}: title generation failed (attempt {item['title_attempts']})")
            if item["title_attempts"] < TITLE_MAX_ATTEMPTS:
                state["status"] = "paused_cpa_down"  # title lane is flaky → back off to a later tick
                write_state(date, state)
                write_reports(date, state)
                return
            result["status"] = "failed"
            result["error"] = "title generation failed 3x"
        state["pending_talk"].pop(0)
        state["picks"].append(result)
        write_state(date, state)

    while state["pending_song"]:
        if not cpa_healthy():
            state["status"] = "paused_cpa_down"
            write_state(date, state)
            write_reports(date, state)
            log(f"{date}: CPA went down mid-batch (songs) — pausing, will resume")
            return
        item = state["pending_song"][0]
        try:
            result = produce_song(date, item)
        except Exception as exc:  # noqa: BLE001
            log(f"song lane crashed for {item['cid']}: {exc}")
            result = {"candidate_id": item["cid"], "rc": -1, "status": "failed",
                      "danmaku": item.get("danmaku", 0), "error": str(exc)}
        state["pending_song"].pop(0)
        state["songs"].append(result)
        write_state(date, state)

    failures = [p for p in state["picks"] if p.get("status") != "ok"] + [s for s in state["songs"] if s.get("status") != "ok"]
    state["status"] = "done_with_failures" if failures else "done"
    write_state(date, state)
    write_reports(date, state)
    log(f"{date} batch finished: {len(state['picks'])} picks / {len(state['songs'])} songs, {len(failures)} failure(s)")


def tick() -> int:
    if (BASE / "DISABLED").exists():
        log("DISABLED flag present — runner paused")
        return 0
    if not cjk_font_present():
        log("WARNING: no CJK font on this host — subtitles would burn as tofu boxes; run: apt install fonts-noto-cjk")
    live = blrec_live_status()
    if live is None:
        log("live status unknown — fail-safe skip this tick")
        return 0
    if live:
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
    heartbeat = BASE / "reports" / "heartbeat.txt"
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text(
        f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} live={live} dates={' '.join(checked) or '(none)'}\n",
        encoding="utf-8",
    )
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
        return 0 if result.get("status") == "ok" else 1
    if args.once:
        return tick()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
