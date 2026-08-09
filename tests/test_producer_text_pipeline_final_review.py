import ast
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.boundary_semantic_review import (
    build_boundary_search_scope,
    cue_grid_sha256,
    semantic_review_sha256,
)
from src.autoslice.chat_authority import ReferentEntity, ReferentGroup
from src.autoslice.surface_canon import CHANNEL_PROFILE
from src.autoslice.final_review_contract import (
    FinalReviewContractError,
    validate_final_review_release,
)
from src.autoslice.final_source_language_owner import (
    register_final_source_language_cpa_repairs,
)
from src.autoslice.producer_boundary_owner_contract import (
    freeze_story_chat_boundary_owners,
)


def _srt(*texts: str) -> str:
    def timestamp(seconds: int) -> str:
        minutes, seconds = divmod(seconds, 60)
        return f"00:{minutes:02d}:{seconds:02d}"

    return "\n\n".join(
        f"{index}\n{timestamp(index * 5)},000 --> {timestamp(index * 5 + 4)},000\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


def test_fidelity_reverts_become_bounded_cpa_candidates(tmp_path):
    media = tmp_path / "clip.mp4"
    media.with_suffix(".fidelity-audit.json").write_text(
        json.dumps(
            {
                "schema_version": "subtitle-fidelity-audit.v2",
                "reverted": [
                    {
                        "cue_index": 1,
                        "draft": "名多的孩子",
                        "attempted": "鸣人的孩子",
                        "kept": "名多的孩子",
                        "violations": [
                            {
                                "op": "replace",
                                "draft_span": "名多",
                                "final_span": "鸣人",
                                "reason": "REPLACE_UNWITNESSED",
                            }
                        ],
                    },
                    {
                        "cue_index": 2,
                        "draft": "好爽哦",
                        "attempted": "好吃哦",
                        "kept": "好爽哦",
                        "violations": [
                            {
                                "op": "replace",
                                "draft_span": "爽",
                                "final_span": "吃",
                                "reason": "REPLACE_UNWITNESSED",
                            }
                        ],
                    },
                    {
                        "cue_index": 3,
                        "draft": "多处错误",
                        "attempted": "多个修复",
                        "kept": "多处错误",
                        "violations": [
                            {"op": "replace", "draft_span": "处", "final_span": "个"},
                            {"op": "replace", "draft_span": "错误", "final_span": "修复"},
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    candidates = pipeline._fidelity_review_candidates(
        media,
        _srt("名多的孩子", "好爽哦", "多处错误"),
    )

    assert [(row["cue"], row["suspect"], row["proposed_full_cue"]) for row in candidates] == [
        (2, "爽", "好吃哦"),
        (1, "名多", "鸣人的孩子"),
    ]
    assert all(row["candidate_origin"] == "fidelity_guard_reverted_candidate" for row in candidates)


def test_late_source_language_cpa_retires_same_window_cpa_surface():
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    current = "わたくし的话就是大小姐"
    proposed = "わたくしはいわゆるひとつの大小姐"
    input_srt = _srt("前文", current)
    output_srt = _srt("前文", proposed)
    chat_audit = {
        "entity_repairs": [
            {
                "mode": "final_review_context_adjudication",
                "decision_authority": "CPA_JUDGE",
                "matched_start_ms": 10_000,
                "matched_end_ms": 14_000,
                "before": ["我他喜欢的快就是大小姐"],
                "after": [current],
                "structured_exact_text": current,
                "survived": True,
                "boundary_required": False,
            }
        ]
    }
    source_audit = {
        "status": "CPA_ADJUDICATED_FOREIGN_SPEAKER_AUDIO",
        "decision_authority": "CPA_JUDGE",
        "applied_count": 1,
        "output_srt_sha256": hashlib.sha256(output_srt.encode()).hexdigest(),
        "cpa_adjudication_rows": [
            {
                "cue_index": 2,
                "resolved": True,
                "choice": "PROPOSED",
                "decision_authority": "CPA_JUDGE",
                "current": current,
                "proposed": proposed,
                "adjudication": {
                    "schema_version": "acoustic-witness-adjudication.v1",
                    "decision_authority": "CPA_JUDGE",
                    "witness_authority": "EVIDENCE_ONLY",
                    "judge": {
                        "schema_version": "acoustic-witness-adjudication.v1",
                        "status": "JUDGED",
                        "choice": "PROPOSED",
                        "prompt_sha256": "a" * 64,
                        "completion_sha256": "b" * 64,
                    },
                },
            }
        ],
    }

    register_final_source_language_cpa_repairs(
        chat_audit,
        input_srt=input_srt,
        output_srt=output_srt,
        source_language_audit=source_audit,
    )

    assert chat_audit["entity_repairs"][0]["reconciliation"]["status"] == (
        "SUPERSEDED_BY_FINAL_SOURCE_LANGUAGE_CPA"
    )
    owner = chat_audit["entity_repairs"][1]
    assert owner["structured_exact_text"] == proposed
    assert owner["superseded_entity_repair_indexes"] == [0]
    assert verify_chat_authority_final_surfaces(
        chat_audit,
        final_text_srt=output_srt,
        final_speaker_srt=output_srt,
        delivery_start_ms=0,
        delivery_end_ms=15_000,
    )


def _adapters() -> pipeline.TextPipelineAdapters:
    def unused(*args, **kwargs):
        return None

    return pipeline.TextPipelineAdapters(
        build_aggregate_transcriber=unused,
        build_agy_transcriber=unused,
        load_term_boundary_surfaces=unused,
        profile_asset_file=lambda _name: Path("/__vtuber_slice_missing_asset__"),
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=unused,
        topic_graph_expected_sha256=lambda: "",
    )


def _resolved_entity_verdict(request, canonical):
    return {
        "schema_version": "chat-entity-verdict.v1",
        "request_sha256": request["request_sha256"],
        "status": "RESOLVED",
        "canonical_entity": canonical,
        "authority_kind": "audio_forced_choice",
        "confidence": 0.97,
        "heard_syllables": canonical,
        "source_media_sha256": "a" * 64,
        "audio_clip_sha256": "b" * 64,
        "prompt_sha256": "c" * 64,
        "response_sha256": "d" * 64,
    }


def _boundary_pass() -> dict:
    cue_grid_sha256 = "sha256:" + "e" * 64
    return {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": "final_delivery",
        "reason_codes": [],
        "request_sha256": "sha256:" + "d" * 64,
        "cue_grid_sha256": cue_grid_sha256,
        "source_separation_witness": {
            "schema_version": (
                "talk-boundary-source-separation-witness.v1"
            ),
            "status": "PASS",
            "source_review_sha256": "sha256:" + "a" * 64,
            "source_request_sha256": "sha256:" + "b" * 64,
            "source_cue_grid_sha256": "sha256:" + "c" * 64,
            "source_final_start_ms": 0,
            "source_final_end_ms": 19_400,
            "reason_codes": [],
        },
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS",
            "recommended_end_cue_index": 3,
            "recommended_end_ms": 19_000,
            "final_closure_cue_index": 3,
            "final_snapped_end_ms": 19_000,
            "semantic_cue_grid_sha256": cue_grid_sha256,
            "final_cue_grid_sha256": cue_grid_sha256,
            "reason_codes": [],
        },
    }


def _correction_pass() -> dict:
    return {
        "schema_version": "final-review-audit.v1",
        "status": "CLEAN",
        "applied_count": 0,
        "findings": [],
        "boundary_semantic_review": _boundary_pass(),
    }


def test_frozen_boundary_owners_require_typed_gate_only_for_exact_reads():
    audit = {
        "applied": [
            {
                "matched_start_ms": 1_000,
                "matched_end_ms": 2_000,
                "owner_eligible": False,
            },
            {
                "matched_start_ms": 2_000,
                "matched_end_ms": 3_000,
                "owner_eligible": True,
            },
        ],
        "sender_repairs": [
            {
                "matched_start_ms": 3_000,
                "matched_end_ms": 4_000,
            }
        ],
    }

    contracts = freeze_story_chat_boundary_owners(
        audit,
        story_start_ms=0,
        story_end_ms=5_000,
    )

    assert [row["owner_kind"] for row in contracts] == [
        "exact_read",
        "sc_sender",
    ]
    assert audit["applied"][0]["boundary_required"] is False
    assert (
        audit["applied"][0]["boundary_owner_rejection"]
        == "EXACT_READ_SUPPORT_NOT_OWNER_ELIGIBLE"
    )
    assert audit["applied"][1]["boundary_required"] is True
    assert audit["sender_repairs"][0]["boundary_required"] is True


def test_boundary_semantic_review_is_downstream_of_final_text_authority():
    run_tree = ast.parse(inspect.getsource(pipeline.run_text_pipeline))
    finalize_calls = [
        node
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_finalize_text_evidence"
    ]
    review_calls = [
        node
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "review_final_boundary_semantics"
    ]
    review_assignments = [
        node
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Assign)
        and node.value in review_calls
    ]

    assert len(finalize_calls) == 1
    assert len(review_calls) == 1
    assert finalize_calls[0].lineno < review_calls[0].lineno
    cues_keyword = next(
        keyword
        for keyword in review_calls[0].keywords
        if keyword.arg == "cues"
    )
    assert ast.dump(cues_keyword.value) == ast.dump(
        ast.Attribute(
            value=ast.Name(id="evidence", ctx=ast.Load()),
            attr="cues",
            ctx=ast.Load(),
        )
    )
    assert len(review_assignments) == 1
    target = review_assignments[0].targets[0]
    assert isinstance(target, ast.Subscript)
    assert isinstance(target.value, ast.Name)
    assert target.value.id == "final_review_audit"
    assert isinstance(target.slice, ast.Constant)
    assert target.slice.value == "boundary_semantic_review"

    correction_tree = ast.parse(
        inspect.getsource(pipeline._run_final_review)
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        in {
            "review_final_boundary_semantics",
            "review_talk_boundary_semantics",
        }
        for node in ast.walk(correction_tree)
    )


def test_frozen_boundary_loader_receives_fresh_owner_contract():
    """Keep the producer handoff aligned with the hash-bound replay loader."""

    run_tree = ast.parse(inspect.getsource(pipeline.run_text_pipeline))
    freeze_calls = [
        node
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "freeze_required_boundary_owner_contract"
    ]
    loader_calls = [
        node
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_boundary_review_authorities"
    ]

    assert len(freeze_calls) == 1
    assert len(loader_calls) == 1
    assert freeze_calls[0].lineno < loader_calls[0].lineno
    owner_keyword = next(
        keyword
        for keyword in loader_calls[0].keywords
        if keyword.arg == "current_owner_contract"
    )
    assert ast.unparse(owner_keyword.value) == (
        "authority.chat_authority_audit.get('frozen_boundary_owner_contract')"
    )


def test_source_only_boundary_authority_cannot_reach_final_delivery_gate():
    """Source-only carry feeds the first gate while final gets only full authority."""

    run_tree = ast.parse(inspect.getsource(pipeline.run_text_pipeline))
    authority_call = next(
        node
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_boundary_review_authorities"
    )
    assignment = next(
        node
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Assign) and node.value is authority_call
    )
    assert ast.unparse(assignment.targets[0]) == (
        "(frozen_boundary_receipt, frozen_source_review)"
    )

    source_call = next(
        node
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "review_final_boundary_semantics"
    )
    source_keyword = next(
        keyword for keyword in source_call.keywords if keyword.arg == "frozen_review"
    )
    assert ast.unparse(source_keyword.value) == "frozen_source_review"

    final_call = next(
        node
        for node in ast.walk(run_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "exact_delivery_correction_audit"
    )
    final_keyword = next(
        keyword
        for keyword in final_call.keywords
        if keyword.arg == "frozen_boundary_receipt"
    )
    assert ast.unparse(final_keyword.value) == "frozen_boundary_receipt"


def test_final_boundary_review_indexes_exact_post_authority_grid():
    seen_prompt = ""

    def review(prompt):
        nonlocal seen_prompt
        seen_prompt = prompt
        return json.dumps(
            {
                "syntax_complete": True,
                "story_closed": True,
                "next_topic_separated": True,
                "recommended_end_cue_index": 2,
                "evidence_cue_indexes": [1, 2, 3],
                "reason_codes": [],
                "summary": "第二句闭环，第三句换题。",
            },
            ensure_ascii=False,
        )

    final_srt = _srt("保留的前句", "最终闭合句", "下一话题")
    result = pipeline.review_final_boundary_semantics(
        cues=pipeline.parse_srt_cues(final_srt),
        boundary_target_ms=14_000,
        candidate_id="final-grid",
        selection_hook="完整包袱",
        selection_scorecard={
            "status": "VALID",
            "dimensions": {
                "self_contained": 4,
                "comedic_payoff": 4,
            },
        },
        structured_context="",
        candidate_context="",
        boundary_max_forward_ms=30_000,
        llm_call=review,
        extract_json=pipeline.extract_json_object,
    )

    assert result["status"] == "PASS"
    assert result["recommended_end_cue_index"] == 2
    assert result["recommended_end_ms"] == 14_000
    assert "最终闭合句" in seen_prompt


def test_source_boundary_review_blocks_before_llm_when_witness_reserve_incomplete():
    calls = 0

    def review(_prompt):
        nonlocal calls
        calls += 1
        raise AssertionError("incomplete source context must block before LLM")

    scope = build_boundary_search_scope(
        semantic_target_ms=116_840,
        manual_lower_bound_ms=116_840,
        required_owner_end_ms=116_830,
        repair_cap_ms=30_000,
        last_piece_start_ms=1_563_150,
    )
    result = pipeline.review_final_boundary_semantics(
        cues=pipeline.parse_srt_cues(_srt("目标", "闭合句")),
        boundary_target_ms=116_840,
        candidate_id="auto_193450_1573_1672",
        selection_hook="椅子反差",
        selection_scorecard={
            "status": "VALID",
            "dimensions": {
                "self_contained": 4,
                "comedic_payoff": 4,
            },
        },
        structured_context="",
        candidate_context="",
        boundary_max_forward_ms=30_000,
        llm_call=review,
        extract_json=pipeline.extract_json_object,
        boundary_search_scope=scope,
        available_local_source_context_end_ms=141_820,
    )

    assert calls == 0
    assert result["status"] == "BLOCK"
    assert result["needs_more_context"] is True
    assert result["retry_scope"] == "source_witness_reserve"
    assert result["reason_codes"] == [
        "BOUNDARY_SOURCE_WITNESS_RESERVE_INCOMPLETE"
    ]
    assert result["source_context_coverage"] == {
        "schema_version": "talk-boundary-source-context-coverage.v1",
        "status": "BLOCK",
        "scope_sha256": scope["scope_sha256"],
        "available_local_source_context_end_ms": 141_820,
        "required_local_source_context_end_ms": 161_840,
        "deficit_ms": 20_020,
    }


def test_source_boundary_review_accepts_exact_required_window():
    scope = build_boundary_search_scope(
        semantic_target_ms=14_000,
        repair_cap_ms=30_000,
    )
    response = json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "recommended_end_cue_index": 2,
            "evidence_cue_indexes": [1, 2, 3],
            "reason_codes": [],
            "summary": "第二句闭环，第三句换题。",
        },
        ensure_ascii=False,
    )
    result = pipeline.review_final_boundary_semantics(
        cues=pipeline.parse_srt_cues(
            _srt("保留的前句", "最终闭合句", "下一话题")
        ),
        boundary_target_ms=14_000,
        candidate_id="exact-source-context",
        selection_hook="完整包袱",
        selection_scorecard={
            "status": "VALID",
            "dimensions": {
                "self_contained": 4,
                "comedic_payoff": 4,
            },
        },
        structured_context="",
        candidate_context="",
        boundary_max_forward_ms=30_000,
        llm_call=lambda _prompt: response,
        extract_json=pipeline.extract_json_object,
        boundary_search_scope=scope,
        available_local_source_context_end_ms=scope[
            "required_local_source_context_end_ms"
        ],
    )

    assert result["status"] == "PASS"
    assert result["recommended_end_ms"] == 14_000


def test_exact_delivery_boundary_review_rebinds_post_baseline_grid():
    source_review = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": "source_full_window",
        "request_sha256": "sha256:" + "a" * 64,
        "cue_grid_sha256": "sha256:" + "b" * 64,
        "next_topic_separated": True,
        "next_topic_witness_valid": True,
        "recommended_end_ms": 29_000,
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS",
            "final_start_ms": 10_000,
            "final_end_ms": 30_000,
        },
    }
    correction = _correction_pass()
    correction["boundary_semantic_review"] = source_review
    final_srt = _srt("baseline 改写后的第一句", "最终闭合句")
    seen_prompt = ""

    def review(prompt):
        nonlocal seen_prompt
        seen_prompt = prompt
        return json.dumps(
            {
                "syntax_complete": True,
                "story_closed": True,
                "next_topic_separated": True,
                "recommended_end_cue_index": 2,
                "evidence_cue_indexes": [1, 2],
                "reason_codes": [],
                "summary": "最终交付的第二句闭环。",
            },
            ensure_ascii=False,
        )

    rebound = pipeline.exact_delivery_correction_audit(
        final_srt_text=final_srt,
        correction_audit=correction,
        source_final_start_ms=10_000,
        source_final_end_ms=30_000,
        candidate_id="672",
        selection_hook="当面对质",
        selection_scorecard={
            "status": "VALID",
            "dimensions": {
                "self_contained": 4,
                "comedic_payoff": 4,
            },
        },
        structured_context="",
        candidate_context="",
        boundary_max_forward_ms=30_000,
        llm_call=review,
        extract_json=pipeline.extract_json_object,
    )

    final_review = rebound["boundary_semantic_review"]
    final_cues = pipeline.parse_srt_cues(final_srt)
    assert final_review["status"] == "PASS"
    assert final_review["review_scope"] == "final_delivery"
    assert final_review["cue_grid_sha256"] == cue_grid_sha256(final_cues)
    assert final_review["cue_grid_sha256"] != source_review[
        "cue_grid_sha256"
    ]
    assert final_review["source_separation_witness"][
        "source_review_sha256"
    ] == semantic_review_sha256(source_review)
    assert final_review["final_endpoint_binding"]["status"] == "PASS"
    assert "baseline 改写后的第一句" in seen_prompt
    assert "后来删除的幻听" not in seen_prompt


def test_exact_delivery_projects_same_closure_source_pass_over_correlated_block():
    final_srt = _srt("前文", "大小姐用的")
    closure = pipeline.parse_srt_cues(final_srt)[-1]
    closure_sha = "sha256:" + hashlib.sha256(closure.text.encode()).hexdigest()
    source_review = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": "source_full_window",
        "request_sha256": "sha256:" + "a" * 64,
        "cue_grid_sha256": "sha256:" + "b" * 64,
        "recommended_end_cue_index": 9,
        "recommended_end_ms": 24_000,
        "syntax_complete": True,
        "story_closed": True,
        "next_topic_separated": True,
        "next_topic_witness_valid": True,
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS",
            "closure_text_sha256": closure_sha,
            "final_start_ms": 10_000,
            "final_end_ms": 24_400,
        },
    }
    correction = _correction_pass()
    correction["boundary_semantic_review"] = source_review

    def correlated_block(_prompt):
        return json.dumps(
            {
                "syntax_complete": False,
                "story_closed": False,
                "next_topic_separated": False,
                "recommended_end_cue_index": 2,
                "evidence_cue_indexes": [2],
                "reason_codes": ["SYNTAX_INCOMPLETE", "STORY_NOT_CLOSED"],
                "summary": "末句看起来像残句。",
            },
            ensure_ascii=False,
        )

    rebound = pipeline.exact_delivery_correction_audit(
        final_srt_text=final_srt,
        correction_audit=correction,
        source_final_start_ms=10_000,
        source_final_end_ms=24_400,
        candidate_id="terminal-projection",
        selection_hook="大小姐用的",
        selection_scorecard={
            "status": "VALID",
            "dimensions": {"self_contained": 4, "comedic_payoff": 4},
        },
        structured_context="",
        candidate_context="",
        boundary_max_forward_ms=30_000,
        llm_call=correlated_block,
        extract_json=pipeline.extract_json_object,
    )

    review = rebound["boundary_semantic_review"]
    assert review["status"] == "PASS"
    assert review["final_endpoint_binding"]["status"] == "PASS"
    projection = review["correlated_source_projection"]
    assert projection["status"] == "PASS"
    assert projection["closure_text_sha256"] == closure_sha


