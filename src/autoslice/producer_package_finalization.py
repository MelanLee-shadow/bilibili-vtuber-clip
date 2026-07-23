"""Final recut, authority binding, burn, staging, and local delivery."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.chat_authority import (
    reconcile_pending_text_overrides,
    reconcile_reviewed_text_override_conflicts,
)
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.cover_reference_authority import (
    load_candidate_cover_reference,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.producer_media import (
    RECUT_PROVENANCE_SCHEMA,
    _resolved_optional_path,
    _validated_burned_artifact,
    _write_json_atomic,
)
from src.autoslice.producer_text_finalization import verify_chat_authority_final_surfaces
from src.autoslice.redelivery_subtitle_baseline import (
    apply_redelivery_subtitle_baseline,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.shadow_review import _sha256
from src.autoslice.source_subtitle_truth import apply_source_subtitle_truth
from src.autoslice.subtitle_fidelity import (
    apply_title_mark_balance_guard,
    resolve_deferred_foreign_introductions,
)
from src.autoslice.subtitle_regression import verify_subtitle_regression_surfaces
from src.autoslice.talk_filler import bind_final_filler_audit_to_burn
from src.autoslice.story_contract import (
    audit_story_artifact,
    build_story_contract,
)
from src.autoslice.clip_context import validate_clip_context


ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)


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
    expected_binding = {
        "schema_version": story_contract.get("schema_version"),
        "relation_state": story_contract.get("relation_state"),
        "participants": story_contract.get("participants"),
        "cover_counterpart_reference_available": story_contract.get(
            "cover_counterpart_reference_available"
        ),
        "cover_reference_authority": story_contract.get(
            "cover_reference_authority"
        ),
        "source_media_sha256s": story_contract.get("source_media_sha256s"),
        "clip_context_binding": story_contract.get("clip_context_binding"),
        "cover_fallback_mode": story_contract.get("cover_fallback_mode"),
    }
    binding = generation.get("story_contract")
    if not isinstance(binding, Mapping) or any(
        binding.get(key) != value for key, value in expected_binding.items()
    ):
        reason_codes.add("COVER_STORY_CONTRACT_BINDING_MISSING_OR_STALE")
    if not cover_text:
        reason_codes.add("COVER_STORY_TEXT_MISSING")
    if not rendered_text:
        reason_codes.add("COVER_RENDERED_TEXT_EVIDENCE_MISSING")
    route_decision = generation.get("route_decision")
    if (
        not isinstance(route_decision, Mapping)
        or route_decision.get("schema_version")
        != "lidousha-cover-route-decision.v1"
        or route_decision.get("selected_treatment")
        not in {"screenshot_direct", "screenshot_polish", "cpa_redraw"}
        or not str(route_decision.get("reason") or "").strip()
    ):
        reason_codes.add("COVER_ROUTE_DECISION_MISSING_OR_INVALID")
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
            timing_pin = row.get("timing_pin")
            if isinstance(timing_pin, dict):
                for field in ("before_start_ms", "after_start_ms"):
                    if field in timing_pin:
                        timing_pin[field] = int(timing_pin[field]) + final_start
    rebased["timeline_basis"] = "padded_candidate"
    rebased["final_recut_offset_ms"] = final_start
    rebased["reapplied_after_redelivery_baseline"] = True
    return rebased


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
            "source_piece": piece_provenance_rows[0] if len(piece_provenance_rows) == 1 else None,
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
                for window in row.get("local_windows") or []:
                    start_ms = max(0, int(window["start_ms"]) - final_start)
                    end_ms = min(
                        final_end - final_start,
                        int(window["end_ms"]) - final_start,
                    )
                    if start_ms < end_ms:
                        protected_windows.append((start_ms, end_ms))
        current_text = subtitle_path.read_text(encoding="utf-8")
        output_text, redelivery_baseline_audit = (
            apply_redelivery_subtitle_baseline(
                current_text,
                config=baseline_config,
                spec_parent=(spec_parent or Path.cwd()),
                protected_windows=protected_windows,
            )
        )
        redelivery_baseline_audit_path = (
            recut_dir / f"{cid}.redelivery-baseline.json"
        )
        final_truth_failed = False
        final_title_failed = False
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
        if final_title_failed:
            raise SystemExit(
                f"TITLE_MARK_BALANCE_REQUIRED_AFTER_REDELIVERY: "
                f"{redelivery_baseline_audit_path}"
            )
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
    authority_rows = [
        {
            "authority_id": (
                f"redelivery-baseline-cue-{row.get('current_cue_index')}"
            ),
            "current_cue_index": row.get("current_cue_index"),
            "local_windows": [
                {
                    "start_ms": row.get("start_ms"),
                    "end_ms": row.get("end_ms"),
                }
            ],
        }
        for row in (baseline_audit.get("mappings") or [])
        if isinstance(row, Mapping)
    ]
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
    # Title LLM only when no manual title (iron rule: manual titles pass through
    # untouched). Cover art-direction LLM ALWAYS runs (Ivan 2026-07-04): even with
    # a hand-given title the cover still benefits from persona-fit expression /
    # layout / background; it is fail-open, so it never blocks.
    # Per-stage CPA chains (2026-07-10, Ivan): title is a single brand-critical
    # short call → gpt-5.6-sol at high effort; art direction is a structured
    # pick with a known good shape, deterministic fallback and judge guardrails
    # → gpt-5.6-luna at medium (the doc-exact luna lane).  Both fall back
    # 5.5 → 5.4.
    title_llm = None
    if not given_title:
        title_llm = build_llm_call(
            LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' high", timeout_seconds=180.0)
        )
    art_direction_llm = None if options.reuse_cover else build_llm_call(
        LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-luna gpt-5.5 gpt-5.4' medium", timeout_seconds=180.0)
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
    story_contract = build_story_contract(
        candidate_id=cid,
        selection_hook=str(spec.get("selection_hook") or ""),
        transcript_text=transcript_text,
        selection_scorecard=spec.get("selection_scorecard"),
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
    story_contract["input_audits"] = [
        audit_story_artifact(
            str(spec.get("selection_hook") or ""),
            story_contract=story_contract,
            artifact_kind="selection_hook",
        ),
        audit_story_artifact(
            transcript_text,
            story_contract=story_contract,
            artifact_kind="subtitle",
        ),
    ]
    record["selection_scorecard"] = spec.get("selection_scorecard")
    record["session_relation_authority"] = spec.get("session_relation_authority")
    record["story_contract"] = story_contract
    if clip_context_path is not None:
        record["clip_context_path"] = str(clip_context_path)
        record["clip_context_payload_sha256"] = clip_context.get(
            "context_sha256"
        )
        record.setdefault("artifact_hashes", {})[
            "clip_context_file_sha256"
        ] = "sha256:" + _sha256(clip_context_path)
    input_violations = [
        violation
        for audit in story_contract["input_audits"]
        for violation in audit.get("violations", [])
        if isinstance(violation, dict)
    ]
    if input_violations:
        reason_codes = sorted(
            {str(row.get("reason_code") or "STORY_CONTRACT_INPUT_INVALID") for row in input_violations}
        )
        raise SystemExit(
            "STORY_CONTRACT_INPUT_INVALID: " + ",".join(reason_codes)
        )
    record = adapters.stage_publish_draft(
        record,
        candidate_id=cid,
        title=given_title or cid,
        cues=final_title_cues,
        run_ffmpeg=True,
        title_llm_call=title_llm,
        art_direction_llm_call=art_direction_llm,
        skip_cover=options.reuse_cover,
        selection_hook=str(spec.get("selection_hook") or ""),
        cover_diversity_slot=spec.get("cover_diversity_slot"),
    )
    staging = record.get("publish_staging") or {}
    if staging.get("title_authority_status") == "BLOCKED_STORY_CONTRACT":
        raise SystemExit(
            "STORY_CONTRACT_TITLE_FAILED: "
            + str(staging.get("title_authority_error") or "unknown")
        )
    if staging.get("cover_status") == "AI_COVER_READY":
        cover_reason_codes, cover_story_audits = _audit_story_bound_cover(
            staging, story_contract
        )
        story_contract["cover_output_audits"] = cover_story_audits
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
    delivery = adapters.delivery_root() / spec["date"]
    delivery.mkdir(parents=True, exist_ok=True)
    name = spec.get("delivery_name") or cid
    # Old sapphire renders may coexist in replacement_recuts; copy only the
    # exact hash-bound speaker burn made by this run.
    burned = _validated_burned_artifact(record)
    adapters.run_command(["cp", str(burned), str(delivery / f"{name}.mp4")])
    adapters.run_command(["cp", str(subtitle_path), str(delivery / f"{name}.srt")])
    for source, suffix in (
        (speaker_review_srt, ".speaker.srt"),
        (speaker_ass, ".speaker.ass"),
        (speaker_manifest_path, ".speaker.json"),
        (chat_authority_path, ".chat-authority.json"),
        (redelivery_baseline_audit_path, ".redelivery-baseline.json"),
        (subtitle_regression_audit_path, ".subtitle-regression.json"),
        (talk_filler_audit_path, ".filler-audit.json"),
        (text_manifest_path, ".text-finalization.json"),
        (clip_context_path, ".clip-context.json"),
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
    if spec.get("subtitle_redelivery_baseline") is not None and not options.reuse_cover:
        raise SystemExit(
            "REDELIVERY_SUBTITLE_BASELINE_REQUIRES_REUSE_COVER"
        )
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
