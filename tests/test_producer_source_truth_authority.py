from src.autoslice.producer_source_truth_authority import (
    reconcile_required_source_truth_chat_authority,
)


def _truth_row(
    truth_id: str,
    cue_index: int,
    after_text: str,
    *,
    source_event_id: str | None = None,
    source_event_sha256: str | None = None,
) -> dict:
    return {
        "truth_id": truth_id,
        "source_event_id": source_event_id,
        "source_event_sha256": source_event_sha256,
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
                "structured_chat_canonical": "meow",
                "structured_chat_occurrence_count": 2,
            }
        ],
    }
    source_truth = {
        "applied": [
            _truth_row("909-meow-first", 58, "摸摸meow吧"),
            _truth_row("909-meow-second", 59, "meow不咬人"),
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
            "structured_chat_canonical": "meow",
            "required_occurrence_count": 2,
            "resolved_occurrence_count": 2,
            "truth_ids": [
                "909-meow-first",
                "909-meow-second",
                "909-tail",
            ],
        }
    ]


def test_source_truth_cannot_resolve_missing_entity_occurrence():
    requirement = {
        "cue_indexes": [58, 59],
        "reason_code": "REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED",
        "structured_chat_canonical": "meow",
        "structured_chat_occurrence_count": 2,
    }
    audit = {
        "status": "ENTITY_VERDICT_REQUIRED",
        "applied": [],
        "entity_verdict_required": [requirement],
    }
    source_truth = {
        "applied": [_truth_row("only-one-meow", 58, "摸摸meow吧")],
        "satisfied": [],
    }

    reconcile_required_source_truth_chat_authority(audit, source_truth)

    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["entity_verdict_required"] == [requirement]
    assert "source_truth_entity_requirement_reconciliations" not in audit


def test_structured_event_binding_includes_adjacent_operator_truth_slot():
    source_sha256 = "ab" * 32
    requirement = {
        "cue_indexes": [59, 60],
        "reason_code": "REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED",
        "source_event_id": "17790700",
        "source_sha256": source_sha256,
        "structured_chat_canonical": "meow",
        "structured_chat_occurrence_count": 2,
    }
    audit = {
        "status": "ENTITY_VERDICT_REQUIRED",
        "applied": [],
        "entity_verdict_required": [requirement],
    }
    source_truth = {
        "applied": [
            _truth_row(
                "first-adjacent-slot",
                58,
                "摸摸meow吧",
                source_event_id="17790700",
                source_event_sha256="sha256:" + source_sha256,
            ),
            _truth_row(
                "second-overlapping-slot",
                59,
                "meow不咬人",
                source_event_id="17790700",
                source_event_sha256="sha256:" + source_sha256,
            ),
            _truth_row(
                "event-tail",
                60,
                "还喜欢被敲",
                source_event_id="17790700",
                source_event_sha256="sha256:" + source_sha256,
            ),
        ],
        "satisfied": [],
    }

    reconcile_required_source_truth_chat_authority(audit, source_truth)

    assert audit["status"] == "APPLIED_AND_VERIFIED"
    assert audit["entity_verdict_required"] == []
    assert audit["source_truth_entity_requirement_reconciliations"] == [
        {
            "cue_indexes": [59, 60],
            "structured_chat_canonical": "meow",
            "required_occurrence_count": 2,
            "resolved_occurrence_count": 2,
            "truth_ids": [
                "event-tail",
                "first-adjacent-slot",
                "second-overlapping-slot",
            ],
        }
    ]


def test_structured_event_binding_does_not_cross_source_hash():
    requirement = {
        "cue_indexes": [59],
        "reason_code": "REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED",
        "source_event_id": "17790700",
        "source_sha256": "ab" * 32,
        "structured_chat_canonical": "meow",
        "structured_chat_occurrence_count": 2,
    }
    audit = {
        "status": "ENTITY_VERDICT_REQUIRED",
        "applied": [],
        "entity_verdict_required": [requirement],
    }
    source_truth = {
        "applied": [
            _truth_row(
                "overlap-one",
                59,
                "meow不咬人",
                source_event_id="17790700",
                source_event_sha256="sha256:" + "ab" * 32,
            ),
            _truth_row(
                "wrong-source-adjacent",
                58,
                "摸摸meow吧",
                source_event_id="17790700",
                source_event_sha256="sha256:" + "cd" * 32,
            ),
        ],
        "satisfied": [],
    }

    reconcile_required_source_truth_chat_authority(audit, source_truth)

    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["entity_verdict_required"] == [requirement]


def test_partitioned_truth_cues_reconcile_against_prepartition_chat_indexes():
    audit = {
        "status": "ENTITY_VERDICT_REQUIRED",
        "applied": [],
        "entity_verdict_required": [
            {
                "cue_indexes": [58],
                "structured_chat_canonical": "meow",
                "structured_chat_occurrence_count": 1,
            }
        ],
    }
    source_truth = {
        "cue_grid_partition": {
            "schema_version": "source-truth-adjacent-cue-partition.v1",
            "status": "PASS",
            "projected_cues": [
                {
                    "cue_index": index,
                    "input_cue_index": index if index < 15 else index - 1,
                    "start_ms": index * 1_000,
                    "end_ms": (index + 1) * 1_000,
                }
                for index in range(1, 60)
            ],
        },
        "applied": [_truth_row("shifted-meow", 59, "摸摸meow吧")],
        "satisfied": [],
    }

    reconcile_required_source_truth_chat_authority(audit, source_truth)

    assert audit["status"] == "APPLIED_AND_VERIFIED"
    assert audit["entity_verdict_required"] == []
    assert audit["source_truth_entity_requirement_reconciliations"][0][
        "truth_ids"
    ] == ["shifted-meow"]
