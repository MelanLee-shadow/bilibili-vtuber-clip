"""Strict, tags-only live-metadata preservation for same-BV repair plans."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


SCHEMA = "same-bv-repair-metadata-preservation.v1"


class TagPreservationError(ValueError):
    """The narrow tags-only preservation contract could not be replayed."""


def normalise_tags(value: object) -> list[str]:
    # API ordering is nonsemantic; exact record-bound order uses _manifest_tags.
    if isinstance(value, str):
        rows = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        rows = [str(part).strip() for part in value if str(part).strip()]
    else:
        rows = []
    return sorted(rows)


def manifest_target(manifest: Mapping[str, Any]) -> dict[str, Any]:
    policy = manifest.get("publish_policy") or {}
    return {
        "title": manifest.get("title"), "desc": manifest.get("description"),
        "tags": normalise_tags(manifest.get("tags")), "tid": policy.get("tid"),
        "copyright": policy.get("copyright"), "source": policy.get("source"), "cover": None,
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _normalise_live_tags(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(tag, str) or not tag or tag != tag.strip() for tag in value
    ):
        raise TagPreservationError(f"{label} tags are empty or invalid")
    normalised = sorted(value)
    if value != normalised or len(set(value)) != len(value):
        raise TagPreservationError(f"{label} tags are non-canonical or duplicated")
    return normalised


def _manifest_tags(value: object) -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(tag, str) or not tag or tag != tag.strip() for tag in value
    ):
        raise TagPreservationError("manifest tags are empty or invalid")
    if len(set(value)) != len(value):
        raise TagPreservationError("manifest tags are duplicated")
    return list(value)


def receipt(*, manifest_tags: object, before: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze an exact live Creator/public tag set and manifest input order."""

    creator = before.get("creator") or {}
    public = before.get("public") or {}
    creator_tags = _normalise_live_tags(
        (creator.get("metadata") or {}).get("tags"), label="Creator"
    )
    public_tags = _normalise_live_tags(
        (public.get("metadata") or {}).get("tags"), label="public"
    )
    if creator_tags != public_tags:
        raise TagPreservationError("Creator and public tags differ during preservation planning")
    original = _manifest_tags(manifest_tags)
    return {
        "schema_version": SCHEMA,
        "field": "tags",
        "manifest_original_tags": original,
        "manifest_tags_sha256": "sha256:" + hashlib.sha256(_canonical_json(original)).hexdigest(),
        "preserved_live_tags": creator_tags,
        "creator_tags_sha256": "sha256:" + hashlib.sha256(_canonical_json(creator_tags)).hexdigest(),
        "public_tags_sha256": "sha256:" + hashlib.sha256(_canonical_json(public_tags)).hexdigest(),
    }


def target(
    *,
    manifest_tags: object,
    before: Mapping[str, Any],
    preservation: object,
    default_target: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild the target, allowing only a canonical preserved tags override."""

    result = dict(default_target)
    if preservation is None:
        return result
    expected = receipt(manifest_tags=manifest_tags, before=before)
    if preservation != expected:
        raise TagPreservationError("repair tags preservation receipt is not canonical")
    result["tags"] = expected["preserved_live_tags"]
    return result