def test_exact_delivery_does_not_project_source_pass_after_closure_text_drift():
    source_text = "大小姐用的"
    final_srt = _srt("前文", "大小姐说的")
    source_review = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": "source_full_window",
        "request_sha256": "sha256:" + "a" * 64,
        "cue_grid_sha256": "sha256:" + "b" * 64,
        "recommended_end_cue_index": 9,
        "recommended_end_ms": 24_000,
        "syntax_complete": True,
        "story_closed": True,
        "next_topic_separated": True,
        "next_topic_witness_valid": True,
        "final_endpoint_binding": {
            "schema_version": "talk-boundary-final-endpoint-binding.v1",
            "status": "PASS",
            "closure_text_sha256": "sha256:"
            + hashlib.sha256(source_text.encode()).hexdigest(),
            "final_start_ms": 10_000,
            "final_end_ms": 24_400,
        },
    }
    correction = _correction_pass()
    correction["boundary_semantic_review"] = source_review

    rebound = pipeline.exact_delivery_correction_audit(
        final_srt_text=final_srt,
        correction_audit=correction,
        source_final_start_ms=10_000,
        source_final_end_ms=24_400,
        candidate_id="terminal-drift",
        selection_hook="大小姐说的",
        selection_scorecard={
            "status": "VALID",
            "dimensions": {"self_contained": 4, "comedic_payoff": 4},
        },
        structured_context="",
        candidate_context="",
        boundary_max_forward_ms=30_000,
        llm_call=lambda _prompt: json.dumps(
            {
                "syntax_complete": False,
                "story_closed": False,
                "next_topic_separated": False,
                "recommended_end_cue_index": 2,
                "evidence_cue_indexes": [2],
                "reason_codes": ["SYNTAX_INCOMPLETE"],
                "summary": "漂移后的末句不完整。",
            },
            ensure_ascii=False,
        ),
        extract_json=pipeline.extract_json_object,
    )

    review = rebound["boundary_semantic_review"]
    assert review["status"] == "BLOCK"
    assert "correlated_source_projection" not in review


def test_exact_final_release_review_binds_explicit_clean_response(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: '{"findings":[]}'),
    )
    srt = _srt("第一句", "第二句", "第三句")

    receipt = pipeline._run_exact_final_release_review(
        srt_text=srt,
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="完整回指",
        clip_context={},
    )

    assert receipt["status"] == "CLEAN"
    assert receipt["release_gate"] == "PASS"
    validate_final_review_release(
        receipt,
        expected_srt_sha256=receipt["reviewed_srt_sha256"],
    )


