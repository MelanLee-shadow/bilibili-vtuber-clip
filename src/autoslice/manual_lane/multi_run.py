"""Several clips in one `run`: each clip in its own thread, all of them at once (the number of clips has no bound of its own).

Threads, not processes: a clip is mostly waiting for remote commands and models, and the lane's bounds (parallel.py)
are counted per process, so clips in threads share them as they must (a process per clip would multiply every
provider's concurrency by the number of clips).  What bounds the clips is what is really limited: the providers' bounds and
the media host's encode gate (parallel.py, stages.GATES).  A clip's own lines go to its log file; the terminal gets one line per
step start and end of each clip, and the log paths.  Exit code: the worst of the clips' codes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TextIO

from .runner import Step, StepOutcome
from .state import utc_now

_SINK = threading.local()          # the log file of the clip the current thread is running, when several clips run
_TERMINAL = threading.Lock()
Progress = Callable[[Step, str, StepOutcome | None], None]


def say(text: str) -> None:
    """One output line of a run: the clip's log in a multi-clip run, the terminal otherwise."""
    log: TextIO | None = getattr(_SINK, "log", None)
    if log is None:
        print(text)
        return
    log.write(text + "\n")
    log.flush()


def _terminal(text: str) -> None:
    with _TERMINAL:
        print(text, flush=True)


def clip_ids(cids: Sequence[str] | None, cids_file: str | None) -> list[str]:
    """The clips of a run, in the order given: --cid (repeatable), then the lines of --cids-file."""
    out = list(cids or [])
    if cids_file:
        out += [line.strip() for line in Path(cids_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not out:
        raise ValueError("run needs --cid (repeatable) or --cids-file")
    repeated = sorted({cid for cid in out if out.count(cid) > 1})
    if repeated:
        raise ValueError(f"clip given more than once: {', '.join(repeated)}")
    return out


def run_many(cids: Sequence[str], run_one: Callable[[str, Progress], int], *, log_dir: Path,
             errors: tuple[type[Exception], ...]) -> int:
    """`run_one(cid, progress)` for every clip, all at once; the worst exit code.  An exception in `errors`
    ends only its clip (code 2, the message in its log)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "")

    def one(cid: str) -> int:
        path = log_dir / f"{cid}-{stamp}.log"
        _terminal(f"[{cid}] 日志 {path}")

        def progress(step: Step, event: str, outcome: StepOutcome | None) -> None:
            what = "开始" if event == "start" else "完成" if outcome is None or outcome.ok else f"停止（{outcome.reason}）"
            line = f"[{cid}] 步骤 {step.number} {step.title} {what}"
            _terminal(line)
            say(line)

        with path.open("a", encoding="utf-8") as log:
            _SINK.log = log
            try:
                code = run_one(cid, progress)
            except errors as exc:
                say(f"error: {exc}")
                code = 2
            finally:
                _SINK.log = None
        _terminal(f"[{cid}] 结束 退出码 {code}")
        return code

    with ThreadPoolExecutor(max_workers=len(cids), thread_name_prefix="lane-clip") as pool:
        return max(pool.map(one, cids))
