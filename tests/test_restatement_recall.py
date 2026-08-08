import src.autoslice.restatement_recall as rr
from src.autoslice.restatement_recall import (
    RestatementCue,
    find_restatement_pairs,
    strip_leading_connectors,
)


def _cue(index: int, start: float, text: str, label: str | None = "李豆沙"):
    return RestatementCue(index=index, start_seconds=start, label=label, text=text)


def test_flagship_cue17_cue29_pair_is_detected_with_production_defaults() -> None:
    cues = [
        _cue(17, 36.3, "这是我的小孩就是了", label="连线"),
        _cue(20, 43.7, "不小心把她小孩杀了"),
        _cue(29, 58.8, "这是我今天的宣言"),
    ]
    pairs = find_restatement_pairs(cues)
    assert [(p.early_index, p.late_index) for p in pairs] == [(17, 29)]
    pair = pairs[0]
    assert pair.prefix_run >= 3
    assert pair.similarity >= 0.45


def test_garbled_early_cue_label_is_not_a_hard_prefilter() -> None:
    # cue17 is machine-labeled 连线; the early side must still qualify.
    cues = [_cue(1, 0.0, "这是我的小孩就是了", label="连线"), _cue(4, 20.0, "这是我今天的宣言")]
    assert find_restatement_pairs(cues)


def test_faithful_restart_is_still_proposed_for_witness_noop() -> None:
    # cue76/79 shape: ASR faithfully caught the interrupted start. The witness
    # is the layer that decides no repair is needed; detection still pairs it.
    cues = [_cue(76, 100.0, "我肯定是杀", label="李豆沙"), _cue(79, 104.0, "我肯定是先杀莉亚")]
    assert find_restatement_pairs(cues)


def test_short_disfluency_restart_is_excluded_by_min_chars() -> None:
    cues = [_cue(7, 14.3, "我是"), _cue(9, 15.6, "我是今天争取骗一骗女主播的沙豆李")]
    assert find_restatement_pairs(cues) == []


def test_connector_prefix_does_not_fake_an_anchor() -> None:
    cues = [_cue(88, 10.0, "然后我看就是"), _cue(94, 20.0, "然后我就在旁边默默")]
    assert find_restatement_pairs(cues) == []


def test_identical_repeat_is_not_a_repair_pair() -> None:
    cues = [_cue(1, 0.0, "再也不相信真善美了"), _cue(4, 30.0, "再也不相信真善美了")]
    assert find_restatement_pairs(cues) == []


def test_guest_late_cue_cannot_be_the_restatement_side() -> None:
    cues = [
        _cue(17, 36.3, "这是我的小孩就是了", label="连线"),
        _cue(29, 58.8, "这是我今天的宣言", label="连线"),
    ]
    assert find_restatement_pairs(cues) == []


def test_gap_beyond_window_is_excluded() -> None:
    cues = [_cue(16, 0.0, "我这把平时贪生怕死不做任务"), _cue(68, 103.0, "我这把会很努力做任务的")]
    assert find_restatement_pairs(cues) == []


def test_missing_pinyin_backend_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(rr, "_lazy_pinyin", None)
    cues = [_cue(17, 36.3, "这是我的小孩就是了"), _cue(29, 58.8, "这是我今天的宣言")]
    assert find_restatement_pairs(cues) == []


def test_strip_leading_connectors_iterates_and_keeps_content() -> None:
    assert strip_leading_connectors("然后呃我看就是") == "我看就是"
    assert strip_leading_connectors("这是我今天的宣言") == "这是我今天的宣言"
