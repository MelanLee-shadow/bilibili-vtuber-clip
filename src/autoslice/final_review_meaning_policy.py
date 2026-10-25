"""Current contextual meaning is the authority for uncertainty disclosure."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from collections.abc import Mapping

POLICY_VERSION = "contextual-meaning-equivalence.v1"
ASSESSMENT_SCHEMA = "subtitle-current-content-risk.v2"
MEANING_POLICY_PROMPT = """
CURRENT_MEANING_POLICY: contextual-meaning-equivalence.v1
meaning_reversal 为兼容旧字段名，现包括改变或破坏原意（不只反义反转）：命题、施受关系、
指代、主题术语、否定及人物/声源归属发生实质改变，或现文在语境中失去可解释句意，均属关键内容问题。
无可靠替换、没有继续计划、听不清、没有事实指控或未证明伤害，都不能证明现文无风险。
只有当前整句在完整上下文中有具体依据支持句意等价，才用 noncritical_uncertainty 保留披露。
例如“喝口水/喝一口水”可表达同一请求；不可把此结论推广到语义损坏的同音词、关系或术语。
合法口语、口吃和未知专名不自动关键；须说明该不确定细节为什么不改变当前所指和整句含义，不能仅说未知。
每个 noncritical_uncertainty 必须同时给 meaning_equivalence 对象：
{"status":"EQUIVALENT","current_meaning":"当前整句表达什么", "alternative_meaning":"不确定细节的另一解释表达什么",
"shared_meaning":"两者保持不变的具体命题/关系", "uncertain_detail":"只是哪一细节未确定",
"context_basis":"实际整片/邻句/所给源证据怎样支持句意相同", "evidence_cue_ids":[实际相关cue编号]}。
有意义改变时不能同时报 EQUIVALENT；不能说明共同含义时保持未决/关键疑点，不用没有候选作等价理由。
此判断不授权改字，不把模型推断或缺音频冒充已听见，不按通顺偏好润色。
"""
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_QUESTION_FIELDS = ("cue_index", "base_text_sha256", "repair_class", "proposed_full_cue",
                    "source_surface", "candidate_provenance", "evidence_cue_ids", "continuation_plan")


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def equivalence_supported(value, *, cue_count: int) -> bool:
    return bool(isinstance(value, Mapping) and value.get("status") == "EQUIVALENT"
        and all(isinstance(value.get(k), str) and value[k].strip() for k in (
            "current_meaning", "alternative_meaning", "shared_meaning", "uncertain_detail", "context_basis"))
        and isinstance(value.get("evidence_cue_ids"), list) and value["evidence_cue_ids"]
        and all(type(i) is int and 1 <= i <= cue_count for i in value["evidence_cue_ids"]))


def bind_current_risk(row: Mapping, *, srt_text: str, cue_count: int,
                      prompt: str, meaning_equivalence=None) -> dict:
    """Compile only a current review's actual answer, never an old scalar."""
    context_sha = "sha256:" + hashlib.sha256(prompt.encode()).hexdigest()
    return {"schema_version": ASSESSMENT_SCHEMA, "status": "COMPLETE",
        "policy_version": POLICY_VERSION, "decision_authority": "CURRENT_FINAL_REVIEW",
        "reviewed_srt_sha256": "sha256:" + hashlib.sha256(srt_text.encode()).hexdigest(),
        "reviewed_cue_count": cue_count, "cue_index": row["cue_index"],
        "base_text_sha256": row["base_text_sha256"],
        "question_sha256": digest({**{k: row.get(k) for k in _QUESTION_FIELDS},
                                   "review_context_sha256": context_sha}),
        "review_context_sha256": context_sha,
        "content_risk": row.get("content_risk"), "risk_basis": row.get("risk_basis"),
        "meaning_equivalence": deepcopy(meaning_equivalence)}


def current_meaning_equivalent(row, *, expected_srt_sha256: str | None = None) -> bool:
    if not isinstance(row, Mapping) or row.get("content_risk") != "noncritical_uncertainty":
        return False
    value = row.get("current_content_risk")
    if not isinstance(value, Mapping):
        return False
    try:
        count = value.get("reviewed_cue_count")
        return bool(value.get("schema_version") == ASSESSMENT_SCHEMA
            and value.get("policy_version") == POLICY_VERSION and value.get("status") == "COMPLETE"
            and value.get("decision_authority") == "CURRENT_FINAL_REVIEW"
            and _SHA.fullmatch(str(value.get("reviewed_srt_sha256") or ""))
            and (expected_srt_sha256 is None or value["reviewed_srt_sha256"] == expected_srt_sha256)
            and _SHA.fullmatch(str(value.get("review_context_sha256") or ""))
            and type(count) is int and count > 0
            and type(row.get("cue_index")) is int and 1 <= row["cue_index"] <= count
            and value.get("cue_index") == row["cue_index"]
            and re.fullmatch(r"[0-9a-f]{64}", str(row.get("base_text_sha256") or ""))
            and value.get("base_text_sha256") == row["base_text_sha256"]
            and value.get("question_sha256") == digest({**{k: row.get(k) for k in _QUESTION_FIELDS},
                "review_context_sha256": value.get("review_context_sha256")})
            and value.get("content_risk") == row["content_risk"]
            and isinstance(row.get("risk_basis"), str) and row["risk_basis"].strip()
            and value.get("risk_basis") == row["risk_basis"]
            and equivalence_supported(value.get("meaning_equivalence"), cue_count=count))
    except (TypeError, ValueError, KeyError):
        return False
