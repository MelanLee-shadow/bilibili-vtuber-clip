"""Source-rebuild ingress/consumer controls; synthetic bytes, no provider calls."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

from src.autoslice.first_publication_boundary import bind_published_recall_to_spec
from src.autoslice.new_bv_repair_authority import (
    build_new_bv_repair_authority, validate_new_bv_repair_authority,
)
from src.autoslice.producer_boundary_owner_contract import freeze_required_boundary_owner_contract
from src.autoslice.producer_boundary_resolution import (
    BoundaryResolutionAdapters, resolve_producer_boundary,
)
from src.autoslice.producer_text_checkpoint import write_source_text_checkpoint
from src.autoslice.source_checkpoint_parent import (
    SOURCE_ENDPOINT_BOUNDARY_FIELDS, SOURCE_ENDPOINT_DELIVERY_FIELDS,
    binding, bound, build_source_checkpoint_parent, consume_source_checkpoint_parent,
    validate_source_checkpoint_parent,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.subtitle_timing_qa import SpeechSpan
from tests.test_first_publication_boundary import published_fixture, recall_authority
from tests.test_retained_source_input import _files, _forbidden, _seed_native


ROOT = Path(__file__).resolve().parents[1]
_module_spec = importlib.util.spec_from_file_location(
    "source_endpoint_rebuild_builder", ROOT / "scripts/build_first_publication_recall_spec.py")
builder = importlib.util.module_from_spec(_module_spec)
_module_spec.loader.exec_module(builder)

SOURCE_SRT = (
    "1\n00:00:09,770 --> 00:00:20,000\n我们来说这件事。\n\n"
    "2\n00:00:55,000 --> 00:01:03,280\n这件事就说完了。\n\n"
    "3\n00:01:10,000 --> 00:01:15,000\n接着说下一个话题。\n\n"
    "4\n00:02:10,000 --> 00:02:16,610\n旧成片的尾段。\n\n"
    "5\n00:02:50,000 --> 00:02:55,000\n更后面的源语境。\n"
)


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _parent(tmp_path, monkeypatch, *, baseline=False):
    """Sister-seat parent shape: source window + old recut + complete CURRENT.

    Reuse a synthetic normal first-publication closure, then emit a native
    SOURCE checkpoint. No production spec, media, registry or secret is read.
    """
    fixture = published_fixture(tmp_path)
    parent = copy.deepcopy(fixture["spec"])
    cid = parent["candidate_id"]
    output_root = tmp_path / "parent" / "output"
    native = output_root / cid
    native.mkdir(parents=True)
    padded = fixture["source"].parent / "padded.mp4"
    srt = native / "source.srt"
    srt.write_text(SOURCE_SRT, encoding="utf-8")
    digest = hashlib.sha256(srt.read_bytes()).hexdigest()
    authority = {
        "schema_version": "chat-authority-audit.v2", "mutation_authorized": False,
        "post_transcript_entity_output_srt": SOURCE_SRT,
        "post_transcript_entity_output_srt_sha256": digest,
        "final_output_srt_sha256": digest,
        "final_review_audit": {
            "schema_version": "final-review-audit.v1", "context_adjudication_count": 2,
            "status": "FLAGGED", "release_gate": "BLOCK",
        },
    }
    live = _write(native / f"{cid}.chat-authority.json", authority)
    checkpoint = native / f"{cid}.source-text-checkpoint.json"
    write_source_text_checkpoint(
        checkpoint, candidate_id=cid, srt_path=srt, authority_path=live,
        context_path=fixture["context"], padded=padded)
    authority["final_review_audit"]["context_adjudication_count"] = 4
    _write(live, authority)
    _seed_native(native, padded)
    (native / f"{cid}.cpa-usage.jsonl").write_text(
        json.dumps({"kind": "text", "status": "OUTCOME_UNKNOWN"}) + "\n")
    current = native / "old-current.srt"
    current.write_text(SOURCE_SRT.split("\n\n5\n")[0] + "\n", encoding="utf-8")
    parent.update(
        output_root=str(output_root), runtime_root=str(tmp_path),
        semantic_end_ms=1_060_000,
        recovery_publication_authority=recall_authority(fixture),
        existing_final_recut={
            "schema_version": "existing-final-recut.v1", "candidate_id": cid,
            "path": str(fixture["recut"]), "sha256": binding(fixture["recut"])["sha256"],
            "provenance_path": str(fixture["recut"].with_suffix(".provenance.json")),
            "provenance_sha256": binding(fixture["recut"].with_suffix(".provenance.json"))["sha256"],
            "source_media_sha256": binding(padded)["sha256"],
            "final_start_ms": 9500, "final_end_ms": 136740,
            "time_domain": "padded_local",
            "authority_kind": "HASH_BOUND_EXISTING_FINAL_RECUT_NOT_PUBLICATION_AUTHORITY",
        },
        retained_current_text={
            "schema_version": "retained-reviewed-draft.v1",
            "current_srt_path": str(current), "current_srt_sha256": binding(current)["sha256"],
            "parent_authority_path": str(live), "parent_authority_sha256": binding(live)["sha256"],
            "parent_clip_context_path": str(fixture["context"]),
            "parent_clip_context_sha256": binding(fixture["context"])["sha256"],
            "padded_sha256": binding(padded)["sha256"],
            "parent_materialization_path": str(padded),
            "parent_materialization_sha256": binding(padded)["sha256"],
            "parent_materialization_duration_ms": 200000,
            "parent_materialization_provenance_path": str(padded.with_suffix(".provenance.json")),
            "parent_materialization_provenance_sha256": binding(padded.with_suffix(".provenance.json"))["sha256"],
            "source_timeline_offset_ms": 0,
            "source_media_sha256s": [parent["pieces"][0]["source_media_sha256"]],
        },
    )
    if baseline:
        parent["subtitle_redelivery_baseline"] = {
            "schema_version": "subtitle-redelivery-baseline.v2",
            "absolute_source_start_ms": 1_009_500,
            "absolute_source_end_ms": 1_136_740, "exact_interval_replay": True,
        }
    bind_published_recall_to_spec(parent)
    path = _write(tmp_path / "parent-spec.json", parent)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    return path, checkpoint, fixture


def _build(parent, checkpoint, output, **kwargs):
    return builder.build_successor(
        parent_spec_path=parent, checkpoint_path=checkpoint, output_root=output, **kwargs)


def _emit_gemini_journal(parent, monkeypatch, *, modality, outcome):
    """Use the ordinary writer; replace only HTTP and provider admission."""
    from contextlib import nullcontext
    import io
    from src.autoslice import agy_gemini_client as gemini
    from src.autoslice import cpa_runtime as cpa

    raw = json.loads(parent.read_bytes())
    journal = Path(raw["output_root"]) / raw["candidate_id"] / f'{raw["candidate_id"]}.cpa-usage.jsonl'
    calls = []

    class Response(io.BytesIO):
        status = 200

    def post(request, timeout):
        calls.append(request)
        if outcome == "unknown":
            raise TimeoutError("fictional transport interruption")
        text = "invalid" if outcome == "rejected" or outcome == "retry" and len(calls) == 1 else "{}"
        return Response(json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode())

    monkeypatch.setattr(gemini, "runtime_provider_slot", lambda **_: nullcontext())
    monkeypatch.setattr(gemini.urllib.request, "urlopen", post)
    with cpa.bind_observation(journal, "fixture-attempt"):
        # Existing kind-bearing producer branches remain accepted too.
        cpa.record_cpa_attempt(model="fixture-text", kind="text", status=200, elapsed_seconds=0.01)
        with cpa.observed_model_request(model="fixture-text", kind="text", transport="fixture"):
            pass
        cpa.record_cpa_event("cache_hit", {"kind": "current_final_review", "stage": "exact_final_review"})
        kwargs = dict(prompt="fixture json", key="fictional", model="fixture-model",
                      inline_data=b"fixture-media", mime_type="audio/wav" if modality == "audio" else "image/jpeg",
                      response_validator=json.loads)
        if outcome == "unknown":
            with pytest.raises(TimeoutError):
                gemini.generate_content(**kwargs)
        elif outcome == "rejected":
            with pytest.raises(ValueError):
                gemini.generate_content(**kwargs)
        else:
            assert gemini.generate_content(**kwargs) == "{}"
    return journal, calls


@pytest.mark.parametrize("rebuild", [False, True], ids=["recall", "source-endpoint"])
@pytest.mark.parametrize("modality", ["audio", "vision"])
@pytest.mark.parametrize("outcome", ["returned", "retry", "rejected", "unknown"])
def test_native_writer_journal_survives_build_validate_and_consume(
        tmp_path, monkeypatch, rebuild, modality, outcome):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    journal, calls = _emit_gemini_journal(parent, monkeypatch, modality=modality, outcome=outcome)
    before = journal.read_bytes()
    rows = [json.loads(line) for line in before.splitlines()]
    kindless = [row for row in rows if "kind" not in row]
    assert {row["event_type"] for row in kindless} == (
        {"audio_model_request", "audio_http_attempt", "audio_format_attempt"}
        if modality == "audio" else {"vision_format_attempt"})
    assert len(calls) == (2 if outcome in {"retry", "rejected"} else 1)
    assert any(row["status"] == "OUTCOME_UNKNOWN" for row in kindless) == (outcome == "unknown")
    spec = _build(parent, checkpoint, tmp_path / "successor", accept_source_endpoint=rebuild)
    retained = validate_source_checkpoint_parent(spec)
    assert retained["cpa_journal"] == binding(journal)
    assert retained["cpa_usage"]["provider_adjudication_count"] == 4
    assert retained["release_authorized"] is False
    output = Path(spec["output_root"]) / spec["candidate_id"]
    output.mkdir(parents=True)
    consumed = consume_source_checkpoint_parent(
        spec=spec, cid=spec["candidate_id"], padded=Path(bound(binding(checkpoint))["padded"]["path"]),
        out_root=output)
    assert consumed.proof["cpa_journal"] == retained["cpa_journal"]
    assert consumed.cpa_usage["provider_adjudication_count"] == 4
    assert consumed.proof["release_authorized"] is False
    assert journal.read_bytes() == before
    # Binding still rejects later byte drift; compatible format is no waiver.
    journal.write_bytes(before + b"\n")
    with pytest.raises(ValueError, match="SOURCE_CHECKPOINT_PARENT_HISTORY_DRIFT"):
        validate_source_checkpoint_parent(spec)


@pytest.mark.parametrize("rebuild", [False, True], ids=["recall", "source-endpoint"])
@pytest.mark.parametrize("damage", ["event_type", "provider", "model", "observation_input_sha256",
                                    "production_attempt_id", "status", "format_attempt", "elapsed_seconds",
                                    "attempt", "json_mode", "schema_name", "schema_sha256", "error",
                                    "completion_sha256", "explicit_null_kind", "unknown_format_status",
                                    "non_object", "unknown_event", "unhashable_event", "wrong_provider",
                                    "empty_model", "bad_digest", "unhashable_status", "bad_attempt_type",
                                    "bad_elapsed_type"])
def test_native_writer_journal_still_rejects_unknown_or_incomplete_rows(
        tmp_path, monkeypatch, rebuild, damage):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    journal, _ = _emit_gemini_journal(parent, monkeypatch, modality="audio", outcome="rejected")
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    event = "audio_format_attempt" if damage in {"attempt", "json_mode", "schema_name", "schema_sha256",
                                                "error", "completion_sha256", "unknown_format_status",
                                                "unhashable_status"} else "audio_http_attempt"
    index = next(i for i, row in enumerate(rows) if row.get("event_type") == event)
    if damage == "non_object":
        rows[index] = []
    elif damage == "explicit_null_kind":
        rows[index]["kind"] = None
    elif damage == "unknown_format_status":
        rows[index]["status"] = "INVENTED"
    elif damage in {"unknown_event", "unhashable_event", "wrong_provider", "empty_model", "bad_digest",
                    "unhashable_status", "bad_attempt_type", "bad_elapsed_type"}:
        key, value = {"unknown_event": ("event_type", "invented_event"),
                      "unhashable_event": ("event_type", []), "wrong_provider": ("provider", "invented"),
                      "empty_model": ("model", ""), "bad_digest": ("observation_input_sha256", "not-a-sha"),
                      "unhashable_status": ("status", []), "bad_attempt_type": ("format_attempt", True),
                      "bad_elapsed_type": ("elapsed_seconds", "unknown")}[damage]
        rows[index][key] = value
    else:
        rows[index].pop(damage)
    journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with pytest.raises(ValueError, match="SOURCE_CHECKPOINT_PARENT_CPA_JOURNAL_INVALID"):
        _build(parent, checkpoint, tmp_path / "successor", accept_source_endpoint=rebuild)


@pytest.mark.parametrize("baseline", [False, True], ids=["sister-seat-shape", "redelivery-sibling"])
def test_source_endpoint_rebuild_removes_delivery_contracts_and_preserves_source(tmp_path, monkeypatch, baseline):
    parent, checkpoint, fixture = _parent(tmp_path, monkeypatch, baseline=baseline)
    before = _files(tmp_path)
    old = json.loads(parent.read_bytes())
    spec = _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)
    assert spec["accept_source_endpoint"] is True
    assert not set(SOURCE_ENDPOINT_DELIVERY_FIELDS + SOURCE_ENDPOINT_BOUNDARY_FIELDS).intersection(spec)
    assert validate_source_checkpoint_parent(spec) == spec["retained_source_input"]
    for key in ("bvid", "aid", "cid", "candidate_id", "recording_date", "source_publication"):
        assert spec["recovery_publication_authority"][key] == old["recovery_publication_authority"][key]
    assert "boundary_end_mode" not in spec["recovery_publication_authority"]
    assert "required_given_end_ms" not in spec["recovery_publication_authority"]
    assert not bind_published_recall_to_spec(spec)  # Producer/resolver cannot reinsert the anchor.
    assert spec["pieces"] == old["pieces"]
    assert spec["semantic_end_ms"] == old["semantic_end_ms"]
    assert spec["retained_source_input"]["cpa_usage"]["provider_adjudication_count"] == 4
    assert spec["source_text_checkpoint"]["sha256"] == binding(checkpoint)["sha256"]
    assert spec["existing_subtitle_surface"]["path"] == bound(binding(checkpoint))["srt"]["path"]
    assert spec["existing_subtitle_surface"]["path"] != old["retained_current_text"]["current_srt_path"]
    assert _files(tmp_path) == before


def test_default_successor_matches_fd758129c_bytes(tmp_path, monkeypatch):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch, baseline=True)
    output = tmp_path / "default-successor"
    expected = _fd758129c_build_successor(
        parent_spec_path=parent, checkpoint_path=checkpoint, output_root=output)
    actual = _build(parent, checkpoint, output)
    assert actual == expected
    def serialize(value):
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    assert serialize(actual) == serialize(expected)


def _drift_live_current(parent):
    raw = json.loads(parent.read_bytes())
    live = Path(raw["output_root"]) / raw["candidate_id"] / f'{raw["candidate_id"]}.chat-authority.json'
    authority = json.loads(live.read_bytes())
    authority["final_output_srt_sha256"] = hashlib.sha256(b"later CURRENT delivery").hexdigest()
    _write(live, authority)
    return live


def _replace_checkpoint_artifact(checkpoint, key, raw):
    native = json.loads(checkpoint.read_bytes())
    old = Path(native[key]["path"])
    digest = hashlib.sha256(raw).hexdigest()
    path = old.parent / f"{key}.{digest}{old.suffix}"
    path.write_bytes(raw)
    native[key] = {"path": str(path), "sha256": "sha256:" + digest}
    _write(checkpoint, native)


def test_rebuild_discloses_live_current_drift_and_consumes_frozen_source(tmp_path, monkeypatch):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    live = _drift_live_current(parent)
    before = _files(tmp_path)
    spec = _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)
    spec = json.loads(_write(tmp_path / "rebuilt-spec.json", spec).read_bytes())
    config = validate_source_checkpoint_parent(spec)
    observation = config["source_authority_observation"]
    frozen = bound(binding(checkpoint))
    assert observation["source_authority_basis"] == "FROZEN_SOURCE_CHECKPOINT"
    assert observation["frozen_authority"] == binding(Path(frozen["authority"]["path"]))
    assert observation["source_srt"] == binding(Path(frozen["srt"]["path"]))
    assert observation["live_authority_role"] == "ACCOUNTING_AND_DRIFT_DISCLOSURE_ONLY"
    assert observation["differing_fields"] == ["final_output_srt_sha256"]
    assert observation["fields"]["final_output_srt_sha256"] == {
        "frozen": frozen["srt"]["sha256"][7:],
        "live": json.loads(live.read_bytes())["final_output_srt_sha256"],
    }
    assert spec["existing_subtitle_surface"]["path"] == frozen["srt"]["path"]
    assert all((tmp_path / path).read_bytes() == raw for path, raw in before.items())
    output = Path(spec["output_root"]) / spec["candidate_id"]
    output.mkdir(parents=True)
    consumed = consume_source_checkpoint_parent(
        spec=spec, cid=spec["candidate_id"], padded=Path(frozen["padded"]["path"]), out_root=output)
    assert consumed.cpa_usage["provider_adjudication_count"] == 4
    assert consumed.proof["source_checkpoint"] == config["source_checkpoint"]


def test_default_recall_still_rejects_live_current_drift(tmp_path, monkeypatch):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    _drift_live_current(parent)
    with pytest.raises(ValueError, match="SOURCE_CHECKPOINT_PARENT_SOURCE_AUTHORITY_DRIFT"):
        _build(parent, checkpoint, tmp_path / "default-successor")


@pytest.mark.parametrize("damage", ["schema", "mutation", "post_hash", "final_hash"])
def test_rebuild_rejects_inconsistent_frozen_authority(tmp_path, monkeypatch, damage):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    _drift_live_current(parent)
    frozen = bound(binding(checkpoint))
    authority = json.loads(Path(frozen["authority"]["path"]).read_bytes())
    if damage == "schema":
        authority["schema_version"] = "other"
    elif damage == "mutation":
        authority["mutation_authorized"] = True
    elif damage == "post_hash":
        authority["post_transcript_entity_output_srt_sha256"] = "0" * 64
    else:
        authority["final_output_srt_sha256"] = "0" * 64
    _replace_checkpoint_artifact(checkpoint, "authority", json.dumps(authority).encode())
    reason = ("POST_TRANSCRIPT_TEXT_CHECKPOINT_INVALID" if damage == "post_hash"
              else "SOURCE_CHECKPOINT_PARENT_SOURCE_AUTHORITY_DRIFT")
    with pytest.raises(ValueError, match=reason):
        _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)


@pytest.mark.parametrize("rebind", [False, True], ids=["raw-bytes-drift", "checkpoint-rebound"])
def test_rebuild_rejects_source_bytes_not_bound_by_frozen_authority(tmp_path, monkeypatch, rebind):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    _drift_live_current(parent)
    frozen = bound(binding(checkpoint))
    source = Path(frozen["srt"]["path"])
    changed = source.read_bytes().replace("这件事".encode(), "另一件事".encode())
    if rebind:
        _replace_checkpoint_artifact(checkpoint, "srt", changed)
    else:
        source.write_bytes(changed)
    reason = "SOURCE_AUTHORITY_DRIFT" if rebind else "CHECKPOINT_BYTES_DRIFT"
    with pytest.raises(ValueError, match=reason):
        _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)


@pytest.mark.parametrize("damage", ["fields", "differing_fields"])
def test_rebuild_revalidates_drift_disclosure(tmp_path, monkeypatch, damage):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    _drift_live_current(parent)
    spec = _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)
    observation = spec["retained_source_input"]["source_authority_observation"]
    if damage == "fields":
        observation["fields"]["final_output_srt_sha256"]["live"] = "0" * 64
    else:
        observation["differing_fields"] = []
    with pytest.raises(ValueError, match="SOURCE_CHECKPOINT_PARENT_HISTORY_DRIFT"):
        validate_source_checkpoint_parent(spec)


def test_rebuild_current_drift_does_not_waive_usage_history(tmp_path, monkeypatch):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    live = _drift_live_current(parent)
    authority = json.loads(live.read_bytes())
    authority["final_review_audit"]["context_adjudication_count"] = 1
    _write(live, authority)
    with pytest.raises(ValueError, match="SOURCE_CHECKPOINT_PARENT_CPA_HISTORY_REGRESSION"):
        _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)


def _resumed_parent(tmp_path, monkeypatch, *, schema="v2", unavailable=False):
    from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
    from src.autoslice.native_audio_budget_receipt import (
        persist_native_audio_budget_receipt, resume_native_audio_budget,
    )
    from src.autoslice.supplement_audio_budget import get_budget, start_budget
    from tests.test_native_audio_history_reconciliation import _observe
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    raw = json.loads(parent.read_bytes())
    cid = raw["candidate_id"]
    native = Path(raw["output_root"]) / cid
    padded = Path(json.loads(checkpoint.read_bytes())["padded"]["path"])
    if unavailable:
        next((native / 'native-exact').glob('*/*/input.audio-evidence.*.json')).unlink()
    state = inherit_prepared_native_audio(cid=cid, parent=native, source=padded, runtime_root=tmp_path)
    if schema == "v3":
        # Build an already registered v3 through the ordinary production join;
        # the builder under test must only read it, never reconcile a new fork.
        _observe(state.cache_root, padded, start=5000)
        incoming = tmp_path / "incoming-account"
        shutil.copytree(native, incoming)
        start_budget(padded, max_windows=3)
        resume_native_audio_budget(source_media=padded,
            source_media_sha256=binding(padded)["sha256"][7:],
            receipt_path=incoming / "native-audio-budget.json", require_existing=True)
        _observe(incoming, padded, start=7000)
        _observe(incoming, padded, start=9000, status="DISPATCHED")
        start_budget(padded, max_windows=3)
        state = inherit_prepared_native_audio(cid=cid, parent=incoming, source=padded, runtime_root=tmp_path)
    else:
        get_budget(padded).consume("mai", "MAI-Transcribe-2", 9000, 10000)
        persist_native_audio_budget_receipt(source_media=padded,
            source_media_sha256=binding(padded)["sha256"][7:], receipt_path=state.budget_receipt)
    (native / "native-audio-budget.json").unlink()
    # A fresh producer must restore all attempts, including the unknown one.
    start_budget(padded, max_windows=3)
    return parent, checkpoint, native, padded, state


@pytest.mark.parametrize("schema", ["v2", "v3"])
@pytest.mark.parametrize("endpoint", [False, True], ids=["recall", "source-endpoint"])
def test_resumed_account_build_and_consume_preserve_history_without_output_receipt(
    tmp_path, monkeypatch, schema, endpoint,
):
    from src.autoslice.supplement_audio_budget import get_budget
    parent, checkpoint, native, padded, state = _resumed_parent(tmp_path, monkeypatch, schema=schema)
    receipt = json.loads(state.budget_receipt.read_bytes())
    before = _files(tmp_path)
    spec = _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=endpoint)
    assert spec["retained_source_input"]["native_budget"] == binding(state.budget_receipt)
    assert spec["retained_source_input"]["live_authority"]["path"] == str(
        native / f'{spec["candidate_id"]}.chat-authority.json')
    assert _files(tmp_path) == before
    output = Path(spec["output_root"]) / spec["candidate_id"]
    output.mkdir(parents=True)
    for _ in range(2):
        consumed = consume_source_checkpoint_parent(spec=spec, cid=spec["candidate_id"],
                                                   padded=padded, out_root=output)
        assert consumed.native == state
        assert consumed.proof["native_budget_receipt"] == str(state.budget_receipt)
        assert get_budget(padded).snapshot() == receipt["budget"]
        assert get_budget(padded).snapshot()["pending_attempt_count"] == 1
        assert validate_source_checkpoint_parent(spec) == spec["retained_source_input"]
    assert _files(tmp_path) == before  # No account self-parent, lost history or new receipt.


@pytest.mark.parametrize("damage", [
    "missing", "symlink", "self_hash", "source_sha", "source_locator", "event_sequence",
    "rollback", "fork", "manifest_missing", "manifest_candidate", "manifest_source",
    "ancestor_missing", "ancestor_bytes", "namespace",
])
def test_resumed_account_rejects_missing_damaged_or_nonextending_history(tmp_path, monkeypatch, damage):
    from src.autoslice.native_audio_budget_receipt import receipt_sha256
    parent, checkpoint, _, padded, state = _resumed_parent(tmp_path, monkeypatch)
    path = state.budget_receipt
    receipt = json.loads(path.read_bytes())
    manifest_path = path.parent / "parent.json"
    manifest = json.loads(manifest_path.read_bytes())
    archived = path.parent / "parents" / (manifest["parents"][0]["budget_sha256"][7:] + ".json")
    if damage == "missing":
        path.unlink()
    elif damage == "symlink":
        target = path.with_name("other.json")
        path.rename(target)
        path.symlink_to(target)
    elif damage == "manifest_missing":
        manifest_path.unlink()
    elif damage.startswith("manifest_"):
        key = "candidate_id" if damage == "manifest_candidate" else "source_media_sha256"
        manifest[key] = "other"
        _write(manifest_path, manifest)
    elif damage == "ancestor_missing":
        archived.unlink()
    elif damage == "ancestor_bytes":
        archived.write_bytes(archived.read_bytes() + b" ")
    elif damage == "namespace":
        monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "other-runtime"))
    elif damage == "fork":
        branch = tmp_path / "unregistered-fork"
        branch.mkdir()
        _seed_native(branch, padded, start=7000, end=7500)
        path.write_bytes((branch / path.name).read_bytes())
    else:
        if damage == "source_sha":
            receipt["source_media_sha256"] = "0" * 64
        elif damage == "source_locator":
            receipt["budget"]["source_media"] = str(tmp_path / "other-source.mp4")
        elif damage == "event_sequence":
            receipt["budget"]["attempts"][0]["attempt_id"] = 9
        elif damage == "rollback":
            from src.autoslice.supplement_audio_budget import get_budget
            empty = get_budget(padded).snapshot()
            receipt.update(budget=empty, revision=0, providers=[])
        receipt["receipt_sha256"] = "bad" if damage == "self_hash" else receipt_sha256(receipt)
        _write(path, receipt)
    before = _files(tmp_path)
    with pytest.raises((ValueError, OSError)):
        _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)
    assert _files(tmp_path) == before


def test_resumed_account_revalidates_bound_bytes_and_completed_cache(tmp_path, monkeypatch):
    parent, checkpoint, _, padded, state = _resumed_parent(tmp_path, monkeypatch)
    spec = _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)
    original = state.budget_receipt.read_bytes()
    state.budget_receipt.write_bytes(original + b" ")
    with pytest.raises(ValueError, match="HISTORY_DRIFT"):
        validate_source_checkpoint_parent(spec)
    state.budget_receipt.write_bytes(original)
    next((state.cache_root / "native-exact").glob("*/*/input.mp3")).unlink()
    output = tmp_path / "rebuild" / spec["candidate_id"]
    output.mkdir(parents=True)
    before = _files(tmp_path)
    with pytest.raises(ValueError, match="PREPARED_NATIVE_COMPLETED_CACHE_MISSING"):
        consume_source_checkpoint_parent(spec=spec, cid=spec["candidate_id"], padded=padded,
                                         out_root=output)
    assert _files(tmp_path) == before


def test_source_endpoint_rebuild_still_requires_parent_recovery_authority(tmp_path, monkeypatch):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    raw = json.loads(parent.read_bytes())
    raw.pop("recovery_publication_authority")
    _write(parent, raw)
    with pytest.raises(ValueError, match="authority"):
        _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)


@pytest.mark.parametrize("damage", [
    "marker", "existing_final_recut", "retained_current_text", "subtitle_redelivery_baseline",
    "given_end_ms", "semantic_tail_trim_cap_ms", "authority", "owner", "source", "checkpoint",
])
def test_rebuild_marker_does_not_waive_authority_or_source_scope(tmp_path, monkeypatch, damage):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    spec = _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)
    if damage == "marker":
        spec.pop("accept_source_endpoint")
    elif damage in SOURCE_ENDPOINT_DELIVERY_FIELDS:
        spec[damage] = {}
    elif damage in {"given_end_ms", "semantic_tail_trim_cap_ms"}:
        spec[damage] = 1
    elif damage == "authority":
        spec["recovery_publication_authority"] = json.loads(parent.read_bytes())["recovery_publication_authority"]
    elif damage == "owner":
        spec["selection_hook"] = "changed owner"
    elif damage == "source":
        spec["pieces"][0]["start_ms"] += 1
    else:
        spec["source_text_checkpoint"]["sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError):
        validate_source_checkpoint_parent(spec)


def test_rebuild_rejects_publication_source_clock_mismatch(tmp_path, monkeypatch):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    raw = json.loads(parent.read_bytes())
    raw["pieces"][0]["source_media_sha256"] = "sha256:" + "0" * 64
    _write(parent, raw)
    with pytest.raises(ValueError, match="source clock/hash/date"):
        _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)


def test_cli_persists_create_only_spec_and_rejects_witness_only_combination(tmp_path, monkeypatch, capsys):
    parent, checkpoint, _ = _parent(tmp_path, monkeypatch)
    default = builder.derive_successor_inputs(parent)
    derived = builder.derive_successor_inputs(parent, accept_source_endpoint=True)
    assert derived["source_checkpoint"] == default["source_checkpoint"] == checkpoint
    assert derived["output_root"] != default["output_root"]
    assert not derived["output_root"].exists()
    args = ["--parent-spec", str(parent), "--derive-inputs", "--accept-source-endpoint"]
    assert builder.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["published_source_end_ms"] is None
    assert report["given_end_mode"] == "semantic_lower_bound"
    assert report["provider_calls"] == 0 and report["release_authorized"] is False
    persisted = json.loads(derived["output_spec"].read_bytes())
    assert validate_source_checkpoint_parent(persisted)
    with pytest.raises(FileExistsError):
        builder.main(args)
    with pytest.raises(SystemExit):
        builder.main(args + ["--extend-source-reserve"])
    with pytest.raises(ValueError, match="witness-only"):
        _build(parent, checkpoint, tmp_path / "conflict", accept_source_endpoint=True, extend_source_reserve=True)


def test_sister_seat_shape_enters_normal_source_boundary_solver(tmp_path, monkeypatch):
    parent, checkpoint, fixture = _parent(tmp_path, monkeypatch)
    spec = _build(parent, checkpoint, tmp_path / "rebuild", accept_source_endpoint=True)
    output = Path(spec["output_root"]) / spec["candidate_id"]
    output.mkdir(parents=True)
    # Persist/read through the same JSON representation as the ordinary CLI.
    path = _write(tmp_path / "rebuild-spec.json", spec)
    spec = json.loads(path.read_bytes())
    assert validate_source_checkpoint_parent(spec)
    cues = parse_srt_cues(SOURCE_SRT)
    freeze_required_boundary_owner_contract(
        spec=spec, durations=[200000], chat_authority_audit={}, required_boundary_owners=[])
    scope = spec["boundary_search_scope"]
    assert scope["boundary_end_mode"] == "semantic_lower_bound"
    assert scope["published_recall_anchor_ms"] is None
    # Preflight writes this normal default; runtime SOURCE validation must
    # accept it, while refusing a caller's nonzero trim-cap override.
    spec["semantic_tail_trim_cap_ms"] = 0
    assert validate_source_checkpoint_parent(spec, runtime=True)
    # Synthetic reviewer output; exercise the real resolver and deterministic
    # guards, without dispatching a reviewer or claiming content acceptance.
    spec["boundary_semantic_review"] = {
        "schema_version": "talk-boundary-semantic-review.v1", "review_scope": "source_full_window",
        "status": "PASS", "request_sha256": "sha256:" + "1" * 64,
        "cue_grid_sha256": cue_grid_sha256(cues), "boundary_search_scope": copy.deepcopy(scope),
        "recommended_end_ms": 63280, "recommended_end_cue_index": 2,
    }
    result = resolve_producer_boundary(
        spec=spec, durations=[200000], padded=fixture["source"].parent / "padded.mp4",
        padded_dur=200000, cid=spec["candidate_id"], out_root=output,
        transcriber=_forbidden, cues=cues,
        spans=[SpeechSpan(c.start_ms, c.end_ms) for c in cues],
        boundary_repair_extend_cap_ms=30000,
        adapters=BoundaryResolutionAdapters(accurate_recut_command=_forbidden, run_command=_forbidden),
    )
    assert result.final_end == 63680
    assert result.final_end < 136740  # Old published/retained tail was not a floor.
    assert result.audit["snapped_sentence_end_ms"] == 63280
    assert result.audit["boundary_semantic_review"]["final_endpoint_binding"]["status"] == "PASS"


# Frozen verbatim build_successor from fd758129c582c723ad1b6dee8df3c0c8b63f7832;
# only the function name differs. This is the default-output compatibility oracle.
def _fd758129c_build_successor(*, parent_spec_path: Path, checkpoint_path: Path, output_root: Path,
                    extend_source_reserve: bool = False) -> dict:
    parent = bound(binding(parent_spec_path))
    cid = str(parent.get("candidate_id") or "")
    previous = validate_new_bv_repair_authority(
        parent.get("recovery_publication_authority"), candidate_id=cid,
        expected_final_title=parent.get("given_title"))
    snapshot = previous.get("review_manifest_snapshot")
    authority = build_new_bv_repair_authority(
        Path(previous["source_publication"]["path"]), candidate_id=cid,
        title=previous["title"],
        review_manifest_snapshot=Path(snapshot["path"]) if snapshot else None,
        published_recall_anchor=True)
    retained = build_source_checkpoint_parent(parent_spec_path=parent_spec_path,
                                               checkpoint_path=checkpoint_path)
    checkpoint = bound(retained["source_checkpoint"])
    successor = copy.deepcopy(parent)
    # These fields are generated by the verified publication bridge, never
    # supplied as an operator endpoint or a hand-signed input.
    for key in ("given_end_ms", "given_end_mode", "given_end_authority",
                "semantic_tail_trim_cap_ms"):
        successor.pop(key, None)
    successor.update(
        output_root=str(output_root),
        recovery_publication_authority=authority,
        retained_source_input=retained,
        source_text_checkpoint={"path": retained["source_checkpoint"]["path"],
                                "sha256": retained["source_checkpoint"]["sha256"]},
        existing_subtitle_surface={
            "schema_version": "existing-subtitle-surface.v1", "candidate_id": cid,
            **checkpoint["srt"],
            "source_media_sha256": checkpoint["padded"]["sha256"],
            "time_domain": "padded_local",
            "authority_kind": "PIPELINE_REVIEWED_CPA_SURFACE_NOT_HUMAN_TRUTH",
        })
    bind_published_recall_to_spec(successor)
    if extend_source_reserve:
        from src.autoslice.source_witness_extension import build_plan
        successor["source_witness_extension"] = build_plan(successor)
    validate_source_checkpoint_parent(successor)
    return successor
