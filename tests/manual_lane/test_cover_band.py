"""cover_band: a screenshot whose punchline has no room above the faces is shrunk onto the bottom edge with the same picture
blurred above it, instead of being dropped for a drawn picture (step 6, 70-cover: 字放不下先换版式，不直接转 AI)."""
from __future__ import annotations

import pytest
from PIL import Image, ImageDraw
from test_cover_layout_result import COVER, FONT, LINES, _box_of_magenta, _ink, _load, needs_renderer, tools  # noqa: F401

from src.autoslice.manual_lane import stage_impl as si

band = _load(f"{si.REMOTE_TOOLS}/cover_band.py", "cover_band_test")


def test_the_scale_brings_the_face_top_down_to_where_the_text_needs_it():
    scale = band.scale_for(100, 400)
    assert scale == round((1080 - 400) / (1080 - 100), 4)
    assert band.moved([0, 100, 1920, 1080], scale)[1] == 400 and band.moved([0, 100, 1920, 1080], scale)[3] == 1080
    x0, _, x1, _ = band.moved([0, 0, 1920, 1080], scale)
    assert (x0 + x1) / 2 == 960                                  # centred


@pytest.mark.parametrize("face_top, need", [(500, 400), (100, None), (100, 800)])
def test_no_band_when_none_is_needed_or_it_would_shrink_the_picture_too_far(face_top, need):
    assert band.scale_for(face_top, need) is None                 # room already / text cannot fit at any size / under 0.6


def test_moved_any_keeps_the_shape_of_what_it_is_given():
    assert band.moved_any(None, 0.8) is None and band.moved_any([], 0.8) == []
    assert band.moved_any([[0, 0, 10, 10]], 0.8) == [band.moved([0, 0, 10, 10], 0.8)]


def _frame(path):
    im = Image.new("RGB", (1920, 1080), (40, 90, 60))
    draw = ImageDraw.Draw(im)
    draw.rectangle((200, 60, 1720, 1080), fill=(190, 190, 215))      # two people side by side filling the frame
    draw.rectangle((760, 260, 1160, 640), fill=(255, 0, 255))        # her face high, the people kept: the punchline needs it lower (~570 px)
    im.save(path)
    return {"frame": str(path), "crop": [0, 0, 1920], "face": [760, 260, 1160, 640], "keep": [[200, 200, 1720, 1080]], "blur": [],
            "layout": "top", "lines": [{"text": t} for t in LINES]}


@needs_renderer
def test_a_screenshot_the_old_layout_refused_is_rendered_banded_with_the_text_off_the_face(tmp_path, tools):
    fit, renderer = tools["fit"], tools["renderer"]
    style, ink, need = _ink(tools)
    spec = _frame(tmp_path / "f.png")
    refused = dict(spec)
    fit.apply_plan(refused, fit.plan_text(ink, 2, refused["face"][1], style, COVER))
    with pytest.raises(SystemExit):                              # the old path: no room for the punchline -> the candidate was lost
        renderer.render(refused, FONT, recent_style_ids=(), content_sha256="0" * 64)
    banded = band.banded_spec(spec, need, str(tmp_path / "band.png"))
    assert banded and banded["band"]["face_top_before"] == 260 and banded["face"][1] == need
    fit.apply_plan(banded, fit.plan_text(ink, 2, banded["face"][1], style, COVER))
    img, rec, _ = renderer.render(banded, FONT, recent_style_ids=(), content_sha256="0" * 64)
    assert int(rec[-1]["size"]) >= COVER["punch_floor_px"]
    box, painted = _box_of_magenta(img)
    assert box and all(abs(a - b) <= 4 for a, b in zip(box, banded["face"], strict=True)), (box, banded["face"])
    assert painted >= 0.97 * (box[2] - box[0]) * (box[3] - box[1])      # nothing painted over the face
    assert band.banded_spec(banded, need, str(tmp_path / "again.png")) is None   # never banded twice


def test_the_shrunk_picture_fades_into_the_blur_at_its_top_and_sides_and_stands_on_the_bottom():
    mask = band.feather_mask((1000, 600))
    assert mask.getpixel((500, 300)) == 255 and mask.getpixel((500, 599)) == 255       # opaque inside and on the bottom edge
    assert mask.getpixel((0, 300)) < 40 and mask.getpixel((999, 300)) < 40 and mask.getpixel((500, 0)) < 40   # no hard edge
