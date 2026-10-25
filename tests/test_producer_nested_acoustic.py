from __future__ import annotations

import copy
import hashlib
import json
import sys
import types
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice.producer_nested_acoustic import (
    build_runtime_nested_acoustic_observer,
    prepare_final_scene_acoustic_spec,
    prepare_missing_nested_acoustic_audio,
    observe_prepared_nested_acoustics,
    current_caption_audio_roles,
    retain_current_acoustic_observations,
    split_current_acoustic_observations_for_reuse,
    caption_audio_roles_context,
    caption_audio_roles_after_typed_text,
    _extract_pcm16_wav,
    _normalize_trailing_pcm_frames,
    _canonical_hash,
    _descriptor_binding,
    _current_cue_scope_from_descriptor,
    _prepare_automatic_voice_reference,
)
from src.autoslice.producer_nested_external_audio import (
    CATALOG_ENV,
    EXTERNAL_AUDIO_SCENE_SOURCE_ROLE,
    resolve_external_audio_reference,
)
from src.autoslice.nested_media_acoustic_observer import NestedMediaAcousticObserverError
from src.autoslice.nested_media_caption_dedup import POLICY as CAPTION_DEDUP_POLICY
from src.autoslice.nested_media_caption_dedup import SCHEMA as CAPTION_DEDUP_SCHEMA
from src.autoslice.nested_media_caption_attribution import OBSERVATION_SCHEMA
import src.autoslice.host_vocal_proof as host_vocal
from tests.test_nested_media_acoustic_observer import _inputs, _observer, _judge


def _prepared(tmp_path):
    inputs = _inputs(tmp_path)
    srt = Path(inputs["final_srt_path"])
    srt.write_text("1\n00:00:00,100 --> 00:00:00,200\nsource words\n", encoding="utf-8")
    inputs["final_srt_sha256"] = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    callbacks = SimpleNamespace(observe_audio=_observer, cpa_judge=_judge,
                                runtime_identity=inputs["runtime_identity"])
    observation = {k: v for k, v in inputs.items() if k not in {
        "candidate_id", "outer_media_path", "final_srt_path", "evidence_root", "runtime_identity"}}
    observation["visual_observation"] = {
        "schema_version": OBSERVATION_SCHEMA, "candidate_id": "candidate",
        "observation_id": inputs["observation_id"], "frame_ms": 150,
        "caption_present": True, "visible_text": "source words",
        "role": "WATCHED_MEDIA_CAPTION", "pipeline_burned_subtitle": False,
        "frame_path": str(inputs["caption_frame_path"]),
        "frame_sha256": inputs["caption_frame_sha256"],
    }
    return inputs, callbacks, {"watched_media": True, "nested_media_acoustic": {
        "schema_version": "nested-media-acoustic-inputs.v1", "observations": [observation]}}


def _run(inputs, callbacks, spec):
    return observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks,
    )


def _voice_spec(inputs, spec):
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    descriptor = dict(descriptor)
    descriptor.pop("mapping_receipt_path", None)
    descriptor.pop("mapping_receipt_sha256", None)
    descriptor["observer_mode"] = "VOICE_REFERENCE_ONLY_UNALIGNED"
    binding_path = Path(inputs["evidence_root"]) / "voice-reference-binding.json"
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding = {
        "schema_version": "nested-media-voice-reference-binding.v1",
        "status": "REFERENCE_BOUND",
        "mode": "VOICE_REFERENCE_ONLY_UNALIGNED",
        "candidate_id": inputs["candidate_id"],
        "observation_id": descriptor["observation_id"],
        "outer_media": {"path": str(Path(inputs["outer_media_path"]).resolve()), "sha256": inputs["outer_media_sha256"]},
        "final_srt": {"path": str(Path(inputs["final_srt_path"]).resolve()), "sha256": inputs["final_srt_sha256"]},
        "caption_frame": {"path": str(Path(inputs["caption_frame_path"]).resolve()), "sha256": inputs["caption_frame_sha256"], "frame_ms": inputs["caption_frame_ms"]},
        "source_media": {"path": str(Path(inputs["independent_source_media_path"]).resolve()), "sha256": inputs["independent_source_media_sha256"]},
        "host_enrollment": {"path": str(Path(inputs["host_enrollment_path"]).resolve()), "sha256": inputs["host_enrollment_sha256"]},
        "source_audio": {"path": str(Path(inputs["source_audio_path"]).resolve()), "sha256": inputs["source_audio_sha256"]},
        "target_audio": {"path": str(Path(inputs["target_audio_path"]).resolve()), "sha256": inputs["target_audio_sha256"]},
        "bvid": "BVREFERENCE123", "cid": "987654",
        "outer_context_interval_ms": descriptor["interval_ms"],
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
    descriptor.update(
        target_interval_ms=[100, 200],
        target_cue_indexes=[1],
        voice_reference_binding_path=str(binding_path),
        voice_reference_binding_sha256="sha256:" + hashlib.sha256(binding_path.read_bytes()).hexdigest(),
    )
    return {"watched_media": True, "nested_media_acoustic": {
        "schema_version": "nested-media-acoustic-inputs.v1", "observations": [descriptor]}}


def _automatic_external_voice(inputs, spec, monkeypatch):
    descriptor = dict(spec["nested_media_acoustic"]["observations"][0])
    for field in ("mapping_receipt_path", "mapping_receipt_sha256",
                  "source_audio_path", "source_audio_sha256", "target_audio_path", "target_audio_sha256"):
        descriptor.pop(field, None)
    cache = {
        "independent_source_media_path": str(inputs["independent_source_media_path"]),
        "independent_source_media_sha256": inputs["independent_source_media_sha256"],
        "source_cache_binding": {
            "kind": "OFFICIAL_EXTERNAL_AUDIO", "source_uri": "https://example.org/original.mp3",
            "source_sha256": inputs["independent_source_media_sha256"],
        },
    }
    calls = _fake_voice_reference_tools(monkeypatch)
    kwargs = dict(
        descriptor=descriptor, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"] / "automatic",
        media_sha=inputs["outer_media_sha256"], srt_sha=inputs["final_srt_sha256"],
        source_cache=cache, context_interval=descriptor["interval_ms"],
        target_interval=[100, 200], target_cues=[1],
    )
    return kwargs, calls


def test_automatic_external_voice_candidate_prepares_and_replays_without_mapping(tmp_path, monkeypatch):
    inputs, callbacks, spec = _prepared(tmp_path)
    kwargs, calls = _automatic_external_voice(inputs, spec, monkeypatch)
    kwargs["descriptor"]["playback_locator"] = {
        "schema_version": "short-reference-playback-locator.v1",
        "status": "LOCATED", "authority": "EVIDENCE_ONLY",
        "time_alignment_performed": False,
    }
    original = copy.deepcopy(kwargs["descriptor"])
    descriptor, prepared = _prepare_automatic_voice_reference(**kwargs)
    assert "playback_locator" not in descriptor
    assert descriptor["target_audio_window_ms"] == descriptor["target_interval_ms"]
    binding = json.loads(Path(prepared["binding_path"]).read_text())
    assert binding["source_identity"] == kwargs["source_cache"]["source_cache_binding"]
    assert "bvid" not in binding and "cid" not in binding
    assert binding["time_alignment_performed"] is False
    assert binding["source_role_authority"] is False
    assert descriptor["target_audio_offset_ms"] == [0, 100]
    assert binding["target_audio_window_ms"] == [100, 200]
    assert binding["target_audio_offset_ms"] == [0, 100]
    before = sum(argv[0] == "ffmpeg" for argv in calls)
    replayed, replay = _prepare_automatic_voice_reference(**kwargs)
    assert replay["status"] == "REPLAYED" and replayed == descriptor
    assert sum(argv[0] == "ffmpeg" for argv in calls) == before
    normal = {"watched_media": False, "nested_media_acoustic": {
        "schema_version": "nested-media-acoustic-inputs.v1", "observations": [descriptor]}}
    audit = _run(inputs, _role_callbacks(callbacks, "source_media"), normal)
    roles = current_caption_audio_roles(candidate_id="candidate", source_media=inputs["outer_media_path"],
                                       subtitle_path=inputs["final_srt_path"], acoustic_audit=audit)
    assert roles["rows"][0]["role"] == "source_media"
    assert roles["rows"][0]["action"] == "KEEP"
    assert kwargs["descriptor"] == original


def test_automatic_voice_preparation_preserves_context_pcm_and_target_offset(
    tmp_path, monkeypatch,
):
    inputs, _callbacks, spec = _prepared(tmp_path)
    kwargs, calls = _automatic_external_voice(inputs, spec, monkeypatch)
    kwargs["target_interval"] = [120, 180]

    descriptor, prepared = _prepare_automatic_voice_reference(**kwargs)

    assert descriptor["target_interval_ms"] == [120, 180]
    assert descriptor["target_audio_window_ms"] == [100, 200]
    assert descriptor["target_audio_offset_ms"] == [20, 80]
    binding = json.loads(Path(prepared["binding_path"]).read_text())
    assert binding["target_interval_ms"] == [120, 180]
    assert binding["target_audio_window_ms"] == [100, 200]
    assert binding["target_audio_offset_ms"] == [20, 80]

    manifest = json.loads(Path(prepared["preparation_path"]).read_text())
    assert manifest["input_binding"]["target_audio_window_ms"] == [100, 200]
    assert manifest["input_binding"]["target_audio_offset_ms"] == [20, 80]
    assert manifest["target_audio"]["window_ms"] == [100, 200]
    assert manifest["target_audio"]["target_interval_ms"] == [120, 180]
    assert manifest["target_audio"]["target_audio_offset_ms"] == [20, 80]
    old_narrow_request = copy.deepcopy(manifest["input_binding"])
    old_narrow_request["target_window_ms"] = [120, 180]
    old_narrow_request.pop("target_audio_window_ms", None)
    old_narrow_request.pop("target_audio_offset_ms", None)
    assert manifest["request_sha256"] != _canonical_hash(old_narrow_request)
    target_calls = [argv for argv in calls if argv[0] == "ffmpeg"]
    assert len(target_calls) == 2
    assert target_calls[-1][target_calls[-1].index("-ss") + 1] == "0.100000"
    assert target_calls[-1][target_calls[-1].index("-t") + 1] == "0.110000"
    assert "playback_locator" not in descriptor


def test_automatic_voice_locator_uses_context_pcm_but_exact_target_scope(
    tmp_path, monkeypatch,
):
    inputs, _callbacks, spec = _prepared(tmp_path)
    kwargs, _calls = _automatic_external_voice(inputs, spec, monkeypatch)
    kwargs["target_interval"] = [120, 180]
    seen = {}

    def locate(**locator_kwargs):
        seen.update(locator_kwargs)
        from src.autoslice.short_reference_playback_locator import (
            ShortReferencePlaybackLocatorError,
        )
        raise ShortReferencePlaybackLocatorError("LOCATOR_WEAK_MATCH")

    monkeypatch.setattr(
        "src.autoslice.producer_nested_acoustic.locate_short_reference", locate,
    )
    descriptor, _prepared_result = _prepare_automatic_voice_reference(**kwargs)

    assert seen["target_audio_window_ms"] == [100, 200]
    assert seen["target_interval_ms"] == [120, 180]
    assert "playback_locator" not in descriptor


def test_short_reference_new_evidence_preserves_pre_locator_preparation(tmp_path, monkeypatch):
    import src.autoslice.producer_nested_acoustic as module

    inputs, callbacks, spec = _prepared(tmp_path)
    kwargs, _calls = _automatic_external_voice(inputs, spec, monkeypatch)
    calls = _fake_voice_reference_tools(monkeypatch, source_duration=0.04)
    # Reproduce the existing cache without an algorithm identity.
    monkeypatch.setattr(module, "SHORT_REFERENCE_LOCATOR_MAX_MS", 0)
    _old_descriptor, old = _prepare_automatic_voice_reference(**kwargs)
    old_files = {p: p.read_bytes() for p in Path(old["binding_path"]).parent.iterdir() if p.is_file()}
    monkeypatch.setattr(module, "SHORT_REFERENCE_LOCATOR_MAX_MS", 8000)
    _new_descriptor, new = _prepare_automatic_voice_reference(**kwargs)

    assert new["binding_path"] != old["binding_path"]
    assert all(p.read_bytes() == content for p, content in old_files.items())
    before = sum(argv[0] == "ffmpeg" for argv in calls)
    _replayed, replay = _prepare_automatic_voice_reference(**kwargs)
    assert replay["status"] == "REPLAYED"
    assert replay["binding_path"] == new["binding_path"]
    assert sum(argv[0] == "ffmpeg" for argv in calls) == before


def test_short_reference_duration_preserves_fractional_source_tail(tmp_path, monkeypatch):
    from src.autoslice.producer_nested_acoustic import _probe_media_duration_ms

    _fake_voice_reference_tools(monkeypatch, source_duration=0.835938)
    assert _probe_media_duration_ms(tmp_path / "source.mp3") == 835.938


@pytest.mark.parametrize("scene_route", ["explicit_not_watched", "observed_external_audio"])
def test_unprepared_external_seed_automatically_prepares_unaligned_voice_reference(
    tmp_path, monkeypatch, scene_route,
):
    inputs, callbacks, spec = _prepared(tmp_path)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    for field in (
        "mapping_receipt_path", "mapping_receipt_sha256",
        "source_audio_path", "source_audio_sha256",
        "target_audio_path", "target_audio_sha256",
        "voice_reference_binding_path", "voice_reference_binding_sha256",
        "observer_mode",
    ):
        descriptor.pop(field, None)
    descriptor["source_cache_binding"] = {
        "kind": "OFFICIAL_EXTERNAL_AUDIO",
        "source_uri": "https://example.org/original.mp3",
        "source_sha256": inputs["independent_source_media_sha256"],
    }
    if scene_route == "explicit_not_watched":
        spec["watched_media"] = False
    else:
        spec.pop("watched_media")
        spec["nested_media_external_audio_scene"] = True
    original = copy.deepcopy(spec)
    calls = _fake_voice_reference_tools(monkeypatch, source_duration=8.0)

    audit = _run(inputs, _role_callbacks(callbacks, "source_media"), spec)

    row = audit["observations"][0]
    assert row["voice_reference_mode"] == "VOICE_REFERENCE_ONLY_UNALIGNED"
    assert row["voice_reference_fallback"]["status"] == "PREPARED"
    assert row["audio_preparation"]["status"] == "PREPARED"
    assert row["observer_result"]["status"] == "PASS"
    assert row["action"] == "KEEP"
    assert len([argv for argv in calls if argv[0] == "ffmpeg"]) == 2
    assert spec == original


def test_mixed_prepared_voice_and_external_seed_batch_is_allowed(tmp_path, monkeypatch):
    inputs, callbacks, spec = _prepared(tmp_path)
    voice_spec = _voice_spec(inputs, spec)
    voice_spec["watched_media"] = False
    external = copy.deepcopy(spec["nested_media_acoustic"]["observations"][0])
    external["observation_id"] = "external-seed"
    external["visual_observation"] = {
        **external["visual_observation"], "observation_id": "external-seed",
    }
    for field in (
        "mapping_receipt_path", "mapping_receipt_sha256",
        "source_audio_path", "source_audio_sha256",
        "target_audio_path", "target_audio_sha256",
        "voice_reference_binding_path", "voice_reference_binding_sha256",
        "observer_mode",
    ):
        external.pop(field, None)
    external["source_cache_binding"] = {
        "kind": "OFFICIAL_EXTERNAL_AUDIO",
        "source_uri": "https://example.org/original.mp3",
        "source_sha256": inputs["independent_source_media_sha256"],
    }
    voice_spec["nested_media_acoustic"]["observations"].append(external)
    calls = _fake_voice_reference_tools(monkeypatch, source_duration=8.0)

    audit = _run(inputs, _role_callbacks(callbacks, "source_media"), voice_spec)

    assert audit.get("reason_code") is None
    assert len(audit["observations"]) == 2
    assert audit["observations"][0]["voice_reference_mode"] == "VOICE_REFERENCE_ONLY_UNALIGNED"
    assert audit["observations"][1]["voice_reference_mode"] == "VOICE_REFERENCE_ONLY_UNALIGNED"
    assert all(row["observer_result"]["status"] == "PASS" for row in audit["observations"])
    assert len([argv for argv in calls if argv[0] == "ffmpeg"]) == 2


@pytest.mark.parametrize("case", ["missing_uri", "mismatched_sha", "partial_audio"])
def test_partial_external_seed_blocks_without_mapping_or_provider(tmp_path, case):
    inputs, _callbacks, spec = _prepared(tmp_path)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    for field in (
        "mapping_receipt_path", "mapping_receipt_sha256",
        "source_audio_path", "source_audio_sha256",
        "target_audio_path", "target_audio_sha256",
        "voice_reference_binding_path", "voice_reference_binding_sha256",
        "observer_mode",
    ):
        descriptor.pop(field, None)
    descriptor["source_cache_binding"] = {
        "kind": "OFFICIAL_EXTERNAL_AUDIO",
        "source_uri": "https://example.org/original.mp3",
        "source_sha256": inputs["independent_source_media_sha256"],
    }
    if case == "missing_uri":
        descriptor["source_cache_binding"].pop("source_uri")
    elif case == "mismatched_sha":
        descriptor["source_cache_binding"]["source_sha256"] = "0" * 64
    else:
        descriptor["source_audio_path"] = str(inputs["source_audio_path"])
    spec["watched_media"] = False

    callbacks = SimpleNamespace(
        observe_audio=lambda *_args, **_kwargs: pytest.fail("invalid external seed dispatched"),
        cpa_judge=lambda *_args, **_kwargs: pytest.fail("invalid external seed dispatched"),
        runtime_identity=inputs["runtime_identity"],
    )
    audit = _run(inputs, callbacks, spec)

    assert audit["reason_code"] == "VOICE_REFERENCE_SOURCE_IDENTITY_INVALID"
    assert audit["observations"] == []
    assert audit["provider_calls"] == 0


@pytest.mark.parametrize("field,value", [
    ("source_uri", "https://example.org/different.mp3"), ("source_sha256", "0" * 64),
    ("kind", None),
])
def test_automatic_external_voice_cache_identity_drift_does_not_dispatch(tmp_path, monkeypatch, field, value):
    inputs, callbacks, spec = _prepared(tmp_path)
    kwargs, _calls = _automatic_external_voice(inputs, spec, monkeypatch)
    descriptor, _prepared_result = _prepare_automatic_voice_reference(**kwargs)
    descriptor["source_cache_binding"][field] = value
    callbacks = SimpleNamespace(observe_audio=lambda *_args, **_kwargs: pytest.fail("drift dispatched"),
                                cpa_judge=lambda *_args, **_kwargs: pytest.fail("drift dispatched"),
                                runtime_identity=inputs["runtime_identity"])
    normal = {"watched_media": False, "nested_media_acoustic": {
        "schema_version": "nested-media-acoustic-inputs.v1", "observations": [descriptor]}}
    audit = _run(inputs, callbacks, normal)
    assert audit["observations"][0]["action"] == "KEEP"
    assert audit["observations"][0]["reason_code"] == "SOURCE_CACHE_VOICE_IDENTITY_MISMATCH"
    assert audit["provider_calls"] == 0


@pytest.mark.parametrize("role", ["source_media", "host", "overlap"])
def test_current_roles_reread_normal_receipt_without_provider_or_text_mutation(tmp_path, role):
    inputs, callbacks, spec = _prepared(tmp_path)
    audit = _run(inputs, _role_callbacks(callbacks, role), spec)
    before = inputs["final_srt_path"].read_bytes()
    roles = current_caption_audio_roles(candidate_id="candidate", source_media=inputs["outer_media_path"],
                                        subtitle_path=inputs["final_srt_path"], acoustic_audit=audit)
    assert roles["unavailable_observations"] == []
    assert roles["rows"][0]["role"] == role
    assert roles["rows"][0]["action"] == "KEEP"
    assert roles["uniform_host_display_only"] is True
    assert inputs["final_srt_path"].read_bytes() == before
    assert role in caption_audio_roles_context(before.decode(), roles)


@pytest.mark.parametrize("drift", [None, "srt", "frame", "pcm", "receipt"])
def test_normal_audit_continuation_revalidates_prior_receipts(tmp_path, drift):
    inputs, callbacks, spec = _prepared(tmp_path)
    previous = _run(inputs, callbacks, spec)
    original = copy.deepcopy(previous)
    if drift:
        paths = {"srt": inputs["final_srt_path"], "frame": inputs["caption_frame_path"],
                 "pcm": inputs["target_audio_path"],
                 "receipt": previous["observations"][0]["observer_result"]["receipt_binding"]["path"]}
        path = Path(paths[drift])
        path.write_bytes(path.read_bytes() + b"\n")
    before = inputs["final_srt_path"].read_bytes()
    current = {"observations": [], "provider_calls": 0, "cpa_calls": 0}
    combined = retain_current_acoustic_observations(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], previous_audit=previous,
        current_audit=current,
    )
    assert combined["retained_current_observation_count"] == (0 if drift else 1)
    roles = current_caption_audio_roles(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], acoustic_audit=combined,
    )
    assert roles["rows"][0]["role"] == ("unknown" if drift else "source_media")
    assert combined["provider_calls"] == combined["cpa_calls"] == 0
    assert previous == original and current["observations"] == []
    assert inputs["final_srt_path"].read_bytes() == before
    if not drift:
        replay = retain_current_acoustic_observations(
            candidate_id="candidate", source_media=inputs["outer_media_path"],
            subtitle_path=inputs["final_srt_path"], previous_audit=previous,
            current_audit=combined,
        )
        assert len(replay["observations"]) == 1
        assert replay["retained_current_observation_count"] == 0


