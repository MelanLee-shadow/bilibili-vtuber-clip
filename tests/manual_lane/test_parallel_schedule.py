"""Scheduling of the lane's steps and clips: independent steps overlap, the result is the serial one, state writes do not
lose updates, a failure stops what depends on it, every clip of a run goes at once (no cap of its own) and the media host's
CPU is one gate, one process per clip.

Concurrency is proven with events and barriers (a step waits until the other one is inside), never with timings; the fake
steps touch no remote, model or network."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fakes import FakeCpa, FakeRemote, make_ctx

from src.autoslice.manual_lane import cli, lane_rules, multi_run, parallel, runner
from src.autoslice.manual_lane.runner import Step, StepOutcome, run_clip
from src.autoslice.manual_lane.source_read import HOST_ENCODE
from src.autoslice.manual_lane.stages import DEPENDS, GATES, STEPS
from src.autoslice.manual_lane.state import ClipState, StateError, clip_run_lock

REPO = Path(__file__).resolve().parents[2]
WAIT_S = 10.0     # a barrier or an event that is not met in this time means the steps did not overlap

# the shape of the lane after the speaker step, with the real step numbers and keys
LANE = [(4, "speaker"), (5, "burn"), (6, "title_cover"), (7, "tags"), (8, "package"), (9, "review")]
LANE_DEPS = {4: (), 5: (4,), 6: (4,), 7: (6,), 8: (5, 6, 7), 9: (8,)}


def _ctx(tmp_path: Path):
    return make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]))


def _ok(ctx, name: str, **extra) -> StepOutcome:
    path = ctx.clip_dir / f"{name}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name, encoding="utf-8")
    return StepOutcome(True, products={name: str(path)}, note=f"did {name}", process=[f"adopted {name}"], **extra)


class Log:
    """The order in which steps started and ended, shared by threads."""

    def __init__(self):
        self.events: list[str] = []
        self._lock = threading.Lock()

    def add(self, event: str) -> None:
        with self._lock:
            self.events.append(event)

    def index(self, event: str) -> int:
        return self.events.index(event)


def _steps(log: Log, behave=None, *, shape=LANE) -> list[Step]:
    """One step per (number, key): logs start and end; `behave[key](ctx)` may replace the outcome (and block)."""
    behave = behave or {}

    def make(number: int, key: str) -> Step:
        def run(ctx):
            log.add(f"start {key}")
            outcome = behave[key](ctx) if key in behave else _ok(ctx, key)
            log.add(f"end {key}")
            return outcome
        return Step(number, key, f"步骤 {key}", run)

    return [make(number, key) for number, key in shape]


def _meet(parties: int) -> threading.Barrier:
    return threading.Barrier(parties, timeout=WAIT_S)


def _bare(clip_dir: Path) -> None:
    clip_dir.mkdir(parents=True, exist_ok=True)


# ----- the schedule -----------------------------------------------------------------------------------------
def test_steps_that_do_not_depend_on_each_other_run_at_once(tmp_path):
    ctx = _ctx(tmp_path)
    inside = _meet(3)

    def both(ctx):
        inside.wait()           # all three must be inside their bodies together, or the barrier breaks and the step fails
        return _ok(ctx, "x")

    steps = [Step(1, "a", "A", lambda c: _ok(c, "a")), Step(2, "b", "B", both), Step(3, "c", "C", both), Step(4, "d", "D", both)]
    report = run_clip(ctx, steps, deps={1: (), 2: (1,), 3: (1,), 4: (1,)})
    assert report.stopped_at is None and report.passed == [1, 2, 3, 4]


def test_a_step_waits_for_everything_it_depends_on(tmp_path):
    ctx = _ctx(tmp_path)
    log = Log()
    gate = threading.Event()

    def slow_burn(ctx):
        assert gate.wait(WAIT_S)
        return _ok(ctx, "burn")

    def title(ctx):
        gate.set()              # the title step ends first; the package must still wait for the burn
        return _ok(ctx, "title_cover")

    report = run_clip(ctx, _steps(log, {"burn": slow_burn, "title_cover": title}), deps=LANE_DEPS)
    assert report.passed == [4, 5, 6, 7, 8, 9]
    for dep in ("burn", "title_cover", "tags"):
        assert log.index(f"end {dep}") < log.index("start package")
    assert log.index("end title_cover") < log.index("start tags")      # tags read the title


def test_until_runs_only_the_steps_up_to_it(tmp_path):
    ctx = _ctx(tmp_path)
    log = Log()
    report = run_clip(ctx, _steps(log), until=6, deps=LANE_DEPS)
    assert sorted(e for e in log.events if e.startswith("start")) == ["start burn", "start speaker", "start title_cover"]
    assert report.passed == [4, 5, 6]
    assert ctx.state.step("tags") == {} and ctx.state.step("package") == {}


def test_resume_reuses_done_steps_and_runs_only_the_missing_one(tmp_path):
    ctx = _ctx(tmp_path)
    run_clip(ctx, _steps(Log()), deps=LANE_DEPS)
    (ctx.clip_dir / "burn.txt").unlink()
    log = Log()
    report = run_clip(ctx, _steps(log), deps=LANE_DEPS)
    assert log.events == ["start burn", "end burn"]
    assert report.passed == [5] and report.reused == [4, 6, 7, 8, 9]


def test_upload_step_is_halted_before_when_not_authorised(tmp_path):
    ctx = _ctx(tmp_path)
    log = Log()
    shape = [*LANE, (10, "upload")]
    report = run_clip(ctx, _steps(log, shape=shape), deps={**LANE_DEPS, 10: (9,)})
    assert report.halted_before_upload and "start upload" not in log.events and report.passed == [4, 5, 6, 7, 8, 9]


# ----- the result is the serial one -------------------------------------------------------------------------------
def _normalised(ctx, root: Path) -> tuple[dict, str]:
    data = json.loads(ctx.state.path.read_text(encoding="utf-8"))
    for key in ("updated_at", "created_at"):
        data.pop(key)
    for rec in data["steps"].values():
        rec.pop("at")
    process = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8") if (ctx.clip_dir / "PROCESS.md").exists() else ""
    return json.loads(json.dumps(data).replace(str(root), "<root>")), process.replace(str(root), "<root>")


def _both_ways(tmp_path, monkeypatch, behave_for):
    """The same fake steps run as a chain (the serial run) and by their dependencies; both results, normalised.
    `behave_for(overlapping)` gives the steps' behaviour; only the dependency run may make steps wait for each other."""
    monkeypatch.setattr(runner, "_stamp", lambda: "T")
    out = []
    for name, deps in (("serial", None), ("parallel", LANE_DEPS)):
        root = tmp_path / name
        ctx = _ctx(root)
        steps = _steps(Log(), behave_for(deps is not None), shape=LANE)
        report = run_clip(ctx, steps, deps=deps)
        out.append((report, *_normalised(ctx, root)))
    return out


