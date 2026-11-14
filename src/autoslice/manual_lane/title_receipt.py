"""The title-edit receipt of a published manuscript: part of the manuscript's history, kept under its name for every successor.

`authorized_upload.py title-repair` edits the title of a published BV in place and writes a `VERIFIED_TITLE_ONLY` receipt; it
does not touch the registry row, which keeps the first publication's title and receipt.  A successor that only replaces the
cover (revision_authority) must carry that receipt in its package record (`verified_title_repair`, the finished-media lane's
`--title-receipt`), or the reconciliation after the cover repair cannot bridge the first title to the title online and the
repair, done online, stays unreconciled locally.  So the receipt is registered once, here, under the manuscript:

    task/title_receipts/<manuscript>.json                 the registration (below)
    task/title_receipts/<manuscript>.title-receipt.json   a copy of the receipt's bytes (the original may be root-only)

`register` replays the receipt in full against the first publication inside the root boundary (title_history.serve, the same
validator the upload tool and the reconciliation run) and records that fact with the time: step 8 hands the receipt to the
finished-media lane, which may then check it only as to its shape (`--authority-shape-only`) because it cannot read those files.
A later title edit supersedes the registration: register its receipt (it replays the earlier one)."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from . import publication_id, title_history
from .reselect import SUPERSEDED
from .runner import Ctx
from .state import ClipState, StateError, utc_now

if TYPE_CHECKING:      # trim_run imports the steps, which import this module through revision_authority
    from .trim_run import AdaptersFor

DIR = "title_receipts"
SCHEMA = "manuscript-title-receipt.v1"


def registration_path(task: Path, manuscript: str) -> Path:
    return Path(task) / DIR / f"{manuscript}.json"


def registered(task: Path, manuscript: str) -> dict | None:
    """The registration of `manuscript` with its receipt copy checked against the sha256 it recorded; None when none is registered.
    StateError when the registration or the copy is damaged: a receipt that cannot be trusted is not silently dropped."""
    path = registration_path(task, manuscript)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        receipt = Path(doc["receipt"]["path"])
        ok = doc["schema_version"] == SCHEMA and doc["manuscript"] == manuscript and receipt.is_file() \
            and hashlib.sha256(receipt.read_bytes()).hexdigest() == doc["receipt"]["sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateError(f"{path}: the title receipt registration is unreadable ({type(exc).__name__}: {exc})") from exc
    if not ok:
        raise StateError(f"{path}: the registered title receipt is missing or differs from the sha256 recorded; register it again")
    return doc


def latest(task: Path, cid: str) -> ClipState:
    """The state of the clip that stands for the manuscript of `cid` now: `cid` itself, or the end of its superseded_by chain."""
    seen: set[str] = set()
    while cid not in seen:
        seen.add(cid)
        state = ClipState.load(Path(task) / "state" / f"{cid}.json")
        clip = state.data["clip"]
        if clip.get("status") != SUPERSEDED or not clip.get("superseded_by"):
            return state
        cid = str(clip["superseded_by"])
    raise StateError(f"{cid}: the superseded_by chain loops")


def register(*, task: Path, cid: str, receipt: Path, adapters_for: AdaptersFor) -> dict:
    """Register `receipt` as the title edit of the manuscript of `cid` (the manuscript's clip or any successor of it).  StateError
    with the reason when the manuscript is not published, the clip has no title yet, or the receipt is not the verified title-only
    edit of this manuscript that gave its title now."""
    from .stage_package import member_slug

    task = Path(task)
    manuscript = publication_id.publication_cid(cid)
    state = ClipState.load(task / "state" / f"{cid}.json")
    clip = state.data["clip"]
    ctx = Ctx(state=state, session=state.data["session"], clip_dir=task / "clips" / cid, remote_dir=clip["remote_dir"],
              adapters=adapters_for(clip["remote_dir"]))
    if not title_history.published_status(ctx, manuscript):
        raise StateError(f"{cid}: the registry holds no published row for {manuscript}; there is no title edit to register")
    current = latest(task, cid)
    title_file = task / "clips" / str(current.data["clip"]["cid"]) / "title/title.txt"
    if not title_file.is_file():
        raise StateError(f"{title_file} is missing: the title of {manuscript} now is not known (register after the title is decided)")
    title = title_file.read_text(encoding="utf-8").strip()
    date = state.data["session"]["recording_date"]
    package = f"fm-{date.replace('-', '')}-{member_slug(cid)}"
    answer, via_boundary = title_history.verify_title_receipt(
        ctx, manuscript, package=package, title=title, path=str(Path(receipt).absolute()))
    if "refused" in answer:
        raise StateError(f"{receipt}: not registered for {manuscript}: {answer['refused']}")
    content = base64.b64decode(answer["content_b64"])
    reference = answer["receipt"]
    if hashlib.sha256(content).hexdigest() != reference["sha256"]:
        raise StateError(f"{receipt}: the bytes the boundary returned differ from the sha256 it named")
    out = registration_path(task, manuscript)
    copy = out.with_name(f"{manuscript}.title-receipt.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(content)
    doc = {"schema_version": SCHEMA, "manuscript": manuscript, "bvid": answer["bvid"], "package": package,
           "final_title": answer["final_title"], "receipt": {"path": str(copy), "sha256": reference["sha256"]},
           "source": reference, "full_replay": {"via": "BOUNDARY" if via_boundary else "DIRECT", "at": utc_now()}}
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc
