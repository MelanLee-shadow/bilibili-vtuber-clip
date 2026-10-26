#!/usr/bin/env python3
# brief-contract: writes <spec.parent>/<spec.stem>.boundary-retry.json <spec.output_root>/<candidate_id>/ <profile.delivery_root>/<spec.date>/ <delivery_runtime>/.prepared-deliveries/talk/<candidate_id>/ <native_audio_state.cache_root>/ <native_audio_state.budget_receipt.parent>/ <AUTOSLICE_BASE-or-/opt/bilive/autoslice>/reports/ <cpa_usage_log.parent>/ <external_audio_cache_root>/ local:<tempdir>/ <ssh-host>:/tmp/ <ssh-host>:<LOCAL_AGY_JOB_ROOT>/ <ssh-host>:/opt/bilive/jingting_jobs/ <python.pycache_root>/ ; reads: <spec-path> <first-attempt-log> <runtime-code-lease> ; delegates: scripts/produce_slice_package.py ; modes: bytecode=unless--B-or-PYTHONDONTWRITEBYTECODE,other-paths=selected-mode-only ; logs: stdout stderr <spec.output_root>/<candidate_id>/<candidate_id>.time-ledger.json <cpa_usage_log> ; expects_change: native-audio-budget.revision=nondecreasing
"""单候选 talk 重跑的 30-boundary 有界重试入口。

runner 的 talk lane 在 ``BOUNDARY_CONTEXT_EXHAUSTED`` 后做一次有界重试；
直接调用 ``scripts/produce_slice_package.py`` 的单候选重跑原本没有这一层。
本入口调用与 runner 完全相同的共享函数
``src.autoslice.talk_lane.boundary_context_retry``，只在
``retry_scope=same_topic_continues`` 时触发、只一次、cap 只升到 60 秒；
piece 放宽与 headroom 由共享函数按首轮 review-flags 的 bound scope 计算，
调用方不指定端点；第二次仍写同一 ``output_root``。

用法（``--`` 之后是除 ``--spec`` 外原样传给 producer 的参数）::

    python scripts/boundary_retry_entry.py --spec SPEC.json -- \
        --ssh-host localhost --speaker-mode uniform_host ...

首轮已经跑完时，用它的日志与退出码代替重跑首轮::

    python scripts/boundary_retry_entry.py --spec SPEC.json \
        --first-attempt-log producer.log --first-attempt-exit-code 1 -- ...

改写后的 spec 写在原 spec 同目录的 ``<stem>.boundary-retry.json``（相对路径
仍按原目录解析，原 spec 不被覆盖）。最后一行 ``BOUNDARY_RETRY_ENTRY key=value ...``
是机器可读结论（不含花括号，producer 自己的 JSON 记录仍是日志里最后一个 JSON 对象）；退出码是最后一次 producer 的退出码。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__" and (ROOT / "DEPLOYED_COMMIT").is_file():
    # Load the stable stdlib bootstrap before importing mutable repository code.
    import runpy as _lease_runpy
    _lease_bootstrap = ROOT.parent / "runtime-code-lease.py"
    _lease_runpy.run_path(str(_lease_bootstrap))["ensure_cli_code_lease"](ROOT, Path(__file__))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.talk_lane import (
    SINGLE_CANDIDATE_BOUNDARY_RETRY_SCOPES,
    _boundary_context_failure_evidence,
    boundary_context_retry,
)

# 与 scripts/session_autoslice.py 的同名常量同值（回归测试钉住相等）；
# headroom 是共享函数自己的固定常量，调用方不传。
# 不 import runner：它在导入时改写 os.environ，会污染 producer 子进程环境。
BOUNDARY_REPAIR_RETRY_CAP_MS = 60_000
PRODUCER_SCRIPT = ROOT / "scripts" / "produce_slice_package.py"
RESULT_PREFIX = "BOUNDARY_RETRY_ENTRY "


def summary_line(summary: dict[str, object]) -> str:
    """``key=value`` without braces: log readers take the LAST JSON object
    (``talk_lane.last_json_block``) as the producer record, so this line must
    not look like one."""

    def text(value: object) -> str:
        if value is None:
            return "none"
        if isinstance(value, (list, tuple, frozenset, set)):
            return ",".join(str(item) for item in value)
        return str(value)

    return RESULT_PREFIX + " ".join(f"{key}={text(value)}" for key, value in summary.items())


def retry_spec_path(spec_path: Path) -> Path:
    return spec_path.with_name(f"{spec_path.stem}.boundary-retry.json")


def probe_duration_ms(path: str) -> int:
    """Same probe as the runner's ``ffprobe_ms``; 0 means unknown."""

    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            check=False, capture_output=True, text=True, timeout=600,
        )
        return int(float(completed.stdout.strip()) * 1000)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def interpreter_flags() -> list[str]:
    """Keep ``-B``: a deployed immutable version must not gain ``__pycache__``."""

    return ["-B"] if sys.flags.dont_write_bytecode else []


