"""Read provider-local audio credentials from a bound native runtime.

The native MAI and MOSS clients normally support an explicitly supplied
ambient environment.  A deployed producer can instead bind a runtime root
through ``AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT`` (or ``AUTOSLICE_BASE``).  This
module reads only the selected provider's private ``*.env`` file and returns a
new mapping; it never mutates ``os.environ`` and never returns credentials for
another provider.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
import re
import shlex
import stat
from typing import Mapping


_ENV_MAX_BYTES = 64 * 1024
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROVIDER_ENV_FILE = {"mai": "mai.env", "moss": "moss.env"}
_ALLOWED_KEYS = {
    "mai": frozenset({"AZURE_ENDPOINT", "AZURE_API_KEY", "MAI_KEY_FILE", "AUTOSLICE_MAI_API_KEY_FILE"}),
    "moss": frozenset({"AUTOSLICE_MOSS_API_KEY", "AUTOSLICE_MOSS_API_KEY_FILE", "MOSS_API_KEY"}),
}


class NativeAudioRuntimeError(RuntimeError):
    """A sanitized, typed failure while reading a native provider runtime."""

    def __init__(self, reason_code: str, message: str = "native audio runtime configuration failed") -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _configured_root(environ: Mapping[str, str]) -> Path | None:
    for name in ("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", "AUTOSLICE_BASE"):
        value = str(environ.get(name, "")).strip()
        if value:
            return Path(value).expanduser()
    return None


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IMODE(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
    )


def _accepted_owner(info: os.stat_result) -> bool:
    # Deployed files are root-owned.  Local test/development runtimes are
    # owned by the process user; accepting that owner preserves the same
    # owner-only contract without permitting an unrelated account.
    return info.st_uid in {0, os.geteuid()}


def _assert_runtime_root(root: Path) -> Path:
    root = root.absolute()
    cursor = Path(root.anchor)
    try:
        for part in root.parts[1:]:
            cursor /= part
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_UNSAFE")
    except NativeAudioRuntimeError:
        raise
    except FileNotFoundError:
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_MISSING") from None
    except OSError:
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_UNSAFE") from None
    return root


def _read_private_env(path: Path) -> str:
    try:
        initial = os.lstat(path)
    except FileNotFoundError:
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_MISSING") from None
    except OSError:
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_UNSAFE") from None
    if (
        stat.S_ISLNK(initial.st_mode)
        or not stat.S_ISREG(initial.st_mode)
        or not _accepted_owner(initial)
        or stat.S_IMODE(initial.st_mode) & 0o077
        or not 0 < initial.st_size <= _ENV_MAX_BYTES
    ):
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_UNSAFE")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    expected = _identity(initial)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _identity(opened) != expected or not stat.S_ISREG(opened.st_mode):
                raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_UNSAFE")
            payload = bytearray()
            while len(payload) <= _ENV_MAX_BYTES:
                block = os.read(descriptor, 4096)
                if not block:
                    break
                payload.extend(block)
            after_descriptor = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = os.lstat(path)
    except NativeAudioRuntimeError:
        raise
    except FileNotFoundError:
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_MISSING") from None
    except OSError:
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_UNSAFE") from None
    if (
        len(payload) != initial.st_size
        or len(payload) > _ENV_MAX_BYTES
        or _identity(after_descriptor) != expected
        or _identity(after_path) != expected
    ):
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_UNSAFE")
    try:
        return bytes(payload).decode("utf-8")
    except UnicodeDecodeError:
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_INVALID") from None


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or _KEY.fullmatch(key) is None:
            raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_INVALID")
        if key in values:
            raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_INVALID")
        try:
            parts = shlex.split(raw_value, comments=False, posix=True)
        except ValueError:
            raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_INVALID") from None
        if len(parts) != 1:
            raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_INVALID")
        values[key] = parts[0]
    return values


def _relative_file(value: str, root: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return str(path)


def _provider_values(provider: str, raw: Mapping[str, str], root: Path) -> dict[str, str]:
    values = {key: value for key, value in raw.items() if key in _ALLOWED_KEYS[provider]}
    if provider == "mai" and "MAI_KEY_FILE" in values:
        if "AUTOSLICE_MAI_API_KEY_FILE" in values:
            raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_INVALID")
        values["AUTOSLICE_MAI_API_KEY_FILE"] = values.pop("MAI_KEY_FILE")
    if provider == "moss" and "MOSS_API_KEY" in values:
        if "AUTOSLICE_MOSS_API_KEY" in values:
            raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_INVALID")
        values["AUTOSLICE_MOSS_API_KEY"] = values.pop("MOSS_API_KEY")
    for key in ("AUTOSLICE_MAI_API_KEY_FILE", "AUTOSLICE_MOSS_API_KEY_FILE"):
        if key in values:
            values[key] = _relative_file(values[key], root)

    if provider == "mai":
        if not values.get("AZURE_ENDPOINT", "").strip() or not (
            values.get("AZURE_API_KEY", "").strip()
            or values.get("AUTOSLICE_MAI_API_KEY_FILE", "").strip()
        ):
            raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_FIELDS_MISSING")
    elif not (
        values.get("AUTOSLICE_MOSS_API_KEY", "").strip()
        or values.get("AUTOSLICE_MOSS_API_KEY_FILE", "").strip()
    ):
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_FIELDS_MISSING")
    return values


def resolve_native_audio_environment(
    provider: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return provider-only runtime values, or ``None`` for ambient mode.

    An explicit runtime binding is fail-closed: a missing or unsafe provider
    file raises ``NativeAudioRuntimeError`` rather than falling back to the
    caller's environment.  The returned dictionary contains no runtime,
    CPA, or other-provider keys.
    """

    if provider not in _PROVIDER_ENV_FILE:
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_PROVIDER_INVALID")
    ambient = os.environ if environ is None else environ
    root = _configured_root(ambient)
    if root is None:
        return None
    bound_root = _assert_runtime_root(root)
    env_path = bound_root / _PROVIDER_ENV_FILE[provider]
    raw = _parse_env(_read_private_env(env_path))
    return _provider_values(provider, raw, bound_root)


