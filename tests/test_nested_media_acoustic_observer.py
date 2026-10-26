from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.nested_media_acoustic_observer import (
    NestedMediaAcousticObserverError,
    _canonical_hash,
    _runtime_identity,
    observe_nested_media_acoustics,
)
from src.autoslice.nested_media_caption_attribution import build_caption_source_attribution


_RUNTIME_IDENTITY = {
    "provider": "fixture-gemini",
    "model": "fixture-model-v1",
    "transport": "fixture.generate_content",
    "transport_module": "tests.fixture_transport",
    "transport_module_sha256": "sha256:" + "1" * 64,
    "observer_prompt_sha256": "sha256:" + "2" * 64,
    "cpa_prompt_sha256": "sha256:" + "3" * 64,
    "cpa_call_identity": {
        "transport": "fixture.cpa",
        "models": ["fixture-cpa-v1"],
        "effort": "medium",
    },
    "cpa_call_identity_sha256": "sha256:1d35efd51e1d0d958a31aaece480bc92e5b3b308c66b4387dba0fedd89d19bd3",
}


def test_long_transport_paths_remain_part_of_runtime_identity():
    path = "/private-runs/" + "a/" * 150 + "PROCESS.json"
    identity = _runtime_identity({**_RUNTIME_IDENTITY, "transport_binding": {"job_dir": path}})
    changed = _runtime_identity({**_RUNTIME_IDENTITY, "transport_binding": {"job_dir": path + "x"}})
    assert identity["transport_binding"]["job_dir"] == path
    assert identity["identity_sha256"] != changed["identity_sha256"]


@pytest.mark.parametrize("binding", [
    {"job_dir": "x" * 4097},
    {"argv": ["x" * 300]},
    {"job_dir": "/private\n/path"},
    {"job_dir": "/private\t/path"},
    {"api_key": "secret-value"},
])
def test_transport_identity_still_rejects_unsafe_values(binding):
    with pytest.raises(NestedMediaAcousticObserverError, match="RUNTIME_IDENTITY_INVALID"):
        _runtime_identity({**_RUNTIME_IDENTITY, "transport_binding": binding})


