"""Trace the ordinary pronoun pass without changing its existing decisions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import full_session_transcription as tx, llm_client
from src.autoslice.source_context_executor import AgyRunnerError

SRT = "1\n00:00:00,000 --> 00:00:01,000\nTA说谢谢\n"
KEEP = '{"rewrites":[]}'
CHANGE = '{"rewrites":[{"n":1,"occurrence":1,"from":"TA","to":"她"}]}'


def _traces(media: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(media.with_suffix(".pronoun-trace").glob("*.json"))]


def test_aggregate_records_each_pronoun_run_even_when_upstream_cache_hits(tmp_path, monkeypatch):
    """A cache hit must not conceal the independently repeated final text pass."""
    import scripts.free_asr_client as free

    monkeypatch.setenv("AUTOSLICE_DISABLE_TOPIC_ENTITY_GRAPH", "1")
    monkeypatch.setattr(free, "extract_audio_mp3", lambda _: b"synthetic audio")
    asr_calls, cpa_calls, pronoun_calls, builders = [], [], [], []

    def asr(*_args, **_kwargs):
        asr_calls.append(True)
        return {"provider": "bcut", "utterances": [
            {"start_time": 0, "end_time": 1000, "transcript": "TA说谢谢", "words": []}
        ]}

    monkeypatch.setattr(free, "transcribe", asr)

    def builder(config):
        stage = len(builders)
        builders.append(config)

        def call(prompt):
            if stage == 0:
                cpa_calls.append(prompt)
                return '{"cues":[{"n":1,"text":"TA说谢谢"}]}'
            pronoun_calls.append(prompt)
            return CHANGE if len(pronoun_calls) == 1 else KEEP

        call.cpa_cache_identity = {"transport": "cpa_command", "models": ["gpt-6-sol"], "effort": "low"}
        return call

    monkeypatch.setattr(llm_client, "build_llm_call", builder)
    media = tmp_path.resolve() / "input.mp4"
    media.write_bytes(b"synthetic media")
    run = tx._build_aggregate_asr_transcriber("localhost", correct="cpa")
    cold, warm = run(media), run(media)
    assert cold != warm  # The diagnostic must not hide the legitimate changed response.
    assert len(asr_calls) == len(cpa_calls) == 1
    assert len(pronoun_calls) == 2
    records = _traces(media)
    assert len(records) == 2
    assert {r["output_srt"]["text"] for r in records} == {cold, warm}
    for record in records:
        assert record["outcome"] == "RETURNED"
        assert record["release_authorized"] is False
        assert record["served_from_cache"] is False
        assert len(record["calls"]) == 1
        assert record["model_identity"]["models"] == ["gpt-6-sol"]
        assert record["input_srt"]["sha256"] == hashlib.sha256(SRT.encode()).hexdigest()
        assert record["calls"][0]["prompt"]["text"] == pronoun_calls[0]
        assert record["calls"][0]["completion"]["text"] in {KEEP, CHANGE}
        assert record["calls"][0]["outcome"] == "RETURNED"
        assert record["output_srt"]["sha256"] == hashlib.sha256(record["output_srt"]["text"].encode()).hexdigest()
    assert json.loads(media.with_suffix(".transcription-reuse.json").read_text())["cpa"]["status"] == "HIT"


def _run(media, callback, srt=SRT, required=True, runner=None):
    from src.autoslice.pronoun_stage_trace import trace_pronoun_pass

    return trace_pronoun_pass(srt, media_path=media, llm_call=callback,
                              run=runner or tx._cpa_pronoun_ta_pass, required=required)


def test_no_pronouns_preserves_bytes_and_records_zero_calls(tmp_path):
    media = tmp_path.resolve() / "no-pronouns.mp4"
    original = SRT.replace("TA说谢谢", "今天晴天")
    assert _run(media, lambda _: pytest.fail("no provider needed"), original) == original
    record = _traces(media)[0]
    assert record["calls"] == []
    assert record["input_srt"] == record["output_srt"]


def test_retries_keep_invalid_completion_and_valid_response(tmp_path):
    media = tmp_path.resolve() / "retry.mp4"
    replies = iter(["not JSON", CHANGE])
    expected = tx._cpa_pronoun_ta_pass(SRT, cpa_llm_call=lambda _: CHANGE, required=True)
    assert _run(media, lambda _: next(replies)) == expected
    record = _traces(media)[0]
    assert [r["completion"]["text"] for r in record["calls"]] == ["not JSON", CHANGE]
    assert record["outcome"] == "RETURNED"
    assert record["required"] is True


def test_transport_failure_keeps_original_exception_and_no_error_message(tmp_path):
    media = tmp_path.resolve() / "failed.mp4"

    def fail(_prompt):
        raise llm_client.LlmCallError("sensitive error body not for persistence")

    with pytest.raises(AgyRunnerError) as exc:
        _run(media, fail)
    assert exc.value.reason_code == "CPA_PRONOUN_UNAVAILABLE"
    record = _traces(media)[0]
    assert record["outcome"] == "RAISED"
    assert len(record["calls"]) == 3
    assert all(r["exception_type"] == "LlmCallError" for r in record["calls"])
    assert "sensitive error body" not in json.dumps(record)
    assert "output_srt" not in record


def test_invalid_occurrence_still_blocks_and_preserves_response(tmp_path):
    media = tmp_path.resolve() / "invalid.mp4"
    reply = CHANGE.replace('"occurrence":1', '"occurrence":0')
    with pytest.raises(AgyRunnerError):
        _run(media, lambda _: reply)
    record = _traces(media)[0]
    assert len(record["calls"]) == 1
    assert record["calls"][0]["completion"]["text"] == reply
    assert record["outcome"] == "RAISED"


def test_each_run_is_private_and_never_overwrites_prior_trace(tmp_path):
    media = tmp_path.resolve() / "repeat.mp4"
    _run(media, lambda _: KEEP)
    folder = media.with_suffix(".pronoun-trace")
    before = {p: p.read_bytes() for p in folder.iterdir()}
    _run(media, lambda _: CHANGE)
    assert len(_traces(media)) == 2
    assert all(p.read_bytes() == data for p, data in before.items())
    assert folder.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in folder.iterdir())


def test_symlink_sink_is_not_followed_or_promoted_to_a_quality_failure(tmp_path, caplog):
    media = tmp_path.resolve() / "unsafe.mp4"
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    media.with_suffix(".pronoun-trace").symlink_to(outside, target_is_directory=True)
    assert _run(media, lambda _: KEEP) == SRT
    assert list(outside.iterdir()) == []
    assert "PRONOUN_STAGE_TRACE_UNAVAILABLE" in caplog.text


def test_credential_echo_is_not_persisted_or_hashed(tmp_path, monkeypatch):
    secret = "test-only-do-not-persist-credential-0123456789"
    monkeypatch.setenv("CPA_API_KEY", secret)
    media = tmp_path.resolve() / "redacted.mp4"
    reply = json.dumps({"rewrites": [], "note": secret})
    assert _run(media, lambda _: reply) == SRT
    record = _traces(media)[0]
    assert record["calls"][0]["completion"] == {"retention": "OMITTED_CREDENTIAL_ECHO"}
    encoded = json.dumps(record)
    assert secret not in encoded
    assert hashlib.sha256(reply.encode()).hexdigest() not in encoded
    assert hashlib.sha256(secret.encode()).hexdigest() not in encoded


def test_sink_failure_does_not_change_valid_return_or_mask_original_error(tmp_path, monkeypatch, caplog):
    from src.autoslice import pronoun_stage_trace as trace

    monkeypatch.setattr(trace, "_write", lambda *_args, **_kwargs: False)
    assert _run(tmp_path.resolve() / "unwritable.mp4", lambda _: KEEP) == SRT
    with pytest.raises(AgyRunnerError):
        _run(tmp_path.resolve() / "unwritable-failed.mp4", lambda _: "{}")
    assert "PRONOUN_STAGE_TRACE_UNAVAILABLE" in caplog.text


def test_checkpoint_exists_before_provider_and_after_response_before_runner_failure(tmp_path):
    media = tmp_path.resolve() / "checkpoint.mp4"

    def provider(_prompt):
        record = _traces(media)[0]
        assert record["outcome"] == "RUNNING"
        assert record["calls"][0]["outcome"] == "STARTED"
        return KEEP

    def interrupted(srt, *, cpa_llm_call, required):
        cpa_llm_call("synthetic prompt")
        record = _traces(media)[0]
        assert record["calls"][0]["completion"]["text"] == KEEP
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        _run(media, provider, runner=interrupted)
    record = _traces(media)[0]
    assert record["outcome"] == "RAISED"
    assert record["exception_type"] == "KeyboardInterrupt"


def test_existing_nonprivate_directory_is_rejected_without_chmod(tmp_path, caplog):
    media = tmp_path.resolve() / "public-dir.mp4"
    folder = media.with_suffix(".pronoun-trace")
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    assert _run(media, lambda _: KEEP) == SRT
    assert list(folder.iterdir()) == []
    assert folder.stat().st_mode & 0o777 == 0o755
    assert "PRONOUN_STAGE_TRACE_UNAVAILABLE" in caplog.text


def test_trace_reports_requested_configuration_not_attested_model_or_endpoint(tmp_path):
    media = tmp_path.resolve() / "identity.mp4"

    def provider(_prompt):
        return KEEP

    provider.cpa_cache_identity = {
        "transport": "cpa_command", "models": ["gpt-6-sol"], "effort": "low",
        "endpoint": "https://private.invalid", "child_env": {"key": "never-copy"},
    }
    assert _run(media, provider) == SRT
    record = _traces(media)[0]
    assert record["model_identity_source"] == "CALLABLE_DECLARED_REQUEST_CONFIGURATION"
    assert record["backend_identity_verified"] is False
    assert set(record["model_identity"]) == {"transport", "models", "effort"}
    assert "private.invalid" not in json.dumps(record)
    assert "never-copy" not in json.dumps(record)


def test_oversized_and_invalid_unicode_return_values_are_only_diagnostic_omissions(tmp_path):
    # The wrapper may not turn an unusual return into a different program result.
    for suffix, result, retention in (
        ("large", "x" * 300_000, "OMITTED_SIZE"),
        ("unicode", "\ud800", "OMITTED_INVALID_UNICODE"),
    ):
        media = tmp_path.resolve() / f"{suffix}.mp4"

        def unchanged(_srt, *, cpa_llm_call, required):
            return cpa_llm_call("synthetic prompt")

        assert _run(media, lambda _: result, runner=unchanged) == result
        record = _traces(media)[0]
        assert record["output_srt"]["retention"] == retention
        assert record["calls"][0]["completion"]["retention"] == retention
        assert "text" not in record["output_srt"]


def test_checkpoint_storage_exception_does_not_mask_return(tmp_path, monkeypatch, caplog):
    from src.autoslice import pronoun_stage_trace as trace

    real_write = trace._write

    def fail_checkpoint(path, payload, *, replace=False):
        if replace:
            raise OSError("private storage error not for logs")
        return real_write(path, payload)

    monkeypatch.setattr(trace, "_write", fail_checkpoint)
    assert _run(tmp_path.resolve() / "checkpoint-error.mp4", lambda _: KEEP) == SRT
    assert "PRONOUN_STAGE_TRACE_UNAVAILABLE" in caplog.text
    assert "private storage error" not in caplog.text
