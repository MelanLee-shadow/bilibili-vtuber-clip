"""Retain the native intermediate text inside its existing chat audit.

This is recovery data, not final-review authority. It preserves the exact
post-correction string even when a later language gate interrupts production.
No provider calls, inferred repairs, filename fallback or auto-resume occur.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
import hashlib

TEXT_KEY = "post_transcript_entity_output_srt"
SHA_KEY = "post_transcript_entity_output_srt_sha256"


def retain_post_transcript_text(audit: MutableMapping[str, object], srt_text: str) -> None:
    """Bind the exact string at the existing native post-transcript checkpoint."""
    # Preserve the established raw UTF-8 digest; do not normalize newlines or cues.
    digest = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    audit[SHA_KEY] = digest
    audit[TEXT_KEY] = srt_text


def load_post_transcript_text(audit: Mapping[str, object]) -> str | None:
    """Read a validated intermediate snapshot; old hash-only audits return None.

    A returned string still needs its original candidate/source/context and
    mutation receipts and all later native gates. A matching hash is not a
    claim that its words are correct or that it is the final delivery text.
    """
    if TEXT_KEY not in audit:
        return None
    text = audit[TEXT_KEY]
    expected = audit.get(SHA_KEY)
    if not isinstance(text, str) or not isinstance(expected, str):
        raise ValueError("POST_TRANSCRIPT_TEXT_CHECKPOINT_INVALID")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
        raise ValueError("POST_TRANSCRIPT_TEXT_CHECKPOINT_INVALID")
    return text