def test_long_absolute_ffmpeg_paths_are_preserved_but_labels_are_bounded():
    path = "/private-runs/" + "a" * 300
    identity = _runtime_identity({
        **_RUNTIME_IDENTITY,
        "transport_binding": {"ffmpeg": {"argv": [["ffmpeg", path]]}},
    })
    assert identity["transport_binding"]["ffmpeg"]["argv"][0][1] == path
    with pytest.raises(NestedMediaAcousticObserverError, match="RUNTIME_IDENTITY_INVALID"):
        _runtime_identity({**_RUNTIME_IDENTITY, "model": "m" * 257})


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _inputs(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    outer = _write(tmp_path / "outer.mp4", b"outer-media")
    inner = _write(tmp_path / "inner.mp4", b"independent-source")
    srt = _write(
        tmp_path / "final.srt",
        b"1\n00:00:00,000 --> 00:00:01,000\nsource words\n",
    )
    frame = _write(tmp_path / "caption.png", b"caption-frame")
    host = _write(tmp_path / "host.wav", b"trusted-host-enrollment")
    source = _write(tmp_path / "source.wav", b"mapped-source-crop")
    target = _write(tmp_path / "target.wav", b"current-mixed-target")
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "schema_version": "test-mapping.v1",
                "status": "EVIDENCE_ONLY",
                "media": {
                    "outer": {"path": "outer.mp4", "sha256": _sha(outer)},
                    "inner": {
                        "path": "inner.mp4",
                        "bvid": "BVTEST123456",
                        "cid": "123456",
                        "sha256": _sha(inner),
                    },
                },
                "lineage_mapping": {
                    "outer_window_ms": [100, 200],
                    "inner_window_ms": [93.0, 193.0],
                    "lag_ms": 7.0,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "candidate_id": "candidate",
        "observation_id": "cue-1",
        "outer_media_path": outer,
        "outer_media_sha256": _sha(outer),
        "final_srt_path": srt,
        "final_srt_sha256": _sha(srt),
        "caption_frame_path": frame,
        "caption_frame_sha256": _sha(frame),
        "interval_ms": [100, 200],
        "caption_frame_ms": 150,
        "independent_source_media_path": inner,
        "independent_source_media_sha256": _sha(inner),
        "mapping_receipt_path": mapping,
        "mapping_receipt_sha256": _sha(mapping),
        "host_enrollment_path": host,
        "host_enrollment_sha256": _sha(host),
        "source_audio_path": source,
        "source_audio_sha256": _sha(source),
        "target_audio_path": target,
        "target_audio_sha256": _sha(target),
        "evidence_root": tmp_path / "evidence",
        "runtime_identity": dict(_RUNTIME_IDENTITY),
    }


def _observer(request: dict[str, object]) -> dict[str, object]:
    response = {
        "schema_version": "nested-media-acoustic-observation.v1",
        "status": "OBSERVED",
        "authority": "EVIDENCE_ONLY",
        "candidate_id": request["candidate_id"],
        "observation_id": request["observation_id"],
        "request_sha256": request["request_sha256"],
        "source_media_speech": "PRESENT",
        "host_speech": "ABSENT",
        "overlap_speech": "ABSENT",
        "evidence": {"parts": request["part_order"]},
    }
    for field in ("target_interval_ms", "target_cue_indexes"):
        if field in request:
            response[field] = request[field]
    return response


def _judge(request: dict[str, object], observation: dict[str, object]) -> dict[str, object]:
    response = {
        "schema_version": "nested-media-acoustic-cpa-judgement.v1",
        "status": "JUDGED",
        "authority": "CPA",
        "candidate_id": request["candidate_id"],
        "observation_id": request["observation_id"],
        "request_sha256": request["request_sha256"],
        "observation_sha256": _canonical_hash(observation),
        "source_media_speech": "PRESENT",
        "host_speech": "ABSENT",
        "overlap_speech": "ABSENT",
        "final_class": "source_media",
        "evidence": {"basis": "typed-observer-evidence"},
    }
    for field in ("target_interval_ms", "target_cue_indexes"):
        if field in request:
            response[field] = request[field]
    return response


def _call(
    values: dict[str, object],
    observer=_observer,
    judge=_judge,
) -> dict[str, object]:
    return observe_nested_media_acoustics(
        **values,
        observe_audio=observer,
        cpa_judge=judge,
    )


def _voice_reference_values(tmp_path: Path) -> dict[str, object]:
    values = _inputs(tmp_path)
    binding_path = tmp_path / "voice-reference-binding.json"
    binding = {
        "schema_version": "nested-media-voice-reference-binding.v1",
        "status": "REFERENCE_BOUND",
        "mode": "VOICE_REFERENCE_ONLY_UNALIGNED",
        "candidate_id": values["candidate_id"],
        "observation_id": values["observation_id"],
        "outer_media": {
            "path": str(Path(values["outer_media_path"]).resolve()),
            "sha256": values["outer_media_sha256"],
        },
        "final_srt": {
            "path": str(Path(values["final_srt_path"]).resolve()),
            "sha256": values["final_srt_sha256"],
        },
        "caption_frame": {
            "path": str(Path(values["caption_frame_path"]).resolve()),
            "sha256": values["caption_frame_sha256"],
            "frame_ms": values["caption_frame_ms"],
        },
        "source_media": {
            "path": str(Path(values["independent_source_media_path"]).resolve()),
            "sha256": values["independent_source_media_sha256"],
        },
        "host_enrollment": {
            "path": str(Path(values["host_enrollment_path"]).resolve()),
            "sha256": values["host_enrollment_sha256"],
        },
        "source_audio": {
            "path": str(Path(values["source_audio_path"]).resolve()),
            "sha256": values["source_audio_sha256"],
        },
        "target_audio": {
            "path": str(Path(values["target_audio_path"]).resolve()),
            "sha256": values["target_audio_sha256"],
        },
        "bvid": "BVREFERENCE123",
        "cid": "987654",
        "outer_context_interval_ms": values["interval_ms"],
        "reference_window_ms": [0, 6500],
        "target_interval_ms": [100, 200],
        "target_cue_indexes": [1],
        "time_alignment_performed": False,
        "source_subtraction_authorized": False,
        "caption_dedup_authority": False,
        "source_role_authority": False,
        "mutation_authorized": False,
    }
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    values.update(
        mapping_receipt_path=None,
        mapping_receipt_sha256=None,
        target_interval_ms=[100, 200],
        target_cue_indexes=[1],
        observer_mode="VOICE_REFERENCE_ONLY_UNALIGNED",
        voice_reference_binding_path=binding_path,
        voice_reference_binding_sha256=_sha(binding_path),
    )
    return values


def test_unaligned_voice_reference_is_single_cue_and_mapping_free(tmp_path: Path):
    values = _voice_reference_values(tmp_path)
    captured: dict[str, object] = {}

    def observer(request):
        captured.update(request)
        return _observer(request)

    result = _call(values, observer)
    assert result["status"] == "PASS"
    assert captured["observer_mode"] == "VOICE_REFERENCE_ONLY_UNALIGNED"
    assert "mapping_receipt" not in captured
    assert captured["target_interval_ms"] == [100, 200]
    assert captured["target_cue_indexes"] == [1]
    assert captured["voice_reference_binding"]["reference_window_ms"] == [0, 6500]
    assert json.dumps(captured["voice_reference_binding"]["reference_window_ms"]) == "[0, 6500]"
    assert captured["policy"]["time_alignment_performed"] is False
    assert captured["policy"]["source_subtraction_authorized"] is False
    receipt = json.loads(Path(result["receipt_binding"]["path"]).read_text(encoding="utf-8"))
    assert receipt["method"] == "HASH_BOUND_UNALIGNED_VOICE_REFERENCE"
    assert receipt["observer_mode"] == "VOICE_REFERENCE_ONLY_UNALIGNED"
    assert "mapping" not in receipt["evidence"]
    assert receipt["voice_reference_binding"]["status"] == "REFERENCE_BOUND"
    assert receipt["mutation_authorized"] is False


def test_voice_reference_binding_binds_context_window_and_target_offset(tmp_path: Path):
    values = _voice_reference_values(tmp_path)
    values["interval_ms"] = [0, 300]
    values["target_audio_window_ms"] = [50, 250]
    binding_path = Path(values["voice_reference_binding_path"])
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["outer_context_interval_ms"] = [0, 300]
    binding["target_audio_window_ms"] = [50, 250]
    binding["target_audio_offset_ms"] = [50, 150]
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    values["voice_reference_binding_sha256"] = _sha(binding_path)

    captured: dict[str, object] = {}

    def observer(request):
        captured.update(request)
        return _observer(request)

    result = _call(values, observer)

    assert result["status"] == "PASS"
    assert captured["target_audio_window_ms"] == [50, 250]
    assert captured["target_audio_offset_ms"] == [50, 150]
    assert captured["voice_reference_binding"]["target_audio_window_ms"] == [50, 250]
    assert captured["voice_reference_binding"]["target_audio_offset_ms"] == [50, 150]


def test_voice_reference_binding_rejects_target_outside_declared_audio_window(
    tmp_path: Path,
):
    values = _voice_reference_values(tmp_path)
    values["interval_ms"] = [0, 300]
    values["target_audio_window_ms"] = [50, 120]
    binding_path = Path(values["voice_reference_binding_path"])
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["outer_context_interval_ms"] = [0, 300]
    binding["target_audio_window_ms"] = [50, 120]
    binding["target_audio_offset_ms"] = [50, 150]
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    values["voice_reference_binding_sha256"] = _sha(binding_path)

    with pytest.raises(
        NestedMediaAcousticObserverError,
        match="VOICE_REFERENCE_TARGET_AUDIO_WINDOW_INVALID",
    ):
        _call(values)


def test_legacy_narrow_voice_binding_remains_valid_only_for_narrow_request(
    tmp_path: Path,
):
    values = _voice_reference_values(tmp_path)
    values["interval_ms"] = [0, 300]
    binding_path = Path(values["voice_reference_binding_path"])
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["outer_context_interval_ms"] = [0, 300]
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    values["voice_reference_binding_sha256"] = _sha(binding_path)
    captured: dict[str, object] = {}

    def observer(request):
        captured.update(request)
        return _observer(request)

    result = _call(values, observer)
    assert result["status"] == "PASS"
    assert captured["target_audio_window_ms"] == [100, 200]

    values["target_audio_window_ms"] = [50, 250]
    with pytest.raises(
        NestedMediaAcousticObserverError,
        match="VOICE_REFERENCE_TARGET_AUDIO_WINDOW_MISMATCH",
    ):
        _call(values)


@pytest.mark.parametrize("external", [False, True])
def test_complete_voice_reference_preserves_fractional_tail_through_dispatch(
    tmp_path: Path, external: bool,
):
    values = _voice_reference_values(tmp_path)
    path = Path(values["voice_reference_binding_path"])
    payload = json.loads(path.read_text())
    payload["reference_window_ms"] = [0, 835.875]
    if external:
        payload.pop("bvid")
        payload.pop("cid")
        uri = "https://example.com/official-button.mp3"
        payload["source_identity"] = {
            "kind": "OFFICIAL_EXTERNAL_AUDIO", "source_uri": uri,
            "source_sha256": values["independent_source_media_sha256"],
        }
        payload["source_media"].update({
            "source_uri": uri,
            "source_sha256": values["independent_source_media_sha256"],
        })
    path.write_text(json.dumps(payload, sort_keys=True))
    values["voice_reference_binding_sha256"] = _sha(path)
    captured = {}

    def observer(request):
        captured.update(request)
        return _observer(request)

    result = _call(values, observer)
    assert result["status"] == "PASS"
    assert captured["voice_reference_binding"]["reference_window_ms"] == [0, 835.875]
    receipt = json.loads(Path(result["receipt_binding"]["path"]).read_text())
    assert receipt["voice_reference_binding"]["reference_window_ms"] == [0, 835.875]
    assert receipt["voice_reference_binding"]["source_role_authority"] is False
    assert captured["target_interval_ms"] == [100, 200]


def test_fractional_reference_does_not_relax_integer_current_target(tmp_path: Path):
    values = _voice_reference_values(tmp_path)
    path = Path(values["voice_reference_binding_path"])
    payload = json.loads(path.read_text())
    payload["reference_window_ms"] = [0, 835.875]
    payload["target_interval_ms"] = [100, 200.5]
    path.write_text(json.dumps(payload, sort_keys=True))
    values["voice_reference_binding_sha256"] = _sha(path)
    with pytest.raises(NestedMediaAcousticObserverError, match="VOICE_REFERENCE_TARGET_INTERVAL_INVALID"):
        _call(values, lambda _request: pytest.fail("invalid current target dispatched"))


def test_voice_reference_cache_isolated_from_aligned_and_joint_scope(tmp_path: Path):
    values = _voice_reference_values(tmp_path)
    calls = {"observer": 0, "judge": 0}

    def observer(request):
        calls["observer"] += 1
        return _observer(request)

    def judge(request, observation):
        calls["judge"] += 1
        return _judge(request, observation)

    first = _call(values, observer, judge)
    second = _call(values, observer, judge)
    assert first["status"] == second["status"] == "PASS"
    assert second["replayed"] is True
    assert calls == {"observer": 1, "judge": 1}
    changed = dict(values)
    changed["target_cue_indexes"] = ["cue-1"]
    with pytest.raises(NestedMediaAcousticObserverError, match="VOICE_REFERENCE_TARGET_SCOPE_MISMATCH"):
        _call(changed, observer, judge)


def test_voice_reference_binding_drift_and_multiple_cues_fail_closed(tmp_path: Path):
    values = _voice_reference_values(tmp_path)
    binding_path = Path(values["voice_reference_binding_path"])
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["target_cue_indexes"] = [1, 2]
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    with pytest.raises(NestedMediaAcousticObserverError, match="VOICE_REFERENCE_BINDING_HASH_MISMATCH"):
        _call(values)


def test_voice_reference_binding_rejects_mapping_and_alignment_fields(tmp_path: Path):
    values = _voice_reference_values(tmp_path)
    binding_path = Path(values["voice_reference_binding_path"])
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["mapping_receipt"] = {"path": "mapping.json", "sha256": "sha256:" + "a" * 64}
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    values["voice_reference_binding_sha256"] = _sha(binding_path)
    with pytest.raises(NestedMediaAcousticObserverError, match="VOICE_REFERENCE_MAPPING_FORBIDDEN"):
        _call(values)


def test_voice_reference_binding_drift_during_callbacks_cannot_pass(tmp_path: Path):
    values = _voice_reference_values(tmp_path)
    binding_path = Path(values["voice_reference_binding_path"])

    def judge(request, observation):
        binding_path.write_text(
            binding_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        return _judge(request, observation)

    result = _call(values, judge=judge)
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "INPUT_DRIFT_AFTER_OBSERVATION"
    assert not Path(result["request_dir"]).joinpath("acoustic-receipt.json").exists()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("candidate_id", "other-candidate", "VOICE_REFERENCE_BINDING_INVALID"),
        ("outer_context_interval_ms", [90, 200], "VOICE_REFERENCE_CONTEXT_INTERVAL_MISMATCH"),
        ("target_interval_ms", [110, 200], "VOICE_REFERENCE_TARGET_SCOPE_MISMATCH"),
        ("caption_frame", {"frame_ms": 0}, "VOICE_REFERENCE_FRAME_BINDING_MISMATCH"),
    ],
)
def test_voice_reference_current_identity_and_span_drift_fail_closed(
    tmp_path: Path, field, value, reason
):
    values = _voice_reference_values(tmp_path / field.replace("_", "-"))
    binding_path = Path(values["voice_reference_binding_path"])
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    if field == "caption_frame":
        binding[field] = {**binding[field], **value}
    else:
        binding[field] = value
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    values["voice_reference_binding_sha256"] = _sha(binding_path)
    with pytest.raises(NestedMediaAcousticObserverError, match=reason):
        _call(values)


