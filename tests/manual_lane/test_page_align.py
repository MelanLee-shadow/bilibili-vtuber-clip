"""A listening-page line takes a doubt's basis only when the doubt covers more than half of the line (page builders).

Regression: a 28 ms overlap with the neighbouring line lent its basis and its 低 mark to the wrong line on 681_874.
"""

from __future__ import annotations

from src.autoslice.manual_lane.page_align import match_doubt, overlap_fraction

D055 = {"id": "d055", "abs_start_ms": 816552, "abs_end_ms": 819872, "state": "ADOPT", "confidence": "低"}


def test_a_line_with_only_a_slight_overlap_with_a_doubt_gets_no_basis():
    # the line before d055 ends where d055 barely begins: 28 ms of overlap on a 5.2 s line
    assert match_doubt(129580, 816552 + 28, [D055]) is None
    assert overlap_fraction(129580, 816552 + 28, D055) < 0.01


def test_the_line_the_doubt_belongs_to_takes_it():
    assert match_doubt(816552, 819872, [D055])["id"] == "d055"


def test_exactly_half_is_not_enough_and_more_than_half_is_enough():
    # a 1000 ms line, doubt covering exactly 500 ms: not more than half, no basis
    d = {"id": "dx", "abs_start_ms": 500, "abs_end_ms": 1500}
    assert match_doubt(0, 1000, [d]) is None
    d_more = {"id": "dy", "abs_start_ms": 499, "abs_end_ms": 1500}
    assert match_doubt(0, 1000, [d_more])["id"] == "dy"


def test_the_largest_overlap_wins_when_two_doubts_touch_a_line():
    small = {"id": "a", "abs_start_ms": 0, "abs_end_ms": 600}
    large = {"id": "b", "abs_start_ms": 200, "abs_end_ms": 1000}   # 800 ms against small 600 ms
    assert match_doubt(0, 1000, [small, large])["id"] == "b"


def test_auto_kept_cues_without_absolute_times_never_match_and_empty_lines_do_not_match():
    assert match_doubt(0, 1000, [{"id": "k", "state": "KEEP"}]) is None
    assert match_doubt(500, 500, [D055]) is None


def test_adjacent_lines_with_a_slight_overlap_never_share_a_basis():
    first = {"id": "d1", "abs_start_ms": 0, "abs_end_ms": 1020}      # covers line 1 and 20 ms of line 2
    line1, line2 = (0, 1000), (1000, 2000)
    assert match_doubt(*line1, [first])["id"] == "d1"
    assert match_doubt(*line2, [first]) is None
