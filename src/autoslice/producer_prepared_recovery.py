"""Ordinary producer recovery from sealed, completed private Talk stages.

The handle supplies immutable inputs, not a current content verdict. Recovery
forks writable text/authority into a separate successor and enters the normal
postburn/exact-repair loop. Original media, receipts and budgets remain bound.
"""

from __future__ import annotations

import json
import re
import shutil
import hashlib
from dataclasses import dataclass
from pathlib import Path
from copy import deepcopy

from src.autoslice.clip_context import validate_clip_context
from src.autoslice.final_review_contract import (
    _validate_exact_final_cpa_self_heal,
    _validate_review_geometry,
)
from src.autoslice.final_subtitle_audio_gate import validate_final_subtitle_audio_check
from src.autoslice.prepared_review_recovery import historical_prepared_review
from src.autoslice.prepared_native_audio_recovery import PreparedNativeAudioState, inherit_prepared_native_audio
from src.autoslice.producer_delivery_transaction import load_prepared_delivery
from src.autoslice.producer_media import (
    _validated_burned_artifact,
    _validated_burned_ass_artifact,
    _write_json_atomic,
    ffprobe_duration_ms,
)
from src.autoslice.producer_package_finalization import (
    AuthorityArtifacts,
    CompletedFinalizationArtifacts,
    FinalRecutArtifacts,
    SpeakerArtifacts,
    StagedRecord,
    _replay_bound_burn,
)
from src.autoslice.producer_source_media import _bind_piece_source_media_sha256, source_piece_clock
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice.shadow_review import _sha256


def _fail(reason: str) -> None:
    raise ValueError("PREPARED_RESUME_" + reason)


def _bound(path: Path, expected: object) -> Path:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or str(expected).removeprefix("sha256:") != _sha256(path)
    ):
        _fail("ARTIFACT_DRIFT:" + str(path))
    return path


def _document(path: Path, *, review: bool = False) -> dict:
    if path.is_symlink() or not path.is_file():
        _fail("DOCUMENT_INVALID:" + str(path))
    raw = path.read_bytes()
    if review:
        from src.autoslice.review_evidence_store import review_from_bytes

        result = review_from_bytes(raw, directory=path.parent)
    else:
        result = json.loads(raw)
        from src.autoslice.review_evidence_store import expand

        result = expand(result)
    if not isinstance(result, dict):
        _fail("DOCUMENT_INVALID:" + str(path))
    return result


def canonical_prepared_resume_output_root(spec_path: Path, spec_sha256: str, prepared_path: Path) -> Path:
    """Keep ordinary recovery inside the original candidate's private scope.

    The SPEC output_root names the whole date, not the original files' write
    set. A content-addressed descendant can preserve those files while using
    the existing CID-scoped bundle contract on both hosts.
    """
    for path in (spec_path, prepared_path):
        if not path.is_absolute() or path.resolve() != path or not path.is_file():
            _fail("RUNNER_INPUT_PATH_INVALID")
    _bound(spec_path, spec_sha256)
    original = _document(spec_path)
    cid = original.get("candidate_id")
    root = Path(original["output_root"])
    if (not isinstance(cid, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,96}", cid) is None
            or not root.is_absolute() or root.resolve() != root):
        _fail("RUNNER_INPUT_SCOPE_INVALID")
    identity = {"spec_sha256": spec_sha256.removeprefix("sha256:"),
                "prepared_manifest_sha256": _sha256(prepared_path)}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return root / cid / ".prepared-resumes" / digest


