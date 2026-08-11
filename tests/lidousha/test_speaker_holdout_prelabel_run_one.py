from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.autoslice import speaker_holdout_prelabel as prelabel
from scripts import run_speaker_holdout_prelabel_one as wrapper
from tests.lidousha.test_speaker_holdout_prelabel_plan import _build, _inputs


def _authorized(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path, str]:
    values = _inputs(tmp_path)
    payload, _, _ = _build(values)
    runner = tmp_path / "run-one-wrapper.py"
    runner.write_text("# frozen fixture runner\n", encoding="utf-8")
    payload["status"] = prelabel.EXECUTION_STATUS
    payload["execution_contract"]["plan_only"] = False
    payload["execution_contract"]["real_provider_execution_authorized"] = True
    payload["execution_contract"]["external_audio_upload_authorized"] = True
    payload["toolchain"]["hash_bound_run_one_wrapper"] = {
        "path": str(runner),
        "sha256": prelabel.file_sha256(runner),
    }
    payload["toolchain"]["runtime_binding_state"] = prelabel.EXECUTION_RUNTIME_STATE
    payload["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {key: value for key, value in payload.items() if key != "deterministic_payload_sha256"}
    )
    segment_id = payload["sessions"][0]["segments"][0]["segment_id"]
    return payload, values, runner, segment_id


def _result(*, elapsed_s: float = 1.0) -> dict[str, object]:
    return {
        "provider": "bcut",
        "elapsed_s": elapsed_s,
        "utterances": [
            {
                "start_time": 0,
                "end_time": 1000,
                "transcript": "测试",
                "words": [
                    {"label": "测", "start_time": 0, "end_time": 500},
                    {"label": "试", "start_time": 500, "end_time": 1000},
                ],
            }
        ],
    }


def _run(
    payload: dict[str, object],
    runner: Path,
    segment_id: str,
    *,
    attempt_id: str = "attempt-001",
    result: dict[str, object] | None = None,
    provider=None,
) -> dict[str, object]:
    return prelabel.run_one(
        plan=payload,
        segment_id=segment_id,
        attempt_id=attempt_id,
        runner_path=runner,
        source_to_mp3=lambda _path: b"fixture-mp3",
        mp3_to_pcm_s16le=lambda _mp3: b"\0\0" * 16_000,
        transcribe_bcut=provider or (lambda _mp3: result or _result()),
    )


def test_current_unexecuted_plan_cannot_call_provider(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    payload, _, _ = _build(values)
    called = False

    def provider(_mp3: bytes) -> dict[str, object]:
        nonlocal called
        called = True
        return _result()

    segment_id = payload["sessions"][0]["segments"][0]["segment_id"]
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="not authorized"):
        prelabel.run_one(
            plan=payload,
            segment_id=segment_id,
            attempt_id="attempt-001",
            runner_path=Path(__file__),
            source_to_mp3=lambda _path: b"mp3",
            mp3_to_pcm_s16le=lambda _mp3: b"\0\0" * 16_000,
            transcribe_bcut=provider,
        )
    assert called is False
    assert list(values["scratch"].iterdir()) == []


