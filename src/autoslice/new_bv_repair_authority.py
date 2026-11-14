"""The publication authority (which clip is which published video, and the proofs a revision of a published video
must replay) is the operating state and safety chain of one channel.  It is not part of this public snapshot;
this module keeps the names the pipeline imports so that everything before publishing runs unchanged.
"""

from __future__ import annotations

from typing import Any


class PublicationAuthorityUnavailable(RuntimeError):
    """The channel's publication authority is not part of this snapshot."""


def build_new_bv_repair_authority(*args: Any, **kwargs: Any) -> dict[str, Any]:
    raise PublicationAuthorityUnavailable("revising a published video needs the channel's own publication authority")


def replay_first_publication_manifest(*args: Any, **kwargs: Any) -> dict[str, Any]:
    raise PublicationAuthorityUnavailable("revising a published video needs the channel's own publication authority")


class NewBvRepairAuthorityError(RuntimeError):
    """The channel's publication authority is not part of this snapshot."""


def validate_new_bv_repair_authority(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("validate_new_bv_repair_authority: the channel's publication authority is not part of this snapshot")
