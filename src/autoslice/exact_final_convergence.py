"""Hash-bound CPA convergence for repeated exact-final cue rewrites.

The ordinary exact-final judge owns each CURRENT/PROPOSED choice, but each
review pass is intentionally independent.  Without same-run memory, two valid
CPA receipts can therefore reverse one another until the bounded self-heal
budget is exhausted.  This module does not let code or the acoustic witness
choose text.  When an exact cue/time window is reconsidered, it gives CPA the
prior receipt chain and the new adjudication, records CPA's final binary choice,
and reuses that choice only while the exact local cue context remains unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import extract_json_object


MEMO_SCHEMA = "exact-final-cpa-convergence-memo.v1"
CACHE_SCHEMA = "exact-final-cpa-convergence-cache.v1"
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")

_PROMPT = """# Exact-final 字幕反复改写收敛（CPA 最终文字裁决）

同一个精确字幕时间窗已经有一条或多条 CPA 授权的修改收据，本轮独立审片又
提出了新的 CURRENT / PROPOSED 二元选择。AGY/Gemini 仅提供声学观察，不能
决定文字。你是最终文字裁决者，必须在本轮 CURRENT 与 PROPOSED 中二选一。

这次决定会绑定当前 cue、局部语境、精确时间窗、历史收据链和本轮证据哈希；
在这些绑定不变时，后续独立扫描不得再次反转。不要因为“上一轮改过”就机械
保留，也不要为了追求变化选择 PROPOSED；结合全部历史和本轮证据选最终文本。

## 绑定
- FINAL_SRT_SHA256: {srt_sha256}
- CUE_INDEX: {cue_index}
- MATCHED_START_MS: {start_ms}
- MATCHED_END_MS: {end_ms}
- CURRENT_CUE_SHA256: {current_sha256}
- PROPOSED_CUE_SHA256: {proposed_sha256}
- LOCAL_CONTEXT_SHA256: {context_sha256}
- HISTORY_SHA256: {history_sha256}
- NEW_EVIDENCE_SHA256: {evidence_sha256}

## 本轮闭集
- CURRENT: {current}
- PROPOSED: {proposed}

## 前后文
前文:
{before}
目标: {current}
后文:
{after}

## 先前 CPA 修改收据链
{history_json}

## 本轮 CPA/声学证据
{evidence_json}

