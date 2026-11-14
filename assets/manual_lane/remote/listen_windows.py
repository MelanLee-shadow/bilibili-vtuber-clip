#!/usr/bin/env python3
"""listen_windows.py WINDOWS.json CUT.mp4 OUTDIR PROMPT.md MODELS.json [--reuse-only]

Blind audio dictation for subtitle doubts.  Each window may carry
  "win"    name, used in the output file name,
  "audio"  [start, end] seconds in the cut,
  "core"   [start, end] seconds of the BCUT utterances the window is built from,
  "tempo"  playback tempo for the excerpt (1.0 = normal).

KEY: each (window, model) call is keyed by sha256 of (excerpt start/end, tempo, model, prompt text), stored in the
record.  An OK record is reused only when its key equals the key of the call about to be made.  A record written before
keys existed is accepted when its own stored fields give the same key with the current prompt.

QUOTA: a 429 means the model's quota is exhausted.  The model is then marked exhausted for the rest of this run and
is not asked again in it.  The window records status=QUOTA with retry_at (now + Retry-After, or a default when the
header is missing).  On a later run a QUOTA record is retried only after retry_at has passed.  Other failures are
ERROR records, retried with a short backoff in the same run.

LEVELS: a model entry may carry "then": further model names of the same level (the slug).  They are asked in order and
the first answer is the level's record, with the name of the model that gave it.  A model that refuses for the account
or the route (not routed, no balance, no credential: see _refused) is not asked again for UNAVAILABLE_S, in this run and
in later ones; when no model of the level answers the record is QUOTA if one of them only asked to wait, else UNAVAILABLE
or ERROR, with what each model said under "tried".  An entry without "then" is asked exactly as before.

--reuse-only: never call a model.  Only OK records with a matching key are read; everything else is left missing.

Run on OCI3 as root with cpa.env loaded by the caller; the repo is read-only.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ENV = "MANUAL_LANE_REMOTE_REPO"   # the deployed repo, set by the lane (adapters.remote_python)
DEFAULT_QUOTA_WAIT_S = 1800   # a 429 without Retry-After: the longest wait before asking that model again
FIRST_QUOTA_WAIT_S = 60       # ... the first such wait; each further refusal within the hour waits five times longer
UNAVAILABLE_S = 1800          # a level's model that refused for the account or the route is skipped for this long


def _cpa_generate(prompt, model_name, mp3):
    """The production CPA call (imported lazily so the module can be read and tested without the repo)."""
    sys.path.insert(0, os.environ[REPO_ENV])
    from src.autoslice import cpa_gemini_client as cg
    prefix = Path(mp3).with_suffix('')
    suffix = hashlib.sha256(model_name.encode()).hexdigest()[:12]
    request_path = prefix.with_name(prefix.name + f'.{suffix}.request.json')
    body = cg.files_body(prompt=prompt, model=model_name, files=[Path(mp3)],
                         generation_config={'maxOutputTokens': 65536, 'responseMimeType': 'application/json'})
    request_path.write_text(json.dumps(body, ensure_ascii=False), encoding='utf-8')
    result = cg.generate_files(prompt=prompt, model=model_name, purpose="manual_lane_subtitle_listen",
                               files=[Path(mp3)], timeout_seconds=240)
    receipt_path = prefix.with_name(prefix.name + f'.{suffix}.receipt.json')
    receipt_path.write_text(json.dumps(dict(result.receipt), ensure_ascii=False), encoding='utf-8')
    return result.text


def excerpt(win, cut, audio_dir):
    a, b = win["audio"]
    tempo = float(win.get("tempo", 1.0))
    out = audio_dir / f"{win['win']}.mp3"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}",
           "-i", str(cut), "-vn", "-ac", "1", "-ar", "16000"]
    if tempo != 1.0:
        cmd += ["-af", f"atempo={tempo}"]
    cmd += ["-b:a", "64k", str(out)]
    subprocess.run(cmd, check=True)
    return out


def prompt_for(win, prompt_tpl):
    tempo = float(win.get("tempo", 1.0))
    lead = (win["core"][0] - win["audio"][0]) / tempo
    tail = (win["audio"][1] - win["core"][1]) / tempo
    return prompt_tpl.replace("{lead}", f"{lead:.1f}").replace("{tail}", f"{tail:.1f}")


def key_for(audio, tempo, model_name, slug, prompt):
    payload = {"audio": [round(float(x), 3) for x in audio], "tempo": float(tempo), "model": model_name,
               "slug": slug, "prompt": prompt}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _read(dest):
    return json.loads(dest.read_text(encoding="utf-8")) if dest.exists() else None


def reusable(dest, win, model, prompt):
    """True only for an OK record of the same call (see the module docstring)."""
    prev = _read(dest)
    if prev is None or prev.get("status") != "OK":
        return False
    want = key_for(win["audio"], win.get("tempo", 1.0), model["name"], model["slug"], prompt)
    if prev.get("key") is not None:
        return prev["key"] == want
    legacy = key_for(prev["audio"], prev["tempo"], prev["model"], prev["slug"], prompt)
    return legacy == want


def model_wait_until(outdir, model, now):
    """Latest retry_at of this model's QUOTA records that is still in the future (0 when none)."""
    latest = 0.0
    for p in outdir.glob(f"*.{model['slug']}.json"):
        rec = _read(p)
        if (rec and rec.get("status") == "QUOTA" and rec.get("model") == model["name"]
                and float(rec.get("retry_at", 0)) > now):
            latest = max(latest, float(rec["retry_at"]))
    return latest


