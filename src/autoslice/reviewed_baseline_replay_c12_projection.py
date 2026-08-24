"""Candidate-locked C12 baseline-grid to record-grid replay adapter.

The ordinary redelivery finalizer treats one source piece as the baseline
window.  C12 is deliberately different: its reviewed interval starts at the
old record boundary and retains a reviewed tail beyond the delivered record.
This module is private-replay-only and accepts that exceptional geometry only
after replaying the complete sealed C12 truth graph.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.redelivery_full_window_replay import (
    FullWindowReplayError,
    project_full_window_audit_to_final_delivery,
    replay_full_window_text_and_crop,
)
from src.autoslice.redelivery_source_binding import V2RedeliverySourceBinding


C12_CANDIDATE_ID = "auto_130012_435_574"
_SCHEMA = "reviewed-baseline-replay-c12-final-delivery-projection.v1"
_STAGE_SCHEMA = "reviewed-baseline-replay-stage.v1"
_RECEIPT_SCHEMA = "reviewed-baseline-full-release-delivery-projection.v2"
_COORDINATE_SCHEMA = "reviewed-baseline-grid-record-grid.v1"
_SOURCE_TRUTH_SUPERSESSION_SCHEMA = (
    "source-subtitle-truth-reapplication-supersession.v1"
)
_OWNERSHIP_SUPERSESSION_SCHEMA = (
    "reviewed-baseline-replay-c12-operator-text-supersession.v1"
)
_DEFERRED_SUPERSESSION_SCHEMA = "deferred-exact-replay-reverification.v1"
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PADDED = re.compile(r"padded_(\d+)_(\d+)\.mp4\Z")


class C12FinalDeliveryProjectionError(RuntimeError):
    """The C12-only private replay projection is not exactly sealed."""


@dataclass(frozen=True, slots=True)
class C12FinalDeliveryProjection:
    """All immutable bindings needed to replay C12's exceptional grid."""

    schema_version: str
    stage: Path
    stage_sha256: str
    record_sha256: str
    record_boundary: Mapping[str, object]
    expected_video_sha256: str
    padded_start_ms: int
    padded_end_ms: int
    final_start_ms: int
    final_end_ms: int
    baseline_start_ms: int
    baseline_end_ms: int
    stage_srt_sha256: str
    stage_audit_sha256: str
    receipt_sha256: str


@dataclass(frozen=True, slots=True)
class _TruthAssets:
    diagnostic_path: Path
    diagnostic_text: str
    baseline_path: Path
    baseline_text: str
    diagnostic_sha256: str
    baseline_sha256: str
    ledger_sha256: str
    diff_sha256: str


def _fail(code: str) -> None:
    raise C12FinalDeliveryProjectionError(f"REPLAY_C12_{code}")


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                   separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _canonical_digest(value: object) -> str:
    """Match the coordinate receipt's JSON-only hash, without a line ending."""

    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha(payload)


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest(value: object, *, code: str) -> str:
    raw = str(value or "")
    if not raw.startswith("sha256:"):
        raw = "sha256:" + raw
    if _SHA.fullmatch(raw) is None:
        _fail(code)
    return raw


def _regular_bytes(path: Path, *, code: str) -> bytes:
    """Read one stable ordinary file without following a final symlink."""

    try:
        before = os.lstat(path)
    except OSError:
        _fail(code)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        _fail(code)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        _fail(code)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        after = os.lstat(path)
    except OSError:
        _fail(code)
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or (before.st_dev, before.st_ino, before.st_mode, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns)
    ):
        _fail(code)
    return b"".join(chunks)


