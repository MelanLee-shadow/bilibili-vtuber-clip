"""The `redo` command: a clip's step, and every step that depends on it, is made again by the code of today.

`redo --cid C --from <step>` forgets the records of that step and of the steps that depend on it (`stages.DEPENDS`, the
transitive closure), so the next `run` makes them again; nothing is run here and no model is asked.  What it does, in order:

1. refuses a clip that is SUPERSEDED, a closure that holds a finished upload, and, when the closure holds the package, a
   package that is sealed and was used online (`package_guards.protected`, the definition step 8 uses: the published clip's
   own package, or a successor's package that the manuscript's receipt chain attests): a published clip is redone in a
   successor clip (`revise`), whose package is its own until it goes online;
2. when the closure holds the cut and the picture of the clip comes from a joined source (compilation.json: a theme
   compilation or the successor of `trim`), builds that source again from the plan the manifest records
   (`compilation.rebuild_media`) and replaces the old file; this is the only way the joined file is made again;
3. copies the state file to clips/<cid>/redo/ with the UTC time in its name, forgets the steps, and drops the review's own
   state keys (review, rewind, revisions) when the closure holds the review;
4. deletes the files the forgotten steps recorded as products (inside the clip directory, never under import/ or
   import_cover/), and for the package step the directories it builds by "reuse when present" (pkg, fmr, fmr_source).

Why deleted, not left to be overwritten: the runner reuses a step when its record is DONE and its products exist, and a few
steps keep a file they find (step 8's local copy of the cut), so a file left from the old run could stand in for the new
one.  With the records and the recorded products gone, the clip is where a clip is before it runs these steps, and `run`
then produces them under the same rules as for a new clip.  Not step products, so kept as they are: the imported
subtitles and cover (state `imported`, `imported_cover` and their directories), the labels, the review's objections
(`dissent`), the reviewed tags, the clip block and the session.  The imported cover is written to cover/cover.png again by
step 6 from import_cover/.
"""

from __future__ import annotations

import datetime as dt
import shutil
from collections.abc import MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from . import compilation, cover_import, package_guards
from .reselect import SUPERSEDED
from .runner import Ctx, Step
from .stage_package import package_dir
from .stages import DEPENDS, STEPS
from .state import ClipState, StateError
from .step_import import IMPORT_DIR
from .trim_run import AdaptersFor

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

REVIEW_STATE_KEYS = ("review", "rewind", "revisions")       # state keys that belong to the review step's products
RESIDUE_DIRS = {"package": ("pkg", "fmr", "fmr_source")}      # built by the step, found and reused by it, not recorded
KEPT_DIRS = (IMPORT_DIR, cover_import.IMPORT_DIR)             # the owner's finished subtitles and cover


class RedoResult(TypedDict):
    cid: str
    start: str
    closure: list[str]        # the steps in scope: the start step and every step that depends on it
    cleared: list[str]        # of those, the steps that had a record, which is what was cleared
    not_recorded: list[str]   # of those, the steps that had no record (never run, or already forgotten): nothing to clear
    summary: str
    deleted_files: int
    source_rebuilt: bool
    backup: str


def resolve_step(spec: str) -> Step:
    """The step named by its key (`title_cover`) or its number (`6`)."""
    for step in STEPS:
        if spec == step.key or spec == str(step.number):
            return step
    raise StateError(f"no step {spec!r}; the steps are " + ", ".join(f"{s.number} {s.key}" for s in STEPS))


def closure(start: Step) -> list[Step]:
    """`start` and every step that depends on it, directly or not, in the list order."""
    wanted = {start.number}
    for step in STEPS:                 # every step depends only on earlier ones: one pass in list order is enough
        if wanted.intersection(DEPENDS[step.number]):
            wanted.add(step.number)
    return [step for step in STEPS if step.number in wanted]


def _inside(path: Path, clip_dir: Path) -> bool:
    root = clip_dir.resolve()
    target = path.resolve()
    return target.is_relative_to(root) and not any(target.is_relative_to(root / kept) for kept in KEPT_DIRS)


def _delete_products(state: ClipState, steps: list[Step], clip_dir: Path) -> int:
    deleted = 0
    for step in steps:
        for value in (state.step(step.key).get("products") or {}).values():
            path = Path(value)
            if path.is_file() and _inside(path, clip_dir):
                path.unlink()
                deleted += 1
        for name in RESIDUE_DIRS.get(step.key, ()):
            shutil.rmtree(clip_dir / name, ignore_errors=True)
    return deleted


def redo_clip(*, task: Path, cid: str, start: str, adapters_for: AdaptersFor) -> RedoResult:
    """Forget step `start` of clip `cid` and its dependants (see the module docstring).  StateError with the reason when it
    cannot be done; then nothing was changed (the joined source is the one thing made before the state is, and is replaced only
    by a file that is as long as the plan)."""
    task = Path(task)
    state = ClipState.load(task / "state" / f"{cid}.json")
    clip = state.data["clip"]
    if clip.get("status") == SUPERSEDED:
        raise StateError(f"{cid} is SUPERSEDED by {clip.get('superseded_by')}; redo the replacement")
    steps = closure(resolve_step(start))
    keys = [step.key for step in steps]
    if "upload" in keys and state.step("upload").get("status") == "DONE":
        raise StateError(f"{cid} has been uploaded; an uploaded clip is not redone (`revise` makes a successor)")
    clip_dir = task / "clips" / cid
    ctx = Ctx(state=state, session=state.data["session"], clip_dir=clip_dir, remote_dir=clip["remote_dir"],
              adapters=adapters_for(clip["remote_dir"]))
    if "package" in keys and (refused := package_guards.protected(ctx, package_dir(ctx))) is not None:
        raise StateError(f"{refused.reason}: {refused.note}")
    rebuilt = "cut" in keys and compilation.load_manifest(clip_dir) is not None
    if rebuilt:
        compilation.rebuild_media(clip_dir=clip_dir, remote_dir=clip["remote_dir"], remote=ctx.adapters.remote)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = clip_dir / "redo" / f"state-{stamp}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(state.path, backup)
    cleared = [key for key in keys if key in state.data["steps"]]
    not_recorded = [key for key in keys if key not in cleared]
    summary = (f"范围内的步骤（closure）：{', '.join(keys)}；实际清掉记录的步骤（cleared）：{', '.join(cleared) or '无'}；"
               f"范围内但本来就没有记录、无需清的步骤（not_recorded）：{', '.join(not_recorded) or '无'}")
    deleted = _delete_products(state, steps, clip_dir)
    for key in cleared:
        state.data["steps"].pop(key)
    if "review" in keys:
        for name in REVIEW_STATE_KEYS:
            cast("MutableMapping[str, JsonValue]", state.data).pop(name, None)
    state.save()
    with (clip_dir / "PROCESS.md").open("a", encoding="utf-8") as fh:
        fh.write(f"\n## 重做 · 从 {keys[0]} · {stamp}\n\n- {summary}；"
                 f"状态备份 {backup}；{'拼接源已按记录的方案重新生成' if rebuilt else '画面不来自拼接源，未重建'}\n")
    return {"cid": cid, "start": keys[0], "closure": keys, "cleared": cleared, "not_recorded": not_recorded, "summary": summary,
            "deleted_files": deleted,
            "source_rebuilt": rebuilt, "backup": str(backup)}
