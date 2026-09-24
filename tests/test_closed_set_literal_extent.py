"""A bound substring is not evidence that a proposed whole cue appears in chat."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from src.autoslice.closed_set_evidence import closed_set_structured_evidence


def build(text: str | None, *, proposed: str = "有人在花园里面聊天", provenance=None):
    original = "有人站在街道等待"
    srt = "1\n00:00:00,000 --> 00:00:02,000\n" + original + "\n"
    finding = {"cue": 1, "replacement": "有人", "suspect": original}
    if provenance is not None:
        finding["candidate_provenance"] = provenance
    context = {
        "structured_chat": []
        if text is None
        else [
            {
                "text": text,
                "offset_ms": 1000,
                "source_sha256": "a" * 64,
                "source_event_id": "sample-1",
            }
        ]
    }
    before = copy.deepcopy((finding, context))
    result = closed_set_structured_evidence(
        srt, finding, proposed_cue=proposed, clip_context=context
    )
    assert (finding, context) == before
    unsigned = {k: v for k, v in result.items() if k != "evidence_sha256"}
    assert (
        result["evidence_sha256"]
        == "sha256:"
        + hashlib.sha256(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    return result


def test_fragment_event_count_is_preserved_but_whole_cue_is_not_claimed():
    evidence = build("这里有人等公交车")
    binding = evidence["structured_chat_binding"]
    assert binding["surface"] == "有人" and binding["bound_event_count"] == 1
    extent = evidence["structured_chat_candidate_extent"]
    assert extent["candidate"] == "PROPOSED"
    assert extent["raw_matching_event_count"] == 1
    assert extent["whole_candidate_literal_event_count"] == 0
    assert extent["scope"] == "FRAGMENT_LITERAL_ONLY"
    assert extent["mutation_authorized"] is False
    assert extent["spoken_text_verified"] is False


def test_whole_literal_occurrence_is_not_spoken_or_mutation_authority():
    result = build("弹幕说：有人在花园里面聊天。")
    extent = result["structured_chat_candidate_extent"]
    assert extent["scope"] == "WHOLE_CANDIDATE_LITERAL_PRESENT"
    assert extent["whole_candidate_literal_event_count"] == 1
    assert extent["whole_candidate_source_event_ids"] == ["sample-1"]
    assert (
        extent["candidate_text_sha256"]
        == "sha256:" + hashlib.sha256("有人在花园里面聊天".encode()).hexdigest()
    )
    assert extent["spoken_text_verified"] is False and extent["mutation_authorized"] is False


def test_declared_provenance_alone_does_not_prove_whole_literal_presence():
    proposed = "有人在花园里面聊天"
    evidence = build(
        None,
        proposed=proposed,
        provenance={
            "kind": "structured_chat_bound",
            "surface": proposed,
            "source_sha256": "b" * 64,
            "source_event_id": "declared-only",
        },
    )
    assert evidence["structured_chat_binding"]["bound_event_count"] == 1
    extent = evidence["structured_chat_candidate_extent"]
    assert extent["scope"] == "DECLARED_SURFACE_ONLY"
    assert extent["raw_matching_event_count"] == 0
    assert extent["whole_candidate_literal_event_count"] == 0


def test_no_binding_stays_no_binding():
    result = build("这里没有其他材料")
    assert result["structured_chat_binding"]["bound_event_count"] == 0
    assert result["structured_chat_candidate_extent"]["scope"] == "NO_SOURCE_TEXT_MATCH"


@pytest.mark.parametrize("text", ["有人在花园里面聊天", "有人在花园，里面聊天"])
def test_whole_match_does_not_remove_punctuation_or_guess_equivalence(text):
    result = build(text)
    assert result["structured_chat_candidate_extent"]["whole_candidate_literal_event_count"] == int(
        text == "有人在花园里面聊天"
    )


def test_counts_retain_all_matching_events_without_treating_them_as_votes():
    srt = "1\n00:00:00,000 --> 00:00:02,000\n原来的句子\n"
    finding = {"cue": 1, "replacement": "有人"}
    context = {
        "structured_chat": [
            {"text": "有人在花园里面聊天", "source_sha256": "a" * 64, "source_event_id": "whole"},
            {"text": "这里有人等公交车", "source_sha256": "a" * 64, "source_event_id": "fragment"},
        ]
    }
    e = closed_set_structured_evidence(
        srt, finding, proposed_cue="有人在花园里面聊天", clip_context=context
    )
    assert e["structured_chat_binding"]["surface"] == "有人"
    assert e["structured_chat_binding"]["bound_event_count"] == 2
    extent = e["structured_chat_candidate_extent"]
    assert extent["raw_matching_event_count"] == 2
    assert extent["whole_candidate_literal_event_count"] == 1
    assert extent["whole_candidate_source_event_ids"] == ["whole"]


def test_no_current_or_proposed_choice_is_changed_by_extent():
    e = build("有人来访")
    assert "choice" not in e and "status" not in e
    assert e["structured_chat_candidate_extent"]["mutation_authorized"] is False


def test_invalid_source_rows_do_not_break_new_extent_or_gain_authority():
    srt = "1\n00:00:00,000 --> 00:00:02,000\n原来的句子\n"
    context = {
        "structured_chat": [
            {"text": "有人在花园里面聊天", "source_sha256": []},
            {"text": "这里有人等车", "source_sha256": "a" * 64, "source_event_id": "real-fragment"},
        ]
    }
    e = closed_set_structured_evidence(
        srt,
        {"cue": 1, "replacement": "有人"},
        proposed_cue="有人在花园里面聊天",
        clip_context=context,
    )
    assert e["structured_chat_binding"]["bound_event_count"] == 1
    assert e["structured_chat_candidate_extent"]["scope"] == "FRAGMENT_LITERAL_ONLY"
    assert e["structured_chat_candidate_extent"]["whole_candidate_literal_event_count"] == 0


def test_fidelity_current_evidence_extent_is_bound_to_current_not_proposed():
    current = "有人在花园里面聊天"
    srt = "1\n00:00:00,000 --> 00:00:02,000\n" + current + "\n"
    provenance = {
        "kind": "draft_fidelity_kept",
        "kept_candidate": "CURRENT",
        "draft_fidelity_kept": True,
        "surface": current,
        "kept_text_sha256": "sha256:" + hashlib.sha256(current.encode()).hexdigest(),
    }
    finding = {"cue": 1, "candidate_provenance": provenance}
    context = {
        "structured_chat": [
            {"text": current, "source_sha256": "a" * 64, "source_event_id": "current"}
        ]
    }
    e = closed_set_structured_evidence(srt, finding, proposed_cue="别的句子", clip_context=context)
    extent = e["structured_chat_candidate_extent"]
    assert extent["candidate"] == "CURRENT"
    assert extent["candidate_text_sha256"] == provenance["kept_text_sha256"]
    assert extent["whole_candidate_literal_event_count"] == 1
    assert extent["mutation_authorized"] is False