def _tee_command(command: Sequence[str], sink: TextIO):
    process = subprocess.Popen(
        list(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=str(ROOT),
    )
    chunks: list[str] = []
    assert process.stdout is not None
    for raw in iter(process.stdout.readline, b""):
        text = raw.decode("utf-8", "replace")
        chunks.append(text)
        sink.write(text)
        sink.flush()
    process.stdout.close()
    return SimpleNamespace(returncode=process.wait()), "".join(chunks)


def run_boundary_retry_entry(
    *,
    spec_path: Path,
    producer_args: Sequence[str],
    first_attempt_log: Path | None = None,
    first_attempt_exit_code: int | None = None,
    python: str = sys.executable,
    python_flags: Sequence[str] | None = None,
    run_command: Callable[[Sequence[str], TextIO], tuple[object, str]] = _tee_command,
    probe_ms: Callable[[str], int] = probe_duration_ms,
    sink: TextIO = sys.stdout,
) -> int:
    spec_path = Path(spec_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out_root = Path(spec["output_root"])
    candidate_id = str(spec["candidate_id"])
    rewritten_path = retry_spec_path(spec_path)
    attempt_specs = [spec_path]
    flags = interpreter_flags() if python_flags is None else list(python_flags)

    def run_producer():
        command = [python, *flags, str(PRODUCER_SCRIPT), "--spec", str(attempt_specs[-1]),
                   *producer_args]
        completed, output = run_command(command, sink)
        return completed, output, None

    def write_spec(text: str) -> None:
        rewritten_path.write_text(text, encoding="utf-8")
        attempt_specs.append(rewritten_path)

    def append_log(text: str) -> None:
        sink.write(text)
        sink.flush()

    if first_attempt_log is not None:
        first = (
            SimpleNamespace(returncode=int(first_attempt_exit_code)),
            Path(first_attempt_log).read_bytes().decode("utf-8", "replace"),
            None,
        )
    else:
        first = run_producer()
    completed, first_output, _ = first
    summary: dict[str, object] = {
        "schema_version": "single-candidate-boundary-retry.v1",
        "candidate_id": candidate_id,
        "spec": str(spec_path),
        "output_root": str(out_root),
        "first_attempt_exit_code": completed.returncode,
        "first_attempt_retry_scope": _boundary_context_failure_evidence(
            first_output[-4000:]
        ).get("retry_scope"),
        "allowed_scopes": sorted(SINGLE_CANDIDATE_BOUNDARY_RETRY_SCOPES),
    }
    last_piece = spec["pieces"][-1]
    segment_duration_ms = probe_ms(str(last_piece["remote_media"]))
    summary["segment_duration_ms"] = segment_duration_ms
    if completed.returncode != 0 and segment_duration_ms <= 0:
        # The shared function treats 0 as "unknown" (no file-end clamp); an
        # entry without a probed recording must not widen past the file.
        summary.update(boundary_context_retries=0, status="SEGMENT_DURATION_UNKNOWN")
        sink.write("\n" + summary_line(summary) + "\n")
        return int(completed.returncode)
    completed, _output, _state, retries, owner_sha256 = boundary_context_retry(
        first=first,
        run_producer=run_producer,
        spec=spec,
        out_root=out_root,
        candidate_id=candidate_id,
        item={"seg_dur_ms": segment_duration_ms, "end_ms": spec["semantic_end_ms"]},
        write_spec=write_spec,
        append_log=append_log,
        retry_cap_ms=BOUNDARY_REPAIR_RETRY_CAP_MS,
        allowed_scopes=SINGLE_CANDIDATE_BOUNDARY_RETRY_SCOPES,
        discover_cross_segment_reserve=None,
    )
    summary.update(
        boundary_context_retries=retries,
        boundary_retry_owner_contract_sha256=owner_sha256,
        status="RETRIED" if retries else "NOT_RETRIED",
        final_exit_code=completed.returncode,
    )
    if retries:
        summary.update(
            retry_spec=str(rewritten_path),
            retry_piece_end_ms=spec["pieces"][-1]["end_ms"],
            retry_cap_ms=spec["boundary_repair_extend_cap_ms"],
        )
    sink.write("\n" + summary_line(summary) + "\n")
    sink.flush()
    return int(completed.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        split = raw.index("--")
        own, producer_args = raw[:split], raw[split + 1:]
    else:
        own, producer_args = raw, []
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--first-attempt-log", type=Path)
    parser.add_argument("--first-attempt-exit-code", type=int)
    args = parser.parse_args(own)
    if (args.first_attempt_log is None) != (args.first_attempt_exit_code is None):
        parser.error("--first-attempt-log and --first-attempt-exit-code go together")
    if any(arg == "--spec" or arg.startswith("--spec=") for arg in producer_args):
        parser.error("pass --spec before --; the entry owns which spec each attempt reads")
    return run_boundary_retry_entry(
        spec_path=args.spec,
        producer_args=producer_args,
        first_attempt_log=args.first_attempt_log,
        first_attempt_exit_code=args.first_attempt_exit_code,
    )


if __name__ == "__main__":
    raise SystemExit(main())
