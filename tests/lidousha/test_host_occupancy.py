"""候选级 Target Host Occupancy Estimator 的对抗性回归。

设计权威：`docs/reviews/evidence/2026-08-10-chatgpt-pro-ordering-and-rubric.txt`
（Ivan 已批准）+ `docs/reviews/2026-08-10-ivan-blind-review-tier1-ground-truth.md`

负向金丝雀（每条都对应一个"如果实现偷懒就会绿"的形态）：
① 声学同质 ≠ 单人 —— 高置信 HOST + 未过严格门 → 不许 SOLO      :: test_quiet_but_not_strict_*
② UNKNOWN 必停泊，绝不猜成 SOLO 放行                          :: test_unknown_*
③ 主播不是主体 → 归属已确定，**不许**送人工                    :: test_host_minor_*
④ 重叠候选区间并集去重 + 全局栅格 → 同窗只推理一次             :: test_overlapping_*
⑤ 前后扩窗必须发生，且越界要钳并披露                          :: test_padding_*
⑥ 单窗孤岛不许支撑 MULTI（最短持续时间约束）                   :: test_single_window_*
⑦ N=10 与补位有界，补位不许回收已剪枝候选                      :: test_contention_*
⑧ 争席排序不看 centrality（改它不许改次序）                    :: test_contention_order_ignores_centrality
"""

from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path

import pytest

from src.autoslice import host_occupancy as ho
from src.autoslice.selection_metric_v2 import (
    ATTRIBUTION_STATUSES,
    ATTRIBUTION_UNVERIFIED,
    ATTRIBUTION_VERIFIED_HOST_DOMINANT,
    ATTRIBUTION_VERIFIED_HOST_MINOR,
    ATTRIBUTION_VERIFIED_SOLO,
    evaluate_selection_metric_v2,
)
from src.autoslice.selection_scorecard import normalize_selection_scorecard
from src.autoslice.speaker_manual_review import SPEAKER_MANUAL_REVIEW_STATUSES

_BASE_DIMENSIONS = {
    "lidousha_centrality": 3,
    "stance_intensity": 2,
    "audience_salience": 2,
    "relationship_interaction": 3,
    "persona_reversal": 3,
    "comedic_payoff": 3,
    "self_contained": 3,
}


def _card(*, tier: int = 2, **overrides) -> dict:
    dimensions = dict(_BASE_DIMENSIONS)
    dimensions.update(overrides)
    card = normalize_selection_scorecard(
        {
            "tier": tier,
            "tier_basis": "personal_stance",
            "tier_reason": "host-occupancy canary",
            "tier_evidence_cues": [1, 2],
            "dimensions": dimensions,
            "uncertainty_penalty": 0.0,
            "fatigue_penalty": 0.0,
        },
        start_cue=1,
        end_cue=2,
    )
    assert card is not None
    return card


def _item(cid: str, *, confidence: float = 0.5, **overrides) -> dict:
    return {"cid": cid, "confidence": confidence, "selection_scorecard": _card(**overrides)}


def _windows(pattern: str, *, start_ms: int = 0) -> list[ho.WindowObservation]:
    """按字符拼窗口序列：H=HOST, O=OTHER, U=UNKNOWN, N=NON_SPEECH。

    栅格与生产一致（窗 2000ms / hop 1000ms），所以覆盖时长也与生产同口径。
    """

    labels = {
        "H": ho.LABEL_HOST,
        "O": ho.LABEL_OTHER,
        "U": ho.LABEL_UNKNOWN,
        "N": ho.LABEL_NON_SPEECH,
    }
    return [
        ho.WindowObservation(
            start_ms=start_ms + index * ho.HOP_MS,
            end_ms=start_ms + index * ho.HOP_MS + ho.WINDOW_MS,
            label=labels[character],
        )
        for index, character in enumerate(pattern)
    ]


# --------------------------------------------------------------------------
# ① 三态本身：SOLO 只能靠严格门挣来
# --------------------------------------------------------------------------


def test_clean_host_only_candidate_is_solo_verified() -> None:
    result = ho.aggregate_occupancy(_windows("H" * 30))
    assert result["state"] == ho.SOLO_VERIFIED
    assert result["host_speech_share"] == 1.0
    assert result["host_speech_share_band"] == ho.BAND_DOMINANT
    assert ho.map_attribution_status(result) == ATTRIBUTION_VERIFIED_SOLO


