from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice import retained_reviewed_draft as retained_reader
from src.autoslice import retained_preparation_checkpoint as checkpoint
from src.autoslice.producer_text_checkpoint import write_partial_source_text_checkpoint
from src.autoslice.subtitle_regression_recovery import without_provider_dispatch
from tests import test_producer_current_text_auto_resume as specimen


def _sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _retained_fixture(tmp_path):
    data = specimen._checkpoint(tmp_path)
    specimen._make_source_stage_checkpoint(data)
    root = data["out_root"]
    proof = {
        "candidate_id": data["cid"], "source_srt_sha256": _sha(data["srt"].encode()),
        "parent_authority_sha256": "sha256:" + "b" * 64,
        "parent_clip_context_sha256": "sha256:" + "c" * 64,
        "filler_plan_sha256": "sha256:" + "d" * 64,
        "padded_sha256": _sha(data["padded"].read_bytes()),
        "source_timeline_offset_ms": 0, "source_endpoint": {"start_ms": 0, "end_ms": 100},
        "pending_entity_reviews": [], "release_authorized": False,
    }
    retained = {
        "proof": proof, "srt_text": data["srt"], "padded_sha256": proof["padded_sha256"],
        "piece_output_sha256s": [_sha((root / "piece_0_0_100.mp4").read_bytes())],
        "source_media_sha256s": ["sha256:" + data["source_sha"]],
        "parent_authority": {"cpa_adjudication_usage": {"provider_adjudication_count": 7}},
    }
    authority = json.loads(data["authority_path"].read_bytes())
    authority.pop("final_output_srt_sha256", None)
    authority["retained_reviewed_draft"] = copy.deepcopy(proof)
    _, truth = pipeline.apply_source_subtitle_truth(
        data["srt"], spec=data["spec"], durations=[100], ledger_path=None)
    authority["source_truth_preview_receipts"] = {
        stage: pipeline.build_source_truth_preview_receipt(
            input_srt_text=data["srt"], source_truth_audit=truth, stage=stage)
        for stage in ("pre_entity_arbitration", "pre_correction_review")}
    authority["cpa_adjudication_usage"] = {"provider_adjudication_count": 7, "policy": "SOFT"}
    return data, retained, authority


def _write_partial(data, retained, authority):
    path = data["out_root"] / f'{data["cid"]}.partial-source-text-checkpoint.json'
    write_partial_source_text_checkpoint(
        path, candidate_id=data["cid"], srt_text=data["srt"], authority=authority,
        final_review=authority["final_review_audit"], context_path=data["context_path"],
        padded=data["padded"], source_language_witness_srt="",
        protected_name_source_srt=data["srt"], retained_reviewed_input=retained)
    return path


def _snapshot(data, retained):
    return pipeline._automatic_current_checkpoint_snapshot(
        spec=data["spec"], durations=[100], padded=data["padded"], padded_dur=100,
        cid=data["cid"], out_root=data["out_root"], validated_retained=retained)


def test_retained_normal_finalize_writes_before_foreign_provider(tmp_path, monkeypatch):
    data, retained, authority = _retained_fixture(tmp_path)
    def stop(*args, **kwargs):
        raise RuntimeError("before foreign provider")
    monkeypatch.setattr(pipeline._foreign_scope, "guard_source_language", stop)
    with without_provider_dispatch(), pytest.raises(RuntimeError, match="before foreign provider"):
        pipeline._finalize_text_evidence(
            spec=data["spec"], durations=[100], srt_text=data["srt"],
            chat_authority_audit=authority, transcript_entity_audit=authority["transcript_entity_audit"],
            referent_groups=[], final_review_audit=authority["final_review_audit"],
            song_name_candidates=[], known_songs_path=tmp_path/"known.json",
            session_topic_authorities=(), source_language_witness_srt="",
            protected_name_source_srt=data["srt"], text_override_path=None,
            source_truth_ledger_path=None, out_root=data["out_root"], cid=data["cid"],
            padded=data["padded"], clip_context=json.loads(data["context_path"].read_bytes()),
            retained_reviewed_input=retained)
    saved = json.loads((data["out_root"]/f'{data["cid"]}.partial-source-text-checkpoint.json').read_bytes())
    assert saved["schema_version"] == checkpoint.RETAINED_PARTIAL_SCHEMA
    assert saved["source_language_witness"] == "NOT_AVAILABLE_RETAINED_INPUT"
    assert "language_witness" not in saved
    assert saved["completed_stage"] == "post_review"
    assert saved["pending_stage"] == "source_finalization"
    assert saved["release_authorized"] is False


