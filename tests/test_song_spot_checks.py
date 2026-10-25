import pytest

from src.autoslice.song_spot_checks import repeated_section_spot_matches


@pytest.mark.parametrize(
    "spot_ms, expected",
    [
        (138000, True),
        (134700, True),
        (141799, True),
        (141800, False),
        (134699, False),
        (101000, False),
        (None, False),
        (138000.0, False),
        (True, False),
    ],
)
def test_repeated_spot_binds_later_audible_interval(spot_ms, expected):
    alignment = [
        {"cue_start_ms": 100000, "cue_end_ms": 107100},
        {"cue_start_ms": 134700, "cue_end_ms": 141800},
    ]
    assert repeated_section_spot_matches(["重复歌词", "重复歌词"], alignment, spot_ms) is expected


def test_unique_lyrics_do_not_require_an_invented_recurrence():
    assert repeated_section_spot_matches(
        ["只唱一次"], [{"cue_start_ms": 100, "cue_end_ms": 200}], None
    )
