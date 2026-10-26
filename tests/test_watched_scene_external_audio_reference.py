"""A recovered source identity reaches current watched cues without a role grant."""

import copy
import json
from pathlib import Path

import pytest

from src.autoslice.producer_nested_acoustic import prepare_final_scene_acoustic_spec
from test_producer_nested_acoustic import _bare_sha, _scene_spec_fixture


def _case(tmp_path, *, captions=None):
    inputs, spec, profile, references, document = _scene_spec_fixture(tmp_path, captions=captions)
    original = tmp_path / "official-source.m4s"
    original.write_bytes(b"independent original audio")
    spec["nested_media_external_audio_reference"] = {
        "candidate_id": "candidate", "outer_media_sha256": _bare_sha(Path(inputs["outer_media_path"])),
        "independent_source_media_path": str(original), "independent_source_media_sha256": _bare_sha(original),
        "source_cache_binding": {
            "kind": "OFFICIAL_EXTERNAL_AUDIO", "source_uri": "https://official.example/video/original",
            "source_sha256": _bare_sha(original),
        },
    }
    return inputs, spec, profile, references, document


def _prepare(case):
    inputs, spec, profile, references, _ = case
    return prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], duration_ms=1000,
        profile_path=profile, reference_dir=references,
    )


@pytest.mark.parametrize("captions", [["source words"], []])
def test_source_reference_reaches_fresh_current_watched_descriptor(tmp_path, captions):
    case = _case(tmp_path, captions=captions)
    before = copy.deepcopy(case[1])
    effective, audit = _prepare(case)
    descriptor = effective["nested_media_acoustic"]["observations"][0]
    assert descriptor["source_cache_binding"] == before["nested_media_external_audio_reference"]["source_cache_binding"]
    assert descriptor["outer_media_sha256"].removeprefix("sha256:") == _bare_sha(Path(case[0]["outer_media_path"]))
    assert descriptor["final_srt_sha256"].removeprefix("sha256:") == _bare_sha(Path(case[0]["final_srt_path"]))
    assert audit["external_audio_reference"]["prepared_cue_indexes"] == [1]
    assert audit["source_role_authority"] is False
    assert audit["provider_calls"] == 0
    assert "mapping_receipt" not in descriptor and "label" not in descriptor
    assert case[1] == before


@pytest.mark.parametrize("failure", ["candidate", "outer", "bytes", "legacy_receipt", "same_file"])
def test_invalid_reference_preserves_normal_unknown_locator_route(tmp_path, failure):
    case = _case(tmp_path)
    ref = case[1]["nested_media_external_audio_reference"]
    if failure == "candidate":
        ref["candidate_id"] = "other"
    elif failure == "outer":
        ref["outer_media_sha256"] = "0" * 64
    elif failure == "bytes":
        Path(ref["independent_source_media_path"]).write_bytes(b"changed")
    elif failure == "legacy_receipt":
        ref["mapping_receipt_path"] = "old-mapping.json"
    else:
        ref["independent_source_media_path"] = case[0]["outer_media_path"]
        ref["independent_source_media_sha256"] = _bare_sha(Path(case[0]["outer_media_path"]))
        ref["source_cache_binding"]["source_sha256"] = ref["independent_source_media_sha256"]
    effective, audit = _prepare(case)
    assert audit["external_audio_reference"]["status"] == "UNKNOWN"
    assert "source_cache_binding" not in effective["nested_media_acoustic"]["observations"][0]
    assert audit["provider_calls"] == 0


def test_unknown_scene_never_receives_known_source_identity(tmp_path):
    case = _case(tmp_path)
    evidence = Path(case[1]["nested_media_caption_dedup"]["evidence_path"])
    document = case[4]
    document["scene_observations"][0].update(scene="UNKNOWN", captions=[])
    evidence.write_text(json.dumps(document, sort_keys=True))
    case[1]["nested_media_caption_dedup"]["evidence_sha256"] = _bare_sha(evidence)
    effective, audit = _prepare(case)
    assert "nested_media_acoustic" not in effective
    assert audit["descriptor_count"] == 0
    assert audit["observations"][0]["action"] == "KEEP"


def test_existing_prepared_handoff_is_never_expanded_or_rebound(tmp_path):
    case = _case(tmp_path)
    case[1]["nested_media_acoustic"] = {"observations": [{"earlier": "immutable"}]}
    effective, audit = _prepare(case)
    assert audit["status"] == "PRESERVED"
    assert effective == case[1]
