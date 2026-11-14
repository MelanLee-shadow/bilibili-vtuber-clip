"""The skill renderer consumes the same rotation rule as the repo, including real-history semantics."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice.cover_style_rotation import REPO_TABLE
from src.autoslice.manual_lane.step6_cover import COVER_RENDERER

FONT = Path(os.environ.get("LIDOUSHA_COVER_FONT") or Path.home() / ".local/share/fonts/lidousha/RuiZiZhenYan.ttf")
S1 = "s1-blue-pink-t4-r70-white"
S2 = "s2-pink-blue-tm4-r55-dark"
S4 = "s4-orange-red-tm3-r70-dark"
S5 = "s5-pink-blue-t3-r85-white"
S6 = "s6-blue-purple-tm2-r55-white"
S7 = "s7-green-pink-t4-r70-double"


@pytest.fixture
def renderer(monkeypatch):
    if not COVER_RENDERER.is_file():
        pytest.skip("title-cover skill renderer not present")
    spec = importlib.util.spec_from_file_location("tested_cover_renderer", COVER_RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_table(module.load_styles(REPO_TABLE))
    monkeypatch.setattr(module, "missing_glyphs", lambda font, text: [])
    monkeypatch.setattr(module, "background", lambda spec: Image.new("RGBA", (1, 1)))
    return module


def _spec(**kwargs):
    return {"face": [1100, 300, 1600, 760], "lines": [{"text": "测试"}], **kwargs}


def _attempt(failures=None):
    failures = failures or {}
    def attempt(spec, lines, font, background, original_background, style):
        mode = failures.get(style["style_id"])
        if mode == "layout":
            raise SystemExit("layout does not fit")
        return {"canvas": Image.new("RGBA", (1, 1)), "record": [{"size": 180}], "layout": "left",
                "low_contrast": mode == "contrast", "warnings": [], "overridden": []}
    return attempt


@pytest.mark.parametrize(("history", "base", "expected", "reason"), [
    ([], 4, S5, None),
    ([S2, S4], 4, S5, None),  # old pink/blue does not exclude that pair after orange/red
    ([S4, S2], 4, S6, "recent_colors"),  # last cover controls the colour pair
    ([S2], 0, S4, "recent_colors"),  # reversed blue/pink also counts as the same pair
    ([S2, "retired-style"], 4, S5, None),  # unknown last ID does not inherit an earlier pair
])
def test_renderer_uses_real_history_order(renderer, monkeypatch, history, base, expected, reason):
    monkeypatch.setattr(renderer, "_attempt", _attempt())
    _, _, selection = renderer.render(_spec(), "unused", recent_style_ids=history,
                                      content_sha256=f"{base:016x}" + "0" * 48)
    assert selection["style_id"] == expected
    assert selection["recent_style_ids"] == sorted(history)
    if reason:
        assert {"style_id": renderer.TABLE["styles"][base]["style_id"], "reason": reason} in selection["skipped"]


def test_renderer_keeps_fallbacks_for_exclusions_layout_and_contrast(renderer, monkeypatch):
    sha = "0000000000000004" + "0" * 48
    history = [s["style_id"] for s in renderer.TABLE["styles"]]
    monkeypatch.setattr(renderer, "_attempt", _attempt())
    _, _, selection = renderer.render(_spec(), "unused", recent_style_ids=history, content_sha256=sha)
    assert selection["style_id"] == S5 and selection["fallback"] == "recent_repeat_forced"

    monkeypatch.setattr(renderer, "_attempt", _attempt({S5: "contrast", S6: "layout"}))
    _, _, selection = renderer.render(_spec(), "unused", content_sha256=sha)
    assert selection["style_id"] == S7
    assert selection["skipped"][:2] == [{"style_id": S5, "reason": "low_contrast"},
                                         {"style_id": S6, "reason": "layout_failed:layout does not fit"}]

    monkeypatch.setattr(renderer, "_attempt", _attempt({s["style_id"]: "contrast" for s in renderer.TABLE["styles"]}))
    _, _, selection = renderer.render(_spec(), "unused", content_sha256=sha)
    assert selection["style_id"] == S5 and selection["fallback"] == "all_styles_rejected"


def test_copied_renderer_without_rotation_source_reports_the_missing_dependency(tmp_path):
    if not COVER_RENDERER.is_file():
        pytest.skip("title-cover skill renderer not present")
    script = tmp_path / "render_cover.py"
    styles = tmp_path / "styles.json"
    shutil.copyfile(COVER_RENDERER, script)
    shutil.copyfile(REPO_TABLE, styles)
    proc = subprocess.run([sys.executable, str(script), "unused.json", "--styles", str(styles)],
                          capture_output=True, text=True)
    assert proc.returncode != 0 and "COVER_STYLE_SELECTOR_UNREADABLE" in proc.stderr


@pytest.mark.skipif(not (FONT.is_file() and COVER_RENDERER.is_file()), reason="private font or skill renderer not present")
def test_local_cli_witness_and_forced_style_with_overrides(tmp_path):
    spec = {"id": "witness", "bg": ["#101010", "#101010"], "face": [1100, 300, 1600, 760],
            "lines": [{"text": "测试2"}, {"text": "怎么回事"}]}
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    command = [sys.executable, str(COVER_RENDERER), str(spec_path), "--styles", str(REPO_TABLE), "--font", str(FONT),
               "--out-dir", str(tmp_path), "--recent", f"{S4},{S2}"]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    chosen = json.loads((tmp_path / "witness.render.json").read_text(encoding="utf-8"))["cover_style"]
    assert chosen["base_index"] == 4 and chosen["style_id"] == S6
    assert {"style_id": S5, "reason": "recent_colors"} in chosen["skipped"]

    spec.update(style_id=S2, tilt=3, lines=[{"text": "测试", "color": "orange", "size": 150},
                                           {"text": "怎么回事", "size": 200}])
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    forced = subprocess.run(command, capture_output=True, text=True)
    assert forced.returncode == 0, forced.stderr
    record = json.loads((tmp_path / "witness.render.json").read_text(encoding="utf-8"))
    assert record["cover_style"]["style_id"] == S2 and record["cover_style"]["selection"] == "spec_style_id"
    assert set(record["cover_style"]["overridden"]) == {"tilt", "line0.color", "line0.size", "line1.size"}
    assert record["lines"][0]["color"] == "orange" and [line["size"] for line in record["lines"]] == [150, 200]
    assert all(line["tilt"] == 3 for line in record["lines"])
