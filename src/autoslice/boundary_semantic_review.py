"""Separate semantic end review for talk-clip boundary resolution.

The selector supplies an interesting content anchor.  ASR cue ends and VAD
only describe the physical transcript/audio grid; neither proves that the
story is complete or that the following cue belongs to another topic.  This
module asks a separate review call to judge those semantic facts and validates
its recommendation against the immutable cue grid.  A separate call is not an
independent vote when selector and reviewer use the same model family; that
correlation is recorded explicitly.  Deterministic code still owns the cut and
fails closed on missing/ambiguous evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.autoslice.clip_context import MAX_PROMPT_CHARS


SCHEMA_VERSION = "talk-boundary-semantic-review.v1"
MAX_FORWARD_MS = 30_000
CONTEXT_CUES_EACH_SIDE = 8
MAX_VISIBLE_CUES = 128
MAX_VISIBLE_TEXT_CHARS = 16_000
NEXT_TOPIC_WITNESS_CUES = 2
SEMANTIC_LLM_INDEPENDENCE_GROUP = "cpa-gpt-5.6-semantic-family"


class BoundarySemanticReviewError(ValueError):
    """The semantic reviewer returned an unusable or unbound decision."""


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _scorecard_story_witness(scorecard: object) -> dict[str, object]:
    dimensions = scorecard.get("dimensions") if isinstance(scorecard, Mapping) else None
    self_contained = dimensions.get("self_contained") if isinstance(dimensions, Mapping) else None
    comedic_payoff = dimensions.get("comedic_payoff") if isinstance(dimensions, Mapping) else None
    valid = (
        isinstance(scorecard, Mapping)
        and scorecard.get("status") == "VALID"
        and isinstance(self_contained, int)
        and not isinstance(self_contained, bool)
        and isinstance(comedic_payoff, int)
        and not isinstance(comedic_payoff, bool)
        and self_contained >= 3
        and comedic_payoff >= 3
    )
    return {
        # The current selector and boundary reviewer both use the CPA
        # gpt-5.6 family.  They are two procedural stages but one correlated
        # semantic vote.  Never manufacture an ensemble by renaming calls.
        "independence_group": SEMANTIC_LLM_INDEPENDENCE_GROUP,
        "status": "PASS" if valid else "INSUFFICIENT",
        "self_contained": self_contained,
        "comedic_payoff": comedic_payoff,
    }


def _cue_rows(cues: Sequence[object]) -> list[dict[str, object]]:
    return [
        {
            "cue_index": index,
            "start_ms": int(getattr(cue, "start_ms")),
            "end_ms": int(getattr(cue, "end_ms")),
            "text": str(getattr(cue, "text") or "").strip(),
        }
        for index, cue in enumerate(cues, start=1)
        if str(getattr(cue, "text", "") or "").strip()
    ]


def cue_grid_sha256(cues: Sequence[object]) -> str:
    """Bind a semantic decision to the complete non-empty cue grid."""

    return _canonical_sha256(
        {
            "schema_version": "talk-boundary-cue-grid.v1",
            "cues": _cue_rows(cues),
        }
    )


def semantic_review_sha256(review: Mapping[str, object]) -> str:
    """Canonical digest for binding a later delivery review to source proof."""

    return _canonical_sha256(review)


def _valid_sha256(value: object) -> bool:
    text = str(value or "")
    return (
        text.startswith("sha256:")
        and len(text) == 71
        and all(char in "0123456789abcdef" for char in text[7:])
    )


def _source_separation_witness(
    review: Mapping[str, object] | None,
    *,
    source_final_start_ms: int | None,
    source_final_end_ms: int | None,
) -> dict[str, object] | None:
    """Compact a bound source-window review for a delivery-terminal rerun."""

    if review is None:
        return None
    binding = review.get("final_endpoint_binding")
    reasons: list[str] = []
    if review.get("status") != "PASS":
        reasons.append("SOURCE_BOUNDARY_REVIEW_NOT_PASS")
    if review.get("next_topic_separated") is not True:
        reasons.append("SOURCE_NEXT_TOPIC_SEPARATION_NOT_PROVEN")
    if review.get("next_topic_witness_valid") is not True:
        reasons.append("SOURCE_NEXT_TOPIC_WITNESS_INVALID")
    if not _valid_sha256(review.get("request_sha256")):
        reasons.append("SOURCE_BOUNDARY_REQUEST_BINDING_INVALID")
    if not _valid_sha256(review.get("cue_grid_sha256")):
        reasons.append("SOURCE_BOUNDARY_CUE_GRID_BINDING_INVALID")
    if (
        not isinstance(binding, Mapping)
        or binding.get("status") != "PASS"
    ):
        reasons.append("SOURCE_BOUNDARY_ENDPOINT_BINDING_INVALID")
        binding = {}
    if (
        isinstance(source_final_start_ms, bool)
        or not isinstance(source_final_start_ms, int)
        or isinstance(source_final_end_ms, bool)
        or not isinstance(source_final_end_ms, int)
        or source_final_end_ms <= source_final_start_ms
    ):
        reasons.append("SOURCE_DELIVERY_INTERVAL_INVALID")
    elif (
        binding.get("final_start_ms") != source_final_start_ms
        or binding.get("final_end_ms") != source_final_end_ms
    ):
        reasons.append("SOURCE_DELIVERY_INTERVAL_MISMATCH")
    return {
        "schema_version": "talk-boundary-source-separation-witness.v1",
        "status": "BLOCK" if reasons else "PASS",
        "source_review_sha256": semantic_review_sha256(review),
        "source_request_sha256": review.get("request_sha256"),
        "source_cue_grid_sha256": review.get("cue_grid_sha256"),
        "source_recommended_end_ms": review.get("recommended_end_ms"),
        "source_final_start_ms": source_final_start_ms,
        "source_final_end_ms": source_final_end_ms,
        "reason_codes": reasons,
    }


def _target_index(rows: Sequence[Mapping[str, object]], target_ms: int) -> int:
    if not rows:
        raise BoundarySemanticReviewError("BOUNDARY_SEMANTIC_REVIEW_NO_CUES")
    return min(
        range(len(rows)),
        key=lambda index: abs(int(rows[index]["end_ms"]) - target_ms),
    )


def _build_prompt(request: Mapping[str, object]) -> str:
    return f"""你是直播切片的单独边界终审。候选选择器已经给了一个内容锚点，但它不是最终切点。你与选择器若来自同一模型族，只算一个相关语义证人；不要把重复调用当独立投票。

