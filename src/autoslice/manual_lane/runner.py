"""Step runner: steps start when their dependencies are done, resume by products, stop on a tool failure, explicit upload
switch, rewind on revision.

Without a dependency table the steps are a chain in list order (the serial run).  With one (stages.DEPENDS) a step starts
as soon as the steps it depends on are done, so steps that do not depend on each other run at once, in threads (a step is
mostly waiting for a remote command or a model).  The result is the serial one: the same state file, products and
PROCESS.md (each step's section is written when it ends but held back until every earlier step of the list has ended, so
the file keeps list order), and the same verdict.  When a step fails the steps already running finish, nothing new starts,
and the failure reported is the one with the lowest step number.

A step is skipped on resume only when the state records it DONE and every product it recorded still exists.
A step stops only when a tool fails or input is missing; judgements made by
CPA are adopted as they are.  A step may ask to rewind (StepOutcome.rewind): the named steps are forgotten and the
run continues from the earliest of them, so the affected stages are produced again.  state.rewind records who asked
and for which steps; the asking step sees it when it runs again, and any step outside that set running in between
drops the record (the products are no longer the rebuild the step asked for).
"""

from __future__ import annotations

import datetime as dt
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import Adapters
from .config import SessionBlock
from .parallel import slot
from .state import ClipState, CpaEvent

UPLOAD_STEP = 10


class UploadNotAuthorized(RuntimeError):
    """Raised when --upload is requested without an 维护者 authorization quote."""


@dataclass
class StepOutcome:
    ok: bool
    reason: str = ""                                      # set only when a tool failed or an input is missing
    products: dict[str, str] = field(default_factory=dict)  # name -> local path; resume checks these exist
    cpa: list[CpaEvent] = field(default_factory=list)
    note: str = ""
    process: list[str] = field(default_factory=list)      # what CPA said and what was adopted (PROCESS.md)
    rewind: tuple[str, ...] = ()                          # step keys to forget; the run continues from the earliest


@dataclass
class Ctx:
    state: ClipState
    session: SessionBlock
    clip_dir: Path          # Mac-side clip directory: products, CPA records, PROCESS.md
    remote_dir: str         # OCI3 working directory for this clip
    adapters: Adapters
    source_dir: str | None = None   # this run's --source-dir: looked in after the clip's recorded directory (source_read)


@dataclass(frozen=True)
class Step:
    number: int
    key: str
    title: str
    run: Callable[[Ctx], StepOutcome]


@dataclass
class RunReport:
    passed: list[int] = field(default_factory=list)
    reused: list[int] = field(default_factory=list)
    rewinds: list[str] = field(default_factory=list)
    stopped_at: int | None = None
    reason: str = ""
    halted_before_upload: bool = False


def check_upload_request(upload: bool, quote: str | None) -> None:
    if upload and not (quote or "").strip():
        raise UploadNotAuthorized("--upload requires a non-empty 维护者 authorization quote (--quote)")


def _products_exist(state: ClipState, key: str) -> bool:
    products = state.step(key).get("products") or {}
    return all(Path(p).exists() for p in products.values())


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def process_text(step: Step, outcome: StepOutcome) -> str:
    """One step's section of PROCESS.md: what was done, what CPA said, what was adopted."""
    status = "完成" if outcome.ok else f"停止（{outcome.reason}）"
    if outcome.ok and outcome.rewind:
        status += f"，回退重跑 {','.join(outcome.rewind)}"
    lines = [f"\n## 步骤 {step.number} · {step.title} · {status} · {_stamp()}", ""]
    if outcome.note:
        lines.append(f"做了什么：{outcome.note}")
    for call in outcome.cpa:
        # TASK-37: `effort` is the text-call thinking tier.  The cover's image edits and vision readings are made by
        # pick_frame.py / cover_candidates.py on OCI3, not through _ask_cpa: they are not text calls and have no tier,
        # so the line says that instead of implying nobody registered them.
        tier = call.get("effort") or ("非文本调用（无思考档位）" if call.get("kind") else "未记录")
        lines.append(f"- CPA 调用 `{call.get('call_id')}`：模型 {call.get('model')}，"
                     f"档位 {tier}，返回码 {call.get('rc')}，"
                     f"耗时 {call.get('elapsed_s')} s")
        if call.get("effort_note"):
            # TASK-37 (U168): a purpose nobody registered ran at the default tier — say so, never silently
            lines.append(f"- {call['effort_note']}")
    lines.extend(f"- {item}" for item in outcome.process)
    return "\n".join(lines) + "\n"


