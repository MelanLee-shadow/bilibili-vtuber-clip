from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_resolver import (
    AnchorCandidate,
    BoundaryPolicy,
    BoundaryResolution,
    TalkCue,
    resolve_talk_boundary,
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
