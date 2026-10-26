import hashlib
import json
import shutil
import subprocess

import pytest

from src.autoslice import producer_source_media
from src.autoslice.redelivery_boundary_projection import (
    AUTHORITY_CONFIG_KEY,
    PROJECTION_MODE,
    PROJECTION_MODE_CONFIG_KEY,
)
from src.autoslice.source_subtitle_truth import apply_source_subtitle_truth


def _clock_row(source, start, end, digest):
    return {"source_path": source, "start_ms": start, "end_ms": end,
            "source_sha256": digest * 64, "source_media_binding": "sha256:" + digest * 64,
            "output_sha256": "c" * 64}


def test_native_multi_piece_concat_preserves_decoded_audio_and_video_clock(monkeypatch, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("native codec regression requires ffmpeg and ffprobe")
    np = pytest.importorskip("numpy")
    times = np.arange(3 * 48000) / 48000
    wave = (0.3 * np.sin(2 * np.pi * (320 * times + 240 * times ** 2))).astype("<f4")
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=30:d=3",
        "-f", "f32le", "-ar", "48000", "-ac", "1", "-i", "pipe:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-t", "3", str(source),
    ], input=wave.tobytes(), check=True, capture_output=True)
    spec = {"pieces": [
        {"remote_media": str(source), "start_ms": 0, "end_ms": 1000},
        {"remote_media": str(source), "start_ms": 2000, "end_ms": 3000},
    ]}
    output = tmp_path / "output"
    output.mkdir()
    legacy = output / "padded_0_3000.mp4"
    legacy.write_bytes(b"historical concat bytes")
    legacy_provenance = legacy.with_suffix(".provenance.json")
    legacy_provenance.write_bytes(b'{"historical":true}')
    prepared = producer_source_media.prepare_source_media(
        spec=spec, cid="codec-clock", out_root=output, host="localhost",
    )
    assert prepared.padded.name == "padded_0_3000.decoded-audio-v1.mp4"
    provenance = json.loads(prepared.padded_provenance_path.read_text())
    assert provenance["materialization"] == producer_source_media.MULTI_PIECE_MATERIALIZATION
    assert provenance["piece_durations_ms"] == prepared.durations
    assert legacy.read_bytes() == b"historical concat bytes"
    assert legacy_provenance.read_bytes() == b'{"historical":true}'
    streams = json.loads(subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time,duration",
        "-of", "json", str(prepared.padded),
    ], check=True, capture_output=True, text=True).stdout)["streams"]
    assert {stream["codec_type"] for stream in streams} == {"audio", "video"}
    assert all(float(stream["start_time"]) == 0 for stream in streams)

    def pcm(path, start_ms):
        raw = subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(path), "-ss", str(start_ms / 1000),
            "-t", "0.6", "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1",
        ], check=True, capture_output=True).stdout
        return np.frombuffer(raw, dtype="<f4").astype(float)

    def audio_lag(actual, reference):
        correlations = []
        for lag in range(-240, 241):
            left = actual[max(0, lag):min(len(actual), len(reference) + lag)]
            right = reference[max(0, -lag):min(len(reference), len(actual) - lag)]
            correlations.append((float(np.dot(left, right) / (np.linalg.norm(left) * np.linalg.norm(right))), lag))
        correlation, lag = max(correlations)
        return correlation, lag / 8

    old_copy = output / "old-stream-copy-counterexample.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i",
        str(output / "concat.txt"), "-c", "copy", str(old_copy),
    ], check=True, capture_output=True)
    for index, recording_ms in enumerate((200, 2200)):
        local_ms = sum(prepared.durations[:index]) + 200
        reference = pcm(source, recording_ms)
        correlation, lag = audio_lag(pcm(prepared.padded, local_ms), reference)
        assert correlation > 0.98
        assert abs(lag) <= 1
        old_correlation, old_lag = audio_lag(pcm(old_copy, local_ms), reference)
        assert old_correlation > 0.9
        assert abs(old_lag) > 10

    original_hash = hashlib.sha256(prepared.padded.read_bytes()).hexdigest()
    monkeypatch.setattr(producer_source_media, "run", lambda *_args, **_kwargs: pytest.fail("warm cache must not materialize again"))
    resumed = producer_source_media.prepare_source_media(
        spec=spec, cid="codec-clock", out_root=output, host="localhost",
    )
    assert hashlib.sha256(resumed.padded.read_bytes()).hexdigest() == original_hash
    retained_spec = {**spec, "retained_current_text": {
        "padded_sha256": "sha256:" + original_hash,
        "parent_materialization_path": str(prepared.padded),
        "parent_materialization_sha256": "sha256:" + original_hash,
        "parent_materialization_duration_ms": prepared.padded_duration_ms,
        "parent_materialization_provenance_path": str(prepared.padded_provenance_path),
        "parent_materialization_provenance_sha256": "sha256:" + hashlib.sha256(prepared.padded_provenance_path.read_bytes()).hexdigest(),
    }}
    retained = producer_source_media.prepare_source_media(
        spec=retained_spec, cid="codec-clock", out_root=tmp_path / "retained-output", host="localhost",
    )
    assert retained.padded == prepared.padded
    assert retained_spec["source_piece_clock"] == spec["source_piece_clock"]
    sidecars = list((tmp_path / "retained-output").iterdir())
    assert len(sidecars) == 2
    assert all(path.read_bytes() == (output / path.name).read_bytes() for path in sidecars)


