"""H104: the ordinary request recovers once, without rewriting original UNKNOWN."""

from pathlib import Path
import json
import os
import subprocess
import sys
import time
import pytest

from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
from src.autoslice.semantic_retry_progress import RetryNeedsPlan
from src.autoslice.infra_retry_policy import (
    CPA_TEXT_RETRY_BASE_SECONDS, CPA_TEXT_RETRY_JITTER_FRACTION,
)

SRT = "1\n00:00:01,000 --> 00:00:02,000\n海边今天刮大风\n"
MODEL = {"transport": "recorded", "models": ["fixture-model"], "effort": "medium"}
ANSWER = '{"choice":"CURRENT","needs_audio":false}'


def seed(root, status="OUTCOME_UNKNOWN"):
    raw = Path(__file__).with_name("fixtures").joinpath("h104_legacy_unknown.json").read_bytes()
    value = json.loads(raw)
    if status != "OUTCOME_UNKNOWN":
        value["status"] = status
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    path = (
        root
        / "cache/semantic-cpa-attempts"
        / value["issue_sha256"]
        / (value["request_sha256"] + ".json")
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    return path, raw


def recorder():
    return SemanticAttemptRecorder(
        srt_text=SRT,
        finding={"cue_index": 1, "why": "ordinary request"},
        clip_context={"source_media_sha256": "a" * 64},
        offset_ms=0,
    )


@pytest.mark.parametrize("status", ["STARTED", "ERROR", "OUTCOME_UNKNOWN"])
def test_ordinary_legacy_unknown_recovery_then_cold_zero_dispatch(tmp_path, monkeypatch, status):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    original, raw = seed(tmp_path, status)
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return ANSWER

    provider.cpa_cache_identity = MODEL
    assert recorder().wrap(provider)("ordinary JSON question") == ANSWER

    def forbidden(prompt):
        raise AssertionError("completed request was sent again")

    forbidden.cpa_cache_identity = MODEL
    assert recorder().wrap(forbidden)("ordinary JSON question") == ANSWER
    assert len(calls) == 1 and original.read_bytes() == raw


def child(root, operation, *, wait=True):
    env = {**os.environ, "AUTOSLICE_BASE": str(root)}
    command = [sys.executable, "-m", "tests.test_cpa_persistent_request", str(root), operation]
    if wait:
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    return subprocess.Popen(
        command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )


def test_ten_reservations_survive_ten_crashed_processes_and_cosmetic_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    original, raw = seed(tmp_path)
    for _ in range(10):
        assert child(tmp_path, "crash").returncode == 77
    exhausted = child(tmp_path, "crash")
    assert exhausted.returncode != 77 and "CPA_TEXT_RETRY_EXHAUSTED" in exhausted.stderr
    assert len((tmp_path / "physical-calls").read_text().splitlines()) == 10
    current = recorder()
    current.plan = {
        "action": "reframe_question",
        "question": "cosmetic",
        "why_previous_failed": "lost",
        "expected_gain": "same",
        "verification": "same",
    }

    def forbidden(prompt):
        raise AssertionError("budget reset")

    forbidden.cpa_cache_identity = MODEL
    with pytest.raises(RetryNeedsPlan, match="CPA_TEXT_RETRY_EXHAUSTED"):
        current.wrap(forbidden)("ordinary JSON question")
    assert original.read_bytes() == raw
    state = json.loads(next(tmp_path.rglob("state")).read_text())
    assert state["reserved_attempts"] == 10 and state["legacy_physical_sends"] is None
    assert all(e["possible_duplicate_billing"] for e in state["events"] if e["status"] == "STARTED")


def test_completed_checkpoint_survives_crash_before_recorder_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    original, raw = seed(tmp_path)
    assert child(tmp_path, "complete-crash").returncode == 78
    replay = child(tmp_path, "forbidden")
    assert replay.returncode == 0, replay.stderr
    assert (
        len((tmp_path / "physical-calls").read_text().splitlines()) == 1
        and original.read_bytes() == raw
    )


def test_live_owner_and_competing_process_do_not_double_send(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    seed(tmp_path)
    owner = child(tmp_path, "block", wait=False)
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "entered").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "entered").exists()
        contender = child(tmp_path, "forbidden")
        assert contender.returncode != 0 and "CPA_TEXT_REQUEST_IN_FLIGHT" in contender.stderr
        (tmp_path / "release").touch()
        assert owner.wait(timeout=10) == 0
        assert child(tmp_path, "forbidden").returncode == 0
        assert len((tmp_path / "physical-calls").read_text().splitlines()) == 1
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=10)


