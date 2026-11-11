#!/usr/bin/env python3
"""witness_asr.py CUT.mp4 OUTDIR DURATION_MS [PROVIDERS] : whole-clip ASR witnesses (MOSS, MAI) for subtitle doubts.

Calls the repo's own modules on OCI3 (read-only deployed repo).  MOSS through transcribe_moss_evidence: its native
segments, overlapping segments included (two people at once is evidence, not an error; TASK-11: the strict one-track
transcribe_moss raised MOSS_OVERLAP on every clip, so MOSS never reached a doubt).  MAI through transcribe_mai (its
phrases split at sentence ends by word timing).  Credentials are loaded by the caller's sudo wrapper (moss.env /
mai.env are sourced there, never read or printed by this script) and by the modules themselves.  Each provider writes
OUTDIR/<provider>.json with either
  {"status": "OK", "segments": [{"start_ms", "end_ms", "text", "speaker"}, ...], "input_sha256", "billed", ...}
or
  {"status": "UNAVAILABLE", "reason_code": "...", "message": "..."}  (no endpoint or keys).

One call per provider and cut: an OK result, or an UNAVAILABLE result from a completed HTTP 2xx response, is reused
when input_sha256 (the extracted audio) matches.  PROVIDERS: comma list, default "moss,mai".
"""
import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
REPO_ENV = "MANUAL_LANE_REMOTE_REPO"   # the deployed repo, set by the lane (adapters.remote_python)
# provider -> (module, function): MOSS by its evidence API (keeps overlap), MAI by its one-track API
PROVIDERS = {"moss": ("src.autoslice.moss_transcription", "transcribe_moss_evidence"),
             "mai": ("src.autoslice.mai_transcription", "transcribe_mai")}


def segments_of(meta):
    rows = []
    # MAI's one-track segments are its phrases split at sentence ends by word timing (closer to one BCUT sentence each);
    # MOSS's evidence has native segments only
    for seg in meta.get("segments") or meta.get("native_segments") or []:
        text = str(seg.get("text") or "").strip()
        if text:
            rows.append({"start_ms": int(seg["start_ms"]), "end_ms": int(seg["end_ms"]), "text": text,
                         "speaker": seg.get("speaker")})
    return rows


def billed(name, raw):
    """What the provider's own response says about the audio it processed (no key material is in these fields)."""
    if not isinstance(raw, dict):
        return {}
    if name == "mai":
        return {"duration_ms": raw.get("durationMilliseconds")}
    return {k: raw[k] for k in ("usage", "duration", "duration_ms", "audio_duration") if k in raw}


def provider_call(name):
    module, func = PROVIDERS[name]
    fn = getattr(importlib.import_module(module), func)
    if func == "transcribe_mai":
        return lambda audio, duration_ms: fn(audio, duration_ms=duration_ms)[1]
    return lambda audio, duration_ms: fn(audio, duration_ms=duration_ms)


def record(name, call, audio, outdir, duration_ms, input_sha):
    """Reuse a same-input OK or completed HTTP 2xx result without another call."""
    dest = outdir / f"{name}.json"
    if dest.exists():
        try:
            prev = json.loads(dest.read_text(encoding="utf-8"))
        except ValueError:
            prev = {}
        http_status = prev.get("http_status")
        terminal = (prev.get("status") == "OK" or
                    (prev.get("status") == "UNAVAILABLE" and type(http_status) is int and 200 <= http_status < 300))
        if terminal and prev.get("input_sha256") == input_sha:
            print(name, "REUSED", prev.get("segment_count", ""), flush=True)
            return prev
    started = time.monotonic()
    try:
        meta = call(audio, duration_ms)
        rows = segments_of(meta)
        result = {"status": "OK" if rows else "UNAVAILABLE", "segments": rows, "segment_count": len(rows),
                  "input_sha256": input_sha, "billed": billed(name, meta.get("raw_response")),
                  "elapsed_s": round(time.monotonic() - started, 2), "model": meta.get("model"),
                  "http_status": meta.get("http_status"),
                  "diagnostics": meta.get("diagnostics") or []}
        if not rows:
            result["reason_code"] = f"{name.upper()}_{meta.get('status', 'NO_SEGMENTS')}"
    except Exception as exc:  # noqa: BLE001 - the module's own error type carries reason_code
        meta = getattr(exc, "metadata", None) or {}
        result = {"status": "UNAVAILABLE", "reason_code": getattr(exc, "reason_code", None) or type(exc).__name__,
                  "message": str(exc)[:200], "input_sha256": input_sha, "billed": billed(name, meta.get("raw_response")),
                  "elapsed_s": round(time.monotonic() - started, 2), "http_status": meta.get("http_status")}
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(name, result["status"], result.get("reason_code", ""), result.get("segment_count", ""), flush=True)
    return result


def main(argv):
    cut_video, outdir, duration_ms = Path(argv[1]), Path(argv[2]), int(argv[3])
    providers = [p for p in (argv[4] if len(argv) > 4 else "moss,mai").split(",") if p]
    outdir.mkdir(parents=True, exist_ok=True)
    # Providers accept WAV/MP3/FLAC audio only (MAI rejects mp4), so extract the clip's audio first.
    audio = outdir / "audio.mp3"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(cut_video), "-vn", "-ac", "1",
                    "-ar", "16000", "-b:a", "64k", str(audio)], check=True)
    input_sha = hashlib.sha256(audio.read_bytes()).hexdigest()
    sys.path.insert(0, os.environ[REPO_ENV])
    for name in ("moss", "mai"):
        if name in providers:
            record(name, provider_call(name), audio, outdir, duration_ms, input_sha)
    print("WITNESS_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