def test_quiet_but_not_strict_candidate_is_unknown_not_solo() -> None:
    """①声学同质 ≠ 单人。

    22 个 HOST 窗 + 4 个 UNKNOWN 窗：归属门（0.85/0.15/8s）全过，主播占比 1.00，
    也**没有**任何持续非主播语音——一个偷懒实现会说"没听见别人，那就是她一个人"。
    但 unknown_share 0.16 > SOLO 的 0.05，严格门不过 → 必须 UNKNOWN。
    ``acoustic_diversity_low`` 不能推出"一定是主播一个人"（Pro §2）。
    """

    result = ho.aggregate_occupancy(_windows("H" * 40 + "U" * 4))
    assert result["host_speech_share"] == 1.0
    assert result["gates"] == {
        "classified_coverage": True,
        "unknown_share": True,
        "classified_speech_ms": True,
    }
    assert result["longest_other_run_ms"] == 0
    assert result["state"] == ho.UNKNOWN
    assert "SOLO_AUDIT_FAILED_UNKNOWN_SHARE" in result["reason_codes"]
    assert "NO_SUSTAINED_NON_HOST_SPEECH_EITHER" in result["reason_codes"]


def test_sustained_other_speech_is_multi_verified() -> None:
    result = ho.aggregate_occupancy(_windows("H" * 20 + "O" * 10))
    assert result["state"] == ho.MULTI_VERIFIED
    assert result["reason_codes"] == ["SUSTAINED_NON_HOST_SPEECH"]
    assert result["longest_other_run_ms"] >= ho.MIN_OTHER_RUN_MS


def test_single_window_other_island_cannot_support_multi() -> None:
    """⑥ 最短持续时间约束：孤立单窗 OTHER 先被平滑成 UNKNOWN。"""

    smoothed = ho.smooth_labels(_windows("H" * 12 + "O" + "H" * 12))
    assert [item.label for item in smoothed].count(ho.LABEL_OTHER) == 0
    result = ho.aggregate_occupancy(smoothed)
    assert result["state"] != ho.MULTI_VERIFIED
    assert result["longest_other_run_ms"] == 0


def test_smoothing_only_moves_toward_abstain() -> None:
    smoothed = ho.smooth_labels(_windows("OHO"))
    assert [item.label for item in smoothed] == [ho.LABEL_UNKNOWN] * 3


def test_named_guest_evidence_forces_multi_before_solo_audit() -> None:
    result = ho.aggregate_occupancy(
        _windows("H" * 30), evidence=ho.MultiSpeakerEvidence(named_guest=True)
    )
    assert result["state"] == ho.MULTI_VERIFIED
    assert result["reason_codes"] == ["NAMED_GUEST_EVIDENCE"]


def test_attribution_gate_failure_precedes_every_other_verdict() -> None:
    """归属门不过时，连"有人在说别的"都不敢下结论。"""

    result = ho.aggregate_occupancy(_windows("H" * 4 + "U" * 10 + "O" * 4))
    assert result["state"] == ho.UNKNOWN
    assert any(
        code.startswith("ATTRIBUTION_GATE_FAILED") for code in result["reason_codes"]
    )


def test_too_little_classified_speech_is_unknown() -> None:
    result = ho.aggregate_occupancy(_windows("HHH"))
    assert result["classified_speech_ms"] < ho.MIN_CLASSIFIED_SPEECH_MS
    assert result["state"] == ho.UNKNOWN
    assert "ATTRIBUTION_GATE_FAILED_CLASSIFIED_SPEECH_MS" in result["reason_codes"]


def test_silence_only_candidate_is_unknown_not_solo() -> None:
    result = ho.aggregate_occupancy(_windows("N" * 30))
    assert result["speech_ms"] == 0
    assert result["state"] == ho.UNKNOWN
    assert result["host_speech_share"] is None
    assert result["host_speech_share_band"] == ho.BAND_UNAVAILABLE


def test_three_states_are_the_only_states() -> None:
    for pattern in ("H" * 30, "H" * 20 + "O" * 10, "H" * 22 + "U" * 4, "N" * 5):
        assert ho.aggregate_occupancy(_windows(pattern))["state"] in ho.OCCUPANCY_STATES


