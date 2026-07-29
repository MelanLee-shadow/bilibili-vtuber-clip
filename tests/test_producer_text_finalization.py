from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from src.autoslice.producer_text_finalization import (
    verify_chat_authority_final_surfaces,
)


def _srt(*cues: tuple[int, int, str]) -> str:
    blocks = []
    for index, (start_ms, end_ms, text) in enumerate(cues, start=1):
        start_seconds, start_millis = divmod(start_ms, 1_000)
        end_seconds, end_millis = divmod(end_ms, 1_000)
        blocks.append(
            f"{index}\n"
            f"00:00:{start_seconds:02d},{start_millis:03d} --> "
            f"00:00:{end_seconds:02d},{end_millis:03d}\n"
            f"{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _truth_row(
    *,
    truth_id: str,
    start_ms: int,
    end_ms: int,
    canonical: str,
    boundary_role: str = "story_content",
) -> dict:
    return {
        "truth_id": truth_id,
        "required": True,
        "boundary_role": boundary_role,
        "action": "replace_cue",
        "local_windows": [{"start_ms": start_ms, "end_ms": end_ms}],
        "declared_output_contract": {
            "action": "replace_cue",
            "canonical_texts": [canonical],
            "required_text": "",
        },
    }


def _audit(*rows: dict, baseline: dict | None = None) -> dict:
    audit = {
        "source_subtitle_truth_audit": {
            "status": "ALREADY_SATISFIED",
            "applied": [],
            "satisfied": [deepcopy(row) for row in rows],
        }
    }
    if baseline is not None:
        audit["redelivery_subtitle_baseline_audit"] = baseline
    return audit


def test_ordinary_post_context_truth_is_explicitly_context_only() -> None:
    audit = _audit(
        _truth_row(
            truth_id="ordinary-post-context",
            start_ms=11_000,
            end_ms=12_000,
            canonical="后续正确文本",
        )
    )
    final = _srt((0, 5_000, "当前故事完整收尾"))

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )

    row = audit["source_subtitle_truth_audit"]["satisfied"][0]
    receipt = audit["final_source_truth_owner_verification"]
    assert row["final_owner_scope"] == ("CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY")
    assert row["final_owner_verified"] is False
    assert row["final_owner_windows"][0]["delivery_relation"] == ("OUTSIDE_AFTER_FINAL_DELIVERY")
    assert receipt["final_delivery_interval"] == {
        "timeline": "padded_source_local_ms",
        "interval_semantics": "half_open",
        "start_ms": 5_000,
        "end_ms": 10_000,
    }
    assert receipt["required_truth_row_count"] == 0
    assert receipt["context_only_truth_row_count"] == 1
    assert receipt["context_only_truth_ids"] == ["ordinary-post-context"]
    assert receipt["context_only_truth_evidence"] == [
        {
            "truth_id": "ordinary-post-context",
            "boundary_role": "story_content",
            "final_owner_scope": (
                "CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY"
            ),
            "final_owner_windows": row["final_owner_windows"],
        }
    ]
    assert receipt["required_window_count"] == 0


def test_ordinary_truth_inside_final_delivery_remains_required() -> None:
    row = _truth_row(
        truth_id="ordinary-story-owner",
        start_ms=7_000,
        end_ms=8_000,
        canonical="正文正确文本",
    )
    wrong = _srt((2_000, 3_000, "正文错误文本"))
    audit = _audit(row)

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=wrong,
        final_speaker_srt=wrong,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )
    receipt = audit["final_source_truth_owner_verification"]
    assert receipt["required_truth_row_count"] == 1
    assert receipt["context_only_truth_row_count"] == 0
    assert "SOURCE_TRUTH_FINAL_CONTRACT_MISMATCH" in {
        failure["reason_code"] for failure in receipt["failures"]
    }