你必须分别判断三个互不替代的事实：
1. syntax_complete：推荐结尾是否没有把一句话截在中间。ASR cue 结束不等于句法结束。
2. story_closed：切片承诺的故事、回答或包袱是否已经落地，不能只因有停顿就算完成。
3. next_topic_separated：推荐结尾之后是否已经进入下一条 SC、谢礼或另一个话题；VAD 连续说话不能证明仍是同一话题。

约束：
- 只可从给出的 cue_index 中选 recommended_end_cue_index；不得改写字幕。
- 可以从目标 cue 向后寻找，最多 {request["max_forward_ms"]}ms；不得提前删掉候选选择器已经圈定的内容。
- 若目标本身已闭环，即使后面无停顿继续说，也应选目标；若目标半句或包袱未落地，才向后选最早同时满足三项的 cue。
- 若请求带 PASS 的 terminal source separation witness，说明 source full-window 已证明 cut 后进入下一话题；此时 delivery 最后一条 cue 可用该 witness 证明 next_topic_separated，但 syntax/story 仍须按当前最终字幕重新判断。
- 结构化弹幕/SC 可证明话题触发或切换；长期记忆只能帮助理解指代，不能单独证明边界。
- 任一项无法证明就给 false，不要为了产片凑结论。

绑定请求 JSON：
{json.dumps(request, ensure_ascii=False, sort_keys=True)}