def test_delivery_projection_resolves_each_joined_recording_without_global_offset():
    from src.autoslice import producer_text_pipeline as pipeline

    clock = producer_source_media.source_piece_clock([
        _clock_row("/source/a.flv", 10000, 12000, "a"),
        _clock_row("/source/b.flv", 20000, 23000, "b"),
    ], [2000, 3000], padded_sha256="d" * 64)
    projection = {"rows": [{"delivery_start_ms": 250, "delivery_end_ms": 1250,
                             "source_start_ms": 1500, "source_end_ms": 2500}]}
    pipeline._bind_projection_recording_clock(projection, {"source_piece_clock": clock}, "d" * 64)
    spans = projection["rows"][0]["recording_spans"]
    assert [(s["recording_basename"], s["recording_start_ms"], s["recording_end_ms"]) for s in spans] == [
        ("a.flv", 11500, 12000), ("b.flv", 20000, 20500)]
    assert projection["rows"][0]["delivery_start_ms"] == 250
    assert projection["root_time_domain"] == "recording_absolute_ms"
    with pytest.raises(pipeline.CurrentTextContinuationError, match="BINDING_INVALID"):
        pipeline._bind_projection_recording_clock(projection, {"source_piece_clock": clock}, "e" * 64)


def test_measured_mux_tail_never_claims_extra_original_recording_content():
    from src.autoslice import producer_text_pipeline as pipeline

    clock = producer_source_media.source_piece_clock([
        _clock_row("/source/a.flv", 10000, 12000, "a"),
        _clock_row("/source/b.flv", 20000, 23000, "b"),
    ], [2014, 3000], padded_sha256="d" * 64)
    assert clock["pieces"][1]["padded_start_ms"] == 2014
    unmapped = {"rows": [{"source_start_ms": 1990, "source_end_ms": 2020}]}
    with pytest.raises(pipeline.CurrentTextContinuationError, match="UNMAPPED_MUX_TAIL"):
        pipeline._bind_projection_recording_clock(unmapped, {"source_piece_clock": clock}, "d" * 64)
    mapped = {"rows": [{"source_start_ms": 2014, "source_end_ms": 2020}]}
    pipeline._bind_projection_recording_clock(mapped, {"source_piece_clock": clock}, "d" * 64)
    assert mapped["rows"][0]["recording_spans"][0]["recording_start_ms"] == 20000


def _srt_ms(start_ms: int, end_ms: int, text: str) -> str:
    def timestamp(value: int) -> str:
        seconds, millis = divmod(value, 1_000)
        return f"00:00:{seconds:02d},{millis:03d}"

    return (
        "1\n"
        f"{timestamp(start_ms)} --> {timestamp(end_ms)}\n"
        f"{text}\n"
    )


