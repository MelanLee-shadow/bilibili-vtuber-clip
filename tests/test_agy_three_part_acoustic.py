from __future__ import annotations

import hashlib
import json
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.autoslice.agy_three_part_acoustic as adapter
from src.autoslice.agy_gemini_client import AgyRun
from src.autoslice.producer_nested_acoustic import observe_prepared_nested_acoustics
from tests.test_producer_nested_acoustic import _prepared


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _wav(path: Path, *, frames: int, value: int) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        sample = int(value).to_bytes(2, "little", signed=True)
        handle.writeframes(sample * frames)
    return path


def _descriptor(tmp_path: Path) -> dict[str, object]:
    paths = [
        _wav(tmp_path / "host.wav", frames=1600, value=100),
        _wav(tmp_path / "source.wav", frames=1920, value=200),
        _wav(tmp_path / "target.wav", frames=32000, value=300),
    ]
    rows = {
        "host_enrollment": ("host_enrollment_path", "host_enrollment_sha256"),
        "independent_source": ("source_audio_path", "source_audio_sha256"),
        "current_target": ("target_audio_path", "target_audio_sha256"),
    }
    descriptor: dict[str, object] = {
        "interval_ms": [0, 2000],
        "target_interval_ms": [500, 1000],
        "target_cue_indexes": [13],
    }
    for role, path in zip(adapter.ROLE_ORDER, paths, strict=True):
        path_field, hash_field = rows[role]
        descriptor[path_field] = str(path)
        descriptor[hash_field] = _sha(path)
    return descriptor


def _provider_payload() -> dict[str, object]:
    return {
        "status": "OBSERVED",
        "source_media_speech": "PRESENT",
        "host_speech": "ABSENT",
        "overlap_speech": "ABSENT",
        "target_interval_ms": [500, 1000],
        "target_cue_indexes": [13],
        "evidence": {"basis": "fixture"},
        "target_audio_offset_ms": [500, 1000],
    }


def _provider_json() -> str:
    return "\n".join([
        json.dumps({"type": "init", "session_id": "fixture"}),
        json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 1, "state": "ACTIVE", "step_type": "tool",
                "tool_name": "view_file", "tool_info": {"name": "view_file"},
            },
        }),
        json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 1, "state": "DONE", "step_type": "tool",
                "tool_name": "view_file",
                "tool_info": {
                    "name": "view_file",
                    "parameters": {"AbsolutePath": "/fixture/neutralpart-1.mp4"},
                },
            },
        }),
        json.dumps({
            "event": "result",
            "result": {"status": "SUCCESS", "structured_output": _provider_payload()},
        }),
    ]) + "\n"


def _legacy_provider_json() -> str:
    return json.dumps(_provider_payload())


def _request(descriptor: dict[str, object], identity: object) -> dict[str, object]:
    return {
        "candidate_id": "candidate",
        "observation_id": "cue-13",
        "runtime_identity": identity,
        "request_sha256": "sha256:" + "a" * 64,
        "interval_ms": [0, 2000],
        "target_interval_ms": [500, 1000],
        "target_cue_indexes": [13],
        "audio_parts": [
            {"role": role, "path": descriptor[path_field], "sha256": descriptor[hash_field]}
            for role, path_field, hash_field in (
                ("host_enrollment", "host_enrollment_path", "host_enrollment_sha256"),
                ("independent_source", "source_audio_path", "source_audio_sha256"),
                ("current_target", "target_audio_path", "target_audio_sha256"),
            )
        ],
    }


def _patch_local_tools(monkeypatch, *, run_result=None):
    monkeypatch.setattr(
        adapter, "_probe_binary_version",
        lambda _binary: {
            "version": "fixture-version", "status": "AVAILABLE", "returncode": 0,
            "output_sha256": "sha256:" + "1" * 64,
        },
    )
    monkeypatch.setattr(
        adapter.agy_gemini_client, "resolve_local_agy_executable",
        lambda _explicit=None: "/fixture/agy",
    )
    ffmpeg_calls: list[list[str]] = []

    def render(argv, **_kwargs):
        ffmpeg_calls.append(list(argv))
        output = Path(argv[-1])
        output.write_bytes(("mp4:" + argv[argv.index("-i", 2) + 1]).encode())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adapter.subprocess, "run", render)
    if run_result is not None:
        monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", run_result)
    return ffmpeg_calls


