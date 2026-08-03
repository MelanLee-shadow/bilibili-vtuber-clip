from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

import src.autoslice.collab_evidence_capture as capture_module
from src.autoslice.collab_evidence_capture import (
    CollabEvidenceCaptureError,
    capture_session as _capture_session_impl,
    evaluate_trigger,
    rebuild_queue,
)
import scripts.session_autoslice as runner


def capture_session(*args, **kwargs):
    kwargs.setdefault("duration_probe", lambda _path: 600_000)
    return _capture_session_impl(*args, **kwargs)


def _candidate(
    source: Path,
    srt: Path,
    *,
    cid: str = "candidate-1",
    start_ms: int = 1_000,
    end_ms: int = 4_000,
    hook: str = "联动嘉宾",
    preview: str = "",
) -> dict:
    return {
        "cid": cid,
        "segment_path": str(source),
        "bcut_srt_path": str(srt),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "hook": hook,
        "preview": preview,
        # These must never be copied into the evidence manifest.
        "danmaku": "SECRET_DANMAKU_TEXT",
        "uid": "SECRET_UID",
    }


def _routing_claim(cid: str, verdict: str) -> dict:
    return {
        "status": "READY",
        "provider": {"name": "already-validated-provider"},
        "provider_evidence_sha256": "a" * 64,
        "candidate_results": [{"candidate_id": cid, "verdict": verdict}],
    }