def _install_cached_media(monkeypatch, tmp_path, *, source_sha256: str):
    out_root = tmp_path / "out"
    out_root.mkdir()
    piece_path = out_root / "piece_0_1000_5000.mp4"
    piece_path.write_bytes(b"piece")
    piece_path.with_suffix(".provenance.json").write_text(
        json.dumps({"source_sha256": source_sha256,
                    "source_media_binding": "sha256:" + source_sha256,
                    "source_path": "/recordings/official-replay.mp4",
                    "start_ms": 1000, "end_ms": 5000,
                    "output_sha256": hashlib.sha256(piece_path.read_bytes()).hexdigest()}),
        encoding="utf-8",
    )
    padded_path = out_root / "padded_1000_5000.mp4"
    padded_path.write_bytes(b"padded")
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda _host, source: (str(source), source_sha256),
    )
    monkeypatch.setattr(
        producer_source_media,
        "_valid_cached_provenance",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        producer_source_media,
        "ffprobe_duration_ms",
        lambda _path: 4_000,
    )
    return out_root


def test_prepare_source_media_binds_runner_piece_for_subtitle_truth_alias(
    monkeypatch, tmp_path
):
    source_sha256 = "a" * 64
    out_root = _install_cached_media(
        monkeypatch,
        tmp_path,
        source_sha256=source_sha256,
    )
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/official-replay.mp4",
                "start_ms": 1_000,
                "end_ms": 5_000,
            }
        ]
    }

    prepared = producer_source_media.prepare_source_media(
        spec=spec,
        cid="candidate",
        out_root=out_root,
        host="free",
    )

    binding = "sha256:" + source_sha256
    assert spec["pieces"][0]["source_media_sha256"] == binding
    assert prepared.piece_provenance_rows[0]["source_sha256"] == source_sha256

    ledger_path = tmp_path / "truth.json"
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "source_aliases": [
                    {
                        "alias_id": "official-complete-replay",
                        "alias_recording_basename": "official-replay.mp4",
                        "alias_source_sha256": binding,
                        "canonical_recording_basename": "canonical.mp4",
                        "alias_timeline_offset_ms": 0,
                        "authority": "exact source bytes plus reviewed alignment",
                    }
                ],
                "entries": [
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "runner-shape-regression",
                        "recording_basename": "canonical.mp4",
                        "source_start_ms": 2_000,
                        "source_end_ms": 3_000,
                        "action": "replace_cue",
                        "text": "审定文本",
                        "required": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(1_000, 2_000, "误听文本"),
        spec=spec,
        durations=prepared.durations,
        ledger_path=ledger_path,
    )

    assert "审定文本" in corrected
    assert audit["status"] == "APPLIED"


def test_prepare_source_media_injects_runtime_terminal_projection_authority(
    monkeypatch, tmp_path
):
    source_sha256 = "a" * 64
    out_root = _install_cached_media(
        monkeypatch,
        tmp_path,
        source_sha256=source_sha256,
    )
    (out_root / "piece_0_1000_5000.provenance.json").write_text(
        json.dumps(
            {
                "source_path": "/recordings/official-replay.mp4",
                "source_sha256": source_sha256,
                "source_media_binding": "sha256:" + source_sha256,
                "start_ms": 1000, "end_ms": 5000,
                "output_sha256": hashlib.sha256(b"piece").hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    baseline = tmp_path / "reviewed.srt"
    baseline.write_text(
        _srt_ms(0, 2_000, "审定收尾"),
        encoding="utf-8",
    )
    config = {
        "schema_version": "subtitle-redelivery-baseline.v2",
        "exact_interval_replay": True,
        PROJECTION_MODE_CONFIG_KEY: PROJECTION_MODE,
        "path": str(baseline),
        "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "authority": "synthetic reviewed delivery",
        "source_recording_basename": "official-replay.mp4",
        "source_sha256": source_sha256,
        "absolute_source_start_ms": 2_000,
        "absolute_source_end_ms": 4_000,
    }
    spec = {
        "candidate_id": "candidate-projection",
        "pieces": [
            {
                "remote_media": "/recordings/official-replay.mp4",
                "start_ms": 1_000,
                "end_ms": 5_000,
            }
        ],
        "subtitle_redelivery_baseline": config,
    }

    producer_source_media.prepare_source_media(
        spec=spec,
        cid="candidate-projection",
        out_root=out_root,
        host="free",
        spec_parent=tmp_path,
    )

    authority = config[AUTHORITY_CONFIG_KEY]
    assert authority["status"] == "BOUND"
    assert authority["source_sha256"] == "sha256:" + source_sha256
    assert authority["local_source_start_ms"] == 1_000
    assert authority["local_source_end_ms"] == 3_000


def test_prepare_source_media_rejects_declared_hash_before_using_cache(
    monkeypatch, tmp_path
):
    source_sha256 = "a" * 64
    out_root = _install_cached_media(
        monkeypatch,
        tmp_path,
        source_sha256=source_sha256,
    )
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/official-replay.mp4",
                "source_media_sha256": "sha256:" + "b" * 64,
                "start_ms": 1_000,
                "end_ms": 5_000,
            }
        ]
    }

    with pytest.raises(RuntimeError, match="SOURCE_MEDIA_DECLARED_SHA256_MISMATCH"):
        producer_source_media.prepare_source_media(
            spec=spec,
            cid="candidate",
            out_root=out_root,
            host="free",
        )

    assert spec["pieces"][0]["source_media_sha256"] == "sha256:" + "b" * 64


