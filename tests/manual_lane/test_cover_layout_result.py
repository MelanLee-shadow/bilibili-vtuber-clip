"""The laid-out cover: the person's face is as tall as the target, the punchline has its floor size, the text stays off the
face (step 6, 70-cover; the approved collage was laid out from the measured face).

For a drawn sticker, a cut-out, an official emote and a screenshot, a face box measured in the material goes through the
real layout (cover_layout.place_solo / cover_face_crop.plan_crop + cover_text_fit) and the skill's real renderer; the
face and the punchline are then READ BACK from the finished picture (the face is a flat magenta box in the material, found
again by colour; the punchline's size is the renderer's own measurement of the lines it painted), never taken from the
numbers handed to the renderer.  Font: the private cover font when installed, otherwise any system CJK font (the numbers
asserted are the renderer's own measurements for whichever font paints them)."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.lane_rules import rules

COVER = {k: v for k, v in rules("cover").items() if not (k.startswith("_") or k.endswith("_source") or k == "source")}
TARGET = round(COVER["face_target_frac"] * 1080)
FLOOR = round(COVER["face_floor_frac"] * 1080)
MAGENTA = (255, 0, 255)
LINES = ["打针不会哭的人", "无趣的大人！"]
FONTS = [os.environ.get("LIDOUSHA_COVER_FONT") or "", str(Path.home() / ".local/share/fonts/lidousha/RuiZiZhenYan.ttf"),
         "/System/Library/Fonts/STHeiti Medium.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc",
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"]
FONT = next((f for f in FONTS if f and Path(f).is_file()), None)
needs_renderer = pytest.mark.skipif(not (FONT and si.COVER_RENDERER.is_file()), reason="no CJK font or skill renderer")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tools():
    remote = Path(si.REMOTE_TOOLS)
    renderer = _load(remote / "cover_fast_text.py", "layout_result_fast").fast_renderer(_load(si.COVER_RENDERER, "layout_result_renderer"))
    renderer.install_table(renderer.load_styles(si.COVER_STYLES))      # the tool's own renderer: fast flood fill, remembered lines
    return {"renderer": renderer, "layout": _load(remote / "cover_layout.py", "layout_result_layout"),
            "fit": _load(remote / "cover_text_fit.py", "layout_result_fit"),
            "crop": _load(remote / "cover_face_crop.py", "layout_result_crop")}


def _material(path, size, body, face, *, alpha=True):
    """A flat person: a grey body box with a magenta face box (the box the vision reading would give)."""
    im = Image.new("RGBA" if alpha else "RGB", size, (0, 0, 0, 0) if alpha else (0, 0, 0))
    draw = ImageDraw.Draw(im)
    draw.rectangle(body, fill=(190, 190, 215, 255) if alpha else (190, 190, 215))
    draw.rectangle(face, fill=MAGENTA + ((255,) if alpha else ()))
    im.save(path)
    return path


def _box_of_magenta(img):
    """(box, pixel count) of the exact magenta pixels in the picture."""
    r, g, b = img.convert("RGB").split()
    mask = ImageChops.multiply(ImageChops.multiply(r.point(lambda v: 255 * (v == 255)), g.point(lambda v: 255 * (v == 0))),
                               b.point(lambda v: 255 * (v == 255)))
    box = mask.getbbox()
    return (list(box), mask.histogram()[255]) if box else (None, 0)


def _render(tools, spec, ink, style, plan_face_top):
    """The renderer's own picture and record for `spec`, after the text plan the lane makes before rendering."""
    fit, renderer = tools["fit"], tools["renderer"]
    text_plan = fit.plan_text(ink, len(spec["lines"]), plan_face_top, style, COVER)
    fit.apply_plan(spec, text_plan)
    sha = hashlib.sha256(b"layout-result").hexdigest()
    img, rec, _ = renderer.render(spec, FONT, recent_style_ids=(), content_sha256=sha)
    return img, rec, text_plan


def _ink(tools):
    renderer, fit = tools["renderer"], tools["fit"]
    sha = hashlib.sha256(b"layout-result").hexdigest()
    style = fit.probe_style(renderer, sha, ())
    ink = fit.renderer_ink(renderer, FONT, [{"text": t} for t in LINES], style)
    return style, ink, fit.need_face_top(ink, 2, COVER, COVER["punch_floor_px"])


