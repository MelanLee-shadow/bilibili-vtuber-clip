#!/usr/bin/env python3
"""Compile one exhaustive operator-reviewed SRT into an exact replay baseline.

This compiler owns subtitle text only.  It emits no speaker override.  When
Ivan has explicitly reviewed the whole SRT, its typed text-ownership pin lets
the producer skip discovery/rewriting work whose output will be overwritten by
the exact replay.  Boundary, exact-final, rendering, cover, and release gates
remain mandatory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.apply_speaker_turn_overrides import atomic_write_text
from src.autoslice.jingting_chunker import parse_srt_cues


REGISTRY_SCHEMA = "candidate-reviewed-subtitle-baseline.v1"
BASELINE_SCHEMA = "subtitle-redelivery-baseline.v2"
BASELINE_MODE = "preserve_text_outside_source_truth"
RECEIPT_SCHEMA = "operator-reviewed-subtitle-baseline-delivery.v1"
TEXT_OWNERSHIP_PIN_SCHEMA = "operator-reviewed-text-full-ownership-pin.v1"
_CANDIDATE_RX = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_SHA256_RX = re.compile(r"[0-9a-f]{64}\Z")
_SPEAKER_PREFIX_RX = re.compile(r"^\[(?:李豆沙|连线)(?:\s+[+-]?\d+(?:\.\d+)?)?\]\s*")
_AB_MARKER_RX = re.compile(r"(?<!\S)[AB](?!\S)")


class OperatorBaselineCompileError(ValueError):
    """The reviewed SRT cannot be frozen without guessing."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _regular_file(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise OperatorBaselineCompileError(
            f"{label} must be a regular non-symlink file"
        )
    return path.resolve(strict=True)