def test_plan_rename_swap_cannot_hash_one_file_and_parse_another(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, _, _, _ = _authorized(tmp_path)
    plan_path = tmp_path / "plan.json"
    replacement_path = tmp_path / "replacement.json"
    plan_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    replacement = copy.deepcopy(payload)
    replacement["status"] = "EXTRACTION_PLANNED"
    replacement["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {
            key: value
            for key, value in replacement.items()
            if key != "deterministic_payload_sha256"
        }
    )
    replacement_path.write_text(
        json.dumps(replacement, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    expected_hash = prelabel.file_sha256(plan_path)
    target = plan_path.resolve(strict=True)
    real_open = os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and isinstance(path, (str, bytes, os.PathLike)):
            if Path(path) == target:
                os.replace(replacement_path, plan_path)
                swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(prelabel.os, "open", swap_before_open)
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="file hash drifted"):
        prelabel.load_plan(plan_path, expected_file_sha256=expected_hash)
    assert swapped is True


def test_mocked_run_one_writes_exact_artifacts_and_receipt_last(tmp_path: Path) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    receipt = _run(payload, runner, segment_id)
    attempt = (
        Path(payload["scratch"]["planned_run_root"])
        / "segments"
        / segment_id
        / "attempt-001"
    )
    assert receipt["status"] == "ASR_CUE_GRID_SEGMENT_FROZEN"
    assert receipt["cue_count"] == 1
    assert receipt["authority"]["human_truth_opened"] is False
    assert receipt["authority"]["predictions_frozen"] is False
    assert {path.name for path in attempt.iterdir()} == {
        "asr-input-16k-mono-64k.mp3",
        "canonical-pcm.s16le",
        "asr.normalized.json",
        "cue-table.json",
        "asr.srt",
        "extraction-receipt.json",
    }
    persisted = json.loads((attempt / "extraction-receipt.json").read_text(encoding="utf-8"))
    assert persisted == receipt
    cue_table = json.loads((attempt / "cue-table.json").read_text(encoding="utf-8"))
    assert cue_table["truth_state"] == "UNLABELED_LOCKED"
    assert cue_table["prediction_state"] == "NOT_RUN"


def test_attempt_namespace_is_parent_fsynced_before_source_or_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    events: list[str] = []
    real_fsync = os.fsync

    def tracking_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def source_to_mp3(_path: Path) -> bytes:
        events.append("source")
        return b"fixture-mp3"

    def provider(_mp3: bytes) -> dict[str, object]:
        events.append("provider")
        return _result()

    monkeypatch.setattr(prelabel.os, "fsync", tracking_fsync)
    prelabel.run_one(
        plan=payload,
        segment_id=segment_id,
        attempt_id="attempt-001",
        runner_path=runner,
        source_to_mp3=source_to_mp3,
        mp3_to_pcm_s16le=lambda _mp3: b"\0\0" * 16_000,
        transcribe_bcut=provider,
    )
    assert events[:5] == ["fsync", "fsync", "fsync", "fsync", "source"]
    assert events.index("provider") > events.index("source")


def test_legacy_v0_plan_is_validate_only_even_if_authority_flags_are_forged(
    tmp_path: Path,
) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    payload["schema_version"] = prelabel.LEGACY_PLAN_SCHEMA
    payload["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {key: value for key, value in payload.items() if key != "deterministic_payload_sha256"}
    )
    called = False

    def provider(_mp3: bytes) -> dict[str, object]:
        nonlocal called
        called = True
        return _result()

    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="validate-only"):
        _run(payload, runner, segment_id, provider=provider)
    assert called is False


def test_artifact_contract_drift_stops_before_provider_or_run_root(tmp_path: Path) -> None:
    payload, values, runner, segment_id = _authorized(tmp_path)
    payload["sessions"][0]["segments"][0]["outputs"]["asr_normalized_json"] = "asr.raw.json"
    payload["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {key: value for key, value in payload.items() if key != "deterministic_payload_sha256"}
    )
    called = False

    def provider(_mp3: bytes) -> dict[str, object]:
        nonlocal called
        called = True
        return _result()

    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="artifact contract drifted"):
        _run(payload, runner, segment_id, provider=provider)
    assert called is False
    assert list(values["scratch"].iterdir()) == []