def test_retained_partial_cold_snapshot_and_normal_entry_keep_blank_witness(tmp_path, monkeypatch):
    data, retained, authority = _retained_fixture(tmp_path)
    path = _write_partial(data, retained, authority)
    before = path.read_bytes()
    with without_provider_dispatch():
        first = _snapshot(data, retained)
        second = _snapshot(data, copy.deepcopy(retained))
    assert first["srt_text"] == second["srt_text"] == data["srt"]
    assert first["partial_source_text"] is True
    assert path.read_bytes() == before
    data["spec"]["retained_current_text"] = {"configured": True}
    data["spec"]["human_truth_mode"] = "withheld"
    monkeypatch.setattr(retained_reader, "load_retained_reviewed_draft", lambda **kwargs: retained)
    monkeypatch.setattr(pipeline, "_collect_timeline_chat", lambda *_args: ([], []))
    monkeypatch.setattr(pipeline, "_transcribe_draft", lambda **kwargs: specimen._fake_draft(kwargs["retained_srt_text"]))
    monkeypatch.setattr(pipeline, "_build_entity_verification_context",
                        lambda **kwargs: SimpleNamespace(referent_groups=[]))
    def stop(**kwargs):
        assert kwargs["srt_text"] == data["srt"]
        assert kwargs["source_language_witness_srt"] == ""
        assert kwargs["retained_reviewed_input"] == retained
        raise RuntimeError("ordinary retained finalization reached")
    monkeypatch.setattr(pipeline, "_finalize_text_evidence", stop)
    with without_provider_dispatch(), pytest.raises(RuntimeError, match="ordinary retained finalization reached"):
        pipeline.run_text_pipeline(**specimen._pipeline_kwargs(data))


@pytest.mark.parametrize("damage", ["parent", "audio", "context", "piece", "clock", "missing_proof", "proof_hash", "invented_witness"])
def test_retained_partial_drift_rejected_before_provider(tmp_path, damage):
    data, retained, authority = _retained_fixture(tmp_path)
    path = _write_partial(data, retained, authority)
    native = json.loads(path.read_bytes())
    if damage == "parent":
        retained["proof"]["parent_authority_sha256"] = "sha256:" + "e" * 64
    elif damage == "audio":
        data["padded"].write_bytes(b"changed")
    elif damage == "context":
        data["context_path"].write_text("{}")
        Path(native["context"]["path"]).write_text("{}")
    elif damage == "piece":
        (data["out_root"]/"piece_0_0_100.mp4").write_bytes(b"changed")
    elif damage == "clock":
        retained["proof"]["source_timeline_offset_ms"] = 1
    elif damage == "missing_proof":
        Path(native["retained_input"]["path"]).unlink()
    elif damage == "proof_hash":
        native["retained_input"]["sha256"] = "sha256:" + "f" * 64
        path.write_text(json.dumps(native))
    elif damage == "invented_witness":
        native["language_witness"] = native["srt"]
        path.write_text(json.dumps(native))
    with without_provider_dispatch(), pytest.raises((ValueError, OSError, pipeline.CurrentTextContinuationError)):
        _snapshot(data, retained)


def _completion_fixture(tmp_path, monkeypatch):
    data, retained, authority = _retained_fixture(tmp_path)
    proof = retained["proof"]
    (data["out_root"]/f'{data["cid"]}.retained-entity-review-input.json').write_text(json.dumps(proof))
    # Component tests exercise storage/selection/clock; the real 149e
    # pending-entity decoder is exercised separately on the frozen B receipt.
    calls = []
    def validator(retained, snapshot):
        calls.append(snapshot)
    monkeypatch.setattr(retained_reader, "validate_pending_entity_checkpoint", validator, raising=False)
    saved = {
        "schema_version": checkpoint.SCHEMA, "status": "CURRENT_REVIEW_COMPLETED",
        "candidate_id": data["cid"], "parent_authority_sha256": proof["parent_authority_sha256"],
        "input_srt_text": data["srt"], "output_srt_text": data["srt"],
        "input_srt_sha256": _sha(data["srt"].encode()), "output_srt_sha256": _sha(data["srt"].encode()),
        "pending_entity_reviews": [], "completed": [], "pending": [],
        "mutation_authorized": False, "release_authorized": False,
        "normal_correction_review": {"schema_version": "final-review-audit.v1", "status": "APPLIED",
            "findings": [], "applied_count": 0, "context_adjudication_count": 0,
            "cpa_adjudication_usage": {"prior_provider_adjudication_count": 7,
                                     "provider_adjudication_count": 7, "policy": "SOFT"}},
    }
    return data, retained, authority, saved, calls


