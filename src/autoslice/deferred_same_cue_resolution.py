"""Re-enter same-cue findings after earlier mutations settle."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping
from difflib import SequenceMatcher
from typing import Any, Callable, Sequence

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.final_review_contract import (
    decided_keep_current_adjudication,
)


SCHEMA_VERSION = "subtitle-deferred-same-cue-resolution.v1"
SUPERSEDED = "SUPERSEDED_BY_SAME_CUE_MUTATION"
UNRESOLVED = "SAME_CUE_REENTRY_UNRESOLVED"


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mapped_unchanged_span(
    original: str,
    live: str,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    opcodes = SequenceMatcher(
        None, original, live
    ).get_opcodes()
    if start == end and any(
        (tag == "insert" and old_start == start)
        or (tag in {"replace", "delete"} and old_start < start < old_end)
        for tag, old_start, old_end, _new_start, _new_end in opcodes
    ):
        return None
    for tag, old_start, old_end, new_start, _new_end in opcodes:
        if tag == "equal" and old_start <= start and end <= old_end:
            offset = new_start - old_start
            return start + offset, end + offset
    if start == end:
        for tag, old_start, old_end, new_start, new_end in opcodes:
            if tag != "equal" and start == old_start:
                return new_start, new_start
            if tag != "equal" and start == old_end:
                return new_end, new_end
    return None


def rebase_deferred_finding(
    finding: Mapping[str, Any],
    *,
    original_text: str,
    live_text: str | None,
    cue_index: int | None,
    matched_start_ms: int,
    matched_end_ms: int,
    original_disposition: str = "DEFERRED_SAME_CUE",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return a fresh-base finding or a typed terminal re-entry outcome."""

    original_base = str(finding.get("base_text_sha256") or "")
    original_proposed = str(finding.get("proposed_full_cue") or "")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "original_disposition": original_disposition,
        "matched_start_ms": matched_start_ms,
        "matched_end_ms": matched_end_ms,
        "original_base_text_sha256": original_base,
        "original_proposed_text_sha256": "sha256:" + _text_sha256(original_proposed),
    }
    if live_text is None or cue_index is None:
        receipt.update(
            status=SUPERSEDED,
            final_disposition="SUPERSEDED",
            reason_code="SAME_CUE_REMOVED_BY_PRIOR_MUTATION",
        )
        return None, receipt
    live_sha = _text_sha256(live_text)
    receipt["live_base_text_sha256"] = live_sha
    if original_base != _text_sha256(original_text):
        receipt.update(
            status=UNRESOLVED,
            final_disposition="UNRESOLVED",
            reason_code="DEFERRED_FINDING_ORIGINAL_BASE_BINDING_INVALID",
        )
        return None, receipt
    if original_proposed == live_text:
        receipt.update(
            status=SUPERSEDED,
            final_disposition="SUPERSEDED",
            reason_code="PROPOSAL_ALREADY_APPLIED_BY_SAME_CUE_MUTATION",
        )
        return None, receipt
    start = finding.get("span_start_codepoint")
    end = finding.get("span_end_codepoint")
    suspect = str(finding.get("suspect") or "")
    suggestion = str(finding.get("suggestion") or "")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or not 0 <= start <= end <= len(original_text)
        or original_text[start:end] != suspect
    ):
        receipt.update(
            status=UNRESOLVED,
            final_disposition="UNRESOLVED",
            reason_code="DEFERRED_FINDING_EDIT_SPAN_INVALID",
        )
        return None, receipt
    mapped = _mapped_unchanged_span(original_text, live_text, start, end)
    if mapped is None or live_text[mapped[0] : mapped[1]] != suspect:
        receipt.update(
            status=SUPERSEDED,
            final_disposition="SUPERSEDED",
            reason_code="EDIT_SPAN_OVERLAPS_PRIOR_SAME_CUE_MUTATION",
        )
        return None, receipt
    proposed = live_text[: mapped[0]] + suggestion + live_text[mapped[1] :]
    rebuilt = dict(finding)
    rebuilt.update(
        cue_index=cue_index,
        base_text_sha256=live_sha,
        span_start_codepoint=mapped[0],
        span_end_codepoint=mapped[1],
        proposed_full_cue=proposed,
    )
    receipt.update(
        status="PENDING_READJUDICATION",
        rebuilt_base_text_sha256=live_sha,
        rebuilt_proposed_text_sha256="sha256:" + _text_sha256(proposed),
    )
    return rebuilt, receipt