def test_state_and_process_record_are_the_serial_ones_even_when_a_later_step_ends_first(tmp_path, monkeypatch):
    def behave_for(overlapping):
        title_done = threading.Event()

        def burn(ctx):                      # by dependencies it ends after the title step (the later list position)
            assert not overlapping or title_done.wait(WAIT_S)
            return StepOutcome(True, products={"burn": str(ctx.clip_dir / "burn.txt")}, note="did burn",
                               cpa=[{"call_id": "c1", "model": "m", "effort": "low", "rc": 0, "elapsed_s": 1.0}],
                               process=["adopted burn", "second line"])

        def title(ctx):
            outcome = _ok(ctx, "title_cover")
            title_done.set()
            return outcome

        return {"burn": burn, "title_cover": title}

    (s_report, s_state, s_process), (p_report, p_state, p_process) = _both_ways(tmp_path, monkeypatch, behave_for)
    assert (p_report.passed, p_report.reused, p_report.stopped_at) == (s_report.passed, s_report.reused, s_report.stopped_at)
    assert p_state == s_state and p_process == s_process
    sections = [line for line in p_process.splitlines() if line.startswith("## 步骤")]
    assert [s.split()[2] for s in sections] == ["4", "5", "6", "7", "8", "9"]


def test_failure_and_rewind_give_the_same_record_as_the_serial_run(tmp_path, monkeypatch):
    def behave_for(_overlapping):
        calls = {"review": 0}

        def review(ctx):
            calls["review"] += 1
            if calls["review"] == 1:        # the first review asks for the rebuild of burn, tags, package and itself
                return _ok(ctx, "review", rewind=("burn", "tags", "package", "review"))
            return _ok(ctx, "review")

        return {"review": review}

    (s_report, s_state, s_process), (p_report, p_state, p_process) = _both_ways(tmp_path, monkeypatch, behave_for)
    assert s_report.rewinds == ["burn", "tags", "package", "review"] == p_report.rewinds
    assert (p_report.passed, p_report.reused) == (s_report.passed, s_report.reused)
    assert p_state == s_state and p_process == s_process
    assert p_process.count("## 步骤 5 ") == 2 and p_process.count("## 步骤 6 ") == 1