@pytest.mark.parametrize("drift", [None, "source", "frame", "target", "srt", "host", "partial_audio", "extra", "transport", "receipt", "pcm"])
def test_current_external_seed_reuses_only_current_validated_voice_receipt(tmp_path, monkeypatch, drift):
    inputs, callbacks, spec = _prepared(tmp_path)
    kwargs, _calls = _automatic_external_voice(inputs, spec, monkeypatch)
    descriptor, _prepared_audio = _prepare_automatic_voice_reference(**kwargs)
    normal = {"watched_media": True, "nested_media_acoustic": {
        "schema_version": "nested-media-acoustic-inputs.v1", "observations": [descriptor]}}
    previous = _run(inputs, _role_callbacks(callbacks, "source_media"), normal)
    assert previous["observations"][0]["observer_result"]["status"] == "PASS"
    derived = {"observer_mode", "source_audio_path", "source_audio_sha256",
               "target_audio_path", "target_audio_sha256", "target_audio_window_ms",
               "target_audio_offset_ms", "voice_reference_binding_path", "voice_reference_binding_sha256"}
    seed = {key: copy.deepcopy(value) for key, value in descriptor.items() if key not in derived}
    if drift in {"source", "frame", "srt", "host"}:
        field = {"source": "independent_source_media_sha256", "frame": "caption_frame_sha256",
                 "srt": "final_srt_sha256", "host": "host_enrollment_sha256"}[drift]
        seed[field] = "0" * 64
    elif drift == "target":
        seed["target_interval_ms"] = [101, 200]
    elif drift == "partial_audio":
        seed["target_audio_path"] = descriptor["target_audio_path"]
    elif drift == "extra":
        seed["unmatched_declared_input"] = True
    elif drift == "receipt":
        Path(previous["observations"][0]["observer_result"]["receipt_binding"]["path"]).write_text("{}")
    elif drift == "pcm":
        Path(descriptor["target_audio_path"]).write_bytes(b"changed")
    current = {"watched_media": True, "nested_media_acoustic": {
        "schema_version": "nested-media-acoustic-inputs.v1", "observations": [seed]}}
    if drift == "transport":
        current["nested_media_acoustic"]["observer_transport"] = "agy_three_part"
    before = copy.deepcopy((previous, current))
    reused = split_current_acoustic_observations_for_reuse(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], previous_audit=previous,
        current_observations=[seed], current_spec=current,
        current_prepared=current["nested_media_acoustic"],
    )
    assert reused["reused_count"] == (1 if drift is None else 0)
    assert reused["unmatched_observations"] == ([] if drift is None else [seed])
    assert (previous, current) == before
    if drift is None:
        assert reused["reused_observations"] == previous["observations"]
        roles = current_caption_audio_roles(candidate_id="candidate", source_media=inputs["outer_media_path"],
                                           subtitle_path=inputs["final_srt_path"], acoustic_audit=previous)
        assert roles["rows"][0]["role"] == "source_media"
        assert roles["rows"][0]["action"] == "KEEP"


def test_current_observation_reuse_requires_valid_receipt_and_byte_identical_descriptor(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    original_current_descriptor = copy.deepcopy(
        spec["nested_media_acoustic"]["observations"][0]
    )
    previous = _run(inputs, callbacks, spec)
    previous_before = copy.deepcopy(previous)
    descriptor = previous["observations"][0]["validated_inputs"]

    reused = split_current_acoustic_observations_for_reuse(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], previous_audit=previous,
        current_observations=[descriptor], current_spec=spec,
        current_prepared=spec["nested_media_acoustic"],
    )
    assert reused["reused_count"] == 1
    assert reused["validated_previous_receipt_count"] == 1
    assert reused["unmatched_observations"] == []

    drifted = copy.deepcopy(descriptor)
    drifted["target_interval_ms"] = [101, 200]
    rejected = split_current_acoustic_observations_for_reuse(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], previous_audit=previous,
        current_observations=[drifted], current_spec=spec,
        current_prepared=spec["nested_media_acoustic"],
    )
    assert rejected["reused_count"] == 0
    assert rejected["unmatched_observations"] == [drifted]
    assert previous == previous_before
    assert spec["nested_media_acoustic"]["observations"][0] == original_current_descriptor


@pytest.mark.parametrize("watched_media", [True, False])
def test_voice_reference_current_roles_keep_without_caption_attribution(tmp_path, monkeypatch, watched_media):
    inputs, callbacks, spec = _prepared(tmp_path)
    voice_spec = _voice_spec(inputs, spec)
    voice_spec["watched_media"] = watched_media
    audit = _run(inputs, callbacks, voice_spec)
    assert audit["observations"][0]["observer_result"]["status"] == "PASS"

    assert audit["observations"][0]["action"] == "KEEP"
    attribution = audit["observations"][0]["attribution"]
    assert attribution["safe_to_drop_cue_indexes"] == []
    assert attribution["rows"][0]["acoustic_receipt"] is None

    def forbidden_attribution(*_args, **_kwargs):
        raise AssertionError("voice-reference receipts must not enter caption attribution")

    monkeypatch.setattr(
        "src.autoslice.producer_nested_acoustic.build_caption_source_attribution",
        forbidden_attribution,
    )
    roles = current_caption_audio_roles(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], acoustic_audit=audit,
    )
    assert roles["unavailable_observations"] == []
    assert roles["rows"][0]["role"] == "source_media"
    assert roles["rows"][0]["action"] == "KEEP"
    assert roles["uniform_host_display_only"] is True


@pytest.mark.parametrize("external_scene", [False, True])
def test_voice_reference_still_requires_explicit_scene_declaration(tmp_path, external_scene):
    inputs, callbacks, spec = _prepared(tmp_path)
    voice_spec = _voice_spec(inputs, spec)
    voice_spec.pop("watched_media")
    if external_scene:
        voice_spec["nested_media_external_audio_scene"] = True
    audit = _run(inputs, callbacks, voice_spec)
    assert audit["reason_code"] == "WATCHED_MEDIA_DECLARATION_REQUIRED"
    assert audit["provider_calls"] == audit["cpa_calls"] == 0


@pytest.mark.parametrize("change", ["other_cue", "target_cue", "source", "pcm"])
def test_typed_voice_context_replays_suffix_but_keeps_receipts_immutable(tmp_path, monkeypatch, change):
    inputs, callbacks, spec = _prepared(tmp_path)
    original = inputs["final_srt_path"]
    original.write_text(original.read_text() + "\n2\n00:00:00,300 --> 00:00:00,400\nold other words\n")
    inputs["final_srt_sha256"] = "sha256:" + hashlib.sha256(original.read_bytes()).hexdigest()
    spec["nested_media_acoustic"]["observations"][0]["final_srt_sha256"] = inputs["final_srt_sha256"]
    audit = _run(inputs, callbacks, _voice_spec(inputs, spec))
    assert audit["observations"][0]["observer_result"]["status"] == "PASS"
    before = original.read_text()
    after = before.replace("old other words", "new other words")
    seen = []
    def replay(authority, **kwargs):
        seen.append(kwargs)
        assert kwargs["reference_text"] == before
        assert kwargs["current_text"] == after
        assert kwargs["current_sha"] == hashlib.sha256(after.encode()).hexdigest()
    monkeypatch.setattr("src.autoslice.producer_text_pipeline._continuation_replay_clock_text_chain", replay)
    if change == "target_cue":
        after = after.replace("source words", "changed target")
    elif change == "source":
        inputs["outer_media_path"].write_bytes(inputs["outer_media_path"].read_bytes() + b"x")
    elif change == "pcm":
        inputs["target_audio_path"].write_bytes(inputs["target_audio_path"].read_bytes() + b"x")
    snapshot = copy.deepcopy(audit)
    roles = caption_audio_roles_after_typed_text(
        candidate_id="candidate", source_media=inputs["outer_media_path"], srt_text=after,
        acoustic_audit=audit, text_authority={"exact_final_cpa_self_heal": {}},
    )
    assert len(seen) == 1
    assert roles["rows"][0]["role"] == ("source_media" if change in {"other_cue", "target_cue"} else "unknown")
    assert roles["rows"][1]["role"] == "unknown"
    assert all(row["action"] == "KEEP" for row in roles["rows"])
    assert roles["mutation_authorized"] is False
    assert roles["final_srt_sha256"] == "sha256:" + hashlib.sha256(after.encode()).hexdigest()
    assert roles["typed_text_binding"] == {
        "reference_final_srt_sha256": [inputs["final_srt_sha256"]],
        "current_final_srt_sha256": roles["final_srt_sha256"],
        "reference_receipts_immutable": True,
        "chain_validation": "CONTINUATION_REPLAY_VALIDATED",
    }
    assert caption_audio_roles_context(after, roles)
    assert original.read_text() == before and audit == snapshot


def test_typed_normal_context_carries_text_only_role_from_original_request(tmp_path, monkeypatch):
    inputs, callbacks, spec = _prepared(tmp_path)
    audit = _run(inputs, _role_callbacks(callbacks, "source_media"), spec)
    original = inputs["final_srt_path"]
    before = original.read_text()
    after = before.replace("source words", "repaired source words")
    seen = []

    def replay(authority, **kwargs):
        seen.append(kwargs)
        assert kwargs["reference_text"] == before
        assert kwargs["current_text"] == after

    monkeypatch.setattr(
        "src.autoslice.producer_text_pipeline._continuation_replay_clock_text_chain",
        replay,
    )
    snapshot = copy.deepcopy(audit)
    roles = caption_audio_roles_after_typed_text(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        srt_text=after, acoustic_audit=audit,
        text_authority={"exact_final_cpa_self_heal": {}},
    )

    assert len(seen) == 1
    assert roles["rows"] == [{
        "cue_index": 1, "interval_ms": [100, 200], "role": "source_media",
        "action": "KEEP", "receipts": roles["rows"][0]["receipts"],
    }]
    assert roles["unavailable_observations"] == []
    assert roles["typed_text_binding"] == {
        "reference_final_srt_sha256": [inputs["final_srt_sha256"]],
        "current_final_srt_sha256": roles["final_srt_sha256"],
        "reference_receipts_immutable": True,
        "chain_validation": "CONTINUATION_REPLAY_VALIDATED",
    }
    assert original.read_text() == before and audit == snapshot


def test_typed_normal_context_does_not_inherit_role_to_deleted_caption(tmp_path, monkeypatch):
    inputs, callbacks, spec = _prepared(tmp_path)
    original = inputs["final_srt_path"]
    original.write_text(original.read_text() + "\n2\n00:00:00,300 --> 00:00:00,400\nother words\n")
    inputs["final_srt_sha256"] = "sha256:" + hashlib.sha256(original.read_bytes()).hexdigest()
    spec["nested_media_acoustic"]["observations"][0]["final_srt_sha256"] = inputs["final_srt_sha256"]
    audit = _run(inputs, _role_callbacks(callbacks, "source_media"), spec)
    after = "2\n00:00:00,300 --> 00:00:00,400\nother words\n"
    monkeypatch.setattr(
        "src.autoslice.producer_text_pipeline._continuation_replay_clock_text_chain",
        lambda *_args, **_kwargs: None,
    )

    roles = caption_audio_roles_after_typed_text(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        srt_text=after, acoustic_audit=audit, text_authority={},
    )

    assert roles["rows"] == [{
        "cue_index": 2, "interval_ms": [300, 400], "role": "unknown",
        "action": "KEEP", "receipts": [],
    }]
    assert roles["unavailable_observations"] == []


@pytest.mark.parametrize("drift", ["timing", "source"])
def test_typed_normal_context_rejects_audio_or_clock_drift(tmp_path, monkeypatch, drift):
    inputs, callbacks, spec = _prepared(tmp_path)
    audit = _run(inputs, _role_callbacks(callbacks, "source_media"), spec)
    original = inputs["final_srt_path"]
    before = original.read_text()
    after = before.replace("source words", "repaired source words")
    if drift == "timing":
        after = after.replace("00:00:00,100", "00:00:00,101")
    else:
        inputs["outer_media_path"].write_bytes(inputs["outer_media_path"].read_bytes() + b"drift")

    monkeypatch.setattr(
        "src.autoslice.producer_text_pipeline._continuation_replay_clock_text_chain",
        lambda *_args, **_kwargs: None,
    )
    roles = caption_audio_roles_after_typed_text(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        srt_text=after, acoustic_audit=audit,
        text_authority={"exact_final_cpa_self_heal": {}},
    )

    assert roles["rows"][0]["role"] == "unknown"
    assert roles["rows"][0]["action"] == "KEEP"
    if drift == "source":
        assert roles["unavailable_observations"][0]["reason_code"] == "OUTER_MEDIA_HASH_MISMATCH"
    else:
        assert roles["unavailable_observations"] == []


