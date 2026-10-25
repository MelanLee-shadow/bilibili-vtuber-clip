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


def test_aggregate_replays_successful_pronoun_after_cold_rebuild(tmp_path, monkeypatch):
    """The normal entry revalidates the saved answer without resampling it."""
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
        stage = len(builders) % 2
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
    cold = run(media)
    warm = tx._build_aggregate_asr_transcriber("localhost", correct="cpa")(media)
    assert cold == warm
    assert len(asr_calls) == len(cpa_calls) == 1
    assert len(pronoun_calls) == 1
    records = _traces(media)
    assert len(records) == 2
    assert {r["output_srt"]["text"] for r in records} == {cold, warm}
    for record in records:
        assert record["outcome"] == "RETURNED"
        assert record["release_authorized"] is False
        assert record["model_identity"]["models"] == ["gpt-6-sol"]
        assert record["input_srt"]["sha256"] == hashlib.sha256(SRT.encode()).hexdigest()
        if record["served_from_cache"]:
            assert record["calls"] == []
            assert record["cache"]["provider_calls"] == 0
        else:
            assert record["calls"][0]["prompt"]["text"] == pronoun_calls[0]
            assert record["calls"][0]["completion"]["text"] == CHANGE
            assert record["calls"][0]["outcome"] == "RETURNED"
        assert record["output_srt"]["sha256"] == hashlib.sha256(record["output_srt"]["text"].encode()).hexdigest()
    receipt = json.loads(media.with_suffix(".transcription-reuse.json").read_text())
    assert receipt["cpa"]["status"] == receipt["pronoun"]["status"] == "HIT"


def _run(media, callback, srt=SRT, required=True, runner=None, *, resume=None, context_text=""):
    from src.autoslice.pronoun_stage_trace import trace_pronoun_pass

    return trace_pronoun_pass(srt, media_path=media, llm_call=callback,
                              run=runner or tx._cpa_pronoun_ta_pass, required=required,
                              resume=resume, context_text=context_text)


def _cached_run(media, callback, **kwargs):
    from src.autoslice.transcription_stage_cache import TranscriptionStageCache

    cache = TranscriptionStageCache(media, b"stable fixture audio")
    return _run(media, callback, resume=cache, **kwargs), cache


def _identified(callback, *, model="gpt-6-sol", effort="low"):
    callback.cpa_cache_identity = {"transport": "cpa_command", "models": [model], "effort": effort}
    return callback


def test_completed_pronoun_survives_interrupt_before_outer_checkpoint(tmp_path, monkeypatch):
    from src.autoslice import transcription_stage_cache as stage

    media = tmp_path.resolve() / "interrupted.mp4"
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return CHANGE

    _identified(provider)
    original = stage.TranscriptionStageCache._record

    def interrupt_after_success(self, name, **fields):
        if name == "pronoun" and fields.get("persisted"):
            raise KeyboardInterrupt()
        return original(self, name, **fields)

    monkeypatch.setattr(stage.TranscriptionStageCache, "_record", interrupt_after_success)
    with pytest.raises(KeyboardInterrupt):
        _cached_run(media, provider)
    original_trace = {p: p.read_bytes() for p in media.with_suffix(".pronoun-trace").iterdir()}
    assert _traces(media)[0]["outcome"] == "RAISED"
    monkeypatch.setattr(stage.TranscriptionStageCache, "_record", original)
    output, cache = _cached_run(media, provider)
    assert "她说谢谢" in output
    assert len(calls) == 1
    assert cache.audit["pronoun"]["provider_calls"] == 0
    assert all(p.read_bytes() == raw for p, raw in original_trace.items())


