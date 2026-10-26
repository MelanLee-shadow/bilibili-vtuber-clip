import pytest

from src.autoslice.subtitle_acceptability_policy import load_policy
from src.autoslice.witness_self_consistency import assess_witness, normalized_edit_distance

CUE33 = [
    "yao liao lao lao",
    "zen me liao dao na",
    "zen me wei liao nao",
    "na me yao liao dao na",
    "zen me liao liao dao nao",
    "zen me bu yao liao nao",
    "zen me liao dao na li",
]


def test_cue33_seven_narrow_readings_cannot_sustain_question():
    r = assess_witness(
        CUE33,
        physical_target="342a821aa889",
        witness_class="candidate_blind_pinyin",
        policy=load_policy(),
    )
    assert r["status"] == "SELF_INCONSISTENT"
    assert r["can_raise_question"] is True and r["can_sustain_question"] is False
    assert len(r["readings"]) == 7


def test_syllable_distance_tone_and_missing_controls():
    assert normalized_edit_distance("bù xǔ", "bu4 xu3") == 0
    assert normalized_edit_distance("nǚ", "nv3") == 0
    assert normalized_edit_distance("bu xu", "bu shi") == 0.5
    assert (
        assess_witness(["bu xu"] * 7, physical_target="p", witness_class="w")["status"]
        == "CONSISTENT"
    )
    assert (
        assess_witness(["", "?", "bu xu"], physical_target="p", witness_class="w")["status"]
        == "INSUFFICIENT"
    )
    with pytest.raises(ValueError):
        assess_witness(CUE33, physical_target="", witness_class="w")


def test_thresholds_are_policy_data():
    p = load_policy()
    p["witness_consistency"]["max_normalized_edit_distance"] = 1
    p["witness_consistency"]["min_conflicting_pair_fraction"] = 0
    assert (
        assess_witness(CUE33, physical_target="p", witness_class="w", policy=p)["status"]
        == "CONSISTENT"
    )
