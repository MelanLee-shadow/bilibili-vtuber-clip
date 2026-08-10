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

from scripts.apply_speaker_turn_overrides import Cue, atomic_write_text, parse_labelled_srt
from scripts.harvest_ivan_truth import parse_srt, split_label
from src.autoslice.reviewed_speaker_baseline import (
    REVIEWED_SPEAKER_BASELINE_SCHEMA,
    TRUTH_SCHEMA,
    canonical_labelled_srt_sha256,
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
DELIVERY_RECEIPT_SCHEMA = "reviewed-speaker-truth-delivery.v2"
TRUTH_FULL_OWNERSHIP_PIN_SCHEMA = "truth-full-ownership-pin.v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
TIMING_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\Z"
)
# Ivan 2026-08-10 令：真值里的圆括号注记分两类，且只有机器类可以被剥。
#   - MACHINE（下面这张白名单）＝标注者对**机器听写可信度**的元评论
#     （「(无可辨别人声)」「(无可分辨人声)」）。它不是李豆沙说的话，
#     烧进字幕就是把审阅便签当台词，必须剥掉。
#   - CONTENT＝Ivan 手写的动作/舞台注记（「(跃起)」等）。它是内容的一部分，
#     **原样保留并渲染**。1323/104「你起什么哄啊(跃起)」、1323/128
#     「你不许再说话(跃起)」正是被旧的一刀切规则误删的实证。
# 判别用白名单类别（而不是「凡括号皆注记」或「按标注来源猜」）：机器类是一个
# 封闭、可枚举、语义单一的短语集；内容类是开放集，天然只能靠 fail-open 保留。
MACHINE_STAGE_NOTES = ("无可辨别人声", "无可分辨人声")
MACHINE_STAGE_NOTE_RE = re.compile(
    r"\s*(?:\(|（)(" + "|".join(MACHINE_STAGE_NOTES) + r")(?:\)|）)\s*\Z"
)
CONTENT_NOTE_RE = re.compile(r"(?:\(|（)([^()（）]+)(?:\)|）)")
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


def _strip_machine_notes(value: str) -> tuple[str, list[str]]:
    """Remove only the machine-annotation notes; keep every content note."""

    text = str(value).strip()
    removed: list[str] = []
    while True:
        match = MACHINE_STAGE_NOTE_RE.search(text)
        if match is None:
            break
        removed.insert(0, match.group(1))
        text = text[: match.start()].rstrip()
    if not text:
        raise DeliveryCompileError("stage-note stripping produced empty subtitle text")
    return text, removed


def _content_notes(value: str) -> list[str]:
    """Disclose the parenthetical notes that stay in the delivered subtitle."""

    return [
        note.strip()
        for note in CONTENT_NOTE_RE.findall(str(value))
        if note.strip() and note.strip() not in MACHINE_STAGE_NOTES
    ]


def _strip_render_boundary_separator(
    value: str,
    *,
    segment_position: int,
) -> tuple[str, str | None]:
    """Keep punctuation in the clean cue while removing a routed line prefix.

    Ivan's annotation format may place one full-width comma at the start of a
    non-initial speaker segment to mark the boundary in the unsplit sentence.
    That comma belongs in the clean sidecar cue, but must not become the first
    visible glyph of the separately rendered speaker segment.
    """

    if segment_position <= 1 or not value.startswith("，"):
        return value, None
    cleaned = value[1:].lstrip()
    if not cleaned:
        raise DeliveryCompileError(
            "speaker render-boundary separator produced empty segment text"
        )
    return cleaned, "，"