def recent_refusals(outdir, model, now):
    """How many times in a row this model answered 429 within the last hour (the longest such chain on record)."""
    longest = 0
    for p in outdir.glob(f"*.{model['slug']}.json"):
        rec = _read(p)
        if (rec and rec.get("status") == "QUOTA" and str(rec.get("error", "")).startswith("429")
                and rec.get("model") == model["name"] and now - 3600 <= float(rec.get("refused_at", 0)) <= now):
            longest = max(longest, int(rec.get("refusals", 1)))
    return longest


def quota_wait_s(refusals):
    """How long to wait after a 429 that names no Retry-After.  A rate limit clears within a minute, an exhausted
    daily quota does not, and the reply does not say which: the first refusal waits FIRST_QUOTA_WAIT_S and every
    further one in a row multiplies the wait by five, up to DEFAULT_QUOTA_WAIT_S."""
    return min(DEFAULT_QUOTA_WAIT_S, FIRST_QUOTA_WAIT_S * 5 ** min(refusals, 6))


def _is_quota(exc):
    return getattr(exc, "code", None) == 429 or getattr(exc, "http_status", None) == 429


def _refused(exc):
    """The account or the route refuses this model (not routed, no balance, no credential): asking again cannot help."""
    code = getattr(exc, "code", None)
    return isinstance(code, int) and (code == 503 or 400 <= code < 500 and code not in (408, 429))


