"""Ordinary retained-source inputs preserve bytes and accounts, not verdicts."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import producer_source_media as media
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
from src.autoslice.retained_source_input import load_retained_source_input
from src.autoslice.subtitle_audio_evidence import CONTRACT, _digest, observe_secondary
from src.autoslice.supplement_audio_budget import get_budget, start_budget
from tests.test_producer_source_media import _install_retained_parent


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return "sha256:" + _sha(path)


def _files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _forbidden(*_args, **_kwargs):
    pytest.fail("retained input must not dispatch an ASR or provider call")


def _seed_native(parent, padded, *, start=1000, end=1500, enforced=False):
    """Two real-format completed cache entries for this exact padded source."""
    budget = start_budget(padded, max_windows=1 if enforced else 3,
                          enforce_limits=enforced)
    for index, (provider, model) in enumerate((
        ("mai", "MAI-Transcribe-2"), ("moss", "moss-transcribe-diarize-pro"),
    )):
        token = budget.consume(provider, model, start, end)
        budget.finish_attempt(token, status="OBSERVED")
        job = parent / "native-exact" / provider / f"{_sha(padded)[:20]}-{start}-{end}"
        job.mkdir(parents=True)
        audio = job / "input.mp3"
        audio.write_bytes(b"bounded synthetic audio" + bytes([index]))
        binding = {
            "contract": CONTRACT, "provider": provider, "model": model,
            "input_audio_sha256": _sha(audio), "duration_ms": end - start,
            "candidate_exposure": "none",
        }
        evidence = {**binding, "status": "OBSERVED", "raw_response": {"text": "fixture"}}
        _write(audio.with_suffix(f".audio-evidence.{provider}.json"), {
            "binding": binding, "evidence": evidence, "evidence_sha256": _digest(evidence),
        })
    receipt = parent / "native-audio-budget.json"
    persist_native_audio_budget_receipt(
        source_media=padded, source_media_sha256=_sha(padded), receipt_path=receipt,
    )
    snapshot = budget.snapshot()
    # Simulate a restarted producer's empty ledger with the same declared
    # policy; the input loader must restore every historical event.
    start_budget(padded, max_windows=1 if enforced else 3, enforce_limits=enforced)
    return snapshot


def _attach_usage_parent(spec, *, padded, parent, out_root, runtime, srt, context,
                         duration=4000, start=1000, end=1500, enforced=False):
    parent.mkdir(parents=True, exist_ok=True)
    cid = spec["candidate_id"]
    original_native = _seed_native(parent, padded, start=start, end=end, enforced=enforced)
    authority_path = parent / f"{cid}.chat-authority.json"
    authority = {
        "schema_version": "chat-authority-audit.v2", "mutation_authorized": False,
        "cpa_adjudication_usage": {
            "policy": "SOFT", "provider_adjudication_count": 18,
            "prior_provider_adjudication_count": 18, "soft_limit": 12,
        },
        # This is deliberately a different old final. The padded CPA-reviewed
        # substrate is an input, not a fabricated retained-final projection.
        "final_output_srt_sha256": hashlib.sha256("旧终稿".encode()).hexdigest(),
        "final_review_audit": {"status": "CLEAN", "release_gate": "PASS",
                               "old_verdict_marker": "must not cross the input boundary"},
    }
    context_path = parent / f"{cid}.clip-context.json"
    record_path = parent / f"{cid}.json"
    record = {
        "candidate_id": cid, "chat_authority_audit_path": str(authority_path),
        "clip_context_path": str(context_path),
        "artifact_hashes": {
            "chat_authority_audit_sha256": _write(authority_path, authority),
            "clip_context_file_sha256": _write(context_path, context),
        },
    }
    srt_path = parent / "padded.cpa-reviewed.srt"
    srt_path.write_text(srt, encoding="utf-8")
    retained_media = spec.pop("retained_current_text", {
        "padded_sha256": "sha256:" + _sha(padded),
        "parent_materialization_path": str(padded),
        "parent_materialization_sha256": "sha256:" + _sha(padded),
        "parent_materialization_duration_ms": duration,
        "parent_materialization_provenance_path": str(padded.with_suffix(".provenance.json")),
        "parent_materialization_provenance_sha256": "sha256:" + _sha(padded.with_suffix(".provenance.json")),
        "source_media_sha256s": [piece["source_media_sha256"] for piece in spec["pieces"]],
    })
    spec.update(
        output_root=str(out_root.parent), runtime_root=str(runtime), human_truth_mode="withheld",
        local_audio_witness_provider="mai",
        retained_source_input={
            **retained_media, "schema_version": "retained-source-input.v1", "candidate_id": cid,
            "usage_parent": {"path": str(record_path), "sha256": _write(record_path, record),
                             "native_budget_sha256": "sha256:" + _sha(parent / "native-audio-budget.json")},
        },
        existing_subtitle_surface={
            "schema_version": "existing-subtitle-surface.v1", "candidate_id": cid,
            "path": str(srt_path), "sha256": "sha256:" + _sha(srt_path),
            "source_media_sha256": "sha256:" + _sha(padded), "time_domain": "padded_local",
            "authority_kind": "PIPELINE_REVIEWED_CPA_SURFACE_NOT_HUMAN_TRUTH",
        },
    )
    return {"authority": authority_path, "context": context_path, "record": record_path,
            "native": parent / "native-audio-budget.json", "subtitle": srt_path,
            "native_snapshot": original_native}


def _fixture(tmp_path, monkeypatch, *, enforced=False):
    from src.autoslice.speech_memory_ledger import SCHEMA_VERSION

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    speech_memory = runtime / "fixture-speech-memory.json"
    _write(speech_memory, {"schema_version": SCHEMA_VERSION, "entries": []})
    fixture = _install_retained_parent(runtime)
    spec, padded = fixture["spec"], fixture["padded"]
    spec["date"] = "2026-10-11"
    cid = spec["candidate_id"]
    out_root = runtime / "successors" / "first" / cid
    out_root.mkdir(parents=True)
    srt = "1\n00:00:00,500 --> 00:00:01,500\n喝口水\n\n2\n00:00:02,000 --> 00:00:03,000\n接着说\n"
    context = {
        "schema_version": "lidousha-clip-context.v1", "candidate_id": cid,
        "pieces": [{"start_ms": 1000, "end_ms": 5000,
                    "source_media_sha256": "sha256:" + fixture["source_sha"],
                    "recording_basename": fixture["source"].name}],
    }
    bindings = _attach_usage_parent(spec, padded=padded, parent=fixture["parent_root"],
        out_root=out_root, runtime=runtime, srt=srt, context=context, enforced=enforced)
    return {**fixture, **bindings, "runtime": runtime, "out_root": out_root,
            "cid": cid, "srt": srt, "speech_memory": speech_memory}


def _kwargs(fixture, *, out_root=None):
    return dict(spec=fixture["spec"], durations=[4000], padded=fixture["padded"],
        padded_dur=4000, host="localhost", text_override_path=None, cid=fixture["cid"],
        out_root=out_root or fixture["out_root"], substrate="existing_srt", correct="cpa",
        screen_text=False, adapters=SimpleNamespace(
            build_aggregate_transcriber=_forbidden, build_agy_transcriber=_forbidden,
            load_term_boundary_surfaces=lambda _spec: (),
            profile_asset_file=lambda name: (
                fixture["speech_memory"]
                if name == "speech_memory_ledger" else fixture["runtime"] / "unused-assets" / name),
            topic_graph_disabled=lambda: True,
        ))


def _prepare_media(fixture, monkeypatch):
    monkeypatch.setattr(media, "_source_media_sha256", lambda _host, path: (str(path.resolve()), _sha(path)))
    monkeypatch.setattr(media, "ffprobe_duration_ms", lambda _path: 4000)
    monkeypatch.setattr(media, "run", _forbidden)
    return media.prepare_source_media(spec=fixture["spec"], cid=fixture["cid"],
        out_root=fixture["out_root"], host="localhost", spec_parent=fixture["runtime"])


class _ReachedCurrentReview(Exception):
    pass


def test_prepared_retained_media_recovery_reads_original_pieces(tmp_path, monkeypatch):
    fixture = _fixture(tmp_path, monkeypatch)
    before = _files(fixture["parent_root"])
    prepared = _prepare_media(fixture, monkeypatch)
    assert prepared.padded.parent != fixture["out_root"]
    padded_sha, source_shas = pipeline._automatic_current_source_bindings(
        spec=fixture["spec"], padded=prepared.padded, out_root=fixture["out_root"],
        durations=prepared.durations,
    )
    assert padded_sha == _sha(prepared.padded)
    assert source_shas == tuple(piece["source_media_sha256"] for piece in fixture["spec"]["pieces"])
    assert _files(fixture["parent_root"]) == before
    assert not list(fixture["out_root"].glob("piece_*.mp4"))


@pytest.mark.parametrize("damage", ["path", "provenance", "piece", "undeclared"])
def test_retained_recovery_rejects_media_binding_drift(tmp_path, monkeypatch, damage):
    fixture = _fixture(tmp_path, monkeypatch)
    prepared = _prepare_media(fixture, monkeypatch)
    retained = fixture["spec"]["retained_source_input"]
    if damage == "path":
        retained["parent_materialization_path"] = str(fixture["out_root"] / prepared.padded.name)
    elif damage == "provenance":
        retained["parent_materialization_provenance_sha256"] = "sha256:" + "0" * 64
    elif damage == "piece":
        (prepared.padded.parent / "piece_0_1000_5000.mp4").write_bytes(b"damaged piece")
    else:
        del fixture["spec"]["retained_source_input"]
    with pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._automatic_current_source_bindings(
            spec=fixture["spec"], padded=prepared.padded, out_root=fixture["out_root"],
            durations=prepared.durations,
        )


@pytest.mark.parametrize("stop_after_review", [False, True])
def test_ordinary_existing_srt_reaches_fresh_review_with_old_usage_and_shared_native_account(tmp_path, monkeypatch, stop_after_review):
    from src.autoslice import entity_audio_verifier, native_context_witness

    fixture = _fixture(tmp_path, monkeypatch)
    original = _files(fixture["parent_root"])
    prepared = _prepare_media(fixture, monkeypatch)
    assert prepared.padded == fixture["padded"]
    assert "retained_current_text" not in fixture["spec"]
    assert fixture["spec"]["existing_subtitle_surface"]["sha256"] != (
        "sha256:" + json.loads(fixture["authority"].read_bytes())["final_output_srt_sha256"])

    constructors, reviews = [], []
    def native_factory(**kwargs):
        constructors.append(kwargs)
        return _forbidden

    monkeypatch.setattr(pipeline, "discover_session_topic_authorities", lambda _spec: [])
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    monkeypatch.setattr(pipeline, "build_ssh_silero_vad_provider", lambda _host:
        lambda *_args: [SimpleNamespace(start_ms=0, end_ms=4000)])
    monkeypatch.setattr(pipeline, "build_env_screen_read_probe", lambda *_args: None)
    monkeypatch.setattr(pipeline, "witness_audio_locally_resolvable", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(entity_audio_verifier, "build_local_audio_entity_verifier", lambda **_kwargs: _forbidden)
    monkeypatch.setattr(native_context_witness, "build_native_context_verifier", native_factory)
    monkeypatch.setattr(native_context_witness, "build_independent_context_witnesses", native_factory)
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: _forbidden)
    monkeypatch.setattr(pipeline, "load_referent_groups", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(pipeline, "_review_priority_candidates", lambda *_args: [])
    monkeypatch.setattr(pipeline, "fidelity_kept_contexts", lambda *_args: [])
    def fresh_authority(**kwargs):
        assert kwargs["retained_reviewed_draft"] is False
        return SimpleNamespace(srt_text=kwargs["srt_text"], chat_authority_audit={"applied": []},
            transcript_entity_audit={}, handled_entity_cues=set())
    monkeypatch.setattr(pipeline, "_apply_entity_authority", fresh_authority)
    def current_review(**kwargs):
        reviews.append(kwargs)
        assert kwargs["srt_text"] == fixture["srt"]
        audit = kwargs["chat_authority_audit"]
        assert audit["cpa_adjudication_usage"]["provider_adjudication_count"] == 18
        assert kwargs["initial_provider_adjudication_count"] == 18
        assert audit["retained_source_input"]["release_authorized"] is False
        assert "final_review_audit" not in audit and "old_verdict_marker" not in json.dumps(audit)
        assert kwargs["clip_context"]["whole_clip_draft_srt"] == fixture["srt"]
        if not stop_after_review:
            raise _ReachedCurrentReview
        return fixture["srt"], {"schema_version": "final-review-audit.v1", "status": "APPLIED",
            "context_adjudication_count": 3, "cpa_adjudication_usage": {
                "prior_provider_adjudication_count": 18, "provider_adjudication_count": 21}}
    monkeypatch.setattr(pipeline, "_run_final_review", current_review)
    def finalize(**kwargs):
        assert kwargs["chat_authority_audit"]["cpa_adjudication_usage"]["provider_adjudication_count"] == 21
        raise _ReachedCurrentReview
    if stop_after_review:
        monkeypatch.setattr(pipeline, "_finalize_text_evidence", finalize)

    with pytest.raises(_ReachedCurrentReview):
        pipeline.run_text_pipeline(**_kwargs(fixture))

    assert len(reviews) == 1 and len(constructors) == 2
    receipt = fixture["spec"]["existing_subtitle_surface_receipt"]
    assert receipt["asr_calls"] == receipt["provider_calls"] == 0
    canonical = fixture["runtime"] / ".native-audio-resume" / fixture["cid"] / _sha(fixture["padded"])
    assert {row["budget_receipt_path"] for row in constructors} == {canonical / "native-audio-budget.json"}
    assert {row["native_observation_cache_root"] for row in constructors} == {canonical}
    assert get_budget(fixture["padded"]).snapshot() == fixture["native_snapshot"]
    assert _files(fixture["parent_root"]) == original


@pytest.mark.parametrize("enforced", [False, True])
def test_new_output_restart_consumes_original_native_cache_without_reset(tmp_path, monkeypatch, enforced):
    from src.autoslice import diarized_transcription, supplement_audio_budget

    fixture = _fixture(tmp_path, monkeypatch, enforced=enforced)
    original = _files(fixture["parent_root"])
    kwargs = {key: _kwargs(fixture)[key] for key in ("spec", "cid", "padded", "out_root")}
    # An actual new process has no registry entry, including no prior policy.
    monkeypatch.delitem(supplement_audio_budget._REGISTRY, str(fixture["padded"].resolve()))
    first = load_retained_source_input(**kwargs)
    assert get_budget(fixture["padded"]).snapshot() == fixture["native_snapshot"]
    monkeypatch.setattr(diarized_transcription, "transcribe_evidence", _forbidden)
    for provider in ("mai", "moss"):
        audio = first.native.cache_root / "native-exact" / provider / f"{_sha(fixture['padded'])[:20]}-1000-1500" / "input.mp3"
        result = observe_secondary(audio.read_bytes(), media_path=audio, provider=provider,
            duration_ms=500, supplement_source=fixture["padded"], crop_start_ms=1000,
            crop_end_ms=1500, exact_cue=True)
        assert result["served_from_cache"] is True
    persist_native_audio_budget_receipt(source_media=fixture["padded"],
        source_media_sha256=_sha(fixture["padded"]), receipt_path=first.native.budget_receipt)
    after_cache = get_budget(fixture["padded"]).snapshot()
    assert after_cache["attempt_count"] == fixture["native_snapshot"]["attempt_count"] == 2
    assert after_cache["revision"] > fixture["native_snapshot"]["revision"]
    assert after_cache["enforce_limits"] is enforced
    monkeypatch.delitem(supplement_audio_budget._REGISTRY, str(fixture["padded"].resolve()))
    next_output = fixture["runtime"] / "other-output" / fixture["cid"]
    next_output.mkdir(parents=True)
    second = load_retained_source_input(**{**kwargs, "out_root": next_output})
    assert second == first
    assert get_budget(fixture["padded"]).snapshot() == after_cache
    assert _files(fixture["parent_root"]) == original


@pytest.mark.parametrize("damage", ["record", "authority", "context", "padded", "native",
                                     "source_scope", "cache_audio", "cache_receipt"])
def test_bound_parent_drift_stops_ordinary_entry_before_asr_or_provider(tmp_path, monkeypatch, damage):
    fixture = _fixture(tmp_path, monkeypatch)
    if damage == "source_scope":
        context = json.loads(fixture["context"].read_bytes())
        context["pieces"][0]["start_ms"] += 1
        record = json.loads(fixture["record"].read_bytes())
        record["artifact_hashes"]["clip_context_file_sha256"] = _write(fixture["context"], context)
        fixture["spec"]["retained_source_input"]["usage_parent"]["sha256"] = _write(fixture["record"], record)
    elif damage.startswith("cache_"):
        pattern = "input.mp3" if damage == "cache_audio" else "*.json"
        next((fixture["parent_root"] / "native-exact").rglob(pattern)).write_bytes(b"drift")
    else:
        fixture[damage].write_bytes(fixture[damage].read_bytes() + b"drift")
    original = _files(fixture["parent_root"])
    monkeypatch.setattr(pipeline, "_transcribe_draft", _forbidden)
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", _forbidden)
    with pytest.raises((ValueError, RuntimeError), match="DRIFT|MISMATCH"):
        _prepare_media(fixture, monkeypatch)
        pipeline.run_text_pipeline(**_kwargs(fixture))
    assert _files(fixture["parent_root"]) == original


def test_partial_checkpoint_freezes_and_resumes_canonical_native_ledger(tmp_path, monkeypatch):
    from tests.test_producer_current_text_auto_resume import (
        _checkpoint, _interrupt_partial_native_emitter, _pipeline_kwargs,
    )

    runtime = tmp_path.resolve()
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    checkpoint = _checkpoint(runtime)
    checkpoint["spec"]["candidate_id"] = checkpoint["cid"]
    # Normal materialization keeps padded media and pieces in one directory.
    # The older generic checkpoint fixture intentionally separates them.
    original_padded = checkpoint["padded"]
    checkpoint["padded"] = checkpoint["out_root"] / original_padded.name
    original_padded.rename(checkpoint["padded"])
    padded_provenance = json.loads(original_padded.with_suffix(".provenance.json").read_bytes())
    padded_provenance["output_path"] = str(checkpoint["padded"].resolve())
    _write(checkpoint["padded"].with_suffix(".provenance.json"), padded_provenance)
    parent = runtime / "original-usage-parent"
    bindings = _attach_usage_parent(checkpoint["spec"], padded=checkpoint["padded"],
        parent=parent, out_root=checkpoint["out_root"], runtime=runtime,
        srt=checkpoint["srt"], context=json.loads(checkpoint["context_path"].read_bytes()),
        duration=100, start=10, end=20)
    original = _files(parent)
    state = load_retained_source_input(spec=checkpoint["spec"], cid=checkpoint["cid"],
        padded=checkpoint["padded"], out_root=checkpoint["out_root"])
    initial_ledger = state.native.budget_receipt.read_bytes()
    budget = get_budget(checkpoint["padded"])
    def append_failure(start):
        attempt = budget.consume("mai", "MAI-Transcribe-2", start, start + 5)
        budget.finish_attempt(attempt, status="FAILED", reason_code="FIXTURE_TRANSPORT_FAILURE")
        persist_native_audio_budget_receipt(source_media=checkpoint["padded"],
            source_media_sha256=_sha(checkpoint["padded"]), receipt_path=state.native.budget_receipt)
    append_failure(30)
    frozen_ledger = state.native.budget_receipt.read_bytes()
    pointer = _interrupt_partial_native_emitter(checkpoint, monkeypatch)
    manifest = json.loads(pointer.read_bytes())
    assert Path(manifest["budget"]["path"]).read_bytes() == frozen_ledger
    assert not (checkpoint["out_root"] / "native-audio-budget.json").exists()
    append_failure(40)
    expected = budget.snapshot()
    start_budget(checkpoint["padded"])
    snapshot_args = {key: value for key, value in _pipeline_kwargs(checkpoint).items()
                     if key in {"spec", "durations", "padded", "padded_dur", "cid", "out_root"}}
    snapshot = pipeline._automatic_current_checkpoint_snapshot(**snapshot_args)
    assert snapshot["partial_source_text"] is True
    assert get_budget(checkpoint["padded"]).snapshot() == expected
    assert expected["attempt_count"] == 4
    assert _files(parent) == original

    # A reset to the old parent still has a valid receipt hash, but is older
    # than this checkpoint's frozen account. It cannot become fresh allowance.
    state.native.budget_receipt.write_bytes(initial_ledger)
    start_budget(checkpoint["padded"])
    with pytest.raises(pipeline.CurrentTextContinuationError, match="PARTIAL_NATIVE_BUDGET_DRIFT"):
        pipeline._automatic_current_checkpoint_snapshot(**snapshot_args)
    assert bindings["native_snapshot"]["attempt_count"] == 2
