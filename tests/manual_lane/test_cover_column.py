"""The cover's text layout takes turns, and a column's rows are the cover lines (owner not every cover one
line top left and one line bottom right)."""

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


def test_the_layout_is_the_first_the_last_covers_did_not_use():
    assert cover_column.next_layout([], COVER)[0] == "top"
    assert cover_column.next_layout(["top"], COVER)[0] == "left"
    assert cover_column.next_layout(["top", "left"], COVER)[0] == "right"
    assert cover_column.next_layout(["top", "left", "right"], COVER)[0] == "top"        # only the last two count
    assert cover_column.next_layout([None, "auto", "top", None], COVER)[0] == "left"    # a layout that is not in the turn is not counted
    assert cover_column.next_layout(["left", "left"], COVER)[0] == "top"


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
    """What Column.place reads of the tool's run: the renderer (unused by the fake placement) and the rows' painted width."""
    renderer = None

    def __init__(self, width_per_char):
        self.width_per_char = width_per_char

    def text_ink(self, spec):
        return None, lambda i, size: (round(len(spec["lines"][i]["text"]) * size * self.width_per_char), size)


def _column(monkeypatch, layout, job, *, face_w=540):
    """A Column whose person lands with a face `face_w` wide, centred where place_solo was asked to centre it."""
    def place_solo(layer, face, renderer, cfg, need_top, host_side, centre_frac=None):
        centre = round((centre_frac if centre_frac is not None else cfg["sticker_face_center_x_frac"]) * 1920)
        return {"x": 0}, [centre - face_w // 2, 500, centre + face_w // 2, 940], {"need_top": need_top, "centre": centre}
    monkeypatch.setattr(layout, "place_solo", place_solo)
    return layout.Column(job, {k: v for k, v in COVER.items() if not k.endswith("_source")})


def test_a_candidate_gets_the_column_when_the_person_leaves_its_punchline_rows_room(monkeypatch):
    layout = _layout_module()
    rows = [_row("打针不会哭", False), _row("的人", False), _row("无趣的", True), _row("大人", True)]
    column = _column(monkeypatch, layout, {"text_layout": "left", "column_rows": rows})
    _, box, plan = column.place(_Run(1.0), "emote", {}, [0, 0, 1, 1], 555, "right", None, None)
    assert plan["text_layout"] == "left" and plan["need_top"] is None and box[0] > 1100          # she stands on the right
    assert column.text("emote", [{"text": t} for t in LINES]) == {"layout": "left", "lines": rows}
    assert column.record()["placed"]["emote"]["punch_row_px"] <= column.record()["placed"]["emote"]["zone_px"]
    mirrored = _column(monkeypatch, layout, {"text_layout": "right", "column_rows": rows})
    _, box, _ = mirrored.place(_Run(1.0), "emote", {}, [0, 0, 1, 1], 555, "right", None, None)
    assert box[2] < 820 and mirrored.text("emote", [])["layout"] == "right"                      # and on the left for a right column


def test_a_column_that_does_not_fit_or_is_not_hers_alone_keeps_the_two_lines_on_top(monkeypatch):
    layout = _layout_module()
    rows = [_row("打针不会哭", False), _row("的人", False), _row("无趣的", True), _row("大人", True)]
    lines = [{"text": t} for t in LINES]
    wide = _column(monkeypatch, layout, {"text_layout": "left", "column_rows": rows})
    _, _, plan = wide.place(_Run(2.0), "cutout", {}, [0, 0, 1, 1], 555, "right", None, None)     # the rows paint too wide for the side
    assert plan["need_top"] == 555 and "text_layout" not in plan
    assert wide.text("cutout", lines, "auto") == {"layout": "auto", "lines": lines} and "cutout" in wide.record()["refused"]
    for job, guest, centre in (({"text_layout": "left", "column_rows": rows}, {"src": "g"}, None),     # another person
                               ({"text_layout": "left", "column_rows": rows}, None, 0.5),              # a fixed centre (kmx)
                               ({"text_layout": "top", "column_rows": rows}, None, None),              # not this cover's turn
                               ({"text_layout": "left", "column_rows": []}, None, None)):              # no rows
        column = _column(monkeypatch, layout, job)
        _, _, plan = column.place(_Run(1.0), "emote", {}, [0, 0, 1, 1], 555, "right", centre, guest)
        assert plan["need_top"] == 555 and column.text("emote", lines) == {"layout": "top", "lines": lines}


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
