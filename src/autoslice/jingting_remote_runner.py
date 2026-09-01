"""Hash-attested chunked jingting refinement over SSH or native local AGY."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Callable, Mapping, Sequence
import uuid

from scripts.gemini_slice_jingting import (
    AGY_MODEL,
    agy_prompt,
    looks_like_srt,
    strip_markdown_fence,
    validate_same_timing,
)
from src.autoslice import agy_gemini_client
from src.autoslice.danmaku_evidence import (
    danmaku_in_window,
    format_danmaku_lines,
)
from src.autoslice.jingting_chunker import (
    JingtingChunk,
    merge_refined_chunks,
    plan_jingting_chunks,
    repair_sparse_refined_chunk,
)
from src.autoslice.source_context_executor import (
    AgyChunkAttestation,
    AgyExecutionResult,
    AgyRunnerError,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Keep this predicate in one existing transport module so producer media,
# chunked refinement, and whole-window transcription cannot slowly grow three
# subtly different definitions of "local".  In particular, ``::1`` is a
# genuine loopback address and must not fall through to an SSH self-hop.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
JINGTING_JOB_ROOT = "/opt/bilive/jingting_jobs"

# AGY's OAuth state is read from its own HOME-backed runtime.  Do not inherit
# the autoslice child environment wholesale: in particular CPA, Gemini, HF,
# and other provider credentials are unrelated to a local AGY invocation.
# Only the standard locale names are passed through; all other names must be
# explicitly listed here before they can reach the child process.
_LOCAL_AGY_ENV_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "COLORTERM",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LANGUAGE",
        "LC_ADDRESS",
        "LC_ALL",
        "LC_COLLATE",
        "LC_CTYPE",
        "LC_IDENTIFICATION",
        "LC_MEASUREMENT",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NAME",
        "LC_NUMERIC",
        "LC_PAPER",
        "LC_TELEPHONE",
        "LC_TIME",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TERM_PROGRAM",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)


def _local_agy_environment() -> dict[str, str]:
    """Build the minimal runtime environment for one local AGY child.

    AGY obtains OAuth material from the configured HOME/runtime rather than
    from autoslice provider variables.  Only generic process, locale, TLS,
    proxy, and XDG paths are retained; names such as ``CPA_API_KEY``,
    ``GEMINI_*``, ``HF_TOKEN``, and unrelated provider secrets are excluded by
    construction.
    """

    return {
        key: value
        for key, value in os.environ.items()
        if key in _LOCAL_AGY_ENV_ALLOWLIST
    }


def is_local_host(host: str) -> bool:
    """Return whether ``host`` names this process's loopback host."""

    return str(host).strip().lower() in LOCAL_HOSTS


