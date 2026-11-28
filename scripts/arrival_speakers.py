#!/usr/bin/env python3
"""Who speaks when in a voice collab, from the order the same words reach two streams.

In a collab over a voice call every participant streams their own picture and sound.  A line reaches its speaker's own
stream at once and every other stream after the call's latency.  So against a guest's public recording of the same
session, the lag (guest's stream minus ours) of a stretch of speech takes one value per speaker: lowest for the
recording's owner, highest for our host, in between for anyone else.  Measured on the trio (李豆沙 / 礼墨Sumi /
安晚awa, 礼墨's recording): three lag groups about 130 ms and 350 ms apart, jitter about 15 ms, the winning lag's
normalized correlation above 0.9 on plain speech; the per-frame sequence showed the turn changes the channel owner had
marked inside single subtitle lines.

usage: arrival_speakers.py --cut CUT --guest GUEST_AUDIO --out OUT.json [--people N] [--source SRC --start-s A --end-s B]

Finds CUT (the clip, the clock its subtitles use) in GUEST_AUDIO by its loudness contour (no clocks needed), groups the
lags of its frames, then scores every frame at each group's lag.  When the clip shows fewer groups than --people (a
speaker with one short line has none), the groups are looked for on SRC from A-margin to B+margin and moved onto the
clip's own lags; when that finds no more (two people on one sound path are one group), the clip's own groups stand.
OUT.json: {status, located, centres_ms, hop_ms, frames: [[t_ms, who]...], runs: [{start_ms, end_ms, cluster}]} with
times from the clip's start; cluster k is the k-th lowest lag.  status NOT_FOUND when the clip is not in the guest's
audio, FEW_GROUPS when fewer than two lag groups were found (one side speaks: nothing to tell apart).  Who each group
is, and how many groups a session should have, is the caller's (manual_lane/arrival.py).
Needs ffmpeg and numpy only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np

SR = 16000
ENV_SR = 8000
ENV_HOP = 80                 # 10 ms at ENV_SR
MARGINS = (120.0, 300.0, 600.0)
LOCATE_MIN_Z = 5.0           # the contour match must stand this many deviations above the other offsets (a 70 s clip
                             # in a 4.5 h recording scored 7.3-7.9, minutes-long ones 12-20)
SAME_AUDIO_MIN = 0.2         # and this share of the clip's voiced frames must correlate sharply at some lag there
PHAT_WIN, PHAT_HOP, PHAT_SEARCH = 0.6, 0.1, 1.0
PHAT_MIN_SHARP = 3.0
GROUP_GAP_MS = 60.0
GROUP_MIN_SHARE = 0.03
NCC_WIN, NCC_HOP, NCC_JITTER = 0.4, 0.05, 0.02
NCC_MIN, NCC_MARGIN = 0.3, 0.1
RUN_MIN_MS = 200
BAND = (300.0, 3400.0)


def _decode(path: str, rate: int, start: float | None = None, dur: float | None = None, voice_band: bool = False) -> np.ndarray:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}"]
    cmd += ["-i", path, "-vn", "-ac", "1", "-ar", str(rate)]
    if voice_band:
        cmd += ["-af", f"highpass=f={BAND[0]:.0f},lowpass=f={BAND[1]:.0f}"]
    cmd += ["-f", "s16le", "pipe:1"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def _band(x: np.ndarray) -> np.ndarray:
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec[(f < BAND[0]) | (f > BAND[1])] = 0
    return np.fft.irfft(spec, len(x)).astype(np.float32)


def contour(x: np.ndarray) -> np.ndarray:
    """Log loudness every 10 ms of voice-band audio sampled at ENV_SR."""
    n = len(x) // ENV_HOP
    return np.log(np.sqrt((x[:n * ENV_HOP].reshape(n, ENV_HOP) ** 2).mean(axis=1)) + 1e-5).astype(np.float32)


def guest_contour(guest: str) -> np.ndarray:
    cache = Path(guest + ".contour.npy")
    if cache.is_file() and cache.stat().st_mtime >= Path(guest).stat().st_mtime:
        return np.load(cache)
    env = contour(_decode(guest, ENV_SR, voice_band=True))
    np.save(cache, env)
    return env


def locate(part: np.ndarray, whole: np.ndarray) -> tuple[float, float]:
    """Where `part` (a contour) lies in `whole`: (seconds from the start of whole, z-score of the match)."""
    if len(part) < 500 or len(whole) < len(part):
        return 0.0, 0.0
    a = part - part.mean()
    n = 1 << int(np.ceil(np.log2(len(whole) + len(a))))
    corr = np.fft.irfft(np.fft.rfft(whole - whole.mean(), n) * np.conj(np.fft.rfft(a, n)), n)[:len(whole) - len(a) + 1]
    k = int(np.argmax(corr))
    return k * ENV_HOP / ENV_SR, float((corr[k] - corr.mean()) / (corr.std() + 1e-9))


def _phat(seg: np.ndarray, gseg: np.ndarray) -> tuple[float, float]:
    n = len(seg)
    nfft = 1 << int(np.ceil(np.log2(len(gseg) + n)))
    r = np.conj(np.fft.rfft(seg * np.hanning(n), nfft)) * np.fft.rfft(gseg, nfft)
    r /= np.abs(r) + 1e-9
    f = np.fft.rfftfreq(nfft, 1 / SR)
    r[(f < BAND[0]) | (f > BAND[1])] = 0
    c = np.fft.irfft(r, nfft)[:len(gseg) - n + 1]
    k = int(np.argmax(c))
    rest = np.delete(c, range(max(0, k - 80), min(len(c), k + 81)))
    return k / SR, float(c[k]) / (float(np.abs(rest).max()) + 1e-9)


def lag_groups(lags_ms: list[float]) -> list[float]:
    """Centres (ms, ascending) of the lag groups: the peaks of the lags' 10 ms histogram, highest first, each at least
    GROUP_GAP_MS from a higher one and holding GROUP_MIN_SHARE of the frames (and three of them) within half that gap.
    Peaks, not chains of neighbours: over minutes stray lags fill the space between two groups 130 ms apart."""
    if not lags_ms:
        return []
    xs = np.asarray(lags_ms)
    lo = float(xs.min())
    counts = np.bincount(((xs - lo) / 10).astype(int))
    smooth = np.convolve(counts, [1, 2, 1], mode="same")
    need = max(3.0, GROUP_MIN_SHARE * len(xs))
    half = GROUP_GAP_MS / 2
    found: list[float] = []
    for k in np.argsort(-smooth):
        at = lo + 10 * int(k) + 5
        if smooth[k] == 0 or any(abs(at - c) < GROUP_GAP_MS for c in found):
            continue
        near = xs[np.abs(xs - at) <= half]
        if len(near) >= need:
            found.append(float(np.median(near)))
    return sorted(found)


def phat_lags(ours: np.ndarray, guest: np.ndarray, pad: float) -> tuple[list[float], int]:
    """The confident frames' lags (ms) and how many voiced frames were tried; `guest` holds the same stretch as `ours`
    with `pad` seconds more on each side."""
    rms = float(np.sqrt((ours ** 2).mean())) + 1e-9
    n = int(PHAT_WIN * SR)
    lags, tried = [], 0
    for start in range(0, len(ours) - n, int(PHAT_HOP * SR)):
        seg = ours[start:start + n]
        g0 = start + int((pad - PHAT_SEARCH) * SR)
        gseg = guest[g0:g0 + n + int(2 * PHAT_SEARCH * SR)]
        if float(np.sqrt((seg ** 2).mean())) < 0.15 * rms or len(gseg) < n + SR:
            continue
        tried += 1
        at, sharp = _phat(seg, gseg)
        if sharp >= PHAT_MIN_SHARP:
            lags.append((at - PHAT_SEARCH) * 1000)
    return lags, tried


def fit_groups(wide: list[float], lags_ms: list[float]) -> tuple[list[float], float] | None:
    """The wide stretch's groups moved onto the clip's own lags: the shift (the two stretches were located separately,
    so their zero points differ) that puts most of the clip's lags within GROUP_GAP_MS / 2 of a group.  None unless two
    groups each take three of the clip's lags there: one group alone fits every shift that reaches it."""
    xs = np.asarray(lags_ms)
    if len(wide) < 2 or len(xs) < 6:
        return None
    half = GROUP_GAP_MS / 2
    centres_arr = np.asarray(wide)
    best: tuple[int, float, float] = (0, 0.0, 0.0)       # hits, -miss distance, shift
    for shift in np.arange(-1500.0, 1500.0, 5.0):
        dist = np.abs(xs[:, None] - (centres_arr[None, :] + shift)).min(axis=1)
        hit = int((dist <= half).sum())
        score = (hit, -float(dist[dist <= half].sum()), float(shift))
        if score[:2] > best[:2]:
            best = score
    per = [xs[np.abs(xs - (c + best[2])) <= half] for c in wide]
    if sum(1 for near in per if len(near) >= 3) < 2:
        return None
    # the populated groups say exactly where the zero point is; an unpopulated one follows them
    shift = float(np.mean([np.median(near) - c for c, near in zip(wide, per) if len(near) >= 3]))
    moved = [float(np.median(near)) if len(near) >= 3 else c + shift for c, near in zip(wide, per)]
    return moved, best[0] / len(xs)


