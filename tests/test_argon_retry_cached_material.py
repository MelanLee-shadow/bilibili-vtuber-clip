"""Local retry admission cannot hide newly available, validated cache evidence."""

import json
import pytest
from types import SimpleNamespace

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.final_review_auditor import (
    adjudicate_context_finding,
    build_context_adjudication_request,
)
from src.autoslice.acoustic_witness_adjudication import build_witness_request

pytest_plugins = ["tests.test_argon_frozen_native_cache"]


@pytest.mark.parametrize("valid_cache", [True, False])
def test_real_admission_replays_new_frozen_evidence_once_without_reset(
    scene, frozen_native, valid_cache
):
    root, _, native_calls = frozen_native
    scene.ordinary_status = "OBSERVED"
    scene.ordinary_choice = "CURRENT"
    scene.diagnosis = None
    out = scene.root / "retry-cache-consumer"
    out.mkdir()
    empty = scene.root / "empty-original-cache"
    empty.mkdir()

    def build(cache):
        return pipeline._build_entity_verification_context(
            spec={
                "date": "2026-10-16",
                "comparison_ordinary_cache_only": True,
                "comparison_witness_cache_root": str(cache),
                "argon_escalation": {"enabled": True},
            },
            padded=scene.source,
            padded_dur=6000,
            host="localhost",
            text_override_path=None,
            cid="synthetic-consumer",
            out_root=out,
            srt_text=scene.srt,
            authoritative_chat=[],
            adapters=SimpleNamespace(
                profile_asset_file=lambda _: scene.root / "absent",
                topic_graph_disabled=lambda: True,
            ),
        ).verify_confusable_entity

    plan = {
        "action": "reframe_question",
        "question": "distinguish target unit from its sentence",
        "why_previous_failed": "original unit not established",
        "expected_gain": "independent target audio",
        "verification": "compare bound target evidence without editing the original answers",
    }
    finding = {**scene.finding, "continuation_plan": plan}
    verifier = build(empty)
    request = build_context_adjudication_request(
        scene.srt,
        finding,
        clip_context=scene.context,
        source_media_timeline_offset_ms=1000,
        require_complete_utterance_support=True,
    )
    original = verifier.argon_escalation.ordinary_verifier(build_witness_request(request))
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder

    # Earlier model questions are dispatched via the genuine public recorder,
    # with actual validated audio bound first; no ledger row is invented.
    recorder = SemanticAttemptRecorder(
        srt_text=scene.srt, finding=finding, clip_context=scene.context, offset_ms=1000
    )
    recorder.sync_judge_evidence(check_request=request, witness=original)
    call = recorder.wrap(scene.judge)
    for question in (
        "historical unit question one",
        "historical unit question two",
        "historical unit question three",
    ):
        call(question)
    _, prior = adjudicate_context_finding(
        scene.srt,
        finding,
        entity_verifier=verifier,
        clip_context=scene.context,
        source_media_timeline_offset_ms=1000,
        judge_llm_call=scene.judge,
        require_complete_utterance_support=True,
    )
    assert (prior.get("witness_judge") or {}).get("judge", {}).get(
        "failure_layer"
    ) == "LOCAL_RETRY_ADMISSION", prior
    before = {
        p: p.read_bytes()
        for p in (scene.root / "runtime/cache/semantic-cpa-attempts").rglob("*.json")
    }
    assert before
    old_count = len(scene.prompts)
    if not valid_cache:
        for p in (root / "independent-context").glob("*/native-context/*.json"):
            value = json.loads(p.read_bytes())
            value["source_media_sha256"] = "f" * 64
            p.write_text(json.dumps(value))
    verifier = build(root)
    _, after = adjudicate_context_finding(
        scene.srt,
        finding,
        entity_verifier=verifier,
        clip_context=scene.context,
        source_media_timeline_offset_ms=1000,
        judge_llm_call=scene.judge,
        require_complete_utterance_support=True,
    )
    (scene.root / "NEW-CACHE-CONSUMER.json").write_text(json.dumps(after, ensure_ascii=False))
    if not valid_cache:
        assert len(scene.prompts) == old_count
        assert all(p.read_bytes() == raw for p, raw in before.items())
        assert len(native_calls) == 2 and not scene.provider.calls
        return
    assert len(scene.prompts) > old_count, after
    assert any("一百五十手" in p for p in scene.prompts[old_count:])
    assert len(native_calls) == 2 and not scene.provider.calls
    assert all(p.read_bytes() == raw for p, raw in before.items())
    count = len(scene.prompts)
    adjudicate_context_finding(
        scene.srt,
        finding,
        entity_verifier=build(root),
        clip_context=scene.context,
        source_media_timeline_offset_ms=1000,
        judge_llm_call=scene.judge,
        require_complete_utterance_support=True,
    )
    assert len(scene.prompts) == count
    assert len(native_calls) == 2 and not scene.provider.calls
