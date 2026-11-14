"""Deterministic, independently replayable title-layer rendering."""

from __future__ import annotations


from PIL import ImageFont


class CoverTitleRenderError(ValueError):
    pass


def _basic_layout_engine(image_font: object = ImageFont) -> object:
    """Resolve Pillow's BASIC engine across the enum and legacy APIs."""

    layout = getattr(image_font, "Layout", None)
    basic = getattr(layout, "BASIC", None) if layout is not None else None
    if basic is not None:
        return basic
    legacy = getattr(image_font, "LAYOUT_BASIC", None)
    if legacy is not None:
        return legacy
    raise CoverTitleRenderError("COVER_TITLE_RENDER_BASIC_LAYOUT_UNAVAILABLE")


