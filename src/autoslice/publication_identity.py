"""One interpretation of delivery records at publication boundaries.

A paired delivery/source record publishes under its delivery id. Its selector
row is an alias, never another publication authority. Legacy projections below
retain each caller's existing admission rules; resolving an id grants no release
or upload authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def _id(value: object) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class PublicationIdentity:
    candidate_id: str
    registry_candidate_id: str
    record_candidate_id: object
    declared_ids: frozenset[str]
    authority_ids: frozenset[str]
    candidate_aliases: frozenset[str]
    lineage_aliases: frozenset[str]
    selector_aliases: frozenset[str]

    def matches_publish_candidate(self, value: object, expected: str) -> bool:
        """Accept the paired selector only for this delivery's bound publish."""
        return value in (None, expected) or (
            self.candidate_id == expected
            and isinstance(value, str)
            and value in self.selector_aliases
        )


def publication_identity(record: Mapping[str, object]) -> PublicationIdentity:
    story = record.get("story_contract")
    raw_story = story.get("candidate_id") if isinstance(story, Mapping) else None
    story_id = _id(raw_story)
    raw_candidate = record.get("candidate_id")
    candidate = _id(raw_candidate)
    raw_delivery = record.get("delivery_candidate_id")
    delivery = _id(raw_delivery)
    source = _id(record.get("source_candidate_id"))
    paired = bool(delivery and source)
    if paired and (candidate not in {"", source} or story_id not in {"", delivery}):
        raise ValueError("publication candidate authorities conflict")
    declared_ids = frozenset({delivery, story_id} - {""})
    if not paired:
        declared_ids |= {candidate} - {""}
    # Legacy QC admitted only nonempty strings, while generic upload and
    # reconciliation used story-before-delivery str conversion. Preserve both
    # projections rather than broadening a legacy lane through normalization.
    authority_ids = frozenset({delivery}) if paired else frozenset(
        value for value in (raw_story, raw_delivery) if isinstance(value, str) and value
    )
    lineage = frozenset(
        _id(record.get(key))
        for key in ("source_candidate_id", "parent_candidate_id", "original_candidate_id")
    ) - {""}
    return PublicationIdentity(
        candidate_id=delivery if paired else str(raw_story or raw_delivery or ""),
        registry_candidate_id=delivery if paired else str(raw_story or ""),
        record_candidate_id=delivery if paired else raw_candidate,
        declared_ids=frozenset(declared_ids),
        authority_ids=authority_ids,
        candidate_aliases=frozenset({candidate, delivery, story_id} - {""}) | lineage,
        lineage_aliases=lineage,
        selector_aliases=frozenset({source}) if paired else frozenset(),
    )
