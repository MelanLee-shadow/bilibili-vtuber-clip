"""Bounded process-local reuse of successful receipt validation.

Keys describe the complete owner inputs. Every hashed/read artifact and its
ancestor placement are captured; reuse rechecks them through evidence_identity's
existing racy-write-safe hash cache. No failure is cached, results are copied,
and a changed/missing/replaced artifact returns to the original validator.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
import json
import stat
from threading import RLock
from typing import Generic, ParamSpec, TypeVar

_P = ParamSpec('_P')
_R = TypeVar('_R')


@dataclass(frozen=True)
class Dependency:
    path: Path
    placement: tuple[int, ...]
    digest: str | None


_CAPTURE: ContextVar[dict[Path, Dependency] | None] = ContextVar('validation_dependencies', default=None)


def _placement(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def capturing_validation() -> bool:
    return _CAPTURE.get() is not None


def _remember(capture: dict[Path, Dependency], current: Dependency) -> None:
    previous = capture.get(current.path)
    if previous is not None and previous != current:
        current = Dependency(current.path, (), current.digest)
    capture[current.path] = current


def note_hashed_file(path: Path, digest: str) -> None:
    """Called only after the original reader/hash owner accepted this file."""
    capture = _CAPTURE.get()
    if capture is None:
        return
    target = Path(path).absolute()
    try:
        for item in (target, *target.parents):
            placement = _placement(item)
            # Permitted source aliases are placement dependencies; the hash
            # owner's resolved regular target is captured independently.
            expected = digest if item == target and not stat.S_ISLNK(placement[2]) else None
            current = Dependency(item, placement, expected)
            # A mixed snapshot cannot be blessed by a later nested cache hit.
            _remember(capture, current)
    except OSError:
        # A publication race disables reuse, never replaces the original read.
        capture[target] = Dependency(target, (), digest)


def _valid(dependencies: tuple[Dependency, ...]) -> bool:
    from .evidence_identity import regular_file_sha256
    try:
        for item in dependencies:
            if _placement(item.path) != item.placement:
                return False
            if item.digest is not None and regular_file_sha256(item.path) != item.digest:
                return False
        return True
    except OSError:
        return False


@contextmanager
def capture_validation_dependencies() -> Iterator[dict[Path, Dependency]]:
    """Capture a document-local reuse entry with the original file guards."""
    dependencies: dict[Path, Dependency] = {}
    parent = _CAPTURE.get()
    token = _CAPTURE.set(dependencies)
    try:
        yield dependencies
    finally:
        _CAPTURE.reset(token)
        if parent is not None:
            for item in dependencies.values():
                _remember(parent, item)


def replay_validation_dependencies(dependencies: tuple[Dependency, ...]) -> bool:
    """Recheck and forward every dependency before reusing an evidence graph."""
    if not _valid(dependencies):
        return False
    parent = _CAPTURE.get()
    if parent is not None:
        for item in dependencies:
            _remember(parent, item)
    return True


@dataclass(frozen=True)
class _Entry(Generic[_R]):
    result: _R
    dependencies: tuple[Dependency, ...]


def _small_json_result(result: object) -> bool:
    """Stop at the cache budget without allocating the complete result JSON."""
    size = 0
    try:
        for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(result):
            size += len(chunk)
            if size > 1_000_000:
                return False
    except (TypeError, ValueError):
        return False
    return True


def memoized_validation(key: Callable[_P, str]) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Memoize successful pure owner checks; keys never replace their guards."""
    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        memo: OrderedDict[str, _Entry[_R]] = OrderedDict()
        result_keys: OrderedDict[str, None] = OrderedDict()
        lock = RLock()
        flights: dict[str, tuple[RLock, int]] = {}

        @wraps(function)
        def validate(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                identity = key(*args, **kwargs)
            except (TypeError, ValueError):
                return function(*args, **kwargs)
            with lock:
                flight, users = flights.get(identity, (RLock(), 0))
                flights[identity] = (flight, users + 1)
            try:
                with flight:
                    with lock:
                        previous = memo.get(identity)
                    if previous is not None and _valid(previous.dependencies):
                        parent = _CAPTURE.get()
                        if parent is not None:
                            for item in previous.dependencies:
                                _remember(parent, item)
                        with lock:
                            if identity in memo:
                                memo.move_to_end(identity)
                                if identity in result_keys:
                                    result_keys.move_to_end(identity)
                        return deepcopy(previous.result)
                    dependencies: dict[Path, Dependency] = {}
                    parent = _CAPTURE.get()
                    token = _CAPTURE.set(dependencies)
                    try:
                        result = function(*args, **kwargs)
                    finally:
                        _CAPTURE.reset(token)
                        if parent is not None:
                            for item in dependencies.values():
                                _remember(parent, item)
                    # A failed function never reaches this point. Recheck the captured
                    # placement after the full validation, including publication races.
                    items = tuple(dependencies.values())
                    # This is a cache budget, never an evidence/acceptance limit.
                    # Large results still run the original validator and are returned.
                    small = _small_json_result(result)
                    if small and _valid(items):
                        with lock:
                            memo[identity] = _Entry(deepcopy(result), items)
                            memo.move_to_end(identity)
                            if result is not None:
                                result_keys[identity] = None
                                result_keys.move_to_end(identity)
                            else:
                                result_keys.pop(identity, None)
                            while len(result_keys) > 32:
                                expired, _ = result_keys.popitem(last=False)
                                memo.pop(expired, None)
                    return result
            finally:
                with lock:
                    held, users = flights[identity]
                    if users == 1:
                        del flights[identity]
                    else:
                        flights[identity] = (held, users - 1)
        return validate
    return decorate
