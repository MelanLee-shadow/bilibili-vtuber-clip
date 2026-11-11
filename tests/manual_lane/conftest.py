"""Test defaults: the suite exercises the full two-pass adjudication unless a test asks for a variant, and variant C's
Gemini-only ladder unless a test asks for another one (the window-ASR ladders have their own tests,
test_task11_witnesses_cover_lone.py).  The production defaults are stage_impl.DEFAULT_ADJUDICATE_MODE and
lane_rules listening.ladder (assets/manual_lane/lane_rules.json)."""

import pytest


@pytest.fixture(autouse=True)
def _adjudicate_full_by_default(monkeypatch):
    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "full")
    monkeypatch.setenv("MANUAL_LANE_LADDER", "gemini")
