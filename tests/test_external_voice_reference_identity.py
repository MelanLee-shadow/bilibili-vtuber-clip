from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.nested_media_acoustic_observer import (
    NestedMediaAcousticObserverError,
    VOICE_REFERENCE_MODE,
    _canonical_hash,
    observe_nested_media_acoustics,
)
from src.autoslice.producer_nested_acoustic import _voice_reference_binding_payload


def _write(path: Path, payload: bytes) -> str:
    path.write_bytes(payload)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _runtime() -> None:
    # The constructor must accept the official source without dispatching.  A
    # missing runtime identity is the existing prepare-only path.
    return None


def _files(tmp_path: Path) -> dict[str, tuple[Path, str]]:
    return {
        "outer": (tmp_path / "outer.mp4", _write(tmp_path / "outer.mp4", b"outer")),
        "srt": (tmp_path / "current.srt", _write(tmp_path / "current.srt", b"1\n00:00:22,090 --> 00:00:23,090\nvoice\n")),
        "frame": (tmp_path / "frame.png", _write(tmp_path / "frame.png", b"frame")),
        "source": (tmp_path / "official.mp3", _write(tmp_path / "official.mp3", b"official-full-source")),
        "host": (tmp_path / "host.wav", _write(tmp_path / "host.wav", b"host")),
        "source_audio": (tmp_path / "source-crop.wav", _write(tmp_path / "source-crop.wav", b"source-pcm")),
        "target_audio": (tmp_path / "target.wav", _write(tmp_path / "target.wav", b"target-pcm")),
    }


def _binding(tmp_path: Path, files: dict[str, tuple[Path, str]], *, external: bool = True) -> tuple[Path, str]:
    outer, srt, frame = files["outer"], files["srt"], files["frame"]
    source, host = files["source"], files["host"]
    source_audio, target_audio = files["source_audio"], files["target_audio"]
    binding: dict[str, object] = {
        "schema_version": "nested-media-voice-reference-binding.v1",
        "status": "REFERENCE_BOUND",
        "mode": VOICE_REFERENCE_MODE,
        "candidate_id": "official-audio-candidate",
        "observation_id": "preflight-01",
        "outer_media": {"path": str(outer[0]), "sha256": outer[1]},
        "final_srt": {"path": str(srt[0]), "sha256": srt[1]},
        "caption_frame": {"path": str(frame[0]), "sha256": frame[1], "frame_ms": 22500},
        "source_media": {"path": str(source[0]), "sha256": source[1]},
        "host_enrollment": {"path": str(host[0]), "sha256": host[1]},
        "source_audio": {"path": str(source_audio[0]), "sha256": source_audio[1]},
        "target_audio": {"path": str(target_audio[0]), "sha256": target_audio[1]},
        "outer_context_interval_ms": [22090, 23090],
        "reference_window_ms": [0, 1384],
        "target_interval_ms": [22090, 23090],
        "target_cue_indexes": [7],
        "time_alignment_performed": False,
        "source_subtraction_authorized": False,
        "caption_dedup_authority": False,
        "source_role_authority": False,
        "mutation_authorized": False,
    }
    if external:
        uri = "https://example.invalid/audio/like.mp3"
        binding["source_identity"] = {
            "kind": "OFFICIAL_EXTERNAL_AUDIO",
            "source_uri": uri,
            "source_sha256": source[1],
        }
        binding["source_media"].update({"source_uri": uri, "source_sha256": source[1]})
    else:
        binding["bvid"] = "BVlegacy"
        binding["cid"] = "123"
        binding["source_media"].update({"bvid": "BVlegacy", "cid": "123"})
    path = tmp_path / ("external-binding.json" if external else "legacy-binding.json")
    raw = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text(raw, encoding="utf-8")
    return path, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _observe(tmp_path: Path, files: dict[str, tuple[Path, str]], binding: tuple[Path, str]) -> dict[str, object]:
    calls = {"observe": 0, "cpa": 0}

    def observe(_request):
        calls["observe"] += 1
        raise AssertionError("prepare-only constructor must not call provider")

    def cpa(_request, _observation):
        calls["cpa"] += 1
        raise AssertionError("prepare-only constructor must not call CPA")

    result = observe_nested_media_acoustics(
        candidate_id="official-audio-candidate",
        observation_id="preflight-01",
        outer_media_path=files["outer"][0], outer_media_sha256=files["outer"][1],
        final_srt_path=files["srt"][0], final_srt_sha256=files["srt"][1],
        caption_frame_path=files["frame"][0], caption_frame_sha256=files["frame"][1],
        interval_ms=[22090, 23090],
        independent_source_media_path=files["source"][0], independent_source_media_sha256=files["source"][1],
        mapping_receipt_path=None, mapping_receipt_sha256=None,
        host_enrollment_path=files["host"][0], host_enrollment_sha256=files["host"][1],
        source_audio_path=files["source_audio"][0], source_audio_sha256=files["source_audio"][1],
        target_audio_path=files["target_audio"][0], target_audio_sha256=files["target_audio"][1],
        evidence_root=tmp_path / "evidence", observe_audio=observe, cpa_judge=cpa,
        runtime_identity=_runtime(), caption_frame_ms=22500,
        target_interval_ms=[22090, 23090], target_cue_indexes=[7],
        observer_mode=VOICE_REFERENCE_MODE,
        voice_reference_binding_path=binding[0], voice_reference_binding_sha256=binding[1],
    )
    assert calls == {"observe": 0, "cpa": 0}
    return result


