#!/usr/bin/env python3
"""Validate or execute one hash-bound holdout ASR segment.

Execution is unreachable unless a v1 plan explicitly authorizes the external
BCUT upload and binds this wrapper, the core, Python, ffmpeg, and ASR client.
There is no provider, media, output, offset, or timeout override on the CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

sys.dont_write_bytecode = True
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice import speaker_holdout_prelabel as prelabel


TOOLCHAIN_FIELDS = {
    "planner",
    "free_asr_client",
    "hash_bound_run_one_wrapper",
    "hash_bound_aggregate_verifier",
    "run_one_core",
    "ffmpeg",
    "python",
    "execution_parameters",
    "runtime_binding_state",
}
EXECUTION_PARAMETERS = {
    "ffmpeg_timeout_s": 900,
    "bcut_poll_interval_s": 3,
    "bcut_poll_timeout_s": 900,
}
SUBPROCESS_ENV = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
EXECUTION_SCRATCH_ROOT = Path("/tmp/hostocc-v2-20260811/speaker-prelabel-plan-v1")
PRODUCTION_ROOTS = {
    "/opt/bilive/autoslice/state",
    "/opt/bilive/autoslice/out",
    "/opt/bilive/autoslice/repo",
    "/opt/bilive/recording",
}
PROXY_ENV_NAMES = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
}
TLS_ENV_NAMES = {"SSLKEYLOGFILE", "SSL_CERT_FILE", "SSL_CERT_DIR"}
FORBIDDEN_NETWORK_ENV_NAMES = PROXY_ENV_NAMES | TLS_ENV_NAMES
BCUT_BASE = "https://member.bilibili.com/x/bcut/rubick-interface"


class HoldoutPrelabelWrapperError(RuntimeError):
    pass


def _bound_file(binding: object, *, label: str, executable: bool = False) -> Path:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise HoldoutPrelabelWrapperError(f"{label} binding field set drifted")
    path = Path(str(binding["path"]))
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise HoldoutPrelabelWrapperError(f"{label} binding is missing") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or absolute.is_symlink()
        or absolute != resolved
        or prelabel.file_sha256(resolved) != binding["sha256"]
        or (executable and not os.access(resolved, os.X_OK))
    ):
        raise HoldoutPrelabelWrapperError(f"{label} binding drifted")
    return resolved


def _read_bound_bytes(binding: object, *, label: str) -> tuple[Path, bytes]:
    path = _bound_file(binding, label=label)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        chunks = []
        while chunk := os.read(descriptor, 4 * 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.lstat()
    data = b"".join(chunks)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        or digest != binding["sha256"]
    ):
        raise HoldoutPrelabelWrapperError(f"{label} changed while reading")
    return path, data


def validate_executor_plan(plan: Mapping[str, object], *, runner_path: Path) -> None:
    prelabel.validate_plan(plan, require_execution_authority=True)
    scratch = plan.get("scratch")
    contract = plan.get("execution_contract")
    if not isinstance(scratch, dict) or set(scratch) != {
        "allowed_root",
        "planned_run_root",
        "plan_path",
        "write_policy",
    }:
        raise HoldoutPrelabelWrapperError("scratch contract field set drifted")
    if not isinstance(contract, dict):
        raise HoldoutPrelabelWrapperError("execution contract is invalid")
    expected_root = EXECUTION_SCRATCH_ROOT
    planned_run_root = Path(str(scratch.get("planned_run_root")))
    plan_path = Path(str(scratch.get("plan_path")))
    if (
        scratch.get("allowed_root") != str(expected_root)
        or scratch.get("write_policy") != "CREATE_ONLY_NO_OVERWRITE"
        or planned_run_root.parent != expected_root
        or plan_path.parent != expected_root
        or set(contract.get("production_roots_forbidden", [])) != PRODUCTION_ROOTS
        or len(contract.get("production_roots_forbidden", [])) != len(PRODUCTION_ROOTS)
    ):
        raise HoldoutPrelabelWrapperError("executor scratch/production boundary drifted")
    toolchain = plan.get("toolchain")
    if not isinstance(toolchain, dict) or set(toolchain) != TOOLCHAIN_FIELDS:
        raise HoldoutPrelabelWrapperError("toolchain field set drifted")
    wrapper = _bound_file(toolchain["hash_bound_run_one_wrapper"], label="run-one wrapper")
    if wrapper != runner_path.resolve(strict=True):
        raise HoldoutPrelabelWrapperError("active run-one wrapper is not the bound wrapper")


def _run_ffmpeg(
    argv: list[str],
    *,
    process_runner: Callable[..., subprocess.CompletedProcess[bytes]],
    input_bytes: bytes | None = None,
) -> bytes:
    try:
        result = process_runner(
            argv,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=EXECUTION_PARAMETERS["ffmpeg_timeout_s"],
            env=SUBPROCESS_ENV,
        )
    except subprocess.TimeoutExpired as exc:
        raise HoldoutPrelabelWrapperError("ffmpeg timed out") from exc
    if result.returncode != 0 or not isinstance(result.stdout, bytes) or not result.stdout:
        stderr = result.stderr.decode("utf-8", errors="replace")[:400]
        raise HoldoutPrelabelWrapperError(
            f"ffmpeg failed or returned empty output (rc={result.returncode}): {stderr}"
        )
    return result.stdout


def build_execution_callbacks(
    plan: Mapping[str, object],
    *,
    runner_path: Path,
    process_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> tuple[Callable[[Path], bytes], Callable[[bytes], bytes], Callable[[bytes], Mapping[str, object]]]:
    """Build fixed ffmpeg/BCUT callbacks after the plan passes execution authority."""

    validate_executor_plan(plan, runner_path=runner_path)
    toolchain = plan.get("toolchain")
    if toolchain.get("execution_parameters") != EXECUTION_PARAMETERS:
        raise HoldoutPrelabelWrapperError("execution parameters drifted")

    _bound_file(toolchain["planner"], label="planner")
    _bound_file(toolchain["hash_bound_aggregate_verifier"], label="aggregate verifier")
    _bound_file(toolchain["ffmpeg"], label="ffmpeg", executable=True)
    python = _bound_file(toolchain["python"], label="Python", executable=True)
    core = _bound_file(toolchain["run_one_core"], label="run-one core")
    _bound_file(toolchain["free_asr_client"], label="ASR client")
    if python != Path(sys.executable).resolve(strict=True):
        raise HoldoutPrelabelWrapperError("bound Python is not the active interpreter")
    if core != Path(prelabel.__file__).resolve(strict=True):
        raise HoldoutPrelabelWrapperError("bound run-one core is not the imported core")

    def source_to_mp3(source_path: Path) -> bytes:
        current_ffmpeg = _bound_file(toolchain["ffmpeg"], label="ffmpeg", executable=True)
        return _run_ffmpeg(
            [
                str(current_ffmpeg),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "+bitexact",
                "-i",
                str(source_path),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                "-map_metadata",
                "-1",
                "-threads",
                "1",
                "-f",
                "mp3",
                "pipe:1",
            ],
            process_runner=process_runner,
        )

    def mp3_to_pcm_s16le(mp3: bytes) -> bytes:
        current_ffmpeg = _bound_file(toolchain["ffmpeg"], label="ffmpeg", executable=True)
        return _run_ffmpeg(
            [
                str(current_ffmpeg),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "+bitexact",
                "-f",
                "mp3",
                "-i",
                "pipe:0",
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-threads",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ],
            process_runner=process_runner,
            input_bytes=mp3,
        )

    def transcribe(mp3: bytes) -> Mapping[str, object]:
        active_network_env = sorted(
            name for name in FORBIDDEN_NETWORK_ENV_NAMES if os.environ.get(name)
        )
        if active_network_env:
            raise HoldoutPrelabelWrapperError(
                "ambient network environment is forbidden for bound BCUT execution: "
                + ",".join(active_network_env)
            )
        current_asr_client, source = _read_bound_bytes(
            toolchain["free_asr_client"],
            label="ASR client",
        )
        namespace: dict[str, object] = {
            "__file__": str(current_asr_client),
            "__name__": "_hash_bound_speaker_holdout_free_asr_client",
        }
        module = types.ModuleType(str(namespace["__name__"]))
        module.__dict__.update(namespace)
        exec(compile(source, str(current_asr_client), "exec"), module.__dict__)  # noqa: S102
        transcribe_bcut = getattr(module, "transcribe_bcut", None)
        if (
            getattr(module, "BCUT_MODEL_ID", None) != "7"
            or getattr(module, "BCUT_BASE", None) != BCUT_BASE
            or not callable(transcribe_bcut)
        ):
            raise HoldoutPrelabelWrapperError("ASR client BCUT endpoint/model binding drifted")
        result = transcribe_bcut(
            mp3,
            poll_interval=EXECUTION_PARAMETERS["bcut_poll_interval_s"],
            poll_timeout=EXECUTION_PARAMETERS["bcut_poll_timeout_s"],
            log=lambda _message: None,
        )
        if not isinstance(result, dict):
            raise HoldoutPrelabelWrapperError("BCUT returned a non-object result")
        return {**result, "provider": "bcut"}

    return source_to_mp3, mp3_to_pcm_s16le, transcribe


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-plan-file-sha256", required=True)
    parser.add_argument("--segment-id")
    parser.add_argument("--attempt-id")
    args = parser.parse_args(argv)
    if bool(args.segment_id) != bool(args.attempt_id):
        parser.error("--segment-id and --attempt-id must be supplied together")

    plan = prelabel.load_plan(
        args.plan,
        expected_file_sha256=args.expected_plan_file_sha256,
    )
    if not args.segment_id:
        print(
            json.dumps(
                {
                    "status": "VALIDATED_ONLY_EXECUTION_NOT_ATTEMPTED",
                    "plan_status": plan["status"],
                    "plan_payload_sha256": plan["deterministic_payload_sha256"],
                    "segment_count": plan["segment_count"],
                    "external_audio_upload_authorized": plan["execution_contract"][
                        "external_audio_upload_authorized"
                    ],
                },
                sort_keys=True,
            )
        )
        return 0

    source_to_mp3, mp3_to_pcm_s16le, transcribe_bcut = build_execution_callbacks(
        plan,
        runner_path=Path(__file__).resolve(strict=True),
    )
    receipt = prelabel.run_one(
        plan=plan,
        segment_id=args.segment_id,
        attempt_id=args.attempt_id,
        runner_path=Path(__file__).resolve(strict=True),
        source_to_mp3=source_to_mp3,
        mp3_to_pcm_s16le=mp3_to_pcm_s16le,
        transcribe_bcut=transcribe_bcut,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
