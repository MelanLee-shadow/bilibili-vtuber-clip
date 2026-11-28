"""A column's rows are the cover lines, and a candidate is placed and worded as its composition template says (owner
not every cover one line top left and one line bottom right)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from src.autoslice.manual_lane import cover_column
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.lane_rules import rules

COVER = rules("cover")
LINES = ["打针不会哭的人", "无趣的大人"]


def _row(text, punch):
    return {"text": text, "punch": punch}


def test_rows_are_kept_only_when_they_are_the_lines_letter_for_letter_within_the_row_lengths():
    good = [_row("打针不会哭", False), _row("的人", False), _row("无趣的", True), _row("大人", True)]
    assert cover_column.rows_of(LINES, good, COVER) == good
    assert cover_column.rows_of(LINES, [_row("打针不会哭的人", False), _row("无趣的大人", True)], COVER) is None   # a punchline row over the limit
    assert cover_column.rows_of(LINES, [_row("打针不哭的人", False), _row("无趣的", True), _row("大人", True)], COVER) is None    # a letter lost
    assert cover_column.rows_of(LINES, [_row("无趣的", True), _row("大人", True), _row("打针不会哭的人", False)], COVER) is None  # punchline first
    assert cover_column.rows_of(LINES, [_row("打针不会哭的人", False), _row("无趣的", False), _row("大人", True)], COVER) is None  # a row in the wrong part
    assert cover_column.rows_of(LINES, good + [_row("！", True)], COVER) is None                                      # more rows than a column holds
    assert cover_column.rows_of(LINES, None, COVER) is None and cover_column.rows_of(LINES, [], COVER) is None
    one = [_row("无趣的", True), _row("大人", True)]
    assert cover_column.rows_of(["无趣的大人"], one, COVER) == one                         # one cover line: it is the punchline
    marks = [_row("无趣的", True), _row("大人！", True)]
    assert cover_column.rows_of(["无趣的大人！"], marks, COVER) == marks                    # punctuation is not a letter


def _layout_module():
    spec = importlib.util.spec_from_file_location("column_layout", Path(si.REMOTE_TOOLS) / "cover_layout.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Run:
    """What Composition.place reads of the tool's run: the renderer (unused by the fake placement)."""
    renderer = None


def _composition(monkeypatch, layout, job, template, *, face_w=540):
    """A Composition in `template` whose person lands with a face `face_w` wide, centred where place_solo was asked."""
    asked = []

    def place_solo(layer, face, renderer, cfg, need_top, host_side, centre_frac=None, face_frac=None, face_top=None):
        asked.append({"need_top": need_top, "centre_frac": centre_frac, "face_frac": face_frac, "face_top": face_top})
        centre = round((centre_frac if centre_frac is not None else cfg["sticker_face_center_x_frac"]) * 1920)
        return {"x": 0}, [centre - face_w // 2, 500, centre + face_w // 2, 940], {"need_top": need_top, "centre": centre}
    monkeypatch.setattr(layout, "place_solo", place_solo)
    comp = layout.Composition(job, {k: v for k, v in COVER.items() if not k.endswith("_source")})
    comp.template = template
    return comp, asked


def _templates():
    from src.autoslice import cover_layout_rotation
    from src.autoslice.manual_lane.step6_cover import cover_layout_table
    return {t["template_id"]: t for t in cover_layout_rotation.load_table(cover_layout_table())["templates"]}


def test_a_column_template_places_her_on_the_other_side_and_takes_the_rows(monkeypatch):
    layout, t = _layout_module(), _templates()
    rows = [_row("打针不会哭", False), _row("的人", False), _row("无趣的", True), _row("大人", True)]
    comp, asked = _composition(monkeypatch, layout, {"column_rows": rows}, t["column-left"])
    _, box, plan = comp.place(_Run(), "emote", {}, [0, 0, 1, 1], 555, "right", None, None)
    assert plan["template_id"] == "column-left" and box[0] > 1100                    # she stands on the right
    assert asked[-1] == {"need_top": None, "centre_frac": 0.72, "face_frac": 0.36, "face_top": None}
    assert comp.text("emote", [{"text": x} for x in LINES]) == {"layout": "left", "lines": rows}
    mirrored, _ = _composition(monkeypatch, layout, {"column_rows": rows}, t["column-right"])
    _, box, _ = mirrored.place(_Run(), "emote", {}, [0, 0, 1, 1], 555, "right", None, None)
    assert box[2] < 820 and mirrored.text("emote", [])["layout"] == "right"           # and on the left for a right column


def test_a_guest_keeps_her_place_and_only_the_words_change(monkeypatch):
    layout, t = _layout_module(), _templates()
    lines = [{"text": x} for x in LINES]
    comp, asked = _composition(monkeypatch, layout, {"column_rows": []}, t["bottom-banner"])
    _, _, plan = comp.place(_Run(), "emote", {}, [0, 0, 1, 1], 555, "right", None, {"src": "g"})   # another person
    assert plan["need_top"] == 555 and asked[-1]["centre_frac"] is None
    assert comp.text("emote", lines) == {"layout": "bottom", "lines": lines, "banner": True}
    top, asked = _composition(monkeypatch, layout, {"column_rows": []}, t["top-two"])           # the approved framing
    _, _, plan = top.place(_Run(), "cutout", {}, [0, 0, 1, 1], 555, "right", None, None)
    assert plan["need_top"] == 555 and asked[-1]["face_frac"] == 0.43
    assert top.text("cutout", lines) == {"layout": "top", "lines": lines}
    none, _ = _composition(monkeypatch, layout, {}, None)                                       # no template: the caller's own
    assert none.text("ai_scene", lines, "auto") == {"layout": "auto", "lines": lines}


def test_the_cover_brief_asks_for_the_rows_with_the_rule_lengths(tmp_path):
    from fakes import FakeCpa, FakeRemote, make_ctx

    from src.autoslice.manual_lane import step6_cover

    cpa = FakeCpa([])
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=cpa)
    cues = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "打针不会哭的人都是无趣的大人"}]
    brief = step6_cover._cover_brief(ctx, cues, "标题", LINES, ["李豆沙"])
    assert "_failed" not in brief and "column_rows" in brief
    prompt = cpa.prompts[-1]
    assert f"2 到 {COVER['column_rows_max']} 行" in prompt and f"不超过 {COVER['column_punch_row_max_chars']} 个字" in prompt
    assert f"不超过 {COVER['column_context_row_max_chars']} 个字" in prompt and "{rows_max}" not in prompt
