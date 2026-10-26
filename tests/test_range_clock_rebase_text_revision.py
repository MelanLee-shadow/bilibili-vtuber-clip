"""41 源/交付时钟: a clock representation plus a text-only revision is one target.

Guimi b4cd6dc cue94 (#22): a context-assisted range heard on the padded clock
(offset 0) was replayed for the delivery request (offset 9800) after one
unrelated cue changed wording. The physical cue grid was identical, yet the
assisted branch compared local grids and returned RANGE_ACOUSTIC_SRT_FILE_DRIFT,
and the clock re-base refused because texts differed. The re-based view now
carries the current text in the observation's own clock and the ordinary
text-revision rules decide. A different physical grid still drifts.
Offline protocol rig; no provider.
"""
from copy import deepcopy

from src.autoslice import final_review_auditor as auditor
from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation
import test_targeted_relisten_public_contract as contract

FINDING, SRT, three_observations = contract.FINDING, contract.SRT, contract.three_observations
rig = contract.rig  # the offline protocol rig fixture


def _shift(srt, delta_ms):
    from src.autoslice.jingting_chunker import parse_srt_cues

    def stamp(ms):
        return f"00:00:{ms // 1000:02d},{ms % 1000:03d}"
    return "".join(f"{i}\n{stamp(c.start_ms + delta_ms)} --> {stamp(c.end_ms + delta_ms)}\n{c.text}\n\n"
                   for i, c in enumerate(parse_srt_cues(srt), 1))


def _delivery_request(rig, srt, offset_ms):
    return auditor.build_context_adjudication_request(
        srt, deepcopy(FINDING), clip_context=rig.context, source_media_timeline_offset_ms=offset_ms)


def test_padded_clock_hearings_survive_a_text_revision_under_the_delivery_clock(rig):
    _witness, _check, _finding, audits = three_observations(rig)
    blind = audits[0]["observations"][0]
    assisted = audits[1]["observations"][0]
    assert assisted["observation_class"] == "CONTEXT_ASSISTED_AUDIO"
    revised = SRT.replace("问的是前半句还是尾音", "问的是前半句或者尾音")
    delivery = _shift(revised, -500)
    request = _delivery_request(rig, delivery, 500)

    for row in (blind, assisted):
        valid, reason = validate_range_acoustic_observation(delivery, request, row)
        assert valid is not None, reason
        assert valid["clock_rebase"]["cue_texts_identical"] is False
        assert valid["clock_rebase"]["original_offset_ms"] == 0
        assert valid["clock_rebase"]["current_offset_ms"] == 500


def test_a_different_physical_grid_still_drifts(rig):
    _witness, _check, _finding, audits = three_observations(rig)
    revised = SRT.replace("问的是前半句还是尾音", "问的是前半句或者尾音")
    delivery = _shift(revised, -500)
    request = _delivery_request(rig, delivery, 400)  # 100 ms off the heard grid
    for row in (audits[0]["observations"][0], audits[1]["observations"][0]):
        valid, reason = validate_range_acoustic_observation(delivery, request, row)
        assert valid is None and reason
