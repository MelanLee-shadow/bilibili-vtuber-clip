"""语义路径 OR 聚合 v2 的对抗性回归（ChatGPT Pro §二末表逐条）。

设计权威：内部设计文档留存（or-gate-metric-and-function-split）
真值权威：内部盲评真值文档留存

八条对抗变形（Pro 原表）与本文件的对应：
① 任意一项篡改为 4 但不给证据合同 → 不得激活 OR      :: test_level_four_without_proof_*
② 同一时间戳证据复制两次 → 不算两份                 :: test_duplicate_atom_*
③ 引文落在片段范围外 → 路径失效                     :: test_out_of_window_*
④ `self_contained` 改为 1 → 无论其他多高都不得发布   :: test_self_contained_gate_*
⑤ 关系分 4 但仅靠 CP lore → 关系路径失效             :: test_relationship_lore_only_*
⑥ 获胜轴 4→3 → 对应突出路径必须关闭                 :: test_winning_axis_downgrade_*
⑦ 非获胜轴不确定性增加 → 不得降低获胜路径分         :: test_losing_axis_uncertainty_*
⑧ 疲劳标签换成较轻类别（语义标签未变）→ 规避不掉     :: test_fatigue_label_*
"""

from __future__ import annotations

import pytest

from src.autoslice.selection_metric_v2 import (
    ATTRIBUTION_UNVERIFIED,
    ATTRIBUTION_VERIFIED_HOST_DOMINANT,
    ATTRIBUTION_VERIFIED_SOLO,
    PARK_STATUS,
    SOLO_PATH_BASE,
    evaluate_selection_metric_v2,
)
from src.autoslice.selection_scorecard import normalize_selection_scorecard
from src.autoslice.speaker_manual_review import SPEAKER_MANUAL_REVIEW_STATUSES

WINDOW_START = 100_000
WINDOW_END = 200_000

_BASE_DIMENSIONS = {
    "lidousha_centrality": 3,
    "stance_intensity": 2,
    "audience_salience": 2,
    "relationship_interaction": 3,
    "persona_reversal": 3,
    "comedic_payoff": 3,
    "self_contained": 3,
}