def test_exact_release_blocks_legacy_ungrounded_correction_even_if_rescan_empty(
    monkeypatch,
):
    """An empty second reviewer pass cannot launder an earlier spelling edit."""

    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: '{"findings":[]}'),
    )
    correction = _correction_pass()
    correction["status"] = "APPLIED"
    correction["applied_count"] = 1
    correction["findings"] = [
        {
            "cue_index": 1,
            "kind": "context",
            "suspect": "毁神",
            "suggestion": "绘声",
            "routed": "homophone_fix",
        }
    ]

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("绘声来了", "第二句", "第三句"),
        correction_audit=correction,
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
    )

    assert receipt["status"] == "FLAGGED"
    assert receipt["findings"] == []
    assert receipt["correction_mutation_authority"]["status"] == "BLOCK"
    assert "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID" in (
        receipt["reason_codes"]
    )
    with pytest.raises(
        FinalReviewContractError,
        match="FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID",
    ):
        validate_final_review_release(receipt)


def test_unavailable_correction_discovery_cannot_be_laundered_by_empty_rescan(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: '{"findings":[]}'),
    )
    correction = _correction_pass()
    correction.update(
        {
            "status": "AUDITOR_UNAVAILABLE",
            "release_gate": "BLOCK",
            "reason_codes": [
                "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
            ],
            "discovery": {
                "status": "AUDITOR_UNAVAILABLE",
                "detail": "provider unavailable",
            },
            "findings": [],
            "applied_count": 0,
            "error_type": "FinalReviewAuditError",
        }
    )

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("第一句", "第二句", "第三句"),
        correction_audit=correction,
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
    )

    mutation_audit = receipt["correction_mutation_authority"]
    assert receipt["status"] == "FLAGGED"
    assert receipt["release_gate"] == "BLOCK"
    assert mutation_audit["status"] == "BLOCK"
    assert mutation_audit["failures"] == [
        {
            "reason_code": "CORRECTION_DISCOVERY_INCOMPLETE",
            "upstream_reason_codes": [
                "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
            ],
        }
    ]
    assert "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID" in (
        receipt["reason_codes"]
    )


def test_correction_mutation_audit_blocks_explicit_empty_unavailable_state():
    mutation_audit = pipeline.audit_correction_mutation_authority(
        {
            "schema_version": "final-review-audit.v1",
            "status": "AUDITOR_UNAVAILABLE",
            "reason_codes": ["FINAL_REVIEW_RESPONSE_FINDINGS_MISSING"],
            "findings": [],
            "applied_count": 0,
        }
    )

    assert mutation_audit["status"] == "BLOCK"
    assert mutation_audit["validated_mutation_count"] == 0
    assert mutation_audit["failures"][0]["reason_code"] == (
        "CORRECTION_DISCOVERY_INCOMPLETE"
    )


def test_correction_mutation_audit_requires_completed_status():
    mutation_audit = pipeline.audit_correction_mutation_authority(
        {
            "schema_version": "final-review-audit.v1",
            "status": "SKIPPED",
            "findings": [],
            "applied_count": 0,
        }
    )

    assert mutation_audit["status"] == "BLOCK"
    assert mutation_audit["failures"] == [
        {
            "reason_code": "CORRECTION_STATUS_INVALID",
            "observed_status": "SKIPPED",
        }
    ]


def test_release_contract_requires_final_boundary_endpoint_binding(monkeypatch):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: '{"findings":[]}'),
    )
    correction = _correction_pass()
    del correction["boundary_semantic_review"]["final_endpoint_binding"]
    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("第一句", "第二句", "第三句"),
        correction_audit=correction,
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
    )

    with pytest.raises(
        FinalReviewContractError,
        match="FINAL_REVIEW_BOUNDARY_ENDPOINT_BINDING_INVALID",
    ):
        validate_final_review_release(receipt)


def test_release_contract_recomputes_final_boundary_endpoint_equality():
    receipt = {
        "schema_version": "final-review-audit.v2",
        "status": "CLEAN",
        "release_gate": "PASS",
        "reviewed_srt_sha256": "sha256:" + "a" * 64,
        "discovery": {"status": "COMPLETE"},
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "PASS",
        },
        "findings": [],
        "validated_finding_count": 0,
        "boundary_semantic_review": _boundary_pass(),
    }
    binding = receipt["boundary_semantic_review"]["final_endpoint_binding"]
    binding["final_closure_cue_index"] = 4
    binding["final_snapped_end_ms"] = 21_000

    with pytest.raises(
        FinalReviewContractError,
        match="FINAL_REVIEW_BOUNDARY_ENDPOINT_BINDING_MISMATCH",
    ):
        validate_final_review_release(receipt)


def test_release_contract_rejects_stale_boundary_cue_grid():
    receipt = {
        "schema_version": "final-review-audit.v2",
        "status": "CLEAN",
        "release_gate": "PASS",
        "reviewed_srt_sha256": "sha256:" + "a" * 64,
        "discovery": {"status": "COMPLETE"},
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "PASS",
        },
        "findings": [],
        "validated_finding_count": 0,
        "boundary_semantic_review": _boundary_pass(),
    }
    receipt["boundary_semantic_review"]["final_endpoint_binding"][
        "final_cue_grid_sha256"
    ] = "sha256:" + "f" * 64

    with pytest.raises(
        FinalReviewContractError,
        match="FINAL_REVIEW_BOUNDARY_CUE_GRID_BINDING_MISMATCH",
    ):
        validate_final_review_release(receipt)


def test_exact_final_release_review_provider_failure_is_a_block(monkeypatch):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    def broken(_prompt):
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        pipeline, "_build_final_review_llm_call", lambda: broken
    )
    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("第一句", "第二句", "第三句"),
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
    )

    assert receipt["status"] == "AUDITOR_UNAVAILABLE"
    assert receipt["release_gate"] == "BLOCK"
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(receipt)


def test_exact_final_release_review_unresolved_finding_is_a_block(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (
            lambda _prompt: json.dumps(
                {
                    "findings": [
                        {
                            "cue": 1,
                            "kind": "context",
                            "suspect": "第一句",
                            "repair_class": "disclosure_only",
                            "why": "still suspicious",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        ),
    )
    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("第一句", "第二句", "第三句"),
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
    )

    assert receipt["status"] == "FLAGGED"
    assert receipt["reason_codes"] == ["FINAL_REVIEW_UNRESOLVED_FINDINGS"]
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(receipt)



def _witness_verdict(request, heard, *, audible=True):
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "request_sha256": request["request_sha256"],
        "status": "OBSERVED",
        "target_audible": audible,
        "heard_pinyin": heard,
        "uncertain_positions": [],
        "syllable_count": len(heard.split()),
        "confidence": 0.9,
        "reason": "test witness",
    }


def _judge_json(choice):
    return json.dumps({"choice": choice, "reason": "test judge"})


def _split_llm(findings_json, judge_choice):
    """Findings discovery and the word-choice judge share one llm seam."""
    def call(prompt):
        if "字幕选字裁决" in prompt:
            return _judge_json(judge_choice)
        return findings_json
    return call

def test_exact_final_release_review_closes_acoustically_disproven_proposal(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: _judge_json("CURRENT")),
    )
    finding = {
        "cue_index": 1,
        "kind": "entity",
        "suspect": "和",
        "suggestion": "洛",
        "proposed_full_cue": "这个是洛天依的联动哦",
        "repair_class": "source_backed_entity",
        "base_text_sha256": hashlib.sha256(
            "这个是和天依的联动哦".encode("utf-8")
        ).hexdigest(),
        "why": "下一句出现洛天依",
    }
    monkeypatch.setattr(
        pipeline, "audit_final_subtitles", lambda *_args, **_kwargs: [finding]
    )

    def keep_current(request):
        return _witness_verdict(request, "zhe ge shi he tian yi de lian dong o")

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("这个是和天依的联动哦", "洛天依，对哦", "第三句"),
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
        verify_confusable_entity=keep_current,
    )

    assert receipt["status"] == "CLEAN"
    assert receipt["findings"] == []
    assert receipt["validated_finding_count"] == 0
    assert receipt["resolved_findings"][0]["resolution"] == (
        "ACOUSTICALLY_DISPROVEN_FINAL_REVIEW_PROPOSAL"
    )
    validate_final_review_release(receipt)


def test_exact_final_memo_replay_runs_after_fresh_acoustic_witness(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: _judge_json("PROPOSED")),
    )
    current = "这个是和天依的联动哦"
    proposed = "这个是洛天依的联动哦"
    finding = {
        "cue_index": 1,
        "kind": "entity",
        "suspect": "和",
        "suggestion": "洛",
        "proposed_full_cue": proposed,
        "repair_class": "source_backed_entity",
        "base_text_sha256": hashlib.sha256(
            current.encode("utf-8")
        ).hexdigest(),
        "why": "需要刷新声学内容后才能决定 memo 是否仍有效",
    }
    monkeypatch.setattr(
        pipeline,
        "audit_final_subtitles",
        lambda *_args, **_kwargs: [finding],
    )
    verifier_calls: list[dict] = []

    def observe(request):
        verifier_calls.append(request)
        return _witness_verdict(
            request,
            "zhe ge shi luo tian yi de lian dong o",
        )

    def assert_fresh_witness_then_replay(
        _srt_text,
        rows,
        *,
        authority_audit,
    ):
        del authority_audit
        materialized = list(rows)
        assert len(verifier_calls) == 1
        assert materialized[0]["exact_release_adjudication"]["verdict"][
            "heard_pinyin"
        ] == "zhe ge shi luo tian yi de lian dong o"
        return materialized, []

    monkeypatch.setattr(
        pipeline,
        "resolve_findings_from_exact_final_convergence_memos",
        assert_fresh_witness_then_replay,
    )

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt(current, "洛天依，对哦", "第三句"),
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
        verify_confusable_entity=observe,
        verified_authority_audit={
            "exact_final_cpa_convergence_memos": []
        },
    )

    assert len(verifier_calls) == 1
    assert receipt["status"] == "FLAGGED"


@pytest.mark.parametrize(
    (
        "current",
        "suspect",
        "suggestion",
        "proposed",
        "repair_class",
        "orthography_blocked",
    ),
    [
        ("毁神来了", "毁神", "绘声", "绘声来了", "phonetic", False),
        (
            "大恩来了",
            "大恩",
            "大N",
            "大N来了",
            "source_backed_entity",
            True,
        ),
    ],
)
def test_exact_final_release_review_requires_cpa_to_choose_text(
    monkeypatch,
    current,
    suspect,
    suggestion,
    proposed,
    repair_class,
    orthography_blocked,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline, "_build_final_review_llm_call", lambda: (lambda _prompt: "{}")
    )
    finding = {
        "cue_index": 1,
        "kind": "entity",
        "suspect": suspect,
        "suggestion": suggestion,
        "proposed_full_cue": proposed,
        "repair_class": repair_class,
        "base_text_sha256": hashlib.sha256(
            current.encode("utf-8")
        ).hexdigest(),
        "why": "同音或字母正字法争议",
    }
    monkeypatch.setattr(
        pipeline, "audit_final_subtitles", lambda *_args, **_kwargs: [finding]
    )

    def acoustic_veto(request):
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "SUPPORTED",
            "proposed_fit": "INCOMPATIBLE",
        }

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt(current, "第二句", "第三句"),
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
        verify_confusable_entity=acoustic_veto,
    )

    assert receipt["status"] == "FLAGGED"
    finding_receipt = receipt["findings"][0]
    assert finding_receipt["exact_release_adjudication"][
        "decision_authority"
    ] == "CPA_JUDGE"
    if orthography_blocked:
        assert finding_receipt[
            "exact_release_acoustic_closure_blocked_reason"
        ] == "ORTHOGRAPHY_NOT_DECIDABLE_FROM_AUDIO"
    else:
        assert (
            "exact_release_acoustic_closure_blocked_reason"
            not in finding_receipt
        )
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(receipt)


