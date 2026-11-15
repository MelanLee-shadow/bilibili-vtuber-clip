"""The cover frame at the host's signature move (owner 片中有经典动作优先截它): pick_frame.py looks over the
whole cut for it, and a screenshot taken there goes first among the candidates that passed."""
import importlib.util
import json

from src.autoslice.manual_lane import adapters, cover_pick
from src.autoslice.manual_lane.step6_cover import on_action

def _load():
    from pathlib import Path

    path = Path(adapters.CPA_TEXT_SCRIPT).parent / "pick_frame.py"
    spec = importlib.util.spec_from_file_location("pick_frame_action", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake(pf, monkeypatch, answers, length=60.0):
    asked = []
    monkeypatch.setattr(pf, "duration", lambda cut: length)
    monkeypatch.setattr(pf, "sheet", lambda cut, times, path: asked.append(list(times)) or "aW1n")
    replies = iter(answers)
    monkeypatch.setattr(pf, "ask", lambda base, key, image, question, **kw: next(replies))
    return asked


def test_the_move_seen_in_a_sheet_is_the_frame_of_that_cell(monkeypatch, tmp_path):
    pf = _load()
    asked = _fake(pf, monkeypatch, ['{"cell": null}', '{"cell": 3, "what": "蹲下抱头"}'])
    found = pf.find_action("cut.mp4", str(tmp_path), "b", "k")
    assert [len(t) for t in asked] == [20, 10]                 # 30 frames two seconds apart, 20 to a sheet
    assert found["t"] == asked[1][2] == 45.0 and found["what"] == "蹲下抱头"


def test_several_moves_are_settled_by_one_more_question(monkeypatch, tmp_path):
    pf = _load()
    asked = _fake(pf, monkeypatch, ['{"cell": 2, "what": "捂脸"}', '{"cell": 1, "what": "蹲下抱头"}', '{"cell": 2}'])
    found = pf.find_action("cut.mp4", str(tmp_path), "b", "k")
    assert asked[-1] == [3.0, 41.0] and found == {"t": 41.0, "what": "蹲下抱头",
                                                  "seen": [{"t": 3.0, "what": "捂脸"}, {"t": 41.0, "what": "蹲下抱头"}]}


def test_no_move_and_an_answer_outside_the_sheet_find_nothing(monkeypatch, tmp_path):
    pf = _load()
    _fake(pf, monkeypatch, ['{"cell": 99, "what": "?"}', "没有"])
    assert pf.find_action("cut.mp4", str(tmp_path), "b", "k")["t"] is None


def test_the_move_is_the_first_frame_asked_whether_it_is_clean(monkeypatch, tmp_path):
    pf = _load()
    monkeypatch.setattr(pf, "find_action", lambda cut, d, base, key: {"t": 69.0, "what": "蹲下抱头"})
    monkeypatch.setattr(pf, "extract", lambda cut, t, path: "aW1n")
    replies = iter(["CLEAN", '{"danmaku": [], "person": [], "guest": []}'])
    monkeypatch.setattr(pf, "ask", lambda base, key, image, question, **kw: next(replies))
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    out = tmp_path / "out.json"
    pf.main(["pick_frame.py", "cut.mp4", str(tmp_path / "f"), str(out), "157.9,153.9"])
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["chosen"] == 69.0 and doc["action"]["t"] == 69.0
    assert doc["times"] == [157.9, 153.9] and doc["question"] == pf.READING_KEY    # the reuse key stays the caller's


def _row(cid, total, *, measured=True, action=False, veto=False):
    return {"id": cid, "route": "screenshot" if action else cid, "status": "RENDERED", "on_action": action,
            "score": {"status": "VETO", "vetoes": ["X"]} if veto else {"status": "ELIGIBLE", "total": total, "C": 1},
            "face_status": "MEASURED" if measured else "UNMEASURED", "face_px": 300, "repeat": False}


def test_a_passing_screenshot_of_the_move_goes_first_even_with_her_face_covered():
    ranked = cover_pick.rank([_row("high", 18), _row("shot", 12, measured=False, action=True), _row("low", 10)],
                             repeats=lambda r: False, target_px=400)
    assert [r["id"] for r in ranked] == ["shot", "high", "low"]


def test_a_vetoed_screenshot_of_the_move_is_not_chosen():
    ranked = cover_pick.rank([_row("high", 18), _row("shot", 12, action=True, veto=True)],
                             repeats=lambda r: False, target_px=400)
    assert [r["id"] for r in ranked] == ["high"]


def test_only_the_screenshot_at_the_moves_frame_counts():
    plan = {"action_frame": {"t": 69.0, "what": "蹲下抱头"}}
    shot = {"route": "screenshot"}
    assert on_action(shot, plan, 69.0)
    assert not on_action(shot, plan, 157.9)                          # a redo moved to another frame
    assert not on_action({"route": "ai_sticker"}, plan, 69.0)
    assert not on_action(shot, {"action_frame": None}, 69.0)


def _screenshot_route(tmp_path, job_extra):
    """The real cover_candidates.py screenshot route on a frame where the head reader finds no face."""
    from src.autoslice.manual_lane import stage_impl as si

    spec = importlib.util.spec_from_file_location("action_candidates", si.COVER_CANDIDATES_TOOL)
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    shot = tmp_path / "spec.json"
    shot.write_text(json.dumps({"id": "screenshot", "crop": [0, 0, 1920], "lines": [{"text": "a"}], "keep": []}), "utf-8")
    cover = object.__new__(cc.Run)
    cover.job = {"screenshot_spec": str(shot), **job_extra}
    cover.frame, cover.guest_boxes, cover.specs, cover.failed, cover.plans = "frame.png", [[0, 0, 1, 1]], [], [], {}
    cover.head = lambda: (None, None)
    cover.guest_in_frame = lambda: None
    cover.screenshot()
    return cover


def test_the_move_hiding_her_face_still_makes_the_screenshot(tmp_path):
    made = _screenshot_route(tmp_path, {"on_action": True, "host_box": [777, 479, 1213, 1080]})
    assert made.failed == [] and made.specs[0][0] == "screenshot"
    assert made.specs[0][4]["face"] == [777, 479, 1213, 479 + 601 * 2 // 5]      # the top of her figure stands for it


def test_without_the_move_a_frame_without_her_face_is_not_made(tmp_path):
    made = _screenshot_route(tmp_path, {"on_action": False, "host_box": [777, 479, 1213, 1080]})
    assert made.failed == [{"id": "screenshot", "route": "screenshot", "failure": "HOST_FACE_NOT_FOUND"}]
