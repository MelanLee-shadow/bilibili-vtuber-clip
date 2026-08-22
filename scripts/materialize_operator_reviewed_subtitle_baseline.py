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
from src.autoslice.qixi_cue21_diagnostic_evidence import (
    QixiCue21DiagnosticEvidenceError,
    validate_qixi_cue21_diagnostic_evidence,
)


REGISTRY_SCHEMA = "candidate-reviewed-subtitle-baseline.v1"
BASELINE_SCHEMA = "subtitle-redelivery-baseline.v2"
BASELINE_MODE = "preserve_text_outside_source_truth"
RECEIPT_SCHEMA = "operator-reviewed-subtitle-baseline-delivery.v1"
TEXT_OWNERSHIP_PIN_SCHEMA = "operator-reviewed-text-full-ownership-pin.v2"
TEXT_OWNERSHIP_PIN_V3_SCHEMA = "operator-reviewed-text-full-ownership-pin.v3"
DECISION_LEDGER_SCHEMA = "operator-reviewed-subtitle-decisions.v2"
DECISION_LEDGER_V3_SCHEMA = "operator-reviewed-subtitle-decisions.v3"
LEGACY_DECISION_LEDGER_SCHEMA = "operator-reviewed-subtitle-decisions.v1"
TRUTH_LANES_SCHEMA = "operator-reviewed-subtitle-truth-lanes.v1"
TRUTH_LANES_V2_SCHEMA = "operator-reviewed-subtitle-truth-lanes.v2"
DIAGNOSTIC_DIFF_SCHEMA = "operator-reviewed-subtitle-truth-diff.v1"
DIAGNOSTIC_DIFF_V2_SCHEMA = "operator-reviewed-subtitle-truth-diff.v2"
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


def _text_sha256(value: str) -> str:
    return _sha256_bytes(value.strip().encode("utf-8"))


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


def _require_operator_authority(value: object, *, label: str) -> dict[str, str]:
    """Accept only a typed Ivan decision, never a free-form attribution string."""

    if not isinstance(value, dict) or set(value) != {"kind", "evidence_ref"}:
        raise OperatorBaselineCompileError(f"{label} must be typed Ivan operator authority")
    kind = value.get("kind")
    evidence_ref = value.get("evidence_ref")
    if kind != "IVAN_OPERATOR" or not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise OperatorBaselineCompileError(f"{label} must be typed Ivan operator authority")
    return {"kind": kind, "evidence_ref": evidence_ref.strip()}


def _operator_text_ownership_pin(
    *,
    baseline_sha: str,
    pipeline_sha: str,
    decision_ledger_sha: str,
    diagnostic_diff_sha: str,
    authority: dict[str, str],
    cue_count: int,
    changed_cue_count: int,
    exact_count: int,
    freeze_count: int,
    drop_count: int = 0,
    source_cue_count: int | None = None,
) -> dict[str, object]:
    if source_cue_count is not None:
        return {
            "schema_version": TEXT_OWNERSHIP_PIN_V3_SCHEMA,
            "baseline_sha256": baseline_sha,
            "pipeline_srt_sha256": pipeline_sha,
            "decision_ledger_sha256": decision_ledger_sha,
            "diagnostic_diff_sha256": diagnostic_diff_sha,
            "operator_authority": authority,
            "source_cue_count": source_cue_count,
            "release_cue_count": cue_count,
            "changed_cue_count": changed_cue_count,
            "operator_exact_text_cue_count": exact_count,
            "operator_unchanged_freeze_cue_count": freeze_count,
            "operator_drop_cue_count": drop_count,
            "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
        }
    return {
        "schema_version": TEXT_OWNERSHIP_PIN_SCHEMA,
        "baseline_sha256": baseline_sha,
        "pipeline_srt_sha256": pipeline_sha,
        "decision_ledger_sha256": decision_ledger_sha,
        "diagnostic_diff_sha256": diagnostic_diff_sha,
        "operator_authority": authority,
        "cue_count": cue_count,
        "changed_cue_count": changed_cue_count,
        "operator_exact_text_cue_count": exact_count,
        "operator_unchanged_freeze_cue_count": freeze_count,
        "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
    }


