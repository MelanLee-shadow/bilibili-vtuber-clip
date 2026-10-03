"""Materialization consumer for explicitly declared watched-media caption policy.

The complete current transcript remains evidence. Only the rendered subtitle
track is filtered, before exact-final review, speaker ASS generation, and burn.
This adapter never grants publication or changes the chosen source interval.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.nested_media_caption_dedup import CaptionDedupError, apply_bound_display_policy


def _immutable_sidecar(path: Path, data: bytes) -> None:
    """Create once, or verify the identical prior bytes on an idempotent replay."""
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise CaptionDedupError("CAPTION_DEDUP_SIDECAR_CONFLICT") from None


def consume_nested_caption_policy(
    *, spec: Mapping[str, object], candidate_id: str, source_media: Path,
    source_start_ms: int, source_end_ms: int, subtitle_path: Path,
    evidence_root: Path, chat_authority_audit: dict | None = None,
) -> str:
    """Consume declared evidence on the real materializer's current SRT bytes.

    A missing declaration leaves ordinary content unchanged. A positive scene
    declaration without its caption evidence is a typed block, not an unnoticed
    fallback to burning every voice. Detection/collection supplies the declaration;
    this deterministic consumer does not infer scene type from title keywords.
    """
    before = subtitle_path.read_bytes()
    transcript = before.decode("utf-8", errors="strict")
    config = spec.get("nested_media_caption_dedup")
    if spec.get("watched_media") is True and config is None:
        raise CaptionDedupError("WATCHED_MEDIA_CAPTION_EVIDENCE_REQUIRED")
    if config is None:
        return transcript
    output, audit = apply_bound_display_policy(
        config=config, candidate_id=candidate_id, source_media=source_media,
        transcript=transcript, source_start_ms=source_start_ms, source_end_ms=source_end_ms,
        evidence_root=evidence_root,
    )
    assert audit is not None
    archive = subtitle_path.with_suffix(".all-source-transcript.txt")
    receipt = subtitle_path.with_suffix(".caption-dedup.json")
    audit.update(
        all_source_transcript_path=str(archive),
        all_source_transcript_sha256=hashlib.sha256(before).hexdigest(),
        display_srt_path=str(subtitle_path),
        source_interval_unchanged=True,
        exact_final_review_required=True,
    )
    # Preserve the evidence and receipt before changing the current display file.
    _immutable_sidecar(archive, before)
    _immutable_sidecar(receipt, (json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
    if subtitle_path.read_bytes() != before or subtitle_path.is_symlink():
        raise CaptionDedupError("CAPTION_DEDUP_TRANSCRIPT_CHANGED_DURING_APPLY")
    if output != transcript:
        temporary = subtitle_path.with_suffix(".caption-dedup.pending")
        with temporary.open("xb") as handle:
            handle.write(output.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, subtitle_path)
    if chat_authority_audit is not None:
        chat_authority_audit["nested_media_caption_dedup"] = audit
        chat_authority_audit["nested_media_caption_dedup_receipt"] = {
            "path": str(receipt), "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
        }
    return output
