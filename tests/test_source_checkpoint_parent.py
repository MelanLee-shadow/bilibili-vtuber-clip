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


def parent_fixture(tmp_path, monkeypatch):
    # Existing native source-stage fixture, with actual ordinary provenance and
    # exact frozen typed audit. Tiny synthetic media is never an AV acceptance.
    origin = tmp_path / "parent"
    origin.mkdir()
    c = _checkpoint(origin, cid="output")
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
    parent_spec = _write(tmp_path / "parent-spec.json", c["spec"])
    config = build_source_checkpoint_parent(parent_spec_path=parent_spec, checkpoint_path=source)
    spec = copy.deepcopy(c["spec"])
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
