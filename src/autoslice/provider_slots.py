"""Runtime-local AIMD admission shared by processes, per provider/modality.

Only metadata transactions take the window mutex. Provider calls hold a unique
lease inode, not that mutex; flock also makes crashed owners reclaimable.
"""

from __future__ import annotations

import fcntl
import errno
import hashlib
import json
import math
import os
import stat
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar, TYPE_CHECKING

if TYPE_CHECKING:
    from src.autoslice.provider_admission import RateFeedback

from src.autoslice.deployed_code_root import deployed_code_root


class ProviderSlotError(RuntimeError):
    """A provider permit could not be established safely."""


class ProviderSlotBusy(ProviderSlotError):
    """Compatibility name for callers that classified immediate saturation."""


class ProviderSlotTimeout(ProviderSlotBusy):
    """No provider permit became available before the bounded wait elapsed."""


@dataclass(frozen=True, slots=True)
class ProviderSlotLease:
    runtime_root: Path
    slot_index: int
    device: int
    inode: int
    report: Callable[[object], None] = field(default=lambda _: None, compare=False, repr=False)


_thread_locks_guard = threading.Lock()
_thread_locks: dict[tuple[int, int, str, int], threading.Lock] = {}
_MAX_PROVIDER_CANDIDATES = 5
_MAX_PROVIDER_QUEUE_SECONDS = 10800
_CANONICAL_PRODUCTION_ROOT = Path("/opt/bilive/autoslice")
_ProviderResult = TypeVar("_ProviderResult")


# The environment supplies only the initial window. Success grows it without
# a numeric ceiling; only structured provider feedback reduces/cools it down.


def provider_capacity() -> int:
    value = os.environ.get("AUTOSLICE_PROVIDER_CONCURRENCY", "20")
    try:
        capacity = int(value)
    except ValueError as exc:
        raise ProviderSlotError("provider concurrency must be a positive integer") from exc
    if capacity < 1:
        raise ProviderSlotError("provider concurrency must be a positive integer")
    return capacity


def provider_wait_seconds() -> float:
    """Return the configured bounded wait; never silently turn it into a retry loop."""

    # AGY's normal 15m process budget plus its grace period is longer than the
    # old 900s cap.  Keep the setting bounded, but do not make a healthy third
    # candidate fail simply because two long-lived provider calls are active.
    value = os.environ.get("AUTOSLICE_PROVIDER_WAIT_SECONDS", str(_MAX_PROVIDER_QUEUE_SECONDS))
    try:
        seconds = int(value)
    except ValueError as exc:
        raise ProviderSlotError("provider wait must be an integer from 1 to 10800 seconds") from exc
    if not 1 <= seconds <= _MAX_PROVIDER_QUEUE_SECONDS:
        raise ProviderSlotError("provider wait must be an integer from 1 to 10800 seconds")
    return float(seconds)


def provider_wait_for_call(call_timeout_seconds: float, *, grace_seconds: float = 60.0) -> float:
    """Return a bounded queue wait which cannot expire before a legal call.

    Adapter-specific subprocess timeouts are local policy; this only makes the
    shared permit wait long enough to accommodate one such call already in a
    slot.  Calls longer than the globally bounded queue policy fail closed.
    """
    if (
        not isinstance(call_timeout_seconds, (int, float))
        or isinstance(call_timeout_seconds, bool)
        or not isinstance(grace_seconds, (int, float))
        or isinstance(grace_seconds, bool)
        or not math.isfinite(call_timeout_seconds) or call_timeout_seconds <= 0
        or not math.isfinite(grace_seconds) or grace_seconds < 0
    ):
        raise ProviderSlotError("provider call timeout is unsafe")
    # The tail candidate waits only for the *preceding* complete waves.  With
    # five candidates and capacity two that is two waves (not three).
    waves = (_MAX_PROVIDER_CANDIDATES - 1) // provider_capacity()
    required = float(call_timeout_seconds) * waves + float(grace_seconds)
    if required > _MAX_PROVIDER_QUEUE_SECONDS:
        raise ProviderSlotError("provider call timeout exceeds the 10800 second queue bound")
    return max(provider_wait_seconds(), required)


def configured_runtime_root() -> Path | None:
    """Return the explicitly declared shared runtime, never an invented one."""
    configured = os.environ.get("AUTOSLICE_BASE")
    if configured:
        return Path(configured)
    # A live host must never silently skip permits because an older manual
    # invocation omitted the variable.  Local development remains opt-in: it
    # does not normally have this exact production runtime topology.
    if _CANONICAL_PRODUCTION_ROOT.is_dir() and deployed_code_root(_CANONICAL_PRODUCTION_ROOT).is_dir():
        return _CANONICAL_PRODUCTION_ROOT
    return None


