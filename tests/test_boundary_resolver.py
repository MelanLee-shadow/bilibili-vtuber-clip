import hashlib

import pytest

from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.boundary_resolver import (
    AnchorCandidate,
    BoundaryPolicy,
    TalkCue,
    resolve_talk_boundary,
)
from src.autoslice.jingting_chunker import SrtCue
from src.autoslice.producer_boundary import boundary_red_flags
from src.autoslice import producer_boundary_resolution as boundary_resolution
from src.autoslice.producer_boundary_resolution import (
    _current_delivery_closure_binding,
    _current_delivery_source_extension,
)


def cue(cue_id, start_s, end_s, text, **overrides):
    data = {
        "cue_id": cue_id,
        "start_ms": int(start_s * 1000),
        "end_ms": int(end_s * 1000),
        "text": text,
        "starts_topic": False,
        "ends_topic": False,
        "has_payoff": False,
        "open_loop_delta": 0,
    }
    data.update(overrides)
    return TalkCue(**data)


class _SanitizedCue:
    def __init__(self, text):
        self.text = text


def _current_projection(cues):
    rows = [
        {
            "cue_index": cue.index,
            "delivery_start_ms": cue.start_ms - 35_020,
            "delivery_end_ms": cue.end_ms - 35_020,
            "source_start_ms": cue.start_ms,
            "source_end_ms": cue.end_ms,
            "text_sha256": "sha256:" + hashlib.sha256(cue.text.encode("utf-8")).hexdigest(),
        }
        for cue in cues
    ]
    return {
        "schema_version": "delivery-to-source-timeline-projection.v1",
        "status": "PASS",
        "cue_count": len(cues),
        "projected_source_cue_grid_sha256": cue_grid_sha256(cues),
        "source_final_start_ms": cues[0].start_ms if cues else 0,
        "source_final_end_ms": cues[-1].end_ms if cues else 0,
        "source_last_end_ms": cues[-1].end_ms if cues else 0,
        "rows": rows,
    }


def test_current_delivery_closure_uses_bound_terminal_text_without_rebinding_source():
    source_closure = SrtCue("45", 101_100, 103_360, "不描绘到是什么感觉了")
    current = [SrtCue("30", 101_100, 103_360, "不必要的会是什么感觉了")]

    delivery_text, binding = _current_delivery_closure_binding(
        closure_cue=source_closure,
        snapped_end_ms=103_360,
        current_delivery_cues=current,
        current_delivery_projection=_current_projection(current),
    )

    assert delivery_text == current[0].text
    assert source_closure.text == "不描绘到是什么感觉了"
    assert binding["status"] == "PASS"
    assert binding["source_start_ms"] == 101_100
    assert binding["source_end_ms"] == 103_360
    assert binding["source_closure_text_sha256"] != binding["current_delivery_closure_text_sha256"]
    assert boundary_red_flags(
        audit={"end_island_continues_ms": 0},
        cues=current,
        sanitized=[_SanitizedCue(delivery_text)],
        final_start_ms=35_020,
        final_end_ms=103_490,
        snapped_end_ms=103_360,
        closure_text=delivery_text,
    ) == []


def test_legacy_closure_adds_no_current_projection_or_endpoint_requirement():
    source_closure = SrtCue("45", 101_100, 103_360, "原始末句")
    text, binding = _current_delivery_closure_binding(
        closure_cue=source_closure,
        snapped_end_ms=103_300,
        current_delivery_cues=None,
        current_delivery_projection=None,
    )
    assert text == source_closure.text
    assert binding["status"] == "NOT_APPLICABLE"


def test_current_unchanged_caption_tail_keeps_source_endpoint_and_current_timing():
    source = SrtCue("42", 95_950, 96_910, "嗯")
    current = [SrtCue("38", 95_950, 96_950, "嗯")]
    projection = _current_projection(current)
    projection["source_final_end_ms"] = 97_310
    text, binding = _current_delivery_closure_binding(
        closure_cue=source, snapped_end_ms=96_910,
        current_delivery_cues=current, current_delivery_projection=projection,
        media_end_ms=97_310,
    )
    assert text == "嗯"
    assert binding["source_end_ms"] == source.end_ms == 96_910
    assert binding["current_source_end_ms"] == current[0].end_ms == 96_950
    assert binding["projection_rows_verified"] is True
    assert binding["unchanged_caption_tail_in_covered_media"] is True


