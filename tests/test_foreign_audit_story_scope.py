from __future__ import annotations

from copy import deepcopy

from src.autoslice.foreign_audit_story_scope import (
    OUTSIDE_IMMUTABLE_STORY_SCOPE,
    project_foreign_audit_to_story_scope,
)


_SRT = """1
00:00:00,000 --> 00:00:00,400
before scope

2
00:00:01,000 --> 00:00:02,000
inside scope

3
00:00:04,500 --> 00:00:05,500
straddles end

4
00:00:06,000 --> 00:00:07,000
after scope
"""


def _spec() -> dict[str, object]:
    return {
        "candidate_id": "candidate",
        "pieces": [{"start_ms": 0, "end_ms": 8_000}],
        "semantic_start_ms": 1_000,
        "semantic_end_ms": 5_000,
    }


def _source_language(*rows: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "source-language-preservation-audit.v1",
        "status": "BLOCKED_UNPROVEN_FOREIGN_SPEAKER",
        "unproven_foreign_introductions": list(rows),
        "introduced_foreign_cues": list(rows),
    }


def _foreign(*rows: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "foreign-script-consistency-audit.v1",
        "status": "BLOCKED_MIXED_CJK_LATIN_PHRASE",
        "mixed_cjk_latin_cues": list(rows),
        "latin_heavy_cues": [],
        "kana_cues": [],
        "cluster_gap_ms": 6_000,
    }


def _project(audit: dict[str, object]) -> dict[str, object]:
    return project_foreign_audit_to_story_scope(
        audit,
        srt_text=_SRT,
        spec=_spec(),
        durations=[8_000],
    )


def test_after_scope_source_language_finding_is_disclosed_not_blocking():
    audit = _source_language(
        {
            "cue_index": 4,
            "start_ms": 6_000,
            "end_ms": 7_000,
            "text": "after scope",
        }
    )

    projected = _project(audit)

    assert projected["status"] == OUTSIDE_IMMUTABLE_STORY_SCOPE
    assert projected["original_status"] == audit["status"]
    assert projected["unproven_foreign_introductions"] == audit[
        "unproven_foreign_introductions"
    ]
    assert projected["story_scope_projection"]["outside_count"] == 1
    assert projected["story_scope_projection"]["blocking_count"] == 0


def test_before_scope_foreign_finding_can_use_cue_time_lookup():
    audit = _foreign({"cue_index": 1, "text": "before scope"})

    projected = _project(audit)

    assert projected["status"] == OUTSIDE_IMMUTABLE_STORY_SCOPE
    row = projected["story_scope_projection"]["findings"][0]
    assert row["start_ms"] == 0
    assert row["end_ms"] == 400
    assert row["scope_disposition"] == "BEFORE_IMMUTABLE_STORY_SCOPE"


def test_in_scope_finding_remains_blocking():
    audit = _foreign({"cue_index": 2, "text": "inside scope"})

    projected = _project(audit)

    assert projected["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"
    assert projected["story_scope_projection"]["blocking_count"] == 1
    assert projected["story_scope_projection"]["findings"][0][
        "scope_disposition"
    ] == "INSIDE_IMMUTABLE_STORY_SCOPE"


def test_straddling_finding_remains_blocking():
    audit = _source_language(
        {
            "cue_index": 3,
            "start_ms": 4_500,
            "end_ms": 5_500,
            "text": "straddles end",
        }
    )

    projected = _project(audit)

    assert projected["status"] == "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"
    assert projected["story_scope_projection"]["findings"][0][
        "scope_disposition"
    ] == "STRADDLES_IMMUTABLE_STORY_SCOPE"


def test_unlocated_finding_remains_blocking():
    audit = _foreign({"cue_index": 99, "text": "missing cue"})

    projected = _project(audit)

    assert projected["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"
    assert projected["story_scope_projection"]["findings"][0][
        "scope_disposition"
    ] == "UNLOCATED_FAIL_CLOSED"


def test_mixed_inside_and_outside_findings_remain_blocking():
    audit = _foreign(
        {"cue_index": 2, "text": "inside scope"},
        {"cue_index": 4, "text": "after scope"},
    )

    projected = _project(audit)

    assert projected["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"
    assert projected["story_scope_projection"]["outside_count"] == 1
    assert projected["story_scope_projection"]["blocking_count"] == 1


def test_clean_audit_is_returned_without_shape_change():
    audit = {
        "schema_version": "foreign-script-consistency-audit.v1",
        "status": "CLEAN",
        "mixed_cjk_latin_cues": [],
        "latin_heavy_cues": [],
        "kana_cues": [],
        "cluster_gap_ms": 6_000,
    }
    preimage = deepcopy(audit)

    projected = _project(audit)

    assert projected == preimage
    assert audit == preimage


def test_unsupported_blocked_audit_is_returned_without_release():
    audit = {
        "schema_version": "future-foreign-audit.v9",
        "status": "BLOCKED_UNKNOWN",
        "findings": [{"cue_index": 4}],
    }

    assert _project(audit) == audit


def test_blocked_audit_without_valid_scope_preserves_legacy_blocker():
    audit = _foreign({"cue_index": 2, "text": "inside scope"})
    preimage = deepcopy(audit)

    projected = project_foreign_audit_to_story_scope(
        audit,
        srt_text=_SRT,
        spec={"pieces": []},
        durations=[],
    )

    assert projected == preimage
    assert audit == preimage
