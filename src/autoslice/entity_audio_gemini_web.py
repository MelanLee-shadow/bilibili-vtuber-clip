"""Shadow-only Gemini consumer-web witness handoff and fallback."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from src.autoslice.llm_client import LlmJsonParseError, extract_json_object


ENTITY_AUDIO_GEMINI_WEB_ENABLED_ENV = "ENTITY_AUDIO_GEMINI_WEB_ENABLED"
ENTITY_AUDIO_GEMINI_WEB_MODEL_ENV = "ENTITY_AUDIO_GEMINI_WEB_MODEL"
ENTITY_AUDIO_GEMINI_WEB_COMMAND_ENV = "ENTITY_AUDIO_GEMINI_WEB_COMMAND"
ENTITY_AUDIO_GEMINI_WEB_PYTHON_ENV = "ENTITY_AUDIO_GEMINI_WEB_PYTHON"
ENTITY_AUDIO_GEMINI_WEB_PROFILE_ENV = "ENTITY_AUDIO_GEMINI_WEB_PROFILE"
ENTITY_AUDIO_GEMINI_WEB_BROWSER_ENV = "ENTITY_AUDIO_GEMINI_WEB_BROWSER"
ENTITY_AUDIO_GEMINI_WEB_XVFB_ENV = "ENTITY_AUDIO_GEMINI_WEB_XVFB"
ENTITY_AUDIO_GEMINI_WEB_USER_ENV = "ENTITY_AUDIO_GEMINI_WEB_USER"
ENTITY_AUDIO_GEMINI_WEB_TIMEOUT_ENV = "ENTITY_AUDIO_GEMINI_WEB_TIMEOUT_SECONDS"


@dataclass(frozen=True)
class _EntityProviderOutcome:
    observed: Any
    provider: str
    model: str
    prompt_path: Path
    response_path: Path
    accepted_key_tier: str | None
    paid_policy_stamp: Mapping[str, Any] | None
    provider_failures: list[dict[str, Any]]
    accepted_key_ordinal: int | None = None
    configured_key_count: int = 0
    served_from_cache: bool = False
    provider_receipt_sha256: str | None = None
    provider_model_label: str | None = None
    provider_model_status: str | None = None
    provider_input_sha256: str | None = None


def _provider_provenance(outcome: _EntityProviderOutcome) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "provider_receipt_sha256": outcome.provider_receipt_sha256,
            "provider_model_label": outcome.provider_model_label,
            "provider_model_status": outcome.provider_model_status,
            "provider_input_sha256": outcome.provider_input_sha256,
        }.items()
        if value
    }


def _provider_artifact_bindings(
    outcome: _EntityProviderOutcome,
    sha256: Callable[[Path], str],
) -> dict[str, Any]:
    """Return the hash/provenance fields shared by provider manifests."""

    return {
        "prompt_sha256": sha256(outcome.prompt_path),
        "response_sha256": sha256(outcome.response_path),
        "model": outcome.model,
        "provider": outcome.provider,
        **_provider_provenance(outcome),
    }


def gemini_web_enabled() -> bool:
    """Open the web leg only for an explicitly no-upload shadow run."""

    return (
        os.environ.get(ENTITY_AUDIO_GEMINI_WEB_ENABLED_ENV, "").strip() == "1"
        and os.environ.get("AUTOSLICE_SHADOW_ONLY", "").strip() == "1"
        and os.environ.get("AUTOSLICE_UPLOAD_ENABLED", "").strip() == "0"
    )


def _web_path(
    value: str | Path,
    label: str,
    *,
    directory: bool = False,
    executable: bool = False,
    max_bytes: int | None = None,
) -> Path:
    path = Path(value).expanduser()
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} unavailable") from exc
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if path.is_symlink() or not valid or (not directory and info.st_size <= 0):
        raise ValueError(f"{label} has an invalid file type")
    if executable and not os.access(path, os.X_OK):
        raise ValueError(f"{label} is not executable")
    if max_bytes is not None and info.st_size > max_bytes:
        raise ValueError(f"{label} is oversized")
    return path


_WEB_HANDOFF_ENTRIES = frozenset(
    {"input.wav", "prompt.md", "receipt.json", "response.json", "screenshots"}
)


def _prepare_web_handoff(
    *,
    job_dir: Path,
    audio_path: Path,
    prompt: str,
    user_name: str,
) -> tuple[Path, Path, Path, Path, Path]:
    """Create a private, fixed-shape handoff for the non-root adapter."""

    handoff = job_dir / "gemini-web"
    if handoff.exists() or handoff.is_symlink():
        _web_path(handoff, "web handoff", directory=True)
    else:
        handoff.mkdir(mode=0o700)
    handoff.chmod(0o700)
    if {entry.name for entry in handoff.iterdir()} - _WEB_HANDOFF_ENTRIES:
        raise ValueError("web handoff contains unexpected files")

    input_path = handoff / "input.wav"
    prompt_path = handoff / "prompt.md"
    receipt_path = handoff / "receipt.json"
    response_path = handoff / "response.json"
    screenshot_dir = handoff / "screenshots"
    for output in (receipt_path, response_path):
        if output.exists() or output.is_symlink():
            _web_path(output, "web handoff output")
            output.unlink()
    if screenshot_dir.exists() or screenshot_dir.is_symlink():
        _web_path(screenshot_dir, "web screenshot directory", directory=True)
        for child in screenshot_dir.iterdir():
            _web_path(child, "web screenshot entry")
            child.unlink()
        screenshot_dir.rmdir()

    for output in (input_path, prompt_path):
        if output.exists() or output.is_symlink():
            _web_path(output, "web handoff input")
    _web_path(audio_path, "web audio")
    temporary = input_path.with_name(f".{input_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(audio_path.read_bytes())
        temporary.chmod(0o600)
        os.replace(temporary, input_path)
        input_path.chmod(0o600)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()
    temporary = prompt_path.with_name(f".{prompt_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(prompt, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, prompt_path)
        prompt_path.chmod(0o600)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()

    if os.geteuid() == 0:
        try:
            account = pwd.getpwnam(user_name)
        except KeyError as exc:
            raise ValueError("web user is not configured") from exc
        if account.pw_uid == 0:
            raise ValueError("web adapter must not run as root")
        try:
            os.chown(handoff, account.pw_uid, account.pw_gid)
            os.chown(input_path, account.pw_uid, account.pw_gid)
            os.chown(prompt_path, account.pw_uid, account.pw_gid)
        except OSError as exc:
            raise ValueError("web handoff ownership could not be secured") from exc
    return handoff, input_path, prompt_path, receipt_path, response_path


def _web_handoff_read(path: Path, label: str, *, max_bytes: int) -> bytes:
    return _web_path(path, label, max_bytes=max_bytes).read_bytes()


def _load_web_receipt(
    *,
    handoff: Path,
    receipt_path: Path,
    response_path: Path,
    audio_path: Path,
    prompt_path: Path,
    requested_model: str,
    sha256: Callable[[Path], str],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    receipt_bytes = _web_handoff_read(receipt_path, "web receipt", max_bytes=256_000)
    response_bytes = _web_handoff_read(response_path, "web response", max_bytes=2_000_000)
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
        raw_response = response_bytes.decode("utf-8")
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("web receipt or response is not valid UTF-8 JSON") from exc
    if not isinstance(receipt, dict) or not isinstance(raw_response, str):
        raise ValueError("web receipt or response shape is invalid")
    response_sha256 = hashlib.sha256(response_bytes).hexdigest()
    expected_response = str(response_path.resolve())
    backend_identity = receipt.get("backend_identity")
    if (
        receipt.get("schema_version") != "gemini-web-subscription-receipt.v1"
        or receipt.get("provider") != "gemini_web_subscription"
        or receipt.get("mode") != "run"
        or receipt.get("status") != "SUCCESS"
        or receipt.get("video_sha256") != sha256(audio_path)
        or receipt.get("prompt_sha256") != sha256(prompt_path)
        or receipt.get("raw_response_sha256") != response_sha256
        or receipt.get("raw_response_path") != expected_response
        or receipt.get("requested_model_label") != requested_model
        or receipt.get("observed_model_label") != requested_model
        or receipt.get("backend_model_status") != "UNVERIFIED"
        or not isinstance(backend_identity, Mapping)
        or backend_identity.get("status") != "UNVERIFIED"
        or backend_identity.get("model_id") is not None
    ):
        raise ValueError("web receipt binding is invalid")
    try:
        observed = extract_json_object(raw_response)
    except (LlmJsonParseError, TypeError, ValueError) as exc:
        raise ValueError("web response JSON is invalid") from exc
    if not isinstance(observed, dict):
        raise ValueError("web response must be an object")
    screenshots = receipt.get("screenshots")
    if not isinstance(screenshots, list):
        raise ValueError("web screenshot receipt is invalid")
    screenshot_dir = handoff / "screenshots"
    for item in screenshots:
        if not isinstance(item, Mapping):
            raise ValueError("web screenshot receipt entry is invalid")
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            raise ValueError("web screenshot path is invalid")
        screenshot_path = Path(raw_path)
        if screenshot_path.parent != screenshot_dir.resolve():
            raise ValueError("web screenshot escaped handoff")
        _web_handoff_read(screenshot_path, "web screenshot", max_bytes=20_000_000)
        if item.get("sha256") != sha256(screenshot_path):
            raise ValueError("web screenshot hash mismatch")
    return receipt, observed, receipt_sha256


def terminate_web_process_group(process: Any) -> None:
    """Reap the adapter and its browser/Xvfb descendants after a timeout."""

    pid = getattr(process, "pid", None)
    try:
        pgid = os.getpgid(pid) if isinstance(pid, int) and pid > 0 else None
    except OSError:
        pgid = None
    if pgid is not None:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                return
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)
        if callable(wait):
            with contextlib.suppress(Exception):
                wait(timeout=5)
        return
    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        with contextlib.suppress(Exception):
            terminate()
    wait = getattr(process, "wait", None)
    if callable(wait):
        try:
            wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            return
    kill = getattr(process, "kill", None)
    if callable(kill):
        with contextlib.suppress(Exception):
            kill()
    if callable(wait):
        with contextlib.suppress(Exception):
            wait(timeout=5)


def run_gemini_web_fallback(
    *,
    request: Mapping[str, Any],
    audio_path: Path,
    job_dir: Path,
    recording_date: str,
    provider_failures: list[dict[str, Any]],
    prompt_builder: Callable[..., str],
    sha256: Callable[[Path], str],
    terminate_process_group: Callable[[Any], None],
) -> _EntityProviderOutcome | None:
    """Run the consumer-web witness out of process and return only raw JSON."""

    model_label = os.environ.get(ENTITY_AUDIO_GEMINI_WEB_MODEL_ENV, "3.1 Pro").strip()
    user_name = os.environ.get(ENTITY_AUDIO_GEMINI_WEB_USER_ENV, "").strip()
    command_value = os.environ.get(ENTITY_AUDIO_GEMINI_WEB_COMMAND_ENV, "").strip()
    python_value = os.environ.get(ENTITY_AUDIO_GEMINI_WEB_PYTHON_ENV, "").strip()
    profile_value = os.environ.get(ENTITY_AUDIO_GEMINI_WEB_PROFILE_ENV, "").strip()
    browser_value = os.environ.get(ENTITY_AUDIO_GEMINI_WEB_BROWSER_ENV, "").strip()
    xvfb_value = os.environ.get(ENTITY_AUDIO_GEMINI_WEB_XVFB_ENV, "").strip()
    adapter_default = str(Path(__file__).resolve().parents[2] / "scripts" / "gemini_web_subscription.py")
    receipt_sha256: str | None = None
    receipt_status: str | None = None
    try:
        adapter_path = _web_path(command_value or adapter_default, "web adapter")
        if python_value:
            python_path = _web_path(python_value, "web Python", executable=True)
        elif os.geteuid() == 0:
            raise ValueError("web Python is not configured")
        else:
            python_path = _web_path(sys.executable, "web Python", executable=True)
        profile = _web_path(profile_value, "web profile", directory=True)
        browser = _web_path(browser_value, "web Chromium", executable=True)
        xvfb = _web_path(xvfb_value, "web Xvfb", executable=True) if xvfb_value else None
        if os.geteuid() == 0 and not user_name:
            raise ValueError("web user is not configured")
        try:
            timeout_seconds = max(
                1,
                int(os.environ.get(ENTITY_AUDIO_GEMINI_WEB_TIMEOUT_ENV, "360")),
            )
        except ValueError as exc:
            raise ValueError("web timeout is invalid") from exc
        prompt = prompt_builder(
            recording_date=recording_date,
            delivery_mode="gemini_api",
            syllable_count_hint=request.get("syllable_count_hint"),
            target_audio_start_ms=request.get("target_audio_start_ms"),
            target_audio_end_ms=request.get("target_audio_end_ms"),
        )
        # Chromium in the proven OCI3 runtime accepts WAV but cannot decode the
        # source MP4's H.264/AAC payload.  Keep the source crop hash in the
        # manifest while binding the web receipt to this exact WAV handoff.
        web_audio = job_dir / "input.gemini-web.wav"
        extract = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(audio_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(web_audio),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if extract.returncode != 0:
            raise ValueError("web audio extraction failed")
        _web_handoff_read(web_audio, "web audio", max_bytes=100_000_000)
        handoff, input_path, prompt_path, receipt_path, response_path = _prepare_web_handoff(
            job_dir=job_dir,
            audio_path=web_audio,
            prompt=prompt,
            user_name=user_name or str(os.getuid()),
        )
        screenshot_dir = handoff / "screenshots"
        argv = [
            str(python_path),
            str(adapter_path),
            "run",
            str(input_path),
            "--prompt-file",
            str(prompt_path),
            "--model-label",
            model_label,
            "--profile-dir",
            str(profile),
            "--receipt",
            str(receipt_path),
            "--response-out",
            str(response_path),
            "--screenshot-dir",
            str(screenshot_dir),
            "--direct-cdp",
            "--browser-executable",
            str(browser),
        ]
        if xvfb is not None:
            argv.extend(("--xvfb-bin", str(xvfb)))
        if os.geteuid() == 0:
            runuser_path = _web_path("/usr/sbin/runuser", "runuser", executable=True)
            argv = [str(runuser_path), "--user", user_name, "--", *argv]
        try:
            account = pwd.getpwnam(user_name) if user_name else pwd.getpwuid(os.getuid())
        except KeyError as exc:
            raise ValueError("web user is unavailable") from exc
        if os.geteuid() != 0 and account.pw_uid != os.geteuid():
            raise ValueError("web user does not match the current account")
        child_env = {
            "HOME": account.pw_dir,
            "USER": account.pw_name,
            "LOGNAME": account.pw_name,
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        adapter_process = subprocess.Popen(
            argv,
            cwd=str(handoff),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = adapter_process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            terminate_process_group(adapter_process)
            raise
        completed = subprocess.CompletedProcess(argv, adapter_process.returncode, stdout, stderr)
        with contextlib.suppress(ValueError):
            receipt_bytes = _web_handoff_read(receipt_path, "web receipt", max_bytes=256_000)
            receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
            raw_receipt = json.loads(receipt_bytes.decode("utf-8"))
            if isinstance(raw_receipt, Mapping):
                receipt_status = str(raw_receipt.get("status") or "") or None
        if completed.returncode != 0:
            row = {
                "provider": "gemini_web_subscription",
                "category": f"GEMINI_WEB_{receipt_status or 'SUBPROCESS_FAILED'}",
                "attempted": True,
                "error_type": "ProviderProcessError",
            }
            if receipt_sha256:
                row["receipt_sha256"] = receipt_sha256
            if receipt_status:
                row["receipt_status"] = receipt_status
            provider_failures.append(row)
            return None
        receipt, observed, receipt_sha256 = _load_web_receipt(
            handoff=handoff,
            receipt_path=receipt_path,
            response_path=response_path,
            audio_path=web_audio,
            prompt_path=prompt_path,
            requested_model=model_label,
            sha256=sha256,
        )
        return _EntityProviderOutcome(
            observed=observed,
            provider="gemini_web_subscription",
            model=model_label,
            prompt_path=prompt_path,
            response_path=response_path,
            accepted_key_tier=None,
            paid_policy_stamp=None,
            provider_failures=list(provider_failures),
            provider_receipt_sha256=receipt_sha256,
            provider_model_label=str(receipt.get("observed_model_label") or model_label),
            provider_model_status="UNVERIFIED",
            provider_input_sha256=sha256(web_audio),
        )
    except subprocess.TimeoutExpired:
        provider_failures.append(
            {
                "provider": "gemini_web_subscription",
                "category": "GEMINI_WEB_TIMEOUT",
                "attempted": True,
            }
        )
    except Exception as exc:
        row = {
            "provider": "gemini_web_subscription",
            "category": (
                "GEMINI_WEB_INVALID_RESPONSE"
                if "receipt" in str(exc).lower() or "response" in str(exc).lower()
                else "GEMINI_WEB_CONFIG_OR_TRANSPORT_FAILED"
            ),
            "attempted": True,
            "error_type": type(exc).__name__,
        }
        if receipt_sha256:
            row["receipt_sha256"] = receipt_sha256
        if receipt_status:
            row["receipt_status"] = receipt_status
        provider_failures.append(row)
    return None


def run_gemini_web_fallback_from_verifier(
    **kwargs: Any,
) -> _EntityProviderOutcome | None:
    """Run through verifier-owned seams resolved afresh for each invocation."""

    from src.autoslice import entity_audio_verifier

    return run_gemini_web_fallback(
        **kwargs,
        prompt_builder=entity_audio_verifier._witness_prompt,
        sha256=entity_audio_verifier._sha256,
        terminate_process_group=entity_audio_verifier._terminate_web_process_group,
    )


def run_gemini_web_fallback_if_enabled(
    eligible: bool,
    request: Mapping[str, Any],
    audio_path: Path,
    job_dir: Path,
    recording_date: str,
    provider_failures: list[dict[str, Any]],
    fallback: Callable[..., Any],
) -> Any | None:
    if not eligible or not gemini_web_enabled():
        return None
    return fallback(
        request=request,
        audio_path=audio_path,
        job_dir=job_dir,
        recording_date=recording_date,
        provider_failures=provider_failures,
    )
