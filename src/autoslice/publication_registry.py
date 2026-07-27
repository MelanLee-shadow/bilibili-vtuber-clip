"""候选↔BV 出版登记：新投稿副作用前的重复出版/搁置件闸口。

2026-07-27 BV1ec3A6bEWF 事故：7/24 争议搁置件（当晚批次 上传许可:否）被
重产成 review_ready 后当新切片上传。state 的 review_ready 只描述产物就绪，
不携带「是否已出版 / 是否被人工搁置」——该事实必须由 committed registry
承载，并在任何新投稿副作用前查询。

- published 候选：内容已在某 BV 上，修复一律 edit-replace 原 BV；
- hold_pending_review 候选：Ivan 放行前禁止任何上传；
- registry 缺失或不可读时 fail-closed（宁可拒发也不重复出版）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

REGISTRY_SCHEMA = "publication-registry.v1"
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "assets"
    / "lidousha"
    / "publication_registry.v1.json"
)

_BLOCKING_STATUSES = {"published", "hold_pending_review"}


def load_publication_registry(path: Path | None = None) -> dict:
    """Load and structurally validate the registry; raise on malformation."""

    registry_path = path or DEFAULT_REGISTRY_PATH
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != REGISTRY_SCHEMA:
        raise ValueError("PUBLICATION_REGISTRY_SCHEMA_INVALID")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("PUBLICATION_REGISTRY_ENTRIES_INVALID")
    for row in entries:
        if not isinstance(row, Mapping):
            raise ValueError("PUBLICATION_REGISTRY_ROW_INVALID")
        if not str(row.get("candidate_id") or "").strip():
            raise ValueError("PUBLICATION_REGISTRY_ROW_CANDIDATE_MISSING")
        status = row.get("status")
        if status not in _BLOCKING_STATUSES:
            raise ValueError("PUBLICATION_REGISTRY_ROW_STATUS_INVALID")
        if status == "published" and not str(row.get("bvid") or "").strip():
            raise ValueError("PUBLICATION_REGISTRY_ROW_BVID_MISSING")
    return data


def upload_block_reason(
    candidate_id: str,
    *,
    recording_date: str | None = None,
    registry: Mapping | None = None,
    registry_path: Path | None = None,
) -> str | None:
    """Return a human-readable refusal for a NEW upload, or None to allow.

    Fail-closed: an unreadable/malformed registry blocks the upload rather
    than silently allowing a duplicate publication.
    """

    cid = str(candidate_id or "").strip()
    if not cid:
        return None
    if registry is None:
        try:
            registry = load_publication_registry(registry_path)
        except (OSError, ValueError) as exc:
            return f"publication registry unreadable ({exc}); refusing new upload fail-closed"
    for row in registry.get("entries", []):
        if str(row.get("candidate_id") or "") != cid:
            continue
        row_date = str(row.get("recording_date") or "")
        if recording_date and row_date and row_date != recording_date:
            continue
        status = row.get("status")
        if status == "published":
            return (
                f"candidate {cid} is already published as {row.get('bvid')}"
                " — repairs must edit-replace that BV, new uploads are forbidden"
            )
        if status == "hold_pending_review":
            return (
                f"candidate {cid} is held pending Ivan's review"
                f" ({row.get('note') or 'no note'}) — uploads forbidden until cleared"
            )
    return None

def manifest_upload_block_reason(manifest: Mapping) -> str | None:
    """New-upload gate keyed off a v3 manifest's own package attestation.

    Reads the attested record.json for story_contract.candidate_id and the
    package_root path for the recording date. Manifests without an attested
    record or candidate_id (curated legacy lanes) stay governed by the
    video-sha ledger guard alone.
    """

    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, Mapping):
        return None
    record_entry = attestation.get("record")
    if not isinstance(record_entry, Mapping):
        return None
    record_path = Path(str(record_entry.get("path") or ""))
    if not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (
            "publication registry gate cannot read the attested record.json;"
            " refusing new upload fail-closed"
        )
    if not isinstance(record, dict):
        return None
    story = record.get("story_contract")
    candidate_id = (
        str(story.get("candidate_id") or "") if isinstance(story, Mapping) else ""
    )
    if not candidate_id:
        return None
    match = re.search(
        r"/out/(\d{4}-\d{2}-\d{2})/", str(attestation.get("package_root") or "") + "/"
    )
    return upload_block_reason(
        candidate_id, recording_date=match.group(1) if match else None
    )