def test_exact_final_release_review_does_not_apply_new_mutation(monkeypatch):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: _judge_json("PROPOSED")),
    )
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "坏词",
        "suggestion": "好词",
        "proposed_full_cue": "好词留在这里",
        "repair_class": "phonetic",
        "base_text_sha256": hashlib.sha256(
            "坏词留在这里".encode("utf-8")
        ).hexdigest(),
        "why": "仍需修改",
    }
    monkeypatch.setattr(
        pipeline, "audit_final_subtitles", lambda *_args, **_kwargs: [finding]
    )

    def prefer_proposed(request):
        return _witness_verdict(request, "hao ci liu zai zhe li")

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("坏词留在这里", "第二句", "第三句"),
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
        verify_confusable_entity=prefer_proposed,
    )

    assert receipt["status"] == "FLAGGED"
    assert receipt["findings"][0]["exact_release_adjudication"][
        "repaired"
    ] is True
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(receipt)


def test_exact_final_release_review_discloses_cpa_keep_current_real_shape(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: _judge_json("CURRENT")),
    )
    current = f"我一会儿让我们先看了这个{CHANNEL_PROFILE.display_name}的队伍"
    proposed = f"我一会儿让我们先看这个{CHANNEL_PROFILE.display_name}的队伍"
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "先看了",
        "suggestion": "先看",
        "proposed_full_cue": proposed,
        "repair_class": "spoken_unit",
        "base_text_sha256": hashlib.sha256(
            current.encode("utf-8")
        ).hexdigest(),
        "why": "语法审查提议删除完成体，但 CPA 对音频作最终裁决",
    }
    monkeypatch.setattr(
        pipeline, "audit_final_subtitles", lambda *_args, **_kwargs: [finding]
    )

    def keep_current(request):
        return _witness_verdict(
            request,
            "en a wo men gang cai kan le zhe ge luo tian yi de dui wu",
        )

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt(current, "第二句", "第三句"),
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
        verify_confusable_entity=keep_current,
    )

    assert receipt["status"] == "CLEAN"
    assert receipt["findings"] == []
    assert len(receipt["unresolved_findings_disclosed"]) == 1
    adjudication = receipt["unresolved_findings_disclosed"][0][
        "exact_release_adjudication"
    ]
    assert adjudication["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    validate_final_review_release(receipt)


def test_exact_final_release_review_does_not_relitigate_verified_human_truth(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline, "_build_final_review_llm_call", lambda: (lambda _prompt: "{}")
    )
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "李",
        "suggestion": "礼",
        "proposed_full_cue": "礼太多了哈",
        "repair_class": "phonetic",
        "base_text_sha256": hashlib.sha256(
            "李太多了哈".encode("utf-8")
        ).hexdigest(),
        "why": "模型想按邻句改字",
    }
    monkeypatch.setattr(
        pipeline, "audit_final_subtitles", lambda *_args, **_kwargs: [finding]
    )

    def verifier_must_not_run(_request):
        raise AssertionError("verified human truth must win before acoustic review")

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("李太多了哈", "第二句", "第三句"),
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
        verify_confusable_entity=verifier_must_not_run,
        verified_authority_audit={
            "source_subtitle_truth_audit": {
                "status": "APPLIED",
                "failures": [],
                "applied": [
                    {
                        "truth_id": "too-many-li",
                        "action": "replace_cue",
                        "local_windows": [
                            {"start_ms": 5_000, "end_ms": 9_000}
                        ],
                        "declared_output_contract": {
                            "action": "replace_cue",
                            "canonical_texts": ["李太多了哈"],
                            "required_text": "",
                        },
                    }
                ],
                "satisfied": [],
            }
        },
        timeline_offset_ms=0,
    )

    assert receipt["status"] == "CLEAN"
    assert receipt["findings"] == []
    resolution = receipt["resolved_findings"][0]
    assert resolution["resolution"] == (
        "VERIFIED_SOURCE_TRUTH_SUPERSEDES_REVIEW_PROPOSAL"
    )
    assert resolution["source_truth_resolution"]["truth_ids"] == [
        "too-many-li"
    ]
    validate_final_review_release(receipt)


def test_final_review_truth_contract_excludes_touching_neighbour_cues() -> None:
    from src.autoslice.final_review_auditor import (
        resolve_verified_source_truth_findings,
    )

    srt_text = (
        "1\n00:00:00,000 --> 00:00:01,000\n前句\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n姐感妹\n\n"
        "3\n00:00:02,000 --> 00:00:03,000\n秦秦\n"
    )
    finding = {
        "cue_index": 2,
        "kind": "context",
        "suspect": "姐感妹",
        "suggestion": "别的词",
        "proposed_full_cue": "别的词",
        "repair_class": "phonetic",
        "base_text_sha256": hashlib.sha256(
            "姐感妹".encode("utf-8")
        ).hexdigest(),
    }
    audit = {
        "status": "APPLIED",
        "failures": [],
        "applied": [
            {
                "truth_id": "jiegammei",
                "action": "replace_cue",
                "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
                "declared_output_contract": {
                    "action": "replace_cue",
                    "canonical_texts": ["姐感妹"],
                    "required_text": "",
                },
            }
        ],
        "satisfied": [],
    }

    pending, resolved = resolve_verified_source_truth_findings(
        srt_text,
        [finding],
        source_truth_audit=audit,
        timeline_offset_ms=0,
    )

    assert pending == []
    assert resolved[0]["resolution"] == (
        "VERIFIED_SOURCE_TRUTH_SUPERSEDES_REVIEW_PROPOSAL"
    )


def test_final_review_does_not_protect_finding_on_twenty_ms_owner_sliver() -> None:
    from src.autoslice.final_review_auditor import (
        resolve_verified_source_truth_findings,
    )

    srt_text = (
        "1\n00:00:00,000 --> 00:00:01,020\n前句\n\n"
        "2\n00:00:01,020 --> 00:00:02,000\n真值\n"
    )
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "前句",
        "suggestion": "",
        "base_text_sha256": hashlib.sha256(
            "前句".encode("utf-8")
        ).hexdigest(),
    }
    audit = {
        "status": "APPLIED",
        "failures": [],
        "applied": [
            {
                "truth_id": "twenty-ms-sliver",
                "action": "replace_cue",
                "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
                "declared_output_contract": {
                    "action": "replace_cue",
                    "canonical_texts": ["真值"],
                    "required_text": "",
                },
            }
        ],
        "satisfied": [],
    }

    pending, resolved = resolve_verified_source_truth_findings(
        srt_text,
        [finding],
        source_truth_audit=audit,
        timeline_offset_ms=0,
    )

    assert pending == [finding]
    assert resolved == []


def test_final_review_uses_projection_not_raw_window_grazing_neighbour() -> None:
    from src.autoslice.final_review_auditor import (
        resolve_verified_source_truth_findings,
    )

    srt_text = (
        "1\n00:00:00,000 --> 00:00:01,000\n真值\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n邻句\n"
    )
    finding = {
        "cue_index": 2,
        "kind": "context",
        "suspect": "邻句",
        "suggestion": "",
        "base_text_sha256": hashlib.sha256(
            "邻句".encode("utf-8")
        ).hexdigest(),
    }
    audit = {
        "status": "APPLIED",
        "failures": [],
        "applied": [
            {
                "truth_id": "shrunk-owner",
                "action": "replace_cue",
                "cue_indexes": [1],
                "local_windows": [{"start_ms": 0, "end_ms": 1_150}],
                "declared_output_contract": {
                    "action": "replace_cue",
                    "canonical_texts": ["真值"],
                    "required_text": "",
                },
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
                            "cue_index": 1,
                            "start_ms": 0,
                            "end_ms": 1_000,
                            "before_text": "误听",
                            "after_text": "真值",
                        }
                    ],
                },
            }
        ],
        "satisfied": [],
    }

    pending, resolved = resolve_verified_source_truth_findings(
        srt_text,
        [finding],
        source_truth_audit=audit,
        timeline_offset_ms=0,
    )

    assert pending == [finding]
    assert resolved == []


def test_optional_source_truth_never_closes_exact_final_finding() -> None:
    from src.autoslice.final_review_auditor import (
        resolve_verified_source_truth_findings,
    )

    srt_text = "1\n00:00:00,000 --> 00:00:01,000\n可选文本\n"
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "可选文本",
        "suggestion": "另一文本",
        "proposed_full_cue": "另一文本",
        "base_text_sha256": hashlib.sha256(
            "可选文本".encode("utf-8")
        ).hexdigest(),
    }
    audit = {
        "status": "APPLIED",
        "failures": [],
        "applied": [
            {
                "truth_id": "best-effort-only",
                "required": False,
                "action": "replace_cue",
                "cue_indexes": [1],
                "local_windows": [{"start_ms": 0, "end_ms": 1_000}],
                "declared_output_contract": {
                    "action": "replace_cue",
                    "canonical_texts": ["可选文本"],
                    "required_text": "",
                },
            }
        ],
        "satisfied": [],
    }

    pending, resolved = resolve_verified_source_truth_findings(
        srt_text,
        [finding],
        source_truth_audit=audit,
        timeline_offset_ms=0,
    )

    assert pending == [finding]
    assert resolved == []


def test_exact_final_release_review_keeps_unrelated_substring_concern(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline, "_build_final_review_llm_call", lambda: (lambda _prompt: "{}")
    )
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "后来",
        "suggestion": "然后",
        "proposed_full_cue": "毁神然后走了",
        "repair_class": "phonetic",
        "base_text_sha256": hashlib.sha256(
            "毁神后来走了".encode("utf-8")
        ).hexdigest(),
        "why": "人名之外仍有可疑词",
    }
    monkeypatch.setattr(
        pipeline, "audit_final_subtitles", lambda *_args, **_kwargs: [finding]
    )
    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("毁神后来走了", "第二句", "第三句"),
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
        verify_confusable_entity=None,
        verified_authority_audit={
            "source_subtitle_truth_audit": {
                "status": "APPLIED",
                "failures": [],
                "applied": [
                    {
                        "truth_id": "huishen-name",
                        "action": "replace_substring",
                        "local_windows": [
                            {"start_ms": 5_000, "end_ms": 9_000}
                        ],
                        "declared_output_contract": {
                            "action": "replace_substring",
                            "canonical_texts": [],
                            "required_text": "毁神",
                        },
                    }
                ],
                "satisfied": [],
            }
        },
        timeline_offset_ms=0,
    )

    assert receipt["status"] == "FLAGGED"
    assert receipt["findings"][0]["suspect"] == "后来"


