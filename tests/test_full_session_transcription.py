import hashlib
import json

import scripts.free_asr_client as free_asr_client
import src.autoslice.full_session_transcription as transcription
import src.autoslice.llm_client as llm_client
from src.autoslice.full_session_transcription import (
    _agy_corroborating_witness,
    _agy_fidelity_witness,
    _agy_refinement_provenance,
)
from src.autoslice.source_context_executor import AgyExecutionResult
from src.autoslice.source_context_executor import AgyChunkAttestation


def test_unbound_direct_agy_refinement_is_not_a_fidelity_witness():
    refined = "1\n00:00:00,000 --> 00:00:01,000\n主播\n"
    result = AgyExecutionResult(
        provider="agy",
        model="Gemini 3.6 Flash (High)",
        agy_rc=0,
        provider_fallback_used=False,
        provider_request_id="agy-job-1",
    )

    assert _agy_fidelity_witness(refined, result) is None
    assert _agy_refinement_provenance(
        result,
        refined_srt=refined,
    )["fidelity_witness_eligible"] is False


def test_hash_bound_direct_agy_is_an_independent_fidelity_witness(tmp_path):
    draft = "1\n00:00:00,000 --> 00:00:01,000\n主薄\n"
    refined = "1\n00:00:00,000 --> 00:00:01,000\n主播\n"
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"bound media")
    result = AgyExecutionResult(
        provider="agy",
        model="Gemini 3.6 Flash (High)",
        agy_rc=0,
        provider_fallback_used=False,
        provider_request_id="agy-job-bound-direct",
        requested_provider="agy",
        executed_provider="agy",
        source_media_sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
        draft_srt_sha256=hashlib.sha256(draft.encode()).hexdigest(),
        refined_srt_sha256=hashlib.sha256(refined.encode()).hexdigest(),
        timing_validated=True,
        audio_input_attested=True,
        chunk_count=1,
        agy_chunk_count=1,
        api_fallback_chunk_count=0,
        chunk_attestations=(
            AgyChunkAttestation(
                chunk_index=0,
                media_start_ms=0,
                media_end_ms=1_000,
                media_sha256="a" * 64,
                draft_srt_sha256="b" * 64,
                refined_srt_sha256="c" * 64,
                executed_provider="agy",
                timing_validated=True,
                audio_input_attested=True,
            ),
        ),
    )

    assert _agy_fidelity_witness(
        refined,
        result,
        draft_srt=draft,
        media_path=media,
    ) == refined
    provenance = _agy_refinement_provenance(
        result,
        refined_srt=refined,
        draft_srt=draft,
        media_path=media,
    )
    assert provenance["witness_tier"] == "independent_audio"
    assert provenance["fidelity_witness_eligible"] is True


def test_api_fallback_refinement_cannot_witness_its_own_rewrite():
    refined = "1\n00:00:00,000 --> 00:00:01,000\n让甲甲线下叫xyz\n"
    result = AgyExecutionResult(
        provider="agy",
        model="Gemini 3.6 Flash (High)",
        agy_rc=0,
        provider_fallback_used=True,
        provider_request_id="agy-job-2:api_fb=1",
    )

    assert _agy_fidelity_witness(refined, result) is None
    provenance = _agy_refinement_provenance(result, refined_srt=refined)
    assert provenance["provider_fallback_used"] is True
    assert provenance["fidelity_witness_eligible"] is False
    assert provenance["refined_srt_sha256"]


def test_incomplete_attestation_counts_fail_closed(tmp_path):
    draft = "1\n00:00:00,000 --> 00:00:01,000\n请问熊\n"
    refined = "1\n00:00:00,000 --> 00:00:01,000\nxyz\n"
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"bound media")
    result = AgyExecutionResult(
        provider="agy",
        agy_rc=0,
        provider_fallback_used=False,
        executed_provider="agy",
        source_media_sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
        draft_srt_sha256=hashlib.sha256(draft.encode()).hexdigest(),
        refined_srt_sha256=hashlib.sha256(refined.encode()).hexdigest(),
        timing_validated=True,
        audio_input_attested=True,
        chunk_count=1,
    )

    provenance = _agy_refinement_provenance(
        result,
        refined_srt=refined,
        draft_srt=draft,
        media_path=media,
    )

    assert provenance["attestation_bound"] is False
    assert provenance["fidelity_witness_eligible"] is False


def test_hash_bound_api_fallback_is_never_an_audio_witness(tmp_path):
    draft = "1\n00:00:00,000 --> 00:00:01,000\n请问熊\n"
    refined = "1\n00:00:00,000 --> 00:00:01,000\nxyz\n"
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"bound media")
    result = AgyExecutionResult(
        provider="agy",
        model="Gemini 3.6 Flash (Low)",
        agy_rc=0,
        provider_fallback_used=True,
        provider_request_id="agy-job-bound:api_fb=1",
        requested_provider="agy",
        executed_provider="gemini_api",
        source_media_sha256=hashlib.sha256(media.read_bytes()).hexdigest(),
        draft_srt_sha256=hashlib.sha256(draft.encode()).hexdigest(),
        refined_srt_sha256=hashlib.sha256(refined.encode()).hexdigest(),
        timing_validated=True,
        audio_input_attested=True,
        chunk_count=1,
        agy_chunk_count=0,
        api_fallback_chunk_count=1,
        chunk_attestations=(
            AgyChunkAttestation(
                chunk_index=0,
                media_start_ms=0,
                media_end_ms=1_000,
                media_sha256="a" * 64,
                draft_srt_sha256="b" * 64,
                refined_srt_sha256="c" * 64,
                executed_provider="gemini_api",
                timing_validated=True,
                audio_input_attested=True,
            ),
        ),
    )

    assert _agy_fidelity_witness(
        refined,
        result,
        draft_srt=draft,
        media_path=media,
    ) is None
    assert _agy_corroborating_witness(
        refined,
        result,
        draft_srt=draft,
        media_path=media,
    ) is None
    provenance = _agy_refinement_provenance(
        result,
        refined_srt=refined,
        draft_srt=draft,
        media_path=media,
    )
    assert provenance["witness_tier"] == "none"
    assert provenance["fidelity_witness_eligible"] is False
    assert provenance["corroborating_audio_eligible"] is False

    drifted = _agy_refinement_provenance(
        result,
        refined_srt=refined.replace("xyz", "甲甲"),
        draft_srt=draft,
        media_path=media,
    )
    assert drifted["corroborating_audio_eligible"] is False


