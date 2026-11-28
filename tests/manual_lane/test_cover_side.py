"""A screenshot framed by the cover's composition template (assets/manual_lane/remote/cover_side.py).

Owner and the covers of one stream were the same picture, one line top left and one line lower
right, in one colour.  The screenshot route is framed by the template the run tries (a column or a close-up beside the
words on a colour panel, the words on top, diagonal, a bottom banner); the panel takes the lane's background colours in
turn, and a cover that repeats the covers before it ranks behind one that does not (it still passes the free stage
when nothing else does: U138)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.autoslice import cover_layout_rotation
from src.autoslice.cover_style_rotation import REPO_TABLE
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.lane_rules import rules
from src.autoslice.manual_lane.step6_cover import cover_layout_table

COVER = {k: v for k, v in rules("cover").items() if not (k.startswith("_") or k.endswith("_source") or k == "source")}
TABLE = json.loads(Path(REPO_TABLE).read_text(encoding="utf-8"))
SKY = (100, 160, 225)
FACE, HEAD_TOP = [800, 440, 1190, 690], 0          # the stream of her head touches the top of the frame
ROWS = [{"text": "摄像头", "punch": False}, {"text": "认出kmx", "punch": False},
        {"text": "永远", "punch": True}, {"text": "看着你", "punch": True}]
LAYOUTS = cover_layout_rotation.load_table(cover_layout_table())
T = {t["template_id"]: t for t in LAYOUTS["templates"]}


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


@pytest.mark.parametrize("tid", ["column-left", "column-right", "closeup-left", "closeup-right"])
def test_the_window_puts_her_on_the_other_side_of_the_words(tid):
    words = T[tid]["text"]
    made, why = side.plan(FACE, HEAD_TOP, [], COVER, T[tid])
    assert why is None
    x0, y0, w = made["crop"]
    assert made["face"][0] >= 0 and made["face"][2] <= 1920 and made["face"][1] >= 0 and made["face"][3] <= 1080   # the face is on the cover
    centre = (made["face"][0] + made["face"][2]) / 2
    assert centre > 1920 * 0.6 if words == "left" else centre < 1920 * 0.4                  # she stands away from the words
    assert made["face_px"] >= COVER["face_floor_frac"] * 1080
    if tid.startswith("closeup"):                                                          # a close-up: a much larger face
        assert made["face_px"] >= 0.5 * 1080


def test_a_face_near_the_words_side_gets_a_window_padded_on_that_side():
    near_left = [300, 300, 500, 500]          # the column's place for her face is right of the words: past the frame's left edge
    made, why = side.plan(near_left, 200, [], COVER, T["column-left"])
    assert why is None and made["pad"]["side"] == "left" and made["pad"]["px"] > 0
    assert (made["face"][0] + made["face"][2]) / 2 > 1920 * 0.6


@pytest.mark.parametrize("face, head_top, keep, why", [
    (FACE, None, [], "HEAD_TOP_UNMEASURED"),
    ([900, 700, 1000, 760], 0, [], "FACE_UNDER_FLOOR"),      # a small face far under her head top: the window would shrink it
    (FACE, HEAD_TOP, [[1700, 100, 1900, 300]], "KEEPS_OUTSIDE"),    # a kept box would leave the window
])
def test_a_frame_without_such_a_window_says_why(face, head_top, keep, why):
    assert side.plan(face, head_top, keep, COVER, T["column-left"]) == (None, why)


def _run(template, job_extra=None):
    job = {"column_rows": ROWS if template["rows"] == "column" else [], **(job_extra or {})}
    run = SimpleNamespace(job=job, rules=COVER, plans={}, column=layout.Composition(job, COVER))
    run.column.template = template
    run.text_ink = lambda spec: ({"style_id": "s"}, lambda i, size: (size * len(spec["lines"][i]["text"]), size))
    return run


def _spec(frame="frame.png"):
    return {"id": "screenshot", "frame": frame, "crop": [0, 0, 1920], "layout": "auto",
            "lines": [{"text": "摄像头认出kmx"}, {"text": "永远看着你"}], "keep": [], "blur": [[0, 300, 300, 800]]}


def test_a_column_template_takes_the_rows_and_the_panel_and_the_top_template_keeps_two_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(side.text_fit, "need_face_top", lambda *a, **k: 500)
    frame = tmp_path / "frame.png"
    Image.new("RGB", (1920, 1080), SKY).save(frame)
    run, spec = _run(T["column-right"]), _spec(str(frame))
    side.solo(run, spec, FACE, HEAD_TOP, SimpleNamespace(phase=lambda name: _Quiet()))
    assert spec["layout"] == "right" and [r["text"] for r in spec["lines"]] == [r["text"] for r in ROWS]
    assert spec["panel"]["side"] == "right" and spec["panel"]["edge"] > run.plans["screenshot"]["face"][2]
    assert run.plans["screenshot"]["template_id"] == "column-right"
    assert "底板" in side.note(spec)
    # the top template: the two lines on top, framed as tight as the head allows, no panel
    run, spec = _run(T["top-two"]), _spec(str(frame))
    side.solo(run, spec, FACE, HEAD_TOP, SimpleNamespace(phase=lambda name: _Quiet()))
    assert spec["layout"] == "top" and len(spec["lines"]) == 2 and "panel" not in spec
    assert "text_top_wanted" in run.plans["screenshot"] and "底板" not in side.note(spec)
    # the bottom banner: the words under her, on a band
    run, spec = _run(T["bottom-banner"]), _spec(str(frame))
    side.solo(run, spec, FACE, HEAD_TOP, SimpleNamespace(phase=lambda name: _Quiet()))
    assert spec["layout"] == "bottom" and spec["banner"] is True and len(spec["lines"]) == 2


def test_a_frame_without_the_templates_window_refuses_the_template(tmp_path):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (1920, 1080), SKY).save(frame)
    run, spec = _run(T["column-left"]), _spec(str(frame))
    with pytest.raises(ValueError, match="HEAD_TOP_UNMEASURED") as refused:   # the tool's own copy of the shared module
        side.solo(run, spec, FACE, None, SimpleNamespace(phase=lambda name: _Quiet()))
    assert type(refused.value).__name__ == "TemplateRefused"


class _Quiet:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _painted(tmp_path, words, job_extra=None, sha="0"):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (1920, 1080), SKY).save(frame)
    made, _ = side.plan(FACE, HEAD_TOP, [], COVER, T[f"column-{words}"])
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
    made, _ = side.plan(FACE, HEAD_TOP, [], COVER, T["column-left"])
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


def test_five_covers_of_one_stream_take_five_compositions(tmp_path):
    """Five screenshot covers of one stream in a row, each in the template the selector gives it after the ones before:
    five different compositions, never the same one twice in a row (owner every cover looked the same)."""
    frame = tmp_path / "frame.png"
    Image.new("RGB", (1920, 1080), SKY).save(frame)
    facts = {"people": "solo", "lines": ["摄像头认出kmx", "永远看着你"], "column_rows": True, "route": "screenshot",
             "fans_emote": False, "watching": False}
    history: list[str] = []
    for n in range(5):
        def attempt(template):
            run, spec = _run(template), _spec(str(frame))
            side.solo(run, spec, FACE, HEAD_TOP, SimpleNamespace(phase=lambda name: _Quiet()))
            return {"punch": 202, "layout": spec["layout"]}
        result, selection = cover_layout_rotation.select(LAYOUTS, facts, attempt=attempt, history=history,
                                                         session_history=history, content_sha256=f"{n:064x}")
        assert selection["fallback"] is None and result["layout"] == result["template"]["text"]
        history.append(selection["template_id"])
    assert len(set(history)) == 5 and all(a != b for a, b in zip(history, history[1:]))


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


def test_a_cover_that_repeats_the_covers_before_it_ranks_behind_but_never_asks_for_a_picture():
    from src.autoslice.manual_lane.step6_cover_round import pick_free

    # a fresh screenshot still goes before the sticker (owner 11-18)
    assert pick_free(_Cover([_row("emote", 15), _row("screenshot", 11)]), ["a", "b"], "id").chosen == "screenshot"
    # the screenshot repeats the covers before it: the sticker, which does not, is taken
    assert pick_free(_Cover([_row("emote", 12), _row("screenshot", 13, repeat=True)]), ["a", "b"], "id").chosen == "emote"
    # review: the fresh one's thumbnail fails; the repeat passed and is free, so it is taken and no picture
    # is generated (U138: 免费合格即选，都不合格才生 1 张; the repeat is recorded, not paid for)
    picked = pick_free(_Cover([_row("emote", 12), _row("screenshot", 13, repeat="构图样板 top-two 最近几张用过")],
                              failing={"emote"}), ["a", "b"], "id")
    assert picked is not None and picked.chosen == "screenshot" and "不为换样子出 AI 图" in picked.why


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
