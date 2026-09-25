"""Hash-chained journal for same-BV title-and-cover revisions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.same_bv_title_cover_plan import (
    TitleCoverRepairError,
    sha256_file,
)


JOURNAL_SCHEMA = "same-bv-title-cover-repair-journal.v1"
STATES = {
    "PLANNED",
    "COVER_UPLOAD_INTENT",
    "COVER_UPLOADED",
    "EDIT_INTENT",
    "EDIT_AMBIGUOUS",
    "PUBLIC_PENDING",
    "SECTION_SYNC_INTENT",
    "SECTION_SYNC_AMBIGUOUS",
    "VERIFIED",
    "BLOCKED_DRIFT",
}
TERMINAL_STATES = {"VERIFIED", "BLOCKED_DRIFT"}
TRANSITIONS = {
    "PLANNED": {"COVER_UPLOAD_INTENT", "BLOCKED_DRIFT"},
    "COVER_UPLOAD_INTENT": {"COVER_UPLOADED", "BLOCKED_DRIFT"},
    "COVER_UPLOADED": {"EDIT_INTENT", "BLOCKED_DRIFT"},
    "EDIT_INTENT": {"EDIT_AMBIGUOUS", "PUBLIC_PENDING", "SECTION_SYNC_INTENT", "VERIFIED", "BLOCKED_DRIFT"},
    "EDIT_AMBIGUOUS": {"PUBLIC_PENDING", "SECTION_SYNC_INTENT", "VERIFIED", "BLOCKED_DRIFT"},
    "PUBLIC_PENDING": {"SECTION_SYNC_INTENT", "VERIFIED", "BLOCKED_DRIFT"},
    "SECTION_SYNC_INTENT": {"SECTION_SYNC_AMBIGUOUS", "PUBLIC_PENDING", "VERIFIED", "BLOCKED_DRIFT"},
    "SECTION_SYNC_AMBIGUOUS": {"PUBLIC_PENDING", "VERIFIED", "BLOCKED_DRIFT"},
    "VERIFIED": set(),
    "BLOCKED_DRIFT": set(),
}


class TitleCoverJournalCorrupt(TitleCoverRepairError):
    pass


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise TitleCoverJournalCorrupt("journal has a partial final row")
    rows: list[dict[str, Any]] = []
    previous_hash: str | None = None
    last_state: dict[str, str] = {}
    active_bvid: dict[str, str] = {}
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TitleCoverJournalCorrupt(f"journal row {line_no} is invalid JSON") from exc
        claimed = row.get("row_sha256") if isinstance(row, dict) else None
        unhashed = dict(row) if isinstance(row, dict) else {}
        unhashed.pop("row_sha256", None)
        if (
            not isinstance(row, dict)
            or row.get("schema_version") != JOURNAL_SCHEMA
            or claimed != _sha256_json(unhashed)
            or row.get("seq") != line_no
            or row.get("prev_row_sha256") != previous_hash
        ):
            raise TitleCoverJournalCorrupt(f"journal row {line_no} hash/chain mismatch")
        plan_id = str(row.get("plan_id") or "")
        state = str(row.get("state") or "")
        bvid = str(row.get("bvid") or "")
        if not plan_id or state not in STATES or not bvid.startswith("BV"):
            raise TitleCoverJournalCorrupt(f"journal row {line_no} identity/state invalid")
        previous_state = last_state.get(plan_id)
        if previous_state is None:
            if state != "PLANNED":
                raise TitleCoverJournalCorrupt("journal plan does not start at PLANNED")
            prior = active_bvid.get(bvid)
            if prior and last_state.get(prior) not in TERMINAL_STATES:
                raise TitleCoverJournalCorrupt(f"BVID {bvid} already has an active title-cover plan")
            active_bvid[bvid] = plan_id
        elif state not in TRANSITIONS[previous_state]:
            raise TitleCoverJournalCorrupt(f"illegal title-cover transition {previous_state}->{state}")
        last_state[plan_id] = state
        previous_hash = claimed
        rows.append(row)
    return rows


def plan_rows(journal: Path, plan_path: Path, plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    expected_path = str(plan_path.resolve())
    expected_sha = sha256_file(plan_path.resolve())
    rows = []
    for row in read_journal(journal):
        if row.get("plan_id") != plan.get("plan_id"):
            continue
        if (
            row.get("bvid") != plan.get("bvid")
            or row.get("plan_path") != expected_path
            or row.get("plan_sha256") != expected_sha
        ):
            raise TitleCoverJournalCorrupt("journal plan binding drift")
        rows.append(row)
    return rows


def append_journal(
    journal: Path,
    *,
    plan_path: Path,
    plan: Mapping[str, Any],
    state: str,
    details: Mapping[str, Any],
    now: str,
) -> dict[str, Any]:
    journal = journal.resolve()
    journal.parent.mkdir(parents=True, exist_ok=True)
    rows = read_journal(journal)
    own = plan_rows(journal, plan_path, plan) if rows else []
    if own:
        previous = str(own[-1]["state"])
        if state not in TRANSITIONS[previous]:
            raise TitleCoverJournalCorrupt(f"illegal title-cover transition {previous}->{state}")
    elif state != "PLANNED":
        raise TitleCoverJournalCorrupt("journal must start at PLANNED")
    else:
        # Reject before appending: read_journal rejecting the next read would
        # already leave the original owner's transaction unrecoverable.
        previous_for_bvid = next(
            (row for row in reversed(rows) if row.get("bvid") == plan["bvid"]),
            None,
        )
        if previous_for_bvid and previous_for_bvid["state"] not in TERMINAL_STATES:
            raise TitleCoverJournalCorrupt(
                f"BVID {plan['bvid']} already has an active title-cover plan"
            )
    row = {
        "schema_version": JOURNAL_SCHEMA,
        "seq": len(rows) + 1,
        "at": now,
        "prev_row_sha256": rows[-1]["row_sha256"] if rows else None,
        "plan_id": plan["plan_id"],
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": sha256_file(plan_path.resolve()),
        "bvid": plan["bvid"],
        "state": state,
        "details": dict(details),
    }
    row["row_sha256"] = _sha256_json(row)
    data = _canonical_json(row) + b"\n"
    fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short journal write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return row


def initialise_journal(
    journal: Path,
    plan_path: Path,
    plan: Mapping[str, Any],
    *,
    now: str,
) -> None:
    if plan_rows(journal, plan_path, plan):
        raise TitleCoverJournalCorrupt("plan is already journaled")
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="PLANNED",
        details={"remote_mutation": False},
        now=now,
    )


def uploaded_cover_url(rows: list[Mapping[str, Any]]) -> str | None:
    for row in reversed(rows):
        value = (row.get("details") or {}).get("uploaded_cover_url")
        if isinstance(value, str) and value:
            return value
    return None
