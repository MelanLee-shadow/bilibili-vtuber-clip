"""Cold continuation: which persisted review state the ordinary writers left.

Two relations shared by every continuation reader (see
``producer_text_pipeline._continuation_validate_existing_authority`` and
``_continuation_project_delivery_to_source``): an unfinished review stage, and
the earlier bytes a pending typed suffix's review actually covered.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from .finding_lifecycle import only_finding_failures
from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues


def unavailable_review_stage(review: Mapping[str, Any]) -> bool:
    """An unfinished exact-review stage, as its writers record it.

    The review callback returns ``provider_failure.auditor_unavailable_discovery``
    with its stage's ``*_PROVIDER_OR_JSON_UNAVAILABLE`` code; the ordinary gate
    may then add the still-pending span obligations and their engineering
    lifecycle rows before persisting it. Either shape is review input only. Any
    finding whose release code is not accounted for, any true content block or
    any other reason disqualifies it.
    """
    from .finding_lifecycle import CONTENT_BLOCK, release_reason_codes

    discovery, codes, findings = review.get("discovery"), review.get("reason_codes"), review.get("findings")
    if not (isinstance(discovery, Mapping) and discovery.get("status") == "AUDITOR_UNAVAILABLE"
            and review.get("release_gate") == "BLOCK"
            and review.get("status") in {"AUDITOR_UNAVAILABLE", "FLAGGED"}
            and isinstance(codes, list) and codes and all(isinstance(code, str) for code in codes)
            and isinstance(findings, list) and review.get("validated_finding_count") == len(findings)):
        return False
    stage = [code for code in codes if code.endswith("_PROVIDER_OR_JSON_UNAVAILABLE")]
    rest = [code for code in codes if code not in stage]
    if not stage:
        return False
    if not findings:
        return not rest
    try:
        produced = release_reason_codes(findings, terminal=bool(review.get("terminal_funnel")))
    except ValueError:
        return False
    return bool(CONTENT_BLOCK not in produced and CONTENT_BLOCK not in rest
                and set(produced) <= set(rest) and only_finding_failures(rest))


def reviewed_reference_cues(
    authority: Mapping[str, object], *, final_review_audit: Mapping[str, object],
    cues: Sequence[object],
) -> list[SrtCue] | None:
    """Cues of the earlier bytes ``final_review_audit`` reviewed, via the typed chain.

    Walk back from the current bytes through the run's registered successors
    (which keep their complete input) and text-only passes (each cue's own
    ``before``), until the reviewed digest is reached. Authenticate the complete
    forward suffix with the same typed-pass replay used by final ownership and
    the shared successor reader. Only explicitly replayed edges may change the
    cue grid; this reconstructs review input and grants no current geometry or
    release verdict.
    """
    from src.autoslice.legacy_final_surface_supersession import (
        _render_srt_cues as _render, _replay_typed_pass,
    )
    from src.autoslice.exact_final_cpa_history import read_successors, ordered_successors
    from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal

    reviewed = final_review_audit.get("reviewed_srt_sha256")
    receipt = authority.get("exact_final_cpa_self_heal")
    passes = receipt.get("passes") if isinstance(receipt, Mapping) else None
    if not isinstance(reviewed, str) or not isinstance(passes, list) or not passes:
        return None
    current_text = _render(list(cues))

    def digest(text: str) -> str:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    current_sha = digest(current_text)
    if reviewed == current_sha:
        return None
    _validate_exact_final_cpa_self_heal(authority, expected_srt_sha256=current_sha, allow_pending=True)
    groups = read_successors(receipt)
    successors = [row for group in groups.values() for row in group]
    text = current_text
    for row in reversed(passes):
        if not isinstance(row, Mapping):
            return None
        while True:
            undo = [s for s in successors if s.get("after_pass_index") == row.get("pass_index")
                    and s.get("output_srt_sha256") == digest(text)]
            if not undo:
                break
            if len(undo) != 1 or not isinstance(undo[0].get("input_srt_text"), str):
                return None
            text = undo[0]["input_srt_text"]
            successors.remove(undo[0])
        if row.get("output_srt_sha256") != digest(text):
            return None
        text = _replay_typed_pass(text, row, reverse=True)
        if text is None:
            return None
        if digest(text) == reviewed:
            replay = text
            start = passes.index(row)
            for forward in passes[start:]:
                replay = _replay_typed_pass(replay, forward, reverse=False)
                if replay is None:
                    return None
                for edge in ordered_successors(groups.get(forward["pass_index"], []), digest(replay)):
                    if replay != edge["input_srt_text"]:
                        return None
                    replay = edge["output_srt_text"]
            if replay != current_text:
                return None
            return list(parse_srt_cues(text))
    return None
