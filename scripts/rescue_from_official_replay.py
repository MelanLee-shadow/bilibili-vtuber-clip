#!/usr/bin/env python3
"""Rebuild lost recorder session media from the anchor's official replay VOD.

Scenario (first: 2026-07-25, sessions 19-20-00/19-50-00): recorder bytes died
in the clouddrive2 write cache (upload Fatal) while the chat sidecars
(.xml/.jsonl) and the full-session BCUT ASR cache survived. The official
replay covers the whole broadcast but stitches over stream hiccups, so wall
time and replay time are related by a PIECEWISE mapping (2026-07-25 measured:
4.3s swallowed between the 20:20 and 20:50 anchors). A single affine cut is
therefore refused by design; the reconstruction is per-session:

1. wall t0 per stem from .meta.json ``description.RecordStartTime``
   (fallback: the survived danmaku XML ``start_time`` attribute);
2. audio cross-correlation anchors against surviving sessions — absolute
   ground truth where recorder media still exists;
3. replay-danmaku unix timestamps (server clock) give a dense rough
   wall->replay mapping for the lost region; recorder-vs-server skew is
   measured from the survived session XML danmaku (rel time + unix ms);
4. per lost session the origin converges by iterated AUDIO-ONLY cuts:
   cut -> free BCUT ASR -> exact cue-text alignment against the session's
   cached BCUT SRT -> shift by the median start delta (<=0.35s converges);
   head-vs-tail delta drift proves the cut is skip-free inside;
5. one final VIDEO re-encode per stem, probe/duration/decode-scan gates,
   a final BCUT verification on the delivered bytes, sha256 + provenance;
6. ``--apply`` places files no-clobber into the canonical recording dir.

Runner state is never touched; candidates revive through the sanctioned
``scripts/revive_rejected_candidates.py`` channel.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import statistics
import subprocess
from datetime import datetime
from pathlib import Path


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, **kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wall_t0(canonical_dir: Path, stem: str) -> datetime:
    meta = canonical_dir / f"{stem}.meta.json"
    if meta.is_file():
        payload = json.loads(meta.read_text(encoding="utf-8"))
        raw = (payload.get("description") or {}).get("RecordStartTime") or payload.get(
            "RecordStartTime"
        )
        if raw:
            return datetime.fromisoformat(str(raw))
    xml = canonical_dir / f"{stem}.xml"
    if xml.is_file():
        head = xml.read_text(encoding="utf-8", errors="replace")[:8192]
        match = re.search(r'start_time="([^"]+)"', head)
        if match:
            return datetime.fromisoformat(match.group(1))
    raise SystemExit(f"REFUSE: no RecordStartTime for {stem} (meta+xml both unusable)")


def _extract_pcm(path: Path, start: float, duration: float, out: Path) -> None:
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{duration:.3f}",
            "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", str(out),
        ]
    )


def _correlate(needle: Path, haystack: Path) -> tuple[float, float]:
    """Return (offset_seconds_of_needle_start_inside_haystack, peak_score)."""

    import numpy as np

    a = np.frombuffer(needle.read_bytes(), dtype=np.int16).astype(np.float64)
    b = np.frombuffer(haystack.read_bytes(), dtype=np.int16).astype(np.float64)
    a -= a.mean()
    b -= b.mean()
    n = len(a) + len(b)
    size = 1 << (n - 1).bit_length()
    fa = np.fft.rfft(a, size)
    fb = np.fft.rfft(b, size)
    corr = np.fft.irfft(fb * np.conj(fa), size)
    valid = corr[: max(1, len(b) - len(a) + 1)]
    peak = int(np.argmax(valid))
    denom = (
        float(np.sqrt(np.sum(a * a) * np.sum(b[peak: peak + len(a)] ** 2))) or 1.0
    )
    return peak / 8000.0, float(valid[peak] / denom)


_DM_RX = re.compile(r'<d p="([^"]+)"[^>]*>([^<]*)</d>')


def _replay_dm_points(dm_xml: Path) -> list[tuple[float, float]]:
    """(unix_seconds, replay_video_seconds) per replay danmaku."""

    points = []
    for match in _DM_RX.finditer(dm_xml.read_text(encoding="utf-8", errors="replace")):
        parts = match.group(1).split(",")
        try:
            video = float(parts[0])
            unix = float(parts[4])
        except (IndexError, ValueError):
            continue
        if unix > 1e9:
            points.append((unix, video))
    points.sort()
    return points


def _recorder_server_skew(canonical_dir: Path, stem: str, t0_epoch: float) -> float:
    """median(recorder_wall - server_unix) from a survived session's XML."""

    deltas = []
    text = (canonical_dir / f"{stem}.xml").read_text(
        encoding="utf-8", errors="replace"
    )
    for match in _DM_RX.finditer(text):
        parts = match.group(1).split(",")
        try:
            rel = float(parts[0])
            unix_ms = float(parts[4])
        except (IndexError, ValueError):
            continue
        if unix_ms > 1e12:
            deltas.append(t0_epoch + rel - unix_ms / 1000.0)
        if len(deltas) >= 800:
            break
    if len(deltas) < 50:
        raise SystemExit(f"REFUSE: too few timestamped danmaku in {stem}.xml")
    return statistics.median(deltas)