# ----- state ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("round_no", range(50))
def test_two_steps_ending_together_both_reach_the_state_file(tmp_path, round_no):
    ctx = _ctx(tmp_path / str(round_no))
    ending = _meet(2)

    def end_together(name):
        def run(ctx):
            outcome = _ok(ctx, name)
            ending.wait()                   # both are about to be recorded at the same moment
            return outcome
        return run

    steps = [Step(1, "a", "A", lambda c: _ok(c, "a")), Step(2, "b", "B", end_together("b")), Step(3, "c", "C", end_together("c"))]
    run_clip(ctx, steps, deps={1: (), 2: (1,), 3: (1,)})
    saved = ClipState.load(ctx.state.path)
    assert [saved.step(k).get("status") for k in "abc"] == ["DONE"] * 3
    assert not ctx.state.path.with_name(ctx.state.path.name + ".tmp").exists()


def test_concurrent_records_and_saves_of_one_state_lose_nothing(tmp_path):
    ctx = _ctx(tmp_path)
    go = _meet(16)

    def hammer(n):
        go.wait()
        for i in range(25):
            ctx.state.record(f"s{n}-{i}", ok=True, reason="", products={}, cpa=[], note="")

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ClipState.load(ctx.state.path).data["steps"]) == 16 * 25


def test_a_second_process_running_the_same_clip_is_refused(tmp_path):
    task = tmp_path / "task"
    ClipState.create(task / "state/c.json", clip={"cid": "c", "remote_dir": str(tmp_path / "media")}, session={})
    code = ("import sys, pathlib\n"
            "from src.autoslice.manual_lane.state import clip_run_lock\n"
            "with clip_run_lock(pathlib.Path(sys.argv[1])):\n"
            "    print('held', flush=True)\n"
            "    sys.stdin.readline()\n")
    holder = subprocess.Popen([sys.executable, "-c", code, str(task / "state/c.json")], cwd=REPO,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(StateError, match="already being run by another process"):
            with clip_run_lock(task / "state/c.json"):
                pytest.fail("the lock was given to a second process")
        assert cli.main(["run", "--task-dir", str(task), "--cid", "c"]) == 2
    finally:
        holder.stdin.close()
        holder.wait(timeout=WAIT_S)
    with clip_run_lock(task / "state/c.json"):       # released with the first process
        pass


# ----- failure --------------------------------------------------------------------------------------------------------
def test_a_failed_step_lets_its_sibling_finish_and_stops_what_depends_on_it(tmp_path):
    ctx = _ctx(tmp_path)
    log = Log()
    failed = threading.Event()

    def title(ctx):
        failed.set()
        return StepOutcome(False, reason="TITLE_FAILED", note="no title")

    def burn(ctx):
        assert failed.wait(WAIT_S)          # the sibling is still running when step 6 has already failed
        return _ok(ctx, "burn")

    report = run_clip(ctx, _steps(log, {"title_cover": title, "burn": burn}), deps=LANE_DEPS)
    assert (report.stopped_at, report.reason) == (6, "TITLE_FAILED")
    assert report.passed == [4, 5]
    assert ctx.state.step("burn")["status"] == "DONE" and ctx.state.step("title_cover")["status"] == "STOPPED"
    assert "start tags" not in log.events and "start package" not in log.events
    text = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert [l.split()[2] for l in text.splitlines() if l.startswith("## 步骤")] == ["4", "5", "6"]


def test_when_two_steps_fail_the_lowest_step_number_is_reported(tmp_path):
    ctx = _ctx(tmp_path)
    six_failed = threading.Event()

    def title(ctx):
        six_failed.set()
        return StepOutcome(False, reason="SIX")

    def burn(ctx):
        assert six_failed.wait(WAIT_S)
        return StepOutcome(False, reason="FIVE")

    report = run_clip(ctx, _steps(Log(), {"title_cover": title, "burn": burn}), deps=LANE_DEPS)
    assert (report.stopped_at, report.reason) == (5, "FIVE")


def test_a_crashing_step_in_a_parallel_run_is_a_tool_failure_with_its_trace(tmp_path):
    ctx = _ctx(tmp_path)

    def boom(_ctx):
        raise OSError("disk gone")

    report = run_clip(ctx, _steps(Log(), {"burn": boom}), deps=LANE_DEPS)
    assert (report.stopped_at, report.reason) == (5, "STEP_EXCEPTION:OSError")
    assert (ctx.clip_dir / "error-step5.txt").exists()


# ----- the declaration ----------------------------------------------------------------------------------------------
def test_every_step_declares_its_dependencies_on_earlier_steps():
    """A new step added to STEPS without a line in DEPENDS (or depending on a later step) fails here."""
    numbers = [s.number for s in STEPS]
    assert sorted(DEPENDS) == sorted(numbers)
    position = {n: i for i, n in enumerate(numbers)}
    for number, deps in DEPENDS.items():
        assert all(dep in position and position[dep] < position[number] for dep in deps), (number, deps)
    assert set(GATES) <= set(numbers)
    for row in set(GATES.values()):
        assert parallel.limit(row) >= 1
    runner._needs(STEPS, DEPENDS)       # the runner accepts the table


def test_the_runner_refuses_a_table_that_misses_a_step_or_points_forward(tmp_path):
    steps = [Step(1, "a", "A", lambda c: _ok(c, "a")), Step(2, "b", "B", lambda c: _ok(c, "b"))]
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="no dependency declaration"):
        run_clip(ctx, steps, deps={1: ()})
    with pytest.raises(ValueError, match="not an earlier step"):
        run_clip(ctx, steps, deps={1: (2,), 2: ()})


