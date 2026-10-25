"""Normal producer consumption of its own bound source-fact rescore input."""

from __future__ import annotations
from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import json
from pathlib import Path
from . import selection_rescore as rescore
from .final_subtitle_audio_gate import load_validated_subtitle_audio_witness
from .normal_text_generation_checkpoint import completed_generation
from .producer_media import (
    _validated_burned_artifact,
    _validated_burned_ass_artifact,
    _write_json_atomic,
)
from .producer_text_checkpoint import _frozen_binding
from .producer_final_review_transport import build_final_review_llm_call
from .publish_staging import _restore_reviewed_scorecard_input
from .selection_scorecard import selection_scorecard_production_admission
from .semantic_candidate_selector import rescore_candidate_scorecard
from .source_fact_review import (
    _finalize_receipt,
    _validate_receipt_entity_context,
    SCHEMA_VERSION,
    RESCORE_CANDIDATE_SCHEMA_VERSION,
)
from .source_fact_rescore_candidate_validation import source_fact_rescore_candidate_shape_is_valid


def _file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("SOURCE_FACT_RESCORE_ARTIFACT_INVALID")
    return "sha256:" + _evidence_file_sha256(path)


def producer_rescore_binding(
    *, spec, cid, recut_dir, record, review, cues, context_prompt, stale_card
):
    srt = recut_dir / f"{cid}.recut.srt"
    stale_card = _restore_reviewed_scorecard_input(
        {"selection_scorecard": stale_card, "source_fact_review": review},
        allow_pending_rescore=True,
    )["selection_scorecard"]
    body = dict(review)
    digest = body.pop("receipt_sha256", None)
    if (
        _finalize_receipt(body).get("receipt_sha256") != digest
        or not _validate_receipt_entity_context(
            review,
            candidate_id=cid,
            final_reviewed_srt_path=srt,
            clip_context_prompt=context_prompt,
        )
        or (
            not source_fact_rescore_candidate_shape_is_valid(
                review,
                selection_hook=review.get("final_selection_hook"),
                title=review.get("final_title"),
                selection_scorecard_sha256=rescore._sha256_json(stale_card),
                schema_version=SCHEMA_VERSION,
                rescore_candidate_schema_version=RESCORE_CANDIDATE_SCHEMA_VERSION,
            )
        )
    ):
        raise ValueError("SOURCE_FACT_RESCORE_REVIEW_DRIFT")
    block = review["rescore_candidate"]
    last = review["passes"][-1]
    if (
        last.get("final_reviewed_srt_sha256") is not None
        and _file(srt) != last["final_reviewed_srt_sha256"]
    ):
        raise ValueError("SOURCE_FACT_RESCORE_REVIEWED_SRT_DRIFT")
    transcript = "\n".join((c.text for c in cues))
    if rescore._sha256_text(transcript) != last.get(
        "final_transcript_sha256"
    ) or rescore._sha256_text(context_prompt) != last.get("clip_context_prompt_sha256"):
        raise ValueError("SOURCE_FACT_RESCORE_INPUT_DRIFT")
    media = _validated_burned_artifact(record)
    ass = _validated_burned_ass_artifact(record)
    load_validated_subtitle_audio_witness(record, srt, media, candidate_id=cid)
    authority = Path(record["chat_authority_audit_path"])
    return {
        "schema_version": "producer-source-fact-rescore-input.v1",
        "candidate_id": cid,
        "review_receipt_sha256": review["receipt_sha256"],
        "repaired_hook_sha256": block["repaired_selection_hook_sha256"],
        "stale_scorecard_sha256": rescore._sha256_json(stale_card),
        "spec": {
            k: spec.get(k)
            for k in (
                "candidate_id",
                "date",
                "profile_binding",
                "pieces",
                "semantic_start_ms",
                "semantic_end_ms",
                "selection_hook",
                "selection_scorecard",
                "boundary_semantic_review",
            )
        },
        "files": {
            "srt": _file(srt),
            "burn": _file(media),
            "ass": _file(ass),
            "authority": _file(authority),
        },
        "final_transcript_sha256": rescore._sha256_text(transcript),
        "clip_context_prompt_sha256": rescore._sha256_text(context_prompt),
    }


