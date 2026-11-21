"""A model judge's "no" that costs something is never taken from one reading (assets/manual_lane/remote/veto.py), and
every model judgement of the lane is classed in lane_rules `veto`, so a new one cannot be added without saying which
kind it is."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.lane_rules import RULES_FILE

REMOTE = Path(si.REMOTE_TOOLS)
REGISTRY = json.loads(RULES_FILE.read_text(encoding="utf-8"))
CLASSES = ("confirm", "repeated", "free", "none")
ASKS = {"vision", "ask"}          # the two model clients of the media-host tools (cover_candidates.vision, pick_frame.ask)


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_veto_test", REMOTE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


veto = _load("veto")


# ----- the rule itself ---------------------------------------------------------------------------------------------------
def _run(answers):
    """confirm() over scripted answers: True/False = a judged reading of one axis, None = unjudged, an exception = failed."""
    asked = []

    def read(n):
        asked.append(n)
        got = answers[n - 1]
        if isinstance(got, Exception):
            raise got
        return got

    taken, verdict = veto.confirm(read, lambda got: None if got is None else {"ok": got})
    return asked, taken, verdict


@pytest.mark.parametrize(("answers", "asked", "passes"), [
    ([True], [1], True),                                   # a clean pass stands: nothing more is asked
    ([False, True, True], [1, 2, 3], True),                # one no among three
    ([False, False, True], [1, 2, 3], False),              # most say no
    ([False, True, False], [1, 2, 3], False),
    ([False, OSError("x"), True], [1, 2, 3], True),        # one no, one yes: not a rejection
    ([False, OSError("x"), ValueError("y")], [1, 2, 3], None),   # one no with nothing to confirm it: no verdict
    ([None, True, OSError("x")], [1, 2, 3], True),         # an unjudged first reading is asked again; a pass stands
    ([None, None, None], [1, 2, 3], None),
])
def test_a_no_is_asked_again_and_only_most_readings_reject(answers, asked, passes):
    got_asked, taken, verdict = _run(answers)
    assert got_asked == asked and len(taken) == len(asked)
    assert (verdict["ok"]["pass"] if verdict else None) is passes
    if verdict:
        assert taken[verdict["ok"]["from"]]["axes"] == {"ok": passes}     # `from` points at a reading that says so


def test_the_first_calls_failure_is_the_callers():
    with pytest.raises(OSError):
        _run([OSError("no answer at all")])


# ----- the cover judge and the locators go through it ------------------------------------------------------------------------
def _answer(cc, **flags):
    ok = {"pass": True, "reason": "看图"}
    doc = {"people": {"expected": [cc.crop_mod.HOST], "present": [cc.crop_mod.HOST], "reason": "看图"},
           "hook_fit": {"score": 2, "reason": "看图"}, "face": [1300, 300, 1600, 650], "keep": [],
           **{k: ok for k in cc.cover_confirm.JUDGE_FLAGS}}
    for key, value in flags.items():
        doc[key] = {"pass": value, "reason": "不像" if not value else "看图"}
    return json.dumps(doc, ensure_ascii=False)


def _scripted(monkeypatch, cc, answers):
    names = []

    def vision(job, name, question, images, out_dir):
        names.append(name)
        return answers[len(names) - 1], False
    monkeypatch.setattr(cc, "vision", vision)
    return names


def test_one_judge_reading_that_vetoes_does_not_veto_the_candidate(monkeypatch, tmp_path):
    cc = _load("cover_candidates")
    names = _scripted(monkeypatch, cc, [_answer(cc, identity=False), _answer(cc), _answer(cc)])
    answer, _, votes = cc.judge({}, "screenshot", "q", "cand.png", "frame.png", str(tmp_path))
    assert names == ["screenshot.judge", "screenshot.judge.2", "screenshot.judge.3"]         # each its own cache name
    assert cc.parse_verdict(answer)["identity"]["pass"] is True
    assert votes["identity"] == {"pass": True, "fails": 1, "judged": 3}


def test_a_veto_most_judge_readings_repeat_stands_and_a_clean_pass_is_read_once(monkeypatch, tmp_path):
    cc = _load("cover_candidates")
    _scripted(monkeypatch, cc, [_answer(cc, clean_layers=False), _answer(cc), _answer(cc, clean_layers=False)])
    answer, _, votes = cc.judge({}, "emote", "q", "cand.png", "frame.png", str(tmp_path))
    assert cc.parse_verdict(answer)["clean_layers"]["pass"] is False and votes["clean_layers"]["fails"] == 2
    names = _scripted(monkeypatch, cc, [_answer(cc)])
    assert cc.judge({}, "emote", "q", "cand.png", "frame.png", str(tmp_path))[2] is None and names == ["emote.judge"]


def test_a_face_one_reading_does_not_find_is_looked_for_again(monkeypatch, tmp_path):
    cc = _load("cover_candidates")
    names = _scripted(monkeypatch, cc, ['{"box": null}', '{"box": [10, 10, 90, 90]}', '{"box": [12, 10, 90, 90]}'])

    def found(answer):
        return json.loads(answer)["box"]
    assert cc.found_by_most({}, "locate-x", "q", [], str(tmp_path), found) == [10, 10, 90, 90]
    assert names == ["locate-x", "locate-x.2", "locate-x.3"]
    _scripted(monkeypatch, cc, ['{"box": null}', '{"box": null}', '{"box": [1, 1, 9, 9]}'])
    assert cc.found_by_most({}, "locate-y", "q", [], str(tmp_path), found) is None


# ----- every model judgement is classed ---------------------------------------------------------------------------------------
def _askers():
    """{tool.function: the names it calls} for every function of the media-host tools that asks a model directly (the
    outermost function or Class.method the call sits in; the two clients themselves are not judgements)."""
    out: dict[str, set[str]] = {}
    for path in sorted(REMOTE.glob("*.py")):
        def walk(node, owner):
            for child in ast.iter_child_nodes(node):
                here = owner
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and (owner is None or owner.endswith(".")):
                    here = (owner or "") + child.name
                elif isinstance(child, ast.ClassDef) and owner is None:
                    here = child.name + "."
                if isinstance(child, ast.Call) and here and not here.endswith("."):
                    fn = child.func
                    called = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
                    out.setdefault(f"{path.stem}.{here}", set()).add(called)
                walk(child, here)
        walk(ast.parse(path.read_text(encoding="utf-8")), None)
    return {name: calls for name, calls in out.items() if calls & ASKS and name.split(".")[-1] not in ASKS}


def test_every_function_that_asks_a_model_on_the_media_host_is_classed_and_a_veto_goes_through_confirm():
    askers, table = _askers(), REGISTRY["veto"]["remote"]
    assert set(askers) == set(table), (
        "a media-host function asks a model without a class in lane_rules veto.remote (or the table names one that is gone): "
        f"{sorted(set(askers) ^ set(table))}")
    for name, entry in table.items():
        kind = entry.split("｜")[0]
        assert kind in CLASSES, (name, entry)
        if kind == "confirm":       # veto.confirm itself, or cover_confirm's confirm_* (which only wrap it)
            assert any(c.startswith("confirm") for c in askers[name]), f"{name} is classed as a veto but does not ask through veto.confirm"


def test_every_text_call_purpose_is_classed():
    table = REGISTRY["veto"]["purposes"]
    assert set(table) == set(REGISTRY["effort"]["purposes"]), sorted(set(table) ^ set(REGISTRY["effort"]["purposes"]))
    assert all(entry.split("｜")[0] in CLASSES for entry in table.values())
    # a purpose classed `confirm` needs a caller that asks through veto.confirm: none is today, so none may be added
    # here without that code and its own behaviour test
    assert not [p for p, entry in table.items() if entry.startswith("confirm")]
