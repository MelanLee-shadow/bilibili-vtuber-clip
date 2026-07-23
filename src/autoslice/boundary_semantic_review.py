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


SCHEMA_VERSION = "talk-boundary-semantic-review.v1"
MAX_FORWARD_MS = 30_000
CONTEXT_CUES_EACH_SIDE = 8
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
- 可以从目标 cue 向后寻找，最多 {MAX_FORWARD_MS}ms；不得提前删掉候选选择器已经圈定的内容。
- 若目标本身已闭环，即使后面无停顿继续说，也应选目标；若目标半句或包袱未落地，才向后选最早同时满足三项的 cue。
- 结构化弹幕/SC 可证明话题触发或切换；长期记忆只能帮助理解指代，不能单独证明边界。
- 任一项无法证明就给 false，不要为了产片凑结论。

绑定请求 JSON：
{json.dumps(request, ensure_ascii=False, sort_keys=True)}

只输出 JSON：
{{"syntax_complete":bool,"story_closed":bool,"next_topic_separated":bool,
"recommended_end_cue_index":整数或null,"evidence_cue_indexes":[整数],
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
) -> dict[str, object]:
    """Return a validated, cue-grid-bound semantic boundary decision."""

    rows = _cue_rows(cues)
    target_pos = _target_index(rows, target_ms)
    lo = max(0, target_pos - CONTEXT_CUES_EACH_SIDE)
    hi = min(len(rows), target_pos + CONTEXT_CUES_EACH_SIDE + 1)
    visible_rows = rows[lo:hi]
    request = {
        "schema_version": "talk-boundary-semantic-request.v1",
        "candidate_id": candidate_id,
        "target_ms": target_ms,
        "target_cue_index": rows[target_pos]["cue_index"],
        "selection_hook": selection_hook,
        "selector_story_witness": _scorecard_story_witness(selection_scorecard),
        "cues": visible_rows,
        "structured_context": structured_context[:12_000],
        "candidate_context": candidate_context[:12_000],
        "max_forward_ms": MAX_FORWARD_MS,
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
        and int(recommended["end_ms"]) <= target_ms + MAX_FORWARD_MS
    )
    evidence_raw = payload.get("evidence_cue_indexes")
    evidence_indexes = sorted(
        {
            value
            for value in evidence_raw if isinstance(value, int) and not isinstance(value, bool)
        }
    ) if isinstance(evidence_raw, list) else []
    evidence_valid = bool(evidence_indexes) and all(index in by_index for index in evidence_indexes)
    selector_witness = request["selector_story_witness"]
    selector_pass = (
        isinstance(selector_witness, Mapping)
        and selector_witness.get("status") == "PASS"
    )
    # This is the fourth boundary proposition: every selected content anchor
    # must remain covered.  It is deterministic and must not be inferred from
    # the reviewer's prose.
    content_anchor_covered = recommendation_valid
    dimensions_pass = all(booleans.values())
    status = (
        "PASS"
        if selector_pass and dimensions_pass and recommendation_valid and evidence_valid
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
    for field, passed in booleans.items():
        if not passed:
            reason_codes.append(field.upper() + "_NOT_PROVEN")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "candidate_id": candidate_id,
        "request_sha256": request_sha256,
        "target_ms": target_ms,
        "target_cue_index": rows[target_pos]["cue_index"],
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
        "reason_codes": sorted(set(reason_codes)),
        "summary": str(payload.get("summary") or "").strip(),
    }
