"""What the judge counts as a flaw at the canvas edge, how fast the renderer is driven, and where a route's seconds go
(step 6, 70-cover; the second real run: every cut-out and sticker was vetoed for being cut by the canvas edge, and one
refused cover took 380 s)."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.step_io import PROMPTS

REMOTE = Path(si.REMOTE_TOOLS)
FONTS = [os.environ.get("LIDOUSHA_COVER_FONT") or "", str(Path.home() / ".local/share/fonts/lidousha/RuiZiZhenYan.ttf"),
         "/System/Library/Fonts/STHeiti Medium.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"]
FONT = next((f for f in FONTS if f and Path(f).is_file()), None)


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_edges_test", REMOTE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----- the judge's definition of a clean layer -------------------------------------------------------------------------------
def test_the_judge_counts_a_layer_cut_by_the_canvas_edge_as_fine_and_a_flat_cut_inside_the_canvas_as_a_flaw():
    text = (PROMPTS / "cover_judge.md").read_text(encoding="utf-8")
    axis = next(line for line in text.splitlines() if line.startswith("- clean_layers"))
    assert "画布边缘" in axis and "不算缺陷" in axis                       # leaving through any canvas edge is intended
    assert "平直切边" in axis and "落在画布内部" in axis                     # a flat cut that lies inside the canvas is a flaw
    for flaw in ("残留背景", "抠图破洞", "方块底"):
        assert flaw in axis
    assert "头被切掉" not in axis                                           # the old wording judged the head cut by the frame itself


# ----- the renderer driven fast, exactly -------------------------------------------------------------------------------------
def test_the_fast_flood_fill_fills_exactly_what_the_pil_one_fills():
    fast = _load("cover_fast_text")
    cases = []
    ring = Image.new("L", (60, 40), 0)
    ImageDraw.Draw(ring).rectangle((10, 8, 49, 31), outline=255, width=3)          # a closed ring: the inside is a hole
    cases.append(ring)
    gap = Image.new("L", (60, 40), 0)
    ImageDraw.Draw(gap).rectangle((10, 8, 49, 31), outline=255, width=3)
    gap.putpixel((30, 8), 0)
    gap.putpixel((30, 9), 0)
    gap.putpixel((30, 10), 0)                                                       # opened: the inside is reachable
    cases.append(gap)
    diagonal = Image.new("L", (40, 40), 0)
    for i in range(40):
        diagonal.putpixel((i, i), 255)                                              # a diagonal wall: 4-connectivity does not cross it
    cases.append(diagonal)
    for image in cases:
        want, got = image.copy(), image.copy()
        ImageDraw.floodfill(want, (0, 0), 128)
        fast.fast_floodfill(got, (0, 0), 128)
        assert got.tobytes() == want.tobytes()


@pytest.mark.skipif(not (FONT and si.COVER_RENDERER.is_file()), reason="no CJK font or skill renderer")
def test_the_fast_renderer_paints_the_same_lines_and_asks_the_slow_painter_once_per_line():
    fast = _load("cover_fast_text")
    spec = importlib.util.spec_from_file_location("edges_renderer", si.COVER_RENDERER)
    plain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plain)
    quick = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(quick)
    plain.install_table(plain.load_styles(si.COVER_STYLES))
    quick.install_table(quick.load_styles(si.COVER_STYLES))
    colorway = next(iter(plain.PAL))
    painted = []
    original = quick.line_layer
    quick.line_layer = lambda *a: painted.append(a) or original(*a)
    fast.fast_renderer(quick)
    for text, size, stroke in (("她破防了", 150, "white"), ("没有那么多", 90, "white")):
        want = plain.line_layer(FONT, text, size, colorway, stroke)
        first = quick.line_layer(FONT, text, size, colorway, stroke)
        second = quick.line_layer(FONT, text, size, colorway, stroke)
        assert first.tobytes() == want.tobytes() and second.tobytes() == want.tobytes()
        assert first is not second                                  # a copy: drawing on it never changes the remembered one
    assert len(painted) == 2


# ----- where a route's seconds go --------------------------------------------------------------------------------------------
def test_the_clock_charges_each_phase_to_the_route_that_is_current():
    clock = _load("cover_clock")
    ticks = iter([0.0, 1.5, 2.0, 5.0, 5.0, 5.25])
    clock.CLOCK["now"] = lambda: next(ticks)

    @clock.timed("locate")
    def locate():
        return "face"
    clock.route("screenshot")
    assert locate() == "face"                                         # 0.0 -> 1.5
    with clock.phase("render"):                                       # 2.0 -> 5.0
        pass
    clock.route("cutout")
    with clock.phase("judge"):                                        # 5.0 -> 5.25
        pass
    assert clock.seconds("screenshot") == {"locate": 1.5, "render": 3.0}
    assert clock.seconds("cutout") == {"judge": 0.25}
    assert clock.seconds("nothing") == {}
