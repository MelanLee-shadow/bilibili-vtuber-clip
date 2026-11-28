"""The publication registry (which package is which published BV), the dispatch ledger, and the lock they are written under.

There is one registry for the channel, the one every other reader uses (publication_registry.load_publication_registry: the
channel's registry asset with the host's runtime overlay `state/publication_registry.runtime.v1.json` merged over it).  This
module reads that view and writes its rows into that overlay, in its row shape (publication_state_projection
.validate_runtime_registry_entry is run on every row before it is written, under the same reconciliation lock the older
upload tool takes), so a BV published or repaired by either tool is one row both see.

A row: {candidate_id (the package name), recording_date, bvid, status: "published", publication_reconciliation:
{schema_version, status, candidate_id, recording_date, bvid, aid, cid, title, authority: {path, sha256, bytes}, reconciled_at}}.
`authority` binds the receipt the lane's receipt chain starts from: the first-publication receipt, after a same-BV repair the
completion receipt of the latest one (status VERIFIED_SAME_BV / VERIFIED_SAME_BV_COVER / VERIFIED_SAME_BV_METADATA, cid the
cid online after it).  This
is the shape manual_lane/title_history and receipt_chain read.

A registry that cannot be found is not an empty one: when this machine has no runtime base at all the read raises
(FileNotFoundError), so a reader on the wrong machine cannot take "not readable here" for "nothing was published"."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.autoslice.publication_registry import load_publication_registry
from src.autoslice.publication_state_projection import (
    RECONCILIATION_SCHEMA,
    RUNTIME_REGISTRY_SCHEMA,
    _reconciliation_lock,
    validate_runtime_registry_entry,
)

from .common import GATE, LOCK, LOCK_BUSY, Refused, base, now, write_json

SCHEMA = RUNTIME_REGISTRY_SCHEMA
REGISTRY = "state/publication_registry.runtime.v1.json"
LEDGER = "state/lane_publish_ledger.jsonl"
LOCK_FILE = "state/lane_publish.lock"


def path(root: Path | None = None) -> Path:
    return (root or base()) / REGISTRY


def load(root: Path | None = None) -> dict[str, Any]:
    """The channel's registry as every reader sees it: {"schema_version", "entries": [...]}."""
    file = path(root)
    if not file.parent.parent.is_dir():
        raise FileNotFoundError(f"no runtime base here: {file.parent.parent}")
    return load_publication_registry(runtime_path=file)


def rows_for(entries: list[Mapping[str, Any]], *, bvid: str | None = None, candidate_id: str | None = None) -> list[dict]:
    return [dict(r) for r in entries if r.get("status") == "published"
            and (bvid is None or r.get("bvid") == bvid) and (candidate_id is None or r.get("candidate_id") == candidate_id)]


def _write_row(row: dict[str, Any]) -> None:
    """Put `row` into the runtime overlay (replacing the row of its candidate and date), validated as every reader validates
    it; under the reconciliation lock the older upload tool also takes."""
    try:
        validate_runtime_registry_entry(row)
    except ValueError as exc:
        raise Refused(GATE, f"出版登记行不合格，没有写入：{exc}") from exc
    file = path()
    with _reconciliation_lock(base()):
        doc = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {"schema_version": SCHEMA, "entries": []}
        if doc.get("schema_version") != SCHEMA or not isinstance(doc.get("entries"), list):
            raise Refused(GATE, f"{file}: 不是 {SCHEMA} 登记，不写")
        key = (row["candidate_id"], row["recording_date"])
        doc["entries"] = [r for r in doc["entries"] if (r.get("candidate_id"), r.get("recording_date")) != key] + [row]
        write_json(file, doc)


def _projection(*, status: str, candidate_id: str, recording_date: str, bvid: str, aid: int, cid: int, title: str,
                authority: dict[str, Any]) -> dict[str, Any]:
    return {"candidate_id": candidate_id, "recording_date": recording_date, "bvid": bvid, "status": "published",
            "publication_reconciliation": {"schema_version": RECONCILIATION_SCHEMA, "status": status,
                                           "candidate_id": candidate_id, "recording_date": recording_date, "bvid": bvid,
                                           "aid": int(aid), "cid": int(cid), "title": title, "authority": authority,
                                           "reconciled_at": now()}}


def add_published(*, candidate_id: str, recording_date: str, bvid: str, aid: int, cid: int, title: str,
                  authority: dict[str, Any]) -> None:
    """A new published row; refused when the package or the BV already has one (under the lock)."""
    entries = load()["entries"]
    if rows_for(entries, candidate_id=candidate_id) or rows_for(entries, bvid=bvid):
        raise Refused(GATE, f"出版登记里已经有 {candidate_id} 或 {bvid} 的已发布行：不重复登记")
    _write_row(_projection(status="VERIFIED_PUBLIC", candidate_id=candidate_id, recording_date=recording_date, bvid=bvid,
                           aid=aid, cid=cid, title=title, authority=authority))


def rebind(bvid: str, *, status: str, receipt: dict[str, Any], aid: int, cid: int, title: str) -> None:
    """Move the published row of `bvid` (whichever tool published it) onto the completion receipt of a repair (under the lock);
    `cid` is the cid online after the repair."""
    rows = rows_for(load()["entries"], bvid=bvid)
    if len(rows) != 1:
        raise Refused(GATE, f"出版登记里 {bvid} 的已发布行有 {len(rows)} 条，应为 1 条：不改登记")
    _write_row(_projection(status=status, candidate_id=str(rows[0]["candidate_id"]),
                           recording_date=str(rows[0]["recording_date"]), bvid=bvid, aid=aid, cid=cid, title=title,
                           authority=receipt))


def ledger_rows() -> list[dict[str, Any]]:
    file = base() / LEDGER
    if not file.is_file():
        return []
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]


def ledger_add(event: str, **fields: Any) -> None:
    file = base() / LEDGER
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now(), "event": event, **fields}, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def locked() -> Iterator[None]:
    """One upload or repair at a time on this host; another one waiting is refused at once (step 10 waits and asks again)."""
    file = base() / LOCK_FILE
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused(LOCK, f"REFUSE: {LOCK_BUSY}: {file}; retry after the active transaction finishes") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def first_upload_locked() -> Iterator[None]:
    """Take the lane lock and the legacy uploader's shared lock during a first-publication decision."""
    with locked():
        file = base() / "upload.lock"
        with file.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Refused(LOCK, f"REFUSE: {LOCK_BUSY}: {file}; retry after the active transaction finishes") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