def test_prepare_derives_three_independent_duration_bound_attachments(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    ffmpeg_calls = _patch_local_tools(monkeypatch)

    prepared = adapter.prepare_three_part_attachments(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence",
    )

    assert len(ffmpeg_calls) == 3
    assert prepared.prompt_path.read_text(encoding="utf-8").startswith(
        adapter.ATTACHMENT_INSTRUCTIONS
    )
    assert [row["role"] for row in prepared.source_parts] == list(adapter.ROLE_ORDER)
    assert [row["role"] for row in prepared.derived_parts] == list(adapter.ROLE_ORDER)
    assert all("-shortest" in argv for argv in ffmpeg_calls)
    assert all("color=c=black:s=320x240:r=50" in argv for argv in ffmpeg_calls)
    assert [argv[argv.index("-t") + 1] for argv in ffmpeg_calls] == [
        "0.100000", "0.120000", "2.000000"
    ]
    assert prepared.transport_binding["attachment_prompt_sha256"] == prepared.attachment_prompt_sha256
    assert prepared.transport_binding["source_audio_sha256"] == {
        role: row["sha256"] for role, row in zip(adapter.ROLE_ORDER, prepared.source_parts, strict=True)
    }
    assert prepared.transport_binding["source_audio_durations_seconds"] == {
        role: row["duration_seconds"]
        for role, row in zip(adapter.ROLE_ORDER, prepared.source_parts, strict=True)
    }
    assert set(prepared.transport_binding["derived_attachment_sha256"]) == set(adapter.ROLE_ORDER)
    assert all(Path(row["path"]).is_file() for row in prepared.derived_parts)
    assert all(Path(descriptor[field]).read_bytes() for field in (
        "host_enrollment_path", "source_audio_path", "target_audio_path"
    ))

    before = len(ffmpeg_calls)
    replay = adapter.prepare_three_part_attachments(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence",
    )
    assert len(ffmpeg_calls) == before
    assert replay.job_dir == prepared.job_dir


def test_equal_source_bytes_are_allowed_when_each_role_has_verified_provenance(
    tmp_path, monkeypatch
):
    descriptor = _descriptor(tmp_path)
    host = Path(str(descriptor["host_enrollment_path"]))
    _wav(host, frames=32000, value=100)
    descriptor["host_enrollment_sha256"] = _sha(host)
    for path_field, hash_field in (
        ("source_audio_path", "source_audio_sha256"),
        ("target_audio_path", "target_audio_sha256"),
    ):
        path = Path(str(descriptor[path_field]))
        path.write_bytes(host.read_bytes())
        descriptor[hash_field] = _sha(path)
    _patch_local_tools(monkeypatch)

    prepared = adapter.prepare_three_part_attachments(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence",
    )

    assert len(prepared.source_parts) == 3
    assert len({str(row["sha256"]) for row in prepared.source_parts}) == 1
    assert len(prepared.derived_parts) == 3


def test_callbacks_bind_prompt_and_attachment_hashes_before_agy_dispatch(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    monkeypatch.delenv("AGY_THREE_PART_MODEL", raising=False)
    agy_calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        agy_calls.append(list(argv))
        return AgyRun(
            completed=subprocess.CompletedProcess(argv, 0, stdout=_provider_json(), stderr=""),
            failure_category=None,
        )

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", fake_run)
    def cpa(_prompt):
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    evidence_root = tmp_path / ("private-run-" + "a" * 90) / ("observation-" + "b" * 90)
    callbacks = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=evidence_root, cpa_call=cpa,
    )
    identity = callbacks.runtime_identity
    assert identity["transport"] == "agy_three_part"
    binding = identity["transport_binding"]
    assert len(binding["job_dir"]) > 256
    assert binding["job_dir"].startswith(str(evidence_root))
    assert all(str(Path(argv[-1]).parent) == binding["job_dir"] for argv in binding["ffmpeg"]["argv"])
    assert binding["observer_transport"] == "agy_three_part"
    assert binding["agy"]["model"] == "Gemini 3.8 Flash (Low)"
    assert binding["attachment_prompt_sha256"].startswith("sha256:")
    assert set(binding["derived_attachment_sha256"]) == set(adapter.ROLE_ORDER)
    assert binding["ffmpeg"]["argv"][0][-2] == "-shortest"

    request = {
        "candidate_id": "candidate",
        "observation_id": "cue-13",
        "runtime_identity": identity,
        "request_sha256": "sha256:" + "a" * 64,
        "interval_ms": [0, 2000],
        "target_interval_ms": [500, 1000],
        "target_cue_indexes": [13],
        "audio_parts": [
            {"role": role, "path": descriptor[path_field], "sha256": descriptor[hash_field]}
            for role, path_field, hash_field in (
                ("host_enrollment", "host_enrollment_path", "host_enrollment_sha256"),
                ("independent_source", "source_audio_path", "source_audio_sha256"),
                ("current_target", "target_audio_path", "target_audio_sha256"),
            )
        ],
    }
    observed = callbacks.observe_audio(request)
    assert observed["status"] == "OBSERVED"
    assert observed["target_interval_ms"] == [500, 1000]
    assert observed["target_cue_indexes"] == [13]
    assert len(agy_calls) == 1
    assert agy_calls[0][agy_calls[0].index("--model") + 1] == "Gemini 3.8 Flash (Low)"
    job_dir = Path(binding["job_dir"])
    assert (job_dir / "prompt.md").is_file()
    assert (job_dir / "agy.stdout").read_text(encoding="utf-8") == _provider_json()


@pytest.mark.parametrize("explicit_model", [None, "Gemini 3.6 Flash (Low)"])
def test_three_part_model_override_keeps_its_real_identity(tmp_path, monkeypatch, explicit_model):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    monkeypatch.setenv("AGY_THREE_PART_MODEL", "Gemini 3.8 Flash (High)")

    prepared = adapter.prepare_three_part_attachments(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", model=explicit_model,
    )

    assert prepared.transport_binding["agy"]["model"] == (
        explicit_model or "Gemini 3.8 Flash (High)"
    )


def test_structured_transport_binds_schema_flags_log_and_terminal_metadata(tmp_path, monkeypatch):
    from src.autoslice import structured_output
    parsed_stdout = []
    original_parse = structured_output.parse_agy_stream
    def parse_stdout(raw):
        parsed_stdout.append(raw)
        return original_parse(raw)
    monkeypatch.setattr(structured_output, 'parse_agy_stream', parse_stdout)
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    agy_calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        agy_calls.append(list(argv))
        return AgyRun(
            completed=subprocess.CompletedProcess(argv, 0, stdout=_provider_json(), stderr=""),
            failure_category=None,
        )

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", fake_run)

    def cpa(_prompt):
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    callbacks = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    observed = callbacks.observe_audio(_request(descriptor, callbacks.runtime_identity))
    assert observed["status"] == "OBSERVED"
    assert parsed_stdout == [_provider_json()]
    assert len(agy_calls) == 1

    binding = callbacks.runtime_identity["transport_binding"]
    assert binding["output_format"] == adapter.STRUCTURED_OUTPUT_FORMAT
    assert binding["structured_schema_version"] == adapter.STRUCTURED_SCHEMA_VERSION
    assert binding["structured_schema_sha256"].startswith("sha256:")
    argv = agy_calls[0]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    schema_path = Path(argv[argv.index("--json-schema") + 1])
    log_path = Path(argv[argv.index("--log-file") + 1])
    assert schema_path == Path(binding["job_dir"]) / adapter.STRUCTURED_SCHEMA_FILENAME
    assert log_path == Path(binding["job_dir"]) / adapter.STRUCTURED_LOG_FILENAME
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$id"] == adapter.STRUCTURED_SCHEMA_VERSION
    assert schema["allOf"]
    assert log_path.is_file()
    process = json.loads((Path(binding["job_dir"]) / "PROCESS.json").read_text(encoding="utf-8"))
    assert process["output_format"] == "stream-json"
    assert process["structured_result"]["status"] == "VALID"
    assert process["structured_result"]["tool_info_event_count"] == 2
    assert process["structured_result"]["step_state_counts"] == {"ACTIVE": 1, "DONE": 1}
    assert process["structured_result"]["done_tool_summaries"] == [{
        "step_type": "tool", "tool_name": "view_file", "step_index": 1,
        "path": "/fixture/neutralpart-1.mp4",
    }]
    assert process["log_sha256"].startswith("sha256:")
    assert process["log_created_by_cli"] is False
    assert process["log_file_exists"] is True
    assert process["stdout_sha256"].startswith("sha256:")


def test_structured_schema_hash_drift_blocks_dispatch(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    agy_calls: list[list[str]] = []

    def forbidden_run(argv, **_kwargs):
        agy_calls.append(list(argv))
        raise AssertionError("schema drift must block before AGY dispatch")

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", forbidden_run)
    cpa_calls: list[str] = []

    def cpa(prompt):
        cpa_calls.append(prompt)
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    callbacks = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="schema-drift",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    binding = callbacks.runtime_identity["transport_binding"]
    job_dir = Path(str(binding["job_dir"]))
    schema_path = job_dir / adapter.STRUCTURED_SCHEMA_FILENAME
    schema_path.write_text(schema_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    observed = callbacks.observe_audio(_request(descriptor, callbacks.runtime_identity))

    assert observed["status"] == "UNKNOWN"
    assert observed["evidence"]["reason_code"] == "AGY_STRUCTURED_SCHEMA_HASH_MISMATCH"
    assert agy_calls == []
    assert cpa_calls == []
    process = json.loads((job_dir / "PROCESS.json").read_text(encoding="utf-8"))
    assert process["status"] == "AGY_FAILED"
    assert process["failure_category"] == "STRUCTURED_SCHEMA_HASH_MISMATCH"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (json.dumps({"type": "step_update", "text": "still running"}), "AGY_STRUCTURED_RESULT_MISSING"),
        (json.dumps({"type": "error", "error": "provider failed"}), "AGY_STRUCTURED_RESULT_ERROR"),
        (json.dumps({"event": "result", "result": {"status": "SUCCESS"}}), "AGY_STRUCTURED_RESULT_PAYLOAD_INVALID"),
        ("not-json", "AGY_STRUCTURED_OUTPUT_INVALID"),
        ("\n", "AGY_STRUCTURED_OUTPUT_EMPTY"),
    ],
)
def test_structured_stream_requires_one_clean_terminal_role_payload(raw, reason):
    with pytest.raises(adapter.AgyThreePartAcousticError) as exc_info:
        adapter._parse_structured_stream(raw)
    assert exc_info.value.reason_code == reason


def test_terminal_error_rejects_valid_role_payload_and_keeps_typed_status():
    raw = json.dumps({
        "event": "result",
        "result": {
            "status": "ERROR",
            "error_code": 503,
            "error": "No capacity available for gemini-3.6-flash-low",
            "structured_output": _provider_payload(),
        },
    })
    with pytest.raises(adapter.AgyThreePartAcousticError) as exc_info:
        adapter._parse_structured_stream(raw)
    error = exc_info.value
    assert error.reason_code == "AGY_STRUCTURED_RESULT_ERROR"
    assert error.provider_error is True
    assert error.terminal_status == "ERROR"
    assert error.http_status == 503


def test_malformed_structured_stream_does_not_invent_http_status():
    with pytest.raises(adapter.AgyThreePartAcousticError) as exc_info:
        adapter._parse_structured_stream("not-json")
    error = exc_info.value
    assert error.http_status is None
    assert error.terminal_status is None


def test_structured_stream_accepts_nested_result_and_preserves_tool_info_receipt():
    payload = _provider_payload()
    raw = "\n".join([
        json.dumps({"type": "init", "session_id": "fixture"}),
        json.dumps({
            "event": "step_update",
            "step_update": {
                "step_index": 4, "state": "DONE", "step_type": "tool",
                "tool_name": "view_file",
                "tool_info": {"name": "view_file", "parameters": {"AbsolutePath": "/fixture/audio.mp4"}},
            },
        }),
        json.dumps({"event": "result", "result": {"status": "SUCCESS", "structured_output": payload}}),
    ])
    completion, metadata = adapter._parse_structured_stream(raw)
    assert json.loads(completion) == payload
    assert metadata["tool_info_event_count"] == 1
    assert metadata["terminal_result_event_index"] == 2
    assert metadata["step_state_counts"] == {"DONE": 1}
    assert metadata["done_tool_summaries"][0]["path"] == "/fixture/audio.mp4"


@pytest.mark.parametrize(
    "result_wrapper",
    [
        {"status": "FAILED", "structured_output": _provider_payload()},
        {"status": "SUCCESS", "is_error": True, "structured_output": _provider_payload()},
        {"status": "SUCCESS", "error": "provider failed", "structured_output": _provider_payload()},
    ],
)
def test_structured_terminal_wrapper_failure_rejects_valid_role_payload(result_wrapper):
    raw = json.dumps({"event": "result", "result": result_wrapper})
    with pytest.raises(adapter.AgyThreePartAcousticError) as exc_info:
        adapter._parse_structured_stream(raw)
    assert exc_info.value.reason_code == "AGY_STRUCTURED_RESULT_ERROR"


def test_structured_terminal_failure_stays_unknown_without_cpa_or_retry(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    agy_calls: list[list[str]] = []

    def failed_result(argv, **_kwargs):
        agy_calls.append(list(argv))
        return AgyRun(
            completed=subprocess.CompletedProcess(
                argv, 0,
                stdout=json.dumps({
                    "event": "result",
                    "result": {
                        "status": "ERROR",
                        "error_code": 503,
                        "error": "No capacity available for gemini-3.6-flash-low",
                        "structured_output": _provider_payload(),
                    },
                }) + "\n",
                stderr="",
            ),
            failure_category=None,
        )

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", failed_result)
    cpa_calls: list[str] = []

    def cpa(prompt):
        cpa_calls.append(prompt)
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    first = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="bad-stream",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    observed = first.observe_audio(_request(descriptor, first.runtime_identity))
    assert observed["status"] == "UNKNOWN"
    evidence = observed["evidence"]
    assert evidence["reason_code"] == "PROVIDER_ERROR"
    assert evidence["structured_reason_code"] == "AGY_STRUCTURED_RESULT_ERROR"
    assert evidence["terminal_status"] == "ERROR"
    assert evidence["http_status"] == 503
    assert cpa_calls == []
    assert len(agy_calls) == 1
    process_path = Path(str(first.runtime_identity["transport_binding"]["job_dir"])) / "PROCESS.json"
    process = json.loads(process_path.read_text(encoding="utf-8"))
    assert process["structured_result"] == {
        "status": "INVALID",
        "reason_code": "AGY_STRUCTURED_RESULT_ERROR",
        "raw_sha256": process["structured_result"]["raw_sha256"],
        "terminal_status": "ERROR",
        "http_status": 503,
    }

    monkeypatch.setattr(
        adapter.agy_gemini_client, "run_local_agy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("structured error retried")),
    )
    second = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="bad-stream",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    replay = second.observe_audio(_request(descriptor, second.runtime_identity))
    assert replay["status"] == "UNKNOWN"
    assert replay["evidence"]["reason_code"] == "PROVIDER_ERROR"
    assert replay["evidence"]["structured_reason_code"] == "AGY_STRUCTURED_RESULT_ERROR"
    assert replay["evidence"]["terminal_status"] == "ERROR"
    assert replay["evidence"]["http_status"] == 503
    assert len(agy_calls) == 1


def test_legacy_text_completion_replay_remains_explicit_and_immutable(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    agy_calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        agy_calls.append(list(argv))
        return AgyRun(
            completed=subprocess.CompletedProcess(argv, 0, stdout=_legacy_provider_json(), stderr=""),
            failure_category=None,
        )

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", fake_run)

    def cpa(_prompt):
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    first = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="legacy-cue",
        evidence_root=tmp_path / "evidence", cpa_call=cpa, output_format="text",
    )
    first_result = first.observe_audio(_request(descriptor, first.runtime_identity))
    assert first_result["status"] == "OBSERVED"
    assert first.runtime_identity["transport_binding"].get("output_format") is None
    assert agy_calls[0][agy_calls[0].index('--output-format') + 1] == 'stream-json'
    assert '--json-schema' in agy_calls[0]
    job_dir = Path(first.runtime_identity["transport_binding"]["job_dir"])
    assert (job_dir / "agy.stdout").read_text(encoding="utf-8") == _legacy_provider_json()

    monkeypatch.setattr(
        adapter.agy_gemini_client, "run_local_agy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy replay redispatched")),
    )
    second = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="legacy-cue",
        evidence_root=tmp_path / "evidence", cpa_call=cpa, output_format="text",
    )
    replayed = second.observe_audio(_request(descriptor, second.runtime_identity))
    assert replayed["status"] == "OBSERVED"


