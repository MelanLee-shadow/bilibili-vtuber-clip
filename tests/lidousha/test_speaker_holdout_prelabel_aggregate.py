from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from src.autoslice import speaker_holdout_prelabel as prelabel
from tests.lidousha.test_speaker_holdout_prelabel_plan import _build, _inputs


def _result() -> dict[str, object]:
    return {
        "provider": "bcut",
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


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(prelabel._json_bytes(value))
    path.chmod(0o600)


def _fixture(tmp_path: Path) -> dict[str, object]:
    values = _inputs(tmp_path)
    plan, _, _ = _build(values)
    run_one_wrapper = tmp_path / "bound-run-one.py"
    run_one_wrapper.write_text("# bound run-one\n", encoding="utf-8")
    aggregate_verifier = tmp_path / "bound-aggregate.py"
    aggregate_verifier.write_text("# bound aggregate\n", encoding="utf-8")
    core = Path(prelabel.__file__).resolve(strict=True)
    plan["status"] = prelabel.EXECUTION_STATUS
    plan["source_freeze"]["acceptance_id"] = prelabel.EXPECTED_HOLDOUT_ACCEPTANCE_ID
    plan["source_freeze"]["deterministic_payload_sha256"] = (
        prelabel.EXPECTED_HOLDOUT_SOURCE_PAYLOAD_SHA256
    )
    plan["execution_contract"]["plan_only"] = False
    plan["execution_contract"]["real_provider_execution_authorized"] = True
    plan["execution_contract"]["external_audio_upload_authorized"] = True
    plan["toolchain"]["hash_bound_run_one_wrapper"] = {
        "path": str(run_one_wrapper),
        "sha256": prelabel.file_sha256(run_one_wrapper),
    }
    plan["toolchain"]["hash_bound_aggregate_verifier"] = {
        "path": str(aggregate_verifier),
        "sha256": prelabel.file_sha256(aggregate_verifier),
    }
    plan["toolchain"]["run_one_core"] = {
        "path": str(core),
        "sha256": prelabel.file_sha256(core),
    }
    plan["toolchain"]["runtime_binding_state"] = prelabel.EXECUTION_RUNTIME_STATE

    media_root = tmp_path / "media"
    media_root.mkdir()
    sessions = []
    segment_pcm: dict[str, bytes] = {}
    for session_id, expected in sorted(prelabel.EXPECTED_HOLDOUT_SESSIONS.items()):
        segments = []
        compact_date = str(expected["session_date"]).replace("-", "")
        for index in range(1, int(expected["segment_count"]) + 1):
            segment_id = f"22966160_{compact_date}-{index:02d}"
            source = media_root / f"{segment_id}.mp4"
            source.write_bytes(f"media:{segment_id}".encode())
            metadata = source.stat()
            source_sha256 = prelabel.file_sha256(source)
            segments.append(
                {
                    "session_date": expected["session_date"],
                    "split_role": expected["split_role"],
                    "segment_id": segment_id,
                    "source": {
                        "path": str(source),
                        "size_bytes": metadata.st_size,
                        "mtime_ns": metadata.st_mtime_ns,
                        "sha256": source_sha256,
                        "adapter_target_sha256": source_sha256,
                        "container_duration_ms": 1000,
                    },
                    "outputs": {
                        "attempt_root_template": f"segments/{segment_id}/{{attempt_id}}",
                        **prelabel.ARTIFACT_OUTPUTS,
                    },
                    "state": "PLANNED_NOT_EXECUTED",
                }
            )
            seed = hashlib.sha256(segment_id.encode()).digest()
            segment_pcm[segment_id] = (seed * 1000)[:32_000]
        sessions.append(
            {
                "session_group_id": session_id,
                "session_date": expected["session_date"],
                "split_role": expected["split_role"],
                "segments": segments,
            }
        )
    plan["sessions"] = sessions
    plan["session_count"] = 2
    plan["segment_count"] = 15
    frozen_segments = []
    for session in sessions:
        for segment in session["segments"]:
            source = segment["source"]
            frozen_segments.append(
                {
                    "session_date": segment["session_date"],
                    "split_role": segment["split_role"],
                    "segment_id": segment["segment_id"],
                    "source_media": {
                        "path": source["path"],
                        "size_bytes": source["size_bytes"],
                        "mtime_ns": source["mtime_ns"],
                        "sha256": source["sha256"],
                        "adapter_target_sha256": source["adapter_target_sha256"],
                        "ffprobe": {
                            "duration_ms": source["container_duration_ms"],
                            "audio_codec": "aac",
                        },
                    },
                    "source_flv_evidence": {},
                    "sidecars": {},
                    "meta_event_evidence": {},
                    "next_state": "PRELABEL_PACKAGE_NOT_BUILT",
                }
            )
    for replay in plan["source_freeze"]["replays"]:
        replay_path = Path(replay["path"])
        manifest = json.loads(replay_path.read_text(encoding="utf-8"))
        manifest["segments"] = frozen_segments
        manifest["deterministic_payload_sha256"] = (
            prelabel.EXPECTED_HOLDOUT_SOURCE_PAYLOAD_SHA256
        )
        _write_json(replay_path, manifest)
        replay["file_sha256"] = prelabel.file_sha256(replay_path).removeprefix(
            "sha256:"
        )
    plan["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {key: value for key, value in plan.items() if key != "deterministic_payload_sha256"}
    )
    plan_path = Path(plan["scratch"]["plan_path"])
    _write_json(plan_path, plan)
    plan_file_sha256 = prelabel.file_sha256(plan_path)

    for session in sessions:
        for segment in session["segments"]:
            segment_id = segment["segment_id"]
            pcm = segment_pcm[segment_id]
            mp3 = f"mp3:{segment_id}".encode()
            prelabel.run_one(
                plan=plan,
                segment_id=segment_id,
                attempt_id="attempt-001",
                runner_path=run_one_wrapper,
                source_to_mp3=lambda _path, value=mp3: value,
                mp3_to_pcm_s16le=lambda _mp3, value=pcm: value,
                transcribe_bcut=lambda _mp3: _result(),
            )
    return {
        "plan": plan,
        "plan_path": plan_path,
        "plan_file_sha256": plan_file_sha256,
        "run_one_wrapper": run_one_wrapper,
        "aggregate_verifier": aggregate_verifier,
        "run_root": Path(plan["scratch"]["planned_run_root"]),
        "segment_pcm": segment_pcm,
        "replay_file_sha256": frozenset(
            row["file_sha256"] for row in plan["source_freeze"]["replays"]
        ),
    }


def _finalize(fixture: dict[str, object]) -> dict[str, object]:
    return prelabel.finalize_aggregate(
        plan_path=fixture["plan_path"],
        expected_plan_file_sha256=fixture["plan_file_sha256"],
        verifier_path=fixture["aggregate_verifier"],
        decode_mp3=lambda mp3: fixture["segment_pcm"][mp3.decode()[4:]],
        expected_replay_file_sha256=fixture["replay_file_sha256"],
    )


def _attempt(fixture: dict[str, object], session_index: int, segment_index: int) -> Path:
    segment = fixture["plan"]["sessions"][session_index]["segments"][segment_index]
    return fixture["run_root"] / "segments" / segment["segment_id"] / "attempt-001"


def _rebind_receipt(attempt: Path, artifact_name: str) -> None:
    receipt_path = attempt / prelabel.ARTIFACT_OUTPUTS["extraction_receipt"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    artifact = (attempt / artifact_name).read_bytes()
    receipt["artifacts"][artifact_name] = {
        "sha256": prelabel.bytes_sha256(artifact),
        "size_bytes": len(artifact),
    }
    if artifact_name == prelabel.ARTIFACT_OUTPUTS["canonical_pcm_s16le"]:
        receipt["canonical_pcm"]["sha256"] = prelabel.bytes_sha256(artifact)
        receipt["canonical_pcm"]["sample_count"] = len(artifact) // 2
    receipt["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {
            key: value
            for key, value in receipt.items()
            if key != "deterministic_payload_sha256"
        }
    )
    _write_json(receipt_path, receipt)


def test_finalize_replays_exact_10_plus_5_and_seals_unlabeled_package(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    receipt = _finalize(fixture)
    persisted = json.loads(
        (fixture["run_root"] / prelabel.AGGREGATE_RECEIPT_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert persisted == receipt
    assert receipt["status"] == "ASR_CUE_PACKAGE_FROZEN_PREDICTIONS_NOT_RUN"
    assert [row["segment_count"] for row in receipt["sessions"]] == [10, 5]
    assert receipt["segment_count"] == 15
    assert receipt["cue_count"] == 15
    assert receipt["authority"] == {
        "aggregate_asr_cue_package_frozen": True,
        "predictions_frozen": False,
        "human_truth_opened": False,
        "production_authority": False,
        "deployment_authority": False,
        "next_required_state": "FREEZE_PRELABEL_PREDICTIONS_BEFORE_HUMAN_TRUTH",
    }
    assert receipt["deterministic_payload_sha256"] == prelabel.canonical_sha256(
        {
            key: value
            for key, value in receipt.items()
            if key != "deterministic_payload_sha256"
        }
    )


def test_missing_or_second_success_receipt_fails_closed(tmp_path: Path) -> None:
    missing = _fixture(tmp_path / "missing")
    missing_attempt = _attempt(missing, 0, 0)
    (missing_attempt / prelabel.ARTIFACT_OUTPUTS["extraction_receipt"]).unlink()
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="exactly one success"):
        _finalize(missing)
    assert not (missing["run_root"] / prelabel.AGGREGATE_RECEIPT_NAME).exists()

    duplicate = _fixture(tmp_path / "duplicate")
    first_attempt = _attempt(duplicate, 0, 0)
    second_attempt = first_attempt.parent / "attempt-002"
    shutil.copytree(first_attempt, second_attempt)
    receipt_path = second_attempt / prelabel.ARTIFACT_OUTPUTS["extraction_receipt"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["attempt_id"] = "attempt-002"
    receipt["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {
            key: value
            for key, value in receipt.items()
            if key != "deterministic_payload_sha256"
        }
    )
    _write_json(receipt_path, receipt)
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="exactly one success"):
        _finalize(duplicate)


def test_artifact_drift_and_semantic_truth_injection_fail_closed(tmp_path: Path) -> None:
    drift = _fixture(tmp_path / "drift")
    attempt = _attempt(drift, 0, 0)
    srt = attempt / prelabel.ARTIFACT_OUTPUTS["asr_srt"]
    srt.write_bytes(srt.read_bytes() + b"drift")
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="artifact binding"):
        _finalize(drift)

    injected = _fixture(tmp_path / "injected")
    attempt = _attempt(injected, 0, 0)
    cue_name = prelabel.ARTIFACT_OUTPUTS["cue_table_json"]
    cue_path = attempt / cue_name
    cue = json.loads(cue_path.read_text(encoding="utf-8"))
    cue["human_labels"] = [{"speaker": "HOST"}]
    _write_json(cue_path, cue)
    _rebind_receipt(attempt, cue_name)
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="semantic replay"):
        _finalize(injected)


