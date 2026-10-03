"""Reviewed candidate-specific cover art-direction overrides."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol, TypeVar


class _CoverDirection(Protocol):
    is_song: bool


_DirectionT = TypeVar("_DirectionT", bound=_CoverDirection)
_BOOKSHOP_QSTYLE_CANDIDATE_ID = "auto_222823_493_595"
_BOOKSHOP_QSTYLE_VISUAL_BRIEF = (
    "Use a cute hand-drawn chibi (Q-style) original editorial redraw, not a polished "
    "livestream screenshot. Stage Li Dousha reacting in an anime bookstore yuri section "
    "beside three separate fictional yuri books whose cover illustrations visibly repeat "
    "the same absurd from-behind neck-grab composition; keep all book art free of legible "
    "titles or logos. Make the three-book repetition immediately readable while Li Dousha "
    "remains the primary identifiable reaction subject. Preserve every source-visible "
    "identity feature exactly, especially the source-visible eyepatch, exact outfit layers "
    "and hair ornaments; never reveal or invent the covered eye. Keep the visible eye, full "
    "eyepatch, mouth, chin and face outline uncropped and unobstructed by shelves, book cards, "
    "props or the reserved local-title zone."
)


def apply_candidate_cover_direction(candidate_id: str, direction: _DirectionT) -> _DirectionT:
    """Apply explicit, reviewable art direction for one adjudicated clip."""

    if candidate_id != _BOOKSHOP_QSTYLE_CANDIDATE_ID or direction.is_song:
        return direction
    return replace(direction, visual_brief=_BOOKSHOP_QSTYLE_VISUAL_BRIEF)