def _install_exact_piece_provenance(
    tmp_path,
    *,
    source_sha256: str,
    source_path: str = "/recordings/official-replay.mp4",
):
    out_root = tmp_path / "out"
    out_root.mkdir()
    local = out_root / "piece_0_1000_5000.mp4"
    local.write_bytes(b"exact cached piece")
    provenance = {
        "source_path": source_path,
        "source_sha256": source_sha256,
        "source_media_binding": "sha256:" + source_sha256,
        "start_ms": 1_000,
        "end_ms": 5_000,
        "output_path": str(local.resolve()),
        "output_sha256": hashlib.sha256(local.read_bytes()).hexdigest(),
    }
    local.with_suffix(".provenance.json").write_text(
        json.dumps(provenance),
        encoding="utf-8",
    )
    return out_root, local, source_path


def test_prepare_source_media_reuses_exact_cache_without_rehashing_present_source(
    monkeypatch, tmp_path
):
    source = tmp_path / "recordings" / "official-replay.mp4"
    source.parent.mkdir()
    source.write_bytes(b"immutable source")
    out_root, local, source_path = _install_exact_piece_provenance(
        tmp_path,
        source_sha256="a" * 64,
        source_path=str(source),
    )
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda *_args: pytest.fail("must not rehash a hash-bound cached source"),
    )
    monkeypatch.setattr(
        producer_source_media,
        "ffprobe_duration_ms",
        lambda _path: 4_000,
    )
    spec = {
        "pieces": [
            {
                "remote_media": source_path,
                "start_ms": 1_000,
                "end_ms": 5_000,
            }
        ]
    }

    prepared = producer_source_media.prepare_source_media(
        spec=spec,
        cid="candidate",
        out_root=out_root,
        host="localhost",
    )

    assert local.is_file()
    assert prepared.piece_provenance_rows[0]["source_revalidation_status"] == (
        "HASH_BOUND_CACHE_REUSED_SOURCE_PATH_PRESENT"
    )


def test_prepare_source_media_reuses_exact_hash_bound_piece_when_source_root_unavailable(
    monkeypatch, tmp_path
):
    source_sha256 = "a" * 64
    out_root, local, source_path = _install_exact_piece_provenance(
        tmp_path,
        source_sha256=source_sha256,
    )
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError(
                "SOURCE_RECORDING_ROOT_UNAVAILABLE: "
                "/recordings/official-replay.mp4"
            )
        ),
    )
    monkeypatch.setattr(
        producer_source_media,
        "ffprobe_duration_ms",
        lambda _path: 4_000,
    )
    monkeypatch.setattr(
        producer_source_media,
        "run",
        lambda command, **_kwargs: (
            shutil.copyfile(command[1], command[2])
            if command[0] == "cp"
            else pytest.fail("must not recut a valid cache")
        ),
    )
    spec = {
        "pieces": [
            {
                "remote_media": source_path,
                "start_ms": 1_000,
                "end_ms": 5_000,
            }
        ]
    }

    prepared = producer_source_media.prepare_source_media(
        spec=spec,
        cid="candidate",
        out_root=out_root,
        host="localhost",
    )

    assert local.is_file()
    assert spec["pieces"][0]["source_media_sha256"] == "sha256:" + source_sha256
    assert prepared.piece_provenance_rows[0]["source_revalidation_status"] == (
        "HASH_BOUND_CACHE_SOURCE_ROOT_UNAVAILABLE"
    )