def test_typed_voice_context_rejects_unproved_suffix_without_provider(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    audit = _run(inputs, callbacks, _voice_spec(inputs, spec))
    after = inputs["final_srt_path"].read_text().replace("source words", "unproved words")
    roles = caption_audio_roles_after_typed_text(
        candidate_id="candidate", source_media=inputs["outer_media_path"], srt_text=after,
        acoustic_audit=audit, text_authority={},
    )
    assert roles["rows"][0]["role"] == "unknown"
    assert roles["unavailable_observations"][0]["reason_code"] == "CURRENT_SOURCE_CLOCK_TEXT_CHAIN_MISSING"


def test_nonvideo_voice_reference_does_not_admit_aligned_requests(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    voice_spec = _voice_spec(inputs, spec)
    voice_spec["watched_media"] = False
    voice_spec["nested_media_acoustic"]["observations"].append(
        spec["nested_media_acoustic"]["observations"][0]
    )
    audit = _run(inputs, callbacks, voice_spec)
    assert audit["reason_code"] == "WATCHED_MEDIA_DECLARATION_REQUIRED"
    assert audit["provider_calls"] == audit["cpa_calls"] == 0


def test_voice_reference_current_roles_keep_for_byte_identical_relocated_outer_and_srt(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path / "original")
    voice_spec = _voice_spec(inputs, spec)
    audit = _run(inputs, callbacks, voice_spec)
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    current_outer = relocated / "outer.mp4"
    current_srt = relocated / "final.srt"
    current_outer.write_bytes(inputs["outer_media_path"].read_bytes())
    current_srt.write_bytes(inputs["final_srt_path"].read_bytes())

    roles = current_caption_audio_roles(
        candidate_id="candidate", source_media=current_outer,
        subtitle_path=current_srt, acoustic_audit=audit,
    )

    assert roles["unavailable_observations"] == []
    assert roles["source_media_sha256"] == inputs["outer_media_sha256"]
    assert roles["final_srt_sha256"] == inputs["final_srt_sha256"]
    assert roles["rows"][0]["role"] == "source_media"
    assert roles["rows"][0]["action"] == "KEEP"


@pytest.mark.parametrize("drift", ["outer", "srt"])
def test_voice_reference_current_roles_reject_one_byte_relocated_drift(tmp_path, drift):
    inputs, callbacks, spec = _prepared(tmp_path / "original")
    voice_spec = _voice_spec(inputs, spec)
    audit = _run(inputs, callbacks, voice_spec)
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    current_outer = relocated / "outer.mp4"
    current_srt = relocated / "final.srt"
    current_outer.write_bytes(inputs["outer_media_path"].read_bytes())
    current_srt.write_bytes(inputs["final_srt_path"].read_bytes())
    if drift == "outer":
        current_outer.write_bytes(current_outer.read_bytes() + b"x")
        expected_reason = "OUTER_MEDIA_HASH_MISMATCH"
    else:
        current_srt.write_bytes(current_srt.read_bytes().replace(b"source words", b"source wordx"))
        expected_reason = "FINAL_SRT_HASH_MISMATCH"

    roles = current_caption_audio_roles(
        candidate_id="candidate", source_media=current_outer,
        subtitle_path=current_srt, acoustic_audit=audit,
    )

    assert roles["rows"][0]["role"] == "unknown"
    assert roles["rows"][0]["action"] == "KEEP"
    assert roles["unavailable_observations"][0]["reason_code"] == expected_reason


@pytest.mark.parametrize("tamper", ["binding", "pcm"])
def test_voice_reference_current_roles_reject_original_binding_or_pcm_drift(tmp_path, tamper):
    inputs, callbacks, spec = _prepared(tmp_path)
    voice_spec = _voice_spec(inputs, spec)
    audit = _run(inputs, callbacks, voice_spec)
    descriptor = audit["observations"][0]["validated_inputs"]
    if tamper == "binding":
        binding_path = Path(descriptor["voice_reference_binding_path"])
        binding_path.write_bytes(binding_path.read_bytes() + b"x")
    else:
        target_audio = Path(descriptor["target_audio_path"])
        target_audio.write_bytes(target_audio.read_bytes() + b"x")

    roles = current_caption_audio_roles(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], acoustic_audit=audit,
    )

    assert roles["rows"][0]["role"] == "unknown"
    assert roles["rows"][0]["action"] == "KEEP"
    assert roles["unavailable_observations"]


def test_voice_reference_does_not_invoke_aligned_mapping_scope(tmp_path, monkeypatch):
    inputs, callbacks, spec = _prepared(tmp_path)
    voice_spec = _voice_spec(inputs, spec)

    def forbidden_mapping_scope(*_args, **_kwargs):
        raise AssertionError("unaligned voice reference must not invoke aligned mapping")

    monkeypatch.setattr(
        "src.autoslice.producer_nested_acoustic._current_cue_scope_from_descriptor",
        forbidden_mapping_scope,
    )
    audit = _run(inputs, callbacks, voice_spec)
    row = audit["observations"][0]
    assert row["observer_result"]["status"] == "PASS"
    assert row["voice_reference_mode"] == "VOICE_REFERENCE_ONLY_UNALIGNED"
    assert row["action"] == "KEEP"


@pytest.mark.parametrize("drift", ["srt", "pcm", "frame", "mapping", "receipt", "raw_only"])
def test_current_roles_withdraw_stale_or_unbound_positive_role(tmp_path, drift):
    inputs, callbacks, spec = _prepared(tmp_path)
    audit = _run(inputs, callbacks, spec)
    if drift == "raw_only":
        audit["observations"][0].pop("validated_inputs")
    else:
        field = {"srt": "final_srt_path", "pcm": "target_audio_path", "frame": "caption_frame_path",
                 "mapping": "mapping_receipt_path"}.get(drift)
        path = Path(inputs[field]) if field else Path(audit["observations"][0]["observer_result"]["receipt_binding"]["path"])
        path.write_bytes(path.read_bytes() + (b"\n\n2\n00:00:00,300 --> 00:00:00,400\nnew cue\n" if drift == "srt" else b"drift"))
    roles = current_caption_audio_roles(candidate_id="candidate", source_media=inputs["outer_media_path"],
                                        subtitle_path=inputs["final_srt_path"], acoustic_audit=audit)
    assert all(row["role"] == "unknown" and row["action"] == "KEEP" for row in roles["rows"])


def test_current_role_context_rejects_earlier_srt_and_conflicting_receipts(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    first = _run(inputs, callbacks, spec)
    other_spec = copy.deepcopy(spec)
    other_spec["nested_media_acoustic"]["observations"][0]["observation_id"] = "other"
    other_spec["nested_media_acoustic"]["observations"][0]["visual_observation"]["observation_id"] = "other"
    second = _run(inputs, _role_callbacks(callbacks, "host"), other_spec)
    first["observations"].extend(second["observations"])
    roles = current_caption_audio_roles(candidate_id="candidate", source_media=inputs["outer_media_path"],
                                        subtitle_path=inputs["final_srt_path"], acoustic_audit=first)
    assert roles["rows"][0]["role"] == "unknown"
    assert len(roles["rows"][0]["receipts"]) == 2
    text = inputs["final_srt_path"].read_text()
    assert caption_audio_roles_context(text.replace("source words", "repaired words"), roles) == ""


def _role_callbacks(callbacks, role):
    states = {
        "source_media": ("PRESENT", "ABSENT", "ABSENT"),
        "host": ("ABSENT", "PRESENT", "ABSENT"),
        "overlap": ("PRESENT", "ABSENT", "PRESENT"),
    }
    source, host, overlap = states[role]

    def observe(request):
        response = _observer(request)
        response.update(
            source_media_speech=source, host_speech=host, overlap_speech=overlap
        )
        return response

    def judge(request, observation):
        response = _judge(request, observation)
        response.update(
            source_media_speech=source,
            host_speech=host,
            overlap_speech=overlap,
            final_class=role,
        )
        return response

    callbacks.observe_audio = observe
    callbacks.cpa_judge = judge
    return callbacks


def _provider_error(request):
    response = {
        "schema_version": "nested-media-acoustic-observation.v1",
        "status": "UNKNOWN",
        "authority": "EVIDENCE_ONLY",
        "candidate_id": request["candidate_id"],
        "observation_id": request["observation_id"],
        "request_sha256": request["request_sha256"],
        "source_media_speech": "UNKNOWN",
        "host_speech": "UNKNOWN",
        "overlap_speech": "UNKNOWN",
        "evidence": {
            "reason_code": "PROVIDER_ERROR",
            "error_type": "HTTPError",
            "http_status": 503,
            "provider_request_sha256": "sha256:" + "1" * 64,
            "provider_attempts": [{
                "free_key_ordinal": 1,
                "status": "FAILED",
                "error_type": "HTTPError",
                "category": "GEMINI_API_SERVER_ERROR",
                "http_status": 503,
            }],
        },
    }
    for field in ("target_interval_ms", "target_cue_indexes"):
        if field in request:
            response[field] = request[field]
    return response


def _missing_audio_spec(inputs):
    srt = Path(inputs["final_srt_path"])
    srt.write_text(
        "1\n00:00:00,100 --> 00:00:00,200\nsource words\n", encoding="utf-8"
    )
    inputs["final_srt_sha256"] = "sha256:" + hashlib.sha256(srt.read_bytes()).hexdigest()
    observation = {
        key: value for key, value in inputs.items()
        if key not in {
            "candidate_id", "outer_media_path", "final_srt_path", "evidence_root",
            "runtime_identity", "source_audio_path", "source_audio_sha256",
            "target_audio_path", "target_audio_sha256",
        }
    }
    observation["visual_observation"] = {
        "schema_version": OBSERVATION_SCHEMA, "candidate_id": "candidate",
        "observation_id": inputs["observation_id"], "frame_ms": 150,
        "caption_present": True, "visible_text": "source words",
        "role": "WATCHED_MEDIA_CAPTION", "pipeline_burned_subtitle": False,
        "frame_path": str(inputs["caption_frame_path"]),
        "frame_sha256": inputs["caption_frame_sha256"],
    }
    return {
        "watched_media": True,
        "nested_media_acoustic": {
            "schema_version": "nested-media-acoustic-inputs.v1",
            "observations": [observation],
        },
    }


def _fake_pcm_tools(monkeypatch, *, probe_duration=0.1, extra_frames=0):
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "ffmpeg":
            duration = float(argv[argv.index("-t") + 1])
            with wave.open(str(argv[-1]), "wb") as handle:
                handle.setnchannels(2)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\0" * ((round(duration * 16000) + extra_frames) * 4))
            return SimpleNamespace(stdout="", stderr="")
        assert argv[0] == "ffprobe"
        duration = float(argv[argv.index("-t") + 1]) if "-t" in argv else probe_duration
        return SimpleNamespace(
            stdout=json.dumps({
                "streams": [{
                    "codec_name": "pcm_s16le", "sample_fmt": "s16",
                    "sample_rate": 16000, "channels": 2,
                }],
                "format": {"duration": f"{duration:.3f}"},
            }),
            stderr="",
        )

    monkeypatch.setattr(
        "src.autoslice.producer_nested_acoustic.subprocess.run", run,
    )
    return calls


def _fake_voice_reference_tools(monkeypatch, *, source_duration=8.0):
    """Fake ffmpeg/ffprobe while preserving strict WAV duration checks."""
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "ffmpeg":
            duration = float(argv[argv.index("-t") + 1])
            with wave.open(str(argv[-1]), "wb") as handle:
                handle.setnchannels(2)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\0" * (round(duration * 16000) * 4))
            return SimpleNamespace(stdout="", stderr="")
        path = Path(argv[-1])
        if path.suffix == ".wav" and path.is_file():
            with wave.open(str(path), "rb") as handle:
                duration = handle.getnframes() / 16000.0
        else:
            duration = source_duration
        return SimpleNamespace(
            stdout=json.dumps({
                "streams": [{
                    "codec_name": "pcm_s16le", "sample_fmt": "s16",
                    "sample_rate": 16000, "channels": 2,
                }],
                "format": {"duration": f"{duration:.6f}"},
            }),
            stderr="",
        )

    monkeypatch.setattr(
        "src.autoslice.producer_nested_acoustic.subprocess.run", run,
    )
    return calls


def _install_weak_mapping(monkeypatch, measurements):
    def prepare(**kwargs):
        measurements.append(kwargs)
        return {
            "decision": "UNKNOWN_KEEP",
            "reason_code": "ALIGNMENT_CURRENT_CUE_INVERSE_WEAK_OR_AMBIGUOUS",
        }

    alignment = types.ModuleType("src.autoslice.nested_source_audio_alignment")
    alignment.prepare_nested_source_mapping = prepare
    alignment._normal_current_cue_scope = lambda scope, interval, **kwargs: dict(scope)
    monkeypatch.setitem(sys.modules, "src.autoslice.nested_source_audio_alignment", alignment)


def _prepare(inputs, spec, output_root):
    return prepare_missing_nested_acoustic_audio(
        spec=spec,
        candidate_id="candidate",
        source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"],
        evidence_root=output_root,
    )


def test_missing_audio_extracts_two_pcm16_crops_and_records_binding(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    calls = _fake_pcm_tools(monkeypatch)

    result = _prepare(inputs, spec, tmp_path / "prepare")

    assert result["status"] == "PREPARED"
    row = result["observations"][0]
    assert row["status"] == "PREPARED"
    fields = row["audio_fields"]
    assert set(fields) == {
        "source_audio_path", "source_audio_sha256",
        "target_audio_path", "target_audio_sha256",
    }
    assert len([argv for argv in calls if argv[0] == "ffmpeg"]) == 2
    assert len([argv for argv in calls if argv[0] == "ffprobe"]) == 2
    ffmpeg_calls = [argv for argv in calls if argv[0] == "ffmpeg"]
    assert all("-frames:a" not in argv for argv in ffmpeg_calls)
    assert ffmpeg_calls[0][ffmpeg_calls[0].index("-ss") + 1] == "0.093000"
    assert ffmpeg_calls[1][ffmpeg_calls[1].index("-ss") + 1] == "0.100000"
    assert all(argv[argv.index("-t") + 1] == "0.110000" for argv in ffmpeg_calls)
    assert all(argv[argv.index("-ar") + 1] == "16000" for argv in ffmpeg_calls)
    assert all(argv[argv.index("-ac") + 1] == "2" for argv in ffmpeg_calls)
    assert all(argv[argv.index("-c:a") + 1] == "pcm_s16le" for argv in ffmpeg_calls)
    prepare_path = Path(row["preparation_path"])
    manifest = json.loads(prepare_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "PREPARED"
    assert manifest["input_binding"]["format"]["codec_name"] == "pcm_s16le"
    assert manifest["input_binding"]["method"] == {
        "name": "FFMPEG_T_WINDOW_PCM16_BOUNDED_TRAILING_OVERSHOOT",
        "start_precision_digits": 6,
        "duration_precision_digits": 6,
        "trailing_timestamp_normalization_version": 2,
        "trailing_overshoot_request_ms": 10,
        "max_trailing_overshoot_ms": 20,
    }
    assert manifest["source_audio"]["window_ms"] == [93.0, 193.0]
    assert manifest["target_audio"]["window_ms"] == [100, 200]


def test_pcm_tail_normalization_preserves_raw_and_requested_start(tmp_path):
    path = tmp_path / "target.wav"
    samples = bytes(range(256)) * 2001
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(samples[:128006 * 4])
    raw_bytes = path.read_bytes()
    result = _normalize_trailing_pcm_frames(path, expected_duration_ms=8000)
    assert result["trimmed_frames"] == 6
    assert Path(result["raw_path"]).read_bytes() == raw_bytes
    assert result["raw_sha256"] == "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    with wave.open(str(path), "rb") as handle:
        assert handle.getnframes() == 128000
        assert handle.readframes(128000) == samples[:128000 * 4]


def test_normal_preparation_records_exact_audio_and_overshoot_receipts(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path / "input")
    _fake_pcm_tools(monkeypatch, extra_frames=6)
    result = _prepare(inputs, _missing_audio_spec(inputs), tmp_path / "prepare")
    assert result["status"] == "PREPARED"
    manifest = json.loads(Path(result["observations"][0]["preparation_path"]).read_text())
    for kind in ("source_audio", "target_audio"):
        probe = manifest[kind]["probe"]
        assert probe["wave"]["frames"] == 1600
        assert probe["trailing_timestamp_normalization"]["trimmed_frames"] == 166


def test_weak_current_mapping_uses_verified_cache_for_unaligned_voice_reference(
    tmp_path, monkeypatch,
):
    """A real source-cache fact may open only the bounded voice fallback."""
    from tests.test_nested_source_cache import _cache

    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    for field in (
        "independent_source_media_path", "independent_source_media_sha256",
        "mapping_receipt_path", "mapping_receipt_sha256",
    ):
        descriptor.pop(field, None)
    cache_root, _entry_path, _entry, locator = _cache(tmp_path, inputs)
    spec["nested_media_source_locator"] = locator
    original = copy.deepcopy(spec)
    measurements = []

    def prepare(**kwargs):
        measurements.append(kwargs)
        return {
            "decision": "UNKNOWN_KEEP",
            "reason_code": "ALIGNMENT_CURRENT_CUE_INVERSE_WEAK_OR_AMBIGUOUS",
        }

    alignment = types.ModuleType("src.autoslice.nested_source_audio_alignment")
    alignment.prepare_nested_source_mapping = prepare
    alignment._normal_current_cue_scope = lambda scope, interval, **kwargs: dict(scope)
    monkeypatch.setitem(sys.modules, "src.autoslice.nested_source_audio_alignment", alignment)
    _fake_voice_reference_tools(monkeypatch, source_duration=8.0)
    callbacks = SimpleNamespace(
        observe_audio=_observer, cpa_judge=_judge,
        runtime_identity=inputs["runtime_identity"],
    )

    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=cache_root,
    )

    row = result["observations"][0]
    assert len(measurements) == 1
    assert row["source_mapping_failure"]["decision"] == "UNKNOWN_KEEP"
    assert row["voice_reference_fallback"]["status"] == "PREPARED"
    assert row["audio_preparation"]["status"] == "PREPARED"
    assert row["voice_reference_mode"] == "VOICE_REFERENCE_ONLY_UNALIGNED"
    assert row["observer_result"]["status"] == "PASS"
    assert row["action"] == "KEEP"
    binding = json.loads(Path(row["voice_reference_fallback"]["binding_path"]).read_text())
    assert binding["reference_window_ms"] == [0, 6500]
    assert binding["target_interval_ms"] == [100, 200]
    assert binding["target_cue_indexes"] == [1]
    assert all(binding[field] is False for field in (
        "time_alignment_performed", "source_subtraction_authorized",
        "caption_dedup_authority", "source_role_authority", "mutation_authorized",
    ))
    roles = current_caption_audio_roles(
        candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], acoustic_audit=result,
    )
    assert roles["unavailable_observations"] == []
    assert roles["rows"][0]["role"] == "source_media"
    assert spec == original


@pytest.mark.parametrize("relevance_decision", [None, "UNKNOWN", "NO_MATCH", "MATCH"])
def test_candidate_cache_requires_separate_frame_relevance_for_voice_reference(
    tmp_path, monkeypatch, relevance_decision,
):
    from tests.test_nested_source_cache import _cache
    import src.autoslice.nested_source_cache as source_cache

    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    for field in (
        "independent_source_media_path", "independent_source_media_sha256",
        "mapping_receipt_path", "mapping_receipt_sha256",
    ):
        descriptor.pop(field, None)
    cache_root, _entry_path, _entry, locator = _cache(tmp_path, inputs)
    locator = {
        **locator,
        "status": "CANDIDATE_DISCOVERY_ONLY",
        "reason_code": "OUTER_SOURCE_HINT_EMPTY",
        "caption_candidate_discovery": {"status": "CANDIDATE_DISCOVERY_ONLY"},
    }
    spec["nested_media_source_locator"] = locator
    cached = {
        "status": "CANDIDATE_CACHE_AVAILABLE",
        "independent_source_media_path": str(inputs["independent_source_media_path"]),
        "independent_source_media_sha256": inputs["independent_source_media_sha256"],
        "source_cache_binding": {
            "bvid": "BVTEST123456", "cid": 123456,
            "source_role_authority": False,
        },
    }
    monkeypatch.setattr(source_cache, "read_cached_caption_source_candidate", lambda **_: cached)
    measurements = []
    _install_weak_mapping(monkeypatch, measurements)
    relevance_calls = []

    def relevance_probe(**kwargs):
        relevance_calls.append(kwargs)
        return {"decision": relevance_decision, "authority": "EVIDENCE_ONLY", "action": "KEEP"}

    def forbidden_audio(*_args):
        pytest.fail("unconfirmed candidate bytes must not dispatch")

    if relevance_decision == "MATCH":
        _fake_voice_reference_tools(monkeypatch, source_duration=8.0)
    callbacks = SimpleNamespace(
        observe_audio=_observer if relevance_decision == "MATCH" else forbidden_audio,
        cpa_judge=_judge if relevance_decision == "MATCH" else forbidden_audio,
        runtime_identity=inputs["runtime_identity"],
    )

    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=cache_root,
        source_relevance_probe=relevance_probe if relevance_decision is not None else None,
    )

    row = result["observations"][0]
    assert len(measurements) == 1
    assert len(relevance_calls) == (0 if relevance_decision is None else 1)
    assert row["action"] == "KEEP"
    if relevance_decision == "MATCH":
        assert row["voice_reference_mode"] == "VOICE_REFERENCE_ONLY_UNALIGNED"
        assert row["observer_result"]["status"] == "PASS"
        binding = json.loads(Path(row["voice_reference_fallback"]["binding_path"]).read_text())
        assert all(binding[field] is False for field in (
            "time_alignment_performed", "source_subtraction_authorized",
            "caption_dedup_authority", "source_role_authority", "mutation_authorized",
        ))
        assert cached["source_cache_binding"]["source_role_authority"] is False
        roles = current_caption_audio_roles(
            candidate_id="candidate", source_media=inputs["outer_media_path"],
            subtitle_path=inputs["final_srt_path"], acoustic_audit=result,
        )
        assert roles["rows"][0]["role"] == "source_media"
        assert roles["rows"][0]["action"] == "KEEP"
    else:
        assert row["reason_code"] == ("SOURCE_MAPPING_UNSUPPORTED" if relevance_decision is None
                                     else "SOURCE_FRAME_RELEVANCE_UNCONFIRMED")
        assert row["label"] == "unknown"
        assert "voice_reference_fallback" not in row


@pytest.mark.parametrize("relevance_decision", ["MATCH", "UNKNOWN"])
def test_caption_candidate_handoff_keeps_old_locator_frame_and_current_target_frame(
    tmp_path, monkeypatch, relevance_decision,
):
    """A neighboring discovery receipt supplies bytes only to the current cue."""
    from tests.test_nested_source_cache import _cache, _caption_discovery

    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    for field in (
        "independent_source_media_path", "independent_source_media_sha256",
        "mapping_receipt_path", "mapping_receipt_sha256",
    ):
        descriptor.pop(field, None)
    current_frame = tmp_path / "current-cue-frame.jpg"
    current_frame.write_bytes(b"current-cue-frame")
    current_frame_sha = "sha256:" + hashlib.sha256(current_frame.read_bytes()).hexdigest()
    descriptor.update(
        caption_frame_path=str(current_frame), caption_frame_sha256=current_frame_sha,
        caption_frame_ms=175,
    )
    descriptor["visual_observation"].update(
        frame_path=str(current_frame), frame_sha256=current_frame_sha, frame_ms=175,
    )

    cache_root, _entry_path, _entry, _cache_locator = _cache(tmp_path, inputs)
    old_locator = _caption_discovery(
        tmp_path / "cue75-discovery", inputs, cache_root=cache_root,
    )
    spec["nested_media_source_locator"] = _caption_discovery(
        tmp_path / "cue73-discovery", inputs, cache_root=cache_root,
        cache_candidate="BV1NO00000A",
    )
    locator_field = "locator" if relevance_decision == "MATCH" else "fresh_source_locator"
    envelope_path = tmp_path / "cue75-locator-envelope.json"
    envelope_path.write_text(json.dumps({locator_field: old_locator}, sort_keys=True), encoding="utf-8")
    envelope_sha = "sha256:" + hashlib.sha256(envelope_path.read_bytes()).hexdigest()
    spec["nested_media_caption_candidate_handoffs"] = [{
        "locator_path": str(envelope_path), "locator_sha256": envelope_sha,
        "locator_field": locator_field,
    }]
    original = copy.deepcopy(spec)

    measurements = []
    _install_weak_mapping(monkeypatch, measurements)
    relevance_calls = []

    def relevance_probe(**kwargs):
        relevance_calls.append(kwargs)
        assert kwargs["descriptor"]["caption_frame_ms"] == 175
        assert kwargs["cached_source"]["status"] == "CANDIDATE_CACHE_AVAILABLE"
        return {
            "decision": relevance_decision,
            "authority": "EVIDENCE_ONLY",
            "action": "KEEP",
        }

    def forbidden_audio(*_args, **_kwargs):
        pytest.fail("UNKNOWN source relevance must not dispatch audio")

    if relevance_decision == "MATCH":
        _fake_voice_reference_tools(monkeypatch, source_duration=8.0)
    callbacks = SimpleNamespace(
        observe_audio=_observer if relevance_decision == "MATCH" else forbidden_audio,
        cpa_judge=_judge if relevance_decision == "MATCH" else forbidden_audio,
        runtime_identity=inputs["runtime_identity"],
    )
    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=cache_root,
        source_relevance_probe=relevance_probe,
    )

    row = result["observations"][0]
    assert len(measurements) == 1
    assert len(relevance_calls) == 1
    handoff = row["caption_candidate_handoff"]
    assert handoff["locator_field"] == locator_field
    assert handoff["locator_frame"]["frame_ms"] == 150
    assert handoff["locator_request_sha256"] == old_locator["request_sha256"]
    assert handoff["current_outer_media"]["sha256"] == inputs["outer_media_sha256"]
    assert handoff["current_final_srt"]["sha256"] == inputs["final_srt_sha256"]
    assert row["current_locator_failure"] == "SOURCE_CACHE_NO_AVAILABLE_PUBLIC_CANDIDATE"
    assert row["current_locator_source_candidate"]["status"] == "CANDIDATE_CACHE_UNAVAILABLE"
    assert row["action"] == "KEEP"
    if relevance_decision == "MATCH":
        assert row["voice_reference_mode"] == "VOICE_REFERENCE_ONLY_UNALIGNED"
        assert row["voice_reference_authority"] == "EVIDENCE_ONLY"
    else:
        assert row["reason_code"] == "SOURCE_FRAME_RELEVANCE_UNCONFIRMED"
        assert row["label"] == "unknown"
        assert "observer_result" not in row
    assert spec == original