只回 JSON（无 markdown、无其他文字）：
{{"choice":"CURRENT"或"PROPOSED","reason":"一句话说明最终选择依据"}}
"""


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _local_context_binding(
    srt_text: str,
    cue_index: int,
    *,
    target_text: str | None = None,
) -> tuple[str, list[str], list[str]]:
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not 1 <= cue_index <= len(cues):
        raise ValueError("CONVERGENCE_CUE_INDEX_INVALID")
    cue = cues[cue_index - 1]
    final_text = cue.text if target_text is None else target_text
    before_rows = cues[max(0, cue_index - 4) : cue_index - 1]
    after_rows = cues[cue_index : min(len(cues), cue_index + 3)]
    before = [row.text for row in before_rows]
    after = [row.text for row in after_rows]
    binding = {
        "cue_index": cue_index,
        "matched_start_ms": cue.start_ms,
        "matched_end_ms": cue.end_ms,
        "target_text": final_text,
        "before": before,
        "after": after,
    }
    return _json_sha256(binding), before, after


def _valid_repair(row: object) -> bool:
    if not isinstance(row, Mapping):
        return False
    before = row.get("before")
    after = row.get("after")
    mutation = row.get("mutation_authority")
    return bool(
        row.get("schema_version") == "exact-final-cpa-self-heal.v1"
        and row.get("decision_authority") == "CPA_JUDGE"
        and row.get("timing_immutable") is True
        and isinstance(before, str)
        and bool(before)
        and isinstance(after, str)
        and bool(after.strip())
        and row.get("before_sha256") == "sha256:" + _text_sha256(before)
        and row.get("after_sha256") == "sha256:" + _text_sha256(after)
        and _valid_sha256(row.get("finding_sha256"))
        and _valid_sha256(row.get("request_sha256"))
        and isinstance(row.get("matched_start_ms"), int)
        and not isinstance(row.get("matched_start_ms"), bool)
        and isinstance(row.get("matched_end_ms"), int)
        and not isinstance(row.get("matched_end_ms"), bool)
        and int(row["matched_start_ms"]) < int(row["matched_end_ms"])
        and isinstance(mutation, Mapping)
        and mutation.get("schema_version") == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
    )


def _repair_histories(
    authority_audit: Mapping[str, object],
) -> dict[tuple[int, int], list[dict[str, Any]]]:
    self_heal = authority_audit.get("exact_final_cpa_self_heal")
    passes = (
        self_heal.get("passes")
        if isinstance(self_heal, Mapping)
        and self_heal.get("schema_version") == "exact-final-cpa-self-heal-audit.v1"
        and self_heal.get("status") in {"REVIEW_PENDING", "PASS"}
        else None
    )
    histories: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for pass_receipt in passes if isinstance(passes, list) else []:
        repairs = (
            pass_receipt.get("repairs")
            if isinstance(pass_receipt, Mapping)
            and pass_receipt.get("schema_version") == "exact-final-cpa-self-heal-pass.v1"
            else None
        )
        for raw in repairs if isinstance(repairs, list) else []:
            if not _valid_repair(raw):
                continue
            row = dict(raw)
            key = (int(row["matched_start_ms"]), int(row["matched_end_ms"]))
            history = histories.setdefault(key, [])
            if history and history[-1]["after_sha256"] != row["before_sha256"]:
                # Keep only the latest contiguous, hash-bound chain.  A gap is
                # not proof that the older decision applies to the live cue.
                history.clear()
            history.append(row)
    return histories


def _compact_history(
    history: Iterable[Mapping[str, Any]],
) -> list[dict[str, object]]:
    return [
        {
            "before": row.get("before"),
            "after": row.get("after"),
            "before_sha256": row.get("before_sha256"),
            "after_sha256": row.get("after_sha256"),
            "finding_sha256": row.get("finding_sha256"),
            "request_sha256": row.get("request_sha256"),
            "mutation_basis": (
                row.get("mutation_authority", {}).get("basis")
                if isinstance(row.get("mutation_authority"), Mapping)
                else None
            ),
        }
        for row in history
    ]


def _compact_current_evidence(
    finding: Mapping[str, Any],
    adjudication: Mapping[str, Any],
) -> dict[str, object]:
    request = adjudication.get("request")
    verdict = adjudication.get("verdict")
    witness_judge = adjudication.get("witness_judge")
    judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
    return {
        "finding_base_text_sha256": finding.get("base_text_sha256"),
        "finding_proposed_full_cue": finding.get("proposed_full_cue"),
        "finding_reason": str(finding.get("why") or "")[:240],
        "request_sha256": (request.get("request_sha256") if isinstance(request, Mapping) else None),
        "candidate_provenance": (
            request.get("candidate_provenance")
            if isinstance(request, Mapping)
            else finding.get("candidate_provenance")
        ),
        "witness": (
            {
                key: verdict.get(key)
                for key in (
                    "status",
                    "target_audible",
                    "heard_pinyin",
                    "confidence",
                    "request_sha256",
                    "audio_clip_sha256",
                )
                if key in verdict
            }
            if isinstance(verdict, Mapping)
            else None
        ),
        "binary_judge": (
            {
                key: judge.get(key)
                for key in (
                    "choice",
                    "reason",
                    "ranking",
                    "prompt_sha256",
                    "completion_sha256",
                )
                if key in judge
            }
            if isinstance(judge, Mapping)
            else None
        ),
    }


def _cache_path(prompt_sha256: str) -> Path | None:
    base = os.environ.get("AUTOSLICE_BASE")
    if not base:
        return None
    return (
        Path(base)
        / "cache"
        / "exact-final-cpa-convergence"
        / prompt_sha256[:2]
        / f"{prompt_sha256}.json"
    )


def _load_or_call_judge(
    prompt: str,
    *,
    judge_llm_call: Callable[[str], str],
) -> tuple[Mapping[str, Any], str, bool]:
    prompt_sha256 = _text_sha256(prompt)
    path = _cache_path(prompt_sha256)
    if path is not None:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, Mapping)
                and cached.get("schema_version") == CACHE_SCHEMA
                and cached.get("prompt_sha256") == prompt_sha256
                and isinstance(cached.get("completion_sha256"), str)
                and isinstance(cached.get("payload"), Mapping)
            ):
                return (
                    cached["payload"],
                    str(cached["completion_sha256"]),
                    True,
                )
        except (OSError, ValueError):
            pass
    completion = judge_llm_call(prompt)
    completion_sha256 = _text_sha256(completion)
    payload = extract_json_object(completion)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
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
    return payload, completion_sha256, False


def _converge_one(
    srt_text: str,
    finding: Mapping[str, Any],
    *,
    history: list[dict[str, Any]],
    judge_llm_call: Callable[[str], str],
) -> tuple[dict[str, Any], bool] | None:
    adjudication = finding.get("exact_release_adjudication")
    if not isinstance(adjudication, Mapping):
        return None
    request = adjudication.get("request")
    witness_judge = adjudication.get("witness_judge")
    binary_judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
    mutation = adjudication.get("mutation_authority")
    if not (
        adjudication.get("schema_version") == "subtitle-span-adjudication.v1"
        and adjudication.get("status") == "OBSERVED"
        and adjudication.get("decision_authority") == "CPA_JUDGE"
        and adjudication.get("repaired") is True
        and adjudication.get("timing_immutable") is True
        and isinstance(mutation, Mapping)
        and mutation.get("schema_version") == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
        and isinstance(request, Mapping)
        and request.get("schema_version") == "subtitle-span-acoustic-check-request.v1"
        and _valid_sha256(request.get("request_sha256"))
        and isinstance(binary_judge, Mapping)
        and binary_judge.get("status") == "JUDGED"
        and binary_judge.get("choice") == "PROPOSED"
    ):
        return None
    try:
        cue_index = int(finding.get("cue_index") or 0)
    except (TypeError, ValueError):
        return None
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not 1 <= cue_index <= len(cues):
        return None
    cue = cues[cue_index - 1]
    current = cue.text
    proposed = request.get("proposed_cue")
    current_sha256 = _text_sha256(current)
    if not (
        isinstance(proposed, str)
        and proposed.strip()
        and proposed != current
        and request.get("current_cue") == current
        and request.get("base_text_sha256") == current_sha256
        and finding.get("base_text_sha256") == current_sha256
        and request.get("matched_start_ms") == cue.start_ms
        and request.get("matched_end_ms") == cue.end_ms
        and history
        and history[-1].get("after_sha256") == "sha256:" + current_sha256
    ):
        return None
    compact_history = _compact_history(history)
    current_evidence = _compact_current_evidence(finding, adjudication)
    context_sha256, before, after = _local_context_binding(srt_text, cue_index)
    history_sha256 = _json_sha256(compact_history)
    evidence_sha256 = _json_sha256(current_evidence)
    srt_sha256 = _text_sha256(srt_text)
    proposed_sha256 = _text_sha256(proposed)
    prompt = _PROMPT.format(
        srt_sha256=srt_sha256,
        cue_index=cue_index,
        start_ms=cue.start_ms,
        end_ms=cue.end_ms,
        current_sha256=current_sha256,
        proposed_sha256=proposed_sha256,
        context_sha256=context_sha256,
        history_sha256=history_sha256,
        evidence_sha256=evidence_sha256,
        current=current,
        proposed=proposed,
        before="\n".join(before) or "（无）",
        after="\n".join(after) or "（无）",
        history_json=json.dumps(compact_history, ensure_ascii=False, sort_keys=True),
        evidence_json=json.dumps(current_evidence, ensure_ascii=False, sort_keys=True),
    )
    prompt_sha256 = _text_sha256(prompt)
    try:
        payload, completion_sha256, served_from_cache = _load_or_call_judge(
            prompt,
            judge_llm_call=judge_llm_call,
        )
    except Exception:
        return None
    choice = str(payload.get("choice") or "").strip().upper()
    reason = str(payload.get("reason") or "")[:240]
    if choice not in {"CURRENT", "PROPOSED"} or not reason:
        return None
    final_text = current if choice == "CURRENT" else proposed
    bound_context_sha256, _before, _after = _local_context_binding(
        srt_text,
        cue_index,
        target_text=final_text,
    )
    memo = {
        "schema_version": MEMO_SCHEMA,
        "status": "RESOLVED",
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
        "choice": choice,
        "final_text": final_text,
        "final_text_sha256": "sha256:" + _text_sha256(final_text),
        "cue_index": cue_index,
        "matched_start_ms": cue.start_ms,
        "matched_end_ms": cue.end_ms,
        "origin_srt_sha256": "sha256:" + srt_sha256,
        "origin_current_cue_sha256": "sha256:" + current_sha256,
        "origin_proposed_cue_sha256": "sha256:" + proposed_sha256,
        "origin_local_context_sha256": "sha256:" + context_sha256,
        "bound_local_context_sha256": "sha256:" + bound_context_sha256,
        "history_sha256": "sha256:" + history_sha256,
        "new_evidence_sha256": "sha256:" + evidence_sha256,
        "new_evidence": current_evidence,
        "origin_request_sha256": "sha256:"
        + str(request.get("request_sha256") or "").removeprefix("sha256:"),
        "history_receipts": compact_history,
        "prompt_sha256": "sha256:" + prompt_sha256,
        "completion_sha256": "sha256:" + completion_sha256,
        "reason": reason,
        "timing_immutable": True,
    }
    if served_from_cache:
        memo["served_from_cache"] = True
    row = dict(finding)
    final_adjudication = dict(adjudication)
    final_adjudication["pre_convergence_adjudication"] = {
        "policy_branch": adjudication.get("policy_branch"),
        "witness_judge": adjudication.get("witness_judge"),
        "mutation_authority": adjudication.get("mutation_authority"),
    }
    final_adjudication["exact_final_cpa_convergence_memo"] = memo
    final_adjudication["witness_judge"] = {
        "witness_status": "HISTORY_AND_CURRENT_EVIDENCE_BOUND",
        "decision_authority": "CPA_JUDGE",
        "judge": {
            "schema_version": "exact-final-cpa-convergence-judge.v1",
            "status": "JUDGED",
            "choice": choice,
            "reason": reason,
            "prompt_sha256": "sha256:" + prompt_sha256,
            "completion_sha256": "sha256:" + completion_sha256,
        },
    }
    if choice == "CURRENT":
        final_adjudication.update(
            repaired=False,
            policy_branch="CPA_HISTORY_CONVERGENCE_KEEP_CURRENT",
            mutation_authority={
                "schema_version": ("subtitle-correction-mutation-authority.v1"),
                "status": "NOT_APPLIED",
                "basis": "CPA_HISTORY_CONVERGENCE_KEEP_CURRENT",
            },
        )
        row["resolution"] = "CPA_HISTORY_CONVERGENCE_KEEP_CURRENT"
    else:
        final_adjudication.update(
            repaired=True,
            policy_branch="CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED",
            mutation_authority={
                "schema_version": ("subtitle-correction-mutation-authority.v1"),
                "status": "PASS",
                "basis": "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED",
            },
        )
    row["exact_release_adjudication"] = final_adjudication
    return row, choice == "CURRENT"


def converge_reconsidered_exact_final_findings(
    srt_text: str,
    findings: Iterable[Mapping[str, Any]],
    *,
    authority_audit: Mapping[str, object],
    judge_llm_call: Callable[[str], str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give CPA the prior receipt chain before accepting another cue rewrite."""

    rows = [dict(row) for row in findings]
    if judge_llm_call is None:
        return rows, []
    histories = _repair_histories(authority_audit)
    unresolved: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for row in rows:
        adjudication = row.get("exact_release_adjudication")
        request = adjudication.get("request") if isinstance(adjudication, Mapping) else None
        key = (
            (
                int(request["matched_start_ms"]),
                int(request["matched_end_ms"]),
            )
            if isinstance(request, Mapping)
            and isinstance(request.get("matched_start_ms"), int)
            and not isinstance(request.get("matched_start_ms"), bool)
            and isinstance(request.get("matched_end_ms"), int)
            and not isinstance(request.get("matched_end_ms"), bool)
            else None
        )
        converged = (
            _converge_one(
                srt_text,
                row,
                history=histories.get(key, []),
                judge_llm_call=judge_llm_call,
            )
            if key is not None
            else None
        )
        if converged is None:
            unresolved.append(row)
            continue
        final_row, keep_current = converged
        (resolved if keep_current else unresolved).append(final_row)
    return unresolved, resolved


