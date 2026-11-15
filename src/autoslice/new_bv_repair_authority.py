"""The same-BV authority of a revision (which published BV a successor package may replace), built from and replayed
against the first-publication receipt the public upload tool wrote: src/autoslice/lane_publish/authority.py."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.lane_publish.authority import SCHEMA_VERSION, build, replay_first
from src.autoslice.lane_publish.authority import AuthorityError as NewBvRepairAuthorityError
from src.autoslice.lane_publish.authority import validate as _validate

__all__ = ["SCHEMA_VERSION", "NewBvRepairAuthorityError", "build_new_bv_repair_authority", "replay_first_publication_manifest",
           "validate_new_bv_repair_authority"]


def build_new_bv_repair_authority(publication_path: Path, *, candidate_id: str, title: str, **_: Any) -> dict[str, Any]:
    return build(Path(publication_path), candidate_id=candidate_id, title=title)


def validate_new_bv_repair_authority(value: object, *, candidate_id: str, expected_final_title: str | None = None,
                                     replay_evidence: bool = True, **_: Any) -> dict[str, Any]:
    return _validate(value, candidate_id=candidate_id, expected_final_title=expected_final_title, replay_evidence=replay_evidence)


def replay_first_publication_manifest(authority: Mapping[str, Any], **_: Any) -> dict[str, Any]:
    return replay_first(authority)
