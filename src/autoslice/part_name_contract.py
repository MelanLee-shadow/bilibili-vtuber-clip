"""Single-P archives: the part name equals the archive's final title.

One definition for every create/edit path.  ``build_edit_payload`` writes the
rule; this module holds the comparison side (a part-name change to the final
title is the only allowed non-target difference) and the post-publish check
with its one normalising edit.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any


def single_part(creator: object) -> Mapping[str, Any] | None:
    videos = creator.get("videos") if isinstance(creator, Mapping) else None
    if isinstance(videos, list) and len(videos) == 1 and isinstance(videos[0], Mapping):
        return videos[0]
    return None


def _align_creator(older: Mapping[str, Any], newer: Mapping[str, Any]) -> Mapping[str, Any]:
    old_part, new_part = single_part(older), single_part(newer)
    if old_part is None or new_part is None or old_part.get("title") == new_part.get("title"):
        return newer
    if new_part.get("title") != (newer.get("metadata") or {}).get("title"):
        return newer
    aligned = copy.deepcopy(dict(newer))
    aligned["videos"][0]["title"] = old_part.get("title")
    return aligned


def same_stable(
    older: Mapping[str, Any],
    newer: Mapping[str, Any],
    stable: Callable[[Mapping[str, Any]], Any],
) -> bool:
    """``stable(older) == stable(newer)``, tolerating exactly one difference.

    A single-P part name may differ when the newer snapshot's part name is its
    own Creator title (old value -> final title).  Any other change, including
    a different cid/filename or a part name that is not the title, still
    compares unequal.
    """

    old_creator, new_creator = older.get("creator"), newer.get("creator")
    if isinstance(old_creator, Mapping) and isinstance(new_creator, Mapping):
        aligned = _align_creator(old_creator, new_creator)
        if aligned is not new_creator:
            newer = {**newer, "creator": aligned}
    return stable(older) == stable(newer)