def _put_completion(data, saved):
    raw = json.dumps(saved).encode()
    path = data["out_root"]/f'{data["cid"]}.retained-entity-review.{hashlib.sha256(raw).hexdigest()}.json'
    path.write_bytes(raw)
    return path


def _load_completion(data, retained):
    return checkpoint.load_completed_retained_review(
        retained=retained, out_root=data["out_root"], cid=data["cid"],
        clip_context=json.loads(data["context_path"].read_bytes()), required=True,
        spec=data["spec"], durations=[100], padded=data["padded"], padded_dur=100)


def test_completed_receipt_uses_current_consumer_and_does_not_reset_usage(tmp_path, monkeypatch):
    data, retained, authority, saved, calls = _completion_fixture(tmp_path, monkeypatch)
    path = _put_completion(data, saved)
    before = path.read_bytes()
    with without_provider_dispatch():
        restored = _load_completion(data, retained)
        text, review = checkpoint.consume_completed_retained_review(
            restored, input_srt_text=data["srt"], authority=authority,
            clip_context=json.loads(data["context_path"].read_bytes()))
        cold = _load_completion(data, retained)
    assert cold == restored and path.read_bytes() == before and len(calls) == 2
    assert text == saved["output_srt_text"]
    assert review["cpa_adjudication_usage"]["provider_adjudication_count"] == 7
    assert authority["retained_entity_review"]["sha256"] == _sha(before)
    assert restored["release_authorized"] is False


@pytest.mark.parametrize("damage,reason", [
    ("candidate", "CLIP_CONTEXT_CANDIDATE_MISMATCH"),
    ("context", "RETAINED_PREPARATION_CURRENT_CONTEXT_DRIFT"),
])
def test_completed_receipt_rejects_rebuilt_context_before_restoring_owners(
    tmp_path, monkeypatch, damage, reason,
):
    data, retained, authority, saved, _ = _completion_fixture(tmp_path, monkeypatch)
    _put_completion(data, saved)
    restored = _load_completion(data, retained)
    context = json.loads(data["context_path"].read_bytes())
    context.pop("context_sha256")
    context["candidate_id" if damage == "candidate" else "selection_hook"] = "changed"
    from src.autoslice.clip_context import _canonical_json_sha256
    context["context_sha256"] = _canonical_json_sha256(context)
    original_authority = copy.deepcopy(authority)
    with without_provider_dispatch(), pytest.raises(ValueError, match=reason):
        checkpoint.consume_completed_retained_review(
            restored, input_srt_text=data["srt"], authority=authority, clip_context=context)
    assert authority == original_authority


@pytest.mark.parametrize("damage", ["missing", "unknown", "hash", "parent", "candidate", "output", "grid", "count", "release", "proof_clock"])
def test_completed_receipt_negative_controls_are_zero_provider(tmp_path, monkeypatch, damage):
    data, retained, authority, saved, calls = _completion_fixture(tmp_path, monkeypatch)
    if damage == "unknown":
        saved["status"] = "PENDING_CURRENT_REVIEW"
    elif damage == "parent":
        saved["parent_authority_sha256"] = "sha256:" + "a"*64
    elif damage == "candidate":
        saved["candidate_id"] = "other"
    elif damage == "output":
        saved["output_srt_sha256"] = "sha256:" + "a"*64
    elif damage == "grid":
        saved["output_srt_text"] = saved["output_srt_text"].replace("00,050", "00,060")
        saved["output_srt_sha256"] = _sha(saved["output_srt_text"].encode())
    elif damage == "count":
        saved["normal_correction_review"]["cpa_adjudication_usage"]["provider_adjudication_count"] = 0
    elif damage == "release":
        saved["release_authorized"] = True
    elif damage == "proof_clock":
        retained["proof"]["source_timeline_offset_ms"] = 1
    path = _put_completion(data, saved)
    if damage == "missing":
        path.unlink()
    elif damage == "hash":
        path.write_bytes(path.read_bytes()+b" ")
    with without_provider_dispatch(), pytest.raises((ValueError, pipeline.CurrentTextContinuationError)):
        _load_completion(data, retained)


