import json
from pathlib import Path

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.chat_authority import ReferentEntity, ReferentGroup
from src.autoslice.final_review_contract import (
    FinalReviewContractError,
    validate_final_review_release,
)


def _srt(*texts: str) -> str:
    def timestamp(seconds: int) -> str:
        minutes, seconds = divmod(seconds, 60)
        return f"00:{minutes:02d}:{seconds:02d}"

    return "\n\n".join(
        f"{index}\n{timestamp(index * 5)},000 --> {timestamp(index * 5 + 4)},000\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


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
    return {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "reason_codes": [],
    }


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
        correction_audit={"boundary_semantic_review": _boundary_pass()},
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


def test_exact_final_release_review_provider_failure_is_a_block(monkeypatch):
    monkeypatch.setattr(pipeline, "clip_context_prompt_text", lambda _value: "")
    def broken(_prompt):
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        pipeline, "_build_final_review_llm_call", lambda: broken
    )
    receipt = pipeline._run_exact_final_release_review(
        srt_text=_srt("第一句", "第二句", "第三句"),
        correction_audit={"boundary_semantic_review": _boundary_pass()},
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
        correction_audit={"boundary_semantic_review": _boundary_pass()},
        adapters=_adapters(),
        authoritative_chat=(),
        selection_hook="",
        clip_context={},
    )

    assert receipt["status"] == "FLAGGED"
    assert receipt["reason_codes"] == ["FINAL_REVIEW_UNRESOLVED_FINDINGS"]
    with pytest.raises(FinalReviewContractError):
        validate_final_review_release(receipt)


def test_post_semantic_entity_stage_never_reverts_name_to_draft_witness(tmp_path):
    """2026-07-16 实案抽象：LLM/词表已把 draft 怪词修成专名后，后置
    Gemini 不得再用“必须和初始听写一致”把它改回 draft 或竞争实体。"""
    padded = tmp_path / "padded.mp4"
    padded.with_suffix(".asr_draft.srt").write_text(
        _srt("给温柔已经成为了李豆沙的帕鲁", "第二句", "第三句"),
        encoding="utf-8",
    )
    semantic_final = _srt("kmx已经成为了李豆沙的帕鲁", "第二句", "第三句")
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
    semantic_final = _srt("所以你是想看小李跟别人亲亲", "第二句", "第三句")
    group = ReferentGroup(
        (
            ReferentEntity("李豆沙", ("李豆沙",), ("li dou sha",)),
            ReferentEntity("小李", ("小李",), ("xiao li",)),
        ),
        audio_verify_all_surfaces=True,
        positions=("witness_disagreement",),
    )
    calls = []

    def conflicting_audio(request):
        calls.append(request)
        return _resolved_entity_verdict(request, "李豆沙")

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
            ReferentEntity("李豆沙", ("李豆沙", "理论上"), ("li dou sha",)),
            ReferentEntity("小李", ("小李",), ("xiao li",)),
        ),
        positions=("transcript_only",),
        uncertain_keep_surfaces=("理论上",),
    )
    calls = []

    def resolve_name(request):
        calls.append(request)
        return _resolved_entity_verdict(request, "李豆沙")

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

    assert "所以李豆沙要直播" in result.srt_text
    assert len(calls) == 1
    assert result.chat_authority_audit["post_semantic_entity_policy"][
        "explicit_audio_groups"
    ] == [["李豆沙", "小李"]]


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
        "build_llm_call",
        lambda config: lambda prompt: json.dumps({"findings": findings}, ensure_ascii=False),
    )
    requests = []

    def choose_proposed(request):
        requests.append(request)
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "PLAUSIBLE",
            "proposed_fit": "SUPPORTED",
            "heard_syllables": "bounded test",
        }

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
        "build_llm_call",
        lambda config: lambda prompt: json.dumps({"findings": findings}, ensure_ascii=False),
    )
    requests = []

    def observe(request):
        requests.append(request)
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "PLAUSIBLE",
            "proposed_fit": "SUPPORTED",
        }

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
        "build_llm_call",
        lambda config: lambda prompt: json.dumps({"findings": findings}, ensure_ascii=False),
    )

    def observe(request):
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "PLAUSIBLE",
            "proposed_fit": "SUPPORTED",
        }

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
        "build_llm_call",
        lambda config: lambda prompt: json.dumps({"findings": findings}, ensure_ascii=False),
    )
    requests = []

    def observe(request):
        requests.append(request)
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "PLAUSIBLE",
            "proposed_fit": "SUPPORTED",
        }

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


