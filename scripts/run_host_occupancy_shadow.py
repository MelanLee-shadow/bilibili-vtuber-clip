#!/usr/bin/env python3
"""Run the provisional host-occupancy estimator as a bounded shadow job."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice import host_occupancy as ho


SCHEMA_VERSION = "host-occupancy-shadow-run.v1"


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _duration_ms(path: Path) -> int:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {completed.stderr[-300:]}")
    duration = int(float(completed.stdout.strip()) * 1000)
    if duration <= ho.WINDOW_MS:
        raise RuntimeError(f"media is too short for occupancy analysis: {path}")
    return duration


def _candidate(value: str) -> tuple[str, Path]:
    candidate_id, separator, raw_path = value.partition("=")
    if not separator or not candidate_id or not raw_path:
        raise argparse.ArgumentTypeError("candidate must be CANDIDATE_ID=/absolute/media.mp4")
    path = Path(raw_path)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("candidate media path must be absolute")
    return candidate_id, path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=_candidate, action="append", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    candidates: list[tuple[str, Path]] = list(args.candidate)
    for _, path in candidates:
        if not path.is_file():
            raise RuntimeError(f"candidate media is missing: {path}")
    prototypes, enrollment = ho.enrollment_prototypes(args.profile, args.reference_dir)
    load_started = time.monotonic()
    similarity = ho.build_campp_similarity(
        args.model_dir, cache_dir=args.work_dir / "embedding-cache"
    )
    model_load_seconds = time.monotonic() - load_started

    reports: list[dict[str, object]] = []
    candidate_timings: list[dict[str, object]] = []
    wall_started = time.monotonic()
    for candidate_id, media in candidates:
        duration_ms = _duration_ms(media)
        started = time.monotonic()
        rows = ho.run_estimator(
            source_media=media,
            source_duration_ms=duration_ms,
            spans=[ho.CandidateSpan(candidate_id, 0, duration_ms)],
            prototypes=prototypes,
            similarity=similarity,
            work_dir=args.work_dir / candidate_id,
            enrollment=enrollment,
        )
        if len(rows) != 1:
            raise RuntimeError(f"unexpected report count for {candidate_id}: {len(rows)}")
        report = rows[0]
        report["source_media_path"] = str(media.resolve())
        report["source_media_sha256"] = _sha256(media)
        reports.append(report)
        candidate_timings.append(
            {
                "candidate_id": candidate_id,
                "media_duration_ms": duration_ms,
                "wall_seconds": round(time.monotonic() - started, 3),
            }
        )

    output = {
        "schema_version": SCHEMA_VERSION,
        "purpose": "SHADOW_ONLY_NOT_SELECTION_OR_RELEASE_AUTHORITY",
        "host_occupancy_schema_version": ho.SCHEMA_VERSION,
        "host_occupancy_estimator_version": ho.ESTIMATOR_VERSION,
        "host_occupancy_threshold_version": ho.THRESHOLD_VERSION,
        "enrollment": enrollment,
        "model_dir": str(args.model_dir.resolve()),
        "model_load_seconds": round(model_load_seconds, 3),
        "wall_seconds": round(time.monotonic() - wall_started, 3),
        "candidate_timings": candidate_timings,
        "reports": reports,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