def _replayed_authority_fixture(tmp_path, monkeypatch):
    data, retained, _, saved, _ = _completion_fixture(tmp_path, monkeypatch)
    _put_completion(data, saved)
    completed = _load_completion(data, retained)
    context = json.loads(data["context_path"].read_bytes())
    authority = checkpoint.completed_retained_input_authority(
        completed, input_srt_text=data["srt"], clip_context=context)
    authority["retained_reviewed_draft"] = copy.deepcopy(retained["proof"])
    text, review = checkpoint.consume_completed_retained_review(
        completed, input_srt_text=data["srt"], authority=authority, clip_context=context)
    authority["final_review_audit"] = review
    pipeline.retain_post_transcript_text(authority, text)
    return data, retained, completed, context, authority


def test_completed_input_native_partial_cold_does_not_fabricate_entity_success(tmp_path, monkeypatch):
    data, retained, completed, context, authority = _replayed_authority_fixture(tmp_path, monkeypatch)
    assert authority["status"] == checkpoint.REPLAYED_INPUT_STATUS
    assert authority["transcript_entity_audit"]["status"] == "NOT_REEXECUTED_COMPLETED_RETAINED_INPUT"
    assert authority["retained_preparation_input"]["pre_entity_arbitration"] == "NOT_REEXECUTED"
    with without_provider_dispatch():
        _write_partial(data, retained, authority)
        cold = _snapshot(data, retained)
        review = pipeline._validate_current_source_stage_authority(
            cold["chat_authority"], spec=data["spec"], durations=[100],
            srt_text=cold["srt_text"], srt_sha256=cold["srt_sha256"],
            clip_context=cold["clip_context"], source_truth_ledger_path=None,
            pending_finalization=True,
            completed_retained_input=cold["completed_retained_input"])
    assert review == completed["review"]
    assert cold["completed_retained_input"] == completed
    assert authority["entity_repairs"] == completed["owners"]
    assert authority["retained_preparation_replay"]["release_authorized"] is False


@pytest.mark.parametrize("damage,reason", [
    ("status", "INPUT_REPLAY_DRIFT"),
    ("binding", "INPUT_REPLAY_DRIFT"),
    ("transcript_success", "INPUT_REPLAY_DRIFT"),
    ("extra_chat", "INPUT_REPLAY_EXTRA_AUTHORITY"),
    ("completion", "INPUT_REPLAY_OWNER_DRIFT"),
    ("usage", "INPUT_REPLAY_USAGE_DRIFT"),
    ("unverified", "INPUT_REPLAY_UNVERIFIED"),
])
def test_completed_input_replay_needs_exact_receipt_not_a_status_exception(
    tmp_path, monkeypatch, damage, reason,
):
    data, retained, completed, context, authority = _replayed_authority_fixture(tmp_path, monkeypatch)
    if damage == "status":
        authority["status"] = "NO_MATCH"
    elif damage == "binding":
        authority["retained_preparation_input"]["review_sha256"] = "sha256:" + "0" * 64
    elif damage == "transcript_success":
        authority["transcript_entity_audit"]["status"] = "VERIFIED"
    elif damage == "extra_chat":
        authority["applied"] = [{"unsupported": True}]
    elif damage == "completion":
        authority["retained_entity_review"]["sha256"] = "sha256:" + "0" * 64
    elif damage == "usage":
        authority["cpa_adjudication_usage"]["provider_adjudication_count"] = 0
    else:
        completed = None
    with without_provider_dispatch(), pytest.raises(ValueError, match=reason):
        pipeline._validate_current_source_stage_authority(
            authority, spec=data["spec"], durations=[100], srt_text=data["srt"],
            srt_sha256=hashlib.sha256(data["srt"].encode()).hexdigest(),
            clip_context=context, source_truth_ledger_path=None,
            pending_finalization=True, completed_retained_input=completed)


