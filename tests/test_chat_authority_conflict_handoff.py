import copy
import hashlib
import json

import pytest

from src.autoslice.chat_authority_ownership import (
    build_chat_authority_conflict_projection,
    chat_authority_owned_cue_indexes,
    protected_review_cue_indexes,
    validate_chat_authority_conflict_projection,
)
from src.autoslice.exact_final_terminal_funnel import terminal_check
from src.autoslice.final_review_auditor import build_context_adjudication_request
from src.autoslice.final_source_language_owner import (
    validated_exact_read_superseded_applied_indexes,
)


SRT = "1\n00:00:01,000 --> 00:00:02,000\n你喜欢老男人\n"
CURRENT = "你喜欢老男人"
PROPOSED = "你喜欢死男人"
OFFSET = 10_000


def _chat_authority():
    return {
        "applied": [{
            "mode": "exact_span",
            "cue_indexes": [1],
            "matched_start_ms": OFFSET + 1_000,
            "matched_end_ms": OFFSET + 2_000,
            "evidence_id": "a" * 64,
            "kind": "danmaku",
            "sender": "viewer",
            "source": "/source/chat.xml",
            "source_sha256": "b" * 64,
            "source_event_id": "",
            "source_offset_ms": 42,
            "exact_text": CURRENT,
            "before": ["你喜欢死男人"],
            "after": [CURRENT],
            "read_aloud_verdict": {
                "authority_kind": "cpa_context_only_closed_set_adjudication",
                "decision_authority": "CPA_JUDGE",
                "witness_authority": "EVIDENCE_ONLY",
                "witness_status": "UNCERTAIN",
                "needs_audio": False,
            },
            "whole_line_exact_copy_gate": {
                "schema_version": "chat-whole-line-support-gate.v1",
                "status": "PASS",
                "proof_basis": "hash_bound_cpa_text_first_closed_set_judge",
                "owner_eligible": True,
            },
        }]
    }


def _finding():
    return {
        "cue_index": 1,
        "base_text_sha256": hashlib.sha256(CURRENT.encode()).hexdigest(),
        "suspect": CURRENT,
        "suggestion": PROPOSED,
        "proposed_full_cue": PROPOSED,
        "repair_class": "phonetic",
    }


def test_prior_chat_authority_reaches_ordinary_terminal_request_and_is_hash_bound():
    authority = _chat_authority()
    projection = build_chat_authority_conflict_projection(
        SRT, authority, cue_index=1, delivery_start_ms=OFFSET,
    )
    assert projection is not None
    assert projection["status"] == "BOUND"
    assert projection["applied_evidence_id"] == "a" * 64
    assert projection["source"] == {
        "path": "/source/chat.xml",
        "sha256": "b" * 64,
        "event_id": "",
        "offset_ms": 42,
    }
    assert projection["authority"]["kind"] == "cpa_context_only_closed_set_adjudication"
    assert projection["authority"]["witness_status"] == "UNCERTAIN"
    assert projection["mutation_authorized"] is False
    validate_chat_authority_conflict_projection(
        projection, srt_text=SRT, chat_authority_audit=authority,
    )

    without = build_context_adjudication_request(
        SRT, _finding(), source_media_timeline_offset_ms=OFFSET,
    )
    with_authority = build_context_adjudication_request(
        SRT, _finding(), source_media_timeline_offset_ms=OFFSET,
        prior_chat_authority=projection,
    )
    assert "prior_chat_authority" not in without
    assert with_authority["prior_chat_authority"] == projection
    assert with_authority["request_sha256"] != without["request_sha256"]

    terminal = terminal_check(
        SRT, _finding(), clip_context=None, offset_ms=OFFSET,
        chat_authority_audit=authority,
    )
    assert terminal["prior_chat_authority"] == projection
    assert isinstance(terminal["request_sha256"], str)