def _parse_srt(path: Path) -> list[tuple[float, str]]:
    cues = []
    block_rx = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3}) --> [^\n]+\n(.*?)(?:\n\n|\Z)",
        re.S,
    )
    for match in block_rx.finditer(path.read_text(encoding="utf-8", errors="replace")):
        start = (
            int(match.group(1)) * 3600
            + int(match.group(2)) * 60
            + int(match.group(3))
            + int(match.group(4)) / 1000.0
        )
        text = " ".join(match.group(5).split())
        if text:
            cues.append((start, text))
    return cues


def _bcut_delta(
    fresh_srt: Path, cached_srt: Path, *, min_matches: int
) -> tuple[float, float, int]:
    """(median_delta_s, head_tail_drift_s, matched) via exact cue-text blocks."""

    fresh = _parse_srt(fresh_srt)
    cached = _parse_srt(cached_srt)
    matcher = difflib.SequenceMatcher(
        a=[text for _, text in cached], b=[text for _, text in fresh], autojunk=False
    )
    deltas: list[tuple[float, float]] = []
    for block in matcher.get_matching_blocks():
        for index in range(block.size):
            cached_start, _ = cached[block.a + index]
            fresh_start, _ = fresh[block.b + index]
            deltas.append((cached_start, fresh_start - cached_start))
    if len(deltas) < min_matches:
        raise SystemExit(
            f"REFUSE: only {len(deltas)} matched BCUT cues "
            f"({fresh_srt.name} vs {cached_srt.name})"
        )
    values = [delta for _, delta in deltas]
    median = statistics.median(values)
    deltas.sort()
    half = len(deltas) // 2
    head = statistics.median([delta for _, delta in deltas[:half]])
    tail = statistics.median([delta for _, delta in deltas[half:]])
    return median, tail - head, len(deltas)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", required=True, help="official replay MP4")
    parser.add_argument("--replay-bvid", required=True)
    parser.add_argument("--replay-dm", required=True, help="replay danmaku XML")
    parser.add_argument("--canonical-dir", required=True)
    parser.add_argument(
        "--lost", required=True,
        help="comma-separated lost stems in broadcast order",
    )
    parser.add_argument(
        "--next-stem", required=True,
        help="surviving stem whose t0 ends the last lost session",
    )
    parser.add_argument(
        "--anchors", required=True,
        help="comma-separated surviving stems with media (audio ground truth)",
    )
    parser.add_argument("--bcut-cache-dir", required=True)
    parser.add_argument("--asr-client", required=True,
                        help="path to free_asr_client.py")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--sample-offset", type=float, default=20.0)
    parser.add_argument("--sample-seconds", type=float, default=90.0)
    parser.add_argument("--min-score", type=float, default=0.30)
    parser.add_argument("--converge-s", type=float, default=0.35)
    parser.add_argument("--drift-s", type=float, default=0.60)
    parser.add_argument("--min-cue-matches", type=int, default=30)
    parser.add_argument("--crf", type=int, default=19)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    replay = Path(args.replay)
    canonical = Path(args.canonical_dir)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    lost = [stem.strip() for stem in args.lost.split(",") if stem.strip()]
    anchors = [stem.strip() for stem in args.anchors.split(",") if stem.strip()]

    t0 = {stem: _wall_t0(canonical, stem) for stem in lost + anchors + [args.next_stem]}
    base = t0[lost[0]]

    # -- absolute audio ground truth on surviving sessions ------------------
    anchor_video: dict[str, float] = {}
    anchor_evidence = []
    for stem in anchors:
        needle = workdir / f"{stem}.needle.pcm"
        _extract_pcm(canonical / f"{stem}.mp4", args.sample_offset,
                     args.sample_seconds, needle)
        expected = (t0[stem] - base).total_seconds() + args.sample_offset
        window_start = max(0.0, expected - 120.0)
        haystack = workdir / f"{stem}.haystack.pcm"
        _extract_pcm(replay, window_start, 240.0 + args.sample_seconds, haystack)
        offset, score = _correlate(needle, haystack)
        if score < args.min_score:
            raise SystemExit(f"REFUSE: weak correlation {score:.3f} for {stem}")
        video_at_t0 = window_start + offset - args.sample_offset
        anchor_video[stem] = video_at_t0
        anchor_evidence.append(
            {"stem": stem, "score": round(score, 4),
             "video_at_t0_s": round(video_at_t0, 3)}
        )
        print(f"anchor {stem}: score={score:.3f} video_at_t0={video_at_t0:.3f}s")

    # -- rough origins for lost sessions from replay-danmaku unix clock -----
    points = _replay_dm_points(Path(args.replay_dm))
    if len(points) < 200:
        raise SystemExit("REFUSE: too few replay danmaku points")
    skew = _recorder_server_skew(canonical, anchors[0], t0[anchors[0]].timestamp())
    print(f"recorder-vs-server skew: {skew:+.3f}s ({len(points)} replay dm points)")

    def rough_video(wall: datetime) -> float:
        u = wall.timestamp() - skew
        nearby = [video + (u - unix) for unix, video in points if abs(unix - u) <= 25.0]
        if len(nearby) < 5:
            raise SystemExit(f"REFUSE: no replay danmaku near {wall.isoformat()}")
        return statistics.median(nearby)

    # -- converge each lost session origin against its cached BCUT ----------
    order = lost + [args.next_stem]
    origin_video: dict[str, float] = {}
    convergence_evidence: dict[str, list[dict]] = {}
    for stem in lost:
        cached_srt = Path(args.bcut_cache_dir) / f"{stem}.bcut.srt"
        if not cached_srt.is_file():
            raise SystemExit(f"REFUSE: cached BCUT missing: {cached_srt}")
        wall_duration = (
            t0[order[order.index(stem) + 1]] - t0[stem]
        ).total_seconds()
        estimate = rough_video(t0[stem])
        rounds = []
        for attempt in range(4):
            probe_audio = workdir / f"{stem}.probe{attempt}.m4a"
            _run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{estimate:.3f}", "-i", str(replay),
                    "-t", f"{wall_duration:.3f}",
                    "-vn", "-c:a", "aac", "-b:a", "96k", str(probe_audio),
                ]
            )
            fresh_srt = workdir / f"{stem}.probe{attempt}.srt"
            _run(
                [
                    "python3", args.asr_client, str(probe_audio),
                    "--provider", "bcut", "--srt", str(fresh_srt),
                ]
            )
            median, drift, matched = _bcut_delta(
                fresh_srt, cached_srt, min_matches=args.min_cue_matches
            )
            rounds.append(
                {"attempt": attempt, "estimate_s": round(estimate, 3),
                 "median_delta_s": round(median, 3),
                 "head_tail_drift_s": round(drift, 3), "matched_cues": matched}
            )
            print(
                f"{stem} round {attempt}: estimate={estimate:.3f} "
                f"delta={median:+.3f}s drift={drift:+.3f}s matched={matched}"
            )
            if abs(drift) > args.drift_s:
                raise SystemExit(
                    f"REFUSE: internal skip inside {stem} cut "
                    f"(head-tail drift {drift:+.3f}s) — needs piecewise split"
                )
            if abs(median) <= args.converge_s:
                break
            estimate += median
        else:
            raise SystemExit(f"REFUSE: {stem} origin failed to converge")
        origin_video[stem] = estimate
        convergence_evidence[stem] = rounds

    # -- cut boundaries: each session ends where the next one starts --------
    end_video: dict[str, float] = {}
    for index, stem in enumerate(lost):
        next_stem = order[index + 1]
        end_video[stem] = (
            origin_video[next_stem]
            if next_stem in origin_video
            else anchor_video[next_stem]
        )
        if next_stem not in origin_video and next_stem not in anchor_video:
            raise SystemExit(f"REFUSE: no end boundary authority for {stem}")

    results = []
    for stem in lost:
        start = origin_video[stem]
        end = end_video[stem]
        duration = end - start
        wall_duration = (
            t0[order[order.index(stem) + 1]] - t0[stem]
        ).total_seconds()
        if abs(duration - wall_duration) > 6.0:
            print(
                f"note: {stem} video duration {duration:.1f}s vs wall "
                f"{wall_duration:.1f}s (replay swallowed "
                f"{wall_duration - duration:+.1f}s at the boundary)"
            )
        if not (600.0 <= duration <= 2100.0):
            raise SystemExit(
                f"REFUSE: implausible duration {duration:.1f}s for {stem}"
            )
        out = workdir / f"{stem}.mp4"
        print(f"cut {stem}: replay [{start:.3f} .. {end:.3f}] ({duration:.1f}s)")
        _run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.3f}", "-i", str(replay), "-t", f"{duration:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
                str(out),
            ]
        )
        probe = json.loads(
            _run(
                [
                    "ffprobe", "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", str(out),
                ]
            ).stdout.decode()
        )
        codecs = {s.get("codec_type") for s in probe.get("streams", [])}
        got = float(probe["format"]["duration"])
        if codecs != {"video", "audio"} or abs(got - duration) > 2.0:
            raise SystemExit(
                f"REFUSE: cut probe failed for {stem} (streams={codecs}, "
                f"duration={got:.2f} vs {duration:.2f})"
            )
        scan = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-v", "error", "-i", str(out),
                "-map", "0:v:0", "-f", "rawvideo", "-y", "/dev/null",
            ],
            capture_output=True, text=True, timeout=3600,
        )
        bad = [
            line for line in scan.stderr.splitlines()
            if re.search(r"Invalid NAL|missing picture|error while decoding|corrupt", line)
        ]
        if scan.returncode != 0 or bad:
            raise SystemExit(f"REFUSE: decode scan failed for {stem}: {bad[:2]}")
        final_srt = workdir / f"{stem}.final-verify.srt"
        _run(
            [
                "python3", args.asr_client, str(out),
                "--provider", "bcut", "--srt", str(final_srt),
            ]
        )
        median, drift, matched = _bcut_delta(
            final_srt,
            Path(args.bcut_cache_dir) / f"{stem}.bcut.srt",
            min_matches=args.min_cue_matches,
        )
        print(
            f"final verify {stem}: delta={median:+.3f}s drift={drift:+.3f}s "
            f"matched={matched}"
        )
        if abs(median) > args.converge_s or abs(drift) > args.drift_s:
            raise SystemExit(
                f"REFUSE: final BCUT verification failed for {stem} "
                f"(delta={median:+.3f}s drift={drift:+.3f}s)"
            )
        sha = _sha256(out)
        provenance = {
            "schema_version": "official-replay-session-rescue.v2",
            "stem": stem,
            "reason": "SOURCE_MEDIA_MISSING (recorder bytes lost in write cache)",
            "replay_bvid": args.replay_bvid,
            "replay_sha256": "sha256:" + _sha256(replay),
            "record_start_time": t0[stem].isoformat(),
            "cut_replay_interval_s": [round(start, 3), round(end, 3)],
            "recorder_server_skew_s": round(skew, 3),
            "anchor_evidence": anchor_evidence,
            "origin_convergence": convergence_evidence[stem],
            "final_bcut_verification": {
                "median_delta_s": round(median, 3),
                "head_tail_drift_s": round(drift, 3),
                "matched_cues": matched,
            },
            "encoder": f"libx264 veryfast crf={args.crf} + aac 192k",
            "output_sha256": "sha256:" + sha,
            "decode_scan": "PASS",
        }
        prov_path = workdir / f"{stem}.rescue-provenance.json"
        prov_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        results.append((stem, out, prov_path, t0[stem]))
        print(f"GATE_OK {stem} sha256={sha[:16]}… duration={got:.1f}s")

    if not args.apply:
        print("STAGED_ONLY (rerun with --apply to place into the canonical dir)")
        return 0

    for stem, out, prov_path, stem_t0 in results:
        target = canonical / f"{stem}.mp4"
        if target.exists():
            raise SystemExit(f"REFUSE: {target} already exists (no-clobber)")
        meta_target = canonical / f"{stem}.meta.json"
        if not meta_target.exists():
            # Deliberately NOT fabricated: adapter meta carries per-file source
            # fields we cannot honestly reproduce. Session identity is frozen
            # in the day state's segment_sessions mapping; the provenance
            # sidecar discloses the wall t0.
            print(f"note: {meta_target.name} absent (t0={stem_t0.isoformat()})")
        _run(["cp", str(prov_path), str(canonical / prov_path.name)])
        tmp = canonical / f".rescue-{stem}.mp4.partial"
        _run(["cp", str(out), str(tmp)])
        tmp.rename(target)
        print(f"APPLIED {target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
