"""Public compatibility boundary: private merged-content proofs are not shipped.

Ordinary publications have no coverage. A declared coverage or successor cannot
be validated here and is rejected, never silently converted into an empty proof.
This file provides no generic multi-candidate approval or adapter-registration API.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

SCHEMA_VERSION = "publication-content-coverage.v1"
_UNAVAILABLE = "PUBLICATION_CONTENT_COVERAGE_ADAPTER_UNAVAILABLE"
_MAX_RECORD_BYTES = 5_000_000


def validate_publication_content_coverage(publication: Mapping) -> list[str]:
    if not isinstance(publication, Mapping):
        raise ValueError("publication content coverage publication must be an object")
    if publication.get("content_coverage") is not None:
        raise ValueError(_UNAVAILABLE)
    return []


def _record(manifest: Mapping) -> dict | None:
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, Mapping) or "record" not in attestation:
        return None
    binding = attestation["record"]
    if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str):
        raise ValueError("publication content coverage record binding is invalid")
    path = Path(binding["path"])
    if not binding["path"] or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("publication content coverage record path is unsafe")
    # A small frozen record read is sufficient to recognize unsupported proof.
    # No private fixture, media, provider or implicit approval is consulted.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_RECORD_BYTES:
            raise ValueError("publication content coverage record is not bounded")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ValueError("publication content coverage record changed")
    finally:
        os.close(descriptor)
    if hashlib.sha256(raw).hexdigest() != binding.get("sha256"):
        raise ValueError("publication content coverage record hash drifted")
    size = binding.get("bytes")
    if type(size) is not int or size != len(raw):
        raise ValueError("publication content coverage record byte count drifted")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("publication content coverage record is not an object")
    return value


def derive_publication_content_coverage(manifest: Mapping, publication: Mapping) -> None:
    if not isinstance(manifest, Mapping) or not isinstance(publication, Mapping):
        raise ValueError("publication content coverage inputs must be objects")
    validate_publication_content_coverage(publication)
    validate_publication_content_coverage(manifest)
    if publication.get("status") == "VERIFIED_SAME_BV_COVER":
        return None  # Reconciliation still validates any inherited runtime coverage.
    record = _record(manifest)
    if record is None:
        return None
    validate_publication_content_coverage(record)
    story = record.get("story_contract")
    fact = story.get("source_fact_review") if isinstance(story, Mapping) else None
    if isinstance(fact, Mapping) and fact.get("final_review_successor") is not None:
        raise ValueError(_UNAVAILABLE)
    return None
