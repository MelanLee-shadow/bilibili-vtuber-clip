"""Candidate-scoped subtitle truth gates.

These gates turn corrections learned from Ivan's review into deterministic
delivery invariants.  They are intentionally evaluated on both final subtitle
surfaces, after text finalization and speaker rendering but before subtitle
burning or delivery.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.autoslice.chat_authority import normalize_chat_text
from src.autoslice.jingting_chunker import parse_srt_cues


SCHEMA_VERSION = "lidousha-subtitle-regression.v1"
AUDIT_SCHEMA_VERSION = "lidousha-subtitle-regression-audit.v1"
_SPEAKER_LABEL = re.compile(r"^\[(?:李豆沙|连线)\]\s*")


class SubtitleRegressionError(ValueError):
    """The truth asset itself is malformed or bound to another candidate."""


def _string_list(document: Mapping[str, Any], key: str, *, required: bool) -> tuple[str, ...]:
    value = document.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or (required and not value):
        qualifier = "non-empty " if required else ""
        raise SubtitleRegressionError(f"{key} must be a {qualifier}list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise SubtitleRegressionError(f"{key} entries must be non-empty strings")
    normalized = tuple(normalize_chat_text(item) for item in value)
    if any(not item for item in normalized):
        raise SubtitleRegressionError(f"{key} entries must contain subtitle text")
    if len(set(normalized)) != len(normalized):
        raise SubtitleRegressionError(f"{key} contains duplicate normalized entries")
    return tuple(str(item) for item in value)


def load_subtitle_regression_document(
    path: Path,
    *,
    candidate_id: str,
) -> tuple[dict[str, Any], str]:
    """Load one immutable candidate truth asset and return its byte hash."""

    if path.is_symlink() or not path.is_file():
        raise SubtitleRegressionError("subtitle regression asset must be a regular non-symlink file")
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubtitleRegressionError(f"invalid subtitle regression JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SubtitleRegressionError("subtitle regression document must be an object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise SubtitleRegressionError("unsupported subtitle regression schema_version")
    if document.get("candidate_id") != candidate_id:
        raise SubtitleRegressionError("subtitle regression candidate_id mismatch")

    required = _string_list(document, "required_payload_substrings", required=True)
    forbidden = _string_list(document, "forbidden_payload_substrings", required=False)
    forbidden_exact = _string_list(document, "forbidden_exact_cues", required=False)
    required_norm = {normalize_chat_text(item) for item in required}
    forbidden_norm = {normalize_chat_text(item) for item in forbidden}
    if required_norm & forbidden_norm:
        raise SubtitleRegressionError("the same normalized text cannot be both required and forbidden")

    normalized_document = dict(document)
    normalized_document["required_payload_substrings"] = list(required)
    normalized_document["forbidden_payload_substrings"] = list(forbidden)
    normalized_document["forbidden_exact_cues"] = list(forbidden_exact)
    return normalized_document, hashlib.sha256(raw).hexdigest()


def _surface_payload(srt_text: str) -> tuple[str, set[str]]:
    cue_payloads: list[str] = []
    for cue in parse_srt_cues(srt_text):
        text = _SPEAKER_LABEL.sub("", cue.text).strip()
        if text:
            cue_payloads.append(normalize_chat_text(text))
    return normalize_chat_text("".join(cue_payloads)), set(cue_payloads)


def _surface_audit(
    srt_text: str,
    *,
    required: Sequence[str],
    forbidden: Sequence[str],
    forbidden_exact: Sequence[str],
) -> dict[str, Any]:
    payload, exact_cues = _surface_payload(srt_text)
    missing_required = [item for item in required if normalize_chat_text(item) not in payload]
    found_forbidden = [item for item in forbidden if normalize_chat_text(item) in payload]
    found_forbidden_exact = [
        item for item in forbidden_exact if normalize_chat_text(item) in exact_cues
    ]
    return {
        "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "required_count": len(required),
        "missing_required": missing_required,
        "found_forbidden": found_forbidden,
        "found_forbidden_exact_cues": found_forbidden_exact,
        "status": (
            "PASS"
            if not missing_required and not found_forbidden and not found_forbidden_exact
            else "FAIL"
        ),
    }


def verify_subtitle_regression_surfaces(
    document_path: Path,
    *,
    candidate_id: str,
    final_text_srt: str,
    final_speaker_srt: str,
) -> dict[str, Any]:
    """Return a hash-bound audit for both delivery subtitle surfaces."""

    document, document_sha256 = load_subtitle_regression_document(
        document_path,
        candidate_id=candidate_id,
    )
    required = document["required_payload_substrings"]
    forbidden = document["forbidden_payload_substrings"]
    forbidden_exact = document["forbidden_exact_cues"]
    surfaces = {
        "final_text_srt": _surface_audit(
            final_text_srt,
            required=required,
            forbidden=forbidden,
            forbidden_exact=forbidden_exact,
        ),
        "final_speaker_srt": _surface_audit(
            final_speaker_srt,
            required=required,
            forbidden=forbidden,
            forbidden_exact=forbidden_exact,
        ),
    }
    status = "PASS" if all(row["status"] == "PASS" for row in surfaces.values()) else "FAIL"
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "status": status,
        "candidate_id": candidate_id,
        "truth_asset_path": str(document_path),
        "truth_asset_sha256": document_sha256,
        "final_text_srt_sha256": hashlib.sha256(final_text_srt.encode("utf-8")).hexdigest(),
        "final_speaker_srt_sha256": hashlib.sha256(final_speaker_srt.encode("utf-8")).hexdigest(),
        "surfaces": surfaces,
    }
