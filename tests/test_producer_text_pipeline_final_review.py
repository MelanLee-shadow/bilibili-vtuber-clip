import json

from src.autoslice import producer_text_pipeline as pipeline


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
        profile_asset_file=unused,
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=unused,
        topic_graph_expected_sha256=lambda: "",
    )


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
