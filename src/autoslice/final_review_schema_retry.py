"""Bounded CPA retry for structurally invalid final-review findings."""

from __future__ import annotations

import json
from functools import wraps
from typing import Any, Callable, Mapping, Sequence


_SCHEMA_REPAIR_INSTRUCTION = """

上一轮返回了非空 findings，但每一条都因合同字段无效而被机器拒绝。请重新
处理下方机器诊断中列出的这些原 finding：
{diagnostics}

严格遵守：
- 这是合同纠错，**不是新一轮全片审查**；只能修复上方已列出
  finding 或者将它撤回，禁止发现/新增其他 cue；
- cue 必须是上方真实存在的编号；
- suspect 若填写，必须逐字出现在该 cue；
- 有修正方案时 proposed_full_cue 必须是该 cue 的完整修正版；
- 不得只返回 cue/kind/why。认为该句确有错误，就必须给可验证的
  proposed_full_cue；无法给出修正版，就删除该 finding；
- 若上方所有原 finding 都无法给出合同合法且有把握的改法，
  明确输出 {{"findings": []}}，这表示撤回这些非可执行疑点。
不要为了保留上一轮意见而猜测，只输出一个替换上轮 invalid
rows 的 JSON 对象。
"""


def schema_repair_prompt(prompt: str, diagnostics: str = "") -> str:
    """Append output-contract feedback without changing review evidence."""

    detail = diagnostics.strip()[:4_000] or "（无可用诊断；重新严格检查合同）"
    return prompt + _SCHEMA_REPAIR_INSTRUCTION.format(diagnostics=detail)


def schema_repair_allowed_cues(diagnostics: str) -> set[int]:
    """Return the original invalid cue set bound into one repair retry."""

    try:
        payload = json.loads(diagnostics)
    except (TypeError, ValueError):
        return set()
    rows = payload.get("invalid_rows") if isinstance(payload, Mapping) else None
    allowed: set[int] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        try:
            allowed.add(int(row.get("cue")))
        except (TypeError, ValueError):
            continue
    return allowed


def schema_repair_new_finding_diagnostic(
    allowed_cues: set[int] | None,
    cue_index: int,
) -> dict[str, object] | None:
    """Reject a retry row that was not one of the original invalid rows."""

    if allowed_cues is None or cue_index in allowed_cues:
        return None
    return {
        "reason": "SCHEMA_REPAIR_NEW_FINDING_FORBIDDEN",
        "cue": cue_index,
        "allowed_cues": sorted(allowed_cues),
    }


def detailed_invalid_finding_diagnostics(
    *,
    raw: Sequence[object],
    invalid_rows: Sequence[Mapping[str, Any]],
    cue_texts: Sequence[str],
    max_rows: int,
) -> str:
    """Bind the retry to exact rejected rows and their current cue text."""

    detailed: list[dict[str, Any]] = []
    # When no finding survived, every raw row produced one terminal invalid
    # diagnostic, so both sequences retain row order.
    for position, invalid in enumerate(invalid_rows[:max_rows]):
        detail = dict(invalid)
        source_row = raw[position] if position < len(raw) else None
        if isinstance(source_row, Mapping):
            detail["returned_row"] = {
                key: source_row.get(key)
                for key in (
                    "cue",
                    "cue_index",
                    "kind",
                    "proposed_full_cue",
                    "repair_class",
                    "source_surface",
                    "candidate_memory_id",
                    "evidence_cue_ids",
                    "suspect",
                    "replacement",
                    "why",
                )
                if key in source_row
            }
        try:
            cue_index = int(detail.get("cue"))
        except (TypeError, ValueError):
            cue_index = 0
        if 1 <= cue_index <= len(cue_texts):
            detail["current_cue"] = cue_texts[cue_index - 1]
        detailed.append(detail)
    return json.dumps(
        {"raw_count": len(raw), "invalid_rows": detailed},
        ensure_ascii=False,
        sort_keys=True,
    )


def retry_invalid_finding_schema_once(
    call_once: Callable[..., Any],
) -> Callable[..., Any]:
    """Retry only ALL_INVALID once; every other failure propagates unchanged."""

    @wraps(call_once)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("_schema_repair_retry", None)
        kwargs.pop("_schema_repair_detail", None)
        try:
            return call_once(*args, **kwargs, _schema_repair_retry=False)
        except Exception as exc:
            if (
                getattr(exc, "reason_code", None)
                != "FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID"
            ):
                raise
            return call_once(
                *args,
                **kwargs,
                _schema_repair_retry=True,
                _schema_repair_detail=str(getattr(exc, "detail", "")),
            )

    return wrapped