@pytest.mark.parametrize("failure", ["media", "reviewed", "text", "overlap", "row"])
def test_current_caption_tail_does_not_waive_media_or_utterance_binding(failure):
    source = SrtCue("42", 95_950, 96_910, "嗯")
    current = [SrtCue("38", 95_950, 96_950, "嗯")]
    projection = _current_projection(current)
    projection["source_final_end_ms"] = 97_310
    media_end = 97_310
    if failure == "media":
        media_end = 96_910
    elif failure == "reviewed":
        projection["source_final_end_ms"] = 96_910
    elif failure == "text":
        current = [SrtCue("38", 95_950, 96_950, "新一句")]
    elif failure == "overlap":
        current.append(SrtCue("39", 96_000, 96_950, "嗯"))
    else:
        projection["rows"][0]["text_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(SystemExit, match="BOUNDARY_CURRENT_DELIVERY_CLOSURE"):
        _current_delivery_closure_binding(
            closure_cue=source, snapped_end_ms=96_910,
            current_delivery_cues=current, current_delivery_projection=projection,
            media_end_ms=media_end,
        )


@pytest.mark.parametrize(
    ("current", "match"),
    [
        ([], "UNBOUND"),
        (
            [
                SrtCue("30a", 101_100, 103_360, "当前终点一"),
                SrtCue("30b", 101_100, 103_360, "当前终点二"),
            ],
            "AMBIGUOUS",
        ),
        ([SrtCue("30", 101_100, 103_500, "越过源端点")], "CROSSING"),
        ([SrtCue("30", 101_100, 103_000, "时序漂移")], "TIMING_DRIFT"),
    ],
)
def test_current_delivery_closure_binding_fails_closed_on_missing_ambiguous_or_drifted_terminal(
    current, match
):
    source_closure = SrtCue("45", 101_100, 103_360, "源端点")
    projection = _current_projection(current)
    if not current:
        projection["source_last_end_ms"] = 103_360
    with pytest.raises(SystemExit, match=f"BOUNDARY_CURRENT_DELIVERY_CLOSURE_{match}"):
        _current_delivery_closure_binding(
            closure_cue=source_closure,
            snapped_end_ms=103_360,
            current_delivery_cues=current,
            current_delivery_projection=projection,
        )


def test_current_delivery_projection_text_hash_drift_is_not_accepted():
    source_closure = SrtCue("45", 101_100, 103_360, "源端点")
    current = [SrtCue("30", 101_100, 103_360, "当前端点")]
    projection = _current_projection(current)
    projection["rows"][0]["text_sha256"] = "sha256:" + "0" * 64

    with pytest.raises(SystemExit, match="BOUNDARY_CURRENT_DELIVERY_CLOSURE_TEXT_HASH_DRIFT"):
        _current_delivery_closure_binding(
            closure_cue=source_closure,
            snapped_end_ms=103_360,
            current_delivery_cues=current,
            current_delivery_projection=projection,
        )


def _source_extension_case():
    source = [
        SrtCue("1", 35_020, 40_000, "原草稿错字"),
        SrtCue("2", 40_000, 43_000, "后续互动"),
        SrtCue("3", 43_000, 45_000, "完整收尾"),
        SrtCue("4", 46_000, 49_000, "下一个话题"),
    ]
    current = [SrtCue("1", 35_020, 40_000, "已经正常修好的专名")]
    review = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "review_scope": "source_full_window",
        "status": "PASS",
        "request_sha256": "sha256:" + "1" * 64,
        "cue_grid_sha256": cue_grid_sha256(source),
        "recommended_end_cue_index": 3,
        "recommended_end_ms": 45_000,
    }
    return source, current, review