def validate_resume_options(args) -> None:
    if (
        not args.prepare_only
        or args.correct != "cpa"
        or args.ssh_host != "localhost"
        or args.speaker_mode != "uniform_host"
    ):
        _fail("REQUIRES_PRIVATE_LOCAL_TALK_CPA_UNIFORM_HOST")
    if any(
        getattr(args, name, None)
        for name in (
            "subtitle_text_overrides",
            "subtitle_regression",
            "speaker_overrides",
            "speaker_source_session_anchors",
            "speaker_mixed_overlap_evidence",
            "screen_text",
        )
    ):
        _fail("NEW_INPUTS_REQUIRE_NORMAL_REVALIDATION")
    if re.fullmatch(r"[0-9a-f]{64}", str(args.resume_spec_sha256 or "")) is None:
        _fail("SPEC_HASH_REQUIRED")
    _bound(args.spec.absolute(), args.resume_spec_sha256)
    failure = getattr(args, "resume_cover_failure", None)
    digest = getattr(args, "resume_cover_failure_sha256", None)
    if bool(failure) != bool(digest) or (getattr(args, "resume_cover_prepare_qc", False) and not failure):
        _fail("COVER_FAILURE_BINDING_REQUIRED")
    if failure:
        _bound(failure, digest)
    output = args.resume_output_root
    if output is None or not output.is_absolute() or output.resolve() != output:
        _fail("OUTPUT_ROOT_MUST_BE_ABSOLUTE_WITHOUT_SYMLINKS")
    original_spec = _document(args.spec)
    original = Path(original_spec["output_root"]).resolve()
    if output.is_relative_to(original) or original.is_relative_to(output):
        cid = original_spec.get("candidate_id")
        if (
            not isinstance(cid, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,96}", cid) is None
            or output.parent != original / cid / ".prepared-resumes"
            or re.fullmatch(r"[0-9a-f]{64}", output.name) is None
            or output != canonical_prepared_resume_output_root(
                args.spec.absolute(), args.resume_spec_sha256, args.resume_prepared,
            )
        ):
            _fail("OUTPUT_OVERLAPS_ORIGINAL")


@dataclass(frozen=True)
class PreparedProducerRecovery:
    completed: CompletedFinalizationArtifacts
    authority: dict
    authority_path: Path
    padded: Path
    padded_provenance_path: Path
    piece_rows: list[dict]
    final_start: int
    final_end: int
    filler_path: Path | None
    native_audio_state: PreparedNativeAudioState


@dataclass(frozen=True)
class PreparedCoverRecovery:
    record: dict
    authority: dict
    roles: dict[str, Path]
    prepared_sha256: str


def _restore_source_piece_bindings(*, spec: dict, original_pieces: list,
                                   context_pieces: list, rows: list) -> list[int]:
    """Replay first-run source bindings into this request after byte validation.

    The original SPEC predates source inspection. Its persisted bytes stay
    intact; later story/context consumers need the same verified in-memory
    fields that normal source preparation supplies.
    """
    if len(rows) != len(original_pieces) or len(rows) != len(context_pieces):
        _fail("SOURCE_PIECE_COUNT_DRIFT")
    if spec.get("pieces") != original_pieces:
        _fail("REQUEST_SOURCE_PIECES_DRIFT")
    bound_pieces = deepcopy(original_pieces)
    durations = []
    for row, piece, context_piece in zip(rows, bound_pieces, context_pieces, strict=True):
        if (
            not isinstance(row, dict)
            or row.get("source_path") != piece.get("remote_media")
            or any(
                row.get(key) != piece.get(key) or row.get(key) != context_piece.get(key)
                for key in ("start_ms", "end_ms")
            )
            or context_piece.get("source_media_sha256") != "sha256:" + str(row.get("source_sha256"))
        ):
            _fail("SOURCE_WINDOW_DRIFT")
        _bound(Path(row["source_path"]), row.get("source_sha256"))
        cut = _bound(Path(row["output_path"]), row.get("output_sha256"))
        _bind_piece_source_media_sha256(piece, source_sha256=row["source_sha256"])
        durations.append(ffprobe_duration_ms(cut))
    spec["pieces"] = bound_pieces
    return durations