def _local_regular_file(path: Path, *, label: str) -> None:
    """Reject links and special files before using a local job artifact."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AgyRunnerError(
            "AGY_JOB_ARTIFACT_INVALID",
            f"{label} is unavailable: {path}",
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AgyRunnerError(
            "AGY_JOB_ARTIFACT_INVALID",
            f"{label} is not a regular non-symlink file: {path}",
        )


def _local_job_dir(job_dir: str | Path) -> Path:
    """Create one private local job directory, never reuse an existing one.

    The SSH implementation historically uses ``mkdir -p`` because its job
    directory lives on the remote host.  A local execution must be stricter:
    an existing directory could belong to a concurrent or previous attempt,
    and replacing its artifacts would make the resulting receipt ambiguous.
    Parent components are created/validated one at a time so a symlink cannot
    silently redirect a job outside the requested path.
    """

    path = Path(job_dir)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise AgyRunnerError(
            "AGY_JOB_PATH_INVALID",
            f"local AGY job directory must be a normalized absolute path: {path}",
        )

    # Validate or create every parent component, then create the leaf with
    # exist_ok=False.  The leaf is the collision boundary for this invocation.
    current = Path(path.anchor)
    for component in path.parts[1:-1]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                metadata = current.lstat()
            else:
                metadata = current.lstat()
        except OSError as exc:
            raise AgyRunnerError(
                "AGY_JOB_PATH_INVALID",
                f"cannot inspect local AGY job parent: {current}",
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AgyRunnerError(
                "AGY_JOB_PATH_INVALID",
                f"local AGY job parent is not a real directory: {current}",
            )

    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise AgyRunnerError(
            "AGY_JOB_COLLISION",
            f"local AGY job directory already exists: {path}",
        ) from exc
    except OSError as exc:
        raise AgyRunnerError(
            "AGY_JOB_PATH_INVALID",
            f"cannot create local AGY job directory: {path}",
        ) from exc
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AgyRunnerError(
            "AGY_JOB_PATH_INVALID",
            f"local AGY job directory is not a real directory: {path}",
        )
    return path


def _local_private_temp(job_dir: Path, target_name: str) -> tuple[Path, tuple[int, int]]:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target_name}.stage-",
        dir=str(job_dir),
    )
    os.fchmod(fd, 0o600)
    metadata = os.fstat(fd)
    os.close(fd)
    return Path(temporary_name), (metadata.st_dev, metadata.st_ino)


def _cleanup_local_private_temp(
    temporary: Path,
    identity: tuple[int, int],
    *,
    expected_sha256: str | None = None,
) -> None:
    """Remove only an unchanged, invocation-owned staging file."""

    try:
        metadata = temporary.lstat()
    except OSError:
        return
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        return
    if expected_sha256 is not None:
        try:
            if _sha256_file(temporary) != expected_sha256:
                return
        except OSError:
            return
    try:
        temporary.unlink()
    except OSError:
        return


def _publish_local_create_only(
    temporary: Path,
    identity: tuple[int, int],
    target: Path,
    *,
    expected_sha256: str,
) -> Path:
    """Publish a private temp without replacing a concurrent artifact.

    ``link`` gives us the POSIX equivalent of an atomic rename-without-
    replace: the destination must not already exist, and the source inode is
    the exact private temp we created.  Removing that source leaves the
    destination as the sole published name.
    """

    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        raise AgyRunnerError(
            "AGY_JOB_COLLISION",
            f"local AGY artifact already exists: {target}",
        )
    try:
        os.link(temporary, target)
    except FileExistsError as exc:
        raise AgyRunnerError(
            "AGY_JOB_COLLISION",
            f"local AGY artifact appeared concurrently: {target}",
        ) from exc
    except OSError as exc:
        raise AgyRunnerError(
            "AGY_JOB_ARTIFACT_INVALID",
            f"cannot publish local AGY artifact: {target}",
        ) from exc
    _cleanup_local_private_temp(
        temporary,
        identity,
        expected_sha256=expected_sha256,
    )
    try:
        _local_regular_file(target, label="published AGY artifact")
        if _sha256_file(target) != expected_sha256:
            raise AgyRunnerError(
                "AGY_INPUT_HASH_MISMATCH",
                f"local AGY artifact hash mismatch: {target}",
            )
    except Exception:
        # The destination is deliberately retained as evidence.  It was
        # never an existing artifact and may be needed to diagnose a local
        # filesystem race; only the private temp is eligible for cleanup.
        raise
    return target


def _stage_local_bytes(job_dir: Path, target_name: str, payload: bytes) -> Path:
    target = job_dir / target_name
    temporary, identity = _local_private_temp(job_dir, target_name)
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return _publish_local_create_only(
            temporary,
            identity,
            target,
            expected_sha256=expected_sha256,
        )
    finally:
        _cleanup_local_private_temp(
            temporary,
            identity,
            expected_sha256=expected_sha256,
        )


def _stage_local_path(job_dir: Path, target_name: str, source: Path) -> Path:
    _local_regular_file(source, label="local AGY input")
    try:
        source_sha256_before = _sha256_file(source)
    except OSError as exc:
        raise AgyRunnerError(
            "AGY_INPUT_HASH_MISMATCH",
            f"local AGY input could not be hashed before staging: {source}",
        ) from exc
    temporary, identity = _local_private_temp(job_dir, target_name)
    expected_sha256: str | None = None
    try:
        with source.open("rb") as source_handle, temporary.open("wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        expected_sha256 = _sha256_file(temporary)
        _local_regular_file(source, label="local AGY input")
        try:
            source_sha256_after = _sha256_file(source)
        except OSError as exc:
            raise AgyRunnerError(
                "AGY_INPUT_HASH_MISMATCH",
                f"local AGY input could not be hashed after staging: {source}",
            ) from exc
        if not (
            source_sha256_before == source_sha256_after == expected_sha256
        ):
            raise AgyRunnerError(
                "AGY_INPUT_HASH_MISMATCH",
                f"local AGY input changed during staging: {source}",
            )
        return _publish_local_create_only(
            temporary,
            identity,
            job_dir / target_name,
            expected_sha256=source_sha256_before,
        )
    finally:
        _cleanup_local_private_temp(
            temporary,
            identity,
            expected_sha256=expected_sha256,
        )


def _stage_local_agy_inputs(
    job_dir: str | Path,
    *,
    files: Mapping[str, Path | bytes],
) -> tuple[Path, dict[str, str]]:
    """Stage local AGY inputs with create-only private-temp publication."""

    directory = _local_job_dir(job_dir)
    hashes: dict[str, str] = {}
    for target_name, source in files.items():
        if Path(target_name).name != target_name or not target_name:
            raise AgyRunnerError(
                "AGY_JOB_PATH_INVALID",
                f"invalid local AGY artifact name: {target_name!r}",
            )
        target = (
            _stage_local_bytes(directory, target_name, source)
            if isinstance(source, bytes)
            else _stage_local_path(directory, target_name, Path(source))
        )
        _local_regular_file(target, label="staged AGY input")
        hashes[target_name] = _sha256_file(target)
    return directory, hashes


def _write_local_rc(directory: Path, returncode: int) -> None:
    _stage_local_bytes(directory, "agy.rc", f"rc={returncode}\n".encode("ascii"))


def _read_local_rc(directory: Path) -> int | None:
    """Read a completed local job's rc marker, rejecting unsafe artifacts."""

    marker = directory / "agy.rc"
    try:
        marker.lstat()
    except FileNotFoundError:
        return None
    _local_regular_file(marker, label="local AGY rc marker")
    value = agy_gemini_client.parse_remote_rc_line(
        marker.read_text(encoding="ascii")
    )
    if value is None:
        raise AgyRunnerError(
            "AGY_JOB_ARTIFACT_INVALID",
            f"invalid local AGY rc marker: {marker}",
        )
    return value


