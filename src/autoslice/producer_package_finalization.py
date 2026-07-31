"""Final recut, authority binding, burn, staging, and local delivery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import dataclass
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
    adjudicated_proposed_full_cue,
    carryover_path,
    load_final_review_carryover,
    persist_final_review_carryover,
)
from src.autoslice.exact_final_convergence import (
    collect_exact_final_convergence_memos,
    rebind_exact_final_convergence_memos,
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
from src.autoslice.final_review_auditor import persist_review_audit
from src.autoslice.final_review_contract import (
    EXACT_FINAL_CPA_SELF_HEAL_MAX_REPAIR_PASSES,
    FinalReviewContractError,
    correction_carryover_consumed,
    validate_final_review_release,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.producer_media import (
    RECUT_PROVENANCE_SCHEMA,
    _resolved_optional_path,
    _validated_burned_ass_artifact,
    _validated_burned_artifact,
    _write_json_atomic,
)
from src.autoslice.producer_text_finalization import (
    _render_cues_to_srt,
    verify_chat_authority_final_surfaces,
)
from src.autoslice.redelivery_subtitle_baseline import (
    apply_redelivery_subtitle_baseline,
)
from src.autoslice.recovery_title_authority import (
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.shadow_review import _sha256
from src.autoslice.source_subtitle_truth import (
    apply_source_subtitle_truth,
    source_truth_owner_windows,
)
from src.autoslice.source_fact_review import source_fact_review_passes
from src.autoslice.surface_canon import (
    canonicalize_japanese_native_script_surfaces,
    normalize_japanese_native_script_surfaces,
)
from src.autoslice.subtitle_fidelity import (
    apply_title_mark_balance_guard,
    resolve_deferred_foreign_introductions,
)
from src.autoslice.subtitle_regression import verify_subtitle_regression_surfaces
from src.autoslice.talk_filler import bind_final_filler_audit_to_burn
from src.autoslice.story_contract import (
    audit_story_artifact,
    build_story_contract,
    canonicalize_relation_summary,
    canonicalize_story_scorecard,
    cover_story_contract_binding_matches,
)
from src.autoslice.clip_context import validate_clip_context


ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)


def _snapshot_file_bytes(
    paths: list[Path],
) -> dict[Path, bytes | None]:
    return {
        path: path.read_bytes() if path.exists() else None
        for path in dict.fromkeys(paths)
    }


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.exact-final-",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_file_bytes(
    snapshots: Mapping[Path, bytes | None],
) -> None:
    for path, payload in snapshots.items():
        if payload is None:
            path.unlink(missing_ok=True)
        else:
            _write_bytes_atomic(path, payload)


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


@dataclass(frozen=True)
class FinalRecutArtifacts:
    recut_dir: Path
    media_path: Path
    subtitle_path: Path
    text_manifest_path: Path | None
    text_manifest: dict | None
    redelivery_baseline_audit_path: Path | None = None
    redelivery_baseline_audit: dict | None = None


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


def _audit_deferred_exact_replay_reverification(
    *,
    pre_truth_audit: Mapping[str, object],
    baseline_audit: Mapping[str, object],
    post_truth_audit: Mapping[str, object] | None,
) -> dict[str, object]:
    """Prove an early cue-shape deferral reached its promised late authority."""

    strategy = pre_truth_audit.get("deferred_strategy")
    result: dict[str, object] = {
        "schema_version": "deferred-exact-replay-reverification.v1",
        "status": "NOT_REQUIRED",
        "deferred_strategy": strategy,
        "required_truth_ids": [],
        "context_only_truth_ids": [],
        "straddling_truth_ids": [],
        "reverified_truth_ids": [],
        "missing_truth_ids": [],
    }
    supported_strategies = {
        "exact_reviewed_interval_replay_then_reapply_source_truth",
        "reviewed_text_restore_then_reapply_source_truth",
    }
    if strategy not in supported_strategies:
        return result

    current_source_interval = baseline_audit.get("current_source_interval")
    if isinstance(current_source_interval, Mapping):
        final_source_start_ms = current_source_interval.get(
            "absolute_source_start_ms"
        )
        final_source_end_ms = current_source_interval.get(
            "absolute_source_end_ms"
        )
    else:
        final_source_start_ms = final_source_end_ms = None
    final_interval_valid = bool(
        isinstance(final_source_start_ms, int)
        and not isinstance(final_source_start_ms, bool)
        and isinstance(final_source_end_ms, int)
        and not isinstance(final_source_end_ms, bool)
        and final_source_end_ms > final_source_start_ms
    )
    required_ids: list[str] = []
    context_only_ids: list[str] = []
    straddling_ids: list[str] = []
    for row in pre_truth_audit.get("failures") or []:
        if not isinstance(row, Mapping):
            continue
        truth_id = str(row.get("truth_id") or "")
        if not truth_id:
            continue
        source_start_ms = row.get("source_start_ms")
        source_end_ms = row.get("source_end_ms")
        source_interval_valid = bool(
            isinstance(source_start_ms, int)
            and not isinstance(source_start_ms, bool)
            and isinstance(source_end_ms, int)
            and not isinstance(source_end_ms, bool)
            and source_end_ms > source_start_ms
        )
        if not (final_interval_valid and source_interval_valid):
            required_ids.append(truth_id)
            continue
        wholly_outside = bool(
            source_end_ms <= final_source_start_ms
            or source_start_ms >= final_source_end_ms
        )
        wholly_inside = bool(
            final_source_start_ms <= source_start_ms
            and source_end_ms <= final_source_end_ms
        )
        if wholly_outside:
            context_only_ids.append(truth_id)
        elif wholly_inside:
            required_ids.append(truth_id)
        else:
            straddling_ids.append(truth_id)
    required_ids = sorted(set(required_ids))
    straddling_ids = sorted(set(straddling_ids))
    # A duplicated truth id is context-only only if every occurrence is wholly
    # outside. Any inside or straddling occurrence keeps it out of that class.
    context_only_ids = sorted(
        set(context_only_ids) - set(required_ids) - set(straddling_ids)
    )
    result["required_truth_ids"] = required_ids
    result["context_only_truth_ids"] = context_only_ids
    result["straddling_truth_ids"] = straddling_ids
    if straddling_ids:
        result["status"] = "FAILED"
        result["reason_code"] = (
            "DEFERRED_TRUTH_STRADDLES_FINAL_DELIVERY"
        )
        return result
    exact_strategy = (
        strategy
        == "exact_reviewed_interval_replay_then_reapply_source_truth"
    )
    baseline_strategy_ok = (
        baseline_audit.get("application_strategy")
        == "exact_reviewed_interval_replay"
        if exact_strategy
        else baseline_audit.get("status")
        in {"APPLIED", "ALREADY_SATISFIED"}
    )
    post_truth_ok = bool(
        isinstance(post_truth_audit, Mapping)
        and post_truth_audit.get("status")
        in {"APPLIED", "ALREADY_SATISFIED", "NO_RELEVANT_INTERVAL"}
    )
    if not baseline_strategy_ok or not post_truth_ok:
        result["status"] = "FAILED"
        result["reason_code"] = (
            "EXACT_REPLAY_OR_POST_TRUTH_AUTHORITY_MISSING"
        )
        return result
    if not required_ids:
        if context_only_ids:
            result["status"] = "PASS"
            result["reason_code"] = (
                "ALL_DEFERRED_TRUTH_CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY"
            )
            return result
        result["status"] = "FAILED"
        result["reason_code"] = "DEFERRED_TRUTH_REQUIREMENT_EMPTY"
        return result

    reverified_ids = sorted(
        {
            str(row.get("truth_id"))
            for key in ("applied", "satisfied")
            for row in (post_truth_audit.get(key) or [])
            if isinstance(row, Mapping) and str(row.get("truth_id") or "")
        }
    )
    missing_ids = sorted(set(required_ids) - set(reverified_ids))
    result["reverified_truth_ids"] = reverified_ids
    result["missing_truth_ids"] = missing_ids
    if missing_ids:
        result["status"] = "FAILED"
        result["reason_code"] = "DEFERRED_TRUTH_ID_NOT_REVERIFIED"
    else:
        result["status"] = "PASS"
    return result


def _materialize_final_recut(
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
) -> FinalRecutArtifacts:
    recut_dir = out_root / "replacement_recuts"
    recut_dir.mkdir(exist_ok=True)
    media_path = recut_dir / f"{cid}.recut.mp4"
    adapters.run_command(adapters.accurate_recut_command(source_video=padded, output_media=media_path, start_ms=final_start, duration_ms=final_end - final_start))
    recut_provenance_path = media_path.with_suffix(".provenance.json")
    _write_json_atomic(
        recut_provenance_path,
        {
            "schema_version": RECUT_PROVENANCE_SCHEMA,
            "source_piece": (
                piece_provenance_rows[0]
                if len(piece_provenance_rows) == 1
                else piece_provenance_rows
            ),
            "padded": json.loads(padded_provenance_path.read_text(encoding="utf-8")),
            "final_recut": {
                "source_path": str(padded.resolve()),
                "source_sha256": _sha256(padded),
                "start_ms": final_start,
                "end_ms": final_end,
                "absolute_source_start_ms": (
                    int(spec["pieces"][0]["start_ms"]) + final_start
                    if len(spec["pieces"]) == 1
                    else None
                ),
                "absolute_source_end_ms": (
                    int(spec["pieces"][0]["start_ms"]) + final_end
                    if len(spec["pieces"]) == 1
                    else None
                ),
                "output_path": str(media_path.resolve()),
                "output_sha256": _sha256(media_path),
            },
        },
    )
    subtitle_path = media_path.with_suffix(".srt")
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
        if override_document.get("schema_version") == 3:
            text_manifest = adapters.apply_text_override_document(
                *text_manifest_args,
                timeline_offset_ms=final_start,
            )
        else:
            text_manifest = adapters.apply_text_override_document(*text_manifest_args)
    else:
        adapters.write_source_range_srt(sanitized, final_start, final_end, subtitle_path)
    redelivery_baseline_audit_path: Path | None = None
    redelivery_baseline_audit: dict | None = None
    baseline_config = spec.get("subtitle_redelivery_baseline")
    if baseline_config is not None:
        if text_override_path is not None:
            raise SystemExit(
                "REDELIVERY_BASELINE_CONFLICTS_WITH_TEXT_OVERRIDE: use source truth "
                "windows for incident corrections"
            )
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
        protected_windows: list[tuple[int, int]] = []
        # A dropped hallucination no longer has a current cue to align against
        # its old baseline cue, so that exact deletion window must be excluded
        # from both sides of the one-to-one mapper.  Every text-bearing truth
        # window is intentionally *not* protected: restore the entire reviewed
        # lexical baseline first, then replay every higher-authority truth.
        # Otherwise one canonical mention anywhere in a broad entity window
        # can hide a new wrong variant in a sibling cue (毁神/鼠神 incident).
        for key in ("applied", "satisfied"):
            for row in truth_audit.get(key) or []:
                if row.get("action") != "drop_cue":
                    continue
                for owner_start, owner_end in source_truth_owner_windows(row):
                    start_ms = max(0, owner_start - final_start)
                    end_ms = min(
                        final_end - final_start,
                        owner_end - final_start,
                    )
                    if start_ms < end_ms:
                        protected_windows.append((start_ms, end_ms))
        current_text = subtitle_path.read_text(encoding="utf-8")
        current_source_start_ms: int | None = None
        current_source_end_ms: int | None = None
        current_source_recording_basename: str | None = None
        current_source_sha256: str | None = None
        if baseline_config.get("schema_version") == "subtitle-redelivery-baseline.v2":
            if len(spec.get("pieces") or []) != 1 or len(piece_provenance_rows) != 1:
                raise SystemExit(
                    "REDELIVERY_BASELINE_V2_REQUIRES_ONE_BOUND_SOURCE_PIECE"
                )
            piece = spec["pieces"][0]
            provenance = piece_provenance_rows[0]
            source_path = str(provenance.get("source_path") or "").strip()
            source_sha256 = str(provenance.get("source_sha256") or "").strip()
            if not source_path or not source_sha256:
                raise SystemExit(
                    "REDELIVERY_BASELINE_V2_SOURCE_PROVENANCE_MISSING"
                )
            piece_start_ms = int(piece["start_ms"])
            current_source_start_ms = piece_start_ms + final_start
            current_source_end_ms = piece_start_ms + final_end
            current_source_recording_basename = Path(source_path).name
            current_source_sha256 = source_sha256
        output_text, redelivery_baseline_audit = (
            apply_redelivery_subtitle_baseline(
                current_text,
                config=baseline_config,
                spec_parent=(spec_parent or Path.cwd()),
                protected_windows=protected_windows,
                current_source_start_ms=current_source_start_ms,
                current_source_end_ms=current_source_end_ms,
                current_source_recording_basename=current_source_recording_basename,
                current_source_sha256=current_source_sha256,
            )
        )
        redelivery_baseline_audit_path = (
            recut_dir / f"{cid}.redelivery-baseline.json"
        )
        final_truth_failed = False
        final_title_failed = False
        post_baseline_truth_audit: Mapping[str, object] | None = None
        if redelivery_baseline_audit["status"] != "FAILED" and truth_reapply:
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
                final_title_audit["status"]
                == "UNRESOLVED_COMPLEX_IMBALANCE"
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
        deferred_exact_replay_audit = (
            _audit_deferred_exact_replay_reverification(
                pre_truth_audit=truth_audit,
                baseline_audit=redelivery_baseline_audit,
                post_truth_audit=post_baseline_truth_audit,
            )
        )
        redelivery_baseline_audit[
            "deferred_exact_replay_reverification"
        ] = deferred_exact_replay_audit
        redelivery_baseline_audit_path.write_text(
            json.dumps(
                redelivery_baseline_audit,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if chat_authority_audit is not None:
            chat_authority_audit["redelivery_subtitle_baseline_audit"] = (
                redelivery_baseline_audit
            )
        subtitle_path.write_text(output_text, encoding="utf-8")
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
    # This is the last text-mutating choke point.  The earlier text pipeline
    # already removes release-invalid slivers, but a hash-bound reviewed
    # baseline is replayed later and can legitimately restore the old cue grid.
    # Run the same validator-driven merge *after* baseline/source-truth replay
    # so no late authority branch can resurrect <300ms or non-exempt one-CJK
    # cues (2026-07-22 1863: 「哦」/「行」).
    final_text = subtitle_path.read_text(encoding="utf-8")
    final_text, final_release_grade_merge_rows = merge_release_grade_cues(
        final_text
    )
    # A hash-bound reviewed baseline is deliberately allowed to restore old
    # wording late. Re-assert the Japanese native-script presentation policy
    # after that replay so a legacy boku/ore/atashi/wakuwaku surface cannot
    # reach burn or merely turn into a final-owner blocker.
    final_text, final_japanese_native_script_audit = (
        normalize_japanese_native_script_surfaces(final_text)
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
            redelivery_baseline_audit_path.write_text(
                json.dumps(
                    redelivery_baseline_audit,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
    elif chat_authority_audit is not None:
        chat_authority_audit[
            "post_redelivery_japanese_native_script_audit"
        ] = final_japanese_native_script_audit
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
    )


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


def _apply_exact_final_cpa_repairs(
    srt_text: str,
    audit: Mapping[str, object],
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

    cues = parse_srt_cues(srt_text)
    findings = audit.get("findings")
    if not isinstance(findings, list):
        return srt_text, []
    repairs: list[dict[str, object]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        cue_index = finding.get("cue_index")
        proposed = adjudicated_proposed_full_cue(finding)
        adjudication = finding.get("exact_release_adjudication")
        cycle_adjudication = finding.get(
            "exact_final_cpa_cycle_adjudication"
        )
        if (
            isinstance(cue_index, bool)
            or not isinstance(cue_index, int)
            or not 1 <= cue_index <= len(cues)
            or not isinstance(proposed, str)
            or (
                isinstance(cycle_adjudication, Mapping)
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
        ):
            continue
        cues[cue_index - 1] = type(current)(
            index=current.index,
            start_ms=current.start_ms,
            end_ms=current.end_ms,
            text=proposed,
        )
        repairs.append(
            {
                "schema_version": "exact-final-cpa-self-heal.v1",
                "cue_index": cue_index,
                "matched_start_ms": current.start_ms,
                "matched_end_ms": current.end_ms,
                "before": current.text,
                "after": proposed,
                "before_sha256": "sha256:" + current_sha256,
                "after_sha256": "sha256:"
                + hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
                "finding_sha256": "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        finding,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "request_sha256": "sha256:" + request_sha256,
                "decision_authority": "CPA_JUDGE",
                "action": "DROP_CUE" if is_drop else "REPLACE_CUE_TEXT",
                "repair_class": request.get("repair_class"),
                "policy_branch": adjudication.get("policy_branch"),
                "acoustic_witness": (
                    {
                        key: adjudication["verdict"].get(key)
                        for key in (
                            "schema_version",
                            "status",
                            "request_sha256",
                            "target_audible",
                        )
                    }
                    if isinstance(adjudication.get("verdict"), Mapping)
                    else None
                ),
                "judge": dict(judge),
                "drop_authority": (
                    dict(adjudication["drop_authority"])
                    if is_drop
                    and isinstance(
                        adjudication.get("drop_authority"), Mapping
                    )
                    else None
                ),
                "inaudible_witness_override": (
                    dict(
                        witness_judge[
                            "inaudible_witness_override"
                        ]
                    )
                    if inaudible_override_valid
                    and isinstance(
                        witness_judge.get(
                            "inaudible_witness_override"
                        ),
                        Mapping,
                    )
                    else None
                ),
                "mutation_authority": dict(mutation),
                "timing_immutable": True,
            }
        )
    if not repairs:
        return srt_text, []
    if any(repair.get("action") == "DROP_CUE" for repair in repairs):
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


def _replayable_exact_final_carryover_findings(
    srt_text: str,
    path: Path,
) -> list[dict[str, object]]:
    """Remap a prior exact CPA decision onto identical current cue bytes/time."""

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
        request = (
            adjudication.get("request")
            if isinstance(adjudication, Mapping)
            else None
        )
        proposed = adjudicated_proposed_full_cue(row)
        if (
            len(matches) != 1
            or proposed is None
            or not isinstance(request, Mapping)
        ):
            continue
        cue_position, cue = matches[0]
        if (
            request.get("base_text_sha256") != base_sha256
            or request.get("current_cue") != cue.text
            or request.get("proposed_cue") != proposed
            or request.get("matched_start_ms") != cue.start_ms
            or request.get("matched_end_ms") != cue.end_ms
        ):
            continue
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
        (
            row.get("base_text_sha256"),
            row.get("proposed_full_cue"),
        )
        for row in audit["findings"]
        if isinstance(row, Mapping)
    }
    additions = [
        row
        for row in findings
        if (row.get("base_text_sha256"), row.get("proposed_full_cue"))
        not in existing
    ]
    if not additions:
        return audit
    audit["findings"] = [*audit["findings"], *additions]
    audit["validated_finding_count"] = len(audit["findings"])
    audit["status"] = "FLAGGED"
    audit["release_gate"] = "BLOCK"
    reasons = [str(value) for value in audit.get("reason_codes") or []]
    if "FINAL_REVIEW_UNRESOLVED_FINDINGS" not in reasons:
        reasons.append("FINAL_REVIEW_UNRESOLVED_FINDINGS")
    audit["reason_codes"] = reasons
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
        before = repair.get("before")
        after = repair.get("after")
        local_start = repair.get("matched_start_ms")
        local_end = repair.get("matched_end_ms")
        mutation = repair.get("mutation_authority")
        is_drop = bool(after == "" and valid_inaudible_drop_repair(repair))
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
        repair_sha256 = "sha256:" + hashlib.sha256(
            json.dumps(
                repair,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
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


def _run_exact_final_review_gate(
    *,
    cid: str,
    out_root: Path,
    final_start: int,
    final_end: int,
    recut: FinalRecutArtifacts,
    chat_authority_audit: dict,
    chat_authority_path: Path,
    adapters: ProducerFinalizationAdapters,
) -> dict[str, object]:
    """Review and bind the actual post-boundary, post-authority SRT bytes."""

    reviewer = adapters.run_exact_final_review
    if reviewer is None:
        raise SystemExit("FINAL_REVIEW_EXACT_FINALIZER_MISSING")
    self_heal_passes: list[dict[str, object]] = []
    carryover_file = carryover_path(out_root, cid)
    replayable_carryover_base_sha256: set[str] = set()
    consumed_carryover_repairs: dict[str, Mapping[str, object]] = {}
    # A long clip can expose a second-order wording error only after an earlier
    # CPA-authorized repair makes the surrounding sentence coherent.  Two
    # repair rounds proved too small for the five-minute pink-room recovery:
    # the third scan found valid repairs and then stopped solely because of the
    # historical cap.  Keep convergence bounded, but allow five repair rounds
    # plus the mandatory final clean scan.
    max_review_passes = (
        EXACT_FINAL_CPA_SELF_HEAL_MAX_REPAIR_PASSES + 1
    )
    review_audit_path = out_root / f"{cid}.review-flags.json"
    for pass_index in range(max_review_passes):
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
        audit = reviewer(
            final_text,
            chat_authority_audit,
            final_start,
            final_end,
        )
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
        if pass_index == 0:
            replayable = _replayable_exact_final_carryover_findings(
                final_text,
                carryover_file,
            )
            replayable_carryover_base_sha256.update(
                str(row.get("base_text_sha256") or "")
                for row in replayable
            )
            audit = _overlay_exact_carryover_findings(audit, replayable)
        chat_authority_audit["final_review_audit"] = audit
        persist_review_audit(review_audit_path, audit)
        expected_srt_sha256 = "sha256:" + hashlib.sha256(
            final_bytes
        ).hexdigest()
        try:
            validate_final_review_release(
                audit,
                expected_srt_sha256=expected_srt_sha256,
            )
        except FinalReviewContractError as exc:
            repaired_text, repairs = _apply_exact_final_cpa_repairs(
                final_text, audit
            )
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
            if (
                (
                    exc.reason_code == "FINAL_REVIEW_UNRESOLVED_FINDINGS"
                    or exact_replay_closes_unconsumed_carryover
                )
                and repairs
                and pass_index < max_review_passes - 1
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
                pending_self_heal_audit = {
                    "schema_version": "exact-final-cpa-self-heal-audit.v1",
                    "status": "REVIEW_PENDING",
                    "passes": next_self_heal_passes,
                }
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
                        delivery_start_ms=final_start,
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

                    payloads: list[tuple[Path, bytes]] = [
                        (review_audit_path, _json_bytes(audit)),
                        (
                            chat_authority_path,
                            _json_bytes(staged_chat_authority),
                        ),
                    ]
                    if (
                        staged_baseline is not None
                        and recut.redelivery_baseline_audit_path
                        is not None
                    ):
                        payloads.append(
                            (
                                recut.redelivery_baseline_audit_path,
                                _json_bytes(staged_baseline),
                            )
                        )
                    # Install the active SRT last, after its complete ledger
                    # and sidecars are ready. Any exception rolls all paths
                    # back to the exact pre-pass bytes below.
                    payloads.append(
                        (
                            recut.subtitle_path,
                            repaired_text.encode("utf-8"),
                        )
                    )
                    for path, payload in payloads:
                        _write_bytes_atomic(path, payload)

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
            # 终审结转仍是无法在当前 exact-final pass 安全落盘时的后备。
            carryover_count = persist_final_review_carryover(
                carryover_file, audit
            )
            if carryover_count:
                audit["carryover_persisted_count"] = carryover_count
            chat_authority_path.write_text(
                json.dumps(
                    chat_authority_audit,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
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
            self_heal_audit = {
                "schema_version": "exact-final-cpa-self-heal-audit.v1",
                "status": "PASS",
                "passes": self_heal_passes,
                "final_srt_sha256": expected_srt_sha256,
            }
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
        chat_authority_path.write_text(
            json.dumps(
                chat_authority_audit,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return audit
    raise AssertionError("exact-final self-heal loop exhausted")

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
    subtitle_regression_path: Path | None,
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
            chat_authority_path.write_text(
                json.dumps(
                    chat_authority_audit,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
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
        chat_authority_audit,
        final_text_srt=final_text,
        final_speaker_srt=final_speaker_text,
        delivery_start_ms=final_start,
        delivery_end_ms=final_end,
    )
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
    chat_authority_path.write_text(
        json.dumps(chat_authority_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
    talk_filler_audit_path: Path | None = None,
) -> dict:
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
    record = adapters.burn_preview_subtitles(record, run_ffmpeg=True, branding_intro=branding_intro)
    if not isinstance(record.get("burned_preview"), dict) or record["burned_preview"].get("status") != "BURNED":
        raise SystemExit(f"FINAL_SUBTITLE_BURN_FAILED: {record.get('burned_preview')}")
    burned = _validated_burned_artifact(record)
    bind_final_filler_audit_to_burn(
        audit_path=talk_filler_audit_path,
        burned_preview=record["burned_preview"],
    )
    if talk_filler_audit_path is not None:
        record["artifact_hashes"]["talk_filler_audit_sha256"] = (
            "sha256:" + _sha256(talk_filler_audit_path)
        )
    chat_authority_audit["burn_binding"] = {
        "burned_media_path": str(burned),
        "burned_media_sha256": _sha256(burned),
        "ass_path": str(speaker_ass) if speaker_ass is not None else None,
        "ass_sha256": _sha256(speaker_ass) if speaker_ass is not None else None,
    }
    chat_authority_path.write_text(
        json.dumps(chat_authority_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    record["artifact_hashes"]["chat_authority_audit_sha256"] = "sha256:" + _sha256(chat_authority_path)
    return record

def _stage_record(
    *,
    options: ProducerFinalizationOptions,
    spec: dict,
    cid: str,
    recut: FinalRecutArtifacts,
    record: dict,
    adapters: ProducerFinalizationAdapters,
) -> StagedRecord:
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
    title_llm = None
    if not given_title:
        title_llm = build_llm_call(
            LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' high", timeout_seconds=180.0)
        )
    art_direction_llm = None if options.reuse_cover else build_llm_call(
        LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-luna gpt-5.5 gpt-5.4' medium", timeout_seconds=180.0)
    )
    source_fact_llm = build_llm_call(
        LlmConfig(
            transport="command",
            command_template=(
                "bash scripts/llm_via_cpa.sh {prompt_file} "
                "{completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' high"
            ),
            timeout_seconds=180.0,
        )
    )
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
    cover_reference_authority = load_candidate_cover_reference(
        cid,
        ledger_path=CHANNEL_PROFILE.asset_file(
            "cover_reference_overrides", repo_root=ROOT
        ),
    )
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

    def build_story_contract_for_hook(
        reviewed_hook: str,
    ) -> dict[str, object]:
        contract = build_story_contract(
            candidate_id=cid,
            selection_hook=reviewed_hook,
            transcript_text=transcript_text,
            selection_scorecard=story_selection_scorecard,
            session_relation_authority=spec.get(
                "session_relation_authority"
            ),
            cover_reference_authority=cover_reference_authority,
            source_media_sha256s=[
                str(piece.get("source_media_sha256"))
                for piece in (spec.get("pieces") or [])
                if isinstance(piece, Mapping)
                and piece.get("source_media_sha256")
            ],
            clip_context=spec.get("clip_context"),
            recording_date=str(spec.get("date") or ""),
            boundary_semantic_review=spec.get(
                "boundary_semantic_review"
            ),
            human_boundary_authority=str(
                spec.get("given_end_authority") or ""
            ),
        )
        contract["input_audits"] = [
            audit_story_artifact(
                reviewed_hook,
                story_contract=contract,
                artifact_kind="selection_hook",
            ),
            audit_story_artifact(
                transcript_text,
                story_contract=contract,
                artifact_kind="subtitle",
            ),
        ]
        return contract

    story_contract = build_story_contract_for_hook(
        story_selection_hook
    )
    record["selection_scorecard"] = story_selection_scorecard
    record["session_relation_authority"] = spec.get("session_relation_authority")
    record["story_contract"] = story_contract
    if normalized_recovery_publication_authority is not None:
        record["recovery_publication_authority"] = (
            normalized_recovery_publication_authority
        )
    if clip_context_path is not None:
        record["clip_context_path"] = str(clip_context_path)
        record["clip_context_payload_sha256"] = clip_context.get(
            "context_sha256"
        )
        record.setdefault("artifact_hashes", {})[
            "clip_context_file_sha256"
        ] = "sha256:" + _sha256(clip_context_path)
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
        selection_hook=story_selection_hook,
        cover_diversity_slot=spec.get("cover_diversity_slot"),
        recovery_publication_authority=(
            normalized_recovery_publication_authority
        ),
    )
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
        marker = (
            "SOURCE_FACT_REVIEW_INFRA_UNRESOLVED"
            if reason
            in {
                "CPA_TEXT_REVIEW_UNAVAILABLE",
                "CPA_TEXT_REVIEW_CALL_FAILED",
            }
            else "SOURCE_FACT_REPAIR_EXHAUSTED"
        )
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
    if staging.get("cover_status") == "AI_COVER_READY":
        cover_reason_codes, cover_story_audits = _audit_story_bound_cover(
            staging, final_story_contract
        )
        final_story_contract["cover_output_audits"] = cover_story_audits
        if cover_reason_codes:
            raise SystemExit(
                "STORY_CONTRACT_COVER_FAILED: " + ",".join(cover_reason_codes)
            )
    # 7. Upload tags (Ivan 2026-07-13): generated at package time against the
    # FINAL title + FINAL delivered subtitles (tag 必须按成品字幕出), frozen
    # into the record so make-manifest picks them up without re-running any
    # model. Fail-safe by contract: generate_upload_tags never raises; a tag
    # failure records status=FAILED and the uploader falls back to base tags.
    if not str(staging.get("title_authority_status") or "").startswith("UNRESOLVED"):
        record["upload_tags"] = adapters.generate_upload_tags(
            str(staging.get("title") or given_title or cid), subtitle_path, timeout=180.0
        )
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
        (clip_context_path, ".clip-context.json"),
        (cover_title_mask_path, ".cover.title-mask.png"),
        (cover_pre_overlay_path, ".cover.pre-overlay.png"),
        (cover_route_background_path, ".cover.route-background.png"),
        (publish_json_path, ".publish.json"),
        (record_path, ".record.json"),
    ):
        if source is not None and source.is_file():
            adapters.run_command(["cp", str(source), str(delivery / f"{name}{suffix}")])
    cover = staging.get("cover_path")
    if cover and Path(cover).is_file():
        adapters.run_command(["cp", str(cover), str(delivery / f"{name}.cover.png")])

    print(json.dumps(
        {
            "candidate_id": cid,
            "final_end_ms": final_end,
            "duration_ms": int(record["duration_ms"]),
            "closure_sentence": audit["closure_sentence"],
            "boundary_verdict": audit["verdict"],
            "red_flags": audit.get("red_flags", []),
            "boundary_repairs": audit.get("boundary_repairs", []),
            "timing_qa": timing_qa.get("counts"),
            "cover_status": staging.get("cover_status"),
            "title": staging.get("title"),
            "delivery": str(delivery / f"{name}.mp4"),
            "subtitle": str(delivery / f"{name}.srt"),
            "speaker_subtitle": str(delivery / f"{name}.speaker.srt") if speaker_review_srt else None,
            "speaker_ass": str(delivery / f"{name}.speaker.ass") if speaker_ass else None,
            "speaker_status": speaker_manifest.get("status") if speaker_manifest else "OFF",
            "subtitle_regression_status": (
                subtitle_regression_audit.get("status")
                if subtitle_regression_audit is not None
                else "NOT_CONFIGURED"
            ),
            "redelivery_baseline_status": (
                recut.redelivery_baseline_audit.get("status")
                if recut.redelivery_baseline_audit is not None
                else "NOT_CONFIGURED"
            ),
            "talk_filler_audit": (
                str(delivery / f"{name}.filler-audit.json")
                if talk_filler_audit_path is not None
                else None
            ),
        },
        ensure_ascii=False,
        indent=2,
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
    talk_filler_audit_path: Path | None = None,
) -> int:
    recut = _materialize_final_recut(
        spec=spec,
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
    )
    exact_final_review = _run_exact_final_review_gate(
        cid=cid,
        out_root=out_root,
        final_start=final_start,
        final_end=final_end,
        recut=recut,
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_authority_path,
        adapters=adapters,
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
    speaker = _finalize_speaker(
        options=options,
        spec=spec,
        host=host,
        cid=cid,
        recut=recut,
        final_start=final_start,
        final_end=final_end,
        adapters=adapters,
    )
    authority = _verify_final_authority(
        cid=cid,
        final_start=final_start,
        final_end=final_end,
        recut=recut,
        speaker=speaker,
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_authority_path,
        subtitle_regression_path=subtitle_regression_path,
    )
    record = _build_and_burn_record(
        options=options,
        profile_id=profile_id,
        speaker_subtitle_style_id=speaker_subtitle_style_id,
        final_start=final_start,
        final_end=final_end,
        recut=recut,
        speaker=speaker,
        authority=authority,
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_authority_path,
        timing_qa=timing_qa,
        audit=audit,
        branding_intro=branding_intro,
        adapters=adapters,
        talk_filler_audit_path=talk_filler_audit_path,
    )
    staged = _stage_record(
        options=options,
        spec=spec,
        cid=cid,
        recut=recut,
        record=record,
        adapters=adapters,
    )
    return _deliver_staged_record(
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
