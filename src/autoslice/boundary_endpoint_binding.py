"""Bind a semantic boundary decision to the cue grid actually delivered."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.jingting_chunker import parse_srt_cues


def final_delivery_review_matches_srt(
    review: object,
    subtitle_path: Path | None,
) -> bool:
    """Recompute the delivery grid and terminal cue from package bytes."""

    if not isinstance(review, Mapping) or subtitle_path is None:
        return False
    try:
        cues = [
            cue
            for cue in parse_srt_cues(
                subtitle_path.read_bytes().decode("utf-8", errors="strict")
            )
            if cue.text.strip()
        ]
    except (OSError, UnicodeError, ValueError):
        return False
    endpoint = review.get("final_endpoint_binding")
    if not cues or not isinstance(endpoint, Mapping):
        return False
    closure = cues[-1]
    closure_sha256 = "sha256:" + hashlib.sha256(
        closure.text.encode("utf-8")
    ).hexdigest()
    return bool(
        review.get("cue_grid_sha256") == cue_grid_sha256(cues)
        and review.get("recommended_end_cue_index") == len(cues)
        and review.get("recommended_end_ms") == closure.end_ms
        and endpoint.get("closure_text_sha256") == closure_sha256
    )


def bind_final_semantic_endpoint(
    *,
    semantic_review: Mapping[str, object] | None,
    cues: Sequence[object],
    closure_cue: object,
    snapped_end_ms: int,
    final_start_ms: int,
    final_end_ms: int,
) -> tuple[dict[str, object], list[str]]:
    """Bind one semantic vote to an exact endpoint and complete cue grid."""

    review = dict(semantic_review or {})
    recommended_index = review.get("recommended_end_cue_index")
    semantic_cues = [
        cue
        for cue in cues
        if str(getattr(cue, "text", "") or "").strip()
    ]
    closure_positions = [
        position
        for position, cue in enumerate(semantic_cues, start=1)
        if int(getattr(cue, "end_ms")) == int(snapped_end_ms)
    ]
    closure_index = (
        closure_positions[0] if len(closure_positions) == 1 else None
    )
    final_grid_sha256 = cue_grid_sha256(semantic_cues)
    reasons: list[str] = []
    if review.get("status") != "PASS":
        reasons.append("BOUNDARY_SEMANTIC_REVIEW_NOT_PASS")
    if review.get("cue_grid_sha256") != final_grid_sha256:
        reasons.append("BOUNDARY_SEMANTIC_CUE_GRID_MISMATCH")
    if review.get("recommended_end_ms") != snapped_end_ms:
        reasons.append("BOUNDARY_SEMANTIC_ENDPOINT_MS_MISMATCH")
    if (
        isinstance(recommended_index, bool)
        or not isinstance(recommended_index, int)
        or recommended_index != closure_index
    ):
        reasons.append("BOUNDARY_SEMANTIC_ENDPOINT_CUE_MISMATCH")
    review["final_endpoint_binding"] = {
        "schema_version": "talk-boundary-final-endpoint-binding.v1",
        "status": "BLOCK" if reasons else "PASS",
        "semantic_request_sha256": review.get("request_sha256"),
        "semantic_cue_grid_sha256": review.get("cue_grid_sha256"),
        "final_cue_grid_sha256": final_grid_sha256,
        "recommended_end_cue_index": recommended_index,
        "recommended_end_ms": review.get("recommended_end_ms"),
        "final_closure_cue_index": closure_index,
        "final_snapped_end_ms": snapped_end_ms,
        "final_start_ms": final_start_ms,
        "final_end_ms": final_end_ms,
        "closure_text_sha256": (
            "sha256:"
            + hashlib.sha256(
                str(getattr(closure_cue, "text")).encode("utf-8")
            ).hexdigest()
        ),
        "reason_codes": reasons,
    }
    return review, reasons
