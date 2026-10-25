import hashlib
import json
import shlex
from pathlib import Path

import pytest

import scripts.free_asr_client as free
from src.autoslice import full_session_transcription as transcription
from src.autoslice.bcut_draft_observation import (
    BcutDraftObservationError,
    SCHEMA_VERSION,
    load_bcut_draft_observation,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding(path: Path) -> dict[str, object]:
    return {"path": str(path), "sha256": f"sha256:{_sha(path)}", "bytes": path.stat().st_size}


def _fixture(tmp_path: Path, *, utterances=None):
    source = tmp_path / "source-original.mp4"
    source.write_bytes(b"immutable source bytes")
    audio = tmp_path / "source-context.mp3"
    audio.write_bytes(b"source audio bytes")
    raw = tmp_path / "bcut.json"
    raw.write_text(
        json.dumps(
            {
                "provider": "bcut",
                "utterances": utterances
                or [
                    {
                        "start_time": 500,
                        "end_time": 800,
                        "transcript": "before",
                        "words": [],
                    },
                    {
                        "start_time": 1200,
                        "end_time": 1600,
                        "transcript": "inside",
                        "words": [{"start_time": 1200, "end_time": 1600, "word": "inside"}],
                    },
                    {
                        "start_time": 2200,
                        "end_time": 2700,
                        "transcript": "tail",
                        "words": [],
                    },
                    {
                        "start_time": 3200,
                        "end_time": 3500,
                        "transcript": "after",
                        "words": [],
                    },
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    request = tmp_path / "BCUT-INPUT.json"
    request.write_text(
        json.dumps(
            {
                "schema": "test-bcut-input.v1",
                "source_sha256": _sha(source),
                "source_audio_sha256": f"sha256:{_sha(audio)}",
                "source_context_ms": [0, 5000],
                "source_offset_ms": 0,
                "argv": [
                    "/opt/bilive/venv/bin/python",
                    "-B",
                    "/opt/bilive/autoslice/repo/scripts/free_asr_client.py",
                    str(audio),
                    "--provider",
                    "bcut",
                    "--poll-interval",
                    "3",
                    "--poll-timeout",
                    "600",
                    "--srt",
                    str(tmp_path / "bcut.srt"),
                    "--json",
                    str(raw),
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    output_root = tmp_path / "producer-output"
    output_root.mkdir()
    piece = output_root / "piece_0_1000_3000.mp4"
    piece.write_bytes(b"piece bytes")
    padded = output_root / "padded_1000_3000.mp4"
    padded.write_bytes(piece.read_bytes())
    source_sha = _sha(source)
    piece_sha = _sha(piece)
    piece_provenance = piece.with_suffix(".provenance.json")
    piece_provenance.write_text(
        json.dumps(
            {
                "source_path": str(source),
                "source_sha256": source_sha,
                "source_media_binding": f"sha256:{source_sha}",
                "start_ms": 1000,
                "end_ms": 3000,
                "output_path": str(piece.resolve()),
                "output_sha256": piece_sha,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    padded.with_suffix(".provenance.json").write_text(
        json.dumps(
            {
                "inputs": [{"path": str(piece.resolve()), "sha256": piece_sha}],
                "output_path": str(padded.resolve()),
                "output_sha256": _sha(padded),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    observation = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": "fixture-candidate",
        "provider": "bcut",
        "source_interval_ms": [1000, 3000],
        "source_context_ms": [0, 5000],
        "source_offset_ms": 0,
        "source_media": _binding(source),
        "raw_response": _binding(raw),
        "source_audio": _binding(audio),
        "request_receipt": _binding(request),
    }
    spec = {
        "id": "fixture-candidate",
        "pieces": [
            {
                "remote_media": str(source),
                "start_ms": 1000,
                "end_ms": 3000,
                "source_media_sha256": f"sha256:{source_sha}",
            }
        ]
    }
    return observation, spec, padded, raw, request, audio


def _rewrite_request(observation, request: Path, **changes):
    document = json.loads(request.read_text(encoding="utf-8"))
    document.update(changes)
    request.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    observation["request_receipt"] = _binding(request)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _c20_fixture(tmp_path: Path):
    observation, spec, padded, raw, request, audio = _fixture(tmp_path)
    asr_dir = tmp_path / "asr-auto"
    asr_dir.mkdir()
    raw.rename(asr_dir / raw.name)
    request.rename(asr_dir / request.name)
    audio.rename(asr_dir / audio.name)
    raw = asr_dir / raw.name
    request = asr_dir / request.name
    audio = asr_dir / audio.name
    source = Path(observation["source_media"]["path"])
    raw_document = json.loads(raw.read_text(encoding="utf-8"))
    srt = tmp_path / "source.auto.srt"
    srt.write_text(free.to_srt(raw_document, offset_ms=0), encoding="utf-8")
    projection = tmp_path / "projection"
    projection.mkdir()
    projected_xml = projection / "source.local-clock-v02.xml"
    projected_xml.write_text("<projection status='valid' />\n", encoding="utf-8")
    source_input = tmp_path / "SOURCE-INPUT-V02.json"
    source_input.write_text(
        json.dumps(
            {
                "schema_version": "c20-ordinary-source-intake-input-v02.v1",
                "status": "V02_VALID_READY_FOR_ASR",
                "derived_media": {
                    "path": str(source),
                    "sha256": _sha(source),
                    "size_bytes": source.stat().st_size,
                },
                "pts_binding_v02": "projection/SOURCE-PTS-BINDING-V02.json",
                "projected_xml": {
                    "path": str(projected_xml),
                    "sha256": _sha(projected_xml),
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    source_input_sha = _sha(source_input)
    source_pts = projection / "SOURCE-PTS-BINDING-V02.json"
    audio_range = [1400019.833, 1400020.5]
    source_pts.write_text(
        json.dumps(
            {
                "schema_version": "c20-ordinary-source-intake-pts-binding-v02.v1",
                "status": "VALID_BINDING_V02",
                "source_input_v02_sha256": source_input_sha,
                "derived_media": str(source),
                "derived_media_sha256": _sha(source),
                "derived_media_size_bytes": source.stat().st_size,
                "audio_clock": {
                    "offset_ms_used_for_xml_projection": 1400020,
                    "observed_range_ms": audio_range,
                },
                "video_offset_ms_median": 1400033.0,
                "derived_packet_probe": {
                    "rc": 0,
                    "packet_count": 1,
                    "data_hash_count": 1,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    source_pts_sha = _sha(source_pts)
    source_xml = projection / "XML-CLOCK-PROJECTION-V02.json"
    source_xml.write_text(
        json.dumps(
            {
                "schema_version": "source-intake-xml-clock-projection-v02.v1",
                "status": "VALID_XML_PROJECTION_V02",
                "xml_parse": "PASS",
                "actual_pts_receipt_sha256": source_pts_sha,
                "output_path_oci3": str(projected_xml),
                "output_sha256": _sha(projected_xml),
                "output_size_bytes": projected_xml.stat().st_size,
                "audio_parent_minus_local_ms": 1400020,
                "audio_rounding_range_ms": audio_range,
                "video_parent_minus_local_ms": 1400033,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    source_xml_sha = _sha(source_xml)
    full_av_decode = projection / "DECODE-RESULT-V02.json"
    full_av_decode.write_text(
        json.dumps(
            {
                "schema_version": "c20-ordinary-source-full-av-decode-result-v02.v1",
                "status": "DECODE_RC0_BOUND_TO_V02_MEDIA",
                "observed_rc": 0,
                "source_input_v02_sha256": source_input_sha,
                "media_sha256": _sha(source),
                "media_size_bytes": source.stat().st_size,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    full_av_decode_sha = _sha(full_av_decode)
    client_path = "/opt/bilive/autoslice/repo/scripts/free_asr_client.py"
    client_sha = _sha_bytes(b"typed client")
    argv = [
        "/opt/bilive/venv-main/bin/python",
        client_path,
        str(audio),
        "--provider",
        "auto",
        "--srt",
        str(srt),
        "--json",
        str(raw),
        "--offset-ms",
        "0",
    ]
    extraction_input = asr_dir / "EXTRACT-INPUT.json"
    extraction_result = asr_dir / "EXTRACT-RESULT.json"
    extraction_input_document = {
        "schema_version": "c20-ordinary-source-asr-audio-input-v02.v1",
        "status": "READY_TO_EXTRACT_AUDIO",
        "provider": "auto",
        "public_or_upload": False,
        "selection_or_production": False,
        "provider_dispatches_before_launch": 0,
        "offset_ms": 0,
        "source_media_path": str(source),
        "source_media_sha256": _sha(source),
        "source_media_size_bytes": source.stat().st_size,
        "output_mp3_path": str(audio),
        "extractor_module": client_path,
        "extractor_sha256": client_sha,
        "source_input_v02_sha256": source_input_sha,
        "source_pts_binding_v02_sha256": source_pts_sha,
        "source_xml_projection_v02_sha256": source_xml_sha,
        "full_av_decode_v02_sha256": full_av_decode_sha,
        "extractor_function": "scripts.free_asr_client.extract_audio_mp3",
        "extractor_signature": "(media: Path) -> bytes",
        "command": [
            "/opt/bilive/venv-main/bin/python",
            "-c",
            f"Path('{audio}').write_bytes(extract_audio_mp3(Path('{source}')))",
        ],
    }
    extraction_input.write_text(
        json.dumps(extraction_input_document, sort_keys=True) + "\n", encoding="utf-8"
    )
    extraction_result_document = {
        "schema_version": "c20-ordinary-source-asr-audio-result-v02.v1",
        "status": "REQUEST_AUDIO_SAVED",
        "source_input_v02_sha256": _sha(extraction_input),
        "output_mp3_path": str(audio),
        "output_mp3_sha256": _sha(audio),
        "output_mp3_size_bytes": audio.stat().st_size,
        "extractor_sha256": client_sha,
        "provider_dispatch_started": False,
    }
    extraction_result.write_text(
        json.dumps(extraction_result_document, sort_keys=True) + "\n", encoding="utf-8"
    )
    request_document = {
        "schema_version": "c20-ordinary-source-asr-input-v02.v1",
        "status": "READY_TO_LAUNCH",
        "provider_requested": "auto",
        "auto_chain": ["bcut", "jianying"],
        "offset_ms": 0,
        "source_media_path": str(source),
        "source_media_sha256": _sha(source),
        "request_mp3_path": str(audio),
        "request_mp3_sha256": _sha(audio),
        "request_mp3_size_bytes": audio.stat().st_size,
        "request_audio_receipt_sha256": _sha(extraction_result),
        "client_path": client_path,
        "client_sha256": client_sha,
        "source_input_v02_sha256": source_input_sha,
        "source_pts_binding_v02_sha256": source_pts_sha,
        "source_xml_projection_v02_sha256": source_xml_sha,
        "full_av_decode_v02_sha256": full_av_decode_sha,
        "argv": argv,
        "command": shlex.join(argv),
        "srt_output_path": str(srt),
        "raw_json_output_path": str(raw),
        "target_preflight": {"srt": "ABSENT", "raw_json": "ABSENT"},
        "provider_dispatches_before_launch": 0,
        "public_or_upload": False,
        "selection_or_production": False,
    }
    request.write_text(json.dumps(request_document, sort_keys=True) + "\n", encoding="utf-8")
    result = asr_dir / "ASR-RESULT.json"
    result_document = {
        "schema_version": "c20-ordinary-source-asr-result-v02.v1",
        "status": "SUCCEEDED_DRAFT_ASR",
        "provider_requested": "auto",
        "auto_chain": ["bcut", "jianying"],
        "provider_result": "bcut",
        "offset_ms": 0,
        "source_media_path": str(source),
        "source_media_sha256": _sha(source),
        "request_mp3_path": str(audio),
        "request_mp3_sha256": _sha(audio),
        "request_mp3_size_bytes": audio.stat().st_size,
        "client_path": client_path,
        "client_sha256": client_sha,
        "source_input_v02_sha256": _sha(request),
        "source_pts_binding_v02_sha256": source_pts_sha,
        "source_xml_projection_v02_sha256": source_xml_sha,
        "audio_extract_result_sha256": _sha(extraction_result),
        "source_clock": "derived local audio clock bound by exact packet SHA/PTS; offset-ms CLI=0",
        "provider_dispatches": {"bcut": 1, "jianying": 0, "kuaishou": 0},
        "bcut_task_id": "fixture-task",
        "actual_provider_evidence": {
            "raw_provider": "bcut",
            "stdout_task_created_line": "[bcut] task created: fixture-task",
            "stdout_sha256": _sha_bytes(b"fixture stdout"),
        },
        "raw_json_path": str(raw),
        "raw_json_sha256": _sha(raw),
        "srt_path": str(srt),
        "srt_sha256": _sha(srt),
        "utterance_count": len(raw_document["utterances"]),
        "first_utterance_ms": [
            raw_document["utterances"][0]["start_time"],
            raw_document["utterances"][0]["end_time"],
        ],
        "last_utterance_ms": [
            raw_document["utterances"][-1]["start_time"],
            raw_document["utterances"][-1]["end_time"],
        ],
    }
    result.write_text(json.dumps(result_document, sort_keys=True) + "\n", encoding="utf-8")
    observation.update(
        {
            "provider_requested": "auto",
            "raw_response": _binding(raw),
            "source_audio": _binding(audio),
            "source_input_v02_sha256": f"sha256:{source_input_sha}",
            "source_pts_binding_v02_sha256": f"sha256:{source_pts_sha}",
            "source_xml_projection_v02_sha256": f"sha256:{source_xml_sha}",
            "srt": {
                "path": str(srt),
                "sha256": f"sha256:{_sha(srt)}",
                "utterance_count": len(raw_document["utterances"]),
            },
        }
    )
    observation["request_receipt"] = _binding(request)
    observation["request_receipt"].update(
        {
            "result_path": str(result),
            "result_sha256": f"sha256:{_sha(result)}",
            "extract_result_sha256": f"sha256:{_sha(extraction_result)}",
        }
    )
    return observation, spec, padded, raw, request, audio, srt, result, extraction_result


def test_derives_padded_cues_from_source_offset_and_piece_start(tmp_path):
    observation, spec, padded, *_ = _fixture(tmp_path)

    loaded = load_bcut_draft_observation(
        observation,
        padded=padded,
        producer_spec=spec,
        padded_duration_ms=2000,
    )

    assert "00:00:00,200 --> 00:00:00,600" in loaded.srt_text
    assert "00:00:01,200 --> 00:00:01,700" in loaded.srt_text
    assert "before" not in loaded.srt_text
    assert "after" not in loaded.srt_text
    assert loaded.receipt["observed_source_context_ms"] == [0, 5000]
    assert loaded.receipt["source_interval_ms"] == [1000, 3000]
    assert loaded.receipt["padded_local_interval_ms"] == [0, 2000]
    assert loaded.receipt["word_timing_shift_ms"] == -1000
    receipt_path = padded.with_suffix(".bcut-draft-observation-reuse.json")
    assert receipt_path.is_file()
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["authority"] == "ASR_DRAFT_OBSERVATION_NOT_FINAL_TEXT"
    assert persisted["provider_calls"] == 0
    assert persisted["bindings"]["raw_response"]["sha256"].startswith("sha256:")


@pytest.mark.parametrize(
    "drift", [None, "container", "video", "audio", "source_tail", "start", "cue"],
)
def test_source_window_rounding_preserves_actual_stream_and_cue_limits(tmp_path, monkeypatch, drift):
    import subprocess
    from types import SimpleNamespace

    rows = None
    if drift == "cue":
        rows = [{"start_time": 1200, "end_time": 2994, "transcript": "whole cue"}]
    observation, spec, padded, *_ = _fixture(tmp_path, utterances=rows)
    output = {"format": {"duration": "1.995"}, "streams": [
        {"codec_type": "video", "start_time": "0", "duration": "1.994", "avg_frame_rate": "60/1"},
        {"codec_type": "audio", "codec_name": "aac", "start_time": "0", "duration": "1.980"},
    ]}
    source = {"streams": [
        {"codec_type": "video", "start_time": "0", "duration": "3.000"},
        {"codec_type": "audio", "start_time": "0", "duration": "2.981"},
    ]}
    if drift == "container":
        output["format"]["duration"] = "1.996"
    elif drift in {"video", "audio"}:
        output["streams"][0 if drift == "video" else 1]["duration"] = "1.800"
    elif drift == "source_tail":
        source["streams"][1]["duration"] = "2.800"
    elif drift == "start":
        output["streams"][0]["start_time"] = "0.030"

    def probe(argv, **kwargs):
        if "-read_intervals" in argv:
            document = {"packets": [{"duration_time": "0.021333"}]}
        else:
            document = output if argv[-1] == str(padded) else source
        return SimpleNamespace(returncode=0, stdout=json.dumps(document))

    monkeypatch.setattr(subprocess, "run", probe)
    if drift is not None:
        with pytest.raises(BcutDraftObservationError, match="actual padded stream coverage"):
            load_bcut_draft_observation(
                observation, padded=padded, producer_spec=spec, padded_duration_ms=1995,
            )
        assert not padded.with_suffix(".bcut-draft-observation-reuse.json").exists()
    else:
        loaded = load_bcut_draft_observation(
            observation, padded=padded, producer_spec=spec, padded_duration_ms=1995,
        )
        assert loaded.receipt["source_interval_ms"] == [1000, 3000]
        assert loaded.receipt["padded_local_interval_ms"] == [0, 1995]
        assert loaded.receipt["container_clock"]["required_cue_end_ms"] == 1700
        assert "00:00:01,200 --> 00:00:01,700" in loaded.srt_text


def test_different_real_30fps_cut_reuses_whole_cues_at_measured_duration(tmp_path):
    import shutil
    import subprocess

    from src.autoslice.producer_source_media import prepare_source_media

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("requires real ffmpeg and ffprobe")
    observation, spec, _, _, request, _ = _fixture(tmp_path)
    source = Path(observation["source_media"]["path"])
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=size=64x64:rate=30:duration=4", "-f", "lavfi", "-i",
         "sine=sample_rate=48000:duration=4", "-c:v", "libx264", "-c:a", "aac", str(source)],
        check=True, capture_output=True, timeout=30,
    )
    observation["source_media"] = _binding(source)
    observation["source_interval_ms"] = [1000, 3011]
    spec["pieces"][0].update(end_ms=3011, source_media_sha256="sha256:" + _sha(source))
    _rewrite_request(observation, request, source_sha256=_sha(source))
    (tmp_path / "ordinary").mkdir()
    prepared = prepare_source_media(
        spec=spec, cid=spec["id"], out_root=tmp_path / "ordinary", host="localhost",
    )
    assert prepared.padded_duration_ms != 2011
    loaded = load_bcut_draft_observation(
        observation, padded=prepared.padded, producer_spec=spec,
        padded_duration_ms=prepared.padded_duration_ms,
    )
    assert loaded.receipt["source_interval_ms"] == [1000, 3011]
    assert loaded.receipt["padded_local_interval_ms"] == [0, prepared.padded_duration_ms]
    assert loaded.receipt["selected_cue_count"] == 2
    assert "00:00:01,200 --> 00:00:01,700" in loaded.srt_text


def test_actual_argv_receipt_derives_bcut_input_and_raw_output(tmp_path):
    observation, spec, padded, *_ = _fixture(tmp_path)

    loaded = load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)

    assert loaded.receipt["raw_response_path"] == observation["raw_response"]["path"]
    assert loaded.receipt["source_audio_path"] == observation["source_audio"]["path"]


def test_c20_request_receipt_compatibility_verifies_typed_companion_chain(tmp_path):
    (
        observation,
        spec,
        padded,
        _raw,
        request,
        audio,
        srt,
        result,
        extraction_result,
    ) = _c20_fixture(tmp_path)
    source = Path(observation["source_media"]["path"])
    original = {
        path: path.read_bytes()
        for path in (source, _raw, request, audio, srt, result, extraction_result)
    }

    loaded = load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)

    compatibility = loaded.receipt["request_receipt_compatibility"]
    assert compatibility["schema_version"] == "c20-ordinary-source-asr-input-v02.v1"
    assert compatibility["request_result_path"] == str(result)
    assert compatibility["source_srt_sha256"].startswith("sha256:")
    assert compatibility["request_audio_result"]["sha256"] == f"sha256:{_sha(extraction_result)}"
    companions = compatibility["source_companions"]
    assert companions["source_input"]["sha256"].startswith("sha256:")
    assert companions["source_pts_binding"]["audio_offset_ms"] == 1400020
    assert companions["source_xml_projection"]["output_path"].endswith(
        "projection/source.local-clock-v02.xml"
    )
    assert companions["full_av_decode"]["observed_rc"] == 0
    assert loaded.receipt["provider_calls"] == 0
    assert loaded.receipt["observed_raw_utterance_count"] == 4
    assert all(path.read_bytes() == payload for path, payload in original.items())


def test_c20_source_companion_bytes_reject_tampered_xml(tmp_path):
    observation, spec, padded, *_ = _c20_fixture(tmp_path)
    projected_xml = tmp_path / "projection" / "source.local-clock-v02.xml"
    projected_xml.write_text("<tampered />\n", encoding="utf-8")

    with pytest.raises(BcutDraftObservationError, match="c20 projected XML sha256 drift"):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


def test_c20_source_companions_reject_missing_pts_evidence(tmp_path):
    observation, spec, padded, *_ = _c20_fixture(tmp_path)
    (tmp_path / "projection" / "SOURCE-PTS-BINDING-V02.json").unlink()

    with pytest.raises(BcutDraftObservationError, match="c20 source PTS binding cannot be stat'ed"):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


def test_c20_source_companions_reject_mismatched_source_document(tmp_path):
    observation, spec, padded, *_ = _c20_fixture(tmp_path)
    source_input = tmp_path / "SOURCE-INPUT-V02.json"
    document = json.loads(source_input.read_text(encoding="utf-8"))
    document["derived_media"]["path"] = str(tmp_path / "wrong-source.mp4")
    source_input.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    new_source_input_sha = _sha(source_input)
    request = Path(observation["request_receipt"]["path"])
    request_document = json.loads(request.read_text(encoding="utf-8"))
    request_document["source_input_v02_sha256"] = new_source_input_sha
    request.write_text(json.dumps(request_document, sort_keys=True) + "\n", encoding="utf-8")
    observation["source_input_v02_sha256"] = f"sha256:{new_source_input_sha}"
    request_binding = _binding(request)
    request_binding.update(
        {
            "result_path": observation["request_receipt"]["result_path"],
            "result_sha256": observation["request_receipt"]["result_sha256"],
            "extract_result_sha256": observation["request_receipt"]["extract_result_sha256"],
        }
    )
    observation["request_receipt"] = request_binding
    with pytest.raises(BcutDraftObservationError, match="c20 source input does not bind"):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


def test_c20_source_companions_reject_mismatched_clock_projection(tmp_path):
    observation, spec, padded, *_ = _c20_fixture(tmp_path)
    source_pts = tmp_path / "projection" / "SOURCE-PTS-BINDING-V02.json"
    document = json.loads(source_pts.read_text(encoding="utf-8"))
    document["audio_clock"]["offset_ms_used_for_xml_projection"] = 1400021
    source_pts.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    new_source_pts_sha = _sha(source_pts)
    request = Path(observation["request_receipt"]["path"])
    request_document = json.loads(request.read_text(encoding="utf-8"))
    request_document["source_pts_binding_v02_sha256"] = new_source_pts_sha
    request.write_text(json.dumps(request_document, sort_keys=True) + "\n", encoding="utf-8")
    observation["source_pts_binding_v02_sha256"] = f"sha256:{new_source_pts_sha}"
    request_binding = _binding(request)
    request_binding.update(
        {
            "result_path": observation["request_receipt"]["result_path"],
            "result_sha256": observation["request_receipt"]["result_sha256"],
            "extract_result_sha256": observation["request_receipt"]["extract_result_sha256"],
        }
    )
    observation["request_receipt"] = request_binding
    with pytest.raises(
        BcutDraftObservationError,
        match="c20 XML clock projection is not bound",
    ):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("request_mp3_sha256", "MP3 binding drift"),
        ("request_offset", "pre-dispatch fields drift"),
        ("request_argv", "c20 auto invocation"),
        ("result_sha256", "c20 ASR result"),
        ("result_raw_sha256", "ASR result source/request binding drift"),
        ("srt_sha256", "c20 ASR result SRT binding drift"),
        ("provider_task_id", "c20 ASR result provider evidence is missing"),
        ("full_av_decode_sha256", "c20 full AV decode result sha256 drift"),
        ("extract_result_sha256", "extraction receipt hash drift"),
    ],
)
def test_c20_request_receipt_compatibility_rejects_tampered_chain(tmp_path, mutation, message):
    (
        observation,
        spec,
        padded,
        _raw,
        request,
        _audio,
        _srt,
        result,
        extraction_result,
    ) = _c20_fixture(tmp_path)
    request_document = json.loads(request.read_text(encoding="utf-8"))
    result_document = json.loads(result.read_text(encoding="utf-8"))
    if mutation == "request_mp3_sha256":
        request_document["request_mp3_sha256"] = _sha_bytes(b"wrong mp3")
    elif mutation == "request_offset":
        request_document["offset_ms"] = 1
    elif mutation == "request_argv":
        request_document["argv"][9:] = ["--offset-ms", "1"]
    elif mutation == "result_sha256":
        observation["request_receipt"]["result_sha256"] = f"sha256:{_sha_bytes(b'wrong result')}"
    elif mutation == "result_raw_sha256":
        result_document["raw_json_sha256"] = _sha_bytes(b"wrong raw")
        result.write_text(json.dumps(result_document, sort_keys=True) + "\n", encoding="utf-8")
        observation["request_receipt"]["result_sha256"] = f"sha256:{_sha(result)}"
    elif mutation == "srt_sha256":
        observation["srt"]["sha256"] = f"sha256:{_sha_bytes(b'wrong srt')}"
    elif mutation == "provider_task_id":
        result_document["bcut_task_id"] = "wrong-task"
        result.write_text(json.dumps(result_document, sort_keys=True) + "\n", encoding="utf-8")
        observation["request_receipt"]["result_sha256"] = f"sha256:{_sha(result)}"
    elif mutation == "full_av_decode_sha256":
        request_document["full_av_decode_v02_sha256"] = _sha_bytes(b"wrong full av decode")
    elif mutation == "extract_result_sha256":
        observation["request_receipt"]["extract_result_sha256"] = f"sha256:{_sha_bytes(b'wrong extract')}"
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(mutation)
    if mutation in {
        "request_mp3_sha256",
        "request_offset",
        "request_argv",
        "full_av_decode_sha256",
    }:
        request.write_text(json.dumps(request_document, sort_keys=True) + "\n", encoding="utf-8")
        observation["request_receipt"] = _binding(request)
        observation["request_receipt"].update(
            {
                "result_path": str(result),
                "result_sha256": f"sha256:{_sha(result)}",
                "extract_result_sha256": f"sha256:{_sha(extraction_result)}",
            }
        )

    with pytest.raises(BcutDraftObservationError, match=message):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)
    assert not padded.with_suffix(".bcut-draft-observation-reuse.json").exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("source_offset_ms", "c20 request receipt pre-dispatch fields drift"),
        ("source_context_ms", "c20 ASR result cue clock is outside the observed context"),
        ("source_interval_ms", "raw utterance 2 crosses the producer piece boundary"),
    ],
)
def test_c20_request_receipt_compatibility_rejects_wrong_window_or_offset(
    tmp_path, change, message
):
    observation, spec, padded, *_ = _c20_fixture(tmp_path)
    if change == "source_offset_ms":
        observation[change] = 1
    elif change == "source_context_ms":
        observation[change] = [0, 3000]
    else:
        observation[change] = [1000, 2500]

    with pytest.raises(BcutDraftObservationError, match=message):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)
    assert not padded.with_suffix(".bcut-draft-observation-reuse.json").exists()


def _finite_local_request(observation, request):
    document = json.loads(request.read_text())
    document.pop("source_context_ms")
    document.update(
        srt_timestamp_domain="finite_local_ms",
        source_context_global_ms=[3480000, 3485000],
        source_offset_ms=3480000,
        global_mapping="source_ms = finite_local_ms + source_offset_ms",
        local_srt_timestamps_are_offset_by_adapter=False,
    )
    document["argv"].extend(["--offset-ms", "0"])
    request.write_text(json.dumps(document) + "\n")
    observation["request_receipt"] = _binding(request)


def test_original_finite_local_receipt_projects_clock_without_rewriting(tmp_path):
    observation, spec, padded, _, request, _ = _fixture(tmp_path)
    _finite_local_request(observation, request)
    original = request.read_bytes()

    loaded = load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)

    assert request.read_bytes() == original
    assert "00:00:00,200 --> 00:00:00,600" in loaded.srt_text
    assert "00:00:01,200 --> 00:00:01,700" in loaded.srt_text
    assert loaded.receipt["request_receipt_sha256"] == observation["request_receipt"]["sha256"]
    assert loaded.receipt["request_clock_projection"] == {
        "srt_timestamp_domain": "finite_local_ms",
        "source_context_global_ms": [3480000, 3485000],
        "global_source_origin_ms": 3480000,
        "consumer_source_context_ms": [0, 5000],
        "consumer_source_offset_ms": 0,
        "authority": "COORDINATE_PROJECTION_ONLY",
    }
    assert loaded.receipt["authority"] == "ASR_DRAFT_OBSERVATION_NOT_FINAL_TEXT"
    assert loaded.receipt["provider_calls"] == 0


@pytest.mark.parametrize("changes", [
    {"srt_timestamp_domain": "global_ms"},
    {"source_context_global_ms": [3480000, 3486000]},
    {"source_offset_ms": 3480001},
    {"global_offset_ms": 3480001},
    {"source_context_ms": [3480000, 3485000]},
    {"global_mapping": "guessed offset"},
    {"local_srt_timestamps_are_offset_by_adapter": True},
])
def test_finite_local_projection_rejects_missing_or_conflicting_clock(tmp_path, changes):
    observation, spec, padded, _, request, _ = _fixture(tmp_path)
    _finite_local_request(observation, request)
    _rewrite_request(observation, request, **changes)

    with pytest.raises(BcutDraftObservationError):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)
    assert not padded.with_suffix(".bcut-draft-observation-reuse.json").exists()


@pytest.mark.parametrize("offset", [None, "1", "3480000"])
def test_finite_local_projection_requires_actual_zero_offset_invocation(tmp_path, offset):
    observation, spec, padded, _, request, _ = _fixture(tmp_path)
    _finite_local_request(observation, request)
    document = json.loads(request.read_text())
    document["argv"] = document["argv"][:-2]
    if offset is not None:
        document["argv"].extend(["--offset-ms", offset])
    _rewrite_request(observation, request, argv=document["argv"])

    with pytest.raises(BcutDraftObservationError, match="offset"):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


def test_finite_local_projection_rejects_global_consumer_clock(tmp_path):
    observation, spec, padded, _, request, _ = _fixture(tmp_path)
    _finite_local_request(observation, request)
    observation["source_offset_ms"] = 3480000
    observation["source_context_ms"] = [3480000, 3485000]
    observation["source_interval_ms"] = [3481000, 3483000]

    with pytest.raises(BcutDraftObservationError, match="clock projection drift"):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"provider": "jianying"}, "explicit provider"),
        ({"audio_path": "/tmp/wrong.mp3"}, "explicit audio_path"),
        ({"raw_output": "/tmp/wrong.json"}, "explicit raw_output"),
    ],
)
def test_explicit_receipt_fields_cannot_conflict_with_argv(tmp_path, changes, message):
    observation, spec, padded, _, request, _ = _fixture(tmp_path)
    _rewrite_request(observation, request, **changes)

    with pytest.raises(BcutDraftObservationError, match=message):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


def test_dual_argv_and_command_must_agree(tmp_path):
    observation, spec, padded, _, request, audio = _fixture(tmp_path)
    document = json.loads(request.read_text(encoding="utf-8"))
    document["command"] = list(document["argv"])
    document["command"][document["command"].index("--json") + 1] = str(tmp_path / "other.json")
    _rewrite_request(observation, request, **{"command": document["command"]})

    with pytest.raises(BcutDraftObservationError, match="argv and command conflict"):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


@pytest.mark.parametrize(
    ("argv_change", "message"),
    [
        (lambda argv: argv.__setitem__(argv.index("bcut"), "jianying"), "provider command"),
        (lambda argv: argv.__setitem__(3, "/tmp/wrong-input.mp3"), "input audio path drift"),
        (
            lambda argv: argv.__setitem__(argv.index("--json") + 1, "/tmp/wrong-output.json"),
            "raw output path drift",
        ),
    ],
)
def test_argv_provider_input_and_raw_output_are_strictly_bound(
    tmp_path, argv_change, message
):
    observation, spec, padded, _, request, _ = _fixture(tmp_path)
    document = json.loads(request.read_text(encoding="utf-8"))
    argv = list(document["argv"])
    argv_change(argv)
    _rewrite_request(observation, request, **{"argv": argv})

    with pytest.raises(BcutDraftObservationError, match=message):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


def test_candidate_id_must_match_producer_id(tmp_path):
    observation, spec, padded, *_ = _fixture(tmp_path)
    observation["candidate_id"] = "different-candidate"

    with pytest.raises(BcutDraftObservationError, match="candidate_id"):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


def test_crossing_raw_cue_is_rejected_without_truncation(tmp_path):
    observation, spec, padded, *_ = _fixture(
        tmp_path,
        utterances=[
            {
                "start_time": 900,
                "end_time": 1200,
                "transcript": "crossing",
                "words": [],
            }
        ],
    )

    with pytest.raises(BcutDraftObservationError, match="crosses"):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


def test_source_hash_drift_is_rejected(tmp_path):
    observation, spec, padded, *_ = _fixture(tmp_path)
    source = Path(observation["source_media"]["path"])
    source.write_bytes(b"tampered source bytes")

    with pytest.raises(BcutDraftObservationError, match="drift"):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)


def test_normal_cpa_uses_observation_without_bcut_call(tmp_path, monkeypatch):
    observation, spec, padded, *_ = _fixture(tmp_path)
    spec["bcut_draft_observation"] = observation
    monkeypatch.setenv("AUTOSLICE_DISABLE_TOPIC_ENTITY_GRAPH", "1")
    monkeypatch.setattr(free, "extract_audio_mp3", lambda _: b"runtime padded audio")
    asr_calls = []
    monkeypatch.setattr(
        free,
        "transcribe",
        lambda *args, **kwargs: asr_calls.append((args, kwargs)) or pytest.fail("BCUT must not run"),
    )
    cpa_calls = []

    def cpa(prompt):
        cpa_calls.append(prompt)
        return json.dumps(
            {
                "cues": [
                    {"n": 1, "text": "inside"},
                    {"n": 2, "text": "tail"},
                ]
            }
        )

    cpa.cpa_cache_identity = {"models": ["test-model"], "effort": "low"}
    monkeypatch.setattr(transcription, "_build_full_session_cpa_llm_call", lambda **_: cpa)
    monkeypatch.setattr(transcription, "_cpa_pronoun_ta_pass", lambda text, **_: text)

    transcriber = transcription._build_aggregate_asr_transcriber(
        "localhost",
        correct="cpa",
        producer_spec=spec,
        bcut_draft_observation=observation,
        duration_ms=2000,
    )
    output = transcriber(padded)

    assert asr_calls == []
    assert cpa_calls
    assert "inside" in output and "tail" in output
    source = json.loads(padded.with_suffix(".asr-source.json").read_text(encoding="utf-8"))
    assert source["observation_reused"] is True
    assert source["provider"] == "bcut"
    assert source["raw_result_sha256"].startswith("sha256:")
    assert padded.with_suffix(".cpa-reviewed.srt").is_file()


def test_invalid_explicit_observation_never_falls_back_to_asr(tmp_path, monkeypatch):
    observation, spec, padded, *_ = _fixture(tmp_path)
    spec["bcut_draft_observation"] = observation
    source = Path(observation["source_media"]["path"])
    source.write_bytes(b"tampered")
    monkeypatch.setattr(
        free,
        "extract_audio_mp3",
        lambda _: pytest.fail("audio extraction must not run"),
    )
    monkeypatch.setattr(
        free,
        "transcribe",
        lambda *_a, **_k: pytest.fail("ASR fallback must not run"),
    )
    with pytest.raises(BcutDraftObservationError):
        transcription._build_aggregate_asr_transcriber(
            "localhost",
            correct="cpa",
            producer_spec=spec,
            bcut_draft_observation=observation,
            duration_ms=2000,
        )(padded)


def test_default_route_still_calls_auto_asr(tmp_path, monkeypatch):
    media = tmp_path / "ordinary.mp4"
    media.write_bytes(b"ordinary media")
    monkeypatch.setenv("AUTOSLICE_DISABLE_TOPIC_ENTITY_GRAPH", "1")
    monkeypatch.setattr(free, "extract_audio_mp3", lambda _: b"ordinary audio")
    calls = []
    monkeypatch.setattr(
        free,
        "transcribe",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or {
            "provider": "bcut",
            "utterances": [
                {"start_time": 0, "end_time": 1000, "transcript": "ordinary", "words": []}
            ],
        },
    )

    def cpa(_prompt):
        return '{"cues":[{"n":1,"text":"ordinary"}]}'

    cpa.cpa_cache_identity = {"models": ["test-model"], "effort": "low"}
    monkeypatch.setattr(transcription, "_build_full_session_cpa_llm_call", lambda **_: cpa)
    monkeypatch.setattr(transcription, "_cpa_pronoun_ta_pass", lambda text, **_: text)

    run = transcription._build_aggregate_asr_transcriber("localhost", correct="cpa")
    assert "ordinary" in run(media)
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", ["interpreter", "boolean_dispatch"])
def test_c20_rejects_conflicting_invocation_or_non_integer_dispatch(tmp_path, mutation):
    observation, spec, padded, _, request, _, _, result, _ = _c20_fixture(tmp_path)
    if mutation == "interpreter":
        document = json.loads(request.read_bytes())
        command = shlex.split(document["command"])
        command[0] = "/different/python3"
        document["command"] = shlex.join(command)
        request.write_text(json.dumps(document))
        observation["request_receipt"].update(_binding(request))
        message = "argv and command conflict at interpreter_path"
    else:
        document = json.loads(result.read_bytes())
        document["provider_dispatches"]["jianying"] = False
        result.write_text(json.dumps(document))
        observation["request_receipt"]["result_sha256"] = "sha256:" + _sha(result)
        message = "provider dispatch evidence drift"
    with pytest.raises(BcutDraftObservationError, match=message):
        load_bcut_draft_observation(observation, padded=padded, producer_spec=spec)