@pytest.mark.parametrize(
    ("field", "replacement", "reason"),
    [
        ("source_media", "host_enrollment_path", "VOICE_REFERENCE_SOURCE_BINDING_MISMATCH"),
        ("final_srt", "outer_media_path", "VOICE_REFERENCE_SRT_BINDING_MISMATCH"),
    ],
)
def test_voice_reference_source_and_srt_binding_drift_fail_closed(
    tmp_path: Path, field, replacement, reason
):
    values = _voice_reference_values(tmp_path / field)
    binding_path = Path(values["voice_reference_binding_path"])
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    replacement_path = Path(values[replacement]).resolve()
    binding[field] = {
        "path": str(replacement_path),
        "sha256": _sha(replacement_path),
    }
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    values["voice_reference_binding_sha256"] = _sha(binding_path)
    with pytest.raises(NestedMediaAcousticObserverError, match=reason):
        _call(values)


def test_bound_receipt_reaches_existing_attribution_consumer(tmp_path: Path):
    values = _inputs(tmp_path)
    captured: dict[str, object] = {}

    def observer(request):
        captured.update(request)
        return _observer(request)

    result = _call(values, observer)

    assert result["status"] == "PASS"
    assert captured["part_order"] == ["host_enrollment", "independent_source", "current_target"]
    assert captured["mapping_receipt"]["outer_window_ms"] == [100.0, 200.0]
    assert captured["mapping_receipt"]["source_window_ms"] == [93.0, 193.0]
    assert captured["mapping_receipt"]["lag_ms"] == 7.0
    binding = result["receipt_binding"]
    assert isinstance(binding, dict)
    receipt = json.loads(Path(binding["path"]).read_text(encoding="utf-8"))
    assert receipt["method"].startswith("HASH_BOUND_")
    assert receipt["accepted_by"] == "CPA"
    assert receipt["mutation_authorized"] is False
    assert receipt["runtime_identity"]["model"] == "fixture-model-v1"
    assert receipt["runtime_identity"]["cpa_call_identity"]["models"] == ["fixture-cpa-v1"]
    assert receipt["runtime_identity"]["cpa_call_identity_sha256"].startswith("sha256:")
    assert receipt["runtime_identity_sha256"].startswith("sha256:")
    assert receipt["bindings"]["runtime_identity_sha256"] == receipt["runtime_identity_sha256"]
    assert receipt["bindings"]["independent_source_media_sha256"] == values[
        "independent_source_media_sha256"
    ]

    observation = {
        "schema_version": "embedded-media-caption-observation.v1",
        "observation_id": "cue-1",
        "candidate_id": "candidate",
        "frame_ms": 150,
        "caption_present": True,
        "visible_text": "source words",
        "role": "WATCHED_MEDIA_CAPTION",
        "pipeline_burned_subtitle": False,
        "frame_path": values["caption_frame_path"],
        "frame_sha256": values["caption_frame_sha256"],
    }
    attribution = build_caption_source_attribution(
        candidate_id="candidate",
        source_media_path=values["outer_media_path"],
        source_media_sha256=values["outer_media_sha256"],
        final_srt_path=values["final_srt_path"],
        final_srt_sha256=values["final_srt_sha256"],
        observations=[observation],
        acoustic_receipt_bindings=[binding],
    )
    assert attribution["rows"][0]["label"] == "unknown"
    assert attribution["rows"][0]["action"] == "KEEP"
    assert attribution["rows"][0]["reason_codes"] == ["ACOUSTIC_CONTEXT_ONLY_KEEP"]


