"""Range screen context must survive the actual exact-review dispatcher."""

import hashlib

from src.autoslice import final_review_auditor as auditor
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice import screen_read_witness as screen
from tests.test_producer_text_pipeline_final_review import (
    _adapters,
    _correction_pass,
    _legacy_pending_audio_authority,
    _srt,
)


def test_whole_exact_review_forwards_range_screen_to_normal_adjudicator(monkeypatch, tmp_path):
    _, _, context, _ = _legacy_pending_audio_authority(tmp_path)
    srt = _srt("观众这句话是什么意思")
    cue = pipeline.parse_srt_cues(srt)[0]
    observation = {"cue_index": 1, "immutable_receipt_sha256": "a" * 64}
    authority = {"final_review_audit": {
        "release_gate": "BLOCK",
        "reviewed_srt_sha256": "sha256:" + hashlib.sha256(srt.encode()).hexdigest(),
        "findings": [{
            "cue_index": 1, "repair_class": "phonetic", "why": "unresolved",
            "base_text_sha256": hashlib.sha256(cue.text.encode()).hexdigest(),
            "exact_release_adjudication": {"request": {
                "current_cue": cue.text, "matched_start_ms": cue.start_ms,
                "matched_end_ms": cue.end_ms,
            }},
        }],
    }}
    monkeypatch.setattr(pipeline, "discover_candidate_pronoun_findings", lambda *_a, **_k: ([], {}))
    seen = []

    def adjudicate(_srt, findings, **kwargs):
        seen.append(kwargs["prior_range_screen_context_observations"])
        return list(findings), []

    monkeypatch.setattr(pipeline, "adjudicate_exact_release_findings", adjudicate)
    monkeypatch.setattr(pipeline, "converge_reconsidered_exact_final_findings",
                        lambda _srt, findings, **_k: (list(findings), []))
    receipt = pipeline._run_exact_final_release_review(
        srt_text=srt, correction_audit=_correction_pass(), adapters=_adapters(),
        authoritative_chat=(), selection_hook="", clip_context=context,
        verified_authority_audit=authority,
        final_review_llm=lambda _prompt: '{"findings": []}',
        prior_range_screen_context_observations=(observation,),
    )
    assert seen == [(observation,)]
    assert receipt["release_gate"] == "BLOCK"


def test_screen_context_requires_ordinary_validated_audio_range(monkeypatch):
    srt = _srt("观众这句话是什么意思")
    evidence = {"cue_index": 1}
    finding = {"cue_index": 1, "suspect": "这句话", "suggestion": "这条弹幕",
               "repair_class": "phonetic",
               "_prior_range_screen_context_observations": [evidence],
               "_prior_range_acoustic_observations": [{"original": "receipt"}]}
    validated_audio = {"authority": "EVIDENCE_ONLY", "audio": "validated"}
    normalized_screen = {"scope": "RANGE_SOURCE_SCREEN_CONTEXT_ONLY",
                         "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
                         "same_cue_direct_transcript_authority": False}
    calls = []

    def validate(_srt, request, audio_range, observation):
        assert audio_range is validated_audio and observation is evidence
        calls.append(request["cue_indexes"])
        return normalized_screen, "RANGE_SCREEN_CONTEXT_VALIDATED_EVIDENCE_ONLY"

    monkeypatch.setattr(screen, "validate_range_screen_context_observation", validate)
    monkeypatch.setattr(auditor, "validate_range_acoustic_observation",
                        lambda *_a: (validated_audio, None))
    request = auditor.build_context_adjudication_request(srt, finding)
    assert calls == [[1]]
    assert request["context_only_screen_observations"] == [normalized_screen]
    assert request["candidate_provenance"]["screen_context_authority"] == "EVIDENCE_ONLY"
    assert request["orthography_authority"]["status"] != "PASS"
    monkeypatch.setattr(auditor, "validate_range_acoustic_observation",
                        lambda *_a: (None, "RANGE_ACOUSTIC_SOURCE_HASH_DRIFT"))
    rejected = auditor.build_context_adjudication_request(srt, finding)
    assert len(calls) == 1
    assert "context_only_screen_observations" not in rejected
    assert rejected["screen_context_observation_rejections"] == [
        "RANGE_SCREEN_CONTEXT_VALIDATED_AUDIO_RANGE_MISSING"
    ]