def consume_producer_rescore(
    *, spec, cid, recut_dir, record, review, cues, context_prompt, stale_card, llm_call=None
):
    """Rescore the real private/canonical output; no runner state is invented."""
    binding = producer_rescore_binding(
        spec=spec,
        cid=cid,
        recut_dir=recut_dir,
        record=record,
        review=review,
        cues=cues,
        context_prompt=context_prompt,
        stale_card=stale_card,
    )
    stale_card = _restore_reviewed_scorecard_input(
        {"selection_scorecard": stale_card, "source_fact_review": review},
        allow_pending_rescore=True,
    )["selection_scorecard"]
    sidecar = rescore.write_pending_rescore_sidecar(
        recut_dir, candidate_id=cid, source_fact_review=review
    )
    receipt = json.loads(sidecar.read_bytes())
    digest = rescore._sha256_json(binding)
    prior_binding = receipt.get("producer_binding_sha256")
    if prior_binding is not None and prior_binding != digest:
        raise ValueError("SOURCE_FACT_RESCORE_PRODUCER_BINDING_DRIFT")
    _frozen_binding(recut_dir / ".source-fact-rescore-history", "sidecar", sidecar)
    hooks = list(receipt.get("producer_consumed_hooks") or [])
    hook_sha = binding["repaired_hook_sha256"]
    if (
        hook_sha in hooks
        and prior_binding is None
        or (hook_sha not in hooks and len(hooks) >= rescore.SOURCE_FACT_RESCORE_CAP)
    ):
        raise SystemExit("SOURCE_FACT_RESCORE_CYCLE_OR_CAP")
    observation_path = (
        recut_dir / ".source-fact-rescore-consumptions" / (digest.removeprefix("sha256:") + ".json")
    )
    prior = None
    if observation_path.exists() or observation_path.is_symlink():
        _file(observation_path)
        prior = json.loads(observation_path.read_bytes())
        if prior.get("binding") != binding:
            raise ValueError("SOURCE_FACT_RESCORE_PRODUCER_BINDING_DRIFT")
    else:
        _write_json_atomic(observation_path, {"binding": binding, "status": "DISPATCHED"})
    receipt["producer_binding_sha256"] = digest
    receipt["producer_request_path"] = str(observation_path)
    _write_json_atomic(sidecar, receipt)
    call = llm_call if llm_call is not None else build_final_review_llm_call()

    def completion(prompt):
        return completed_generation(
            artifact_root=recut_dir,
            candidate_id=cid,
            lane="selection_rescore",
            attempt=1,
            prompt=prompt,
            call=call,
            dependencies=lambda: producer_rescore_binding(
                spec=spec,
                cid=cid,
                recut_dir=recut_dir,
                record=record,
                review=review,
                cues=cues,
                context_prompt=context_prompt,
                stale_card=stale_card,
            ),
            allow_dispatch=prior is None,
        )

    outcome = rescore_candidate_scorecard(
        candidate_id=cid,
        cues=cues,
        repaired_hook=receipt["repaired_hook"],
        clip_context_prompt=context_prompt,
        llm_call=completion,
        stale_scorecard=stale_card,
    )
    if outcome["outcome"] == "PROVIDER_UNAVAILABLE":
        raise SystemExit("SOURCE_FACT_RESCORE_INFRA_UNRESOLVED")
    if (
        producer_rescore_binding(
            spec=spec,
            cid=cid,
            recut_dir=recut_dir,
            record=record,
            review=review,
            cues=cues,
            context_prompt=context_prompt,
            stale_card=stale_card,
        )
        != binding
    ):
        raise ValueError("SOURCE_FACT_RESCORE_PRODUCER_BINDING_DRIFT")
    admission = (
        selection_scorecard_production_admission(outcome.get("selection_scorecard"))
        if outcome["outcome"] == "RESCORED"
        else {"status": "HOLD", "reason_code": outcome.get("reason_code")}
    )
    saved = {"binding": binding, "status": "COMPLETED", "outcome": outcome, "admission": admission}
    _write_json_atomic(observation_path, saved)
    if hook_sha not in hooks:
        hooks.append(hook_sha)
    if not any((a.get("rescore_fingerprint") == digest for a in receipt.get("attempts") or [])):
        rescore._record_rescore_attempt(
            receipt,
            fingerprint=digest,
            outcome=outcome["outcome"],
            new_scorecard_sha256=rescore._sha256_json(outcome["selection_scorecard"])
            if outcome["outcome"] == "RESCORED"
            else None,
        )
    receipt.update(
        producer_consumed_hooks=hooks,
        status="RESCORED_REQUEUED" if admission["status"] == "ADMIT" else "FAILED",
        producer_result=saved,
    )
    _write_json_atomic(sidecar, receipt)
    if admission["status"] != "ADMIT":
        raise SystemExit(
            "SOURCE_FACT_RESCORE_CANDIDATE_REJECTED:" + str(admission.get("reason_code"))
        )
    spec.update(
        selection_hook=receipt["repaired_hook"], selection_scorecard=outcome["selection_scorecard"]
    )
    record["source_fact_rescore_consumption"] = receipt
    return True
