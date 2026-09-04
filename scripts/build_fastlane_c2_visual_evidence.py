#!/usr/bin/env python3
"""Create-only visual/audio evidence for the fixed C2 private burned package."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

CID = "auto_203011_328_389"
INTRO_OFFSET_MS = 5749
CUES = (
    ("cue5", 8720, 11240, "小豆老公；； 不是你老公"),
    ("cue21", 56080, 58860, "小豆哪有好吵"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str]) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"ffmpeg failed: {result.stderr[-600:]}")


def _frame(video: Path, at_ms: int, path: Path, *, crop: bool = False) -> None:
    filters = "crop=1920:480:0:600" if crop else "null"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at_ms / 1000:.3f}",
        "-i", str(video), "-vf", filters, "-frames:v", "1", str(path),
    ])


def _segment(video: Path, start_ms: int, duration_ms: int, path: Path) -> None:
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start_ms / 1000:.3f}",
        "-i", str(video), "-t", f"{duration_ms / 1000:.3f}", "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(path),
    ])
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-i", str(path), "-f", "null", "-"])


def _contact_sheet(images: list[Path], output: Path) -> None:
    loaded = [Image.open(path).convert("RGB") for path in images]
    try:
        thumb_width = 640
        thumb_height = round(loaded[0].height * thumb_width / loaded[0].width)
        canvas = Image.new("RGB", (thumb_width * 3, (thumb_height + 46) * 3), "black")
        draw = ImageDraw.Draw(canvas)
        for index, (image, path) in enumerate(zip(loaded, images)):
            x, y = (index % 3) * thumb_width, (index // 3) * (thumb_height + 46)
            canvas.paste(image.resize((thumb_width, thumb_height)), (x, y))
            draw.text((x + 8, y + thumb_height + 10), path.stem, fill="white")
        canvas.save(output)
    finally:
        for image in loaded:
            image.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    package, out = args.package.resolve(), args.out.resolve()
    if out.exists():
        raise SystemExit("refusing to overwrite evidence output")
    video = package / f"{CID}.recut.burned-final-speaker.mp4"
    ass = package / f"{CID}.recut.final-sapphire72.ass"
    srt = package / f"{CID}.recut.srt"
    if not all(path.is_file() for path in (video, ass, srt)):
        raise SystemExit("C2 candidate artifacts missing")
    ass_text, srt_text = ass.read_text(encoding="utf-8"), srt.read_text(encoding="utf-8")
    for _name, start, end, text in CUES:
        if text not in ass_text or text not in srt_text:
            raise SystemExit("C2 active subtitle surface drift")
        if start >= end:
            raise SystemExit("C2 invalid cue timing")
    out.mkdir(mode=0o700, parents=True)
    evidence: dict[str, object] = {
        "schema_version": "fastlane-c2-burned-visual-evidence.v1",
        "candidate_id": CID,
        "burned_video": {"path": video.name, "sha256": sha256(video)},
        "sidecars": {"srt": {"path": srt.name, "sha256": sha256(srt)}, "ass": {"path": ass.name, "sha256": sha256(ass)}},
        "intro_offset_ms": INTRO_OFFSET_MS,
        "items": [],
    }
    contact_inputs: list[Path] = []
    for name, source_start, source_end, text in CUES:
        burned_start, burned_end = source_start + INTRO_OFFSET_MS, source_end + INTRO_OFFSET_MS
        points = (("start", burned_start), ("mid", (burned_start + burned_end) // 2), ("end", burned_end - 80))
        frames = []
        for label, at_ms in points:
            full = out / f"{name}-{label}-{at_ms}ms.png"
            crop = out / f"{name}-{label}-{at_ms}ms.subtitle-crop.png"
            _frame(video, at_ms, full)
            _frame(video, at_ms, crop, crop=True)
            frames.append({"label": label, "burned_ms": at_ms, "source_ms": at_ms - INTRO_OFFSET_MS, "frame": full.name, "frame_sha256": sha256(full), "subtitle_crop": crop.name, "subtitle_crop_sha256": sha256(crop)})
            contact_inputs.append(full)
        segment_start = max(0, burned_start - 1000)
        segment = out / f"{name}-{segment_start}ms-4s.mp4"
        _segment(video, segment_start, 4000, segment)
        evidence["items"].append({"kind": "active_ass_cue", "name": name, "active_text": text, "source_window_ms": [source_start, source_end], "burned_window_ms": [burned_start, burned_end], "mapping": "burned_ms = source_ms + 5749", "frames": frames, "segment": segment.name, "segment_sha256": sha256(segment), "segment_window_burned_ms": [segment_start, segment_start + 4000]})
    intro_points = (("intro-before", INTRO_OFFSET_MS - 500), ("intro-after", INTRO_OFFSET_MS + 500), ("main-first-frame", INTRO_OFFSET_MS + 1200))
    intro_frames = []
    for label, at_ms in intro_points:
        path = out / f"{label}-{at_ms}ms.png"
        _frame(video, at_ms, path)
        intro_frames.append({"label": label, "burned_ms": at_ms, "frame": path.name, "frame_sha256": sha256(path)})
        contact_inputs.append(path)
    _contact_sheet(contact_inputs, out / "contact-sheet.png")
    evidence["intro_transition"] = {"expected_intro_offset_ms": INTRO_OFFSET_MS, "frames": intro_frames, "contact_sheet": "contact-sheet.png", "contact_sheet_sha256": sha256(out / "contact-sheet.png")}
    evidence["artifacts"] = {path.name: sha256(path) for path in sorted(out.iterdir()) if path.is_file()}
    (out / "visual-evidence.v1.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
