from src.autoslice.auto_review import DecisionAction, JingtingProvenance, review_candidate
from src.autoslice.review_evidence import ReviewEvidence, to_candidate_review
from src.autoslice.style_profile import ManualStyleProfile, apply_style_profile, score_style_match


def good_provenance():
    return JingtingProvenance(True, "agy", 0, "Gemini 3.6 Flash (Low)", False)


def evidence(**overrides):
    data = {
        "candidate_id": "clip",
        "foreground_song_overlap_seconds": 0.0,
        "song_complete": True,
        "lyrics_alignment_ready": True,
        "start_boundary_score": 0.97,
        "end_boundary_score": 0.98,
        "standalone_score": 0.94,
        "payoff_score": 0.95,
        "open_loop_count": 0,
        "editorial_score": 86.0,
        "duplicate_similarity": 0.1,
        "subtitle_alignment_p95_ms": 100.0,
        "actual_cut_error_ms": 0.0,
        "evidence_gaps": (),
    }
    data.update(overrides)
    return ReviewEvidence(**data)


def profile():
    return ManualStyleProfile(
        profile_id="lidousha-manual-test",
        sample_count=5,
        preferred_duration_seconds={"p25": 20.0, "p50": 65.0, "p75": 110.0},
        title_hook_patterns=("？", "也太", "突然", "笑"),
        kept_content_types=("funny_talk", "interaction", "mistake", "callback", "song"),
        intro_outro_tolerance="low",
        subtitle_density_range=(0.18, 0.70),
        danmaku_density_range=(0.05, 0.90),
        negative_patterns=("contextless_fragment", "song_cut", "no_payoff", "duplicate", "pure_noise"),
    )


def test_profile_missing_keeps_candidate_fail_closed_not_auto_upload():
    styled = apply_style_profile(evidence(), None, title="这个反应也太离谱了", duration_seconds=60.0)

    candidate = to_candidate_review(styled, good_provenance())
    decision = review_candidate(candidate)

    assert styled.editorial_score is None
    assert "STYLE_PROFILE_MISSING" in styled.evidence_gaps
    assert decision.action == DecisionAction.BLOCK
    assert decision.action != DecisionAction.AUTO_UPLOAD


def test_hand_cut_like_sample_scores_higher_than_generic_no_payoff_sample():
    hand_cut_like = score_style_match(evidence(payoff_score=0.98), profile(), title="她突然说自己是小皇帝？", duration_seconds=65.0)
    generic = score_style_match(evidence(payoff_score=0.40), profile(), title="普通游戏片段", duration_seconds=180.0)

    assert hand_cut_like.style_match_score > generic.style_match_score
    assert "hook_title_pattern" in hand_cut_like.reasons
    assert "missing_payoff" in generic.reasons


def test_duplicate_gate_still_wins_even_when_style_match_is_high():
    styled = apply_style_profile(
        evidence(candidate_id="dupe", duplicate_similarity=0.96, payoff_score=0.98),
        profile(),
        title="她突然说自己是小皇帝？",
        duration_seconds=65.0,
    )

    decision = review_candidate(to_candidate_review(styled, good_provenance()))

    assert styled.editorial_score and styled.editorial_score >= 82.0
    assert decision.action == DecisionAction.DROP
    assert "DUPLICATE" in decision.reason_codes