def test_exact_final_release_review_forwards_recut_offset_to_audio_adjudication(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline, "_build_final_review_llm_call", lambda: (lambda _prompt: "{}")
    )
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "原文",
        "suggestion": "建议",
        "base_text_sha256": hashlib.sha256("原文".encode("utf-8")).hexdigest(),
    }
    monkeypatch.setattr(
        pipeline, "audit_final_subtitles", lambda *_args, **_kwargs: [finding]
    )
    captured: dict[str, object] = {}

    def fake_adjudicate(
        _srt_text,
        findings,
        *,
        entity_verifier,
        clip_context,
        source_media_timeline_offset_ms,
        judge_llm_call=None,
        screen_read_probe=None,
    ):
        captured["findings"] = list(findings)
        captured["entity_verifier"] = entity_verifier
        captured["clip_context"] = clip_context
        captured["source_media_timeline_offset_ms"] = (
            source_media_timeline_offset_ms
        )
        return list(captured["findings"]), []

    monkeypatch.setattr(
        pipeline, "adjudicate_exact_release_findings", fake_adjudicate
    )

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("原文", "第二句", "第三句"),
        correction_audit=_correction_pass(),
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
        verify_confusable_entity=None,
        verified_authority_audit=None,
        timeline_offset_ms=9_770,
    )

    assert receipt["status"] == "FLAGGED"
    assert captured["source_media_timeline_offset_ms"] == 9_770
    assert captured["findings"] == [finding]


def test_exact_release_context_only_cpa_cannot_reopen_bound_terminal_closure(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline, "_build_final_review_llm_call", lambda: (lambda _prompt: "{}")
    )
    current = "大小姐说了"
    proposed = "大小姐说的"
    finding = {
        "cue_index": 3,
        "kind": "context",
        "suspect": "了",
        "suggestion": "的",
        "proposed_full_cue": proposed,
        "base_text_sha256": hashlib.sha256(current.encode()).hexdigest(),
    }
    monkeypatch.setattr(
        pipeline, "audit_final_subtitles", lambda *_args, **_kwargs: [finding]
    )

    def context_only_adjudication(_srt_text, findings, **_kwargs):
        row = dict(list(findings)[0])
        row["exact_release_adjudication"] = {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "repaired": True,
            "decision_authority": "CPA_JUDGE",
            "policy_branch": "CPA_JUDGE_APPLY_PROPOSED_WITHOUT_AUDIO_WITNESS",
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "PASS",
                "basis": "CPA_CONTEXT_ONLY_CLOSED_SET_DISAMBIGUATION",
            },
            "verdict": {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "status": "UNCERTAIN",
                "reason_code": "ENTITY_AUDIO_PROVIDER_FAILED",
            },
            "request": {
                "schema_version": "subtitle-span-acoustic-check-request.v1",
                "current_cue": current,
                "proposed_cue": proposed,
            },
        }
        return [row], []

    monkeypatch.setattr(
        pipeline,
        "adjudicate_exact_release_findings",
        context_only_adjudication,
    )
    correction = _correction_pass()
    correction["boundary_semantic_review"]["final_endpoint_binding"][
        "closure_text_sha256"
    ] = "sha256:" + hashlib.sha256(current.encode()).hexdigest()

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("前文", "わたくし的话就是大小姐", current),
        correction_audit=correction,
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="日语人称翻译",
        clip_context={},
    )

    assert receipt["status"] == "CLEAN"
    assert receipt["release_gate"] == "PASS"
    assert receipt["findings"] == []
    resolved = receipt["resolved_findings"][0]
    assert resolved["resolution"] == (
        "BOUNDARY_SEMANTIC_PRESERVES_TERMINAL_CLOSURE_WITHOUT_AUDIO"
    )
    assert resolved["boundary_closure_authority"]["status"] == "KEEP_CURRENT"


def test_exact_release_terminal_closure_guard_does_not_override_audio_cpa(
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    monkeypatch.setattr(
        pipeline, "_build_final_review_llm_call", lambda: (lambda _prompt: "{}")
    )
    current = "大小姐说了"
    finding = {"cue_index": 3}
    monkeypatch.setattr(
        pipeline, "audit_final_subtitles", lambda *_args, **_kwargs: [finding]
    )

    def acoustic_adjudication(_srt_text, findings, **_kwargs):
        row = dict(list(findings)[0])
        row["exact_release_adjudication"] = {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "repaired": True,
            "decision_authority": "CPA_JUDGE",
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "PASS",
                "basis": "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION",
            },
            "verdict": {"status": "OBSERVED", "target_audible": True},
            "request": {"current_cue": current, "proposed_cue": "大小姐说的"},
        }
        return [row], []

    monkeypatch.setattr(
        pipeline,
        "adjudicate_exact_release_findings",
        acoustic_adjudication,
    )
    correction = _correction_pass()
    correction["boundary_semantic_review"]["final_endpoint_binding"][
        "closure_text_sha256"
    ] = "sha256:" + hashlib.sha256(current.encode()).hexdigest()

    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("前文", "中段", current),
        correction_audit=correction,
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
    )

    assert receipt["status"] == "FLAGGED"
    assert len(receipt["findings"]) == 1
    assert not any(
        row.get("boundary_closure_authority")
        for row in receipt["resolved_findings"]
    )


def test_post_semantic_entity_stage_never_reverts_name_to_draft_witness(tmp_path):
    """2026-07-16 实案抽象：LLM/词表已把 draft 怪词修成专名后，后置
    Gemini 不得再用“必须和初始听写一致”把它改回 draft 或竞争实体。"""
    padded = tmp_path / "padded.mp4"
    padded.with_suffix(".asr_draft.srt").write_text(
        _srt(f"给温柔已经成为了{CHANNEL_PROFILE.display_name}的帕鲁", "第二句", "第三句"),
        encoding="utf-8",
    )
    # "kmx" must stay literal: this exercises a real registered protected
    # term (src.autoslice.term_authority.protected_terms()), not a synthetic
    # fixture name — introduced_term_cues() only flags protected terms.
    semantic_final = _srt(f"kmx已经成为了{CHANNEL_PROFILE.display_name}的帕鲁", "第二句", "第三句")
    group = ReferentGroup(
        (
            ReferentEntity("kmx", ("kmx",), ("k m x",)),
            ReferentEntity("乒乓球", ("乒乓球",), ("ping pang qiu",)),
        ),
        audio_verify_all_surfaces=True,
    )
    calls = []

    def conflicting_audio(request):
        calls.append(request)
        candidates = [row["canonical"] for row in request["candidate_entities"]]
        winner = "乒乓球" if "乒乓球" in candidates else candidates[-1]
        return _resolved_entity_verdict(request, winner)

    result = pipeline._apply_entity_authority(
        srt_text=semantic_final,
        authoritative_chat=[],
        support_srts=[],
        referent_groups=[group],
        verify_confusable_entity=conflicting_audio,
        code_switch_audit={},
        term_boundary_moves=[],
        padded=padded,
        adapters=_adapters(),
    )

    assert result.srt_text == semantic_final
    assert calls == []
    assert result.chat_authority_audit["post_semantic_entity_policy"]["status"] == (
        "SEMANTIC_TEXT_FINAL"
    )
    assert result.chat_authority_audit["introduced_term_audits"][0]["status"] == (
        "SEMANTIC_AUTHORITY_PRESERVED"
    )


def test_witness_disagreement_is_disclosure_not_post_semantic_rewrite(tmp_path):
    padded = tmp_path / "padded.mp4"
    padded.with_suffix(".asr_draft.srt").write_text(
        _srt("所以你是想看留下跟别人亲亲", "第二句", "第三句"),
        encoding="utf-8",
    )
    semantic_final = _srt(f"所以你是想看{CHANNEL_PROFILE.short_name}跟别人亲亲", "第二句", "第三句")
    group = ReferentGroup(
        (
            ReferentEntity(CHANNEL_PROFILE.display_name, (CHANNEL_PROFILE.display_name,), ("li dou sha",)),
            ReferentEntity(CHANNEL_PROFILE.short_name, (CHANNEL_PROFILE.short_name,), ("xiao li",)),
        ),
        audio_verify_all_surfaces=True,
        positions=("witness_disagreement",),
    )
    calls = []

    def conflicting_audio(request):
        calls.append(request)
        return _resolved_entity_verdict(request, CHANNEL_PROFILE.display_name)

    result = pipeline._apply_entity_authority(
        srt_text=semantic_final,
        authoritative_chat=[],
        support_srts=[],
        referent_groups=[group],
        verify_confusable_entity=conflicting_audio,
        code_switch_audit={},
        term_boundary_moves=[],
        padded=padded,
        adapters=_adapters(),
    )

    assert result.srt_text == semantic_final
    assert calls == []
    audit = result.chat_authority_audit["witness_disagreement_audits"][0]
    assert audit["status"] == "SEMANTIC_AUTHORITY_PRESERVED"
    assert audit["suspicious_cue_indexes"] == [1]


def test_explicit_transcript_only_rescue_still_uses_audio_after_semantic_stage(tmp_path):
    """真正未决的误听面仍可显式进入声学层；新边界只禁止重审普通专名。"""
    padded = tmp_path / "padded.mp4"
    padded.with_suffix(".asr_draft.srt").write_text(
        _srt("所以理论上要直播", "第二句", "第三句"), encoding="utf-8"
    )
    source = _srt("所以理论上要直播", "第二句", "第三句")
    group = ReferentGroup(
        (
            ReferentEntity(CHANNEL_PROFILE.display_name, (CHANNEL_PROFILE.display_name, "理论上"), ("li dou sha",)),
            ReferentEntity(CHANNEL_PROFILE.short_name, (CHANNEL_PROFILE.short_name,), ("xiao li",)),
        ),
        positions=("transcript_only",),
        uncertain_keep_surfaces=("理论上",),
    )
    calls = []

    def resolve_name(request):
        calls.append(request)
        return _resolved_entity_verdict(request, CHANNEL_PROFILE.display_name)

    result = pipeline._apply_entity_authority(
        srt_text=source,
        authoritative_chat=[],
        support_srts=[],
        referent_groups=[group],
        verify_confusable_entity=resolve_name,
        code_switch_audit={},
        term_boundary_moves=[],
        padded=padded,
        adapters=_adapters(),
    )

    assert f"所以{CHANNEL_PROFILE.display_name}要直播" in result.srt_text
    assert len(calls) == 1
    assert result.chat_authority_audit["post_semantic_entity_policy"][
        "explicit_audio_groups"
    ] == [[CHANNEL_PROFILE.display_name, CHANNEL_PROFILE.short_name]]


def test_final_review_adjudicates_all_bounded_findings_and_skips_protected_cue(monkeypatch):
    source_texts = [f"坏词{index}留在这里" for index in range(1, 9)]
    findings = [
        {
            "cue": index,
            "kind": "context",
            "proposed_full_cue": f"好词{index}留在这里",
            "repair_class": "phonetic",
            "why": "上下文明确",
        }
        for index in range(1, 9)
    ]
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: _split_llm(
            json.dumps({"findings": findings}, ensure_ascii=False), "PROPOSED"
        ),
    )
    requests = []

    def choose_proposed(request):
        requests.append(request)
        index = str(request["cue_indexes"][0])
        return _witness_verdict(request, f"hao ci {index} liu zai zhe li")

    output, audit = pipeline._run_final_review(
        srt_text=_srt(*source_texts),
        chat_authority_audit={"applied": []},
        handled_entity_cues={8},
        verify_confusable_entity=choose_proposed,
        adapters=_adapters(),
    )

    assert len(requests) == 7  # no old [:6] truncation; cue 8 is protected
    for index in range(1, 8):
        assert f"好词{index}留在这里" in output
    assert "坏词8留在这里" in output
    assert audit["applied_count"] == 7
    assert audit["findings"][7]["routed"] == "disclosure_protected"


