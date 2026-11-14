"""Runner: resume by products, stop only on a failed step, --until, the explicit upload switch, PROCESS.md."""

from __future__ import annotations

import json

import pytest
from fakes import FakeCpa, FakeRemote, make_ctx

from src.autoslice.manual_lane.runner import (
    Step,
    StepOutcome,
    UploadNotAuthorized,
    check_upload_request,
    run_clip,
)
from src.autoslice.manual_lane.state import ClipState


def _step(number, key, outcome_factory, calls):
    def run(ctx):
        calls.append(key)
        return outcome_factory(ctx)
    return Step(number, key, f"step {key}", run)


def _ok_with_product(ctx, name):
    path = ctx.clip_dir / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")
    return StepOutcome(True, products={name: str(path)}, note=f"did {name}", process=[f"adopted {name}"])


def _ctx(tmp_path):
    return make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]))


def test_resume_skips_a_done_step_whose_products_exist_and_reruns_when_one_is_gone(tmp_path):
    ctx = _ctx(tmp_path)
    calls: list[str] = []
    steps = [_step(1, "a", lambda c: _ok_with_product(c, "a"), calls),
             _step(2, "b", lambda c: _ok_with_product(c, "b"), calls)]
    first = run_clip(ctx, steps)
    assert first.passed == [1, 2] and calls == ["a", "b"]

    calls.clear()
    second = run_clip(ctx, steps)
    assert second.reused == [1, 2] and calls == []

    (ctx.clip_dir / "a.txt").unlink()        # delete a product: that step runs again, nothing else is compared
    third = run_clip(ctx, steps)
    assert calls == ["a"] and third.passed == [1] and third.reused == [2]


def test_a_failed_step_stops_the_clip_and_later_steps_do_not_run(tmp_path):
    ctx = _ctx(tmp_path)
    calls: list[str] = []
    steps = [_step(1, "a", lambda c: StepOutcome(False, reason="CPA_CALL_FAILED", note="down"), calls),
             _step(2, "b", lambda c: _ok_with_product(c, "b"), calls)]
    report = run_clip(ctx, steps)
    assert report.stopped_at == 1 and report.reason == "CPA_CALL_FAILED"
    assert calls == ["a"]
    assert ctx.state.step("a")["status"] == "STOPPED"


def test_until_stops_after_that_step(tmp_path):
    ctx = _ctx(tmp_path)
    calls: list[str] = []
    steps = [_step(1, "a", lambda c: _ok_with_product(c, "a"), calls),
             _step(2, "b", lambda c: _ok_with_product(c, "b"), calls),
             _step(3, "c", lambda c: _ok_with_product(c, "c"), calls)]
    report = run_clip(ctx, steps, until=2)
    assert calls == ["a", "b"] and report.passed == [1, 2]


def test_a_crashing_step_is_a_tool_failure_with_a_trace_file(tmp_path):
    ctx = _ctx(tmp_path)

    def boom(_ctx):
        raise OSError("disk gone")

    report = run_clip(ctx, [Step(1, "a", "crash", boom)])
    assert report.stopped_at == 1 and report.reason == "STEP_EXCEPTION:OSError"
    assert (ctx.clip_dir / "error-step1.txt").exists()


def test_process_record_is_written_per_step(tmp_path):
    ctx = _ctx(tmp_path)
    run_clip(ctx, [Step(1, "a", "A 步骤", lambda c: StepOutcome(True, note="判定完成", process=["采用了 X"]))])
    text = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert "## 步骤 1 · A 步骤 · 完成" in text and "做了什么：判定完成" in text and "采用了 X" in text


def test_upload_step_is_not_reached_without_the_switch(tmp_path):
    ctx = _ctx(tmp_path)
    calls: list[str] = []
    steps = [_step(1, "a", lambda c: _ok_with_product(c, "a"), calls),
             _step(10, "upload", lambda c: StepOutcome(False, reason="UPLOAD_CHAIN_NOT_WIRED"), calls)]
    report = run_clip(ctx, steps)
    assert calls == ["a"] and report.halted_before_upload is True


def test_upload_requires_a_non_empty_reviewer_quote():
    with pytest.raises(UploadNotAuthorized):
        check_upload_request(True, None)
    with pytest.raises(UploadNotAuthorized):
        check_upload_request(True, "   ")
    check_upload_request(True, "维护者：可以上传")
    check_upload_request(False, None)


def test_state_records_status_products_and_cpa_without_hashes(tmp_path):
    ctx = _ctx(tmp_path)
    run_clip(ctx, [Step(1, "a", "A", lambda c: _ok_with_product(c, "a"))])
    raw = json.loads(ctx.state.path.read_text(encoding="utf-8"))
    rec = raw["steps"]["a"]
    assert set(rec) == {"status", "reason", "products", "cpa", "note", "at"}
    assert rec["status"] == "DONE" and "input_hashes" not in raw and "version" not in json.dumps(raw)
    assert ClipState.load(ctx.state.path).step("a")["status"] == "DONE"
