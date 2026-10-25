import hashlib
import json

import pytest

from src.autoslice.retained_current_text_view import (
    RetainedCurrentTextViewError,
    build_retained_current_text_view,
    is_zero_deletion_identity_plan,
)


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _srt(*, straddling: bool = False) -> bytes:
    middle = "00:00:00,110 --> 00:00:00,120\n删除区间"
    if straddling:
        middle = "00:00:00,090 --> 00:00:00,150\n跨越删除边界"
    return (
        "1\n00:00:00,010 --> 00:00:00,030\n保留前段\n\n"
        f"2\n{middle}\n\n"
        "10\n00:00:00,150 --> 00:00:00,170\n保留后段\n"
    ).encode("utf-8")


def _inputs(source_srt: bytes):
    source_hash = _sha256(source_srt)
    authority = b'{"authority":"parent"}'
    context = b'{"clip":"parent-context"}'
    media_raw_hash = "a" * 64
    media_hash = "sha256:" + media_raw_hash
    source_binding = {
        "source_srt_sha256": source_hash,
        "source_sha256": media_raw_hash,
        "source_media_binding": media_hash,
    }
    plan = {
        "schema_version": "talk-filler-plan.v1",
        "status": "active",
        "source_start_ms": 0,
        "source_end_ms": 200,
        "source_srt_sha256": source_hash,
        "effective_duration_ms": 160,
        "removed_duration_ms": 40,
        "retained_intervals": [
            {"start_ms": 0, "end_ms": 100},
            {"start_ms": 140, "end_ms": 200},
        ],
        "removals": [
            {"source_start_ms": 100, "source_end_ms": 140},
        ],
        "global_verification": {
            "status": "PASS",
            "reason_code": "GLOBAL_SEMANTIC_INVARIANTS_PASSED",
        },
    }
    # Physical source clock = delivery clock + 1,000 ms.  The first piece has
    # left padding and the last piece has right padding; neither inner edge
    # carries any part of the deleted interval.
    provenance = [
        {
            "source_path": "/recording/source.mp4",
            "source_sha256": media_raw_hash,
            "source_media_binding": media_hash,
            "start_ms": 990,
            "end_ms": 1100,
            "output_sha256": "sha256:" + "b" * 64,
        },
        {
            "source_path": "/recording/source.mp4",
            "source_sha256": media_raw_hash,
            "source_media_binding": media_hash,
            "start_ms": 1140,
            "end_ms": 1210,
            "output_sha256": "sha256:" + "c" * 64,
        },
    ]
    plan_bytes = _json_bytes(plan)
    provenance_bytes = _json_bytes({"piece_provenance": provenance})
    return {
        "source_srt_bytes": source_srt,
        "authority_bytes": authority,
        "clip_context_bytes": context,
        "plan": plan,
        "plan_bytes": plan_bytes,
        "plan_sha256": _sha256(plan_bytes),
        "piece_provenance": provenance,
        "provenance_bytes": provenance_bytes,
        "provenance_sha256": _sha256(provenance_bytes),
        # The first piece is three milliseconds longer than its source range;
        # the second cue must begin at that measured join, not at 100 ms.
        "piece_durations_ms": [103, 77],
        "source_binding": source_binding,
        "authority_sha256": _sha256(authority),
        "clip_context_sha256": _sha256(context),
        "source_timeline_offset_ms": 1000,
    }


def _refresh_plan_bytes(kwargs):
    kwargs["plan_bytes"] = _json_bytes(kwargs["plan"])
    kwargs["plan_sha256"] = _sha256(kwargs["plan_bytes"])


def _refresh_provenance_bytes(kwargs):
    kwargs["provenance_bytes"] = _json_bytes(
        {"piece_provenance": kwargs["piece_provenance"]}
    )
    kwargs["provenance_sha256"] = _sha256(kwargs["provenance_bytes"])


def _identity_inputs(source_srt: bytes):
    kwargs = _inputs(source_srt)
    kwargs["plan"].update(
        retained_intervals=[{"start_ms": 0, "end_ms": 200}],
        removals=[],
        effective_duration_ms=200,
        removed_duration_ms=0,
        global_verification={
            "status": "NOT_REQUIRED",
            "reason_code": "NO_AUTOMATIC_REMOVAL",
        },
    )
    kwargs["piece_provenance"] = [
        {
            **kwargs["piece_provenance"][0],
            "start_ms": 990,
            "end_ms": 1210,
        }
    ]
    kwargs["piece_durations_ms"] = [220]
    _refresh_plan_bytes(kwargs)
    _refresh_provenance_bytes(kwargs)
    return kwargs


