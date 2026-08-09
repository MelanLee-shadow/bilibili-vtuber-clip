import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.produce_slice_package import run_speaker_finalizer
from scripts.apply_subtitle_text_overrides import TextCue

from src.autoslice.speaker_finalizer import (
    CAMPP_EMBEDDING_CACHE_SCHEMA,
    CAMPP_EMBEDDING_DIMENSION,
    MIXED_OVERLAP_EVIDENCE_SCHEMA,
    SOURCE_SESSION_ANCHOR_SCHEMA,
    SpeakerFinalizationError,
    _assert_runtime_assets_stable,
    _build_embedding_similarity,
    _campp_similarity_score,
    _campp_embedding,
    _context_prompt,
    _cosine_similarity,
    _load_source_session_anchor_samples,
    _resolve_singleton_outlier,
    _run_campplus_analysis,
    _singleton_nonlexical_dominant,
    _speaker_context_env,
    _validate_source_session_anchor_document,
    finalize_speaker_subtitles,
    resolve_ambiguous_labels,
    validate_mixed_overlap_evidence_document,
    validate_speaker_review_manifest_document,
)
from src.autoslice.host_vocal_proof import _sha256_directory
from src.autoslice.speaker_common import GUEST_SPEAKER, HOST_SPEAKER
from src.autoslice.surface_canon import CHANNEL_PROFILE


def _source_session_document() -> dict:
    anchors = []
    for cue_index in (9, 11):
        anchors.append(
            {
                "source_cue": cue_index,
                "start": "00:00:01,000",
                "end": "00:00:03,000",
                "text": f"{HOST_SPEAKER} donor",
                "sample_sha256": str(cue_index)[0] * 64,
                "reference_scores": {"r1": 0.72, "r2": 0.74, "r3": 0.76},
                "enroll_median_score": 0.74,
            }
        )
    return {
        "schema_version": SOURCE_SESSION_ANCHOR_SCHEMA,
        "status": "READY",
        "subject": HOST_SPEAKER,
        "source_session_id": "session-1",
        "source_recording": "/remote/session.mp4",
        "profile_sha256": "a" * 64,
        "model_tree_sha256": "b" * 64,
        "reference_hashes": {"r1": "c" * 64, "r2": "d" * 64, "r3": "e" * 64},
        "allowed_targets": [
            {
                "candidate_id": "target-1",
                "media_path": "/remote/target.mp4",
                "media_sha256": "f" * 64,
                "provenance_path": "/remote/target-spec.json",
                "provenance_sha256": "3" * 64,
            }
        ],
        "donor": {
            "candidate_id": "donor-1",
            "media_path": "/remote/donor.mp4",
            "media_sha256": "1" * 64,
            "text_srt_path": "/remote/donor.srt",
            "text_srt_sha256": "2" * 64,
            "provenance_path": "/remote/donor-spec.json",
            "provenance_sha256": "4" * 64,
        },
        "anchors": anchors,
    }


