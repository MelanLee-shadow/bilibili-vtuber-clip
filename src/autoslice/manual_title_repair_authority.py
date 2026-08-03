"""Narrow, hash-bound authorization for one manual-title source-fact repair."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping


from src.autoslice.channel_profile import load_channel_profile as _load_channel_profile

ROOT = Path(__file__).resolve().parents[2]
_CHANNEL_PROFILE = _load_channel_profile(ROOT)
# 授权文件是部署本地一次性配置（人手写、当场消费），schema 与目录按 profile
# 派生；默认 profile 字节等价。
SCHEMA_VERSION = (
    f"{_CHANNEL_PROFILE.profile_id}-manual-title-repair-authority.v1"
)
AUTHORITY_ROOT = _CHANNEL_PROFILE.asset_directory("manual_title_repair_authorities")
_FIELDS = {
    "schema_version",
    "candidate_id",
    "original_title",
    "original_selection_hook",
    "source_fact_receipt_sha256",
    "final_title",
    "final_selection_hook",
    "user_authorization",
    "authority_sha256",
}


class ManualTitleRepairAuthorityError(ValueError):
    """The scoped manual-title authority is absent, malformed, or mismatched."""


def _canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _load_one(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManualTitleRepairAuthorityError(
            "MANUAL_TITLE_REPAIR_AUTHORITY_UNREADABLE"
        ) from exc
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise ManualTitleRepairAuthorityError("MANUAL_TITLE_REPAIR_AUTHORITY_SCHEMA_INVALID")
    authority = dict(value)
    if authority.get("schema_version") != SCHEMA_VERSION:
        raise ManualTitleRepairAuthorityError("MANUAL_TITLE_REPAIR_AUTHORITY_SCHEMA_INVALID")
    if not all(isinstance(authority.get(key), str) and authority[key].strip() for key in _FIELDS - {"user_authorization", "authority_sha256", "schema_version"}):
        raise ManualTitleRepairAuthorityError("MANUAL_TITLE_REPAIR_AUTHORITY_SCHEMA_INVALID")
    user_authorization = authority.get("user_authorization")
    if not isinstance(user_authorization, Mapping) or set(user_authorization) != {"quote", "timestamp"}:
        raise ManualTitleRepairAuthorityError("MANUAL_TITLE_REPAIR_AUTHORITY_SCHEMA_INVALID")
    if not all(isinstance(user_authorization.get(key), str) and user_authorization[key].strip() for key in ("quote", "timestamp")):
        raise ManualTitleRepairAuthorityError("MANUAL_TITLE_REPAIR_AUTHORITY_SCHEMA_INVALID")
    declared = authority.pop("authority_sha256")
    if declared != _canonical_sha256(authority):
        raise ManualTitleRepairAuthorityError("MANUAL_TITLE_REPAIR_AUTHORITY_HASH_MISMATCH")
    authority["authority_sha256"] = declared
    return authority


def load_manual_title_repair_authority(
    candidate_id: str,
    *,
    root: Path = ROOT,
) -> dict[str, object] | None:
    """Load exactly one candidate-scoped authority, never a wildcard grant."""

    authority_root = _CHANNEL_PROFILE.asset_directory(
        "manual_title_repair_authorities", repo_root=root
    )
    if not authority_root.is_dir():
        return None
    matches: list[dict[str, object]] = []
    for path in sorted(authority_root.glob("*.json")):
        authority = _load_one(path)
        if authority["candidate_id"] == candidate_id:
            matches.append(authority)
    if len(matches) > 1:
        raise ManualTitleRepairAuthorityError("MANUAL_TITLE_REPAIR_AUTHORITY_AMBIGUOUS")
    return matches[0] if matches else None


def validate_manual_title_repair_authority(
    authority: Mapping[str, object],
    *,
    candidate_id: str,
    original_title: str,
    original_selection_hook: str,
    source_fact_receipt_sha256: str,
    final_title: str,
    final_selection_hook: str,
) -> dict[str, object]:
    """Return a persisted consumption receipt only for the exact approved repair."""

    required = {
        "candidate_id": candidate_id,
        "original_title": original_title,
        "original_selection_hook": original_selection_hook,
        "source_fact_receipt_sha256": source_fact_receipt_sha256,
        "final_title": final_title,
        "final_selection_hook": final_selection_hook,
    }
    if any(authority.get(key) != value for key, value in required.items()):
        raise ManualTitleRepairAuthorityError("MANUAL_TITLE_REPAIR_AUTHORITY_BINDING_MISMATCH")
    return {
        "schema_version": "manual-title-repair-authority-consumption.v1",
        "status": "CONSUMED",
        "authority_sha256": authority["authority_sha256"],
        "candidate_id": candidate_id,
        "source_fact_receipt_sha256": source_fact_receipt_sha256,
        "original_title": original_title,
        "original_selection_hook": original_selection_hook,
        "final_title": final_title,
        "final_selection_hook": final_selection_hook,
        "user_authorization": dict(authority["user_authorization"]),
    }
