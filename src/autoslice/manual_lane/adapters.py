"""Side-effect adapters for the manual-lane pipeline.

Every network, ssh or CPA call goes through one of these objects.  The
default instances raise AdapterNotConfigured, so a unit test (or a mis-wired
driver) cannot reach OCI3, N100, CPA or the upload chain by accident.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, NotRequired, Protocol, TypedDict, cast

from .layout import layout
from .state import sha256_file

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

_LAYOUT = layout()                    # the one runtime layout (profiles/<id>/manual_lane.json; layout.py)
REMOTE_HOST = _LAYOUT.remote_host
REMOTE_REPO = _LAYOUT.remote_repo     # read-only: existing tools only
REMOTE_PY = _LAYOUT.remote_py
CPA_JUDGE_MODEL = "gpt-6.1-sol"
CPA_JUDGE_EFFORT = "medium"
# How many attempts one answer gets, read by both layers that make an answer: cpa_text.py gets this number as --tries
# (its own default, now named once), and step_io._ask_cpa asks the same prompt again this many times when the text
# that came back is not a whole answer (TASK-35).  One number, not a second retry setting.
CPA_TEXT_TRIES = 10
CPA_TEXT_SCRIPT = Path(__file__).resolve().parents[3] / "assets" / "manual_lane" / "remote" / "cpa_text.py"
REMOTE_SYSTEM_PY = "/usr/bin/python3"


class AdapterNotConfigured(RuntimeError):
    """A stage tried to use a side effect that the caller did not inject."""


class ProviderError(TypedDict):
    code: str | None
    type: str | None


class CpaAttempt(TypedDict, total=False):
    attempt: int
    status: str
    billing: str
    wire_sha256: str
    http: int
    chars: int
    complete: bool
    reason: str | None
    body: str
    error: str
    provider_error: ProviderError
    quota_refusal: bool


class CpaPayload(TypedDict, total=False):
    model: str
    effort: str
    max_output_tokens: int
    stream: bool
    prompt_sha256: str
    complete: bool
    reason: str | None
    text: str
    attempts: int
    budget_attempts: int
    http_log: list[CpaAttempt]
    usage: JsonValue
    elapsed_seconds: float


class CpaResult(TypedDict):
    call_id: str
    model: str
    effort: str
    stream: bool
    rc: int
    complete: bool
    reason: str | None
    elapsed_s: float | None
    attempts: int | None
    budget_attempts: int | None
    http_log: list[CpaAttempt] | None
    usage: JsonValue
    prompt_sha256: str
    response_sha256: str | None
    response_text: str
    stderr_tail: str
    cached: NotRequired[bool]


@dataclass(frozen=True)
class RemoteResult:
    rc: int
    stdout: str
    stderr: str


class Remote(Protocol):
    def run_script(self, *, name: str, body: str, timeout: int | None) -> RemoteResult: ...
    def sha256(self, remote_path: str) -> str | None: ...
    def put(self, local: Path, remote_path: str) -> None: ...
    def get(self, remote_path: str, local: Path) -> None: ...


class _Forbidden:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Callable[..., NoReturn]:
        def _raise(*_a: object, **_k: object) -> NoReturn:
            raise AdapterNotConfigured(f"{self._name}.{attr} was called but no adapter is configured")
        return _raise


@dataclass
class Adapters:
    remote: Any = field(default_factory=lambda: _Forbidden("remote"))
    cpa: Any = field(default_factory=lambda: _Forbidden("cpa"))
    danmaku: Any = field(default_factory=lambda: _Forbidden("danmaku"))
    lookup: Any = field(default_factory=lambda: _Forbidden("lookup"))   # work_lookup.WebLookup: Bangumi, wiki, baike, router


# ----- remote command builders ---------------------------------------------------------------
def remote_python(args: str, *, python: str = REMOTE_PY) -> str:
    """The one way a lane body runs a Python tool on the media host: the deployed repo's location goes to the tool in
    MANUAL_LANE_REMOTE_REPO (the tools name no host path); the interpreter runs without writing bytecode."""
    return f"MANUAL_LANE_REMOTE_REPO={REMOTE_REPO} {python} -B {args}"


def root_script(cmd: str, *, env: Sequence[str], optional: bool = False, owned: str | None = None,
                recursive: bool = False) -> str:
    """The one way a lane body runs a command as root with provider credentials (TASK-13: the wrapper was spelled
    out at seven call sites).  The named credential files of the media host (`<base>/<name>.env`) are sourced inside
    the root subshell only, never read or printed here; `optional` sources a file only when it exists.  With `owned`,
    the command's exit code is kept and the files it wrote are handed back to the lane's user (`recursive` for a
    directory)."""
    loads = "".join(f"[ -f {f} ] && . {f}; " if optional else f". {f}; " for f in map(_LAYOUT.env_file, env))
    body = f"sudo -n bash -c 'set -a; {loads}set +a; export PYTHONDONTWRITEBYTECODE=1; {cmd}'\n"
    if owned is None:
        return body
    user = _LAYOUT.remote_user
    return (body + "rc=$?\n"
            f"sudo -n chown {'-R ' if recursive else ''}{user}:{user} {owned} 2>/dev/null || true\n"
            "exit $rc\n")


# ----- real implementations ----------------------------------------------------------------
SSH_CONNECT_ATTEMPTS = 3   # ssh exits 255 on connection failures (banner timeout, refused); retry only those
SSH_RETRY_WAIT_S = 30
# A long remote step (listening runs up to 90 min) can lose its connection without either end noticing; keepalive
# turns that into exit 255 within ~3 min, which the connection-level retry below then handles.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6"]


def _ssh(host: str, command: str, *, timeout: int | None, input_bytes: bytes | None = None) -> RemoteResult:
    """One ssh call.  A connection-level failure (exit 255, before the remote command ran) is retried a few times;
    any other exit code is the remote command's own result and is returned as is."""
    for attempt in range(1, SSH_CONNECT_ATTEMPTS + 1):
        proc = subprocess.run(["ssh", *SSH_OPTS, host, command],
                              input=input_bytes, capture_output=True, timeout=timeout,
                              check=False)   # the rc is the result: 255 retried below, any other returned
        result = RemoteResult(proc.returncode, proc.stdout.decode("utf-8", "replace"),
                              proc.stderr.decode("utf-8", "replace"))
        if result.rc != 255 or attempt == SSH_CONNECT_ATTEMPTS:
            return result
        time.sleep(SSH_RETRY_WAIT_S * attempt)
    raise AssertionError("unreachable")


