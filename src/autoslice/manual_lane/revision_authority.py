"""Step 8 for a successor of a published manuscript (`<cid>_trim`, `<cid>_r<N>`): the same-BV repair authority and the
record of what the successor changes against the published version.

A manuscript that the registry holds as published is only ever redone in a successor clip, whose package replaces the
published one on the same BV.  Step 8 therefore builds the authority for the successor itself (from the registry row and the
first-publication receipt it binds, read where the receipt is readable: title_history.through_boundary; scripts/
build_new_bv_repair_authority.py is the same builder) and hands it to the
finished-media lane's check and seal (`--repair-authority`).  A successor whose authority cannot be built stops step 8
with the builder's reason: no package is made that could not be used for the revision.  A clip that is not a successor, or
whose manuscript is not published, gets neither the authority nor the flag.

`revision.json` (clips/<cid>/) says which BV is revised and what differs from the version online now, as the sha256 of the
video and the cover against those of that version (the first publication's, or after a repair the latest completion's:
receipt_chain), and the completion receipt of the latest repair that changed the video with its `new_cid` (`predecessor_completed`,
null when none): the upload step reads it to decide between replacing the video and replacing only the cover, and takes the
receipt as `--predecessor-completed`.  A manuscript repaired before is built from its first-publication receipt all the same:
the registry row binds the latest completion, and the chain behind it is walked in the root boundary.

A manuscript whose title was edited after its first publication (the authority's `observed_public_title` is the first title, its
`title` the one now) has the receipt of that edit registered under its name (title_receipt, `record-title-receipt`).  Step 8 hands
it to the finished-media lane's check and seal as `--title-receipt` next to `--repair-authority` (where the authority is shape-only,
the receipt is too: it was replayed in full when it was registered), so the package record carries `verified_title_repair`, which
the reconciliation of a cover-only repair requires.  A cover-only revision of such a manuscript without a registered receipt stops
step 8 (TITLE_RECEIPT_UNREGISTERED): online the repair would succeed and its local reconciliation could not."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import publication_id, title_history, title_receipt
from .publication_id import publication_cid
from .runner import Ctx, StepOutcome
from .state import StateError
from .step_io import _fail

AUTHORITY_UNBUILT = "REVISION_AUTHORITY_UNBUILT"
TITLE_RECEIPT_UNREGISTERED = "TITLE_RECEIPT_UNREGISTERED"
AUTHORITY_FILE = "repair_authority.json"
REVISION_FILE = "revision.json"
ORIGIN_FILE = "repair_authority.origin"      # where the first-publication evidence was replayed and checked: BOUNDARY or DIRECT
BOUNDARY, DIRECT = "BOUNDARY", "DIRECT"
FLAG = "--repair-authority"
TITLE_FLAG = "--title-receipt"
SHAPE_ONLY = "--authority-shape-only"
SCHEMA = "published-revision.v1"


def authority_path(ctx: Ctx) -> Path:
    return ctx.clip_dir / AUTHORITY_FILE


def _task(ctx: Ctx) -> Path:
    return ctx.clip_dir.parent.parent


def title_changed(authority: dict) -> bool:
    """Whether the title was edited after the first publication: the authority's final title is not the title first published."""
    return authority["title"] != authority["observed_public_title"]


