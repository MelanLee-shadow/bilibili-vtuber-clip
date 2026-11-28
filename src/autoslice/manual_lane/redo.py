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
   state keys (review, rewind, revisions) when the closure holds the review, and the review's tag choice (reviewed_tags)
   when it also holds the tags: those tags are made again, and step 7 would otherwise apply to them what an earlier review
   chose on other materials (11-18: a recut clip lost 「养」 again, removed by the review before the cut was redone);
4. deletes the files the forgotten steps recorded as products (inside the clip directory, never under import/ or
   import_cover/), and the directories a step builds and reuses when present: the package's pkg, fmr, fmr_source; the
   review is forgotten by `stage_package.forget_review`, the one function every path that forgets the review calls,
   which also deletes what the review resumes from on disk: its post-review listening rounds (wide_held.ROOT: the
   snapshot it takes before listening is kept when present and each round's listening is read back from its file, so a
   redo that left them replayed the old run's review text and rounds on the new subtitles -- 11-24 316670, redone on
   11-24, carried the 11-23 report that called the collab a solo session and never listened again to its five held
   lines) and the focused review calls' saved questions (cpa/: a question with no record naming it is resumed as a
   request in flight, so with the rounds gone the review still asked 11-23's question and got 11-23's answer back).

Why deleted, not left to be overwritten: the runner reuses a step when its record is DONE and its products exist, and a few
steps keep a file they find (step 8's local copy of the cut), so a file left from the old run could stand in for the new
one.  With the records and the recorded products gone, the clip is where a clip is before it runs these steps, and `run`
then produces them under the same rules as for a new clip.  Not step products, so kept as they are: the imported
subtitles and cover (state `imported`, `imported_cover` and their directories), the labels, the review's objections
(`dissent`), the reviewed tags when the tags are not made again, the clip block and the session.  The imported cover is written to cover/cover.png again by
step 6 from import_cover/.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import uuid
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from . import compilation, cover_import, package_guards, title_acceptance
from .reselect import SUPERSEDED
from .runner import Ctx, Step
from .stage_package import forget_review, package_dir
from .stages import DEPENDS, STEPS
from .state import ClipState, StateError
from .step_import import IMPORT_DIR
from .trim_run import AdaptersFor

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

RESIDUE_DIRS = {"package": ("pkg", "fmr", "fmr_source")}     # built by the step, found and reused by it, not recorded
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
    fresh_title: bool
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


def _carry_receipt_involved(clip_dir: Path, *, step6_status: str) -> str:
    """Before `redo` deletes `title/candidates.json`, copy `chosen.involved` into a valid old-format
    `title/accepted.json` that lacks `involved` ONLY when all three hold: step 6 (`title_cover`) is `DONE` in clip
    state, `receipt['by'] == 'step6'`, and `title/title.txt` equals the receipt's title (same successful step-6 run,
    even when writing canon changed `chosen.title`'s spelling).  Returns a note for the redo summary / PROCESS.md when
    `candidates.json` had `chosen.involved` on a valid receipt lacking `involved` but any condition failed."""
    receipt = title_acceptance.load_receipt(clip_dir).receipt
    if receipt is None or receipt.get("involved"):
        return ""
    cands_path = clip_dir / "title/candidates.json"
    if not cands_path.is_file():
        return ""
    try:
        doc = json.loads(cands_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(doc, dict):
        return ""
    chosen = doc.get("chosen")
    if not isinstance(chosen, dict):
        return ""
    raw_involved = chosen.get("involved")
    if not isinstance(raw_involved, list):
        return ""
    involved = list(dict.fromkeys(x.strip() for x in raw_involved if isinstance(x, str) and x.strip()))
    if not involved:
        return ""
    if step6_status != "DONE":
        return (f"旧格式过判回执缺 involved，但第 6 步状态为 {step6_status or '未记录'}（非 DONE），"
                f"无法确认 title/candidates.json 属于同一次成功的第 6 步，未迁入 involved（{'、'.join(involved)}）")
    by = str(receipt.get("by") or "")
    if by != "step6":
        return (f"旧格式过判回执缺 involved，且回执来源为 {by}（非 step6），"
                f"与 title/candidates.json 候选不对应，未迁入 involved（{'、'.join(involved)}）")
    receipt_title = str(receipt["title"]).strip()
    title_file = clip_dir / "title/title.txt"
    if not title_file.is_file():
        return f"旧格式过判回执缺 involved，但 title/title.txt 缺失，无法确认候选对应关系，未迁入 involved（{'、'.join(involved)}）"
    try:
        current_title = title_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"旧格式过判回执缺 involved，但 title/title.txt 无法读取（{exc}），未迁入 involved"
    if current_title != receipt_title:
        return (f"旧格式过判回执缺 involved，且 title/title.txt（「{current_title}」）与回执标题（「{receipt_title}」）不一致，"
                f"未迁入 involved（{'、'.join(involved)}）")
    title_acceptance.write(
        clip_dir,
        str(receipt["title"]),
        str(receipt["by"]),
        cast("Mapping[str, object] | None", receipt.get("judge")),
        objections=cast("Sequence[str]", receipt.get("objections") or ()),
        cover_lines=cast("Sequence[str] | None", receipt.get("cover_lines")),
        involved=involved,
        retitle_evidence=cast("str | None", receipt.get("retitle_evidence")),
    )
    return ""


def redo_clip(*, task: Path, cid: str, start: str, adapters_for: AdaptersFor,
              fresh_title: bool = False) -> RedoResult:
    """Forget step `start` of clip `cid` and its dependants (see the module docstring).  StateError with the reason when it
    cannot be done; then nothing was changed (the joined source is the one thing made before the state is, and is replaced only
    by a file that is as long as the plan).  `title/accepted.json` stays on disk so a step-6 re-run enters the earlier
    accepted title in round 0 unless `fresh_title=True` explicitly asks for a new title."""
    task = Path(task)
    state = ClipState.load(task / "state" / f"{cid}.json")
    clip = state.data["clip"]
    if clip.get("status") == SUPERSEDED:
        raise StateError(f"{cid} is SUPERSEDED by {clip.get('superseded_by')}; redo the replacement")
    steps = closure(resolve_step(start))
    keys = [step.key for step in steps]
    if fresh_title and "title_cover" not in keys:
        raise StateError(f"--fresh-title only applies when title_cover is in the redo closure (not {keys[0]})")
    if fresh_title and isinstance(state.data.get("revised_from"), dict):
        raise StateError(f"{cid} is a revision of a published manuscript; a revision's title changes only by the owner's word (retitle / approved title), not --fresh-title")
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
    prev_title = ""
    prev_attempt = 0
    carry_note = ""
    if fresh_title:
        prior = title_acceptance.load_receipt(clip_dir).receipt
        if prior is not None:
            prev_title = str(prior["title"]).strip()
        elif (title_file := clip_dir / "title/title.txt").is_file():
            prev_title = title_file.read_text(encoding="utf-8").strip()
        existing_fresh = state.data.get(title_acceptance.FRESH_TITLE_KEY)
        if isinstance(existing_fresh, dict):
            if not prev_title and isinstance(existing_fresh.get("previous_title"), str):
                prev_title = existing_fresh["previous_title"].strip()
            if isinstance(existing_fresh.get("attempt"), int) and not isinstance(existing_fresh.get("attempt"), bool):
                prev_attempt = existing_fresh["attempt"]
    elif "title_cover" in keys:
        step6_status = str(state.step("title_cover").get("status") or "")
        carry_note = _carry_receipt_involved(clip_dir, step6_status=step6_status)
    if carry_note:
        summary = f"{summary}；{carry_note}"
    deleted = _delete_products(state, steps, clip_dir)
    if fresh_title:
        receipt = clip_dir / title_acceptance.RECEIPT
        if receipt.is_file() and _inside(receipt, clip_dir):
            receipt.unlink()
            deleted += 1
        fresh_payload: dict[str, JsonValue] = {"request": f"{stamp}-{uuid.uuid4().hex[:8]}", "attempt": prev_attempt}
        if prev_title:
            fresh_payload["previous_title"] = prev_title
        cast("MutableMapping[str, JsonValue]", state.data)[title_acceptance.FRESH_TITLE_KEY] = fresh_payload
    for key in cleared:
        state.data["steps"].pop(key)
    if "review" in keys:
        deleted += forget_review(state, clip_dir)
        if "tags" in keys:
            cast("MutableMapping[str, JsonValue]", state.data).pop("reviewed_tags", None)
    state.save()
    with (clip_dir / "PROCESS.md").open("a", encoding="utf-8") as fh:
        fh.write(f"\n## 重做 · 从 {keys[0]} · {stamp}\n\n- {summary}；"
                 f"状态备份 {backup}；{'拼接源已按记录的方案重新生成' if rebuilt else '画面不来自拼接源，未重建'}"
                 + ("；要求重起标题（已清除原有过判记录）" if fresh_title else "") + "\n")
    return {"cid": cid, "start": keys[0], "closure": keys, "cleared": cleared, "not_recorded": not_recorded, "summary": summary,
            "deleted_files": deleted,
            "source_rebuilt": rebuilt, "fresh_title": fresh_title, "backup": str(backup)}