def _fake_extractor(source: Path, start_ms: int, end_ms: int, dest: Path) -> None:
    del source
    frames = round((end_ms - start_ms) * 16_000 / 1000)
    with wave.open(str(dest), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\0\0" * frames)


def _srt_time(ms: int) -> str:
    return (
        f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:"
        f"{ms // 1_000 % 60:02d},{ms % 1_000:03d}"
    )


def _srt_cues(count: int = 1) -> str:
    blocks = []
    for index in range(count):
        start = index * 2_000
        end = start + 1_000
        blocks.append(
            f"{index + 1}\n{_srt_time(start)} --> {_srt_time(end)}\ncue {index + 1}"
        )
    return "\n\n".join(blocks) + "\n"


def test_evaluate_trigger_requires_independent_text_families_or_provider_result():
    candidates = [
        {"cid": "c1", "hook": "今天联动", "preview": ""},
        {"cid": "c2", "hook": "", "preview": "和嘉宾聊天"},
    ]
    decision = evaluate_trigger(None, candidates)
    assert decision.triggered is True
    assert decision.text_signal_classes == ("EXPLICIT_COLLAB", "GUEST_ROLE")

    repeated_family = evaluate_trigger(
        None,
        [
            {"cid": "c1", "hook": "联动", "preview": ""},
            {"cid": "c2", "hook": "一起直播", "preview": ""},
        ],
    )
    assert repeated_family.triggered is False

    provider = evaluate_trigger(_routing_claim("c1", "UNCERTAIN"), candidates[:1])
    assert provider.triggered is True
    assert provider.reason_codes == ("PROVIDER_CAPTURE_TRIGGER",)
    assert provider.text_signal_classes == ()
    assert provider.routing_candidate_ids == ()

    unbacked = dict(_routing_claim("c1", "MULTI_SPEAKER"))
    unbacked["provider_evidence_sha256"] = None
    assert evaluate_trigger(unbacked, candidates[:1]).triggered is False


def test_no_trigger_is_zero_filesystem_hash_and_extractor_io(tmp_path: Path):
    base = tmp_path / "must-not-exist"
    candidate = {
        "cid": "solo",
        "segment_path": str(tmp_path / "missing.mp4"),
        "bcut_srt_path": str(tmp_path / "missing.srt"),
        "start_ms": 0,
        "end_ms": 1_000,
        "hook": "普通直播",
        "preview": "一个人聊天",
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("NO_TRIGGER called an injected I/O callback")

    result = capture_session(
        "not-even-a-date",
        [candidate],
        routing_claim=None,
        base_dir=base,
        extractor=forbidden,
        hash_file=forbidden,
    )
    assert result["status"] == "NO_TRIGGER"
    assert not base.exists()


def test_capture_filters_song_interval_and_writes_only_unlabelled_authority(tmp_path: Path):
    source = tmp_path / "recording.mp4"
    srt = tmp_path / "recording.srt"
    source.write_bytes(b"sealed recording")
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\ntext\n", encoding="utf-8")
    candidates = [
        _candidate(source, srt, cid="keep", start_ms=1_000, end_ms=3_000),
        _candidate(source, srt, cid="song-overlap", start_ms=5_000, end_ms=8_000),
    ]
    result = capture_session(
        "2026-07-13",
        candidates,
        routing_claim=None,
        song_intervals=[
            {"segment_path": str(source), "start_ms": 4_500, "end_ms": 8_500}
        ],
        base_dir=tmp_path / "capture",
        extractor=_fake_extractor,
    )
    assert result["status"] == "CAPTURED"
    assert result["cue_count"] == 1

    manifest_path = Path(result["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["cue_count"] == 1
    assert manifest["cues"][0]["source_cue"] == 1
    assert manifest["cues"][0]["text_sha256"]
    assert manifest["cues"][0]["label"] is None
    assert manifest["cues"][0]["prediction"] is None
    for field in (
        "labels_present",
        "predictions_present",
        "training",
        "upload",
        "authorized_for_training",
        "authorized_for_upload",
    ):
        assert manifest[field] is False
    serialized = manifest_path.read_text(encoding="utf-8")
    assert "SECRET_DANMAKU_TEXT" not in serialized
    assert "SECRET_UID" not in serialized
    assert "联动嘉宾" not in serialized

    queue = json.loads(
        (tmp_path / "capture" / "queue" / "queue.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert queue["candidate_session_count"] == 1
    assert queue["entries"][0]["capture_id"] == result["capture_id"]

    duplicate = capture_session(
        "2026-07-13",
        candidates,
        routing_claim=None,
        song_intervals=[
            {"segment_path": str(source), "start_ms": 4_500, "end_ms": 8_500}
        ],
        base_dir=tmp_path / "capture",
        extractor=lambda *_args: pytest.fail("dedup should precede extraction"),
    )
    assert duplicate["status"] == "ALREADY_CAPTURED"
    assert duplicate["queue"]["candidate_session_count"] == 1
    cue = manifest["cues"][0]
    assert len(cue["cue_id"]) == 64
    assert cue["audio_path"] == f"{cue['cue_id']}.wav"
    assert manifest["manifest_integrity_sha256"]


def test_source_drift_during_extraction_leaves_no_manifest_queue_or_alert(tmp_path: Path):
    source = tmp_path / "recording.mp4"
    srt = tmp_path / "recording.srt"
    source.write_bytes(b"sealed recording")
    srt.write_text(_srt_cues(), encoding="utf-8")
    base = tmp_path / "capture"

    def drifting_extractor(
        source_path: Path, start_ms: int, end_ms: int, dest: Path
    ) -> None:
        _fake_extractor(source_path, start_ms, end_ms, dest)
        source_path.write_bytes(source_path.read_bytes() + b"drift")

    with pytest.raises(CollabEvidenceCaptureError, match="changed during extraction"):
        capture_session(
            "2026-07-12",
            [_candidate(source, srt)],
            routing_claim=None,
            base_dir=base,
            extractor=drifting_extractor,
        )
    assert not list(base.glob("sessions/*/*/manifest.json"))
    assert not (base / "queue" / "queue.v1.json").exists()
    assert not list(base.glob("alerts/ALERT_COLLAB_EVIDENCE_CANDIDATE_QUOTA_*.txt"))


def test_queue_dedup_and_quota_alert_is_created_once_with_o_excl(tmp_path: Path):
    base = tmp_path / "capture"
    for day in range(13, 18):
        source = tmp_path / f"recording-{day}.mp4"
        srt = tmp_path / f"recording-{day}.srt"
        source.write_bytes(f"sealed recording {day}".encode())
        srt.write_text(_srt_cues(), encoding="utf-8")
        result = capture_session(
            f"2026-07-{day:02d}",
            [_candidate(source, srt, cid=f"candidate-{day}")],
            routing_claim=None,
            base_dir=base,
            extractor=_fake_extractor,
        )
        assert result["status"] == "CAPTURED"

    alerts = list(base.glob("alerts/ALERT_COLLAB_EVIDENCE_CANDIDATE_QUOTA_*.txt"))
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.is_file()
    original = alert.read_bytes()
    queue = rebuild_queue(base)
    assert queue["candidate_session_count"] == 5
    assert queue["candidate_session_quota_reached"] is True
    assert queue["confirmed_collab_session_count"] == 0
    assert queue["training_ready"] is False
    assert alert.read_bytes() == original


def test_capture_caps_deterministic_sample_at_120(tmp_path: Path):
    source = tmp_path / "recording.mp4"
    srt = tmp_path / "recording.srt"
    source.write_bytes(b"sealed recording")
    srt.write_text(_srt_cues(121), encoding="utf-8")
    candidates = [_candidate(source, srt)]
    count = 0

    def extracting(source_path: Path, start_ms: int, end_ms: int, dest: Path) -> None:
        nonlocal count
        count += 1
        _fake_extractor(source_path, start_ms, end_ms, dest)

    result = capture_session(
        "2026-07-13",
        candidates,
        routing_claim=None,
        base_dir=tmp_path / "capture",
        extractor=extracting,
    )
    assert result["cue_count"] == 120
    assert count == 120


def test_triggered_capture_rejects_symlink_input(tmp_path: Path):
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"sealed recording")
    linked = tmp_path / "linked.mp4"
    linked.symlink_to(source)
    srt = tmp_path / "recording.srt"
    srt.write_text("sealed", encoding="utf-8")
    with pytest.raises(CollabEvidenceCaptureError, match="symlink|escapes"):
        capture_session(
            "2026-07-12",
            [_candidate(linked, srt)],
            routing_claim=None,
            base_dir=tmp_path / "capture",
            extractor=_fake_extractor,
        )


def test_capture_rejects_non_wav_extractor_output(tmp_path: Path):
    source = tmp_path / "recording.mp4"
    srt = tmp_path / "recording.srt"
    source.write_bytes(b"sealed recording")
    srt.write_text(_srt_cues(), encoding="utf-8")

    def invalid_extractor(_source, _start, _end, destination):
        destination.write_bytes(b"x")

    with pytest.raises(CollabEvidenceCaptureError, match="WAV"):
        capture_session(
            "2026-07-13",
            [_candidate(source, srt)],
            routing_claim=None,
            base_dir=tmp_path / "capture",
            extractor=invalid_extractor,
        )
    assert not list((tmp_path / "capture").glob("sessions/*/*/manifest.json"))


def test_capture_rejects_srt_cue_outside_probed_source_duration(tmp_path: Path):
    source = tmp_path / "recording.mp4"
    srt = tmp_path / "recording.srt"
    source.write_bytes(b"sealed recording")
    srt.write_text(_srt_cues(), encoding="utf-8")
    result = _capture_session_impl(
        "2026-07-13",
        [_candidate(source, srt)],
        routing_claim=None,
        base_dir=tmp_path / "capture",
        extractor=lambda *_args: pytest.fail("out-of-bounds cue must not extract"),
        duration_probe=lambda _path: 500,
    )
    assert result["status"] == "NO_ELIGIBLE_CUES"


def test_queue_rejects_manifest_and_audio_tamper(tmp_path: Path):
    source = tmp_path / "recording.mp4"
    srt = tmp_path / "recording.srt"
    source.write_bytes(b"sealed recording")
    srt.write_text(_srt_cues(), encoding="utf-8")
    base = tmp_path / "capture"
    result = capture_session(
        "2026-07-13",
        [_candidate(source, srt)],
        routing_claim=None,
        base_dir=base,
        extractor=_fake_extractor,
    )
    manifest_path = Path(result["manifest_path"])
    original_manifest = manifest_path.read_bytes()
    document = json.loads(original_manifest)
    document["training_ready"] = True
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CollabEvidenceCaptureError, match="integrity"):
        rebuild_queue(base)

    manifest_path.write_bytes(original_manifest)
    document = json.loads(original_manifest)
    audio = manifest_path.parent / document["cues"][0]["audio_path"]
    audio.write_bytes(audio.read_bytes() + b"tamper")
    with pytest.raises(CollabEvidenceCaptureError, match="audio binding"):
        rebuild_queue(base)


def test_preexisting_development_date_does_not_count_toward_future_quota(tmp_path: Path):
    source = tmp_path / "recording.mp4"
    srt = tmp_path / "recording.srt"
    source.write_bytes(b"sealed recording")
    srt.write_text(_srt_cues(), encoding="utf-8")
    result = capture_session(
        "2026-07-12",
        [_candidate(source, srt)],
        routing_claim=None,
        base_dir=tmp_path / "capture",
        extractor=_fake_extractor,
    )
    assert result["queue"]["candidate_session_count"] == 0
    assert result["queue"]["candidate_session_quota_reached"] is False


def test_retry_subset_cannot_count_one_livestream_date_twice(tmp_path: Path):
    base = tmp_path / "capture"
    first_source = tmp_path / "first.mp4"
    first_srt = tmp_path / "first.srt"
    first_source.write_bytes(b"first source")
    first_srt.write_text(_srt_cues(), encoding="utf-8")
    first = capture_session(
        "2026-07-13",
        [_candidate(first_source, first_srt)],
        routing_claim=None,
        base_dir=base,
        extractor=_fake_extractor,
    )
    assert first["queue"]["candidate_session_count"] == 1

    second_source = tmp_path / "retry-subset.mp4"
    second_srt = tmp_path / "retry-subset.srt"
    second_source.write_bytes(b"different retry source")
    second_srt.write_text(_srt_cues(), encoding="utf-8")
    with pytest.raises(CollabEvidenceCaptureError, match="same livestream date"):
        capture_session(
            "2026-07-13",
            [_candidate(second_source, second_srt)],
            routing_claim=None,
            base_dir=base,
            extractor=_fake_extractor,
        )
    assert rebuild_queue(base)["candidate_session_count"] == 1
    assert len(list(base.glob("sessions/2026-07-13/*/manifest.json"))) == 1


def test_runner_capture_queue_is_sanitized_and_cannot_change_production_state(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(runner, "BASE", tmp_path)
    launched = []

    class FakeProcess:
        pid = 4321

    def fake_popen(command, **kwargs):
        launched.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    state = {
        "status": "processing",
        "pending_talk": [{"cid": "keep"}],
        "picks": [{"candidate_id": "done"}],
        "song_quarantine_intervals": [],
    }
    before = {key: state[key] for key in ("status", "pending_talk", "picks")}
    summary = runner.queue_collab_evidence_capture(
        "2026-07-13",
        state,
        [
            {
                "cid": "provider-sensitive-id",
                "segment_path": "/recordings/session.mp4",
                "bcut_srt_path": "/cache/session.srt",
                "hook": "联动嘉宾",
                "preview": "private text must not persist",
            }
        ],
        routing_claim=None,
    )
    assert summary["status"] == "CAPTURE_QUEUED"
    assert summary["worker_pid"] == 4321
    assert {key: state[key] for key in before} == before
    assert len(launched) == 1
    request = json.loads(
        (tmp_path / "state" / "collab-evidence" / "requests" / "2026-07-13.json").read_text()
    )
    serialized = json.dumps(request, ensure_ascii=False)
    assert "provider-sensitive-id" not in serialized
    assert "private text" not in serialized
    assert "MULTI_SPEAKER" not in serialized
    assert request["trigger"]["reason_codes"] == [
        "TEXT_SIGNAL_CLASSES:EXPLICIT_COLLAB,GUEST_ROLE"
    ]

    no_trigger_base = tmp_path / "solo"
    monkeypatch.setattr(runner, "BASE", no_trigger_base)
    summary = runner.queue_collab_evidence_capture(
        "not-even-a-date",
        {},
        [{"hook": "普通独播", "preview": "一个人聊天"}],
        routing_claim=None,
    )
    assert summary["status"] == "NO_TRIGGER"
    assert not no_trigger_base.exists()


def test_runner_rechecks_disabled_before_worker_launch(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runner, "BASE", tmp_path)
    (tmp_path / "DISABLED").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("DISABLED must prevent worker launch"),
    )
    summary = runner.queue_collab_evidence_capture(
        "2026-07-13",
        {},
        [
            {
                "segment_path": "/recordings/session.mp4",
                "bcut_srt_path": "/cache/session.srt",
                "hook": "联动嘉宾",
            }
        ],
        routing_claim=None,
    )
    assert summary["status"] == "SKIPPED_DISABLED"
    assert not (tmp_path / "state" / "collab-evidence").exists()


def test_bounded_worker_consumes_only_opaque_request(monkeypatch, tmp_path: Path):
    base = tmp_path / "capture"
    requests = base / "requests"
    requests.mkdir(parents=True)
    source = tmp_path / "source.mp4"
    srt = tmp_path / "source.srt"
    source.write_bytes(b"source")
    srt.write_text(_srt_cues(), encoding="utf-8")
    request = {
        "schema_version": capture_module.WORKER_REQUEST_SCHEMA_VERSION,
        "date": "2026-07-13",
        "candidates": [
            {"segment_path": str(source), "bcut_srt_path": str(srt)}
        ],
        "song_intervals": [],
        "trigger": {
            "reason_codes": ["PROVIDER_CAPTURE_TRIGGER"],
            "text_signal_classes": [],
        },
    }
    request_path = requests / "2026-07-13.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(capture_module, "ffprobe_duration_ms", lambda _path: 10_000)
    monkeypatch.setattr(capture_module, "ffmpeg_extract_cue", _fake_extractor)
    result = capture_module.run_worker_request(
        request_path,
        base_dir=base,
        alert_dir=tmp_path / "reports",
    )
    assert result["worker_status"] == "COMPLETE"
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert "MULTI_SPEAKER" not in serialized
    assert "UNCERTAIN" not in serialized
    assert "candidate-sensitive" not in serialized
    assert manifest["trigger"]["reason_codes"] == ["PROVIDER_CAPTURE_TRIGGER"]
    assert manifest["predictions_present"] is False


@pytest.mark.parametrize(
    ("reason_codes", "text_signal_classes"),
    [
        (["PROVIDER_CAPTURE_TRIGGER"], ["MULTI_SPEAKER:candidate-sensitive"]),
        (
            ["TEXT_SIGNAL_CLASSES:EXPLICIT_COLLAB,GUEST_ROLE"],
            ["EXPLICIT_COLLAB", "LIVE_CONNECTION"],
        ),
        (["PROVIDER_CAPTURE_TRIGGER"], ["EXPLICIT_COLLAB"]),
        (
            ["TEXT_SIGNAL_CLASSES:EXPLICIT_COLLAB,GUEST_ROLE"],
            ["GUEST_ROLE", "EXPLICIT_COLLAB"],
        ),
    ],
)
def test_worker_rejects_noncanonical_trigger_authority(
    tmp_path: Path,
    reason_codes: list[str],
    text_signal_classes: list[str],
):
    base = tmp_path / "capture"
    requests = base / "requests"
    requests.mkdir(parents=True)
    request_path = requests / "2026-07-13.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": capture_module.WORKER_REQUEST_SCHEMA_VERSION,
                "date": "2026-07-13",
                "candidates": [
                    {
                        "segment_path": "/recordings/session.mp4",
                        "bcut_srt_path": "/cache/session.srt",
                    }
                ],
                "song_intervals": [],
                "trigger": {
                    "reason_codes": reason_codes,
                    "text_signal_classes": text_signal_classes,
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CollabEvidenceCaptureError, match="trigger"):
        capture_module.run_worker_request(
            request_path,
            base_dir=base,
            alert_dir=tmp_path / "reports",
        )


@pytest.mark.parametrize(
    "interval",
    [
        {"segment_path": "/recordings/session.mp4", "start_ms": "0", "end_ms": "1000"},
        {"segment_path": "/recordings/session.mp4", "start_ms": False, "end_ms": 1_000},
        {"segment_path": "/recordings/session.mp4", "start_ms": -1, "end_ms": 1_000},
        {"segment_path": "/recordings/session.mp4", "start_ms": 1_000, "end_ms": 1_000},
        {"segment_path": "session.mp4", "start_ms": 0, "end_ms": 1_000},
        {"segment_path": "/other/session.mp4", "start_ms": 0, "end_ms": 1_000},
    ],
)
def test_worker_rejects_invalid_song_interval_authority(
    tmp_path: Path,
    interval: dict,
):
    base = tmp_path / "capture"
    requests = base / "requests"
    requests.mkdir(parents=True)
    request_path = requests / "2026-07-13.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": capture_module.WORKER_REQUEST_SCHEMA_VERSION,
                "date": "2026-07-13",
                "candidates": [
                    {
                        "segment_path": "/recordings/session.mp4",
                        "bcut_srt_path": "/cache/session.srt",
                    }
                ],
                "song_intervals": [interval],
                "trigger": {
                    "reason_codes": ["PROVIDER_CAPTURE_TRIGGER"],
                    "text_signal_classes": [],
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CollabEvidenceCaptureError, match="song interval"):
        capture_module.run_worker_request(
            request_path,
            base_dir=base,
            alert_dir=tmp_path / "reports",
        )


def test_runner_rejects_tampered_existing_worker_request(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runner, "BASE", tmp_path)
    request_path = (
        tmp_path
        / "state"
        / "collab-evidence"
        / "requests"
        / "2026-07-13.json"
    )
    request_path.parent.mkdir(parents=True)
    request_path.write_text(
        json.dumps(
            {
                "schema_version": capture_module.WORKER_REQUEST_SCHEMA_VERSION,
                "date": "2026-07-13",
                "candidates": [
                    {
                        "segment_path": "/recordings/session.mp4",
                        "bcut_srt_path": "/cache/session.srt",
                    }
                ],
                "song_intervals": [],
                "trigger": {
                    "reason_codes": ["PROVIDER_CAPTURE_TRIGGER"],
                    "text_signal_classes": ["MULTI_SPEAKER:candidate-sensitive"],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("invalid request must not launch worker"),
    )
    summary = runner.queue_collab_evidence_capture(
        "2026-07-13",
        {},
        [
            {
                "segment_path": "/recordings/session.mp4",
                "bcut_srt_path": "/cache/session.srt",
                "hook": "联动嘉宾",
            }
        ],
        routing_claim=None,
    )
    assert summary["status"] == "CAPTURE_QUEUE_FAILED"
    assert "trigger" in str(summary["error"])


def test_capture_rejects_symlinked_output_subdirectory(tmp_path: Path):
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"sealed recording")
    srt = tmp_path / "recording.srt"
    srt.write_text(_srt_cues(), encoding="utf-8")
    base = tmp_path / "capture"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (base / ".staging").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CollabEvidenceCaptureError, match="symlink|escapes"):
        capture_session(
            "2026-07-13",
            [_candidate(source, srt)],
            routing_claim=None,
            base_dir=base,
            extractor=_fake_extractor,
        )
    assert list(outside.iterdir()) == []