def test_contiguous_projected_truth_accepts_exact_final_recue_merge() -> None:
    """Release hygiene may merge two adjacent projected cues without drift."""

    row = _truth_row(
        truth_id="contiguous-recue-merge",
        start_ms=1_000,
        end_ms=4_280,
        canonical="切，那就差礼墨没吃了",
    )
    row.update(
        {
            "cue_indexes": [16, 17],
            "resolved_target_projection": {
                "schema_version": (
                    "source-truth-resolved-target-projection.v1"
                ),
                "selector": (
                    "half-open-overlap-gte-min-then-action-resolution"
                ),
                "min_overlap_ms": 80,
                "action": "replace_cue",
                "status": "RESOLVED",
                "cues": [
                    {
                        "cue_index": 16,
                        "start_ms": 1_000,
                        "end_ms": 1_720,
                        "before_text": "切，",
                        "after_text": "切，",
                    },
                    {
                        "cue_index": 17,
                        "start_ms": 1_720,
                        "end_ms": 4_280,
                        "before_text": "那就差礼墨没吃了",
                        "after_text": "那就差礼墨没吃了",
                    },
                ],
            },
        }
    )
    merged = _srt((1_000, 4_280, "切，那就差礼墨没吃了"))
    audit = _audit(row)

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=merged,
        final_speaker_srt=merged,
        delivery_start_ms=0,
        delivery_end_ms=5_000,
    )
    verified = audit["source_subtitle_truth_audit"]["satisfied"][0]
    receipt = verified["final_owner_contract"]["recue_coalescence"]
    assert receipt["status"] == "PASS"
    assert receipt["projected_window_count"] == 2
    assert verified["final_owner_verified"] is True


def test_contiguous_projected_truth_recue_merge_rejects_extra_speech() -> None:
    row = _truth_row(
        truth_id="contiguous-recue-extra",
        start_ms=1_000,
        end_ms=4_280,
        canonical="切，那就差礼墨没吃了",
    )
    row.update(
        {
            "cue_indexes": [16, 17],
            "resolved_target_projection": {
                "schema_version": (
                    "source-truth-resolved-target-projection.v1"
                ),
                "selector": (
                    "half-open-overlap-gte-min-then-action-resolution"
                ),
                "min_overlap_ms": 80,
                "action": "replace_cue",
                "status": "RESOLVED",
                "cues": [
                    {
                        "cue_index": 16,
                        "start_ms": 1_000,
                        "end_ms": 1_720,
                        "before_text": "切，",
                        "after_text": "切，",
                    },
                    {
                        "cue_index": 17,
                        "start_ms": 1_720,
                        "end_ms": 4_280,
                        "before_text": "那就差礼墨没吃了",
                        "after_text": "那就差礼墨没吃了",
                    },
                ],
            },
        }
    )
    merged = _srt(
        (900, 4_400, "前句切，那就差礼墨没吃了后句")
    )
    audit = _audit(row)

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=merged,
        final_speaker_srt=merged,
        delivery_start_ms=0,
        delivery_end_ms=5_000,
    )
    verified = audit["source_subtitle_truth_audit"]["satisfied"][0]
    receipt = verified["final_owner_contract"]["recue_coalescence"]
    assert receipt["status"] == "FAIL"
    assert receipt["text_payload"] == (
        "前句切那就差礼墨没吃了后句"
    )


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "expected_relation"),
    [
        (4_500, 5_500, "STRADDLES_FINAL_DELIVERY_START"),
        (9_500, 10_500, "STRADDLES_FINAL_DELIVERY_END"),
        (4_500, 10_500, "STRADDLES_BOTH_FINAL_DELIVERY_ENDPOINTS"),
    ],
)
def test_truth_straddling_final_endpoint_fails_closed(
    start_ms: int,
    end_ms: int,
    expected_relation: str,
) -> None:
    audit = _audit(
        _truth_row(
            truth_id="straddling-story-owner",
            start_ms=start_ms,
            end_ms=end_ms,
            canonical="不能靠残片放行",
        )
    )
    final = _srt((0, 5_000, "不能靠残片放行"))

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )
    receipt = audit["final_source_truth_owner_verification"]
    assert receipt["straddling_truth_row_count"] == 1
    assert receipt["failures"][0]["reason_code"] == ("SOURCE_TRUTH_FINAL_WINDOW_STRADDLES_DELIVERY")
    assert receipt["failures"][0]["windows"][0]["delivery_relation"] == expected_relation


def test_next_topic_witness_uses_interval_not_role_to_decide_ownership() -> None:
    outside = _truth_row(
        truth_id="next-topic-outside",
        start_ms=11_000,
        end_ms=12_000,
        canonical="后话题",
        boundary_role="next_topic_witness",
    )
    inside = _truth_row(
        truth_id="next-topic-inside",
        start_ms=7_000,
        end_ms=8_000,
        canonical="误裁进来的后话题",
        boundary_role="next_topic_witness",
    )
    final = _srt((2_000, 3_000, "错误表面"))

    outside_audit = _audit(outside)
    assert verify_chat_authority_final_surfaces(
        outside_audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )
    assert (
        outside_audit["final_source_truth_owner_verification"]["context_only_truth_row_count"] == 1
    )

    inside_audit = _audit(inside)
    assert not verify_chat_authority_final_surfaces(
        inside_audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )
    assert inside_audit["final_source_truth_owner_verification"]["required_truth_row_count"] == 1