只输出 JSON：
{{"syntax_complete":bool,"story_closed":bool,"next_topic_separated":bool,
"recommended_end_cue_index":整数或null,"evidence_cue_indexes":[整数],
"same_topic_continues_after_target":bool,"needs_more_context":bool,
"reason_codes":[字符串],"summary":"一句中文结论"}}
"""


def review_talk_boundary_semantics(
    *,
    cues: Sequence[object],
    target_ms: int,
    candidate_id: str,
    selection_hook: str,
    selection_scorecard: object,
    structured_context: str,
    candidate_context: str,
    llm_call: Callable[[str], str],
    extract_json: Callable[[str], Any],
    max_forward_ms: int = MAX_FORWARD_MS,
    terminal_source_review: Mapping[str, object] | None = None,
    source_final_start_ms: int | None = None,
    source_final_end_ms: int | None = None,
) -> dict[str, object]:
    """Return a validated, cue-grid-bound semantic boundary decision."""

    if (
        isinstance(max_forward_ms, bool)
        or not isinstance(max_forward_ms, int)
        or not 1_000 <= max_forward_ms <= 60_000
    ):
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEMANTIC_REVIEW_FORWARD_CAP_INVALID"
        )
    rows = _cue_rows(cues)
    target_pos = _target_index(rows, target_ms)
    lo = max(0, target_pos - CONTEXT_CUES_EACH_SIDE)
    cap_end_ms = target_ms + max_forward_ms
    recommendation_hi = target_pos + 1
    while (
        recommendation_hi < len(rows)
        and int(rows[recommendation_hi]["end_ms"]) <= cap_end_ms
    ):
        recommendation_hi += 1
    witness_hi = min(
        len(rows),
        recommendation_hi + NEXT_TOPIC_WITNESS_CUES,
    )
    visible_rows = rows[lo:witness_hi]
    visible_chars = sum(len(str(row["text"])) for row in visible_rows)
    if (
        len(visible_rows) > MAX_VISIBLE_CUES
        or visible_chars > MAX_VISIBLE_TEXT_CHARS
    ):
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEMANTIC_REVIEW_CONTEXT_OVERFLOW"
        )
    if len(candidate_context) > MAX_PROMPT_CHARS:
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEMANTIC_REVIEW_CANDIDATE_CONTEXT_OVERFLOW"
        )
    source_separation_witness = _source_separation_witness(
        terminal_source_review,
        source_final_start_ms=source_final_start_ms,
        source_final_end_ms=source_final_end_ms,
    )
    request = {
        "schema_version": "talk-boundary-semantic-request.v1",
        "candidate_id": candidate_id,
        "target_ms": target_ms,
        "target_cue_index": rows[target_pos]["cue_index"],
        "selection_hook": selection_hook,
        "selector_story_witness": _scorecard_story_witness(selection_scorecard),
        "cues": visible_rows,
        "structured_context": structured_context[:12_000],
        "candidate_context": candidate_context,
        "max_forward_ms": max_forward_ms,
        "recommendation_cue_indexes": [
            int(row["cue_index"])
            for row in rows[target_pos:recommendation_hi]
        ],
        "next_topic_witness_cue_indexes": [
            int(row["cue_index"])
            for row in rows[recommendation_hi:witness_hi]
        ],
        "terminal_source_separation_witness": (
            source_separation_witness
        ),
    }
    request_sha256 = _canonical_sha256(request)
    try:
        payload = extract_json(llm_call(_build_prompt(request)))
    except Exception as exc:
        raise BoundarySemanticReviewError(
            f"BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:{type(exc).__name__}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise BoundarySemanticReviewError("BOUNDARY_SEMANTIC_REVIEW_NOT_OBJECT")

    booleans: dict[str, bool] = {}
    for field in ("syntax_complete", "story_closed", "next_topic_separated"):
        value = payload.get(field)
        if not isinstance(value, bool):
            raise BoundarySemanticReviewError(
                f"BOUNDARY_SEMANTIC_REVIEW_INVALID_{field.upper()}"
            )
        booleans[field] = value
    recommended_raw = payload.get("recommended_end_cue_index")
    if isinstance(recommended_raw, bool) or not isinstance(recommended_raw, int):
        recommended_index = None
    else:
        recommended_index = recommended_raw
    by_index = {int(row["cue_index"]): row for row in rows}
    recommended = by_index.get(recommended_index) if recommended_index is not None else None
    target_end = int(rows[target_pos]["end_ms"])
    recommendation_valid = bool(
        recommended is not None
        and int(recommended["end_ms"]) >= target_end
        and int(recommended["end_ms"]) <= target_ms + max_forward_ms
    )
    evidence_raw = payload.get("evidence_cue_indexes")
    evidence_indexes = sorted(
        {
            value
            for value in evidence_raw if isinstance(value, int) and not isinstance(value, bool)
        }
    ) if isinstance(evidence_raw, list) else []
    visible_indexes = {
        int(row["cue_index"]) for row in visible_rows
    }
    evidence_valid = bool(evidence_indexes) and all(
        index in visible_indexes for index in evidence_indexes
    )
    selector_witness = request["selector_story_witness"]
    selector_pass = (
        isinstance(selector_witness, Mapping)
        and selector_witness.get("status") == "PASS"
    )
    post_target_evidence = [
        index
        for index in evidence_indexes
        if index in by_index
        and int(by_index[index]["end_ms"]) > target_end
    ]
    row_positions = {
        int(row["cue_index"]): position
        for position, row in enumerate(rows)
    }
    recommended_position = (
        row_positions.get(recommended_index)
        if recommended_index is not None
        else None
    )
    next_topic_witness_valid = bool(
        not booleans["next_topic_separated"]
        or (
            recommendation_valid
            and recommended_position is not None
            and (
                any(
                    row_positions.get(index, -1) > recommended_position
                    for index in evidence_indexes
                )
                or (
                    isinstance(source_separation_witness, Mapping)
                    and source_separation_witness.get("status") == "PASS"
                    and recommended_position == len(rows) - 1
                )
            )
        )
    )
    same_topic_reported = payload.get(
        "same_topic_continues_after_target"
    ) is True
    more_context_reported = payload.get("needs_more_context") is True
    same_topic_continues_after_target = bool(
        same_topic_reported
        and not booleans["story_closed"]
        and not booleans["next_topic_separated"]
        and recommended is None
        and post_target_evidence
        and selector_pass
    )
    needs_more_context = bool(
        more_context_reported and same_topic_continues_after_target
    )
    # This is the fourth boundary proposition: every selected content anchor
    # must remain covered.  It is deterministic and must not be inferred from
    # the reviewer's prose.
    content_anchor_covered = recommendation_valid
    dimensions_pass = all(booleans.values())
    status = (
        "PASS"
        if (
            selector_pass
            and dimensions_pass
            and recommendation_valid
            and evidence_valid
            and next_topic_witness_valid
        )
        else "BLOCK"
    )
    reason_codes_raw = payload.get("reason_codes")
    reason_codes = [
        str(value)
        for value in reason_codes_raw
        if isinstance(value, str) and value.strip()
    ] if isinstance(reason_codes_raw, list) else []
    if not selector_pass:
        reason_codes.append("SELECTOR_STORY_WITNESS_INSUFFICIENT")
    if not recommendation_valid:
        reason_codes.append("BOUNDARY_RECOMMENDATION_OUT_OF_SCOPE")
    if not evidence_valid:
        reason_codes.append("BOUNDARY_EVIDENCE_CUES_INVALID")
    if not next_topic_witness_valid:
        reason_codes.append("BOUNDARY_NEXT_TOPIC_WITNESS_MISSING")
    if needs_more_context:
        reason_codes.append("BOUNDARY_CONTEXT_EXHAUSTED")
    for field, passed in booleans.items():
        if not passed:
            reason_codes.append(field.upper() + "_NOT_PROVEN")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "review_scope": (
            "final_delivery"
            if source_separation_witness is not None
            else "source_full_window"
        ),
        "candidate_id": candidate_id,
        "request_sha256": request_sha256,
        "cue_grid_sha256": cue_grid_sha256(cues),
        "target_ms": target_ms,
        "target_cue_index": rows[target_pos]["cue_index"],
        "max_forward_ms": max_forward_ms,
        "recommended_end_cue_index": recommended_index,
        "recommended_end_ms": (
            int(recommended["end_ms"]) if recommendation_valid and recommended is not None else None
        ),
        **booleans,
        "content_anchor_covered": content_anchor_covered,
        "selector_story_witness": selector_witness,
        "reviewer_independence_group": SEMANTIC_LLM_INDEPENDENCE_GROUP,
        "semantic_independence_groups": [SEMANTIC_LLM_INDEPENDENCE_GROUP],
        "independent_semantic_vote_count": 1,
        "correlated_reviewer_disclosure": True,
        "evidence_cue_indexes": evidence_indexes,
        "next_topic_witness_valid": next_topic_witness_valid,
        "source_separation_witness": source_separation_witness,
        "same_topic_continues_after_target": (
            same_topic_continues_after_target
        ),
        "needs_more_context": needs_more_context,
        "retry_scope": (
            "same_topic_continues" if needs_more_context else "none"
        ),
        "reason_codes": sorted(set(reason_codes)),
        "summary": str(payload.get("summary") or "").strip(),
    }