def test_bound_wrapper_uses_pipe_only_ffmpeg_and_direct_bcut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, values, _, _ = _authorized(tmp_path)
    asr_client = values["asr_script"]
    asr_client.write_text(
        "BCUT_MODEL_ID = '7'\n"
        f"BCUT_BASE = {wrapper.BCUT_BASE!r}\n"
        "def transcribe(*args, **kwargs):\n"
        "    raise AssertionError('auto dispatcher must not be used')\n"
        "def transcribe_bcut(sound, *, poll_interval, poll_timeout, log):\n"
        "    assert sound == b'fixture-mp3'\n"
        "    assert poll_interval == 3 and poll_timeout == 900\n"
        "    return {'utterances': []}\n",
        encoding="utf-8",
    )
    core = Path(prelabel.__file__).resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    payload["toolchain"]["free_asr_client"] = {
        "path": str(asr_client),
        "sha256": prelabel.file_sha256(asr_client),
    }
    payload["toolchain"]["run_one_core"] = {
        "path": str(core),
        "sha256": prelabel.file_sha256(core),
    }
    payload["toolchain"]["python"] = {
        "path": str(python),
        "sha256": prelabel.file_sha256(python),
    }
    payload["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {key: value for key, value in payload.items() if key != "deterministic_payload_sha256"}
    )
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(wrapper, "EXECUTION_SCRATCH_ROOT", values["scratch"])
    for name in wrapper.FORBIDDEN_NETWORK_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    def fake_process(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = b"fixture-mp3" if len(calls) == 1 else b"\0\0" * 16_000
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

    source_to_mp3, mp3_to_pcm, transcribe = wrapper.build_execution_callbacks(
        payload,
        runner_path=Path(payload["toolchain"]["hash_bound_run_one_wrapper"]["path"]),
        process_runner=fake_process,
    )
    source = Path(payload["sessions"][0]["segments"][0]["source"]["path"])
    assert source_to_mp3(source) == b"fixture-mp3"
    assert mp3_to_pcm(b"fixture-mp3") == b"\0\0" * 16_000
    assert transcribe(b"fixture-mp3") == {"utterances": [], "provider": "bcut"}
    assert len(calls) == 2
    ffmpeg = payload["toolchain"]["ffmpeg"]["path"]
    assert calls[0][0] == [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+bitexact",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        "-map_metadata",
        "-1",
        "-threads",
        "1",
        "-f",
        "mp3",
        "pipe:1",
    ]
    assert calls[1][0] == [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+bitexact",
        "-f",
        "mp3",
        "-i",
        "pipe:0",
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-threads",
        "1",
        "-f",
        "s16le",
        "pipe:1",
    ]
    assert all("-y" not in argv for argv, _ in calls)
    assert calls[0][1]["input"] is None
    assert calls[1][1]["input"] == b"fixture-mp3"
    assert all(call[1]["env"] == wrapper.SUBPROCESS_ENV for call in calls)


def test_wrapper_binding_drift_fails_before_ffmpeg_or_asr_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, values, _, _ = _authorized(tmp_path)
    payload["toolchain"]["ffmpeg"]["sha256"] = "sha256:" + "0" * 64
    payload["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {key: value for key, value in payload.items() if key != "deterministic_payload_sha256"}
    )
    called = False
    monkeypatch.setattr(wrapper, "EXECUTION_SCRATCH_ROOT", values["scratch"])

    def fake_process(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("ffmpeg must not run")

    with pytest.raises(wrapper.HoldoutPrelabelWrapperError, match="ffmpeg binding drifted"):
        wrapper.build_execution_callbacks(
            payload,
            runner_path=Path(payload["toolchain"]["hash_bound_run_one_wrapper"]["path"]),
            process_runner=fake_process,
        )
    assert called is False
    assert list(values["scratch"].iterdir()) == []


def test_executor_rejects_plan_only_and_scratch_escape_before_client_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, values, _, _ = _authorized(tmp_path)
    runner = Path(payload["toolchain"]["hash_bound_run_one_wrapper"]["path"])
    monkeypatch.setattr(wrapper, "EXECUTION_SCRATCH_ROOT", values["scratch"])

    payload["execution_contract"]["plan_only"] = True
    payload["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {key: value for key, value in payload.items() if key != "deterministic_payload_sha256"}
    )
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="not authorized"):
        wrapper.build_execution_callbacks(payload, runner_path=runner)

    payload["execution_contract"]["plan_only"] = False
    payload["scratch"]["allowed_root"] = "/opt/bilive/autoslice/state"
    payload["scratch"]["planned_run_root"] = "/opt/bilive/autoslice/state/escape"
    payload["scratch"]["plan_path"] = "/opt/bilive/autoslice/state/escape.plan.json"
    payload["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {key: value for key, value in payload.items() if key != "deterministic_payload_sha256"}
    )
    with pytest.raises(wrapper.HoldoutPrelabelWrapperError, match="scratch/production"):
        wrapper.build_execution_callbacks(payload, runner_path=runner)
    assert list(values["scratch"].iterdir()) == []


@pytest.mark.parametrize(
    "environment_name",
    ["HTTPS_PROXY", "SSLKEYLOGFILE", "SSL_CERT_FILE", "SSL_CERT_DIR"],
)
def test_ambient_network_environment_stops_before_asr_client_source_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
) -> None:
    payload, values, _, _ = _authorized(tmp_path)
    asr_client = values["asr_script"]
    asr_client.write_text("raise AssertionError('ASR source executed')\n", encoding="utf-8")
    core = Path(prelabel.__file__).resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    payload["toolchain"]["free_asr_client"] = {
        "path": str(asr_client),
        "sha256": prelabel.file_sha256(asr_client),
    }
    payload["toolchain"]["run_one_core"] = {
        "path": str(core),
        "sha256": prelabel.file_sha256(core),
    }
    payload["toolchain"]["python"] = {
        "path": str(python),
        "sha256": prelabel.file_sha256(python),
    }
    payload["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {key: value for key, value in payload.items() if key != "deterministic_payload_sha256"}
    )
    monkeypatch.setattr(wrapper, "EXECUTION_SCRATCH_ROOT", values["scratch"])
    for name in wrapper.FORBIDDEN_NETWORK_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    sentinel = tmp_path / f"{environment_name.lower()}.sentinel"
    monkeypatch.setenv(environment_name, str(sentinel))
    callbacks = wrapper.build_execution_callbacks(
        payload,
        runner_path=Path(payload["toolchain"]["hash_bound_run_one_wrapper"]["path"]),
    )
    with pytest.raises(wrapper.HoldoutPrelabelWrapperError, match="network environment"):
        callbacks[2](b"fixture-mp3")
    assert not sentinel.exists()


