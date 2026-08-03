"""Candidate-only bridge from subtitle fidelity rejection to CPA review."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from src.autoslice.jingting_chunker import parse_srt_cues


FIDELITY_REVIEW_CANDIDATE_LIMIT = 8


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
        audit = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    rows = audit.get("reverted") if isinstance(audit, Mapping) else None
    if not isinstance(rows, list):
        return []
    cues = [cue for cue in parse_srt_cues(current_srt) if cue.text.strip()]
    candidates: list[tuple[tuple[int, int, int], dict[str, object]]] = []
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
        if current != kept or not attempted or attempted == current:
            continue
        if not isinstance(violations, list) or len(violations) != 1:
            continue
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
        ascii_penalty = int(
            bool(any(ch.isascii() and ch.isalpha() for ch in replacement))
        )
        candidates.append(
            (
                (ascii_penalty, len(suspect) + len(replacement), cue_index),
                candidate,
            )
        )
    candidates.sort(key=lambda item: item[0])
    return [
        candidate
        for _priority, candidate in candidates[:FIDELITY_REVIEW_CANDIDATE_LIMIT]
    ]
