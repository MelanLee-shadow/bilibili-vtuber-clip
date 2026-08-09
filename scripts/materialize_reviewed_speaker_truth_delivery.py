#!/usr/bin/env python3
"""Compile Ivan truth-diff v2 into bound delivery baseline artifacts.

This is a delivery-only compiler.  It never runs during blind selection and it
does not turn the truth workbook into a software-test oracle.  The output is a
clean reviewed subtitle baseline plus the existing speaker-override document,
augmented with a reviewed-speaker-baseline envelope for early CAM++ anchors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.apply_speaker_turn_overrides import atomic_write_text
from scripts.harvest_ivan_truth import parse_srt, split_label
from src.autoslice.reviewed_speaker_baseline import (
    REVIEWED_SPEAKER_BASELINE_SCHEMA,
    TRUTH_SCHEMA,
)
from src.autoslice.speaker_common import GUEST_SPEAKER, HOST_SPEAKER


BASELINE_REGISTRY_SCHEMA = "candidate-reviewed-subtitle-baseline.v1"
BASELINE_SCHEMA = "subtitle-redelivery-baseline.v2"
BASELINE_SCHEMAS = {
    "subtitle-redelivery-baseline.v1",
    "subtitle-redelivery-baseline.v2",
}
BASELINE_MODE = "preserve_text_outside_source_truth"
ARBITRATION_SCHEMA = "reviewed-speaker-machine-cue-arbitration.v1"
DELIVERY_RECEIPT_SCHEMA = "reviewed-speaker-truth-delivery.v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
TIMING_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\Z"
)
STAGE_NOTE_RE = re.compile(
    r"\s*(?:\(|（)(跃起|无可辨别人声|无可分辨人声)(?:\)|）)\s*\Z"
)
ALLOWED_SPEAKERS = {HOST_SPEAKER, GUEST_SPEAKER}


class DeliveryCompileError(ValueError):
    """The truth input cannot be compiled without guessing."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timing_ms(value: str) -> tuple[int, int]:
    match = TIMING_RE.fullmatch(str(value).strip())
    if match is None:
        raise DeliveryCompileError(f"invalid timing: {value!r}")
    values = [int(item) for item in match.groups()]
    return (
        values[0] * 3_600_000
        + values[1] * 60_000
        + values[2] * 1_000
        + values[3],
        values[4] * 3_600_000
        + values[5] * 60_000
        + values[6] * 1_000
        + values[7],
    )


