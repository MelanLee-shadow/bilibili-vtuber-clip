"""Repository-sealed retirement of rejected source-language text proposals.

A review package may preserve a chat/entity row as a media-boundary owner while
retiring only its attempted text surface when the frozen parent audit proves
that the proposal introduced an unwitnessed foreign-language surface.  This is
historical package compatibility, not an assertion that the retained subtitle
text is correct and never an authority for titles, chat quotations, or other
public copy.  The package receipt is never self-authorizing: candidate-specific
hashes, windows, and expected historical bytes live in a committed data
authority consumed by this generic validator.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

from src.autoslice.jingting_chunker import parse_srt_cues

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUTHORITY_PATH = (
    REPO_ROOT / "assets/lidousha/source_language_preservation_supersessions.v1.json"
)
AUTHORITY_SCHEMA_VERSION = "lidousha-source-language-preservation-supersessions.v1"
GENERIC_RECEIPT_SCHEMA_VERSION = "source-language-preservation-supersession.v1"
_RECEIPT_SCHEMA_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\.v[1-9][0-9]*")
_CANDIDATE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,96}")
STATUS = "SUPERSEDED_BY_REJECTED_UNPROVEN_FOREIGN_INTRODUCTION"
REASON_CODE = "FOREIGN_LANGUAGE_INTRODUCED_WITHOUT_SOURCE_WITNESS"
AUTHORITY_SCOPE = "HISTORICAL_PACKAGE_BOUNDARY_OWNER_RETENTION_ONLY"
PARENT_AUDIT_SCHEMA_VERSION = "source-language-preservation-audit.v1"
PARENT_AUDIT_STATUS = "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
PARENT_ROW_MODE = "final_review_context_adjudication"
PARENT_DECISION_AUTHORITY = "CPA_JUDGE"

_DYNAMIC_TOP_LEVEL = frozenset(
    {
        "source_subtitle_truth_audit",
        "frozen_boundary_owner_contract",
        "final_source_truth_owner_verification",
        "final_required_legacy_decision_count",
        "final_required_source_truth_owner_count",
        "final_required_redelivery_baseline_owner_count",
        "final_required_decision_count",
        "final_boundary_required_exclusion_count",
        "final_superseded_by_source_truth_count",
        "final_superseded_by_redelivery_baseline_count",
        "final_superseded_by_exact_final_cpa_count",
        "final_superseded_by_expected_value_canon_count",
        "final_projected_by_parallel_subtitle_count",
        "final_superseded_by_parallel_subtitle_count",
        "final_correction_gift_supersession_count",
        "final_hard_meme_surface_verification",
        "final_japanese_native_script_verification",
        "final_outside_delivery_count",
        "final_redelivery_baseline_owner_verification",
        "final_superseded_by_correction_pass_count",
        "final_verification_failure",
    }
)
_ROW_GROUPS = (
    "applied",
    "sender_repairs",
    "gift_repairs",
    "coreference_repairs",
    "entity_repairs",
)
_REQUIRED_STRING_FIELDS = (
    "candidate_id",
    "receipt_schema_version",
    "status",
    "parent_chat_preimage_path",
    "parent_chat_preimage_sha256",
    "parent_chat_projected_sha256",
    "final_srt_sha256",
    "draft_text",
    "attempted_text",
    "reason_code",
)
_REQUIRED_INT_FIELDS = (
    "entity_row_index",
    "cue_index",
    "final_cue_index",
    "padded_start_ms",
    "padded_end_ms",
    "delivery_start_ms",
)
_REQUIRED_BOOLEAN_FIELDS = {
    "boundary_owner_retention_authorized": True,
    "subtitle_text_truth_authorized": False,
    "public_text_authorized": False,
}


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: object) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def project_historical_preimage(value: Mapping[str, object]) -> dict[str, object]:
    """Remove only fields produced after the immutable parent audit."""

    projected = copy.deepcopy(dict(value))
    for key in _DYNAMIC_TOP_LEVEL:
        projected.pop(key, None)
    for group in _ROW_GROUPS:
        rows = projected.get(group)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in list(row):
                if (
                    key
                    in {
                        "boundary_required",
                        "boundary_owner_id",
                        "boundary_owner_rejection",
                        "reconciliation",
                    }
                    or key.startswith("final_")
                    or key.startswith("survived_")
                ):
                    row.pop(key, None)
    return projected


def _resolved_regular_nonsymlink_file(path: Path) -> Path | None:
    """Resolve one existing regular file while rejecting any symlink component."""

    try:
        if path.is_symlink() or not path.is_file():
            return None
        absolute = path.absolute()
        resolved = path.resolve(strict=True)
        if resolved != absolute:
            return None
        return resolved
    except OSError:
        return None


def _receipt_schema_name_valid(value: object) -> bool:
    """Accept only versioned receipt names stored in the sealed authority.

    The name itself is not an authorization.  Routing is candidate-bound via
    :func:`receipt_routes_to_source_language_supersession` below.
    """

    return isinstance(value, str) and _RECEIPT_SCHEMA_RE.fullmatch(value) is not None


def receipt_routes_to_source_language_supersession(
    value: object,
    *,
    authority_path: Path = DEFAULT_AUTHORITY_PATH,
) -> bool:
    """Return true only when a receipt exactly matches its sealed candidate entry."""

    try:
        if not isinstance(value, Mapping):
            return False
        candidate_id = value.get("candidate_id")
        schema_version = value.get("schema_version")
        if not isinstance(candidate_id, str) or not candidate_id:
            return False
        if not _receipt_schema_name_valid(schema_version):
            return False
        authority = load_authority_entry(candidate_id, authority_path=authority_path)
        return schema_version == authority["receipt_schema_version"]
    except (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _sha_field_valid(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _validated_authority_entry(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_ENTRY_NOT_OBJECT")
    entry = dict(value)
    for field in _REQUIRED_STRING_FIELDS:
        if not isinstance(entry.get(field), str) or not str(entry[field]):
            raise ValueError(f"SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_FIELD_INVALID:{field}")
    for field in _REQUIRED_INT_FIELDS:
        field_value = entry.get(field)
        if isinstance(field_value, bool) or not isinstance(field_value, int) or field_value < 0:
            raise ValueError(f"SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_FIELD_INVALID:{field}")
    for field, expected in _REQUIRED_BOOLEAN_FIELDS.items():
        if entry.get(field) is not expected:
            raise ValueError(f"SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_FIELD_INVALID:{field}")
    if _CANDIDATE_ID_RE.fullmatch(str(entry["candidate_id"])) is None:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_CANDIDATE_ID_INVALID")
    if entry.get("authority_scope") != AUTHORITY_SCOPE:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_SCOPE_INVALID")
    if not _receipt_schema_name_valid(entry["receipt_schema_version"]):
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_RECEIPT_SCHEMA_INVALID")
    if entry["status"] != STATUS or entry["reason_code"] != REASON_CODE:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_DECISION_INVALID")
    for field in (
        "parent_chat_preimage_sha256",
        "parent_chat_projected_sha256",
        "final_srt_sha256",
    ):
        if not _sha_field_valid(entry[field]):
            raise ValueError(f"SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_HASH_INVALID:{field}")
    parent_relative = Path(str(entry["parent_chat_preimage_path"]))
    if parent_relative.is_absolute() or ".." in parent_relative.parts or not parent_relative.parts:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_PARENT_PATH_INVALID")
    expected_parent_name = f"{entry['candidate_id']}.parent-chat-authority.json"
    if parent_relative.name != expected_parent_name:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_PARENT_PATH_CANDIDATE_MISMATCH")
    if entry["entity_row_index"] < 0 or entry["cue_index"] <= 0 or entry["final_cue_index"] <= 0:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_INDEX_INVALID")
    if entry["padded_end_ms"] <= entry["padded_start_ms"]:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_WINDOW_INVALID")
    if entry["padded_start_ms"] < entry["delivery_start_ms"]:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_DELIVERY_OFFSET_INVALID")
    if entry["draft_text"] == entry["attempted_text"]:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_TEXTS_IDENTICAL")
    return entry


def load_authority_entry(
    candidate_id: str,
    *,
    authority_path: Path = DEFAULT_AUTHORITY_PATH,
) -> dict[str, object]:
    resolved_authority_path = _resolved_regular_nonsymlink_file(authority_path)
    if resolved_authority_path is None:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_UNAVAILABLE")
    raw = resolved_authority_path.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_INVALID_JSON") from exc
    if (
        not isinstance(document, Mapping)
        or document.get("schema_version") != AUTHORITY_SCHEMA_VERSION
    ):
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_SCHEMA_INVALID")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_ENTRIES_INVALID")
    validated_entries = [_validated_authority_entry(entry) for entry in entries]
    candidate_ids = [str(entry["candidate_id"]) for entry in validated_entries]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_DUPLICATE_CANDIDATE")
    matches = [entry for entry in validated_entries if entry["candidate_id"] == candidate_id]
    if len(matches) != 1:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_NOT_UNIQUE")
    return matches[0]


def _parent_and_finding_valid(
    parent: Mapping[str, object],
    authority: Mapping[str, object],
) -> bool:
    rows = parent.get("entity_repairs")
    audit = parent.get("final_source_language_preservation_audit")
    row_index = int(authority["entity_row_index"])
    if not isinstance(rows, list) or len(rows) <= row_index:
        return False
    row = rows[row_index]
    findings = audit.get("unproven_foreign_introductions") if isinstance(audit, Mapping) else None
    expected_finding = {
        "attempted": authority["attempted_text"],
        "cue_index": authority["cue_index"],
        "draft": authority["draft_text"],
        "end_ms": authority["padded_end_ms"],
        "reason": REASON_CODE,
        "start_ms": authority["padded_start_ms"],
    }
    return bool(
        isinstance(row, Mapping)
        and row.get("matched_start_ms") == authority["padded_start_ms"]
        and row.get("matched_end_ms") == authority["padded_end_ms"]
        and row.get("before") == [authority["draft_text"]]
        and row.get("after") == [authority["attempted_text"]]
        and row.get("mode") == PARENT_ROW_MODE
        and row.get("decision_authority") == PARENT_DECISION_AUTHORITY
        and isinstance(audit, Mapping)
        and audit.get("schema_version") == PARENT_AUDIT_SCHEMA_VERSION
        and audit.get("status") == PARENT_AUDIT_STATUS
        and findings == [expected_finding]
    )


def _final_srt_valid(path: Path, authority: Mapping[str, object]) -> bool:
    resolved = _resolved_regular_nonsymlink_file(path)
    if resolved is None:
        return False
    raw = resolved.read_bytes()
    if _sha256_bytes(raw) != authority["final_srt_sha256"]:
        return False
    cues = parse_srt_cues(raw.decode("utf-8"))
    final_cue_index = int(authority["final_cue_index"])
    if len(cues) < final_cue_index:
        return False
    cue = cues[final_cue_index - 1]
    text = raw.decode("utf-8")
    return bool(
        cue.start_ms == int(authority["padded_start_ms"]) - int(authority["delivery_start_ms"])
        and cue.end_ms == int(authority["padded_end_ms"]) - int(authority["delivery_start_ms"])
        and cue.text == authority["draft_text"]
        and str(authority["attempted_text"]) not in text
    )


def build_reconciliation(
    *,
    chat_authority: Mapping[str, object],
    parent_chat_path: Path,
    final_srt_path: Path,
    authority: Mapping[str, object],
) -> dict[str, object]:
    entry = _validated_authority_entry(authority)
    resolved_parent_path = _resolved_regular_nonsymlink_file(parent_chat_path)
    if resolved_parent_path is None:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_PARENT_CHAT_UNAVAILABLE")
    parent_raw = resolved_parent_path.read_bytes()
    if _sha256_bytes(parent_raw) != entry["parent_chat_preimage_sha256"]:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_PARENT_CHAT_DRIFT")
    parent = json.loads(parent_raw)
    if not isinstance(parent, dict) or not _parent_and_finding_valid(parent, entry):
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_PARENT_EVIDENCE_INVALID")
    parent_projected_sha256 = _canonical_sha256(project_historical_preimage(parent))
    if parent_projected_sha256 != entry["parent_chat_projected_sha256"]:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_PARENT_PROJECTION_DRIFT")
    if _canonical_sha256(project_historical_preimage(chat_authority)) != parent_projected_sha256:
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_CHAT_HISTORICAL_PREIMAGE_DRIFT")
    if not _final_srt_valid(final_srt_path, entry):
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_FINAL_SRT_DRIFT")
    return {
        "schema_version": entry["receipt_schema_version"],
        "status": entry["status"],
        "candidate_id": entry["candidate_id"],
        "authority_scope": entry["authority_scope"],
        "boundary_owner_retention_authorized": entry["boundary_owner_retention_authorized"],
        "subtitle_text_truth_authorized": entry["subtitle_text_truth_authorized"],
        "public_text_authorized": entry["public_text_authorized"],
        "parent_chat_preimage_path": entry["parent_chat_preimage_path"],
        "parent_chat_preimage_sha256": entry["parent_chat_preimage_sha256"],
        "parent_chat_projected_sha256": entry["parent_chat_projected_sha256"],
        "final_srt_sha256": entry["final_srt_sha256"],
        "entity_row_index": entry["entity_row_index"],
        "cue_index": entry["cue_index"],
        "final_cue_index": entry["final_cue_index"],
        "padded_start_ms": entry["padded_start_ms"],
        "padded_end_ms": entry["padded_end_ms"],
        "delivery_start_ms": entry["delivery_start_ms"],
        "draft_text": entry["draft_text"],
        "attempted_text": entry["attempted_text"],
        "reason_code": entry["reason_code"],
    }


def register_reconciliation(
    *,
    chat_authority: dict[str, object],
    parent_chat_path: Path,
    final_srt_path: Path,
    authority: Mapping[str, object],
) -> dict[str, object]:
    entry = _validated_authority_entry(authority)
    rows = chat_authority.get("entity_repairs")
    row_index = int(entry["entity_row_index"])
    if (
        not isinstance(rows, list)
        or len(rows) <= row_index
        or not isinstance(rows[row_index], dict)
    ):
        raise ValueError("SOURCE_LANGUAGE_SUPERSESSION_ENTITY_ROW_MISSING")
    receipt = build_reconciliation(
        chat_authority=chat_authority,
        parent_chat_path=parent_chat_path,
        final_srt_path=final_srt_path,
        authority=entry,
    )
    rows[row_index]["reconciliation"] = receipt
    return receipt


def reconciled_boundary_owner_valid(
    *,
    row: Mapping[str, object],
    row_index: int,
    chat_authority: Mapping[str, object],
    chat_authority_path: Path | None,
    authority_path: Path = DEFAULT_AUTHORITY_PATH,
) -> bool:
    """Validate one package row against the repository-sealed authority."""

    try:
        if chat_authority_path is None:
            return False
        receipt = row.get("reconciliation")
        if not receipt_routes_to_source_language_supersession(
            receipt, authority_path=authority_path
        ):
            return False
        candidate_id = receipt.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            return False
        authority = load_authority_entry(candidate_id, authority_path=authority_path)
        if row_index != authority["entity_row_index"]:
            return False
        if chat_authority.get("candidate_id") != candidate_id:
            return False
        resolved_chat_path = _resolved_regular_nonsymlink_file(chat_authority_path)
        if resolved_chat_path is None:
            return False
        if resolved_chat_path.name != f"{candidate_id}.chat-authority.json":
            return False
        root = resolved_chat_path.parent
        parent_relative = Path(str(authority["parent_chat_preimage_path"]))
        parent_candidate = root / parent_relative
        parent_path = _resolved_regular_nonsymlink_file(parent_candidate)
        if parent_path is None or root not in parent_path.parents:
            return False
        final_srt_path = root / f"{candidate_id}.srt"
        expected = build_reconciliation(
            chat_authority=chat_authority,
            parent_chat_path=parent_path,
            final_srt_path=final_srt_path,
            authority=authority,
        )
        if dict(receipt) != expected:
            return False
        rows = chat_authority.get("entity_repairs")
        parent = json.loads(parent_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or not isinstance(parent, dict):
            return False
        parent_rows = parent.get("entity_repairs")
        if not isinstance(parent_rows, list) or len(parent_rows) <= row_index:
            return False
        current_projected = project_historical_preimage({"entity_repairs": [dict(row)]})[
            "entity_repairs"
        ][0]
        parent_projected = project_historical_preimage(
            {"entity_repairs": [parent_rows[row_index]]}
        )["entity_repairs"][0]
        expected_owner_id = (
            f"entity_repair:{row_index + 1}:"
            f"{authority['padded_start_ms']}:{authority['padded_end_ms']}"
        )
        return bool(
            current_projected == parent_projected
            and row.get("boundary_required") is True
            and row.get("boundary_owner_id") == expected_owner_id
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False
