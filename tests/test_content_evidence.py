from src.autoslice.auto_review import DecisionAction, JingtingProvenance, review_candidate
from src.autoslice.content_evidence import analyze_content_evidence
from src.autoslice.review_evidence import SourceCue, to_candidate_review


def good_provenance():
    return JingtingProvenance(True, "agy", 0, "Gemini 3.5 Flash (Low)", False)


def cue(cue_id, start_s, end_s, text, kind="speech"):
    return SourceCue(
        cue_id=cue_id,
        source_start_ms=int(start_s * 1000),
        source_end_ms=int(end_s * 1000),
        text=text,
        kind=kind,
        language="zh",
        confidence=0.9,
    )


def decision_for(evidence):
    return review_candidate(to_candidate_review(evidence, good_provenance()))


def test_partial_foreground_song_blocks_or_drops_never_auto_uploads():
    evidence = analyze_content_evidence(
        candidate_id="partial-song",
        cues=[cue("song", 0, 12, "虫儿飞 花儿睡", kind="singing")],
        title="《虫儿飞》翻车成《冲而飞》？",
        foreground_song_overlap_seconds=12.0,
        song_duration_seconds=90.0,
        song_complete=False,
    )

    decision = decision_for(evidence)

    assert evidence.foreground_song_overlap_seconds == 12.0
    assert evidence.song_complete is False
    assert decision.action in {DecisionAction.BLOCK, DecisionAction.DROP}
    assert decision.action != DecisionAction.AUTO_UPLOAD
    assert "SONG_PARTIAL" in decision.reason_codes


def test_complete_non_song_talk_with_clean_start_and_end_scores_pass():
    evidence = analyze_content_evidence(
        candidate_id="clean-talk",
        cues=[
            cue("setup", 10, 13, "我跟你们说一个事"),
            cue("payoff", 20, 24, "结果她说你也是这个表情 哈哈哈"),
            cue("end", 24, 28, "这个事情就结束了"),
        ],
        title="这个反应也太离谱了",
    )

    decision = decision_for(evidence)

    assert evidence.foreground_song_overlap_seconds == 0.0
    assert evidence.lyrics_alignment_ready is True
    assert evidence.start_boundary_score >= 0.92
    assert evidence.end_boundary_score >= 0.95
    assert evidence.standalone_score >= 0.90
    assert evidence.payoff_score >= 0.90
    assert evidence.open_loop_count == 0
    assert decision.action == DecisionAction.AUTO_UPLOAD


def test_connective_start_gets_low_start_boundary_score():
    evidence = analyze_content_evidence(
        candidate_id="bad-start",
        cues=[cue("bad", 30, 34, "然后她就突然这样说"), cue("end", 35, 40, "最后大家都笑了 哈哈哈")],
        title="突然开始解释",
    )

    assert evidence.start_boundary_score < 0.92
    assert "STARTS_WITH_CONNECTIVE" in evidence.evidence_gaps
    assert decision_for(evidence).action == DecisionAction.AUTO_RECUT


def test_open_question_or_no_closure_lowers_end_score_and_counts_open_loop():
    evidence = analyze_content_evidence(
        candidate_id="open-question",
        cues=[cue("setup", 0, 4, "我问你们一个问题"), cue("question", 5, 8, "所以到底为什么会这样呢？")],
        title="到底为什么会这样",
    )

    assert evidence.end_boundary_score < 0.95
    assert evidence.open_loop_count > 0
    assert "OPEN_LOOP_OR_NO_CLOSURE" in evidence.evidence_gaps


def test_missing_punchline_or_reaction_gets_low_payoff_score():
    evidence = analyze_content_evidence(
        candidate_id="no-payoff",
        cues=[cue("setup", 0, 3, "我跟你们说一个事"), cue("body", 4, 8, "这个东西就是这样")],
        title="普通说明片段",
    )

    assert evidence.payoff_score < 0.90
    assert "PAYOFF_MISSING" in evidence.evidence_gaps
    assert decision_for(evidence).action in {DecisionAction.DROP, DecisionAction.AUTO_RECUT}


def test_japanese_or_known_song_lyrics_unresolved_keeps_alignment_not_ready():
    evidence = analyze_content_evidence(
        candidate_id="jp-song",
        cues=[cue("jp", 0, 10, "言って あのね 私 アイドル", kind="singing")],
        title="《私、アイドル宣言》现场唱歌",
        foreground_song_overlap_seconds=10.0,
        song_duration_seconds=100.0,
        song_complete=True,
    )

    assert evidence.lyrics_alignment_ready is False
    assert "LYRICS_AUTO_ALIGNMENT_UNIMPLEMENTED" in evidence.evidence_gaps
    decision = decision_for(evidence)
    assert decision.action == DecisionAction.BLOCK
    assert "LYRICS_ALIGNMENT_REQUIRED" in decision.reason_codes
