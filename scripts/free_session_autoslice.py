#!/usr/bin/env python3
"""Unattended post-stream auto-slice runner (runs ON the free host).

Ivan's goal (2026-07-05): when a 李豆沙 stream ends, free starts the FULL
canonical pipeline by itself — no human kick-off:

    stream end (blrec live_status via API)
      → per new segment: BCUT aggregate ASR transcript (ms timeline)
      → semantic recall candidate selection (CPA, viewer-perspective;
        deterministic fallback lanes if the LLM is down — zero-output is loud)
      → top-N talk candidates → produce_slice_package per candidate
        (BCUT+AGY+CPA subtitles, sentence boundaries, pillarbox, sapphire72
        burn, REAL CPA cover, 李豆沙-style title)
      → delivery under <repo>/lidousha/<date>/ + AUTOSLICE_SUMMARY.md
      → status report file (no chat/email; Mac pulls via launchd)

Upload stays OFF by design: this runner has no upload path at all and
produce_slice_package writes publish drafts with upload_enabled=false.
Publishing remains a separate, explicitly Ivan-authorized step.

Song candidates are NOT auto-produced (LRC alignment lane needs the strict
completeness gate); they are listed in the summary for a manual/next-agent run.

Deployment (free):
    repo   /opt/bilive/autoslice/repo        (rsync of scripts/ src/ assets/)
    env    /opt/bilive/autoslice/cpa.env     (CPA_BASE_URL / CPA_API_KEY, 600)
    state  /opt/bilive/autoslice/state/<date>.json
    cron   */10 min: flock -n lock python3 scripts/free_session_autoslice.py --once
    kill   touch /opt/bilive/autoslice/DISABLED to pause everything

Safety rails: per-date claim files (no reprocessing, no retry storms — one
produce attempt per candidate per batch), global flock via cron, skip while the
room is live or blrec API is unreachable (fail-safe), and existing dates were
pre-claimed at install so automation only owns streams from 2026-07-06 on.
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
PER_SEGMENT_CANDIDATES = 3
PIECE_PRE_MS = 10_000
PIECE_POST_MS = 32_000
SONG_WINDOW_PRE_MS = 15_000   # window must stay SONG-dominated or the in-window
SONG_WINDOW_POST_MS = 20_000  # recall reclassifies it as talk (smoke-proven at
                              # ±60/45s and ±180/150s).  15/20s matches the
                              # validated 虫儿飞 run (486a: 26:40–28:20 around a
                              # 26:55–28:03 song).  The song lane re-detects
                              # boundaries inside the window; the completeness
                              # gate fails closed if the window clips the song.
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


def danmaku_count_in(xml_path: Path | None, start_ms: int, end_ms: int) -> int:
    if xml_path is None:
        return 0
    try:
        from src.autoslice.danmaku_evidence import load_danmaku_xml

        items = load_danmaku_xml(xml_path)
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


def recall_candidates(srt_path: Path, hints: str | None) -> tuple[list, str]:
    """(talk/song candidates, lane) — semantic lane first, deterministic fallback."""
    from scripts.run_auto_review_shadow_pipeline import _parse_srt
    from src.autoslice.full_session_candidate_selector import select_full_session_candidates
    from src.autoslice.llm_client import LlmCallError, LlmConfig, build_llm_call
    from src.autoslice.semantic_candidate_selector import select_semantic_session_candidates

    cues = _parse_srt(srt_path)
    if not cues:
        return [], "empty"
    llm = build_llm_call(
        LlmConfig(
            transport="command",
            command_template=f"bash {REPO_ROOT}/scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}}",
            timeout_seconds=600.0,
        )
    )
    try:
        candidates, _diag = select_semantic_session_candidates(
            cues, llm_call=llm, max_candidates=PER_SEGMENT_CANDIDATES, danmaku_hints=hints
        )
        return candidates, "semantic_recall"
    except LlmCallError as exc:
        log(f"semantic recall failed ({exc}); falling back to deterministic lanes")
        return select_full_session_candidates(cues, max_candidates=PER_SEGMENT_CANDIDATES), "deterministic_fallback"


def produce_pick(date: str, segment: Path, seg_dur_ms: int, cand, xml_path: Path | None) -> dict:
    boundary = cand.boundary
    start = max(0, int(boundary.resolved_start_ms))
    end = min(seg_dur_ms, int(boundary.resolved_end_ms)) if seg_dur_ms else int(boundary.resolved_end_ms)
    seg_tag = re.sub(r"\D", "", segment.stem)[-6:]
    cid = f"auto_{seg_tag}_{start // 1000}_{end // 1000}"
    out_root = BASE / "out" / date
    spec = {
        "candidate_id": cid,
        "date": date,
        "output_root": str(out_root),
        "delivery_name": None,
        "given_title": None,
        "lead_pad_ms": 400,
        "semantic_start_ms": start,
        "semantic_end_ms": end,
        "pieces": [
            {
                "remote_media": str(segment),
                "start_ms": max(0, start - PIECE_PRE_MS),
                "end_ms": min(seg_dur_ms, end + PIECE_POST_MS) if seg_dur_ms else end + PIECE_POST_MS,
                **({"danmaku_xml_local": str(xml_path)} if xml_path else {}),
            }
        ],
    }
    out_root.mkdir(parents=True, exist_ok=True)
    spec_path = out_root / f"spec_{cid}.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    log_path = BASE / "logs" / f"{date}_{cid}.log"
    log(f"producing {cid} ({(end - start) // 1000}s) from {segment.name}")
    with open(log_path, "w", encoding="utf-8") as sink:
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "produce_slice_package.py"),
             "--spec", str(spec_path), "--ssh-host", "localhost"],
            check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=5400,
            cwd=str(REPO_ROOT), env=child_env(),
        )
    result = {"candidate_id": cid, "segment": segment.name, "start_ms": start, "end_ms": end,
              "rc": completed.returncode, "log": str(log_path), "preview": cand.text_preview[:60]}
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    m = re.search(r"\{[^{}]*\"title\"[^{}]*\}", tail, re.S)
    if m:
        try:
            result["summary"] = json.loads(m.group())
        except ValueError:
            pass
    return result


def produce_song(date: str, segment: Path, seg_dur_ms: int, cand, danmaku_n: int) -> dict:
    """Song lane (Ivan 2026-07-05): cut a generous window around the sung anchor,
    run the canonical song pipeline (netease LRC global-shift alignment + strict
    completeness gate, fail-closed) and deliver only gate-passing results."""
    anchor_start = int(cand.anchor.anchor_start_ms)
    anchor_end = int(cand.anchor.anchor_end_ms)
    start = max(0, anchor_start - SONG_WINDOW_PRE_MS)
    end = min(seg_dur_ms, anchor_end + SONG_WINDOW_POST_MS) if seg_dur_ms else anchor_end + SONG_WINDOW_POST_MS
    seg_tag = re.sub(r"\D", "", segment.stem)[-6:]
    cid = f"song_{seg_tag}_{anchor_start // 1000}"
    out_dir = BASE / "out" / date / cid
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"song lane {cid}: window {start // 1000}-{end // 1000}s (danmaku x{danmaku_n}) from {segment.name}")

    window_mp4 = out_dir / f"{cid}_source.mp4"
    cut = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{start / 1000:.3f}", "-to", f"{end / 1000:.3f}", "-i", str(segment),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
         str(window_mp4)],
        check=False, capture_output=True, text=True, timeout=3600,
    )
    result = {"candidate_id": cid, "segment": segment.name, "start_ms": start, "end_ms": end,
              "danmaku": danmaku_n, "preview": cand.text_preview[:60], "rc": -1}
    if cut.returncode != 0 or not window_mp4.is_file():
        result["error"] = f"window cut failed: {cut.stderr[-200:]}"
        return result

    src_srt = BASE / "cache" / date / f"{segment.stem}.bcut.srt"
    window_srt = out_dir / f"{cid}_source.srt"
    if slice_srt(src_srt, start, end, window_srt) == 0:
        result["error"] = "empty window srt"
        return result

    # NOTE: segment danmaku XML is segment-relative — do not pass it here (the
    # selector would misalign it against the window-relative video); LRC is the
    # subtitle authority for songs anyway.
    log_path = BASE / "logs" / f"{date}_{cid}.log"
    with open(log_path, "w", encoding="utf-8") as sink:
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
    is_song = False
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            for entry in summary.get("records", []) if isinstance(summary, dict) else []:
                decision = entry.get("decision_action") or decision
                job = entry.get("source_context_job") or {}
                is_song = is_song or bool(job.get("song_boundary")) or bool(job.get("lyrics_alignment"))
        except ValueError:
            pass
    result["decision"] = decision
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

        shutil.copy2(burned[0], delivery / f"歌切_{cid}.mp4")
        if covers:
            shutil.copy2(covers[-1], delivery / f"歌切_{cid}.cover.png")
        result["delivered"] = str(delivery / f"歌切_{cid}.mp4")
    return result


def write_reports(date: str, state: dict) -> None:
    delivery = REPO_ROOT / "lidousha" / date
    delivery.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {date} 无人值守自动切片批次",
        "",
        f"- 状态: {state.get('status')}  (runner: free_session_autoslice, 上传永远关闭)",
        f"- 已处理段: {len(state.get('segments_done', []))}",
        "",
        "## 产出 (交付在本目录, 工作目录在 /opt/bilive/autoslice/out/)",
        "",
    ]
    for pick in state.get("picks", []):
        summary = pick.get("summary") or {}
        lines.append(
            f"- `{pick['candidate_id']}` [{pick['segment']} {pick['start_ms'] // 1000}-{pick['end_ms'] // 1000}s] "
            f"rc={pick['rc']} 标题={summary.get('title', '(见log)')} 封面={summary.get('cover_status', '?')} "
            f"边界={summary.get('boundary_verdict', '?')} | {pick.get('preview', '')}"
        )
    songs = state.get("songs", [])
    if songs:
        lines += ["", f"## 歌切 (每场至多 {MAX_SONGS_PER_DATE} 个, 弹幕最高优先; LRC 完整性门 fail-closed)", ""]
        for song in songs:
            lines.append(
                f"- `{song['candidate_id']}` [{song.get('segment', '?')}] 弹幕x{song.get('danmaku', 0)} "
                f"rc={song.get('rc')} 门判定={song.get('decision', '?')} "
                f"{'交付 ' + song['delivered'] if song.get('delivered') else '(未过门/失败, 看log)'} | {song.get('preview', '')}"
            )
    backlog = state.get("song_backlog", [])
    if backlog:
        lines += ["", "## 歌切候选备份 (超上限未产出)", ""]
        lines += [f"- {s}" for s in backlog]
    fails = [p for p in state.get("picks", []) if p.get("rc") != 0] + [s for s in state.get("songs", []) if s.get("rc") != 0]
    if fails:
        lines += ["", f"## ⚠ 失败 {len(fails)} 条 (不自动重试, 防 retry storm; 看各自 log)", ""]
    (delivery / "AUTOSLICE_SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = BASE / "reports" / "latest.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f"# autoslice runner 最新状态\n\n- 时间: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        f"- 日期: {date}  状态: {state.get('status')}\n"
        f"- 谈话: {sum(1 for p in state.get('picks', []) if p.get('rc') == 0)} 成功/{len(state.get('picks', []))} 尝试; "
        f"歌切: {sum(1 for s in state.get('songs', []) if s.get('delivered'))} 交付/{len(state.get('songs', []))} 尝试\n"
        f"- 交付: {REPO_ROOT}/lidousha/{date}/ (Mac 由 launchd 拉取)\n",
        encoding="utf-8",
    )


def process_date(date: str) -> None:
    state = read_state(date)
    done = set(state.get("segments_done", []))
    segments = [s for s in list_segments(date) if s.stem not in done]
    if not segments:
        return
    log(f"processing {date}: {len(segments)} new segment(s)")
    state.setdefault("picks", [])
    state.setdefault("songs", [])
    state.setdefault("song_backlog", [])
    state["status"] = "processing"
    write_state(date, state)

    talk_queue: list[tuple[Path, int, object, Path | None]] = []
    song_queue: list[tuple[Path, int, object, int]] = []
    for segment in segments:
        srt = bcut_transcribe(segment, date)
        if srt is None:
            continue  # leave out of segments_done → retried next batch
        xml = find_danmaku_xml(segment)
        candidates, lane = recall_candidates(srt, danmaku_hints(xml))
        seg_dur = ffprobe_ms(segment)
        log(f"{segment.name}: {len(candidates)} candidate(s) via {lane}")
        for cand in candidates:
            if getattr(cand, "content_type_hint", "talk") == "song":
                count = danmaku_count_in(xml, int(cand.anchor.anchor_start_ms), int(cand.anchor.anchor_end_ms))
                song_queue.append((segment, seg_dur, cand, count))
            else:
                talk_queue.append((segment, seg_dur, cand, xml))
        done.add(segment.stem)

    # round-robin across segments so picks spread over the whole stream
    by_segment: dict[str, list] = {}
    for item in talk_queue:
        by_segment.setdefault(item[0].stem, []).append(item)
    picks: list[tuple[Path, int, object, Path | None]] = []
    while len(picks) < MAX_TALK_PICKS and any(by_segment.values()):
        for stem in sorted(by_segment):
            if by_segment[stem] and len(picks) < MAX_TALK_PICKS:
                picks.append(by_segment[stem].pop(0))

    for segment, seg_dur, cand, xml in picks:
        try:
            state["picks"].append(produce_pick(date, segment, seg_dur, cand, xml))
        except Exception as exc:  # noqa: BLE001 — one bad candidate must not kill the batch
            log(f"produce crashed for {segment.name}: {exc}")
            state["picks"].append({"candidate_id": f"crash_{segment.stem}", "segment": segment.name,
                                   "start_ms": 0, "end_ms": 0, "rc": -1, "error": str(exc)})
        write_state(date, state)

    # Song lane: 每场至多 MAX_SONGS_PER_DATE 个歌切，弹幕最高的优先 (Ivan 2026-07-05).
    song_queue.sort(key=lambda item: (-item[3], -(item[2].anchor.anchor_end_ms - item[2].anchor.anchor_start_ms)))
    budget = max(0, MAX_SONGS_PER_DATE - len(state["songs"]))
    for segment, seg_dur, cand, count in song_queue[:budget]:
        try:
            state["songs"].append(produce_song(date, segment, seg_dur, cand, count))
        except Exception as exc:  # noqa: BLE001
            log(f"song lane crashed for {segment.name}: {exc}")
            state["songs"].append({"candidate_id": f"crash_{segment.stem}", "segment": segment.name,
                                   "rc": -1, "danmaku": count, "error": str(exc)})
        write_state(date, state)
    for segment, _dur, cand, count in song_queue[budget:]:
        state["song_backlog"].append(
            f"{segment.name} {cand.anchor.anchor_start_ms // 1000}-{cand.anchor.anchor_end_ms // 1000}s "
            f"弹幕x{count}: {cand.text_preview[:50]} (超出每场{MAX_SONGS_PER_DATE}个上限)"
        )

    state["segments_done"] = sorted(done)
    failures = [p for p in state["picks"] if p.get("rc") != 0]
    state["status"] = "done_with_failures" if failures else "done"
    write_state(date, state)
    write_reports(date, state)
    log(f"{date} batch finished: {len(state['picks'])} picks, {len(failures)} failure(s)")


def tick() -> int:
    if (BASE / "DISABLED").exists():
        log("DISABLED flag present — runner paused")
        return 0
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
        if state.get("status") in ("manual_preclaim", "processing"):
            checked.append(f"{date}:{state.get('status')}")
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
    parser.add_argument("--smoke-segment", type=Path, help="end-to-end smoke: recall+produce ONE candidate from this segment into the smoke area")
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
        candidates, lane = recall_candidates(srt, danmaku_hints(xml))
        talk = [c for c in candidates if getattr(c, "content_type_hint", "talk") != "song"]
        log(f"smoke: {len(candidates)} candidates via {lane}; producing first talk candidate")
        if not talk:
            log("smoke: no talk candidate found")
            return 1
        result = produce_pick(date, segment, ffprobe_ms(segment), talk[0], xml)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("rc") == 0 else 1
    if args.once:
        return tick()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