# ----- bounds shared by every clip of the process --------------------------------------------------------------------
def _limits(monkeypatch, **rows: int) -> None:
    monkeypatch.setattr(parallel, "limit", lambda name: rows.get(name, 1))


class Peak:
    """In-flight counter that holds the first entrants until `want` are inside (so the peak is reached, not hoped for)."""

    def __init__(self, want: int):
        self.now = self.peak = 0
        self.reached = threading.Event()
        self.want = want
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            if self.now >= self.want:
                self.reached.set()
        self.reached.wait(WAIT_S)

    def leave(self) -> None:
        with self._lock:
            self.now -= 1


class Held:
    """A gate probe: every entrant waits inside until `release` is set; `full` is set when `cap` are inside, `over` when one
    more than that is.  The test releases only after `full`, so a gate that lets a third in shows as `over` (the 'no third
    came in' check waits a short while: only the passing direction can be slow, a leak shows at once)."""

    def __init__(self, cap: int):
        self.cap, self.now, self.peak = cap, 0, 0
        self.full, self.over, self.release = threading.Event(), threading.Event(), threading.Event()
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            if self.now >= self.cap:
                self.full.set()
            if self.now > self.cap:
                self.over.set()
        self.release.wait(WAIT_S)

    def leave(self) -> None:
        with self._lock:
            self.now -= 1

    def settle(self, threads: list[threading.Thread]) -> None:
        """Wait until `cap` are inside, check that no more come in meanwhile, let everything go and join the threads."""
        assert self.full.wait(WAIT_S)
        assert not self.over.wait(0.5)
        self.release.set()
        for t in threads:
            t.join()