def validated_native_audio_values(provider: str, *, environ: Mapping[str, str]) -> dict[str, str]:
    """Resolve and validate one provider without dispatching HTTP.

    Values stay in memory. Key-file references are materialized only for the
    protected controller-to-worker configuration transport.
    """
    values = resolve_native_audio_environment(provider, environ=environ)
    values = dict(environ) if values is None else values
    if provider == "mai":
        from src.autoslice.mai_transcription import _endpoint_url, _read_api_key
        _endpoint_url(values)
        return {"AZURE_ENDPOINT": values["AZURE_ENDPOINT"], "AZURE_API_KEY": _read_api_key(values)}
    from src.autoslice.moss_transcription import _read_api_key
    return {"AUTOSLICE_MOSS_API_KEY": _read_api_key(values)}


def native_audio_readiness(runtime_root: Path) -> list[dict]:
    """Return sanitized actual-adapter readiness; never keys or fingerprints."""
    root = _assert_runtime_root(Path(runtime_root))
    result = []
    for provider, name in _PROVIDER_ENV_FILE.items():
        path = root / name
        row = {"provider": provider, "source": str(path), "ready": False}
        try:
            values = validated_native_audio_values(provider, environ={"AUTOSLICE_BASE": str(root)})
            row.update(ready=True, variables=sorted(values), mode=oct(stat.S_IMODE(path.stat().st_mode)),
                       owner_uid=path.stat().st_uid)
        except Exception as exc:
            row["reason_code"] = getattr(exc, "reason_code", "NATIVE_AUDIO_RUNTIME_CONFIG_INVALID")
        result.append(row)
    return result


def install_native_audio_configuration(runtime_root: Path, payload: Mapping) -> dict:
    """Create missing provider-only files; preserve existing node credentials.

    This operation is independent of immutable code versions and job stores.
    Input arrives only on SSH stdin; neither values nor digests enter receipts.
    """
    root = _assert_runtime_root(Path(runtime_root))
    if not isinstance(payload, dict) or set(payload) != set(_PROVIDER_ENV_FILE):
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_INVALID")
    expected = {"mai": {"AZURE_ENDPOINT", "AZURE_API_KEY"}, "moss": {"AUTOSLICE_MOSS_API_KEY"}}
    for provider, values in payload.items():
        if (not isinstance(values, dict) or set(values) != expected[provider]
                or any(not isinstance(v, str) or not v.strip() or any(c in v for c in "\r\n\x00")
                       for v in values.values())):
            raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_INVALID")
        validated_native_audio_values(provider, environ=values)
    created = []
    for provider, name in _PROVIDER_ENV_FILE.items():
        path = root / name
        # Existing node-specific keys are never replaced by controller copies.
        if path.exists() or path.is_symlink():
            _provider_values(provider, _parse_env(_read_private_env(path)), root)
            continue
        data = "".join(f"{k}={shlex.quote(v)}\n" for k, v in payload[provider].items()).encode()
        temporary = path.with_name('.' + name + '.' + os.urandom(12).hex())
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Publish complete bytes atomically without replacing a node key.
            os.link(temporary, path, follow_symlinks=False)
        finally:
            if temporary.exists():
                temporary.unlink()
        directory = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        created.append(provider)
    readiness = native_audio_readiness(root)
    if not all(row["ready"] for row in readiness):
        raise NativeAudioRuntimeError("NATIVE_AUDIO_RUNTIME_CONFIG_NOT_READY")
    return {"status": "READY", "runtime_root": str(root), "created_providers": created,
            "providers": readiness, "provider_calls": 0}


__all__ = ["NativeAudioRuntimeError", "resolve_native_audio_environment", "native_audio_readiness"]