def one(job, *, outdir, prompt_tpl, state, clock=time.time, **how):
    """One window heard by one level.  A level with one model is asked as `_ask` asks; see LEVELS for several."""
    win, model = job
    names = [model["name"], *model.get("then", [])]
    if len(names) == 1:
        return _ask(job, outdir=outdir, prompt_tpl=prompt_tpl, state=state, clock=clock, **how)
    slug = model["slug"]
    dest, down = outdir / f"{win['win']}.{slug}.json", outdir / ".unavailable"
    prompt = prompt_for(win, prompt_tpl)
    if any(reusable(dest, win, {"name": name, "slug": slug}, prompt) for name in names):
        return dest.name, "reused"
    tried, waiting, last = [], None, None
    for name in names:
        with state["lock"]:
            until = float((_read(down) or {}).get(name, 0))
        if until > clock():
            tried.append({"model": name, "status": "UNAVAILABLE", "error": "refused earlier, not asked"})
            continue
        status = _ask((win, {"name": name, "slug": slug}), outdir=outdir, prompt_tpl=prompt_tpl, state=state,
                      clock=clock, refusable=True, **how)[1]
        if status not in ("ERROR", "UNAVAILABLE", "QUOTA", "quota_wait"):
            return dest.name, status        # answered, or not to be asked at all (reuse only, dispatch unknown)
        last = _read(dest)
        if last["status"] == "OK":          # an older good answer of this window was kept (_write_quota): it stays
            return dest.name, status
        tried.append({k: last[k] for k in ("model", "status", "error", "http_status") if k in last})
        if last["status"] == "QUOTA" and (waiting is None or last["retry_at"] < waiting["retry_at"]):
            waiting = last
        if last["status"] == "UNAVAILABLE":
            with state["lock"]:
                down.write_text(json.dumps({**(_read(down) or {}), name: clock() + UNAVAILABLE_S}), encoding="utf-8")
    record = waiting or last or {
        "win": win["win"], "model": names[0], "slug": slug, "tempo": float(win.get("tempo", 1.0)),
        "audio": win["audio"], "core": win["core"], "status": "UNAVAILABLE", "lines": [], "error": ""}
    if waiting is None:
        record["status"] = "UNAVAILABLE" if all(t["status"] == "UNAVAILABLE" for t in tried) else "ERROR"
    dest.write_text(json.dumps({**record, "tried": tried}, ensure_ascii=False, indent=1), encoding="utf-8")
    return dest.name, record["status"]


def _ask(job, *, outdir, prompt_tpl, state, generate=_cpa_generate, clock=time.time, reuse_only=False, max_attempts=3,
         refusable=False):
    """state: {"exhausted": {model name: retry_at}, "lock": Lock}, shared by the jobs of one run.  `refusable`: the
    level has other models, so a refusal (_refused) is recorded as UNAVAILABLE at once instead of being asked again."""
    win, model = job
    slug = model["slug"]
    dest = outdir / f"{win['win']}.{slug}.json"
    prompt = prompt_for(win, prompt_tpl)
    if reusable(dest, win, model, prompt):
        return dest.name, "reused"
    if reuse_only:
        return dest.name, "missing"
    now = clock()
    with state["lock"]:
        exhausted_until = state["exhausted"].get(model["name"], 0.0)
    if exhausted_until <= now:
        exhausted_until = model_wait_until(outdir, model, now)
    if exhausted_until > now:
        prev = _read(dest)
        if prev is None or prev.get("status") != "OK":
            _write_quota(dest, win, model, prompt, retry_at=exhausted_until, retry_after=None, note="skipped: waiting")
        return dest.name, "quota_wait"
    tempo = float(win.get("tempo", 1.0))
    key = key_for(win["audio"], tempo, model["name"], slug, prompt)
    record = {"win": win["win"], "model": model["name"], "slug": slug, "tempo": tempo, "audio": win["audio"],
              "core": win["core"], "key": key, "status": "ERROR", "lines": [], "error": ""}
    if max_attempts == 1:
        # SSH can disconnect after dispatch. A repeated script must not buy an
        # unaccounted second observation while the first outcome is unknown.
        member = "." + hashlib.sha256(model["name"].encode()).hexdigest()[:8] if refusable else ""
        marker = outdir / f".{win['win']}.{slug}{member}.dispatched"
        try:
            with marker.open('x', encoding='utf-8') as stream:
                stream.write(key)
        except FileExistsError:
            return dest.name, "dispatch_unknown"
    for attempt in range(1, max_attempts + 1):
        try:
            text = generate(prompt, model["name"], win["mp3"])
            # Keep even a malformed provider response for the agent and its receipt (the last try's).
            (outdir / f"{win['win']}.{slug}.raw.txt").write_text(text, encoding="utf-8")
            start = text.find("{")
            parsed = json.loads(text[start:text.rfind("}") + 1])
            record.update(status="OK", lines=parsed.get("lines", []), attempts=attempt)
            break
        except Exception as exc:  # noqa: BLE001 - recorded as evidence
            if _is_quota(exc):
                retry_after = getattr(exc, "retry_after_seconds", None)
                refused = recent_refusals(outdir, model, clock())
                retry_at = clock() + (int(retry_after) if retry_after else quota_wait_s(refused))
                with state["lock"]:
                    state["exhausted"][model["name"]] = max(state["exhausted"].get(model["name"], 0.0), retry_at)
                _write_quota(dest, win, model, prompt, retry_at=retry_at, retry_after=retry_after,
                             note=f"429 on attempt {attempt}", refused_at=clock(), refusals=refused + 1)
                return dest.name, "QUOTA"
            record.update(error=f"{type(exc).__name__}: {str(exc)[:300]}", attempts=attempt)
            if refusable and _refused(exc):
                record.update(status="UNAVAILABLE", http_status=exc.code)
                break
            if max_attempts != 1:
                time.sleep(45 * attempt)  # transient provider errors: short backoff, same run only
    dest.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return dest.name, record["status"]


