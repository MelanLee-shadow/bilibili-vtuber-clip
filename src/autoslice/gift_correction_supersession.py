"""Revalidate complete correction-pass successors of older gift-text claims.

No text is changed here. Original gift evidence and frozen media windows stay
intact; an intermediate checkpoint is a timing/hash binding, never a verdict.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from .jingting_chunker import parse_srt_cues
from .producer_text_checkpoint import load_post_transcript_text

RECEIPT_KEY = "correction_pass_gift_supersession"
SCOPE = "SUPERSEDED_BY_CORRECTION_PASS"
COUNTER = "final_superseded_by_correction_pass_count"
_NO_CHANGE = frozenset({"asr_win_no_change", "uncertain_no_change",
    "no_verifier_no_change", "gift_arbitration_cap_exceeded_no_change"})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(value: object) -> str:
    return "sha256:" + _sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")))


def _gift_identity(row: Mapping) -> dict:
    # These are verifier annotations, not fields from the original gift attempt.
    return {k: v for k, v in row.items() if k != RECEIPT_KEY
            and not k.startswith(("final_", "survived_final_"))}


def _context(audit: Mapping, final: str, offset: int):
    from .final_review_contract import validate_final_review_release
    from .final_review_auditor import audit_correction_mutation_authority

    review = audit.get("final_review_audit")
    if type(offset) is not int or offset < 0 or not isinstance(review, Mapping):
        return None
    correction = review.get("correction_pass")
    if not isinstance(correction, Mapping):
        return None
    try:
        validate_final_review_release(review, expected_srt_sha256="sha256:" + _sha(final))
        checked = audit_correction_mutation_authority(correction)
        checkpoint = load_post_transcript_text(audit)
        if checked.get("status") != "PASS" or not checkpoint:
            return None
        return correction, checkpoint, parse_srt_cues(checkpoint), parse_srt_cues(final)
    except (TypeError, ValueError, KeyError):
        return None


def _cpa_link(finding: Mapping, before: str, after: str, start: int, end: int) -> str | None:
    adjudication = finding.get("context_audio_adjudication")
    if not isinstance(adjudication, Mapping):
        return None
    request = adjudication.get("request")
    witness_judge = adjudication.get("witness_judge")
    judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
    if not isinstance(request, Mapping) or not isinstance(judge, Mapping):
        return None
    request_sha = request.get("request_sha256")
    actual_sha = _digest({k: v for k, v in request.items() if k != "request_sha256"})[7:]
    if not (
        adjudication.get("repaired") is True
        and adjudication.get("timing_immutable") is True
        and adjudication.get("decision_authority") == "CPA_JUDGE"
        and request_sha == actual_sha
        and request.get("current_cue") == before
        and request.get("proposed_cue") == after
        and request.get("base_text_sha256") == _sha(before)
        and type(request.get("matched_start_ms")) is int
        and type(request.get("matched_end_ms")) is int
        and (request["matched_start_ms"], request["matched_end_ms"]) == (start, end)
        and judge.get("status") == "JUDGED" and judge.get("choice") == "PROPOSED"
        and judge.get("check_request_sha256") == request_sha
    ):
        return None
    original = request.get("whole_clip_current_srt")
    if not isinstance(original, str):
        return None
    matches = [c for c in parse_srt_cues(original)
               if (c.start_ms, c.end_ms, c.text) == (start, end, before)]
    return str(request_sha) if len(matches) == 1 else None


def _canon_link(row: Mapping, finding: Mapping, before: str, after: str) -> bool:
    # A compact canon receipt must never override a gift name actually changed
    # by a previous judge. Only the complete CPA branch may do that.
    start, end = finding.get("span_start_codepoint"), finding.get("span_end_codepoint")
    return bool(
        row.get("outcome") in _NO_CHANGE and row.get("before") == before
        and finding.get("before") == before and finding.get("after") == after
        and finding.get("base_text_sha256") == _sha(before)
        and finding.get("proposed_full_cue") == after
        and type(start) is int and type(end) is int and 0 <= start <= end <= len(before)
        and before[start:end] == finding.get("suspect")
        and isinstance(finding.get("suggestion"), str)
        and before[:start] + finding["suggestion"] + before[end:] == after
    )


def _receipt(row: Mapping, context, final: str, offset: int) -> dict | None:
    correction, checkpoint, source_cues, final_cues = context
    start, end, before = row.get("matched_start_ms"), row.get("matched_end_ms"), row.get("after")
    if not (type(start) is int and type(end) is int and 0 <= offset <= start < end
            and isinstance(before, str) and before and not row.get("reconciliation")
            and row.get("outcome") in (_NO_CHANGE | {"gift_name_repaired"})):
        return None
    target = [c for c in final_cues if (c.start_ms, c.end_ms) == (start-offset, end-offset)]
    if len(target) != 1 or target[0].text == before:
        return None
    after = target[0].text
    matches = []
    for ordinal, finding in enumerate(correction.get("findings", [])):
        if not isinstance(finding, Mapping) or type(finding.get("cue_index")) is not int:
            continue
        index = finding["cue_index"]
        if not 1 <= index <= len(source_cues):
            continue
        source = source_cues[index-1]
        if (source.start_ms, source.end_ms, source.text) != (start, end, after):
            continue
        route, request_sha = finding.get("routed"), None
        if route in {"context_audio_adjudicated_fix", "same_cue_readjudicated_fix"}:
            request_sha = _cpa_link(finding, before, after, start, end)
            if request_sha is None:
                continue
            authority = "CPA_JUDGE"
        elif route == "expected_value_canon" and _canon_link(row, finding, before, after):
            authority = "EXPECTED_VALUE_CANON"
        else:
            continue
        matches.append({
            "schema_version": "correction-pass-gift-owner-supersession.v1",
            "status": SCOPE, "decision_authority": authority,
            "original_gift_sha256": _digest(_gift_identity(row)),
            "correction_pass_sha256": _digest(correction),
            "finding_sha256": _digest(finding), "finding_ordinal": ordinal,
            "request_sha256": request_sha, "source_cue_index": index,
            "matched_start_ms": start, "matched_end_ms": end,
            "delivery_start_ms": offset, "timing_immutable": True,
            "before_sha256": "sha256:" + _sha(before),
            "after_sha256": "sha256:" + _sha(after),
            "checkpoint_srt_sha256": "sha256:" + _sha(checkpoint),
            "final_srt_sha256": "sha256:" + _sha(final),
        })
    return matches[0] if len(matches) == 1 else None


def reconcile_gift_correction_supersessions(
    audit: dict, final: str, speaker: str, *, delivery_start_ms: int,
) -> int:
    from .chat_authority import normalize_chat_text, normalize_srt_payload_window

    context = _context(audit, final, delivery_start_ms)
    count = 0
    for row in audit.get("gift_repairs") or []:
        if not isinstance(row, dict):
            continue
        row.pop(RECEIPT_KEY, None)
        if row.get("final_verification_scope") == SCOPE:
            row.pop("final_verification_scope")
        proof = _receipt(row, context, final, delivery_start_ms) if context else None
        if proof is None:
            continue
        start, end = row["matched_start_ms"]-delivery_start_ms, row["matched_end_ms"]-delivery_start_ms
        clean_window = normalize_srt_payload_window(final, start_ms=start, end_ms=end)
        speaker_window = normalize_srt_payload_window(speaker, start_ms=start, end_ms=end,
                                                      strip_speaker_labels=True)
        if not clean_window or normalize_chat_text(speaker_window) != normalize_chat_text(clean_window):
            continue
        row[RECEIPT_KEY] = proof
        row["final_verification_scope"] = SCOPE
        row.pop("survived_final_text_srt", None)
        row.pop("survived_final_speaker_srt", None)
        count += 1
    return count


def gift_correction_receipts_valid(audit: Mapping, final: str, *, delivery_start_ms: int) -> bool:
    rows = [r for r in (audit.get("gift_repairs") or []) if isinstance(r, Mapping)
            and (RECEIPT_KEY in r or r.get("final_verification_scope") == SCOPE)]
    count = audit.get(COUNTER, 0)
    if type(count) is not int or count != len(rows):
        return False
    if not rows:
        return True
    context = _context(audit, final, delivery_start_ms)
    if context is None:
        return False
    return all(r.get("final_verification_scope") == SCOPE
               and _receipt(r, context, final, delivery_start_ms) == r.get(RECEIPT_KEY)
               and r.get(RECEIPT_KEY) is not None for r in rows)


def audit_package_gift_corrections(*, audit: Mapping, record: Mapping,
                                  subtitle_path: Path | None, report_invalid) -> None:
    if not audit.get(COUNTER) and not any(isinstance(r, Mapping) and
            (RECEIPT_KEY in r or r.get("final_verification_scope") == SCOPE)
            for r in (audit.get("gift_repairs") or [])):
        return
    try:
        boundary = record.get("boundary_audit") or {}
        if subtitle_path is None or subtitle_path.is_symlink() or not subtitle_path.is_file():
            raise ValueError("Missing canonical final subtitles")
        raw = subtitle_path.read_bytes()
        valid = gift_correction_receipts_valid(audit, raw.decode("utf-8"),
            delivery_start_ms=boundary.get("final_start_ms"))
    except (OSError, UnicodeError, TypeError, ValueError, KeyError):
        valid = False
    if not valid:
        report_invalid()
