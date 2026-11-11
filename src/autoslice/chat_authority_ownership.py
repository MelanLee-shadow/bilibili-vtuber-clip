"""Chat-authority-owned interval enforcement for exact-final CPA self-heal.

``chat_authority.apply_authoritative_chat_evidence`` writes ``applied`` rows
whenever a cue's text is replaced verbatim by matched danmaku/SC evidence
(``exact_span``/``question_particle_patch``), but until this module existed
nothing stopped the exact-final CPA self-heal channel (inside
``producer_package_finalization._run_exact_final_review_gate``) from
independently re-listening to the exact same window and silently "correcting"
already-verbatim chat text — the 主包/主播 shape: cue 1:12 of
auto_173005_934_1166 read danmaku 「主包给…」 (a common livestream meme
spelling of 主播) verbatim into the chat-authority-applied cue, and a later
self-heal pass rewrote it to 主播给 because a general-text judge, unaware the
span was already chat-verbatim-owned, treated the meme spelling as an ASR
typo. 公开规则ruling #2: 「弹幕会有梗，有刻意的错写，因此只要识别出是
弹幕就不应该改」.

This is the same architecture gap ``redelivery_baseline_ownership.py`` closed
for 公开规则-reviewed baseline diffs (zsm8 case) — a second, different
authority source with the identical unread-owned-interval shape. This module
mirrors that fix: it is the single place that consults chat-authority
``applied`` rows before the self-heal apply step. Ordinary findings whose
target cue falls inside one are suppressed with a typed
``CHAT_AUTHORITY_OWNED_CUE_SELF_HEAL_SUPPRESSED`` receipt instead of a silent
drop; authenticated postburn witness rows remain pending for the typed
terminal consumer while their mutation authority stays vetoed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Mapping, Sequence, TypedDict, cast

from .jingting_chunker import parse_srt_cues
from .witness_disagreement import PROVENANCE_KIND as POSTBURN_WITNESS_PROVENANCE_KIND

_VERBATIM_CHAT_MODES = frozenset({"exact_span", "question_particle_patch"})
_CHAT_AUTHORITY_PROJECTION_SCHEMA = "chat-authority-conflict-projection.v1"


class ChatAuthoritySource(TypedDict):
    path: str
    sha256: str
    event_id: str
    offset_ms: int


class ChatCopyGate(TypedDict, total=False):
    schema_version: str
    status: str
    proof_basis: str
    owner_eligible: bool
    full_span_audio_verdict: bool
    full_span_cpa_text_verdict: bool
    full_span_cpa_witness_verdict: bool
    independent_owner_support_count: int


class ChatAuthorityDecision(TypedDict):
    kind: str
    decision_authority: str | None
    witness_authority: str
    witness_status: str
    needs_audio: bool | None
    witness_protocol: str | None
    whole_line_exact_copy_gate: ChatCopyGate | None


class ChatAuthorityProjection(TypedDict, total=False):
    """Serialized evidence handoff; the validator distinguishes BOUND/INVALID."""

    schema_version: str
    status: Literal["BOUND", "INVALID"]
    invalid_reason: str
    applied_index: int
    applied_evidence_id: str
    applied_row_sha256: str
    source: ChatAuthoritySource
    exact_text: str
    before: list[str]
    after: list[str]
    mode: str
    kind: str
    cue_index: int
    cue_indexes: list[int]
    source_cue_position: int
    matched_start_ms: int
    matched_end_ms: int
    delivery_start_ms: int
    cue_start_ms: int | None
    cue_end_ms: int | None
    base_text_sha256: str
    current_srt_sha256: str
    authority: ChatAuthorityDecision
    mutation_authorized: Literal[False]
    projection_sha256: str


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _valid_hex_digest(value: object, *, prefix: str = "") -> bool:
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    value = value[len(prefix):]
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _projection_digest(projection: object) -> str:
    if not isinstance(projection, Mapping):
        raise ValueError("CHAT_AUTHORITY_CONFLICT_PROJECTION_INVALID")
    unsigned = {key: value for key, value in projection.items()
                if key != "projection_sha256"}
    return _canonical_sha(unsigned)


def _invalid_chat_authority_projection(
    *, final_text: str, cue_index: int, delivery_start_ms: int, reason: str,
    cue: object | None = None,
) -> ChatAuthorityProjection:
    projection: ChatAuthorityProjection = {
        "schema_version": _CHAT_AUTHORITY_PROJECTION_SCHEMA,
        "status": "INVALID",
        "invalid_reason": reason,
        "cue_index": cue_index,
        "cue_start_ms": getattr(cue, "start_ms", None),
        "cue_end_ms": getattr(cue, "end_ms", None),
        "delivery_start_ms": delivery_start_ms,
        "current_srt_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
        "mutation_authorized": False,
    }
    projection["projection_sha256"] = _projection_digest(projection)
    return projection


def build_chat_authority_conflict_projection(
    final_text: str,
    chat_authority_audit: object,
    *,
    cue_index: int,
    delivery_start_ms: int,
) -> ChatAuthorityProjection | None:
    """Project one real applied chat receipt into terminal evidence.

    The projection is deliberately evidence-only.  It contains enough of the
    applied row and its read-aloud receipt to bind a terminal question, while
    the original audit remains the mutation guard.  A malformed row becomes an
    explicitly invalid projection rather than silently disappearing; the
    request builder then rejects it and the existing protection remains in
    force.
    """
    if (
        not isinstance(final_text, str)
        or not isinstance(chat_authority_audit, Mapping)
        or type(cue_index) is not int
        or cue_index <= 0
        or type(delivery_start_ms) is not int
        or delivery_start_ms < 0
    ):
        return None
    # Finding/cue indexes are the original SRT indexes; do not renumber them
    # by dropping empty cues before the target.
    cues = list(parse_srt_cues(final_text))
    if cue_index > len(cues):
        return None
    cue = cues[cue_index - 1]
    rows = chat_authority_audit.get("applied")
    if not isinstance(rows, list):
        return _invalid_chat_authority_projection(
            final_text=final_text, cue_index=cue_index,
            delivery_start_ms=delivery_start_ms, reason="APPLIED_LEDGER_MISSING", cue=cue,
        )
    expected_start = cue.start_ms + delivery_start_ms
    expected_end = cue.end_ms + delivery_start_ms
    from .final_source_language_owner import (
        validated_exact_read_superseded_applied_indexes,
    )
    retired_indexes = validated_exact_read_superseded_applied_indexes(
        chat_authority_audit,
        current_srt_text=final_text,
        delivery_start_ms=delivery_start_ms,
    )
    from .chat_authority_reconciliation import validated_retired_chat_rows
    retired_indexes |= validated_retired_chat_rows(chat_authority_audit,
        current_srt_text=final_text, delivery_start_ms=delivery_start_ms)
    all_candidates = [
        (index, row) for index, row in enumerate(rows)
        if isinstance(row, Mapping)
        and row.get("mode") in _VERBATIM_CHAT_MODES
        and isinstance(row.get("cue_indexes"), list)
        and row.get("matched_start_ms") == expected_start
        and row.get("matched_end_ms") == expected_end
    ]
    candidates = [
        (index, row) for index, row in all_candidates
        if index not in retired_indexes
    ]
    if not candidates and any(index in retired_indexes for index, _ in all_candidates):
        # A valid existing source/foreign owner has already superseded this
        # exact-read row.  It is intentionally absent from the conflict
        # projection; the owner validator, not generic terminal, authorized
        # the transition.
        return None
    if len(candidates) != 1:
        return _invalid_chat_authority_projection(
            final_text=final_text, cue_index=cue_index,
            delivery_start_ms=delivery_start_ms,
            reason=("APPLIED_ROW_MISSING" if not candidates else "APPLIED_ROW_AMBIGUOUS"),
            cue=cue,
        )
    applied_index, row = candidates[0]
    indexes = row.get("cue_indexes")
    exact_text = row.get("exact_text")
    before = row.get("before")
    after = row.get("after")
    source = row.get("source")
    source_sha256 = row.get("source_sha256")
    source_event_id = row.get("source_event_id")
    source_offset_ms = row.get("source_offset_ms")
    evidence_id = row.get("evidence_id")
    verdict = row.get("read_aloud_verdict")
    gate = row.get("whole_line_exact_copy_gate")
    authority_kind = (
        verdict.get("authority_kind") if isinstance(verdict, Mapping)
        else row.get("authority_kind")
    )
    witness_authority = (
        verdict.get("witness_authority") if isinstance(verdict, Mapping)
        else row.get("witness_authority")
    )
    witness_status = (
        verdict.get("witness_status") if isinstance(verdict, Mapping)
        else row.get("witness_status")
    )
    valid_indexes = (
        isinstance(indexes, list)
        and bool(indexes)
        and all(type(index) is int and index > 0 for index in indexes)
        and (cue_index in indexes or len(indexes) == 1)
    )
    # A boundary trim renumbers delivery cues.  For a single source cue,
    # exact physical geometry and the text below identify its only position;
    # multi-cue rows still require the original index until they have an
    # independently bound per-cue geometry.
    cue_position = (indexes.index(cue_index) if cue_index in indexes else 0) if valid_indexes else -1
    valid = (
        row.get("mode") in _VERBATIM_CHAT_MODES
        and valid_indexes
        and type(row.get("matched_start_ms")) is int
        and type(row.get("matched_end_ms")) is int
        and row["matched_end_ms"] > row["matched_start_ms"]
        and isinstance(evidence_id, str)
        and _valid_hex_digest(evidence_id)
        and isinstance(source, str) and bool(source)
        and isinstance(source_sha256, str) and _valid_hex_digest(source_sha256)
        and isinstance(source_event_id, str)
        and type(source_offset_ms) is int and source_offset_ms >= 0
        and isinstance(exact_text, str) and bool(exact_text)
        and isinstance(before, list) and len(before) == len(indexes)
        and all(isinstance(text, str) for text in before)
        and isinstance(after, list) and len(after) == len(indexes)
        and all(isinstance(text, str) for text in after)
        and cue_position >= 0 and after[cue_position] == cue.text
        and isinstance(authority_kind, str) and bool(authority_kind)
        and isinstance(witness_authority, str) and bool(witness_authority)
        and isinstance(witness_status, str) and bool(witness_status)
    )
    if not valid:
        return _invalid_chat_authority_projection(
            final_text=final_text, cue_index=cue_index,
            delivery_start_ms=delivery_start_ms,
            reason="APPLIED_RECEIPT_BINDING_INVALID", cue=cue,
        )
    gate_projection = None
    if isinstance(gate, Mapping):
        gate_projection = {
            key: gate.get(key)
            for key in (
                "schema_version", "status", "proof_basis", "owner_eligible",
                "full_span_audio_verdict", "full_span_cpa_text_verdict",
                "full_span_cpa_witness_verdict", "independent_owner_support_count",
            )
            if key in gate
        }
    projection: ChatAuthorityProjection = {
        "schema_version": _CHAT_AUTHORITY_PROJECTION_SCHEMA,
        "status": "BOUND",
        "applied_index": applied_index,
        "applied_evidence_id": evidence_id,
        "applied_row_sha256": _canonical_sha(dict(row)),
        "source": {
            "path": source,
            "sha256": source_sha256,
            "event_id": source_event_id,
            "offset_ms": source_offset_ms,
        },
        "exact_text": exact_text,
        "before": list(before),
        "after": list(after),
        "mode": row["mode"],
        "kind": row.get("kind"),
        "cue_index": cue_index,
        "cue_indexes": list(indexes),
        "matched_start_ms": row["matched_start_ms"],
        "matched_end_ms": row["matched_end_ms"],
        "delivery_start_ms": delivery_start_ms,
        "cue_start_ms": cue.start_ms,
        "cue_end_ms": cue.end_ms,
        "base_text_sha256": hashlib.sha256(cue.text.encode("utf-8")).hexdigest(),
        "current_srt_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
        "authority": {
            "kind": authority_kind,
            "decision_authority": (
                verdict.get("decision_authority") if isinstance(verdict, Mapping)
                else row.get("decision_authority")
            ),
            "witness_authority": witness_authority,
            "witness_status": witness_status,
            "needs_audio": verdict.get("needs_audio") if isinstance(verdict, Mapping) else None,
            "witness_protocol": verdict.get("witness_protocol") if isinstance(verdict, Mapping) else None,
            "whole_line_exact_copy_gate": gate_projection,
        },
        "mutation_authorized": False,
    }
    if cue_index not in indexes:
        projection["source_cue_position"] = cue_position
    projection["projection_sha256"] = _projection_digest(projection)
    try:
        # One definition serves both writer and reader admission.  Do not
        # attach the audit here: the reader uses this builder to compare it.
        validate_chat_authority_conflict_projection(
            projection, srt_text=final_text, delivery_start_ms=delivery_start_ms,
        )
    except ValueError:
        return _invalid_chat_authority_projection(
            final_text=final_text, cue_index=cue_index,
            delivery_start_ms=delivery_start_ms,
            reason="APPLIED_RECEIPT_BINDING_INVALID", cue=cue,
        )
    return projection


def validate_chat_authority_conflict_projection(
    projection: object,
    *,
    srt_text: str,
    finding: object = None,
    delivery_start_ms: int | None = None,
    chat_authority_audit: object = None,
) -> None:
    """Validate the evidence-only handoff without granting mutation authority."""
    if (
        not isinstance(projection, Mapping)
        or projection.get("schema_version") != _CHAT_AUTHORITY_PROJECTION_SCHEMA
        or projection.get("status") != "BOUND"
        or projection.get("mutation_authorized") is not False
        or projection.get("projection_sha256") != _projection_digest(projection)
    ):
        raise ValueError("CHAT_AUTHORITY_CONFLICT_PROJECTION_INVALID")
    if not isinstance(srt_text, str):
        raise ValueError("CHAT_AUTHORITY_CONFLICT_SRT_INVALID")
    if delivery_start_ms is None:
        delivery_start_ms = projection.get("delivery_start_ms")
    if type(delivery_start_ms) is not int or delivery_start_ms < 0:
        raise ValueError("CHAT_AUTHORITY_CONFLICT_OFFSET_INVALID")
    cue_index = projection.get("cue_index")
    if type(cue_index) is not int or cue_index <= 0:
        raise ValueError("CHAT_AUTHORITY_CONFLICT_CUE_INVALID")
    cues = list(parse_srt_cues(srt_text))
    if cue_index > len(cues):
        raise ValueError("CHAT_AUTHORITY_CONFLICT_CUE_INVALID")
    cue = cues[cue_index - 1]
    if (
        projection.get("delivery_start_ms") != delivery_start_ms
        or projection.get("cue_start_ms") != cue.start_ms
        or projection.get("cue_end_ms") != cue.end_ms
        or projection.get("matched_start_ms") != cue.start_ms + delivery_start_ms
        or projection.get("matched_end_ms") != cue.end_ms + delivery_start_ms
        or projection.get("base_text_sha256") != hashlib.sha256(cue.text.encode("utf-8")).hexdigest()
        or projection.get("current_srt_sha256") != hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    ):
        raise ValueError("CHAT_AUTHORITY_CONFLICT_GEOMETRY_OR_SRT_DRIFT")
    indexes = projection.get("cue_indexes")
    before = projection.get("before")
    after = projection.get("after")
    source = projection.get("source")
    authority = projection.get("authority")
    exact_text = projection.get("exact_text")
    source_position = projection.get("source_cue_position")
    if source_position is None and isinstance(indexes, list) and cue_index in indexes:
        source_position = indexes.index(cue_index)
    if (
        not isinstance(indexes, list)
        or not indexes
        or any(type(index) is not int or index <= 0 for index in indexes)
        or len(set(indexes)) != len(indexes)
        or type(source_position) is not int
        or not 0 <= source_position < len(indexes)
        or (cue_index not in indexes and (len(indexes) != 1 or source_position != 0))
        or (cue_index in indexes and source_position != indexes.index(cue_index))
        or not isinstance(before, list) or len(before) != len(indexes)
        or not isinstance(after, list) or len(after) != len(indexes)
        or any(not isinstance(text, str) for text in (*before, *after))
        or after[source_position] != cue.text
        or projection.get("mode") not in _VERBATIM_CHAT_MODES
        or projection.get("kind") not in {"danmaku", "superchat"}
        or not isinstance(exact_text, str)
        or not exact_text
        or exact_text not in after[source_position]
        or not isinstance(source, Mapping)
        or not isinstance(source.get("path"), str) or not source["path"]
        or not _valid_hex_digest(source.get("sha256"))
        or not isinstance(source.get("event_id"), str)
        or type(source.get("offset_ms")) is not int or source["offset_ms"] < 0
        or not isinstance(projection.get("applied_evidence_id"), str)
        or not _valid_hex_digest(projection.get("applied_evidence_id"))
        or not _valid_hex_digest(projection.get("applied_row_sha256"))
        or type(projection.get("applied_index")) is not int
        or projection["applied_index"] < 0
        or not isinstance(authority, Mapping)
        or not isinstance(authority.get("kind"), str) or not authority["kind"]
        or not isinstance(authority.get("witness_authority"), str)
        or not isinstance(authority.get("witness_status"), str)
    ):
        raise ValueError("CHAT_AUTHORITY_CONFLICT_RECEIPT_INVALID")
    if isinstance(finding, Mapping):
        if finding.get("cue_index") not in {None, cue_index}:
            raise ValueError("CHAT_AUTHORITY_CONFLICT_FINDING_CUE_INVALID")
        if finding.get("base_text_sha256") not in {
            None, projection.get("base_text_sha256")
        }:
            raise ValueError("CHAT_AUTHORITY_CONFLICT_FINDING_BASE_STALE")
    if chat_authority_audit is not None:
        expected = build_chat_authority_conflict_projection(
            srt_text, chat_authority_audit, cue_index=cue_index,
            delivery_start_ms=delivery_start_ms,
        )
        if expected != dict(projection):
            raise ValueError("CHAT_AUTHORITY_CONFLICT_RECEIPT_DRIFT")


def bound_chat_authority_conflict_projection(
    projection: object, *, srt_text: str,
    finding: object, delivery_start_ms: int,
) -> ChatAuthorityProjection:
    """Validate one ordinary request handoff and detach its small receipt."""
    from copy import deepcopy
    from .review_errors import ContractRejection
    if not isinstance(projection, Mapping):
        raise ContractRejection("CHAT_AUTHORITY_CONFLICT_PROJECTION_INVALID")
    try:
        validate_chat_authority_conflict_projection(
            projection, srt_text=srt_text, finding=finding,
            delivery_start_ms=delivery_start_ms,
        )
    except ValueError as exc:
        raise ContractRejection(str(exc)) from exc
    return cast(ChatAuthorityProjection, deepcopy(dict(projection)))


def _chat_authority_owned_intervals(
    chat_authority_audit: Mapping[str, object] | None,
    *,
    delivery_start_ms: int,
    final_text: str | None = None,
) -> list[tuple[int, int]]:
    """Rebase globally-timed ``applied`` windows into delivery-local ms.

    Chat authority runs on the full-session/global timeline (like every other
    ``matched_start_ms``/``matched_end_ms`` field this repo produces before
    boundary trimming — see ``producer_text_finalization.
    _exact_final_cpa_retires_decision_row`` doing the same ``matched_start -
    delivery_start_ms`` rebase), but ``final_text`` here is the delivered
    clip's own local-time SRT. Comparing global windows against local cue
    timestamps without this offset silently never matches for any clip that
    does not start at absolute zero.
    """

    if not isinstance(chat_authority_audit, Mapping):
        return []
    raw = chat_authority_audit.get("applied")
    if not isinstance(raw, list):
        return []
    retired_indexes = frozenset()
    if isinstance(final_text, str):
        from .final_source_language_owner import (
            validated_exact_read_superseded_applied_indexes,
        )
        retired_indexes = validated_exact_read_superseded_applied_indexes(
            chat_authority_audit,
            current_srt_text=final_text,
            delivery_start_ms=delivery_start_ms,
        )
        from .chat_authority_reconciliation import validated_retired_chat_rows
        retired_indexes |= validated_retired_chat_rows(chat_authority_audit,
            current_srt_text=final_text, delivery_start_ms=delivery_start_ms)
    intervals: list[tuple[int, int]] = []
    for row_index, row in enumerate(raw):
        if row_index in retired_indexes:
            continue
        if not isinstance(row, Mapping) or row.get("mode") not in _VERBATIM_CHAT_MODES:
            continue
        start_ms = row.get("matched_start_ms")
        end_ms = row.get("matched_end_ms")
        if (
            isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
        ):
            continue
        local_start = start_ms - delivery_start_ms
        local_end = end_ms - delivery_start_ms
        if local_start < 0:
            continue
        intervals.append((local_start, local_end))
    return intervals


def _matching_owned_interval(
    start_ms: int,
    end_ms: int,
    owned_intervals: Sequence[tuple[int, int]],
) -> tuple[int, int] | None:
    for interval_start, interval_end in owned_intervals:
        if start_ms >= interval_start and end_ms <= interval_end:
            return (interval_start, interval_end)
    return None


def chat_authority_owned_cue_intervals(
    final_text: str,
    chat_authority_audit: object,
    *,
    delivery_start_ms: int,
) -> dict[int, tuple[int, int]]:
    """Return the delivered cue indexes covered by verbatim chat authority.

    This is the same rebased interval definition used by the suppression
    boundary.  Callers that apply a later typed repair use this projection as
    a protection set, so the pre-terminal and post-terminal apply paths cannot
    silently grow a second, divergent interpretation of ``applied`` rows.
    """
    if not isinstance(chat_authority_audit, Mapping):
        return {}
    owned_intervals = _chat_authority_owned_intervals(
        chat_authority_audit, delivery_start_ms=delivery_start_ms,
        final_text=final_text,
    )
    if not owned_intervals:
        return {}
    owned_cues: dict[int, tuple[int, int]] = {}
    for cue_index, cue in enumerate(parse_srt_cues(final_text), start=1):
        interval = _matching_owned_interval(
            cue.start_ms, cue.end_ms, owned_intervals
        )
        if interval is not None:
            owned_cues[cue_index] = interval
    return owned_cues


def chat_authority_owned_cue_indexes(
    final_text: str,
    chat_authority_audit: object,
    *,
    delivery_start_ms: int,
) -> frozenset[int]:
    """Return cue indexes protected by the shared chat-authority definition."""
    return frozenset(
        chat_authority_owned_cue_intervals(
            final_text,
            chat_authority_audit,
            delivery_start_ms=delivery_start_ms,
        )
    )


def protected_review_cue_indexes(
    final_text: str,
    human_alignment: object,
    chat_authority_audit: object,
    *,
    human_offset_ms: int,
    chat_offset_ms: int,
) -> frozenset[int]:
    """Keep both existing ownership sources immutable across terminal review."""
    from .human_sequence_alignment import protected_indexes

    if not isinstance(human_alignment, dict):
        raise TypeError("human sequence alignment must be an object")
    return frozenset(protected_indexes(
        human_alignment, final_text, timeline_offset_ms=human_offset_ms
    )) | chat_authority_owned_cue_indexes(
        final_text, chat_authority_audit, delivery_start_ms=chat_offset_ms
    )


def _postburn_finding_identity(finding: object) -> tuple[int, str, str, str | None, str] | None:
    if not isinstance(finding, Mapping):
        return None
    cue_index = finding.get("cue_index", finding.get("cue"))
    if type(cue_index) is not int:
        return None
    proposed = finding.get("proposed_full_cue")
    if proposed is not None and not isinstance(proposed, str):
        return None
    return (
        cue_index,
        str(finding.get("base_text_sha256") or "").removeprefix("sha256:"),
        str(finding.get("suspect") or ""),
        proposed,
        str(finding.get("repair_class") or ""),
    )


def _postburn_priority_identities(
    final_text: str,
    chat_authority_audit: object,
) -> frozenset[tuple[int, str, str, str | None, str]]:
    """Read authenticated full postburn rows when provenance is absent.

    Disclosure-only postburn rows legitimately have no candidate provenance.
    The completed, current-byte postburn receipt is their existing authority;
    it lets this boundary preserve the question without inventing a trust flag.
    A persisted content-addressed reference has no rows and therefore cannot
    classify unrelated findings.
    """
    if not isinstance(chat_authority_audit, Mapping):
        return frozenset()
    expected_srt_sha = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
    nested = chat_authority_audit.get("final_review_audit")
    identities: set[tuple[int, str, str, str | None, str]] = set()
    seen_receipts: set[int] = set()
    for container in (chat_authority_audit, nested):
        if not isinstance(container, Mapping):
            continue
        receipt = container.get("postburn_subtitle_audio_review")
        if not isinstance(receipt, Mapping) or id(receipt) in seen_receipts:
            continue
        seen_receipts.add(id(receipt))
        if (
            receipt.get("schema_version") != "postburn-subtitle-audio-review.v1"
            or receipt.get("status") != "FLAGGED"
            or receipt.get("release_gate") != "BLOCK"
            or str(receipt.get("reviewed_srt_sha256") or "").removeprefix("sha256:")
            != expected_srt_sha
        ):
            continue
        rows = receipt.get("findings")
        if not isinstance(rows, list):
            continue
        for row in rows:
            identity = _postburn_finding_identity(row)
            if identity is not None:
                identities.add(identity)
    return frozenset(identities)


def _is_postburn_witness_finding(
    finding: object,
    postburn_priority_identities: frozenset[tuple[int, str, str, str | None, str]],
    expected_srt_sha256: str,
) -> bool:
    """Recognize real auditor provenance or its authenticated disclosure row."""
    if not isinstance(finding, Mapping):
        return False
    provenance = finding.get("candidate_provenance")
    return (
        isinstance(provenance, Mapping)
        and provenance.get("kind") == POSTBURN_WITNESS_PROVENANCE_KIND
        and str(provenance.get("current_srt_sha256") or "").removeprefix(
            "sha256:"
        )
        == expected_srt_sha256
    ) or _postburn_finding_identity(finding) in postburn_priority_identities


def suppress_chat_authority_owned_self_heal_findings(
    final_text: str,
    audit: object,
    chat_authority_audit: Mapping[str, object] | None,
    delivery_start_ms: int,
) -> list[dict[str, object]]:
    """Refuse to let exact-final self-heal re-adjudicate a chat-owned cue.

    A normal self-heal finding whose target cue falls fully inside a verbatim
    chat-authority-applied window is dropped here — before
    ``_apply_exact_final_cpa_repairs`` ever sees it — with a typed
    ``CHAT_AUTHORITY_OWNED_CUE_SELF_HEAL_SUPPRESSED`` receipt.  A postburn
    witness disagreement is different: the exact auditor's existing
    ``candidate_provenance.kind`` identifies it as evidence-only priority
    material.  Its repair authority is still suppressed, but the finding is
    retained for the ordinary typed terminal consumer.  Release-gate fields
    are recomputed only when ordinary suppression is the reason the finding
    list is now empty; a retained postburn row remains a block.
    """
    owned_cue_intervals = chat_authority_owned_cue_intervals(
        final_text, chat_authority_audit, delivery_start_ms=delivery_start_ms
    )
    if not isinstance(audit, dict) or not owned_cue_intervals:
        return []
    findings = audit.get("findings")
    if not isinstance(findings, list) or not findings:
        return []
    postburn_priority_identities = _postburn_priority_identities(
        final_text, chat_authority_audit
    )
    expected_srt_sha256 = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
    cues = parse_srt_cues(final_text)
    kept: list[object] = []
    ownership_receipts: list[dict[str, object]] = []
    for finding in findings:
        cue_index = (
            finding.get("cue_index") if isinstance(finding, Mapping) else None
        )
        interval = (
            owned_cue_intervals.get(cue_index)
            if isinstance(cue_index, int) and not isinstance(cue_index, bool)
            else None
        )
        if interval is None:
            kept.append(finding)
            continue
        receipt = {
            "schema_version": "chat-authority-owned-cue-self-heal-suppressed.v1",
            "reason_code": "CHAT_AUTHORITY_OWNED_CUE_SELF_HEAL_SUPPRESSED",
            "cue_index": cue_index,
            "matched_start_ms": (
                cues[cue_index - 1].start_ms
                if isinstance(cue_index, int)
                and not isinstance(cue_index, bool)
                and 1 <= cue_index <= len(cues)
                else interval[0]
            ),
            "matched_end_ms": (
                cues[cue_index - 1].end_ms
                if isinstance(cue_index, int)
                and not isinstance(cue_index, bool)
                and 1 <= cue_index <= len(cues)
                else interval[1]
            ),
            "owned_interval": {
                "start_ms": interval[0],
                "end_ms": interval[1],
            },
            "suppressed_finding_sha256": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    finding,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
        if _is_postburn_witness_finding(
            finding, postburn_priority_identities, expected_srt_sha256
        ):
            # This receipt records the vetoed mutation authority; the finding
            # itself remains pending so the typed terminal consumer can make
            # (or preserve) an evidence-only decision.
            receipt.update(
                {
                    "status": "PENDING",
                    "finding_preserved": True,
                    "mutation_authorized": False,
                    "pending_consumer": "exact_final_terminal_funnel",
                }
            )
            kept.append(finding)
        ownership_receipts.append(receipt)
    if not ownership_receipts:
        return []
    audit["findings"] = kept
    audit["validated_finding_count"] = len(kept)
    discovery = audit.get("discovery")
    if isinstance(discovery, dict):
        discovery["explicit_empty_findings"] = not kept
    existing = audit.get("chat_authority_owned_findings_suppressed")
    audit["chat_authority_owned_findings_suppressed"] = [
        *(existing if isinstance(existing, list) else []),
        *ownership_receipts,
    ]
    if not kept:
        from .finding_lifecycle import is_finding_failure
        reason_codes = [
            code
            for code in (audit.get("reason_codes") or [])
            if not is_finding_failure(code) and code
            not in {
                "CPA_CYCLE_ADJUDICATION_UNAVAILABLE",
                "CPA_CYCLE_ADJUDICATION_INVALID",
            }
        ]
        audit["reason_codes"] = reason_codes
        if not reason_codes:
            audit["status"] = "CLEAN"
            audit["release_gate"] = "PASS"
    return ownership_receipts
