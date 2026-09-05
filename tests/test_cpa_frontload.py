import json

import pytest

from src.autoslice import full_session_transcription as transcription
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.source_context_executor import AgyRunnerError


DRAFT = "1\n00:00:00,000 --> 00:00:01,000\n不是不是十五\n"


@pytest.mark.parametrize("reply", ["", "{}", '{"cues":null}', '{"cues":[]}',
    '{"cues":[{"n":true,"text":"错"}]}', '{"cues":[{"n":1,"text":null}]}',
    '{"cues":[{"n":2,"text":"错"}]}', '{"cues":[{"n":1,"text":""}]}'])
def test_cpa_missing_or_invalid_review_cannot_return_a_draft(reply):
    for call in (
        lambda: transcription._cpa_correct_draft_cues(
            DRAFT, danmaku_lines=[], cpa_llm_call=lambda _: reply),
        lambda: transcription._cpa_reconcile_draft_cues(
            DRAFT, DRAFT, danmaku_lines=[], cpa_llm_call=lambda _: reply),
    ):
        with pytest.raises(AgyRunnerError, match="complete usable"):
            call()


def test_zero_edit_is_a_complete_valid_review():
    out = transcription._cpa_correct_draft_cues(DRAFT, danmaku_lines=[],
        cpa_llm_call=lambda _: '{"cues":[{"n":1,"text":"不是不是十五"}]}')
    assert out == DRAFT
    pronoun = DRAFT.replace("不是不是十五", "TA说不是")
    assert transcription._cpa_pronoun_ta_pass(
        pronoun, cpa_llm_call=lambda _: '{"rewrites":[]}', required=True) == pronoun
    with pytest.raises(AgyRunnerError):
        transcription._cpa_pronoun_ta_pass(pronoun, cpa_llm_call=lambda _: '{}', required=True)


def test_frontload_is_idempotent_and_boundary_moves_are_lossless():
    raw = ("1\n00:00:00,000 --> 00:00:01,000\n我喜欢梦\n\n"
           "2\n00:00:01,000 --> 00:00:02,000\n限大不是不是十五\n")
    boundary, prepared, audit = transcription._prepare_cpa_draft(raw, ["梦限大"])
    original, moved = parse_srt_cues(raw), parse_srt_cues(boundary)
    assert audit["term_boundary_moves"]
    assert [(c.start_ms, c.end_ms) for c in original] == [(c.start_ms, c.end_ms) for c in moved]
    assert ''.join(c.text for c in original) == ''.join(c.text for c in moved)
    assert transcription._prepare_cpa_draft(prepared, ["梦限大"])[1] == prepared
    assert "不是不是十五" in prepared
    blocked, _, blocked_audit = transcription._prepare_cpa_draft(
        raw, ["梦限大"], blocked_boundaries=[0])
    assert blocked == raw
    assert blocked_audit["term_boundary_moves"] == []


def test_moss_route_keeps_raw_and_requires_cpa(tmp_path, monkeypatch):
    import scripts.free_asr_client as free
    from src.autoslice import llm_client, moss_transcription

    calls = []
    monkeypatch.setenv("AUTOSLICE_DISABLE_TOPIC_ENTITY_GRAPH", "1")
    monkeypatch.setattr(free, "extract_audio_mp3", lambda _: b"mp3")
    monkeypatch.setattr(free, "transcribe", lambda *a, **k: pytest.fail("MOSS success must not run BCUT"))
    monkeypatch.setattr(moss_transcription, "transcribe_moss", lambda audio, **k:
        (DRAFT, {"provider": "moss", "speaker_labels": ["S01"]}))
    monkeypatch.setattr(transcription, "_build_ssh_agy_runner", lambda *a, **k:
        lambda *a, **k: pytest.fail("MOSS success must not run full AGY"))
    def review(prompt):
        calls.append(prompt)
        return '{"cues":[{"n":1,"text":"不是不是十五"}]}'
    monkeypatch.setattr(llm_client, "build_llm_call", lambda _: review)
    media = tmp_path / "audio.mp4"
    media.write_bytes(b"fixture")
    transcribe = transcription._build_aggregate_asr_transcriber("localhost", correct="moss_cpa")
    assert transcribe(media) == DRAFT
    assert len(calls) == 1
    assert media.with_suffix(".asr_draft.srt").read_text() == DRAFT
    assert media.with_suffix(".cpa-reviewed.srt").read_text() == DRAFT
    assert json.loads(media.with_suffix(".asr-source.json").read_text())["effective_route"] == "moss_cpa"
    def unavailable(*args, **kwargs):
        raise moss_transcription.MossTranscriptionError("MOSS_TIMEOUT", "unavailable")
    monkeypatch.setattr(moss_transcription, "transcribe_moss", unavailable)
    monkeypatch.setattr(free, "transcribe", lambda *a, **k: DRAFT)
    monkeypatch.setattr(free, "to_srt", lambda result: result)
    attempts = []
    def legacy_refine(*args, **kwargs):
        attempts.append("AGY")
        return None, None
    monkeypatch.setattr(transcription, "_run_agy_refinement_attempt", legacy_refine)
    transcribe = transcription._build_aggregate_asr_transcriber("localhost", correct="moss_cpa")
    assert transcribe(media) == DRAFT
    assert attempts == ["AGY"]
    assert len(calls) == 2
    source = json.loads(media.with_suffix(".asr-source.json").read_text())
    assert source["effective_route"] == "bcut_agy_cpa"
    assert source["fallback_reason"] == "MOSS_TIMEOUT"


def test_runtime_default_cannot_disable_cpa(monkeypatch):
    from src.autoslice.producer_request import parse_producer_args
    monkeypatch.setenv("AUTOSLICE_CORRECTION_MODE", "none")
    with pytest.raises(SystemExit):
        parse_producer_args(["--spec", "unused.json"], description="test",
            speaker_display_name="test", default_speaker_mode="required")


def test_cpa_failure_kind_is_separate_from_text_quality():
    from src.autoslice.talk_lane import classify_talk_failure
    unavailable = classify_talk_failure("CPA_CORRECTION_UNAVAILABLE")
    invalid = classify_talk_failure("CPA_CORRECTION_INVALID_OUTPUT")
    assert unavailable["failure_kind"] == "provider_transient"
    assert invalid["failure_kind"] == "provider_contract"