def test_voice_reference_prompt_identity_is_forwarded_to_agy_runtime(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    voice_observer = "voice-reference observer prompt"
    voice_cpa = "voice-reference CPA prompt"

    def cpa(_prompt):
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    callbacks = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
        observer_prompt=voice_observer, cpa_prompt=voice_cpa,
        attachment_instructions=adapter.VOICE_REFERENCE_ATTACHMENT_INSTRUCTIONS,
    )
    assert callbacks.runtime_identity["observer_prompt_sha256"] == (
        "sha256:" + hashlib.sha256(voice_observer.encode()).hexdigest()
    )
    assert callbacks.runtime_identity["cpa_prompt_sha256"] == (
        "sha256:" + hashlib.sha256(voice_cpa.encode()).hexdigest()
    )
    prompt_path = Path(callbacks.runtime_identity["transport_binding"]["job_dir"]) / "prompt.md"
    assert "UNALIGNED VOICE REFERENCE" in prompt_path.read_text(encoding="utf-8")


def test_voice_reference_attachment_binding_reaches_fake_agy_transport(
    tmp_path, monkeypatch
):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    agy_calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        agy_calls.append(list(argv))
        return AgyRun(
            completed=subprocess.CompletedProcess(argv, 0, stdout=_provider_json(), stderr=""),
            failure_category=None,
        )

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", fake_run)

    def cpa(_prompt):
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    callbacks = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
        observer_prompt="voice-reference observer prompt",
        cpa_prompt="voice-reference CPA prompt",
        attachment_instructions=adapter.VOICE_REFERENCE_ATTACHMENT_INSTRUCTIONS,
    )

    observed = callbacks.observe_audio(_request(descriptor, callbacks.runtime_identity))

    assert observed["status"] == "OBSERVED"
    assert len(agy_calls) == 1
    binding = callbacks.runtime_identity["transport_binding"]
    prompt_path = Path(str(binding["job_dir"])) / "prompt.md"
    assert "UNALIGNED VOICE REFERENCE" in prompt_path.read_text(encoding="utf-8")
    process = json.loads((prompt_path.parent / "PROCESS.json").read_text(encoding="utf-8"))
    assert process["job_invocations"] == 1


