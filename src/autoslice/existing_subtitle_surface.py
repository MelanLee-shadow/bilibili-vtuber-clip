"""Hash-bound reuse of an existing padded-timeline subtitle surface.

This is a transcript substrate, not human truth. It avoids a new ASR pass while
leaving the ordinary correction, boundary, exact-final, audio, title, cover,
and package gates in force.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues

CONFIG_KEY = "existing_subtitle_surface"
SCHEMA_VERSION = "existing-subtitle-surface.v1"
AUTHORITY_KIND = "PIPELINE_REVIEWED_CPA_SURFACE_NOT_HUMAN_TRUTH"
RECEIPT_SCHEMA = "existing-subtitle-surface-consumption.v1"
_SHA256 = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _expected_sha(value: object, *, label: str) -> str:
    match = _SHA256.fullmatch(str(value or "").strip())
    if match is None:
        raise ValueError(f"existing subtitle {label} is invalid")
    return "sha256:" + match.group(1)


def _regular_file(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(
            f"existing subtitle {label} must be a regular non-symlink file"
        )
    return path


def _load_surface(
    spec: Mapping[str, object], *, padded: Path, padded_duration_ms: int
) -> tuple[str, dict[str, object]]:
    config = spec.get(CONFIG_KEY)
    if not isinstance(config, Mapping):
        raise ValueError(f"{CONFIG_KEY} is required for existing_srt")
    required = {
        "schema_version",
        "candidate_id",
        "path",
        "sha256",
        "source_media_sha256",
        "time_domain",
        "authority_kind",
    }
    if set(config) != required or config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("existing subtitle surface schema is invalid")
    if config.get("candidate_id") != spec.get("candidate_id"):
        raise ValueError("existing subtitle candidate binding does not match")
    if config.get("time_domain") != "padded_local":
        raise ValueError("existing subtitle time domain must be padded_local")
    if config.get("authority_kind") != AUTHORITY_KIND:
        raise ValueError("existing subtitle authority kind is invalid")

    subtitle = _regular_file(Path(str(config.get("path") or "")), label="path")
    media = _regular_file(padded, label="source media")
    subtitle_sha = _digest(subtitle)
    media_sha = _digest(media)
    if subtitle_sha != _expected_sha(config.get("sha256"), label="sha256"):
        raise ValueError("existing subtitle sha256 does not match")
    if media_sha != _expected_sha(
        config.get("source_media_sha256"), label="source media sha256"
    ):
        raise ValueError("existing subtitle source media sha256 does not match")
    try:
        text = subtitle.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("existing subtitle surface is not UTF-8") from exc
    cues = parse_srt_cues(text)
    if not cues:
        raise ValueError("existing subtitle surface has no cues")
    if any(
        cue.start_ms < 0
        or cue.end_ms <= cue.start_ms
        or cue.end_ms > padded_duration_ms
        for cue in cues
    ):
        raise ValueError("existing subtitle cue is outside padded media")
    receipt: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "status": "PASS",
        "candidate_id": str(config["candidate_id"]),
        "subtitle_sha256": subtitle_sha,
        "source_media_sha256": media_sha,
        "time_domain": "padded_local",
        "authority_kind": AUTHORITY_KIND,
        "cue_count": len(cues),
        "asr_calls": 0,
        "provider_calls": 0,
    }
    return text, receipt


def select_nonaggregate_transcriber_builder(
    substrate: str,
    *,
    spec: dict,
    padded: Path,
    padded_duration_ms: int,
    legacy_builder: object,
) -> object:
    """Return the legacy AGY builder, or a zero-ASR exact-surface builder."""

    if substrate != "existing_srt":
        return legacy_builder
    text, receipt = _load_surface(
        spec, padded=padded, padded_duration_ms=padded_duration_ms
    )
    spec["existing_subtitle_surface_receipt"] = receipt

    def builder(_host: str, **_kwargs: object) -> Callable[..., str]:
        def transcribe(_media: Path, _spans: object) -> str:
            return text

        return transcribe

    return builder