def _json(path: Path, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(_regular_bytes(path, code=code).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(code)
    if not isinstance(value, dict):
        _fail(code)
    return value


def _path(value: object, *, code: str) -> Path:
    if not isinstance(value, str) or not value:
        _fail(code)
    return Path(value)


def _int(value: object, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(code)
    return value


def _strict_cues(text: str, *, code: str):
    """Use the production ``src`` SRT parser, then close its permissive gaps.

    ``parse_srt_cues`` is deliberately useful for loose ASR input.  A sealed
    C12 receipt needs the stricter properties historically supplied by the
    script parser: nonempty, contiguous numeric indexes and positive cue
    intervals.  Keeping that narrow validation here avoids a finalizer import
    from a CLI script layer.
    """

    try:
        cues = parse_srt_cues(text)
    except ValueError:
        _fail(code)
    if (
        not cues
        or [cue.index for cue in cues] != [str(index) for index in range(1, len(cues) + 1)]
        or any(
            cue.end_ms <= cue.start_ms or not cue.text.strip()
            for cue in cues
        )
    ):
        _fail(code)
    return cues


def _truth_assets(config: Mapping[str, object]) -> _TruthAssets:
    """Revalidate all C12 text authority, including its 56/3 freeze ledger."""

    if (
        config.get("schema_version") != "subtitle-redelivery-baseline.v2"
        or config.get("exact_interval_replay") is not True
        or config.get("source_recording_basename")
        != "22966160_20260815-13-00-12.mp4"
        or config.get("source_sha256")
        != "297cfb07b04b006ac76307ad0369af7160a62f64d04d315ddcfa7146d2625077"
        or config.get("absolute_source_start_ms") != 434_920
        or config.get("absolute_source_end_ms") != 603_840
    ):
        _fail("AUTHORITY_CONFIG_INVALID")
    baseline_path = _path(config.get("path"), code="BASELINE_PATH_INVALID")
    baseline_raw = _regular_bytes(baseline_path, code="BASELINE_PATH_INVALID")
    baseline_sha = _digest(config.get("sha256"), code="BASELINE_SHA_INVALID")
    if _sha(baseline_raw) != baseline_sha:
        _fail("BASELINE_SHA_DRIFT")
    try:
        baseline_text = baseline_raw.decode("utf-8")
        baseline_cues = _strict_cues(
            baseline_text, code="BASELINE_SRT_INVALID"
        )
    except (UnicodeDecodeError, ValueError):
        _fail("BASELINE_SRT_INVALID")

    ownership = config.get("operator_text_full_ownership")
    lanes = config.get("operator_truth_lanes")
    if not isinstance(ownership, Mapping) or not isinstance(lanes, Mapping) or (
        ownership.get("schema_version")
        != "operator-reviewed-text-full-ownership-pin.v3"
        or ownership.get("baseline_sha256") != baseline_sha.removeprefix("sha256:")
        or ownership.get("source_cue_count") != 59
        or ownership.get("release_cue_count") != 59
        or ownership.get("changed_cue_count") != 3
        or ownership.get("operator_exact_text_cue_count") != 3
        or ownership.get("operator_unchanged_freeze_cue_count") != 56
        or ownership.get("operator_drop_cue_count") != 0
        or ownership.get("speaker_authority") != "NOT_CLAIMED_TEXT_ONLY"
    ):
        _fail("OWNERSHIP_PIN_INVALID")
    if lanes.get("schema_version") != "operator-reviewed-subtitle-truth-lanes.v2":
        _fail("TRUTH_LANES_INVALID")
    descriptors: dict[str, Mapping[str, object]] = {}
    for name in ("pipeline_diagnostic", "decision_ledger", "diff_receipt"):
        value = lanes.get(name)
        if not isinstance(value, Mapping):
            _fail("TRUTH_LANES_INVALID")
        descriptors[name] = value

    diagnostic_path = _path(
        descriptors["pipeline_diagnostic"].get("path"),
        code="PIPELINE_DIAGNOSTIC_PATH_INVALID",
    )
    ledger_path = _path(
        descriptors["decision_ledger"].get("path"), code="LEDGER_PATH_INVALID"
    )
    diff_path = _path(
        descriptors["diff_receipt"].get("path"), code="DIFF_PATH_INVALID"
    )
    diagnostic_raw = _regular_bytes(diagnostic_path, code="PIPELINE_DIAGNOSTIC_PATH_INVALID")
    ledger_raw = _regular_bytes(ledger_path, code="LEDGER_PATH_INVALID")
    diff_raw = _regular_bytes(diff_path, code="DIFF_PATH_INVALID")
    diagnostic_sha = _digest(
        descriptors["pipeline_diagnostic"].get("sha256"), code="PIPELINE_DIAGNOSTIC_SHA_INVALID"
    )
    ledger_sha = _digest(descriptors["decision_ledger"].get("sha256"), code="LEDGER_SHA_INVALID")
    diff_sha = _digest(descriptors["diff_receipt"].get("sha256"), code="DIFF_SHA_INVALID")
    if (
        _sha(diagnostic_raw) != diagnostic_sha
        or _sha(ledger_raw) != ledger_sha
        or _sha(diff_raw) != diff_sha
        or ownership.get("pipeline_srt_sha256") != diagnostic_sha.removeprefix("sha256:")
        or ownership.get("decision_ledger_sha256") != ledger_sha.removeprefix("sha256:")
        or ownership.get("diagnostic_diff_sha256") != diff_sha.removeprefix("sha256:")
    ):
        _fail("TRUTH_LANE_HASH_DRIFT")
    try:
        diagnostic_text = diagnostic_raw.decode("utf-8")
        diagnostic_cues = _strict_cues(
            diagnostic_text, code="TRUTH_LANE_CONTENT_INVALID"
        )
        ledger = json.loads(ledger_raw.decode("utf-8"))
        diff = json.loads(diff_raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        _fail("TRUTH_LANE_CONTENT_INVALID")
    if not isinstance(ledger, Mapping) or not isinstance(diff, Mapping):
        _fail("TRUTH_LANE_CONTENT_INVALID")
    rows = ledger.get("cue_decisions")
    diff_rows = diff.get("rows")
    authority = ledger.get("operator_authority")
    if (
        ledger.get("schema_version") != "operator-reviewed-subtitle-decisions.v3"
        or ledger.get("candidate_id") != C12_CANDIDATE_ID
        or ledger.get("report_scope") != "EXHAUSTIVE"
        or ledger.get("pipeline_srt_sha256") != diagnostic_sha.removeprefix("sha256:")
        or not isinstance(authority, Mapping)
        or authority.get("kind") != "IVAN_OPERATOR"
        or not isinstance(authority.get("evidence_ref"), str)
        or not authority["evidence_ref"].strip()
        or diff.get("schema_version") != "operator-reviewed-subtitle-truth-diff.v2"
        or diff.get("candidate_id") != C12_CANDIDATE_ID
        or diff.get("pipeline_srt_sha256") != diagnostic_sha.removeprefix("sha256:")
        or diff.get("release_truth_srt_sha256") != baseline_sha.removeprefix("sha256:")
        or diff.get("decision_ledger_sha256") != ledger_sha.removeprefix("sha256:")
        or not isinstance(rows, list)
        or not isinstance(diff_rows, list)
        or len(diagnostic_cues) != 59
        or len(baseline_cues) != 59
        or len(rows) != 59
        or len(diff_rows) != 59
    ):
        _fail("TRUTH_GRAPH_INVALID")

    exact, frozen = 0, 0
    for ordinal, (diagnostic, release, decision, diff_row) in enumerate(
        zip(diagnostic_cues, baseline_cues, rows, diff_rows, strict=True), start=1
    ):
        if not isinstance(decision, Mapping) or not isinstance(diff_row, Mapping):
            _fail("TRUTH_GRAPH_INVALID")
        if (
            diagnostic.start_ms != release.start_ms
            or diagnostic.end_ms != release.end_ms
            or decision.get("cue") != ordinal
            or decision.get("disposition") != diff_row.get("disposition")
        ):
            _fail("TRUTH_GRAPH_INVALID")
        expected: dict[str, object] = {
            "cue": ordinal,
            "source_index": str(ordinal),
            "start_ms": diagnostic.start_ms,
            "end_ms": diagnostic.end_ms,
            "pipeline_text": diagnostic.text.strip(),
            "disposition": decision.get("disposition"),
            "release_cue_index": ordinal,
            "release_truth_text": release.text.strip(),
        }
        if decision.get("disposition") == "OPERATOR_UNCHANGED_FREEZE":
            if set(decision) != {"cue", "disposition"} or release.text.strip() != diagnostic.text.strip():
                _fail("TRUTH_GRAPH_INVALID")
            frozen += 1
        elif decision.get("disposition") == "OPERATOR_EXACT_TEXT":
            if (
                set(decision) != {"cue", "disposition", "release_text", "decision_authority"}
                or decision.get("decision_authority") != "LEDGER_OPERATOR_AUTHORITY"
                or decision.get("release_text") != release.text.strip()
            ):
                _fail("TRUTH_GRAPH_INVALID")
            expected["decision_authority"] = dict(authority)
            exact += 1
        else:
            _fail("TRUTH_GRAPH_INVALID")
        if dict(diff_row) != expected:
            _fail("TRUTH_GRAPH_INVALID")
    if (exact, frozen) != (3, 56):
        _fail("TRUTH_GRAPH_INVALID")
    return _TruthAssets(
        diagnostic_path=diagnostic_path,
        diagnostic_text=diagnostic_text,
        baseline_path=baseline_path,
        baseline_text=baseline_text,
        diagnostic_sha256=diagnostic_sha,
        baseline_sha256=baseline_sha,
        ledger_sha256=ledger_sha,
        diff_sha256=diff_sha,
    )


def _padded_interval(path: Path) -> tuple[int, int]:
    match = _PADDED.fullmatch(path.name)
    if match is None:
        _fail("PADDED_NAME_INVALID")
    start, end = (int(value) for value in match.groups())
    if start >= end:
        _fail("PADDED_NAME_INVALID")
    return start, end


def _validate_geometry(
    *, padded_start_ms: int, padded_end_ms: int, final_start_ms: int,
    final_end_ms: int, baseline_start_ms: int, baseline_end_ms: int,
) -> None:
    if not (
        padded_start_ms < padded_end_ms
        and 0 <= final_start_ms < final_end_ms <= padded_end_ms - padded_start_ms
        and padded_start_ms <= baseline_start_ms < baseline_end_ms <= padded_end_ms
        and baseline_start_ms == padded_start_ms + final_start_ms
        and padded_start_ms + final_end_ms <= baseline_end_ms
    ):
        _fail("COORDINATE_DRIFT")


def _stage_document(
    *, projection: C12FinalDeliveryProjection, truth: _TruthAssets,
) -> tuple[bytes, dict[str, Any], bytes]:
    stage = projection.stage
    try:
        metadata = os.lstat(stage)
    except OSError:
        _fail("STAGE_UNAVAILABLE")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail("STAGE_UNSAFE")
    stage_path = stage / "stage.json"
    document = _json(stage_path, code="STAGE_DOCUMENT_INVALID")
    unsigned = dict(document)
    declared = _digest(unsigned.pop("stage_sha256", None), code="STAGE_DOCUMENT_INVALID")
    if declared != projection.stage_sha256 or _sha(_canonical(unsigned)) != declared:
        _fail("STAGE_DOCUMENT_DRIFT")
    artifacts = document.get("artifacts")
    delivery = document.get("delivery_projection_receipt")
    if not isinstance(artifacts, Mapping) or not isinstance(delivery, Mapping) or (
        document.get("schema_version") != _STAGE_SCHEMA
        or document.get("candidate_id") != C12_CANDIDATE_ID
        or _digest(document.get("record_sha256"), code="STAGE_DOCUMENT_INVALID")
        != projection.record_sha256
        or _digest(document.get("expected_video_sha256"), code="STAGE_DOCUMENT_INVALID")
        != projection.expected_video_sha256
        or _digest(document.get("baseline_sha256"), code="STAGE_DOCUMENT_INVALID")
        != truth.baseline_sha256
        or _digest(artifacts.get("subtitle"), code="STAGE_DOCUMENT_INVALID")
        != projection.stage_srt_sha256
        or _digest(artifacts.get("baseline_audit"), code="STAGE_DOCUMENT_INVALID")
        != projection.stage_audit_sha256
        or delivery.get("path") != str(stage / "full-release-delivery-projection.json")
        or _digest(delivery.get("sha256"), code="STAGE_DOCUMENT_INVALID")
        != projection.receipt_sha256
    ):
        _fail("STAGE_DOCUMENT_DRIFT")
    reviewed = _regular_bytes(stage / "reviewed.srt", code="STAGE_SRT_INVALID")
    audit = _regular_bytes(stage / "redelivery-baseline.json", code="STAGE_AUDIT_INVALID")
    receipt = _regular_bytes(
        stage / "full-release-delivery-projection.json", code="RECEIPT_INVALID"
    )
    if (
        _sha(reviewed) != projection.stage_srt_sha256
        or _sha(audit) != projection.stage_audit_sha256
        or _sha(receipt) != projection.receipt_sha256
    ):
        _fail("STAGE_ARTIFACT_DRIFT")
    return reviewed, document, receipt


def _validate_receipt(
    *, projection: C12FinalDeliveryProjection, truth: _TruthAssets,
    reviewed: bytes,
) -> None:
    """Validate C12's v2 two-grid receipt without a CLI-script dependency."""

    receipt_path = projection.stage / "full-release-delivery-projection.json"
    try:
        receipt_raw = _regular_bytes(receipt_path, code="RECEIPT_INVALID")
        receipt = json.loads(receipt_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("DELIVERY_PROJECTION_RECEIPT_INVALID")
    if not isinstance(receipt, Mapping) or _sha(receipt_raw) != projection.receipt_sha256:
        _fail("DELIVERY_PROJECTION_RECEIPT_INVALID")
    old_cues = _strict_cues(
        truth.diagnostic_text, code="DELIVERY_PROJECTION_RECEIPT_INVALID"
    )
    release_cues = _strict_cues(
        truth.baseline_text, code="DELIVERY_PROJECTION_RECEIPT_INVALID"
    )
    try:
        delivery_text = reviewed.decode("utf-8")
    except UnicodeDecodeError:
        _fail("DELIVERY_PROJECTION_RECEIPT_INVALID")
    delivery_cues = _strict_cues(
        delivery_text, code="DELIVERY_PROJECTION_RECEIPT_INVALID"
    )
    expected_keys = {
        "schema_version",
        "candidate_id",
        "record_sha256",
        "record_boundary_sha256",
        "pipeline_diagnostic_sha256",
        "full_release_srt_sha256",
        "operator_ledger_sha256",
        "operator_truth_diff_sha256",
        "staged_srt_sha256",
        "staged_media_sha256",
        "old_diagnostic_cue_count",
        "full_release_cue_count",
        "final_delivery_cue_count",
        "rows",
        "coordinate_contract",
    }
    if (
        set(receipt) != expected_keys
        or receipt.get("schema_version") != _RECEIPT_SCHEMA
        or receipt.get("candidate_id") != C12_CANDIDATE_ID
        or _digest(
            receipt.get("record_sha256"),
            code="DELIVERY_PROJECTION_RECEIPT_INVALID",
        ) != projection.record_sha256
        or receipt.get("record_boundary_sha256")
        != _canonical_digest(dict(projection.record_boundary))
        or _digest(
            receipt.get("pipeline_diagnostic_sha256"),
            code="DELIVERY_PROJECTION_RECEIPT_INVALID",
        ) != truth.diagnostic_sha256
        or _digest(
            receipt.get("full_release_srt_sha256"),
            code="DELIVERY_PROJECTION_RECEIPT_INVALID",
        ) != truth.baseline_sha256
        or _digest(
            receipt.get("operator_ledger_sha256"),
            code="DELIVERY_PROJECTION_RECEIPT_INVALID",
        ) != truth.ledger_sha256
        or _digest(
            receipt.get("operator_truth_diff_sha256"),
            code="DELIVERY_PROJECTION_RECEIPT_INVALID",
        ) != truth.diff_sha256
        or _digest(
            receipt.get("staged_srt_sha256"),
            code="DELIVERY_PROJECTION_RECEIPT_INVALID",
        ) != _sha(reviewed)
        or _digest(
            receipt.get("staged_media_sha256"),
            code="DELIVERY_PROJECTION_RECEIPT_INVALID",
        ) != projection.expected_video_sha256
        or receipt.get("old_diagnostic_cue_count") != len(old_cues)
        or receipt.get("full_release_cue_count") != len(release_cues)
        or receipt.get("final_delivery_cue_count") != len(delivery_cues)
    ):
        _fail("DELIVERY_PROJECTION_RECEIPT_INVALID")
    contract = receipt.get("coordinate_contract")
    delivery_duration_ms = projection.final_end_ms - projection.final_start_ms
    expected_contract = {
        "schema_version": _COORDINATE_SCHEMA,
        "baseline_source_interval": {
            "start_ms": projection.baseline_start_ms,
            "end_ms": projection.baseline_end_ms,
        },
        "baseline_delivery_crop": {
            "start_ms": 0,
            "end_ms": delivery_duration_ms,
        },
        "record_padded_source_interval": {
            "start_ms": projection.padded_start_ms,
            "end_ms": projection.padded_end_ms,
        },
        "record_final_delivery_boundary": {
            "start_ms": projection.final_start_ms,
            "end_ms": projection.final_end_ms,
        },
        "baseline_to_record_padded_offset_ms": (
            projection.baseline_start_ms - projection.padded_start_ms
        ),
    }
    if not isinstance(contract, Mapping) or dict(contract) != expected_contract:
        _fail("DELIVERY_PROJECTION_COORDINATE_INVALID")
    receipt_rows = receipt.get("rows")
    if not isinstance(receipt_rows, list) or len(receipt_rows) != len(old_cues):
        _fail("DELIVERY_PROJECTION_MAP_INVALID")
    expected_rows: list[dict[str, object]] = []
    delivery_cursor = 0
    for old_index, (old, release) in enumerate(
        zip(old_cues, release_cues, strict=True), start=1
    ):
        if (old.start_ms, old.end_ms) != (release.start_ms, release.end_ms):
            _fail("DELIVERY_PROJECTION_MAP_INVALID")
        if release.end_ms <= 0 or release.start_ms >= delivery_duration_ms:
            expected_rows.append(
                {
                    "old_source_index": old_index,
                    "release_cue_index": old_index,
                    "delivery_cue_index": None,
                    "disposition": "OUTSIDE_FINAL_DELIVERY",
                }
            )
            continue
        if release.start_ms < 0 or release.end_ms > delivery_duration_ms:
            _fail("DELIVERY_PROJECTION_STRADDLER")
        if delivery_cursor >= len(delivery_cues):
            _fail("DELIVERY_PROJECTION_DELIVERY_GRID_DRIFT")
        delivery = delivery_cues[delivery_cursor]
        delivery_index = delivery_cursor + 1
        if (
            delivery.index != str(delivery_index)
            or (delivery.start_ms, delivery.end_ms)
            != (release.start_ms, release.end_ms)
            or delivery.text != release.text
        ):
            _fail("DELIVERY_PROJECTION_DELIVERY_GRID_DRIFT")
        expected_rows.append(
            {
                "old_source_index": old_index,
                "release_cue_index": old_index,
                "delivery_cue_index": delivery_index,
                "disposition": "RETAINED_FINAL_DELIVERY",
                "release_start_ms": release.start_ms,
                "release_end_ms": release.end_ms,
                "delivery_start_ms": delivery.start_ms,
                "delivery_end_ms": delivery.end_ms,
            }
        )
        delivery_cursor += 1
    if delivery_cursor != len(delivery_cues) or receipt_rows != expected_rows:
        _fail("DELIVERY_PROJECTION_MAP_INVALID")


def _validate_projection(
    *, projection: C12FinalDeliveryProjection, config: Mapping[str, object],
    cid: str, final_start_ms: int, final_end_ms: int,
    binding: V2RedeliverySourceBinding | None,
) -> _TruthAssets:
    if (
        not isinstance(projection, C12FinalDeliveryProjection)
        or projection.schema_version != _SCHEMA
        or cid != C12_CANDIDATE_ID
    ):
        _fail("PROJECTION_SCOPE_INVALID")
    if final_start_ms != projection.final_start_ms or final_end_ms != projection.final_end_ms:
        _fail("RECORD_BOUNDARY_DRIFT")
    if not isinstance(projection.record_boundary, Mapping):
        _fail("RECORD_BOUNDARY_DRIFT")
    if (
        projection.record_boundary.get("final_start_ms") != final_start_ms
        or projection.record_boundary.get("final_end_ms") != final_end_ms
    ):
        _fail("RECORD_BOUNDARY_DRIFT")
    truth = _truth_assets(config)
    _validate_geometry(
        padded_start_ms=projection.padded_start_ms,
        padded_end_ms=projection.padded_end_ms,
        final_start_ms=final_start_ms,
        final_end_ms=final_end_ms,
        baseline_start_ms=projection.baseline_start_ms,
        baseline_end_ms=projection.baseline_end_ms,
    )
    if (
        projection.baseline_start_ms != config.get("absolute_source_start_ms")
        or projection.baseline_end_ms != config.get("absolute_source_end_ms")
        or binding is None
        or binding.content_absolute_start_ms != projection.padded_start_ms
        or binding.content_absolute_end_ms != projection.padded_end_ms
        or binding.padded_content_start_ms != 0
        or binding.padded_content_end_ms != projection.padded_end_ms - projection.padded_start_ms
        or binding.absolute_source_start_ms != projection.baseline_start_ms
        or binding.absolute_source_end_ms != projection.padded_start_ms + final_end_ms
    ):
        _fail("GRID_BINDING_DRIFT")
    reviewed, _document, receipt_raw = _stage_document(
        projection=projection, truth=truth
    )
    try:
        receipt = json.loads(receipt_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("RECEIPT_INVALID")
    contract = receipt.get("coordinate_contract") if isinstance(receipt, Mapping) else None
    if not isinstance(contract, Mapping) or (
        receipt.get("schema_version") != _RECEIPT_SCHEMA
        or contract.get("schema_version") != _COORDINATE_SCHEMA
        or contract.get("baseline_source_interval")
        != {"start_ms": projection.baseline_start_ms, "end_ms": projection.baseline_end_ms}
        or contract.get("baseline_delivery_crop")
        != {"start_ms": 0, "end_ms": final_end_ms - final_start_ms}
        or contract.get("record_padded_source_interval")
        != {"start_ms": projection.padded_start_ms, "end_ms": projection.padded_end_ms}
        or contract.get("record_final_delivery_boundary")
        != {"start_ms": final_start_ms, "end_ms": final_end_ms}
        or contract.get("baseline_to_record_padded_offset_ms")
        != projection.baseline_start_ms - projection.padded_start_ms
    ):
        _fail("COORDINATE_RECEIPT_DRIFT")
    _validate_receipt(projection=projection, truth=truth, reviewed=reviewed)
    return truth


def _ownership_supersession(
    *, projection: C12FinalDeliveryProjection, truth: _TruthAssets,
) -> dict[str, object]:
    """Return the only audit marker that can suppress C12 source-truth replay."""

    return {
        "schema_version": _OWNERSHIP_SUPERSESSION_SCHEMA,
        "candidate_id": C12_CANDIDATE_ID,
        "record_sha256": projection.record_sha256,
        "delivery_projection_receipt_sha256": projection.receipt_sha256,
        "baseline_sha256": truth.baseline_sha256,
        "pipeline_diagnostic_sha256": truth.diagnostic_sha256,
        "decision_ledger_sha256": truth.ledger_sha256,
        "diagnostic_diff_sha256": truth.diff_sha256,
        "source_cue_count": 59,
        "release_cue_count": 59,
        "operator_exact_text_cue_count": 3,
        "operator_unchanged_freeze_cue_count": 56,
        "operator_drop_cue_count": 0,
        "final_delivery_srt_sha256": projection.stage_srt_sha256,
        "source_truth_reapplication_allowed": False,
    }


def _validate_c12_final_delivery_bytes(
    *, projection: C12FinalDeliveryProjection, config: Mapping[str, object],
    cid: str, output_text: str,
) -> tuple[_TruthAssets, dict[str, object]]:
    """Re-open the sealed stage and require byte identity with final delivery."""

    if (
        not isinstance(projection, C12FinalDeliveryProjection)
        or projection.schema_version != _SCHEMA
        or cid != C12_CANDIDATE_ID
    ):
        _fail("PROJECTION_SCOPE_INVALID")
    truth = _truth_assets(config)
    _validate_geometry(
        padded_start_ms=projection.padded_start_ms,
        padded_end_ms=projection.padded_end_ms,
        final_start_ms=projection.final_start_ms,
        final_end_ms=projection.final_end_ms,
        baseline_start_ms=projection.baseline_start_ms,
        baseline_end_ms=projection.baseline_end_ms,
    )
    reviewed, _document, _receipt = _stage_document(
        projection=projection, truth=truth
    )
    _validate_receipt(projection=projection, truth=truth, reviewed=reviewed)
    try:
        output = output_text.encode("utf-8")
    except UnicodeEncodeError:
        _fail("FINAL_DELIVERY_TEXT_DRIFT")
    if output != reviewed or _sha(output) != projection.stage_srt_sha256:
        _fail("FINAL_DELIVERY_TEXT_DRIFT")
    return truth, _ownership_supersession(projection=projection, truth=truth)


def c12_source_truth_reapplication_supersession(
    *, projection: C12FinalDeliveryProjection, config: Mapping[str, object],
    cid: str, output_text: str, baseline_audit: Mapping[str, object],
    pre_truth_audit: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Seal C12's full operator ownership over old source-truth replay.

    C12's v3 pin covers all 59 reviewed text cues (56 frozen, three exact
    operator corrections).  It is therefore the candidate-specific final text
    authority once the stage crop is byte-identical.  The normal source-truth
    adapter must not write after that point; this function records that decision
    without carrying forward source text or invoking the adapter.
    """

    truth, marker = _validate_c12_final_delivery_bytes(
        projection=projection, config=config, cid=cid, output_text=output_text
    )
    if not isinstance(baseline_audit, Mapping) or (
        baseline_audit.get("application_strategy")
        != "exact_reviewed_interval_replay"
        or baseline_audit.get("exact_replay_then_final_crop") is not True
        or baseline_audit.get("operator_text_full_ownership_supersession")
        != marker
    ):
        _fail("SOURCE_TRUTH_SUPERSESSION_INVALID")
    if not isinstance(pre_truth_audit, Mapping):
        _fail("SOURCE_TRUTH_AUDIT_INVALID")
    rows: list[Mapping[str, object]] = []
    for key in ("applied", "satisfied", "failures"):
        value = pre_truth_audit.get(key) or []
        if not isinstance(value, list):
            _fail("SOURCE_TRUTH_AUDIT_INVALID")
        if not all(isinstance(row, Mapping) for row in value):
            _fail("SOURCE_TRUTH_AUDIT_INVALID")
        rows.extend(value)
    truth_ids = sorted({str(row.get("truth_id") or "") for row in rows})
    if not rows or not all(truth_ids):
        _fail("SOURCE_TRUTH_AUDIT_INVALID")
    try:
        pre_truth_audit_sha256 = _sha(_canonical(pre_truth_audit))
    except (TypeError, ValueError):
        _fail("SOURCE_TRUTH_AUDIT_INVALID")
    post_truth_audit: dict[str, object] = {
        "schema_version": _SOURCE_TRUTH_SUPERSESSION_SCHEMA,
        "status": "SUPERSEDED_BY_OPERATOR_TEXT_FULL_OWNERSHIP",
        "candidate_id": C12_CANDIDATE_ID,
        "reapplication_attempted": False,
        "source_truth_reapplication_allowed": False,
        "pre_redelivery_audit_sha256": pre_truth_audit_sha256,
        "preexisting_truth_row_count": len(rows),
        "superseded_truth_ids": truth_ids,
        "operator_text_full_ownership": marker,
        "final_delivery_srt_sha256": projection.stage_srt_sha256,
        "applied": [],
        "satisfied": [],
        "failures": [],
    }
    deferred_audit: dict[str, object] = {
        "schema_version": _DEFERRED_SUPERSESSION_SCHEMA,
        "status": "PASS",
        "deferred_strategy": pre_truth_audit.get("deferred_strategy"),
        "required_truth_ids": [],
        "context_only_truth_ids": [],
        "straddling_truth_ids": [],
        "reverified_truth_ids": [],
        "missing_truth_ids": [],
        "superseded_truth_ids": truth_ids,
        "reason_code": (
            "C12_OPERATOR_TEXT_FULL_OWNERSHIP_SUPERSEDES_SOURCE_TRUTH_REAPPLICATION"
        ),
        "operator_text_full_ownership": marker,
    }
    # Keep this local binding explicit even though ``marker`` already carries
    # it: an audit reader can prove which sealed graph supplied the decision.
    post_truth_audit["baseline_sha256"] = truth.baseline_sha256
    return post_truth_audit, deferred_audit


def validate_c12_final_delivery_bytes(
    *, projection: C12FinalDeliveryProjection, config: Mapping[str, object],
    cid: str, output_text: str,
) -> None:
    """Fail closed if any later materializer mutates C12's reviewed delivery."""

    _validate_c12_final_delivery_bytes(
        projection=projection, config=config, cid=cid, output_text=output_text
    )


def build_c12_final_delivery_projection(
    *, candidate_id: str, config: Mapping[str, object], record_sha256: str,
    record_boundary: Mapping[str, object], expected_video_sha256: str,
    padded_path: Path, final_start_ms: int, final_end_ms: int, stage: Path,
) -> C12FinalDeliveryProjection | None:
    """Build the only private adapter allowed for C12's attested tail window."""

    if candidate_id != C12_CANDIDATE_ID:
        return None
    truth = _truth_assets(config)
    padded_start, padded_end = _padded_interval(padded_path)
    baseline_start = _int(config.get("absolute_source_start_ms"), code="AUTHORITY_CONFIG_INVALID")
    baseline_end = _int(config.get("absolute_source_end_ms"), code="AUTHORITY_CONFIG_INVALID")
    _validate_geometry(
        padded_start_ms=padded_start, padded_end_ms=padded_end,
        final_start_ms=final_start_ms, final_end_ms=final_end_ms,
        baseline_start_ms=baseline_start, baseline_end_ms=baseline_end,
    )
    record_sha = _digest(record_sha256, code="RECORD_SHA_INVALID")
    video_sha = _digest(expected_video_sha256, code="VIDEO_SHA_INVALID")
    if not isinstance(record_boundary, Mapping):
        _fail("RECORD_BOUNDARY_DRIFT")
    projection = C12FinalDeliveryProjection(
        schema_version=_SCHEMA,
        stage=stage,
        stage_sha256="sha256:" + "0" * 64,
        record_sha256=record_sha,
        record_boundary=dict(record_boundary),
        expected_video_sha256=video_sha,
        padded_start_ms=padded_start,
        padded_end_ms=padded_end,
        final_start_ms=final_start_ms,
        final_end_ms=final_end_ms,
        baseline_start_ms=baseline_start,
        baseline_end_ms=baseline_end,
        stage_srt_sha256="sha256:" + "0" * 64,
        stage_audit_sha256="sha256:" + "0" * 64,
        receipt_sha256="sha256:" + "0" * 64,
    )
    document = _json(stage / "stage.json", code="STAGE_DOCUMENT_INVALID")
    stage_sha = _digest(document.get("stage_sha256"), code="STAGE_DOCUMENT_INVALID")
    artifacts = document.get("artifacts")
    receipt = document.get("delivery_projection_receipt")
    if not isinstance(artifacts, Mapping) or not isinstance(receipt, Mapping):
        _fail("STAGE_DOCUMENT_INVALID")
    projection = C12FinalDeliveryProjection(
        schema_version=_SCHEMA,
        stage=stage,
        stage_sha256=stage_sha,
        record_sha256=record_sha,
        record_boundary=dict(record_boundary),
        expected_video_sha256=video_sha,
        padded_start_ms=padded_start,
        padded_end_ms=padded_end,
        final_start_ms=final_start_ms,
        final_end_ms=final_end_ms,
        baseline_start_ms=baseline_start,
        baseline_end_ms=baseline_end,
        stage_srt_sha256=_digest(artifacts.get("subtitle"), code="STAGE_DOCUMENT_INVALID"),
        stage_audit_sha256=_digest(artifacts.get("baseline_audit"), code="STAGE_DOCUMENT_INVALID"),
        receipt_sha256=_digest(receipt.get("sha256"), code="STAGE_DOCUMENT_INVALID"),
    )
    # Re-run all finalizer-time checks before exposing the opaque capability.
    # ``binding`` is supplied there; every other proof is already static.
    _stage_document(projection=projection, truth=truth)
    return projection


def replay_c12_final_delivery_projection(
    *, projection: C12FinalDeliveryProjection, config: Mapping[str, object],
    cid: str, final_start_ms: int, final_end_ms: int,
    binding: V2RedeliverySourceBinding | None, subtitle_path: Path,
    write_source_range_srt: Callable[..., None],
) -> tuple[str, dict[str, Any]]:
    """Replay the C12 baseline grid and crop exactly to its old record bounds."""

    truth = _validate_projection(
        projection=projection, config=config, cid=cid,
        final_start_ms=final_start_ms, final_end_ms=final_end_ms, binding=binding,
    )
    crop = subtitle_path.with_name(f".{cid}.c12-reviewed-window.srt")
    try:
        if crop.exists() or crop.is_symlink():
            _fail("TEMP_COLLISION")
        cropped, audit = replay_full_window_text_and_crop(
            text=truth.diagnostic_text,
            config=config,
            spec_parent=truth.baseline_path.parent,
            padded_start_ms=projection.baseline_start_ms,
            padded_end_ms=projection.baseline_end_ms,
            final_start_ms=0,
            final_end_ms=final_end_ms - final_start_ms,
            write_source_range_srt=write_source_range_srt,
            crop_path=crop,
            read_crop=lambda path: _regular_bytes(path, code="CROP_INVALID"),
        )
    except FullWindowReplayError as exc:
        raise C12FinalDeliveryProjectionError(
            "REPLAY_C12_BASELINE_APPLICATION_FAILED"
        ) from exc
    if _sha(cropped) != projection.stage_srt_sha256:
        _fail("RECOMPUTED_DELIVERY_DRIFT")
    try:
        output_text = cropped.decode("utf-8")
    except UnicodeDecodeError:
        _fail("CROP_INVALID")
    project_full_window_audit_to_final_delivery(
        audit, final_start_ms=0, final_end_ms=final_end_ms - final_start_ms
    )
    audit["exact_replay_then_final_crop"] = True
    audit["final_delivery_projection"] = {
        "absolute_source_start_ms": projection.baseline_start_ms,
        "absolute_source_end_ms": projection.padded_start_ms + final_end_ms,
    }
    audit["full_release_delivery_projection"] = {
        "schema_version": _RECEIPT_SCHEMA,
        "receipt_sha256": projection.receipt_sha256,
        "full_release_cue_count": 59,
        "final_delivery_cue_count": len(parse_srt_cues(output_text)),
    }
    audit["operator_text_full_ownership_supersession"] = (
        _ownership_supersession(projection=projection, truth=truth)
    )
    try:
        before = os.lstat(subtitle_path)
    except OSError:
        _fail("SUBTITLE_TARGET_INVALID")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        _fail("SUBTITLE_TARGET_INVALID")
    try:
        descriptor = os.open(
            subtitle_path,
            os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            view = memoryview(cropped)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    _fail("SUBTITLE_TARGET_INVALID")
                view = view[written:]
        finally:
            os.close(descriptor)
    except OSError:
        _fail("SUBTITLE_TARGET_INVALID")
    if _sha(_regular_bytes(subtitle_path, code="SUBTITLE_TARGET_INVALID")) != projection.stage_srt_sha256:
        _fail("SUBTITLE_TARGET_INVALID")
    return output_text, audit
