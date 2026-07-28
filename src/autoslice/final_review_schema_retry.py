"""Bounded CPA retry for structurally invalid final-review findings."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable


_SCHEMA_REPAIR_INSTRUCTION = """

上一轮返回了非空 findings，但每一条都因合同字段无效而被机器拒绝。请重新
独立审查同一份字幕，并严格遵守：
- cue 必须是上方真实存在的编号；
- suspect 若填写，必须逐字出现在该 cue；
- 有修正方案时 proposed_full_cue 必须是该 cue 的完整修正版；
- 没有合同合法且确有把握的疑点时，明确输出 {"findings": []}。
不得复述或猜测上一轮内容，只输出一个新的 JSON 对象。
"""


def schema_repair_prompt(prompt: str) -> str:
    """Append output-contract feedback without changing review evidence."""

    return prompt + _SCHEMA_REPAIR_INSTRUCTION


def retry_invalid_finding_schema_once(
    call_once: Callable[..., Any],
) -> Callable[..., Any]:
    """Retry only ALL_INVALID once; every other failure propagates unchanged."""

    @wraps(call_once)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("_schema_repair_retry", None)
        try:
            return call_once(*args, **kwargs, _schema_repair_retry=False)
        except Exception as exc:
            if (
                getattr(exc, "reason_code", None)
                != "FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID"
            ):
                raise
            return call_once(*args, **kwargs, _schema_repair_retry=True)

    return wrapped