def test_completed_job_replays_exact_stdout_across_callbacks_without_new_agy(
    tmp_path, monkeypatch
):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    agy_calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        agy_calls.append(list(argv))
        return AgyRun(
            completed=subprocess.CompletedProcess(argv, 0, stdout=_provider_json(), stderr=""),
            failure_category=None,
        )

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", fake_run)

    def cpa(_prompt):
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    first = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    first_result = first.observe_audio(_request(descriptor, first.runtime_identity))
    assert first_result["status"] == "OBSERVED"
    assert len(agy_calls) == 1

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("completed AGY job must replay without a new call")

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", forbidden_run)

    def cpa_changed(_prompt):
        return "{}"

    cpa_changed.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "high"
    }
    second = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa_changed,
    )
    assert second.runtime_identity["identity_sha256"] != first.runtime_identity["identity_sha256"]
    assert second.runtime_identity["transport_binding"] == first.runtime_identity["transport_binding"]
    second_result = second.observe_audio(_request(descriptor, second.runtime_identity))
    assert second_result["status"] == "OBSERVED"
    attempts = second_result["evidence"]["provider_attempts"]
    assert attempts[0]["job_invocations"] == 0
    assert attempts[0]["historical_job_invocations"] == 1
    assert attempts[0]["replayed_completion"] is True
    assert len(agy_calls) == 1