@pytest.mark.parametrize(
    "failure",
    [
        "SOURCE_MEDIA_MISSING: /recordings/official-replay.mp4",
        "SOURCE_RECORDING_ROOT_UNAVAILABLE: /recordings/official-replay.mp4",
    ],
)
def test_prepare_source_media_does_not_reuse_ineligible_or_corrupt_cache(
    monkeypatch, tmp_path, failure
):
    out_root, local, source_path = _install_exact_piece_provenance(
        tmp_path,
        source_sha256="a" * 64,
    )
    if failure.startswith("SOURCE_RECORDING_ROOT_UNAVAILABLE"):
        local.write_bytes(b"tampered cached piece")
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda *_args: (_ for _ in ()).throw(RuntimeError(failure)),
    )
    spec = {
        "pieces": [
            {
                "remote_media": source_path,
                "start_ms": 1_000,
                "end_ms": 5_000,
            }
        ]
    }

    with pytest.raises(RuntimeError, match=failure.split(":", 1)[0]):
        producer_source_media.prepare_source_media(
            spec=spec,
            cid="candidate",
            out_root=out_root,
            host="localhost",
        )


def _install_retained_parent(tmp_path):
    parent_root = tmp_path / "retained-parent"
    parent_root.mkdir()
    source = tmp_path / "recordings" / "official-replay.mp4"
    source.parent.mkdir()
    source.write_bytes(b"immutable source bytes")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()

    piece = parent_root / "piece_0_1000_5000.mp4"
    piece.write_bytes(b"retained piece bytes")
    piece_sha = hashlib.sha256(piece.read_bytes()).hexdigest()
    piece_provenance = {
        "source_path": str(source.resolve()),
        "source_sha256": source_sha,
        "source_media_binding": f"sha256:{source_sha}",
        "start_ms": 1_000,
        "end_ms": 5_000,
        "output_path": str(piece.resolve()),
        "output_sha256": piece_sha,
    }
    piece_provenance_path = piece.with_suffix(".provenance.json")
    piece_provenance_path.write_text(
        json.dumps(piece_provenance, ensure_ascii=False),
        encoding="utf-8",
    )
    piece_provenance_raw = piece_provenance_path.read_bytes()

    padded = parent_root / "padded_1000_5000.mp4"
    padded.write_bytes(b"retained padded bytes")
    padded_sha = hashlib.sha256(padded.read_bytes()).hexdigest()
    padded_provenance = {
        "inputs": [{"path": str(piece.resolve()), "sha256": piece_sha}],
        "output_path": str(padded.resolve()),
        "output_sha256": padded_sha,
    }
    padded_provenance_path = padded.with_suffix(".provenance.json")
    padded_provenance_path.write_text(
        json.dumps(padded_provenance, ensure_ascii=False),
        encoding="utf-8",
    )
    retained = {
        "padded_sha256": f"sha256:{padded_sha}",
        "parent_materialization_path": str(padded),
        "parent_materialization_sha256": f"sha256:{padded_sha}",
        "parent_materialization_duration_ms": 4_000,
        "parent_materialization_provenance_path": str(padded_provenance_path),
        "parent_materialization_provenance_sha256": (
            "sha256:"
            + hashlib.sha256(padded_provenance_path.read_bytes()).hexdigest()
        ),
    }
    spec = {
        "candidate_id": "retained-candidate",
        "pieces": [
            {
                "remote_media": str(source.resolve()),
                "start_ms": 1_000,
                "end_ms": 5_000,
                "source_media_sha256": f"sha256:{source_sha}",
            }
        ],
        "retained_current_text": retained,
    }
    retained["source_media_sha256s"] = [f"sha256:{source_sha}"]
    return {
        "parent_root": parent_root,
        "source": source,
        "source_sha": source_sha,
        "piece": piece,
        "piece_provenance": piece_provenance_path,
        "piece_provenance_raw": piece_provenance_raw,
        "padded": padded,
        "padded_provenance": padded_provenance_path,
        "spec": spec,
    }


def test_retained_old_multi_piece_materialization_blocks_without_mutation(tmp_path):
    fixture = _install_retained_parent(tmp_path)
    fixture["spec"]["pieces"].append(dict(fixture["spec"]["pieces"][0]))
    before = {path: path.read_bytes() for path in fixture["parent_root"].iterdir()}
    with pytest.raises(RuntimeError, match="RETAINED_MULTI_PIECE_CLOCK_MATERIALIZATION_UNVERIFIED"):
        producer_source_media.prepare_source_media(
            spec=fixture["spec"], cid="retained-candidate", out_root=tmp_path / "new", host="localhost",
        )
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert not (tmp_path / "new").exists()