def _check(img, rec, planned_face, plan_face_px, limited_by, *, tol):
    """What the finished picture shows: the face where and as tall as planned, the target met or the shortfall recorded,
    the punchline at its floor, nothing painted over the face."""
    box, painted = _box_of_magenta(img)
    assert box is not None, "the face is gone from the picture"
    assert all(abs(a - b) <= tol for a, b in zip(box, planned_face, strict=True)), (box, planned_face)
    area = (box[2] - box[0]) * (box[3] - box[1])
    assert painted >= 0.97 * area, "text or outline was painted over the face"
    shown = box[3] - box[1]
    assert shown >= FLOOR, "the face is under the veto floor"
    assert abs(shown - plan_face_px) <= tol
    assert shown >= 0.95 * TARGET or limited_by, (shown, limited_by)       # the target, or the recorded reason it is not
    return box, shown


@needs_renderer
@pytest.mark.parametrize("kind", ["sticker", "cutout", "emote"])
def test_a_person_picture_is_laid_out_by_its_measured_face(tmp_path, tools, kind):
    layout, renderer = tools["layout"], tools["renderer"]
    style, ink, need = _ink(tools)
    assert need is not None and need < 700
    if kind == "sticker":                                  # a drawn chibi: face in the upper half of a tall figure
        src = _material(tmp_path / "s.png", (700, 1100), (60, 20, 640, 1080), (200, 330, 500, 640))
        layer, face = {"src": str(src), "cutout": "auto"}, [200, 330, 500, 640]
    elif kind == "cutout":                                 # two people in one cut-out: the host is the right one, the crop picks her
        src = _material(tmp_path / "c.png", (1500, 1000), (800, 40, 1450, 990), (980, 300, 1280, 600))
        ImageDraw.Draw(Image.open(src)).rectangle((50, 40, 500, 990))
        layer, face = {"src": str(src), "cutout": "auto", "crop": [760, 0, 1500, 1000]}, [980, 300, 1280, 600]
    else:                                                  # an official emote: a black-backed picture and its small transparent mask
        _material(tmp_path / "hd.png", (1254, 1254), (150, 100, 1100, 1240), (330, 420, 920, 900), alpha=False)
        small = _material(tmp_path / "small.png", (162, 162), (19, 13, 142, 160), (43, 54, 119, 116))
        src = tmp_path / "hd.png"
        layer, face = {"src": str(src), "cutout": f"mask:{small}", "outline": 12}, [330, 420, 920, 900]
    sticker, box, plan = layout.place_solo(layer, face, renderer, COVER, need, "right")
    spec = {"bg": ["#dff0ff", "#ffe3f1"], "stickers": [sticker], "layout": "top", "lines": [{"text": t} for t in LINES],
            "face": box, "keep": []}
    assert plan["need_top"] == need and box[1] >= need                                 # room above the face for the punchline
    img, rec, text_plan = _render(tools, spec, ink, style, box[1])
    shown_box, shown = _check(img, rec, box, plan["face_px"], plan["limited_by"], tol=12 if kind == "emote" else 4)
    assert text_plan["floor_met"] and int(rec[-1]["size"]) >= COVER["punch_floor_px"], rec[-1]
    assert shown_box[1] >= need - 4
    assert 1100 - 80 < (shown_box[0] + shown_box[2]) / 2 < 1100 + 80                  # centred a little right of the middle (1100 px)
    assert sticker["y"] >= 0 and sticker["x"] >= 0                                    # nothing leaves through the top or the left


@needs_renderer
def test_a_head_that_leaves_no_room_records_the_face_it_really_got_and_still_clears_the_floor(tmp_path, tools):
    layout, renderer = tools["layout"], tools["renderer"]
    style, ink, need = _ink(tools)
    # long hair above a face low in a short figure: the face cannot reach the target without leaving through the top
    src = _material(tmp_path / "h.png", (700, 700), (60, 10, 640, 690), (230, 380, 470, 560))
    sticker, box, plan = layout.place_solo({"src": str(src), "cutout": "auto"}, [230, 380, 470, 560], renderer, COVER, need, "right")
    assert plan["limited_by"] in ("HEAD_ABOVE_FACE", "FRAME_BOTTOM", "PUNCH_TEXT_ZONE")
    assert plan["face_px"] < TARGET * 0.95 and plan["met"]["face_floor"]
    spec = {"bg": ["#dff0ff", "#ffe3f1"], "stickers": [sticker], "layout": "top", "lines": [{"text": t} for t in LINES],
            "face": box, "keep": []}
    img, rec, _ = _render(tools, spec, ink, style, box[1])
    _check(img, rec, box, plan["face_px"], plan["limited_by"], tol=4)


