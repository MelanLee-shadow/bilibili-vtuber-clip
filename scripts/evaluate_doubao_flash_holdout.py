#!/usr/bin/env python3
"""Frozen, truth-withheld evaluation of the Volcengine Flash ASR route against BCUT.

This evaluates only the documented ``recognize/flash`` endpoint with resource
``volc.bigasr.auc_turbo``. It does not establish results for Seed-ASR or the
separately marketed Doubao ASR 2.0 product.

The workflow is intentionally split into four processes:

``freeze``
    Build an audio/baseline generation manifest and a separate withheld-truth
    manifest from already-existing historical assets.
``preflight``
    Validate only the generation manifest, media hashes, and credential shape.
``run``
    Call the explicit ``doubao_flash`` evidence route.  This process never
    accepts or derives the truth manifest.  Every item is guarded by a durable
    content-bound dispatch ledger; an ambiguous one-shot request is never
    automatically resubmitted.
``score``
    Open the withheld truth only after generation has finished and compare the
    provider-native transcript with the frozen BCUT transcript on the same
    temporal reference.

The historical reviewed subtitles are release references, not pure acoustic
verbatim truth.  The reported metric is therefore named release-reference
edit distance and must not be presented as standard CER.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping
import unicodedata
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.autoslice.content_bound_request_ledger import (  # noqa: E402
    ContentBoundRequestLedger,
    RequestLedgerError,
)
from src.autoslice.doubao_transcription import (  # noqa: E402
    DOUBAO_FLASH_ENDPOINT,
    DOUBAO_FLASH_MODEL,
    DOUBAO_FLASH_RESOURCE,
    DoubaoTranscriptionError,
    doubao_api_key_status,
    transcribe_doubao_flash_evidence,
)


GENERATION_SCHEMA = "doubao-flash-holdout-generation.v1"
TRUTH_SCHEMA = "doubao-flash-holdout-withheld-truth.v1"
PREFLIGHT_SCHEMA = "doubao-flash-holdout-preflight.v1"
RUN_SCHEMA = "doubao-flash-holdout-run.v1"
RESULT_SCHEMA = "doubao-flash-holdout-provider-result.v1"
SCORE_SCHEMA = "doubao-flash-holdout-score.v2"
MAX_MANIFEST_BYTES = 5_000_000
MAX_RESULT_BYTES = 25_000_000
MAX_AUDIO_BYTES = 20_000_000
MAX_ITEMS = 24
TIME_OVERLAP_TOLERANCE_MS = 40
ENCODER_DURATION_TOLERANCE_MS = 750
EVALUATION_NAMESPACE = uuid.UUID("705e568f-2dc8-4be5-85bc-da28cb8f1527")
CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

REFERENCE_ANNOTATION_POLICY = {
    "policy_id": "lidousha-reviewed-reference-speaker-labels.v1",
    "scope": "withheld_reference_only_before_unicode_normalization",
    "exact_non_acoustic_labels": ["[李豆沙]"],
}

# Keep this explicit and candidate-blind.  It mirrors the provider client and
# is hash-bound into every request identity.
FLASH_REQUEST_POLICY = {
    "model_name": "bigmodel",
    "enable_itn": False,
    "enable_punc": True,
    "enable_ddc": False,
    "enable_speaker_info": False,
    "show_utterances": True,
    "enable_lid": False,
    "hotwords": False,
    "replacement_table": False,
    "prior_transcript": False,
    "dialog_context": False,
}


PILOT_DECISION_POLICY = {
    "policy_id": "doubao-flash-release-reference-pilot.v1",
    "required_provider_result_fraction": 1.0,
    "minimum_relative_d_keep_reduction": 0.10,
    "minimum_paired_win_fraction": 0.60,
    "maximum_paired_loss_fraction": 0.20,
    "advance_scope": "LARGER_UNTOUCHED_HOLDOUT_ONLY",
    "production_authorized": False,
    "note": (
        "Exploratory pilot threshold frozen before any provider output. Passing "
        "does not establish standard acoustic CER or production utility."
    ),
}


class HoldoutError(RuntimeError):
    """Typed evaluation failure safe to report without provider secrets."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _error(reason_code: str, message: str) -> HoldoutError:
    return HoldoutError(reason_code, message)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise _error("HOLDOUT_VALUE_INVALID", "value is not canonical JSON") from None


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_binding() -> dict[str, Any]:
    files = (
        "scripts/evaluate_doubao_flash_holdout.py",
        "src/autoslice/doubao_transcription.py",
        "src/autoslice/content_bound_request_ledger.py",
    )
    rows = []
    for relative in files:
        path = ROOT / relative
        rows.append(
            {
                "path": relative,
                "sha256": _sha256_path(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return {"files": rows, "files_sha256": _digest(rows)}


def _regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int = MAX_MANIFEST_BYTES,
) -> Path:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_absolute():
        raise _error("HOLDOUT_PATH_NOT_ABSOLUTE", f"{label} path must be absolute")
    try:
        info = path.lstat()
    except OSError:
        raise _error("HOLDOUT_FILE_MISSING", f"{label} is missing") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _error("HOLDOUT_PATH_UNSAFE", f"{label} must be one regular non-symlink file")
    if not 0 < info.st_size <= max_bytes:
        raise _error("HOLDOUT_FILE_SIZE_INVALID", f"{label} has an invalid size")
    return path.resolve(strict=True)


def _load_json(
    path: Path,
    *,
    label: str,
    max_bytes: int = MAX_MANIFEST_BYTES,
) -> dict[str, Any]:
    path = _regular_file(path, label=label, max_bytes=max_bytes)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise _error("HOLDOUT_JSON_INVALID", f"{label} is not valid JSON") from None
    if not isinstance(value, dict):
        raise _error("HOLDOUT_JSON_INVALID", f"{label} must be an object")
    return value


def _ensure_output_directory(path: Path) -> Path:
    path = path.absolute()
    if path.is_symlink():
        raise _error("HOLDOUT_OUTPUT_PATH_UNSAFE", "output directory may not be a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        info = path.lstat()
    except OSError:
        raise _error("HOLDOUT_OUTPUT_PATH_UNSAFE", "output directory cannot be inspected") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise _error("HOLDOUT_OUTPUT_PATH_UNSAFE", "output directory must be user-owned")
    # This directory contains provider evidence and private local paths.
    os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o077)
    return path


def _atomic_write(path: Path, data: bytes) -> None:
    _ensure_output_directory(path.parent)
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise _error("HOLDOUT_WRITE_FAILED", f"cannot persist {path.name}") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _seal(value: Mapping[str, Any], *, field: str) -> dict[str, Any]:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return {**unsigned, field: _digest(unsigned)}


def _verify_seal(value: Mapping[str, Any], *, field: str, label: str) -> dict[str, Any]:
    unsigned = dict(value)
    supplied = unsigned.pop(field, None)
    if supplied != _digest(unsigned):
        raise _error("HOLDOUT_HASH_MISMATCH", f"{label} self-hash differs")
    return {**unsigned, field: supplied}


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        dict(value), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ).encode("utf-8") + b"\n"
    _atomic_write(path, encoded)


def _safe_child(root: Path, relative: str, *, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise _error("HOLDOUT_PATH_INVALID", f"{label} relative path is invalid")
    root_resolved = root.resolve(strict=True)
    candidate = (root_resolved / relative).resolve(strict=True)
    try:
        common = Path(os.path.commonpath([str(root_resolved), str(candidate)]))
    except ValueError:
        raise _error("HOLDOUT_PATH_ESCAPE", f"{label} escapes its root") from None
    if common != root_resolved:
        raise _error("HOLDOUT_PATH_ESCAPE", f"{label} escapes its root")
    return candidate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ms(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", value.strip())
    if match is None:
        raise _error("HOLDOUT_SRT_INVALID", "SRT timestamp is invalid")
    hours, minutes, seconds, millis = map(int, match.groups())
    if minutes > 59 or seconds > 59:
        raise _error("HOLDOUT_SRT_INVALID", "SRT timestamp is out of range")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _parse_srt(path: Path, *, label: str) -> list[dict[str, Any]]:
    path = _regular_file(path, label=label)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise _error("HOLDOUT_SRT_INVALID", f"{label} cannot be decoded") from None
    cues: list[dict[str, Any]] = []
    expected_number = 1
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.rstrip("\r") for line in block.splitlines()]
        if not lines or not any(line.strip() for line in lines):
            continue
        if len(lines) < 3:
            raise _error("HOLDOUT_SRT_INVALID", f"{label} has a short cue")
        try:
            number = int(lines[0].strip())
        except ValueError:
            raise _error("HOLDOUT_SRT_INVALID", f"{label} cue number is invalid") from None
        if number != expected_number:
            raise _error("HOLDOUT_SRT_INVALID", f"{label} cue numbering is not contiguous")
        timing = lines[1].split(" --> ")
        if len(timing) != 2:
            raise _error("HOLDOUT_SRT_INVALID", f"{label} cue timing is invalid")
        start_ms, end_ms = _ms(timing[0]), _ms(timing[1])
        cue_text = " ".join(line.strip() for line in lines[2:] if line.strip())
        if end_ms <= start_ms or not cue_text:
            raise _error("HOLDOUT_SRT_INVALID", f"{label} cue is empty or reversed")
        cues.append(
            {
                "n": number,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": cue_text,
            }
        )
        expected_number += 1
    if not cues:
        raise _error("HOLDOUT_SRT_INVALID", f"{label} has no cues")
    return cues


def _format_ms(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def _render_srt(segments: Iterable[Mapping[str, Any]]) -> str:
    blocks: list[str] = []
    for index, segment in enumerate(segments, 1):
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        text = segment.get("text")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise _error("HOLDOUT_PROVIDER_SEGMENT_INVALID", "provider segment cannot form SRT")
        blocks.append(
            f"{index}\n{_format_ms(start_ms)} --> {_format_ms(end_ms)}\n{text.strip()}"
        )
    if not blocks:
        raise _error("HOLDOUT_PROVIDER_SEGMENT_INVALID", "provider supplied no SRT cues")
    return "\n\n".join(blocks) + "\n"


def _normalize(text: str) -> str:
    return "".join(
        char.casefold()
        for char in unicodedata.normalize("NFKC", text)
        if unicodedata.category(char)[0] not in {"P", "Z", "C"} and not char.isspace()
    )


def _prepare_reference_text(text: str) -> tuple[str, dict[str, int]]:
    """Remove only declared non-acoustic release annotations from truth text."""

    prepared = text
    removed: dict[str, int] = {}
    for label in REFERENCE_ANNOTATION_POLICY["exact_non_acoustic_labels"]:
        count = prepared.count(label)
        if count:
            prepared = prepared.replace(label, " ")
            removed[label] = count
    return prepared, removed


def _merge_annotation_counts(
    left: Mapping[str, int], right: Mapping[str, int]
) -> dict[str, int]:
    merged = dict(left)
    for label, count in right.items():
        merged[label] = merged.get(label, 0) + count
    return dict(sorted(merged.items()))


def _distance(left: str, right: str) -> int:
    row = list(range(len(right) + 1))
    for index, left_char in enumerate(left, 1):
        next_row = [index]
        for column, right_char in enumerate(right, 1):
            next_row.append(
                min(
                    next_row[-1] + 1,
                    row[column] + 1,
                    row[column - 1] + (left_char != right_char),
                )
            )
        row = next_row
    return row[-1]


def _overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        min(int(left["end_ms"]), int(right["end_ms"]))
        - max(int(left["start_ms"]), int(right["start_ms"]))
        > TIME_OVERLAP_TOLERANCE_MS
    )


def _temporal_groups(
    reference: list[dict[str, Any]],
    hypothesis: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes = [("r", index) for index in range(len(reference))] + [
        ("h", index) for index in range(len(hypothesis))
    ]
    edges = {node: set() for node in nodes}
    for reference_index, reference_cue in enumerate(reference):
        for hypothesis_index, hypothesis_cue in enumerate(hypothesis):
            if _overlap(reference_cue, hypothesis_cue):
                edges[("r", reference_index)].add(("h", hypothesis_index))
                edges[("h", hypothesis_index)].add(("r", reference_index))
    seen: set[tuple[str, int]] = set()
    groups: list[dict[str, Any]] = []
    for node in nodes:
        if node in seen:
            continue
        pending = [node]
        component: set[tuple[str, int]] = set()
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(edges[current] - component)
        seen |= component
        reference_rows = [reference[index] for kind, index in component if kind == "r"]
        hypothesis_rows = [hypothesis[index] for kind, index in component if kind == "h"]
        reference_rows.sort(key=lambda row: row["n"])
        hypothesis_rows.sort(key=lambda row: row["n"])
        rows = reference_rows + hypothesis_rows
        reference_text = " ".join(row["text"] for row in reference_rows)
        scored_reference, removed_annotations = _prepare_reference_text(reference_text)
        hypothesis_text = " ".join(row["text"] for row in hypothesis_rows)
        groups.append(
            {
                "start_ms": min(row["start_ms"] for row in rows),
                "end_ms": max(row["end_ms"] for row in rows),
                "reference_cues": [row["n"] for row in reference_rows],
                "hypothesis_cues": [row["n"] for row in hypothesis_rows],
                "reference": reference_text,
                "scored_reference": scored_reference,
                "reference_annotations_removed": removed_annotations,
                "hypothesis": hypothesis_text,
                "distance": _distance(
                    _normalize(scored_reference), _normalize(hypothesis_text)
                ),
                "reference_chars": len(_normalize(scored_reference)),
                "scope": "REFERENCE_BEARING" if reference_rows else "UNMATCHED_OUTPUT",
            }
        )
    return sorted(groups, key=lambda row: (row["start_ms"], row["end_ms"]))


def _score_cues(
    reference: list[dict[str, Any]],
    hypothesis: list[dict[str, Any]],
) -> dict[str, Any]:
    groups = _temporal_groups(reference, hypothesis)
    reference_chars = sum(row["reference_chars"] for row in groups)
    d_keep = sum(row["distance"] for row in groups if row["scope"] == "REFERENCE_BEARING")
    unmatched = sum(row["distance"] for row in groups if row["scope"] == "UNMATCHED_OUTPUT")
    reference_text = " ".join(row["text"] for row in reference)
    scored_reference, whole_removed = _prepare_reference_text(reference_text)
    hypothesis_text = " ".join(row["text"] for row in hypothesis)
    whole_distance = _distance(_normalize(scored_reference), _normalize(hypothesis_text))
    removed_annotations: dict[str, int] = {}
    for row in groups:
        removed_annotations = _merge_annotation_counts(
            removed_annotations, row["reference_annotations_removed"]
        )
    if removed_annotations != whole_removed:
        raise _error(
            "HOLDOUT_REFERENCE_ANNOTATION_MISMATCH",
            "reference annotation accounting differs between grouped and whole-text scoring",
        )
    return {
        "reference_cue_count": len(reference),
        "hypothesis_cue_count": len(hypothesis),
        "reference_chars": reference_chars,
        "reference_annotation_policy": REFERENCE_ANNOTATION_POLICY["policy_id"],
        "reference_annotations_removed": removed_annotations,
        "d_keep_distance": d_keep,
        "d_keep_ratio": round(d_keep / reference_chars, 8) if reference_chars else None,
        "unmatched_output_distance": unmatched,
        "whole_text_distance": whole_distance,
        "whole_text_ratio": round(whole_distance / reference_chars, 8) if reference_chars else None,
        "temporal_groups": groups,
        "metric_note": (
            "NFKC/case/punctuation/spacing normalized edit distance on a historical "
            "human-approved release reference after declared non-acoustic speaker-label "
            "removal; this is not standard acoustic CER."
        ),
    }


def _provider_contract() -> dict[str, Any]:
    return {
        "provider": "doubao_flash",
        "endpoint": DOUBAO_FLASH_ENDPOINT,
        "resource_id": DOUBAO_FLASH_RESOURCE,
        "model": DOUBAO_FLASH_MODEL,
        "transport": "official_direct_base64",
        "official_contract": {
            "document_id": "6561/1631584",
            "document_title": "录音文件极速版识别HTTP",
            "seed_asr_mapping_verified": False,
            "doubao_asr_2_0_mapping_verified": False,
        },
        "request_policy": FLASH_REQUEST_POLICY,
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
    }


def _load_generation_manifest(path: Path) -> dict[str, Any]:
    manifest = _verify_seal(
        _load_json(path, label="generation manifest"),
        field="manifest_sha256",
        label="generation manifest",
    )
    if manifest.get("schema_version") != GENERATION_SCHEMA:
        raise _error("HOLDOUT_SCHEMA_MISMATCH", "generation manifest schema differs")
    items = manifest.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise _error("HOLDOUT_MANIFEST_INVALID", "generation item count is invalid")
    if manifest.get("item_count") != len(items):
        raise _error("HOLDOUT_MANIFEST_INVALID", "generation item count differs")
    if manifest.get("scoring_truth_supplied_to_provider_run") is not False:
        raise _error("HOLDOUT_TRUTH_SEPARATION_INVALID", "truth separation flag differs")
    if manifest.get("provider_contract") != _provider_contract():
        raise _error("HOLDOUT_PROVIDER_CONTRACT_MISMATCH", "provider contract differs")
    if manifest.get("pilot_decision_policy") != PILOT_DECISION_POLICY:
        raise _error("HOLDOUT_DECISION_POLICY_MISMATCH", "pilot decision policy differs")
    current_implementation = _implementation_binding()
    if (
        manifest.get("implementation") != current_implementation
        or manifest.get("implementation_sha256") != _digest(current_implementation)
    ):
        raise _error(
            "HOLDOUT_IMPLEMENTATION_MISMATCH",
            "frozen evaluator/client implementation hashes differ",
        )
    seen_ids: set[str] = set()
    seen_requests: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise _error("HOLDOUT_MANIFEST_INVALID", "generation item is not an object")
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, str) or CANDIDATE_ID.fullmatch(candidate_id) is None:
            raise _error("HOLDOUT_MANIFEST_INVALID", "candidate id is invalid")
        if candidate_id in seen_ids:
            raise _error("HOLDOUT_MANIFEST_INVALID", "candidate id is duplicated")
        seen_ids.add(candidate_id)
        request_id = item.get("request_id")
        try:
            canonical_request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, AttributeError):
            raise _error("HOLDOUT_MANIFEST_INVALID", "request id is invalid") from None
        if request_id != canonical_request_id or request_id in seen_requests:
            raise _error("HOLDOUT_MANIFEST_INVALID", "request id is duplicated or noncanonical")
        seen_requests.add(request_id)
    return manifest


def _load_truth_manifest(path: Path) -> dict[str, Any]:
    manifest = _verify_seal(
        _load_json(path, label="withheld truth manifest"),
        field="manifest_sha256",
        label="withheld truth manifest",
    )
    if manifest.get("schema_version") != TRUTH_SCHEMA:
        raise _error("HOLDOUT_SCHEMA_MISMATCH", "withheld truth schema differs")
    if manifest.get("reference_kind") != "historical_human_approved_release_text":
        raise _error("HOLDOUT_TRUTH_INVALID", "withheld reference kind differs")
    if manifest.get("standard_acoustic_cer_truth") is not False:
        raise _error("HOLDOUT_TRUTH_INVALID", "withheld reference overclaims acoustic truth")
    return manifest


def _validate_hashed_file(surface: Mapping[str, Any], *, label: str, max_bytes: int) -> Path:
    path_value = surface.get("path")
    expected_sha = surface.get("sha256")
    expected_size = surface.get("size_bytes")
    if not isinstance(path_value, str) or not isinstance(expected_sha, str):
        raise _error("HOLDOUT_MANIFEST_INVALID", f"{label} binding is invalid")
    path = _regular_file(Path(path_value), label=label, max_bytes=max_bytes)
    if expected_size is not None and expected_size != path.stat().st_size:
        raise _error("HOLDOUT_INPUT_CHANGED", f"{label} size differs")
    if _sha256_path(path) != expected_sha:
        raise _error("HOLDOUT_INPUT_CHANGED", f"{label} SHA-256 differs")
    return path


def freeze_manifests(
    *,
    historical_intent: Path,
    baseline_root: Path,
    generation_manifest_path: Path,
    truth_manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if generation_manifest_path.absolute() == truth_manifest_path.absolute():
        raise _error("HOLDOUT_TRUTH_SEPARATION_INVALID", "generation and truth paths must differ")
    if generation_manifest_path.exists() or truth_manifest_path.exists():
        raise _error(
            "HOLDOUT_FROZEN_EXISTS",
            "frozen manifests already exist; create a new evaluation directory",
        )
    historical_intent = _regular_file(historical_intent.absolute(), label="historical intent")
    intent = _load_json(historical_intent, label="historical intent")
    items = intent.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise _error("HOLDOUT_SOURCE_INVALID", "historical intent item count is invalid")
    baseline_root = baseline_root.absolute().resolve(strict=True)
    if not baseline_root.is_dir() or baseline_root.is_symlink():
        raise _error("HOLDOUT_SOURCE_INVALID", "baseline root is invalid")

    provider_contract = _provider_contract()
    provider_contract_sha256 = _digest(provider_contract)
    implementation = _implementation_binding()
    implementation_sha256 = _digest(implementation)
    generation_items: list[dict[str, Any]] = []
    truth_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise _error("HOLDOUT_SOURCE_INVALID", "historical item is invalid")
        candidate_id = raw.get("candidate_id")
        duration_ms = raw.get("duration_ms")
        if (
            not isinstance(candidate_id, str)
            or CANDIDATE_ID.fullmatch(candidate_id) is None
            or candidate_id in seen
        ):
            raise _error("HOLDOUT_SOURCE_INVALID", "historical candidate id is invalid")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 1_000:
            raise _error("HOLDOUT_SOURCE_INVALID", "historical duration is invalid")
        seen.add(candidate_id)

        audio_value = raw.get("encoded_audio_path")
        if not isinstance(audio_value, str):
            raise _error("HOLDOUT_SOURCE_INVALID", f"{candidate_id} has no encoded audio")
        audio_path = _regular_file(
            Path(audio_value).absolute(),
            label=f"{candidate_id} encoded audio",
            max_bytes=MAX_AUDIO_BYTES,
        )
        audio_format = audio_path.suffix.casefold().lstrip(".")
        if audio_format not in {"mp3", "wav", "ogg"}:
            raise _error("HOLDOUT_SOURCE_INVALID", f"{candidate_id} audio format is unsupported")
        audio_sha256 = _sha256_path(audio_path)

        cache_root = raw.get("cache_root")
        if not isinstance(cache_root, str):
            raise _error("HOLDOUT_SOURCE_INVALID", f"{candidate_id} cache root is missing")
        bcut_path = (
            Path(cache_root).absolute() / "results" / candidate_id / "bcut" / "transcript.srt"
        )
        bcut_path = _regular_file(bcut_path, label=f"{candidate_id} BCUT transcript")
        _parse_srt(bcut_path, label=f"{candidate_id} BCUT transcript")
        bcut_sha256 = _sha256_path(bcut_path)

        metadata_path = _regular_file(
            baseline_root / f"{candidate_id}.subtitle-baseline.v1.json",
            label=f"{candidate_id} baseline metadata",
        )
        metadata = _load_json(metadata_path, label=f"{candidate_id} baseline metadata")
        reference_path = _regular_file(
            _safe_child(baseline_root, metadata.get("path"), label=f"{candidate_id} reference"),
            label=f"{candidate_id} reviewed reference",
        )
        reference_sha256 = _sha256_path(reference_path)
        if reference_sha256 != metadata.get("sha256"):
            raise _error("HOLDOUT_INPUT_CHANGED", f"{candidate_id} reference hash differs")
        _parse_srt(reference_path, label=f"{candidate_id} reviewed reference")

        binding = {
            "candidate_id": candidate_id,
            "input_audio_sha256": audio_sha256,
            "duration_ms": duration_ms,
            "audio_format": audio_format,
            "provider_contract_sha256": provider_contract_sha256,
        }
        request_id = str(uuid.uuid5(EVALUATION_NAMESPACE, _digest(binding)))
        generation_items.append(
            {
                "candidate_id": candidate_id,
                "duration_ms": duration_ms,
                "request_id": request_id,
                "audio": {
                    "path": str(audio_path),
                    "sha256": audio_sha256,
                    "size_bytes": audio_path.stat().st_size,
                    "format": audio_format,
                },
                "bcut_baseline": {
                    "path": str(bcut_path),
                    "sha256": bcut_sha256,
                    "size_bytes": bcut_path.stat().st_size,
                },
            }
        )
        truth_item: dict[str, Any] = {
            "candidate_id": candidate_id,
            "reference": {
                "path": str(reference_path),
                "sha256": reference_sha256,
                "size_bytes": reference_path.stat().st_size,
            },
            "baseline_metadata": {
                "path": str(metadata_path),
                "sha256": _sha256_path(metadata_path),
                "size_bytes": metadata_path.stat().st_size,
            },
        }
        lane = metadata.get("operator_truth_lanes")
        if isinstance(lane, dict):
            frozen_lane: dict[str, Any] = {}
            for key in ("pipeline_diagnostic", "decision_ledger"):
                surface = lane.get(key)
                if not isinstance(surface, dict):
                    continue
                lane_path = _regular_file(
                    _safe_child(
                        baseline_root,
                        surface.get("path"),
                        label=f"{candidate_id} {key}",
                    ),
                    label=f"{candidate_id} {key}",
                )
                lane_sha = _sha256_path(lane_path)
                if lane_sha != surface.get("sha256"):
                    raise _error("HOLDOUT_INPUT_CHANGED", f"{candidate_id} {key} hash differs")
                frozen_lane[key] = {
                    "path": str(lane_path),
                    "sha256": lane_sha,
                    "size_bytes": lane_path.stat().st_size,
                }
            if frozen_lane:
                truth_item["operator_truth_lanes"] = frozen_lane
        truth_items.append(truth_item)

    cohort_rows = [
        {
            "candidate_id": item["candidate_id"],
            "duration_ms": item["duration_ms"],
            "audio_sha256": item["audio"]["sha256"],
            "bcut_sha256": item["bcut_baseline"]["sha256"],
        }
        for item in generation_items
    ]
    cohort_sha256 = _digest(cohort_rows)
    generation = _seal(
        {
            "schema_version": GENERATION_SCHEMA,
            "created_at_utc": _now(),
            "source_intent": {
                "path": str(historical_intent),
                "sha256": _sha256_path(historical_intent),
            },
            "cohort_sha256": cohort_sha256,
            "item_count": len(generation_items),
            "total_duration_ms": sum(item["duration_ms"] for item in generation_items),
            "provider_contract": provider_contract,
            "provider_contract_sha256": provider_contract_sha256,
            "pilot_decision_policy": PILOT_DECISION_POLICY,
            "pilot_decision_policy_sha256": _digest(PILOT_DECISION_POLICY),
            "implementation": implementation,
            "implementation_sha256": implementation_sha256,
            "scoring_truth_supplied_to_provider_run": False,
            "gold_text_supplied_to_provider": False,
            "generation_process_accepts_truth_manifest": False,
            "items": generation_items,
        },
        field="manifest_sha256",
    )
    truth = _seal(
        {
            "schema_version": TRUTH_SCHEMA,
            "created_at_utc": _now(),
            "generation_manifest_sha256": generation["manifest_sha256"],
            "cohort_sha256": cohort_sha256,
            "item_count": len(truth_items),
            "reference_kind": "historical_human_approved_release_text",
            "standard_acoustic_cer_truth": False,
            "metric_scope_note": (
                "The reference contains release edits and omissions. Score temporal "
                "reference-bearing groups and report unmatched output separately."
            ),
            "items": truth_items,
        },
        field="manifest_sha256",
    )
    _write_json(generation_manifest_path.absolute(), generation)
    _write_json(truth_manifest_path.absolute(), truth)
    return generation, truth


def _probe_duration_ms(path: Path) -> int:
    try:
        run = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=20,
            check=True,
        )
        seconds = float(run.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        raise _error("HOLDOUT_MEDIA_PROBE_FAILED", f"cannot probe {path.name}") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise _error("HOLDOUT_MEDIA_PROBE_FAILED", f"invalid duration for {path.name}")
    return round(seconds * 1000)


def preflight(
    *,
    generation_manifest_path: Path,
    output_path: Path,
    probe_media: bool = False,
) -> dict[str, Any]:
    generation = _load_generation_manifest(generation_manifest_path.absolute())
    item_reports: list[dict[str, Any]] = []
    for item in generation["items"]:
        audio_path = _validate_hashed_file(
            item["audio"], label=f"{item['candidate_id']} audio", max_bytes=MAX_AUDIO_BYTES
        )
        bcut_path = _validate_hashed_file(
            item["bcut_baseline"],
            label=f"{item['candidate_id']} BCUT transcript",
            max_bytes=MAX_MANIFEST_BYTES,
        )
        _parse_srt(bcut_path, label=f"{item['candidate_id']} BCUT transcript")
        row: dict[str, Any] = {
            "candidate_id": item["candidate_id"],
            "status": "INPUTS_VERIFIED",
            "audio_sha256": item["audio"]["sha256"],
            "bcut_sha256": item["bcut_baseline"]["sha256"],
        }
        if probe_media:
            observed_duration = _probe_duration_ms(audio_path)
            row["observed_duration_ms"] = observed_duration
            row["duration_delta_ms"] = observed_duration - item["duration_ms"]
            if abs(row["duration_delta_ms"]) > ENCODER_DURATION_TOLERANCE_MS:
                raise _error(
                    "HOLDOUT_DURATION_MISMATCH",
                    f"{item['candidate_id']} audio duration differs from the frozen interval",
                )
        item_reports.append(row)
    credential = doubao_api_key_status()
    status = "READY" if credential["configured"] else "BLOCKED_NO_CREDENTIALS"
    report = _seal(
        {
            "schema_version": PREFLIGHT_SCHEMA,
            "checked_at_utc": _now(),
            "status": status,
            "generation_manifest_sha256": generation["manifest_sha256"],
            "cohort_sha256": generation["cohort_sha256"],
            "provider_contract_sha256": generation["provider_contract_sha256"],
            "credential": credential,
            "truth_manifest_opened": False,
            "provider_calls": 0,
            "probe_media": probe_media,
            "items": item_reports,
        },
        field="report_sha256",
    )
    _write_json(output_path.absolute(), report)
    return report


def _item_binding(generation: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evaluation_schema": GENERATION_SCHEMA,
        "generation_manifest_sha256": generation["manifest_sha256"],
        "cohort_sha256": generation["cohort_sha256"],
        "candidate_id": item["candidate_id"],
        "provider": "doubao_flash",
        "model": DOUBAO_FLASH_MODEL,
        "resource_id": DOUBAO_FLASH_RESOURCE,
        "input_audio_sha256": item["audio"]["sha256"],
        "duration_ms": item["duration_ms"],
        "request_config_sha256": generation["provider_contract_sha256"],
        "implementation_sha256": generation["implementation_sha256"],
        "candidate_exposure": "none",
    }


def _validate_provider_evidence(
    evidence: Mapping[str, Any],
    *,
    item: Mapping[str, Any],
) -> dict[str, Any]:
    value = dict(evidence)
    expected = {
        "provider": "doubao_flash",
        "model": DOUBAO_FLASH_MODEL,
        "resource_id": DOUBAO_FLASH_RESOURCE,
        "input_audio_sha256": item["audio"]["sha256"],
        "request_id": item["request_id"],
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise _error(
            "HOLDOUT_RESULT_BINDING_MISMATCH",
            f"{item['candidate_id']} provider evidence binding differs",
        )
    for field in ("request_config_sha256", "response_sha256"):
        field_value = value.get(field)
        if not isinstance(field_value, str) or re.fullmatch(r"[0-9a-f]{64}", field_value) is None:
            raise _error(
                "HOLDOUT_RESULT_BINDING_MISMATCH",
                f"{item['candidate_id']} provider evidence hash is invalid",
            )
    if value.get("status") not in {"OK", "TEXT_UNLOCATED", "NO_SPEECH"}:
        raise _error(
            "HOLDOUT_RESULT_INVALID",
            f"{item['candidate_id']} provider evidence status is invalid",
        )
    if not isinstance(value.get("native_segments"), list):
        raise _error(
            "HOLDOUT_RESULT_INVALID",
            f"{item['candidate_id']} provider segment surface is invalid",
        )
    return value


def _provider_receipt_path(result_dir: Path, candidate_id: str) -> Path:
    return result_dir / "items" / candidate_id / "provider-result.json"


def _load_provider_receipt(
    path: Path,
    *,
    generation: Mapping[str, Any],
    item: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = _verify_seal(
        _load_json(
            path,
            label=f"{item['candidate_id']} provider result",
            max_bytes=MAX_RESULT_BYTES,
        ),
        field="receipt_sha256",
        label=f"{item['candidate_id']} provider result",
    )
    evidence = receipt.get("evidence")
    if (
        receipt.get("schema_version") != RESULT_SCHEMA
        or receipt.get("candidate_id") != item["candidate_id"]
        or receipt.get("generation_manifest_sha256") != generation["manifest_sha256"]
        or receipt.get("input_audio_sha256") != item["audio"]["sha256"]
        or receipt.get("request_id") != item["request_id"]
        or not isinstance(evidence, dict)
        or receipt.get("evidence_sha256") != _digest(evidence)
    ):
        raise _error("HOLDOUT_RESULT_BINDING_MISMATCH", "provider result binding differs")
    _validate_provider_evidence(evidence, item=item)
    return receipt


def _persist_provider_result(
    *,
    result_dir: Path,
    generation: Mapping[str, Any],
    item: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> Path:
    item_dir = _ensure_output_directory(result_dir / "items" / item["candidate_id"])
    receipt = _seal(
        {
            "schema_version": RESULT_SCHEMA,
            "created_at_utc": _now(),
            "candidate_id": item["candidate_id"],
            "generation_manifest_sha256": generation["manifest_sha256"],
            "input_audio_sha256": item["audio"]["sha256"],
            "request_id": item["request_id"],
            "evidence": dict(evidence),
            "evidence_sha256": _digest(dict(evidence)),
            "truth_manifest_opened": False,
        },
        field="receipt_sha256",
    )
    path = item_dir / "provider-result.json"
    _write_json(path, receipt)
    if evidence.get("one_track_srt_eligible") is True:
        srt = _render_srt(evidence.get("native_segments", []))
        _atomic_write(item_dir / "doubao-native.srt", srt.encode("utf-8"))
    text = evidence.get("text")
    if isinstance(text, str):
        _atomic_write(item_dir / "doubao-native.txt", (text + "\n").encode("utf-8"))
    return path


def run_provider(
    *,
    generation_manifest_path: Path,
    result_dir: Path,
    execute_provider_calls: bool,
    candidate_ids: set[str] | None = None,
) -> dict[str, Any]:
    generation = _load_generation_manifest(generation_manifest_path.absolute())
    result_dir = _ensure_output_directory(result_dir.absolute())
    selected = [
        item
        for item in generation["items"]
        if candidate_ids is None or item["candidate_id"] in candidate_ids
    ]
    if candidate_ids is not None and {item["candidate_id"] for item in selected} != candidate_ids:
        raise _error("HOLDOUT_CANDIDATE_UNKNOWN", "requested candidate is not in the cohort")
    if not selected:
        raise _error("HOLDOUT_CANDIDATE_UNKNOWN", "no provider items were selected")
    if not execute_provider_calls:
        report = _seal(
            {
                "schema_version": RUN_SCHEMA,
                "created_at_utc": _now(),
                "status": "REFUSED_EXPLICIT_PROVIDER_FLAG_REQUIRED",
                "generation_manifest_sha256": generation["manifest_sha256"],
                "truth_manifest_opened": False,
                "provider_calls": 0,
                "items": [],
            },
            field="report_sha256",
        )
        _write_json(result_dir / "run-summary.json", report)
        return report
    credential = doubao_api_key_status()
    ledger_root = _ensure_output_directory(result_dir / "dispatch-ledgers")
    item_reports: list[dict[str, Any]] = []
    provider_calls = 0
    for item in selected:
        candidate_id = item["candidate_id"]
        audio_path = _validate_hashed_file(
            item["audio"], label=f"{candidate_id} audio", max_bytes=MAX_AUDIO_BYTES
        )
        binding = _item_binding(generation, item)
        ledger = ContentBoundRequestLedger(ledger_root, binding)
        snapshot = ledger.prepare(request_id=item["request_id"])
        receipt_path = _provider_receipt_path(result_dir, candidate_id)
        if snapshot["state"] == "COMPLETED":
            receipt = _load_provider_receipt(
                receipt_path, generation=generation, item=item
            )
            item_reports.append(
                {
                    "candidate_id": candidate_id,
                    "status": "CACHED_COMPLETE",
                    "request_id": item["request_id"],
                    "receipt_sha256": receipt["receipt_sha256"],
                    "provider_call": False,
                }
            )
            continue
        if snapshot["state"] != "PREPARED":
            item_reports.append(
                {
                    "candidate_id": candidate_id,
                    "status": "BLOCKED_NO_AUTOMATIC_RESUBMIT",
                    "reason_code": "DOUBAO_FLASH_PRIOR_DISPATCH_NOT_RESUBMITTED",
                    "ledger_state": snapshot["state"],
                    "request_id": item["request_id"],
                    "provider_call": False,
                }
            )
            continue
        if credential["configured"] is not True:
            item_reports.append(
                {
                    "candidate_id": candidate_id,
                    "status": "BLOCKED_NO_CREDENTIALS",
                    "reason_code": credential["reason_code"],
                    "request_id": item["request_id"],
                    "provider_call": False,
                }
            )
            continue

        dispatched = False

        def mark_dispatch() -> None:
            nonlocal dispatched, provider_calls
            ledger.transition("SUBMIT_DISPATCHED", event="FLASH_DISPATCH_INTENT_PERSISTED")
            dispatched = True
            provider_calls += 1

        try:
            evidence = transcribe_doubao_flash_evidence(
                audio_path,
                duration_ms=item["duration_ms"],
                before_request=mark_dispatch,
                request_id=item["request_id"],
                expected_audio_sha256=item["audio"]["sha256"],
            )
        except DoubaoTranscriptionError as exc:
            if dispatched:
                if exc.reason_code in {"DOUBAO_TIMEOUT", "DOUBAO_TRANSPORT_ERROR"}:
                    ledger.transition(
                        "SUBMIT_AMBIGUOUS",
                        event="FLASH_TRANSPORT_AMBIGUOUS",
                        reason_code=exc.reason_code,
                    )
                else:
                    ledger.transition(
                        "FAILED",
                        event="FLASH_RESPONSE_FAILED",
                        reason_code=exc.reason_code,
                        metadata={
                            key: value
                            for key, value in (exc.metadata or {}).items()
                            if key in {"http_status", "provider_status_code"}
                        },
                    )
            item_reports.append(
                {
                    "candidate_id": candidate_id,
                    "status": "PROVIDER_FAILED",
                    "reason_code": exc.reason_code,
                    "request_id": item["request_id"],
                    "provider_call": dispatched,
                    "ledger_state": ledger.snapshot()["state"],
                }
            )
            continue
        except RequestLedgerError as exc:
            raise _error(exc.reason_code, "provider dispatch ledger rejected the request") from None

        try:
            evidence = _validate_provider_evidence(evidence, item=item)
        except HoldoutError as exc:
            ledger.transition(
                "FAILED",
                event="FLASH_RESULT_BINDING_FAILED",
                reason_code=exc.reason_code,
            )
            raise
        receipt_path = _persist_provider_result(
            result_dir=result_dir,
            generation=generation,
            item=item,
            evidence=evidence,
        )
        receipt = _load_provider_receipt(receipt_path, generation=generation, item=item)
        ledger.transition(
            "RESULT_AVAILABLE",
            event="FLASH_RESULT_PERSISTED",
            metadata={
                "response_sha256": evidence.get("response_sha256"),
                "result_sha256": receipt["evidence_sha256"],
            },
        )
        ledger.transition("COMPLETED", event="FLASH_RESULT_VERIFIED")
        item_reports.append(
            {
                "candidate_id": candidate_id,
                "status": "COMPLETE",
                "request_id": item["request_id"],
                "receipt_sha256": receipt["receipt_sha256"],
                "provider_call": True,
            }
        )

    complete_states = {"COMPLETE", "CACHED_COMPLETE"}
    if all(row["status"] in complete_states for row in item_reports):
        status = "COMPLETE"
    elif any(row["status"] in complete_states for row in item_reports):
        status = "PARTIAL"
    else:
        status = "BLOCKED_OR_FAILED"
    report = _seal(
        {
            "schema_version": RUN_SCHEMA,
            "created_at_utc": _now(),
            "status": status,
            "generation_manifest_sha256": generation["manifest_sha256"],
            "cohort_sha256": generation["cohort_sha256"],
            "truth_manifest_opened": False,
            "credential": credential,
            "provider_calls": provider_calls,
            "automatic_resubmit_after_ambiguous_dispatch": False,
            "items": item_reports,
        },
        field="report_sha256",
    )
    _write_json(result_dir / "run-summary.json", report)
    return report


def _scorable_hypothesis(
    evidence: Mapping[str, Any],
    *,
    item: Mapping[str, Any],
) -> list[dict[str, Any]]:
    native_segments = evidence.get("native_segments")
    if not isinstance(native_segments, list) or not native_segments:
        return []
    hypothesis: list[dict[str, Any]] = []
    for index, segment in enumerate(native_segments, 1):
        if not isinstance(segment, dict):
            raise _error(
                "HOLDOUT_RESULT_INVALID",
                f"{item['candidate_id']} segment is invalid",
            )
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        text = segment.get("text")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
            or end_ms > item["duration_ms"] + ENCODER_DURATION_TOLERANCE_MS
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise _error(
                "HOLDOUT_RESULT_INVALID",
                f"{item['candidate_id']} segment timing/text is invalid",
            )
        hypothesis.append(
            {
                "n": index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
            }
        )
    return hypothesis


def _paired_sign_test(wins: int, losses: int) -> float | None:
    total = wins + losses
    if total == 0:
        return None
    tail = min(wins, losses)
    probability = sum(math.comb(total, value) for value in range(tail + 1)) / (2**total)
    return min(1.0, 2 * probability)


def score_results(
    *,
    generation_manifest_path: Path,
    truth_manifest_path: Path,
    result_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    generation = _load_generation_manifest(generation_manifest_path.absolute())
    truth = _load_truth_manifest(truth_manifest_path.absolute())
    if (
        truth.get("generation_manifest_sha256") != generation["manifest_sha256"]
        or truth.get("cohort_sha256") != generation["cohort_sha256"]
        or truth.get("item_count") != generation["item_count"]
    ):
        raise _error("HOLDOUT_TRUTH_BINDING_MISMATCH", "truth cohort differs from generation")
    truth_by_id = {
        item.get("candidate_id"): item
        for item in truth.get("items", [])
        if isinstance(item, dict)
    }
    if set(truth_by_id) != {item["candidate_id"] for item in generation["items"]}:
        raise _error("HOLDOUT_TRUTH_BINDING_MISMATCH", "truth candidates differ")

    rows: list[dict[str, Any]] = []
    cohort_bcut_total = 0
    cohort_reference_total = 0
    cohort_whole_bcut_total = 0
    bcut_total = 0
    doubao_total = 0
    reference_total = 0
    whole_bcut_total = 0
    whole_doubao_total = 0
    wins = ties = losses = 0
    scored_count = 0
    for item in generation["items"]:
        candidate_id = item["candidate_id"]
        truth_item = truth_by_id[candidate_id]
        reference_path = _validate_hashed_file(
            truth_item["reference"],
            label=f"{candidate_id} withheld reference",
            max_bytes=MAX_MANIFEST_BYTES,
        )
        bcut_path = _validate_hashed_file(
            item["bcut_baseline"],
            label=f"{candidate_id} BCUT transcript",
            max_bytes=MAX_MANIFEST_BYTES,
        )
        reference = _parse_srt(reference_path, label=f"{candidate_id} withheld reference")
        bcut = _parse_srt(bcut_path, label=f"{candidate_id} BCUT transcript")
        bcut_score = _score_cues(reference, bcut)
        cohort_reference_total += bcut_score["reference_chars"]
        cohort_bcut_total += bcut_score["d_keep_distance"]
        cohort_whole_bcut_total += bcut_score["whole_text_distance"]
        receipt_path = _provider_receipt_path(result_dir.absolute(), candidate_id)
        if not receipt_path.exists():
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "status": "NOT_SCORED_PROVIDER_RESULT_MISSING",
                    "bcut": bcut_score,
                }
            )
            continue
        receipt = _load_provider_receipt(
            receipt_path, generation=generation, item=item
        )
        evidence = receipt["evidence"]
        hypothesis = _scorable_hypothesis(evidence, item=item)
        if not hypothesis:
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "status": "NOT_SCORED_PROVIDER_TIMELINE_MISSING",
                    "provider_status": evidence.get("status"),
                    "bcut": bcut_score,
                }
            )
            continue
        doubao_score = _score_cues(reference, hypothesis)
        delta = doubao_score["d_keep_distance"] - bcut_score["d_keep_distance"]
        if delta < 0:
            paired = "DOUBAO_BETTER"
            wins += 1
        elif delta > 0:
            paired = "DOUBAO_WORSE"
            losses += 1
        else:
            paired = "TIE"
            ties += 1
        rows.append(
            {
                "candidate_id": candidate_id,
                "status": "SCORED",
                "provider_result_sha256": receipt["receipt_sha256"],
                "reference_sha256": truth_item["reference"]["sha256"],
                "bcut": bcut_score,
                "doubao": doubao_score,
                "doubao_minus_bcut_d_keep": delta,
                "paired_result": paired,
            }
        )
        scored_count += 1
        reference_total += bcut_score["reference_chars"]
        bcut_total += bcut_score["d_keep_distance"]
        doubao_total += doubao_score["d_keep_distance"]
        whole_bcut_total += bcut_score["whole_text_distance"]
        whole_doubao_total += doubao_score["whole_text_distance"]

    required_count = generation["item_count"]
    result_fraction = scored_count / required_count if required_count else 0.0
    relative_reduction = (
        (bcut_total - doubao_total) / bcut_total
        if scored_count == required_count and bcut_total > 0
        else None
    )
    win_fraction = wins / scored_count if scored_count else None
    loss_fraction = losses / scored_count if scored_count else None
    if scored_count != required_count:
        raw_verdict = "NOT_JUDGEABLE_INCOMPLETE_PROVIDER_RESULTS"
        pilot_advance_decision = "NOT_JUDGEABLE_INCOMPLETE_PROVIDER_RESULTS"
    elif doubao_total < bcut_total:
        raw_verdict = "DOUBAO_FLASH_RAW_ASR_IMPROVED_ON_THIS_FROZEN_RELEASE_REFERENCE"
        pilot_advance_decision = (
            "ADVANCE_TO_LARGER_UNTOUCHED_HOLDOUT_ONLY"
            if relative_reduction is not None
            and relative_reduction >= PILOT_DECISION_POLICY["minimum_relative_d_keep_reduction"]
            and win_fraction is not None
            and win_fraction >= PILOT_DECISION_POLICY["minimum_paired_win_fraction"]
            and loss_fraction is not None
            and loss_fraction <= PILOT_DECISION_POLICY["maximum_paired_loss_fraction"]
            and result_fraction >= PILOT_DECISION_POLICY["required_provider_result_fraction"]
            else "DO_NOT_ADVANCE_PILOT_THRESHOLD_NOT_MET"
        )
    elif doubao_total == bcut_total:
        raw_verdict = "DOUBAO_FLASH_RAW_ASR_TIED_BCUT_ON_THIS_FROZEN_RELEASE_REFERENCE"
        pilot_advance_decision = "DO_NOT_ADVANCE_PILOT_THRESHOLD_NOT_MET"
    else:
        raw_verdict = "DOUBAO_FLASH_RAW_ASR_WORSE_ON_THIS_FROZEN_RELEASE_REFERENCE"
        pilot_advance_decision = "DO_NOT_ADVANCE_PILOT_THRESHOLD_NOT_MET"
    aggregate = {
        "scored_items": scored_count,
        "required_items": required_count,
        "provider_result_fraction": round(result_fraction, 8),
        "cohort_reference_chars": cohort_reference_total,
        "cohort_bcut_d_keep_distance": cohort_bcut_total,
        "cohort_bcut_d_keep_ratio": round(cohort_bcut_total / cohort_reference_total, 8)
        if cohort_reference_total
        else None,
        "cohort_bcut_whole_text_distance": cohort_whole_bcut_total,
        "reference_chars": reference_total if scored_count else None,
        "bcut_d_keep_distance": bcut_total if scored_count else None,
        "doubao_d_keep_distance": doubao_total if scored_count else None,
        "bcut_d_keep_ratio": round(bcut_total / reference_total, 8)
        if reference_total
        else None,
        "doubao_d_keep_ratio": round(doubao_total / reference_total, 8)
        if reference_total
        else None,
        "doubao_minus_bcut_d_keep": (doubao_total - bcut_total)
        if scored_count
        else None,
        "relative_d_keep_reduction": round(relative_reduction, 8)
        if relative_reduction is not None
        else None,
        "bcut_whole_text_distance": whole_bcut_total if scored_count else None,
        "doubao_whole_text_distance": whole_doubao_total if scored_count else None,
        "paired_wins": wins,
        "paired_ties": ties,
        "paired_losses": losses,
        "paired_win_fraction": round(win_fraction, 8) if win_fraction is not None else None,
        "paired_loss_fraction": round(loss_fraction, 8) if loss_fraction is not None else None,
        "paired_sign_test_two_sided_p": _paired_sign_test(wins, losses),
        "raw_asr_verdict": raw_verdict,
        "pilot_advance_decision": pilot_advance_decision,
        "production_decision": "NOT_AUTHORIZED_EXPLORATORY_RAW_ASR_ONLY",
        "next_gate": (
            "A passing pilot may only advance to a larger untouched holdout. "
            "Production still requires a separate BCUT-grid + CPA end-to-end "
            "evidence-arm evaluation."
        ),
    }
    report = _seal(
        {
            "schema_version": SCORE_SCHEMA,
            "scored_at_utc": _now(),
            "generation_manifest_sha256": generation["manifest_sha256"],
            "truth_manifest_sha256": truth["manifest_sha256"],
            "cohort_sha256": generation["cohort_sha256"],
            "reference_kind": truth["reference_kind"],
            "reference_annotation_policy": REFERENCE_ANNOTATION_POLICY,
            "pilot_decision_policy": PILOT_DECISION_POLICY,
            "standard_acoustic_cer": False,
            "aggregate": aggregate,
            "items": rows,
        },
        field="report_sha256",
    )
    _write_json(output_path.absolute(), report)
    return report


def _default_history() -> Path:
    return ROOT / "data" / "flash-evaluation" / "historical-intent.json"


def _default_baseline_root() -> Path:
    return ROOT / "data" / "flash-evaluation" / "reviewed-baselines"


def _default_report_root() -> Path:
    return ROOT / "reports" / "flash-evaluation"


def _print_summary(value: Mapping[str, Any]) -> None:
    compact = {
        key: value[key]
        for key in (
            "schema_version",
            "status",
            "manifest_sha256",
            "report_sha256",
            "item_count",
            "provider_calls",
            "aggregate",
        )
        if key in value
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    report_root = _default_report_root()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    freeze_parser = commands.add_parser("freeze")
    freeze_parser.add_argument("--historical-intent", type=Path, default=_default_history())
    freeze_parser.add_argument("--baseline-root", type=Path, default=_default_baseline_root())
    freeze_parser.add_argument(
        "--generation-manifest",
        type=Path,
        default=report_root / "generation-manifest.json",
    )
    freeze_parser.add_argument(
        "--truth-manifest",
        type=Path,
        default=report_root / "withheld" / "truth-manifest.json",
    )
    preflight_parser = commands.add_parser("preflight")
    preflight_parser.add_argument(
        "--generation-manifest",
        type=Path,
        default=report_root / "generation-manifest.json",
    )
    preflight_parser.add_argument(
        "--output", type=Path, default=report_root / "preflight.json"
    )
    preflight_parser.add_argument("--probe-media", action="store_true")

    run_parser = commands.add_parser("run")
    run_parser.add_argument(
        "--generation-manifest",
        type=Path,
        default=report_root / "generation-manifest.json",
    )
    run_parser.add_argument("--result-dir", type=Path, default=report_root / "run")
    run_parser.add_argument("--execute-provider-calls", action="store_true")
    run_parser.add_argument("--candidate", action="append", default=[])

    score_parser = commands.add_parser("score")
    score_parser.add_argument(
        "--generation-manifest",
        type=Path,
        default=report_root / "generation-manifest.json",
    )
    score_parser.add_argument(
        "--truth-manifest",
        type=Path,
        default=report_root / "withheld" / "truth-manifest.json",
    )
    score_parser.add_argument("--result-dir", type=Path, default=report_root / "run")
    score_parser.add_argument(
        "--output", type=Path, default=report_root / "score.json"
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            generation, truth = freeze_manifests(
                historical_intent=args.historical_intent,
                baseline_root=args.baseline_root,
                generation_manifest_path=args.generation_manifest,
                truth_manifest_path=args.truth_manifest,
            )
            _print_summary(generation)
            print(
                json.dumps(
                    {
                        "withheld_truth_manifest_sha256": truth["manifest_sha256"],
                        "truth_path": str(args.truth_manifest.absolute()),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "preflight":
            report = preflight(
                generation_manifest_path=args.generation_manifest,
                output_path=args.output,
                probe_media=args.probe_media,
            )
            _print_summary(report)
            return 0 if report["status"] == "READY" else 3
        if args.command == "run":
            report = run_provider(
                generation_manifest_path=args.generation_manifest,
                result_dir=args.result_dir,
                execute_provider_calls=args.execute_provider_calls,
                candidate_ids=set(args.candidate) or None,
            )
            _print_summary(report)
            return 0 if report["status"] == "COMPLETE" else 4
        if args.command == "score":
            report = score_results(
                generation_manifest_path=args.generation_manifest,
                truth_manifest_path=args.truth_manifest,
                result_dir=args.result_dir,
                output_path=args.output,
            )
            _print_summary(report)
            return 0
    except HoldoutError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "reason_code": exc.reason_code, "message": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