# --------------------------------------------------------------------------
# ② / ③ 三态 → attribution_status → 停泊
# --------------------------------------------------------------------------


def test_unknown_maps_to_unverified_and_parks() -> None:
    """② UNKNOWN 必停泊；绝不猜成 SOLO 放行。"""

    occupancy = ho.aggregate_occupancy(_windows("H" * 22 + "U" * 4))
    assert ho.map_attribution_status(occupancy) == ATTRIBUTION_UNVERIFIED
    assert ho.requires_manual_review(occupancy) is True
    record: dict = {"cid": "auto_x", "status": "pending"}
    receipt = ho.park_unverified_candidate(record, occupancy)
    assert receipt is not None
    assert record["status"] in SPEAKER_MANUAL_REVIEW_STATUSES
    assert receipt["upload_authorized"] is False
    assert receipt["review_authority"] == "HUMAN_OPERATOR"


def test_unknown_parked_candidate_is_not_deliverable() -> None:
    # 交付白名单是运行时入口的常量；停泊态天然不在里面（fail-closed 由 status
    # 承载，不由回执承载）。
    delivered = {"ok", "review_ready", "quarantine"}
    record: dict = {"cid": "auto_x", "status": "pending"}
    ho.park_unverified_candidate(record, ho.aggregate_occupancy(_windows("N" * 30)))
    assert record["status"] not in delivered
    assert record["status"] in SPEAKER_MANUAL_REVIEW_STATUSES


def test_host_minor_is_verified_and_never_parks() -> None:
    """③ Ivan「主要发言人不是李豆沙」的两条：归属**已确定**，不进人工队列。

    「最好是能够自然给出低分，而不是强制压低」——低分要靠 centrality 看见
    ``[其他]`` 标签自然得到，不靠把一条已确定的候选伪装成存疑。
    """

    occupancy = ho.aggregate_occupancy(_windows("H" * 8 + "O" * 22))
    assert occupancy["state"] == ho.MULTI_VERIFIED
    assert occupancy["host_speech_share"] < ho.HOST_DOMINANT_MIN_SHARE
    assert ho.map_attribution_status(occupancy) == ATTRIBUTION_VERIFIED_HOST_MINOR
    assert ho.requires_manual_review(occupancy) is False
    record: dict = {"cid": "auto_x", "status": "pending"}
    assert ho.park_unverified_candidate(record, occupancy) is None
    assert record["status"] == "pending"
    assert "speaker_manual_review" not in record


def test_host_dominant_multi_maps_to_host_dominant() -> None:
    occupancy = ho.aggregate_occupancy(_windows("H" * 24 + "O" * 6))
    assert occupancy["state"] == ho.MULTI_VERIFIED
    assert occupancy["host_speech_share"] >= ho.HOST_DOMINANT_MIN_SHARE
    assert ho.map_attribution_status(occupancy) == ATTRIBUTION_VERIFIED_HOST_DOMINANT


def test_every_mapped_status_is_accepted_by_metric_v2() -> None:
    """映射进的是既有词汇，不是第三套命名。"""

    for pattern in ("H" * 30, "H" * 24 + "O" * 6, "H" * 8 + "O" * 22, "N" * 30):
        status = ho.map_attribution_status(ho.aggregate_occupancy(_windows(pattern)))
        assert status in ATTRIBUTION_STATUSES
        result = evaluate_selection_metric_v2(
            scorecard=_card(),
            attribution_status=status,
            candidate_start_ms=0,
            candidate_end_ms=100_000,
        )
        assert result is not None
        assert result["attribution_status"] == status


def test_metric_v2_parks_only_unverified() -> None:
    for status, parked in (
        (ATTRIBUTION_VERIFIED_SOLO, False),
        (ATTRIBUTION_VERIFIED_HOST_DOMINANT, False),
        (ATTRIBUTION_VERIFIED_HOST_MINOR, False),
        (ATTRIBUTION_UNVERIFIED, True),
    ):
        result = evaluate_selection_metric_v2(
            scorecard=_card(),
            attribution_status=status,
            candidate_start_ms=0,
            candidate_end_ms=100_000,
        )
        assert result is not None
        assert result["requires_speaker_manual_review"] is parked


def test_multi_verified_without_share_fails_closed() -> None:
    with_no_share = {"state": ho.MULTI_VERIFIED, "host_speech_share": None}
    assert ho.map_attribution_status(with_no_share) == ATTRIBUTION_UNVERIFIED


