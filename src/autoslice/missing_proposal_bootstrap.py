"""CPA-only proposal bootstrap for exact-final findings without candidates.

The final reviewer may correctly identify a bounded nonword while declining to
guess its replacement.  This module creates at most one text-context proposal;
it never hears audio and never authorizes a subtitle mutation.  The caller must
still route the proposal through candidate-blind AGY evidence and a separate CPA
CURRENT/PROPOSED judgment.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import extract_json_object


PROPOSAL_REBUILD_SCHEMA = "subtitle-closed-set-proposal-rebuild.v1"
PROPOSAL_REBUILD_CACHE_SCHEMA = "proposal-rebuild-cache.v1"
CPA_CONVERGENCE_SCHEMA = "subtitle-missing-proposal-cpa-convergence.v1"
CPA_CONVERGENCE_CACHE_SCHEMA = "missing-proposal-cpa-convergence-cache.v1"

_PROMPT = """# 字幕缺失候选重建（只提案，不裁决）

独立成品审片员确认目标 cue 中有一个可疑片段，但为了避免瞎猜，没有给出
替换候选。你现在是 CPA 的文字提案层：只能根据整片转写语境和绑定的结构化
聊天，提出一个有界的完整 cue 候选；你没有听过音频，也没有改字权限。

约束：
1. 只改目标 cue，输出单行完整字幕；不得改时间轴、合并或拆分 cue。
2. 修改必须覆盖审片员标出的 SUSPECT，且尽量最小；不得润色或概括。
3. 保留口语、专名、数字和中英日混说。证据不足时返回 UNRESOLVED，不能猜。
4. 候选产生后还必须经过 AGY 无候选盲听，再由另一轮 CPA 在 CURRENT 与
   PROPOSED 中作最终裁决；本轮提案绝不授权修改。

## 当前与可疑片段
- CURRENT: {current}
- SUSPECT: {suspect}
- 审片理由: {reviewer_reason}

## 同片转写语境（不是逐字真值）
前文:
{before}
目标 cue: {current}
后文:
{after}

## 绑定结构化聊天（可能为空，只作语境）
{structured_chat}

只回 JSON（无 markdown、无其他文字）：
{{"status":"PROPOSED"或"UNRESOLVED","proposed_cue":"单行完整候选；UNRESOLVED 时为空","reason":"一句话说明文字语境依据"}}
"""

_CONVERGENCE_PROMPT = """# 字幕缺失候选最终收敛（CPA 最终裁决）

上一轮文字提案没有产生一个满足局部 span 合同的候选。你现在是最终文字裁决者；
AGY 只会提供声学辅助，不能替你决定。此轮必须根据完整局部语境，在以下两个动作
中二选一，不能返回 UNRESOLVED：

- KEEP_EXISTING：现有完整 cue 在语境中合理，保留原文。
- REPLACE_WITH_EXACT_TEXT：现有 cue 确有语境错误，给出应替换成的单行完整 cue。

约束：
1. 只裁决目标 cue；不得改时间轴、合并或拆分 cue。
2. replacement_text 必须是完整单行 cue；不得润色、概括或补写没有语境依据的信息。
3. 结构化聊天只是绑定语境，不是逐字真值；必须结合相邻消息链与 cue 上下文判断。
4. KEEP_EXISTING 时 replacement_text 必须为空；REPLACE_WITH_EXACT_TEXT 时必须与
   CURRENT 不同，并覆盖审片员指出的 SUSPECT。
5. 你的决定将绑定 cue/current/context/final-SRT 哈希并直接成为最终裁决。

## 哈希绑定
- FINAL_SRT_SHA256: {final_srt_sha256}
- CURRENT_CUE_SHA256: {current_cue_sha256}
- CONTEXT_SHA256: {context_sha256}

## 目标
- CUE_INDEX: {cue_index}
- CURRENT: {current}
- SUSPECT: {suspect}
- 审片理由: {reviewer_reason}
- 上轮提案结果: {proposal_failure}

## 相邻字幕（前 3 / 后 3）
前文:
{before}
目标 cue: {current}
后文:
{after}

## 邻近结构化消息链（按时间顺序，只作语境）
{structured_chat}