def frames(ours: np.ndarray, guest: np.ndarray, pad: float, centres_ms: list[float], first: float, last: float) -> list[list[int]]:
    """[[t_ms from `first`, who]...] for the frames of ours[first:last] (seconds): who is the group whose lag gives the
    highest normalized correlation, -1 when no group stands out or the frame is quiet."""
    a_all, g_all = _band(ours), _band(guest)
    rms = float(np.sqrt((a_all ** 2).mean())) + 1e-9
    n, j = int(NCC_WIN * SR), int(NCC_JITTER * SR)
    ones = np.ones(n, dtype=np.float32)
    out = []
    t = first
    while t + NCC_WIN <= last:
        s0 = int(t * SR)
        a = a_all[s0:s0 + n]
        ea = float(np.sqrt((a ** 2).sum()))
        who = -1
        if len(a) == n and ea / np.sqrt(n) > 0.2 * rms:
            scores = []
            for c in centres_ms:
                g0 = s0 + int((pad + c / 1000) * SR) - j
                g = g_all[g0:g0 + n + 2 * j]
                if g0 < 0 or len(g) < n + 2 * j:
                    scores.append(0.0)
                    continue
                eg = np.sqrt(np.convolve(g ** 2, ones, mode="valid")) + 1e-9
                scores.append(float((np.correlate(g, a, mode="valid") / (eg * ea + 1e-9)).max()))
            order = sorted(scores, reverse=True)
            if order and order[0] >= NCC_MIN and (len(order) == 1 or order[0] - order[1] >= NCC_MARGIN):
                who = int(np.argmax(scores))
        out.append([round((t - first) * 1000), who])
        t += NCC_HOP
    return out