def test_completed_input_extra_owner_still_needs_normal_typed_validation(tmp_path, monkeypatch):
    data, retained, completed, context, authority = _replayed_authority_fixture(tmp_path, monkeypatch)
    cues = pipeline.parse_srt_cues(data["srt"])
    cue = cues[0]
    authority["entity_repairs"].append({
        "mode": "invented", "action": "REPLACE_CUE_TEXT", "cue_indexes": [1],
        "matched_start_ms": cue.start_ms, "matched_end_ms": cue.end_ms,
        "before": [cue.text], "after": ["unsupported"],
    })
    cues[0] = pipeline.SrtCue(cue.index, cue.start_ms, cue.end_ms, "unsupported")
    current = pipeline._render_cues_to_srt(cues)
    pipeline.retain_post_transcript_text(authority, current)
    with without_provider_dispatch(), pytest.raises(pipeline.CurrentTextContinuationError):
        pipeline._validate_current_source_stage_authority(
            authority, spec=data["spec"], durations=[100], srt_text=current,
            srt_sha256=hashlib.sha256(current.encode()).hexdigest(),
            clip_context=context, source_truth_ledger_path=None,
            pending_finalization=True, completed_retained_input=completed)



@pytest.mark.parametrize("damage", ["demote", "remove_binding", "forge_transcript"])
def test_native_replay_origin_rejects_demoted_or_mixed_authority(tmp_path, monkeypatch, damage):
    data, retained, completed, context, authority = _replayed_authority_fixture(tmp_path, monkeypatch)
    path = _write_partial(data, retained, authority)
    native = json.loads(path.read_bytes())
    saved = json.loads(Path(native["authority"]["path"]).read_bytes())
    saved["status"] = "NO_MATCH"
    if damage == "remove_binding":
        saved.pop("retained_preparation_input")
    elif damage == "forge_transcript":
        saved["transcript_entity_audit"] = {"schema_version": "transcript-entity-audit.v1",
                                           "status": "VERIFIED"}
    from src.autoslice.producer_text_checkpoint import _frozen_bytes
    native["authority"] = _frozen_bytes(path.parent/"negative-artifacts", "authority",
        json.dumps(saved, sort_keys=True).encode(), ".json")
    path.write_text(json.dumps(native))
    with without_provider_dispatch(), pytest.raises(ValueError, match="RETAINED_PREPARATION_"):
        _snapshot(data, retained)


def test_completed_input_unowned_text_edit_resealed_in_native_partial_is_rejected(tmp_path, monkeypatch):
    data, retained, completed, context, authority = _replayed_authority_fixture(tmp_path, monkeypatch)
    cues = pipeline.parse_srt_cues(data["srt"])
    cue = cues[-1]
    cues[-1] = pipeline.SrtCue(cue.index, cue.start_ms, cue.end_ms, cue.text + " changed")
    data["srt"] = pipeline._render_cues_to_srt(cues)
    pipeline.retain_post_transcript_text(authority, data["srt"])
    _write_partial(data, retained, authority)
    with without_provider_dispatch(), pytest.raises(ValueError, match="UNPROVEN_TEXT_TRANSITION"):
        _snapshot(data, retained)


def test_native_partial_references_exact_previous_pointer_and_cold_loads(tmp_path, monkeypatch):
    data, retained, completed, context, authority = _replayed_authority_fixture(tmp_path, monkeypatch)
    path = _write_partial(data, retained, authority)
    first = path.read_bytes()
    _write_partial(data, retained, authority)
    second = json.loads(path.read_bytes())
    previous = second["preparation_history"]["previous_checkpoint"]
    assert previous["role"] == "PREVIOUS_NORMAL_PREPARATION_CHECKPOINT"
    assert previous["sha256"] == _sha(first)
    assert Path(previous["path"]).read_bytes() == first
    with without_provider_dispatch():
        assert _snapshot(data, retained)["srt_text"] == data["srt"]


@pytest.mark.parametrize("damage", ["missing", "hash", "origin"])
def test_native_partial_predecessor_damage_is_not_an_initial_checkpoint(tmp_path, monkeypatch, damage):
    data, retained, completed, context, authority = _replayed_authority_fixture(tmp_path, monkeypatch)
    path = _write_partial(data, retained, authority)
    _write_partial(data, retained, authority)
    native = json.loads(path.read_bytes())
    if damage == "missing":
        native["preparation_history"].pop("previous_checkpoint")
    elif damage == "hash":
        native["preparation_history"]["previous_checkpoint"]["sha256"] = "sha256:" + "0" * 64
    else:
        native["preparation_history"]["restored_input_origin"] = None
    path.write_text(json.dumps(native))
    with without_provider_dispatch(), pytest.raises(ValueError, match="RETAINED_PREPARATION_PHASE_"):
        _snapshot(data, retained)