@needs_renderer
def test_a_screenshot_is_cropped_by_its_measured_face_and_the_text_stays_above_it(tmp_path, tools):
    crop_mod, renderer = tools["crop"], tools["renderer"]
    style, ink, need = _ink(tools)
    frame = _material(tmp_path / "f.png", (1920, 1080), (0, 0, 1920, 1080), (1300, 330, 1500, 530), alpha=False)
    plan = crop_mod.plan_crop([1300, 330, 1500, 530], 250, [], COVER, need)
    face = crop_mod.frame_face([1300, 330, 1500, 530], plan["crop"])
    spec = {"frame": str(frame), "crop": plan["crop"], "layout": "top", "lines": [{"text": t} for t in LINES], "face": face,
            "keep": [], "blur": []}
    img, rec, text_plan = _render(tools, spec, ink, style, face[1])
    shown_box, _ = _check(img, rec, face, plan["face_px"], plan["limited_by"], tol=4)
    assert text_plan["floor_met"] and int(rec[-1]["size"]) >= COVER["punch_floor_px"], (rec[-1], plan)
    assert max(ln["y"] + ln["h"] for ln in rec) <= shown_box[1] + 4                   # every line ends above the face


BODY = (190, 190, 215)


def _body_top(img, column):
    """The first row where the figure's own flat grey body colour shows in `column` (the cut edge, read from the picture)."""
    px = img.convert("RGB").load()
    return next((y for y in range(img.height) if px[column, y] == BODY), None)


@needs_renderer
def test_a_figure_whose_head_is_cut_by_its_source_picture_records_where_the_flat_cut_lands(tmp_path, tools):
    layout, renderer = tools["layout"], tools["renderer"]
    style, ink, need = _ink(tools)
    # the figure runs into the source picture's own top edge (a head cut by the live frame): the cut cannot leave the canvas
    src = _material(tmp_path / "cut.png", (700, 1100), (60, 0, 640, 1100), (200, 330, 500, 640))
    sticker, box, plan = layout.place_solo({"src": str(src), "cutout": "auto"}, [200, 330, 500, 640], renderer, COVER, need, "right")
    assert plan["head_cut"] == "INSIDE_CANVAS" and plan["cut_edge_y"] > 0          # recorded, not hidden
    spec = {"bg": ["#dff0ff", "#ffe3f1"], "stickers": [sticker], "layout": "top", "lines": [{"text": t} for t in LINES],
            "face": box, "keep": []}
    img, _, _ = _render(tools, spec, ink, style, box[1])
    seen = _body_top(img, round((box[0] + box[2]) / 2))
    assert seen is not None and abs(seen - plan["cut_edge_y"]) <= 3                  # the recorded edge is where the picture shows it
    # a figure whose top is its own (the alpha begins below the picture's edge) is not a cut
    whole = _material(tmp_path / "whole.png", (700, 1100), (60, 60, 640, 1040), (200, 330, 500, 640))
    assert "head_cut" not in layout.place_solo({"src": str(whole), "cutout": "auto"}, [200, 330, 500, 640], renderer, COVER, need,
                                               "right")[2]


@needs_renderer
def test_a_cut_head_is_put_on_the_canvas_edge_when_the_target_face_and_the_text_room_allow_it(tmp_path, tools):
    layout, renderer = tools["layout"], tools["renderer"]
    # the face lies low enough in the figure that, at the target face height, its top already clears the punchline's room
    src = _material(tmp_path / "bleed.png", (700, 1100), (60, 0, 640, 1100), (200, 440, 500, 860))
    sticker, box, plan = layout.place_solo({"src": str(src), "cutout": "auto"}, [200, 440, 500, 860], renderer, COVER, 480, "right")
    assert plan["head_cut"] == "AT_CANVAS_TOP" and plan["cut_edge_y"] == 0
    assert sticker["outline"] == 0 and sticker["y"] == 0                                # no outline band above the cut
    assert abs(box[3] - box[1] - TARGET) <= 6 and box[1] >= 480
    spec = {"bg": ["#dff0ff", "#ffe3f1"], "stickers": [sticker], "lines": [{"text": "短"}], "face": box, "keep": [], "layout": "top"}
    img, _ = renderer.render(spec, FONT, recent_style_ids=(), content_sha256=hashlib.sha256(b"edge").hexdigest())[:2]
    assert _body_top(img, round((box[0] + box[2]) / 2)) == 0                            # the flat cut is the canvas edge itself
