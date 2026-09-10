"""Deduplicating a selectable surface must not delete its source evidence."""

import json

from src.autoslice.acoustic_witness_adjudication import _word_choice_prompt


def prompt(request):
    return _word_choice_prompt(
        llm_call=lambda _: "",
        check_request=request,
        witness={
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "candidate_blind_transcript",
            "status": "OBSERVED",
            "target_audible": True,
            "candidate_exposure": "none",
            "exact_transcript": "新听写",
        },
    )[0]


def test_bcut_source_survives_when_source_text_equals_current():
    request = {
        "current_cue": "同一段原始口播",
        "proposed_cue": "新听写",
        "candidate_provenance": {"kind": "new_audio"},
        "proposed_candidates": [
            {
                "text": "同一段原始口播",
                "source": {
                    "kind": "source_asr_draft",
                    "provider": "bcut",
                    "source_srt_sha256": "a" * 64,
                    "source_response_sha256": "b" * 64,
                },
            }
        ],
    }
    rendered = prompt(request)
    assert "source_asr_draft" in rendered
    assert "a" * 64 in rendered and "b" * 64 in rendered
    assert "CURRENT 的既有来源" in rendered
    assert "来源不是新的独立证人" in rendered


def test_current_sources_do_not_become_duplicate_selectable_proposals():
    result = _word_choice_prompt(
        llm_call=lambda _: "",
        check_request={
            "current_cue": "原话",
            "proposed_cue": "新听写",
            "proposed_candidates": [{"text": "原话", "source": {"kind": "bcut"}}],
        },
        witness={
            "witness_protocol": "candidate_blind_transcript",
            "status": "OBSERVED",
            "target_audible": True,
            "candidate_exposure": "none",
            "exact_transcript": "新听写",
        },
    )
    assert list(result[4]) == ["PROPOSAL"]


def test_other_source_text_stays_in_proposed_set_not_current_provenance():
    rendered = prompt(
        {
            "current_cue": "当前",
            "proposed_cue": "新听写",
            "proposed_candidates": [{"text": "另一种", "source": {"kind": "other_original"}}],
        }
    )
    assert "other_original" in rendered
    assert "CURRENT 的既有来源" not in rendered


def test_same_current_source_is_displayed_once():
    source = {"text": "原话", "source": {"kind": "duplicate_source_marker"}}
    rendered = prompt(
        {
            "current_cue": "原话",
            "proposed_cue": "新听写",
            "proposed_candidates": [source, json.loads(json.dumps(source))],
        }
    )
    assert rendered.count("duplicate_source_marker") == 1