def test_prepare_source_media_reuses_retained_parent_through_normal_consumer(
    monkeypatch, tmp_path
):
    fixture = _install_retained_parent(tmp_path)
    output_root = tmp_path / "new-normal-output"
    source = fixture["source"]
    source_sha = fixture["source_sha"]
    calls = []
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda host, path: (calls.append((host, path)) or (str(source.resolve()), source_sha)),
    )
    monkeypatch.setattr(
        producer_source_media,
        "ffprobe_duration_ms",
        lambda _path: 4_000,
    )
    monkeypatch.setattr(
        producer_source_media,
        "run",
        lambda *_args, **_kwargs: pytest.fail("retained cache must not recut or concatenate"),
    )

    prepared = producer_source_media.prepare_source_media(
        spec=fixture["spec"],
        cid="retained-candidate",
        out_root=output_root,
        host="localhost",
        spec_parent=tmp_path,
    )

    assert prepared.padded == fixture["padded"]
    assert prepared.padded.read_bytes() == fixture["padded"].read_bytes()
    assert output_root.joinpath("piece_0_1000_5000.provenance.json").read_bytes() == (
        fixture["piece_provenance_raw"]
    )
    assert not output_root.joinpath("piece_0_1000_5000.mp4").exists()
    assert prepared.piece_provenance_rows[0]["output_path"] == str(
        fixture["piece"].resolve()
    )
    assert calls == [("localhost", source)]


@pytest.mark.parametrize(
    "measured_ms,stream_duration,accepted",
    [(4014, "4.014", True), (4019, "4.019", False), (4014, "4.100", False)],
)
def test_retained_mux_rounding_uses_measured_frame_and_all_streams(
    monkeypatch, tmp_path, measured_ms, stream_duration, accepted
):
    import subprocess
    from types import SimpleNamespace

    fixture = _install_retained_parent(tmp_path)
    original_spec = json.loads(json.dumps(fixture["spec"]))
    monkeypatch.setattr(
        producer_source_media, "_source_media_sha256",
        lambda host, path: (str(fixture["source"].resolve()), fixture["source_sha"]),
    )
    monkeypatch.setattr(producer_source_media, "ffprobe_duration_ms", lambda path: measured_ms)
    monkeypatch.setattr(
        producer_source_media, "run",
        lambda *args, **kwargs: pytest.fail("retained media must not be rematerialized"),
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps({
            "format": {"duration": str(measured_ms / 1000)},
            "streams": [
                {"codec_type": "video", "duration": str(measured_ms / 1000), "avg_frame_rate": "60/1"},
                {"codec_type": "audio", "duration": stream_duration},
            ],
        })),
    )
    def call():
        return producer_source_media.prepare_source_media(
            spec=fixture["spec"], cid="retained-candidate",
            out_root=tmp_path / "new-normal-output", host="localhost", spec_parent=tmp_path,
        )
    if accepted:
        prepared = call()
        assert prepared.padded_duration_ms == measured_ms
        assert fixture["spec"]["retained_current_text"]["parent_materialization_duration_ms"] == 4000
        assert fixture["spec"]["pieces"] == original_spec["pieces"]
    else:
        with pytest.raises(RuntimeError, match="RETAINED_PARENT_DURATION_MISMATCH"):
            call()
    assert fixture["padded"].read_bytes() == b"retained padded bytes"