def _validate_v3_decision_ledger(
    *,
    ledger: object,
    candidate_id: str,
    source_sha256: str,
    source_cues: list[Any],
    reviewed_cues: list[Any],
) -> tuple[dict[str, Any], list[dict[str, object]], int, int, int]:
    """Validate exhaustive KEEP/REPLACE/DROP decisions without empty cues."""

    expected = {
        "schema_version", "candidate_id", "report_scope", "pipeline_srt_sha256",
        "operator_authority", "cue_decisions",
    }
    if not isinstance(ledger, dict) or set(ledger) != expected or ledger.get("schema_version") != DECISION_LEDGER_V3_SCHEMA:
        raise OperatorBaselineCompileError("v3 decision ledger schema is invalid")
    if ledger.get("candidate_id") != candidate_id or ledger.get("report_scope") != "EXHAUSTIVE":
        raise OperatorBaselineCompileError("v3 decision ledger identity is invalid")
    if _require_sha256(ledger.get("pipeline_srt_sha256"), label="pipeline SRT") != source_sha256:
        raise OperatorBaselineCompileError("v3 decision ledger pipeline SRT sha256 drift")
    authority = _require_operator_authority(ledger.get("operator_authority"), label="decision ledger operator authority")
    rows = ledger.get("cue_decisions")
    if not isinstance(rows, list) or len(rows) != len(source_cues):
        raise OperatorBaselineCompileError("v3 decision ledger must account for every source cue")

    release_cursor = 0
    exact_count = freeze_count = drop_count = 0
    diagnostics: list[dict[str, object]] = []
    for ordinal, (source, row) in enumerate(zip(source_cues, rows, strict=True), start=1):
        common = {"cue", "disposition"}
        if not isinstance(row, dict) or not common.issubset(row):
            raise OperatorBaselineCompileError("v3 decision ledger cue row is incomplete")
        if row.get("cue") != ordinal:
            raise OperatorBaselineCompileError("v3 decision ledger source cue binding drift")
        before = str(source.text).strip()
        disposition = row.get("disposition")
        diagnostic: dict[str, object] = {
            "cue": ordinal, "source_index": str(source.index),
            "start_ms": source.start_ms, "end_ms": source.end_ms,
            "pipeline_text": before, "disposition": disposition,
        }
        if disposition == "OPERATOR_DROP":
            if set(row) != common | {"decision_authority", "drop_reason"}:
                raise OperatorBaselineCompileError("operator drop row is invalid")
            diagnostic.update({"release_cue_index": None, "release_truth_text": None,
                               "decision_authority": _v3_row_authority(row.get("decision_authority"), ledger_authority=authority),
                               "drop_reason": _required_drop_reason(row.get("drop_reason"))})
            drop_count += 1
        else:
            if release_cursor >= len(reviewed_cues):
                raise OperatorBaselineCompileError("v3 reviewed SRT is missing a retained cue")
            reviewed = reviewed_cues[release_cursor]
            release_index = release_cursor + 1
            if str(reviewed.index) != str(release_index) or (reviewed.start_ms, reviewed.end_ms) != (source.start_ms, source.end_ms):
                raise OperatorBaselineCompileError("v3 reviewed SRT release grid drift")
            after = str(reviewed.text).strip()
            if not after:
                raise OperatorBaselineCompileError("v3 reviewed SRT retained cue is empty")
            diagnostic.update({"release_cue_index": release_index, "release_truth_text": after})
            if disposition == "OPERATOR_UNCHANGED_FREEZE":
                if set(row) != common or before != after:
                    raise OperatorBaselineCompileError("v3 unchanged freeze must preserve its source cue")
                freeze_count += 1
            elif disposition == "OPERATOR_EXACT_TEXT":
                if set(row) != common | {"release_text", "decision_authority"} or row.get("release_text") != after:
                    raise OperatorBaselineCompileError("v3 operator exact text is invalid")
                diagnostic["decision_authority"] = _v3_row_authority(row.get("decision_authority"), ledger_authority=authority)
                exact_count += 1
            else:
                raise OperatorBaselineCompileError("v3 decision ledger disposition is invalid")
            release_cursor += 1
        diagnostics.append(diagnostic)
    if release_cursor != len(reviewed_cues) or not (exact_count + drop_count):
        raise OperatorBaselineCompileError("v3 decision ledger release coverage is invalid")
    return (
        {"schema_version": DECISION_LEDGER_V3_SCHEMA, "candidate_id": candidate_id,
         "report_scope": "EXHAUSTIVE", "pipeline_srt_sha256": source_sha256,
         "operator_authority": authority, "cue_decisions": rows},
        diagnostics, exact_count, freeze_count, drop_count,
    )