def test_the_provider_bound_is_shared_by_all_clips_of_the_process(monkeypatch):
    _limits(monkeypatch, cpa=3)
    meter = Peak(3)

    def call(_item):
        meter.enter()
        meter.leave()

    def clip():
        parallel.run(range(8), call, provider="cpa")

    threads = [threading.Thread(target=clip) for _ in range(3)]        # three clips, each wanting up to 3 at once
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert meter.reached.is_set() and meter.peak == 3


def test_a_call_cannot_ask_for_a_slot_of_the_provider_it_holds(monkeypatch):
    _limits(monkeypatch, cpa=2)

    def outer(_item):
        parallel.run([1], lambda _x: None, provider="cpa")

    with pytest.raises(RuntimeError, match="holds a 'cpa' slot"):
        parallel.run([0], outer, provider="cpa")


def test_the_host_cpu_steps_share_one_gate_row_and_the_rules_file_has_no_clip_cap():
    assert HOST_ENCODE == "host_encode" and GATES == {2: HOST_ENCODE, 5: HOST_ENCODE}       # cut from the source, and burn
    row = json.loads((REPO / "assets/manual_lane/lane_rules.json").read_text(encoding="utf-8"))["concurrency"]
    assert "clips" not in row and "burn" not in row and row["host_encode"]["limit"] >= 1
    assert "clips" not in lane_rules.rules("concurrency")


