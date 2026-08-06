"""Deterministic query planning for community-name discovery."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence, TypeVar


_CJK_RX = re.compile(r"[\u3400-\u9fff]")
_T = TypeVar("_T")


def bounded_mappings(
    rows: Sequence[Mapping[str, Any]], *, transient_limit: int = 2_048
) -> list[Mapping[str, Any]]:
    """Keep durable decisions and cap unattended candidate-state growth."""

    durable = [row for row in rows if row.get("status") in {"accepted", "conflict"}]
    transient = [row for row in rows if row.get("status") not in {"accepted", "conflict"}]
    transient.sort(
        key=lambda row: (
            int(row.get("score", 0)),
            str(row.get("last_seen_at", "")),
            str(row.get("mapping_key", "")),
        ),
        reverse=True,
    )
    return durable + transient[:transient_limit]


def advance_query_cursor(
    *, order: int, order_count: int, page: int, page_count: int, query: int, query_count: int
) -> dict[str, int]:
    """Advance order first, then page, then query without extra requests."""

    next_order = (order + 1) % order_count
    completed_page = next_order == 0
    next_page = page % page_count + 1 if completed_page else page
    next_query = (query + 1) % query_count if completed_page and page == page_count else query
    return {
        "search_order_cursor": next_order,
        "search_page_cursor": next_page,
        "query_variant_cursor": next_query,
    }


def query_variants(member: Mapping[str, Any], suffixes: Sequence[object]) -> list[str]:
    """Prefer a short official CJK surface, then widen to canonical queries."""

    canonical = str(member["canonical"]).strip()
    official = {
        str(value).strip()
        for value in member.get("official_surfaces", [])
        if str(value).strip()
    }
    short_cjk = sorted(
        (
            value
            for value in official
            if _CJK_RX.search(value) and 2 <= len(value) < len(canonical)
        ),
        key=lambda value: (len(value), value.casefold()),
    )
    planned = [*short_cjk[:1], canonical]
    planned.extend(canonical + str(suffix) for suffix in suffixes if str(suffix))
    return list(dict.fromkeys(planned))


def evenly_sample(rows: Sequence[_T], limit: int) -> list[_T]:
    """Keep a bounded prompt while observing the whole rotated result page."""

    if limit <= 0:
        return []
    if limit == 1:
        return list(rows[:1])
    if len(rows) <= limit:
        return list(rows)
    indexes = [round(index * (len(rows) - 1) / (limit - 1)) for index in range(limit)]
    return [rows[index] for index in indexes]


def title_complete_prompt_rows(
    rows: Sequence[Mapping[str, Any]], detail_limit: int
) -> list[dict[str, Any]]:
    """Expose every bounded title and sample larger description/tag fields."""

    detailed_bvids = {str(row["bvid"]) for row in evenly_sample(rows, detail_limit)}
    result: list[dict[str, Any]] = []
    for row in rows:
        prompt_row = {
            key: row[key]
            for key in ("bvid", "uploader_mid", "published_at", "title")
        }
        if str(row["bvid"]) in detailed_bvids:
            prompt_row.update(description=row["description"], tags=row["tags"])
        result.append(prompt_row)
    return result