@pytest.mark.parametrize("drift", ["locator", "source", "srt", "role"])
def test_caption_candidate_handoff_drift_keeps_unknown_without_mapping_or_audio(
    tmp_path, monkeypatch, drift,
):
    from tests.test_nested_source_cache import _cache, _caption_discovery

    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    for field in (
        "independent_source_media_path", "independent_source_media_sha256",
        "mapping_receipt_path", "mapping_receipt_sha256",
    ):
        descriptor.pop(field, None)
    cache_root, _entry_path, _entry, _cache_locator = _cache(tmp_path, inputs)
    locator = _caption_discovery(tmp_path / "cue75-discovery", inputs, cache_root=cache_root)
    if drift == "source":
        locator["source_media_sha256"] = "sha256:" + "0" * 64
    elif drift == "srt":
        locator["final_srt_sha256"] = "sha256:" + "1" * 64
    elif drift == "role":
        locator["source_role_authority"] = True
    locator_path = tmp_path / "cue75-locator.json"
    locator_path.write_text(json.dumps(locator, sort_keys=True), encoding="utf-8")
    locator_sha = hashlib.sha256(locator_path.read_bytes()).hexdigest()
    handoff_sha = ("0" * 64) if drift == "locator" else locator_sha
    spec["nested_media_caption_candidate_handoffs"] = [{
        "locator_path": str(locator_path), "locator_sha256": "sha256:" + handoff_sha,
    }]
    measurements = []
    _install_weak_mapping(monkeypatch, measurements)

    def forbidden_audio(*_args, **_kwargs):
        pytest.fail("invalid candidate handoff must not dispatch audio")

    callbacks = SimpleNamespace(
        observe_audio=forbidden_audio, cpa_judge=forbidden_audio,
        runtime_identity=inputs["runtime_identity"],
    )
    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=cache_root,
        source_relevance_probe=lambda **_: pytest.fail("invalid handoff must not reach relevance"),
    )

    row = result["observations"][0]
    assert row["action"] == "KEEP"
    assert row["label"] == "unknown"
    assert row["reason_code"] == "NESTED_SOURCE_CACHE_UNAVAILABLE"
    handoff_audit = row["caption_candidate_handoff"]
    assert handoff_audit["status"] == "UNKNOWN_KEEP"
    assert handoff_audit["attempts"]
    assert handoff_audit["attempts"][0]["index"] == 0
    assert result["provider_calls"] == 0
    assert measurements == []


@pytest.mark.parametrize("drift", ["outer", "srt"])
def test_current_locator_input_drift_does_not_use_caption_handoff(
    tmp_path, monkeypatch, drift,
):
    """A current discovery drift is a hard failure, not a neighboring-cue fallback."""
    from tests.test_nested_source_cache import _cache, _caption_discovery

    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    for field in (
        "independent_source_media_path", "independent_source_media_sha256",
        "mapping_receipt_path", "mapping_receipt_sha256",
    ):
        descriptor.pop(field, None)
    cache_root, _entry_path, _entry, _cache_locator = _cache(tmp_path, inputs)
    valid_locator = _caption_discovery(tmp_path / "valid-discovery", inputs, cache_root=cache_root)
    current_locator = copy.deepcopy(valid_locator)
    if drift == "outer":
        current_locator["source_media_sha256"] = "sha256:" + "0" * 64
    else:
        current_locator["final_srt_sha256"] = "sha256:" + "1" * 64
    current_path = tmp_path / "current-locator.json"
    current_path.write_text(json.dumps(current_locator, sort_keys=True), encoding="utf-8")
    handoff_path = tmp_path / "handoff-locator.json"
    handoff_path.write_text(json.dumps(valid_locator, sort_keys=True), encoding="utf-8")
    handoff_sha = "sha256:" + hashlib.sha256(handoff_path.read_bytes()).hexdigest()
    spec["nested_media_source_locator"] = json.loads(current_path.read_text(encoding="utf-8"))
    spec["nested_media_caption_candidate_handoffs"] = [{
        "locator_path": str(handoff_path), "locator_sha256": handoff_sha,
    }]

    measurements = []
    _install_weak_mapping(monkeypatch, measurements)
    callbacks = SimpleNamespace(
        observe_audio=lambda *_args, **_kwargs: pytest.fail("current locator drift must not dispatch audio"),
        cpa_judge=lambda *_args, **_kwargs: pytest.fail("current locator drift must not dispatch CPA"),
        runtime_identity=inputs["runtime_identity"],
    )
    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=cache_root,
        source_relevance_probe=lambda **_: pytest.fail("current locator drift must not use handoff"),
    )

    row = result["observations"][0]
    assert row["current_locator_failure"] == "CAPTION_DISCOVERY_CURRENT_INPUT_DRIFT"
    assert "caption_candidate_handoff" not in row
    assert row["action"] == "KEEP"
    assert row["label"] == "unknown"
    assert result["provider_calls"] == 0
    assert measurements == []


@pytest.mark.parametrize("reuse_kind", ["path", "bytes", "identity"])
def test_verified_cache_outer_mix_cannot_open_voice_reference_fallback(
    tmp_path, monkeypatch, reuse_kind,
):
    """A cache receipt does not make the current outer mix an independent voice source."""
    from tests.test_nested_source_cache import _cache
    import src.autoslice.nested_source_cache as source_cache

    inputs = _inputs(tmp_path / "input")
    if reuse_kind == "path":
        inputs["independent_source_media_path"] = inputs["outer_media_path"]
        inputs["independent_source_media_sha256"] = inputs["outer_media_sha256"]
    elif reuse_kind == "bytes":
        source_path = Path(inputs["independent_source_media_path"])
        source_path.write_bytes(Path(inputs["outer_media_path"]).read_bytes())
        inputs["independent_source_media_sha256"] = inputs["outer_media_sha256"]
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    for field in (
        "independent_source_media_path", "independent_source_media_sha256",
        "mapping_receipt_path", "mapping_receipt_sha256",
    ):
        descriptor.pop(field, None)
    cache_root, _entry_path, _entry, locator = _cache(tmp_path, inputs)
    spec["nested_media_source_locator"] = locator
    if reuse_kind == "identity":
        cached = source_cache.read_cached_nested_source(
            cache_root=cache_root, locator=locator, candidate_id="candidate",
            source_media=inputs["outer_media_path"], subtitle_path=inputs["final_srt_path"],
        )
        context_path = tmp_path / "outer-context.mp4"
        context_path.write_bytes(b"outer-context")
        spec["nested_media_outer_context"] = {
            "current_outer": {
                "path": str(context_path),
                "sha256": "sha256:" + hashlib.sha256(context_path.read_bytes()).hexdigest(),
                "bvid": cached["source_cache_binding"]["bvid"],
                "cid": cached["source_cache_binding"]["cid"],
            },
        }
    measurements = []
    _install_weak_mapping(monkeypatch, measurements)
    _fake_voice_reference_tools(monkeypatch, source_duration=8.0)
    callbacks = SimpleNamespace(
        observe_audio=lambda *_args: pytest.fail("outer mix must not dispatch as source voice"),
        cpa_judge=lambda *_args: pytest.fail("outer mix must not dispatch as source voice"),
        runtime_identity=inputs["runtime_identity"],
    )

    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=cache_root,
    )

    row = result["observations"][0]
    assert len(measurements) == 1
    assert row["source_mapping_failure"]["decision"] == "UNKNOWN_KEEP"
    assert row["voice_reference_fallback"]["status"] == "UNKNOWN_KEEP"
    assert row["voice_reference_fallback"]["reason_code"] in {
        "VOICE_REFERENCE_SOURCE_IS_OUTER_MEDIA",
        "VOICE_REFERENCE_SOURCE_BYTES_ARE_OUTER_MEDIA",
        "VOICE_REFERENCE_SOURCE_IDENTITY_IS_OUTER",
    }
    assert row["reason_code"] == "SOURCE_MAPPING_UNSUPPORTED"
    assert row["action"] == "KEEP" and row["label"] == "unknown"


