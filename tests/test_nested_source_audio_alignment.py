"""Regression tests for the evidence-only nested source time mapper."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.autoslice.nested_source_audio_alignment import (
    NestedSourceAudioAlignmentError, _best_peak, _peak_ambiguous,
    prepare_nested_source_mapping,
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _stereo_signal(seconds: int = 24, *, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    count = seconds * 8000
    # Two independent broadband components exercise both mid and side
    # representations instead of allowing a mono-only correlation to pass.
    first = rng.normal(0.0, 0.25, count)
    second = rng.normal(0.0, 0.18, count)
    return np.column_stack((first + second, 0.18 * second)).astype("<f4")


def _install_fake_ffmpeg(monkeypatch, audio: dict[str, np.ndarray]) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[0] == "ffprobe":
            media = Path(argv[-1])
            duration = audio[str(media)].shape[0] / 8000.0
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{"codec_type": "audio", "sample_rate": "8000", "channels": 2}],
                    "format": {"duration": str(duration)},
                }),
                stderr="",
            )
        assert argv[0] == "ffmpeg"
        media = Path(argv[argv.index("-i") + 1])
        output = Path(argv[-1])
        output.write_bytes(audio[str(media)].tobytes())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "src.autoslice.nested_source_audio_alignment.subprocess.run", run,
    )
    return calls


def _fixture(tmp_path: Path, *, outer: np.ndarray, source: np.ndarray):
    outer_path = tmp_path / "outer.mp4"
    source_path = tmp_path / "source.mp4"
    outer_path.write_bytes(b"outer-fixture")
    source_path.write_bytes(b"source-fixture")
    return outer_path, source_path, {str(outer_path): outer, str(source_path): source}


def test_stable_delay_with_mixed_host_produces_evidence_only_mapping(tmp_path, monkeypatch):
    source = _stereo_signal(seed=4)
    lag_samples = 20_000  # 2.5 s
    outer = np.random.default_rng(8).normal(0.0, 0.02, (30 * 8000, 2)).astype("<f4")
    outer[lag_samples : lag_samples + source.shape[0]] += source
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)

    result = prepare_nested_source_mapping(
        outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
        [6000, 13000], tmp_path / "evidence",
    )

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["decision"] == "MAPPING_CANDIDATE_BOUND"
    assert result["mutation_authorized"] is False
    mapping_path = Path(result["mapping_receipt"]["path"])
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert mapping["status"] == "EVIDENCE_ONLY"
    assert mapping["media"]["inner"]["cid"] == "123"
    assert mapping["lineage_mapping"]["outer_window_ms"] == [6000, 13000]
    assert abs(mapping["lineage_mapping"]["lag_ms"] - 2500.0) <= 1.0
    assert _sha(mapping_path) == result["mapping_receipt"]["sha256"]
    assert len(calls) == 4


def _local_scope(*, frame_path: str = "/tmp/caption.png") -> dict[str, object]:
    return {
        "schema_version": "nested-source-audio-local-scope.v1",
        "outer_window_ms": [0, 25000],
        "source_window_ms": [0, 20000],
        "chunk_windows_ms": [[0, 8000], [8000, 16000]],
        "descriptor_context_interval_ms": [6000, 13000],
        "target_interval_ms": [7000, 9000],
        "target_cue_indexes": [1],
        "observation_id": "cue-1",
        "caption_frame_path": frame_path,
        "caption_frame_sha256": "sha256:" + "a" * 64,
        "caption_frame_ms": 8000,
        "final_srt_sha256": "sha256:" + "b" * 64,
    }


def _current_cue_scope(
    outer_path: Path, *, context: list[int] | None = None,
    target: list[int] | None = None, frame_ms: int = 8000,
    candidate_id: str = "candidate",
) -> dict[str, object]:
    context = context or [6000, 13000]
    target = target or [7000, 9000]
    return {
        "schema_version": "nested-source-audio-current-cue-scope.v1",
        "kind": "CURRENT_WATCHED_MEDIA_CUE",
        "candidate_id": candidate_id,
        "observation_id": "candidate-scene-1-cue-1",
        "descriptor_context_interval_ms": context,
        "target_interval_ms": target,
        "target_cue_indexes": [1],
        "caption_frame_path": "/tmp/current-caption.png",
        "caption_frame_sha256": "sha256:" + "a" * 64,
        "caption_frame_ms": frame_ms,
        "final_srt_sha256": "sha256:" + "b" * 64,
        "outer_media_sha256": _sha(outer_path),
    }


def _outer_context_surface(
    tmp_path: Path,
    *,
    outer: Path,
    context: Path,
    scope: dict[str, object],
) -> tuple[dict[str, object], Path, Path]:
    current_srt = tmp_path / "current.recut.srt"
    current_srt.write_text(
        "1\n00:00:07,000 --> 00:00:09,000\n当前 cue\n\n"
        "2\n00:00:12,000 --> 00:00:13,000\n邻近 cue\n",
        encoding="utf-8",
    )
    frame = tmp_path / "current-frame.jpg"
    frame.write_bytes(b"current-frame")
    scope["caption_frame_path"] = str(frame)
    scope["caption_frame_sha256"] = _sha(frame)
    scope["final_srt_sha256"] = _sha(current_srt)

    provenance = tmp_path / "recut.provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-speaker-recut-provenance.v1",
                "final_recut": {
                    "source_path": str(context),
                    "source_sha256": hashlib.sha256(context.read_bytes()).hexdigest(),
                    "start_ms": 1_000,
                    "end_ms": 21_000,
                    "output_path": str(outer),
                    "output_sha256": hashlib.sha256(outer.read_bytes()).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    outer_context = {
        "context_media": {"path": str(context), "sha256": _sha(context)},
        "current_srt": {"path": str(current_srt), "sha256": _sha(current_srt)},
        "recut_provenance": {"path": str(provenance), "sha256": _sha(provenance)},
    }
    return outer_context, current_srt, frame


def test_outer_context_projects_current_recut_and_replays_without_measurement(
    tmp_path, monkeypatch,
):
    source = _stereo_signal(seconds=40, seed=71)
    source_offset = 7 * 8000
    context_audio = np.random.default_rng(72).normal(
        0.0, 0.02, (30 * 8000, 2),
    ).astype("<f4")
    context_audio += source[source_offset : source_offset + context_audio.shape[0]]
    outer_audio = context_audio[8000 : 21 * 8000]
    outer_path = tmp_path / "current-recut.mp4"
    context_path = tmp_path / "padded-context.mp4"
    source_path = tmp_path / "source.mp4"
    outer_path.write_bytes(b"current recut bytes")
    context_path.write_bytes(b"padded context bytes")
    source_path.write_bytes(b"independent source bytes")
    audio = {str(outer_path): outer_audio, str(context_path): context_audio, str(source_path): source}
    calls = _install_fake_ffmpeg(monkeypatch, audio)
    monkeypatch.setattr(
        "src.autoslice.nested_source_audio_alignment._extract_outer_context_frame",
        lambda _media, _frame_ms: b"context-frame-bytes",
    )
    scope = _current_cue_scope(outer_path)
    outer_context, current_srt, _frame = _outer_context_surface(
        tmp_path, outer=outer_path, context=context_path, scope=scope,
    )

    result = prepare_nested_source_mapping(
        outer_path,
        source_path,
        {"bvid": "BVTEST123456", "cid": 123},
        [6000, 13000],
        tmp_path / "outer-context",
        current_cue_scope=scope,
        candidate_id="candidate",
        outer_context=outer_context,
    )

    assert result["decision"] == "MAPPING_CANDIDATE_BOUND"
    assert result["mapping_scope"] == "CURRENT_CUE_REVERSE_NCC"
    mapping = json.loads(Path(result["mapping_receipt"]["path"]).read_text(encoding="utf-8"))
    assert mapping["media"]["outer"]["path"] == str(outer_path)
    assert mapping["lineage_mapping"]["outer_window_ms"] == [6000, 13000]
    projection = mapping["outer_context_projection"]
    assert projection["offset_ms"] == 1000
    assert projection["current_srt"]["path"] == str(current_srt)
    projected_srt = Path(projection["context_srt"]["path"])
    assert "00:00:08,000 --> 00:00:10,000" in projected_srt.read_text(encoding="utf-8")
    assert Path(projection["context_frame"]["path"]).read_bytes() == b"context-frame-bytes"
    assert Path(mapping["context_mapping_receipt"]["path"]).is_file()
    first_call_count = len(calls)
    second = prepare_nested_source_mapping(
        outer_path,
        source_path,
        {"bvid": "BVTEST123456", "cid": 123},
        [6000, 13000],
        tmp_path / "outer-context",
        current_cue_scope=scope,
        candidate_id="candidate",
        outer_context=outer_context,
    )
    assert second["replayed"] is True
    assert len(calls) == first_call_count


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("outer", "ALIGNMENT_OUTER_CONTEXT_PROVENANCE_PATH_MISMATCH"),
        ("srt", "ALIGNMENT_OUTER_CONTEXT_SRT_SHA256_MISMATCH"),
        ("frame", "ALIGNMENT_OUTER_CONTEXT_FRAME_SHA256_MISMATCH"),
    ],
)
def test_outer_context_rejects_current_binding_drift(tmp_path, monkeypatch, mutation, reason):
    source = _stereo_signal(seconds=40, seed=73)
    context_audio = np.random.default_rng(74).normal(
        0.0, 0.02, (30 * 8000, 2),
    ).astype("<f4")
    outer_audio = np.random.default_rng(75).normal(
        0.0, 0.02, (20 * 8000, 2),
    ).astype("<f4")
    outer_path, source_path, audio = _fixture(
        tmp_path, outer=outer_audio, source=source,
    )
    context_path = tmp_path / "padded-context.mp4"
    context_path.write_bytes(b"padded context bytes")
    audio[str(context_path)] = context_audio
    _install_fake_ffmpeg(monkeypatch, audio)
    scope = _current_cue_scope(outer_path)
    outer_context, _srt, frame = _outer_context_surface(
        tmp_path, outer=outer_path, context=context_path, scope=scope,
    )
    if mutation == "outer":
        other = tmp_path / "other-recut.mp4"
        other.write_bytes(b"other")
        outer_context["recut_provenance"]["path"] = outer_context["recut_provenance"]["path"]
        document = json.loads(Path(outer_context["recut_provenance"]["path"]).read_text())
        document["final_recut"]["output_path"] = str(other)
        Path(outer_context["recut_provenance"]["path"]).write_text(json.dumps(document))
        outer_context["recut_provenance"]["sha256"] = _sha(Path(outer_context["recut_provenance"]["path"]))
    elif mutation == "srt":
        outer_context["current_srt"]["sha256"] = "sha256:" + "0" * 64
    else:
        frame.write_bytes(b"drifted frame")

    with pytest.raises(NestedSourceAudioAlignmentError, match=reason):
        prepare_nested_source_mapping(
            outer_path,
            source_path,
            {"bvid": "BVTEST123456", "cid": 123},
            [6000, 13000],
            tmp_path / f"drift-{mutation}",
            current_cue_scope=scope,
            candidate_id="candidate",
            outer_context=outer_context,
        )


def test_outer_context_rejects_interval_outside_recut_before_mapping(tmp_path, monkeypatch):
    source = _stereo_signal(seconds=40, seed=76)
    outer = np.random.default_rng(77).normal(
        0.0, 0.02, (20 * 8000, 2),
    ).astype("<f4")
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    context_path = tmp_path / "padded-context.mp4"
    context_path.write_bytes(b"padded context bytes")
    audio[str(context_path)] = np.random.default_rng(78).normal(
        0.0, 0.02, (30 * 8000, 2),
    ).astype("<f4")
    _install_fake_ffmpeg(monkeypatch, audio)
    scope = _current_cue_scope(outer_path, context=[6000, 22000], target=[7000, 9000])
    outer_context, _srt, _frame = _outer_context_surface(
        tmp_path, outer=outer_path, context=context_path, scope=scope,
    )

    with pytest.raises(
        NestedSourceAudioAlignmentError,
        match="ALIGNMENT_OUTER_CONTEXT_INTERVAL_OUT_OF_RECUT",
    ):
        prepare_nested_source_mapping(
            outer_path,
            source_path,
            {"bvid": "BVTEST123456", "cid": 123},
            [6000, 22000],
            tmp_path / "boundary",
            current_cue_scope=scope,
            candidate_id="candidate",
            outer_context=outer_context,
        )


def test_current_cue_scope_reverse_search_projects_long_source_to_current_cue(
    tmp_path, monkeypatch,
):
    source = _stereo_signal(seconds=40, seed=54)
    source_offset = 7 * 8000
    outer = np.random.default_rng(55).normal(
        0.0, 0.02, (30 * 8000, 2),
    ).astype("<f4")
    outer[: 20 * 8000] += source[source_offset : source_offset + 20 * 8000]
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)

    result = prepare_nested_source_mapping(
        outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
        [6000, 13000], tmp_path / "current", current_cue_scope=_current_cue_scope(outer_path),
        candidate_id="candidate",
    )

    assert result["decision"] == "MAPPING_CANDIDATE_BOUND"
    assert result["mapping_scope"] == "CURRENT_CUE_REVERSE_NCC"
    mapping = json.loads(Path(result["mapping_receipt"]["path"]).read_text(encoding="utf-8"))
    assert mapping["alignment_candidate"] == "CURRENT_CUE_REVERSE_NCC_CANDIDATE"
    assert mapping["lineage_mapping"]["outer_window_ms"] == [6000, 13000]
    assert mapping["lineage_mapping"]["source_window_ms"] == [13000, 20000]
    assert abs(mapping["lineage_mapping"]["lag_ms"] + 7000.0) <= 1.0
    assert mapping["alignment_evidence"]["measurement_orientation"] == (
        "independent_source_long_to_current_outer_short"
    )
    inverse = mapping["alignment_evidence"]["inverse_measurement"]
    assert inverse["decision_metrics"]["unique_offset"] is True
    assert abs(inverse["decision_metrics"]["source_start_ms"] - 7000.0) <= 1.0
    assert len(calls) == 4


def test_current_cue_scope_rejects_ambiguous_inverse_without_trying_another_window(
    tmp_path, monkeypatch,
):
    repeated = _stereo_signal(seconds=20, seed=56)
    source = np.concatenate((repeated, repeated), axis=0)
    outer = np.random.default_rng(57).normal(
        0.0, 0.02, (30 * 8000, 2),
    ).astype("<f4")
    outer[: 20 * 8000] += repeated
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)

    result = prepare_nested_source_mapping(
        outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
        [6000, 13000], tmp_path / "ambiguous",
        current_cue_scope=_current_cue_scope(outer_path), candidate_id="candidate",
    )

    assert result["decision"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "ALIGNMENT_CURRENT_CUE_INVERSE_WEAK_OR_AMBIGUOUS"
    assert result["alignment_evidence"]["inverse_measurement"]["side"]["best"]["ambiguous_peak"] is True
    assert len(calls) == 4


def test_current_cue_scope_rejects_outer_window_shorter_than_required_two_chunks(
    tmp_path, monkeypatch,
):
    source = _stereo_signal(seconds=20, seed=58)
    outer = np.random.default_rng(59).normal(
        0.0, 0.02, (15 * 8000, 2),
    ).astype("<f4")
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)

    result = prepare_nested_source_mapping(
        outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
        [4000, 9000], tmp_path / "short",
        current_cue_scope=_current_cue_scope(
            outer_path, context=[4000, 9000], target=[5000, 7000], frame_ms=6000,
        ), candidate_id="candidate",
    )

    assert result["decision"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "ALIGNMENT_CURRENT_CUE_OUTER_TOO_SHORT"
    assert len(calls) == 4


def test_current_cue_scope_media_and_descriptor_drift_blocks_before_media_probe(
    tmp_path, monkeypatch,
):
    source = _stereo_signal(seconds=40, seed=60)
    outer = np.zeros((30 * 8000, 2), dtype="<f4")
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)
    scope = _current_cue_scope(outer_path)
    scope["outer_media_sha256"] = "sha256:" + "c" * 64
    with pytest.raises(
        NestedSourceAudioAlignmentError, match="ALIGNMENT_CURRENT_CUE_SCOPE_MEDIA_MISMATCH"
    ):
        prepare_nested_source_mapping(
            outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
            [6000, 13000], tmp_path / "drift", current_cue_scope=scope,
            candidate_id="candidate",
        )
    assert calls == []

    scope = _current_cue_scope(outer_path)
    scope["caption_frame_ms"] = 9500
    with pytest.raises(
        NestedSourceAudioAlignmentError, match="ALIGNMENT_CURRENT_CUE_SCOPE_FRAME_INVALID"
    ):
        prepare_nested_source_mapping(
            outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
            [6000, 13000], tmp_path / "frame-drift", current_cue_scope=scope,
            candidate_id="candidate",
        )
    assert calls == []


def test_explicit_local_scope_projects_receipt_to_original_clocks(tmp_path, monkeypatch):
    source = _stereo_signal(seconds=20, seed=44)
    lag_samples = 20_000  # 2.5 s
    outer = np.random.default_rng(45).normal(0.0, 0.02, (30 * 8000, 2)).astype("<f4")
    outer[lag_samples : lag_samples + source.shape[0]] += source
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)

    result = prepare_nested_source_mapping(
        outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
        [6000, 13000], tmp_path / "local-evidence", local_scope=_local_scope(),
    )

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["decision"] == "MAPPING_CANDIDATE_BOUND"
    assert result["mapping_scope"] == "LOCAL_BOUNDED"
    assert result["global_decision"] == "MAPPING_CANDIDATE_BOUND"
    mapping = json.loads(Path(result["mapping_receipt"]["path"]).read_text(encoding="utf-8"))
    assert mapping["alignment_candidate"] == "LOCAL_SCOPE_LINEAR_AUDIO_OFFSET_CANDIDATE"
    assert mapping["lineage_mapping"]["outer_window_ms"] == [6000, 13000]
    assert mapping["lineage_mapping"]["source_window_ms"] == [3500, 10500]
    assert abs(mapping["lineage_mapping"]["lag_ms"] - 2500.0) <= 1.0
    assert mapping["alignment_scope"]["target_interval_ms"] == [7000, 9000]
    assert mapping["alignment_scope"]["final_srt_sha256"] == "sha256:" + "b" * 64
    assert result["alignment_evidence"]["whole_source_alignment"]["accepted"] is True
    assert result["alignment_evidence"]["local_scope_alignment"]["decision"] == "MAPPING_CANDIDATE_BOUND"
    assert len(calls) == 4


def test_local_scope_rejects_overlapping_chunks_before_media_probe(tmp_path, monkeypatch):
    scope = _local_scope()
    scope["chunk_windows_ms"] = [[0, 8000], [7000, 15000]]
    source = _stereo_signal(seconds=20, seed=46)
    outer = np.zeros((30 * 8000, 2), dtype="<f4")
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)

    with pytest.raises(NestedSourceAudioAlignmentError, match="ALIGNMENT_LOCAL_SCOPE_CHUNKS_OVERLAP"):
        prepare_nested_source_mapping(
            outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
            [6000, 13000], tmp_path / "invalid", local_scope=scope,
        )
    assert calls == []


def test_local_scope_cache_binding_drift_is_not_replayed(tmp_path, monkeypatch):
    source = _stereo_signal(seconds=20, seed=47)
    outer = np.random.default_rng(48).normal(0.0, 0.02, (30 * 8000, 2)).astype("<f4")
    outer[20_000 : 20_000 + source.shape[0]] += source
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)
    kwargs = {
        "outer_media": outer_path,
        "source_media": source_path,
        "source_identity": {"bvid": "BVTEST123456", "cid": 123},
        "interval_ms": [6000, 13000],
        "evidence_root": tmp_path / "cache",
        "local_scope": _local_scope(),
    }
    first = prepare_nested_source_mapping(**kwargs)
    assert first["decision"] == "MAPPING_CANDIDATE_BOUND"
    request_path = Path(first["request_path"])
    saved = json.loads(request_path.read_text(encoding="utf-8"))
    drifted = copy.deepcopy(saved)
    drifted["local_scope"]["target_cue_indexes"] = [2]
    request_path.write_text(json.dumps(drifted, sort_keys=True), encoding="utf-8")
    with pytest.raises(NestedSourceAudioAlignmentError, match="ALIGNMENT_CACHE_BINDING_DRIFT"):
        prepare_nested_source_mapping(**kwargs)
    assert len(calls) == 4


def test_noncoherent_audio_is_unknown_and_exact_input_replays_without_extract(tmp_path, monkeypatch):
    source = _stereo_signal(seed=11)
    outer = _stereo_signal(seconds=30, seed=12)
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)
    kwargs = dict(
        outer_media=outer_path, source_media=source_path,
        source_identity={"bvid": "BVTEST123456", "cid": 123},
        interval_ms=[6000, 13000], evidence_root=tmp_path / "evidence",
    )

    first = prepare_nested_source_mapping(**kwargs)
    assert first["status"] == "EVIDENCE_ONLY"
    assert first["decision"] == "UNKNOWN_KEEP"
    assert first["reason_code"] in {
        "ALIGNMENT_GLOBAL_WEAK_OR_INCONSISTENT",
        "ALIGNMENT_LOCAL_WEAK_NONLINEAR_OR_INCONSISTENT",
        "ALIGNMENT_TARGET_WINDOW_UNSUPPORTED",
    }
    assert len(calls) == 4

    second = prepare_nested_source_mapping(**kwargs)
    assert second["replayed"] is True
    assert second["request_sha256"] == first["request_sha256"]
    assert len(calls) == 4


def test_nonlinear_delay_and_out_of_bounds_interval_keep_unknown(tmp_path, monkeypatch):
    source = _stereo_signal(seed=17)
    outer = np.random.default_rng(19).normal(0.0, 0.02, (42 * 8000, 2)).astype("<f4")
    half = source.shape[0] // 2
    outer[20_000 : 20_000 + half] += source[:half]
    outer[36_000 : 36_000 + source.shape[0] - half] += source[half:]
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)
    identity = {"bvid": "BVTEST123456", "cid": 123}

    nonlinear = prepare_nested_source_mapping(
        outer_path, source_path, identity, [6000, 13000], tmp_path / "nonlinear",
    )
    assert nonlinear["decision"] == "UNKNOWN_KEEP"
    assert nonlinear["reason_code"] in {
        "ALIGNMENT_GLOBAL_WEAK_OR_INCONSISTENT",
        "ALIGNMENT_LOCAL_WEAK_NONLINEAR_OR_INCONSISTENT",
        "ALIGNMENT_TARGET_WINDOW_UNSUPPORTED",
    }

    out_of_bounds = prepare_nested_source_mapping(
        outer_path, source_path, identity, [41_000, 43_000], tmp_path / "bounds",
    )
    assert out_of_bounds["decision"] == "UNKNOWN_KEEP"
    assert out_of_bounds["reason_code"] == "ALIGNMENT_TARGET_OUTSIDE_OUTER"
    assert len(calls) == 8


def test_competing_full_source_peaks_keep_unknown(tmp_path, monkeypatch):
    source = _stereo_signal(seed=23)
    outer = np.random.default_rng(29).normal(0.0, 0.01, (40 * 8000, 2)).astype("<f4")
    outer[20_000 : 20_000 + source.shape[0]] += source
    outer[100_000 : 100_000 + source.shape[0]] += source
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    _install_fake_ffmpeg(monkeypatch, audio)

    result = prepare_nested_source_mapping(
        outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
        [6000, 13000], tmp_path / "evidence",
    )

    assert result["decision"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "ALIGNMENT_GLOBAL_WEAK_OR_INCONSISTENT"


def test_broad_first_peak_cannot_hide_a_separate_competing_match():
    scores = np.zeros(200)
    scores[30:60] = np.linspace(0.99, 1.0, 30)
    scores[150] = 0.98
    summary = _best_peak(scores, separation_samples=40)
    assert summary["peaks"][1]["lag_samples"] == 150
    assert _peak_ambiguous(summary)


def test_unsupported_long_source_is_rejected_before_pcm_extraction(tmp_path, monkeypatch):
    source = _stereo_signal(seconds=30, seed=41)
    outer = _stereo_signal(seconds=24, seed=43)
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    calls = _install_fake_ffmpeg(monkeypatch, audio)
    result = prepare_nested_source_mapping(
        outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
        [6000, 13000], tmp_path / "evidence",
    )
    assert result["decision"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "ALIGNMENT_SOURCE_LONGER_THAN_OUTER"
    assert [call[0] for call in calls] == ["ffprobe", "ffprobe"]


def test_input_changed_during_extraction_never_yields_a_mapping(tmp_path, monkeypatch):
    source = _stereo_signal(seed=37)
    outer = np.zeros((30 * 8000, 2), dtype="<f4")
    outer[20_000:20_000 + len(source)] = source
    outer_path, source_path, audio = _fixture(tmp_path, outer=outer, source=source)
    _install_fake_ffmpeg(monkeypatch, audio)
    from src.autoslice import nested_source_audio_alignment as alignment
    extract = alignment._run_extract
    def changed(media, output, *, temp):
        result = extract(media, output, temp=temp)
        if media == source_path:
            source_path.write_bytes(b"changed-source")
        return result
    monkeypatch.setattr(alignment, "_run_extract", changed)
    result = prepare_nested_source_mapping(
        outer_path, source_path, {"bvid": "BVTEST123456", "cid": 123},
        [6000, 13000], tmp_path / "evidence",
    )
    assert result["decision"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "ALIGNMENT_MEDIA_CHANGED_DURING_EXTRACTION"
    assert "mapping_receipt" not in result