def test_cross_session_pcm_duplicate_is_rejected_even_when_receipt_is_rebound(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    first = _attempt(fixture, 0, 0)
    second = _attempt(fixture, 1, 0)
    pcm_name = prelabel.ARTIFACT_OUTPUTS["canonical_pcm_s16le"]
    mp3_name = prelabel.ARTIFACT_OUTPUTS["asr_input_mp3"]
    (second / pcm_name).write_bytes((first / pcm_name).read_bytes())
    (second / mp3_name).write_bytes((first / mp3_name).read_bytes())
    _rebind_receipt(second, pcm_name)
    _rebind_receipt(second, mp3_name)
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="duplicated across"):
        _finalize(fixture)


def test_mp3_to_pcm_replay_mismatch_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="MP3 to canonical PCM"):
        prelabel.finalize_aggregate(
            plan_path=fixture["plan_path"],
            expected_plan_file_sha256=fixture["plan_file_sha256"],
            verifier_path=fixture["aggregate_verifier"],
            decode_mp3=lambda _mp3: b"\0\0" * 16_000,
            expected_replay_file_sha256=fixture["replay_file_sha256"],
        )


def test_wrong_population_and_preexisting_output_fail_without_overwrite(
    tmp_path: Path,
) -> None:
    population = _fixture(tmp_path / "population")
    changed = copy.deepcopy(population["plan"])
    changed["sessions"][0]["segments"].pop()
    changed["segment_count"] = 14
    changed["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {
            key: value
            for key, value in changed.items()
            if key != "deterministic_payload_sha256"
        }
    )
    _write_json(population["plan_path"], changed)
    population["plan_file_sha256"] = prelabel.file_sha256(population["plan_path"])
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="population"):
        _finalize(population)

    existing = _fixture(tmp_path / "existing")
    output = existing["run_root"] / prelabel.AGGREGATE_RECEIPT_NAME
    output.write_bytes(b"sentinel")
    output.chmod(0o600)
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="already exists"):
        _finalize(existing)
    assert output.read_bytes() == b"sentinel"