def test_unknown_occupancy_state_is_rejected() -> None:
    with pytest.raises(ho.HostOccupancyError):
        ho.map_attribution_status({"state": "PROBABLY_HER"})


# --------------------------------------------------------------------------
# 标签按时间对齐到现有 ASR（centrality rubric 唯一被允许的身份来源）
# --------------------------------------------------------------------------


def _labelled(pattern: str):
    return _windows(pattern)


def test_cue_labels_follow_time_overlap_only() -> None:
    observations = _labelled("HHHOOO")
    cues = [
        {"cue_id": "a", "start_ms": 0, "end_ms": 3_000},
        {"cue_id": "b", "start_ms": 4_000, "end_ms": 7_000},
    ]
    labels = {row["cue_id"]: row["speaker_label"] for row in ho.label_cues(cues, observations)}
    assert labels == {"a": ho.CUE_LABEL_HOST, "b": ho.CUE_LABEL_OTHER}


def test_uncovered_cue_is_uncertain_not_nearest_neighbour() -> None:
    """覆盖不足不许"就近取一个"——rubric 明文禁止自行认定未标注话语。"""

    rows = ho.label_cues(
        [{"cue_id": "z", "start_ms": 500_000, "end_ms": 502_000}], _labelled("HHHH")
    )
    assert rows[0]["speaker_label"] == ho.CUE_LABEL_UNCERTAIN
    assert rows[0]["overlap_share"] == 0.0


def test_partially_covered_cue_is_uncertain() -> None:
    rows = ho.label_cues(
        [{"cue_id": "p", "start_ms": 0, "end_ms": 10_000}], _labelled("HH")
    )
    assert rows[0]["overlap_share"] < ho.CUE_LABEL_MIN_OVERLAP_SHARE
    assert rows[0]["speaker_label"] == ho.CUE_LABEL_UNCERTAIN


def test_tied_coverage_is_uncertain_not_alphabetical() -> None:
    """平票不许靠标签名的字典序决定归属。"""

    observations = [
        ho.WindowObservation(start_ms=0, end_ms=2_000, label=ho.LABEL_HOST),
        ho.WindowObservation(start_ms=2_000, end_ms=4_000, label=ho.LABEL_OTHER),
    ]
    rows = ho.label_cues([{"cue_id": "t", "start_ms": 0, "end_ms": 4_000}], observations)
    assert rows[0]["speaker_label"] == ho.CUE_LABEL_UNCERTAIN


def test_unknown_windows_never_produce_a_speaker_label() -> None:
    rows = ho.label_cues([{"cue_id": "u", "start_ms": 0, "end_ms": 3_000}], _labelled("UUU"))
    assert rows[0]["speaker_label"] == ho.CUE_LABEL_UNCERTAIN


def test_key_moment_with_uncertain_speaker_blocks_scoring() -> None:
    """rubric 规则 4：关键落点是 [存疑] → 不得输出 0–4 分。"""

    labeled = ho.label_cues(
        [
            {"cue_id": "setup", "start_ms": 0, "end_ms": 3_000},
            {"cue_id": "payoff", "start_ms": 500_000, "end_ms": 502_000},
        ],
        _labelled("HHHH"),
    )
    assert ho.key_moments_are_attributed(labeled, ["setup"]) is True
    assert ho.key_moments_are_attributed(labeled, ["setup", "payoff"]) is False
    assert ho.key_moments_are_attributed(labeled, []) is False
    assert ho.key_moments_are_attributed(labeled, ["absent"]) is False


# --------------------------------------------------------------------------
# 双阈值：重叠带全部 abstain
# --------------------------------------------------------------------------


def test_dual_threshold_band_abstains() -> None:
    assert ho.OTHER_SIMILARITY_MAX < ho.HOST_SIMILARITY_MIN
    assert ho.classify_score(ho.HOST_SIMILARITY_MIN) == ho.LABEL_HOST
    assert ho.classify_score(ho.OTHER_SIMILARITY_MAX) == ho.LABEL_OTHER
    midpoint = (ho.HOST_SIMILARITY_MIN + ho.OTHER_SIMILARITY_MAX) / 2
    assert ho.classify_score(midpoint) == ho.LABEL_UNKNOWN
    assert ho.classify_score(ho.HOST_SIMILARITY_MIN - 1e-9) == ho.LABEL_UNKNOWN
    assert ho.classify_score(ho.OTHER_SIMILARITY_MAX + 1e-9) == ho.LABEL_UNKNOWN


