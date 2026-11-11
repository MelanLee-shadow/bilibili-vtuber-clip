"""Protocol fixtures only: no production ASR, provider, or acoustic quality claim."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.source_checkpoint_parent import (
    binding, build_source_checkpoint_parent, validate_source_checkpoint_parent,
)
from src.autoslice.producer_text_checkpoint import write_source_text_checkpoint
from src.autoslice.retained_source_input import load_retained_source_input
from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
from src.autoslice.supplement_audio_budget import get_budget, start_budget
from tests.test_producer_current_text_auto_resume import (
    _checkpoint, _make_source_stage_checkpoint, _stub_source_stage_reconstruction,
    _pipeline_kwargs,
)
from tests.test_retained_source_input import _seed_native, _files, _forbidden


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False))
    return path


def parent_fixture(tmp_path, monkeypatch, *, source_bytes=None, cid="output", original_substrate=False):
    # Existing native source-stage fixture, with actual ordinary provenance and
    # exact frozen typed audit. Tiny synthetic media is never an AV acceptance.
    origin = tmp_path / "parent"
    origin.mkdir()
    c = _checkpoint(origin, cid=cid)
    if cid != "output":
        old_output, new_output = c["out_root"], origin / cid
        old_output.rename(new_output)
        for key, value in list(c.items()):
            if isinstance(value, Path) and value.is_relative_to(old_output):
                c[key] = new_output / value.relative_to(old_output)
    original_pieces = None
    if source_bytes is not None:
        from src.autoslice import producer_source_media as media
        from src.autoslice.clip_context import build_clip_context
        source = tmp_path / f"{cid}.recording.mp4"
        source.write_bytes(source_bytes)
        c["spec"]["pieces"] = [{"remote_media": str(source), "start_ms": 0, "end_ms": 100}]
        original_pieces = copy.deepcopy(c["spec"]["pieces"])
        # Only the AV codec is substituted. The ordinary source inspector,
        # binder, provenance writer and context producer all run.
        def codec(command, **_kwargs):
            Path(command[-1]).write_bytes(source_bytes)
        monkeypatch.setattr(media, "run", codec)
        monkeypatch.setattr(media, "ffprobe_duration_ms", lambda _path: 100)
        prepared = media.prepare_source_media(
            spec=c["spec"], cid=cid, out_root=c["out_root"], host="localhost")
        c["padded"] = prepared.padded
        context = build_clip_context(
            candidate_id=cid, spec=c["spec"], draft_srt=c["srt"], authoritative_chat=[],
            topic_resolution={}, session_topic_authorities=[],
            speech_memory_ledger_path=Path(__file__).resolve().parents[1] / "assets/lidousha/speech_memory_ledger.v1.json")
        _write(c["context_path"], context)
    if original_substrate:
        original = origin/'original-substrate.srt'
        original.write_text(c['srt'].replace('当前文字', '原始初稿'))
        authority = json.loads(c['authority_path'].read_bytes())
        authority['input_srt_sha256'] = hashlib.sha256(original.read_bytes()).hexdigest()
        _write(c['authority_path'], authority)
        c['spec']['existing_subtitle_surface'] = {
            'path': str(original), 'sha256': 'sha256:'+authority['input_srt_sha256'],
            'time_domain': 'padded_local'}
    _make_source_stage_checkpoint(c)
    _stub_source_stage_reconstruction(monkeypatch, c)
    c["spec"].update(candidate_id=c["cid"], output_root=str(c["out_root"].parent),
                     runtime_root=str(tmp_path))
    old = c["padded"]
    padded = c["out_root"] / old.name
    old.rename(padded)
    prov = json.loads(old.with_suffix(".provenance.json").read_bytes())
    prov["output_path"] = str(padded)
    _write(padded.with_suffix(".provenance.json"), prov)
    c["padded"] = padded
    authority = json.loads(c["authority_path"].read_bytes())
    authority["final_review_audit"]["context_adjudication_count"] = 2
    _write(c["authority_path"], authority)
    source = c["out_root"] / f'{c["cid"]}.source-text-checkpoint.json'
    write_source_text_checkpoint(source, candidate_id=c["cid"],
        srt_path=c["out_root"] / "padded.fresh.srt",
        authority_path=c["authority_path"], context_path=c["context_path"], padded=padded)
    # A later failed review increments actual usage; old SOURCE still has 2.
    authority["final_review_audit"].update(context_adjudication_count=4,
        status="FLAGGED", findings=[{"reason": "synthetic unresolved boundary"}])
    _write(c["authority_path"], authority)
    _seed_native(c["out_root"], padded, start=10, end=20)
    from src.autoslice.native_audio_budget_receipt import resume_native_audio_budget
    resume_native_audio_budget(source_media=padded,
        source_media_sha256=hashlib.sha256(padded.read_bytes()).hexdigest(),
        receipt_path=c["out_root"] / "native-audio-budget.json", require_existing=True)
    budget = get_budget(padded)
    budget.consume("mai", "MAI-Transcribe-2", 30, 40)
    # Native unknown is an unclosed DISPATCHED event, never a fabricated final status.
    persist_native_audio_budget_receipt(source_media=padded,
        source_media_sha256=hashlib.sha256(padded.read_bytes()).hexdigest(),
        receipt_path=c["out_root"] / "native-audio-budget.json")
    journal = c["out_root"] / f'{c["cid"]}.cpa-usage.jsonl'
    journal.write_text(json.dumps({"event_type": "model_request", "kind": "text",
        "status": "OUTCOME_UNKNOWN", "production_attempt_id": "fixture-original"}) + "\n")
    original_spec = copy.deepcopy(c["spec"])
    if original_pieces is not None:
        original_spec["pieces"] = original_pieces
    parent_spec = _write(tmp_path / "parent-spec.json", original_spec)
    config = build_source_checkpoint_parent(parent_spec_path=parent_spec, checkpoint_path=source)
    spec = copy.deepcopy(original_spec)
    output = tmp_path / "successor"
    spec.update(output_root=str(output),
        retained_source_input=config,
        source_text_checkpoint={k: config["source_checkpoint"][k] for k in ("path", "sha256")})
    frozen = json.loads(source.read_bytes())
    spec["existing_subtitle_surface"] = {
        "schema_version": "existing-subtitle-surface.v1", "candidate_id": c["cid"],
        **frozen["srt"], "source_media_sha256": frozen["padded"]["sha256"],
        "time_domain": "padded_local",
        "authority_kind": "PIPELINE_REVIEWED_CPA_SURFACE_NOT_HUMAN_TRUTH"}
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    return c, spec, output / c["cid"]


@pytest.mark.parametrize('cid', ['output', 'new-language-source'])
@pytest.mark.parametrize('damage', [None, 'original_bytes', 'parent_bytes'])
def test_source_successor_reads_original_language_from_verified_parent(tmp_path, monkeypatch, cid, damage):
    c, successor, _ = parent_fixture(tmp_path, monkeypatch, cid=cid, original_substrate=True)
    authority = json.loads(c['authority_path'].read_bytes())
    original = tmp_path/'parent'/'original-substrate.srt'
    expected = original.read_text()
    assert expected != c['srt']
    if damage == 'original_bytes':
        original.write_text(expected+'\n')
    elif damage == 'parent_bytes':
        parent = Path(successor['retained_source_input']['parent_spec']['path'])
        parent.write_bytes(parent.read_bytes()+b' ')
    before = _files(tmp_path)
    if damage:
        with pytest.raises((ValueError, pipeline.CurrentTextContinuationError),
                           match='SOURCE_CHECKPOINT_PARENT|SOURCE_LANGUAGE_WITNESS'):
            pipeline._source_stage_original_language_witness(authority, successor, c['padded'], 100)
    else:
        assert pipeline._source_stage_original_language_witness(
            authority, successor, c['padded'], 100) == expected
    assert _files(tmp_path) == before


def test_normal_source_parent_restores_stage_accounts_and_unknown_without_asr(tmp_path, monkeypatch):
    c, spec, output = parent_fixture(tmp_path, monkeypatch)
    before = _files(c["out_root"])
    output.mkdir(parents=True)
    kwargs = {**_pipeline_kwargs(c), "spec": spec, "out_root": output}
    state = load_retained_source_input(spec=spec, cid=c["cid"], padded=c["padded"], out_root=output)
    assert state.cpa_usage["provider_adjudication_count"] == 4
    assert get_budget(c["padded"]).snapshot()["attempt_count"] == 3
    assert any(a["status"] == "DISPATCHED"
               for a in get_budget(c["padded"]).snapshot()["attempts"])
    snapshot = pipeline._automatic_current_checkpoint_snapshot(**{
        k: kwargs[k] for k in ("spec", "durations", "padded", "padded_dur", "cid", "out_root")})
    assert snapshot["source_usage_parent"] is not None
    assert snapshot["source_accounting_parent"]["native_budget"]["sha256"] == binding(c["out_root"] / "native-audio-budget.json")["sha256"]
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", _forbidden)
    result = pipeline.run_text_pipeline(**kwargs)
    assert result.srt_text == c["srt"]
    assert result.chat_authority_audit["cpa_adjudication_usage"]["provider_adjudication_count"] == 4
    assert result.chat_authority_audit["source_input_observation_parent"]["release_authorized"] is False
    assert result.chat_authority_audit["final_review_audit"]["boundary_semantic_review"]["status"] == "BLOCK"
    assert _files(c["out_root"]) == before


@pytest.mark.parametrize("damage", ["candidate", "piece", "owner", "date", "runtime", "account_namespace", "semantic_start", "semantic_end", "content_anchor", "required_owner", "unverified_boundary", "original_output", "source_declaration",
                                     "parent_bytes", "checkpoint_bytes", "usage_bytes", "budget", "journal"])
def test_source_parent_rejects_identity_clock_owner_or_history_drift(tmp_path, monkeypatch, damage):
    c, spec, output = parent_fixture(tmp_path, monkeypatch)
    if damage == "candidate":
        spec["candidate_id"] = "other"
    elif damage == "piece":
        spec["pieces"][0]["start_ms"] += 1
    elif damage == "owner":
        spec["human_truth_mode"] = "delivery"
    elif damage == "date":
        spec["date"] = "2026-10-11"
    elif damage == "runtime":
        spec["runtime_root"] = str(tmp_path / "new-account")
    elif damage == "account_namespace":
        monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "reset-account"))
    elif damage in {"semantic_start", "semantic_end"}:
        spec[damage + "_ms"] += 1
    elif damage == "content_anchor":
        spec["selection_hook"] = "changed story anchor"
    elif damage == "required_owner":
        spec["required_boundary_owners"] = []
    elif damage == "unverified_boundary":
        spec["given_end_mode"] = "published_recall_anchor"
        spec["given_end_ms"] = 80
    elif damage == "original_output":
        spec["output_root"] = str(c["out_root"].parent)
    elif damage == "source_declaration":
        spec["source_text_checkpoint"]["sha256"] = "sha256:" + "0" * 64
    else:
        key = {"parent_bytes": "parent_spec", "checkpoint_bytes": "source_checkpoint",
               "usage_bytes": "live_authority", "budget": "native_budget", "journal": "cpa_journal"}[damage]
        path = Path(spec["retained_source_input"][key]["path"])
        path.write_bytes(path.read_bytes() + b" ")
    monkeypatch.setattr(pipeline, "_transcribe_draft", _forbidden)
    with pytest.raises(ValueError, match="SOURCE_CHECKPOINT_PARENT"):
        validate_source_checkpoint_parent(spec)


def test_source_parent_rejects_rebuilt_counter_rollback(tmp_path, monkeypatch):
    c, spec, output = parent_fixture(tmp_path, monkeypatch)
    live = json.loads(c["authority_path"].read_bytes())
    live["final_review_audit"]["context_adjudication_count"] = 1
    _write(c["authority_path"], live)
    config = spec["retained_source_input"]
    with pytest.raises(ValueError, match="CPA_HISTORY_REGRESSION"):
        build_source_checkpoint_parent(parent_spec_path=Path(config["parent_spec"]["path"]),
                                       checkpoint_path=Path(config["source_checkpoint"]["path"]))


def test_new_boundary_mode_preserves_physical_novelty_identity(tmp_path, monkeypatch):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    c, spec, output = parent_fixture(tmp_path, monkeypatch)
    context = json.loads(c["context_path"].read_bytes())
    finding = {"cue_index": 1, "why": "synthetic same physical concern"}
    old = SemanticAttemptRecorder(srt_text=c["srt"], finding=finding, clip_context=context, offset_ms=0)
    spec.update(given_end_mode="published_recall_anchor", given_end_ms=80, semantic_tail_trim_cap_ms=15000)
    new = SemanticAttemptRecorder(srt_text=c["srt"], finding=finding, clip_context=context, offset_ms=0)
    assert old.issue == new.issue and old.directory == new.directory
    assert spec["retained_source_input"]["cpa_journal"] == binding(c["out_root"] / f'{c["cid"]}.cpa-usage.jsonl')


@pytest.mark.parametrize("cid,source_bytes", [("witness", b"first source"),
                                            ("existing-other", b"different source bytes"),
                                            ("new-member", b"unseen ordinary candidate")])
def test_unbound_candidate_roundtrips_source_writer_parent_and_runtime_reader(tmp_path, monkeypatch, cid, source_bytes):
    c, spec, output = parent_fixture(tmp_path, monkeypatch, cid=cid, source_bytes=source_bytes)
    from src.autoslice import producer_source_media as media
    parent_path = Path(spec["retained_source_input"]["parent_spec"]["path"])
    before = parent_path.read_bytes()
    assert "source_media_sha256" not in json.loads(before)["pieces"][0]
    persisted = _write(tmp_path / "successor.json", spec)
    restored = json.loads(persisted.read_bytes())
    output.mkdir(parents=True)
    prepared = media.prepare_source_media(spec=restored, cid=cid, out_root=output, host="localhost")
    validate_source_checkpoint_parent(restored, runtime=True)
    state = load_retained_source_input(spec=restored, cid=cid, padded=prepared.padded, out_root=output)
    assert state.cpa_usage["provider_adjudication_count"] == 4
    expected = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
    assert restored["pieces"][0]["source_media_sha256"] == expected
    assert restored["retained_source_input"]["source_media_sha256s"] == [expected]
    assert parent_path.read_bytes() == before


@pytest.mark.parametrize("damage", ["wrong_source", "missing_source", "clock", "bad_summary", "missing_checkpoint"])
def test_unbound_source_restoration_preserves_rejections(tmp_path, monkeypatch, damage):
    c, spec, _ = parent_fixture(tmp_path, monkeypatch, source_bytes=b"verified source")
    config = spec["retained_source_input"]
    source = Path(c["spec"]["pieces"][0]["remote_media"])
    if damage == "wrong_source":
        source.write_bytes(b"another recording")
    elif damage == "missing_source":
        source.unlink()
    elif damage == "clock":
        parent = json.loads(Path(config["parent_spec"]["path"]).read_bytes())
        parent["pieces"][0]["start_ms"] += 1
        _write(Path(config["parent_spec"]["path"]), parent)
    elif damage == "bad_summary":
        spec["retained_source_input"]["source_media_sha256s"] = ["sha256:" + "0" * 64]
    else:
        Path(config["source_checkpoint"]["path"]).unlink()
    with pytest.raises((ValueError, RuntimeError)):
        validate_source_checkpoint_parent(spec)


def test_explicit_source_successor_keeps_scope_and_discloses_current_drift(tmp_path, monkeypatch):
    from src.autoslice.source_checkpoint_parent import build_source_checkpoint_successor
    c, spec, _ = parent_fixture(tmp_path, monkeypatch, source_bytes=b"source snapshot")
    live = json.loads(c["authority_path"].read_bytes())
    live["final_output_srt_sha256"] = hashlib.sha256(b"later CURRENT").hexdigest()
    _write(c["authority_path"], live)
    parent = Path(spec["retained_source_input"]["parent_spec"]["path"])
    checkpoint = Path(spec["source_text_checkpoint"]["path"])
    with pytest.raises(ValueError, match="SOURCE_AUTHORITY_DRIFT"):
        build_source_checkpoint_parent(parent_spec_path=parent, checkpoint_path=checkpoint)
    successor = build_source_checkpoint_successor(parent_spec_path=parent, output_root=tmp_path / "source-successor")
    restored = json.loads(_write(tmp_path / "source-successor.json", successor).read_bytes())
    validated = validate_source_checkpoint_parent(restored)
    assert validated["source_authority_observation"]["differing_fields"] == ["final_output_srt_sha256"]
    assert validated["release_authorized"] is False
    output = Path(restored["output_root"]) / c["cid"]
    output.mkdir(parents=True)
    from src.autoslice.producer_source_media import prepare_source_media
    prepare_source_media(spec=restored, cid=c["cid"], out_root=output, host="localhost")
    state = load_retained_source_input(spec=restored, cid=c["cid"], padded=c["padded"], out_root=output)
    assert state.cpa_usage["provider_adjudication_count"] == 4
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", _forbidden)
    result = pipeline.run_text_pipeline(**{**_pipeline_kwargs(c), "spec": restored, "out_root": output})
    assert result.srt_text == c["srt"]
    assert result.chat_authority_audit["source_input_observation_parent"]["release_authorized"] is False
    for field in ("semantic_start_ms", "semantic_tail_trim_cap_ms", "given_end_ms"):
        changed = copy.deepcopy(restored)
        changed[field] = 999
        with pytest.raises(ValueError, match="SPEC_SCOPE_DRIFT"):
            validate_source_checkpoint_parent(changed)
    changed = copy.deepcopy(restored)
    changed["restore_source_checkpoint"] = False
    with pytest.raises(ValueError, match="SPEC_SCOPE_DRIFT"):
        validate_source_checkpoint_parent(changed)
    live["final_review_audit"]["context_adjudication_count"] = 1
    _write(c["authority_path"], live)
    with pytest.raises(ValueError, match="CPA_HISTORY_REGRESSION"):
        build_source_checkpoint_successor(parent_spec_path=parent, output_root=tmp_path / "regressed")


def test_source_successor_survives_ordinary_request_enrichment_only_with_parent_profile(tmp_path, monkeypatch):
    import argparse
    from src.autoslice import channel_profile as channel, producer_request as producer
    from src.autoslice.source_checkpoint_parent import build_source_checkpoint_successor
    c, spec, _ = parent_fixture(tmp_path, monkeypatch, source_bytes=b"ordinary request source")
    parent = Path(spec["retained_source_input"]["parent_spec"]["path"])
    successor = build_source_checkpoint_successor(parent_spec_path=parent, output_root=tmp_path / "request-output")
    parent_raw = parent.read_bytes()
    path = _write(tmp_path / "request-spec.json", successor)
    raw = path.read_bytes()
    repo = Path(__file__).resolve().parents[1]
    selected = channel.load_channel_profile(repo, profile_id="lidousha", environ={})
    for key, value in channel.profile_environment(selected, {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", successor["human_truth_mode"])
    monkeypatch.setattr(producer, "require_branding_intro", lambda *_a, **_k: None)
    monkeypatch.setattr(producer, "validate_producer_runtime_prerequisites", lambda **_k: None)
    args = argparse.Namespace(spec=path, ssh_host="localhost", speaker_mode="uniform_host",
        subtitle_text_overrides=None, subtitle_regression=None, speaker_overrides=None)
    request = producer.load_producer_request(args, repo_root=repo,
        profile_asset_file=selected.asset_file, selected_channel_profile=selected,
        trusted_parent_profile_binding=channel.binding_for_profile(selected))
    validate_source_checkpoint_parent(request.spec, runtime=True)
    assert path.read_bytes() == raw and parent.read_bytes() == parent_raw
    changed = copy.deepcopy(request.spec)
    changed["profile_binding"]["profile_id"] = "different-channel"
    with pytest.raises(ValueError, match="SPEC_SCOPE_DRIFT"):
        validate_source_checkpoint_parent(changed)
    changed = copy.deepcopy(request.spec)
    changed["argon_escalation"]["allow_paid"] = False
    with pytest.raises(ValueError, match="SPEC_SCOPE_DRIFT"):
        validate_source_checkpoint_parent(changed)
    monkeypatch.delenv(channel.PROFILE_BINDING_ENV)
    with pytest.raises(ValueError):
        validate_source_checkpoint_parent(request.spec)