def test_aggregate_seal_blocks_later_attempt_before_audio_or_provider(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _finalize(fixture)
    segment = fixture["plan"]["sessions"][0]["segments"][0]
    called: list[str] = []

    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="sealed"):
        prelabel.run_one(
            plan=fixture["plan"],
            segment_id=segment["segment_id"],
            attempt_id="attempt-002",
            runner_path=fixture["run_one_wrapper"],
            source_to_mp3=lambda _path: called.append("audio") or b"mp3",
            mp3_to_pcm_s16le=lambda _mp3: b"\0\0" * 16_000,
            transcribe_bcut=lambda _mp3: called.append("provider") or _result(),
        )
    assert called == []


def test_verifier_binding_drift_stops_without_aggregate_output(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["aggregate_verifier"].write_text("# drift\n", encoding="utf-8")
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="binding drifted"):
        _finalize(fixture)
    assert not (fixture["run_root"] / prelabel.AGGREGATE_RECEIPT_NAME).exists()


def test_source_freeze_replay_byte_drift_stops_without_output(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    replay = Path(fixture["plan"]["source_freeze"]["replays"][0]["path"])
    replay.write_bytes(replay.read_bytes() + b"drift")
    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="replay bytes drifted"):
        _finalize(fixture)
    assert not (fixture["run_root"] / prelabel.AGGREGATE_RECEIPT_NAME).exists()


