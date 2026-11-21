"""A screenshot with a dead panel is not offered as a cover (assets/manual_lane/remote/cover_dead_panel.py).
one third of a cropped screenshot was the solid black of a screen-share window that captured nothing, and
every check passed it."""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

import pytest

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.lane_rules import rules

pytest.importorskip("PIL")
from PIL import Image, ImageDraw

RULES = rules("cover")


def _tool():
    spec = importlib.util.spec_from_file_location("cover_dead_panel_test", Path(si.REMOTE_TOOLS) / "cover_dead_panel.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame(tmp_path, paint):
    im = Image.new("RGB", (1920, 1080), (120, 170, 230))
    paint(ImageDraw.Draw(im), im)
    path = tmp_path / "frame.png"
    im.save(path)
    return str(path)


def test_a_window_that_shows_nothing_is_a_dead_panel_and_the_cover_is_not_made_from_it(tmp_path):
    tool = _tool()
    frame = _frame(tmp_path, lambda d, im: d.rectangle([0, 20, 620, 1060], fill=(0, 0, 0)))    # a black window on the left
    dead = tool.widest_dead_block(frame, None, RULES)
    assert dead >= RULES["dead_panel_max_frac"] and 0.3 <= dead <= 0.35
    assert tool.widest_dead_block(frame, [700, 0, 1220], RULES) < RULES["dead_panel_max_frac"]   # a crop beside it is fine
    bar = _frame(tmp_path, lambda d, im: d.rectangle([0, 0, 1919, 200], fill=(0, 0, 0)))        # a black bar on top
    assert tool.widest_dead_block(bar, None, RULES) >= RULES["dead_panel_max_frac"]


def test_a_dark_scene_is_not_a_dead_panel(tmp_path):
    tool = _tool()
    rng = random.Random(7)

    def night(draw, im):                       # a third of the pixels are black, scattered: no column is dark end to end
        px = im.load()
        for _ in range(1920 * 1080 // 3):
            px[rng.randrange(1920), rng.randrange(1080)] = (0, 0, 0)
        draw.ellipse([700, 200, 1200, 900], fill=(8, 8, 10))        # her black hood: a dark blob, not a panel
    assert tool.widest_dead_block(_frame(tmp_path, night), None, RULES) < RULES["dead_panel_max_frac"]


def test_the_cover_tool_asks_the_dead_panel_check_before_it_lays_out_a_screenshot():
    source = (Path(si.REMOTE_TOOLS) / "cover_candidates.py").read_text(encoding="utf-8")
    start = source.index("    def screenshot(self):")
    body = source[start:source.index("    def emote(self, emote):", start)]
    assert "dead_panel.widest_dead_block" in body and "SCREENSHOT_DEAD_PANEL" in body
    assert body.index("SCREENSHOT_DEAD_PANEL") < body.index('self.specs.append(("screenshot"')