@pytest.mark.parametrize("drift", ["partial", "stale"])
def test_voice_reference_prepare_drift_remains_unknown_keep(tmp_path, monkeypatch, drift):
    from tests.test_nested_source_cache import _cache

    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    for field in (
        "independent_source_media_path", "independent_source_media_sha256",
        "mapping_receipt_path", "mapping_receipt_sha256",
    ):
        descriptor.pop(field, None)
    cache_root, _entry_path, _entry, locator = _cache(tmp_path, inputs)
    spec["nested_media_source_locator"] = locator
    measurements = []
    _install_weak_mapping(monkeypatch, measurements)
    _fake_voice_reference_tools(monkeypatch, source_duration=8.0)
    callbacks = SimpleNamespace(
        observe_audio=_observer, cpa_judge=_judge,
        runtime_identity=inputs["runtime_identity"],
    )
    kwargs = dict(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=cache_root,
    )
    first = observe_prepared_nested_acoustics(**kwargs)
    fallback = first["observations"][0]["voice_reference_fallback"]
    binding_path = Path(fallback["binding_path"])
    manifest_path = Path(fallback["preparation_path"])
    if drift == "partial":
        manifest_path.unlink()
    else:
        payload = json.loads(binding_path.read_text(encoding="utf-8"))
        payload["target_interval_ms"] = [100, 199]
        binding_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    callbacks.observe_audio = callbacks.cpa_judge = lambda *_args: pytest.fail(
        "partial or stale fallback must not dispatch"
    )

    second = observe_prepared_nested_acoustics(**kwargs)
    row = second["observations"][0]
    assert row["reason_code"] == "SOURCE_MAPPING_UNSUPPORTED"
    assert row["voice_reference_fallback"]["status"] == "UNKNOWN_KEEP"
    assert row["action"] == "KEEP" and row["label"] == "unknown"


def test_pcm_extract_requests_bounded_tail_but_keeps_original_window(tmp_path, monkeypatch):
    output = tmp_path / "target.wav"
    calls = _fake_pcm_tools(monkeypatch)

    _argv, probe = _extract_pcm16_wav(
        source=tmp_path / "source.mkv", output=output, window_ms=[93, 193],
    )

    ffmpeg = next(argv for argv in calls if argv[0] == "ffmpeg")
    assert ffmpeg[ffmpeg.index("-ss") + 1] == "0.093000"
    assert ffmpeg[ffmpeg.index("-t") + 1] == "0.110000"
    assert probe["wave"]["frames"] == 1600
    assert probe["trailing_timestamp_normalization"]["expected_frames"] == 1600
    assert probe["trailing_timestamp_normalization"]["trimmed_frames"] == 160


def test_pcm_extract_rejects_true_eof_short_result(tmp_path, monkeypatch):
    output = tmp_path / "target.wav"
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] != "ffmpeg":
            raise AssertionError("a short extraction must fail before ffprobe")
        with wave.open(str(argv[-1]), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\0" * (1599 * 4))
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr("src.autoslice.producer_nested_acoustic.subprocess.run", run)
    with pytest.raises(NestedMediaAcousticObserverError, match="PCM16_AUDIO_FRAME_COUNT_MISMATCH"):
        _extract_pcm16_wav(
            source=tmp_path / "source.mkv", output=output, window_ms=[0, 100],
        )

    ffmpeg = calls[0]
    assert ffmpeg[ffmpeg.index("-ss") + 1] == "0.000000"
    assert ffmpeg[ffmpeg.index("-t") + 1] == "0.110000"
    assert not output.with_name(output.name + ".timestamp-overshoot.wav").exists()
    with wave.open(str(output), "rb") as handle:
        assert handle.getnframes() == 1599


@pytest.mark.parametrize("frames", [127999, 128321])
def test_pcm_tail_normalization_rejects_short_or_large_overshoot(tmp_path, frames):
    path = tmp_path / "target.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0" * frames * 4)
    before = path.read_bytes()
    with pytest.raises(NestedMediaAcousticObserverError, match="PCM16_AUDIO_FRAME_COUNT_MISMATCH"):
        _normalize_trailing_pcm_frames(path, expected_duration_ms=8000)
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".timestamp-overshoot.wav").exists()


