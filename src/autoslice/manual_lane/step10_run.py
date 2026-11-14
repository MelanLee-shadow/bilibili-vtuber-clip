"""Step 10: put the sealed package online by the upload tool, the way the nine-clip hand run did, and read back what is online.

Order (step10_commands has the commands): package audit -> upload manifest -> verify the manifest -> [a revision: plan dry run
(its plan is compared with the package and revision.json, step10_check) -> plan -> status -> run dry run -> run until the tool
says done -> verify live, which writes the completion receipt] | [a first publication: upload] -> read back (step10_readback)
-> online.json.  The road is chosen from the clip's own facts (step10_route).

Waiting is part of the step.  `run` answers 6 (still in progress): the same command is run again after
`upload.wait.poll_interval_s`, at most `max_polls` times; then the step stops at UPLOAD_WAITING_LIVE and the next `run` only
polls on (the plan is never made again).  An upload refused for quota (8) stops at UPLOAD_WAITING_QUOTA with the earliest time to
retry; one refused because another upload holds the tool's lock is run again after the interval (UPLOAD_WAITING_LOCK when the polls
are used up).  Any other unexpected return code fails the step with the tool's own words.  Progress is kept in upload/upload_state.json:
the phases done, the commands run with their return codes and times, the road, and the package seal it was started for.

Before anything is sent: lane_rules `upload` filled in (UPLOAD_RULES_UNSET), no hold on the clip (UPLOAD_HELD: `<clip>/upload/HOLD`,
put there by whoever looked at it), the package sealed, the cover without a vetoed-material
flag.  The audit is the first command; the tool's own gates do the rest."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from . import step10_commands as commands
from . import step10_readback as readback
from . import step10_route
from .adapters import RemoteResult
from .runner import Ctx, StepOutcome
from .step10_check import PLAN_MISMATCH, Stop, cover_material, held, plan_problems, read_json, sealed, sha, stamp, stop, tail
from .step10_route import FIRST, NONE, Route, RouteRefused
from .step10_rules import RULES_UNSET, UploadRules, UploadRulesUnset
from .step10_rules import load as load_rules
from .step_io import _fail, _run_remote

STATE_FILE, ONLINE_FILE, SCHEMA = "upload_state.json", "online.json", "manual-lane.upload-state.v1"
COMMAND_FAILED = "UPLOAD_COMMAND_FAILED"
AUDIT_FAILED = "UPLOAD_AUDIT_FAILED"
PACKAGE_UNREADABLE = "UPLOAD_PACKAGE_UNREADABLE"
WAITING_LIVE = "UPLOAD_WAITING_LIVE"
WAITING_QUOTA = "UPLOAD_WAITING_QUOTA"
WAITING_LOCK = "UPLOAD_WAITING_LOCK"
LOCK_BUSY = "shared upload/repair lock is busy"      # authorized_upload.py UploadLockBusy: refused before anything starts
ATTEMPT_UNRESOLVED = "UPLOAD_ATTEMPT_UNRESOLVED"
PACKAGE_CHANGED = "UPLOAD_PACKAGE_CHANGED"
READBACK_MISMATCH = "UPLOAD_READBACK_MISMATCH"

_sleep = time.sleep         # a test replaces these two
_epoch = time.time


def _at(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Run:
    def __init__(self, ctx: Ctx, package: Path):
        self.ctx, self.package = ctx, package
        self.out = ctx.clip_dir / "upload"
        self.rules: UploadRules
        self.route: Route | None = None
        self.job: commands.Job
        self.state: dict[str, Any] = {"commands": []}
        self.last = RemoteResult(0, "", "")

    # ----- state and commands ------------------------------------------------------------------------------------------
    def _save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        tmp = self.out / (STATE_FILE + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.out / STATE_FILE)

    def _mark(self, phase: str) -> None:
        self.state["done"].append(phase)
        self._save()

    def _cmd(self, phase: str, body: str, log: str | None, *, accept: tuple[int, ...] | None = (0,), timeout: int = 1800) -> int:
        """Send one script and record it (return code, start, end).  A return code outside `accept` stops the step with the tool's
        words; `accept=None` returns whatever came back for the caller to judge."""
        started = stamp()
        result = self.last = _run_remote(self.ctx, f"s10-{phase.split('#')[0]}", body, timeout=timeout)
        self.state["commands"].append({"command": phase, "rc": result.rc, "started": started, "finished": stamp()})
        self._save()
        if accept is not None and result.rc not in accept:
            words = (tail(self.out / log) if log else "") or (result.stdout + result.stderr)[-700:].strip()
            stop(COMMAND_FAILED, f"{phase} 返回码 {result.rc}（预期 {'/'.join(map(str, accept))}），不重试、不换路线：{words}")
        return result.rc

    def _once(self, phase: str, body: str, log: str, accept: tuple[int, ...] = (0,)) -> None:
        if phase not in self.state["done"]:
            self._cmd(phase, body, log, accept=accept)
            self._mark(phase)

    def _prepare(self, seal: str, route: Route) -> None:
        try:
            state = read_json(self.out / STATE_FILE)
        except (OSError, ValueError):
            state = {}
        if state.get("schema") == SCHEMA and state.get("seal_sha256") != seal:
            if "plan" in state["done"] or state.get("upload_started_at"):
                stop(PACKAGE_CHANGED, f"{self.out} 里的上线进度是为另一个封存包建的（计划或上传已发出）：包已变，不能沿用，也不重建计划")
            state = {}
        if state.get("schema") == SCHEMA:
            route = Route(**{**state["route"], "flags": tuple(state["route"]["flags"])})     # decided once, kept
        else:
            state = {"schema": SCHEMA, "seal_sha256": seal, "route": dataclasses.asdict(route), "done": [], "commands": [],
                     "polls": 0, "upload_started_at": None, "quota_retry_epoch": None}
        self.route, self.state = route, state
        self._save()

    def _job(self, route: Route) -> commands.Job:
        auth = self.rules.first_publication if route.kind == FIRST else self.rules.revision
        name = self.package.name
        try:
            title = (self.package / f"{name}.title.txt").read_text(encoding="utf-8").rstrip("\n")
            tags = ",".join(read_json(self.package / f"{name}.record.json")["upload_tags"]["final_tags"]) if route.kind == FIRST else ""
        except (OSError, ValueError, KeyError, TypeError) as exc:
            stop(PACKAGE_UNREADABLE, f"读不到包里的标题或标签：{type(exc).__name__}: {exc!s:.300}")
        return commands.Job(route=route.kind, bvid=route.bvid, package=self.package, out=self.out, title=title, tags=tags,
                            predecessor=route.predecessor, authorized_by=auth.by, quote=auth.quote)

    # ----- the phases --------------------------------------------------------------------------------------------------
    def _audit(self) -> None:
        rc = self._cmd("audit", commands.audit(self.job), None, accept=None)         # the audit's JSON decides, as in the hand run
        try:
            data = read_json(self.out / "package_audit.json")
        except (OSError, ValueError):
            data = {}
        if rc != 0 or data.get("passed") is not True or data.get("blocking_issue_count"):
            issues = "；".join(str(i)[:200] for i in (data.get("issues") or [])[:5])
            said = issues or tail(self.out / "package_audit.err") or (self.last.stdout + self.last.stderr)[-300:].strip()
            stop(AUDIT_FAILED, f"包审计未通过（返回码 {rc}，passed={data.get('passed')}，阻断项 {data.get('blocking_issue_count')}）：{said}")
        shutil.copyfile(self.out / "package_audit.json", self.package / "package_audit.json")

    def _poll(self) -> None:
        """The run command, again after the interval while the tool says 6; WAITING_LIVE when this call's polls are used up."""
        for attempt in range(1, self.rules.max_polls + 1):
            self.state["polls"] += 1
            n = self.state["polls"]
            if self._cmd(f"run#{n}", commands.run(self.job, log=f"run-{n}.log"), f"run-{n}.log", accept=(0, 6)) == 0:
                return
            if attempt < self.rules.max_polls:
                _sleep(self.rules.poll_interval_s)
        stop(WAITING_LIVE, f"执行命令仍在推进（返回 6）：已轮询 {self.rules.max_polls} 次、间隔 {self.rules.poll_interval_s:g} 秒；"
             "下次 run 只继续轮询，不重新发起改动")

    def _revision(self) -> None:
        job, done = self.job, self.state["done"]
        self._once("mk", commands.make_manifest(job), "make-manifest.log")
        self._once("verify", commands.verify_manifest(job), "verify.log")
        if "plan-dry" not in done:
            self._cmd("plan-dry", commands.plan(job, dry_run=True), "plan-dry.log")
            if problems := plan_problems(job, self.ctx.clip_dir):
                stop(PLAN_MISMATCH, "计划预演与包/revision.json 对不上，不建正式计划：" + "；".join(problems))
            self._mark("plan-dry")
        if "plan" not in done:
            if not job.plan.is_file():            # a plan made before is never made again
                self._cmd("plan", commands.plan(job, dry_run=False), "plan.log")
            self._mark("plan")
        self._once("status", commands.status(job), "status.log", accept=(0, 6))             # 6 = waiting for the run, as expected
        self._once("run-dry", commands.run(job, log="run-dry.log", dry_run=True), "run-dry.log", accept=(0, 6))
        if "run" not in done:
            self._poll()
            self._mark("run")
        if "verify-live" not in done:
            self._cmd("verify-live", commands.verify_live(job), "verify-live.log")
            status = read_json(job.completed).get("status") if job.completed.is_file() else None
            if not str(status).startswith("VERIFIED"):
                stop(COMMAND_FAILED, f"线上核验没有产出已核验的完成回执（{job.completed}，status={status}）")
            self._mark("verify-live")

    def _send(self, job: commands.Job) -> tuple[int, str]:
        """The upload command and the tool's last words.  The tool serialises uploads and repairs by one lock on the host and
        refuses at once when another one holds it (return 4, nothing started): that is a wait, not a failure - the command is
        run again after the interval, and when the polls are used up the step stops at WAITING_LOCK for the next `run`."""
        state = self.state
        for attempt in range(1, self.rules.max_polls + 1):
            state["upload_started_at"] = stamp()
            self._save()
            rc = self._cmd("upload", commands.upload(job), "upload.log", accept=None)
            words = tail(self.out / "upload.log") or (self.last.stdout + self.last.stderr)[-700:].strip()
            busy = rc == 4 and LOCK_BUSY in words
            if rc in (2, 3, 8) or busy:                                # refused before any attempt: nothing was started
                state["upload_started_at"] = None
            if not busy:
                return rc, words
            self._save()
            if attempt < self.rules.max_polls:
                _sleep(self.rules.poll_interval_s)
        stop(WAITING_LOCK, f"上传工具的共用锁一直被别的上传或修复占着：已等 {self.rules.max_polls} 次、间隔 {self.rules.poll_interval_s:g} 秒；"
             "没有发出任何上传，下次 run 再试")

    def _upload(self) -> None:
        job, state = self.job, self.state
        if "upload" not in state["done"]:
            retry = state.get("quota_retry_epoch")
            if retry and _epoch() < retry:
                stop(WAITING_QUOTA, f"首发配额不足，可重试的最早时间 {_at(retry)}：到时间下次 run 再试")
            if not job.uploaded.is_file():            # a receipt that is there: the upload went through, the step stopped before noting it
                if state.get("upload_started_at") and LOCK_BUSY not in (tail(self.out / "upload.log") or ""):
                    stop(ATTEMPT_UNRESOLVED, f"上一次 run 在 {state['upload_started_at']} 发出了上传命令，没有留下完成回执：不重发，"
                         "以上传工具的 ledger 为准查清后处理")
                rc, words = self._send(job)
                if rc == 8:
                    until = re.search(r"until (\d+(?:\.\d+)?)", words)
                    state["quota_retry_epoch"] = float(until.group(1)) if until else _epoch() + self.rules.quota_retry_s
                    self._save()
                    stop(WAITING_QUOTA, f"上传工具报配额不足（返回 8）：{words[-300:]}；可重试的最早时间 {_at(state['quota_retry_epoch'])}")
                if rc != 0:
                    self._save()
                    stop(COMMAND_FAILED, f"upload 返回码 {rc}（预期 0），不重试、不换路线：{words}")
            try:
                receipt = read_json(job.uploaded)
            except (OSError, ValueError) as exc:
                stop(COMMAND_FAILED, f"没有可读的上传完成回执 {job.uploaded}：{type(exc).__name__}: {exc!s:.200}")
            if receipt.get("status") != "VERIFIED_PUBLIC" or not receipt.get("bvid"):
                stop(COMMAND_FAILED, f"上传的完成回执不是 VERIFIED_PUBLIC：status={receipt.get('status')} bvid={receipt.get('bvid')}")
            state["bvid"] = receipt["bvid"]
            self._mark("upload")
        self.job = dataclasses.replace(job, bvid=state["bvid"])

    # ----- the record --------------------------------------------------------------------------------------------------
    def _online(self, route: Route, *, record: dict[str, Any] | None, problems: list[str]) -> Path:
        job = self.job
        files = {} if route.kind == NONE else {
            "manifest": job.manifest, "package_audit": job.audit, "plan": job.plan, "completed": job.completed, "uploaded": job.uploaded,
            "revision": self.ctx.clip_dir / "revision.json"}
        auth = self.rules.first_publication if route.kind == FIRST else self.rules.revision
        doc = {"schema_version": "manual-lane-online.v1", "written_at": stamp(), "route": route.kind, "why": route.why,
               "manuscript": route.manuscript, "bvid": job.bvid, "title": job.title, "package": str(self.package),
               "package_seal_sha256": self.state.get("seal_sha256"), "flags": list(route.flags),
               "authorization": {"by": auth.by, "quote": auth.quote, "rule_date": auth.rule_date},
               "artifacts": {k: {"path": str(p), "sha256": sha(p), "bytes": p.stat().st_size} for k, p in files.items() if p.is_file()},
               "commands": self.state["commands"], "readback": record, "problems": problems}
        path = self.out / ONLINE_FILE
        self.out.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def process(self) -> list[str]:
        route = self.route
        lines = [f"路线：{route.why}", *(f"【注意·不拦】{flag}" for flag in route.flags)] if route else []
        return lines + [f"{c['command']}：返回码 {c['rc']}，{c['started']} → {c['finished']}" for c in self.state["commands"]]

    def execute(self) -> StepOutcome:
        try:
            self.rules = load_rules()
        except UploadRulesUnset as exc:
            stop(RULES_UNSET, str(exc))
        held(self.ctx)
        seal = sealed(self.ctx, self.package)
        try:
            route = step10_route.decide(self.ctx)
        except RouteRefused as exc:
            stop(exc.reason, exc.note)
        if route.kind == NONE:
            self.route = route
            self.job = commands.Job(NONE, route.bvid, self.package, self.out, "", "", None, "", "")
            path = self._online(route, record=None, problems=[])
            return StepOutcome(True, products={"online": str(path)}, note=f"无需上线：{route.why}", process=self.process())
        cover_material(self.ctx)
        self._prepare(seal, route)
        route = self.route = self.route or route
        self.job = self._job(route)
        if "audit" not in self.state["done"]:
            self._audit()
            self._mark("audit")
        if route.kind == FIRST:
            self._once("mk", commands.make_manifest(self.job), "make-manifest.log")
            self._once("verify", commands.verify_manifest(self.job), "verify.log")
            self._upload()
        else:
            self._revision()
        record, problems = readback.verify_online(self.ctx, self.job, route.manuscript)
        path = self._online(route, record=record, problems=problems)
        if problems:
            stop(READBACK_MISMATCH, "回读与应上线的内容不一致（线上已改，不回滚；online.json 记下了读到的）：" + "；".join(problems))
        self._mark("readback")
        return StepOutcome(True, products={"online": str(path)}, process=self.process(), note=f"已上线并回读核对：{route.kind}，{self.job.bvid}")


def run(ctx: Ctx, package: Path) -> StepOutcome:
    """Step 10 of the lane (stage_package.step10_upload calls this)."""
    job = _Run(ctx, package)
    try:
        return job.execute()
    except Stop as stop_:
        return _fail(stop_.reason, stop_.note, process=job.process())
