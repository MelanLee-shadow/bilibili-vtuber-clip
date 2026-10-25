from copy import deepcopy
from types import SimpleNamespace
import hashlib
import json

import pytest
from PIL import Image

from src.autoslice.producer_prepared_cover_recovery import _cover_successor_documents
from src.autoslice.producer_prepared_cover_recovery import _retained_environment
from src.autoslice.normal_cover_generation_checkpoint import file_identity
from src.autoslice.producer_prepared_recovery import validate_resume_options
from src.autoslice.cover_host_identity_gate import (
    verify_final_host_identity,
    validate_final_host_identity_verification,
)


@pytest.mark.parametrize("damage", [None, "context_bytes", "context_conflict"])
def test_cover_resume_restores_only_bound_original_context(tmp_path, monkeypatch, damage):
    import os

    context = tmp_path / "context.json"
    context.write_text('{"session": "original"}')
    old = {
        "provider": {"environment": {"LIDOUSHA_SESSION_GAME_CONTEXT": str(context)}},
        "dependencies": [file_identity(context)],
    }
    args = SimpleNamespace(
        resume_cover_failure=tmp_path / "failure.json", resume_cover_failure_sha256="a" * 64
    )
    monkeypatch.delenv("LIDOUSHA_SESSION_GAME_CONTEXT", raising=False)
    monkeypatch.delenv("LIDOUSHA_DISABLE_SESSION_GAME_CONTEXT", raising=False)
    if damage == "context_bytes":
        context.write_text('{"session": "changed"}')
    elif damage == "context_conflict":
        monkeypatch.setenv("LIDOUSHA_SESSION_GAME_CONTEXT", str(tmp_path / "different.json"))
    if damage:
        with pytest.raises(ValueError, match="PREPARED_COVER_SESSION_CONTEXT_"):
            with _retained_environment(args, old):
                pytest.fail("invalid context was admitted")
    else:
        with _retained_environment(args, old):
            assert os.environ["LIDOUSHA_SESSION_GAME_CONTEXT"] == str(context)
        assert "LIDOUSHA_SESSION_GAME_CONTEXT" not in os.environ


@pytest.mark.parametrize("damage", [None, "sha", "partial", "qc_without_failure"])
def test_cover_resume_requires_exact_native_failure(tmp_path, damage):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"output_root": str(tmp_path / "original")}))
    failure = tmp_path / "failure.json"
    failure.write_text("{}")
    args = SimpleNamespace(
        prepare_only=True,
        correct="cpa",
        ssh_host="localhost",
        speaker_mode="uniform_host",
        spec=spec,
        resume_spec_sha256=hashlib.sha256(spec.read_bytes()).hexdigest(),
        resume_output_root=tmp_path / "successor",
        resume_cover_failure=failure,
        resume_cover_failure_sha256=hashlib.sha256(failure.read_bytes()).hexdigest(),
        resume_cover_prepare_qc=True,
    )
    if damage == "sha":
        failure.write_text('{"different":true}')
    if damage == "partial":
        args.resume_cover_failure_sha256 = None
    if damage == "qc_without_failure":
        args.resume_cover_failure = args.resume_cover_failure_sha256 = None
    if damage:
        with pytest.raises(ValueError, match="PREPARED_RESUME_"):
            validate_resume_options(args)
    else:
        validate_resume_options(args)


def test_prepare_identity_comparison_has_no_verdict_or_provider_call(tmp_path, monkeypatch):
    from src.autoslice import visual_witness

    def forbidden(*args, **kwargs):
        pytest.fail("prepare-only comparison called a provider")

    monkeypatch.setattr(visual_witness, "image_vision_probe", forbidden)
    reference, final = tmp_path / "ref.png", tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), "blue").save(reference)
    Image.new("RGB", (1920, 1080), "green").save(final)
    sha = "sha256:" + hashlib.sha256(final.read_bytes()).hexdigest()
    receipt = verify_final_host_identity(
        final_cover_path=final,
        final_cover_sha256=sha,
        reference_path=reference,
        host_only_required=True,
        prepare_only=True,
    )
    assert receipt["status"] == "PENDING"
    assert receipt["reason_code"] == "CURRENT_IDENTITY_PROVIDER_REQUIRED"
    assert (
        receipt["comparison_sha256"]
        == "sha256:"
        + hashlib.sha256(
            __import__("pathlib").Path(receipt["comparison_path"]).read_bytes()
        ).hexdigest()
    )
    assert not validate_final_host_identity_verification(
        {"final_host_identity_verification": receipt, "final_cover_sha256": sha}
    )


@pytest.mark.parametrize("damage", [None, "no_current_qc", "other_failure"])
def test_cover_successor_preserves_text_and_old_receipts(tmp_path, damage):
    cover = tmp_path / "new.png"
    cover.write_bytes(b"new pixels")
    stage = {
        "title": "reviewed title",
        "cover_text": "reviewed copy",
        "reason_codes": ["COVER_FINAL_HOST_IDENTITY_UNVERIFIED"],
        "title_selection": {"retained": "identity"},
        "source_fact_review": {"original": "receipt"},
    }
    record = {
        "candidate_id": "cid",
        "publish_staging": stage,
        "artifact_hashes": {},
        "chat_authority": {"model_calls": 104},
        "burned_preview": {"sha256": "original"},
        "story_contract": {"selection_hook": "original story"},
    }
    publish = deepcopy(stage)
    before_record, before_publish = deepcopy(record), deepcopy(publish)
    result = {
        "status": "AI_COVER_READY",
        "cover_path": str(cover),
        "cover_generation": {"actual": "new verdict"},
        "cover_sha256": "new",
        "ai_background_sha256": "same background",
        "cover_reference_sha256": "original ref",
    }
    if damage == "no_current_qc":
        result["status"] = "BLOCKED_AI_COVER_REQUIRED"
    if damage == "other_failure":
        stage["reason_codes"].append("OTHER_CONTENT_FAILURE")
    if damage:
        with pytest.raises(ValueError):
            _cover_successor_documents(
                record=record, publish=publish, result=result, publish_path=tmp_path / "new.json"
            )
        return
    successor, new_publish = _cover_successor_documents(
        record=record, publish=publish, result=result, publish_path=tmp_path / "new.json"
    )
    assert record == before_record and publish == before_publish
    for key in ("story_contract", "chat_authority", "burned_preview"):
        assert successor[key] == record[key]
    for key in ("title", "cover_text", "title_selection", "source_fact_review"):
        assert successor["publish_staging"][key] == stage[key] == new_publish[key]
    assert successor["publish_staging"]["current_binding"] == new_publish["current_binding"]
