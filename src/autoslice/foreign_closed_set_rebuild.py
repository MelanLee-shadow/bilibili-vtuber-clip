"""CPA proposal-only rebuild for a rejected foreign-audio closed set."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from src.autoslice.llm_client import extract_json_object


SCHEMA = "foreign-closed-set-proposal-rebuild.v1"
CACHE_SCHEMA = "foreign-closed-set-proposal-rebuild-cache.v1"

_PROMPT = """# 字幕外语坏闭集重建（只提案，不裁决）

上一轮 CPA 已判定 CURRENT 与 AGY_TRANSCRIPT 都不能完整表示目标 cue。你是文字
提案层，没有音频输入、没有改字权限；只能结合 AGY 候选盲听写、上一轮判词与
前后转写语境，重建一个有界的完整 cue。随后另一轮 CPA 会用同一份 AGY 声学
证据在 CURRENT/THIRD_CANDIDATE 中最终选边。

约束：
1. 只输出目标 cue 的单行完整字幕，不得改时间轴、拆分、合并、润色或概括。
2. 保留实际中英日混说；若证据足以确认说的是日语，普通日语词句必须写成
   假名/惯用日文，不得写罗马音或中文谐音；真正英语和已登记的官方拉丁专名
   保持原样。
3. 不得因为 AGY 把假名误成拉丁字母就机械抹掉语境中的专名。
4. 第三候选必须同时不同于 CURRENT 与 AGY_TRANSCRIPT；证据不足就 UNRESOLVED。
5. 不得声称听过音频，本轮提案绝不授权修改。

- CURRENT: {current}
- AGY_TRANSCRIPT: {rejected}
- AGY 声学证词: {witness}
- 上一轮 CPA 判词: {judge_reason}

前文:
{before}
后文:
{after}

只回 JSON（无 markdown、无其他文字）：
{{"status":"PROPOSED"或"UNRESOLVED","proposed_cue":"单行完整第三候选；UNRESOLVED 时为空","reason":"一句话说明组合依据"}}
"""


def _cache_path(prompt_sha256: str) -> Path | None:
    base = os.environ.get("AUTOSLICE_BASE")
    if not base:
        return None
    return (
        Path(base)
        / "cache"
        / "foreign-proposal-rebuilds"
        / prompt_sha256[:2]
        / f"{prompt_sha256}.json"
    )


def rebuild_foreign_closed_set(
    *,
    current: str,
    rejected: str,
    before: str,
    after: str,
    witness: Mapping[str, Any],
    judge_reason: str,
    llm_call: Callable[[str], str] | None,
) -> tuple[str | None, dict[str, Any]]:
    """Propose one third candidate; never authorize a mutation."""

    audit: dict[str, Any] = {
        "schema_version": SCHEMA,
        "status": "UNAVAILABLE",
        "decision_authority": "CPA_PROPOSAL_ONLY",
        "mutation_authorized": False,
    }
    if llm_call is None:
        audit["reason_code"] = "PROPOSAL_REBUILD_LLM_UNAVAILABLE"
        return None, audit
    prompt = _PROMPT.format(
        current=current,
        rejected=rejected,
        before=before or "（无）",
        after=after or "（无）",
        witness=json.dumps(
            {
                "heard_pinyin": witness.get("heard_pinyin"),
                "syllable_count": witness.get("syllable_count"),
                "confidence": witness.get("confidence"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        judge_reason=judge_reason[:240],
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    audit["prompt_sha256"] = "sha256:" + prompt_sha256
    cache_path = _cache_path(prompt_sha256)
    payload: Mapping[str, Any] | None = None
    completion_sha256: str | None = None
    served_from_cache = False
    if cache_path is not None:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, Mapping)
                and cached.get("schema_version") == CACHE_SCHEMA
                and cached.get("prompt_sha256") == prompt_sha256
                and isinstance(cached.get("payload"), Mapping)
                and isinstance(cached.get("completion_sha256"), str)
            ):
                payload = cached["payload"]
                completion_sha256 = str(cached["completion_sha256"])
                served_from_cache = True
        except (OSError, ValueError):
            pass
    if payload is None:
        try:
            completion = llm_call(prompt)
            completion_sha256 = hashlib.sha256(completion.encode("utf-8")).hexdigest()
            payload = extract_json_object(completion)
        except Exception as exc:
            audit.update(
                reason_code="PROPOSAL_REBUILD_PROVIDER_OR_JSON_UNAVAILABLE",
                error_type=type(exc).__name__,
            )
            return None, audit
    audit["completion_sha256"] = "sha256:" + str(completion_sha256)
    if served_from_cache:
        audit["served_from_cache"] = True
    proposed = payload.get("proposed_cue")
    reason = str(payload.get("reason") or "")[:240]
    if str(payload.get("status") or "") != "PROPOSED":
        audit.update(status="UNRESOLVED", reason=reason)
        return None, audit
    if (
        not isinstance(proposed, str)
        or proposed != proposed.strip()
        or not proposed
        or proposed in {current, rejected}
        or "\n" in proposed
        or "\r" in proposed
        or "-->" in proposed
        or len(proposed) > 96
        or any(ord(char) < 32 for char in proposed)
    ):
        audit.update(
            status="INVALID",
            reason_code="PROPOSAL_REBUILD_TEXT_CONTRACT_INVALID",
            reason=reason,
        )
        return None, audit
    audit.update(
        status="PROPOSED",
        proposed_cue=proposed,
        proposed_cue_sha256="sha256:"
        + hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
        reason=reason,
    )
    if cache_path is not None and not served_from_cache:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": CACHE_SCHEMA,
                        "prompt_sha256": prompt_sha256,
                        "completion_sha256": completion_sha256,
                        "payload": dict(payload),
                    },
                    ensure_ascii=False,
                    indent=1,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return proposed, audit
