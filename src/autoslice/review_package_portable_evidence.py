"""Fail-closed path resolution for portable review-package evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.addressee_attribution import (
    SpeakerEvidenceRejected,
    rebuild_speaker_evidence,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.review_evidence import SourceCue


def contained_package_artifact(root: Path, value: object, *, label: str) -> Path:
    """Resolve one package artifact without trusting producer absolute paths."""

    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} path is missing")
    raw = Path(value)
    relative = Path(raw.name) if raw.is_absolute() else raw
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{label} is not a safe package-relative path")
    try:
        resolved_root = root.resolve(strict=True)
        candidate = resolved_root
        for part in relative.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError(f"{label} package path contains a symlink")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is not package-contained") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} is not a regular package artifact")
    return resolved


def resolve_portable_primary_artifacts(
    root: Path,
    item: Mapping[str, object],
) -> tuple[dict[str, Path | None], list[tuple[str, str]]]:
    """Resolve the four story inputs and report every containment failure."""

    fields = {
        "subtitle": (item.get("subtitle_srt") or item.get("subtitle"), "reviewed subtitle"),
        "evidence": (item.get("evidence_json") or item.get("evidence"), "record evidence"),
        "publish": (item.get("publish_json") or item.get("publish"), "publish draft"),
        "title": (item.get("title_txt") or item.get("title_path"), "title text"),
    }
    paths: dict[str, Path | None] = {}
    errors: list[tuple[str, str]] = []
    for name, (value, label) in fields.items():
        if value in (None, ""):
            paths[name] = None
            continue
        try:
            paths[name] = contained_package_artifact(root, value, label=label)
        except ValueError as exc:
            paths[name] = None
            errors.append((label, str(exc)))
    return paths, errors


def rebuild_package_speaker_evidence(
    *,
    root: Path,
    item: Mapping[str, object],
    record: Mapping[str, object],
    subtitle_path: Path | None,
) -> dict[str, object]:
    """Rebuild source-fact speaker evidence exclusively from package bytes."""

    if subtitle_path is None:
        raise ValueError("reviewed subtitle is missing")
    subtitle = contained_package_artifact(
        root,
        item.get("subtitle_srt") or item.get("subtitle"),
        label="reviewed subtitle",
    )
    try:
        parsed_cues = parse_srt_cues(subtitle.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("reviewed subtitle is unreadable") from exc
    cues = [
        SourceCue(
            cue_id=str(cue.index),
            source_start_ms=cue.start_ms,
            source_end_ms=cue.end_ms,
            text=cue.text,
        )
        for cue in parsed_cues
    ]
    raw_speaker_path = record.get("speaker_review_srt_path")
    raw_manifest_path = record.get("speaker_finalization_manifest_path")
    claims_speaker_evidence = any(
        value is not None
        for value in (
            raw_speaker_path,
            raw_manifest_path,
            record.get("speaker_finalization"),
            record.get("speaker_finalization_manifest_sha256"),
        )
    )
    speaker_srt_bytes: bytes | None = None
    manifest_bytes: bytes | None = None
    if claims_speaker_evidence:
        speaker_srt = contained_package_artifact(
            root,
            item.get("speaker_srt") or raw_speaker_path,
            label="speaker-final SRT",
        )
        speaker_manifest = contained_package_artifact(
            root,
            item.get("speaker_finalization_manifest") or raw_manifest_path,
            label="speaker finalization manifest",
        )
        speaker_srt_bytes = speaker_srt.read_bytes()
        manifest_bytes = speaker_manifest.read_bytes()
        declared_manifest_sha256 = item.get("speaker_finalization_manifest_sha256")
        if declared_manifest_sha256 is not None and declared_manifest_sha256 != (
            "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        ):
            raise ValueError(
                "speaker finalization manifest item hash does not match package bytes"
            )
    try:
        result = rebuild_speaker_evidence(
            record,
            cues,
            speaker_srt_bytes=speaker_srt_bytes,
            speaker_manifest_bytes=manifest_bytes,
        )
    except SpeakerEvidenceRejected as exc:
        raise ValueError(f"{exc.code}: {exc.detail}") from exc
    return dict(result.speaker_evidence)


def portable_item_artifact_path(
    root: Path, item: Mapping[str, Any], *keys: str
) -> Path | None:
    """Return one package-relative regular file with no symlink component."""

    value = next((item.get(key) for key in keys if item.get(key)), None)
    if not isinstance(value, str) or not value:
        return None
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        return None
    resolved_root = root.resolve()
    cursor = root
    for part in raw.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return None
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _legacy_resolve(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        if path.exists():
            return path
        candidates = list(root.rglob(path.name))
        return candidates[0] if candidates else path
    return root / path


def resolve_review_item_evidence(
    *,
    root: Path,
    item: Mapping[str, Any],
    portable_required: bool,
    stem: str,
    manifest_path: Path,
) -> tuple[Path | None, Path | None, Path | None, list[dict[str, str]]]:
    """Resolve record/chat/context together and emit portable path failures."""

    keys = {
        "record": ("record", "record_json"),
        "chat_authority": ("chat_authority",),
        "clip_context": ("clip_context_json", "clip_context"),
    }
    if not portable_required:
        return (
            _legacy_resolve(root, next((item.get(k) for k in keys["record"] if item.get(k)), None)),
            _legacy_resolve(root, item.get("chat_authority")),
            _legacy_resolve(root, next((item.get(k) for k in keys["clip_context"] if item.get(k)), None)),
            [],
        )
    paths = {
        name: portable_item_artifact_path(root, item, *aliases)
        for name, aliases in keys.items()
    }
    codes = {
        "record": "RECORD_PATH_MISSING_OR_NONPORTABLE",
        "chat_authority": "CHAT_AUTHORITY_PATH_MISSING_OR_NONPORTABLE",
        "clip_context": "CLIP_CONTEXT_PATH_MISSING_OR_NONPORTABLE",
    }
    issues = [
        {
            "code": codes[name],
            "severity": "BLOCK",
            "stem": stem,
            "path": str(manifest_path),
            "detail": (
                f"item.{name} must name a package-relative regular file "
                "without traversal or symlink components"
            ),
        }
        for name, path in paths.items()
        if path is None
    ]
    return paths["record"], paths["chat_authority"], paths["clip_context"], issues