def test_non_finite_similarity_is_rejected() -> None:
    with pytest.raises(ho.HostOccupancyError):
        ho.classify_score(math.nan)


def test_band_edges_match_pro_table() -> None:
    assert ho.occupancy_band(0.05) == ho.BAND_TRACE
    assert ho.occupancy_band(0.10) == ho.BAND_LOW
    assert ho.occupancy_band(0.29) == ho.BAND_LOW
    assert ho.occupancy_band(0.30) == ho.BAND_MIXED
    assert ho.occupancy_band(0.59) == ho.BAND_MIXED
    assert ho.occupancy_band(0.60) == ho.BAND_DOMINANT
    assert ho.occupancy_band(None) == ho.BAND_UNAVAILABLE


# --------------------------------------------------------------------------
# ④ / ⑤ 区间：扩窗、钳边界、并集去重、全局栅格
# --------------------------------------------------------------------------


def test_padding_extends_both_sides() -> None:
    padded = ho.pad_candidate(
        ho.CandidateSpan("c", 100_000, 160_000), source_duration_ms=1_800_000
    )
    assert padded.start_ms == 100_000 - ho.CANDIDATE_PAD_MS
    assert padded.end_ms == 160_000 + ho.CANDIDATE_PAD_MS
    assert padded.head_pad_truncated_ms == 0
    assert padded.tail_pad_truncated_ms == 0


def test_padding_clamps_at_segment_bounds_and_discloses() -> None:
    """⑤ 段尾候选（auto_210739_1695_1804 就是这种）不许越界读，截断要披露。"""

    padded = ho.pad_candidate(
        ho.CandidateSpan("c", 3_000, 1_798_000), source_duration_ms=1_800_000
    )
    assert padded.start_ms == 0
    assert padded.end_ms == 1_800_000
    assert padded.head_pad_truncated_ms == ho.CANDIDATE_PAD_MS - 3_000
    assert padded.tail_pad_truncated_ms == ho.CANDIDATE_PAD_MS - 2_000


def test_overlapping_candidates_collapse_into_one_union_span() -> None:
    """④ 重叠候选先求并集，避免重复推理。"""

    spans = [
        ho.pad_candidate(ho.CandidateSpan("a", 100_000, 140_000), source_duration_ms=900_000),
        ho.pad_candidate(ho.CandidateSpan("b", 135_000, 180_000), source_duration_ms=900_000),
        ho.pad_candidate(ho.CandidateSpan("c", 500_000, 520_000), source_duration_ms=900_000),
    ]
    assert ho.union_spans(spans) == [
        (100_000 - ho.CANDIDATE_PAD_MS, 180_000 + ho.CANDIDATE_PAD_MS),
        (500_000 - ho.CANDIDATE_PAD_MS, 520_000 + ho.CANDIDATE_PAD_MS),
    ]


def test_window_grid_is_globally_anchored_not_candidate_anchored() -> None:
    """④ 两条错位候选共用的窗口必须是**同一批**起止点，否则缓存永不命中。"""

    left = set(ho.grid_windows(100_000, 140_000))
    right = set(ho.grid_windows(100_500, 140_000))
    # 错位 500ms 的跨度不许把栅格也挪 500ms：窗口起点是**绝对**栅格点，于是
    # 错位跨度的窗口是对齐跨度窗口的真子集，共用的那些逐字节相同。
    assert right < left
    assert min(start for start, _ in right) == 101_000
    assert min(start for start, _ in left) == 100_000
    assert all(start % ho.HOP_MS == ho.GRID_ANCHOR_MS for start, _ in left | right)
    assert all(start >= 100_500 for start, _ in right)


def test_partial_window_is_not_zero_padded() -> None:
    assert ho.grid_windows(0, ho.WINDOW_MS - 1) == []
    assert ho.grid_windows(0, ho.WINDOW_MS) == [(0, ho.WINDOW_MS)]


def test_padded_span_must_be_inside_the_source() -> None:
    with pytest.raises(ho.HostOccupancyError):
        ho.pad_candidate(ho.CandidateSpan("c", 10, 20), source_duration_ms=0)