def _valid_memo(memo: object) -> bool:
    if not isinstance(memo, Mapping):
        return False
    final_text = memo.get("final_text")
    history = memo.get("history_receipts")
    new_evidence = memo.get("new_evidence")
    basic = bool(
        memo.get("schema_version") == MEMO_SCHEMA
        and memo.get("status") == "RESOLVED"
        and memo.get("decision_authority") == "CPA_JUDGE"
        and memo.get("witness_authority") == "EVIDENCE_ONLY"
        and memo.get("choice") in {"CURRENT", "PROPOSED"}
        and isinstance(final_text, str)
        and bool(final_text.strip())
        and memo.get("final_text_sha256") == "sha256:" + _text_sha256(final_text)
        and isinstance(memo.get("matched_start_ms"), int)
        and not isinstance(memo.get("matched_start_ms"), bool)
        and isinstance(memo.get("matched_end_ms"), int)
        and not isinstance(memo.get("matched_end_ms"), bool)
        and int(memo["matched_start_ms"]) < int(memo["matched_end_ms"])
        and _valid_sha256(memo.get("origin_srt_sha256"))
        and _valid_sha256(memo.get("origin_current_cue_sha256"))
        and _valid_sha256(memo.get("origin_proposed_cue_sha256"))
        and _valid_sha256(memo.get("origin_local_context_sha256"))
        and _valid_sha256(memo.get("bound_local_context_sha256"))
        and _valid_sha256(memo.get("history_sha256"))
        and _valid_sha256(memo.get("new_evidence_sha256"))
        and _valid_sha256(memo.get("origin_request_sha256"))
        and _valid_sha256(memo.get("prompt_sha256"))
        and _valid_sha256(memo.get("completion_sha256"))
        and isinstance(history, list)
        and bool(history)
        and isinstance(new_evidence, Mapping)
        and memo.get("timing_immutable") is True
    )
    if not basic:
        return False
    valid_history = all(
        isinstance(row, Mapping)
        and isinstance(row.get("before"), str)
        and isinstance(row.get("after"), str)
        and row.get("before_sha256") == "sha256:" + _text_sha256(str(row["before"]))
        and row.get("after_sha256") == "sha256:" + _text_sha256(str(row["after"]))
        and _valid_sha256(row.get("finding_sha256"))
        and _valid_sha256(row.get("request_sha256"))
        for row in history
    )
    return bool(
        valid_history
        and memo.get("history_sha256") == "sha256:" + _json_sha256(history)
        and memo.get("new_evidence_sha256") == "sha256:" + _json_sha256(new_evidence)
    )