@pytest.mark.parametrize("change", ["body", "context", "model", "effort", "endpoint", "policy", "parser"])
def test_pronoun_cache_invalidates_actual_semantic_dependencies(tmp_path, monkeypatch, change):
    from src.autoslice import transcription_stage_cache as stage

    media = tmp_path.resolve() / "drift.mp4"
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return KEEP

    _identified(provider)
    _cached_run(media, provider)
    options = {}
    if change == "body":
        options["srt"] = SRT.replace("谢谢", "再见")
    elif change == "context":
        options["context_text"] = "当前全文指代的是另一位嘉宾"
    elif change == "model":
        _identified(provider, model="different-declared-model")
    elif change == "effort":
        _identified(provider, effort="medium")
    elif change == "endpoint":
        monkeypatch.setenv("CPA_BASE_URL", "https://fixture.invalid/v2")
    elif change == "policy":
        original = stage.inspect.getsource
        monkeypatch.setattr(stage.inspect, "getsource", lambda function:
                            original(function) + "\n# changed policy implementation")
    else:
        original = stage._file_sha
        monkeypatch.setattr(stage, "_file_sha", lambda p:
                            "f" * 64 if p.name == "jingting_chunker.py" else original(p))
    _, cache = _cached_run(media, provider, **options)
    assert len(calls) == 2
    assert cache.audit["pronoun"]["served_from_cache"] is False


@pytest.mark.parametrize("identity", [None, {"transport": "cpa_command", "models": [], "effort": "low"}])
def test_unknown_pronoun_identity_cannot_replay(tmp_path, identity):
    media = tmp_path.resolve() / "unknown.mp4"
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return KEEP

    provider.cpa_cache_identity = identity
    _cached_run(media, provider)
    _, cache = _cached_run(media, provider)
    assert len(calls) == 2
    assert cache.audit["pronoun"]["status"] == "DISABLED"


@pytest.mark.parametrize("bad_payload", [
    CHANGE.replace('"occurrence":1', '"occurrence":0'), "not JSON",
    '{"rewrites":"invalid-list"}',
    '{"rewrites":[],"note":"cache-test-credential-echo"}',
])
def test_cached_raw_answer_is_revalidated_by_current_occurrence_gate(tmp_path, monkeypatch, bad_payload):
    from src.autoslice import transcription_stage_cache as stage
    from src.autoslice.transcription_stage_cache import _digest

    monkeypatch.setenv("CPA_API_KEY", "cache-test-credential-echo")
    media = tmp_path.resolve() / "bad-occurrence.mp4"
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return CHANGE

    _identified(provider)
    _cached_run(media, provider)
    path = next((tmp_path / ".transcription-stage-cache/pronoun").glob("*.json"))
    entry = json.loads(path.read_text())
    entry["payload"] = bad_payload
    entry["payload_sha256"] = _digest(entry["payload"])
    path.write_text(json.dumps(entry))
    corrupted = path.read_bytes()

    def guarded_digest(value):
        if "cache-test-credential-echo" in bad_payload:
            assert value != bad_payload  # Retention must reject before hashing.
        return _digest(value)

    monkeypatch.setattr(stage, "_digest", guarded_digest)
    output, cache = _cached_run(media, provider)
    assert "她说谢谢" in output
    assert len(calls) == 2
    assert cache.audit["pronoun"]["status"] == "INVALID_PRESERVED"
    assert path.read_bytes() == corrupted


@pytest.mark.parametrize("reply,calls,reason", [
    (CHANGE.replace('"occurrence":1', '"occurrence":0'), 1, "CPA_PRONOUN_INVALID_OUTPUT"),
    ("not JSON", 3, "CPA_PRONOUN_INVALID_OUTPUT"),
    (llm_client.LlmCallError("fixture transport"), 3, "CPA_PRONOUN_UNAVAILABLE"),
])
def test_cache_does_not_change_required_failure_or_retry_count(tmp_path, reply, calls, reason):
    media = tmp_path.resolve() / "failed-cache.mp4"
    attempts = []

    def provider(prompt):
        attempts.append(prompt)
        if isinstance(reply, Exception):
            raise reply
        return reply

    _identified(provider)
    with pytest.raises(AgyRunnerError) as exc:
        _cached_run(media, provider)
    assert exc.value.reason_code == reason
    assert len(attempts) == calls
    assert not list((tmp_path / ".transcription-stage-cache/pronoun").glob("*.json"))
    assert len(_traces(media)[0]["calls"]) == calls


