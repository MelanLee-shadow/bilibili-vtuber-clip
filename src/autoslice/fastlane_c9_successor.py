"""OSS stub: private C9 successor authority is not redistributed."""
from __future__ import annotations

from pathlib import Path
from typing import Any


C9_PRIVATE_SCHEMA = "fastlane-c9-private-successor-review.v1"


def audit_c9_successor(
    root: Path, manifest: dict[str, Any]
) -> list[dict[str, str]] | None:
    """Keep the public auditor import while failing closed on private C9 data."""
    del root
    if not isinstance(manifest, dict) or manifest.get("schema_version") != C9_PRIVATE_SCHEMA:
        return None
    return [{"code": "C9_PRIVATE_AUTHORITY_UNAVAILABLE", "severity": "BLOCK"}]
