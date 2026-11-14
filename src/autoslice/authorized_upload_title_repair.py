"""The publication authority (which clip is which published video, and the proofs a revision of a published video
must replay) is the operating state and safety chain of one channel.  It is not part of this public snapshot;
this module keeps the names the pipeline imports so that everything before publishing runs unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


SCHEMA_VERSION = "same-bv-title-repair.v1"


class TitleRepairError(RuntimeError):
    """A title-only repair cannot be replayed."""


def _binding_from_receipt(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    binding = receipt.get("binding")
    if not isinstance(binding, Mapping):
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_BINDING_MISSING")
    return binding


def validate_title_repair_successor(reference: object, *, authority: Mapping[str, Any], candidate_id: str, title: str) -> None:
    raise TitleRepairError("revising a published video needs the channel's own publication authority, which this snapshot does not include")


def _normal_sha(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("_normal_sha: the channel's publication authority is not part of this snapshot")


def _read_json(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("_read_json: the channel's publication authority is not part of this snapshot")


def _required_text(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("_required_text: the channel's publication authority is not part of this snapshot")


def _stable_without_title(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("_stable_without_title: the channel's publication authority is not part of this snapshot")


def _surface_identity(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("_surface_identity: the channel's publication authority is not part of this snapshot")


def _validate_receipt(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("_validate_receipt: the channel's publication authority is not part of this snapshot")