def _append_process(ctx: Ctx, text: str) -> None:
    path = ctx.clip_dir / "PROCESS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def write_process(ctx: Ctx, step: Step, outcome: StepOutcome) -> None:
    """PROCESS.md: a human-readable record of each step: what was done, what CPA said, what was adopted."""
    _append_process(ctx, process_text(step, outcome))


def _needs(steps: Sequence[Step], deps: Mapping[int, Sequence[int]] | None) -> list[set[int]]:
    """Per list index, the list indexes it waits for.  No table: each step waits for the one before it."""
    if deps is None:
        return [{i - 1} if i else set() for i in range(len(steps))]
    index = {step.number: i for i, step in enumerate(steps)}
    out: list[set[int]] = []
    for i, step in enumerate(steps):
        if step.number not in deps:
            raise ValueError(f"step {step.number} has no dependency declaration")
        for dep in deps[step.number]:
            if dep not in index or index[dep] >= i:
                raise ValueError(f"step {step.number} depends on {dep}, which is not an earlier step of the list")
        out.append({index[dep] for dep in deps[step.number]})
    return out


def _execute(ctx: Ctx, step: Step, gate: str | None) -> StepOutcome:
    """Run one step and record it in the state (in the step's thread).  A crash is a tool failure with its traceback."""
    state = ctx.state
    with state.lock:
        pending = state.data.get("rewind")
        if pending and step.key not in pending.get("steps", ()):
            # a step outside the rebuild a step asked for is running: whatever that step kept about the rebuild
            # (the review's adopted fixes) no longer describes the products, so it is dropped
            state.data.pop("rewind", None)
            state.save()
    try:
        with slot(gate) if gate else nullcontext():
            outcome = step.run(ctx)
    except Exception as exc:  # noqa: BLE001 - the runner's crash boundary: any crash of a step is a tool
        # failure, recorded with its traceback, and the clip stops there (never swallowed)
        ctx.clip_dir.mkdir(parents=True, exist_ok=True)
        trace = ctx.clip_dir / f"error-step{step.number}.txt"
        trace.write_text(traceback.format_exc(), encoding="utf-8")
        outcome = StepOutcome(False, reason=f"STEP_EXCEPTION:{type(exc).__name__}",
                              note=f"{exc!s:.300} (traceback: {trace})")
    state.record(step.key, ok=outcome.ok, reason=outcome.reason, products=outcome.products,
                 cpa=outcome.cpa, note=outcome.note)
    return outcome