def test_successful_missing_audio_prep_replays_without_ffmpeg(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    calls = _fake_pcm_tools(monkeypatch)
    first = _prepare(inputs, spec, tmp_path / "prepare")
    assert first["status"] == "PREPARED"
    assert len(calls) == 4

    def no_reextract(*_args, **_kwargs):
        raise AssertionError("stable preparation must not rerun ffmpeg")

    monkeypatch.setattr("src.autoslice.producer_nested_acoustic.subprocess.run", no_reextract)
    second = _prepare(inputs, spec, tmp_path / "prepare")
    assert second["status"] == "AUDIO_COMPLETE"
    assert second["observations"][0]["status"] == "REPLAYED"
    assert second["observations"][0]["audio_fields"] == first["observations"][0]["audio_fields"]


def test_prepared_output_path_drift_refuses_replay(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    _fake_pcm_tools(monkeypatch)
    first = _prepare(inputs, spec, tmp_path / "prepare")
    manifest_path = Path(first["observations"][0]["preparation_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_audio"]["path"] = str(manifest_path.parent / "other.wav")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    def no_reextract(*_args, **_kwargs):
        raise AssertionError("drifted preparation must not be replaced")

    monkeypatch.setattr("src.autoslice.producer_nested_acoustic.subprocess.run", no_reextract)
    result = _prepare(inputs, spec, tmp_path / "prepare")
    assert result["observations"][0]["reason_code"] == "PCM16_AUDIO_PREPARE_OUTPUT_DRIFT"
    assert result["observations"][0]["status"] == "EVIDENCE_ONLY"


def test_existing_complete_audio_is_preserved_without_repair(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path / "input")
    descriptor_spec = _missing_audio_spec(inputs)
    descriptor = descriptor_spec["nested_media_acoustic"]["observations"][0]
    descriptor.update(
        source_audio_path=str(inputs["source_audio_path"]),
        source_audio_sha256=inputs["source_audio_sha256"],
        target_audio_path=str(inputs["target_audio_path"]),
        target_audio_sha256=inputs["target_audio_sha256"],
    )

    def no_reextract(*_args, **_kwargs):
        raise AssertionError("complete audio must not be replaced")

    monkeypatch.setattr("src.autoslice.producer_nested_acoustic.subprocess.run", no_reextract)
    result = _prepare(inputs, descriptor_spec, tmp_path / "prepare")
    assert result["status"] == "AUDIO_COMPLETE"
    assert result["observations"][0]["status"] == "AUDIO_COMPLETE"
    assert result["observations"][0]["audio_fields"]["source_audio_path"] == str(inputs["source_audio_path"])


def test_partial_audio_fields_fail_closed_without_repair(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    descriptor["source_audio_path"] = str(inputs["source_audio_path"])

    def no_reextract(*_args, **_kwargs):
        raise AssertionError("partial audio must not be silently repaired")

    monkeypatch.setattr("src.autoslice.producer_nested_acoustic.subprocess.run", no_reextract)
    result = _prepare(inputs, spec, tmp_path / "prepare")
    assert result["observations"][0]["reason_code"] == "AUDIO_FIELDS_INCOMPLETE"
    assert result["observations"][0]["status"] == "EVIDENCE_ONLY"


@pytest.mark.parametrize("drift", ["srt", "source", "frame", "mapping"])
def test_current_binding_drift_does_not_dispatch_ffmpeg(tmp_path, monkeypatch, drift):
    inputs = _inputs(tmp_path / drift)
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    if drift == "srt":
        Path(inputs["final_srt_path"]).write_bytes(b"changed current srt")
        expected = "FINAL_SRT_HASH_MISMATCH"
    elif drift == "source":
        Path(inputs["independent_source_media_path"]).write_bytes(b"changed source")
        expected = "INDEPENDENT_SOURCE_HASH_MISMATCH"
    elif drift == "frame":
        Path(inputs["caption_frame_path"]).write_bytes(b"changed frame")
        expected = "CAPTION_FRAME_HASH_MISMATCH"
    else:
        mapping_path = Path(inputs["mapping_receipt_path"])
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        mapping["lineage_mapping"]["lag_ms"] = 8.0
        mapping_path.write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")
        descriptor["mapping_receipt_sha256"] = "sha256:" + hashlib.sha256(
            mapping_path.read_bytes()
        ).hexdigest()
        expected = "MAPPING_LAG_WINDOW_MISMATCH"

    def no_reextract(*_args, **_kwargs):
        raise AssertionError("binding drift must block before ffmpeg")

    monkeypatch.setattr("src.autoslice.producer_nested_acoustic.subprocess.run", no_reextract)
    result = _prepare(inputs, spec, tmp_path / "prepare")
    assert result["observations"][0]["reason_code"] == expected
    assert result["observations"][0]["status"] == "EVIDENCE_ONLY"


def test_local_mapping_receipt_is_bound_to_current_descriptor_scope(tmp_path):
    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    descriptor.update(target_interval_ms=[100, 200], target_cue_indexes=[1])
    scope = {
        "schema_version": "nested-source-audio-local-scope.v1",
        "outer_window_ms": [0, 25000],
        "source_window_ms": [0, 20000],
        "chunk_windows_ms": [[0, 8000], [8000, 16000]],
        "descriptor_context_interval_ms": [100, 200],
        "target_interval_ms": [100, 200],
        "target_cue_indexes": [1],
        "observation_id": descriptor["observation_id"],
        "caption_frame_path": str(descriptor["caption_frame_path"]),
        "caption_frame_sha256": descriptor["caption_frame_sha256"],
        "caption_frame_ms": descriptor["caption_frame_ms"],
        "final_srt_sha256": descriptor["final_srt_sha256"],
    }
    mapping_path = Path(inputs["mapping_receipt_path"])
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["alignment_scope"] = scope
    mapping_path.write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")
    descriptor["mapping_receipt_sha256"] = "sha256:" + hashlib.sha256(
        mapping_path.read_bytes()
    ).hexdigest()
    descriptor["local_source_mapping_scope"] = scope

    binding, state = _descriptor_binding(
        descriptor=descriptor, candidate_id="candidate",
        source_media=inputs["outer_media_path"], subtitle_path=inputs["final_srt_path"],
    )
    assert state["source_window_ms"] == [93.0, 193.0]
    assert binding["mapping_receipt"]["outer_window_ms"] == [100.0, 200.0]

    mismatched = copy.deepcopy(descriptor)
    mismatched["local_source_mapping_scope"] = {**scope, "target_cue_indexes": [2]}
    with pytest.raises(NestedMediaAcousticObserverError, match="LOCAL_MAPPING_DESCRIPTOR_SCOPE_MISMATCH"):
        _descriptor_binding(
            descriptor=mismatched, candidate_id="candidate",
            source_media=inputs["outer_media_path"], subtitle_path=inputs["final_srt_path"],
        )


def test_current_cue_scope_is_automatically_bound_to_current_media_srt_and_frame(tmp_path):
    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    descriptor.update(target_interval_ms=[100, 200], target_cue_indexes=[1])
    scope = _current_cue_scope_from_descriptor(
        descriptor=descriptor, candidate_id="candidate",
        context_interval=[100, 200], target_interval=[100, 200], target_cues=[1],
        media_sha=inputs["outer_media_sha256"], srt_sha=inputs["final_srt_sha256"],
    )
    assert scope is not None
    assert scope["candidate_id"] == "candidate"
    assert scope["outer_media_sha256"] == inputs["outer_media_sha256"]
    assert scope["final_srt_sha256"] == inputs["final_srt_sha256"]
    assert scope["caption_frame_sha256"] == inputs["caption_frame_sha256"]

    mapping_path = Path(inputs["mapping_receipt_path"])
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["mapping_scope"] = "CURRENT_CUE_REVERSE_NCC"
    mapping["current_cue_scope"] = scope
    mapping_path.write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")
    descriptor["mapping_receipt_sha256"] = "sha256:" + hashlib.sha256(
        mapping_path.read_bytes()
    ).hexdigest()
    descriptor["current_cue_scope"] = scope
    binding, _state = _descriptor_binding(
        descriptor=descriptor, candidate_id="candidate",
        source_media=inputs["outer_media_path"], subtitle_path=inputs["final_srt_path"],
    )
    assert binding["mapping_receipt"]["outer_window_ms"] == [100.0, 200.0]

    drifted = copy.deepcopy(descriptor)
    drifted["current_cue_scope"] = {**scope, "final_srt_sha256": "sha256:" + "c" * 64}
    with pytest.raises(
        NestedMediaAcousticObserverError, match="CURRENT_CUE_SCOPE_DESCRIPTOR_MISMATCH"
    ):
        _descriptor_binding(
            descriptor=drifted, candidate_id="candidate",
            source_media=inputs["outer_media_path"], subtitle_path=inputs["final_srt_path"],
        )


def test_current_cue_scope_requires_validated_acoustic_only_binding(tmp_path):
    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    descriptor.update(target_interval_ms=[100, 200], target_cue_indexes=[1])
    descriptor["visual_observation"] = {
        **descriptor["visual_observation"],
        "caption_present": True,
        "role": "UNKNOWN_KEEP",
        "visible_text": "unmatched source frame",
    }
    descriptor["acoustic_only_timing_scope"] = {
        "schema_version": "nested-media-acoustic-timing-scope.v1",
        "kind": "CURRENT_WATCHED_MEDIA_CUE",
    }

    # A raw acoustic-only marker cannot authorize automatic source mapping.
    assert _current_cue_scope_from_descriptor(
        descriptor=descriptor, candidate_id="candidate",
        context_interval=[100, 200], target_interval=[100, 200], target_cues=[1],
        media_sha=inputs["outer_media_sha256"], srt_sha=inputs["final_srt_sha256"],
    ) is None

    scope = _current_cue_scope_from_descriptor(
        descriptor=descriptor, candidate_id="candidate",
        context_interval=[100, 200], target_interval=[100, 200], target_cues=[1],
        media_sha=inputs["outer_media_sha256"], srt_sha=inputs["final_srt_sha256"],
        validated_acoustic_only=True,
    )
    assert scope is not None
    assert scope["target_cue_indexes"] == [1]
    assert scope["observation_id"] == descriptor["observation_id"]


def test_current_cue_scope_rejects_unknown_nonwatched_visual_without_acoustic_validation(tmp_path):
    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    descriptor["visual_observation"] = {
        **descriptor["visual_observation"],
        "caption_present": False,
        "role": "NO_EMBEDDED_CAPTION_OBSERVED",
    }
    assert _current_cue_scope_from_descriptor(
        descriptor=descriptor, candidate_id="candidate",
        context_interval=[100, 200], target_interval=[100, 200], target_cues=[1],
        media_sha=inputs["outer_media_sha256"], srt_sha=inputs["final_srt_sha256"],
    ) is None


def test_ordinary_handoff_collects_role_without_mutating_subtitle(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    before = Path(inputs["final_srt_path"]).read_bytes()
    result = _run(inputs, callbacks, spec)
    assert result["observations"][0]["label"] == "source_media"
    assert result["observations"][0]["action"] == "KEEP"
    assert result["observations"][0]["observer_result"]["status"] == "PASS"
    assert result["display_mutation_authorized"] is False
    assert result["publication_authorized"] is False
    assert Path(inputs["final_srt_path"]).read_bytes() == before


def test_callback_factory_routes_mixed_voice_and_aligned_rows_independently(tmp_path):
    inputs, callbacks, normal_spec = _prepared(tmp_path)
    voice_spec = _voice_spec(inputs, normal_spec)
    voice_descriptor = voice_spec["nested_media_acoustic"]["observations"][0]
    normal_descriptor = copy.deepcopy(
        normal_spec["nested_media_acoustic"]["observations"][0]
    )
    normal_descriptor["observation_id"] = "cue-2"
    normal_descriptor["visual_observation"]["observation_id"] = "cue-2"
    mixed = {
        "watched_media": True,
        "nested_media_acoustic": {
            "schema_version": "nested-media-acoustic-inputs.v1",
            "observations": [voice_descriptor, normal_descriptor],
        },
    }
    modes = []

    def factory(**kwargs):
        descriptor = kwargs["descriptor"]
        modes.append(descriptor.get("observer_mode"))
        return callbacks

    result = observe_prepared_nested_acoustics(
        spec=mixed, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks_factory=factory,
    )

    assert modes == ["VOICE_REFERENCE_ONLY_UNALIGNED", None]
    assert [row["observer_result"]["status"] for row in result["observations"]] == [
        "PASS", "PASS",
    ]


def test_missing_audio_hook_prepares_private_copy_before_existing_consumer(
    tmp_path, monkeypatch
):
    inputs = _inputs(tmp_path / "input")
    callbacks = SimpleNamespace(
        observe_audio=None, cpa_judge=None, runtime_identity=inputs["runtime_identity"]
    )
    captured = {}

    def observer(request):
        captured.update(request)
        return _observer(request)

    callbacks.observe_audio = observer
    callbacks.cpa_judge = _judge
    spec = _missing_audio_spec(inputs)
    before = copy.deepcopy(spec)
    _fake_pcm_tools(monkeypatch)

    result = _run(inputs, callbacks, spec)

    row = result["observations"][0]
    assert row["observer_result"]["status"] == "PASS"
    assert row["audio_preparation"]["status"] == "PREPARED"
    assert captured["part_order"] == [
        "host_enrollment", "independent_source", "current_target"
    ]
    assert Path(captured["audio_parts"][1]["path"]).is_file()
    assert Path(captured["audio_parts"][2]["path"]).is_file()
    assert captured["interval_ms"] == [100, 200]
    assert spec == before


def test_missing_audio_hook_rejects_partial_audio_before_dispatch(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path / "input")
    callbacks = SimpleNamespace(
        observe_audio=lambda *_args, **_kwargs: pytest.fail("partial audio dispatched"),
        cpa_judge=lambda *_args, **_kwargs: pytest.fail("partial audio judged"),
        runtime_identity=inputs["runtime_identity"],
    )
    spec = _missing_audio_spec(inputs)
    spec["nested_media_acoustic"]["observations"][0]["source_audio_path"] = str(
        inputs["source_audio_path"]
    )
    result = _run(inputs, callbacks, spec)
    assert result["observations"][0]["reason_code"] == "AUDIO_FIELDS_INCOMPLETE"


def test_missing_audio_hook_rejects_current_cue_interval_drift_before_prepare(
    tmp_path, monkeypatch
):
    inputs = _inputs(tmp_path / "input")
    callbacks = SimpleNamespace(
        observe_audio=lambda *_args, **_kwargs: pytest.fail("drifted cue dispatched"),
        cpa_judge=lambda *_args, **_kwargs: pytest.fail("drifted cue judged"),
        runtime_identity=inputs["runtime_identity"],
    )
    spec = _missing_audio_spec(inputs)
    spec["nested_media_acoustic"]["observations"][0]["interval_ms"] = [150, 200]
    calls = _fake_pcm_tools(monkeypatch)
    result = _run(inputs, callbacks, spec)
    assert result["observations"][0]["reason_code"] == "CURRENT_CUE_INTERVAL_MISMATCH"
    assert not [argv for argv in calls if argv[0] == "ffmpeg"]


def test_missing_audio_hook_accepts_mapped_context_covering_the_whole_cue(
    tmp_path, monkeypatch
):
    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    descriptor["interval_ms"] = [50, 250]
    mapping_path = Path(inputs["mapping_receipt_path"])
    mapping = json.loads(mapping_path.read_text())
    mapping["lineage_mapping"].update(
        outer_window_ms=[50, 250], inner_window_ms=[43.0, 243.0]
    )
    mapping_path.write_text(json.dumps(mapping, sort_keys=True))
    descriptor["mapping_receipt_sha256"] = (
        "sha256:" + hashlib.sha256(mapping_path.read_bytes()).hexdigest()
    )
    before = copy.deepcopy(spec)
    calls = _fake_pcm_tools(monkeypatch, probe_duration=0.2)
    seen = []

    def observer(request):
        seen.append(request)
        return _observer(request)

    callbacks = SimpleNamespace(
        observe_audio=observer, cpa_judge=_judge,
        runtime_identity=inputs["runtime_identity"],
    )
    result = _run(inputs, callbacks, spec)
    row = result["observations"][0]
    assert row["audio_preparation"]["status"] == "PREPARED"
    assert row["observer_result"]["status"] == "PASS"
    assert row["cue_interval_ms"] == [100, 200]
    assert row["context_interval_ms"] == [50, 250]
    assert seen[0]["interval_ms"] == [50, 250]
    assert seen[0]["target_interval_ms"] == [100, 200]
    assert seen[0]["target_cue_indexes"] == [1]
    assert seen[0]["mapping_receipt"]["source_window_ms"] == [43.0, 243.0]
    assert len([argv for argv in calls if argv[0] == "ffmpeg"]) == 2
    assert spec == before


def test_existing_target_scope_must_match_current_visual_cue_before_dispatch(tmp_path):
    inputs, _callbacks, spec = _prepared(tmp_path)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    descriptor.update(target_interval_ms=[100, 200], target_cue_indexes=[2])
    callbacks = SimpleNamespace(observe_audio=lambda *_args: pytest.fail("wrong target dispatched"),
                                cpa_judge=lambda *_args: pytest.fail("wrong target judged"),
                                runtime_identity=inputs["runtime_identity"])
    result = _run(inputs, callbacks, spec)
    assert result["observations"][0]["reason_code"] == "CURRENT_TARGET_CUE_BINDING_MISMATCH"
    assert result["observations"][0]["label"] == "unknown"


def test_wider_context_without_matching_source_mapping_cannot_dispatch(
    tmp_path, monkeypatch
):
    inputs = _inputs(tmp_path / "input")
    spec = _missing_audio_spec(inputs)
    spec["nested_media_acoustic"]["observations"][0]["interval_ms"] = [50, 250]
    callbacks = SimpleNamespace(
        observe_audio=lambda *_args: pytest.fail("unmapped context dispatched"),
        cpa_judge=lambda *_args: pytest.fail("unmapped context judged"),
        runtime_identity=inputs["runtime_identity"],
    )
    calls = _fake_pcm_tools(monkeypatch)
    result = _run(inputs, callbacks, spec)
    assert result["observations"][0]["label"] == "unknown"
    assert result["observations"][0]["action"] == "KEEP"
    assert not [argv for argv in calls if argv[0] == "ffmpeg"]


def test_current_srt_change_does_not_rebind_prepared_evidence(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    def never(*args):
        raise AssertionError("stale prepared evidence cannot dispatch")
    callbacks.observe_audio = callbacks.cpa_judge = never
    Path(inputs["final_srt_path"]).write_text("1\n00:00:00,100 --> 00:00:00,200\nnew words\n")
    result = _run(inputs, callbacks, spec)
    assert result["observations"][0]["reason_code"] == "PREPARED_ACOUSTIC_INPUT_STALE"
    assert result["observations"][0]["label"] == "unknown"


def test_game_or_absent_descriptor_makes_no_acoustic_call(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    callbacks.observe_audio = callbacks.cpa_judge = lambda *args: (_ for _ in ()).throw(AssertionError("not requested"))
    assert _run(inputs, callbacks, {})["status"] == "NOT_REQUESTED"
    spec["watched_media"] = False
    result = _run(inputs, callbacks, spec)
    assert result["reason_code"] == "WATCHED_MEDIA_DECLARATION_REQUIRED"


def test_different_visual_frame_cannot_use_acoustic_descriptor(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    spec = copy.deepcopy(spec)
    spec["nested_media_acoustic"]["observations"][0]["visual_observation"]["frame_ms"] = 160
    result = _run(inputs, callbacks, spec)
    assert result["observations"][0]["reason_code"] == "CURRENT_VISUAL_BINDING_MISMATCH"
    assert result["observations"][0]["action"] == "KEEP"


def test_optional_runtime_config_failure_is_unknown_without_secret_or_dispatch(tmp_path, monkeypatch):
    import scripts.session_autoslice as runtime
    def unavailable():
        raise OSError("secret credential details")
    monkeypatch.setattr(runtime, "load_gemini_credentials", unavailable)
    observe = build_runtime_nested_acoustic_observer(tmp_path)
    result = observe(spec={"nested_media_acoustic": {}}, candidate_id="candidate")
    assert result["reason_code"] == "NESTED_ACOUSTIC_RUNTIME_CONFIG_UNAVAILABLE"
    assert result["label"] == "unknown"
    assert result["action"] == "KEEP"
    assert "secret" not in str(result)


def test_agy_runtime_factory_binds_prompts_per_descriptor_mode(tmp_path, monkeypatch):
    import src.autoslice.agy_three_part_acoustic as agy
    import src.autoslice.nested_media_acoustic_runtime as acoustic_runtime
    import src.autoslice.producer_final_review_transport as final_transport
    import src.autoslice.producer_nested_acoustic as producer

    cpa = SimpleNamespace(
        cpa_cache_identity={
            "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium",
        }
    )
    monkeypatch.setattr(final_transport, "build_final_review_llm_call", lambda **_: cpa)
    captured = {}
    monkeypatch.setattr(
        producer, "observe_prepared_nested_acoustics",
        lambda **kwargs: captured.update(kwargs) or {"status": "EVIDENCE_ONLY"},
    )
    built = []

    def fake_build(**kwargs):
        built.append(kwargs)
        return "callbacks"

    monkeypatch.setattr(agy, "build_three_part_callbacks", fake_build)
    observe = producer.build_runtime_nested_acoustic_observer(tmp_path)
    observe(spec={"nested_media_acoustic": {"observer_transport": "agy_three_part"}})
    factory = captured["callbacks_factory"]
    factory(descriptor={}, candidate_id="candidate", observation_id="normal", evidence_root=tmp_path)
    locator = {
        "schema_version": "short-reference-playback-locator.v1",
        "status": "LOCATED", "authority": "EVIDENCE_ONLY",
        "time_alignment_performed": False,
    }
    factory(
        descriptor={"observer_mode": "VOICE_REFERENCE_ONLY_UNALIGNED",
                    "playback_locator": locator},
        candidate_id="candidate", observation_id="voice", evidence_root=tmp_path,
    )

    assert built[0]["observer_prompt"] == agy.OBSERVER_PROMPT
    assert built[0]["cpa_prompt"] == agy.CPA_PROMPT
    assert built[0]["attachment_instructions"] == agy.ATTACHMENT_INSTRUCTIONS
    assert built[1]["observer_prompt"] == acoustic_runtime.VOICE_REFERENCE_OBSERVER_PROMPT
    assert built[1]["cpa_prompt"] == acoustic_runtime.VOICE_REFERENCE_CPA_PROMPT
    assert built[1]["attachment_instructions"] == agy.VOICE_REFERENCE_ATTACHMENT_INSTRUCTIONS
    assert built[1]["playback_locator"] == locator


def test_gemini_runtime_factory_binds_prompts_per_descriptor_mode(tmp_path, monkeypatch):
    import scripts.session_autoslice as runtime
    import src.autoslice.entity_audio_verifier as verifier
    import src.autoslice.nested_media_acoustic_runtime as acoustic_runtime
    import src.autoslice.producer_final_review_transport as final_transport
    import src.autoslice.producer_nested_acoustic as producer

    cpa = SimpleNamespace(
        cpa_cache_identity={
            "transport": "fixture.cpa", "models": ["fixture"], "effort": "medium",
        }
    )
    monkeypatch.setattr(runtime, "load_gemini_credentials", lambda: {"GEMINI_API_KEY": "key"})
    monkeypatch.setattr(final_transport, "build_final_review_llm_call", lambda **_: cpa)
    monkeypatch.setattr(verifier, "_entity_api_model", lambda: "fixture-model")
    captured = {}
    monkeypatch.setattr(
        producer, "observe_prepared_nested_acoustics",
        lambda **kwargs: captured.update(kwargs) or {"status": "EVIDENCE_ONLY"},
    )
    built = []

    def fake_build(**kwargs):
        built.append(kwargs)
        return "callbacks"

    monkeypatch.setattr(acoustic_runtime, "build_runtime_acoustic_callbacks", fake_build)
    observe = producer.build_runtime_nested_acoustic_observer(tmp_path)
    observe(spec={"nested_media_acoustic": {}})
    factory = captured["callbacks_factory"]
    factory(descriptor={}, candidate_id="candidate", observation_id="normal", evidence_root=tmp_path)
    locator = {
        "schema_version": "short-reference-playback-locator.v1",
        "status": "LOCATED", "authority": "EVIDENCE_ONLY",
        "time_alignment_performed": False,
    }
    factory(
        descriptor={"observer_mode": "VOICE_REFERENCE_ONLY_UNALIGNED",
                    "playback_locator": locator},
        candidate_id="candidate", observation_id="voice", evidence_root=tmp_path,
    )

    assert built[0]["observer_prompt"] == acoustic_runtime.OBSERVER_PROMPT
    assert built[0]["cpa_prompt"] == acoustic_runtime.CPA_PROMPT
    assert built[1]["observer_prompt"] == acoustic_runtime.VOICE_REFERENCE_OBSERVER_PROMPT
    assert built[1]["cpa_prompt"] == acoustic_runtime.VOICE_REFERENCE_CPA_PROMPT
    assert built[1]["playback_locator"] == locator


@pytest.mark.parametrize("failure_status", [429, 503])
def test_normal_free_routes_fail_over_only_quota_and_remember_exhausted_key(
    tmp_path, monkeypatch, failure_status,
):
    import urllib.error
    import scripts.session_autoslice as runtime
    import src.autoslice.agy_gemini_client as gemini
    import src.autoslice.nested_media_acoustic_runtime as acoustic_runtime
    import src.autoslice.producer_final_review_transport as final_transport
    import src.autoslice.producer_nested_acoustic as producer

    monkeypatch.setattr(runtime, "load_gemini_credentials", lambda: {
        "GEMINI_API_KEY": "first-secret", "GEMINI_API_KEY_2": "second-secret",
        "GEMINI_API_KEY_3": "second-secret", "GEMINI_PAID_BACKUP_KEY": "paid-secret"})
    def cpa(_prompt):
        return "unused"
    cpa.cpa_cache_identity = {"transport": "fixture.cpa", "models": ["fixture"], "effort": "medium"}
    monkeypatch.setattr(final_transport, "build_final_review_llm_call", lambda **_kwargs: cpa)
    calls = []

    def generate(*, key, **_kwargs):
        calls.append(key)
        if key == "first-secret":
            raise urllib.error.HTTPError("https://example.invalid", failure_status, "failure", {}, None)
        return "actual response"

    monkeypatch.setattr(gemini, "generate_content", generate)
    captured = {}
    monkeypatch.setattr(acoustic_runtime, "build_runtime_acoustic_callbacks",
                        lambda **kwargs: captured.update(kwargs) or SimpleNamespace())
    monkeypatch.setattr(
        producer, "observe_prepared_nested_acoustics",
        lambda **kwargs: captured.update(kwargs) or {},
    )
    producer.build_runtime_nested_acoustic_observer(tmp_path)(spec={"nested_media_acoustic": {}})
    captured["callbacks_factory"](
        descriptor={}, candidate_id="candidate", observation_id="normal", evidence_root=tmp_path,
    )
    if failure_status == 429:
        assert captured["audio_generate"]() == "actual response"
        first_trace = captured["audio_attempts"]()
        assert [row["free_key_ordinal"] for row in first_trace] == [1, 2]
        assert first_trace[0]["http_status"] == 429
        assert "secret" not in str(first_trace)
        assert captured["audio_generate"]() == "actual response"
        assert calls == ["first-secret", "second-secret", "second-secret"]
        assert captured["audio_attempts"]() == [{"free_key_ordinal": 2, "status": "RESPONDED"}]
    else:
        with pytest.raises(urllib.error.HTTPError):
            captured["audio_generate"]()
        assert calls == ["first-secret"]
        assert captured["audio_attempts"]()[0]["http_status"] == 503


def test_provider_error_trace_is_preserved_in_normal_audit_without_cpa_dispatch(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    callbacks.observe_audio = _provider_error
    callbacks.cpa_judge = lambda *_args: pytest.fail("provider failure must not dispatch CPA")

    result = _run(inputs, callbacks, spec)
    row = result["observations"][0]

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "PROVIDER_ERROR"
    assert result["provider_calls"] == 1
    assert result["provider_attempts"] == [{
        "free_key_ordinal": 1,
        "status": "FAILED",
        "error_type": "HTTPError",
        "category": "GEMINI_API_SERVER_ERROR",
        "http_status": 503,
    }]
    assert row["reason_code"] == "PROVIDER_ERROR"
    assert row["provider_trace"]["http_status"] == 503
    assert row["provider_trace"]["request_sha256"] == row["observer_result"]["request_sha256"]
    assert row["provider_trace"]["request_file_sha256"].startswith("sha256:")
    assert row["provider_trace"]["observer_response_sha256"].startswith("sha256:")
    assert row["observer_reason_code"] == "PROVIDER_ERROR"
    assert row["action"] == "KEEP"
    assert row["label"] == "unknown"


def test_provider_trace_rejects_request_file_drift(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    callbacks.observe_audio = _provider_error
    callbacks.cpa_judge = lambda *_args: pytest.fail("provider failure must not dispatch CPA")

    result = _run(inputs, callbacks, spec)
    observer_result = result["observations"][0]["observer_result"]
    request_path = Path(observer_result["request_path"])
    request_path.write_text(request_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    from src.autoslice import producer_nested_acoustic as producer

    assert producer._observer_provider_trace(observer_result, inputs["evidence_root"]) is None


def test_replayed_provider_trace_is_historical_only_in_normal_audit(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    calls = {"observer": 0}

    def provider_error_once(request):
        calls["observer"] += 1
        return _provider_error(request)

    callbacks.observe_audio = provider_error_once
    callbacks.cpa_judge = lambda *_args: pytest.fail("provider failure must not dispatch CPA")

    first = _run(inputs, callbacks, spec)
    second = _run(inputs, callbacks, spec)
    first_attempt = first["provider_attempts"]

    assert calls == {"observer": 1}
    assert first["provider_calls"] == 1
    assert first["cpa_calls"] == 0
    assert second["observations"][0]["observer_result"]["replayed"] is True
    assert second["provider_calls"] == 0
    assert second["cpa_calls"] == 0
    assert second["provider_attempts"] == []
    assert second["historical_provider_attempts"] == first_attempt
    assert "reason_code" not in second
    row = second["observations"][0]
    assert "provider_attempts" not in row
    assert row["historical_provider_attempts"] == first_attempt
    assert row["historical_observer_reason_code"] == "PROVIDER_ERROR"


@pytest.mark.parametrize("jobs", [0, 1])
def test_normal_agy_trace_counts_native_jobs_and_preserves_unknown_api_count(tmp_path, jobs):
    inputs, callbacks, spec = _prepared(tmp_path)

    def observe(request):
        response = _observer(request)
        response["evidence"]["provider_attempts"] = [{
            "provider": "agy", "transport": "agy_three_part",
            "job_invocations": jobs, "historical_job_invocations": 1 - jobs,
            "replayed_completion": jobs == 0,
            "underlying_api_call_count": "UNKNOWN_UNEXPOSED",
            "process_path": "/private/secret-bearing-path",
            "argv": ["secret"], "api_key": "secret",
        }]
        return response

    callbacks.observe_audio = observe
    first = _run(inputs, callbacks, spec)
    attempt = first["provider_attempts"][0]
    assert first["provider_calls"] == jobs
    assert first["cpa_calls"] == 1
    assert attempt == {
        "provider": "agy", "transport": "agy_three_part",
        "job_invocations": jobs, "historical_job_invocations": 1 - jobs,
        "replayed_completion": jobs == 0,
        "underlying_api_call_count": "UNKNOWN_UNEXPOSED",
    }
    assert "secret" not in str(first["observations"][0]["provider_trace"])
    second = _run(inputs, callbacks, spec)
    assert second["provider_calls"] == second["cpa_calls"] == 0
    assert second["historical_provider_attempts"] == [attempt]


def test_replayed_cpa_path_is_historical_only_in_normal_audit(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    _role_callbacks(callbacks, "source_media")

    first = _run(inputs, callbacks, spec)
    second = _run(inputs, callbacks, spec)

    assert first["cpa_calls"] == 1
    assert second["observations"][0]["observer_result"]["replayed"] is True
    assert second["cpa_calls"] == 0
    assert second["historical_cpa_calls"] == 1


def _scene_spec_fixture(tmp_path: Path, *, scene: str = "WATCHED_MEDIA", captions=None):
    inputs = _inputs(tmp_path / "scene")
    subtitle = Path(inputs["final_srt_path"])
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nsource words\n", encoding="utf-8"
    )
    media = Path(inputs["outer_media_path"])
    frame = Path(inputs["caption_frame_path"])
    reference_dir = tmp_path / "references"
    reference_dir.mkdir()
    references = []
    for index in range(3):
        path = reference_dir / f"enrollment-{index + 1}.wav"
        path.write_bytes(f"enrollment-{index + 1}".encode())
        references.append({
            "id": f"ref-{index + 1}", "filename": path.name, "sha256": _bare_sha(path),
        })
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "schema_version": host_vocal.PROFILE_SCHEMA_VERSION,
        "profile_id": "scene-profile",
        "subject": host_vocal.CHANNEL_PROFILE.display_name,
        "model": {"model_id": "fixture-model", "tree_sha256": "sha256:" + "0" * 64},
        "references": references,
        "policy": host_vocal._canonical_policy(),
    }, sort_keys=True), encoding="utf-8")
    captions = ["source words"] if captions is None else captions
    evidence = tmp_path / "scene-evidence.json"
    document = {
        "schema_version": CAPTION_DEDUP_SCHEMA,
        "candidate_id": "candidate",
        "source_role": "WATCHED_MEDIA",
        "policy": CAPTION_DEDUP_POLICY,
        "source_media_sha256": _bare_sha(media),
        "source_interval_ms": [0, 1000],
        "input_transcript_sha256": _bare_sha(subtitle),
        "observations": [],
        "scene_observations": [{
            "frame_id": "cue-1", "cue_index": 1, "frame_ms": 500,
            "frame_path": str(frame), "frame_sha256": _bare_sha(frame),
            "scene": scene, "captions": captions,
        }],
    }
    evidence.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    spec = {
        "watched_media": True,
        "nested_media_caption_dedup": {
            "policy": CAPTION_DEDUP_POLICY,
            "evidence_path": str(evidence),
            "evidence_sha256": _bare_sha(evidence),
        },
        "pieces": [{"start_ms": 0, "end_ms": 1000}],
    }
    return inputs, spec, profile, reference_dir, document


def _bare_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_scene(tmp_path: Path, **kwargs):
    inputs, spec, profile, reference_dir, document = _scene_spec_fixture(tmp_path, **kwargs)
    result = prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], duration_ms=1000,
        profile_path=profile, reference_dir=reference_dir,
    )
    return inputs, spec, result, profile, reference_dir, document


def _external_audio_catalog(
    tmp_path: Path, *, expected_sha: str | None = None,
    record_url: str = "https://official.example/audio/hate.mp3",
) -> Path:
    audio = tmp_path / "official-button.mp3"
    audio.write_bytes(b"cached official button recording")
    actual_sha = hashlib.sha256(audio.read_bytes()).hexdigest()
    catalog = tmp_path / "external-audio-catalog.json"
    catalog.write_text(json.dumps({
        "schema_version": "nested-external-audio-catalog.v1",
        "button_map": {
            "source_url": "https://official.example/soundboard",
            "buttons": [{"label": "我讨厌你", "url": "https://official.example/audio/hate.mp3"}],
        },
        "audio_result": {
            "files": [{
                "label": "我讨厌你", "path": str(audio),
                "url": record_url,
                "file_sha256": expected_sha or actual_sha,
            }]
        },
    }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return catalog


def _external_button_scene(tmp_path: Path):
    inputs, spec, _result, profile, refs, document = _prepare_scene(tmp_path, captions=[])
    controls = {
        "visible_ui": ["喜欢你", "我讨厌你"],
        "button_under_cursor": "我讨厌你",
        "visible_site_or_url": "https://official.example/soundboard",
    }
    document["scene_observations"][0].update(
        scene="NOT_WATCHED_MEDIA", source_controls=controls,
    )
    document["source_role"] = EXTERNAL_AUDIO_SCENE_SOURCE_ROLE
    evidence_path = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    evidence_path.write_text(json.dumps(document, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    spec["nested_media_caption_dedup"]["evidence_sha256"] = _bare_sha(evidence_path)
    spec["nested_media_external_audio_scene"] = True
    return inputs, spec, profile, refs, document


def test_external_audio_button_ui_yields_only_evidence_reference_candidate(tmp_path, monkeypatch):
    inputs, spec, profile, refs, _document = _external_button_scene(tmp_path)
    catalog = _external_audio_catalog(tmp_path)
    monkeypatch.setenv(
        CATALOG_ENV,
        f"{catalog}#sha256:{hashlib.sha256(catalog.read_bytes()).hexdigest()}",
    )

    effective, audit = prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], duration_ms=1000,
        profile_path=profile, reference_dir=refs,
    )

    descriptor = effective["nested_media_acoustic"]["observations"][0]
    assert descriptor["source_cache_binding"]["kind"] == "OFFICIAL_EXTERNAL_AUDIO"
    assert descriptor["source_cache_binding"]["source_uri"].endswith("hate.mp3")
    assert descriptor["acoustic_only_timing_scope"]["kind"] == "CURRENT_WATCHED_MEDIA_CUE_NO_CAPTION"
    assert descriptor["acoustic_only_timing_scope"]["scene"] == "NOT_WATCHED_MEDIA"
    assert descriptor["visual_observation"]["caption_present"] is False
    assert descriptor["visual_observation"]["external_audio_candidate"]["audio_playback_proven"] is False
    assert descriptor["visual_observation"]["external_audio_candidate"]["source_role_authority"] is False
    assert audit["observations"][0]["action"] == "PREPARE_EXTERNAL_AUDIO_REFERENCE"
    assert audit["provider_calls"] == 0


def test_external_audio_current_cue_finds_recording_after_cursor_moves(tmp_path, monkeypatch):
    inputs, spec, profile, refs, document = _external_button_scene(tmp_path)
    subtitle = Path(inputs["final_srt_path"])
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n我讨厌你\n", encoding="utf-8")
    document["input_transcript_sha256"] = _bare_sha(subtitle)
    controls = document["scene_observations"][0]["source_controls"]
    controls["button_under_cursor"] = "喜欢你"
    evidence = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    evidence.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    spec["nested_media_caption_dedup"]["evidence_sha256"] = _bare_sha(evidence)
    catalog = _external_audio_catalog(tmp_path)
    monkeypatch.setenv(CATALOG_ENV, f"{catalog}#sha256:{_bare_sha(catalog)}")

    effective, audit = prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=subtitle, duration_ms=1000, profile_path=profile, reference_dir=refs,
    )

    descriptor = effective["nested_media_acoustic"]["observations"][0]
    candidate = descriptor["visual_observation"]["external_audio_candidate"]
    assert candidate["label"] == "我讨厌你"
    assert candidate["selection_basis"] == "CURRENT_CUE_EXACT_VISIBLE_LABEL"
    assert candidate["source_controls"]["button_under_cursor"] == "喜欢你"
    assert candidate["audio_playback_proven"] is False
    assert candidate["source_role_authority"] is False
    assert audit["provider_calls"] == audit["subtitle_mutations"] == 0


def test_external_audio_reply_can_prepare_visible_cached_voice_sample(tmp_path, monkeypatch):
    inputs, spec, profile, refs, document = _external_button_scene(tmp_path)
    subtitle = Path(inputs["final_srt_path"])
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n不许讨厌我\n", encoding="utf-8")
    document["input_transcript_sha256"] = _bare_sha(subtitle)
    controls = document["scene_observations"][0]["source_controls"]
    controls["button_under_cursor"] = None
    evidence = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    evidence.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    spec["nested_media_caption_dedup"]["evidence_sha256"] = _bare_sha(evidence)
    catalog = _external_audio_catalog(tmp_path)
    monkeypatch.setenv(CATALOG_ENV, f"{catalog}#sha256:{_bare_sha(catalog)}")

    effective, audit = prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=subtitle, duration_ms=1000, profile_path=profile, reference_dir=refs,
    )

    descriptor = effective["nested_media_acoustic"]["observations"][0]
    candidate = descriptor["visual_observation"]["external_audio_candidate"]
    assert candidate["label"] == "我讨厌你"
    assert candidate["reference_only"] is True
    assert candidate["selection_basis"] == "CURRENT_VISIBLE_LABEL_UNALIGNED_VOICE_REFERENCE"
    assert candidate["source_controls"]["button_under_cursor"] is None
    assert candidate["source_controls"]["visible_ui"] == controls["visible_ui"]
    assert candidate["audio_playback_proven"] is candidate["source_role_authority"] is False
    assert "observer_mode" not in descriptor  # normal preparation owns the binding
    assert audit["provider_calls"] == audit["subtitle_mutations"] == 0


@pytest.mark.parametrize("visible,expected_sha,record_url,expected", [
    (["讨厌你"], None, "https://official.example/audio/hate.mp3", "EXTERNAL_AUDIO_VISIBLE_REFERENCE_UNAVAILABLE"),
    (["我讨厌你"], "0" * 64, "https://official.example/audio/hate.mp3", "EXTERNAL_AUDIO_AUDIO_HASH_MISMATCH"),
    (["我讨厌你"], None, "https://official.example/audio/other.mp3", "EXTERNAL_AUDIO_CACHED_URL_MISMATCH"),
])
def test_visible_voice_reference_keeps_exact_label_url_and_bytes_guards(
    tmp_path, visible, expected_sha, record_url, expected,
):
    catalog = _external_audio_catalog(tmp_path, expected_sha=expected_sha, record_url=record_url)
    reference = resolve_external_audio_reference(
        {"visible_ui": visible, "button_under_cursor": None},
        catalog_value=f"{catalog}#sha256:{_bare_sha(catalog)}", cue_text="不许讨厌我",
        unaligned_visible_reference=True,
    )
    assert reference["status"] == "UNKNOWN_KEEP"
    assert reference["reason_code"] == expected


@pytest.mark.parametrize("visible,cue,expected", [
    (["喜欢你"], "我讨厌你", "EXTERNAL_AUDIO_BUTTON_LABEL_UNAVAILABLE"),
    (["喜欢你", "我讨厌您"], "我讨厌您", "EXTERNAL_AUDIO_BUTTON_LABEL_UNAVAILABLE"),
    (["喜欢你", "我讨厌你"], "我讨厌你！", "EXTERNAL_AUDIO_BUTTON_LABEL_UNAVAILABLE"),
])
def test_external_audio_cue_hint_requires_exact_visible_official_label(tmp_path, visible, cue, expected):
    catalog = _external_audio_catalog(tmp_path)
    reference = resolve_external_audio_reference(
        {"visible_ui": visible, "button_under_cursor": "喜欢你"},
        catalog_value=f"{catalog}#sha256:{_bare_sha(catalog)}", cue_text=cue,
    )
    assert reference["status"] == "UNKNOWN_KEEP"
    assert reference["reason_code"] == expected


def test_external_audio_cue_hint_still_rejects_cached_audio_drift(tmp_path):
    catalog = _external_audio_catalog(tmp_path, expected_sha="0" * 64)
    reference = resolve_external_audio_reference(
        {"visible_ui": ["喜欢你", "我讨厌你"], "button_under_cursor": "喜欢你"},
        catalog_value=f"{catalog}#sha256:{_bare_sha(catalog)}", cue_text="我讨厌你",
    )
    assert reference["status"] == "UNKNOWN_KEEP"
    assert reference["reason_code"] == "EXTERNAL_AUDIO_AUDIO_HASH_MISMATCH"


def test_external_audio_catalog_hash_mismatch_keeps_ui_candidate_unknown(tmp_path, monkeypatch):
    inputs, spec, profile, refs, _document = _external_button_scene(tmp_path)
    catalog = _external_audio_catalog(tmp_path, expected_sha="0" * 64)
    monkeypatch.setenv(
        CATALOG_ENV,
        f"{catalog}#sha256:{hashlib.sha256(catalog.read_bytes()).hexdigest()}",
    )

    effective, audit = prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], duration_ms=1000,
        profile_path=profile, reference_dir=refs,
    )

    assert "nested_media_acoustic" not in effective
    assert audit["observations"][0]["reason_code"] == "EXTERNAL_AUDIO_AUDIO_HASH_MISMATCH"
    assert audit["provider_calls"] == 0


def test_external_audio_catalog_url_mismatch_keeps_ui_candidate_unknown(tmp_path, monkeypatch):
    inputs, spec, profile, refs, _document = _external_button_scene(tmp_path)
    catalog = _external_audio_catalog(
        tmp_path, record_url="https://official.example/audio/other.mp3",
    )
    monkeypatch.setenv(
        CATALOG_ENV,
        f"{catalog}#sha256:{hashlib.sha256(catalog.read_bytes()).hexdigest()}",
    )

    effective, audit = prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], duration_ms=1000,
        profile_path=profile, reference_dir=refs,
    )

    assert "nested_media_acoustic" not in effective
    assert audit["observations"][0]["reason_code"] == "EXTERNAL_AUDIO_CACHED_URL_MISMATCH"
    assert audit["provider_calls"] == 0


def test_m33_independent_reference_rejects_outer_byte_copy(tmp_path):
    inputs, spec, profile, refs, _ = _scene_spec_fixture(tmp_path)
    media = Path(inputs["outer_media_path"])
    reference = tmp_path / "reference.mp4"
    reference.write_bytes(media.read_bytes())

    def prepare():
        spec["nested_media_external_audio_reference"] = {
            "candidate_id": "candidate", "outer_media_sha256": _bare_sha(media),
            "independent_source_media_path": str(reference),
            "independent_source_media_sha256": _bare_sha(reference),
            "source_cache_binding": {"kind": "OFFICIAL_EXTERNAL_AUDIO",
                "source_uri": "https://official.example/reference", "source_sha256": _bare_sha(reference)},
        }
        return prepare_final_scene_acoustic_spec(
            spec=spec, candidate_id="candidate", source_media=media,
            subtitle_path=inputs["final_srt_path"], duration_ms=1000,
            profile_path=profile, reference_dir=refs)[1]["external_audio_reference"]

    assert prepare()["reason_code"] == "INDEPENDENT_SOURCE_IS_OUTER_MEDIA"
    reference.write_bytes(b"independent reference bytes")
    assert prepare()["status"] == "EVIDENCE_ONLY"
    moved = tmp_path / "moved.mp4"
    moved.write_bytes(reference.read_bytes())
    reference = moved
    assert prepare()["status"] == "EVIDENCE_ONLY"


def test_final_scene_acoustic_spec_accepts_current_caption_binding(tmp_path):
    inputs, original, (effective, audit), _profile, _refs, _document = _prepare_scene(tmp_path)

    assert effective["nested_media_acoustic"]["schema_version"] == "nested-media-acoustic-inputs.v1"
    descriptor = effective["nested_media_acoustic"]["observations"][0]
    assert descriptor["interval_ms"] == [0, 1000]
    assert descriptor["caption_frame_ms"] == 500
    assert descriptor["visual_observation"]["role"] == "WATCHED_MEDIA_CAPTION"
    assert "independent_source_media_path" not in descriptor
    assert audit["descriptor_count"] == 1
    assert audit["subtitle_mutations"] == 0
    assert audit["publication_authorized"] is False
    assert inputs["outer_media_path"].exists()
    assert "nested_media_acoustic" not in original


def test_final_scene_acoustic_spec_accepts_no_caption_control(tmp_path):
    _inputs_value, _original, (effective, audit), _profile, _refs, _document = _prepare_scene(
        tmp_path, captions=[]
    )

    descriptor = effective["nested_media_acoustic"]["observations"][0]
    visual = descriptor["visual_observation"]
    assert visual["caption_present"] is False
    assert visual["role"] == "NO_EMBEDDED_CAPTION_OBSERVED"
    assert visual["visible_text"] == ""
    assert descriptor["acoustic_only_timing_scope"]["kind"] == "CURRENT_WATCHED_MEDIA_CUE_NO_CAPTION"
    assert descriptor["acoustic_only_timing_scope"]["visual_attribution"] == "NO_CAPTION_CURRENT_SCENE"
    assert audit["descriptor_count"] == 1


def test_final_scene_acoustic_spec_chooses_matching_caption_from_multiple_rows(tmp_path):
    _inputs_value, _original, (effective, audit), _profile, _refs, _document = _prepare_scene(
        tmp_path, captions=["unrelated", "source words"]
    )

    descriptor = effective["nested_media_acoustic"]["observations"][0]
    assert descriptor["visual_observation"]["visible_text"] == "source words"
    assert audit["descriptor_count"] == 1


@pytest.mark.parametrize(
    ("scene", "captions", "reason"),
    [
        ("UNKNOWN", [], "SCENE_UNKNOWN_KEEP"),
        ("NOT_WATCHED_MEDIA", [], "SCENE_NOT_WATCHED_MEDIA_KEEP"),
        ("WATCHED_MEDIA", ["unrelated text"], "SCENE_CAPTION_UNMATCHED_CURRENT_CUE"),
    ],
)
def test_final_scene_acoustic_spec_keeps_unknown_or_unmatched_rows(
    tmp_path, scene, captions, reason
):
    _inputs_value, _original, (effective, audit), _profile, _refs, _document = _prepare_scene(
        tmp_path, scene=scene, captions=captions
    )

    assert audit["observations"][0]["reason_code"] == reason
    if scene == "WATCHED_MEDIA":
        assert effective["nested_media_acoustic"]["observations"]
        descriptor = effective["nested_media_acoustic"]["observations"][0]
        scope = descriptor["acoustic_only_timing_scope"]
        assert descriptor["target_interval_ms"] == [0, 1000]
        assert descriptor["target_cue_indexes"] == [1]
        assert scope["kind"] == "CURRENT_WATCHED_MEDIA_CUE"
        assert scope["visual_attribution"] == "UNMATCHED_CURRENT_CUE"
        assert audit["descriptor_count"] == 1
        assert audit["observations"][0]["action"] == "KEEP"
    else:
        assert "nested_media_acoustic" not in effective
        assert audit["descriptor_count"] == 0


def _add_unmatched_scope_evidence(tmp_path, inputs, spec):
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    observation_id = "candidate-scene-1-cue-1"
    descriptor["observation_id"] = observation_id
    descriptor["visual_observation"]["observation_id"] = observation_id
    descriptor["visual_observation"]["visible_text"] = "unrelated text"
    descriptor["target_interval_ms"] = [100, 200]
    descriptor["target_cue_indexes"] = [1]
    evidence = tmp_path / "current-scene-evidence.json"
    document = {
        "schema_version": CAPTION_DEDUP_SCHEMA,
        "candidate_id": "candidate",
        "source_role": "WATCHED_MEDIA",
        "policy": CAPTION_DEDUP_POLICY,
        "source_media_sha256": _bare_sha(Path(inputs["outer_media_path"])),
        "source_interval_ms": [0, 200],
        "input_transcript_sha256": _bare_sha(Path(inputs["final_srt_path"])),
        "observations": [],
        "scene_observations": [{
            "frame_id": "cue-1", "cue_index": 1, "frame_ms": 150,
            "frame_path": str(inputs["caption_frame_path"]),
            "frame_sha256": _bare_sha(Path(inputs["caption_frame_path"])),
            "scene": "WATCHED_MEDIA", "captions": ["unrelated text"],
        }],
    }
    evidence.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    evidence_sha = _bare_sha(evidence)
    spec["nested_media_caption_dedup"] = {
        "policy": CAPTION_DEDUP_POLICY,
        "evidence_path": str(evidence),
        "evidence_sha256": evidence_sha,
    }
    descriptor["acoustic_only_timing_scope"] = {
        "schema_version": "nested-media-acoustic-timing-scope.v1",
        "kind": "CURRENT_WATCHED_MEDIA_CUE",
        "observation_id": observation_id,
        "scene": "WATCHED_MEDIA",
        "cue_index": 1,
        "cue_interval_ms": [100, 200],
        "frame_id": "cue-1",
        "frame_ms": 150,
        "caption_frame_ms": 150,
        "frame_path": str(Path(inputs["caption_frame_path"]).resolve()),
        "frame_sha256": _bare_sha(Path(inputs["caption_frame_path"])),
        "evidence_path": str(evidence),
        "evidence_sha256": evidence_sha,
        "visual_attribution": "UNMATCHED_CURRENT_CUE",
    }
    return evidence


def test_unmatched_current_caption_dispatches_audio_only_and_keeps_visual_unknown(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    _add_unmatched_scope_evidence(tmp_path, inputs, spec)

    result = _run(inputs, callbacks, spec)
    row = result["observations"][0]

    assert row["observer_result"]["status"] == "PASS"
    assert row["label"] == "source_media"
    assert row["visual_caption_label"] == "unknown"
    assert row["action"] == "KEEP"
    assert row["cue_interval_ms"] is None
    assert row["target_interval_ms"] == [100, 200]
    assert row["target_cue_indexes"] == [1]
    assert row["attribution"]["rows"][0]["visual_match"] == "unmatched"
    assert row["attribution"]["safe_to_drop_cue_indexes"] == []


def _add_no_caption_scope_evidence(tmp_path, inputs, spec):
    evidence = _add_unmatched_scope_evidence(tmp_path, inputs, spec)
    document = json.loads(evidence.read_text(encoding="utf-8"))
    document["scene_observations"][0]["captions"] = []
    evidence.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    descriptor["visual_observation"].update(
        caption_present=False, role="NO_EMBEDDED_CAPTION_OBSERVED", visible_text="",
    )
    scope = descriptor["acoustic_only_timing_scope"]
    scope.update(kind="CURRENT_WATCHED_MEDIA_CUE_NO_CAPTION",
                 visual_attribution="NO_CAPTION_CURRENT_SCENE",
                 evidence_sha256=_bare_sha(evidence))
    spec["nested_media_caption_dedup"]["evidence_sha256"] = _bare_sha(evidence)
    return evidence


@pytest.mark.parametrize("role", ["host", "source_media", "overlap"])
def test_no_caption_watched_scene_dispatches_audio_and_preserves_visual_control(tmp_path, role):
    inputs, callbacks, spec = _prepared(tmp_path)
    _add_no_caption_scope_evidence(tmp_path, inputs, spec)
    callbacks = _role_callbacks(callbacks, role)

    audit = _run(inputs, callbacks, spec)
    row = audit["observations"][0]

    assert row["observer_result"]["status"] == "PASS"
    assert row["label"] == role
    assert row["action"] == "KEEP"
    assert row["target_interval_ms"] == [100, 200]
    assert row["target_cue_indexes"] == [1]
    assert row["attribution"]["safe_to_drop_cue_indexes"] == []
    roles = current_caption_audio_roles(candidate_id="candidate", source_media=inputs["outer_media_path"],
                                        subtitle_path=inputs["final_srt_path"], acoustic_audit=audit)
    assert roles["unavailable_observations"] == []
    assert roles["rows"][0]["role"] == role
    assert roles["rows"][0]["action"] == "KEEP"


@pytest.mark.parametrize("drift", ["scene", "captions", "legacy_kind"])
def test_no_caption_scope_requires_its_actual_scene_and_mode(tmp_path, drift):
    inputs, callbacks, spec = _prepared(tmp_path)
    evidence = _add_no_caption_scope_evidence(tmp_path, inputs, spec)
    descriptor = spec["nested_media_acoustic"]["observations"][0]
    if drift == "legacy_kind":
        descriptor["acoustic_only_timing_scope"]["kind"] = "CURRENT_WATCHED_MEDIA_CUE"
    else:
        document = json.loads(evidence.read_text(encoding="utf-8"))
        document["scene_observations"][0].update(
            {"scene": "UNKNOWN"} if drift == "scene" else {"captions": ["source words"]}
        )
        evidence.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        descriptor["acoustic_only_timing_scope"]["evidence_sha256"] = _bare_sha(evidence)
        spec["nested_media_caption_dedup"]["evidence_sha256"] = _bare_sha(evidence)
    callbacks.observe_audio = lambda *_args: pytest.fail("invalid no-caption scope dispatched")
    callbacks.cpa_judge = lambda *_args: pytest.fail("invalid no-caption scope judged")

    row = _run(inputs, callbacks, spec)["observations"][0]

    assert row["label"] == "unknown"
    assert row["action"] == "KEEP"
    assert "observer_result" not in row


@pytest.mark.parametrize("role", ["host", "overlap"])
def test_unmatched_current_caption_handoff_preserves_verified_non_source_roles(tmp_path, role):
    inputs, callbacks, spec = _prepared(tmp_path)
    _add_unmatched_scope_evidence(tmp_path, inputs, spec)
    callbacks = _role_callbacks(callbacks, role)

    row = _run(inputs, callbacks, spec)["observations"][0]

    assert row["observer_result"]["status"] == "PASS"
    assert row["label"] == role
    assert row["visual_caption_label"] == "unknown"
    assert row["action"] == "KEEP"
    assert row["attribution"]["rows"][0]["acoustic_receipt"]["final_class"] == role
    assert row["attribution"]["safe_to_drop_cue_indexes"] == []


def test_unmatched_current_caption_evidence_drift_blocks_audio_dispatch(tmp_path):
    inputs, callbacks, spec = _prepared(tmp_path)
    evidence = _add_unmatched_scope_evidence(tmp_path, inputs, spec)
    evidence.write_text(evidence.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    callbacks.observe_audio = lambda *_args: pytest.fail("drifted scene evidence dispatched")
    callbacks.cpa_judge = lambda *_args: pytest.fail("drifted scene evidence judged")

    result = _run(inputs, callbacks, spec)
    row = result["observations"][0]
    assert row["reason_code"] == "CURRENT_ACOUSTIC_SCOPE_EVIDENCE_HASH_MISMATCH"
    assert row["label"] == "unknown"
    assert row["action"] == "KEEP"


def test_unmatched_current_caption_receipt_drift_never_promotes_role(tmp_path, monkeypatch):
    inputs, callbacks, spec = _prepared(tmp_path)
    _add_unmatched_scope_evidence(tmp_path, inputs, spec)
    from src.autoslice import producer_nested_acoustic as producer

    original = producer.build_caption_source_attribution

    def drift_receipt(**kwargs):
        bindings = kwargs["acoustic_receipt_bindings"]
        binding = bindings[0]
        Path(binding["path"]).write_text(
            Path(binding["path"]).read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        return original(**kwargs)

    monkeypatch.setattr(producer, "build_caption_source_attribution", drift_receipt)
    row = _run(inputs, callbacks, spec)["observations"][0]

    assert row["reason_code"] == "NESTED_ACOUSTIC_EVIDENCE_UNAVAILABLE"
    assert row["label"] == "unknown"
    assert row["action"] == "KEEP"
    assert "visual_caption_label" not in row


@pytest.mark.parametrize("drift", ["media", "srt", "interval"])
def test_final_scene_acoustic_spec_rejects_current_binding_drift(tmp_path, drift):
    inputs, spec, _result, profile, reference_dir, document = _prepare_scene(tmp_path)
    evidence_path = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    if drift == "media":
        Path(inputs["outer_media_path"]).write_bytes(b"changed-media")
    elif drift == "srt":
        Path(inputs["final_srt_path"]).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nchanged words\n", encoding="utf-8"
        )
    else:
        document["source_interval_ms"] = [0, 999]
        evidence_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        spec["nested_media_caption_dedup"]["evidence_sha256"] = _bare_sha(evidence_path)
    effective, audit = prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], duration_ms=1000,
        profile_path=profile, reference_dir=reference_dir,
    )
    assert "nested_media_acoustic" not in effective
    assert audit["reason_code"] in {
        "CURRENT_CAPTION_DEDUP_BINDING_MISMATCH",
        "OUTER_MEDIA_UNAVAILABLE",
        "FINAL_SRT_UNAVAILABLE",
    }


def test_final_scene_acoustic_spec_missing_enrollment_blocks(tmp_path):
    inputs, spec, _result, profile, reference_dir, document = _prepare_scene(tmp_path)
    profile_payload = json.loads(profile.read_text(encoding="utf-8"))
    for reference in profile_payload["references"]:
        reference["sha256"] = "0" * 64
    profile.write_text(json.dumps(profile_payload, sort_keys=True), encoding="utf-8")
    effective, audit = prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], duration_ms=1000,
        profile_path=profile, reference_dir=reference_dir,
    )

    assert "nested_media_acoustic" not in effective
    assert audit["reason_code"] == "HOST_ENROLLMENT_UNAVAILABLE"


def test_final_scene_acoustic_spec_preserves_explicit_descriptor_byte_equivalent(tmp_path):
    inputs, spec, _result, profile, reference_dir, _document = _prepare_scene(tmp_path)
    spec["nested_media_acoustic"] = {"schema_version": "old", "observations": [{"partial": True}]}
    before = json.dumps(spec, sort_keys=True)
    effective, audit = prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], duration_ms=1000,
        profile_path=profile, reference_dir=reference_dir,
    )

    assert json.dumps(effective, sort_keys=True) == before
    assert audit["status"] == "PRESERVED"
    assert audit["reason_code"] == "EXISTING_NESTED_ACOUSTIC_PRESERVED"


