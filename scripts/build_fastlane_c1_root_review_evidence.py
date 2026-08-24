#!/usr/bin/env python3
"""Create hash-bound C1 root-review frames and short segments privately.

This is intentionally locked to ``auto_173005_934_1166``.  It accepts only a
validated C1 formal private package and writes a new, create-only evidence
directory.  It does not upload, edit Bilibili, or create a human receipt.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.fastlane_c1_formal_adapter import (  # noqa: E402
    CID,
    ROOT_TECHNICAL_STATUS,
    SIX_NAMED_POINTS,
    TITLE,
    sha256_file,
    validate_formal_package,
)
from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues  # noqa: E402


SCHEMA_VERSION = "fastlane-c1-root-review-evidence.v1"
RECEIPT_NAME = "root-review-evidence.v1.json"
CONTACT_SHEET_NAME = "root-review-contact-sheet.jpg"
SIDECAR_CROP_CONTACT_SHEET_NAME = "root-review-sidecar-crops.jpg"
# The C1 delivery is a fixed 1920x1080 horizontal render.  The expected live
# sidecar sits within this bottom third; retaining the adjacent watched-video
# subtitle area makes the two absence checks visually reviewable as well.
SIDECAR_CROP_BOX = (0, 720, 1920, 1080)


@dataclass(frozen=True)
class ReviewPointSpec:
    point_id: str
    public_anchor_ms: int
    proof_frame_public_ms: int
    segment_start_ms: int
    segment_end_ms: int
    source_cue: int | None = None
    watched_video_source_cues: tuple[int, ...] = ()


# The anchors are Ivan's public-burn landmarks.  The sixth reported point is
# approximate: its exact successor cue begins at source 02:21.560, so the
# proof frame is deliberately inside that cue while retaining the 02:26 anchor
# frame and a continuous segment that covers both.
REVIEW_POINT_SPECS = (
    ReviewPointSpec("c1-01-0046-weioula", 46_000, 46_000, 44_800, 47_200, source_cue=6),
    ReviewPointSpec(
        "c1-02-0101-watched-video-absent",
        61_000,
        61_000,
        59_800,
        62_200,
        watched_video_source_cues=(9, 10, 11, 12, 13, 14),
    ),
    ReviewPointSpec("c1-03-0112-zhubao", 72_000, 72_000, 70_800, 73_200, source_cue=16),
    ReviewPointSpec("c1-04-0137-laile", 97_000, 97_000, 95_800, 98_200),
    ReviewPointSpec(
        "c1-05-0200-watched-video-absent",
        120_000,
        120_000,
        118_800,
        121_200,
        watched_video_source_cues=(26, 27, 28),
    ),
    ReviewPointSpec("c1-06-0226-daisuki", 146_000, 147_800, 145_200, 149_400, source_cue=25),
)


class C1RootReviewEvidenceError(ValueError):
    """The candidate-locked C1 visual evidence cannot be made safely."""


def _format_ms(value: int) -> str:
    hours, rest = divmod(value, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, milliseconds = divmod(rest, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise C1RootReviewEvidenceError(f"C1_{label}_INVALID") from exc
    if not isinstance(value, dict):
        raise C1RootReviewEvidenceError(f"C1_{label}_INVALID")
    return value


def _point_lookup() -> dict[str, Mapping[str, object]]:
    points = {str(point["point_id"]): point for point in SIX_NAMED_POINTS}
    expected_ids = [spec.point_id for spec in REVIEW_POINT_SPECS]
    if list(points) != expected_ids:
        raise C1RootReviewEvidenceError("C1_REVIEW_POINT_AUTHORITY_DRIFT")
    return points


def build_review_plan(*, intro_offset_ms: int) -> list[dict[str, object]]:
    """Map the six public anchors to the Z1-offset source-content timeline."""

    if intro_offset_ms != 5_749:
        raise C1RootReviewEvidenceError("C1_REVIEW_INTRO_OFFSET_DRIFT")
    points = _point_lookup()
    plan: list[dict[str, object]] = []
    for position, spec in enumerate(REVIEW_POINT_SPECS, start=1):
        authority = points[spec.point_id]
        if spec.proof_frame_public_ms < intro_offset_ms or spec.public_anchor_ms < intro_offset_ms:
            raise C1RootReviewEvidenceError("C1_REVIEW_POINT_PRE_INTRO_INVALID")
        expected = str(authority["expected"])
        plan.append(
            {
                "ordinal": position,
                "point_id": spec.point_id,
                "expected": expected,
                "public_anchor_ms": spec.public_anchor_ms,
                "public_anchor": _format_ms(spec.public_anchor_ms),
                "source_anchor_ms": spec.public_anchor_ms - intro_offset_ms,
                "source_anchor": _format_ms(spec.public_anchor_ms - intro_offset_ms),
                "proof_frame_public_ms": spec.proof_frame_public_ms,
                "proof_frame_public": _format_ms(spec.proof_frame_public_ms),
                "proof_frame_source_ms": spec.proof_frame_public_ms - intro_offset_ms,
                "proof_frame_source": _format_ms(spec.proof_frame_public_ms - intro_offset_ms),
                "segment_public_start_ms": spec.segment_start_ms,
                "segment_public_end_ms": spec.segment_end_ms,
                "segment_public_start": _format_ms(spec.segment_start_ms),
                "segment_public_end": _format_ms(spec.segment_end_ms),
                "source_cue": spec.source_cue,
                "watched_video_source_cues": list(spec.watched_video_source_cues),
            }
        )
    return plan


def _cue_payload(cue: SrtCue) -> dict[str, object]:
    try:
        ordinal = int(cue.index)
    except ValueError as exc:
        raise C1RootReviewEvidenceError("C1_REVIEW_SUCCESSOR_SRT_INDEX_INVALID") from exc
    return {
        "ordinal": ordinal,
        "start_ms": cue.start_ms,
        "end_ms": cue.end_ms,
        "start": _format_ms(cue.start_ms),
        "end": _format_ms(cue.end_ms),
        "text": cue.text,
    }


def _active_cues(cues: Sequence[SrtCue], source_time_ms: int) -> list[dict[str, object]]:
    return [
        _cue_payload(cue)
        for cue in cues
        if cue.start_ms <= source_time_ms < cue.end_ms
    ]


def _require_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise C1RootReviewEvidenceError("C1_REVIEW_FFMPEG_TOOLCHAIN_UNAVAILABLE")
    return ffmpeg, ffprobe


def _run(command: list[str], *, label: str) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=600)
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-500:]
        raise C1RootReviewEvidenceError(f"C1_REVIEW_{label}_FAILED: {detail}")


def _extract_frame(*, ffmpeg: str, video: Path, public_time_ms: int, output: Path) -> None:
    _run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(video),
            "-ss",
            _format_ms(public_time_ms),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output),
        ],
        label="FRAME_EXTRACTION",
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise C1RootReviewEvidenceError("C1_REVIEW_FRAME_MISSING")


def _extract_segment(
    *, ffmpeg: str, video: Path, start_ms: int, end_ms: int, output: Path
) -> None:
    if end_ms <= start_ms:
        raise C1RootReviewEvidenceError("C1_REVIEW_SEGMENT_WINDOW_INVALID")
    _run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(video),
            "-ss",
            _format_ms(start_ms),
            "-t",
            _format_ms(end_ms - start_ms),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        label="SEGMENT_EXTRACTION",
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise C1RootReviewEvidenceError("C1_REVIEW_SEGMENT_MISSING")


def _probe(ffprobe: str, path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=index,codec_type,codec_name,width,height,avg_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise C1RootReviewEvidenceError("C1_REVIEW_PROBE_FAILED")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise C1RootReviewEvidenceError("C1_REVIEW_PROBE_INVALID") from exc
    if not isinstance(value, dict):
        raise C1RootReviewEvidenceError("C1_REVIEW_PROBE_INVALID")
    return value


def _build_contact_sheet(
    *,
    frame_entries: Sequence[dict[str, object]],
    output: Path,
    tile_width: int = 640,
    tile_height: int = 360,
    label_height: int = 34,
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageOps
    except ImportError as exc:  # pragma: no cover - environment preflight owns Pillow
        raise C1RootReviewEvidenceError("C1_REVIEW_PIL_UNAVAILABLE") from exc
    if not frame_entries:
        raise C1RootReviewEvidenceError("C1_REVIEW_CONTACT_SHEET_EMPTY")
    columns = 3
    rows = math.ceil(len(frame_entries) / columns)
    canvas = Image.new("RGB", (columns * tile_width, rows * (tile_height + label_height)), (18, 36, 79))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:  # Pillow < 10 compatibility
        font = ImageFont.load_default()
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    for position, entry in enumerate(frame_entries):
        path = Path(str(entry["path"]))
        x = position % columns * tile_width
        y = position // columns * (tile_height + label_height)
        with Image.open(path) as image:
            tile = ImageOps.fit(image.convert("RGB"), (tile_width, tile_height), method=resampling)
        canvas.paste(tile, (x, y + label_height))
        label = str(entry["label"])
        draw.rectangle((x, y, x + tile_width, y + label_height), fill=(18, 36, 79))
        draw.text((x + 8, y + 7), label, fill=(255, 246, 214), font=font)
    canvas.save(output, format="JPEG", quality=94, subsampling=0, optimize=False)
    if not output.is_file() or output.stat().st_size == 0:
        raise C1RootReviewEvidenceError("C1_REVIEW_CONTACT_SHEET_MISSING")


def _crop_sidecar_frame(*, source: Path, output: Path) -> None:
    """Derive a labeled-review crop from one exact canonical frame.

    This is not a second render: the receipt carries the full-frame hash and
    the fixed crop box, making the enlarged lower-third directly traceable to
    the extracted burned-final frame.
    """

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment preflight owns Pillow
        raise C1RootReviewEvidenceError("C1_REVIEW_PIL_UNAVAILABLE") from exc
    with Image.open(source) as image:
        if image.size != (1920, 1080):
            raise C1RootReviewEvidenceError("C1_REVIEW_FRAME_DIMENSIONS_DRIFT")
        crop = image.crop(SIDECAR_CROP_BOX).convert("RGB")
    crop.save(output, format="JPEG", quality=96, subsampling=0, optimize=False)
    if not output.is_file() or output.stat().st_size == 0:
        raise C1RootReviewEvidenceError("C1_REVIEW_SIDECAR_CROP_MISSING")


def _sidecar_crop_artifact(
    *, crop: Path, source_frame: Mapping[str, object], stage: Path
) -> dict[str, object]:
    return _artifact(
        crop,
        stage=stage,
        extra={
            "derived_from_frame_sha256": source_frame["sha256"],
            "crop_box_px": {
                "left": SIDECAR_CROP_BOX[0],
                "top": SIDECAR_CROP_BOX[1],
                "right": SIDECAR_CROP_BOX[2],
                "bottom": SIDECAR_CROP_BOX[3],
            },
        },
    )


def _artifact(path: Path, *, stage: Path, extra: Mapping[str, object] | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "path": path.relative_to(stage).as_posix(),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }
    if extra:
        value.update(extra)
    return value


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def materialize_root_review_evidence(*, package: Path, out: Path) -> Path:
    """Materialize C1's six-point visual evidence without a receipt or upload."""

    package = package.resolve()
    out = out.resolve()
    if out.exists():
        raise C1RootReviewEvidenceError("C1_REVIEW_OUTPUT_ALREADY_EXISTS")
    validate_formal_package(package)
    ffmpeg, ffprobe = _require_tools()
    manifest = _read_json(package / "review_manifest.json", label="FORMAL_MANIFEST")
    package_audit = _read_json(package / "package_audit.json", label="PACKAGE_AUDIT")
    template = _read_json(package / "delegated-root-technical-review.template.v1.json", label="TECHNICAL_TEMPLATE")
    ruling_seal = _read_json(package / "c1.ruling-seal.v1.json", label="RULING_SEAL")
    if (
        manifest.get("candidate_id") != CID
        or manifest.get("title") != TITLE
        or manifest.get("status") != ROOT_TECHNICAL_STATUS
        or package_audit.get("passed") is not True
        or package_audit.get("issue_count") != 0
        or package_audit.get("blocking_issue_count") != 0
        or template.get("status") != ROOT_TECHNICAL_STATUS
        or template.get("ivan_rereview_required") is not False
    ):
        raise C1RootReviewEvidenceError("C1_REVIEW_PACKAGE_STATE_DRIFT")

    video = package / f"{CID}.recut.burned-final-speaker.mp4"
    subtitle = package / f"{CID}.recut.srt"
    if not video.is_file() or not subtitle.is_file():
        raise C1RootReviewEvidenceError("C1_REVIEW_PRIMARY_ARTIFACT_MISSING")
    authority = _read_json(
        ROOT / "assets/lidousha/fastlane_c1_private/auto_173005_934_1166.formal-adapter.v1.json",
        label="FORMAL_AUTHORITY",
    )
    intro = authority.get("branding_intro")
    if not isinstance(intro, dict) or intro.get("intro_offset_ms") != 5_749:
        raise C1RootReviewEvidenceError("C1_REVIEW_INTRO_AUTHORITY_DRIFT")
    plan = build_review_plan(intro_offset_ms=intro["intro_offset_ms"])
    cues = parse_srt_cues(subtitle.read_text(encoding="utf-8"))
    if len(cues) != 26 or [cue.index for cue in cues] != [str(index) for index in range(1, 27)]:
        raise C1RootReviewEvidenceError("C1_REVIEW_SUCCESSOR_SRT_GRID_DRIFT")

    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fastlane-c1-root-review-", dir=out.parent) as raw_stage:
        stage = Path(raw_stage)
        frame_entries: list[dict[str, object]] = []
        sidecar_crop_entries: list[dict[str, object]] = []
        points: list[dict[str, object]] = []
        for item in plan:
            ordinal = int(item["ordinal"])
            point_id = str(item["point_id"])
            anchor_path = stage / f"point-{ordinal:02d}-anchor-{item['public_anchor'].replace(':', '').replace('.', '_')}.jpg"
            _extract_frame(
                ffmpeg=ffmpeg,
                video=video,
                public_time_ms=int(item["public_anchor_ms"]),
                output=anchor_path,
            )
            anchor_active = _active_cues(cues, int(item["source_anchor_ms"]))
            anchor_artifact = _artifact(
                anchor_path,
                stage=stage,
                extra={
                    "public_time_ms": item["public_anchor_ms"],
                    "public_time": item["public_anchor"],
                    "source_content_time_ms": item["source_anchor_ms"],
                    "source_content_time": item["source_anchor"],
                    "active_successor_sidecar_cues": anchor_active,
                },
            )
            anchor_crop_path = stage / (
                f"point-{ordinal:02d}-anchor-{item['public_anchor'].replace(':', '').replace('.', '_')}.sidecar.jpg"
            )
            _crop_sidecar_frame(source=anchor_path, output=anchor_crop_path)
            anchor_crop_artifact = _sidecar_crop_artifact(
                crop=anchor_crop_path,
                source_frame=anchor_artifact,
                stage=stage,
            )
            frame_entries.append(
                {
                    "path": anchor_path,
                    "label": f"P{ordinal} anchor {item['public_anchor']}",
                }
            )
            sidecar_crop_entries.append(
                {
                    "path": anchor_crop_path,
                    "label": f"P{ordinal} anchor lower-third {item['public_anchor']}",
                }
            )

            proof_path = anchor_path
            proof_active = anchor_active
            proof_crop_artifact = anchor_crop_artifact
            if int(item["proof_frame_public_ms"]) != int(item["public_anchor_ms"]):
                proof_path = stage / f"point-{ordinal:02d}-proof-{item['proof_frame_public'].replace(':', '').replace('.', '_')}.jpg"
                _extract_frame(
                    ffmpeg=ffmpeg,
                    video=video,
                    public_time_ms=int(item["proof_frame_public_ms"]),
                    output=proof_path,
                )
                proof_active = _active_cues(cues, int(item["proof_frame_source_ms"]))
                frame_entries.append(
                    {
                        "path": proof_path,
                        "label": f"P{ordinal} proof {item['proof_frame_public']}",
                    }
                )
            proof_artifact = _artifact(
                proof_path,
                stage=stage,
                extra={
                    "public_time_ms": item["proof_frame_public_ms"],
                    "public_time": item["proof_frame_public"],
                    "source_content_time_ms": item["proof_frame_source_ms"],
                    "source_content_time": item["proof_frame_source"],
                    "active_successor_sidecar_cues": proof_active,
                },
            )
            if proof_path != anchor_path:
                proof_crop_path = stage / (
                    f"point-{ordinal:02d}-proof-{item['proof_frame_public'].replace(':', '').replace('.', '_')}.sidecar.jpg"
                )
                _crop_sidecar_frame(source=proof_path, output=proof_crop_path)
                proof_crop_artifact = _sidecar_crop_artifact(
                    crop=proof_crop_path,
                    source_frame=proof_artifact,
                    stage=stage,
                )
                sidecar_crop_entries.append(
                    {
                        "path": proof_crop_path,
                        "label": f"P{ordinal} proof lower-third {item['proof_frame_public']}",
                    }
                )
            expected = str(item["expected"])
            if expected.startswith("ABSENT_"):
                if anchor_active or proof_active:
                    raise C1RootReviewEvidenceError("C1_REVIEW_ABSENCE_OVERLAY_DRIFT")
                sidecar_assertion: dict[str, object] = {
                    "verdict": "ABSENT",
                    "expected": expected,
                    "watched_video_source_cues": item["watched_video_source_cues"],
                    "interpretation": (
                        "The frame and segment are canonical burned pixels. Any watched-video subtitle visible in those "
                        "pixels is original-video text; the active successor sidecar cue list is empty, so no Li Dousha "
                        "overlay is rendered at this anchor."
                    ),
                }
            else:
                if not any(cue["text"] == expected for cue in proof_active):
                    raise C1RootReviewEvidenceError("C1_REVIEW_PRESENT_OVERLAY_DRIFT")
                sidecar_assertion = {
                    "verdict": "PRESENT",
                    "expected": expected,
                    "source_cue": item["source_cue"],
                    "proof_active_successor_sidecar_cues": proof_active,
                }

            segment_path = stage / f"point-{ordinal:02d}-segment.mp4"
            _extract_segment(
                ffmpeg=ffmpeg,
                video=video,
                start_ms=int(item["segment_public_start_ms"]),
                end_ms=int(item["segment_public_end_ms"]),
                output=segment_path,
            )
            points.append(
                {
                    "point_id": point_id,
                    "expected": expected,
                    "public_to_source_mapping": {
                        "formula": "public_burn_ms = source_content_ms + 5749",
                        "z1_intro_id": intro["intro_id"],
                        "z1_intro_offset_ms": intro["intro_offset_ms"],
                        "public_anchor_ms": item["public_anchor_ms"],
                        "source_anchor_ms": item["source_anchor_ms"],
                        "proof_frame_public_ms": item["proof_frame_public_ms"],
                        "proof_frame_source_ms": item["proof_frame_source_ms"],
                    },
                    "anchor_frame": anchor_artifact,
                    "anchor_sidecar_crop": anchor_crop_artifact,
                    "proof_frame": proof_artifact,
                    "proof_sidecar_crop": proof_crop_artifact,
                    "short_segment": _artifact(
                        segment_path,
                        stage=stage,
                        extra={
                            "public_start_ms": item["segment_public_start_ms"],
                            "public_end_ms": item["segment_public_end_ms"],
                            "public_start": item["segment_public_start"],
                            "public_end": item["segment_public_end"],
                            "probe": _probe(ffprobe, segment_path),
                        },
                    ),
                    "sidecar_assertion": sidecar_assertion,
                }
            )

        contact_sheet = stage / CONTACT_SHEET_NAME
        _build_contact_sheet(frame_entries=frame_entries, output=contact_sheet)
        sidecar_crop_contact_sheet = stage / SIDECAR_CROP_CONTACT_SHEET_NAME
        _build_contact_sheet(
            frame_entries=sidecar_crop_entries,
            output=sidecar_crop_contact_sheet,
            tile_width=640,
            tile_height=120,
        )
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": CID,
            "title": TITLE,
            "status": ROOT_TECHNICAL_STATUS,
            "create_only": True,
            "upload_allowed": False,
            "ivan_rereview_required": False,
            "root_technical_receipt": {
                "accepted": False,
                "receipt_path": None,
            },
            "package_bindings": {
                "package_path": str(package),
                "review_manifest_sha256": sha256_file(package / "review_manifest.json"),
                "formal_adapter_receipt_sha256": sha256_file(package / "c1.formal-adapter-receipt.v1.json"),
                "package_audit_sha256": sha256_file(package / "package_audit.json"),
                "burned_final": _artifact(
                    video,
                    stage=package,
                    extra={"probe": _probe(ffprobe, video)},
                ),
                "successor_srt_sha256": sha256_file(subtitle),
                "successor_ass_sha256": sha256_file(package / f"{CID}.recut.final-sapphire72.ass"),
                "ruling_seal": {
                    "raw_line947_sha256": ruling_seal["raw_line947_sha256"],
                    "raw_line947_content_sha256": ruling_seal["raw_line947_content_sha256"],
                },
            },
            "contact_sheet": _artifact(contact_sheet, stage=stage),
            "sidecar_crop_contact_sheet": _artifact(sidecar_crop_contact_sheet, stage=stage),
            "points": points,
        }
        _write_json(stage / RECEIPT_NAME, receipt)
        try:
            os.rename(stage, out)
        except FileExistsError as exc:
            raise C1RootReviewEvidenceError("C1_REVIEW_OUTPUT_ALREADY_EXISTS") from exc
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = materialize_root_review_evidence(package=args.package, out=args.out)
    except C1RootReviewEvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    receipt = output / RECEIPT_NAME
    print(
        json.dumps(
            {
                "output": str(output),
                "receipt": str(receipt),
                "receipt_sha256": sha256_file(receipt),
                "upload_performed": False,
                "root_technical_receipt_created": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
