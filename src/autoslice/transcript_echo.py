"""Classify registered confusable surfaces that cannot self-witness in ASR text."""

from __future__ import annotations

import re
from typing import Any


def _official_legal_surfaces(canonical: str, profile: Any) -> set[str]:
    try:
        from src.autoslice.psplive_roster_crawler import load_source_config

        config = load_source_config(profile.asset_file("psplive_roster_sources"))
    except Exception:
        return set()
    legal: set[str] = set()
    for official, row in config.get("member_overrides", {}).items():
        if not isinstance(row, dict) or str(row.get("canonical") or "").casefold() != canonical.casefold():
            continue
        legal.add(str(official))
        legal.update(str(value) for value in row.get("aliases", []) if str(value))
    return legal


def confusable_transcript_echo(surface: str) -> dict[str, object] | None:
    """Return an audit receipt for a registered, non-authoritative ASR surface.

    The shared referent loader intentionally keeps aliases and mishearings in
    one surface list.  We therefore exempt canonical/full-name forms,
    occurrence-neutral official-roster aliases, declared uncertain-keep
    surfaces, and mixed-script spellings; ambiguous spellings fail safe as
    legal instead of being demoted.
    """

    if not surface:
        return None
    try:
        from src.autoslice.chat_authority import load_referent_groups
        from src.autoslice.term_authority import CHANNEL_PROFILE

        groups = load_referent_groups(
            CHANNEL_PROFILE.asset_file("entity_confusables"), include_singletons=True
        )
    except Exception:
        return None
    folded = surface.casefold()
    for group in groups:
        for entity in group.entities:
            registered = {value.casefold(): value for value in entity.surfaces}
            if folded not in registered or folded == entity.canonical.casefold():
                continue
            official = _official_legal_surfaces(entity.canonical, CHANNEL_PROFILE)
            legal = {
                value
                for value in entity.surfaces
                if (
                    value.casefold() == entity.canonical.casefold()
                    or entity.canonical.casefold() in value.casefold()
                    or value in group.uncertain_keep_surfaces
                    or re.search(r"[A-Za-z]", value)
                    or value.casefold() in {item.casefold() for item in official}
                )
            }
            if folded in {value.casefold() for value in legal}:
                return None
            return {
                "kind": "transcript_echo_suspected",
                "surface": registered[folded],
                "canonical": entity.canonical,
                "legal_surfaces": sorted(legal, key=str.casefold),
            }
    return None
