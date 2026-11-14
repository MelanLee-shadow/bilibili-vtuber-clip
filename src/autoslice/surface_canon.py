"""Final, profile-bound surface invariants.

Ordinary transcript canonicalization is advisory and may be superseded by
better evidence. ``hard-meme-canon`` and ``caption-display-canon`` instead
declare fixed final spellings and run after every mutable text stage.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.term_authority import expected_value_surface_rules


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
_CAPTION_DISPLAY_CANON_AUTHORITY = "lidousha-caption-display-canon.v1"
_HARD_MEME_SURFACE_RULES = tuple(
    rule
    for rule in CHANNEL_PROFILE.canonical_surface_rules
    if rule.authority.endswith(("-hard-meme-canon.v1", "-caption-display-canon.v1"))
)
_CAPTION_DISPLAY_SURFACE_RULES = tuple(
    rule
    for rule in CHANNEL_PROFILE.canonical_surface_rules
    if rule.authority == _CAPTION_DISPLAY_CANON_AUTHORITY
)


def _partition_expected_value_surface_rules():
    """Disable a statistical canon as soon as its source becomes a peer term."""

    # Delayed import avoids the term-authority/chat-authority import cycle at
    # module load time.  registered_terms() is the shared glossary/roster
    # authority; a profile typo surface is deliberately absent from it until
    # that surface is independently registered as a real name/term.
    from src.autoslice.term_authority import registered_terms

    registered = registered_terms()
    eligible = []
    conflicts = []
    for rule in expected_value_surface_rules():
        if rule.surface in registered and rule.canonical in registered:
            conflicts.append(rule)
        else:
            eligible.append(rule)
    return tuple(eligible), tuple(conflicts)


def _canonicalize_expected_value_surfaces_with_rules(
    text: str,
    rules,
) -> tuple[str, list[dict[str, str | int]]]:
    normalized = text
    replacements: list[dict[str, str | int]] = []
    for rule in rules:
        count = normalized.count(rule.surface)
        if not count:
            continue
        normalized = normalized.replace(rule.surface, rule.canonical)
        replacements.append(
            {
                "surface": rule.surface,
                "canonical": rule.canonical,
                "authority": rule.authority,
                "count": count,
            }
        )
    return normalized, replacements


def hard_meme_surface_rules():
    """Expose the unbypassable meme-canon rules to downstream judges.

    Text gates that reason over evidence (e.g. source-fact review) must be
    taught these rules explicitly: the canonical spelling carries the banned
    surface's full semantics, so a literal-evidence judge may neither treat
    the canonical form as unsupported nor repair it back to the banned form.
    """

    return _HARD_MEME_SURFACE_RULES


def _hard_meme_canon_prompt_block() -> str:
    """Teach the judge the channel's unbypassable meme canon.

    Without this the literal-evidence gate and the meme canon deadlock: the
    final transcript spells the meme canonically, raw danmaku keeps the banned
    surface, and a judge that only does literal binding "repairs" derived copy
    back and forth until the candidate dies (hook rewrite then also trips the
    scorecard-stale gate).  The canon is final-output law, so the judge must
    read canonical spellings as carrying the original surface's semantics.
    """

    rules = hard_meme_surface_rules()
    if not rules:
        return ""
    listing = "；".join(f"「{rule.surface}」一律写作「{rule.canonical}」" for rule in rules)
    return (
        "频道固定显示规范（梗词和已登记的口播缩写，最终输出铁律）：" + listing + "。"
        "规范词面承载同一原词的语义，不改变原音的读法：最终字幕与两份文案里的规范"
        "词面承载原词的完整语义，判断事实支持时必须按原词语义理解；不得因为"
        "弹幕/证据原文用了被禁拼写而判定文案不受支持，也永远不得把规范词面"
        "改回被禁拼写。你输出的一切文案必须使用规范词面。\n"
    )


def _canonicalize_surface_rules(
    text: str,
    rules,
) -> tuple[str, list[dict[str, str | int]]]:
    normalized = text
    replacements: list[dict[str, str | int]] = []
    for rule in rules:
        if rule.authority.endswith("-caption-display-canon.v1") and rule.surface.isascii():
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_]){re.escape(rule.surface)}(?![A-Za-z0-9_])",
                flags=re.IGNORECASE,
            )
            normalized, count = pattern.subn(rule.canonical, normalized)
        elif (rule.authority.endswith("-caption-display-canon.v1")
              and rule.surface[-1:].isascii() and rule.surface[-1:].isdigit()):
            # A mixed-script role spelling is not a numeric/identifier prefix.
            # For example an explicitly registered X0 does not rewrite X01.
            pattern = re.compile(re.escape(rule.surface) + r"(?![A-Za-z0-9_])")
            normalized, count = pattern.subn(rule.canonical, normalized)
        else:
            count = normalized.count(rule.surface)
            if count:
                normalized = normalized.replace(rule.surface, rule.canonical)
        if not count:
            continue
        replacements.append(
            {
                "surface": rule.surface,
                "canonical": rule.canonical,
                "authority": rule.authority,
                "count": count,
            }
        )
    return normalized, replacements


def canonicalize_hard_meme_surfaces(
    text: str,
) -> tuple[str, list[dict[str, str | int]]]:
    return _canonicalize_surface_rules(text, _HARD_MEME_SURFACE_RULES)


def canonicalize_caption_display_surfaces(
    text: str,
) -> tuple[str, list[dict[str, str | int]]]:
    """Apply only the profile's exact caption-display canon rules."""

    return _canonicalize_surface_rules(text, _CAPTION_DISPLAY_SURFACE_RULES)


def canonicalize_expected_value_surfaces(
    text: str,
) -> tuple[str, list[dict[str, str | int]]]:
    """Apply selected high-prior canon unless both spellings are registered."""

    eligible, _conflicts = _partition_expected_value_surface_rules()
    from src.autoslice.term_authority import spelling_equivalence_rules
    return _canonicalize_expected_value_surfaces_with_rules(text, (*spelling_equivalence_rules(), *eligible))


