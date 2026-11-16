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


def test_a_fontsdir_font_counts_only_for_the_styles_that_name_it(monkeypatch, tmp_path):
    """libass takes a missing glyph only from fontconfig's fonts (an emoji font in FONTSDIR still burned 🥺
    as a box), so a FONTSDIR font no style names covers nothing."""
    tool = _tool(monkeypatch)
    (tmp_path / "Emoji.ttf").write_bytes(b"")
    (tmp_path / "Smiley.ttf").write_bytes(b"")

    class Done:
        def __init__(self, stdout):
            self.stdout = stdout

    def run(cmd, **_):
        if cmd[0] == "fc-list":
            return Done("20-7e 3000-303f 4e00-9fff ff00-ffef\n")
        return Done({"Emoji.ttf": "Noto Emoji\t1f97a\n", "Smiley.ttf": "Smiley Sans,得意黑\t3000-303f\n"}[Path(cmd[-1]).name])

    monkeypatch.setattr(tool.subprocess, "run", run)
    styles = "[V4+ Styles]\nStyle: LDS,Smiley Sans,72,&H00FFFFFF\n" + ASS.replace("🔑", "🥺")
    covered = tool.drawable(tmp_path, styles)
    assert tool.missing(styles, covered) == ["🥺"]
    assert tool.missing(styles.replace("Smiley Sans,72", "Noto Emoji,72"), tool.drawable(tmp_path, styles.replace("Smiley Sans,72", "Noto Emoji,72"))) == []


def test_step5_adds_the_lane_fonts_to_fontconfig_for_the_glyph_check_and_the_burn(tmp_path):
    import json
    from fakes import FakeCpa, FakeRemote, make_ctx
    from src.autoslice.manual_lane.adapters import RemoteResult
    from src.autoslice.manual_lane.step5_burn import step5_burn
    from src.autoslice.manual_lane.step_io import REMOTE_LANE_FONTS

    def run(name, body, remote):
        if name == "s5-burn":
            remote.files["/r/clip/burn/intro_result.json"] = json.dumps(
                {"status": "PREPENDED", "intro_id": "A", "intro_offset_ms": 1000}).encode()
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=run)
    ctx = make_ctx(tmp_path, remote=remote, cpa=FakeCpa([]))
    speaker = ctx.clip_dir / "speaker/speaker.srt"
    speaker.parent.mkdir(parents=True)
    speaker.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    assert step5_burn(ctx).ok
    script = remote.scripts[-1][1]
    conf = script.index("export FONTCONFIG_FILE=")
    assert f"<dir>{REMOTE_LANE_FONTS}</dir>" in script[:conf]
    assert conf < script.index("render_ass.py") < script.index("ffmpeg ")
    shipped = HELPER.parents[1] / "fonts"
    assert (shipped / "NotoEmoji-wght.ttf").is_file() and (shipped / "OFL.txt").is_file()
