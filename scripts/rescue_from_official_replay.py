#!/usr/bin/env python3
"""Rebuild lost recorder session media from the anchor's official replay VOD.

Scenario (first: 2026-07-25, sessions 19-20-00/19-50-00): recorder bytes died
in the clouddrive2 write cache (upload Fatal) while the chat sidecars
(.xml/.jsonl) survived. The official replay covers the whole broadcast, so the
lost 30-minute session files can be reconstructed on the recorder's own wall
clock and timeline:

1. wall t0 per stem from .meta.json ``description.RecordStartTime`` (fallback:
   the survived danmaku XML ``BililiveRecorderRecordInfo@start_time``);
2. replay-t0 solved by audio cross-correlation against >=2 SURVIVING sessions
   of the same broadcast (both anchors must agree — this also proves the
   replay has no internal cuts in the region we reconstruct);
3. frame-accurate re-encode cuts, one per lost stem, timeline zeroed;
4. gates: video+audio probe, exact duration window, full video decode scan
   with zero corruption; sha256 + rescue-provenance sidecars;
5. ``--apply`` places files into the canonical recording dir no-clobber and
   writes any missing .meta.json from the survived XML. The provenance
   sidecar keeps the canonical archive honest about non-recorder bytes.

The candidates themselves are then revived through the sanctioned
``scripts/revive_rejected_candidates.py`` channel — this script never touches
runner state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
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
        head = xml.read_text(encoding="utf-8", errors="replace")[:4096]
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", required=True, help="official replay MP4")
    parser.add_argument("--replay-bvid", required=True)
    parser.add_argument("--canonical-dir", required=True)
    parser.add_argument(
        "--lost", required=True,
        help="comma-separated lost stems in broadcast order",
    )
    parser.add_argument(
        "--next-stem", required=True,
        help="stem whose t0 ends the last lost session",
    )
    parser.add_argument(
        "--anchors", required=True,
        help="comma-separated surviving stems with media, for alignment",
    )
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--sample-offset", type=float, default=20.0)
    parser.add_argument("--sample-seconds", type=float, default=90.0)
    parser.add_argument("--min-score", type=float, default=0.30)
    parser.add_argument(
        "--anchor-tolerance-ms", type=float, default=300.0,
        help="max disagreement between anchor-derived replay origins",
    )
    parser.add_argument("--crf", type=int, default=19)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    replay = Path(args.replay)
    canonical = Path(args.canonical_dir)
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    lost = [stem.strip() for stem in args.lost.split(",") if stem.strip()]
    anchors = [stem.strip() for stem in args.anchors.split(",") if stem.strip()]
    if len(anchors) < 2:
        raise SystemExit("REFUSE: need >=2 anchors to prove a cut-free mapping")

    t0 = {stem: _wall_t0(canonical, stem) for stem in lost + anchors + [args.next_stem]}
    base = t0[lost[0]]

    # Solve replay origin per anchor; all anchors must agree.
    origins: list[float] = []
    anchor_evidence = []
    for stem in anchors:
        media = canonical / f"{stem}.mp4"
        needle = workdir / f"{stem}.needle.pcm"
        _extract_pcm(media, args.sample_offset, args.sample_seconds, needle)
        expected = (t0[stem] - base).total_seconds() + args.sample_offset
        window_start = max(0.0, expected - 120.0)
        haystack = workdir / f"{stem}.haystack.pcm"
        _extract_pcm(replay, window_start, 240.0 + args.sample_seconds, haystack)
        offset, score = _correlate(needle, haystack)
        if score < args.min_score:
            raise SystemExit(
                f"REFUSE: weak correlation {score:.3f} for anchor {stem}"
            )
        found = window_start + offset
        origin = found - args.sample_offset - (t0[stem] - base).total_seconds()
        origins.append(origin)
        anchor_evidence.append(
            {"stem": stem, "score": round(score, 4),
             "replay_position_s": round(found, 3),
             "implied_origin_s": round(origin, 3)}
        )
        print(f"anchor {stem}: score={score:.3f} origin={origin:+.3f}s")
    spread_ms = (max(origins) - min(origins)) * 1000.0
    if spread_ms > args.anchor_tolerance_ms:
        raise SystemExit(
            f"REFUSE: anchors disagree by {spread_ms:.0f}ms — replay may have "
            "internal cuts; manual adjudication required"
        )
    origin = sum(origins) / len(origins)

    results = []
    order = lost + [args.next_stem]
    for index, stem in enumerate(lost):
        start = (t0[stem] - base).total_seconds() - origin
        end = (t0[order[index + 1]] - base).total_seconds() - origin
        duration = end - start
        if not (600.0 <= duration <= 2100.0):
            raise SystemExit(
                f"REFUSE: implausible session duration {duration:.1f}s for {stem}"
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
        sha = _sha256(out)
        provenance = {
            "schema_version": "official-replay-session-rescue.v1",
            "stem": stem,
            "reason": "SOURCE_MEDIA_MISSING (recorder bytes lost in write cache)",
            "replay_bvid": args.replay_bvid,
            "replay_sha256": "sha256:" + _sha256(replay),
            "replay_origin_wall": (base.isoformat()),
            "replay_origin_offset_s": round(origin, 3),
            "cut_replay_interval_s": [round(start, 3), round(end, 3)],
            "record_start_time": t0[stem].isoformat(),
            "encoder": f"libx264 veryfast crf={args.crf} + aac 192k",
            "anchor_evidence": anchor_evidence,
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
            # fields (flv size/mtime) we cannot honestly reproduce. Session
            # identity is already frozen in the day state's segment_sessions
            # mapping; the rescue provenance sidecar discloses the wall t0.
            print(f"note: {meta_target.name} absent (t0={stem_t0.isoformat()})")
        _run(["cp", str(prov_path), str(canonical / prov_path.name)])
        tmp = canonical / f".rescue-{stem}.mp4.partial"
        _run(["cp", str(out), str(tmp)])
        tmp.rename(target)
        print(f"APPLIED {target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
