import pytest

from src.autoslice.song_global_shift import solve_global_shift


ACTUAL_RESIDUALS = [
    44810,
    45130,
    45230,
    42730,
    44430,
    44670,
    44690,
    45050,
    43860,
    44610,
    44240,
    44700,
    45070,
    44310,
    44560,
    44820,
    44440,
    45370,
    43920,
    44830,
    44340,
    44930,
    43530,
    44440,
    44730,
    45370,
    45300,
    44760,
    45150,
    45650,
]


def test_asymmetric_feasible_observations_do_not_require_the_median_to_fit():
    proof = solve_global_shift(ACTUAL_RESIDUALS)
    assert proof["median_offset_ms"] == 44730
    assert (proof["feasible_minimum_ms"], proof["feasible_maximum_ms"]) == (44150, 44230)
    assert proof["offset_ms"] == 44190 and proof["maximum_error_ms"] == 1460
    assert proof["strategy"] == "feasible_interval_midpoint"
    assert all(abs(d - proof["offset_ms"]) <= 1500 for d in ACTUAL_RESIDUALS)


@pytest.mark.parametrize(
    "values,expected",
    [([0] * 30, 0), ([10000] * 30, 10000), ([18000, 18800, 19500], 18800), ([0, 3000], 1500)],
)
def test_global_shift_preserves_successful_upper_median_and_exact_boundary(values, expected):
    assert solve_global_shift(values)["offset_ms"] == expected


@pytest.mark.parametrize("values", [[], [0, 3001], [False, 1], [1.5, 2]])
def test_global_shift_rejects_no_intersection_or_invalid_clock(values):
    with pytest.raises(ValueError):
        solve_global_shift(values)
