"""Step 7: tags (tool; CPA inside the tool).

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.autoslice.upload_tag_policy import load_selected_upload_tag_policy

from . import revision_authority
from .adapters import remote_python, root_script
from .runner import Ctx, StepOutcome
from .step_io import REMOTE_TOOLS, _fail, _local, _run_remote


ONLINE_SOURCE = "published"      # tags.json `source` of a successor that keeps the tags online (no tool, no model)


def apply_review_tags(ctx: Ctx, tags: Any) -> bool:
    """Persist the review's full selection and its removals before a downstream rebuild.  Not for a successor of a published
    manuscript, whose tags are the ones online: a revision changes what it is for, and there is no revision that changes
    tags, so a tag change asked by the review is refused here (False) and the tags stay."""
    if json.loads((ctx.clip_dir / "tags/tags.json").read_text(encoding="utf-8")).get("source") == ONLINE_SOURCE:
        return False
    if tags is not None and not (isinstance(tags, list) and tags and all(isinstance(t, str) and t.strip() for t in tags)):
        return False
    path = ctx.clip_dir / "tags/tags.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    previous = data["final_tags"]
    kept = previous if tags is None else [t.strip() for t in tags]
    removed = list(dict.fromkeys([*ctx.state.data.get("reviewed_tags", {}).get("removed", []),
                                 *(t for t in previous if t not in kept)]))
    ctx.state.put("reviewed_tags", {"kept": kept, "removed": [t for t in removed if t not in kept]})
    if kept == previous:
        return False
    data["final_tags"], data["final_tag_line"] = kept, ",".join(kept)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ----- step 7: tags (tool; CPA inside the tool) -----------------------------------------------
def _keep_online_tags(ctx: Ctx, online: list[str]) -> StepOutcome:
    """A successor of a published manuscript takes the tags online as its tags: nothing is generated, no model is asked."""
    srt = ctx.clip_dir / "subs/final.srt"
    title = (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip()
    local_tags = _local(ctx, "tags/tags.json")
    local_tags.write_text(json.dumps({
        "status": "OK", "source": ONLINE_SOURCE, "content_review_status": "PUBLISHED_TAGS_KEPT",
        "final_tags": online, "final_tag_line": ",".join(online),
        "input_binding": {"title_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(), "srt_path": str(srt),
                          "srt_sha256": hashlib.sha256(srt.read_bytes()).hexdigest(), "existing_tags": online}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return StepOutcome(True, products={"tags": str(local_tags)}, note=f"{len(online)} 个标签（沿用线上版本，不重新生成）",
                       process=[f"标签沿用该稿件线上版本：{'、'.join(online)}；后继片的修订不改标签，不调用标签工具与模型"])


def step7_tags(ctx: Ctx) -> StepOutcome:
    try:
        online = revision_authority.online_tags(ctx)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        return _fail("ONLINE_TAGS_UNREADABLE", f"{ctx.state.data['clip']['cid']} 是已发布稿件的后继片，读不到线上那一版的标签，"
                     f"不重新生成：{type(exc).__name__}: {exc!s:.600}")
    if online is not None:
        return _keep_online_tags(ctx, online)
    rd = ctx.remote_dir
    ctx.adapters.remote.put(ctx.clip_dir / "title/title.txt", f"{rd}/tags/title.txt")
    ctx.adapters.remote.put(ctx.clip_dir / "subs/final.srt", f"{rd}/tags/final.srt")
    ctx.adapters.remote.put(REMOTE_TOOLS / "tags_remote.py", f"{rd}/tools/tags_remote.py")
    body = (f"mkdir -p {rd}/tags {rd}/tools\n"
            + root_script(remote_python(f"{rd}/tools/tags_remote.py {rd}/tags/title.txt {rd}/tags/final.srt "
                                        f"{rd}/tags/tags.json"),
                          env=["cpa"], owned=f"{rd}/tags", recursive=True))      # the tool's own files in tags/ too (it writes .normal-text-generations)
    res = _run_remote(ctx, "s7-tags", body, timeout=1800)
    if res.rc != 0:
        return _fail("TAGS_TOOL_FAILED", (res.stdout + res.stderr)[-400:])
    local_tags = _local(ctx, "tags/tags.json")
    ctx.adapters.remote.get(f"{rd}/tags/tags.json", local_tags)
    tags = json.loads(local_tags.read_text(encoding="utf-8"))
    if tags.get("status") != "OK":
        # OK_NO_LLM is the tool's degraded run (base tags only): a tool failure, not a judgement.
        return _fail("TAGS_TOOL_DEGRADED", f"status={tags.get('status')!r}")
    reviewed = ctx.state.data.get("reviewed_tags")
    if reviewed:
        final = list(dict.fromkeys([*reviewed["kept"],
                                    *(t for t in tags["final_tags"] if t not in reviewed["removed"])]))
        limit = max(len(reviewed["kept"]), load_selected_upload_tag_policy().max_tags_default)
        tags["final_tags"] = final[:limit]
        tags["final_tag_line"] = ",".join(tags["final_tags"])
        local_tags.write_text(json.dumps(tags, ensure_ascii=False, indent=2), encoding="utf-8")
    final = tags.get("final_tags") or []
    return StepOutcome(True, products={"tags": str(local_tags)},
                       note=f"{len(final)} 个标签（CPA 判定，直接采用）",
                       process=[f"标签（CPA 判定，直接采用）：{'、'.join(str(t) for t in final)}",
                                f"标签工具在 OCI3 内部调用 CPA 文本（content_review_status="
                                f"{tags.get('content_review_status')}；工具不报告次数，未计入 cpa 记录）"])
