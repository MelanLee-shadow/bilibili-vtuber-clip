"""Deterministic, independently replayable title-layer rendering."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont


SCHEMA_VERSION = "lidousha-cover-title-render-spec.v1"
FEED_SAFE_X0 = 260
FEED_SAFE_X1 = 1660
TITLE_BASE_FILL = (255, 246, 214)
TITLE_OUTER_STROKE = (18, 36, 79)
TITLE_INNER_STROKE = (255, 255, 255)
TITLE_HOOK_FILLS = (
    (255, 198, 41),
    (255, 92, 138),
    (150, 106, 245),
    (58, 141, 237),
    (255, 140, 60),
    (233, 69, 69),
)
TITLE_ALLOWED_FILLS = frozenset((TITLE_BASE_FILL, *TITLE_HOOK_FILLS))


class CoverTitleRenderError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def render_spec_sha256(spec: Mapping[str, object]) -> str:
    encoded = json.dumps(
        spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def materialize_title_layer_spec(
    *,
    font_path: Path,
    font_face_index: int,
    layer_size: tuple[int, int],
    angle_degrees: float,
    render_lines: Sequence[Mapping[str, object]],
    top_pad: int,
) -> tuple[Image.Image, dict[str, object]]:
    """Build the producer spec and immediately replay its final RGBA layer."""

    y = top_pad
    lines: list[dict[str, object]] = []
    for line in render_lines:
        outlines = list(line["outlines"])  # producer-internal normalized rows
        lines.append(
            {
                "x": (layer_size[0] - float(line["width"])) / 2,
                "y": y,
                "font_size": int(line["font_size"]),
                "segment_pad": int(outlines[0]["width"]),
                "segments": list(line["segments"]),
                "outlines": outlines,
            }
        )
        y += int(line["height"]) + int(line["gap"])
    spec: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "font_file_name": font_path.name,
        "font_file_sha256": sha256_file(font_path),
        "font_face_index": int(font_face_index),
        "layer_size": list(layer_size),
        "rendered_layer_size": [],
        "angle_degrees": angle_degrees,
        "lines": lines,
    }
    first = render_title_layer(
        spec, font_path=font_path, verify_output_size=False
    )
    spec["rendered_layer_size"] = list(first.size)
    return render_title_layer(spec, font_path=font_path), spec


def _color(value: object, *, label: str) -> tuple[int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or any(
            isinstance(part, bool)
            or not isinstance(part, int)
            or not 0 <= part <= 255
            for part in value
        )
    ):
        raise CoverTitleRenderError(f"{label}_INVALID")
    return tuple(value)


def _positive_int(
    value: object, *, label: str, maximum: int = 4096
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= maximum
    ):
        raise CoverTitleRenderError(f"{label}_INVALID")
    return value


def _seg_outlines(
    fill: tuple[int, int, int],
    outlines: list[tuple[int, tuple[int, int, int]]],
) -> list[tuple[int, tuple[int, int, int]]]:
    luminance = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
    return outlines[:1] if luminance > 200 else outlines


def render_title_layer(
    spec: Mapping[str, object],
    *,
    font_path: Path,
    verify_output_size: bool = True,
    layout_engine: ImageFont.Layout | None = None,
) -> Image.Image:
    """Strictly validate and replay one renderer-produced RGBA title layer."""

    if spec.get("schema_version") != SCHEMA_VERSION:
        raise CoverTitleRenderError("COVER_TITLE_RENDER_SPEC_SCHEMA_INVALID")
    if str(spec.get("font_file_name") or "") != font_path.name:
        raise CoverTitleRenderError("COVER_TITLE_RENDER_FONT_NAME_MISMATCH")
    if spec.get("font_file_sha256") != sha256_file(font_path):
        raise CoverTitleRenderError("COVER_TITLE_RENDER_FONT_HASH_MISMATCH")
    face_index = spec.get("font_face_index")
    if isinstance(face_index, bool) or not isinstance(face_index, int) or face_index < 0:
        raise CoverTitleRenderError("COVER_TITLE_RENDER_FACE_INDEX_INVALID")
    layer_size = spec.get("layer_size")
    if (
        not isinstance(layer_size, list)
        or len(layer_size) != 2
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 4096
            for value in layer_size
        )
    ):
        raise CoverTitleRenderError("COVER_TITLE_RENDER_LAYER_SIZE_INVALID")
    raw_lines = spec.get("lines")
    if (
        not isinstance(raw_lines, list)
        or not raw_lines
        or len(raw_lines) > 12
    ):
        raise CoverTitleRenderError("COVER_TITLE_RENDER_LINES_INVALID")
    layer = Image.new("RGBA", tuple(layer_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for line_index, raw_line in enumerate(raw_lines):
        if not isinstance(raw_line, Mapping):
            raise CoverTitleRenderError("COVER_TITLE_RENDER_LINE_INVALID")
        x = raw_line.get("x")
        y = raw_line.get("y")
        if (
            isinstance(x, bool)
            or not isinstance(x, (int, float))
            or isinstance(y, bool)
            or not isinstance(y, (int, float))
            or not math.isfinite(float(x))
            or not math.isfinite(float(y))
            or not -8192 <= float(x) <= 8192
            or not -8192 <= float(y) <= 8192
        ):
            raise CoverTitleRenderError("COVER_TITLE_RENDER_POSITION_INVALID")
        size = _positive_int(
            raw_line.get("font_size"),
            label="COVER_TITLE_RENDER_FONT_SIZE",
            maximum=1024,
        )
        pad = _positive_int(
            raw_line.get("segment_pad"),
            label="COVER_TITLE_RENDER_SEGMENT_PAD",
            maximum=512,
        )
        font = ImageFont.truetype(
            str(font_path), size, index=face_index, layout_engine=layout_engine
        )
        raw_outlines = raw_line.get("outlines")
        if (
            not isinstance(raw_outlines, list)
            or not raw_outlines
            or len(raw_outlines) > 4
        ):
            raise CoverTitleRenderError("COVER_TITLE_RENDER_OUTLINES_INVALID")
        outlines = []
        for outline in raw_outlines:
            if not isinstance(outline, Mapping):
                raise CoverTitleRenderError("COVER_TITLE_RENDER_OUTLINE_INVALID")
            outlines.append(
                (
                    _positive_int(
                        outline.get("width"),
                        label="COVER_TITLE_RENDER_OUTLINE_WIDTH",
                        maximum=256,
                    ),
                    _color(
                        outline.get("color"),
                        label="COVER_TITLE_RENDER_OUTLINE_COLOR",
                    ),
                )
            )
        expected_outlines = [
            (
                max(8, int(round(size * 0.085))),
                TITLE_OUTER_STROKE,
            ),
            (
                max(4, int(round(size * 0.042))),
                TITLE_INNER_STROKE,
            ),
        ]
        if outlines != expected_outlines:
            raise CoverTitleRenderError(
                "COVER_TITLE_RENDER_OUTLINE_POLICY_INVALID"
            )
        raw_segments = raw_line.get("segments")
        if (
            not isinstance(raw_segments, list)
            or not raw_segments
            or len(raw_segments) > 32
        ):
            raise CoverTitleRenderError("COVER_TITLE_RENDER_SEGMENTS_INVALID")
        segments: list[tuple[str, tuple[int, int, int]]] = []
        for segment in raw_segments:
            if not isinstance(segment, Mapping):
                raise CoverTitleRenderError("COVER_TITLE_RENDER_SEGMENT_INVALID")
            text = str(segment.get("text") or "")
            if not text or any(ord(char) < 32 for char in text):
                raise CoverTitleRenderError("COVER_TITLE_RENDER_TEXT_INVALID")
            segments.append(
                (
                    text,
                    _color(
                        segment.get("fill"),
                        label="COVER_TITLE_RENDER_FILL",
                    ),
                )
            )
            if segments[-1][1] not in TITLE_ALLOWED_FILLS:
                raise CoverTitleRenderError(
                    "COVER_TITLE_RENDER_FILL_POLICY_INVALID"
                )
        if sum(len(text) for text, _fill in segments) > 256:
            raise CoverTitleRenderError("COVER_TITLE_RENDER_TEXT_INVALID")

        def segment_width(text: str) -> float:
            return sum(draw.textlength(char, font=font) for char in text)

        validation_cursor = float(x)
        for index, (text, fill) in enumerate(segments):
            max_stroke = max(
                width for width, _color_value in _seg_outlines(fill, outlines)
            )
            char_x = validation_cursor
            for char in text:
                glyph_box = draw.textbbox(
                    (char_x, y),
                    char,
                    font=font,
                    stroke_width=max_stroke,
                )
                if (
                    glyph_box[0] < 0
                    or glyph_box[1] < 0
                    or glyph_box[2] > layer.width
                    or glyph_box[3] > layer.height
                ):
                    raise CoverTitleRenderError(
                        "COVER_TITLE_RENDER_GLYPH_CLIPPED"
                    )
                char_x += draw.textlength(char, font=font)
            validation_cursor += segment_width(text) + (
                0 if index == len(segments) - 1 else pad
            )

        cursor = float(x)
        for index, (text, fill) in enumerate(segments):
            for width, color in _seg_outlines(fill, outlines):
                char_x = cursor
                for char in text:
                    draw.text(
                        (char_x, y),
                        char,
                        font=font,
                        fill=fill,
                        stroke_width=width,
                        stroke_fill=color,
                    )
                    char_x += draw.textlength(char, font=font)
            cursor += segment_width(text) + (
                0 if index == len(segments) - 1 else pad
            )
        cursor = float(x)
        for index, (text, fill) in enumerate(segments):
            char_x = cursor
            for char in text:
                draw.text((char_x, y), char, font=font, fill=fill)
                char_x += draw.textlength(char, font=font)
            cursor += segment_width(text) + (
                0 if index == len(segments) - 1 else pad
            )
    angle = spec.get("angle_degrees")
    if isinstance(angle, bool) or not isinstance(angle, (int, float)):
        raise CoverTitleRenderError("COVER_TITLE_RENDER_ANGLE_INVALID")
    if not math.isfinite(float(angle)) or not -30 <= float(angle) <= 30:
        raise CoverTitleRenderError("COVER_TITLE_RENDER_ANGLE_INVALID")
    if angle:
        layer = layer.rotate(
            float(angle),
            resample=Image.Resampling.BICUBIC,
            expand=True,
        )
    expected_size = spec.get("rendered_layer_size")
    if verify_output_size and expected_size != list(layer.size):
        raise CoverTitleRenderError("COVER_TITLE_RENDER_OUTPUT_SIZE_MISMATCH")
    return layer