只回 JSON（无 markdown、无其他文字）：
{{"decision":"KEEP_EXISTING"或"REPLACE_WITH_EXACT_TEXT","replacement_text":"完整单行 cue；KEEP_EXISTING 时为空","reason":"一句话说明语境依据"}}
"""


def _cache_path(prompt_sha256: str) -> Path | None:
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


def _convergence_cache_path(prompt_sha256: str) -> Path | None:
    base = os.environ.get("AUTOSLICE_BASE")
    if not base:
        return None
    return (
        Path(base)
        / "cache"
        / "missing-proposal-cpa-convergence"
        / prompt_sha256[:2]
        / f"{prompt_sha256}.json"
    )


def converge_missing_proposal(
    *,
    srt_text: str,
    finding: Mapping[str, Any],
    structured_chat: str,
    proposal_audit: Mapping[str, Any],
    llm_call: Callable[[str], str] | None,
    derive_single_span_edit: Callable[..., tuple[str, str, int, int, str | None]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Force a second CPA pass to decide keep-current or exact replacement.

    This is the final text decision for a missing-candidate finding.  It does
    not ask AGY to choose words.  Provider/schema failure remains fail-closed.
    """

    audit: dict[str, Any] = {
        "schema_version": CPA_CONVERGENCE_SCHEMA,
        "status": "UNAVAILABLE",
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
        "timing_immutable": True,
    }
    if llm_call is None:
        audit["reason_code"] = "CPA_CONVERGENCE_LLM_UNAVAILABLE"
        return None, audit
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    try:
        cue_index = int(finding.get("cue_index") or 0)
    except (TypeError, ValueError):
        cue_index = 0
    if not 1 <= cue_index <= len(cues):
        audit.update(status="INVALID", reason_code="CONTEXT_CUE_INDEX_INVALID")
        return None, audit
    current = cues[cue_index - 1].text
    current_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
    if finding.get("base_text_sha256") not in {None, current_sha256}:
        audit.update(status="INVALID", reason_code="STALE_BASE")
        return None, audit
    suspect = str(finding.get("suspect") or "")
    suspect_occurrence_count = current.count(suspect) if suspect else 0
    if suspect_occurrence_count < 1:
        audit.update(status="INVALID", reason_code="SUSPECT_NOT_FOUND")
        return None, audit
    before_rows = cues[max(0, cue_index - 4) : cue_index - 1]
    after_rows = cues[cue_index : min(len(cues), cue_index + 3)]
    before = "\n".join(row.text for row in before_rows) or "（无）"
    after = "\n".join(row.text for row in after_rows) or "（无）"
    structured = structured_chat or "（无）"
    final_srt_sha256 = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    context_binding = {
        "cue_index": cue_index,
        "current": current,
        "before": [row.text for row in before_rows],
        "after": [row.text for row in after_rows],
        "structured_chat": structured,
    }
    context_sha256 = hashlib.sha256(
        json.dumps(
            context_binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    prompt = _CONVERGENCE_PROMPT.format(
        final_srt_sha256=final_srt_sha256,
        current_cue_sha256=current_sha256,
        context_sha256=context_sha256,
        cue_index=cue_index,
        current=current,
        suspect=suspect,
        reviewer_reason=str(finding.get("why") or "")[:240],
        proposal_failure=json.dumps(
            {
                "status": proposal_audit.get("status"),
                "reason_code": proposal_audit.get("reason_code"),
                "reason": proposal_audit.get("reason"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        before=before,
        after=after,
        structured_chat=structured,
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    audit.update(
        cue_index=cue_index,
        final_srt_sha256="sha256:" + final_srt_sha256,
        current_cue_sha256="sha256:" + current_sha256,
        context_sha256="sha256:" + context_sha256,
        prompt_sha256="sha256:" + prompt_sha256,
        suspect_occurrence_count=suspect_occurrence_count,
    )
    cache_path = _convergence_cache_path(prompt_sha256)
    payload: Mapping[str, Any] | None = None
    completion_sha256: str | None = None
    served_from_cache = False
    if cache_path is not None:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, Mapping)
                and cached.get("schema_version") == CPA_CONVERGENCE_CACHE_SCHEMA
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
            completion_sha256 = hashlib.sha256(
                completion.encode("utf-8")
            ).hexdigest()
            payload = extract_json_object(completion)
        except Exception as exc:
            audit.update(
                reason_code="CPA_CONVERGENCE_PROVIDER_OR_JSON_UNAVAILABLE",
                error_type=type(exc).__name__,
            )
            return None, audit
    audit["completion_sha256"] = "sha256:" + str(completion_sha256)
    if served_from_cache:
        audit["served_from_cache"] = True
    decision = str(payload.get("decision") or "")
    replacement_text = payload.get("replacement_text")
    reason = str(payload.get("reason") or "")[:240]
    if decision == "KEEP_EXISTING" and replacement_text in {"", None}:
        audit.update(
            status="RESOLVED",
            decision=decision,
            replacement_text="",
            reason=reason,
            mutation_authorized=False,
        )
        rebuilt = dict(finding)
        rebuilt["_cpa_missing_proposal_convergence"] = audit
    elif decision == "REPLACE_WITH_EXACT_TEXT":
        if (
            not isinstance(replacement_text, str)
            or replacement_text != replacement_text.strip()
            or not replacement_text
            or replacement_text == current
            or "\n" in replacement_text
            or "\r" in replacement_text
            or "-->" in replacement_text
            or len(replacement_text) > 96
            or any(ord(char) < 32 for char in replacement_text)
        ):
            audit.update(
                status="INVALID",
                reason_code="CPA_CONVERGENCE_TEXT_CONTRACT_INVALID",
                reason=reason,
            )
            return None, audit
        edit_suspect, replacement, start, end, error = derive_single_span_edit(
            current,
            replacement_text,
            allow_insertion=True,
            allow_deletion=True,
        )
        full_cue_audit_fallback = error is not None
        if full_cue_audit_fallback:
            edit_suspect = current
            replacement = replacement_text
            start = 0
            end = len(current)
        # Unlike the proposal layer, this is CPA's final, hash-bound decision
        # over the complete target cue.  A local SequenceMatcher span is only
        # audit metadata here: shared prefixes, repeated one-character
        # suspects, or a broad exact rewrite must not let deterministic code
        # overrule CPA and recreate a permanent suggestion=null blocker.
        audit.update(
            status="RESOLVED",
            decision=decision,
            replacement_text=replacement_text,
            replacement_text_sha256="sha256:"
            + hashlib.sha256(replacement_text.encode("utf-8")).hexdigest(),
            reason=reason,
            mutation_authorized=True,
        )
        if full_cue_audit_fallback:
            audit["full_cue_audit_fallback"] = True
            audit["span_derivation_reason_code"] = error
        rebuilt = dict(finding)
        rebuilt.update(
            suspect=edit_suspect,
            suggestion=replacement,
            span_start_codepoint=start,
            span_end_codepoint=end,
            proposed_full_cue=replacement_text,
            base_text_sha256=current_sha256,
            candidate_provenance={
                "kind": "cpa_context_only_exact_text_convergence",
                "mutation_authorized": True,
                "prompt_sha256": "sha256:" + prompt_sha256,
                "context_sha256": "sha256:" + context_sha256,
                "final_srt_sha256": "sha256:" + final_srt_sha256,
            },
            candidate_memory_id=None,
            why=(f"[CPA 最终文字裁决] {reason or finding.get('why') or ''}")[
                :240
            ],
            _cpa_missing_proposal_convergence=audit,
        )
        if rebuilt.get("repair_class") == "disclosure_only":
            rebuilt["repair_class"] = "phonetic"
    else:
        audit.update(
            status="INVALID",
            reason_code="CPA_CONVERGENCE_DECISION_CONTRACT_INVALID",
            reason=reason,
        )
        return None, audit
    if cache_path is not None and not served_from_cache:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": CPA_CONVERGENCE_CACHE_SCHEMA,
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


def bootstrap_missing_proposal(
    *,
    srt_text: str,
    finding: Mapping[str, Any],
    structured_chat: str,
    llm_call: Callable[[str], str] | None,
    derive_single_span_edit: Callable[..., tuple[str, str, int, int, str | None]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Ask CPA for one bounded text candidate without mutation authority."""

    audit: dict[str, Any] = {
        "schema_version": PROPOSAL_REBUILD_SCHEMA,
        "status": "UNAVAILABLE",
        "decision_authority": "CPA_PROPOSAL_ONLY",
        "mutation_authorized": False,
    }
    if llm_call is None:
        audit["reason_code"] = "MISSING_PROPOSAL_LLM_UNAVAILABLE"
        return None, audit
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    try:
        cue_index = int(finding.get("cue_index") or 0)
    except (TypeError, ValueError):
        cue_index = 0
    if not 1 <= cue_index <= len(cues):
        audit.update(status="INVALID", reason_code="CONTEXT_CUE_INDEX_INVALID")
        return None, audit
    current = cues[cue_index - 1].text
    current_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
    if finding.get("base_text_sha256") not in {None, current_sha256}:
        audit.update(status="INVALID", reason_code="STALE_BASE")
        return None, audit
    suspect = str(finding.get("suspect") or "")
    if not suspect or current.count(suspect) != 1:
        audit.update(status="INVALID", reason_code="SUSPECT_NOT_UNIQUE")
        return None, audit
    before = cues[max(0, cue_index - 4) : cue_index - 1]
    after = cues[cue_index : min(len(cues), cue_index + 3)]
    prompt = _PROMPT.format(
        current=current,
        suspect=suspect,
        reviewer_reason=str(finding.get("why") or "")[:240],
        before="\n".join(row.text for row in before) or "（无）",
        after="\n".join(row.text for row in after) or "（无）",
        structured_chat=structured_chat or "（无）",
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
                and cached.get("schema_version") == PROPOSAL_REBUILD_CACHE_SCHEMA
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
                reason_code="MISSING_PROPOSAL_PROVIDER_OR_JSON_UNAVAILABLE",
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
        or proposed == current
        or "\n" in proposed
        or "\r" in proposed
        or "-->" in proposed
        or len(proposed) > 96
        or any(ord(char) < 32 for char in proposed)
    ):
        audit.update(
            status="INVALID",
            reason_code="MISSING_PROPOSAL_TEXT_CONTRACT_INVALID",
            reason=reason,
        )
        return None, audit
    edit_suspect, replacement, start, end, error = derive_single_span_edit(
        current,
        proposed,
        allow_insertion=True,
        allow_deletion=True,
    )
    bounded_full_cue_repair = bool(
        error == "EDIT_LENGTH_DELTA_TOO_LARGE"
        and suspect == current
        and len(current) <= 24
        and len(proposed) <= 24
    )
    if bounded_full_cue_repair:
        edit_suspect = current
        replacement = proposed
        start = 0
        end = len(current)
        error = None
    original_start = current.index(suspect)
    original_end = original_start + len(suspect)
    if error is not None or start > original_start or end < original_end:
        audit.update(
            status="INVALID",
            reason_code=error or "MISSING_PROPOSAL_DOES_NOT_COVER_SUSPECT",
            reason=reason,
        )
        return None, audit
    rebuilt = dict(finding)
    rebuilt.update(
        suspect=edit_suspect,
        suggestion=replacement,
        span_start_codepoint=start,
        span_end_codepoint=end,
        proposed_full_cue=proposed,
        base_text_sha256=current_sha256,
        candidate_provenance={
            "kind": "cpa_context_proposal",
            "mutation_authorized": False,
            "prompt_sha256": "sha256:" + prompt_sha256,
        },
        candidate_memory_id=None,
        why=(f"[CPA 文字候选] {reason or finding.get('why') or ''}")[:240],
    )
    if rebuilt.get("repair_class") == "disclosure_only":
        rebuilt["repair_class"] = "phonetic"
    audit.update(
        status="PROPOSED",
        proposed_cue=proposed,
        proposed_cue_sha256="sha256:"
        + hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
        suspect=edit_suspect,
        suggestion=replacement,
        span_start_codepoint=start,
        span_end_codepoint=end,
        reason=reason,
    )
    if bounded_full_cue_repair:
        audit["bounded_full_cue_repair"] = True
    if cache_path is not None and not served_from_cache:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": PROPOSAL_REBUILD_CACHE_SCHEMA,
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


def prepare_missing_proposal_candidate(
    *,
    srt_text: str,
    finding: Mapping[str, Any],
    structured_chat: str,
    llm_call: Callable[[str], str] | None,
    derive_single_span_edit: Callable[..., tuple[str, str, int, int, str | None]],
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Prepare a finding and a ready fail-closed response for the caller."""

    missing = (
        finding.get("proposed_full_cue") is None
        and not str(finding.get("suggestion") or "")
        and bool(str(finding.get("suspect") or ""))
    )
    if not missing:
        return dict(finding), None, None
    rebuilt, audit = bootstrap_missing_proposal(
        srt_text=srt_text,
        finding=finding,
        structured_chat=structured_chat,
        llm_call=llm_call,
        derive_single_span_edit=derive_single_span_edit,
    )
    if rebuilt is not None:
        return rebuilt, audit, None
    converged, convergence_audit = converge_missing_proposal(
        srt_text=srt_text,
        finding=finding,
        structured_chat=structured_chat,
        proposal_audit=audit,
        llm_call=llm_call,
        derive_single_span_edit=derive_single_span_edit,
    )
    if converged is not None:
        return converged, audit, None
    return None, audit, {
        "schema_version": "subtitle-span-adjudication.v1",
        "status": "MISSING_PROPOSAL_UNRESOLVED",
        "repaired": False,
        "reason_code": convergence_audit.get("reason_code"),
        "proposal_bootstrap": audit,
        "cpa_missing_proposal_convergence": convergence_audit,
        "decision_authority": "CPA_JUDGE_FAILED",
    }