def test_completed_job_raw_stdout_drift_is_unknown_without_new_agy(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    agy_calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        agy_calls.append(list(argv))
        return AgyRun(
            completed=subprocess.CompletedProcess(argv, 0, stdout=_provider_json(), stderr=""),
            failure_category=None,
        )

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", fake_run)

    def cpa(_prompt):
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    first = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    first.observe_audio(_request(descriptor, first.runtime_identity))
    stdout_path = Path(str(first.runtime_identity["transport_binding"]["job_dir"])) / "agy.stdout"
    stdout_path.write_text(_provider_json() + "\nraw drift", encoding="utf-8")
    monkeypatch.setattr(
        adapter.agy_gemini_client, "run_local_agy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("raw drift must not redispatch")),
    )
    second = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    result = second.observe_audio(_request(descriptor, second.runtime_identity))
    assert result["status"] == "UNKNOWN"
    assert result["evidence"]["reason_code"] == "AGY_COMPLETED_STDOUT_DRIFT"
    assert len(agy_calls) == 1


def test_running_job_is_unknown_without_redispatch_across_callbacks(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    agy_calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        agy_calls.append(list(argv))
        return AgyRun(
            completed=subprocess.CompletedProcess(argv, 0, stdout=_provider_json(), stderr=""),
            failure_category=None,
        )

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", fake_run)

    def cpa(_prompt):
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    first = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    first.observe_audio(_request(descriptor, first.runtime_identity))
    process_path = Path(str(first.runtime_identity["transport_binding"]["job_dir"])) / "PROCESS.json"
    process = json.loads(process_path.read_text(encoding="utf-8"))
    process["status"] = "AGY_RUNNING"
    process_path.write_text(json.dumps(process), encoding="utf-8")

    def cpa_changed(_prompt):
        return "{}"

    cpa_changed.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "high"
    }
    monkeypatch.setattr(
        adapter.agy_gemini_client, "run_local_agy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("AGY_RUNNING job must not redispatch")
        ),
    )
    second = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa_changed,
    )
    result = second.observe_audio(_request(descriptor, second.runtime_identity))
    assert result["status"] == "UNKNOWN"
    assert result["evidence"]["reason_code"] == "AGY_RUNNING_REPLAY_BLOCKED"
    attempts = result["evidence"]["provider_attempts"]
    assert attempts[0]["job_invocations"] == 0
    assert attempts[0]["historical_job_invocations"] == 1
    assert agy_calls and len(agy_calls) == 1


