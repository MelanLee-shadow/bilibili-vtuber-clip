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

from src.autoslice.frozen_boundary_receipt import (
    FrozenBoundaryReview,
    frozen_review_payload,
)
from src.autoslice.redelivery_boundary_projection import (
    RedeliveryBoundaryProjectionError,
    terminal_projection_relaxation,
    validate_projection_scope,
)

from src.autoslice.clip_context import MAX_PROMPT_CHARS


SCHEMA_VERSION = "talk-boundary-semantic-review.v1"
SEARCH_SCOPE_SCHEMA_VERSION = "talk-boundary-search-scope.v1"
ENDPOINT_SELECTION_CONTRACT_VERSION = (
    "talk-boundary-endpoint-selection-contract.v2"
)
MAX_FORWARD_MS = 30_000
SEMANTIC_TAIL_TRIM_MAX_MS = 15_000
CONTEXT_CUES_EACH_SIDE = 8
MAX_VISIBLE_CUES = 128
MAX_VISIBLE_TEXT_CHARS = 16_000
NEXT_TOPIC_WITNESS_CUES = 2
SOURCE_WITNESS_RESERVE_MS = 15_000
DELIVERY_TAIL_PAD_MS = 400
# Fresh-ASR cue ends and frozen ms authorities come from different timing
# sources; absorption is bounded and only ever crosses proven-silent gaps.
PIN_CROSSING_TOLERANCE_MS = 600
# Must not exceed DELIVERY_TAIL_PAD_MS: the resolver's tail-pad coverage
# bridge is what carries the delivered media from the earlier closure cue up
# to the untouched delivery floor.
SEMANTIC_FLOOR_SILENT_GAP_MS = DELIVERY_TAIL_PAD_MS
SEMANTIC_LLM_INDEPENDENCE_GROUP = "cpa-gpt-5.6-semantic-family"


