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
typo. 维护者 ruling #2: 「弹幕会有梗，有刻意的错写，因此只要识别出是
弹幕就不应该改」.

This is the same architecture gap ``redelivery_baseline_ownership.py`` closed
for 维护者-reviewed baseline diffs (zsm8 case) — a second, different
authority source with the identical unread-owned-interval shape. This module
mirrors that fix: it is the single place that consults chat-authority
``applied`` rows before the self-heal apply step and drops any finding whose
target cue falls inside one, with a typed
``CHAT_AUTHORITY_OWNED_CUE_SELF_HEAL_SUPPRESSED`` receipt instead of a silent
drop.
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping, Sequence

from .jingting_chunker import parse_srt_cues

_VERBATIM_CHAT_MODES = frozenset({"exact_span", "question_particle_patch"})


def _chat_authority_owned_intervals(
    chat_authority_audit: Mapping[str, object] | None,
    *,
    delivery_start_ms: int,
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
    intervals: list[tuple[int, int]] = []
    for row in raw:
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


def suppress_chat_authority_owned_self_heal_findings(
    final_text: str,
    audit: object,
    chat_authority_audit: Mapping[str, object] | None,
    delivery_start_ms: int,
) -> list[dict[str, object]]:
    """Refuse to let exact-final self-heal re-adjudicate a chat-owned cue.

    A finding whose target cue falls fully inside a verbatim
    chat-authority-applied window is dropped here — before
    ``_apply_exact_final_cpa_repairs`` ever sees it, so it can never mutate
    that cue's bytes — with a typed
    ``CHAT_AUTHORITY_OWNED_CUE_SELF_HEAL_SUPPRESSED`` receipt recorded on the
    audit instead of a silent drop. Release-gate fields are recomputed only
    when suppression is the reason the finding list is now empty; any other
    genuine block reason is left untouched.
    """
    owned_intervals = _chat_authority_owned_intervals(
        chat_authority_audit, delivery_start_ms=delivery_start_ms
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
                "schema_version": "chat-authority-owned-cue-self-heal-suppressed.v1",
                "reason_code": "CHAT_AUTHORITY_OWNED_CUE_SELF_HEAL_SUPPRESSED",
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
    existing = audit.get("chat_authority_owned_findings_suppressed")
    audit["chat_authority_owned_findings_suppressed"] = [
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
