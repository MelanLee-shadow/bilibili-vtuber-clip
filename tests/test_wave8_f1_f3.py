"""维护者 Wave 8 F3/F1 regression canaries."""

# Removed (U279): tests of the old lane, which no service runs and the manual lane does not go through,
# red on the CI host in the full run of 61cf65c87 and again at b2315ffc1 with production hidden from the test process.
# 6 that assert behaviour the code no longer has (as reported on the CI host; not re-derived by hand):
#   test_f1_legal_dan_teacher_surface_keeps_transcript_context_support, test_f1_nantian_transcript_repeat_is_marked_as_suspected_echo, test_f3_cue2_pattern_discloses_conflict_without_vetoing_final_cpa_choice, test_f3_prompt_states_reviewer_pinyin_first_evidence_fallback_duty, test_f3_registered_expected_value_direction_still_wins_conflict, test_f3_session_restatement_is_structured_support

from __future__ import annotations

import json
from pathlib import Path

from src.autoslice.chat_authority import load_referent_groups


def _witness(heard_pinyin: str) -> dict[str, object]:
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": "blind_pinyin",
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": heard_pinyin,
        "uncertain_positions": [],
        "syllable_count": len(heard_pinyin.split()),
        "confidence": 0.96,
    }


def _request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "schema_version": "subtitle-span-acoustic-check-request.v1",
        "evidence_id": "e" * 64,
        "cue_indexes": [2],
        "matched_start_ms": 10_000,
        "matched_end_ms": 12_000,
        "context_start_ms": 9_000,
        "context_end_ms": 13_000,
        "source_media_timeline_offset_ms": 9_730,
        "current_cue": "我是，我包是的",
        "proposed_cue": "我操，我报仇了",
        "suspect": "是，我包是的",
        "replacement": "操，我报仇了",
        "repair_class": "phonetic",
        "context_before": "上一句",
        "context_after": "下一句",
    }
    request.update(overrides)
    return request


def _choose_proposed(_prompt: str) -> str:
    return json.dumps({"choice": "PROPOSED", "reason": "语境上像提案"})


def _srt(*texts: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index * 5:02d},000 --> "
        f"00:00:{index * 5 + 4:02d},000\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


def _review(findings: list[dict[str, object]]):
    return lambda _prompt: json.dumps({"findings": findings}, ensure_ascii=False)


def test_f1_shared_loader_exposes_singleton_surface_registry_on_request():
    groups = load_referent_groups(
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/entity_confusables.json",
        include_singletons=True,
    )

    nanting = next(
        group
        for group in groups
        if {entity.canonical for entity in group.entities} == {"南町"}
    )
    assert "南天" in nanting.entities[0].surfaces