class BoundarySemanticReviewError(ValueError):
    """The semantic reviewer returned an unusable or unbound decision."""


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _required_int_ms(name: str, value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BoundarySemanticReviewError(
            f"BOUNDARY_SEARCH_SCOPE_{name.upper()}_INVALID"
        )
    return value


def _optional_int_ms(name: str, value: object) -> int | None:
    if value is None:
        return None
    return _required_int_ms(name, value)


def _bounded_delivery_geometry(
    *,
    semantic_target_ms: int,
    manual_lower_bound_ms: int | None,
    published_recall_anchor_ms: int | None,
    structured_payoff_ms: int | None,
    required_owner_end_ms: int | None,
    boundary_end_mode: str,
    semantic_tail_trim_cap_ms: int,
) -> tuple[int, int]:
    """Return search origin and delivery floor for a non-pin scope.

    Keeping the calculation in one helper lets the reviewed-tail payoff clamp
    ask the exact counterfactual it governs: where would delivery land if the
    payoff detection hypothesis were absent?
    """

    search_origin_ms = max(
        [
            semantic_target_ms,
            *(
                value
                for value in (
                    manual_lower_bound_ms,
                    published_recall_anchor_ms,
                    structured_payoff_ms,
                )
                if value is not None
            ),
        ]
    )
    tail_trim_floor_ms = search_origin_ms
    if (
        boundary_end_mode
        in {"semantic_lower_bound", "published_recall_anchor"}
        and manual_lower_bound_ms is None
        and semantic_tail_trim_cap_ms > 0
    ):
        tail_trim_floor_ms = max(
            0, search_origin_ms - semantic_tail_trim_cap_ms
        )
    return search_origin_ms, max(
        tail_trim_floor_ms,
        required_owner_end_ms or 0,
        structured_payoff_ms or 0,
        manual_lower_bound_ms or 0,
    )


def boundary_search_scope_sha256(scope: Mapping[str, object]) -> str:
    """Digest a scope without trusting its self-declared digest."""

    return _canonical_sha256(
        {
            str(key): value
            for key, value in scope.items()
            if key != "scope_sha256"
        }
    )


def boundary_search_scope_is_valid(scope: object) -> bool:
    if not isinstance(scope, Mapping):
        return False
    if scope.get("schema_version") != SEARCH_SCOPE_SCHEMA_VERSION:
        return False
    digest = scope.get("scope_sha256")
    if (
        not _valid_sha256(digest)
        or digest != boundary_search_scope_sha256(scope)
    ):
        return False
    try:
        expected = build_boundary_search_scope(
            semantic_target_ms=scope.get("semantic_target_ms"),
            repair_cap_ms=scope.get("repair_cap_ms"),
            manual_lower_bound_ms=scope.get("manual_lower_bound_ms"),
            structured_payoff_ms=scope.get("structured_payoff_ms"),
            required_owner_end_ms=scope.get("required_owner_end_ms"),
            last_piece_start_ms=scope.get("last_piece_start_ms"),
            prior_piece_duration_ms=scope.get(
                "prior_piece_duration_ms"
            ),
            witness_reserve_ms=scope.get("witness_reserve_ms"),
            boundary_end_mode=scope.get(
                "boundary_end_mode", "semantic_lower_bound"
            ),
            published_recall_anchor_ms=scope.get(
                "published_recall_anchor_ms"
            ),
            baseline_tail_cap_ms=scope.get("baseline_tail_cap_ms"),
            semantic_tail_trim_cap_ms=scope.get(
                "semantic_tail_trim_cap_ms", 0
            ),
            reviewed_exact_interval_projection=scope.get(
                "reviewed_exact_interval_projection"
            ),
        )
    except BoundarySemanticReviewError:
        return False
    observed = dict(scope)
    legacy_missing_keys = [
        key
        for key in (
            "baseline_tail_cap_ms",
            "structured_payoff_clamped_from_ms",
            "semantic_tail_trim_cap_ms",
            "recommendation_backward_ms",
            "published_recall_anchor_ms",
            "reviewed_exact_interval_projection",
        )
        if key not in observed
    ]
    if legacy_missing_keys:
        # 旧产物 scope（对应键加入前冻结）：重建件多出的新键剔除后逐字段
        # 比对；自声明 sha 已在上方独立验证，语义比对双方剔除 sha 字段
        # （剔键后重建件的自带 sha 必然不同，属预期）。
        for key in legacy_missing_keys:
            expected.pop(key, None)
        observed.pop("scope_sha256", None)
        expected.pop("scope_sha256", None)
    return observed == expected


def build_boundary_search_scope(
    *,
    semantic_target_ms: int,
    repair_cap_ms: int,
    manual_lower_bound_ms: int | None = None,
    structured_payoff_ms: int | None = None,
    required_owner_end_ms: int | None = None,
    last_piece_start_ms: int = 0,
    prior_piece_duration_ms: int = 0,
    witness_reserve_ms: int = SOURCE_WITNESS_RESERVE_MS,
    boundary_end_mode: str = "semantic_lower_bound",
    published_recall_anchor_ms: int | None = None,
    baseline_tail_cap_ms: int | None = None,
    semantic_tail_trim_cap_ms: int = 0,
    reviewed_exact_interval_projection: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the one scope shared by source review, resolver, and retry.

    A hash-bound human lower bound and a structured payoff may move the normal
    search origin. A published recall anchor recenters review on the old
    endpoint without turning that endpoint into a delivery floor, so a bounded
    full rerun can remove an incomplete or off-topic tail. An exact source pin
    instead fixes the media ceiling while exposing only the preceding
    tail-pad-sized semantic closure cue. A required owner is only a
    delivery/review lower bound.
    """

    semantic_target = _required_int_ms(
        "semantic_target_ms", semantic_target_ms
    )
    repair_cap = _required_int_ms(
        "repair_cap_ms", repair_cap_ms, minimum=1_000
    )
    if repair_cap > 60_000:
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEARCH_SCOPE_REPAIR_CAP_MS_INVALID"
        )
    manual_lower_bound = _optional_int_ms(
        "manual_lower_bound_ms", manual_lower_bound_ms
    )
    structured_payoff = _optional_int_ms(
        "structured_payoff_ms", structured_payoff_ms
    )
    required_owner_end = _optional_int_ms(
        "required_owner_end_ms", required_owner_end_ms
    )
    published_recall_anchor = _optional_int_ms(
        "published_recall_anchor_ms", published_recall_anchor_ms
    )
    last_piece_start = _required_int_ms(
        "last_piece_start_ms", last_piece_start_ms
    )
    prior_piece_duration = _required_int_ms(
        "prior_piece_duration_ms", prior_piece_duration_ms
    )
    witness_reserve = _required_int_ms(
        "witness_reserve_ms", witness_reserve_ms
    )
    semantic_tail_trim_cap = _required_int_ms(
        "semantic_tail_trim_cap_ms", semantic_tail_trim_cap_ms
    )
    if semantic_tail_trim_cap > SEMANTIC_TAIL_TRIM_MAX_MS:
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEARCH_SCOPE_SEMANTIC_TAIL_TRIM_CAP_MS_INVALID"
        )
    baseline_tail_cap = _optional_int_ms(
        "baseline_tail_cap_ms", baseline_tail_cap_ms
    )
    try:
        terminal_projection = (
            validate_projection_scope(reviewed_exact_interval_projection)
            if reviewed_exact_interval_projection is not None
            else None
        )
    except RedeliveryBoundaryProjectionError as exc:
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEARCH_SCOPE_REVIEWED_EXACT_INTERVAL_PROJECTION_INVALID"
        ) from exc
    if terminal_projection is not None and (
        baseline_tail_cap is None
        or terminal_projection["reviewed_endpoint_ms"] != baseline_tail_cap
        or boundary_end_mode == "exact_source_pin"
    ):
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEARCH_SCOPE_REVIEWED_EXACT_INTERVAL_PROJECTION_INVALID"
        )
    if boundary_end_mode not in {
        "semantic_lower_bound",
        "published_recall_anchor",
        "exact_source_pin",
    }:
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEARCH_SCOPE_END_MODE_INVALID"
        )

    reasons: list[str] = []
    if (
        manual_lower_bound is not None
        and manual_lower_bound < semantic_target
    ):
        reasons.append("MANUAL_END_CANNOT_TRUNCATE_SEMANTIC_TARGET")

    # r13 尾锚的补全：structured payoff 是
    # 检测假设，不是复核权威。redelivery 尾锚在、其余锚（语义/手动/owner）
    # 全部可在 baseline 之内交付、唯独 payoff 越界时，假设让位于已复核终点
    # ——钳制并披露（评审仍在已发布终点裁收尾；评审判收不住才是真冲突）。
    # “可交付”复用同一条 bounded semantic-tail trim 计算；manual/owner 真越界
    # 或回剪帽仍够不到 baseline 时，照旧走 REQUIRED_OWNER_EXCLUDED 硬拦。
    structured_payoff_clamped_from_ms: int | None = None
    structured_payoff_effective = structured_payoff
    if (
        boundary_end_mode != "exact_source_pin"
        and baseline_tail_cap is not None
        and structured_payoff is not None
        and structured_payoff > baseline_tail_cap
        and _bounded_delivery_geometry(
            semantic_target_ms=semantic_target,
            manual_lower_bound_ms=manual_lower_bound,
            published_recall_anchor_ms=published_recall_anchor,
            structured_payoff_ms=None,
            required_owner_end_ms=required_owner_end,
            boundary_end_mode=boundary_end_mode,
            semantic_tail_trim_cap_ms=semantic_tail_trim_cap,
        )[1]
        <= baseline_tail_cap
    ):
        structured_payoff_clamped_from_ms = structured_payoff
        structured_payoff_effective = baseline_tail_cap

    exact_source_pin = (
        manual_lower_bound
        if boundary_end_mode == "exact_source_pin"
        else None
    )
    if boundary_end_mode == "exact_source_pin":
        if exact_source_pin is None:
            reasons.append("BOUNDARY_EXACT_SOURCE_PIN_MISSING")
            exact_source_pin = semantic_target
        if (
            structured_payoff is not None
            and structured_payoff > exact_source_pin
        ):
            # r13 钳制的 pin 模式对偶：payoff 是
            # 检测假设，不是复核权威。上方 r13 钳制只写给非 pin 模式，理由是
            # 「pin 被更强权威定死、尾锚无增量约束」——但这漏了一层：pin 是
            # 已验证公开媒体的字节终点，比 baseline 尾锚**更强**，却反而没有
            # 压制假设的能力，同一个越界 payoff 在弱模式下让位、在最强模式下
            # 硬拦（本案：semantic/manual/owner 全部 ≤ pin 113570，唯独 payoff
            # 检测到 124320——已发布终点之后的下一段结构化朗读被误判为本事件
            # 收尾）。语义目标或必需 owner 真越过 pin 才是真冲突，照旧硬拦。
            if semantic_target <= exact_source_pin and (
                required_owner_end is None
                or required_owner_end <= exact_source_pin
            ):
                structured_payoff_clamped_from_ms = structured_payoff
                structured_payoff_effective = exact_source_pin
            else:
                reasons.append(
                    "BOUNDARY_EXACT_SOURCE_PIN_PAYOFF_CONFLICT"
                )
        search_origin_ms = exact_source_pin
        delivery_lower_bound_ms = max(
            search_origin_ms,
            required_owner_end or 0,
            structured_payoff_effective or 0,
            manual_lower_bound or 0,
        )
    else:
        search_origin_ms, delivery_lower_bound_ms = (
            _bounded_delivery_geometry(
                semantic_target_ms=semantic_target,
                manual_lower_bound_ms=manual_lower_bound,
                published_recall_anchor_ms=published_recall_anchor,
                structured_payoff_ms=structured_payoff_effective,
                required_owner_end_ms=required_owner_end,
                boundary_end_mode=boundary_end_mode,
                semantic_tail_trim_cap_ms=semantic_tail_trim_cap,
            )
        )
    max_recommended_end_ms = (
        exact_source_pin
        if exact_source_pin is not None
        else search_origin_ms + repair_cap
    )
    # redelivery 尾部锚：
    # 同 BV 修复的成片终点不得越过已发布 baseline 覆盖终点——lower_bound
    # 模式的语义延伸在修复包上会把 V13 已裁定排除的下一话题包回来（replay/
    # regression 会正确拦下但永远无法收敛）。exact_source_pin 模式除外：
    # pin 是官方已发布媒体的字节终点权威（媒体轴），baseline 覆盖终点是
    # 字幕轴，天然比 pin 早一个尾垫（1863 r18 案 202320 vs 202720 恒
    # BLOCK）；pin 模式的终点已被更强权威定死，尾锚无增量约束。
    if (
        boundary_end_mode != "exact_source_pin"
        and baseline_tail_cap is not None
        and baseline_tail_cap < max_recommended_end_ms
    ):
        max_recommended_end_ms = baseline_tail_cap
    if delivery_lower_bound_ms > max_recommended_end_ms:
        reasons.append("BOUNDARY_REQUIRED_OWNER_EXCLUDED")
    recommendation_forward_ms = max(
        0, max_recommended_end_ms - search_origin_ms
    )
    recommendation_backward_ms = max(
        0, search_origin_ms - delivery_lower_bound_ms
    )
    required_local_source_context_end_ms = (
        max_recommended_end_ms + witness_reserve
    )
    if required_local_source_context_end_ms < prior_piece_duration:
        reasons.append("BOUNDARY_SEARCH_SCOPE_LAST_PIECE_MAPPING_INVALID")
        required_last_piece_source_end_ms = last_piece_start
    else:
        required_last_piece_source_end_ms = (
            last_piece_start
            + required_local_source_context_end_ms
            - prior_piece_duration
        )

    # 精确复核区间重播：
    # reviewed_exact_interval_projection 在场 = 交付必须逐字节重播 维护者 已复核
    # 的那一段源区间，语义回剪车道在这个模式下没有裁量权——终点只能是复核终点。
    # 旧写法把下限留在 bounded semantic-tail trim 的 delivery_lower_bound 上，
    # 于是只有「structured payoff 恰好被尾锚钳制」时下限才等于终点，projection
    # 才够得着（terminal_projection_relaxation 要求 min==cap==endpoint）；没有
    # payoff 的复核重播（本案 payoff=None，下限 65570 vs 终点 67760）会让评审
    # 选中终点前 90ms 的机器闭合 cue，成片短 90ms，随后被 redelivery baseline
    # 的 REDELIVERY_BASELINE_CUE_CUT_BY_NEW_BOUNDARY 硬拦——机制在、接线断。
    # 钉死下限后：栅格上恰好有 cue 收在复核终点则正常入选；没有则 positions 空、
    # 由 projection 以「闭合 cue + 跨越终点的下一话题 cue」证据桥接（漂移帽
    # 250ms 内），两者都不成立才 fail-closed——比事后被 baseline 门拦更早、更准。
    reviewed_endpoint_floor_ms: int | None = None
    if (
        terminal_projection is not None
        and max_recommended_end_ms
        == int(terminal_projection["reviewed_endpoint_ms"])
    ):
        reviewed_endpoint_floor_ms = int(
            terminal_projection["reviewed_endpoint_ms"]
        )

    core: dict[str, object] = {
        "schema_version": SEARCH_SCOPE_SCHEMA_VERSION,
        "boundary_end_mode": boundary_end_mode,
        "status": "BLOCK" if reasons else "PASS",
        "semantic_target_ms": semantic_target,
        "manual_lower_bound_ms": manual_lower_bound,
        "published_recall_anchor_ms": published_recall_anchor,
        "structured_payoff_ms": structured_payoff,
        "required_owner_end_ms": required_owner_end,
        "semantic_search_origin_ms": search_origin_ms,
        "delivery_lower_bound_ms": delivery_lower_bound_ms,
        # Keep the review centered on the automatic recall tail when the
        # bounded trim lane lowers only the delivery floor.  Required owners
        # may still move the review target later, preserving the old behavior.
        "review_target_ms": max(
            search_origin_ms, delivery_lower_bound_ms
        ),
        "repair_cap_ms": repair_cap,
        "semantic_tail_trim_cap_ms": semantic_tail_trim_cap,
        # 尾锚参与 sha 与重建验证；旧产物无此键=旧行为，向后兼容。
        "baseline_tail_cap_ms": baseline_tail_cap,
        # payoff 钳制披露：非 None 即「假设让位于已复核 baseline 终点」。
        "structured_payoff_clamped_from_ms": structured_payoff_clamped_from_ms,
        "max_recommended_end_ms": max_recommended_end_ms,
        "recommendation_forward_ms": recommendation_forward_ms,
        "recommendation_backward_ms": recommendation_backward_ms,
        "minimum_recommended_end_ms": (
            reviewed_endpoint_floor_ms
            if reviewed_endpoint_floor_ms is not None
            else (
                max(0, delivery_lower_bound_ms - DELIVERY_TAIL_PAD_MS)
                if exact_source_pin is not None
                else (
                    delivery_lower_bound_ms
                    if recommendation_backward_ms
                    else search_origin_ms
                )
            )
        ),
        "delivery_tail_pad_ms": DELIVERY_TAIL_PAD_MS,
        "witness_reserve_ms": witness_reserve,
        "prior_piece_duration_ms": prior_piece_duration,
        "last_piece_start_ms": last_piece_start,
        "required_local_source_context_end_ms": (
            required_local_source_context_end_ms
        ),
        "required_last_piece_source_end_ms": (
            required_last_piece_source_end_ms
        ),
        "reason_codes": sorted(set(reasons)),
    }
    if terminal_projection is not None:
        core["reviewed_exact_interval_projection"] = terminal_projection
    return {
        **core,
        "scope_sha256": boundary_search_scope_sha256(core),
    }


def required_source_context_end_ms(
    scope: Mapping[str, object],
    *,
    repair_cap_ms: int | None = None,
) -> int:
    """Translate a local retry ceiling plus witness reserve to source time."""

    if not boundary_search_scope_is_valid(scope):
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEARCH_SCOPE_BINDING_INVALID"
        )
    search_origin_ms = _required_int_ms(
        "semantic_search_origin_ms",
        scope.get("semantic_search_origin_ms"),
    )
    effective_cap_ms = _required_int_ms(
        "repair_cap_ms",
        (
            scope.get("repair_cap_ms")
            if repair_cap_ms is None
            else repair_cap_ms
        ),
        minimum=1_000,
    )
    if effective_cap_ms > 60_000:
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEARCH_SCOPE_REPAIR_CAP_MS_INVALID"
        )
    witness_reserve_ms = _required_int_ms(
        "witness_reserve_ms",
        scope.get("witness_reserve_ms"),
    )
    prior_piece_duration_ms = _required_int_ms(
        "prior_piece_duration_ms",
        scope.get("prior_piece_duration_ms"),
    )
    last_piece_start_ms = _required_int_ms(
        "last_piece_start_ms",
        scope.get("last_piece_start_ms"),
    )
    recommendation_ceiling_ms = (
        int(scope["max_recommended_end_ms"])
        if scope.get("boundary_end_mode") == "exact_source_pin"
        else search_origin_ms + effective_cap_ms
    )
    required_local_end_ms = (
        recommendation_ceiling_ms + witness_reserve_ms
    )
    if required_local_end_ms < prior_piece_duration_ms:
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEARCH_SCOPE_LAST_PIECE_MAPPING_INVALID"
        )
    return (
        last_piece_start_ms
        + required_local_end_ms
        - prior_piece_duration_ms
    )


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


def cue_rows(cues: Sequence[object]) -> list[dict[str, object]]:
    """1-based rows over the non-empty cue grid, shared with the resolver."""

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


_cue_rows = cue_rows


def cue_grid_sha256(cues: Sequence[object]) -> str:
    """Bind a semantic decision to the complete non-empty cue grid."""

    return _canonical_sha256(
        {
            "schema_version": "talk-boundary-cue-grid.v1",
            "cues": _cue_rows(cues),
        }
    )


def recommendation_eligibility(
    rows: Sequence[Mapping[str, object]],
    scope: Mapping[str, object],
) -> dict[str, object]:
    """Return recommendable cue positions plus bounded grid-jitter absorption.

    Frozen millisecond authorities (a published lower bound, recall anchor,
    or official source pin) come from a different timing source than the fresh
    ASR grid, so the closure cue may miss the base window ``[minimum, cap]`` by
    a bounded, provably content-free margin. Absorption never crosses speech:

    - ``exact_source_pin``: the single cue containing the pin may witness
      closure when it overruns the pin by at most
      ``PIN_CROSSING_TOLERANCE_MS``; its effective recommendation end is the
      pin itself and the delivered media end stays exactly the pin.
    - ``semantic_lower_bound`` / ``published_recall_anchor``: the latest cue
      ending before ``minimum`` may witness closure when the gap up to
      ``minimum`` contains no cue start and is at most
      ``SEMANTIC_FLOOR_SILENT_GAP_MS``. The former keeps its delivery floor;
      the latter already exposes a bounded backward review window.
    - A source-bound exact reviewed interval may project the unique cue just
      before its endpoint when the immediately following next-topic cue
      crosses that endpoint by no more than the stricter reviewed-timing drift
      bound. The reviewer still judges the fresh closure text and must cite the
      crossing cue as next-topic evidence.
    """

    minimum_end_ms = int(scope["minimum_recommended_end_ms"])
    cap_end_ms = int(scope["max_recommended_end_ms"])
    end_mode = str(scope.get("boundary_end_mode") or "semantic_lower_bound")
    positions = [
        position
        for position, row in enumerate(rows)
        if minimum_end_ms <= int(row["end_ms"]) <= cap_end_ms
    ]
    effective_end_ms = {
        int(rows[position]["cue_index"]): int(rows[position]["end_ms"])
        for position in positions
    }
    relaxations: list[dict[str, object]] = []
    if end_mode == "exact_source_pin":
        crossing = [
            position
            for position, row in enumerate(rows)
            if int(row["start_ms"]) <= cap_end_ms < int(row["end_ms"])
            and int(row["end_ms"]) - cap_end_ms
            <= PIN_CROSSING_TOLERANCE_MS
        ]
        for position in crossing:
            if position in positions:
                continue
            cue_index = int(rows[position]["cue_index"])
            positions.append(position)
            effective_end_ms[cue_index] = cap_end_ms
            relaxations.append(
                {
                    "kind": "pin_crossing_closure_cue",
                    "cue_index": cue_index,
                    "cue_end_ms": int(rows[position]["end_ms"]),
                    "pin_ms": cap_end_ms,
                    "overrun_ms": int(rows[position]["end_ms"])
                    - cap_end_ms,
                    "tolerance_ms": PIN_CROSSING_TOLERANCE_MS,
                }
            )
    else:
        before = [
            position
            for position, row in enumerate(rows)
            if int(row["end_ms"]) < minimum_end_ms
        ]
        if before:
            closure_position = max(
                before, key=lambda position: int(rows[position]["end_ms"])
            )
            closure_end_ms = int(rows[closure_position]["end_ms"])
            gap_ms = minimum_end_ms - closure_end_ms
            gap_has_speech = any(
                closure_end_ms <= int(row["start_ms"]) < minimum_end_ms
                for position, row in enumerate(rows)
                if position != closure_position
            )
            if (
                gap_ms <= SEMANTIC_FLOOR_SILENT_GAP_MS
                and not gap_has_speech
                and closure_position not in positions
            ):
                cue_index = int(rows[closure_position]["cue_index"])
                positions.append(closure_position)
                effective_end_ms[cue_index] = closure_end_ms
                relaxations.append(
                    {
                        "kind": "silent_gap_closure_cue",
                        "cue_index": cue_index,
                        "cue_end_ms": closure_end_ms,
                        "floor_ms": minimum_end_ms,
                        "gap_ms": gap_ms,
                        "tolerance_ms": SEMANTIC_FLOOR_SILENT_GAP_MS,
                    }
                )
        projection_scope = scope.get(
            "reviewed_exact_interval_projection"
        )
        if not positions and projection_scope is not None:
            try:
                projection = terminal_projection_relaxation(
                    rows,
                    projection_scope=projection_scope,
                    minimum_end_ms=minimum_end_ms,
                    cap_end_ms=cap_end_ms,
                    cue_grid_sha256=_canonical_sha256(
                        {
                            "schema_version": "talk-boundary-cue-grid.v1",
                            "cues": [dict(row) for row in rows],
                        }
                    ),
                )
            except RedeliveryBoundaryProjectionError as exc:
                raise BoundarySemanticReviewError(
                    "BOUNDARY_REVIEWED_EXACT_INTERVAL_PROJECTION_INVALID"
                ) from exc
            if projection is not None:
                position = next(
                    (
                        ordinal
                        for ordinal, row in enumerate(rows)
                        if int(row["cue_index"])
                        == int(projection["cue_index"])
                    ),
                    None,
                )
                if position is None:
                    raise BoundarySemanticReviewError(
                        "BOUNDARY_REVIEWED_EXACT_INTERVAL_PROJECTION_INVALID"
                    )
                positions.append(position)
                effective_end_ms[int(projection["cue_index"])] = int(
                    projection["reviewed_endpoint_ms"]
                )
                relaxations.append(projection)
    positions.sort()
    return {
        "positions": positions,
        "cue_indexes": [
            int(rows[position]["cue_index"]) for position in positions
        ],
        "effective_end_ms": effective_end_ms,
        "relaxations": relaxations,
    }


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
- evidence_cue_indexes 的每个索引也只可从绑定请求 JSON 的 cues[*].cue_index 中选择；structured_context 和 candidate_context 只帮助理解语义，绝不可从中引用 cue 索引。
- 只能从 recommendation_cue_indexes 选择。若 boundary_search_scope.recommendation_backward_ms>0，目标 cue 只是自动/旧公开召回尾锚；当它已拖入新话题、未回答问题或不完整尾巴时，可在该有界窗口内回剪到**最晚一个**已经覆盖 selection_hook 全部内容锚点、故事闭环且后续换题可证的 cue，并必须回 content_anchor_covered=true。普通 semantic_lower_bound 超出该窗口不得提前删内容；published_recall_anchor 只允许这次有界回剪，不把旧公开终点伪装成已人工确认的下界；若 boundary_end_mode=exact_source_pin，可选择 source pin 前最多 delivery_tail_pad_ms 的完整语义句尾，最终媒体仍由 source pin 精确截止，不可选择 pin 之后才开始的 cue。
- recommendation_relaxations 里列出的 cue 是经确定性证明后放行的有界例外：pin_crossing_closure_cue 是包含 pin 的收尾 cue（媒体仍精确截止在 pin）；silent_gap_closure_cue 是下限前最后一个收尾 cue，且它到下限之间没有任何语音；reviewed_exact_interval_terminal_projection 是 hash/source 绑定的 reviewed endpoint 在 fresh 网格上的终点投影，必须把其中 crossing_witness_cue_index 作为下一话题证据。语义合适才可选择。
- 若 next_topic_separated=true，evidence_cue_indexes 必须包含推荐 cue 之后、证明已进入下一话题/SC/谢礼的 cue；缺了会被判 BOUNDARY_NEXT_TOPIC_WITNESS_MISSING。
- review_scope=source_full_window 时，选点前必须把 recommendation_cue_indexes 全部比较完，并逐项检查 next_topic_witness_cue_indexes；后者是 endpoint 上限之外专门保留的换题见证，不是可以跳过的附录。next_topic_separated 不要求紧邻推荐 cue 的下一 cue 就换题：任何绑定请求中可见、位于推荐 cue 之后的明确新 SC、谢礼、另一话题或直播阶段切换都可作证，但必须在 evidence_cue_indexes 中实际引用。
- 可以从目标 cue 向后寻找，最多 {request["max_forward_ms"]}ms；exact_source_pin 的该值为 0。
- “目标内容已经讲完”不等于“此处已经形成安全切点”。若紧接目标的同话题提问、回应或收尾互动仍在继续，必须把 endpoint 向后推进到明确换题见证之前、仍在 recommendation_cue_indexes 内的**最晚一个完整收束 cue**；不得把问句留在片尾，也不得把回应切到片外。只有目标之后第一段可见内容已经明确属于另一话题时，目标才可直接充当收束点。
- “进入尾声”等字面词不能由确定性关键词自动签发 PASS；仍须结合绑定 cue 的上下文判断它是否真是推荐 endpoint 之后的阶段/话题切换。source_full_window 内没有可见的 post-end 换题见证时，next_topic_separated 必须为 false，即使 syntax/story 已经完整；final_delivery 只可使用下一条所述的 bound source witness 例外。
- 若请求带 PASS 的 terminal source separation witness，说明 source full-window 已证明 cut 后进入下一话题；此时 delivery 最后一条 cue 可用该 witness 证明 next_topic_separated，但 syntax/story 仍须按当前最终字幕重新判断。
- 结构化弹幕/SC 可证明话题触发或切换；长期记忆只能帮助理解指代，不能单独证明边界。
- 任一项无法证明就给 false，不要为了产片凑结论。

绑定请求 JSON：
{json.dumps(request, ensure_ascii=False, sort_keys=True)}

只输出 JSON：
{{"syntax_complete":bool,"story_closed":bool,"next_topic_separated":bool,
"content_anchor_covered":bool,
"recommended_end_cue_index":整数或null,"evidence_cue_indexes":[整数],
"same_topic_continues_after_target":bool,"needs_more_context":bool,
"reason_codes":[字符串],"summary":"一句中文结论"}}
"""


def _parse_recommended_cue_index(
    payload: Mapping[str, object],
) -> tuple[int | None, str]:
    value = payload.get("recommended_end_cue_index")
    if value is None:
        return None, "MISSING"
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "INVALID"
    return value, "PRESENT"


def _content_anchor_coverage_is_proven(
    *,
    payload: Mapping[str, object],
    recommendation_valid: bool,
    recommended: Mapping[str, object] | None,
    target_end_ms: int,
) -> bool:
    """Require an explicit CPA anchor vote only for a backward tail trim."""

    if not recommendation_valid or recommended is None:
        return False
    if int(recommended["end_ms"]) >= target_end_ms:
        return True
    return payload.get("content_anchor_covered") is True


def _semantic_review_passes(
    *,
    selector_pass: bool,
    booleans: Mapping[str, bool],
    recommendation_valid: bool,
    content_anchor_covered: bool,
    evidence_valid: bool,
    next_topic_witness_valid: bool,
) -> bool:
    return bool(
        selector_pass
        and all(booleans.values())
        and recommendation_valid
        and content_anchor_covered
        and evidence_valid
        and next_topic_witness_valid
    )


def _semantic_payload_for_request(
    request: Mapping[str, object],
    current_cue_grid_sha256: str,
    *,
    frozen_review: FrozenBoundaryReview | None,
    replay_audit: dict[str, object] | None,
    llm_call: Callable[[str], str],
    extract_json: Callable[[str], Any],
) -> object:
    frozen_payload = frozen_review_payload(
        frozen_review,
        request=request,
        cue_grid_sha256=current_cue_grid_sha256,
    )
    if frozen_payload is not None:
        payload, carry = frozen_payload
        if replay_audit is not None:
            replay_audit.update(carry)
        return payload
    try:
        return extract_json(llm_call(_build_prompt(request)))
    except Exception as exc:
        raise BoundarySemanticReviewError(
            f"BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:{type(exc).__name__}"
        ) from exc


def _semantic_request(
    *,
    candidate_id: str,
    target_ms: int,
    target_cue_index: int,
    selection_hook: str,
    selector_story_witness: object,
    visible_rows: list[dict[str, object]],
    structured_context: str,
    candidate_context: str,
    max_forward_ms: int,
    scope: Mapping[str, object],
    recommendation_indexes: list[int],
    recommendation_relaxations: list[dict[str, object]],
    next_topic_witness_rows: Sequence[Mapping[str, object]],
    source_separation_witness: Mapping[str, object] | None,
) -> dict[str, object]:
    return {
        "schema_version": "talk-boundary-semantic-request.v1",
        "endpoint_selection_contract_version": (
            ENDPOINT_SELECTION_CONTRACT_VERSION
        ),
        "review_scope": (
            "final_delivery"
            if source_separation_witness is not None
            else "source_full_window"
        ),
        "candidate_id": candidate_id,
        "target_ms": target_ms,
        "target_cue_index": target_cue_index,
        "selection_hook": selection_hook,
        "selector_story_witness": selector_story_witness,
        "cues": visible_rows,
        "structured_context": structured_context[:12_000],
        "candidate_context": candidate_context,
        "max_forward_ms": max_forward_ms,
        "boundary_search_scope": scope,
        "recommendation_cue_indexes": recommendation_indexes,
        "recommendation_relaxations": recommendation_relaxations,
        "next_topic_witness_cue_indexes": [
            int(row["cue_index"]) for row in next_topic_witness_rows
        ],
        "terminal_source_separation_witness": source_separation_witness,
    }


def _frozen_decision_binding(
    replay_audit: Mapping[str, object] | None,
) -> dict[str, object]:
    return (
        {"frozen_decision_binding": dict(replay_audit)}
        if replay_audit
        else {}
    )


def _next_topic_witness_assessment(
    *,
    booleans: Mapping[str, bool],
    recommendation_valid: bool,
    recommended_index: int | None,
    rows: Sequence[Mapping[str, object]],
    evidence_indexes: Sequence[int],
    source_separation_witness: Mapping[str, object] | None,
    recommendation_relaxations: Sequence[Mapping[str, object]],
) -> tuple[bool, Mapping[str, object] | None]:
    row_positions = {
        int(row["cue_index"]): position
        for position, row in enumerate(rows)
    }
    recommended_position = (
        row_positions.get(recommended_index)
        if recommended_index is not None
        else None
    )
    valid = bool(
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
    selected_projection = next(
        (
            row
            for row in recommendation_relaxations
            if row.get("kind")
            == "reviewed_exact_interval_terminal_projection"
            and row.get("cue_index") == recommended_index
        ),
        None,
    )
    if (
        selected_projection is not None
        and selected_projection.get("crossing_witness_cue_index")
        not in evidence_indexes
    ):
        valid = False
    return valid, selected_projection


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
    boundary_search_scope: Mapping[str, object] | None = None,
    frozen_review: FrozenBoundaryReview | None = None,
    replay_audit: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return a validated, cue-grid-bound semantic boundary decision."""

    if boundary_search_scope is None:
        scope = build_boundary_search_scope(
            semantic_target_ms=target_ms,
            repair_cap_ms=max_forward_ms,
        )
    else:
        scope = dict(boundary_search_scope)
    if not boundary_search_scope_is_valid(scope):
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEMANTIC_SEARCH_SCOPE_INVALID"
        )
    if scope.get("status") != "PASS":
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEMANTIC_SEARCH_SCOPE_BLOCKED:"
            + ",".join(
                str(reason)
                for reason in scope.get("reason_codes") or []
            )
        )
    if (
        isinstance(max_forward_ms, bool)
        or not isinstance(max_forward_ms, int)
        or not 0 <= max_forward_ms <= 60_000
    ):
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEMANTIC_REVIEW_FORWARD_CAP_INVALID"
        )
    if (
        scope.get("review_target_ms") != target_ms
        or scope.get("recommendation_forward_ms") != max_forward_ms
    ):
        raise BoundarySemanticReviewError(
            "BOUNDARY_SEMANTIC_SEARCH_SCOPE_ARGUMENT_MISMATCH"
        )
    rows = _cue_rows(cues)
    target_pos = _target_index(rows, target_ms)
    eligibility = recommendation_eligibility(rows, scope)
    recommendation_positions = list(eligibility["positions"])
    recommendation_indexes = list(eligibility["cue_indexes"])
    recommendation_effective_end_ms = dict(
        eligibility["effective_end_ms"]
    )
    recommendation_relaxations = list(eligibility["relaxations"])
    recommendation_lo = (
        recommendation_positions[0]
        if recommendation_positions
        else target_pos
    )
    recommendation_hi = (
        recommendation_positions[-1] + 1
        if recommendation_positions
        else target_pos + 1
    )
    lo = max(
        0,
        min(target_pos, recommendation_lo) - CONTEXT_CUES_EACH_SIDE,
    )
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
    request = _semantic_request(
        candidate_id=candidate_id,
        target_ms=target_ms,
        target_cue_index=int(rows[target_pos]["cue_index"]),
        selection_hook=selection_hook,
        selector_story_witness=_scorecard_story_witness(selection_scorecard),
        visible_rows=visible_rows,
        structured_context=structured_context,
        candidate_context=candidate_context,
        max_forward_ms=max_forward_ms,
        scope=scope,
        recommendation_indexes=recommendation_indexes,
        recommendation_relaxations=recommendation_relaxations,
        next_topic_witness_rows=rows[recommendation_hi:witness_hi],
        source_separation_witness=source_separation_witness,
    )
    request_sha256 = _canonical_sha256(request)
    current_cue_grid_sha256 = cue_grid_sha256(cues)
    payload = _semantic_payload_for_request(
        request,
        current_cue_grid_sha256,
        frozen_review=frozen_review,
        replay_audit=replay_audit,
        llm_call=llm_call,
        extract_json=extract_json,
    )
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
    recommended_index, recommendation_value_state = (
        _parse_recommended_cue_index(payload)
    )
    by_index = {int(row["cue_index"]): row for row in rows}
    recommended = by_index.get(recommended_index) if recommended_index is not None else None
    target_end = int(rows[target_pos]["end_ms"])
    recommendation_valid = bool(
        recommended is not None
        and recommended_index in recommendation_indexes
        and recommended_index in recommendation_effective_end_ms
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
    next_topic_witness_valid, selected_projection = (
        _next_topic_witness_assessment(
            booleans=booleans,
            recommendation_valid=recommendation_valid,
            recommended_index=recommended_index,
            rows=rows,
            evidence_indexes=evidence_indexes,
            source_separation_witness=source_separation_witness,
            recommendation_relaxations=recommendation_relaxations,
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
    content_anchor_covered = _content_anchor_coverage_is_proven(
        payload=payload,
        recommendation_valid=recommendation_valid,
        recommended=recommended,
        target_end_ms=target_end,
    )
    status = "PASS" if _semantic_review_passes(
        selector_pass=selector_pass,
        booleans=booleans,
        recommendation_valid=recommendation_valid,
        content_anchor_covered=content_anchor_covered,
        evidence_valid=evidence_valid,
        next_topic_witness_valid=next_topic_witness_valid,
    ) else "BLOCK"
    reason_codes_raw = payload.get("reason_codes")
    reason_codes = [
        str(value)
        for value in reason_codes_raw
        if isinstance(value, str) and value.strip()
    ] if isinstance(reason_codes_raw, list) else []
    if not selector_pass:
        reason_codes.append("SELECTOR_STORY_WITNESS_INSUFFICIENT")
    if not recommendation_valid:
        if recommendation_value_state == "MISSING":
            reason_codes.append("BOUNDARY_RECOMMENDATION_MISSING")
        elif recommendation_value_state == "INVALID":
            reason_codes.append("BOUNDARY_RECOMMENDATION_INVALID")
        elif recommended is None:
            reason_codes.append("BOUNDARY_RECOMMENDATION_UNKNOWN_CUE")
        else:
            reason_codes.append("BOUNDARY_RECOMMENDATION_OUT_OF_SCOPE")
    if not evidence_valid:
        reason_codes.append("BOUNDARY_EVIDENCE_CUES_INVALID")
    if not next_topic_witness_valid:
        reason_codes.append("BOUNDARY_NEXT_TOPIC_WITNESS_MISSING")
        if selected_projection is not None:
            reason_codes.append(
                "BOUNDARY_REVIEWED_PROJECTION_WITNESS_MISSING"
            )
    if not content_anchor_covered:
        reason_codes.append("CONTENT_ANCHOR_COVERED_NOT_PROVEN")
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
        "cue_grid_sha256": current_cue_grid_sha256,
        "target_ms": target_ms,
        "target_cue_index": rows[target_pos]["cue_index"],
        "max_forward_ms": max_forward_ms,
        "boundary_search_scope": scope,
        "recommended_end_cue_index": recommended_index,
        "recommended_end_ms": (
            int(recommendation_effective_end_ms[recommended_index])
            if recommendation_valid and recommended is not None
            else None
        ),
        "recommendation_relaxations": recommendation_relaxations,
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
        **_frozen_decision_binding(replay_audit),
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