def test_completed_request_replays_without_callbacks(tmp_path: Path):
    values = _inputs(tmp_path)
    calls = {"observer": 0, "judge": 0}

    def observer(request):
        calls["observer"] += 1
        return _observer(request)

    def judge(request, observation):
        calls["judge"] += 1
        return _judge(request, observation)

    first = _call(values, observer, judge)
    second = _call(values, observer, judge)
    assert first["status"] == second["status"] == "PASS"
    assert second["replayed"] is True
    assert calls == {"observer": 1, "judge": 1}


def test_focused_target_binding_changes_request_cache_and_receipt(tmp_path: Path):
    values = _inputs(tmp_path)
    values.update(target_interval_ms=[110, 190], target_cue_indexes=[1])
    calls = {"observer": 0, "judge": 0}

    def observer(request):
        calls["observer"] += 1
        return _observer(request)

    def judge(request, observation):
        calls["judge"] += 1
        return _judge(request, observation)

    first = _call(values, observer, judge)
    second = _call(values, observer, judge)
    assert first["status"] == second["status"] == "PASS"
    assert second["replayed"] is True
    assert calls == {"observer": 1, "judge": 1}
    request = json.loads(Path(first["request_path"]).read_text(encoding="utf-8"))
    receipt = json.loads(Path(first["receipt_binding"]["path"]).read_text(encoding="utf-8"))
    assert request["target_interval_ms"] == [110, 190]
    assert request["target_cue_indexes"] == [1]
    assert receipt["target_interval_ms"] == [110, 190]
    assert receipt["target_cue_indexes"] == [1]
    assert receipt["bindings"]["target_interval_ms"] == [110, 190]
    assert receipt["bindings"]["target_cue_indexes"] == [1]

    changed = dict(values)
    changed["target_interval_ms"] = [120, 190]
    third = _call(changed, observer, judge)
    assert third["status"] == "PASS"
    assert calls == {"observer": 2, "judge": 2}
    assert third["request_sha256"] != first["request_sha256"]