def _format_timestamp(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _timing_parts(value: str) -> tuple[str, str]:
    start, end = _timing_ms(value)
    if end <= start:
        raise DeliveryCompileError(f"non-positive timing: {value!r}")
    return _format_timestamp(start), _format_timestamp(end)


def _strip_stage_notes(value: str) -> tuple[str, list[str]]:
    text = str(value).strip()
    removed: list[str] = []
    while True:
        match = STAGE_NOTE_RE.search(text)
        if match is None:
            break
        removed.insert(0, match.group(1))
        text = text[: match.start()].rstrip()
    if not text:
        raise DeliveryCompileError("stage-note stripping produced empty subtitle text")
    return text, removed


def _normalise_payload(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _proportional_segments(
    *,
    start: str,
    end: str,
    segments: list[dict[str, str]],
) -> list[dict[str, str]]:
    start_ms, end_ms = _timing_ms(f"{start} --> {end}")
    duration = end_ms - start_ms
    weights = [max(1, len(re.sub(r"\s+", "", row["text"]))) for row in segments]
    total = sum(weights)
    if duration < len(segments):
        raise DeliveryCompileError("mixed cue is too short for positive segment timing")
    boundaries = [start_ms]
    cumulative = 0
    for weight in weights[:-1]:
        cumulative += weight
        proposed = start_ms + round(duration * cumulative / total)
        minimum = boundaries[-1] + 1
        maximum = end_ms - (len(segments) - len(boundaries))
        boundaries.append(min(maximum, max(minimum, proposed)))
    boundaries.append(end_ms)
    return [
        {
            **row,
            "start": _format_timestamp(boundaries[index]),
            "end": _format_timestamp(boundaries[index + 1]),
        }
        for index, row in enumerate(segments)
    ]


def _validate_arbitration(
    document: Mapping[str, object],
    *,
    candidate_id: str,
) -> tuple[str, dict[int, Mapping[str, object]]]:
    if document.get("schema_version") != ARBITRATION_SCHEMA:
        raise DeliveryCompileError("machine-cue arbitration schema is unsupported")
    if document.get("candidate_id") != candidate_id:
        raise DeliveryCompileError("machine-cue arbitration candidate mismatch")
    receipt_sha = str(document.get("receipt_sha256") or "")
    if not SHA256_RE.fullmatch(receipt_sha):
        raise DeliveryCompileError("machine-cue arbitration receipt hash is invalid")
    raw = document.get("decisions")
    if not isinstance(raw, Mapping):
        raise DeliveryCompileError("machine-cue arbitration decisions must be an object")
    decisions: dict[int, Mapping[str, object]] = {}
    for cue_raw, decision in raw.items():
        try:
            cue_number = int(str(cue_raw))
        except ValueError as exc:
            raise DeliveryCompileError(
                f"machine-cue arbitration cue is invalid: {cue_raw!r}"
            ) from exc
        if str(cue_raw) != str(cue_number) or cue_number <= 0:
            raise DeliveryCompileError(
                f"machine-cue arbitration cue is invalid: {cue_raw!r}"
            )
        if cue_number in decisions or not isinstance(decision, Mapping):
            raise DeliveryCompileError(
                f"machine-cue arbitration row is invalid: {cue_number}"
            )
        if decision.get("action") not in {"retain_machine", "drop_hallucination"}:
            raise DeliveryCompileError(
                f"machine-cue arbitration action is invalid: {cue_number}"
            )
        if not str(decision.get("reason") or "").strip():
            raise DeliveryCompileError(
                f"machine-cue arbitration reason is missing: {cue_number}"
            )
        voice_observed = decision.get("human_voice_observed")
        if not isinstance(voice_observed, bool):
            raise DeliveryCompileError(
                f"machine-cue arbitration voice verdict is invalid: {cue_number}"
            )
        if (decision["action"] == "retain_machine") != voice_observed:
            raise DeliveryCompileError(
                f"machine-cue arbitration action contradicts voice verdict: {cue_number}"
            )
        decisions[cue_number] = decision
    return receipt_sha, decisions


def _build_delivery_outputs(
    *,
    candidate_id: str,
    authority: str,
    truth_path: Path,
    truth_relative: str,
    source_text_srt: Path,
    source_media: Path,
    source_cue_count: int,
    receipt_sha: str,
    automatic_srt_sha256: str,
    source_recording_basename: str,
    source_recording_sha256: str,
    absolute_source_start_ms: int,
    absolute_source_end_ms: int,
    reviewed_at: str,
    baseline_schema_version: str,
    clean_rows: list[dict[str, object]],
    overrides: list[dict[str, object]],
    machine_rows: list[dict[str, object]],
    transform_rows: list[dict[str, object]],
) -> dict[str, object]:
    baseline_srt = "\n\n".join(
        f"{index}\n{row['start']} --> {row['end']}\n{row['text']}"
        for index, row in enumerate(clean_rows, start=1)
    ) + "\n"
    baseline_sha = hashlib.sha256(baseline_srt.encode("utf-8")).hexdigest()
    eligible_anchors = []
    for row in overrides:
        segments = row["segments"]
        if (
            isinstance(segments, list)
            and len(segments) == 1
            and segments[0].get("speaker") == HOST_SPEAKER
            and segments[0].get("start") == row["expect"]["start"]
            and segments[0].get("end") == row["expect"]["end"]
            and segments[0].get("text") == row["expect"]["text"]
        ):
            start_ms, end_ms = _timing_ms(
                f"{row['expect']['start']} --> {row['expect']['end']}"
            )
            if end_ms - start_ms >= 1_000:
                eligible_anchors.append((end_ms - start_ms, int(row["source_cue"])))
    eligible_anchors.sort(key=lambda item: (-item[0], item[1]))
    anchors = [cue for _duration, cue in eligible_anchors[:4]]
    if len(anchors) < 2:
        raise DeliveryCompileError("truth input provides fewer than two viable host anchors")

    truth_sha = _sha256(truth_path)
    media_sha = _sha256(source_media)
    baseline_manifest = {
        "registry_schema_version": BASELINE_REGISTRY_SCHEMA,
        "candidate_id": candidate_id,
        "schema_version": baseline_schema_version,
        "mode": BASELINE_MODE,
        "path": f"{candidate_id}.reviewed.srt",
        "sha256": baseline_sha,
        "authority": authority,
    }
    if baseline_schema_version == "subtitle-redelivery-baseline.v2":
        baseline_manifest.update(
            {
                "exact_interval_replay": True,
                "source_recording_basename": source_recording_basename,
                "source_sha256": source_recording_sha256,
                "absolute_source_start_ms": absolute_source_start_ms,
                "absolute_source_end_ms": absolute_source_end_ms,
            }
        )
    override_document = {
        "schema_version": 1,
        "source_media_sha256": media_sha,
        "text_final_srt_sha256": baseline_sha,
        "source_srt_sha256": automatic_srt_sha256,
        "candidate_id": candidate_id,
        "status": "ivan_reviewed_speaker_partial_machine",
        "reviewed_at": reviewed_at,
        "overlap_policy": "保留主说话人；混说段只用已披露的字符比例近似时轴。",
        "notes": (
            "Delivery-time compilation from hash-bound Ivan truth. Reviewed cues are "
            "post-analysis overrides; explicitly uncovered cues retain the existing "
            "machine analyzer decision and never default from omission."
        ),
        "reviewed_speaker_baseline": {
            "schema_version": REVIEWED_SPEAKER_BASELINE_SCHEMA,
            "authority": authority,
            "truth_input": {"path": truth_relative, "sha256": truth_sha},
            "cue_count": len(clean_rows),
            "anchor_source_cues": anchors,
            "machine_cues": machine_rows,
        },
        "overrides": overrides,
    }
    receipt = {
        "schema_version": DELIVERY_RECEIPT_SCHEMA,
        "candidate_id": candidate_id,
        "authority": authority,
        "truth_input": {"path": truth_relative, "sha256": truth_sha},
        "source_text_srt": {
            "path": str(source_text_srt),
            "sha256": _sha256(source_text_srt),
            "cue_count": source_cue_count,
        },
        "source_media": {"path": str(source_media), "sha256": media_sha},
        "baseline": {
            "schema_version": baseline_schema_version,
            "sha256": baseline_sha,
            "cue_count": len(clean_rows),
        },
        "automatic_labelled_srt_sha256": automatic_srt_sha256,
        "arbitration_receipt_sha256": receipt_sha,
        "reviewed_override_count": len(overrides),
        "machine_cues": [row["source_cue"] for row in machine_rows],
        "anchor_source_cues": anchors,
        "transformations": transform_rows,
    }
    return {
        "baseline_srt": baseline_srt,
        "baseline_manifest": baseline_manifest,
        "speaker_override": override_document,
        "receipt": receipt,
    }


def compile_delivery(
    *,
    truth_path: Path,
    source_text_srt: Path,
    source_media: Path,
    repo_root: Path,
    authority: str,
    arbitration: Mapping[str, object],
    automatic_srt_sha256: str,
    source_recording_basename: str,
    source_recording_sha256: str,
    absolute_source_start_ms: int,
    absolute_source_end_ms: int,
    reviewed_at: str,
    baseline_schema_version: str = BASELINE_SCHEMA,
) -> dict[str, object]:
    """Compile and return all output documents without writing files."""

    if not authority.strip():
        raise DeliveryCompileError("authority must be non-empty")
    if baseline_schema_version not in BASELINE_SCHEMAS:
        raise DeliveryCompileError("baseline schema version is unsupported")
    if not SHA256_RE.fullmatch(automatic_srt_sha256):
        raise DeliveryCompileError("automatic_srt_sha256 is invalid")
    if not SHA256_RE.fullmatch(source_recording_sha256):
        raise DeliveryCompileError("source_recording_sha256 is invalid")
    if Path(source_recording_basename).name != source_recording_basename:
        raise DeliveryCompileError("source_recording_basename must be a filename")
    if (
        isinstance(absolute_source_start_ms, bool)
        or not isinstance(absolute_source_start_ms, int)
        or isinstance(absolute_source_end_ms, bool)
        or not isinstance(absolute_source_end_ms, int)
        or absolute_source_start_ms < 0
        or absolute_source_end_ms <= absolute_source_start_ms
    ):
        raise DeliveryCompileError("absolute source interval is invalid")
    truth_path = truth_path.resolve(strict=True)
    source_text_srt = source_text_srt.resolve(strict=True)
    source_media = source_media.resolve(strict=True)
    repo_root = repo_root.resolve(strict=True)
    if not truth_path.is_relative_to(repo_root):
        raise DeliveryCompileError("truth input must be inside the repository")
    truth_relative = str(truth_path.relative_to(repo_root))
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    if not isinstance(truth, Mapping) or truth.get("schema") != TRUTH_SCHEMA:
        raise DeliveryCompileError("truth input schema is unsupported")
    candidate_id = str(truth.get("candidate_id") or "").strip()
    if not candidate_id:
        raise DeliveryCompileError("truth input candidate_id is missing")
    if truth.get("source_machine_sha256") != _sha256(source_text_srt):
        raise DeliveryCompileError("truth input does not bind source text SRT")
    receipt_sha, arbitration_rows = _validate_arbitration(
        arbitration,
        candidate_id=candidate_id,
    )
    source_cues = parse_srt(source_text_srt.read_text(encoding="utf-8"))
    expected_source_ordinals = list(range(1, len(source_cues) + 1))
    if [cue.index for cue in source_cues] != expected_source_ordinals:
        raise DeliveryCompileError("source text SRT cue indices are not contiguous")
    source_by_number = {cue.index: cue for cue in source_cues}

    truth_rows = truth.get("cues")
    if not isinstance(truth_rows, list) or not truth_rows:
        raise DeliveryCompileError("truth input cues must be a non-empty list")
    consumed_source_cues: set[int] = set()
    clean_rows: list[dict[str, object]] = []
    overrides: list[dict[str, object]] = []
    machine_rows: list[dict[str, object]] = []
    transform_rows: list[dict[str, object]] = []
    used_arbitration: set[int] = set()

    for truth_position, raw_row in enumerate(truth_rows, start=1):
        if not isinstance(raw_row, Mapping):
            raise DeliveryCompileError(f"truth cue {truth_position} must be an object")
        truth_cue = raw_row.get("cue")
        if isinstance(truth_cue, bool) or not isinstance(truth_cue, int):
            raise DeliveryCompileError(f"truth cue {truth_position} has invalid cue id")
        merged_from = raw_row.get("merged_from")
        if merged_from is None:
            source_numbers = [truth_cue]
        elif (
            not isinstance(merged_from, list)
            or len(merged_from) < 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in merged_from)
            or merged_from != list(range(merged_from[0], merged_from[-1] + 1))
            or merged_from[0] != truth_cue
        ):
            raise DeliveryCompileError(f"truth cue {truth_cue} has invalid merged_from")
        else:
            source_numbers = list(merged_from)
        if any(number in consumed_source_cues for number in source_numbers):
            raise DeliveryCompileError(f"truth cue {truth_cue} reuses a source cue")
        try:
            source_group = [source_by_number[number] for number in source_numbers]
        except KeyError as exc:
            raise DeliveryCompileError(
                f"truth cue {truth_cue} references a missing source cue"
            ) from exc
        consumed_source_cues.update(source_numbers)
        expected_timings = [cue.timing for cue in source_group]
        if merged_from is not None:
            if raw_row.get("merged_machine_timings") != expected_timings:
                raise DeliveryCompileError(
                    f"truth cue {truth_cue} merged source timing drift"
                )
            output_timing = raw_row.get("annotated_timing")
            timing_policy = "merged_annotated_timing"
        else:
            if raw_row.get("timing") != source_group[0].timing:
                raise DeliveryCompileError(f"truth cue {truth_cue} source timing drift")
            output_timing = source_group[0].timing
            timing_policy = (
                "machine_timing_editor_jitter_ignored"
                if raw_row.get("annotated_timing") is not None
                else "machine_timing"
            )
        start, end = _timing_parts(str(output_timing or ""))

        source_labels_bodies = [split_label(cue.text) for cue in source_group]
        machine_labels = {label for label, _body in source_labels_bodies}
        if len(machine_labels) != 1 or next(iter(machine_labels)) != raw_row.get(
            "machine_label"
        ):
            raise DeliveryCompileError(f"truth cue {truth_cue} machine label drift")
        source_machine_text = " ".join(body for _label, body in source_labels_bodies)
        if source_machine_text != raw_row.get("machine_text"):
            raise DeliveryCompileError(f"truth cue {truth_cue} machine text drift")
        raw_segments = raw_row.get("truth_segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise DeliveryCompileError(f"truth cue {truth_cue} has no truth segments")
        segments: list[dict[str, str | None]] = []
        removed_notes: list[str] = []
        for segment_position, segment in enumerate(raw_segments, start=1):
            if not isinstance(segment, Mapping):
                raise DeliveryCompileError(
                    f"truth cue {truth_cue} segment {segment_position} is invalid"
                )
            label = segment.get("label")
            if label is not None and label not in ALLOWED_SPEAKERS:
                raise DeliveryCompileError(
                    f"truth cue {truth_cue} segment {segment_position} speaker is invalid"
                )
            cleaned, removed = _strip_stage_notes(str(segment.get("text") or ""))
            removed_notes.extend(removed)
            segments.append({"speaker": label, "text": cleaned})
        if any(segment["speaker"] is None for segment in segments):
            if len(segments) != 1 or segments[0]["speaker"] is not None:
                raise DeliveryCompileError(
                    f"truth cue {truth_cue} mixes unresolved and reviewed speaker segments"
                )
            decision = arbitration_rows.get(truth_cue)
            if decision is None:
                raise DeliveryCompileError(
                    f"truth cue {truth_cue} requires audio arbitration"
                )
            used_arbitration.add(truth_cue)
            action = str(decision["action"])
            if action == "drop_hallucination":
                transform_rows.append(
                    {
                        "truth_cue": truth_cue,
                        "source_cues": source_numbers,
                        "final_cue": None,
                        "timing_policy": timing_policy,
                        "removed_stage_notes": removed_notes,
                        "speaker_disposition": "DROPPED_NO_HUMAN_VOICE",
                        "arbitration_reason": str(decision["reason"]),
                    }
                )
                continue
            clean_text = str(segments[0]["text"])
            final_cue = len(clean_rows) + 1
            clean_rows.append(
                {"source_cue": truth_cue, "start": start, "end": end, "text": clean_text}
            )
            expect = {"start": start, "end": end, "text": clean_text}
            machine_rows.append(
                {
                    "source_cue": final_cue,
                    "expect": expect,
                    "reason": str(decision["reason"]),
                    "arbitration": {
                        "human_voice_observed": True,
                        "receipt_sha256": receipt_sha,
                    },
                }
            )
            speaker_disposition = "MACHINE_DECISION_REQUIRED"
        else:
            if raw_row.get("text_changed") is True:
                clean_text, _ignored = _strip_stage_notes(
                    str(raw_row.get("truth_text") or "")
                )
            else:
                clean_text = source_machine_text
            segment_payload = "".join(str(segment["text"]) for segment in segments)
            if _normalise_payload(segment_payload) != _normalise_payload(clean_text):
                raise DeliveryCompileError(
                    f"truth cue {truth_cue} segment payload does not equal clean text"
                )
            final_cue = len(clean_rows) + 1
            clean_rows.append(
                {"source_cue": truth_cue, "start": start, "end": end, "text": clean_text}
            )
            timed_segments = _proportional_segments(
                start=start,
                end=end,
                segments=[
                    {"speaker": str(segment["speaker"]), "text": str(segment["text"])}
                    for segment in segments
                ],
            )
            overrides.append(
                {
                    "source_cue": final_cue,
                    "expect": {"start": start, "end": end, "text": clean_text},
                    "authority": authority,
                    "note": (
                        "Ivan reviewed speaker truth; mixed segment timing uses "
                        "the established character-proportion proxy and is not acoustic alignment."
                        if len(timed_segments) > 1
                        else "Ivan reviewed speaker truth; exact full-cue interval."
                    ),
                    "segments": timed_segments,
                }
            )
            speaker_disposition = (
                "REVIEWED_MIXED_PROXY_TIMING"
                if len(timed_segments) > 1
                else "REVIEWED_FULL_CUE"
            )
        transform_rows.append(
            {
                "truth_cue": truth_cue,
                "source_cues": source_numbers,
                "final_cue": final_cue,
                "timing_policy": timing_policy,
                "removed_stage_notes": removed_notes,
                "speaker_disposition": speaker_disposition,
            }
        )

    if consumed_source_cues != set(expected_source_ordinals):
        missing = sorted(set(expected_source_ordinals) - consumed_source_cues)
        raise DeliveryCompileError(f"truth input omitted source cues: {missing}")
    if used_arbitration != set(arbitration_rows):
        unused = sorted(set(arbitration_rows) - used_arbitration)
        raise DeliveryCompileError(f"machine-cue arbitration contains unused rows: {unused}")
    if not clean_rows:
        raise DeliveryCompileError("delivery baseline contains no cues")

    return _build_delivery_outputs(
        candidate_id=candidate_id,
        authority=authority,
        truth_path=truth_path,
        truth_relative=truth_relative,
        source_text_srt=source_text_srt,
        source_media=source_media,
        source_cue_count=len(source_cues),
        receipt_sha=receipt_sha,
        automatic_srt_sha256=automatic_srt_sha256,
        source_recording_basename=source_recording_basename,
        source_recording_sha256=source_recording_sha256,
        absolute_source_start_ms=absolute_source_start_ms,
        absolute_source_end_ms=absolute_source_end_ms,
        reviewed_at=reviewed_at,
        baseline_schema_version=baseline_schema_version,
        clean_rows=clean_rows,
        overrides=overrides,
        machine_rows=machine_rows,
        transform_rows=transform_rows,
    )


def _write_json(path: Path, document: object) -> None:
    atomic_write_text(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--source-text-srt", required=True, type=Path)
    parser.add_argument("--source-media", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--authority", required=True)
    parser.add_argument("--arbitration", required=True, type=Path)
    parser.add_argument("--automatic-srt-sha256", required=True)
    parser.add_argument("--source-recording-basename", required=True)
    parser.add_argument("--source-recording-sha256", required=True)
    parser.add_argument("--absolute-source-start-ms", required=True, type=int)
    parser.add_argument("--absolute-source-end-ms", required=True, type=int)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument(
        "--baseline-schema-version",
        choices=sorted(BASELINE_SCHEMAS),
        default=BASELINE_SCHEMA,
    )
    parser.add_argument("--baseline-srt-out", required=True, type=Path)
    parser.add_argument("--baseline-manifest-out", required=True, type=Path)
    parser.add_argument("--speaker-override-out", required=True, type=Path)
    parser.add_argument("--receipt-out", required=True, type=Path)
    args = parser.parse_args()
    arbitration = json.loads(args.arbitration.read_text(encoding="utf-8"))
    if not isinstance(arbitration, Mapping):
        raise DeliveryCompileError("arbitration input must be an object")
    output = compile_delivery(
        truth_path=args.truth,
        source_text_srt=args.source_text_srt,
        source_media=args.source_media,
        repo_root=args.repo_root,
        authority=args.authority,
        arbitration=arbitration,
        automatic_srt_sha256=args.automatic_srt_sha256,
        source_recording_basename=args.source_recording_basename,
        source_recording_sha256=args.source_recording_sha256,
        absolute_source_start_ms=args.absolute_source_start_ms,
        absolute_source_end_ms=args.absolute_source_end_ms,
        reviewed_at=args.reviewed_at,
        baseline_schema_version=args.baseline_schema_version,
    )
    atomic_write_text(args.baseline_srt_out, str(output["baseline_srt"]))
    baseline_manifest = dict(output["baseline_manifest"])
    baseline_manifest["path"] = args.baseline_srt_out.name
    _write_json(args.baseline_manifest_out, baseline_manifest)
    _write_json(args.speaker_override_out, output["speaker_override"])
    _write_json(args.receipt_out, output["receipt"])
    print(
        json.dumps(
            {
                "candidate_id": output["receipt"]["candidate_id"],
                "baseline_sha256": output["receipt"]["baseline"]["sha256"],
                "reviewed_override_count": output["receipt"]["reviewed_override_count"],
                "machine_cues": output["receipt"]["machine_cues"],
                "anchor_source_cues": output["receipt"]["anchor_source_cues"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