def test_agy_failure_is_unknown_and_does_not_fall_back_to_cpa(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)

    def failed_run(*_args, **_kwargs):
        return AgyRun(
            completed=None,
            failure_category="AGY_TIMEOUT",
            launch_error_type="TimeoutExpired",
            launch_error=RuntimeError("bounded fixture failure"),
        )

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", failed_run)
    cpa_calls = []

    def cpa(prompt):
        cpa_calls.append(prompt)
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    callbacks = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    request = {
        "candidate_id": "candidate", "observation_id": "cue-13",
        "runtime_identity": callbacks.runtime_identity,
        "request_sha256": "sha256:" + "a" * 64, "interval_ms": [0, 2000],
        "target_interval_ms": [500, 1000], "target_cue_indexes": [13],
        "audio_parts": [
            {"role": role, "path": descriptor[path_field], "sha256": descriptor[hash_field]}
            for role, path_field, hash_field in (
                ("host_enrollment", "host_enrollment_path", "host_enrollment_sha256"),
                ("independent_source", "source_audio_path", "source_audio_sha256"),
                ("current_target", "target_audio_path", "target_audio_sha256"),
            )
        ],
    }
    observed = callbacks.observe_audio(request)
    assert observed["status"] == "UNKNOWN"
    assert observed["evidence"]["reason_code"] == "AGY_JOB_FAILED"
    assert cpa_calls == []

    second_run_calls = []

    def forbidden_retry(*_args, **_kwargs):
        second_run_calls.append(True)
        raise AssertionError("AGY_FAILED job must not retry unchanged")

    monkeypatch.setattr(adapter.agy_gemini_client, "run_local_agy", forbidden_retry)
    second = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    replay_blocked = second.observe_audio(_request(descriptor, second.runtime_identity))
    assert replay_blocked["status"] == "UNKNOWN"
    assert replay_blocked["evidence"]["reason_code"] == "AGY_FAILED_REPLAY_BLOCKED"
    attempts = replay_blocked["evidence"]["provider_attempts"]
    assert attempts[0]["job_invocations"] == 0
    assert attempts[0]["historical_job_invocations"] == 1
    assert second_run_calls == []


