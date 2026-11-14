"""A channel with its intro off: step 8's checks read offset 0 from the no-intro receipt, and the roster check still
holds that receipt against the channel's manifest."""
import json
from types import SimpleNamespace

import pytest

from src.autoslice.finished_media_review import checks
from src.autoslice.finished_media_review.common import FmrError
from src.autoslice.manual_lane import gates

NO_INTRO = {"status": "NO_INTRO", "reason": "branding_intro.v1.json: enabled is false", "intro_id": None,
            "intro_offset_ms": 0}


def _ctx(tmp_path, receipt):
    path = tmp_path / "intro_result.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return SimpleNamespace(files={"intro_receipt": path})


def test_the_no_intro_receipt_reads_as_offset_zero_and_both_stages_accept_it(tmp_path):
    assert checks._intro_offset_ms(_ctx(tmp_path, NO_INTRO)) == 0
    assert gates.intro_gate(NO_INTRO).ok


@pytest.mark.parametrize("receipt", [
    {**NO_INTRO, "intro_id": "Z1"},                    # says no intro but names one
    {"status": "PREPENDED", "intro_id": "Z1", "intro_offset_ms": 0},
    {"status": "PREPENDED", "intro_id": "Z1", "intro_offset_ms": False},
])
def test_any_other_receipt_without_a_positive_offset_is_refused(tmp_path, receipt):
    with pytest.raises(FmrError, match="FMR_INTRO_OFFSET_INVALID"):
        checks._intro_offset_ms(_ctx(tmp_path, receipt))


def test_a_no_intro_receipt_on_a_channel_whose_intro_is_on_fails_the_roster_check(monkeypatch):
    # a channel whose manifest is enabled: a clip that went out without its intro is not finished
    from src.autoslice import branding_intro

    monkeypatch.setattr(branding_intro, "load_branding_intro_policy", lambda path: {"enabled": True, "intros": []})
    problems = checks.intro_problems(NO_INTRO, video_duration_ms=None)
    assert "BRANDING_INTRO_BINDING_NOT_PREPENDED" in problems


def test_a_channel_with_its_intro_off_has_no_roster_problem(monkeypatch):
    from src.autoslice import branding_intro

    monkeypatch.setattr(branding_intro, "load_branding_intro_policy", lambda path: None)
    assert checks.intro_problems(NO_INTRO, video_duration_ms=None) == []