def test_unretained_completion_is_neither_cached_nor_hashed(tmp_path, monkeypatch):
    secret = "test-pronoun-cache-credential-echo"
    monkeypatch.setenv("CPA_API_KEY", secret)
    media = tmp_path.resolve() / "echo-cache.mp4"
    reply = json.dumps({"rewrites": [], "note": secret})
    callback = _identified(lambda _: reply)
    _, cache = _cached_run(media, callback)
    assert cache.audit["pronoun"]["persisted"] is False
    assert cache.audit["pronoun"]["payload_sha256"] is None
    raw = "\n".join(p.read_text() for p in tmp_path.rglob("*.json"))
    assert secret not in raw
    assert hashlib.sha256(reply.encode()).hexdigest() not in raw
    assert not list((tmp_path / ".transcription-stage-cache/pronoun").glob("*.json"))


def test_oversized_valid_completion_cannot_be_replayed(tmp_path):
    media = tmp_path.resolve() / "oversized-cache.mp4"
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return json.dumps({"rewrites": [], "note": "x" * 256_001})

    _identified(provider)
    _cached_run(media, provider)
    _, cache = _cached_run(media, provider)
    assert len(calls) == 2
    assert cache.audit["pronoun"]["persisted"] is False
    assert not list((tmp_path / ".transcription-stage-cache/pronoun").glob("*.json"))
    assert _traces(media)[0]["calls"][0]["completion"]["retention"] == "OMITTED_SIZE"


def test_pronoun_cache_storage_failure_preserves_return_and_existing_history(tmp_path, monkeypatch):
    from src.autoslice import transcription_stage_cache as stage

    media = tmp_path.resolve() / "storage.mp4"
    real_write = stage._write
    monkeypatch.setattr(stage, "_write", lambda path, *args, **kwargs:
                        False if path.parent.name == "pronoun" else real_write(path, *args, **kwargs))
    output, cache = _cached_run(media, _identified(lambda _: CHANGE))
    assert "她说谢谢" in output
    assert cache.audit["pronoun"]["persisted"] is False
    assert _traces(media)[0]["outcome"] == "RETURNED"
    trace_dir = media.with_suffix(".pronoun-trace")
    original_records = {p.name: p.read_bytes() for p in trace_dir.glob("*.json")}
    monkeypatch.setattr(stage, "_write", lambda *_args, **_kwargs: False)
    attempts = []

    def failed_provider(prompt):
        attempts.append(prompt)
        raise llm_client.LlmCallError("fixture transport")

    with pytest.raises(AgyRunnerError) as exc:
        _cached_run(media, _identified(failed_provider))
    assert exc.value.reason_code == "CPA_PRONOUN_UNAVAILABLE"
    assert len(attempts) == 3
    current_records = {p.name: p.read_bytes() for p in trace_dir.glob("*.json")}
    added = current_records.keys() - original_records.keys()
    assert len(added) == 1
    assert json.loads(current_records[added.pop()])["outcome"] == "RAISED"
    assert all(current_records[name] == content for name, content in original_records.items())


def test_interrupted_atomic_cache_write_never_installs_partial_result(tmp_path, monkeypatch):
    from src.autoslice import transcription_stage_cache as stage

    media = tmp_path.resolve() / "atomic.mp4"
    original = stage.os.link

    def interrupt_link(source, target, **kwargs):
        if Path(target).parent.name == "pronoun":
            raise KeyboardInterrupt()
        return original(source, target, **kwargs)

    monkeypatch.setattr(stage.os, "link", interrupt_link)
    callback = _identified(lambda _: KEEP)
    with pytest.raises(KeyboardInterrupt):
        _cached_run(media, callback)
    folder = tmp_path / ".transcription-stage-cache/pronoun"
    assert not list(folder.glob("*.json"))
    assert not list(folder.glob(".stage-*.tmp"))
    monkeypatch.setattr(stage.os, "link", original)
    _, cache = _cached_run(media, callback)
    assert cache.audit["pronoun"]["persisted"] is True


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
