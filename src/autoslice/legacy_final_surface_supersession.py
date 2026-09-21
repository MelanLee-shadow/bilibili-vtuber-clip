"""Existing exact-final receipt-chain handling; no policy change."""
from __future__ import annotations
import hashlib
from collections.abc import Mapping
from .jingting_chunker import parse_srt_cues


def _exact_final_cpa_retires_decision_row(
    row: dict,
    *,
    expected_text: str,
    audit: Mapping[str, object],
    final_text_srt: str,
    delivery_start_ms: int,
) -> bool:
    """Retire an older correction only through an exact CPA receipt chain.

    Older packages predate explicit exact-final surface-owner rows. Their
    self-heal receipts are still sufficient when the prior owner's text hash,
    unchanged cue geometry, ordered before->after chain, and final SRT hash all
    agree. This is intentionally narrower than generic overlap supersession.
    """

    self_heal = audit.get("exact_final_cpa_self_heal")
    if (
        not isinstance(self_heal, Mapping)
        or self_heal.get("schema_version")
        != "exact-final-cpa-self-heal-audit.v1"
        or self_heal.get("status") != "PASS"
        or self_heal.get("final_srt_sha256")
        != "sha256:" + hashlib.sha256(final_text_srt.encode("utf-8")).hexdigest()
        or not expected_text
    ):
        return False
    try:
        matched_start = int(row["matched_start_ms"])
        matched_end = int(row["matched_end_ms"])
    except (KeyError, TypeError, ValueError):
        return False
    local_start = matched_start - delivery_start_ms
    local_end = matched_end - delivery_start_ms
    cue_matches = [
        cue
        for cue in parse_srt_cues(final_text_srt)
        if cue.start_ms == local_start and cue.end_ms == local_end
    ]
    if len(cue_matches) != 1:
        return False
    cue = cue_matches[0]
    chain_hash = "sha256:" + hashlib.sha256(
        expected_text.encode("utf-8")
    ).hexdigest()
    final_hash = "sha256:" + hashlib.sha256(
        cue.text.encode("utf-8")
    ).hexdigest()
    consumed: list[dict[str, object]] = []
    passes = self_heal.get("passes")
    if not isinstance(passes, list):
        return False
    for pass_row in passes:
        if not isinstance(pass_row, Mapping):
            return False
        repairs = pass_row.get("repairs")
        if not isinstance(repairs, list):
            return False
        for repair in repairs:
            if not isinstance(repair, Mapping):
                return False
            mutation = repair.get("mutation_authority")
            if (
                repair.get("schema_version")
                != "exact-final-cpa-self-heal.v1"
                or str(repair.get("cue_index")) != str(cue.index)
                or repair.get("decision_authority") != "CPA_JUDGE"
                or repair.get("timing_immutable") is not True
                or not isinstance(mutation, Mapping)
                or mutation.get("schema_version")
                != "subtitle-correction-mutation-authority.v1"
                or mutation.get("status") != "PASS"
            ):
                continue
            before_hash = repair.get("before_sha256")
            after_hash = repair.get("after_sha256")
            if (
                before_hash == chain_hash
                and isinstance(after_hash, str)
                and len(after_hash) == 71
                and after_hash.startswith("sha256:")
            ):
                chain_hash = after_hash
                consumed.append(
                    {
                        "pass_index": pass_row.get("pass_index"),
                        "finding_sha256": repair.get("finding_sha256"),
                        "request_sha256": repair.get("request_sha256"),
                        "before_sha256": before_hash,
                        "after_sha256": after_hash,
                    }
                )
    if not consumed or chain_hash != final_hash:
        return False
    row["reconciliation"] = {
        "schema_version": "exact-final-cpa-receipt-chain-supersession.v1",
        "status": "SUPERSEDED_BY_EXACT_FINAL_CPA",
        "cue_index": cue.index,
        "matched_start_ms": matched_start,
        "matched_end_ms": matched_end,
        "final_cue_sha256": final_hash,
        "receipt_chain": consumed,
        "timing_immutable": True,
    }
    return True