def _v1_card(*, tier: int = 2, uncertainty: float = 0.0, **overrides) -> dict:
    dimensions = dict(_BASE_DIMENSIONS)
    dimensions.update(overrides)
    card = normalize_selection_scorecard(
        {
            "tier": tier,
            "tier_basis": "personal_stance",
            "tier_reason": "v2 canary",
            "tier_evidence_cues": [1, 2],
            "dimensions": dimensions,
            "uncertainty_penalty": uncertainty,
            "fatigue_penalty": 0.0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert card is not None
    return card


def _atom(axis: str, cue: int, start: int, end: int, *, role: str = "", kind: str = "quote") -> dict:
    return {
        "axis": axis,
        "kind": kind,
        "cue_index": cue,
        "start_ms": start,
        "end_ms": end,
        "role": role,
    }


def _closed_proof(axis: str) -> list[dict]:
    """两个不重叠的独立原子——事件闭合的 (b) 路。"""

    return [
        _atom(axis, 11, 110_000, 120_000),
        _atom(axis, 21, 150_000, 160_000),
    ]


def _evaluate(card: dict, **kwargs) -> dict:
    result = evaluate_selection_metric_v2(
        scorecard=card,
        attribution_status=kwargs.pop(
            "attribution_status", ATTRIBUTION_VERIFIED_HOST_DOMINANT
        ),
        candidate_start_ms=kwargs.pop("candidate_start_ms", WINDOW_START),
        candidate_end_ms=kwargs.pop("candidate_end_ms", WINDOW_END),
        **kwargs,
    )
    assert result is not None
    return result


# --------------------------------------------------------------------------
# 基础形状
# --------------------------------------------------------------------------


def test_verified_solo_path_wins_over_broad_composite():
    """一条被证据合同确认的单轴卖点足以放行——这就是 OR 的全部意义。"""

    card = _v1_card(comedic_payoff=4)
    result = _evaluate(card, proof_atoms=_closed_proof("comedic_payoff"))
    assert result["status"] == "VALID"
    assert result["winning_path_v2"] == "comedic_payoff"
    assert result["quality_score_v2"] == pytest.approx(SOLO_PATH_BASE)
    assert result["verified_level_4_axes"] == ["comedic_payoff"]
    # 综合路径仍然被算出来并留痕，只是没赢。
    assert result["path_scores_v2"]["broad"]["available"] is True
    assert result["path_scores_v2"]["broad"]["score"] < SOLO_PATH_BASE


def test_action_reaction_chain_closes_a_single_overlapping_moment():
    """Pro 明确：不得因为强求"两段证据"而误杀一个本身完整的单一高潮。"""

    atoms = [
        _atom("relationship_interaction", 11, 110_000, 120_000, role="action"),
        # 与动作重叠——(b) 两不重叠原子这条走不通，必须靠 (a) 动作→反应链闭合。
        _atom("relationship_interaction", 12, 115_000, 125_000, role="reaction"),
    ]
    result = _evaluate(_v1_card(relationship_interaction=4), proof_atoms=atoms)
    assert result["winning_path_v2"] == "relationship_interaction"
    assert (
        result["path_scores_v2"]["relationship_interaction"]["reason"]
        == "ACTION_REACTION_CHAIN"
    )


def test_dual_track_v1_fields_are_preserved_verbatim():
    """双口径留痕（Pro §五）：v1 原样并存，绝不被 v2 覆盖。"""

    card = _v1_card(comedic_payoff=4, uncertainty=3.0)
    result = _evaluate(card, proof_atoms=_closed_proof("comedic_payoff"))
    assert result["legacy_raw_v1"] == card["raw_score"]
    assert result["legacy_effective_v1"] == card["effective_score"]
    # v2 是影子口径，自陈未标定、不作放行门。
    assert result["provisional_calibration"] is True
    assert "V2_SHADOW_ONLY_NOT_A_RELEASE_GATE" in result["reason_codes"]
    for field in (
        "decision_policy_version",
        "rubric_version",
        "config_hash",
        "model_version",
    ):
        assert field in result


def test_invalid_v1_scorecard_yields_none_fail_closed():
    """v1 卡无效 → v2 不成立；不是"放行"，调用方保持 v1 行为。"""

    assert (
        evaluate_selection_metric_v2(
            scorecard={"status": "VALID"},
            attribution_status=ATTRIBUTION_VERIFIED_SOLO,
            candidate_start_ms=WINDOW_START,
            candidate_end_ms=WINDOW_END,
        )
        is None
    )


def test_unknown_attribution_status_yields_none():
    assert (
        evaluate_selection_metric_v2(
            scorecard=_v1_card(),
            attribution_status="PROBABLY_HER",
            candidate_start_ms=WINDOW_START,
            candidate_end_ms=WINDOW_END,
        )
        is None
    )


# --------------------------------------------------------------------------
# 维护者 第 3 条：说话人存疑 → 直接人工审阅
# --------------------------------------------------------------------------


def test_unverified_attribution_parks_for_manual_review_not_a_penalty():
    """「说话人存疑都要直接给人工审阅」——不是扣分、不是猜。"""

    card = _v1_card(comedic_payoff=4)
    result = _evaluate(
        card,
        attribution_status=ATTRIBUTION_UNVERIFIED,
        proof_atoms=_closed_proof("comedic_payoff"),
    )
    assert result["status"] == "PARKED"
    assert result["requires_speaker_manual_review"] is True
    # 复用既有停泊态，不新造平行状态机。
    assert result["speaker_manual_review_status"] == PARK_STATUS
    assert PARK_STATUS in SPEAKER_MANUAL_REVIEW_STATUSES
    # 关键：没有被"扣成一个低分"，而是根本没有分。
    assert result["quality_score_v2"] is None
    assert result["winning_path_v2"] is None
    assert "SPEAKER_ATTRIBUTION_UNVERIFIED" in result["reason_codes"]


def test_solo_stream_attribution_is_accepted():
    """维护者：「除非是单人直播」——单人场不需要分离即可算分。"""

    result = _evaluate(
        _v1_card(comedic_payoff=4),
        attribution_status=ATTRIBUTION_VERIFIED_SOLO,
        proof_atoms=_closed_proof("comedic_payoff"),
    )
    assert result["status"] == "VALID"
    assert result["requires_speaker_manual_review"] is False


# --------------------------------------------------------------------------
# ① 篡改为 4 但不给证据合同 → 不得激活 OR
# --------------------------------------------------------------------------


def test_level_four_without_proof_does_not_activate_or():
    result = _evaluate(_v1_card(comedic_payoff=4), proof_atoms=[])
    assert result["winning_path_v2"] == "broad"
    assert result["verified_level_4_axes"] == []
    assert result["path_scores_v2"]["comedic_payoff"]["available"] is False
    assert result["path_scores_v2"]["comedic_payoff"]["reason"] == "NO_ADMISSIBLE_PROOF"


def test_level_four_without_proof_is_not_a_deduction():
    """证据不足 = 路径不成立，**不是减分**：综合路径分毫发无损。"""

    with_claim = _evaluate(_v1_card(comedic_payoff=4), proof_atoms=[])
    # 同样的维度分、同样没有证据——综合路径分只由维度决定。
    assert with_claim["path_scores_v2"]["broad"]["score"] == pytest.approx(
        with_claim["path_scores_v2"]["broad"]["base"]
    )
    assert with_claim["path_scores_v2"]["comedic_payoff"]["score"] is None


def test_legacy_level_four_never_becomes_verified_level_four():
    """Pro §五：旧的 `level=4` 不得直接当 `verified_level_4`。

    历史卡（没有新证据合同所需的证据原子）必须重跑验证，不能被追认。
    """

    result = _evaluate(
        _v1_card(
            comedic_payoff=4,
            persona_reversal=4,
            relationship_interaction=4,
            stance_intensity=4,
            audience_salience=4,
        ),
        proof_atoms=[],
    )
    assert result["verified_level_4_axes"] == []
    assert result["winning_path_v2"] == "broad"


# --------------------------------------------------------------------------
# ② 同一时间戳证据复制两次 → 不算两份
# --------------------------------------------------------------------------


def test_duplicate_atom_does_not_count_twice():
    duplicated = [
        _atom("comedic_payoff", 11, 110_000, 120_000),
        _atom("comedic_payoff", 11, 110_000, 120_000),
    ]
    result = _evaluate(_v1_card(comedic_payoff=4), proof_atoms=duplicated)
    assert result["path_scores_v2"]["comedic_payoff"]["available"] is False
    assert result["path_scores_v2"]["comedic_payoff"]["reason"] == "EVENT_NOT_CLOSED"
    assert len(result["proof_ids_v2"]["comedic_payoff"]) == 1


def test_duplicate_visual_event_at_same_span_does_not_count_twice():
    duplicated = [
        _atom("persona_reversal", -1, 110_000, 120_000, kind="visual_event"),
        _atom("persona_reversal", -1, 110_000, 120_000, kind="visual_event"),
    ]
    result = _evaluate(_v1_card(persona_reversal=4), proof_atoms=duplicated)
    assert result["path_scores_v2"]["persona_reversal"]["available"] is False
    assert len(result["proof_ids_v2"]["persona_reversal"]) == 1


# --------------------------------------------------------------------------
# ③ 引文落在片段范围外 → 路径失效
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end",
    [
        (WINDOW_START - 5_000, WINDOW_START - 1_000),   # 整段在窗前
        (WINDOW_END + 1_000, WINDOW_END + 5_000),       # 整段在窗后
        (WINDOW_START - 1_000, WINDOW_START + 5_000),   # 跨左边界
        (WINDOW_END - 5_000, WINDOW_END + 1_000),       # 跨右边界
    ],
)
def test_out_of_window_quote_kills_the_path(start: int, end: int):
    atoms = [
        _atom("comedic_payoff", 11, 110_000, 120_000),
        _atom("comedic_payoff", 21, start, end),
    ]
    result = _evaluate(_v1_card(comedic_payoff=4), proof_atoms=atoms)
    # 只剩一条可采信原子 → 闭合不了 → 路径不成立。
    assert result["path_scores_v2"]["comedic_payoff"]["available"] is False
    assert len(result["proof_ids_v2"]["comedic_payoff"]) == 1
    assert result["winning_path_v2"] == "broad"


# --------------------------------------------------------------------------
# ④ self_contained 改为 1 → 无论其他多高都不得发布
# --------------------------------------------------------------------------


def test_self_contained_gate_blocks_regardless_of_everything_else():
    card = _v1_card(
        self_contained=1,
        comedic_payoff=4,
        persona_reversal=4,
        relationship_interaction=4,
        stance_intensity=4,
        audience_salience=4,
    )
    result = _evaluate(card, proof_atoms=_closed_proof("comedic_payoff"))
    assert result["status"] == "GATED"
    assert result["quality_score_v2"] is None
    assert result["winning_path_v2"] is None
    assert result["gate_results_v2"]["self_contained"]["passed"] is False
    assert "SELF_CONTAINED_GATE_FAILED" in result["reason_codes"]


@pytest.mark.parametrize(
    "attribution,self_contained,expected_status",
    [
        (ATTRIBUTION_UNVERIFIED, 3, "PARKED"),
        (ATTRIBUTION_VERIFIED_SOLO, 1, "GATED"),
        (ATTRIBUTION_VERIFIED_HOST_DOMINANT, 3, "VALID"),
    ],
)
def test_record_key_set_is_stable_across_statuses(
    attribution: str, self_contained: int, expected_status: str
):
    """持久化/报表侧不必按 status 分支取字段——键集合不随状态漂移。"""

    result = _evaluate(
        _v1_card(comedic_payoff=4, self_contained=self_contained),
        attribution_status=attribution,
        proof_atoms=_closed_proof("comedic_payoff"),
    )
    assert result["status"] == expected_status
    for field in (
        "quality_score_v2",
        "winning_path_v2",
        "path_scores_v2",
        "proof_ids_v2",
        "penalty_components_v2",
        "verified_level_4_axes",
        "requires_speaker_manual_review",
        "legacy_raw_v1",
        "legacy_effective_v1",
        "gate_results_v2",
    ):
        assert field in result, field


def test_self_contained_gate_is_not_an_or_path():
    """gate 是必要条件不是卖点：它不出现在任何路径里。"""

    result = _evaluate(_v1_card(comedic_payoff=4), proof_atoms=_closed_proof("comedic_payoff"))
    assert "self_contained" not in result["path_scores_v2"]
    assert "lidousha_centrality" not in result["path_scores_v2"]


# --------------------------------------------------------------------------
# ⑤ 关系分 4 但仅靠 CP lore → 关系路径失效
# --------------------------------------------------------------------------


def test_relationship_lore_only_does_not_verify_the_path():
    """站外 CP lore 不是可定位的片内证据。"""

    lore = [
        {
            "axis": "relationship_interaction",
            "kind": "external_lore",
            "cue_index": -1,
            "start_ms": 110_000,
            "end_ms": 120_000,
            "role": "",
        },
        {
            "axis": "relationship_interaction",
            "kind": "external_lore",
            "cue_index": -1,
            "start_ms": 150_000,
            "end_ms": 160_000,
            "role": "",
        },
    ]
    result = _evaluate(_v1_card(relationship_interaction=4), proof_atoms=lore)
    assert result["path_scores_v2"]["relationship_interaction"]["available"] is False
    assert result["proof_ids_v2"]["relationship_interaction"] == []
    assert result["winning_path_v2"] == "broad"


# --------------------------------------------------------------------------
# ⑥ 获胜轴 4→3 → 对应突出路径必须关闭
# --------------------------------------------------------------------------


def test_winning_axis_downgrade_closes_that_path():
    proof = _closed_proof("comedic_payoff")
    won = _evaluate(_v1_card(comedic_payoff=4), proof_atoms=proof)
    assert won["winning_path_v2"] == "comedic_payoff"

    # 同样的证据，只把主张从 4 降到 3。
    downgraded = _evaluate(_v1_card(comedic_payoff=3), proof_atoms=proof)
    assert downgraded["path_scores_v2"]["comedic_payoff"]["available"] is False
    assert downgraded["path_scores_v2"]["comedic_payoff"]["reason"] == "LEVEL_BELOW_CLAIM"
    assert downgraded["winning_path_v2"] == "broad"
    assert downgraded["quality_score_v2"] < won["quality_score_v2"]


# --------------------------------------------------------------------------
# ⑦ 非获胜轴不确定性增加 → 不得降低获胜路径分
# --------------------------------------------------------------------------


def test_losing_axis_uncertainty_never_pollutes_the_winning_path():
    """Pro 点名的陷阱：把七维不确定性先加总、再从 max 之后扣。"""

    card = _v1_card(comedic_payoff=4)
    proof = _closed_proof("comedic_payoff")
    clean = _evaluate(card, proof_atoms=proof, axis_uncertainty={})
    noisy = _evaluate(
        card,
        proof_atoms=proof,
        # 全部噪声都堆在**落败**轴上。
        axis_uncertainty={
            "relationship_interaction": 9.0,
            "stance_intensity": 8.0,
            "audience_salience": 7.0,
            "persona_reversal": 6.0,
        },
    )
    assert noisy["winning_path_v2"] == clean["winning_path_v2"] == "comedic_payoff"
    assert noisy["quality_score_v2"] == clean["quality_score_v2"]
    assert (
        noisy["path_scores_v2"]["comedic_payoff"]["score"]
        == clean["path_scores_v2"]["comedic_payoff"]["score"]
    )
    # 而综合路径确实被它自己依赖的证据噪声压低了——这是对的。
    assert noisy["path_scores_v2"]["broad"]["score"] < clean["path_scores_v2"]["broad"]["score"]


def test_winning_axis_uncertainty_does_reduce_its_own_path():
    """只扣该路径实际依赖的证据——获胜轴自己的不确定性必须照扣。"""

    card = _v1_card(comedic_payoff=4)
    proof = _closed_proof("comedic_payoff")
    penalised = _evaluate(
        card, proof_atoms=proof, axis_uncertainty={"comedic_payoff": 5.0}
    )
    assert penalised["path_scores_v2"]["comedic_payoff"]["score"] == pytest.approx(
        SOLO_PATH_BASE - 5.0
    )


# --------------------------------------------------------------------------
# ⑧ 疲劳标签换成较轻类别（语义标签未变）→ 规避不掉惩罚
# --------------------------------------------------------------------------


def test_fatigue_label_swap_cannot_dodge_the_penalty():
    card = _v1_card(comedic_payoff=4)
    proof = _closed_proof("comedic_payoff")
    counts = {"妈感姐妹分类": 3}
    heavy = _evaluate(
        card,
        proof_atoms=proof,
        topic_fingerprint="妈感姐妹分类",
        session_fingerprint_counts=counts,
        fatigue_label="high_repetition",
    )
    light = _evaluate(
        card,
        proof_atoms=proof,
        topic_fingerprint="妈感姐妹分类",
        session_fingerprint_counts=counts,
        fatigue_label="incidental",  # 只换标签，语义指纹没变
    )
    assert heavy["quality_score_v2"] == light["quality_score_v2"]
    assert (
        heavy["penalty_components_v2"]["fatigue"]
        == light["penalty_components_v2"]["fatigue"]
        == pytest.approx(6.0)
    )
    # 标签只回显，不参与计算。
    assert light["penalty_components_v2"]["declared_fatigue_label"] == "incidental"


def test_fatigue_applies_after_max_not_inside_a_path():
    """疲劳是"组合选择效用"不是片子自身质量 → 最后全局扣。"""

    card = _v1_card(comedic_payoff=4)
    proof = _closed_proof("comedic_payoff")
    result = _evaluate(
        card,
        proof_atoms=proof,
        topic_fingerprint="同题",
        session_fingerprint_counts={"同题": 2},
    )
    # 路径内分数不含疲劳；疲劳只体现在最终分上。
    assert result["path_scores_v2"]["comedic_payoff"]["score"] == pytest.approx(SOLO_PATH_BASE)
    assert result["quality_score_v2"] == pytest.approx(SOLO_PATH_BASE - 3.0)


def test_global_uncertainty_applies_after_max():
    result = _evaluate(
        _v1_card(comedic_payoff=4),
        proof_atoms=_closed_proof("comedic_payoff"),
        global_uncertainty=4.0,
    )
    assert result["path_scores_v2"]["comedic_payoff"]["score"] == pytest.approx(SOLO_PATH_BASE)
    assert result["quality_score_v2"] == pytest.approx(SOLO_PATH_BASE - 4.0)


# --------------------------------------------------------------------------
# Representative candidate scorecards for public metric regressions.
# --------------------------------------------------------------------------

REAL_TIER1 = {
    "auto_210739_1695_1804": (
        dict(
            lidousha_centrality=4, stance_intensity=2, audience_salience=2,
            relationship_interaction=4, persona_reversal=3, comedic_payoff=3,
            self_contained=4,
        ),
        2.0,
        "不适合发，没有看点",
    ),
    "auto_223750_734_822": (
        dict(
            lidousha_centrality=4, stance_intensity=2, audience_salience=2,
            relationship_interaction=4, persona_reversal=3, comedic_payoff=3,
            self_contained=3,
        ),
        4.0,
        "不适合发，主要发言人不是李豆沙",
    ),
    "auto_210739_727_840": (
        dict(
            lidousha_centrality=3, stance_intensity=3, audience_salience=2,
            relationship_interaction=3, persona_reversal=4, comedic_payoff=4,
            self_contained=4,
        ),
        7.0,
        "同样的问题，不需要发",
    ),
    "auto_223750_578_654": (
        dict(
            lidousha_centrality=3, stance_intensity=2, audience_salience=2,
            relationship_interaction=4, persona_reversal=4, comedic_payoff=3,
            self_contained=3,
        ),
        3.0,
        "最值得发，至少 90 分以上",
    ),
}


@pytest.mark.parametrize("candidate_id", sorted(REAL_TIER1))
def test_real_tier1_cards_have_no_verified_level_four(candidate_id: str):
    """真实历史卡一条证据原子都没有 → 没有任何单轴路径可以成立。

    这正是 Pro §五 要的行为：历史记录若没有新证据合同所需的证据，必须重跑验证。
    ⚠️ 因此这四条**全部**落回综合路径——本用例不演示 维护者 的排序，排序分离要等
    归属判定（②）就位，不许靠调权重去凑（真值文档已记：光靠降权只有 2.6 分差）。
    """

    dimensions, uncertainty, _verdict = REAL_TIER1[candidate_id]
    card = _v1_card(tier=1, uncertainty=uncertainty, **dimensions)
    # 真实卡的 tier_basis 是 relationship_chain；准入条件本用例不重演。
    result = _evaluate(card, proof_atoms=[])
    assert result["verified_level_4_axes"] == []
    assert result["winning_path_v2"] == "broad"
    # v1 双口径原样留痕。
    assert result["legacy_effective_v1"] == card["effective_score"]


def test_real_tier1_v2_is_not_tuned_to_correlate_with_v1():
    """v2 与 v1 的排序**本来就应该不同**——相关性不是目标（Pro §五）。"""

    v1_order = []
    v2_order = []
    for candidate_id, (dimensions, uncertainty, _v) in REAL_TIER1.items():
        card = _v1_card(tier=1, uncertainty=uncertainty, **dimensions)
        result = _evaluate(card, proof_atoms=[])
        v1_order.append((card["effective_score"], candidate_id))
        v2_order.append((result["quality_score_v2"], candidate_id))
    v1_ranked = [cid for _s, cid in sorted(v1_order, reverse=True)]
    v2_ranked = [cid for _s, cid in sorted(v2_order, reverse=True)]
    # 归属轴离开加权和后，两条 centrality=4 的候选不再靠它占位。
    assert v1_ranked != v2_ranked
    assert v1_ranked[0] == "auto_210739_1695_1804"
