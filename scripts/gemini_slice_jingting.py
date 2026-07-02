#!/usr/bin/env python3
"""
Li Dousha slice fine-transcription runner.

Runs on the `free` host. Full-recording subtitles still drive rough semantic
slicing; this script refines final slice sidecars only and writes a separate
`<slice>.jingting.srt` plus a hash/log manifest.

Providers:
  - gemini: direct Gemini API, audio + draft SRT -> corrected SRT.
  - agy: Antigravity CLI on the free host, local media + draft SRT -> output.srt.

Examples:
  gemini_slice_jingting.py --provider agy /path/to/slice.flv
  gemini_slice_jingting.py --provider agy --once --room 22966160
  gemini_slice_jingting.py --provider agy --daemon --room 22966160
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOM = "22966160"
HOST_VIDEOS = "/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming"
CONTAINER_VIDEOS = "/app/Videos"
VIDEOS = os.environ.get("BILIVE_VIDEOS_ROOT") or (
    HOST_VIDEOS if os.path.isdir(HOST_VIDEOS) else CONTAINER_VIDEOS
)

GEMINI_MODEL = os.environ.get("JINGTING_GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

AGY_BIN = os.environ.get("AGY_BIN", str(Path.home() / ".local/bin/agy"))
AGY_MODEL = os.environ.get("AGY_MODEL", "Gemini 3.5 Flash (Low)")
AGY_TIMEOUT = os.environ.get("AGY_PRINT_TIMEOUT", "15m")
JINGTING_JOB_ROOT = os.environ.get("JINGTING_JOB_ROOT", "/opt/bilive/jingting_jobs")

_GLOSSARY_ENV = os.environ.get("LIDOUSHA_GLOSSARY")
GLOSSARY_PATHS = (
    [_GLOSSARY_ENV]
    if _GLOSSARY_ENV
    else ["/opt/bilive/app/lidousha_glossary.txt", "/app/lidousha_glossary.txt"]
)
SLICE_RX_TEMPLATE = r"\d+s_.*_%s_.*\.(flv|mp4)$"
SRT_TIME_RX = re.compile(
    r"\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}"
)


def log(message: str) -> None:
    print(message, flush=True)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def agy_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    if not env.get("HOME"):
        try:
            env["HOME"] = str(Path.home())
        except RuntimeError:
            env["HOME"] = "/root"
    return env


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def gemini_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY_2") or ""


def glossary() -> str:
    for path in GLOSSARY_PATHS:
        if not path:
            continue
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


def find_srt(slice_path: str | Path) -> str | None:
    slice_path = str(slice_path)
    stem = slice_path.rsplit(".", 1)[0]
    base = os.path.basename(stem)
    candidates = (
        stem + ".srt",
        os.path.join(os.path.dirname(slice_path), "subtitles", base + ".srt"),
        os.path.join(os.path.dirname(slice_path), "subtitles", base + ".coarse.srt"),
    )
    for cand in candidates:
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            return cand
    return None


def looks_like_srt(text: str) -> bool:
    return bool(text.strip()) and bool(SRT_TIME_RX.search(text)) and "\n" in text


def srt_signature(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    signature: list[tuple[str, str]] = []
    for i, line in enumerate(lines):
        index = line.strip()
        if not index.isdigit():
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            continue
        match = SRT_TIME_RX.search(lines[j])
        if match:
            signature.append((index, match.group(0)))
    return signature


def validate_same_timing(draft_text: str, corrected_text: str) -> None:
    draft_sig = srt_signature(draft_text)
    corrected_sig = srt_signature(corrected_text)
    if not draft_sig:
        raise RuntimeError("draft SRT has no parseable cue timings")
    if draft_sig != corrected_sig:
        raise RuntimeError(
            f"output SRT cue/timing mismatch: draft={len(draft_sig)} output={len(corrected_sig)}"
        )


def subtitle_review_findings(text: str) -> list[str]:
    """Return reasons this refined SRT still needs human/lyrics review."""
    findings: list[str] = []
    if re.search(r"(?i)(?:la\s*){16,}|(?:啦|拉){10,}", text):
        findings.append("repeated_vocalization_block")

    blocks = [b for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    if any(sum(1 for line in b.splitlines() if line.strip() and not line.strip().isdigit() and "-->" not in line) > 6 for b in blocks):
        findings.append("oversized_multiline_cue")

    japanese_chars = len(re.findall(r"[\u3040-\u30ff\u31f0-\u31ff]", text))
    if japanese_chars >= 20:
        findings.append("japanese_song_or_lyrics_alignment_required")

    if re.search(r"(樱花草|櫻花草|黄昏晓|黃昏曉|怪獣|初恋サイダー|言って)", text):
        findings.append("known_song_lyrics_alignment_required")

    return sorted(set(findings))


def strip_markdown_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?|\n?```$", "", text).strip()
    return text


def extract_audio(slice_path: str, out_mp3: str) -> bool:
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            slice_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            out_mp3,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return result.returncode == 0 and os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 0


def gemini_correct(audio_mp3: str, srt_text: str, key: str) -> str:
    prompt = (
        glossary()
        + "\n\n----\n下面是这条李豆沙切片的 whisper 字幕草稿（SRT）。"
        "请你听这段音频，按上面的术语表和纠错规则精修每一条字幕的文本："
        "改正误听、专有名词、标点、自然断句，保留主播口癖和语气。"
        "严格保留每条的序号和时间轴（时间戳一字不改），只改字幕文本。"
        "输出完整 SRT 文本，不要任何解释、不要 markdown 代码块。\n\n----\n"
        + srt_text
    )
    audio_b64 = base64.b64encode(Path(audio_mp3).read_bytes()).decode()
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "audio/mp3", "data": audio_b64}},
                ]
            }
        ],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
    }
    req = urllib.request.Request(
        GEMINI_URL.format(model=GEMINI_MODEL, key=key),
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    cand = (d.get("candidates") or [{}])[0]
    parts = cand.get("content", {}).get("parts", [])
    return strip_markdown_fence("".join(p.get("text", "") for p in parts))


def remux_for_agy(slice_path: Path, job_dir: Path) -> Path:
    """Normalize media to input.mp4 for Antigravity view_file."""
    out = job_dir / "input.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(slice_path),
        "-c",
        "copy",
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return out

    # Some FLV inputs cannot be stream-copied cleanly. Re-encode as a slower but
    # dependable fallback because Antigravity has been validated on MP4 input.
    fallback_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(slice_path),
        "-vf",
        "scale=1280:-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        str(out),
    ]
    fallback = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=900)
    if fallback.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return out
    raise RuntimeError(
        "ffmpeg failed to prepare input.mp4: "
        + (result.stderr or fallback.stderr or "unknown error")[:500]
    )


def agy_prompt(srt_text: str) -> str:
    glossary_text = glossary().strip()
    glossary_block = f"\nGlossary and style rules:\n{glossary_text}\n" if glossary_text else ""
    return f"""You are refining subtitles for a Li Dousha Chinese VTuber clip.

