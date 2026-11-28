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
import re
import shlex
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
from .state import RECUT_PENDING, ClipState, StateError
from .step_import import IMPORT_DIR
from .trim_run import AdaptersFor

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

RESIDUE_DIRS = {"package": ("pkg", "fmr", "fmr_source")}     # built by the step, found and reused by it, not recorded
KEPT_DIRS = (IMPORT_DIR, cover_import.IMPORT_DIR)             # the owner's finished subtitles and cover
REMOTE_RECUT_DIRS = ("asr", "listen", "witness", "speaker", "burn", "cover", "tags", "fmr", "pkg")
LOCAL_RECUT_DIRS = ("asr", "listen", "witness")
REMOTE_CLEAN_SCRIPT = "redo-clean-remote"
_PLAIN_CID_RE = re.compile(r"[A-Za-z0-9_.-]+")


class RedoResult(TypedDict):
    cid: str
    start: str
    closure: list[str]        # the steps in scope: the start step and every step that depends on it
    cleared: list[str]        # of those, the steps that had a record, which is what was cleared
    not_recorded: list[str]   # of those, the steps that had no record (never run, or already forgotten): nothing to clear
    summary: str
    deleted_files: int
    remote_removed: list[str]
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


def _collect_products(state: ClipState, steps: Sequence[Step]) -> list[Path]:
    return [Path(value) for step in steps for value in (state.step(step.key).get("products") or {}).values()]


def _delete_products(product_paths: Sequence[Path], steps: list[Step], clip_dir: Path) -> int:
    deleted = 0
    for path in product_paths:
        if path.is_file() and _inside(path, clip_dir):
            path.unlink()
            deleted += 1
    for step in steps:
        for name in RESIDUE_DIRS.get(step.key, ()):
            residue = clip_dir / name
            if not residue.exists():
                continue
            try:      # a directory the step finds and reuses: one that cannot be removed would be reused for the new clip
                shutil.rmtree(residue)
            except OSError as exc:
                raise StateError(f"cannot remove {residue}: {exc}") from exc
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


def _validate_remote_recut(cid: str, clip_cid: object, remote_dir: object) -> str:
    """Validate clip id and remote work directory before any recut deletion."""
    if not isinstance(cid, str) or cid in (".", "..") or not _PLAIN_CID_RE.fullmatch(cid):
        raise StateError(f"invalid clip id for recut cleanup: {cid!r}")
    if clip_cid != cid:
        raise StateError(f"clip.cid {clip_cid!r} does not match state id {cid!r}")
    if not isinstance(remote_dir, str) or not remote_dir or any(ch in remote_dir for ch in "\x00\r\n"):
        raise StateError(f"invalid remote_dir for recut cleanup: {remote_dir!r}")
    parts = remote_dir.split("/")
    if not remote_dir.startswith("/") or "" in parts[1:] or "." in parts or ".." in parts or parts[-1] != cid:
        raise StateError(f"remote_dir {remote_dir!r} does not end with clip id {cid!r}")
    return remote_dir


def _validate_local_recut_dirs(clip_dir: Path) -> None:
    root = clip_dir.resolve()
    for name in LOCAL_RECUT_DIRS:
        target = clip_dir / name
        if target.is_symlink() or (target.exists() and not target.resolve().is_relative_to(root)):
            raise StateError(f"refusing to delete local directory outside clip_dir: {target}")


def _clean_remote_for_recut(ctx: Ctx, rd: str) -> list[str]:
    dirs = " ".join(REMOTE_RECUT_DIRS)
    body = (
        "set -e\n"
        f"rd={shlex.quote(rd)}\n"
        'if [ -d "$rd" ]; then\n'
        f"  for d in {dirs}; do\n"
        '    if [ -e "$rd/$d" ] || [ -L "$rd/$d" ]; then\n'
        '      rm -rf -- "$rd/$d"\n'
        "      printf 'REMOVED %s\\n' \"$d\"\n"
        "    fi\n"
        "  done\n"
        "fi\n"
    )
    res = ctx.adapters.remote.run_script(name=REMOTE_CLEAN_SCRIPT, body=body, timeout=300)
    if res.rc != 0:
        raise StateError(f"remote recut cleanup failed: rc={res.rc} {res.stderr[-300:]}")
    return [line.removeprefix("REMOVED ").strip()
            for line in res.stdout.splitlines()
            if line.startswith("REMOVED ") and line.removeprefix("REMOVED ").strip()]