@pytest.mark.parametrize("failure_kind", ["source", "missing", "piece"])
def test_prepare_source_media_retained_failure_does_not_mutate_new_or_parent_cache(
    monkeypatch, tmp_path, failure_kind
):
    fixture = _install_retained_parent(tmp_path)
    output_root = tmp_path / "new-normal-output"
    source = fixture["source"]
    source_sha = fixture["source_sha"]
    parent_piece_before = fixture["piece"].read_bytes()
    parent_provenance_before = fixture["piece_provenance"].read_bytes()
    padded_before = fixture["padded"].read_bytes()
    padded_provenance_before = fixture["padded_provenance"].read_bytes()
    if failure_kind == "source":
        monkeypatch.setattr(
            producer_source_media,
            "_source_media_sha256",
            lambda *_args: (str(source.resolve()), "b" * 64),
        )
    elif failure_kind == "missing":
        monkeypatch.setattr(
            producer_source_media,
            "_source_media_sha256",
            lambda *_args: (_ for _ in ()).throw(
                RuntimeError("SOURCE_MEDIA_MISSING: official-replay.mp4")
            ),
        )
    else:
        monkeypatch.setattr(
            producer_source_media,
            "_source_media_sha256",
            lambda *_args: (str(source.resolve()), source_sha),
        )
        fixture["piece"].write_bytes(b"tampered retained piece")
    monkeypatch.setattr(
        producer_source_media,
        "run",
        lambda *_args, **_kwargs: pytest.fail("retained validation must fail before recut"),
    )

    expected = {
        "source": "RETAINED_PIECE_SOURCE_SHA256_MISMATCH",
        "missing": "SOURCE_MEDIA_MISSING",
        "piece": "RETAINED_PIECE_OUTPUT_HASH_MISMATCH",
    }[failure_kind]
    with pytest.raises(RuntimeError, match=expected):
        producer_source_media.prepare_source_media(
            spec=fixture["spec"],
            cid="retained-candidate",
            out_root=output_root,
            host="localhost",
            spec_parent=tmp_path,
        )

    assert not output_root.exists()
    assert fixture["padded"].read_bytes() == padded_before
    assert fixture["padded_provenance"].read_bytes() == padded_provenance_before
    assert fixture["piece_provenance"].read_bytes() == parent_provenance_before
    if failure_kind != "piece":
        assert fixture["piece"].read_bytes() == parent_piece_before


@pytest.mark.parametrize(
    ("field", "value"),
    [("start_ms", True), ("end_ms", 5_000.0), ("start_ms", "1000")],
)
def test_prepare_source_media_rejects_non_integer_retained_interval_without_writes(
    monkeypatch, tmp_path, field, value
):
    fixture = _install_retained_parent(tmp_path)
    fixture["spec"]["pieces"][0][field] = value
    output_root = tmp_path / "new-normal-output"
    padded_before = fixture["padded"].read_bytes()
    padded_provenance_before = fixture["padded_provenance"].read_bytes()
    monkeypatch.setattr(
        producer_source_media,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid retained interval must stop before recut"),
    )

    with pytest.raises(RuntimeError, match="RETAINED_PIECE_(START|END)_MS_INVALID"):
        producer_source_media.prepare_source_media(
            spec=fixture["spec"],
            cid="retained-candidate",
            out_root=output_root,
            host="localhost",
            spec_parent=tmp_path,
        )

    assert not output_root.exists()
    assert fixture["padded"].read_bytes() == padded_before
    assert fixture["padded_provenance"].read_bytes() == padded_provenance_before


def test_prepare_source_media_rejects_hash_valid_padded_alias_before_normal_mutation(
    monkeypatch, tmp_path
):
    fixture = _install_retained_parent(tmp_path)
    old_padded = fixture["padded"]
    old_provenance = fixture["padded_provenance"]
    padded_alias = fixture["parent_root"] / "padded-alias.mp4"
    provenance_alias = fixture["parent_root"] / "padded-alias.provenance.json"
    old_padded.rename(padded_alias)
    old_provenance.rename(provenance_alias)
    provenance = json.loads(provenance_alias.read_text(encoding="utf-8"))
    provenance["output_path"] = str(padded_alias.resolve())
    provenance_alias.write_text(json.dumps(provenance), encoding="utf-8")
    retained = fixture["spec"]["retained_current_text"]
    retained["parent_materialization_path"] = str(padded_alias)
    retained["parent_materialization_provenance_path"] = str(provenance_alias)
    retained["parent_materialization_provenance_sha256"] = (
        "sha256:" + hashlib.sha256(provenance_alias.read_bytes()).hexdigest()
    )
    output_root = tmp_path / "new-normal-output"
    padded_before = padded_alias.read_bytes()
    provenance_before = provenance_alias.read_bytes()
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda *_args: (str(fixture["source"].resolve()), fixture["source_sha"]),
    )
    monkeypatch.setattr(
        producer_source_media,
        "run",
        lambda *_args, **_kwargs: pytest.fail("异名 padded must fail before normal mutation"),
    )

    with pytest.raises(RuntimeError, match="RETAINED_PARENT_MATERIALIZATION_NAME_MISMATCH"):
        producer_source_media.prepare_source_media(
            spec=fixture["spec"],
            cid="retained-candidate",
            out_root=output_root,
            host="localhost",
            spec_parent=tmp_path,
        )

    assert not output_root.exists()
    assert padded_alias.read_bytes() == padded_before
    assert provenance_alias.read_bytes() == provenance_before


