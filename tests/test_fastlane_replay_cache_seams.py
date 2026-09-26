"""The real replay builder must preserve zero-provider cache seams."""

import json
from types import SimpleNamespace

from src.autoslice import reviewed_baseline_replay as replay
from src.autoslice.acoustic_witness_adjudication import judge_word_choice


def test_actual_replay_consumer_keeps_model_cache_and_object_method_probes(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    cid = "auto_fixture"
    monkeypatch.setattr(replay, "_reconstruct_structured_chat", lambda *_a, **_k: ())
    monkeypatch.setattr(
        "src.autoslice.delivery_fast_path.resolve_operator_text_full_ownership",
        lambda _: {"schema_version": "operator-text-full-ownership.v1"},
    )
    monkeypatch.setattr(
        "src.autoslice.delivery_fast_path.skipped_final_review_audit", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        "src.autoslice.review_package_boundary_validators.semantic_boundary_review_is_valid",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        "src.autoslice.producer_boundary_review_stage.exact_delivery_correction_audit",
        lambda **_k: {},
    )
    monkeypatch.setattr(
        "src.autoslice.delivery_fast_path.discover_priority_findings", lambda *_a, **_k: ([], {})
    )
    monkeypatch.setattr(
        "src.autoslice.producer_text_pipeline._final_review_structured_context", lambda **_k: {}
    )
    monkeypatch.setattr("src.autoslice.clip_context.clip_context_prompt_text", lambda _: "context")
    attempts = []
    cache_reads = []
    model = {"models": ["gpt-6-sol"], "effort": "medium"}

    class Verifier:
        def __call__(self, request):
            return {"actual_provider": True}

        def probe_witness_cache(self, request):
            cache_reads.append("witness")
            return {"served_from_cache": True}

        def probe_exact_source_transcript_cache(self, request):
            cache_reads.append("exact")
            return {"served_from_cache": True}

        def exact_source_transcript(self, request):
            return {"fresh_exact": True}

    def judge(_prompt):
        return json.dumps({"choice": "CURRENT", "needs_audio": False})

    judge.cpa_cache_identity = model

    def consume(**kwargs):
        entity = kwargs["verify_confusable_entity"]
        final = kwargs["final_review_llm"]
        assert final.cpa_cache_identity == model
        assert entity.probe_witness_cache({}) == {"served_from_cache": True}
        assert entity.probe_exact_source_transcript_cache({}) == {"served_from_cache": True}
        assert attempts == []
        request = {"current_cue": "原句", "proposed_cue": "候选", "request_sha256": "a" * 64}
        witness = {"status": "UNCERTAIN", "reason_code": "CPA_TEXT_FIRST_NOT_REQUESTED"}
        one = judge_word_choice(llm_call=final, check_request=request, witness=witness)
        two = judge_word_choice(llm_call=final, check_request=request, witness=witness)
        assert one["choice"] == two["choice"] == "CURRENT"
        assert two["served_from_cache"] is True
        assert attempts == ["provider"]
        assert entity.exact_source_transcript({}) == {"fresh_exact": True}
        assert attempts == ["provider", "provider"]
        return {"status": "TEST_CONSUMER_OK"}

    monkeypatch.setattr(
        "src.autoslice.producer_text_pipeline._run_exact_final_release_review", consume
    )
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"synthetic input, no decoding performed in this test")
    reviewer = replay.replay_exact_final_reviewer(
        SimpleNamespace(candidate_id=cid, date="2026-09-09"),
        spec={
            "pieces": [{"source_media_sha256": "sha256:" + "1" * 64}],
            "selection_hook": "hook",
            "selection_scorecard": {},
            "boundary_semantic_review": {"candidate_id": cid},
        },
        clip_context={},
        runtime_root=tmp_path,
        out_root=tmp_path,
        padded=padded,
        verify_confusable_entity=Verifier(),
        screen_read_probe=lambda *_a: {},
        boundary_llm=judge,
        final_llm=judge,
        pronoun_llm=judge,
        provider_invocation=lambda: attempts.append("provider"),
    )
    assert reviewer("1\n00:00:00,000 --> 00:00:01,000\n原句\n", {}, 0, 1000) == {
        "status": "TEST_CONSUMER_OK"
    }
    assert cache_reads == ["witness", "exact"]
