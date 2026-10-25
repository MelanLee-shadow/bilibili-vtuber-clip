"""Materialization consumer for explicitly declared watched-media caption policy.

The complete current transcript remains evidence. Only the rendered subtitle
track is filtered, before exact-final review, speaker ASS generation, and burn.
This adapter never grants publication or changes the chosen source interval.
"""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.nested_media_caption_dedup import CaptionDedupError, apply_bound_display_policy
from src.autoslice.exact_final_cpa_history import with_nested_caption_successor


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


def _sidecar_binding_sha256(audit: Mapping[str, object]) -> str:
    """Hash the complete policy audit before adding output-path metadata."""
    canonical = json.dumps(
        audit, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _sidecar_paths(subtitle_path: Path, binding_sha256: str) -> tuple[Path, Path]:
    """Return immutable sidecars for one source/evidence binding."""
    stem = subtitle_path.with_suffix("")
    return (
        stem.with_name(f"{stem.name}.all-source-transcript.{binding_sha256}.txt"),
        stem.with_name(f"{stem.name}.caption-dedup.{binding_sha256}.json"),
    )


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
    binding_sha256 = _sidecar_binding_sha256(audit)
    archive, receipt = _sidecar_paths(subtitle_path, binding_sha256)
    audit.update(
        sidecar_binding_sha256=binding_sha256,
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
    if output != transcript and chat_authority_audit is not None:
        previous = chat_authority_audit.get("exact_final_cpa_self_heal")
        if isinstance(previous, Mapping) and previous.get("passes"):
            chat_authority_audit["exact_final_cpa_self_heal"] = with_nested_caption_successor(
                previous, before=transcript, after=output, config=config,
                candidate_id=candidate_id, source_media=source_media,
                source_start_ms=source_start_ms, source_end_ms=source_end_ms,
                evidence_root=evidence_root,
            )
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
            "path": str(receipt), "sha256": _evidence_file_sha256(receipt),
        }
    return output