def test_final_review_preserves_typed_discovery_failure_and_original_bytes(
    monkeypatch,
):
    source = _srt("第一句", "第二句")
    chat_audit = {"applied": [], "entity_repairs": [{"mode": "preexisting"}]}
    before_chat = json.loads(json.dumps(chat_audit))
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: "{}"),
    )

    def unavailable(*_args, **_kwargs):
        raise pipeline.FinalReviewAuditError(
            "FINAL_REVIEW_RESPONSE_FINDINGS_MISSING",
            "response object omitted findings",
        )

    monkeypatch.setattr(pipeline, "audit_final_subtitles", unavailable)

    output, audit = pipeline._run_final_review(
        srt_text=source,
        chat_authority_audit=chat_audit,
        handled_entity_cues=set(),
        verify_confusable_entity=lambda _request: {},
        adapters=_adapters(),
    )

    assert output == source
    assert chat_audit == before_chat
    assert audit["status"] == "AUDITOR_UNAVAILABLE"
    assert audit["release_gate"] == "BLOCK"
    assert audit["reason_codes"] == [
        "FINAL_REVIEW_RESPONSE_FINDINGS_MISSING"
    ]
    assert audit["discovery"] == {
        "status": "AUDITOR_UNAVAILABLE",
        "detail": "response object omitted findings",
    }
    assert audit["findings"] == []
    assert audit["applied_count"] == 0
    assert audit["error_type"] == "FinalReviewAuditError"


def test_final_review_rolls_back_prior_mutations_when_later_stage_raises(
    monkeypatch,
):
    source = _srt("欢迎季下", "坏词留在这里")
    chat_audit = {"applied": [], "entity_repairs": [{"mode": "preexisting"}]}
    before_chat = json.loads(json.dumps(chat_audit))
    findings = [
        {
            "cue_index": 1,
            "kind": "nonword",
            "suspect": "季下",
            "suggestion": "记下",
            "proposed_full_cue": "欢迎记下",
            "repair_class": "phonetic",
            "candidate_provenance": {
                "kind": "glossary",
                "surface": "记下",
            },
            "base_text_sha256": hashlib.sha256(
                "欢迎季下".encode("utf-8")
            ).hexdigest(),
        },
        {
            "cue_index": 2,
            "kind": "context",
            "suspect": "坏词",
            "suggestion": "好词",
            "proposed_full_cue": "好词留在这里",
            "repair_class": "phonetic",
            "candidate_provenance": None,
            "base_text_sha256": hashlib.sha256(
                "坏词留在这里".encode("utf-8")
            ).hexdigest(),
        },
    ]
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: (lambda _prompt: '{"findings":[]}'),
    )
    monkeypatch.setattr(
        pipeline,
        "audit_final_subtitles",
        lambda *_args, **_kwargs: findings,
    )
    monkeypatch.setattr(
        pipeline,
        "adjudicate_context_finding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("post-route failure")
        ),
    )

    output, audit = pipeline._run_final_review(
        srt_text=source,
        chat_authority_audit=chat_audit,
        handled_entity_cues=set(),
        verify_confusable_entity=lambda _request: {},
        adapters=_adapters(),
    )

    assert output == source
    assert "欢迎记下" not in output
    assert chat_audit == before_chat
    assert audit["status"] == "AUDITOR_UNAVAILABLE"
    assert audit["reason_codes"] == ["FINAL_REVIEW_UNEXPECTED_ERROR"]
    assert audit["discovery"] == {
        "status": "AUDITOR_UNAVAILABLE",
        "detail": "RuntimeError",
    }
    assert audit["findings"] == []
    assert audit["applied_count"] == 0


def test_single_character_glossary_prose_cannot_authorize_li_to_li():
    source = _srt("这个李有点太多了")
    findings = pipeline.audit_final_subtitles(
        source,
        llm_call=lambda _prompt: json.dumps(
            {
                "findings": [
                    {
                        "cue": 1,
                        "kind": "context",
                        "proposed_full_cue": "这个礼有点太多了",
                        "repair_class": "phonetic",
                        "source_surface": "礼",
                        "why": "词表 prose 恰好包含同音单字",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        extract_json=json.loads,
        glossary_text=(
            "可以是礼（乙乙的礼），也可以是礼（花礼的礼）"
        ),
    )

    assert findings[0]["candidate_provenance"] == {
        "kind": "glossary_context",
        "surface": "礼",
    }
    output, audit = pipeline.route_findings(
        source,
        findings,
        protected_term_set=frozenset(),
    )
    assert output == source
    assert audit["applied_count"] == 0
    assert audit["findings"][0]["routed"] == "disclosure"
    assert audit["findings"][0]["orthography_authority"]["status"] == (
        "BLOCK"
    )


def test_final_review_allows_only_one_contextual_mutation_per_cue(monkeypatch):
    findings = [
        {
            "cue": 1,
            "kind": "context",
            "proposed_full_cue": "好甲和坏乙",
            "repair_class": "phonetic",
            "why": "first",
        },
        {
            "cue": 1,
            "kind": "context",
            "proposed_full_cue": "坏甲和好乙",
            "repair_class": "phonetic",
            "why": "second",
        },
    ]
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: _split_llm(
            json.dumps({"findings": findings}, ensure_ascii=False), "PROPOSED"
        ),
    )
    requests = []

    def observe(request):
        requests.append(request)
        return _witness_verdict(request, "hao jia he huai yi")

    output, audit = pipeline._run_final_review(
        srt_text=_srt("坏甲和坏乙"),
        chat_authority_audit={"applied": []},
        handled_entity_cues=set(),
        verify_confusable_entity=observe,
        adapters=_adapters(),
    )

    assert len(requests) == 1
    assert "好甲和坏乙" in output
    assert audit["findings"][1]["routed"] == "deferred_same_cue"
    assert audit["status"] == "PARTIAL"


def test_final_review_context_fixes_register_for_final_surface_verification(monkeypatch):
    """已应用的语境裁决修复必须进入 entity_repairs 登记，被终稿面验证按原时窗
    复证存活（delivery-divergence 防线）；交付工件若回退到修复前文本必须判失败。"""
    findings = [
        {
            "cue": 1,
            "kind": "context",
            "proposed_full_cue": "好词1留在这里",
            "repair_class": "phonetic",
            "why": "register test",
        }
    ]
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: _split_llm(
            json.dumps({"findings": findings}, ensure_ascii=False), "PROPOSED"
        ),
    )

    def observe(request):
        return _witness_verdict(request, "hao ci yi liu zai zhe li")

    chat_authority_audit: dict = {"applied": []}
    output, audit = pipeline._run_final_review(
        srt_text=_srt("坏词1留在这里", "第二句不动"),
        chat_authority_audit=chat_authority_audit,
        handled_entity_cues=set(),
        verify_confusable_entity=observe,
        adapters=_adapters(),
    )

    assert audit["applied_count"] == 1
    rows = chat_authority_audit["entity_repairs"]
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "final_review_context_adjudication"
    assert row["structured_exact_text"] == "好词1留在这里"
    assert row["before"] == ["坏词1留在这里"]
    assert (row["matched_start_ms"], row["matched_end_ms"]) == (5000, 9000)
    # 无 expected_entity/resolved_canonical：未注册回退与矛盾和解都跳过该行。
    assert "expected_entity" not in row and "resolved_canonical" not in row

    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    assert verify_chat_authority_final_surfaces(
        chat_authority_audit,
        final_text_srt=output,
        final_speaker_srt=output,
        delivery_start_ms=0,
        delivery_end_ms=60_000,
    )
    diverged = output.replace("好词1", "坏词1")
    assert not verify_chat_authority_final_surfaces(
        chat_authority_audit,
        final_text_srt=diverged,
        final_speaker_srt=diverged,
        delivery_start_ms=0,
        delivery_end_ms=60_000,
    )


def test_final_review_drop_cue_registers_typed_final_surface_receipt(
    monkeypatch,
):
    findings = [
        {
            "cue": 1,
            "kind": "context",
            "proposed_full_cue": "",
            "repair_class": "acoustic_drop_cue",
            "why": "whole cue is non-semantic hallucination",
        }
    ]
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: _split_llm(
            json.dumps({"findings": findings}, ensure_ascii=False),
            "DROP",
        ),
    )

    chat_authority_audit: dict = {"applied": []}
    output, audit = pipeline._run_final_review(
        srt_text=_srt("咳咳咳", "保留的下一句"),
        chat_authority_audit=chat_authority_audit,
        handled_entity_cues=set(),
        verify_confusable_entity=lambda request: _witness_verdict(
            request,
            "",
            audible=False,
        ),
        adapters=_adapters(),
    )

    assert audit["applied_count"] == 1
    assert "咳咳咳" not in output
    row = chat_authority_audit["entity_repairs"][0]
    assert row["repair_class"] == "acoustic_drop_cue"
    assert row["decision_authority"] == "CPA_JUDGE"
    assert row["judge"]["choice"] == "DROP"
    assert row["drop_authority"]["status"] == "PASS"
    assert row["mutation_authority"]["status"] == "PASS"

    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    assert verify_chat_authority_final_surfaces(
        chat_authority_audit,
        final_text_srt=output,
        final_speaker_srt=output,
        delivery_start_ms=0,
        delivery_end_ms=60_000,
    )


def test_final_review_inaudible_proposed_records_explicit_cpa_override(
    monkeypatch,
):
    findings = [
        {
            "cue": 1,
            "kind": "context",
            "proposed_full_cue": "嗯嗯",
            "repair_class": "phonetic",
            "why": "CPA may overrule AGY after seeing inaudible evidence",
        }
    ]
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: _split_llm(
            json.dumps({"findings": findings}, ensure_ascii=False),
            "PROPOSED",
        ),
    )

    output, audit = pipeline._run_final_review(
        srt_text=_srt("咳咳", "保留的下一句"),
        chat_authority_audit={"applied": []},
        handled_entity_cues=set(),
        verify_confusable_entity=lambda request: _witness_verdict(
            request,
            "",
            audible=False,
        ),
        adapters=_adapters(),
    )

    assert "嗯嗯" in output
    adjudication = audit["findings"][0]["context_audio_adjudication"]
    assert adjudication["policy_branch"] == (
        "CPA_EXPLICIT_OVERRIDE_INAUDIBLE_WITNESS"
    )
    assert adjudication["mutation_authority"]["basis"] == (
        "CPA_EXPLICIT_OVERRIDE_INAUDIBLE_WITNESS"
    )
    assert (
        adjudication["witness_judge"]["inaudible_witness_override"][
            "status"
        ]
        == "PASS"
    )

    from src.autoslice.final_review_auditor import (
        audit_correction_mutation_authority,
    )

    assert audit_correction_mutation_authority(audit)["status"] == "PASS"


