"""Validation of the sealed current-terminal ASS-repair receipt."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


def validate_current_ass_repair(
    *,
    authority: Mapping[str, Any],
    artifacts: Mapping[str, tuple[Path, str, int, str, str]],
    record: Mapping[str, object],
    load_object: Callable[..., Mapping[str, object]],
    sha256: Callable[[Path], str],
    normal_sha: Callable[..., str],
) -> None:
    """Bind the repaired record to its sealed-runtime ASS receipt."""

    receipt = load_object(artifacts["ass_repair_receipt"][0], label="ASS repair receipt")
    drift = authority["source_drift"]
    if (
        sha256(artifacts["ass_repair_receipt"][0]) != drift["ass_repair_receipt_sha256"]
        or receipt.get("schema_version") != "qixi-delivery-record-ass-binding-recovery.v1"
        or receipt.get("mode") != "APPLIED"
        or receipt.get("candidate_id") != authority["candidate_id"]
        or receipt.get("allowed_json_pointers") != ["/artifact_hashes/ass_sha256", "/subtitle_ass_path"]
    ):
        raise ValueError("current ASS repair receipt is invalid")
    postimage = receipt.get("postimage")
    evidence = receipt.get("evidence_descriptors")
    evidence_rows = {
        name: evidence.get(name) if isinstance(evidence, Mapping) else None
        for name in ("ass", "subtitle", "burned", "correction")
    }
    if (
        not isinstance(postimage, Mapping)
        or not isinstance(evidence, Mapping)
        or not all(isinstance(row, Mapping) for row in evidence_rows.values())
        or (
            normal_sha(postimage.get("sha256"), label="ASS repair postimage") != artifacts["record"][1]
            or postimage.get("bytes") != artifacts["record"][2]
            or normal_sha(evidence_rows["ass"].get("sha256"), label="ASS repair evidence") != artifacts["ass"][1]
            or normal_sha(evidence_rows["subtitle"].get("sha256"), label="ASS repair evidence") != artifacts["subtitle"][1]
            or normal_sha(evidence_rows["burned"].get("sha256"), label="ASS repair evidence") != artifacts["video"][1]
            or normal_sha(evidence_rows["correction"].get("sha256"), label="ASS repair evidence") != artifacts["correction"][1]
        )
    ):
        raise ValueError("current ASS repair receipt evidence drifts")
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, Mapping) or normal_sha(hashes.get("ass_sha256"), label="current record ASS") != artifacts["ass"][1]:
        raise ValueError("current ASS repair record binding drifts")
