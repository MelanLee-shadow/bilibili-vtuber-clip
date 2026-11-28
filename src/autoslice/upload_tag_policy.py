"""Strict loader for profile-owned upload-tag knowledge and prompt policy."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.autoslice.channel_profile import ChannelProfile, load_channel_profile


UPLOAD_TAG_POLICY_SCHEMA = "vtuber-slice.upload-tag-policy.v2"
REPO_ROOT = Path(__file__).resolve().parents[2]
# Circle-wide term rules every channel's policy gets after its own (the channel's request is the genre's way).
CIRCLE_TERMS = REPO_ROOT / "assets" / "manual_lane" / "upload_tag_circle_terms.json"


class UploadTagPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class TermRule:
    """One deterministic mention-to-search-tag rule."""

    name: str
    patterns: tuple[str, ...]
    tags: tuple[str, ...]
    min_hits: int = 1
    note: str = ""


@dataclass(frozen=True)
class UploadTagPolicy:
    base_tags: tuple[str, ...]
    max_tags_default: int
    max_dynamic_tags: int
    max_tag_chars: int
    max_dynamic_tag_chars: int
    term_rules: tuple[TermRule, ...]
    important_content_ips: tuple[TermRule, ...]
    known_proper_surfaces_extra: tuple[str, ...]
    theme_allowed: frozenset[str]
    banned_content_tags: frozenset[str]
    content_prompt_template: str
    source_path: Path


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise UploadTagPolicyError(f"{label} must be an object")
    return value


def _strict_keys(
    value: Mapping[str, object], *, label: str, required: set[str]
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required)
    if missing or unknown:
        raise UploadTagPolicyError(
            f"{label} keys mismatch: missing={missing}, unknown={unknown}"
        )


def _string(value: object, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise UploadTagPolicyError(f"{label} must be {qualifier}")
    return value if allow_empty else value.strip()


def _string_list(
    value: object, *, label: str, allow_empty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        qualifier = "a string list" if allow_empty else "a non-empty string list"
        raise UploadTagPolicyError(f"{label} must be {qualifier}")
    result = tuple(_string(item, label=f"{label} entry") for item in value)
    if len(result) != len(set(result)):
        raise UploadTagPolicyError(f"{label} must not contain duplicates")
    return result


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UploadTagPolicyError(f"{label} must be a positive integer")
    return value


def load_upload_tag_policy(path: Path) -> UploadTagPolicy:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UploadTagPolicyError(f"cannot read upload tag policy {path}: {exc}") from exc
    root = _mapping(payload, label="upload tag policy")
    expected = {
        "schema_version",
        "base_tags",
        "max_tags_default",
        "max_dynamic_tags",
        "max_tag_chars",
        "max_dynamic_tag_chars",
        "term_rules",
        "important_content_ips",
        "known_proper_surfaces_extra",
        "theme_allowed",
        "banned_content_tags",
        "content_prompt_template",
    }
    _strict_keys(root, label="upload tag policy", required=expected)
    if root.get("schema_version") != UPLOAD_TAG_POLICY_SCHEMA:
        raise UploadTagPolicyError(
            f"unsupported upload tag policy schema {root.get('schema_version')!r}"
        )

    rules = _parse_term_rules(root.get("term_rules"), label="term_rules")
    if CIRCLE_TERMS.is_file():
        circle = _mapping(json.loads(CIRCLE_TERMS.read_text(encoding="utf-8")), label="circle term rules")
        _strict_keys(circle, label="circle term rules", required={"term_rules"})
        rules = (*rules, *_parse_term_rules(circle.get("term_rules"), label="circle term_rules"))
    important_content_ips = _parse_term_rules(
        root.get("important_content_ips"), label="important_content_ips"
    )
    for rule in important_content_ips:
        if len(rule.tags) != 1:
            raise UploadTagPolicyError(
                f"important_content_ips rule {rule.name!r} must emit one canonical tag"
            )
    names = [rule.name for rule in (*rules, *important_content_ips)]
    if len(names) != len(set(names)):
        raise UploadTagPolicyError(
            "term_rules and important_content_ips must not repeat rule names"
        )

    base_tags = _string_list(root.get("base_tags"), label="base_tags")
    max_tags_default = _positive_int(
        root.get("max_tags_default"), label="max_tags_default"
    )
    max_dynamic_tags = _positive_int(
        root.get("max_dynamic_tags"), label="max_dynamic_tags"
    )
    if max_tags_default < len(base_tags):
        raise UploadTagPolicyError("max_tags_default is smaller than base_tags")
    if max_tags_default > len(base_tags) + max_dynamic_tags:
        raise UploadTagPolicyError(
            "max_tags_default exceeds the base plus dynamic tag slot budget"
        )
    max_tag_chars = _positive_int(root.get("max_tag_chars"), label="max_tag_chars")
    max_dynamic_tag_chars = _positive_int(
        root.get("max_dynamic_tag_chars"), label="max_dynamic_tag_chars"
    )
    prompt = _string(
        root.get("content_prompt_template"),
        label="content_prompt_template",
        allow_empty=True,
    )
    if not prompt.strip():
        raise UploadTagPolicyError("content_prompt_template must be a non-empty string")
    # Resolve policy numbers without consuming escaped JSON or input slots.
    for name, value in {
        "max_tags_default": max_tags_default,
        "max_dynamic_tags": max_dynamic_tags,
        "max_tag_chars": max_tag_chars,
        "max_dynamic_tag_chars": max_dynamic_tag_chars,
        "base_tag_count": len(base_tags),
    }.items():
        prompt = prompt.replace("{" + name + "}", str(value))
    try:
        rendered = prompt.format(
            existing_tags="EXISTING_TAGS", title="TITLE", srt_text="SRT_TEXT"
        )
    except (KeyError, ValueError) as exc:
        raise UploadTagPolicyError(f"invalid content_prompt_template: {exc}") from exc
    if not all(probe in rendered for probe in ("EXISTING_TAGS", "TITLE", "SRT_TEXT")):
        raise UploadTagPolicyError(
            "content_prompt_template must preserve existing_tags, title, and srt_text"
        )

    policy = UploadTagPolicy(
        base_tags=base_tags,
        max_tags_default=max_tags_default,
        max_dynamic_tags=max_dynamic_tags,
        max_tag_chars=max_tag_chars,
        max_dynamic_tag_chars=max_dynamic_tag_chars,
        term_rules=tuple(rules),
        important_content_ips=tuple(important_content_ips),
        known_proper_surfaces_extra=_string_list(
            root.get("known_proper_surfaces_extra"),
            label="known_proper_surfaces_extra",
            allow_empty=True,
        ),
        theme_allowed=frozenset(
            _string_list(root.get("theme_allowed"), label="theme_allowed", allow_empty=True)
        ),
        banned_content_tags=frozenset(
            _string_list(
                root.get("banned_content_tags"),
                label="banned_content_tags",
                allow_empty=True,
            )
        ),
        content_prompt_template=prompt,
        source_path=path,
    )
    from src.autoslice.authorized_upload_metadata import validate_tag_policy_limits

    problems = validate_tag_policy_limits(policy)
    if problems:
        raise UploadTagPolicyError("; ".join(problems))
    return policy


def _parse_term_rules(value: object, *, label: str) -> tuple[TermRule, ...]:
    raw_rules = value
    if not isinstance(raw_rules, list):
        raise UploadTagPolicyError(f"{label} must be a list")
    rules: list[TermRule] = []
    for index, raw_rule in enumerate(raw_rules):
        rule_label = f"{label}[{index}]"
        rule = _mapping(raw_rule, label=rule_label)
        _strict_keys(
            rule,
            label=rule_label,
            required={"name", "patterns", "tags", "min_hits", "note"},
        )
        patterns = _string_list(rule.get("patterns"), label=f"{rule_label}.patterns")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise UploadTagPolicyError(
                    f"{rule_label}.patterns contains invalid regex {pattern!r}: {exc}"
                ) from exc
        rules.append(
            TermRule(
                name=_string(rule.get("name"), label=f"{rule_label}.name"),
                patterns=patterns,
                tags=_string_list(rule.get("tags"), label=f"{rule_label}.tags"),
                min_hits=_positive_int(
                    rule.get("min_hits"), label=f"{rule_label}.min_hits"
                ),
                note=_string(
                    rule.get("note"), label=f"{rule_label}.note", allow_empty=True
                ),
            )
        )
    names = [rule.name for rule in rules]
    if len(names) != len(set(names)):
        raise UploadTagPolicyError(f"{label} must not repeat rule names")
    return tuple(rules)


def load_selected_upload_tag_policy(
    profile: ChannelProfile | None = None,
) -> UploadTagPolicy:
    selected = profile or load_channel_profile(REPO_ROOT)
    return load_upload_tag_policy(selected.asset_file("upload_tag_policy"))


@dataclass(frozen=True)
class TagPolicyResult:
    """Outcome of enforcing upload tag policy on a post-step-7 tag list."""

    tags: tuple[str, ...]
    ok: bool
    reasons: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    restored_base: tuple[str, ...] = ()


def apply_tag_policy(
    proposed: object,
    *,
    previous: Sequence[str] | None = None,
    base_tags: Sequence[str] | None = None,
    priority_tags: Sequence[str] = (),
    policy: UploadTagPolicy | None = None,
    on_overflow: Literal["evict", "refuse"] = "evict",
    prefer_new: bool = False,
) -> TagPolicyResult:
    """Enforce the step-7 tag policy on any tag list added or changed after step 7.

    Guarantees cap (min(policy.max_tags_default, PLATFORM_MAX_TAGS)), case-insensitive
    de-duplication, per-tag length/separator validity, and retention of fixed base tags.
    In ``evict`` mode, one unified eviction order applies in both merge modes: fixed base
    tags are never evicted; priority/proper-noun tags are evicted only when no general
    tag remains; among general tags, ``prefer_new=True`` (review path) keeps new tags over
    existing ones, while ``prefer_new=False`` (step-7 re-run merge) keeps earlier
    (reviewed-kept) tags over trailing regenerated ones, evicting rightmost-first within
    each tier.
    In ``refuse`` mode, any overflow, duplicate, missing fixed base tag, or invalid tag
    is refused with stated reasons.
    """
    from src.autoslice.authorized_upload_metadata import (
        PLATFORM_MAX_TAG_CHARS,
        PLATFORM_MAX_TAGS,
        validate_tags,
    )

    if (
        not isinstance(proposed, Sequence)
        or isinstance(proposed, (str, bytes))
        or not proposed
    ):
        return TagPolicyResult(
            tags=(), ok=False, reasons=("tags must be a non-empty list of strings",)
        )
    if any(not isinstance(item, str) for item in proposed):
        return TagPolicyResult(
            tags=(), ok=False, reasons=("tags must be a list of strings",)
        )

    active = policy or load_selected_upload_tag_policy()
    cap = min(active.max_tags_default, PLATFORM_MAX_TAGS)
    max_chars = min(active.max_tag_chars, PLATFORM_MAX_TAG_CHARS)

    items = [item.strip() for item in proposed]
    structural_errors: list[str] = []
    if any(not item for item in items):
        structural_errors.append("tags contain an empty item")
    for item in items:
        if len(item) > max_chars:
            structural_errors.append(f"tag too long (>{max_chars} chars): {item!r}")
        if any(ch in item for ch in ",，\n\t"):
            structural_errors.append(f"tag contains a separator character: {item!r}")
    if structural_errors:
        return TagPolicyResult(tags=(), ok=False, reasons=tuple(structural_errors))

    prev_keys = (
        {item.strip().casefold() for item in previous if isinstance(item, str) and item.strip()}
        if previous is not None
        else set()
    )
    if base_tags is not None:
        raw_base = tuple(item.strip() for item in base_tags if isinstance(item, str) and item.strip())
    elif previous is not None:
        raw_base = tuple(item for item in active.base_tags if item.casefold() in prev_keys)
    else:
        raw_base = active.base_tags

    base_kept: list[str] = []
    base_keys: set[str] = set()
    for item in raw_base:
        key = item.casefold()
        if key not in base_keys:
            base_keys.add(key)
            base_kept.append(item)

    if len(base_kept) > cap:
        return TagPolicyResult(
            tags=(),
            ok=False,
            reasons=(f"{len(base_kept)} fixed base tags exceed the cap of {cap}",),
        )

    deduped: list[str] = []
    seen_keys: set[str] = set()
    duplicate_items: list[str] = []
    for item in items:
        key = item.casefold()
        if key in seen_keys:
            duplicate_items.append(item)
            continue
        seen_keys.add(key)
        deduped.append(item)

    missing_base = tuple(item for item in base_kept if item.casefold() not in seen_keys)

    if on_overflow == "refuse":
        refusals: list[str] = []
        if duplicate_items:
            refusals.append(f"tags contain duplicates: {', '.join(duplicate_items)}")
        if missing_base:
            refusals.append(f"missing fixed base tags: {', '.join(missing_base)}")
        if len(items) > cap:
            refusals.append(f"{len(items)} tags exceed the cap of {cap}")
        for problem in validate_tags(items, policy=active):
            if problem not in refusals and not (
                problem == "tags contain duplicates" and duplicate_items
            ):
                refusals.append(problem)
        if refusals:
            return TagPolicyResult(tags=(), ok=False, reasons=tuple(refusals))
        return TagPolicyResult(tags=tuple(items), ok=True)

    reasons: list[str] = []
    if duplicate_items:
        reasons.append(f"去重重复标签：{', '.join(duplicate_items)}")
    if missing_base:
        reasons.append(f"保留固定基础标签：{', '.join(missing_base)}")

    non_base = [item for item in deduped if item.casefold() not in base_keys]
    budget = cap - len(base_kept)
    dropped: tuple[str, ...] = ()
    if len(non_base) > budget:
        excess = len(non_base) - budget
        prio_keys = {
            item.strip().casefold()
            for item in priority_tags
            if isinstance(item, str) and item.strip()
        }

        def _evict_rank(entry: tuple[int, str]) -> tuple[int, int, int]:
            idx, value = entry
            key = value.casefold()
            is_general = 0 if key in prio_keys else 1
            is_lower_group = (
                1 if (prefer_new and previous is not None and key in prev_keys) else 0
            )
            return (is_general, is_lower_group, idx)

        ranked = sorted(enumerate(non_base), key=_evict_rank, reverse=True)
        evict_indices = {idx for idx, _value in ranked[:excess]}
        dropped = tuple(
            value for idx, value in enumerate(non_base) if idx in evict_indices
        )
        non_base = [
            value for idx, value in enumerate(non_base) if idx not in evict_indices
        ]
        reasons.append(f"超过上限 {cap} 个，按规则让出内容标签：{', '.join(dropped)}")

    settled = (*base_kept, *non_base)
    if not settled:
        return TagPolicyResult(
            tags=(), ok=False, reasons=("tags must be a non-empty list of strings",)
        )
    problems = validate_tags(list(settled), policy=active)
    if problems:
        return TagPolicyResult(tags=(), ok=False, reasons=tuple(problems))
    return TagPolicyResult(
        tags=settled,
        ok=True,
        reasons=tuple(reasons),
        dropped=dropped,
        restored_base=missing_base,
    )
