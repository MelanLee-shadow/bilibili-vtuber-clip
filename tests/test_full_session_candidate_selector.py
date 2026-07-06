from src.autoslice.auto_review import DecisionAction
from src.autoslice.full_session_candidate_selector import select_full_session_candidates
from src.autoslice.review_evidence import SourceCue


def cue(cue_id: str, start_s: float, end_s: float, text: str) -> SourceCue:
    return SourceCue(
        cue_id=cue_id,
        source_start_ms=int(start_s * 1000),
        source_end_ms=int(end_s * 1000),
        text=text,
    )


def test_selects_setup_payoff_closure_window_as_source_context_candidate():
    cues = [
        cue("pre", 0, 2, "晚上好我先调一下麦"),
        cue("setup", 10, 12, "我跟你们说一个事"),
        cue("detail", 20, 24, "然后她突然发来一句话"),
        cue("payoff", 35, 39, "结果她回我你也是这个表情"),
        cue("closure", 45, 48, "哈哈哈哈就很离谱"),
        cue("song", 70, 76, "啊啊啊啊君のこと愛してる"),
    ]

    candidates = select_full_session_candidates(cues, max_candidates=3)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.anchor.candidate_id == "fullctx_10000_48000"
    assert candidate.anchor.anchor_start_ms == 10_000
    assert candidate.anchor.anchor_end_ms == 48_000
    assert candidate.boundary.action == DecisionAction.AUTO_UPLOAD
    assert candidate.boundary.resolved_start_ms == 10_000
    assert candidate.boundary.resolved_end_ms == 48_000
    assert "我跟你们说一个事" in candidate.text_preview
    assert candidate.to_source_context_job(source_duration_ms=120_000)["timeline"] == {
        "source_duration_ms": 120_000,
        "anchor_start_ms": 10_000,
        "anchor_end_ms": 48_000,
        "context_start_ms": 10_000,
        "context_end_ms": 48_000,
        "context_duration_ms": 38_000,
    }
    assert candidate.to_source_context_job(source_duration_ms=120_000)["content_type_hint"] == "talk"
    assert candidate.to_source_context_job(source_duration_ms=120_000)["requires_full_source_song_boundary_redo"] is False


def test_song_like_window_is_emitted_as_song_anchor_not_filtered_out():
    cues = [
        cue("setup", 10, 12, "我跟你们说这首歌真的很离谱"),
        cue("lyric-1", 20, 25, "啊啊啊啊啊"),
        cue("lyric-2", 36, 40, "最后の最后まで君を愛してる"),
        cue("lyric-3", 45, 48, "怎么明明相爱的两个人你要拆散他们啊"),
        cue("lyric-4", 50, 54, "上天啊你千万不要偷偷告诉他"),
        cue("lyric-5", 55, 60, "我遇见所有回忆来庆祝你的婚礼"),
        cue("closure", 70, 74, "唱完大家都笑了"),
    ]

    candidates = select_full_session_candidates(cues, max_candidates=3)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.content_type_hint == "song"
    assert candidate.boundary.action == DecisionAction.AUTO_RECUT
    assert "SONG_BOUNDARY_REDO_REQUIRED" in candidate.boundary.reason_codes
    assert candidate.anchor.anchor_start_ms == 10_000
    assert candidate.anchor.anchor_end_ms == 40_000
    manifest = candidate.to_manifest()
    assert manifest["content_type_hint"] == "song"
    assert manifest["requires_full_source_song_boundary_redo"] is True
    job = candidate.to_source_context_job(source_duration_ms=300_000)
    assert job["content_type_hint"] == "song"
    assert job["song_candidate"] is True
    assert job["requires_full_source_song_boundary_redo"] is True
    assert job["timeline"] == {
        "source_duration_ms": 300_000,
        "anchor_start_ms": 10_000,
        "anchor_end_ms": 40_000,
        "context_start_ms": 0,
        "context_end_ms": 300_000,
        "context_duration_ms": 300_000,
    }


def test_song_continuation_after_song_anchor_is_not_reclassified_as_talk():
    cues = [
        cue("setup", 10, 12, "我跟你们说这首歌真的很离谱"),
        cue("lyric-1", 20, 25, "啊啊啊啊啊"),
        cue("lyric-2", 36, 40, "最后の最后まで君を愛してる"),
        cue("lyric-3", 45, 48, "怎么明明相爱的两个人你要拆散他们啊"),
        cue("closure", 70, 74, "唱完大家都笑了"),
    ]

    candidates = select_full_session_candidates(cues, max_candidates=3)

    assert [candidate.content_type_hint for candidate in candidates] == ["song"]


