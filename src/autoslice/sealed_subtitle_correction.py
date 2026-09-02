"""Candidate-sealed replay authority for the 女友感 repair.

This is intentionally not a generic operator-edit facility.  It binds one
known live preimage, four complete-cue substitutions, two positive assertions,
and the current delivery branding identity.  The runner consumes it before it
hands work to the existing transactional correction/reburn implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.apply_subtitle_text_overrides import parse_srt
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)


SCHEMA_VERSION = "sealed-subtitle-correction-authority.v1"
CANDIDATE_ID = "auto_123655_771_844"
RECORDING_DATE = "2026-08-17"
RELATIVE_PATH = (
    "assets/lidousha/sealed_subtitle_corrections/"
    "auto_123655_771_844.v1.json"
)
_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "recording_date",
        "authority",
        "artifacts",
        "delivery",
        "diagnostic",
        "correction",
        "manual_title",
        "upload",
        "authority_sha256",
    }
)
_DESCRIPTOR_FIELDS = frozenset({"path", "sha256", "bytes", "mode", "uid", "gid"})
_ARTIFACT_ROLES = frozenset(
    {"record", "publish", "main", "srt", "ass", "burn", "cover", "chat", "timing"}
)
_DELIVERY_FIELDS = frozenset(
    {
        "video",
        "subtitle",
        "record",
        "speaker_sidecars",
        "correction_receipts",
        "branding_intro",
    }
)
_BRANDING_FIELDS = frozenset(
    {"status", "intro_id", "intro_media_sha256", "intro_offset_ms"}
)
_CORRECTION_FIELDS = frozenset(
    {
        "cue_count",
        "source_srt_sha256",
        "output_srt_sha256",
        "replacements",
        "assertions",
        "other_cues_immutable",
    }
)
_REPLACEMENT_FIELDS = frozenset({"cue_index", "start", "end", "before", "after"})
_ASSERTION_FIELDS = frozenset({"cue_index", "start", "end", "text"})
_IMMUTABLE_ARTIFACT_ROLES = frozenset({"publish", "main", "cover", "chat", "timing"})
_SPEAKER_SIDECAR_FIELDS = frozenset({"srt", "ass", "manifest"})
_CORRECTION_RECEIPT_FIELDS = frozenset({"primary_path", "delivery_path"})
_DIAGNOSTIC_FIELDS = frozenset(
    {"pipeline_srt", "decision_receipt", "operator_full_ownership"}
)
_REPO_DESCRIPTOR_FIELDS = frozenset({"path", "sha256", "bytes"})
_CORRECTION_RECEIPT_V2_FIELDS = frozenset(
    {
        "schema_version",
        "stage_order",
        "corrected_at",
        "candidate_id",
        "before_srt_sha256",
        "after_srt_sha256",
        "replace_operations",
        "set_line_operations",
        "refresh_only",
        "text_source",
        "text_source_sha256",
        "text_override",
        "text_override_sha256",
        "text_override_manifest",
        "text_override_manifest_sha256",
        "timing_source",
        "timing_source_sha256",
        "text_override_decision_output",
        "text_override_decision_output_sha256",
        "text_override_output",
        "text_override_output_sha256",
        "speaker_mode",
        "speaker_manifest",
        "speaker_manifest_sha256",
        "burned_media",
        "burned_media_sha256",
        "delivery_branding_authority",
        "upload_enabled",
    }
)


class SealedSubtitleCorrectionError(ValueError):
    """The candidate-sealed correction cannot safely be replayed."""


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def canonical_authority_path(repo_root: Path) -> Path:
    return repo_root / RELATIVE_PATH


def _require_mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise SealedSubtitleCorrectionError(f"{label} must be an object")
    return dict(value)


def _require_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise SealedSubtitleCorrectionError(f"{label} must be a sha256 digest")
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise SealedSubtitleCorrectionError(f"{label} must be a sha256 digest") from exc
    return value


def _validate_descriptor(value: object, *, label: str) -> dict[str, object]:
    descriptor = _require_mapping(value, label=label)
    if set(descriptor) != _DESCRIPTOR_FIELDS:
        raise SealedSubtitleCorrectionError(f"{label} fields drift")
    path = descriptor["path"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise SealedSubtitleCorrectionError(f"{label} path is invalid")
    _require_sha(descriptor["sha256"], label=f"{label} sha256")
    if (
        isinstance(descriptor["bytes"], bool)
        or not isinstance(descriptor["bytes"], int)
        or descriptor["bytes"] < 0
    ):
        raise SealedSubtitleCorrectionError(f"{label} bytes is invalid")
    if not isinstance(descriptor["mode"], str) or len(descriptor["mode"]) != 4:
        raise SealedSubtitleCorrectionError(f"{label} mode is invalid")
    try:
        int(descriptor["mode"], 8)
    except ValueError as exc:
        raise SealedSubtitleCorrectionError(f"{label} mode is invalid") from exc
    for field in ("uid", "gid"):
        if isinstance(descriptor[field], bool) or not isinstance(descriptor[field], int):
            raise SealedSubtitleCorrectionError(f"{label} {field} is invalid")
    return descriptor


def _validate_repo_descriptor(value: object, *, label: str) -> dict[str, object]:
    descriptor = _require_mapping(value, label=label)
    if set(descriptor) != _REPO_DESCRIPTOR_FIELDS:
        raise SealedSubtitleCorrectionError(f"{label} fields drift")
    path = descriptor["path"]
    if not isinstance(path, str) or not path.startswith("assets/"):
        raise SealedSubtitleCorrectionError(f"{label} path is invalid")
    _require_sha(descriptor["sha256"], label=f"{label} sha256")
    if (
        isinstance(descriptor["bytes"], bool)
        or not isinstance(descriptor["bytes"], int)
        or descriptor["bytes"] < 0
    ):
        raise SealedSubtitleCorrectionError(f"{label} bytes is invalid")
    return descriptor


def validate_authority(value: object) -> dict[str, object]:
    """Validate the static sealed decision independently of deployment state."""

    authority = _require_mapping(value, label="sealed subtitle correction authority")
    declared_sha = authority.pop("authority_sha256", None)
    if set(authority) | {"authority_sha256"} != _AUTHORITY_FIELDS:
        raise SealedSubtitleCorrectionError("sealed subtitle correction authority fields drift")
    if authority.get("schema_version") != SCHEMA_VERSION:
        raise SealedSubtitleCorrectionError("sealed subtitle correction authority schema drifts")
    if authority.get("candidate_id") != CANDIDATE_ID or authority.get("recording_date") != RECORDING_DATE:
        raise SealedSubtitleCorrectionError("sealed subtitle correction authority identity drifts")
    if _require_sha(declared_sha, label="authority_sha256") != _canonical_sha256(authority):
        raise SealedSubtitleCorrectionError("sealed subtitle correction authority self hash drifts")
    if authority.get("upload") is not False:
        raise SealedSubtitleCorrectionError("sealed subtitle correction authority must keep upload disabled")
    if not isinstance(authority.get("manual_title"), str) or authority["manual_title"] != (
        "小李有女友感吗？宿敌是否有点亲密了"
    ):
        raise SealedSubtitleCorrectionError("sealed subtitle correction manual title drifts")
    if not isinstance(authority.get("authority"), str) or not authority["authority"].strip():
        raise SealedSubtitleCorrectionError("sealed subtitle correction operator authority is absent")

    artifacts = _require_mapping(authority.get("artifacts"), label="artifacts")
    if set(artifacts) != _ARTIFACT_ROLES:
        raise SealedSubtitleCorrectionError("sealed subtitle correction artifact roles drift")
    normalized_artifacts = {
        role: _validate_descriptor(value, label=f"artifact {role}")
        for role, value in artifacts.items()
    }

    delivery = _require_mapping(authority.get("delivery"), label="delivery")
    if set(delivery) != _DELIVERY_FIELDS:
        raise SealedSubtitleCorrectionError("sealed subtitle correction delivery fields drift")
    normalized_delivery = {
        key: _validate_descriptor(delivery[key], label=f"delivery {key}")
        for key in ("video", "subtitle", "record")
    }
    speaker_sidecars = _require_mapping(
        delivery["speaker_sidecars"], label="delivery speaker sidecars"
    )
    if set(speaker_sidecars) != _SPEAKER_SIDECAR_FIELDS:
        raise SealedSubtitleCorrectionError("sealed subtitle correction speaker sidecars drift")
    normalized_sidecars = {
        key: _validate_descriptor(value, label=f"delivery speaker {key}")
        for key, value in speaker_sidecars.items()
    }
    correction_receipts = _require_mapping(
        delivery["correction_receipts"], label="delivery correction receipts"
    )
    if set(correction_receipts) != _CORRECTION_RECEIPT_FIELDS or not all(
        isinstance(value, str) and Path(value).is_absolute()
        for value in correction_receipts.values()
    ):
        raise SealedSubtitleCorrectionError("sealed subtitle correction receipt paths drift")
    branding = _require_mapping(delivery["branding_intro"], label="delivery branding intro")
    if set(branding) != _BRANDING_FIELDS:
        raise SealedSubtitleCorrectionError("sealed subtitle correction branding fields drift")
    if branding.get("status") != "PREPENDED" or not isinstance(branding.get("intro_id"), str):
        raise SealedSubtitleCorrectionError("sealed subtitle correction branding identity drifts")
    _require_sha(branding.get("intro_media_sha256"), label="delivery branding intro media")
    if isinstance(branding.get("intro_offset_ms"), bool) or not isinstance(branding.get("intro_offset_ms"), int):
        raise SealedSubtitleCorrectionError("sealed subtitle correction branding offset drifts")

    diagnostic = _require_mapping(authority.get("diagnostic"), label="diagnostic")
    if set(diagnostic) != _DIAGNOSTIC_FIELDS or diagnostic.get("operator_full_ownership") is not False:
        raise SealedSubtitleCorrectionError("sealed subtitle correction diagnostic scope drifts")
    normalized_diagnostic = {
        key: _validate_repo_descriptor(diagnostic[key], label=f"diagnostic {key}")
        for key in ("pipeline_srt", "decision_receipt")
    }

    correction = _require_mapping(authority.get("correction"), label="correction")
    if set(correction) != _CORRECTION_FIELDS:
        raise SealedSubtitleCorrectionError("sealed subtitle correction fields drift")
    if correction.get("cue_count") != 33 or correction.get("other_cues_immutable") is not True:
        raise SealedSubtitleCorrectionError("sealed subtitle correction scope drifts")
    if _require_sha(correction.get("source_srt_sha256"), label="correction source SRT") != normalized_artifacts["srt"]["sha256"]:
        raise SealedSubtitleCorrectionError("sealed subtitle correction source SRT binding drifts")
    _require_sha(correction.get("output_srt_sha256"), label="correction output SRT")
    replacements = correction.get("replacements")
    assertions = correction.get("assertions")
    if not isinstance(replacements, list) or not isinstance(assertions, list):
        raise SealedSubtitleCorrectionError("sealed subtitle correction cue lists are invalid")
    if len(replacements) != 4 or len(assertions) != 2:
        raise SealedSubtitleCorrectionError("sealed subtitle correction cue scope drifts")
    replacement_indices: list[int] = []
    for row in replacements:
        row = _require_mapping(row, label="correction replacement")
        if set(row) != _REPLACEMENT_FIELDS:
            raise SealedSubtitleCorrectionError("sealed subtitle correction replacement fields drift")
        if not isinstance(row["cue_index"], int) or not all(isinstance(row[key], str) and row[key] for key in ("start", "end", "before", "after")):
            raise SealedSubtitleCorrectionError("sealed subtitle correction replacement is invalid")
        replacement_indices.append(row["cue_index"])
    if replacement_indices != [1, 3, 6, 27]:
        raise SealedSubtitleCorrectionError("sealed subtitle correction replacement scope drifts")
    assertion_indices: list[int] = []
    for row in assertions:
        row = _require_mapping(row, label="correction assertion")
        if set(row) != _ASSERTION_FIELDS:
            raise SealedSubtitleCorrectionError("sealed subtitle correction assertion fields drift")
        if not isinstance(row["cue_index"], int) or not all(isinstance(row[key], str) and row[key] for key in ("start", "end", "text")):
            raise SealedSubtitleCorrectionError("sealed subtitle correction assertion is invalid")
        assertion_indices.append(row["cue_index"])
    if assertion_indices != [5, 14]:
        raise SealedSubtitleCorrectionError("sealed subtitle correction assertion scope drifts")
    return {
        **authority,
        "authority_sha256": declared_sha,
        "artifacts": normalized_artifacts,
        "delivery": {
            **normalized_delivery,
            "speaker_sidecars": normalized_sidecars,
            "correction_receipts": correction_receipts,
            "branding_intro": branding,
        },
        "diagnostic": {**normalized_diagnostic, "operator_full_ownership": False},
        "correction": correction,
    }


def load_deployed_authority(repo_root: Path) -> tuple[dict[str, object], dict[str, str]]:
    """Load the exact candidate asset and prove the deployment/Git seal."""

    path = canonical_authority_path(repo_root)
    try:
        payload = path.read_bytes()
        seal = require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=Path(RELATIVE_PATH),
            observed_bytes=payload,
        )
        document = json.loads(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, RepositoryAssetAuthorityError) as exc:
        raise SealedSubtitleCorrectionError("sealed subtitle correction authority is not deployed") from exc
    return validate_authority(document), {
        "mode": seal.mode,
        "deployed_commit": seal.commit,
        "relative_path": seal.relative_path,
        "sha256": seal.file_sha256,
    }


def _check_descriptor(descriptor: Mapping[str, object], *, label: str) -> None:
    path = Path(str(descriptor["path"]))
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise SealedSubtitleCorrectionError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SealedSubtitleCorrectionError(f"{label} must be a regular non-symlink file")
    if (
        metadata.st_size != descriptor["bytes"]
        or stat.S_IMODE(metadata.st_mode) != int(str(descriptor["mode"]), 8)
        or metadata.st_uid != descriptor["uid"]
        or metadata.st_gid != descriptor["gid"]
        or _file_sha256(path) != descriptor["sha256"]
    ):
        raise SealedSubtitleCorrectionError(f"{label} preimage drifts")


def _regular_file(path: Path, *, label: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise SealedSubtitleCorrectionError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SealedSubtitleCorrectionError(f"{label} must be a regular non-symlink file")


def _repository_asset_bytes(
    *, repo_root: Path, descriptor: Mapping[str, object], label: str
) -> bytes:
    relative = Path(str(descriptor["path"]))
    path = repo_root / relative
    try:
        _regular_file(path, label=label)
        payload = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=relative, observed_bytes=payload
        )
    except (OSError, RepositoryAssetAuthorityError) as exc:
        raise SealedSubtitleCorrectionError(f"{label} is not repository-sealed") from exc
    if len(payload) != descriptor["bytes"] or "sha256:" + hashlib.sha256(payload).hexdigest() != descriptor["sha256"]:
        raise SealedSubtitleCorrectionError(f"{label} bytes drift")
    return payload


def _required_prefixed_sha(value: object, *, label: str) -> str:
    digest = _require_sha(value, label=label)
    return digest


def _record_sha(record: Mapping[str, object], key: str) -> str:
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, Mapping):
        raise SealedSubtitleCorrectionError("sealed subtitle correction record lacks artifact hashes")
    return _required_prefixed_sha(hashes.get(key), label=f"record artifact {key}")


def _cue_rows(text: str) -> list[tuple[str, str, str, str]]:
    blocks = [block for block in text.replace("\r\n", "\n").strip().split("\n\n") if block.strip()]
    rows: list[tuple[str, str, str, str]] = []
    for block in blocks:
        lines = block.split("\n")
        if len(lines) < 3 or " --> " not in lines[1]:
            raise SealedSubtitleCorrectionError("sealed subtitle correction SRT shape drifts")
        start, end = lines[1].split(" --> ", 1)
        rows.append((lines[0], start, end, "\n".join(lines[2:])))
    return rows


def _validate_diagnostic_decision_receipt(
    authority: Mapping[str, object], *, pipeline_text: str, receipt_payload: object
) -> None:
    receipt = _require_mapping(receipt_payload, label="diagnostic decision receipt")
    required = {
        "schema_version",
        "candidate_id",
        "recording_date",
        "pipeline_srt_sha256",
        "cue_count",
        "repair_scope",
        "operator_full_ownership",
        "rows",
    }
    if set(receipt) != required or (
        receipt.get("schema_version") != "sealed-subtitle-correction-diagnostic-diff.v1"
        or receipt.get("candidate_id") != CANDIDATE_ID
        or receipt.get("recording_date") != RECORDING_DATE
        or receipt.get("pipeline_srt_sha256") != authority["correction"]["source_srt_sha256"]
        or receipt.get("cue_count") != 33
        or receipt.get("repair_scope") != "TARGETED_REPAIR_PLUS_SYSTEMIC_FIX"
        or receipt.get("operator_full_ownership") is not False
    ):
        raise SealedSubtitleCorrectionError("sealed subtitle correction diagnostic receipt drifts")
    rows = receipt.get("rows")
    source_rows = _cue_rows(pipeline_text)
    if not isinstance(rows, list) or len(rows) != len(source_rows) or len(source_rows) != 33:
        raise SealedSubtitleCorrectionError("sealed subtitle correction diagnostic cue count drifts")
    correction = authority["correction"]
    assert isinstance(correction, Mapping)
    replacements = {
        int(row["cue_index"]): row
        for row in correction["replacements"]
        if isinstance(row, Mapping)
    }
    assertion_indices = {5, 14}
    for position, (source, row) in enumerate(zip(source_rows, rows), start=1):
        row = _require_mapping(row, label="diagnostic decision row")
        common = {
            "cue_index": position,
            "source_index": str(position),
            "start": source[1],
            "end": source[2],
            "pipeline_text": source[3],
        }
        if any(row.get(key) != value for key, value in common.items()):
            raise SealedSubtitleCorrectionError("sealed subtitle correction diagnostic cue geometry drifts")
        if position in replacements:
            expected = replacements[position]
            if set(row) != set(common) | {
                "release_text", "disposition", "decision_authority", "evidence_ref"
            } or (
                row.get("release_text") != expected["after"]
                or row.get("disposition") != "OPERATOR_EXACT_TEXT"
                or row.get("decision_authority") != "REVIEWER_OPERATOR"
                or row.get("evidence_ref") != "维护者 2026-08-19 Claude JSONL line 947 exact cue correction."
            ):
                raise SealedSubtitleCorrectionError("sealed subtitle correction operator decision drifts")
        elif position in assertion_indices:
            if set(row) != set(common) | {"release_text", "disposition", "assertion"} or (
                row.get("release_text") != source[3]
                or row.get("disposition") != "OPERATOR_ASSERTION_ONLY"
                or row.get("assertion")
                != "维护者 line947 reports this text; assertion only, not a full-ownership decision."
            ):
                raise SealedSubtitleCorrectionError("sealed subtitle correction assertion decision drifts")
        elif set(row) != set(common) | {"release_text", "disposition"} or (
            row.get("release_text") != source[3]
            or row.get("disposition") != "MACHINE_UNCHANGED_OUTSIDE_REPORTED_SCOPE"
        ):
            raise SealedSubtitleCorrectionError("sealed subtitle correction unchanged decision drifts")


def validate_diagnostic_assets(
    authority: Mapping[str, object], *, repo_root: Path, expected_source_text: str | None
) -> None:
    """Prove the blind pipeline lane is repository-sealed and never a release baseline."""

    normalized = validate_authority(authority)
    diagnostic = normalized["diagnostic"]
    assert isinstance(diagnostic, Mapping)
    pipeline = _repository_asset_bytes(
        repo_root=repo_root,
        descriptor=diagnostic["pipeline_srt"],
        label="sealed pipeline diagnostic SRT",
    )
    receipt_bytes = _repository_asset_bytes(
        repo_root=repo_root,
        descriptor=diagnostic["decision_receipt"],
        label="sealed diagnostic decision receipt",
    )
    pipeline_text = pipeline.decode("utf-8")
    if expected_source_text is not None and pipeline_text != expected_source_text:
        raise SealedSubtitleCorrectionError("sealed pipeline diagnostic does not match live preimage")
    try:
        receipt = json.loads(receipt_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SealedSubtitleCorrectionError("sealed diagnostic decision receipt is invalid") from exc
    _validate_diagnostic_decision_receipt(
        normalized, pipeline_text=pipeline_text, receipt_payload=receipt
    )


def _validate_exact_cue_transform(
    authority: Mapping[str, object], *, source_text: str, output_text: str
) -> None:
    source_rows = _cue_rows(source_text)
    output_rows = _cue_rows(output_text)
    if len(source_rows) != len(output_rows):
        raise SealedSubtitleCorrectionError("sealed subtitle correction cue count changes")
    correction = authority["correction"]
    assert isinstance(correction, Mapping)
    replacements = correction["replacements"]
    assert isinstance(replacements, list)
    permitted = {int(row["cue_index"]): str(row["after"]) for row in replacements if isinstance(row, Mapping)}
    if set(permitted) != {1, 3, 6, 27}:
        raise SealedSubtitleCorrectionError("sealed subtitle correction replacement scope drifts")
    changed: set[int] = set()
    for index, (before, after) in enumerate(zip(source_rows, output_rows), start=1):
        if before[:3] != after[:3]:
            raise SealedSubtitleCorrectionError("sealed subtitle correction cue geometry drifts")
        expected = permitted.get(index, before[3])
        if after[3] != expected:
            raise SealedSubtitleCorrectionError("sealed subtitle correction cue text drifts")
        if after[3] != before[3]:
            changed.add(index)
    if changed != set(permitted):
        raise SealedSubtitleCorrectionError("sealed subtitle correction changed-cue scope drifts")


def expected_output_srt(authority: Mapping[str, object], source_text: str) -> str:
    """Apply exactly the sealed cue payload while retaining all timing bytes."""

    correction = authority["correction"]
    assert isinstance(correction, Mapping)
    blocks = [block for block in source_text.replace("\r\n", "\n").strip().split("\n\n") if block.strip()]
    replacements = correction["replacements"]
    assert isinstance(replacements, list)
    for row in replacements:
        assert isinstance(row, Mapping)
        index = int(row["cue_index"])
        lines = blocks[index - 1].split("\n")
        blocks[index - 1] = "\n".join(lines[:2] + [str(row["after"])])
    output = "\n\n".join(blocks) + "\n"
    _validate_exact_cue_transform(authority, source_text=source_text, output_text=output)
    return output


def validate_runtime(authority: Mapping[str, object], *, repo_root: Path | None = None) -> str:
    """Prove every preimage and return the only permitted post-correction SRT."""

    normalized = validate_authority(authority)
    artifacts = normalized["artifacts"]
    delivery = normalized["delivery"]
    assert isinstance(artifacts, Mapping) and isinstance(delivery, Mapping)
    for role, descriptor in artifacts.items():
        assert isinstance(descriptor, Mapping)
        _check_descriptor(descriptor, label=f"artifact {role}")
    for role in ("video", "subtitle", "record"):
        descriptor = delivery[role]
        assert isinstance(descriptor, Mapping)
        _check_descriptor(descriptor, label=f"delivery {role}")
    if (
        artifacts["burn"]["sha256"] != delivery["video"]["sha256"]
        or artifacts["srt"]["sha256"] != delivery["subtitle"]["sha256"]
        or artifacts["record"]["sha256"] != delivery["record"]["sha256"]
    ):
        raise SealedSubtitleCorrectionError("sealed subtitle correction delivery mirrors drift")
    sidecars = delivery["speaker_sidecars"]
    assert isinstance(sidecars, Mapping)
    for role, descriptor in sidecars.items():
        assert isinstance(descriptor, Mapping)
        _check_descriptor(descriptor, label=f"delivery stale speaker {role}")
    receipts = delivery["correction_receipts"]
    assert isinstance(receipts, Mapping)
    for label, path in receipts.items():
        if os.path.lexists(str(path)):
            raise SealedSubtitleCorrectionError(f"sealed correction receipt preimage {label} exists")

    source_path = Path(str(artifacts["srt"]["path"]))
    source_text = source_path.read_text(encoding="utf-8")
    cues = parse_srt(source_path)
    correction = normalized["correction"]
    assert isinstance(correction, Mapping)
    if len(cues) != correction["cue_count"]:
        raise SealedSubtitleCorrectionError("sealed subtitle correction cue count drifts")
    if [cue.source_index for cue in cues] != list(range(1, 34)):
        raise SealedSubtitleCorrectionError("sealed subtitle correction cue source indices drift")
    by_index = {cue.source_index: cue for cue in cues}
    for row in correction["replacements"]:
        assert isinstance(row, Mapping)
        cue = by_index.get(int(row["cue_index"]))
        if cue is None or (cue.start, cue.end, cue.text) != (row["start"], row["end"], row["before"]):
            raise SealedSubtitleCorrectionError("sealed subtitle correction replacement preimage drifts")
    for row in correction["assertions"]:
        assert isinstance(row, Mapping)
        cue = by_index.get(int(row["cue_index"]))
        if cue is None or (cue.start, cue.end, cue.text) != (row["start"], row["end"], row["text"]):
            raise SealedSubtitleCorrectionError("sealed subtitle correction unchanged-cue assertion drifts")
    output = expected_output_srt(normalized, source_text)
    if "sha256:" + hashlib.sha256(output.encode("utf-8")).hexdigest() != correction["output_srt_sha256"]:
        raise SealedSubtitleCorrectionError("sealed subtitle correction output closure drifts")
    if repo_root is not None:
        validate_diagnostic_assets(
            normalized, repo_root=repo_root, expected_source_text=source_text
        )
    return output


@dataclass(frozen=True)
class SealedSubtitleCorrectionTransactionContext:
    """Private transaction token which recomputes the sealed context at use time."""

    repo_root: Path

    def validate_record_delta(
        self, *, before: Mapping[str, object], after: Mapping[str, object]
    ) -> None:
        """Reject tag/provider or unrelated record mutation before record staging."""

        if before.get("upload_tags") != after.get("upload_tags"):
            raise SealedSubtitleCorrectionError(
                "sealed correction may not regenerate upload tags"
            )
        allowed = {
            "/artifact_hashes/subtitle_sha256",
            "/artifact_hashes/ass_sha256",
            "/artifact_hashes/burned_video_sha256",
            "/speaker_mode",
            "/speaker_review_srt_path",
            "/subtitle_ass_path",
            "/subtitle_style",
            "/speaker_finalization_manifest_path",
            "/speaker_finalization_manifest_sha256",
            "/speaker_finalization",
            "/human_text_correction_manifest_path",
            "/human_text_correction_manifest_sha256",
        }
        for pointer in _json_pointer_differences(before, after):
            if pointer in allowed or pointer == "/burned_preview" or pointer.startswith("/burned_preview/"):
                continue
            raise SealedSubtitleCorrectionError(
                f"sealed correction record field is outside the allowlist: {pointer}"
            )

    def prepare_for_transaction(
        self, *, args: Any, working_record_path: Path
    ) -> tuple[Mapping[str, object] | None, dict[str, object] | None]:
        """Return a revalidated branding context for only this candidate invocation."""

        if (
            args.cid != CANDIDATE_ID
            or args.date != RECORDING_DATE
            or any(
                value is not None
                for value in (
                    args.delivery_authority_record,
                    args.delivery_authority_publish,
                    args.recovery_branding_authority,
                )
            )
        ):
            raise SealedSubtitleCorrectionError("sealed transaction command identity drifts")
        authority, seal = load_deployed_authority(self.repo_root)
        expected_srt = validate_runtime(authority, repo_root=self.repo_root)
        artifacts = authority["artifacts"]
        delivery = authority["delivery"]
        assert isinstance(artifacts, Mapping) and isinstance(delivery, Mapping)
        if (
            working_record_path != Path(str(artifacts["record"]["path"]))
            or args.delivery != Path(str(delivery["video"]["path"]))
            or Path(str(artifacts["srt"]["path"])).read_text(encoding="utf-8")
            == expected_srt
        ):
            raise SealedSubtitleCorrectionError("sealed transaction preimage is not applicable")
        from src.autoslice.branding_intro import pin_existing_delivery_intro, require_branding_intro

        binding = delivery["branding_intro"]
        assert isinstance(binding, Mapping)
        try:
            branding_intro = pin_existing_delivery_intro(
                require_branding_intro(self.repo_root), binding
            )
        except Exception as exc:
            raise SealedSubtitleCorrectionError(
                "sealed subtitle correction branding context drifts"
            ) from exc
        return branding_intro, {
            "schema_version": "sealed-subtitle-correction-delivery-authority.v1",
            "authority_path": str(self.repo_root / seal["relative_path"]),
            "authority_sha256": seal["sha256"],
            "authority_repository_seal": seal,
            "record_sha256": artifacts["record"]["sha256"],
            "publish_sha256": artifacts["publish"]["sha256"],
            "burned_video_sha256": artifacts["burn"]["sha256"],
            "branding_intro": dict(binding),
        }


def _json_pointer_differences(
    before: object, after: object, *, pointer: str = ""
) -> list[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        differences: list[str] = []
        for key in sorted(set(before) | set(after)):
            child = f"{pointer}/{key}"
            if key not in before or key not in after:
                differences.append(child)
            else:
                differences.extend(
                    _json_pointer_differences(before[key], after[key], pointer=child)
                )
        return differences
    return [] if before == after else [pointer or "/"]


def _validate_correction_receipt(
    authority: Mapping[str, object],
    *,
    receipt: Mapping[str, object],
    correction_path: Path,
    record: Mapping[str, object],
    burn_path: Path,
    repository_seal: Mapping[str, object],
) -> None:
    if set(receipt) != _CORRECTION_RECEIPT_V2_FIELDS or (
        receipt.get("schema_version") != "human-subtitle-correction.v2"
        or receipt.get("stage_order") != "human_text_then_speaker_then_burn"
        or not isinstance(receipt.get("corrected_at"), str)
        or receipt.get("candidate_id") != CANDIDATE_ID
        or receipt.get("speaker_mode") != "uniform_host"
        or receipt.get("speaker_manifest") is not None
        or receipt.get("speaker_manifest_sha256") is not None
        or receipt.get("replace_operations") != []
        or receipt.get("refresh_only") is not False
        or receipt.get("upload_enabled") is not False
    ):
        raise SealedSubtitleCorrectionError("corrected receipt fixed fields drift")
    correction = authority["correction"]
    artifacts = authority["artifacts"]
    delivery = authority["delivery"]
    assert isinstance(correction, Mapping) and isinstance(artifacts, Mapping) and isinstance(delivery, Mapping)
    if (
        receipt.get("before_srt_sha256")
        != str(correction["source_srt_sha256"]).removeprefix("sha256:")
        or receipt.get("after_srt_sha256")
        != str(correction["output_srt_sha256"]).removeprefix("sha256:")
        or receipt.get("burned_media") != str(burn_path)
        or receipt.get("burned_media_sha256") != _file_sha256(burn_path).removeprefix("sha256:")
    ):
        raise SealedSubtitleCorrectionError("corrected receipt hash or burn binding drifts")
    expected_operations = [
        f"{row['cue_index']}={row['after']}"
        for row in correction["replacements"]
        if isinstance(row, Mapping)
    ]
    if receipt.get("set_line_operations") != expected_operations:
        raise SealedSubtitleCorrectionError("corrected receipt set-line operations drift")
    for field in (
        "text_source",
        "text_source_sha256",
        "text_override",
        "text_override_sha256",
        "text_override_manifest",
        "text_override_manifest_sha256",
        "timing_source",
        "timing_source_sha256",
        "text_override_decision_output",
        "text_override_decision_output_sha256",
        "text_override_output",
        "text_override_output_sha256",
    ):
        if receipt.get(field) is not None:
            raise SealedSubtitleCorrectionError("corrected receipt text-override lane drifts")
    binding = delivery["branding_intro"]
    assert isinstance(binding, Mapping)
    expected_delivery_authority = {
        "schema_version": "sealed-subtitle-correction-delivery-authority.v1",
        "authority_path": str(repository_seal["absolute_path"]),
        "authority_sha256": repository_seal["sha256"],
        "authority_repository_seal": dict(repository_seal["seal"]),
        "record_sha256": artifacts["record"]["sha256"],
        "publish_sha256": artifacts["publish"]["sha256"],
        "burned_video_sha256": artifacts["burn"]["sha256"],
        "branding_intro": dict(binding),
    }
    if receipt.get("delivery_branding_authority") != expected_delivery_authority:
        raise SealedSubtitleCorrectionError("corrected receipt delivery authority drifts")
    if record.get("human_text_correction_manifest_path") != str(correction_path):
        raise SealedSubtitleCorrectionError("corrected record correction receipt path drifts")


def validate_post_transaction(
    authority: Mapping[str, object], *, expected_srt: str, repo_root: Path | None = None
) -> None:
    """Require the committed transaction closure before the sealed runner returns."""

    normalized = validate_authority(authority)
    artifacts = normalized["artifacts"]
    delivery = normalized["delivery"]
    assert isinstance(artifacts, Mapping) and isinstance(delivery, Mapping)
    for role in _IMMUTABLE_ARTIFACT_ROLES:
        descriptor = artifacts[role]
        assert isinstance(descriptor, Mapping)
        _check_descriptor(descriptor, label=f"immutable artifact {role}")

    source_srt = Path(str(artifacts["srt"]["path"]))
    _regular_file(source_srt, label="corrected SRT")
    actual_srt = source_srt.read_text(encoding="utf-8")
    if actual_srt != expected_srt:
        raise SealedSubtitleCorrectionError("sealed subtitle correction final SRT differs")
    corrected_srt_sha = _file_sha256(source_srt)

    record_path = Path(str(artifacts["record"]["path"]))
    _regular_file(record_path, label="corrected record")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SealedSubtitleCorrectionError("corrected record is invalid") from exc
    if not isinstance(record, Mapping):
        raise SealedSubtitleCorrectionError("corrected record is invalid")
    if record.get("subtitle_path") != str(source_srt):
        raise SealedSubtitleCorrectionError("corrected record subtitle path drifts")
    if _record_sha(record, "subtitle_sha256") != corrected_srt_sha:
        raise SealedSubtitleCorrectionError("corrected record subtitle hash drifts")

    ass_path = Path(str(record.get("subtitle_ass_path") or ""))
    _regular_file(ass_path, label="corrected ASS")
    if _record_sha(record, "ass_sha256") != _file_sha256(ass_path):
        raise SealedSubtitleCorrectionError("corrected record ASS hash drifts")

    preview = record.get("burned_preview")
    if not isinstance(preview, Mapping):
        raise SealedSubtitleCorrectionError("corrected record burned preview is absent")
    burn_path = Path(str(preview.get("path") or ""))
    _regular_file(burn_path, label="corrected burned video")
    burn_sha = _file_sha256(burn_path)
    if (
        _record_sha(record, "burned_video_sha256") != burn_sha
        or _required_prefixed_sha(preview.get("burned_sha256"), label="burned preview hash")
        != burn_sha
        or preview.get("ass_path") != str(ass_path)
    ):
        raise SealedSubtitleCorrectionError("corrected burned binding drifts")
    binding = delivery["branding_intro"]
    actual_binding = preview.get("branding_intro")
    if not isinstance(binding, Mapping) or not isinstance(actual_binding, Mapping):
        raise SealedSubtitleCorrectionError("corrected branding binding is absent")
    for field in ("intro_id", "intro_media_sha256", "intro_offset_ms"):
        left = str(actual_binding.get(field)).removeprefix("sha256:")
        right = str(binding.get(field)).removeprefix("sha256:")
        if left != right:
            raise SealedSubtitleCorrectionError("corrected branding binding drifts")

    correction_path = Path(str(record.get("human_text_correction_manifest_path") or ""))
    _regular_file(correction_path, label="corrected correction receipt")
    if _required_prefixed_sha(
        record.get("human_text_correction_manifest_sha256"), label="correction receipt hash"
    ) != _file_sha256(correction_path):
        raise SealedSubtitleCorrectionError("corrected correction receipt binding drifts")
    try:
        receipt = json.loads(correction_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SealedSubtitleCorrectionError("corrected correction receipt is invalid") from exc
    if not isinstance(receipt, Mapping):
        raise SealedSubtitleCorrectionError("corrected correction receipt identity drifts")

    delivery_video = Path(str(delivery["video"]["path"]))
    delivery_srt = Path(str(delivery["subtitle"]["path"]))
    delivery_record = Path(str(delivery["record"]["path"]))
    correction_receipts = delivery["correction_receipts"]
    assert isinstance(correction_receipts, Mapping)
    expected_primary_receipt = Path(str(correction_receipts["primary_path"]))
    delivery_receipt = Path(str(correction_receipts["delivery_path"]))
    if correction_path != expected_primary_receipt:
        raise SealedSubtitleCorrectionError("corrected primary receipt path drifts")
    for path, label in (
        (delivery_video, "delivery video"),
        (delivery_srt, "delivery SRT"),
        (delivery_record, "delivery record"),
        (delivery_receipt, "delivery correction receipt"),
    ):
        _regular_file(path, label=label)
    if (
        _file_sha256(delivery_video) != burn_sha
        or _file_sha256(delivery_srt) != corrected_srt_sha
        or delivery_record.read_bytes() != record_path.read_bytes()
        or delivery_receipt.read_bytes() != correction_path.read_bytes()
    ):
        raise SealedSubtitleCorrectionError("corrected delivery mirrors drift")
    sidecars = delivery["speaker_sidecars"]
    assert isinstance(sidecars, Mapping)
    for label, descriptor in sidecars.items():
        assert isinstance(descriptor, Mapping)
        if os.path.lexists(str(descriptor["path"])):
            raise SealedSubtitleCorrectionError(
                f"corrected uniform-host speaker sidecar remains: {label}"
            )
    if repo_root is not None:
        loaded, seal = load_deployed_authority(repo_root)
        if loaded != normalized:
            raise SealedSubtitleCorrectionError("sealed correction authority changed posttransaction")
        validate_diagnostic_assets(
            normalized, repo_root=repo_root, expected_source_text=None
        )
        _validate_correction_receipt(
            normalized,
            receipt=receipt,
            correction_path=correction_path,
            record=record,
            burn_path=burn_path,
            repository_seal={
                "absolute_path": repo_root / seal["relative_path"],
                "sha256": seal["sha256"],
                "seal": seal,
            },
        )
    if normalized.get("upload") is not False:
        raise SealedSubtitleCorrectionError("sealed correction upload control drifts")
