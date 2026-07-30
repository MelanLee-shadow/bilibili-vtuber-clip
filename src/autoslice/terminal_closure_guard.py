"""Keep text-only repairs from reopening a bound terminal story closure."""

from __future__ import annotations

import hashlib
from typing import Mapping


def preserve_context_only_terminal_closure(
    findings: list[dict[str, object]],
    *,
    boundary: object,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split boundary-safe unresolved findings from guarded closure edits.

    CPA remains the final word-choice authority.  This guard is the separate
    publication invariant that prevents a text-only decision, made without an
    acoustic observation, from reopening the already PASS/hash-bound terminal
    closure.  Observed acoustic decisions are deliberately unaffected.
    """

    if not isinstance(boundary, Mapping):
        return findings, []
    endpoint = boundary.get("final_endpoint_binding")
    source_witness = boundary.get("source_separation_witness")
    if not (
        boundary.get("status") == "PASS"
        and boundary.get("review_scope") == "final_delivery"
        and isinstance(endpoint, Mapping)
        and isinstance(source_witness, Mapping)
        and source_witness.get("status") == "PASS"
    ):
        return findings, []
    closure_index = endpoint.get("final_closure_cue_index")
    closure_sha256 = endpoint.get("closure_text_sha256")
    if (
        isinstance(closure_index, bool)
        or not isinstance(closure_index, int)
        or not isinstance(closure_sha256, str)
    ):
        return findings, []

    preserved: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []
    for finding in findings:
        adjudication = finding.get("exact_release_adjudication")
        mutation = (
            adjudication.get("mutation_authority")
            if isinstance(adjudication, Mapping)
            else None
        )
        verdict = (
            adjudication.get("verdict")
            if isinstance(adjudication, Mapping)
            else None
        )
        request = (
            adjudication.get("request")
            if isinstance(adjudication, Mapping)
            else None
        )
        current_cue = (
            str(request.get("current_cue") or "")
            if isinstance(request, Mapping)
            else ""
        )
        current_sha256 = (
            "sha256:"
            + hashlib.sha256(current_cue.encode("utf-8")).hexdigest()
            if current_cue
            else None
        )
        guard_applies = bool(
            finding.get("cue_index") == closure_index
            and current_sha256 == closure_sha256
            and isinstance(adjudication, Mapping)
            and adjudication.get("status") == "OBSERVED"
            and adjudication.get("repaired") is True
            and isinstance(mutation, Mapping)
            and mutation.get("status") == "PASS"
            and mutation.get("basis")
            == "CPA_CONTEXT_ONLY_CLOSED_SET_DISAMBIGUATION"
            and isinstance(verdict, Mapping)
            and verdict.get("status") == "UNCERTAIN"
        )
        if not guard_applies:
            unresolved.append(finding)
            continue
        row = dict(finding)
        row["resolution"] = (
            "BOUNDARY_SEMANTIC_PRESERVES_TERMINAL_CLOSURE_WITHOUT_AUDIO"
        )
        row["boundary_closure_authority"] = {
            "schema_version": "terminal-closure-mutation-guard.v1",
            "status": "KEEP_CURRENT",
            "decision_authority": "BOUNDARY_SEMANTIC_INVARIANT",
            "closure_cue_index": closure_index,
            "closure_text_sha256": closure_sha256,
            "source_boundary_review_sha256": source_witness.get(
                "source_review_sha256"
            ),
            "reason_code": "CONTEXT_ONLY_REPAIR_CANNOT_REOPEN_BOUND_CLOSURE",
        }
        preserved.append(row)
    return unresolved, preserved
