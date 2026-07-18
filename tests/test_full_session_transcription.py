import json

import scripts.free_asr_client as free_asr_client
import src.autoslice.full_session_transcription as transcription
import src.autoslice.llm_client as llm_client
from src.autoslice.full_session_transcription import (
    _agy_fidelity_witness,
    _agy_refinement_provenance,
)
from src.autoslice.source_context_executor import AgyExecutionResult


def test_direct_agy_refinement_is_an_independent_fidelity_witness():
    refined = "1\n00:00:00,000 --> 00:00:01,000\n李豆沙\n"
    result = AgyExecutionResult(
        provider="agy",
        model="Gemini 3.5 Flash (High)",
        agy_rc=0,
        provider_fallback_used=False,
        provider_request_id="agy-job-1",
    )

    assert _agy_fidelity_witness(refined, result) == refined
    assert _agy_refinement_provenance(
        result,
        refined_srt=refined,
    )["fidelity_witness_eligible"] is True


def test_api_fallback_refinement_cannot_witness_its_own_rewrite():
    refined = "1\n00:00:00,000 --> 00:00:01,000\n让礼墨线下叫kmx\n"
    result = AgyExecutionResult(
        provider="agy",
        model="Gemini 3.5 Flash (High)",
        agy_rc=0,
        provider_fallback_used=True,
        provider_request_id="agy-job-2:api_fb=1",
    )

    assert _agy_fidelity_witness(refined, result) is None
    provenance = _agy_refinement_provenance(result, refined_srt=refined)
    assert provenance["provider_fallback_used"] is True
    assert provenance["fidelity_witness_eligible"] is False
    assert provenance["refined_srt_sha256"]


def test_api_fallback_rewrite_is_rejected_by_aggregate_transcriber(
    tmp_path,
    monkeypatch,
):
    draft = (
        "1\n00:00:14,540 --> 00:00:17,740\n"
        "让刘莎线下叫停了时\n"
    )
    fallback_refined = (
        "1\n00:00:14,540 --> 00:00:17,740\n"
        "让礼墨线下叫kmx\n"
    )

    def fallback_runner(_media_path, _draft_path, output_path):
        output_path.write_text(fallback_refined, encoding="utf-8")
        return AgyExecutionResult(
            provider="agy",
            model="Gemini 3.5 Flash (High)",
            agy_rc=0,
            provider_fallback_used=True,
            provider_request_id="agy-job-3:api_fb=1",
        )

    monkeypatch.setattr(
        transcription,
        "_build_ssh_agy_runner",
        lambda *_args, **_kwargs: fallback_runner,
    )
    monkeypatch.setattr(
        transcription,
        "_cpa_reconcile_draft_cues",
        lambda *_args, **_kwargs: fallback_refined,
    )
    monkeypatch.setattr(
        transcription,
        "_cpa_pronoun_ta_pass",
        lambda corrected, **_kwargs: corrected,
    )
    monkeypatch.setattr(
        llm_client,
        "build_llm_call",
        lambda _config: lambda _prompt: "{}",
    )
    monkeypatch.setattr(
        free_asr_client,
        "extract_audio_mp3",
        lambda media_path: media_path,
    )
    monkeypatch.setattr(
        free_asr_client,
        "transcribe",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(free_asr_client, "to_srt", lambda _result: draft)

    transcriber = transcription._build_aggregate_asr_transcriber(
        host="free",
        correct="bcut_agy_cpa",
    )
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"media")

    corrected = transcriber(media)

    assert "让礼墨线下叫kmx" not in corrected
    assert "让刘莎线下叫停了时" in corrected
    provenance = json.loads(
        media.with_suffix(".agy_refined.manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert provenance["provider_fallback_used"] is True
    assert provenance["fidelity_witness_eligible"] is False
    fidelity = json.loads(
        media.with_suffix(".fidelity-audit.json").read_text(encoding="utf-8")
    )
    assert (
        fidelity["agy_refinement_provenance"]["fidelity_witness_eligible"]
        is False
    )
