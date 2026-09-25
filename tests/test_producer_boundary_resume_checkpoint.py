from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.clip_context import build_clip_context, write_clip_context
from src.autoslice.producer_boundary_owner_contract import (
    freeze_required_boundary_owner_contract,
)
from src.autoslice.producer_boundary_resume_checkpoint import (
    BoundaryResumeCheckpointError,
    load_boundary_resume_checkpoint,
    load_or_compute_vad_spans,
)
from src.autoslice.producer_chat_input import build_structured_chat_binding_audit
from src.autoslice.speech_memory_ledger import SCHEMA_VERSION as MEMORY_SCHEMA
from src.autoslice.subtitle_timing_qa import SpeechSpan


CID = "resume-fixture"
DATE = "2026-09-24"
SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\n开场\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\n故事\n\n"
    "3\n00:00:05,000 --> 00:00:06,000\n收束\n"
)


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path: Path):
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"media" * 100)
    source_sha = _sha(b"source")
    spec = {
        "candidate_id": CID,
        "date": DATE,
        "selection_hook": "讲完一个故事",
        "semantic_start_ms": 1_000,
        "semantic_end_ms": 50_000,
        "semantic_tail_trim_cap_ms": 0,
        "boundary_repair_extend_cap_ms": 30_000,
        "pieces": [
            {
                "start_ms": 0,
                "end_ms": 100_000,
                "remote_media": "/private/source.mp4",
                "source_media_sha256": source_sha,
            }
        ],
    }
    durations = [100_000]
    chat: dict[str, object] = {"applied": [], "entity_repairs": []}
    _target, scope = freeze_required_boundary_owner_contract(
        spec=spec,
        durations=durations,
        chat_authority_audit=chat,
        required_boundary_owners=[],
    )
    unavailable = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "BLOCK",
        "candidate_id": CID,
        "boundary_search_scope": scope,
        "reason_codes": ["BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError"],
    }
    review = {
        "schema_version": "final-review-audit.v1",
        "status": "PARTIAL",
        "findings": [],
        "applied_count": 0,
        "infra_unresolved": [],
        "infra_unresolved_count": 0,
        "boundary_semantic_review": unavailable,
    }
    chat.update(
        {
            "final_output_srt_sha256": hashlib.sha256(SRT.encode()).hexdigest(),
            "final_review_audit": review,
            "structured_chat_binding_audit": build_structured_chat_binding_audit(
                spec, []
            ),
        }
    )
    ledger = tmp_path / "speech-memory.json"
    ledger.write_text(
        json.dumps({"schema_version": MEMORY_SCHEMA, "entries": []}),
        encoding="utf-8",
    )
    clip = build_clip_context(
        candidate_id=CID,
        spec=spec,
        draft_srt=SRT,
        authoritative_chat=[],
        topic_resolution={"schema_version": "topic-resolution.v1", "status": "NO_GRAPH"},
        session_topic_authorities=[],
        speech_memory_ledger_path=ledger,
    )
    (tmp_path / "padded.fresh.srt").write_text(SRT, encoding="utf-8")
    (tmp_path / f"{CID}.review-flags.json").write_text(
        json.dumps(review), encoding="utf-8"
    )
    (tmp_path / f"{CID}.chat-authority.json").write_text(
        json.dumps(chat), encoding="utf-8"
    )
    write_clip_context(tmp_path / f"{CID}.clip-context.json", clip)
    return spec, durations, padded, review, chat


def test_boundary_checkpoint_validates_all_surfaces(tmp_path):
    spec, durations, padded, review, _chat = _fixture(tmp_path)
    checkpoint = load_boundary_resume_checkpoint(
        spec=spec,
        durations=durations,
        candidate_id=CID,
        recording_date=DATE,
        out_root=tmp_path,
        padded=padded,
        padded_duration_ms=100_000,
        authoritative_chat=[],
    )
    assert checkpoint.srt_text == SRT
    assert checkpoint.final_review_audit == review
    assert checkpoint.receipt["prior_semantic_verdict_observed"] is False
    assert checkpoint.receipt["completed_provider_stages_replay_allowed"] is False
    assert checkpoint.receipt_path.is_file()


