#!/usr/bin/env python3
"""Offline whole-path observation of in-flight slices (plan A, stage one).

Runs the ordinary producer CLI (``scripts/produce_slice_package.py`` of a code
root) on a copy of one slice's existing outputs, inside
``scripts/continuation_authority_preflight_sandbox.sh`` (private mount and
network namespace, runtime root behind a disposable overlay, credentials
masked), from cold continuation through boundary, finalization, packaging and
delivery preparation.  It reports the blocks a production rerun would meet:

* Stop versus collect has exactly one switch,
  ``continuation_subchecks.audit(collect=True)``: grouped sub-checks are
  recorded and the path continues.  A block observed after such a collected
  failure is marked ``tainted`` (it ran on state production would reject).
* Every other check stops the path exactly where production stops; the block
  is recorded with its stage, raise site (file:line), reason code, cause chain
  and whether it is a pre-judgment (bookkeeping/identity/binding) check.
* No provider is ever called.  The sandbox has no network.  A text CPA request
  whose exact prompt bytes were answered in the same production run is
  replayed from that run's archived exchange (``cpa-text-exchanges``,
  ``COMPLETED``); content-addressed caches the producer consults itself are
  ordinary reads.  Any other provider-facing step (an unrecorded prompt, a
  network connection, a provider or remote process) stops the path as
  ``UNOBSERVED`` at its call site.  Credential loading returns a fixed
  non-secret placeholder so that construction of a transport is not itself
  mistaken for a provider call; the masked ``cpa.env`` is never read.

Normal production never imports this module.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import traceback
from typing import Any, Iterator, Mapping

SCHEMA_VERSION = "whole-path-observation.v1"
PROVIDER_PROCESSES = frozenset({"ssh", "scp", "sftp", "rsync", "curl", "wget", "agy", "codex",
                                "claude", "gemini"})
PROVIDER_SCRIPTS = frozenset({"llm_via_cpa.sh", "llm_via_runtime_cpa_ssh.py"})
PLACEHOLDER_CPA_ENVIRONMENT = {"CPA_BASE_URL": "http://observation.invalid/v1",
                               "CPA_API_KEY": "whole-path-observation-placeholder"}
DEFAULT_TIME_BUDGET_S = 3600
# Reason-code families of bookkeeping/identity/source-consistency checks: they
# stop a slice before (or without) any content judgment.  A code outside the
# family is a review verdict or an explicit policy rule.
PRE_JUDGMENT = re.compile(
    r"(DRIFT|INVALID|MISSING|MISMATCH|UNBOUND|AMBIGUOUS|BINDING|UNSAFE|REQUIRED|"
    r"WITHOUT_RECEIPT|PRESENT|CONTRACT|ENGINEERING_FAILURE|NOT_PENDING|UNSUPPORTED|"
    r"CROSSING|SCOPE|STALE|CYCLIC|NO_PROGRESS|UNREADABLE|EMPTY|INTEGER|HASH)")
JUDGMENT = re.compile(
    r"^(FINAL_REVIEW_UNRESOLVED_FINDINGS|TALK_EFFECTIVE_DURATION_NOT_OVER_60S_AFTER_BOUNDARY|"
    r"BOUNDARY_CONTEXT_EXHAUSTED|TITLE_AUTHORITY_UNRESOLVED|STORY_CONTRACT_TITLE_FAILED|"
    r"STORY_CONTRACT_COVER_FAILED|POSTBURN_SUBTITLE_AUDIO_FLAGGED)")
_CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


class ObservationBoundary(BaseException):
    """A provider-facing step was reached; nothing past it is observed.

    ``BaseException`` so that no production ``except Exception`` retry,
    fallback or AUDITOR_UNAVAILABLE path can turn the sandbox into a block.
    """

    def __init__(self, reason: str, detail: Mapping[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = dict(detail or {})


class ObservationTimeBudget(BaseException):
    """The declared per-slice wall-clock budget expired (path still running)."""


@contextmanager
def observe() -> Iterator[list[dict[str, Any]]]:
    """Collect grouped sub-checks instead of stopping at them (the one switch)."""
    from src.autoslice import continuation_subchecks

    with continuation_subchecks.audit(collect=True) as rows:
        yield rows


def reason_code(message: object) -> str | None:
    match = _CODE.search(str(message or ""))
    return match.group(0) if match else None


def classify(code: str | None) -> str:
    if not code:
        return "UNCLASSIFIED"
    if JUDGMENT.match(code):
        return "JUDGMENT_OR_POLICY"
    return "PRE_JUDGMENT" if PRE_JUDGMENT.search(code) else "UNCLASSIFIED"


def _utc(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


class ExchangeArchive:
    """Same-run recorded text exchanges, keyed by exact prompt SHA-256.

    Only ``COMPLETED`` exchanges whose request started inside ``window`` (the
    production run being observed) and whose stored prompt bytes hash to the
    recorded SHA are admitted.  A prompt answered twice with different text is
    ambiguous and never replayed.
    """

    def __init__(self, roots: list[Path], window: tuple[dt.datetime, dt.datetime] | None) -> None:
        self.by_prompt: dict[str, dict[str, object]] = {}
        self.ambiguous: set[str] = set()
        self.scanned = self.admitted = 0
        if window is None:
            return
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            for folder in sorted(root.glob("request-*")):
                self.scanned += 1
                try:
                    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
                    terminal = json.loads((folder / "terminal.json").read_text(encoding="utf-8"))
                    prompt = (folder / "prompt.txt").read_bytes()
                    completion = (folder / "completion.txt").read_bytes().decode("utf-8")
                except (OSError, ValueError, UnicodeDecodeError):
                    continue
                started = _utc(request.get("started_utc"))
                sha = hashlib.sha256(prompt).hexdigest()
                if (terminal.get("status") != "COMPLETED" or started is None
                        or not window[0] <= started <= window[1]
                        or request.get("prompt_sha256") != sha
                        or terminal.get("completion_sha256")
                        != hashlib.sha256(completion.encode("utf-8")).hexdigest()):
                    continue
                row = {"folder": folder.name, "completion": completion,
                       "answering_model": terminal.get("answering_model"),
                       "started_utc": request.get("started_utc"),
                       "prompt": prompt.decode("utf-8", "replace")}
                previous = self.by_prompt.get(sha)
                if previous is not None and previous["completion"] != completion:
                    self.ambiguous.add(sha)
                self.by_prompt[sha] = row
                self.admitted += 1
        for sha in self.ambiguous:
            self.by_prompt.pop(sha, None)

    def get(self, prompt: str) -> dict[str, object] | None:
        return self.by_prompt.get(hashlib.sha256(prompt.encode("utf-8")).hexdigest())

    def nearest(self, prompt: str, limit: int = 24) -> dict[str, object] | None:
        """Why an unrecorded prompt missed: its closest recorded prompt and the
        differing lines, masked to structure (no subtitle text, no escapes)."""
        import difflib

        lines = prompt.splitlines()
        best, best_ratio = None, -1.0
        for row in self.by_prompt.values():
            ratio = difflib.SequenceMatcher(None, lines, str(row["prompt"]).splitlines(),
                                            autojunk=False).ratio()
            if ratio > best_ratio:
                best, best_ratio = row, ratio
        if best is None:
            return None
        changed = [line for line in difflib.unified_diff(str(best["prompt"]).splitlines(), lines,
                                                         lineterm="", n=0)
                   if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        removed = [line[1:] for line in changed if line.startswith("-")]
        added = [line[1:] for line in changed if line.startswith("+")]
        where = []
        for old, new in list(zip(removed, added))[:limit]:
            first = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), min(len(old), len(new)))
            where.append({"first_difference_at": first, "old_length": len(old), "new_length": len(new),
                          "old_masked": _mask(old[max(0, first - 60):first + 100]),
                          "new_masked": _mask(new[max(0, first - 60):first + 100])})
        return {"folder": best["folder"], "started_utc": best["started_utc"],
                "line_similarity": round(best_ratio, 4), "changed_lines": len(changed),
                "changed_lines_masked": [_mask(line)[:160] for line in changed[:limit]],
                "changed_line_pairs": where}


_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}")


def _mask(line: str) -> str:
    """Keep ASCII structure (keys, numbers, hashes, paths); mask every other character."""
    return "".join(ch if ch.isascii() else "·" for ch in _ESCAPE.sub("·", line))


_WRAPPERS = frozenset({"nice", "ionice", "env", "timeout", "sudo", "setsid", "nohup", "stdbuf"})


def provider_process(args: object) -> str | None:
    """Name the provider/remote command a child process would run, if any."""
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    tokens = [os.path.basename(part) for value in argv for part in os.fsdecode(value).split()]
    for token in tokens:
        if token in PROVIDER_SCRIPTS:
            return token
    for token in tokens:
        if token in _WRAPPERS or token.startswith("-") or token.isdigit() or "=" in token:
            continue
        return token if token in PROVIDER_PROCESSES else None
    return None


def isolation_problems() -> list[str]:
    problems = []
    for kind in ("net", "mnt"):
        try:
            shared = os.readlink(f"/proc/self/ns/{kind}") == os.readlink(f"/proc/1/ns/{kind}")
        except OSError:  # not root / no procfs: isolation cannot be shown
            problems.append(f"{kind.upper()}_NAMESPACE_UNVERIFIABLE")
            continue
        if shared:
            problems.append("NETWORK_NAMESPACE_NOT_PRIVATE" if kind == "net" else "MOUNT_NAMESPACE_NOT_PRIVATE")
    for secret in (Path("/opt/bilive/.env"), Path("/opt/bilive/autoslice/cpa.env")):
        try:
            if secret.exists() and secret.stat().st_size:
                problems.append("CREDENTIAL_FILE_NOT_MASKED:" + str(secret))
        except OSError:
            pass
    if os.environ.get("CONTINUATION_PREFLIGHT_SANDBOX") != "1":
        problems.append("SANDBOX_LAUNCHER_REQUIRED")
    return problems


def _repo_frames(tb, code_root: Path, limit: int = 6) -> list[str]:
    frames = []
    for frame in traceback.extract_tb(tb):
        path = Path(frame.filename)
        try:
            rel = path.resolve().relative_to(code_root)
        except ValueError:
            continue
        frames.append(f"{rel}:{frame.lineno}:{frame.name}")
    return frames[-limit:]


_TRANSPORT_PLUMBING = frozenset({"llm_client.py", "infra_retry_policy.py", "structured_output.py",
                                 "producer_final_review_transport.py", "whole_path_observation.py"})


def _stack_sites(code_root: Path, limit: int = 8) -> list[str]:
    """The stage callers that asked (transport plumbing skipped), then the innermost frame."""
    sites, plumbing = [], []
    for frame in traceback.extract_stack()[:-2]:
        try:
            rel = Path(frame.filename).resolve().relative_to(code_root)
        except ValueError:
            continue
        site = f"{rel}:{frame.lineno}:{frame.name}"
        (plumbing if rel.name in _TRANSPORT_PLUMBING else sites).append(site)
    return sites[-limit:] + plumbing[-1:]


class Observation:
    """Process-wide hooks of one observed slice (installed once per process)."""

    def __init__(self, code_root: Path, archive: ExchangeArchive) -> None:
        self.code_root = code_root
        self.archive = archive
        self.stages: list[str] = []
        self.timeline: list[dict[str, object]] = []
        self.replayed: list[dict[str, object]] = []
        self.unobserved: list[dict[str, object]] = []
        self.started = time.monotonic()

    def stage_path(self) -> str:
        return " > ".join(self.stages)

    def boundary(self, reason: str, **detail: object) -> ObservationBoundary:
        row = {"reason": reason, "stage": self.stage_path(), "call_sites": _stack_sites(self.code_root),
               "elapsed_s": round(time.monotonic() - self.started, 3), **detail}
        self.unobserved.append(row)
        return ObservationBoundary(reason, row)

    # -- hooks ------------------------------------------------------------
    def install(self) -> None:
        def refuse_network(*_args, **_kwargs):
            raise self.boundary("NETWORK")

        socket.socket.connect = refuse_network
        socket.socket.connect_ex = refuse_network
        socket.create_connection = refuse_network
        socket.getaddrinfo = refuse_network
        original_popen = subprocess.Popen.__init__

        def guarded_popen(popen, args, *rest, **kwargs):
            head = provider_process(args)
            if head is not None:
                popen._child_created = False  # no child: Popen.__del__ stays quiet
                raise self.boundary("PROVIDER_OR_REMOTE_PROCESS:" + head)
            return original_popen(popen, args, *rest, **kwargs)

        subprocess.Popen.__init__ = guarded_popen

        from src.autoslice import llm_client, producer_stage_log

        def replay(prompt: str, config: object) -> str:
            hit = self.archive.get(prompt)
            sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            if hit is None:
                raise self.boundary("PROVIDER_TEXT_UNRECORDED", prompt_sha256=sha,
                                    transport=getattr(config, "transport", None),
                                    nearest_recorded=self.archive.nearest(prompt))
            self.replayed.append({"prompt_sha256": sha, "folder": hit["folder"],
                                  "stage": self.stage_path()})
            model = hit.get("answering_model")
            return llm_client.CpaCompletion(str(hit["completion"]), str(model)) if model else str(hit["completion"])

        llm_client._call_command = replay
        llm_client._call_direct = replay
        original_emit = producer_stage_log.emit

        def emit(event: str, stage: str, **fields: object) -> None:
            if event == "START":
                self.stages.append(stage)
            elif event == "END" and stage in self.stages:
                del self.stages[len(self.stages) - 1 - self.stages[::-1].index(stage):]
            self.timeline.append({"event": event, "stage": stage,
                                  "t": round(time.monotonic() - self.started, 3)})
            original_emit(event, stage, **fields)

        producer_stage_log.emit = emit

    def placeholder_credentials(self) -> None:
        """Rebind every loaded reference to the credential loader (no secret read)."""
        from src.autoslice import llm_client

        original = llm_client.runtime_cpa_command_environment

        def placeholder(_runtime_root, **_kwargs):
            child = os.environ.copy()
            child.update(PLACEHOLDER_CPA_ENVIRONMENT)
            return child

        for module in list(sys.modules.values()):
            name = getattr(module, "__name__", "") or ""
            if name.startswith(("src.autoslice", "scripts", "produce_slice_package")):
                for attr, value in list(vars(module).items()):
                    if value is original:
                        setattr(module, attr, placeholder)


def _exception_rows(exc: BaseException, code_root: Path) -> list[dict[str, object]]:
    rows, seen = [], set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(rows) < 6:
        seen.add(id(current))
        message = str(current.args[0]) if getattr(current, "args", None) else str(current)
        rows.append({"type": type(current).__name__, "message": message[:600],
                     "reason_code": getattr(current, "reason_code", None) or reason_code(message),
                     "frames": _repo_frames(current.__traceback__, code_root)})
        current = current.__cause__ or current.__context__
    return rows


def _review_summary(message: str) -> dict[str, object] | None:
    """Summarize a chat-authority JSON named in a block message (codes only)."""
    match = re.search(r"(/\S+\.chat-authority\.json)", message)
    if not match or not Path(match.group(1)).is_file():
        return None
    try:
        review = json.loads(Path(match.group(1)).read_text(encoding="utf-8")).get("final_review_audit") or {}
    except (OSError, ValueError):
        return None
    from collections import Counter

    findings = review.get("findings") or []
    codes = Counter()
    for row in findings if isinstance(findings, list) else []:
        if not isinstance(row, Mapping):
            continue
        life = row.get("lifecycle") if isinstance(row.get("lifecycle"), Mapping) else {}
        error = row.get("terminal_funnel_error") if isinstance(row.get("terminal_funnel_error"), Mapping) else {}
        # Closed lifecycle/funnel codes only; never subtitle text.
        codes["|".join(str(value) for value in (
            life.get("state"), life.get("release_action"), error.get("reason_code")))] += 1
    boundary = review.get("boundary_semantic_review") or {}
    return {"path": match.group(1), "status": review.get("status"),
            "release_gate": review.get("release_gate"), "reason_codes": review.get("reason_codes"),
            "findings": len(findings) if isinstance(findings, list) else None,
            "finding_lifecycle_state|release_action|funnel_reason": dict(codes.most_common(12)),
            "boundary_semantic_review": {k: boundary.get(k) for k in ("status", "reason_codes", "review_scope")}}


def run_one(args: argparse.Namespace) -> dict[str, object]:
    report: dict[str, object] = {"schema_version": SCHEMA_VERSION, "spec": str(args.spec),
                                 "code_root": str(args.code_root),
                                 "isolation_problems": isolation_problems()}
    if report["isolation_problems"]:
        report["outcome"] = "REFUSED_NOT_ISOLATED"
        return report
    code_root = args.code_root.resolve()
    if Path(__file__).resolve().parents[2] != code_root:
        report["outcome"] = "REFUSED_MIXED_CODE_ROOT"
        return report
    if str(code_root) not in sys.path:
        sys.path.insert(0, str(code_root))
    spec_doc = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    cid = str(spec_doc.get("candidate_id"))
    report["candidate_id"] = cid
    out_root = Path(str(spec_doc.get("output_root"))) / cid
    window = None
    if args.exchange_window:
        start, _, end = args.exchange_window.partition("..")
        window = (_utc(start), _utc(end))
        if None in window:
            raise SystemExit("--exchange-window must be <iso>..<iso>")
    archive = ExchangeArchive([out_root / "cpa-text-exchanges"], window)
    report["exchange_archive"] = {"window": args.exchange_window, "scanned": archive.scanned,
                                  "admitted": archive.admitted, "ambiguous": len(archive.ambiguous)}
    observation = Observation(code_root, archive)
    observation.install()
    module_spec = importlib.util.spec_from_file_location(
        "produce_slice_package", code_root / "scripts/produce_slice_package.py")
    producer = importlib.util.module_from_spec(module_spec)
    sys.modules["produce_slice_package"] = producer
    module_spec.loader.exec_module(producer)
    observation.placeholder_credentials()
    budget = args.time_budget_s or DEFAULT_TIME_BUDGET_S

    def expire(_signum, _frame):
        raise ObservationTimeBudget(budget)

    signal.signal(signal.SIGALRM, expire)
    signal.alarm(budget)
    argv = ["--spec", str(args.spec), "--ssh-host", "localhost", "--substrate", args.substrate,
            "--correct", "cpa", "--speaker-mode", "uniform_host", "--reuse-cover", "--prepare-only"]
    rows: list[dict[str, Any]] = []
    stop: BaseException | None = None
    started = time.perf_counter()
    try:
        with observe() as rows:
            result = producer.main(argv)
        report["outcome"] = "PRODUCER_RETURNED"
        report["producer_return"] = result
    except ObservationBoundary as exc:
        report["outcome"] = "UNOBSERVED"
        report["unobserved_at"] = exc.detail
        stop = exc
    except ObservationTimeBudget:
        report["outcome"] = "TIME_BUDGET_EXCEEDED"
    except BaseException as exc:  # noqa: BLE001 - every production stop is the observed block
        report["outcome"] = "BLOCKED"
        stop = exc
    finally:
        signal.alarm(0)
    report["elapsed_s"] = round(time.perf_counter() - started, 3)
    report["stage_at_stop"] = observation.stage_path()
    report["timeline"] = observation.timeline
    report["replayed_exchanges"] = observation.replayed
    report["unobserved"] = observation.unobserved
    report["subchecks"] = rows
    report["collected_failures"] = [row for row in rows if row.get("status") == "COLLECTED"]
    report["blocks"] = blocks_of(rows, report, stop, code_root)
    return report


def blocks_of(rows: list[dict[str, Any]], report: Mapping[str, object],
              stop: BaseException | None, code_root: Path) -> list[dict[str, object]]:
    """Every observed block in path order.

    The first block is where production would stop.  A later block is seen
    only because an earlier grouped failure was collected (``tainted``): it is
    a "what comes next if that were fixed" finding, never a prediction.
    """
    blocks: list[dict[str, object]] = []
    tainted = False
    for row in rows:
        if row.get("status") == "FAIL":
            code = reason_code(row.get("reason_code"))
            blocks.append({"check": row.get("reason_code"), "subcheck": row.get("check"),
                           "stage": "text_pipeline > cold continuation authority",
                           "site": f"{row.get('validator')} @ {row.get('call_site')}",
                           "collected": True, "pre_judgment": classify(code), "tainted": tainted})
        elif row.get("status") == "COLLECTED":
            tainted = True
    if report.get("outcome") == "BLOCKED" and stop is not None:
        chain = _exception_rows(stop, code_root)
        message = chain[0]["message"] if chain else ""
        code = reason_code(message) or (chain[0]["reason_code"] if chain else None)
        block = {"check": code, "message": message, "stage": report.get("stage_at_stop"),
                 "site": (chain[0]["frames"] or [None])[-1] if chain else None,
                 "cause_chain": chain, "collected": False,
                 "pre_judgment": classify(code), "tainted": tainted}
        summary = _review_summary(message)
        if summary is not None:
            block["review_evidence"] = summary
        blocks.append(block)
    for index, block in enumerate(blocks):
        block["production_stop"] = index == 0
    return blocks


def summary_row(report: Mapping[str, object]) -> dict[str, object]:
    return {"candidate_id": report.get("candidate_id"), "outcome": report.get("outcome"),
            "stage_at_stop": report.get("stage_at_stop"), "elapsed_s": report.get("elapsed_s"),
            "blocks": [{key: block.get(key) for key in ("check", "stage", "site", "pre_judgment",
                                                         "tainted", "production_stop")}
                       for block in report.get("blocks") or []],
            "unobserved": [{**{key: row.get(key) for key in ("reason", "stage", "call_sites")},
                            "nearest_recorded": {key: (row.get("nearest_recorded") or {}).get(key)
                                                 for key in ("folder", "line_similarity", "changed_lines")}}
                           for row in report.get("unobserved") or []][:3],
            "replayed_exchanges": len(report.get("replayed_exchanges") or []),
            "exchange_archive": report.get("exchange_archive")}


def render_markdown(reports: list[Mapping[str, object]]) -> str:
    """One block list per slice: the stage-one deliverable of plan A."""
    lines = ["# 整条路径离线观察（方案 A 阶段一）", "",
             "每片一份拦截清单。第一行是生产会停的位置；标 tainted 的行只在收集了上游分组失败后才看得到，"
             "是“上游修好后接着会撞什么”，不是生产预测。UNOBSERVED 表示该处需要 provider 而同一次运行没有已录回答，"
             "其后各阶段未观察。", ""]
    for report in reports:
        lines += [f"## {report.get('candidate_id')}", "",
                  f"- 结果：`{report.get('outcome')}`，停在阶段：`{report.get('stage_at_stop') or '-'}`，"
                  f"耗时 {report.get('elapsed_s')} s",
                  f"- 同次运行已录交换：{json.dumps(report.get('exchange_archive'), ensure_ascii=False)}；"
                  f"回放 {len(report.get('replayed_exchanges') or [])} 次", ""]
        blocks = report.get("blocks") or []
        if blocks:
            lines += ["| # | 检查 | 阶段 | 位置 | 判定之前的检查 | tainted | 生产停点 |",
                      "|---:|---|---|---|---|---|---|"]
            for index, block in enumerate(blocks, start=1):
                lines.append(f"| {index} | `{block.get('check')}` | {block.get('stage')} | `{block.get('site')}` | "
                             f"{block.get('pre_judgment')} | {block.get('tainted')} | {block.get('production_stop')} |")
            lines.append("")
            for index, block in enumerate(blocks, start=1):
                if block.get("message"):
                    lines.append(f"- #{index} 原文：`{str(block.get('message'))[:300]}`")
                for cause in (block.get("cause_chain") or [])[1:]:
                    lines.append(f"  - cause `{cause.get('type')}` `{cause.get('reason_code')}` "
                                 f"@ `{(cause.get('frames') or [None])[-1]}`")
                if block.get("review_evidence"):
                    lines.append(f"  - 证据：`{json.dumps(block['review_evidence'], ensure_ascii=False)[:600]}`")
            lines.append("")
        for row in report.get("unobserved") or []:
            near = row.get("nearest_recorded") or {}
            lines.append(f"- UNOBSERVED `{row.get('reason')}` 阶段 `{row.get('stage')}` @ "
                         f"`{' < '.join(reversed((row.get('call_sites') or [])[-3:]))}`；最近已录 `{near.get('folder')}` "
                         f"行相似 {near.get('line_similarity')}，差异 {near.get('changed_lines')} 行")
        lines.append("")
    return "\n".join(lines) + "\n"


def run_slices(code_root: Path, args: argparse.Namespace) -> int:
    """``--exec`` entry inside the sandbox: one fresh process per slice."""
    reports = Path(args.report_dir)
    reports.mkdir(parents=True, exist_ok=True)
    rc = 0
    env = {**os.environ, "GEMINI_PAID_BACKUP_DEV_EXCEPTION": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    for line in Path(args.slices).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        spec, substrate = line.split()[:2]
        cid = Path(spec).parent.name
        # The sealed copy of this tool: one code identity per observed process.
        command = ["ionice", "-c", "3", "nice", "-n", "10", sys.executable, "-I", "-B",
                   str(code_root / "src/autoslice/whole_path_observation.py"), "--run-one",
                   "--spec", spec, "--code-root", str(code_root), "--substrate", substrate,
                   "--report", str(reports / f"{cid}.json"), "--time-budget-s", str(args.time_budget_s)]
        if args.exchange_window:
            command += ["--exchange-window", args.exchange_window]
        log = reports / f"{cid}.log"
        with log.open("w", encoding="utf-8") as handle:
            completed = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, env=env)
        rc |= int(completed.returncode != 0)
    return rc


def _seal_version(argv: list[str]) -> int:
    tool = Path(__file__).resolve().parents[2] / "scripts" / "continuation_authority_preflight.py"
    spec = importlib.util.spec_from_file_location("continuation_authority_preflight", tool)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seal_version(Path(argv[0]), argv[1], Path(argv[2]))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-one", action="store_true")
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--code-root", type=Path)
    parser.add_argument("--substrate", choices=("existing_srt", "aggregate_asr"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--slices", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--time-budget-s", type=int, default=0)
    parser.add_argument("--exchange-window", default="",
                        help="<iso>..<iso>: start window of the observed production run's exchanges")
    if argv and not argv[0].startswith("-"):
        # sandbox --exec: the first argument is the sealed code root.
        code_root, rest = Path(argv[0]).resolve(), argv[1:]
        return run_slices(code_root, parser.parse_args(rest))
    args = parser.parse_args(argv)
    if not args.run_one or not all((args.spec, args.code_root, args.substrate, args.report)):
        parser.error("--run-one needs --spec, --code-root, --substrate and --report")
    args.code_root = args.code_root.resolve()
    report = run_one(args)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                           encoding="utf-8")
    print(json.dumps({"candidate_id": report.get("candidate_id"), "outcome": report.get("outcome"),
                      "blocks": [row.get("check") for row in report.get("blocks", [])],
                      "elapsed_s": report.get("elapsed_s")}, ensure_ascii=False))
    return 0 if report.get("outcome") != "REFUSED_NOT_ISOLATED" else 2


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--seal-version":
        # Before importing any repository package: the sealed tree owns ``src``.
        raise SystemExit(_seal_version(sys.argv[2:]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.autoslice.whole_path_observation import main as _main

    raise SystemExit(_main())
