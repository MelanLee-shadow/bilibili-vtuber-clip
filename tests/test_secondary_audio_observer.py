import hashlib
import json

from src.autoslice import secondary_audio_observer as module
from src.autoslice.acoustic_witness_adjudication import WITNESS_REQUEST_SCHEMA
from src.autoslice.acoustic_witness_protocol import BLIND_PINYIN_PROTOCOL


def _seal(payload):
    value = dict(payload)
    value["request_sha256"] = hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return value


def _request():
    return _seal({
        "schema_version": WITNESS_REQUEST_SCHEMA,
        "witness_protocol": BLIND_PINYIN_PROTOCOL,
        "kind": "subtitle_span_acoustic_witness",
        "cue_indexes": [21],
        "matched_start_ms": 54560,
        "matched_end_ms": 56070,
        "context_start_ms": 51320,
        "context_end_ms": 61220,
        "source_media_timeline_offset_ms": 0,
    })


def test_secondary_observer_preserves_blind_geometry_and_has_no_mutation_authority(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    def crop(**kwargs):
        kwargs["audio_path"].write_bytes(b"crop")
        return True, ""

    def extract(_source, output):
        output.write_bytes(b"mp3")
        return True, ""

    seen = {}
    def secondary(audio, **kwargs):
        seen.update(kwargs)
        assert audio == b"mp3"
        return {
            "provider": "moss",
            "model": "moss-transcribe-diarize-pro",
            "input_audio_sha256": hashlib.sha256(audio).hexdigest(),
            "response_sha256": "b" * 64,
            "native_segments": [
                {"start_ms": 300, "end_ms": 800, "text": "听到的原文", "speaker": "S00",
                 "words": [{"start_ms": 350, "end_ms": 500, "text": "听到"}]},
            ],
            "served_from_cache": False,
        }

    monkeypatch.setattr(module, "_crop_black_frame_audio", crop)
    monkeypatch.setattr(module, "_extract_mp3", extract)
    monkeypatch.setattr(module, "observe_secondary", secondary)
    observer = module.build_secondary_audio_observer(
        source_media=source, output_dir=tmp_path / "out", source_duration_ms=225910,
        provider="moss",
    )
    result = observer(_request())

    assert result["status"] == "OBSERVED"
    assert result["authority"] == "EVIDENCE_ONLY"
    assert result["mutation_authorized"] is False
    assert result["candidate_exposure"] == "none"
    assert result["provider"] == "moss"
    assert result["timeline_binding"]["source_media"] == {
        "target_start_ms": 54560, "target_end_ms": 56070,
        "crop_start_ms": 54100, "crop_end_ms": 56500,
    }
    assert result["native_segments"][0]["start_ms"] == 54400
    assert result["native_segments"][0]["words"][0]["start_ms"] == 54450
    assert len(result["target_overlap_segments"]) == 1
    assert seen["provider"] == "moss"
    assert seen["crop_start_ms"] == 54100
    assert seen["crop_end_ms"] == 56500


def test_secondary_observer_rejects_candidate_bearing_request_before_crop(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    called = False
    def crop(**_kwargs):
        nonlocal called
        called = True
        return True, ""
    monkeypatch.setattr(module, "_crop_black_frame_audio", crop)
    observer = module.build_secondary_audio_observer(
        source_media=source, output_dir=tmp_path / "out", source_duration_ms=225910,
        provider="mai",
    )
    request = _request()
    request["candidate_entities"] = [{"canonical": "答案"}]
    result = observer(request)
    assert result["status"] == "INVALID"
    assert result["reason_code"] == "SECONDARY_AUDIO_REQUEST_INVALID"
    assert called is False


def test_secondary_provider_is_explicit_and_never_hidden_fallback(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    try:
        module.build_secondary_audio_observer(
            source_media=source, output_dir=tmp_path / "out", source_duration_ms=1000,
            provider="auto",
        )
    except ValueError as exc:
        assert "mai or moss" in str(exc)
    else:
        raise AssertionError("implicit secondary provider selection was accepted")