def test_native_partial_cannot_lower_the_last_persisted_usage_to_original_floor(tmp_path, monkeypatch):
    data, retained, completed, context, authority = _replayed_authority_fixture(tmp_path, monkeypatch)
    authority["cpa_adjudication_usage"]["provider_adjudication_count"] = 9
    path = _write_partial(data, retained, authority)
    previous = path.read_bytes()
    authority["cpa_adjudication_usage"]["provider_adjudication_count"] = 8
    with without_provider_dispatch(), pytest.raises(ValueError, match="PHASE_PARENT_OR_USAGE_DRIFT"):
        _write_partial(data, retained, authority)
    assert path.read_bytes() == previous



def _owner_authority_fixture(tmp_path, monkeypatch):
    """A completed retained review that restores one ordinary typed owner."""
    data, retained, _, saved, _ = _completion_fixture(tmp_path, monkeypatch)
    cue = pipeline.parse_srt_cues(data["srt"])[0]
    owner = {"mode": "cpa_context_adjudication", "action": "REPLACE_CUE_TEXT",
             "cue_indexes": [1], "matched_start_ms": cue.start_ms, "matched_end_ms": cue.end_ms,
             "before": ["旧文字"], "after": [cue.text], "request_sha256": "0" * 64}
    # The saved-review replay is the normal owner reader; this fixture only
    # supplies its owner rows (the typed decoder is exercised elsewhere).
    monkeypatch.setattr(pipeline, "_replay_saved_correction_review",
                        lambda **_: [copy.deepcopy(owner)])
    _put_completion(data, saved)
    completed = _load_completion(data, retained)
    assert completed["owners"] == [owner]
    context = json.loads(data["context_path"].read_bytes())
    authority = checkpoint.completed_retained_input_authority(
        completed, input_srt_text=data["srt"], clip_context=context)
    authority["retained_reviewed_draft"] = copy.deepcopy(retained["proof"])
    text, review = checkpoint.consume_completed_retained_review(
        completed, input_srt_text=data["srt"], authority=authority, clip_context=context)
    authority["final_review_audit"] = review
    pipeline.retain_post_transcript_text(authority, text)
    return data, retained, completed, context, authority


def _freeze_boundary_owners(data, authority):
    # The same in-place source-finalization freeze that the source checkpoint
    # later binds (producer_boundary_owner_contract).
    from src.autoslice.producer_boundary_owner_contract import (
        freeze_required_boundary_owner_contract)
    spec = {**copy.deepcopy(data["spec"]), "semantic_start_ms": 0, "semantic_end_ms": 100}
    freeze_required_boundary_owner_contract(
        spec=spec, durations=[100], chat_authority_audit=authority, required_boundary_owners=[])


def test_boundary_freeze_marks_are_not_restored_owner_drift(tmp_path, monkeypatch):
    """Resume after source finalization: the frozen boundary marks are the
    boundary stage's own contract, not a change of the completed owners."""
    data, retained, completed, context, authority = _owner_authority_fixture(tmp_path, monkeypatch)
    checkpoint.validate_completed_retained_input_authority(
        completed, authority=authority, clip_context=context)
    _freeze_boundary_owners(data, authority)
    row = authority["entity_repairs"][0]
    assert row["boundary_required"] is True and row != completed["owners"][0]
    with without_provider_dispatch():
        transcript = checkpoint.validate_completed_retained_input_authority(
            completed, authority=authority, clip_context=context)
        _write_partial(data, retained, authority)
        cold = _snapshot(data, retained)
    assert transcript["status"] == "NOT_REEXECUTED_COMPLETED_RETAINED_INPUT"
    assert cold["completed_retained_input"] == completed


@pytest.mark.parametrize("damage", [
    "owner_after", "owner_clock", "mark_owner_id", "mark_without_contract", "contract_scope",
])
def test_real_restored_owner_drift_still_rejected_after_boundary_freeze(
    tmp_path, monkeypatch, damage,
):
    data, retained, completed, context, authority = _owner_authority_fixture(tmp_path, monkeypatch)
    _freeze_boundary_owners(data, authority)
    row = authority["entity_repairs"][0]
    if damage == "owner_after":
        row["after"] = ["别的文字"]
    elif damage == "owner_clock":
        row["matched_end_ms"] += 1
    elif damage == "mark_owner_id":
        row["boundary_owner_id"] = "entity_repair:9:0:50"
    elif damage == "mark_without_contract":
        authority.pop("frozen_boundary_owner_contract")
    else:
        authority["frozen_boundary_owner_contract"]["story_end_ms"] = 10
    with without_provider_dispatch(), pytest.raises(
            ValueError, match="RETAINED_PREPARATION_INPUT_REPLAY_OWNER_DRIFT"):
        checkpoint.validate_completed_retained_input_authority(
            completed, authority=authority, clip_context=context)