def stage_completed_editorial(*, completed: CompletedFinalizationArtifacts, spec: dict,
                             cid: str, recut: FinalRecutArtifacts, record: dict) -> StagedRecord | None:
    """Reuse sealed editorial work only for byte-identical current inputs.

    Current postburn review has already run. This replays the native title,
    source-fact and cover consumers; it does not manufacture generation
    checkpoints or promote the old handle into materialization authority.
    """
    from src.autoslice.producer_completed_prepare_revalidation import validate_completed_prepare_artifacts
    from src.autoslice.candidate_entity_publish_gate import evaluate_candidate_title_gates
    from src.autoslice.authorized_upload_metadata import validate_tag_generation, validate_tags
    from src.autoslice.publish_staging_paths import (
        current_editorial_binding, validate_current_editorial_binding, validate_staged_editorial_files,
    )

    handle = completed.prepared_handle
    if handle is None:
        return None
    with exclusive_runner_commit(handle.runtime_root) as lease:
        checked = historical_prepared_review(handle, lease)
        roles = {entry[0]["role"]: entry[1] for entry in checked.checked}
        original = _document(roles["record"])
        staging = original.get("publish_staging") or {}
        if "cover" not in roles or staging.get("cover_status") != "AI_COVER_READY":
            return None
        validate_completed_prepare_artifacts(handle, {})
        publish = _document(roles["publish"])
        if (
            _sha256(recut.subtitle_path) != _sha256(roles["subtitle"])
            or _sha256(recut.media_path) != _sha256(Path(original["media_path"]))
            or _sha256(_validated_burned_artifact(record)) != _sha256(roles["video"])
            or record.get("story_contract") != original.get("story_contract")
        ):
            return None
        if any(surface.get("upload_enabled") is not False for surface in (staging, publish)):
            _fail("EDITORIAL_NOT_PRIVATE")
        current_staging = record.get("publish_staging")
        if current_staging != staging:
            _fail("EDITORIAL_STAGING_DRIFT")
        current = current_editorial_binding(record, staging, candidate_id=cid)
        if current != current_editorial_binding(original, staging, candidate_id=cid):
            return None
        context = _document(Path(spec["clip_context_path"]))
        if context != spec.get("clip_context"):
            _fail("CONTEXT_DRIFT")
        validate_clip_context(
            context, candidate_id=cid, recording_date=str(spec.get("date") or ""),
            source_media_sha256s=[piece["source_media_sha256"] for piece in spec["pieces"]],
        )
        validate_current_editorial_binding(record, staging, candidate_id=cid, publish=publish)
        title_gate = evaluate_candidate_title_gates(
            candidate_id=cid, title=staging["title"], lane="talk",
            enforce_automatic_style=str(staging.get("title_source") or "").startswith("llm"),
            prior_violations=staging.get("title_policy_violations") or [],
            prior_authority_error=staging.get("title_authority_error"),
            prior_authority_status=staging.get("title_authority_status"),
            story_contract=record["story_contract"], source_fact_review=staging.get("source_fact_review"),
        )
        if title_gate.authority_error or title_gate.violations:
            _fail("EDITORIAL_CURRENT_TITLE_REJECTED")
        tags = record.get("upload_tags")
        if (
            not isinstance(tags, dict)
            or validate_tag_generation(tags)
            or validate_tags(tags.get("final_tags"))
        ):
            _fail("EDITORIAL_CURRENT_TAGS_REJECTED")
        publish = deepcopy(publish)
        staging = deepcopy(staging)
        record = deepcopy(record)
        publish_path = recut.recut_dir / f"{cid}.publish.json"
        record_path = recut.recut_dir / f"{cid}.record.json"
        if publish_path.is_symlink() or record_path.is_symlink():
            _fail("EDITORIAL_SUCCESSOR_UNSAFE")
        publish["subtitle_path"] = str(recut.subtitle_path)
        publish["video_path"] = str(recut.media_path)
        publish["artifact_hashes"] = dict(record["artifact_hashes"])
        publish["artifact_hashes"].pop("publish_draft_sha256", None)
        staging["publish_json_path"] = str(publish_path)
        record["publish_staging"] = staging
        record["prepared_editorial_recovery"] = {
            "method": "BYTE_IDENTICAL_EDITORIAL_INPUT_REVALIDATION",
            "prepared_sha256": handle.prepared_sha256,
            "original_record_sha256": "sha256:" + _sha256(roles["record"]),
            "original_publish_sha256": "sha256:" + _sha256(roles["publish"]),
            "new_title_cover_or_tag_calls": 0,
        }
        validate_current_editorial_binding(record, staging, candidate_id=cid, publish=publish)
        _write_json_atomic(publish_path, publish)
        record["artifact_hashes"]["publish_draft_sha256"] = "sha256:" + _sha256(publish_path)
        validate_staged_editorial_files(record, staging, candidate_id=cid, subtitle_path=recut.subtitle_path)
        _write_json_atomic(record_path, record)
        return StagedRecord(record=record, staging=staging, record_path=record_path)


