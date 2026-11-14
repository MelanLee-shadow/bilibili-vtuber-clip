"""Shared constants and tiny helpers for the finished-media review lane (FMR).

The lane is an upload-compatibility interface for hand-made finished media: it
binds the reviewer's verdict and report to the exact bytes.  It never edits
subtitles or media; see ``docs/pipeline/85-finished-media-review.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

MEMBER_SCHEMA = "finished-media-member.v1"
PACKAGE_SCHEMA = "finished-media-review-package.v1"
RECEIPT_SCHEMA = "finished-media-review-receipt.v2"
AUTO_CHECKS_SCHEMA = "finished-media-auto-checks.v1"
RECORD_KIND = "FINISHED_MEDIA_REVIEW"

_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SHA_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

EVIDENCE_DIR = "evidence"
AUTO_CHECKS_NAME = "auto-checks.json"
NOMINATIONS_NAME = "nominations.json"
REVIEW_NAME = "REVIEW.md"


class FmrError(ValueError):
    """A finished-media review input or evidence problem."""


def is_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def sha256_hex(path: str | Path) -> str:
    from src.autoslice.evidence_identity import file_sha256

    return file_sha256(Path(path))


def file_sha(path: str | Path) -> str:
    return "sha256:" + sha256_hex(path)


def bytes_sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_sha(value: object) -> str:
    return bytes_sha(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def load_json(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise FmrError(f"FMR_JSON_MISSING: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FmrError(f"FMR_JSON_UNREADABLE: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FmrError(f"FMR_JSON_NOT_OBJECT: {path}")
    return value


def write_json(path: str | Path, document: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def valid_slug(value: object) -> bool:
    return isinstance(value, str) and _SLUG_RE.fullmatch(value) is not None


def valid_date(value: object) -> bool:
    return isinstance(value, str) and _DATE_RE.fullmatch(value) is not None


def candidate_id_for(recording_date: str, slug: str) -> str:
    if not valid_date(recording_date) or not valid_slug(slug):
        raise FmrError("FMR_MEMBER_IDENTITY_INVALID")
    return f"fm-{recording_date.replace('-', '')}-{slug}"


def evidence_ref(root: Path, path: Path) -> dict[str, str]:
    """Package-relative path plus content hash of an evidence file."""
    root = Path(root).resolve()
    resolved = Path(path).resolve()
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise FmrError(f"FMR_EVIDENCE_OUTSIDE_PACKAGE: {path}") from exc
    return {"path": rel.as_posix(), "sha256": file_sha(resolved)}


def srt_cues(text: str) -> list[dict[str, Any]]:
    """Minimal SRT parse: index, start_ms, end_ms, lines."""
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if len(lines) < 3 or " --> " not in lines[1]:
            continue
        start, end = (_ts(part) for part in lines[1].split(" --> ", 1))
        cues.append(
            {"index": int(lines[0].strip()), "start_ms": start, "end_ms": end,
             "lines": lines[2:], "text": " ".join(lines[2:])}
        )
    return cues


def _ts(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+):(\d\d):(\d\d)[,.](\d{3})\s*", value)
    if not match:
        raise FmrError(f"FMR_SRT_TIMESTAMP_INVALID: {value!r}")
    h, m, s, ms = (int(part) for part in match.groups())
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def strip_speaker_label(text: str) -> tuple[str, str]:
    match = re.match(r"\[([^\]]+)\]\s*(.*)", text)
    return (match.group(1), match.group(2)) if match else ("", text)