def test_api_fallback_rewrite_is_rejected_by_aggregate_transcriber(
    tmp_path,
    monkeypatch,
):
    draft = (
        "1\n00:00:14,540 --> 00:00:17,740\n"
        "让刘薄线下叫停了时\n"
    )
    fallback_refined = (
        "1\n00:00:14,540 --> 00:00:17,740\n"
        "让甲甲线下叫xyz\n"
    )

    def fallback_runner(_media_path, _draft_path, output_path):
        output_path.write_text(fallback_refined, encoding="utf-8")
        return AgyExecutionResult(
            provider="agy",
            model="Gemini 3.6 Flash (High)",
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

    assert "让甲甲线下叫xyz" not in corrected
    assert "让刘薄线下叫停了时" in corrected
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


def test_failed_rerun_replaces_stale_agy_artifacts_with_v2_none_manifest(
    tmp_path,
    monkeypatch,
):
    draft = "1\n00:00:00,000 --> 00:00:01,000\n我这真的有一些题\n"

    def unavailable_runner(_media_path, _draft_path, _output_path):
        raise RuntimeError("current AGY attempt unavailable")

    monkeypatch.setattr(
        transcription,
        "_build_ssh_agy_runner",
        lambda *_args, **_kwargs: unavailable_runner,
    )
    monkeypatch.setattr(
        transcription,
        "_cpa_correct_draft_cues",
        lambda draft_srt, **_kwargs: draft_srt,
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
    media.write_bytes(b"current media")
    refined_path = media.with_suffix(".agy_refined.srt")
    manifest_path = media.with_suffix(".agy_refined.manifest.json")
    refined_path.write_text("stale refined text", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "agy-refinement-provenance.v1",
                "provider_request_id": "stale-run",
            }
        ),
        encoding="utf-8",
    )

    assert transcriber(media) == draft
    assert not refined_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "agy-refinement-provenance.v2"
    assert manifest["refinement_status"] == "UNAVAILABLE"
    assert manifest["failure_reason_code"] == "RuntimeError"
    assert manifest["witness_tier"] == "none"
    assert manifest["attestation_bound"] is False
    assert manifest["provider_request_id"] is None


def test_pronoun_pass_gets_its_own_low_effort_config_not_the_reconcile_one(
    monkeypatch,
):
    """_cpa_pronoun_ta_pass must no longer share cpa_llm_call
    (the dual-source BCUT+AGY reconcile config) with the reconcile stages.
    It gets its own command_template with effort dropped medium -> low, same
    approved model chain."""

    captured_configs: list = []

    def _capture(config):
        captured_configs.append(config)
        return lambda _prompt: "{}"

    monkeypatch.setattr(llm_client, "build_llm_call", _capture)
    monkeypatch.setattr(
        transcription,
        "_build_ssh_agy_runner",
        lambda *_args, **_kwargs: (lambda *_a, **_k: None),
    )

    transcription._build_aggregate_asr_transcriber(host="free", correct="bcut_agy_cpa")

    command_configs = [c for c in captured_configs if c.transport == "command"]
    assert len(command_configs) == 2

    reconcile_cfg, pronoun_cfg = command_configs
    assert "gpt-5.6-sol gpt-5.5 gpt-5.4" in reconcile_cfg.command_template
    assert reconcile_cfg.command_template.strip().endswith("medium")

    assert "gpt-5.6-sol gpt-5.5 gpt-5.4" in pronoun_cfg.command_template
    assert pronoun_cfg.command_template.strip().endswith("low")
    # Model chain must be byte-identical between the two configs; only the
    # trailing effort token differs.
    assert reconcile_cfg.command_template.replace("medium", "low") == (
        pronoun_cfg.command_template
    )
    assert pronoun_cfg.timeout_seconds == 600.0


def test_cpa_pronoun_ta_pass_still_validates_minimal_legal_rewrite():
    """Downgrading effort must not change the accepted completion shape."""

    srt = (
        "1\n00:00:00,000 --> 00:00:01,000\nTA说今天很开心\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n然后她走了\n"
    )

    def stub(_prompt: str) -> str:
        return json.dumps(
            {"rewrites": [{"n": 1, "occurrence": 1, "from": "TA", "to": "她"}]}
        )

    out = transcription._cpa_pronoun_ta_pass(srt, cpa_llm_call=stub)

    assert "她说今天很开心" in out
    assert "TA说今天很开心" not in out