def applicable_title_receipt(ctx: Ctx, authority: dict) -> dict | None:
    """The registration of the title receipt of this manuscript when the clip's title is the one that edit gave (and differs from
    the first title); None otherwise.  A registration that gave another title is not this clip's chain (a revision may carry a
    new title of its own)."""
    if not title_changed(authority):
        return None
    registration = title_receipt.registered(_task(ctx), publication_cid(str(ctx.state.data["clip"]["cid"])))
    return registration if registration is not None and registration["final_title"] == authority["title"] else None


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
    registration = applicable_title_receipt(ctx, json.loads(path.read_text(encoding="utf-8")))
    return [FLAG, str(path), *([SHAPE_ONLY] if shape_only else []),
            *([TITLE_FLAG, registration["receipt"]["path"]] if registration else [])]


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
                     f"{type(exc).__name__}: {exc!s:.600}"), []
    if not published:
        for stale in (path, ctx.clip_dir / REVISION_FILE, ctx.clip_dir / ORIGIN_FILE):
            stale.unlink(missing_ok=True)
        return None, []
    try:
        title = (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip()
        authority, via_boundary = title_history.build_authority(ctx, manuscript, package=package_name, title=title)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        return _fail(AUTHORITY_UNBUILT, f"{cid} 是已发布稿件「{manuscript}」的后继片，建不出同稿件修订的换源授权，不打包："
                     f"{type(exc).__name__}: {exc!s:.600}"), []
    try:
        registration = applicable_title_receipt(ctx, authority)
    except StateError as exc:
        return _fail(TITLE_RECEIPT_UNREGISTERED, f"{cid}: {exc}"), []
    if title_changed(authority) and registration is None and _cover_only(ctx):
        return _fail(TITLE_RECEIPT_UNREGISTERED, (
            f"{cid} 只换封面，但「{manuscript}」的标题在首发后改过（首发「{authority['observed_public_title']}」，现「{authority['title']}」），"
            f"稿件名下没有这次改标题的回执：线上换封面会成功，本地对账会因缺 verified_title_repair 对不上。先登记标题修订回执："
            f"manual_lane record-title-receipt --task-dir {_task(ctx)} --cid {cid} --receipt <title-repair 的 VERIFIED_TITLE_ONLY 回执>，再重跑第 8 步")), []
    path.write_text(json.dumps(authority, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ctx.clip_dir / ORIGIN_FILE).write_text((BOUNDARY if via_boundary else DIRECT) + "\n", encoding="utf-8")
    where = ("首发证据已在 root 边界内重放核对，本进程读不到那些文件：check、seal 加 --authority-shape-only（只跳过证据重放，授权形状与候选/标题绑定照查；上传步骤仍完整重放）"
             if via_boundary else "首发证据由本进程直接读到并完整重放，check、seal 不加 --authority-shape-only")
    carried = f"；标题修订回执（{registration['receipt']['path']}，登记时已在{'root 边界内' if registration['full_replay']['via'] == BOUNDARY else '本进程'}完整重放）一并带上" \
        if registration else ""
    return None, [f"后继片：同稿件修订的换源授权已建（{path.name}，BV {authority['bvid']}），成品审查 check、seal 带上它；{where}{carried}"]


def online_tags(ctx: Ctx) -> list[str] | None:
    """The tags the published version of this clip's manuscript carries online now, for a successor (`<cid>_trim`, `<cid>_r<N>`) of
    a published manuscript; None for any other clip (nothing is read).  A successor keeps the tags of the version it replaces:
    the upload tool replaces only the cover only when title, description, tags and partition equal the live ones, and a video
    replacement keeps the live tags.  OSError / ValueError / RuntimeError when the registry or the receipts cannot be read."""
    cid = str(ctx.state.data["clip"]["cid"])
    manuscript = publication_cid(cid)
    if manuscript == cid or not title_history.published_status(ctx, manuscript):
        return None
    return title_history.online_tags(ctx, manuscript)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cover_only(ctx: Ctx) -> bool:
    """Whether the revision is a cover replacement alone: a `revise` successor (not a cut picture) whose subtitles are still the
    ones it was made with (no `import-subtitles` after) and which has a decided cover."""
    cid = str(ctx.state.data["clip"]["cid"])
    imported = ctx.state.data.get("imported") or {}
    return (publication_id.lineage_ending(cid) == "revise" and imported.get("source") == "revise"
            and ctx.state.data.get("imported_cover") is not None)


def _changed_metadata(ctx: Ctx, staged: Path, online: dict | None) -> dict:
    """What the tags and the description of the staged package are against the version online: {tags, description} as
    True (differs) / False (same) / None (the chain does not say)."""
    if online is None:
        return {"tags": None, "description": None}
    now_tags = json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8")).get("final_tags")
    desc = online.get("desc")
    return {"tags": sorted(now_tags or []) != sorted(online["tags"]),
            "description": None if desc is None else (staged / "description.txt").read_text(encoding="utf-8").strip() != desc.strip()}


def record(ctx: Ctx, staged: Path, notes: list[str]) -> dict[str, str]:
    """Write clips/<cid>/revision.json from the staged package files (`final.mp4`, `cover.png`, `description.txt`) and
    tags/tags.json; returns the step's products for it ({} when step 8 built no authority for this clip).  A revision that is
    a cover replacement alone yet differs from the version online in the video, the tags or the description gets a conspicuous
    line in `notes` (the step's PROCESS) and in revision.json `flags`: it does not stop the step, the upload step chooses its
    route from revision.json (the cover-only route refuses what is not field-for-field the live version)."""
    path = authority_path(ctx)
    if not path.is_file():
        return {}
    authority = json.loads(path.read_text(encoding="utf-8"))
    video, cover, predecessor, online = title_history.online_version(ctx, publication_cid(str(ctx.state.data["clip"]["cid"])), authority)
    published = {"video_sha256": video, "cover_sha256": cover}
    now = {"video_sha256": _sha(staged / "final.mp4"), "cover_sha256": _sha(staged / "cover.png")}
    changed = {"video": now["video_sha256"] != published["video_sha256"], "cover": now["cover_sha256"] != published["cover_sha256"]}
    metadata = _changed_metadata(ctx, staged, online)
    registration = applicable_title_receipt(ctx, authority)
    flags: list[str] = []
    if _cover_only(ctx):
        for what, differs, hint in (
                ("视频", changed["video"], "成片字节与线上不同：重新烧录的编码/片头/字幕与线上那版不一致，查片头是否按稿件 id 取、字幕是否与前身一致"),
                ("标签", metadata["tags"], "标签与线上不同：第 7 步应沿用线上标签，查 tags/tags.json 的来源与复审是否改了标签"),
                ("简介", metadata["description"], "简介与线上不同：简介由标题生成，查标题是否与线上不同")):
            if differs:
                flags.append(f"只换封面的修订，但{what}变了（{hint}）；上传步骤将按 revision.json 选路线，只换封面路线会拒绝")
    notes.extend(f"【注意·不拦】{line}" for line in flags)
    out = ctx.clip_dir / REVISION_FILE
    out.write_text(json.dumps({
        "schema_version": SCHEMA, "manuscript": publication_cid(str(ctx.state.data["clip"]["cid"])),
        "bvid": authority["bvid"], "authority": str(path), "authority_sha256": authority["authority_sha256"],
        "published": published, "predecessor_completed": predecessor, "revision": now,
        "changed": changed, "metadata_changed": metadata, "flags": flags,
        "title": {"changed_since_first_publication": title_changed(authority),
                  "first_published": authority["observed_public_title"], "now": authority["title"],
                  "receipt": registration["receipt"]["path"] if registration else None},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"revision": str(out), "repair_authority": str(path)}
