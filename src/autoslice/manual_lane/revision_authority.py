"""Step 8 for a successor of a published manuscript (`<cid>_trim`, `<cid>_r<N>`): the same-BV repair authority and the
record of what the successor changes against the published version.

A manuscript that the registry holds as published is only ever redone in a successor clip, whose package replaces the
published one on the same BV.  Step 8 therefore builds the authority for the successor itself (from the registry row and the
first-publication receipt it binds, read where the receipt is readable: title_history.through_boundary; scripts/
build_new_bv_repair_authority.py is the same builder) and hands it to the
finished-media lane's check and seal (`--repair-authority`).  A successor whose authority cannot be built stops step 8
with the builder's reason: no package is made that could not be used for the revision.  A clip that is not a successor, or
whose manuscript is not published, gets neither the authority nor the flag.

`revision.json` (clips/<cid>/) says which BV is revised and what differs from the published version, as the sha256 of the
video and the cover against those the first publication's manifest recorded: the upload step reads it to decide between
replacing the video and replacing only the cover."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import title_history
from .publication_id import publication_cid
from .runner import Ctx, StepOutcome
from .step_io import _fail

AUTHORITY_UNBUILT = "REVISION_AUTHORITY_UNBUILT"
AUTHORITY_FILE = "repair_authority.json"
REVISION_FILE = "revision.json"
ORIGIN_FILE = "repair_authority.origin"      # where the first-publication evidence was replayed and checked: BOUNDARY or DIRECT
BOUNDARY, DIRECT = "BOUNDARY", "DIRECT"
FLAG = "--repair-authority"
SHAPE_ONLY = "--authority-shape-only"
SCHEMA = "published-revision.v1"


def authority_path(ctx: Ctx) -> Path:
    return ctx.clip_dir / AUTHORITY_FILE


def fmr_args(ctx: Ctx) -> list[str]:
    """The finished-media lane's arguments for the authority step 8 built for this clip; [] when it built none.  When the
    authority was built (its first-publication closure replayed and checked) inside the root boundary, this process cannot
    read those root-only files, so check and seal take it as to its shape and bindings only (`--authority-shape-only`: the
    lane skips just the replay of the evidence files); built by this process, they replay it in full."""
    path = authority_path(ctx)
    if not path.is_file():
        return []
    origin = ctx.clip_dir / ORIGIN_FILE
    shape_only = origin.is_file() and origin.read_text(encoding="utf-8").strip() == BOUNDARY
    return [FLAG, str(path), *([SHAPE_ONLY] if shape_only else [])]


def prepare(ctx: Ctx, package_name: str) -> tuple[StepOutcome | None, list[str]]:
    """(the failure that stops step 8, or None; PROCESS lines).  Builds clips/<cid>/repair_authority.json for a successor of a
    published manuscript, with the clip's own final title; `package_name` is the lane's candidate id of the package."""
    cid = str(ctx.state.data["clip"]["cid"])
    manuscript = publication_cid(cid)
    if manuscript == cid:
        return None, []
    path = authority_path(ctx)
    try:
        published = title_history.published_status(ctx, manuscript)
    except (OSError, ValueError, RuntimeError) as exc:
        return _fail(AUTHORITY_UNBUILT, f"{cid} 是「{manuscript}」的后继片，读不到出版登记，无法确认是否已发布，不打包："
                     f"{type(exc).__name__}: {exc!s:.200}"), []
    if not published:
        for stale in (path, ctx.clip_dir / REVISION_FILE, ctx.clip_dir / ORIGIN_FILE):
            stale.unlink(missing_ok=True)
        return None, []
    try:
        title = (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip()
        authority, via_boundary = title_history.build_authority(ctx, manuscript, package=package_name, title=title)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        return _fail(AUTHORITY_UNBUILT, f"{cid} 是已发布稿件「{manuscript}」的后继片，建不出同稿件修订的换源授权，不打包："
                     f"{type(exc).__name__}: {exc!s:.300}"), []
    path.write_text(json.dumps(authority, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ctx.clip_dir / ORIGIN_FILE).write_text((BOUNDARY if via_boundary else DIRECT) + "\n", encoding="utf-8")
    where = ("首发证据已在 root 边界内重放核对，本进程读不到那些文件：check、seal 加 --authority-shape-only（只跳过证据重放，授权形状与候选/标题绑定照查；上传步骤仍完整重放）"
             if via_boundary else "首发证据由本进程直接读到并完整重放，check、seal 不加 --authority-shape-only")
    return None, [f"后继片：同稿件修订的换源授权已建（{path.name}，BV {authority['bvid']}），成品审查 check、seal 带上它；{where}"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record(ctx: Ctx, staged: Path) -> dict[str, str]:
    """Write clips/<cid>/revision.json from the staged package files (`final.mp4`, `cover.png`); returns the step's products
    for it ({} when step 8 built no authority for this clip)."""
    path = authority_path(ctx)
    if not path.is_file():
        return {}
    authority = json.loads(path.read_text(encoding="utf-8"))
    video, cover = title_history.first_digests(ctx, publication_cid(str(ctx.state.data["clip"]["cid"])), authority)
    published = {"video_sha256": video, "cover_sha256": cover}
    now = {"video_sha256": _sha(staged / "final.mp4"), "cover_sha256": _sha(staged / "cover.png")}
    out = ctx.clip_dir / REVISION_FILE
    out.write_text(json.dumps({
        "schema_version": SCHEMA, "manuscript": publication_cid(str(ctx.state.data["clip"]["cid"])),
        "bvid": authority["bvid"], "authority": str(path), "authority_sha256": authority["authority_sha256"],
        "published": published, "revision": now,
        "changed": {"video": now["video_sha256"] != published["video_sha256"],
                    "cover": now["cover_sha256"] != published["cover_sha256"]},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"revision": str(out), "repair_authority": str(path)}