def test_failed_render_partial_is_preserved_and_next_attempt_rerenders_all_parts(
    tmp_path, monkeypatch
):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    ffmpeg_calls: list[list[str]] = []

    def render(argv, **_kwargs):
        ffmpeg_calls.append(list(argv))
        output = Path(argv[-1])
        if len(ffmpeg_calls) == 1:
            output.write_bytes(b"partial-render")
            return SimpleNamespace(returncode=1, stdout="", stderr="fixture render failure")
        output.write_bytes(("mp4:" + argv[argv.index("-i", 2) + 1]).encode())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adapter.subprocess, "run", render)
    with pytest.raises(adapter.AgyThreePartAcousticError, match="AGY_ATTACHMENT_RENDER_FAILED"):
        adapter.prepare_three_part_attachments(
            descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
            evidence_root=tmp_path / "evidence",
        )
    process_paths = list((tmp_path / "evidence").rglob("PROCESS.json"))
    assert len(process_paths) == 1
    failed_process_path = process_paths[0]
    failed_job_dir = failed_process_path.parent
    partial_path = failed_job_dir / "neutralpart-1.mp4"
    assert partial_path.read_bytes() == b"partial-render"
    assert json.loads(failed_process_path.read_text(encoding="utf-8"))["status"] == "FAILED_RENDER"

    prepared = adapter.prepare_three_part_attachments(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence",
    )
    assert prepared.job_dir != failed_job_dir
    assert len(ffmpeg_calls) == 4
    assert partial_path.read_bytes() == b"partial-render"
    assert json.loads(prepared.process_path.read_text(encoding="utf-8"))["status"] == "PREPARED"
    replay = adapter.prepare_three_part_attachments(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence",
    )
    assert replay.job_dir == prepared.job_dir
    assert len(ffmpeg_calls) == 4


def test_source_hash_change_gets_new_attachment_identity(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)
    def cpa(_prompt):
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    first = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    target = Path(str(descriptor["target_audio_path"]))
    target.write_bytes(target.read_bytes() + b"drift")
    descriptor["target_audio_sha256"] = _sha(target)
    second = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    assert first.runtime_identity["identity_sha256"] != second.runtime_identity["identity_sha256"]
    assert first.runtime_identity["transport_binding"]["job_dir"] != second.runtime_identity["transport_binding"]["job_dir"]


