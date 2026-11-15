"""The same-BV authority of a revision (which published BV a successor package may replace), built from and replayed
against the first-publication receipt the public upload tool wrote: src/autoslice/lane_publish/authority.py."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.lane_publish.authority import SCHEMA_VERSION, build, replay_first
from src.autoslice.lane_publish.authority import AuthorityError as NewBvRepairAuthorityError
from src.autoslice.lane_publish.authority import validate as validate_new_bv_repair_authority

__all__ = ["SCHEMA_VERSION", "NewBvRepairAuthorityError", "build_new_bv_repair_authority", "replay_first_publication_manifest",
           "validate_new_bv_repair_authority"]


def build_new_bv_repair_authority(publication_path: Path, *, candidate_id: str, title: str, **_: Any) -> dict[str, Any]:
    return build(Path(publication_path), candidate_id=candidate_id, title=title)


def replay_first_publication_manifest(authority: Mapping[str, Any], **_: Any) -> dict[str, Any]:
    return replay_first(authority)
