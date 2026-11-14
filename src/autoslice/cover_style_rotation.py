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
from collections.abc import Callable, Mapping, Sequence
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


def default_content_sha256(spec: Mapping) -> str:
    """Fallback content key when the caller has no finished-content hash: lines text plus frame name."""

    payload = {
        "lines": [line.get("text") for line in spec.get("lines", []) if isinstance(line, Mapping)],
        "frame": spec.get("frame") or spec.get("bg"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()






RECENT_COLOR_WINDOW = 1


def recent_exclusions(table: Mapping, recent_style_ids: Sequence[str] = ()) -> dict[str, str]:
    """The styles the next cover skips, ``{style_id: reason}``.

    ``recent_used``: a recent cover used the style.  ``recent_colors``: the style has the
    colour pair of the last ``RECENT_COLOR_WINDOW`` covers.  ``recent_style_ids`` are the
    recent covers' styles, oldest first (``title_cover_history.recent_cover_style_ids``); an id
    not in the table only counts as used.  Callers including the skill script's ``--recent``
    pass the real history, not the expanded exclusion keys.
    """

    ids = [str(s) for s in recent_style_ids]
    by_id = {str(s["style_id"]): s for s in table["styles"]}
    out = {sid: "recent_used" for sid in ids}
    last = [by_id[sid] for sid in ids[-RECENT_COLOR_WINDOW:] if sid in by_id] if ids else []
    pairs = {frozenset(style["colors"]) for style in last}
    for style in table["styles"]:
        sid = str(style["style_id"])
        if sid not in out and frozenset(style["colors"]) in pairs:
            out[sid] = "recent_colors"
    return out


def select_style(
    table: Mapping,
    content_sha256: str,
    *,
    attempt: Callable[[Mapping], Mapping],
    recent_style_ids: Sequence[str] = (),
) -> tuple[dict, dict]:
    """Return ``(attempt_result, selection)``.

    ``attempt(style)`` lays the cover out with that style and returns a mapping that
    contains ``low_contrast`` (bool). It may raise ``SystemExit`` or ``ValueError``
    when that style does not fit; such a style is skipped with reason ``layout_failed``.
    """

    styles = list(table["styles"])
    n = len(styles)
    if not isinstance(content_sha256, str) or len(content_sha256) < 16:
        raise CoverStyleTableError("COVER_STYLE_CONTENT_HASH_INVALID")
    base = int(content_sha256[:16], 16) % n
    excluded = recent_exclusions(table, recent_style_ids)
    recent = set(excluded)
    order = [(base + k) % n for k in range(n)]
    skipped: list[dict] = []

    def run(index: int):
        style = styles[index]
        try:
            result = attempt(style)
        except (SystemExit, ValueError) as exc:
            return None, "layout_failed:" + str(exc)[:120]
        if result.get("low_contrast"):
            return result, "low_contrast"
        return result, None

    def finish(index: int, result: Mapping, reason: str | None) -> tuple[dict, dict]:
        style = styles[index]
        selection = {
            "style_id": style["style_id"],
            "table_sha256": table.get("table_sha256"),
            "content_sha256": content_sha256,
            "base_index": base,
            "chosen_index": index,
            "selection": "hash" if index == base and not skipped else "rotated",
            "skipped": list(skipped),
            "recent_style_ids": sorted({str(x) for x in recent_style_ids}),
            "fallback": reason,
        }
        return {**dict(result), "style": style, "selection_detail": selection}, selection

    for index in order:
        style_id = styles[index]["style_id"]
        if style_id in recent:
            skipped.append({"style_id": style_id, "reason": excluded[style_id]})
            continue
        result, reason = run(index)
        if reason is None:
            return finish(index, result, None)
        skipped.append({"style_id": style_id, "reason": reason})
    # Every non-recent style failed or was recent. Recent styles are the last resort, so a
    # short table can still repeat rather than fail.
    for index in order:
        style_id = styles[index]["style_id"]
        if style_id not in recent:
            continue
        result, reason = run(index)
        if reason is None:
            return finish(index, result, "recent_repeat_forced")
        skipped.append({"style_id": style_id, "reason": reason})
    # Everything was rejected. Use the hashed base style and keep its warning visible.
    result = attempt(styles[base])
    return finish(base, result, "all_styles_rejected")
