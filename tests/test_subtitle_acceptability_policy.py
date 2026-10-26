import copy
import json

import pytest

from src.autoslice.subtitle_acceptability_policy import classify_grade, load_policy, validate_policy


def test_policy_is_strict_at_every_level(tmp_path):
    policy = load_policy()
    assert policy["disclosure"]["audience_visible_allowed"] is False
    assert policy["unsourced_name"]["block"] is False
    for branch in [(), ("features",), ("grades", "A"), ("features", "word_order_rules", 0)]:
        changed = copy.deepcopy(policy)
        node = changed
        for key in branch:
            node = node[key]
        node["typo"] = True
        with pytest.raises(ValueError, match="unknown"):
            validate_policy(changed)
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":"a","schema_version":"b"}')
    with pytest.raises(ValueError, match="duplicate"):
        load_policy(path)
    path.write_text(json.dumps({**policy, "schema_version": "next"}))
    with pytest.raises(ValueError, match="schema_version"):
        load_policy(path)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("refrain", "min_occurrences", True),
        ("refrain", "min_pinyin_similarity", 1.1),
        ("refrain", "max_extra_syllables", -1),
        ("witness_consistency", "min_readings", 1),
    ],
)
def test_invalid_thresholds_rejected(section, key, value):
    p = load_policy()
    p[section][key] = value
    with pytest.raises(ValueError):
        validate_policy(p)


def test_grade_conditions_are_not_inferred_from_equivalent_text():
    p = load_policy()
    args = dict(clear_audio=False, evidence_decisive=False, meaning_carrying=False, policy=p)
    assert classify_grade(**args) == "C"
    assert classify_grade(**{**args, "meaning_carrying": True}) == "D"
    assert classify_grade(**{**args, "evidence_decisive": True}) == "B"
    assert classify_grade(**{**args, "clear_audio": True}) == "UNKNOWN"
    assert (
        classify_grade(
            **{
                **args,
                "clear_audio": True,
                "independent_wide_witnesses": 2,
                "context_and_refrain_agree": True,
            }
        )
        == "A"
    )
    assert classify_grade(**{**args, "clear_audio": None}) == "UNKNOWN"
    assert classify_grade(**{**args, "evidence_decisive": None}) == "UNKNOWN"
    assert classify_grade(**{**args, "meaning_carrying": True, "unsourced_name_only": True}) == "C"
