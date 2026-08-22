"""Narrow opt-in controls for sealed, screenshot-only cover repair lanes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TypeVar
from collections.abc import Mapping
from collections.abc import Callable

from .cover_punch_semantics import validate_cover_punch_semantic_review


T = TypeVar("T")


@dataclass(frozen=True)
class FixedCoverStageOptions:
    """Explicit repair controls; ordinary staging retains environment defaults."""

    cover_mode_override: str | None = None
    require_screenshot_direct: bool = False
    approved_punch: tuple[str, ...] | None = None
    approved_punch_receipt: Mapping[str, object] | None = None


def resolved_cover_mode(options: FixedCoverStageOptions, environment_mode: str) -> str:
    value = options.cover_mode_override if options.cover_mode_override is not None else environment_mode
    return value if value in ("auto", "screenshot", "polish", "cpa") else "auto"


def apply_approved_punch(
    art_direction: T,
    *, options: FixedCoverStageOptions, punch_allowed: bool,
    cover_text: str,
    story_hook: str,
) -> T | None:
    """Return the exact reviewed lines, or ``None`` when the sealed proof fails."""
    punch = options.approved_punch
    if punch is None:
        return art_direction
    receipt = options.approved_punch_receipt
    if (
        not punch_allowed
        or not isinstance(receipt, Mapping)
        or receipt.get("status") != "PASS"
        or tuple(receipt.get("final_punch") or ()) != punch
        or not validate_cover_punch_semantic_review(
            receipt, rendered_lines=list(punch), cover_text=cover_text, story_hook=story_hook,
        )
    ):
        return None
    return replace(
        art_direction,
        cover_punch=punch,
        cover_punch_semantic_review=dict(receipt),
    )


def fixed_art_direction(
    builder: Callable[..., T],
    *, candidate_id: str, title: str, cover_text: str, llm_call: object,
    emote_library: object, punch_allowed: bool, diversity_slot: int | None,
    story_hook: str, options: FixedCoverStageOptions,
) -> T | None:
    """Build ordinary art direction, then optionally bind a sealed punch."""
    direction = builder(
        candidate_id=candidate_id, title=title, cover_text=cover_text,
        art_direction_llm_call=llm_call, emote_library=emote_library,
        allow_punch=punch_allowed, diversity_slot=diversity_slot, story_hook=story_hook,
    )
    return apply_approved_punch(
        direction, options=options, punch_allowed=punch_allowed,
        cover_text=cover_text, story_hook=story_hook,
    )
