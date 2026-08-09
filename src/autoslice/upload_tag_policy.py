"""Strict loader for profile-owned upload-tag knowledge and prompt policy."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.autoslice.channel_profile import ChannelProfile, load_channel_profile


UPLOAD_TAG_POLICY_SCHEMA = "vtuber-slice.upload-tag-policy.v2"
REPO_ROOT = Path(__file__).resolve().parents[2]


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
    prompt = _string(
        root.get("content_prompt_template"),
        label="content_prompt_template",
        allow_empty=True,
    )
    if not prompt.strip():
        raise UploadTagPolicyError("content_prompt_template must be a non-empty string")
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

    return UploadTagPolicy(
        base_tags=base_tags,
        max_tags_default=max_tags_default,
        max_dynamic_tags=max_dynamic_tags,
        max_tag_chars=_positive_int(root.get("max_tag_chars"), label="max_tag_chars"),
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
