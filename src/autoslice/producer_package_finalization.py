"""Final recut, authority binding, burn, staging, and local delivery."""

from __future__ import annotations

from src.autoslice.discovery_channels import batch_from_rows

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256, sha256_ref
from .finding_lifecycle import is_finding_failure

from src.autoslice.review_evidence_store import (
    expand as _expand_review_document,
    persist_document as _persist_review_document,
    review_from_bytes,
)

import hashlib
import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.chat_authority import (
    reconcile_pending_text_overrides,
    reconcile_reviewed_text_override_conflicts,
)
from src.autoslice.acoustic_witness_adjudication import (
    valid_inaudible_drop_authority,
    valid_inaudible_drop_repair,
    valid_inaudible_override_repair,
    valid_inaudible_witness_override,
)
from src.autoslice.final_review_carryover import (
    _snapshot_file_bytes, _write_bytes_atomic, _restore_file_bytes,
    _row_key as _final_review_carryover_row_key,
    adjudicated_proposed_full_cue,
    carryover_path,
    load_final_review_carryover,
    persist_final_review_carryover,
)
from src.autoslice.exact_final_convergence import (
    collect_exact_final_convergence_memos,
    exact_final_convergence_memo_identity,
    rebind_exact_final_convergence_memos,
    valid_exact_final_convergence_memo_binding,
)
from src.autoslice.exact_final_witness_authority import (
    build_self_heal_repair_receipt,
    valid_convergence_mutation_authority,
)
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.cover_reference_authority import (
    load_candidate_cover_reference,
)
from src.autoslice.cover_font_paths import resolve_trusted_cover_font
from src.autoslice.cover_generation import (
    validate_cover_punch_semantic_review,
)
from src.autoslice.cover_punch_semantics import (
    talk_cover_thumbnail_gate_violations,
    cover_punch_shape_contract,
)
from src.autoslice.branding_intro import (
    BrandingIntroError,
    load_branding_intro_policy,
)
from src.autoslice.clip_profile import (
    CLIP_PROFILE_FIELD, ClipProfile, clip_profile_from_spec, default_clip_profile,
    holder_clip_profile_transcription, render_clip_profile, requires_intro,
    spec_clip_profile_transcription,
)
from src.autoslice.cover_route_evidence import (
    validate_cover_route_decision,
    validate_rendered_text_pixel_evidence,
)
from src.autoslice.cover_text_pixel_evidence import (
    verify_pre_overlay_route_background,
    verify_rendered_text_pixel_artifacts,
)
from src.autoslice.cue_split_hygiene import merge_release_grade_cues
from src.autoslice.delivery_fast_path import resolve_operator_text_full_ownership
from src.autoslice.final_review_auditor import (
    audit_final_subtitles, persist_review_audit, audit_correction_mutation_authority,
)
from src.autoslice.final_review_schema_retry import is_schema_repair_prompt_for
from src.autoslice.exact_final_cpa_history import (
    build_run_audit, retained_runs, SOURCE_EXTENSION_SUCCESSORS_KEY,
    with_overlap_timing_successor, with_release_grade_merge_successor, successor_payload,
    with_japanese_display_successor,
    CHAIN_TERMINAL_MISMATCH, RECOVERY_MATERIAL_MISSING, RECOVERY_TYPE_INAPPLICABLE,
    RECOVERY_PREFIX_INVALID, FINAL_IDENTITY_MISMATCH,
)
from src.autoslice.final_subtitle_audio_gate import (
    require_final_subtitle_audio_check,
    copy_subtitle_audio_artifacts,
    load_validated_subtitle_audio_witness,
)
from src.autoslice.final_review_meaning_policy import POLICY_VERSION as MEANING_POLICY_VERSION
from src.autoslice.final_review_contract import (
    EXACT_FINAL_CPA_SELF_HEAL_MAX_REPAIR_PASSES,
    FinalReviewContractError,
    _validate_exact_final_cpa_self_heal,
    correction_carryover_consumed,
    validate_final_review_release,
    is_keep_current_disclosed,
)
from src.autoslice.term_authority import terminology_prompt_context
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.subtitle_validation import validate_srt_text
from src.autoslice.producer_final_review_transport import (
    build_final_review_llm_call,
    build_publication_llm_call,
)
from src.autoslice.clip_context import clip_context_prompt_text
from src.autoslice.llm_client import extract_json_object
from src.autoslice.producer_media import (
    _resolved_optional_path,
    _validated_burned_ass_artifact,
    _validated_burned_artifact,
)
from src.autoslice.producer_delivery_prepare import (
    emit_talk_delivery_summary,
    prepare_and_emit_talk_delivery_from_finalization,
    resolve_talk_delivery_runtime_root,
    talk_delivery_summary,
)
from src.autoslice.producer_delivery_transaction import PreparedDelivery, deployment_authority_binding
from src.autoslice.producer_final_recut_source_binding import (
    resolve_final_recut_source,
)
from src.autoslice.producer_recut_provenance import write_final_recut_provenance
from src.autoslice.producer_delivery_prepare import _recut_provenance
# Exact projection replay is activated only by the resolver-selected grant.
from src.autoslice.redelivery_boundary_projection import materialization_spec_for_selected_projection
from src.autoslice.producer_text_finalization import (
    _render_cues_to_srt,
    verify_chat_authority_final_surfaces,
)
from src.autoslice.producer_text_pipeline import (
    _bound_source_evidence_context,
    validated_postburn_priority_findings,
)
from src.autoslice.redelivery_baseline_ownership import (
    suppress_baseline_owned_self_heal_findings,
)


from src.autoslice.chat_authority_ownership import (
    suppress_chat_authority_owned_self_heal_findings,
)
from src.autoslice.redelivery_full_window_replay import (
    FullWindowReplayError,
    attach_deferred_exact_replay_reverification,
    replay_baseline_for_final_recut,
)
from src.autoslice.reviewed_baseline_replay_c12_projection import (
    C12FinalDeliveryProjection,
    replay_c12_final_delivery_and_supersede_source_truth,
    require_c12_baseline_config,
    require_c12_final_delivery_bytes,
)
from src.autoslice.recovery_title_authority import (
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.shadow_review import _sha256
from src.autoslice.unreadable_cue_drop_stage import (
    seal_unreadable_cue_drops,
    stage_unreadable_cue_drop_pass,
)
from src.autoslice.source_subtitle_truth import (
    apply_source_subtitle_truth,
)
from src.autoslice.source_fact_review import source_fact_review_passes
from src.autoslice.publish_staging_paths import validate_staged_editorial_files
from src.autoslice.source_fact_rescore_provenance import (
    SourceFactRescoreProvenanceError,
    bind_finalization_provenance,
    bind_publish_staging_provenance,
    bind_story_contract_provenance,
)
from src.autoslice import selection_rescore, speaker_guess
from src.autoslice.surface_canon import (
    canonicalize_japanese_native_script_surfaces,
    normalize_japanese_native_script_surfaces,
    normalize_expected_value_surfaces,
)
from src.autoslice.subtitle_fidelity import (
    apply_title_mark_balance_guard,
    resolve_deferred_foreign_introductions,
)
from src.autoslice.subtitle_regression import verify_subtitle_regression_surfaces
from src.autoslice.talk_filler import bind_final_filler_audit_to_burn
from src.autoslice.producer_nested_caption import consume_nested_caption_policy
from src.autoslice.nested_media_caption_dedup import (
    CaptionDedupError,
    POLICY as CAPTION_DEDUP_POLICY,
    SCHEMA as CAPTION_DEDUP_SCHEMA,
    _file_bytes as _caption_evidence_bytes,
)
from src.autoslice.producer_nested_external_audio import (
    EXTERNAL_AUDIO_SCENE_SOURCE_ROLE,
)
from src.autoslice.producer_nested_acoustic import (
    _current_file_hash,
    _hashes_match,
    current_caption_audio_roles,
    retain_current_acoustic_observations,
    split_current_acoustic_observations_for_reuse,
)
from src.autoslice.story_contract import (
    audit_story_artifact,
    build_story_contract,
    canonicalize_relation_summary,
    canonicalize_story_scorecard,
    cover_story_contract_binding_matches,
)
from src.autoslice.clip_context import validate_clip_context
from src.autoslice.producer_stage_log import producer_stage
from src.autoslice.canonical_json_hash import canonical_sha256

ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)


def _json_bytes(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _read_review_bytes(raw: bytes, *, directory: Path, label: str) -> Mapping[str, object]:
    """Decode one persisted authority from the exact bytes just read."""

    try:
        value = review_from_bytes(raw, directory=directory)
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}_JSON_INVALID") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}_JSON_OBJECT_REQUIRED")
    return value


def _read_review_document(path: Path, *, label: str) -> Mapping[str, object]:
    return _read_review_bytes(path.read_bytes(), directory=path.parent, label=label)

@dataclass(frozen=True)
class ProducerFinalizationOptions:
    spec: Path
    substrate: str
    correct: str
    speaker_mode: str
    speaker_overrides: Path | None
    speaker_source_session_anchors: Path | None
    speaker_mixed_overlap_evidence: Path | None
    speaker_python: Path
    reuse_cover: bool
    # The runner will opt in after its batch-CAS transaction is installed.
    # Standalone/manual producer invocations retain their established direct
    # delivery behavior until then.
    prepare_only: bool = False

@dataclass(frozen=True)
class ProducerFinalizationAdapters:
    accurate_recut_command: Callable[..., list[str]]
    run_command: Callable[..., None]
    write_source_range_srt: Callable[..., None]
    apply_text_override_document: Callable[..., dict]
    run_speaker_finalization: Callable[..., dict]
    burn_preview_subtitles: Callable[..., dict]
    stage_publish_draft: Callable[..., dict]
    generate_upload_tags: Callable[..., dict]
    delivery_root: Callable[[], Path]
    run_exact_final_review: Callable[..., dict] | None = None
    capture_subtitle_audio_check: Callable[..., dict] | None = None
    observe_nested_captions: Callable[..., dict] | None = None
    observe_nested_acoustics: Callable[..., dict] | None = None
    locate_nested_source: Callable[..., dict] | None = None
    prepare_nested_acoustic_spec: Callable[..., tuple[dict, dict]] | None = None

@dataclass(frozen=True)
class CurrentTextReviewArtifacts:
    """Current text files only; this does not claim a materialized recut."""

    subtitle_path: Path
    redelivery_baseline_audit_path: Path | None = None
    redelivery_baseline_audit: dict | None = None


@dataclass(frozen=True)
class FinalRecutArtifacts:
    recut_dir: Path
    media_path: Path
    subtitle_path: Path
    text_manifest_path: Path | None
    text_manifest: dict | None
    redelivery_baseline_audit_path: Path | None = None
    redelivery_baseline_audit: dict | None = None
    nested_scene_spec: dict | None = None

@dataclass(frozen=True)
class SpeakerArtifacts:
    manifest: dict | None
    review_srt: Path | None
    ass: Path | None
    manifest_path: Path | None

@dataclass(frozen=True)
class AuthorityArtifacts:
    subtitle_regression_audit_path: Path | None
    subtitle_regression_audit: dict | None


@dataclass(frozen=True)
class CompletedFinalizationArtifacts:
    """Validated private inputs, never a publication or materialization grant."""

    recut: FinalRecutArtifacts
    speaker: SpeakerArtifacts
    authority: AuthorityArtifacts
    record: dict
    exact_review: dict
    reuse_render: bool = True
    prepared_handle: PreparedDelivery | None = None


_DELIVERY_LANES = frozenset({"talk", "song", "shadow"})


def _branding_intro_required_for_talk() -> bool:
    """Return whether the current committed policy mandates a talk intro.

    This reads the policy switch only.  Runtime media resolution remains the
    responsibility of ``require_branding_intro`` at the normal producer
    request boundary; this finalization check closes callers that bypass that
    boundary with a null context.
    """

    manifest_path = CHANNEL_PROFILE.asset_file("branding_intro_manifest")
    try:
        policy = load_branding_intro_policy(manifest_path)
    except BrandingIntroError as exc:
        raise SystemExit(f"BRANDING_INTRO_POLICY_INVALID: {exc}") from exc
    if policy is None:
        return False
    lane_policy = policy.get("policy")
    if not isinstance(lane_policy, Mapping):
        return False
    applies_to = lane_policy.get("applies_to")
    return lane_policy.get("mandatory") is True and isinstance(applies_to, list) and "talk" in applies_to


def _require_delivery_branding_intro(
    *,
    delivery_lane: str,
    branding_intro: dict[str, object] | None,
    clip_profile: ClipProfile | None = None,
) -> None:
    """Reject a missing intro at the producer's final delivery boundary.

    The declared form decides the intro. Undeclared talk/song callers keep
    the historical lane default; shadow retains its test policy exemption.
    """

    lane = str(delivery_lane or "").strip().lower()
    if lane not in _DELIVERY_LANES:
        raise SystemExit(f"DELIVERY_LANE_INVALID: {delivery_lane!r}")
    form = clip_profile or default_clip_profile("song" if lane == "song" else "talk")
    if not requires_intro(form) and branding_intro is not None:
        raise SystemExit("BRANDING_INTRO_FORBIDDEN_FOR_PROFILE")
    if requires_intro(form) and lane == "talk" and _branding_intro_required_for_talk() and branding_intro is None:
        raise SystemExit("BRANDING_INTRO_REQUIRED_FOR_TALK_DELIVERY")


@dataclass(frozen=True)
class StagedRecord:
    record: dict
    staging: dict
    record_path: Path


def _audit_story_bound_cover(
    staging: Mapping[str, object], story_contract: Mapping[str, object]
) -> tuple[list[str], list[dict[str, object]]]:
    """Recheck model-selected cover words before any delivery copy occurs."""

    generation = staging.get("cover_generation")
    if not isinstance(generation, Mapping):
        return ["COVER_STORY_CONTRACT_BINDING_MISSING_OR_STALE"], []
    if generation.get("method") == "skill.candidates":
        from .title_cover_candidates import validate_choice_bundle
        choice_problems = validate_choice_bundle(generation)
        if choice_problems:
            return ["COVER_CHOICE_EVIDENCE_INVALID", *choice_problems], []
    cover_text = str(staging.get("cover_text") or generation.get("cover_text") or "")
    rendered_lines = generation.get("rendered_lines")
    rendered_text = (
        "".join(str(value) for value in rendered_lines)
        if isinstance(rendered_lines, list)
        else ""
    )
    audits = [
        audit_story_artifact(
            surface,
            story_contract=story_contract,
            artifact_kind=kind,
        )
        for kind, surface in (
            ("cover_text", cover_text),
            ("cover_rendered_text", rendered_text),
        )
        if surface
    ]
    reason_codes = {
        str(violation.get("reason_code") or "STORY_CONTRACT_COVER_FAILED")
        for audit in audits
        for violation in audit.get("violations", [])
        if isinstance(violation, Mapping)
    }
    binding = generation.get("story_contract")
    if (
        not isinstance(binding, Mapping)
        or "selection_hook" not in binding
        or not cover_story_contract_binding_matches(story_contract, binding)
    ):
        reason_codes.add("COVER_STORY_CONTRACT_BINDING_MISSING_OR_STALE")
    if not cover_text:
        reason_codes.add("COVER_STORY_TEXT_MISSING")
    if not rendered_text:
        reason_codes.add("COVER_RENDERED_TEXT_EVIDENCE_MISSING")
    # The delivery choke point only accepts the complete v2 evidence emitted
    # by current staging.  Cover repair remains read-compatible with immutable
    # v1 packages, but a fresh producer run cannot downgrade its audit schema.
    if not validate_cover_route_decision(generation, allow_legacy_v1=False):
        reason_codes.add("COVER_ROUTE_DECISION_MISSING_OR_INVALID")
    if not validate_rendered_text_pixel_evidence(generation):
        reason_codes.add("COVER_RENDERED_TEXT_PIXELS_MISSING_OR_INVALID")
    if generation.get("cover_text_mode") == "punch":
        art_direction = generation.get("art_direction")
        punch_review = (
            art_direction.get("cover_punch_semantic_review")
            if isinstance(art_direction, Mapping)
            else None
        )
        if not validate_cover_punch_semantic_review(
            punch_review,
            rendered_lines=rendered_lines,
            cover_text=cover_text,
            story_hook=str(story_contract.get("selection_hook") or ""),
            shape_contract=cover_punch_shape_contract(generation),
        ):
            reason_codes.add("COVER_PUNCH_SEMANTIC_REVIEW_INVALID")
    reason_codes.update(
        talk_cover_thumbnail_gate_violations(
            generation,
            cover_text=cover_text,
        )
    )
    pixel_evidence = generation.get("rendered_text_pixels")
    if isinstance(pixel_evidence, Mapping):
        try:
            trusted_font = resolve_trusted_cover_font(
                file_name=str(
                    pixel_evidence.get("font_file_name") or ""
                ),
                expected_sha256=str(
                    pixel_evidence.get("font_file_sha256") or ""
                ),
                channel_profile=CHANNEL_PROFILE,
                root=ROOT,
            )
            artifacts_valid = verify_rendered_text_pixel_artifacts(
                pixel_evidence,
                final_cover_path=Path(
                    str(generation.get("final_cover") or "")
                ),
                pre_overlay_path=Path(
                    str(generation.get("pre_overlay_path") or "")
                ),
                mask_path=Path(
                    str(pixel_evidence.get("mask_path") or "")
                ),
                font_path=trusted_font,
                expected_pre_overlay_sha256=generation.get(
                    "pre_overlay_sha256"
                ),
            ) and verify_pre_overlay_route_background(
                route_background_path=Path(
                    str(generation.get("ai_background") or "")
                ),
                pre_overlay_path=Path(
                    str(generation.get("pre_overlay_path") or "")
                ),
                expected_route_background_sha256=generation.get(
                    "ai_background_sha256"
                ),
                text_backing=generation.get("text_backing"),
                scrim=generation.get("scrim"),
            )
        except (OSError, RuntimeError, ValueError):
            artifacts_valid = False
    else:
        artifacts_valid = False
    if not artifacts_valid:
        reason_codes.add("COVER_RENDERED_TEXT_PIXEL_ARTIFACT_MISMATCH")
    return sorted(reason_codes), audits


def _trim_spec_to_final_timeline(
    spec: Mapping[str, object], *, final_start: int, final_end: int
) -> tuple[dict[str, object], list[int]]:
    """Project a concatenated padded-piece spec onto the final recut timeline."""

    pieces_raw = spec.get("pieces")
    if not isinstance(pieces_raw, list) or final_start < 0 or final_end <= final_start:
        raise RuntimeError("REDELIVERY_SOURCE_TRUTH_TIMELINE_INVALID")
    cursor = 0
    retained: list[dict[str, object]] = []
    durations: list[int] = []
    for raw_piece in pieces_raw:
        if not isinstance(raw_piece, Mapping):
            raise RuntimeError("REDELIVERY_SOURCE_TRUTH_PIECE_INVALID")
        source_start = raw_piece.get("start_ms")
        source_end = raw_piece.get("end_ms")
        if (
            isinstance(source_start, bool)
            or not isinstance(source_start, int)
            or isinstance(source_end, bool)
            or not isinstance(source_end, int)
            or source_end <= source_start
        ):
            raise RuntimeError("REDELIVERY_SOURCE_TRUTH_PIECE_RANGE_INVALID")
        duration = source_end - source_start
        overlap_start = max(final_start, cursor)
        overlap_end = min(final_end, cursor + duration)
        if overlap_start < overlap_end:
            clipped = dict(raw_piece)
            clipped["start_ms"] = source_start + overlap_start - cursor
            clipped["end_ms"] = source_start + overlap_end - cursor
            retained.append(clipped)
            durations.append(overlap_end - overlap_start)
        cursor += duration
    if not retained or final_end > cursor:
        raise RuntimeError("REDELIVERY_SOURCE_TRUTH_FINAL_RANGE_UNMAPPED")
    return {"pieces": retained}, durations


def _rebase_source_truth_audit_to_padded(
    audit: dict[str, object], *, final_start: int
) -> dict[str, object]:
    """Return a verifier-facing copy whose local windows use padded time."""

    rebased = deepcopy(audit)
    for key in ("applied", "satisfied", "failures"):
        rows = rebased.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for window in row.get("local_windows") or []:
                if isinstance(window, dict):
                    window["start_ms"] = int(window["start_ms"]) + final_start
                    window["end_ms"] = int(window["end_ms"]) + final_start
            projection = row.get("resolved_target_projection")
            if isinstance(projection, dict):
                for cue in projection.get("cues") or []:
                    if isinstance(cue, dict):
                        cue["start_ms"] = int(cue["start_ms"]) + final_start
                        cue["end_ms"] = int(cue["end_ms"]) + final_start
            timing_pin = row.get("timing_pin")
            if isinstance(timing_pin, dict):
                for field in ("before_start_ms", "after_start_ms"):
                    if field in timing_pin:
                        timing_pin[field] = int(timing_pin[field]) + final_start
    rebased["timeline_basis"] = "padded_candidate"
    rebased["final_recut_offset_ms"] = final_start
    rebased["reapplied_after_redelivery_baseline"] = True
    return rebased