class SshRemote:
    """Runs scripts on OCI3 over ssh.  Bodies are written to a file first and executed as a file,
    never as a nested `bash -c` string (see memory: inline quoting stalled a worker for 7 h)."""

    def __init__(self, host: str = REMOTE_HOST, *, scripts_dir: str):
        self.host = host
        self.scripts_dir = scripts_dir

    def run_script(self, *, name: str, body: str, timeout: int | None) -> RemoteResult:
        path = f"{self.scripts_dir}/{name}.sh"
        script = "#!/bin/bash\nset -uo pipefail\n" + body
        put = _ssh(self.host, f"mkdir -p {shlex.quote(self.scripts_dir)} && cat > {shlex.quote(path)}",
                   timeout=60, input_bytes=script.encode("utf-8"))
        if put.rc != 0:
            return put
        return _ssh(self.host, f"bash {shlex.quote(path)}", timeout=timeout)

    def sha256(self, remote_path: str) -> str | None:
        res = _ssh(self.host, f"sha256sum {shlex.quote(remote_path)}", timeout=600)
        if res.rc != 0 or not res.stdout.strip():
            return None
        return res.stdout.split()[0]

    def put(self, local: Path, remote_path: str) -> None:
        data = Path(local).read_bytes()
        res = _ssh(self.host, f"mkdir -p {shlex.quote(str(Path(remote_path).parent))} && cat > {shlex.quote(remote_path)}",
                   timeout=900, input_bytes=data)
        if res.rc != 0:
            raise RuntimeError(f"put to {remote_path} failed: rc={res.rc}")

    def get(self, remote_path: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, SSH_CONNECT_ATTEMPTS + 1):   # same connection-level retry as _ssh (exit 255 only)
            proc = subprocess.run(["ssh", *SSH_OPTS, self.host, f"cat {shlex.quote(remote_path)}"],
                                  capture_output=True, timeout=900, check=False)   # rc checked below
            if proc.returncode == 0:
                local.write_bytes(proc.stdout)
                return
            if proc.returncode != 255 or attempt == SSH_CONNECT_ATTEMPTS:
                raise RuntimeError(f"get {remote_path} failed: rc={proc.returncode}")
            time.sleep(SSH_RETRY_WAIT_S * attempt)


