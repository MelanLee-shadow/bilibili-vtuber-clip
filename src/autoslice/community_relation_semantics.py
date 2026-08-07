"""Reviewed semantic labels for discovered community relations.

This module deliberately owns only operator adjudication structure. Discovery,
evidence collection, and acceptance remain in the crawler; a reviewed row may
correct an owner or relation kind but cannot create or accept a mapping.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable, Collection, Mapping


def _compact(value: object) -> str:
    return "".join(str(value or "").split())


def semantic_diagnostics(
    surface: str,
    rows: list[dict[str, Any]],
    *,
    official_surfaces: Collection[str],
) -> dict[str, Any]:
    """Expose linguistic evidence to CPA without mechanically assigning a kind."""

    compact_surface = _compact(surface)
    escaped = re.escape(compact_surface)
    patterns = {
        "collective_identity": re.compile(
            rf"(?:成为|加入|召集|驱逐|全体|各位|所有|百万|资深|男|女|有){escaped}"
            rf"|{escaped}(?:们|粉丝|自用|的米|的舰长|集合|出来)"
        ),
        "activity_or_process": re.compile(
            rf"(?:让|允许|开始|停止|继续|讲|搞|进行|拒绝|禁止)(?:你们|大家|观众)?{escaped}"
            rf"|{escaped}(?:行为|一下|起来|成功|现场|大会)"
        ),
        "derived_group_form": re.compile(rf"{escaped}(?:民|党|人)"),
        "person_predicate": re.compile(
            rf"{escaped}(?:也?会|吃|喝|说|唱|哭|笑|急了|本人|今天|直播|下播|开播|睡觉|发出)"
            rf"|(?:吃|喝|唱|说|哭|笑|直播|下播|开播|急了|发出)[^，。！？!?]{{0,16}}(?:的)?{escaped}"
            rf"|(?:叫做|称为|都叫|昵称是){escaped}"
            rf"|{escaped}(?:这个)?(?:名字|昵称|称呼)"
        ),
    }
    official = tuple(
        value for value in (_compact(item) for item in official_surfaces) if len(value) >= 2
    )
    bvids: dict[str, set[str]] = {name: set() for name in patterns}
    same_field_official_anchor_bvids: set[str] = set()
    example_candidates: list[tuple[tuple[int, int, str, str], dict[str, Any]]] = []
    for row in rows:
        texts = [(field, str(row.get(field) or "")) for field in ("title", "description", "tags")]
        texts.extend(
            ("comment", str(comment.get("message") or ""))
            for comment in row.get("comments", [])
            if isinstance(comment, Mapping)
        )
        for field, text in texts:
            compact = _compact(text)
            if compact_surface not in compact:
                continue
            labels = [name for name, pattern in patterns.items() if pattern.search(compact)]
            if any(anchor in compact for anchor in official):
                labels.append("same_field_official_anchor")
                same_field_official_anchor_bvids.add(str(row["bvid"]))
            for label in labels:
                if label in bvids:
                    bvids[label].add(str(row["bvid"]))
            strong = any(label in patterns for label in labels)
            field_rank = {"title": 0, "comment": 1, "description": 2, "tags": 3}[field]
            example = {
                "bvid": str(row["bvid"]),
                "field": field,
                "text": text[:320],
                "markers": labels or ["exact_surface_occurrence"],
            }
            example_candidates.append(
                ((0 if strong else 1, field_rank, str(row["bvid"]), text), example)
            )
    examples = [item[1] for item in sorted(example_candidates)[:32]]
    return {
        **{f"{name}_bvids": sorted(values) for name, values in bvids.items()},
        "target_cooccurrence_bvids": sorted(same_field_official_anchor_bvids),
        "exact_surface_contexts": examples,
    }


def validate_reviewed_relations(
    rows: object,
    *,
    allowed_kinds: Collection[str],
    normalize_key: Callable[[str], str],
    validate_surface: Callable[[object], str],
) -> None:
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError("reviewed_relations are invalid")
    reviewed_keys: set[tuple[str, str]] = set()
    exclusive_owners: dict[str, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "entity_id",
            "surface",
            "relation_kind",
            "version",
            "reviewed_at",
            "note",
            "exclusive_owner",
        }:
            raise ValueError(f"reviewed relation {index} has invalid fields")
        entity_id = str(row["entity_id"])
        if not entity_id or len(entity_id) > 96:
            raise ValueError(f"reviewed relation {index} has invalid entity_id")
        surface = validate_surface(row["surface"])
        relation_kind = str(row["relation_kind"])
        if relation_kind not in allowed_kinds:
            raise ValueError(f"reviewed relation {index} has invalid kind")
        if not isinstance(row["version"], int) or int(row["version"]) <= 0:
            raise ValueError(f"reviewed relation {index} has invalid version")
        try:
            dt.date.fromisoformat(str(row["reviewed_at"]))
        except ValueError as exc:
            raise ValueError(f"reviewed relation {index} has invalid reviewed_at") from exc
        if not isinstance(row["note"], str) or not 1 <= len(row["note"]) <= 240:
            raise ValueError(f"reviewed relation {index} has invalid note")
        if not isinstance(row["exclusive_owner"], bool):
            raise ValueError(f"reviewed relation {index} has invalid exclusive_owner")
        normalized = normalize_key(surface)
        key = (entity_id, normalized)
        if key in reviewed_keys:
            raise ValueError(f"reviewed relation {index} is duplicated")
        reviewed_keys.add(key)
        if row["exclusive_owner"]:
            owner = exclusive_owners.setdefault(normalized, entity_id)
            if owner != entity_id:
                raise ValueError(f"reviewed relation {index} has conflicting exclusive owners")


def reviewed_relation_indexes(
    config: Mapping[str, Any], *, normalize_key: Callable[[str], str]
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, str]]:
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    exclusive: dict[str, str] = {}
    for raw in config.get("reviewed_relations", []):
        row = dict(raw)
        normalized = normalize_key(str(row["surface"]))
        exact[(str(row["entity_id"]), normalized)] = row
        if bool(row["exclusive_owner"]):
            exclusive[normalized] = str(row["entity_id"])
    return exact, exclusive
