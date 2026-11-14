"""What step 10 checks itself, and the small helpers its modules share.

Before anything is sent: the package is sealed by step 9, the chosen cover carries no vetoed-material flag (choose.json
`material_flag`: the cover step has already redone a vetoed cover, so a flag still there means it could not be made right).
After the plan dry run: the plan the tool froze is the one for this package (BV, title, cover, and for a video replacement the
video; each against the package files and against revision.json) - the check the hand run made before it built the real plan."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, NoReturn

from .runner import Ctx
from .step10_commands import Job
from .step10_route import VIDEO

NOT_SEALED = "UPLOAD_NOT_SEALED"
COVER_VETOED = "UPLOAD_COVER_MATERIAL_VETOED"
HELD = "UPLOAD_HELD"
HOLD_FILE = "upload/HOLD"
COVER_UNCHECKED = "UPLOAD_COVER_RECORD_MISSING"
PLAN_MISMATCH = "UPLOAD_PLAN_MISMATCH"


class Stop(Exception):
    """The step ends here with this reason (a failure, or one of the two waiting states)."""

    def __init__(self, reason: str, note: str):
        super().__init__(note)
        self.reason, self.note = reason, note


def stop(reason: str, note: str) -> NoReturn:
    raise Stop(reason, note)


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: not a JSON object")
    return doc


def tail(path: Path, n: int = 700) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:].strip()
    except OSError:
        return ""


def sealed(ctx: Ctx, package: Path) -> str:
    """The sha256 of the package's seal; stops when step 9 has not sealed it."""
    seal = package / "review_manifest.json"
    if ctx.state.step("review").get("status") != "DONE" or not seal.is_file():
        stop(NOT_SEALED, f"第 9 步没有封存这个包（review 步骤未完成或没有 {seal}）：不发")
    return sha(seal)


def held(ctx: Ctx) -> None:
    """A clip someone looked at and kept back: `<clip>/upload/HOLD` (who and why, any text).  Nothing is sent while the
    file is there; deleting it is the release."""
    path = ctx.clip_dir / HOLD_FILE
    if path.is_file():
        stop(HELD, f"这条片被留下不发：{path.read_text(encoding='utf-8').strip()[:300] or '（没有写原因）'}；要发先删掉 {path}")


def cover_material(ctx: Ctx) -> None:
    path = ctx.clip_dir / "cover/cand/choose.json"
    try:
        flag = read_json(path).get("material_flag")
    except (OSError, ValueError) as exc:
        stop(COVER_UNCHECKED, f"读不到 {path}，无法确认封面素材没有被否决：{type(exc).__name__}: {exc!s:.200}")
    if flag:
        stop(COVER_VETOED, f"封面素材被否决（choose.json material_flag：{json.dumps(flag, ensure_ascii=False)[:400]}）：不发，封面步骤重做封面后再来")


def plan_problems(job: Job, clip_dir: Path) -> list[str]:
    """How the dry-run plan (upload/plan-dry.log) differs from the package and revision.json; [] when it is the same."""
    try:
        plan = read_json(job.out / "plan-dry.log")
        revision = read_json(clip_dir / "revision.json")["revision"]
        cover = sha(job.package_file(".cover.png"))
        planned_cover = (plan.get("replacement_cover") or (plan.get("replacement") or {}).get("cover") or {}).get("sha256")
        meta = plan.get("unchanged_metadata") or plan.get("target_metadata") or {}
        problems = []
        if plan.get("bvid") != job.bvid:
            problems.append(f"计划的 BV 是 {plan.get('bvid')}，应为 {job.bvid}")
        if not (planned_cover == cover == str(revision["cover_sha256"]).split(":")[-1]):
            problems.append("计划里的封面、成片包封面、revision.json 的封面指纹不是同一个")
        if meta.get("title") != job.title:
            problems.append(f"计划的标题「{meta.get('title')}」不等于包里的标题「{job.title}」")
        if job.route == VIDEO:
            video = sha(job.package_file(".mp4"))
            planned_video = ((plan.get("replacement") or {}).get("video") or {}).get("sha256")
            if not (planned_video == video == str(revision["video_sha256"]).split(":")[-1]):
                problems.append("计划里的视频、成片包视频、revision.json 的视频指纹不是同一个")
        return problems
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return [f"预演计划读不出：{type(exc).__name__}: {exc!s:.200}"]
