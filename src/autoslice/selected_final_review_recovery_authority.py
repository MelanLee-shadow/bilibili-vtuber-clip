"""Repository-sealed, one-CID authority for a v7 final-review recovery.

This is deliberately not a generic grant loader.  A checked-in authority can
only materialize the one transient v7 scope and first recovery receipt it
describes; it never grants upload or provider work.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from src.autoslice.operator_processing_scope import (
    _validate_grant,
    operator_scope_admission,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthority,
    require_repository_asset_authority,
)
from src.autoslice.selected_final_review_recovery import (
    build_selected_final_review_recovery_receipt,
    is_selected_final_review_rejection,
)


SCHEMA = "selected-final-review-recovery-authority.v1"
_ASSET_DIRECTORY = Path("assets/lidousha/selected_final_review_recovery_authorities")
_CID_RE = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "state_date",
        "source_ruling",
        "scope_template",
        "scope_predecessor",
        "receipt_contract",
        "expected_failure",
        "text_override",
        "permissions",
        "authority_sha256",
    }
)
_SOURCE_RULING_FIELDS = frozenset(
    {"path", "sha256", "commit", "quote", "user_event"}
)
_USER_EVENT_FIELDS = frozenset({"session_id", "uuid", "timestamp", "line_sha256"})
_SCOPE_TEMPLATE_FIELDS = frozenset(
    {
        "schema_version",
        "grant_id",
        "recording_date",
        "reason",
        "candidate_ids",
        "user_authorization",
        "intent",
        "upload_allowed",
    }
)
_SCOPE_PREDECESSOR_EXACT_FIELDS = frozenset({"mode", "scope", "sha256"})
_SCOPE_PREDECESSOR_ABSENT_FIELDS = frozenset({"mode"})
_SCOPE_PREDECESSOR_CONVERGED_FIELDS = frozenset(
    {"mode", "candidate_id", "relative_path", "authority_sha256", "scope_template_sha256"}
)
_RECEIPT_CONTRACT_FIELDS = frozenset(
    {"schema_version", "action", "transition_kind", "candidate_id"}
)
_EXPECTED_FAILURE_FIELDS = frozenset(
    {
        "failure_recovery_fingerprint",
        "reviewed_srt_sha256",
        "surface_files",
        "finding",
    }
)
_TEXT_OVERRIDE_FIELDS = frozenset(
    {
        "relative_path",
        "sha256",
        "source_cue_witness_sha256",
        "decision_output_witness_sha256",
    }
)
_PERMISSION_FIELDS = frozenset(
    {"upload_allowed", "provider_allowed", "allowed_operations"}
)
_REJECTION_FIELDS = {
    "status": "candidate_rejected",
    "rejected_status": "failed",
    "rc": 1,
    "selected_repair": True,
    "failure_kind": "subtitle_authority",
    "failure_stage": "final_review_findings",
    "failure_recoverable": False,
    "rejection_reason": "subtitle_authority_unresolved_backfilled",
}


class SelectedFinalReviewRecoveryAuthorityError(ValueError):
    """The sealed one-candidate recovery authority cannot be consumed."""


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _asset_relative(candidate_id: str) -> Path:
    if not isinstance(candidate_id, str) or _CID_RE.fullmatch(candidate_id) is None:
        raise SelectedFinalReviewRecoveryAuthorityError("candidate id is unsafe")
    return _ASSET_DIRECTORY / f"{candidate_id}.v1.json"


def _text_override_relative(candidate_id: str) -> Path:
    return Path("assets/lidousha/subtitle_text_overrides") / f"{candidate_id}.text.v1.json"


def _json_object(payload: bytes, *, label: str) -> dict[str, object]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectedFinalReviewRecoveryAuthorityError(f"{label} is unreadable") from exc
    if not isinstance(document, dict):
        raise SelectedFinalReviewRecoveryAuthorityError(f"{label} is not an object")
    return document


def _sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _require_mapping(value: object, *, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SelectedFinalReviewRecoveryAuthorityError(f"{label} has an invalid shape")
    return value


def _validate_authority(document: Mapping[str, object], *, candidate_id: str) -> None:
    if set(document) != _AUTHORITY_FIELDS or document.get("schema_version") != SCHEMA:
        raise SelectedFinalReviewRecoveryAuthorityError("authority schema is invalid")
    if document.get("candidate_id") != candidate_id:
        raise SelectedFinalReviewRecoveryAuthorityError("authority candidate id mismatches")
    body = {key: value for key, value in document.items() if key != "authority_sha256"}
    if document.get("authority_sha256") != _canonical_sha256(body):
        raise SelectedFinalReviewRecoveryAuthorityError("authority self-hash mismatches")
    ruling = _require_mapping(document.get("source_ruling"), fields=_SOURCE_RULING_FIELDS, label="source ruling")
    if (
        ruling.get("path") != "docs/reviews/2026-08-19-ivan-review-batch-rulings.md"
        or not _sha256(ruling.get("sha256"))
        or not isinstance(ruling.get("commit"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", ruling["commit"])
        or not isinstance(ruling.get("quote"), str)
        or len(ruling["quote"].strip()) < 8
    ):
        raise SelectedFinalReviewRecoveryAuthorityError("source ruling binding is invalid")
    event = _require_mapping(ruling.get("user_event"), fields=_USER_EVENT_FIELDS, label="source event")
    if not (
        all(
            isinstance(event.get(field), str)
            and re.fullmatch(r"[0-9a-f-]{36}", str(event[field]))
            for field in ("session_id", "uuid")
        )
        and isinstance(event.get("timestamp"), str)
        and event["timestamp"] == "2026-08-19T00:08:52.249Z"
        and event.get("line_sha256") == "e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa"
    ):
        raise SelectedFinalReviewRecoveryAuthorityError("source event binding is invalid")
    scope = _require_mapping(document.get("scope_template"), fields=_SCOPE_TEMPLATE_FIELDS, label="scope template")
    if not (
        scope.get("schema_version") == "operator-processing-scope-grant.v7"
        and scope.get("recording_date") == document.get("state_date")
        and scope.get("candidate_ids") == [candidate_id]
        and scope.get("intent") == "RECOVER_NAMED_SELECTED_FINAL_REVIEW_REJECTION"
        and scope.get("upload_allowed") is False
        and isinstance(scope.get("grant_id"), str)
        and isinstance(scope.get("reason"), str)
        and scope.get("user_authorization")
        == {"quote": ruling["quote"], "timestamp": event["timestamp"]}
    ):
        raise SelectedFinalReviewRecoveryAuthorityError("scope template is not strict v7")
    predecessor = document.get("scope_predecessor")
    if not isinstance(predecessor, Mapping):
        raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor is invalid")
    if predecessor.get("mode") == "exact_replace":
        if set(predecessor) != _SCOPE_PREDECESSOR_EXACT_FIELDS or not isinstance(
            predecessor.get("scope"), Mapping
        ):
            raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor is invalid")
        validated, _reason = _validate_grant(predecessor["scope"])
        if validated is None or predecessor.get("sha256") != _canonical_sha256(predecessor["scope"]):
            raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor is invalid")
    elif predecessor.get("mode") == "absent_or_expired":
        if set(predecessor) != _SCOPE_PREDECESSOR_ABSENT_FIELDS:
            raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor is invalid")
    elif predecessor.get("mode") == "exact_converged_final_review_v7":
        predecessor_candidate = predecessor.get("candidate_id")
        if not (
            set(predecessor) == _SCOPE_PREDECESSOR_CONVERGED_FIELDS
            and isinstance(predecessor_candidate, str)
            and predecessor_candidate != candidate_id
            and predecessor.get("relative_path")
            == _asset_relative(predecessor_candidate).as_posix()
            and _sha256(str(predecessor.get("authority_sha256")).removeprefix("sha256:"))
            and _sha256(str(predecessor.get("scope_template_sha256")).removeprefix("sha256:"))
        ):
            raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor is invalid")
    else:
        raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor is invalid")
    receipt = _require_mapping(document.get("receipt_contract"), fields=_RECEIPT_CONTRACT_FIELDS, label="receipt contract")
    if receipt != {
        "schema_version": "selected-final-review-recovery-receipt.v1",
        "action": "REQUEUED_BY_OPERATOR_FINAL_REVIEW_GRANT",
        "transition_kind": "REJECTION_TO_QUEUE",
        "candidate_id": candidate_id,
    }:
        raise SelectedFinalReviewRecoveryAuthorityError("receipt contract is invalid")
    failure = _require_mapping(document.get("expected_failure"), fields=_EXPECTED_FAILURE_FIELDS, label="expected failure")
    if not _sha256(str(failure.get("failure_recovery_fingerprint")).removeprefix("sha256:")) or not _sha256(str(failure.get("reviewed_srt_sha256")).removeprefix("sha256:")):
        raise SelectedFinalReviewRecoveryAuthorityError("expected failure hashes are invalid")
    if not isinstance(failure.get("surface_files"), list) or not isinstance(failure.get("finding"), Mapping):
        raise SelectedFinalReviewRecoveryAuthorityError("expected failure evidence is invalid")
    finding = failure["finding"]
    if not (
        finding.get("cue_index") in {4, 6}
        and isinstance(finding.get("proposed_full_cue"), str)
        and isinstance(finding.get("suspect"), str)
        and _sha256(finding.get("base_text_sha256"))
        and isinstance(finding.get("candidate_provenance"), Mapping)
        and finding["candidate_provenance"].get("mutation_authorized") is False
    ):
        raise SelectedFinalReviewRecoveryAuthorityError("exact finding is invalid")
    override = _require_mapping(document.get("text_override"), fields=_TEXT_OVERRIDE_FIELDS, label="text override")
    if (
        override.get("relative_path") != _text_override_relative(candidate_id).as_posix()
        or not all(_sha256(override.get(field)) for field in _TEXT_OVERRIDE_FIELDS if field != "relative_path")
    ):
        raise SelectedFinalReviewRecoveryAuthorityError("text override binding is invalid")
    permissions = _require_mapping(document.get("permissions"), fields=_PERMISSION_FIELDS, label="permissions")
    if permissions != {
        "upload_allowed": False,
        "provider_allowed": False,
        "allowed_operations": [
            "apply_exact_subtitle_text_override",
            "requeue_selected_final_review",
        ],
    }:
        raise SelectedFinalReviewRecoveryAuthorityError("authority permissions are invalid")


def load_selected_final_review_recovery_authority(
    *, repo_root: Path, candidate_id: str
) -> tuple[dict[str, object], RepositoryAssetAuthority]:
    """Load one sealed recovery capability and its exact override binding."""

    relative = _asset_relative(candidate_id)
    path = repo_root / relative
    if path.is_symlink() or not path.is_file():
        raise SelectedFinalReviewRecoveryAuthorityError("recovery authority is unavailable")
    payload = path.read_bytes()
    try:
        seal = require_repository_asset_authority(
            repo_root=repo_root, relative_path=relative, observed_bytes=payload
        )
    except Exception as exc:  # authority errors must not become an ambient fallback
        raise SelectedFinalReviewRecoveryAuthorityError("recovery authority is unsealed") from exc
    document = _json_object(payload, label="recovery authority")
    _validate_authority(document, candidate_id=candidate_id)
    override = document["text_override"]
    assert isinstance(override, Mapping)
    override_relative = Path(str(override["relative_path"]))
    override_path = repo_root / override_relative
    if override_path.is_symlink() or not override_path.is_file():
        raise SelectedFinalReviewRecoveryAuthorityError("text override is unavailable")
    observed = override_path.read_bytes()
    if hashlib.sha256(observed).hexdigest() != override["sha256"]:
        raise SelectedFinalReviewRecoveryAuthorityError("text override bytes drifted")
    try:
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=override_relative, observed_bytes=observed
        )
    except Exception as exc:
        raise SelectedFinalReviewRecoveryAuthorityError("text override is unsealed") from exc
    return document, seal


def materialize_selected_final_review_recovery_scope(
    authority: Mapping[str, object], *, expires_at: str
) -> dict[str, object]:
    """Add the one operational expiry to the source-event-sealed v7 template."""

    candidate_id = authority.get("candidate_id")
    if not isinstance(candidate_id, str):
        raise SelectedFinalReviewRecoveryAuthorityError("authority candidate id is invalid")
    _validate_authority(authority, candidate_id=candidate_id)
    scope = dict(authority["scope_template"])
    scope["expires_at"] = expires_at
    validated, _reason = _validate_grant(scope)
    if validated is None:
        raise SelectedFinalReviewRecoveryAuthorityError("materialized v7 scope is invalid")
    return scope


def validate_selected_final_review_scope_predecessor(
    authority: Mapping[str, object], *, repo_root: Path, state: Mapping[str, object], scope: Mapping[str, object], now: datetime | None = None
) -> None:
    """Admit only the sealed old grant, no grant, an expired grant, or re-entry.

    A recovery must never erase an unrelated active scope.  The #2 authority
    therefore names the exact v3 predecessor it may replace; the #3 authority
    may take over only after its exact receipt-bound v7 lineage converged.
    """

    candidate_id = authority.get("candidate_id")
    if not isinstance(candidate_id, str):
        raise SelectedFinalReviewRecoveryAuthorityError("authority candidate id is invalid")
    _validate_authority(authority, candidate_id=candidate_id)
    predecessor = authority["scope_predecessor"]
    assert isinstance(predecessor, Mapping)
    current = state.get("operator_processing_scope")
    if predecessor["mode"] == "exact_replace":
        expected = predecessor["scope"]
        if current != expected or predecessor["sha256"] != _canonical_sha256(expected):
            raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor mismatches")
        return
    if predecessor["mode"] == "exact_converged_final_review_v7":
        predecessor_candidate = str(predecessor["candidate_id"])
        try:
            predecessor_authority, _seal = load_selected_final_review_recovery_authority(
                repo_root=repo_root, candidate_id=predecessor_candidate
            )
        except SelectedFinalReviewRecoveryAuthorityError as exc:
            raise SelectedFinalReviewRecoveryAuthorityError(
                "scope predecessor conflicts"
            ) from exc
        if not (
            predecessor_authority.get("authority_sha256") == predecessor["authority_sha256"]
            and _canonical_sha256(predecessor_authority.get("scope_template"))
            == predecessor["scope_template_sha256"]
            and isinstance(current, Mapping)
        ):
            raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor conflicts")
        expected_scope = dict(predecessor_authority["scope_template"])
        expected_scope["expires_at"] = current.get("expires_at")
        if current != expected_scope:
            raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor conflicts")
        admission = operator_scope_admission(
            state, date=str(authority["state_date"]), now=now
        )
        if not (
            admission.reason_code == "CONVERGED"
            and admission.grant_id == expected_scope["grant_id"]
            and admission.candidate_ids == (predecessor_candidate,)
        ):
            raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor conflicts")
        return
    if current is None or current == scope:
        return
    validated, _reason = _validate_grant(current)
    if validated is None:
        raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor conflicts")
    expiry = validated.get("expires_at")
    try:
        expires_at = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor conflicts") from exc
    if expires_at.tzinfo is None:
        raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor conflicts")
    observed_now = now or datetime.now(timezone.utc)
    if expires_at.astimezone(timezone.utc) > observed_now.astimezone(timezone.utc):
        raise SelectedFinalReviewRecoveryAuthorityError("scope predecessor conflicts")


def _current_rejection(
    state: Mapping[str, object], *, candidate_id: str, expected_failure: Mapping[str, object]
) -> Mapping[str, object]:
    rows: list[Mapping[str, object]] = []
    for collection in (
        "pending_talk",
        "talk_backlog",
        "picks",
        "talk_below_confidence_threshold",
        "talk_superseded_attempts",
    ):
        values = state.get(collection)
        if not isinstance(values, list):
            continue
        rows.extend(
            row
            for row in values
            if isinstance(row, Mapping)
            and str(row.get("candidate_id") or row.get("cid") or "") == candidate_id
            and row.get("status") == "candidate_rejected"
            and is_selected_final_review_rejection(row)
            and all(row.get(key) == value for key, value in _REJECTION_FIELDS.items())
            and isinstance(row.get("failure_evidence"), Mapping)
            and row.get("failure_recovery_fingerprint")
            == expected_failure["failure_recovery_fingerprint"]
            and row["failure_evidence"].get("reviewed_srt_sha256")
            == expected_failure["reviewed_srt_sha256"]
            and row["failure_evidence"].get("surface_files")
            == expected_failure["surface_files"]
            and row["failure_evidence"].get("findings") == [expected_failure["finding"]]
        )
    if not rows:
        raise SelectedFinalReviewRecoveryAuthorityError("current rejection evidence mismatches")
    if len(rows) != 1:
        raise SelectedFinalReviewRecoveryAuthorityError("current rejection is not unique")
    return rows[0]


def validate_selected_final_review_recovery_rejection(
    authority: Mapping[str, object], *, state: Mapping[str, object], state_date: str
) -> Mapping[str, object]:
    """Bind the sealed authority to one unchanged current rejection row."""

    candidate_id = authority.get("candidate_id")
    if not isinstance(candidate_id, str):
        raise SelectedFinalReviewRecoveryAuthorityError("authority candidate id is invalid")
    _validate_authority(authority, candidate_id=candidate_id)
    if authority.get("state_date") != state_date:
        raise SelectedFinalReviewRecoveryAuthorityError("recovery state date mismatches")
    expected = authority["expected_failure"]
    assert isinstance(expected, Mapping)
    row = _current_rejection(state, candidate_id=candidate_id, expected_failure=expected)
    if not is_selected_final_review_rejection(row) or any(
        row.get(key) != value for key, value in _REJECTION_FIELDS.items()
    ):
        raise SelectedFinalReviewRecoveryAuthorityError("current rejection shape mismatches")
    evidence = row.get("failure_evidence")
    if not isinstance(evidence, Mapping) or not (
        row.get("failure_recovery_fingerprint") == expected["failure_recovery_fingerprint"]
        and evidence.get("reviewed_srt_sha256") == expected["reviewed_srt_sha256"]
        and evidence.get("surface_files") == expected["surface_files"]
        and evidence.get("findings") == [expected["finding"]]
    ):
        raise SelectedFinalReviewRecoveryAuthorityError("current rejection evidence mismatches")
    return row


def build_authorized_selected_final_review_recovery_receipt(
    authority: Mapping[str, object],
    *,
    state: Mapping[str, object],
    state_date: str,
    queued_row: Mapping[str, object],
    current_fingerprint: str,
) -> dict[str, object]:
    """Build the standard v7 receipt only after the sealed checks above pass."""

    old_row = validate_selected_final_review_recovery_rejection(
        authority, state=state, state_date=state_date
    )
    scope = authority["scope_template"]
    assert isinstance(scope, Mapping)
    return build_selected_final_review_recovery_receipt(
        old_row=old_row,
        queued_row=queued_row,
        candidate_id=str(authority["candidate_id"]),
        grant_id=str(scope["grant_id"]),
        current_fingerprint=current_fingerprint,
    )