def test_focused_observer_must_echo_target_binding(tmp_path: Path):
    values = _inputs(tmp_path)
    values.update(target_interval_ms=[110, 190], target_cue_indexes=[1])

    def observer(request):
        response = _observer(request)
        response.pop("target_interval_ms")
        return response

    result = _call(values, observer, _judge)
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "OBSERVER_TARGET_BINDING_MISMATCH"
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))


def test_focused_cpa_must_echo_target_binding(tmp_path: Path):
    values = _inputs(tmp_path)
    values.update(target_interval_ms=[110, 190], target_cue_indexes=[1])

    def judge(request, observation):
        response = _judge(request, observation)
        response.pop("target_cue_indexes")
        return response

    result = _call(values, _observer, judge)
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "CPA_TARGET_BINDING_MISMATCH"
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))


@pytest.mark.parametrize(
    ("target_interval", "target_indexes", "reason"),
    [
        ([90, 190], [1], "TARGET_INTERVAL_OUTSIDE_CONTEXT"),
        ([110, 190], [True], "TARGET_CUE_INDEXES_INVALID"),
        ([110, 180], [1, 1], "TARGET_CUE_INDEXES_INVALID"),
        ([150, 150], [1], "TARGET_INTERVAL_INVALID"),
    ],
)
def test_invalid_focused_target_fails_before_callbacks(
    tmp_path: Path, target_interval, target_indexes, reason
):
    values = _inputs(tmp_path)
    values.update(target_interval_ms=target_interval, target_cue_indexes=target_indexes)
    with pytest.raises(NestedMediaAcousticObserverError, match=reason):
        _call(values)