def test_narrow_target_wav_cannot_claim_full_context_clock(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    target = Path(str(descriptor["target_audio_path"]))
    _wav(target, frames=8000, value=300)
    descriptor["target_audio_sha256"] = _sha(target)
    _patch_local_tools(monkeypatch)
    with pytest.raises(adapter.AgyThreePartAcousticError, match="TARGET_AUDIO_WINDOW_DURATION_MISMATCH"):
        adapter.prepare_three_part_attachments(descriptor=descriptor, candidate_id="candidate",
            observation_id="cue-13", evidence_root=tmp_path / "evidence")
    descriptor["target_audio_window_ms"] = [500, 1000]
    prepared = adapter.prepare_three_part_attachments(descriptor=descriptor, candidate_id="candidate",
        observation_id="cue-13", evidence_root=tmp_path / "evidence")
    assert '"target_audio_offset_ms":[0,500]' in prepared.prompt_path.read_text()


def test_prompt_and_derived_attachment_drift_cannot_reuse_prepared_artifact(
    tmp_path, monkeypatch
):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)

    first = adapter.prepare_three_part_attachments(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence",
    )
    first_prompt = first.prompt_path.read_text(encoding="utf-8")
    first.prompt_path.write_text(first_prompt + "\nchanged", encoding="utf-8")
    try:
        adapter.prepare_three_part_attachments(
            descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
            evidence_root=tmp_path / "evidence",
        )
    except adapter.AgyThreePartAcousticError as exc:
        assert str(exc) == "AGY_ARTIFACT_BINDING_MISMATCH"
    else:
        raise AssertionError("prompt drift must not reuse the prepared artifact")

    first.prompt_path.write_text(first_prompt, encoding="utf-8")
    derived = Path(str(first.derived_parts[0]["path"]))
    derived.write_bytes(derived.read_bytes() + b"drift")
    try:
        adapter.prepare_three_part_attachments(
            descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
            evidence_root=tmp_path / "evidence",
        )
    except adapter.AgyThreePartAcousticError as exc:
        assert str(exc) == "AGY_ARTIFACT_BINDING_MISMATCH"
    else:
        raise AssertionError("derived attachment drift must not reuse the prepared artifact")


def test_observer_prompt_change_changes_normal_identity(tmp_path, monkeypatch):
    descriptor = _descriptor(tmp_path)
    _patch_local_tools(monkeypatch)

    def cpa(_prompt):
        return "{}"

    cpa.cpa_cache_identity = {
        "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
    }
    first = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
    )
    second = adapter.build_three_part_callbacks(
        descriptor=descriptor, candidate_id="candidate", observation_id="cue-13",
        evidence_root=tmp_path / "evidence", cpa_call=cpa,
        observer_prompt=adapter.OBSERVER_PROMPT + "\nPrompt revision.",
    )
    assert first.runtime_identity["identity_sha256"] != second.runtime_identity["identity_sha256"]
    assert first.runtime_identity["transport_binding"]["attachment_prompt_sha256"] != (
        second.runtime_identity["transport_binding"]["attachment_prompt_sha256"]
    )


def test_prepared_observer_factory_receives_audio_bound_descriptor(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    seen: list[dict[str, object]] = []

    def factory(**kwargs):
        seen.append(kwargs)
        return callbacks

    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks_factory=factory,
    )
    assert result["observations"][0]["observer_result"]["status"] == "PASS"
    assert len(seen) == 1
    descriptor = seen[0]["descriptor"]
    assert all(field in descriptor for field in (
        "host_enrollment_path", "source_audio_path", "target_audio_path",
        "host_enrollment_sha256", "source_audio_sha256", "target_audio_sha256",
    ))


def test_explicit_agy_transport_selects_factory_without_gemini_setup(tmp_path, monkeypatch):
    import src.autoslice.producer_final_review_transport as final_transport
    import src.autoslice.producer_nested_acoustic as producer

    cpa = SimpleNamespace(
        cpa_cache_identity={
            "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"
        }
    )
    monkeypatch.setattr(final_transport, "build_final_review_llm_call", lambda **_kwargs: cpa)
    captured: dict[str, object] = {}

    def fake_observe_prepared(**kwargs):
        captured.update(kwargs)
        return {"route": "agy_three_part"}

    monkeypatch.setattr(producer, "observe_prepared_nested_acoustics", fake_observe_prepared)
    built: dict[str, object] = {}

    def fake_build_callbacks(**kwargs):
        built.update(kwargs)
        return "callbacks"

    monkeypatch.setattr(adapter, "build_three_part_callbacks", fake_build_callbacks)
    observe = producer.build_runtime_nested_acoustic_observer(tmp_path)
    result = observe(spec={"nested_media_acoustic": {"observer_transport": "agy_three_part"}})

    assert result == {"route": "agy_three_part"}
    assert callable(captured["callbacks_factory"])
    assert captured["source_cache_root"] == tmp_path / "cache" / "nested-sources"
    assert captured["callbacks_factory"](
        descriptor={}, candidate_id="candidate", observation_id="observation",
        evidence_root=tmp_path,
    ) == "callbacks"
    assert built["cpa_call"] is cpa