class _CountingCampp:
    """Fake ModelScope SV pipeline: one embedding per call, records every call."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors
        self.calls: list[dict[str, object]] = []
        self.score_calls: list[tuple[object, object]] = []

    def __call__(self, inputs, output_emb: bool = False, thr=None):
        self.calls.append({"inputs": list(inputs), "output_emb": output_emb})
        if not output_emb or len(inputs) != 1:
            raise AssertionError(
                "embed-once path must call the pipeline with one wav and output_emb=True"
            )
        return {"embs": [list(self._vectors[str(inputs[0])])]}

    def compute_cos_similarity(self, left, right):
        self.score_calls.append((left, right))
        left_values = left.tolist() if hasattr(left, "tolist") else left
        right_values = right.tolist() if hasattr(right, "tolist") else right
        return _cosine_similarity(left_values, right_values)


def _campp_vector(*head: float) -> list[float]:
    assert len(head) <= CAMPP_EMBEDDING_DIMENSION
    return [*head, *([0.0] * (CAMPP_EMBEDDING_DIMENSION - len(head)))]


def test_cosine_similarity_matches_pipeline_pairwise_semantics() -> None:
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0
    # Magnitude-invariant, like the torch CosineSimilarity the CAM++ pipeline uses.
    assert abs(_cosine_similarity([2.0, 1.0], [4.0, 2.0]) - 1.0) < 1e-9
    # A silent/degenerate embedding must not divide by zero.
    assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    # Torch clamps each norm independently; 1e-4 is above its 1e-6 epsilon.
    assert _cosine_similarity([1e-4], [1e-4]) == 1.0
    # Values below epsilon are attenuated on both sides.
    assert abs(_cosine_similarity([1e-8], [1e-8]) - 1e-4) < 1e-12


@pytest.mark.parametrize(
    "left,right",
    [
        ([float("nan"), 0.0], [1.0, 0.0]),
        ([float("inf"), 0.0], [1.0, 0.0]),
        ([], []),
        ([1.0], [1.0, 0.0]),
    ],
)
def test_cosine_similarity_rejects_invalid_embeddings(left, right) -> None:
    with pytest.raises(SpeakerFinalizationError):
        _cosine_similarity(left, right)


def test_campp_similarity_uses_runtime_float32_scorer_and_validates_score() -> None:
    class _Runtime:
        def __init__(self, score):
            self.score = score
            self.calls = []

        def compute_cos_similarity(self, left, right):
            self.calls.append((left, right))
            return self.score

    runtime = _Runtime(0.1234567)
    assert _campp_similarity_score(
        runtime, _campp_vector(1.0), _campp_vector(0.0, 1.0)
    ) == 0.1234567
    assert len(runtime.calls) == 1

    for invalid in (float("nan"), float("inf"), 1.1):
        with pytest.raises(SpeakerFinalizationError):
            _campp_similarity_score(
                _Runtime(invalid), _campp_vector(1.0), _campp_vector(0.0, 1.0)
            )

    # Float32 cosine can overshoot one by a few ULP; production rounds this to
    # 1.0, while a materially invalid score must still fail closed.
    assert _campp_similarity_score(
        _Runtime(1.000000119), _campp_vector(1.0), _campp_vector(1.0)
    ) == 1.0


def test_campp_embedding_rejects_wrong_dimension_and_degenerate_norm() -> None:
    with pytest.raises(SpeakerFinalizationError, match="192 values"):
        _campp_embedding(_CountingCampp({"x.wav": [1.0, 0.0]}), Path("x.wav"))
    with pytest.raises(SpeakerFinalizationError, match="norm is degenerate"):
        _campp_embedding(
            _CountingCampp({"x.wav": _campp_vector(1e-10)}), Path("x.wav")
        )


def test_campp_embedding_uses_single_input_output_emb() -> None:
    expected = _campp_vector(0.6, 0.8)
    verifier = _CountingCampp({"/tmp/a.wav": expected})
    assert _campp_embedding(verifier, Path("/tmp/a.wav")) == expected
    assert verifier.calls == [{"inputs": ["/tmp/a.wav"], "output_emb": True}]


def test_campp_embedding_accepts_numpy_like_and_raw_array_returns() -> None:
    class _Row(list):
        def tolist(self):  # emulate a numpy ndarray row
            return [float(x) for x in self]

    class _RawArray:
        def __call__(self, inputs, output_emb: bool = False, thr=None):
            return [_Row(_campp_vector(0.1, 0.2, 0.3))]  # no Mapping wrapper

    assert _campp_embedding(_RawArray(), Path("x.wav")) == _campp_vector(0.1, 0.2, 0.3)


def test_embedding_similarity_embeds_each_wav_once_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import math as _math
    import src.autoslice.speaker_finalizer as speaker_finalizer

    unit = {
        "a": _campp_vector(1.0),
        "b": _campp_vector(0.0, 1.0),
        "c": _campp_vector(1.0, 1.0),
    }
    wavs: dict[str, Path] = {}
    vectors: dict[str, list[float]] = {}
    for name, vec in unit.items():
        p = tmp_path / f"cue-{name}.wav"
        p.write_bytes(name.encode() * 32)  # distinct content -> distinct fingerprint
        wavs[name] = p
        vectors[str(p)] = vec

    verifier = _CountingCampp(vectors)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    writes: list[tuple[Path, int]] = []
    real_atomic_write = speaker_finalizer.atomic_write_text

    def counted_write(path: Path, value: str) -> None:
        writes.append((path, len(value.encode("utf-8"))))
        real_atomic_write(path, value)

    monkeypatch.setattr(speaker_finalizer, "atomic_write_text", counted_write)
    similarity = _build_embedding_similarity(
        verifier=verifier, model_hash="model-x", work_dir=work_dir
    )

    got = {
        ("a", "b"): similarity(wavs["a"], wavs["b"]),
        ("a", "c"): similarity(wavs["a"], wavs["c"]),
        ("b", "c"): similarity(wavs["b"], wavs["c"]),
        ("a", "b_again"): similarity(wavs["a"], wavs["b"]),
        ("a", "a"): similarity(wavs["a"], wavs["a"]),
    }

    # O(cues): exactly one embedding inference per distinct wav, never a pair call.
    assert len(verifier.calls) == 3
    assert sorted(c["inputs"][0] for c in verifier.calls) == sorted(vectors)
    assert all(len(c["inputs"]) == 1 for c in verifier.calls)
    assert len(verifier.score_calls) == 5

    # Score parity with a cosine pipeline, rounded to the pipeline's 5 dp.
    assert got[("a", "b")] == 0.0
    assert got[("a", "a")] == 1.0
    assert got[("a", "c")] == round(1.0 / _math.sqrt(2.0), 5)
    assert got[("a", "b")] == got[("a", "b_again")]

    # A persisted cache lets a fresh builder score with zero new inferences.
    cache_files = sorted((work_dir / "embedding-cache-v3").glob("*.json"))
    assert len(cache_files) == 3
    assert len(writes) == 3
    assert sum(size for _, size in writes) < 30_000
    for cache_file in cache_files:
        assert json.loads(cache_file.read_text(encoding="utf-8"))["schema_version"] == (
            CAMPP_EMBEDDING_CACHE_SCHEMA
        )
    verifier2 = _CountingCampp(vectors)
    similarity2 = _build_embedding_similarity(
        verifier=verifier2, model_hash="model-x", work_dir=work_dir
    )
    assert similarity2(wavs["b"], wavs["c"]) == round(1.0 / _math.sqrt(2.0), 5)
    assert verifier2.calls == []
    assert len(verifier2.score_calls) == 1
    assert len(writes) == 3


def test_embedding_cache_tamper_reembeds_instead_of_scoring_modified_vector(
    tmp_path: Path,
) -> None:
    wav_a = tmp_path / "a.wav"
    wav_b = tmp_path / "b.wav"
    wav_a.write_bytes(b"a" * 32)
    wav_b.write_bytes(b"b" * 32)
    vectors = {
        str(wav_a): _campp_vector(1.0),
        str(wav_b): _campp_vector(0.0, 1.0),
    }
    work_dir = tmp_path / "work"
    similarity = _build_embedding_similarity(
        verifier=_CountingCampp(vectors), model_hash="model-x", work_dir=work_dir
    )
    assert similarity(wav_a, wav_b) == 0.0

    cache_files = sorted((work_dir / "embedding-cache-v3").glob("*.json"))
    assert len(cache_files) == 2
    tampered = json.loads(cache_files[0].read_text(encoding="utf-8"))
    tampered["embedding"] = _campp_vector(1.0, 1.0)
    # Deliberately leave the old checksum: this entry must never be scored.
    cache_files[0].write_text(json.dumps(tampered), encoding="utf-8")

    verifier = _CountingCampp(vectors)
    similarity2 = _build_embedding_similarity(
        verifier=verifier, model_hash="model-x", work_dir=work_dir
    )
    assert similarity2(wav_a, wav_b) == 0.0
    assert len(verifier.calls) == 1


def test_embedding_cache_rejects_vector_and_digest_spliced_from_other_audio(
    tmp_path: Path,
) -> None:
    wav_a = tmp_path / "a.wav"
    wav_b = tmp_path / "b.wav"
    wav_a.write_bytes(b"a" * 32)
    wav_b.write_bytes(b"b" * 32)
    vectors = {
        str(wav_a): _campp_vector(1.0),
        str(wav_b): _campp_vector(0.0, 1.0),
    }
    work_dir = tmp_path / "work"
    initial = _build_embedding_similarity(
        verifier=_CountingCampp(vectors), model_hash="model-x", work_dir=work_dir
    )
    assert initial(wav_a, wav_b) == 0.0

    documents: dict[str, tuple[Path, dict]] = {}
    for path in (work_dir / "embedding-cache-v3").glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        documents[document["audio_sha256"]] = (path, document)
    audio_a = hashlib.sha256(wav_a.read_bytes()).hexdigest()
    audio_b = hashlib.sha256(wav_b.read_bytes()).hexdigest()
    _path_a, document_a = documents[audio_a]
    path_b, document_b = documents[audio_b]
    document_b["embedding"] = document_a["embedding"]
    document_b["binding_sha256"] = document_a["binding_sha256"]
    path_b.write_text(json.dumps(document_b), encoding="utf-8")

    verifier = _CountingCampp(vectors)
    after_splice = _build_embedding_similarity(
        verifier=verifier, model_hash="model-x", work_dir=work_dir
    )
    assert after_splice(wav_a, wav_b) == 0.0
    assert len(verifier.calls) == 1


def test_embedding_cache_runtime_drift_reembeds_all_entries(tmp_path: Path) -> None:
    class _OtherCountingCampp(_CountingCampp):
        pass

    wav_a = tmp_path / "a.wav"
    wav_b = tmp_path / "b.wav"
    wav_a.write_bytes(b"a" * 32)
    wav_b.write_bytes(b"b" * 32)
    vectors = {
        str(wav_a): _campp_vector(1.0),
        str(wav_b): _campp_vector(0.0, 1.0),
    }
    work_dir = tmp_path / "work"
    initial = _build_embedding_similarity(
        verifier=_CountingCampp(vectors), model_hash="model-x", work_dir=work_dir
    )
    assert initial(wav_a, wav_b) == 0.0

    verifier = _OtherCountingCampp(vectors)
    after_drift = _build_embedding_similarity(
        verifier=verifier, model_hash="model-x", work_dir=work_dir
    )
    assert after_drift(wav_a, wav_b) == 0.0
    assert len(verifier.calls) == 2


def test_runtime_model_and_reference_assets_are_rehashed_before_ready(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    model_file = model_dir / "weights.bin"
    model_file.write_bytes(b"model-a")
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference-a")
    rows = [
        {
            "id": "ref-1",
            "path": reference,
            "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        }
    ]
    model_hash = _sha256_directory(model_dir)
    _assert_runtime_assets_stable(
        model_dir=model_dir,
        model_tree_sha256=model_hash,
        references=rows,
    )
    model_file.write_bytes(b"model-b")
    with pytest.raises(SpeakerFinalizationError, match="model tree drifted"):
        _assert_runtime_assets_stable(
            model_dir=model_dir,
            model_tree_sha256=model_hash,
            references=rows,
        )
    model_file.write_bytes(b"model-a")
    reference.write_bytes(b"reference-b")
    with pytest.raises(SpeakerFinalizationError, match="voiceprint reference drifted"):
        _assert_runtime_assets_stable(
            model_dir=model_dir,
            model_tree_sha256=model_hash,
            references=rows,
        )


def test_source_session_anchors_are_target_bound_and_keep_the_original_high_gate() -> None:
    document = _source_session_document()
    validated = _validate_source_session_anchor_document(
        document,
        target_media_sha256="f" * 64,
        profile_sha256="a" * 64,
        model_tree_sha256="b" * 64,
        reference_hashes={"r1": "c" * 64, "r2": "d" * 64, "r3": "e" * 64},
        host_seed_min=0.68,
    )
    assert validated["source_session_id"] == "session-1"

    with pytest.raises(SpeakerFinalizationError, match="not allowlisted"):
        _validate_source_session_anchor_document(
            document,
            target_media_sha256="0" * 64,
            profile_sha256="a" * 64,
            model_tree_sha256="b" * 64,
            reference_hashes={"r1": "c" * 64, "r2": "d" * 64, "r3": "e" * 64},
            host_seed_min=0.68,
        )

    document["anchors"][0]["reference_scores"] = {"r1": 0.40, "r2": 0.42, "r3": 0.44}
    document["anchors"][0]["enroll_median_score"] = 0.42
    with pytest.raises(SpeakerFinalizationError, match="unchanged host seed gate"):
        _validate_source_session_anchor_document(
            document,
            target_media_sha256="f" * 64,
            profile_sha256="a" * 64,
            model_tree_sha256="b" * 64,
            reference_hashes={"r1": "c" * 64, "r2": "d" * 64, "r3": "e" * 64},
            host_seed_min=0.68,
        )


def test_source_session_loader_creates_donor_workdir_and_replays_hash_bound_cues(
    tmp_path: Path, monkeypatch,
) -> None:
    source_recording_path = tmp_path / "source-recording.mp4"
    source_recording_path.write_bytes(b"source-recording")
    source_recording = str(source_recording_path)
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    target_media = tmp_path / "target.mp4"
    target_media.write_bytes(b"target-media")
    donor_media = tmp_path / "donor.mp4"
    donor_media.write_bytes(b"donor-media")
    donor_srt = tmp_path / "donor.srt"
    donor_srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nanchor one\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\nanchor two\n",
        encoding="utf-8",
    )
    donor_spec = tmp_path / "donor-spec.json"
    donor_spec.write_text(
        json.dumps(
            {
                "candidate_id": "donor-1",
                "pieces": [{"remote_media": source_recording}],
            }
        ),
        encoding="utf-8",
    )
    target_spec = tmp_path / "target-spec.json"
    target_spec.write_text(
        json.dumps(
            {
                "candidate_id": "target-1",
                "pieces": [{"remote_media": source_recording}],
            }
        ),
        encoding="utf-8",
    )
    reference_rows = []
    reference_hashes = {}
    for index, reference_id in enumerate(("r1", "r2", "r3"), start=1):
        path = tmp_path / f"{reference_id}.wav"
        path.write_bytes(f"reference-{index}".encode())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        reference_hashes[reference_id] = digest
        reference_rows.append({"id": reference_id, "sha256": digest, "path": path})

    samples = [tmp_path / "sample-1.wav", tmp_path / "sample-2.wav"]
    samples[0].write_bytes(b"sample-one")
    samples[1].write_bytes(b"sample-two")
    document = _source_session_document()
    document.update(
        source_recording=source_recording,
        profile_sha256=hashlib.sha256(profile.read_bytes()).hexdigest(),
        model_tree_sha256="b" * 64,
        reference_hashes=reference_hashes,
        allowed_targets=[
            {
                "candidate_id": "target-1",
                "media_path": str(target_media),
                "media_sha256": hashlib.sha256(target_media.read_bytes()).hexdigest(),
                "provenance_path": str(target_spec),
                "provenance_sha256": hashlib.sha256(target_spec.read_bytes()).hexdigest(),
            }
        ],
        donor={
            "candidate_id": "donor-1",
            "media_path": str(donor_media),
            "media_sha256": hashlib.sha256(donor_media.read_bytes()).hexdigest(),
            "text_srt_path": str(donor_srt),
            "text_srt_sha256": hashlib.sha256(donor_srt.read_bytes()).hexdigest(),
            "provenance_path": str(donor_spec),
            "provenance_sha256": hashlib.sha256(donor_spec.read_bytes()).hexdigest(),
        },
        anchors=[
            {
                "source_cue": index,
                "start": f"00:00:0{1 if index == 1 else 4},000",
                "end": f"00:00:0{3 if index == 1 else 6},000",
                "text": f"anchor {'one' if index == 1 else 'two'}",
                "sample_sha256": hashlib.sha256(samples[index - 1].read_bytes()).hexdigest(),
                "reference_scores": {"r1": 0.72, "r2": 0.74, "r3": 0.76},
                "enroll_median_score": 0.74,
            }
            for index in (1, 2)
        ],
    )
    manifest = tmp_path / "session.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    def fake_extract(_media, _cues, work_dir):
        assert work_dir.is_dir()
        return None, 16_000, samples

    monkeypatch.setattr(
        "src.autoslice.speaker_finalizer._extract_cue_wavs", fake_extract
    )
    score_by_reference = {"r1.wav": 0.72, "r2.wav": 0.74, "r3.wav": 0.76}
    loaded, evidence = _load_source_session_anchor_samples(
        manifest,
        target_media_path=target_media,
        profile_path=profile,
        references=reference_rows,
        model_tree_sha256="b" * 64,
        host_seed_min=0.68,
        work_dir=tmp_path / "work",
        similarity=lambda left, _right: score_by_reference[left.name],
    )
    assert loaded == samples
    assert evidence["source_recording"] == source_recording
    assert evidence["target_candidate_id"] == "target-1"


def test_source_session_loader_reextracts_hash_bound_source_recording_segments(
    tmp_path: Path, monkeypatch,
) -> None:
    source_recording = tmp_path / "source.mp4"
    source_recording.write_bytes(b"source")
    canonical_target_media = tmp_path / "canonical-target.mp4"
    canonical_target_media.write_bytes(b"target")
    target_media = tmp_path / "runtime-copy.mp4"
    target_media.write_bytes(canonical_target_media.read_bytes())
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    target_spec = tmp_path / "target-spec.json"
    target_spec.write_text(
        json.dumps(
            {
                "candidate_id": "target-1",
                "pieces": [{"remote_media": str(source_recording)}],
            }
        ),
        encoding="utf-8",
    )
    reference_rows = []
    reference_hashes = {}
    for reference_id in ("r1", "r2", "r3"):
        path = tmp_path / f"{reference_id}.wav"
        path.write_bytes(reference_id.encode())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        reference_hashes[reference_id] = digest
        reference_rows.append({"id": reference_id, "sha256": digest, "path": path})
    sample_payloads = {1_000: b"segment-one", 5_000: b"segment-two"}
    anchors = []
    for start_ms, payload in sample_payloads.items():
        anchors.append(
            {
                "anchor_type": "source_recording_segment",
                "source_start_ms": start_ms,
                "source_end_ms": start_ms + 2_000,
                "text": f"segment {start_ms}",
                "sample_sha256": hashlib.sha256(payload).hexdigest(),
                "reference_scores": {"r1": 0.72, "r2": 0.74, "r3": 0.76},
                "enroll_median_score": 0.74,
            }
        )
    document = {
        "schema_version": SOURCE_SESSION_ANCHOR_SCHEMA,
        "status": "READY",
        "subject": HOST_SPEAKER,
        "source_session_id": "session-1",
        "source_recording": str(source_recording),
        "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
        "model_tree_sha256": "b" * 64,
        "reference_hashes": reference_hashes,
        "allowed_targets": [
            {
                "candidate_id": "target-1",
                "media_path": str(canonical_target_media),
                "media_sha256": hashlib.sha256(canonical_target_media.read_bytes()).hexdigest(),
                "provenance_path": str(target_spec),
                "provenance_sha256": hashlib.sha256(target_spec.read_bytes()).hexdigest(),
            }
        ],
        "anchors": anchors,
    }
    manifest = tmp_path / "segments.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    def fake_extract(_source, *, start_ms, expected_duration_ms, output_path):
        assert expected_duration_ms == 2_000
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(sample_payloads[start_ms])

    monkeypatch.setattr(
        "src.autoslice.speaker_finalizer._extract_checkpoint", fake_extract
    )
    scores = {"r1.wav": 0.72, "r2.wav": 0.74, "r3.wav": 0.76}
    loaded, evidence = _load_source_session_anchor_samples(
        manifest,
        target_media_path=target_media,
        profile_path=profile,
        references=reference_rows,
        model_tree_sha256="b" * 64,
        host_seed_min=0.68,
        work_dir=tmp_path / "work",
        similarity=lambda left, _right: scores[left.name],
    )
    assert len(loaded) == 2
    assert [row["anchor_type"] for row in evidence["anchors"]] == [
        "source_recording_segment",
        "source_recording_segment",
    ]

    mutated = False

    def drift_canonical_target(left, _right):
        nonlocal mutated
        if not mutated:
            canonical_target_media.write_bytes(b"drifted")
            mutated = True
        return scores[left.name]

    with pytest.raises(
        SpeakerFinalizationError,
        match="canonical target media drifted during analysis",
    ):
        _load_source_session_anchor_samples(
            manifest,
            target_media_path=target_media,
            profile_path=profile,
            references=reference_rows,
            model_tree_sha256="b" * 64,
            host_seed_min=0.68,
            work_dir=tmp_path / "drift-work",
            similarity=drift_canonical_target,
        )


def test_runner_surfaces_blocked_manifest_reason_before_runtime_warnings(
    tmp_path: Path, monkeypatch,
) -> None:
    output_manifest = tmp_path / "speaker.json"
    (tmp_path / "media.mp4").write_bytes(b"media")
    (tmp_path / "text.srt").write_text("text", encoding="utf-8")

    def fake_run(*_args, **_kwargs):
        output_manifest.write_text(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "production_ready": False,
                    "reason": "SpeakerFinalizationError: not enough Li Dousha clip anchors: []",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess([], 3, stdout="", stderr="ModelScope warning noise")

    monkeypatch.setattr("scripts.produce_slice_package.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="not enough Li Dousha clip anchors"):
        run_speaker_finalizer(
            host="localhost",
            candidate_id="candidate",
            media_path=tmp_path / "media.mp4",
            text_srt_path=tmp_path / "text.srt",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=output_manifest,
            work_dir=tmp_path / "work",
            speaker_python=tmp_path / "python",
        )


def test_runner_surfaces_structured_speaker_review_status(tmp_path: Path, monkeypatch) -> None:
    output_manifest = tmp_path / "speaker.json"
    (tmp_path / "media.mp4").write_bytes(b"media")
    (tmp_path / "text.srt").write_text("text", encoding="utf-8")

    def fake_run(*_args, **_kwargs):
        output_manifest.write_text(
            json.dumps(
                {
                    "status": "SPEAKER_REVIEW_REQUIRED",
                    "production_ready": False,
                    "reason": "singleton speaker evidence requires review",
                    "source_media_sha256": hashlib.sha256(b"media").hexdigest(),
                    "text_final_srt_sha256": hashlib.sha256(b"text").hexdigest(),
                    "context_unresolved_cues": [1],
                    "review_required_cues": [
                        {
                            "source_cue": 1,
                            "zero_based_index": 0,
                            "start": "00:00:00,000",
                            "end": "00:00:01,000",
                            "text": "fixture",
                            "audio_sha256": "a" * 64,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess([], 4, stdout="", stderr="warning noise")

    monkeypatch.setattr("scripts.produce_slice_package.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="SPEAKER_REVIEW_REQUIRED: singleton"):
        run_speaker_finalizer(
            host="localhost",
            candidate_id="candidate",
            media_path=tmp_path / "media.mp4",
            text_srt_path=tmp_path / "text.srt",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=output_manifest,
            work_dir=tmp_path / "work",
            speaker_python=tmp_path / "python",
        )


def test_runner_rejects_malformed_speaker_review_manifest(tmp_path: Path, monkeypatch) -> None:
    output_manifest = tmp_path / "speaker.json"
    (tmp_path / "media.mp4").write_bytes(b"media")
    (tmp_path / "text.srt").write_text("text", encoding="utf-8")

    def fake_run(*_args, **_kwargs):
        output_manifest.write_text(
            json.dumps(
                {
                    "status": "SPEAKER_REVIEW_REQUIRED",
                    "production_ready": False,
                    "reason": "missing evidence",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess([], 4, stdout="", stderr="speaker exited 4")

    monkeypatch.setattr("scripts.produce_slice_package.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="SPEAKER_FINALIZATION_FAILED") as raised:
        run_speaker_finalizer(
            host="localhost",
            candidate_id="candidate",
            media_path=tmp_path / "media.mp4",
            text_srt_path=tmp_path / "text.srt",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=output_manifest,
            work_dir=tmp_path / "work",
            speaker_python=tmp_path / "python",
        )
    assert "SPEAKER_REVIEW_REQUIRED" not in str(raised.value)

def test_context_prompt_treats_exact_shadow_name_as_host_not_fourth_speaker() -> None:
    prompt = _context_prompt([], [], [])
    # Pin updated 2026-08-07: Ivan's asymmetric host-evidence ruling added a
    # sentence telling the judge its HOST vote is only adopted inside the
    # acoustic corroboration band (speaker_host_evidence.py).
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
        "24fdbbaacdc0e2e71a343e81828c21fe7cfa02575cade8d7318ca7111f90adbd"
    )
    assert (
        f"精确词 {CHANNEL_PROFILE.speaker_identity_aliases[-1]} 是{HOST_SPEAKER}的自称之一"
        in prompt
    )
    assert "不是第四位说话人" in prompt


def test_speaker_context_loads_private_runtime_cpa_env(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "cpa.env"
    env_file.write_text(
        "export CPA_BASE_URL='http://127.0.0.1:8317/v1'\nexport CPA_API_KEY='secret-test-value'\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.setenv("AUTOSLICE_CPA_ENV", str(env_file))
    env = _speaker_context_env()
    assert env["CPA_BASE_URL"] == "http://127.0.0.1:8317/v1"
    assert env["CPA_API_KEY"] == "secret-test-value"


def test_ambiguous_speaker_resolution_defaults_to_guest_and_requires_hard_evidence() -> None:
    """Ivan 2026-08-07: default GUEST; HOST only inside the corroboration band."""

    policy = {
        "host_semantic_min_confidence": 0.7,
    }
    labels, sources = resolve_ambiguous_labels(
        ["连线", None, HOST_SPEAKER, None, HOST_SPEAKER],
        [-0.3, 0.02, 0.3, 0.01, 0.4],
        0.0,
        {1: HOST_SPEAKER},
        band=0.1,
        policy=policy,
        context_confidences={1: 0.9},
    )
    # index 1 sits on the HOST-leaning half of the narrow corroboration band and
    # the context vote clears the confidence floor, so it corroborates to HOST;
    # index 3 sits inside the same half-band but has no vote and defaults GUEST.
    assert labels == ["连线", HOST_SPEAKER, HOST_SPEAKER, GUEST_SPEAKER, HOST_SPEAKER]
    assert sources[1] == "campp_semantic_corroborated"
    assert sources[3] == "guest_default_ambiguity"


def test_ambiguous_speaker_resolution_rejects_semantic_host_outside_corroboration_band() -> None:
    """A confident semantic HOST vote must not override deep guest-ward acoustics."""

    policy = {
        "host_semantic_min_confidence": 0.7,
    }
    labels, sources = resolve_ambiguous_labels(
        [None],
        [-0.5],
        0.0,
        {0: HOST_SPEAKER},
        band=0.6,
        policy=policy,
        context_confidences={0: 0.95},
    )
    assert labels == [GUEST_SPEAKER]
    assert sources[0] == "guest_default_ambiguity"


def test_finalizer_binds_text_before_speaker_and_renders_colour_without_prefixes(tmp_path: Path) -> None:
    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n她想问是三个位置哦\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n结果还是聋人啊\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    reference_dir = tmp_path / "refs"
    model_dir = tmp_path / "model"
    reference_dir.mkdir()
    model_dir.mkdir()

    def fake_analyzer(**kwargs):
        assert [cue.text for cue in kwargs["cues"]] == ["她想问是三个位置哦", "结果还是聋人啊"]
        return {
            "mode": "multi_speaker",
            "multi_speaker_detected": True,
            "decisions": [
                {"speaker": "连线", "decision_source": "campp_audio", "margin": -0.4},
                {"speaker": "连线", "decision_source": "whole_clip_context", "margin": 0.01},
            ],
        }

    output_srt = tmp_path / "speaker-final.srt"
    output_ass = tmp_path / "speaker-final.ass"
    output_manifest = tmp_path / "speaker-final.json"
    manifest = finalize_speaker_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        profile_path=profile,
        reference_dir=reference_dir,
        model_dir=model_dir,
        output_srt_path=output_srt,
        output_ass_path=output_ass,
        output_manifest_path=output_manifest,
        work_dir=tmp_path / "work",
        analyzer=fake_analyzer,
    )

    assert "[连线] 她想问是三个位置哦" in output_srt.read_text(encoding="utf-8")
    ass = output_ass.read_text(encoding="utf-8")
    assert "Style: LDS" in ass and "Style: GUEST" in ass
    assert "[连线]" not in ass and f"[{HOST_SPEAKER}]" not in ass
    assert "Dialogue: 0,0:00:02.00,0:00:04.00,GUEST" in ass
    assert manifest["stage_order"] == "text_final_then_speaker_then_ass_then_burn"
    assert manifest["visible_speaker_prefixes"] is False
    assert manifest["subtitle_style"] == f"{CHANNEL_PROFILE.profile_id}-speaker-sapphire-host-white-guest-v2"
    assert manifest["speaker_taxonomy"] == "binary_visual_host_vs_guest"
    assert manifest["host_identity_aliases"] == list(CHANNEL_PROFILE.speaker_identity_aliases)
    assert json.loads(output_manifest.read_text(encoding="utf-8"))["production_ready"] is True


def test_reviewed_speaker_overrides_reject_automatic_label_drift_even_when_text_matches(
    tmp_path: Path,
) -> None:
    import hashlib
    import pytest

    from src.autoslice.speaker_finalizer import SpeakerFinalizationError

    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n结果还是聋人啊\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": "test_candidate",
                "source_media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "text_final_srt_sha256": hashlib.sha256(text_srt.read_bytes()).hexdigest(),
                "source_srt_sha256": hashlib.sha256(
                    "1\n00:00:00,000 --> 00:00:02,000\n[连线] 结果还是聋人啊\n".encode()
                ).hexdigest(),
                "overrides": [
                    {
                        "source_cue": 1,
                        "expect": {
                            "start": "00:00:00,000",
                            "end": "00:00:02,000",
                            "text": "结果还是聋人啊",
                        },
                        "authority": "Ivan direct correction",
                        "segments": [
                            {
                                "start": "00:00:00,000",
                                "end": "00:00:02,000",
                                "speaker": "连线",
                                "speaker_detail": "乙乙/Sumi",
                                "text": "结果还是聋人啊",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def wrong_auto(**_kwargs):
        return {
            "decisions": [{"speaker": HOST_SPEAKER, "decision_source": "acoustic_threshold_fallback", "margin": 0.5}],
            "context_unresolved_cues": [1],
        }

    with pytest.raises(SpeakerFinalizationError, match="source hash mismatch"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            candidate_id="test_candidate",
            override_path=overrides,
            analyzer=wrong_auto,
        )


def test_reviewed_context_votes_are_hash_bound_and_passed_to_analyzer(tmp_path: Path) -> None:
    import hashlib

    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n嘿嘿嘿\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    accepted_automatic = (
        "1\n00:00:00,000 --> 00:00:02,000\n[连线] 嘿嘿嘿\n".encode()
    )
    accepted_hash = hashlib.sha256(accepted_automatic).hexdigest()
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": "test_candidate",
                "source_media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "text_final_srt_sha256": hashlib.sha256(text_srt.read_bytes()).hexdigest(),
                "source_srt_sha256": accepted_hash,
                "reviewed_context_votes": {
                    "authority": "accepted review fixture",
                    "source_automatic_srt_sha256": accepted_hash,
                    "labels": {"1": "连线"},
                },
                "overrides": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def analyzer(**kwargs):
        assert kwargs["reviewed_context_votes"] == {0: "连线"}
        return {
            "decisions": [
                {"speaker": "连线", "decision_source": "accepted_context_baseline", "margin": -0.1}
            ],
            "context_unresolved_cues": [],
        }

    output_srt = tmp_path / "speaker.srt"
    manifest = finalize_speaker_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        profile_path=profile,
        reference_dir=tmp_path / "refs",
        model_dir=tmp_path / "model",
        output_srt_path=output_srt,
        output_ass_path=tmp_path / "speaker.ass",
        output_manifest_path=tmp_path / "speaker.json",
        work_dir=tmp_path / "work",
        candidate_id="test_candidate",
        override_path=overrides,
        analyzer=analyzer,
    )
    assert output_srt.read_bytes() == accepted_automatic
    assert manifest["automatic_labelled_srt_sha256"] == accepted_hash
    assert manifest["reviewed_output_cue_count"] == 0
    assert manifest["accepted_context_output_cue_count"] == 1


def test_reviewed_speaker_override_rejects_media_drift(tmp_path: Path) -> None:
    import hashlib
    import pytest

    from src.autoslice.speaker_finalizer import SpeakerFinalizationError

    media = tmp_path / "clean.mp4"
    media.write_bytes(b"different media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n结果还是聋人啊\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_media_sha256": hashlib.sha256(b"original media").hexdigest(),
                "text_final_srt_sha256": hashlib.sha256(text_srt.read_bytes()).hexdigest(),
                "overrides": [],
            }
        ),
        encoding="utf-8",
    )

    def analyzer(**_kwargs):
        return {"decisions": [{"speaker": "连线", "decision_source": "campp_audio"}]}

    with pytest.raises(SpeakerFinalizationError, match="media hash mismatch"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            override_path=overrides,
            analyzer=analyzer,
        )

    document = json.loads(overrides.read_text(encoding="utf-8"))
    document.pop("source_media_sha256")
    overrides.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SpeakerFinalizationError, match="missing source_media_sha256"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            override_path=overrides,
            analyzer=analyzer,
        )


def test_production_candidate_rejects_cross_candidate_speaker_override(tmp_path: Path) -> None:
    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nhello\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    automatic = "1\n00:00:00,000 --> 00:00:02,000\n[连线] hello\n"
    overrides = tmp_path / "overrides.json"
    overrides.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": "different_candidate",
                "source_media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "text_final_srt_sha256": hashlib.sha256(text_srt.read_bytes()).hexdigest(),
                "source_srt_sha256": hashlib.sha256(automatic.encode()).hexdigest(),
                "overrides": [
                    {
                        "source_cue": 1,
                        "expect": {
                            "start": "00:00:00,000",
                            "end": "00:00:02,000",
                            "text": "hello",
                        },
                        "authority": "wrong candidate fixture",
                        "segments": [
                            {
                                "start": "00:00:00,000",
                                "end": "00:00:02,000",
                                "speaker": HOST_SPEAKER,
                                "text": "hello",
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def analyzer(**_kwargs):
        return {
            "decisions": [{"speaker": "连线", "decision_source": "campp_audio"}],
            "context_unresolved_cues": [],
        }

    with pytest.raises(SpeakerFinalizationError, match="candidate_id mismatch"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            candidate_id="intended_candidate",
            override_path=overrides,
            analyzer=analyzer,
        )

    document = json.loads(overrides.read_text(encoding="utf-8"))
    document["candidate_id"] = "intended_candidate"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SpeakerFinalizationError, match="candidate_id is required"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work-omitted-candidate",
            override_path=overrides,
            analyzer=analyzer,
        )

def test_unanswered_ambiguous_context_blocks_production(tmp_path: Path) -> None:
    import pytest

    from src.autoslice.speaker_finalizer import SpeakerFinalizationError

    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n为什么\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()

    def incomplete_analyzer(**_kwargs):
        return {
            "decisions": [{"speaker": HOST_SPEAKER, "decision_source": "acoustic_threshold_fallback"}],
            "context_unresolved_cues": [1],
        }

    with pytest.raises(SpeakerFinalizationError, match="did not resolve ambiguous speaker cues: 1"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            analyzer=incomplete_analyzer,
        )


def _timestamp(milliseconds: int) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _singleton_incident_fixture(text: str = "我这，哈哈"):
    cues = []
    cursor = 0
    for index in range(78):
        duration = 2030 if index == 74 else 2000
        cues.append(
            TextCue(index + 1, _timestamp(cursor), _timestamp(cursor + duration), text if index == 74 else f"主播句子{index + 1}")
        )
        cursor += duration
    seed_scores = [0.67843] * 78
    seed_scores[:4] = [0.85, 0.84, 0.83, 0.82]
    seed_scores[74] = 0.33793
    host_bank_scores = {index: 0.7 for index in range(78)}
    host_bank_scores[74] = 0.28934
    audio_hashes = [hashlib.sha256(f"cue-{index}".encode()).hexdigest() for index in range(78)]
    return cues, seed_scores, host_bank_scores, audio_hashes


def test_singleton_laughter_incident_uses_whole_clip_context_as_host() -> None:
    cues, seed_scores, host_bank_scores, audio_hashes = _singleton_incident_fixture()

    def context(prompt: str) -> str:
        assert "75. [待定] 我这，哈哈" in prompt
        return json.dumps(
            {
                "labels": [
                    {
                        "n": 75,
                        "speaker": HOST_SPEAKER,
                        "confidence": 0.98,
                        "reason": "相邻两句延续主播自嘲，当前句是笑声回应",
                    }
                ]
            },
            ensure_ascii=False,
        )

    result = _resolve_singleton_outlier(
        cues=cues,
        singleton_index=74,
        seed_scores=seed_scores,
        host_bank_scores=host_bank_scores,
        clip_host_indices=[0, 1, 2, 3],
        policy={
            "single_host_median_seed_min": 0.55,
            "guest_session_similarity_max": 0.45,
            "host_session_anchor_count": 4,
        },
        cue_audio_sha256=audio_hashes,
        context_call=context,
    )

    assert result["review_required"] is False
    assert result["context_unresolved_cues"] == []
    assert result["decisions"][74]["speaker"] == HOST_SPEAKER
    assert result["decisions"][74]["decision_source"] == "whole_clip_context_singleton"
    evidence = result["singleton_evidence"][0]
    assert evidence["source_cue"] == 75
    assert evidence["zero_based_index"] == 74
    assert evidence["text"] == "我这，哈哈"
    assert evidence["duration_ms"] == 2030
    assert evidence["seed_score"] == 0.33793
    assert evidence["host_bank_score"] == 0.28934
    assert evidence["audio_sha256"] == audio_hashes[74]
    assert [row["source_cue"] for row in evidence["neighbours"]] == [74, 76]


@pytest.mark.parametrize(
    "context_speaker,confidence,reason_code",
    [
        ("连线", 0.99, "CONTEXT_GUEST"),
        ("REVIEW", 0.99, "CONTEXT_REVIEW"),
        (HOST_SPEAKER, 0.80, "CONTEXT_HOST_CONFIDENCE_LOW"),
    ],
)
def test_singleton_non_host_or_low_confidence_context_stays_review_required(
    context_speaker: str, confidence: float, reason_code: str
) -> None:
    cues, seed_scores, host_bank_scores, audio_hashes = _singleton_incident_fixture()
    result = _resolve_singleton_outlier(
        cues=cues,
        singleton_index=74,
        seed_scores=seed_scores,
        host_bank_scores=host_bank_scores,
        clip_host_indices=[0, 1, 2, 3],
        policy={
            "single_host_median_seed_min": 0.55,
            "guest_session_similarity_max": 0.45,
            "host_session_anchor_count": 4,
        },
        cue_audio_sha256=audio_hashes,
        context_call=lambda _prompt: json.dumps(
            {
                "labels": [
                    {
                        "n": 75,
                        "speaker": context_speaker,
                        "confidence": confidence,
                        "reason": "fixture",
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )

    assert result["review_required"] is True
    assert result["context_unresolved_cues"] == [75]
    assert reason_code in result["review_reason_codes"]


def test_singleton_incomplete_context_retries_then_requires_review() -> None:
    cues, seed_scores, host_bank_scores, audio_hashes = _singleton_incident_fixture()
    calls = 0

    def incomplete(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return '{"labels":[]}'

    result = _resolve_singleton_outlier(
        cues=cues,
        singleton_index=74,
        seed_scores=seed_scores,
        host_bank_scores=host_bank_scores,
        clip_host_indices=[0, 1, 2, 3],
        policy={
            "single_host_median_seed_min": 0.55,
            "guest_session_similarity_max": 0.45,
            "host_session_anchor_count": 4,
        },
        cue_audio_sha256=audio_hashes,
        context_call=incomplete,
    )

    assert calls == 3
    assert result["context_attempts"] == 3
    assert result["context_unresolved_cues"] == [75]
    assert "CONTEXT_INCOMPLETE" in result["review_reason_codes"]


def test_singleton_boolean_confidence_cannot_auto_ready() -> None:
    cues, seed_scores, host_bank_scores, audio_hashes = _singleton_incident_fixture()
    result = _resolve_singleton_outlier(
        cues=cues,
        singleton_index=74,
        seed_scores=seed_scores,
        host_bank_scores=host_bank_scores,
        clip_host_indices=[0, 1, 2, 3],
        policy={
            "single_host_median_seed_min": 0.55,
            "guest_session_similarity_max": 0.45,
            "host_session_anchor_count": 4,
        },
        cue_audio_sha256=audio_hashes,
        context_call=lambda _prompt: json.dumps(
            {
                "labels": [
                    {
                        "n": 75,
                        "speaker": HOST_SPEAKER,
                        "confidence": True,
                        "reason": "JSON bool is not a confidence score",
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )

    assert result["review_required"] is True
    assert result["context_unresolved_cues"] == [75]
    assert result["context_attempts"] == 3
    assert "CONTEXT_INCOMPLETE" in result["review_reason_codes"]
    assert all("JSON number" in error for error in result["context_errors"])


def test_single_real_guest_is_not_swallowed_by_host_majority() -> None:
    cues, seed_scores, host_bank_scores, audio_hashes = _singleton_incident_fixture("我是连线主播")
    result = _resolve_singleton_outlier(
        cues=cues,
        singleton_index=74,
        seed_scores=seed_scores,
        host_bank_scores=host_bank_scores,
        clip_host_indices=[0, 1, 2, 3],
        policy={
            "single_host_median_seed_min": 0.55,
            "guest_session_similarity_max": 0.45,
            "host_session_anchor_count": 4,
        },
        cue_audio_sha256=audio_hashes,
        context_call=lambda _prompt: json.dumps(
            {"labels": [{"n": 75, "speaker": HOST_SPEAKER, "confidence": 0.99, "reason": "host-majority trap"}]},
            ensure_ascii=False,
        ),
    )

    assert _singleton_nonlexical_dominant("我这，哈哈") is True
    assert _singleton_nonlexical_dominant("你好哈哈") is False
    assert _singleton_nonlexical_dominant("我是连线主播") is False
    assert result["review_required"] is True
    assert result["context_unresolved_cues"] == [75]
    assert "SINGLETON_LEXICAL_CONTENT" in result["review_reason_codes"]
    assert "CONTEXT_ACOUSTIC_CONFLICT" in result["review_reason_codes"]


def test_singleton_review_manifest_preserves_bound_evidence(tmp_path: Path) -> None:
    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text("1\n00:00:00,000 --> 00:00:02,030\n我这，哈哈\n", encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    evidence = {
        "source_cue": 1,
        "zero_based_index": 0,
        "start": "00:00:00,000",
        "end": "00:00:02,030",
        "text": "我这，哈哈",
        "seed_score": 0.33793,
        "host_bank_score": 0.28934,
        "audio_sha256": "a" * 64,
        "neighbours": [],
    }

    def analyzer(**_kwargs):
        return {
            "review_required": True,
            "review_reason_codes": ["CONTEXT_REVIEW"],
            "context_unresolved_cues": [1],
            "singleton_evidence": [evidence],
            "decisions": [
                {"speaker": "连线", "decision_source": "speaker_review_required_singleton"}
            ],
        }

    output_srt = tmp_path / "speaker.srt"
    output_ass = tmp_path / "speaker.ass"
    output_manifest = tmp_path / "speaker.json"
    returned = finalize_speaker_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        profile_path=profile,
        reference_dir=tmp_path / "refs",
        model_dir=tmp_path / "model",
        output_srt_path=output_srt,
        output_ass_path=output_ass,
        output_manifest_path=output_manifest,
        work_dir=tmp_path / "work",
        analyzer=analyzer,
    )

    manifest = json.loads(output_manifest.read_text(encoding="utf-8"))
    assert returned == manifest
    assert manifest["status"] == "SPEAKER_REVIEW_REQUIRED"
    assert manifest["production_ready"] is False
    assert manifest["context_unresolved_cues"] == [1]
    assert manifest["review_required_cues"] == [evidence]
    assert manifest["source_media_sha256"] == hashlib.sha256(media.read_bytes()).hexdigest()
    assert manifest["text_final_srt_sha256"] == hashlib.sha256(text_srt.read_bytes()).hexdigest()
    assert not output_srt.exists()
    assert not output_ass.exists()

    def evidence_free_analyzer(**_kwargs):
        return {
            "review_required": True,
            "context_unresolved_cues": [1],
            "singleton_evidence": [],
            "decisions": [
                {"speaker": "连线", "decision_source": "speaker_review_required_singleton"}
            ],
        }

    invalid_manifest = tmp_path / "invalid-speaker.json"
    with pytest.raises(SpeakerFinalizationError, match="evidence rows must be non-empty"):
        finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "refs",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "invalid.srt",
            output_ass_path=tmp_path / "invalid.ass",
            output_manifest_path=invalid_manifest,
            work_dir=tmp_path / "invalid-work",
            analyzer=evidence_free_analyzer,
        )
    assert not invalid_manifest.exists()


def test_generic_review_evidence_is_not_limited_to_singletons(tmp_path: Path) -> None:
    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text("1\n00:00:00,000 --> 00:00:02,030\n两个人同时说话\n", encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    evidence = {
        "source_cue": 1,
        "zero_based_index": 0,
        "start": "00:00:00,000",
        "end": "00:00:02,030",
        "text": "两个人同时说话",
        "audio_sha256": "a" * 64,
        "reason_codes": ["CUE_OVERLAPPING_SPEECH"],
    }

    manifest = finalize_speaker_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        profile_path=profile,
        reference_dir=tmp_path / "refs",
        model_dir=tmp_path / "model",
        output_srt_path=tmp_path / "speaker.srt",
        output_ass_path=tmp_path / "speaker.ass",
        output_manifest_path=tmp_path / "speaker.json",
        work_dir=tmp_path / "work",
        analyzer=lambda **_kwargs: {
            "review_required": True,
            "review_reason_codes": ["CUE_OVERLAPPING_SPEECH"],
            "context_unresolved_cues": [1],
            "review_required_cues": [evidence],
            "decisions": [
                {"speaker": "连线", "decision_source": "speaker_review_required_overlap"}
            ],
        },
    )

    assert manifest["review_required_cues"] == [evidence]
    assert manifest["review_reason_codes"] == ["CUE_OVERLAPPING_SPEECH"]
    assert "singleton" not in manifest["reason"]


def test_mixed_overlap_provider_evidence_stops_before_analyzer_and_render(tmp_path: Path) -> None:
    media = tmp_path / "clean.mp4"
    media.write_bytes(b"clean media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text("1\n00:00:00,000 --> 00:00:02,030\n两个人同时说话\n", encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "model").mkdir()
    review_audio = tmp_path / "mixed-review-cue-0001.wav"
    review_audio.write_bytes(b"actual extracted cue audio")
    evidence_path = tmp_path / "mixed-overlap.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": MIXED_OVERLAP_EVIDENCE_SCHEMA,
                "status": "REVIEW_REQUIRED",
                "source_media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "text_final_srt_sha256": hashlib.sha256(text_srt.read_bytes()).hexdigest(),
                "provider": {"name": "fixture", "config_sha256": "b" * 64},
                "review_required_cues": [
                    {
                        "source_cue": 1,
                        "zero_based_index": 0,
                        "start": "00:00:00,000",
                        "end": "00:00:02,030",
                        "text": "两个人同时说话",
                        "audio_sha256": hashlib.sha256(
                            review_audio.read_bytes()
                        ).hexdigest(),
                        "reason_codes": [
                            "CUE_MULTI_CLUSTER",
                            "CUE_OVERLAPPING_SPEECH",
                        ],
                        "provider_details": {
                            "cluster_count": 2,
                            "overlap_detected": True,
                            "audio_path": str(review_audio.resolve()),
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    analyzer_called = False

    def analyzer(**_kwargs):
        nonlocal analyzer_called
        analyzer_called = True
        raise AssertionError("mixed/overlap gate must stop before CAM++")

    output_srt = tmp_path / "speaker.srt"
    output_ass = tmp_path / "speaker.ass"
    manifest = finalize_speaker_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        profile_path=profile,
        reference_dir=tmp_path / "refs",
        model_dir=tmp_path / "model",
        output_srt_path=output_srt,
        output_ass_path=output_ass,
        output_manifest_path=tmp_path / "speaker.json",
        work_dir=tmp_path / "work",
        mixed_overlap_evidence_path=evidence_path,
        analyzer=analyzer,
    )

    assert analyzer_called is False
    assert manifest["status"] == "SPEAKER_REVIEW_REQUIRED"
    assert manifest["context_unresolved_cues"] == [1]
    assert manifest["mixed_overlap_evidence_sha256"] == hashlib.sha256(
        evidence_path.read_bytes()
    ).hexdigest()
    assert not output_srt.exists()
    assert not output_ass.exists()


def test_mixed_overlap_provider_evidence_rejects_binding_drift(tmp_path: Path) -> None:
    cues = [TextCue(1, "00:00:00,000", "00:00:02,030", "原文")]
    document = {
        "schema_version": MIXED_OVERLAP_EVIDENCE_SCHEMA,
        "status": "REVIEW_REQUIRED",
        "source_media_sha256": "a" * 64,
        "text_final_srt_sha256": "b" * 64,
        "provider": {"name": "fixture", "config_sha256": "c" * 64},
        "review_required_cues": [
            {
                "source_cue": 1,
                "zero_based_index": 0,
                "start": "00:00:00,000",
                "end": "00:00:02,030",
                "text": "被篡改的文字",
                "audio_sha256": "d" * 64,
                "reason_codes": ["CUE_MULTI_CLUSTER"],
                "provider_details": {"cluster_count": 2},
            }
        ],
    }

    with pytest.raises(SpeakerFinalizationError, match="text/timeline binding mismatch"):
        validate_mixed_overlap_evidence_document(
            document,
            expected_media_sha256="a" * 64,
            expected_text_sha256="b" * 64,
            cues=cues,
            expected_audio_root=tmp_path,
        )


@pytest.mark.parametrize("attack", ["escape", "symlink", "drift"])
def test_mixed_overlap_provider_evidence_rejects_audio_path_or_byte_attack(
    tmp_path: Path, attack: str
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    inside_audio = evidence_root / "cue.wav"
    inside_audio.write_bytes(b"sealed cue audio")
    audio_path = inside_audio
    if attack == "escape":
        audio_path = tmp_path / "outside.wav"
        audio_path.write_bytes(b"sealed cue audio")
    elif attack == "symlink":
        target = evidence_root / "target.wav"
        target.write_bytes(b"sealed cue audio")
        audio_path = evidence_root / "cue-link.wav"
        audio_path.symlink_to(target)
    digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    document = {
        "schema_version": MIXED_OVERLAP_EVIDENCE_SCHEMA,
        "status": "REVIEW_REQUIRED",
        "source_media_sha256": "a" * 64,
        "text_final_srt_sha256": "b" * 64,
        "provider": {"name": "fixture", "config_sha256": "c" * 64},
        "review_required_cues": [
            {
                "source_cue": 1,
                "zero_based_index": 0,
                "start": "00:00:00,000",
                "end": "00:00:02,030",
                "text": "原文",
                "audio_sha256": digest,
                "reason_codes": ["CUE_MULTI_CLUSTER"],
                "provider_details": {
                    "cluster_count": 2,
                    "audio_path": str(audio_path.absolute()),
                },
            }
        ],
    }
    if attack == "drift":
        inside_audio.write_bytes(b"tampered after evidence generation")

    with pytest.raises(
        SpeakerFinalizationError,
        match="escapes the evidence root|non-symlink|audio bytes drifted",
    ):
        validate_mixed_overlap_evidence_document(
            document,
            expected_media_sha256="a" * 64,
            expected_text_sha256="b" * 64,
            cues=[TextCue(1, "00:00:00,000", "00:00:02,030", "原文")],
            expected_audio_root=evidence_root,
        )


def test_review_manifest_rechecks_bound_audio_bytes(tmp_path: Path) -> None:
    audio = tmp_path / "review.wav"
    audio.write_bytes(b"review bytes")
    manifest = {
        "status": "SPEAKER_REVIEW_REQUIRED",
        "production_ready": False,
        "reason": "mixed review",
        "source_media_sha256": "a" * 64,
        "text_final_srt_sha256": "b" * 64,
        "context_unresolved_cues": [1],
        "review_required_cues": [
            {
                "source_cue": 1,
                "zero_based_index": 0,
                "start": "00:00:00,000",
                "end": "00:00:01,000",
                "text": "fixture",
                "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "provider_details": {"audio_path": str(audio.resolve())},
            }
        ],
    }
    assert validate_speaker_review_manifest_document(manifest)
    audio.write_bytes(b"tampered review bytes")
    with pytest.raises(SpeakerFinalizationError, match="audio bytes drifted"):
        validate_speaker_review_manifest_document(manifest)


def test_speaker_review_manifest_validator_rejects_unbound_rows() -> None:
    base = {
        "status": "SPEAKER_REVIEW_REQUIRED",
        "production_ready": False,
        "reason": "review",
        "source_media_sha256": "a" * 64,
        "text_final_srt_sha256": "b" * 64,
        "context_unresolved_cues": [1],
        "review_required_cues": [
            {
                "source_cue": 1,
                "zero_based_index": 0,
                "start": "00:00:00,000",
                "end": "00:00:01,000",
                "text": "fixture",
                "audio_sha256": "sha256:" + "c" * 64,
            }
        ],
    }
    assert validate_speaker_review_manifest_document(base)[0]["source_cue"] == 1

    invalid_documents = []
    for mutate in ("audio", "index", "coverage", "source_hash"):
        document = json.loads(json.dumps(base))
        if mutate == "audio":
            document["review_required_cues"][0]["audio_sha256"] = "not-a-hash"
        elif mutate == "index":
            document["review_required_cues"][0]["zero_based_index"] = 1
        elif mutate == "coverage":
            document["context_unresolved_cues"] = [2]
        else:
            document.pop("source_media_sha256")
        invalid_documents.append(document)
    for document in invalid_documents:
        with pytest.raises(SpeakerFinalizationError):
            validate_speaker_review_manifest_document(document)


def test_two_guest_anchors_keep_existing_cluster_path(tmp_path: Path, monkeypatch) -> None:
    import src.autoslice.speaker_finalizer as speaker_finalizer

    # Wave 8 F6 negative canary: the first four HOST cues are deliberately
    # shorter than short_cue_ms.  Their CAM++ margins are nevertheless strong,
    # so the higher-quality acoustic source must settle them before the lower-
    # quality whole-clip-context veto pool is built.
    cue_bounds = [
        (0, 1_000),
        (1_000, 2_000),
        (2_000, 3_000),
        (3_000, 4_000),
        (4_000, 6_000),
        (6_000, 8_000),
    ]
    cues = [
        TextCue(index + 1, _timestamp(start), _timestamp(end), f"cue {index + 1}")
        for index, (start, end) in enumerate(cue_bounds)
    ]
    cue_paths = [tmp_path / f"cue-{index}.wav" for index in range(6)]
    references = [{"id": "r1", "sha256": "a" * 64, "path": tmp_path / "ref.wav"}]

    def similarity(left: Path, right: Path) -> float:
        def cue_index(path: Path):
            return int(path.stem.split("-")[-1]) if path.stem.startswith("cue-") else None

        left_index, right_index = cue_index(left), cue_index(right)
        if left_index is None:
            return 0.9 if right_index < 4 else 0.2
        if right_index is None:
            return 0.9 if left_index < 4 else 0.2
        if left_index < 4 and right_index < 4:
            return 0.9
        if left_index >= 4 and right_index >= 4:
            return 0.8
        return 0.1

    monkeypatch.setattr(
        speaker_finalizer,
        "_load_runtime",
        lambda *_args: ({}, references, "model-hash", object()),
    )
    monkeypatch.setattr(
        speaker_finalizer,
        "_extract_cue_wavs",
        lambda *_args: (None, 16000, cue_paths),
    )
    monkeypatch.setattr(
        speaker_finalizer,
        "_build_embedding_similarity",
        lambda **_kwargs: similarity,
    )
    monkeypatch.setattr(speaker_finalizer, "_assert_runtime_assets_stable", lambda **_kwargs: None)

    result = _run_campplus_analysis(
        media_path=tmp_path / "media.mp4",
        cues=cues,
        profile_path=tmp_path / "profile.json",
        reference_dir=tmp_path / "refs",
        model_dir=tmp_path / "model",
        work_dir=tmp_path / "work",
        context_call=None,
    )

    assert result["mode"] == "multi_speaker"
    assert result["guest_anchor_groups"] == [[5, 6]]
    assert result["context_required_cues"] == []
    assert [row["speaker"] for row in result["decisions"]] == [
        HOST_SPEAKER,
        HOST_SPEAKER,
        HOST_SPEAKER,
        HOST_SPEAKER,
        "连线",
        "连线",
    ]