def _history_copy_fixture(tmp_path):
    import shutil
    source, output = tmp_path / "original", tmp_path / "consumer"
    source.mkdir()
    output.mkdir(mode=0o755)
    # The test process may use umask 077. Set only this synthetic consumer's
    # requested mode so the test exercises an existing public output folder.
    output.chmod(0o755)
    old = {
        "schema_version": retained_reader._SCHEMA, "candidate_id": "candidate",
        "source_srt_sha256": "sha256:" + "a" * 64,
        "parent_authority_sha256": "sha256:" + "b" * 64,
        "source_timeline_offset_ms": 123,
        "source_endpoint": {"final_start_ms": 10, "final_end_ms": 90},
        "history_paths": {},
    }
    for key in ("parent_srt", "parent_authority"):
        path = source / f"candidate.{key}"
        path.write_bytes(("original-" + key).encode())
        shutil.copyfile(path, output / path.name)
        old["history_paths"][key] = str(path)
    raw = json.dumps(old, ensure_ascii=False, sort_keys=True, indent=2).encode()
    name = "candidate.retained-entity-review-input.json"
    (source / name).write_bytes(raw)
    shutil.copyfile(source / name, output / name)
    new = copy.deepcopy(old)
    new["history_paths"] = {key: str(output / Path(path).name)
                            for key, path in old["history_paths"].items()}
    requested = json.dumps(new, ensure_ascii=False, sort_keys=True, indent=2).encode()
    return source, output / name, raw, requested


def test_normal_history_copy_has_private_receipt_and_preserves_original(tmp_path):
    from src.autoslice.media_copy_provenance import file_snapshot
    source, path, raw, requested = _history_copy_fixture(tmp_path)
    before = {p.name: file_snapshot(p) for p in source.iterdir()}
    with without_provider_dispatch():
        assert retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT") == path
        assert retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT") == path
    intent, receipt = checkpoint.retained_history_materialization_paths(path)
    assert path.read_bytes() == raw
    assert before == {p.name: file_snapshot(p) for p in source.iterdir()}
    saved = json.loads(receipt.read_bytes())
    assert json.loads(intent.read_bytes())["status"] == "PREPARING"
    assert saved["operation"] == "INDEPENDENT_BYTE_COPY"
    assert saved["provider_observation"] is False and saved["release_authorized"] is False
    assert path.parent.stat().st_mode & 0o777 == 0o755
    assert receipt.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("damage", ["source_bytes", "output_bytes", "source_stat",
                                  "output_stat", "source_proof", "output_proof",
                                  "missing_receipt", "missing_intent"])
def test_history_copy_cold_drift_cannot_replace_identity(tmp_path, damage):
    import os
    source, path, raw, requested = _history_copy_fixture(tmp_path)
    retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT")
    intent, receipt = checkpoint.retained_history_materialization_paths(path)
    saved_receipt, saved_intent = receipt.read_bytes(), intent.read_bytes()
    source_history = source / "candidate.parent_srt"
    output_history = path.parent / source_history.name
    if damage.endswith("_stat"):
        target = source_history if damage.startswith("source") else output_history
        s = target.stat()
        os.utime(target, ns=(s.st_atime_ns, s.st_mtime_ns + 1000000))
    elif damage.endswith("_bytes"):
        target = source_history if damage.startswith("source") else output_history
        target.write_bytes(b"changed")
    elif damage.endswith("_proof"):
        target = source / path.name if damage.startswith("source") else path
        target.write_bytes(raw + b" ")
    elif damage == "missing_receipt":
        receipt.unlink()
    else:
        intent.unlink()
    with without_provider_dispatch(), pytest.raises((ValueError, OSError)):
        retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT")
    if damage != "missing_receipt":
        assert receipt.read_bytes() == saved_receipt
    else:
        assert not receipt.exists()
    if damage != "missing_intent":
        assert intent.read_bytes() == saved_intent
    else:
        assert not intent.exists()


