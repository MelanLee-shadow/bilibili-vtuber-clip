"""Bounded process-local reuse of successful receipt validation.

Keys describe the complete owner inputs. Every hashed/read artifact and its
ancestor placement are captured; reuse rechecks them through evidence_identity's
existing racy-write-safe hash cache. Exceptions are never cached, results are copied,
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
import hashlib
import json
import math
import stat
import sys
from threading import RLock
from typing import TYPE_CHECKING, Generic, ParamSpec, TypeVar

if TYPE_CHECKING:
    from .finding_closure_ports import RetainedObject

_P = ParamSpec('_P')
_R = TypeVar('_R')


@dataclass(frozen=True)
class Dependency:
    path: Path
    placement: tuple[int, ...]
    digest: str | None
    directory_membership: bool = True


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
            current = _placement(item.path)
            if (current if item.directory_membership else current[:5]) != (
                    item.placement if item.directory_membership else item.placement[:5]):
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
            if not item.directory_membership:
                # Only exact-name blob decoding can ignore directory contents.
                # A caller may search this directory, so its own cache must
                # still observe the full current membership dependency.
                try:
                    current = _placement(item.path)
                except OSError:
                    return False
                if current[:5] != item.placement[:5]:
                    return False
                item = Dependency(item.path, current, item.digest)
            _remember(parent, item)
    return True


@dataclass(frozen=True)
class _Entry(Generic[_R]):
    result: _R
    dependencies: tuple[Dependency, ...]


def validation_content_key(value: object) -> str:
    """Fingerprint current arguments without unfolding a shared JSON graph.

    This is only a local memo key, never an evidence seal or owner identity.
    Object addresses deduplicate work within this one walk; every new call
    traverses current contents, including mutations of previously seen objects.
    Unsupported/cyclic values and inputs exceeding the key-work budget leave
    their caller on the uncached path. A full memo must not turn a shared
    graph into an unbounded traversal that cannot benefit from reuse.
    """
    seen: dict[int, bytes] = {}
    active: set[int] = set()
    remaining_work = 1_048_576
    remaining_bytes = 32 * 1024 * 1024

    def digest(item: object) -> bytes:
        nonlocal remaining_work, remaining_bytes
        if remaining_work <= 0:
            raise ValueError('validation key work budget exceeded')
        remaining_work -= 1
        kind = type(item)
        address = id(item)
        if address in active:
            raise ValueError('cyclic validation input')
        if address in seen:
            return seen[address]
        if kind not in (dict, list, tuple):
            if kind not in (str, bytes, int, float, bool, type(None)):
                raise TypeError('non-JSON validation input')
            if kind in (str, bytes):
                if len(item) > remaining_bytes:
                    raise ValueError('validation key content budget exceeded')
                raw = item.encode('utf-8') if kind is str else item
            elif kind is int:
                raw = str(item).encode('ascii')
            elif kind is bool:
                raw = b'true' if item else b'false'
            elif item is None:
                raw = b'null'
            else:
                raw = json.dumps(item, allow_nan=False).encode('ascii')
            remaining_bytes -= len(raw)
            if remaining_bytes < 0:
                raise ValueError('validation key content budget exceeded')
            result = hashlib.sha256(kind.__name__.encode('ascii') + b':' + raw).digest()
            seen[address] = result
            return result
        active.add(address)
        try:
            hashed = hashlib.sha256(kind.__name__.encode('ascii') + b':')
            if kind is dict:
                if any(type(key) is not str for key in item):
                    raise TypeError('non-string validation key')
                for key in sorted(item):
                    hashed.update(digest(key))
                    hashed.update(digest(item[key]))
            else:
                for child in item:
                    hashed.update(digest(child))
            result = hashed.digest()
            seen[address] = result
            return result
        finally:
            active.remove(address)

    try:
        return digest(value).hex()
    finally:
        # The recursive helper forms a closure cycle. Its untracked digest
        # bytes otherwise survive until a cyclic-GC pass, accumulating across
        # many successful or budget-rejected validation keys.
        seen.clear()
        active.clear()


def _graph_size(value: object, limit: int) -> int | None:
    """Bound retained memory by unique objects rather than expanded JSON bytes."""
    size, seen, stack = 0, set(), [value]
    while stack:
        item = stack.pop()
        address = id(item)
        if address in seen:
            continue
        seen.add(address)
        size += sys.getsizeof(item)
        if size > limit or len(seen) + len(stack) > 65536:
            return None
        if isinstance(item, dict):
            if len(seen) + len(stack) + 2 * len(item) > 65536:
                return None
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            if len(seen) + len(stack) + len(item) > 65536:
                return None
            stack.extend(item)
        elif type(item) not in (str, int, float, bool, type(None)):
            return None
    return size


class SessionValidationMemo:
    """One bounded cold-read cache using the existing dependency contract.

    Results are detached on both edges. Exceptions never enter this cache;
    callers explicitly select the completed result types they may remember.
    The budget is shared by expansion and validation within the session.
    """
    MAX_BYTES = 8 * 1024 * 1024
    MAX_ENTRIES = 32

    def __init__(self) -> None:
        self.entries: OrderedDict[object, tuple[_Entry[object], int]] = OrderedDict()
        self.size = 0
        self.lock = RLock()

    def recall(self, key: object) -> tuple[bool, object]:
        with self.lock:
            previous = self.entries.pop(key, None)
            if previous is None:
                return False, None
            entry, size = previous
            if not replay_validation_dependencies(entry.dependencies):
                self.size -= size
                return False, None
            self.entries[key] = previous
            return True, deepcopy(entry.result)

    def remember(self, key: object, result: object, dependencies: tuple[Dependency, ...], *, evict=True, named_blobs=False) -> None:
        # A scan of one-off evidence must not churn the completed validators
        # and earlier evidence it is supposed to accelerate. It may use spare
        # capacity; validation results keep the normal eviction priority.
        with self.lock:
            previous = self.entries.get(key)
            if not evict and previous is None and len(self.entries) >= self.MAX_ENTRIES:
                return
            available = self.MAX_BYTES if evict else self.MAX_BYTES - self.size + (previous[1] if previous else 0)
        # Count path and placement storage too; references never escape this
        # session and eviction cannot change the original validation outcome.
        dep_size = sum(sys.getsizeof(item) + sys.getsizeof(item.__dict__) + sys.getsizeof(item.path)
                       + 2 * sys.getsizeof(str(item.path)) + sys.getsizeof(item.placement)
                       + sum(sys.getsizeof(value) for value in item.placement)
                       + sys.getsizeof(item.digest) for item in dependencies)
        size = _graph_size(result, available - dep_size)
        if size is None or not replay_validation_dependencies(dependencies):
            return
        size += dep_size
        if named_blobs:
            # The codec follows explicit SHA/path references, never a glob or
            # absence query. New unrelated children cannot change those bytes.
            # Keep inode/device/mode/owner for directories, and every original
            # placement/content field for files and permitted symlink aliases.
            dependencies = tuple(Dependency(d.path, d.placement, d.digest, False)
                if len(d.placement) == 9 and stat.S_ISDIR(d.placement[2]) else d
                for d in dependencies)
        with self.lock:
            previous = self.entries.get(key)
            if not evict and (self.size - (previous[1] if previous else 0) + size > self.MAX_BYTES
                              or previous is None and len(self.entries) >= self.MAX_ENTRIES):
                return
            previous = self.entries.pop(key, None)
            if previous is not None:
                self.size -= previous[1]
            while self.entries and (self.size + size > self.MAX_BYTES
                                    or len(self.entries) >= self.MAX_ENTRIES):
                _, (_, expired_size) = self.entries.popitem(last=False)
                self.size -= expired_size
            self.entries[key] = (_Entry(deepcopy(result), dependencies), size)
            self.size += size


def _small_json_result(result: object) -> bool:
    """Preserve the JSON character budget without serializing a shared tree."""
    limit, memo, active = 1_000_000, {}, set()

    def atom_chars(value):
        kind = type(value)
        if kind is str:
            return len(json.encoder.encode_basestring(value))
        if value is None:
            return 4
        if kind is bool:
            return 4 if value else 5
        if kind is int:
            return len(str(value))
        if kind is float:
            return len(repr(value)) if math.isfinite(value) else 3 if math.isnan(value) else 9 if value < 0 else 8
        return None

    def length(value):
        identity, kind = id(value), type(value)
        if identity in active:
            raise ValueError('Circular reference detected')
        if identity in memo:
            return memo[identity][1]
        if kind not in (dict, list, tuple):
            size = atom_chars(value)
        else:
            active.add(identity)
            size = 2
            try:
                for index, (key, child) in enumerate(value.items() if kind is dict else enumerate(value)):
                    child_size = length(child)
                    if child_size is None:
                        return None
                    size += child_size + (2 if index else 0)
                    if kind is dict:
                        key_size = atom_chars(key)
                        if key_size is None:
                            return None
                        size += key_size + 2 + (0 if type(key) is str else 2)
                    if size > limit:
                        size = limit + 1
                        break
            finally:
                active.remove(identity)
        if len(memo) < 4096:
            memo[identity] = (value, size)
        return size

    try:
        size = length(result)
        if size is not None:
            return size <= limit
        # Subclasses and other legacy JSON cases keep the standard encoder.
        size = 0
        for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(result):
            size += len(chunk)
            if size > limit:
                return False
    except (TypeError, ValueError, RecursionError):
        return False
    finally:
        memo.clear()
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


class RetainedLedgerCache:
    # A ledger may live through a whole producer. Bound both private DAGs and
    # dependency receipts; never retain the expanded canonical text merely to
    # calculate this budget. Large/unaccountable owners take the original path.
    MAX_BYTES = 64 * 1024 * 1024
    MAX_ENTRIES = 32

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str], tuple[RetainedObject, tuple[Dependency, ...], int]] = OrderedDict()
        self._bytes = 0
        self._lock = RLock()
        self._stripes = tuple(RLock() for _ in range(32))

    def get(self, key: tuple[str, str], load: Callable[[], RetainedObject]) -> RetainedObject:
        # A stripe prevents duplicate publication for the same key, without
        # serializing independent reads or retaining an unbounded lock table.
        from .finding_closure_ports import RetainedObject
        with self._stripes[hash(key) % len(self._stripes)]:
            with self._lock:
                previous = self._entries.get(key)
            if previous is not None:
                owner, dependencies, size = previous
                if replay_validation_dependencies(dependencies):
                    with self._lock:
                        if self._entries.get(key) is previous:
                            self._entries.move_to_end(key)
                    return owner
                with self._lock:
                    if self._entries.get(key) is previous:
                        del self._entries[key]
                        self._bytes -= size
            with capture_validation_dependencies() as captured:
                owner = load()
            dependencies = tuple(captured.values())
            if type(owner) is not RetainedObject or not dependencies:
                return owner
            dep_size = sys.getsizeof(dependencies) + sum(sys.getsizeof(d) + sys.getsizeof(d.__dict__) + sys.getsizeof(d.path)
                + 2 * sys.getsizeof(str(d.path)) + sys.getsizeof(d.placement)
                + sum(sys.getsizeof(x) for x in d.placement) + sys.getsizeof(d.digest)
                for d in dependencies)
            budget = self.MAX_BYTES - dep_size
            payload = owner._json_graph if owner._json_graph is not None else owner._canonical_text
            graph_size = _graph_size(payload, budget) if budget > 0 else None
            if graph_size is None:
                return owner
            key_size = _graph_size(key, self.MAX_BYTES)
            if key_size is None:
                return owner
            size = dep_size + graph_size + key_size + sys.getsizeof(owner) + sys.getsizeof(owner.__dict__) + 128
            if size > self.MAX_BYTES or not replay_validation_dependencies(dependencies):
                return owner
            with self._lock:
                while self._entries and (len(self._entries) >= self.MAX_ENTRIES
                                          or self._bytes + size > self.MAX_BYTES):
                    _, (_, _, removed) = self._entries.popitem(last=False)
                    self._bytes -= removed
                self._entries[key] = (owner, dependencies, size)
                self._bytes += size
            return owner