def _write_redelivery_baseline_audit(path: Path, audit: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _has_source_extension_boundary(*, cid: str, out_root: Path) -> bool:
    path = out_root / f"{cid}.boundary_audit.json"
    try:
        boundary = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    extension = boundary.get("current_delivery_closure_binding") if isinstance(boundary, Mapping) else None
    return isinstance(extension, Mapping) and extension.get("schema_version") == "current-delivery-source-extension.v1"


def _materialize_final_recut(**kwargs) -> FinalRecutArtifacts:
    """Roll back the SRT/authority unit on any failed materialization or save."""
    out_root, cid = kwargs["out_root"], kwargs["cid"]
    authority_path = kwargs.pop("chat_authority_path", out_root / f"{cid}.chat-authority.json")
    recut_dir = out_root / "replacement_recuts"
    subtitle = recut_dir / f"{cid}.recut.srt"
    authority = kwargs.get("chat_authority_audit")
    # Keep active bytes untouched until their complete typed history is durable.
    # Caption evidence may retain this small materialization snapshot as its
    # original display artifact; later exact-review passes use the active SRT.
    work_subtitle = (recut_dir / f".{cid}.recut.materializing.srt"
                     if isinstance((authority or {}).get("exact_final_cpa_self_heal"), Mapping) else subtitle)
    paths = [subtitle, authority_path, recut_dir / f"{cid}.recut.timing_qa.json",
             recut_dir / f"{cid}.redelivery-baseline.json", work_subtitle]
    original = _snapshot_file_bytes(paths)
    staged = deepcopy(authority) if authority is not None else None
    try:
        result = _materialize_final_recut_body(**{**kwargs, "chat_authority_audit": staged,
                                                "_subtitle_path": work_subtitle})
        # Install authority first, then the active SRT, using the same atomic
        # writes/rollback as the exact-review transaction. A partial unit can
        # never be accepted by the ordinary hash-bound cold reader.
        text = work_subtitle.read_bytes()
        if staged is not None:
            _persist_review_document(authority_path, staged)
        _write_bytes_atomic(subtitle, text)
    except BaseException:
        _restore_file_bytes(original)
        raise
    if authority is not None:
        authority.clear()
        authority.update(staged)
    return replace(result, subtitle_path=subtitle)


def _materialize_final_recut_body(
    *,
    spec: dict,
    cid: str,
    out_root: Path,
    padded: Path,
    padded_provenance_path: Path,
    piece_provenance_rows: list[dict],
    final_start: int,
    final_end: int,
    sanitized: list[SourceCue],
    timing_qa: dict,
    text_override_path: Path | None,
    adapters: ProducerFinalizationAdapters,
    spec_parent: Path | None = None,
    chat_authority_audit: dict | None = None,
    reviewed_baseline_replay_c12_projection: C12FinalDeliveryProjection | None = None,
    allow_existing_record: bool = False,
    _subtitle_path: Path | None = None,
) -> FinalRecutArtifacts:
    baseline_config = spec.get("subtitle_redelivery_baseline")
    if reviewed_baseline_replay_c12_projection is not None:
        baseline_config = require_c12_baseline_config(baseline_config)
    if baseline_config is not None and text_override_path is not None:
        raise SystemExit("REDELIVERY_BASELINE_CONFLICTS_WITH_TEXT_OVERRIDE: use source truth windows for incident corrections")
    v2_source_binding, absolute_source_start_ms, absolute_source_end_ms = resolve_final_recut_source(
        spec=spec, piece_provenance_rows=piece_provenance_rows,
        final_start=final_start, final_end=final_end
    )
    recut_dir = out_root / "replacement_recuts"
    recut_dir.mkdir(exist_ok=True)
    existing_record_path = recut_dir / f"{cid}.record.json"
    try:
        existing_record = bool(os.lstat(existing_record_path))
    except OSError as exc:
        existing_record = not isinstance(exc, FileNotFoundError)
    if baseline_config is not None and existing_record and not allow_existing_record:
        raise SystemExit("REDELIVERY_BASELINE_EXISTING_RECORD_REQUIRES_PRIVATE_REPLAY")
    media_path = recut_dir / f"{cid}.recut.mp4"
    adapters.run_command(adapters.accurate_recut_command(source_video=padded, output_media=media_path, start_ms=final_start, duration_ms=final_end - final_start))
    write_final_recut_provenance(
        media_path=media_path,
        padded=padded,
        padded_provenance_path=padded_provenance_path,
        piece_provenance_rows=piece_provenance_rows,
        final_start=final_start,
        final_end=final_end,
        absolute_source_start_ms=absolute_source_start_ms,
        absolute_source_end_ms=absolute_source_end_ms,
    )
    subtitle_path = _subtitle_path or media_path.with_suffix(".srt")
    text_manifest_path: Path | None = None
    text_manifest: dict | None = None
    if text_override_path is not None:
        automatic_text_path = media_path.with_suffix(".automatic-text.srt")
        adapters.write_source_range_srt(sanitized, final_start, final_end, automatic_text_path)
        text_manifest_path = media_path.with_suffix(".text-finalization.json")
        override_document = json.loads(text_override_path.read_text(encoding="utf-8"))
        text_manifest_args = (
            automatic_text_path,
            text_override_path,
            subtitle_path,
            text_manifest_path,
        )
        text_manifest = adapters.apply_text_override_document(
            *text_manifest_args,
            expected_candidate_id=cid,
            **(
                {"timeline_offset_ms": final_start} if override_document.get("schema_version") in {3, 4} else {}
            ),
        )
    else:
        adapters.write_source_range_srt(sanitized, final_start, final_end, subtitle_path)
    redelivery_baseline_audit_path: Path | None = None
    redelivery_baseline_audit: dict | None = None
    if baseline_config is not None:
        truth_audit = (
            (chat_authority_audit or {}).get("source_subtitle_truth_audit") or {}
        )
        truth_rows = [
            row
            for key in ("applied", "satisfied", "failures")
            for row in (truth_audit.get(key) or [])
            if isinstance(row, Mapping)
        ]
        truth_reapply = bool(truth_rows)
        replay_spec_parent = spec_parent or Path.cwd()
        raw_baseline_path = (
            baseline_config.get("path")
            if isinstance(baseline_config, Mapping)
            else None
        )
        if isinstance(raw_baseline_path, str) and Path(raw_baseline_path).is_absolute():
            replay_spec_parent = Path(raw_baseline_path).parent
        post_baseline_truth_audit: Mapping[str, object] | None = None
        deferred_exact_replay_audit: dict[str, object] | None = None
        if reviewed_baseline_replay_c12_projection is not None:
            (
                output_text,
                redelivery_baseline_audit,
                post_baseline_truth_audit,
                deferred_exact_replay_audit,
            ) = replay_c12_final_delivery_and_supersede_source_truth(
                projection=reviewed_baseline_replay_c12_projection,
                config=baseline_config,
                cid=cid,
                final_start_ms=final_start,
                final_end_ms=final_end,
                binding=v2_source_binding,
                subtitle_path=subtitle_path,
                write_source_range_srt=adapters.write_source_range_srt,
                pre_truth_audit=truth_audit,
                source_truth_reapply=truth_reapply,
                chat_authority_audit=chat_authority_audit,
            )
        else:
            try:
                output_text, redelivery_baseline_audit = replay_baseline_for_final_recut(
                    truth_audit=truth_audit,
                    recut_dir=recut_dir,
                    cid=cid,
                    sanitized=sanitized,
                    binding=v2_source_binding,
                    config=baseline_config,
                    spec_parent=replay_spec_parent,
                    final_start_ms=final_start,
                    final_end_ms=final_end,
                    subtitle_path=subtitle_path,
                    write_source_range_srt=adapters.write_source_range_srt,
                    recording_date=str(spec.get("date") or ""),
                )
            except FullWindowReplayError as exc:
                raise SystemExit(str(exc)) from exc
        # Replay may resurrect spellings removed by the ordinary text pipeline.
        # Normalize before reapplying source truth so an explicit source-bound
        # exception remains authoritative over the glossary default.
        output_text, expected_value_audit = normalize_expected_value_surfaces(output_text)
        redelivery_baseline_audit["post_redelivery_expected_value_surface_audit"] = expected_value_audit
        if chat_authority_audit is not None:
            chat_authority_audit["post_redelivery_expected_value_surface_audit"] = expected_value_audit
        redelivery_baseline_audit_path = recut_dir / f"{cid}.redelivery-baseline.json"
        final_truth_failed = False
        final_title_failed = False
        if (
            reviewed_baseline_replay_c12_projection is None
            and redelivery_baseline_audit["status"] != "FAILED"
            and truth_reapply
        ):
            ledger_raw = truth_audit.get("ledger_path")
            if not isinstance(ledger_raw, str) or not ledger_raw:
                raise SystemExit("REDELIVERY_SOURCE_TRUTH_LEDGER_PATH_MISSING")
            trimmed_spec, trimmed_durations = _trim_spec_to_final_timeline(
                spec,
                final_start=final_start,
                final_end=final_end,
            )
            output_text, post_baseline_truth_audit = apply_source_subtitle_truth(
                output_text,
                spec=trimmed_spec,
                durations=trimmed_durations,
                ledger_path=Path(ledger_raw),
                satisfaction_reviews=truth_audit.get("meaning_reviews") or (),
                local_term_reviews=truth_audit.get("local_term_reviews") or (),
            )
            post_baseline_truth_audit["timeline_basis"] = "final_delivery"
            post_baseline_truth_audit["reapplied_after_redelivery_baseline"] = True
            redelivery_baseline_audit["source_truth_reapplication"] = (
                post_baseline_truth_audit
            )
            final_truth_failed = post_baseline_truth_audit["status"] == "FAILED"
            if chat_authority_audit is not None:
                chat_authority_audit[
                    "source_subtitle_truth_pre_redelivery_audit"
                ] = deepcopy(truth_audit)
                chat_authority_audit[
                    "source_subtitle_truth_post_redelivery_audit"
                ] = deepcopy(post_baseline_truth_audit)
                chat_authority_audit["source_subtitle_truth_audit"] = (
                    _rebase_source_truth_audit_to_padded(
                        post_baseline_truth_audit,
                        final_start=final_start,
                    )
                )
            output_text, final_title_audit = apply_title_mark_balance_guard(
                output_text
            )
            final_title_failed = (
                final_title_audit["status"] == "UNRESOLVED_COMPLEX_IMBALANCE"
            )
            if chat_authority_audit is not None:
                chat_authority_audit["final_title_mark_balance_audit"] = (
                    final_title_audit
                )
                chat_authority_audit["final_output_srt_sha256"] = hashlib.sha256(
                    output_text.encode("utf-8")
                ).hexdigest()
            redelivery_baseline_audit["post_source_truth_output_sha256"] = (
                hashlib.sha256(output_text.encode("utf-8")).hexdigest()
            )
        deferred_exact_replay_audit = attach_deferred_exact_replay_reverification(
            baseline_audit=redelivery_baseline_audit,
            pre_truth_audit=truth_audit,
            post_truth_audit=post_baseline_truth_audit,
            deferred_audit=deferred_exact_replay_audit,
        )
        _write_redelivery_baseline_audit(
            redelivery_baseline_audit_path, redelivery_baseline_audit
        )
        if chat_authority_audit is not None:
            chat_authority_audit["redelivery_subtitle_baseline_audit"] = (
                redelivery_baseline_audit
            )
        if redelivery_baseline_audit["status"] == "FAILED":
            raise SystemExit(
                f"REDELIVERY_SUBTITLE_BASELINE_FAILED: {redelivery_baseline_audit_path}"
            )
        if final_truth_failed:
            raise SystemExit(
                f"SOURCE_SUBTITLE_TRUTH_REQUIRED_AFTER_REDELIVERY: "
                f"{redelivery_baseline_audit_path}"
            )
        if deferred_exact_replay_audit["status"] == "FAILED":
            raise SystemExit(
                "DEFERRED_EXACT_REPLAY_TRUTH_NOT_REVERIFIED: "
                f"{redelivery_baseline_audit_path}"
            )
        if final_title_failed:
            raise SystemExit(
                f"TITLE_MARK_BALANCE_REQUIRED_AFTER_REDELIVERY: "
                f"{redelivery_baseline_audit_path}"
            )
        subtitle_path.write_text(output_text, encoding="utf-8")
    # This is the last text-mutating choke point.  The earlier text pipeline
    # already removes release-invalid slivers, but a hash-bound reviewed
    # baseline is replayed later and can legitimately restore the old cue grid.
    # Run the same validator-driven merge *after* baseline/source-truth replay
    # so no late authority branch can resurrect <300ms or non-exempt one-CJK
    # cues (1863: 「哦」/「行」).
    final_text = subtitle_path.read_bytes().decode("utf-8", errors="strict")
    # Timing happened upstream, before this merger. Never record their compound
    # difference as one timing edge; pending receipts have no final-hash field.
    timing_before = timing_qa.get("current_delivery_input_srt_text")
    previous_self_heal = (chat_authority_audit or {}).get("exact_final_cpa_self_heal")
    if isinstance(previous_self_heal, Mapping):
        try:
            _validate_exact_final_cpa_self_heal(
                {"exact_final_cpa_self_heal": previous_self_heal},
                expected_srt_sha256=sha256_ref(hashlib.sha256(final_text.encode()).hexdigest()),
                allow_pending=True)
        except FinalReviewContractError as exc:
            # A source-tail addition already happened in the boundary writer.
            # Register that exact edge before a later merger changes its grid.
            if (exc.reason_code in {CHAIN_TERMINAL_MISMATCH, FINAL_IDENTITY_MISMATCH}
                    and _has_source_extension_boundary(cid=cid, out_root=out_root)):
                previous_self_heal = _resume_source_extended_review(
                    cid=cid, out_root=out_root, final_text=final_text,
                    chat_authority_audit=chat_authority_audit, timing_qa=timing_qa)
                chat_authority_audit["exact_final_cpa_self_heal"] = previous_self_heal
    if (isinstance(timing_before, str) and timing_before != final_text
            and isinstance(previous_self_heal, Mapping)):
        try:
            _validate_exact_final_cpa_self_heal(
                {"exact_final_cpa_self_heal": previous_self_heal},
                expected_srt_sha256=sha256_ref(hashlib.sha256(timing_before.encode()).hexdigest()),
                allow_pending=True,
            )
        except FinalReviewContractError:
            # An unrelated upstream/baseline input is not timing authority.
            # The exact gate still rejects its unregistered terminal drift.
            pass
        else:
            chat_authority_audit["exact_final_cpa_self_heal"] = with_overlap_timing_successor(
                previous_self_heal, timing_before, final_text,
            )
    merge_before = final_text
    final_text, final_release_grade_merge_rows = merge_release_grade_cues(merge_before)
    previous_self_heal = (chat_authority_audit or {}).get("exact_final_cpa_self_heal")
    if final_text != merge_before and isinstance(previous_self_heal, Mapping):
        chat_authority_audit["exact_final_cpa_self_heal"] = with_release_grade_merge_successor(
            previous_self_heal, before=merge_before, after=final_text,
            merge_report=final_release_grade_merge_rows,
        )
    # A hash-bound reviewed baseline is deliberately allowed to restore old
    # wording late. Re-assert the Japanese native-script presentation policy
    # after that replay so a legacy boku/ore/atashi/wakuwaku surface cannot
    # reach burn or merely turn into a final-owner blocker.
    japanese_before = final_text
    final_text, final_japanese_native_script_audit = (
        normalize_japanese_native_script_surfaces(final_text)
    )
    previous_self_heal = (chat_authority_audit or {}).get("exact_final_cpa_self_heal")
    if final_text != japanese_before and isinstance(previous_self_heal, Mapping):
        chat_authority_audit["exact_final_cpa_self_heal"] = with_japanese_display_successor(
            previous_self_heal, japanese_before, final_text,
        )
    if reviewed_baseline_replay_c12_projection is not None:
        require_c12_final_delivery_bytes(
            projection=reviewed_baseline_replay_c12_projection,
            config=baseline_config,
            cid=cid,
            output_text=final_text,
        )
    if (
        final_release_grade_merge_rows
        or final_japanese_native_script_audit["status"] == "APPLIED"
    ):
        subtitle_path.write_text(final_text, encoding="utf-8")
        if chat_authority_audit is not None:
            chat_authority_audit["final_release_grade_cue_merges"] = (
                final_release_grade_merge_rows
            )
            chat_authority_audit[
                "post_redelivery_japanese_native_script_audit"
            ] = final_japanese_native_script_audit
        if redelivery_baseline_audit is not None:
            redelivery_baseline_audit["final_release_grade_cue_merges"] = (
                final_release_grade_merge_rows
            )
            redelivery_baseline_audit[
                "post_redelivery_japanese_native_script_audit"
            ] = final_japanese_native_script_audit
            redelivery_baseline_audit[
                "post_release_grade_output_sha256"
            ] = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
            assert redelivery_baseline_audit_path is not None
            _write_redelivery_baseline_audit(
                redelivery_baseline_audit_path, redelivery_baseline_audit
            )
    elif chat_authority_audit is not None:
        chat_authority_audit[
            "post_redelivery_japanese_native_script_audit"
        ] = final_japanese_native_script_audit
    # Caption display ownership is resolved on the post-baseline final text.
    # Subsequent exact-final/boundary review and ASS generation see these bytes,
    # not the original all-speaker transcript archived by this consumer.
    caption_spec = spec
    if adapters.observe_nested_captions is not None:
        caption_spec = adapters.observe_nested_captions(
            spec=spec, candidate_id=cid, source_media=padded,
            source_start_ms=final_start, source_end_ms=final_end,
            subtitle_path=subtitle_path, evidence_root=spec_parent or Path.cwd(),
            chat_authority_audit=chat_authority_audit,
        )
    caption_spec = _run_nested_source_locator(
        spec=spec,
        caption_spec=caption_spec,
        adapters=adapters,
        cid=cid,
        padded=padded,
        final_start=final_start,
        final_end=final_end,
        subtitle_path=subtitle_path,
        evidence_root=spec_parent or Path.cwd(),
        chat_authority_audit=chat_authority_audit,
    )
    if caption_spec.get("nested_media_external_audio_scene") is True:
        # A soundboard frame is scene evidence for acoustic preparation, not
        # watched-video caption authority. Keep the current transcript bytes
        # and do not route this evidence through the display-deletion policy.
        final_text = subtitle_path.read_bytes().decode("utf-8", errors="strict")
        if chat_authority_audit is not None:
            chat_authority_audit["nested_media_caption_dedup"] = {
                "status": "SKIPPED_EXTERNAL_AUDIO_SCENE",
                "display_mutation_authorized": False,
                "source_role_authority": False,
                "subtitle_mutations": 0,
            }
    else:
        final_text = consume_nested_caption_policy(
            spec=caption_spec, candidate_id=cid, source_media=padded,
            source_start_ms=final_start, source_end_ms=final_end,
            subtitle_path=subtitle_path,
            evidence_root=spec_parent or Path.cwd(),
            chat_authority_audit=chat_authority_audit,
        )
    if chat_authority_audit is not None:
        chat_authority_audit["final_output_srt_sha256"] = hashlib.sha256(
            final_text.encode("utf-8")
        ).hexdigest()
    (recut_dir / f"{cid}.recut.timing_qa.json").write_text(
        json.dumps(timing_qa, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return FinalRecutArtifacts(
        recut_dir=recut_dir,
        media_path=media_path,
        subtitle_path=subtitle_path,
        text_manifest_path=text_manifest_path,
        text_manifest=text_manifest,
        redelivery_baseline_audit_path=redelivery_baseline_audit_path,
        redelivery_baseline_audit=redelivery_baseline_audit,
        nested_scene_spec=dict(caption_spec),
    )


def _caption_scene_evidence_binding(
    *,
    scene_spec: Mapping[str, object],
    candidate_id: str,
    source_media: Path,
    subtitle_path: Path,
    duration_ms: int,
) -> tuple[str, dict[str, object] | None]:
    """Classify a caption receipt before choosing reuse or fresh survey.

    This only establishes the binding identity.  The ordinary scene preparer
    remains the owner of schema, controls, descriptor, and frame validation.
    """
    config = scene_spec.get("nested_media_caption_dedup")
    if config is None:
        return "ABSENT", {"status": "ABSENT"}
    # Older callers used an unbound marker. It cannot be reused, but it is not
    # a damaged receipt; let the ordinary current-pixel path replace it. A
    # partially populated binding, however, is malformed and must fail closed.
    if isinstance(config, Mapping) and "evidence_path" not in config:
        binding_keys = {"policy", "schema_version", "source_role", "evidence_sha256"}
        if not binding_keys.intersection(config):
            return "STALE", {
                "status": "STALE", "reason_code": "CAPTION_EVIDENCE_NOT_BOUND"
            }
        return "INVALID", {
            "status": "INVALID", "reason_code": "CAPTION_EVIDENCE_PATH_INVALID"
        }
    if not isinstance(config, Mapping) or config.get("policy") != CAPTION_DEDUP_POLICY:
        return "INVALID", {"status": "INVALID", "reason_code": "CAPTION_EVIDENCE_POLICY_INVALID"}
    evidence_value = config.get("evidence_path")
    if not isinstance(evidence_value, str) or not evidence_value:
        return "INVALID", {"status": "INVALID", "reason_code": "CAPTION_EVIDENCE_PATH_INVALID"}
    evidence_path = Path(evidence_value)
    if not evidence_path.is_absolute():
        evidence_path = subtitle_path.parent / evidence_path
    try:
        evidence_bytes = _caption_evidence_bytes(
            evidence_path, config.get("evidence_sha256"), "CAPTION_EVIDENCE"
        )
        evidence_sha = hashlib.sha256(evidence_bytes).hexdigest()
        evidence = json.loads(evidence_bytes)
    except (CaptionDedupError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        return "INVALID", {
            "status": "INVALID", "reason_code": str(exc) or "CAPTION_EVIDENCE_INVALID",
            "evidence_path": str(evidence_path),
        }
    if not isinstance(evidence, Mapping):
        return "INVALID", {
            "status": "INVALID", "reason_code": "CAPTION_EVIDENCE_JSON_INVALID",
            "evidence_path": str(evidence_path), "evidence_sha256": evidence_sha,
        }
    if evidence.get("schema_version") != CAPTION_DEDUP_SCHEMA:
        return "INVALID", {"status": "INVALID", "reason_code": "CAPTION_EVIDENCE_SCHEMA_INVALID"}
    if evidence.get("policy") != CAPTION_DEDUP_POLICY:
        return "INVALID", {"status": "INVALID", "reason_code": "CAPTION_EVIDENCE_POLICY_INVALID"}
    external_scene = scene_spec.get("nested_media_external_audio_scene") is True
    expected_source_role = (
        EXTERNAL_AUDIO_SCENE_SOURCE_ROLE if external_scene else "WATCHED_MEDIA"
    )
    if evidence.get("source_role") != expected_source_role:
        return "INVALID", {"status": "INVALID", "reason_code": "CAPTION_EVIDENCE_SOURCE_ROLE_INVALID"}
    interval = evidence.get("source_interval_ms")
    if (
        not isinstance(evidence.get("candidate_id"), str) or not evidence["candidate_id"]
        or not isinstance(interval, list) or len(interval) != 2
        or any(not isinstance(value, int) or isinstance(value, bool) for value in interval)
        or interval[0] < 0 or interval[1] <= interval[0]
        or any(not _hashes_match(evidence.get(key), evidence.get(key))
               for key in ("source_media_sha256", "input_transcript_sha256"))
    ):
        return "INVALID", {"status": "INVALID", "reason_code": "CAPTION_EVIDENCE_BINDING_INVALID"}
    try:
        _, media_sha = _current_file_hash(
            source_media, unavailable="CURRENT_SCENE_INPUT_UNAVAILABLE"
        )
        _, srt_sha = _current_file_hash(
            subtitle_path, unavailable="CURRENT_SCENE_INPUT_UNAVAILABLE"
        )
    except Exception as exc:
        return "INVALID", {"status": "INVALID", "reason_code": str(exc)}
    drift: list[str] = []
    if evidence.get("candidate_id") != candidate_id:
        drift.append("candidate_id")
    if evidence.get("source_interval_ms") != [0, duration_ms]:
        drift.append("source_interval_ms")
    if not _hashes_match(evidence.get("source_media_sha256"), media_sha):
        drift.append("source_media_sha256")
    if not _hashes_match(evidence.get("input_transcript_sha256"), srt_sha):
        drift.append("input_transcript_sha256")
    if drift:
        return "STALE", {
            "status": "STALE", "reason_code": "CAPTION_EVIDENCE_CURRENT_BINDING_DRIFT",
            "drift_fields": drift, "evidence_path": str(evidence_path),
            "evidence_sha256": evidence_sha, "source_media_sha256": media_sha,
            "final_srt_sha256": srt_sha, "source_interval_ms": [0, duration_ms],
        }
    return "CURRENT", {
        "status": "CURRENT", "candidate_id": candidate_id,
        "evidence_path": str(evidence_path), "evidence_sha256": evidence_sha,
        "source_media_sha256": media_sha, "final_srt_sha256": srt_sha,
        "source_interval_ms": [0, duration_ms],
    }


def _run_current_nested_acoustic_observation(
    *, spec: dict, recut: FinalRecutArtifacts, cid: str,
    final_start: int, final_end: int, out_root: Path,
    adapters: ProducerFinalizationAdapters,
    previous_audit: Mapping[str, object] | None = None,
) -> dict:
    """Observe current final bytes without importing draft visual authority.

    Caption filtering has already finished. This fresh pass supplies only
    acoustic input evidence; it never consumes caption deletion policy again.
    Explicit descriptors retain their original, fail-closed input contract.
    """
    effective = dict(spec)
    handoff: dict = {"status": "EXPLICIT_INPUT", "subtitle_mutations": 0,
                     "publication_authorized": False}
    if spec.get("nested_media_acoustic") is None:
        historical = recut.nested_scene_spec or spec
        external_audio_scene = historical.get("nested_media_external_audio_scene") is True
        if historical.get("watched_media") is not True and not external_audio_scene:
            return {"status": "NOT_REQUESTED", "reason_code": "NO_WATCHED_SCENE_DISCOVERY",
                    "subtitle_mutations": 0, "publication_authorized": False}
        handoff = {"status": "EVIDENCE_ONLY", "timeline_basis": "final_recut_local",
                   "source_interval_ms": [0, final_end - final_start],
                   "padded_interval_ms": [final_start, final_end],
                   "historical_caption_evidence": historical.get("nested_media_caption_dedup"),
                   "subtitle_mutations": 0, "publication_authorized": False}
        provenance_path = recut.media_path.with_suffix(".provenance.json")
        if provenance_path.is_file() and not provenance_path.is_symlink():
            handoff["recut_provenance"] = {
                "path": str(provenance_path),
                "sha256": _evidence_file_sha256(provenance_path),
            }
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            final_recut = provenance.get("final_recut") if isinstance(provenance, Mapping) else None
            if isinstance(final_recut, Mapping):
                effective["nested_media_outer_context"] = {
                    "context_media": {
                        "path": final_recut.get("source_path"),
                        "sha256": final_recut.get("source_sha256"),
                    },
                    "current_srt": {
                        "path": str(recut.subtitle_path),
                        "sha256": _evidence_file_sha256(recut.subtitle_path),
                    },
                    "recut_provenance": handoff["recut_provenance"],
                }
        caption_source = historical
        if (
            "nested_media_caption_dedup" not in historical
            and "nested_media_caption_dedup" in spec
        ):
            caption_source = spec
        caption_status, caption_binding = _caption_scene_evidence_binding(
            scene_spec=caption_source,
            candidate_id=cid,
            source_media=recut.media_path,
            subtitle_path=recut.subtitle_path,
            duration_ms=final_end - final_start,
        )
        handoff["caption_evidence_binding"] = caption_binding
        if caption_status == "INVALID":
            return {
                **handoff,
                "label": "unknown",
                "action": "KEEP",
                "reason_code": "FINAL_SCENE_CAPTION_EVIDENCE_INVALID",
            }
        if (
            adapters.prepare_nested_acoustic_spec is None
            or (caption_status != "CURRENT" and adapters.observe_nested_captions is None)
        ):
            return {**handoff, "label": "unknown", "action": "KEEP",
                    "reason_code": "FINAL_SCENE_HANDOFF_RUNTIME_UNAVAILABLE"}
        if external_audio_scene:
            effective["nested_media_external_audio_scene"] = True
        else:
            effective["watched_media"] = True
        if caption_status == "CURRENT":
            # The observer's existing-bound-evidence fast path is deliberately
            # not called here: normal preparation below revalidates the full
            # scene/frame descriptor, while no new survey is dispatched.
            effective["nested_media_caption_dedup"] = deepcopy(
                caption_source["nested_media_caption_dedup"]
            )
        else:
            # A valid but older receipt must go through the ordinary fresh
            # current-pixel path. Never carry its locator/frame fields forward.
            for field in ("nested_media_caption_dedup", "nested_media_source_locator",
                          "nested_media_source_frame"):
                effective.pop(field, None)
        scene_audit: dict = {}
        current_srt = recut.subtitle_path.read_bytes()
        try:
            if caption_status != "CURRENT":
                effective = adapters.observe_nested_captions(
                    spec=effective, candidate_id=cid, source_media=recut.media_path,
                    source_start_ms=0, source_end_ms=final_end - final_start,
                    subtitle_path=recut.subtitle_path, evidence_root=out_root,
                    chat_authority_audit=scene_audit,
                )
            effective = _run_nested_source_locator(
                spec=effective, caption_spec=effective, adapters=adapters, cid=cid,
                padded=recut.media_path, final_start=0, final_end=final_end - final_start,
                subtitle_path=recut.subtitle_path, evidence_root=out_root,
                chat_authority_audit=scene_audit,
            )
            effective, preparation = adapters.prepare_nested_acoustic_spec(
                spec=effective, candidate_id=cid, source_media=recut.media_path,
                subtitle_path=recut.subtitle_path, duration_ms=final_end - final_start,
            )
            handoff["scene_audit"] = scene_audit
            handoff["descriptor_preparation"] = preparation
        except Exception as exc:
            if recut.subtitle_path.read_bytes() != current_srt:
                raise SystemExit("FINAL_SCENE_HANDOFF_MUTATED_SUBTITLES") from exc
            return {**handoff, "scene_audit": scene_audit, "label": "unknown", "action": "KEEP",
                    "reason_code": "FINAL_SCENE_HANDOFF_UNAVAILABLE", "error_type": type(exc).__name__}
        if recut.subtitle_path.read_bytes() != current_srt:
            raise SystemExit("FINAL_SCENE_HANDOFF_MUTATED_SUBTITLES")
        if effective.get("nested_media_acoustic") is None:
            return {**handoff, "label": "unknown", "action": "KEEP",
                    "reason_code": "FINAL_SCENE_ACOUSTIC_INPUT_UNAVAILABLE"}
    prepared = effective.get("nested_media_acoustic")
    current_observations = (
        prepared.get("observations")
        if isinstance(prepared, Mapping)
        else None
    )
    reuse = split_current_acoustic_observations_for_reuse(
        candidate_id=cid, source_media=recut.media_path,
        subtitle_path=recut.subtitle_path, previous_audit=previous_audit,
        current_observations=current_observations,
        current_spec=effective, current_prepared=prepared,
    )
    reused = reuse.get("reused_observations")
    unmatched = reuse.get("unmatched_observations")
    if not isinstance(reused, list):
        reused = []
    if not isinstance(unmatched, list):
        unmatched = list(current_observations) if isinstance(current_observations, list) else []

    dispatch_spec = effective
    if reused and isinstance(prepared, Mapping):
        dispatch_spec = deepcopy(effective)
        dispatch_prepared = dict(dispatch_spec["nested_media_acoustic"])
        dispatch_prepared["observations"] = deepcopy(unmatched)
        dispatch_spec["nested_media_acoustic"] = dispatch_prepared

    def with_reused_observations(
        fresh_audit: Mapping[str, object] | None,
    ) -> dict[str, object]:
        has_fresh_audit = isinstance(fresh_audit, Mapping)
        if isinstance(fresh_audit, Mapping):
            combined = deepcopy(dict(fresh_audit))
            fresh_observations = combined.get("observations", [])
            if not isinstance(fresh_observations, list):
                fresh_observations = []
        elif isinstance(previous_audit, Mapping):
            combined = deepcopy(dict(previous_audit))
            fresh_observations = []
        else:
            combined = {}
            fresh_observations = []
        combined["observations"] = deepcopy(reused) + deepcopy(fresh_observations)
        combined["status"] = "EVIDENCE_ONLY"
        combined["mutation_authorized"] = False
        combined["publication_authorized"] = False
        combined["subtitle_mutations"] = 0
        combined["provider_calls"] = (
            int(combined.get("provider_calls", 0) or 0) if has_fresh_audit else 0
        )
        combined["cpa_calls"] = (
            int(combined.get("cpa_calls", 0) or 0) if has_fresh_audit else 0
        )
        combined["continued_current_observation_count"] = len(reused)
        combined["continued_current_observation_binding"] = "STRICT_PREVIOUS_AUDIT"
        return combined

    if reused and not unmatched:
        result = with_reused_observations(None)
    elif adapters.observe_nested_acoustics is None:
        unavailable = {"status": "EVIDENCE_ONLY", "label": "unknown", "action": "KEEP",
                       "reason_code": "NESTED_ACOUSTIC_RUNTIME_UNAVAILABLE",
                       "subtitle_mutations": 0, "publication_authorized": False}
        result = with_reused_observations(unavailable) if reused else unavailable
    else:
        fresh = adapters.observe_nested_acoustics(
            spec=dispatch_spec, candidate_id=cid, source_media=recut.media_path,
            subtitle_path=recut.subtitle_path,
            evidence_root=out_root / "nested-acoustic-evidence",
        )
        result = with_reused_observations(fresh) if reused else fresh
    return {**result, "final_scene_handoff": handoff}


def _run_nested_source_locator(
    *,
    spec: dict,
    caption_spec: Mapping[str, object],
    adapters: ProducerFinalizationAdapters,
    cid: str,
    padded: Path,
    final_start: int,
    final_end: int,
    subtitle_path: Path,
    evidence_root: Path,
    chat_authority_audit: dict | None,
) -> dict:
    """Attach candidate-only source evidence after the watched scene check.

    The source locator is intentionally downstream of the existing visual
    caption observer and upstream of display-caption consumption.  It is not
    called for ordinary talk/game scenes, and its result never enables the
    acoustic observer or changes subtitle bytes.
    """

    locator = adapters.locate_nested_source
    dedup = caption_spec.get("nested_media_caption_dedup")
    if (
        locator is None
        or caption_spec.get("watched_media") is not True
        or not isinstance(dedup, Mapping)
        or not isinstance(dedup.get("evidence_path"), str)
    ):
        return dict(caption_spec)
    try:
        observed = locator(
            spec=caption_spec,
            candidate_id=cid,
            source_media=padded,
            source_start_ms=final_start,
            source_end_ms=final_end,
            subtitle_path=subtitle_path,
            evidence_root=evidence_root,
            chat_authority_audit=chat_authority_audit,
        )
        if not isinstance(observed, Mapping):
            raise TypeError("source locator returned a non-mapping")
        result = dict(observed)
        if result.get("status") not in {"EVIDENCE_ONLY", "UNKNOWN"} or result.get("action") != "KEEP":
            raise ValueError("source locator returned an authority result")
    except Exception as exc:
        # Source lookup is evidence-only.  A malformed or unavailable lookup
        # must leave the already valid caption policy usable and cannot expose
        # an exception string or turn into an authority result.
        result = {
            "schema_version": "nested-media-source-locator.v1",
            "candidate_id": cid,
            "status": "UNKNOWN",
            "action": "KEEP",
            "reason_code": f"SOURCE_LOCATOR_RUNTIME_{type(exc).__name__.upper()}",
            "mutation_authorized": False,
            "source_role_authority": False,
        }
    result["mutation_authorized"] = False
    result["source_role_authority"] = False
    if chat_authority_audit is not None:
        chat_authority_audit["nested_media_source_locator"] = result
    effective = dict(caption_spec)
    effective["nested_media_source_locator"] = result
    # Preserve the candidate for later audit consumers, but do not add an
    # acoustic descriptor or any text mutation authority to the production spec.
    spec["nested_media_source_locator"] = result
    return effective


def _resolve_deferred_foreign_introductions_after_redelivery(
    *,
    source_language_audit: dict[str, object],
    final_text: str,
    baseline_audit: Mapping[str, object],
    final_start: int,
) -> bool:
    """Verify the hash-bound baseline actually removed every deferred surface."""

    if source_language_audit.get("status") != (
        "DEFERRED_TO_REDELIVERY_BASELINE"
    ):
        return True
    authority_rows: list[dict[str, object]] = []
    baseline_sha256 = str(baseline_audit.get("baseline_sha256") or "")
    for row in baseline_audit.get("mappings") or []:
        if not isinstance(row, Mapping):
            continue
        canonical_text, _replacements = (
            canonicalize_japanese_native_script_surfaces(
                str(row.get("text") or "")
            )
        )
        baseline_cue_index = row.get("baseline_cue_index")
        output_cue_index = row.get("output_cue_index")
        authority_rows.append(
            {
                "authority_kind": "hash_bound_redelivery_baseline",
                "authority_id": (
                    "redelivery-baseline-cue-"
                    f"{baseline_cue_index or output_cue_index or 'unknown'}"
                ),
                "baseline_sha256": baseline_sha256,
                "mapping_kind": row.get("mapping_kind"),
                "baseline_cue_index": baseline_cue_index,
                "output_cue_index": output_cue_index,
                "authorized_native_script_surfaces": sorted(
                    set(re.findall(r"[ぁ-ゖァ-ヺー]{2,}", canonical_text))
                ),
                "local_windows": [
                    {
                        "start_ms": row.get("start_ms"),
                        "end_ms": row.get("end_ms"),
                    }
                ],
            }
        )
    resolution = resolve_deferred_foreign_introductions(
        source_language_audit,
        final_text,
        authority_rows=authority_rows,
        authority_kind="hash_bound_redelivery_baseline",
        timeline_offset_ms=final_start,
    )
    source_language_audit["deferred_resolution"] = resolution
    baseline_ok = (
        baseline_audit.get("status") in {"APPLIED", "ALREADY_SATISFIED"}
        and not baseline_audit.get("failures")
    )
    if baseline_ok and resolution["status"] == "PASS":
        source_language_audit["status"] = "RESOLVED_BY_REDELIVERY_BASELINE"
        return True
    source_language_audit["status"] = (
        "BLOCKED_REDELIVERY_BASELINE_DID_NOT_RESOLVE_FOREIGN_INTRODUCTION"
    )
    return False


def _repair_ruling_identity(repair: Mapping[str, object]) -> str:
    """Identity of an already validated local decision, without report noise.

    This is a progress check, never a substitute for _apply's authority gates.
    Changing just whole-film bytes, controller wording or pressure cannot turn
    a consumed target decision into another gain.
    """
    from .acoustic_witness_adjudication import _semantic_normalize
    judge = repair.get("judge") or {}
    judge = judge if isinstance(judge, Mapping) else {}
    projection = {key: repair.get(key) for key in (
        "matched_start_ms", "matched_end_ms", "before", "after", "action",
        "acoustic_witness_binding", "range_word_projection_binding",
    )}
    projection["judge"] = {key: judge.get(key) for key in (
        "completion_sha256", "authority_sha256", "choice", "model_identity", "semantic_evidence_fingerprint",
    )}
    editorial = repair.get("editorial_authority")
    if isinstance(editorial, Mapping):
        projection["raw_completion"] = editorial.get("raw_completion")
        projection["editorial_material"] = _semantic_normalize(editorial.get("request"), top_level=True)
    return hashlib.sha256(json.dumps(_semantic_normalize(projection),
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _new_repair_rulings(repairs, passes) -> bool:
    consumed = {_repair_ruling_identity(repair) for row in passes for repair in row.get("repairs", [])}
    return bool(repairs) and all(_repair_ruling_identity(repair) not in consumed for repair in repairs)


def _apply_exact_final_cpa_repairs(
    srt_text: str,
    audit: Mapping[str, object], *,
    clip_context: Mapping[str, object] | None = None,
    adjudication_stage: str = "exact_release_adjudication",
    protected_cue_indexes: frozenset[int] = frozenset(),
) -> tuple[str, list[dict[str, object]]]:
    """Apply only exact-final findings already authorized by CPA.

    The exact-final review runs after redelivery-baseline replay, so it is the
    first stage that can see a defect resurrected by that late authority.  A
    CPA decision with a typed mutation receipt should be able to repair the
    exact bytes in the same producer run; forcing a complete ASR/cover rerun
    merely to consume the persisted carryover wastes time and provider quota.

    This remains fail closed: cue position, current-text hash, request payload,
    immutable timing, decision authority, and mutation receipt must all agree.
    Any finding that does not satisfy the complete contract is left untouched
    for the normal release block/carryover path.
    """

    if adjudication_stage not in {"exact_release_adjudication", "context_audio_adjudication"}:
        raise ValueError("TYPED_CORRECTION_STAGE_INVALID")
    cues = parse_srt_cues(srt_text)
    if any(type(index) is not int or not 1 <= index <= len(cues)
           for index in protected_cue_indexes):
        raise ValueError("HUMAN_LITERAL_PROTECTED_CUE_INVALID")
    findings = audit.get("findings")
    if not isinstance(findings, list):
        return srt_text, []
    repairs: list[dict[str, object]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        cue_index = finding.get("cue_index")
        proposed = adjudicated_proposed_full_cue(
            finding, srt_text=srt_text, clip_context=clip_context,
            adjudication_stage=adjudication_stage
        )
        if (type(cue_index) is int and cue_index in protected_cue_indexes
                and isinstance(proposed, str)):
            from src.autoslice.chat_evidence import normalize_chat_text
            if normalize_chat_text(proposed) != normalize_chat_text(cues[cue_index - 1].text):
                if isinstance(finding, dict):
                    finding["human_literal_proposal_veto"] = {
                        "status": "BLOCKED_PROPOSAL",
                        "reason_code": "HUMAN_LITERAL_REQUIRES_CONTEXT_REVIEW",
                        "mutation_authorized": False,
                        "finding_resolved": False,
                    }
                continue
        adjudication = finding.get(adjudication_stage)
        editorial_cycle = finding.get("exact_final_cpa_cycle_adjudication")
        if (isinstance(adjudication, Mapping)
            and adjudication.get("schema_version") == "subtitle-editorial-adjudication.v1"
            and isinstance(editorial_cycle, Mapping) and editorial_cycle.get("status") == "BLOCK"):
            continue
        if (isinstance(adjudication, Mapping)
            and adjudication.get("schema_version") == "subtitle-editorial-adjudication.v1"
            and adjudication.get("repaired") is True and isinstance(proposed, str)
            and type(cue_index) is int and 1 <= cue_index <= len(cues)):
            from src.autoslice.editorial_subtitle import repair_receipt
            current = cues[cue_index - 1]
            try:
                repair = repair_receipt(
                    finding, adjudication, current=current.text,
                    window=(current.start_ms, current.end_ms),
                    srt_text=srt_text, clip_context=clip_context,
                )
            except (ValueError, TypeError, KeyError):
                continue
            cues[cue_index - 1] = type(current)(index=current.index, start_ms=current.start_ms,
                                               end_ms=current.end_ms, text=proposed)
            repairs.append(repair)
            continue
        cycle_adjudication = finding.get("exact_final_cpa_cycle_adjudication")
        if (
            isinstance(cue_index, bool)
            or not isinstance(cue_index, int)
            or not 1 <= cue_index <= len(cues)
            or not isinstance(proposed, str)
            or (
                adjudication_stage == "exact_release_adjudication"
                and isinstance(cycle_adjudication, Mapping)
                and cycle_adjudication.get("schema_version")
                == "exact-final-cpa-cycle-adjudication.v1"
                and cycle_adjudication.get("status") == "BLOCK"
            )
            or not isinstance(adjudication, Mapping)
            or adjudication.get("schema_version")
            != "subtitle-span-adjudication.v1"
            or adjudication.get("status") != "OBSERVED"
            or adjudication.get("decision_authority") != "CPA_JUDGE"
            or adjudication.get("repaired") is not True
            or adjudication.get("timing_immutable") is not True
        ):
            continue
        mutation = adjudication.get("mutation_authority")
        request = adjudication.get("request")
        witness_judge = adjudication.get("witness_judge")
        judge = (
            witness_judge.get("judge")
            if isinstance(witness_judge, Mapping)
            else None
        )
        current = cues[cue_index - 1]
        is_drop = bool(
            proposed == ""
            and finding.get("repair_class") == "acoustic_drop_cue"
            and valid_inaudible_drop_authority(adjudication)
        )
        current_sha256 = hashlib.sha256(
            current.text.encode("utf-8")
        ).hexdigest()
        request_sha256 = (
            str(request.get("request_sha256") or "")
            if isinstance(request, Mapping)
            else ""
        )
        witness = adjudication.get("verdict")
        inaudible_nonempty = bool(
            proposed
            and isinstance(witness, Mapping)
            and witness.get("status") == "OBSERVED"
            and witness.get("target_audible") is False
        )
        inaudible_override_valid = bool(
            inaudible_nonempty
            and isinstance(request, Mapping)
            and isinstance(witness_judge, Mapping)
            and valid_inaudible_witness_override(
                check_request=request,
                witness=witness,
                witness_judge=witness_judge,
            )
        )
        if (
            not isinstance(mutation, Mapping)
            or mutation.get("schema_version")
            != "subtitle-correction-mutation-authority.v1"
            or mutation.get("status") != "PASS"
            or not isinstance(request, Mapping)
            or request.get("schema_version")
            != "subtitle-span-acoustic-check-request.v1"
            or request.get("base_text_sha256") != current_sha256
            or finding.get("base_text_sha256") != current_sha256
            or request.get("current_cue") != current.text
            or (adjudication_stage == "context_audio_adjudication" and (
                request.get("matched_start_ms") != current.start_ms
                or request.get("matched_end_ms") != current.end_ms
            ))
            or request.get("proposed_cue") != proposed
            or proposed == current.text
            or (not proposed.strip() and not is_drop)
            or len(request_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in request_sha256
            )
            or not isinstance(judge, Mapping)
            or judge.get("status") != "JUDGED"
            or (
                judge.get("choice") != "PROPOSED"
                and not (
                    is_drop
                    and judge.get("choice") == "DROP"
                )
            )
            or (
                inaudible_nonempty
                and not inaudible_override_valid
            )
            or not valid_convergence_mutation_authority(
                adjudication,
                proposed=proposed,
                window=(current.start_ms, current.end_ms),
            )
        ):
            continue
        cues[cue_index - 1] = type(current)(
            index=current.index,
            start_ms=current.start_ms,
            end_ms=current.end_ms,
            text=proposed,
        )
        repairs.append(
            build_self_heal_repair_receipt(
                finding=finding,
                cue_index=cue_index,
                current_text=current.text,
                proposed=proposed,
                matched_start_ms=current.start_ms,
                matched_end_ms=current.end_ms,
                adjudication=adjudication,
                judge=judge,
                mutation=mutation,
                request_sha256=request_sha256,
                is_drop=is_drop,
                inaudible_override_valid=inaudible_override_valid,
            )
        )
        if adjudication_stage != "exact_release_adjudication":
            repairs[-1]["origin_adjudication_stage"] = adjudication_stage
    if not repairs:
        return srt_text, []
    if any(repair.get("action") in {"DROP_CUE", "OMIT_CUE_DISPLAY"} for repair in repairs):
        cues = [
            type(cue)(
                index=index,
                start_ms=cue.start_ms,
                end_ms=cue.end_ms,
                text=cue.text,
            )
            for index, cue in enumerate(
                (cue for cue in cues if cue.text.strip()),
                start=1,
            )
        ]
    return _render_cues_to_srt(cues), repairs


def resume_typed_context_correction_checkpoint(
    srt_text: str,
    chat_authority_audit: Mapping[str, object],
    correction_result: Mapping[str, object],
    *,
    output_srt_text: str,
    delivery_start_ms: int,
    clip_context: Mapping[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    """Register a completed normal correction without repeating its CPA calls.

    Ordinary context adjudication can finish while the whole-film review is
    still blocked. Replay its original requests against the exact predecessor,
    using the finalizer's receipt/owner registration, and retain the actual
    blocked review. The original context stage is explicit; no old exact
    adjudication or cycle BLOCK is relabelled as the new decision.
    """
    before_sha = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    after_sha = hashlib.sha256(output_srt_text.encode("utf-8")).hexdigest()
    correction = correction_result.get("correction_audit")
    review = correction_result.get("candidate_final_audit")
    release = correction_result.get("final_release_gate")
    previous_review = chat_authority_audit.get("final_review_audit")
    if (
        correction_result.get("schema_version") != "normal-cpa-typed-adjudication-result.v1"
        or correction_result.get("status") not in {"COMPLETED", "COMPLETED_PARTIAL"}
        or correction_result.get("input_srt_sha256") != before_sha
        or correction_result.get("output_srt_sha256") != after_sha
        or before_sha == after_sha
        or chat_authority_audit.get("schema_version") != "chat-authority-audit.v2"
        or chat_authority_audit.get("mutation_authorized") is True
        or chat_authority_audit.get("final_output_srt_sha256") != before_sha
        or not isinstance(previous_review, Mapping)
        or previous_review.get("reviewed_srt_sha256") != "sha256:" + before_sha
        or (clip_context is not None and correction_result.get("candidate_id")
            != clip_context.get("candidate_id"))
        or not isinstance(correction, Mapping)
        or correction.get("reviewed_srt_sha256") != "sha256:" + after_sha
        or not isinstance(review, Mapping)
        or review.get("status") != "FLAGGED"
        or review.get("release_gate") != "BLOCK"
        or review.get("reviewed_srt_sha256") != "sha256:" + after_sha
        or review.get("correction_pass") != correction
        or not isinstance(release, Mapping)
        or release.get("status") != "BLOCK"
    ):
        raise ValueError("TYPED_CONTEXT_CHECKPOINT_BINDING_INVALID")
    mutation_audit = audit_correction_mutation_authority(correction)
    if mutation_audit.get("status") != "PASS":
        raise ValueError("TYPED_CONTEXT_CHECKPOINT_MUTATION_INVALID")
    _validate_exact_final_cpa_self_heal(
        chat_authority_audit, expected_srt_sha256="sha256:" + before_sha,
        allow_pending=True,
    )
    from src.autoslice.human_sequence_alignment import protected_indexes as human_protected_indexes
    replayed, repairs = _apply_exact_final_cpa_repairs(
        srt_text, correction, clip_context=clip_context,
        adjudication_stage="context_audio_adjudication",
        protected_cue_indexes=frozenset(human_protected_indexes(
            chat_authority_audit.get("human_sequence_alignment") or {}, srt_text,
            timeline_offset_ms=delivery_start_ms,
        )),
    )
    if (
        replayed != output_srt_text
        or not repairs
        or len(repairs) != correction.get("applied_count")
        or mutation_audit.get("validated_mutation_count") != len(repairs)
    ):
        raise ValueError("TYPED_CONTEXT_CHECKPOINT_REPLAY_DRIFT")
    authority = deepcopy(dict(chat_authority_audit))
    previous = authority.get("exact_final_cpa_self_heal")
    pending = deepcopy(previous) if isinstance(previous, Mapping) else build_run_audit([], [])
    # Completed runs keep their independently accepted history. Only a
    # pending run's terminal bytes can acquire a new pass in that same run.
    if pending.get("status") == "PASS":
        pending = build_run_audit([], retained_runs(previous))
    passes = pending.setdefault("passes", [])
    passes.append({
        "schema_version": "exact-final-cpa-self-heal-pass.v1",
        "pass_index": len(passes) + 1,
        "input_srt_sha256": "sha256:" + before_sha,
        "output_srt_sha256": "sha256:" + after_sha,
        "repairs": repairs,
    })
    pending["status"] = "REVIEW_PENDING"
    pending.pop("final_srt_sha256", None)
    _register_exact_final_cpa_repairs(
        authority, repairs, delivery_start_ms=delivery_start_ms,
    )
    authority["exact_final_cpa_self_heal"] = pending
    authority["final_output_srt_sha256"] = after_sha
    current_review = deepcopy(dict(review))
    # These completed calls occurred after the predecessor checkpoint. Keep
    # their soft usage for future control decisions; replay itself costs zero.
    usage = authority.get("cpa_adjudication_usage")
    calls = correction.get("context_adjudication_count")
    if isinstance(usage, Mapping) and type(calls) is int and calls >= len(repairs):
        count = usage.get("provider_adjudication_count")
        if type(count) is int and count >= 0:
            next_usage = {**usage, "prior_provider_adjudication_count": count,
                          "provider_adjudication_count": count + calls}
            authority["cpa_adjudication_usage"] = next_usage
            current_review["cpa_adjudication_usage"] = deepcopy(next_usage)
    authority["final_review_audit"] = current_review
    _validate_exact_final_cpa_self_heal(
        authority, expected_srt_sha256="sha256:" + after_sha, allow_pending=True,
    )
    return replayed, authority


def _replayable_exact_final_carryover_findings(
    srt_text: str, path: Path, *,
    clip_context: Mapping[str, object] | None = None,
    review_timeline_offset_ms: int | None = None,
) -> list[dict[str, object]]:
    """Remap a prior exact CPA decision onto identical current cue bytes/time.

    With ``review_timeline_offset_ms`` the time match is the physical target
    (G2: local window + its own offset); a request sealed on another local
    clock (``text_recovery_only`` derives a different origin) is the same cue.
    """

    def same_window(request: Mapping[str, object], cue: object) -> bool:
        start, end = request.get("matched_start_ms"), request.get("matched_end_ms")
        own_offset = request.get("source_media_timeline_offset_ms")
        if (review_timeline_offset_ms is None or type(own_offset) is not int
                or type(review_timeline_offset_ms) is not int
                or type(start) is not int or type(end) is not int):
            return start == cue.start_ms and end == cue.end_ms
        return ((start + own_offset, end + own_offset)
                == (cue.start_ms + review_timeline_offset_ms, cue.end_ms + review_timeline_offset_ms))

    cues = parse_srt_cues(srt_text)
    by_sha256: dict[str, list[tuple[int, object]]] = {}
    for cue_position, cue in enumerate(cues, start=1):
        digest = hashlib.sha256(cue.text.encode("utf-8")).hexdigest()
        by_sha256.setdefault(digest, []).append((cue_position, cue))
    findings: list[dict[str, object]] = []
    for row in load_final_review_carryover(path):
        base_sha256 = str(row.get("base_text_sha256") or "")
        matches = by_sha256.get(base_sha256, [])
        adjudication = row.get("exact_release_adjudication")
        request = adjudication.get("request") if isinstance(adjudication, Mapping) else None
        proposed = adjudicated_proposed_full_cue(
            row, srt_text=srt_text, clip_context=clip_context
        )
        if proposed is None or not isinstance(request, Mapping):
            continue
        matching_windows = [
            (cue_position, cue)
            for cue_position, cue in matches
            if (
                request.get("base_text_sha256") == base_sha256
                and request.get("current_cue") == cue.text
                and request.get("proposed_cue") == proposed
                and same_window(request, cue)
            )
        ]
        if len(matching_windows) != 1:
            continue
        cue_position, cue = matching_windows[0]
        finding = dict(row)
        finding["cue_index"] = cue_position
        finding["proposed_full_cue"] = proposed
        finding["carryover_exact_replay"] = {
            "schema_version": "exact-final-carryover-replay.v1",
            "status": "REPLAYABLE",
            "basis": "UNIQUE_TEXT_HASH_AND_EXACT_TIME_WINDOW",
            "before_sha256": "sha256:" + base_sha256,
            "matched_start_ms": cue.start_ms,
            "matched_end_ms": cue.end_ms,
        }
        findings.append(finding)
    return findings


def _overlay_exact_carryover_findings(
    audit: object,
    findings: list[dict[str, object]],
) -> object:
    """Add replayable CPA findings to an otherwise independent exact scan."""

    if (
        not findings
        or not isinstance(audit, dict)
        or audit.get("schema_version") != "final-review-audit.v2"
        or not isinstance(audit.get("findings"), list)
    ):
        return audit
    existing = {
        _final_review_carryover_row_key(row)
        for row in audit["findings"]
        if isinstance(row, Mapping)
    }
    additions = [
        row
        for row in findings
        if _final_review_carryover_row_key(row) not in existing
    ]
    if not additions:
        return audit
    audit["findings"] = [*audit["findings"], *additions]
    audit["validated_finding_count"] = len(audit["findings"])
    audit["status"] = "FLAGGED"
    audit["release_gate"] = "BLOCK"
    from .finding_lifecycle import review_outcome
    audit = review_outcome(audit)
    discovery = audit.get("discovery")
    if isinstance(discovery, dict):
        discovery["explicit_empty_findings"] = False
        raw_count = discovery.get("raw_validated_finding_count")
        discovery["raw_validated_finding_count"] = (
            int(raw_count) if isinstance(raw_count, int) else 0
        ) + len(additions)
    return audit


def _annotate_consumed_correction_carryovers(
    audit: object,
    repairs: Mapping[str, Mapping[str, object]],
) -> None:
    if not isinstance(audit, dict) or not repairs:
        return
    correction_pass = audit.get("correction_pass")
    findings = (
        correction_pass.get("findings")
        if isinstance(correction_pass, dict)
        else None
    )
    if not isinstance(findings, list):
        return
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        base_sha256 = str(finding.get("base_text_sha256") or "")
        repair = repairs.get(base_sha256)
        if repair is None:
            continue
        finding["carryover_consumption"] = {
            "schema_version": "exact-final-carryover-consumption.v1",
            "status": "CONSUMED_BY_EXACT_FINAL_CPA",
            "before_sha256": repair["before_sha256"],
            "after_sha256": repair["after_sha256"],
            "request_sha256": repair["request_sha256"],
        }


def _unconsumed_correction_carryover_base_sha256(
    audit: object,
) -> set[str]:
    """Return the exact remapped rows that still need a terminal decision."""

    if not isinstance(audit, Mapping):
        return set()
    correction_pass = audit.get("correction_pass")
    findings = (
        correction_pass.get("findings")
        if isinstance(correction_pass, Mapping)
        else None
    )
    if not isinstance(findings, list):
        return set()
    pending: set[str] = set()
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        remap = finding.get("carryover_replay_remap")
        base_sha256 = str(finding.get("base_text_sha256") or "")
        if (
            isinstance(remap, Mapping)
            and remap.get("schema_version")
            == "final-review-carryover-remap.v1"
            and remap.get("status") == "PASS"
            and len(base_sha256) == 64
            and all(
                character in "0123456789abcdef"
                for character in base_sha256
            )
            and not correction_carryover_consumed(finding)
        ):
            pending.add(base_sha256)
    return pending


def _register_exact_final_cpa_repairs(
    chat_authority_audit: dict[str, object],
    repairs: list[dict[str, object]],
    *,
    delivery_start_ms: int,
) -> None:
    """Make exact-final repairs own their final delivery surfaces.

    Correction review runs before exact-final and may have already registered
    a typed ``entity_repairs`` row. When exact-final later changes that exact
    cue again, retaining the earlier expected surface creates an impossible
    authority conflict (the pink-room ``好磕吧`` -> ``好可怕`` case).

    Reconcile only an exact same-window, exact-text predecessor and append the
    CPA repair as the new final-surface owner. Hashes, timing, judge authority,
    and mutation receipt are revalidated here; an overlapping but non-identical
    row is never retired.
    """

    rows = chat_authority_audit.setdefault("entity_repairs", [])
    if not isinstance(rows, list):
        raise ValueError("EXACT_FINAL_ENTITY_REPAIR_LEDGER_INVALID")
    applied_rows = chat_authority_audit.setdefault("applied", [])
    if not isinstance(applied_rows, list):
        raise ValueError("EXACT_FINAL_APPLIED_LEDGER_INVALID")
    registrations = chat_authority_audit.setdefault(
        "exact_final_cpa_surface_registrations", []
    )
    if not isinstance(registrations, list):
        raise ValueError("EXACT_FINAL_SURFACE_REGISTRATION_LEDGER_INVALID")

    for repair in repairs:
        if repair.get("repair_class") == "editorial_rendering" or repair.get("editorial_authority") is not None:
            from src.autoslice.editorial_subtitle import validate_repair
            validate_repair(repair)
            if delivery_start_ms != repair["editorial_authority"]["request"]["source_media_timeline_offset_ms"]:
                raise ValueError("EDITORIAL_REGISTRATION_CLOCK_DRIFT")
        before = repair.get("before")
        after = repair.get("after")
        local_start = repair.get("matched_start_ms")
        local_end = repair.get("matched_end_ms")
        mutation = repair.get("mutation_authority")
        from src.autoslice.editorial_subtitle import valid_display_omission_repair
        is_drop = bool(after == "" and (valid_inaudible_drop_repair(repair)
                                        or valid_display_omission_repair(repair)))
        inaudible_override = bool(
            after and valid_inaudible_override_repair(repair)
        )
        witness = repair.get("acoustic_witness")
        inaudible_nonempty = bool(
            after
            and isinstance(witness, Mapping)
            and witness.get("status") == "OBSERVED"
            and witness.get("target_audible") is False
        )
        if (
            not isinstance(before, str)
            or not before
            or not isinstance(after, str)
            or (not after.strip() and not is_drop)
            or (inaudible_nonempty and not inaudible_override)
            or isinstance(local_start, bool)
            or not isinstance(local_start, int)
            or isinstance(local_end, bool)
            or not isinstance(local_end, int)
            or not 0 <= local_start < local_end
            or repair.get("decision_authority") != "CPA_JUDGE"
            or repair.get("timing_immutable") is not True
            or not isinstance(mutation, Mapping)
            or mutation.get("schema_version")
            != "subtitle-correction-mutation-authority.v1"
            or mutation.get("status") != "PASS"
            or repair.get("before_sha256")
            != "sha256:" + hashlib.sha256(before.encode("utf-8")).hexdigest()
            or repair.get("after_sha256")
            != "sha256:" + hashlib.sha256(after.encode("utf-8")).hexdigest()
        ):
            raise ValueError("EXACT_FINAL_SURFACE_REGISTRATION_INVALID")
        matched_start = delivery_start_ms + local_start
        matched_end = delivery_start_ms + local_end
        repair_sha256 = "sha256:" + canonical_sha256(repair, allow_nan=True)
        superseded_indexes: list[int] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("reconciliation"):
                continue
            expected = str(row.get("structured_exact_text") or "")
            if not expected:
                row_after = row.get("after")
                if isinstance(row_after, list) and len(row_after) == 1:
                    expected = str(row_after[0] or "")
                elif isinstance(row_after, str):
                    expected = row_after
            if (
                row.get("matched_start_ms") == matched_start
                and row.get("matched_end_ms") == matched_end
                and expected == before
            ):
                row["reconciliation"] = {
                    "schema_version": "exact-final-cpa-supersession.v1",
                    "status": "SUPERSEDED_BY_EXACT_FINAL_CPA",
                    "exact_final_repair_sha256": repair_sha256,
                    "before_sha256": repair["before_sha256"],
                    "after_sha256": repair["after_sha256"],
                    "timing_immutable": True,
                }
                superseded_indexes.append(index)
        superseded_applied_indexes: list[int] = []
        for index, row in enumerate(applied_rows):
            if not isinstance(row, dict) or row.get("reconciliation"):
                continue
            expected = str(row.get("exact_text") or "")
            if (
                row.get("matched_start_ms") == matched_start
                and row.get("matched_end_ms") == matched_end
                and expected
                and expected in before
                and expected not in after
            ):
                # A chat exact-read may own only a substring of a cue (for
                # example ``小李你怎么被点了！`` inside a cue that continues
                # with ``，不知道``).  A later exact-final CPA repair owns the
                # complete cue.  Retire the older substring only when the
                # same immutable cue geometry and the hash-validated
                # before→after receipt prove that the CPA repair removed it.
                # Mere overlap is never sufficient.
                row["reconciliation"] = {
                    "schema_version": (
                        "exact-final-cpa-exact-read-supersession.v1"
                    ),
                    "status": "SUPERSEDED_BY_EXACT_FINAL_CPA",
                    "exact_final_repair_sha256": repair_sha256,
                    "before_sha256": repair["before_sha256"],
                    "after_sha256": repair["after_sha256"],
                    "timing_immutable": True,
                }
                superseded_applied_indexes.append(index)
        owner = {
            "mode": "exact_final_cpa_self_heal",
            "decision_authority": "CPA_JUDGE",
            "action": repair.get("action"),
            "repair_class": repair.get("repair_class"),
            "policy_branch": repair.get("policy_branch"),
            "acoustic_witness": repair.get("acoustic_witness"),
            "judge": repair.get("judge"),
            "drop_authority": repair.get("drop_authority"),
            "inaudible_witness_override": repair.get(
                "inaudible_witness_override"
            ),
            "request_sha256": repair.get("request_sha256"),
            "mutation_authority": dict(mutation),
            "cue_indexes": [repair["cue_index"]],
            "matched_start_ms": matched_start,
            "matched_end_ms": matched_end,
            "before": [before],
            "after": [after],
            "structured_exact_text": after,
            "survived": True,
            "timing_immutable": True,
            "boundary_required": False,
            "boundary_owner_rejection": (
                "POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER"
            ),
            "exact_final_repair_sha256": repair_sha256,
            "superseded_entity_repair_indexes": superseded_indexes,
            "superseded_applied_indexes": superseded_applied_indexes,
        }
        if repair.get("editorial_authority") is not None:
            owner.update(editorial_authority=repair["editorial_authority"],
                         rendering_mode=repair["rendering_mode"], literal_transcription=False)
        rows.append(owner)
        registrations.append(
            {
                "schema_version": "exact-final-cpa-surface-registration.v1",
                "status": "REGISTERED",
                "exact_final_repair_sha256": repair_sha256,
                "owner_entity_repair_index": len(rows) - 1,
                "superseded_entity_repair_indexes": superseded_indexes,
                "superseded_applied_indexes": superseded_applied_indexes,
            }
        )


def _matching_cycle_return_memos(
    repairs: list[dict[str, object]],
    convergence_memos: list[dict[str, object]],
    *,
    prior_memo_ids: set[str],
    input_srt_sha256: str,
) -> list[tuple[dict[str, object], dict[str, object], str]]:
    """Find one fresh, valid CPA memo for every repeated-output repair."""

    available: list[tuple[str, dict[str, object]]] = []
    for memo in convergence_memos:
        memo_id = exact_final_convergence_memo_identity(memo)
        if memo_id is not None and memo_id not in prior_memo_ids:
            available.append((memo_id, memo))
    matches_for_repairs: list[
        tuple[dict[str, object], dict[str, object], str]
    ] = []
    used_ids: set[str] = set()
    for repair in repairs:
        matches: list[tuple[str, dict[str, object]]] = []
        for memo_id, memo in available:
            if memo_id in used_ids:
                continue
            if not valid_exact_final_convergence_memo_binding(
                memo,
                input_srt_sha256=input_srt_sha256,
                before_text=str(repair.get("before") or ""),
                after_text=str(repair.get("after") or ""),
                before_sha256=str(repair.get("before_sha256") or ""),
                after_sha256=str(repair.get("after_sha256") or ""),
                matched_start_ms=repair.get("matched_start_ms"),
                matched_end_ms=repair.get("matched_end_ms"),
            ):
                continue
            matches.append((memo_id, memo))
        if len(matches) != 1:
            return []
        memo_id, memo = matches[0]
        used_ids.add(memo_id)
        matches_for_repairs.append((repair, memo, memo_id))
    return matches_for_repairs if len(matches_for_repairs) == len(repairs) else []


def _resume_source_extended_review(*, cid: str, out_root: Path, final_text: str,
                                   chat_authority_audit: dict, timing_qa: Mapping | None = None) -> dict:
    """Bind a normal boundary draft to the unchanged terminal repair prefix."""
    path = out_root / f"{cid}.boundary_audit.json"
    if not path.is_file():
        raise FinalReviewContractError(RECOVERY_MATERIAL_MISSING)
    try:
        boundary = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise FinalReviewContractError(RECOVERY_TYPE_INAPPLICABLE) from exc
    extension = boundary.get("current_delivery_closure_binding", {}) if isinstance(boundary, Mapping) else None
    if not isinstance(extension, Mapping) or extension.get("schema_version") != "current-delivery-source-extension.v1":
        raise FinalReviewContractError(RECOVERY_TYPE_INAPPLICABLE)
    count = extension.get("current_prefix_cue_count")
    if type(count) is not int or count < 1:
        raise FinalReviewContractError(RECOVERY_PREFIX_INVALID)
    blocks = final_text.rstrip().split("\n\n")
    if len(blocks) <= count:
        raise FinalReviewContractError(RECOVERY_PREFIX_INVALID)
    before = "\n\n".join(blocks[:count]) + "\n"
    _validate_exact_final_cpa_self_heal(
        chat_authority_audit,
        expected_srt_sha256="sha256:" + hashlib.sha256(before.encode()).hexdigest(),
        allow_pending=True,
    )
    review = chat_authority_audit.get("final_review_audit")
    context = review.get("source_boundary_context") if isinstance(review, Mapping) else None
    context = context if isinstance(context, Mapping) else {}
    source_path = Path(str(context.get("source_srt_path") or ""))
    if not source_path.is_file():
        raise FinalReviewContractError(RECOVERY_MATERIAL_MISSING)
    source = source_path.read_text()
    if context.get("source_srt_sha256") != "sha256:" + hashlib.sha256(source.encode()).hexdigest():
        raise FinalReviewContractError("EXACT_FINAL_SOURCE_EXTENSION_SOURCE_DRIFT")
    receipt = deepcopy(chat_authority_audit["exact_final_cpa_self_heal"])
    receipt["status"] = "REVIEW_PENDING"
    receipt.pop("final_srt_sha256", None)
    timing_path = out_root / "replacement_recuts" / f"{cid}.recut.timing_qa.json"
    receipt.setdefault(SOURCE_EXTENSION_SUCCESSORS_KEY, []).append({
        "schema_version": "exact-final-source-extension-successor.v1",
        "status": "DRAFT_REQUIRES_COMPLETE_REVIEW", "release_gate": "BLOCK",
        "mutation_authorized": False, "after_pass_index": len(receipt["passes"]),
        "input_srt_text": before, "output_srt_text": final_text,
        "source_srt_text": source,
        "input_srt_sha256": "sha256:" + hashlib.sha256(before.encode()).hexdigest(),
        "output_srt_sha256": sha256_ref(hashlib.sha256(final_text.encode()).hexdigest()),
        "source_srt_sha256": context["source_srt_sha256"],
        "boundary_audit": boundary,
        "timing_qa": (dict(timing_qa) if timing_qa is not None else
                      json.loads(timing_path.read_text()) if timing_path.is_file() else {}),
    })
    _validate_exact_final_cpa_self_heal(
        {"exact_final_cpa_self_heal": receipt},
        expected_srt_sha256=sha256_ref(hashlib.sha256(final_text.encode()).hexdigest()),
        allow_pending=True,
    )
    return receipt


def _persist_human_sequence_current_checkpoint(kwargs: dict) -> None:
    """The normal exact gate owns this recovery write, including safe blocks."""
    from src.autoslice.producer_text_pipeline import _continuation_validate_existing_authority
    from src.autoslice.producer_text_checkpoint import _frozen_binding, write_current_text_checkpoint
    out_root, cid, recut = kwargs['out_root'], kwargs['cid'], kwargs['recut']
    authority, authority_path = kwargs['chat_authority_audit'], kwargs['chat_authority_path']
    text = recut.subtitle_path.read_text(encoding='utf-8')
    context_path = out_root / f'{cid}.clip-context.json'
    context = json.loads(context_path.read_bytes())
    review = _continuation_validate_existing_authority(authority, srt_text=text,
        srt_sha256=hashlib.sha256(text.encode()).hexdigest(), clip_context=context, candidate_id=cid)
    from src.autoslice.final_review_contract import _validate_review_geometry
    _validate_review_geometry(review, expected_srt_sha256='sha256:' + hashlib.sha256(text.encode()).hexdigest())
    source = json.loads((out_root / f'{cid}.source-text-checkpoint.json').read_bytes())
    padded = Path(source['padded']['path'])
    if 'sha256:' + _sha256(padded) != source['padded']['sha256']:
        raise ValueError('HUMAN_SEQUENCE_CHECKPOINT_MEDIA_DRIFT')
    stage_binding, boundary_binding = source.get('authority') or {}, source.get('boundary_srt') or {}
    if any(not b.get('path') or not b.get('sha256') for b in (stage_binding,boundary_binding)):
        raise ValueError('HUMAN_SEQUENCE_CHECKPOINT_SOURCE_BINDING_MISSING')
    stage_path, boundary_path = Path(stage_binding['path']), Path(boundary_binding['path'])
    for path, binding in ((stage_path,stage_binding),(boundary_path,boundary_binding)):
        if path.is_symlink() or 'sha256:' + _sha256(path) != binding['sha256']:
            raise ValueError('HUMAN_SEQUENCE_CHECKPOINT_SOURCE_DRIFT')
    try:
        stage = review_from_bytes(stage_path.read_bytes(), directory=stage_path.parent)
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('HUMAN_SEQUENCE_CHECKPOINT_SOURCE_AUTHORITY_INVALID') from exc
    from src.autoslice.boundary_semantic_review import cue_grid_sha256
    from src.autoslice.jingting_chunker import parse_srt_cues
    witness = review['boundary_semantic_review'].get('source_separation_witness') or {}
    source_review = (stage.get('final_review_audit') or {}).get('boundary_semantic_review') or {}
    source_context = (stage.get('final_review_audit') or {}).get('source_boundary_context') or {}
    grid = cue_grid_sha256(parse_srt_cues(boundary_path.read_text()))
    # Endpoint binding is added after source-stage freezing. The frozen
    # request and exact wider input remain the source evidence, while the
    # complete current geometry validator above owns the final witness.
    if (witness.get('source_request_sha256') != source_review.get('request_sha256')
            or not source_review.get('request_sha256')
            or witness.get('source_cue_grid_sha256') != grid
            or source_review.get('cue_grid_sha256') != grid
            or source_context.get('source_srt_sha256') != boundary_binding['sha256']
            or stage.get('post_transcript_entity_output_srt_sha256') != authority.get('post_transcript_entity_output_srt_sha256')):
        raise ValueError('HUMAN_SEQUENCE_CHECKPOINT_SOURCE_WITNESS_DRIFT')
    clock_root = out_root / 'current-text-clock'
    clock = {'authority':_frozen_binding(clock_root,'authority',authority_path),
             'srt':_frozen_binding(clock_root,'srt',recut.subtitle_path),
             'boundary_srt':_frozen_binding(clock_root,'boundary_srt',boundary_path)}
    write_current_text_checkpoint(out_root / f'{cid}.current-text-checkpoint.json', candidate_id=cid,
        srt_path=recut.subtitle_path, authority_path=authority_path, context_path=context_path,
        padded=padded, source_clock=clock)


def _apply_exact_caption_display_canon(*, recut, chat_authority_audit, chat_authority_path):
    """Use the existing display owner and typed display-successor contract.

    This never supplies an acoustic repair or release PASS. Original CPA rows
    and literal input are retained; the resulting actual bytes enter a fresh
    normal exact review. Authority is installed before the active subtitle.
    """
    from src.autoslice.surface_canon import normalize_caption_display_surfaces
    from src.autoslice.exact_final_cpa_history import with_caption_successor
    before = recut.subtitle_path.read_bytes().decode("utf-8", errors="strict")
    after, display = normalize_caption_display_surfaces(before)
    if before == after:
        return False
    previous = chat_authority_audit.get("exact_final_cpa_self_heal")
    staged = deepcopy(chat_authority_audit)
    if isinstance(previous, Mapping):
        _validate_exact_final_cpa_self_heal(chat_authority_audit,
            expected_srt_sha256="sha256:" + hashlib.sha256(before.encode()).hexdigest(),
            allow_pending=previous.get("status") == "REVIEW_PENDING")
        staged["exact_final_cpa_self_heal"] = with_caption_successor(previous, before, after)
    staged["current_caption_display_surface_audit"] = {
        **display, "review_scope": "CAPTION_DISPLAY_CANON_ONLY",
        "input_srt_text": before, "release_authorized": False,
    }
    staged["final_output_srt_sha256"] = hashlib.sha256(after.encode()).hexdigest()
    paths = [chat_authority_path, recut.subtitle_path]
    baseline = deepcopy(recut.redelivery_baseline_audit)
    if recut.redelivery_baseline_audit_path is not None:
        paths.append(recut.redelivery_baseline_audit_path)
    original = _snapshot_file_bytes(paths)
    try:
        _persist_review_document(chat_authority_path, staged)
        if baseline is not None:
            baseline["post_exact_final_cpa_output_sha256"] = staged["final_output_srt_sha256"]
            baseline["current_caption_display_surface_audit"] = staged["current_caption_display_surface_audit"]
            if staged.get("exact_final_cpa_self_heal"):
                baseline["exact_final_cpa_self_heal"] = staged["exact_final_cpa_self_heal"]
            if recut.redelivery_baseline_audit_path is not None:
                _persist_review_document(recut.redelivery_baseline_audit_path, baseline)
        _write_bytes_atomic(recut.subtitle_path, after.encode())
    except Exception:
        _restore_file_bytes(original)
        raise
    chat_authority_audit.clear()
    chat_authority_audit.update(staged)
    if baseline is not None:
        recut.redelivery_baseline_audit.clear()
        recut.redelivery_baseline_audit.update(baseline)
    return True


def _run_exact_final_review_gate(**kwargs) -> dict[str, object]:
    failed = True
    try:
        _apply_exact_caption_display_canon(recut=kwargs["recut"],
            chat_authority_audit=kwargs["chat_authority_audit"],
            chat_authority_path=kwargs["chat_authority_path"])
        result = _run_exact_final_review_gate_body(**kwargs)
        failed = False
        return result
    finally:
        if (kwargs['chat_authority_audit'].get('human_sequence_alignment') or {}).get('records'):
            try:
                _persist_human_sequence_current_checkpoint(kwargs)
            except (ValueError, RuntimeError, OSError):
                if not failed:
                    raise


def _run_exact_final_review_gate_body(
    *,
    cid: str,
    out_root: Path,
    final_start: int,
    final_end: int,
    recut: FinalRecutArtifacts | CurrentTextReviewArtifacts,
    chat_authority_audit: dict,
    chat_authority_path: Path,
    adapters: ProducerFinalizationAdapters | None = None,
    run_exact_final_review: Callable[..., dict] | None = None,
    text_recovery_only: bool = False,
    clip_context: Mapping[str, object] | None = None,
    before_review: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Review and bind the actual post-boundary, post-authority SRT bytes."""

    # Text adjudication cannot repair an immutable, overlapping cue grid.
    # Catch mechanical timing failures before spending any provider calls.
    timing_errors = [row for row in validate_srt_text(
        recut.subtitle_path.read_text(encoding="utf-8")
    )["errors"] if row["code"] in {
        "SRT_TIMING_INVALID", "SRT_TIMESTAMP_INVALID", "SRT_DURATION_NON_POSITIVE",
        "SRT_START_ORDER_INVALID", "SRT_CUE_OVERLAP",
    }]
    if timing_errors:
        raise SystemExit("FINAL_REVIEW_SRT_TIMING_INVALID:" + json.dumps(timing_errors, sort_keys=True))
    reviewer = run_exact_final_review or (adapters.run_exact_final_review if adapters else None)
    if reviewer is None:
        raise SystemExit("FINAL_REVIEW_EXACT_FINALIZER_MISSING")
    if os.environ.get("AUTOSLICE_FINAL_REVIEW_MODE") == "per_finding":
        from src.autoslice.finding_closure_shell import (
            PackageCall, PerFindingReviewer, run_package_closure,
        )
        if not isinstance(reviewer, PerFindingReviewer):
            raise SystemExit("FINDING_CLOSURE_TYPED_REVIEWER_REQUIRED")
        review_offset = final_start
        if text_recovery_only and isinstance(recut, CurrentTextReviewArtifacts):
            current_cues = [c for c in parse_srt_cues(recut.subtitle_path.read_text(encoding="utf-8")) if c.text.strip()]
            if not current_cues:
                raise SystemExit("CURRENT_TEXT_SOURCE_CLOCK_EMPTY")
            review_offset = final_start - current_cues[0].start_ms
            if review_offset < 0 or current_cues[-1].end_ms + review_offset != final_end:
                raise SystemExit("CURRENT_TEXT_SOURCE_CLOCK_INTERVAL_DRIFT")
        return run_package_closure(PackageCall(reviewer, cid, out_root, recut.subtitle_path,
            chat_authority_audit, chat_authority_path, review_offset, final_end,
            recut.redelivery_baseline_audit, recut.redelivery_baseline_audit_path, before_review,
            text_recovery_only=text_recovery_only))
    self_heal_passes: list[dict[str, object]] = []
    previous_self_heal = chat_authority_audit.get("exact_final_cpa_self_heal")
    prior_self_heal_runs = retained_runs(previous_self_heal)
    if isinstance(previous_self_heal, Mapping) and previous_self_heal.get("status") == "REVIEW_PENDING":
        try:
            _validate_exact_final_cpa_self_heal(
                chat_authority_audit,
                expected_srt_sha256=sha256_ref(_evidence_file_sha256(recut.subtitle_path)),
                allow_pending=True,
            )
        except FinalReviewContractError as exc:
            if exc.reason_code != CHAIN_TERMINAL_MISMATCH:
                raise
            if not _has_source_extension_boundary(cid=cid, out_root=out_root):
                raise
            previous_self_heal = _resume_source_extended_review(
                cid=cid, out_root=out_root, final_text=recut.subtitle_path.read_bytes().decode("utf-8", errors="strict"),
                chat_authority_audit=chat_authority_audit,
            )
            chat_authority_audit["exact_final_cpa_self_heal"] = previous_self_heal
        self_heal_passes = deepcopy(previous_self_heal["passes"])
    # Successor indices belong to their original repair run. Completed runs
    # already retain their full display/source receipts in history; copying
    # those indices into a new run makes its next review reject valid repairs.
    resumed_self_heal = (
        previous_self_heal
        if isinstance(previous_self_heal, Mapping)
        and previous_self_heal.get("status") == "REVIEW_PENDING"
        else {}
    )
    resumed_successors = successor_payload(resumed_self_heal)
    unreadable_drop_passes: list[dict[str, object]] = []
    carryover_file = carryover_path(out_root, cid)
    replayable_carryover_base_sha256: set[str] = set()
    consumed_carryover_repairs: dict[str, Mapping[str, object]] = {
        str(repair["before_sha256"]).removeprefix("sha256:"): repair
        for row in self_heal_passes for repair in row["repairs"]
    }
    # CPA remains the controller while distinct, authorized repairs progress.
    # Stop unchanged/cyclic text instead of cutting off at an arbitrary count.
    reviewed_hashes: set[str] = {
        str(row[key]).removeprefix("sha256:")
        for row in self_heal_passes for key in ("input_srt_sha256", "output_srt_sha256")
    }
    # Only a memo embedded in an applied repair is consumed.  A memo merely
    # present in chat authority may be the valid response that a previous
    # attempt persisted before its byte-novelty gate rejected the repair.
    prior_convergence_memo_ids: set[str] = set()
    for pass_receipt in self_heal_passes:
        repairs = (
            pass_receipt.get("repairs")
            if isinstance(pass_receipt, Mapping)
            else None
        )
        for repair in repairs if isinstance(repairs, list) else []:
            if not isinstance(repair, Mapping):
                continue
            for memo_key in (
                "exact_final_cpa_cycle_memo",
                "exact_final_cpa_convergence_memo",
            ):
                memo = repair.get(memo_key)
                if isinstance(memo, Mapping):
                    memo_id = exact_final_convergence_memo_identity(memo)
                    if memo_id is not None:
                        prior_convergence_memo_ids.add(memo_id)
                    break
    review_audit_path = out_root / f"{cid}.review-flags.json"
    resumed_pass_count = len(self_heal_passes)
    pass_index = resumed_pass_count - 1
    from src.autoslice.exact_final_terminal_funnel import (
        capture_terminal_adapters, review_pass_receipt, terminalize_review, applied_pass_receipt,
        unresolved_review_rows,
    )
    terminal_pass_receipts = [applied_pass_receipt(row) for row in self_heal_passes]
    while True:
        pass_index += 1
        if _apply_exact_caption_display_canon(recut=recut,
                chat_authority_audit=chat_authority_audit, chat_authority_path=chat_authority_path):
            resumed_successors = successor_payload(chat_authority_audit.get("exact_final_cpa_self_heal") or {})
        chat_authority_before_pass = deepcopy(chat_authority_audit)
        baseline_before_pass = (
            deepcopy(recut.redelivery_baseline_audit)
            if recut.redelivery_baseline_audit is not None
            else None
        )
        transactional_paths = [
            recut.subtitle_path,
            chat_authority_path,
            review_audit_path,
        ]
        if recut.redelivery_baseline_audit_path is not None:
            transactional_paths.append(
                recut.redelivery_baseline_audit_path
            )
        file_bytes_before_pass = _snapshot_file_bytes(
            transactional_paths
        )
        final_bytes = recut.subtitle_path.read_bytes()
        final_text = final_bytes.decode("utf-8", errors="strict")
        review_timeline_offset_ms = final_start
        if text_recovery_only and isinstance(recut, CurrentTextReviewArtifacts):
            # Current text retains its delivery grid, including a leading
            # subtitle gap. final_start is the first semantic source cue,
            # whereas the reviewer needs the origin of the delivery clock.
            # Adding that gap twice moves every acoustic window forward.
            current_cues = [cue for cue in parse_srt_cues(final_text) if cue.text.strip()]
            if not current_cues:
                raise SystemExit("CURRENT_TEXT_SOURCE_CLOCK_EMPTY")
            review_timeline_offset_ms = final_start - current_cues[0].start_ms
            if (
                review_timeline_offset_ms < 0
                or current_cues[-1].end_ms + review_timeline_offset_ms != final_end
            ):
                raise SystemExit("CURRENT_TEXT_SOURCE_CLOCK_INTERVAL_DRIFT")
        reviewed_hashes.add(hashlib.sha256(final_bytes).hexdigest())
        if before_review is not None:
            before_review()
            if recut.subtitle_path.read_bytes() != final_bytes:
                raise SystemExit("ACOUSTIC_ROLE_REFRESH_MUTATED_SUBTITLES")
        with capture_terminal_adapters() as terminal_adapters:
            audit = reviewer(
                final_text,
                chat_authority_audit,
                review_timeline_offset_ms,
                final_end,
            )
        from src.autoslice.span_finding_obligations import enforce_review as enforce_span_review
        with producer_stage("exact_final_review.postprocess.span_obligations"):
            audit = enforce_span_review(final_text, chat_authority_audit, audit,
                                        timeline_offset_ms=review_timeline_offset_ms)
        if "span_obligation_review" in audit:
            chat_authority_audit["span_obligation_closures"] = deepcopy(audit["span_obligation_review"]["closures"])
            chat_authority_audit["span_unapplied_questions"] = deepcopy(audit["span_obligation_review"]["span_unapplied_questions"])
        if text_recovery_only and (
            audit.get("release_gate") != "BLOCK"
            or audit.get("boundary_semantic_review", {}).get("status") != "BLOCK"
            or audit.get("boundary_semantic_review", {}).get("reason_codes")
            != ["CURRENT_TEXT_REQUIRES_SOURCE_REVIEW"]
        ):
            raise SystemExit("CURRENT_TEXT_RECOVERY_SCOPE_INVALID")
        # A fresh ordinary exact review replaces its own authority only.
        # Preserve the separate postburn receipt so completed raw responses
        # remain available to the normal same-input parser/cache consumer.
        # Its BLOCK never becomes a CLEAN postburn verdict by inheritance.
        previous_postburn = chat_authority_before_pass.get(
            "postburn_subtitle_audio_review"
        )
        if isinstance(previous_postburn, Mapping):
            audit["postburn_subtitle_audio_review"] = deepcopy(previous_postburn)
        with producer_stage("exact_final_review.postprocess.convergence"):
            convergence_memos = collect_exact_final_convergence_memos(audit)
        if convergence_memos:
            existing_memos = chat_authority_audit.get(
                "exact_final_cpa_convergence_memos"
            )
            by_window = {
                (
                    int(row["matched_start_ms"]),
                    int(row["matched_end_ms"]),
                ): dict(row)
                for row in (
                    existing_memos
                    if isinstance(existing_memos, list)
                    else []
                )
                if isinstance(row, Mapping)
                and isinstance(row.get("matched_start_ms"), int)
                and not isinstance(row.get("matched_start_ms"), bool)
                and isinstance(row.get("matched_end_ms"), int)
                and not isinstance(row.get("matched_end_ms"), bool)
            }
            for row in convergence_memos:
                by_window[
                    (
                        int(row["matched_start_ms"]),
                        int(row["matched_end_ms"]),
                    )
                ] = row
            chat_authority_audit[
                "exact_final_cpa_convergence_memos"
            ] = list(by_window.values())
        _annotate_consumed_correction_carryovers(
            audit,
            consumed_carryover_repairs,
        )
        if pass_index == resumed_pass_count:
            replayable = _replayable_exact_final_carryover_findings(
                final_text, carryover_file, clip_context=clip_context,
                review_timeline_offset_ms=review_timeline_offset_ms,
            )
            replayable_carryover_base_sha256.update(
                str(row.get("base_text_sha256") or "")
                for row in replayable
            )
            audit = _overlay_exact_carryover_findings(audit, replayable)
        suppress_baseline_owned_self_heal_findings(
            final_text, audit, recut.redelivery_baseline_audit
        )
        # 主包/主播案：danmaku 逐字读音一旦被 chat authority 判定并
        # 落笔，同一窗口不得再被本轮 self-heal 当成普通 ASR 错字重判。
        suppress_chat_authority_owned_self_heal_findings(
            final_text, audit, chat_authority_before_pass, review_timeline_offset_ms
        )
        chat_authority_audit["final_review_audit"] = audit
        if isinstance(audit.get("cpa_adjudication_usage"), Mapping):
            prior_usage=chat_authority_audit.get('cpa_adjudication_usage') or {}
            prior_count=prior_usage.get('provider_adjudication_count')
            reviewed_count=audit['cpa_adjudication_usage'].get('provider_adjudication_count')
            if type(prior_count) is int and type(reviewed_count) is int and prior_count>reviewed_count:
                # Replayed content is unchanged; its historical accounting
                # cannot replace later cumulative calls already on this input.
                audit['cpa_adjudication_usage']=deepcopy(prior_usage)
            chat_authority_audit["cpa_adjudication_usage"] = dict(
                audit["cpa_adjudication_usage"]
            )
        audit["cpa_review_resource_pressure"] = {
            "review_pass_count": pass_index + 1,
            "soft_repair_pass_target": EXACT_FINAL_CPA_SELF_HEAL_MAX_REPAIR_PASSES,
            "policy": "SOFT_PROGRESS_REQUIRED",
        }
        with producer_stage("exact_final_review.postprocess.persist"):
            persist_review_audit(review_audit_path, audit)
        expected_srt_sha256 = "sha256:" + hashlib.sha256(
            final_bytes
        ).hexdigest()
        from src.autoslice.human_sequence_alignment import protected_indexes as human_protected_indexes
        protected = frozenset(human_protected_indexes(
            chat_authority_audit.get('human_sequence_alignment') or {}, final_text,
            timeline_offset_ms=final_start))
        with producer_stage("exact_final_review.postprocess.repairs"):
            repaired_text, repairs = _apply_exact_final_cpa_repairs(
                final_text, audit, clip_context=clip_context, protected_cue_indexes=protected)
        proposed_sha256 = hashlib.sha256(repaired_text.encode('utf-8')).hexdigest()
        returning_memos = (_matching_cycle_return_memos(repairs, convergence_memos,
            prior_memo_ids=prior_convergence_memo_ids, input_srt_sha256=expected_srt_sha256)
            if repairs and proposed_sha256 in reviewed_hashes else [])
        ordinary_progress = bool(repairs and (_new_repair_rulings(repairs, self_heal_passes)
            or (returning_memos and len(returning_memos) == len(repairs)))
            and (proposed_sha256 not in reviewed_hashes
            or (returning_memos and len(returning_memos) == len(repairs))))
        with producer_stage("exact_final_review.postprocess.pass_receipt"):
            terminal_pass_receipts.append(review_pass_receipt(pass_index=pass_index + 1,
                srt_text=final_text, audit=audit,
                repaired_text=repaired_text if ordinary_progress else final_text,
                repairs=repairs if ordinary_progress else []))
        if not ordinary_progress and unresolved_review_rows(audit):
            with producer_stage("exact_final_review.postprocess.terminal_funnel"):
                audit = terminalize_review(final_text, audit, pass_receipts=terminal_pass_receipts,
                    adapters=terminal_adapters)
            # Terminal B keeps are new adjudications of the same outstanding
            # span questions. Feed them back to the ordinary closure consumer
            # before G; retaining the pre-terminal proof leaves those actual
            # obligations pending despite the completed full-CURRENT ruling.
            audit = enforce_span_review(final_text, chat_authority_audit, audit,
                                        timeline_offset_ms=review_timeline_offset_ms)
            if 'span_obligation_review' in audit:
                chat_authority_audit['span_obligation_closures'] = deepcopy(audit['span_obligation_review']['closures'])
                chat_authority_audit['span_unapplied_questions'] = deepcopy(audit['span_obligation_review']['span_unapplied_questions'])
            chat_authority_audit['final_review_audit'] = audit
            if isinstance(audit.get('cpa_adjudication_usage'), Mapping):
                chat_authority_audit['cpa_adjudication_usage'] = deepcopy(audit['cpa_adjudication_usage'])
            persist_review_audit(review_audit_path, audit)
            repaired_text, repairs = _apply_exact_final_cpa_repairs(
                final_text, audit, clip_context=clip_context, protected_cue_indexes=protected)
            if repairs:
                terminal_pass_receipts[-1] = review_pass_receipt(pass_index=pass_index + 1,
                    srt_text=final_text, audit=audit, repaired_text=repaired_text, repairs=repairs)
        try:
            validate_final_review_release(
                audit,
                expected_srt_sha256=expected_srt_sha256,
            )
        except FinalReviewContractError as exc:
            # A pre-boundary continuation may complete text adjudication
            # while source closure is still pending.  This is the same full
            # ordinary text consumer; its BLOCK remains a release BLOCK.
            if text_recovery_only and exc.reason_code == "FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED":
                audit["pre_boundary_text_recovery"] = {
                    "status": "TEXT_COMPLETE_BOUNDARY_PENDING",
                    "reviewed_srt_sha256": expected_srt_sha256,
                    "release_gate": "BLOCK",
                }
                # The ordinary release validator has accepted the reviewed
                # text chain; only source closure remains blocked. Bind its
                # persisted authority to these actual bytes before the caller
                # snapshots it, including registered display normalization.
                chat_authority_audit["final_output_srt_sha256"] = (
                    expected_srt_sha256.removeprefix("sha256:")
                )
                if self_heal_passes:
                    pending = build_run_audit(self_heal_passes, prior_self_heal_runs,
                                              successors=resumed_successors)
                    chat_authority_audit["exact_final_cpa_self_heal"] = pending
                    audit["exact_final_cpa_self_heal"] = pending
                persist_final_review_carryover(carryover_file, audit)
                persist_review_audit(review_audit_path, audit)
                _persist_review_document(chat_authority_path, chat_authority_audit)
                return audit
            repaired_base_sha256 = {
                str(repair.get("before_sha256") or "").removeprefix(
                    "sha256:"
                )
                for repair in repairs
            }
            unconsumed_carryover_sha256 = (
                _unconsumed_correction_carryover_base_sha256(audit)
            )
            exact_replay_closes_unconsumed_carryover = bool(
                exc.reason_code == "FINAL_REVIEW_CARRYOVER_UNCONSUMED"
                and unconsumed_carryover_sha256
                and unconsumed_carryover_sha256.issubset(
                    repaired_base_sha256
                )
            )
            repaired_sha256 = hashlib.sha256(
                repaired_text.encode("utf-8")
            ).hexdigest()
            cycle_return_memos: list[
                tuple[dict[str, object], dict[str, object], str]
            ] = []
            cycle_return_progress = False
            if repairs and repaired_sha256 in reviewed_hashes:
                cycle_return_memos = _matching_cycle_return_memos(
                    repairs,
                    convergence_memos,
                    prior_memo_ids=prior_convergence_memo_ids,
                    input_srt_sha256=expected_srt_sha256,
                )
                cycle_return_progress = bool(
                    cycle_return_memos
                    and len(cycle_return_memos) == len(repairs)
                )
                if cycle_return_progress:
                    for repair, memo, _memo_id in cycle_return_memos:
                        memo_key = (
                            "exact_final_cpa_cycle_memo"
                            if memo.get("schema_version")
                            == "exact-final-cpa-cycle-memo.v1"
                            else "exact_final_cpa_convergence_memo"
                        )
                        repair[memo_key] = deepcopy(memo)
            repair_progress = bool(
                repairs
                and (_new_repair_rulings(repairs, self_heal_passes) or cycle_return_progress)
                and (
                    repaired_sha256 not in reviewed_hashes
                    or cycle_return_progress
                )
            )
            if repairs and not repair_progress:
                audit["cpa_progress_stop_reason"] = (
                    "UNCHANGED_OR_CYCLIC_FINAL_SRT"
                    if repaired_sha256 in reviewed_hashes and not cycle_return_progress
                    else "NO_NEW_SUBSTANTIVE_MATERIAL_OR_LEGAL_DECISION")
                persist_review_audit(review_audit_path, audit)
            if (
                (
                    is_finding_failure(exc.reason_code)
                    or exact_replay_closes_unconsumed_carryover
                )
                and repair_progress
            ):
                staged_chat_authority = deepcopy(
                    chat_authority_audit
                )
                staged_baseline = (
                    deepcopy(recut.redelivery_baseline_audit)
                    if recut.redelivery_baseline_audit is not None
                    else None
                )
                staged_consumed = dict(consumed_carryover_repairs)
                repaired_sha256 = hashlib.sha256(
                    repaired_text.encode("utf-8")
                ).hexdigest()
                pass_receipt = {
                    "schema_version": "exact-final-cpa-self-heal-pass.v1",
                    "pass_index": pass_index + 1,
                    "input_srt_sha256": expected_srt_sha256,
                    "output_srt_sha256": "sha256:" + repaired_sha256,
                    "repairs": repairs,
                }
                next_self_heal_passes = [
                    *self_heal_passes,
                    pass_receipt,
                ]
                pending_self_heal_audit = build_run_audit(
                    next_self_heal_passes, prior_self_heal_runs,
                    successors=resumed_successors,
                )
                try:
                    existing_memos = staged_chat_authority.get(
                        "exact_final_cpa_convergence_memos"
                    )
                    if isinstance(existing_memos, list):
                        staged_chat_authority[
                            "exact_final_cpa_convergence_memos"
                        ] = rebind_exact_final_convergence_memos(
                            repaired_text,
                            existing_memos,
                        )
                    # Complete the authority ledger before exposing any new
                    # active SRT bytes or sidecar state.
                    _register_exact_final_cpa_repairs(
                        staged_chat_authority,
                        repairs,
                        delivery_start_ms=review_timeline_offset_ms,
                    )
                    for repair in repairs:
                        before_sha256 = str(
                            repair.get("before_sha256") or ""
                        ).removeprefix("sha256:")
                        if (
                            before_sha256
                            in replayable_carryover_base_sha256
                        ):
                            staged_consumed[before_sha256] = repair
                    staged_chat_authority[
                        "exact_final_cpa_self_heal"
                    ] = pending_self_heal_audit
                    staged_chat_authority[
                        "final_output_srt_sha256"
                    ] = repaired_sha256
                    if staged_baseline is not None:
                        staged_baseline[
                            "post_exact_final_cpa_output_sha256"
                        ] = repaired_sha256
                        staged_baseline[
                            "exact_final_cpa_self_heal"
                        ] = pending_self_heal_audit

                    # Both sidecars use the same content-addressed publisher
                    # as ordinary review checkpoints. Blobs are published
                    # before the document; rollback restores exact old bytes.
                    _persist_review_document(review_audit_path, audit)
                    _persist_review_document(chat_authority_path, staged_chat_authority)
                    if (
                        staged_baseline is not None
                        and recut.redelivery_baseline_audit_path
                        is not None
                    ):
                        _persist_review_document(
                            recut.redelivery_baseline_audit_path, staged_baseline)
                    # Install the active SRT last, after its complete ledger
                    # and sidecars are ready. Any exception rolls all paths
                    # back to the exact pre-pass bytes below.
                    _write_bytes_atomic(recut.subtitle_path, repaired_text.encode("utf-8"))

                    chat_authority_audit.clear()
                    chat_authority_audit.update(staged_chat_authority)
                    if (
                        recut.redelivery_baseline_audit is not None
                        and staged_baseline is not None
                    ):
                        recut.redelivery_baseline_audit.clear()
                        recut.redelivery_baseline_audit.update(
                            staged_baseline
                        )
                    self_heal_passes = next_self_heal_passes
                    consumed_carryover_repairs = staged_consumed
                    prior_convergence_memo_ids.update(
                        memo_id for _repair, _memo, memo_id in cycle_return_memos
                    )
                except Exception:
                    _restore_file_bytes(file_bytes_before_pass)
                    chat_authority_audit.clear()
                    chat_authority_audit.update(
                        chat_authority_before_pass
                    )
                    if (
                        recut.redelivery_baseline_audit is not None
                        and baseline_before_pass is not None
                    ):
                        recut.redelivery_baseline_audit.clear()
                        recut.redelivery_baseline_audit.update(
                            baseline_before_pass
                        )
                    raise
                continue




            next_drop_passes = None if text_recovery_only else stage_unreadable_cue_drop_pass(
                reason_code=exc.reason_code,
                cpa_repairs=repairs,
                pass_budget_left=not bool(repairs),
                final_text=final_text,
                audit=audit,
                expected_srt_sha256=expected_srt_sha256,
                passes=unreadable_drop_passes,
                recut=recut,
                chat_authority_audit=chat_authority_audit,
                chat_authority_path=chat_authority_path,
                review_audit_path=review_audit_path,
                snapshots=(
                    file_bytes_before_pass, chat_authority_before_pass,
                    baseline_before_pass,
                ),
            )
            if next_drop_passes is not None:
                unreadable_drop_passes = next_drop_passes
                continue
            # 终审结转仍是无法在当前 exact-final pass 安全落盘时的后备。
            carryover_count = persist_final_review_carryover(
                carryover_file, audit
            )
            if carryover_count:
                audit["carryover_persisted_count"] = carryover_count
            _persist_review_document(chat_authority_path, chat_authority_audit)
            raise SystemExit(
                f"FINAL_REVIEW_RELEASE_BLOCKED: {exc.reason_code}: "
                f"{chat_authority_path}"
            ) from exc
        # A clean pass clears any already-consumed carryover and binds the
        # complete same-run self-heal history to the final authority receipt.
        persist_final_review_carryover(
            carryover_file, audit
        )
        if self_heal_passes:
            self_heal_audit = build_run_audit(
                self_heal_passes, prior_self_heal_runs, final_srt_sha256=expected_srt_sha256,
                successors=resumed_successors,
            )
            chat_authority_audit["exact_final_cpa_self_heal"] = (
                self_heal_audit
            )
            audit["exact_final_cpa_self_heal"] = self_heal_audit
            validate_final_review_release(
                audit,
                expected_srt_sha256=expected_srt_sha256,
            )
            persist_review_audit(
                out_root / f"{cid}.review-flags.json", audit
            )
            if recut.redelivery_baseline_audit is not None:
                recut.redelivery_baseline_audit[
                    "post_exact_final_cpa_output_sha256"
                ] = expected_srt_sha256.removeprefix("sha256:")
                recut.redelivery_baseline_audit[
                    "exact_final_cpa_self_heal"
                ] = self_heal_audit
                if recut.redelivery_baseline_audit_path is not None:
                    recut.redelivery_baseline_audit_path.write_text(
                        json.dumps(
                            recut.redelivery_baseline_audit,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
        if unreadable_drop_passes:
            seal_unreadable_cue_drops(
                cid=cid,
                passes=unreadable_drop_passes,
                expected_srt_sha256=expected_srt_sha256,
                audit=audit,
                chat_authority_audit=chat_authority_audit,
                recut=recut,
                review_audit_path=review_audit_path,
            )
        _persist_review_document(chat_authority_path, chat_authority_audit)
        return audit

def _finalize_speaker(
    *,
    options: ProducerFinalizationOptions,
    spec: dict,
    host: str,
    cid: str,
    recut: FinalRecutArtifacts,
    final_start: int,
    final_end: int,
    adapters: ProducerFinalizationAdapters,
) -> SpeakerArtifacts:
    recut_dir = recut.recut_dir
    media_path = recut.media_path
    subtitle_path = recut.subtitle_path
    speaker_manifest: dict | None = None
    speaker_review_srt: Path | None = None
    speaker_ass: Path | None = None
    speaker_manifest_path: Path | None = None
    if options.speaker_mode in {"required", "auto"}:
        speaker_review_srt = media_path.with_suffix(".speaker-final.srt")
        speaker_ass = media_path.with_suffix(".speaker-final.ass")
        speaker_manifest_path = media_path.with_suffix(".speaker-final.json")
        speaker_override_path = options.speaker_overrides or _resolved_optional_path(
            spec.get("speaker_overrides"), relative_to=options.spec.parent
        )
        source_session_anchor_path = (
            options.speaker_source_session_anchors
            or _resolved_optional_path(
                spec.get("speaker_source_session_anchors"), relative_to=options.spec.parent
            )
        )
        mixed_overlap_evidence_path = (
            options.speaker_mixed_overlap_evidence
            or _resolved_optional_path(
                spec.get("speaker_mixed_overlap_evidence"),
                relative_to=options.spec.parent,
            )
        )
        speaker_session_context_path = _resolved_optional_path(
            spec.get("speaker_session_context"), relative_to=options.spec.parent
        )
        if not (  # downstream binds every current piece to this context source
            speaker_session_context_path
            and speaker_session_context_path.is_file()
            and not speaker_session_context_path.is_symlink()
        ):
            speaker_session_context_path = None
        speaker_manifest = adapters.run_speaker_finalization(
            speaker_mode=options.speaker_mode,
            host=host,
            candidate_id=cid,
            media_path=media_path,
            text_srt_path=subtitle_path,
            output_srt_path=speaker_review_srt,
            output_ass_path=speaker_ass,
            output_manifest_path=speaker_manifest_path,
            work_dir=recut_dir / f"{cid}.speaker-work",
            override_path=speaker_override_path,
            source_session_anchor_path=source_session_anchor_path,
            mixed_overlap_evidence_path=mixed_overlap_evidence_path,
            speaker_session_context_path=speaker_session_context_path,
            speaker_python=options.speaker_python,
            spec=spec,
            spec_parent=options.spec.parent,
            final_source_start_ms=(
                int(spec["pieces"][0]["start_ms"]) + final_start
                if len(spec.get("pieces") or []) == 1
                else None
            ),
            final_source_end_ms=(
                int(spec["pieces"][0]["start_ms"]) + final_end
                if len(spec.get("pieces") or []) == 1
                else None
            ),
        )
    return SpeakerArtifacts(
        manifest=speaker_manifest,
        review_srt=speaker_review_srt,
        ass=speaker_ass,
        manifest_path=speaker_manifest_path,
    )

def _verify_final_authority(
    *,
    cid: str,
    final_start: int,
    final_end: int,
    recut: FinalRecutArtifacts,
    speaker: SpeakerArtifacts,
    chat_authority_audit: dict,
    chat_authority_path: Path,
    subtitle_regression_path: Path | None, operator_text_full_ownership: Mapping[str, object] | None = None,
) -> AuthorityArtifacts:
    subtitle_path = recut.subtitle_path
    text_manifest = recut.text_manifest
    recut_dir = recut.recut_dir
    speaker_review_srt = speaker.review_srt
    speaker_ass = speaker.ass
    speaker_manifest_path = speaker.manifest_path
    final_text = subtitle_path.read_text(encoding="utf-8", errors="replace")
    final_speaker_text = (
        speaker_review_srt.read_text(encoding="utf-8", errors="replace")
        if speaker_review_srt is not None and speaker_review_srt.is_file()
        else final_text
    )
    source_language_audit = chat_authority_audit.get(
        "final_source_language_preservation_audit"
    )
    if (
        isinstance(source_language_audit, dict)
        and source_language_audit.get("status")
        == "DEFERRED_TO_REDELIVERY_BASELINE"
    ):
        baseline_audit = recut.redelivery_baseline_audit or {}
        if not _resolve_deferred_foreign_introductions_after_redelivery(
            source_language_audit=source_language_audit,
            final_text=final_text,
            baseline_audit=baseline_audit,
            final_start=final_start,
        ):
            _persist_review_document(chat_authority_path, chat_authority_audit)
            raise SystemExit(
                "FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED_AFTER_REDELIVERY: "
                f"{chat_authority_path}"
            )
    pending_override_ok = reconcile_pending_text_overrides(
        chat_authority_audit,
        text_manifest,
        delivery_start_ms=final_start,
    )
    reconcile_reviewed_text_override_conflicts(
        chat_authority_audit,
        text_manifest,
        delivery_start_ms=final_start,
    )
    final_authority_ok = pending_override_ok and verify_chat_authority_final_surfaces(
        chat_authority_audit, final_text_srt=final_text,
        final_speaker_srt=final_speaker_text, delivery_start_ms=final_start,
        delivery_end_ms=final_end,
        operator_text_full_ownership=operator_text_full_ownership)
    human_alignment = chat_authority_audit.get('human_sequence_alignment') or {}
    if human_alignment.get('records'):
        from src.autoslice.human_sequence_alignment import verify_surfaces
        human_ok = verify_surfaces(human_alignment, text=final_text, speaker=final_speaker_text,
                                  delivery_start_ms=final_start, delivery_end_ms=final_end)
        human_alignment['final_surfaces_verified'] = human_ok
        final_authority_ok = final_authority_ok and human_ok
    # Final-owner verification annotates every reviewed-baseline mapping in
    # place.  Persist those post-verification bytes before the record hashes
    # and embeds the same object; otherwise the package contains a stale
    # sidecar while the record carries the newer owner receipts, and the exact
    # recovery manifest correctly refuses the mismatch.
    if recut.redelivery_baseline_audit is not None:
        if recut.redelivery_baseline_audit_path is None:
            raise SystemExit("REDELIVERY_BASELINE_AUDIT_PATH_MISSING")
        recut.redelivery_baseline_audit_path.write_text(
            json.dumps(
                recut.redelivery_baseline_audit,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    chat_authority_audit.update(
        {
            "final_status": "FINAL_ARTIFACTS_VERIFIED" if final_authority_ok else "FINAL_ARTIFACTS_FAILED",
            "final_text_srt_path": str(subtitle_path),
            "final_text_srt_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
            "final_speaker_srt_path": str(speaker_review_srt) if speaker_review_srt is not None else None,
            "final_speaker_srt_sha256": hashlib.sha256(final_speaker_text.encode("utf-8")).hexdigest(),
            "speaker_ass_path": str(speaker_ass) if speaker_ass is not None else None,
            "speaker_ass_sha256": _sha256(speaker_ass) if speaker_ass is not None else None,
            "speaker_manifest_sha256": _sha256(speaker_manifest_path) if speaker_manifest_path is not None else None,
        }
    )
    _persist_review_document(chat_authority_path, chat_authority_audit)
    if not final_authority_ok:
        raise SystemExit(f"CHAT_AUTHORITY_FINAL_ARTIFACT_FAILED: {chat_authority_path}")

    subtitle_regression_audit_path: Path | None = None
    subtitle_regression_audit: dict | None = None
    if subtitle_regression_path is not None:
        subtitle_regression_audit = verify_subtitle_regression_surfaces(
            subtitle_regression_path,
            candidate_id=cid,
            final_text_srt=final_text,
            final_speaker_srt=final_speaker_text,
        )
        subtitle_regression_audit_path = recut_dir / f"{cid}.subtitle-regression.json"
        subtitle_regression_audit_path.write_text(
            json.dumps(subtitle_regression_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if subtitle_regression_audit["status"] != "PASS":
            raise SystemExit(
                f"SUBTITLE_REGRESSION_FAILED: {subtitle_regression_audit_path}"
            )
    return AuthorityArtifacts(
        subtitle_regression_audit_path=subtitle_regression_audit_path,
        subtitle_regression_audit=subtitle_regression_audit,
    )

def _branding_burn_revision_inputs(
    branding_intro: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if branding_intro is None:
        return None
    return {
        "intro_id": branding_intro.get("intro_id"),
        "media_sha256": branding_intro.get("media_sha256"),
        "manifest_sha256": branding_intro.get("manifest_sha256"),
    }


def _derive_burn_revision(
    record: Mapping[str, object],
    *,
    branding_intro: Mapping[str, object] | None,
    render_revision_marker: str,
) -> str:
    """Derive the stable sibling namespace for a typed successor burn."""

    if not isinstance(render_revision_marker, str) or not render_revision_marker:
        raise ValueError("render_revision_marker must be a non-empty string")
    artifact_hashes = record.get("artifact_hashes")
    if not isinstance(artifact_hashes, Mapping):
        raise ValueError("burn revision requires record artifact hashes")
    revision_inputs = {
        "marker": render_revision_marker,
        "video_sha256": artifact_hashes.get("video_sha256"),
        "subtitle_sha256": artifact_hashes.get("subtitle_sha256"),
        "ass_sha256": artifact_hashes.get("ass_sha256"),
        "subtitle_style": record.get("subtitle_style"),
        "branding_intro": _branding_burn_revision_inputs(branding_intro),
    }
    for key in ("video_sha256", "subtitle_sha256"):
        value = revision_inputs[key]
        if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ValueError(f"burn revision requires a verified {key}")
    serialized = json.dumps(
        revision_inputs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "r" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _retained_burn_metadata(authority: Mapping[str, object], *, out_root: Path) -> tuple[object, dict | None]:
    """Read render metadata from an exact frozen input-recovery authority.

    No content verdict crosses this boundary. The render consumer still has
    to prove its input revision and output bytes before reusing a film.
    """
    if isinstance(authority.get("burn_binding"), Mapping):
        return authority["burn_binding"], None
    def read_bound(binding: Mapping, root: Path, *, review: bool = False) -> Mapping:
        path = Path(str(binding.get("path") or ""))
        digest = str(binding.get("sha256") or "").removeprefix("sha256:")
        if (path.is_symlink() or not path.resolve().is_relative_to(root.resolve())
                or not path.is_file() or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or _sha256(path) != digest):
            raise SystemExit("SOURCE_RENDER_PARENT_BINDING_DRIFT")
        try:
            raw = path.read_bytes()
            result = review_from_bytes(raw, directory=path.parent) if review else json.loads(raw)
        except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit("SOURCE_RENDER_PARENT_INVALID") from exc
        if not isinstance(result, Mapping):
            raise SystemExit("SOURCE_RENDER_PARENT_INVALID")
        return result

    # The completed observation is frozen before input restoration and binds
    # its full original authority, including the actual render. No review
    # verdict fields are consumed here.
    cache = out_root / f".{out_root.name}.postburn-cpa"
    pointer_path = cache / "observation.json"
    if pointer_path.exists():
        if pointer_path.is_symlink():
            raise SystemExit("SOURCE_RENDER_PARENT_BINDING_DRIFT")
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SystemExit("SOURCE_RENDER_PARENT_INVALID") from exc
        if (not isinstance(pointer, Mapping) or pointer.get("candidate_id") != out_root.name
                or pointer.get("release_authorized") is not False
                or not isinstance(pointer.get("source_authority"), Mapping)):
            raise SystemExit("SOURCE_RENDER_PARENT_INVALID")
        read_bound(pointer, cache)
        ancestor = read_bound(pointer["source_authority"], cache, review=True)
        if isinstance(ancestor.get("burn_binding"), Mapping):
            return ancestor["burn_binding"], {**pointer["source_authority"], "role": "RENDER_METADATA_ONLY"}
    parent = authority.get("source_input_usage_parent")
    if not isinstance(parent, Mapping):
        return None, None
    if parent.get("role") != "CUMULATIVE_USAGE_ONLY":
        raise SystemExit("SOURCE_RENDER_PARENT_BINDING_DRIFT")
    ancestor = read_bound(parent, out_root / "source-text-input", review=True)
    return ancestor.get("burn_binding"), {**dict(parent), "role": "RENDER_METADATA_ONLY"}


def _replay_bound_burn(record: dict, prior: object, *, branding_intro) -> dict | None:
    """Reuse exact prior render bytes only after replaying their input identity."""
    if not isinstance(prior, Mapping):
        return None
    expected_input = _derive_burn_revision(
        record, branding_intro=branding_intro, render_revision_marker="native-burn-input-v1",
    )
    legacy_revision = _derive_burn_revision(
        record, branding_intro=branding_intro, render_revision_marker="postburn-typed-srt-v1",
    )
    if (prior.get("input_revision") != expected_input
            and prior.get("burn_revision") != legacy_revision):
        return None
    preview = prior.get("burned_preview")
    if (not isinstance(preview, Mapping) or preview.get("status") != "BURNED"
            or preview.get("path") != prior.get("burned_media_path")
            or preview.get("ass_path") != prior.get("ass_path")
            or preview.get("burn_revision") != prior.get("burn_revision")):
        raise SystemExit("CURRENT_BURN_BINDING_INVALID")
    for path_key, hash_key in (("burned_media_path", "burned_media_sha256"), ("ass_path", "ass_sha256")):
        path = Path(str(prior.get(path_key) or ""))
        expected = str(prior.get(hash_key) or "").removeprefix("sha256:")
        if (not re.fullmatch(r"[0-9a-f]{64}", expected) or path.is_symlink()
                or not path.is_file() or _sha256(path) != expected):
            raise SystemExit("CURRENT_BURN_BINDING_ARTIFACT_DRIFT")
    result = {**record, "burned_preview": deepcopy(dict(preview)),
              "subtitle_ass_path": prior["ass_path"],
              "burn_recovery": {"status": "REUSED_EXACT_INPUTS", "input_revision": expected_input}}
    if prior.get("burn_revision") is not None:
        result["burn_revision"] = prior["burn_revision"]
    result["artifact_hashes"] = {
        **record["artifact_hashes"],
        "burned_video_sha256": "sha256:" + str(prior["burned_media_sha256"]).removeprefix("sha256:"),
        "ass_sha256": "sha256:" + str(prior["ass_sha256"]).removeprefix("sha256:"),
    }
    return result


def _build_and_burn_record(
    *,
    options: ProducerFinalizationOptions,
    profile_id: str,
    speaker_subtitle_style_id: str,
    final_start: int,
    final_end: int,
    recut: FinalRecutArtifacts,
    speaker: SpeakerArtifacts,
    authority: AuthorityArtifacts,
    chat_authority_audit: dict,
    chat_authority_path: Path,
    timing_qa: dict,
    audit: dict,
    branding_intro: dict[str, object] | None,
    adapters: ProducerFinalizationAdapters,
    delivery_lane: str = "talk",
    clip_profile: ClipProfile | None = None,
    talk_filler_audit_path: Path | None = None,
    candidate_id: str | None = None,
    render_revision_marker: str | None = None,
) -> dict:
    _require_delivery_branding_intro(
        delivery_lane=delivery_lane,
        branding_intro=branding_intro,
        clip_profile=clip_profile,
    )
    media_path = recut.media_path
    subtitle_path = recut.subtitle_path
    text_manifest_path = recut.text_manifest_path
    redelivery_baseline_audit_path = recut.redelivery_baseline_audit_path
    redelivery_baseline_audit = recut.redelivery_baseline_audit
    speaker_manifest = speaker.manifest
    speaker_review_srt = speaker.review_srt
    speaker_ass = speaker.ass
    speaker_manifest_path = speaker.manifest_path
    subtitle_regression_audit_path = authority.subtitle_regression_audit_path
    subtitle_regression_audit = authority.subtitle_regression_audit
    record: dict = {
        "status": "MATERIALIZED",
        "classification": delivery_lane,
        "media_path": str(media_path),
        "subtitle_path": str(subtitle_path),
        "subtitle_source": (
            f"{options.substrate}+{options.correct}+pronoun+text_final"
            + ("+redelivery_baseline" if redelivery_baseline_audit is not None else "")
        ),
        "start_ms": 0,
        "end_ms": final_end - final_start,
        "duration_ms": final_end - final_start,
        "artifact_hashes": {
            "video_sha256": "sha256:" + _sha256(media_path),
            "subtitle_sha256": "sha256:" + _sha256(subtitle_path),
            **({"ass_sha256": "sha256:" + _sha256(speaker_ass)} if speaker_ass is not None else {}),
            **({"speaker_review_srt_sha256": "sha256:" + _sha256(speaker_review_srt)} if speaker_review_srt is not None else {}),
            "chat_authority_audit_sha256": "sha256:" + _sha256(chat_authority_path),
            **(
                {
                    "redelivery_baseline_audit_sha256": "sha256:"
                    + _sha256(redelivery_baseline_audit_path)
                }
                if redelivery_baseline_audit_path is not None
                else {}
            ),
            **(
                {
                    "subtitle_regression_audit_sha256": "sha256:"
                    + _sha256(subtitle_regression_audit_path)
                }
                if subtitle_regression_audit_path is not None
                else {}
            ),
            **(
                {
                    "talk_filler_audit_sha256": "sha256:"
                    + _sha256(talk_filler_audit_path)
                }
                if talk_filler_audit_path is not None
                else {}
            ),
        },
        "chat_authority_audit_path": str(chat_authority_path),
        "redelivery_baseline_audit_path": (
            str(redelivery_baseline_audit_path)
            if redelivery_baseline_audit_path is not None
            else None
        ),
        "redelivery_baseline": redelivery_baseline_audit,
        "subtitle_regression_audit_path": (
            str(subtitle_regression_audit_path)
            if subtitle_regression_audit_path is not None
            else None
        ),
        "subtitle_regression": subtitle_regression_audit,
        "talk_filler_audit_path": (
            str(talk_filler_audit_path)
            if talk_filler_audit_path is not None
            else None
        ),
        "text_finalization_manifest_path": str(text_manifest_path) if text_manifest_path is not None else None,
        "speaker_mode": options.speaker_mode,
        "speaker_review_srt_path": str(speaker_review_srt) if speaker_review_srt is not None else None,
        "subtitle_ass_path": str(speaker_ass) if speaker_ass is not None else None,
        "subtitle_style": (
            speaker_subtitle_style_id
            if speaker_ass is not None
            else f"{profile_id}-final-sapphire72"
        ),
        "speaker_finalization_manifest_path": str(speaker_manifest_path) if speaker_manifest_path is not None else None,
        "speaker_finalization_manifest_sha256": ("sha256:" + _sha256(speaker_manifest_path)) if speaker_manifest_path is not None else None,
        "speaker_finalization": speaker_manifest,
        "subtitle_timing_qa": timing_qa,
        "boundary_audit": audit,
    }
    if clip_profile is not None:
        # Only explicit declarations are transcribed: legacy record bytes stay unchanged.
        record[CLIP_PROFILE_FIELD] = render_clip_profile(clip_profile)
    roles = chat_authority_audit.get("caption_audio_roles")
    if (isinstance(roles, Mapping)
            and roles.get("final_srt_sha256") == record["artifact_hashes"]["subtitle_sha256"]
            and roles.get("source_media_sha256") == record["artifact_hashes"]["video_sha256"]):
        # Logical stream ownership is consumed even when uniform_host skips
        # person-level speaker finalization. Rendering fields stay unchanged.
        record["caption_audio_roles"] = deepcopy(dict(roles))
    input_revision = _derive_burn_revision(
        record, branding_intro=branding_intro, render_revision_marker="native-burn-input-v1",
    )
    burn_revision = (
        _derive_burn_revision(
            record,
            branding_intro=branding_intro,
            render_revision_marker=render_revision_marker,
        )
        if render_revision_marker is not None
        else None
    )
    if burn_revision is not None:
        record["burn_revision"] = burn_revision
    burn_kwargs: dict[str, object] = {
        "run_ffmpeg": True,
        "branding_intro": branding_intro,
    }
    if burn_revision is not None:
        burn_kwargs["burn_revision"] = burn_revision
    prior_burn, render_parent = _retained_burn_metadata(chat_authority_audit, out_root=chat_authority_path.parent)
    replayed_burn = _replay_bound_burn(record, prior_burn, branding_intro=branding_intro)
    if replayed_burn is None:
        if isinstance(prior_burn, Mapping) and burn_revision is None:
            # Older native burns did not record an input revision. They are
            # preserved, not certified from output hashes alone. Render the
            # verified current input into its deterministic sibling namespace.
            burn_revision = _derive_burn_revision(
                record, branding_intro=branding_intro,
                render_revision_marker="preserved-prior-burn-input-v1",
            )
            burn_kwargs["burn_revision"] = burn_revision
            record["burn_revision"] = burn_revision
        record = adapters.burn_preview_subtitles(record, **burn_kwargs)
        if isinstance(prior_burn, Mapping):
            record["burn_recovery"] = {"status": "RENDERED_VERIFIED_CURRENT_SIBLING",
                "input_revision": input_revision,
                "preserved_prior_media_path": prior_burn.get("burned_media_path")}
    else:
        record = replayed_burn
        burn_revision = record.get("burn_revision")
    if render_parent is not None:
        record.setdefault("burn_recovery", {})["metadata_parent"] = render_parent
    if not isinstance(record.get("burned_preview"), dict) or record["burned_preview"].get("status") != "BURNED":
        raise SystemExit(f"FINAL_SUBTITLE_BURN_FAILED: {record.get('burned_preview')}")
    if burn_revision is not None:
        rendered_ass_path = record["burned_preview"].get("ass_path")
        if isinstance(rendered_ass_path, str) and rendered_ass_path:
            record["subtitle_ass_path"] = rendered_ass_path
    burned = _validated_burned_artifact(record)
    bind_final_filler_audit_to_burn(
        audit_path=talk_filler_audit_path,
        burned_preview=record["burned_preview"],
    )
    if talk_filler_audit_path is not None:
        record["artifact_hashes"]["talk_filler_audit_sha256"] = (
            "sha256:" + _sha256(talk_filler_audit_path)
        )
    rendered_ass = (
        Path(str(record["burned_preview"].get("ass_path")))
        if isinstance(record["burned_preview"].get("ass_path"), str)
        and record["burned_preview"].get("ass_path")
        else speaker_ass
    )
    chat_authority_audit["burn_binding"] = {
        "input_revision": input_revision,
        "burned_media_path": str(burned),
        "burned_media_sha256": _sha256(burned),
        "ass_path": str(rendered_ass) if rendered_ass is not None else None,
        "ass_sha256": _sha256(rendered_ass) if rendered_ass is not None else None,
        # The later title/cover stage may fail before record.json is saved.
        # Retain the renderer's actual intro/command identity with this burn.
        "burned_preview": deepcopy(record["burned_preview"]),
        **({"burn_revision": burn_revision} if burn_revision is not None else {}),
    }
    _persist_review_document(chat_authority_path, chat_authority_audit)
    record["artifact_hashes"]["chat_authority_audit_sha256"] = "sha256:" + _sha256(chat_authority_path)
    return require_final_subtitle_audio_check(
        record, subtitle_path, recut.recut_dir, candidate_id or media_path.stem,
        capture=adapters.capture_subtitle_audio_check,
    )


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _complete_postburn_cpa_identity(value: object) -> dict[str, object] | None:
    """Accept only the identity emitted by the real CPA callable.

    The command transport already exposes this through ``cpa_cache_identity``.
    Missing model/effort/transport fields are an unbound call, never a reason
    to invent a cache key from local defaults.
    """

    if not isinstance(value, Mapping):
        return None
    identity = dict(value)
    transport = identity.get("transport")
    models = identity.get("models")
    effort = identity.get("effort")
    if (
        not isinstance(transport, str)
        or not transport.strip()
        or not isinstance(models, (list, tuple))
        or not models
        or any(not isinstance(model, str) or not model.strip() for model in models)
        or not isinstance(effort, str)
        or not effort.strip()
    ):
        return None
    identity["models"] = list(models)
    return identity


def _write_postburn_cpa_text(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))
    os.chmod(path, 0o600)


def _read_postburn_cpa_text(path_value: object, expected_sha256: object) -> str:
    path = Path(str(path_value or ""))
    if not path.is_file() or path.is_symlink():
        raise ValueError("POSTBURN_SUBTITLE_AUDIO_CPA_ARTIFACT_MISSING")
    value = path.read_bytes().decode("utf-8", errors="strict")
    if _sha256_text(value) != str(expected_sha256 or ""):
        raise ValueError("POSTBURN_SUBTITLE_AUDIO_CPA_ARTIFACT_HASH_DRIFT")
    return value


def _recover_completed_postburn_transport(review: Mapping[str, object], out_root: Path) -> dict:
    recovered = deepcopy(dict(review))
    execution_path = os.environ.get("AUTOSLICE_COMPLETED_CPA_EXECUTION")
    if review.get("status") != "RUNNING" or not execution_path:
        return recovered
    if review.get("release_gate") != "BLOCK":
        raise ValueError("POSTBURN_COMPLETED_TRANSPORT_STATE_INVALID")
    expected = os.environ.get("AUTOSLICE_COMPLETED_CPA_EXECUTION_SHA256", "")
    execution = json.loads(_read_postburn_cpa_text(execution_path, "sha256:" + expected.removeprefix("sha256:")))
    interruption = execution.get("interruption") or {}
    raw = interruption.get("raw_completion_unconsumed") or {}
    materials = execution.get("terminal_materials") or {}
    request = review.get("cpa_request") or {}
    if (Path(str(execution.get("output_root"))).resolve() != out_root.resolve()
            or execution.get("status") != "INTERRUPTED"
            or interruption.get("provider_child_completed_before_parent_stop") is not True
            or raw.get("outcome") != "CHILD_COMPLETED_RAW_UNCONSUMED_PARENT_INTERRUPTED"
            or type(raw.get("raw_wait_exit_code")) is not int or raw.get("raw_wait_exit_code") != 0
            or raw.get("child_not_signalled") is not True
            or raw.get("pidfd_exit_ready") is not True
            or "sha256:" + str((materials.get("srt") or {}).get("sha256")) != request.get("final_srt_sha256")
            or "sha256:" + str((materials.get("video") or {}).get("sha256")) != request.get("actual_media_sha256")):
        raise ValueError("POSTBURN_COMPLETED_TRANSPORT_BINDING_INVALID")
    files = {Path(str(item.get("source"))).name: item for item in raw.get("files", [])}
    verified = {}
    for name in ("prompt.txt", "completion.txt"):
        item = files[name]
        text = _read_postburn_cpa_text(item["preserved"], "sha256:" + item["sha256"])
        if len(text.encode("utf-8")) != item["bytes"]:
            raise ValueError("POSTBURN_COMPLETED_TRANSPORT_BYTES_INVALID")
        verified[name] = item
    calls = recovered.get("cpa_calls") or []
    if not isinstance(calls, list) or not calls:
        raise ValueError("POSTBURN_COMPLETED_TRANSPORT_REQUEST_INVALID")
    for earlier in calls[:-1]:
        if (not isinstance(earlier, Mapping) or earlier.get("dispatch_status") != "COMPLETED"
                or earlier.get("error_type")
                or earlier.get("request_sha256") != request.get("request_sha256")):
            raise ValueError("POSTBURN_COMPLETED_TRANSPORT_REQUEST_INVALID")
        _read_postburn_cpa_text(earlier.get("prompt_path"), earlier.get("prompt_sha256"))
        _read_postburn_cpa_text(earlier.get("response_path"), earlier.get("response_sha256"))
    if (not calls or not isinstance(calls[-1], Mapping)
            or calls[-1].get("dispatch_status") not in {"RUNNING", "COMPLETED"}
            or calls[-1].get("request_sha256") != request.get("request_sha256")
            or calls[-1].get("prompt_sha256") != "sha256:" + verified["prompt.txt"]["sha256"]
            or (calls[-1].get("dispatch_status") == "COMPLETED"
                and calls[-1].get("response_sha256") != "sha256:" + verified["completion.txt"]["sha256"])):
        raise ValueError("POSTBURN_COMPLETED_TRANSPORT_REQUEST_INVALID")
    calls[-1].update(dispatch_status="COMPLETED",
        prompt_path=verified["prompt.txt"]["preserved"],
        response_path=verified["completion.txt"]["preserved"],
        response_sha256="sha256:" + verified["completion.txt"]["sha256"])
    recovered["completion_recovery"] = {
        "execution_path": execution_path, "execution_sha256": expected,
        "status": "TRANSPORT_COMPLETE_RAW_PENDING_PARSE", "release_authorized": False,
    }
    return recovered


def preserve_postburn_observation(out_root: Path, cid: str) -> None:
    """Keep provider evidence before ordinary input restoration replaces an audit."""
    source = out_root / f"{cid}.chat-authority.json"
    if not source.is_file():
        return
    authority = _read_review_document(source, label="POSTBURN_CHAT_AUTHORITY")
    review = authority.get("postburn_subtitle_audio_review")
    if not isinstance(review, Mapping):
        return
    from src.autoslice.package_import import atomic_write_bytes
    review = _recover_completed_postburn_transport(review, out_root)
    snapshot = {key: value for key, value in review.items()
                if key != "prior_postburn_subtitle_audio_review"}
    raw = _json_bytes(snapshot)
    root = out_root / f".{cid}.postburn-cpa"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    from src.autoslice.producer_text_checkpoint import _frozen_binding
    original_authority = _frozen_binding(root, "authority", source)
    digest = hashlib.sha256(raw).hexdigest()
    artifact = root / f"observation-{digest}.json"
    if artifact.exists() and artifact.read_bytes() != raw:
        raise ValueError("POSTBURN_OBSERVATION_CACHE_DRIFT")
    if not artifact.exists():
        atomic_write_bytes(artifact, raw)
    atomic_write_bytes(root / "observation.json", _json_bytes({
        "candidate_id": cid, "release_authorized": False,
        "path": str(artifact), "sha256": "sha256:" + digest,
        "source_authority": original_authority,
    }))


def _load_postburn_observation(out_root: Path, cid: str) -> Mapping[str, object] | None:
    pointer = out_root / f".{cid}.postburn-cpa" / "observation.json"
    execution_sha = os.environ.get("AUTOSLICE_COMPLETED_CPA_EXECUTION_SHA256", "").removeprefix("sha256:")
    if bool(execution_sha) != bool(os.environ.get("AUTOSLICE_COMPLETED_CPA_EXECUTION")):
        raise ValueError("POSTBURN_COMPLETED_EXECUTION_BINDING_INCOMPLETE")
    if execution_sha:
        matches = []
        for artifact in sorted(pointer.parent.glob("observation-*.json")):
            digest = artifact.stem.removeprefix("observation-")
            try:
                review = _read_review_bytes(
                    _read_postburn_cpa_text(artifact, "sha256:" + digest).encode("utf-8"),
                    directory=artifact.parent, label="POSTBURN_OBSERVATION",
                )
            except (OSError, ValueError, UnicodeError):
                continue
            if not isinstance(review, Mapping):
                continue
            recovery = review.get("completion_recovery") or {}
            if (review.get("status") != "RUNNING"
                    or recovery.get("status") != "TRANSPORT_COMPLETE_RAW_PENDING_PARSE"
                    or str(recovery.get("execution_sha256") or "").removeprefix("sha256:") != execution_sha):
                continue
            review = _recover_completed_postburn_transport(review, out_root)
            if (not _postburn_cached_review_complete(review)
                    or review["cpa_request"].get("candidate_id") != cid):
                raise ValueError("POSTBURN_COMPLETED_OBSERVATION_BINDING_INVALID")
            matches.append(review)
        if matches:
            identities = {(row["cpa_request"]["request_sha256"], tuple(
                (call["prompt_sha256"], call["response_sha256"]) for call in row["cpa_calls"]
            )) for row in matches}
            if len(identities) != 1:
                raise ValueError("POSTBURN_COMPLETED_OBSERVATION_AMBIGUOUS")
            return matches[0]
        raise ValueError("POSTBURN_COMPLETED_OBSERVATION_NOT_FOUND")
    if not pointer.exists():
        return None
    binding = json.loads(pointer.read_text(encoding="utf-8"))
    path = Path(str(binding.get("path") or ""))
    if (pointer.is_symlink() or binding.get("candidate_id") != cid
            or binding.get("release_authorized") is not False
            or not path.resolve().is_relative_to(pointer.parent.resolve())):
        raise ValueError("POSTBURN_OBSERVATION_CACHE_BINDING_INVALID")
    review = _read_review_bytes(
        _read_postburn_cpa_text(path, binding.get("sha256")).encode("utf-8"),
        directory=path.parent, label="POSTBURN_OBSERVATION",
    )
    # Compact observations omit nested history. A zero-dispatch parser failure
    # still needs its completed parent, which is retained in the exact frozen
    # authority written alongside the observation, never guessed from mtime.
    if (isinstance(review, Mapping) and review.get("cpa_calls") == []
            and review.get("status") == "AUDITOR_UNAVAILABLE"
            and review.get("error_type") == "ValueError"
            and review.get("reason_codes") == ["POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY"]):
        source = binding.get("source_authority")
        if isinstance(source, Mapping):
            source_path = Path(str(source.get("path") or ""))
            authority = _read_review_bytes(
                _read_postburn_cpa_text(source_path, source.get("sha256")).encode("utf-8"),
                directory=source_path.parent, label="POSTBURN_CHAT_AUTHORITY",
            )
            original = authority.get("postburn_subtitle_audio_review")
            if (not isinstance(original, Mapping)
                    or {key: value for key, value in original.items()
                        if key != "prior_postburn_subtitle_audio_review"} != review):
                raise ValueError("POSTBURN_OBSERVATION_AUTHORITY_DRIFT")
            return original
    return review


def _postburn_request_sha256(request: Mapping[str, object]) -> str:
    payload = {
        key: value for key, value in request.items() if key != "request_sha256"
    }
    return _sha256_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _postburn_cached_review_complete(review: object) -> bool:
    if not isinstance(review, Mapping):
        return False
    request = review.get("cpa_request")
    calls = review.get("cpa_calls")
    if (
        review.get("schema_version") != "postburn-subtitle-audio-review.v1"
        or not isinstance(request, Mapping)
        or _complete_postburn_cpa_identity(request.get("cpa_cache_identity")) is None
        or not isinstance(request.get("provider_runtime_binding"), Mapping)
        or not isinstance(request.get("request_sha256"), str)
        or _postburn_request_sha256(request) != request.get("request_sha256")
        or not isinstance(review.get("findings"), list)
        or not isinstance(calls, list)
        or not calls
    ):
        return False
    for call in calls:
        if (
            not isinstance(call, Mapping)
            or call.get("error_type")
            or call.get("dispatch_status") != "COMPLETED"
            or call.get("request_sha256") != request.get("request_sha256")
        ):
            return False
        try:
            _read_postburn_cpa_text(call.get("prompt_path"), call.get("prompt_sha256"))
            _read_postburn_cpa_text(call.get("response_path"), call.get("response_sha256"))
        except (OSError, UnicodeError, ValueError):
            return False
    return True


def _completed_postburn_before_parser_failure(review: object) -> object:
    """A failed replay with no dispatch cannot erase a complete bound parent."""
    if (not isinstance(review, Mapping) or review.get("status") != "AUDITOR_UNAVAILABLE"
            or review.get("error_type") != "ValueError" or review.get("cpa_calls") != []
            or review.get("reason_codes") != ["POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY"]):
        return review
    parent = review.get("prior_postburn_subtitle_audio_review")
    if (isinstance(parent, Mapping) and _postburn_cached_review_complete(parent)
            and review.get("cpa_request") == parent.get("cpa_request")
            and review.get("reviewed_srt_sha256") == parent.get("reviewed_srt_sha256")):
        return parent
    return review


def _postburn_input_bindings(value: object) -> list[object]:
    keys = {"source_media_sha256", "source_sha256", "audio_sha256", "frame_sha256",
            "witness_srt_sha256", "source_event_id", "matched_start_ms", "matched_end_ms"}
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key.startswith("_"):
                continue
            if key in keys:
                found.append({key: item})
            elif isinstance(item, (Mapping, list, tuple)):
                found.extend(_postburn_input_bindings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_postburn_input_bindings(item))
    return [item for _, item in sorted({
        json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in found
    }.items())]


def _postburn_material_progress(value: object) -> object:
    """Identify input/decision changes without counting new report wording.

    Original rows stay in their audits. This projection is only a dispatch
    progress key; it grants neither mutation nor release authority.
    """
    if isinstance(value, Mapping):
        if type(value.get("cue_index")) is int:
            # A new proposed wording or classification is still a hypothesis
            # about the same cue. Count its risk/disposition or bound external
            # inputs, rather than another rationale or unconsumed proposal.
            adjudication = value.get("exact_release_adjudication") or {}
            adjudication = adjudication if isinstance(adjudication, Mapping) else {}
            witness_judge = adjudication.get("witness_judge") or {}
            witness_judge = witness_judge if isinstance(witness_judge, Mapping) else {}
            judge = witness_judge.get("judge") or {}
            judge = judge if isinstance(judge, Mapping) else {}
            return {
                **{key: value[key] for key in (
                    "cue_index", "base_text_sha256", "content_risk", "decision", "resolution",
                ) if key in value},
                "input_bindings": _postburn_input_bindings(value),
                "exact_release_adjudication": {
                    **{key: adjudication[key] for key in (
                        "status", "policy_branch", "repaired", "timing_immutable", "decision_authority",
                    ) if key in adjudication},
                    "witness_judge": {"judge": {key: judge[key] for key in (
                        "choice", "current_utterance_supported", "needs_audio",
                    ) if key in judge}},
                },
            }
        narrative = {"why", "reason", "summary", "risk_basis", "explanation", "detail",
                     "suspect", "suggestion", "evidence_cue_ids", "evidence_cue_indexes",
                     "span_start_codepoint", "span_end_codepoint", "candidate_memory_id"}
        volatile = {"prompt_sha256", "completion_sha256", "response_sha256",
                    "request_sha256", "raw_result_sha256", "receipt_sha256",
                    "fingerprint", "finding_sha256", "finding_sha256s", "observation_sha256"}
        return {key: _postburn_material_progress(item) for key, item in value.items()
                if not key.startswith("_") and key not in narrative | volatile
                and not key.endswith("_path")}
    if isinstance(value, (list, tuple)):
        items = [_postburn_material_progress(item) for item in value]
        from .prompt_materials import material_multiset
        return material_multiset(items)
    return value


def _postburn_progress_identity(srt_sha: str, resolutions: object, source_evidence: object) -> str:
    return _sha256_text(json.dumps(
        [srt_sha, _postburn_material_progress(resolutions), _postburn_material_progress(source_evidence)],
        ensure_ascii=False, sort_keys=True,
    ))


def _postburn_current_risk_partition(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    from src.autoslice.final_review_contract import is_release_resolved_keep
    from src.autoslice.final_review_candidate_risk import needs_candidate_content_review, candidate_risk_assessment_valid




    def can_disclose(row):
        return (is_release_resolved_keep(row)
                and (not needs_candidate_content_review(row) or candidate_risk_assessment_valid(row)))
    disclosed = [row for row in findings if can_disclose(row)]
    blocked = [row for row in findings if not can_disclose(row)]
    return blocked, disclosed


def _postburn_reuse_current_gift_risk(findings: list[dict], *, preburn: Mapping,
        final_text: str) -> list[dict]:
    """A detector question cannot erase a completed same-byte risk assessment."""
    from src.autoslice.editorial_subtitle import GIFT_NOMINATION_REASON
    from src.autoslice.final_review_contract import current_noncritical_editorial_assessment
    digest=_sha256_text(final_text)
    try:
        validate_final_review_release(preburn,expected_srt_sha256=digest)
    except FinalReviewContractError:
        return findings
    cues=parse_srt_cues(final_text)
    known={}
    for row in [*(preburn.get('resolved_findings') or []),*(preburn.get('unresolved_findings_disclosed') or [])]:
        if not isinstance(row, Mapping):
            continue
        if current_noncritical_editorial_assessment(row,expected_srt_sha256=digest):
            known.setdefault(row.get('cue_index'),[]).append(row['current_content_risk'])
    critical={row.get('cue_index') for row in findings
        if row.get('content_risk') in {'meaning_reversal','factual_error','potential_harm'}}
    result=[]
    for row in findings:
        index = row.get('cue_index')
        assessments = known.get(index, [])
        pure_nomination=(row.get('why')==GIFT_NOMINATION_REASON and row.get('kind')=='entity'
            and row.get('repair_class')=='editorial_rendering' and row.get('proposed_full_cue') is None
            and not row.get('content_risk') and not row.get('exact_release_adjudication'))
        if (pure_nomination and index not in critical and type(index) is int and 1<=index<=len(cues)
                and assessments and all(item==assessments[0] for item in assessments)
                and assessments[0].get('base_text_sha256')==_sha256_text(cues[index-1].text).removeprefix('sha256:')
                and row.get('base_text_sha256')==assessments[0]['base_text_sha256']):
            assessment=deepcopy(assessments[0])
            row={**row,'content_risk':assessment['content_risk'],'risk_basis':assessment['risk_basis'],
                'current_content_risk':assessment,
                'risk_assessment_replay':{'role':'COMPLETED_CURRENT_RISK_ONLY_NO_MUTATION','reviewed_srt_sha256':digest}}
        result.append(row)
    return result


def _postburn_reuse_completed_context(context: str, evidence: object, previous: object) -> str:
    """Keep the original prompt bytes when only its decision report changed."""
    if not evidence or not _postburn_cached_review_complete(previous):
        return context
    marker = "EXACT_FINAL_TYPED_RESOLUTIONS (EVIDENCE ONLY; independently reassess):\n"
    if context.count(marker) != 1:
        return context
    start = context.index(marker) + len(marker)
    _current, end = json.JSONDecoder().raw_decode(context[start:])
    expected = previous["cpa_request"].get("structured_context_sha256")
    for call in previous["cpa_calls"]:
        prompt = _read_postburn_cpa_text(call.get("prompt_path"), call.get("prompt_sha256"))
        if prompt.count(marker) != 1:
            continue
        raw = prompt.split(marker, 1)[1]
        old, old_end = json.JSONDecoder().raw_decode(raw)
        from .prompt_materials import canonical_materials
        if canonical_materials(_postburn_material_progress(old)) != canonical_materials(_postburn_material_progress(evidence)):
            continue
        candidate = context[:start] + raw[:old_end] + context[start + end:]
        if _sha256_text(candidate) == expected:
            return candidate
    return context


def _postburn_carry_disclosure_metadata(
    evidence: object, previous: object, srt_sha256: str, *, witness: Mapping[str, object],
) -> object:
    """Retain omitted ordinary descriptors, without importing new decisions."""
    if (not _postburn_cached_review_complete(previous)
            or previous["cpa_request"].get("final_srt_sha256") != srt_sha256
            or any(previous["cpa_request"].get(key) != witness.get(key) for key in (
                "actual_media_sha256", "audio_sha256", "witness_srt_sha256", "raw_result_sha256",
                "receipt_sha256", "intro_offset_ms",
            ))):
        return evidence
    marker = "EXACT_FINAL_TYPED_RESOLUTIONS (EVIDENCE ONLY; independently reassess):\n"
    for call in previous["cpa_calls"]:
        prompt = _read_postburn_cpa_text(call.get("prompt_path"), call.get("prompt_sha256"))
        if prompt.count(marker) != 1:
            continue
        old, _ = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])
        if not isinstance(old, Mapping):
            continue
        if old.get("reviewed_srt_sha256") != srt_sha256:
            continue
        retained = []
        for row in old.get("resolved_findings") or []:
            material = _postburn_material_progress(row)
            if (material.get("content_risk") == "noncritical_uncertainty"
                    and not material.get("input_bindings") and not material.get("decision")
                    and not material.get("resolution")
                    and material.get("exact_release_adjudication") == {"witness_judge": {"judge": {}}}):
                retained.append(row)
        if retained:
            return _current_postburn_resolution_evidence(
                exact_review={"resolved_findings": (evidence or {}).get("resolved_findings") or []},
                reviewed_srt_sha256=srt_sha256,
                previous={"reviewed_srt_sha256": srt_sha256, "resolved_findings": retained},
            )
    return evidence


def _current_postburn_resolution_evidence(
    *,
    exact_review: Mapping[str, object],
    reviewed_srt_sha256: str,
    previous: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """Retain ordinary exact decisions within this fixed source/window run.

    A later review may address only newly discovered cues. Earlier decisions
    remain evidence while the entire SRT is unchanged; changed bytes discard
    them, and a new decision about the same cue supersedes the old one. CPA
    still independently reviews this context and supplies the postburn gate.
    """
    latest = exact_review.get("resolved_findings")
    disclosed = exact_review.get("unresolved_findings_disclosed")
    latest = latest if isinstance(latest, list) else []
    disclosed = disclosed if isinstance(disclosed, list) else []
    postburn = exact_review.get("postburn_subtitle_audio_review")
    if (isinstance(postburn, Mapping)
            and postburn.get("reviewed_srt_sha256") == reviewed_srt_sha256
            and _postburn_cached_review_complete(postburn)):
        # The previous postburn's own ordinary disclosures are not new exact
        # decisions to feed back into its discovery request. Preserve their
        # original audit and risk obligations; genuinely new typed decisions
        # or external evidence differ from these complete, byte-bound rows.
        echoes = postburn.get("unresolved_findings_disclosed") or []
        disclosed = [row for row in disclosed if not any(
            row == old and not row.get("exact_release_adjudication")
            and not row.get("mutation_authority")
            for old in echoes if isinstance(old, Mapping)
        )]
    addressed_cues = {
        row.get("cue_index") for row in [*latest, *disclosed]
        if isinstance(row, Mapping)
    }
    retained = (
        previous.get("resolved_findings")
        if isinstance(previous, Mapping)
        and previous.get("reviewed_srt_sha256") == reviewed_srt_sha256
        else None
    )
    resolved = [
        *(row for row in (retained if isinstance(retained, list) else [])
          if isinstance(row, Mapping) and row.get("cue_index") not in addressed_cues),
        *(row for row in latest if isinstance(row, Mapping)),
        *(row for row in disclosed if is_keep_current_disclosed(row)),
    ]
    # Preserve the first complete account of an equivalent decision. New
    # prose, prompt/response hashes or ordering cannot manufacture new input.
    previous_rows = retained if isinstance(retained, list) else []
    first_by_identity = {
        json.dumps(_postburn_material_progress(row), ensure_ascii=False, sort_keys=True): row
        for row in previous_rows if isinstance(row, Mapping)
    }
    resolved = [first_by_identity.get(
        json.dumps(_postburn_material_progress(row), ensure_ascii=False, sort_keys=True), row,
    ) for row in resolved]
    resolved.sort(key=lambda row: json.dumps(_postburn_material_progress(row), ensure_ascii=False, sort_keys=True))
    return (
        {"reviewed_srt_sha256": reviewed_srt_sha256,
         "resolved_findings": deepcopy(resolved)}
        if resolved else None
    )


def _run_postburn_subtitle_audio_review(
    *,
    cid: str,
    out_root: Path,
    recut: FinalRecutArtifacts,
    record: dict,
    chat_authority_audit: dict,
    chat_authority_path: Path,
    clip_context: Mapping[str, object] | None = None,
    selection_hook: str = "",
    typed_resolution_evidence: Mapping[str, object] | None = None,
    source_evidence_spec: Mapping[str, object] | None = None,
    timeline_offset_ms: int = 0,
    completed_exact_review: Mapping[str, object] | None = None,
) -> dict:
    """Let normal CPA inspect an already-validated postburn text witness.

    The gate has already paid for BCUT.  This pass only reads and validates its
    artifacts, then sends the current final SRT plus witness context through
    the normal CPA transport.  It never applies a subtitle mutation.
    """

    try:
        burned = _validated_burned_artifact(record)
        witness = load_validated_subtitle_audio_witness(
            record,
            recut.subtitle_path,
            burned,
            candidate_id=cid,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(
            f"POSTBURN_SUBTITLE_AUDIO_WITNESS_INVALID: {type(exc).__name__}: {exc}"
        ) from exc
    final_bytes = recut.subtitle_path.read_bytes()
    final_text = final_bytes.decode("utf-8", errors="strict")
    reviewed_srt_sha256 = _sha256_text(final_text)
    wire_existing = chat_authority_audit.get("final_review_audit")
    wire_prior_postburn = (
        wire_existing.get("postburn_subtitle_audio_review")
        if isinstance(wire_existing, Mapping)
        else None
    )
    # Callers may hand this gate the compact authority object read from disk
    # while the first pass historically supplied the expanded in-memory map.
    # Expand the actual review children in place so subsequent cache identity
    # and finding comparisons see the same legacy object either way.
    expanded_chat_authority = _expand_review_document(chat_authority_audit)
    if expanded_chat_authority is not chat_authority_audit:
        chat_authority_audit.clear()
        chat_authority_audit.update(expanded_chat_authority)
    existing = chat_authority_audit.get("final_review_audit")
    if not isinstance(existing, Mapping):
        raise SystemExit("POSTBURN_SUBTITLE_AUDIO_FINAL_REVIEW_AUDIT_MISSING")
    saved = _load_postburn_observation(out_root, cid)
    if saved is not None:
        existing = {**existing, "postburn_subtitle_audio_review": saved}
    original_postburn = existing.get("postburn_subtitle_audio_review")
    completed_parent = _completed_postburn_before_parser_failure(original_postburn)
    replay_failure_history = original_postburn if completed_parent is not original_postburn else None
    if replay_failure_history is not None:
        existing = {**existing, "postburn_subtitle_audio_review": completed_parent}
        if existing.get("release_gate") != "PASS" and isinstance(completed_exact_review, Mapping):
            # Only the zero-dispatch postburn envelope may differ. Revalidate
            # the retained exact verdict against these identical SRT bytes;
            # the failed/new postburn question still has its own strict gate.
            envelope = {"status", "release_gate", "reason_codes", "postburn_subtitle_audio_review"}
            if ({key: value for key, value in existing.items() if key not in envelope}
                    != {key: value for key, value in completed_exact_review.items() if key not in envelope}
                    or set(existing.get("reason_codes") or []) != (
                        set(completed_exact_review.get("reason_codes") or [])
                        | set(replay_failure_history.get("reason_codes") or []))):
                raise ValueError("POSTBURN_PREBURN_INPUT_DRIFT")
            from src.autoslice.final_review_contract import validate_final_review_release
            validate_final_review_release(completed_exact_review, expected_srt_sha256=reviewed_srt_sha256)
            existing = {**deepcopy(dict(completed_exact_review)),
                        "postburn_subtitle_audio_review": completed_parent}
    typed_resolution_evidence = _postburn_carry_disclosure_metadata(
        typed_resolution_evidence, existing.get("postburn_subtitle_audio_review"), reviewed_srt_sha256,
        witness=witness,
    )
    declared_srt_sha256 = str(existing.get("reviewed_srt_sha256") or "")
    if (
        declared_srt_sha256.removeprefix("sha256:")
        != reviewed_srt_sha256.removeprefix("sha256:")
        or existing.get("release_gate") != "PASS"
    ):
        raise SystemExit("POSTBURN_SUBTITLE_AUDIO_FINAL_REVIEW_AUDIT_DRIFT")
    receipt = witness["receipt"]
    receipt_mapping = receipt if isinstance(receipt, Mapping) else {}
    context_text = "\n".join(
        (
            "POSTBURN_SUBTITLE_AUDIO_WITNESS (EVIDENCE ONLY)",
            f"candidate_id: {cid}",
            f"actual_media_sha256: {witness['actual_media_sha256']}",
            f"final_srt_sha256: {reviewed_srt_sha256}",
            f"witness_srt_sha256: {witness['witness_srt_sha256']}",
            f"intro_offset_ms: {witness['intro_offset_ms']}",
            "timebase: witness_ms = final_delivery_ms + intro_offset_ms; apply once",
            f"timing_status: {receipt_mapping.get('timing_status')}",
            f"text_correctness_status: {receipt_mapping.get('text_correctness_status')}",
            "BCUT text is an independent witness; a difference is not proof of an error.",
            *([f"selection_hook: {selection_hook.strip()}"] if selection_hook.strip() else []),
            "witness_srt:",
            str(witness["witness_text"]),
        )
    )
    if typed_resolution_evidence:
        # The ordinary exact-final consumer just reviewed these same bytes.
        # Its resolutions are new evidence for CPA, never a fabricated CLEAN
        # postburn receipt. In particular, a supported CURRENT need not edit
        # the SRT merely to make a subsequent review possible.
        if typed_resolution_evidence.get("reviewed_srt_sha256") != reviewed_srt_sha256:
            raise SystemExit("POSTBURN_TYPED_RESOLUTION_SRT_DRIFT")
        context_text += "\nEXACT_FINAL_TYPED_RESOLUTIONS (EVIDENCE ONLY; independently reassess):\n" + json.dumps(
            _postburn_material_progress(typed_resolution_evidence), ensure_ascii=False, sort_keys=True,
        )
    # Burning the same scene does not remove its checked source evidence.
    # Replay the ordinary validators against the current delivery grid; raw
    # observations remain context, rather than inheriting an earlier PASS.
    source_spec = source_evidence_spec or {}
    source_context, source_context_audit = _bound_source_evidence_context(
        final_text, clip_context or {}, timeline_offset_ms,
        range_audio=source_spec.get("prior_range_acoustic_observations") or (),
        earlier_screen=source_spec.get("prior_screen_context_observations") or (),
        range_screen=source_spec.get("prior_range_screen_context_observations") or (),
    )
    context_text += source_context
    context_text = _postburn_reuse_completed_context(
        context_text, typed_resolution_evidence, existing.get("postburn_subtitle_audio_review"),
    )
    context_sha256 = _sha256_text(context_text)
    final_review_llm = build_final_review_llm_call()
    cache_identity = _complete_postburn_cpa_identity(
        getattr(final_review_llm, "cpa_cache_identity", None)
    )
    if cache_identity is None:
        raise SystemExit("POSTBURN_SUBTITLE_AUDIO_CPA_IDENTITY_MISSING")
    runtime_binding = getattr(final_review_llm, "provider_runtime_binding", {})
    if isinstance(runtime_binding, Mapping):
        runtime_binding = dict(runtime_binding)
    else:
        runtime_binding = {}
    # These values come from the callable's real command identity.  Keep them
    # beside the runtime/credential binding so a model or effort change cannot
    # look like the same transport merely because the host stayed constant.
    runtime_binding.update(
        {
            "provider_call_transport": cache_identity["transport"],
            "provider_model": list(cache_identity["models"]),
            "provider_effort": cache_identity["effort"],
        }
    )
    candidate_context_text = (
        clip_context_prompt_text(clip_context)
        if isinstance(clip_context, Mapping)
        else ""
    )
    from src.autoslice.editorial_subtitle import rendering_context
    editorial_text, editorial_cues = rendering_context(final_text, chat_authority_audit, timeline_offset_ms)
    candidate_context_text += editorial_text
    from src.autoslice.audio_language_discovery import current_language_discovery_context
    language_context, language_findings = current_language_discovery_context(
        final_text, chat_authority_audit,
        source_media_sha256=(source_spec.get("source_piece_clock") or {}).get("padded_sha256"),
        timeline_offset_ms=timeline_offset_ms,
    )
    candidate_context_text += language_context
    # The independent witness's span-level wording disagreements are deterministic
    # nominations: the ordinary reviewer must hear the audio and choose between
    # the current words and the witness words. Decisions already made on these
    # exact bytes (typed resolutions) are not asked again.
    from src.autoslice.witness_disagreement import (
        decided_nomination_ids, find_witness_disagreements, nomination_digest,
        to_priority_findings,
    )
    witness_nominations = find_witness_disagreements(
        final_text, str(witness["witness_text"]), intro_offset_ms=int(witness["intro_offset_ms"]),
    )
    decided_witness_ids = decided_nomination_ids(
        witness_nominations,
        (typed_resolution_evidence or {}).get("resolved_findings") or [],
    )
    open_witness_nominations = [
        row for row in witness_nominations if row["nomination_id"] not in decided_witness_ids
    ]
    witness_findings = to_priority_findings(
        open_witness_nominations, final_srt_text=final_text,
        witness_srt_sha256=str(witness["witness_srt_sha256"]),
    )
    review_extra_findings = [*language_findings, *witness_findings]
    review_glossary = terminology_prompt_context(
        final_text, str(witness["witness_text"]), candidate_context_text,
    )
    from src.autoslice.final_review_auditor import _AUDIT_PROMPT
    request = {
        "schema_version": "postburn-subtitle-audio-cpa-request.v1",
        "meaning_policy": MEANING_POLICY_VERSION,
        "candidate_id": cid,
        "actual_media_sha256": witness["actual_media_sha256"],
        "final_srt_sha256": reviewed_srt_sha256,
        "witness_srt_sha256": witness["witness_srt_sha256"],
        "audio_sha256": witness["audio_sha256"],
        "raw_result_sha256": witness["raw_result_sha256"],
        "receipt_sha256": witness["receipt_sha256"],
        "intro_offset_ms": witness["intro_offset_ms"],
        "timebase": "witness_ms = final_delivery_ms + intro_offset_ms",
        "witness_authority": "EVIDENCE_ONLY",
        "structured_context_sha256": context_sha256,
        "candidate_context_sha256": _sha256_text(candidate_context_text),
        "glossary_sha256": _sha256_text(review_glossary),
        "discovery_prompt_template_sha256": _sha256_text(_AUDIT_PROMPT),
        "cpa_cache_identity": cache_identity,
        "provider_runtime_binding": runtime_binding,
        **({"witness_disagreement_sha256": nomination_digest(open_witness_nominations)}
           if open_witness_nominations else {}),
    }
    request["request_sha256"] = _postburn_request_sha256(request)
    cpa_calls: list[dict[str, object]] = []
    subtitle_audio_binding = {
        key: witness[key]
        for key in (
            "candidate_id", "provider", "provider_route",
            "actual_media_sha256", "final_srt_sha256",
            "witness_srt_sha256", "audio_sha256", "raw_result_sha256",
            "intro_offset_ms", "witness_srt_path", "provenance_path",
            "correspondence_path", "raw_result_path", "receipt_sha256",
        )
        if key in witness
    }
    review_audit_path = out_root / f"{cid}.review-flags.json"

    def persist_running_checkpoint(call: Mapping[str, object]) -> None:
        checkpoint = {
            "schema_version": "postburn-subtitle-audio-review.v1",
            "status": "RUNNING",
            "release_gate": "BLOCK",
            "reason_codes": [
                "POSTBURN_SUBTITLE_AUDIO_CPA_DISPATCH_INCOMPLETE"
            ],
            "reviewed_srt_sha256": reviewed_srt_sha256,
            "validated_finding_count": 0,
            "findings": [],
            "cache_reused": False,
            "cpa_cache_identity": cache_identity,
            "subtitle_audio_binding": subtitle_audio_binding,
            "cpa_request": dict(request),
            "cpa_calls": [dict(item) for item in cpa_calls],
            "provider_runtime_binding": dict(runtime_binding),
            "checkpoint": "before_provider_dispatch",
            **(
                {"prior_postburn_subtitle_audio_review": dict(
                    wire_prior_postburn
                    if isinstance(wire_prior_postburn, Mapping)
                    else (replay_failure_history or prior_review_for_audit)
                )}
                if isinstance(wire_prior_postburn, Mapping)
                or isinstance(replay_failure_history or prior_review_for_audit, Mapping)
                else {}
            ),
        }
        checkpoint_audit = deepcopy(dict(existing))
        # Keep the ordinary exact-final PASS as the input authority.  The
        # nested postburn checkpoint carries the separate incomplete state.
        checkpoint_audit["postburn_subtitle_audio_review"] = checkpoint
        chat_authority_audit["postburn_subtitle_audio_review"] = checkpoint
        chat_authority_audit["final_review_audit"] = checkpoint_audit
        _persist_review_document(chat_authority_path, chat_authority_audit)
        persist_review_audit(review_audit_path, checkpoint_audit)
        try:
            persisted_chat = _read_review_document(
                chat_authority_path, label="POSTBURN_CHAT_AUTHORITY",
            )
            persisted_checkpoint = persisted_chat.get("postburn_subtitle_audio_review")
            persisted_request = (
                persisted_checkpoint.get("cpa_request")
                if isinstance(persisted_checkpoint, Mapping)
                else None
            )
            if (
                not isinstance(persisted_checkpoint, Mapping)
                or not isinstance(persisted_request, Mapping)
                or persisted_request.get("request_sha256")
                != request["request_sha256"]
                or persisted_checkpoint.get("status") != "RUNNING"
            ):
                raise ValueError("POSTBURN_SUBTITLE_AUDIO_CHECKPOINT_DRIFT")
            persisted_review = _read_review_document(
                review_audit_path, label="POSTBURN_REVIEW_AUDIT",
            )
            if persisted_review.get("postburn_subtitle_audio_review") != _expand_review_document(checkpoint):
                raise ValueError("POSTBURN_SUBTITLE_AUDIO_REVIEW_CHECKPOINT_DRIFT")
            preserve_postburn_observation(out_root, cid)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("POSTBURN_SUBTITLE_AUDIO_CHECKPOINT_PERSIST_FAILED") from exc

    def tracked_llm_call(prompt: str) -> str:
        prompt_sha256 = _sha256_text(prompt)
        attempt = len(cpa_calls) + 1
        private_root = out_root / f".{cid}.postburn-cpa"
        prompt_path = private_root / f"attempt-{attempt:02d}-{prompt_sha256[7:19]}.prompt.txt"
        _write_postburn_cpa_text(prompt_path, prompt)
        call = {
            "attempt": attempt,
            "request_sha256": request["request_sha256"],
            "prompt_sha256": prompt_sha256,
            "prompt_path": str(prompt_path),
            "served_from_cache": False,
            "dispatch_status": "RUNNING",
        }
        cpa_calls.append(call)
        persist_running_checkpoint(call)
        try:
            completion = final_review_llm(prompt)
            if not isinstance(completion, str):
                raise TypeError("CPA completion must be text")
            response_sha256 = _sha256_text(completion)
            response_path = private_root / (
                f"attempt-{attempt:02d}-{response_sha256[7:19]}.completion.txt"
            )
            _write_postburn_cpa_text(response_path, completion)
        except Exception as exc:
            call["dispatch_status"] = "FAILED"
            call["error_type"] = type(exc).__name__
            raise
        call.update(
            dispatch_status="COMPLETED",
            response_sha256=response_sha256,
            response_path=str(response_path),
        )
        return completion

    tracked_llm_call.cpa_cache_identity = cache_identity  # type: ignore[attr-defined]
    tracked_llm_call.provider_runtime_binding = dict(runtime_binding)  # type: ignore[attr-defined]

    def replay_cached_call(prompt: str) -> str:
        cached_calls = cached_review.get("cpa_calls") if isinstance(cached_review, Mapping) else None
        if not isinstance(cached_calls, list) or replay_index[0] >= len(cached_calls):
            raise ValueError("POSTBURN_SUBTITLE_AUDIO_CPA_CACHE_CALL_MISSING")
        cached_call = cached_calls[replay_index[0]]
        if not isinstance(cached_call, Mapping):
            raise ValueError("POSTBURN_SUBTITLE_AUDIO_CPA_CACHE_CALL_INVALID")
        cached_prompt = _read_postburn_cpa_text(
            cached_call.get("prompt_path"), cached_call.get("prompt_sha256")
        )
        from .prompt_materials import same_prompt_materials
        if not same_prompt_materials(cached_prompt, prompt):
            raise ValueError("POSTBURN_SUBTITLE_AUDIO_CPA_CACHE_PROMPT_BYTES_DRIFT")
        completion = _read_postburn_cpa_text(
            cached_call.get("response_path"), cached_call.get("response_sha256")
        )
        replay_index[0] += 1
        return completion

    replay_cached_call.cpa_cache_identity = cache_identity  # type: ignore[attr-defined]
    replay_cached_call.provider_runtime_binding = dict(runtime_binding)  # type: ignore[attr-defined]
    from .completed_review_checkpoint import inherit_ruling_context
    inherit_ruling_context(tracked_llm_call, final_review_llm)
    inherit_ruling_context(replay_cached_call, final_review_llm)

    def replay_or_complete_candidate_risk(prompt: str) -> str:
        from src.autoslice.final_review_candidate_risk import RISK_PROMPT_MARKER
        if RISK_PROMPT_MARKER not in prompt:
            raise ValueError("POSTBURN_CANDIDATE_RISK_REQUEST_INVALID")
        calls = cached_review["cpa_calls"]
        if replay_index[0] < len(calls):
            return replay_cached_call(prompt)
        # All original requests are complete and have been re-read byte for
        # byte. This is a distinct, source-bound risk question, not a resend
        # or a replacement of the saved discovery response.
        if not cpa_calls:
            cpa_calls.extend({**dict(call), "served_from_cache": True} for call in calls)
        return tracked_llm_call(prompt)

    inherit_ruling_context(replay_or_complete_candidate_risk, final_review_llm)
    findings: list[dict[str, object]] = []
    error: BaseException | None = None
    error_reason_code: str | None = None
    cached_review = existing.get("postburn_subtitle_audio_review")
    replay_index = [0]
    cache_reused = False
    cached_parser_recovery = False
    prior_review_for_audit: Mapping[str, object] | None = None
    try:
        cached_request = (
            cached_review.get("cpa_request")
            if isinstance(cached_review, Mapping)
            else None
        )
        prior_review_present = "postburn_subtitle_audio_review" in existing
        cached_request_sha = (
            cached_request.get("request_sha256")
            if isinstance(cached_request, Mapping)
            else None
        )
        same_request = cached_request_sha == request["request_sha256"]
        prior_complete = _postburn_cached_review_complete(cached_review)
        completed_raw_pending = bool(isinstance(cached_review, Mapping)
            and cached_review.get("status") == "RUNNING"
            and (cached_review.get("completion_recovery") or {}).get("status") == "TRANSPORT_COMPLETE_RAW_PENDING_PARSE")
        ambiguous_prior = (
            prior_review_present
            and (
                not isinstance(cached_review, Mapping)
                or not isinstance(cached_request, Mapping)
                or not isinstance(cached_request_sha, str)
                or not cached_request_sha
                or not prior_complete
            )
        )
        if ambiguous_prior:
            # An old or incomplete dispatch has no safe way to establish that
            # the provider did not receive the request. Preserve it and stop.
            prior_review_for_audit = (
                cached_review if isinstance(cached_review, Mapping) else None
            )
            error = ValueError("POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY")
            error_reason_code = "POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY"
        elif prior_review_present and same_request:
            prior_review_for_audit = cached_review
            try:
                replay_findings = audit_final_subtitles(
                    final_text,
                    llm_call=replay_cached_call,
                    extract_json=extract_json_object,
                    glossary_text=review_glossary,
                    structured_context_text=context_text,
                    candidate_context_text=candidate_context_text,
                    candidate_context=clip_context,
                    editorial_rendered_cues=editorial_cues,
                    source_media_timeline_offset_ms=timeline_offset_ms,
                    candidate_risk_llm_call=replay_or_complete_candidate_risk,
                    **({"discovery_batch": batch_from_rows(
                        review_extra_findings, context=clip_context, source_offset_ms=timeline_offset_ms)}
                        if os.environ.get("AUTOSLICE_FINAL_REVIEW_MODE") == "per_finding" else {
                            "extra_raw_findings": review_extra_findings,
                            "prioritize_extra_raw_findings": bool(review_extra_findings)}),
                )
                cached_parser_recovery = bool(completed_raw_pending or (
                    cached_review.get("status") == "AUDITOR_UNAVAILABLE"
                    and cached_review.get("error_type") == "FinalReviewAuditError"
                    and cached_review.get("reason_codes")
                    == ["POSTBURN_SUBTITLE_AUDIO_CPA_UNAVAILABLE"]
                    and replay_findings
                    and not cached_review["findings"]
                ))
                if replay_index[0] != len(cached_review["cpa_calls"]):
                    calls = cached_review["cpa_calls"]
                    if not (cached_parser_recovery and replay_index[0] == 1 and len(calls) == 2):
                        raise ValueError("POSTBURN_SUBTITLE_AUDIO_CPA_CACHE_CALL_COUNT_DRIFT")
                    original_prompt = _read_postburn_cpa_text(
                        calls[0].get("prompt_path"), calls[0].get("prompt_sha256")
                    )
                    retry_prompt = _read_postburn_cpa_text(
                        calls[1].get("prompt_path"), calls[1].get("prompt_sha256")
                    )
                    if not is_schema_repair_prompt_for(retry_prompt, original_prompt):
                        raise ValueError("POSTBURN_SUBTITLE_AUDIO_CPA_CACHE_CALL_COUNT_DRIFT")
                from src.autoslice.editorial_subtitle import withdrawn_generic_gift_nomination
                cached_findings = cached_review.get("discovered_findings", cached_review["findings"])
                cached_findings = [row for row in cached_findings
                                   if not withdrawn_generic_gift_nomination(row, final_text)]
                from src.autoslice.final_review_candidate_risk import candidate_risk_parser_successor
                risk_parser_recovery = candidate_risk_parser_successor(cached_findings, replay_findings)
                if not cached_parser_recovery and replay_findings != cached_findings and not risk_parser_recovery:
                    raise ValueError(
                        "POSTBURN_SUBTITLE_AUDIO_CPA_CACHE_FINDINGS_DRIFT"
                    )
                findings = replay_findings
                if not cpa_calls:
                    cpa_calls = [
                        {**dict(call), "served_from_cache": True}
                        for call in cached_review["cpa_calls"][:replay_index[0]]
                        if isinstance(call, Mapping)
                    ]
                cached_parser_recovery = cached_parser_recovery or (risk_parser_recovery and replay_findings != cached_findings)
                cache_reused = True
            except Exception as exc:
                error = ValueError("POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY")
                error.__cause__ = exc
                error_reason_code = "POSTBURN_SUBTITLE_AUDIO_CPA_AMBIGUOUS_REPLAY"
        elif completed_raw_pending and not same_request:
            prior_review_for_audit = cached_review
            error = ValueError("POSTBURN_COMPLETED_RAW_REQUIRES_ORIGINAL_INPUT")
            error_reason_code = "POSTBURN_COMPLETED_RAW_REQUIRES_ORIGINAL_INPUT"
        elif prior_review_present and not same_request and prior_complete:
            # A changed, fully completed request may be sent once, but retain
            # the old receipt so the input transition remains auditable.
            prior_review_for_audit = cached_review

        if error is None and not cache_reused:
            # A missing prior review, or a clearly different bound input, may
            # make one fresh normal call.  Same-input incomplete/ambiguous
            # dispatches have already taken the blocking branch above.
            findings = audit_final_subtitles(
                final_text,
                llm_call=tracked_llm_call,
                extract_json=extract_json_object,
                glossary_text=review_glossary,
                structured_context_text=context_text,
                candidate_context_text=candidate_context_text,
                candidate_context=clip_context,
                editorial_rendered_cues=editorial_cues,
                source_media_timeline_offset_ms=timeline_offset_ms,
                **({"discovery_batch": batch_from_rows(
                    review_extra_findings, context=clip_context, source_offset_ms=timeline_offset_ms)}
                    if os.environ.get("AUTOSLICE_FINAL_REVIEW_MODE") == "per_finding" else {
                        "extra_raw_findings": review_extra_findings,
                        "prioritize_extra_raw_findings": bool(review_extra_findings)}),
            )
    except Exception as exc:
        if error is None:
            error = exc
            error_reason_code = "POSTBURN_SUBTITLE_AUDIO_CPA_UNAVAILABLE"

    discovered_findings = deepcopy(findings)
    if error is None:
        if cache_reused:
            from src.autoslice.cpa_runtime import record_cpa_event
            record_cpa_event('cache_hit',{'kind':'postburn_review','stage':'postburn_subtitle_audio'})
        findings=_postburn_reuse_current_gift_risk(findings,preburn=existing,final_text=final_text)
    findings, disclosed_findings = _postburn_current_risk_partition(findings) if error is None else (findings, [])
    # Every open witness nomination must be on the exact-final agenda. One the
    # reviewer dropped, capped or could not parse is an unresolved wording
    # question, never silent: the receipt blocks release on it.
    # R2: a lone ASR disagreement is material, not an open task. Keep the
    # raw nomination for diagnostics; enforce the existing unqueued-task gate
    # only on nominations the registry actually admitted (possibly via A/G).
    material_only_witness_ids: list[str] = []
    if os.environ.get("AUTOSLICE_FINAL_REVIEW_MODE") == "per_finding":
        admitted_witness_cues = {row.get("cue_index") for row in discovered_findings
            if isinstance(row, Mapping) and "I" in (row.get("discovery_task") or {}).get("channels", ())}
        material_only_witness_ids = [row["nomination_id"] for row in open_witness_nominations
            if row["cue_index"] not in admitted_witness_cues]
        open_witness_nominations = [row for row in open_witness_nominations
            if row["nomination_id"] not in material_only_witness_ids]
    queued_witness_ids = {
        (row.get("candidate_provenance") or {}).get("nomination_id")
        for row in findings if isinstance(row, Mapping)
        and isinstance(row.get("candidate_provenance"), Mapping)
    }
    unqueued_witness = [
        row["nomination_id"] for row in open_witness_nominations
        if row["nomination_id"] not in queued_witness_ids
    ] if error is None else []
    postburn = {
        "schema_version": "postburn-subtitle-audio-review.v1",
        "meaning_policy": MEANING_POLICY_VERSION,
        "status": (
            "AUDITOR_UNAVAILABLE" if error is not None
            else "FLAGGED" if findings or unqueued_witness else "CLEAN"
        ),
        "release_gate": "BLOCK" if error is not None or findings or unqueued_witness else "PASS",
        "reason_codes": (
            [error_reason_code or "POSTBURN_SUBTITLE_AUDIO_CPA_UNAVAILABLE"] if error is not None
            else [
                *(["POSTBURN_SUBTITLE_AUDIO_CPA_FINDINGS"] if findings else []),
                *(["POSTBURN_WITNESS_DISAGREEMENT_UNRESOLVED"] if unqueued_witness else []),
            ]
        ),
        "witness_disagreement": {
            "schema_version": "postburn-witness-disagreement.v1",
            "nomination_count": len(witness_nominations),
            "material_only_nomination_ids": material_only_witness_ids,
            "decided_nomination_ids": sorted(decided_witness_ids),
            "open_nomination_ids": [row["nomination_id"] for row in open_witness_nominations],
            "unqueued_nomination_ids": unqueued_witness,
            "nominations": deepcopy(open_witness_nominations),
        },
        "reviewed_srt_sha256": reviewed_srt_sha256,
        "validated_finding_count": len(findings),
        "findings": findings,
        "discovered_findings": discovered_findings,
        "unresolved_findings_disclosed": deepcopy(disclosed_findings),
        "cache_reused": cache_reused,
        **({"cached_parser_recovery": True} if cached_parser_recovery and cache_reused else {}),
        "cpa_cache_identity": cache_identity,
        "subtitle_audio_binding": subtitle_audio_binding,
        "cpa_request": {
            **request,
        },
        "cpa_calls": cpa_calls,
        "provider_runtime_binding": (
            dict(runtime_binding)
        ),
        **({"source_context_discovery_audit": source_context_audit}
           if source_context_audit else {}),
        **(
            {"prior_postburn_subtitle_audio_review": dict(
                wire_prior_postburn
                if isinstance(wire_prior_postburn, Mapping)
                else (replay_failure_history or prior_review_for_audit)
            )}
            if isinstance(wire_prior_postburn, Mapping)
            or isinstance(replay_failure_history or prior_review_for_audit, Mapping)
            else {}
        ),
        **({"error_type": type(error).__name__} if error is not None else {}),
    }
    merged_audit = deepcopy(dict(existing))
    merged_findings = [
        row for row in merged_audit.get("findings") or [] if isinstance(row, Mapping)
    ]
    merged_findings.extend(findings)
    merged_audit["postburn_subtitle_audio_review"] = postburn
    merged_audit["findings"] = merged_findings
    merged_audit["validated_finding_count"] = len(merged_findings)
    merged_audit["unresolved_findings_disclosed"] = [
        *(merged_audit.get("unresolved_findings_disclosed") or []),
        *deepcopy(disclosed_findings),
    ]
    if postburn["release_gate"] != "PASS":
        merged_audit["release_gate"] = "BLOCK"
        merged_audit["status"] = postburn["status"]
        merged_audit["reason_codes"] = sorted(
            {str(code) for code in [*(merged_audit.get("reason_codes") or []), *postburn["reason_codes"]]}
        )
    record["postburn_subtitle_audio_review"] = postburn
    chat_authority_audit["postburn_subtitle_audio_review"] = postburn
    chat_authority_audit["final_review_audit"] = merged_audit
    persist_review_audit(review_audit_path, merged_audit)
    _persist_review_document(chat_authority_path, chat_authority_audit)
    preserve_postburn_observation(out_root, cid)
    record["chat_authority_audit_path"] = str(chat_authority_path)
    record.setdefault("artifact_hashes", {})[
        "chat_authority_audit_sha256"
    ] = "sha256:" + _sha256(chat_authority_path)
    if error is not None:
        raise SystemExit(
            "POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED: "
            f"{type(error).__name__}: {review_audit_path}"
        ) from error
    if findings or unqueued_witness:
        raise SystemExit(
            "POSTBURN_SUBTITLE_AUDIO_REVIEW_BLOCKED: "
            f"{review_audit_path}"
        )
    return record


def _stage_record(
    *,
    options: ProducerFinalizationOptions,
    spec: dict,
    cid: str,
    recut: FinalRecutArtifacts,
    record: dict,
    adapters: ProducerFinalizationAdapters,
    completed: CompletedFinalizationArtifacts | None = None,
) -> StagedRecord:
    if completed is not None and completed.prepared_handle is not None:
        from src.autoslice.producer_prepared_recovery import stage_completed_editorial
        retained = stage_completed_editorial(
            completed=completed, spec=spec, cid=cid, recut=recut, record=record,
        )
        if retained is not None:
            return retained
    recut_dir = recut.recut_dir
    subtitle_path = recut.subtitle_path
    given_title = spec.get("given_title")
    recovery_publication_authority = spec.get(
        "recovery_publication_authority"
    )
    if bool(given_title) != bool(recovery_publication_authority):
        raise SystemExit("RECOVERY_PUBLICATION_AUTHORITY_PAIR_INVALID")
    normalized_recovery_publication_authority = None
    if given_title:
        try:
            normalized_recovery_publication_authority = (
                validate_recovery_publication_authority(
                    recovery_publication_authority,
                    candidate_id=cid,
                    expected_final_title=str(given_title),
                )
            )
        except RecoveryTitleAuthorityError as exc:
            raise SystemExit(
                f"RECOVERY_PUBLICATION_AUTHORITY_INVALID:{exc}"
            ) from exc
    # Title LLM runs only when no manual body exists. A manual body is not
    # rewritten, but it still passes the shared archive-envelope/structure gate.
    # Cover art direction remains independent of title authorship and uses the
    # current configured adapter plus deterministic fallback; do not pin model
    # names here because runtime/provider selection is live configuration.
    title_llm = None if given_title else build_publication_llm_call(effort="high")
    art_direction_llm = None if options.reuse_cover else build_publication_llm_call(effort="medium")
    source_fact_llm = build_publication_llm_call(effort="high")
    final_title_cues = [
        SourceCue(
            f"text_final_{index:04d}", cue.start_ms, cue.end_ms, cue.text.strip(),
            "zh", "speech", 1.0,
        )
        for index, cue in enumerate(parse_srt_cues(subtitle_path.read_text(encoding="utf-8")), start=1)
        if cue.text.strip()
    ]
    transcript_text = "\n".join(cue.text for cue in final_title_cues)
    clip_context_path: Path | None = None
    clip_context = spec.get("clip_context")
    if isinstance(clip_context, Mapping):
        raw_context_path = spec.get("clip_context_path")
        if not isinstance(raw_context_path, str) or not raw_context_path:
            raise RuntimeError("CLIP_CONTEXT_PATH_MISSING")
        clip_context_path = Path(raw_context_path)
        if clip_context_path.is_symlink() or not clip_context_path.is_file():
            raise RuntimeError("CLIP_CONTEXT_FILE_INVALID")
        try:
            file_context = json.loads(clip_context_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("CLIP_CONTEXT_FILE_INVALID") from exc
        if file_context != clip_context:
            raise RuntimeError("CLIP_CONTEXT_FILE_PAYLOAD_MISMATCH")
        validate_clip_context(
            file_context,
            candidate_id=cid,
            recording_date=str(spec.get("date") or ""),
            source_media_sha256s=[
                str(piece.get("source_media_sha256"))
                for piece in (spec.get("pieces") or [])
                if isinstance(piece, Mapping) and piece.get("source_media_sha256")
            ],
        )
    cover_reference_authority = load_candidate_cover_reference(cid, ledger_path=CHANNEL_PROFILE.asset_file(
        "cover_reference_overrides", repo_root=ROOT
    ))
    story_selection_hook = canonicalize_relation_summary(
        str(spec.get("selection_hook") or ""),
        session_relation_authority=spec.get("session_relation_authority"),
        transcript_text=transcript_text,
    )
    story_selection_scorecard = canonicalize_story_scorecard(
        spec.get("selection_scorecard"),
        session_relation_authority=spec.get("session_relation_authority"),
        transcript_text=transcript_text,
    )
    rescore_provenance = None
    def build_story_contract_for_hook(reviewed_hook: str) -> dict[str, object]:
        contract = build_story_contract(
            candidate_id=cid,
            selection_hook=reviewed_hook,
            transcript_text=transcript_text,
            selection_scorecard=story_selection_scorecard,
            session_relation_authority=spec.get("session_relation_authority"),
            cover_reference_authority=cover_reference_authority,
            source_media_sha256s=[
                str(piece.get("source_media_sha256"))
                for piece in (spec.get("pieces") or [])
                if isinstance(piece, Mapping) and piece.get("source_media_sha256")
            ],
            clip_context=spec.get("clip_context"),
            recording_date=str(spec.get("date") or ""),
            boundary_semantic_review=spec.get("boundary_semantic_review"),
            human_boundary_authority=str(spec.get("given_end_authority") or ""),
        )
        contract["input_audits"] = [
            audit_story_artifact(reviewed_hook, story_contract=contract,
                                 artifact_kind="selection_hook"),
            audit_story_artifact(transcript_text, story_contract=contract,
                                 artifact_kind="subtitle"),
        ]
        bind_story_contract_provenance(contract, rescore_provenance)
        return contract

    story_contract = build_story_contract_for_hook(story_selection_hook)
    record["selection_scorecard"] = story_selection_scorecard
    try:
        rescore_provenance = bind_finalization_provenance(
            spec=spec, record=record, story_contract=story_contract,
            selection_hook=story_selection_hook,
            selection_scorecard=story_selection_scorecard,
        )
    except SourceFactRescoreProvenanceError as exc:
        raise SystemExit(str(exc)) from exc
    record["session_relation_authority"] = spec.get("session_relation_authority")
    record["story_contract"] = story_contract
    if normalized_recovery_publication_authority is not None:
        record["recovery_publication_authority"] = normalized_recovery_publication_authority
    if clip_context_path is not None:
        record["clip_context_path"] = str(clip_context_path)
        record["clip_context_payload_sha256"] = clip_context.get("context_sha256")
        record.setdefault("artifact_hashes", {})["clip_context_file_sha256"] = (
            "sha256:" + _sha256(clip_context_path)
        )
    reuse_cover_source_record: Path | None = None
    raw_reuse_cover_source_record = spec.get("reuse_cover_source_record")
    if raw_reuse_cover_source_record is not None:
        reuse_cover_source_record = Path(str(raw_reuse_cover_source_record))
        if (
            not reuse_cover_source_record.is_absolute()
            or reuse_cover_source_record.is_symlink()
            or not reuse_cover_source_record.is_file()
        ):
            raise SystemExit("REUSE_COVER_SOURCE_RECORD_INVALID")
    record = adapters.stage_publish_draft(
        record,
        candidate_id=cid,
        title=given_title or cid,
        cues=final_title_cues,
        run_ffmpeg=True,
        title_llm_call=title_llm,
        art_direction_llm_call=art_direction_llm,
        source_fact_llm_call=source_fact_llm,
        story_contract_rebuilder=build_story_contract_for_hook,
        skip_cover=options.reuse_cover,
        reuse_cover_source_record=reuse_cover_source_record,
        selection_hook=story_selection_hook,
        cover_diversity_slot=spec.get("cover_diversity_slot"),
        recovery_publication_authority=normalized_recovery_publication_authority,
    )
    try:
        bind_publish_staging_provenance(record, rescore_provenance)
    except SourceFactRescoreProvenanceError as exc:
        raise SystemExit(str(exc)) from exc
    staging = record.get("publish_staging") or {}
    source_fact_review = staging.get("source_fact_review")
    if (
        staging.get("title_authority_status")
        == "BLOCKED_SOURCE_FACT_REVIEW"
        or (
            isinstance(source_fact_review, Mapping)
            and not source_fact_review_passes(source_fact_review)
        )
    ):
        reason = (
            str(source_fact_review.get("reason_code") or "unknown")
            if isinstance(source_fact_review, Mapping)
            else "missing_receipt"
        )
        # 狍哥案修复：
        # marker 分类本身是纯函数，抽到 selection_rescore.py 独立可测；
        # RESCORE_REQUIRED 一支还要落 pending sidecar，留在这个薄调用点。
        marker = selection_rescore.classify_source_fact_review_marker(
            source_fact_review
        )
        if marker == "SOURCE_FACT_REPAIRED_RESCORE_REQUIRED" and isinstance(
            source_fact_review, Mapping
        ):
            selection_rescore.consume_producer_rescore(spec=spec,cid=cid,recut_dir=recut_dir,
                record=record,review=source_fact_review,cues=final_title_cues,
                context_prompt=clip_context_prompt_text(clip_context) if isinstance(clip_context,Mapping) else '',
                stale_card=(record.get('story_contract') or {}).get('selection_scorecard'))
            return _stage_record(options=options,spec=spec,cid=cid,recut=recut,record=record,adapters=adapters)
        raise SystemExit(f"{marker}: {reason}")
    raw_final_story_contract = record.get("story_contract")
    if not isinstance(raw_final_story_contract, Mapping):
        raise SystemExit("STORY_CONTRACT_FINAL_MISSING")
    # Source-fact review may replace both the title and the selection hook.
    # From this point onward there must be only one active StoryContract: the
    # post-review contract returned by staging.  Copy it into the record so
    # cover audits and persisted evidence cannot accidentally retain the
    # pre-review local variable built above.
    final_story_contract = dict(raw_final_story_contract)
    record["story_contract"] = final_story_contract
    if isinstance(source_fact_review, Mapping):
        bound_source_fact_review = final_story_contract.get(
            "source_fact_review"
        )
        if (
            not isinstance(bound_source_fact_review, Mapping)
            or dict(bound_source_fact_review) != dict(source_fact_review)
        ):
            raise SystemExit(
                "STORY_CONTRACT_SOURCE_FACT_BINDING_MISSING_OR_STALE"
            )
        final_selection_hook = str(
            source_fact_review.get("final_selection_hook") or ""
        )
        if (
            final_story_contract.get("selection_hook")
            != final_selection_hook
        ):
            raise SystemExit(
                "STORY_CONTRACT_SOURCE_FACT_HOOK_MISMATCH"
            )
        expected_selection_hook_sha256 = (
            "sha256:"
            + hashlib.sha256(
                final_selection_hook.encode("utf-8")
            ).hexdigest()
        )
        if (
            final_story_contract.get("selection_hook_sha256")
            != expected_selection_hook_sha256
        ):
            raise SystemExit(
                "STORY_CONTRACT_SOURCE_FACT_HASH_MISMATCH"
            )
    input_violations = [
        violation
        for audit in (final_story_contract.get("input_audits") or [])
        if isinstance(audit, Mapping)
        for violation in (audit.get("violations") or [])
        if isinstance(violation, dict)
    ]
    if input_violations:
        reason_codes = sorted(
            {
                str(
                    row.get("reason_code")
                    or "STORY_CONTRACT_INPUT_INVALID"
                )
                for row in input_violations
            }
        )
        raise SystemExit(
            "STORY_CONTRACT_INPUT_INVALID: " + ",".join(reason_codes)
        )
    if (
        normalized_recovery_publication_authority is not None
        and (
            staging.get("title")
            != str(given_title)
            or staging.get("recovery_publication_authority")
            != normalized_recovery_publication_authority
        )
    ):
        raise SystemExit(
            "RECOVERY_PUBLICATION_STAGING_BINDING_MISMATCH"
        )
    if staging.get("title_authority_status") == "BLOCKED_STORY_CONTRACT":
        raise SystemExit(
            "STORY_CONTRACT_TITLE_FAILED: "
            + str(staging.get("title_authority_error") or "unknown")
        )
    cover_generation = staging.get("cover_generation")
    private_reuse_ready = (
        staging.get("cover_status") == "REUSED_COVER"
        and isinstance(cover_generation, Mapping)
        and cover_generation.get("private_reuse_schema_version")
        == "normal-private-cover-reuse.v1"
    )
    if staging.get("cover_status") == "AI_COVER_READY" or private_reuse_ready:
        cover_reason_codes, cover_story_audits = _audit_story_bound_cover(
            staging, final_story_contract
        )
        final_story_contract["cover_output_audits"] = cover_story_audits
        if cover_reason_codes:
            raise SystemExit(
                "STORY_CONTRACT_COVER_FAILED: " + ",".join(cover_reason_codes)
            )






    if not str(staging.get("title_authority_status") or "").startswith("UNRESOLVED"):
        record["upload_tags"] = adapters.generate_upload_tags(
            str(staging.get("title") or given_title or cid), subtitle_path, timeout=180.0
        )
    validate_staged_editorial_files(record, staging, candidate_id=cid, subtitle_path=subtitle_path)
    publish_json_path = (
        Path(str(staging.get("publish_json_path")))
        if staging.get("publish_json_path")
        else None
    )
    if publish_json_path is not None:
        if publish_json_path.is_symlink() or not publish_json_path.is_file():
            raise SystemExit("PUBLISH_DRAFT_FILE_INVALID")
        record.setdefault("artifact_hashes", {})[
            "publish_draft_sha256"
        ] = "sha256:" + _sha256(publish_json_path)
    record_path = recut_dir / f"{cid}.record.json"
    with record_path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    if str(staging.get("title_authority_status") or "").startswith("UNRESOLVED"):
        # The standalone producer is also a supported entry point.  Never
        # materialize delivery bytes and rely on the unattended runner to
        # notice and delete them afterward.  The publish/record evidence above
        # remains available for classification and bounded retry.
        raise SystemExit(
            f"TITLE_AUTHORITY_UNRESOLVED: {staging.get('title_authority_error') or 'unknown'}"
        )
    return StagedRecord(record=record, staging=staging, record_path=record_path)

def _deliver_staged_record(
    *,
    spec: dict,
    cid: str,
    final_end: int,
    audit: dict,
    timing_qa: dict,
    recut: FinalRecutArtifacts,
    speaker: SpeakerArtifacts,
    authority: AuthorityArtifacts,
    chat_authority_path: Path,
    staged: StagedRecord,
    adapters: ProducerFinalizationAdapters,
    talk_filler_audit_path: Path | None = None,
) -> int:
    record = staged.record
    staging = staged.staging
    record_path = staged.record_path
    subtitle_path = recut.subtitle_path
    text_manifest_path = recut.text_manifest_path
    redelivery_baseline_audit_path = recut.redelivery_baseline_audit_path
    speaker_manifest = speaker.manifest
    speaker_review_srt = speaker.review_srt
    speaker_ass = speaker.ass
    speaker_manifest_path = speaker.manifest_path
    subtitle_regression_audit_path = authority.subtitle_regression_audit_path
    subtitle_regression_audit = authority.subtitle_regression_audit
    validate_staged_editorial_files(record, staging, candidate_id=cid, subtitle_path=subtitle_path)
    recut_provenance_path = _recut_provenance(record)
    clip_context_path = (
        Path(str(record.get("clip_context_path")))
        if record.get("clip_context_path")
        else None
    )
    cover_generation = (
        staging.get("cover_generation")
        if isinstance(staging.get("cover_generation"), dict)
        else {}
    )
    rendered_text_pixels = cover_generation.get("rendered_text_pixels")
    cover_title_mask_path = (
        Path(str(rendered_text_pixels.get("mask_path")))
        if isinstance(rendered_text_pixels, dict)
        and rendered_text_pixels.get("mask_path")
        else None
    )
    cover_pre_overlay_path = (
        Path(str(cover_generation.get("pre_overlay_path")))
        if cover_generation.get("pre_overlay_path")
        else None
    )
    cover_route_background_path = (
        Path(str(cover_generation.get("ai_background")))
        if cover_generation.get("ai_background")
        else None
    )
    publish_json_path = (
        Path(str(staging.get("publish_json_path")))
        if staging.get("publish_json_path")
        else None
    )
    delivery = adapters.delivery_root() / spec["date"]
    delivery.mkdir(parents=True, exist_ok=True)
    name = spec.get("delivery_name") or cid
    # Old sapphire renders may coexist in replacement_recuts; copy only the
    # exact hash-bound speaker burn made by this run.
    burned = _validated_burned_artifact(record)
    uniform_host_ass = (
        _validated_burned_ass_artifact(record)
        if speaker_ass is None
        else None
    )
    adapters.run_command(["cp", str(burned), str(delivery / f"{name}.mp4")])
    adapters.run_command(["cp", str(subtitle_path), str(delivery / f"{name}.srt")])
    copy_subtitle_audio_artifacts(
        record, candidate_id=cid, delivery=delivery, name=name,
        run_command=adapters.run_command,
    )
    for source, suffix in (
        (uniform_host_ass, ".final-sapphire72.ass"),
        (speaker_review_srt, ".speaker.srt"),
        (speaker_ass, ".speaker.ass"),
        (speaker_manifest_path, ".speaker.json"),
        (chat_authority_path, ".chat-authority.json"),
        (redelivery_baseline_audit_path, ".redelivery-baseline.json"),
        (subtitle_regression_audit_path, ".subtitle-regression.json"),
        (talk_filler_audit_path, ".filler-audit.json"),
        (text_manifest_path, ".text-finalization.json"),
        (recut_provenance_path, ".provenance.json"),
        (clip_context_path, ".clip-context.json"),
        (cover_title_mask_path, ".cover.title-mask.png"),
        (cover_pre_overlay_path, ".cover.pre-overlay.png"),
        (cover_route_background_path, ".cover.route-background.png"),
        (Path(str(cover_generation["request_path"])) if cover_generation.get("request_sha256") else None, ".cover-request.json"),
        (Path(str(cover_generation["response_path"])) if cover_generation.get("response_sha256") else None, ".cover-response.json"),
        (publish_json_path, ".publish.json"),
        (record_path, ".record.json"),
    ):
        if source is not None and source.is_file():
            adapters.run_command(["cp", str(source), str(delivery / f"{name}{suffix}")])
    if cover_generation.get("method") == "skill.candidates":
        from .title_cover_candidates import copy_choice_bundle
        copy_choice_bundle(cover_generation, delivery / f"{name}.title-cover")
    cover = staging.get("cover_path")
    if cover and Path(cover).is_file():
        adapters.run_command(["cp", str(cover), str(delivery / f"{name}.cover.png")])

    emit_talk_delivery_summary(talk_delivery_summary(
        candidate_id=cid, final_end=final_end, record=record, audit=audit,
        timing_qa=timing_qa, staging=staging, delivery=delivery / name,
        speaker_review_srt=speaker_review_srt, speaker_ass=speaker_ass,
        speaker_manifest=speaker_manifest, speaker_guess=speaker_guess,
        subtitle_regression_audit=subtitle_regression_audit,
        redelivery_baseline_audit=recut.redelivery_baseline_audit,
        talk_filler_audit_path=talk_filler_audit_path,
    ))
    return 0


def finalize_producer_package(
    *,
    options: ProducerFinalizationOptions,
    profile_id: str,
    speaker_subtitle_style_id: str,
    spec: dict,
    cid: str,
    out_root: Path,
    host: str,
    padded: Path,
    padded_provenance_path: Path,
    piece_provenance_rows: list[dict],
    final_start: int,
    final_end: int,
    sanitized: list[SourceCue],
    timing_qa: dict,
    audit: dict,
    text_override_path: Path | None,
    subtitle_regression_path: Path | None,
    chat_authority_audit: dict,
    chat_authority_path: Path,
    branding_intro: dict[str, object] | None,
    adapters: ProducerFinalizationAdapters,
    delivery_lane: str = "talk",
    talk_filler_audit_path: Path | None = None,
    reviewed_baseline_replay_c12_projection: C12FinalDeliveryProjection | None = None,
    completed: CompletedFinalizationArtifacts | None = None,
) -> int:
    if completed is not None and (not options.prepare_only or delivery_lane != "talk"):
        raise SystemExit("PREPARED_RESUME_REQUIRES_PRIVATE_TALK")
    if (
        reviewed_baseline_replay_c12_projection is not None
        and options.substrate != "reviewed-baseline-replay"
    ):
        raise SystemExit("REPLAY_C12_PRIVATE_PROJECTION_SUBSTRATE_INVALID")
    profile_lane = "song" if str(delivery_lane or "").strip().lower() == "song" else "talk"
    clip_form = clip_profile_from_spec(spec, lane=profile_lane)
    _require_delivery_branding_intro(
        delivery_lane=delivery_lane,
        branding_intro=branding_intro,
        clip_profile=clip_form,
    )
    if completed is not None and (
        spec_clip_profile_transcription(spec, lane=profile_lane)
        != holder_clip_profile_transcription(completed.record, lane=profile_lane)
    ):
        raise SystemExit("CLIP_PROFILE_INVALID: prepared record differs from its spec")
    if options.prepare_only and str(delivery_lane).strip().lower() == "talk":
        # Preparation requires this runtime seal even when all media/CPA work
        # succeeds.  Resolve it now using the preparation consumer's exact
        # rule, before spending on recut, review, burn or title/cover work.
        deployment_authority_binding(resolve_talk_delivery_runtime_root(spec))
    recut = completed.recut if completed is not None else _materialize_final_recut(
        spec=materialization_spec_for_selected_projection(spec, audit),
        cid=cid,
        out_root=out_root,
        padded=padded,
        padded_provenance_path=padded_provenance_path,
        piece_provenance_rows=piece_provenance_rows,
        final_start=final_start,
        final_end=final_end,
        sanitized=sanitized,
        timing_qa=timing_qa,
        text_override_path=text_override_path,
        adapters=adapters,
        spec_parent=options.spec.parent,
        chat_authority_audit=chat_authority_audit,
        reviewed_baseline_replay_c12_projection=(
            reviewed_baseline_replay_c12_projection
        ),
        allow_existing_record=options.substrate == "reviewed-baseline-replay" and options.prepare_only,
        chat_authority_path=chat_authority_path,
    )
    current_scene_spec = recut.nested_scene_spec or spec
    acoustic_requested = (
        spec.get("nested_media_acoustic") is not None
        or current_scene_spec.get("watched_media") is True
        or current_scene_spec.get("nested_media_external_audio_scene") is True
    )

    def refresh_audio_roles() -> None:
        # A typed exact repair may change the whole SRT binding and cue IDs.
        # Withdraw old roles before gathering this pass's current evidence.
        chat_authority_audit.pop("caption_audio_roles", None)
        if acoustic_requested:
            previous_acoustic = chat_authority_audit.get("nested_media_acoustic_observation", {})
            acoustic_audit = _run_current_nested_acoustic_observation(
                spec=spec, recut=recut, cid=cid, final_start=final_start,
                final_end=final_end, out_root=out_root, adapters=adapters,
                previous_audit=previous_acoustic if isinstance(previous_acoustic, Mapping) else None,
            )
            if isinstance(previous_acoustic, Mapping):
                acoustic_audit = retain_current_acoustic_observations(
                    candidate_id=cid, source_media=recut.media_path,
                    subtitle_path=recut.subtitle_path,
                    previous_audit=previous_acoustic, current_audit=acoustic_audit,
                )
            chat_authority_audit["nested_media_acoustic_observation"] = acoustic_audit
            audit["nested_media_acoustic_observation"] = acoustic_audit
            chat_authority_audit["caption_audio_roles"] = current_caption_audio_roles(
                candidate_id=cid, source_media=recut.media_path, subtitle_path=recut.subtitle_path,
                acoustic_audit=acoustic_audit,
            )

    retained_render_srt_sha256 = _evidence_file_sha256(recut.subtitle_path)
    retained_exact_is_current = False
    if completed is not None and completed.reuse_render:
        try:
            validate_final_review_release(
                completed.exact_review,
                expected_srt_sha256="sha256:" + retained_render_srt_sha256,
            )
            retained_exact_is_current = True
        except FinalReviewContractError:
            # The loader already checked the retained bytes and geometry.
            # A historical PASS may still contain questions the current
            # contract requires the ordinary exact reviewer to consume.
            pass
    exact_final_review = completed.exact_review if retained_exact_is_current else _run_exact_final_review_gate(
        cid=cid,
        out_root=out_root,
        final_start=final_start,
        final_end=final_end,
        recut=recut,
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_authority_path,
        adapters=adapters,
        clip_context=spec.get("clip_context") if isinstance(spec.get("clip_context"), Mapping) else {},
        before_review=refresh_audio_roles,
    )
    final_delivery_boundary_review = exact_final_review.get(
        "boundary_semantic_review"
    )
    if not isinstance(final_delivery_boundary_review, Mapping):
        raise SystemExit("FINAL_DELIVERY_BOUNDARY_REVIEW_MISSING")
    spec["boundary_semantic_review"] = dict(
        final_delivery_boundary_review
    )
    audit["final_delivery_boundary_semantic_review"] = dict(
        final_delivery_boundary_review
    )
    boundary_receipt_replay = exact_final_review.get(
        "boundary_receipt_replay"
    )
    if isinstance(boundary_receipt_replay, Mapping):
        audit["boundary_receipt_replay"] = dict(boundary_receipt_replay)
    def build_burn_record(
        *,
        render_revision_marker: str | None = None,
    ) -> tuple[SpeakerArtifacts, AuthorityArtifacts, dict]:
        if render_revision_marker is None and (chat_authority_audit.get('human_sequence_alignment') or {}).get('records'):
            render_revision_marker = 'human-sequence-aligned-source-v1'
        if acoustic_requested:
            # Final record gets a fresh read of the final pass's receipts, not
            # another model call or an earlier exact/scene PASS.
            chat_authority_audit["caption_audio_roles"] = current_caption_audio_roles(
                candidate_id=cid, source_media=recut.media_path, subtitle_path=recut.subtitle_path,
                acoustic_audit=chat_authority_audit.get("nested_media_acoustic_observation", {}),
            )
        speaker_result = _finalize_speaker(
            options=options,
            spec=spec,
            host=host,
            cid=cid,
            recut=recut,
            final_start=final_start,
            final_end=final_end,
            adapters=adapters,
        )
        authority_result = _verify_final_authority(
            cid=cid,
            final_start=final_start,
            final_end=final_end,
            recut=recut,
            speaker=speaker_result,
            chat_authority_audit=chat_authority_audit,
            chat_authority_path=chat_authority_path,
            subtitle_regression_path=subtitle_regression_path,
            operator_text_full_ownership=resolve_operator_text_full_ownership(spec),
        )
        record_result = _build_and_burn_record(
            options=options,
            profile_id=profile_id,
            speaker_subtitle_style_id=speaker_subtitle_style_id,
            final_start=final_start,
            final_end=final_end,
            recut=recut,
            speaker=speaker_result,
            authority=authority_result,
            chat_authority_audit=chat_authority_audit,
            chat_authority_path=chat_authority_path,
            timing_qa=timing_qa,
            audit=audit,
            branding_intro=branding_intro,
            adapters=adapters,
            delivery_lane=delivery_lane,
            clip_profile=clip_form if CLIP_PROFILE_FIELD in spec else None,
            talk_filler_audit_path=talk_filler_audit_path,
            candidate_id=cid,
            render_revision_marker=render_revision_marker,
        )
        return speaker_result, authority_result, record_result

    clip_context_for_postburn = (
        spec.get("clip_context")
        if isinstance(spec.get("clip_context"), Mapping)
        else None
    )
    if (completed is not None and completed.reuse_render
            and _evidence_file_sha256(recut.subtitle_path) == retained_render_srt_sha256):
        speaker, authority, record = completed.speaker, completed.authority, completed.record
    else:
        speaker, authority, record = build_burn_record(
            render_revision_marker="postburn-typed-srt-v1" if completed is not None else None,
        )
    postburn_reviewed_inputs: set[str] = set()
    typed_resolution_evidence = _current_postburn_resolution_evidence(
        exact_review=exact_final_review,
        reviewed_srt_sha256=_sha256_text(
            recut.subtitle_path.read_text(encoding="utf-8", errors="strict")
        ),
    )
    while True:
        srt_sha_before_postburn = _sha256_text(
            recut.subtitle_path.read_text(encoding="utf-8", errors="strict")
        )
        review_input = _postburn_progress_identity(
            srt_sha_before_postburn, typed_resolution_evidence,
            {key: spec.get(key) for key in (
                "prior_range_acoustic_observations", "prior_screen_context_observations",
                "prior_range_screen_context_observations",
            )},
        )
        if review_input in postburn_reviewed_inputs:
            raise SystemExit("POSTBURN_SUBTITLE_AUDIO_REPAIR_CYCLIC_SRT")
        postburn_reviewed_inputs.add(review_input)
        authority_before_postburn = deepcopy(chat_authority_audit)
        prior_postburn_receipt = deepcopy(
            chat_authority_audit.get("postburn_subtitle_audio_review")
        )
        try:
            record = _run_postburn_subtitle_audio_review(
                cid=cid,
                out_root=out_root,
                recut=recut,
                record=record,
                chat_authority_audit=chat_authority_audit,
                chat_authority_path=chat_authority_path,
                clip_context=clip_context_for_postburn,
                selection_hook=str(spec.get("selection_hook") or ""),
                typed_resolution_evidence=typed_resolution_evidence,
                source_evidence_spec=spec,
                timeline_offset_ms=final_start,
                completed_exact_review=exact_final_review if completed is not None else None,
            )
            break
        except SystemExit:
            postburn_receipt = chat_authority_audit.get(
                "postburn_subtitle_audio_review"
            )
            postburn_findings = (
                postburn_receipt.get("findings")
                if isinstance(postburn_receipt, Mapping)
                else None
            )
            if postburn_receipt == prior_postburn_receipt:
                # The postburn call failed before publishing a new receipt;
                # an older completed finding cannot authorize a second pass.
                raise
            # A manually replayed postburn request may have been built with an
            # empty candidate context.  It is discovery evidence only; normal
            # recovery requires the persisted producer clip-context payload that
            # was bound to this candidate.
            has_bound_clip_context = (
                isinstance(clip_context_for_postburn, Mapping)
                and bool(clip_context_for_postburn)
            )
            recovery = (
                validated_postburn_priority_findings(
                    final_srt_text=recut.subtitle_path.read_text(
                        encoding="utf-8", errors="strict"
                    ),
                    verified_authority_audit=chat_authority_audit,
                    candidate_id=cid,
                    clip_context=clip_context_for_postburn,
                    expected_media_sha256=(
                        str(
                            record.get("burned_preview", {}).get("burned_sha256")
                        )
                        if isinstance(record.get("burned_preview"), Mapping)
                        else None
                    ),
                )
                if (
                    isinstance(postburn_receipt, Mapping)
                    and postburn_receipt.get("status") == "FLAGGED"
                    and postburn_receipt.get("release_gate") == "BLOCK"
                    and isinstance(postburn_findings, list)
                    and postburn_findings
                    and has_bound_clip_context
                )
                else None
            )
            if recovery is None:
                raise
            # Keep the completed postburn receipt as evidence while restoring
            # the ordinary exact-final PASS as the authority input for typed
            # review.  The next loop iteration retains this receipt in the
            # changed-input cache, so every completed round remains linked.
            preserved_postburn = deepcopy(dict(postburn_receipt))
            restored_authority = deepcopy(authority_before_postburn)
            restored_final_review = restored_authority.get("final_review_audit")
            if not isinstance(restored_final_review, dict):
                raise SystemExit("POSTBURN_SUBTITLE_AUDIO_FINAL_REVIEW_AUDIT_MISSING")
            restored_final_review["postburn_subtitle_audio_review"] = preserved_postburn
            restored_authority["postburn_subtitle_audio_review"] = preserved_postburn
            chat_authority_audit.clear()
            chat_authority_audit.update(restored_authority)
            _persist_review_document(chat_authority_path, chat_authority_audit)
            exact_final_review = _run_exact_final_review_gate(
                cid=cid,
                out_root=out_root,
                final_start=final_start,
                final_end=final_end,
                recut=recut,
                chat_authority_audit=chat_authority_audit,
                chat_authority_path=chat_authority_path,
                adapters=adapters,
                clip_context=(
                    spec.get("clip_context")
                    if isinstance(spec.get("clip_context"), Mapping)
                    else {}
                ),
                before_review=refresh_audio_roles,
            )
            repaired_srt_sha256 = _sha256_text(
                recut.subtitle_path.read_text(encoding="utf-8", errors="strict")
            )
            typed_resolution_evidence = _current_postburn_resolution_evidence(
                exact_review=exact_final_review,
                reviewed_srt_sha256=repaired_srt_sha256,
                previous=typed_resolution_evidence,
            )
            if repaired_srt_sha256 == srt_sha_before_postburn and not typed_resolution_evidence:
                raise SystemExit("POSTBURN_SUBTITLE_AUDIO_REPAIR_NO_PROGRESS")
            final_delivery_boundary_review = exact_final_review.get(
                "boundary_semantic_review"
            )
            if not isinstance(final_delivery_boundary_review, Mapping):
                raise SystemExit("FINAL_DELIVERY_BOUNDARY_REVIEW_MISSING")
            spec["boundary_semantic_review"] = dict(final_delivery_boundary_review)
            audit["final_delivery_boundary_semantic_review"] = dict(
                final_delivery_boundary_review
            )
            boundary_receipt_replay = exact_final_review.get(
                "boundary_receipt_replay"
            )
            if isinstance(boundary_receipt_replay, Mapping):
                audit["boundary_receipt_replay"] = dict(boundary_receipt_replay)
            # The new SRT must receive a new speaker/authority/burn/witness
            # chain.  Keep the old postburn receipt in the current audit so the
            # next changed-input review records it as prior evidence.
            current_final_review = chat_authority_audit.get("final_review_audit")
            if not isinstance(current_final_review, dict):
                raise SystemExit("FINAL_REVIEW_EXACT_FINALIZER_MISSING")
            current_final_review["postburn_subtitle_audio_review"] = preserved_postburn
            chat_authority_audit["postburn_subtitle_audio_review"] = preserved_postburn
            _persist_review_document(chat_authority_path, chat_authority_audit)
            if repaired_srt_sha256 != srt_sha_before_postburn:
                speaker, authority, record = build_burn_record(
                    render_revision_marker="postburn-typed-srt-v1"
                )
    staged = _stage_record(
        options=options,
        spec=spec,
        cid=cid,
        recut=recut,
        record=record,
        adapters=adapters,
        completed=completed,
    )
    delivery_kwargs = dict(
        spec=spec,
        cid=cid,
        final_end=final_end,
        audit=audit,
        timing_qa=timing_qa,
        recut=recut,
        speaker=speaker,
        authority=authority,
        chat_authority_path=chat_authority_path,
        staged=staged,
        adapters=adapters,
        talk_filler_audit_path=talk_filler_audit_path,
    )
    if options.prepare_only:
        return prepare_and_emit_talk_delivery_from_finalization(**delivery_kwargs)
    return _deliver_staged_record(**delivery_kwargs)