def test_boundary_checkpoint_writes_receipt_only_to_new_namespace(tmp_path):
    spec, durations, padded, _review, _chat = _fixture(tmp_path)
    source_snapshot = {
        path.name: path.read_bytes()
        for path in tmp_path.iterdir()
        if path.is_file()
    }
    destination = tmp_path / "out-v6"
    destination.mkdir()
    checkpoint = load_boundary_resume_checkpoint(
        spec=spec,
        durations=durations,
        candidate_id=CID,
        recording_date=DATE,
        out_root=tmp_path,
        padded=padded,
        padded_duration_ms=100_000,
        authoritative_chat=[],
        receipt_root=destination,
    )
    assert checkpoint.receipt_path.parent == destination
    assert checkpoint.receipt_path.is_file()
    assert not list(tmp_path.glob(f"{CID}.text-boundary-resume-input.*.json"))
    assert {
        name: (tmp_path / name).read_bytes()
        for name in source_snapshot
    } == source_snapshot


def test_boundary_checkpoint_rejects_content_block_even_with_unavailable_marker(tmp_path):
    spec, durations, padded, review, chat = _fixture(tmp_path)
    review["boundary_semantic_review"]["story_closed"] = False
    chat["final_review_audit"] = review
    (tmp_path / f"{CID}.review-flags.json").write_text(json.dumps(review))
    (tmp_path / f"{CID}.chat-authority.json").write_text(json.dumps(chat))
    with pytest.raises(
        BoundaryResumeCheckpointError,
        match="BOUNDARY_RESUME_CONTENT_VERDICT_PRESENT",
    ):
        load_boundary_resume_checkpoint(
            spec=spec,
            durations=durations,
            candidate_id=CID,
            recording_date=DATE,
            out_root=tmp_path,
            padded=padded,
            padded_duration_ms=100_000,
            authoritative_chat=[],
        )


def test_boundary_checkpoint_rejects_single_surface_tamper(tmp_path):
    spec, durations, padded, _review, _chat = _fixture(tmp_path)
    (tmp_path / "padded.fresh.srt").write_text(SRT.replace("收束", "篡改"))
    with pytest.raises(
        BoundaryResumeCheckpointError,
        match="BOUNDARY_RESUME_FINAL_SRT_HASH_MISMATCH",
    ):
        load_boundary_resume_checkpoint(
            spec=spec,
            durations=durations,
            candidate_id=CID,
            recording_date=DATE,
            out_root=tmp_path,
            padded=padded,
            padded_duration_ms=100_000,
            authoritative_chat=[],
        )


@pytest.mark.parametrize(
    "error_type,evidence,allowed",
    [
        ("PermissionError", None, False),
        ("ValueError", None, False),
        ("LlmCallError", {"failure_class": "permission_denied"}, False),
        ("LlmCallError", {"failure_class": "stage_exception"}, False),
        ("HTTPError", {"failure_class": "provider_transport",
                       "provider_class": "rejected"}, False),
        ("LlmCallError", {"failure_class": "provider_transport",
                          "provider_class": "quota",
                          "provider_status_codes": [429, 403]}, False),
        ("TimeoutError", None, True),
        ("LlmCallError", {"failure_class": "provider_transport",
                          "provider_class": "quota",
                          "provider_status_codes": [429]}, True),
    ],
)
def test_boundary_resume_consumes_only_transport_failure_checkpoints(
    tmp_path, error_type, evidence, allowed
):
    spec, durations, padded, review, chat = _fixture(tmp_path)
    boundary = review["boundary_semantic_review"]
    boundary["reason_codes"] = [
        f"BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:{error_type}"
    ]
    if evidence is not None:
        boundary["unavailable_evidence"] = {
            **evidence, "semantic_verdict_observed": False
        }
    (tmp_path / f"{CID}.review-flags.json").write_text(json.dumps(review))
    (tmp_path / f"{CID}.chat-authority.json").write_text(json.dumps(chat))
    kwargs = dict(
        spec=spec, durations=durations, candidate_id=CID, recording_date=DATE,
        out_root=tmp_path, padded=padded, padded_duration_ms=100_000,
        authoritative_chat=[],
    )
    if allowed:
        assert load_boundary_resume_checkpoint(**kwargs).receipt["status"] == "VALIDATED"
    else:
        with pytest.raises(
            BoundaryResumeCheckpointError,
            match="BOUNDARY_RESUME_NOT_PROVIDER_UNAVAILABLE",
        ):
            load_boundary_resume_checkpoint(**kwargs)
        assert not list(tmp_path.glob(f"{CID}.text-boundary-resume-input.*.json"))


