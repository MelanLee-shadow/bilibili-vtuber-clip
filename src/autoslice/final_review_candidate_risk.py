"""Keep content-risk decisions separate from replacement eligibility."""

from __future__ import annotations

from src.autoslice.structured_output import call_json

import hashlib
import json
import re
from collections.abc import Mapping

RISK_PROMPT_MARKER = "CANDIDATE_CONTENT_RISK_REVIEW_V2_MEANING_EQUIVALENCE"
_FIELDS = (
    "cue_index", "base_text_sha256", "kind", "suspect", "suggestion",
    "proposed_full_cue", "repair_class", "candidate_provenance",
    "evidence_cue_ids", "suggestion_rejected_reason",
    "candidate_content_risk_required",
)


def _digest(value) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def candidate_question_sha256(row: Mapping) -> str:
    return _digest({key: row.get(key) for key in _FIELDS})


def needs_candidate_content_review(row: object) -> bool:
    if not isinstance(row, Mapping):
        return False
    rejected = str(row.get("suggestion_rejected_reason") or "")
    provenance = row.get("candidate_provenance")
    return bool(
        rejected.startswith(("ENTITY_", "LATIN_SCRIPT_REPAIR_"))
        or (row.get("repair_class") == "source_backed_entity"
            and isinstance(provenance, Mapping))
        or (row.get("kind") in {"self_ref", "entity"}
            and row.get("proposed_full_cue") is not None
            and isinstance(provenance, Mapping))
    )


def candidate_risk_assessment_valid(row: Mapping, *, srt_sha256: str | None = None) -> bool:
    from src.autoslice.final_review_meaning_policy import POLICY_VERSION, current_meaning_equivalent
    assessment = row.get("candidate_content_risk")
    if not isinstance(assessment, Mapping):
        return False
    return bool(
        assessment.get("schema_version") == "candidate-content-risk.v2"
        and assessment.get("meaning_policy") == POLICY_VERSION
        and assessment.get("status") == "COMPLETE"
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(assessment.get("reviewed_srt_sha256") or ""))
        and (srt_sha256 is None or assessment["reviewed_srt_sha256"] == srt_sha256)
        and assessment.get("cue_index") == row.get("cue_index")
        and assessment.get("base_text_sha256") == row.get("base_text_sha256")
        and assessment.get("question_sha256") == candidate_question_sha256(row)
        and assessment.get("content_risk") == row.get("content_risk")
        and assessment.get("risk_basis") == row.get("risk_basis")
        and assessment.get("content_effect") == {
            "noncritical_uncertainty": "context_supported_meaning_equivalence",
            "meaning_reversal": "concrete_meaning_or_fact_conflict",
            "factual_error": "concrete_meaning_or_fact_conflict",
            "potential_harm": "potential_harm",
        }.get(row.get("content_risk"), "INVALID")
        and isinstance(assessment.get("evidence_cue_ids"), list)
        and assessment["evidence_cue_ids"]
        and (row.get("content_risk") != "noncritical_uncertainty"
             or current_meaning_equivalent(row, expected_srt_sha256=srt_sha256))
    )


def candidate_risk_parser_successor(previous, current) -> bool:
    """Only recompile bound source-class normalization and new risk decisions."""
    if not isinstance(previous, list) or len(previous) != len(current):
        return False
    for old, new in zip(previous, current, strict=True):
        if old == new:
            continue
        if not isinstance(old, Mapping) or not candidate_risk_assessment_valid(new):
            return False
        ignored = {"candidate_content_risk", "candidate_content_risk_required", "content_risk", "risk_basis", "current_content_risk"}
        inference = new.get("source_surface_inference") or {}
        if inference.get("basis") == "provided_surface_witnessed_in_cited_source_cue":
            if not str(old.get("suggestion_rejected_reason") or "").startswith(("ENTITY_", "LATIN_SCRIPT_REPAIR_")):
                return False
            ignored |= {"repair_class", "candidate_provenance", "force_acoustic",
                        "correlated_text_witness", "suggestion", "proposed_full_cue",
                        "suggestion_rejected_reason", "source_surface_inference"}
        if ({k: v for k, v in old.items() if k not in ignored}
                != {k: v for k, v in new.items() if k not in ignored}):
            return False
    return True


def unreviewed_postburn_candidate_cues(record, *, srt_sha256=None):
    from src.autoslice.final_review_contract import pending_noncritical_repair
    review = record.get("postburn_subtitle_audio_review") if isinstance(record, Mapping) else None
    if not isinstance(review, Mapping):
        return []
    return sorted({row.get("cue_index") for row in review.get("discovered_findings", [])
                   if isinstance(row, Mapping) and type(row.get("cue_index")) is int
                   and (row.get("content_risk") in {"meaning_reversal", "factual_error", "potential_harm"}
                        or pending_noncritical_repair(row)
                        or (needs_candidate_content_review(row)
                            and not candidate_risk_assessment_valid(row, srt_sha256=srt_sha256)))})


