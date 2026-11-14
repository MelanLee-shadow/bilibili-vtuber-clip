"""The fallback compares the actual rendered candidates using the existing vision adapter."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


def _tool(monkeypatch, answer):
    seen = []
    fake = types.ModuleType("cover_candidates")
    fake.CALLS = []

    def vision(job, name, question, images, out):
        seen.append((job, name, question, images, out))
        return json.dumps(answer, ensure_ascii=False), False

    fake.vision = vision
    monkeypatch.setitem(sys.modules, "cover_candidates", fake)
    path = Path(__file__).resolve().parents[2] / "assets/manual_lane/remote/cover_fallback.py"
    spec = importlib.util.spec_from_file_location("fallback_tool_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, seen


def _job(tmp_path):
    job = {"out": str(tmp_path), "frame": str(tmp_path / "frame.png"), "title": "标题", "lines": ["短字"],
           "judge_model": "fake", "candidates": [{"id": "screenshot", "vetoes": ["MAIN_FACE_BELOW_25_PERCENT"]},
                                                   {"id": "cutout", "vetoes": ["IDENTITY_UNCERTAIN"]}]}
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    return path


def test_actual_candidate_images_and_veto_reasons_reach_vision(tmp_path, monkeypatch):
    module, seen = _tool(monkeypatch, {"chosen": "cutout", "reason": "更贴近原片，但脸略小"})
    module.main(str(_job(tmp_path)))
    _, name, question, images, _ = seen[0]
    assert name == "fallback-choose" and "MAIN_FACE_BELOW_25_PERCENT" in question
    assert [Path(p).name for p, _width in images] == ["screenshot.png", "cutout.png", "frame.png"]
    assert json.loads((tmp_path / "fallback.result.json").read_text())["choice"]["chosen"] == "cutout"


def test_vision_must_choose_a_rendered_candidate(tmp_path, monkeypatch):
    module, _ = _tool(monkeypatch, {"chosen": "invented", "reason": "x"})
    with pytest.raises(ValueError, match="CPA_COVER_CHOICE_INVALID"):
        module.main(str(_job(tmp_path)))