def _write_quota(dest, win, model, prompt, *, retry_at, retry_after, note, refused_at=None, refusals=None):
    prev = _read(dest)
    if prev is not None and prev.get("status") == "OK":
        return   # never overwrite a good answer with a quota note
    record = {"win": win["win"], "model": model["name"], "slug": model["slug"],
              "tempo": float(win.get("tempo", 1.0)), "audio": win["audio"], "core": win["core"],
              "key": key_for(win["audio"], win.get("tempo", 1.0), model["name"], model["slug"], prompt),
              "status": "QUOTA", "http_status": 429, "retry_after_seconds": retry_after,
              "retry_at": retry_at, "lines": [], "error": note}
    if refusals is not None:       # a real refusal (not a skipped window): when, and the how-many-th in a row
        record.update(refused_at=refused_at, refusals=refusals)
    dest.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


def run(windows, models, cut, outdir, prompt_tpl, *, excerpt_fn=excerpt, generate=_cpa_generate,
        clock=time.time, reuse_only=False, workers=16, max_attempts=3):
    audio_dir = outdir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for win in windows:
        if not reuse_only:
            win["mp3"] = str(excerpt_fn(win, cut, audio_dir))
        for model in models:
            jobs.append((win, model))
    state = {"exhausted": {}, "lock": threading.Lock()}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
        results = list(pool.map(lambda job: one(job, outdir=outdir, prompt_tpl=prompt_tpl, state=state,
                                                generate=generate, clock=clock, reuse_only=reuse_only,
                                                max_attempts=max_attempts), jobs))
    for name, status in results:
        print(name, status, flush=True)
    collected = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(outdir.glob("*.json"))
                 if p.name != "all.json"]
    (outdir / "all.json").write_text(json.dumps(collected, ensure_ascii=False, indent=1), encoding="utf-8")
    counts: dict[str, int] = {}
    for _, status in results:
        counts[status] = counts.get(status, 0) + 1
    print("LISTEN_DONE", len(jobs), "records", len(collected), json.dumps(counts, ensure_ascii=False), flush=True)
    return results


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    reuse_only = "--reuse-only" in argv
    max_attempts = 1 if "--one-attempt" in argv else 3
    windows_path, cut, outdir, prompt_path, models_path = (Path(a) for a in args[:5])
    windows = json.loads(windows_path.read_text(encoding="utf-8"))
    models = json.loads(models_path.read_text(encoding="utf-8"))
    prompt_tpl = prompt_path.read_text(encoding="utf-8")
    run(windows, models, cut, outdir, prompt_tpl, reuse_only=reuse_only, max_attempts=max_attempts)
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main(sys.argv))
