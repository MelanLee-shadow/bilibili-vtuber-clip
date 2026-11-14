"""Read inline and shared-table canonical surfaces for channel profiles."""
from __future__ import annotations

import json
from pathlib import Path

from .channel_profile import (
    CanonicalSurfaceRule, ChannelProfileError, _mapping, _resolve_repo_path,
    _strict_keys, _string,
)


def parse_surface_spec(value: object, repo_root: Path) -> tuple[tuple[CanonicalSurfaceRule, ...], Path | None]:
    text = _mapping(value, label="text_normalization")
    keys = {"canonical_surfaces"}
    if "canonical_surfaces_table" in text:
        keys.add("canonical_surfaces_table")
    _strict_keys(text, label="text_normalization", required=keys)
    raw_rules = text.get("canonical_surfaces")
    if not isinstance(raw_rules, list):
        raise ChannelProfileError("text_normalization.canonical_surfaces must be a list")
    table_path = None
    if "canonical_surfaces_table" in text:
        if raw_rules:
            raise ChannelProfileError("table-backed canonical surfaces cannot also be defined in the profile")
        label = "text_normalization.canonical_surfaces_table"
        table_path = _resolve_repo_path(
            repo_root, _string(text["canonical_surfaces_table"], label=label), label=label)
    rules = []
    for index, raw in enumerate(raw_rules):
        label = f"text_normalization.canonical_surfaces[{index}]"
        item = _mapping(raw, label=label)
        _strict_keys(item, label=label, required={"surface", "canonical", "authority"})
        rules.append(CanonicalSurfaceRule(
            surface=_string(item.get("surface"), label=f"{label}.surface"),
            canonical=_string(item.get("canonical"), label=f"{label}.canonical"),
            authority=_string(item.get("authority"), label=f"{label}.authority")))
    if len({rule.surface for rule in rules}) != len(rules):
        raise ChannelProfileError("text_normalization.canonical_surfaces must not repeat a surface")
    return tuple(rules), table_path


def load_table_surface_rules(path: Path) -> tuple[CanonicalSurfaceRule, ...]:
    """Load ordered rules lazily; a missing or malformed table remains an error."""
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ChannelProfileError(f"cannot read canonical surface table {path}: {exc}") from exc
    if not isinstance(table, list):
        raise ChannelProfileError("canonical surface table must be a list")
    ordered: list[tuple[int, CanonicalSurfaceRule]] = []
    for raw_row in table:
        row = _mapping(raw_row, label="canonical surface table row")
        rules = row.get("profile_surface_rules", [])
        if not isinstance(rules, list):
            raise ChannelProfileError("profile_surface_rules must be a list")
        for raw in rules:
            item = _mapping(raw, label="profile surface rule")
            _strict_keys(item, label="profile surface rule", required={"order", "surface", "authority"})
            order = item["order"]
            if not isinstance(order, int) or isinstance(order, bool) or order < 0:
                raise ChannelProfileError("profile surface rule order must be a nonnegative integer")
            ordered.append((order, CanonicalSurfaceRule(
                surface=_string(item["surface"], label="profile surface rule surface"),
                canonical=_string(row.get("canonical"), label="profile surface rule canonical"),
                authority=_string(item["authority"], label="profile surface rule authority"))))
    if len({index for index, _ in ordered}) != len(ordered):
        raise ChannelProfileError("profile surface rule order must be unique")
    rules_out = tuple(rule for _, rule in sorted(ordered, key=lambda pair: pair[0]))
    if len({rule.surface for rule in rules_out}) != len(rules_out):
        raise ChannelProfileError("profile surface rules must not repeat a surface")
    return rules_out
