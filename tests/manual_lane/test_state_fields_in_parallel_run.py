"""The state fields later batches added (recording directory, the import's owner_edited, the dissent register, revised_from,
imported_cover) survive a run whose steps overlap: the steps save the one in-memory state under its lock, so a record or a
save made while another step ends loses none of them, and what a late step adds (dissent) reaches the file."""

from __future__ import annotations

import threading

from fakes import FakeCpa, FakeRemote, make_ctx

from src.autoslice.manual_lane import dissent_register
from src.autoslice.manual_lane.runner import Step, StepOutcome, run_clip
from src.autoslice.manual_lane.state import ClipState

WAIT_S = 10.0
FIELDS = ("imported", "imported_cover", "revised_from", "dissent")


def _fields(data) -> dict:
    return {"source_dir": data["clip"]["source_dir"], **{k: data[k] for k in FIELDS}}


def test_the_new_state_fields_reach_the_file_when_steps_end_together_and_a_late_step_adds_to_them(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]))
    data = ctx.state.data
    data["clip"]["source_dir"] = "/media/rec"
    data["imported"] = {"source": "push", "decided_by": "owner", "owner_edited": [2, 5]}
    data["imported_cover"] = {"lines": ["甲", "乙"], "decided_by": "push", "png_sha256": "00"}
    data["revised_from"] = {"cid": "clip0", "reason": "r", "at": "T"}
    data["dissent"] = []
    ctx.state.save()
    meet = threading.Barrier(2, timeout=WAIT_S)

    def together(name):
        def run(c):
            meet.wait()                     # both end, and are recorded, at the same moment
            return StepOutcome(True, note=name)
        return run

    def review(c):
        dissent_register.add(c.state, [{"kind": "cover", "cue": None, "text": "甲 / 乙", "proposed": "丙", "value": "丙", "reason": "r"}])
        return StepOutcome(True, note="review")

    steps = [Step(4, "speaker", "s", lambda c: StepOutcome(True)), Step(5, "burn", "b", together("burn")),
             Step(6, "title_cover", "t", together("title_cover")), Step(9, "review", "r", review)]
    report = run_clip(ctx, steps, deps={4: (), 5: (4,), 6: (4,), 9: (5, 6)})
    assert report.stopped_at is None
    saved = ClipState.load(ctx.state.path)
    assert _fields(saved.data) == _fields(ctx.state.data)
    assert saved.data["imported"]["owner_edited"] == [2, 5] and saved.data["clip"]["source_dir"] == "/media/rec"
    assert [d["n"] for d in dissent_register.items(saved)] == [1]


def test_reviewed_tags_are_not_written_into_the_state_while_another_step_is_saving_it(tmp_path, monkeypatch):
    import json
    import types

    from src.autoslice.manual_lane import state as state_module
    from src.autoslice.manual_lane.step7_tags import apply_review_tags

    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]))
    tags = ctx.clip_dir / "tags/tags.json"
    tags.parent.mkdir(parents=True, exist_ok=True)
    tags.write_text(json.dumps({"final_tags": ["甲", "乙", "丙"], "final_tag_line": "甲,乙,丙"}), encoding="utf-8")
    saving, seen_during_save = threading.Event(), []
    real = json

    def slow_dumps(obj, **kw):
        if threading.current_thread().name == "other-step":
            saving.set()
            threading.Event().wait(0.4)          # the other step's save is under way: the review step writes now
            seen_during_save.append("reviewed_tags" in obj)
        return real.dumps(obj, **kw)

    monkeypatch.setattr(state_module, "json", types.SimpleNamespace(dumps=slow_dumps, loads=real.loads))
    other = threading.Thread(target=lambda: ctx.state.record("burn", ok=True, reason="", products={}, cpa=[], note=""),
                             name="other-step")
    other.start()
    assert saving.wait(WAIT_S)
    apply_review_tags(ctx, ["甲", "乙", "新"])      # waits for the other step's save to end
    other.join()
    assert seen_during_save == [False]
    monkeypatch.undo()
    saved = ClipState.load(ctx.state.path)
    assert saved.data["reviewed_tags"]["kept"] == ["甲", "乙", "新"] and saved.step("burn")["status"] == "DONE"
