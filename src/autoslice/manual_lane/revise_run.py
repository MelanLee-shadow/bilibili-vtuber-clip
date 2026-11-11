"""The `revise` command: a published clip is redone (new subtitles, a new cover, a cut picture) in a successor clip.

A clip the registry holds as published is not edited: its package is bound by the first publication's evidence.  `revise`
builds the successor `<X>_r<N>` (N from 2, the smallest not yet used; a revision of a revision `<X>_r2` becomes `<X>_r3`,
beside it) of the same manuscript (publication_id), so that step 8 of the successor builds the same-BV repair authority
(revision_authority) and the successor's package lands in its own directory, with X's tree not touched.

The successor starts where `trim`'s successor starts, at step 3, with what X had decided:
- the state: X's, key by key (the clip block with the cut, the session, the labels and whatever else it holds), without
  the status X got as a candidate, its upload, review and later steps; steps 1, 0 and 2 stay done (boundary, dedupe and
  cut records, their local products, and on the media host X's cut directory copied under the successor's own work
  directory);
- the subtitles and speakers X has now (after its review), imported verbatim as `trim`'s successor imports its own
  (step_import.record_import, `decided_by` as X's); a later `import-subtitles` replaces them;
- an imported cover (cover_import) follows the manuscript: the record and its files are copied;
- the dictation and the compilation record, which later steps and the package guards read.
Last X is marked SUPERSEDED by the successor (`reselect.supersede_candidate`, as `trim` does).  Nothing is asked of CPA.
Any stop before that leaves no successor behind (what this run wrote is removed; X is untouched)."""

from __future__ import annotations

import copy
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, cast

from . import compilation, cover_import, publication_id, title_history, writing_canon
from .reselect import SUPERSEDED, supersede_candidate
from .runner import Ctx
from .state import ClipState, StateError, utc_now
from .step_import import record_import
from .trim_run import MAX_CID, AdaptersFor

if TYPE_CHECKING:
    from .state import ClipInfo

FROM_STEP_3 = ("boundary", "dedupe", "cut")       # the steps whose records and products the successor takes over
COPIED = ("boundary", "dedup", "cut", "asr", compilation.WORK_DIR, compilation.MANIFEST_FILE)      # X's local files it needs
STATUS_KEYS = ("status", "superseded_by", "superseded")      # what X is as a candidate, not as a cut
NOT_CARRIED = ("review", "rewind", "revisions", "manual_import", "reviewed_tags", "imported", "upload", "steps")
COPY_CUT_TIMEOUT_S = 1800


def successor_id(task: Path, cid: str) -> str:
    """`<base>_r<N>`, N the smallest number from 2 whose state file and clip directory do not exist."""
    base = publication_id.revision_base(cid)
    n = 2
    while (task / "state" / f"{base}{publication_id.REVISE_SUFFIX}{n}.json").exists() \
            or (task / "clips" / f"{base}{publication_id.REVISE_SUFFIX}{n}").exists():
        n += 1
    return f"{base}{publication_id.REVISE_SUFFIX}{n}"


def _check_source(task: Path, cid: str, state: ClipState, adapters_for: AdaptersFor) -> None:
    clip = state.data["clip"]
    if clip.get("status") == SUPERSEDED:
        raise StateError(f"{cid} is superseded by {clip.get('superseded_by')}; revise that one")
    manuscript = publication_id.publication_cid(cid)
    clip_dir = task / "clips" / cid
    ctx = Ctx(state=state, session=state.data["session"], clip_dir=clip_dir, remote_dir=clip["remote_dir"],
              adapters=adapters_for(clip["remote_dir"]))
    try:
        published = title_history.published_status(ctx, manuscript)      # the registry is root-only on the media host
    except (OSError, ValueError, RuntimeError) as exc:
        raise StateError(f"{cid}: the publication registry cannot be read, so whether {manuscript} is published is not known: "
                         f"{type(exc).__name__}: {exc!s:.300}") from exc
    if not published:
        raise StateError(f"{cid}: the registry holds no published row for {manuscript}; an unpublished clip is redone in place")
    for key in (*FROM_STEP_3, "subtitles"):
        if state.step(key).get("status") != "DONE":
            raise StateError(f"step {key} of {cid} is not DONE")
    if not (task / "clips" / cid / "subs/final.srt").is_file():
        raise StateError(f"{task / 'clips' / cid / 'subs/final.srt'} is missing")