def test_overlapping_windows_are_not_double_counted() -> None:
    """窗口 50% 重叠：占比必须按覆盖时长算，不是窗数 × 窗长。"""

    result = ho.aggregate_occupancy(_windows("H" * 10))
    assert result["host_speech_ms"] == 9 * ho.HOP_MS + ho.WINDOW_MS


# --------------------------------------------------------------------------
# VAD
# --------------------------------------------------------------------------


def test_frame_rms_matches_hand_computation() -> None:
    samples = [3, 4, 0, 0]
    assert ho.frame_rms(samples, frame_length=2) == pytest.approx(
        [math.sqrt(12.5), 0.0]
    )


def test_silence_is_not_a_speech_window() -> None:
    flags = ho.speech_frame_flags([0.0] * 20)
    assert ho.window_is_speech(flags) is False


def test_loud_frames_are_speech_windows() -> None:
    flags = ho.speech_frame_flags([2_000.0] * 20)
    assert ho.window_is_speech(flags) is True


def test_half_silent_window_still_counts_as_speech() -> None:
    values = [2_000.0] * 10 + [0.0] * 10
    assert ho.window_is_speech(ho.speech_frame_flags(values)) is True


# --------------------------------------------------------------------------
# ⑦ / ⑧ 两段式争席次序
# --------------------------------------------------------------------------


def test_contention_order_ignores_centrality() -> None:
    """⑧ 召回打分不打 centrality：改它一个字都不许改争席次序。"""

    low = _item("low_centrality", lidousha_centrality=0, stance_intensity=4)
    high = _item("high_centrality", lidousha_centrality=4, stance_intensity=1)
    assert ho.contention_rank_key(low) < ho.contention_rank_key(high)
    baseline = [ho.contention_rank_key(item) for item in (low, high)]
    for level in range(5):
        moved = _item("low_centrality", lidousha_centrality=level, stance_intensity=4)
        assert ho.contention_rank_key(moved) == baseline[0]


def test_base_score_excludes_centrality_and_keeps_v1_scale() -> None:
    card = _card(lidousha_centrality=4)
    assert ho.base_score(card) == pytest.approx(
        sum(
            ho.DIMENSION_WEIGHTS[axis] * _BASE_DIMENSIONS[axis] / 4
            for axis in ho.BASE_AXES
        )
    )
    lower, upper = ho.score_bounds(_item("x"))  # type: ignore[misc]
    assert upper - lower == ho.CENTRALITY_WEIGHT == 25.0


def test_contention_set_takes_top_n() -> None:
    """⑦ Ivan：「N 可以选 10 个」。"""

    assert ho.CONTENTION_SET_SIZE == 10
    items = [_item(f"c{index:02d}", confidence=index / 100) for index in range(25)]
    result = ho.build_contention_set(items)
    assert len(result.admitted) == 10
    assert len(result.deferred) == 15
    assert result.detections_spent == 10


def test_contention_set_prunes_by_upper_bound_without_audio() -> None:
    """Pro §1 安全剪枝：B+25 < τ 的候选连音频都不跑。"""

    weak = _item("weak", **{axis: 0 for axis in ho.BASE_AXES})
    strong = _item("strong", **{axis: 4 for axis in ho.BASE_AXES})
    result = ho.build_contention_set([weak, strong], publish_threshold=90.0)
    assert result.pruned == ("weak",)
    assert result.admitted == ("strong",)


def test_backfill_is_bounded_and_replaces_only_vacated_seats() -> None:
    """⑦「不够了再补上」= 有界补位。"""

    items = [_item(f"c{index:02d}", confidence=(30 - index) / 100) for index in range(30)]
    first = ho.build_contention_set(items)
    vacated = first.admitted[:3]
    second = ho.backfill_contention_set(first, items, vacated=vacated)
    assert len(second.admitted) == 10
    assert set(vacated) & set(second.admitted) == set()
    assert len(second.backfilled) == 3
    assert second.detections_spent == 13


def test_backfill_stops_at_the_detection_cap() -> None:
    items = [_item(f"c{index:02d}", confidence=(40 - index) / 100) for index in range(40)]
    current = ho.build_contention_set(items)
    rounds = 0
    while current.admitted and rounds < 10:
        current = ho.backfill_contention_set(current, items, vacated=current.admitted)
        rounds += 1
    assert current.detections_spent <= ho.CONTENTION_DETECTION_CAP
    assert ho.CONTENTION_DETECTION_CAP == 2 * ho.CONTENTION_SET_SIZE


