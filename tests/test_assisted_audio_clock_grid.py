"""41 源/交付时钟: an assisted hearing's anchor is the physical target, not its local clock.

``context_assisted_audio._text_only_revision`` compared the recorded anchor's
local offset and the local cue grid, so the same assisted observation checked
against the same physical grid on another local clock (offset 0 vs 500) was
ASSISTED_AUDIO_CURRENT_CONTEXT_DRIFT. It now uses the shared
``same_physical_cue_grid``; a genuinely different physical grid still drifts.
Offline protocol rig; no provider.
"""
from copy import deepcopy

import pytest

from src.autoslice import context_assisted_audio as assisted
from src.autoslice import final_review_auditor as auditor
import test_targeted_relisten_public_contract as contract

FINDING, SRT, three_observations = contract.FINDING, contract.SRT, contract.three_observations
rig = contract.rig  # the offline protocol rig fixture


def _shift(srt, delta_ms):
    from src.autoslice.jingting_chunker import parse_srt_cues

    def stamp(ms):
        return f"00:00:{ms // 1000:02d},{ms % 1000:03d}"
    return "".join(f"{i}\n{stamp(c.start_ms + delta_ms)} --> {stamp(c.end_ms + delta_ms)}\n{c.text}\n\n"
                   for i, c in enumerate(parse_srt_cues(srt), 1))


def _assistance(rig):
    _witness, _check, _finding, audits = three_observations(rig)
    row = audits[1]["observations"][0]
    assert row["observation_class"] == assisted.OBSERVATION_CLASS
    return row["assistance"]


@pytest.mark.parametrize("revised", [False, True])
def test_same_physical_grid_on_another_clock_keeps_the_assistance(rig, revised):
    assistance = _assistance(rig)
    current = SRT.replace("问的是前半句还是尾音", "问的是前半句或者尾音") if revised else SRT
    delivery = _shift(current, -500)
    check = auditor.build_context_adjudication_request(
        delivery, deepcopy(FINDING), clip_context=rig.context, source_media_timeline_offset_ms=500)
    assert assisted.anchor(check)["physical_target_ms"] == assistance["anchor"]["physical_target_ms"]
    validated = assisted.validate_assistance(assistance, check, original_srt=SRT)
    assert validated["assistance_sha256"] == assistance["assistance_sha256"]


def test_a_different_physical_grid_still_drifts(rig):
    assistance = _assistance(rig)
    delivery = _shift(SRT, -500)
    check = auditor.build_context_adjudication_request(
        delivery, deepcopy(FINDING), clip_context=rig.context, source_media_timeline_offset_ms=400)
    with pytest.raises(ValueError, match="ASSISTED_AUDIO_CURRENT_CONTEXT_DRIFT"):
        assisted.validate_assistance(assistance, check, original_srt=SRT)
