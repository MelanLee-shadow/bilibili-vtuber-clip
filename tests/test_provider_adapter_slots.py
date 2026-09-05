from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

from scripts import gemini_slice_jingting as jingting
from src.autoslice import agy_gemini_client, cover_generation, cpa_frame_witness, llm_client
from src.autoslice import provider_slots
from src.autoslice.provider_slots import provider_slot
from src.autoslice.qixi_transaction_core import exclusive_runner_commit


class _Response(io.BytesIO):
    status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _write(path: Path, payload: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def _assert_waits_then_succeeds(call: callable, *, runtime: Path) -> None:
    """Run two mocked provider calls and prove a held provider never holds runner."""
    errors: list[BaseException] = []
    threads = [threading.Thread(target=lambda index=index: _call_catching(call, index, errors)) for index in range(2)]
    for thread in threads:
        thread.start()
    # The caller's mock records entry in its shared closure; wait for both
    # threads below through the attributes it intentionally exposes.
    assert getattr(call, "entered").wait(5)
    time.sleep(0.1)
    assert getattr(call, "starts")[0] == 1
    with exclusive_runner_commit(runtime):
        pass
    getattr(call, "release").set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert not errors
    assert getattr(call, "starts")[0] == 2
    assert getattr(call, "max_active")[0] == 1


def _call_catching(call: callable, index: int, errors: list[BaseException]) -> None:
    try:
        call(index)
    except BaseException as exc:  # test harness must surface background errors
        errors.append(exc)


def _blocking_call() -> tuple[callable, threading.Event, threading.Event, list[int], list[int]]:
    entered, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    starts, active, max_active = [0], [0], [0]

    def block() -> None:
        with guard:
            starts[0] += 1
            active[0] += 1
            max_active[0] = max(max_active[0], active[0])
            entered.set()
        release.wait(5)
        with guard:
            active[0] -= 1

    return block, entered, release, starts, max_active


def _attach_observation(call: callable, entered: threading.Event, release: threading.Event, starts: list[int], max_active: list[int]) -> callable:
    # Functions are mutable, and this keeps the shared test helper small while
    # making its timing assertions explicit at each adapter seam.
    call.entered = entered  # type: ignore[attr-defined]
    call.release = release  # type: ignore[attr-defined]
    call.starts = starts  # type: ignore[attr-defined]
    call.max_active = max_active  # type: ignore[attr-defined]
    return call


def test_cpa_image_edit_pool_covers_api_and_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("AUTOSLICE_PROVIDER_WAIT_SECONDS", "5")
    reference = _write(tmp_path / "reference.png", b"reference")
    block, entered, release, starts, max_active = _blocking_call()

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        if isinstance(request, str):
            return _Response(b"downloaded-image")
        block()
        return _Response(json.dumps({"data": [{"url": "https://download.invalid/cover.png"}]}).encode())

    monkeypatch.setattr(cover_generation.urllib.request, "urlopen", fake_urlopen)

    def call(index: int) -> None:
        result = cover_generation._call_cpa_image_edit(
            base_url="https://cpa.invalid",
            api_key="not-a-real-key",
            reference_path=reference,
            output_path=tmp_path / f"output-{index}.png",
            prompt="fixture",
            request_path=tmp_path / f"request-{index}.json",
            response_path=tmp_path / f"response-{index}.json",
            normalize_canvas=lambda _path: (1920, 1080),
        )
        assert result["status"] == "AI_BACKGROUND_READY"

    _assert_waits_then_succeeds(_attach_observation(call, entered, release, starts, max_active), runtime=runtime)


def test_cpa_vision_pool_waits_and_releases_after_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("AUTOSLICE_PROVIDER_WAIT_SECONDS", "5")
    block, entered, release, starts, max_active = _blocking_call()

    def fake_urlopen(_request: object, *, timeout: float) -> _Response:
        block()
        return _Response(json.dumps({"output_text": "observed"}).encode())

    monkeypatch.setattr(cpa_frame_witness.urllib.request, "urlopen", fake_urlopen)

    def call(_index: int) -> None:
        receipt = cpa_frame_witness._vision_qa(
            {}, b"jpeg", "fixture", api_base="https://cpa.invalid", api_key="not-a-real-key", model="fixture", timeout_seconds=1, max_tokens=8
        )
        assert receipt["status"] == "OBSERVED"

    _assert_waits_then_succeeds(_attach_observation(call, entered, release, starts, max_active), runtime=runtime)
    monkeypatch.setattr(cpa_frame_witness.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cpa_frame_witness._vision_qa({}, b"jpeg", "fixture", api_base="https://cpa.invalid", api_key="not-a-real-key", model="fixture", timeout_seconds=1, max_tokens=8)["status"] == "UNAVAILABLE"
    with provider_slot(runtime):
        pass


def test_gemini_and_agy_provider_transports_share_bounded_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("AUTOSLICE_PROVIDER_WAIT_SECONDS", "5")
    audio = _write(tmp_path / "audio.mp3", b"audio")
    block, entered, release, starts, max_active = _blocking_call()

    def fake_urlopen(_request: object, *, timeout: float) -> _Response:
        block()
        return _Response(json.dumps({"candidates": [{"content": {"parts": [{"text": "corrected"}]}}]}).encode())

    monkeypatch.setattr(jingting.urllib.request, "urlopen", fake_urlopen)

    def call(_index: int) -> None:
        assert jingting.gemini_correct(str(audio), "draft", "not-a-real-key") == "corrected"

    _assert_waits_then_succeeds(_attach_observation(call, entered, release, starts, max_active), runtime=runtime)
    monkeypatch.setattr(jingting.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        jingting.gemini_correct(str(audio), "draft", "not-a-real-key")
    with provider_slot(runtime):
        pass

    agy = _write(tmp_path / "agy", b"binary")
    source = _write(tmp_path / "source.mp4", b"source")
    draft = _write(tmp_path / "draft.srt", "1\n00:00:00,000 --> 00:00:01,000\nold\n")
    monkeypatch.setattr(jingting, "AGY_BIN", str(agy))
    monkeypatch.setattr(jingting, "JINGTING_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setattr(jingting, "remux_for_agy", lambda _source, job_dir: _write(job_dir / "input.mp4", b"prepared"))
    block, entered, release, starts, max_active = _blocking_call()

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(_command: object, *, cwd: str | Path, **_kwargs: object) -> Completed:
        block()
        _write(Path(cwd) / "output.srt", draft.read_text(encoding="utf-8"))
        return Completed()

    monkeypatch.setattr(jingting.subprocess, "run", fake_run)

    def agy_call(index: int) -> None:
        indexed_source = _write(tmp_path / f"source-{index}.mp4", b"source")
        jingting.run_agy(str(indexed_source), str(draft), str(tmp_path / f"result-{index}.srt"), process_timeout_seconds=30)

    _assert_waits_then_succeeds(_attach_observation(agy_call, entered, release, starts, max_active), runtime=runtime)
    monkeypatch.setattr(jingting.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        jingting.run_agy(str(source), str(draft), str(tmp_path / "failed.srt"), process_timeout_seconds=30)
    with provider_slot(runtime):
        pass


def test_two_provider_adapters_share_one_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("AUTOSLICE_PROVIDER_WAIT_SECONDS", "5")
    audio = _write(tmp_path / "audio.mp3", b"audio")
    block, entered, release, starts, max_active = _blocking_call()

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        block()
        url = request.full_url if hasattr(request, "full_url") else ""
        payload = {"output_text": "observed"} if "/responses" in url else {"candidates": [{"content": {"parts": [{"text": "corrected"}]}}]}
        return _Response(json.dumps(payload).encode())

    monkeypatch.setattr(jingting.urllib.request, "urlopen", fake_urlopen)

    def call(index: int) -> None:
        if index:
            assert jingting.gemini_correct(str(audio), "draft", "not-a-real-key") == "corrected"
        else:
            assert cpa_frame_witness._vision_qa({}, b"jpeg", "fixture", api_base="https://cpa.invalid", api_key="not-a-real-key", model="fixture", timeout_seconds=1, max_tokens=8)["status"] == "OBSERVED"

    _assert_waits_then_succeeds(_attach_observation(call, entered, release, starts, max_active), runtime=runtime)


def test_shared_agy_client_subprocess_is_bounded_and_gemini_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("AUTOSLICE_PROVIDER_WAIT_SECONDS", "5")
    block, entered, release, starts, max_active = _blocking_call()

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_runner(_argv: object, **_kwargs: object) -> Completed:
        block()
        return Completed()

    def call(_index: int) -> None:
        assert agy_gemini_client.run_local_agy(
            ["agy"], cwd=tmp_path, timeout=30, command_runner=fake_runner
        ).launched

    _assert_waits_then_succeeds(_attach_observation(call, entered, release, starts, max_active), runtime=runtime)
    monkeypatch.setattr(
        agy_gemini_client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        agy_gemini_client.generate_content(prompt="fixture", key="not-a-real-key", model="fixture", timeout_seconds=30)
    with provider_slot(runtime):
        pass


def test_llm_client_and_direct_cpa_adapter_share_one_runtime_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("AUTOSLICE_PROVIDER_WAIT_SECONDS", "5")
    block, entered, release, starts, max_active = _blocking_call()
    monkeypatch.setattr(llm_client, "_call_command", lambda *_args: (block(), "ok")[1])

    def fake_urlopen(_request: object, *, timeout: float) -> _Response:
        block()
        return _Response(json.dumps({"output_text": "observed"}).encode())

    monkeypatch.setattr(cpa_frame_witness.urllib.request, "urlopen", fake_urlopen)
    command = llm_client.build_llm_call(llm_client.LlmConfig(transport="command", command_template="ignored"))

    def call(index: int) -> None:
        if index:
            assert command("fixture") == "ok"
        else:
            assert cpa_frame_witness._vision_qa({}, b"jpeg", "fixture", api_base="https://cpa.invalid", api_key="not-a-real-key", model="fixture", timeout_seconds=1, max_tokens=8)["status"] == "OBSERVED"

    _assert_waits_then_succeeds(_attach_observation(call, entered, release, starts, max_active), runtime=runtime)


def test_provider_capacity_timeout_has_adapter_specific_failure_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setattr(provider_slots, "provider_wait_for_call", lambda *_args, **_kwargs: 0.01)
    monkeypatch.setattr(cpa_frame_witness, "provider_wait_for_call", lambda *_args, **_kwargs: 0.01)
    monkeypatch.setattr(agy_gemini_client, "provider_wait_for_call", lambda *_args, **_kwargs: 0.01)
    monkeypatch.setattr(jingting, "provider_wait_for_call", lambda *_args, **_kwargs: 0.01)
    reference = _write(tmp_path / "reference.png", b"reference")
    with provider_slot(runtime):
        cover = cover_generation._call_cpa_image_edit(
            base_url="https://cpa.invalid", api_key="not-a-real-key", reference_path=reference,
            output_path=tmp_path / "output.png", prompt="fixture", request_path=tmp_path / "request.json", response_path=tmp_path / "response.json",
        )
        assert cover["reason_code"] == "CPA_IMAGE_EDIT_PROVIDER_CAPACITY"
        receipt = cpa_frame_witness._vision_qa({}, b"jpeg", "fixture", api_base="https://cpa.invalid", api_key="not-a-real-key", model="fixture", timeout_seconds=1, max_tokens=8)
        assert receipt["reason_code"] == "VISION_PROVIDER_CAPACITY"
        agy = agy_gemini_client.run_local_agy(["agy"], cwd=tmp_path, timeout=30, command_runner=lambda *_a, **_k: None)
        assert agy.failure_category == agy_gemini_client.AGY_TIMEOUT
        assert agy.launch_error_type == "ProviderSlotTimeout"
        binary = _write(tmp_path / "agy", b"binary")
        source = _write(tmp_path / "source.mp4", b"source")
        draft = _write(tmp_path / "draft.srt", "1\n00:00:00,000 --> 00:00:01,000\nold\n")
        monkeypatch.setattr(jingting, "AGY_BIN", str(binary))
        monkeypatch.setattr(jingting, "JINGTING_JOB_ROOT", str(tmp_path / "jobs"))
        monkeypatch.setattr(jingting, "remux_for_agy", lambda _source, job_dir: _write(job_dir / "input.mp4", b"prepared"))
        from src.autoslice.source_context_executor import AgyRunnerError

        with pytest.raises(AgyRunnerError) as caught:
            jingting.run_agy(str(source), str(draft), str(tmp_path / "result.srt"), process_timeout_seconds=30)
        assert caught.value.reason_code == "AGY_TIMEOUT"
        assert "provider capacity" in str(caught.value)
