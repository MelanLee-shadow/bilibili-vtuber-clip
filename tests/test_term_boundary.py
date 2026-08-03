from src.autoslice.jingting_chunker import SrtCue
from src.autoslice.term_boundary import unify_terms_across_cues


def _cue(index, start_ms, end_ms, text):
    return SrtCue(index=str(index), start_ms=start_ms, end_ms=end_ms, text=text)


# The three real straddled 梦限大 pairs delivered (cue5/6, cue24/25,
# cue27/28 of one clip) — see AGENTS.md / task for provenance.
_REAL_PAIRS = [
    (5, "我们要看，我们要看那个梦", 6, "限大，梦——"),
    (24, "来到了我们这个梦", 25, "限大现在直接给大家推出一个究极坏女人"),
    (27, "拯救这个梦", 28, "限大的企划，就这样"),
]


def _timestamps(cues):
    return [(c.index, c.start_ms, c.end_ms) for c in cues]


def test_real_delivered_pairs_are_unified():
    cues = []
    cursor = 0
    for a_index, a_text, b_index, b_text in _REAL_PAIRS:
        cues.append(_cue(a_index, cursor, cursor + 1000, a_text))
        cursor += 1000
        cues.append(_cue(b_index, cursor, cursor + 1000, b_text))
        cursor += 2000

    before_timing = _timestamps(cues)
    out, moves = unify_terms_across_cues(cues, ["梦限大"])

    assert _timestamps(out) == before_timing
    assert len(out) == len(cues)

    texts = {c.index: c.text for c in out}
    assert texts["5"] == "我们要看，我们要看那个"
    assert texts["6"] == "梦限大，梦——"
    assert texts["24"] == "来到了我们这个"
    assert texts["25"] == "梦限大现在直接给大家推出一个究极坏女人"
    assert texts["27"] == "拯救这个"
    assert texts["28"] == "梦限大的企划，就这样"

    assert len(moves) == 3
    for move in moves:
        assert move["term"] == "梦限大"
        assert move["direction"] == "forward"
        assert move["moved_text"] == "梦"


def test_no_term_no_op():
    cues = [
        _cue(1, 0, 1000, "今天天气真好"),
        _cue(2, 1000, 2000, "我们去公园玩吧"),
    ]
    out, moves = unify_terms_across_cues(cues, ["示范例", "邦多利"])
    assert moves == []
    assert [(c.index, c.text) for c in out] == [(c.index, c.text) for c in cues]


def test_donor_would_empty_is_skipped():
    # A's entire text is the term's own leading fragment: moving it would
    # leave cue A with nothing (or only punctuation), so the move must be
    # refused and both cues left untouched.
    cues = [
        _cue(1, 0, 1000, "示"),
        _cue(2, 1000, 2000, "范例来了"),
    ]
    out, moves = unify_terms_across_cues(cues, ["示范例"])
    assert moves == []
    assert out[0].text == "示"
    assert out[1].text == "范例来了"


def test_donor_would_become_punctuation_only_is_skipped():
    cues = [
        _cue(1, 0, 1000, "，示"),
        _cue(2, 1000, 2000, "范例来了"),
    ]
    out, moves = unify_terms_across_cues(cues, ["示范例"])
    assert moves == []
    assert out[0].text == "，示"
    assert out[1].text == "范例来了"


def test_idempotent():
    cues = [
        _cue(5, 0, 1000, "我们要看，我们要看那个示"),
        _cue(6, 1000, 2000, "范例，示——"),
    ]
    once, moves_once = unify_terms_across_cues(cues, ["示范例"])
    assert len(moves_once) == 1
    twice, moves_twice = unify_terms_across_cues(once, ["示范例"])
    assert moves_twice == []
    assert [(c.index, c.text) for c in twice] == [(c.index, c.text) for c in once]


def test_multi_term_longest_first_prefers_longer_surface():
    # "范例" alone is a substring of "示范例"; if the shorter surface were
    # tried first it could "fix" a boundary that the longer, correct term
    # should own instead.  Longest-first must try 示范例 before 范例.
    cues = [
        _cue(1, 0, 1000, "这是示"),
        _cue(2, 1000, 2000, "范例的故事"),
    ]
    out, moves = unify_terms_across_cues(cues, ["范例", "示范例"])
    assert len(moves) == 1
    assert moves[0]["term"] == "示范例"
    assert out[0].text == "这是"
    assert out[1].text == "示范例的故事"


def test_majority_rule_moves_backward_when_a_holds_more():
    # A holds the majority (2 chars) of the term, B holds the minority
    # (1 char): the fragment should move backward into A instead.
    cues = [
        _cue(1, 0, 1000, "这是示范"),
        _cue(2, 1000, 2000, "例的故事"),
    ]
    out, moves = unify_terms_across_cues(cues, ["示范例"])
    assert len(moves) == 1
    assert moves[0]["direction"] == "backward"
    assert out[0].text == "这是示范例"
    assert out[1].text == "的故事"


def test_tie_goes_to_later_cue():
    # A holds "示" only, hypothetical 2-char term split 1/1 -> tie -> later
    # cue (B) is treated as the majority holder, so text moves forward.
    cues = [
        _cue(1, 0, 1000, "这是邦"),
        _cue(2, 1000, 2000, "多的故事"),
    ]
    out, moves = unify_terms_across_cues(cues, ["邦多"])
    assert len(moves) == 1
    assert moves[0]["direction"] == "forward"
    assert out[0].text == "这是"
    assert out[1].text == "邦多的故事"


def test_timestamps_and_count_never_change():
    cues = [
        _cue(1, 0, 1234, "这是示"),
        _cue(2, 1234, 5000, "范例的故事"),
        _cue(3, 5000, 8000, "还有更多内容在这里"),
    ]
    before = [(c.index, c.start_ms, c.end_ms) for c in cues]
    out, _moves = unify_terms_across_cues(cues, ["示范例"])
    assert len(out) == len(cues)
    assert [(c.index, c.start_ms, c.end_ms) for c in out] == before