def test_native_transport_shares_one_retry_owner_and_retry_after(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.autoslice import cpa_request_lifecycle as life, llm_client as llm
    from src.autoslice.infra_retry_policy import terminal_retry_scope
    from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    clock = [100.0]
    sleeps = []
    calls = []
    keys = []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(
        life,
        "time",
        SimpleNamespace(
            time=lambda: clock[0],
            monotonic=lambda: clock[0],
            sleep=sleep,
            time_ns=lambda: int(clock[0] * 1e9),
        ),
    )

    def physical(prompt, config):
        calls.append(prompt)
        keys.append(life.request_headers()["Idempotency-Key"])
        if len(calls) < 3:
            raise llm.LlmCallError(
                "quota",
                provider_diagnostics={
                    "provider_http_status": 429,
                    "provider_retry_after_seconds": 7,
                },
                response_received=True,
            )
        return llm.CpaCompletion(ANSWER, TOP_CPA_MODEL)

    monkeypatch.setattr(llm, "_call_direct_once", physical)
    call = llm.build_llm_call(
        llm.LlmConfig(
            transport="direct",
            api_base="https://fixture.invalid/v1",
            model=TOP_CPA_MODEL,
            api_key_env="CPA_API_KEY",
            runtime_root=tmp_path,
        )
    )
    with terminal_retry_scope():
        assert call("JSON ordinary native request") == ANSWER
    assert sleeps == [7, 7] and len(set(keys)) == 1 and len(calls) == 3
    assert call("JSON ordinary native request") == ANSWER and len(calls) == 3
    state = json.loads(next(tmp_path.rglob("state")).read_text())
    assert state["reserved_attempts"] == 3


@pytest.mark.parametrize("changed", ["model", "provider", "schema", "input"])
def test_changed_request_cannot_reuse_other_bound_answer(tmp_path, monkeypatch, changed):
    from src.autoslice.cpa_request_lifecycle import persistent_text_call
    from src.autoslice.structured_output import response_contract

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return ANSWER

    base = {
        "model": MODEL,
        "root": tmp_path,
        "provider": {"provider_endpoint_host": "first.invalid"},
    }
    assert persistent_text_call(provider, "JSON request", **base) == ANSWER
    next_binding = dict(base)
    prompt = "JSON request"
    if changed == "model":
        next_binding["model"] = {**MODEL, "models": ["other-model"]}
    if changed == "provider":
        next_binding["provider"] = {"provider_endpoint_host": "other.invalid"}
    if changed == "input":
        prompt += " new input"
    with response_contract(
        {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        if changed == "schema"
        else None,
        json_mode=changed == "schema",
    ):
        assert persistent_text_call(provider, prompt, **next_binding) == ANSWER
    assert len(calls) == 2


def test_bad_schema_and_damaged_raw_fail_without_new_dispatch(tmp_path, monkeypatch):
    from src.autoslice.cpa_request_lifecycle import persistent_text_call
    from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpointError
    from src.autoslice.llm_client import extract_json_object, LlmJsonParseError

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return "not JSON"

    for _ in range(2):
        raw = persistent_text_call(provider, "JSON request", model=MODEL, root=tmp_path)
        with pytest.raises(LlmJsonParseError):
            extract_json_object(raw)
    assert len(calls) == 1
    stored = next((tmp_path / "cache/completed-reviews").rglob("*.json"))
    stored.write_text(stored.read_text() + " ")
    with pytest.raises(CompletedReviewCheckpointError):
        persistent_text_call(provider, "JSON request", model=MODEL, root=tmp_path)
    assert len(calls) == 1


def test_typed_ordinary_ruling_budget_survives_cosmetic_rendering_and_output_dir(
    tmp_path, monkeypatch
):
    from src.autoslice import cpa_request_lifecycle as life, llm_client as llm
    from src.autoslice.completed_review_checkpoint import ruling_response
    from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL
    from tests.test_wpb_finding_context import check, materials

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    monkeypatch.setattr(life, "cpa_text_retry_delay_seconds", lambda *args: 0)
    sent = []

    def fail(prompt, config):
        sent.append(prompt)
        raise llm.LlmCallError(
            "response missing", safe_reason="LLM_COMMAND_TIMEOUT", response_received=False
        )

    monkeypatch.setattr(llm, "_call_direct_once", fail)
    call = llm.build_llm_call(
        llm.LlmConfig(
            transport="direct",
            api_base="https://fixture.invalid/v1",
            model=TOP_CPA_MODEL,
            api_key_env="CPA_API_KEY",
            runtime_root=tmp_path,
        )
    )
    inputs = {"finding_materials": materials(check())}
    with pytest.raises(llm.LlmCallError):
        ruling_response(
            stage="fixture-typed-stage",
            prompt="JSON original rendering",
            llm_call=call,
            inputs=inputs,
            parser_path=Path(__file__),
        )
    output = tmp_path / "other-output"
    output.mkdir()
    monkeypatch.chdir(output)
    with pytest.raises(RetryNeedsPlan, match="CPA_TEXT_RETRY_EXHAUSTED"):
        ruling_response(
            stage="fixture-typed-stage",
            prompt="JSON same typed input with different wording",
            llm_call=call,
            inputs=inputs,
            parser_path=Path(__file__),
        )
    assert len(sent) == 10 and len(list(tmp_path.rglob("state"))) == 1


def test_crashed_dispatch_preserves_backoff_before_next_process_send(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    original, raw = seed(tmp_path)
    for _ in range(3):
        result = child(tmp_path, "crash-with-backoff")
        assert result.returncode == 77, result.stderr
    sent_at = [float(x) for x in (tmp_path / "dispatch-times").read_text().splitlines()]
    assert sent_at[:2] == [100.0, 100.0]  # The first retry remains immediate.
    assert sent_at[2] - sent_at[1] >= (
        CPA_TEXT_RETRY_BASE_SECONDS * (1 - CPA_TEXT_RETRY_JITTER_FRACTION)
    )  # Ordinary 2s backoff, allowing its 20% jitter.
    assert original.read_bytes() == raw
    assert json.loads(next(tmp_path.rglob("state")).read_text())["reserved_attempts"] == 3


@pytest.mark.parametrize("abort", [SystemExit, KeyboardInterrupt])
def test_caught_process_abort_keeps_cross_resume_backoff(tmp_path, monkeypatch, abort):
    from types import SimpleNamespace
    from src.autoslice import cpa_request_lifecycle as life

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    original, raw = seed(tmp_path)
    clock = [100.0]
    sent_at = []

    def sleep(delay):
        clock[0] += delay

    monkeypatch.setattr(life, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: clock[0],
        time_ns=lambda: int(clock[0] * 1e9), sleep=sleep,
    ))

    def provider(prompt):
        sent_at.append(clock[0])
        raise abort("process interrupted without a response")

    provider.cpa_cache_identity = MODEL
    for _ in range(3):
        with pytest.raises(abort):
            recorder().wrap(provider)("ordinary JSON question")
    assert sent_at[:2] == [100.0, 100.0]
    assert sent_at[2] - sent_at[1] >= (
        CPA_TEXT_RETRY_BASE_SECONDS * (1 - CPA_TEXT_RETRY_JITTER_FRACTION)
    )
    assert original.read_bytes() == raw
    state = json.loads(next(tmp_path.rglob("state")).read_text())
    assert state["reserved_attempts"] == 3
    assert sum(e["status"] == "OUTCOME_UNKNOWN" for e in state["events"]) == 3


def _child_main():
    from src.autoslice import cpa_request_lifecycle as life

    root = Path(sys.argv[1])
    operation = sys.argv[2]
    if operation == "crash-with-backoff":
        from types import SimpleNamespace

        clock_path = root / "dispatch-clock"
        clock = [float(clock_path.read_text()) if clock_path.exists() else 100.0]

        def sleep(delay):
            clock[0] += delay
            clock_path.write_text(str(clock[0]))

        life.time = SimpleNamespace(
            time=lambda: clock[0], monotonic=lambda: clock[0],
            time_ns=lambda: int(clock[0] * 1e9), sleep=sleep,
        )
    else:
        life.cpa_text_retry_delay_seconds = lambda *args: 0
    original_save = life.TextRequest.save

    def save(self):
        if (
            operation == "complete-crash"
            and self.state["events"]
            and self.state["events"][-1]["status"] == "COMPLETED"
        ):
            os._exit(78)
        return original_save(self)

    life.TextRequest.save = save

    def provider(prompt):
        if operation == "forbidden":
            raise AssertionError("completed/in-flight request was dispatched")
        with (root / "physical-calls").open("a") as stream:
            stream.write("dispatch\n")
            stream.flush()
            os.fsync(stream.fileno())
        if operation == "crash-with-backoff":
            with (root / "dispatch-times").open("a") as stream:
                stream.write(str(life.time.time()) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        if operation in {"crash", "crash-with-backoff"}:
            os._exit(77)
        if operation == "block":
            (root / "entered").touch()
            deadline = time.monotonic() + 10
            while not (root / "release").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (root / "release").exists()
        return ANSWER

    provider.cpa_cache_identity = MODEL
    assert recorder().wrap(provider)("ordinary JSON question") == ANSWER


if __name__ == "__main__":
    _child_main()