@pytest.mark.parametrize("field,value", [
    ("candidate_id", "other"), ("parent_authority_sha256", "sha256:" + "c" * 64),
    ("source_srt_sha256", "sha256:" + "d" * 64), ("source_timeline_offset_ms", 124),
    ("source_endpoint", {"final_start_ms": 11, "final_end_ms": 90}),
])
def test_history_relocation_does_not_relax_source_parent_or_clock(tmp_path, field, value):
    _, path, _, requested = _history_copy_fixture(tmp_path)
    changed = json.loads(requested)
    changed[field] = value
    with without_provider_dispatch(), pytest.raises(ValueError, match="HISTORY_DRIFT"):
        retained_reader._write_history(path, json.dumps(changed).encode(), label="PENDING_ENTITY_INPUT")
    assert not checkpoint.retained_history_materialization_paths(path)[1].exists()


@pytest.mark.parametrize("damage", ["hardlink", "symlink", "wrong_directory",
                                  "missing_origin", "missing_history"])
def test_history_copy_requires_real_independent_source_chain(tmp_path, damage):
    import os
    source, path, _, requested = _history_copy_fixture(tmp_path)
    target = path.parent / "candidate.parent_srt"
    if damage in {"hardlink", "symlink"}:
        target.unlink()
        if damage == "hardlink":
            os.link(source / target.name, target)
        else:
            target.symlink_to(source / target.name)
    elif damage == "wrong_directory":
        changed = json.loads(requested)
        changed["history_paths"]["parent_srt"] = str(source / target.name)
        requested = json.dumps(changed).encode()
    elif damage == "missing_origin":
        (source / path.name).unlink()
    else:
        target.unlink()
    with without_provider_dispatch(), pytest.raises((ValueError, OSError)):
        retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT")
    assert not checkpoint.retained_history_materialization_paths(path)[1].exists()


def test_first_history_copy_rechecks_earlier_files_before_persisting(tmp_path, monkeypatch):
    from src.autoslice import media_copy_provenance as media
    source, path, _, requested = _history_copy_fixture(tmp_path)
    original_snapshot = media.file_snapshot
    changed = False
    def snapshot(p, expected_sha256=None):
        nonlocal changed
        result = original_snapshot(p, expected_sha256)
        if p == path.parent / "candidate.parent_srt" and not changed:
            (source / "candidate.parent_authority").write_bytes(b"changed after first snapshot")
            changed = True
        return result
    monkeypatch.setattr(media, "file_snapshot", snapshot)
    with without_provider_dispatch(), pytest.raises(ValueError):
        retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT")
    assert changed
    assert not checkpoint.retained_history_materialization_paths(path)[1].exists()


def test_interrupted_history_copy_leaves_non_success_intent(tmp_path, monkeypatch):
    from src.autoslice import argon_escalation_storage as storage
    _, path, _, requested = _history_copy_fixture(tmp_path)
    intent, receipt = checkpoint.retained_history_materialization_paths(path)
    put = storage.put_json
    def interrupt(p, value):
        if p == receipt:
            raise KeyboardInterrupt()
        return put(p, value)
    monkeypatch.setattr(storage, "put_json", interrupt)
    with without_provider_dispatch(), pytest.raises(KeyboardInterrupt):
        retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT")
    assert json.loads(intent.read_bytes())["stage"] == "MATERIALIZATION_STARTED"
    assert not receipt.exists()
    monkeypatch.setattr(storage, "put_json", put)
    with without_provider_dispatch(), pytest.raises(ValueError, match="INCOMPLETE_OR_UNPROVEN"):
        retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT")
    assert not receipt.exists()


def test_history_copy_new_interpreter_replays_existing_identity(tmp_path):
    import os
    import subprocess
    import sys
    _, path, _, requested = _history_copy_fixture(tmp_path)
    retained_reader._write_history(path, requested, label="PENDING_ENTITY_INPUT")
    request_file = tmp_path / "requested.json"
    request_file.write_bytes(requested)
    _, receipt = checkpoint.retained_history_materialization_paths(path)
    original_receipt = receipt.read_bytes()
    code = (
        "import pathlib,sys; "
        "from src.autoslice.retained_reviewed_draft import _write_history; "
        "from src.autoslice.subtitle_regression_recovery import without_provider_dispatch; "
        "\nwith without_provider_dispatch():\n"
        " _write_history(pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]).read_bytes(),"
        "label='PENDING_ENTITY_INPUT')\n"
        "print('COLD_HISTORY_IDENTITY_PASS')\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(path), str(request_file)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "COLD_HISTORY_IDENTITY_PASS"
    assert receipt.read_bytes() == original_receipt
