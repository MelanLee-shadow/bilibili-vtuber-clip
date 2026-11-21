"""The official emote as a clean layer (assets/manual_lane/remote/cover_emote_layer.py): the HD picture's own edge instead
of the small original's alpha stretched over it, and no straight edge where the original is cut by its frame.
19 of 34 emote covers were vetoed for backdrop left inside the mask or a flat cut inside the canvas."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.lane_rules import rules

pytest.importorskip("PIL")
from PIL import Image, ImageDraw

RULES = rules("cover")
SIZE = 400


def _tool():
    spec = importlib.util.spec_from_file_location("cover_emote_layer_test", Path(si.REMOTE_TOOLS) / "cover_emote_layer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _layout():
    spec = importlib.util.spec_from_file_location("cover_layout_emote_test", Path(si.REMOTE_TOOLS) / "cover_layout.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pair(tmp_path, figure, mask, *, backdrop=(0, 0, 0), paint=None):
    """An HD picture (flat backdrop, a white-outlined grey figure in box `figure`) and its small original whose opaque
    box is `mask` (both in HD pixels).  `paint(draw)` adds to the HD picture."""
    hd = Image.new("RGB", (SIZE, SIZE), backdrop)
    draw = ImageDraw.Draw(hd)
    draw.rectangle(figure, fill=(255, 255, 255))
    draw.rectangle([figure[0] + 8, figure[1] + 8, figure[2] - 8, figure[3] - 8], fill=(150, 160, 200))
    if paint:
        paint(draw)
    small = Image.new("RGBA", (SIZE // 4, SIZE // 4), (0, 0, 0, 0))
    ImageDraw.Draw(small).rectangle([v // 4 for v in mask], fill=(150, 160, 200, 255))
    hd.save(tmp_path / "hd.png")
    small.save(tmp_path / "small.png")
    return str(tmp_path / "hd.png"), str(tmp_path / "small.png")


def _alpha(path):
    return Image.open(path).getchannel("A")


def test_backdrop_the_original_mask_would_have_kept_is_removed_at_the_redraws_own_edge(tmp_path):
    tool = _tool()
    # the redraw's figure is smaller than the original: the stretched mask kept a band of backdrop around it
    hd, small = _pair(tmp_path, figure=[110, 108, 330, 399], mask=[100, 100, 340, 399])
    path, side, facts = tool.layer_for(hd, small, str(tmp_path), RULES, True)
    alpha = _alpha(path)
    assert alpha.getpixel((104, 250)) == 0 and alpha.getpixel((336, 250)) == 0       # the band is gone
    assert alpha.getpixel((220, 250)) == 255 and facts["cut_sides"] == {} and side is None   # whole; nothing is cut
    assert tool.layer_for(hd, small, str(tmp_path), RULES)[2]["reused"] is True      # worked out once for the same pictures


def test_a_figure_cut_by_its_own_frame_fades_out_when_it_cannot_go_to_the_canvas_edge(tmp_path):
    tool = _tool()
    hd, small = _pair(tmp_path, figure=[160, 120, 399, 399], mask=[160, 120, 399, 399])      # runs into the right frame
    path, side, facts = tool.layer_for(hd, small, str(tmp_path), RULES)          # another layer stands beside it
    alpha = _alpha(path)
    assert set(facts["cut_sides"]) == {"R"} and side is None
    assert alpha.getpixel((SIZE - 1, 250)) == 0                                       # nothing at the frame itself
    inside = round(SIZE * RULES["emote_fade_frac"])
    assert 0 < alpha.getpixel((SIZE - 1 - inside // 2, 250)) < 255                    # a ramp, not a line
    assert alpha.getpixel((SIZE - 1 - inside - 4, 250)) == 255 and alpha.getpixel((200, 398)) == 255   # the bottom is left


def test_a_flood_that_eats_into_the_figure_is_dropped_for_a_stricter_one(tmp_path):
    tool = _tool()
    # the figure's own colour also lies outside the original (the redraw spills past it): counted as backdrop it would
    # flood the figure away through the gap in its outline
    def spill(draw):
        draw.rectangle([20, 20, 80, 380], fill=(150, 160, 200))                      # > 4 % of the outside, < 15 %
        draw.rectangle([80, 200, 150, 215], fill=(150, 160, 200))                    # joins it to the figure's inside
    hd, small = _pair(tmp_path, figure=[140, 140, 300, 399], mask=[130, 130, 310, 399], paint=spill)
    path, _, facts = tool.layer_for(hd, small, str(tmp_path), RULES)
    assert len(facts["tried"]) > 1 and facts["tried"][0]["figure_frac"] < facts["tried"][-1]["figure_frac"]
    assert _alpha(path).getpixel((220, 250)) == 255                                   # the figure is whole


def test_a_picture_with_no_flat_backdrop_keeps_the_originals_alpha(tmp_path):
    tool = _tool()
    def noise(draw):
        for i in range(SIZE):                                                         # no colour holds a share of the outside
            draw.line([(0, i), (SIZE, i)], fill=((i * 37) % 256, (i * 91) % 256, (i * 151) % 256))
        draw.rectangle([140, 140, 300, 399], fill=(150, 160, 200))
    hd, small = _pair(tmp_path, figure=[140, 140, 300, 399], mask=[140, 140, 300, 399], paint=noise)
    path, _, facts = tool.layer_for(hd, small, str(tmp_path), RULES)
    alpha = _alpha(path)
    assert "backdrop" in facts and alpha.getpixel((220, 250)) == 255 and alpha.getpixel((40, 40)) == 0


def test_a_real_side_cut_goes_to_the_canvas_edge_and_is_not_faded(tmp_path):
    # emote 05 : the ear the frame cuts off, faded inside the canvas, was vetoed by three readings of three
    tool = _tool()
    hd, small = _pair(tmp_path, figure=[160, 120, 399, 399], mask=[160, 120, 399, 399])
    path, side, _ = tool.layer_for(hd, small, str(tmp_path), RULES, True)         # alone on the cover
    assert side == "R" and _alpha(path).getpixel((SIZE - 1, 250)) == 255          # the cut stays sharp: the canvas hides it
    assert tool.flush_side({"L": 0.9, "R": 0.5, "T": 0.95}, RULES) == "L"         # the most cut lateral side; never the top
    assert tool.flush_side({"L": RULES["emote_flush_min_frac"] - 0.01}, RULES) is None      # a strand is faded, not moved

    layout = _layout()
    renderer = type("R", (), {"cutout": staticmethod(lambda raw, mode: raw.convert("RGBA"))})
    face = [220, 200, 330, 300]
    for flush, edge in (("R", 1920), ("L", 0)):
        sticker, box, plan = layout.place_solo({"src": path, "cutout": "alpha", "flush": flush}, face, renderer, RULES, None, "right")
        left = sticker["x"]
        right = left + plan["width"]
        assert (right if flush == "R" else left) == edge and sticker["outline"] == 0     # no outline band between cut and edge
        assert plan["side_cut"] == {"side": flush, "at": "CANVAS_EDGE"} and 0 <= box[0] < box[2] <= 1920
