"""This directory's conftest isolates every test here (see conftest.py); the probe the arg-grouping guard runs."""

import os


def test_this_directorys_conftest_applies():
    assert os.environ.get("MANUAL_LANE_LADDER") == "gemini"
