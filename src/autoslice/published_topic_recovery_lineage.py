"""Pure schema rules for published-topic recovery row lineage."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from src.autoslice.published_topic_final_review_handoff import (
    HANDOFF_TRANSITION_KIND,
    validate_handoff_transition_structure,
)


RECOVERY_TRANSITION_SCHEMA = "published-topic-resolution-recovery-transition.v2"
RECOVERY_ROW_REBOUND_TRANSITION = "CURRENT_ROW_REBOUND"
RECOVERY_PRODUCE_TRANSITION = "QUEUED_ROW_PRODUCED_PICK"
RECOVERY_QUEUE_TERMINAL_TRANSITION = "QUEUED_ROW_TERMINATED"
RECOVERY_REQUEUE_TRANSITION = "RECOVERABLE_FAILED_PICK_REQUEUED"
RECOVERY_REBOUND_SESSION_ANNOTATION = "SESSION_ANNOTATION"
RECOVERY_REBOUND_SEMANTIC_SCORECARD_REFRESH = "SEMANTIC_SCORECARD_REFRESH"
RECOVERY_REBOUND_PRODUCTION_PREPARE = "PRODUCTION_PREPARE"
RECOVERY_REBOUND_PRODUCTION_QUEUE_RETRY = "PRODUCTION_QUEUE_RETRY"
RECOVERY_ROW_REBOUND_PHASES = frozenset(
    {
        RECOVERY_REBOUND_SESSION_ANNOTATION,
        RECOVERY_REBOUND_SEMANTIC_SCORECARD_REFRESH,
        RECOVERY_REBOUND_PRODUCTION_PREPARE,
        RECOVERY_REBOUND_PRODUCTION_QUEUE_RETRY,
    }
)
RECOVERY_QUEUE_COLLECTIONS = frozenset({"pending_talk", "talk_backlog"})
RECOVERY_CURRENT_COLLECTIONS = frozenset(
    {"picks", "talk_below_confidence_threshold"}
)
RECOVERY_ROW_COLLECTIONS = RECOVERY_QUEUE_COLLECTIONS | RECOVERY_CURRENT_COLLECTIONS
RECOVERY_REBOUND_FIELDS = {
    RECOVERY_REBOUND_SESSION_ANNOTATION: frozenset(
        {"segment_scene_context", "session_relation_authority", "session_id"}
    ),
    RECOVERY_REBOUND_SEMANTIC_SCORECARD_REFRESH: frozenset(
        {"selection_scorecard", "semantic_evidence_scorecard_refresh"}
    ),
    RECOVERY_REBOUND_PRODUCTION_PREPARE: frozenset(
        {
            "cover_diversity_slot",
            "speaker_routing_claim",
            "speaker_routing_claim_sha256",
            "speaker_routing_candidate",
            "song_name_candidates",
        }
    ),
    RECOVERY_REBOUND_PRODUCTION_QUEUE_RETRY: frozenset({"title_attempts"}),
}

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def recovery_row_binding_is_valid(binding: object) -> bool:
    return bool(
        isinstance(binding, Mapping)
        and set(binding) == {"collection", "row_sha256"}
        and binding.get("collection") in RECOVERY_ROW_COLLECTIONS
        and _SHA256_RE.fullmatch(str(binding.get("row_sha256") or ""))
    )


def top_level_changed_fields(
    before: Mapping[str, object], after: Mapping[str, object]
) -> list[str]:
    return sorted(
        key
        for key in set(before) | set(after)
        if key not in before or key not in after or before[key] != after[key]
    )


def validated_transition_next_binding(
    transition: object,
    *,
    index: int,
    expected_previous: Mapping[str, object],
    canonical_sha256: Callable[[object], str],
) -> dict[str, object] | None:
    """Validate one kind-specific transition and its ordered collection state."""

    if not isinstance(transition, Mapping):
        return None
    body = {key: value for key, value in transition.items() if key != "transition_sha256"}
    if not (
        transition.get("schema_version") == RECOVERY_TRANSITION_SCHEMA
        and transition.get("transition_index") == index
        and transition.get("previous_row_binding") == expected_previous
        and recovery_row_binding_is_valid(transition.get("next_row_binding"))
        and transition.get("next_row_binding") != expected_previous
        and transition.get("transition_sha256") == canonical_sha256(body)
    ):
        return None
    kind = transition.get("transition_kind")
    previous_collection = expected_previous.get("collection")
    next_binding = dict(transition["next_row_binding"])
    next_collection = next_binding.get("collection")
    if kind == RECOVERY_ROW_REBOUND_TRANSITION:
        if set(transition) != {
            "schema_version", "transition_index", "transition_kind",
            "transition_phase", "changed_fields", "previous_row_binding",
            "next_row_binding", "transition_sha256",
        }:
            return None
        phase = transition.get("transition_phase")
        fields = transition.get("changed_fields")
        if (
            phase not in RECOVERY_ROW_REBOUND_PHASES
            or not isinstance(fields, list)
            or fields != sorted(set(fields))
            or not set(fields).issubset(RECOVERY_REBOUND_FIELDS[phase])
        ):
            return None
        if phase == RECOVERY_REBOUND_SESSION_ANNOTATION:
            if previous_collection != next_collection:
                return None
        elif not (
            previous_collection in RECOVERY_QUEUE_COLLECTIONS
            and next_collection in RECOVERY_QUEUE_COLLECTIONS
        ):
            return None
    elif kind == RECOVERY_PRODUCE_TRANSITION:
        if not (
            set(transition) == {
                "schema_version", "transition_index", "transition_kind",
                "previous_row_binding", "produced_pick_sha256",
                "next_row_binding", "transition_sha256",
            }
            and previous_collection in RECOVERY_QUEUE_COLLECTIONS
            and next_collection == "picks"
            and transition.get("produced_pick_sha256") == next_binding.get("row_sha256")
        ):
            return None
    elif kind == RECOVERY_QUEUE_TERMINAL_TRANSITION:
        if not (
            set(transition) == {
                "schema_version", "transition_index", "transition_kind",
                "previous_row_binding", "terminal_row_sha256",
                "next_row_binding", "transition_sha256",
            }
            and previous_collection in RECOVERY_QUEUE_COLLECTIONS
            and next_collection == "talk_below_confidence_threshold"
            and transition.get("terminal_row_sha256") == next_binding.get("row_sha256")
        ):
            return None
    elif kind == RECOVERY_REQUEUE_TRANSITION:
        if not (
            set(transition) == {
                "schema_version", "transition_index", "transition_kind",
                "previous_row_binding", "failed_pick_sha256",
                "next_row_binding", "transition_sha256",
            }
            and previous_collection == "picks"
            and next_collection in RECOVERY_QUEUE_COLLECTIONS
            and transition.get("failed_pick_sha256") == expected_previous.get("row_sha256")
        ):
            return None
    elif kind == HANDOFF_TRANSITION_KIND:
        return validate_handoff_transition_structure(
            transition, index=index, expected_previous=expected_previous
        )
    else:
        return None
    return next_binding