@contextmanager
def runtime_provider_slot(
    *,
    runtime_root: Path | str | None = None,
    timeout_seconds: float | None = None,
    provider: str = "cpa",
    modality: str = "text",
) -> Iterator[ProviderSlotLease | None]:
    """Permit provider transport for an explicit or canonical runtime root."""
    resolved_root = Path(runtime_root) if runtime_root is not None else configured_runtime_root()
    if resolved_root is None:
        yield None
        return
    key = (os.getpid(), threading.get_ident(), str(resolved_root.absolute()), *_route_key(provider, modality))
    # Physical transports nested inside admission wrappers share one lease;
    # explicit provider_slot callers still acquire independent permits.
    for held_key, held_lease in _HELD.get():
        if held_key == key:
            try:
                yield held_lease
            except BaseException as exc:
                from src.autoslice.provider_admission import rate_feedback
                held_lease.report(rate_feedback(exc))
                raise
            return
    with provider_slot(resolved_root, timeout_seconds=timeout_seconds,
                       provider=provider, modality=modality) as lease:
        token = _HELD.set((*_HELD.get(), (key, lease)))
        try:
            yield lease
        finally:
            _HELD.reset(token)


def provider_transport(
    *, timeout_result: Callable[[], _ProviderResult] | None = None,
    provider: str = "cpa", modality: str = "image",
) -> Callable[[Callable[..., _ProviderResult]], Callable[..., _ProviderResult]]:
    """Bind one transport function to one permit for its complete call."""

    def decorate(call: Callable[..., _ProviderResult]) -> Callable[..., _ProviderResult]:
        @wraps(call)
        def dispatch(*args: Any, **kwargs: Any) -> _ProviderResult:
            timeout = kwargs.get("timeout_seconds", 180.0)
            try:
                with runtime_provider_slot(timeout_seconds=provider_wait_for_call(timeout),
                                           provider=provider, modality=modality) as lease:
                    result = call(*args, **kwargs)
                    if lease is not None:
                        lease.report(result)
                    return result
            except ProviderSlotTimeout:
                if timeout_result is None:
                    raise
                return timeout_result()

        # Keep the decorator's non-secret route explicit for checkpoint binding.
        dispatch.provider_runtime_binding = {"adapter": "provider_slots.provider_transport",
                                             "provider": provider, "modality": modality}
        dispatch._provider_transport_callables = (call, timeout_result)
        return dispatch

    return decorate