def test_final_review_marks_findings_beyond_audio_budget(monkeypatch):
    source_texts = [f"坏词{index}留在这里" for index in range(1, 14)]
    findings = [
        {
            "cue": index,
            "kind": "context",
            "proposed_full_cue": f"好词{index}留在这里",
            "repair_class": "phonetic",
            "why": "budget test",
        }
        for index in range(1, 14)
    ]
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: _split_llm(
            json.dumps({"findings": findings}, ensure_ascii=False), "PROPOSED"
        ),
    )
    requests = []

    def observe(request):
        requests.append(request)
        return _witness_verdict(request, "hao jia he huai yi")

    _, audit = pipeline._run_final_review(
        srt_text=_srt(*source_texts),
        chat_authority_audit={"applied": []},
        handled_entity_cues=set(),
        verify_confusable_entity=observe,
        adapters=_adapters(),
    )

    assert len(requests) == 12
    assert audit["findings"][12]["routed"] == "skipped_budget"
    assert audit["findings"][12]["context_audio_adjudication"]["status"] == "SKIPPED_BUDGET"
    assert audit["status"] == "PARTIAL"


def test_final_review_lets_cpa_decide_when_agy_provider_fails(monkeypatch):
    """AGY quota failure removes evidence, not CPA's final authority."""
    findings = [
        {
            "cue": 1,
            "kind": "context",
            "proposed_full_cue": "只剩下和成天下了",
            "repair_class": "phonetic",
            "why": "quota blocked",
        },
        {
            "cue": 2,
            "kind": "context",
            "proposed_full_cue": "观察后保留原文的句子",
            "repair_class": "phonetic",
            "why": "observed keep current",
        },
    ]
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: _split_llm(
            json.dumps({"findings": findings}, ensure_ascii=False),
            "PROPOSED",
        ),
    )

    def provider_failed_then_observed(request):
        if request["cue_indexes"] == [1]:
            return {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "request_sha256": request["request_sha256"],
                "status": "UNCERTAIN",
                "reason_code": "ENTITY_AUDIO_PROVIDER_FAILED",
                "detail": "GEMINI_API_QUOTA_EXHAUSTED;GEMINI_API_QUOTA_EXHAUSTED",
            }
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "SUPPORTED",
            "proposed_fit": "UNRESOLVED",
        }

    output, audit = pipeline._run_final_review(
        srt_text=_srt("只剩下核酸天下了", "观察后保留原立的句子"),
        chat_authority_audit={"applied": []},
        handled_entity_cues=set(),
        verify_confusable_entity=provider_failed_then_observed,
        adapters=_adapters(),
    )

    assert "和成天下" in output
    first = audit["findings"][0]["context_audio_adjudication"]
    assert first["status"] == "OBSERVED"
    assert first["policy_branch"] == (
        "CPA_JUDGE_APPLY_PROPOSED_WITHOUT_AUDIO_WITNESS"
    )
    assert first["mutation_authority"]["basis"] == (
        "CPA_CONTEXT_ONLY_CLOSED_SET_DISAMBIGUATION"
    )
    assert audit["infra_unresolved_count"] == 0


def test_ledger_owned_cue_skips_entity_arbitration(tmp_path):
    """钉子辖区先豁免（2026-07-20 七星 r6 零三案）：ledger 拥有的 cue 不进
    声学仲裁——不烧 key ladder,也不许 infra 失败挡住钉子能解决的槽位。"""
    padded = tmp_path / "padded.mp4"
    padded.with_suffix(".asr_draft.srt").write_text(
        _srt("我的我也不零三", "第二句", "第三句"), encoding="utf-8"
    )
    source = _srt("我的我也不零三", "第二句", "第三句")
    group = ReferentGroup(
        (
            ReferentEntity(CHANNEL_PROFILE.display_name, (CHANNEL_PROFILE.display_name, "零三"), ("li dou sha",)),
            ReferentEntity(CHANNEL_PROFILE.short_name, (CHANNEL_PROFILE.short_name,), ("xiao li",)),
        ),
        positions=("transcript_only",),
        uncertain_keep_surfaces=(),
    )
    calls = []

    def resolve_name(request):
        calls.append(request)
        return _resolved_entity_verdict(request, CHANNEL_PROFILE.display_name)

    result = pipeline._apply_entity_authority(
        srt_text=source,
        authoritative_chat=[],
        support_srts=[],
        referent_groups=[group],
        verify_confusable_entity=resolve_name,
        code_switch_audit={},
        term_boundary_moves=[],
        padded=padded,
        adapters=_adapters(),
        source_truth_protected_cue_indexes=[1],
    )

    assert calls == []
    assert result.srt_text == source
    audit = result.chat_authority_audit["transcript_entity_audit"]
    assert audit["ledger_excluded_cue_indexes"] == [1]


def test_exact_source_truth_projection_does_not_exclude_grazed_neighbour(
    tmp_path,
):
    padded = tmp_path / "padded.mp4"
    source = _srt(
        "我的我也不零三",
        "邻句也提到零三",
        "第三句",
    )
    padded.with_suffix(".asr_draft.srt").write_text(
        source,
        encoding="utf-8",
    )
    group = ReferentGroup(
        (
            ReferentEntity(CHANNEL_PROFILE.display_name, (CHANNEL_PROFILE.display_name, "零三"), ("li dou sha",)),
            ReferentEntity(CHANNEL_PROFILE.short_name, (CHANNEL_PROFILE.short_name,), ("xiao li",)),
        ),
        positions=("transcript_only",),
        uncertain_keep_surfaces=(),
    )
    calls = []

    def resolve_name(request):
        calls.append(request)
        return _resolved_entity_verdict(request, CHANNEL_PROFILE.display_name)

    result = pipeline._apply_entity_authority(
        srt_text=source,
        authoritative_chat=[],
        support_srts=[],
        referent_groups=[group],
        verify_confusable_entity=resolve_name,
        code_switch_audit={},
        term_boundary_moves=[],
        padded=padded,
        adapters=_adapters(),
        source_truth_protected_cue_indexes=[1],
    )

    assert [request["cue_indexes"] for request in calls] == [[2]]
    audit = result.chat_authority_audit["transcript_entity_audit"]
    assert audit["source_truth_preview_excluded_cue_indexes"] == [1]


def test_missing_substring_truth_defers_only_with_redelivery_baseline(tmp_path):
    path = tmp_path / "chat-authority.json"
    audit = {
        "status": "FAILED",
        "failures": [
            {
                "action": "replace_substring",
                "reason_code": "REQUIRED_SOURCE_TRUTH_NOT_SATISFIED",
                "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
            }
        ],
    }
    chat = {"source_subtitle_truth_audit": audit}

    pipeline._defer_source_truth_failure_for_redelivery(
        spec={"subtitle_redelivery_baseline": {"schema_version": "test"}},
        source_truth_audit=audit,
        chat_authority_audit=chat,
        chat_authority_path=path,
    )

    assert audit["status"] == "DEFERRED_TO_REDELIVERY_BASELINE"
    assert json.loads(path.read_text(encoding="utf-8"))[
        "source_subtitle_truth_audit"
    ]["pre_redelivery_status"] == "FAILED"


def test_exact_interval_replay_defers_fresh_cue_shape_failure(tmp_path):
    """Reviewed exact replay owns the timeline; fresh ASR cue splits do not."""

    path = tmp_path / "chat-authority.json"
    audit = {
        "status": "FAILED",
        "failures": [
            {
                "action": "replace_cue",
                "reason_code": "REPLACE_CUE_TARGET_NOT_UNIQUE",
                "local_windows": [{"start_ms": 34_640, "end_ms": 36_520}],
            }
        ],
    }

    pipeline._defer_source_truth_failure_for_redelivery(
        spec={
            "subtitle_redelivery_baseline": {
                "schema_version": "subtitle-redelivery-baseline.v2",
                "exact_interval_replay": True,
            }
        },
        source_truth_audit=audit,
        chat_authority_audit={"source_subtitle_truth_audit": audit},
        chat_authority_path=path,
    )

    assert audit["status"] == "DEFERRED_TO_REDELIVERY_BASELINE"
    assert audit["deferred_strategy"] == (
        "exact_reviewed_interval_replay_then_reapply_source_truth"
    )


@pytest.mark.parametrize(
    "reason_code",
    [
        "MENTION_REQUIRED_TEXT_MISSING",
        "MENTION_FORBIDDEN_TOKEN_SURVIVED",
    ],
)
def test_exact_interval_replay_defers_isolated_mention_postconditions(
    tmp_path,
    reason_code,
):
    path = tmp_path / "chat-authority.json"
    audit = {
        "status": "FAILED",
        "failures": [
            {
                "required": True,
                "action": "replace_substring",
                "reason_code": reason_code,
                "local_windows": [
                    {"start_ms": 85_660, "end_ms": 99_780}
                ],
                "mention_owner_resolution": {
                    "status": "PASS",
                    "cue_indexes": [38, 39, 41, 43, 44],
                    "failure_reason_codes": [],
                },
            }
        ],
    }

    pipeline._defer_source_truth_failure_for_redelivery(
        spec={
            "subtitle_redelivery_baseline": (
                _redelivery_baseline_config_v2()
            )
        },
        source_truth_audit=audit,
        chat_authority_audit={"source_subtitle_truth_audit": audit},
        chat_authority_path=path,
    )

    assert audit["status"] == "DEFERRED_TO_REDELIVERY_BASELINE"
    assert audit["deferred_strategy"] == (
        "exact_reviewed_interval_replay_then_reapply_source_truth"
    )


@pytest.mark.parametrize(
    ("failure_override", "baseline_override"),
    [
        (
            {"required": False},
            {},
        ),
        (
            {"local_windows": []},
            {},
        ),
        (
            {
                "reason_code": (
                    "MENTION_POSTCONDITION_TARGET_NOT_ISOLATED"
                )
            },
            {},
        ),
        (
            {
                "mention_owner_resolution": {
                    "status": "BLOCK",
                    "cue_indexes": [],
                    "failure_reason_codes": [
                        "MENTION_POSTCONDITION_TARGET_NOT_ISOLATED"
                    ],
                }
            },
            {},
        ),
        (
            {},
            {"source_sha256": "invalid"},
        ),
        (
            {},
            {"exact_interval_replay": False},
        ),
    ],
)
def test_exact_replay_mention_deferral_requires_complete_isolated_authority(
    tmp_path,
    failure_override,
    baseline_override,
):
    path = tmp_path / "chat-authority.json"
    failure = {
        "required": True,
        "action": "replace_substring",
        "reason_code": "MENTION_REQUIRED_TEXT_MISSING",
        "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
        "mention_owner_resolution": {
            "status": "PASS",
            "cue_indexes": [1],
            "failure_reason_codes": [],
        },
        **failure_override,
    }
    baseline = {
        **_redelivery_baseline_config_v2(),
        **baseline_override,
    }
    audit = {"status": "FAILED", "failures": [failure]}

    with pytest.raises(SystemExit, match="SOURCE_SUBTITLE_TRUTH_REQUIRED"):
        pipeline._defer_source_truth_failure_for_redelivery(
            spec={"subtitle_redelivery_baseline": baseline},
            source_truth_audit=audit,
            chat_authority_audit={
                "source_subtitle_truth_audit": audit
            },
            chat_authority_path=path,
        )

    assert audit["status"] == "FAILED"
    assert not path.exists()


