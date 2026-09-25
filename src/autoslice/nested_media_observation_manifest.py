"""Hash-bound completeness contract for nested-media visual observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path


SCHEMA_VERSION = "nested-media-observation-manifest.v1"
COMPLETE_STATUS = "COMPLETE_OBSERVATION_SET"


class ObservationManifestError(ValueError):
    """The visual observation manifest is incomplete, unsafe, or drifted."""


def _normal_hash(value: object) -> str:
    if not isinstance(value, str):
        raise ObservationManifestError("OBSERVATION_MANIFEST_SHA256_INVALID")
    normalized = value.removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ObservationManifestError("OBSERVATION_MANIFEST_SHA256_INVALID")
    return "sha256:" + normalized


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _read_regular(path: str | Path, *, code: str) -> tuple[Path, bytes]:
    candidate = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise ObservationManifestError(code)
    try:
        resolved = candidate.resolve(strict=True)
        before = resolved.stat()
        raw = resolved.read_bytes()
        after = resolved.stat()
    except OSError as exc:
        raise ObservationManifestError(code) from exc
    if not resolved.is_file() or (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ObservationManifestError(code)
    if len(raw) != before.st_size:
        raise ObservationManifestError(code)
    return resolved, raw


def _evidence_descriptor(value: object, *, index: int) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "bytes"}:
        raise ObservationManifestError("OBSERVATION_SOURCE_EVIDENCE_INVALID")
    path, raw = _read_regular(
        str(value.get("path") or ""), code="OBSERVATION_SOURCE_EVIDENCE_UNAVAILABLE"
    )
    expected_hash = _normal_hash(value.get("sha256"))
    expected_bytes = value.get("bytes")
    if _sha256(raw) != expected_hash:
        raise ObservationManifestError("OBSERVATION_SOURCE_EVIDENCE_HASH_DRIFT")
    if type(expected_bytes) is not int or expected_bytes != len(raw):
        raise ObservationManifestError("OBSERVATION_SOURCE_EVIDENCE_SIZE_DRIFT")
    return {
        "path": str(path),
        "sha256": expected_hash,
        "bytes": expected_bytes,
        "ordinal": index,
    }


def load_complete_observation_manifest(
    path: str | Path,
    *,
    expected_sha256: str,
    candidate_id: str,
    list_key: str = "observations",
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Validate the exact observation set and every declared upstream file."""

    manifest_path, raw = _read_regular(
        path, code="OBSERVATION_MANIFEST_UNAVAILABLE"
    )
    observed_hash = _sha256(raw)
    if observed_hash != _normal_hash(expected_sha256):
        raise ObservationManifestError("OBSERVATION_MANIFEST_HASH_DRIFT")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ObservationManifestError("OBSERVATION_MANIFEST_INVALID") from exc
    if not isinstance(value, Mapping):
        raise ObservationManifestError("OBSERVATION_MANIFEST_INVALID")
    if not (
        value.get("schema_version") == SCHEMA_VERSION
        and value.get("status") == COMPLETE_STATUS
        and value.get("candidate_id") == candidate_id
        and isinstance(list_key, str)
        and list_key
    ):
        raise ObservationManifestError("OBSERVATION_MANIFEST_INCOMPLETE")
    raw_observations = value.get(list_key)
    expected_count = value.get("expected_observation_count")
    if not (
        isinstance(raw_observations, list)
        and raw_observations
        and all(isinstance(item, Mapping) for item in raw_observations)
        and type(expected_count) is int
        and expected_count == len(raw_observations)
    ):
        raise ObservationManifestError("OBSERVATION_MANIFEST_INCOMPLETE")
    observation_ids: list[str] = []
    observations: list[dict[str, object]] = []
    for raw_observation in raw_observations:
        observation = dict(raw_observation)
        observation_id = observation.get("observation_id")
        if not (
            isinstance(observation_id, str)
            and observation_id
            and observation_id not in observation_ids
            and observation.get("candidate_id") == candidate_id
        ):
            raise ObservationManifestError("OBSERVATION_MANIFEST_OBSERVATION_INVALID")
        observation_ids.append(observation_id)
        observations.append(observation)
    raw_evidence = value.get("source_evidence")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise ObservationManifestError("OBSERVATION_SOURCE_EVIDENCE_INVALID")
    evidence = [
        _evidence_descriptor(item, index=index)
        for index, item in enumerate(raw_evidence, start=1)
    ]
    descriptor = {
        "path": str(manifest_path),
        "sha256": observed_hash,
        "bytes": len(raw),
        "schema_version": SCHEMA_VERSION,
        "status": COMPLETE_STATUS,
        "list_key": list_key,
        "expected_observation_count": expected_count,
        "source_evidence": evidence,
    }
    return descriptor, observations
