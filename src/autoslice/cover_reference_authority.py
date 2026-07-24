"""Hash-bound, candidate-scoped authority for real-frame cover composition."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


SCHEMA_VERSION = "lidousha-cover-reference-overrides.v1"
SOURCE_VISUAL_VERIFICATION_SCHEMA = "lidousha-cover-source-visual-verification.v1"
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}")
_CANDIDATE_RX = re.compile(r"[A-Za-z0-9_-]{1,96}")
_RECUT_SUFFIX_RX = re.compile(r"r\d+$")
_SOURCE_VISUAL_CONTRACT_FIELDS = (
    "source_visible_claims",
    "narrative_presentation",
    "source_visual_verification",
)


class CoverReferenceAuthorityError(ValueError):
    """A claimed dual/real-frame reference is ambiguous or not hash-bound."""


def _candidate_family(value: str) -> str:
    return _RECUT_SUFFIX_RX.sub("", str(value or "").strip())


def _is_unique_nonempty_string_list(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if not all(isinstance(item, str) and item == item.strip() and bool(item) for item in value):
        return False
    return len(value) == len(set(value))


def _source_visual_claim_contract_is_valid(row: dict[str, object]) -> bool:
    """Validate source-pixel claims without upgrading narrative prose.

    Generic historical rows without ``relation_action`` remain compatible.
    Once any source-visual contract field is supplied, however, the complete
    contract is required.  A committed-style ``relation_action`` row always
    requires it: the free-form narrative is presentation guidance only, while
    only ``source_visible_claims`` may be mirrored by pixel verification.
    """

    relation_action_present = "relation_action" in row
    contract_field_present = any(field in row for field in _SOURCE_VISUAL_CONTRACT_FIELDS)
    if not relation_action_present and not contract_field_present:
        return True
    if not all(field in row for field in _SOURCE_VISUAL_CONTRACT_FIELDS):
        return False

    claims = row.get("source_visible_claims")
    narrative = row.get("narrative_presentation")
    verification = row.get("source_visual_verification")
    if (
        not _is_unique_nonempty_string_list(claims)
        or not isinstance(narrative, str)
        or not narrative.strip()
        or not isinstance(verification, dict)
    ):
        return False

    return (
        verification.get("schema_version") == SOURCE_VISUAL_VERIFICATION_SCHEMA
        and verification.get("status") == "PASS"
        and verification.get("reference_png_sha256") == row.get("reference_png_sha256")
        and verification.get("visible_participant_ids") == row.get("visible_participant_ids")
        and verification.get("verified_claims") == claims
        and isinstance(verification.get("authority"), str)
        and bool(str(verification["authority"]).strip())
    )


def load_candidate_cover_reference(
    candidate_id: str, *, ledger_path: Path
) -> dict[str, object] | None:
    """Return one validated override for a candidate family, if present.

    The ledger never authorizes a generated lookalike.  It points to an exact
    content/source timestamp and the expected PNG bytes extracted from the
    source-bound recut.  Runtime extraction must reproduce that hash before a
    dual-person screenshot route can be used.
    """

    try:
        raw = ledger_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CoverReferenceAuthorityError(
            f"COVER_REFERENCE_LEDGER_UNREADABLE:{ledger_path}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise CoverReferenceAuthorityError("COVER_REFERENCE_LEDGER_SCHEMA_INVALID")
    overrides = payload.get("overrides")
    if not isinstance(overrides, list):
        raise CoverReferenceAuthorityError("COVER_REFERENCE_LEDGER_ROWS_INVALID")

    family = _candidate_family(candidate_id)
    matches = [
        row
        for row in overrides
        if isinstance(row, dict)
        and _candidate_family(str(row.get("candidate_id") or "")) == family
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise CoverReferenceAuthorityError(
            f"COVER_REFERENCE_OVERRIDE_AMBIGUOUS:{candidate_id}"
        )
    row = dict(matches[0])
    row_candidate = str(row.get("candidate_id") or "")
    visible = row.get("visible_participant_ids")
    if (
        _CANDIDATE_RX.fullmatch(row_candidate) is None
        or not isinstance(row.get("content_time_ms"), int)
        or isinstance(row.get("content_time_ms"), bool)
        or int(row["content_time_ms"]) < 0
        or not isinstance(row.get("source_time_ms"), int)
        or isinstance(row.get("source_time_ms"), bool)
        or int(row["source_time_ms"]) < 0
        or _SHA256_RX.fullmatch(str(row.get("source_sha256") or "")) is None
        or _SHA256_RX.fullmatch(str(row.get("reference_png_sha256") or "")) is None
        or not isinstance(visible, list)
        or len(visible) < 2
        or len(visible) != len(set(visible))
        or not all(_CANDIDATE_RX.fullmatch(str(value or "")) for value in visible)
        or row.get("required_treatment")
        not in {"screenshot_direct", "screenshot_polish"}
        or not str(row.get("authority") or "").strip()
        or not _source_visual_claim_contract_is_valid(row)
    ):
        raise CoverReferenceAuthorityError(
            f"COVER_REFERENCE_OVERRIDE_INVALID:{candidate_id}"
        )
    row["ledger_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    return row
