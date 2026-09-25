from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.producer_boundary_resume_checkpoint import (
    BoundaryResumeCheckpoint,
)
from src.autoslice import producer_text_resume as resume
from src.autoslice.subtitle_timing_qa import SpeechSpan


CID = "resume-control-flow"
SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\n开场\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\n故事\n\n"
    "3\n00:00:05,000 --> 00:00:06,000\n收束\n"
)


def _checkpoint(tmp_path: Path) -> BoundaryResumeCheckpoint:
    prior = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "BLOCK",
        "candidate_id": CID,
        "reason_codes": ["BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError"],
    }
    audit = {
        "schema_version": "final-review-audit.v1",
        "status": "PARTIAL",
        "findings": [],
        "applied_count": 0,
        "boundary_semantic_review": prior,
    }
    return BoundaryResumeCheckpoint(
        srt_text=SRT,
        final_review_audit=audit,
        chat_authority_audit={
            "final_review_audit": audit,
            "audio_witness_routing": {},
        },
        clip_context={
            "whole_clip_draft_srt": SRT,
            "topic_resolution": {"status": "NO_GRAPH"},
        },
        frozen_owner_contract={"contract_sha256": "sha256:" + "a" * 64},
        boundary_target_ms=6_000,
        boundary_search_scope={"review_target_ms": 6_000},
        receipt={"checkpoint_sha256": "sha256:" + "b" * 64},
        receipt_path=tmp_path / "input.json",
    )


def _adapters():
    def forbidden(*_args, **_kwargs):
        raise AssertionError("completed text provider was replayed")

    return pipeline.TextPipelineAdapters(
        build_aggregate_transcriber=forbidden,
        build_agy_transcriber=forbidden,
        load_term_boundary_surfaces=lambda _spec: [],
        profile_asset_file=lambda name: Path(f"/{name}"),
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=lambda: Path("/missing"),
        topic_graph_expected_sha256=lambda: "",
    )


def _install_common(monkeypatch, tmp_path):
    checkpoint = _checkpoint(tmp_path)
    monkeypatch.setattr(
        resume,
        "load_boundary_resume_checkpoint",
        lambda **_kwargs: checkpoint,
    )
    monkeypatch.setattr(
        pipeline,
        "_collect_timeline_chat",
        lambda _spec, _durations: ([], []),
    )
    monkeypatch.setattr(
        resume,
        "_rebuild_entity_context",
        lambda **_kwargs: pipeline.EntityVerificationContext(
            verify_confusable_entity=lambda _request: None,
            referent_groups=[],
            topic_resolution_audit={"status": "NO_GRAPH"},
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "resolve_truth_full_ownership",
        lambda _spec: None,
    )
    monkeypatch.setattr(
        pipeline,
        "resolve_operator_text_full_ownership",
        lambda _spec: None,
    )
    monkeypatch.setattr(
        pipeline,
        "build_env_screen_read_probe",
        lambda _padded: None,
    )
    return checkpoint


def test_resume_dispatches_only_boundary_review_and_returns_normal_result(
    tmp_path, monkeypatch
):
    checkpoint = _install_common(monkeypatch, tmp_path)
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"media")
    dispatches = []
    pass_review = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "candidate_id": CID,
        "reason_codes": [],
        "recommended_end_ms": 6_000,
    }

    def review_source_boundary(**_kwargs):
        dispatches.append("boundary")
        return None, pass_review, None, None

    monkeypatch.setattr(pipeline, "review_source_boundary", review_source_boundary)
    monkeypatch.setattr(
        pipeline,
        "build_ssh_silero_vad_provider",
        lambda _host: object(),
    )
    monkeypatch.setattr(
        resume,
        "load_or_compute_vad_spans",
        lambda **_kwargs: (
            [SpeechSpan(1_000, 2_000)],
            {
                "status": "RECOMPUTED_NO_PRIOR_CHECKPOINT",
                "model_dispatch_count": 1,
            },
        ),
    )
    result = resume.run_boundary_resume_text_pipeline(
        spec={"candidate_id": CID, "date": "2026-09-24", "pieces": []},
        checkpoint_spec={"candidate_id": CID, "date": "2026-09-24", "pieces": []},
        durations=[10_000],
        padded=padded,
        padded_dur=10_000,
        host="localhost",
        text_override_path=None,
        cid=CID,
        checkpoint_root=tmp_path / "checkpoint",
        out_root=tmp_path,
        adapters=_adapters(),
        allow_vad_recompute_if_missing=True,
    )
    assert dispatches == ["boundary"]
    assert result.srt_text == SRT
    assert result.cues[-1].text == "收束"
    assert result.spans == [SpeechSpan(1_000, 2_000)]
    assert result.chat_authority_audit["final_review_audit"][
        "boundary_semantic_review"
    ] == pass_review
    with pytest.raises(
        RuntimeError,
        match="BOUNDARY_RESUME_COMPLETED_TEXT_STAGE_REPLAY_FORBIDDEN",
    ):
        result.transcriber(padded, [])
    stored = json.loads((tmp_path / f"{CID}.review-flags.json").read_text())
    assert stored["boundary_semantic_review"]["status"] == "PASS"
    attempts = list(tmp_path.glob(f"{CID}.text-boundary-resume-attempt.*.json"))
    assert len(attempts) == 1
    attempt = json.loads(attempts[0].read_text())
    assert attempt["provider_dispatch"] == {
        "source_full_window_boundary_semantic_review": 1,
        "bcut": 0,
        "cpa_transcript_correction": 0,
        "pronoun_review": 0,
        "pre_boundary_final_review": 0,
    }
    assert checkpoint.final_review_audit["boundary_semantic_review"]["status"] == "BLOCK"


def test_resume_provider_failure_persists_diagnostics_before_vad(tmp_path, monkeypatch):
    _install_common(monkeypatch, tmp_path)
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"media")
    unavailable = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "BLOCK",
        "candidate_id": CID,
        "reason_codes": ["BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError"],
        "unavailable_evidence": {
            "semantic_verdict_observed": False,
            "failure_class": "provider_transport",
            "provider_class": "service",
            "provider_status_codes": [503],
        },
    }
    monkeypatch.setattr(
        pipeline,
        "review_source_boundary",
        lambda **_kwargs: (None, unavailable, None, None),
    )
    monkeypatch.setattr(
        resume,
        "load_or_compute_vad_spans",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("VAD ran before boundary recovered")
        ),
    )
    with pytest.raises(SystemExit, match="BOUNDARY_SEMANTIC_REVIEW_REQUIRED"):
        resume.run_boundary_resume_text_pipeline(
            spec={"candidate_id": CID, "date": "2026-09-24", "pieces": []},
            checkpoint_spec={"candidate_id": CID, "date": "2026-09-24", "pieces": []},
            durations=[10_000],
            padded=padded,
            padded_dur=10_000,
            host="localhost",
            text_override_path=None,
            cid=CID,
            checkpoint_root=tmp_path / "checkpoint",
            out_root=tmp_path,
            adapters=_adapters(),
            allow_vad_recompute_if_missing=True,
        )
    stored = json.loads((tmp_path / f"{CID}.review-flags.json").read_text())
    assert stored["boundary_semantic_review"]["unavailable_evidence"][
        "provider_status_codes"
    ] == [503]
    assert not (tmp_path / f"{CID}.padded-vad-spans.json").exists()