def test_final_scene_context_covers_entire_matched_caption_span(tmp_path):
    inputs, spec, profile, reference_dir, document = _scene_spec_fixture(tmp_path)
    subtitle = Path(inputs["final_srt_path"])
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nsource\n\n2\n00:00:08,000 --> 00:00:10,000\nwords\n")
    document["input_transcript_sha256"] = _bare_sha(subtitle)
    document["source_interval_ms"] = [0, 20000]
    evidence = Path(spec["nested_media_caption_dedup"]["evidence_path"])
    evidence.write_text(json.dumps(document, sort_keys=True))
    spec["nested_media_caption_dedup"]["evidence_sha256"] = _bare_sha(evidence)
    effective, audit = prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=subtitle, duration_ms=20000, profile_path=profile, reference_dir=reference_dir)
    assert audit["descriptor_count"] == 1
    assert effective["nested_media_acoustic"]["observations"][0]["interval_ms"] == [0, 14000]
    assert effective["nested_media_acoustic"]["observations"][0]["target_interval_ms"] == [0, 10000]
    assert effective["nested_media_acoustic"]["observations"][0]["target_cue_indexes"] == [1, 2]


@pytest.mark.parametrize("attached_to_primary", [False, True])
def test_caption_search_candidate_requires_normal_mapping_before_role(tmp_path, monkeypatch, attached_to_primary):
    from tests.test_nested_source_cache import _automatic_inputs, _caption_discovery

    inputs, callbacks, spec, cache_root, _ = _automatic_inputs(tmp_path)
    spec["nested_media_source_locator"] = _caption_discovery(tmp_path / "discovery", inputs)
    if attached_to_primary:
        spec["nested_media_source_locator"] = {
            "status": "UNKNOWN_KEEP", "reason_code": "OUTER_SOURCE_HINT_EMPTY", "action": "KEEP",
            "caption_candidate_discovery": spec["nested_media_source_locator"],
        }
    original = copy.deepcopy(spec)
    measurements = []

    def prepare(**kwargs):
        measurements.append(kwargs)
        assert kwargs["source_identity"] == {"bvid": "BVTEST123456", "cid": 123456}
        assert Path(kwargs["source_media"]) == Path(inputs["independent_source_media_path"])
        return {"decision": "UNKNOWN_KEEP"}

    def unavailable(*args, **kwargs):
        raise AssertionError("a caption search candidate cannot authorize a role call")

    monkeypatch.setattr("src.autoslice.nested_source_audio_alignment.prepare_nested_source_mapping", prepare)
    callbacks.observe_audio = callbacks.cpa_judge = unavailable
    result = observe_prepared_nested_acoustics(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], evidence_root=inputs["evidence_root"],
        callbacks=callbacks, source_cache_root=cache_root,
    )
    row = result["observations"][0]
    assert len(measurements) == 1
    assert row["caption_source_candidate"]["selection_policy"] == "FIRST_AVAILABLE_PUBLIC_SEARCH_CANDIDATE"
    assert row["source_cache_binding"]["source_role_authority"] is False
    assert row["reason_code"] == "SOURCE_MAPPING_UNSUPPORTED"
    assert row["label"] == "unknown" and row["action"] == "KEEP"
    assert result["provider_calls"] == 0
    assert spec == original
