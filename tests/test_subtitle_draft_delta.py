import json

import pytest

from src.autoslice.subtitle_draft_delta import parse_draft_delta


def reply(**updates):
    row = {
        "schema_version": "cpa-draft-delta.v1",
        "review_complete": True,
        "reviewed_cue_count": 2,
        "edits": [],
        "needs_audio": [],
    }
    row.update(updates)
    return json.dumps(row, ensure_ascii=False)


def test_unchanged_text_kept_exactly_without_asking_model_to_copy():
    texts, doubts = parse_draft_delta(reply(), ["不是十五", "あれ？"])
    assert texts == {1: "不是十五", 2: "あれ？"}
    assert doubts == []


def test_only_exact_before_bound_edit_is_applied():
    text, _ = parse_draft_delta(
        reply(edits=[{"n": 2, "before": "候选", "after": "新候选"}]), ["不变", "候选"]
    )
    assert text == {1: "不变", 2: "新候选"}


@pytest.mark.parametrize(
    "edits",
    [
        [{"n": True, "before": "甲", "after": "乙"}],
        [{"n": 3, "before": "甲", "after": "乙"}],
        [{"n": 1, "before": "错前像", "after": "乙"}],
        [{"n": 1, "before": "甲", "after": ""}],
        [{"n": 1, "before": "甲", "after": "跨\n行"}],
        [{"n": 1, "before": "甲", "after": "乙"}, {"n": 1, "before": "甲", "after": "丙"}],
    ],
)
def test_invalid_changes_fail_without_returning_partial_output(edits):
    with pytest.raises(ValueError):
        parse_draft_delta(reply(edits=edits), ["甲", "丁"])


@pytest.mark.parametrize(
    "update",
    [
        {"review_complete": False},
        {"reviewed_cue_count": 1},
        {"reviewed_cue_count": True},
        {"edits": None},
        {"needs_audio": None},
    ],
)
def test_missing_review_is_not_zero_change_success(update):
    with pytest.raises(ValueError):
        parse_draft_delta(reply(**update), ["甲", "乙"])