def collect_exact_final_convergence_memos(
    audit: Mapping[str, object],
) -> list[dict[str, Any]]:
    """Collect typed memos from current unresolved and resolved audit rows."""

    by_window: dict[tuple[int, int], dict[str, Any]] = {}
    for bucket in (
        audit.get("findings"),
        audit.get("resolved_findings"),
        audit.get("unresolved_findings_disclosed"),
    ):
        for row in bucket if isinstance(bucket, list) else []:
            adjudication = (
                row.get("exact_release_adjudication") if isinstance(row, Mapping) else None
            )
            memo = (
                adjudication.get("exact_final_cpa_convergence_memo")
                if isinstance(adjudication, Mapping)
                else None
            )
            if not _valid_memo(memo):
                continue
            key = (
                int(memo["matched_start_ms"]),
                int(memo["matched_end_ms"]),
            )
            by_window[key] = dict(memo)
    return list(by_window.values())


def rebind_exact_final_convergence_memos(
    srt_text: str,
    memos: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Advance valid memos across other CPA-authorized repairs in one pass.

    The convergence judge runs before the producer atomically applies every
    repair from that audit.  A neighboring cue can therefore change in the
    same output and legitimately alter the local-context hash.  Rebind only
    when the memo's own exact text and time window survive; record both hashes
    and the complete output SRT hash so this is auditable, not a loose remap.
    """

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    output_srt_sha256 = "sha256:" + _text_sha256(srt_text)
    rebound: list[dict[str, Any]] = []
    for raw in memos:
        if not _valid_memo(raw):
            continue
        memo = dict(raw)
        matches = [
            (index, cue)
            for index, cue in enumerate(cues, start=1)
            if cue.start_ms == memo["matched_start_ms"]
            and cue.end_ms == memo["matched_end_ms"]
            and cue.text == memo["final_text"]
        ]
        if len(matches) != 1:
            rebound.append(memo)
            continue
        cue_index, _cue = matches[0]
        context_sha256, _before, _after = _local_context_binding(srt_text, cue_index)
        after_binding = "sha256:" + context_sha256
        before_binding = str(memo["bound_local_context_sha256"])
        if before_binding != after_binding:
            history = memo.get("context_rebindings")
            history_rows = list(history) if isinstance(history, list) else []
            history_rows.append(
                {
                    "schema_version": ("exact-final-cpa-convergence-context-rebind.v1"),
                    "status": "PASS",
                    "basis": ("SAME_PASS_CPA_AUTHORIZED_REPAIRS_FINAL_OUTPUT"),
                    "before_local_context_sha256": before_binding,
                    "after_local_context_sha256": after_binding,
                    "output_srt_sha256": output_srt_sha256,
                    "own_final_text_sha256": memo["final_text_sha256"],
                    "timing_immutable": True,
                }
            )
            memo["context_rebindings"] = history_rows
            memo["bound_local_context_sha256"] = after_binding
        memo["bound_output_srt_sha256"] = output_srt_sha256
        rebound.append(memo)
    return rebound


def resolve_findings_from_exact_final_convergence_memos(
    srt_text: str,
    findings: Iterable[Mapping[str, Any]],
    *,
    authority_audit: Mapping[str, object],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Close repeated scans only while the memo's exact local context survives."""

    memos = authority_audit.get("exact_final_cpa_convergence_memos")
    valid_memos = [
        dict(memo) for memo in (memos if isinstance(memos, list) else []) if _valid_memo(memo)
    ]
    if not valid_memos:
        return [dict(row) for row in findings], []
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    pending: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for finding in findings:
        try:
            cue_index = int(finding.get("cue_index") or 0)
        except (TypeError, ValueError):
            cue_index = 0
        if not 1 <= cue_index <= len(cues):
            pending.append(dict(finding))
            continue
        cue = cues[cue_index - 1]
        context_sha256, _before, _after = _local_context_binding(srt_text, cue_index)
        matched = next(
            (
                memo
                for memo in reversed(valid_memos)
                if memo["matched_start_ms"] == cue.start_ms
                and memo["matched_end_ms"] == cue.end_ms
                and memo["final_text"] == cue.text
                and memo["final_text_sha256"] == "sha256:" + _text_sha256(cue.text)
                and memo["bound_local_context_sha256"] == "sha256:" + context_sha256
            ),
            None,
        )
        if matched is None:
            pending.append(dict(finding))
            continue
        row = dict(finding)
        memo_request = {
            "schema_version": ("exact-final-cpa-convergence-memo-replay.v1"),
            "cue_index": cue_index,
            "matched_start_ms": cue.start_ms,
            "matched_end_ms": cue.end_ms,
            "current_cue_sha256": _text_sha256(cue.text),
            "bound_local_context_sha256": context_sha256,
            "memo_prompt_sha256": matched["prompt_sha256"],
            "timing_immutable": True,
        }
        memo_request["request_sha256"] = _json_sha256(memo_request)
        row["resolution"] = "CPA_HISTORY_CONVERGENCE_MEMO_FINAL_TEXT"
        row["exact_release_adjudication"] = {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "repaired": False,
            "policy_branch": "CPA_HISTORY_CONVERGENCE_MEMO_KEEP_FINAL",
            "timing_immutable": True,
            "request": memo_request,
            "verdict": {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "status": "NOT_REQUIRED",
                "target_audible": None,
                "reason_code": "CPA_HISTORY_CONVERGENCE_MEMO",
            },
            "witness_judge": {
                "witness_status": "MEMO_REPLAY",
                "judge": {
                    "schema_version": ("exact-final-cpa-convergence-judge.v1"),
                    "status": "JUDGED",
                    "choice": "CURRENT",
                    "reason": matched.get("reason"),
                    "prompt_sha256": matched.get("prompt_sha256"),
                    "completion_sha256": matched.get("completion_sha256"),
                },
            },
            "exact_final_cpa_convergence_memo": matched,
            "decision_authority": "CPA_JUDGE",
            "witness_authority": "EVIDENCE_ONLY",
            "mutation_authority": {
                "schema_version": ("subtitle-correction-mutation-authority.v1"),
                "status": "NOT_APPLIED",
                "basis": "CPA_HISTORY_CONVERGENCE_MEMO_KEEP_FINAL",
            },
        }
        resolved.append(row)
    return pending, resolved
