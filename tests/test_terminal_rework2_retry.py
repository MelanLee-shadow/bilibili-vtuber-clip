import json
from contextlib import contextmanager
from email.message import Message
from urllib.error import HTTPError

import pytest

from src.autoslice import infra_retry_policy as retry
from src.autoslice.llm_client import LlmCallError


@pytest.mark.parametrize('error', [
    AssertionError('saved material contains timeout and OUTCOME_UNKNOWN'),
    ValueError('validation of previous OUTCOME_UNKNOWN failed'),
    RuntimeError('the provider record said timed out'),
])
def test_code_and_validation_errors_never_become_transport_retries(error):
    from src.autoslice import infra_retry_policy as retry
    assert not retry.terminal_failure_is_transient(error)


def _clock_and_sleeper():
    now = [0.0]
    sleeps = []

    def clock():
        return now[0]

    def sleeper(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    return now, sleeps, clock, sleeper


def _service_error(*, status=503, retry_after=None):
    diagnostics = {"provider_http_status": status}
    if retry_after is not None:
        diagnostics["provider_retry_after_seconds"] = retry_after
    return LlmCallError(f"HTTP {status}", provider_diagnostics=diagnostics)


def test_terminal_transport_retry_succeeds_without_an_attempt_cap():
    now, sleeps, clock, sleeper = _clock_and_sleeper()
    calls = []
    failures = [_service_error(), _service_error()]

    def call():
        calls.append(len(calls) + 1)
        if failures:
            raise failures.pop(0)
        return "answer"

    with retry.terminal_retry_scope(
        wallclock_seconds=30, clock=clock, sleeper=sleeper
    ) as state:
        assert retry.terminal_retry_call(call) == "answer"

    assert calls == [1, 2, 3]
    assert sleeps == [2.0, 4.0]
    assert state["attempts"] == 3
    assert state["retries"] == 2
    assert [row["status"] for row in state["events"]] == [
        "RETRY_SCHEDULED",
        "RETRY_SCHEDULED",
        "COMPLETED",
    ]


def test_terminal_permanent_provider_rejection_is_not_retried():
    now, sleeps, clock, sleeper = _clock_and_sleeper()
    calls = []

    def call():
        calls.append(1)
        raise LlmCallError(
            "HTTP 400", provider_diagnostics={"provider_http_status": 400}
        )

    with retry.terminal_retry_scope(
        wallclock_seconds=30, clock=clock, sleeper=sleeper
    ) as state:
        with pytest.raises(LlmCallError):
            retry.terminal_retry_call(call)

    assert len(calls) == 1
    assert sleeps == []
    assert state["events"][-1]["status"] == "PERMANENT_FAILURE"


def test_terminal_wallclock_exhaustion_preserves_explicit_reason():
    now, sleeps, clock, sleeper = _clock_and_sleeper()
    error = _service_error()

    with retry.terminal_retry_scope(
        wallclock_seconds=1, clock=clock, sleeper=sleeper
    ) as state:
        with pytest.raises(LlmCallError, match=retry.TERMINAL_RETRY_WALLCLOCK_EXHAUSTED):
            retry.terminal_retry_call(lambda: (_ for _ in ()).throw(error))

    assert state["attempts"] == 1
    assert state["retries"] == 0
    assert state["events"][-1]["status"] == "WALLCLOCK_EXHAUSTED"
    assert error.terminal_retry_reason == retry.TERMINAL_RETRY_WALLCLOCK_EXHAUSTED


def test_terminal_wallclock_does_not_start_retry_that_cannot_fit_call_timeout():
    now, sleeps, clock, sleeper = _clock_and_sleeper()
    calls = []

    def call():
        calls.append(1)
        raise _service_error()

    with retry.terminal_retry_scope(
        wallclock_seconds=5, clock=clock, sleeper=sleeper
    ):
        with pytest.raises(LlmCallError, match=retry.TERMINAL_RETRY_WALLCLOCK_EXHAUSTED):
            retry.terminal_retry_call(call, timeout_seconds=4)

    assert calls == [1]
    assert sleeps == [2.0]


def test_terminal_unknown_outcome_is_retained_and_physical_attempts_are_counted():
    now, sleeps, clock, sleeper = _clock_and_sleeper()
    calls = []
    unknown = LlmCallError("OUTCOME_UNKNOWN")

    def call():
        calls.append(1)
        if len(calls) == 1:
            raise unknown
        return "answer"

    with retry.terminal_retry_scope(
        wallclock_seconds=30, clock=clock, sleeper=sleeper
    ) as state:
        assert retry.terminal_retry_call(call) == "answer"

    assert len(calls) == state["attempts"] == 2
    assert state["unknown_attempts"] == 1
    assert state["events"][0]["status"] == "RETRY_SCHEDULED"
    assert state["events"][0]["error_type"] == "LlmCallError"


def test_terminal_retry_after_overrides_local_exponential_delay():
    now, sleeps, clock, sleeper = _clock_and_sleeper()
    failures = [_service_error(retry_after=9)]

    def call():
        if failures:
            raise failures.pop(0)
        return "answer"

    with retry.terminal_retry_scope(
        wallclock_seconds=30, clock=clock, sleeper=sleeper
    ):
        assert retry.terminal_retry_call(call) == "answer"

    assert sleeps == [9.0]


def test_direct_http_retry_after_is_preserved_in_closed_diagnostics(monkeypatch):
    from src.autoslice import llm_client

    headers = Message()
    headers["Retry-After"] = "17"

    def fail(*_args, **_kwargs):
        raise HTTPError("https://cpa.invalid/responses", 503, "busy", headers, None)

    monkeypatch.setenv("TERMINAL_TEST_CPA_KEY", "test-key")
    monkeypatch.setattr(llm_client.urllib.request, "urlopen", fail)
    config = llm_client.LlmConfig(
        transport="direct",
        model="gpt-6.1-sol",
        api_base="https://cpa.invalid/v1",
        api_key_env="TERMINAL_TEST_CPA_KEY",
    )

    with pytest.raises(LlmCallError) as raised:
        llm_client._call_direct("prompt", config)

    assert raised.value.retry_after_seconds == 17
    assert raised.value.provider_diagnostics["provider_http_status"] == 503
    assert raised.value.provider_diagnostics["provider_retry_after_seconds"] == 17


def test_request_terminal_answer_retries_unwrapped_transport_adapter(monkeypatch):
    from src.autoslice import response_schemas, semantic_terminal_adjudication as terminal
    from src.autoslice import structured_output

    calls = []
    failures = [_service_error()]

    def call(prompt):
        calls.append(prompt)
        if failures:
            raise failures.pop(0)
        return json.dumps({"choice": "CURRENT"})

    monkeypatch.setattr(response_schemas, "terminal_schema", lambda _request: {})
    monkeypatch.setattr(
        structured_output,
        "call_json",
        lambda send, prompt, **_kwargs: send(prompt),
    )
    monkeypatch.setattr(retry.time, "sleep", lambda _seconds: None)

    assert terminal.request_terminal_answer(call, "prompt", {}) == json.dumps(
        {"choice": "CURRENT"}
    )
    assert len(calls) == 2


def test_request_terminal_answer_surfaces_wallclock_reason(monkeypatch):
    from src.autoslice import response_schemas, semantic_terminal_adjudication as terminal
    from src.autoslice import structured_output

    monkeypatch.setattr(response_schemas, "terminal_schema", lambda _request: {})
    monkeypatch.setattr(
        structured_output,
        "call_json",
        lambda send, prompt, **_kwargs: send(prompt),
    )

    with pytest.raises(ValueError, match=retry.TERMINAL_RETRY_WALLCLOCK_EXHAUSTED):
        terminal.request_terminal_answer(
            lambda _prompt: (_ for _ in ()).throw(_service_error()),
            "prompt",
            {},
            wallclock_seconds=1,
        )


def test_native_call_binds_slot_and_transport_to_remaining_budget(monkeypatch):
    from src.autoslice import llm_client

    now = [0.0]
    slot_timeouts = []
    transport_timeouts = []

    @contextmanager
    def fake_slot(*, runtime_root, timeout_seconds=None):
        assert runtime_root == "/runtime"
        slot_timeouts.append(timeout_seconds)
        now[0] += 50.0
        yield None

    def fake_command(_prompt, config):
        transport_timeouts.append(config.timeout_seconds)
        return '{"choice":"CURRENT"}'

    monkeypatch.setattr(llm_client, "runtime_provider_slot", fake_slot)
    monkeypatch.setattr(llm_client, "_call_command", fake_command)
    call = llm_client.build_llm_call(
        llm_client.LlmConfig(
            transport="command",
            command_template="bridge {prompt_file} {completion_file}",
            runtime_root="/runtime",
            timeout_seconds=60.0,
        )
    )

    with retry.terminal_retry_scope(
        wallclock_seconds=100.0,
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    ):
        assert call("prompt") == '{"choice":"CURRENT"}'

    assert slot_timeouts == [100.0]
    assert transport_timeouts == [50.0]


def test_native_call_uses_runtime_environment_slot_and_short_call_deadline(
    monkeypatch,
):
    from src.autoslice import llm_client

    now = [0.0]
    slot_calls = []
    transport_timeouts = []

    monkeypatch.setenv("AUTOSLICE_BASE", "/runtime-from-env")

    @contextmanager
    def fake_slot(*, runtime_root, timeout_seconds=None):
        slot_calls.append((runtime_root, timeout_seconds))
        now[0] += 2.0
        yield None

    def fake_command(_prompt, config):
        transport_timeouts.append(config.timeout_seconds)
        now[0] += 2.0
        return '{"choice":"CURRENT"}'

    monkeypatch.setattr(llm_client, "runtime_provider_slot", fake_slot)
    monkeypatch.setattr(llm_client, "_call_command", fake_command)
    call = llm_client.build_llm_call(
        llm_client.LlmConfig(
            transport="command",
            command_template="bridge {prompt_file} {completion_file}",
            runtime_root=None,
            timeout_seconds=60.0,
        )
    )

    with retry.terminal_retry_scope(
        wallclock_seconds=5.0,
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    ):
        assert call("prompt") == '{"choice":"CURRENT"}'

    assert slot_calls == [(None, 5.0)]
    assert transport_timeouts == [3.0]
    assert now[0] == 4.0


def test_native_retry_reclamps_short_remaining_deadline_after_transient_failure(
    monkeypatch,
):
    from src.autoslice import llm_client

    now = [0.0]
    slot_timeouts = []
    transport_timeouts = []
    failures = [_service_error()]

    @contextmanager
    def fake_slot(*, runtime_root, timeout_seconds=None):
        assert runtime_root == "/runtime"
        slot_timeouts.append(timeout_seconds)
        yield None

    def fake_command(_prompt, config):
        transport_timeouts.append(config.timeout_seconds)
        if failures:
            raise failures.pop(0)
        now[0] += 2.0
        return '{"choice":"CURRENT"}'

    monkeypatch.setattr(llm_client, "runtime_provider_slot", fake_slot)
    monkeypatch.setattr(llm_client, "_call_command", fake_command)
    call = llm_client.build_llm_call(
        llm_client.LlmConfig(
            transport="command",
            command_template="bridge {prompt_file} {completion_file}",
            runtime_root="/runtime",
            timeout_seconds=60.0,
        )
    )

    with retry.terminal_retry_scope(
        wallclock_seconds=5.0,
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    ) as state:
        assert call("prompt") == '{"choice":"CURRENT"}'

    assert transport_timeouts == [5.0, 3.0]
    assert slot_timeouts == [5.0, 3.0]
    assert state["attempts"] == 2
    assert state["retries"] == 1
    assert now[0] == 4.0


def test_pressure_wrapper_preserves_native_retry_and_recorder_does_not_double_retry(
    tmp_path, monkeypatch
):
    from src.autoslice import llm_client
    from src.autoslice.final_review_provider_budget import with_cpa_resource_pressure

    now, _sleeps, clock, sleeper = _clock_and_sleeper()
    physical_calls = []
    failures = [_service_error()]

    @contextmanager
    def fake_slot(*, runtime_root, timeout_seconds=None):
        assert runtime_root == "/runtime"
        yield None

    def fake_command(_prompt, config):
        physical_calls.append(config.timeout_seconds)
        if failures:
            raise failures.pop(0)
        return "answer"

    monkeypatch.setattr(llm_client, "runtime_provider_slot", fake_slot)
    monkeypatch.setattr(llm_client, "_call_command", fake_command)
    native = llm_client.build_llm_call(
        llm_client.LlmConfig(
            transport="command",
            command_template="bridge {prompt_file} {completion_file}",
            runtime_root="/runtime",
            timeout_seconds=60.0,
        )
    )
    native.terminal_retry_wallclock_seconds = 30.0
    native._semantic_attempt_recorder = "native-recorder-marker"
    pressured = with_cpa_resource_pressure(native, count=2, soft_target=1)

    for attr in (
        "terminal_retry_capable",
        "terminal_retry_wallclock_seconds",
        "timeout_seconds",
        "structured_output_transport",
        "_semantic_attempt_recorder",
    ):
        assert getattr(pressured, attr) == getattr(native, attr)

    recorder, wrapped = _recorder_fixture(tmp_path, monkeypatch, pressured)
    with retry.terminal_retry_scope(
        wallclock_seconds=100.0, clock=clock, sleeper=sleeper
    ) as state:
        assert wrapped("question") == "answer"

    assert physical_calls == [60.0, 60.0]
    assert state["attempts"] == 2
    assert state["retries"] == 1
    assert len(recorder._entries()) == 1


def _recorder_fixture(tmp_path, monkeypatch, call):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    recorder = SemanticAttemptRecorder(
        srt_text="1\n00:00:01,000 --> 00:00:02,000\n目标句\n",
        finding={"cue_index": 1, "why": "核对目标句"},
        clip_context={"source_media_sha256": "a" * 64},
        offset_ms=0,
    )
    assert not recorder._entries(), {
        "directory": str(recorder.directory), "audit": recorder.audit(),
        "reason": "Each retry fixture starts in its own empty runtime",
    }
    call.cpa_cache_identity = {"models": ["fixture"]}
    return recorder, recorder.wrap(call)


def test_recorder_wrap_retries_plain_callable_as_one_logical_attempt(
    tmp_path, monkeypatch
):
    calls = []
    failures = [_service_error()]

    def call(prompt):
        calls.append(prompt)
        if failures:
            raise failures.pop(0)
        return "answer"

    recorder, wrapped = _recorder_fixture(tmp_path, monkeypatch, call)
    now, _sleeps, clock, sleeper = _clock_and_sleeper()
    with retry.terminal_retry_scope(
        wallclock_seconds=30, clock=clock, sleeper=sleeper
    ) as state:
        assert wrapped("question") == "answer"

    rows = recorder._entries()
    assert len(calls) == state["attempts"] == 2, {
        "calls": len(calls), "attempts": state["attempts"],
        "directory": str(recorder.directory), "audit": recorder.audit(),
    }
    assert len(rows) == 1
    assert rows[0]["status"] == "COMPLETED"
    assert state["retries"] == 1


def test_recorder_wrap_retains_unknown_after_transport_exhaustion(
    tmp_path, monkeypatch
):
    calls = []

    def call(_prompt):
        calls.append(1)
        raise LlmCallError("OUTCOME_UNKNOWN")

    recorder, wrapped = _recorder_fixture(tmp_path, monkeypatch, call)
    now, _sleeps, clock, sleeper = _clock_and_sleeper()
    with retry.terminal_retry_scope(
        wallclock_seconds=1, clock=clock, sleeper=sleeper
    ) as state:
        with pytest.raises(LlmCallError, match=retry.TERMINAL_RETRY_WALLCLOCK_EXHAUSTED):
            wrapped("question")

    rows = recorder._entries()
    assert calls == [1]
    assert len(rows) == 1
    assert rows[0]["status"] == "OUTCOME_UNKNOWN"
    assert state["unknown_attempts"] == 1


@pytest.mark.parametrize("status", [400, 401, 403])
def test_auth_marker_cannot_override_explicit_client_rejection(status):
    error = LlmCallError(
        f"HTTP {status} auth_unavailable",
        provider_diagnostics={
            "provider_http_status": status,
            "provider_error_code": "auth_unavailable",
        },
    )
    assert not retry.terminal_failure_is_transient(error)
