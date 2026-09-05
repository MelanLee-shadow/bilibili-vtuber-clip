"""Deterministic subtitle draft preparation and strict CPA response parsing."""

from __future__ import annotations

import hashlib

from src.autoslice.chat_evidence import normalize_code_switch_surfaces
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.surface_canon import normalize_expected_value_surfaces
from src.autoslice.term_boundary import unify_terms_across_cues


def _asr_ts(ms: int) -> str:
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _required_cpa_cues(prompt: str, cpa_llm_call, cue_count: int) -> dict[int, str]:
    """A failed or partial review is not a successful zero-edit review."""
    from src.autoslice.llm_client import LlmCallError, extract_json_object
    from src.autoslice.source_context_executor import AgyRunnerError

    try:
        items = extract_json_object(cpa_llm_call(prompt))["cues"]
        if not isinstance(items, list) or len(items) != cue_count:
            raise ValueError("incomplete cue set")
        corrected = {}
        for item in items:
            if (not isinstance(item, dict) or type(item.get("n")) is not int
                    or not isinstance(item.get("text"), str)
                    or item["n"] in corrected):
                raise ValueError("invalid or duplicate cue")
            corrected[item["n"]] = item["text"]
        if set(corrected) != set(range(1, cue_count + 1)):
            raise ValueError("out-of-range cue set")
        if not any(text.strip() for text in corrected.values()):
            raise ValueError("empty reviewed transcript")
        return corrected
    except (LlmCallError, ValueError, KeyError, TypeError) as exc:
        raise AgyRunnerError(
            "CPA_CORRECTION_UNAVAILABLE" if isinstance(exc, LlmCallError) else "CPA_CORRECTION_INVALID_OUTPUT",
            "CPA did not return a complete usable subtitle review"
        ) from exc


def _prepare_cpa_draft(raw_srt: str, term_boundary_surfaces=(), *, blocked_boundaries=()):
    """Keep lossless boundary moves separate from authorized spelling edits."""
    original = parse_srt_cues(raw_srt)
    cues, moves = unify_terms_across_cues(
        original, term_boundary_surfaces, blocked_boundaries=blocked_boundaries
    )
    if "".join(c.text for c in cues) != "".join(c.text for c in original):
        cues, moves = original, []
    boundary_srt = raw_srt
    if moves:
        boundary_srt = "\n\n".join(
            f"{i}\n{_asr_ts(c.start_ms)} --> {_asr_ts(c.end_ms)}\n{c.text}"
            for i, c in enumerate(cues, 1)
        ) + "\n"
    prepared, surface_audit = normalize_code_switch_surfaces(boundary_srt)
    prepared, expected_audit = normalize_expected_value_surfaces(prepared)
    return boundary_srt, prepared, {
        "raw_srt_sha256": hashlib.sha256(raw_srt.encode()).hexdigest(),
        "prepared_srt_sha256": hashlib.sha256(prepared.encode()).hexdigest(),
        "term_boundary_moves": moves,
        "blocked_term_boundaries": list(blocked_boundaries),
        "surface_canon": surface_audit,
        "expected_value_canon": expected_audit,
        "authority": "authorized_text_rules_not_independent_audio",
    }


__all__ = ["_prepare_cpa_draft", "_required_cpa_cues"]
