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
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

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
    attempted_models: list[str] | None
    reported_model: str | None
    status: str | None


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


def bcut_source_zero_ms(clip: ClipInfo) -> int:
    """Read the explicit BCUT binding; older states use the source clock directly."""
    value = clip.get("bcut_source_zero_ms", DEFAULT_BCUT_SOURCE_ZERO_MS)
    if type(value) is not int:
        raise StateError("clip.bcut_source_zero_ms must be an integer")
    return value


@contextmanager
def clip_run_lock(state_path: Path) -> Iterator[None]:
    """Hold the clip's run lock (a file next to its state file) for a run; a second process asking for it stops with an error."""
    if not Path(state_path).exists():
        raise StateError(f"cannot read state {state_path}: no such file")
    lock_path = Path(state_path).with_name(Path(state_path).name + ".run.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError(f"{state_path} is already being run by another process ({lock_path})") from exc
        yield
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
