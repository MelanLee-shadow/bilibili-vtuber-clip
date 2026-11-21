"""A screenshot with the words in a column beside her, on a colour panel (assets/manual_lane/remote/cover_side.py).

Owner the covers of one stream were the same picture, one line top left and one line lower right, in one
colour.  The screenshot route had one composition; now it has the column's too, the panel takes the lane's background
colours in turn, and a cover that looks like one the stream already has goes behind one that does not."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.autoslice.cover_style_rotation import REPO_TABLE
from src.autoslice.manual_lane import cover_column
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.lane_rules import rules

COVER = {k: v for k, v in rules("cover").items() if not (k.startswith("_") or k.endswith("_source") or k == "source")}
TABLE = json.loads(Path(REPO_TABLE).read_text(encoding="utf-8"))
SKY = (100, 160, 225)
FACE, HEAD_TOP = [800, 440, 1190, 690], 0          # the stream of her head touches the top of the frame
ROWS = [{"text": "摄像头", "punch": False}, {"text": "认出kmx", "punch": False},
        {"text": "永远", "punch": True}, {"text": "看着你", "punch": True}]


def _load(name):
    sys.path.insert(0, str(si.REMOTE_TOOLS))          # the tool's sibling modules, as on the media host
    spec = importlib.util.spec_from_file_location(f"tested_{name}", Path(si.REMOTE_TOOLS) / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


side = _load("cover_side")
background = _load("cover_background")
layout = _load("cover_layout")
STUB = SimpleNamespace(H=1080, MIN_CONTRAST=TABLE["contrast"]["min_distance"], STROKES=TABLE["strokes"],
                       PAL={n: (tuple(p["top"]), tuple(p["bottom"])) for n, p in TABLE["palette"].items()},
                       default_content_sha256=lambda spec: hashlib.sha256(b"x").hexdigest())


@pytest.mark.parametrize("words", ["left", "right"])
def test_the_window_puts_her_on_the_other_side_and_leaves_the_column_its_room(words):
    made, why = side.plan(FACE, HEAD_TOP, [], COVER, words, 650)
    assert why is None
    x0, y0, w = made["crop"]
    assert 0 <= x0 and x0 + w <= 1920 and y0 == 0 and y0 + round(w * 9 / 16) >= FACE[3]      # inside the frame, head and chin in
    centre = (made["face"][0] + made["face"][2]) / 2
    assert centre > 1920 * 0.6 if words == "left" else centre < 1920 * 0.4                  # she stands away from the words
    assert made["zone_px"] >= 650 and made["face_px"] >= COVER["face_floor_frac"] * 1080


@pytest.mark.parametrize("face, head_top, keep, row_px, why", [
    (FACE, None, [], 650, "HEAD_TOP_UNMEASURED"),
    (FACE, HEAD_TOP, [], 1200, "COLUMN_NO_ROOM"),                       # a punchline row wider than the room beside her
    ([900, 700, 1000, 760], 0, [], 300, "COLUMN_FACE_UNDER_FLOOR"),     # a small face far under her head top: the window would shrink it
    (FACE, HEAD_TOP, [[1700, 100, 1900, 300]], 650, "COLUMN_KEEPS_OUTSIDE"),    # a kept box would leave the window
])
def test_a_frame_without_such_a_window_says_why(face, head_top, keep, row_px, why):
    assert side.plan(face, head_top, keep, COVER, "left", row_px) == (None, why)


def _run(job_extra):
    job = {"text_layout": None, "column_rows": [], **job_extra}
    run = SimpleNamespace(job=job, rules=COVER, plans={}, column=layout.Column(job, COVER))
    run.text_ink = lambda spec: ({"style_id": "s"}, lambda i, size: (size * len(spec["lines"][i]["text"]), size))
    return run


def _spec():
    return {"id": "screenshot", "frame": "frame.png", "crop": [0, 0, 1920], "layout": "auto",
            "lines": [{"text": "摄像头认出kmx"}, {"text": "永远看着你"}], "keep": [], "blur": [[0, 300, 300, 800]]}


def test_at_the_columns_turn_the_screenshot_takes_the_rows_and_asks_for_the_panel(monkeypatch):
    monkeypatch.setattr(side.text_fit, "need_face_top", lambda *a, **k: 500)
    run, spec = _run({"text_layout": "right", "column_rows": ROWS}), _spec()
    side.solo(run, spec, FACE, HEAD_TOP, SimpleNamespace(phase=lambda name: _Quiet()))
    assert spec["layout"] == "right" and [r["text"] for r in spec["lines"]] == [r["text"] for r in ROWS]
    assert spec["panel"]["side"] == "right" and spec["panel"]["edge"] > run.plans["screenshot"]["face"][2]
    assert run.plans["screenshot"]["text_layout"] == "right" and "screenshot" in run.column.placed
    assert "底板" in side.note(spec)
    # not its turn: the two lines on top, as before, and no panel
    run, spec = _run({}), _spec()
    side.solo(run, spec, FACE, HEAD_TOP, SimpleNamespace(phase=lambda name: _Quiet()))
    assert spec["layout"] == "auto" and len(spec["lines"]) == 2 and "panel" not in spec
    assert "text_top_wanted" in run.plans["screenshot"] and "底板" not in side.note(spec)


class _Quiet:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_column_refused_once_is_not_tried_again(monkeypatch):
    monkeypatch.setattr(side.text_fit, "need_face_top", lambda *a, **k: 500)
    run, spec = _run({"text_layout": "left", "column_rows": ROWS}), _spec()
    run.column.refused["screenshot"] = {"reason": "RENDER_REFUSED"}        # the renderer refused the column (cover_candidates)
    side.solo(run, spec, FACE, HEAD_TOP, SimpleNamespace(phase=lambda name: _Quiet()))
    assert "panel" not in spec and len(spec["lines"]) == 2


def _painted(tmp_path, words, job_extra=None, sha="0"):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (1920, 1080), SKY).save(frame)
    made, _ = side.plan(FACE, HEAD_TOP, [], COVER, words, 650)
    spec = {"id": "screenshot", "frame": str(frame), "crop": made["crop"], "layout": words, "lines": ROWS, "face": made["face"],
            "blur": [], "panel": {"side": words, "edge": side.panel_edge(words, made["face"], COVER)}}
    job = {"rules": COVER, "out": str(tmp_path), "content_sha256": hashlib.sha256(sha.encode()).hexdigest(), **(job_extra or {})}
    style = next(s for s in TABLE["styles"] if s["style_id"] == "s3-blue-pink-t2-r85-double")
    record = background.choose(STUB, spec, style, "screenshot", job)
    return spec, record, made


@pytest.mark.parametrize("words", ["left", "right"])
def test_the_panel_colours_the_words_side_and_leaves_her_face_and_the_crop(tmp_path, words):
    spec, record, made = _painted(tmp_path, words)
    # the crop is what it was (the face is measured through it; a repainted crop made the face read small)
    assert "panel" not in spec and spec["crop"] == made["crop"] and spec["composition"] == "picture_panel"
    picture = Image.open(spec["frame"]).convert("RGB")
    assert picture.size == (1920, 1080)                                    # the frame's own size
    x0, y0, w = made["crop"]
    to_frame = lambda x, y: (round(x0 + x * w / 1920), round(y0 + y * w / 1920))      # a cover pixel in the frame
    fx0, fy0, fx1, fy1 = made["face"]
    assert all(picture.getpixel(to_frame(x, y)) == SKY for x in (fx0, fx1 - 1) for y in (fy0, fy1 - 1))     # her face is the frame's
    words_x = 400 if words == "left" else 1520
    assert picture.getpixel(to_frame(words_x, 30)) != SKY and picture.getpixel(to_frame(words_x, 1050)) != SKY   # the panel
    outside = (max(0, x0 - 5), 5) if x0 >= 5 else (min(1919, x0 + w + 5), 5)
    assert picture.getpixel(outside) == SKY                                 # nothing is painted outside the window
    assert record["id"] in {e["id"] for e in COVER["backgrounds"]} and record["chosen"]["ok"]
    assert record["panel"]["side"] == words


def test_a_blur_box_under_the_panel_is_dropped_and_one_beside_her_stays(tmp_path):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (1920, 1080), SKY).save(frame)
    made, _ = side.plan(FACE, HEAD_TOP, [], COVER, "left", 650)
    edge = side.panel_edge("left", made["face"], COVER)
    spec = {"id": "s", "frame": str(frame), "crop": made["crop"], "layout": "left", "lines": ROWS, "face": made["face"],
            "blur": [[0, 300, 200, 800], [1700, 0, 1920, 200]], "panel": {"side": "left", "edge": edge}}
    side.paint(str(frame), spec, ["#7b4fd6", "#d36cc8"], COVER, str(tmp_path / "out.png"))
    assert spec["blur"] == [[1700, 0, 1920, 200]]


def test_the_panel_takes_a_colour_the_stream_has_not_used(tmp_path):
    first = _painted(tmp_path, "left")[1]["id"]
    seen = {first}
    for n in range(4):                       # each next cover of the same stream, the same content hash on purpose
        record = _painted(tmp_path, "left", {"recent_background_ids": sorted(seen), "session_background_ids": sorted(seen)})[1]
        assert record["id"] not in seen
        seen.add(record["id"])


def test_the_layout_turn_skips_what_this_stream_already_used():
    # the last two covers took left and right; an older cover of this same stream took the top
    assert cover_column.next_layout(["top", "left", "right"], COVER)[0] == "top"
    assert cover_column.next_layout(["left", "right"], COVER, ["top"])[0] == "top"            # nothing else is free: the oldest
    assert cover_column.next_layout(["top"], COVER, ["top"])[0] == "left"
    assert cover_column.next_layout(["x", "top"], COVER, ["top", "left"])[0] == "right"       # left was this stream's too
    assert "本场用过" in cover_column.next_layout(["top"], COVER, ["top"])[1]


def test_five_covers_of_one_stream_take_five_looks(monkeypatch):
    """top, a column on a panel on each side, then the columns again on the picture itself."""
    monkeypatch.setattr(side.text_fit, "need_face_top", lambda *a, **k: 500)
    layouts, looks = [], []
    for _ in range(5):
        turn = cover_column.next_layout(layouts, COVER, layouts)[0]
        run, spec = _run({"text_layout": turn if turn != "top" else None, "column_rows": ROWS if turn != "top" else [],
                          "session_compositions": [list(look) for look in looks]}), _spec()
        side.solo(run, spec, FACE, HEAD_TOP, SimpleNamespace(phase=lambda name: _Quiet()))
        layouts.append(turn)
        looks.append(("screenshot", turn, "picture_panel" if "panel" in spec else "picture"))
    assert looks == [("screenshot", "top", "picture"), ("screenshot", "left", "picture_panel"),
                     ("screenshot", "right", "picture_panel"), ("screenshot", "left", "picture"),
                     ("screenshot", "right", "picture")]
    assert len(set(looks)) == 5


def test_the_tool_ships_the_module_and_the_judge_is_told_what_a_panel_is():
    from src.autoslice.manual_lane import step6_cover
    source6 = Path(step6_cover.__file__).read_text(encoding="utf-8")
    assert "cover_side.py" in source6
    # a clip about the audience offers the fans' sticker to the emote route; that must not take the
    # screenshot's column turn away (the emote collage keeps its own two lines)
    assert 'not plan.get("fans_emote") else None' not in source6
    judge = (Path(si.REMOTE_TOOLS).parent / "prompts" / "cover_judge.md").read_text(encoding="utf-8")
    assert "底板" in judge
    # the thumbnail reader took "the last line" for the punchline and failed a column whose punchline
    # stood in two rows ("永远" / "看着你"), so a paid picture replaced the best free cover
    thumb = (Path(si.REMOTE_TOOLS).parent / "prompts" / "cover_thumb.md").read_text(encoding="utf-8")
    assert "最后一行" not in thumb and "拆成" in thumb
    source = (Path(si.REMOTE_TOOLS) / "cover_candidates.py").read_text(encoding="utf-8")
    assert "side_shot.solo(self, spec, hf, head_top, clock)" in source and "side_shot.note(spec)" in source


def _row(route, total, *, repeat=False):
    return {"id": route, "route": route, "repeat": repeat, "face_status": "MEASURED", "face_px": 400,
            "score": {"status": "ELIGIBLE", "total": total, "C": 2}}


class _Cover:
    def __init__(self, rows, failing=()):
        self.log, self.thumbs, self.failing = SimpleNamespace(rows=rows), {}, set(failing)

    def thumb(self, cid, lines, identity):
        self.thumbs[cid] = {"status": "FAIL" if cid in self.failing else "PASS"}
        return self.thumbs[cid]


def test_a_cover_the_stream_already_has_goes_behind_one_it_does_not():
    from src.autoslice.manual_lane.step6_cover_round import pick_free

    # a fresh screenshot still goes before the sticker (owner 11-18)
    assert pick_free(_Cover([_row("emote", 15), _row("screenshot", 11)]), ["a", "b"], "id").chosen == "screenshot"
    # the screenshot repeats a cover of this stream: the sticker, which does not, is taken
    assert pick_free(_Cover([_row("emote", 12), _row("screenshot", 13, repeat=True)]), ["a", "b"], "id").chosen == "emote"
    # the fresh one's thumbnail fails: the repeat is still better than nothing free
    picked = pick_free(_Cover([_row("emote", 12), _row("screenshot", 13, repeat=True)], failing={"emote"}), ["a", "b"], "id")
    assert picked.chosen == "screenshot"


def test_a_revision_keeps_the_title_an_in_place_edit_gave_the_manuscript(tmp_path):
    """a cover revision carried the title the edit had just replaced, and the review rewrote it again."""
    from src.autoslice.manual_lane import revise_run
    from src.autoslice.manual_lane.state import ClipState

    task = tmp_path / "task"
    (task / "clips/x/upload").mkdir(parents=True)
    (task / "clips/x/upload/online.json").write_text(json.dumps({"title": "【李豆沙】旧标题"}), encoding="utf-8")
    (task / "clips/x_r2").mkdir(parents=True)
    state = SimpleNamespace(data={"revised_from": {"cid": "x"}, "clip": {"cid": "x_r2"}})
    assert revise_run.title_online(task / "clips/x_r2", state) == "【李豆沙】旧标题"                 # the lane's own record
    assert revise_run.title_online(task / "clips/x_r2", state, "【李豆沙】改过的标题") == "【李豆沙】改过的标题"
    first = SimpleNamespace(data={"clip": {"cid": "x"}})
    assert revise_run.title_online(task / "clips/x", first, "【李豆沙】改过的标题") is None             # not a revision
    assert revise_run.keeps_title(state) is True and revise_run.keeps_title(first) is False
    assert ClipState is not None


def test_the_registry_row_gives_the_edited_title_only_when_bound_to_an_edit(monkeypatch):
    from src.autoslice.manual_lane import title_history

    rows = iter([{"status": "published", "bvid": "BV1", "reconciliation_status": "VERIFIED_SAME_BV_METADATA",
                  "reconciliation_title": "【李豆沙】改过的标题"},
                 {"status": "published", "bvid": "BV1", "reconciliation_status": "VERIFIED_PUBLIC",
                  "reconciliation_title": "【李豆沙】首发标题"}, None])
    monkeypatch.setattr(title_history, "published_row", lambda ctx, manuscript: next(rows))
    assert title_history.edited_title(None, "x") == "【李豆沙】改过的标题"
    assert title_history.edited_title(None, "x") is None            # a first publication: the lane's own record stands
    assert title_history.edited_title(None, "x") is None            # not published


def test_the_review_does_not_retitle_a_revision_of_a_published_manuscript(tmp_path):
    from src.autoslice.manual_lane import stage_package

    clip = tmp_path / "clips/x_r2"
    (clip / "title").mkdir(parents=True)
    (clip / "title/title.txt").write_text("【李豆沙】线上的标题\n", encoding="utf-8")
    ctx = SimpleNamespace(clip_dir=clip, session={}, state=SimpleNamespace(data={"revised_from": {"cid": "x"}, "clip": {"cid": "x_r2"}}))
    skipped: list[str] = []
    assert stage_package._title_fix(ctx, "【李豆沙】复审想换的标题", skipped, []) is False
    assert (clip / "title/title.txt").read_text(encoding="utf-8").strip() == "【李豆沙】线上的标题"
    assert any("a revision keeps the published title" in line for line in skipped)