def test_prepare_source_media_rejects_hash_valid_piece_alias_before_normal_mutation(
    monkeypatch, tmp_path
):
    fixture = _install_retained_parent(tmp_path)
    old_piece = fixture["piece"]
    piece_alias = fixture["parent_root"] / "piece-alias.mp4"
    old_piece.rename(piece_alias)
    piece_provenance = json.loads(
        fixture["piece_provenance"].read_text(encoding="utf-8")
    )
    piece_provenance["output_path"] = str(piece_alias.resolve())
    fixture["piece_provenance"].write_text(
        json.dumps(piece_provenance),
        encoding="utf-8",
    )
    padded_provenance = json.loads(
        fixture["padded_provenance"].read_text(encoding="utf-8")
    )
    padded_provenance["inputs"][0]["path"] = str(piece_alias.resolve())
    fixture["padded_provenance"].write_text(
        json.dumps(padded_provenance),
        encoding="utf-8",
    )
    retained = fixture["spec"]["retained_current_text"]
    retained["parent_materialization_provenance_sha256"] = (
        "sha256:"
        + hashlib.sha256(fixture["padded_provenance"].read_bytes()).hexdigest()
    )
    output_root = tmp_path / "new-normal-output"
    piece_before = piece_alias.read_bytes()
    piece_provenance_before = fixture["piece_provenance"].read_bytes()
    padded_provenance_before = fixture["padded_provenance"].read_bytes()
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda *_args: (str(fixture["source"].resolve()), fixture["source_sha"]),
    )
    monkeypatch.setattr(
        producer_source_media,
        "run",
        lambda *_args, **_kwargs: pytest.fail("异名 piece must fail before normal mutation"),
    )

    with pytest.raises(RuntimeError, match="RETAINED_PIECE_OUTPUT_NAME_MISMATCH"):
        producer_source_media.prepare_source_media(
            spec=fixture["spec"],
            cid="retained-candidate",
            out_root=output_root,
            host="localhost",
            spec_parent=tmp_path,
        )

    assert not output_root.exists()
    assert piece_alias.read_bytes() == piece_before
    assert fixture["piece_provenance"].read_bytes() == piece_provenance_before
    assert fixture["padded_provenance"].read_bytes() == padded_provenance_before


def test_prepare_source_media_rejects_hash_valid_provenance_drift_before_normal_mutation(
    monkeypatch, tmp_path
):
    fixture = _install_retained_parent(tmp_path)
    provenance = json.loads(
        fixture["padded_provenance"].read_text(encoding="utf-8")
    )
    provenance["unexpected_drift"] = True
    fixture["padded_provenance"].write_text(
        json.dumps(provenance),
        encoding="utf-8",
    )
    fixture["spec"]["retained_current_text"]["parent_materialization_provenance_sha256"] = (
        "sha256:"
        + hashlib.sha256(fixture["padded_provenance"].read_bytes()).hexdigest()
    )
    output_root = tmp_path / "new-normal-output"
    padded_provenance_before = fixture["padded_provenance"].read_bytes()
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda *_args: (str(fixture["source"].resolve()), fixture["source_sha"]),
    )
    monkeypatch.setattr(
        producer_source_media,
        "run",
        lambda *_args, **_kwargs: pytest.fail("provenance drift must stop before normal mutation"),
    )

    with pytest.raises(RuntimeError, match="RETAINED_PARENT_PROVENANCE_INVALID"):
        producer_source_media.prepare_source_media(
            spec=fixture["spec"],
            cid="retained-candidate",
            out_root=output_root,
            host="localhost",
            spec_parent=tmp_path,
        )

    assert not output_root.exists()
    assert fixture["padded_provenance"].read_bytes() == padded_provenance_before