def complete_candidate_content_risks(findings, *, srt_text, cues, prompt, llm_call, extract_json):
    from src.autoslice.final_review_meaning_policy import (
        POLICY_VERSION, MEANING_POLICY_PROMPT, equivalence_supported, bind_current_risk,
    )
    pending = [row for row in findings
               if row.get("content_risk") == "noncritical_uncertainty"
               and needs_candidate_content_review(row)]
    if not pending:
        return
    for row in pending:
        # Set by the current compiler, never copied from untrusted model JSON.
        row["candidate_content_risk_required"] = True
    agenda = [{**{key: row.get(key) for key in _FIELDS},
               "current_cue": cues[row["cue_index"] - 1].text,
               "prior_risk_basis": row.get("risk_basis")}
              for row in pending]
    question = prompt + "\n" + RISK_PROMPT_MARKER + "\n" + (
        "只复核下列当前句的内容风险，不重新发现、不改字、不重新听音。候选拼写缺来源、"
        "候选被拒和声音尚未确定，都不证明当前事实正确。分别判断当前文本是否已经把"
        "同一主体/对象换成另一人或物、改变/破坏原意、引入具体错误事实或伤害。"
        "同一主体连续自述且没有第三者引入是具体指代证据；合法第三者、昵称、引用、"
        "口头别名和未知专名不能自动判严重；非关键须说明当前整句及不确定细节的另一解释为何句意相同。"
        "只按实际提供的完整字幕、源观察及上下文判断；同稿重复只可提名候选，不能证明听到或授权改字。"
        "每个cue返回一次：{\"findings\":[{\"cue\":编号,\"content_risk\":"
        "\"meaning_reversal|factual_error|potential_harm|noncritical_uncertainty\","
        "\"content_effect\":\"concrete_meaning_or_fact_conflict|potential_harm|context_supported_meaning_equivalence\","
        "\"risk_basis\":\"具体影响与依据\",\"meaning_equivalence\":\"非关键时为完整对象，否则null\",\"evidence_cue_ids\":[实际字幕编号]}]}。"
        "明确的含义/事实冲突不能同时写成noncritical_uncertainty；找不到可靠替换只代表未决。\n"
    ) + MEANING_POLICY_PROMPT + json.dumps(agenda, ensure_ascii=False, sort_keys=True)
    response = extract_json(call_json(llm_call, question))
    rows = response.get("findings") if isinstance(response, Mapping) else None
    expected = {row["cue_index"] for row in pending}
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("CANDIDATE_CONTENT_RISK_REVIEW_INCOMPLETE")
    decisions = {}
    for decision in rows:
        if not isinstance(decision, Mapping):
            raise ValueError("CANDIDATE_CONTENT_RISK_REVIEW_INVALID")
        cue = decision.get("cue")
        risk, effect = decision.get("content_risk"), decision.get("content_effect")
        basis, evidence = decision.get("risk_basis"), decision.get("evidence_cue_ids")
        effects = {"meaning_reversal": "concrete_meaning_or_fact_conflict",
                   "factual_error": "concrete_meaning_or_fact_conflict",
                   "potential_harm": "potential_harm",
                   "noncritical_uncertainty": "context_supported_meaning_equivalence"}
        if (type(cue) is not int or cue not in expected or cue in decisions
                or risk not in effects or effect != effects[risk]
                or not isinstance(basis, str) or not basis.strip()
                or not isinstance(evidence, list) or not evidence
                or any(type(index) is not int or not 1 <= index <= len(cues) for index in evidence)):
            raise ValueError("CANDIDATE_CONTENT_RISK_REVIEW_INVALID")
        if risk == "noncritical_uncertainty" and not equivalence_supported(
                decision.get("meaning_equivalence"), cue_count=len(cues)):
            raise ValueError("CANDIDATE_MEANING_EQUIVALENCE_REQUIRED")
        decisions[cue] = decision
    for row in pending:
        cue = row["cue_index"]
        decision = decisions[cue]
        row.update(content_risk=decision["content_risk"], risk_basis=decision["risk_basis"])
        row["candidate_content_risk"] = {
            "schema_version": "candidate-content-risk.v2", "status": "COMPLETE",
            "meaning_policy": POLICY_VERSION,
            "reviewed_srt_sha256": "sha256:" + hashlib.sha256(srt_text.encode()).hexdigest(),
            "cue_index": cue, "base_text_sha256": row["base_text_sha256"],
            "question_sha256": candidate_question_sha256(row),
            "content_risk": decision["content_risk"], "risk_basis": decision["risk_basis"],
            "content_effect": decision["content_effect"],
            "evidence_cue_ids": list(decision["evidence_cue_ids"]),
        }
        row["current_content_risk"] = bind_current_risk(
            row, srt_text=srt_text, cue_count=len(cues), prompt=question,
            meaning_equivalence=decision.get("meaning_equivalence"),
        )