def test_elapsed_observation_does_not_change_normalized_asr(tmp_path: Path) -> None:
    first = prelabel.normalize_asr(_result(elapsed_s=1.0), pcm_sample_count=16_000)
    second = prelabel.normalize_asr(_result(elapsed_s=999.0), pcm_sample_count=16_000)
    assert first == second
    assert prelabel.canonical_sha256(first) == prelabel.canonical_sha256(second)


def test_empty_asr_segment_is_retained_with_receipt(tmp_path: Path) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    result = {"provider": "bcut", "elapsed_s": 2.0, "utterances": []}
    receipt = _run(payload, runner, segment_id, result=result)
    assert receipt["status"] == "ASR_EMPTY_SEGMENT_RETAINED"
    assert receipt["cue_count"] == 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.update(start_time=-1), "bounds or text"),
        (lambda row: row.update(start_time=True), "bounds or text"),
        (lambda row: row.update(end_time=1001), "bounds or text"),
        (lambda row: row.update(transcript=""), "bounds or text"),
        (
            lambda row: row["words"][0].update(end_time=1001),
            "word 1 bounds or label",
        ),
    ],
)
def test_malformed_cues_fail_closed(tmp_path: Path, mutation, message: str) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    result = _result()
    mutation(result["utterances"][0])
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match=message):
        _run(payload, runner, segment_id, result=result)


def test_overlapping_cues_fail_closed(tmp_path: Path) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    result = _result()
    result["utterances"].append(
        {"start_time": 999, "end_time": 1000, "transcript": "重叠", "words": []}
    )
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="cue 2"):
        _run(payload, runner, segment_id, result=result)


def test_source_drift_stops_before_provider_call(tmp_path: Path) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    source = Path(payload["sessions"][0]["segments"][0]["source"]["path"])
    source.write_bytes(source.read_bytes() + b"drift")
    called = False

    def provider(_mp3: bytes) -> dict[str, object]:
        nonlocal called
        called = True
        return _result()

    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="stat drifted"):
        _run(payload, runner, segment_id, provider=provider)
    assert called is False


def test_same_stat_source_byte_drift_stops_before_provider_call(tmp_path: Path) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    source = Path(payload["sessions"][0]["segments"][0]["source"]["path"])
    original_stat = source.stat()
    called = False

    def mutate_source(_path: Path) -> bytes:
        original = source.read_bytes()
        source.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return b"fixture-mp3"

    def provider(_mp3: bytes) -> dict[str, object]:
        nonlocal called
        called = True
        return _result()

    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="during input extraction"):
        prelabel.run_one(
            plan=payload,
            segment_id=segment_id,
            attempt_id="attempt-001",
            runner_path=runner,
            source_to_mp3=mutate_source,
            mp3_to_pcm_s16le=lambda _mp3: b"\0\0" * 16_000,
            transcribe_bcut=provider,
        )
    assert called is False


def test_unknown_segment_and_unsafe_attempt_fail_before_provider(tmp_path: Path) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    called = False

    def provider(_mp3: bytes) -> dict[str, object]:
        nonlocal called
        called = True
        return _result()

    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="exactly one"):
        _run(payload, runner, "not-in-plan", provider=provider)
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="safe path"):
        _run(payload, runner, segment_id, attempt_id="../escape", provider=provider)
    assert called is False


def test_provider_failure_leaves_reserved_attempt_without_success_receipt(tmp_path: Path) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)

    def fail(_mp3: bytes) -> dict[str, object]:
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        _run(payload, runner, segment_id, provider=fail)
    attempt = (
        Path(payload["scratch"]["planned_run_root"])
        / "segments"
        / segment_id
        / "attempt-001"
    )
    assert attempt.is_dir()
    assert not (attempt / "extraction-receipt.json").exists()
    assert list(attempt.iterdir()) == []


def test_retrying_same_attempt_is_rejected_before_second_upload(tmp_path: Path) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    _run(payload, runner, segment_id)
    called = False

    def provider(_mp3: bytes) -> dict[str, object]:
        nonlocal called
        called = True
        return _result()

    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="already exists"):
        _run(payload, runner, segment_id, provider=provider)
    assert called is False


def test_runner_and_plan_hash_drift_fail_closed(tmp_path: Path) -> None:
    payload, _, runner, segment_id = _authorized(tmp_path)
    runner.write_text("# drift\n", encoding="utf-8")
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="wrapper drifted"):
        _run(payload, runner, segment_id)

    payload, _, runner, segment_id = _authorized(tmp_path / "plan")
    changed = copy.deepcopy(payload)
    changed["segment_count"] = 999
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="payload hash drifted"):
        _run(changed, runner, segment_id)