class _Schedule:
    """One run of run_clip: which steps are settled, which are running, the PROCESS.md sections waiting for their turn."""

    def __init__(self, ctx: Ctx, steps: Sequence[Step], needs: list[set[int]], last: int, *, upload: bool,
                 gates: Mapping[int, str], progress: Callable[[Step, str, StepOutcome | None], None] | None):
        self.ctx, self.order, self.needs, self.last = ctx, steps, needs, last
        self.upload, self.gates, self.progress = upload, gates, progress
        self.report = RunReport()
        self.settled: set[int] = set()       # done in this run or reused: what later steps may start after
        self.ended: set[int] = set()         # settled, or ended in a failure / rewind request
        self.running: dict[Future[StepOutcome], int] = {}
        self.events: list[tuple[int, int, str, int]] = []    # (round, list index, "passed"/"reused", number)
        self.held: dict[int, str] = {}       # PROCESS.md sections waiting for the earlier steps of the list
        self.failures: list[tuple[int, str]] = []
        self.rewind: tuple[int, StepOutcome] | None = None
        self.halted = False
        self.round = 0

    @property
    def stopping(self) -> bool:
        return bool(self.failures) or self.halted or self.rewind is not None

    def launch_ready(self, pool: ThreadPoolExecutor) -> None:
        """Reuse the done steps and start the others whose dependencies are settled, in list order."""
        state = self.ctx.state
        busy = set(self.running.values())
        for i in range(self.last + 1):
            if i in self.settled or i in busy or not self.needs[i] <= self.settled:
                continue
            step = self.order[i]
            if step.number == UPLOAD_STEP and not self.upload:
                self.report.halted_before_upload = self.halted = True
                return
            if state.step(step.key).get("status") == "DONE" and _products_exist(state, step.key):
                self.events.append((self.round, i, "reused", step.number))
                self.settled.add(i)
                self.ended.add(i)
                continue
            if self.progress:
                self.progress(step, "start", None)
            self.running[pool.submit(_execute, self.ctx, step, self.gates.get(step.number))] = i

    def collect(self, finished: Iterable[Future[StepOutcome]]) -> None:
        state = self.ctx.state
        for future in sorted(finished, key=lambda f: self.running[f]):
            i = self.running.pop(future)
            step, outcome = self.order[i], future.result()
            if self.progress:
                self.progress(step, "end", outcome)
            self.held[i] = process_text(step, outcome)
            self.ended.add(i)
            if not outcome.ok:
                self.failures.append((step.number, outcome.reason))
            elif outcome.rewind:
                self.rewind = self.rewind or (i, outcome)
            else:
                with state.lock:
                    if state.data.get("rewind", {}).get("by") == step.key:   # the asking step ran again: rebuild done
                        state.data.pop("rewind", None)
                        state.save()
                self.events.append((self.round, i, "passed", step.number))
                self.settled.add(i)

    def apply_rewind(self) -> None:
        """The asking step has ended and nothing else runs: forget the named steps and continue from the earliest."""
        assert self.rewind is not None
        asking, outcome = self.rewind
        targets = [i for i, s in enumerate(self.order) if s.key in outcome.rewind]
        state = self.ctx.state
        with state.lock:
            for key in outcome.rewind:
                state.data["steps"].pop(key, None)
            state.data["rewind"] = {"by": self.order[asking].key, "steps": list(outcome.rewind)}
            state.save()
        self.report.rewinds.extend(outcome.rewind)
        self.flush(final=True)
        restart = min(targets)
        keep = {i for i in self.settled if i < restart}
        kept_ended = {i for i in self.ended if i < restart}
        if asking < restart:        # it is not run again, and not counted as passed (as in the serial run)
            keep.add(asking)
            kept_ended.add(asking)
        self.settled, self.ended = keep, kept_ended
        self.rewind, self.round = None, self.round + 1

    def flush(self, final: bool = False) -> None:
        """Write the held PROCESS.md sections in list order; one waits while an earlier step may still produce its own."""
        busy = set(self.running.values())
        for i in sorted(self.held):
            if not final and any(j < i and j not in self.ended and (j in busy or not self.stopping) for j in range(i)):
                break
            _append_process(self.ctx, self.held.pop(i))

    def finish(self) -> RunReport:
        self.flush(final=True)
        self.events.sort(key=lambda e: e[:2])
        self.report.passed = [number for _r, _i, kind, number in self.events if kind == "passed"]
        self.report.reused = [number for _r, _i, kind, number in self.events if kind == "reused"]
        if self.failures:
            self.report.stopped_at, self.report.reason = min(self.failures)
        return self.report


def run_clip(ctx: Ctx, steps: Sequence[Step], *, until: int | None = None,
             upload: bool = False, quote: str | None = None,
             deps: Mapping[int, Sequence[int]] | None = None, gates: Mapping[int, str] | None = None,
             progress: Callable[[Step, str, StepOutcome | None], None] | None = None) -> RunReport:
    """Run the clip's steps (see the module docstring).  `until` is the last step to run, as in the list order;
    `gates` names, per step number, the concurrency row bounding how many clips run that step at once; `progress` is
    told ("start", step, None) and ("end", step, outcome) from the calling thread."""
    check_upload_request(upload, quote)
    order = list(steps)
    last = next((i for i, s in enumerate(order) if s.number == until), len(order) - 1)
    run = _Schedule(ctx, order, _needs(order, deps), last, upload=upload, gates=gates or {}, progress=progress)
    with ThreadPoolExecutor(max_workers=max(1, last + 1), thread_name_prefix="lane-step") as pool:
        while True:
            if not run.stopping:
                run.launch_ready(pool)
            if run.running:
                finished, _ = wait(list(run.running), return_when=FIRST_COMPLETED)
                run.collect(finished)
                run.flush()
            elif run.rewind is not None and not run.failures:    # a failure stops the clip; the rewind is not carried out
                run.apply_rewind()
            else:
                break
    return run.finish()
