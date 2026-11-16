"""Step 5 does not burn a character no font draws (「南町的🔑」 came out 「南町的□」 on the media host)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2] / "assets/manual_lane/remote/render_ass.py"


def _tool(monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_REMOTE_REPO", str(HELPER.parents[3]))
    spec = importlib.util.spec_from_file_location("render_ass_tool", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ASS = ("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
       "Dialogue: 0,0:00:01.00,0:00:02.00,Host,,0,0,0,,{\\an2}嘴上说着南町的🔑，\\N谢谢你，薯ki。\n")
CJK = [(0x20, 0x7E), (0x3000, 0x303F), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF)]


def test_a_character_no_font_covers_is_reported_and_one_some_font_covers_is_not(monkeypatch):
    tool = _tool(monkeypatch)
    assert tool.missing(ASS, CJK) == ["🔑"]
    assert tool.missing(ASS, CJK + tool._ranges("1f500-1f53d")) == []
    assert tool.missing(ASS.replace("🔑", "钥匙️"), CJK) == []        # a variation selector draws nothing