def finalize_reentry_receipt(
    receipt: Mapping[str, Any],
    *,
    adjudication: Mapping[str, Any],
) -> dict[str, Any]:
    final = dict(receipt)
    repaired = adjudication.get("repaired") is True
    decided_keep = decided_keep_current_adjudication(
        adjudication,
        timing_immutable=adjudication.get("timing_immutable") is True,
    )
    final.update(
        status="READJUDICATED",
        final_disposition=(
            "APPLIED"
            if repaired
            else (
                "KEEP_CURRENT"
                if decided_keep
                else "UNRESOLVED"
            )
        ),
        final_policy_branch=adjudication.get("policy_branch"),
    )
    return final


def append_or_compose_staged_owner(
    repairs: list[dict[str, Any]],
    repair: dict[str, Any],
) -> None:
    """Keep one final-surface owner per immutable cue window."""

    prior = next(
        (
            row
            for row in reversed(repairs)
            if row.get("mode") == "final_review_context_adjudication"
            and row.get("matched_start_ms") == repair.get("matched_start_ms")
            and row.get("matched_end_ms") == repair.get("matched_end_ms")
        ),
        None,
    )
    if prior is None:
        repairs.append(repair)
        return
    chain = list(prior.get("same_cue_mutation_chain") or [])
    if not chain:
        chain.append(
            {
                "request_sha256": prior.get("request_sha256"),
                "before": prior.get("before"),
                "after": prior.get("after"),
                "policy_branch": prior.get("policy_branch"),
            }
        )
    chain.append(
        {
            "request_sha256": repair.get("request_sha256"),
            "before": repair.get("before"),
            "after": repair.get("after"),
            "policy_branch": repair.get("policy_branch"),
        }
    )
    initial_before = prior.get("before")
    prior.update(repair)
    prior["before"] = initial_before
    prior["same_cue_mutation_chain"] = chain


def _live_cue_for_window(
    srt_text: str,
    window: tuple[int, int],
) -> tuple[int | None, str | None]:
    for index, cue in enumerate(
        (cue for cue in parse_srt_cues(srt_text) if cue.text.strip()),
        start=1,
    ):
        if (cue.start_ms, cue.end_ms) == window:
            return index, cue.text
    return None, None


def _staged_owner(
    finding_cue: int,
    adjudication: Mapping[str, Any],
) -> dict[str, Any]:
    request = adjudication.get("request") or {}
    witness_judge = adjudication.get("witness_judge") or {}
    return {
        "mode": "final_review_context_adjudication",
        "action": (
            "DROP_CUE"
            if adjudication.get("policy_branch")
            == "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"
            else "REPLACE_CUE_TEXT"
        ),
        "repair_class": request.get("repair_class"),
        "decision_authority": adjudication.get("decision_authority"),
        "policy_branch": adjudication.get("policy_branch"),
        "mutation_authority": adjudication.get("mutation_authority"),
        "evidence_id": request.get("evidence_id"),
        "cue_indexes": [finding_cue],
        "matched_start_ms": int(request.get("matched_start_ms") or 0),
        "matched_end_ms": int(request.get("matched_end_ms") or 0),
        "before": [request.get("current_cue")],
        "after": [request.get("proposed_cue")],
        "structured_exact_text": request.get("proposed_cue"),
        "survived": True,
        "verdict": adjudication.get("verdict"),
        "acoustic_witness": adjudication.get("verdict"),
        "judge": witness_judge.get("judge"),
        "drop_authority": adjudication.get("drop_authority"),
        "inaudible_witness_override": witness_judge.get(
            "inaudible_witness_override"
        ),
        "request_sha256": request.get("request_sha256"),
        "timing_immutable": adjudication.get("timing_immutable"),
    }


