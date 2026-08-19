"""Exact-section read and one-shot episode-title repair helpers."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from src.autoslice.bilibili_member_api import BiliSession
from src.autoslice.same_bv_cover_reconciliation import (
    normalise_cover_url,
    snapshot_with_current_cover_identity,
)


def episode_rows(payload: object) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "episodes" and isinstance(value, list):
                rows.extend(dict(row) for row in value if isinstance(row, dict))
            else:
                rows.extend(episode_rows(value))
    elif isinstance(payload, list):
        for value in payload:
            rows.extend(episode_rows(value))
    return rows


def section_ids(payload: object) -> set[int]:
    ids: set[int] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"section_id", "sectionId"} and isinstance(value, int):
                ids.add(value)
            elif (
                key == "id"
                and isinstance(value, int)
                and (
                    "episodes" in payload
                    or "season_id" in payload
                    or "seasonId" in payload
                )
            ):
                ids.add(value)
            ids.update(section_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            ids.update(section_ids(value))
    return ids


def section_order_episode_ids(rows: Sequence[Mapping[str, Any]]) -> list[int]:
    """整节 episode id 的**线上顺序**，只在它自证是 1..n 时才交出。

    ``season/section/episode/edit`` 的 ``sorts`` 是整节顺序表（见
    ``bilibili_member_api.season_episode_edit`` 的契约注释），原样回传才等于
    “不改顺序”。所以这里要求返回列表里每行的 ``order`` 恰好等于它的 1 起下标：
    这既证明读回的就是规范顺序，也保证写回是可证的 no-op 重排。任何缺 id、
    重复 id、乱序或 recursive 收集到重复列表的情况一律 fail-closed。
    """

    ids: list[int] = []
    for index, row in enumerate(rows, start=1):
        row_id = episode_value(row, "id")
        row_order = episode_value(row, "order")
        if (
            not isinstance(row_id, int)
            or isinstance(row_id, bool)
            or row_id <= 0
            or not isinstance(row_order, int)
            or isinstance(row_order, bool)
            or row_order != index
        ):
            raise RuntimeError(
                "section title sync exact-section order is not a clean 1..n sequence"
            )
        ids.append(row_id)
    if not ids or len(set(ids)) != len(ids):
        raise RuntimeError("section title sync exact-section episode ids are not unique")
    return ids


def episode_value(row: Mapping[str, Any], key: str) -> object:
    if row.get(key) is not None:
        return row.get(key)
    for nested_key in ("archive", "arc"):
        nested = row.get(nested_key)
        if isinstance(nested, Mapping) and nested.get(key) is not None:
            return nested.get(key)
    return None


def section_title_only_pending(
    snapshot: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    new_video: Mapping[str, Any],
    cover_url: str,
) -> bool:
    """Recognize only the known post-swap stale episode-title shape."""

    current = snapshot_with_current_cover_identity(snapshot)
    before = snapshot_with_current_cover_identity(plan.get("before") or {})
    public = current.get("public") or {}
    section = current.get("section") or {}
    matches = section.get("matches") or []
    before_matches = (before.get("section") or {}).get("matches") or []
    if (
        public.get("available") is not True
        or section.get("available") is not True
        or len(matches) != 1
        or len(before_matches) != 1
    ):
        return False
    target_metadata = dict(plan.get("target_metadata") or {})
    target_metadata["cover"] = normalise_cover_url(cover_url)
    target_public = {
        "available": True,
        "bvid": plan["bvid"],
        "aid": ((before.get("creator") or {}).get("aid")),
        "cid": new_video["cid"],
        "state": 0,
        "metadata": {
            key: value for key, value in target_metadata.items() if key != "source"
        },
    }
    before_match = before_matches[0]
    target_match_without_title = {
        "bvid": before_match.get("bvid"),
        "aid": ((before.get("creator") or {}).get("aid")),
        "cid": new_video["cid"],
    }
    current_match = dict(matches[0])
    current_title = current_match.pop("title", None)
    return bool(
        public == target_public
        and current_match == target_match_without_title
        and isinstance(current_title, str)
        and current_title == before_match.get("title")
        and current_title != target_metadata.get("title")
    )


def sync_exact_section_episode_title(
    *,
    session: BiliSession,
    http: Callable[[str], Mapping[str, Any]],
    section_url: str,
    bvid: str,
    section_id: int,
    expected_current_title: str,
    target_title: str,
) -> Mapping[str, Any]:
    """Re-read all identities, then edit only the exact existing episode."""

    creator = session.archive_view(bvid)
    archive = creator.get("archive") or {}
    videos = creator.get("videos") or []
    if (
        not isinstance(archive, Mapping)
        or archive.get("bvid") != bvid
        or archive.get("title") != target_title
        or not isinstance(videos, list)
        or len(videos) != 1
    ):
        raise RuntimeError("section title sync Creator identity is not exact")
    aid = archive.get("aid")
    page_cids = [row.get("cid") for row in videos if isinstance(row, Mapping)]
    if (
        not isinstance(aid, int)
        or isinstance(aid, bool)
        or aid <= 0
        or len(page_cids) != 1
        or not isinstance(page_cids[0], int)
        or isinstance(page_cids[0], bool)
        or page_cids[0] <= 0
    ):
        raise RuntimeError("section title sync Creator AID/CID identity is invalid")
    payload = http(section_url.format(section_id=section_id))
    all_rows = episode_rows(payload)
    rows = [
        row
        for row in all_rows
        if episode_value(row, "bvid") == bvid
        or episode_value(row, "aid") == aid
    ]
    if (
        not isinstance(payload, Mapping)
        or payload.get("code") != 0
        or section_id not in section_ids(payload)
        or len(rows) != 1
        or episode_value(rows[0], "title") != expected_current_title
        or episode_value(rows[0], "aid") != aid
        or episode_value(rows[0], "cid") != page_cids[0]
    ):
        raise RuntimeError("section title sync exact-section identity drifted")
    row = rows[0]
    section_episode_ids = section_order_episode_ids(all_rows)

    def required_int(keys: Sequence[str]) -> int:
        for key in keys:
            value = episode_value(row, key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        raise RuntimeError(
            f"section title sync is missing integer identity {keys[0]}"
        )

    episode_id = required_int(("id", "episode_id", "episodeId"))
    order = required_int(("order",))
    if (
        episode_id not in section_episode_ids
        or section_episode_ids.index(episode_id) + 1 != order
    ):
        raise RuntimeError("section title sync episode order is not the live position")

    return session.season_episode_edit(
        episode_id=episode_id,
        title=target_title,
        aid=required_int(("aid",)),
        cid=required_int(("cid",)),
        season_id=required_int(("seasonId", "season_id")),
        section_id=required_int(("sectionId", "section_id")),
        order=order,
        section_episode_ids=section_episode_ids,
    )