def test_backfill_never_recycles_pruned_candidates() -> None:
    weak = _item("weak", **{axis: 0 for axis in ho.BASE_AXES})
    others = [_item(f"c{index:02d}", confidence=(20 - index) / 100) for index in range(20)]
    first = ho.build_contention_set([weak, *others], publish_threshold=90.0)
    assert "weak" in first.pruned
    second = ho.backfill_contention_set(
        first, [weak, *others], vacated=first.admitted[:2], publish_threshold=90.0
    )
    assert "weak" not in second.admitted


def test_backfill_rejects_foreign_vacancies() -> None:
    items = [_item(f"c{index:02d}") for index in range(12)]
    first = ho.build_contention_set(items)
    with pytest.raises(ho.HostOccupancyError):
        ho.backfill_contention_set(first, items, vacated=("not_admitted",))


def test_already_detected_budget_shrinks_the_next_set() -> None:
    items = [_item(f"c{index:02d}", confidence=(30 - index) / 100) for index in range(30)]
    result = ho.build_contention_set(
        items, already_detected=[f"prev{index}" for index in range(15)]
    )
    assert len(result.admitted) == ho.CONTENTION_DETECTION_CAP - 15
    assert result.capped is True


def test_invalid_scorecards_rank_as_tier_three() -> None:
    unscored = {"cid": "legacy", "confidence": 0.9}
    scored = _item("scored", tier=1)
    assert ho.contention_rank_key(scored) < ho.contention_rank_key(unscored)
    assert ho.score_bounds(unscored) is None


# --------------------------------------------------------------------------
# 端到端（注入假抽取器与假打分器；不加载 ML runtime、不碰 ffmpeg）
# --------------------------------------------------------------------------


def _write_wav(path: Path, samples) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(ho.SAMPLE_RATE_HZ)
        handle.writeframes(array("h", samples).tobytes())
    return path


def _tone(duration_ms: int, amplitude: int = 6_000) -> list[int]:
    count = duration_ms * ho.SAMPLE_RATE_HZ // 1000
    return [amplitude if index % 2 else -amplitude for index in range(count)]


def test_end_to_end_dedupes_overlapping_window_inference(tmp_path: Path) -> None:
    """④ embed-once：两条重叠候选共用的窗口只推理一次。"""

    source_duration_ms = 200_000
    prototype = _write_wav(tmp_path / "enroll.wav", _tone(2_000))
    calls: list[tuple[str, str]] = []
    embedded: set[str] = set()

    def fake_extract(_source, *, start_ms, end_ms, output_path):
        return _write_wav(output_path, _tone(end_ms - start_ms))

    def fake_similarity(left: Path, right: Path) -> float:
        calls.append((left.name, right.name))
        embedded.add(left.read_bytes().hex()[:16] + left.name)
        return 0.9

    spans = [
        ho.CandidateSpan("a", 60_000, 100_000),
        ho.CandidateSpan("b", 90_000, 130_000),
    ]
    reports = ho.run_estimator(
        source_media=Path("/dev/null"),
        source_duration_ms=source_duration_ms,
        spans=spans,
        prototypes={"p1": prototype},
        similarity=fake_similarity,
        work_dir=tmp_path / "work",
        extract=fake_extract,
    )
    assert [report["candidate_id"] for report in reports] == ["a", "b"]
    # 一个并集跨度 → 每个栅格窗口恰好一次前向；重叠区不重复。
    union = ho.union_spans(
        [ho.pad_candidate(span, source_duration_ms=source_duration_ms) for span in spans]
    )
    assert len(union) == 1
    assert len(calls) == len(ho.grid_windows(*union[0]))
    assert len({name for name, _ in calls}) == len(calls)
    for report in reports:
        assert report["occupancy"]["state"] == ho.SOLO_VERIFIED
        assert report["threshold_version"] == ho.THRESHOLD_VERSION
        assert report["provisional_calibration"] is True