def _safe_directory(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProviderSlotError("provider slot directory cannot be created") from exc
        info = os.lstat(path)
    except OSError as exc:
        raise ProviderSlotError("provider slot directory cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProviderSlotError("provider slot directory is unsafe")


def _safe_runtime_root(path: Path) -> os.stat_result:
    if not path.is_absolute():
        raise ProviderSlotError("provider runtime root is unsafe")
    cursor = Path(path.anchor)
    info: os.stat_result | None = None
    for part in path.parts[1:]:
        cursor /= part
        try:
            info = os.lstat(cursor)
        except FileNotFoundError as exc:
            # Direct adapter callers historically supplied an empty per-run
            # ``AUTOSLICE_BASE`` and expected the permit namespace to be
            # initialized there.  Only the final authority component may be
            # created: walking or creating a missing ancestor would make a
            # misspelled/redirected runtime root authoritative.
            if cursor != path:
                raise ProviderSlotError("provider runtime root cannot be inspected") from exc
            try:
                os.mkdir(cursor, 0o700)
            except OSError as mkdir_exc:
                if mkdir_exc.errno != errno.EEXIST:
                    raise ProviderSlotError("provider runtime root cannot be created") from mkdir_exc
            try:
                info = os.lstat(cursor)
            except OSError as stat_exc:
                raise ProviderSlotError("provider runtime root cannot be inspected") from stat_exc
        except OSError as exc:
            raise ProviderSlotError("provider runtime root cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ProviderSlotError("provider runtime root is unsafe")
    if info is None:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise ProviderSlotError("provider runtime root cannot be inspected") from exc
    return info


def _slot_open_flags() -> int:
    """Require a no-follow, close-on-exec open rather than emulating it unsafely."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    cloexec = getattr(os, "O_CLOEXEC", None)
    if not isinstance(nofollow, int) or not isinstance(cloexec, int):
        raise ProviderSlotError("provider slot safe open is unavailable")
    return os.O_RDWR | os.O_CREAT | nofollow | cloexec


def _same_safe_slot(path: Path, descriptor: int, opened: os.stat_result) -> None:
    try:
        current = os.lstat(path)
        live = os.fstat(descriptor)
    except OSError as exc:
        raise ProviderSlotError("provider slot file cannot be revalidated") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o600
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or stat.S_IMODE(current.st_mode) != 0o600
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        or (live.st_dev, live.st_ino) != (opened.st_dev, opened.st_ino)
        or not stat.S_ISREG(live.st_mode)
        or stat.S_IMODE(live.st_mode) != 0o600
    ):
        raise ProviderSlotError("provider slot file ownership drifted")


def _slot_fd(path: Path) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(path, _slot_open_flags(), 0o600)
    except OSError as exc:
        raise ProviderSlotError("provider slot file cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        _same_safe_slot(path, descriptor, opened)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _thread_lock(runtime_identity: tuple[int, int], slot_index: int, route: str = "") -> threading.Lock:
    key = (*runtime_identity, route, slot_index)
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


_HeldKey = tuple[int, int, str, str, str]
_HELD: ContextVar[tuple[tuple[_HeldKey, ProviderSlotLease], ...]] = ContextVar("provider_slots", default=())


def _route_key(provider: str, modality: str) -> tuple[str, str]:
    if provider.startswith("canonical-runtime-cpa:"):
        provider = "cpa"
    if provider == "gemini_api":
        provider = "gemini"
    if not provider or not modality:
        raise ProviderSlotError("provider route must be nonempty")
    return provider, modality


def report_provider_feedback(value: object) -> None:
    """Preserve protocol feedback even when a transport catches its exception."""
    from src.autoslice.provider_admission import rate_feedback
    for key, lease in reversed(_HELD.get()):
        if key[:2] == (os.getpid(), threading.get_ident()):
            lease.report(rate_feedback(value) if isinstance(value, BaseException) else value)
            return


def _route_root(runtime_root: Path, provider: str, modality: str) -> Path:
    root = runtime_root / "provider-slots"
    _safe_directory(root)
    key = _route_key(provider, modality)
    # Preserve the historical default namespace, never a second global cap.
    if key == ("cpa", "text"):
        return root
    routes = root / "routes"
    _safe_directory(routes)
    bucket = routes / hashlib.sha256(json.dumps(key).encode()).hexdigest()
    _safe_directory(bucket)
    return bucket


@dataclass
class _WindowState:
    limit: int
    active: list[int] = field(default_factory=list)
    retry_at: float = 0.0


def _read_window(root: Path, route: tuple[str, str]) -> _WindowState:
    path = root / "window.json"
    try:
        os.lstat(path)
    except FileNotFoundError:
        return _WindowState(provider_capacity())
    except OSError as exc:
        raise ProviderSlotError("provider window state cannot be inspected") from exc
    fd, opened = _slot_fd(path)
    try:
        with os.fdopen(os.dup(fd), encoding="utf-8") as stream:
            value = json.load(stream)
        _same_safe_slot(path, fd, opened)
        if (not isinstance(value, dict) or value.get("version") != 1
                or value.get("route") != list(route)
                or type(value.get("limit")) is not int or value["limit"] < 1
                or not isinstance(value.get("active"), list)
                or any(type(index) is not int or index < 0 for index in value["active"])
                or len(set(value["active"])) != len(value["active"])
                or type(value.get("retry_at")) not in (int, float)
                or not math.isfinite(value["retry_at"]) or value["retry_at"] < 0):
            raise ProviderSlotError("provider window state is invalid")
        return _WindowState(value["limit"], value["active"], float(value["retry_at"]))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProviderSlotError("provider window state is unreadable") from exc
    finally:
        os.close(fd)


def _write_window(root: Path, route: tuple[str, str], state: _WindowState) -> None:
    import tempfile
    try:
        fd, name = tempfile.mkstemp(prefix=".window-", dir=root)
    except OSError as exc:
        raise ProviderSlotError("provider window state cannot be written") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "route": route, "limit": state.limit,
                       "active": state.active, "retry_at": state.retry_at}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, root / "window.json")
    except OSError as exc:
        raise ProviderSlotError("provider window state cannot be written") from exc
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _pause(deadline: float, poll_seconds: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderSlotTimeout("provider capacity wait timed out")
    time.sleep(min(poll_seconds, remaining))


@contextmanager
def _window_lock(runtime: Path, identity: tuple[int, int], root: Path,
                 deadline: float, poll_seconds: float) -> Iterator[None]:
    mutex = _thread_lock(identity, -1, str(root))
    while not mutex.acquire(blocking=False):
        _pause(deadline, poll_seconds)
    fd: int | None = None
    try:
        live = _safe_runtime_root(runtime)
        if (live.st_dev, live.st_ino) != identity:
            raise ProviderSlotError("provider runtime root ownership drifted")
        _safe_directory(runtime / "provider-slots")
        if root != runtime / "provider-slots":
            _safe_directory(root.parent)
        _safe_directory(root)
        fd, opened = _slot_fd(root / "window.lock")
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                _pause(deadline, poll_seconds)
        live = _safe_runtime_root(runtime)
        if (live.st_dev, live.st_ino) != identity:
            raise ProviderSlotError("provider runtime root ownership drifted")
        _same_safe_slot(root / "window.lock", fd, opened)
        yield
    finally:
        if fd is not None:
            os.close(fd)
        mutex.release()


def _live_slots(root: Path, identity: tuple[int, int], active: list[int]) -> list[int]:
    live = []
    for index in active:
        mutex = _thread_lock(identity, index, str(root))
        if not mutex.acquire(blocking=False):
            live.append(index)
            continue
        fd: int | None = None
        try:
            fd, opened = _slot_fd(root / f"slot-{index}.lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                live.append(index)
            _same_safe_slot(root / f"slot-{index}.lock", fd, opened)
        finally:
            if fd is not None:
                os.close(fd)
            mutex.release()
    return live


@contextmanager
def provider_slot(
    runtime_root: Path, *, timeout_seconds: float | None = None,
    poll_seconds: float = 0.05, provider: str = "cpa", modality: str = "text",
) -> Iterator[ProviderSlotLease]:
    """Admit one call, sharing window, live leases and cooldown across processes.

    Atomic state replacement is serialized only for admission/finish. Active
    leases can outnumber a newly halved window; no new call starts until their
    count falls below it. A crashed lease is reclaimed on the next acquisition.
    Malformed/unwritable state fails closed, never falls back to a private pool.
    """
    from src.autoslice.provider_admission import RateFeedback, observed_rate_feedback, rate_feedback

    if timeout_seconds is None:
        timeout_seconds = provider_wait_seconds()
    if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ProviderSlotError("provider slot timeout must be positive")
    if (not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool)
            or not 0 < poll_seconds <= 1):
        raise ProviderSlotError("provider slot poll interval must be between 0 and 1 second")
    runtime_root = Path(runtime_root)
    runtime_info = _safe_runtime_root(runtime_root)
    identity = (runtime_info.st_dev, runtime_info.st_ino)
    route = _route_key(provider, modality)
    root = _route_root(runtime_root, *route)
    deadline = time.monotonic() + timeout_seconds
    descriptor: int | None = None
    index = 0
    while descriptor is None:
        with _window_lock(runtime_root, identity, root, deadline, poll_seconds):
            state = _read_window(root, route)
            state.active = _live_slots(root, identity, state.active)
            if len(state.active) < state.limit and state.retry_at <= time.time():
                index = 0
                # Also respect held historical/default inodes without treating
                # their former fixed capacity as an admission constraint.
                while index in state.active:
                    index += 1
                mutex = _thread_lock(identity, index, str(root))
                if mutex.acquire(blocking=False):
                    try:
                        descriptor, opened = _slot_fd(root / f"slot-{index}.lock")
                        try:
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            os.close(descriptor)
                            descriptor = None
                        if descriptor is not None:
                            live = _safe_runtime_root(runtime_root)
                            if (live.st_dev, live.st_ino) != identity:
                                raise ProviderSlotError("provider runtime root ownership drifted")
                            _same_safe_slot(root / f"slot-{index}.lock", descriptor, opened)
                            state.active.append(index)
                            _write_window(root, route, state)
                    except BaseException:
                        if descriptor is not None:
                            os.close(descriptor)
                            descriptor = None
                        mutex.release()
                        raise
                    if descriptor is None:
                        mutex.release()
        if descriptor is None:
            _pause(deadline, poll_seconds)

    feedback = RateFeedback()
    def report(value: object) -> None:
        nonlocal feedback
        fresh = value if isinstance(value, RateFeedback) else observed_rate_feedback(value)
        if not math.isfinite(fresh.retry_after) or fresh.retry_after < 0:
            raise ProviderSlotError("provider retry-after is invalid")
        feedback = RateFeedback(feedback.rate_limited or fresh.rate_limited,
                                max(feedback.retry_after, fresh.retry_after),
                                feedback.successful and fresh.successful)

    error: BaseException | None = None
    try:
        yield ProviderSlotLease(runtime_root, index, opened.st_dev, opened.st_ino, report)
    except BaseException as exc:
        error = exc
        raise
    finally:
        report(rate_feedback(error))
        try:
            with _window_lock(runtime_root, identity, root,
                              time.monotonic() + timeout_seconds, poll_seconds):
                _same_safe_slot(root / f"slot-{index}.lock", descriptor, opened)
                state = _read_window(root, route)
                if index not in state.active:
                    raise ProviderSlotError("provider window lease ownership drifted")
                state.active.remove(index)
                if feedback.rate_limited:
                    state.limit = max(1, state.limit // 2)
                    # Even a bare 429 must pace following calls. Retry-After
                    # (when supplied) always takes precedence over this floor.
                    state.retry_at = max(state.retry_at, time.time() + max(1.0, feedback.retry_after))
                elif feedback.successful:
                    state.limit += 1
                _write_window(root, route, state)
        finally:
            os.close(descriptor)
            mutex.release()
