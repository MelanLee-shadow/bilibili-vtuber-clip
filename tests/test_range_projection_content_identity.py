"""41: evidence identity is content, never a directory/path representation.

Guimi b4cd6dc cue69 (#8/#14): one cached acquisition was projected for two
findings whose assistance differed; both projections were addressed by the
same (request, SRT, candidate, acquisition) directory, so the second write hit
"range artifact already contains different bytes" and a valid hearing became
GEMINI_RANGE_OBSERVATION_FAILED. Projections are now content-addressed; an
identical replay still lands on the same bytes. Synthetic files, no provider.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import gemini_range_context as gemini


@pytest.fixture
def acquisition(tmp_path: Path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-media")
    run = tmp_path / "out" / "gemini-range-acquisition" / "window" / "prompt" / "run-acq1"
    run.mkdir(parents=True)
    files = {}
    for role, name, data in (
        ("audio_clip", "input.mp4", b"clip"),
        ("actual_provider_audio", "input.mp3", b"mp3"),
        ("rendered_audio_clip", "render.mp4", b"render"),
        ("prompt", "prompt.md", b"geometry-only prompt"),
        ("raw_response", "verdict.raw.json", b'{"status": "OBSERVED"}'),
        ("provider_stdout", "agy.stdout", b"ok"),
        ("execution", "execution.json", json.dumps({"status": "COMPLETED"}).encode()),
    ):
        path = run / name
        path.write_bytes(data)
        files[role] = {"path": str(path), "sha256": gemini._file_sha(path)}
    verifier = SimpleNamespace(output_dir=tmp_path / "out", source_media=source,
                               source_sha256=gemini._file_sha(source))
    metadata = {"provider": "agy", "model": "test-model", "paths": files}
    return verifier, metadata


def geometry(**changes):
    base = {"candidate_id": "candidate-1", "cue_indexes": [68, 69, 70],
            "matched_start_ms": 1_000, "matched_end_ms": 3_000, "source_offset_ms": 9_800,
            "source_target_start_ms": 10_800, "source_target_end_ms": 12_800,
            "crop_start_ms": 10_300, "crop_end_ms": 13_300,
            "context_assistance": {"sources": {"cue:68": {"text_sha256": "a" * 64}}},
            "prior_blind_ranges": [], "prior_blind_narrow": []}
    return {**base, **changes}


def project(verifier, metadata, geo):
    return gemini._materialize_current_observation(
        verifier=verifier, check_request={}, geometry=geo,
        srt_text="1\n00:00:01,000 --> 00:00:03,000\n不许跟别人说话了\n\n",
        rows=[], blind_request={"schema_version": "context-assisted-audio-request.v1"},
        prompt="geometry-only prompt", metadata=metadata,
        current_request_sha="f" * 64, served_from_cache=True)


def test_two_findings_on_one_acquisition_get_distinct_content_addressed_projections(acquisition):
    verifier, metadata = acquisition
    first, first_doc = project(verifier, metadata, geometry())
    second, second_doc = project(verifier, metadata, geometry(
        context_assistance={"sources": {"cue:70": {"text_sha256": "b" * 64}}},
        prior_blind_ranges=[{"observation_id": "c" * 64}]))

    a, b = first["artifact_bindings"], second["artifact_bindings"]
    assert a["segmented_input_path"] != b["segmented_input_path"]
    # Same acquisition bytes are still bound by both projections.
    for key in ("raw_response_sha256", "audio_clip_sha256", "prompt_sha256", "source_media_sha256"):
        assert a[key] == b[key]
    for bindings, doc in ((a, first_doc), (b, second_doc)):
        on_disk = json.loads(Path(bindings["segmented_input_path"]).read_text(encoding="utf-8"))
        assert on_disk == doc
        assert gemini._file_sha(Path(bindings["segmented_input_path"])) == bindings["segmented_input_sha256"]


def test_identical_replay_reuses_the_same_projection_bytes(acquisition):
    verifier, metadata = acquisition
    first, _ = project(verifier, metadata, geometry())
    again, _ = project(verifier, metadata, geometry())
    assert again["artifact_bindings"] == first["artifact_bindings"]


def test_tampered_projection_bytes_are_still_refused(acquisition):
    verifier, metadata = acquisition
    first, _ = project(verifier, metadata, geometry())
    Path(first["artifact_bindings"]["segmented_input_path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(OSError, match="already contains different bytes"):
        project(verifier, metadata, geometry())