def test_filters_open_loops_and_overlapping_duplicates_while_classifying_song_windows():
    cues = [
        cue("open-setup", 0, 2, "我跟你们说一个事"),
        cue("open-loop", 10, 14, "然后这个问题到底怎么办"),
        cue("lyric-1", 30, 35, "啊啊啊啊啊"),
        cue("lyric-2", 36, 40, "最后の最后まで君を愛してる"),
        cue("lyric-3", 45, 48, "怎么明明相爱的两个人你要拆散他们啊"),
        cue("lyric-4", 50, 54, "上天啊你千万不要偷偷告诉他"),
        cue("lyric-5", 55, 60, "我遇见所有回忆来庆祝你的婚礼"),
        cue("lyric-6", 61, 64, "这始终没有勇气祝福你"),
        cue("good-setup", 90, 92, "有个事我真的绷不住"),
        cue("good-detail", 100, 102, "她说先别急"),
        cue("good-payoff", 110, 113, "结果下一秒就翻车了"),
        cue("good-closure", 120, 124, "最后大家都笑了"),
        cue("overlap", 125, 127, "哈哈哈哈"),
    ]

    candidates = select_full_session_candidates(cues, max_candidates=5)

    assert [candidate.content_type_hint for candidate in candidates] == ["song", "talk"]
    assert [candidate.boundary.action for candidate in candidates] == [DecisionAction.AUTO_RECUT, DecisionAction.AUTO_UPLOAD]
    assert candidates[0].anchor.candidate_id == "fullsong_0_40000"
    assert candidates[1].anchor.candidate_id == "fullctx_90000_124000"


def test_fallback_recall_surfaces_singing_run_when_primary_finds_nothing():
    from src.autoslice.full_session_candidate_selector import (
        select_fallback_session_candidates,
        select_full_session_candidates,
    )

    # Shaped like the real 2026-07-02 live capture of room 26730839: chat with
    # no lidousha setup markers, then a dense ~110s singing run.
    cues = []
    chat = [(0, 1200, "你却开篇"), (27020, 28300, "本体的神乃上大"), (32060, 33660, "什么时候玩摄氏天下"), (44560, 45760, "喜欢就好")]
    for index, (start, end, text) in enumerate(chat):
        cues.append(SourceCue(f"chat-{index}", start, end, text))
    cursor = 50_760
    for index in range(18):
        cues.append(SourceCue(f"sing-{index}", cursor, cursor + 5_200, f"江湖难测侠骨柔情红颜梦第{index}句"))
        cursor += 6_200
    primary = select_full_session_candidates(cues)
    fallback = select_fallback_session_candidates(cues)

    assert primary == []
    assert fallback, "fallback recall must surface the singing run"
    candidate = fallback[0]
    assert candidate.content_type_hint == "song"
    assert candidate.anchor.anchor_start_ms == 50_760
    assert candidate.anchor.anchor_end_ms == cursor - 6_200 + 5_200
    assert "SONG_BOUNDARY_REDO_REQUIRED" in candidate.boundary.reason_codes


def test_fallback_recall_also_surfaces_talk_windows_outside_song_runs():
    from src.autoslice.full_session_candidate_selector import select_fallback_session_candidates

    # Real-capture shape: chat with a punchline, then a singing run.
    cues = [
        SourceCue("chat-0", 0, 4_000, "什么时候玩摄氏天下"),
        SourceCue("chat-1", 5_000, 9_500, "其实我是觉得他那个游戏"),
        SourceCue("chat-2", 9_700, 14_500, "价格有点贵哈哈哈"),
        SourceCue("chat-3", 15_000, 18_000, "喜欢就好"),
    ]
    cursor = 40_000
    for index in range(18):
        cues.append(SourceCue(f"sing-{index}", cursor, cursor + 5_200, f"江湖难测侠骨柔情红颜梦第{index}句"))
        cursor += 6_200

    fallback = select_fallback_session_candidates(cues, max_candidates=3)

    hints = [candidate.content_type_hint for candidate in fallback]
    assert "song" in hints
    assert "talk" in hints
    talk = next(candidate for candidate in fallback if candidate.content_type_hint == "talk")
    assert talk.anchor.anchor_start_ms == 0
    assert "哈哈" in talk.text_preview or "喜欢就好" in talk.text_preview