def test_context_only_truth_does_not_relax_baseline_final_surface() -> None:
    audit = _audit(
        _truth_row(
            truth_id="ordinary-post-context",
            start_ms=11_000,
            end_ms=12_000,
            canonical="后续正确文本",
        ),
        baseline={
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "text": "人工审定正文",
                }
            ],
        },
    )
    wrong = _srt((0, 1_000, "被错误改写的正文"))

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=wrong,
        final_speaker_srt=wrong,
        delivery_start_ms=5_000,
        delivery_end_ms=10_000,
    )
    assert audit["final_source_truth_owner_verification"]["status"] == "PASS"
    assert audit["final_redelivery_baseline_owner_verification"]["status"] == "FAIL"
    assert audit["final_verification_failure"] == ("REDELIVERY_BASELINE_FINAL_OWNER_NOT_VERIFIED")


def test_hash_bound_release_grade_merge_preserves_baseline_owner() -> None:
    final = _srt(
        (0, 880, "哦，这样吗"),
        (2_000, 4_560, "嘻，晓得吧，行"),
    )
    final_hash = hashlib.sha256(final.encode("utf-8")).hexdigest()
    receipts = [
        {"block": 1, "text": "哦", "action": "MERGED_INTO_NEXT"},
        {"block": 3, "text": "行", "action": "MERGED_INTO_PREV"},
    ]
    audit = _audit(
        baseline={
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 240,
                    "text": "哦",
                },
                {
                    "baseline_cue_index": 2,
                    "start_ms": 240,
                    "end_ms": 880,
                    "text": "这样吗",
                },
                {
                    "baseline_cue_index": 3,
                    "start_ms": 2_000,
                    "end_ms": 3_480,
                    "text": "嘻，晓得吧",
                },
                {
                    "baseline_cue_index": 4,
                    "start_ms": 3_560,
                    "end_ms": 4_560,
                    "text": "行",
                },
            ],
            "final_release_grade_cue_merges": deepcopy(receipts),
            "post_release_grade_output_sha256": final_hash,
        },
    )
    audit["final_release_grade_cue_merges"] = deepcopy(receipts)
    audit["final_output_srt_sha256"] = final_hash
    audit["final_verification_failure"] = (
        "REDELIVERY_BASELINE_FINAL_OWNER_NOT_VERIFIED"
    )

    assert verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=5_000,
    )
    receipt = audit["final_redelivery_baseline_owner_verification"]
    assert receipt["status"] == "PASS"
    assert "final_verification_failure" not in audit
    assert receipt["release_grade_merge_group_count"] == 2
    assert all(
        row["final_owner_scope"]
        == "DETERMINISTIC_RELEASE_GRADE_CUE_MERGE"
        for row in audit["redelivery_subtitle_baseline_audit"]["mappings"]
    )


@pytest.mark.parametrize("tamper", ["hash", "receipt", "extra_text"])
def test_release_grade_merge_owner_receipt_fails_closed(tamper: str) -> None:
    final = _srt((0, 880, "哦，这样吗"))
    final_hash = hashlib.sha256(final.encode("utf-8")).hexdigest()
    receipts = [
        {"block": 1, "text": "哦", "action": "MERGED_INTO_NEXT"},
    ]
    audit = _audit(
        baseline={
            "status": "APPLIED",
            "mappings": [
                {
                    "baseline_cue_index": 1,
                    "start_ms": 0,
                    "end_ms": 240,
                    "text": "哦",
                },
                {
                    "baseline_cue_index": 2,
                    "start_ms": 240,
                    "end_ms": 880,
                    "text": "这样吗",
                },
            ],
            "final_release_grade_cue_merges": deepcopy(receipts),
            "post_release_grade_output_sha256": final_hash,
        },
    )
    audit["final_release_grade_cue_merges"] = deepcopy(receipts)
    audit["final_output_srt_sha256"] = final_hash
    if tamper == "hash":
        audit["redelivery_subtitle_baseline_audit"][
            "post_release_grade_output_sha256"
        ] = "0" * 64
    elif tamper == "receipt":
        audit["final_release_grade_cue_merges"][0]["action"] = (
            "MERGED_INTO_PREV"
        )
    else:
        final = _srt((0, 880, "哦，这样吗，额外词"))

    assert not verify_chat_authority_final_surfaces(
        audit,
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=1_000,
    )
    assert audit["final_verification_failure"] == (
        "REDELIVERY_BASELINE_FINAL_OWNER_NOT_VERIFIED"
    )
