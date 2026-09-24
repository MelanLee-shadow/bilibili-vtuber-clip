"""Fail-closed hardlink reuse for an already materialized final recut."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Mapping

CONFIG_KEY = "existing_final_recut"
SCHEMA_VERSION = "existing-final-recut.v1"
AUTHORITY_KIND = "HASH_BOUND_EXISTING_FINAL_RECUT_NOT_PUBLICATION_AUTHORITY"
RECEIPT_SCHEMA = "existing-final-recut-consumption.v1"
PROVENANCE_SCHEMA = "lidousha-speaker-recut-provenance.v1"
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
        raise ValueError(f"existing final recut {label} is invalid")
    return "sha256:" + match.group(1)


def _regular_file(path: Path, *, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(
            f"existing final recut {label} must be a regular non-symlink file"
        )
    return path


def _load_contract(
    spec: Mapping[str, object], *, padded: Path
) -> tuple[Mapping[str, object], Path, str, str]:
    config = spec.get(CONFIG_KEY)
    if not isinstance(config, Mapping):
        raise ValueError(f"{CONFIG_KEY} must be an object")
    required = {
        "schema_version",
        "candidate_id",
        "path",
        "sha256",
        "provenance_path",
        "provenance_sha256",
        "source_media_sha256",
        "final_start_ms",
        "final_end_ms",
        "time_domain",
        "authority_kind",
    }
    if set(config) != required or config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("existing final recut schema is invalid")
    if config.get("candidate_id") != spec.get("candidate_id"):
        raise ValueError("existing final recut candidate binding does not match")
    if config.get("time_domain") != "padded_local":
        raise ValueError("existing final recut time domain must be padded_local")
    if config.get("authority_kind") != AUTHORITY_KIND:
        raise ValueError("existing final recut authority kind is invalid")

    media = _regular_file(Path(str(config.get("path") or "")), label="path")
    provenance_path = _regular_file(
        Path(str(config.get("provenance_path") or "")), label="provenance path"
    )
    source = _regular_file(padded, label="source media")
    media_sha = _digest(media)
    provenance_sha = _digest(provenance_path)
    source_sha = _digest(source)
    if media_sha != _expected_sha(config.get("sha256"), label="media sha256"):
        raise ValueError("existing final recut media sha256 does not match")
    if provenance_sha != _expected_sha(
        config.get("provenance_sha256"), label="provenance sha256"
    ):
        raise ValueError("existing final recut provenance sha256 does not match")
    if source_sha != _expected_sha(
        config.get("source_media_sha256"), label="source media sha256"
    ):
        raise ValueError("existing final recut source media sha256 does not match")

    try:
        document = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("existing final recut provenance is unreadable") from exc
    final = document.get("final_recut") if isinstance(document, Mapping) else None
    if (
        document.get("schema_version") != PROVENANCE_SCHEMA
        or not isinstance(final, Mapping)
        or _expected_sha(final.get("source_sha256"), label="provenance source sha256")
        != source_sha
        or _expected_sha(final.get("output_sha256"), label="provenance output sha256")
        != media_sha
        or int(final.get("start_ms", -1)) != config.get("final_start_ms")
        or int(final.get("end_ms", -1)) != config.get("final_end_ms")
    ):
        raise ValueError("existing final recut provenance binding is invalid")
    try:
        if Path(str(final.get("output_path") or "")).resolve(strict=True) != media.resolve(
            strict=True
        ):
            raise ValueError("existing final recut provenance output path is invalid")
    except OSError as exc:
        raise ValueError("existing final recut provenance output path is invalid") from exc
    return config, media, provenance_sha, source_sha


def select_accurate_recut_command(
    *,
    spec: dict,
    padded: Path,
    chat_authority_audit: dict,
    legacy_command: object,
) -> object:
    """Return the normal ffmpeg builder or a validated hardlink builder."""

    if CONFIG_KEY not in spec:
        return legacy_command
    config, media, provenance_sha, source_sha = _load_contract(spec, padded=padded)
    media_sha = _digest(media)

    def command_builder(
        *, source_video: Path, output_media: Path, start_ms: int, duration_ms: int
    ) -> list[str]:
        if source_video.resolve(strict=True) != padded.resolve(strict=True):
            raise ValueError("existing final recut source path drifted")
        final_end_ms = start_ms + duration_ms
        if (
            start_ms != config.get("final_start_ms")
            or final_end_ms != config.get("final_end_ms")
        ):
            raise ValueError("existing final recut boundary does not match")
        if os.path.lexists(output_media):
            raise ValueError("existing final recut destination already exists")
        if media.stat().st_dev != output_media.parent.stat().st_dev:
            raise ValueError("existing final recut hardlink device does not match")
        chat_authority_audit["existing_final_recut_receipt"] = {
            "schema_version": RECEIPT_SCHEMA,
            "status": "PASS",
            "candidate_id": str(config["candidate_id"]),
            "media_sha256": media_sha,
            "source_media_sha256": source_sha,
            "provenance_sha256": provenance_sha,
            "final_start_ms": start_ms,
            "final_end_ms": final_end_ms,
            "time_domain": "padded_local",
            "authority_kind": AUTHORITY_KIND,
            "materialization": "HARDLINK_REUSE",
            "ffmpeg_calls": 0,
            "provider_calls": 0,
        }
        return ["/bin/ln", str(media), str(output_media)]

    return command_builder