def load_prepared_producer_recovery(*, args, request) -> PreparedProducerRecovery | PreparedCoverRecovery:
    """Validate original stages before making a writable successor fork."""
    validate_resume_options(args)
    original_spec = _document(args.spec)
    runtime = Path(
        original_spec.get("runtime_root") or Path(original_spec["output_root"]).parents[1]
    )
    handle = load_prepared_delivery(runtime_root=runtime, manifest_path=args.resume_prepared)
    with exclusive_runner_commit(runtime) as lease:
        checked = historical_prepared_review(handle, lease)
        if (
            checked.document.get("lane") != "talk"
            or checked.document.get("candidate_id") != request.cid
        ):
            _fail("CANDIDATE_OR_LANE_DRIFT")
        roles = {entry[0]["role"]: entry[1] for entry in checked.checked}
        if any(path.is_relative_to(request.out_root) for path in (
                args.spec.absolute(), args.resume_prepared, *roles.values())):
            _fail("SUCCESSOR_OVERLAPS_BOUND_INPUT")
        if (
            not {
                "record",
                "source_provenance",
                "subtitle",
                "chat_authority",
                "clip_context",
                "video",
                "uniform_host_ass",
            }
            <= roles.keys()
        ):
            _fail("COMPLETED_ROLES_MISSING")
        record = _document(roles["record"])
        authority = _document(roles["chat_authority"], review=True)
        context = _document(roles["clip_context"])
        provenance = _document(roles["source_provenance"])
    hashes = record.get("artifact_hashes") or {}
    for role, key in (
        ("subtitle", "subtitle_sha256"),
        ("chat_authority", "chat_authority_audit_sha256"),
        ("clip_context", "clip_context_file_sha256"),
        ("video", "burned_video_sha256"),
        ("uniform_host_ass", "ass_sha256"),
    ):
        _bound(roles[role], hashes.get(key))
    if record.get("candidate_id") != request.cid or record.get("speaker_mode") != args.speaker_mode:
        _fail("RECORD_SCOPE_DRIFT")
    if original_spec.get("selection_scorecard") != record.get("selection_scorecard"):
        _fail("SCORECARD_DRIFT")
    validate_clip_context(
        context, candidate_id=request.cid, recording_date=original_spec.get("date")
    )
    if context.get("selection_hook") != original_spec.get("selection_hook") or context.get(
        "context_sha256"
    ) != record.get("clip_context_payload_sha256"):
        _fail("CONTEXT_DRIFT")
    final = provenance.get("final_recut") or {}
    if provenance.get("schema_version") != "lidousha-speaker-recut-provenance.v1":
        _fail("SOURCE_PROVENANCE_INVALID")
    media = _bound(Path(record["media_path"]), hashes.get("video_sha256"))
    subtitle = _bound(Path(record["subtitle_path"]), hashes.get("subtitle_sha256"))
    if _sha256(subtitle) != _sha256(roles["subtitle"]):
        _fail("SUBTITLE_DRIFT")
    padded = _bound(Path(final["source_path"]), final.get("source_sha256"))
    if Path(final["output_path"]) != media or final.get("output_sha256") != _sha256(media):
        _fail("RECUT_DRIFT")
    start, end = final.get("start_ms"), final.get("end_ms")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end
        or record.get("start_ms") != 0
        or record.get("end_ms") != end - start
    ):
        _fail("FINAL_WINDOW_DRIFT")
    rows = provenance.get("source_piece")
    rows = rows if isinstance(rows, list) else [rows]
    pieces = original_spec.get("pieces") or []
    context_pieces = context.get("pieces") or []
    durations = _restore_source_piece_bindings(
        spec=request.spec, original_pieces=pieces, context_pieces=context_pieces, rows=rows,
    )
    padded_proof = provenance.get("padded") or {}
    if (
        padded_proof.get("output_path") != str(padded)
        or padded_proof.get("output_sha256") != _sha256(padded)
        or padded_proof.get("inputs")
        != [{"path": row["output_path"], "sha256": row["output_sha256"]} for row in rows]
    ):
        _fail("PADDED_BINDING_DRIFT")
    request.spec["source_piece_clock"] = source_piece_clock(
        rows, durations, padded_sha256=_sha256(padded)
    )
    for source, digest in {
        (row["source"], row["source_sha256"])
        for row in context.get("structured_chat", [])
        if row.get("source") and row.get("source_sha256")
    }:
        _bound(Path(source), digest)
    boundary = record.get("boundary_audit") or {}
    if boundary.get("final_start_ms") != start or boundary.get("final_end_ms") != end:
        _fail("BOUNDARY_WINDOW_DRIFT")
    review = authority.get("final_review_audit") or {}
    # Geometry is retained evidence. Current postburn content gates still run;
    # an old CLEAN is never promoted into a new release decision here.
    _validate_review_geometry(review, expected_srt_sha256="sha256:" + _sha256(subtitle))
    # Uniform-host ASS is generated inside the burner. Its input revision
    # predates that added hash; the actual ASS is checked independently below.
    render_input = {**record, "artifact_hashes": dict(hashes)}
    if record.get("subtitle_ass_path") is None:
        render_input["artifact_hashes"].pop("ass_sha256", None)
    replay = _replay_bound_burn(
        render_input, authority.get("burn_binding"), branding_intro=request.branding_intro
    )
    if replay is None or _validated_burned_artifact(record) != Path(
        record["burned_preview"]["path"]
    ):
        _fail("BURN_INPUT_BINDING_DRIFT")
    if _sha256(_validated_burned_artifact(record)) != _sha256(roles["video"]) or _sha256(
        _validated_burned_ass_artifact(record)
    ) != _sha256(roles["uniform_host_ass"]):
        _fail("BURN_ROLE_DRIFT")
    validate_final_subtitle_audio_check(
        record, subtitle, _validated_burned_artifact(record), candidate_id=request.cid
    )

    if getattr(args, "resume_cover_failure", None):
        # Cover-only continuation has no text/native-audio account or renderer
        # to resume. Revalidate the current release gates before reusing bytes.
        from src.autoslice.final_review_contract import validate_final_review_release
        from src.autoslice.final_review_candidate_risk import unreviewed_postburn_candidate_cues
        validate_final_review_release(review, expected_srt_sha256="sha256:" + _sha256(subtitle))
        if unreviewed_postburn_candidate_cues(record, srt_sha256="sha256:" + _sha256(subtitle)):
            _fail("COVER_ONLY_CONTENT_REVIEW_REQUIRED")
        if "publish" not in roles:
            _fail("COVER_ONLY_PUBLISH_MISSING")
        from src.autoslice.producer_completed_prepare_revalidation import validate_completed_prepare_artifacts
        validate_completed_prepare_artifacts(handle, {})
        return PreparedCoverRecovery(record, authority, roles, handle.prepared_sha256)

    native_state = inherit_prepared_native_audio(
        cid=request.cid, parent=Path(record["chat_authority_audit_path"]).parent,
        source=padded, runtime_root=runtime,
    )
    root = request.out_root
    if (
        root.resolve() != root
        or (root / "replacement_recuts").resolve() != root / "replacement_recuts"
    ):
        _fail("SUCCESSOR_DIRECTORY_DRIFT")
    receipt_path = root / "prepared-resume-input.json"
    identity = {
        "schema_version": "producer-prepared-resume-input.v1",
        "candidate_id": request.cid,
        "prepared_path": str(args.resume_prepared),
        "prepared_sha256": handle.prepared_sha256,
        "spec_sha256": args.resume_spec_sha256,
        "release_authorized": False,
        "native_audio_account": str(native_state.cache_root),
    }
    recut_dir = root / "replacement_recuts"
    target_media = recut_dir / media.name
    target_subtitle = recut_dir / subtitle.name
    target_authority = root / f"{request.cid}.chat-authority.json"
    context_path = root / f"{request.cid}.clip-context.json"
    if receipt_path.exists():
        # inherit_prepared_native_audio already derives and validates the
        # live account from candidate ID and the actual padded-source hash.
        saved_identity = _document(receipt_path)
        locators = {"prepared_path", "native_audio_account"}
        if ({k: v for k, v in saved_identity.items() if k not in locators}
                != {k: v for k, v in identity.items() if k not in locators}):
            _fail("SUCCESSOR_IDENTITY_DRIFT")
        _bound(target_media, hashes["video_sha256"])
        _bound(context_path, hashes["clip_context_file_sha256"])
        authority = _document(target_authority, review=True)
    else:
        if any(root.iterdir()):
            _fail("SUCCESSOR_NOT_EMPTY")
        recut_dir.mkdir()
        # Copies prevent later repairs or a renderer from mutating a retained
        # source inode. No generated verdict/checkpoint is synthesized.
        shutil.copyfile(media, target_media)
        shutil.copyfile(subtitle, target_subtitle)
        shutil.copyfile(roles["chat_authority"], target_authority)
        shutil.copyfile(roles["clip_context"], context_path)
        copied_provenance = deepcopy(provenance)
        copied_provenance["final_recut"]["output_path"] = str(target_media)
        copied_provenance["prepared_recovery"] = {
            "method": "BYTE_IDENTICAL_PRIVATE_COPY",
            "source_path": str(media),
            "source_sha256": _sha256(media),
            "prepared_sha256": handle.prepared_sha256,
            "original_provenance_sha256": _sha256(roles["source_provenance"]),
        }
        _write_json_atomic(target_media.with_suffix(".provenance.json"), copied_provenance)
        _write_json_atomic(receipt_path, identity)
    if target_subtitle.is_symlink() or not target_subtitle.is_file():
        _fail("SUCCESSOR_SUBTITLE_UNSAFE")
    unchanged = _sha256(target_subtitle) == _sha256(subtitle)
    if not unchanged:
        _validate_exact_final_cpa_self_heal(
            authority, expected_srt_sha256="sha256:" + _sha256(target_subtitle), allow_pending=True
        )
    # Mutable authority may contain a running/completed second question. Keep
    # it, including every earlier call and all cumulative usage fields.
    if authority.get("cpa_adjudication_usage") != _document(
        roles["chat_authority"], review=True
    ).get(
        "cpa_adjudication_usage"
    ):
        # New successful work may extend usage, never erase the original rows.
        old = _document(roles["chat_authority"], review=True).get("cpa_adjudication_usage") or {}
        new = authority.get("cpa_adjudication_usage") or {}
        if any(
            isinstance(value, (int, float)) and new.get(key, -1) < value
            for key, value in old.items()
        ):
            _fail("CUMULATIVE_USAGE_REGRESSION")
    request.spec["clip_context"] = context
    request.spec["clip_context_path"] = str(context_path)
    request.spec["boundary_semantic_review"] = review["boundary_semantic_review"]
    local_record = dict(
        record,
        media_path=str(target_media),
        subtitle_path=str(target_subtitle),
        chat_authority_audit_path=str(target_authority),
        clip_context_path=str(context_path),
    )
    optional_paths = {}
    for role, field, hash_key in (
        (
            "redelivery_baseline",
            "redelivery_baseline_audit_path",
            "redelivery_baseline_audit_sha256",
        ),
        (
            "subtitle_regression",
            "subtitle_regression_audit_path",
            "subtitle_regression_audit_sha256",
        ),
        ("filler_audit", "talk_filler_audit_path", "talk_filler_audit_sha256"),
        (
            "text_finalization",
            "text_finalization_manifest_path",
            "text_finalization_manifest_sha256",
        ),
    ):
        if record.get(field):
            if role not in roles:
                _fail("COMPLETED_OPTIONAL_ROLE_MISSING:" + role)
            original_path = Path(record[field])
            if hash_key in hashes:
                _bound(original_path, hashes[hash_key])
            _bound(original_path, _sha256(roles[role]))
            target = recut_dir / original_path.name
            if target.is_symlink():
                _fail("SUCCESSOR_OPTIONAL_ROLE_UNSAFE:" + role)
            if not target.exists():
                shutil.copyfile(roles[role], target)
            optional_paths[role] = target
            local_record[field] = str(target)
    recut = FinalRecutArtifacts(
        recut_dir,
        target_media,
        target_subtitle,
        optional_paths.get("text_finalization"),
        _document(optional_paths["text_finalization"])
        if "text_finalization" in optional_paths
        else None,
        optional_paths.get("redelivery_baseline"),
        record.get("redelivery_baseline"),
    )
    completed = CompletedFinalizationArtifacts(
        recut,
        SpeakerArtifacts(None, None, None, None),
        AuthorityArtifacts(
            optional_paths.get("subtitle_regression"), record.get("subtitle_regression")
        ),
        local_record,
        review,
        unchanged,
        handle,
    )
    return PreparedProducerRecovery(
        completed,
        authority,
        target_authority,
        padded,
        padded.with_suffix(".provenance.json"),
        rows,
        start,
        end,
        optional_paths.get("filler_audit"),
        native_state,
    )
