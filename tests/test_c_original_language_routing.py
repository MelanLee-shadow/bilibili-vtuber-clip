"""Synthetic ordinary producer controls for foreign-target protocol routing."""
from copy import deepcopy
import json
import socket

import pytest

from src.autoslice.entity_audio_verifier import _prepare_audio_span
from src.autoslice.exact_source_transcript_contract import (
    build_exact_source_transcript_request, seal_exact_source_transcript_observation,
)
from src.autoslice.final_review_auditor import adjudicate_context_finding
from tests.test_span_phoneme_resolution import Observers, SOURCE_SHA, model_for, sha, srt


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Synthetic original-language controls cannot call providers")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("defect", [None, "source", "language", "clock"])
def test_observed_mandarin_is_not_an_original_language_observation(defect):
    # All content is synthetic; the public producer receives a genuine blind
    # pinyin-shaped result, which says nothing about an original Japanese form.
    native_text = "他说，他说，ミトコンドリアです"
    primary_receipts = []
    class BlindMandarin(Observers):
        def __call__(self, target):
            result = super().__call__(target)
            primary_receipts.append((result, deepcopy(result)))
            return result
    verifier = BlindMandarin(heard="ta shuo ta shuo mi tou kang duo li ya de si",
                             transcripts={"mai": native_text, "moss": native_text})
    calls = []
    def native(request):
        calls.append(deepcopy(request))
        assert not {"current_cue", "proposed_cue", "replacement", "candidate_entities"} & request.keys()
        physical = request
        if defect == "clock":
            physical = build_exact_source_transcript_request({
                **request, "source_media_timeline_offset_ms": request["source_media_timeline_offset_ms"] + 200})
        span = _prepare_audio_span(request=physical, context_mode=True,
            source_media_timeline_offset_ms=physical["source_media_timeline_offset_ms"],
            source_duration_ms=10000, witness_mode=True)
        observation = seal_exact_source_transcript_observation(
            request=physical, exact_transcript=native_text,
            audible_language="zh" if defect == "language" else "ja",
            source_media_sha256=SOURCE_SHA, audio_clip_sha256=sha(b"synthetic original language audio"),
            provider="agy", model="synthetic-original-language",
            response_sha256=sha("synthetic original language response"),
            timeline_binding=span.timeline_binding)
        if defect == "source":
            observation["source_media_sha256"] = "b" * 64
        return observation

    text = "他说，他说，米头康多里亚です"
    finding = {"cue_index": 1, "suspect": "米头康多里亚", "suggestion": "ミトコンドリア",
               "proposed_full_cue": native_text, "repair_class": "phonetic"}
    output, audit = adjudicate_context_finding(
        srt(text), finding, entity_verifier=verifier, exact_source_transcript_provider=native,
        judge_llm_call=model_for(target_quote="ミトコンドリア", blind_quote=None, question_span=[0, 5]),
        require_complete_utterance_support=True)
    assert len(calls) == 1
    recovery = audit["primary_protocol_recovery"]
    assert recovery["prior_primary_status"] == "OBSERVED"
    assert recovery["authority"] == "EVIDENCE_ONLY" and recovery["mutation_authorized"] is False
    # The actual primary response remains blind pinyin; no success flag or
    # invented Japanese is written into that immutable observation.
    assert primary_receipts and all(after == before for after, before in primary_receipts)
    assert all(after["witness_protocol"] == "blind_pinyin" for after, _before in primary_receipts)
    if defect is None:
        assert output == srt(native_text)
        assert audit["repaired"] is True and audit["policy_branch"] == "CONTEXT_PHONEME_FIT"
        assert audit["span_context_resolution"]["outside_findings"]
        assert "ミトコンドリア" in json.dumps(recovery["observation"], ensure_ascii=False)
    else:
        assert output == srt(text) and audit["repaired"] is False


@pytest.mark.parametrize("complete, japanese", [(True, False), (False, True)])
def test_original_language_addition_does_not_dispatch_outside_its_existing_scope(complete, japanese):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from src.autoslice.final_review_witness_dispatch import original_language_scope_evidence
    text = "这是米头康多里亚です" if japanese else "两个电阻要并联"
    finding = {"cue_index": 1, "suspect": "米头康多里亚" if japanese else "并联",
               "suggestion": "ミトコンドリア" if japanese else "串联", "repair_class": "phonetic"}
    finding["proposed_full_cue"] = text.replace(finding["suspect"], finding["suggestion"])
    request = build_context_adjudication_request(srt(text), finding,
                    require_complete_utterance_support=complete)
    witness = {"status": "OBSERVED", "witness_protocol": "blind_pinyin"}
    def forbidden(_request):
        raise AssertionError("This branch has no new original-language dispatch")
    rebound, audit = original_language_scope_evidence(request, witness, forbidden)
    assert rebound == request and audit is None
    assert witness == {"status": "OBSERVED", "witness_protocol": "blind_pinyin"}