def test_end_to_end_occupancy_uses_candidate_span_not_padding(tmp_path: Path) -> None:
    """⑤ 扩窗只作上下文/披露；占比按候选自身区间算。"""

    prototype = _write_wav(tmp_path / "enroll.wav", _tone(2_000))

    def fake_extract(_source, *, start_ms, end_ms, output_path):
        return _write_wav(output_path, _tone(end_ms - start_ms))

    def fake_similarity(left: Path, _right: Path) -> float:
        start_ms = int(left.name.split("_")[1])
        return 0.1 if start_ms < 60_000 else 0.9

    report = ho.run_estimator(
        source_media=Path("/dev/null"),
        source_duration_ms=200_000,
        spans=[ho.CandidateSpan("a", 60_000, 120_000)],
        prototypes={"p1": prototype},
        similarity=fake_similarity,
        work_dir=tmp_path / "work",
        extract=fake_extract,
    )[0]
    # 扩窗里那 8 秒全是别人；候选自身区间几乎全是主播。两份分开记，占比只看
    # 候选区间——否则 8 秒前摇能把一条干净的独白判成多人。
    assert report["occupancy"]["host_speech_share"] > 0.95
    assert report["context_occupancy"]["host_speech_share"] < 0.90
    assert report["padded_start_ms"] == 60_000 - ho.CANDIDATE_PAD_MS


def test_end_to_end_silence_is_unknown_and_parks(tmp_path: Path) -> None:
    """② 抽不到语音 → UNKNOWN → 停泊，不是"没听见别人所以是她"。"""

    prototype = _write_wav(tmp_path / "enroll.wav", _tone(2_000))

    def fake_extract(_source, *, start_ms, end_ms, output_path):
        return _write_wav(output_path, [0] * ((end_ms - start_ms) * ho.SAMPLE_RATE_HZ // 1000))

    def fake_similarity(_left: Path, _right: Path) -> float:  # pragma: no cover
        raise AssertionError("silent windows must never reach CAM++")

    report = ho.run_estimator(
        source_media=Path("/dev/null"),
        source_duration_ms=200_000,
        spans=[ho.CandidateSpan("a", 60_000, 120_000)],
        prototypes={"p1": prototype},
        similarity=fake_similarity,
        work_dir=tmp_path / "work",
        extract=fake_extract,
    )[0]
    assert report["occupancy"]["state"] == ho.UNKNOWN
    assert report["attribution_status"] == ATTRIBUTION_UNVERIFIED
    assert report["requires_speaker_manual_review"] is True


def test_empty_prototype_set_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ho.HostOccupancyError):
        ho.observe_span(
            span_start_ms=0,
            span_end_ms=10_000,
            samples=_tone(10_000),
            prototypes={},
            similarity=lambda _a, _b: 0.9,
            window_dir=tmp_path,
        )


def test_multiple_prototypes_take_the_best_match(tmp_path: Path) -> None:
    """Pro：保留 3–5 个 prototype 而非单一 centroid → 取最佳匹配。"""

    left = _write_wav(tmp_path / "p1.wav", _tone(2_000))
    right = _write_wav(tmp_path / "p2.wav", _tone(2_000, amplitude=5_000))
    observations = ho.observe_span(
        span_start_ms=0,
        span_end_ms=6_000,
        samples=_tone(6_000),
        prototypes={"p1": left, "p2": right},
        similarity=lambda _window, reference: 0.2 if reference.name == "p1.wav" else 0.8,
        window_dir=tmp_path / "w",
    )
    assert observations
    for observation in observations:
        assert observation.best_prototype == "p2"
        assert observation.label == ho.LABEL_HOST
        assert set(observation.prototype_scores) == {"p1", "p2"}


def test_wav_roundtrip_is_sample_exact(tmp_path: Path) -> None:
    samples = _tone(50)
    path = ho.write_window_wav(samples, tmp_path / "w.wav")
    assert list(ho.read_wav_samples(path)) == samples


def test_foreign_format_wav_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44_100)
        handle.writeframes(b"\x00" * 100)
    with pytest.raises(ho.HostOccupancyError):
        ho.read_wav_samples(path)


def test_candidate_without_covering_span_fails_closed() -> None:
    padded = ho.pad_candidate(ho.CandidateSpan("a", 60_000, 90_000), source_duration_ms=200_000)
    with pytest.raises(ho.HostOccupancyError):
        ho.candidate_observations(
            padded, {}, candidate_start_ms=60_000, candidate_end_ms=90_000
        )