def test_vad_checkpoint_recomputes_once_then_reuses(tmp_path):
    padded = tmp_path / "padded.wav"
    padded.write_bytes(b"audio")
    calls = []

    def provider(_path, _start, _end):
        calls.append(1)
        return [SpeechSpan(10, 20), SpeechSpan(30, 40)]

    provider.vad_cache_identity = {
        "schema_version": "silero-vad-provider-identity.v1",
        "status": "VERIFIED",
        "script_sha256": _sha(b"script"),
        "model_sha256": _sha(b"model"),
    }
    first, first_receipt = load_or_compute_vad_spans(
        candidate_id=CID,
        out_root=tmp_path,
        padded=padded,
        padded_duration_ms=100,
        provider=provider,
        allow_recompute_if_missing=True,
        recompute_reason="TEST",
        require_reusable_identity=True,
    )
    second, second_receipt = load_or_compute_vad_spans(
        candidate_id=CID,
        out_root=tmp_path,
        padded=padded,
        padded_duration_ms=100,
        provider=provider,
        allow_recompute_if_missing=False,
        recompute_reason="SHOULD_NOT_RUN",
        require_reusable_identity=True,
    )
    assert first == second
    assert calls == [1]
    assert first_receipt["model_dispatch_count"] == 1
    assert second_receipt["model_dispatch_count"] == 0
    assert second_receipt["status"] == "REUSED"


def test_vad_checkpoint_conflict_never_overwrites(tmp_path):
    padded = tmp_path / "padded.wav"
    padded.write_bytes(b"audio")

    def provider(_path, _start, _end):
        return [SpeechSpan(10, 20)]

    provider.vad_cache_identity = {
        "schema_version": "silero-vad-provider-identity.v1",
        "status": "VERIFIED",
        "script_sha256": _sha(b"script"),
        "model_sha256": _sha(b"model"),
    }
    load_or_compute_vad_spans(
        candidate_id=CID,
        out_root=tmp_path,
        padded=padded,
        padded_duration_ms=100,
        provider=provider,
        allow_recompute_if_missing=True,
        recompute_reason="TEST",
        require_reusable_identity=True,
    )
    path = tmp_path / f"{CID}.padded-vad-spans.json"
    original = path.read_bytes()
    document = json.loads(original)
    document["spans"][0]["end_ms"] = 21
    path.write_text(json.dumps(document))
    with pytest.raises(BoundaryResumeCheckpointError, match="VAD_CHECKPOINT_CONFLICT"):
        load_or_compute_vad_spans(
            candidate_id=CID,
            out_root=tmp_path,
            padded=padded,
            padded_duration_ms=100,
            provider=provider,
            allow_recompute_if_missing=True,
            recompute_reason="NO_OVERWRITE",
            require_reusable_identity=True,
        )
    assert json.loads(path.read_text())["spans"][0]["end_ms"] == 21
