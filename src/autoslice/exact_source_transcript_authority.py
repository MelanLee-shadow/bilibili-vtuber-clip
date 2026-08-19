"""Mutation-bound validation for exact-source transcript candidates.

The transcript observation is candidate-only.  A later CPA mutation may use
it only while the complete v2 handoff remains attached to the exact request
and still validates against that request, its acoustic witness, and the live
SRT bytes.
"""

from __future__ import annotations

from typing import Any, Mapping

from src.autoslice.exact_source_transcript_contract import (
    valid_exact_source_transcript_handoff,
)


EXACT_SOURCE_TRANSCRIPT_PROVENANCE_KIND = (
    "bounded_exact_source_audio_transcript"
)
_PROVENANCE_KEYS = {
    "kind",
    "surface",
    "source_media_sha256",
    "audio_clip_sha256",
    "handoff_receipt_sha256",
    "rejected_proposed_sha256",
    "mutation_authorized",
}


def _digest(value: object) -> str:
    return str(value or "").removeprefix("sha256:")


def exact_source_transcript_candidate_marked(
    *,
    adjudication: Mapping[str, Any] | None = None,
    request: Mapping[str, Any] | None = None,
    candidate_provenance: Mapping[str, Any] | None = None,
) -> bool:
    """Whether any durable field identifies the exact-transcript lane."""

    request_provenance = (
        request.get("candidate_provenance")
        if isinstance(request, Mapping)
        else None
    )
    return bool(
        (
            isinstance(adjudication, Mapping)
            and "exact_source_transcript_handoff" in adjudication
        )
        or (
            isinstance(request, Mapping)
            and "exact_source_transcript_handoff" in request
        )
        or (
            isinstance(request_provenance, Mapping)
            and request_provenance.get("kind")
            == EXACT_SOURCE_TRANSCRIPT_PROVENANCE_KIND
        )
        or (
            isinstance(candidate_provenance, Mapping)
            and candidate_provenance.get("kind")
            == EXACT_SOURCE_TRANSCRIPT_PROVENANCE_KIND
        )
    )


def priority_exact_source_transcript_candidate(
    row: Mapping[str, Any],
    cue_index: int,
    current_cue: str,
    proposed_cue: str,
    srt_text: str,
    clip_context: Mapping[str, object] | None,
    trusted: bool,
) -> tuple[bool, bool, Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Validate an exact-transcript candidate restored from trusted carryover."""

    adjudication = row.get("exact_release_adjudication")
    request = (
        adjudication.get("request")
        if isinstance(adjudication, Mapping)
        else None
    )
    provenance = row.get("candidate_provenance")
    marked = bool(
        trusted
        and exact_source_transcript_candidate_marked(
            adjudication=(
                adjudication if isinstance(adjudication, Mapping) else None
            ),
            request=request if isinstance(request, Mapping) else None,
            candidate_provenance=(
                provenance if isinstance(provenance, Mapping) else None
            ),
        )
    )
    valid = bool(
        marked
        and isinstance(adjudication, Mapping)
        and isinstance(request, Mapping)
        and isinstance(provenance, Mapping)
        and request.get("cue_indexes") == [cue_index]
        and request.get("current_cue") == current_cue
        and request.get("proposed_cue") == proposed_cue
        and valid_exact_source_transcript_adjudication_handoff(
            adjudication,
            srt_text=srt_text,
            candidate_provenance=provenance,
            clip_context=clip_context,
        )
    )
    return (
        marked,
        valid,
        adjudication if isinstance(adjudication, Mapping) else None,
        provenance if isinstance(provenance, Mapping) else None,
    )


def valid_exact_source_transcript_adjudication_handoff(
    adjudication: Mapping[str, Any],
    *,
    srt_text: str | None = None,
    expected_srt_sha256: object = None,
    candidate_provenance: Mapping[str, Any] | None = None,
    clip_context: Mapping[str, object] | None = None,
) -> bool:
    """Revalidate the complete candidate receipt at a mutation boundary."""

    request = adjudication.get("request")
    witness = adjudication.get("verdict")
    handoff = adjudication.get("exact_source_transcript_handoff")
    if not all(
        isinstance(value, Mapping)
        for value in (request, witness, handoff)
    ):
        return False
    request_handoff = request.get("exact_source_transcript_handoff")
    provenance = request.get("candidate_provenance")
    if not (
        isinstance(request_handoff, Mapping)
        and dict(request_handoff) == dict(handoff)
        and isinstance(provenance, Mapping)
        and set(provenance) == _PROVENANCE_KEYS
        and provenance.get("kind")
        == EXACT_SOURCE_TRANSCRIPT_PROVENANCE_KIND
        and provenance.get("mutation_authorized") is False
        and provenance.get("surface") == handoff.get("proposed_cue")
        and _digest(provenance.get("source_media_sha256"))
        == _digest(handoff.get("source_media_sha256"))
        and _digest(provenance.get("audio_clip_sha256"))
        == _digest(handoff.get("audio_clip_sha256"))
        and _digest(provenance.get("handoff_receipt_sha256"))
        == _digest(handoff.get("receipt_sha256"))
        and _digest(provenance.get("rejected_proposed_sha256"))
        == _digest(handoff.get("rejected_proposed_sha256"))
        and (
            candidate_provenance is None
            or dict(candidate_provenance) == dict(provenance)
        )
        and (
            expected_srt_sha256 is None
            or _digest(expected_srt_sha256)
            == _digest(handoff.get("final_srt_sha256"))
        )
    ):
        return False
    return valid_exact_source_transcript_handoff(
        handoff,
        srt_text=srt_text,
        check_request=request,
        witness=witness,
        clip_context=clip_context,
    )