def _start_local_agy_process(
    directory: Path,
    *,
    binary: str,
    model: str,
    short_prompt: str,
    print_timeout: str,
) -> subprocess.Popen:
    """Start one AGY process in its own process group with private logs."""

    stdout_path = directory / "agy.stdout"
    stderr_path = directory / "agy.stderr"
    stdout_fd = os.open(
        stdout_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        stderr_fd = os.open(
            stderr_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except BaseException:
        os.close(stdout_fd)
        raise
    stdout_handle = os.fdopen(stdout_fd, "wb")
    stderr_handle = os.fdopen(stderr_fd, "wb")
    command = [
        binary,
        "--sandbox",
        "--dangerously-skip-permissions",
        "--add-dir",
        str(directory),
        "--model",
        model,
        "-p",
        short_prompt,
        "--print-timeout",
        print_timeout,
    ]
    try:
        return subprocess.Popen(
            command,
            cwd=str(directory),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=_local_agy_environment(),
            start_new_session=True,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()


def _terminate_owned_local_process(process: subprocess.Popen) -> None:
    """Terminate only the process/group started for this local job."""

    pid = getattr(process, "pid", None)
    own_group = False
    if isinstance(pid, int) and pid > 0:
        try:
            own_group = os.getpgid(pid) == pid
        except (OSError, ProcessLookupError):
            own_group = False
    try:
        if own_group:
            os.killpg(pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if own_group:
            os.killpg(pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _run_local_agy_job(
    job_dir: str | Path,
    *,
    input_path: Path,
    extra_files: Mapping[str, Path | bytes] | None = None,
    prompt: str,
    output_name: str,
    model: str,
    short_prompt: str,
    print_timeout: str,
    poll_deadline_seconds: float,
    poll_interval_seconds: float,
    stage: str,
) -> str:
    """Run a local AGY job and return one staged output's text."""

    if Path(output_name).name != output_name or not output_name:
        raise AgyRunnerError(
            "AGY_JOB_PATH_INVALID",
            f"invalid local AGY output name: {output_name!r}",
        )
    files: dict[str, Path | bytes] = {
        "input.mp4": input_path,
        "prompt.md": prompt.encode("utf-8"),
    }
    if extra_files:
        files.update(extra_files)
    directory, _ = _stage_local_agy_inputs(job_dir, files=files)
    binary = agy_gemini_client.resolve_remote_agy_binary()
    try:
        process = _start_local_agy_process(
            directory,
            binary=binary,
            model=model,
            short_prompt=short_prompt,
            print_timeout=print_timeout,
        )
    except FileNotFoundError as exc:
        _write_local_rc(directory, 127)
        raise AgyRunnerError(
            agy_gemini_client.AGY_BINARY_ABSENT,
            f"{stage}: no local agy at {binary}; see {directory}",
        ) from exc
    except OSError as exc:
        raise AgyRunnerError(
            agy_gemini_client.AGY_SUBPROCESS_ERROR,
            f"{stage}: could not start local agy: {exc}; see {directory}",
        ) from exc

    state: dict[str, object] = {"error": None}

    def record_returncode() -> None:
        try:
            returncode = int(process.wait())
            state["returncode"] = returncode
            _write_local_rc(directory, returncode)
        except BaseException as exc:  # surface the watcher failure below
            state["error"] = exc

    watcher = threading.Thread(
        target=record_returncode,
        name=f"agy-rc-{directory.name}",
        daemon=True,
    )
    watcher.start()
    deadline = time.time() + poll_deadline_seconds
    # Unlike the remote shell wrapper, the local adapter owns the Popen and
    # writes the same rc marker from a tiny waiter thread.  Poll the marker so
    # the local and remote evidence contract remains identical.
    local_poll_interval = max(poll_interval_seconds, 0.0)
    while True:
        returncode = _read_local_rc(directory)
        if returncode is not None:
            break
        watcher_error = state.get("error")
        if watcher_error is not None:
            raise AgyRunnerError(
                "AGY_JOB_ARTIFACT_INVALID",
                f"local AGY rc marker could not be recorded; see {directory}",
            ) from watcher_error
        if time.time() >= deadline:
            _terminate_owned_local_process(process)
            watcher.join(timeout=5)
            raise AgyRunnerError(
                "AGY_TIMEOUT",
                f"{stage} did not finish within {poll_deadline_seconds}s; see {directory}",
            )
        time.sleep(max(0.0, min(local_poll_interval, deadline - time.time())))

    if returncode != 0:
        category = agy_gemini_client.classify_remote_agy_rc(int(returncode))
        if category == agy_gemini_client.AGY_BINARY_ABSENT:
            raise AgyRunnerError(
                agy_gemini_client.AGY_BINARY_ABSENT,
                f"{stage}: no local agy ({returncode}); see {directory}",
            )
        raise AgyRunnerError(
            "AGY_FAILED_RC",
            f"{stage}: local agy failed rc={returncode}; see {directory}/agy.stderr",
        )

    output_path = directory / output_name
    try:
        _local_regular_file(output_path, label="local AGY output")
        return output_path.read_text(encoding="utf-8")
    except (AgyRunnerError, OSError):
        return ""


class _RemoteJingtingRunner:
    def __init__(
        self,
        host: str,
        *,
        danmaku_items=None,
        context_start_ms: int = 0,
        topic_entity_context_provider: Callable[[], str] | None = None,
        song_name_candidates: Sequence[str] = (),
    ) -> None:
        self.host = host
        self.danmaku_items = danmaku_items
        self.context_start_ms = context_start_ms
        self.topic_entity_context_provider = topic_entity_context_provider
        self.song_name_candidates = song_name_candidates
        self.chunk_print_timeout = "15m"
        self.chunk_poll_deadline_seconds = 1500
        self.chunk_poll_interval_seconds = 20
        self.attempts_per_chunk = 2

    @staticmethod
    def _run(
        cmd: list[str],
        *,
        timeout: int = 2400,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{cmd[0]} failed rc={completed.returncode}: "
                f"{completed.stderr[-400:]}"
            )
        return completed

    def _encode_chunk_clip(
        self,
        media_path: Path,
        chunk: JingtingChunk,
        out_path: Path,
    ) -> None:
        self._run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{chunk.media_start_ms / 1000:.3f}",
                "-i",
                str(media_path),
                "-t",
                f"{chunk.media_duration_ms / 1000:.3f}",
                "-vf",
                "scale=1280:-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(out_path),
            ],
            timeout=1800,
        )

    def _chunk_danmaku_lines(
        self,
        chunk: JingtingChunk,
    ) -> list[str] | None:
        if not self.danmaku_items:
            return None
        window_start = self.context_start_ms + chunk.media_start_ms
        window_end = self.context_start_ms + chunk.media_end_ms
        in_window = danmaku_in_window(
            self.danmaku_items,
            window_start,
            window_end,
            max_items=60,
        )
        return (
            format_danmaku_lines(in_window, base_ms=window_start)
            if in_window
            else None
        )

    def _stage_chunk_inputs(
        self,
        job_dir: str,
        chunk_clip: Path,
        chunk_srt_text: str,
        prompt: str,
    ) -> None:
        self._run(["ssh", self.host, f"mkdir -p {shlex.quote(job_dir)}"])
        with tempfile.TemporaryDirectory(prefix="ssh_agy_chunk_") as tmp:
            prompt_file = Path(tmp) / "prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            draft_file = Path(tmp) / "draft.srt"
            draft_file.write_text(
                chunk_srt_text
                if chunk_srt_text.endswith("\n")
                else chunk_srt_text + "\n",
                encoding="utf-8",
            )
            self._run(
                ["scp", "-q", str(chunk_clip), f"{self.host}:{job_dir}/input.mp4"]
            )
            self._run(
                ["scp", "-q", str(draft_file), f"{self.host}:{job_dir}/draft.srt"]
            )
            self._run(
                ["scp", "-q", str(prompt_file), f"{self.host}:{job_dir}/prompt.md"]
            )
            expected_hashes = [
                _sha256_file(chunk_clip),
                _sha256_file(draft_file),
            ]
        hash_probe = self._run(
            [
                "ssh",
                self.host,
                (
                    f"sha256sum {shlex.quote(job_dir)}/input.mp4 "
                    f"{shlex.quote(job_dir)}/draft.srt"
                ),
            ],
            timeout=120,
        )
        remote_hashes = [
            line.split()[0]
            for line in hash_probe.stdout.splitlines()
            if line.split()
        ]
        if remote_hashes != expected_hashes:
            raise AgyRunnerError(
                "AGY_INPUT_HASH_MISMATCH",
                f"remote AGY chunk input hash mismatch; see {self.host}:{job_dir}",
            )

    def _run_chunk_agy(
        self,
        job_dir: str,
        chunk_clip: Path,
        chunk_srt_text: str,
        chunk: JingtingChunk,
    ) -> str:
        topic_context = (
            str(self.topic_entity_context_provider() or "")
            if self.topic_entity_context_provider is not None
            else ""
        )
        prompt = agy_prompt(
            chunk_srt_text,
            danmaku_lines=self._chunk_danmaku_lines(chunk),
            topic_entity_context=topic_context,
            song_name_candidates=self.song_name_candidates,
        )
        short_prompt = (
            f"Open {job_dir}/prompt.md with view_file and follow it exactly. "
            f"Use only {job_dir}/prompt.md, {job_dir}/input.mp4, "
            f"{job_dir}/draft.srt, and {job_dir}/output.srt. "
            "Do not inspect any other file or directory. Do not use shell or terminal."
        )
        if is_local_host(self.host):
            draft_bytes = (
                chunk_srt_text
                if chunk_srt_text.endswith("\n")
                else chunk_srt_text + "\n"
            ).encode("utf-8")
            corrected = strip_markdown_fence(
                _run_local_agy_job(
                    job_dir,
                    input_path=chunk_clip,
                    extra_files={"draft.srt": draft_bytes},
                    prompt=prompt,
                    output_name="output.srt",
                    model=AGY_MODEL,
                    short_prompt=short_prompt,
                    print_timeout=self.chunk_print_timeout,
                    poll_deadline_seconds=self.chunk_poll_deadline_seconds,
                    poll_interval_seconds=self.chunk_poll_interval_seconds,
                    stage="local agy chunk",
                )
            )
        else:
            self._stage_chunk_inputs(job_dir, chunk_clip, chunk_srt_text, prompt)
            agy_inner = (
                f"{shlex.quote(agy_gemini_client.resolve_remote_agy_binary())} "
                f"--sandbox --dangerously-skip-permissions --add-dir {shlex.quote(job_dir)} "
                f"--model {shlex.quote(AGY_MODEL)} -p {shlex.quote(short_prompt)} "
                f"--print-timeout {self.chunk_print_timeout}"
            )
            agy_cmd = (
                f"cd {shlex.quote(job_dir)} && "
                f"script -qec {shlex.quote(agy_inner)} /dev/null "
                f"> {shlex.quote(job_dir)}/agy.stdout "
                f"2> {shlex.quote(job_dir)}/agy.stderr; "
                f"echo rc=$? > {shlex.quote(job_dir)}/agy.rc"
            )
            self._run(
                [
                    "ssh",
                    self.host,
                    f"nohup bash -c {shlex.quote(agy_cmd)} >/dev/null 2>&1 & echo started",
                ]
            )

            deadline = time.time() + self.chunk_poll_deadline_seconds
            rc_line = ""
            while time.time() < deadline:
                probe = subprocess.run(
                    [
                        "ssh",
                        self.host,
                        f"cat {shlex.quote(job_dir)}/agy.rc 2>/dev/null",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                rc_line = probe.stdout.strip()
                if rc_line:
                    break
                time.sleep(self.chunk_poll_interval_seconds)
            if not rc_line:
                subprocess.run(
                    [
                        "ssh",
                        self.host,
                        f"pkill -f {shlex.quote(job_dir)} || true",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
                raise AgyRunnerError(
                    "AGY_TIMEOUT",
                    "remote agy chunk did not finish within "
                    f"{self.chunk_poll_deadline_seconds}s; see {self.host}:{job_dir}",
                )
            if rc_line != "rc=0":
                # rc=127 = 那台机器上没有 agy（远端版的 AGY_BINARY_ABSENT）。
                # 分类归统一客户端，运维不用再去查一台本来就不装 AGY 的机器。
                category = agy_gemini_client.classify_remote_agy_rc(
                    agy_gemini_client.parse_remote_rc_line(rc_line)
                )
                if category == agy_gemini_client.AGY_BINARY_ABSENT:
                    raise AgyRunnerError(
                        agy_gemini_client.AGY_BINARY_ABSENT,
                        f"no agy on {self.host} ({rc_line}); see {self.host}:{job_dir}",
                    )
                raise AgyRunnerError(
                    "AGY_FAILED_RC",
                    f"remote agy failed {rc_line}; see {self.host}:{job_dir}/agy.stderr",
                )

            fetched = subprocess.run(
                ["ssh", self.host, f"cat {shlex.quote(job_dir)}/output.srt"],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            corrected = (
                strip_markdown_fence(fetched.stdout)
                if fetched.returncode == 0
                else ""
            )
        if not looks_like_srt(corrected):
            evidence_location = (
                str(job_dir)
                if is_local_host(self.host)
                else f"{self.host}:{job_dir}"
            )
            raise AgyRunnerError(
                "AGY_EMPTY_OUTPUT",
                f"{('local' if is_local_host(self.host) else 'remote')} agy exited "
                "rc=0 but produced no valid output.srt; "
                f"see {evidence_location}",
            )
        try:
            validate_same_timing(chunk_srt_text, corrected)
        except RuntimeError as timing_error:
            try:
                corrected, sparse_audit = repair_sparse_refined_chunk(
                    chunk_srt_text,
                    corrected,
                )
            except ValueError:
                raise timing_error
            print(
                "[agy] bounded sparse-cue self-heal: "
                + json.dumps(
                    sparse_audit,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
            validate_same_timing(chunk_srt_text, corrected)
        return corrected

    @staticmethod
    def _attestation(
        chunk: JingtingChunk,
        chunk_clip: Path,
        chunk_srt_text: str,
        corrected: str,
        *,
        provider: str,
    ) -> AgyChunkAttestation:
        draft_bytes = (
            chunk_srt_text
            if chunk_srt_text.endswith("\n")
            else chunk_srt_text + "\n"
        ).encode("utf-8")
        return AgyChunkAttestation(
            chunk_index=chunk.chunk_index,
            media_start_ms=chunk.media_start_ms,
            media_end_ms=chunk.media_end_ms,
            media_sha256=_sha256_file(chunk_clip),
            draft_srt_sha256=hashlib.sha256(draft_bytes).hexdigest(),
            refined_srt_sha256=hashlib.sha256(
                corrected.encode("utf-8")
            ).hexdigest(),
            executed_provider=provider,
            timing_validated=True,
            audio_input_attested=True,
        )

    def __call__(
        self,
        media_path: Path,
        draft_srt_path: Path,
        output_srt_path: Path,
    ) -> AgyExecutionResult:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        job_suffix = (
            f"-{os.getpid()}-{uuid.uuid4().hex[:12]}"
            if is_local_host(self.host)
            else ""
        )
        srt_text = draft_srt_path.read_text(encoding="utf-8")
        chunks = plan_jingting_chunks(srt_text)
        if not chunks:
            raise AgyRunnerError(
                "AGY_NO_DRAFT_CUES",
                f"draft SRT has no parseable cues: {draft_srt_path}",
            )

        refined_pairs: list[tuple[JingtingChunk, str]] = []
        attestations: list[AgyChunkAttestation] = []
        with tempfile.TemporaryDirectory(prefix="ssh_agy_clips_") as clips_tmp:
            for chunk in chunks:
                chunk_clip = Path(clips_tmp) / (
                    f"chunk_{chunk.chunk_index:02d}.mp4"
                )
                self._encode_chunk_clip(media_path, chunk, chunk_clip)
                chunk_srt_text = chunk.chunk_srt_text()
                last_error: Exception | None = None
                corrected = ""
                for attempt in range(1, self.attempts_per_chunk + 1):
                    job_dir = (
                        f"{JINGTING_JOB_ROOT}/ssh-{media_path.stem}-{stamp}"
                        f"{job_suffix}-c{chunk.chunk_index:02d}a{attempt}"
                    )
                    try:
                        corrected = self._run_chunk_agy(
                            job_dir,
                            chunk_clip,
                            chunk_srt_text,
                            chunk,
                        )
                        last_error = None
                        break
                    except (AgyRunnerError, RuntimeError) as exc:
                        last_error = exc
                if last_error is not None:
                    raise last_error
                attestation = self._attestation(
                    chunk,
                    chunk_clip,
                    chunk_srt_text,
                    corrected,
                    provider="agy",
                )
                refined_pairs.append((chunk, corrected))
                attestations.append(attestation)

        merged = merge_refined_chunks(srt_text, refined_pairs)
        validate_same_timing(srt_text, merged)
        normalized_merged = merged if merged.endswith("\n") else merged + "\n"
        output_srt_path.write_text(normalized_merged, encoding="utf-8")
        return AgyExecutionResult(
            provider="agy",
            model=AGY_MODEL,
            agy_rc=0,
            provider_fallback_used=False,
            provider_request_id=(
                f"{self.host}:jingting-chunked:{stamp}:{len(chunks)}chunks"
            ),
            requested_provider="agy",
            executed_provider="agy",
            source_media_sha256=_sha256_file(media_path),
            draft_srt_sha256=hashlib.sha256(
                srt_text.encode("utf-8")
            ).hexdigest(),
            refined_srt_sha256=hashlib.sha256(
                normalized_merged.encode("utf-8")
            ).hexdigest(),
            timing_validated=True,
            audio_input_attested=True,
            chunk_count=len(chunks),
            agy_chunk_count=len(chunks),
            api_fallback_chunk_count=0,
            chunk_attestations=tuple(attestations),
        )


def build_ssh_agy_runner(
    host: str,
    *,
    danmaku_items=None,
    context_start_ms: int = 0,
    topic_entity_context_provider: Callable[[], str] | None = None,
    song_name_candidates: Sequence[str] = (),
):
    """Build a chunked, hash-attested second-listen runner."""

    return _RemoteJingtingRunner(
        host,
        danmaku_items=danmaku_items,
        context_start_ms=context_start_ms,
        topic_entity_context_provider=topic_entity_context_provider,
        song_name_candidates=song_name_candidates,
    )
