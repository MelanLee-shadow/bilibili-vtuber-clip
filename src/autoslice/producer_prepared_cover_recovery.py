"""Cover-only successors from sealed partial Talk prepares.

The original subtitle, burn, audio and editorial receipts are read-only inputs.
Only a new current cover verdict can add the missing cover to a new handle.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from .fixed_cover_stage import FixedCoverStageOptions
from .normal_cover_generation_checkpoint import _read_json, canonical, file_identity
from .producer_delivery_prepare import (
    prepare_talk_delivery,
    talk_delivery_summary,
    emit_talk_delivery_summary,
)
from .producer_media import _write_json_atomic
from .producer_prepared_recovery import _bound, _document
from .publish_staging_paths import validate_staged_editorial_files, current_editorial_binding
from .shadow_review import _sha256


@contextmanager
def _retained_environment(args, old):
    values = {
        "AUTOSLICE_REVIEW_COVER_FAILURE": str(args.resume_cover_failure),
        "AUTOSLICE_REVIEW_COVER_FAILURE_SHA256": args.resume_cover_failure_sha256,
    }
    # The frozen cover may have consumed a runtime-owned session context.
    # Restore its locator only after proving the exact original preimage;
    # never replace an explicitly conflicting current context.
    environment = old.get("provider", {}).get("environment", {})
    context = environment.get("LIDOUSHA_SESSION_GAME_CONTEXT")
    if context:
        dependency = next(
            (row for row in old.get("dependencies", []) if row.get("path") == context), None
        )
        if dependency is None or file_identity(Path(context)) != dependency:
            raise ValueError("PREPARED_COVER_SESSION_CONTEXT_DRIFT")
        for name in ("LIDOUSHA_SESSION_GAME_CONTEXT", "LIDOUSHA_DISABLE_SESSION_GAME_CONTEXT"):
            value = environment.get(name)
            if os.environ.get(name) not in (None, value):
                raise ValueError("PREPARED_COVER_SESSION_CONTEXT_CONFLICT")
            if value is not None:
                values[name] = value
    names = {*values, "AUTOSLICE_REUSE_COVER_RESULT", "AUTOSLICE_REUSE_COVER_RESULT_SHA256"}
    prior = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ.update(values)
        yield
    finally:
        for name, value in prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _cover_successor_documents(
    *, record: dict, publish: dict, result: dict, publish_path: Path
) -> tuple[dict, dict]:
    """Replace the cover surface while preserving all reviewed text inputs."""
    record, publish = deepcopy(record), deepcopy(publish)
    staging = record["publish_staging"]
    if result.get("status") != "AI_COVER_READY":
        raise ValueError("PREPARED_COVER_CURRENT_QC_REQUIRED")
    if staging.get("reason_codes") != ["COVER_FINAL_HOST_IDENTITY_UNVERIFIED"]:
        raise ValueError("PREPARED_COVER_OTHER_FAILURE_REQUIRES_NORMAL_REVIEW")
    updates = {
        "cover_path": result["cover_path"],
        "cover_status": result["status"],
        "cover_generation": result["cover_generation"],
        "reason_codes": [],
        "upload_enabled": False,
    }
    staging.update(updates)
    publish.update(deepcopy(updates))
    staging["publish_json_path"] = str(publish_path)
    hashes = record["artifact_hashes"]
    for key in ("cover_sha256", "ai_background_sha256", "cover_reference_sha256"):
        hashes[key] = result[key]
    publish["artifact_hashes"] = deepcopy(hashes)
    publish["artifact_hashes"].pop("publish_draft_sha256", None)
    staging["current_binding"] = current_editorial_binding(
        record, staging, candidate_id=record["candidate_id"]
    )
    publish["current_binding"] = deepcopy(staging["current_binding"])
    return record, publish


def resume_prepared_cover(*, args, request, recovery, delivery_root: Path) -> int:
    from .publish_staging import _stage_ai_cover
    from .producer_package_finalization import _audit_story_bound_cover

    record = recovery.record
    staging = record["publish_staging"]
    subtitle = Path(record["subtitle_path"])
    validate_staged_editorial_files(
        record, staging, candidate_id=request.cid, subtitle_path=subtitle
    )
    publish = _document(recovery.roles["publish"])
    _bound(recovery.roles["publish"], record["artifact_hashes"]["publish_draft_sha256"])
    if (
        staging.get("status") != "STAGED"
        or staging.get("title_authority_error")
        or staging.get("reason_codes") != ["COVER_FINAL_HOST_IDENTITY_UNVERIFIED"]
        or publish.get("upload_enabled") is not False
    ):
        raise ValueError("PREPARED_COVER_OTHER_FAILURE_REQUIRES_NORMAL_REVIEW")
    failure = _read_json(
        args.resume_cover_failure,
        expected_sha256="sha256:" + args.resume_cover_failure_sha256.removeprefix("sha256:"),
    )
    old = failure["identity"]
    if old.get("title") != staging["title"] or old.get("cover_text") != staging["cover_text"]:
        raise ValueError("PREPARED_COVER_EDITORIAL_DRIFT")
    root = request.out_root
    input_path = root / "prepared-cover-resume-input.json"
    identity = {
        "schema_version": "producer-prepared-cover-resume.v1",
        "candidate_id": request.cid,
        "prepared_path": str(args.resume_prepared),
        "prepared_sha256": recovery.prepared_sha256,
        "spec_sha256": args.resume_spec_sha256,
        "failure": file_identity(args.resume_cover_failure),
        "original_artifacts": [file_identity(path) for path in recovery.roles.values()],
        "prepare_qc_only": args.resume_cover_prepare_qc,
        "upload_enabled": False,
    }
    if input_path.exists():
        if _document(input_path) != identity:
            raise ValueError("PREPARED_COVER_SUCCESSOR_INPUT_DRIFT")
    else:
        if any(root.iterdir()):
            raise ValueError("PREPARED_COVER_SUCCESSOR_NOT_EMPTY")
        with input_path.open("xb") as stream:
            stream.write(canonical(identity))
    options = old["options"]
    with _retained_environment(args, old):
        result = _stage_ai_cover(
            record,
            media_path=Path(record["media_path"]),
            candidate_id=request.cid,
            title=staging["title"],
            cover_text=staging["cover_text"],
            run_ffmpeg=True,
            enforce_final_host_identity=True,
            punch_allowed=options.get("punch_allowed", False),
            diversity_slot=options.get("diversity_slot"),
            full_text_cover_contract=options.get("full_text_cover_contract"),
            fixed_options=FixedCoverStageOptions(**options["fixed_options"]),
            private_artifact_root=root,
            retained_artifact_root=str(Path(record["media_path"]).parent),
            retained_prepare_qc_only=args.resume_cover_prepare_qc,
        )
    _write_json_atomic(root / "cover-resume-review.json", result)
    if result.get("status") != "AI_COVER_READY":
        print(
            json.dumps(
                {
                    "candidate_id": request.cid,
                    "status": "cover_resume_pending",
                    "review_path": str(root / "cover-resume-review.json"),
                    "reason_codes": result.get("reason_codes"),
                    "upload_enabled": False,
                },
                ensure_ascii=False,
            )
        )
        return 4
    # Check retained original preimages again after the asynchronous witness.
    for item in identity["original_artifacts"]:
        if file_identity(Path(item["path"])) != item:
            raise ValueError("PREPARED_COVER_ORIGINAL_ARTIFACT_DRIFT")
    _bound(args.resume_cover_failure, args.resume_cover_failure_sha256)
    publish_path = root / f"{request.cid}.publish.json"
    record, publish = _cover_successor_documents(
        record=record, publish=publish, result=result, publish_path=publish_path
    )
    codes, _ = _audit_story_bound_cover(record["publish_staging"], record["story_contract"])
    if codes:
        raise ValueError("PREPARED_COVER_CURRENT_STORY_QC_FAILED:" + ",".join(codes))
    _write_json_atomic(publish_path, publish)
    record["artifact_hashes"]["publish_draft_sha256"] = "sha256:" + _sha256(publish_path)
    record["prepared_cover_recovery"] = identity
    record_path = root / f"{request.cid}.record.json"
    _write_json_atomic(record_path, record)
    roles = recovery.roles
    handle = prepare_talk_delivery(
        spec=request.spec,
        candidate_id=request.cid,
        record=record,
        staging=record["publish_staging"],
        record_path=record_path,
        subtitle_path=subtitle,
        speaker_review_srt=None,
        speaker_ass=None,
        speaker_manifest_path=None,
        redelivery_baseline_audit_path=roles.get("redelivery_baseline"),
        subtitle_regression_audit_path=roles.get("subtitle_regression"),
        chat_authority_path=Path(record["chat_authority_audit_path"]),
        talk_filler_audit_path=roles.get("filler_audit"),
        text_manifest_path=roles.get("text_finalization"),
        delivery_root=delivery_root,
    )
    from . import speaker_guess

    emit_talk_delivery_summary(
        talk_delivery_summary(
            candidate_id=request.cid,
            final_end=record["end_ms"],
            record=record,
            audit=record["boundary_audit"],
            timing_qa=record["subtitle_timing_qa"],
            staging=record["publish_staging"],
            delivery=delivery_root / request.spec["date"] / request.cid,
            speaker_review_srt=None,
            speaker_ass=None,
            speaker_manifest=None,
            speaker_guess=speaker_guess,
            subtitle_regression_audit=None,
            redelivery_baseline_audit=None,
            talk_filler_audit_path=roles.get("filler_audit"),
            prepared=handle,
        )
    )
    return 0