def test_projects_at_measured_join_with_physical_offset_and_contiguous_output_indexes():
    source_srt = _srt()
    original = bytes(source_srt)
    result = build_retained_current_text_view(**_inputs(source_srt))

    assert result["status"] == "PROJECTED"
    assert result["source_srt_sha256"] == _sha256(source_srt)
    assert result["parent_authority_sha256"] == _sha256(b'{"authority":"parent"}')
    assert result["parent_clip_context_sha256"] == _sha256(b'{"clip":"parent-context"}')
    assert [row["old_cue_index"] for row in result["cue_map"]] == ["1", "10"]
    assert [row["new_cue_index"] for row in result["cue_map"]] == ["1", "2"]
    assert result["cue_map"][0]["physical_old_start_ms"] == 1010
    assert result["cue_map"][1]["new_start_ms"] == 113
    assert result["cue_map"][1]["new_end_ms"] == 133
    assert result["dropped_cues"] == [{"old_cue_index": "2", "reason": "INSIDE_REMOVED_INTERVAL"}]
    assert "1\n00:00:00,020 --> 00:00:00,040\n保留前段" in result["srt_text"]
    assert "2\n00:00:00,113 --> 00:00:00,133\n保留后段" in result["srt_text"]
    assert source_srt == original


def test_projects_strict_zero_deletion_identity_without_dropping_cues():
    kwargs = _identity_inputs(_srt())

    assert is_zero_deletion_identity_plan(kwargs["plan"]) is True
    result = build_retained_current_text_view(**kwargs)

    assert result["status"] == "PROJECTED"
    assert result["dropped_cues"] == []
    assert [row["old_cue_index"] for row in result["cue_map"]] == ["1", "2", "10"]
    assert [row["new_start_ms"] for row in result["cue_map"]] == [20, 120, 160]
    assert [row["new_end_ms"] for row in result["cue_map"]] == [40, 130, 180]


def test_identity_rejects_current_cue_outside_complete_source_range():
    source_srt = _srt() + (
        "\n11\n00:00:00,250 --> 00:00:00,270\n范围外正文\n"
    ).encode("utf-8")
    kwargs = _identity_inputs(source_srt)

    with pytest.raises(
        RetainedCurrentTextViewError,
        match="IDENTITY_CURRENT_CUE_OUTSIDE_SOURCE_RANGE",
    ):
        build_retained_current_text_view(**kwargs)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("effective_duration_ms", "FILLER_PLAN_EFFECTIVE_DURATION_MS_REQUIRED"),
        ("removed_duration_ms", "FILLER_PLAN_REMOVED_DURATION_MS_REQUIRED"),
    ],
)
def test_identity_requires_explicit_zero_deletion_durations(field, expected):
    kwargs = _identity_inputs(_srt())
    kwargs["plan"].pop(field)
    _refresh_plan_bytes(kwargs)

    with pytest.raises(RetainedCurrentTextViewError, match=expected):
        build_retained_current_text_view(**kwargs)


def test_identity_rejects_nonzero_duration_or_pass_verifier():
    kwargs = _identity_inputs(_srt())
    kwargs["plan"]["removed_duration_ms"] = 1
    _refresh_plan_bytes(kwargs)
    with pytest.raises(RetainedCurrentTextViewError, match="FILLER_PLAN_REMOVED_DURATION_MS_MISMATCH"):
        build_retained_current_text_view(**kwargs)

    kwargs = _identity_inputs(_srt())
    kwargs["plan"]["global_verification"] = {
        "status": "PASS",
        "reason_code": "GLOBAL_SEMANTIC_INVARIANTS_PASSED",
    }
    _refresh_plan_bytes(kwargs)
    with pytest.raises(RetainedCurrentTextViewError, match="FILLER_PLAN_IDENTITY_VERIFICATION_INVALID"):
        build_retained_current_text_view(**kwargs)


def test_nonempty_human_removal_cannot_use_identity_verification():
    kwargs = _inputs(_srt())
    kwargs["plan"]["global_verification"] = {
        "status": "NOT_REQUIRED",
        "reason_code": "NO_AUTOMATIC_REMOVAL",
    }
    _refresh_plan_bytes(kwargs)

    with pytest.raises(RetainedCurrentTextViewError, match="FILLER_PLAN_GLOBAL_VERIFICATION_NOT_PASS"):
        build_retained_current_text_view(**kwargs)


def test_full_source_split_into_multiple_intervals_is_not_identity():
    kwargs = _identity_inputs(_srt())
    kwargs["plan"]["retained_intervals"] = [
        {"start_ms": 0, "end_ms": 100},
        {"start_ms": 100, "end_ms": 200},
    ]
    kwargs["piece_provenance"] = [
        kwargs["piece_provenance"][0],
        {
            **kwargs["piece_provenance"][0],
            "start_ms": 1100,
            "end_ms": 1210,
            "output_sha256": "sha256:" + "c" * 64,
        },
    ]
    kwargs["piece_durations_ms"] = [110, 110]
    _refresh_plan_bytes(kwargs)
    _refresh_provenance_bytes(kwargs)

    with pytest.raises(RetainedCurrentTextViewError, match="FILLER_PLAN_GLOBAL_VERIFICATION_NOT_PASS"):
        build_retained_current_text_view(**kwargs)


def test_rejects_source_srt_drift_before_projection():
    kwargs = _inputs(_srt())
    kwargs["source_srt_bytes"] = _srt().replace("保留前段".encode(), "改过文本".encode())

    with pytest.raises(RetainedCurrentTextViewError, match="SOURCE_SRT_PLAN_HASH_MISMATCH"):
        build_retained_current_text_view(**kwargs)


