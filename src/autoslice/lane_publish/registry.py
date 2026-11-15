"""The publication registry (which package is which published BV), the dispatch ledger, and the lock they are written under.

A registry row: {candidate_id (the package name), recording_date, bvid, aid, title, status: "published",
publication_reconciliation: {status, authority: {path, sha256, bytes}}}.  `authority` binds the receipt the lane's receipt
chain starts from: the first-publication receipt, after a same-BV repair the completion receipt of the latest one
(status VERIFIED_SAME_BV / VERIFIED_SAME_BV_COVER).  This is the shape manual_lane/title_history and receipt_chain read.

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

from .common import GATE, LOCK, LOCK_BUSY, Refused, base, now, write_json

SCHEMA = "lane-publication-registry.v1"
REGISTRY = "state/lane_publication_registry.json"
LEDGER = "state/lane_publish_ledger.jsonl"
LOCK_FILE = "state/lane_publish.lock"


def path(root: Path | None = None) -> Path:
    return (root or base()) / REGISTRY


def load(root: Path | None = None) -> dict[str, Any]:
    """{"schema_version", "entries": [...]}; empty on a runtime base that holds no registry yet."""
    file = path(root)
    if not file.is_file():
        if not file.parent.parent.is_dir():
            raise FileNotFoundError(f"no runtime base here: {file.parent.parent}")
        return {"schema_version": SCHEMA, "entries": []}
    doc = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA or not isinstance(doc.get("entries"), list):
        raise ValueError(f"{file}: not a {SCHEMA} registry")
    return doc


def rows_for(entries: list[Mapping[str, Any]], *, bvid: str | None = None, candidate_id: str | None = None) -> list[dict]:
    return [dict(r) for r in entries if r.get("status") == "published"
            and (bvid is None or r.get("bvid") == bvid) and (candidate_id is None or r.get("candidate_id") == candidate_id)]


def add_published(row: dict[str, Any]) -> None:
    """A new published row; refused when the package or the BV already has one (under the lock)."""
    doc = load()
    if rows_for(doc["entries"], candidate_id=row["candidate_id"]) or rows_for(doc["entries"], bvid=row["bvid"]):
        raise Refused(GATE, f"出版登记里已经有 {row['candidate_id']} 或 {row['bvid']} 的已发布行：不重复登记")
    doc["entries"].append({**row, "registered_at": now()})
    write_json(path(), doc)


def rebind(bvid: str, *, status: str, receipt: dict[str, Any]) -> None:
    """Move the published row of `bvid` onto the completion receipt of a repair (under the lock)."""
    doc = load()
    rows = [r for r in doc["entries"] if r.get("status") == "published" and r.get("bvid") == bvid]
    if len(rows) != 1:
        raise Refused(GATE, f"出版登记里 {bvid} 的已发布行有 {len(rows)} 条，应为 1 条：不改登记")
    rows[0]["publication_reconciliation"] = {"status": status, "authority": receipt, "reconciled_at": now()}
    write_json(path(), doc)


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
