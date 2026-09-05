"""Fail-closed C2 authority resolver for the ordinary upload manifest gate.

The uploader normally requires a StoryContract for Talk packages.  C2 is the
one reviewed legacy release whose sealed bridge is the authority instead.  This
module recognizes no shape other than that exact bridge and never constructs or
normalizes a record; callers only receive C2's candidate id after a full bridge
replay succeeds.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from . import fastlane_c2_release_bridge as bridge


_RECORD_SCHEMA = "lidousha-c2-release-record.v1"
_SCOPE = "C2_NAMED_FASTLANE_NEW_BV_ONLY"
_RECORD_KEYS = frozenset(
    {
        "artifact_hashes",
        "c2_tag_generation_receipt",
        "candidate_id",
        "legacy_execution_contract",
        "publish_staging",
        "recording_date",
        "schema_version",
        "upload_allowed",
        "upload_tags",
    }
)


def _safe_root(root: Path) -> Path | None:
    try:
        root = Path(root).absolute()
        if root.is_symlink() or not root.is_dir():
            return None
    except (OSError, TypeError):
        return None
    return root


def _read_regular_object(path: Path) -> dict[str, object] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _is_exact_c2_record(record: Mapping[str, object]) -> bool:
    return (
        set(record) == _RECORD_KEYS
        and record.get("schema_version") == _RECORD_SCHEMA
        and record.get("candidate_id") == bridge.CID
        and record.get("recording_date") == bridge.DATE
        and record.get("upload_allowed") is False
        and record.get("publish_staging") == {"title": bridge.TITLE}
        and "story_contract" not in record
    )


def verified_c2_release_candidate_id(
    root: Path, record: Mapping[str, object]
) -> str | None:
    """Return C2's candidate only for its current sealed package at ``root``."""
    if not isinstance(record, Mapping) or not _is_exact_c2_record(record):
        return None
    root = _safe_root(root)
    if root is None:
        return None
    root_record = _read_regular_object(root / bridge.RECORD_NAME)
    review = _read_regular_object(root / "review_manifest.json")
    if root_record != dict(record) or review is None:
        return None
    if (
        not bridge.is_fastlane_c2_release_manifest(review)
        or review.get("title") != bridge.TITLE
        or review.get("scope") != _SCOPE
    ):
        return None
    try:
        problems = bridge.audit_fastlane_c2_release_package(root)
    except Exception:
        return None
    return bridge.CID if problems == [] else None


def candidate_id_from_record(root: Path, record: Mapping[str, object]) -> str:
    """Keep generic candidate resolution unchanged, then try exact C2 only."""
    if not isinstance(record, Mapping):
        return ""
    story_contract = record.get("story_contract")
    story_contract = story_contract if isinstance(story_contract, Mapping) else {}
    candidate_id = str(
        story_contract.get("candidate_id") or record.get("delivery_candidate_id") or ""
    )
    return candidate_id or verified_c2_release_candidate_id(root, record) or ""