def test_cpa_cannot_turn_unknown_into_absent(tmp_path: Path):
    values = _inputs(tmp_path)

    def observer(request):
        response = _observer(request)
        response.update(host_speech="UNKNOWN", overlap_speech="UNKNOWN")
        return response

    result = _call(values, observer, _judge)
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "CPA_DISPATCH_FAILED"
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))


def test_cpa_can_only_downgrade_definite_states_to_unknown(tmp_path: Path):
    values = _inputs(tmp_path)

    def judge(request, observation):
        result = _judge(request, observation)
        result.update(host_speech="UNKNOWN", overlap_speech="UNKNOWN", final_class="unknown")
        return result

    result = _call(values, _observer, judge)
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "CPA_UNKNOWN_KEEP"
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))


@pytest.mark.parametrize("tamper", ["request", "observer", "cpa", "receipt", "result"])
def test_completed_cache_revalidates_all_bound_documents(tmp_path: Path, tamper: str):
    values = _inputs(tmp_path / tamper)
    first = _call(values)
    if tamper == "request":
        path = Path(first["request_path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["candidate_id"] = "tampered"
    elif tamper == "observer":
        path = Path(first["observer_response_path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["evidence"] = {"tampered": True}
    elif tamper == "cpa":
        path = Path(first["cpa_response_path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["evidence"] = {"tampered": True}
    elif tamper == "receipt":
        path = Path(first["receipt_binding"]["path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["source_media_speech"] = "ABSENT"
    else:
        path = Path(first["request_dir"]) / "RESULT.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["final_class"] = "host"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    calls = {"observer": 0, "judge": 0}

    def observer(_request):
        calls["observer"] += 1
        raise AssertionError("tampered complete cache must not redispatch")

    def judge(_request, _observation):
        calls["judge"] += 1
        raise AssertionError("tampered complete cache must not redispatch")

    with pytest.raises(NestedMediaAcousticObserverError, match="CACHED_RECEIPT_INVALID"):
        _call(values, observer, judge)
    assert calls == {"observer": 0, "judge": 0}


@pytest.mark.parametrize("state_status", ["COMPLETE", "RUNNING", "CPA_RUNNING"])
def test_complete_result_without_receipt_never_replays_pass(tmp_path: Path, state_status: str):
    values = _inputs(tmp_path)
    first = _call(values)
    Path(first["receipt_binding"]["path"]).unlink()
    state_path = Path(first["request_dir"]) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = state_status
    state_path.write_text(json.dumps(state), encoding="utf-8")
    calls = {"observer": 0, "judge": 0}

    def observer(request):
        calls["observer"] += 1
        return _observer(request)

    def judge(request, observation):
        calls["judge"] += 1
        return _judge(request, observation)

    result = _call(values, observer, judge)
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "COMPLETED_RECEIPT_MISSING"
    assert calls == {"observer": 0, "judge": 0}


def test_failed_dispatch_is_evidence_only_and_is_not_retried(tmp_path: Path):
    values = _inputs(tmp_path)
    calls = {"observer": 0, "judge": 0}

    def failing_observer(_request):
        calls["observer"] += 1
        raise RuntimeError("provider unavailable")

    def judge(_request, _observation):
        calls["judge"] += 1
        raise AssertionError("CPA must not run after observer failure")

    first = _call(values, failing_observer, judge)
    second = _call(values, failing_observer, judge)
    assert first["status"] == second["status"] == "EVIDENCE_ONLY"
    assert first["reason_code"] == "OBSERVER_DISPATCH_FAILED"
    assert second["reason_code"] == "OBSERVER_DISPATCH_FAILED"
    assert second["replayed"] is True
    assert calls == {"observer": 1, "judge": 0}
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))


def test_failure_cache_does_not_persist_exception_message(tmp_path: Path):
    values = _inputs(tmp_path)
    secret = "https://provider.invalid/?Authorization=DO_NOT_PERSIST"

    def observer(_request):
        raise RuntimeError(secret)

    result = _call(values, observer, _judge)
    assert result["status"] == "EVIDENCE_ONLY"
    for path in Path(values["evidence_root"]).rglob("*.json"):
        assert secret not in path.read_text(encoding="utf-8")


def test_unknown_observation_stays_unknown_without_cpa(tmp_path: Path):
    values = _inputs(tmp_path)
    calls = {"judge": 0}

    def observer(request):
        response = _observer(request)
        response.update(
            status="UNKNOWN",
            source_media_speech="UNKNOWN",
            host_speech="UNKNOWN",
            overlap_speech="UNKNOWN",
        )
        return response

    def judge(_request, _observation):
        calls["judge"] += 1
        raise AssertionError("unknown observation is not promotable")

    result = _call(values, observer, judge)
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "OBSERVER_UNKNOWN_KEEP"
    assert calls["judge"] == 0
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))


@pytest.mark.parametrize("drift", ["srt", "frame", "source", "mapping", "window"])
def test_current_binding_or_mapping_drift_fails_closed(tmp_path: Path, drift: str):
    values = _inputs(tmp_path / drift)
    if drift == "srt":
        Path(values["final_srt_path"]).write_bytes(b"changed\n")
        values["final_srt_sha256"] = "sha256:" + "0" * 64
        expected = "FINAL_SRT_HASH_MISMATCH"
    elif drift == "frame":
        Path(values["caption_frame_path"]).write_bytes(b"changed-frame")
        values["caption_frame_sha256"] = "sha256:" + "0" * 64
        expected = "CAPTION_FRAME_HASH_MISMATCH"
    elif drift == "source":
        Path(values["independent_source_media_path"]).write_bytes(b"changed-source")
        values["independent_source_media_sha256"] = "sha256:" + "0" * 64
        expected = "INDEPENDENT_SOURCE_HASH_MISMATCH"
    elif drift == "mapping":
        mapping = Path(values["mapping_receipt_path"])
        payload = json.loads(mapping.read_text(encoding="utf-8"))
        payload["lineage_mapping"]["lag_ms"] = 8.0
        mapping.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        values["mapping_receipt_sha256"] = _sha(mapping)
        expected = "MAPPING_LAG_WINDOW_MISMATCH"
    else:
        values["interval_ms"] = [101, 200]
        expected = "MAPPING_OUTER_WINDOW_MISMATCH"
    with pytest.raises(NestedMediaAcousticObserverError, match=expected):
        _call(values)


def test_cpa_cannot_promote_unobserved_source(tmp_path: Path):
    values = _inputs(tmp_path)

    def observer(request):
        response = _observer(request)
        response["source_media_speech"] = "ABSENT"
        return response

    # The injected judge is intentionally fabricated; the adapter must retain
    # the failure as evidence-only and never create a receipt.
    result = _call(values, observer, _judge)
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "CPA_DISPATCH_FAILED"
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))


def test_missing_runtime_identity_is_evidence_only_without_callbacks(tmp_path: Path):
    values = _inputs(tmp_path)
    values.pop("runtime_identity")
    calls = {"observer": 0, "judge": 0}

    def observer(_request):
        calls["observer"] += 1
        raise AssertionError("missing runtime identity must not dispatch")

    def judge(_request, _observation):
        calls["judge"] += 1
        raise AssertionError("missing runtime identity must not judge")

    result = _call(values, observer, judge)
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "RUNTIME_IDENTITY_REQUIRED"
    assert calls == {"observer": 0, "judge": 0}
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))