def test_final_review_marks_provider_failed_adjudications_infra_unresolved(monkeypatch):
    """2026-07-18 交付事故类机制：provider 额度失败导致的 UNCERTAIN 不是证据
    裁决，必须记入 infra_unresolved（而 OBSERVED 下的 keep-current 不记）。"""
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
        "build_llm_call",
        lambda config: lambda prompt: json.dumps({"findings": findings}, ensure_ascii=False),
    )

    def provider_failed_then_observed(request):
        if request["cue_indexes"] == [1]:
            return {
                "schema_version": "subtitle-span-acoustic-check-verdict.v1",
                "request_sha256": request["request_sha256"],
                "status": "UNCERTAIN",
                "reason_code": "ENTITY_AUDIO_PROVIDER_FAILED",
                "detail": "GEMINI_API_QUOTA_EXHAUSTED;GEMINI_API_QUOTA_EXHAUSTED",
            }
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
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

    assert "核酸天下" in output  # 未修（provider 失败），但必须被标记
    assert audit["infra_unresolved_count"] == 1
    assert audit["infra_unresolved"][0]["cue_index"] == 1
    assert audit["infra_unresolved"][0]["reason_code"] == "ENTITY_AUDIO_PROVIDER_FAILED"


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
            ReferentEntity("李豆沙", ("李豆沙", "零三"), ("li dou sha",)),
            ReferentEntity("小李", ("小李",), ("xiao li",)),
        ),
        positions=("transcript_only",),
        uncertain_keep_surfaces=(),
    )
    calls = []

    def resolve_name(request):
        calls.append(request)
        return _resolved_entity_verdict(request, "李豆沙")

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
        # 第一条 cue (5-9s) 在钉子辖区内
        source_truth_windows=[(5_000, 7_500)],
    )

    assert calls == []
    assert result.srt_text == source
    audit = result.chat_authority_audit["transcript_entity_audit"]
    assert audit["ledger_excluded_cue_indexes"] == [1]


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

    pipeline._defer_unproven_foreign_introductions_to_late_authority(
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

    pipeline._defer_unproven_foreign_introductions_to_late_authority(
        audit,
        source_truth_windows=[(81_020, 82_820)],
        redelivery_baseline_config=None,
    )

    assert audit["status"] == "DEFERRED_TO_SOURCE_SUBTITLE_TRUTH"


def test_source_truth_owned_cue_is_not_mutated_by_final_review(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "build_llm_call",
        lambda config: lambda prompt: json.dumps(
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
        source_truth_windows=[(4_950, 8_970)],
    )

    assert calls == []
    assert "非常亚撒西雅" in output
    assert "やさしい" not in output
    assert audit["source_truth_protected_cue_indexes"] == [1]
    assert audit["findings"][0]["routed"] == "disclosure_protected"


def test_unproven_foreign_cue_does_not_defer_to_partial_source_truth():
    audit = _unproven_foreign_audit()

    pipeline._defer_unproven_foreign_introductions_to_late_authority(
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

    pipeline._defer_unproven_foreign_introductions_to_late_authority(
        audit,
        source_truth_windows=[(86_680, 102_020)],
        redelivery_baseline_config=_redelivery_baseline_config(),
    )

    assert audit["status"] == "DEFERRED_TO_REDELIVERY_BASELINE"


def test_partial_source_truth_can_defer_to_valid_v2_redelivery_baseline():
    audit = _unproven_foreign_audit()

    pipeline._defer_unproven_foreign_introductions_to_late_authority(
        audit,
        source_truth_windows=[(86_680, 102_020)],
        redelivery_baseline_config=_redelivery_baseline_config_v2(),
    )

    assert audit["status"] == "DEFERRED_TO_REDELIVERY_BASELINE"