def invalidate_for_recut(ctx: Ctx) -> tuple[int, list[str], bool]:
    """Run all local invalidation, compilation/trim source rebuild, and remote cleanup idempotently from `recut_pending`."""
    clip = ctx.state.data["clip"]
    rd = _validate_remote_recut(ctx.state.path.stem, clip.get("cid"), clip.get("remote_dir"))
    _validate_local_recut_dirs(ctx.clip_dir)
    marker = ctx.state.data.get(RECUT_PENDING)
    if isinstance(marker, Mapping):
        raw_products = marker.get("products")
        product_paths = [Path(str(p)) for p in raw_products] if isinstance(raw_products, list) else []
        needs_rebuild = bool(marker.get("rebuild"))
    else:
        product_paths = []
        needs_rebuild = compilation.load_manifest(ctx.clip_dir) is not None

    recut_steps = closure(resolve_step("cut"))
    deleted = _delete_products(product_paths, recut_steps, ctx.clip_dir)
    for name in LOCAL_RECUT_DIRS:
        if (p := ctx.clip_dir / name).is_dir():
            shutil.rmtree(p)
    with ctx.state.lock:
        deleted += forget_review(ctx.state, ctx.clip_dir)
        cast("MutableMapping[str, JsonValue]", ctx.state.data).pop("reviewed_tags", None)
        ctx.state.save()

    rebuilt = False
    if needs_rebuild:
        compilation.rebuild_media(clip_dir=ctx.clip_dir, remote_dir=rd, remote=ctx.adapters.remote)
        rebuilt = True
        with ctx.state.lock:
            cast("MutableMapping[str, JsonValue]", ctx.state.data)[RECUT_PENDING] = {"cleaned": False, "rebuild": False}
            ctx.state.save()

    remote_removed = _clean_remote_for_recut(ctx, rd)
    with ctx.state.lock:
        cast("MutableMapping[str, JsonValue]", ctx.state.data)[RECUT_PENDING] = {"cleaned": True, "rebuild": False}
        ctx.state.save()
    return deleted, remote_removed, rebuilt


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
    recut = "cut" in keys
    if recut:
        _validate_remote_recut(cid, clip.get("cid"), clip.get("remote_dir"))
        _validate_local_recut_dirs(clip_dir)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = clip_dir / "redo" / f"state-{stamp}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(state.path, backup)
    cleared = [key for key in keys if key in state.data["steps"]]
    not_recorded = [key for key in keys if key not in cleared]
    summary = (f"范围内的步骤（closure）：{', '.join(keys)}；实际清掉记录的步骤（cleared）：{', '.join(cleared) or '无'}；"
               f"范围内但本来就没有记录、无需清的步骤（not_recorded）：{', '.join(not_recorded) or '无'}")
    product_paths = _collect_products(state, steps)
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
    fresh_payload: dict[str, JsonValue] | None = None
    if fresh_title:
        fresh_payload = {"request": f"{stamp}-{uuid.uuid4().hex[:8]}", "attempt": prev_attempt}
        if prev_title:
            fresh_payload["previous_title"] = prev_title
    remote_removed: list[str] = []
    rebuilt = False
    if recut:
        needs_rebuild = compilation.load_manifest(clip_dir) is not None
        pending_products = [str(p) for p in product_paths]
        if fresh_payload is not None:
            pending_products.append(str(clip_dir / title_acceptance.RECEIPT))
            cast("MutableMapping[str, JsonValue]", state.data)[title_acceptance.FRESH_TITLE_KEY] = fresh_payload
        # One atomic pre-invalidation save persists pending products, rebuild flag, and drops step records from cut
        # onward so any interrupted rebuild or cleanup is safely resumed by invalidate_for_recut before cutting.
        cast("MutableMapping[str, JsonValue]", state.data)[RECUT_PENDING] = {
            "cleaned": False,
            "products": cast("list[JsonValue]", pending_products),
            "rebuild": needs_rebuild,
        }
        imported_rec = state.data.get("imported")
        if (isinstance(imported_rec, dict)
                and "cut_start_ms" not in imported_rec
                and "cut_end_ms" not in imported_rec
                and isinstance(clip.get("start_ms"), int)
                and isinstance(clip.get("end_ms"), int)):
            imported_rec["cut_start_ms"] = clip["start_ms"]
            imported_rec["cut_end_ms"] = clip["end_ms"]
        for key in cleared:
            state.data["steps"].pop(key)
        state.save()
        deleted, remote_removed, rebuilt = invalidate_for_recut(ctx)
        summary = f"{summary}；主机已清派生目录：{', '.join(remote_removed) if remote_removed else '无'}"
    else:
        deleted = _delete_products(product_paths, steps, clip_dir)
        if fresh_payload is not None:
            receipt = clip_dir / title_acceptance.RECEIPT
            if receipt.is_file() and _inside(receipt, clip_dir):
                receipt.unlink()
                deleted += 1
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
            "deleted_files": deleted, "remote_removed": remote_removed,
            "source_rebuilt": rebuilt, "fresh_title": fresh_title, "backup": str(backup)}