class LocalRemote:
    """The same remote protocol when the controller runs on the media host itself."""

    def __init__(self, *, scripts_dir: str):
        self.scripts_dir = scripts_dir

    def run_script(self, *, name: str, body: str, timeout: int | None) -> RemoteResult:
        directory = Path(self.scripts_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.sh"
        path.write_text("#!/bin/bash\nset -uo pipefail\n" + body, encoding="utf-8")
        proc = subprocess.run(["bash", str(path)], capture_output=True, timeout=timeout, check=False)
        return RemoteResult(proc.returncode, proc.stdout.decode("utf-8", "replace"),
                            proc.stderr.decode("utf-8", "replace"))

    def sha256(self, remote_path: str) -> str | None:
        try:
            return sha256_file(Path(remote_path))
        except OSError:
            return None

    @staticmethod
    def _copy(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and source.is_file() and source.samefile(target):
            return
        shutil.copyfile(source, target)

    def put(self, local: Path, remote_path: str) -> None:
        self._copy(Path(local), Path(remote_path))

    def get(self, remote_path: str, local: Path) -> None:
        self._copy(Path(remote_path), Path(local))


def _wire_prompt(prompt_text: str) -> str:
    """The exact heredoc body sent to the remote prompt file, including its final newline."""
    return prompt_text + "\n"


def _decode_cpa_result(*, call_id: str, prompt_text: str, model: str, effort: str,
                       payload: CpaPayload | None, rc: int, elapsed_s: float | None, stderr: str,
                       recovered: bool = False) -> CpaResult:
    """The single OUT_JSON-to-result mapping for ordinary calls and terminal recovery."""
    complete = payload is not None and payload.get("complete") is True
    reason = (None if complete else str(payload.get("reason") or "CPA_INCOMPLETE")) if payload is not None else "NO_OUT_JSON"
    raw_text = payload.get("text") if payload is not None and complete else ""
    response = raw_text if isinstance(raw_text, str) else ""
    ok = complete and bool(response.strip())
    stderr_tail = "\n".join(stderr.strip().splitlines()[-6:])[:800]
    result: CpaResult = {
        "call_id": call_id, "model": model, "effort": effort, "stream": True,
        "rc": 0 if ok else (rc or 1), "complete": ok, "reason": None if ok else (reason or "EMPTY_ANSWER"),
        "elapsed_s": elapsed_s, "attempts": payload.get("attempts") if payload is not None else None,
        "budget_attempts": payload.get("budget_attempts") if payload is not None else None,
        "http_log": payload.get("http_log", None if recovered else []) if payload is not None else [],
        "usage": payload.get("usage") if payload is not None else None,
        "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest() if response else None,
        "response_text": response,
        "stderr_tail": stderr_tail if ok else f"CPA_NOT_COMPLETE {reason or 'EMPTY_ANSWER'}\n{stderr_tail}"[:800],
    }
    if recovered:
        result["cached"] = True
    return result


_READ_CPA_TERMINAL = '''import base64,json,os,sys
from pathlib import Path
out,prompt=map(Path,sys.argv[1:])
try:
    raw=out.read_bytes()
except FileNotFoundError:
    sys.exit(45 if os.path.lexists(out) else 44)
except OSError:
    sys.exit(45)
try:
    wire=prompt.read_bytes()
except OSError:
    sys.exit(45)
print(json.dumps({"out":base64.b64encode(raw).decode("ascii"),"prompt":base64.b64encode(wire).decode("ascii")}))
'''


def recover_cpa_terminal(remote: Remote, *, call_id: str, prompt_text: str, model: str,
                         effort: str, remote_dir: str, max_output_tokens: int = 16000) -> CpaResult | None:
    """Read an existing matching terminal without modifying it; only definite absence returns None."""
    cdir = f"{remote_dir}/cpa"
    out_path, prompt_path = f"{cdir}/{call_id}.out.json", f"{cdir}/{call_id}.prompt.txt"
    body = (f"{shlex.quote(REMOTE_SYSTEM_PY)} -c {shlex.quote(_READ_CPA_TERMINAL)} "
            f"{shlex.quote(out_path)} {shlex.quote(prompt_path)}\n")
    result = remote.run_script(name=f"cpa-recover-{call_id}", body=body, timeout=120)
    if result.rc == 44:
        return None
    if result.rc != 0:
        raise RuntimeError(f"focused terminal read failed: rc={result.rc}")
    try:
        envelope = json.loads(result.stdout)
        wire = base64.b64decode(envelope["prompt"], validate=True)
        raw = base64.b64decode(envelope["out"], validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("focused terminal is unreadable") from exc
    expected_wire = _wire_prompt(prompt_text).encode("utf-8")
    if wire != expected_wire:
        raise ValueError("focused terminal prompt bytes differ")
    # cpa_text.py hashes Path.read_text's output, including Python's universal newline handling.
    provider_prompt = wire.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if (not isinstance(payload, dict) or payload.get("prompt_sha256") != hashlib.sha256(provider_prompt.encode()).hexdigest()
            or payload.get("model") != model or payload.get("effort") != effort
            or payload.get("max_output_tokens", 16000) != max_output_tokens or payload.get("stream") is not True):
        raise ValueError("focused terminal request identity differs")
    if payload.get("complete") is not True or not isinstance(payload.get("text"), str) or not payload["text"].strip():
        raise ValueError("focused terminal is incomplete")
    elapsed = payload.get("elapsed_seconds")
    if elapsed is not None and type(elapsed) not in (int, float):
        raise ValueError("focused terminal elapsed time is invalid")
    if payload.get("attempts") is not None and type(payload["attempts"]) is not int:
        raise ValueError("focused terminal attempts are invalid")
    if payload.get("budget_attempts") is not None and type(payload["budget_attempts"]) is not int:
        raise ValueError("focused terminal budget attempts are invalid")
    if payload.get("http_log") is not None and not isinstance(payload["http_log"], list):
        raise ValueError("focused terminal HTTP log is invalid")
    return _decode_cpa_result(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort,
                              payload=cast(CpaPayload, payload), rc=0,
                              elapsed_s=float(elapsed) if elapsed is not None else None, stderr="", recovered=True)


class SshCpaJudge:
    """CPA text judge on OCI3.  Same client as the lidousha title judge (cpa_text.py): /responses,
    gpt-6.1-sol, effort medium, stream on.  The root wrapper (root_script) loads cpa.env inside the script; the key
    is never printed.  Streaming avoids the 524 that non-streaming high-effort calls hit (10-31 diagnosis)."""

    def __init__(self, remote: Remote):
        self.remote = remote

    def ask(self, *, call_id: str, prompt_text: str, model: str = CPA_JUDGE_MODEL,
            effort: str = CPA_JUDGE_EFFORT, remote_dir: str, tries: int = CPA_TEXT_TRIES,
            max_output_tokens: int | None = None) -> CpaResult:
        from ..prompt_material_hash import prompt_material_sha256

        cdir = f"{remote_dir}/cpa"
        prompt_path = f"{cdir}/{call_id}.prompt.txt"
        out_path = f"{cdir}/{call_id}.out.json"
        history_dir = f"{cdir}/history"
        journal_dir = f"{_LAYOUT.work_root}/.cpa-text-journal"
        client_path = f"{cdir}/{call_id}/cpa_text.py"
        material_hash = cast(Callable[[str], str], prompt_material_sha256)(prompt_text)
        identity = [call_id, model, effort, material_hash]
        if max_output_tokens is not None:
            identity.append(f"max_output_tokens={max_output_tokens}")
        request_id = hashlib.sha256(json.dumps(identity,
                                               ensure_ascii=False).encode()).hexdigest()
        token_arg = f" --max-tokens {shlex.quote(str(max_output_tokens))}" if max_output_tokens is not None else ""
        self.remote.put(CPA_TEXT_SCRIPT, client_path)
        body = (
            f"mkdir -p {shlex.quote(cdir)}\n"
            f"mkdir -p {shlex.quote(history_dir)}\n"
            f"if [ -f {shlex.quote(prompt_path)} ]; then psha=$(sha256sum {shlex.quote(prompt_path)} | cut -d ' ' -f1); "
            f"cp -n {shlex.quote(prompt_path)} {shlex.quote(history_dir)}/{shlex.quote(call_id)}.prompt.$psha.txt; fi\n"
            f"cat > {shlex.quote(prompt_path)} <<'MANUAL_LANE_PROMPT_EOF'\n{_wire_prompt(prompt_text)}MANUAL_LANE_PROMPT_EOF\n"
            + root_script(f"{REMOTE_SYSTEM_PY} -B {shlex.quote(client_path)} {shlex.quote(prompt_path)} "
                          f"{shlex.quote(out_path)} --model {shlex.quote(model)} --effort {shlex.quote(effort)} "
                          f"--tries {tries}{token_arg} --stream --journal-dir {shlex.quote(journal_dir)} --logical-id {request_id}",
                          env=["cpa"], owned=shlex.quote(out_path))
        )
        t0 = time.time()
        # The client limits idle reads per attempt; do not kill a stream which is still making progress.
        res = self.remote.run_script(name=f"cpa-{call_id}", body=body, timeout=None)
        elapsed = round(time.time() - t0, 1)
        proc = self.remote.run_script(name=f"cpa-read-{call_id}", body=f"cat {shlex.quote(out_path)}\n", timeout=120)
        payload = cast(CpaPayload, json.loads(proc.stdout)) if proc.rc == 0 and proc.stdout.strip() else None
        wire = _wire_prompt(prompt_text).replace("\r\n", "\n").replace("\r", "\n")
        if payload is not None and (payload.get("prompt_sha256") != hashlib.sha256(wire.encode()).hexdigest()
                                    or payload.get("model") != model or payload.get("effort") != effort
                                    or payload.get("max_output_tokens", 16000) !=
                                    (max_output_tokens if max_output_tokens is not None else 16000)
                                    or payload.get("stream") is not True):
            payload = None
            res = RemoteResult(res.rc or 1, res.stdout, "CPA_RESULT_IDENTITY_MISMATCH")
        return _decode_cpa_result(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort,
                                  payload=payload, rc=res.rc, elapsed_s=elapsed, stderr=res.stderr,
                                  recovered="REUSED terminal answer" in res.stdout)


class DanmakuSource:
    """Reads the recording's danmaku XML from the recording archive (read-only cat over ssh).  Returns the raw bytes."""

    def __init__(self, host: str = _LAYOUT.danmaku_host):
        self.host = host

    def fetch(self, remote_path: str) -> bytes:
        proc = subprocess.run(["ssh", *SSH_OPTS, self.host, f"cat {shlex.quote(remote_path)}"],
                              capture_output=True, timeout=600, check=False)   # rc checked below
        if proc.returncode != 0:
            raise RuntimeError(f"danmaku fetch failed: rc={proc.returncode}")
        return proc.stdout

    def names(self, remote_dir: str) -> list[str]:
        """The XML files of one recording date (one live session), by name."""
        proc = subprocess.run(["ssh", *SSH_OPTS, self.host, f"ls -1 {shlex.quote(remote_dir)}"],
                              capture_output=True, timeout=600, check=False)   # rc checked below
        if proc.returncode != 0:
            raise RuntimeError(f"danmaku listing failed: rc={proc.returncode}")
        return [n for n in proc.stdout.decode("utf-8").splitlines() if n.endswith(".xml")]