def _timestamp(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def compile_operator_baseline(
    *,
    source_srt: Path,
    reviewed_srt: Path,
    candidate_id: str,
    authority: str,
    source_recording_basename: str,
    source_recording_sha256: str,
    absolute_source_start_ms: int,
    absolute_source_end_ms: int,
) -> dict[str, Any]:
    """Validate and return the baseline, manifest, and provenance receipt."""

    if not _CANDIDATE_RX.fullmatch(str(candidate_id or "")):
        raise OperatorBaselineCompileError("candidate_id is invalid")
    if not str(authority or "").strip():
        raise OperatorBaselineCompileError("authority is required")
    if Path(source_recording_basename).name != source_recording_basename:
        raise OperatorBaselineCompileError(
            "source recording basename must be a filename"
        )
    if not _SHA256_RX.fullmatch(str(source_recording_sha256 or "")):
        raise OperatorBaselineCompileError("source recording sha256 is invalid")
    if (
        isinstance(absolute_source_start_ms, bool)
        or not isinstance(absolute_source_start_ms, int)
        or isinstance(absolute_source_end_ms, bool)
        or not isinstance(absolute_source_end_ms, int)
        or absolute_source_start_ms < 0
        or absolute_source_end_ms <= absolute_source_start_ms
    ):
        raise OperatorBaselineCompileError("absolute source interval is invalid")

    source_srt = _regular_file(source_srt, label="source SRT")
    reviewed_srt = _regular_file(reviewed_srt, label="reviewed SRT")
    try:
        source_cues = parse_srt_cues(source_srt.read_text(encoding="utf-8"))
        reviewed_cues = parse_srt_cues(reviewed_srt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise OperatorBaselineCompileError("SRT input is invalid") from exc
    if not source_cues or len(source_cues) != len(reviewed_cues):
        raise OperatorBaselineCompileError("reviewed SRT cue count drift")

    changed: list[dict[str, object]] = []
    rendered: list[str] = []
    for ordinal, (source, reviewed) in enumerate(
        zip(source_cues, reviewed_cues, strict=True), start=1
    ):
        if str(source.index) != str(reviewed.index) or str(reviewed.index) != str(ordinal):
            raise OperatorBaselineCompileError("reviewed SRT cue index drift")
        if (source.start_ms, source.end_ms) != (reviewed.start_ms, reviewed.end_ms):
            raise OperatorBaselineCompileError(
                f"reviewed SRT cue {ordinal} timing drift"
            )
        text = str(reviewed.text).strip()
        if not text:
            raise OperatorBaselineCompileError(
                f"reviewed SRT cue {ordinal} is empty"
            )
        if _SPEAKER_PREFIX_RX.match(text) or _AB_MARKER_RX.search(text):
            raise OperatorBaselineCompileError(
                f"reviewed SRT cue {ordinal} carries speaker annotation"
            )
        rendered.append(
            f"{ordinal}\n{_timestamp(reviewed.start_ms)} --> "
            f"{_timestamp(reviewed.end_ms)}\n{text}"
        )
        if str(source.text).strip() != text:
            changed.append(
                {
                    "cue": ordinal,
                    "start_ms": reviewed.start_ms,
                    "end_ms": reviewed.end_ms,
                    "absolute_source_start_ms": (
                        absolute_source_start_ms + reviewed.start_ms
                    ),
                    "absolute_source_end_ms": (
                        absolute_source_start_ms + reviewed.end_ms
                    ),
                    "before": str(source.text).strip(),
                    "after": text,
                }
            )

    if absolute_source_start_ms + reviewed_cues[-1].end_ms > absolute_source_end_ms:
        raise OperatorBaselineCompileError(
            "reviewed SRT extends beyond the bound source interval"
        )
    if not changed:
        raise OperatorBaselineCompileError("reviewed SRT contains no operator changes")

    baseline_text = "\n\n".join(rendered) + "\n"
    baseline_sha = _sha256_bytes(baseline_text.encode("utf-8"))
    source_sha = _sha256(source_srt)
    reviewed_sha = _sha256(reviewed_srt)
    manifest = {
        "registry_schema_version": REGISTRY_SCHEMA,
        "candidate_id": candidate_id,
        "schema_version": BASELINE_SCHEMA,
        "mode": BASELINE_MODE,
        "exact_interval_replay": True,
        "path": f"{candidate_id}.reviewed.srt",
        "sha256": baseline_sha,
        "authority": authority,
        "source_recording_basename": source_recording_basename,
        "source_sha256": source_recording_sha256,
        "absolute_source_start_ms": absolute_source_start_ms,
        "absolute_source_end_ms": absolute_source_end_ms,
        "operator_text_full_ownership": {
            "schema_version": TEXT_OWNERSHIP_PIN_SCHEMA,
            "authority": authority,
            "baseline_sha256": baseline_sha,
            "source_srt_sha256": source_sha,
            "cue_count": len(reviewed_cues),
            "changed_cue_count": len(changed),
            "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
        },
    }
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "candidate_id": candidate_id,
        "authority": authority,
        "source_srt": {"path": str(source_srt), "sha256": source_sha},
        "reviewed_srt": {"path": str(reviewed_srt), "sha256": reviewed_sha},
        "baseline_sha256": baseline_sha,
        "source_recording": {
            "basename": source_recording_basename,
            "sha256": source_recording_sha256,
            "absolute_start_ms": absolute_source_start_ms,
            "absolute_end_ms": absolute_source_end_ms,
        },
        "cue_count": len(reviewed_cues),
        "changed_cue_count": len(changed),
        "changed_cues": changed,
        "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
    }
    return {
        "baseline_srt": baseline_text,
        "baseline_manifest": manifest,
        "receipt": receipt,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-srt", required=True, type=Path)
    parser.add_argument("--reviewed-srt", required=True, type=Path)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--authority", required=True)
    parser.add_argument("--source-recording-basename", required=True)
    parser.add_argument("--source-recording-sha256", required=True)
    parser.add_argument("--absolute-source-start-ms", required=True, type=int)
    parser.add_argument("--absolute-source-end-ms", required=True, type=int)
    parser.add_argument("--baseline-srt-out", required=True, type=Path)
    parser.add_argument("--baseline-manifest-out", required=True, type=Path)
    parser.add_argument("--receipt-out", required=True, type=Path)
    args = parser.parse_args()

    result = compile_operator_baseline(
        source_srt=args.source_srt,
        reviewed_srt=args.reviewed_srt,
        candidate_id=args.candidate_id,
        authority=args.authority,
        source_recording_basename=args.source_recording_basename,
        source_recording_sha256=args.source_recording_sha256,
        absolute_source_start_ms=args.absolute_source_start_ms,
        absolute_source_end_ms=args.absolute_source_end_ms,
    )
    atomic_write_text(args.baseline_srt_out, str(result["baseline_srt"]))
    manifest = dict(result["baseline_manifest"])
    manifest["path"] = args.baseline_srt_out.name
    atomic_write_text(
        args.baseline_manifest_out,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    atomic_write_text(
        args.receipt_out,
        json.dumps(result["receipt"], ensure_ascii=False, indent=2) + "\n",
    )
    print(
        json.dumps(
            {
                "candidate_id": args.candidate_id,
                "baseline_sha256": result["receipt"]["baseline_sha256"],
                "cue_count": result["receipt"]["cue_count"],
                "changed_cue_count": result["receipt"]["changed_cue_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