def test_official_external_identity_and_pcm_hashes_are_bound_without_dispatch(tmp_path: Path) -> None:
    files = _files(tmp_path)
    binding = _binding(tmp_path, files)
    result = _observe(tmp_path, files, binding)
    assert result["reason_code"] == "RUNTIME_IDENTITY_REQUIRED"
    request_path = Path(str(result["request_path"]))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    info = request["voice_reference_binding"]
    assert info["source_identity"]["source_sha256"] == files["source"][1]
    assert info["source_media"]["source_sha256"] == files["source"][1]
    assert info["source_audio"]["sha256"] == files["source_audio"][1]
    assert info["target_audio"]["sha256"] == files["target_audio"][1]
    assert "source_audio_scope" not in info
    assert request["target_interval_ms"] == [22090, 23090]


def test_legacy_bvid_cid_identity_remains_valid(tmp_path: Path) -> None:
    files = _files(tmp_path)
    binding = _binding(tmp_path, files, external=False)
    result = _observe(tmp_path, files, binding)
    assert result["reason_code"] == "RUNTIME_IDENTITY_REQUIRED"
    request = json.loads(Path(str(result["request_path"])).read_text(encoding="utf-8"))
    assert request["voice_reference_binding"]["source_media"]["bvid"] == "BVlegacy"
    assert "source_identity" not in request["voice_reference_binding"]


def test_external_source_identity_drift_blocks(tmp_path: Path) -> None:
    files = _files(tmp_path)
    binding_path, _ = _binding(tmp_path, files)
    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    payload["source_identity"]["source_sha256"] = "sha256:" + "0" * 64
    binding_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    binding_sha = "sha256:" + hashlib.sha256(binding_path.read_bytes()).hexdigest()
    with pytest.raises(NestedMediaAcousticObserverError, match="SOURCE_IDENTITY_MISMATCH"):
        _observe(tmp_path, files, (binding_path, binding_sha))


def test_producer_payload_carries_external_identity(tmp_path: Path) -> None:
    files = _files(tmp_path)
    payload = _voice_reference_binding_payload(
        candidate_id="official-audio-candidate", observation_id="preflight-01",
        outer=(files["outer"][0], files["outer"][1]),
        srt=(files["srt"][0], files["srt"][1]),
        frame=(files["frame"][0], files["frame"][1], 22500),
        source=(files["source"][0], files["source"][1]),
        host=(files["host"][0], files["host"][1]),
        source_identity={
            "kind": "OFFICIAL_EXTERNAL_AUDIO",
            "source_uri": "https://example.invalid/audio/like.mp3",
            "source_sha256": files["source"][1],
        },
        context_interval=[22090, 23090], reference_window=[0, 1384],
        target_interval=[22090, 23090], target_cues=[7],
        source_audio=(files["source_audio"][0], files["source_audio"][1]),
        target_audio=(files["target_audio"][0], files["target_audio"][1]),
    )
    assert payload["source_identity"]["kind"] == "OFFICIAL_EXTERNAL_AUDIO"
    assert payload["source_media"]["source_sha256"] == files["source"][1]
    assert "bvid" not in payload and "cid" not in payload