def _routed_segment_text(
    cleaned: str,
    *,
    segment_position: int,
    removed: list[dict[str, object]],
) -> str:
    """Return the rendered segment text, recording any stripped separator.

    合并 ft-a8600994 时,HEAD 的 `_content_notes` 披露与 ft 的行首分隔符剥离都落在
    `compile_delivery` 的同一个循环里,把它顶过 300 行入场线。按账本规则「拆掉它」,
    这里只把分隔符记账抽成模块级 helper,判据一字未动。
    """

    render_text, removed_separator = _strip_render_boundary_separator(
        cleaned,
        segment_position=segment_position,
    )
    if removed_separator is not None:
        removed.append(
            {"segment_position": segment_position, "separator": removed_separator}
        )
    return render_text


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
    automatic_srt_sha256: str,
) -> tuple[str, dict[int, Mapping[str, object]]]:
    if document.get("schema_version") != ARBITRATION_SCHEMA:
        raise DeliveryCompileError("machine-cue arbitration schema is unsupported")
    if document.get("candidate_id") != candidate_id:
        raise DeliveryCompileError("machine-cue arbitration candidate mismatch")
    if document.get("automatic_labelled_srt_sha256") != automatic_srt_sha256:
        raise DeliveryCompileError("machine-cue arbitration automatic SRT hash mismatch")
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
        speaker = decision.get("speaker")
        if decision["action"] == "retain_machine" and speaker not in ALLOWED_SPEAKERS:
            raise DeliveryCompileError(
                f"machine-cue arbitration speaker is invalid: {cue_number}"
            )
        if decision["action"] == "drop_hallucination" and speaker is not None:
            raise DeliveryCompileError(
                f"dropped machine-cue arbitration must not assign a speaker: {cue_number}"
            )
        decisions[cue_number] = decision
    return receipt_sha, decisions


def _load_automatic_speaker_srt(
    path: Path,
    *,
    repo_root: Path,
) -> tuple[str, str, list[Cue]]:
    candidate = path.absolute()
    if not candidate.is_relative_to(repo_root):
        raise DeliveryCompileError("automatic speaker SRT must be inside the repository")
    relative = candidate.relative_to(repo_root)
    current = repo_root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise DeliveryCompileError("automatic speaker SRT must not traverse symlinks")
    try:
        resolved = candidate.resolve(strict=True)
        cues = parse_labelled_srt(resolved)
    except (OSError, ValueError) as exc:
        raise DeliveryCompileError("automatic speaker SRT is invalid") from exc
    if not resolved.is_relative_to(repo_root) or not resolved.is_file():
        raise DeliveryCompileError("automatic speaker SRT escapes the repository")
    actual_sha256 = _sha256(resolved)
    if canonical_labelled_srt_sha256(cues) != actual_sha256:
        raise DeliveryCompileError(
            "automatic speaker SRT is not canonical labelled SRT bytes"
        )
    return str(relative), actual_sha256, cues