@pytest.mark.parametrize("edge", ["head", "tail"])
def test_projects_verified_edge_removal_without_requiring_deleted_cue_to_survive(edge):
    kwargs = _inputs(_srt())
    keep = {"start_ms": 140, "end_ms": 200} if edge == "head" else {"start_ms": 0, "end_ms": 100}
    remove = {"source_start_ms": 0, "source_end_ms": 140} if edge == "head" else {"source_start_ms": 100, "source_end_ms": 200}
    kwargs["plan"].update(
        retained_intervals=[keep], removals=[remove],
        effective_duration_ms=keep["end_ms"] - keep["start_ms"],
        removed_duration_ms=remove["source_end_ms"] - remove["source_start_ms"],
    )
    index = 1 if edge == "head" else 0
    kwargs["piece_provenance"] = [kwargs["piece_provenance"][index]]
    kwargs["piece_durations_ms"] = [kwargs["piece_durations_ms"][index]]
    _refresh_plan_bytes(kwargs)
    _refresh_provenance_bytes(kwargs)

    result = build_retained_current_text_view(**kwargs)
    assert result["status"] == "PROJECTED"
    assert [row["old_cue_index"] for row in result["cue_map"]] == (["10"] if edge == "head" else ["1"])
    assert len(result["dropped_cues"]) == 2

    kwargs["plan"]["removals"] = []
    _refresh_plan_bytes(kwargs)
    with pytest.raises(RetainedCurrentTextViewError, match="FILLER_PLAN_REMOVALS_DO_NOT_MATCH_RETAINED_INTERVALS"):
        build_retained_current_text_view(**kwargs)


def test_rejects_cue_straddling_removed_interval():
    source_srt = _srt(straddling=True)
    with pytest.raises(RetainedCurrentTextViewError, match="CUE_CROSSES_REMOVED_INTERVAL"):
        build_retained_current_text_view(**_inputs(source_srt))


@pytest.mark.parametrize("value", [True, 0.5, "0"])
def test_rejects_non_integer_plan_clock(value):
    kwargs = _inputs(_srt())
    kwargs["plan"]["source_start_ms"] = value
    _refresh_plan_bytes(kwargs)

    with pytest.raises(RetainedCurrentTextViewError, match="INTEGER_REQUIRED"):
        build_retained_current_text_view(**kwargs)


@pytest.mark.parametrize("value", [True, 103.0, "103"])
def test_rejects_non_integer_piece_duration(value):
    kwargs = _inputs(_srt())
    kwargs["piece_durations_ms"][0] = value

    with pytest.raises(RetainedCurrentTextViewError, match="PIECE_DURATION_INTEGER_REQUIRED"):
        build_retained_current_text_view(**kwargs)


def test_rejects_inner_padding_and_missing_output_binding():
    kwargs = _inputs(_srt())
    kwargs["piece_provenance"][1]["start_ms"] = 1130
    _refresh_provenance_bytes(kwargs)
    with pytest.raises(RetainedCurrentTextViewError, match="PIECE_PROVENANCE_INNER_PADDING"):
        build_retained_current_text_view(**kwargs)

    kwargs = _inputs(_srt())
    del kwargs["piece_provenance"][1]["output_sha256"]
    _refresh_provenance_bytes(kwargs)
    with pytest.raises(RetainedCurrentTextViewError, match="OUTPUT_SHA256_MISSING"):
        build_retained_current_text_view(**kwargs)


def test_rejects_dict_source_media_binding_or_source_hash_mismatch():
    kwargs = _inputs(_srt())
    kwargs["piece_provenance"][0]["source_media_binding"] = {"sha256": "a" * 64}
    _refresh_provenance_bytes(kwargs)
    with pytest.raises(RetainedCurrentTextViewError, match="SOURCE_MEDIA_BINDING_INVALID"):
        build_retained_current_text_view(**kwargs)

    kwargs = _inputs(_srt())
    kwargs["piece_provenance"][0]["source_sha256"] = "d" * 64
    _refresh_provenance_bytes(kwargs)
    with pytest.raises(RetainedCurrentTextViewError, match="SOURCE_MEDIA_BINDING_MISMATCH"):
        build_retained_current_text_view(**kwargs)


def test_requires_frozen_plan_mapping_to_match_plan_bytes():
    kwargs = _inputs(_srt())
    kwargs["plan"]["source_end_ms"] = 201

    with pytest.raises(RetainedCurrentTextViewError, match="FILLER_PLAN_BYTES_MAPPING_MISMATCH"):
        build_retained_current_text_view(**kwargs)


def test_rejects_piece_row_drift_against_frozen_provenance_bytes():
    kwargs = _inputs(_srt())
    kwargs["piece_provenance"][1]["start_ms"] = 1141

    with pytest.raises(RetainedCurrentTextViewError, match="PIECE_PROVENANCE_BYTES_MAPPING_MISMATCH"):
        build_retained_current_text_view(**kwargs)
