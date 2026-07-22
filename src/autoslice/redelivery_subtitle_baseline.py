"""Fail-closed subtitle text preservation for reviewed redeliveries.

A subtitle-only rerun still invokes nondeterministic ASR and reviewer stages.
Without an explicit baseline, unrelated cues can silently change while the
operator is fixing one known incident or merely re-burning media.  This module
binds a previous delivered SRT by hash and projects only its text onto the new
cue timing outside higher-authority source-truth windows.

Timing is never copied from the baseline.  Every current cue and every prior
cue must align one-to-one by strong timeline overlap; segmentation drift,
missing cues, ambiguous matches, or a stale hash fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues


SCHEMA_VERSION = "subtitle-redelivery-baseline.v1"
AUDIT_SCHEMA_VERSION = "subtitle-redelivery-baseline-audit.v1"
MODE = "preserve_text_outside_source_truth"
MIN_ALIGNMENT_OVERLAP_MS = 80
MIN_ALIGNMENT_RATIO = 0.80
_SHA256_RX = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _format_ms(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _render(cues: Sequence[SrtCue], texts: Sequence[str]) -> str:
    return "\n\n".join(
        f"{index}\n{_format_ms(cue.start_ms)} --> {_format_ms(cue.end_ms)}\n{text}"
        for index, (cue, text) in enumerate(zip(cues, texts), start=1)
    ) + ("\n" if cues else "")


def _overlap_ms(left: SrtCue, right: SrtCue) -> int:
    return max(0, min(left.end_ms, right.end_ms) - max(left.start_ms, right.start_ms))


def _protected(cue: SrtCue, windows: Sequence[tuple[int, int]]) -> bool:
    return any(
        max(0, min(cue.end_ms, end_ms) - max(cue.start_ms, start_ms))
        >= MIN_ALIGNMENT_OVERLAP_MS
        for start_ms, end_ms in windows
    )


def _read_config(
    config: Mapping[str, Any], *, spec_parent: Path
) -> tuple[Path, str, str]:
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("REDELIVERY_BASELINE_SCHEMA_INVALID")
    if config.get("mode") != MODE:
        raise ValueError("REDELIVERY_BASELINE_MODE_INVALID")
    raw_path = config.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("REDELIVERY_BASELINE_PATH_MISSING")
    path = Path(raw_path)
    if not path.is_absolute():
        path = spec_parent / path
    if not path.is_file() or path.is_symlink():
        raise ValueError("REDELIVERY_BASELINE_PATH_INVALID")
    path = path.resolve()
    expected_match = _SHA256_RX.fullmatch(str(config.get("sha256") or ""))
    if expected_match is None:
        raise ValueError("REDELIVERY_BASELINE_SHA256_INVALID")
    authority = str(config.get("authority") or "").strip()
    if not authority:
        raise ValueError("REDELIVERY_BASELINE_AUTHORITY_MISSING")
    return path, expected_match.group(1), authority


def apply_redelivery_subtitle_baseline(
    current_srt: str,
    *,
    config: Mapping[str, Any] | None,
    spec_parent: Path,
    protected_windows: Sequence[tuple[int, int]] = (),
) -> tuple[str, dict[str, Any]]:
    """Preserve baseline text outside reviewed source-truth windows."""

    audit: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": "SKIPPED_NOT_CONFIGURED",
        "mode": MODE,
        "baseline_path": None,
        "baseline_sha256": None,
        "current_input_sha256": _sha256_bytes(current_srt.encode("utf-8")),
        "output_sha256": None,
        "protected_intervals": [
            {"start_ms": int(start_ms), "end_ms": int(end_ms)}
            for start_ms, end_ms in protected_windows
        ],
        "owned_intervals": [],
        "mappings": [],
        "failures": [],
    }
    if config is None:
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit
    if not isinstance(config, Mapping):
        audit["status"] = "FAILED"
        audit["failures"].append({"reason_code": "REDELIVERY_BASELINE_CONFIG_INVALID"})
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit

    try:
        path, expected_sha256, authority = _read_config(
            config, spec_parent=spec_parent
        )
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        audit["status"] = "FAILED"
        audit["failures"].append({"reason_code": str(exc)})
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit

    actual_sha256 = _sha256_bytes(raw)
    audit.update(
        {
            "baseline_path": str(path),
            "baseline_sha256": actual_sha256,
            "expected_baseline_sha256": expected_sha256,
            "authority": authority,
        }
    )
    if actual_sha256 != expected_sha256:
        audit["status"] = "FAILED"
        audit["failures"].append({"reason_code": "REDELIVERY_BASELINE_SHA256_MISMATCH"})
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit

    try:
        baseline_srt = raw.decode("utf-8")
    except UnicodeDecodeError:
        audit["status"] = "FAILED"
        audit["failures"].append({"reason_code": "REDELIVERY_BASELINE_UTF8_INVALID"})
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit
    current = parse_srt_cues(current_srt)
    baseline = parse_srt_cues(baseline_srt)
    if not current or not baseline:
        audit["status"] = "FAILED"
        audit["failures"].append({"reason_code": "REDELIVERY_BASELINE_SRT_EMPTY"})
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit

    current_indexes = [
        index for index, cue in enumerate(current) if not _protected(cue, protected_windows)
    ]
    baseline_indexes = [
        index for index, cue in enumerate(baseline) if not _protected(cue, protected_windows)
    ]
    mappings: list[tuple[int, int, int, float]] = []
    used_baseline: set[int] = set()
    for current_index in current_indexes:
        current_cue = current[current_index]
        candidates: list[tuple[int, int, float]] = []
        for baseline_index in baseline_indexes:
            if baseline_index in used_baseline:
                continue
            baseline_cue = baseline[baseline_index]
            overlap = _overlap_ms(current_cue, baseline_cue)
            denominator = max(
                1,
                min(
                    current_cue.end_ms - current_cue.start_ms,
                    baseline_cue.end_ms - baseline_cue.start_ms,
                ),
            )
            ratio = overlap / denominator
            if overlap >= MIN_ALIGNMENT_OVERLAP_MS and ratio >= MIN_ALIGNMENT_RATIO:
                candidates.append((baseline_index, overlap, ratio))
        candidates.sort(key=lambda row: (row[1], row[2]), reverse=True)
        if not candidates:
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_CURRENT_CUE_UNALIGNED",
                    "current_cue_index": current_index + 1,
                    "start_ms": current_cue.start_ms,
                    "end_ms": current_cue.end_ms,
                    "text": current_cue.text,
                }
            )
            continue
        if len(candidates) > 1 and candidates[0][1:] == candidates[1][1:]:
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_CURRENT_CUE_ALIGNMENT_AMBIGUOUS",
                    "current_cue_index": current_index + 1,
                    "baseline_cue_indexes": [row[0] + 1 for row in candidates[:2]],
                }
            )
            continue
        baseline_index, overlap, ratio = candidates[0]
        used_baseline.add(baseline_index)
        mappings.append((current_index, baseline_index, overlap, ratio))

    for baseline_index in baseline_indexes:
        if baseline_index not in used_baseline:
            baseline_cue = baseline[baseline_index]
            audit["failures"].append(
                {
                    "reason_code": "REDELIVERY_BASELINE_CUE_UNCONSUMED",
                    "baseline_cue_index": baseline_index + 1,
                    "start_ms": baseline_cue.start_ms,
                    "end_ms": baseline_cue.end_ms,
                    "text": baseline_cue.text,
                }
            )

    if audit["failures"]:
        audit["status"] = "FAILED"
        audit["output_sha256"] = audit["current_input_sha256"]
        return current_srt, audit

    texts = [cue.text for cue in current]
    changed_count = 0
    for current_index, baseline_index, overlap, ratio in mappings:
        current_cue = current[current_index]
        baseline_cue = baseline[baseline_index]
        before = texts[current_index]
        after = baseline_cue.text
        if before != after:
            changed_count += 1
            texts[current_index] = after
        audit["mappings"].append(
            {
                "current_cue_index": current_index + 1,
                "baseline_cue_index": baseline_index + 1,
                "start_ms": current_cue.start_ms,
                "end_ms": current_cue.end_ms,
                "overlap_ms": overlap,
                "overlap_ratio": round(ratio, 6),
                "changed": before != after,
                "before": before,
                "after": after,
            }
        )
        audit["owned_intervals"].append(
            {"start_ms": current_cue.start_ms, "end_ms": current_cue.end_ms}
        )

    output = _render(current, texts)
    audit.update(
        {
            "status": "APPLIED" if changed_count else "ALREADY_SATISFIED",
            "mapped_cue_count": len(mappings),
            "changed_cue_count": changed_count,
            "protected_cue_count": len(current) - len(current_indexes),
            "output_sha256": _sha256_bytes(output.encode("utf-8")),
        }
    )
    return output, audit
