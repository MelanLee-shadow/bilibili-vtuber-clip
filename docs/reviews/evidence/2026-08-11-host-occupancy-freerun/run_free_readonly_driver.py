"""Read-only host-occupancy measurement driver for free. Writes only under /tmp."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/tmp/hostocc")

from src.autoslice import host_occupancy as ho  # noqa: E402

REC = Path("/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/2026-08-07")
PROFILE = Path("/tmp/hostocc/assets/lidousha/voiceprint_profile.v1.json")
REFERENCES = Path("/opt/bilive/autoslice/voiceprints/lidousha")
MODEL = Path("/opt/bilive/autoslice/models/campp")
WORK = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("/tmp/hostocc-work")

SEGMENT_DURATIONS = {
    "22966160_20260807-20-37-35": 1803589,
    "22966160_20260807-21-07-39": 1804192,
    "22966160_20260807-21-37-43": 1803565,
    "22966160_20260807-22-07-47": 1803441,
    "22966160_20260807-22-37-50": 1804195,
}

BATCHES = json.loads(Path(sys.argv[1]).read_text())
OUTPUT = Path(sys.argv[2])

prototypes, enrollment = ho.enrollment_prototypes(PROFILE, REFERENCES)
print("enrollment:", json.dumps(enrollment, ensure_ascii=False), flush=True)

load_started = time.monotonic()
similarity = ho.build_campp_similarity(MODEL, cache_dir=WORK / "embedding-cache")
model_load_seconds = time.monotonic() - load_started
print(f"model_load_seconds={model_load_seconds:.2f}", flush=True)

timings: dict[str, float] = {"extract": 0.0, "score": 0.0}
inference_calls = [0]

_real_extract = ho.extract_span_wav


def timed_extract(source, *, start_ms, end_ms, output_path):
    started = time.monotonic()
    result = _real_extract(source, start_ms=start_ms, end_ms=end_ms, output_path=output_path)
    timings["extract"] += time.monotonic() - started
    return result


def timed_similarity(left: Path, right: Path) -> float:
    started = time.monotonic()
    value = similarity(left, right)
    timings["score"] += time.monotonic() - started
    inference_calls[0] += 1
    return value


reports = []
wall_started = time.monotonic()
for batch in BATCHES:
    segment = batch["segment"]
    spans = [
        ho.CandidateSpan(item["cid"], item["start_ms"], item["end_ms"])
        for item in batch["candidates"]
    ]
    segment_started = time.monotonic()
    reports.extend(
        ho.run_estimator(
            source_media=REC / f"{segment}.mp4",
            source_duration_ms=SEGMENT_DURATIONS[segment],
            spans=spans,
            prototypes=prototypes,
            similarity=timed_similarity,
            work_dir=WORK / segment,
            enrollment=enrollment,
            extract=timed_extract,
        )
    )
    print(f"segment {segment} done in {time.monotonic() - segment_started:.1f}s", flush=True)

wall_seconds = time.monotonic() - wall_started
candidate_ms = sum(
    report["candidate_end_ms"] - report["candidate_start_ms"] for report in reports
)
cost = {
    "wall_seconds": round(wall_seconds, 2),
    "model_load_seconds": round(model_load_seconds, 2),
    "ffmpeg_extract_seconds": round(timings["extract"], 2),
    "campp_score_seconds": round(timings["score"], 2),
    "similarity_calls": inference_calls[0],
    "candidate_count": len(reports),
    "candidate_audio_seconds": round(candidate_ms / 1000, 1),
}
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(
    json.dumps({"cost": cost, "reports": reports}, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print("COST", json.dumps(cost), flush=True)
for report in reports:
    occupancy = report["occupancy"]
    print(
        f"{report['candidate_id']}\t{occupancy['state']}\t"
        f"host_share={occupancy['host_speech_share']}\t"
        f"band={occupancy['host_speech_share_band']}\t"
        f"cov={occupancy['classified_coverage']}\tunk={occupancy['unknown_share']}\t"
        f"other_run={occupancy['longest_other_run_ms']}\t"
        f"attr={report['attribution_status']}\t{occupancy['reason_codes']}",
        flush=True,
    )
