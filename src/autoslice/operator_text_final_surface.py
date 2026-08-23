"""Final-surface authority for exhaustive operator-reviewed subtitle baselines."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping


def apply_operator_text_final_surface_supersession(
    chat_authority_audit: dict,
    *,
    ownership: Mapping[str, object] | None,
    final_text_srt: str,
    final_speaker_srt: str,
) -> bool:
    """Retire older text owners when Ivan owns the complete sealed cue grid."""

    if ownership is None:
        return False
    chat_authority_audit[
        "operator_text_full_ownership_final_surface_supersession"
    ] = {
        "schema_version": "operator-text-full-ownership-final-surface-supersession.v1",
        "status": "SUPERSEDED_BY_OPERATOR_REVIEWED_BASELINE",
        "ownership": dict(ownership),
        "final_text_srt_sha256": hashlib.sha256(final_text_srt.encode("utf-8")).hexdigest(),
        "final_speaker_srt_sha256": hashlib.sha256(
            final_speaker_srt.encode("utf-8")
        ).hexdigest(),
    }
    return True