@pytest.mark.parametrize("damage", ["row_hash", "geometry", "srt_hash", "evidence_id"])
def test_stale_or_corrupt_chat_projection_fails_closed(damage):
    authority = _chat_authority()
    projection = build_chat_authority_conflict_projection(
        SRT, authority, cue_index=1, delivery_start_ms=OFFSET,
    )
    assert projection is not None
    damaged = copy.deepcopy(projection)
    if damage == "row_hash":
        damaged["applied_row_sha256"] = "c" * 64
    elif damage == "geometry":
        damaged["matched_end_ms"] += 1
    elif damage == "srt_hash":
        damaged["current_srt_sha256"] = "c" * 64
    else:
        damaged["applied_evidence_id"] = "c" * 64
    damaged["projection_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in damaged.items() if key != "projection_sha256"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with pytest.raises(ValueError):
        validate_chat_authority_conflict_projection(
            damaged, srt_text=SRT, chat_authority_audit=authority,
        )


def test_valid_registered_source_owner_retires_only_the_old_chat_row(tmp_path, monkeypatch):
    from tests.test_final_source_language_replay import _produced

    authority, output = _produced(
        tmp_path, monkeypatch, surface="source_language", predecessors=True,
    )
    applied = authority["applied"][0]
    applied.update(
        mode="exact_span", cue_indexes=[3], evidence_id="a" * 64,
        kind="danmaku", source="/source/chat.xml", source_sha256="b" * 64,
        source_event_id="", source_offset_ms=42,
    )
    assert validated_exact_read_superseded_applied_indexes(
        authority, current_srt_text=output, delivery_start_ms=0,
    ) == frozenset({0})
    assert chat_authority_owned_cue_indexes(
        output, authority, delivery_start_ms=0,
    ) == frozenset()
    assert build_chat_authority_conflict_projection(
        output, authority, cue_index=3, delivery_start_ms=0,
    ) is None

    human = {"records": [{"units": [{
        "start_ms": 3_000, "end_ms": 4_660,
        "text": "完全に無双状態",
    }]}]}
    assert protected_review_cue_indexes(
        output, human, authority, human_offset_ms=0, chat_offset_ms=0,
    ) == frozenset({3})

    damaged = copy.deepcopy(authority)
    damaged["applied"][0]["reconciliation"]["before_sha256"] = "sha256:" + "c" * 64
    assert validated_exact_read_superseded_applied_indexes(
        damaged, current_srt_text=output, delivery_start_ms=0,
    ) == frozenset()
    assert chat_authority_owned_cue_indexes(
        output, damaged, delivery_start_ms=0,
    ) == frozenset({3})


def test_ordinary_control_without_chat_authority_is_unchanged():
    assert chat_authority_owned_cue_indexes(SRT, {}, delivery_start_ms=OFFSET) == frozenset()
    baseline = build_context_adjudication_request(
        SRT, _finding(), source_media_timeline_offset_ms=OFFSET,
    )
    assert "prior_chat_authority" not in baseline


def test_boundary_renumbered_single_cue_remains_bound_to_source_owner():
    # Xiada: source cue 4 became delivery cue 1, but both clocks describe
    # the same 9730..12330 ms and the exact source-owned text is unchanged.
    srt = "1\n00:00:00,250 --> 00:00:02,850\n主包还没忘记nus之梦\n"
    authority = _chat_authority()
    row = authority["applied"][0]
    row.update(cue_indexes=[4], matched_start_ms=9730, matched_end_ms=12330,
               before=["主播还没忘记 NUS"], after=["主包还没忘记nus之梦"],
               exact_text="主包还没忘记nus之梦")
    projection = build_chat_authority_conflict_projection(
        srt, authority, cue_index=1, delivery_start_ms=9480)
    assert projection["status"] == "BOUND"
    assert projection["cue_indexes"] == [4]
    assert projection["source_cue_position"] == 0
    validate_chat_authority_conflict_projection(
        projection, srt_text=srt, chat_authority_audit=authority)
    finding = {"cue_index": 1, "base_text_sha256": hashlib.sha256(
        "主包还没忘记nus之梦".encode()).hexdigest(), "suspect": "主包",
        "suggestion": "主播", "proposed_full_cue": "主播还没忘记nus之梦",
        "repair_class": "phonetic"}
    check = terminal_check(srt, finding, clip_context=None, offset_ms=9480,
                           chat_authority_audit=authority)
    assert check["prior_chat_authority"] == projection
    row["after"] = ["改过的字"]
    with pytest.raises(ValueError, match="CHAT_AUTHORITY_CONFLICT_RECEIPT_DRIFT"):
        validate_chat_authority_conflict_projection(
            projection, srt_text=srt, chat_authority_audit=authority)


def test_renumbered_ambiguous_owner_and_changed_text_are_rejected():
    srt = "1\n00:00:00,250 --> 00:00:02,850\n主包还没忘记nus之梦\n"
    authority = _chat_authority()
    row = authority["applied"][0]
    row.update(cue_indexes=[4], matched_start_ms=9730, matched_end_ms=12330,
               before=["主播还没忘记 NUS"], after=["主包还没忘记nus之梦"],
               exact_text="主包还没忘记nus之梦")
    finding = {"cue_index": 1, "suspect": "主包", "suggestion": "主播",
               "proposed_full_cue": "主播还没忘记nus之梦", "repair_class": "phonetic"}
    authority["applied"].append(copy.deepcopy(row))
    with pytest.raises(ValueError, match="CHAT_AUTHORITY_CONFLICT_PROJECTION_INVALID"):
        terminal_check(srt, finding, clip_context=None, offset_ms=9480,
                       chat_authority_audit=authority)
    authority["applied"].pop()
    row["after"] = ["其他内容"]
    with pytest.raises(ValueError, match="CHAT_AUTHORITY_CONFLICT_PROJECTION_INVALID"):
        terminal_check(srt, finding, clip_context=None, offset_ms=9480,
                       chat_authority_audit=authority)


def test_old_index_alone_does_not_claim_an_unrelated_current_cue():
    authority = _chat_authority()
    authority["applied"][0].update(matched_start_ms=20000, matched_end_ms=21000)
    check = terminal_check(SRT, _finding(), clip_context=None, offset_ms=OFFSET,
                           chat_authority_audit=authority)
    assert "prior_chat_authority" not in check


def test_boundary_renumbering_does_not_guess_multi_cue_position():
    authority = _chat_authority()
    authority["applied"][0].update(cue_indexes=[4, 5], before=[PROPOSED, PROPOSED],
                                   after=[CURRENT, CURRENT])
    projection = build_chat_authority_conflict_projection(
        SRT, authority, cue_index=1, delivery_start_ms=OFFSET)
    assert projection["status"] == "INVALID"


@pytest.mark.parametrize("damage", ["duplicate_cue", "unknown_kind", "absent_exact_text"])
def test_projection_writer_uses_the_same_receipt_admission_as_reader(damage):
    authority = _chat_authority()
    row = authority["applied"][0]
    if damage == "duplicate_cue":
        row.update(cue_indexes=[1, 1], before=[PROPOSED, PROPOSED], after=[CURRENT, CURRENT])
    elif damage == "unknown_kind":
        row["kind"] = "unknown"
    else:
        row["exact_text"] = "different source words"
    projection = build_chat_authority_conflict_projection(
        SRT, authority, cue_index=1, delivery_start_ms=OFFSET,
    )
    assert projection["status"] == "INVALID"
    assert chat_authority_owned_cue_indexes(SRT, authority, delivery_start_ms=OFFSET) == frozenset({1})


def test_supplied_receipt_cannot_override_changed_live_chat_authority():
    authority = _chat_authority()
    finding = _finding()
    finding["prior_chat_authority"] = build_chat_authority_conflict_projection(
        SRT, authority, cue_index=1, delivery_start_ms=OFFSET,
    )
    authority["applied"][0]["source_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="CHAT_AUTHORITY_CONFLICT_RECEIPT_DRIFT"):
        terminal_check(SRT, finding, clip_context=None, offset_ms=OFFSET,
                       chat_authority_audit=authority)


@pytest.mark.parametrize("conflict", ["same_surface", "other_surface_owner", "other_surface_applied"])
def test_duplicate_registration_claim_never_retires_first_owner(tmp_path, monkeypatch, conflict):
    from tests.test_final_source_language_replay import _produced

    authority, output = _produced(tmp_path, monkeypatch, surface="source_language", predecessors=True)
    authority["applied"][0].update(mode="exact_span", cue_indexes=[3])
    original = authority["final_source_language_cpa_surface_registrations"][0]
    duplicate = copy.deepcopy(original)
    if conflict == "same_surface":
        authority["final_source_language_cpa_surface_registrations"].append(duplicate)
    else:
        if conflict == "other_surface_applied":
            duplicate["owner_entity_repair_index"] = 99
        authority["final_foreign_script_cpa_surface_registrations"] = [duplicate]
    assert validated_exact_read_superseded_applied_indexes(
        authority, current_srt_text=output, delivery_start_ms=0,
    ) == frozenset()
    assert chat_authority_owned_cue_indexes(output, authority, delivery_start_ms=0) == frozenset({3})


def test_native_terminal_validator_cannot_turn_chat_conflict_into_proposed_edit(monkeypatch):
    from src.autoslice import semantic_terminal_adjudication as terminal
    from src.autoslice.subtitle_acceptability_policy import load_policy

    authority = _chat_authority()
    projection = build_chat_authority_conflict_projection(
        SRT, authority, cue_index=1, delivery_start_ms=OFFSET,
    )
    check = build_context_adjudication_request(
        SRT, _finding(), source_media_timeline_offset_ms=OFFSET,
        prior_chat_authority=projection,
    )
    request = {
        "terminal_contract_version": 8,
        "terminal_policy": load_policy(),
        "terminal_glossary": [],
        "terminal_context": {},
        "check_request": check,
        "audio_gain_termination": {
            "projection": {"observations": []},
            "context_sources": {},
            "material_exhaustion": {"status": "EXHAUSTED"},
        },
    }
    monkeypatch.setattr(
        terminal,
        "terminal_materials",
        lambda _request: (
            {"keep_routes": terminal.KEEP_ROUTES,
             "current_support_observation_refs": []},
            {"prior-chat-authority": ["prior_chat_authority"]},
        ),
    )
    answer = {
        "choice": "PROPOSED",
        "grade": "B",
        "reason": "候选字形看似更顺",
        "evidence_decisive": True,
        "context_can_decide": True,
        "comparison_text": PROPOSED,
        "evidence_refs": ["prior-chat-authority"],
        "edit_start": 3,
        "edit_end": 4,
        "replacement": "死",
        "status": "RESOLVED",
        "mode": "faithful_paraphrase",
        "reliable_fragments": [],
        "meaning_basis": "未提供",
        "literal_uncertainty": "未提供",
        "no_new_facts": True,
    }
    with pytest.raises(ValueError, match="CHAT_AUTHORITY_CONFLICT_MUTATION_BLOCKED"):
        terminal.validate_terminal_answer(request, answer)
    with pytest.raises(ValueError, match="CHAT_AUTHORITY_CONFLICT_MUTATION_BLOCKED"):
        terminal.validate_terminal_answer(request, answer, _source_owner_reconciliation=True)
    from src.autoslice import chat_authority_reconciliation as reconciliation
    monkeypatch.setattr(reconciliation, 'validate_owner_request', lambda _request: None)
    assert terminal.validate_terminal_answer(request, answer,
        _source_owner_reconciliation=True) == 'PROPOSED'
