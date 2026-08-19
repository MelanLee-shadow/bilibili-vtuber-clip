"""Candidate-only bridge from subtitle fidelity rejection to CPA review."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from src.autoslice.closed_set_evidence import (
    DRAFT_FIDELITY_PROVENANCE_SCHEMA,
)
from src.autoslice.jingting_chunker import parse_srt_cues


FIDELITY_REVIEW_CANDIDATE_LIMIT = 8


def _draft_kept_surface(
    kept: str, violations: list[object]
) -> str:
    surfaces = {
        str(value.get("draft_span") or "").strip()
        for value in violations
        if isinstance(value, Mapping)
        and len(str(value.get("draft_span") or "").strip()) >= 2
        and str(value.get("draft_span") or "").strip() in kept
    }
    return max(surfaces, key=lambda value: (len(value), value), default="")


def _draft_kept_provenance(
    *, cue_index: int, start_ms: int, end_ms: int, kept: str, current: str,
    kept_candidate: str, audit_sha256: str, source_srt_sha256: str,
    violations: list[object]
) -> dict[str, object]:
    return {
        "schema_version": DRAFT_FIDELITY_PROVENANCE_SCHEMA,
        "kind": "draft_fidelity_kept",
        "scope": "cue",
        "cue_index": cue_index,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "draft_fidelity_kept": True,
        "kept_candidate": kept_candidate,
        "surface": _draft_kept_surface(kept, violations),
        "audit_sha256": "sha256:" + audit_sha256,
        "source_srt_sha256": "sha256:" + source_srt_sha256,
        "kept_text_sha256": "sha256:"
        + hashlib.sha256(kept.encode("utf-8")).hexdigest(),
        "current_text_sha256": "sha256:"
        + hashlib.sha256(current.encode("utf-8")).hexdigest(),
        "violation_reason_codes": sorted(
            {
                str(value.get("reason") or "")
                for value in violations
                if isinstance(value, Mapping) and str(value.get("reason") or "")
            }
        ),
        "mutation_authorized": False,
    }


def fidelity_review_candidates(
    padded: Path,
    current_srt: str,
) -> list[dict[str, object]]:
    """Recover bounded candidates that the fidelity guard could not prove.

    Rejection by the guard is not evidence that a proposal is wrong.  Only a
    single exact replacement bound to the current cue is forwarded, with no
    mutation authority; CPA still makes the final closed-set choice.
    """

    path = padded.with_suffix(".fidelity-audit.json")
    try:
        audit_bytes = path.read_bytes()
        audit = json.loads(audit_bytes)
    except (OSError, ValueError, TypeError):
        return []
    if audit.get("schema_version") != "subtitle-fidelity-audit.v2":
        return []
    rows = audit.get("reverted") if isinstance(audit, Mapping) else None
    if not isinstance(rows, list):
        return []
    cues = [cue for cue in parse_srt_cues(current_srt) if cue.text.strip()]
    draft_path = padded.with_suffix(".asr_draft.srt")
    try:
        draft_bytes = draft_path.read_bytes()
        draft_cues = [
            cue
            for cue in parse_srt_cues(draft_bytes.decode("utf-8", "replace"))
            if cue.text.strip()
        ]
    except OSError:
        draft_bytes = b""
        draft_cues = []
    audit_sha256 = hashlib.sha256(audit_bytes).hexdigest()
    source_srt_sha256 = hashlib.sha256(draft_bytes).hexdigest()
    candidates: list[tuple[tuple[int, int, int, int], dict[str, object]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            cue_index = int(row.get("cue_index") or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= cue_index <= len(cues):
            continue
        current = cues[cue_index - 1].text
        kept = str(row.get("kept") or "")
        attempted = str(row.get("attempted") or "")
        violations = row.get("violations")
        if not kept or not isinstance(violations, list) or not violations:
            continue
        draft_cue = draft_cues[cue_index - 1] if 1 <= cue_index <= len(draft_cues) else None
        draft_bound = bool(
            draft_cue is not None
            and str(row.get("draft") or "") == draft_cue.text
            and cues[cue_index - 1].start_ms == draft_cue.start_ms
            and cues[cue_index - 1].end_ms == draft_cue.end_ms
        )
        candidate: dict[str, object] | None = None
        priority_lane = 1
        replacement_for_priority = ""
        if current != kept:
            if not draft_bound or draft_cue is None:
                continue
            priority_lane = 0
            replacement_for_priority = kept
            candidate = {
                "cue": cue_index,
                "kind": "context",
                "suspect": "",
                "proposed_full_cue": kept,
                "repair_class": "phonetic",
                "base_text_sha256": hashlib.sha256(
                    current.encode("utf-8")
                ).hexdigest(),
                "candidate_provenance": _draft_kept_provenance(
                    cue_index=cue_index, start_ms=draft_cue.start_ms,
                    end_ms=draft_cue.end_ms, kept=kept, current=current,
                    kept_candidate="PROPOSED", audit_sha256=audit_sha256,
                    source_srt_sha256=source_srt_sha256, violations=violations,
                ),
                "why": (
                    "fidelity guard kept this draft-side cue before the current "
                    "surface drifted; candidate only, final choice belongs to CPA"
                ),
                "candidate_origin": "draft_fidelity_kept",
            }
        elif attempted and attempted != current and len(violations) == 1:
            violation = violations[0]
            if not isinstance(violation, Mapping) or violation.get("op") != "replace":
                continue
            suspect = str(violation.get("draft_span") or "")
            replacement = str(violation.get("final_span") or "")
            if (
                not suspect
                or not replacement
                or current.count(suspect) != 1
                or current.replace(suspect, replacement, 1) != attempted
            ):
                continue
            replacement_for_priority = replacement
            candidate = {
                "cue": cue_index,
                "kind": "context",
                "suspect": suspect,
                "proposed_full_cue": attempted,
                "repair_class": "phonetic",
                "base_text_sha256": hashlib.sha256(
                    current.encode("utf-8")
                ).hexdigest(),
                "why": (
                    "CPA refinement candidate reverted by the fidelity guard; "
                    "candidate only, final choice belongs to CPA"
                ),
                "candidate_origin": "fidelity_guard_reverted_candidate",
            }
            if draft_bound and draft_cue is not None:
                candidate["candidate_provenance"] = _draft_kept_provenance(
                    cue_index=cue_index, start_ms=draft_cue.start_ms,
                    end_ms=draft_cue.end_ms, kept=kept, current=current,
                    kept_candidate="CURRENT", audit_sha256=audit_sha256,
                    source_srt_sha256=source_srt_sha256, violations=violations,
                )
        if candidate is None:
            continue
        ascii_penalty = int(
            bool(
                any(
                    ch.isascii() and ch.isalpha()
                    for ch in replacement_for_priority
                )
            )
        )
        candidates.append(
            (
                (
                    priority_lane,
                    ascii_penalty,
                    len(replacement_for_priority),
                    cue_index,
                ),
                candidate,
            )
        )
    candidates.sort(key=lambda item: item[0])
    return [
        candidate
        for _priority, candidate in candidates[:FIDELITY_REVIEW_CANDIDATE_LIMIT]
    ]
