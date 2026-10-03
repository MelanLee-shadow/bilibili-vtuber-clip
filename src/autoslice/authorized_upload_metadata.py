"""Small, reusable metadata rules for the authorized upload boundary."""

from __future__ import annotations




MAX_TAGS = 12
MAX_TAG_CHARS = 20


def validate_tags(tags: list[str]) -> list[str]:
    """Return problems; empty means the tag list is manifest-worthy."""

    problems: list[str] = []
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        return ["tags must be a list of strings"]
    cleaned = [tag.strip() for tag in tags]
    if any(not tag for tag in cleaned):
        problems.append("tags contain an empty item")
    if len(cleaned) > MAX_TAGS:
        problems.append(f"{len(cleaned)} tags exceed the cap of {MAX_TAGS}")
    if len({tag.casefold() for tag in cleaned}) != len(cleaned):
        problems.append("tags contain duplicates")
    for tag in cleaned:
        if len(tag) > MAX_TAG_CHARS:
            problems.append(f"tag too long (>{MAX_TAG_CHARS} chars): {tag!r}")
        if any(character in tag for character in ",，\n\t"):
            problems.append(f"tag contains a separator character: {tag!r}")
    return problems


def normalise_tags(value: object) -> list[str]:
    """Normalize accepted tag input shapes without asserting validity."""

    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


__all__ = ["MAX_TAGS", "MAX_TAG_CHARS", "normalise_tags", "validate_tags"]
