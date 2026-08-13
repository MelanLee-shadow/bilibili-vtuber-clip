"""Leaf validation for candidate-local subtitle text override headers."""

from __future__ import annotations

import re


TEXT_OVERRIDE_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4})
NO_UPLOAD_TEXT_OVERRIDE_SCHEMA_VERSION = 4
NO_UPLOAD_TEXT_OVERRIDE_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "upload",
        "source_cue_witness_sha256",
        "decision_output_witness_sha256",
        "overrides",
    }
)
_CANDIDATE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,96}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def validate_text_override_document_header(
    document: object,
    *,
    expected_candidate_id: str | None,
) -> int:
    """Validate version, candidate binding, and no-upload schema authority."""

    if not isinstance(document, dict):
        raise ValueError("text override document must be a JSON object")
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version not in TEXT_OVERRIDE_SCHEMA_VERSIONS:
        raise ValueError(
            "text override schema_version must be 1, 2, 3, or 4; "
            f"got {schema_version!r}"
        )
    candidate_id = document.get("candidate_id")
    candidate_required = (
        expected_candidate_id is not None
        or schema_version == NO_UPLOAD_TEXT_OVERRIDE_SCHEMA_VERSION
    )
    if candidate_required and (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID_RE.fullmatch(candidate_id) is None
    ):
        raise ValueError("text override candidate_id is missing or unsafe")
    if candidate_id is not None and (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID_RE.fullmatch(candidate_id) is None
    ):
        raise ValueError("text override candidate_id is unsafe")
    if expected_candidate_id is not None and candidate_id != expected_candidate_id:
        raise ValueError(
            "text override candidate_id mismatch: "
            f"expected {expected_candidate_id!r}, got {candidate_id!r}"
        )
    if schema_version == NO_UPLOAD_TEXT_OVERRIDE_SCHEMA_VERSION:
        if set(document) != NO_UPLOAD_TEXT_OVERRIDE_FIELDS:
            raise ValueError(
                "no-upload text override schema v4 requires exact top-level fields: "
                f"{sorted(NO_UPLOAD_TEXT_OVERRIDE_FIELDS)!r}"
            )
        if document.get("upload") is not False:
            raise ValueError("no-upload text override schema v4 requires upload=false")
        for field in ("source_cue_witness_sha256", "decision_output_witness_sha256"):
            if _SHA256_RE.fullmatch(str(document.get(field) or "")) is None:
                raise ValueError(f"no-upload text override schema v4 has invalid {field}")
        if not isinstance(document.get("overrides"), list) or not document["overrides"]:
            raise ValueError("no-upload text override schema v4 requires override decisions")
    elif "upload" in document:
        raise ValueError("text override upload authority is valid only in no-upload schema v4")
    return schema_version
