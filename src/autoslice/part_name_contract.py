"""Single-P archives: the part name equals the archive's final title.

One definition for every create/edit path.  ``build_edit_payload`` writes the
rule; this module holds the comparison side (a part-name change to the final
title is the only allowed non-target difference) and the post-publish check
with its one normalising edit.
"""

from __future__ import annotations

import copy
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from src.autoslice.bilibili_member_api import ARCHIVE_EDIT, EDIT_CLONE_FIELDS, build_edit_payload

PART_NAME_NOT_TITLE = "PART_NAME_NOT_TITLE"


def single_part(creator: object) -> Mapping[str, Any] | None:
    videos = creator.get("videos") if isinstance(creator, Mapping) else None
    if isinstance(videos, list) and len(videos) == 1 and isinstance(videos[0], Mapping):
        return videos[0]
    return None


def part_name_is_title(creator: Mapping[str, Any], title: object = None) -> bool:
    """False only for a single-P Creator row whose part name is not the title."""

    part = single_part(creator)
    if part is None:
        return True
    expected = title if title is not None else (creator.get("metadata") or {}).get("title")
    return part.get("title") == expected


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


def aligned_creator(older: Mapping[str, Any], newer: Mapping[str, Any]) -> Mapping[str, Any]:
    """Creator surface of ``newer`` with the allowed part-name change folded."""

    return _align_creator(older, newer)


def _only_part_name_differs(
    member_data: Mapping[str, Any], payload: Mapping[str, Any], title: str
) -> str | None:
    archive = member_data.get("archive") or {}
    raw_video = member_data["videos"][0]
    expected_keys = {key for key in EDIT_CLONE_FIELDS if key in archive}
    expected_keys |= {"aid", "bvid", "videos", "csrf"}
    if set(payload) != expected_keys:
        return "edit payload carries unexpected fields"
    for key in EDIT_CLONE_FIELDS:
        if key in archive and payload[key] != archive[key]:
            return f"edit payload drifts in {key}"
    if payload["aid"] != archive.get("aid") or payload["bvid"] != archive.get("bvid"):
        return "edit payload identity drift"
    rows = payload["videos"]
    if (
        len(rows) != 1
        or rows[0].get("filename") != raw_video.get("filename")
        or rows[0].get("cid") != raw_video.get("cid")
        or rows[0].get("title") != title
    ):
        return "edit payload drifts outside the part name"
    return None


def verify_part_name(
    *,
    member_data: Mapping[str, Any],
    expected_title: object,
    http: Callable[..., Mapping[str, Any]],
    csrf: str | None,
    state: dict[str, Any],
    evidence: list[dict[str, Any]],
    now: Callable[[], str],
) -> list[str]:
    """Problems for the single-P part-name contract; edit at most once.

    ``state`` persists across the caller's polling loop (``edited``).  Multi-P
    archives are not checked.  A mismatch with a matching archive title is
    repaired by one ``build_edit_payload`` edit that changes only the part
    name; the readback on the next poll decides.
    """

    videos = member_data.get("videos")
    if not isinstance(videos, list) or not videos:
        return [f"{PART_NAME_NOT_TITLE}: Creator Center returned no part list"]
    if len(videos) != 1:
        return []
    actual = videos[0].get("title")
    if actual == expected_title:
        if state.get("edited"):
            evidence[:] = [row for row in evidence if row["event"] != "PART_NAME_READBACK"]
            evidence.append({"event": "PART_NAME_READBACK", "at": now(), "part_name_after": actual})
        return []
    head = f"{PART_NAME_NOT_TITLE}: part name {actual!r} != manifest title {expected_title!r}"
    if state.get("edited"):
        evidence[:] = [row for row in evidence if row["event"] != "PART_NAME_READBACK"]
        evidence.append({"event": "PART_NAME_READBACK", "at": now(), "part_name_after": actual})
        return [f"{head} after the one normalising edit"]
    archive = member_data.get("archive") or {}
    if csrf is None or not isinstance(expected_title, str) or archive.get("title") != expected_title:
        return [f"{head}; no normalising edit (no credential or archive title differs)"]
    payload = build_edit_payload(member_data, csrf=csrf)
    drift = _only_part_name_differs(member_data, payload, expected_title)
    if drift:
        return [f"{head}; no normalising edit ({drift})"]
    state["edited"] = True
    evidence.append(
        {"event": "PART_NAME_EDIT_INTENT", "at": now(), "part_name_before": actual,
         "part_name_target": expected_title}
    )
    try:
        response = http(f"{ARCHIVE_EDIT}?csrf={urllib.parse.quote(csrf)}", payload, True)
        outcome = f"code {response.get('code')}"
    except (OSError, ValueError) as exc:  # transport or undecodable reply: outcome unknown
        outcome = f"error {type(exc).__name__}"
    evidence.append({"event": "PART_NAME_EDIT_RESPONSE", "at": now(), "outcome": outcome})
    return [f"{head}; normalising edit submitted ({outcome}), awaiting readback"]


class PartNameCheck:
    """Per-verification state for ``verify_part_name`` (the one-edit budget)."""

    def __init__(
        self,
        *,
        http: Callable[..., Mapping[str, Any]],
        csrf: str | None,
        now: Callable[[], str],
        enabled: bool = True,
    ) -> None:
        self.http, self.csrf, self.now, self.enabled = http, csrf, now, enabled
        self.state: dict[str, Any] = {}
        self.evidence: list[dict[str, Any]] = []

    def check(
        self,
        member_data: Mapping[str, Any] | None,
        expected_title: object,
        *,
        result: dict[str, Any],
    ) -> list[str]:
        """Problems for this poll; ``None`` member data defers the check.

        The caller defers while any other verification problem exists, so the
        one normalising edit only happens on an otherwise verified archive.
        Evidence (edit intent/response/readback) is copied into ``result``.
        """

        if not self.enabled or member_data is None:
            return []
        problems = verify_part_name(
            member_data=member_data,
            expected_title=expected_title,
            http=self.http,
            csrf=self.csrf,
            state=self.state,
            evidence=self.evidence,
            now=self.now,
        )
        if self.evidence:
            result["part_name_evidence"] = copy.deepcopy(self.evidence)
        return problems
