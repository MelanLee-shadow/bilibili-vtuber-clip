"""候选↔BV 出版登记：新投稿副作用前的重复出版/搁置件闸口。

BV1ec3A6bEWF 事故：7/24 争议搁置件（当晚批次 上传许可:否）被
重产成 review_ready 后当新切片上传。state 的 review_ready 只描述产物就绪，
不携带「是否已出版 / 是否被人工搁置」——该事实必须由 committed registry
承载，并在任何新投稿副作用前查询。

- published 候选：内容已在某 BV 上，修复只允许 authorized_upload.py
  repair-* 的原 BV 修复链；
- hold_pending_review 候选：维护者 放行前禁止任何上传；
- released_for_upload 候选：保留历史 hold 与 维护者 放行证据，但不再阻断新投稿；
- registry 缺失或不可读时 fail-closed（宁可拒发也不重复出版）。

贪生怕死（auto_223750_913_1322）：封面维护车道当时只认 published，
被 维护者 明令搁置的 hold 件照旧每个 tick 刷预算、出图（已烧 9 次尝试），而按
定义那些像素永远不会被上传。维护者 逐字「贪生怕死不需要进行上传，就不需要封面
了」。所以 cover 闸口按 ``_BLOCKING_STATUSES`` 成员判定：登记里凡是阻断上传
的状态，一律不再为它花图片额度。判据是每次调用现读 committed registry 的纯
函数，不在 state 里写任何 hold 标记——维护者 把该行改成 released_for_upload，
下一 tick 封面自然恢复，没有需要人工清理的终态。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from src.autoslice import fastlane_c2_authorized_upload as c2_upload
from src.autoslice.channel_profile import load_channel_profile as _load_channel_profile
from src.autoslice.publication_reconciliation import (
    RUNTIME_REGISTRY_SCHEMA,
    validate_runtime_registry_entry,
)

REGISTRY_SCHEMA = "publication-registry.v1"
# 出版登记是上传唯一授权门；路径按 profile manifest 派生（默认 profile 字节
# 等价），换频道即各用各的登记台账。

DEFAULT_REGISTRY_PATH = _load_channel_profile(
    Path(__file__).resolve().parent.parent.parent
).asset_file("publication_registry")

_VALID_STATUSES = {
    "published",
    "hold_pending_review",
    "released_for_upload",
}
_BLOCKING_STATUSES = {"published", "hold_pending_review"}


def _runtime_registry_path() -> Path:
    return (
        Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
        / "state"
        / "publication_registry.runtime.v1.json"
    )


def _merge_runtime_registry(registry: dict, runtime_path: Path) -> dict:
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    if (
        not isinstance(runtime, dict)
        or runtime.get("schema_version") != RUNTIME_REGISTRY_SCHEMA
        or not isinstance(runtime.get("entries"), list)
    ):
        raise ValueError("PUBLICATION_RUNTIME_REGISTRY_INVALID")
    entries = registry["entries"]
    by_key: dict[tuple[str, str], dict] = {}
    for row in entries:
        key = (
            str(row.get("candidate_id") or ""),
            str(row.get("recording_date") or ""),
        )
        if key in by_key:
            raise ValueError("PUBLICATION_REGISTRY_DUPLICATE_CANDIDATE_DATE")
        by_key[key] = row
    for raw in runtime["entries"]:
        try:
            row = validate_runtime_registry_entry(raw)
        except ValueError as exc:
            raise ValueError("PUBLICATION_RUNTIME_REGISTRY_INVALID") from exc
        key = (str(row["candidate_id"]), str(row["recording_date"]))
        current = by_key.get(key)
        if current is not None:
            if (
                current.get("status") == "published"
                and current.get("bvid") != row.get("bvid")
            ):
                raise ValueError("PUBLICATION_RUNTIME_REGISTRY_BVID_CONFLICT")
            current["status"] = "published"
            current["bvid"] = row["bvid"]
            current["publication_reconciliation"] = dict(
                row["publication_reconciliation"]
            )
        else:
            current = {
                "candidate_id": row["candidate_id"],
                "recording_date": row["recording_date"],
                "status": "published",
                "bvid": row["bvid"],
                "publication_reconciliation": dict(
                    row["publication_reconciliation"]
                ),
            }
            entries.append(current)
            by_key[key] = current
    return registry


def load_publication_registry(
    path: Path | None = None,
    *,
    runtime_path: Path | None = None,
) -> dict:
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
        if status not in _VALID_STATUSES:
            raise ValueError("PUBLICATION_REGISTRY_ROW_STATUS_INVALID")
        if status == "published" and not str(row.get("bvid") or "").strip():
            raise ValueError("PUBLICATION_REGISTRY_ROW_BVID_MISSING")
    selected_runtime = runtime_path
    if selected_runtime is None and path is None:
        selected_runtime = _runtime_registry_path()
    if selected_runtime is not None and selected_runtime.is_file():
        data = _merge_runtime_registry(data, selected_runtime)
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
                " — repairs must use the authorized same-BV repair lane; "
                "new uploads are forbidden"
            )
        if status == "hold_pending_review":
            return (
                f"candidate {cid} is held pending 维护者's review"
                f" ({row.get('note') or 'no note'}) — uploads forbidden until cleared"
            )
    return None


def cover_maintenance_block_reason(
    candidate_id: str,
    *,
    recording_date: str | None = None,
    registry: Mapping | None = None,
    registry_path: Path | None = None,
) -> str | None:
    """Refuse generic cover maintenance for any upload-blocked candidate.

    Cover-policy fingerprints intentionally invalidate old evidence, but that
    must never turn the unattended maintenance loop into an implicit same-BV
    repair lane. Published pixels may change only through the explicit
    authorized repair workflow. Registry read failures also stop maintenance:
    spending image quota is not safe while publication identity is unknown.

    ``hold_pending_review`` is refused for a different reason with the same
    conclusion: a held candidate cannot be uploaded at all, so every image
    request spent on its cover is pure waste. The membership test is
    ``_BLOCKING_STATUSES`` itself so a future blocking status cannot silently
    keep burning image quota — an unknown blocking status falls back to a
    generic refusal rather than to spending.

    This is a *cover* gate only; it never widens the upload gate, and it
    persists nothing — flipping the row to ``released_for_upload`` restores
    normal cover maintenance on the next call.
    """

    cid = str(candidate_id or "").strip()
    if not cid:
        return None
    if registry is None:
        try:
            registry = load_publication_registry(registry_path)
        except (OSError, ValueError) as exc:
            return (
                f"publication registry unreadable ({exc}); refusing generic "
                "cover maintenance fail-closed"
            )
    for row in registry.get("entries", []):
        if str(row.get("candidate_id") or "") != cid:
            continue
        row_date = str(row.get("recording_date") or "")
        if recording_date and row_date and row_date != recording_date:
            continue
        status = row.get("status")
        if status not in _BLOCKING_STATUSES:
            continue
        if status == "published":
            return (
                f"candidate {cid} is already published as {row.get('bvid')}; "
                "generic cover maintenance is forbidden and any pixel change "
                "must use the authorized same-BV repair lane"
            )
        if status == "hold_pending_review":
            return (
                f"candidate {cid} is held pending 维护者's review"
                f" ({row.get('note') or 'no note'}) — it cannot be uploaded, "
                "so no cover is generated or repaired until the registry "
                "releases it"
            )
        return (
            f"candidate {cid} is upload-blocked by the publication registry "
            f"({status}); no image quota is spent on its cover"
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
    # Verified Song delivery records deliberately have no Talk StoryContract.
    # Their outer delivery candidate is the publication identity; require the
    # paired selector candidate as well so an unrelated legacy Talk record
    # cannot accidentally opt into this lane.
    if not candidate_id:
        delivery_candidate = record.get("delivery_candidate_id")
        source_candidate = record.get("source_candidate_id")
        if (
            isinstance(delivery_candidate, str)
            and delivery_candidate.strip()
            and isinstance(source_candidate, str)
            and source_candidate.strip()
        ):
            candidate_id = delivery_candidate.strip()
    # Legacy C2 has neither generic candidate field; let its exact resolver
    # validate the attested package before consulting the publication registry.
    if not candidate_id and "delivery_candidate_id" not in record:
        candidate_id = c2_upload.candidate_id_from_record(
            Path(str(attestation.get("package_root") or "")), record
        )
    if not candidate_id:
        return None
    match = re.search(
        r"/out/(\d{4}-\d{2}-\d{2})/", str(attestation.get("package_root") or "") + "/"
    )
    return upload_block_reason(
        candidate_id, recording_date=match.group(1) if match else None
    )