def test_exact_replay_does_not_defer_mixed_mention_failure_kinds(tmp_path):
    path = tmp_path / "chat-authority.json"
    owner = {
        "status": "PASS",
        "cue_indexes": [1],
        "failure_reason_codes": [],
    }
    audit = {
        "status": "FAILED",
        "failures": [
            {
                "required": True,
                "action": "replace_substring",
                "reason_code": "MENTION_REQUIRED_TEXT_MISSING",
                "local_windows": [
                    {"start_ms": 1_000, "end_ms": 2_000}
                ],
                "mention_owner_resolution": owner,
            },
            {
                "required": True,
                "action": "replace_substring",
                "reason_code": "REPLACE_SUBSTRING_OWNER_AMBIGUOUS",
                "local_windows": [
                    {"start_ms": 2_000, "end_ms": 3_000}
                ],
                "mention_owner_resolution": owner,
            },
        ],
    }

    with pytest.raises(SystemExit, match="SOURCE_SUBTITLE_TRUTH_REQUIRED"):
        pipeline._defer_source_truth_failure_for_redelivery(
            spec={
                "subtitle_redelivery_baseline": (
                    _redelivery_baseline_config_v2()
                )
            },
            source_truth_audit=audit,
            chat_authority_audit={
                "source_subtitle_truth_audit": audit
            },
            chat_authority_path=path,
        )

    assert audit["status"] == "FAILED"
    assert not path.exists()


def test_exact_interval_replay_grant_must_be_literal_boolean(tmp_path):
    path = tmp_path / "chat-authority.json"
    audit = {
        "status": "FAILED",
        "failures": [
            {
                "action": "replace_cue",
                "reason_code": "REPLACE_CUE_TARGET_NOT_UNIQUE",
                "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
            }
        ],
    }

    with pytest.raises(SystemExit, match="SOURCE_SUBTITLE_TRUTH_REQUIRED"):
        pipeline._defer_source_truth_failure_for_redelivery(
            spec={
                "subtitle_redelivery_baseline": {
                    "schema_version": "subtitle-redelivery-baseline.v2",
                    "exact_interval_replay": "true",
                }
            },
            source_truth_audit=audit,
            chat_authority_audit={"source_subtitle_truth_audit": audit},
            chat_authority_path=path,
        )


def test_structural_source_truth_failure_never_defers_to_baseline(tmp_path):
    path = tmp_path / "chat-authority.json"
    audit = {
        "status": "FAILED",
        "failures": [
            {
                "action": "drop_cue",
                "reason_code": "DROP_CUE_STRADDLES_TRUTH_INTERVAL",
                "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
            }
        ],
    }

    with pytest.raises(SystemExit, match="SOURCE_SUBTITLE_TRUTH_REQUIRED"):
        pipeline._defer_source_truth_failure_for_redelivery(
            spec={"subtitle_redelivery_baseline": {"schema_version": "test"}},
            source_truth_audit=audit,
            chat_authority_audit={"source_subtitle_truth_audit": audit},
            chat_authority_path=path,
        )

    assert audit["status"] == "FAILED"
    assert not path.exists()


def test_exact_replay_does_not_defer_structural_source_truth_failure(tmp_path):
    path = tmp_path / "chat-authority.json"
    audit = {
        "status": "FAILED",
        "failures": [
            {
                "action": "drop_cue",
                "reason_code": "DROP_CUE_STRADDLES_TRUTH_INTERVAL",
                "local_windows": [{"start_ms": 1_000, "end_ms": 2_000}],
            }
        ],
    }

    with pytest.raises(SystemExit, match="SOURCE_SUBTITLE_TRUTH_REQUIRED"):
        pipeline._defer_source_truth_failure_for_redelivery(
            spec={
                "subtitle_redelivery_baseline": {
                    "schema_version": "subtitle-redelivery-baseline.v2",
                    "exact_interval_replay": True,
                }
            },
            source_truth_audit=audit,
            chat_authority_audit={"source_subtitle_truth_audit": audit},
            chat_authority_path=path,
        )

    assert audit["status"] == "FAILED"


def _unproven_foreign_audit() -> dict:
    return {
        "status": "BLOCKED_UNPROVEN_FOREIGN_SPEAKER",
        "unproven_foreign_introductions": [
            {
                "cue_index": 37,
                "start_ms": 85_220,
                "end_ms": 87_900,
                "draft": "分牙三四关就毁神",
                "attempted": "非常やさしい，就病院坂灵",
            }
        ],
    }


def _redelivery_baseline_config() -> dict:
    return {
        "schema_version": "subtitle-redelivery-baseline.v1",
        "mode": "preserve_text_outside_source_truth",
        "path": "/reviewed/prior.srt",
        "sha256": "a" * 64,
        "authority": "hash-bound reviewed prior delivery",
    }


def _redelivery_baseline_config_v2() -> dict:
    return {
        **_redelivery_baseline_config(),
        "schema_version": "subtitle-redelivery-baseline.v2",
        "source_recording_basename": "recording.mp4",
        "source_sha256": "b" * 64,
        "absolute_source_start_ms": 10_000,
        "absolute_source_end_ms": 20_000,
        "exact_interval_replay": True,
    }


def test_unproven_foreign_cue_defers_to_full_source_truth_ownership():
    audit = _unproven_foreign_audit()

    pipeline.defer_unproven_foreign_introductions_to_late_authority(
        audit,
        source_truth_windows=[(85_000, 88_000)],
        redelivery_baseline_config=None,
    )

    assert audit["status"] == "DEFERRED_TO_SOURCE_SUBTITLE_TRUTH"


def test_unproven_foreign_cue_defers_across_tiny_timing_sliver():
    """7/22 real shape: the reviewed window ends 30 ms before the ASR cue."""

    audit = {
        "status": "BLOCKED_UNPROVEN_FOREIGN_SPEAKER",
        "unproven_foreign_introductions": [
            {
                "cue_index": 36,
                "start_ms": 81_070,
                "end_ms": 82_850,
                "draft": "非常亚撒西雅",
                "attempted": "非常やさしい呀",
            }
        ],
    }

    pipeline.defer_unproven_foreign_introductions_to_late_authority(
        audit,
        source_truth_windows=[(81_020, 82_820)],
        redelivery_baseline_config=None,
    )

    assert audit["status"] == "DEFERRED_TO_SOURCE_SUBTITLE_TRUTH"


def test_source_truth_owned_cue_is_not_mutated_by_final_review(monkeypatch):
    # 终审 llm_call 由 producer_final_review_transport 构建；patch
    # pipeline.build_llm_call 是死 seam（曾靠开发机 ambient CPA 凭据真打
    # 网络"变绿"，密闭守卫见根 conftest）。必须 patch pipeline 自己引用的
    # _build_final_review_llm_call。
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: lambda prompt: json.dumps(
            {
                "findings": [
                    {
                        "cue": 1,
                        "kind": "nonword",
                        "proposed_full_cue": "非常やさしい呀",
                        "repair_class": "phonetic",
                        "why": "model prefers source script",
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    calls = []

    def verifier(request):
        calls.append(request)
        raise AssertionError("source-truth-owned cue must not reach acoustics")

    output, audit = pipeline._run_final_review(
        srt_text=_srt("非常亚撒西雅"),
        chat_authority_audit={"applied": []},
        handled_entity_cues=set(),
        verify_confusable_entity=verifier,
        adapters=_adapters(),
        source_truth_protected_cue_indexes=[1],
    )

    assert calls == []
    assert "非常亚撒西雅" in output
    assert "やさしい" not in output
    assert audit["source_truth_protected_cue_indexes"] == [1]
    assert audit["findings"][0]["routed"] == "disclosure_protected"


def test_unproven_foreign_cue_does_not_defer_to_partial_source_truth():
    audit = _unproven_foreign_audit()

    pipeline.defer_unproven_foreign_introductions_to_late_authority(
        audit,
        # 2026-07-22 actual shape: the broad 毁神 window overlaps the cue but
        # begins after the blocked cue's start, so source truth alone cannot
        # own the finding.
        source_truth_windows=[(86_680, 102_020)],
        redelivery_baseline_config=None,
    )

    assert audit["status"] == "BLOCKED_UNPROVEN_FOREIGN_SPEAKER"


def test_partial_source_truth_can_defer_to_hash_bound_redelivery_baseline():
    audit = _unproven_foreign_audit()

    pipeline.defer_unproven_foreign_introductions_to_late_authority(
        audit,
        source_truth_windows=[(86_680, 102_020)],
        redelivery_baseline_config=_redelivery_baseline_config(),
    )

    assert audit["status"] == "DEFERRED_TO_REDELIVERY_BASELINE"


def test_partial_source_truth_can_defer_to_valid_v2_redelivery_baseline():
    audit = _unproven_foreign_audit()

    pipeline.defer_unproven_foreign_introductions_to_late_authority(
        audit,
        source_truth_windows=[(86_680, 102_020)],
        redelivery_baseline_config=_redelivery_baseline_config_v2(),
    )

    assert audit["status"] == "DEFERRED_TO_REDELIVERY_BASELINE"


def _pinned_replay_spec() -> dict:
    return {
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v2",
            "mode": "preserve_text_outside_source_truth",
            "exact_interval_replay": True,
            "path": "/tmp/reviewed.srt",
            "sha256": "de" + "ad" * 31,
            "authority": "published bytes are the lexical baseline",
            "source_recording_basename": "123456_20260729-22-50-56.mp4",
            "source_sha256": "15" + "ed" * 31,
            "absolute_source_start_ms": 1_013_630,
            "absolute_source_end_ms": 1_117_320,
        },
        "recovery_publication_authority": {
            "title_mode": "verified_public_exact",
            "authority_sha256": "sha256:" + "ab" * 32,
        },
    }


def test_pinned_replay_ownership_requires_both_pins():
    spec = _pinned_replay_spec()
    ownership = pipeline._pinned_replay_reviewed_text_ownership(spec)
    assert ownership is not None
    assert ownership["status"] if "status" in ownership else True
    assert ownership["baseline_sha256"] == spec[
        "subtitle_redelivery_baseline"
    ]["sha256"]
    assert ownership["publication_authority_sha256"] == "sha256:" + "ab" * 32

    for mutate in (
        lambda s: s.pop("subtitle_redelivery_baseline"),
        lambda s: s.pop("recovery_publication_authority"),
        lambda s: s["subtitle_redelivery_baseline"].pop("exact_interval_replay"),
        lambda s: s["subtitle_redelivery_baseline"].update(
            schema_version="subtitle-redelivery-baseline.v1"
        ),
        lambda s: s["subtitle_redelivery_baseline"].update(authority=""),
        lambda s: s["recovery_publication_authority"].update(
            title_mode="ivan_manual_override"
        ),
        lambda s: s["recovery_publication_authority"].update(
            authority_sha256=""
        ),
    ):
        broken = _pinned_replay_spec()
        mutate(broken)
        assert pipeline._pinned_replay_reviewed_text_ownership(broken) is None


def test_pinned_replay_branch_skips_reviewer_without_touching_gates():
    source = inspect.getsource(pipeline.run_text_pipeline)
    tree = ast.parse(source)
    branch = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test_src = ast.unparse(node.test)
            if "pinned_replay_ownership is not None" in test_src:
                branch = node
                break
    assert branch is not None, "pinned-replay branch missing from run_text_pipeline"
    then_src = "\n".join(ast.unparse(row) for row in branch.body)
    else_src = "\n".join(ast.unparse(row) for row in branch.orelse)
    assert "_run_final_review" not in then_src
    assert "SKIPPED_PINNED_REPLAY" in then_src
    assert "pinned_replay_ownership" in then_src
    assert "_run_final_review" in else_src
    assert "_fidelity_review_candidates" in else_src
    # 快路径不得越权：重放后的 exact-final 终审与边界评审必须仍在
    # 无条件路径上（不在这个 if 的任一分支里被吞掉）。
    assert "_run_exact_final_release_review" not in then_src
    assert "review_final_boundary_semantics" not in then_src
