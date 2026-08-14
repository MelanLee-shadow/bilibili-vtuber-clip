"""Human-bound review holds for candidates that may repeat a published topic.

This is deliberately *not* a fuzzy duplicate detector.  The current selector's
``event_key`` is local to one semantic-recall call and selection-metric v2 only
consumes an opaque caller-provided ``topic_fingerprint``; neither is a safe
cross-publication identity.  A candidate is therefore parked only when the
active repository contains a committed/deployed, candidate-scoped authority
which says that the exact candidate/published pair requires human review.

The authority does not lower a score, delete a candidate, or declare the two
clips duplicates.  It binds the current hooks, scorecards, scene/date, the
committed publication-registry row, public title and BVID.  Any drift leaves
the candidate parked with a stale-authority reason rather than silently
returning it to production.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping
from copy import deepcopy
from pathlib import Path

from src.autoslice.publication_registry import (
    DEFAULT_REGISTRY_PATH,
    load_publication_registry,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    repository_authority_expects_asset,
    require_repository_asset_authority,
)
from src.autoslice.semantic_candidate_selector import SEMANTIC_CHAT_POLICY_SHA256
from src.autoslice.semantic_evidence_scorecard_refresh import (
    PROVIDER_CONTRACT_SHA256 as CURRENT_REFRESH_PROVIDER_CONTRACT_SHA256,
)
from src.autoslice.published_topic_recovery_lineage import (
    RECOVERY_PRODUCE_TRANSITION,
    RECOVERY_QUEUE_COLLECTIONS as _RECOVERY_QUEUE_COLLECTIONS,
    RECOVERY_QUEUE_TERMINAL_TRANSITION,
    RECOVERY_REBOUND_FIELDS as _RECOVERY_REBOUND_FIELDS,
    RECOVERY_REBOUND_PRODUCTION_PREPARE,
    RECOVERY_REBOUND_PRODUCTION_QUEUE_RETRY,
    RECOVERY_REBOUND_SEMANTIC_SCORECARD_REFRESH as _RECOVERY_REBOUND_SEMANTIC_SCORECARD_REFRESH,
    RECOVERY_REBOUND_SESSION_ANNOTATION,
    RECOVERY_REQUEUE_TRANSITION,
    RECOVERY_ROW_REBOUND_PHASES,
    RECOVERY_ROW_REBOUND_TRANSITION,
    RECOVERY_TRANSITION_SCHEMA,
    recovery_row_binding_is_valid as _recovery_row_binding_is_valid,
    top_level_changed_fields as _top_level_changed_fields,
    validated_transition_next_binding,
)
from src.autoslice.published_topic_selected_rejection import (
    failure_recovery_fingerprint_disposition as _failure_recovery_fingerprint_disposition,
    hold_mentions_candidate as _hold_mentions_candidate,
    is_selected_subtitle_authority_rejection as _is_selected_subtitle_authority_rejection,
    review_holds_are_valid as _selected_rejection_review_holds_are_valid,
)
from src.autoslice.published_topic_hold_scope import (
    current_rows_by_id as _current_rows_by_id,
    held_candidates as _held_candidates,
    merge_scoped_holds as _merge_scoped_holds,
    normalized_candidate_allowlist as _normalized_candidate_allowlist,
    prior_hold_records as _prior_hold_records,
    prior_holds_in_order as _prior_holds_in_order,
)


AUTHORITY_SCHEMA = "published-topic-collision-authority.v1"
AUTHORITY_KIND = "HUMAN_ASSERTED_REVIEW_REQUIRED"
PAIR_FINGERPRINT_KIND = "PAIR_IDENTITY_ONLY_NOT_DUPLICATE_VERDICT"
REVIEW_STATE_SCHEMA = "published-topic-dedup-review-state.v1"
REVIEW_STATUS = "HUMAN_TOPIC_DEDUP_REVIEW"
STALE_REVIEW_STATUS = "HUMAN_TOPIC_DEDUP_REVIEW_AUTHORITY_STALE"
REVIEW_STATE_FIELD = "published_topic_dedup_review"
AUTHORITY_DIRECTORY = Path("assets/lidousha/published_topic_collision_authorities")
AUTHORITY_SUFFIX = ".published-topic-collision-authority.v1.json"
RESOLUTION_SCHEMA = "published-topic-dedup-resolution.v1"
REFRESHED_RESOLUTION_SCHEMA = "published-topic-dedup-resolution.v2"
RESOLUTION_DECISION = "RELEASE_FOR_PRODUCTION"
RESOLUTION_DIRECTORY = Path("assets/lidousha/published_topic_dedup_resolutions")
RESOLUTION_SUFFIX = ".published-topic-dedup-resolution.v1.json"
REFRESHED_RESOLUTION_SUFFIX = ".published-topic-dedup-resolution.v2.json"
REFRESH_RECEIPT_SCHEMA = "semantic-evidence-scorecard-refresh-receipt.v1"
REFRESH_RECEIPT_FIELD = "semantic_evidence_scorecard_refresh"
RECOVERY_MARKER_FIELD = "published_topic_resolution_recovery"
RECOVERY_MARKER_SCHEMA = "published-topic-resolution-recovery-marker.v2"
RECOVERY_LEDGER_SCHEMA = "published-topic-resolution-recovery-ledger.v1"
RECOVERY_REBOUND_SEMANTIC_SCORECARD_REFRESH = (
    _RECOVERY_REBOUND_SEMANTIC_SCORECARD_REFRESH
)
RECOVERY_READY_TO_RELEASE = "READY_TO_RELEASE"
RECOVERY_RELEASED_QUEUED = "RELEASED_QUEUED"
RECOVERY_RELEASED_RETRY_PENDING = "RELEASED_RETRY_PENDING"
RECOVERY_CONVERGED = "CONVERGED"
RECOVERY_BLOCKED = "BLOCKED"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]

_CANDIDATE_ID_RE = re.compile(r"auto_[0-9]+_[0-9]+_[0-9]+\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCENE_KINDS = frozenset({"event", "talk", "game"})
_RECOVERABLE_TALK_FAILURE_STATUSES = frozenset(
    {
        "boundary_unrepairable",
        "failed",
        "speaker_evidence_insufficient",
        "speaker_review_required",
    }
)
_DELIVERED_TALK_STATUSES = frozenset(
    {"ok", "published", "quarantine", "review_ready"}
)
_COVER_PENDING_TALK_STATUS = "media_ready_cover_pending"


class PublishedTopicCollisionError(ValueError):
    """The review authority or one of its current bindings is invalid."""


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def authority_relative_path(candidate_id: str) -> Path:
    if _CANDIDATE_ID_RE.fullmatch(str(candidate_id or "")) is None:
        raise PublishedTopicCollisionError("candidate_id is invalid")
    return AUTHORITY_DIRECTORY / f"{candidate_id}{AUTHORITY_SUFFIX}"


def resolution_relative_path(candidate_id: str) -> Path:
    """Return the one candidate-scoped human resolution path.

    The original collision authority is deliberately retained.  A resolution
    is an additional, sealed statement that may release production only after
    the original pair has been revalidated against current state.
    """

    if _CANDIDATE_ID_RE.fullmatch(str(candidate_id or "")) is None:
        raise PublishedTopicCollisionError("candidate_id is invalid")
    return RESOLUTION_DIRECTORY / f"{candidate_id}{RESOLUTION_SUFFIX}"


def refreshed_resolution_relative_path(candidate_id: str) -> Path:
    """Return the v2 path that rebinds a reviewed pair after score refresh."""

    if _CANDIDATE_ID_RE.fullmatch(str(candidate_id or "")) is None:
        raise PublishedTopicCollisionError("candidate_id is invalid")
    return RESOLUTION_DIRECTORY / f"{candidate_id}{REFRESHED_RESOLUTION_SUFFIX}"


def _raw_sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _candidate_id(row: Mapping[str, object]) -> str:
    return str(row.get("candidate_id") or row.get("cid") or "")


def _scene_identity(row: Mapping[str, object]) -> tuple[str, str]:
    context = row.get("segment_scene_context")
    if not isinstance(context, Mapping):
        raise PublishedTopicCollisionError("candidate scene context is missing")
    recording_date = str(context.get("recording_date") or "")
    scene_kind = str(context.get("scene_kind") or "")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", recording_date) is None:
        raise PublishedTopicCollisionError("candidate recording date is invalid")
    if scene_kind not in _SCENE_KINDS:
        raise PublishedTopicCollisionError("candidate scene kind is invalid")
    return recording_date, scene_kind


def _require_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublishedTopicCollisionError(f"{label} is missing")
    return value


def _require_sha(value: object, *, label: str) -> str:
    digest = str(value or "")
    if _SHA256_RE.fullmatch(digest) is None:
        raise PublishedTopicCollisionError(f"{label} is not a SHA-256 digest")
    return digest


def _require_bound_candidate(
    binding: object,
    row: Mapping[str, object],
    *,
    label: str,
    expected_scorecard_sha256: str | None = None,
) -> dict[str, object]:
    if not isinstance(binding, Mapping):
        raise PublishedTopicCollisionError(f"{label} binding is invalid")
    candidate_id = _candidate_id(row)
    recording_date, scene_kind = _scene_identity(row)
    hook = _require_text(row.get("hook"), label=f"{label} current hook")
    scorecard = row.get("selection_scorecard")
    if not isinstance(scorecard, Mapping):
        raise PublishedTopicCollisionError(f"{label} current scorecard is missing")
    current_scorecard_sha256 = canonical_sha256(scorecard)
    scorecard_sha256 = (
        _require_sha(
            expected_scorecard_sha256,
            label=f"{label} expected scorecard SHA-256",
        )
        if expected_scorecard_sha256 is not None
        else current_scorecard_sha256
    )
    expected = {
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "scene_kind": scene_kind,
        "hook": hook,
        "hook_sha256": canonical_sha256(hook),
        "selection_scorecard_sha256": scorecard_sha256,
    }
    for key, value in expected.items():
        if binding.get(key) != value:
            raise PublishedTopicCollisionError(f"{label} {key} binding drifted")
    return expected


def _registry_row(
    registry: Mapping[str, object], candidate_id: str, recording_date: str
) -> Mapping[str, object]:
    entries = registry.get("entries")
    if not isinstance(entries, list):
        raise PublishedTopicCollisionError("publication registry entries are invalid")
    matches = [
        row
        for row in entries
        if isinstance(row, Mapping)
        and str(row.get("candidate_id") or "") == candidate_id
        and str(row.get("recording_date") or "") == recording_date
    ]
    if len(matches) != 1:
        raise PublishedTopicCollisionError(
            "published candidate has no unique publication registry row"
        )
    row = matches[0]
    if row.get("status") != "published" or not str(row.get("bvid") or ""):
        raise PublishedTopicCollisionError("review target is not registered as published")
    return row


def _validate_authority(
    value: object,
    *,
    candidate_row: Mapping[str, object],
    published_row: Mapping[str, object],
    publication_registry: Mapping[str, object],
    candidate_scorecard_sha256: str | None = None,
) -> dict[str, object]:
    """Validate one exact pair, optionally at an internal historical card."""

    if not isinstance(value, Mapping):
        raise PublishedTopicCollisionError("authority is not an object")
    authority = dict(value)
    declared_sha = authority.pop("authority_sha256", None)
    if (
        authority.get("schema_version") != AUTHORITY_SCHEMA
        or authority.get("authority_kind") != AUTHORITY_KIND
        or authority.get("decision") != REVIEW_STATUS
        or authority.get("suppression_authorized") is not False
        or authority.get("score_mutation_authorized") is not False
        or authority.get("upload_authorized") is not False
        or canonical_sha256(authority) != declared_sha
    ):
        raise PublishedTopicCollisionError("authority envelope is invalid")
    _require_text(authority.get("authority_quote"), label="authority quote")
    _require_text(authority.get("asserted_by"), label="asserted_by")

    candidate = _require_bound_candidate(
        authority.get("candidate"),
        candidate_row,
        label="candidate",
        expected_scorecard_sha256=candidate_scorecard_sha256,
    )
    published = _require_bound_candidate(
        authority.get("published"), published_row, label="published candidate"
    )
    if candidate["candidate_id"] == published["candidate_id"]:
        raise PublishedTopicCollisionError("candidate cannot review against itself")
    if candidate["recording_date"] != published["recording_date"]:
        raise PublishedTopicCollisionError("candidate and published date differ")
    if candidate["scene_kind"] != published["scene_kind"]:
        raise PublishedTopicCollisionError("candidate and published scene differ")
    if published_row.get("status") != "published":
        raise PublishedTopicCollisionError("published state row is not published")

    registry_row = _registry_row(
        publication_registry,
        str(published["candidate_id"]),
        str(published["recording_date"]),
    )
    published_binding = authority.get("published")
    assert isinstance(published_binding, Mapping)
    if (
        published_binding.get("registry_row_sha256") != canonical_sha256(registry_row)
        or published_binding.get("registry_status") != "published"
        or published_binding.get("bvid") != registry_row.get("bvid")
    ):
        raise PublishedTopicCollisionError("publication registry binding drifted")
    bvid = _require_text(registry_row.get("bvid"), label="published BVID")
    public_title = _require_text(
        published_binding.get("public_title"), label="published public title"
    )
    if published_binding.get("public_title_sha256") != canonical_sha256(public_title):
        raise PublishedTopicCollisionError("published public title hash is invalid")
    state_bvid = str(published_row.get("bvid") or "")
    reconciliation = published_row.get("publication_reconciliation")
    reconciliation_bvid = (
        str(reconciliation.get("bvid") or "") if isinstance(reconciliation, Mapping) else ""
    )
    if bvid not in {state_bvid, reconciliation_bvid}:
        raise PublishedTopicCollisionError("published state BVID binding drifted")

    pair_fingerprint = {
        "kind": PAIR_FINGERPRINT_KIND,
        "authority_quote": authority["authority_quote"],
        "candidate": candidate,
        "published": {
            **published,
            "registry_row_sha256": canonical_sha256(registry_row),
            "bvid": bvid,
            "public_title": public_title,
            "public_title_sha256": canonical_sha256(public_title),
        },
    }
    if authority.get("topic_review_fingerprint_sha256") != canonical_sha256(pair_fingerprint):
        raise PublishedTopicCollisionError("topic review fingerprint drifted")
    return {
        **authority,
        "authority_sha256": declared_sha,
        "validated_pair": pair_fingerprint,
    }


def validate_authority(
    value: object,
    *,
    candidate_row: Mapping[str, object],
    published_row: Mapping[str, object],
    publication_registry: Mapping[str, object],
) -> dict[str, object]:
    """Validate one exact pair against the current scorecard and registry."""

    return _validate_authority(
        value,
        candidate_row=candidate_row,
        published_row=published_row,
        publication_registry=publication_registry,
    )


def _require_nonnegative_int(value: object, *, label: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PublishedTopicCollisionError(f"{label} is not an integer")
    if value < (1 if positive else 0):
        raise PublishedTopicCollisionError(f"{label} is out of range")
    return value


def _refresh_candidate_binding(row: Mapping[str, object]) -> dict[str, object]:
    start_ms = _require_nonnegative_int(row.get("start_ms"), label="refresh start_ms")
    end_ms = _require_nonnegative_int(row.get("end_ms"), label="refresh end_ms", positive=True)
    if end_ms <= start_ms:
        raise PublishedTopicCollisionError("refresh candidate window is invalid")
    lane = str(row.get("lane") or "")
    if lane not in {"semantic_recall", "semantic_recall_sharded"}:
        raise PublishedTopicCollisionError("refresh candidate lane is invalid")
    return {
        "candidate_id": _candidate_id(row),
        "lane": lane,
        "hook": _require_text(row.get("hook"), label="refresh candidate hook"),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "segment_path": _require_text(
            row.get("segment_path"), label="refresh candidate segment path"
        ),
        "bcut_srt_path": _require_text(
            row.get("bcut_srt_path"), label="refresh candidate BCUT path"
        ),
        "xml_path": _require_text(row.get("xml"), label="refresh candidate XML path"),
    }


def _require_refresh_stat(
    value: object,
    *,
    label: str,
    expected_path: str,
    require_sha256: bool,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PublishedTopicCollisionError(f"{label} is invalid")
    expected_keys = {
        "path",
        "size_bytes",
        "mtime_ns",
        "ctime_ns",
        "device",
        "inode",
        "mode",
    }
    if require_sha256:
        expected_keys.add("sha256")
    if set(value) != expected_keys or value.get("path") != expected_path:
        raise PublishedTopicCollisionError(f"{label} path or shape drifted")
    for key in ("size_bytes", "mtime_ns", "ctime_ns", "device", "inode", "mode"):
        _require_nonnegative_int(
            value.get(key), label=f"{label} {key}", positive=key == "size_bytes"
        )
    if require_sha256:
        _require_sha(value.get("sha256"), label=f"{label} SHA-256")
    return value


def _validate_scorecard_refresh(
    refresh_binding: object,
    *,
    candidate_row: Mapping[str, object],
    original_scorecard_sha256: str,
) -> dict[str, object]:
    """Prove one exact, successful old-card -> current-card refresh receipt."""

    if not isinstance(refresh_binding, Mapping):
        raise PublishedTopicCollisionError("resolution scorecard refresh binding is invalid")
    expected_binding_keys = {
        "receipt_schema_version",
        "receipt_sha256",
        "candidate_id",
        "recording_date",
        "old_selection_scorecard_sha256",
        "new_selection_scorecard_sha256",
        "candidate_binding_sha256",
        "input_provenance_sha256",
        "provider_contract_sha256",
        "semantic_chat_policy_sha256",
        "semantic_chat_source_sha256",
        "semantic_chat_evidence_sha256",
    }
    if set(refresh_binding) != expected_binding_keys:
        raise PublishedTopicCollisionError("resolution scorecard refresh shape drifted")
    receipt = candidate_row.get(REFRESH_RECEIPT_FIELD)
    if not isinstance(receipt, Mapping):
        raise PublishedTopicCollisionError("current scorecard refresh receipt is missing")
    expected_receipt_keys = {
        "schema_version",
        "candidate_id",
        "recording_date",
        "scope_grant_id",
        "scope_grant_sha256",
        "status",
        "reason_code",
        "attempt_fingerprint",
        "attempts",
        "old_scorecard_sha256",
        "new_scorecard_sha256",
        "provider_contract",
        "provider_contract_sha256",
        "input_provenance",
        "receipt_sha256",
    }
    receipt_body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    candidate_id = _candidate_id(candidate_row)
    recording_date, _scene_kind = _scene_identity(candidate_row)
    current_scorecard_sha256 = canonical_sha256(candidate_row.get("selection_scorecard"))
    if (
        set(receipt) != expected_receipt_keys
        or receipt.get("schema_version") != REFRESH_RECEIPT_SCHEMA
        or receipt.get("status") != "REFRESHED"
        or receipt.get("reason_code") != "REFRESHED_WITH_CURRENT_CHAT_EVIDENCE"
        or receipt.get("candidate_id") != candidate_id
        or receipt.get("recording_date") != recording_date
        or receipt.get("old_scorecard_sha256") != original_scorecard_sha256
        or receipt.get("new_scorecard_sha256") != current_scorecard_sha256
        or receipt.get("receipt_sha256") != canonical_sha256(receipt_body)
    ):
        raise PublishedTopicCollisionError("current scorecard refresh receipt drifted")
    _require_text(receipt.get("scope_grant_id"), label="refresh scope grant ID")
    _require_sha(receipt.get("scope_grant_sha256"), label="refresh scope grant SHA-256")
    attempt_fingerprint = _require_sha(
        receipt.get("attempt_fingerprint"), label="refresh attempt fingerprint"
    )

    provider_contract = receipt.get("provider_contract")
    if (
        not isinstance(provider_contract, Mapping)
        or provider_contract.get("schema_version")
        != "semantic-evidence-scorecard-refresh-provider.v1"
        or provider_contract.get("prompt_schema") != "semantic-evidence-scorecard-refresh-prompt.v1"
        or receipt.get("provider_contract_sha256") != canonical_sha256(provider_contract)
        or receipt.get("provider_contract_sha256")
        != CURRENT_REFRESH_PROVIDER_CONTRACT_SHA256
    ):
        raise PublishedTopicCollisionError("refresh provider contract drifted")

    provenance = receipt.get("input_provenance")
    if not isinstance(provenance, Mapping):
        raise PublishedTopicCollisionError("refresh input provenance is invalid")
    expected_provenance_keys = {
        "source_media",
        "bcut_srt",
        "semantic_chat",
        "semantic_chat_source",
        "candidate_binding",
        "candidate_binding_sha256",
        "prompt_sha256",
        "response_sha256",
        "old_scorecard_sha256",
    }
    current_binding = _refresh_candidate_binding(candidate_row)
    if (
        set(provenance) != expected_provenance_keys
        or provenance.get("candidate_binding") != current_binding
        or provenance.get("candidate_binding_sha256") != canonical_sha256(current_binding)
        or provenance.get("old_scorecard_sha256") != original_scorecard_sha256
    ):
        raise PublishedTopicCollisionError("refresh candidate or input binding drifted")
    prompt_sha256 = _require_sha(provenance.get("prompt_sha256"), label="refresh prompt SHA-256")
    response_sha256 = _require_sha(
        provenance.get("response_sha256"), label="refresh response SHA-256"
    )
    _require_refresh_stat(
        provenance.get("source_media"),
        label="refresh source media",
        expected_path=str(current_binding["segment_path"]),
        require_sha256=False,
    )
    _require_refresh_stat(
        provenance.get("bcut_srt"),
        label="refresh BCUT SRT",
        expected_path=str(current_binding["bcut_srt_path"]),
        require_sha256=True,
    )
    chat_source = _require_refresh_stat(
        provenance.get("semantic_chat_source"),
        label="refresh semantic chat source",
        expected_path=str(current_binding["xml_path"]),
        require_sha256=True,
    )
    semantic_chat = provenance.get("semantic_chat")
    if (
        not isinstance(semantic_chat, Mapping)
        or set(semantic_chat)
        != {
            "schema_version",
            "algorithm_id",
            "status",
            "policy_sha256",
            "source_sha256",
            "evidence_sha256",
            "window_start_ms",
            "window_end_ms",
        }
        or semantic_chat.get("schema_version") != "semantic-recall-chat-evidence.v1"
        or semantic_chat.get("algorithm_id") != "bounded-request-reaction.v1"
        or semantic_chat.get("status") != "LOADED"
        or semantic_chat.get("policy_sha256") != SEMANTIC_CHAT_POLICY_SHA256
        or semantic_chat.get("window_start_ms") != current_binding["start_ms"]
        or semantic_chat.get("window_end_ms") != current_binding["end_ms"]
        or semantic_chat.get("source_sha256") != chat_source.get("sha256")
    ):
        raise PublishedTopicCollisionError("refresh semantic chat evidence drifted")
    for key in ("policy_sha256", "source_sha256", "evidence_sha256"):
        _require_sha(semantic_chat.get(key), label=f"refresh semantic chat {key}")
    scorecard = candidate_row.get("selection_scorecard")
    if (
        not isinstance(scorecard, Mapping)
        or scorecard.get("semantic_recall_chat_evidence") != semantic_chat
    ):
        raise PublishedTopicCollisionError("current scorecard lacks refreshed chat provenance")

    attempts = receipt.get("attempts")
    final_attempt = attempts[-1] if isinstance(attempts, list) and attempts else None
    if (
        not isinstance(final_attempt, Mapping)
        or final_attempt.get("attempt_fingerprint") != attempt_fingerprint
        or final_attempt.get("outcome") != "REFRESHED"
        or final_attempt.get("reason_code") != "REFRESHED_WITH_CURRENT_CHAT_EVIDENCE"
        or final_attempt.get("prompt_sha256") != prompt_sha256
        or final_attempt.get("response_sha256") != response_sha256
    ):
        raise PublishedTopicCollisionError("refresh successful attempt binding drifted")

    expected_refresh_binding = {
        "receipt_schema_version": REFRESH_RECEIPT_SCHEMA,
        "receipt_sha256": receipt["receipt_sha256"],
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "old_selection_scorecard_sha256": original_scorecard_sha256,
        "new_selection_scorecard_sha256": current_scorecard_sha256,
        "candidate_binding_sha256": provenance["candidate_binding_sha256"],
        "input_provenance_sha256": canonical_sha256(provenance),
        "provider_contract_sha256": receipt["provider_contract_sha256"],
        "semantic_chat_policy_sha256": semantic_chat["policy_sha256"],
        "semantic_chat_source_sha256": semantic_chat["source_sha256"],
        "semantic_chat_evidence_sha256": semantic_chat["evidence_sha256"],
    }
    if dict(refresh_binding) != expected_refresh_binding:
        raise PublishedTopicCollisionError("resolution scorecard refresh binding drifted")
    return expected_refresh_binding


def validate_resolution(
    value: object,
    *,
    candidate_row: Mapping[str, object],
    published_row: Mapping[str, object],
    validated_authority: Mapping[str, object],
    authority_path: Path,
    authority_bytes: bytes,
) -> dict[str, object]:
    """Validate a human production release without widening upload authority.

    ``validated_authority`` is the result of :func:`validate_authority`, so it
    already proves the live candidate, the published state row, registry row,
    BVID and public title.  The resolution deliberately repeats those exact
    bindings and pins both the original raw bytes and its self-seal; a stale
    pair therefore cannot be released by merely retaining an old resolution.
    """

    if not isinstance(value, Mapping):
        raise PublishedTopicCollisionError("resolution is not an object")
    resolution = dict(value)
    declared_sha = resolution.pop("resolution_sha256", None)
    schema_version = resolution.get("schema_version")
    expected_keys = {
        "schema_version",
        "decision",
        "asserted_by",
        "asserted_at",
        "authority_quote",
        "candidate",
        "published",
        "original_authority_relative_path",
        "original_authority_raw_sha256",
        "original_authority_sha256",
        "upload_authorized",
    }
    if schema_version == REFRESHED_RESOLUTION_SCHEMA:
        expected_keys.add("scorecard_refresh")
    if (
        set(resolution) != expected_keys
        or schema_version not in {RESOLUTION_SCHEMA, REFRESHED_RESOLUTION_SCHEMA}
        or resolution.get("decision") != RESOLUTION_DECISION
        or resolution.get("upload_authorized") is not False
        or canonical_sha256(resolution) != declared_sha
    ):
        raise PublishedTopicCollisionError("resolution envelope is invalid")
    _require_text(resolution.get("asserted_by"), label="resolution asserted_by")
    _require_text(resolution.get("asserted_at"), label="resolution asserted_at")
    _require_text(resolution.get("authority_quote"), label="resolution authority quote")

    candidate = _require_bound_candidate(
        resolution.get("candidate"), candidate_row, label="resolution candidate"
    )
    published = _require_bound_candidate(
        resolution.get("published"), published_row, label="resolution published candidate"
    )
    original_candidate = validated_authority.get("candidate")
    original_published = validated_authority.get("published")
    if not isinstance(original_candidate, Mapping) or not isinstance(original_published, Mapping):
        raise PublishedTopicCollisionError("validated original authority is incomplete")
    if schema_version == RESOLUTION_SCHEMA:
        if dict(resolution["candidate"]) != dict(original_candidate) or candidate != dict(
            original_candidate
        ):
            raise PublishedTopicCollisionError("resolution candidate binding drifted")
    else:
        expected_current_candidate = {
            **dict(original_candidate),
            "selection_scorecard_sha256": canonical_sha256(
                candidate_row.get("selection_scorecard")
            ),
        }
        if (
            dict(resolution["candidate"]) != expected_current_candidate
            or candidate != expected_current_candidate
        ):
            raise PublishedTopicCollisionError("refreshed resolution candidate binding drifted")
        _validate_scorecard_refresh(
            resolution.get("scorecard_refresh"),
            candidate_row=candidate_row,
            original_scorecard_sha256=_require_sha(
                original_candidate.get("selection_scorecard_sha256"),
                label="original candidate scorecard SHA-256",
            ),
        )
    # _require_bound_candidate intentionally checks the moving source fields;
    # the original authority carries the additional registry/public fields.
    required_published = {
        key: original_published.get(key)
        for key in (
            "candidate_id",
            "recording_date",
            "scene_kind",
            "hook",
            "hook_sha256",
            "selection_scorecard_sha256",
            "registry_row_sha256",
            "registry_status",
            "bvid",
            "public_title",
            "public_title_sha256",
        )
    }
    if dict(resolution["published"]) != required_published or any(
        published.get(key) != required_published.get(key)
        for key in (
            "candidate_id",
            "recording_date",
            "scene_kind",
            "hook",
            "hook_sha256",
            "selection_scorecard_sha256",
        )
    ):
        raise PublishedTopicCollisionError("resolution published binding drifted")
    if resolution.get("original_authority_relative_path") != authority_path.as_posix():
        raise PublishedTopicCollisionError("resolution original authority path drifted")
    if resolution.get("original_authority_raw_sha256") != _raw_sha256(authority_bytes):
        raise PublishedTopicCollisionError("resolution original authority bytes drifted")
    if resolution.get("original_authority_sha256") != validated_authority.get("authority_sha256"):
        raise PublishedTopicCollisionError("resolution original authority self-seal drifted")
    return {**resolution, "resolution_sha256": declared_sha}


def _remove_from_queue(state: dict, candidate_id: str) -> None:
    for field in ("pending_talk", "talk_backlog"):
        values = state.get(field)
        if isinstance(values, list):
            state[field] = [
                row
                for row in values
                if not (isinstance(row, Mapping) and _candidate_id(row) == candidate_id)
            ]


def _queue_origin(state: Mapping[str, object], candidate_id: str) -> str | None:
    """Return the unique queue currently owning a candidate, if any."""

    origins = [
        field
        for field in ("pending_talk", "talk_backlog")
        if any(
            isinstance(row, Mapping) and _candidate_id(row) == candidate_id
            for row in (state.get(field) or [])
        )
    ]
    return origins[0] if len(origins) == 1 else None


def _restore_resolved_hold(
    state: dict,
    *,
    candidate_id: str,
    prior_hold: Mapping[str, object] | None,
) -> None:
    """Put a validly released sticky hold back exactly where it was parked."""

    if prior_hold is None:
        return
    for field in ("pending_talk", "talk_backlog"):
        rows = state.get(field)
        if isinstance(rows, list) and any(
            isinstance(row, Mapping) and _candidate_id(row) == candidate_id for row in rows
        ):
            # Delivery recovery may already have restored an older sticky hold
            # whose pre-v1 receipt predates ``queue_origin``.  The current
            # queue row is stronger evidence than that legacy omission: keep
            # it in place, but never infer an origin when no fresh row exists.
            return
    origin = prior_hold.get("queue_origin")
    candidate = prior_hold.get("candidate")
    if origin not in {"pending_talk", "talk_backlog"} or not isinstance(candidate, Mapping):
        raise PublishedTopicCollisionError("resolved sticky hold has no valid queue origin")
    target = state.get(origin)
    if target is None:
        target = []
        state[origin] = target
    if not isinstance(target, list):
        raise PublishedTopicCollisionError("resolved sticky hold queue is malformed")
    target.append(dict(candidate))


def _resolved_target_shadow(
    state: Mapping[str, object],
    *,
    candidate_id: str,
    repo_root: Path,
    publication_registry: Mapping[str, object] | None,
) -> tuple[dict, str, dict[str, object]] | None:
    """Evaluate one parked candidate on a copy and return its restored row."""

    current = state.get(REVIEW_STATE_FIELD)
    holds = current.get("holds") if isinstance(current, Mapping) else None
    matches = [
        hold
        for hold in (holds if isinstance(holds, list) else [])
        if isinstance(hold, Mapping) and _candidate_id(hold) == candidate_id
    ]
    if len(matches) != 1:
        return None
    shadow = deepcopy(dict(state))
    remaining = hold_published_topic_collision_reviews(
        shadow,
        repo_root=repo_root,
        publication_registry=publication_registry,
    )
    if any(_candidate_id(hold) == candidate_id for hold in remaining):
        return None
    restored = [
        (field, row)
        for field in ("pending_talk", "talk_backlog")
        for row in (shadow.get(field) if isinstance(shadow.get(field), list) else [])
        if isinstance(row, dict) and _candidate_id(row) == candidate_id
    ]
    if len(restored) != 1:
        return None
    field, row = restored[0]
    return shadow, field, row


def _recovery_marker(
    *,
    candidate_id: str,
    queue_origin: str,
    hold: Mapping[str, object],
    restored: Mapping[str, object],
    repo_root: Path,
) -> dict[str, object]:
    resolution_relative = refreshed_resolution_relative_path(candidate_id)
    resolution_bytes = (repo_root / resolution_relative).read_bytes()
    resolution = json.loads(resolution_bytes.decode("utf-8"))
    if not isinstance(resolution, Mapping):
        raise PublishedTopicCollisionError("refreshed resolution is not an object")
    authority_relative = authority_relative_path(candidate_id)
    authority_bytes = (repo_root / authority_relative).read_bytes()
    authority = json.loads(authority_bytes.decode("utf-8"))
    if not isinstance(authority, Mapping):
        raise PublishedTopicCollisionError("original authority is not an object")
    refresh = resolution.get("scorecard_refresh")
    if not isinstance(refresh, Mapping):
        raise PublishedTopicCollisionError("refreshed resolution binding is missing")
    marker: dict[str, object] = {
        "schema_version": RECOVERY_MARKER_SCHEMA,
        "candidate_id": candidate_id,
        "queue_origin": queue_origin,
        "upload_authorized": False,
        "original_hold": deepcopy(dict(hold)),
        "original_hold_sha256": canonical_sha256(hold),
        "restored_candidate_sha256": canonical_sha256(restored),
        "resolution": {
            "relative_path": resolution_relative.as_posix(),
            "raw_sha256": _raw_sha256(resolution_bytes),
            "resolution_sha256": resolution.get("resolution_sha256"),
        },
        "original_authority": {
            "relative_path": authority_relative.as_posix(),
            "raw_sha256": _raw_sha256(authority_bytes),
            "authority_sha256": authority.get("authority_sha256"),
        },
        "scorecard_refresh": {
            "receipt_sha256": refresh.get("receipt_sha256"),
            "old_selection_scorecard_sha256": refresh.get(
                "old_selection_scorecard_sha256"
            ),
            "new_selection_scorecard_sha256": refresh.get(
                "new_selection_scorecard_sha256"
            ),
        },
        "current_row_binding": {
            "collection": queue_origin,
            "row_sha256": canonical_sha256(restored),
        },
        "transitions": [],
    }
    marker["marker_sha256"] = canonical_sha256(marker)
    return marker


def _marker_structure_is_valid(candidate_id: str, marker: object) -> bool:
    """Validate one sealed candidate entry without consulting mutable rows/assets."""

    if not isinstance(marker, Mapping):
        return False
    body = {key: value for key, value in marker.items() if key != "marker_sha256"}
    expected_keys = {
        "schema_version",
        "candidate_id",
        "queue_origin",
        "upload_authorized",
        "original_hold",
        "original_hold_sha256",
        "restored_candidate_sha256",
        "resolution",
        "original_authority",
        "scorecard_refresh",
        "current_row_binding",
        "transitions",
        "marker_sha256",
    }
    if not (
        set(marker) == expected_keys
        and marker.get("schema_version") == RECOVERY_MARKER_SCHEMA
        and marker.get("candidate_id") == candidate_id
        and marker.get("queue_origin") in {"pending_talk", "talk_backlog"}
        and marker.get("upload_authorized") is False
        and marker.get("marker_sha256") == canonical_sha256(body)
    ):
        return False
    current_binding = marker.get("current_row_binding")
    transitions = marker.get("transitions")
    if not (_recovery_row_binding_is_valid(current_binding) and isinstance(transitions, list)):
        return False
    expected_previous = {
        "collection": marker.get("queue_origin"),
        "row_sha256": marker.get("restored_candidate_sha256"),
    }
    for index, transition in enumerate(transitions):
        next_binding = validated_transition_next_binding(
            transition,
            index=index,
            expected_previous=expected_previous,
            canonical_sha256=canonical_sha256,
        )
        if next_binding is None:
            return False
        expected_previous = next_binding
    return dict(current_binding) == expected_previous


def _recovery_ledger_entries(state: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    """Return the strict candidate-keyed ledger, or reject the entire container.

    Only the selected candidate's entry is later revalidated against current
    repository/state authority.  Historical entries retain their sealed evidence
    without allowing an unrelated old candidate to block a later serial recovery.
    """

    ledger = state.get(RECOVERY_MARKER_FIELD)
    if ledger is None:
        return {}
    if not isinstance(ledger, Mapping):
        raise PublishedTopicCollisionError("recovery ledger is not an object")
    body = {key: value for key, value in ledger.items() if key != "ledger_sha256"}
    entries = ledger.get("entries")
    if (
        set(ledger) != {"schema_version", "entries", "ledger_sha256"}
        or ledger.get("schema_version") != RECOVERY_LEDGER_SCHEMA
        or not isinstance(entries, Mapping)
        or ledger.get("ledger_sha256") != canonical_sha256(body)
    ):
        raise PublishedTopicCollisionError("recovery ledger seal is invalid")
    validated: dict[str, Mapping[str, object]] = {}
    for candidate_id, marker in entries.items():
        if (
            not isinstance(candidate_id, str)
            or _CANDIDATE_ID_RE.fullmatch(candidate_id) is None
            or not _marker_structure_is_valid(candidate_id, marker)
        ):
            raise PublishedTopicCollisionError("recovery ledger entry is invalid")
        assert isinstance(marker, Mapping)
        validated[candidate_id] = marker
    return validated


def _ledger_with_appended_marker(
    state: Mapping[str, object], candidate_id: str, marker: Mapping[str, object]
) -> dict[str, object]:
    entries = _recovery_ledger_entries(state)
    if candidate_id in entries or not _marker_structure_is_valid(candidate_id, marker):
        raise PublishedTopicCollisionError("recovery ledger entry cannot be replaced")
    next_entries = {key: deepcopy(dict(value)) for key, value in entries.items()}
    next_entries[candidate_id] = deepcopy(dict(marker))
    ledger: dict[str, object] = {
        "schema_version": RECOVERY_LEDGER_SCHEMA,
        "entries": next_entries,
    }
    ledger["ledger_sha256"] = canonical_sha256(ledger)
    return ledger


def _ledger_with_replaced_marker(
    state: Mapping[str, object], candidate_id: str, marker: Mapping[str, object]
) -> dict[str, object]:
    entries = _recovery_ledger_entries(state)
    if candidate_id not in entries or not _marker_structure_is_valid(
        candidate_id, marker
    ):
        raise PublishedTopicCollisionError("recovery ledger entry cannot be advanced")
    next_entries = {key: deepcopy(dict(value)) for key, value in entries.items()}
    next_entries[candidate_id] = deepcopy(dict(marker))
    ledger: dict[str, object] = {
        "schema_version": RECOVERY_LEDGER_SCHEMA,
        "entries": next_entries,
    }
    ledger["ledger_sha256"] = canonical_sha256(ledger)
    return ledger


def _recovery_identity_binding(row: Mapping[str, object]) -> dict[str, object] | None:
    """Project immutable selection identity shared by queue and pick rows."""

    candidate_id = _candidate_id(row)
    scorecard = row.get("selection_scorecard")
    segment_path = row.get("segment_path")
    segment = row.get("segment")
    segment_name = (
        Path(segment_path).name
        if isinstance(segment_path, str) and segment_path.strip()
        else str(segment or "").strip()
    )
    if (
        _CANDIDATE_ID_RE.fullmatch(candidate_id) is None
        or not isinstance(scorecard, Mapping)
        or not segment_name
        or isinstance(row.get("start_ms"), bool)
        or not isinstance(row.get("start_ms"), int)
        or isinstance(row.get("end_ms"), bool)
        or not isinstance(row.get("end_ms"), int)
        or row.get("start_ms", 0) >= row.get("end_ms", 0)
        or not isinstance(row.get("hook"), str)
    ):
        return None
    return {
        "candidate_id": candidate_id,
        "segment_name": segment_name,
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "hook": row["hook"],
        "selection_scorecard_sha256": canonical_sha256(scorecard),
    }


def _target_rows(
    state: Mapping[str, object], candidate_id: str
) -> tuple[
    list[tuple[str, Mapping[str, object]]],
    list[tuple[str, Mapping[str, object]]],
]:
    queued = [
        (field, row)
        for field in ("pending_talk", "talk_backlog")
        for row in (state.get(field) if isinstance(state.get(field), list) else [])
        if isinstance(row, Mapping) and _candidate_id(row) == candidate_id
    ]
    current = [
        (field, row)
        for field in ("picks", "talk_below_confidence_threshold")
        for row in (state.get(field) if isinstance(state.get(field), list) else [])
        if isinstance(row, Mapping) and _candidate_id(row) == candidate_id
    ]
    return queued, current


def _marker_is_valid(
    state: Mapping[str, object],
    candidate_id: str,
    marker: Mapping[str, object],
    *,
    repo_root: Path,
    publication_registry: Mapping[str, object] | None,
) -> tuple[
    bool,
    list[tuple[str, Mapping[str, object]]],
    list[tuple[str, Mapping[str, object]]],
]:
    queued, current = _target_rows(state, candidate_id)
    if not _marker_structure_is_valid(candidate_id, marker):
        return False, queued, current
    original_hold = marker.get("original_hold")
    original_candidate = (
        original_hold.get("candidate") if isinstance(original_hold, Mapping) else None
    )
    if (
        not isinstance(original_hold, Mapping)
        or not isinstance(original_candidate, Mapping)
        or _candidate_id(original_hold) != candidate_id
        or _candidate_id(original_candidate) != candidate_id
        or original_hold.get("queue_origin") != marker.get("queue_origin")
        or original_hold.get("upload_authorized") is not False
        or marker.get("original_hold_sha256") != canonical_sha256(original_hold)
        or marker.get("restored_candidate_sha256")
        != canonical_sha256(original_candidate)
    ):
        return False, queued, current
    original_identity = _recovery_identity_binding(original_candidate)
    if original_identity is None or any(
        _recovery_identity_binding(row) != original_identity
        for _collection, row in [*queued, *current]
    ):
        return False, queued, current
    documents: dict[str, Mapping[str, object]] = {}
    payloads: dict[str, bytes] = {}
    for key, relative_path, self_key in (
        ("resolution", refreshed_resolution_relative_path(candidate_id), "resolution_sha256"),
        ("original_authority", authority_relative_path(candidate_id), "authority_sha256"),
    ):
        binding = marker.get(key)
        try:
            payload = (repo_root / relative_path).read_bytes()
            document = json.loads(payload.decode("utf-8"))
            require_repository_asset_authority(
                repo_root=repo_root,
                relative_path=relative_path,
                observed_bytes=payload,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, RepositoryAssetAuthorityError):
            return False, queued, current
        if (
            not isinstance(binding, Mapping)
            or set(binding) != {"relative_path", "raw_sha256", self_key}
            or binding.get("relative_path") != relative_path.as_posix()
            or binding.get("raw_sha256") != _raw_sha256(payload)
            or not isinstance(document, Mapping)
            or binding.get(self_key) != document.get(self_key)
        ):
            return False, queued, current
        documents[key] = document
        payloads[key] = payload
    resolution_binding = marker.get("resolution")
    assert isinstance(resolution_binding, Mapping)
    resolution = documents["resolution"]
    authority = documents["original_authority"]
    refresh = resolution.get("scorecard_refresh")
    if (
        resolution.get("schema_version") != REFRESHED_RESOLUTION_SCHEMA
        or not isinstance(refresh, Mapping)
        or marker.get("scorecard_refresh")
        != {
            "receipt_sha256": refresh.get("receipt_sha256"),
            "old_selection_scorecard_sha256": refresh.get(
                "old_selection_scorecard_sha256"
            ),
            "new_selection_scorecard_sha256": refresh.get(
                "new_selection_scorecard_sha256"
            ),
        }
    ):
        return False, queued, current
    published_binding = authority.get("published")
    published_id = (
        str(published_binding.get("candidate_id") or "")
        if isinstance(published_binding, Mapping)
        else ""
    )
    published = _current_rows_by_id(state).get(published_id)
    registry = publication_registry
    try:
        if published is None:
            raise PublishedTopicCollisionError("published marker row is missing")
        if registry is None:
            registry = load_publication_registry(DEFAULT_REGISTRY_PATH)
        validated_authority = _validate_authority(
            authority,
            candidate_row=original_candidate,
            published_row=published,
            publication_registry=registry,
            candidate_scorecard_sha256=_require_sha(
                refresh.get("old_selection_scorecard_sha256"),
                label="marker old scorecard SHA-256",
            ),
        )
        validate_resolution(
            resolution,
            candidate_row=original_candidate,
            published_row=published,
            validated_authority=validated_authority,
            authority_path=authority_relative_path(candidate_id),
            authority_bytes=payloads["original_authority"],
        )
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        PublishedTopicCollisionError,
    ):
        return False, queued, current
    target_rows = [*queued, *current]
    current_binding = marker.get("current_row_binding")
    if (
        len(target_rows) != 1
        or not isinstance(current_binding, Mapping)
        or target_rows[0][0] != current_binding.get("collection")
        or current_binding.get("row_sha256")
        != canonical_sha256(target_rows[0][1])
    ):
        return False, queued, current
    return True, queued, current


def _inspect_released_recovery_entry(
    state: Mapping[str, object],
    candidate_id: str,
    marker: Mapping[str, object],
    *,
    repo_root: Path,
    publication_registry: Mapping[str, object] | None,
) -> str:
    """Classify one sealed release entry without treating every pick as terminal."""

    review_state = state.get(REVIEW_STATE_FIELD)
    holds = review_state.get("holds") if isinstance(review_state, Mapping) else None
    if not isinstance(holds, list):
        return RECOVERY_BLOCKED
    target_holds = [
        hold for hold in holds if _hold_mentions_candidate(hold, candidate_id)
    ]
    valid, queued, current = _marker_is_valid(
        state,
        candidate_id,
        marker,
        repo_root=repo_root,
        publication_registry=publication_registry,
    )
    if not valid or (queued and current):
        return RECOVERY_BLOCKED
    if queued:
        return (
            RECOVERY_RELEASED_QUEUED
            if len(queued) == 1 and not target_holds
            else RECOVERY_BLOCKED
        )
    if len(current) != 1:
        return RECOVERY_BLOCKED
    collection, row = current[0]
    selected_authority_rejection = _is_selected_subtitle_authority_rejection(row)
    if selected_authority_rejection and not (
        _selected_rejection_review_holds_are_valid(state, candidate_id)
    ):
        return RECOVERY_BLOCKED
    if not selected_authority_rejection and target_holds:
        return RECOVERY_BLOCKED
    if collection == "talk_below_confidence_threshold":
        return RECOVERY_CONVERGED
    status = str(row.get("status") or "")
    if status == _COVER_PENDING_TALK_STATUS:
        return RECOVERY_RELEASED_RETRY_PENDING
    if status in _RECOVERABLE_TALK_FAILURE_STATUSES:
        if row.get("failure_recoverable") is True:
            return RECOVERY_RELEASED_RETRY_PENDING
        if row.get("failure_recoverable") is False:
            if row.get("failure_kind") != "content_boundary":
                return RECOVERY_CONVERGED
            return _failure_recovery_fingerprint_disposition(
                row, candidate_id
            )
        return RECOVERY_BLOCKED
    if selected_authority_rejection:
        return _failure_recovery_fingerprint_disposition(row, candidate_id)
    if status in _DELIVERED_TALK_STATUSES or status == "candidate_rejected":
        return RECOVERY_CONVERGED
    return RECOVERY_BLOCKED


def inspect_published_topic_resolution_recovery(
    state: Mapping[str, object],
    candidate_id: str,
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
    publication_registry: Mapping[str, object] | None = None,
) -> str:
    """Inspect a first release, a marker-bound retry, or terminal convergence."""

    if _CANDIDATE_ID_RE.fullmatch(str(candidate_id or "")) is None:
        return RECOVERY_BLOCKED
    try:
        ledger_entries = _recovery_ledger_entries(state)
    except PublishedTopicCollisionError:
        return RECOVERY_BLOCKED
    marker = ledger_entries.get(candidate_id)
    if marker is not None:
        return _inspect_released_recovery_entry(
            state,
            candidate_id,
            marker,
            repo_root=repo_root,
            publication_registry=publication_registry,
        )
    # v5 is intentionally serial.  A new target cannot release while any older
    # ledger entry is still queued, waiting on a bounded retry, or invalid.
    for prior_candidate_id, prior_marker in ledger_entries.items():
        if (
            _inspect_released_recovery_entry(
                state,
                prior_candidate_id,
                prior_marker,
                repo_root=repo_root,
                publication_registry=publication_registry,
            )
            != RECOVERY_CONVERGED
        ):
            return RECOVERY_BLOCKED
    try:
        ready = _resolved_target_shadow(
            state,
            candidate_id=candidate_id,
            repo_root=repo_root,
            publication_registry=publication_registry,
        )
    except (OSError, ValueError, TypeError):
        return RECOVERY_BLOCKED
    return RECOVERY_READY_TO_RELEASE if ready is not None else RECOVERY_BLOCKED


def published_topic_resolution_recovery_outstanding(
    state: Mapping[str, object],
    candidate_id: str,
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
    publication_registry: Mapping[str, object] | None = None,
) -> bool:
    """Pure probe for an exact parked candidate whose sealed release is valid.

    Historical v2 failed-pick scope admission runs before ``prioritize``.  A
    candidate already parked here is absent from the ordinary queues, so this
    probe lets the orchestrator keep that exact scope alive without widening
    the queue or mutating state during date discovery.
    """

    return inspect_published_topic_resolution_recovery(
        state,
        candidate_id,
        repo_root=repo_root,
        publication_registry=publication_registry,
    ) in {
        RECOVERY_READY_TO_RELEASE,
        RECOVERY_RELEASED_QUEUED,
        RECOVERY_RELEASED_RETRY_PENDING,
    }


def _one_recovery_target_row(
    state: Mapping[str, object], candidate_id: str
) -> tuple[str, Mapping[str, object]] | None:
    queued, current = _target_rows(state, candidate_id)
    rows = [*queued, *current]
    return rows[0] if len(rows) == 1 else None


def _marker_with_transition(
    marker: Mapping[str, object],
    *,
    transition_kind: str,
    next_binding: Mapping[str, object],
    detail: Mapping[str, object],
) -> dict[str, object] | None:
    previous_binding = marker.get("current_row_binding")
    transitions = marker.get("transitions")
    if not isinstance(previous_binding, Mapping) or not isinstance(transitions, list):
        return None
    transition: dict[str, object] = {
        "schema_version": RECOVERY_TRANSITION_SCHEMA,
        "transition_index": len(transitions),
        "transition_kind": transition_kind,
        "previous_row_binding": deepcopy(dict(previous_binding)),
        **deepcopy(dict(detail)),
        "next_row_binding": deepcopy(dict(next_binding)),
    }
    transition["transition_sha256"] = canonical_sha256(transition)
    next_marker = deepcopy(dict(marker))
    next_marker["current_row_binding"] = deepcopy(dict(next_binding))
    next_marker["transitions"] = [*deepcopy(transitions), transition]
    next_marker["marker_sha256"] = canonical_sha256(
        {key: value for key, value in next_marker.items() if key != "marker_sha256"}
    )
    return next_marker if _marker_structure_is_valid(
        str(marker.get("candidate_id") or ""), next_marker
    ) else None


def _ledger_with_rebound_markers(
    state: Mapping[str, object], replacements: Mapping[str, Mapping[str, object]]
) -> dict[str, object] | None:
    try:
        entries = _recovery_ledger_entries(state)
    except PublishedTopicCollisionError:
        return None
    if not set(replacements).issubset(entries):
        return None
    next_entries = {key: deepcopy(dict(value)) for key, value in entries.items()}
    for candidate_id, marker in replacements.items():
        if not _marker_structure_is_valid(candidate_id, marker):
            return None
        next_entries[candidate_id] = deepcopy(dict(marker))
    ledger: dict[str, object] = {
        "schema_version": RECOVERY_LEDGER_SCHEMA,
        "entries": next_entries,
    }
    ledger["ledger_sha256"] = canonical_sha256(ledger)
    return ledger


def seal_published_topic_resolution_row_rebounds(
    state: dict,
    *,
    pre_state: Mapping[str, object],
    phase: str,
    candidate_ids: tuple[str, ...] | None = None,
    repo_root: Path = DEFAULT_REPO_ROOT,
    publication_registry: Mapping[str, object] | None = None,
) -> bool:
    """Seal allowed exact-row enrichment/movement without widening authority."""

    if phase not in RECOVERY_ROW_REBOUND_PHASES:
        return False
    try:
        entries = _recovery_ledger_entries(state)
        pre_entries = _recovery_ledger_entries(pre_state)
    except PublishedTopicCollisionError:
        return False
    if entries != pre_entries:
        return False
    selected_ids = set(entries) if candidate_ids is None else set(candidate_ids)
    if (
        any(_CANDIDATE_ID_RE.fullmatch(value) is None for value in selected_ids)
        or not selected_ids.issubset(entries)
    ):
        return False
    replacements: dict[str, Mapping[str, object]] = {}
    for candidate_id in selected_ids:
        marker = entries[candidate_id]
        valid, _queued, _current = _marker_is_valid(
            pre_state,
            candidate_id,
            marker,
            repo_root=repo_root,
            publication_registry=publication_registry,
        )
        before = _one_recovery_target_row(pre_state, candidate_id)
        after = _one_recovery_target_row(state, candidate_id)
        if not valid or before is None or after is None:
            return False
        before_collection, before_row = before
        after_collection, after_row = after
        if before_collection == after_collection and dict(before_row) == dict(after_row):
            continue
        changed_fields = _top_level_changed_fields(before_row, after_row)
        if not set(changed_fields).issubset(_RECOVERY_REBOUND_FIELDS[phase]):
            return False
        if phase == RECOVERY_REBOUND_SESSION_ANNOTATION:
            collections_valid = before_collection == after_collection
        elif phase == RECOVERY_REBOUND_PRODUCTION_PREPARE and (
            before_collection in _RECOVERY_QUEUE_COLLECTIONS
            and after_collection == "talk_below_confidence_threshold"
        ):
            collections_valid = True
        elif phase == RECOVERY_REBOUND_PRODUCTION_PREPARE:
            collections_valid = (
                before_collection in _RECOVERY_QUEUE_COLLECTIONS
                and after_collection in _RECOVERY_QUEUE_COLLECTIONS
            )
        else:
            collections_valid = (
                before_collection == after_collection
                and before_collection in _RECOVERY_QUEUE_COLLECTIONS
            )
        if not collections_valid or _recovery_identity_binding(before_row) != (
            _recovery_identity_binding(after_row)
        ):
            return False
        next_binding = {
            "collection": after_collection,
            "row_sha256": canonical_sha256(after_row),
        }
        terminal = after_collection == "talk_below_confidence_threshold"
        next_marker = _marker_with_transition(
            marker,
            transition_kind=(
                RECOVERY_QUEUE_TERMINAL_TRANSITION
                if terminal
                else RECOVERY_ROW_REBOUND_TRANSITION
            ),
            next_binding=next_binding,
            detail=(
                {"terminal_row_sha256": next_binding["row_sha256"]}
                if terminal
                else {"transition_phase": phase, "changed_fields": changed_fields}
            ),
        )
        if next_marker is None:
            return False
        replacements[candidate_id] = next_marker
    if not replacements:
        return True
    next_ledger = _ledger_with_rebound_markers(state, replacements)
    if next_ledger is None:
        return False
    state[RECOVERY_MARKER_FIELD] = next_ledger
    return True


def seal_published_topic_resolution_production_transition(
    state: dict,
    candidate_id: str,
    *,
    pre_state: Mapping[str, object],
    repo_root: Path = DEFAULT_REPO_ROOT,
    publication_registry: Mapping[str, object] | None = None,
) -> bool:
    """Seal the exact queued-row -> produced-pick transition before persistence."""

    try:
        entries = _recovery_ledger_entries(state)
        pre_entries = _recovery_ledger_entries(pre_state)
    except PublishedTopicCollisionError:
        return False
    marker = entries.get(candidate_id)
    before = _one_recovery_target_row(pre_state, candidate_id)
    after = _one_recovery_target_row(state, candidate_id)
    if marker is None or entries != pre_entries or before is None or after is None:
        return False
    valid, _queued, _current = _marker_is_valid(
        pre_state,
        candidate_id,
        marker,
        repo_root=repo_root,
        publication_registry=publication_registry,
    )
    before_collection, before_row = before
    after_collection, after_row = after
    if not valid or before_collection not in _RECOVERY_QUEUE_COLLECTIONS:
        return False
    if before_collection == after_collection and dict(before_row) == dict(after_row):
        return True
    if after_collection in _RECOVERY_QUEUE_COLLECTIONS:
        return seal_published_topic_resolution_row_rebounds(
            state,
            pre_state=pre_state,
            phase=RECOVERY_REBOUND_PRODUCTION_QUEUE_RETRY,
            candidate_ids=(candidate_id,),
            repo_root=repo_root,
            publication_registry=publication_registry,
        )
    if not (
        after_collection == "picks"
        and _recovery_identity_binding(before_row)
        == _recovery_identity_binding(after_row)
    ):
        return False
    next_binding = {
        "collection": "picks",
        "row_sha256": canonical_sha256(after_row),
    }
    next_marker = _marker_with_transition(
        marker,
        transition_kind=RECOVERY_PRODUCE_TRANSITION,
        next_binding=next_binding,
        detail={"produced_pick_sha256": next_binding["row_sha256"]},
    )
    if next_marker is None:
        return False
    next_ledger = _ledger_with_rebound_markers(
        state, {candidate_id: next_marker}
    )
    if next_ledger is None:
        return False
    state[RECOVERY_MARKER_FIELD] = next_ledger
    return True


def advance_published_topic_resolution_recovery(
    state: dict,
    candidate_id: str,
    *,
    pre_state: Mapping[str, object],
    from_collection: str,
    from_row: Mapping[str, object],
    to_collection: str,
    to_row: Mapping[str, object],
    transition_kind: str = RECOVERY_REQUEUE_TRANSITION,
    repo_root: Path = DEFAULT_REPO_ROOT,
    publication_registry: Mapping[str, object] | None = None,
) -> bool:
    """Seal one generic recoverable-pick -> queue transition into the ledger.

    The generic recovery lane already writes ``recovery_source_record_sha256``
    on the new queue row.  This function binds that exact failed pick and exact
    successor row to the prior sealed queue head before advancing it.  It never
    manufactures authority for an arbitrary queued row.
    """

    if (
        _CANDIDATE_ID_RE.fullmatch(str(candidate_id or "")) is None
        or from_collection != "picks"
        or to_collection not in {"pending_talk", "talk_backlog"}
        or transition_kind != RECOVERY_REQUEUE_TRANSITION
        or _candidate_id(from_row) != candidate_id
        or _candidate_id(to_row) != candidate_id
        or to_row.get("recovery_source_record_sha256") != canonical_sha256(from_row)
    ):
        return False
    try:
        entries = _recovery_ledger_entries(state)
        pre_entries = _recovery_ledger_entries(pre_state)
    except PublishedTopicCollisionError:
        return False
    marker = entries.get(candidate_id)
    if (
        marker is None
        or pre_entries != entries
        or not _marker_structure_is_valid(candidate_id, marker)
    ):
        return False
    queued, current = _target_rows(state, candidate_id)
    if (
        len(queued) != 1
        or current
        or queued[0][0] != to_collection
        or dict(queued[0][1]) != dict(to_row)
    ):
        return False
    original_hold = marker.get("original_hold")
    original_candidate = (
        original_hold.get("candidate") if isinstance(original_hold, Mapping) else None
    )
    if not (
        isinstance(original_candidate, Mapping)
        and _recovery_identity_binding(from_row)
        == _recovery_identity_binding(original_candidate)
        == _recovery_identity_binding(to_row)
    ):
        return False
    pre_queued, pre_current = _target_rows(pre_state, candidate_id)
    if (
        pre_queued
        or len(pre_current) != 1
        or pre_current[0][0] != from_collection
        or dict(pre_current[0][1]) != dict(from_row)
        or
        _inspect_released_recovery_entry(
            pre_state,
            candidate_id,
            marker,
            repo_root=repo_root,
            publication_registry=publication_registry,
        )
        != RECOVERY_RELEASED_RETRY_PENDING
    ):
        return False
    previous_binding = marker.get("current_row_binding")
    transitions = marker.get("transitions")
    if not isinstance(previous_binding, Mapping) or not isinstance(transitions, list):
        return False
    next_binding = {
        "collection": to_collection,
        "row_sha256": canonical_sha256(to_row),
    }
    transition: dict[str, object] = {
        "schema_version": RECOVERY_TRANSITION_SCHEMA,
        "transition_index": len(transitions),
        "transition_kind": transition_kind,
        "previous_row_binding": deepcopy(dict(previous_binding)),
        "failed_pick_sha256": canonical_sha256(from_row),
        "next_row_binding": next_binding,
    }
    transition["transition_sha256"] = canonical_sha256(transition)
    next_marker = deepcopy(dict(marker))
    next_marker["current_row_binding"] = next_binding
    next_marker["transitions"] = [*deepcopy(transitions), transition]
    next_marker["marker_sha256"] = canonical_sha256(
        {key: value for key, value in next_marker.items() if key != "marker_sha256"}
    )
    try:
        next_ledger = _ledger_with_replaced_marker(state, candidate_id, next_marker)
    except PublishedTopicCollisionError:
        return False
    state[RECOVERY_MARKER_FIELD] = next_ledger
    return True


def release_resolved_published_topic_hold(
    state: dict,
    candidate_id: str,
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
    publication_registry: Mapping[str, object] | None = None,
) -> bool:
    """Restore one validly resolved hold without touching any other candidate."""

    disposition = inspect_published_topic_resolution_recovery(
        state,
        candidate_id,
        repo_root=repo_root,
        publication_registry=publication_registry,
    )
    if disposition == RECOVERY_RELEASED_QUEUED:
        return True
    if disposition != RECOVERY_READY_TO_RELEASE:
        return False
    try:
        result = _resolved_target_shadow(
            state,
            candidate_id=candidate_id,
            repo_root=repo_root,
            publication_registry=publication_registry,
        )
    except (OSError, ValueError, TypeError):
        return False
    if result is None:
        return False
    _shadow, origin, restored = result
    current = state.get(REVIEW_STATE_FIELD)
    holds = current.get("holds") if isinstance(current, Mapping) else None
    if not isinstance(holds, list):
        return False
    matches = [
        hold for hold in holds if isinstance(hold, Mapping) and _candidate_id(hold) == candidate_id
    ]
    if len(matches) != 1:
        return False
    hold = matches[0]
    existing = [
        (field, row)
        for field in ("pending_talk", "talk_backlog")
        for row in (state.get(field) if isinstance(state.get(field), list) else [])
        if isinstance(row, Mapping) and _candidate_id(row) == candidate_id
    ]
    if len(existing) > 1 or (
        existing
        and (existing[0][0] != origin or dict(existing[0][1]) != restored)
    ):
        return False
    origin_rows = state.get(origin)
    if not isinstance(origin_rows, list):
        return False
    try:
        marker = _recovery_marker(
            candidate_id=candidate_id,
            queue_origin=origin,
            hold=hold,
            restored=restored,
            repo_root=repo_root,
        )
        next_ledger = _ledger_with_appended_marker(state, candidate_id, marker)
    except (OSError, UnicodeError, json.JSONDecodeError, PublishedTopicCollisionError):
        return False
    next_review = {
        **dict(current),
        "schema_version": REVIEW_STATE_SCHEMA,
        "holds": [hold for hold in holds if hold is not matches[0]],
    }
    next_origin = list(origin_rows)
    if not existing:
        next_origin.append(deepcopy(restored))
    state[REVIEW_STATE_FIELD] = next_review
    state[origin] = next_origin
    state[RECOVERY_MARKER_FIELD] = next_ledger
    return True


def hold_published_topic_collision_reviews(
    state: dict,
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
    publication_registry: Mapping[str, object] | None = None,
    candidate_ids: Collection[str] | None = None,
) -> list[dict[str, object]]:
    """Park every candidate with an active pair-review authority.

    Only an asset declared by ``HEAD`` or the commit-bound deployed manifest
    can create a new hold.  Once created, a hold is sticky: missing, malformed,
    or drifted authority bytes become ``...AUTHORITY_STALE`` and never silently
    release the candidate.
    """

    allowed = _normalized_candidate_allowlist(candidate_ids)
    prior_holds = _prior_holds_in_order(state)
    rows = _current_rows_by_id(state)
    prior_held = _held_candidates(state)
    prior_hold_records = _prior_hold_records(state)
    rows = {**prior_held, **rows}

    active_ids = {
        candidate_id
        for candidate_id in prior_held
        if allowed is None or candidate_id in allowed
    }
    for candidate_id in rows:
        if allowed is not None and candidate_id not in allowed:
            continue
        try:
            relative = authority_relative_path(candidate_id)
            if repository_authority_expects_asset(repo_root=repo_root, relative_path=relative):
                active_ids.add(candidate_id)
        except (PublishedTopicCollisionError, RepositoryAssetAuthorityError):
            if candidate_id in prior_held:
                active_ids.add(candidate_id)

    if not active_ids:
        holds = _merge_scoped_holds(
            prior_holds, [], candidate_ids=allowed
        )
        next_review = {
            "schema_version": REVIEW_STATE_SCHEMA,
            "holds": holds,
        }
        current_review = state.get(REVIEW_STATE_FIELD)
        if allowed is not None and isinstance(current_review, Mapping):
            next_review = {**dict(current_review), **next_review}
        state[REVIEW_STATE_FIELD] = next_review
        return holds

    registry = publication_registry
    registry_error: Exception | None = None
    if registry is None:
        # Passing the committed path intentionally excludes a runtime overlay:
        # the pair authority binds the exact repository row, not a mutable
        # post-publication cache projection.  An unreadable registry is
        # converted into a sticky stale hold for the scoped candidate rather
        # than crashing unrelated selection work.
        try:
            registry = load_publication_registry(DEFAULT_REGISTRY_PATH)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            registry = {"entries": []}
            registry_error = exc

    holds: list[dict[str, object]] = []
    for candidate_id in sorted(active_ids):
        candidate = rows.get(candidate_id)
        if candidate is None:
            # The prior row is retained inside the hold, so this is reachable
            # only for a corrupt pre-v1 state.  Keep a typed stale tombstone.
            holds.append(
                {
                    "candidate_id": candidate_id,
                    "disposition": STALE_REVIEW_STATUS,
                    "reason_code": "CANDIDATE_ROW_MISSING",
                    "candidate": {},
                }
            )
            continue
        relative = authority_relative_path(candidate_id)
        path = repo_root / relative
        disposition = REVIEW_STATUS
        reason_code = "HUMAN_ASSERTED_PUBLISHED_TOPIC_REVIEW_REQUIRED"
        evidence: dict[str, object] = {}
        try:
            observed = path.read_bytes()
            repository_binding = require_repository_asset_authority(
                repo_root=repo_root,
                relative_path=relative,
                observed_bytes=observed,
            )
            raw = json.loads(observed.decode("utf-8"))
            if not isinstance(raw, Mapping):
                raise PublishedTopicCollisionError("authority is not an object")
            published_binding = raw.get("published")
            published_id = (
                str(published_binding.get("candidate_id") or "")
                if isinstance(published_binding, Mapping)
                else ""
            )
            published = rows.get(published_id)
            if published is None:
                raise PublishedTopicCollisionError("published candidate state row is missing")
            if registry_error is not None:
                raise PublishedTopicCollisionError(
                    f"publication registry is unreadable: {registry_error}"
                )
            refreshed_resolution_relative = refreshed_resolution_relative_path(candidate_id)
            legacy_resolution_relative = resolution_relative_path(candidate_id)
            resolution_relative = (
                refreshed_resolution_relative
                if (repo_root / refreshed_resolution_relative).exists()
                else legacy_resolution_relative
            )
            resolution_path = repo_root / resolution_relative
            resolution: object | None = None
            candidate_authority_scorecard_sha256: str | None = None
            if resolution_path.exists():
                resolution_bytes = resolution_path.read_bytes()
                require_repository_asset_authority(
                    repo_root=repo_root,
                    relative_path=resolution_relative,
                    observed_bytes=resolution_bytes,
                )
                resolution = json.loads(resolution_bytes.decode("utf-8"))
                if (
                    isinstance(resolution, Mapping)
                    and resolution.get("schema_version") == REFRESHED_RESOLUTION_SCHEMA
                ):
                    refresh_binding = resolution.get("scorecard_refresh")
                    if not isinstance(refresh_binding, Mapping):
                        raise PublishedTopicCollisionError(
                            "resolution scorecard refresh binding is invalid"
                        )
                    candidate_authority_scorecard_sha256 = _require_sha(
                        refresh_binding.get("old_selection_scorecard_sha256"),
                        label="resolution old scorecard SHA-256",
                    )
            validated = _validate_authority(
                raw,
                candidate_row=candidate,
                published_row=published,
                publication_registry=registry,
                candidate_scorecard_sha256=candidate_authority_scorecard_sha256,
            )
            if resolution is not None:
                validate_resolution(
                    resolution,
                    candidate_row=candidate,
                    published_row=published,
                    validated_authority=validated,
                    authority_path=relative,
                    authority_bytes=observed,
                )
                _restore_resolved_hold(
                    state,
                    candidate_id=candidate_id,
                    prior_hold=prior_hold_records.get(candidate_id),
                )
                # This statement only releases selection/production.  Its
                # explicit upload_authorized=false is deliberately not
                # translated into a publication or manifest permission.
                continue
            evidence = {
                "authority": {
                    "relative_path": repository_binding.relative_path,
                    "file_sha256": repository_binding.file_sha256,
                    "commit": repository_binding.commit,
                    "mode": repository_binding.mode,
                    "authority_sha256": validated["authority_sha256"],
                },
                "authority_quote": validated["authority_quote"],
                "topic_review_fingerprint_sha256": validated["topic_review_fingerprint_sha256"],
                "published_candidate_id": published_id,
                "published_bvid": published_binding.get("bvid"),
                "published_public_title": published_binding.get("public_title"),
                "candidate_hook_sha256": validated["candidate"]["hook_sha256"],
                "candidate_scorecard_sha256": validated["candidate"]["selection_scorecard_sha256"],
            }
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            PublishedTopicCollisionError,
            RepositoryAssetAuthorityError,
        ) as exc:
            disposition = STALE_REVIEW_STATUS
            reason_code = "PUBLISHED_TOPIC_REVIEW_AUTHORITY_STALE"
            evidence = {"error": f"{type(exc).__name__}: {exc}"}

        origin = _queue_origin(state, candidate_id)
        _remove_from_queue(state, candidate_id)
        candidate_copy = dict(candidate)
        holds.append(
            {
                "candidate_id": candidate_id,
                "disposition": disposition,
                "reason_code": reason_code,
                "score_mutated": False,
                "suppression_authorized": False,
                "upload_authorized": False,
                "queue_origin": origin,
                "candidate": candidate_copy,
                "evidence": evidence,
            }
        )

    holds = _merge_scoped_holds(
        prior_holds, holds, candidate_ids=allowed
    )
    next_review = {
        "schema_version": REVIEW_STATE_SCHEMA,
        "holds": holds,
    }
    current_review = state.get(REVIEW_STATE_FIELD)
    if allowed is not None and isinstance(current_review, Mapping):
        next_review = {**dict(current_review), **next_review}
    state[REVIEW_STATE_FIELD] = next_review
    return holds
