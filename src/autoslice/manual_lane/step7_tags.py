"""Step 7: tags (tool; CPA inside the tool).

Split out of stage_impl.py (TASK-13); stage_impl re-exports every name, so callers and tests keep
`stage_impl.<name>`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.autoslice.authorized_upload_metadata import validate_tags
from src.autoslice.upload_tag_policy import apply_tag_policy

from . import revision_authority
from .adapters import remote_python, root_script
from .runner import Ctx, StepOutcome
from .step_io import REMOTE_TOOLS, _fail, _local, _run_remote


ONLINE_SOURCE = "published"      # tags.json `source` of a successor that keeps the tags online (no tool, no model)


def _proper_noun_tags(data: dict[str, Any]) -> list[str]:
    rows = data.get("proper_noun_tags")
    if not isinstance(rows, list):
        return []
    return [str(r["tag"]).strip() for r in rows if isinstance(r, dict) and isinstance(r.get("tag"), str) and r["tag"].strip()]


def _prior_proper_noun_tags(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return _proper_noun_tags(old) if isinstance(old, dict) else []


def _dedup_ci(items: list[str], *, exclude_keys: set[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = item.strip()
        key = clean.casefold()
        if not clean or key in exclude_keys or key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def apply_review_tags(ctx: Ctx, tags: Any, skipped: list[str] | None = None) -> bool:
    """Persist the review's full selection and its removals before a downstream rebuild.  Not for a successor of a published
    manuscript, whose tags are the ones online: a revision changes what it is for, and there is no revision that changes
    tags, so a tag change asked by the review is refused here (False) and the tags stay.  Every accepted list satisfies the
    same policy step 7 enforces (apply_tag_policy): cap, case-insensitive de-duplication, length/separator validity and fixed
    base tags kept."""
    path = ctx.clip_dir / "tags/tags.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        if skipped is not None:
            skipped.append("review tags not applied: tags.json root is not an object")
        return False
    previous = data.get("final_tags")
    if (
        not isinstance(previous, list)
        or not previous
        or any(not isinstance(t, str) or not t.strip() for t in previous)
    ):
        if skipped is not None:
            skipped.append(
                f"review tags not applied: tags.json final_tags is missing or invalid ({previous!r})"
            )
        return False
    if data.get("source") == ONLINE_SOURCE:
        if tags is not None and skipped is not None:
            skipped.append("review tags not applied: a revision keeps the published tags")
        return False
    prev_state = ctx.state.data.get("reviewed_tags", {})
    prev_proper = [t for t in prev_state.get("proper_noun_tags", []) if isinstance(t, str) and t.strip()]
    proper = _dedup_ci([*prev_proper, *_proper_noun_tags(data)], exclude_keys=set())
    if tags is None:
        settled = apply_tag_policy(previous, previous=previous, priority_tags=proper, on_overflow="evict", prefer_new=False)
    else:
        settled = apply_tag_policy(tags, previous=previous, priority_tags=proper, on_overflow="evict", prefer_new=True)
    if not settled.ok:
        if skipped is not None:
            skipped.append(f"review tags not applied ({'; '.join(settled.reasons)})")
        return False
    if settled.reasons and skipped is not None:
        skipped.append(f"review tags adjusted by policy ({'；'.join(settled.reasons)})")
    kept = list(settled.tags)
    kept_keys = {t.casefold() for t in kept}
    proper_keys = {t.casefold() for t in proper}
    kept_proper = [t for t in kept if t.casefold() in proper_keys]
    prev_removed = [t for t in prev_state.get("removed", []) if isinstance(t, str)]
    removed = _dedup_ci([*prev_removed, *previous, *settled.dropped], exclude_keys=kept_keys)
    ctx.state.put("reviewed_tags", {"kept": kept, "removed": removed, "proper_noun_tags": kept_proper})
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
    reviewed = ctx.state.data.get("reviewed_tags")
    if reviewed and "proper_noun_tags" not in reviewed:
        kept_keys = {t.strip().casefold() for t in reviewed.get("kept", []) if isinstance(t, str) and t.strip()}
        migrated = [
            t for t in _prior_proper_noun_tags(ctx.clip_dir / "tags/tags.json") if t.casefold() in kept_keys
        ]
        ctx.state.put("reviewed_tags", {**reviewed, "proper_noun_tags": migrated})
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
    if not isinstance(tags, dict):
        return _fail("TAGS_POLICY_INVALID", "tags.json root must be an object")
    if tags.get("status") != "OK":
        # OK_NO_LLM is the tool's degraded run (base tags only): a tool failure, not a judgement.
        return _fail("TAGS_TOOL_DEGRADED", f"status={tags.get('status')!r}")
    raw_generated = tags.get("final_tags")
    if (
        not isinstance(raw_generated, list)
        or not raw_generated
        or any(not isinstance(t, str) or not t.strip() for t in raw_generated)
    ):
        return _fail(
            "TAGS_POLICY_INVALID",
            f"generated final_tags must be a non-empty list of non-empty strings: {raw_generated!r}"[:400],
        )
    if raw_problems := validate_tags(raw_generated):
        return _fail(
            "TAGS_POLICY_INVALID",
            f"generated final_tags invalid: {'; '.join(raw_problems)}"[:400],
        )
    extra_process: list[str] = []
    reviewed = ctx.state.data.get("reviewed_tags")
    if reviewed:
        removed_keys = {t.strip().casefold() for t in reviewed.get("removed", []) if isinstance(t, str)}
        prev_proper = [t for t in reviewed.get("proper_noun_tags", []) if isinstance(t, str) and t.strip()]
        priority = _dedup_ci([*prev_proper, *_proper_noun_tags(tags)], exclude_keys=set())
        combined = [
            *reviewed["kept"],
            *(t for t in raw_generated if t.strip().casefold() not in removed_keys),
        ]
        settled = apply_tag_policy(
            combined,
            previous=raw_generated,
            priority_tags=priority,
            on_overflow="evict",
            prefer_new=False,
        )
        if not settled.ok:
            return _fail("TAGS_POLICY_INVALID", "; ".join(settled.reasons))
        tags["final_tags"] = list(settled.tags)
        tags["final_tag_line"] = ",".join(tags["final_tags"])
        local_tags.write_text(json.dumps(tags, ensure_ascii=False, indent=2), encoding="utf-8")
        priority_keys = {t.casefold() for t in priority}
        kept_proper = [t for t in settled.tags if t.casefold() in priority_keys]
        if settled.dropped or kept_proper != prev_proper:
            kept_keys = {t.casefold() for t in settled.tags}
            prev_removed = [t for t in reviewed.get("removed", []) if isinstance(t, str)]
            updated_removed = _dedup_ci([*prev_removed, *settled.dropped], exclude_keys=kept_keys)
            ctx.state.put(
                "reviewed_tags",
                {"kept": list(settled.tags), "removed": updated_removed, "proper_noun_tags": kept_proper},
            )
        if settled.reasons:
            extra_process.append(f"标签按策略收口（{'；'.join(settled.reasons)}）")
    final = tags["final_tags"]
    if problems := validate_tags(final):
        return _fail("TAGS_POLICY_INVALID", "; ".join(problems))
    return StepOutcome(True, products={"tags": str(local_tags)},
                       note=f"{len(final)} 个标签（CPA 判定，直接采用）",
                       process=[f"标签（CPA 判定，直接采用）：{'、'.join(str(t) for t in final)}",
                                *extra_process,
                                f"标签工具在 OCI3 内部调用 CPA 文本（content_review_status="
                                f"{tags.get('content_review_status')}；工具不报告次数，未计入 cpa 记录）"])


def review_tags(tags: dict[str, object]) -> str:
    """Final tags plus step 7's reason for each content tag: 「养」 (cute, clingy clips) was lost to a reader of the bare word."""
    final = tags.get("final_tags") if isinstance(tags.get("final_tags"), list) else []
    why = [f"- {r['tag']}：{r['why']}" for r in tags.get("content_tags") or [] if isinstance(r, dict) and r.get("why") and r.get("tag") in final]
    return "\n".join([json.dumps(final, ensure_ascii=False)] + (["内容标签按频道的标签规则选，下面是选它的理由（有的是圈内词，意思以理由为准）；理由和字幕对不上才删："] + why if why else []))
