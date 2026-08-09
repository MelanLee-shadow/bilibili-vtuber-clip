"""Ivan 2026-08-08 Wave 8 F3/F1 regression canaries."""

from __future__ import annotations

import json
from pathlib import Path

from src.autoslice.acoustic_witness_adjudication import (
    adjudicate_with_witness,
    judge_word_choice,
)
from src.autoslice.chat_authority import load_referent_groups
from src.autoslice.final_review_auditor import audit_final_subtitles


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


def test_f3_cue2_pattern_blocks_unsupported_proposed_over_witness_conflict():
    repaired, reason, audit = adjudicate_with_witness(
        check_request=_request(),
        witness=_witness("wo shi wo bao shi de"),
        llm_call=_choose_proposed,
    )

    assert repaired is False
    assert reason == "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT"
    assert audit["witness_conflict_gate"]["reason_code"] == reason
    assert audit["witness_diagnostic_conflict"] is True


def test_f3_registered_expected_value_direction_still_wins_conflict():
    repaired, reason, audit = adjudicate_with_witness(
        check_request=_request(
            current_cue="你听到停放熊在门口叫了吗",
            proposed_cue="你听到kmx在门口叫了吗",
            suspect="停放熊",
            replacement="kmx",
            repair_class="source_backed_entity",
            candidate_provenance={"kind": "glossary", "surface": "kmx"},
        ),
        witness=_witness("ni ting dao ting fang xiong zai men kou jiao le ma"),
        llm_call=_choose_proposed,
    )

    assert repaired is True
    assert reason == "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT"
    assert audit["witness_conflict_gate"]["registered_direction"] is True


def test_f3_session_restatement_is_structured_support():
    repaired, reason, audit = adjudicate_with_witness(
        check_request=_request(
            candidate_provenance={
                "kind": "session_restatement",
                "surface": "我操，我报仇了",
            }
        ),
        witness=_witness("wo shi wo bao shi de"),
        llm_call=_choose_proposed,
    )

    assert repaired is True
    assert reason == "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT"
    assert audit["witness_conflict_gate"]["structured_text_support"] is True


def test_f3_prompt_states_ivan_pinyin_first_evidence_fallback_duty():
    prompts: list[str] = []

    judge_word_choice(
        check_request=_request(),
        witness=_witness("wo shi wo bao shi de"),
        llm_call=lambda prompt: prompts.append(prompt) or json.dumps({"choice": "CURRENT"}),
    )

    assert "优先在与证人听写拼音相容的候选内选择" in prompts[0]
    assert "PROPOSED 与听写明显不相容且无独立结构化证据时选择 CURRENT" in prompts[0]
    assert "Ivan 2026-08-08" in prompts[0]


def test_f1_nantian_transcript_repeat_is_marked_as_suspected_echo():
    findings = audit_final_subtitles(
        _srt("南天今天也来了", "我看到南田老师上线了", "南天刚刚说过了"),
        llm_call=_review(
            [
                {
                    "cue": 2,
                    "kind": "entity",
                    "proposed_full_cue": "我看到南天老师上线了",
                    "repair_class": "source_backed_entity",
                    "source_surface": "南天",
                    "why": "同片多处写成南天",
                }
            ]
        ),
        extract_json=json.loads,
        glossary_text="- 南町：南天是已登记误听面，不是规范写法",
    )

    provenance = findings[0]["candidate_provenance"]
    assert provenance["kind"] == "transcript_echo_suspected"
    assert provenance["surface"] == "南天"
    assert provenance["canonical"] == "南町"
    assert {"南町", "大N", "大N老师", "小N", "南町nightin"} <= set(
        provenance["legal_surfaces"]
    )
    assert findings[0]["force_acoustic"] is True
    assert findings[0]["correlated_text_witness"] is False


def test_f1_legal_dan_teacher_surface_keeps_transcript_context_support():
    findings = audit_final_subtitles(
        _srt("大N老师今天也来了", "我看到大恩老师上线了"),
        llm_call=_review(
            [
                {
                    "cue": 2,
                    "kind": "entity",
                    "proposed_full_cue": "我看到大N老师上线了",
                    "repair_class": "source_backed_entity",
                    "source_surface": "大N老师",
                    "why": "同片使用合法昵称",
                }
            ]
        ),
        extract_json=json.loads,
    )

    assert findings[0]["candidate_provenance"] == {
        "kind": "transcript_context",
        "surface": "大N老师",
        "nearest_cue_distance": 1,
    }


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
