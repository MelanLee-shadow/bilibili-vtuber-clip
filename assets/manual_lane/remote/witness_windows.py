#!/usr/bin/env python3
"""witness_windows.py WINDOWS.json CUT.mp4 OUTDIR PROVIDERS [--reuse-only]

Window-level ASR witnesses (TASK-11): the doubt windows of step 3 (the same windows the Gemini listening uses, one
sentence each, stage_impl.listen_windows) transcribed by MAI-Transcribe-2 and MOSS Pro through the repo's evidence
APIs (transcribe_mai_evidence / transcribe_moss_evidence: the provider's native segments, overlap kept as evidence,
never forced into one track).  PROVIDERS is a comma list of mai, moss.

KEY: one call per (provider, excerpt bytes).  The excerpt is the window's audio cut from CUT.mp4 (mono 16 kHz mp3);
its sha256 names the cached result OUTDIR/cache/<provider>-<sha>.json.  An OK or NO_SPEECH result, or an answer the
parser rejected, is reused; a transport failure is recorded in this run's output and asked again on a later run.

USAGE: each new call records what the provider returned about the audio it billed: MAI `durationMilliseconds`, MOSS
any `usage` / `duration` fields of its response, plus the excerpt's own length.  OUTDIR/result-<name>.json holds the
records of this run; the last stdout line is `WITNESS_WINDOWS_DONE <n> {counts}`.

--reuse-only: never call a provider; only cached results are read, the rest are left missing.

Run on OCI3 as root with moss.env / mai.env loaded by the caller's sudo wrapper (never read or printed here); the
repo is read-only.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ENV = "MANUAL_LANE_REMOTE_REPO"   # the deployed repo, set by the lane (adapters.remote_python)
WORKERS = 4


def excerpt(win, cut, audio_dir):
    a, b = win["audio"]
    out = audio_dir / f"{win['win']}.mp3"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}",
                    "-i", str(cut), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out)], check=True)
    return out


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _provider_fn(name):
    sys.path.insert(0, os.environ[REPO_ENV])
    if name == "mai":
        from src.autoslice.mai_transcription import (
            transcribe_mai_evidence as fn,
        )
    elif name == "moss":
        from src.autoslice.moss_transcription import (
            transcribe_moss_evidence as fn,
        )
    else:
        raise ValueError(f"unknown provider {name}")
    return fn


def billed(name, raw):
    """What the provider's own response says about the audio it processed (no key material is in these fields)."""
    if not isinstance(raw, dict):
        return {}
    if name == "mai":
        return {"duration_ms": raw.get("durationMilliseconds")}
    out = {k: raw[k] for k in ("usage", "duration", "duration_ms", "audio_duration") if k in raw}
    return out


def segments_of(meta):
    rows = []
    for seg in meta.get("native_segments") or []:
        text = str(seg.get("text") or "").strip()
        if text:
            rows.append({"start_ms": int(seg["start_ms"]), "end_ms": int(seg["end_ms"]), "text": text,
                         "speaker": seg.get("speaker")})
    return rows


def call(name, mp3, duration_ms):
    fn = _provider_fn(name)
    started = time.monotonic()
    try:
        meta = fn(mp3, duration_ms=duration_ms)
    except Exception as exc:  # noqa: BLE001 - the module's own error type carries reason_code
        meta = getattr(exc, "metadata", None) or {}
        return {"status": "ERROR", "reason_code": getattr(exc, "reason_code", None) or type(exc).__name__,
                "message": str(exc)[:200], "elapsed_s": round(time.monotonic() - started, 2),
                "billed": billed(name, meta.get("raw_response")), "http_status": meta.get("http_status")}
    return {"status": meta.get("status", "OK"), "segments": segments_of(meta),
            "elapsed_s": round(time.monotonic() - started, 2), "billed": billed(name, meta.get("raw_response")),
            "model": meta.get("model"), "response_sha256": meta.get("response_sha256"),
            "diagnostics": meta.get("diagnostics") or []}


def run_one(win, name, cut, out, reuse_only):
    audio_dir = out / "audio"
    mp3 = excerpt(win, cut, audio_dir)
    digest = _sha(mp3)
    cache = out / "cache" / f"{name}-{digest}.json"
    dur_ms = int(round((win["audio"][1] - win["audio"][0]) * 1000))
    base = {"win": win["win"], "provider": name, "audio_sha256": digest, "excerpt_ms": dur_ms}
    if cache.exists():
        return dict(base, **json.loads(cache.read_text(encoding="utf-8")), reused=True)
    if reuse_only:
        return dict(base, status="MISSING", reused=False)
    rec = call(name, mp3, dur_ms)
    # a provider that answered (HTTP 200) with a response the evidence parser rejects (MOSS zero-length segment) would
    # answer the same bytes the same way and bill again: that answer is kept too; transport failures are retried
    if rec["status"] in ("OK", "NO_SPEECH") or rec.get("http_status") == 200:
        cache.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return dict(base, **rec, reused=False)


def main(argv):
    windows = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    cut, out = Path(argv[2]), Path(argv[3])
    providers = [p for p in argv[4].split(",") if p]
    reuse_only = "--reuse-only" in argv
    (out / "audio").mkdir(parents=True, exist_ok=True)
    (out / "cache").mkdir(parents=True, exist_ok=True)
    jobs = [(w, p) for w in windows for p in providers]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        records = list(pool.map(lambda job: run_one(job[0], job[1], cut, out, reuse_only), jobs))
    name = Path(argv[1]).stem
    (out / f"result-{name}.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = {"reused": sum(1 for r in records if r.get("reused"))}
    for r in records:
        if not r.get("reused"):
            counts[r["status"]] = counts.get(r["status"], 0) + 1
    counts["new_audio_ms"] = sum(r["excerpt_ms"] for r in records if not r.get("reused") and r["status"] != "MISSING")
    print(f"WITNESS_WINDOWS_DONE {len(records)} {json.dumps(counts)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
