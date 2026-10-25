"""Small, reusable metadata rules for the authorized upload boundary.

Platform upper bounds (not verified rejection boundaries):
* Tag count: repository readback reports/tag-suggestions-20260713/
  tag_update_results_10.json:147-151, BV1EQNk6KErE. Source grade:
  "线上观察到已接受，未见拒收边界"; the row is SKIP_ALREADY, not an edit receipt.
* Characters per tag: admin-supplied third-party submission descriptions
  (ucjz.com 投稿 API article, shankongmao.com, zhihu.com/p/687058405).
  Source grade: "外部资料，未经拒收验证". These descriptions say at most ten
  tags, unlike the observed twelve; the selected channel policy uses ten,
  satisfying both count descriptions. These are conservative local bounds,
  not a claim that the next value has been rejected by Bilibili.

Channel limits belong to the selected upload_tag_policy.json, separately from
these platform bounds. Keep existing MAX_* exports as compatibility aliases.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.autoslice.upload_tag_policy import UploadTagPolicy

PLATFORM_MAX_TAGS = 12
PLATFORM_MAX_TAG_CHARS = 20
MAX_TAGS = PLATFORM_MAX_TAGS
MAX_TAG_CHARS = PLATFORM_MAX_TAG_CHARS


def validate_tag_policy_limits(policy: UploadTagPolicy) -> list[str]:
    """Reject channel configuration outside the declared platform bounds."""
    problems: list[str] = []
    if policy.max_tags_default > PLATFORM_MAX_TAGS:
        problems.append("max_tags_default exceeds platform tag count bound")
    if policy.max_tag_chars > PLATFORM_MAX_TAG_CHARS:
        problems.append("max_tag_chars exceeds platform character bound")
    if policy.max_dynamic_tag_chars > policy.max_tag_chars:
        problems.append("max_dynamic_tag_chars exceeds max_tag_chars")
    return problems


def validate_tags(tags: list[str]) -> list[str]:
    """Return problems; empty means the tag list is manifest-worthy."""

    problems: list[str] = []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        return ["tags must be a list of strings"]
    from src.autoslice.upload_tag_policy import load_selected_upload_tag_policy

    policy = load_selected_upload_tag_policy()
    max_tags = min(policy.max_tags_default, PLATFORM_MAX_TAGS)
    max_tag_chars = min(policy.max_tag_chars, PLATFORM_MAX_TAG_CHARS)
    cleaned = [tag.strip() for tag in tags]
    if any(not tag for tag in cleaned):
        problems.append("tags contain an empty item")
    if len(cleaned) > max_tags:
        problems.append(f"{len(cleaned)} tags exceed the cap of {max_tags}")
    if len({tag.casefold() for tag in cleaned}) != len(cleaned):
        problems.append("tags contain duplicates")
    for tag in cleaned:
        if len(tag) > max_tag_chars:
            problems.append(f"tag too long (>{max_tag_chars} chars): {tag!r}")
        if any(character in tag for character in ",，\n\t"):
            problems.append(f"tag contains a separator character: {tag!r}")
    return problems


def validate_tag_generation(upload_tags: object) -> list[str]:
    """A failed topic pass must not silently publish base tags alone.

    Successful empty topic results and real searchable proper-name tags keep
    their existing semantics; this detects a provider/configuration failure.
    """
    if not isinstance(upload_tags, Mapping):
        return []
    incomplete = upload_tags.get("status") in {"OK_NO_LLM", "FAILED"} or (
        upload_tags.get("content_review_status") == "NOT_REQUESTED"
    )
    if not incomplete:
        return []
    from src.autoslice.upload_tag_policy import load_selected_upload_tag_policy

    base = set(load_selected_upload_tag_policy().base_tags)
    final = upload_tags.get("final_tags") or []
    if isinstance(final, list) and any(isinstance(tag, str) and tag not in base for tag in final):
        return []
    return ["CONTENT_TAG_GENERATION_INCOMPLETE: topic generation failed or was skipped; review tags before publishing"]


def normalise_tags(value: object) -> list[str]:
    """Normalize accepted tag input shapes without asserting validity."""

    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


__all__ = [
    "PLATFORM_MAX_TAGS", "PLATFORM_MAX_TAG_CHARS", "MAX_TAGS", "MAX_TAG_CHARS",
    "normalise_tags", "validate_tags", "validate_tag_generation", "validate_tag_policy_limits",
]