def _required_drop_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperatorBaselineCompileError("operator drop reason is required")
    return value.strip()


def _v3_row_authority(value: object, *, ledger_authority: dict[str, str]) -> dict[str, str]:
    if value == "LEDGER_OPERATOR_AUTHORITY":
        return ledger_authority
    return _require_operator_authority(value, label="v3 operator cue decision")


def _require_sha256(value: object, *, label: str) -> str:
    text = str(value or "").removeprefix("sha256:")
    if not _SHA256_RX.fullmatch(text):
        raise OperatorBaselineCompileError(f"{label} sha256 is invalid")
    return text


def _validate_decision_ledger(
    *,
    ledger: object,
    candidate_id: str,
    source_sha256: str,
    source_cues: list[Any],
    reviewed_cues: list[Any],
    decision_ledger_root: Path | None,
) -> tuple[dict[str, Any], list[dict[str, object]], int, int]:
    """Validate a complete per-cue release-truth decision ledger.

    The source input is the independently retained pipeline result.  A model
    proposal is welcome in this ledger for diagnosis, but it can never change
    the release SRT.  Only an explicitly typed Ivan decision can do that.
    """

    if not isinstance(ledger, dict):
        raise OperatorBaselineCompileError("decision ledger must be an object")
    expected = {
        "schema_version",
        "candidate_id",
        "report_scope",
        "pipeline_srt_sha256",
        "operator_authority",
        "cue_decisions",
    }
    if set(ledger) != expected or ledger.get("schema_version") not in {
        LEGACY_DECISION_LEDGER_SCHEMA, DECISION_LEDGER_SCHEMA
    }:
        raise OperatorBaselineCompileError("decision ledger schema is invalid")
    if ledger.get("candidate_id") != candidate_id:
        raise OperatorBaselineCompileError("decision ledger candidate id drift")
    if ledger.get("report_scope") != "EXHAUSTIVE":
        raise OperatorBaselineCompileError("decision ledger must declare exhaustive report scope")
    if _require_sha256(ledger.get("pipeline_srt_sha256"), label="pipeline SRT") != source_sha256:
        raise OperatorBaselineCompileError("decision ledger pipeline SRT sha256 drift")
    operator_authority = _require_operator_authority(
        ledger.get("operator_authority"), label="decision ledger operator authority"
    )
    rows = ledger.get("cue_decisions")
    if not isinstance(rows, list) or len(rows) != len(source_cues):
        raise OperatorBaselineCompileError("decision ledger must contain one decision per cue")

    expected_ordinals = set(range(1, len(source_cues) + 1))
    seen: set[int] = set()
    diagnostic_rows: list[dict[str, object]] = []
    exact_count = 0
    freeze_count = 0
    for ordinal, (source, reviewed, row) in enumerate(
        zip(source_cues, reviewed_cues, rows, strict=True), start=1
    ):
        if not isinstance(row, dict):
            raise OperatorBaselineCompileError("decision ledger cue row is invalid")
        required = {
            "cue",
            "source_index",
            "start_ms",
            "end_ms",
            "source_text_sha256",
            "disposition",
        }
        if not required.issubset(row):
            raise OperatorBaselineCompileError("decision ledger cue row is incomplete")
        if set(row) - (
            required
            | {"release_text", "decision_authority", "proposal", "rejected_machine_proposal", "blocking_evidence", "diagnostic_evidence"}
        ):
            raise OperatorBaselineCompileError("decision ledger cue row has unsupported fields")
        if row.get("cue") != ordinal or row.get("source_index") != str(source.index):
            raise OperatorBaselineCompileError("decision ledger cue index drift")
        if (row.get("start_ms"), row.get("end_ms")) != (source.start_ms, source.end_ms):
            raise OperatorBaselineCompileError("decision ledger cue timing drift")
        if _require_sha256(row.get("source_text_sha256"), label="decision ledger source text") != _text_sha256(str(source.text)):
            raise OperatorBaselineCompileError("decision ledger source text drift")
        if ordinal in seen:
            raise OperatorBaselineCompileError("decision ledger cue is duplicated")
        seen.add(ordinal)
        before = str(source.text).strip()
        after = str(reviewed.text).strip()
        disposition = row.get("disposition")
        diagnostic: dict[str, object] = {
            "cue": ordinal,
            "source_index": str(source.index),
            "start_ms": source.start_ms,
            "end_ms": source.end_ms,
            "pipeline_text": before,
            "release_truth_text": after,
            "disposition": disposition,
        }
        if disposition == "OPERATOR_EXACT_TEXT":
            if row.get("release_text") != after:
                raise OperatorBaselineCompileError("operator exact text must bind the release text")
            decision_authority = _require_operator_authority(
                row.get("decision_authority"), label="operator exact text decision"
            )
            if "proposal" in row:
                raise OperatorBaselineCompileError("operator exact text cannot carry a machine proposal")
            diagnostic["decision_authority"] = decision_authority
            evidence = row.get("diagnostic_evidence")
            if evidence is not None:
                if (
                    not isinstance(evidence, dict)
                    or set(evidence) != {"path", "sha256", "reason"}
                    or not isinstance(evidence.get("path"), str)
                    or not evidence["path"]
                    or not isinstance(evidence.get("reason"), str)
                    or not evidence["reason"]
                ):
                    raise OperatorBaselineCompileError("operator diagnostic evidence is invalid")
                _require_sha256(evidence.get("sha256"), label="operator diagnostic evidence")
                if decision_ledger_root is None:
                    raise OperatorBaselineCompileError("operator diagnostic evidence root is required")
                try:
                    validate_qixi_cue21_diagnostic_evidence(
                        evidence,
                        evidence_root=decision_ledger_root,
                        candidate_id=candidate_id,
                        source_basename="22966160_20260817-11-30-22.mp4",
                        source_sha256="212eb59bee50dda1601a3f30e3c7b9a307715058fb472646ddcfa200615c6a97",
                        cue_start_ms=source.start_ms,
                        cue_end_ms=source.end_ms,
                        absolute_source_start_ms=354420,
                    )
                except QixiCue21DiagnosticEvidenceError as exc:
                    raise OperatorBaselineCompileError(f"operator diagnostic evidence is invalid: {exc}") from exc
                diagnostic["diagnostic_evidence"] = dict(evidence)
            rejected = row.get("rejected_machine_proposal")
            if rejected is not None:
                if not isinstance(rejected, dict) or set(rejected) != {"text", "provenance"}:
                    raise OperatorBaselineCompileError("rejected machine proposal is invalid")
                provenance = rejected.get("provenance")
                if (
                    not isinstance(rejected.get("text"), str)
                    or not rejected["text"].strip()
                    or not isinstance(provenance, dict)
                    or set(provenance) != {"provider", "artifact_sha256"}
                    or not isinstance(provenance.get("provider"), str)
                    or not provenance["provider"].strip()
                ):
                    raise OperatorBaselineCompileError("rejected machine proposal is invalid")
                _require_sha256(provenance.get("artifact_sha256"), label="rejected machine proposal artifact")
                diagnostic["rejected_machine_proposal"] = {
                    "text": rejected["text"].strip(),
                    "provenance": dict(provenance),
                    "disposition": "REJECTED_BY_OPERATOR_EXACT_TEXT",
                }
            exact_count += 1
        elif disposition == "DELEGATED_PROVIDER_REVIEW_UNRESOLVED":
            if (
                before != after
                or set(row) != required | {"blocking_evidence"}
                or not isinstance(row.get("blocking_evidence"), dict)
                or set(row["blocking_evidence"]) != {"path", "sha256", "reason"}
                or not isinstance(row["blocking_evidence"].get("path"), str)
                or not row["blocking_evidence"]["path"]
                or not isinstance(row["blocking_evidence"].get("reason"), str)
                or not row["blocking_evidence"]["reason"]
            ):
                raise OperatorBaselineCompileError("unresolved delegated provider review is invalid")
            _require_sha256(row["blocking_evidence"].get("sha256"), label="unresolved provider evidence")
            raise OperatorBaselineCompileError(
                "unresolved delegated provider review cannot materialize a release baseline"
            )
        elif disposition == "OPERATOR_UNCHANGED_FREEZE":
            if (
                before != after
                or "release_text" in row
                or "decision_authority" in row
                or "rejected_machine_proposal" in row
            ):
                raise OperatorBaselineCompileError("unchanged freeze must preserve the pipeline cue exactly")
            proposal = row.get("proposal")
            if proposal is not None:
                if not isinstance(proposal, dict) or set(proposal) != {"text", "provenance"}:
                    raise OperatorBaselineCompileError("machine proposal is invalid")
                provenance = proposal.get("provenance")
                if (
                    not isinstance(proposal.get("text"), str)
                    or not proposal["text"].strip()
                    or not isinstance(provenance, dict)
                    or set(provenance) != {"provider", "artifact_sha256"}
                    or not isinstance(provenance.get("provider"), str)
                    or not provenance["provider"].strip()
                ):
                    raise OperatorBaselineCompileError("machine proposal is invalid")
                _require_sha256(provenance.get("artifact_sha256"), label="machine proposal artifact")
                diagnostic["proposal"] = {
                    "text": proposal["text"].strip(),
                    "provenance": dict(provenance),
                }
            freeze_count += 1
        elif disposition == "MACHINE_PROPOSAL":
            raise OperatorBaselineCompileError(
                "standalone machine proposal cannot satisfy operator coverage"
            )
        else:
            raise OperatorBaselineCompileError("decision ledger disposition is invalid")
        diagnostic_rows.append(diagnostic)
    if seen != expected_ordinals:
        raise OperatorBaselineCompileError("decision ledger cue coverage is incomplete")
    if exact_count < 1:
        raise OperatorBaselineCompileError("decision ledger contains no operator exact text")
    normalized = {
        "schema_version": DECISION_LEDGER_SCHEMA,
        "candidate_id": candidate_id,
        "report_scope": "EXHAUSTIVE",
        "pipeline_srt_sha256": source_sha256,
        "operator_authority": operator_authority,
        "cue_decisions": rows,
    }
    return normalized, diagnostic_rows, exact_count, freeze_count


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
    decision_ledger: object,
    decision_ledger_root: Path | None = None,
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
    if not source_cues or not reviewed_cues:
        raise OperatorBaselineCompileError("reviewed SRT must contain a cue")
    for ordinal, reviewed in enumerate(reviewed_cues, start=1):
        text = str(reviewed.text).strip()
        if not text:
            raise OperatorBaselineCompileError(f"reviewed SRT cue {ordinal} is empty")
        if _SPEAKER_PREFIX_RX.match(text) or _AB_MARKER_RX.search(text):
            raise OperatorBaselineCompileError(f"reviewed SRT cue {ordinal} carries speaker annotation")
    source_sha = _sha256(source_srt)
    is_v3 = isinstance(decision_ledger, dict) and decision_ledger.get("schema_version") == DECISION_LEDGER_V3_SCHEMA
    if is_v3:
        normalized_ledger, diagnostic_rows, exact_count, freeze_count, drop_count = _validate_v3_decision_ledger(
            ledger=decision_ledger, candidate_id=candidate_id, source_sha256=source_sha,
            source_cues=source_cues, reviewed_cues=reviewed_cues,
        )
    else:
        if len(source_cues) != len(reviewed_cues):
            raise OperatorBaselineCompileError("reviewed SRT cue count drift")
        for ordinal, (source, reviewed) in enumerate(zip(source_cues, reviewed_cues, strict=True), start=1):
            if str(source.index) != str(reviewed.index) or (source.start_ms, source.end_ms) != (reviewed.start_ms, reviewed.end_ms):
                raise OperatorBaselineCompileError(f"reviewed SRT cue {ordinal} timing drift")
        normalized_ledger, diagnostic_rows, exact_count, freeze_count = _validate_decision_ledger(
            ledger=decision_ledger, candidate_id=candidate_id, source_sha256=source_sha,
            source_cues=source_cues, reviewed_cues=reviewed_cues,
            decision_ledger_root=decision_ledger_root,
        )
        drop_count = 0

    rendered: list[str] = []
    for ordinal, reviewed in enumerate(reviewed_cues, start=1):
        if str(reviewed.index) != str(ordinal):
            raise OperatorBaselineCompileError("reviewed SRT cue index drift")
        text = str(reviewed.text).strip()
        rendered.append(f"{ordinal}\n{_timestamp(reviewed.start_ms)} --> {_timestamp(reviewed.end_ms)}\n{text}")

    if absolute_source_start_ms + reviewed_cues[-1].end_ms > absolute_source_end_ms:
        raise OperatorBaselineCompileError(
            "reviewed SRT extends beyond the bound source interval"
        )
    changed = [
        {
            "cue": row["cue"], "source_index": row["source_index"],
            "start_ms": row["start_ms"], "end_ms": row["end_ms"],
            "absolute_source_start_ms": absolute_source_start_ms + int(row["start_ms"]),
            "absolute_source_end_ms": absolute_source_start_ms + int(row["end_ms"]),
            "before": row["pipeline_text"], "after": row["release_truth_text"],
            "disposition": row["disposition"],
        }
        for row in diagnostic_rows
        if row["disposition"] == "OPERATOR_DROP" or row["pipeline_text"] != row["release_truth_text"]
    ]

    baseline_text = "\n\n".join(rendered) + "\n"
    baseline_sha = _sha256_bytes(baseline_text.encode("utf-8"))
    reviewed_sha = _sha256(reviewed_srt)
    decision_ledger_text = json.dumps(normalized_ledger, ensure_ascii=False, indent=2) + "\n"
    decision_ledger_sha = _sha256_bytes(decision_ledger_text.encode("utf-8"))
    diagnostic_diff = {
        "schema_version": DIAGNOSTIC_DIFF_V2_SCHEMA if is_v3 else DIAGNOSTIC_DIFF_SCHEMA,
        "candidate_id": candidate_id,
        "pipeline_srt_sha256": source_sha,
        "release_truth_srt_sha256": baseline_sha,
        "decision_ledger_sha256": decision_ledger_sha,
        "cue_count": len(reviewed_cues),
        **({"source_cue_count": len(source_cues), "release_cue_count": len(reviewed_cues), "operator_drop_cue_count": drop_count} if is_v3 else {}),
        "changed_cue_count": len(changed),
        "rows": diagnostic_rows,
    }
    diagnostic_diff_text = json.dumps(diagnostic_diff, ensure_ascii=False, indent=2) + "\n"
    diagnostic_diff_sha = _sha256_bytes(diagnostic_diff_text.encode("utf-8"))
    operator_authority = dict(normalized_ledger["operator_authority"])
    truth_lanes = {
        "schema_version": TRUTH_LANES_V2_SCHEMA if is_v3 else TRUTH_LANES_SCHEMA,
        "release_truth": {"srt_sha256": baseline_sha},
        "pipeline_diagnostic": {"sha256": source_sha},
        "decision_ledger": {"sha256": decision_ledger_sha},
        "diff_receipt": {"sha256": diagnostic_diff_sha},
    }
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
        "operator_text_full_ownership": _operator_text_ownership_pin(
            baseline_sha=baseline_sha,
            pipeline_sha=source_sha,
            decision_ledger_sha=decision_ledger_sha,
            diagnostic_diff_sha=diagnostic_diff_sha,
            authority=operator_authority,
            cue_count=len(reviewed_cues),
            changed_cue_count=len(changed),
            exact_count=exact_count,
            freeze_count=freeze_count,
            drop_count=drop_count,
            source_cue_count=len(source_cues) if is_v3 else None,
        ),
        "operator_truth_lanes": truth_lanes,
    }
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "candidate_id": candidate_id,
        "authority": authority,
        "operator_authority": operator_authority,
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
        **({"source_cue_count": len(source_cues), "operator_drop_cue_count": drop_count} if is_v3 else {}),
        "changed_cue_count": len(changed),
        "changed_cues": changed,
        "truth_lanes": truth_lanes,
        "diagnostic_diff": diagnostic_diff,
        "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
    }
    return {
        "baseline_srt": baseline_text,
        "pipeline_diagnostic_srt": source_srt.read_bytes().decode("utf-8"),
        "decision_ledger": decision_ledger_text,
        "diagnostic_diff": diagnostic_diff_text,
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
    parser.add_argument("--decision-ledger", required=True, type=Path)
    parser.add_argument("--baseline-srt-out", required=True, type=Path)
    parser.add_argument("--baseline-manifest-out", required=True, type=Path)
    parser.add_argument("--receipt-out", required=True, type=Path)
    parser.add_argument("--pipeline-diagnostic-srt-out", required=True, type=Path)
    parser.add_argument("--decision-ledger-out", required=True, type=Path)
    parser.add_argument("--diagnostic-diff-out", required=True, type=Path)
    args = parser.parse_args()

    try:
        decision_ledger = json.loads(args.decision_ledger.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorBaselineCompileError("decision ledger is invalid JSON") from exc
    result = compile_operator_baseline(
        source_srt=args.source_srt,
        reviewed_srt=args.reviewed_srt,
        candidate_id=args.candidate_id,
        authority=args.authority,
        source_recording_basename=args.source_recording_basename,
        source_recording_sha256=args.source_recording_sha256,
        absolute_source_start_ms=args.absolute_source_start_ms,
        absolute_source_end_ms=args.absolute_source_end_ms,
        decision_ledger=decision_ledger,
        decision_ledger_root=args.decision_ledger.parent,
    )
    atomic_write_text(args.baseline_srt_out, str(result["baseline_srt"]))
    manifest = dict(result["baseline_manifest"])
    manifest["path"] = args.baseline_srt_out.name
    lanes = dict(manifest["operator_truth_lanes"])
    lanes["pipeline_diagnostic"] = {
        **dict(lanes["pipeline_diagnostic"]),
        "path": args.pipeline_diagnostic_srt_out.name,
    }
    lanes["decision_ledger"] = {
        **dict(lanes["decision_ledger"]),
        "path": args.decision_ledger_out.name,
    }
    lanes["diff_receipt"] = {
        **dict(lanes["diff_receipt"]),
        "path": args.diagnostic_diff_out.name,
    }
    manifest["operator_truth_lanes"] = lanes
    atomic_write_text(
        args.baseline_manifest_out,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    atomic_write_text(
        args.receipt_out,
        json.dumps(result["receipt"], ensure_ascii=False, indent=2) + "\n",
    )
    atomic_write_text(args.pipeline_diagnostic_srt_out, str(result["pipeline_diagnostic_srt"]))
    atomic_write_text(args.decision_ledger_out, str(result["decision_ledger"]))
    atomic_write_text(args.diagnostic_diff_out, str(result["diagnostic_diff"]))
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