def test_rehashed_plan_cannot_substitute_a_member_outside_accepted_replays(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    changed = copy.deepcopy(fixture["plan"])
    segment = changed["sessions"][0]["segments"][0]
    old_source = Path(segment["source"]["path"])
    substitute = old_source.with_name("substitute.mp4")
    substitute.write_bytes(b"substitute source")
    substitute_stat = substitute.stat()
    substitute_sha256 = prelabel.file_sha256(substitute)
    segment["segment_id"] = "22966160_20260810-substitute"
    segment["source"] = {
        "path": str(substitute),
        "size_bytes": substitute_stat.st_size,
        "mtime_ns": substitute_stat.st_mtime_ns,
        "sha256": substitute_sha256,
        "adapter_target_sha256": substitute_sha256,
        "container_duration_ms": 1000,
    }
    segment["outputs"] = {
        "attempt_root_template": "segments/22966160_20260810-substitute/{attempt_id}",
        **prelabel.ARTIFACT_OUTPUTS,
    }
    changed["deterministic_payload_sha256"] = prelabel.canonical_sha256(
        {
            key: value
            for key, value in changed.items()
            if key != "deterministic_payload_sha256"
        }
    )
    _write_json(fixture["plan_path"], changed)
    fixture["plan_file_sha256"] = prelabel.file_sha256(fixture["plan_path"])

    with pytest.raises(prelabel.SpeakerHoldoutPrelabelError, match="membership differs"):
        _finalize(fixture)
    assert not (fixture["run_root"] / prelabel.AGGREGATE_RECEIPT_NAME).exists()
