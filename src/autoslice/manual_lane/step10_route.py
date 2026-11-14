"""Which road step 10 takes, from the clip's own facts (nothing is passed in): the registry (is the manuscript published?) and,
for a successor of a published manuscript, revision.json (what step 8 recorded against the version online now).

    manuscript not published                           FIRST   new BV upload
    published, the video differs from the live one     VIDEO   same-BV video replacement (keeps the live tags)
    published, only the cover differs                  COVER   same-BV cover replacement
    published, neither differs                         NONE    nothing to put online

The published original itself (not a successor) is refused: its package is protected and a revision is a successor clip
(`revise`).  A published manuscript's successor without revision.json is refused too: step 8 builds it, so run step 8 again."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import revision_authority, title_history
from .publication_id import publication_cid
from .runner import Ctx

FIRST, VIDEO, COVER, NONE = "first_publication", "replace_video", "replace_cover", "none"

REGISTRY_UNREADABLE = "UPLOAD_REGISTRY_UNREADABLE"
ALREADY_PUBLISHED = "UPLOAD_ALREADY_PUBLISHED"
REVISION_MISSING = "UPLOAD_REVISION_MISSING"


class RouteRefused(RuntimeError):
    def __init__(self, reason: str, note: str):
        super().__init__(note)
        self.reason, self.note = reason, note


@dataclass(frozen=True)
class Route:
    kind: str
    manuscript: str
    why: str
    bvid: str | None = None
    predecessor: str | None = None         # the completion receipt of the latest repair that changed the video (revision.json)
    flags: tuple[str, ...] = field(default_factory=tuple)


def _revision(ctx: Ctx, cid: str) -> dict:
    path = ctx.clip_dir / revision_authority.REVISION_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        changed = doc["changed"]
        if (doc["schema_version"] != revision_authority.SCHEMA or not isinstance(doc["bvid"], str) or not doc["bvid"]
                or type(changed["video"]) is not bool or type(changed["cover"]) is not bool):
            raise ValueError("revision.json 的字段不合约定")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RouteRefused(REVISION_MISSING, f"{cid} 是已发布稿件的后继片，读不到可用的 {path}（第 8 步建它）：{type(exc).__name__}: {exc!s:.300}；"
                           "先重跑第 8 步") from exc
    return doc


def decide(ctx: Ctx) -> Route:
    cid = str(ctx.state.data["clip"]["cid"])
    manuscript = publication_cid(cid)
    try:
        published = title_history.published_status(ctx, manuscript)
    except (OSError, ValueError, RuntimeError) as exc:
        raise RouteRefused(REGISTRY_UNREADABLE, f"读不到出版登记，无法确认「{manuscript}」是否已发布，不发："
                           f"{type(exc).__name__}: {exc!s:.600}") from exc
    if not published:
        return Route(FIRST, manuscript, f"「{manuscript}」在出版登记里没有已发布记录：首发")
    if cid == manuscript:
        raise RouteRefused(ALREADY_PUBLISHED, f"「{manuscript}」已发布，{cid} 是它本身而不是后继片：已发布的包不许覆盖，"
                           "要改请先 `revise` 建后继片，在后继片上重做")
    doc = _revision(ctx, cid)
    predecessor = (doc.get("predecessor_completed") or {}).get("path")
    common = {"manuscript": manuscript, "bvid": doc["bvid"], "predecessor": predecessor, "flags": tuple(doc.get("flags") or ())}
    changed = doc["changed"]
    if changed["video"]:
        return Route(VIDEO, why=f"后继片，revision.json 视频已变：同稿件（{doc['bvid']}）换视频，保留线上标签", **common)
    if changed["cover"]:
        return Route(COVER, why=f"后继片，revision.json 视频未变、封面已变：同稿件（{doc['bvid']}）只换封面", **common)
    return Route(NONE, why=f"后继片，revision.json 视频与封面都与线上（{doc['bvid']}）相同：无需上线", **common)