def _validate_automatic_speaker_grid(
    automatic_cues: list[Cue],
    *,
    clean_rows: list[dict[str, object]],
    machine_rows: list[dict[str, object]],
) -> None:
    if len(automatic_cues) != len(clean_rows):
        raise DeliveryCompileError("automatic speaker SRT cue count drift")
    for position, (automatic_cue, clean_row) in enumerate(
        zip(automatic_cues, clean_rows, strict=True), start=1
    ):
        if getattr(automatic_cue, "source_index", None) != position:
            raise DeliveryCompileError(
                "automatic speaker SRT cue indices are not contiguous"
            )
        for field in ("start", "end", "text"):
            if getattr(automatic_cue, field, None) != clean_row[field]:
                raise DeliveryCompileError(
                    f"automatic speaker SRT cue {position} {field} drift"
                )
    for machine_row in machine_rows:
        cue_number = int(machine_row["source_cue"])
        if getattr(automatic_cues[cue_number - 1], "speaker", None) != machine_row["speaker"]:
            raise DeliveryCompileError(
                f"automatic speaker SRT machine cue {cue_number} label drift"
            )


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
    automatic_srt_relative: str,
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
    # F20 覆盖证明：每一条交付 cue 的文字都来自这份 hash-bound 真值，而且它
    # 恰好属于「Ivan 复核的说话人 override」或「Ivan 留给机器判说话人、但由
    # 正面人声仲裁保住的 cue」两桶之一。编译器的分桶本来就是穷尽的，这里把
    # 它写成显式不变量，pin 才有资格当下游快路径的授权凭据。
    if len(overrides) + len(machine_rows) != len(clean_rows):
        raise DeliveryCompileError("truth cue ownership does not cover the delivery grid")
    truth_full_ownership_pin = {
        "schema_version": TRUTH_FULL_OWNERSHIP_PIN_SCHEMA,
        "authority": authority,
        "truth_input": {"path": truth_relative, "sha256": truth_sha},
        "baseline_sha256": baseline_sha,
        "arbitration_receipt_sha256": receipt_sha,
        "cue_count": len(clean_rows),
        "reviewed_override_count": len(overrides),
        "machine_cues": [int(row["source_cue"]) for row in machine_rows],
    }
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
                # 只有精确区间重放才让文字所有权 100% 落到真值上；v1 基线是
                # 按窗口合并的，交付文本仍可能混入本轮 ASR，不发这张 pin。
                "truth_full_ownership": truth_full_ownership_pin,
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
            "automatic_input": {
                "path": automatic_srt_relative,
                "sha256": automatic_srt_sha256,
            },
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
        "automatic_labelled_srt": {
            "path": automatic_srt_relative,
            "sha256": automatic_srt_sha256,
            "cue_count": len(clean_rows),
        },
        "arbitration_receipt_sha256": receipt_sha,
        "reviewed_override_count": len(overrides),
        "machine_cues": [row["source_cue"] for row in machine_rows],
        "anchor_source_cues": anchors,
        "truth_full_ownership": truth_full_ownership_pin,
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
    automatic_srt_path: Path,
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
    automatic_srt_relative, automatic_srt_sha256, automatic_cues = (
        _load_automatic_speaker_srt(automatic_srt_path, repo_root=repo_root)
    )
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
        automatic_srt_sha256=automatic_srt_sha256,
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
        segment_payload_parts: list[str] = []
        removed_notes: list[str] = []
        retained_notes: list[str] = []
        removed_render_boundary_separators: list[dict[str, object]] = []
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
            cleaned, removed = _strip_machine_notes(str(segment.get("text") or ""))
            removed_notes.extend(removed)
            retained_notes.extend(_content_notes(cleaned))
            segment_payload_parts.append(cleaned)
            render_text = _routed_segment_text(
                cleaned,
                segment_position=segment_position,
                removed=removed_render_boundary_separators,
            )
            segments.append({"speaker": label, "text": render_text})
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
                        "removed_machine_notes": removed_notes,
                        "retained_content_notes": retained_notes,
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
                    "speaker": str(decision["speaker"]),
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
                clean_text, _ignored = _strip_machine_notes(
                    str(raw_row.get("truth_text") or "")
                )
            else:
                clean_text = source_machine_text
            segment_payload = "".join(segment_payload_parts)
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
        transform_row: dict[str, object] = {
            "truth_cue": truth_cue,
            "source_cues": source_numbers,
            "final_cue": final_cue,
            "timing_policy": timing_policy,
            "removed_machine_notes": removed_notes,
            "retained_content_notes": retained_notes,
            "speaker_disposition": speaker_disposition,
        }
        if removed_render_boundary_separators:
            transform_row["removed_render_boundary_separators"] = (
                removed_render_boundary_separators
            )
        transform_rows.append(transform_row)

    if consumed_source_cues != set(expected_source_ordinals):
        missing = sorted(set(expected_source_ordinals) - consumed_source_cues)
        raise DeliveryCompileError(f"truth input omitted source cues: {missing}")
    if used_arbitration != set(arbitration_rows):
        unused = sorted(set(arbitration_rows) - used_arbitration)
        raise DeliveryCompileError(f"machine-cue arbitration contains unused rows: {unused}")
    if not clean_rows:
        raise DeliveryCompileError("delivery baseline contains no cues")
    _validate_automatic_speaker_grid(
        automatic_cues,
        clean_rows=clean_rows,
        machine_rows=machine_rows,
    )

    return _build_delivery_outputs(
        candidate_id=candidate_id,
        authority=authority,
        truth_path=truth_path,
        truth_relative=truth_relative,
        source_text_srt=source_text_srt,
        source_media=source_media,
        source_cue_count=len(source_cues),
        receipt_sha=receipt_sha,
        automatic_srt_relative=automatic_srt_relative,
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
    parser.add_argument("--automatic-speaker-srt", required=True, type=Path)
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
        automatic_srt_path=args.automatic_speaker_srt,
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
