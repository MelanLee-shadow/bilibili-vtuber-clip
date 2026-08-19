"""Baseline-owned interval enforcement for exact-final CPA self-heal.

``redelivery_subtitle_baseline.py`` writes ``owned_intervals`` for every cue
whose text/timing came from an 维护者-reviewed baseline diff, but until this
module existed nothing ever read the field back (``grep owned_intervals``
only ever matched the writer). Without it, the exact-final CPA self-heal
channel (inside ``producer_package_finalization._run_exact_final_review_gate``)
independently re-listens to the exact same windows and can silently
overwrite already-correct, 维护者-reviewed text with its own transcription —
the zsm8 shape : baseline-applied cues 52/58/59 rewritten away
from 维护者's verified transcript, caught only by the later, separate final
owner-verifier (``REDELIVERY_BASELINE_FINAL_OWNER_NOT_VERIFIED``).

This module is the single place that consults ``owned_intervals`` before the
self-heal apply step and drops any finding whose target cue falls inside one,
with a typed ``BASELINE_OWNED_CUE_SELF_HEAL_SUPPRESSED`` receipt instead of a
silent drop. (维护者 配额上传波修复.)
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping, Sequence

from .jingting_chunker import parse_srt_cues


def _redelivery_baseline_owned_intervals(
    redelivery_baseline_audit: Mapping[str, object] | None,
) -> list[tuple[int, int]]:
    if not isinstance(redelivery_baseline_audit, Mapping):
        return []
    raw = redelivery_baseline_audit.get("owned_intervals")
    if not isinstance(raw, list):
        return []
    intervals: list[tuple[int, int]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        start_ms = row.get("start_ms")
        end_ms = row.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
        ):
            continue
        intervals.append((start_ms, end_ms))
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


def suppress_baseline_owned_self_heal_findings(
    final_text: str,
    audit: object,
    redelivery_baseline_audit: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    """Refuse to let exact-final self-heal re-adjudicate a baseline-owned cue.

    Baseline authority outranks a later self-heal re-adjudication. A finding
    whose target cue falls fully inside an owned interval is dropped here —
    before ``_apply_exact_final_cpa_repairs`` ever sees it, so it can never
    mutate that cue's bytes — with a typed
    ``BASELINE_OWNED_CUE_SELF_HEAL_SUPPRESSED`` receipt recorded on the audit
    instead of a silent drop. Release-gate fields are recomputed only when
    suppression is the reason the finding list is now empty; any other
    genuine block reason is left untouched.
    """
    owned_intervals = _redelivery_baseline_owned_intervals(
        redelivery_baseline_audit
    )
    if not isinstance(audit, dict) or not owned_intervals:
        return []
    findings = audit.get("findings")
    if not isinstance(findings, list) or not findings:
        return []
    cues = parse_srt_cues(final_text)
    kept: list[object] = []
    suppressed: list[dict[str, object]] = []
    for finding in findings:
        cue_index = (
            finding.get("cue_index") if isinstance(finding, Mapping) else None
        )
        interval = None
        cue = None
        if (
            isinstance(cue_index, int)
            and not isinstance(cue_index, bool)
            and 1 <= cue_index <= len(cues)
        ):
            cue = cues[cue_index - 1]
            interval = _matching_owned_interval(
                cue.start_ms, cue.end_ms, owned_intervals
            )
        if interval is None:
            kept.append(finding)
            continue
        suppressed.append(
            {
                "schema_version": "baseline-owned-cue-self-heal-suppressed.v1",
                "reason_code": "BASELINE_OWNED_CUE_SELF_HEAL_SUPPRESSED",
                "cue_index": cue_index,
                "matched_start_ms": cue.start_ms,
                "matched_end_ms": cue.end_ms,
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
        )
    if not suppressed:
        return []
    audit["findings"] = kept
    audit["validated_finding_count"] = len(kept)
    discovery = audit.get("discovery")
    if isinstance(discovery, dict):
        discovery["explicit_empty_findings"] = not kept
    existing = audit.get("baseline_owned_findings_suppressed")
    audit["baseline_owned_findings_suppressed"] = [
        *(existing if isinstance(existing, list) else []),
        *suppressed,
    ]
    if not kept:
        reason_codes = [
            code
            for code in (audit.get("reason_codes") or [])
            if code
            not in {
                "FINAL_REVIEW_UNRESOLVED_FINDINGS",
                "CPA_CYCLE_ADJUDICATION_UNAVAILABLE",
                "CPA_CYCLE_ADJUDICATION_INVALID",
            }
        ]
        audit["reason_codes"] = reason_codes
        if not reason_codes:
            audit["status"] = "CLEAN"
            audit["release_gate"] = "PASS"
    return suppressed
