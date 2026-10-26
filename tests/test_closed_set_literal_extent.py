"""A bound substring is not evidence that a proposed whole cue appears in chat."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from src.autoslice.closed_set_evidence import closed_set_structured_evidence
from src.autoslice.missing_proposal_bootstrap import converge_missing_proposal


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


def test_sender_context_is_source_bound_but_sender_is_not_spoken_literal():
    srt = "1\n00:03:35,670 --> 00:03:38,310\n这个大黑手投胎\n"
    source_sha256 = "b" * 64
    context = {
        "structured_chat": [
            {
                "sender": "大黑鼠头胎轴二胎满",
                "text": "张无忌，谢谢你总是替大家着想",
                "offset_ms": 200870,
                "kind": "superchat",
                "source_event_id": "sc-70-200870",
                "source_sha256": source_sha256,
            },
            {
                "sender": "missing-hash",
                "text": "张无忌",
                "offset_ms": 200900,
                "kind": "superchat",
                "source_event_id": "sc-no-hash",
            },
            {
                "sender": "missing-event",
                "text": "张无忌",
                "offset_ms": 200910,
                "kind": "superchat",
                "source_sha256": source_sha256,
            },
            {
                "sender": "remote-nickname",
                "text": "张无忌",
                "offset_ms": -750000,
                "kind": "superchat",
                "source_event_id": "sc-remote",
                "source_sha256": "c" * 64,
            },
        ]
    }
    finding = {"cue_index": 1, "suspect": "投胎", "suggestion": "这个大黑手投胎"}
    evidence = closed_set_structured_evidence(
        srt, finding, proposed_cue="这个大黑手投胎", clip_context=context
    )
    sender_context = evidence["structured_chat_sender_context"]
    assert sender_context["authority"] == "EVIDENCE_ONLY"
    assert sender_context["spoken_text_verified"] is False
    assert sender_context["mutation_authorized"] is False
    assert sender_context["rows"] == [
        {
            "sender": "大黑鼠头胎轴二胎满",
            "text": "张无忌，谢谢你总是替大家着想",
            "offset_ms": 200870,
            "kind": "superchat",
            "source_event_id": "sc-70-200870",
            "source_sha256": source_sha256,
            "authority": "EVIDENCE_ONLY",
            "spoken_text_verified": False,
            "mutation_authorized": False,
        }
    ]
    assert evidence["structured_chat_binding"]["bound_event_count"] == 0

    sender_as_candidate = closed_set_structured_evidence(
        srt,
        {"cue_index": 1, "suspect": "投胎", "suggestion": "大黑鼠头胎轴二胎满"},
        proposed_cue="大黑鼠头胎轴二胎满",
        clip_context=context,
    )
    binding = sender_as_candidate["structured_chat_binding"]
    assert binding["matched_field"] == "sender"
    assert sender_as_candidate["structured_chat_candidate_extent"]["scope"] == (
        "DECLARED_SURFACE_ONLY"
    )
    assert sender_as_candidate["structured_chat_candidate_extent"][
        "whole_candidate_literal_event_count"
    ] == 0


def test_delayed_prior_sc_sender_uses_normal_kind_window_without_speech_authority():
    srt = "1\n00:00:00,250 --> 00:00:01,730\n一条待核短句\n"
    def event(kind, offset, event_id):
        return {"kind": kind, "offset_ms": offset, "sender": event_id,
                "text": "这是原始消息", "source_event_id": event_id,
                "source_sha256": "b" * 64}
    context = {"structured_chat": [
        event("superchat", -824380, "delayed-sc"),
        event("superchat", -910000, "too-old-sc"),
        event("danmaku", -50000, "old-danmaku"),
        event("danmaku", -15000, "prior-danmaku"),
        event("gift", -119000, "prior-gift"),
        event("guard", -250000, "prior-guard"),
    ]}
    evidence = closed_set_structured_evidence(
        srt, {"cue_index": 1, "suspect": "待核", "suggestion": "未知"},
        proposed_cue="一条未知短句", clip_context=context,
    )
    sender = evidence["structured_chat_sender_context"]
    assert [r["source_event_id"] for r in sender["rows"]] == [
        "delayed-sc", "prior-danmaku", "prior-gift", "prior-guard",
    ]
    assert sender["spoken_text_verified"] is False
    assert sender["mutation_authorized"] is False
    assert all(r["authority"] == "EVIDENCE_ONLY" for r in sender["rows"])
    assert evidence["structured_chat_candidate_extent"]["scope"] == "NO_SOURCE_TEXT_MATCH"


def test_missing_proposal_convergence_keeps_sender_identity_in_closed_set_evidence(
    monkeypatch,
):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    srt = "1\n00:03:35,670 --> 00:03:38,310\n这个大黑手投胎\n"
    source_sha256 = "b" * 64
    context = {
        "structured_chat": [
            {
                "sender": "大黑鼠头胎轴二胎满",
                "text": "张无忌，谢谢你总是替大家着想",
                "offset_ms": 200870,
                "kind": "superchat",
                "source_event_id": "sc-70-200870",
                "source_sha256": source_sha256,
            }
        ]
    }
    prompts = []

    def llm_call(prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "decision": "KEEP_EXISTING",
                "replacement_text": "",
                "needs_audio": False,
                "reason": "只验证证据收据接线",
            },
            ensure_ascii=False,
        )

    rebuilt, audit = converge_missing_proposal(
        srt_text=srt,
        finding={"cue_index": 1, "suspect": "投胎", "proposed_full_cue": "这个大黑手投胎"},
        structured_chat="-[SC] 张无忌，谢谢你总是替大家着想",
        clip_context=context,
        proposal_audit={},
        llm_call=llm_call,
        derive_single_span_edit=lambda *args, **kwargs: ("", "", 0, 0, None),
    )
    assert rebuilt is not None and audit["decision"] == "KEEP_EXISTING"
    sender_context = audit["closed_set_structured_evidence"]["structured_chat_sender_context"]
    assert sender_context["rows"][0]["source_event_id"] == "sc-70-200870"
    assert sender_context["rows"][0]["source_sha256"] == source_sha256
    assert sender_context["rows"][0]["sender"] == "大黑鼠头胎轴二胎满"
    assert sender_context["rows"][0]["text"].startswith("张无忌")
    assert prompts and "sc-70-200870" in prompts[0]
    assert source_sha256 in prompts[0]


@pytest.mark.parametrize(
    ("row", "sender_bound"),
    [
        (
            {
                "sender": "missing-event-sender",
                "text": "张无忌",
                "offset_ms": 200870,
                "source_sha256": "d" * 64,
            },
            False,
        ),
        (
            {
                "sender": "missing-hash-sender",
                "text": "张无忌",
                "offset_ms": 200870,
                "source_event_id": "sc-no-hash",
            },
            False,
        ),
        (
            {
                "sender": "remote-full-sender",
                "text": "张无忌",
                "offset_ms": 150000,
                "source_event_id": "sc-remote",
                "source_sha256": "e" * 64,
            },
            False,
        ),
        (
            {
                "sender": "valid-near-sender",
                "text": "张无忌",
                "offset_ms": 200870,
                "source_event_id": "sc-valid",
                "source_sha256": "f" * 64,
            },
            True,
        ),
    ],
)
def test_sender_binding_requires_hash_event_and_nearby_offset(row, sender_bound):
    srt = "1\n00:03:35,670 --> 00:03:38,310\n这个大黑手投胎\n"
    evidence = closed_set_structured_evidence(
        srt,
        {"cue_index": 1, "suspect": "投胎", "suggestion": row["sender"]},
        proposed_cue=row["sender"],
        clip_context={"structured_chat": [row]},
    )
    binding = evidence["structured_chat_binding"]
    if sender_bound:
        assert binding["matched_field"] == "sender"
        assert evidence["structured_chat_candidate_extent"]["scope"] == (
            "DECLARED_SURFACE_ONLY"
        )
    else:
        assert binding["bound_event_count"] == 0
        assert binding["matched_field"] is None
