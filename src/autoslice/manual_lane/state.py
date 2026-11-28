"""Per-clip state JSON for the manual-lane pipeline: one file per clip.

Holds the clip's selection and adopted cut, the session, 维护者's listening labels, and one record per step
(status, products, CPA calls, short note).  No hashes, versions or stale marks: resume looks only at products.
Writes are atomic (temp file + os.replace).  Steps of one clip may finish at the same time (runner.py): all of them
write the one in-memory ClipState of the clip, and `lock` makes each read-modify-write and each save one piece, so no
step's record is lost.  One process runs a clip at a time (`clip_run_lock`).
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue
    from .adapters import CpaAttempt
    from .config import SessionBlock
    from .dissent_register import DissentItem
    from .cover_import import CoverRecord
    from .stage_package import ReviewRecord
    from .step3_evidence import IvanSection
    from .step_import import ImportRecord

SCHEMA = "manual-lane-clip.v2"
DEFAULT_BCUT_SOURCE_ZERO_MS = 0
CLIP_RUN_LOCK_BUSY = "CLIP_RUN_LOCK_BUSY"
CLIP_RUN_LOCK_STALE = "CLIP_RUN_LOCK_STALE"


class ClipProposal(TypedDict):
    start_cue: int
    end_cue: int


class ReselectInfo(TypedDict):
    authority: str
    quote: str
    original: str
    adopted_cut: list[int]


class SupersedeInfo(TypedDict):
    by: str
    authority: str
    quote: str
    reason: str
    at: str


class ClipInfo(TypedDict, total=False):
    cid: str
    source_name: str
    source_dir: str  # where the recording source_name is on the media host (found or given once; steps read it from here)
    recording_date: str
    remote_dir: str
    mac_bcut_srt: str
    bcut_source_zero_ms: int  # source-recording time corresponding to zero in the original BCUT file
    hook: str
    selection_reason: str    # the selection's detailed reason for the candidate (clip_init.entry_reason)
    peaks: list[dict[str, object]]   # the moments the selection says most deserve to be seen (clip_init.Peak); absent when it named none
    start_cue: int
    end_cue: int
    start_ms: int
    end_ms: int
    proposal: ClipProposal
    selection_file: str
    selection_sha256: str
    reselected_from: str
    authority: str
    reselect: ReselectInfo
    status: str
    superseded_by: str
    superseded: SupersedeInfo


class CpaEvent(TypedDict, total=False):
    call_id: str
    model: str | None
    effort: str
    max_output_tokens: int
    kind: str
    effort_note: str
    rc: int | None
    elapsed_s: float | None
    stream: bool
    complete: bool
    reason: str | None
    attempts: int | None
    budget_attempts: int | None
    http_log: list[CpaAttempt]
    usage: JsonValue
    prompt_sha256: str
    response_sha256: str | None
    response_text: str
    stderr_tail: str
    file: str
    cache_path: str
    cached: bool
    purpose: str
    rejected_answers: list[RejectedAnswer]
    requests: int
    tolerated: bool          # the step went on without this call's answer (step_io.ask_json goes_on=True)
    attempted_models: list[str] | None
    reported_model: str | None
    status: str | None
    fallback_from: str
    fallback_reason: str
    fallback_note: str


class RejectedAnswer(TypedDict):
    reason: str
    chars: int
    attempt: NotRequired[int]
    usage: NotRequired[JsonValue]


class StoredStep(TypedDict, total=False):
    status: str
    reason: str
    products: dict[str, str]
    cpa: list[CpaEvent]
    note: str
    at: str


class RewindState(TypedDict):
    by: str
    steps: list[str]


class UploadState(TypedDict):
    enabled: bool
    authorized_quote: str | None


class ReviewedTags(TypedDict):
    kept: list[str]
    removed: list[str]
    proper_noun_tags: NotRequired[list[str]]   # the kept tags that are proper nouns: evicted last on a re-run merge


class ClipData(TypedDict, total=False):
    schema: str
    clip: ClipInfo
    session: SessionBlock
    released: list[dict[str, str]]
    reviewer_labels: IvanSection
    steps: dict[str, StoredStep]
    upload: UploadState
    created_at: str
    updated_at: str
    rewind: RewindState
    imported: ImportRecord
    imported_cover: CoverRecord
    withdrawn_covers: list[JsonValue]      # imported covers taken back (cover_import.withdraw), oldest first
    revised_from: JsonValue
    review: ReviewRecord
    dissent: list[DissentItem]
    reviewed_tags: ReviewedTags
    revisions: JsonValue
    manual_import: JsonValue


class StateError(RuntimeError):
    """Raised when a state file is malformed or cannot be read."""


class ClipRunLockError(StateError):
    """Raised when a clip's run lock cannot be acquired; `code` separates live runs from stale locks."""

    def __init__(self, code: str, message: str, *, lock_path: Path, owner_pid: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.lock_path = lock_path
        self.owner_pid = owner_pid


class ClipRunBusyError(ClipRunLockError):
    """Raised when another live run holds the clip's run lock (`CLIP_RUN_LOCK_BUSY`)."""

    def __init__(self, state_path: Path, lock_path: Path, *, owner_pid: int | None = None) -> None:
        who = f", pid={owner_pid}" if owner_pid is not None else ""
        super().__init__(
            CLIP_RUN_LOCK_BUSY,
            f"{state_path} is already being run by another process ({lock_path}{who})",
            lock_path=lock_path,
            owner_pid=owner_pid,
        )


class ClipRunStaleLockError(ClipRunLockError):
    """Raised when a clip's run lock is still held via flock while its recorded owner is dead (`CLIP_RUN_LOCK_STALE`)."""

    def __init__(self, state_path: Path, lock_path: Path, *, owner_pid: int | None = None, detail: str = "") -> None:
        who = f"pid={owner_pid}" if owner_pid is not None else "unknown owner"
        extra = f"; {detail}" if detail else ""
        super().__init__(
            CLIP_RUN_LOCK_STALE,
            f"{state_path} has a stale run lock ({lock_path}, {who}{extra})",
            lock_path=lock_path,
            owner_pid=owner_pid,
        )


@dataclass(frozen=True)
class ClipRunTakeover:
    """Previous owner metadata overwritten when acquiring an unheld lock left by a terminated run."""

    lock_path: Path
    previous_pid: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class ClipRunLockStatus:
    """Observed state of a clip's run lock: FREE (including unheld leftover), BUSY (live owner), or STALE (held with dead owner)."""

    state: Literal["FREE", "BUSY", "STALE"]
    code: str | None
    lock_path: Path
    owner_pid: int | None = None
    detail: str = ""
    leftover: bool = False


def clip_run_lock_path(state_path: Path) -> Path:
    return Path(state_path).with_name(Path(state_path).name + ".run.lock")


def _proc_is_zombie(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    _, sep, rest = stat.rpartition(")")
    if not sep:
        return False
    parts = rest.split()
    return bool(parts) and parts[0] == "Z"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return not _proc_is_zombie(pid)


def _read_fd_bytes(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_lock_owner_pid(raw: bytes) -> tuple[int | None, bool]:
    if not raw.strip():
        return None, False
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, True
    if isinstance(doc, dict) and type(doc.get("pid")) is int:
        return cast(int, doc["pid"]), True
    return None, True


def _write_lock_owner(fd: int, state_path: Path) -> None:
    payload = (json.dumps({"pid": os.getpid(), "started_at": utc_now(), "state_path": str(state_path)},
                          ensure_ascii=False) + "\n").encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _classify_held_lock(fd: int, lock_path: Path) -> ClipRunLockStatus:
    pid, _ = _parse_lock_owner_pid(_read_fd_bytes(fd))
    if pid is not None and not _pid_alive(pid):
        return ClipRunLockStatus(
            state="STALE",
            code=CLIP_RUN_LOCK_STALE,
            lock_path=lock_path,
            owner_pid=pid,
            detail="flock still held while recorded owner is dead",
        )
    return ClipRunLockStatus(state="BUSY", code=CLIP_RUN_LOCK_BUSY, lock_path=lock_path, owner_pid=pid)


def _classify_unheld_lock(fd: int, lock_path: Path) -> ClipRunLockStatus:
    pid, has_metadata = _parse_lock_owner_pid(_read_fd_bytes(fd))
    if not has_metadata:
        return ClipRunLockStatus(state="FREE", code=None, lock_path=lock_path)
    if pid is None:
        detail = "unreadable leftover lock metadata with no active flock"
    elif not _pid_alive(pid):
        detail = "previous owner process is dead and did not release lock"
    else:
        detail = "previous run did not release lock and no active flock is held"
    return ClipRunLockStatus(state="FREE", code=None, lock_path=lock_path, owner_pid=pid, detail=detail, leftover=True)


def inspect_clip_run_lock(state_path: Path) -> ClipRunLockStatus:
    """Inspect a clip's run lock without creating or modifying it."""
    lock_path = clip_run_lock_path(state_path)
    if not lock_path.exists():
        return ClipRunLockStatus(state="FREE", code=None, lock_path=lock_path)
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
        return ClipRunLockStatus(state="FREE", code=None, lock_path=lock_path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _classify_held_lock(fd, lock_path)
        return _classify_unheld_lock(fd, lock_path)
    finally:
        os.close(fd)


def bcut_source_zero_ms(clip: ClipInfo) -> int:
    """Read the explicit BCUT binding; older states use the source clock directly."""
    value = clip.get("bcut_source_zero_ms", DEFAULT_BCUT_SOURCE_ZERO_MS)
    if type(value) is not int:
        raise StateError("clip.bcut_source_zero_ms must be an integer")
    return value


@contextmanager
def clip_run_lock(state_path: Path) -> Iterator[ClipRunTakeover | None]:
    """Hold the clip's run lock (a file next to its state file) for a run.

    Raises `ClipRunBusyError` (`CLIP_RUN_LOCK_BUSY`) when a live run holds it, and
    `ClipRunStaleLockError` (`CLIP_RUN_LOCK_STALE`) when `flock` is still held while the recorded owner is dead.
    When `flock` is acquired on a file with leftover metadata from a terminated run, takes over cleanly,
    logs one stderr line naming the previous owner pid, overwrites the metadata, and yields `ClipRunTakeover`.
    """
    state_file = Path(state_path)
    if not state_file.exists():
        raise StateError(f"cannot read state {state_path}: no such file")
    lock_path = clip_run_lock_path(state_file)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            status = _classify_held_lock(fd, lock_path)
            if status.state == "STALE":
                raise ClipRunStaleLockError(state_file, lock_path, owner_pid=status.owner_pid, detail=status.detail) from exc
            raise ClipRunBusyError(state_file, lock_path, owner_pid=status.owner_pid) from exc
        status = _classify_unheld_lock(fd, lock_path)
        takeover: ClipRunTakeover | None = None
        if status.leftover:
            takeover = ClipRunTakeover(lock_path=lock_path, previous_pid=status.owner_pid, detail=status.detail)
            who = f"pid={takeover.previous_pid}" if takeover.previous_pid is not None else "pid=unknown"
            print(f"[clip_run_lock] took over unheld lock {lock_path} (previous owner {who}: {takeover.detail})",
                  file=sys.stderr, flush=True)
        _write_lock_owner(fd, state_file)
        acquired = True
        yield takeover
    finally:
        try:
            if acquired:
                os.ftruncate(fd, 0)
        finally:
            os.close(fd)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClipState:
    """In-memory view of one clip's state file."""

    def __init__(self, path: Path, data: ClipData):
        self.path = Path(path)
        self.data = data
        self.lock = threading.RLock()

    @classmethod
    def create(cls, path: Path, *, clip: ClipInfo, session: SessionBlock) -> ClipState:
        if Path(path).exists():
            raise StateError(f"state already exists, refusing to overwrite: {path}")
        data: ClipData = {
            "schema": SCHEMA,
            "clip": cast(ClipInfo, dict(clip)),
            "session": cast("SessionBlock", dict(session)),
            "released": [],
            "reviewer_labels": {},
            "steps": {},
            "upload": {"enabled": False, "authorized_quote": None},
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        state = cls(Path(path), data)
        state.save()
        return state

    @classmethod
    def load(cls, path: Path) -> ClipState:
        try:
            data = cast(ClipData, json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"cannot read state {path}: {exc}") from exc
        if data.get("schema") != SCHEMA:
            raise StateError(f"unexpected schema in {path}: {data.get('schema')!r}")
        return cls(Path(path), data)

    def save(self) -> None:
        with self.lock:
            self.data["updated_at"] = utc_now()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)

    def put(self, key: str, value: object) -> None:
        """Set one top-level field and save, as one piece under the lock (the way a step writes anything but its own record)."""
        with self.lock:
            cast("dict[str, object]", self.data)[key] = value
            self.save()

    def step(self, key: str) -> StoredStep:
        return self.data["steps"].get(key, {})

    def record(self, key: str, *, ok: bool, reason: str, products: Mapping[str, str],
               cpa: Sequence[CpaEvent], note: str) -> None:
        with self.lock:      # the step writes only its own entry; the lock keeps concurrent entries and saves whole
            self.data["steps"][key] = {
                "status": "DONE" if ok else "STOPPED",
                "reason": reason,
                "products": dict(products),
                "cpa": [cast(CpaEvent, dict(c)) for c in cpa],
                "note": note,
                "at": utc_now(),
            }
            self.save()

    def released_reasons(self) -> set[str]:
        return {item["reason"] for item in self.data.get("released", []) if item.get("reason")}

    def cpa_call_count(self) -> int:
        return sum(len(rec.get("cpa") or []) for rec in self.data["steps"].values())