def test_the_host_encode_gate_bounds_how_many_clips_are_in_step_2_or_5_at_once(tmp_path, monkeypatch):
    _limits(monkeypatch, host_encode=2)
    meter = Held(2)

    def heavy(key):
        def run(ctx):
            meter.enter()                  # inside the gate: two are in at once, then they leave and the next two go in
            meter.leave()
            return _ok(ctx, key)
        return run

    steps = [Step(2, "cut", "C", heavy("cut")), Step(5, "burn", "B", heavy("burn"))]
    results: list[runner.RunReport] = []

    def clip(n):
        results.append(run_clip(_ctx(tmp_path / str(n)), steps, deps={2: (), 5: (2,)}, gates=GATES))

    threads = [threading.Thread(target=clip, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    meter.settle(threads)
    assert meter.peak == 2 and len(results) == 6 and all(r.passed == [2, 5] for r in results)


# ----- several clips ----------------------------------------------------------------------------------------------------
def test_six_clips_are_all_driven_at_once_there_is_no_cap_on_the_number_of_clips(tmp_path):
    meter = Peak(6)               # every clip waits inside until all six are inside: a cap below six never reaches it

    def run_one(cid, progress):
        meter.enter()
        meter.leave()
        return 0

    code = multi_run.run_many([f"c{i}" for i in range(6)], run_one, log_dir=tmp_path / "logs", errors=(ValueError,))
    assert code == 0 and meter.reached.is_set() and meter.peak == 6


def test_run_with_six_clips_has_all_six_in_a_step_together(tmp_path, monkeypatch):
    task = _task(tmp_path, [f"c{i}" for i in range(6)])
    together = threading.Barrier(6)

    def step(ctx):
        together.wait(WAIT_S)         # BrokenBarrierError (a failed step) unless all six clips are inside the step at once
        return _ok(ctx, "speaker")

    monkeypatch.setattr(cli, "STEPS", (Step(4, "speaker", "步骤 speaker", step),))
    monkeypatch.setattr(cli, "DEPENDS", {4: ()})
    monkeypatch.setattr(cli, "GATES", {})
    args = ["run", "--task-dir", str(task)]
    assert cli.main([*args, *(a for i in range(6) for a in ("--cid", f"c{i}"))]) == 0
    assert all(ClipState.load(task / "state" / f"c{i}.json").step("speaker")["status"] == "DONE" for i in range(6))


def _task(tmp_path, cids) -> Path:
    task = tmp_path / "task"
    for cid in cids:
        ClipState.create(task / "state" / f"{cid}.json", clip={"cid": cid, "remote_dir": str(tmp_path / "media" / cid)}, session={})
    return task


def _lane(monkeypatch, fail_clip: str | None = None) -> None:
    def make(number, key):
        def run(ctx):
            if key == "burn" and ctx.state.data["clip"]["cid"] == fail_clip:
                return StepOutcome(False, reason="BURN_FAILED")
            return _ok(ctx, key)
        return Step(number, key, f"步骤 {key}", run)

    monkeypatch.setattr(cli, "STEPS", tuple(make(n, k) for n, k in LANE))
    monkeypatch.setattr(cli, "DEPENDS", LANE_DEPS)
    monkeypatch.setattr(cli, "GATES", {})


def test_several_clips_one_fails_the_others_finish_and_the_exit_code_is_the_worst(tmp_path, monkeypatch, capsys):
    task = _task(tmp_path, ["a", "b", "c"])
    _lane(monkeypatch, fail_clip="b")
    code = cli.main(["run", "--task-dir", str(task), "--cid", "a", "--cid", "b", "--cid", "c"])
    out = capsys.readouterr().out
    assert code == 1
    status = {cid: [ClipState.load(task / "state" / f"{cid}.json").step(k).get("status") for _n, k in LANE] for cid in "abc"}
    assert status["a"] == status["c"] == ["DONE"] * 6
    assert status["b"][:2] == ["DONE", "STOPPED"] and status["b"][4:] == [None, None]     # nothing after the failed burn
    assert "[b] 步骤 5 步骤 burn 停止（BURN_FAILED）" in out and "[a] 步骤 9 步骤 review 完成" in out
    for cid in "abc":
        log = next((task / "run-logs").glob(f"{cid}-*.log"))
        assert str(log) in out
        result = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        assert result["cid"] == cid and result["stopped_at"] == (5 if cid == "b" else None)
    assert '"passed"' not in out          # the clips' result lines are in their logs, the terminal has the step lines


def test_a_clip_that_cannot_be_started_ends_with_code_two_and_the_others_still_run(tmp_path, monkeypatch, capsys):
    task = _task(tmp_path, ["a", "c"])
    _lane(monkeypatch)
    code = cli.main(["run", "--task-dir", str(task), "--cid", "a", "--cid", "missing", "--cid", "c"])
    assert code == 2
    assert all(ClipState.load(task / "state" / f"{cid}.json").step("review")["status"] == "DONE" for cid in "ac")
    assert "error:" in next((task / "run-logs").glob("missing-*.log")).read_text(encoding="utf-8")


def test_one_clip_prints_exactly_what_it_printed_before(tmp_path, monkeypatch, capsys):
    task = _task(tmp_path, ["a"])
    _lane(monkeypatch)
    assert cli.main(["run", "--task-dir", str(task), "--cid", "a"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["passed"] == [4, 5, 6, 7, 8, 9]
    assert not (task / "run-logs").exists()


def test_cids_file_and_repeated_cids_are_one_list_without_repeats(tmp_path):
    listing = tmp_path / "cids.txt"
    listing.write_text("b\n\n c \n", encoding="utf-8")
    assert multi_run.clip_ids(["a"], str(listing)) == ["a", "b", "c"]
    with pytest.raises(ValueError, match="more than once"):
        multi_run.clip_ids(["a", "a"], None)
    with pytest.raises(ValueError, match="needs --cid"):
        multi_run.clip_ids(None, None)