def adjudicate_routed_findings(
    srt_text: str,
    findings: Sequence[dict[str, Any]],
    *,
    max_adjudications: int,
    adjudicate: Callable[[str, dict[str, Any]], tuple[str, dict[str, Any]]],
    original_srt_text: str | None = None,
) -> tuple[str, int, bool, int, list[dict[str, Any]]]:
    """Adjudicate every routed finding, including fresh-base same-cue re-entry."""

    initial_cues = [
        cue
        for cue in parse_srt_cues(original_srt_text or srt_text)
        if cue.text.strip()
    ]
    targets = {
        index: ((cue.start_ms, cue.end_ms), cue.text)
        for index, cue in enumerate(initial_cues, start=1)
    }
    adjudicated_windows: set[tuple[int, int]] = set()
    admitted_windows: set[tuple[int, int]] = set()
    rejected_windows: set[tuple[int, int]] = set()
    group_sizes = Counter(
        targets.get(int(row.get("cue_index") or 0), ((0, 0), ""))[0]
        for row in findings
    )
    reserved_calls = 0
    staged_repairs: list[dict[str, Any]] = []
    adjudication_count = applied_count = 0
    partial = False
    for row in findings:
        original_index = int(row.get("cue_index") or 0)
        target = targets.get(original_index)
        if target is None:
            row["routed"] = "same_cue_unresolved"
            row["context_audio_adjudication"] = {
                "schema_version": "subtitle-span-adjudication.v1",
                "status": UNRESOLVED,
                "repaired": False,
                "reason_code": "FINDING_ORIGINAL_CUE_WINDOW_INVALID",
            }
            partial = True
            continue
        window, original_text = target or ((0, 0), "")
        finding_cue, live_text = _live_cue_for_window(srt_text, window)
        reentry: dict[str, Any] | None = None
        deferred = window in adjudicated_windows
        live_sha = _text_sha256(live_text) if live_text is not None else None
        if deferred or live_text is None or row.get("base_text_sha256") != live_sha:
            rebuilt, reentry = rebase_deferred_finding(
                row,
                original_text=original_text,
                live_text=live_text,
                cue_index=finding_cue,
                matched_start_ms=window[0],
                matched_end_ms=window[1],
                original_disposition=(
                    "DEFERRED_SAME_CUE"
                    if deferred
                    else "REBASED_AFTER_ROUTE_MUTATION"
                ),
            )
            if rebuilt is None:
                superseded = reentry.get("final_disposition") == "SUPERSEDED"
                row["routed"] = (
                    "same_cue_superseded" if superseded else "same_cue_unresolved"
                )
                row["context_audio_adjudication"] = {
                    "schema_version": "subtitle-span-adjudication.v1",
                    "status": reentry.get("status"),
                    "repaired": False,
                    "reason_code": reentry.get("reason_code"),
                    "same_cue_reentry": reentry,
                }
                partial = partial or not superseded
                continue
            row.update(rebuilt)
        elif finding_cue is not None:
            row["cue_index"] = finding_cue
        if window not in admitted_windows and window not in rejected_windows:
            required = group_sizes.get(window, 1)
            if reserved_calls + required <= max_adjudications:
                admitted_windows.add(window)
                reserved_calls += required
            else:
                rejected_windows.add(window)
        if window in rejected_windows:
            row["routed"] = "skipped_budget"
            row["context_audio_adjudication"] = {
                "schema_version": "subtitle-span-adjudication.v1",
                "status": "SKIPPED_BUDGET",
                "repaired": False,
            }
            partial = True
            continue
        suspect = str(row.get("suspect") or "")
        if live_text is None or suspect not in live_text:
            row["context_audio_adjudication"] = {
                "status": "STALE_FINDING_SKIPPED",
                "repaired": False,
            }
            continue
        adjudication_count += 1
        adjudicated_windows.add(window)
        srt_text, audit = adjudicate(srt_text, row)
        rebuilt_finding = audit.get("rebuilt_finding")
        if isinstance(rebuilt_finding, Mapping):
            for key in (
                "suspect",
                "suggestion",
                "span_start_codepoint",
                "span_end_codepoint",
                "proposed_full_cue",
                "base_text_sha256",
                "candidate_provenance",
                "candidate_memory_id",
                "why",
                "repair_class",
            ):
                if key in rebuilt_finding:
                    row[key] = rebuilt_finding[key]
        if reentry is not None:
            audit["same_cue_reentry"] = finalize_reentry_receipt(
                reentry,
                adjudication=audit,
            )
            row["routed"] = (
                "same_cue_readjudicated"
                if deferred
                else "context_audio_adjudicated"
            )
        repaired = audit.get("repaired") is True
        row["context_audio_adjudication"] = audit
        if not repaired:
            continue
        row["routed"] = (
            "same_cue_readjudicated_fix"
            if deferred
            else "context_audio_adjudicated_fix"
        )
        applied_count += 1
        append_or_compose_staged_owner(
            staged_repairs,
            _staged_owner(int(row["cue_index"]), audit),
        )
    return srt_text, adjudication_count, partial, applied_count, staged_repairs
