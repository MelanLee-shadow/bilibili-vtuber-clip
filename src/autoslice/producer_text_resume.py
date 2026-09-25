"""Resume the producer after a boundary reviewer transport-only failure.

The ordinary producer remains the authority.  This module reconstructs its
post-text result from hash-bound artifacts, dispatches only the missing source
boundary semantic review, and then returns the same ``TextPipelineResult``
shape consumed by boundary resolution and package finalization.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.chat_authority import ChatEvidence
from src.autoslice.producer_boundary_resume_checkpoint import (
    BoundaryResumeCheckpointError,
    canonical_sha256,
    load_boundary_resume_checkpoint,
    load_or_compute_vad_spans,
    seal_checkpoint,
    write_atomic_json,
)


def _forbid_completed_text_stage_replay(*_args, **_kwargs):
    raise RuntimeError("BOUNDARY_RESUME_COMPLETED_TEXT_STAGE_REPLAY_FORBIDDEN")


def _write_attempt_receipt(
    *,
    out_root: Path,
    candidate_id: str,
    input_checkpoint_sha256: object,
    prior_review: Mapping[str, object],
    current_review: Mapping[str, object],
) -> tuple[dict[str, object], Path]:
    receipt = seal_checkpoint(
        {
            "schema_version": "producer-text-boundary-resume-attempt.v1",
            "status": "COMPLETE",
            "candidate_id": candidate_id,
            "input_checkpoint_sha256": input_checkpoint_sha256,
            "provider_dispatch": {
                "source_full_window_boundary_semantic_review": 1,
                "bcut": 0,
                "cpa_transcript_correction": 0,
                "pronoun_review": 0,
                "pre_boundary_final_review": 0,
            },
            "prior_boundary_review_sha256": canonical_sha256(prior_review),
            "current_boundary_review_sha256": canonical_sha256(current_review),
            "current_boundary_status": current_review.get("status"),
            "current_reason_codes": current_review.get("reason_codes"),
            "semantic_verdict_observed": (
                current_review.get("status") in {"PASS", "BLOCK"}
                and not any(
                    str(reason).startswith(
                        "BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:"
                    )
                    for reason in current_review.get("reason_codes") or []
                )
            ),
        }
    )
    digest = str(receipt["checkpoint_sha256"]).removeprefix("sha256:")
    path = out_root / (
        f"{candidate_id}.text-boundary-resume-attempt.{digest[:16]}.json"
    )
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != receipt:
            raise BoundaryResumeCheckpointError(
                "BOUNDARY_RESUME_ATTEMPT_RECEIPT_CONFLICT"
            )
    else:
        write_atomic_json(path, receipt)
    return receipt, path


def _build_exact_final_review_callback(
    *,
    spec: Mapping[str, object],
    cid: str,
    out_root: Path,
    padded: Path,
    adapters: pipeline.TextPipelineAdapters,
    authoritative_chat: Sequence[ChatEvidence],
    clip_context: Mapping[str, object],
    entity_context: pipeline.EntityVerificationContext,
    truth_ownership: Mapping[str, object] | None,
    final_review_audit: dict[str, object],
    screen_read_probe: Callable[[int, int], Mapping[str, object]] | None,
    frozen_boundary_receipt: object,
) -> Callable[[str, Mapping[str, object], int, int], dict[str, object]]:
    """Mirror the ordinary exact-final gate after the recovered boundary."""

    def review_exact_final_srt(
        final_srt_text: str,
        verified_authority_audit: Mapping[str, object],
        timeline_offset_ms: int,
        source_final_end_ms: int,
    ) -> dict[str, object]:
        microcue_findings, microcue_audit = pipeline.discover_priority_findings(
            final_srt_text,
            timeline_offset_ms=timeline_offset_ms,
            entity_verifier=entity_context.verify_confusable_entity,
            out_root=out_root,
            cid=cid,
            truth_full_ownership=truth_ownership,
        )
        exact_correction_audit = pipeline.exact_delivery_correction_audit(
            final_srt_text=final_srt_text,
            correction_audit=final_review_audit,
            source_final_start_ms=timeline_offset_ms,
            source_final_end_ms=source_final_end_ms,
            candidate_id=cid,
            recording_date=str(spec.get("date") or ""),
            selection_hook=str(spec.get("selection_hook") or ""),
            selection_scorecard=spec.get("selection_scorecard"),
            structured_context=pipeline._final_review_structured_context(
                selection_hook=str(spec.get("selection_hook") or ""),
                authoritative_chat=authoritative_chat,
            ),
            candidate_context=pipeline.clip_context_prompt_text(clip_context),
            boundary_max_forward_ms=int(
                spec.get("boundary_repair_extend_cap_ms", 30_000)
            ),
            llm_call=pipeline._build_final_review_llm_call(),
            extract_json=pipeline.extract_json_object,
            disabled=(os.environ.get("AUTOSLICE_DISABLE_FINAL_REVIEW") == "1"),
            frozen_boundary_receipt=frozen_boundary_receipt,
        )
        exact_final_audit = pipeline._run_exact_final_release_review(
            screen_read_probe=screen_read_probe,
            srt_text=final_srt_text,
            correction_audit=exact_correction_audit,
            adapters=adapters,
            authoritative_chat=authoritative_chat,
            selection_hook=str(spec.get("selection_hook") or ""),
            clip_context=clip_context,
            verify_confusable_entity=entity_context.verify_confusable_entity,
            verified_authority_audit=verified_authority_audit,
            timeline_offset_ms=timeline_offset_ms,
            priority_raw_findings=microcue_findings,
            draft_fidelity_contexts=pipeline.fidelity_kept_contexts(
                padded,
                final_srt_text,
            ),
            acoustic_discovery_audit=microcue_audit,
        )
        pipeline.checkpoint_final_review_carryover(
            pipeline.carryover_path(out_root, cid),
            exact_final_audit,
        )
        return exact_final_audit

    return review_exact_final_srt


def _rebuild_entity_context(
    *,
    spec: dict,
    padded: Path,
    padded_dur: int,
    host: str,
    text_override_path: Path | None,
    cid: str,
    out_root: Path,
    clip_context: Mapping[str, object],
    authoritative_chat: list[ChatEvidence],
    adapters: pipeline.TextPipelineAdapters,
    audio_witness_routing: Mapping[str, object] | None,
) -> pipeline.EntityVerificationContext:
    draft_srt = clip_context.get("whole_clip_draft_srt")
    if not isinstance(draft_srt, str):
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_CLIP_CONTEXT_DRAFT_MISSING"
        )
    with tempfile.TemporaryDirectory(
        prefix=".boundary-resume-topic-probe-",
        dir=out_root,
    ) as temporary:
        probe = pipeline._build_entity_verification_context(
            spec=spec,
            padded=padded,
            padded_dur=padded_dur,
            host=host,
            text_override_path=text_override_path,
            cid=cid,
            out_root=Path(temporary),
            srt_text=draft_srt,
            authoritative_chat=authoritative_chat,
            adapters=adapters,
            audio_witness_routing=audio_witness_routing,
        )
    if probe.topic_resolution_audit != clip_context.get("topic_resolution"):
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_TOPIC_RESOLUTION_DRIFT"
        )
    context = pipeline._build_entity_verification_context(
        spec=spec,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        text_override_path=text_override_path,
        cid=cid,
        out_root=out_root,
        srt_text=draft_srt,
        authoritative_chat=authoritative_chat,
        adapters=adapters,
        audio_witness_routing=audio_witness_routing,
    )
    if context.topic_resolution_audit != probe.topic_resolution_audit:
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_TOPIC_RESOLUTION_NONDETERMINISTIC"
        )
    return context


def run_boundary_resume_text_pipeline(
    *,
    spec: dict,
    checkpoint_spec: Mapping[str, object],
    durations: list[int],
    padded: Path,
    padded_dur: int,
    host: str,
    text_override_path: Path | None,
    cid: str,
    checkpoint_root: Path,
    out_root: Path,
    adapters: pipeline.TextPipelineAdapters,
    allow_vad_recompute_if_missing: bool,
) -> pipeline.TextPipelineResult:
    """Retry only the missing boundary review and continue normal consumers."""

    _merged, authoritative_chat = pipeline._collect_timeline_chat(
        spec,
        durations,
    )
    checkpoint = load_boundary_resume_checkpoint(
        spec=checkpoint_spec,
        durations=durations,
        candidate_id=cid,
        recording_date=str(checkpoint_spec.get("date") or ""),
        out_root=checkpoint_root,
        padded=padded,
        padded_duration_ms=padded_dur,
        authoritative_chat=authoritative_chat,
        receipt_root=out_root,
    )
    write_atomic_json(
        out_root / f"{cid}.clip-context.json",
        checkpoint.clip_context,
    )
    cues = [
        cue
        for cue in pipeline.parse_srt_cues(checkpoint.srt_text)
        if cue.text.strip()
    ]
    if len(cues) < 3:
        raise BoundaryResumeCheckpointError(
            "BOUNDARY_RESUME_TRANSCRIPTION_TOO_SPARSE"
        )
    final_review_audit = deepcopy(checkpoint.final_review_audit)
    chat_authority_audit = deepcopy(checkpoint.chat_authority_audit)
    prior_review = deepcopy(final_review_audit["boundary_semantic_review"])
    (
        frozen_boundary_receipt,
        current_review,
        source_boundary_replay,
        exact_interval_replay,
    ) = pipeline.review_source_boundary(
        spec=spec,
        candidate_id=cid,
        cues=cues,
        boundary_target_ms=checkpoint.boundary_target_ms,
        boundary_search_scope=checkpoint.boundary_search_scope,
        current_owner_contract=checkpoint.frozen_owner_contract,
        authoritative_chat=authoritative_chat,
        clip_context=checkpoint.clip_context,
        available_local_source_context_end_ms=sum(durations),
    )
    final_review_audit["boundary_semantic_review"] = current_review
    if exact_interval_replay is not None:
        final_review_audit[
            "reviewed_exact_source_interval_replay"
        ] = exact_interval_replay
    if source_boundary_replay:
        final_review_audit["boundary_receipt_replay"] = {
            "source_full_window": source_boundary_replay,
        }
    attempt_receipt, attempt_path = _write_attempt_receipt(
        out_root=out_root,
        candidate_id=cid,
        input_checkpoint_sha256=checkpoint.receipt.get("checkpoint_sha256"),
        prior_review=prior_review,
        current_review=current_review,
    )
    chat_authority_audit["final_review_audit"] = final_review_audit
    chat_authority_audit["boundary_resume_attempt"] = {
        "path": str(attempt_path),
        "checkpoint_sha256": attempt_receipt["checkpoint_sha256"],
        "input_checkpoint_sha256": checkpoint.receipt.get("checkpoint_sha256"),
    }
    review_path = out_root / f"{cid}.review-flags.json"
    chat_path = out_root / f"{cid}.chat-authority.json"
    write_atomic_json(review_path, final_review_audit)
    write_atomic_json(chat_path, chat_authority_audit)
    if current_review.get("status") != "PASS":
        raise SystemExit(
            "BOUNDARY_SEMANTIC_REVIEW_REQUIRED:"
            + json.dumps(
                current_review.get("reason_codes") or [],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    vad = pipeline.build_ssh_silero_vad_provider(host)
    spans, vad_consumption = load_or_compute_vad_spans(
        candidate_id=cid,
        out_root=out_root,
        padded=padded,
        padded_duration_ms=padded_dur,
        provider=vad,
        allow_recompute_if_missing=allow_vad_recompute_if_missing,
        recompute_reason="BOUNDARY_RESUME_NO_PRIOR_VAD_CHECKPOINT",
        require_reusable_identity=True,
    )
    chat_authority_audit["vad_span_consumption"] = vad_consumption
    write_atomic_json(chat_path, chat_authority_audit)

    routing = chat_authority_audit.get("audio_witness_routing")
    entity_context = _rebuild_entity_context(
        spec=spec,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        text_override_path=text_override_path,
        cid=cid,
        out_root=out_root,
        clip_context=checkpoint.clip_context,
        authoritative_chat=authoritative_chat,
        adapters=adapters,
        audio_witness_routing=(
            routing if isinstance(routing, Mapping) else None
        ),
    )
    truth_ownership = (
        pipeline.resolve_truth_full_ownership(spec)
        or pipeline.resolve_operator_text_full_ownership(spec)
    )
    screen_read_probe = pipeline.build_env_screen_read_probe(padded)
    review_exact_final_srt = _build_exact_final_review_callback(
        spec=spec,
        cid=cid,
        out_root=out_root,
        padded=padded,
        adapters=adapters,
        authoritative_chat=authoritative_chat,
        clip_context=checkpoint.clip_context,
        entity_context=entity_context,
        truth_ownership=truth_ownership,
        final_review_audit=final_review_audit,
        screen_read_probe=screen_read_probe,
        frozen_boundary_receipt=frozen_boundary_receipt,
    )
    return pipeline.TextPipelineResult(
        srt_text=checkpoint.srt_text,
        cues=cues,
        spans=spans,
        transcriber=_forbid_completed_text_stage_replay,
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_path,
        clip_context=checkpoint.clip_context,
        clip_context_path=out_root / f"{cid}.clip-context.json",
        review_exact_final_srt=review_exact_final_srt,
    )