def test_source_boundary_extension_keeps_typed_prefix_and_adds_only_pending_draft():
    source, current, review = _source_extension_case()
    projection = _current_projection(current)
    original_projection = repr(projection)
    combined, binding = _current_delivery_source_extension(
        source_cues=source, current_delivery_cues=current,
        current_delivery_projection=projection, semantic_review=review,
        closure_cue=source[2], snapped_end_ms=45_000,
        final_start_ms=35_020, final_end_ms=45_000,
        manual_end_mode="semantic_lower_bound",
    )
    assert [c.text for c in combined] == ["已经正常修好的专名", "后续互动", "完整收尾"]
    assert current[0].text == "已经正常修好的专名"
    assert source[0].text == "原草稿错字"
    assert repr(projection) == original_projection
    assert binding["current_prefix_closure_binding"]["status"] == "PASS"
    assert binding["source_boundary_endpoint_binding"]["status"] == "PASS"
    assert binding["status"] == "PENDING_FINAL_REVIEW"
    assert binding["delivery_text_authorized"] is False
    assert binding["requires_current_complete_final_review"] is True
    assert binding["release_gate"] == "BLOCK"


@pytest.mark.parametrize("drift", ["grid", "endpoint", "index", "status", "scope", "request", "manual", "prefix"])
def test_source_boundary_extension_rejects_drift_or_missing_current_authority(drift):
    source, current, review = _source_extension_case()
    projection = _current_projection(current)
    mode = "semantic_lower_bound"
    if drift == "grid":
        review["cue_grid_sha256"] = "sha256:" + "0" * 64
    elif drift == "endpoint":
        review["recommended_end_ms"] = 44_000
    elif drift == "index":
        review["recommended_end_cue_index"] = 2
    elif drift == "status":
        review["status"] = "BLOCK"
    elif drift == "scope":
        review["review_scope"] = "final_delivery"
    elif drift == "request":
        review.pop("request_sha256")
    elif drift == "manual":
        mode = "exact_source_pin"
    else:
        projection["rows"][0]["text_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(SystemExit, match="BOUNDARY_CURRENT_DELIVERY"):
        _current_delivery_source_extension(
            source_cues=source, current_delivery_cues=current,
            current_delivery_projection=projection, semantic_review=review,
            closure_cue=source[2], snapped_end_ms=45_000,
            final_start_ms=35_020, final_end_ms=45_000,
            manual_end_mode=mode,
        )


def test_source_boundary_extension_does_not_trim_a_crossing_new_utterance():
    source, current, review = _source_extension_case()
    source[1] = SrtCue("2", 39_900, 43_000, "跨过旧字幕末端的新句子")
    review["cue_grid_sha256"] = cue_grid_sha256(source)
    with pytest.raises(SystemExit, match="EXTENSION_CROSSES_PREFIX"):
        _current_delivery_source_extension(
            source_cues=source, current_delivery_cues=current,
            current_delivery_projection=_current_projection(current), semantic_review=review,
            closure_cue=source[2], snapped_end_ms=45_000,
            final_start_ms=35_020, final_end_ms=45_000,
            manual_end_mode="semantic_lower_bound",
        )


def test_normal_boundary_repair_passes_extended_draft_to_timing_without_sealing_text(
    tmp_path, monkeypatch,
):
    source, current, review = _source_extension_case()
    observed = {}
    monkeypatch.setattr(boundary_resolution, "_resolved_tail_adjustment", lambda *_a, **_k: {
        "final_end_ms": 45_000, "nominal_end_ms": 45_000,
        "next_speech_island_start_ms": 46_000, "next_speech_island_guard_ms": 100,
        "reason": None,
    })
    monkeypatch.setattr(boundary_resolution, "boundary_audit", lambda *_a, **_k: {
        "verdict": "ok_sentence_boundary_cut", "end_island_continues_ms": 0,
    })
    def sanitize(cues, *_args, **_kwargs):
        observed["draft_texts"] = [c.text for c in cues]
        return [_SanitizedCue(c.text) for c in cues], {}
    monkeypatch.setattr(boundary_resolution, "sanitize_cue_timing", sanitize)
    monkeypatch.setattr(boundary_resolution, "boundary_red_flags", lambda **_k: [])

    result = boundary_resolution._repair_boundary(
        cid="extended-current", out_root=tmp_path, padded_dur=50_000,
        spans=[], cues=source, target_start_rel=35_020, snapped_start=35_020,
        final_start=35_020, target_rel=45_000, closure_selection_lower_bound_ms=45_000,
        repair_search_origin_ms=40_000, repair_max_end_ms=50_000, snapped=45_000,
        closure_cue=source[2], refinement_used=False, manual_end_authority=None,
        manual_end_mode="semantic_lower_bound", semantic_review=review,
        required_boundary_owners=[], required_owner_start_ms=None,
        required_owner_end_ms=None, boundary_repair_extend_cap_ms=30_000,
        current_delivery_cues=current, current_delivery_projection=_current_projection(current),
    )
    assert observed["draft_texts"] == ["已经正常修好的专名", "后续互动", "完整收尾"]
    assert result.audit["boundary_semantic_review"]["final_endpoint_binding"]["status"] == "PASS"
    assert result.audit["current_delivery_closure_binding"]["status"] == "PENDING_FINAL_REVIEW"
    assert result.audit["current_delivery_closure_binding"]["release_gate"] == "BLOCK"
    assert current[0].text == "已经正常修好的专名"


def test_boundary_repair_keeps_source_closure_and_passes_current_text_to_physical_flags(
    tmp_path, monkeypatch
):
    source_closure = SrtCue("45", 101_100, 103_360, "不描绘到是什么感觉了")
    current = [SrtCue("30", 101_100, 103_360, "不必要的会是什么感觉了")]
    observed = {}
    monkeypatch.setattr(
        boundary_resolution,
        "_resolved_tail_adjustment",
        lambda *_args, **_kwargs: {
            "final_end_ms": 103_490,
            "nominal_end_ms": 103_490,
            "next_speech_island_start_ms": None,
            "next_speech_island_guard_ms": 0,
            "reason": None,
        },
    )
    monkeypatch.setattr(
        boundary_resolution,
        "boundary_audit",
        lambda *_args, **_kwargs: {
            "verdict": "ok_sentence_boundary_cut",
            "end_island_continues_ms": 0,
        },
    )
    monkeypatch.setattr(
        boundary_resolution,
        "sanitize_cue_timing",
        lambda *_args, **_kwargs: ([_SanitizedCue(current[0].text)], {}),
    )
    monkeypatch.setattr(
        boundary_resolution,
        "boundary_red_flags",
        lambda **kwargs: (observed.update(kwargs) or []),
    )
    monkeypatch.setattr(
        boundary_resolution,
        "_bind_final_semantic_endpoint",
        lambda **kwargs: (kwargs["semantic_review"], []),
    )

    result = boundary_resolution._repair_boundary(
        cid="current-closure-binding",
        out_root=tmp_path,
        padded_dur=103_490,
        spans=[],
        cues=[source_closure],
        target_start_rel=35_020,
        snapped_start=35_020,
        final_start=35_020,
        target_rel=103_360,
        closure_selection_lower_bound_ms=103_360,
        repair_search_origin_ms=103_360,
        repair_max_end_ms=103_360,
        snapped=103_360,
        closure_cue=source_closure,
        refinement_used=False,
        manual_end_authority=None,
        manual_end_mode="semantic_lower_bound",
        semantic_review=None,
        required_boundary_owners=[],
        required_owner_start_ms=None,
        required_owner_end_ms=None,
        boundary_repair_extend_cap_ms=30_000,
        current_delivery_cues=current,
        current_delivery_projection=_current_projection(current),
    )

    assert observed["closure_text"] == current[0].text
    assert result.audit["closure_sentence"] == source_closure.text
    assert result.audit["delivery_closure_sentence"] == current[0].text
    assert result.audit["current_delivery_closure_binding"]["status"] == "PASS"


def test_short_complete_talk_is_not_padded_to_minimum_duration():
    policy = BoundaryPolicy(min_publish_ms=20_000, soft_max_ms=180_000, hard_max_ms=300_000)
    anchor = AnchorCandidate(candidate_id="short-complete", anchor_start_ms=11_000, anchor_end_ms=23_000)
    cues = [
        cue("setup", 10, 12, "我跟你们说一个事", starts_topic=True, open_loop_delta=1),
        cue("payoff", 20, 23, "结果她回我：你也是这个表情", has_payoff=True, open_loop_delta=-1),
        cue("reaction", 23, 28, "哈哈哈哈就很离谱", ends_topic=True),
    ]

    resolution = resolve_talk_boundary(anchor, cues, policy=policy)

    assert resolution.action == DecisionAction.AUTO_UPLOAD
    assert resolution.resolved_start_ms == 10_000
    assert resolution.resolved_end_ms == 28_000
    assert resolution.duration_ms == 18_000
    assert resolution.duration_ms < policy.min_publish_ms
    assert "SHORT_BUT_COMPLETE" in resolution.reason_codes


def test_incomplete_ending_returns_auto_recut_when_tail_can_extend_to_closure():
    anchor = AnchorCandidate(candidate_id="needs-tail", anchor_start_ms=10_000, anchor_end_ms=40_000)
    cues = [
        cue("setup", 10, 12, "我跟你们说一个事", starts_topic=True, open_loop_delta=1),
        cue("anchor", 32, 40, "然后她突然说", has_payoff=True),
        cue("closure", 70, 75, "所以最后这事就结束了", ends_topic=True, open_loop_delta=-1),
    ]

    resolution = resolve_talk_boundary(anchor, cues)

    assert resolution.action == DecisionAction.AUTO_RECUT
    assert resolution.next_start_ms == 10_000
    assert resolution.next_end_ms == 75_000
    assert "END_BOUNDARY_LOW" in resolution.reason_codes
    assert "OPEN_LOOPS_PRESENT" in resolution.reason_codes


def test_incomplete_ending_drops_when_no_closure_is_found():
    anchor = AnchorCandidate(candidate_id="open-loop", anchor_start_ms=10_000, anchor_end_ms=40_000)
    cues = [
        cue("setup", 10, 12, "我跟你们说一个事", starts_topic=True, open_loop_delta=1),
        cue("anchor", 32, 40, "然后她突然说", has_payoff=True),
        cue("more", 70, 75, "接着还有另一个没讲完的点", open_loop_delta=1),
    ]

    resolution = resolve_talk_boundary(anchor, cues)

    assert resolution.action == DecisionAction.DROP
    assert resolution.resolved_end_ms == 40_000
    assert "NO_NATURAL_CLOSURE" in resolution.reason_codes


def test_hard_max_without_closure_drops_instead_of_truncating():
    policy = BoundaryPolicy(min_publish_ms=20_000, soft_max_ms=180_000, hard_max_ms=60_000)
    anchor = AnchorCandidate(candidate_id="too-long-open", anchor_start_ms=10_000, anchor_end_ms=40_000)
    cues = [
        cue("setup", 10, 12, "我跟你们说一个事", starts_topic=True, open_loop_delta=1),
        cue("anchor", 32, 40, "然后她突然说", has_payoff=True),
        cue("late-closure", 80, 85, "最后结束了", ends_topic=True, open_loop_delta=-1),
    ]

    resolution = resolve_talk_boundary(anchor, cues, policy=policy)

    assert resolution.action == DecisionAction.DROP
    assert resolution.resolved_start_ms == 10_000
    assert resolution.resolved_end_ms == 40_000
    assert resolution.next_end_ms is None
    assert "HARD_MAX_WITHOUT_CLOSURE" in resolution.reason_codes


def test_start_boundary_scores_penalize_connective_anchor_start():
    anchor = AnchorCandidate(candidate_id="bad-start", anchor_start_ms=30_000, anchor_end_ms=55_000)
    cues = [
        cue("prior", 10, 15, "前面先铺垫", starts_topic=True, open_loop_delta=1),
        cue("anchor", 30, 40, "然后她就这样说", has_payoff=True),
        cue("closure", 50, 55, "所以结束了", ends_topic=True, open_loop_delta=-1),
    ]

    resolution = resolve_talk_boundary(anchor, cues)

    assert resolution.action == DecisionAction.AUTO_RECUT
    assert resolution.next_start_ms == 10_000
    assert resolution.start_boundary_score < 0.92
    assert "START_BOUNDARY_LOW" in resolution.reason_codes
