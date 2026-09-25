from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.produce_slice_package as producer
from src.autoslice.producer_boundary_resolution import BoundaryResolution


CID = "resume-entry-integration"


def test_resume_entry_reaches_normal_resolver_and_final_delivery_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovered source review must rejoin the ordinary downstream pipeline.

    The checkpoint namespace remains the read-only input, the resume artifacts
    are created under a new namespace, and the resulting text authority is the
    one consumed by both the normal boundary resolver and the final-delivery
    reviewer supplied to package finalization.
    """

    spec_path = tmp_path / "candidate.json"
    spec_path.write_text("{}\n", encoding="utf-8")
    checkpoint_root = tmp_path / "out-v5" / CID
    checkpoint_root.mkdir(parents=True)
    resume_namespace = tmp_path / "out-v7"
    padded = tmp_path / "source-window.mp4"
    padded.write_bytes(b"source-window")
    padded_provenance = tmp_path / "source-window.provenance.json"
    padded_provenance.write_text("{}\n", encoding="utf-8")

    original_spec = {
        "candidate_id": CID,
        "date": "2026-09-25",
        "selection_hook": "恢复后完成同一故事",
        "pieces": [],
    }
    args = SimpleNamespace(
        spec=spec_path,
        substrate="aggregate_asr",
        resume_text_checkpoint=True,
        resume_output_namespace=resume_namespace,
        allow_resume_vad_recompute=False,
    )
    request = SimpleNamespace(
        spec=original_spec,
        boundary_repair_extend_cap_ms=60_000,
        branding_intro=None,
        cid=CID,
        out_root=checkpoint_root,
        host="localhost",
        text_override_path=None,
        subtitle_regression_path=None,
    )
    source_media = SimpleNamespace(
        durations=[10_000],
        padded=padded,
        padded_duration_ms=10_000,
        padded_provenance_path=padded_provenance,
        piece_provenance_rows=[],
    )

    source_review = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "candidate_id": CID,
        "review_scope": "source_full_window",
        "reason_codes": [],
    }
    resolved_source_review = {
        **source_review,
        "resolver_binding": "PASS",
    }
    final_delivery_review = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "candidate_id": CID,
        "review_scope": "final_delivery",
        "reason_codes": [],
    }
    final_review_audit = {
        "schema_version": "final-review-audit.v1",
        "status": "PARTIAL",
        "findings": [],
        "applied_count": 0,
        "boundary_semantic_review": source_review,
    }
    chat_authority = {
        "frozen_boundary_owner_contract": {"status": "PASS"},
        "final_review_audit": final_review_audit,
    }
    exact_calls: list[dict[str, object]] = []

    def exact_final_review(
        final_srt_text: str,
        verified_authority_audit: dict[str, object],
        timeline_offset_ms: int,
        source_final_end_ms: int,
    ) -> dict[str, object]:
        exact_calls.append(
            {
                "final_srt_text": final_srt_text,
                "verified_authority_audit": verified_authority_audit,
                "timeline_offset_ms": timeline_offset_ms,
                "source_final_end_ms": source_final_end_ms,
            }
        )
        return {
            "schema_version": "final-review-audit.v2",
            "status": "CLEAN",
            "boundary_semantic_review": final_delivery_review,
        }

    calls: dict[str, object] = {}

    monkeypatch.setattr(
        producer,
        "parse_producer_args",
        lambda *_args, **_kwargs: args,
    )
    monkeypatch.setattr(
        producer,
        "load_producer_request",
        lambda *_args, **_kwargs: request,
    )

    def prepare_source_media(**kwargs: object) -> SimpleNamespace:
        calls["source_media"] = kwargs
        return source_media

    monkeypatch.setattr(producer, "prepare_source_media", prepare_source_media)
    monkeypatch.setattr(
        producer,
        "select_nonaggregate_transcriber_builder",
        lambda *_args, **_kwargs: (
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("completed transcription provider was replayed")
            )
        ),
    )

    def run_resume(**kwargs: object) -> SimpleNamespace:
        calls["resume"] = kwargs
        new_root = kwargs["out_root"]
        assert isinstance(new_root, Path)
        return SimpleNamespace(
            srt_text=(
                "1\n00:00:00,000 --> 00:00:02,000\n开场\n\n"
                "2\n00:00:02,000 --> 00:00:04,000\n发展\n\n"
                "3\n00:00:04,000 --> 00:00:06,000\n收束\n"
            ),
            cues=["resume-cue-grid"],
            spans=["resume-vad-spans"],
            transcriber=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("completed text provider was replayed")
            ),
            chat_authority_audit=chat_authority,
            chat_authority_path=new_root / f"{CID}.chat-authority.json",
            clip_context={"schema_version": "clip-context.v1", "candidate_id": CID},
            clip_context_path=new_root / f"{CID}.clip-context.json",
            review_exact_final_srt=exact_final_review,
        )

    monkeypatch.setattr(producer, "run_boundary_resume_text_pipeline", run_resume)
    monkeypatch.setattr(
        producer,
        "run_text_pipeline",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("ordinary text pipeline replayed completed provider stages")
        ),
    )
    monkeypatch.setattr(
        producer,
        "structured_chat_payoff_scope_ms_from_frozen_contract",
        lambda _contract: None,
    )

    def resolve_boundary(**kwargs: object) -> BoundaryResolution:
        calls["resolver"] = kwargs
        assert kwargs["cues"] == ["resume-cue-grid"]
        assert kwargs["spans"] == ["resume-vad-spans"]
        assert kwargs["chat_authority_audit"] is chat_authority
        assert kwargs["spec"]["boundary_semantic_review"] == source_review
        return BoundaryResolution(
            final_start=1_000,
            final_end=6_000,
            audit={"boundary_semantic_review": resolved_source_review},
            sanitized_cues=[],
            timing_qa={"status": "PASS"},
        )

    monkeypatch.setattr(producer, "resolve_producer_boundary", resolve_boundary)
    monkeypatch.setattr(
        producer,
        "write_final_filler_audit",
        lambda **_kwargs: tmp_path / "filler-audit.json",
    )
    finalization_options = object()
    monkeypatch.setattr(
        producer,
        "finalization_options_from_args",
        lambda _args: finalization_options,
    )
    monkeypatch.setattr(
        producer,
        "select_accurate_recut_command",
        lambda **_kwargs: (lambda **_inner: ["recut"]),
    )

    def finalize(**kwargs: object) -> int:
        calls["finalize"] = kwargs
        assert kwargs["options"] is finalization_options
        assert kwargs["spec"]["boundary_semantic_review"] == resolved_source_review
        assert kwargs["chat_authority_audit"]["final_review_audit"][
            "boundary_semantic_review"
        ] == resolved_source_review
        callback = kwargs["adapters"].run_exact_final_review
        assert callback is exact_final_review
        exact = callback("final delivery bytes", {"status": "PASS"}, 1_000, 6_000)
        assert exact["boundary_semantic_review"] == final_delivery_review
        return 23

    monkeypatch.setattr(producer, "finalize_producer_package", finalize)

    assert producer.main([]) == 23

    resume_call = calls["resume"]
    assert resume_call["checkpoint_root"] == checkpoint_root
    assert resume_call["checkpoint_spec"] is original_spec
    assert resume_call["spec"] is not original_spec
    assert resume_call["out_root"] == resume_namespace / CID
    assert resume_call["spec"]["output_root"] == str(resume_namespace)
    assert "output_root" not in original_spec

    source_call = calls["source_media"]
    assert source_call["spec"] is original_spec
    assert source_call["out_root"] == checkpoint_root
    assert source_call["spec_parent"] == spec_path.parent
    assert source_call["require_existing_cache"] is True
    assert (resume_namespace / CID).is_dir()
    assert len(exact_calls) == 1
    assert exact_calls[0]["timeline_offset_ms"] == 1_000
    assert exact_calls[0]["source_final_end_ms"] == 6_000
