"""Deterministic cover style rotation (no PIL dependency).

The authority is ``assets/lidousha/cover_style_rotation.json``. The production
renderer and the skill script both load that file; neither keeps its own palette
or default style. The selection walks the table in rotation order from the
content hash, skips styles used by recent covers and styles with the previous
cover's colour pair (``recent_exclusions``), and skips styles whose line
colour is too close to the background. The caller supplies ``attempt`` so the
layout is evaluated once per candidate style.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA = "lidousha-cover-style-rotation.v1"
REPO_TABLE = Path(__file__).resolve().parents[2] / "assets" / "lidousha" / "cover_style_rotation.json"


class CoverStyleTableError(ValueError):
    pass


def _rgb(value: object) -> tuple[int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255 for v in value)
    ):
        raise CoverStyleTableError("COVER_STYLE_TABLE_COLOR_INVALID")
    return tuple(value)


def validate_table(table: object) -> dict:
    if not isinstance(table, dict) or table.get("schema_version") != SCHEMA:
        raise CoverStyleTableError("COVER_STYLE_TABLE_SCHEMA_INVALID")
    palette = table.get("palette")
    if not isinstance(palette, dict) or not palette:
        raise CoverStyleTableError("COVER_STYLE_TABLE_PALETTE_INVALID")
    for name, pair in palette.items():
        if not isinstance(pair, dict):
            raise CoverStyleTableError("COVER_STYLE_TABLE_PALETTE_INVALID")
        _rgb(pair.get("top"))
        _rgb(pair.get("bottom"))
    strokes = table.get("strokes")
    if not isinstance(strokes, dict) or not strokes:
        raise CoverStyleTableError("COVER_STYLE_TABLE_STROKES_INVALID")
    for name, recipe in strokes.items():
        if not isinstance(recipe, dict):
            raise CoverStyleTableError("COVER_STYLE_TABLE_STROKES_INVALID")
        if recipe.get("inner") is None and recipe.get("outer") is None:
            raise CoverStyleTableError("COVER_STYLE_TABLE_STROKES_INVALID")
        for key in ("inner", "outer"):
            if recipe.get(key) is not None:
                _rgb(recipe[key])
        ratio = recipe.get("outer_extra_ratio")
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or ratio < 0:
            raise CoverStyleTableError("COVER_STYLE_TABLE_STROKES_INVALID")
    tilts = table.get("allowed_tilts")
    if (
        not isinstance(tilts, list)
        or not tilts
        or any(isinstance(t, bool) or not isinstance(t, int) or t == 0 or abs(t) > 6 for t in tilts)
    ):
        raise CoverStyleTableError("COVER_STYLE_TABLE_TILT_INVALID")
    contrast = table.get("contrast") or {}
    if (
        isinstance(contrast.get("min_distance"), bool)
        or not isinstance(contrast.get("min_distance"), int)
        or contrast["min_distance"] <= 0
    ):
        raise CoverStyleTableError("COVER_STYLE_TABLE_CONTRAST_INVALID")
    styles = table.get("styles")
    if not isinstance(styles, list) or not styles:
        raise CoverStyleTableError("COVER_STYLE_TABLE_STYLES_INVALID")
    seen: set[str] = set()
    for style in styles:
        if not isinstance(style, dict):
            raise CoverStyleTableError("COVER_STYLE_TABLE_STYLES_INVALID")
        sid = style.get("style_id")
        if not isinstance(sid, str) or not sid or sid in seen:
            raise CoverStyleTableError("COVER_STYLE_TABLE_STYLES_INVALID")
        seen.add(sid)
        colors = style.get("colors")
        if (
            not isinstance(colors, list)
            or len(colors) != 2
            or any(c not in palette for c in colors)
        ):
            raise CoverStyleTableError("COVER_STYLE_TABLE_STYLES_INVALID")
        if style.get("tilt") not in tilts or isinstance(style.get("tilt"), bool):
            raise CoverStyleTableError("COVER_STYLE_TABLE_STYLES_INVALID")
        ratio = style.get("size_ratio")
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0.3 <= ratio <= 1:
            raise CoverStyleTableError("COVER_STYLE_TABLE_STYLES_INVALID")
        if style.get("stroke") not in strokes:
            raise CoverStyleTableError("COVER_STYLE_TABLE_STYLES_INVALID")
    return table


def load_table(path: Path | str) -> dict:
    """Read and validate the table. Missing or malformed input raises; there is no built-in fallback."""

    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CoverStyleTableError("COVER_STYLE_TABLE_UNREADABLE") from exc
    try:
        table = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoverStyleTableError("COVER_STYLE_TABLE_JSON_INVALID") from exc
    table = validate_table(table)
    return {**table, "table_sha256": hashlib.sha256(raw).hexdigest()}


