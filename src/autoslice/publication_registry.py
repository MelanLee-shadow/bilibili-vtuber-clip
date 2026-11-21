"""候选↔BV 出版登记：新投稿副作用前的重复出版/搁置件闸口。

BV1PUBLIC000 事故：7/24 争议搁置件（当晚批次 上传许可:否）被
重产成 review_ready 后当新切片上传。state 的 review_ready 只描述产物就绪，
不携带「是否已出版 / 是否被人工搁置」——该事实必须由 committed registry
承载，并在任何新投稿副作用前查询。

- published 候选：内容已在某 BV 上，修复只允许 authorized_upload.py
 repair-* 的原 BV 修复链；
- hold_pending_review 候选：公开规则放行前禁止任何上传；
- released_for_upload 候选：保留历史 hold 与 公开规则放行证据，但不再阻断新投稿；
- registry 缺失或不可读时 fail-closed（宁可拒发也不重复出版）。

贪生怕死（auto_000000_0_0）：封面维护车道当时只认 published，
被 公开规则明令搁置的 hold 件照旧每个 tick 刷预算、出图（已烧 9 次尝试），而按
定义那些像素永远不会被上传。公开规则：贪生怕死不需要进行上传，就不需要封面
了。所以 cover 闸口按 ``_BLOCKING_STATUSES`` 成员判定：登记里凡是阻断上传
的状态，一律不再为它花图片额度。判据是每次调用现读 committed registry 的纯
函数，不在 state 里写任何 hold 标记——公开规则把该行改成 released_for_upload，
下一 tick 封面自然恢复，没有需要人工清理的终态。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.base_default import default_base
from src.autoslice.channel_profile import load_channel_profile as _load_channel_profile
from src.autoslice.publication_state_projection import (
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


def _candidate_ids(row: Mapping) -> set[str]:
    """One upload may have consumed several source candidates."""
    from .publication_content_coverage import validate_publication_content_coverage

    publication = row.get("publication_reconciliation") or {}
    covered = validate_publication_content_coverage(publication)
    if covered and (
        row.get("status") != "published"
        or any(row.get(key) != publication.get(key)
               for key in ("candidate_id", "recording_date", "bvid"))
    ):
        raise ValueError("PUBLICATION_CONTENT_COVERAGE_ROW_BINDING_MISMATCH")
    return {str(row.get("candidate_id") or ""), *covered}


def _runtime_registry_path() -> Path:
    return (
        Path(os.environ.get("AUTOSLICE_BASE", default_base()))
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
        _candidate_ids(row)
    selected_runtime = runtime_path
    if selected_runtime is None and path is None:
        selected_runtime = _runtime_registry_path()
    if selected_runtime is not None and selected_runtime.is_file():
        data = _merge_runtime_registry(data, selected_runtime)
    return data


