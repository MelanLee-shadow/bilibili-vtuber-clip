"""Fail-closed consumption of sealed Qixi terminal-baseline supersessions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from src.autoslice.producer_text_finalization import normalize_chat_text


def reconcile_terminal_baseline_replay_supersessions(
    chat: dict[str, Any],
    *,
    terminal_projection_authority: object,
    delivery_start_ms: int,
) -> None:
    """Mark a historical replay supersession only when the fresh cue owns it.

    The current-terminal projection replaces a historical redelivery audit with
    a fresh replay of the sealed terminal SRT. A carried boundary-owner
    reconciliation is consumed only if its exact before payload is the owned
    decision and its exact after payload and source-time span remain owned by
    the newly replayed baseline cues.
    """

    if not isinstance(terminal_projection_authority, Mapping):
        raise ValueError("terminal projection authority is invalid")
    authority_value = terminal_projection_authority.get("authority_sha256")
    authority_sha = str(authority_value or "").removeprefix("sha256:")
    if len(authority_sha) != 64 or any(char not in "0123456789abcdef" for char in authority_sha):
        raise ValueError("terminal projection authority is invalid")
    if isinstance(delivery_start_ms, bool) or not isinstance(delivery_start_ms, int) or delivery_start_ms < 0:
        raise ValueError("terminal delivery start is invalid")
    baseline = chat.get("redelivery_subtitle_baseline_audit")
    mappings = baseline.get("mappings") if isinstance(baseline, Mapping) else None
    applied = chat.get("applied")
    if not isinstance(mappings, list) or not isinstance(applied, list):
        return
    by_index: dict[int, Mapping[str, object]] = {}
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            return
        index = mapping.get("baseline_cue_index")
        if isinstance(index, bool) or not isinstance(index, int) or index <= 0 or index in by_index:
            return
        by_index[index] = mapping

    for row in applied:
        if not isinstance(row, dict) or row.get("reconciliation"):
            continue
        if (
            row.get("boundary_required") is not True
            or row.get("final_verification_scope") != "SUPERSEDED_BY_REDELIVERY_BASELINE"
            or row.get("final_verification_scope_reason")
            != "BOUNDARY_OWNER_REVERTED_BY_VERIFIED_BASELINE_REPLAY"
        ):
            continue
        expected = normalize_chat_text(str(row.get("exact_text") or ""))
        proof = row.get("final_redelivery_baseline_revert")
        if not expected or not isinstance(proof, Mapping):
            continue
        before = normalize_chat_text(str(proof.get("before_payload") or ""))
        after = normalize_chat_text(str(proof.get("after_payload") or ""))
        indexes = proof.get("baseline_cue_indexes")
        if (
            before != expected
            or not after
            or not isinstance(indexes, list)
            or not indexes
            or any(isinstance(index, bool) or not isinstance(index, int) for index in indexes)
            or indexes != sorted(set(indexes))
        ):
            continue
        current = [by_index.get(index) for index in indexes]
        matched_start = row.get("matched_start_ms")
        matched_end = row.get("matched_end_ms")
        if (
            any(mapping is None for mapping in current)
            or any(mapping.get("final_owner_verified") is not True for mapping in current)
            or isinstance(matched_start, bool)
            or isinstance(matched_end, bool)
            or not isinstance(matched_start, int)
            or not isinstance(matched_end, int)
            or matched_start >= matched_end
            or any(
                isinstance(mapping.get("start_ms"), bool)
                or isinstance(mapping.get("end_ms"), bool)
                or not isinstance(mapping.get("start_ms"), int)
                or not isinstance(mapping.get("end_ms"), int)
                for mapping in current
            )
            or current[0].get("start_ms") != matched_start - delivery_start_ms
            or current[-1].get("end_ms") != matched_end - delivery_start_ms
            or any(left.get("end_ms") != right.get("start_ms") for left, right in zip(current, current[1:]))
            or normalize_chat_text("".join(str(mapping.get("text") or mapping.get("after") or "") for mapping in current))
            != after
        ):
            continue
        row["reconciliation"] = {
            "schema_version": "qixi-terminal-baseline-replay-reconciliation.v1",
            "status": "SUPERSEDED_BY_REDELIVERY_BASELINE",
            "terminal_projection_authority_sha256": "sha256:" + authority_sha,
            "baseline_cue_indexes": indexes,
            "before_payload_sha256": "sha256:" + hashlib.sha256(before.encode()).hexdigest(),
            "after_payload_sha256": "sha256:" + hashlib.sha256(after.encode()).hexdigest(),
        }