def _copy_cut_on_host(adapters_for: AdaptersFor, source_dir: str, target_dir: str) -> None:
    target = shlex.quote(f"{target_dir}/cut")
    body = f"set -e\nmkdir -p {target}\ncp -a {shlex.quote(source_dir + '/cut')}/. {target}/\n"
    result = adapters_for(target_dir).remote.run_script(name="revise-copy-cut", body=body, timeout=COPY_CUT_TIMEOUT_S)
    if result.rc != 0:
        raise StateError(f"copying the cut of {source_dir} to {target_dir} failed: rc={result.rc} {result.stderr[-300:]}")


def _copy_local(src: Path, dst: Path) -> None:
    for name in COPIED:
        if (src / name).is_dir():
            shutil.copytree(src / name, dst / name)
        elif (src / name).is_file():
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src / name, dst / name)


def _successor_state(task: Path, state_x: ClipState, successor: str, remote_dir: str, cid: str, reason: str) -> ClipState:
    data = copy.deepcopy(state_x.data)
    clip = {k: v for k, v in data["clip"].items() if k not in STATUS_KEYS}
    clip.update(cid=successor, remote_dir=remote_dir)
    state = ClipState.create(task / "state" / f"{successor}.json", clip=cast("ClipInfo", clip), session=data["session"])
    for key, value in data.items():
        if key not in ("schema", "clip", "session", "created_at", "updated_at", *NOT_CARRIED):
            state.data[key] = value        # type: ignore[literal-required]
    xdir, ydir = str(task / "clips" / cid) + "/", str(task / "clips" / successor) + "/"
    for key in FROM_STEP_3:
        rec = copy.deepcopy(state_x.step(key))
        rec["products"] = {name: path.replace(xdir, ydir) for name, path in (rec.get("products") or {}).items()}
        rec["cpa"], rec["note"] = [], f"沿用 {cid}：{rec.get('note', '')}"
        state.data["steps"][key] = rec
    state.data["revised_from"] = {"cid": cid, "reason": reason, "at": utc_now()}
    state.save()
    return state


def _import_subtitles(task: Path, state_x: ClipState, state: ClipState, cid: str, successor: str, reason: str) -> None:
    """X's subtitles and speakers as they are now, as a verbatim push on the successor's clock (the same cut)."""
    xdir, ydir = task / "clips" / cid, task / "clips" / successor
    push = ydir / "revise" / "push"
    shutil.rmtree(push, ignore_errors=True)
    for rel in ("subs/final.srt", "speaker/speaker.srt", "subs/doubts.json", writing_canon.FILE):
        if (xdir / rel).is_file():
            (push / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(xdir / rel, push / rel)
    prior = state_x.data.get("imported")
    record_import(state, ydir, push, source="revise", authority=f"修订：{cid}（{reason}）", verbatim=True,
                  decided_by="owner" if prior is not None and prior.get("decided_by") == "owner" else None)


def revise_clip(*, task: Path, cid: str, reason: str, adapters_for: AdaptersFor) -> str:
    """Build the successor of the published clip `cid` of `task` and supersede `cid` by it; returns the successor's id.
    StateError (or the OSError of a copy) with the reason when it cannot be done; then nothing of the successor is left."""
    task = Path(task)
    if not reason.strip():
        raise StateError("revise needs a non-empty reason")
    state_x = ClipState.load(task / "state" / f"{cid}.json")
    _check_source(task, cid, state_x, adapters_for)
    successor = successor_id(task, cid)
    if len(successor) > MAX_CID:
        raise StateError(f"the successor id {successor} is longer than {MAX_CID} characters")
    remote_x = state_x.data["clip"]["remote_dir"]
    remote_y = f"{remote_x.rsplit('/', 1)[0]}/{successor}"
    _copy_cut_on_host(adapters_for, remote_x, remote_y)
    try:
        _copy_local(task / "clips" / cid, task / "clips" / successor)
        state = _successor_state(task, state_x, successor, remote_y, cid, reason)
        _import_subtitles(task, state_x, state, cid, successor, reason)
        cover_import.carry(state_x, state, task / "clips" / cid, task / "clips" / successor)
        done = supersede_candidate(
            task_dir=task, cid=cid, by=successor, authority=f"修订（revise 命令）：{cid}", quote=reason,
            reason=f"已发布稿件的修订：{reason}；另建 {successor}，原片与它的成品包保持原样")
        if not done.ok:
            raise StateError(f"supersede failed: {done.reason} {done.detail}")
    except BaseException:
        (task / "state" / f"{successor}.json").unlink(missing_ok=True)
        shutil.rmtree(task / "clips" / successor, ignore_errors=True)
        raise
    return successor
