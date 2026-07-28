from src.autoslice.producer_source_truth_authority import (
    reconcile_required_source_truth_chat_authority,
)


def _truth_row(
    truth_id: str,
    cue_index: int,
    after_text: str,
) -> dict:
    return {
        "truth_id": truth_id,
        "required": True,
        "action": "replace_cue",
        "cue_indexes": [cue_index],
        "local_windows": [
            {
                "start_ms": cue_index * 1_000,
                "end_ms": (cue_index + 1) * 1_000,
            }
        ],
        "resolved_target_projection": {
            "schema_version": "source-truth-resolved-target-projection.v1",
            "selector": "half-open-overlap-gte-min-then-action-resolution",
            "min_overlap_ms": 80,
            "action": "replace_cue",
            "status": "RESOLVED",
            "cues": [
                {
                    "cue_index": cue_index,
                    "start_ms": cue_index * 1_000,
                    "end_ms": (cue_index + 1) * 1_000,
                    "before_text": "误听",
                    "after_text": after_text,
                }
            ],
        },
    }


def test_source_truth_resolves_repeated_structured_chat_entity_slots():
    audit = {
        "status": "ENTITY_VERDICT_REQUIRED",
        "applied": [],
        "entity_verdict_required": [
            {
                "cue_indexes": [58, 59, 60, 61],
                "reason_code": "REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED",
                "structured_chat_canonical": "kmx",
                "structured_chat_occurrence_count": 2,
            }
        ],
    }
    source_truth = {
        "applied": [
            _truth_row("909-kmx-first", 58, "摸摸kmx吧"),
            _truth_row("909-kmx-second", 59, "kmx不咬人"),
            _truth_row("909-tail", 60, "还喜欢被敲"),
        ],
        "satisfied": [],
    }

    reconcile_required_source_truth_chat_authority(audit, source_truth)

    assert audit["status"] == "APPLIED_AND_VERIFIED"
    assert audit["entity_verdict_required"] == []
    assert audit["source_truth_entity_requirement_reconciliations"] == [
        {
            "cue_indexes": [58, 59, 60, 61],
            "structured_chat_canonical": "kmx",
            "required_occurrence_count": 2,
            "resolved_occurrence_count": 2,
            "truth_ids": [
                "909-kmx-first",
                "909-kmx-second",
                "909-tail",
            ],
        }
    ]


def test_source_truth_cannot_resolve_missing_entity_occurrence():
    requirement = {
        "cue_indexes": [58, 59],
        "reason_code": "REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED",
        "structured_chat_canonical": "kmx",
        "structured_chat_occurrence_count": 2,
    }
    audit = {
        "status": "ENTITY_VERDICT_REQUIRED",
        "applied": [],
        "entity_verdict_required": [requirement],
    }
    source_truth = {
        "applied": [_truth_row("only-one-kmx", 58, "摸摸kmx吧")],
        "satisfied": [],
    }

    reconcile_required_source_truth_chat_authority(audit, source_truth)

    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["entity_verdict_required"] == [requirement]
    assert "source_truth_entity_requirement_reconciliations" not in audit