def runs(rows: list[list[int]], hop_ms: int, frame_ms: int) -> list[dict[str, int]]:
    """Stretches one group speaks: consecutive frames of the same group (an undecided frame between two frames of the
    same group joins them), at least RUN_MIN_MS long.  A frame is placed at its middle."""
    out: list[dict[str, int]] = []
    last_who, last_t = -1, -10 ** 9
    for t, who in rows:
        if who < 0:
            continue
        mid = t + frame_ms // 2
        if out and who == last_who and t - last_t <= 2 * hop_ms:
            out[-1]["end_ms"] = mid + hop_ms // 2
        else:
            out.append({"start_ms": mid - hop_ms // 2, "end_ms": mid + hop_ms // 2, "cluster": who})
        last_who, last_t = who, t
    return [r for r in out if r["end_ms"] - r["start_ms"] >= RUN_MIN_MS]


def _located(part: str, whole: np.ndarray, start: float | None = None, dur: float | None = None) -> tuple[float, float]:
    return locate(contour(_decode(part, ENV_SR, start, dur, voice_band=True)), whole)


def analyse(cut: str, guest: str, people: int, source: str | None = None, start: float = 0.0, end: float = 0.0) -> dict[str, object]:
    whole = guest_contour(guest)
    doc: dict[str, object] = {"status": "NOT_FOUND", "hop_ms": round(NCC_HOP * 1000), "frame_ms": round(NCC_WIN * 1000)}
    pad = PHAT_SEARCH + 1.0
    at, z = _located(cut, whole)
    doc["located"] = {"guest_s": round(at, 2), "z": round(z, 1)}
    if z < LOCATE_MIN_Z or at < pad:
        return doc
    ours = _decode(cut, SR)
    theirs = _decode(guest, SR, at - pad, len(ours) / SR + 2 * pad)
    lags, tried = phat_lags(_band(ours), _band(theirs), pad)
    doc["located"]["sharp_share"] = round(len(lags) / max(1, tried), 2)     # type: ignore[index]
    if len(lags) < SAME_AUDIO_MIN * tried:
        return doc
    found = lag_groups(lags)
    doc["centres_ms"], doc["centres_from"] = [round(c, 1) for c in found], "clip"
    if len(found) < people and source:
        for margin in MARGINS:      # a speaker with one short line in the clip has a group on the wider stretch
            lo = max(0.0, start - margin)
            w_at, w_z = _located(source, whole, lo, end + margin - lo)
            if w_z < LOCATE_MIN_Z or w_at < pad:
                break
            w_ours = _decode(source, SR, lo, end + margin - lo)
            wide = lag_groups(phat_lags(_band(w_ours), _band(_decode(guest, SR, w_at - pad, len(w_ours) / SR + 2 * pad)), pad)[0])
            fitted = fit_groups(wide, lags) if len(wide) >= people else None
            if fitted:
                found = fitted[0]
                doc.update(centres_ms=[round(c, 1) for c in found], centres_from=f"source ±{margin:.0f}s",
                           fit_share=round(fitted[1], 2))
                break
    if len(found) < 2:
        doc["status"] = "FEW_GROUPS"
        return doc
    rows = frames(ours, theirs, pad, found, 0.0, len(ours) / SR)
    doc.update(status="OK", frames=rows, decided=sum(1 for r in rows if r[1] >= 0),
               runs=runs(rows, round(NCC_HOP * 1000), round(NCC_WIN * 1000)))
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cut", required=True)
    ap.add_argument("--guest", required=True)
    ap.add_argument("--source")
    ap.add_argument("--start-s", type=float, default=0.0)
    ap.add_argument("--end-s", type=float, default=0.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--people", type=int, default=2)
    args = ap.parse_args()
    doc = analyse(args.cut, args.guest, args.people, args.source, args.start_s, args.end_s)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False) + "\n", encoding="utf-8")
    print("ARRIVAL", doc["status"], json.dumps({k: doc.get(k) for k in ("located", "centres_ms", "decided")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