Use only these local files in this job directory:
- input.mp4
- draft.srt

Allowed tools:
- view_file on prompt.md
- view_file on input.mp4 and draft.srt
- write_to_file to relative output.srt
- view_file on output.srt only after writing

Forbidden actions:
- Do not use shell, terminal, browser, web, search, repository reads, or any file outside this job directory.
- Do not write to an absolute path.
- Do not change subtitle indices or timestamps.

Task:
1. Watch/listen to input.mp4.
2. Use draft.srt as the timing authority.
3. Correct only subtitle text: mishearings, names, memes, punctuation, and natural Chinese wording.
4. Preserve Li Dousha tone, streamer-specific terms, and uncertainty when audio is unclear.
5. Write a complete valid SRT to relative file output.srt.

Output requirement:
- output.srt must contain SRT only.
- Same cue count, same cue indices, and same timestamps as draft.srt.
- No Markdown fences, no explanations.
{glossary_block}
Current draft.srt content:
{srt_text}
"""


def run_agy(slice_path: str, srt_path: str, out_path: str) -> str:
    agy_bin = shutil.which(AGY_BIN) or AGY_BIN
    if not os.path.exists(agy_bin):
        raise FileNotFoundError(f"agy binary not found: {AGY_BIN}")

    slice_p = Path(slice_path)
    stem = slice_p.stem
    room = "unknown-room"
    date = "unknown-date"
    parts = slice_p.parts
    for i, part in enumerate(parts):
        if part == "live-streaming" and i + 2 < len(parts):
            room = parts[i + 1]
            date = parts[i + 2]
            break
        if part == "Videos" and i + 2 < len(parts):
            room = parts[i + 1]
            date = parts[i + 2]
            break
    job_root = Path(JINGTING_JOB_ROOT) / room / date
    job_dir = job_root / f"{stem}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    job_dir.mkdir(parents=True, exist_ok=True)

    srt_text = Path(srt_path).read_text(encoding="utf-8")
    media = remux_for_agy(slice_p, job_dir)
    draft = job_dir / "draft.srt"
    draft.write_text(srt_text if srt_text.endswith("\n") else srt_text + "\n", encoding="utf-8")
    prompt = agy_prompt(srt_text)
    (job_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    short_prompt = (
        f"Open {job_dir}/prompt.md with view_file and follow it exactly. "
        f"Use only {job_dir}/prompt.md, {job_dir}/input.mp4, "
        f"{job_dir}/draft.srt, and {job_dir}/output.srt. "
        "Do not inspect any other file or directory. Do not use shell or terminal."
    )

    cmd = [
        agy_bin,
        "--sandbox",
        "--add-dir",
        str(job_dir),
        "--model",
        AGY_MODEL,
        "-p",
        short_prompt,
        "--print-timeout",
        AGY_TIMEOUT,
    ]
    started = utc_now()
    proc = subprocess.run(
        cmd,
        cwd=job_dir,
        env=agy_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=parse_timeout_seconds(AGY_TIMEOUT) + 120,
    )
    (job_dir / "agy.stdout").write_text(proc.stdout, encoding="utf-8")
    (job_dir / "agy.stderr").write_text(proc.stderr, encoding="utf-8")

    output_file = job_dir / "output.srt"
    corrected = ""
    if output_file.exists():
        corrected = strip_markdown_fence(output_file.read_text(encoding="utf-8"))
    if not looks_like_srt(corrected):
        corrected = strip_markdown_fence(proc.stdout)
    if proc.returncode != 0:
        raise RuntimeError(f"agy failed rc={proc.returncode}; see {job_dir}/agy.stderr")
    if not looks_like_srt(corrected):
        raise RuntimeError(f"agy did not produce valid SRT; see {job_dir}")
    validate_same_timing(srt_text, corrected)

    Path(out_path).write_text(corrected if corrected.endswith("\n") else corrected + "\n", encoding="utf-8")
    manifest = {
        "provider": "agy",
        "model": AGY_MODEL,
        "started_at": started,
        "finished_at": utc_now(),
        "slice_path": str(slice_p),
        "slice_sha256": sha256_file(slice_p),
        "draft_srt": str(srt_path),
        "draft_srt_sha256": sha256_file(srt_path),
        "output_srt": str(out_path),
        "output_srt_sha256": sha256_file(out_path),
        "job_dir": str(job_dir),
        "agy_rc": proc.returncode,
        "agy_sandbox": True,
        "prepared_media": str(media),
        "prepared_media_size": media.stat().st_size,
    }
    Path(out_path).with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return str(job_dir)


def parse_timeout_seconds(value: str) -> int:
    value = str(value).strip()
    m = re.match(r"^(\d+)([smh]?)$", value)
    if not m:
        return 15 * 60
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "h":
        return n * 3600
    if unit == "m":
        return n * 60
    return n


def retry_marker_path(slice_path: str | Path) -> Path:
    return Path(str(slice_path).rsplit(".", 1)[0] + ".jingting.retry.json")


def slice_lock_path(slice_path: str | Path) -> Path:
    return Path(str(slice_path).rsplit(".", 1)[0] + ".jingting.lock")


def acquire_directory_lock(lock_path: Path) -> bool:
    try:
        lock_path.mkdir(parents=False)
    except FileExistsError:
        if _lock_pid_is_dead(lock_path):
            release_directory_lock(lock_path)
            try:
                lock_path.mkdir(parents=False)
            except FileExistsError:
                return False
        else:
            return False
    lock_path.joinpath("pid").write_text(str(os.getpid()) + "\n", encoding="utf-8")
    lock_path.joinpath("created_at").write_text(utc_now() + "\n", encoding="utf-8")
    return True


def release_directory_lock(lock_path: Path) -> None:
    try:
        for child in lock_path.iterdir():
            child.unlink()
        lock_path.rmdir()
    except OSError:
        pass


def _lock_pid_is_dead(lock_path: Path) -> bool:
    try:
        pid = int(lock_path.joinpath("pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid():
        return False
    if hasattr(os, "kill"):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
    return False


def write_retry_marker(slice_path: str | Path, *, provider: str, error: Exception) -> None:
    marker = retry_marker_path(slice_path)
    marker.write_text(
        json.dumps(
            {
                "status": "retry_blocked",
                "created_at": utc_now(),
                "slice": str(slice_path),
                "provider": provider,
                "error_type": type(error).__name__,
                "error": str(error)[:1000],
                "release_ready": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def clear_retry_marker(slice_path: str | Path) -> None:
    try:
        retry_marker_path(slice_path).unlink()
    except OSError:
        pass


def process_slice(slice_path: str, provider: str, key: str = "", verbose: bool = False) -> str:
    stem = slice_path.rsplit(".", 1)[0]
    done = stem + ".jingting.done"
    if os.path.exists(done):
        return "skip(done)"
    lock_path = slice_lock_path(slice_path)
    if not acquire_directory_lock(lock_path):
        return "skip(locked)"
    try:
        if os.path.exists(done):
            return "skip(done)"
        return _process_slice_locked(slice_path, provider, key=key, verbose=verbose)
    finally:
        release_directory_lock(lock_path)


def _process_slice_locked(slice_path: str, provider: str, key: str = "", verbose: bool = False) -> str:
    stem = slice_path.rsplit(".", 1)[0]
    done = stem + ".jingting.done"
    srt = find_srt(slice_path)
    if not srt:
        return "skip(no-srt)"

    out = stem + ".jingting.srt"
    try:
        if provider == "gemini":
            mp3 = stem + ".jingting.mp3"
            try:
                if not extract_audio(slice_path, mp3):
                    return "fail(audio)"
                corrected = gemini_correct(mp3, Path(srt).read_text(encoding="utf-8"), key)
            finally:
                try:
                    os.remove(mp3)
                except OSError:
                    pass
            if not looks_like_srt(corrected):
                return "fail(empty-or-not-srt)"
            Path(out).write_text(
                corrected if corrected.endswith("\n") else corrected + "\n",
                encoding="utf-8",
            )
            job_note = ""
        elif provider == "agy":
            job_note = run_agy(slice_path, srt, out)
        else:
            return f"fail(unknown-provider {provider})"
    except urllib.error.HTTPError as e:
        write_retry_marker(slice_path, provider=provider, error=e)
        return f"fail(gemini http {e.code}: {e.read()[:120].decode(errors='replace')})"
    except Exception as e:
        write_retry_marker(slice_path, provider=provider, error=e)
        return f"fail({provider} {type(e).__name__}: {e})"

    clear_retry_marker(slice_path)
    findings = subtitle_review_findings(Path(out).read_text(encoding="utf-8"))
    review_marker = stem + ".jingting.review-required.json"
    if findings:
        Path(review_marker).write_text(
            json.dumps(
                {
                    "status": "review_required",
                    "created_at": utc_now(),
                    "slice": slice_path,
                    "output_srt": out,
                    "findings": findings,
                    "release_ready": False,
                    "note": "Processed SRT exists, but song/lyrics or subtitle QA requires human review before burn/upload.",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    else:
        try:
            os.remove(review_marker)
        except OSError:
            pass

    Path(done).write_text(
        json.dumps(
            {
                "provider": provider,
                "processed_at": utc_now(),
                "slice": slice_path,
                "draft_srt": srt,
                "output_srt": out,
                "job": job_note,
                "subtitle_qc_status": "review_required" if findings else "draft_processed",
                "subtitle_qc_findings": findings,
                "release_ready": False,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    if verbose:
        log(f"--- {os.path.basename(srt)} draft ---\n{Path(srt).read_text(encoding='utf-8')[:500]}")
        log(f"--- {os.path.basename(out)} jingting ---\n{Path(out).read_text(encoding='utf-8')[:500]}")
    suffix = f" job={job_note}" if job_note else ""
    return f"ok -> {os.path.basename(out)}{suffix}"


def date_dirs(root: str, room: str, date: str | None, all_dates: bool) -> list[str]:
    room_root = os.path.join(root, room)
    if date:
        return [os.path.join(room_root, date)]
    try:
        names = os.listdir(room_root) if os.path.isdir(room_root) else []
    except OSError as exc:
        log(f"[jingting] date_dirs failed for {room_root}: {type(exc).__name__}: {exc}")
        return []
    dirs = sorted(
        os.path.join(room_root, d)
        for d in names
        if re.match(r"\d{4}-\d{2}-\d{2}$", d) and os.path.isdir(os.path.join(room_root, d))
    )
    if all_dates:
        return dirs
    return dirs[-1:] if dirs else []


def pending_slices(root: str, room: str, date: str | None, all_dates: bool, *, retry_failed: bool = False) -> list[str]:
    out: list[str] = []
    slice_rx = re.compile(SLICE_RX_TEMPLATE % re.escape(room))
    skip_parts = {
        ".jingting_jobs",
        "sources",
        "burned_final",
        "final_release",
        "replacement_recuts",
        "bad_dash_merge_20260619-064753",
        "bad_flv_and_corrupt_source_20260618-230407",
    }
    for dd in date_dirs(root, room, date, all_dates):
        if not os.path.isdir(dd):
            continue
        for cur, dirs, files in os.walk(dd):
            dirs[:] = [d for d in dirs if d not in skip_parts and not d.startswith(".")]
            if any(part in skip_parts for part in Path(cur).parts):
                continue
            for name in sorted(files):
                if not slice_rx.search(name):
                    continue
                f = os.path.join(cur, name)
                if os.path.exists(f.rsplit(".", 1)[0] + ".jingting.done"):
                    continue
                if not retry_failed and retry_marker_path(f).exists():
                    continue
                if find_srt(f):
                    out.append(f)
    return sorted(out)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("slice", nargs="?", help="single slice .flv/.mp4")
    p.add_argument("--provider", choices=["gemini", "agy"], default=os.getenv("JINGTING_PROVIDER", "agy"))
    p.add_argument("--daemon", action="store_true", help="watch for pending slices")
    p.add_argument("--once", action="store_true", help="one sweep over pending slices")
    p.add_argument("--room", default=ROOM)
    p.add_argument("--root", default=VIDEOS, help="videos root; host default is CloudDrive live-streaming")
    p.add_argument("--date", help="limit to one YYYY-MM-DD date dir")
    p.add_argument("--all-dates", action="store_true", help="scan all date dirs instead of latest only")
    p.add_argument("--retry-failed", action="store_true", help="include slices with existing .jingting.retry.json markers")
    p.add_argument("--sleep", type=int, default=60)
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    key = ""
    if args.provider == "gemini":
        key = gemini_key()
        if not key:
            log("no GEMINI_API_KEY")
            return 1

    if args.slice:
        log(process_slice(args.slice, args.provider, key=key, verbose=True))
        return 0

    if not args.daemon and not args.once:
        args.once = True

    if args.daemon:
        daemon_lock = Path("/opt/bilive/run") / f"jingting-{args.room}.daemon.lock"
        daemon_lock.parent.mkdir(parents=True, exist_ok=True)
        if not acquire_directory_lock(daemon_lock):
            log(f"[jingting] daemon already running lock={daemon_lock}")
            return 0
        log(f"[jingting] daemon provider={args.provider} root={args.root} room={args.room}")
        try:
            while True:
                try:
                    for f in pending_slices(args.root, args.room, args.date, args.all_dates, retry_failed=args.retry_failed):
                        log(f"[jingting] {os.path.basename(f)}: {process_slice(f, args.provider, key=key)}")
                except Exception as exc:
                    log(f"[jingting] daemon sweep failed: {type(exc).__name__}: {exc}")
                time.sleep(args.sleep)
        finally:
            release_directory_lock(daemon_lock)

    ps = pending_slices(args.root, args.room, args.date, args.all_dates, retry_failed=args.retry_failed)
    log(f"[jingting] provider={args.provider} root={args.root} room={args.room} pending={len(ps)}")
    for f in ps:
        log(f"[jingting] {os.path.basename(f)}: {process_slice(f, args.provider, key=key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
