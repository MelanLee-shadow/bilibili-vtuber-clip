"""Text-only third-candidate rebuild after a CPA bad-closed-set verdict."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from src.autoslice.llm_client import extract_json_object
from src.autoslice.exact_source_transcript_contract import _text_sha256

_PROPOSAL_REBUILD_SCHEMA = "subtitle-closed-set-proposal-rebuild.v1"
_PROPOSAL_REBUILD_CACHE_SCHEMA = "proposal-rebuild-cache.v1"

_PROPOSAL_REBUILD_PROMPT = """# 字幕坏闭集重建（只提案，不裁决）

上一位 CPA 法官已经明确判定 CURRENT 和 REJECTED_PROPOSED 都不是目标区间的
完整原话（NEITHER）。你现在只负责重建一个第三候选；你不是音频模型，也没有
直接听过音频。AGY 拼音只是高可信辅助，可能只覆盖半句、邻句或错位窗口。

约束：
1. 只改目标 cue，不得合并、拆分或改时间轴；输出必须是单行完整字幕。
2. 尽量做最小修改，保留口语、专名、数字和中英混说；不得润色或概括。
3. 可以用前后 cue 修复跨 cue 误切，但本候选只能给出目标 cue 应保留的文字。
4. 若拼音、语法和语境足以确认说的是日语，普通日语词句必须写成假名/惯用日文，
   不得写罗马音或中文谐音；真正英语和已登记的官方拉丁专名保持原样。
5. 新候选必须同时不同于 CURRENT 和 REJECTED_PROPOSED；若证据仍不足以给出
   一个有界第三候选，返回 UNRESOLVED。不要声称自己听过音频。

## AGY 拼音辅助（未见候选）
{witness}

## 被拒闭集
- CURRENT: {current}
- REJECTED_PROPOSED: {rejected}

## 同片语境（转写上下文，不是逐字真值）
前文:
{before}
目标 cue: <待重建>
后文:
{after}

## 原审片发现与文字候选出处
{finding_evidence}

只回 JSON（无 markdown、无其他文字）：
{{"status":"PROPOSED"或"UNRESOLVED","proposed_cue":"单行完整第三候选；UNRESOLVED 时为空","reason":"一句话说明拼音覆盖与语境依据"}}
"""


def _proposal_rebuild_cache_path(prompt_sha256: str) -> Path | None:
    base = os.environ.get("AUTOSLICE_BASE")
    if not base:
        return None
    return (
        Path(base)
        / "cache"
        / "proposal-rebuilds"
        / prompt_sha256[:2]
        / f"{prompt_sha256}.json"
    )


def rebuild_candidate_after_neither(
    *,
    finding: Mapping[str, Any],
    request: Mapping[str, Any],
    witness: Mapping[str, Any],
    llm_call: Callable[[str], str] | None,
    derive_single_span_edit: Callable[..., tuple[str, str, int, int, str | None]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Ask the text-only proposal layer for one bounded third candidate."""

    audit: dict[str, Any] = {
        "schema_version": _PROPOSAL_REBUILD_SCHEMA,
        "status": "UNAVAILABLE",
        "decision_authority": "CPA_PROPOSAL_ONLY",
        "mutation_authorized": False,
    }
    if llm_call is None:
        audit["reason_code"] = "PROPOSAL_REBUILD_LLM_UNAVAILABLE"
        return None, audit
    current = str(request.get("current_cue") or "")
    rejected = str(request.get("proposed_cue") or "")
    original_repair_class = str(finding.get("repair_class") or "")
    audible_acoustic_drop_rebuild = bool(
        original_repair_class == "acoustic_drop_cue"
        and rejected == ""
        and witness.get("schema_version") == "subtitle-span-acoustic-witness.v1"
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is True
    )
    audit.update(
        original_repair_class=original_repair_class,
        rejected_candidates=[current, rejected],
    )
    if original_repair_class == "acoustic_drop_cue" and not audible_acoustic_drop_rebuild:
        audit["reason_code"] = (
            "PROPOSAL_REBUILD_ACOUSTIC_DROP_REQUIRES_AUDIBLE_TARGET"
        )
        return None, audit
    prompt = _PROPOSAL_REBUILD_PROMPT.format(
        witness=json.dumps(
            {
                "target_audible": witness.get("target_audible"),
                "heard_pinyin": witness.get("heard_pinyin"),
                "syllable_count": witness.get("syllable_count"),
                "uncertain_positions": witness.get("uncertain_positions"),
                "confidence": witness.get("confidence"),
                "self_count_mismatch": witness.get("self_count_mismatch"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        current=current,
        rejected=rejected,
        before=str(request.get("context_before") or "（无）"),
        after=str(request.get("context_after") or "（无）"),
        finding_evidence=json.dumps(
            {
                "repair_class": finding.get("repair_class"),
                "reviewer_reason": finding.get("why"),
                "candidate_provenance": finding.get("candidate_provenance"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    prompt_sha256 = _text_sha256(prompt)
    audit["prompt_sha256"] = "sha256:" + prompt_sha256
    cache_path = _proposal_rebuild_cache_path(prompt_sha256)
    payload: Mapping[str, Any] | None = None
    completion_sha256: str | None = None
    served_from_cache = False
    if cache_path is not None:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, Mapping)
                and cached.get("schema_version") == _PROPOSAL_REBUILD_CACHE_SCHEMA
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
            completion_sha256 = _text_sha256(completion)
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
    status = str(payload.get("status") or "")
    proposed = payload.get("proposed_cue")
    reason = str(payload.get("reason") or "")[:240]
    if status != "PROPOSED":
        audit.update(status="UNRESOLVED", reason=reason)
        return None, audit
    if (
        not isinstance(proposed, str)
        or proposed != proposed.strip()
        or not proposed
        or proposed in {"", current, rejected}
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
    suspect, replacement, start, end, error = derive_single_span_edit(
        current, proposed, allow_insertion=True, allow_deletion=True
    )
    if error is not None:
        audit.update(status="INVALID", reason_code=error, reason=reason)
        return None, audit
    rebuilt = dict(finding)
    rebuilt.update(
        suspect=suspect,
        suggestion=replacement,
        span_start_codepoint=start,
        span_end_codepoint=end,
        proposed_full_cue=proposed,
        base_text_sha256=_text_sha256(current),
        why=(f"[CPA 坏闭集重建] {reason or finding.get('why') or ''}")[:240],
    )
    if audible_acoustic_drop_rebuild:
        rebuilt["repair_class"] = "spoken_unit"
        rebuilt["candidate_provenance"] = None
        rebuilt["candidate_memory_id"] = None
    provenance = rebuilt.get("candidate_provenance")
    surface = (
        str(provenance.get("surface") or "").strip()
        if isinstance(provenance, Mapping)
        else ""
    )
    if surface and surface.casefold() not in proposed.casefold():
        rebuilt["candidate_provenance"] = None
        rebuilt["candidate_memory_id"] = None
    audit.update(
        status="PROPOSED",
        proposed_cue=proposed,
        proposed_cue_sha256="sha256:" + _text_sha256(proposed),
        suspect=suspect,
        suggestion=replacement,
        span_start_codepoint=start,
        span_end_codepoint=end,
        reason=reason,
        rebuilt_repair_class=str(rebuilt.get("repair_class") or ""),
    )
    if cache_path is not None and not served_from_cache:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": _PROPOSAL_REBUILD_CACHE_SCHEMA,
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
    return rebuilt, audit
