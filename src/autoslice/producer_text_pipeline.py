"""Transcript, structured-chat, entity, and final-review stage for the producer."""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from .finding_lifecycle import is_finding_failure, only_finding_failures

from src.autoslice.review_evidence_store import (
    persist_document as _persist_review_document,
    review_from_bytes,
)

import datetime as dt
import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.autoslice.acoustic_witness_availability import witness_audio_locally_resolvable
from src.autoslice.prepared_native_audio_recovery import PreparedNativeAudioState
from src.autoslice.acoustic_witness_adjudication import (
    valid_cpa_entity_repair,
    valid_inaudible_drop_repair,
)
from src.autoslice.chat_authority import (
    ChatEvidence,
    ReferentGroup,
    apply_authoritative_chat_evidence,
    build_human_text_entity_verifier,
    clip_opening_address_group,
    introduced_term_cues,
    load_clip_opening_address_config,
    load_referent_groups,
    normalize_code_switch_surfaces,
    normalize_expected_value_surfaces,
    normalize_hard_meme_surfaces,
    normalize_japanese_native_script_surfaces,
    reconcile_contradictory_entity_repairs,
    registered_entity_names,
    repetition_divergence_groups,
    revert_unregistered_entity_repairs,
    sanitize_chat_display_text,
    witness_disagreement_cues,
)
from src.autoslice.surface_canon import normalize_caption_display_surfaces
from src.autoslice.continuation_subchecks import cold_read_session_scope
from src.autoslice.continuation_review_state import reviewed_reference_cues, unavailable_review_stage
from src.autoslice.exact_final_cpa_history import caption_successors, with_caption_successor
from src.autoslice.clip_context import (
    build_clip_context,
    clip_context_prompt_text,
    validate_clip_context,
    write_clip_context,
)
from src.autoslice.delivery_fast_path import (
    discover_priority_findings,
    pinned_replay_reviewed_text_ownership as _pinned_replay_reviewed_text_ownership,
    resolve_operator_text_full_ownership,
    resolve_truth_full_ownership,
    skipped_final_review_audit,
    verify_transcript_entities,
)
from src.autoslice.final_review_carryover import (
    adjudicated_proposed_full_cue,
    carryover_path,
    checkpoint_final_review_carryover,
    load_replayable_final_review_carryover,
)
from src.autoslice.exact_final_convergence import (
    converge_reconsidered_exact_final_findings,
    resolve_findings_from_exact_final_convergence_memos,
)
from src.autoslice.exact_final_witness_authority import (
    RANGE_WORD_PROJECTION_FIELD,
    history_audio_recovery_from_adjudication,
    valid_range_word_projection_binding,
)
from src.autoslice.danmaku_evidence import DanmakuItem
from src.autoslice.deferred_same_cue_resolution import adjudicate_routed_findings
from src.autoslice.final_review_auditor import (
    FinalReviewAuditError,
    MAX_CONTEXT_ADJUDICATIONS,
    _final_review_structured_context,
    adjudicate_context_finding, adjudicate_exact_release_findings,
    audit_correction_mutation_authority, audit_final_subtitles,
    build_context_adjudication_request,
    exact_source_transcript_infra_reason_codes,
    persist_review_audit,
    resolve_verified_source_truth_findings,
    route_findings,
)
from src.autoslice.final_review_provider_budget import with_cpa_resource_pressure
from src.autoslice.final_review_meaning_policy import POLICY_VERSION as MEANING_POLICY_VERSION
from src.autoslice.final_review_contract import (
    SCHEMA_VERSION as FINAL_REVIEW_SCHEMA_VERSION,
    FinalReviewContractError,
    _validate_exact_final_cpa_self_heal,
    _validate_review_geometry,
    _validate_review_integrity,
    is_keep_current_disclosed,
    is_release_resolved_keep,
    validate_final_review_release,
)
from src.autoslice.review_priority_candidates import (
    fidelity_review_candidates as _fidelity_review_candidates,  # noqa: F401
    review_priority_candidate_counts as _review_priority_candidate_counts,  # noqa: F401
    review_priority_candidates as _review_priority_candidates,
)
from src.autoslice.fidelity_review_candidates import fidelity_kept_contexts
from src.autoslice.final_source_language_owner import register_final_foreign_script_cpa_repairs, register_final_source_language_cpa_repairs
from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues
from src.autoslice.llm_client import extract_json_object
from src.autoslice.producer_chat_input import (
    build_structured_chat_binding_audit,
    DANMAKU_PRE_CONTEXT_MS,
    GIFT_PRE_CONTEXT_MS,
    GUARD_PRE_CONTEXT_MS,
    SC_PRE_CONTEXT_MS,
    _load_independent_chat_support_srts,
    _piece_chat_evidence,
)
from src.autoslice.producer_final_review_transport import (
    build_final_review_llm_call as _build_final_review_llm_call,
    build_pronoun_audit_llm_call as _build_pronoun_audit_llm_call,
)
from src.autoslice.producer_nested_acoustic import caption_audio_roles_context
from src.autoslice.producer_stage_log import producer_stage, staged as _stage_logged
from src.autoslice.producer_native_witness import (
    ensure_local_native_audio_budget as _ensure_local_native_audio_budget,
    native_foreign_script_kwargs as _native_foreign_script_kwargs,
)
from src.autoslice.pronoun_consistency import (
    CandidatePronounAuditError,
    discover_candidate_pronoun_findings,
)
from src.autoslice import provider_failure as _pf
from src.autoslice.producer_boundary_review_stage import (
    exact_delivery_correction_audit,
    review_final_boundary_semantics,  # noqa: F401 - compatibility export
)
from src.autoslice.frozen_source_boundary_receipt import (  # noqa: F401
    load_boundary_review_authorities,
)
from src.autoslice.producer_source_boundary_review import (
    attach_current_timing_qa_projection,
    review_source_boundary,
)
from src.autoslice.boundary_semantic_review import (
    boundary_search_scope_is_valid,
    cue_grid_sha256,
)
from src.autoslice.producer_boundary_owner_contract import (
    freeze_required_boundary_owner_contract,
    redelivery_baseline_boundary_owner as _redelivery_baseline_boundary_owner,
    validate_frozen_boundary_owner_contract,
)
from src.autoslice.producer_text_finalization import _render_cues_to_srt
from src.autoslice.producer_text_checkpoint import retain_post_transcript_text
from src.autoslice.producer_source_truth_authority import (
    reconcile_required_source_truth_chat_authority,
    verify_source_truth_preview_formal_binding,
)
from src.autoslice.song_name_semantic_verification import verify_and_pin_song_names
from src.autoslice.foreign_span_witness import (
    adjudicate_language_preservation_audit,
    adjudicate_foreign_script_audit,
    retranscribe_foreign_script_cluster,
)
from src.autoslice import foreign_audit_story_scope as _foreign_scope
from src.autoslice.self_reference_absorption import absorb_host_self_references
from src.autoslice.session_topic_authority import (
    absorb_session_topic_entities,
    discover_session_topic_authorities,
)
from src.autoslice.cue_split_hygiene import (
    merge_release_grade_cues,
)
from src.autoslice.screen_read_witness import (
    build_env_screen_read_probe,
)
from src.autoslice.source_subtitle_truth import (
    apply_source_subtitle_truth,
    build_source_truth_preview_receipt,
    ledger_local_windows,
    ledger_required_owner_contracts,
)
from src.autoslice.subtitle_timing_qa import build_ssh_silero_vad_provider
from src.autoslice.subtitle_fidelity import (
    apply_numeric_fact_provenance_guard,
    apply_impossible_punctuation_guard,
    apply_title_mark_balance_guard,
    defer_truth_owned_mixed_latin_cues,
    defer_unproven_foreign_introductions_to_late_authority,
    valid_redelivery_baseline_config as _valid_redelivery_baseline_config,
    mixed_cjk_latin_findings_covered_by_overrides,
    resolve_deferred_foreign_introductions,
    unproven_foreign_introductions_covered_by_overrides,
    windows_fully_cover,
)
from src.autoslice.term_boundary import unify_terms_across_cues
from src.autoslice.topic_entity_graph import (
    TopicEvidence,
    build_scoped_topic_context,
    dynamic_referent_groups,
    load_topic_entity_graph,
    merge_referent_groups,
    resolve_topic_context,
)


@dataclass(frozen=True)
class TextPipelineAdapters:
    """Repository- and script-owned seams supplied by the CLI entrypoint."""

    build_aggregate_transcriber: Callable[..., Callable]
    build_agy_transcriber: Callable[..., Callable]
    load_term_boundary_surfaces: Callable[[dict], list[str]]
    profile_asset_file: Callable[[str], Path]
    review_glossary: Callable[[], str]
    topic_graph_disabled: Callable[[], bool]
    topic_graph_path: Callable[[], Path]
    topic_graph_expected_sha256: Callable[[], str]


@dataclass(frozen=True)
class TextPipelineResult:
    srt_text: str
    cues: list[object]
    spans: list[object]
    transcriber: Callable
    chat_authority_audit: dict
    chat_authority_path: Path
    clip_context: dict
    clip_context_path: Path
    review_exact_final_srt: Callable[[str, Mapping[str, object], int, int], dict[str, object]]
    boundary_context_cues: list[object] | None = None


@dataclass(frozen=True)
class CurrentTextContinuation:
    """Inputs for the normal current-text continuation path.

    The continuation accepts only the artifacts the normal producer already
    writes.  In particular, there is no private typed-evidence schema here:
    existing ``chat-authority``/``final-review-audit`` records remain the
    authority and are validated before the normal owner and boundary
    consumers run again.
    """

    current_srt_path: Path
    chat_authority_path: Path
    clip_context_path: Path
    padded_sha256: str
    source_media_sha256s: tuple[str, ...]
    spans: Sequence[object]
    transcriber: Callable
    authoritative_chat: Sequence[ChatEvidence] = ()
    verify_confusable_entity: Callable | None = None
    expected_chat_authority_sha256: str | None = None
    expected_clip_context_sha256: str | None = None
    source_boundary_srt_path: Path | None = None
    expected_source_boundary_srt_sha256: str | None = None
    source_context_job_path: Path | None = None
    expected_source_context_job_sha256: str | None = None
    source_context_manifest_path: Path | None = None
    expected_source_context_manifest_sha256: str | None = None
    source_clock_authority_path: Path | None = None
    expected_source_clock_authority_sha256: str | None = None
    source_clock_srt_path: Path | None = None
    expected_source_clock_srt_sha256: str | None = None
    source_timing_resolution_path: Path | None = None
    expected_source_timing_resolution_sha256: str | None = None
    native_audio_state: PreparedNativeAudioState | None = None
    witness_cache_roots: tuple[Path, ...] = ()


class CurrentTextContinuationError(RuntimeError):
    """Raised when existing normal authority cannot be continued safely."""


class NativeAudioAccountContinuationError(CurrentTextContinuationError):
    """A verifier reconstruction lost the already active source account."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _require_native_audio_account(
    padded: Path, native_audio_state: PreparedNativeAudioState | None,
) -> None:
    from src.autoslice.supplement_audio_budget import get_budget

    budget = get_budget(padded)
    if native_audio_state is None and budget is not None and budget.snapshot()["revision"] != 0:
        raise NativeAudioAccountContinuationError("CURRENT_NATIVE_AUDIO_ACCOUNT_IDENTITY_MISSING")


@dataclass(frozen=True)
class TranscriptionDraft:
    song_name_candidates: list[str]
    transcriber: Callable
    spans: list[object]
    srt_text: str
    code_switch_audit: object
    term_boundary_moves: list[dict]
    support_srts: list[str]
    session_topic_authorities: tuple[dict[str, Any], ...]
    session_topic_absorption_audits: list[dict[str, Any]]
    source_language_witness_srt: str


@dataclass(frozen=True)
class EntityVerificationContext:
    verify_confusable_entity: Callable
    referent_groups: list[ReferentGroup]
    topic_resolution_audit: dict[str, object]


@dataclass(frozen=True)
class EntityAuthorityResult:
    srt_text: str
    chat_authority_audit: dict
    transcript_entity_audit: dict
    handled_entity_cues: set[int]


@dataclass(frozen=True)
class TextEvidenceResult:
    srt_text: str
    cues: list[object]
    chat_authority_path: Path


def _collect_timeline_chat(
    spec: dict, durations: list[int]
) -> tuple[list[DanmakuItem], list[ChatEvidence]]:
    merged: list[DanmakuItem] = []
    authoritative_chat: list[ChatEvidence] = []
    offset = 0
    for piece, dur in zip(spec["pieces"], durations):
        for item in _piece_chat_evidence(piece):
            rel = item.offset_ms - piece["start_ms"]
            if item.kind == "superchat":
                pre_context = SC_PRE_CONTEXT_MS
            elif item.kind == "gift":
                pre_context = GIFT_PRE_CONTEXT_MS
            elif item.kind == "guard":
                pre_context = GUARD_PRE_CONTEXT_MS
            else:
                pre_context = DANMAKU_PRE_CONTEXT_MS
            if not (-pre_context <= rel <= dur + 1_000):
                continue
            marker = f"·{item.sender}" if item.sender else ""
            if item.kind == "superchat":
                prefix = "【SC此前" if rel < 0 else "【SC"
            elif item.kind == "gift":
                prefix = "【礼物此前" if rel < 0 else "【礼物"
            elif item.kind == "guard":
                prefix = "【上舰此前" if rel < 0 else "【上舰"
            else:
                prefix = "【弹幕此前" if rel < 0 else "【弹幕"
            label = f"{prefix}{marker}】{sanitize_chat_display_text(item.text)}"
            merged.append(DanmakuItem(offset_ms=offset + max(0, rel), text=label))
            authoritative_chat.append(
                ChatEvidence(
                    item.kind,
                    offset + rel,
                    item.text,
                    item.sender,
                    item.source,
                    item.source_sha256,
                    item.source_event_id,
                )
            )
        offset += dur
    merged.sort(key=lambda item: item.offset_ms)
    return merged, authoritative_chat


def _transcribe_draft(
    *,
    spec: dict,
    padded: Path,
    padded_dur: int,
    host: str,
    substrate: str,
    correct: str,
    screen_text: bool,
    merged: list[DanmakuItem],
    adapters: TextPipelineAdapters,
    retained_srt_text: str | None = None,
) -> TranscriptionDraft:
    session_topic_authorities = discover_session_topic_authorities(spec)
    term_boundary_surfaces = adapters.load_term_boundary_surfaces(spec)
    song_name_candidates = [
        str(title).strip()
        for title in (spec.get("song_name_candidates") or [])
        if isinstance(title, str) and str(title).strip()
    ]
    if retained_srt_text is not None:
        if not isinstance(retained_srt_text, str) or not retained_srt_text:
            raise ValueError("RETAINED_REVIEWED_DRAFT_SRT_INVALID")

        # A retained draft is already a hash-bound, normally reviewed text
        # surface.  Keep the ordinary VAD stage below, but defer construction
        # and invocation of a fresh transcriber until a later normal
        # supplement (for example, boundary extension) actually requests it.
        # The callable returned here is deliberately lazy; its output is not
        # used as ``source_language_witness_srt`` for this seed.
        aggregate_kwargs = dict(
            danmaku_items=merged or None,
            window_start_ms=0,
            source_video=padded,
            correct=correct,
            screen_text=screen_text,
            recording_date=str(spec.get("date") or ""),
            topic_hint=str(spec.get("selection_hook") or ""),
            song_name_candidates=song_name_candidates,
            session_topic_authorities=session_topic_authorities,
            term_boundary_surfaces=term_boundary_surfaces,
            duration_ms=padded_dur,
        )
        observation = spec.get("bcut_draft_observation")
        if observation is not None:
            aggregate_kwargs.update(
                producer_spec=spec,
                bcut_draft_observation=observation,
            )

        def retained_transcriber(source_video: Path, speech_spans: object = None) -> str:
            if substrate == "aggregate_asr":
                fresh = adapters.build_aggregate_transcriber(
                    host,
                    **aggregate_kwargs,
                )
            else:
                fresh = adapters.build_agy_transcriber(
                    host, danmaku_items=merged or None, window_start_ms=0
                )
            return fresh(source_video, speech_spans)

        transcriber = retained_transcriber
    elif substrate == "aggregate_asr":
        aggregate_kwargs = dict(
            danmaku_items=merged or None,
            window_start_ms=0,
            source_video=padded,
            correct=correct,
            screen_text=screen_text,
            recording_date=str(spec.get("date") or ""),
            topic_hint=str(spec.get("selection_hook") or ""),
            song_name_candidates=song_name_candidates,
            session_topic_authorities=session_topic_authorities,
            term_boundary_surfaces=term_boundary_surfaces,
            duration_ms=padded_dur,
        )
        # Keep the default adapter call byte-compatible for external/test
        # adapters.  The extra seam is opt-in only when a spec explicitly
        # declares an immutable BCUT draft observation.
        observation = spec.get("bcut_draft_observation")
        if observation is not None:
            aggregate_kwargs.update(
                producer_spec=spec,
                bcut_draft_observation=observation,
            )
        transcriber = adapters.build_aggregate_transcriber(
            host,
            **aggregate_kwargs,
        )
    else:
        from src.autoslice.existing_subtitle_surface import select_nonaggregate_transcriber_builder

        builder = select_nonaggregate_transcriber_builder(
            substrate, spec=spec, padded=padded, padded_duration_ms=padded_dur,
            legacy_builder=adapters.build_agy_transcriber,
        )
        transcriber = builder(
            host, danmaku_items=merged or None, window_start_ms=0
        )
    vad = build_ssh_silero_vad_provider(host)
    with producer_stage("vad"):
        spans = vad(padded, 0, padded_dur)
    if retained_srt_text is not None:
        srt_text = retained_srt_text
        code_switch_audit = {
            "schema_version": "code-switch-surface-audit.v1",
            "status": "RETAINED_REVIEWED_DRAFT_PRESERVED",
            "moves": [],
        }
        session_topic_absorption_audit = {
            "schema_version": "session-topic-absorption-audit.v1",
            "status": "SKIPPED_RETAINED_REVIEWED_DRAFT",
            "absorbed": [],
        }
        term_boundary_moves = []
    else:
        with producer_stage("transcription", substrate=substrate, correct=correct):
            srt_text = transcriber(padded, [(s.start_ms, s.end_ms) for s in spans])
        srt_text, code_switch_audit = normalize_code_switch_surfaces(srt_text)
        srt_text, session_topic_absorption_audit = absorb_session_topic_entities(
            srt_text, session_topic_authorities
        )
        # Frontload before CPA; reassert for non-aggregate adapters and audit.
        term_boundary_moves = []
        pre_cpa_audit_path = padded.with_suffix(".pre-cpa-audit.json")
        if substrate == "aggregate_asr" and correct in ("bcut_agy_cpa", "cpa", "moss_cpa") and pre_cpa_audit_path.is_file():
            term_boundary_moves = json.loads(pre_cpa_audit_path.read_text(encoding="utf-8"))["term_boundary_moves"]
        elif term_boundary_surfaces:
            unified_cues, term_boundary_moves = unify_terms_across_cues(
                parse_srt_cues(srt_text), term_boundary_surfaces
            )
            if term_boundary_moves:
                srt_text = _render_cues_to_srt(unified_cues)
    support_srts = _load_independent_chat_support_srts(padded)
    draft_witness_path = padded.with_suffix(".asr_draft.srt")
    source_language_witness_srt = "" if retained_srt_text is not None else (
        draft_witness_path.read_text(encoding="utf-8", errors="replace")
        if draft_witness_path.is_file()
        else srt_text
    )
    return TranscriptionDraft(
        song_name_candidates=song_name_candidates,
        transcriber=transcriber,
        spans=spans,
        srt_text=srt_text,
        code_switch_audit=code_switch_audit,
        term_boundary_moves=term_boundary_moves,
        support_srts=support_srts,
        session_topic_authorities=session_topic_authorities,
        session_topic_absorption_audits=[session_topic_absorption_audit],
        source_language_witness_srt=source_language_witness_srt,
    )


def _retained_native_audio_state(retained_reviewed_draft):
    """The retained parent's joined native account, partial or completed."""
    if retained_reviewed_draft is None:
        return None
    partial = retained_reviewed_draft.get("partial_evidence") or {}
    return partial.get("native") or retained_reviewed_draft.get("retained_native")


def _retained_witness_cache_roots(retained_reviewed_draft) -> tuple[Path, ...]:
    """Prior output roots whose original witness caches are reused first (41:1541)."""
    if retained_reviewed_draft is None:
        return ()
    partial = retained_reviewed_draft.get("partial_evidence")
    if partial:
        return (Path(partial["proof"]["original_output_root"]),)
    parent_root = retained_reviewed_draft.get("retained_native_parent_output_root")
    return (Path(parent_root),) if parent_root else ()


def _build_entity_verification_context(
    *,
    spec: dict,
    padded: Path,
    padded_dur: int,
    host: str,
    text_override_path: Path | None,
    cid: str,
    out_root: Path,
    srt_text: str,
    authoritative_chat: list[ChatEvidence],
    adapters: TextPipelineAdapters,
    native_audio_state=None,
    witness_cache_roots: tuple[Path, ...] = (),
) -> EntityVerificationContext:
    _require_native_audio_account(padded, native_audio_state)
    if spec.get("comparison_ordinary_cache_only") is True:
        witness_cache_roots = (*witness_cache_roots, Path(spec["comparison_witness_cache_root"]))
    if native_audio_state is not None:
        witness_cache_roots = (*witness_cache_roots, *getattr(native_audio_state, "history_cache_roots", ()))
    human_entity_verifier = (
        build_human_text_entity_verifier(text_override_path, candidate_id=cid)
        if text_override_path is not None
        else None
    )
    audio_entity_verifier = None
    argon_ordinary_verifier = None
    native_provider = _ensure_local_native_audio_budget(spec, padded)
    native_budget_receipt = (native_audio_state.budget_receipt if native_audio_state is not None
                             else out_root / "native-audio-budget.json")
    # Direct and independent native observers must address the same exact
    # provider cache.  Their witness envelopes remain in their own output
    # directories, while this root lets ``observe_secondary`` reuse the raw
    # provider receipt instead of dispatching the same source/window twice.
    native_observation_cache_root = (native_audio_state.cache_root if native_audio_state is not None else out_root)
    if witness_audio_locally_resolvable(padded, host=host):  # F21：音频窗可解析门，不再是 host 门
        from src.autoslice.entity_audio_verifier import build_local_audio_entity_verifier

        audio_entity_verifier = build_local_audio_entity_verifier(
            source_media=padded,
            output_dir=out_root,
            recording_date=str(spec.get("date") or ""),
            source_duration_ms=padded_dur,
            witness_cache_roots=witness_cache_roots,
        )
        argon_ordinary_verifier = audio_entity_verifier
        if native_provider in {"moss", "mai"}:
            from src.autoslice.native_context_witness import build_native_context_verifier

            audio_entity_verifier = build_native_context_verifier(
                fallback=audio_entity_verifier,
                source_media=padded,
                output_dir=out_root,
                provider=native_provider, **(spec.get("local_audio_witness_budget") or {}),
                budget_receipt_path=native_budget_receipt,
                native_observation_cache_root=native_observation_cache_root,
            )

    if audio_entity_verifier is not None:
        from src.autoslice.native_context_witness import (
            build_independent_context_witnesses,
        )

        audio_entity_verifier = build_independent_context_witnesses(
            fallback=audio_entity_verifier,
            source_media=padded,
            output_dir=out_root / "independent-context",
            providers=("mai", "moss"),
            budget_receipt_path=native_budget_receipt,
            native_observation_cache_root=native_observation_cache_root,
            **({"cached_observation_roots": tuple(root / "independent-context" for root in witness_cache_roots)}
               if spec.get("comparison_ordinary_cache_only") is True else {}),
        )

    if spec.get("comparison_ordinary_cache_only") is True:
        from src.autoslice.argon_regression_comparison import CachedOrdinaryAudio
        audio_entity_verifier = CachedOrdinaryAudio(audio_entity_verifier)

    # Read-aloud (danmaku/SC) arbitration is a pure-context judgment, so route it
    # through a general LLM on CPA *before* the audio verifier: this keeps
    # "she read this danmaku" corrections alive when AGY/Gemini is quota-exhausted
    # (regression: agy jingting AND the audio arbitration are both
    # Gemini-family, so one quota wall reverted a slice to garble) and takes AGY
    # off the hot path. For acoustic ambiguity and registered-name conflicts,
    # AGY receives only a candidate-free pinyin witness request and CPA still
    # owns the closed-set choice. With no CPA configured these cases remain
    # unresolved; audio can never inherit final authority.
    from src.autoslice.read_aloud_llm_verifier import build_cpa_read_aloud_verifier

    # Runtime credentials live in cpa.env, not in the producer's ambient env.
    # Use the same canonical medium-effort judge as the remaining text stages.
    # An unbound runtime fails closed when called; audio stays evidence-only.
    read_aloud_llm_call = _build_final_review_llm_call()
    cpa_read_aloud_verifier = build_cpa_read_aloud_verifier(
        read_aloud_llm_call, next_verifier=audio_entity_verifier
    )

    def verify_confusable_entity(request):
        if human_entity_verifier is not None:
            verdict = human_entity_verifier(request)
            if verdict is not None:
                return verdict
        return cpa_read_aloud_verifier(request)

    # Preserve exact-final's provider/cache seams through human/CPA composition.
    for seam in (
        "probe_witness_cache", "exact_source_transcript",
        "probe_exact_source_transcript_cache", "observe_independent_witnesses",
        "observe_cached_independent_witnesses",
        "observe_range_context",
        "observe_secondary_audio_evidence", "observe_secondary_witnesses",
    ):
        callback = getattr(cpa_read_aloud_verifier, seam, None)
        if callable(callback):
            setattr(verify_confusable_entity, seam, callback)

    for name in ("span_termination_source_identity", "span_termination_ordinary_routes",
                 "span_termination_upgrade_enabled", "span_termination_upgrade_status"):
        if hasattr(cpa_read_aloud_verifier, name):
            setattr(verify_confusable_entity, name, getattr(cpa_read_aloud_verifier, name))

    # The native owner's read-only budget reader proves relisten dispatch and
    # no-gain terminal ledgers (41 同一物理疑点的实际补听). The read-aloud layer
    # does not forward it, so expose the audio witness chain's own reader.
    budget_state = getattr(cpa_read_aloud_verifier, "relisten_budget_state", None)
    if not callable(budget_state):
        budget_state = getattr(audio_entity_verifier, "relisten_budget_state", None)
    if callable(budget_state):
        verify_confusable_entity.relisten_budget_state = budget_state
    # The candidate's own producer media (what the native witnesses read); the
    # relisten projection re-validates its provenance before using it (41).
    verify_confusable_entity.relisten_own_source_media = str(padded)

    if spec.get("argon_escalation"):
        from src.autoslice.argon_escalation_integration import build_argon_integration
        verify_confusable_entity.argon_escalation = build_argon_integration(
            config=spec["argon_escalation"], source_media=padded,
            source_duration_ms=padded_dur, ordinary_output_dir=out_root,
            ordinary_verifier=argon_ordinary_verifier)
        integration = verify_confusable_entity.argon_escalation
        if integration is not None:
            from src.autoslice.argon_span_termination import read_upgrade
            verify_confusable_entity.span_termination_upgrade_enabled = True
            verify_confusable_entity.span_termination_upgrade_status = lambda request: read_upgrade(integration, request)

    static_referent_groups = load_referent_groups(
        adapters.profile_asset_file("entity_confusables"), include_singletons=True
    )
    dynamic_groups = []
    topic_resolution_audit: dict[str, object] = {
        "schema_version": "topic-resolution.v1",
        "status": "NO_GRAPH",
        "recording_date": str(spec.get("date") or ""),
        "selected_topic_ids": [],
        "selected_work_ids": [],
        "scoped_entity_ids": [],
        "evidence": [],
        "scoped_graph_context": {
            "schema_version": "topic-scoped-context.v1",
            "status": "NO_GRAPH",
            "recording_date": str(spec.get("date") or ""),
            "topics": [],
            "works": [],
            "entities": [],
        },
    }
    if not adapters.topic_graph_disabled():
        graph_path = adapters.topic_graph_path()
        if graph_path.is_file() and not graph_path.is_symlink():
            try:
                graph, graph_sha = load_topic_entity_graph(
                    graph_path,
                    expected_sha256=adapters.topic_graph_expected_sha256(),
                )
                if dt.datetime.now(dt.timezone.utc) <= dt.datetime.fromisoformat(
                    graph["expires_at"]
                ):
                    topic_evidence = [TopicEvidence("transcript", srt_text)]
                    selection_hook = str(spec.get("selection_hook") or "")
                    if selection_hook:
                        topic_evidence.append(TopicEvidence("selection_hook", selection_hook))
                    if authoritative_chat:
                        topic_evidence.append(
                            TopicEvidence(
                                "structured_chat",
                                "\n".join(item.text for item in authoritative_chat),
                            )
                        )
                    resolution = resolve_topic_context(
                        graph,
                        topic_evidence,
                        recording_date=str(spec.get("date") or ""),
                        graph_sha256=graph_sha,
                    )
                    topic_resolution_audit = resolution.as_dict()
                    topic_resolution_audit["scoped_graph_context"] = build_scoped_topic_context(
                        graph, resolution
                    )
                    dynamic_groups = dynamic_referent_groups(graph, resolution, srt_text)
                else:
                    topic_resolution_audit["status"] = "GRAPH_EXPIRED"
                    topic_resolution_audit["graph_sha256"] = graph_sha
                    topic_resolution_audit["scoped_graph_context"]["status"] = "GRAPH_EXPIRED"
                    topic_resolution_audit["scoped_graph_context"]["graph_sha256"] = graph_sha
            except (OSError, ValueError) as exc:
                topic_resolution_audit["status"] = "GRAPH_INVALID"
                topic_resolution_audit["error"] = f"{type(exc).__name__}: {exc}"
                topic_resolution_audit["scoped_graph_context"]["status"] = "GRAPH_INVALID"
    topic_resolution_path = out_root / f"{cid}.topic-resolution.json"
    topic_resolution_path.write_text(
        json.dumps(topic_resolution_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    referent_groups = merge_referent_groups(static_referent_groups, dynamic_groups)
    return EntityVerificationContext(
        verify_confusable_entity=verify_confusable_entity,
        referent_groups=referent_groups,
        topic_resolution_audit=topic_resolution_audit,
    )


def _apply_entity_authority(
    *,
    srt_text: str,
    authoritative_chat: list[ChatEvidence],
    support_srts: list[str],
    referent_groups: list[ReferentGroup],
    verify_confusable_entity: Callable,
    code_switch_audit: object,
    term_boundary_moves: list[dict],
    padded: Path,
    adapters: TextPipelineAdapters,
    session_topic_absorption_audits: list[dict[str, Any]] | None = None,
    source_truth_protected_cue_indexes: Sequence[int] = (),
    truth_full_ownership: Mapping[str, object] | None = None,
    retained_reviewed_draft: bool = False,
) -> EntityAuthorityResult:
    cue_count = len([cue for cue in parse_srt_cues(srt_text) if cue.text.strip()])
    if any(type(index) is not int or not 1 <= index <= cue_count for index in source_truth_protected_cue_indexes):
        raise RuntimeError('SOURCE_TRUTH_PREVIEW_PROTECTED_CUE_INVALID')
    # A retained reviewed draft deliberately has no initial-ASR witness.  An
    # old sidecar next to the padded media is historical evidence and must not
    # silently become the witness for this new text surface.
    draft_witness_path = (
        None if retained_reviewed_draft else padded.with_suffix(".asr_draft.srt")
    )
    source_witness_srt = (
        draft_witness_path.read_text(encoding="utf-8", errors="replace")
        if draft_witness_path is not None and draft_witness_path.is_file()
        else None
    )
    srt_text, self_reference_absorption_audit = absorb_host_self_references(
        srt_text,
        source_witness_srt=source_witness_srt,
        protected_cue_indexes=tuple(source_truth_protected_cue_indexes),
    )
    srt_text, chat_authority_audit = apply_authoritative_chat_evidence(
        srt_text,
        authoritative_chat,
        support_srt_texts=support_srts,
        referent_groups=referent_groups,
        entity_verifier=verify_confusable_entity,
        protected_cue_indexes=source_truth_protected_cue_indexes,
    )
    chat_authority_audit["self_reference_absorption_audit"] = self_reference_absorption_audit
    chat_authority_audit["session_topic_absorption_audits"] = list(
        session_topic_absorption_audits or []
    )
    if draft_witness_path is not None and draft_witness_path.is_file():
        srt_text, numeric_fact_audit = apply_numeric_fact_provenance_guard(
            draft_witness_path.read_text(encoding="utf-8", errors="replace"),
            srt_text,
            structured_evidence=authoritative_chat,
            matched_structured_evidence=chat_authority_audit.get("applied") or (),
        )
    else:
        numeric_fact_audit = {
            "schema_version": "numeric-fact-provenance-audit.v1",
            "status": "SKIPPED_NO_INITIAL_ASR",
            "reverted": [],
            "reverted_count": 0,
        }
    chat_authority_audit["numeric_fact_provenance_audit"] = numeric_fact_audit
    # 答谢完整性：SC 字幕卡行丢「谢谢」按 draft 见证还原；
    # 窗口内未被答谢的送礼人/SC 发送者只披露不改写（0:23 打码礼物案）。
    from src.autoslice.thank_integrity import (
        restore_thank_prefixes,
        unthanked_donor_disclosure,
    )

    if draft_witness_path is not None and draft_witness_path.is_file():
        srt_text, thank_restore_audit = restore_thank_prefixes(
            srt_text,
            draft_witness_path.read_text(encoding="utf-8", errors="replace"),
        )
    else:
        thank_restore_audit = {
            "schema_version": "thank-prefix-restore-audit.v1",
            "status": "SKIPPED_NO_INITIAL_ASR",
            "restorations": [],
        }
    chat_authority_audit["thank_prefix_restore_audit"] = thank_restore_audit
    chat_authority_audit["unthanked_donor_disclosure"] = unthanked_donor_disclosure(
        srt_text, authoritative_chat
    )
    handled_entity_cues = {
        int(index)
        for key in ("applied", "pending_text_overrides", "entity_repairs", "coreference_repairs")
        for row in chat_authority_audit.get(key) or []
        for index in (
            row.get("cue_indexes")
            or ([row.get("cue_index")] if row.get("cue_index") is not None else [])
        )
    }
    # 怀疑编译器（通用机制，）：位置先验 + 重复一致性都在 chat
    # 证据落定后的最终文本上编译成临时混淆组，与词典/话题图组共用同一个
    # 音频仲裁引擎；positions 非空使它们天然进不了 chat 证据路径。
    opening_group = clip_opening_address_group(
        srt_text,
        load_clip_opening_address_config(adapters.profile_asset_file("clip_opening_address")),
    )
    repetition_groups = repetition_divergence_groups(srt_text)
    # 欠账 #0/#5 落地：短语级重复分歧编译器（抱/帮案）+ 词表
    # 拼音候选发现层（皮毛熊/卖批案）。**披露不改写（降级）**：
    # 91_291 实证单次黑帧强制二选一会顺从确认动态候选（小李小李→小立希李、
    # 干粮之类→干祥子类），不满足保向铁律的证据门（本句音节+结构化证据）。
    # 两条 lane 的产出只进审计披露供审片员/人工复核，不进声学仲裁、不产生
    # 任何文本改写；见证充分的裁决通道（phase 2）见 41 文档欠账 #11。
    from src.autoslice.phonetic_scan import (
        glossary_phonetic_candidate_groups,
        phrase_repetition_divergence_groups,
    )

    phrase_divergence_groups = phrase_repetition_divergence_groups(srt_text)
    phonetic_candidate_groups = glossary_phonetic_candidate_groups(srt_text, referent_groups)
    # AGY/CPA/词表已经完成专名语义定稿。无位置标记的静态/话题实体组只给
    # 结构化聊天匹配与终稿验证使用，绝不能再被黑帧 Gemini 按初始听写强制
    # 二选一。声学层只接明确声明为 transcript_only/clip_initial 的未决槽位，
    # 以及在终稿上新编译的片首/重复异常审计。
    explicit_post_semantic_audio_groups = [
        group
        for group in referent_groups
        if set(group.positions) & {"transcript_only", "clip_initial"}
    ]
    semantic_text_final_groups = [
        group for group in referent_groups if group not in explicit_post_semantic_audio_groups
    ]
    transcript_groups = [
        *explicit_post_semantic_audio_groups,
        *([opening_group] if opening_group is not None else []),
        *repetition_groups,
    ]
    cue_count = len([cue for cue in parse_srt_cues(srt_text) if cue.text.strip()])
    ledger_excluded_cues: set[int] = set()
    for raw_index in source_truth_protected_cue_indexes:
        if (
            isinstance(raw_index, bool)
            or not isinstance(raw_index, int)
            or not 1 <= raw_index <= cue_count
        ):
            raise RuntimeError("SOURCE_TRUTH_PREVIEW_PROTECTED_CUE_INVALID")
        ledger_excluded_cues.add(raw_index)
    srt_text, transcript_entity_audit = verify_transcript_entities(
        srt_text,
        referent_groups=transcript_groups,
        entity_verifier=verify_confusable_entity,
        excluded_cue_indexes=handled_entity_cues | ledger_excluded_cues,
        truth_full_ownership=truth_full_ownership,
    )
    if ledger_excluded_cues:
        transcript_entity_audit["ledger_excluded_cue_indexes"] = sorted(ledger_excluded_cues)
        transcript_entity_audit["source_truth_preview_excluded_cue_indexes"] = sorted(
            ledger_excluded_cues
        )
    chat_authority_audit["transcript_entity_audit"] = transcript_entity_audit
    chat_authority_audit["post_semantic_entity_policy"] = {
        "schema_version": "post-semantic-entity-policy.v1",
        "status": "SEMANTIC_TEXT_FINAL",
        "reason_code": "POST_SEMANTIC_ENTITY_AUDIO_OVERRIDE_DISABLED",
        "semantic_text_final_groups": [
            [entity.canonical for entity in group.entities] for group in semantic_text_final_groups
        ],
        "explicit_audio_groups": [
            [entity.canonical for entity in group.entities]
            for group in explicit_post_semantic_audio_groups
        ],
        "dynamic_audio_group_count": len(repetition_groups)
        + (1 if opening_group is not None else 0),
        # 披露专用（不进声学仲裁、零改写）：审片员与人工复核的注意力提示。
        "phrase_divergence_candidates_disclosure_only": [
            [entity.canonical for entity in group.entities] for group in phrase_divergence_groups
        ],
        "phonetic_candidates_disclosure_only": [
            [entity.canonical for entity in group.entities] for group in phonetic_candidate_groups
        ],
    }
    chat_authority_audit["code_switch_surface_audit"] = code_switch_audit
    chat_authority_audit["term_boundary_audit"] = {
        "schema_version": "term-boundary-audit.v1",
        "status": "APPLIED" if term_boundary_moves else "NO_CHANGE",
        "moves": term_boundary_moves,
    }
    chat_authority_audit.setdefault("entity_repairs", []).extend(
        transcript_entity_audit.get("repairs") or []
    )
    # draft/终稿专名差异只披露，不再把语义定稿交给黑帧 Gemini 强制回归
    # 初始听写。初始 ASR 的价值是可追溯证人，不是语义修正后的 postcondition。
    wd_audits: list[dict] = []
    wd_groups = [
        group
        for group in referent_groups
        if getattr(group, "positions", ()) and "witness_disagreement" in group.positions
    ]
    if wd_groups and draft_witness_path is not None and draft_witness_path.is_file():
        draft_witness = draft_witness_path.read_text(encoding="utf-8", errors="replace")
        for wd_group in wd_groups:
            suspicious = witness_disagreement_cues(draft_witness, srt_text, wd_group)
            if not suspicious:
                continue
            wd_audits.append(
                {
                    "schema_version": "witness-disagreement-audit.v2",
                    "status": "SEMANTIC_AUTHORITY_PRESERVED",
                    "reason_code": "DRAFT_WITNESS_IS_NOT_POST_SEMANTIC_AUTHORITY",
                    "suspicious_cue_indexes": suspicious,
                    "candidate_entities": [entity.canonical for entity in wd_group.entities],
                }
            )
    chat_authority_audit["witness_disagreement_audits"] = wd_audits
    # 语义层引入词与 draft 不同是专名修正的预期结果，只留来源差异审计。
    # 禁止再构造 {终稿专名, draft 片段} 给 Gemini 强制二选一。
    introduced_term_audits: list[dict] = []
    if draft_witness_path is not None and draft_witness_path.is_file():
        from src.autoslice.term_authority import protected_terms as _protected_terms

        draft_witness_text = draft_witness_path.read_text(encoding="utf-8", errors="replace")
        for row in introduced_term_cues(draft_witness_text, srt_text, _protected_terms()):
            introduced_term_audits.append(
                {
                    "schema_version": "introduced-term-audit.v2",
                    "status": "SEMANTIC_AUTHORITY_PRESERVED",
                    "reason_code": "DRAFT_WITNESS_IS_NOT_POST_SEMANTIC_AUTHORITY",
                    "introduced_term": row,
                }
            )
    chat_authority_audit["introduced_term_audits"] = introduced_term_audits
    return EntityAuthorityResult(
        srt_text=srt_text,
        chat_authority_audit=chat_authority_audit,
        transcript_entity_audit=transcript_entity_audit,
        handled_entity_cues=handled_entity_cues,
    )


def _run_final_review(
    *,
    srt_text: str,
    chat_authority_audit: dict,
    handled_entity_cues: set[int],
    verify_confusable_entity: Callable,
    adapters: TextPipelineAdapters,
    authoritative_chat: list[ChatEvidence] | tuple[ChatEvidence, ...] = (),
    selection_hook: str = "",
    referent_groups: Sequence[object] = (),
    clip_context: Mapping[str, object] | None = None,
    source_truth_protected_cue_indexes: Sequence[int] = (),
    carryover_file: Path | None = None,
    priority_raw_findings: Sequence[Mapping[str, Any]] = (),
    draft_fidelity_contexts: Sequence[Mapping[str, Any]] = (),
    screen_read_probe: Callable[[int, int], Mapping[str, object]] | None = None,
    initial_provider_adjudication_count: int | None = None,
    source_media_sha256: str | None = None,
) -> tuple[str, dict]:
    final_review_audit: dict[str, Any] = {
        "schema_version": "final-review-audit.v1",
        "status": "SKIPPED",
    }
    original_srt_text = srt_text
    staged_entity_repairs: list[dict[str, Any]] = []
    if os.environ.get("AUTOSLICE_DISABLE_FINAL_REVIEW") != "1":
        try:
            from src.autoslice.semantic_request_history import with_partial_usage_guard
            from .correction_review_parallel import prefetch_correction_reviews
            controller_call = _build_final_review_llm_call()
            review_llm_call = with_partial_usage_guard(controller_call, chat_authority_audit,
                usage_dependency="SOFT_PRESSURE", source_media_sha256=source_media_sha256,
                candidate_id=(clip_context or {}).get("candidate_id"))
            structured_context_text = _final_review_structured_context(
                selection_hook=selection_hook,
                authoritative_chat=authoritative_chat,
            )
            candidate_context_text = (
                clip_context_prompt_text(clip_context) if isinstance(clip_context, Mapping) else ""
            )
            # 终审结转并入：上轮 exact 终审声学
            # 确证却无权落盘的修复，以 raw 行进同一个解析/校验循环（stale
            # suspect 天然过滤，零特权）——确定性闭环。
            carryover_rows = (
                load_replayable_final_review_carryover(carryover_file)
                if carryover_file is not None
                else []
            )
            priority_rows = [*carryover_rows, *priority_raw_findings]
            from src.autoslice.audio_language_discovery import current_language_discovery_context
            language_context, language_findings = current_language_discovery_context(
                srt_text, chat_authority_audit, source_media_sha256=source_media_sha256,
            )
            candidate_context_text += language_context
            priority_rows.extend(language_findings)
            review_findings = audit_final_subtitles(
                srt_text,
                llm_call=review_llm_call,
                extract_json=extract_json_object,
                glossary_text=adapters.review_glossary(),
                structured_context_text=structured_context_text,
                candidate_context_text=candidate_context_text,
                candidate_context=clip_context,
                extra_raw_findings=priority_rows,
                prioritize_extra_raw_findings=bool(priority_rows),
                draft_fidelity_contexts=draft_fidelity_contexts,
            )
            protected_review_cues = set(handled_entity_cues)
            cue_count = len([cue for cue in parse_srt_cues(srt_text) if cue.text.strip()])
            source_truth_protected_cues: set[int] = set()
            for raw_index in source_truth_protected_cue_indexes:
                if (
                    isinstance(raw_index, bool)
                    or not isinstance(raw_index, int)
                    or not 1 <= raw_index <= cue_count
                ):
                    raise RuntimeError("SOURCE_TRUTH_PREVIEW_PROTECTED_CUE_INVALID")
                source_truth_protected_cues.add(raw_index)
            protected_review_cues.update(source_truth_protected_cues)
            for row in chat_authority_audit.get("applied") or []:
                for index in row.get("cue_indexes") or []:
                    protected_review_cues.add(int(index))
            # 注册实体词面（canonical+surfaces）：suspect 命中即实体选边，
            # T1 纯文本车道让位声学仲裁（kmx/乒乓球保向铁律）。
            entity_surfaces = frozenset(
                surface
                for group in referent_groups
                for entity in getattr(group, "entities", ())
                for surface in (
                    getattr(entity, "canonical", ""),
                    *getattr(entity, "surfaces", ()),
                )
                if surface
            )
            srt_text, final_review_audit = route_findings(
                srt_text,
                review_findings,
                protected_cue_indexes=protected_review_cues,
                entity_surface_set=entity_surfaces,
            )
            final_review_audit["source_truth_protected_cue_indexes"] = sorted(
                source_truth_protected_cues
            )
            # 无人值守自定夺：非同音建议交专用的“完整 cue + 前后语境 +
            # 上下文音频”声学相容度检查，再由固定代码规则融合。没有安全候选的
            # phonetic/segmentation/spoken_unit 怀疑也必须进入同一条候选盲听观察
            # 支路；disclosure_only 仍保持文字语境 KEEP 的零音频合同。验证器不能
            # 选择或生成文本；chat/词典权威 cue 已在 route 阶段被挡。
            adjudicable = [
                row
                for row in (final_review_audit.get("findings") or [])
                if row.get("routed") == "disclosure"
                and (
                    row.get("proposed_full_cue") is not None
                    or (
                        row.get("proposed_full_cue") is None
                        and not str(row.get("suggestion") or "").strip()
                        and str(row.get("repair_class") or "")
                        in {"phonetic", "segmentation", "spoken_unit"}
                        and bool(str(row.get("suspect") or "").strip())
                    )
                )
            ]

            if initial_provider_adjudication_count is not None and (
                isinstance(initial_provider_adjudication_count, bool)
                or not isinstance(initial_provider_adjudication_count, int)
                or initial_provider_adjudication_count < 0
            ):
                raise ValueError("CPA_ADJUDICATION_USAGE_INVALID")
            from threading import Lock
            correction_adjudication_count = 0
            correction_count_lock = Lock()

            def adjudicate(live_srt: str, finding: dict[str, Any]):
                nonlocal correction_adjudication_count
                with correction_count_lock:
                    correction_adjudication_count += 1
                    adjudication_number = correction_adjudication_count
                from src.autoslice.semantic_request_history import HISTORY_KEY, sources_from_audit
                history_sources = sources_from_audit(chat_authority_audit)
                if history_sources:
                    finding = {**finding, HISTORY_KEY: history_sources}
                from src.autoslice.retained_partial_evidence import partial_ranges_from_audit
                partial_ranges = partial_ranges_from_audit(chat_authority_audit)
                if partial_ranges:
                    finding = {**finding, "_prior_range_acoustic_observations": [
                        *finding.get("_prior_range_acoustic_observations", []), *partial_ranges]}
                return adjudicate_context_finding(
                    live_srt,
                    entity_verifier=verify_confusable_entity,
                    finding=finding,
                    clip_context=clip_context,
                    judge_llm_call=with_cpa_resource_pressure(
                        review_llm_call,
                        (initial_provider_adjudication_count or 0)
                        + adjudication_number,
                        MAX_CONTEXT_ADJUDICATIONS,
                    ),
                    screen_read_probe=screen_read_probe,
                )

            # Only CPA-unresolved findings need local audio. Eager prewarming
            # would spend calls before that decision and defeat text-first.
            prewarm_receipt = {
                "schema_version": "context-adjudication-witness-prewarm.v1",
                "status": "SKIPPED",
                "reason_code": "CPA_TEXT_FIRST_DEMAND_DRIVEN_AUDIO",
                "fired_count": 0,
                "mutation_authorized": False,
            }

            with prefetch_correction_reviews(srt_text, adjudicable, adjudicate=adjudicate,
                    original_srt_text=original_srt_text, provider_call=controller_call) as prepared:
                (
                    srt_text,
                    adjudication_count,
                    partial,
                    newly_applied,
                    staged_entity_repairs,
                ) = adjudicate_routed_findings(
                    srt_text,
                    adjudicable,
                    max_adjudications=None,
                    adjudicate=prepared,
                    original_srt_text=original_srt_text,
                )
            final_review_audit['correction_review_execution'] = dict(prepared.receipt)
            if newly_applied:
                final_review_audit["applied_count"] = (
                    int(final_review_audit.get("applied_count") or 0) + newly_applied
                )
                final_review_audit["status"] = "APPLIED"
            final_review_audit["context_adjudication_count"] = correction_adjudication_count
            final_review_audit["context_adjudication_target_count"] = adjudication_count
            final_review_audit["context_adjudication_budget"] = MAX_CONTEXT_ADJUDICATIONS
            final_review_audit["context_adjudication_budget_policy"] = "SOFT"
            final_review_audit["context_adjudication_witness_prewarm"] = prewarm_receipt
            final_review_audit["priority_raw_finding_count"] = len(priority_rows)
            final_review_audit.update(_review_priority_candidate_counts(priority_raw_findings))
            if partial:
                final_review_audit["status"] = "PARTIAL"
            # 交付事故类机制：区分「证据裁决后的保留」与「基础设施
            # 失败导致的未决」。前者（OBSERVED 下 keep-current）是正当结论；
            # 后者（provider 额度/异常，裁决根本没发生）不许当作终局，记入
            # infra_unresolved 由 run_text_pipeline 拒绝带伤交付、转 runner 重试。
            # F21 刻意不收 AUDIO_VERIFIER_UNAVAILABLE：那是「本机没有声学
            # provider」不是瞬时故障，重试永不会好；它照旧走 exact-final 未决门。
            infra_unresolved = []
            for row in adjudicable:
                adjudication = row.get("context_audio_adjudication") or {}
                if adjudication.get("repaired") or adjudication.get("status") != "UNCERTAIN":
                    continue
                verdict = adjudication.get("verdict") or {}
                reason_code = str(verdict.get("reason_code") or "")
                if reason_code in {"ENTITY_AUDIO_PROVIDER_FAILED", "CONTEXT_VERIFIER_ERROR"}:
                    infra_unresolved.append(
                        {
                            "cue_index": row.get("cue_index"),
                            "suspect": row.get("suspect"),
                            "reason_code": reason_code,
                        }
                    )
            final_review_audit["infra_unresolved"] = infra_unresolved
            final_review_audit["infra_unresolved_count"] = len(infra_unresolved)
            if initial_provider_adjudication_count is not None:
                final_review_audit["cpa_adjudication_usage"] = {
                    "provider_adjudication_count": (
                        initial_provider_adjudication_count + correction_adjudication_count
                    ),
                    "prior_provider_adjudication_count": initial_provider_adjudication_count,
                    "soft_limit": MAX_CONTEXT_ADJUDICATIONS,
                    "policy": "SOFT",
                }
            if staged_entity_repairs:
                existing_repairs = chat_authority_audit.get("entity_repairs")
                if existing_repairs is None:
                    chat_authority_audit["entity_repairs"] = list(staged_entity_repairs)
                elif isinstance(existing_repairs, list):
                    existing_repairs.extend(staged_entity_repairs)
                else:
                    raise TypeError("CHAT_AUTHORITY_ENTITY_REPAIRS_CONTRACT_INVALID")
            from src.autoslice.final_review_auditor import refresh_rebuilt_current_risks
            try:
                refresh_rebuilt_current_risks(
                    srt_text, adjudicable, llm_call=review_llm_call,
                    extract_json=extract_json_object,
                    structured_context_text=structured_context_text,
                    candidate_context_text=candidate_context_text,
                )
            except FinalReviewAuditError as exc:
                # Keep actual correction/audio receipts and cumulative usage.
                # A failed risk refresh grants neither completion nor release.
                final_review_audit.update(
                    status="AUDITOR_UNAVAILABLE", release_gate="BLOCK",
                    reason_codes=[exc.reason_code],
                )
        except FinalReviewAuditError as exc:
            srt_text = original_srt_text
            final_review_audit = {
                "schema_version": "final-review-audit.v1",
                "status": "AUDITOR_UNAVAILABLE",
                "release_gate": "BLOCK",
                "reason_codes": [exc.reason_code],
                "discovery": _pf.auditor_unavailable_discovery(
                    exc.detail, _pf.provider_failure_detail_from_cause(exc)
                ),
                "findings": [],
                "applied_count": 0,
                "error_type": type(exc).__name__,
            }
        except Exception as exc:
            srt_text = original_srt_text
            final_review_audit = {
                "schema_version": "final-review-audit.v1",
                "status": "AUDITOR_UNAVAILABLE",
                "release_gate": "BLOCK",
                "reason_codes": ["FINAL_REVIEW_UNEXPECTED_ERROR"],
                "discovery": {
                    "status": "AUDITOR_UNAVAILABLE",
                    "detail": type(exc).__name__,
                },
                "findings": [],
                "applied_count": 0,
                "error_type": type(exc).__name__,
            }
    partial = ((chat_authority_audit.get("retained_reviewed_draft") or {}).get("partial_evidence") or {})
    if partial:
        final_review_audit["retained_partial_evidence"] = copy.deepcopy(partial)
        if isinstance(final_review_audit.get("cpa_adjudication_usage"), dict):
            final_review_audit["cpa_adjudication_usage"]["partial_usage_lineage"] = {
                key: partial[key] for key in ("manifest", "raw_cpa_usage",
                    "partial_logical_adjudication_count", "partial_logical_usage_status")}
    from src.autoslice.semantic_request_history import preserve_partial_usage
    if isinstance(final_review_audit.get("cpa_adjudication_usage"), Mapping):
        final_review_audit["cpa_adjudication_usage"] = preserve_partial_usage(
            final_review_audit["cpa_adjudication_usage"], chat_authority_audit)
    return srt_text, final_review_audit


# Stage timestamps (J-lite). Wrap the function object rather than its body:
# _pending_source_review_contract_sha256 hashes this function's source.
_run_final_review = _stage_logged("correction_review", _run_final_review)


def _postburn_request_sha256(request: Mapping[str, object]) -> str:
    payload = {
        key: value for key, value in request.items() if key != "request_sha256"
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _postburn_text_artifact_matches(path_value: object, expected: object) -> bool:
    path = Path(str(path_value or ""))
    if not path.is_file() or path.is_symlink():
        return False
    try:
        value = path.read_bytes().decode("utf-8", errors="strict")
    except (OSError, UnicodeError):
        return False
    return (
        "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
        == str(expected or "")
    )


def validated_postburn_priority_findings(
    *,
    final_srt_text: str,
    verified_authority_audit: Mapping[str, object] | None,
    candidate_id: str,
    clip_context: Mapping[str, object] | None,
    expected_media_sha256: str | None = None,
    allow_parser_recovery: bool = False,
    allow_clean_checkpoint: bool = False,
) -> tuple[list[dict[str, Any]], str] | None:
    """Return only completed, current-byte postburn rows for normal CPA.

    The BCUT/CPA postburn audit is evidence and discovery.  This handoff
    deliberately strips no fields and applies no mutation: the ordinary exact
    final reviewer receives the rows as priority raw findings and must run its
    existing text/entity/audio/CPA adjudication before a repair can be used.
    Missing or drifted bindings return ``None`` so the caller keeps the
    postburn block rather than treating an unbound receipt as a new review.
    Current-text recovery may explicitly validate a clean same-byte checkpoint;
    it returns no findings and grants no release over a later semantic BLOCK.
    """

    if not isinstance(verified_authority_audit, Mapping):
        return None
    postburn = verified_authority_audit.get("postburn_subtitle_audio_review")
    if not isinstance(postburn, Mapping):
        nested = verified_authority_audit.get("final_review_audit")
        postburn = (
            nested.get("postburn_subtitle_audio_review")
            if isinstance(nested, Mapping)
            else None
        )
    if not isinstance(postburn, Mapping):
        return None
    clean_checkpoint = bool(
        allow_clean_checkpoint
        and postburn.get("status") == "CLEAN"
        and postburn.get("release_gate") == "PASS"
        and postburn.get("reason_codes") == []
        and postburn.get("findings") == []
    )
    parser_recovery = bool(
        allow_parser_recovery
        and postburn.get("status") == "AUDITOR_UNAVAILABLE"
        and postburn.get("error_type") == "FinalReviewAuditError"
        and postburn.get("reason_codes")
        == ["POSTBURN_SUBTITLE_AUDIO_CPA_UNAVAILABLE"]
        and postburn.get("findings") == []
    )
    if (
        postburn.get("schema_version") != "postburn-subtitle-audio-review.v1"
        or (postburn.get("status") != "FLAGGED" and not parser_recovery and not clean_checkpoint)
        or (postburn.get("release_gate") != "BLOCK" and not clean_checkpoint)
    ):
        return None
    findings = postburn.get("findings")
    if (
        not isinstance(findings, list)
        or (not findings and not parser_recovery and not clean_checkpoint)
        or postburn.get("validated_finding_count") != len(findings)
    ):
        return None
    final_srt_sha256 = "sha256:" + hashlib.sha256(
        final_srt_text.encode("utf-8")
    ).hexdigest()
    if postburn.get("reviewed_srt_sha256") != final_srt_sha256:
        return None
    request = postburn.get("cpa_request")
    binding = postburn.get("subtitle_audio_binding")
    if not isinstance(request, Mapping) or not isinstance(binding, Mapping):
        return None
    if (
        request.get("schema_version") != "postburn-subtitle-audio-cpa-request.v1"
        or request.get("candidate_id") != candidate_id
        or request.get("final_srt_sha256") != final_srt_sha256
        or binding.get("candidate_id") != candidate_id
        or binding.get("final_srt_sha256") != final_srt_sha256
        or request.get("witness_authority") != "EVIDENCE_ONLY"
        or request.get("request_sha256")
        != _postburn_request_sha256(request)
    ):
        return None
    candidate_context_text = (
        clip_context_prompt_text(clip_context)
        if isinstance(clip_context, Mapping)
        else ""
    )
    if request.get("candidate_context_sha256") != (
        "sha256:" + hashlib.sha256(candidate_context_text.encode("utf-8")).hexdigest()
    ):
        return None
    actual_media_sha256 = str(request.get("actual_media_sha256") or "")
    if (
        not actual_media_sha256
        or binding.get("actual_media_sha256") != actual_media_sha256
        or (expected_media_sha256 is not None and actual_media_sha256 != expected_media_sha256)
        or any(
            request.get(key) != binding.get(key)
            for key in (
                "witness_srt_sha256",
                "audio_sha256",
                "raw_result_sha256",
                "receipt_sha256",
                "intro_offset_ms",
            )
        )
    ):
        return None
    calls = postburn.get("cpa_calls")
    cache_identity = request.get("cpa_cache_identity")
    runtime_binding = request.get("provider_runtime_binding")
    if (
        not isinstance(calls, list)
        or not calls
        or not isinstance(cache_identity, Mapping)
        or not isinstance(runtime_binding, Mapping)
        or not isinstance(cache_identity.get("transport"), str)
        or not cache_identity.get("transport")
        or not isinstance(cache_identity.get("models"), list)
        or not cache_identity.get("models")
        or not all(isinstance(model, str) and model for model in cache_identity["models"])
        or not isinstance(cache_identity.get("effort"), str)
        or not cache_identity.get("effort")
        or postburn.get("cpa_cache_identity") != dict(cache_identity)
        or postburn.get("provider_runtime_binding") != dict(runtime_binding)
    ):
        return None
    for call in calls:
        if (
            not isinstance(call, Mapping)
            or call.get("dispatch_status") != "COMPLETED"
            or call.get("request_sha256") != request.get("request_sha256")
            or not _postburn_text_artifact_matches(
                call.get("prompt_path"), call.get("prompt_sha256")
            )
            or not _postburn_text_artifact_matches(
                call.get("response_path"), call.get("response_sha256")
            )
        ):
            return None
    try:
        cues = [cue for cue in parse_srt_cues(final_srt_text) if cue.text.strip()]
    except (TypeError, ValueError):
        return None
    validated_rows: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            return None
        # A postburn receipt is discovery evidence.  A typed repair in this
        # field would bypass the ordinary exact-final adjudication chain.
        if "exact_release_adjudication" in finding or "mutation_authority" in finding:
            return None
        raw_cue_index = finding.get("cue_index")
        if raw_cue_index is None:
            raw_cue_index = finding.get("cue")
        if isinstance(raw_cue_index, bool):
            return None
        try:
            cue_index = int(raw_cue_index)
        except (TypeError, ValueError):
            return None
        if not 1 <= cue_index <= len(cues):
            return None
        base_sha256 = str(finding.get("base_text_sha256") or "").removeprefix("sha256:")
        if len(base_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in base_sha256
        ):
            return None
        if hashlib.sha256(cues[cue_index - 1].text.encode("utf-8")).hexdigest() != base_sha256:
            return None
        row = dict(finding)
        row["cue"] = cue_index
        row["cue_index"] = cue_index
        validated_rows.append(row)
    return validated_rows, str(request["request_sha256"])


def _persisted_secondary_audio_observations(
    verified_authority_audit: Mapping[str, object] | None,
) -> tuple[Mapping[str, Any], ...]:
    """Read an explicit persisted secondary-receipt handoff for exact review.

    The verifier/supplement runner owns reading and hashing its receipt files.
    The normal producer only accepts the resulting handoff from the authority
    audit; it never searches an output or cache tree.  The final auditor still
    validates each observation against the current request and artifact chain.
    """

    if not isinstance(verified_authority_audit, Mapping):
        return ()
    containers: list[Mapping[str, object]] = [verified_authority_audit]
    nested = verified_authority_audit.get("final_review_audit")
    if isinstance(nested, Mapping):
        containers.append(nested)
    for container in containers:
        handoff = container.get("secondary_audio_handoff")
        if isinstance(handoff, Mapping):
            rows = handoff.get("observations")
            if (
                handoff.get("schema_version")
                not in {None, "secondary-audio-observation-handoff.v1"}
                or handoff.get("mutation_authorized") is not False
                or not isinstance(rows, list)
            ):
                continue
        else:
            rows = container.get("prior_secondary_audio_observations")
            if not isinstance(rows, list):
                rows = container.get("secondary_audio_observations")
            if not isinstance(rows, list):
                continue
        observations = tuple(row for row in rows if isinstance(row, Mapping))
        if observations:
            return observations
    return ()


def _persisted_exact_source_transcript_handoffs(
    authority: Mapping[str, object] | None,
) -> tuple[Mapping[str, Any], ...]:
    """Carry original explicit transcript receipts out of the prior review.

    No cache search or receipt rebinding: the final consumer validates these
    old observations against their original inputs and the current audio.
    """
    if not isinstance(authority, Mapping):
        return ()
    containers = [authority]
    nested = authority.get("final_review_audit")
    if isinstance(nested, Mapping):
        containers.append(nested)
    result, seen = [], set()
    for container in containers:
        rows = list(container.get("prior_exact_source_transcript_handoffs") or [])
        for field in ("findings", "resolved_findings"):
            for row in container.get(field) or []:
                if not isinstance(row, Mapping):
                    continue
                adjudication = row.get("exact_release_adjudication")
                if isinstance(adjudication, Mapping):
                    rows.append(adjudication.get("exact_source_transcript_handoff"))
        for handoff in rows:
            if not isinstance(handoff, Mapping):
                continue
            receipt = handoff.get("receipt_sha256")
            if isinstance(receipt, str) and receipt not in seen:
                seen.add(receipt)
                result.append(handoff)
    return tuple(result)


def _pending_legacy_acoustic_convergence_findings(
    srt_text: str, authority: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    """Reopen a legacy history choice whose ear still requested evidence.

    This nominates the actual surviving cue for ordinary review without
    manufacturing a replacement or restoring a historical subtitle surface.
    Old convergence receipts remain intact; an empty new discovery cannot
    silently settle their incomplete acoustic decision.
    """
    receipt = (authority or {}).get("exact_final_cpa_self_heal")
    if not isinstance(receipt, Mapping):
        return []
    _validate_exact_final_cpa_self_heal(
        authority, expected_srt_sha256="sha256:" + hashlib.sha256(
            srt_text.encode("utf-8")
        ).hexdigest(), allow_pending=True,
    )
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    pending: dict[int, dict[str, object]] = {}
    for item in receipt.get("passes") or []:
        for repair in item.get("repairs") or []:
            if (repair.get("mutation_authority") or {}).get("basis") != (
                "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED"
            ):
                continue
            binding = repair.get("acoustic_witness_binding") or {}
            judge = binding.get("judge") or {}
            needs_evidence = (
                judge.get("needs_audio_requested") is True
                or judge.get("needs_audio_request_unresolved") is True
                or ("needs_audio_requested" not in judge
                    and judge.get("needs_audio") is True)
            )
            index = repair.get("cue_index")
            if not needs_evidence or type(index) is not int or not 1 <= index <= len(cues):
                continue
            cue = cues[index - 1]
            from src.autoslice.surface_canon import canonicalize_caption_display_surfaces
            surviving_after, _ = canonicalize_caption_display_surfaces(str(repair.get("after") or ""))
            if (surviving_after != cue.text
                or repair.get("matched_start_ms") != cue.start_ms
                or repair.get("matched_end_ms") != cue.end_ms):
                continue
            pending[index] = {
                "cue": index, "kind": "context", "suspect": cue.text,
                "proposed_full_cue": None, "repair_class": "phonetic",
                "why": "Prior CPA acoustic decision still needs evidence; history convergence did not establish this current utterance.",
            }
    return list(pending.values())


def _retained_pending_audio_findings(
    srt_text: str, authority: Mapping[str, object] | None, *,
    clip_context: Mapping[str, object], timeline_offset_ms: int,
) -> list[dict[str, object]]:
    """Restore immutable pending audio requests for surviving physical targets."""
    from src.autoslice.audio_question_identity import same_audio_question
    from src.autoslice.final_review_auditor import _candidate_blind_missing_proposal_scaffold
    from src.autoslice.semantic_audio_recovery import _physical_target, recover_semantic_audio_range

    authority = authority or {}
    review = authority.get("final_review_audit")
    if not isinstance(review, Mapping):
        return []
    old_rows = []
    history = authority.get("retained_delivery_review_history")
    if (isinstance(history, Mapping)
            and history.get("role") == "HISTORY_AND_REVIEW_NOMINATIONS_ONLY"):
        parent = history.get("parent")
        if isinstance(parent, Mapping) and parent.get("role") == "PENDING_OBSERVATIONS_ONLY":
            try:
                saved, sha = _continuation_read_json(
                    Path(parent["path"]), label="PENDING_AUDIO_PARENT", review=True,
                )
                _continuation_require_sha(sha, parent.get("sha256"), label="PENDING_AUDIO_PARENT")
                historical_review = history.get("review")
                if isinstance(historical_review, Mapping) and saved.get("final_review_audit") == historical_review:
                    old_rows.extend(historical_review.get("findings") or [])
            except (CurrentTextContinuationError, KeyError, TypeError, OSError):
                pass  # A missing/unbound history cannot authorize acquisition.
    correction = review.get("correction_pass")
    if isinstance(correction, Mapping):
        old_rows.extend(correction.get("findings") or [])
    # A later explicit current decision wins. Requestless retry-block summaries
    # contain no new decision and cannot erase the older evidence obligation.
    old_rows.extend(review.get("findings") or [])
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    pending = []
    for index in sorted({row.get("cue_index") for row in review.get("findings") or []
                         if isinstance(row, Mapping) and type(row.get("cue_index")) is int}):
        if not 1 <= index <= len(cues):
            continue
        cue = cues[index - 1]
        nomination = {"cue": index, "cue_index": index, "kind": "context", "suspect": cue.text,
                      "base_text_sha256": hashlib.sha256(cue.text.encode("utf-8")).hexdigest(),
                      "proposed_full_cue": None, "repair_class": "phonetic",
                      "why": "A saved CPA request still requires contextual audio for this unchanged physical utterance."}
        scaffold = _candidate_blind_missing_proposal_scaffold(srt_text, nomination)
        if scaffold is None:
            continue
        try:
            current = build_context_adjudication_request(
                srt_text, scaffold, clip_context=clip_context,
                source_media_timeline_offset_ms=timeline_offset_ms,
            )
        except (ValueError, TypeError):
            continue
        bound_context = current.get("clip_context_binding") or {}
        if not bound_context.get("candidate_id") or not bound_context.get("source_media_sha256s"):
            continue
        chosen = None
        for row in old_rows:
            if not isinstance(row, Mapping):
                continue
            old = row.get("exact_release_adjudication") or row.get("context_audio_adjudication")
            request = old.get("request") if isinstance(old, Mapping) else None
            # Same pending audio question (G6 shared judgment): cue text, the
            # physical target and the source binding; mode, the local clock and
            # a rebuilt whole-clip context are recorded only (admin).
            if (not isinstance(request, Mapping) or _physical_target(request) is None
                    or not same_audio_question(request, current, compare_proposed=False,
                                               strict_source=True)[0]):
                continue
            recovery = history_audio_recovery_from_adjudication(old, request=request)
            mutation = old.get("mutation_authority")
            mutation_pass = isinstance(mutation, Mapping) and mutation.get("status") == "PASS"
            if recovery is not None and old.get("repaired") is False and not mutation_pass:
                chosen = old
            else:
                witness_judge = old.get("witness_judge")
                judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
                convergence = old.get("cpa_missing_proposal_convergence")
                if (old.get("repaired") is True or mutation_pass
                        or (isinstance(judge, Mapping) and judge.get("needs_audio_requested") is False)
                        or (isinstance(convergence, Mapping) and convergence.get("needs_audio") is False)):
                    chosen = None
        if chosen is None:
            continue
        nomination["exact_release_adjudication"] = copy.deepcopy(chosen)
        restored, check = recover_semantic_audio_range(
            srt_text=srt_text, finding=nomination, check_request=current,
            entity_verifier=None, llm_call=None,
        )
        if check and (check.get("reason_code") == "SEMANTIC_RANGE_OBSERVER_UNAVAILABLE"
                      or check.get("status") == "OBSERVED"):
            pending.append(restored)
    return pending


def _pending_current_exact_findings(
    srt_text: str, authority: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    """Keep a bound unresolved cue on the ordinary discovery agenda.

    An unresolved BLOCK nomination carries no prior mutation power. A
    historical disclosure that the current policy now requires repairing
    carries its candidate back through the ordinary parser and fresh judge.
    A subsequent empty discovery cannot erase an unresolved current review.
    """
    previous = (authority or {}).get("final_review_audit")
    if not isinstance(previous, Mapping):
        return []
    current_sha = "sha256:" + hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    reviewed_sha = previous.get("reviewed_srt_sha256")
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    pending: dict[int, dict[str, object]] = {}
    if reviewed_sha == current_sha:
        from src.autoslice.final_review_contract import pending_noncritical_repair
        for row in previous.get("unresolved_findings_disclosed") or []:
            if not isinstance(row, Mapping):
                continue
            if not pending_noncritical_repair(row) and previous.get("meaning_policy") == MEANING_POLICY_VERSION:
                continue
            index = row.get("cue_index")
            if (type(index) is not int or not 1 <= index <= len(cues)
                    or row.get("base_text_sha256") != hashlib.sha256(cues[index - 1].text.encode()).hexdigest()):
                continue
            # Retain the question, never its old answer or release decision.
            pending[index] = {"cue": index, **{
                key: copy.deepcopy(row[key]) for key in (
                    "kind", "repair_class", "why", "suspect", "proposed_full_cue",
                    "source_surface", "evidence_cue_ids", "continuation_plan",
                    "content_risk", "risk_basis",
                ) if key in row}, "candidate_provenance": {
                    "kind": "pending_current_exact_review", "mutation_authorized": False,
                    "previous_reviewed_srt_sha256": reviewed_sha,
                    "previous_content_risk": row.get("content_risk"),
                    "previous_risk_basis": row.get("risk_basis"),
                }}
    if previous.get("release_gate") != "BLOCK":
        return list(pending.values())
    successor_validated = False
    if reviewed_sha != current_sha:
        receipt = (authority or {}).get("exact_final_cpa_self_heal")
        if not isinstance(receipt, Mapping):
            return []
        _validate_exact_final_cpa_self_heal(
            authority, expected_srt_sha256=current_sha, allow_pending=True,
        )
        if reviewed_sha not in {
            row.get("input_srt_sha256") for row in receipt.get("passes") or []
        }:
            return []
        successor_validated = True
    for row in previous.get("findings") or []:
        if not isinstance(row, Mapping):
            continue
        index = row.get("cue_index")
        if type(index) is not int or not 1 <= index <= len(cues):
            continue
        cue = cues[index - 1]
        if row.get("base_text_sha256") != hashlib.sha256(cue.text.encode()).hexdigest():
            continue
        from src.autoslice.semantic_retry_progress import continuation_plan
        plan = continuation_plan(row.get('continuation_plan'))
        adjudication = row.get("exact_release_adjudication")
        request = adjudication.get("request") if isinstance(adjudication, Mapping) else None
        if not isinstance(request, Mapping):
            # A failed witness-request builder can leave a legitimate doubt
            # without acoustic geometry. The exact current review SHA plus
            # cue/base binding keeps it on the agenda, with zero old authority.
            # No such fallback is allowed across a text/grid successor.
            if (
                reviewed_sha == current_sha
                and row.get("proposed_full_cue") is None
                and str(row.get("suspect") or "").strip()
                and str(row.get("why") or "").strip()
            ):
                nomination = {
                    "cue": index, "kind": "context", "suspect": cue.text,
                    "proposed_full_cue": None,
                    "repair_class": row.get("repair_class") or "phonetic",
                    "why": "Prior current review doubt lacked a valid witness request: "
                           + str(row["why"]),
                    **({'continuation_plan':plan} if plan is not None else {}),
                }
                existing = pending.setdefault(index,nomination)
                if (plan is not None and not existing.get('continuation_plan')
                        and all(existing.get(k)==nomination.get(k) for k in ('cue','suspect','repair_class'))):
                    existing['continuation_plan'] = copy.deepcopy(plan)
            continue
        if (
            request.get("current_cue") != cue.text
            or request.get("matched_start_ms") != cue.start_ms
            or request.get("matched_end_ms") != cue.end_ms
        ):
            continue
        pending_row: dict[str, object] = {
            "cue": index, "kind": "context", "suspect": cue.text,
            "proposed_full_cue": None, "repair_class": row.get("repair_class") or "phonetic",
            "why": "Prior bound exact review remains unresolved for this current cue: "
                   + str(row.get("why") or "requires ordinary evidence and adjudication"),
            **({'continuation_plan':plan} if plan is not None else {}),
        }
        # Preserve a still-open CPA audio gate as evidence for the next normal
        # recovery pass.  The historical proposal and mutation receipt remain
        # untouched; only the typed BLOCK/needs_audio adjudication is carried.
        if isinstance(adjudication, Mapping):
            request_srt = request.get("whole_clip_current_srt")
            request_srt_sha = (
                "sha256:" + hashlib.sha256(request_srt.encode("utf-8")).hexdigest()
                if isinstance(request_srt, str)
                else None
            )
            request_payload = dict(request)
            stored_request_sha = str(
                request_payload.pop("request_sha256", "")
            ).removeprefix("sha256:")
            request_hash_valid = False
            if len(stored_request_sha) == 64:
                try:
                    request_hash_valid = stored_request_sha == hashlib.sha256(
                        json.dumps(
                            request_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                except (TypeError, ValueError):
                    request_hash_valid = False
            mutation_authority = adjudication.get("mutation_authority")
            history_recovery = history_audio_recovery_from_adjudication(
                adjudication,
                request=request,
            )
            valid_typed_gate = bool(
                isinstance(history_recovery, Mapping)
                and history_recovery.get("status") == "BLOCK"
                and adjudication.get("repaired") is False
                and not (
                    isinstance(mutation_authority, Mapping)
                    and mutation_authority.get("status") == "PASS"
                )
                and request.get("schema_version")
                == "subtitle-span-acoustic-check-request.v1"
                and request.get("base_text_sha256")
                == hashlib.sha256(cue.text.encode("utf-8")).hexdigest()
                and request.get("cue_indexes") == [index]
                and type(request.get("source_media_timeline_offset_ms")) is int
                and request.get("source_media_timeline_offset_ms") >= 0
                and request_srt_sha == reviewed_sha
                and request_hash_valid
            )
            if valid_typed_gate:
                pending_row["base_text_sha256"] = hashlib.sha256(
                    cue.text.encode("utf-8")
                ).hexdigest()
                pending_row["exact_release_adjudication"] = copy.deepcopy(
                    adjudication
                )
                if successor_validated:
                    pending_row["_history_audio_srt_successor"] = {
                        "schema_version": (
                            "exact-final-cpa-current-srt-successor-binding.v1"
                        ),
                        "status": "PASS",
                        "reviewed_srt_sha256": reviewed_sha,
                        "current_srt_sha256": current_sha,
                    }
        # Multiple discoveries can nominate the same unresolved cue. An
        # untyped/no-new-evidence duplicate must not erase a validated audio
        # obligation. This retains collection evidence only, never an edit.
        existing = pending.get(index, {})
        if (not pending_row.get('continuation_plan') and existing.get('continuation_plan')
                and all(existing.get(k) == pending_row.get(k) for k in ('cue','suspect','repair_class'))):
            pending_row['continuation_plan'] = copy.deepcopy(existing['continuation_plan'])
        if (
            not isinstance(existing.get("exact_release_adjudication"), Mapping)
            or isinstance(pending_row.get("exact_release_adjudication"), Mapping)
        ):
            pending[index] = pending_row
    return list(pending.values())


def _bound_source_evidence_context(
    srt_text: str,
    clip_context: Mapping[str, object],
    timeline_offset_ms: int,
    *,
    range_audio: Sequence[Mapping[str, Any]],
    earlier_screen: Sequence[Mapping[str, Any]],
    range_screen: Sequence[Mapping[str, Any]],
) -> tuple[str, dict[str, object]]:
    """Expose checked source context to discovery without creating findings.

    Reuse the ordinary adjudication request validators.  A neutral CURRENT
    scaffold only supplies cue geometry; it is never passed to the reviewer
    as a proposed edit or a finding.  Raw provider answers remain fallible.
    """
    if not (range_audio or earlier_screen or range_screen):
        return "", {}
    from src.autoslice.native_acoustic_feature_context import TONE_CONTEXT_SCHEMA

    indexes = {
        row.get("cue_index") for row in (*earlier_screen, *range_screen)
        if isinstance(row, Mapping) and type(row.get("cue_index")) is int
    }
    for observation in range_audio:
        if not isinstance(observation, Mapping):
            continue
        if observation.get("schema_version") == TONE_CONTEXT_SCHEMA:
            # Only select a scaffold here. The normal packet validator below
            # must still check every artifact, current cue and media origin.
            tone_binding = observation.get("source_binding") or {}
            if isinstance(tone_binding, Mapping) and type(tone_binding.get("cue_index")) is int:
                indexes.add(tone_binding["cue_index"])
            continue
        bindings = observation.get("artifact_bindings")
        if not isinstance(bindings, Mapping):
            continue
        # Read only a hash-bound input to select the range's cue geometry. The
        # full source/SRT/provider geometry is then replayed below.
        try:
            input_role = "segmented_input" if "segmented_input_path" in bindings else "input"
            data = Path(str(bindings[f"{input_role}_path"])).read_bytes()
            if hashlib.sha256(data).hexdigest() != str(
                bindings[f"{input_role}_sha256"]
            ).removeprefix("sha256:"):
                continue
            input_doc = json.loads(data)
            window = input_doc.get("delivery_window") or {}
            cue_indexes = (input_doc.get("cue_indexes") if input_role == "segmented_input"
                           else window.get("cue_indexes")) or []
            # The first/last cue may be only partly covered by the crop.
            # Replay all declared anchors; the normal validator still rejects
            # any cue outside the actual witness range.
            indexes.update(index for index in cue_indexes if type(index) is int)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    audio_rows, screen_rows = {}, {}
    for index in sorted(indexes):
        if not 1 <= index <= len(cues):
            continue
        try:
            request = build_context_adjudication_request(
                srt_text,
                {
                    "cue_index": index,
                    "suspect": cues[index - 1].text,
                    "suggestion": cues[index - 1].text,
                    "span_start_codepoint": 0,
                    "span_end_codepoint": len(cues[index - 1].text),
                    "proposed_full_cue": cues[index - 1].text,
                    "candidate_blind_observation_scaffolding": True,
                    "_prior_range_acoustic_observations": range_audio,
                    "_prior_screen_context_observations": earlier_screen,
                    "_prior_range_screen_context_observations": range_screen,
                },
                clip_context=clip_context,
                source_media_timeline_offset_ms=timeline_offset_ms,
            )
        except (ValueError, TypeError, KeyError, OSError):
            continue
        for row in request.get("context_only_range_acoustic_observations") or []:
            verdict = row.get("verdict") or {}
            witness_sha = (row.get("witness_request_sha256") or row.get("original_tone_request_sha256")
                           or row.get("original_input_sha256"))
            key = (witness_sha, row.get("provider"), row.get("model"))
            audio_rows[key] = {
                "authority": "EVIDENCE_ONLY",
                "provider": row.get("provider"), "model": row.get("model"),
                "witness_request_sha256": witness_sha,
                "source_media_sha256": (row.get("media_source_binding") or {}).get("media_sha256"),
                "observed_cue_texts": row.get("cue_texts"),
                "observed_srt_sha256": (row.get("artifact_bindings") or {}).get("reviewed_srt_sha256")
                or (row.get("source_binding") or {}).get("current_srt_sha256"),
                "current_cue_texts": [
                    {"cue_index": item["cue_index"], "text": cues[item["cue_index"] - 1].text}
                    for item in row.get("cue_texts") or []
                    if type(item.get("cue_index")) is int and 1 <= item["cue_index"] <= len(cues)
                ],
                "exact_transcript": verdict.get("exact_transcript"),
                **({field: row[field] for field in ("observation_class", "candidate_blind", "independent_witness",
                                                   "assistance", "audio_partition") if field in row}),
                "observed_pinyin": verdict.get("observed_pinyin") or verdict.get("heard_pinyin"),
                "transcript_scope": row.get("transcript_scope"),
            }
            for field in ("audio_observations", "audio_observation_policy", "audio_observation_time_domain"):
                if field in verdict:
                    audio_rows[key][field] = verdict[field]
            native = verdict.get("native_observation")
            if isinstance(native, Mapping) and isinstance(native.get("native_segments"), list):
                audio_rows[key].update({
                    "native_segments": native["native_segments"],
                    "native_segment_time_domain": "audio_crop_local_ms",
                    "native_audio_crop_source_ms": [verdict.get("audio_start_ms"), verdict.get("audio_end_ms")],
                    "source_to_delivery_offset_ms": timeline_offset_ms,
                    "speaker_id_scope": "PROVIDER_LOCAL_ANONYMOUS_VOICE; no human identity or rendering authority",
                })
            if "native_word_overlap_evidence" in row:
                audio_rows[key]["native_word_overlap_evidence"] = row["native_word_overlap_evidence"]
            if row.get("tone_feature_only") is True:
                audio_rows[key].update({
                    "evidence_scope": row.get("evidence_scope"),
                    "tone_feature_window": row.get("tone_feature_window"),
                    "tone_sequence": verdict.get("tone_sequence"),
                    "pitch_contours": verdict.get("pitch_contours"),
                    "syllables": verdict.get("syllables"),
                    "evidence_basis": verdict.get("evidence_basis"),
                    "uncertainty": verdict.get("uncertainty"),
                })
        for row in request.get("context_only_screen_observations") or []:
            for frame in row.get("observations") or []:
                receipt = frame.get("receipt") or {}
                key = (index, frame.get("frame_sha256"), frame.get("receipt_result_sha256"))
                screen_rows[key] = {
                    "authority": "EVIDENCE_ONLY", "cue_index": index,
                    "source_media_sha256": (row.get("related_source_media") or {}).get("sha256"),
                    "frame_ms": frame.get("related_source_frame_ms"),
                    "frame_sha256": frame.get("frame_sha256"),
                    "provider": receipt.get("provider"), "model": receipt.get("model"),
                    "texts": frame.get("texts"),
                    "scope": row.get("scope"),
                }
    rows = {"range_audio": list(audio_rows.values()), "screen": list(screen_rows.values())}
    serialized = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    audit = {
        "status": "VALIDATED_CONTEXT_ONLY" if any(rows.values()) else "NO_VALIDATED_CONTEXT",
        "current_srt_sha256": "sha256:" + hashlib.sha256(srt_text.encode()).hexdigest(),
        "range_count": len(audio_rows), "screen_frame_count": len(screen_rows),
        "context_sha256": "sha256:" + hashlib.sha256(serialized.encode()).hexdigest(),
        "mutation_authorized": False, "provider_calls": 0,
    }
    if not any(rows.values()):
        return "", audit
    return (
        "\n\n已验证与当前字幕、源时钟绑定的原始画面、范围听写及局部声学证据：\n"
        "这些是可能出错的来源观察，不能直接授权改字。画面中的游戏文字不是全部主播口述；"
        "整块听写也不是单 cue 的逐字真相。比较角色称呼、自我纠正、游戏叙述和主播插话，"
        "发现实际疑点后仍由正常逐项裁决处理，保留有意义的重复。\n" + serialized,
        audit,
    )


def _prior_pronoun_discovery_audit(authority: Mapping[str, object] | None) -> object:
    """Select the existing typed pronoun discovery receipt for replay.

    The exact gate stores each completed review as ``final_review_audit``;
    the discovery receipt is nested there.  A top-level receipt keeps its
    original priority, and an explicit but invalid one is not bypassed by
    the nested copy.  The pronoun consumer still revalidates current SRT,
    policy, context, prompt, decisions and findings; this grants no
    mutation or release authority.
    """
    authority = authority or {}
    if "candidate_pronoun_consistency_audit" in authority:
        return authority.get("candidate_pronoun_consistency_audit")
    previous_review = authority.get("final_review_audit")
    if isinstance(previous_review, Mapping):
        return previous_review.get("candidate_pronoun_consistency_audit")
    return None


def _run_exact_final_release_review(
    *,
    srt_text: str,
    correction_audit: Mapping[str, object],
    adapters: TextPipelineAdapters,
    authoritative_chat: Sequence[ChatEvidence],
    selection_hook: str,
    clip_context: Mapping[str, object],
    verify_confusable_entity: Callable | None = None,
    verified_authority_audit: Mapping[str, object] | None = None,
    timeline_offset_ms: int = 0,
    screen_read_probe: Callable[[int, int], Mapping[str, object]] | None = None,
    priority_raw_findings: Sequence[Mapping[str, Any]] = (),
    draft_fidelity_contexts: Sequence[Mapping[str, Any]] = (),
    acoustic_discovery_audit: Mapping[str, object] | None = None,
    final_review_llm: Callable[[str], str] | None = None, pronoun_audit_llm: Callable[[str], str] | None = None,
    prior_secondary_audio_observations: Sequence[Mapping[str, Any]] | None = None,
    prior_range_acoustic_observations: Sequence[Mapping[str, Any]] | None = None,
    prior_screen_context_observations: Sequence[Mapping[str, Any]] | None = None,
    prior_range_screen_context_observations: Sequence[Mapping[str, Any]] | None = None,
    prior_exact_source_transcript_handoffs: Sequence[Mapping[str, Any]] | None = None,
    source_media_sha256: str | None = None,
) -> dict[str, object]:
    """Review the exact post-authority bytes and issue a fail-closed receipt."""
    reviewed_srt_sha256 = "sha256:" + hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    boundary_semantic_review = correction_audit.get("boundary_semantic_review")
    correction_mutation_audit = audit_correction_mutation_authority(correction_audit)
    base: dict[str, object] = {
        "schema_version": FINAL_REVIEW_SCHEMA_VERSION,
        "meaning_policy": MEANING_POLICY_VERSION,
        "reviewed_srt_sha256": reviewed_srt_sha256,
        "boundary_semantic_review": (
            dict(boundary_semantic_review)
            if isinstance(boundary_semantic_review, Mapping)
            else {
                "schema_version": "talk-boundary-semantic-review.v1",
                "status": "BLOCK",
                "reason_codes": ["BOUNDARY_SEMANTIC_REVIEW_MISSING"],
            }
        ),
        "correction_pass": dict(correction_audit),
        "correction_mutation_authority": correction_mutation_audit,
    }
    boundary_receipt_replay = correction_audit.get("boundary_receipt_replay")
    if isinstance(boundary_receipt_replay, Mapping):
        base["boundary_receipt_replay"] = dict(boundary_receipt_replay)
    if isinstance(acoustic_discovery_audit, Mapping):
        base["candidate_blind_acoustic_discovery"] = dict(acoustic_discovery_audit)
    if prior_secondary_audio_observations is None:
        prior_secondary_audio_observations = _persisted_secondary_audio_observations(
            verified_authority_audit
        )
    else:
        prior_secondary_audio_observations = tuple(
            row for row in prior_secondary_audio_observations if isinstance(row, Mapping)
        )
    if prior_secondary_audio_observations:
        base["secondary_audio_handoff"] = {
            "schema_version": "secondary-audio-observation-handoff.v1",
            "status": "LOADED_CONTEXT_ONLY",
            "observation_count": len(prior_secondary_audio_observations),
            "mutation_authorized": False,
        }
    prior_transcript_handoffs = _persisted_exact_source_transcript_handoffs({
        "prior_exact_source_transcript_handoffs": [
            *_persisted_exact_source_transcript_handoffs(verified_authority_audit),
            *(prior_exact_source_transcript_handoffs or []),
        ],
    })
    if prior_transcript_handoffs:
        base["prior_exact_source_transcript_handoffs"] = list(prior_transcript_handoffs)
    if os.environ.get("AUTOSLICE_DISABLE_FINAL_REVIEW") == "1":
        return {
            **base,
            "status": "SKIPPED_TEST_ONLY",
            "release_gate": "BLOCK",
            "reason_codes": ["FINAL_REVIEW_DISABLED"],
            "discovery": {"status": "SKIPPED_TEST_ONLY"},
            "findings": [],
            "validated_finding_count": 0,
        }
    try:
        review_glossary = adapters.review_glossary()
        candidate_context_text = clip_context_prompt_text(clip_context)
        human_sequence = (verified_authority_audit or {}).get('human_sequence_alignment') or {}
        if human_sequence.get('records'):
            candidate_context_text += '\n已确认的原话与话语角色；字面确认不等于事实、语境或伤害风险已通过：\n' + json.dumps(
                human_sequence['records'],ensure_ascii=False)
        from src.autoslice.editorial_subtitle import rendering_context
        editorial_text, editorial_cues = rendering_context(srt_text, verified_authority_audit, timeline_offset_ms)
        candidate_context_text += editorial_text
        from src.autoslice.semantic_retry_progress import retry_context
        candidate_context_text += retry_context(srt_text, clip_context, timeline_offset_ms)
        role_context = caption_audio_roles_context(
            srt_text, (verified_authority_audit or {}).get("caption_audio_roles")
        )
        if role_context:
            candidate_context_text += "\n\n" + role_context
            base["caption_audio_role_context_sha256"] = "sha256:" + hashlib.sha256(role_context.encode()).hexdigest()
        source_context, source_context_audit = _bound_source_evidence_context(
            srt_text, clip_context, timeline_offset_ms,
            range_audio=prior_range_acoustic_observations or (),
            earlier_screen=prior_screen_context_observations or (),
            range_screen=prior_range_screen_context_observations or (),
        )
        candidate_context_text += source_context
        if source_context_audit:
            base["source_context_discovery_audit"] = source_context_audit
        # Optional identity context is useful only for this physical source and clock.
        # A missing/stale document stays absent evidence, not a new release blocker.
        speaker_document = (verified_authority_audit or {}).get("speaker_attribution")
        speaker_profile_hash = None
        if speaker_document is not None and source_media_sha256:
            try:
                speaker_profile_hash = _evidence_file_sha256(adapters.profile_asset_file("voiceprint_profile"))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                speaker_document = None
        else:
            speaker_document = None
        pronoun_findings, pronoun_audit = discover_candidate_pronoun_findings(
            srt_text,
            policy_text=review_glossary,
            candidate_context_text=candidate_context_text,
            llm_call=pronoun_audit_llm or _build_pronoun_audit_llm_call(),
            extract_json=extract_json_object,
            prior_audit=_prior_pronoun_discovery_audit(verified_authority_audit),
            speaker_attribution=speaker_document,
            expected_source_media_sha256=source_media_sha256,
            expected_profile_sha256=speaker_profile_hash,
            expected_timeline_offset_ms=timeline_offset_ms,
        )
        base["candidate_pronoun_consistency_audit"] = pronoun_audit
        legacy_audio_findings = _pending_legacy_acoustic_convergence_findings(
            srt_text, verified_authority_audit,
        )
        pending_current_findings = _pending_current_exact_findings(
            srt_text, verified_authority_audit,
        )
        retained_audio = _retained_pending_audio_findings(
            srt_text, verified_authority_audit, clip_context=clip_context,
            timeline_offset_ms=timeline_offset_ms,
        )
        current_by_cue = {row["cue"]: row for row in pending_current_findings}
        for row in retained_audio:
            existing = current_by_cue.get(row["cue"])
            if not isinstance((existing or {}).get("exact_release_adjudication"), Mapping):
                current_by_cue[row["cue"]] = row
        pending_current_findings = list(current_by_cue.values())
        from src.autoslice.audio_language_discovery import current_language_discovery_context
        language_context, language_findings = current_language_discovery_context(
            srt_text, verified_authority_audit, source_media_sha256=source_media_sha256,
            timeline_offset_ms=timeline_offset_ms,
        )
        candidate_context_text += language_context
        priority_findings = [*pronoun_findings, *priority_raw_findings,
                            *legacy_audio_findings, *pending_current_findings, *language_findings]
        from src.autoslice.span_finding_obligations import pending_findings as pending_span_findings
        priority_findings.extend(pending_span_findings(srt_text, verified_authority_audit or {}))
        from src.autoslice.retained_reviewed_draft import retained_entity_handoff_findings
        priority_findings.extend(retained_entity_handoff_findings(srt_text, verified_authority_audit or {}, timeline_offset_ms=timeline_offset_ms))
        if pending_current_findings:
            base["pending_current_exact_review"] = {
                "cue_indexes": [row["cue"] for row in pending_current_findings],
                "mutation_authorized": False,
            }
        if legacy_audio_findings:
            base["pending_legacy_acoustic_review"] = {
                "cue_indexes": [row["cue"] for row in legacy_audio_findings],
                "mutation_authorized": False,
            }
        with producer_stage("exact_final_review.discovery"):
            findings = audit_final_subtitles(
                srt_text,
                llm_call=final_review_llm or _build_final_review_llm_call(),
                extract_json=extract_json_object,
                glossary_text=review_glossary,
                structured_context_text=_final_review_structured_context(
                    selection_hook=selection_hook,
                    authoritative_chat=authoritative_chat,
                ),
                candidate_context_text=candidate_context_text,
                candidate_context=clip_context,
                editorial_rendered_cues=editorial_cues,
                source_media_timeline_offset_ms=timeline_offset_ms,
                extra_raw_findings=priority_findings,
                prioritize_extra_raw_findings=bool(priority_findings),
                draft_fidelity_contexts=draft_fidelity_contexts,
            )
    except CandidatePronounAuditError as exc:
        contract_repair = getattr(exc, "contract_repair", None)
        if isinstance(contract_repair, Mapping):
            base["candidate_pronoun_contract_repair"] = dict(contract_repair)
        return {
            **base,
            "status": "AUDITOR_UNAVAILABLE",
            "release_gate": "BLOCK",
            "reason_codes": [exc.reason_code],
            "discovery": _pf.auditor_unavailable_discovery(
                exc.detail, _pf.provider_failure_detail_from_cause(exc),
                provider_diagnostics=getattr(exc.__cause__, "provider_diagnostics", None),
            ),
            "findings": [],
            "validated_finding_count": 0,
        }
    except FinalReviewAuditError as exc:
        return {
            **base,
            "status": "AUDITOR_UNAVAILABLE",
            "release_gate": "BLOCK",
            "reason_codes": [exc.reason_code],
            "discovery": _pf.auditor_unavailable_discovery(
                exc.detail, _pf.provider_failure_detail_from_cause(exc),
                provider_diagnostics=getattr(exc.__cause__, "provider_diagnostics", None),
            ),
            "findings": [],
            "validated_finding_count": 0,
        }
    except Exception as exc:
        from src.autoslice.review_errors import (
            ContractRejection, ProviderFailure, record_program_error, review_error_record,
        )
        if isinstance(exc, (ContractRejection, ProviderFailure)):
            return {
                **base, "status": "AUDITOR_UNAVAILABLE", "release_gate": "BLOCK",
                "reason_codes": [exc.reason_code],
                "discovery": {"status": "AUDITOR_UNAVAILABLE",
                              "review_error": review_error_record(exc)},
                "findings": [], "validated_finding_count": 0,
            }
        record_program_error(exc, boundary="exact_final_review.discovery")
        raise
    authority_pending, authority_resolved = resolve_verified_source_truth_findings(
        srt_text,
        findings,
        source_truth_audit=(
            verified_authority_audit.get("source_subtitle_truth_audit")
            if isinstance(verified_authority_audit, Mapping)
            else None
        ),
        timeline_offset_ms=timeline_offset_ms,
    )
    from src.autoslice.human_sequence_alignment import resolve_literal_findings
    authority_pending,human_resolved = resolve_literal_findings(
        (verified_authority_audit or {}).get('human_sequence_alignment') or {},srt_text,authority_pending,
        timeline_offset_ms=timeline_offset_ms)
    authority_resolved.extend(human_resolved)
    # Keep the exact-round pronoun audit available to the downstream context
    # judge as evidence.  The scoped consumer revalidates the full audit and
    # current SRT before exposing one cue; this internal carryover is not an
    # authority receipt and never mutates subtitle bytes.
    if (
        isinstance(pronoun_audit, Mapping)
        and pronoun_audit.get("status") == "PASS"
        and type(pronoun_audit.get("occurrence_count")) is int
        and pronoun_audit.get("occurrence_count") > 0
    ):
        for finding in authority_pending:
            if isinstance(finding, dict):
                finding["_candidate_pronoun_policy_audit"] = dict(pronoun_audit)
    from src.autoslice.semantic_request_history import HISTORY_KEY, sources_from_audit
    history_sources = sources_from_audit(verified_authority_audit)
    if history_sources:
        authority_pending = [{**row, HISTORY_KEY: history_sources} for row in authority_pending]
    from src.autoslice.semantic_request_history import with_partial_usage_guard
    exact_judge_llm_call = with_partial_usage_guard(
        final_review_llm or _build_final_review_llm_call(), verified_authority_audit,
        usage_dependency="SOFT_PRESSURE", source_media_sha256=source_media_sha256,
        candidate_id=(clip_context or {}).get("candidate_id"))
    prior_usage = (
        verified_authority_audit.get("cpa_adjudication_usage")
        if isinstance(verified_authority_audit, Mapping)
        else None
    )
    prior_count = (
        prior_usage.get("provider_adjudication_count")
        if isinstance(prior_usage, Mapping)
        else correction_audit.get("context_adjudication_count", 0)
    )
    if isinstance(prior_count, bool) or not isinstance(prior_count, int) or prior_count < 0:
        raise ValueError("CPA_ADJUDICATION_USAGE_INVALID")
    exact_adjudication_kwargs: dict[str, Any] = {
        "entity_verifier": verify_confusable_entity,
        "clip_context": clip_context,
        "source_media_timeline_offset_ms": timeline_offset_ms,
        "judge_llm_call": exact_judge_llm_call,
        "screen_read_probe": screen_read_probe,
        "initial_provider_adjudication_count": prior_count,
    }
    roles = (verified_authority_audit or {}).get("caption_audio_roles")
    if caption_audio_roles_context(srt_text, roles):
        exact_adjudication_kwargs["caption_audio_roles"] = roles
    if prior_secondary_audio_observations:
        exact_adjudication_kwargs[
            "prior_secondary_audio_observations"
        ] = prior_secondary_audio_observations
    from src.autoslice.retained_partial_evidence import partial_ranges_from_audit
    prior_range_acoustic_observations = tuple(prior_range_acoustic_observations or ()) + partial_ranges_from_audit(verified_authority_audit)
    if prior_range_acoustic_observations:
        # These retain their original file/request bindings. The ordinary
        # context builder revalidates them against each actual current finding.
        exact_adjudication_kwargs["prior_range_acoustic_observations"] = (
            prior_range_acoustic_observations
        )
    if prior_screen_context_observations:
        exact_adjudication_kwargs["prior_screen_context_observations"] = prior_screen_context_observations
    if prior_range_screen_context_observations:
        exact_adjudication_kwargs["prior_range_screen_context_observations"] = prior_range_screen_context_observations
    if prior_transcript_handoffs:
        exact_adjudication_kwargs["prior_exact_source_transcript_handoffs"] = prior_transcript_handoffs
    from src.autoslice.span_finding_obligations import merge_pending as merge_span_pending
    authority_pending = merge_span_pending(srt_text, verified_authority_audit or {}, list(authority_pending))
    with producer_stage("exact_final_review.adjudication", findings=len(authority_pending)):
        acoustic_pending, acoustic_resolved = adjudicate_exact_release_findings(
            srt_text,
            authority_pending,
            **exact_adjudication_kwargs,
        )
    cumulative_count = max(
        [prior_count]
        + [
            int(pressure["provider_adjudication_count"])
            for finding in [*acoustic_pending, *acoustic_resolved]
            if isinstance(
                pressure := (finding.get("exact_release_adjudication") or {}).get(
                    "cpa_resource_pressure"
                ), Mapping
            )
        ]
    )
    base["cpa_adjudication_usage"] = {
        "provider_adjudication_count": cumulative_count,
        "prior_provider_adjudication_count": prior_count,
        "soft_limit": MAX_CONTEXT_ADJUDICATIONS,
        "policy": "SOFT",
    }
    from src.autoslice.semantic_request_history import preserve_partial_usage
    base["cpa_adjudication_usage"] = preserve_partial_usage(
        base["cpa_adjudication_usage"], verified_authority_audit)
    unresolved_findings, memo_resolved = resolve_findings_from_exact_final_convergence_memos(
        srt_text,
        acoustic_pending,
        authority_audit=verified_authority_audit or {},
    )
    unresolved_findings, convergence_resolved = converge_reconsidered_exact_final_findings(
        srt_text,
        unresolved_findings,
        authority_audit=verified_authority_audit or {},
        judge_llm_call=exact_judge_llm_call,
    )
    # A boundary PASS proves closure on the current subtitle surface; it does
    # not make a misspelled terminal name immutable. CPA-authorized repairs
    # stay pending for the finalizer to apply. Its next exact review calls
    # exact_delivery_correction_audit on the new bytes, rechecking closure
    # before release instead of silently resolving the word-choice finding.
    resolved_findings = [
        *authority_resolved,
        *memo_resolved,
        *acoustic_resolved,
        *convergence_resolved,
    ]





    disclosed_keep_current: list = []
    blocking_findings: list = []
    for finding in unresolved_findings:
        bucket = disclosed_keep_current if is_release_resolved_keep(finding) else blocking_findings
        bucket.append(finding)
    unresolved_findings = blocking_findings
    boundary_passed = (
        isinstance(base["boundary_semantic_review"], Mapping)
        and base["boundary_semantic_review"].get("status") == "PASS"
    )
    reason_codes = exact_source_transcript_infra_reason_codes(unresolved_findings, srt_text=srt_text, clip_context=clip_context)
    if unresolved_findings:
        from .finding_lifecycle import release_reason_codes
        reason_codes.extend(release_reason_codes(unresolved_findings))
        cycle_reason_codes = sorted(
            {
                str(receipt.get("reason_code"))
                for finding in unresolved_findings
                for receipt in [
                    (
                        finding.get("exact_final_cpa_cycle_adjudication")
                        if isinstance(finding, Mapping)
                        else None
                    )
                ]
                if isinstance(receipt, Mapping)
                and receipt.get("status") == "BLOCK"
                and receipt.get("reason_code")
                in {
                    "CPA_CYCLE_ADJUDICATION_UNAVAILABLE",
                    "CPA_CYCLE_ADJUDICATION_INVALID",
                }
            }
        )
        reason_codes.extend(cycle_reason_codes)
    if correction_mutation_audit.get("status") != "PASS":
        reason_codes.append("FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID")
    if not boundary_passed:
        reason_codes.append("FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED")
    passed = not reason_codes
    from .finding_lifecycle import review_outcome
    return review_outcome({
        **base,
        "status": "CLEAN" if passed else "FLAGGED",
        "release_gate": "PASS" if passed else "BLOCK",
        "reason_codes": reason_codes,
        "discovery": {
            "status": "COMPLETE",
            "explicit_empty_findings": not unresolved_findings,
            "raw_validated_finding_count": len(findings),
            "resolved_finding_count": len(resolved_findings),
        },
        "findings": unresolved_findings,
        "resolved_findings": resolved_findings,
        "unresolved_findings_disclosed": disclosed_keep_current,
        "validated_finding_count": len(unresolved_findings),
    })


_run_exact_final_release_review = _stage_logged(
    "exact_final_review", _run_exact_final_release_review)


def _reusable_completed_current_review(completed: object, *, srt_text: str,
        source_review: Mapping, timeline_offset_ms: int, source_final_end_ms: int,
        postburn: object = None) -> dict | None:
    """Replay a completed review after the caller has validated its full input."""
    if not isinstance(completed, Mapping):
        return None
    boundary = completed.get('boundary_semantic_review') or {}
    source = boundary.get('source_separation_witness') or {}
    projection = completed.get('current_delivery_source_projection', source)
    if not isinstance(projection, Mapping):
        return None
    digest = 'sha256:' + hashlib.sha256(srt_text.encode('utf-8')).hexdigest()
    if (completed.get('status') != 'CLEAN' or completed.get('release_gate') != 'PASS'
            or completed.get('reviewed_srt_sha256') != digest
            or boundary.get('review_scope') != 'final_delivery'
            or source_review.get('status') != 'PASS' or source_review.get('review_scope') != 'source_full_window'
            or source.get('source_request_sha256') != source_review.get('request_sha256')
            or source.get('source_cue_grid_sha256') != source_review.get('cue_grid_sha256')
            or projection.get('source_final_start_ms') != timeline_offset_ms
            or projection.get('source_final_end_ms') != source_final_end_ms
            or (postburn is not None and not isinstance(postburn, Mapping))
            or (isinstance(postburn, Mapping)
                and (postburn.get('status') != 'CLEAN' or postburn.get('release_gate') != 'PASS'))):
        return None
    validate_final_review_release(completed, expected_srt_sha256=digest)
    return copy.deepcopy(dict(completed))


def _prepared_boundary_review_binding(*, clock: Mapping[str, object], candidate_id: str,
                                      completed_record: Mapping[str, object],
                                      clip_context: Mapping[str, object]) -> dict[str, object]:
    """Producer identity omitted from the rendered prepared boundary prompt.

    A validated independent byte copy carries the original clock receipt;
    the consumer pathname of a copy is not new source evidence.
    """
    source_clock = clock
    while (materialization := source_clock["source_binding"].get("materialization")) is not None:
        source_clock = materialization["source_clock"]

    def value_sha256(value: object) -> str:
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    return {
        "consumer": "prepared-exact-final-boundary", "candidate_id": candidate_id,
        "source_clock": copy.deepcopy(dict(source_clock)),
        "completed_record_sha256": value_sha256(completed_record),
        "clip_context_sha256": value_sha256(clip_context),
    }


def _bind_prepared_boundary_observation(llm_call, *, prepared_review_binding: Mapping[str, object],
                                        source_final_start_ms: int, source_final_end_ms: int,
                                        current_owner_contract: object, out_root: Path) -> None:
    """Route the prepared exact boundary review through the existing
    ``CompletedReviewCheckpoint`` consumer of ``review_talk_boundary_semantics``.

    The checkpoint itself also binds the full request, cue grid, provider
    routing/runtime/transport and parser.  It installs only parser-complete
    responses; provider failures and UNKNOWN calls are never admitted, and a
    replayed negative observation is not a quality PASS or text authority.
    """
    llm_call.completed_review_binding = {
        "prepared": copy.deepcopy(dict(prepared_review_binding)),
        "source_final_start_ms": source_final_start_ms,
        "source_final_end_ms": source_final_end_ms,
        "current_owner_contract": copy.deepcopy(current_owner_contract),
    }
    llm_call.completed_review_root = (
        Path(out_root).expanduser().absolute() / ".completed-review-observations"
    )


def _build_exact_final_review_callback(
    *,
    spec: Mapping[str, object],
    out_root: Path,
    cid: str,
    padded: Path,
    adapters: TextPipelineAdapters,
    authoritative_chat: Sequence[ChatEvidence],
    clip_context: Mapping[str, object],
    final_review_audit: Mapping[str, object],
    verify_confusable_entity: Callable | None,
    truth_ownership: Mapping[str, object] | None,
    screen_read_probe: Callable[[int, int], Mapping[str, object]] | None,
    frozen_boundary_receipt: object | None = None,
    continuation_correction: bool = False,
    text_recovery_only: bool = False,
    source_boundary_review: Mapping[str, object] | None = None,
    source_boundary_timeline_offset_ms: int = 0,
    completed_final_review: Mapping[str, object] | None = None,
    prepared_review_binding: Mapping[str, object] | None = None,
) -> Callable[[str, Mapping[str, object], int, int], dict[str, object]]:
    """Build the normal exact-final reviewer from current stage evidence.

    The ordinary producer used to create this callback as a closure inside
    ``run_text_pipeline``.  Keeping the construction here makes the same
    reviewer recoverable for a private package continuation: the caller must
    provide the hash-bound current audit, source review, SRT/context and
    runtime adapters.  This factory only creates the callback; it does not
    issue a release verdict or promote a historical audit.
    """

    if source_boundary_review is not None and (
        source_boundary_review.get("review_scope") != "source_full_window"
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_REVIEW_SCOPE_INVALID")
    if (
        isinstance(source_boundary_timeline_offset_ms, bool)
        or not isinstance(source_boundary_timeline_offset_ms, int)
        or source_boundary_timeline_offset_ms < 0
        or (source_boundary_timeline_offset_ms and source_boundary_review is None)
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_TIMELINE_OFFSET_INVALID")

    if (
        not continuation_correction
        and isinstance(final_review_audit.get("correction_pass"), Mapping)
        and "applied_count" not in final_review_audit
    ):
        # Reject a completed review envelope before acoustic discovery or
        # provider dispatch. Its raw correction pass is not the envelope;
        # the continuation caller must select the existing recovery mode.
        raise CurrentTextContinuationError(
            "CURRENT_COMPLETED_CORRECTION_ENVELOPE_REQUIRES_CONTINUATION"
        )

    generation_options = spec.get("speaker_attribution_generation")
    generate_speakers = (isinstance(generation_options, Mapping)
                        and generation_options.get("enabled") is True)
    consumed_postburn_request_shas: set[str] = set()

    def review_exact_final_srt(
        final_srt_text: str,
        verified_authority_audit: Mapping[str, object],
        timeline_offset_ms: int,
        source_final_end_ms: int,
    ) -> dict[str, object]:
        context_infra = final_review_audit.get("source_context_infra_scope")
        if isinstance(context_infra, Mapping) and any(
            row["start_ms"] < source_final_end_ms and row["end_ms"] > timeline_offset_ms
            for row in context_infra.get("deferred", [])
        ):
            raise CurrentTextContinuationError("FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED")
        if continuation_correction and not text_recovery_only and not generate_speakers:
            completed = _reusable_completed_current_review(completed_final_review,
                srt_text=final_srt_text, source_review=final_review_audit.get('boundary_semantic_review') or {},
                timeline_offset_ms=timeline_offset_ms, source_final_end_ms=source_final_end_ms,
                postburn=verified_authority_audit.get('postburn_subtitle_audio_review'))
            if completed is not None:
                from src.autoslice.cpa_runtime import record_cpa_event
                record_cpa_event('cache_hit',{'kind':'current_final_review','stage':'exact_final_review'})
                return completed
        # Unrelated normal typed repairs change the full SRT hash. Retain
        # voice context only after replaying that suffix and rereading the
        # original immutable receipts; never relabel those old receipts.
        acoustic_audit = verified_authority_audit.get("nested_media_acoustic_observation")
        if (
            isinstance(acoustic_audit, Mapping)
            and not caption_audio_roles_context(
                final_srt_text, verified_authority_audit.get("caption_audio_roles")
            )
        ):
            from src.autoslice.producer_nested_acoustic import caption_audio_roles_after_typed_text

            roles = caption_audio_roles_after_typed_text(
                candidate_id=cid, source_media=padded, srt_text=final_srt_text,
                acoustic_audit=acoustic_audit, text_authority=verified_authority_audit,
            )
            if isinstance(verified_authority_audit, dict):
                verified_authority_audit["caption_audio_roles"] = roles
            else:
                verified_authority_audit = {**verified_authority_audit, "caption_audio_roles": roles}
        if continuation_correction:
            # A continuation receives a completed final-review envelope.  Its
            # correction pass is the raw historical input for the next exact
            # review; passing the outer envelope would lose applied_count and
            # fail the mutation-authority contract.
            persisted_correction = final_review_audit.get("correction_pass")
            if not isinstance(persisted_correction, Mapping):
                raise CurrentTextContinuationError("CURRENT_CORRECTION_PASS_MISSING")
            correction_audit: Mapping[str, object] = copy.deepcopy(
                dict(persisted_correction)
            )
            correction_audit["boundary_semantic_review"] = copy.deepcopy(
                final_review_audit["boundary_semantic_review"]
            )
        else:
            correction_audit = final_review_audit

        # A completed continuation stores a delivery review in its audit.
        # Keep that historical envelope intact and use the separately bound
        # source review for the fresh source-to-delivery closure check.
        if source_boundary_review is not None:
            correction_audit = dict(correction_audit)
            correction_audit["boundary_semantic_review"] = copy.deepcopy(
                dict(source_boundary_review)
            )

        microcue_findings, microcue_audit = discover_priority_findings(
            final_srt_text,
            timeline_offset_ms=timeline_offset_ms,
            entity_verifier=verify_confusable_entity,
            out_root=out_root,
            cid=cid,
            truth_full_ownership=truth_ownership,
        )

        def postburn_priority_findings() -> list[Mapping[str, Any]]:
            postburn_result = validated_postburn_priority_findings(
                final_srt_text=final_srt_text,
                verified_authority_audit=verified_authority_audit,
                candidate_id=cid,
                clip_context=clip_context,
            )
            if postburn_result is None:
                return []
            rows, request_sha = postburn_result
            if request_sha in consumed_postburn_request_shas:
                return []
            consumed_postburn_request_shas.add(request_sha)
            return rows

        # Preserve the historical order of each caller.  Continuation loads a
        # completed postburn block before exact delivery correction; the
        # initial run only consumes it after correction succeeds, so a failed
        # correction does not consume a request for a later handoff.
        postburn_findings: list[Mapping[str, Any]] = []
        if continuation_correction:
            postburn_findings = postburn_priority_findings()
        if text_recovery_only:
            # Text repair does not inherit the previous source PASS.  The
            # independent source reviewer runs after actual repaired bytes.
            correction = dict(correction_audit)
            correction["boundary_semantic_review"] = {
                "schema_version": "talk-boundary-semantic-review.v1",
                "status": "BLOCK",
                "reason_codes": ["CURRENT_TEXT_REQUIRES_SOURCE_REVIEW"],
            }
        else:
            boundary_llm = _build_final_review_llm_call()
            if prepared_review_binding is not None:
                _bind_prepared_boundary_observation(
                    boundary_llm, prepared_review_binding=prepared_review_binding,
                    source_final_start_ms=timeline_offset_ms + source_boundary_timeline_offset_ms,
                    source_final_end_ms=source_final_end_ms + source_boundary_timeline_offset_ms,
                    current_owner_contract=verified_authority_audit.get("frozen_boundary_owner_contract"),
                    out_root=out_root,
                )
            correction = exact_delivery_correction_audit(
                final_srt_text=final_srt_text,
                correction_audit=correction_audit,
                # An assembled source review and its prepared physical crop
                # may use different origins.  Translate both endpoints by
                # the same provenance-derived origin; acoustic requests and
                # subtitle timing below stay on the physical crop's clock.
                source_final_start_ms=timeline_offset_ms + source_boundary_timeline_offset_ms,
                source_final_end_ms=source_final_end_ms + source_boundary_timeline_offset_ms,
                candidate_id=cid,
                recording_date=str(spec.get("date") or ""),
                selection_hook=str(spec.get("selection_hook") or ""),
                selection_scorecard=spec.get("selection_scorecard"),
                structured_context=_final_review_structured_context(
                    selection_hook=str(spec.get("selection_hook") or ""),
                    authoritative_chat=authoritative_chat,
                ),
                candidate_context=clip_context_prompt_text(clip_context) + (
                    "\n\n"
                    + caption_audio_roles_context(
                        final_srt_text,
                        verified_authority_audit.get("caption_audio_roles"),
                    )
                    if caption_audio_roles_context(
                        final_srt_text,
                        verified_authority_audit.get("caption_audio_roles"),
                    )
                    else ""
                ),
                boundary_max_forward_ms=int(
                    spec.get("boundary_repair_extend_cap_ms", 30_000)
                ),
                llm_call=boundary_llm,
                extract_json=extract_json_object,
                disabled=os.environ.get("AUTOSLICE_DISABLE_FINAL_REVIEW") == "1",
                frozen_boundary_receipt=frozen_boundary_receipt,
            )
        if not continuation_correction:
            postburn_findings = postburn_priority_findings()
        from src.autoslice.retained_range_context import persisted_native_range_artifacts
        retained_ranges = persisted_native_range_artifacts(verified_authority_audit)
        speaker_generation = None
        speaker_media_sha = (spec.get("source_piece_clock") or {}).get("padded_sha256")
        if generate_speakers:
            from src.autoslice.producer_speaker_attribution import generate_for_producer
            from src.autoslice.speaker_attribution import attribute_speakers

            try:
                document, speaker_generation = generate_for_producer(
                    options=generation_options, media_path=padded, srt_text=final_srt_text,
                    profile_path=adapters.profile_asset_file("voiceprint_profile"),
                    timeline_offset_ms=timeline_offset_ms, source_end_ms=source_final_end_ms,
                    out_root=out_root, expected_media_sha256=speaker_media_sha)
            except Exception as exc:
                # Optional evidence I/O cannot become a new whole-production gate.
                document = attribute_speakers(final_srt_text, scorer=None)
                document["reason_codes"] = ["SPEAKER_GENERATION_IO_UNAVAILABLE"]
                speaker_generation = {"status":"UNKNOWN", "reason_code":type(exc).__name__}
            addition = {"speaker_attribution":document,
                        "speaker_attribution_generation":speaker_generation}
            if isinstance(verified_authority_audit, dict):
                verified_authority_audit.update(addition)
            else:
                verified_authority_audit = {**verified_authority_audit, **addition}
            speaker_media_sha = speaker_media_sha or speaker_generation.get("source_media_sha256")
        exact = _run_exact_final_release_review(
            screen_read_probe=screen_read_probe,
            srt_text=final_srt_text,
            correction_audit=correction,
            adapters=adapters,
            authoritative_chat=authoritative_chat,
            selection_hook=str(spec.get("selection_hook") or ""),
            clip_context=clip_context,
            verify_confusable_entity=verify_confusable_entity,
            verified_authority_audit=verified_authority_audit,
            timeline_offset_ms=timeline_offset_ms,
            source_media_sha256=speaker_media_sha,
            priority_raw_findings=[*microcue_findings, *postburn_findings],
            draft_fidelity_contexts=fidelity_kept_contexts(padded, final_srt_text),
            acoustic_discovery_audit=microcue_audit,
            prior_secondary_audio_observations=_persisted_secondary_audio_observations(
                verified_authority_audit
            ),
            prior_range_acoustic_observations=tuple(
                row for row in (spec.get("prior_range_acoustic_observations") or [])
                if isinstance(row, Mapping)
            ) + retained_ranges,
            prior_screen_context_observations=tuple(
                row for row in (spec.get("prior_screen_context_observations") or [])
                if isinstance(row, Mapping)
            ),
            prior_range_screen_context_observations=tuple(
                row for row in (spec.get("prior_range_screen_context_observations") or [])
                if isinstance(row, Mapping)
            ),
            prior_exact_source_transcript_handoffs=tuple(
                row for row in (spec.get("prior_exact_source_transcript_handoffs") or [])
                if isinstance(row, Mapping)
            ),
        )
        if speaker_generation is not None:
            exact["speaker_attribution_generation"] = speaker_generation
            exact["speaker_attribution"] = verified_authority_audit["speaker_attribution"]
        checkpoint_final_review_carryover(carryover_path(out_root, cid), exact)
        return exact

    return review_exact_final_srt


def build_prepared_exact_final_reviewer(*, spec, out_root, cid, padded, host,
                                        authority, completed_record, adapters, native_audio_state):
    """Lazily construct the ordinary reviewer after a new postburn finding.

    Prepared recovery has already validated source/media/text inputs. Do not
    enter source transcription or entity discovery just to inspect a completed
    burn; the normal exact reviewer is needed only if postburn finds new risk.
    """
    from src.autoslice.producer_media import ffprobe_duration_ms
    from src.autoslice.producer_recut_provenance import prepared_boundary_clock_binding
    clock = prepared_boundary_clock_binding(
        media_path=Path(padded), completed_record=completed_record, clip_context=spec["clip_context"], candidate_id=cid,
    )
    prepared_review_binding = _prepared_boundary_review_binding(
        clock=clock, candidate_id=cid, completed_record=completed_record,
        clip_context=spec["clip_context"],
    )
    callback = None
    entity = None

    def entity_context():
        nonlocal entity
        if entity is None:
            entity = _build_entity_verification_context(
                spec=spec, padded=padded, padded_dur=ffprobe_duration_ms(padded),
                host=host, text_override_path=None, cid=cid, out_root=out_root,
                srt_text=Path(completed_record["subtitle_path"]).read_text(),
                authoritative_chat=list(_continuation_chat_from_context(spec["clip_context"])),
                adapters=adapters, native_audio_state=native_audio_state,
            )
        return entity

    def review(text, verified_authority, start, end):
        nonlocal callback
        if callback is None:
            context = spec["clip_context"]
            chat = _continuation_chat_from_context(context)
            current_entity = entity_context()
            callback = _build_exact_final_review_callback(
                spec=spec, out_root=out_root, cid=cid, padded=padded, adapters=adapters,
                authoritative_chat=chat, clip_context=context,
                final_review_audit=authority["final_review_audit"],
                verify_confusable_entity=current_entity.verify_confusable_entity,
                truth_ownership=resolve_truth_full_ownership(spec) or resolve_operator_text_full_ownership(spec),
                screen_read_probe=(None if spec.get("comparison_ordinary_cache_only") is True
                                   else build_env_screen_read_probe(padded)),
                continuation_correction=True,
                source_boundary_review=completed_record["boundary_audit"]["boundary_semantic_review"],
                source_boundary_timeline_offset_ms=clock["source_boundary_timeline_offset_ms"],
                prepared_review_binding=prepared_review_binding,
            )
        audit = callback(text, verified_authority, start, end)
        audit["prepared_source_boundary_clock"] = clock
        return audit

    def observe_independent_witnesses(request):
        # The same lazy ordinary entity context used by the exact reviewer;
        # this evidence-only seam cannot produce a final text verdict.
        verifier = entity_context().verify_confusable_entity
        return verifier.observe_independent_witnesses(request)

    review.observe_independent_witnesses = observe_independent_witnesses
    review.source_boundary_clock = clock
    return review


def _defer_source_truth_failure_for_redelivery(
    *,
    spec: Mapping[str, object],
    source_truth_audit: dict[str, Any],
    chat_authority_audit: dict[str, Any],
    chat_authority_path: Path,
) -> None:
    if source_truth_audit["status"] != "FAILED":
        return
    failures = source_truth_audit.get("failures") or []
    baseline_config = spec.get("subtitle_redelivery_baseline")
    recoverable_missing_substrings = bool(failures) and all(
        isinstance(row, Mapping)
        and row.get("action") == "replace_substring"
        and row.get("reason_code") == "REQUIRED_SOURCE_TRUTH_NOT_SATISFIED"
        and bool(row.get("local_windows"))
        for row in failures
    )
    # An exact-interval replay is a total, hash-bound late text authority: the
    # fresh ASR timeline is disposable and may split/merge/omit cues before the
    # reviewed SRT is restored.  Do not let cue-shape failures on that
    # disposable input prevent the authority from running.  This remains
    # fail-closed: package finalization validates the v2 baseline source hash,
    # basename and exact interval, then reapplies every source-truth rule and
    # blocks if any rule is still unsatisfied.  A generic/trimmed baseline does
    # not receive this exemption.
    recoverable_exact_cue_shape = bool(failures) and all(
        isinstance(row, Mapping)
        and row.get("action") == "replace_cue"
        and row.get("reason_code") == "REPLACE_CUE_TARGET_NOT_UNIQUE"
        and bool(row.get("local_windows"))
        for row in failures
    )
    # Mention-scoped postconditions may also be temporarily absent from fresh
    # ASR when a fully source-bound v2 exact replay is waiting to restore the
    # reviewed cue grid.  This exemption is deliberately narrower than the
    # generic substring fallback above: every required failure must already
    # have an isolated, positively resolved mention owner.  Missing windows,
    # ambiguous/not-isolated mentions, optional rows, mixed failure kinds, and
    # incomplete baseline/source bindings remain hard failures.
    recoverable_exact_mention_postconditions = bool(failures) and all(
        isinstance(row, Mapping)
        and row.get("required") is True
        and row.get("action") == "replace_substring"
        and row.get("reason_code")
        in {
            "MENTION_REQUIRED_TEXT_MISSING",
            "MENTION_FORBIDDEN_TOKEN_SURVIVED",
        }
        and bool(row.get("local_windows"))
        and isinstance(row.get("mention_owner_resolution"), Mapping)
        and row["mention_owner_resolution"].get("status") == "PASS"
        for row in failures
    )
    exact_v2_baseline_binding_valid = (
        _valid_redelivery_baseline_config(baseline_config)
        and baseline_config.get("schema_version") == "subtitle-redelivery-baseline.v2"
        and baseline_config.get("exact_interval_replay") is True
    )
    exact_mention_postconditions_pending = (
        exact_v2_baseline_binding_valid and recoverable_exact_mention_postconditions
    )
    exact_interval_replay_pending = (
        isinstance(baseline_config, Mapping)
        and baseline_config.get("schema_version") == "subtitle-redelivery-baseline.v2"
        and baseline_config.get("exact_interval_replay") is True
        and (recoverable_exact_cue_shape or exact_mention_postconditions_pending)
    )
    if baseline_config is None or not (
        recoverable_missing_substrings or exact_interval_replay_pending
    ):
        raise SystemExit(f"SOURCE_SUBTITLE_TRUTH_REQUIRED: {chat_authority_path}")
    source_truth_audit["pre_redelivery_status"] = "FAILED"
    source_truth_audit["status"] = "DEFERRED_TO_REDELIVERY_BASELINE"
    if exact_interval_replay_pending:
        source_truth_audit["deferred_strategy"] = (
            "exact_reviewed_interval_replay_then_reapply_source_truth"
        )
        source_truth_audit["deferred_reason"] = (
            "fresh ASR cue shape is not authoritative for this exact source "
            "interval; replay the hash-bound reviewed timeline, then reapply "
            "and verify every source-truth rule"
        )
    else:
        source_truth_audit["deferred_strategy"] = "reviewed_text_restore_then_reapply_source_truth"
        source_truth_audit["deferred_reason"] = (
            "fresh text did not retain the reviewed correction target; "
            "restore hash-bound prior delivery before reapplying source truth"
        )
    _persist_review_document(chat_authority_path, chat_authority_audit)


_windows_fully_cover = windows_fully_cover


def _apply_source_truth_and_resolve_deferred_foreign(
    srt_text: str,
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    ledger_path: Path | None,
    source_language_audit: dict[str, Any],
    satisfaction_reviews: Sequence[Mapping[str, object]] = (),
    local_term_reviews: Sequence[Mapping[str, object]] = (),
) -> tuple[str, dict[str, Any]]:
    srt_text, source_truth_audit = apply_source_subtitle_truth(
        srt_text,
        spec=spec,
        durations=durations,
        ledger_path=ledger_path,
        satisfaction_reviews=satisfaction_reviews,
        local_term_reviews=local_term_reviews,
    )
    if source_language_audit.get("status") != ("DEFERRED_TO_SOURCE_SUBTITLE_TRUTH"):
        return srt_text, source_truth_audit
    truth_rows = [
        row
        for key in ("applied", "satisfied")
        for row in (source_truth_audit.get(key) or [])
        if isinstance(row, Mapping) and row.get("required") is not False
    ]
    resolution = resolve_deferred_foreign_introductions(
        source_language_audit,
        srt_text,
        authority_rows=truth_rows,
        authority_kind="source_subtitle_truth",
    )
    source_language_audit["deferred_resolution"] = resolution
    if resolution["status"] == "PASS":
        source_language_audit["status"] = "RESOLVED_BY_SOURCE_SUBTITLE_TRUTH"
    elif _valid_redelivery_baseline_config(spec.get("subtitle_redelivery_baseline")):
        source_language_audit["source_truth_resolution_status"] = "NOT_RESOLVED_BEFORE_REDELIVERY"
        source_language_audit["status"] = "DEFERRED_TO_REDELIVERY_BASELINE"
    else:
        source_language_audit["status"] = (
            "BLOCKED_SOURCE_SUBTITLE_TRUTH_DID_NOT_RESOLVE_FOREIGN_INTRODUCTION"
        )
    return srt_text, source_truth_audit


def _structured_chat_names(
    clip_context: Mapping[str, object] | None,
) -> tuple[str, ...]:
    """Sender/gift names bound to this candidate's structured chat record."""

    rows = clip_context.get("structured_chat") if isinstance(clip_context, Mapping) else None
    if not isinstance(rows, list):
        return ()
    names: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for key in ("sender", "gift_name"):
            value = str(row.get(key) or "").strip()
            if value:
                names.append(value)
    return tuple(dict.fromkeys(names))


def _post_truth_release_hygiene(srt_text: str, chat_authority_audit: dict) -> str:
    """Post-truth structural guards + release-grade sliver merges.

    标题守卫在每个文本权威后复跑（后来的拼接可绕过早期守卫）；发布级贴邻
    合并把 <300ms 残片与非豁免单字并入邻居——生产
    出的包必须直接满足发布校验，质量债不留给发布链。
    """

    srt_text, final_title_mark_balance_audit = apply_title_mark_balance_guard(srt_text)
    chat_authority_audit["final_title_mark_balance_audit"] = final_title_mark_balance_audit
    srt_text, release_grade_merge_rows = merge_release_grade_cues(srt_text)
    if release_grade_merge_rows:
        chat_authority_audit["release_grade_cue_merges"] = release_grade_merge_rows
    return srt_text


def _adjudicate_final_language(
    padded: Path,
    srt_text: str,
    audit: dict[str, Any],
    out_root: Path,
    cid: str,
    chat_authority_audit: dict[str, Any],
    *,
    source_language: bool,
    source_srt: str,
    native_provider: str | None = None,
) -> tuple[str, dict[str, Any]]:
    if source_language and native_provider is not None:
        raise ValueError("native provider is currently scoped to mixed-script adjudication")
    input_srt = srt_text
    adjudicate = (adjudicate_language_preservation_audit if source_language
                  else adjudicate_foreign_script_audit)
    srt_text, audit = adjudicate(
        media_path=padded, srt_text=srt_text, audit=audit,
        out_root=out_root, cid=cid,
        llm_call=_build_final_review_llm_call(), source_srt=source_srt,
        **({"native_provider": native_provider} if native_provider is not None else {}),
    )
    register = (register_final_source_language_cpa_repairs if source_language
                else register_final_foreign_script_cpa_repairs)
    key = "source_language_audit" if source_language else "foreign_script_audit"
    register(chat_authority_audit, input_srt=input_srt, output_srt=srt_text, **{key: audit})
    return srt_text, audit


def _optional_text_override_document(path: Path | None) -> dict[str, Any]:
    if path is not None:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _finalize_text_evidence(
    *,
    spec: dict,
    durations: list[int],
    srt_text: str,
    chat_authority_audit: dict,
    transcript_entity_audit: dict,
    referent_groups: list[ReferentGroup],
    final_review_audit: dict,
    song_name_candidates: list[str],
    known_songs_path: Path,
    session_topic_authorities: tuple[dict[str, Any], ...],
    source_language_witness_srt: str,
    text_override_path: Path | None,
    source_truth_ledger_path: Path | None,
    out_root: Path,
    cid: str,
    padded: Path | None = None,
    clip_context: Mapping[str, object] | None = None,
    protected_name_source_srt: str | None = None,
    source_language_guard_reference: tuple[str, dict] | None = None,
    retained_reviewed_input: Mapping[str, Any] | None = None,
) -> TextEvidenceResult:
    from src.autoslice.retained_source_usage import reconcile_legacy_source_usage
    reconcile_legacy_source_usage(chat_authority_audit)
    if source_truth_ledger_path is not None and source_truth_ledger_path.is_file():
        from src.autoslice.producer_text_checkpoint import _frozen_binding
        chat_authority_audit['source_truth_attempt'] = {
            'schema_version':'producer-source-truth-attempt.v1',
            'ledger':_frozen_binding(out_root/'source-truth-input','ledger',source_truth_ledger_path)}
    def checkpoint_partial(text: str) -> None:
        context_path = out_root / f'{cid}.clip-context.json'
        if (padded is not None and context_path.is_file()
                and (source_language_witness_srt or retained_reviewed_input is not None)):
            from src.autoslice.producer_text_checkpoint import write_partial_source_text_checkpoint
            from src.autoslice.retained_source_input import load_retained_source_input
            retained_input = load_retained_source_input(spec=spec, cid=cid, padded=padded, out_root=out_root)
            write_partial_source_text_checkpoint(
                out_root / f'{cid}.partial-source-text-checkpoint.json', candidate_id=cid,
                srt_text=text, authority=chat_authority_audit, final_review=final_review_audit,
                context_path=context_path, padded=padded,
                source_language_witness_srt=source_language_witness_srt,
                protected_name_source_srt=protected_name_source_srt or text,
                native_budget_receipt_path=(retained_input.native.budget_receipt if retained_input else None),
                retained_reviewed_input=retained_reviewed_input,
            )

    # Review observations precede language/finalization providers. Persist the
    # actual unfinished input before any of those later operations can fail.
    checkpoint_partial(srt_text)
    if final_review_audit.get('status') == 'AUDITOR_UNAVAILABLE':
        raise SystemExit('FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED: source text review unavailable')
    srt_text, final_source_language_audit = _foreign_scope.guard_source_language(
        source_language_guard_reference[0] if source_language_guard_reference is not None
        else source_language_witness_srt, srt_text, spec, durations,
        structured_chat_names=_structured_chat_names(clip_context),
    )
    if (source_language_guard_reference is not None
            and final_source_language_audit.get("cue_binding") == "UNCHANGED_FOREIGN_CUE_INTERVALS"
            and final_source_language_audit.get("status") == "CLEAN"):
        final_source_language_audit['typed_language_reference'] = copy.deepcopy(source_language_guard_reference[1])
    chat_authority_audit["final_source_language_preservation_audit"] = final_source_language_audit
    override_document = _optional_text_override_document(text_override_path)
    if str(final_source_language_audit["status"]).startswith(
        "BLOCKED_UNPROVEN_FOREIGN_"
    ) and unproven_foreign_introductions_covered_by_overrides(
        final_source_language_audit,
        override_document,
    ):
        final_source_language_audit["status"] = "DEFERRED_TO_BOUND_TEXT_OVERRIDE"
        final_source_language_audit["deferred_reason"] = (
            "every un-witnessed foreign-language cue has a timeline-bound reviewed repair"
        )
    if padded is not None:
        # AGY listens candidate-blind; mismatches go to the text-only CPA judge.
        srt_text, final_source_language_audit = _adjudicate_final_language(
            padded,
            srt_text,
            final_source_language_audit,
            out_root,
            cid,
            chat_authority_audit,
            source_srt=source_language_witness_srt,
            source_language=True,
        )
        chat_authority_audit["final_source_language_preservation_audit"] = (
            final_source_language_audit
        )
    source_truth_local_windows = ledger_local_windows(
        spec=spec,
        durations=durations,
        ledger_path=source_truth_ledger_path,
    )
    defer_unproven_foreign_introductions_to_late_authority(
        final_source_language_audit,
        source_truth_windows=source_truth_local_windows,
        redelivery_baseline_config=spec.get("subtitle_redelivery_baseline"),
    )
    if padded is not None:
        from src.autoslice.audio_language_discovery import repair_audio_language_candidates
        srt_text, audio_language_audit = repair_audio_language_candidates(
            media_path=padded, srt_text=srt_text, out_root=out_root, cid=cid,
            llm_call=_build_final_review_llm_call(), authority=chat_authority_audit,
            source_srt=source_language_witness_srt,
            confirmed_audio_rows=final_source_language_audit.get("audio_witness_rows"),
            **_native_foreign_script_kwargs(spec),
        )
        chat_authority_audit["audio_language_discovery_audit"] = audio_language_audit
    foreign_script_audit = _foreign_scope.audit_foreign(srt_text, spec, durations)
    # 742_887 案：真值窗口完整覆盖的 mixed-latin cue 让位给
    # 稍后必然接管的 SOURCE_INTERVAL_TRUTH，早期门不得在乱码上否决更高权威。
    defer_truth_owned_mixed_latin_cues(
        foreign_script_audit,
        source_truth_windows=source_truth_local_windows,
    )
    if (
        foreign_script_audit["status"] == "BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER"
        and text_override_path is not None
    ):
        foreign_script_audit["status"] = "DEFERRED_TO_BOUND_TEXT_OVERRIDE"
        foreign_script_audit["deferred_reason"] = (
            "candidate has a hash-bound reviewed text override before delivery"
        )
    elif (
        foreign_script_audit["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"
        and text_override_path is not None
    ):
        if mixed_cjk_latin_findings_covered_by_overrides(
            foreign_script_audit,
            override_document,
        ):
            foreign_script_audit["status"] = "DEFERRED_TO_BOUND_TEXT_OVERRIDE"
            foreign_script_audit["deferred_reason"] = (
                "every mixed-language cue has a timeline-bound reviewed repair"
            )
    if (
        padded is not None
        and foreign_script_audit["status"] == "BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER"
    ):




        srt_text, cluster_repair_audit = retranscribe_foreign_script_cluster(
            media_path=padded,
            srt_text=srt_text,
            audit=foreign_script_audit,
            out_root=out_root,
            cid=cid,
        )
        if cluster_repair_audit.get("replaced_count"):
            foreign_script_audit = _foreign_scope.audit_foreign(srt_text, spec, durations)
        foreign_script_audit["cluster_retranscription"] = cluster_repair_audit
    if padded is not None:
        srt_text, foreign_script_audit = _adjudicate_final_language(
            padded, srt_text, foreign_script_audit, out_root, cid, chat_authority_audit,
            source_srt=source_language_witness_srt,
            source_language=False,
            **_native_foreign_script_kwargs(spec),
        )
    chat_authority_audit["foreign_script_consistency_audit"] = foreign_script_audit
    srt_text, title_mark_balance_audit = apply_title_mark_balance_guard(srt_text)
    chat_authority_audit["title_mark_balance_audit"] = title_mark_balance_audit
    srt_text, impossible_punctuation_audit = apply_impossible_punctuation_guard(srt_text)
    chat_authority_audit["impossible_punctuation_audit"] = impossible_punctuation_audit
    srt_text, final_session_topic_absorption_audit = absorb_session_topic_entities(
        srt_text, session_topic_authorities
    )
    chat_authority_audit.setdefault("session_topic_absorption_audits", []).append(
        final_session_topic_absorption_audit
    )
    srt_text, unregistered_entity_reverts = revert_unregistered_entity_repairs(
        srt_text,
        chat_authority_audit.get("entity_repairs") or [],
        registered_entity_names(referent_groups),
    )
    if unregistered_entity_reverts:
        chat_authority_audit["unregistered_entity_reverts"] = unregistered_entity_reverts
        print(
            "[chat-authority] unregistered entity repairs reverted: "
            + json.dumps(unregistered_entity_reverts, ensure_ascii=False),
            flush=True,
        )
    # 同槽矛盾裁定和解：多个 pass 对同一 cue
    # 给出互斥的 RESOLVED 实体 → 该处听证不可信，回退最早改写前的文本并披露；
    # 严禁后写者赢。
    srt_text, entity_verdict_contradictions = reconcile_contradictory_entity_repairs(
        srt_text, chat_authority_audit.get("entity_repairs") or []
    )
    if entity_verdict_contradictions:
        chat_authority_audit["entity_verdict_contradictions"] = entity_verdict_contradictions
        print(
            "[chat-authority] contradictory entity verdicts reverted: "
            + json.dumps(entity_verdict_contradictions, ensure_ascii=False),
            flush=True,
        )
    chat_authority_audit["final_review_audit"] = final_review_audit
    persist_review_audit(out_root / f"{cid}.review-flags.json", final_review_audit)
    retain_post_transcript_text(chat_authority_audit, srt_text)
    chat_authority_path = out_root / f"{cid}.chat-authority.json"
    _persist_review_document(chat_authority_path, chat_authority_audit)
    checkpoint_partial(srt_text)
    if str(final_source_language_audit["status"]).startswith("BLOCKED_") or str(
        foreign_script_audit["status"]
    ).startswith("BLOCKED_"):
        raise SystemExit(f"FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED: {chat_authority_path}")
    if song_name_candidates:
        srt_text, song_name_pin_audit, semantic_verification = verify_and_pin_song_names(
            srt_text,
            candidates=song_name_candidates,
            known_songs_path=known_songs_path,
            selection_hook=str(spec.get("selection_hook") or ""),
            title_quote=str(spec.get("given_title") or ""),
        )
        for suffix, payload in (
            ("song-name-semantic-verification", semantic_verification),
            ("song-name-pin", song_name_pin_audit),
        ):
            (out_root / f"{cid}.{suffix}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if song_name_pin_audit.get("replacements"):
            print(
                f"{cid}: pinned {len(song_name_pin_audit['replacements'])} "
                "song name(s) from screen-songlist/点歌 evidence"
            )
    # 称呼串等价类：、包夹单字近音按成员词补全，与 hard canon 同级。
    from src.autoslice.surface_canon import repair_address_enumerations
    srt_text, address_enumeration_audit = repair_address_enumerations(srt_text)
    chat_authority_audit["address_enumeration_audit"] = address_enumeration_audit
    # Final meme canon runs after every mutable text stage; the later bound
    # human override independently re-runs it before speaker rendering.
    srt_text, hard_meme_surface_audit = normalize_hard_meme_surfaces(srt_text)
    chat_authority_audit["final_hard_meme_surface_audit"] = hard_meme_surface_audit
    # Explicit expected-value canon is a narrow, auditable zero-CPA lane.
    # It runs after every mutable model stage but before operator source truth,
    # so a rare source-bound exception can still override the statistical
    # default.
    from src.autoslice.surface_canon import protect_reviewed_name_slots
    srt_text, protected_name_repairs = protect_reviewed_name_slots(
        srt_text, protected_name_source_srt or srt_text,
    )
    chat_authority_audit["protected_name_slot_repairs"] = protected_name_repairs
    srt_text, expected_value_surface_audit = normalize_expected_value_surfaces(srt_text)
    chat_authority_audit["final_expected_value_surface_audit"] = expected_value_surface_audit
    srt_text, source_truth_audit = _apply_source_truth_and_resolve_deferred_foreign(
        srt_text,
        spec=spec,
        durations=durations,
        ledger_path=source_truth_ledger_path,
        source_language_audit=final_source_language_audit,
        satisfaction_reviews=(chat_authority_audit.get("source_subtitle_truth_audit") or {}).get("meaning_reviews") or (),
        local_term_reviews=(chat_authority_audit.get("source_subtitle_truth_audit") or {}).get("local_term_reviews") or (),
    )
    if source_truth_audit.get("status") == "FAILED" and source_truth_ledger_path is not None:
        from src.autoslice.source_truth_satisfaction import review_missing_truth
        try:
            reviews, call_ids = review_missing_truth(
                srt_text=srt_text, spec=spec, durations=durations,
                ledger_path=source_truth_ledger_path, audit=source_truth_audit,
                clip_context=clip_context or {}, llm_factory=_build_final_review_llm_call,
            )
        except Exception as exc:
            chat_authority_audit["source_truth_satisfaction_error"] = {
                "reason_code": "SOURCE_TRUTH_SATISFACTION_REVIEW_UNAVAILABLE", "error_type": type(exc).__name__}
            checkpoint_partial(srt_text)
            raise SystemExit("SOURCE_TRUTH_SATISFACTION_REVIEW_UNAVAILABLE") from exc
        if reviews:
            reviews = [*(source_truth_audit.get('meaning_reviews') or ()),
                       *(row for row in reviews if row not in (source_truth_audit.get('meaning_reviews') or ()))]
            accounted = chat_authority_audit.setdefault("source_truth_satisfaction_accounted", [])
            usage = chat_authority_audit.setdefault("cpa_adjudication_usage", {})
            count = usage.get("provider_adjudication_count")
            if type(count) is not int or count < 0:
                raise SystemExit("SOURCE_TRUTH_SATISFACTION_USAGE_UNBOUND")
            new_ids = [value for value in call_ids if value not in accounted]
            usage["provider_adjudication_count"] = count + len(new_ids)
            accounted.extend(new_ids)
            final_review_audit["cpa_adjudication_usage"] = copy.deepcopy(usage)
            srt_text, source_truth_audit = _apply_source_truth_and_resolve_deferred_foreign(
                srt_text, spec=spec, durations=durations, ledger_path=source_truth_ledger_path,
                source_language_audit=final_source_language_audit, satisfaction_reviews=reviews,
            )
    if source_truth_audit.get('status') == 'FAILED' and source_truth_ledger_path is not None:
        from src.autoslice.source_truth_term_alignment import review_local_term_alignment
        try:
            term_reviews, term_call_ids = review_local_term_alignment(srt_text=srt_text,
                spec=spec,durations=durations,ledger_path=source_truth_ledger_path,
                audit=source_truth_audit,clip_context=clip_context or {},llm_factory=_build_final_review_llm_call)
        except Exception as exc:
            chat_authority_audit['source_truth_term_alignment_error'] = {
                'reason_code':'SOURCE_TRUTH_TERM_ALIGNMENT_REVIEW_UNAVAILABLE','error_type':type(exc).__name__}
            checkpoint_partial(srt_text)
            raise SystemExit('SOURCE_TRUTH_TERM_ALIGNMENT_REVIEW_UNAVAILABLE') from exc
        if term_reviews:
            term_reviews = [*(source_truth_audit.get('local_term_reviews') or ()),
                            *(row for row in term_reviews if row not in (source_truth_audit.get('local_term_reviews') or ()))]
            accounted = chat_authority_audit.setdefault('source_truth_term_alignment_accounted',[])
            usage = chat_authority_audit.setdefault('cpa_adjudication_usage',{})
            count = usage.get('provider_adjudication_count')
            if type(count) is not int or count < 0:
                raise SystemExit('SOURCE_TRUTH_TERM_ALIGNMENT_USAGE_UNBOUND')
            new_ids = [value for value in term_call_ids if value not in accounted]
            usage['provider_adjudication_count'] = count+len(new_ids)
            accounted.extend(new_ids)
            final_review_audit['cpa_adjudication_usage'] = copy.deepcopy(usage)
            srt_text, source_truth_audit = _apply_source_truth_and_resolve_deferred_foreign(
                srt_text,spec=spec,durations=durations,ledger_path=source_truth_ledger_path,
                source_language_audit=final_source_language_audit,
                satisfaction_reviews=source_truth_audit.get('meaning_reviews') or (),local_term_reviews=term_reviews)
    chat_authority_audit["source_subtitle_truth_audit"] = source_truth_audit
    checkpoint_partial(srt_text)
    verify_source_truth_preview_formal_binding(
        chat_authority_audit,
        source_truth_audit,
    )
    # 让位给真值的 mixed-latin 门必须闭环：接管后的文本重审，没洗净就硬拦。
    if foreign_script_audit.get("status") == "DEFERRED_TO_SOURCE_SUBTITLE_TRUTH":
        post_truth_foreign = _foreign_scope.audit_foreign(srt_text, spec, durations)
        foreign_script_audit["post_truth_reaudit"] = post_truth_foreign
        if str(post_truth_foreign["status"]).startswith("BLOCKED_"):
            raise SystemExit(
                f"FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED_AFTER_TRUTH: {chat_authority_path}"
            )
        foreign_script_audit["status"] = "RESOLVED_BY_SOURCE_SUBTITLE_TRUTH"
    srt_text = _post_truth_release_hygiene(srt_text, chat_authority_audit)
    # Source/operator truth may restore an older romanized spelling. Native
    # Japanese script is a final presentation invariant, so re-assert it after
    # the last mutable truth/hygiene stage.
    srt_text, japanese_native_script_audit = normalize_japanese_native_script_surfaces(srt_text)
    chat_authority_audit["final_japanese_native_script_audit"] = japanese_native_script_audit




    reconcile_required_source_truth_chat_authority(
        chat_authority_audit,
        source_truth_audit,
    )
    # Late cue merges can reindex unchanged foreign context. Keep the adjudicated
    # audit (status, audio witness rows, deferrals) and bind a recomputable raw
    # guard to the bytes actually persisted by this emitter.
    _bind_foreign_raw_guard(
        chat_authority_audit["foreign_script_consistency_audit"],
        _foreign_scope.audit_foreign(srt_text, spec, durations),
        srt_text,
    )
    chat_authority_audit["final_output_srt_sha256"] = hashlib.sha256(
        srt_text.encode("utf-8")
    ).hexdigest()
    _persist_review_document(chat_authority_path, chat_authority_audit)
    if str(final_source_language_audit.get("status") or "").startswith("BLOCKED_"):
        raise SystemExit(f"FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED: {chat_authority_path}")
    if _foreign_guard_blocks(chat_authority_audit["foreign_script_consistency_audit"]):
        raise SystemExit(f"FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED: {chat_authority_path}")
    if (
        chat_authority_audit["status"]
        in {"FAILED", "ENTITY_VERDICT_REQUIRED", "SC_SENDER_VERDICT_REQUIRED"}
        or transcript_entity_audit["status"] == "ENTITY_VERDICT_REQUIRED"
    ):
        # （1013 jyl-r10 案）：v2 精确重放的 redelivery 里，SC 发送者
        # 裁决 UNRESOLVED 可让位于基线——本轮 ASR 把「万事屋_Official」听岔成
        # 「问15」，CPA 闭集裁不动；但该 cue 的终局注定被 exact_interval_replay
        # 盖回已发布基线文本（发送者表面在原次发布时已验证过），garble 根本
        # 到不了最终产物，final owner 校验仍强制逐字节等于基线，fail-closed
        # 链完整。这是 source_truth `DEFERRED_TO_REDELIVERY_BASELINE`
        # （:1313）的同款惯例；FAILED / 实体裁决缺失照旧硬拦。
        baseline_config = spec.get("subtitle_redelivery_baseline")
        sender_only = (
            chat_authority_audit["status"] == "SC_SENDER_VERDICT_REQUIRED"
            and transcript_entity_audit["status"] != "ENTITY_VERDICT_REQUIRED"
        )
        if (
            sender_only
            and _valid_redelivery_baseline_config(baseline_config)
            and isinstance(baseline_config, Mapping)
            and baseline_config.get("schema_version") == "subtitle-redelivery-baseline.v2"
            and baseline_config.get("exact_interval_replay") is True
        ):
            chat_authority_audit["sc_sender_verdict_deferral"] = {
                "status": "DEFERRED_TO_REDELIVERY_BASELINE",
                "reason": (
                    "unresolved SC sender surface is overwritten by the "
                    "exact reviewed interval replay; the published baseline "
                    "sender surface was verified in the original run and the "
                    "final owner verification still enforces byte equality"
                ),
            }
            _persist_review_document(chat_authority_path, chat_authority_audit)
        else:
            raise SystemExit(f"CHAT_AUTHORITY_FINALIZATION_FAILED: {chat_authority_path}")
    # In a hash-bound subtitle redelivery only, a missing substring target can
    # be restored from the reviewed baseline and the higher source truth then
    # reapplied.  Every other mode still stops here.
    _defer_source_truth_failure_for_redelivery(
        spec=spec,
        source_truth_audit=source_truth_audit,
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_authority_path,
    )
    if (
        chat_authority_audit["final_title_mark_balance_audit"]["status"]
        == "UNRESOLVED_COMPLEX_IMBALANCE"
    ):
        raise SystemExit(f"TITLE_MARK_BALANCE_REQUIRED: {chat_authority_path}")
    (out_root / "padded.fresh.srt").write_text(srt_text, encoding="utf-8")
    cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
    if len(cues) < 3:
        raise SystemExit("FRESH_TRANSCRIPTION_TOO_SPARSE")
    return TextEvidenceResult(
        srt_text=srt_text,
        cues=cues,
        chat_authority_path=chat_authority_path,
    )


def _continuation_sha256_path(path: Path, *, label: str) -> str:
    if not path.is_file() or path.is_symlink():
        raise CurrentTextContinuationError(f"{label}_ARTIFACT_NOT_REGULAR_FILE")
    # The one in-process, placement-keyed file digest (evidence_identity):
    # cold continuation rebinds the same media/checkpoints from several
    # validators and must not re-read unchanged bytes for each of them.
    from src.autoslice.evidence_identity import regular_file_sha256
    digest = regular_file_sha256(path)
    if digest is None:
        raise CurrentTextContinuationError(f"{label}_ARTIFACT_UNREADABLE")
    return digest


def _continuation_normalize_sha(value: object) -> str:
    return str(value or "").removeprefix("sha256:")


def _continuation_require_sha(actual: str, expected: object, *, label: str) -> None:
    expected_sha = _continuation_normalize_sha(expected)
    if len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
        raise CurrentTextContinuationError(f"{label}_DECLARATION_INVALID")
    if actual != expected_sha:
        raise CurrentTextContinuationError(f"{label}_DRIFT")


def _continuation_read_json(
    path: Path, *, label: str, review: bool = False,
) -> tuple[dict[str, Any], str]:
    digest = _continuation_sha256_path(path, label=label)
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise CurrentTextContinuationError(f"{label}_ARTIFACT_CHANGED_DURING_READ")
        if review:
            value = review_from_bytes(raw, directory=path.parent)
        else:
            value = json.loads(raw.decode("utf-8"))
            from src.autoslice.review_evidence_store import expand
            value = expand(value)
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentTextContinuationError(f"{label}_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise CurrentTextContinuationError(f"{label}_JSON_OBJECT_REQUIRED")
    return value, digest


def _continuation_load_timing_resolution(
    continuation: CurrentTextContinuation,
) -> tuple[dict[str, Any], str] | None:
    path = continuation.source_timing_resolution_path
    expected = continuation.expected_source_timing_resolution_sha256
    if path is None and expected is None:
        return None
    if path is None or expected is None:
        raise CurrentTextContinuationError("CURRENT_SOURCE_TIMING_RESOLUTION_BINDING_MISSING")
    if not path.is_file() or path.is_symlink():
        raise CurrentTextContinuationError("CURRENT_SOURCE_TIMING_RESOLUTION_ARTIFACT_NOT_REGULAR_FILE")
    try:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        _continuation_require_sha(digest, expected, label="CURRENT_SOURCE_TIMING_RESOLUTION")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CurrentTextContinuationError("CURRENT_SOURCE_TIMING_RESOLUTION_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise CurrentTextContinuationError("CURRENT_SOURCE_TIMING_RESOLUTION_JSON_OBJECT_REQUIRED")
    return value, "sha256:" + digest


def _continuation_validate_existing_repair(
    row: Mapping[str, Any],
    *,
    srt_text: str,
    clip_context: Mapping[str, object],
) -> None:
    """Validate a repair already emitted by the normal CPA consumer.

    This deliberately accepts the producer's existing ``entity_repairs`` and
    ``context_audio_adjudication`` shapes.  It does not create a continuation
    receipt or promote a private evidence wrapper.  A repair is usable only
    when the normal closed-set judge is bound to the same request and its
    mutation authority is already present.
    """

    cue_indexes = row.get("cue_indexes")
    if not isinstance(cue_indexes, list) or len(cue_indexes) != 1:
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_CUE_BINDING_INVALID")
    before = row.get("before")
    after = row.get("after")
    if not isinstance(before, list) or not isinstance(after, list) or len(before) != 1:
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_TEXT_BINDING_INVALID")
    changed = before[0] != after[0]
    if not changed:
        return
    # DROP_CUE has an established two-request contract: ``request_sha256``
    # identifies the effective drop request, while the typed judge is bound to
    # ``drop_authority.original_request_sha256``.  Do not flatten that
    # contract into the ordinary replacement-row check below.  The shared
    # validator recomputes both request identities and the witness/judge
    # hashes from the existing receipt.
    if row.get("action") == "DROP_CUE":
        if not valid_inaudible_drop_repair(row):
            raise CurrentTextContinuationError(
                "CURRENT_ENTITY_REPAIR_DROP_AUTHORITY_INVALID"
            )
        return
    judge = row.get("judge")
    if not isinstance(judge, Mapping) or judge.get("status") != "JUDGED":
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_JUDGE_MISSING")
    choice = str(judge.get("choice") or "")
    expected_choice = "DROP" if row.get("action") == "DROP_CUE" else "PROPOSED"
    if choice != expected_choice:
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_CHOICE_MISMATCH")
    if (
        judge.get("decision_contract") == "current-proposed-candidate-id-neither.v1"
        and judge.get("selected_candidate_text") != after[0]
    ):
        # Historical direct-native rows may omit the richer adjudication
        # wrapper below. Their selected transcript still has to be the text
        # actually written; a later continuation cannot legitimize the old
        # candidate/proposal mapping error merely from a request hash.
        raise CurrentTextContinuationError(
            "CURRENT_ENTITY_REPAIR_SELECTED_TRANSCRIPT_MISMATCH"
        )
    request_sha = _continuation_normalize_sha(row.get("request_sha256"))
    if len(request_sha) != 64 or any(c not in "0123456789abcdef" for c in request_sha):
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_REQUEST_MISSING")
    if _continuation_normalize_sha(judge.get("check_request_sha256")) != request_sha:
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_REQUEST_DRIFT")
    mutation = row.get("mutation_authority")
    if not isinstance(mutation, Mapping) or mutation.get("status") != "PASS":
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_MUTATION_AUTHORITY_INVALID")
    if row.get("timing_immutable") is not True:
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_TIMING_NOT_IMMUTABLE")

    native_witness = row.get("acoustic_witness")
    if isinstance(native_witness, Mapping) and native_witness.get("argon_escalation_evidence") is not None:
        from src.autoslice.exact_final_witness_authority import valid_acoustic_witness_binding
        bound = row.get("acoustic_witness_binding")
        if (not valid_acoustic_witness_binding(bound, proposed=after[0],
                window=(row.get("matched_start_ms"), row.get("matched_end_ms")))
            or bound.get("witness") != native_witness or bound.get("judge") != judge
            or bound["check_request"]["current_cue"] != before[0]):
            raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_WITNESS_INVALID")
    adjudication = row.get("context_audio_adjudication")
    if not isinstance(adjudication, Mapping):
        return
    nested_judge = adjudication.get("witness_judge")
    nested_judge = nested_judge.get("judge") if isinstance(nested_judge, Mapping) else None
    request = adjudication.get("request")
    if not isinstance(request, Mapping) or not isinstance(nested_judge, Mapping):
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_ADJUDICATION_INCOMPLETE")
    if nested_judge.get("status") != "JUDGED" or nested_judge.get("choice") != choice:
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_ADJUDICATION_CHOICE_DRIFT")
    nested_request_sha = _continuation_normalize_sha(request.get("request_sha256"))
    if nested_request_sha != request_sha:
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_ADJUDICATION_REQUEST_DRIFT")
    rebuilt = build_context_adjudication_request(
        srt_text,
        {**dict(row), **({"_caption_audio_roles": request["caption_audio_roles"]}
                        if "caption_audio_roles" in request else {}),
         **({"_prior_range_acoustic_observations": request["context_only_range_acoustic_observations"]}
            if "context_only_range_acoustic_observations" in request else {})},
        clip_context=clip_context,
        source_media_timeline_offset_ms=int(
            request.get("source_media_timeline_offset_ms") or 0
        ),
        require_complete_utterance_support=bool(
            request.get("require_complete_utterance_support") is True
        ),
    )
    if rebuilt.get("request_sha256") != request.get("request_sha256"):
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_REQUEST_REBUILD_DRIFT")
    verdict = adjudication.get("verdict")
    if isinstance(verdict, Mapping):
        from src.autoslice.acoustic_witness_adjudication import (
            build_witness_request,
            valid_witness_evidence,
        )

        witness_request = build_witness_request(request)
        if not valid_witness_evidence(
            verdict, request_sha256=str(witness_request["request_sha256"])
        ):
            raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_WITNESS_INVALID")
    candidate = dict(row)
    candidate["exact_release_adjudication"] = dict(adjudication)
    proposed = str(after[0])
    if adjudicated_proposed_full_cue(
        candidate,
        srt_text=srt_text,
        expected_srt_sha256="sha256:" + hashlib.sha256(srt_text.encode("utf-8")).hexdigest(),
        clip_context=clip_context,
    ) != proposed:
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_PROPOSAL_INVALID")


def _continuation_validate_final_language_owner(
    row: Mapping[str, Any], *, row_index: int, chat_authority: Mapping[str, Any],
    srt_text: str, final_review: Mapping[str, Any],
    projection_review: Mapping[str, Any] | None = None,
) -> None:
    """Replay a source-language owner on its original clock after a real cut."""
    from src.autoslice.final_source_language_owner import validate_final_cpa_owner

    try:
        if (final_review.get("boundary_semantic_review") or {}).get("review_scope") == "source_full_window":
            validate_final_cpa_owner(row, row_index=row_index, authority=chat_authority,
                                     srt_text=srt_text)
            return
        # The source writer predates selection. Its original complete text is
        # authenticated separately; a source owner outside the selected story
        # must not be relabeled as a delivery-clock replacement.
        source_text = chat_authority.get("post_transcript_entity_output_srt")
        if not isinstance(source_text, str) or not source_text:
            raise ValueError("FINAL_LANGUAGE_SOURCE_TEXT_MISSING")
        _continuation_require_sha(
            hashlib.sha256(source_text.encode()).hexdigest(),
            chat_authority.get("post_transcript_entity_output_srt_sha256"),
            label="CURRENT_FINAL_LANGUAGE_SOURCE_TEXT",
        )
        projected, clock = _continuation_project_delivery_to_source(
            parse_srt_cues(srt_text),
            final_review_audit=final_review if projection_review is None else projection_review,
            authority=chat_authority,
        )
        validate_final_cpa_owner(
            row, row_index=row_index, authority=chat_authority, srt_text=source_text,
            delivery_source_window=(clock["source_final_start_ms"], clock["source_final_end_ms"]),
        )
        start, end = row["matched_start_ms"], row["matched_end_ms"]
        if end <= clock["source_final_start_ms"] or start >= clock["source_final_end_ms"]:
            return  # A validated historical effect, absent from this delivery.
        matches = [cue for cue in projected if (cue.start_ms, cue.end_ms) == (start, end)]
        if len(matches) != 1 or row.get("after") != [matches[0].text]:
            raise ValueError("FINAL_LANGUAGE_DELIVERY_SURFACE_DRIFT")
    except (ValueError, KeyError, TypeError) as exc:
        raise CurrentTextContinuationError("CURRENT_FINAL_LANGUAGE_OWNER_INVALID") from exc


def _continuation_validate_exact_final_owner(
    row: Mapping[str, Any],
    *,
    row_index: int,
    chat_authority: Mapping[str, Any],
    source_clock_review: Mapping[str, object] | None = None,
    source_timeline_offset_ms: int | None = None,
) -> None:
    """Replay an existing exact-final owner from its validated repair chain.

    Cycle and convergence judges use their own request contract, so they do
    not carry an ordinary ``check_request_sha256``.  Their surface owner must
    instead exactly match the normal registration writer and a unique repair
    in the already-validated self-heal chain.  Nothing is re-bound or applied.
    """
    from src.autoslice.producer_package_finalization import _register_exact_final_cpa_repairs

    digest = row.get("exact_final_repair_sha256")
    receipt = chat_authority.get("exact_final_cpa_self_heal")
    passes = receipt.get("passes") if isinstance(receipt, Mapping) else None
    repairs = [
        repair
        for item in passes if isinstance(item, Mapping)
        for repair in item.get("repairs", [])
        if isinstance(repair, Mapping)
    ] if isinstance(passes, list) else []
    # A normal later review moves completed runs into retained_previous_runs.
    # An owner registration still names the original repair, not the new run.
    # Replay that original completed contract without changing history or
    # transferring its final SRT/PASS to the current review.
    if isinstance(receipt, Mapping) and receipt.get("retained_previous_runs"):
        from src.autoslice.exact_final_cpa_history import repair_history

        try:
            repair_history(receipt)  # Validate the immutable history envelope.
            for entry in receipt["retained_previous_runs"]:
                prior = entry["audit"]
                _validate_exact_final_cpa_self_heal(
                    {"exact_final_cpa_self_heal": prior},
                    expected_srt_sha256=prior["final_srt_sha256"],
                )
                repairs.extend(
                    repair
                    for item in prior["passes"]
                    for repair in item["repairs"]
                )
        except (ValueError, KeyError, TypeError) as exc:
            raise CurrentTextContinuationError(
                "CURRENT_EXACT_FINAL_OWNER_HISTORY_INVALID"
            ) from exc
    matches = [
        repair
        for repair in repairs
        if "sha256:" + hashlib.sha256(json.dumps(
            repair, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest() == digest
    ]
    registrations = chat_authority.get("exact_final_cpa_surface_registrations")
    registered = [
        entry for entry in registrations
        if isinstance(entry, Mapping)
        and entry.get("schema_version") == "exact-final-cpa-surface-registration.v1"
        and entry.get("status") == "REGISTERED"
        and entry.get("owner_entity_repair_index") == row_index
        and entry.get("exact_final_repair_sha256") == digest
    ] if isinstance(registrations, list) else []
    if len(matches) != 1 or len(registered) != 1:
        raise CurrentTextContinuationError("CURRENT_EXACT_FINAL_OWNER_UNBOUND")
    repair = matches[0]
    if repair.get("editorial_authority") is not None:
        from src.autoslice.editorial_subtitle import validate_repair
        try:
            validate_repair(repair)
            offset = repair["editorial_authority"]["request"]["source_media_timeline_offset_ms"]
            if type(offset) is not int or offset < 0:
                raise ValueError("EDITORIAL_CLOCK_INVALID")
        except (ValueError, TypeError, KeyError) as exc:
            raise CurrentTextContinuationError("CURRENT_EDITORIAL_OWNER_INVALID") from exc
    else:
        # A range projection is a separate authority from the narrow exact crop.
        # The normal writer may retain ``None`` in the unused field, but two
        # live mappings are ambiguous and cannot be replayed as one owner.
        exact_binding = repair.get("acoustic_witness_binding")
        range_binding = repair.get(RANGE_WORD_PROJECTION_FIELD)
        has_exact_binding = isinstance(exact_binding, Mapping)
        has_range_binding = isinstance(range_binding, Mapping)
        if has_exact_binding == has_range_binding:
            raise CurrentTextContinuationError(
                "CURRENT_EXACT_FINAL_OWNER_WITNESS_INVALID"
            )
        local_start = repair.get("matched_start_ms")
        local_end = repair.get("matched_end_ms")
        if has_range_binding:
            if (
                type(local_start) is not int
                or type(local_end) is not int
                or not valid_range_word_projection_binding(
                    range_binding,
                    proposed=str(repair.get("after") or ""),
                    window=(local_start, local_end),
                )
            ):
                raise CurrentTextContinuationError(
                    "CURRENT_EXACT_FINAL_OWNER_WITNESS_INVALID"
                )
            check_request = range_binding.get("check_request")
            broad_request = range_binding.get("witness_request")
            validated_witness = range_binding.get("witness")
            broad_in_witness = (
                validated_witness.get("witness_request")
                if isinstance(validated_witness, Mapping)
                else None
            )
            if not all(
                isinstance(request, Mapping)
                for request in (check_request, broad_request, broad_in_witness)
            ):
                raise CurrentTextContinuationError(
                    "CURRENT_EXACT_FINAL_OWNER_WITNESS_INVALID"
                )
            offsets = [
                request.get("source_media_timeline_offset_ms")
                for request in (check_request, broad_request, broad_in_witness)
            ]
            if (
                any(type(offset_value) is not int or offset_value < 0 for offset_value in offsets)
                or len(set(offsets)) != 1
            ):
                raise CurrentTextContinuationError(
                    "CURRENT_EXACT_FINAL_OWNER_CLOCK_INVALID"
                )
            binding = range_binding
        else:
            # Preserve the established exact-crop owner path byte-for-byte.  Its
            # full binding was validated by the existing self-heal contract; this
            # replay only retains the historical clock checks below.
            binding = exact_binding

        # Exact-final rows can come from different clock bases in one otherwise
        # coherent authority (for example, a later repair may have been written
        # against a 30200 ms delivery offset while an earlier row used 29950 ms).
        # The boundary/current offset is therefore not a valid replay input for
        # this owner.  Require the two request records emitted with this exact
        # repair to agree and replay with that repair-local value.
        del source_clock_review, source_timeline_offset_ms
        if not isinstance(binding, Mapping):
            raise CurrentTextContinuationError(
                "CURRENT_EXACT_FINAL_OWNER_CLOCK_INVALID"
            )
        request_offsets: list[int] = []
        for request_key in ("check_request", "witness_request"):
            request = binding.get(request_key)
            if not isinstance(request, Mapping):
                raise CurrentTextContinuationError(
                    "CURRENT_EXACT_FINAL_OWNER_CLOCK_INVALID"
                )
            offset_value = request.get("source_media_timeline_offset_ms")
            if (
                isinstance(offset_value, bool)
                or not isinstance(offset_value, int)
                or offset_value < 0
            ):
                raise CurrentTextContinuationError(
                    "CURRENT_EXACT_FINAL_OWNER_CLOCK_INVALID"
                )
            request_offsets.append(offset_value)
        if request_offsets[0] != request_offsets[1]:
            raise CurrentTextContinuationError(
                "CURRENT_EXACT_FINAL_OWNER_CLOCK_INVALID"
            )
        offset = request_offsets[0]
    replay: dict[str, Any] = {}
    _register_exact_final_cpa_repairs(
        replay, [dict(matches[0])], delivery_start_ms=offset,
    )
    expected = replay["entity_repairs"][0]
    # Supersession lists refer to earlier rows in the original ledger.  All
    # authority, text and source geometry fields come from the normal writer.
    ignored = {"superseded_entity_repair_indexes", "superseded_applied_indexes", "survived"}
    if any(row.get(key) != value for key, value in expected.items() if key not in ignored):
        raise CurrentTextContinuationError("CURRENT_EXACT_FINAL_OWNER_DRIFT")
    judge = row.get("judge")
    if not isinstance(judge, Mapping) or judge.get("status") != "JUDGED" or judge.get("choice") != "PROPOSED":
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_CHOICE_MISMATCH")

    reconciliation = row.get("reconciliation")
    if reconciliation is None:
        return
    if not isinstance(reconciliation, Mapping):
        raise CurrentTextContinuationError(
            "CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID"
        )
    if (
        reconciliation.get("schema_version")
        != "exact-final-cpa-supersession.v1"
        or reconciliation.get("status") != "SUPERSEDED_BY_EXACT_FINAL_CPA"
        or reconciliation.get("timing_immutable") is not True
    ):
        raise CurrentTextContinuationError(
            "CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID"
        )

    successor_digest = reconciliation.get("exact_final_repair_sha256")
    if not isinstance(successor_digest, str) or not successor_digest.startswith("sha256:"):
        raise CurrentTextContinuationError(
            "CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID"
        )
    entity_rows = chat_authority.get("entity_repairs")
    if not isinstance(entity_rows, list):
        raise CurrentTextContinuationError(
            "CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID"
        )
    successor_rows = [
        (index, candidate)
        for index, candidate in enumerate(entity_rows)
        if isinstance(candidate, Mapping)
        and candidate.get("exact_final_repair_sha256") == successor_digest
    ]
    if len(successor_rows) != 1 or successor_rows[0][0] == row_index:
        raise CurrentTextContinuationError(
            "CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID"
        )
    successor_index, successor_row = successor_rows[0]
    successor_repairs = [
        repair
        for repair in repairs
        if "sha256:" + hashlib.sha256(json.dumps(
            repair, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest() == successor_digest
    ]
    if len(successor_repairs) != 1:
        raise CurrentTextContinuationError(
            "CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID"
        )
    successor_repair = successor_repairs[0]
    registrations = chat_authority.get("exact_final_cpa_surface_registrations")
    successor_registrations = [
        entry
        for entry in registrations
        if isinstance(entry, Mapping)
        and entry.get("schema_version")
        == "exact-final-cpa-surface-registration.v1"
        and entry.get("status") == "REGISTERED"
        and entry.get("owner_entity_repair_index") == successor_index
        and entry.get("exact_final_repair_sha256") == successor_digest
    ] if isinstance(registrations, list) else []
    if len(successor_registrations) != 1:
        raise CurrentTextContinuationError(
            "CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID"
        )

    # A supersession is valid only for the same immutable cue surface.  The
    # predecessor's after text must be the successor's before text, and the
    # marker hashes must agree with those exact bytes.  This keeps a retired
    # owner in history without allowing it to become an active replay row.
    for field in ("cue_indexes", "matched_start_ms", "matched_end_ms", "timing_immutable"):
        if row.get(field) != successor_row.get(field):
            raise CurrentTextContinuationError(
                "CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID"
            )

    def _surface_text(owner: Mapping[str, Any], field: str) -> str:
        value = owner.get(field)
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            return value[0]
        if isinstance(value, str) and value:
            return value
        raise CurrentTextContinuationError(
            "CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID"
        )

    predecessor_after = _surface_text(row, "after")
    successor_before = _surface_text(successor_row, "before")
    successor_after = _surface_text(successor_row, "after")
    predecessor_after_sha = "sha256:" + hashlib.sha256(
        predecessor_after.encode("utf-8")
    ).hexdigest()
    successor_before_sha = "sha256:" + hashlib.sha256(
        successor_before.encode("utf-8")
    ).hexdigest()
    successor_after_sha = "sha256:" + hashlib.sha256(
        successor_after.encode("utf-8")
    ).hexdigest()
    if (
        predecessor_after != successor_before
            or reconciliation.get("before_sha256") != predecessor_after_sha
            or reconciliation.get("after_sha256") != successor_after_sha
            or reconciliation.get("before_sha256") != successor_before_sha
        or successor_repair.get("before_sha256") != successor_before_sha
        or successor_repair.get("after_sha256") != successor_after_sha
    ):
        raise CurrentTextContinuationError(
            "CURRENT_EXACT_FINAL_OWNER_SUPERSESSION_INVALID"
        )


def _continuation_pending_source_boundary(
    audit: Mapping[str, object], *, srt_text: str, candidate_id: str,
    source_clock_review: Mapping[str, object] | None = None,
) -> bool:
    """Recognize a bound boundary checkpoint, never a delivery verdict.

    The ordinary resolver replaces the text-stage placeholder before recut
    creates the final endpoint.  That intermediate review may PASS or BLOCK;
    its current-text and source-grid bindings must survive either result.
    Historical source-clock geometry is still validated by the caller.
    """
    boundary = audit.get("boundary_semantic_review")
    if boundary == {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "BLOCK", "reason_codes": ["CURRENT_TEXT_REQUIRES_SOURCE_REVIEW"],
    }:
        return True
    # Exact review can reach a real final-delivery endpoint before discovering
    # that its source-separation witness is still blocked.  Its current cue
    # geometry can resume review using a separately validated source clock;
    # neither this checkpoint nor that historical clock grants release.
    if (
        isinstance(boundary, Mapping)
        and boundary.get("schema_version") == "talk-boundary-semantic-review.v1"
        and boundary.get("status") == "BLOCK"
        and boundary.get("candidate_id") == candidate_id
        and boundary.get("review_scope") == "final_delivery"
    ):
        from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint

        endpoint = boundary.get("final_endpoint_binding")
        clock_boundary = (source_clock_review or {}).get("boundary_semantic_review")
        clock_endpoint = (
            clock_boundary.get("final_endpoint_binding")
            if isinstance(clock_boundary, Mapping) else None
        )
        request_sha = _continuation_normalize_sha(boundary.get("request_sha256"))
        if (
            not isinstance(endpoint, Mapping)
            or not isinstance(clock_endpoint, Mapping)
            or any(
                endpoint.get(key) != clock_endpoint.get(key)
                for key in ("final_start_ms", "final_end_ms")
            )
            or len(request_sha) != 64
            or any(c not in "0123456789abcdef" for c in request_sha)
            or any(
                isinstance(endpoint.get(key), bool)
                or not isinstance(endpoint.get(key), int)
                for key in ("final_start_ms", "final_end_ms")
            )
        ):
            return False
        cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
        if not cues or not 0 <= endpoint["final_start_ms"] < endpoint["final_end_ms"]:
            return False
        rebound, reasons = bind_final_semantic_endpoint(
            semantic_review=boundary, cues=cues, closure_cue=cues[-1],
            snapped_end_ms=cues[-1].end_ms,
            final_start_ms=endpoint["final_start_ms"],
            final_end_ms=endpoint["final_end_ms"],
        )
        return (
            reasons == ["BOUNDARY_SEMANTIC_REVIEW_NOT_PASS"]
            and rebound["final_endpoint_binding"] == endpoint
        )
    if (
        not isinstance(boundary, Mapping)
        or boundary.get("schema_version") != "talk-boundary-semantic-review.v1"
        or boundary.get("status") not in {"PASS", "BLOCK"}
        or boundary.get("final_endpoint_binding") is not None
        or boundary.get("candidate_id") != candidate_id
        or boundary.get("review_scope") != "source_full_window"
    ):
        return False
    for key in ("request_sha256", "cue_grid_sha256"):
        digest = _continuation_normalize_sha(boundary.get(key))
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return False
    text_evidence = boundary.get("current_delivery_text_evidence")
    projection = audit.get("current_delivery_source_projection")
    if not isinstance(text_evidence, Mapping) or not isinstance(projection, Mapping):
        return False
    projected_grid = _continuation_normalize_sha(
        projection.get("projected_source_cue_grid_sha256")
    )
    if len(projected_grid) != 64 or any(c not in "0123456789abcdef" for c in projected_grid):
        return False
    current_grid = cue_grid_sha256(parse_srt_cues(srt_text))
    return bool(
        projection.get("schema_version") == "delivery-to-source-timeline-projection.v1"
        and projection.get("status") == "PASS"
        and text_evidence.get("role") == "CURRENT_TEXT_PROPOSAL_NOT_FINAL_CLEAN"
        and text_evidence.get("delivery_cue_grid_sha256") == current_grid
        and projection.get("delivery_srt_cue_grid_sha256") == current_grid
        and text_evidence.get("source_cue_grid_sha256") == boundary.get("cue_grid_sha256")
        and text_evidence.get("projected_source_cue_grid_sha256")
        == projection.get("projected_source_cue_grid_sha256")
    )


def _continuation_validate_existing_authority(
    chat_authority: Mapping[str, Any],
    *,
    srt_text: str,
    srt_sha256: str,
    clip_context: Mapping[str, object],
    candidate_id: str,
    source_clock_review: Mapping[str, object] | None = None,
    source_clock_srt_sha256: str | None = None,
    allow_source_scope_input: bool = False,
) -> dict[str, Any]:
    """Check the normal final audit and typed rows without importing new schemas.

    Each item below is one named sub-check of :mod:`continuation_subchecks`.
    The producer runs them fail-fast (first error unchanged); the offline
    preflight runs the same list exhaustively and reports every item.
    """
    from src.autoslice.continuation_subchecks import Group
    from src.autoslice.exact_final_cpa_history import review_input_of_pending_run

    checks = Group("_continuation_validate_existing_authority")
    current_sha = "sha256:" + srt_sha256
    with checks.check("chat_authority.schema"):
        if chat_authority.get("schema_version") != "chat-authority-audit.v2":
            raise CurrentTextContinuationError("CURRENT_CHAT_AUTHORITY_SCHEMA_INVALID")
    with checks.check("final_srt.binding"):
        final_output_sha = chat_authority.get("final_output_srt_sha256")
        if final_output_sha is None:
            final_output_sha = chat_authority.get("final_text_srt_sha256")
        try:
            _continuation_require_sha(srt_sha256, final_output_sha, label="CURRENT_FINAL_SRT")
        except CurrentTextContinuationError as exc:
            if str(exc) != "CURRENT_FINAL_SRT_DRIFT":
                raise
            # A pending normal review can stop after a registered display-only
            # transition while its historical final-output pointer stays at the
            # preceding typed bytes. Replay the exact terminal transition; never
            # rewrite that pointer or borrow the old review's release verdict.
            receipt = chat_authority.get("exact_final_cpa_self_heal")
            if not isinstance(receipt, Mapping) or receipt.get("status") != "REVIEW_PENDING":
                raise
            try:
                terminal = caption_successors(receipt).get(len(receipt.get("passes", [])))
            except (TypeError, ValueError) as successor_exc:
                raise CurrentTextContinuationError("CURRENT_FINAL_SRT_DRIFT") from successor_exc
            if (
                terminal is None
                or _continuation_normalize_sha(terminal["input_srt_sha256"])
                != _continuation_normalize_sha(final_output_sha)
                or terminal["output_srt_sha256"] != current_sha
                or terminal["output_srt_text"] != srt_text
            ):
                raise
    with checks.check("chat_authority.mutation_flag"):
        if chat_authority.get("mutation_authorized") is True:
            raise CurrentTextContinuationError("CURRENT_CHAT_AUTHORITY_MUTATION_INVALID")
    final_review = chat_authority.get("final_review_audit")
    with checks.check("final_review.present"):
        if not isinstance(final_review, Mapping):
            raise CurrentTextContinuationError("CURRENT_FINAL_REVIEW_AUDIT_MISSING")
    preburn_pending = False
    boundary_pending = False
    mixed_pending = False
    postburn_clean_pending = False
    discovery_pending = False
    source_scope_input = False
    validated_final_review: Mapping[str, Any] = final_review if isinstance(final_review, Mapping) else {}
    if not isinstance(final_review, Mapping):
        for name in ("final_review.successor_binding", "final_review.release_or_pending",
                     "final_review.input_integrity_geometry"):
            checks.skip(name, "CURRENT_FINAL_REVIEW_AUDIT_MISSING")
        final_review = {}
    # The interrupted exact-final writer state: the review that produced the
    # terminal pass of a pending run stays bound to that pass's input bytes.
    # It is admitted only as review input for the actual active bytes (whose
    # chain is validated below); every review-internal binding uses its own
    # bytes, every active-byte binding uses the current bytes.
    review_sha = current_sha
    successor_review_pending = False
    def borrows_source_clock_geometry() -> bool:
        # One predicate for every admitted pending-input branch that reads
        # historical geometry from the source clock instead of the current review;
        # the flags below are final by the time any caller evaluates it.
        return boundary_pending or mixed_pending or discovery_pending or source_scope_input

    if final_review:
        with checks.check("final_review.successor_binding"):
            reviewed = final_review.get("reviewed_srt_sha256")
            lag_input = review_input_of_pending_run(chat_authority.get("exact_final_cpa_self_heal"))
            if reviewed != current_sha and lag_input is not None and reviewed == lag_input:
                review_sha = lag_input
                successor_review_pending = True
    if final_review:
        with checks.check("final_review.release_or_pending"):
            try:
                validated_final_review = validate_final_review_release(
                    final_review,
                    expected_srt_sha256=review_sha,
                )
                if successor_review_pending:
                    # A released review of predecessor bytes is never a
                    # release (or completed review) of the active bytes.
                    raise CurrentTextContinuationError("CURRENT_FINAL_REVIEW_SUCCESSOR_NOT_PENDING")
            except FinalReviewContractError as exc:
                boundary = final_review.get('boundary_semantic_review') or {}
                context = final_review.get('source_boundary_context') or {}
                # Some historical checkpoints completed source-window review while
                # retaining the delivery proposal. Resume only that input scope using
                # a separately validated clock; the original still fails final release.
                source_scope_input = bool(
                    allow_source_scope_input
                    and not successor_review_pending
                    and exc.reason_code == 'FINAL_REVIEW_BOUNDARY_ENDPOINT_BINDING_INVALID'
                    and final_review.get('status') == 'CLEAN' and final_review.get('release_gate') == 'PASS'
                    and final_review.get('findings') == [] and final_review.get('validated_finding_count') == 0
                    and boundary.get('status') == 'PASS' and boundary.get('review_scope') == 'source_full_window'
                    and isinstance(source_clock_review, Mapping)
                    and context.get('schema_version') == 'source-boundary-transcript-context.v1'
                    and context.get('status') == 'PASS'
                    and context.get('source_witness_request_sha256') == boundary.get('request_sha256')
                    and context.get('source_cue_grid_sha256') == boundary.get('cue_grid_sha256')
                    and _continuation_pending_source_boundary(final_review, srt_text=srt_text,
                        candidate_id=candidate_id, source_clock_review=source_clock_review))
                if source_scope_input:
                    source_path = Path(str(context.get('source_srt_path') or ''))
                    source_sha = _continuation_sha256_path(source_path, label='CURRENT_SOURCE_REVIEW_INPUT')
                    _continuation_require_sha(source_sha, context.get('source_srt_sha256'), label='CURRENT_SOURCE_REVIEW_INPUT')
                    if cue_grid_sha256(parse_srt_cues(source_path.read_text(encoding='utf-8'))) != boundary.get('cue_grid_sha256'):
                        raise CurrentTextContinuationError('CURRENT_SOURCE_BOUNDARY_GRID_DRIFT')
                # Completed, byte-bound pending evidence can resume ordinary exact
                # review after a parser failure or unresolved semantic finding.
                # Release and input validation are separate; retain the original verdict.
                postburn = chat_authority.get("postburn_subtitle_audio_review")
                parser_pending = (
                    not successor_review_pending
                    and exc.reason_code == "FINAL_REVIEW_RELEASE_GATE_BLOCKED"
                    and final_review.get("status") == "AUDITOR_UNAVAILABLE"
                    and final_review.get("reason_codes")
                    == ["POSTBURN_SUBTITLE_AUDIO_CPA_UNAVAILABLE"]
                    and isinstance(postburn, Mapping)
                    and postburn.get("status") == "AUDITOR_UNAVAILABLE"
                    and postburn.get("error_type") == "FinalReviewAuditError"
                )
                semantic_pending = (
                    not successor_review_pending
                    and is_finding_failure(exc.reason_code)
                    and final_review.get("status") == "FLAGGED"
                    and only_finding_failures(final_review.get("reason_codes"))
                    and isinstance(postburn, Mapping)
                    and postburn.get("status") == "FLAGGED"
                )
                # Normal exact review can reopen a semantic/acoustic finding after
                # the same-byte postburn audit was clean. That clean audit is only a
                # completed input checkpoint; it cannot settle the newer finding.
                postburn_clean_pending = (
                    not successor_review_pending
                    and is_finding_failure(exc.reason_code)
                    and final_review.get("status") == "FLAGGED"
                    and only_finding_failures(final_review.get("reason_codes"))
                    and isinstance(postburn, Mapping)
                    and postburn.get("status") == "CLEAN"
                )
                findings = final_review.get("findings")
                exact_self_heal = chat_authority.get("exact_final_cpa_self_heal")
                # Input recovery can retry an unavailable discovery stage. Preserve
                # its actual BLOCK; a separately validated original clock and the
                # complete typed text chain below remain mandatory. Release callers
                # continue to require COMPLETE discovery without this input exception.
                discovery_pending = (
                    not successor_review_pending
                    and exc.reason_code == "FINAL_REVIEW_DISCOVERY_INCOMPLETE"
                    and unavailable_review_stage(final_review)
                    and isinstance(source_clock_review, Mapping)
                    and _continuation_pending_source_boundary(
                        final_review, srt_text=srt_text, candidate_id=candidate_id,
                        source_clock_review=source_clock_review,
                    )
                )
                # A normal postburn self-heal may leave its original receipt attached
                # after changing the SRT.  It stays historical evidence, never current
                # discovery or release authority.  Resume only when the existing typed
                # chain connects those old bytes to the current pending review; the
                # full chain is still validated below.
                passes = exact_self_heal.get("passes") if isinstance(exact_self_heal, Mapping) else None
                prior_postburn = bool(
                    isinstance(postburn, Mapping)
                    and postburn == final_review.get("postburn_subtitle_audio_review")
                    and postburn.get("schema_version") == "postburn-subtitle-audio-review.v1"
                    and postburn.get("status") == "FLAGGED"
                    and postburn.get("release_gate") == "BLOCK"
                    and isinstance(postburn.get("reviewed_srt_sha256"), str)
                    and postburn.get("reviewed_srt_sha256") != current_sha
                    and isinstance(passes, list)
                    and any(
                        isinstance(row, Mapping)
                        and row.get("input_srt_sha256") == postburn.get("reviewed_srt_sha256")
                        for row in passes
                    )
                )
                preburn_pending = (
                    is_finding_failure(exc.reason_code)
                    and final_review.get("status") == "FLAGGED"
                    and final_review.get("release_gate") == "BLOCK"
                    and only_finding_failures(final_review.get("reason_codes"))
                    and isinstance(findings, list)
                    and bool(findings)
                    and final_review.get("validated_finding_count") == len(findings)
                    and (
                        prior_postburn
                        or (
                            "postburn_subtitle_audio_review" not in chat_authority
                            and "postburn_subtitle_audio_review" not in final_review
                        )
                    )
                    and isinstance(exact_self_heal, Mapping)
                    and exact_self_heal.get("status") == "REVIEW_PENDING"
                )
                pending_text = final_review.get("pre_boundary_text_recovery")
                boundary_pending = (
                    not successor_review_pending
                    and exc.reason_code in {
                        "FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED",
                        "FINAL_REVIEW_BOUNDARY_ENDPOINT_BINDING_INVALID",
                    }
                    and final_review.get("status") == "FLAGGED"
                    and final_review.get("reason_codes") == ["FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"]
                    and (
                        (
                            isinstance(pending_text, Mapping)
                            and pending_text.get("status") == "TEXT_COMPLETE_BOUNDARY_PENDING"
                            and pending_text.get("release_gate") == "BLOCK"
                            and pending_text.get("reviewed_srt_sha256") == review_sha
                        )
                        or (
                            # Ordinary exact review may finish text repairs before
                            # rejecting its source boundary. It does not emit the
                            # pre-boundary-only marker; its byte-bound pending typed
                            # chain is validated below instead. This resumes input
                            # evidence and keeps the existing release BLOCK intact.
                            pending_text is None
                            and isinstance(exact_self_heal, Mapping)
                            and exact_self_heal.get("status") == "REVIEW_PENDING"
                        )
                    )
                    and _continuation_pending_source_boundary(
                        final_review, srt_text=srt_text, candidate_id=candidate_id,
                        source_clock_review=source_clock_review,
                    )
                    and final_review.get("findings") == []
                    and final_review.get("validated_finding_count") == 0
                    and isinstance(source_clock_review, Mapping)
                )
                # A real CURRENT checkpoint may carry both the unresolved text
                # findings and the later source-boundary BLOCK.  Keep that checkpoint
                # as-is, but only resume when a separately bound source-clock review
                # supplies the geometry for the current bytes.  The exact text
                # recovery receipt is optional here because the mixed checkpoint's
                # findings are already the retained text-stage receipt; if present,
                # it must still describe the same current bytes.
                mixed_pending_text = final_review.get("pre_boundary_text_recovery")
                mixed_pending = (
                    is_finding_failure(exc.reason_code)
                    and final_review.get("status") == "FLAGGED"
                    and final_review.get("release_gate") == "BLOCK"
                    and isinstance(final_review.get("reason_codes"), list)
                    and all(isinstance(code, str) for code in final_review.get("reason_codes"))
                    and "FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED" in final_review.get("reason_codes")
                    and only_finding_failures([code for code in final_review.get("reason_codes")
                                              if code != "FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"])
                    and isinstance(findings, list)
                    and bool(findings)
                    and final_review.get("validated_finding_count") == len(findings)
                    and _continuation_pending_source_boundary(
                        final_review, srt_text=srt_text, candidate_id=candidate_id,
                        source_clock_review=source_clock_review,
                    )
                    and (
                        mixed_pending_text is None
                        or (
                            isinstance(mixed_pending_text, Mapping)
                            and mixed_pending_text.get("status") == "TEXT_COMPLETE_BOUNDARY_PENDING"
                            and mixed_pending_text.get("release_gate") == "BLOCK"
                            and mixed_pending_text.get("reviewed_srt_sha256")
                            == review_sha
                        )
                    )
                    and (
                        prior_postburn
                        or (
                            "postburn_subtitle_audio_review" not in chat_authority
                            and "postburn_subtitle_audio_review" not in final_review
                        )
                    )
                    and isinstance(source_clock_review, Mapping)
                    and isinstance(exact_self_heal, Mapping)
                    and exact_self_heal.get("status") == "REVIEW_PENDING"
                )
                if (
                    not (
                        parser_pending
                        or semantic_pending
                        or preburn_pending
                        or boundary_pending
                        or mixed_pending
                        or postburn_clean_pending
                        or discovery_pending
                        or source_scope_input
                    )
                    or (final_review.get("release_gate") != "BLOCK" and not source_scope_input)
                ):
                    raise CurrentTextContinuationError(exc.reason_code) from exc
                if not (preburn_pending or boundary_pending or mixed_pending or discovery_pending or source_scope_input):
                    if (
                        postburn != final_review.get("postburn_subtitle_audio_review")
                        or validated_postburn_priority_findings(
                            final_srt_text=srt_text,
                            verified_authority_audit=chat_authority,
                            candidate_id=candidate_id,
                            clip_context=clip_context,
                            allow_parser_recovery=parser_pending,
                            allow_clean_checkpoint=postburn_clean_pending,
                        ) is None
                    ):
                        raise CurrentTextContinuationError(exc.reason_code) from exc
                validated_final_review = final_review
        with checks.check("final_review.input_integrity_geometry"):
            if validated_final_review is final_review and (
                preburn_pending or boundary_pending or mixed_pending or postburn_clean_pending
                or discovery_pending or source_scope_input
                or final_review.get("release_gate") == "BLOCK"
            ):
                # Unresolved findings stop release before its geometry checks.  They
                # may resume as ordinary review input only after all existing input
                # integrity/geometry checks pass.  The original BLOCK and findings
                # remain intact; no proposal or prior CLEAN is promoted here.
                clock_geometry = borrows_source_clock_geometry()
                try:
                    if discovery_pending:
                        from src.autoslice.producer_text_checkpoint import validate_incomplete_review_input

                        validate_incomplete_review_input(final_review, expected_srt_sha256=review_sha)
                    else:
                        _validate_review_integrity(final_review, expected_srt_sha256=review_sha)
                    disclosed = final_review.get("unresolved_findings_disclosed")
                    if disclosed is not None and (
                        not isinstance(disclosed, list)
                        or any(not is_keep_current_disclosed(row) for row in disclosed)
                    ):
                        raise FinalReviewContractError("FINAL_REVIEW_FINDINGS_CONTRACT_INVALID")
                    _validate_review_geometry(
                        source_clock_review if clock_geometry else final_review,
                        expected_srt_sha256=(source_clock_srt_sha256 or current_sha)
                        if clock_geometry else review_sha,
                    )
                except FinalReviewContractError as input_exc:
                    raise CurrentTextContinuationError(input_exc.reason_code) from input_exc

    # ``correction_pass`` is historical provenance.  The contract validator
    # checks its mutation/carryover integrity; its findings remain bound to
    # their original base transcript and are not re-bound to final delivery.
    with checks.check("self_heal.chain"):
        try:
            _validate_exact_final_cpa_self_heal(
                chat_authority,
                expected_srt_sha256=current_sha,
                allow_pending=preburn_pending or boundary_pending or mixed_pending or postburn_clean_pending or discovery_pending,
            )
        except FinalReviewContractError as exc:
            raise CurrentTextContinuationError(exc.reason_code) from exc

    def owner_projection_review() -> Mapping[str, Any] | None:
        # Only the admitted pending-input branches borrow historical geometry.
        # The current review still determines scope and the returned verdict.
        if borrows_source_clock_geometry():
            return source_clock_review
        return validated_final_review

    entity_rows = chat_authority.get("entity_repairs") or []
    with checks.check("entity_repairs.ledger"):
        if not isinstance(entity_rows, list):
            raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIRS_INVALID")
    for row_index, row in enumerate(entity_rows if isinstance(entity_rows, list) else []):
        mode = row.get("mode") if isinstance(row, Mapping) else None
        with checks.check(f"entity_repairs[{row_index}]:{mode}"):
            if not isinstance(row, Mapping):
                raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_INVALID")
            judge = row.get("judge")
            if (
                row.get("mode") == "exact_final_cpa_self_heal"
                and isinstance(judge, Mapping)
                and judge.get("schema_version") in {
                    "exact-final-cpa-cycle-judge.v1", "exact-final-cpa-convergence-judge.v1",
                    "editorial-subtitle-judge.v1",
                }
            ):
                _continuation_validate_exact_final_owner(
                    row, row_index=row_index, chat_authority=chat_authority,
                    source_clock_review=owner_projection_review(),
                )
            elif row.get("mode") in {
                "final_foreign_script_cpa_adjudication", "final_source_language_cpa_adjudication",
            }:
                _continuation_validate_final_language_owner(
                    row, row_index=row_index, chat_authority=chat_authority,
                    srt_text=srt_text, final_review=validated_final_review,
                    projection_review=owner_projection_review(),
                )
            else:
                _continuation_validate_existing_repair(
                    row, srt_text=srt_text, clip_context=clip_context,
                )
    checks.finish()
    return dict(validated_final_review)


def _continuation_source_clock_reference(
    continuation: CurrentTextContinuation,
    *,
    chat_authority: Mapping[str, object],
    srt_text: str,
    srt_sha256: str,
    clip_context: Mapping[str, object],
    candidate_id: str,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Read a normal clock; typed text-only successors still need fresh review."""
    path = continuation.source_clock_authority_path
    expected = continuation.expected_source_clock_authority_sha256
    if path is None and expected is None:
        return None, None
    if path is None or expected is None:
        raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_ARTIFACT_PAIR_INVALID")
    reference, digest = _continuation_read_json(path, label="CURRENT_SOURCE_CLOCK", review=True)
    _continuation_require_sha(digest, expected, label="CURRENT_SOURCE_CLOCK")
    reference_text, reference_sha = srt_text, srt_sha256
    reference_path = getattr(continuation, "source_clock_srt_path", None)
    expected_reference_sha = getattr(continuation, "expected_source_clock_srt_sha256", None)
    if (reference_path is None) != (expected_reference_sha is None):
        raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_SRT_PAIR_INVALID")
    if reference_path is not None:
        reference_sha = _continuation_sha256_path(reference_path, label="CURRENT_SOURCE_CLOCK_SRT")
        _continuation_require_sha(reference_sha, expected_reference_sha, label="CURRENT_SOURCE_CLOCK_SRT")
        reference_text = reference_path.read_text(encoding="utf-8")
        if hashlib.sha256(reference_text.encode("utf-8")).hexdigest() != reference_sha:
            raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_SRT_DRIFT")
    review = _continuation_validate_existing_authority(
        reference, srt_text=reference_text, srt_sha256=reference_sha,
        clip_context=clip_context, candidate_id=candidate_id,
    )
    try:
        _validate_review_geometry(review, expected_srt_sha256="sha256:" + reference_sha)
    except FinalReviewContractError as exc:
        raise CurrentTextContinuationError(exc.reason_code) from exc
    boundary = review["boundary_semantic_review"]
    if boundary.get("candidate_id") != candidate_id:
        raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_CANDIDATE_DRIFT")
    # These source/owner records survive normal text recovery. Require the
    # reference to be from that same producer chain, not a similarly timed clip.
    for field in ("post_transcript_entity_output_srt_sha256", "frozen_boundary_owner_contract"):
        if not reference.get(field) or reference.get(field) != chat_authority.get(field):
            raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_SOURCE_OR_OWNER_DRIFT")
    if reference_sha != srt_sha256:
        _continuation_replay_clock_text_chain(
            chat_authority, reference_text=reference_text,
            reference_sha=reference_sha, current_text=srt_text, current_sha=srt_sha256,
        )
    return review, {
        "authority_path": str(path), "authority_sha256": "sha256:" + digest,
        "reviewed_srt_sha256": "sha256:" + reference_sha,
        "current_srt_sha256": "sha256:" + srt_sha256,
        "reference_srt_path": str(reference_path) if reference_path is not None else None,
        "role": "EVIDENCE_ONLY_SOURCE_CLOCK", "semantic_review_required": True,
        "historical_release_gate": review.get("release_gate"),
    }


def _continuation_replay_clock_text_chain(
    chat_authority: Mapping[str, object], *, reference_text: str,
    reference_sha: str, current_text: str, current_sha: str,
) -> None:
    """Recompute the normal typed suffix without modifying either artifact."""
    try:
        _validate_exact_final_cpa_self_heal(
            chat_authority, expected_srt_sha256="sha256:" + current_sha, allow_pending=True,
        )
    except FinalReviewContractError as exc:
        raise CurrentTextContinuationError(exc.reason_code) from exc
    receipt = chat_authority.get("exact_final_cpa_self_heal")
    passes = receipt.get("passes") if isinstance(receipt, Mapping) else None
    starts = [i for i, row in enumerate(passes or [])
              if row.get("input_srt_sha256") == "sha256:" + reference_sha]
    if len(starts) != 1:
        raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_TEXT_CHAIN_MISSING")
    original = parse_srt_cues(reference_text)
    cues = list(original)
    text = reference_text
    display_successors = caption_successors(receipt)
    for row in passes[starts[0]:]:
        if row["input_srt_sha256"] != "sha256:" + hashlib.sha256(text.encode()).hexdigest():
            raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_TEXT_CHAIN_DRIFT")
        for repair in row["repairs"]:
            position = repair["cue_index"] - 1
            if not 0 <= position < len(cues):
                raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_TEXT_CHAIN_DRIFT")
            cue = cues[position]
            if (repair["before"] != cue.text or not repair["after"].strip()
                    or repair.get("matched_start_ms") != cue.start_ms
                    or repair.get("matched_end_ms") != cue.end_ms):
                raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_TEXT_CHAIN_DRIFT")
            cues[position] = type(cue)(index=cue.index, start_ms=cue.start_ms,
                                       end_ms=cue.end_ms, text=repair["after"])
        text = _render_cues_to_srt(cues)
        if row["output_srt_sha256"] != "sha256:" + hashlib.sha256(text.encode()).hexdigest():
            raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_TEXT_CHAIN_DRIFT")
        successor = display_successors.get(row["pass_index"])
        if successor is not None:
            if text != successor["input_srt_text"]:
                raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_TEXT_CHAIN_DRIFT")
            text = successor["output_srt_text"]
            cues = list(parse_srt_cues(text))
    if text != current_text or hashlib.sha256(text.encode()).hexdigest() != current_sha:
        raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_TEXT_CHAIN_DRIFT")
    current_cues = parse_srt_cues(current_text)
    if len(original) != len(current_cues) or any(
        (old.index, old.start_ms, old.end_ms) != (new.index, new.start_ms, new.end_ms)
        for old, new in zip(original, current_cues)
    ):
        raise CurrentTextContinuationError("CURRENT_TEXT_REPAIR_TIMING_DRIFT")


def _continuation_review_summary(review: Mapping[str, object] | None) -> dict[str, object] | None:
    """Retain only the old review binding needed by current-surface audit."""

    if not isinstance(review, Mapping):
        return None
    return {
        field: review.get(field)
        for field in ("schema_version", "status", "release_gate", "reviewed_srt_sha256")
    }


def _continuation_chat_from_context(context: Mapping[str, object]) -> tuple[ChatEvidence, ...]:
    rows = context.get("structured_chat")
    if not isinstance(rows, list):
        return ()
    result: list[ChatEvidence] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        offset = row.get("offset_ms")
        text = row.get("text")
        if isinstance(offset, bool) or not isinstance(offset, int) or not isinstance(text, str) or not text:
            continue
        result.append(
            ChatEvidence(
                str(row.get("kind") or "danmaku"),
                offset,
                text,
                str(row.get("sender") or ""),
                str(row.get("source") or ""),
                str(row.get("source_sha256") or ""),
                str(row.get("source_event_id") or ""),
            )
        )
    return tuple(result)


def _continuation_project_delivery_to_source(
    cues: Sequence[object],
    *,
    final_review_audit: Mapping[str, object],
    reference_cues: Sequence[object] | None = None,
    authority: Mapping[str, object] | None = None,
) -> tuple[list[SrtCue], dict[str, object]]:
    """Bind the current delivery grid to the padded source timeline.

    The final SRT is zero-based delivery text.  The terminal source witness in
    the existing final-review audit is the only authority for the source
    offset; deriving a shifted grid from that witness keeps current text and
    source-domain owner geometry separate.  This does not turn the projected
    rows into a new audio witness.
    """

    boundary = final_review_audit.get("boundary_semantic_review")
    if not isinstance(boundary, Mapping):
        raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_BOUNDARY_MISSING")
    endpoint = boundary.get("final_endpoint_binding")
    witness = boundary.get("source_separation_witness")
    if not isinstance(endpoint, Mapping) or not isinstance(witness, Mapping):
        raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_BINDING_MISSING")
    if endpoint.get("status") != "PASS" or witness.get("status") != "PASS":
        raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_BINDING_NOT_PASS")
    delivery_grid = cue_grid_sha256(cues)
    reference_grid = cue_grid_sha256(reference_cues) if reference_cues is not None else delivery_grid
    if authority is not None and endpoint.get("final_cue_grid_sha256") != reference_grid:
        # The review may cover the input of the run's pending typed suffix
        # (the interrupted exact-final writer state). Its geometry belongs to
        # those reviewed bytes; reconstruct them from the same typed chain.
        reviewed_cues = reviewed_reference_cues(
            authority, final_review_audit=final_review_audit, cues=cues)
        if reviewed_cues is not None:
            reference_cues = reviewed_cues
            reference_grid = cue_grid_sha256(reviewed_cues)
    if reference_cues is not None and (
        len(cues) != len(reference_cues)
        or any(
            (getattr(current, "index", None), current.start_ms, current.end_ms)
            != (getattr(original, "index", None), original.start_ms, original.end_ms)
            for current, original in zip(cues, reference_cues)
        )
    ):
        raise CurrentTextContinuationError("CURRENT_TEXT_REPAIR_TIMING_DRIFT")
    if (
        endpoint.get("semantic_cue_grid_sha256") != reference_grid
        or endpoint.get("final_cue_grid_sha256") != reference_grid
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_DELIVERY_GRID_DRIFT")

    final_start = endpoint.get("final_start_ms")
    final_end = endpoint.get("final_end_ms")
    source_start = witness.get("source_final_start_ms")
    source_end = witness.get("source_final_end_ms")
    ints = (final_start, final_end, source_start, source_end)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in ints):
        raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_INTERVAL_INVALID")
    if final_end <= final_start or source_end <= source_start:
        raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_INTERVAL_INVALID")
    if source_end - source_start != final_end - final_start:
        raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_DURATION_DRIFT")
    offset = source_start - final_start
    projected: list[SrtCue] = []
    rows: list[dict[str, object]] = []
    for cue in cues:
        delivery_cue_start = getattr(cue, "start_ms", None)
        delivery_cue_end = getattr(cue, "end_ms", None)
        cue_index = str(getattr(cue, "index", ""))
        text = str(getattr(cue, "text", "") or "")
        if (
            isinstance(delivery_cue_start, bool)
            or not isinstance(delivery_cue_start, int)
            or isinstance(delivery_cue_end, bool)
            or not isinstance(delivery_cue_end, int)
            or delivery_cue_start < final_start
            or delivery_cue_end > final_end
            or delivery_cue_end <= delivery_cue_start
            or not cue_index
            or not text
        ):
            raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_CUE_INVALID")
        source_cue_start = delivery_cue_start + offset
        source_cue_end = delivery_cue_end + offset
        if source_cue_end <= source_cue_start or source_cue_start < source_start or source_cue_end > source_end:
            raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_CUE_OUT_OF_RANGE")
        projected.append(
            SrtCue(
                index=cue_index,
                start_ms=source_cue_start,
                end_ms=source_cue_end,
                text=text,
            )
        )
        rows.append(
            {
                "cue_index": cue_index,
                "delivery_start_ms": delivery_cue_start,
                "delivery_end_ms": delivery_cue_end,
                "source_start_ms": source_cue_start,
                "source_end_ms": source_cue_end,
                "text_sha256": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    if not projected:
        raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_EMPTY")
    return projected, {
        "schema_version": "delivery-to-source-timeline-projection.v1",
        "status": "PASS",
        "delivery_srt_role": "CURRENT_FINAL_DELIVERY_TEXT",
        "source_srt_role": "PADDED_SOURCE_DOMAIN_COORDINATES",
        "delivery_final_start_ms": final_start,
        "delivery_final_end_ms": final_end,
        "source_final_start_ms": source_start,
        "source_final_end_ms": source_end,
        "offset_ms": offset,
        "delivery_srt_cue_grid_sha256": delivery_grid,
        "projected_source_cue_grid_sha256": cue_grid_sha256(projected),
        "source_witness_request_sha256": witness.get("source_request_sha256"),
        "source_witness_review_sha256": witness.get("source_review_sha256"),
        "cue_count": len(rows),
        "delivery_first_start_ms": rows[0]["delivery_start_ms"],
        "delivery_last_end_ms": rows[-1]["delivery_end_ms"],
        "source_first_start_ms": rows[0]["source_start_ms"],
        "source_last_end_ms": rows[-1]["source_end_ms"],
        "rows": rows,
    }


def _continuation_source_context_draft_binding(
    continuation: CurrentTextContinuation,
    *,
    spec: Mapping[str, object],
    candidate_id: str,
    padded_dur: int,
    source_media_sha256s: tuple[str, ...],
) -> dict[str, object] | None:
    """Bind an executor-produced expanded draft as context for a fresh review.

    This is not a source-separation witness or final subtitle authority.  The
    normal executor's existing job/result bind the source clock and draft;
    the subsequent ordinary source review must still decide the boundary.
    """
    values = (
        continuation.source_context_job_path,
        continuation.expected_source_context_job_sha256,
        continuation.source_context_manifest_path,
        continuation.expected_source_context_manifest_sha256,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values) or continuation.source_boundary_srt_path is None:
        raise CurrentTextContinuationError("CURRENT_SOURCE_CONTEXT_ARTIFACT_PAIR_INVALID")
    job, job_sha = _continuation_read_json(
        Path(continuation.source_context_job_path), label="CURRENT_SOURCE_CONTEXT_JOB"
    )
    manifest, manifest_sha = _continuation_read_json(
        Path(continuation.source_context_manifest_path), label="CURRENT_SOURCE_CONTEXT_MANIFEST"
    )
    _continuation_require_sha(job_sha, continuation.expected_source_context_job_sha256, label="CURRENT_SOURCE_CONTEXT_JOB")
    _continuation_require_sha(manifest_sha, continuation.expected_source_context_manifest_sha256, label="CURRENT_SOURCE_CONTEXT_MANIFEST")
    pieces = spec.get("pieces")
    timeline = job.get("timeline")
    source_input = job.get("input")
    provenance = job.get("provenance")
    if (
        job.get("schema_version") != "source-context-jingting-job.v1"
        or job.get("job_kind") != "SOURCE_CONTEXT_JINGTING"
        or job.get("candidate_id") != candidate_id
        or not isinstance(job.get("job_id"), str)
        or not job["job_id"]
        or manifest.get("schema_version") != "jingting-source-context-result.v1"
        or manifest.get("job_id") != job["job_id"]
        or manifest.get("provider") != "source_draft_context"
        or manifest.get("refinement_required") is not False
        or manifest.get("agy_rc") != 0
        or manifest.get("provider_fallback_used") is not False
        or not isinstance(pieces, list)
        or len(pieces) != 1
        or not isinstance(pieces[0], Mapping)
        or not isinstance(timeline, Mapping)
        or not isinstance(source_input, Mapping)
        or not isinstance(provenance, Mapping)
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_CONTEXT_DRAFT_INVALID")
    piece = pieces[0]
    start, end = piece.get("start_ms"), piece.get("end_ms")
    if (
        isinstance(start, bool) or not isinstance(start, int)
        or isinstance(end, bool) or not isinstance(end, int)
        or start < 0 or end - start != padded_dur
        or timeline.get("context_start_ms") != start
        or timeline.get("context_end_ms") != end
        or timeline.get("context_duration_ms") != padded_dur
        or source_input.get("duration_ms") != padded_dur
        or any(value != start for value in (
            job.get("source_offset_ms"), source_input.get("source_offset_ms"),
            manifest.get("source_offset_ms"),
        ))
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_CONTEXT_CLOCK_DRIFT")
    source_sha = _continuation_normalize_sha(manifest.get("source_sha256"))
    if (
        source_sha not in {_continuation_normalize_sha(value) for value in source_media_sha256s}
        or any(_continuation_normalize_sha(value) != source_sha for value in (
            piece.get("source_media_sha256"), source_input.get("source_sha256"),
            provenance.get("source_sha256"),
        ))
        or piece.get("remote_media") != source_input.get("source_uri")
        or provenance.get("source_uri") != source_input.get("source_uri")
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_CONTEXT_SOURCE_DRIFT")
    draft_sha = _continuation_sha256_path(continuation.source_boundary_srt_path, label="CURRENT_SOURCE_CONTEXT_DRAFT")
    for declared in (continuation.expected_source_boundary_srt_sha256, manifest.get("input_sha256"), manifest.get("output_sha256")):
        _continuation_require_sha(draft_sha, declared, label="CURRENT_SOURCE_CONTEXT_DRAFT")
    return {
        "role": "EVIDENCE_ONLY_EXPANDED_SOURCE_DRAFT_REQUIRES_FRESH_BOUNDARY_REVIEW",
        "job_path": str(continuation.source_context_job_path),
        "job_sha256": "sha256:" + job_sha,
        "manifest_path": str(continuation.source_context_manifest_path),
        "manifest_sha256": "sha256:" + manifest_sha,
        "source_media_sha256": "sha256:" + source_sha,
        "source_offset_ms": start,
        "source_context_duration_ms": padded_dur,
        "draft_srt_sha256": "sha256:" + draft_sha,
        "prior_boundary_pass_inherited": False,
    }


def _continuation_source_boundary_cues(
    *,
    chat_authority: Mapping[str, object],
    clip_context: Mapping[str, object],
    final_review_audit: Mapping[str, object],
    padded_dur: int,
    source_boundary_srt_path: Path | None = None,
    expected_source_boundary_srt_sha256: str | None = None,
    source_context_binding: Mapping[str, object] | None = None,
) -> tuple[list[SrtCue], dict[str, object]]:
    """Load the existing padded-domain transcript used by source review.

    ``whole_clip_draft_srt`` is retained as historical context.  The source
    boundary consumer uses the normal post-transcript source-domain SRT or an
    explicitly bound artifact. An executor-produced expanded draft remains
    evidence for a fresh review; the old witness validates only its old grid.
    None is substituted with the current zero-based delivery SRT.
    """

    historical_raw = clip_context.get("whole_clip_draft_srt")
    historical_declared = clip_context.get("whole_clip_draft_srt_sha256")
    if not isinstance(historical_raw, str) or not historical_raw:
        raise CurrentTextContinuationError("CURRENT_SOURCE_HISTORY_SRT_MISSING")
    historical_sha = "sha256:" + hashlib.sha256(historical_raw.encode("utf-8")).hexdigest()
    _continuation_require_sha(historical_sha.removeprefix("sha256:"), historical_declared, label="CURRENT_SOURCE_HISTORY_SRT")
    historical_cues = [cue for cue in parse_srt_cues(historical_raw) if cue.text.strip()]
    if not historical_cues or any(
        cue.start_ms < 0 or cue.end_ms > padded_dur or cue.end_ms <= cue.start_ms
        for cue in historical_cues
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_HISTORY_SRT_INVALID")

    explicit_source = source_boundary_srt_path is not None or expected_source_boundary_srt_sha256 is not None
    if explicit_source and (
        source_boundary_srt_path is None
        or expected_source_boundary_srt_sha256 is None
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_ARTIFACT_PAIR_INVALID")
    source_path: Path | None = None
    if explicit_source:
        source_path = Path(source_boundary_srt_path)
        source_digest = _continuation_sha256_path(
            source_path, label="CURRENT_SOURCE_BOUNDARY_ARTIFACT"
        )
        try:
            source_bytes = source_path.read_bytes()
            source_raw = source_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_BOUNDARY_ARTIFACT_UNREADABLE"
            ) from exc
        if hashlib.sha256(source_bytes).hexdigest() != source_digest:
            raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_ARTIFACT_DRIFT")
        _continuation_require_sha(
            source_digest,
            expected_source_boundary_srt_sha256,
            label="CURRENT_SOURCE_BOUNDARY_ARTIFACT",
        )
        source_sha = "sha256:" + source_digest
        source_role = "EXPLICIT_SOURCE_BOUNDARY_ARTIFACT"
    else:
        source_raw = chat_authority.get("post_transcript_entity_output_srt")
        source_declared = chat_authority.get("post_transcript_entity_output_srt_sha256")
        if not isinstance(source_raw, str) or not source_raw:
            raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_SRT_MISSING")
        source_sha = "sha256:" + hashlib.sha256(source_raw.encode("utf-8")).hexdigest()
        _continuation_require_sha(source_sha.removeprefix("sha256:"), source_declared, label="CURRENT_SOURCE_BOUNDARY_SRT")
        source_role = "CHAT_AUTHORITY_POST_TRANSCRIPT_ENTITY_OUTPUT"
    source_cues = [cue for cue in parse_srt_cues(source_raw) if cue.text.strip()]
    if not source_cues or any(
        cue.start_ms < 0 or cue.end_ms > padded_dur or cue.end_ms <= cue.start_ms
        for cue in source_cues
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_SRT_INVALID")
    boundary = final_review_audit.get("boundary_semantic_review")
    witness = boundary.get("source_separation_witness") if isinstance(boundary, Mapping) else None
    witness_cues = source_cues
    if source_context_binding is not None:
        if not explicit_source or source_context_binding.get("draft_srt_sha256") != source_sha:
            raise CurrentTextContinuationError("CURRENT_SOURCE_CONTEXT_DRAFT_DRIFT")
        # The old witness validates only its old grid.  A new executor draft
        # may supply missing post-end context, but cannot inherit that PASS.
        old_source = chat_authority.get("post_transcript_entity_output_srt")
        if not isinstance(old_source, str) or not old_source:
            raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_SRT_MISSING")
        _continuation_require_sha(
            hashlib.sha256(old_source.encode()).hexdigest(),
            chat_authority.get("post_transcript_entity_output_srt_sha256"),
            label="CURRENT_SOURCE_BOUNDARY_SRT",
        )
        witness_cues = [cue for cue in parse_srt_cues(old_source) if cue.text.strip()]
        source_role = "EXECUTOR_EXPANDED_SOURCE_DRAFT_EVIDENCE_ONLY"
    if not isinstance(witness, Mapping) or witness.get("source_cue_grid_sha256") != cue_grid_sha256(witness_cues):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_GRID_DRIFT")
    return source_cues, {
        "schema_version": "source-boundary-transcript-context.v1",
        "status": "PASS",
        "role": "SOURCE_DOMAIN_BOUNDARY_REVIEW_CONTEXT",
        "source_srt_role": source_role,
        "source_srt_path": str(source_path) if source_path is not None else None,
        "source_srt_sha256": source_sha,
        "source_cue_grid_sha256": cue_grid_sha256(source_cues),
        "source_cue_count": len(source_cues),
        "source_first_start_ms": source_cues[0].start_ms,
        "source_last_end_ms": source_cues[-1].end_ms,
        "source_witness_request_sha256": witness.get("source_request_sha256"),
        "source_witness_review_sha256": witness.get("source_review_sha256"),
        "historical_clip_context_srt_sha256": historical_sha,
        "historical_clip_context_cue_count": len(historical_cues),
        "historical_role": "EVIDENCE_ONLY_PREVIOUS_TRANSCRIPT",
        "expanded_source_context": dict(source_context_binding) if source_context_binding is not None else None,
        "source_witness_role": "HISTORICAL_GRID_ONLY" if source_context_binding is not None else "CURRENT_SOURCE_GRID",
        "fresh_source_review_required": source_context_binding is not None,
    }


def _continuation_retain_superseded_boundary_geometry(audit: dict[str, Any]) -> None:
    """Keep an already frozen source window after a validated text-only repair.

    Called on the freezer's evidence copy after CURRENT authority validation.
    Supersession retires the old spelling, not its mandatory source interval.
    The original repair ledger and its reconciliation receipt stay untouched.
    """
    frozen = audit.get("frozen_boundary_owner_contract")
    if not isinstance(frozen, Mapping):
        return
    owners = frozen.get("owners")
    if not isinstance(owners, list):
        return  # The ordinary contract validator still rejects malformed input.
    repairs = {
        "sha256:" + hashlib.sha256(json.dumps(
            repair, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        for item in (audit.get("exact_final_cpa_self_heal") or {}).get("passes", [])
        for repair in item.get("repairs", [])
    }
    for row in audit.get("entity_repairs") or []:
        reconciliation = row.get("reconciliation")
        if not isinstance(reconciliation, Mapping) or not (
            reconciliation.get("schema_version") == "exact-final-cpa-supersession.v1"
            and reconciliation.get("status") == "SUPERSEDED_BY_EXACT_FINAL_CPA"
            and reconciliation.get("timing_immutable") is True
            and reconciliation.get("exact_final_repair_sha256") in repairs
            and row.get("boundary_required") is True
        ):
            continue
        expected = {
            "owner_kind": "entity_repair", "owner_id": row.get("boundary_owner_id"),
            "required": True, "local_windows": [{
                "start_ms": row.get("matched_start_ms"),
                "end_ms": row.get("matched_end_ms"),
            }],
        }
        if expected in owners:
            row.pop("reconciliation")


def _continuation_boundary_owner_contract(
    persisted: Mapping[str, object],
    generated: Mapping[str, object],
    *,
    padded_dur: int,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Keep owners fixed while allowing a source-covered search extension.

    A larger search window is a new semantic request, not a new owner set or
    an inherited boundary verdict. Historical clock authority stays intact.
    """
    try:
        old = validate_frozen_boundary_owner_contract(persisted)
        new = validate_frozen_boundary_owner_contract(generated)
    except RuntimeError as exc:
        raise CurrentTextContinuationError("CURRENT_BOUNDARY_OWNER_CONTRACT_INVALID") from exc
    if (
        old.get("owner_set_sha256") != new.get("owner_set_sha256")
        or old.get("owner_eligibility_scope") != new.get("owner_eligibility_scope")
    ):
        raise CurrentTextContinuationError("CURRENT_BOUNDARY_OWNER_SET_DRIFT")
    before = old.get("boundary_search_scope")
    after = new.get("boundary_search_scope")
    if before == after:
        return old, None
    changing = {
        "repair_cap_ms", "recommendation_forward_ms", "max_recommended_end_ms",
        "required_local_source_context_end_ms", "required_last_piece_source_end_ms",
        "scope_sha256",
    }
    if (
        not boundary_search_scope_is_valid(before)
        or not boundary_search_scope_is_valid(after)
        or before.get("status") != "PASS"
        or after.get("status") != "PASS"
        or before.get("boundary_end_mode") not in {"semantic_lower_bound", "published_recall_anchor"}
        or before.get("baseline_tail_cap_ms") is not None
        or before.get("reviewed_exact_interval_projection") is not None
        or after["repair_cap_ms"] <= before["repair_cap_ms"]
        or after["required_local_source_context_end_ms"] > padded_dur
        or {k: v for k, v in before.items() if k not in changing}
        != {k: v for k, v in after.items() if k not in changing}
        or {k: v for k, v in old.items() if k not in {"boundary_search_scope", "contract_sha256"}}
        != {k: v for k, v in new.items() if k not in {"boundary_search_scope", "contract_sha256"}}
    ):
        raise CurrentTextContinuationError("CURRENT_BOUNDARY_OWNER_SET_DRIFT")
    return new, {
        "status": "SOURCE_COVERED_SEARCH_EXTENSION_REQUIRES_SEMANTIC_REVIEW",
        "prior_contract_sha256": old["contract_sha256"],
        "current_contract_sha256": new["contract_sha256"],
        "prior_scope_sha256": before["scope_sha256"],
        "current_scope_sha256": after["scope_sha256"],
        "prior_repair_cap_ms": before["repair_cap_ms"],
        "current_repair_cap_ms": after["repair_cap_ms"],
        "available_local_source_context_end_ms": padded_dur,
        "required_local_source_context_end_ms": after["required_local_source_context_end_ms"],
        "owners_unchanged": True,
        "prior_semantic_verdict_reused": False,
    }


def _current_checkpoint_clock_namespace(kwargs: Mapping[str, object]) -> object:
    from types import SimpleNamespace

    defaults = {field: None for field in CurrentTextContinuation.__dataclass_fields__}
    return SimpleNamespace(**(defaults | dict(kwargs)))


def _current_checkpoint_clock_bindings(native: Mapping[str, object]) -> dict[str, object]:
    clock = native.get('source_clock')
    if not isinstance(clock, Mapping):
        raise CurrentTextContinuationError('CURRENT_AUTO_RESUME_SOURCE_CLOCK_MISSING')
    values = {}
    for key, field in (('authority','source_clock_authority'), ('srt','source_clock_srt'),
            ('boundary_srt','source_boundary_srt'), ('timing_resolution','source_timing_resolution'),
            ('context_job','source_context_job'), ('context_manifest','source_context_manifest')):
        binding = clock.get(key)
        if binding is None and key not in {'authority','srt'}:
            continue
        if not isinstance(binding, Mapping) or not binding.get('path') or not binding.get('sha256'):
            raise CurrentTextContinuationError('CURRENT_AUTO_RESUME_SOURCE_CLOCK_BINDING_INVALID')
        values[field + '_path'] = Path(str(binding['path']))
        values['expected_' + field + '_sha256'] = binding['sha256']
    return values


def _bind_projection_recording_clock(projection: dict, spec: Mapping[str, object], padded_sha: str) -> None:
    """Bind delivery -> padded -> each original recording, without retiming SRT."""
    clock = spec.get("source_piece_clock")
    if clock is None:
        return  # Legacy explicit APIs cannot claim this ordinary provenance.
    if (not isinstance(clock, Mapping) or clock.get("time_domain") != "padded_local"
            or clock.get("root_time_domain") != "recording_absolute_ms"
            or _continuation_normalize_sha(clock.get("padded_sha256")) != padded_sha):
        raise CurrentTextContinuationError("CURRENT_SOURCE_PIECE_CLOCK_BINDING_INVALID")
    pieces = clock.get("pieces")
    if not isinstance(pieces, list) or not pieces:
        raise CurrentTextContinuationError("CURRENT_SOURCE_PIECE_CLOCK_BINDING_INVALID")
    for row in projection["rows"]:
        spans = []
        for piece in pieces:
            start = max(row["source_start_ms"], piece["padded_start_ms"])
            end = min(row["source_end_ms"], piece["mapped_padded_end_ms"])
            if start >= end:
                continue
            spans.append({
                "piece_index": piece["piece_index"],
                "recording_basename": piece["recording_basename"],
                "recording_sha256": piece["recording_sha256"],
                "piece_sha256": piece["piece_sha256"],
                "padded_start_ms": start, "padded_end_ms": end,
                "recording_start_ms": piece["recording_start_ms"] + start - piece["padded_start_ms"],
                "recording_end_ms": piece["recording_start_ms"] + end - piece["padded_start_ms"],
            })
        if sum(span["padded_end_ms"] - span["padded_start_ms"] for span in spans) != row["source_end_ms"] - row["source_start_ms"]:
            raise CurrentTextContinuationError("CURRENT_SOURCE_PROJECTION_UNMAPPED_MUX_TAIL")
        row["recording_spans"] = spans
    projection["delivery_time_domain"] = "delivery_local"
    projection["source_time_domain"] = "padded_local"
    projection["root_time_domain"] = "recording_absolute_ms"
    projection["source_piece_clock"] = dict(clock)


def _checkpoint_current_source_clock(
    continuation: CurrentTextContinuation, out_root: Path,
) -> dict[str, object]:
    """Freeze the original normal clock before mutable current output advances."""
    root = out_root / "current-text-clock"
    root.mkdir(parents=True, exist_ok=True)
    clock: dict[str, object] = {}
    for key, source in (
        ("authority", continuation.source_clock_authority_path or continuation.chat_authority_path),
        ("srt", continuation.source_clock_srt_path or continuation.current_srt_path),
        ("boundary_srt", continuation.source_boundary_srt_path),
        ("timing_resolution", continuation.source_timing_resolution_path),
        ("context_job", continuation.source_context_job_path),
        ("context_manifest", continuation.source_context_manifest_path),
    ):
        if source is None:
            continue
        from src.autoslice.producer_text_checkpoint import _frozen_binding

        try:
            clock[key] = _frozen_binding(root, key, source)
        except ValueError as exc:
            raise CurrentTextContinuationError("CURRENT_CHECKPOINT_CLOCK_DRIFT") from exc
    return clock


def _run_current_text_continuation(
    *,
    spec: dict,
    durations: list[int],
    padded: Path,
    padded_dur: int,
    host: str,
    cid: str,
    out_root: Path,
    adapters: TextPipelineAdapters,
    continuation: CurrentTextContinuation,
) -> TextPipelineResult:
    """Continue current bytes through the existing normal consumers."""

    if not callable(continuation.transcriber):
        raise CurrentTextContinuationError("CURRENT_TEXT_TRANSCRIBER_REQUIRED")
    retained_timing = _continuation_load_timing_resolution(continuation)
    if (
        (continuation.source_boundary_srt_path is None)
        != (continuation.expected_source_boundary_srt_sha256 is None)
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_ARTIFACT_PAIR_INVALID")
    if not isinstance(continuation.spans, Sequence) or isinstance(continuation.spans, (str, bytes)):
        raise CurrentTextContinuationError("CURRENT_TEXT_SPANS_REQUIRED")
    if any(
        not isinstance(getattr(span, "start_ms", None), int)
        or not isinstance(getattr(span, "end_ms", None), int)
        or int(getattr(span, "start_ms")) < 0
        or int(getattr(span, "end_ms")) > padded_dur
        or int(getattr(span, "end_ms")) <= int(getattr(span, "start_ms"))
        for span in continuation.spans
    ):
        raise CurrentTextContinuationError("CURRENT_TEXT_SPANS_INVALID")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in durations
    ) or (sum(durations) != padded_dur and not _source_piece_container_clock_matches(
        padded, sum(durations), padded_dur, allow_audio_packet_rounding=True,
    )):
        raise CurrentTextContinuationError("CURRENT_TEXT_DURATION_BINDING_INVALID")
    padded_sha256 = _continuation_sha256_path(padded, label="CURRENT_PADDED_MEDIA")
    _continuation_require_sha(padded_sha256, continuation.padded_sha256, label="CURRENT_PADDED_MEDIA")
    source_media_sha256s = tuple(
        value if str(value).startswith("sha256:") else "sha256:" + str(value)
        for value in continuation.source_media_sha256s
    )
    if not source_media_sha256s or any(len(_continuation_normalize_sha(value)) != 64 for value in source_media_sha256s):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BINDING_INVALID")
    pieces = spec.get("pieces")
    if isinstance(pieces, list):
        declared = {
            _continuation_normalize_sha(piece.get("source_media_sha256"))
            for piece in pieces
            if isinstance(piece, Mapping) and piece.get("source_media_sha256")
        }
        if declared and declared != {_continuation_normalize_sha(value) for value in source_media_sha256s}:
            raise CurrentTextContinuationError("CURRENT_SOURCE_BINDING_DRIFT")

    try:
        srt_raw = continuation.current_srt_path.read_bytes()
        srt_text = srt_raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CurrentTextContinuationError("CURRENT_SRT_UNREADABLE") from exc
    srt_sha256 = _continuation_sha256_path(continuation.current_srt_path, label="CURRENT_SRT")
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not cues:
        raise CurrentTextContinuationError("CURRENT_SRT_EMPTY")
    original_current_srt_raw = srt_raw
    original_current_srt_text = srt_text
    original_current_srt_sha256 = srt_sha256
    original_current_cues = list(cues)
    chat_authority, chat_sha256 = _continuation_read_json(
        continuation.chat_authority_path, label="CURRENT_CHAT_AUTHORITY", review=True
    )
    if continuation.expected_chat_authority_sha256 is not None:
        _continuation_require_sha(chat_sha256, continuation.expected_chat_authority_sha256, label="CURRENT_CHAT_AUTHORITY")
    clip_context, clip_sha256 = _continuation_read_json(
        continuation.clip_context_path, label="CURRENT_CLIP_CONTEXT"
    )
    if continuation.expected_clip_context_sha256 is not None:
        _continuation_require_sha(clip_sha256, continuation.expected_clip_context_sha256, label="CURRENT_CLIP_CONTEXT")
    try:
        clip_context = validate_clip_context(
            clip_context,
            candidate_id=cid,
            recording_date=str(spec.get("date") or "") or None,
            source_media_sha256s=source_media_sha256s,
        )
    except (TypeError, ValueError) as exc:
        raise CurrentTextContinuationError("CURRENT_CLIP_CONTEXT_INVALID") from exc
    source_clock_review, source_clock_reference = _continuation_source_clock_reference(
        continuation, chat_authority=chat_authority, srt_text=srt_text,
        srt_sha256=srt_sha256, clip_context=clip_context, candidate_id=cid,
    )
    final_review_audit = _continuation_validate_existing_authority(
        chat_authority,
        srt_text=srt_text,
        srt_sha256=srt_sha256,
        clip_context=clip_context,
        candidate_id=cid,
        source_clock_review=source_clock_review,
        source_clock_srt_sha256=(source_clock_reference or {}).get("reviewed_srt_sha256"),
        allow_source_scope_input=True,
    )
    completed_candidate = final_review_audit
    if ((final_review_audit.get('boundary_semantic_review') or {}).get('review_scope') == 'source_full_window'
            and final_review_audit.get('status') == 'CLEAN' and final_review_audit.get('release_gate') == 'PASS'):
        # A clock can also contain an actual completed content review of these
        # exact bytes. It is reusable only after the callback independently
        # matches the current source question/grid and physical clip interval.
        completed_candidate = source_clock_review or {}
    completed_final_review = copy.deepcopy(completed_candidate) if (
        (completed_candidate.get('boundary_semantic_review') or {}).get('review_scope') == 'final_delivery'
        and completed_candidate.get('status') == 'CLEAN' and completed_candidate.get('release_gate') == 'PASS'
        and completed_candidate.get('reviewed_srt_sha256') == 'sha256:' + srt_sha256) else None
    clock_review_audit = source_clock_review or final_review_audit
    clock_cues = None
    if source_clock_reference and source_clock_reference.get("reference_srt_path"):
        clock_text = Path(source_clock_reference["reference_srt_path"]).read_text(encoding="utf-8")
        if "sha256:" + hashlib.sha256(clock_text.encode()).hexdigest() != source_clock_reference["reviewed_srt_sha256"]:
            raise CurrentTextContinuationError("CURRENT_SOURCE_CLOCK_SRT_DRIFT")
        clock_cues = parse_srt_cues(clock_text)
    projected_source_cues, delivery_source_projection = _continuation_project_delivery_to_source(
        cues,
        final_review_audit=clock_review_audit,
        reference_cues=clock_cues,
        authority=chat_authority,
    )
    _bind_projection_recording_clock(delivery_source_projection, spec, padded_sha256)
    source_boundary_cues, source_boundary_context = _continuation_source_boundary_cues(
        chat_authority=chat_authority,
        clip_context=clip_context,
        final_review_audit=clock_review_audit,
        padded_dur=padded_dur,
        source_boundary_srt_path=continuation.source_boundary_srt_path,
        expected_source_boundary_srt_sha256=continuation.expected_source_boundary_srt_sha256,
        source_context_binding=_continuation_source_context_draft_binding(
            continuation, spec=spec, candidate_id=cid, padded_dur=padded_dur,
            source_media_sha256s=source_media_sha256s,
        ),
    )
    checkpoint_clock = _checkpoint_current_source_clock(continuation, out_root)
    from src.autoslice.producer_text_checkpoint import write_current_text_checkpoint

    write_current_text_checkpoint(
        out_root / f"{cid}.current-text-checkpoint.json", candidate_id=cid,
        srt_path=continuation.current_srt_path, authority_path=continuation.chat_authority_path,
        context_path=continuation.clip_context_path, padded=padded, source_clock=checkpoint_clock,
    )
    reconciled_srt, contradiction_audit = reconcile_contradictory_entity_repairs(
        srt_text, chat_authority.get("entity_repairs") or []
    )
    if reconciled_srt != srt_text:
        raise CurrentTextContinuationError("CURRENT_CHAT_AUTHORITY_TEXT_RECONCILIATION_DRIFT")
    if contradiction_audit:
        chat_authority["contradictory_entity_repair_audit"] = contradiction_audit
    live_path = out_root / f'{cid}.chat-authority.json'
    if live_path.is_file() and live_path.resolve() != continuation.chat_authority_path.resolve():
        live, live_sha = _continuation_read_json(live_path, label='CURRENT_USAGE_PARENT', review=True)
        usage = _later_current_cpa_usage(chat_authority, live, srt_sha256=srt_sha256)
        if usage is not None:
            from src.autoslice.producer_text_checkpoint import _frozen_binding
            bound = _frozen_binding(out_root / 'current-text-input', 'usage_parent', live_path)
            _continuation_require_sha(bound['sha256'].removeprefix('sha256:'), live_sha, label='CURRENT_USAGE_PARENT')
            chat_authority['current_input_usage_parent'] = {
                **bound, 'role': 'CUMULATIVE_USAGE_ONLY'}
            chat_authority['cpa_adjudication_usage'] = usage

    authoritative_chat = tuple(continuation.authoritative_chat) or _continuation_chat_from_context(clip_context)
    from src.autoslice.retained_source_usage import reconcile_legacy_source_usage
    reconcile_legacy_source_usage(chat_authority)
    required_boundary_owners = ledger_required_owner_contracts(
        spec=spec,
        durations=durations,
        ledger_path=None
        if str(spec.get("human_truth_mode") or "delivery") == "withheld"
        else adapters.profile_asset_file("subtitle_truth_ledger"),
    )
    required_boundary_owners.extend(_redelivery_baseline_boundary_owner(spec, durations))
    from src.autoslice.human_sequence_alignment import required_owners as human_sequence_owners, validate_records
    current_human_alignment = chat_authority.get("human_sequence_alignment") or {}
    if current_human_alignment.get("records"):
        validate_records(current_human_alignment,
            ledger_path=None if str(spec.get("human_truth_mode") or "delivery") == "withheld" else adapters.profile_asset_file("subtitle_truth_ledger"),
            spec=spec, durations=durations)
    required_boundary_owners.extend(human_sequence_owners(current_human_alignment))
    spec["required_boundary_owners"] = required_boundary_owners
    # A current delivery already carries a frozen source-domain owner set.
    # Exact-final surface repairs explicitly marked
    # POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER remain valid repair history but
    # must not be reintroduced as boundary owners on continuation.  Run the
    # existing freezer against an evidence copy with only those rows omitted,
    # then compare it to the persisted contract before the source review.
    boundary_owner_audit = copy.deepcopy(chat_authority)
    _continuation_retain_superseded_boundary_geometry(boundary_owner_audit)
    for owner_rows_key in (
        "exact_read",
        "sender_repairs",
        "gift_repairs",
        "coreference_repairs",
        "entity_repairs",
    ):
        rows = boundary_owner_audit.get(owner_rows_key)
        if not isinstance(rows, list):
            continue
        boundary_owner_audit[owner_rows_key] = [
            row
            for row in rows
            if not (
                isinstance(row, Mapping)
                and row.get("boundary_required") is False
                and row.get("boundary_owner_rejection")
                == "POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER"
            )
        ]
    boundary_target_ms, boundary_search_scope = freeze_required_boundary_owner_contract(
        spec=spec,
        durations=durations,
        chat_authority_audit=boundary_owner_audit,
        required_boundary_owners=required_boundary_owners,
    )
    persisted_owner_contract = chat_authority.get("frozen_boundary_owner_contract")
    generated_owner_contract = boundary_owner_audit.get("frozen_boundary_owner_contract")
    boundary_scope_extension = None
    if isinstance(persisted_owner_contract, Mapping):
        current_owner_contract, boundary_scope_extension = _continuation_boundary_owner_contract(
            persisted_owner_contract, generated_owner_contract, padded_dur=padded_dur,
        )
    else:
        current_owner_contract = generated_owner_contract

    # The current delivery may have been produced before a fixed caption
    # display spelling was registered.  Consume only that exact profile
    # authority after all immutable/current-input checks and owner replay.
    # Keep the old input, review, clock, and delivery grid as evidence; the
    # normal current-text consumer below owns any resulting new bytes.
    normalized_caption_srt, caption_display_audit = normalize_caption_display_surfaces(
        original_current_srt_text
    )
    caption_display_changed = normalized_caption_srt != original_current_srt_text
    normalized_caption_cues = [
        cue for cue in parse_srt_cues(normalized_caption_srt) if cue.text.strip()
    ]
    caption_display_audit = {
        **caption_display_audit,
        "review_scope": "CAPTION_DISPLAY_CANON_ONLY",
    }
    if caption_display_changed:
        source_clock_authority_sha256 = (
            source_clock_reference.get("authority_sha256")
            if isinstance(source_clock_reference, Mapping)
            else None
        )
        caption_display_audit.update(
            {
                "input_srt_path": str(continuation.current_srt_path),
                "input_srt_sha256_prefixed": "sha256:" + original_current_srt_sha256,
                "input_cue_grid_sha256": cue_grid_sha256(original_current_cues),
                "output_cue_grid_sha256": cue_grid_sha256(normalized_caption_cues),
                "input_chat_authority_path": str(continuation.chat_authority_path),
                "input_chat_authority_sha256": "sha256:" + chat_sha256,
                "input_clip_context_path": str(continuation.clip_context_path),
                "input_clip_context_sha256": "sha256:" + clip_sha256,
                "input_source_clock_authority_path": (
                    str(continuation.source_clock_authority_path)
                    if continuation.source_clock_authority_path is not None
                    else None
                ),
                "input_source_clock_authority_sha256": source_clock_authority_sha256,
                "prior_final_review_summary": _continuation_review_summary(final_review_audit),
                "prior_source_clock_review_summary": _continuation_review_summary(source_clock_review),
            }
        )
    chat_authority["current_caption_display_surface_audit"] = caption_display_audit
    previous_self_heal = chat_authority.get("exact_final_cpa_self_heal")
    if caption_display_changed and isinstance(previous_self_heal, Mapping):
        # This is a profile display transition, never a fabricated CPA repair.
        # The immutable input chain was validated above; current bytes still
        # require a new ordinary review before any release verdict.
        chat_authority["exact_final_cpa_self_heal"] = with_caption_successor(
            previous_self_heal, original_current_srt_text, normalized_caption_srt,
        )

    # A pending current transcript can itself cause source closure to fail.
    # Consume its ordinary text findings before asking source closure again;
    # retain the validated input clock and frozen source owners unchanged.
    current_text_recovery_required = bool(
        caption_display_changed
        or final_review_audit.get("status") == "AUDITOR_UNAVAILABLE"
        or (
            final_review_audit.get("release_gate") == "BLOCK"
            and final_review_audit.get("findings")
        )
    )
    if current_text_recovery_required:
        from src.autoslice.producer_package_finalization import (
            CurrentTextReviewArtifacts,
            _run_exact_final_review_gate,
        )

        recovery_root = out_root / "current-text-recovery"
        recovery_root.mkdir(parents=True, exist_ok=True)
        recovery_srt = recovery_root / f"{cid}.current.srt"
        recovery_srt.write_bytes(
            normalized_caption_srt.encode("utf-8")
            if caption_display_changed
            else original_current_srt_raw
        )
        recovery_chat = recovery_root / f"{cid}.chat-authority.json"
        _persist_review_document(recovery_chat, chat_authority)
        recovery_verifier = continuation.verify_confusable_entity
        if recovery_verifier is None:
            # The native audio account and its provider-facing observation
            # cache belong to this candidate's source, not to the directory a
            # sub-stage writes its review artifacts into. Recovery uses the
            # same account/cache as every other consumer of this output, so a
            # physical question already observed is a cache hit and is never
            # re-dispatched or re-counted from zero.
            recovery_verifier = _build_entity_verification_context(
                spec=spec, padded=padded, padded_dur=padded_dur, host=host,
                text_override_path=None, cid=cid, out_root=out_root,
                srt_text=srt_text, authoritative_chat=list(authoritative_chat),
                adapters=adapters,
                native_audio_state=continuation.native_audio_state,
                witness_cache_roots=continuation.witness_cache_roots,
            ).verify_confusable_entity
        recovery_reviewer = _build_exact_final_review_callback(
            spec=spec, out_root=recovery_root, cid=cid, padded=padded,
            adapters=adapters, authoritative_chat=authoritative_chat,
            clip_context=clip_context, final_review_audit=final_review_audit,
            verify_confusable_entity=recovery_verifier,
            truth_ownership=resolve_truth_full_ownership(spec) or resolve_operator_text_full_ownership(spec),
            screen_read_probe=build_env_screen_read_probe(padded, prior_observations=final_review_audit),
            continuation_correction=True, text_recovery_only=True,
        )
        try:
            from src.autoslice.continuation_subchecks import end_cold_read
            end_cold_read()  # the live review revalidates everything it consumes
            recovery_audit = _run_exact_final_review_gate(
                cid=cid, out_root=recovery_root,
                # The gate consumes cue extents; the projection retains cut gaps.
                final_start=delivery_source_projection["source_first_start_ms"],
                final_end=delivery_source_projection["source_last_end_ms"],
                recut=CurrentTextReviewArtifacts(recovery_srt),
                chat_authority_audit=chat_authority, chat_authority_path=recovery_chat,
                run_exact_final_review=recovery_reviewer, text_recovery_only=True,
                clip_context=clip_context,
            )
        except BaseException as gate_failure:
            try:
                write_current_text_checkpoint(
                    out_root / f"{cid}.current-text-checkpoint.json", candidate_id=cid,
                    srt_path=recovery_srt, authority_path=recovery_chat,
                    context_path=continuation.clip_context_path, padded=padded,
                    source_clock=checkpoint_clock,
                )
            except (OSError, ValueError, TypeError) as checkpoint_failure:
                gate_failure.add_note(f"CURRENT_CHECKPOINT_NOT_ADVANCED: {checkpoint_failure}")
            raise
        else:
            write_current_text_checkpoint(
                out_root / f"{cid}.current-text-checkpoint.json", candidate_id=cid,
                srt_path=recovery_srt, authority_path=recovery_chat,
                context_path=continuation.clip_context_path, padded=padded,
                source_clock=checkpoint_clock,
            )
        original_cues = cues
        srt_raw = recovery_srt.read_bytes()
        srt_text = srt_raw.decode("utf-8")
        cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
        projected_source_cues, delivery_source_projection = _continuation_project_delivery_to_source(
            cues, final_review_audit=clock_review_audit,
            reference_cues=clock_cues if clock_cues is not None else original_cues,
            authority=chat_authority,
        )
        _bind_projection_recording_clock(delivery_source_projection, spec, padded_sha256)
        # The copy is a clock reference, not a restored semantic PASS.
        final_review_audit = recovery_audit
        final_review_audit["current_text_recovery_artifacts"] = {
            "srt_path": str(recovery_srt), "chat_authority_path": str(recovery_chat),
            "srt_sha256": "sha256:" + hashlib.sha256(srt_raw).hexdigest(),
            "source_review_required": True,
        }
        if caption_display_changed:
            # The original current SRT remains untouched at its input path.
            # Only the newly reviewed continuation artifact becomes current.
            current_output_sha256 = hashlib.sha256(srt_raw).hexdigest()
            if "final_output_srt_sha256" in chat_authority:
                chat_authority["final_output_srt_sha256"] = current_output_sha256
            elif "final_text_srt_sha256" in chat_authority:
                chat_authority["final_text_srt_sha256"] = current_output_sha256
    if retained_timing is not None:
        timing_resolution, timing_resolution_sha256 = retained_timing
        attach_current_timing_qa_projection(
            source_boundary_cues, projected_source_cues, delivery_source_projection,
            resolution=timing_resolution, resolution_sha256=timing_resolution_sha256,
        )
    frozen_receipt, boundary_review, source_boundary_replay, exact_interval_replay = review_source_boundary(
        spec=spec,
        candidate_id=cid,
        cues=source_boundary_cues,
        boundary_target_ms=boundary_target_ms,
        boundary_search_scope=boundary_search_scope,
        current_owner_contract=current_owner_contract,
        authoritative_chat=authoritative_chat,
        clip_context=clip_context,
        available_local_source_context_end_ms=sum(durations),
        current_source_cues=projected_source_cues,
        current_delivery_projection=delivery_source_projection,
    )
    final_review_audit["boundary_semantic_review"] = boundary_review
    final_review_audit["current_delivery_source_projection"] = delivery_source_projection
    final_review_audit["source_boundary_context"] = source_boundary_context
    if source_clock_reference is not None:
        source_boundary_context["clock_reference"] = source_clock_reference
    final_review_audit["source_boundary_owner_contract"] = {
        "status": "FROZEN",
        "owner_set_sha256": current_owner_contract.get(
            "owner_set_sha256"
        ),
        "required_owner_count": current_owner_contract.get(
            "required_owner_count"
        ),
        "owners": current_owner_contract.get("owners") or [],
    }
    final_review_audit["boundary_owner_freeze_replay"] = {
        "status": "PASS",
        "persisted_contract_reused": isinstance(persisted_owner_contract, Mapping) and boundary_scope_extension is None,
        "generated_owner_set_sha256": generated_owner_contract.get("owner_set_sha256"),
        "generated_required_owner_count": generated_owner_contract.get("required_owner_count"),
        "post_boundary_surface_rows_excluded": True,
    }
    if boundary_scope_extension is not None:
        final_review_audit["boundary_owner_freeze_replay"]["source_context_extension"] = boundary_scope_extension
    if source_boundary_replay:
        final_review_audit["boundary_receipt_replay"] = {"source_full_window": source_boundary_replay}
    if exact_interval_replay is not None:
        final_review_audit["reviewed_exact_source_interval_replay"] = exact_interval_replay
    # Match the ordinary pipeline contract: the caller receives the complete
    # final-review audit through chat authority, while the delivery SRT itself
    # remains the original 0-based surface.  The boundary caller consumes the
    # separately projected source-domain cue objects below.
    chat_authority["final_review_audit"] = final_review_audit
    out_root.mkdir(parents=True, exist_ok=True)
    persist_review_audit(out_root / f"{cid}.review-flags.json", final_review_audit)
    output_srt = out_root / f"{cid}.continuation.srt"
    output_srt.write_bytes(srt_raw)
    output_context = out_root / f"{cid}.clip-context.json"
    output_context.write_bytes(continuation.clip_context_path.read_bytes())
    output_chat = out_root / f"{cid}.chat-authority.json"
    _persist_review_document(output_chat, chat_authority)

    from src.autoslice.producer_text_checkpoint import write_current_text_checkpoint

    write_current_text_checkpoint(
        out_root / f"{cid}.current-text-checkpoint.json", candidate_id=cid,
        srt_path=output_srt, authority_path=output_chat, context_path=output_context,
        padded=padded, source_clock=checkpoint_clock,
    )

    screen_read_probe = build_env_screen_read_probe(padded, prior_observations=final_review_audit)
    blocked_entity_verifier = continuation.verify_confusable_entity
    if blocked_entity_verifier is None:
        blocked_entity_verifier = _build_entity_verification_context(
            spec=spec,
            padded=padded,
            padded_dur=padded_dur,
            host=host,
            text_override_path=None,
            cid=cid,
            out_root=out_root,
            srt_text=srt_text,
            authoritative_chat=list(authoritative_chat),
            adapters=adapters,
            native_audio_state=continuation.native_audio_state,
            witness_cache_roots=continuation.witness_cache_roots,
        ).verify_confusable_entity

    review_exact_final_srt = _build_exact_final_review_callback(
        spec=spec,
        out_root=out_root,
        cid=cid,
        padded=padded,
        adapters=adapters,
        authoritative_chat=authoritative_chat,
        clip_context=clip_context,
        final_review_audit=final_review_audit,
        verify_confusable_entity=blocked_entity_verifier,
        truth_ownership=resolve_truth_full_ownership(spec)
        or resolve_operator_text_full_ownership(spec),
        screen_read_probe=screen_read_probe,
        frozen_boundary_receipt=frozen_receipt,
        continuation_correction=True,
        completed_final_review=completed_final_review,
    )

    return TextPipelineResult(
        srt_text=srt_text,
        # Boundary resolution operates on the padded/source timeline.  Keep
        # srt_text and the persisted continuation SRT byte-identical to the
        # current delivery surface; only the in-memory cue view is projected.
        cues=projected_source_cues,
        spans=list(continuation.spans),
        transcriber=continuation.transcriber,
        chat_authority_audit=chat_authority,
        chat_authority_path=output_chat,
        clip_context=clip_context,
        clip_context_path=output_context,
        review_exact_final_srt=review_exact_final_srt,
        boundary_context_cues=list(source_boundary_cues),
    )


def _retained_parent_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CurrentTextContinuationError(f"{label}_INTEGER_REQUIRED")
    return value


def _retained_parent_hash(value: object, *, label: str) -> str:
    digest = _continuation_normalize_sha(value)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise CurrentTextContinuationError(f"{label}_HASH_INVALID")
    return digest


def _later_current_cpa_usage(authority: Mapping, live: Mapping, *, srt_sha256: str) -> dict | None:
    """Keep later usage of this exact retained input, never its failed verdict."""
    reviewed = live.get('final_review_audit') or {}
    if (reviewed.get('schema_version') != FINAL_REVIEW_SCHEMA_VERSION
            or _continuation_normalize_sha(reviewed.get('reviewed_srt_sha256')) != srt_sha256):
        return None
    parent = authority.get('retained_reviewed_draft') or {}
    later_parent = live.get('retained_reviewed_draft') or {}
    if not parent and not later_parent:
        return None
    source_sha = _continuation_normalize_sha(authority.get('post_transcript_entity_output_srt_sha256'))
    if (re.fullmatch(r'[0-9a-f]{64}', source_sha) is None
            or _continuation_normalize_sha(live.get('post_transcript_entity_output_srt_sha256')) != source_sha
            or any(parent.get(key) is None or later_parent.get(key) != parent[key] for key in (
                'source_srt_sha256', 'parent_authority_sha256', 'parent_clip_context_sha256',
                'filler_plan_sha256', 'padded_sha256', 'source_timeline_offset_ms', 'source_endpoint'))
            or (authority.get('human_sequence_alignment') or {}).get('records')
                != (live.get('human_sequence_alignment') or {}).get('records')):
        raise CurrentTextContinuationError('CURRENT_USAGE_PARENT_DRIFT')
    old_usage, new_usage = _retained_parent_cpa_usage_seed(authority), _retained_parent_cpa_usage_seed(live)
    return new_usage if new_usage['provider_adjudication_count'] > old_usage['provider_adjudication_count'] else None


def _retained_parent_cpa_usage_seed(authority: Mapping[str, object]) -> dict[str, object]:
    """Carry only the validated cumulative CPA budget into a new draft.

    The parent audit, findings, entity repairs, and typed history remain
    immutable history references.  The ordinary exact-final reviewer only
    needs this cumulative usage mapping to avoid resetting the soft budget.
    """

    usage = authority.get("cpa_adjudication_usage")
    if not isinstance(usage, Mapping):
        raise CurrentTextContinuationError("CURRENT_PARENT_CPA_USAGE_MISSING")
    count = usage.get("provider_adjudication_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise CurrentTextContinuationError("CURRENT_PARENT_CPA_USAGE_INVALID")
    from src.autoslice.semantic_request_history import preserve_partial_usage
    return preserve_partial_usage(usage, authority)


def _retained_parent_media_offset_evidence(
    value: object,
    *,
    offset_ms: int,
    padded_sha256: str,
) -> bool:
    """Find a hash-bound physical-clock witness in the immutable parent.

    The retained seed may use a historical source resolver whose boundary
    review has no delivery ``source_separation_witness``.  The parent still
    has to contain a real source-media/timeline binding for the physical
    offset. Normal acoustic observations store the clock in their typed
    timeline_binding; unrelated sibling rows are never joined.
    """

    if isinstance(value, Mapping):
        raw_offset = value.get("source_media_timeline_offset_ms")
        media_values = (
            value.get("source_media_sha256"),
            value.get("source_sha256"),
            value.get("media_sha256"),
            value.get("padded_sha256"),
            value.get("source_media_binding"),
        )
        media_matches = any(
                isinstance(candidate, str)
                and _continuation_normalize_sha(candidate) == padded_sha256
                for candidate in media_values
        )
        if raw_offset == offset_ms and media_matches:
            return True
        timeline = value.get("timeline_binding")
        if media_matches and isinstance(timeline, Mapping):
            delivery = timeline.get("delivery_local")
            source = timeline.get("source_media")
            if (
                timeline.get("schema_version") == "subtitle-audio-timeline-binding.v1"
                and timeline.get("source_media_timeline_offset_ms") == offset_ms
                and isinstance(delivery, Mapping) and isinstance(source, Mapping)
                and all(isinstance(row.get(key), int) and not isinstance(row.get(key), bool)
                        for row in (delivery, source) for key in ("target_start_ms", "target_end_ms"))
                and source["target_start_ms"] == delivery["target_start_ms"] + offset_ms
                and source["target_end_ms"] == delivery["target_end_ms"] + offset_ms
                and source["target_end_ms"] > source["target_start_ms"] >= 0
                and source.get("crop_start_ms") == value.get("audio_start_ms")
                and source.get("crop_end_ms") == value.get("audio_end_ms")
                and isinstance(source.get("crop_start_ms"), int)
                and isinstance(source.get("crop_end_ms"), int)
                and source["crop_start_ms"] <= source["target_start_ms"]
                and source["target_end_ms"] <= source["crop_end_ms"]
            ):
                return True
        return any(
            _retained_parent_media_offset_evidence(
                child, offset_ms=offset_ms, padded_sha256=padded_sha256,
            )
            for child in value.values()
        )
    if isinstance(value, list):
        return any(
            _retained_parent_media_offset_evidence(
                child, offset_ms=offset_ms, padded_sha256=padded_sha256,
            )
            for child in value
        )
    return False


def _validate_retained_reviewed_parent_input(
    *,
    authority: Mapping[str, Any],
    srt_text: str,
    srt_sha256: str,
    clip_context: Mapping[str, object],
    candidate_id: str,
    boundary_semantic_review: Mapping[str, object],
    retained_config: Mapping[str, object] | None = None,
    padded_sha256: object | None = None,
    source_media_sha256s: Sequence[str] | None = None,
    padded_duration_ms: int | None = None,
    parent_materialization_path: str | None = None,
    parent_materialization_sha256: object | None = None,
    parent_materialization_duration_ms: int | None = None,
    parent_materialization_provenance_path: str | None = None,
    parent_materialization_provenance_sha256: object | None = None,
) -> Mapping[str, object]:
    """Validate a retained parent as input evidence, without release geometry.

    A retained draft is a new projected text surface.  Its parent can remain
    ``FLAGGED/BLOCK`` with unresolved findings; the only accepted result here
    is an input checkpoint followed by a fresh normal review.  In particular,
    this helper deliberately does not call ``validate_final_review_release``
    or ``_validate_review_geometry`` and never turns the historical source
    audit into a current boundary PASS.
    """

    if authority.get("schema_version") != "chat-authority-audit.v2":
        raise CurrentTextContinuationError("CURRENT_CHAT_AUTHORITY_SCHEMA_INVALID")
    if authority.get("mutation_authorized") is True:
        raise CurrentTextContinuationError("CURRENT_CHAT_AUTHORITY_MUTATION_INVALID")
    if clip_context.get("schema_version") != "lidousha-clip-context.v1":
        raise CurrentTextContinuationError("CURRENT_PARENT_CLIP_CONTEXT_SCHEMA_INVALID")
    if clip_context.get("mutation_authorized") is True:
        raise CurrentTextContinuationError("CURRENT_PARENT_CLIP_CONTEXT_MUTATION_INVALID")
    if clip_context.get("candidate_id") != candidate_id:
        raise CurrentTextContinuationError("CURRENT_PARENT_CANDIDATE_DRIFT")
    if not isinstance(retained_config, Mapping):
        raise CurrentTextContinuationError("CURRENT_RETAINED_CONFIG_REQUIRED")
    if not isinstance(source_media_sha256s, Sequence) or isinstance(source_media_sha256s, (str, bytes)):
        raise CurrentTextContinuationError("CURRENT_PARENT_SOURCE_HASHES_INVALID")
    normalised_source_hashes = tuple(
        _retained_parent_hash(value, label="CURRENT_PARENT_SOURCE")
        for value in source_media_sha256s
    )
    if not normalised_source_hashes:
        raise CurrentTextContinuationError("CURRENT_PARENT_SOURCE_HASHES_INVALID")
    padded_digest = _retained_parent_hash(padded_sha256, label="CURRENT_PADDED_MEDIA")
    configured_parent_sha = retained_config.get("parent_materialization_sha256")
    parent_digest = _retained_parent_hash(
        parent_materialization_sha256
        if parent_materialization_sha256 is not None
        else configured_parent_sha,
        label="CURRENT_PARENT_MATERIALIZATION",
    )
    if configured_parent_sha is None or _retained_parent_hash(
        configured_parent_sha, label="CURRENT_PARENT_MATERIALIZATION"
    ) != parent_digest:
        raise CurrentTextContinuationError("CURRENT_PARENT_MATERIALIZATION_HASH_DRIFT")
    parent_duration = _retained_parent_int(
        parent_materialization_duration_ms
        if parent_materialization_duration_ms is not None
        else retained_config.get("parent_materialization_duration_ms"),
        label="CURRENT_PARENT_MATERIALIZATION_DURATION_MS",
    )
    if parent_duration <= 0:
        raise CurrentTextContinuationError("CURRENT_PARENT_MATERIALIZATION_DURATION_INVALID")
    configured_parent_duration = _retained_parent_int(
        retained_config.get("parent_materialization_duration_ms"),
        label="CURRENT_PARENT_MATERIALIZATION_DURATION_MS",
    )
    if configured_parent_duration != parent_duration:
        raise CurrentTextContinuationError("CURRENT_PARENT_MATERIALIZATION_DURATION_DRIFT")
    configured_parent_path = retained_config.get("parent_materialization_path")
    if not isinstance(configured_parent_path, str) or not configured_parent_path:
        raise CurrentTextContinuationError("CURRENT_PARENT_MATERIALIZATION_PATH_REQUIRED")
    if parent_materialization_path is not None and not isinstance(parent_materialization_path, str):
        raise CurrentTextContinuationError("CURRENT_PARENT_MATERIALIZATION_PATH_INVALID")
    if parent_materialization_path is not None and parent_materialization_path != configured_parent_path:
        raise CurrentTextContinuationError("CURRENT_PARENT_MATERIALIZATION_PATH_DRIFT")
    configured_parent_provenance_path = retained_config.get(
        "parent_materialization_provenance_path"
    )
    if not isinstance(configured_parent_provenance_path, str) or not configured_parent_provenance_path:
        raise CurrentTextContinuationError(
            "CURRENT_PARENT_MATERIALIZATION_PROVENANCE_PATH_REQUIRED"
        )
    if parent_materialization_provenance_path is not None and not isinstance(
        parent_materialization_provenance_path, str
    ):
        raise CurrentTextContinuationError("CURRENT_PARENT_MATERIALIZATION_PROVENANCE_PATH_INVALID")
    if (
        parent_materialization_provenance_path is not None
        and parent_materialization_provenance_path != configured_parent_provenance_path
    ):
        raise CurrentTextContinuationError(
            "CURRENT_PARENT_MATERIALIZATION_PROVENANCE_PATH_DRIFT"
        )
    configured_parent_provenance_sha = _retained_parent_hash(
        retained_config.get("parent_materialization_provenance_sha256"),
        label="CURRENT_PARENT_MATERIALIZATION_PROVENANCE",
    )
    if parent_materialization_provenance_sha256 is not None:
        parent_provenance_digest = _retained_parent_hash(
            parent_materialization_provenance_sha256,
            label="CURRENT_PARENT_MATERIALIZATION_PROVENANCE",
        )
        if parent_provenance_digest != configured_parent_provenance_sha:
            raise CurrentTextContinuationError(
                "CURRENT_PARENT_MATERIALIZATION_PROVENANCE_HASH_DRIFT"
            )

    terminal_srt_sha = authority.get("final_output_srt_sha256")
    if terminal_srt_sha is None:
        terminal_srt_sha = authority.get("final_text_srt_sha256")
    if terminal_srt_sha is None:
        raise CurrentTextContinuationError("CURRENT_FINAL_OUTPUT_SRT_BINDING_MISSING")
    if _retained_parent_hash(terminal_srt_sha, label="CURRENT_FINAL_OUTPUT_SRT") != _retained_parent_hash(
        srt_sha256, label="CURRENT_SRT"
    ):
        raise CurrentTextContinuationError("CURRENT_FINAL_OUTPUT_SRT_BINDING_MISMATCH")
    if not isinstance(srt_text, str) or not srt_text:
        raise CurrentTextContinuationError("CURRENT_SRT_TEXT_INVALID")
    try:
        parsed_current_cues = parse_srt_cues(srt_text)
    except Exception as exc:
        raise CurrentTextContinuationError("CURRENT_SRT_PARSE_INVALID") from exc
    if not parsed_current_cues or any(not cue.text.strip() for cue in parsed_current_cues):
        raise CurrentTextContinuationError("CURRENT_SRT_PARSE_INVALID")

    context_pieces = clip_context.get("pieces")
    if not isinstance(context_pieces, list) or not context_pieces:
        raise CurrentTextContinuationError("CURRENT_PARENT_SOURCE_PIECES_INVALID")
    context_hashes: list[str] = []
    for piece in context_pieces:
        if not isinstance(piece, Mapping):
            raise CurrentTextContinuationError("CURRENT_PARENT_SOURCE_PIECE_INVALID")
        try:
            context_hashes.append(_continuation_normalize_sha(piece.get("source_media_sha256")))
        except CurrentTextContinuationError as exc:
            raise CurrentTextContinuationError("CURRENT_PARENT_SOURCE_PIECE_HASH_INVALID") from exc
    if tuple(context_hashes) != normalised_source_hashes:
        raise CurrentTextContinuationError("CURRENT_PARENT_SOURCE_HASHES_DRIFT")

    final_review = authority.get("final_review_audit")
    if not isinstance(final_review, Mapping):
        raise CurrentTextContinuationError("CURRENT_FINAL_REVIEW_AUDIT_MISSING")
    try:
        _validate_review_integrity(
            final_review,
            expected_srt_sha256="sha256:" + srt_sha256,
        )
        _validate_exact_final_cpa_self_heal(
            authority,
            expected_srt_sha256="sha256:" + srt_sha256,
            allow_pending=True,
        )
    except FinalReviewContractError as exc:
        raise CurrentTextContinuationError(exc.reason_code) from exc

    findings = final_review.get("findings")
    finding_count = final_review.get("validated_finding_count")
    if not isinstance(findings, list) or (
        isinstance(finding_count, bool)
        or not isinstance(finding_count, int)
        or finding_count != len(findings)
    ):
        raise CurrentTextContinuationError("CURRENT_FINAL_REVIEW_FINDINGS_INVALID")
    entity_rows = authority.get("entity_repairs") or []
    if not isinstance(entity_rows, list):
        raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIRS_INVALID")

    correction_pass = final_review.get("correction_pass")
    if not isinstance(correction_pass, Mapping):
        raise CurrentTextContinuationError("CURRENT_CORRECTION_PASS_MISSING")
    # The normal continuation writer stores transcript context on the outer
    # review. Ordinary parents may predate that disclosure. The external,
    # hash-bound resolver audit and its exact source grid are the input
    # authority; a caller-created correction_pass placeholder is not.
    source_context = final_review.get("source_boundary_context")
    if source_context is not None and not isinstance(source_context, Mapping):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_CONTEXT_INVALID")

    audit_binding = retained_config.get("source_boundary_audit")
    if isinstance(audit_binding, Mapping):
        audit_path_value = audit_binding.get("path")
        audit_expected_sha = audit_binding.get("sha256")
    else:
        audit_path_value = retained_config.get("source_boundary_audit_path")
        audit_expected_sha = retained_config.get("source_boundary_audit_sha256")
    if (
        not isinstance(audit_path_value, str)
        or not audit_path_value
        or audit_expected_sha is None
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_AUDIT_BINDING_MISSING")
    try:
        source_audit, source_audit_digest = _continuation_read_json(
            Path(audit_path_value), label="CURRENT_SOURCE_BOUNDARY_AUDIT",
        )
        _continuation_require_sha(
            source_audit_digest,
            audit_expected_sha,
            label="CURRENT_SOURCE_BOUNDARY_AUDIT",
        )
    except CurrentTextContinuationError:
        raise
    except Exception as exc:
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_AUDIT_INVALID") from exc

    source_review = source_audit.get("boundary_semantic_review")
    if not isinstance(source_review, Mapping):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_REVIEW_MISSING")
    if (
        source_audit.get("schema_version") != "boundary-audit.v1"
        or source_review.get("schema_version") != "talk-boundary-semantic-review.v1"
        or source_review.get("status") != "PASS"
        or source_review.get("review_scope") != "source_full_window"
        or source_review.get("candidate_id") != candidate_id
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_AUDIT_NOT_PASS")
    endpoint = source_review.get("final_endpoint_binding")
    if (
        not isinstance(endpoint, Mapping)
        or endpoint.get("schema_version") != "talk-boundary-final-endpoint-binding.v1"
        or endpoint.get("status") != "PASS"
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_ENDPOINT_INVALID")
    try:
        audit_start = _retained_parent_int(source_audit.get("final_start_ms"), label="CURRENT_SOURCE_AUDIT_START")
        audit_end = _retained_parent_int(source_audit.get("final_end_ms"), label="CURRENT_SOURCE_AUDIT_END")
        endpoint_start = _retained_parent_int(endpoint.get("final_start_ms"), label="CURRENT_SOURCE_ENDPOINT_START")
        endpoint_end = _retained_parent_int(endpoint.get("final_end_ms"), label="CURRENT_SOURCE_ENDPOINT_END")
    except CurrentTextContinuationError:
        raise
    if audit_end <= audit_start or (audit_start, audit_end) != (endpoint_start, endpoint_end):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_ENDPOINT_DRIFT")
    source_request_sha = _retained_parent_hash(
        source_review.get("request_sha256"), label="CURRENT_SOURCE_BOUNDARY_REQUEST",
    )
    source_path_value = retained_config.get("source_boundary_srt_path")
    source_expected_sha = retained_config.get("source_boundary_srt_sha256")
    explicit_source = source_path_value is not None or source_expected_sha is not None
    if explicit_source:
        if not isinstance(source_path_value, str) or not source_path_value or source_expected_sha is None:
            raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_ARTIFACT_PAIR_INVALID")
        source_path = Path(source_path_value)
        source_srt_sha = _continuation_sha256_path(
            source_path, label="CURRENT_SOURCE_BOUNDARY_ARTIFACT",
        )
        try:
            source_bytes = source_path.read_bytes()
            source_raw = source_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_ARTIFACT_UNREADABLE") from exc
        if hashlib.sha256(source_bytes).hexdigest() != source_srt_sha:
            raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_ARTIFACT_DRIFT")
        _continuation_require_sha(
            source_srt_sha, source_expected_sha, label="CURRENT_SOURCE_BOUNDARY_ARTIFACT",
        )
    else:
        source_raw = authority.get("post_transcript_entity_output_srt")
        if not isinstance(source_raw, str) or not source_raw:
            raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_SRT_MISSING")
        source_srt_sha = hashlib.sha256(source_raw.encode()).hexdigest()
        _continuation_require_sha(
            source_srt_sha, authority.get("post_transcript_entity_output_srt_sha256"),
            label="CURRENT_SOURCE_BOUNDARY_SRT",
        )
    source_cues = parse_srt_cues(source_raw)
    source_grid_sha = cue_grid_sha256(source_cues)
    if (
        not source_cues
        or any(cue.start_ms < 0 or cue.end_ms <= cue.start_ms or cue.end_ms > parent_duration for cue in source_cues)
        or source_review.get("cue_grid_sha256") != source_grid_sha
        or endpoint.get("semantic_cue_grid_sha256") != source_grid_sha
        or endpoint.get("semantic_request_sha256") != "sha256:" + source_request_sha
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_GRID_DRIFT")
    try:
        frozen = validate_frozen_boundary_owner_contract(authority.get("frozen_boundary_owner_contract"))
    except RuntimeError as exc:
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_OWNER_CONTRACT_INVALID") from exc
    owner_verification = source_audit.get("required_boundary_owner_verification")
    if (
        source_review.get("target_ms") != frozen.get("boundary_review_target_ms")
        or source_review.get("boundary_search_scope") != frozen.get("boundary_search_scope")
        or source_audit.get("frozen_required_boundary_owners") != frozen.get("owners")
        or not isinstance(owner_verification, Mapping)
        or owner_verification.get("status") != "PASS"
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_OWNER_SCOPE_DRIFT")
    if isinstance(source_context, Mapping) and (
        source_context.get("schema_version") != "source-boundary-transcript-context.v1"
        or source_context.get("source_srt_sha256") != "sha256:" + source_srt_sha
        or source_context.get("source_cue_grid_sha256") != source_grid_sha
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_CONTEXT_DRIFT")

    declared_delivery_offset = retained_config.get("source_boundary_delivery_offset_ms")
    if declared_delivery_offset is None:
        declared_delivery_offset = retained_config.get("source_timeline_offset_ms")
    declared_delivery_offset = _retained_parent_int(
        declared_delivery_offset, label="CURRENT_SOURCE_BOUNDARY_DELIVERY_OFFSET",
    )
    if audit_start != declared_delivery_offset:
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_OFFSET_DRIFT")
    source_piece_start = context_pieces[0].get("start_ms")
    if source_piece_start is not None:
        source_piece_start = _retained_parent_int(
            source_piece_start, label="CURRENT_SOURCE_PIECE_START",
        )
        source_piece_end = _retained_parent_int(
            context_pieces[0].get("end_ms"), label="CURRENT_SOURCE_PIECE_END",
        )
        if source_piece_end <= source_piece_start:
            raise CurrentTextContinuationError("CURRENT_SOURCE_PIECE_RANGE_INVALID")
        if audit_start < 0 or audit_end > source_piece_end - source_piece_start:
            raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_CONTEXT_RANGE_DRIFT")
        projection_offset = _retained_parent_int(
            retained_config.get("source_timeline_offset_ms"),
            label="SOURCE_TIMELINE_OFFSET_MS",
        )
        if projection_offset != source_piece_start + declared_delivery_offset:
            raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_PHYSICAL_OFFSET_DRIFT")
    else:
        projection_offset = declared_delivery_offset
    if padded_duration_ms is not None:
        retained_padded_duration = _retained_parent_int(
            padded_duration_ms, label="CURRENT_PADDED_DURATION_MS"
        )
        if retained_padded_duration <= 0:
            raise CurrentTextContinuationError("CURRENT_PADDED_DURATION_INVALID")
    if audit_end > parent_duration:
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_PARENT_DURATION_INVALID")
    if not _retained_parent_media_offset_evidence(
        authority, offset_ms=declared_delivery_offset, padded_sha256=parent_digest,
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_BOUNDARY_OFFSET_EVIDENCE_MISSING")

    pending_entity_reviews = []
    for row_index, row in enumerate(entity_rows):
        if not isinstance(row, Mapping):
            raise CurrentTextContinuationError("CURRENT_ENTITY_REPAIR_INVALID")
        judge = row.get("judge")
        if (
            row.get("mode") == "exact_final_cpa_self_heal"
            and isinstance(judge, Mapping)
            and judge.get("schema_version") in {
                "exact-final-cpa-cycle-judge.v1", "exact-final-cpa-convergence-judge.v1",
                "editorial-subtitle-judge.v1",
            }
        ):
            _continuation_validate_exact_final_owner(
                row,
                row_index=row_index,
                chat_authority=authority,
                source_timeline_offset_ms=declared_delivery_offset,
            )
        elif row.get("mode") == "transcript_entity_only":
            # The normal entity writer owns this verdict in the padded source
            # grid, before boundary trimming renumbers delivery cues. Replay
            # that original contract against the hash-bound source transcript;
            # it supplies input provenance, never a current release judge.
            _validate_source_stage_transcript_entity_repair(
                row, authority=authority, srt_text=source_raw,
                allow_pending_audio_input=True,
            )
            if row["verdict"].get("needs_audio") is True:
                # Admission is not adjudication. An old RESOLVED spelling can
                # still carry an unresolved acoustic question. Recover only
                # the identical surviving physical target, never its answer.
                matches = [(index, cue) for index, cue in enumerate(parsed_current_cues, 1)
                    if cue.start_ms + declared_delivery_offset == row["matched_start_ms"]
                    and cue.end_ms + declared_delivery_offset == row["matched_end_ms"]
                    and cue.text == row["after"][0]]
                if len(matches) != 1:
                    raise CurrentTextContinuationError(
                        "CURRENT_TRANSCRIPT_ENTITY_PENDING_TARGET_DRIFT")
                index, cue = matches[0]
                pending_entity_reviews.append({
                    "role": "REVIEW_NOMINATION_ONLY",
                    "source_repair_index": row_index,
                    "source_receipt_sha256": "sha256:" + hashlib.sha256(json.dumps(
                        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")).hexdigest(),
                    "source_cue_index": row["cue_indexes"][0],
                    "source_start_ms": row["matched_start_ms"],
                    "source_end_ms": row["matched_end_ms"],
                    "entity_start_codepoint": re.search(re.escape(row["transcript_surface"]),
                        row["before"][0], flags=re.IGNORECASE).start(),
                    "entity_end_codepoint": re.search(re.escape(row["transcript_surface"]),
                        row["before"][0], flags=re.IGNORECASE).start() + len(row["expected_entity"]),
                    "parent_delivery_cue_index": index,
                    "parent_delivery_start_ms": cue.start_ms,
                    "parent_delivery_end_ms": cue.end_ms,
                    "text_sha256": "sha256:" + hashlib.sha256(cue.text.encode()).hexdigest(),
                    "mutation_authorized": False,
                    "release_authorized": False,
                })
        else:
            _continuation_validate_existing_repair(
                row, srt_text=srt_text, clip_context=clip_context,
            )

    return {
        "status": "INPUT_VALIDATED_PENDING_REVIEW" if pending_entity_reviews else "INPUT_VALIDATED",
        "pending_entity_reviews": pending_entity_reviews,
        "fresh_review_required": True,
        "historical_release_gate": final_review.get("release_gate"),
        "unresolved_finding_count": len(findings),
        "source_timeline_offset_ms": projection_offset,
        "source_endpoint": {
            "final_start_ms": endpoint_start,
            "final_end_ms": endpoint_end,
            "delivery_offset_ms": declared_delivery_offset,
        },
        "source_boundary_audit_path": audit_path_value,
        "source_boundary_audit_sha256": "sha256:" + source_audit_digest,
        "source_boundary_context": {
            "role": "EVIDENCE_ONLY_RETAINED_PARENT_SOURCE_CONTEXT",
            "source_srt_role": "EXPLICIT_SOURCE_BOUNDARY_ARTIFACT" if explicit_source else "CHAT_AUTHORITY_POST_TRANSCRIPT_ENTITY_OUTPUT",
            "source_srt_path": source_path_value if explicit_source else None,
            "source_srt_sha256": "sha256:" + source_srt_sha,
            "source_cue_grid_sha256": source_grid_sha,
            "source_full_window_request_sha256": "sha256:" + source_request_sha,
            "frozen_owner_contract_sha256": frozen["contract_sha256"],
            "prior_boundary_pass_inherited": False,
            "fresh_review_required": True,
        },
        "new_padded_sha256": "sha256:" + padded_digest,
        "new_padded_duration_ms": padded_duration_ms,
        "parent_materialization_path": parent_materialization_path
        or retained_config.get("parent_materialization_path"),
        "parent_materialization_sha256": "sha256:" + parent_digest,
        "parent_materialization_duration_ms": parent_duration,
        "parent_materialization_provenance_path": parent_materialization_provenance_path
        or retained_config.get("parent_materialization_provenance_path"),
        "parent_materialization_provenance_sha256": (
            "sha256:" + (
                _retained_parent_hash(
                    parent_materialization_provenance_sha256,
                    label="CURRENT_PARENT_MATERIALIZATION_PROVENANCE",
                )
                if parent_materialization_provenance_sha256 is not None
                else configured_parent_provenance_sha
            )
        ),
        "source_media_sha256s": tuple("sha256:" + value for value in normalised_source_hashes),
    }


def _validate_retained_reviewed_parent(
    *,
    authority: Mapping[str, Any],
    srt_text: str,
    srt_sha256: str,
    clip_context: Mapping[str, object],
    candidate_id: str,
    boundary_semantic_review: Mapping[str, object],
    retained_config: Mapping[str, object] | None = None,
    padded_sha256: object | None = None,
    source_media_sha256s: Sequence[str] | None = None,
    padded_duration_ms: int | None = None,
    parent_materialization_path: str | None = None,
    parent_materialization_sha256: object | None = None,
    parent_materialization_duration_ms: int | None = None,
    parent_materialization_provenance_path: str | None = None,
    parent_materialization_provenance_sha256: object | None = None,
) -> Mapping[str, object]:
    return _validate_retained_reviewed_parent_input(
        authority=authority,
        srt_text=srt_text,
        srt_sha256=srt_sha256,
        clip_context=clip_context,
        candidate_id=candidate_id,
        boundary_semantic_review=boundary_semantic_review,
        retained_config=retained_config,
        padded_sha256=padded_sha256,
        source_media_sha256s=source_media_sha256s,
        padded_duration_ms=padded_duration_ms,
        parent_materialization_path=parent_materialization_path,
        parent_materialization_sha256=parent_materialization_sha256,
        parent_materialization_duration_ms=parent_materialization_duration_ms,
        parent_materialization_provenance_path=parent_materialization_provenance_path,
        parent_materialization_provenance_sha256=parent_materialization_provenance_sha256,
    )


def _automatic_current_source_bindings(
    *,
    spec: Mapping[str, object],
    padded: Path,
    out_root: Path,
    durations: list[int] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Read the source bindings already produced by normal media preparation.

    Automatic current-text ingress is deliberately narrower than the explicit
    continuation API.  The producer has already materialized one or more
    source pieces and a padded concat before it reaches this module.  Reuse
    those byte-bound provenance files and the now-mutated normal spec; never
    infer a source identity from an authority/context claim alone.
    """

    pieces = spec.get("pieces")
    if not isinstance(pieces, list) or not pieces:
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_SPEC_INVALID")

    padded_sha256 = _continuation_sha256_path(padded, label="CURRENT_PADDED_MEDIA")
    padded_provenance_path = padded.with_suffix(".provenance.json")
    padded_provenance, _ = _continuation_read_json(
        padded_provenance_path, label="CURRENT_PADDED_PROVENANCE"
    )
    if padded_provenance.get("output_path") != str(padded.resolve()):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_PADDED_PROVENANCE_PATH_DRIFT")
    _continuation_require_sha(
        padded_sha256,
        padded_provenance.get("output_sha256"),
        label="CURRENT_PADDED_MEDIA",
    )
    padded_inputs = padded_provenance.get("inputs")
    if not isinstance(padded_inputs, list) or len(padded_inputs) != len(pieces):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_PADDED_PROVENANCE_INPUTS_INVALID")

    # Retained media keeps its real piece paths in the immutable parent. The
    # new output directory only contains byte-identical provenance sidecars.
    # Select that parent from the explicit, hash-bound declaration, never from
    # an arbitrary output_path inside a sidecar.
    piece_root = out_root
    retained = spec.get("retained_source_input")
    legacy_retained = spec.get("retained_current_text")
    if retained is not None and legacy_retained is not None:
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_RETAINED_MEDIA_CONFLICT")
    retained = retained if retained is not None else legacy_retained
    if retained is not None:
        if not isinstance(retained, Mapping):
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_RETAINED_MEDIA_INVALID")
        if (Path(str(retained.get("parent_materialization_path") or "")).resolve() != padded.resolve()
                or Path(str(retained.get("parent_materialization_provenance_path") or "")).resolve()
                != padded_provenance_path.resolve()):
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_RETAINED_MEDIA_PATH_DRIFT")
        for field in ("padded_sha256", "parent_materialization_sha256"):
            _continuation_require_sha(padded_sha256, retained.get(field), label="CURRENT_RETAINED_MEDIA")
        _continuation_require_sha(
            _continuation_sha256_path(padded_provenance_path, label="CURRENT_PADDED_PROVENANCE"),
            retained.get("parent_materialization_provenance_sha256"), label="CURRENT_RETAINED_PROVENANCE",
        )
        piece_root = padded.parent

    source_media_sha256s: list[str] = []
    validated_piece_rows: list[dict] = []
    for index, piece in enumerate(pieces):
        if not isinstance(piece, Mapping):
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_PIECE_INVALID")
        start_ms = piece.get("start_ms")
        end_ms = piece.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_PIECE_RANGE_INVALID")
        source_sha = _continuation_normalize_sha(piece.get("source_media_sha256"))
        if len(source_sha) != 64 or any(char not in "0123456789abcdef" for char in source_sha):
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_HASH_MISSING")

        piece_path = piece_root / f"piece_{index}_{start_ms}_{end_ms}.mp4"
        piece_provenance, _ = _continuation_read_json(
            piece_path.with_suffix(".provenance.json"),
            label="CURRENT_SOURCE_PIECE_PROVENANCE",
        )
        expected_source_path = str(piece.get("remote_media") or "")
        expected_source_paths = {expected_source_path}
        if expected_source_path:
            # Local source preparation records the resolved path, while a
            # remote hash worker records the path exactly as supplied.  Both
            # are the same normal source binding; accept only those two
            # deterministic spellings and still require the byte hash below.
            expected_source_paths.add(str(Path(expected_source_path).resolve()))
        if (
            piece_provenance.get("source_path") not in expected_source_paths
            or piece_provenance.get("source_sha256") != source_sha
            or piece_provenance.get("source_media_binding") != "sha256:" + source_sha
            or piece_provenance.get("start_ms") != start_ms
            or piece_provenance.get("end_ms") != end_ms
            or piece_provenance.get("output_path") != str(piece_path.resolve())
        ):
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_PROVENANCE_DRIFT")
        piece_sha256 = _continuation_sha256_path(
            piece_path, label="CURRENT_SOURCE_PIECE_MEDIA"
        )
        _continuation_require_sha(
            piece_sha256,
            piece_provenance.get("output_sha256"),
            label="CURRENT_SOURCE_PIECE_MEDIA",
        )

        padded_input = padded_inputs[index]
        if (
            not isinstance(padded_input, Mapping)
            or padded_input.get("path") != str(piece_path.resolve())
        ):
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_PADDED_INPUT_PATH_DRIFT")
        _continuation_require_sha(
            piece_sha256,
            padded_input.get("sha256"),
            label="CURRENT_AUTO_RESUME_PADDED_INPUT",
        )
        source_media_sha256s.append("sha256:" + source_sha)
        validated_piece_rows.append(dict(piece_provenance))

    if durations is not None and isinstance(spec, dict):
        from src.autoslice.producer_source_media import source_piece_clock

        spec["source_piece_clock"] = source_piece_clock(
            validated_piece_rows, durations, padded_sha256=padded_sha256,
        )
    return padded_sha256, tuple(source_media_sha256s)


def _retained_source_boundary_artifact(*, out_root: Path, cid: str,
        retained: Mapping[str, Any], current_authority: Mapping[str, Any],
        current_native: Mapping[str, Any]) -> dict[str, str]:
    """Recover the boundary original from its parent and frozen source stage.

    The source-stage display and the wider source witness have different grids.
    Rebuilding the existing projection proves the exact wider bytes; matching a
    witness grid alone never selects or authorizes a replacement transcript.
    """
    from src.autoslice.producer_text_checkpoint import SOURCE_SCHEMA
    from src.autoslice.retained_reviewed_draft import build_retained_source_boundary_context

    source, _ = _continuation_read_json(out_root / f'{cid}.source-text-checkpoint.json', label='SOURCE_TEXT_CHECKPOINT')
    if (source.get('schema_version') != SOURCE_SCHEMA or source.get('candidate_id') != cid
            or source.get('time_domain') != 'padded_local' or source.get('release_authorized') is not False
            or source.get('padded') != current_native.get('padded')
            or source.get('context', {}).get('sha256') != current_native.get('context', {}).get('sha256')):
        raise CurrentTextContinuationError('CURRENT_SOURCE_BOUNDARY_STAGE_BINDING_DRIFT')
    def artifact(key):
        binding = source.get(key) or {}
        path = Path(str(binding.get('path') or ''))
        _continuation_require_sha(_continuation_sha256_path(path, label='SOURCE_' + key.upper()),
                                  binding.get('sha256'), label='SOURCE_' + key.upper())
        return path
    stage_path, stage_authority_path = artifact('srt'), artifact('authority')
    artifact('context')
    try:
        stage = review_from_bytes(stage_authority_path.read_bytes(), directory=stage_authority_path.parent)
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentTextContinuationError('CURRENT_SOURCE_BOUNDARY_STAGE_AUTHORITY_INVALID') from exc
    stage_text = stage_path.read_text(encoding='utf-8')
    _continuation_require_sha(hashlib.sha256(stage_text.encode()).hexdigest(),
                              stage.get('final_output_srt_sha256'), label='SOURCE_STAGE_OUTPUT')
    if stage.get('post_transcript_entity_output_srt_sha256') != current_authority.get('post_transcript_entity_output_srt_sha256'):
        raise CurrentTextContinuationError('CURRENT_SOURCE_BOUNDARY_STAGE_PARENT_DRIFT')
    for field in ('source_srt_sha256', 'parent_authority_sha256', 'parent_clip_context_sha256',
                  'filler_plan_sha256', 'padded_sha256', 'source_timeline_offset_ms', 'source_endpoint'):
        expected = retained['proof'].get(field)
        if expected is None or any((a.get('retained_reviewed_draft') or {}).get(field) != expected
                                   for a in (stage, current_authority)):
            raise CurrentTextContinuationError('CURRENT_SOURCE_BOUNDARY_STAGE_PARENT_DRIFT')
    cues, _ = build_retained_source_boundary_context(retained, stage_text)
    expected_raw = _render_cues_to_srt(cues).encode()
    path = artifact('boundary_srt') if source.get('boundary_srt') else out_root / f'{cid}.source-boundary-context.srt'
    actual_sha = _continuation_sha256_path(path, label='CURRENT_SOURCE_BOUNDARY_ORIGINAL')
    if path.read_bytes() != expected_raw:
        raise CurrentTextContinuationError('CURRENT_SOURCE_BOUNDARY_ORIGINAL_DRIFT')
    witness = ((current_authority.get('final_review_audit') or {}).get('boundary_semantic_review') or {}).get('source_separation_witness') or {}
    if witness.get('source_cue_grid_sha256') != cue_grid_sha256(cues):
        # A pending current content review may not have reached the final
        # boundary stage. Its explicitly frozen source context is still an
        # input, distinct from the source-stage display SRT. Prove that exact
        # context from the original stage; never import its PASS as delivery.
        source_review = (stage.get('final_review_audit') or {}).get('boundary_semantic_review') or {}
        source_context = (stage.get('final_review_audit') or {}).get('source_boundary_context') or {}
        explicit = source.get('boundary_srt') or {}
        current_binding = (current_native.get('source_clock') or {}).get('boundary_srt') or {}
        current_review = current_authority.get('final_review_audit') or {}
        current_boundary = current_review.get('boundary_semantic_review') or {}
        same_source_scope = (current_boundary.get('status') == 'PASS'
            and current_boundary.get('review_scope') == 'source_full_window'
            and current_boundary.get('request_sha256') == source_review.get('request_sha256')
            and current_boundary.get('cue_grid_sha256') == cue_grid_sha256(cues))
        if (witness or (current_review.get('release_gate') != 'BLOCK' and not same_source_scope)
                or source_review.get('status') != 'PASS' or source_review.get('review_scope') != 'source_full_window'
                or source_review.get('cue_grid_sha256') != cue_grid_sha256(cues)
                or re.fullmatch(r'sha256:[0-9a-f]{64}', str(source_review.get('request_sha256') or '')) is None
                or source_context.get('schema_version') != 'source-boundary-transcript-context.v1'
                or source_context.get('source_srt_sha256') != 'sha256:' + actual_sha
                or source_context.get('source_cue_grid_sha256') != cue_grid_sha256(cues)
                or explicit.get('sha256') != 'sha256:' + actual_sha
                or current_binding.get('sha256') != explicit.get('sha256')):
            raise CurrentTextContinuationError('CURRENT_SOURCE_BOUNDARY_GRID_DRIFT')
    return {'path':str(path.resolve()), 'sha256':'sha256:' + actual_sha}


def _retained_source_successor(source_path: Path, current: Mapping[str, Any], *, cid: str) -> bool:
    """Select a saved source generation only through its immutable parent chain."""
    from src.autoslice.producer_text_checkpoint import SOURCE_SCHEMA
    source, _ = _continuation_read_json(source_path, label='SOURCE_TEXT_CHECKPOINT')
    if (source.get('schema_version') != SOURCE_SCHEMA or source.get('candidate_id') != cid
            or source.get('time_domain') != 'padded_local' or source.get('release_authorized') is not False
            or source.get('padded') != current.get('padded')):
        return False
    binding = source.get('authority') or {}
    path = Path(str(binding.get('path') or ''))
    authority, sha = _continuation_read_json(path, label='SOURCE_SUCCESSOR_AUTHORITY', review=True)
    _continuation_require_sha(sha, binding.get('sha256'), label='SOURCE_SUCCESSOR_AUTHORITY')
    if not (authority.get('human_sequence_alignment') or {}).get('records'):
        return False
    visited = set()
    while True:
        parent = (authority.get('retained_reviewed_draft') or {}).get('latest_current_parent')
        if not isinstance(parent, Mapping):
            return False
        key = (parent.get('authority_sha256'), parent.get('srt_sha256'))
        if key in visited:
            raise CurrentTextContinuationError('SOURCE_SUCCESSOR_PARENT_CYCLE')
        visited.add(key)
        parent_authority, parent_sha = _continuation_read_json(
            Path(parent['authority_path']), label='SOURCE_SUCCESSOR_PARENT', review=True,
        )
        _continuation_require_sha(parent_sha, parent.get('authority_sha256'), label='SOURCE_SUCCESSOR_PARENT')
        text_sha = _continuation_sha256_path(Path(parent['srt_path']), label='SOURCE_SUCCESSOR_PARENT_SRT')
        _continuation_require_sha(text_sha, parent.get('srt_sha256'), label='SOURCE_SUCCESSOR_PARENT_SRT')
        _continuation_require_sha(text_sha, parent_authority.get('final_output_srt_sha256'), label='SOURCE_SUCCESSOR_PARENT_OUTPUT')
        if (key == ((current.get('authority') or {}).get('sha256'), (current.get('srt') or {}).get('sha256'))):
            return True
        authority = parent_authority


def _retained_language_reference(snapshot: Mapping[str, Any], *, cid: str) -> str | None:
    """Read foreign words from the validated current ancestor, never new ASR."""
    ancestor=snapshot.get('source_ancestor_checkpoint')
    if not isinstance(ancestor, Mapping):
        return None
    values={}
    for key in ('srt','authority','context'):
        binding=ancestor[key];path=Path(binding['path'])
        _continuation_require_sha(_continuation_sha256_path(path,label='RETAINED_LANGUAGE_'+key.upper()),
                                  binding['sha256'],label='RETAINED_LANGUAGE_'+key.upper())
        values[key] = path.read_bytes() if key == 'authority' else path.read_text(encoding='utf-8')
    try:
        authority = review_from_bytes(
            values['authority'], directory=Path(ancestor['authority']['path']).parent,
        )
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentTextContinuationError('RETAINED_LANGUAGE_AUTHORITY_INVALID') from exc
    context=json.loads(values['context'])
    validate_clip_context(context,candidate_id=cid,source_media_sha256s=snapshot['source_media_sha256s'])
    review=_continuation_validate_existing_authority(authority,srt_text=values['srt'],
        srt_sha256=ancestor['srt']['sha256'].removeprefix('sha256:'),clip_context=context,candidate_id=cid)
    projected,_=_continuation_project_delivery_to_source(parse_srt_cues(values['srt']),final_review_audit=review,
        authority=authority)
    return _render_cues_to_srt(projected)


def _source_checkpoint_reproduces_delivery(
    *, out_root: Path, cid: str, source_text: str,
    source_authority: Mapping[str, Any], live_authority: Mapping[str, Any],
    allow_unresolved_current: bool = False,
) -> bool:
    """Keep a completed source input when only a delivery transport failed.

    Replay the ordinary timing actions and clip geometry byte-for-byte. Any
    new word or unmatched timing change requires the normal CURRENT reader.
    Cumulative usage is carried separately; this grants no boundary PASS.
    """
    try:
        review = live_authority.get("final_review_audit") or {}
        boundary = review.get("boundary_semantic_review") or {}
        transport_only = (review.get("reason_codes") == ["FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"]
            and review.get("findings") == [] and review.get("validated_finding_count") == 0
            and _pf.transport_unavailable_reason(boundary.get("reason_codes")))
        pending_current = (allow_unresolved_current
            and only_finding_failures(review.get("reason_codes"))
            and isinstance(review.get("findings"), list) and bool(review["findings"])
            and review.get("validated_finding_count") == len(review["findings"]))
        if (review.get("status") != "FLAGGED" or review.get("release_gate") != "BLOCK"
                or not (transport_only or pending_current)):
            return False
        count = (source_authority.get("cpa_adjudication_usage") or {}).get("provider_adjudication_count")
        live_count = (live_authority.get("cpa_adjudication_usage") or {}).get("provider_adjudication_count")
        if (type(live_count) is not int or live_count < 0
                or (count is not None and (type(count) is not int or not 0 <= count <= live_count))):
            return False
        path = Path(live_authority.get("final_text_srt_path") or out_root / "replacement_recuts" / f"{cid}.recut.srt")
        if not path.resolve().is_relative_to(out_root.resolve()):
            return False
        delivery = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(delivery.encode()).hexdigest()
        if (live_authority.get("final_output_srt_sha256") != digest
                or review.get("reviewed_srt_sha256") != "sha256:" + digest):
            return False
        resolved, _ = _continuation_read_json(out_root / f"{cid}.boundary_audit.json", label="SOURCE_REPLAY_BOUNDARY")
        timing, _ = _continuation_read_json(out_root / "replacement_recuts" / f"{cid}.recut.timing_qa.json", label="SOURCE_REPLAY_TIMING")
        start, end = resolved.get("final_start_ms"), resolved.get("final_end_ms")
        semantic = resolved.get("boundary_semantic_review") or {}
        endpoint = semantic.get("final_endpoint_binding") or {}
        cues = list(parse_srt_cues(source_text))
        if (type(start) is not int or type(end) is not int or not 0 <= start < end
                or resolved.get("schema_version") != "boundary-audit.v1"
                or semantic.get("status") != "PASS" or semantic.get("review_scope") != "source_full_window"
                or semantic.get("cue_grid_sha256") != cue_grid_sha256(cues)
                or endpoint.get("status") != "PASS" or endpoint.get("final_start_ms") != start
                or endpoint.get("final_end_ms") != end
                or timing.get("schema_version") != "subtitle-timing-qa.v3"
                or timing.get("window") != {"start_ms": start, "end_ms": end}):
            return False
        seen = set()
        for action in timing["actions"]:
            if not isinstance(action, Mapping):
                return False
            before, after = action["before"], action["after"]
            if (not isinstance(before, Mapping) or not isinstance(after, Mapping)
                    or any(type(before[k]) is not int for k in ("start_ms", "end_ms"))):
                return False
            matches = [i for i, cue in enumerate(cues)
                       if (cue.start_ms, cue.end_ms) == (before["start_ms"], before["end_ms"])]
            if action.get("action") != "retime" or len(matches) != 1 or matches[0] in seen:
                return False
            index = matches[0]; cue = cues[index]; seen.add(index)
            if (action.get("cue_id") != f"fresh_{int(cue.index):04d}"
                    or action.get("text") != cue.text[:40]
                    or any(type(after[k]) is not int for k in ("start_ms", "end_ms"))
                    or not 0 <= after["start_ms"] < after["end_ms"]):
                return False
            cues[index] = SrtCue(index=cue.index, start_ms=after["start_ms"], end_ms=after["end_ms"], text=cue.text)
        cues.sort(key=lambda cue: (cue.start_ms, cue.end_ms, int(cue.index)))
        from src.autoslice.recut_materialization import BOUNDARY_CUE_START_TOLERANCE_MS, BOUNDARY_CLIPPED_CUE_MAX_VISIBLE_MS
        projected = []
        for i, cue in enumerate(cues):
            cue_end = cue.end_ms
            if cue_end > start and cue.start_ms < end:
                cue_end = max(cue.start_ms + 1, min(cue_end, end, cues[i + 1].start_ms if i + 1 < len(cues) else end))
            left, right = max(cue.start_ms, start), min(cue_end, end)
            if right <= left or (left - start <= BOUNDARY_CUE_START_TOLERANCE_MS and right - left <= BOUNDARY_CLIPPED_CUE_MAX_VISIBLE_MS):
                continue
            projected.append((left - start, right - start, cue.text.strip()))
        return projected == [(cue.start_ms, cue.end_ms, cue.text) for cue in parse_srt_cues(delivery)]
    except (OSError, KeyError, TypeError, ValueError, CurrentTextContinuationError):
        return False


def _automatic_current_checkpoint_snapshot(
    *,
    spec: Mapping[str, object],
    durations: list[int],
    padded: Path,
    padded_dur: int,
    cid: str,
    out_root: Path,
    validated_retained: Mapping[str, Any] | None = None,
    source_truth_ledger_path: Path | None = None,
) -> dict[str, Any] | None:
    """Load the immutable normal checkpoint surfaces before choosing a lane.

    The same three files are shared by the source-text and completed-delivery
    lanes.  Keep their byte/provenance/context checks in one loader, but leave
    the lane-specific authority validators separate: a normal v1 text-stage
    audit is an input to the next producer stages, while a v2 exact-final
    audit is a different release contract.
    """

    from src.autoslice.producer_text_checkpoint import CURRENT_SCHEMA, SOURCE_SCHEMA, PARTIAL_SCHEMA, load_post_transcript_text
    from src.autoslice.retained_preparation_checkpoint import RETAINED_PARTIAL_SCHEMA, validate_retained_partial

    native_path = out_root / f"{cid}.current-text-checkpoint.json"
    declared = spec.get("current_text_checkpoint")
    source_declared = spec.get("source_text_checkpoint")
    if source_declared is not None:
        from src.autoslice.source_checkpoint_parent import validate_source_checkpoint_parent
        if declared is not None or validate_source_checkpoint_parent(spec, runtime=True) is None:
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_CHECKPOINT_BINDING_INVALID")
        # Prefer a real successor emitted by this new attempt after its parent
        # has been validated. Never repeatedly restore an older SOURCE over it.
        if not any(path.exists() or path.is_symlink() for path in (
                native_path, out_root / f"{cid}.source-text-checkpoint.json",
                out_root / f"{cid}.partial-source-text-checkpoint.json")):
            declared = source_declared
    if declared is None and not (native_path.exists() or native_path.is_symlink()):
        native_path = out_root / f"{cid}.source-text-checkpoint.json"
        partial_path = out_root / f'{cid}.partial-source-text-checkpoint.json'
        if partial_path.exists() or partial_path.is_symlink():
            partial, _ = _continuation_read_json(partial_path, label='PARTIAL_SOURCE_TEXT_CHECKPOINT')
            parent = partial.get('source_checkpoint_parent') or {}
            if (not native_path.exists() or parent.get('sha256') == 'sha256:' +
                    _continuation_sha256_path(native_path, label='SOURCE_TEXT_CHECKPOINT')):
                native_path = partial_path
    native = None
    inline_partial_text = None
    source_successor = False
    source_ancestor = None
    source_usage_parent = None
    source_observation_parent = None
    if declared is not None or native_path.exists() or native_path.is_symlink():
        if declared is not None:
            if not isinstance(declared, Mapping) or not declared.get("path"):
                raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_CHECKPOINT_BINDING_INVALID")
            native_path = Path(str(declared["path"]))
        native, native_sha = _continuation_read_json(native_path, label="CURRENT_TEXT_CHECKPOINT")
        source_path = out_root / f'{cid}.source-text-checkpoint.json'
        if (declared is None and validated_retained is not None
                and native.get('schema_version') == CURRENT_SCHEMA and source_path.is_file()
                and _retained_source_successor(source_path, native, cid=cid)):
            source_ancestor = native
            native, native_sha = _continuation_read_json(source_path, label='SOURCE_TEXT_CHECKPOINT')
            source_successor = True
        if declared is not None:
            _continuation_require_sha(native_sha, declared.get("sha256"), label="CURRENT_TEXT_CHECKPOINT")
        expected_domain = {CURRENT_SCHEMA: "delivery_local", SOURCE_SCHEMA: "padded_local", PARTIAL_SCHEMA:"padded_local", RETAINED_PARTIAL_SCHEMA: "padded_local"}.get(native.get("schema_version"))
        if (expected_domain is None or (declared is not None and native.get("schema_version")
                != (SOURCE_SCHEMA if declared is source_declared else CURRENT_SCHEMA))
                or native.get("candidate_id") != cid
                or native.get("time_domain") != expected_domain
                or native.get("release_authorized") is not False):
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_CHECKPOINT_BINDING_INVALID")

        def native_artifact(key: str) -> Path:
            binding = native.get(key)
            if not isinstance(binding, Mapping) or not binding.get("path"):
                raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_CHECKPOINT_BINDING_INVALID")
            path = Path(str(binding["path"]))
            _continuation_require_sha(_continuation_sha256_path(path, label=key.upper()),
                                      binding.get("sha256"), label=key.upper())
            return path

        current_srt_path = native_artifact("srt")
        chat_authority_path = native_artifact("authority")
        clip_context_path = native_artifact("context")
        if native.get('schema_version') in {PARTIAL_SCHEMA, RETAINED_PARTIAL_SCHEMA}:
            if (native.get('completed_stage'), native.get('pending_stage')) not in {
                ('post_review','source_finalization'), ('post_entity','review_and_source_finalization'),
            }:
                raise CurrentTextContinuationError('PARTIAL_SOURCE_TEXT_STAGE_INVALID')
            if native.get('schema_version') == PARTIAL_SCHEMA:
                native_artifact('language_witness')
            native_artifact('name_reference')
            if 'source_checkpoint_parent' in native:
                native_artifact('source_checkpoint_parent')
            if 'budget' in native:
                prior, _ = _continuation_read_json(native_artifact('budget'), label='PARTIAL_NATIVE_BUDGET')
                from src.autoslice.retained_source_input import load_retained_source_input
                retained_input = load_retained_source_input(spec=spec, cid=cid, padded=padded, out_root=out_root)
                live_path = retained_input.native.budget_receipt if retained_input else out_root/'native-audio-budget.json'
                live, _ = _continuation_read_json(live_path, label='CURRENT_NATIVE_BUDGET')
                from src.autoslice.native_audio_budget_receipt import _require_budget_history_extension, _validate_existing
                media_sha = _continuation_sha256_path(padded, label='CURRENT_PADDED_MEDIA')
                try:
                    _validate_existing(prior, source_media_sha256=media_sha)
                    _validate_existing(live, source_media_sha256=media_sha)
                    _require_budget_history_extension(prior, live)
                except Exception as exc:
                    raise CurrentTextContinuationError('PARTIAL_NATIVE_BUDGET_DRIFT') from exc
        if _continuation_sha256_path(padded, label="CURRENT_PADDED_MEDIA") != _continuation_sha256_path(
                native_artifact("padded"), label="CHECKPOINT_PADDED_MEDIA"):
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_CHECKPOINT_PADDED_DRIFT")
    else:
        current_srt_path = out_root / "padded.fresh.srt"
        chat_authority_path = out_root / f"{cid}.chat-authority.json"
        clip_context_path = out_root / f"{cid}.clip-context.json"
        if (not current_srt_path.exists() and chat_authority_path.is_file() and clip_context_path.is_file()):
            unfinished, _ = _continuation_read_json(
                chat_authority_path, label='PARTIAL_CHAT_AUTHORITY', review=True,
            )
            # Only the native emitter's exact retained text can bridge a
            # pre-finalization interruption. A declared final output is never
            # treated as partial merely because its SRT is absent or drifted.
            if not unfinished.get('final_output_srt_sha256'):
                try:
                    inline_partial_text = load_post_transcript_text(unfinished)
                except ValueError as exc:
                    raise CurrentTextContinuationError('PARTIAL_SOURCE_TEXT_DRIFT') from exc
                if inline_partial_text is not None:
                    current_srt_path = out_root/'partial-source-text-input'/('srt.' +
                        hashlib.sha256(inline_partial_text.encode()).hexdigest()+'.srt')
    checkpoint_paths = (current_srt_path, chat_authority_path, clip_context_path)
    present = tuple(path for path in checkpoint_paths if path.exists() or path.is_symlink()
                    or (inline_partial_text is not None and path == current_srt_path))
    if not present:
        return None
    if len(present) != len(checkpoint_paths):
        missing = ",".join(path.name for path in checkpoint_paths if path not in present)
        raise CurrentTextContinuationError(
            f"CURRENT_AUTO_RESUME_CHECKPOINT_INCOMPLETE:{missing}"
        )

    srt_sha256 = (hashlib.sha256(inline_partial_text.encode()).hexdigest() if inline_partial_text is not None
                  else _continuation_sha256_path(current_srt_path, label="CURRENT_SRT"))
    try:
        srt_text = inline_partial_text if inline_partial_text is not None else current_srt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SRT_UNREADABLE") from exc
    try:
        cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    except (TypeError, ValueError) as exc:
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SRT_PARSE_INVALID") from exc
    if not cues:
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SRT_EMPTY")
    if any(cue.start_ms < 0 or cue.end_ms <= cue.start_ms or cue.end_ms > padded_dur for cue in cues):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_GRID_INVALID")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in durations
    ):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_DURATION_BINDING_INVALID")

    retained_piece_paths = None
    if validated_retained is None:
        padded_sha256, source_media_sha256s = _automatic_current_source_bindings(
            spec=spec, padded=padded, out_root=out_root, durations=durations,
        )
        if spec.get("retained_source_input") is not None or spec.get("retained_current_text") is not None:
            retained_piece_paths = [
                padded.parent / f"piece_{index}_{piece['start_ms']}_{piece['end_ms']}.mp4"
                for index, piece in enumerate(spec["pieces"])
            ]
    else:
        # The retained loader has already replayed the original piece/padded
        # provenance. Its real media can live in the immutable parent folder.
        padded_sha256 = _continuation_sha256_path(padded, label="CURRENT_PADDED_MEDIA")
        _continuation_require_sha(padded_sha256, validated_retained["padded_sha256"], label="CURRENT_PADDED_MEDIA")
        source_media_sha256s = tuple(validated_retained["source_media_sha256s"])
        provenance, _ = _continuation_read_json(padded.with_suffix(".provenance.json"),
                                               label="CURRENT_PADDED_PROVENANCE")
        inputs, expected_pieces = provenance.get("inputs"), validated_retained["piece_output_sha256s"]
        if (provenance.get("output_path") != str(padded.resolve())
                or not isinstance(inputs, list) or len(inputs) != len(expected_pieces)
                or len(inputs) != len(durations)):
            raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_RETAINED_MEDIA_DRIFT")
        _continuation_require_sha(padded_sha256, provenance.get("output_sha256"), label="CURRENT_PADDED_MEDIA")
        retained_piece_paths = []
        for row, expected in zip(inputs, expected_pieces):
            if not isinstance(row, Mapping) or not row.get("path"):
                raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_RETAINED_MEDIA_DRIFT")
            piece_path = Path(row["path"])
            actual = _continuation_sha256_path(piece_path, label="CURRENT_RETAINED_PIECE")
            _continuation_require_sha(actual, expected, label="CURRENT_RETAINED_PIECE")
            _continuation_require_sha(actual, row.get("sha256"), label="CURRENT_RETAINED_PIECE")
            retained_piece_paths.append(piece_path)
    if sum(durations) != padded_dur and not _source_piece_container_clock_matches(
        padded, sum(durations), padded_dur, allow_audio_packet_rounding=True,
    ):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_DURATION_BINDING_INVALID")
    chat_authority, chat_authority_sha256 = _continuation_read_json(
        chat_authority_path, label="CURRENT_CHAT_AUTHORITY", review=True
    )
    if (native or {}).get('schema_version') == RETAINED_PARTIAL_SCHEMA:
        validate_retained_partial(native=native, authority=chat_authority, retained=validated_retained)
    if validated_retained is not None:
        prior_retained = chat_authority.get("retained_reviewed_draft") or {}
        proof = validated_retained["proof"]
        for field in ("source_srt_sha256", "parent_authority_sha256", "parent_clip_context_sha256",
                      "filler_plan_sha256", "padded_sha256", "source_timeline_offset_ms", "source_endpoint"):
            if proof.get(field) is None or prior_retained.get(field) != proof[field]:
                raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_RETAINED_PARENT_DRIFT")
    clip_context, clip_context_sha256 = _continuation_read_json(
        clip_context_path, label="CURRENT_CLIP_CONTEXT"
    )
    try:
        clip_context = validate_clip_context(
            clip_context,
            candidate_id=cid,
            recording_date=str(spec.get("date") or "") or None,
            source_media_sha256s=source_media_sha256s,
        )
    except (TypeError, ValueError) as exc:
        raise CurrentTextContinuationError(
            f"CURRENT_AUTO_RESUME_CLIP_CONTEXT_INVALID:{exc}"
        ) from exc
    _validate_current_source_context(
        spec=spec,
        durations=durations,
        padded_dur=padded_dur,
        srt_text=srt_text,
        clip_context=clip_context,
        piece_media_paths=retained_piece_paths or [
            out_root / f"piece_{index}_{piece['start_ms']}_{piece['end_ms']}.mp4"
            for index, piece in enumerate(spec["pieces"])
        ],
        padded_media_path=padded,
    )
    completed_retained_input = None
    from src.autoslice.retained_preparation_checkpoint import replayed_input_present
    if replayed_input_present(chat_authority):
        from src.autoslice.retained_preparation_checkpoint import (
            load_completed_retained_review, validate_completed_retained_input_authority)
        if validated_retained is None:
            raise CurrentTextContinuationError("RETAINED_PREPARATION_INPUT_REPLAY_UNVERIFIED")
        completed_retained_input = load_completed_retained_review(
            retained=validated_retained, out_root=out_root, cid=cid,
            clip_context=clip_context, required=True, spec=spec, durations=durations,
            padded=padded, padded_dur=padded_dur)
        validate_completed_retained_input_authority(
            completed_retained_input, authority=chat_authority, clip_context=clip_context)
    if (native is not None and native.get('schema_version') == CURRENT_SCHEMA
            and ((chat_authority.get('final_review_audit') or {}).get('boundary_semantic_review') or {}).get('review_scope') == 'source_full_window'):
        live_path = out_root / f'{cid}.chat-authority.json'
        if live_path.is_file() and not live_path.is_symlink():
            live, live_sha = _continuation_read_json(
                live_path, label='CURRENT_COMPLETED_SUCCESSOR', review=True,
            )
            review = live.get('final_review_audit') or {}
            boundary = review.get('boundary_semantic_review') or {}
            if (live_sha != chat_authority_sha256 and review.get('status') == 'CLEAN'
                    and review.get('release_gate') == 'PASS' and boundary.get('review_scope') == 'final_delivery'):
                delivery = Path(live.get('final_text_srt_path') or out_root / 'replacement_recuts' / f'{cid}.recut.srt')
                if not delivery.resolve().is_relative_to(out_root.resolve()):
                    raise CurrentTextContinuationError('CURRENT_AUTO_RESUME_DELIVERY_PATH_INVALID')
                delivery_sha = _continuation_sha256_path(delivery, label='CURRENT_COMPLETED_SRT')
                original_source_sha = _continuation_normalize_sha(chat_authority.get('post_transcript_entity_output_srt_sha256'))
                if (delivery_sha != srt_sha256 or re.fullmatch(r'[0-9a-f]{64}', original_source_sha) is None
                        or _continuation_normalize_sha(live.get('post_transcript_entity_output_srt_sha256'))
                        != original_source_sha):
                    raise CurrentTextContinuationError('CURRENT_COMPLETED_SUCCESSOR_PARENT_DRIFT')
                if validated_retained is not None and any(
                    (live.get('retained_reviewed_draft') or {}).get(key) != validated_retained['proof'].get(key)
                    for key in ('source_srt_sha256','parent_authority_sha256','parent_clip_context_sha256',
                                'filler_plan_sha256','padded_sha256','source_timeline_offset_ms','source_endpoint')):
                    raise CurrentTextContinuationError('CURRENT_COMPLETED_SUCCESSOR_PARENT_DRIFT')
                validate_final_review_release(review, expected_srt_sha256='sha256:' + delivery_sha)
                clock = native.get('source_clock') or {}
                if any(not isinstance(clock.get(key), Mapping) for key in ('authority','srt')):
                    raise CurrentTextContinuationError('CURRENT_COMPLETED_SUCCESSOR_PARENT_DRIFT')
                clock_kwargs = {}
                for key, field in (('authority','source_clock_authority'),('srt','source_clock_srt'),
                        ('boundary_srt','source_boundary_srt'),('timing_resolution','source_timing_resolution'),
                        ('context_job','source_context_job'),('context_manifest','source_context_manifest')):
                    if isinstance(clock.get(key), Mapping):
                        clock_kwargs[field + '_path'] = Path(clock[key]['path'])
                        clock_kwargs['expected_' + field + '_sha256'] = clock[key]['sha256']
                clock_review, clock_reference = _continuation_source_clock_reference(
                    _current_checkpoint_clock_namespace(clock_kwargs), chat_authority=live,
                    srt_text=delivery.read_text(encoding='utf-8'), srt_sha256=delivery_sha,
                    clip_context=clip_context, candidate_id=cid)
                _continuation_validate_existing_authority(live, srt_text=delivery.read_text(encoding='utf-8'),
                    srt_sha256=delivery_sha, clip_context=clip_context, candidate_id=cid,
                    source_clock_review=clock_review,
                    source_clock_srt_sha256=(clock_reference or {}).get('reviewed_srt_sha256'))
                from src.autoslice.producer_text_checkpoint import write_current_text_checkpoint
                write_current_text_checkpoint(out_root / f'{cid}.current-text-checkpoint.json',
                    candidate_id=cid, srt_path=delivery, authority_path=live_path,
                    context_path=clip_context_path, padded=padded, source_clock=copy.deepcopy(native['source_clock']))
                return _automatic_current_checkpoint_snapshot(spec=spec,durations=durations,padded=padded,
                    padded_dur=padded_dur,cid=cid,out_root=out_root,validated_retained=validated_retained,
                    source_truth_ledger_path=source_truth_ledger_path)
    if not source_successor and (native is None or native.get("schema_version") == SOURCE_SCHEMA):
        # A normal postburn repair can finish after the source checkpoint was
        # frozen. Adopt that actual delivery before restoring the older source
        # text. Its existing owner/typed/clock validators remain authoritative.
        live_authority_path = out_root / f"{cid}.chat-authority.json"
        if live_authority_path.is_file() and not live_authority_path.is_symlink():
            live_authority, live_authority_sha = _continuation_read_json(
                live_authority_path, label="CURRENT_CHAT_AUTHORITY", review=True,
            )
            live_review = live_authority.get("final_review_audit")
            from src.autoslice.producer_source_truth_checkpoint import added_local_term_review_required
            added_terms = (spec.get('retained_source_input') is not None
                and str(spec.get('human_truth_mode') or 'delivery') == 'delivery'
                and added_local_term_review_required(chat_authority, spec=spec, durations=durations,
                    ledger_path=source_truth_ledger_path, current_text=srt_text))
            replay_source = (native is not None and native.get("schema_version") == SOURCE_SCHEMA
                and chat_authority.get("post_transcript_entity_output_srt_sha256")
                and live_authority.get("post_transcript_entity_output_srt_sha256") == chat_authority.get("post_transcript_entity_output_srt_sha256")
                and _source_checkpoint_reproduces_delivery(out_root=out_root, cid=cid, source_text=srt_text,
                    source_authority=chat_authority, live_authority=live_authority,
                    allow_unresolved_current=added_terms))
            if replay_source:
                source_usage_parent = {"path": str(live_authority_path), "sha256": "sha256:" + live_authority_sha,
                    "role": "CUMULATIVE_USAGE_ONLY"}
                if added_terms and (live_review or {}).get('findings'):
                    source_observation_parent = {**source_usage_parent,
                        "role": "PENDING_OBSERVATIONS_ONLY"}
            if (isinstance(live_review, Mapping) and live_review.get("schema_version") == FINAL_REVIEW_SCHEMA_VERSION
                    and not replay_source):
                delivery_path = Path(live_authority.get("final_text_srt_path") or
                                     out_root / "replacement_recuts" / f"{cid}.recut.srt")
                if not delivery_path.resolve().is_relative_to(out_root.resolve()):
                    raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_DELIVERY_PATH_INVALID")
                delivery_sha = _continuation_sha256_path(delivery_path, label="CURRENT_DELIVERY_SRT")
                delivery_text = delivery_path.read_text(encoding="utf-8")
                if (native is not None and live_authority.get("post_transcript_entity_output_srt_sha256")
                        != chat_authority.get("post_transcript_entity_output_srt_sha256")):
                    raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_DELIVERY_SOURCE_DRIFT")
                validated = _continuation_validate_existing_authority(
                    live_authority, srt_text=delivery_text, srt_sha256=delivery_sha,
                    clip_context=clip_context, candidate_id=cid,
                )
                _continuation_project_delivery_to_source(
                    parse_srt_cues(delivery_text), final_review_audit=validated, authority=live_authority,
                )
                from src.autoslice.producer_text_checkpoint import _frozen_binding, write_current_text_checkpoint
                clock_root = out_root / "current-text-clock"
                clock = {
                    "authority": _frozen_binding(clock_root, "authority", live_authority_path),
                    "srt": _frozen_binding(clock_root, "srt", delivery_path),
                    "boundary_srt": _frozen_binding(clock_root, "boundary_srt", Path(
                        _retained_source_boundary_artifact(out_root=out_root, cid=cid,
                            retained=validated_retained, current_authority=live_authority,
                            current_native=native)['path']) if validated_retained is not None and native is not None
                        else current_srt_path),
                }
                write_current_text_checkpoint(
                    out_root / f"{cid}.current-text-checkpoint.json", candidate_id=cid,
                    srt_path=delivery_path, authority_path=live_authority_path,
                    context_path=clip_context_path, padded=padded, source_clock=clock,
                )
                return _automatic_current_checkpoint_snapshot(
                    spec=spec, durations=durations, padded=padded, padded_dur=padded_dur,
                    cid=cid, out_root=out_root, validated_retained=validated_retained,
                    source_truth_ledger_path=source_truth_ledger_path,
                )
    if (native is not None and native.get('schema_version') == CURRENT_SCHEMA and validated_retained is not None
            and (native.get('source_clock') or {}).get('boundary_srt')):
        boundary_binding = (native.get('source_clock') or {}).get('boundary_srt') or {}
        boundary_path = Path(str(boundary_binding.get('path') or ''))
        boundary_sha = _continuation_sha256_path(boundary_path, label='CURRENT_SOURCE_BOUNDARY_ARTIFACT')
        _continuation_require_sha(boundary_sha, boundary_binding.get('sha256'), label='CURRENT_SOURCE_BOUNDARY_ARTIFACT')
        witness = ((chat_authority.get('final_review_audit') or {}).get('boundary_semantic_review') or {}).get('source_separation_witness') or {}
        if cue_grid_sha256(parse_srt_cues(boundary_path.read_text())) != witness.get('source_cue_grid_sha256'):
            source, _ = _continuation_read_json(out_root / f'{cid}.source-text-checkpoint.json', label='SOURCE_TEXT_CHECKPOINT')
            if boundary_binding.get('sha256') not in {
                (source.get('srt') or {}).get('sha256'), (source.get('boundary_srt') or {}).get('sha256'),
            }:
                raise CurrentTextContinuationError('CURRENT_SOURCE_BOUNDARY_GRID_DRIFT')
            bound = _retained_source_boundary_artifact(out_root=out_root, cid=cid,
                retained=validated_retained, current_authority=chat_authority, current_native=native)
            native = copy.deepcopy(native)
            native['source_clock']['boundary_srt'] = bound
    source_accounting_parent = None
    if source_declared is not None:
        from src.autoslice.source_checkpoint_parent import validate_source_checkpoint_parent
        source_accounting_parent = validate_source_checkpoint_parent(spec, runtime=True)
        # Only cumulative usage and immutable observation history cross this
        # input boundary. A failed semantic verdict is not a successor PASS.
        source_usage_parent = {**source_accounting_parent["live_authority"],
            "role": "CUMULATIVE_USAGE_ONLY",
            "source_checkpoint_parent": source_accounting_parent}
    return {
        "current_srt_path": current_srt_path,
        "chat_authority_path": chat_authority_path,
        "clip_context_path": clip_context_path,
        "srt_sha256": srt_sha256,
        "srt_text": srt_text,
        "cues": cues,
        "padded_sha256": padded_sha256,
        "source_media_sha256s": source_media_sha256s,
        "chat_authority": chat_authority,
        "chat_authority_sha256": chat_authority_sha256,
        "clip_context": clip_context,
        "clip_context_sha256": clip_context_sha256,
        "native_checkpoint": native,
        "checkpoint_root": out_root,
        "source_successor": source_successor,
        "source_ancestor_checkpoint": source_ancestor,
        "source_usage_parent": source_usage_parent,
        "source_accounting_parent": source_accounting_parent,
        "source_observation_parent": source_observation_parent,
        "partial_source_text": inline_partial_text is not None or (native or {}).get('schema_version') in {PARTIAL_SCHEMA, RETAINED_PARTIAL_SCHEMA},
        "validated_retained": validated_retained,
        "completed_retained_input": completed_retained_input,
    }


def _source_piece_container_clock_matches(
    path: Path, expected_ms: int, duration_ms: int, *,
    allow_audio_packet_rounding: bool = False,
    source_path: Path | None = None,
    source_interval_ms: tuple[int, int] | None = None,
    required_end_ms: int = 0,
) -> bool:
    """Allow only one measured video frame of mux rounding on bound media.

    Called after the ordinary source/provenance/hash validation. Source and
    context intervals still match exactly; no clock is rewritten. Every audio
    and video stream must fit that same frame bound, and the actual container
    probe must agree with the duration passed by the ordinary producer.
    Concatenated AAC may add one actual encoded packet at the mux tail. That
    bound is measured from a packet, never a percentage or rewritten clock.
    With a validated source window, compare stream ends with that source's
    available coverage and require every cue to fit both actual AV streams.
    """
    from fractions import Fraction
    import math
    import subprocess

    try:
        completed = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,start_time,duration,avg_frame_rate", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if completed.returncode != 0:
            return False
        probe = json.loads(completed.stdout)
        if int(float(probe["format"]["duration"]) * 1000) != duration_ms:
            return False
        streams = [row for row in probe["streams"] if row.get("codec_type") in {"video", "audio"}]
        video = [row for row in streams if row.get("codec_type") == "video"]
        if not video or not streams:
            return False
        rates = [Fraction(row["avg_frame_rate"]) for row in video]
        if any(rate <= 0 for rate in rates):
            return False
        tolerance_ms = max(math.ceil(1000 / rate) for rate in rates) + 1
        video_tolerance_ms = tolerance_ms
        if allow_audio_packet_rounding:
            for audio_index, stream in enumerate(row for row in streams if row.get("codec_type") == "audio"):
                if stream.get("codec_name") != "aac":
                    continue
                packet_probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", f"a:{audio_index}",
                     "-read_intervals", "%+#1", "-show_entries", "packet=duration_time",
                     "-of", "json", str(path)],
                    capture_output=True, text=True, timeout=60, check=False,
                )
                if packet_probe.returncode != 0:
                    return False
                packets = json.loads(packet_probe.stdout)["packets"]
                if len(packets) != 1:
                    return False
                quantum_ms = float(packets[0]["duration_time"]) * 1000
                if not math.isfinite(quantum_ms) or quantum_ms <= 0:
                    return False
                tolerance_ms = max(tolerance_ms, math.ceil(quantum_ms) + 1)
        if source_path is not None or source_interval_ms is not None:
            # The source window and mux duration are different clocks. At a
            # source EOF the audio can also finish before the last video frame.
            # Compare each stream with the coverage actually available in that
            # same hash-validated source; never extend or clip subtitle cues.
            if source_path is None or source_interval_ms is None:
                return False
            start_ms, end_ms = source_interval_ms
            if not 0 <= start_ms < end_ms or end_ms - start_ms != expected_ms:
                return False
            source_probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type,start_time,duration", "-of", "json", str(source_path)],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if source_probe.returncode != 0:
                return False
            source_streams = json.loads(source_probe.stdout)["streams"]
            if abs(duration_ms - expected_ms) > video_tolerance_ms:
                return False
            for kind in ("video", "audio"):
                outputs = [row for row in streams if row.get("codec_type") == kind]
                inputs = [row for row in source_streams if row.get("codec_type") == kind]
                if len(outputs) != 1 or not inputs:
                    return False
                output, original = outputs[0], inputs[0]
                source_start = float(original["start_time"]) * 1000
                source_end = source_start + float(original["duration"]) * 1000
                actual_start = float(output["start_time"]) * 1000
                actual_end = actual_start + float(output["duration"]) * 1000
                bound = video_tolerance_ms if kind == "video" else tolerance_ms
                available = min(end_ms, source_end) - start_ms
                if (not all(math.isfinite(v) for v in (source_start, source_end, actual_start, actual_end))
                        or source_start > start_ms or source_end < end_ms - bound
                        or abs(actual_start) > 1 or actual_end < required_end_ms
                        or abs(actual_end - available) > bound):
                    return False
            return True
        return (abs(duration_ms - expected_ms) <= tolerance_ms
                and all(abs(float(row["duration"]) * 1000 - expected_ms) <= tolerance_ms
                        for row in streams))
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return False


def _validate_current_source_context(
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    padded_dur: int,
    srt_text: str,
    clip_context: Mapping[str, object],
    piece_media_paths: Sequence[Path] | None = None,
    padded_media_path: Path | None = None,
) -> None:
    """Check that the retained text and context still describe this source grid.

    ``validate_clip_context`` binds the candidate/date/source hashes, but the
    ordinary producer also consumes the selection hook and piece clock.  Keep
    those checks here so a context from a neighbouring candidate cannot be
    accepted merely because it happens to name the same recording bytes.
    """

    if clip_context.get("selection_hook") != str(spec.get("selection_hook") or ""):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SELECTION_HOOK_DRIFT")
    raw_spec_pieces = spec.get("pieces")
    raw_context_pieces = clip_context.get("pieces")
    if not isinstance(raw_spec_pieces, list) or not isinstance(raw_context_pieces, list):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_CONTEXT_PIECES_INVALID")
    if len(raw_spec_pieces) != len(raw_context_pieces) or len(raw_spec_pieces) != len(durations):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_CONTEXT_PIECE_COUNT_DRIFT")

    expected_context_pieces: list[dict[str, object]] = []
    for index, (spec_piece, duration) in enumerate(zip(raw_spec_pieces, durations)):
        context_piece = raw_context_pieces[index]
        if not isinstance(spec_piece, Mapping) or not isinstance(context_piece, Mapping):
            raise CurrentTextContinuationError(
                f"CURRENT_AUTO_RESUME_SOURCE_CONTEXT_PIECE_INVALID:{index}"
            )
        start_ms = spec_piece.get("start_ms")
        end_ms = spec_piece.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
            or (end_ms - start_ms != duration and not (
                piece_media_paths is not None and len(piece_media_paths) == len(durations)
                and _source_piece_container_clock_matches(
                    piece_media_paths[index], end_ms - start_ms, duration,
                )
            ))
        ):
            raise CurrentTextContinuationError(
                f"CURRENT_AUTO_RESUME_SOURCE_CLOCK_INVALID:{index}"
            )
        expected_context_pieces.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "source_media_sha256": spec_piece.get("source_media_sha256"),
                "recording_basename": Path(str(spec_piece.get("remote_media") or "")).name,
            }
        )
        actual_context_piece = {
            "start_ms": context_piece.get("start_ms"),
            "end_ms": context_piece.get("end_ms"),
            "source_media_sha256": context_piece.get("source_media_sha256"),
            "recording_basename": context_piece.get("recording_basename"),
        }
        if (
            actual_context_piece["start_ms"] != expected_context_pieces[-1]["start_ms"]
            or actual_context_piece["end_ms"] != expected_context_pieces[-1]["end_ms"]
            or _continuation_normalize_sha(actual_context_piece["source_media_sha256"])
            != _continuation_normalize_sha(expected_context_pieces[-1]["source_media_sha256"])
            or actual_context_piece["recording_basename"]
            != expected_context_pieces[-1]["recording_basename"]
        ):
            raise CurrentTextContinuationError(
                f"CURRENT_AUTO_RESUME_SOURCE_CONTEXT_PIECE_DRIFT:{index}"
            )

    if sum(durations) != padded_dur and not (
        padded_media_path is not None and _source_piece_container_clock_matches(
            padded_media_path, sum(durations), padded_dur, allow_audio_packet_rounding=True,
        )
    ):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_CLOCK_DURATION_DRIFT")
    try:
        context_cues = [cue for cue in parse_srt_cues(
            str(clip_context.get("whole_clip_draft_srt") or "")
        ) if cue.text.strip()]
    except (TypeError, ValueError) as exc:
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_CONTEXT_SRT_INVALID") from exc
    if any(
        cue.start_ms < 0 or cue.end_ms <= cue.start_ms or cue.end_ms > padded_dur
        for cue in context_cues
    ):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_CONTEXT_CLOCK_DRIFT")


def _source_context_infra_scope(
    final_review: Mapping[str, object], srt_text: str,
    boundary_search_scope: Mapping[str, object],
) -> dict[str, object]:
    """Retain unresolved witness padding without treating it as reviewed text.

    Only ordinary post-ceiling context qualifies. The ceiling is the normal
    frozen search scope plus the resolver's maximum tail air, not the selector
    endpoint. Missing/drifted cue bindings and every possibly delivered cue
    remain blocking. Final boundary and exact-delivery review still run.
    """
    from src.autoslice.producer_boundary import TAIL_PAD_MS

    rows = final_review.get("infra_unresolved") or []
    if not isinstance(rows, list) or not boundary_search_scope_is_valid(boundary_search_scope):
        raise CurrentTextContinuationError("SOURCE_CONTEXT_INFRA_SCOPE_INVALID")
    cues = parse_srt_cues(srt_text)
    by_index = {int(cue.index): cue for cue in cues}
    if len(by_index) != len(cues):
        raise CurrentTextContinuationError("SOURCE_CONTEXT_INFRA_CUE_BINDING_INVALID")
    ceiling = int(boundary_search_scope["max_recommended_end_ms"]) + TAIL_PAD_MS
    deferred, blocking = [], []
    for row in rows:
        if not isinstance(row, Mapping) or type(row.get("cue_index")) is not int:
            raise CurrentTextContinuationError("SOURCE_CONTEXT_INFRA_CUE_BINDING_INVALID")
        cue = by_index.get(row["cue_index"])
        suspect = row.get("suspect")
        if cue is None or not isinstance(suspect, str) or not suspect or suspect not in cue.text:
            raise CurrentTextContinuationError("SOURCE_CONTEXT_INFRA_CUE_BINDING_INVALID")
        if (boundary_search_scope.get("boundary_end_mode") == "semantic_lower_bound"
                and cue.start_ms >= ceiling):
            deferred.append({**dict(row), "start_ms": cue.start_ms, "end_ms": cue.end_ms,
                             "disposition": "UNRESOLVED_SOURCE_CONTEXT_ONLY"})
        else:
            blocking.append(dict(row))
    return {
        "source_srt_sha256": "sha256:" + hashlib.sha256(srt_text.encode()).hexdigest(),
        "scope_sha256": boundary_search_scope["scope_sha256"],
        "maximum_possible_delivery_end_ms": ceiling,
        "deferred": deferred, "blocking": blocking,
        "final_boundary_and_exact_review_required": True,
    }


def _validate_source_stage_chat_reads(
    rows: list, *, srt_text: str, clip_context: Mapping[str, object],
    authoritative_chat: Sequence[ChatEvidence],
    independent_support_srts: Sequence[str] = (),
) -> None:
    """Replay the ordinary exact-read lane against actual immutable chat.

    This is source-checkpoint ingress only; it grants no new spelling or
    release verdict. Sender/gift/coreference and reconciled rows remain on
    their existing unsupported lanes.
    """
    from src.autoslice.chat_read_aloud_candidates import (
        find_best_read_aloud_candidate, read_aloud_support_receipts,
    )
    from src.autoslice.read_aloud_arbitration import whole_line_exact_copy_gate

    original = str(clip_context.get("whole_clip_draft_srt") or "")
    original_cues = {int(cue.index): cue for cue in parse_srt_cues(original)}
    current_by_geometry: dict[tuple[int, int], list] = {}
    for cue in parse_srt_cues(srt_text):
        current_by_geometry.setdefault((cue.start_ms, cue.end_ms), []).append(cue)
    for row in rows:
        if not isinstance(row, Mapping):
            raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_APPLIED_UNSUPPORTED")
        matches = [item for item in authoritative_chat if item.evidence_id == row.get("evidence_id")]
        indexes = row.get("cue_indexes")
        if (len(matches) != 1 or not isinstance(indexes, list) or not indexes
                or any(type(i) is not int or i not in original_cues for i in indexes)
                or row.get("mode") not in {"exact_span", "question_particle_patch"} or row.get("reconciliation")
                or row.get("entity_verdict") or row.get("thread_anchored")):
            raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_APPLIED_UNSUPPORTED")
        # Historical cue numbers belong to the original draft. Deterministic
        # source hygiene can merge earlier cues and renumber every later one;
        # reuse only a unique exact physical span, never a nearby cue or a
        # text-only search. Keep the original row and its source binding intact.
        current_cues = {}
        for index in indexes:
            original_cue = original_cues[index]
            candidates = current_by_geometry.get((original_cue.start_ms, original_cue.end_ms), [])
            if len(candidates) != 1:
                raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_APPLIED_CHAT_OR_TEXT_DRIFT")
            current_cues[index] = candidates[0]
        item = matches[0]
        expected = {"kind": item.kind, "sender": item.sender, "source": item.source,
                    "source_sha256": item.source_sha256, "source_event_id": item.source_event_id,
                    "source_offset_ms": item.offset_ms, "exact_text": item.text}
        if (item.kind not in {"danmaku", "superchat"}
                or any(row.get(k) != v for k, v in expected.items())
                or row.get("before") != [original_cues[i].text for i in indexes]
                or row.get("after") != [current_cues[i].text for i in indexes]
                or row.get("matched_start_ms") != current_cues[indexes[0]].start_ms
                or row.get("matched_end_ms") != current_cues[indexes[-1]].end_ms):
            raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_APPLIED_CHAT_OR_TEXT_DRIFT")
        # The whole-clip draft is the primary transcript, not an independent
        # witness. Replay only the same raw-audio sidecars used by the normal
        # producer; a text-first CPA decision can legitimately have none.
        supports = read_aloud_support_receipts(
            item, support_srt_texts=independent_support_srts, max_cues=4,
        )
        gate = row.get("whole_line_exact_copy_gate")
        if (supports != row.get("audio_transcript_supports") or not isinstance(gate, Mapping)
                or not isinstance(gate.get("primary_transcript"), Mapping)):
            raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_APPLIED_SUPPORT_DRIFT")
        proposal = {**gate["primary_transcript"], "support_receipts": supports, "thread_anchored": False}
        if row["mode"] == "question_particle_patch":
            # This ordinary lane restores only a missing question particle
            # and retains the same-cue reply. It does not need, or grant,
            # whole-line copy ownership. Replay its existing discovery and
            # actual independent-transcript support instead of promoting the
            # historical BLOCKED_PARTIAL_EVIDENCE gate to PASS.
            best = None
            if len(indexes) == 1 and item.kind == "danmaku":
                best, _ = find_best_read_aloud_candidate(
                    item, evidence=authoritative_chat,
                    cues=[original_cues[indexes[0]]], texts=row["before"], max_cues=4,
                )
            if (best is None or best["mode"] != "question_particle_patch"
                    or row["after"] != [best["replacement"]]
                    or row.get("read_aloud_verdict") is not None
                    or not any(receipt.get("owner_eligible") is True
                               or receipt.get("question_particle_patch_supported") is True
                               for receipt in supports)):
                raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_APPLIED_QUESTION_PATCH_DRIFT")
            proposal = {**best, "support_receipts": supports}
        replayed = whole_line_exact_copy_gate(
            item, proposal, "".join(row["before"]), verdict=row.get("read_aloud_verdict"),
        )
        if (replayed != gate or row.get("owner_eligible") is not replayed.get("owner_eligible")
                or (row["mode"] == "exact_span" and replayed.get("status") != "PASS")):
            raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_APPLIED_SUPPORT_DRIFT")


_SOURCE_STAGE_PENDING_INFRA_REASONS = frozenset(
    {"ENTITY_AUDIO_PROVIDER_FAILED", "CONTEXT_VERIFIER_ERROR"}
)


def _pending_source_stage_review_scope(
    final_review: Mapping[str, object],
    *,
    srt_text: str,
    srt_sha256: str,
    current_authority_srt_sha256: object,
) -> dict[str, object] | None:
    """Turn only a bound infrastructure miss into an ordinary review input.

    The old acoustic verdict is deliberately not copied.  The row returned to
    ``_run_final_review`` is a discovery candidate with the current cue/base
    binding and its textual context only; the next reviewer must obtain a new
    witness and CPA decision through the normal route.
    """

    unresolved = final_review.get("infra_unresolved")
    if unresolved in (None, []):
        declared_count = final_review.get("infra_unresolved_count")
        if declared_count not in (None, 0):
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_ROWS_INVALID"
            )
        return None
    if final_review.get("status") not in {"APPLIED", "PARTIAL"}:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_PENDING_REVIEW_STATUS_INVALID"
        )
    if not isinstance(unresolved, list):
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_PENDING_REVIEW_ROWS_INVALID"
        )
    declared_count = final_review.get("infra_unresolved_count")
    if declared_count is not None and declared_count != len(unresolved):
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_PENDING_REVIEW_COUNT_INVALID"
        )
    findings = final_review.get("findings")
    if not isinstance(findings, list):
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_PENDING_REVIEW_FINDINGS_MISSING"
        )
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not cues:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_PENDING_REVIEW_SRT_EMPTY"
        )
    current_srt_sha256 = _continuation_normalize_sha(srt_sha256)
    try:
        _continuation_require_sha(
            current_srt_sha256,
            current_authority_srt_sha256,
            label="CURRENT_SOURCE_STAGE_FINAL_OUTPUT_SRT",
        )
    except CurrentTextContinuationError as exc:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_FINAL_OUTPUT_SRT_BINDING_INVALID"
        ) from exc
    pending_rows: list[dict[str, object]] = []
    seen_indexes: set[int] = set()
    if len(current_srt_sha256) != 64:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_PENDING_REVIEW_SRT_BINDING_INVALID"
        )
    for unresolved_row in unresolved:
        if not isinstance(unresolved_row, Mapping):
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_ROW_INVALID"
            )
        cue_index = unresolved_row.get("cue_index")
        if type(cue_index) is not int or not 1 <= cue_index <= len(cues):
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_CUE_BINDING_INVALID"
            )
        if cue_index in seen_indexes:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_CUE_DUPLICATE"
            )
        seen_indexes.add(cue_index)
        reason_code = str(unresolved_row.get("reason_code") or "")
        if reason_code not in _SOURCE_STAGE_PENDING_INFRA_REASONS:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_REASON_UNSUPPORTED"
            )
        suspect = unresolved_row.get("suspect")
        if not isinstance(suspect, str) or not suspect or suspect not in cues[cue_index - 1].text:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_CUE_BINDING_INVALID"
            )
        matching = [
            finding
            for finding in findings
            if isinstance(finding, Mapping)
            and finding.get("cue_index") == cue_index
            and finding.get("suspect") == suspect
        ]
        if len(matching) != 1:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_FINDING_BINDING_INVALID"
            )
        finding = matching[0]
        cue = cues[cue_index - 1]
        base_sha256 = _continuation_normalize_sha(finding.get("base_text_sha256"))
        expected_base_sha256 = hashlib.sha256(cue.text.encode("utf-8")).hexdigest()
        if base_sha256 != expected_base_sha256:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_BASE_TEXT_DRIFT"
            )
        if finding.get("proposed_full_cue") is not None or str(
            finding.get("suggestion") or ""
        ).strip():
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_PROPOSAL_UNSAFE"
            )
        repair_class = str(finding.get("repair_class") or "")
        if repair_class not in {"phonetic", "segmentation", "spoken_unit"}:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_CLASS_INVALID"
            )
        start = finding.get("span_start_codepoint")
        end = finding.get("span_end_codepoint")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start <= end <= len(cue.text)
            or cue.text[start:end] != suspect
        ):
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_SPAN_INVALID"
            )
        # A previous request is useful only as a binding check.  Its old
        # verdict/witness never crosses into the new raw row.
        old_adjudication = finding.get("context_audio_adjudication")
        old_request = (
            old_adjudication.get("request")
            if isinstance(old_adjudication, Mapping)
            else None
        )
        if isinstance(old_request, Mapping):
            if (
                old_request.get("current_cue") != cue.text
                or _continuation_normalize_sha(old_request.get("base_text_sha256"))
                != expected_base_sha256
                or old_request.get("cue_indexes") != [cue_index]
                or old_request.get("matched_start_ms") != cue.start_ms
                or old_request.get("matched_end_ms") != cue.end_ms
            ):
                raise CurrentTextContinuationError(
                    "CURRENT_SOURCE_STAGE_PENDING_REVIEW_REQUEST_DRIFT"
                )
            # The old request is retained as historical evidence, but its
            # whole-clip text can predate unrelated typed repairs on the
            # current surface.  Its target cue/base/window are the only
            # bindings reused here; the old request is never new evidence.

        # Only discovery/identity fields enter the new ordinary reviewer.  In
        # particular, context_audio_adjudication and routed/mutation fields
        # are intentionally omitted so an old FAILED verdict cannot become
        # new evidence or authority.
        raw: dict[str, object] = {
            "cue": cue_index,
            "cue_index": cue_index,
            "kind": finding.get("kind") or "context",
            "suspect": suspect,
            "proposed_full_cue": None,
            "suggestion": None,
            "repair_class": repair_class,
            "why": finding.get("why") or "Prior acoustic provider failure requires ordinary re-review.",
            "base_text_sha256": expected_base_sha256,
            "span_start_codepoint": start,
            "span_end_codepoint": end,
            "current_srt_sha256": "sha256:" + current_srt_sha256,
        }
        for key in (
            "evidence_cue_ids",
            "candidate_provenance",
            "candidate_memory_id",
            "force_acoustic",
            "correlated_text_witness",
            "provenance",
        ):
            if key in finding:
                raw[key] = copy.deepcopy(finding[key])
        pending_rows.append(raw)
    return {
        "status": "PENDING_REVIEW",
        "source_srt_sha256": "sha256:" + current_srt_sha256,
        "cue_indexes": [row["cue_index"] for row in pending_rows],
        "rows": pending_rows,
        "historical_context_adjudication_count": final_review.get(
            "context_adjudication_count", 0
        ),
        "historical_applied_count": final_review.get("applied_count", 0),
    }


def _validate_source_stage_transcript_entity_repair(
    row: Mapping[str, object],
    *,
    authority: Mapping[str, object],
    srt_text: str,
    allow_pending_audio_input: bool = False,
) -> None:
    """Replay the normal transcript entity consumer's nested CPA verdict.

    This source-stage row predates the final span judge contract. Preserve its
    actual owner and require the same audit row, cue geometry and minimal
    replacement; it grants no boundary or final release authority. Only the
    retained-parent input consumer may admit a true needs_audio flag, and it
    must turn that flag into a mandatory current review before proceeding.
    """
    audit = authority.get("transcript_entity_audit")
    if not isinstance(audit, Mapping) or row not in (audit.get("repairs") or []):
        raise CurrentTextContinuationError("CURRENT_TRANSCRIPT_ENTITY_AUDIT_ROW_DRIFT")
    from src.autoslice.transcript_entity_contract import read_receipt
    try:
        row = read_receipt(row)
    except ValueError as exc:
        raise CurrentTextContinuationError(str(exc)) from exc
    expected = row.get("expected_entity")
    verdict = row.get("verdict")
    if (
        not isinstance(expected, str)
        or not valid_cpa_entity_repair(row, expected_entity=expected)
        or not isinstance(verdict, Mapping)
        or (verdict.get("needs_audio") is not False
            and not (allow_pending_audio_input and verdict.get("needs_audio") is True))
        or row.get("resolved_canonical") != expected
    ):
        raise CurrentTextContinuationError("CURRENT_TRANSCRIPT_ENTITY_VERDICT_INVALID")
    indexes, before, after = row.get("cue_indexes"), row.get("before"), row.get("after")
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if (
        not isinstance(indexes, list) or len(indexes) != 1
        or isinstance(indexes[0], bool) or not isinstance(indexes[0], int)
        or not 1 <= indexes[0] <= len(cues)
        or not isinstance(before, list) or len(before) != 1
        or not isinstance(after, list) or len(after) != 1
        or not isinstance(before[0], str) or not isinstance(after[0], str)
    ):
        raise CurrentTextContinuationError("CURRENT_TRANSCRIPT_ENTITY_CUE_BINDING_INVALID")
    cue = cues[indexes[0] - 1]
    if (
        row.get("matched_start_ms") != cue.start_ms
        or row.get("matched_end_ms") != cue.end_ms
        or after[0] != cue.text
    ):
        raise CurrentTextContinuationError("CURRENT_TRANSCRIPT_ENTITY_CURRENT_CUE_DRIFT")
    surface = row.get("transcript_surface")
    if (
        not isinstance(surface, str) or not surface
        or not re.search(re.escape(surface), before[0], flags=re.IGNORECASE)
        or re.sub(re.escape(surface), expected, before[0], count=1,
                  flags=re.IGNORECASE) != after[0]
        or before[0] == after[0]
        or row.get("survived") is not True
    ):
        raise CurrentTextContinuationError("CURRENT_TRANSCRIPT_ENTITY_MINIMAL_REPAIR_DRIFT")


_FOREIGN_FINDING_KEYS = ("mixed_cjk_latin_cues", "latin_heavy_cues")


def _foreign_observation_view(value):
    if isinstance(value, Mapping):
        return {key: _foreign_observation_view(item) for key, item in value.items()
                if key not in {"cue_index", "srt_sha256", "audit_sha256_before_projection"}}
    if isinstance(value, list):
        return [_foreign_observation_view(item) for item in value]
    return value


def _foreign_surface_bindings(text):
    return [(c.start_ms,c.end_ms,c.text) for c in parse_srt_cues(text)
            if re.search(r'[A-Za-z\u3040-\u30ff]',c.text)]


def _bind_foreign_raw_guard(audit, raw_guard, srt_text):
    """Attach the recomputable raw guard without touching the adjudication."""
    audit["raw_guard"] = raw_guard
    audit["raw_guard_srt_sha256"] = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()


def _foreign_finding_signatures(audit):
    """Index-free (kind, cue text, source start, source end) per finding row.

    Times come from the row itself, else from the story-scope projection that
    located it; a row without usable text/time gets ``None`` fields, so it can
    never cover (or be covered by) anything.
    """
    projection = audit.get("story_scope_projection")
    located = {}
    if isinstance(projection, Mapping):
        for item in projection.get("findings") or []:
            if isinstance(item, Mapping):
                located[(item.get("source_key"), item.get("source_position"))] = (
                    item.get("start_ms"), item.get("end_ms"))
    signatures = []
    for key in _FOREIGN_FINDING_KEYS:
        rows = audit.get(key) or []
        for position, row in enumerate(rows if isinstance(rows, list) else [None]):
            text = row.get("text") if isinstance(row, Mapping) else None
            start, end = (row.get("start_ms"), row.get("end_ms")) if isinstance(row, Mapping) else (None, None)
            if start is None and end is None:
                start, end = located.get((key, position), (None, None))
            usable = isinstance(text, str) and type(start) is int and type(end) is int
            signatures.append((key, text, start, end) if usable else (key, None, None, None))
    return signatures


def _foreign_guard_blocks(audit):
    """Effective foreign-guard block decision for one stored audit.

    Without a ``raw_guard`` (records written before the separation) the audit
    status alone decides. With one: a clean raw guard never blocks; otherwise
    the adjudicated status must be unblocked and every raw finding must already
    be an adjudicated/deferred finding with identical cue text and source time
    (cue index ignored). Anything new, changed or unlocatable blocks.
    """
    status_blocked = str(audit.get("status") or "").startswith("BLOCKED_")
    raw = audit.get("raw_guard")
    if not isinstance(raw, Mapping):
        return status_blocked
    if not str(raw.get("status") or "").startswith("BLOCKED_"):
        return False
    if status_blocked:
        return True
    raw_findings = _foreign_finding_signatures(raw)
    covered = set(_foreign_finding_signatures(audit))
    return not raw_findings or any(
        signature[1] is None or signature not in covered for signature in raw_findings
    )


def _replay_source_stage_foreign_guard(authority, *, srt_text, spec, durations):
    stored = dict(authority["foreign_script_consistency_audit"])
    current = _foreign_scope.audit_foreign(srt_text, spec, durations)
    stored_raw = stored.get("raw_guard")
    def same_observation(expected):
        current_view, expected_view = _foreign_observation_view(current), _foreign_observation_view(expected)
        if current_view == expected_view:
            return True
        # Kana-only audits retain cue numbers rather than finding text/time.
        # Reindexing is safe only when the original hash-bound audit replays
        # and every actual foreign surface has identical words and time.
        previous = authority.get('post_transcript_entity_output_srt')
        return (isinstance(previous,str)
            and hashlib.sha256(previous.encode()).hexdigest() ==
                _continuation_normalize_sha(authority.get('post_transcript_entity_output_srt_sha256'))
            and _foreign_scope.audit_foreign(previous,spec,durations) == expected
            and _foreign_surface_bindings(previous) == _foreign_surface_bindings(srt_text)
            and {k:v for k,v in current_view.items() if k != 'kana_cue_indexes'} ==
                {k:v for k,v in expected_view.items() if k != 'kana_cue_indexes'})
    if isinstance(stored_raw, Mapping):
        # The adjudication is kept as persisted; only the recomputable raw
        # guard is replayed. Reindexing cannot change an observation view.
        if current != stored_raw and not same_observation(stored_raw):
            raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FOREIGN_GUARD_DRIFT")
        replayed = dict(stored)
        _bind_foreign_raw_guard(replayed, current, srt_text)
        return replayed
    if current == stored:
        return current
    previous = authority.get("post_transcript_entity_output_srt")
    if not isinstance(previous, str):
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FOREIGN_GUARD_DRIFT")
    _continuation_require_sha(hashlib.sha256(previous.encode()).hexdigest(),
                              authority.get("post_transcript_entity_output_srt_sha256"),
                              label="CURRENT_SOURCE_STAGE_FOREIGN_PREIMAGE")
    if _foreign_scope.audit_foreign(previous, spec, durations) != stored:
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FOREIGN_GUARD_DRIFT")
    # Reindexing a preceding cue does not change these actual foreign words,
    # their source clock or their frozen story scope. Any such change still
    # rejects the old stage; the current guard is always recomputed.
    if not same_observation(stored):
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FOREIGN_GUARD_DRIFT")
    return current


def _validate_current_source_stage_authority(
    authority: Mapping[str, object],
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    srt_text: str,
    srt_sha256: str,
    clip_context: Mapping[str, object],
    source_truth_ledger_path: Path | None,
    authoritative_chat: Sequence[ChatEvidence] = (),
    independent_support_srts: Sequence[str] = (),
    allow_pending_review: bool = False,
    verified_language_reference: str | None = None,
    pending_finalization: bool = False,
    pending_review: bool = False,
    completed_retained_input: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    """Validate a normal producer's v1 text-stage checkpoint.

    This deliberately stops at the source-text stage.  It checks the guards
    that the normal producer had to pass before writing ``padded.fresh.srt``
    and replays ordinary typed rows, but it never interprets the v1 audit as a
    final release receipt or requires the later v2 boundary witness.
    """

    if authority.get("schema_version") != "chat-authority-audit.v2":
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_CHAT_AUTHORITY_SCHEMA_INVALID")
    if authority.get("mutation_authorized") is True:
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_MUTATION_AUTHORITY_INVALID")

    for field in (("post_transcript_entity_output_srt_sha256",) if pending_finalization else ("final_output_srt_sha256",)):
        try:
            _continuation_require_sha(
                srt_sha256, authority.get(field), label=f"CURRENT_SOURCE_STAGE_{field.upper()}"
            )
        except CurrentTextContinuationError as exc:
            raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FINAL_SRT_DRIFT") from exc
    post_transcript_text = authority.get("post_transcript_entity_output_srt")
    if not isinstance(post_transcript_text, str) or not post_transcript_text:
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_POST_TRANSCRIPT_TEXT_MISSING")
    post_transcript_digest = hashlib.sha256(post_transcript_text.encode("utf-8")).hexdigest()
    _continuation_require_sha(
        post_transcript_digest,
        authority.get("post_transcript_entity_output_srt_sha256"),
        label="CURRENT_SOURCE_STAGE_POST_TRANSCRIPT_TEXT",
    )

    chat_status = authority.get("status")
    validated_replayed_transcript = None
    from src.autoslice.retained_preparation_checkpoint import replayed_input_present
    if replayed_input_present(authority):
        from src.autoslice.retained_preparation_checkpoint import validate_completed_retained_input_authority
        validated_replayed_transcript = validate_completed_retained_input_authority(
            completed_retained_input, authority=authority, clip_context=clip_context)
    elif chat_status not in {"NO_MATCH", "APPLIED_AND_VERIFIED"}:
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_CHAT_AUTHORITY_STATUS_INVALID")
    for field in ("entity_verdict_required", "sender_verdict_required", "pending_text_overrides"):
        value = authority.get(field)
        if value not in (None, []):
            raise CurrentTextContinuationError(
                f"CURRENT_SOURCE_STAGE_{field.upper()}_PENDING"
            )

    # These lanes have their own normal producer consumers.  Until their
    # individual replay contracts are available, a non-empty row must stop the
    # source-stage ingress instead of being silently treated as an entity row.
    for field in ("applied", "sender_repairs", "gift_repairs", "coreference_repairs"):
        rows = authority.get(field)
        if not isinstance(rows, list):
            raise CurrentTextContinuationError(
                f"CURRENT_SOURCE_STAGE_{field.upper()}_INVALID"
            )
        if rows:
            if field == "applied":
                _validate_source_stage_chat_reads(
                    rows, srt_text=srt_text, clip_context=clip_context,
                    authoritative_chat=authoritative_chat,
                    independent_support_srts=independent_support_srts,
                )
                continue
            raise CurrentTextContinuationError(
                f"CURRENT_SOURCE_STAGE_{field.upper()}_UNSUPPORTED"
            )

    final_review = authority.get("final_review_audit")
    if not isinstance(final_review, Mapping):
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FINAL_REVIEW_MISSING")
    if final_review.get("schema_version") != "final-review-audit.v1":
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FINAL_REVIEW_SCHEMA_INVALID")
    allowed_review_statuses = ({"APPLIED", "PARTIAL", "SKIPPED", "FLAGGED"}
        if allow_pending_review else {"APPLIED", "PARTIAL", "SKIPPED"})
    if pending_review:
        allowed_review_statuses.add('AUDITOR_UNAVAILABLE')
    if final_review.get("status") not in allowed_review_statuses:
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FINAL_REVIEW_STATUS_INVALID")
    findings = final_review.get("findings")
    if findings is not None and not isinstance(findings, list):
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FINAL_REVIEW_FINDINGS_INVALID")
    validated_review = copy.deepcopy(dict(final_review))
    infra_unresolved = final_review.get("infra_unresolved")
    if infra_unresolved not in (None, []):
        scoped_spec, scoped_authority = copy.deepcopy(dict(spec)), copy.deepcopy(dict(authority))
        owners = ledger_required_owner_contracts(
            spec=scoped_spec, durations=durations, ledger_path=source_truth_ledger_path,
        )
        owners.extend(_redelivery_baseline_boundary_owner(scoped_spec, durations))
        _, search_scope = freeze_required_boundary_owner_contract(
            spec=scoped_spec, durations=durations, chat_authority_audit=scoped_authority,
            required_boundary_owners=owners,
        )
        scope = _source_context_infra_scope(final_review, srt_text, search_scope)
        pending_scope = _pending_source_stage_review_scope(
            final_review,
            srt_text=srt_text,
            srt_sha256=srt_sha256,
            current_authority_srt_sha256=authority.get("final_output_srt_sha256"),
        ) if allow_pending_review else None
        if scope["blocking"] and pending_scope is None:
            raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FINAL_REVIEW_INFRA_UNRESOLVED")
        validated_review["source_context_infra_scope"] = scope
        if pending_scope is not None:
            pending_scope["source_context_infra_scope"] = scope
            validated_review["pending_review_scope"] = pending_scope
    applied_count = final_review.get("applied_count")
    if applied_count is not None and (
        isinstance(applied_count, bool) or not isinstance(applied_count, int) or applied_count < 0
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_FINAL_REVIEW_APPLIED_COUNT_INVALID")

    entity_rows = authority.get("entity_repairs") or []
    if not isinstance(entity_rows, list):
        raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_ENTITY_REPAIRS_INVALID")
    for row_index, row in enumerate(entity_rows):
        if not isinstance(row, Mapping):
            raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_ENTITY_REPAIR_INVALID")
        judge = row.get("judge")
        if (
            row.get("mode") == "exact_final_cpa_self_heal"
            and isinstance(judge, Mapping)
            and judge.get("schema_version") in {
                "exact-final-cpa-cycle-judge.v1", "exact-final-cpa-convergence-judge.v1",
                "editorial-subtitle-judge.v1",
            }
        ):
            # Exact-final owners are a later release lane.  They cannot be
            # relabeled as ordinary source-stage repairs merely to resume.
            raise CurrentTextContinuationError(
                f"CURRENT_SOURCE_STAGE_EXACT_OWNER_UNSUPPORTED:{row_index}"
            )
        try:
            if row.get("mode") == "transcript_entity_only":
                _validate_source_stage_transcript_entity_repair(
                    row, authority=authority, srt_text=srt_text,
                )
            elif row.get("mode") in {
                "final_foreign_script_cpa_adjudication", "final_source_language_cpa_adjudication",
            }:
                from src.autoslice.final_source_language_owner import validate_final_cpa_owner
                try:
                    validate_final_cpa_owner(
                        row, row_index=row_index, authority=authority, srt_text=srt_text,
                    )
                except ValueError as exc:
                    raise CurrentTextContinuationError("CURRENT_FINAL_LANGUAGE_OWNER_INVALID") from exc
            else:
                _continuation_validate_existing_repair(
                    row, srt_text=srt_text, clip_context=clip_context,
                )
        except CurrentTextContinuationError as exc:
            raise CurrentTextContinuationError(
                f"CURRENT_SOURCE_STAGE_ENTITY_REPAIR_INVALID:{row_index}:{exc}"
            ) from exc
    if not pending_finalization:
        # Language reference effects are usable only after the existing chat
        # and entity consumers above have revalidated their original owners.
        _validate_current_source_stage_guards(
            authority, spec=spec, durations=durations, srt_text=srt_text, srt_sha256=srt_sha256,
            source_truth_ledger_path=source_truth_ledger_path,
            verified_language_reference=verified_language_reference if allow_pending_review else None,
            validated_language_effects=_source_language_effect_rows(authority),
            retained_transcript_input=validated_replayed_transcript,
        )
    return validated_review


def _source_language_effect_rows(authority: Mapping[str, object]) -> tuple:
    return tuple((kind, row) for kind in ("applied", "entity_repairs")
                 for row in authority.get(kind) or [] if isinstance(row, Mapping)
                 and not row.get("reconciliation"))


def _needs_typed_language_reference(authority: Mapping[str, object]) -> bool:
    audit = authority.get('final_source_language_preservation_audit') or {}
    return (audit.get('typed_language_reference') is not None
            or audit.get('status') == 'SKIPPED_CUE_COUNT_MISMATCH'
            and type(audit.get('draft_cue_count')) is int and audit['draft_cue_count'] > 0)


def _typed_source_language_reference(
    original_reference: str, srt_text: str,
    validated_effects: Sequence[tuple[str, Mapping[str, object]]],
) -> tuple[str, dict] | None:
    """Derive only effects already accepted by the ordinary typed readers."""
    original = parse_srt_cues(original_reference)
    current = parse_srt_cues(srt_text)
    texts = [cue.text for cue in original]
    effects = []
    seen = set()
    for kind, row in validated_effects:
        before, after = row.get("before"), row.get("after")
        if (not isinstance(before, list) or not isinstance(after, list)
                or len(before) != 1 or len(after) != 1
                or not isinstance(before[0], str) or not isinstance(after[0], str)
                or before == after or not re.search(r"[A-Za-z\u3040-\u30ff]", before[0] + after[0])):
            continue
        interval = (row.get("matched_start_ms"), row.get("matched_end_ms"))
        if (any(type(value) is not int for value in interval) or interval in seen):
            raise CurrentTextContinuationError("CURRENT_SOURCE_LANGUAGE_TYPED_REFERENCE_AMBIGUOUS")
        source_indexes = [i for i, cue in enumerate(original)
                          if (cue.start_ms, cue.end_ms) == interval]
        targets = [cue for cue in current if (cue.start_ms, cue.end_ms) == interval]
        if (len(source_indexes) != 1 or len(targets) != 1
                or targets[0].text != after[0] or texts[source_indexes[0]] != before[0]):
            raise CurrentTextContinuationError("CURRENT_SOURCE_LANGUAGE_TYPED_REFERENCE_DRIFT")
        seen.add(interval)
        texts[source_indexes[0]] = after[0]
        effects.append({"ledger": kind, "effect_sha256": "sha256:" + hashlib.sha256(
            json.dumps(dict(row), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode()).hexdigest()})
    if not effects:
        return None
    from src.autoslice.source_subtitle_truth import _render
    reference = _render(original, texts)
    binding = {"schema_version": "typed-source-language-reference.v1",
               "original_reference_sha256": hashlib.sha256(original_reference.encode()).hexdigest(),
               "derived_reference_sha256": hashlib.sha256(reference.encode()).hexdigest(),
               "effects": effects}
    return reference, binding


def _replay_typed_source_language_guard(
    authority: Mapping[str, object], *, original_reference: str | None,
    srt_text: str, spec: Mapping[str, object], durations: Sequence[int],
    validated_effects: Sequence[tuple[str, Mapping[str, object]]] | None,
) -> dict[str, Any] | None:
    """Recheck a count-changing successor after its typed effects are validated.

    This is called only after the ordinary chat/entity readers. It derives an
    in-memory reference on the original physical grid; it never changes the
    independent audio reference or promotes an old skipped audit.
    """
    stored = authority.get("final_source_language_preservation_audit")
    if not isinstance(stored, Mapping) or original_reference is None or validated_effects is None:
        return None
    previous = stored.get("typed_language_reference")
    if stored.get("status") != "SKIPPED_CUE_COUNT_MISMATCH" and previous is None:
        return None
    if (type(stored.get("draft_cue_count")) is not int or stored["draft_cue_count"] <= 0
            or stored.get("reverted") != [] or stored.get("reverted_count") != 0
            or stored.get("unproven_foreign_introductions") != []):
        return None
    derived = _typed_source_language_reference(original_reference, srt_text, validated_effects)
    if derived is None:
        return None
    reference, binding = derived
    output, audit = _foreign_scope.guard_source_language(reference, srt_text, spec, durations)
    if (output != srt_text or audit.get("status") != "CLEAN"
            or audit.get("cue_binding") != "UNCHANGED_FOREIGN_CUE_INTERVALS"
            or any(stored.get(key) != audit.get(key) for key in ("draft_cue_count", "final_cue_count"))):
        return None
    if previous is not None and previous != binding:
        raise CurrentTextContinuationError("CURRENT_SOURCE_LANGUAGE_TYPED_REFERENCE_BINDING_DRIFT")
    audit["typed_language_reference"] = binding
    return audit


def _source_truth_replay_matches(original: Mapping[str, object], replay: Mapping[str, object]) -> bool:
    """Compare actual source effects, keeping the original receipt unchanged.

    A freshly recomputed empty scope has no applicable ledger dependency.
    Nonempty scopes still require the exact ledger digest and effect rows.
    """
    if dict(original) == dict(replay):
        return True
    digest = _continuation_normalize_sha(original.get("ledger_sha256"))
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return False
    old, new = dict(original), dict(replay)
    old.pop("ledger_path", None)
    new.pop("ledger_path", None)
    empty_scope = all(
        audit.get("schema_version") == "source-subtitle-truth-audit.v1"
        and audit.get("status") == "NO_RELEVANT_INTERVAL"
        and all(audit.get(key) == [] for key in ("applied", "satisfied", "failures"))
        for audit in (original, replay)
    )
    replay_digest = _continuation_normalize_sha(replay.get("ledger_sha256"))
    if empty_scope and len(replay_digest) == 64 and all(c in "0123456789abcdef" for c in replay_digest):
        old.pop("ledger_sha256", None)
        new.pop("ledger_sha256", None)
    return old == new


def _validate_current_source_stage_guards(
    authority: Mapping[str, object],
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    srt_text: str,
    srt_sha256: str,
    source_truth_ledger_path: Path | None,
    verified_language_reference: str | None = None,
    validated_language_effects: Sequence[tuple[str, Mapping[str, object]]] | None = None,
    retained_transcript_input: Mapping[str, object] | None = None,
) -> None:
    """Replay the persisted normal text-stage guards without promoting them.

    The ordinary producer writes the v1 audit only after these gates.  A
    resumed source-stage input therefore needs the same typed evidence and the
    deterministic guards must still produce the same bytes on the current SRT.
    Source-language preservation is special: the current SRT is never used as
    its own witness.  A saved, typed source-language audit is accepted as the
    normal producer's witness fact; a future producer may add an explicitly
    bound witness without changing this lane.
    """

    def require_audit(
        key: str,
        schema_version: str,
    ) -> Mapping[str, object]:
        value = authority.get(key)
        if not isinstance(value, Mapping):
            raise CurrentTextContinuationError(
                f"CURRENT_SOURCE_STAGE_{key.upper()}_MISSING"
            )
        if value.get("schema_version") != schema_version:
            raise CurrentTextContinuationError(
                f"CURRENT_SOURCE_STAGE_{key.upper()}_SCHEMA_INVALID"
            )
        return value

    source_language = require_audit(
        "final_source_language_preservation_audit",
        "source-language-preservation-audit.v1",
    )
    source_language_status = str(source_language.get("status") or "")
    retained_language_input = False
    typed_language_replay = _replay_typed_source_language_guard(
        authority, original_reference=verified_language_reference,
        srt_text=srt_text, spec=spec, durations=durations,
        validated_effects=validated_language_effects,
    )
    if source_language.get("typed_language_reference") is not None and typed_language_replay is None:
        raise CurrentTextContinuationError("CURRENT_SOURCE_LANGUAGE_TYPED_REFERENCE_UNVERIFIED")
    retained_language_input = typed_language_replay is not None
    if (source_language_status == 'SKIPPED_CUE_COUNT_MISMATCH'
            and type(source_language.get('draft_cue_count')) is int and source_language['draft_cue_count'] == 0
            and source_language.get('reverted') == [] and source_language.get('reverted_count') == 0
            and source_language.get('unproven_foreign_introductions') == []
            and verified_language_reference is not None):
        from src.autoslice.chat_evidence import normalize_chat_text
        from src.autoslice.human_sequence_alignment import apply_plans
        plans=(authority.get('human_sequence_alignment') or {}).get('records',[])
        current,_=apply_plans(srt_text,plans)
        reference,_=apply_plans(verified_language_reference,plans)
        def foreign_rows(text):
            return [(c.start_ms,c.end_ms,normalize_chat_text(c.text)) for c in parse_srt_cues(text)
                    if re.search(r'[A-Za-z\u3040-\u30ff]',c.text)]
        retained_language_input = bool(plans) and foreign_rows(current)==foreign_rows(reference)
    if source_language_status == _foreign_scope.OUTSIDE_IMMUTABLE_STORY_SCOPE:
        original_language = copy.deepcopy(dict(source_language))
        original_language["status"] = original_language.pop("original_status", None)
        original_language.pop("story_scope_projection", None)
        replayed_language = _foreign_scope.project_foreign_audit_to_story_scope(
            original_language, srt_text=srt_text, spec=spec, durations=durations,
        )
        if replayed_language != dict(source_language):
            # The language observation precedes deterministic source hygiene.
            # Replay its own hash-bound text, while requiring every foreign
            # surface to retain exact words and physical time in the successor.
            reference = authority.get('post_transcript_entity_output_srt')
            if (not isinstance(reference,str)
                    or hashlib.sha256(reference.encode()).hexdigest() !=
                    _continuation_normalize_sha(authority.get('post_transcript_entity_output_srt_sha256'))
                    or _foreign_surface_bindings(reference) != _foreign_surface_bindings(srt_text)
                    or _foreign_scope.project_foreign_audit_to_story_scope(
                        original_language,srt_text=reference,spec=spec,durations=durations) != dict(source_language)):
                raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_SCOPE_DRIFT")
    if not retained_language_input and source_language_status not in {
        "CLEAN",
        "DEFERRED_TO_SOURCE_SUBTITLE_TRUTH",
        "RESOLVED_BY_SOURCE_SUBTITLE_TRUTH",
        "DEFERRED_TO_REDELIVERY_BASELINE",
        "WITNESSED_FOREIGN_AUDIO_TRANSCRIPTION",
        "CPA_ADJUDICATED_FOREIGN_SPEAKER_AUDIO",
        _foreign_scope.OUTSIDE_IMMUTABLE_STORY_SCOPE,
    }:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_GUARD_BLOCKED"
        )
    for field in ("reverted", "unproven_foreign_introductions"):
        if not isinstance(source_language.get(field), list):
            raise CurrentTextContinuationError(
                f"CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_{field.upper()}_INVALID"
            )
    reverted_count = source_language.get("reverted_count")
    if reverted_count is not None and (
        isinstance(reverted_count, bool)
        or not isinstance(reverted_count, int)
        or reverted_count < 0
    ):
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_REVERTED_COUNT_INVALID"
        )
    # If a future/older producer included an explicit output binding, it must
    # still name the current bytes.  We never invent that binding from SRT.
    for field in ("input_srt_sha256", "output_srt_sha256", "srt_sha256"):
        if field in source_language and source_language.get(field) is not None:
            try:
                _continuation_require_sha(
                    srt_sha256,
                    source_language.get(field),
                    label=f"CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_{field.upper()}",
                )
            except CurrentTextContinuationError as exc:
                raise CurrentTextContinuationError(
                    "CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_SRT_DRIFT"
                ) from exc

    require_audit(
        "foreign_script_consistency_audit",
        "foreign-script-consistency-audit.v1",
    )
    try:
        replayed_foreign = _replay_source_stage_foreign_guard(
            authority, srt_text=srt_text, spec=spec, durations=durations,
        )
    except CurrentTextContinuationError:
        raise
    except (TypeError, ValueError, RuntimeError) as exc:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_FOREIGN_GUARD_REPLAY_FAILED"
        ) from exc
    if _foreign_guard_blocks(replayed_foreign):
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_FOREIGN_GUARD_BLOCKED"
        )

    if retained_transcript_input is not None:
        if authority.get("transcript_entity_audit") != retained_transcript_input:
            raise CurrentTextContinuationError("RETAINED_PREPARATION_INPUT_REPLAY_DRIFT")
    else:
        transcript_entity = require_audit(
            "transcript_entity_audit",
            "transcript-entity-audit.v1",
        )
        transcript_status = str(transcript_entity.get("status") or "")
        if transcript_status == "ENTITY_VERDICT_REQUIRED":
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_TRANSCRIPT_ENTITY_GUARD_BLOCKED"
            )
        if transcript_status not in {
            "NO_ENTITY",
            "VERIFIED",
            "APPLIED_AND_VERIFIED",
            "SKIPPED_TRUTH_FULL_OWNERSHIP",
        }:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_TRANSCRIPT_ENTITY_STATUS_INVALID"
            )
        for field in ("confirmed", "repairs", "entity_verdict_required"):
            if not isinstance(transcript_entity.get(field), list):
                raise CurrentTextContinuationError(
                    f"CURRENT_SOURCE_STAGE_TRANSCRIPT_ENTITY_{field.upper()}_INVALID"
                )
        for field in ("input_srt_sha256", "output_srt_sha256"):
            value = transcript_entity.get(field)
            if not isinstance(value, str) or len(value.removeprefix("sha256:")) != 64:
                raise CurrentTextContinuationError(
                    f"CURRENT_SOURCE_STAGE_TRANSCRIPT_ENTITY_{field.upper()}_INVALID"
                )
        if transcript_status == "NO_ENTITY" and (
            transcript_entity.get("input_srt_sha256")
            != transcript_entity.get("output_srt_sha256")
        ):
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_TRANSCRIPT_ENTITY_NO_ENTITY_DRIFT"
            )

    source_truth = require_audit(
        "source_subtitle_truth_audit",
        "source-subtitle-truth-audit.v1",
    )
    try:
        verify_source_truth_preview_formal_binding(authority, source_truth)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_SOURCE_TRUTH_PREVIEW_BINDING_INVALID"
        ) from exc
    try:
        post_transcript_text = authority.get("post_transcript_entity_output_srt")
        truth_input = post_transcript_text if isinstance(post_transcript_text, str) else srt_text
        replayed_srt, replayed_source_truth = apply_source_subtitle_truth(
            truth_input,
            spec=spec,
            durations=durations,
            ledger_path=source_truth_ledger_path,
            satisfaction_reviews=source_truth.get("meaning_reviews") or (),
            local_term_reviews=source_truth.get("local_term_reviews") or (),
        )
    except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_SOURCE_TRUTH_REPLAY_FAILED"
        ) from exc
    if truth_input != srt_text:
        # Reproduce the actual deterministic stages after source truth. A
        # historical snapshot is not relabeled as final, and arbitrary edits
        # still fail this byte comparison before any recovery can consume it.
        replayed_srt = _post_truth_release_hygiene(replayed_srt, copy.deepcopy(dict(authority)))
        replayed_srt, _ = normalize_japanese_native_script_surfaces(replayed_srt)
    if replayed_srt != srt_text:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_SOURCE_TRUTH_REVIEW_REQUIRED"
        )
    if not _source_truth_replay_matches(source_truth, replayed_source_truth):
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_SOURCE_TRUTH_AUDIT_DRIFT"
        )
    if str(replayed_source_truth.get("status") or "").startswith("FAILED"):
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_SOURCE_TRUTH_BLOCKED"
        )

    final_title_marks = require_audit(
        "final_title_mark_balance_audit",
        "title-mark-balance-audit.v1",
    )
    replayed_title_text, replayed_title_marks = apply_title_mark_balance_guard(srt_text)
    if replayed_title_text != srt_text:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_TITLE_MARK_REVIEW_REQUIRED"
        )
    if replayed_title_marks != dict(final_title_marks):
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_TITLE_MARK_GUARD_DRIFT"
        )
    if replayed_title_marks.get("status") == "UNRESOLVED_COMPLEX_IMBALANCE":
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_TITLE_MARK_GUARD_BLOCKED"
        )


def _source_stage_original_language_witness(
    authority: Mapping[str, object],
    spec: Mapping[str, object],
    padded: Path,
    padded_dur: int,
) -> str | None:
    """Load the saved original substrate, never the current repaired text."""

    expected_input_sha = _continuation_normalize_sha(authority.get("input_srt_sha256"))
    if len(expected_input_sha) != 64:
        return None

    config_candidates: list[Mapping[str, object]] = []
    existing_surface = spec.get("existing_subtitle_surface")
    if isinstance(existing_surface, Mapping):
        config_candidates.append(existing_surface)
    for key in ("source_cache_checkpoint", "existing_subtitle_surface_receipt"):
        value = spec.get(key)
        if isinstance(value, Mapping):
            config_candidates.append(value)

    witness_path: Path | None = None
    declared_sha: str | None = None
    declared_media_sha: str | None = None
    witness_time_domain: object = None
    for config in config_candidates:
        path_value = config.get("path") or config.get("existing_srt_path")
        if not path_value:
            continue
        if (
            config.get("time_domain") is not None
            and config.get("time_domain") != "padded_local"
        ):
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_BINDING_INVALID"
            )
        if (
            config.get("candidate_id") is not None
            and config.get("candidate_id") != spec.get("candidate_id")
        ):
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_BINDING_INVALID"
            )
        candidate_path = Path(str(path_value))
        candidate_sha = _continuation_normalize_sha(
            config.get("sha256") or config.get("existing_srt_sha256")
        )
        if len(candidate_sha) != 64 or candidate_sha != expected_input_sha:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_BINDING_INVALID"
            )
        witness_path = candidate_path
        declared_sha = candidate_sha
        declared_media_sha = _continuation_normalize_sha(config.get("source_media_sha256"))
        witness_time_domain = config.get("time_domain")
        break
    if witness_path is None:
        from src.autoslice.subtitle_draft_preparation import load_recorded_asr_boundary_origin
        try:
            return load_recorded_asr_boundary_origin(padded)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CurrentTextContinuationError('CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_DRIFT') from exc
    if witness_path.is_symlink() or not witness_path.is_file():
        return None
    if declared_media_sha and witness_time_domain == "padded_local":
        try:
            _continuation_require_sha(
                _continuation_sha256_path(padded, label="CURRENT_PADDED_MEDIA"),
                declared_media_sha,
                label="CURRENT_SOURCE_LANGUAGE_WITNESS_MEDIA",
            )
        except CurrentTextContinuationError:
            raise
    try:
        raw = witness_path.read_bytes()
        witness_sha = hashlib.sha256(raw).hexdigest()
        value = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    if witness_sha != declared_sha or witness_sha != expected_input_sha:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_DRIFT"
        )
    try:
        cues = [cue for cue in parse_srt_cues(value) if cue.text.strip()]
    except (TypeError, ValueError) as exc:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_INVALID"
        ) from exc
    if not cues or any(
        cue.start_ms < 0 or cue.end_ms <= cue.start_ms or cue.end_ms > padded_dur
        for cue in cues
    ):
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_GRID_INVALID"
        )
    return value


def _source_language_count_recheck_pending(
    authority: Mapping[str, object], *, spec: Mapping[str, object], durations: Sequence[int],
    padded: Path, padded_dur: int, srt_text: str, clip_context: Mapping[str, object],
) -> bool:
    """Resume finalization for a proven legacy cue-count skip, without ASR.

    The old immutable audit stays intact. The ordinary finalizer re-emits all
    current audits and its successor must pass every source-stage guard.
    """
    audit = authority.get("final_source_language_preservation_audit")
    if (not isinstance(audit, Mapping) or audit.get("status") != "SKIPPED_CUE_COUNT_MISMATCH"
            or type(audit.get("draft_cue_count")) is not int or audit["draft_cue_count"] <= 0
            or audit.get("reverted") != [] or audit.get("reverted_count") != 0
            or audit.get("unproven_foreign_introductions") != []):
        return False
    witness = _source_stage_original_language_witness(authority, spec, padded, padded_dur)
    if not witness:
        return False
    text, replay = _foreign_scope.guard_source_language(
        witness, srt_text, spec, durations,
        structured_chat_names=_structured_chat_names(clip_context),
    )
    return (
        text == srt_text and replay.get("status") == "CLEAN"
        and replay.get("cue_binding") == "UNCHANGED_FOREIGN_CUE_INTERVALS"
        and all(type(audit.get(key)) is int and audit[key] == replay.get(key)
                for key in ("draft_cue_count", "final_cue_count"))
    )


def _append_source_stage_review_history(
    authority: dict[str, object],
    previous_review: Mapping[str, object],
    *,
    srt_sha256: str,
) -> None:
    """Retain the complete predecessor audit before replacing current review."""

    history_key = "final_review_history"
    existing = authority.get(history_key)
    if existing is None:
        history: list[object] = []
    elif isinstance(existing, list):
        history = copy.deepcopy(existing)
    else:
        raise CurrentTextContinuationError(
            "CURRENT_SOURCE_STAGE_FINAL_REVIEW_HISTORY_INVALID"
        )
    history.append(
        {
            "srt_sha256": "sha256:" + _continuation_normalize_sha(srt_sha256),
            "context_adjudication_count": previous_review.get(
                "context_adjudication_count", 0
            ),
            "applied_count": previous_review.get("applied_count", 0),
            "final_review_audit": copy.deepcopy(dict(previous_review)),
        }
    )
    authority[history_key] = history


def _pending_rebuilt_span_consumed(
    finding: Mapping[str, object], row: Mapping[str, object], cue: SrtCue,
    adjudication: Mapping[str, object], request: Mapping[str, object],
) -> bool:
    """A normal whole-cue decision may represent only its minimal edit span."""
    rebuild = adjudication.get("proposal_rebuild")
    rebuilt = adjudication.get("rebuilt_finding")
    mutation = adjudication.get("mutation_authority")
    witness_judge = adjudication.get("witness_judge")
    if not all(isinstance(value, Mapping) for value in (rebuild, rebuilt, mutation, witness_judge)):
        return False
    judge = witness_judge.get("judge")
    reuse = rebuild.get("witness_reuse")
    if not isinstance(judge, Mapping) or not isinstance(reuse, Mapping):
        return False
    if not (
        rebuild.get("status") == "PROPOSED"
        and adjudication.get("repaired") is True
        and adjudication.get("timing_immutable") is True
        and adjudication.get("decision_authority") == "CPA_JUDGE"
        and mutation.get("status") == "PASS"
        and judge.get("status") == "JUDGED"
        and judge.get("choice") == "PROPOSED"
        and judge.get("selected_candidate_text") == request.get("proposed_cue")
        and judge.get("check_request_sha256") == request.get("request_sha256")
        and reuse.get("status") == "PASS"
        and reuse.get("basis") == "IDENTICAL_AUDIO_GEOMETRY"
        and len(_continuation_normalize_sha(reuse.get("original_witness_request_sha256"))) == 64
        and reuse.get("original_witness_request_sha256") == reuse.get("reused_witness_request_sha256")
    ):
        return False
    span_keys = ("cue_index", "base_text_sha256", "suspect", "suggestion",
                 "span_start_codepoint", "span_end_codepoint", "proposed_full_cue")
    if any(rebuilt.get(key) != finding.get(key) for key in span_keys):
        return False
    start, end = finding.get("span_start_codepoint"), finding.get("span_end_codepoint")
    old_start, old_end = row.get("span_start_codepoint"), row.get("span_end_codepoint")
    if not all(type(value) is int for value in (start, end, old_start, old_end)):
        return False
    suggestion = finding.get("suggestion")
    if not (
        0 <= old_start <= start < end <= old_end <= len(cue.text)
        and cue.text[old_start:old_end] == row.get("suspect")
        and cue.text[start:end] == request.get("suspect") == finding.get("suspect")
        and isinstance(suggestion, str)
        and request.get("replacement") == suggestion
        and cue.text[:start] + suggestion + cue.text[end:]
        == request.get("proposed_cue") == finding.get("proposed_full_cue")
    ):
        return False
    payload = dict(request)
    declared_sha = _continuation_normalize_sha(payload.pop("request_sha256", ""))
    if declared_sha != hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest():
        return False
    from src.autoslice.acoustic_witness_adjudication import build_witness_request

    try:
        witness_request = build_witness_request(request)
    except (KeyError, TypeError, ValueError):
        return False
    geometry_keys = ("kind", "cue_indexes", "matched_start_ms", "matched_end_ms",
                     "context_start_ms", "context_end_ms", "source_media_timeline_offset_ms")
    geometry_sha = hashlib.sha256(json.dumps(
        {key: witness_request.get(key) for key in geometry_keys},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return (
        _continuation_normalize_sha(reuse.get("geometry_sha256")) == geometry_sha
        and _continuation_normalize_sha(reuse.get("rebuilt_witness_request_sha256"))
        == witness_request["request_sha256"]
    )


def _pending_review_consumed(
    pending_scope: Mapping[str, object], final_review: Mapping[str, object], srt_text: str
) -> bool:
    """Require every nominated pending cue to reach the ordinary review output."""

    rows = pending_scope.get("rows")
    findings = final_review.get("findings")
    if not isinstance(rows, list) or not isinstance(findings, list):
        return False
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    consumed_indexes: set[int] = set()
    def target_context_adjudication(
        finding: Mapping[str, object], row: Mapping[str, object], cue: object
    ) -> bool:
        adjudication = finding.get("context_audio_adjudication")
        if not isinstance(adjudication, Mapping):
            return False
        if adjudication.get("status") in {
            "SKIPPED_BUDGET",
            "STALE_FINDING_SKIPPED",
            "SAME_CUE_REENTRY_UNRESOLVED",
        }:
            return False
        request = adjudication.get("request")
        verdict = adjudication.get("verdict")
        if not isinstance(request, Mapping) or not isinstance(verdict, Mapping):
            return False
        index = row.get("cue_index")
        expected_base = _continuation_normalize_sha(row.get("base_text_sha256"))
        # Ordinary review applies typed repairs sequentially. Other cues may
        # already be corrected when this request is built; bind the nominated
        # cue in that real request instead of requiring the old whole SRT.
        request_srt = request.get("whole_clip_current_srt")
        if not isinstance(request_srt, str):
            return False
        try:
            request_cues = [value for value in parse_srt_cues(request_srt) if value.text.strip()]
        except (TypeError, ValueError):
            return False
        if not 1 <= index <= len(request_cues):
            return False
        request_cue = request_cues[index - 1]
        if (
            request_cue.text != cue.text
            or request_cue.start_ms != cue.start_ms
            or request_cue.end_ms != cue.end_ms
            or request.get("current_cue") != cue.text
            or _continuation_normalize_sha(request.get("base_text_sha256")) != expected_base
            or request.get("cue_indexes") != [index]
            or request.get("matched_start_ms") != cue.start_ms
            or request.get("matched_end_ms") != cue.end_ms
            or type(request.get("source_media_timeline_offset_ms")) is not int
            or request.get("source_media_timeline_offset_ms") < 0
        ):
            return False
        if request.get("suspect") != row.get("suspect") and not _pending_rebuilt_span_consumed(
            finding, row, cue, adjudication, request,
        ):
            return False
        return isinstance(adjudication.get("repaired"), bool)

    def target_typed_repair(
        finding: Mapping[str, object], row: Mapping[str, object], cue: object
    ) -> bool:
        if finding.get("routed") not in {"expected_value_canon", "exact_cue_canon"}:
            return False
        if _continuation_normalize_sha(row.get("base_text_sha256")) != hashlib.sha256(
            cue.text.encode("utf-8")
        ).hexdigest():
            return False
        mutation = finding.get("mutation_authority") or finding.get("orthography_authority")
        before = finding.get("before")
        after = finding.get("after")
        return (
            isinstance(mutation, Mapping)
            and mutation.get("status") == "PASS"
            and isinstance(before, list)
            and before == [cue.text]
            and isinstance(after, list)
            and len(after) == 1
            and finding.get("cue_index") == row.get("cue_index")
        )

    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        index = finding.get("cue_index")
        if type(index) is not int or not 1 <= index <= len(cues):
            continue
        cue = cues[index - 1]
        for row in rows:
            if not isinstance(row, Mapping) or finding.get("cue_index") != row.get("cue_index"):
                continue
            from src.autoslice.final_review_meaning_policy import current_meaning_equivalent
            equivalent = (finding.get('suspect') == row.get('suspect')
                and _continuation_normalize_sha(finding.get('base_text_sha256'))
                == _continuation_normalize_sha(row.get('base_text_sha256'))
                == hashlib.sha256(cue.text.encode()).hexdigest()
                and is_keep_current_disclosed(finding)
                and current_meaning_equivalent(finding,expected_srt_sha256='sha256:'+
                    hashlib.sha256(srt_text.encode()).hexdigest()))
            if equivalent or target_context_adjudication(finding, row, cue) or target_typed_repair(finding,row,cue):
                consumed_indexes.add(int(row["cue_index"]))
                break
    return all(
        isinstance(row, Mapping)
        and type(row.get("cue_index")) is int
        and row.get("cue_index") in consumed_indexes
        for row in rows
    )


def _pending_source_review_contract_sha256() -> str:
    """Invalidate semantic observations without invalidating media/ASR caches."""
    import inspect

    digest = hashlib.sha256()
    for name in ("final_review_auditor.py", "deferred_same_cue_resolution.py",
                 "final_review_provider_budget.py", "producer_final_review_transport.py"):
        digest.update(name.encode() + b"\0" + Path(__file__).with_name(name).read_bytes())
    for function in (_run_final_review, _pending_review_consumed):
        digest.update(inspect.getsource(function).encode())
    call = _build_final_review_llm_call()
    digest.update(json.dumps(getattr(call, "cpa_cache_identity", None), sort_keys=True).encode())
    return "sha256:" + digest.hexdigest()


def _persist_pending_source_review_observation(
    *,
    snapshot: Mapping[str, Any],
    pending_scope: Mapping[str, object],
    reviewed_srt: str,
    resumed_review: object,
    prior_count: int,
    cid: str,
    out_root: Path,
) -> Path:
    """Keep the actual review before a continuation guard can reject it.

    This observation has no text or release authority.  In particular, do
    not replace the retained SRT/audit with a review whose pending coverage
    has not passed.  Content-addressed files preserve successive failures.
    """
    observation = {
        "schema_version": "source-stage-pending-review-observation.v1",
        "authority": "EVIDENCE_ONLY",
        "candidate_id": cid,
        "source_srt_sha256": snapshot["srt_sha256"],
        "source_srt": snapshot["srt_text"],
        "source_authority_sha256": snapshot["chat_authority_sha256"],
        "clip_context_sha256": snapshot["clip_context_sha256"],
        "padded_sha256": snapshot["padded_sha256"],
        "source_media_sha256s": snapshot["source_media_sha256s"],
        "pending_review_scope": copy.deepcopy(dict(pending_scope)),
        "prior_provider_adjudication_count": prior_count,
        "reviewed_srt": reviewed_srt,
        "reviewed_srt_sha256": hashlib.sha256(reviewed_srt.encode("utf-8")).hexdigest(),
        "final_review_audit": copy.deepcopy(resumed_review),
        "pending_review_consumed": (
            isinstance(resumed_review, Mapping)
            and _pending_review_consumed(pending_scope, resumed_review, snapshot["srt_text"])
        ),
        "mutation_authorized": False,
        "release_authorized": False,
        "semantic_review_contract_sha256": _pending_source_review_contract_sha256(),
    }
    data = (json.dumps(observation, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    path = out_root / f"{cid}.pending-source-review.{digest}.json"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != data:
            raise CurrentTextContinuationError("CURRENT_SOURCE_STAGE_REVIEW_OBSERVATION_DRIFT")
    else:
        from src.autoslice.package_import import atomic_write_bytes

        atomic_write_bytes(path, data)
    # The ordinary worker must consume its own saved observation after a
    # later guard/interruption. Keep immutable observations; only advance the
    # native checkpoint pointer after their complete bytes are on disk.
    from src.autoslice.producer_media import _write_json_atomic

    _write_json_atomic(out_root / f"{cid}.pending-source-review.json", {
        "path": str(path.resolve()), "sha256": "sha256:" + digest,
    })
    return path


def _discover_pending_source_review_observation(
    *, root: Path, snapshot: Mapping[str, Any], pending_scope: Mapping[str, object],
    prior_count: int, cid: str,
) -> tuple[str, dict[str, Any]] | None:
    """Recover durable candidate observations when their pointer is missing/stale."""
    if not root.exists() and not root.is_symlink():
        return None
    if root.is_symlink() or not root.is_dir():
        raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_CHECKPOINT_ROOT_INVALID")
    prefix = f"{cid}.pending-source-review."
    matched = None
    matched_review = None
    for path in sorted(root.iterdir()):
        if path.name == f"{cid}.pending-source-review.json" or not path.name.startswith(prefix) or not path.name.endswith(".json"):
            continue
        declared = path.name[len(prefix):-len(".json")]
        observation, digest = _continuation_read_json(
            path, label="CURRENT_SOURCE_REVIEW_OBSERVATION", review=True,
        )
        _continuation_require_sha(digest, declared, label="CURRENT_SOURCE_REVIEW_OBSERVATION")
        if observation.get("candidate_id") != cid:
            raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_INPUT_DRIFT")
        replayed = _replay_pending_source_review_observation(
            observation=observation, digest=digest, automatic=True,
            snapshot=snapshot, pending_scope=pending_scope, prior_count=prior_count, cid=cid,
        )
        if replayed is None:
            continue
        comparable = copy.deepcopy(replayed[1])
        # Re-persisting a replay adds only this informational receipt. Compare
        # the validated review itself, never file recency or number of copies.
        comparable.pop("source_pending_review_replay", None)
        if matched is not None and (replayed[0], comparable) != (matched[0], matched_review):
            raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_AMBIGUOUS")
        if matched is None:
            matched, matched_review = replayed, comparable
    return matched


def _load_pending_source_review_observation(
    *, spec: Mapping[str, object], snapshot: Mapping[str, Any],
    pending_scope: Mapping[str, object], prior_count: int, cid: str,
) -> tuple[str, dict[str, Any]] | None:
    """Replay a saved ordinary review through its typed repairs, without calls."""
    config = spec.get("source_pending_review_observation")
    automatic = config is None
    if automatic:
        root = Path(snapshot.get("checkpoint_root") or Path(snapshot["chat_authority_path"]).parent)
        pointer = root / f"{cid}.pending-source-review.json"
        if not pointer.exists() and not pointer.is_symlink():
            # The immutable observation is durable before pointer commit.
            return _discover_pending_source_review_observation(
                root=root, snapshot=snapshot, pending_scope=pending_scope, prior_count=prior_count, cid=cid,
            )
        config, _ = _continuation_read_json(pointer, label="CURRENT_SOURCE_REVIEW_CHECKPOINT")
    if not isinstance(config, Mapping) or not config.get("path"):
        raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_BINDING_MISSING")
    observation, digest = _continuation_read_json(
        Path(str(config["path"])), label="CURRENT_SOURCE_REVIEW_OBSERVATION", review=True,
    )
    _continuation_require_sha(digest, config.get("sha256"), label="CURRENT_SOURCE_REVIEW_OBSERVATION")
    replayed = _replay_pending_source_review_observation(
        observation=observation, digest=digest, automatic=automatic,
        snapshot=snapshot, pending_scope=pending_scope, prior_count=prior_count, cid=cid,
    )
    if automatic and replayed is None:
        # A former input's valid pointer can survive the same commit window;
        # it must not hide an already complete observation for the new input.
        return _discover_pending_source_review_observation(
            root=root, snapshot=snapshot, pending_scope=pending_scope, prior_count=prior_count, cid=cid,
        )
    return replayed


def _replay_saved_correction_review(
    *, input_srt_text: str, output_srt_text: str, review: Mapping,
    clip_context: Mapping, validate_normal_repair: Callable | None = None,
) -> list[dict]:
    """Replay the normal typed mutation chain without redispatching its judges."""
    from src.autoslice.deferred_same_cue_resolution import _staged_owner, append_or_compose_staged_owner

    replayed = input_srt_text
    owners = []
    applied = 0
    for finding in review["findings"]:
        if not isinstance(finding, Mapping):
            raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_FINDING_INVALID")
        adjudication = finding.get("context_audio_adjudication")
        if not isinstance(adjudication, Mapping) or adjudication.get("repaired") is not True:
            continue
        request = adjudication.get("request")
        index = finding.get("cue_index")
        cues = [cue for cue in parse_srt_cues(replayed) if cue.text.strip()]
        if not isinstance(request, Mapping) or type(index) is not int or not 1 <= index <= len(cues):
            raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_REPAIR_INVALID")
        request_cues = [cue for cue in parse_srt_cues(str(request.get("whole_clip_current_srt") or "")) if cue.text.strip()]
        cue = cues[index - 1]
        nominated = {"cue_index": index, "suspect": cue.text,
                     "base_text_sha256": hashlib.sha256(cue.text.encode("utf-8")).hexdigest(),
                     "span_start_codepoint": 0, "span_end_codepoint": len(cue.text)}
        if request_cues != cues:
            raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_TYPED_REPAIR_DRIFT")
        if validate_normal_repair is None:
            # Pending whole-cue reconstruction keeps its existing strict
            # coverage predicate. Completed ordinary review has a separate
            # native request/witness/raw-decision replay supplied by its reader.
            if not _pending_rebuilt_span_consumed(finding, nominated, cue, adjudication, request):
                raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_TYPED_REPAIR_DRIFT")
        else:
            validate_normal_repair(finding=finding, adjudication=adjudication, request=request,
                                   cue=cue, srt_text=replayed, clip_context=clip_context)
        owner = _staged_owner(index, adjudication)
        _continuation_validate_existing_repair(owner, srt_text=replayed, clip_context=clip_context)
        append_or_compose_staged_owner(owners, owner)
        cues[index - 1] = SrtCue(cue.index, cue.start_ms, cue.end_ms, request["proposed_cue"])
        replayed = _render_cues_to_srt(cues)
        applied += 1
    if replayed != output_srt_text or type(review.get("applied_count")) is not int or review["applied_count"] != applied:
        raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_TYPED_OUTPUT_DRIFT")
    return owners


def _replay_pending_source_review_observation(
    *, observation: Mapping[str, Any], digest: str, automatic: bool,
    snapshot: Mapping[str, Any], pending_scope: Mapping[str, object], prior_count: int, cid: str,
) -> tuple[str, dict[str, Any]] | None:
    """Validate immutable observation identity, coverage, and each typed repair."""
    bindings = {
        "candidate_id": cid, "source_srt_sha256": snapshot["srt_sha256"],
        "source_srt": snapshot["srt_text"],
        "source_authority_sha256": snapshot["chat_authority_sha256"],
        "clip_context_sha256": snapshot["clip_context_sha256"],
        "padded_sha256": snapshot["padded_sha256"],
        "source_media_sha256s": list(snapshot["source_media_sha256s"]),
        "pending_review_scope": dict(pending_scope),
        "prior_provider_adjudication_count": prior_count,
    }
    if (
        observation.get("schema_version") != "source-stage-pending-review-observation.v1"
        or observation.get("authority") != "EVIDENCE_ONLY"
        or observation.get("mutation_authorized") is not False
        or observation.get("release_authorized") is not False
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_INPUT_DRIFT")
    if any(observation.get(key) != value for key, value in bindings.items()):
        if automatic:
            # New source/text/context/pending inputs invalidate this stage's
            # observation only. Native ASR/media/witness caches stay intact.
            return None
        raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_INPUT_DRIFT")
    if automatic and observation.get("semantic_review_contract_sha256") != _pending_source_review_contract_sha256():
        return None
    review = observation.get("final_review_audit")
    reviewed_srt = observation.get("reviewed_srt")
    if not (
        isinstance(review, Mapping) and review.get("schema_version") == "final-review-audit.v1"
        and review.get("status") in {"APPLIED", "FLAGGED", "CLEAN"}
        and isinstance(reviewed_srt, str)
        and type(review.get("context_adjudication_count")) is int
        and review["context_adjudication_count"] >= 0
        and _pending_review_consumed(pending_scope, review, snapshot["srt_text"])
    ):
        raise CurrentTextContinuationError("CURRENT_SOURCE_REVIEW_OBSERVATION_REVIEW_INVALID")
    _continuation_require_sha(
        hashlib.sha256(reviewed_srt.encode("utf-8")).hexdigest(),
        observation.get("reviewed_srt_sha256"), label="CURRENT_SOURCE_REVIEW_OBSERVATION_OUTPUT",
    )
    _replay_saved_correction_review(
        input_srt_text=snapshot["srt_text"], output_srt_text=reviewed_srt,
        review=review, clip_context=snapshot["clip_context"])
    applied = review["applied_count"]
    review = copy.deepcopy(dict(review))
    review["source_pending_review_replay"] = {
        "observation_sha256": "sha256:" + digest, "provider_calls_this_replay": 0,
        "historical_context_adjudication_count": review["context_adjudication_count"],
        "replayed_typed_repairs": applied, "release_authorized": False,
    }
    return reviewed_srt, review


def _carry_source_usage_parent(authority, snapshot, *, out_root):
    parent = snapshot.get('source_usage_parent')
    if parent is None:
        return
    from src.autoslice.producer_text_checkpoint import _frozen_binding
    path = Path(parent['path'])
    live, actual = _continuation_read_json(path, label='SOURCE_REPLAY_USAGE', review=True)
    _continuation_require_sha(actual, parent['sha256'], label='SOURCE_REPLAY_USAGE')
    checkpoint_parent = parent.get('source_checkpoint_parent')
    if checkpoint_parent is not None:
        from src.autoslice.source_checkpoint_parent import bound, _usage
        bound(checkpoint_parent['live_authority'])
        usage, field = _usage(live)
        if usage != checkpoint_parent['cpa_usage'] or field != checkpoint_parent['cpa_usage_source_field']:
            raise CurrentTextContinuationError('SOURCE_REPLAY_USAGE_DRIFT')
        existing = authority.get('cpa_adjudication_usage')
        if existing is not None:
            count = _retained_parent_cpa_usage_seed(authority)['provider_adjudication_count']
            if count > usage['provider_adjudication_count']:
                usage = copy.deepcopy(existing)
        authority['cpa_adjudication_usage'] = usage
        authority['source_input_observation_parent'] = {
            key: copy.deepcopy(checkpoint_parent[key]) for key in
            ('source_checkpoint', 'live_authority', 'native_budget', 'cpa_journal')}
        authority['source_input_observation_parent']['release_authorized'] = False
    else:
        authority['cpa_adjudication_usage'] = _retained_parent_cpa_usage_seed(live)
    if live.get('legacy_retained_source_usage_reconciliation') is not None:
        authority['legacy_retained_source_usage_reconciliation'] = copy.deepcopy(
            live['legacy_retained_source_usage_reconciliation'])
    authority['source_input_usage_parent'] = {
        **_frozen_binding(out_root/'source-text-input', 'usage_parent', path),
        'role': 'CUMULATIVE_USAGE_ONLY',
    }


def _source_observation_scope(snapshot, *, out_root, cid):
    """Nominate every retained delivery concern on its exact source cue.

    Geometry replay proves the words are unchanged. Old acoustic decisions,
    proposals and release verdicts are history, never mutation authority.
    """
    parent = snapshot.get('source_observation_parent')
    if parent is None:
        return None, None
    live, actual = _continuation_read_json(
        Path(parent['path']), label='SOURCE_REPLAY_OBSERVATIONS', review=True,
    )
    _continuation_require_sha(actual, parent['sha256'], label='SOURCE_REPLAY_OBSERVATIONS')
    if not _source_checkpoint_reproduces_delivery(out_root=out_root, cid=cid,
            source_text=snapshot['srt_text'], source_authority=snapshot['chat_authority'],
            live_authority=live, allow_unresolved_current=True):
        raise CurrentTextContinuationError('SOURCE_REPLAY_OBSERVATIONS_GEOMETRY_DRIFT')
    review = live['final_review_audit']
    delivery = Path(live.get('final_text_srt_path') or out_root/'replacement_recuts'/f'{cid}.recut.srt')
    delivery_cues = parse_srt_cues(delivery.read_text(encoding='utf-8'))
    projected, _ = _continuation_project_delivery_to_source(delivery_cues, final_review_audit=review, authority=live)
    source_cues = parse_srt_cues(snapshot['srt_text'])
    rows = []
    for finding in review['findings']:
        index = finding.get('cue_index')
        if type(index) is not int or not 1 <= index <= len(projected):
            raise CurrentTextContinuationError('SOURCE_REPLAY_OBSERVATION_CUE_INVALID')
        cue = projected[index-1]
        matches = [(i, current) for i, current in enumerate(source_cues, 1)
            if current.text == cue.text and min(current.end_ms,cue.end_ms) > max(current.start_ms,cue.start_ms)]
        suspect = finding.get('suspect')
        if len(matches) != 1 or not isinstance(suspect,str) or not suspect or cue.text.count(suspect) != 1:
            raise CurrentTextContinuationError('SOURCE_REPLAY_OBSERVATION_OWNER_DRIFT')
        source_index, current = matches[0]
        start = current.text.index(suspect)
        from src.autoslice.semantic_retry_progress import continuation_plan
        plan = continuation_plan(finding.get('continuation_plan'))
        rows.append({'cue':source_index,'cue_index':source_index,
            'kind':finding.get('kind') or 'context','suspect':suspect,
            'suggestion':None,'proposed_full_cue':None,
            'repair_class':finding.get('repair_class') or 'phonetic',
            'why':str(finding.get('why') or 'Retained unresolved source observation'),
            'base_text_sha256':'sha256:'+hashlib.sha256(current.text.encode()).hexdigest(),
            'span_start_codepoint':start,'span_end_codepoint':start+len(suspect),
            'current_srt_sha256':'sha256:'+snapshot['srt_sha256'],
            **({'continuation_plan':plan} if plan is not None else {})})
    return {'rows':rows,'parent':copy.deepcopy(parent)}, review


def _resume_pending_source_finalization(
    *, spec, durations, padded, padded_dur, host, text_override_path, cid, out_root,
    substrate, correct, screen_text, adapters, merged, authoritative_chat, snapshot,
    validated_review, pending_review, native_audio_state=None, witness_cache_roots=(),
):
    """Consume a proven partial native input through the ordinary remaining stages."""
    from src.autoslice.continuation_subchecks import end_cold_read
    end_cold_read()  # the remaining stages are provider-backed, not the cold read
    from src.autoslice.producer_text_checkpoint import PARTIAL_SCHEMA, write_source_text_checkpoint
    from src.autoslice.retained_preparation_checkpoint import RETAINED_PARTIAL_SCHEMA, validate_retained_partial
    authority = copy.deepcopy(dict(snapshot['chat_authority']))
    _carry_source_usage_parent(authority, snapshot, out_root=out_root)
    from src.autoslice.retained_source_usage import reconcile_legacy_source_usage
    reconcile_legacy_source_usage(authority)
    current = snapshot['srt_text']
    native = snapshot.get('native_checkpoint') or {}
    retained_partial = native.get('schema_version') == RETAINED_PARTIAL_SCHEMA
    if retained_partial:
        validate_retained_partial(
            native=native, authority=authority, retained=snapshot.get('validated_retained'))
        witness = ""
        name_reference = Path(native['name_reference']['path']).read_text(encoding='utf-8')
    elif native.get('schema_version') == PARTIAL_SCHEMA:
        witness = Path(native['language_witness']['path']).read_text(encoding='utf-8')
        name_reference = Path(native['name_reference']['path']).read_text(encoding='utf-8')
    else:
        witness = _source_stage_original_language_witness(authority, spec, padded, padded_dur)
        # This is a presentation reference for already validated typed text,
        # never the independent source-language witness above.
        name_reference = current
    if not retained_partial:
        if not witness:
            raise CurrentTextContinuationError('CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_MISSING')
        witness_cues = parse_srt_cues(witness)
        if not witness_cues or any(c.start_ms < 0 or c.end_ms <= c.start_ms or c.end_ms > padded_dur for c in witness_cues):
            raise CurrentTextContinuationError('CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_GRID_INVALID')
    language_guard_reference = (_typed_source_language_reference(
        witness, current, _source_language_effect_rows(authority))
        if _needs_typed_language_reference(authority) else None)
    ledger = None if str(spec.get('human_truth_mode') or 'delivery') == 'withheld' else adapters.profile_asset_file('subtitle_truth_ledger')
    from src.autoslice.producer_source_truth_checkpoint import rebase_pending_previews
    try:
        rebase_pending_previews(authority, spec=spec, durations=durations, ledger_path=ledger,
            origin_path=getattr(adapters,'source_truth_origin_path',ledger),
            clip_context=snapshot['clip_context'], name_reference=name_reference,
            current_text=current, out_root=out_root, allow_added_local_terms=pending_review)
    except (OSError, ValueError, RuntimeError) as exc:
        raise CurrentTextContinuationError(str(exc)) from exc
    human = authority.get('human_sequence_alignment') or {}
    from src.autoslice.human_sequence_alignment import validate_records, verify_surfaces, protected_indexes
    if human.get('records'):
        validate_records(human, ledger_path=ledger, spec=spec, durations=durations)
        if not verify_surfaces(human,text=current,speaker=current,delivery_start_ms=0,delivery_end_ms=padded_dur):
            raise CurrentTextContinuationError('HUMAN_SEQUENCE_SOURCE_LITERAL_DRIFT')
    draft = _transcribe_draft(spec=spec,padded=padded,padded_dur=padded_dur,host=host,
        substrate=substrate,correct=correct,screen_text=screen_text,merged=merged,adapters=adapters,
        retained_srt_text=current)
    context = _build_entity_verification_context(spec=spec,padded=padded,padded_dur=padded_dur,
        host=host,text_override_path=text_override_path,cid=cid,out_root=out_root,srt_text=current,
        authoritative_chat=authoritative_chat,adapters=adapters,
        native_audio_state=native_audio_state,witness_cache_roots=witness_cache_roots)
    review = copy.deepcopy(dict(validated_review))
    pending_scope, delivery_review = _source_observation_scope(snapshot, out_root=out_root, cid=cid)
    if pending_scope is not None:
        authority['retained_delivery_review_history'] = {
            'parent':{**copy.deepcopy(authority['source_input_usage_parent']),
                      'role':'PENDING_OBSERVATIONS_ONLY'},
            'review':copy.deepcopy(delivery_review),'nominations':copy.deepcopy(pending_scope['rows']),
            'role':'HISTORY_AND_REVIEW_NOMINATIONS_ONLY',
        }
    if pending_review:
        previous_usage = authority.get('cpa_adjudication_usage')
        old_usage = (previous_usage.get('provider_adjudication_count') if isinstance(previous_usage, Mapping)
            else review.get('context_adjudication_count', 0))
        if type(old_usage) is not int or old_usage < 0:
            raise CurrentTextContinuationError('CURRENT_SOURCE_STAGE_CPA_ADJUDICATION_USAGE_INVALID')
        _append_source_stage_review_history(authority,review,srt_sha256=snapshot['srt_sha256'])
        _, truth_audit = apply_source_subtitle_truth(current, spec=spec, durations=durations, ledger_path=ledger)
        truth_preview = build_source_truth_preview_receipt(
            input_srt_text=current, source_truth_audit=truth_audit, stage='pre_correction_review')
        authority.setdefault('source_truth_preview_receipts', {})['pre_correction_review'] = truth_preview
        cues = parse_srt_cues(current)
        handled = {int(c.index) for c in cues for key in ('applied','entity_repairs','coreference_repairs')
            for row in authority.get(key) or [] if c.start_ms == row.get('matched_start_ms')
            and c.end_ms == row.get('matched_end_ms') and c.text in (row.get('after') or [])}
        review_input = current
        observation_scope = pending_scope or {'rows':[], 'truth_transition':copy.deepcopy(
            authority.get('source_truth_protection_transition'))}
        saved = _load_pending_source_review_observation(spec=spec,snapshot=snapshot,
            pending_scope=observation_scope,prior_count=old_usage,cid=cid)
        if saved is not None:
            current, review = saved
        else:
            current, review = _run_final_review(srt_text=current,chat_authority_audit=authority,
            source_media_sha256=(spec.get('source_piece_clock') or {}).get('padded_sha256'),
            handled_entity_cues=handled,verify_confusable_entity=context.verify_confusable_entity,
            authoritative_chat=authoritative_chat,adapters=adapters,
            selection_hook=str(spec.get('selection_hook') or ''),referent_groups=context.referent_groups,
            clip_context=snapshot['clip_context'],source_truth_protected_cue_indexes=tuple(sorted(
                set(protected_indexes(human,snapshot['srt_text'])) | set(truth_preview['protected_cue_indexes']))),
            carryover_file=carryover_path(out_root,cid),priority_raw_findings=(
                tuple(pending_scope['rows']) if pending_scope is not None
                else _review_priority_candidates(padded,current)),
            draft_fidelity_contexts=fidelity_kept_contexts(padded,current),screen_read_probe=build_env_screen_read_probe(padded),
                initial_provider_adjudication_count=old_usage)
        _persist_pending_source_review_observation(snapshot=snapshot,pending_scope=observation_scope,
            reviewed_srt=current,resumed_review=review,prior_count=old_usage,cid=cid,out_root=out_root)
        if pending_scope is not None and not _pending_review_consumed(pending_scope,review,review_input):
            raise CurrentTextContinuationError('SOURCE_REPLAY_PENDING_OBSERVATION_OMITTED')
        new_usage = review.get('context_adjudication_count',0)
        if type(new_usage) is not int or new_usage < 0:
            raise CurrentTextContinuationError('CURRENT_SOURCE_STAGE_PENDING_REVIEW_COUNT_INVALID')
        authority['cpa_adjudication_usage'] = {'provider_adjudication_count':old_usage+new_usage,
            'prior_provider_adjudication_count':old_usage,'soft_limit':MAX_CONTEXT_ADJUDICATIONS,'policy':'SOFT'}
        from src.autoslice.semantic_request_history import preserve_partial_usage
        authority["cpa_adjudication_usage"] = preserve_partial_usage(
            authority["cpa_adjudication_usage"], authority)
        review['cpa_adjudication_usage'] = copy.deepcopy(authority['cpa_adjudication_usage'])
        authority['final_review_audit'] = review
        if authority.get('source_truth_protection_transition'):
            authority['source_truth_protection_transition']['status'] = 'CURRENT_REVIEW_COMPLETED'
        if current != review_input:
            authority.setdefault('source_review_input_history',[]).append({
                'role':'PREVIOUS_POST_ENTITY_INPUT_BEFORE_CURRENT_TYPED_REVIEW',
                'srt':authority['post_transcript_entity_output_srt'],
                'sha256':authority['post_transcript_entity_output_srt_sha256'],
                'source_checkpoint_authority_sha256':snapshot['chat_authority_sha256'],
            })
            authority['post_transcript_entity_output_srt'] = current
            authority['post_transcript_entity_output_srt_sha256'] = hashlib.sha256(current.encode()).hexdigest()
    evidence = _finalize_text_evidence(spec=spec,durations=durations,srt_text=current,
        protected_name_source_srt=name_reference,chat_authority_audit=authority,
        transcript_entity_audit=authority['transcript_entity_audit'],referent_groups=context.referent_groups,
        final_review_audit=review,song_name_candidates=draft.song_name_candidates,
        known_songs_path=adapters.profile_asset_file('known_songs'),session_topic_authorities=draft.session_topic_authorities,
        source_language_witness_srt=witness,text_override_path=text_override_path,
        retained_reviewed_input=snapshot.get('validated_retained'),
        source_language_guard_reference=language_guard_reference,
        source_truth_ledger_path=ledger,out_root=out_root,cid=cid,padded=padded,clip_context=snapshot['clip_context'])
    write_source_text_checkpoint(out_root/f'{cid}.source-text-checkpoint.json',candidate_id=cid,
        srt_path=out_root/'padded.fresh.srt',authority_path=evidence.chat_authority_path,
        context_path=snapshot['clip_context_path'],padded=padded)
    return _automatic_current_checkpoint_snapshot(spec=spec,durations=durations,padded=padded,
        padded_dur=padded_dur,cid=cid,out_root=out_root,source_truth_ledger_path=ledger,
        validated_retained=snapshot.get('validated_retained'))


def _automatic_current_source_stage_checkpoint(
    *,
    spec: dict,
    durations: list[int],
    padded: Path,
    padded_dur: int,
    host: str,
    text_override_path: Path | None,
    cid: str,
    out_root: Path,
    substrate: str,
    correct: str,
    screen_text: bool,
    adapters: TextPipelineAdapters,
    merged: list[DanmakuItem],
    authoritative_chat: list[ChatEvidence],
    snapshot: Mapping[str, Any] | None = None,
    native_audio_state: PreparedNativeAudioState | None = None,
    witness_cache_roots: tuple[Path, ...] = (),
) -> TextPipelineResult | None:
    """Resume a genuine normal source-text checkpoint in memory.

    A v1 checkpoint is the output of the ordinary text producer.  Return the
    same ``TextPipelineResult`` shape that a fresh run returns, while leaving
    boundary resolution and exact-final release to their existing callers.
    Completed v2 delivery checkpoints intentionally return ``None`` here so
    the established continuation path remains the only consumer for them.
    """

    if snapshot is None:
        snapshot = _automatic_current_checkpoint_snapshot(
            spec=spec,
            durations=durations,
            padded=padded,
            padded_dur=padded_dur,
            cid=cid,
            out_root=out_root,
            source_truth_ledger_path=(None if str(spec.get('human_truth_mode') or 'delivery') == 'withheld'
                else adapters.profile_asset_file('subtitle_truth_ledger')),
        )
    if snapshot is None:
        return None
    authority = snapshot["chat_authority"]
    partial = bool(snapshot.get('partial_source_text'))
    if not partial and _source_language_count_recheck_pending(
        authority, spec=spec, durations=durations, padded=padded, padded_dur=padded_dur,
        srt_text=snapshot["srt_text"], clip_context=snapshot["clip_context"],
    ):
        partial = True
    final_review = authority.get("final_review_audit")
    if not isinstance(final_review, Mapping) or final_review.get("schema_version") != "final-review-audit.v1":
        if (snapshot.get("native_checkpoint") or {}).get("time_domain") == "padded_local":
            raise CurrentTextContinuationError("CURRENT_SOURCE_CHECKPOINT_STAGE_DRIFT")
        return None
    source_truth_ledger_path = (
        None
        if str(spec.get("human_truth_mode") or "delivery") == "withheld"
        else adapters.profile_asset_file("subtitle_truth_ledger")
    )
    from src.autoslice.producer_source_truth_checkpoint import added_local_term_review_required
    added_terms = added_local_term_review_required(authority,spec=spec,durations=durations,
        ledger_path=source_truth_ledger_path,current_text=snapshot['srt_text'])
    if snapshot.get('source_observation_parent') is not None and not added_terms:
        raise CurrentTextContinuationError('SOURCE_REPLAY_OBSERVATIONS_TRUTH_SCOPE_DRIFT')
    pending_review = final_review.get('status') == 'AUDITOR_UNAVAILABLE' or added_terms
    language_reference = snapshot.get('verified_language_reference')
    source_language = authority.get('final_source_language_preservation_audit') or {}
    if language_reference is None and (source_language.get('status') == 'SKIPPED_CUE_COUNT_MISMATCH'
                                      or source_language.get('typed_language_reference') is not None):
        language_reference = _source_stage_original_language_witness(authority, spec, padded, padded_dur)
    validated_final_review = _validate_current_source_stage_authority(
        authority,
        spec=spec,
        durations=durations,
        srt_text=snapshot["srt_text"],
        srt_sha256=snapshot["srt_sha256"],
        clip_context=snapshot["clip_context"],
        source_truth_ledger_path=None if added_terms else source_truth_ledger_path,
        authoritative_chat=authoritative_chat,
        independent_support_srts=_load_independent_chat_support_srts(padded),
        allow_pending_review=True,
        verified_language_reference=language_reference,
        pending_finalization=partial,
        pending_review=pending_review,
        completed_retained_input=snapshot.get("completed_retained_input"),
    )
    if partial or pending_review:
        completed = _resume_pending_source_finalization(spec=spec,durations=durations,padded=padded,
            padded_dur=padded_dur,host=host,text_override_path=text_override_path,cid=cid,out_root=out_root,
            substrate=substrate,correct=correct,screen_text=screen_text,adapters=adapters,merged=merged,
            authoritative_chat=authoritative_chat,snapshot=snapshot,validated_review=validated_final_review,
            pending_review=pending_review,native_audio_state=native_audio_state,
            witness_cache_roots=witness_cache_roots)
        return _automatic_current_source_stage_checkpoint(spec=spec,durations=durations,padded=padded,
            padded_dur=padded_dur,host=host,text_override_path=text_override_path,cid=cid,out_root=out_root,
            substrate=substrate,correct=correct,screen_text=screen_text,adapters=adapters,merged=merged,
            authoritative_chat=authoritative_chat,snapshot=completed,
            native_audio_state=native_audio_state,witness_cache_roots=witness_cache_roots)
    from src.autoslice.producer_text_checkpoint import write_source_text_checkpoint

    source_checkpoint_path = out_root / f"{cid}.source-text-checkpoint.json"
    write_source_text_checkpoint(
        source_checkpoint_path, candidate_id=cid, srt_path=snapshot["current_srt_path"],
        authority_path=snapshot["chat_authority_path"], context_path=snapshot["clip_context_path"], padded=padded,
    )
    authority = copy.deepcopy(dict(authority))
    language_replay = _replay_typed_source_language_guard(
        authority, original_reference=language_reference,
        srt_text=snapshot['srt_text'], spec=spec, durations=durations,
        validated_effects=_source_language_effect_rows(authority),
    )
    language_refreshed = False
    if language_replay is not None and language_replay != authority['final_source_language_preservation_audit']:
        authority.setdefault('source_language_review_history', []).append(
            copy.deepcopy(authority['final_source_language_preservation_audit']))
        authority['final_source_language_preservation_audit'] = language_replay
        language_refreshed = True
    usage_parent = snapshot.get("source_usage_parent")
    _carry_source_usage_parent(authority,snapshot,out_root=out_root)
    human_source = authority.get('human_sequence_alignment') or {}
    from src.autoslice.human_sequence_alignment import protected_indexes as human_protected_indexes
    human_source_indexes = human_protected_indexes(human_source,snapshot['srt_text'])
    if human_source.get('records'):
        from src.autoslice.human_sequence_alignment import validate_records, verify_surfaces
        validate_records(human_source,ledger_path=source_truth_ledger_path,spec=spec,durations=durations)
        if not verify_surfaces(human_source,text=snapshot['srt_text'],speaker=snapshot['srt_text'],
                               delivery_start_ms=0,delivery_end_ms=padded_dur):
            raise CurrentTextContinuationError('HUMAN_SEQUENCE_SOURCE_LITERAL_DRIFT')
    # Resume keeps the persisted adjudication and only refreshes its raw guard
    # (records without a raw guard keep the plain recompute).
    authority["foreign_script_consistency_audit"] = _replay_source_stage_foreign_guard(
        authority, srt_text=snapshot["srt_text"], spec=spec, durations=durations,
    )
    validated_final_review = dict(validated_final_review)
    if usage_parent is not None:
        validated_final_review["cpa_adjudication_usage"] = copy.deepcopy(authority["cpa_adjudication_usage"])
    pending_scope = validated_final_review.pop("pending_review_scope", None)
    authority["final_review_audit"] = validated_final_review

    # Keep the normal lazy transcriber/VAD contract.  The retained bytes are
    # the source-stage text; no fresh ASR is requested merely to reconstruct
    # the in-memory producer result.
    draft = _transcribe_draft(
        spec=spec,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        substrate=substrate,
        correct=correct,
        screen_text=screen_text,
        merged=merged,
        adapters=adapters,
        retained_srt_text=snapshot["srt_text"],
    )
    entity_context = _build_entity_verification_context(
        spec=spec,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        text_override_path=text_override_path,
        cid=cid,
        out_root=out_root,
        srt_text=snapshot["srt_text"],
        authoritative_chat=authoritative_chat,
        adapters=adapters,
        native_audio_state=native_audio_state,
        witness_cache_roots=witness_cache_roots,
    )
    current_srt_text = snapshot["srt_text"]
    current_cues = snapshot["cues"]
    current_authority_path = out_root / f"{cid}.chat-authority.json"
    current_checkpoint_srt_path = snapshot["current_srt_path"]
    if language_refreshed:
        _persist_review_document(current_authority_path, authority)
        write_source_text_checkpoint(
            source_checkpoint_path, candidate_id=cid, srt_path=current_checkpoint_srt_path,
            authority_path=current_authority_path, context_path=snapshot['clip_context_path'], padded=padded,
        )
    if isinstance(pending_scope, Mapping):
        previous_final_review = copy.deepcopy(validated_final_review)
        handled_entity_cues = {
            int(index)
            for key in (
                "applied",
                "pending_text_overrides",
                "entity_repairs",
                "coreference_repairs",
            )
            for row in authority.get(key) or []
            for index in (
                row.get("cue_indexes")
                or ([row.get("cue_index")] if row.get("cue_index") is not None else [])
            )
            if type(index) is int
        }
        protected_indexes: list[int] = []
        for container in (
            previous_final_review,
            authority.get("source_truth_preview_receipts"),
        ):
            if not isinstance(container, Mapping):
                continue
            values = container.get("source_truth_protected_cue_indexes")
            if values is None:
                for receipt in container.values():
                    if isinstance(receipt, Mapping):
                        values = receipt.get("protected_cue_indexes")
                        if values:
                            break
            if isinstance(values, list):
                protected_indexes.extend(
                    value for value in values if type(value) is int
                )
        previous_usage = authority.get("cpa_adjudication_usage")
        if isinstance(previous_usage, Mapping):
            prior_count = previous_usage.get("provider_adjudication_count")
        else:
            prior_count = previous_final_review.get("context_adjudication_count", 0)
        if type(prior_count) is not int or prior_count < 0:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_CPA_ADJUDICATION_USAGE_INVALID"
            )
        saved_review = _load_pending_source_review_observation(
            spec=spec, snapshot=snapshot, pending_scope=pending_scope,
            prior_count=prior_count, cid=cid,
        )
        if saved_review is not None:
            reviewed_srt, resumed_review = saved_review
        else:
            from src.autoslice.continuation_subchecks import end_cold_read
            end_cold_read()  # a provider-backed review is not part of the cold read
            reviewed_srt, resumed_review = _run_final_review(
                srt_text=current_srt_text,
                source_media_sha256=(spec.get("source_piece_clock") or {}).get("padded_sha256"),
                chat_authority_audit=authority,
                handled_entity_cues=handled_entity_cues,
                verify_confusable_entity=entity_context.verify_confusable_entity,
                authoritative_chat=authoritative_chat,
                adapters=adapters,
                selection_hook=str(spec.get("selection_hook") or ""),
                referent_groups=entity_context.referent_groups,
                clip_context=snapshot["clip_context"],
                source_truth_protected_cue_indexes=tuple(sorted(set(protected_indexes) | human_source_indexes)),
                carryover_file=carryover_path(out_root, cid),
                priority_raw_findings=tuple(pending_scope["rows"]),
                draft_fidelity_contexts=fidelity_kept_contexts(padded, current_srt_text),
                screen_read_probe=build_env_screen_read_probe(padded),
                initial_provider_adjudication_count=prior_count,
            )
        _persist_pending_source_review_observation(
            snapshot=snapshot,
            pending_scope=pending_scope,
            reviewed_srt=reviewed_srt,
            resumed_review=resumed_review,
            prior_count=prior_count,
            cid=cid,
            out_root=out_root,
        )
        if not isinstance(resumed_review, Mapping) or resumed_review.get("status") in {
            "SKIPPED",
            "SKIPPED_TEST_ONLY",
            "AUDITOR_UNAVAILABLE",
        }:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_UNAVAILABLE"
            )
        if not _pending_review_consumed(pending_scope, resumed_review, current_srt_text):
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_OMITTED"
            )
        _append_source_stage_review_history(
            authority,
            previous_final_review,
            srt_sha256=snapshot["srt_sha256"],
        )
        resumed_review = copy.deepcopy(dict(resumed_review))
        current_adjudication_count = resumed_review.get("context_adjudication_count", 0)
        if type(current_adjudication_count) is not int or current_adjudication_count < 0:
            raise CurrentTextContinuationError(
                "CURRENT_SOURCE_STAGE_PENDING_REVIEW_COUNT_INVALID"
            )
        authority["cpa_adjudication_usage"] = {
            "provider_adjudication_count": prior_count + current_adjudication_count,
            "prior_provider_adjudication_count": prior_count,
            "soft_limit": MAX_CONTEXT_ADJUDICATIONS,
            "policy": "SOFT",
        }
        from src.autoslice.semantic_request_history import preserve_partial_usage
        authority["cpa_adjudication_usage"] = preserve_partial_usage(
            authority["cpa_adjudication_usage"], authority)
        resumed_review["cpa_adjudication_usage"] = copy.deepcopy(
            authority["cpa_adjudication_usage"]
        )
        authority["final_review_audit"] = resumed_review
        current_srt_sha256 = hashlib.sha256(reviewed_srt.encode("utf-8")).hexdigest()
        if reviewed_srt != current_srt_text:
            source_language_witness_srt = _source_stage_original_language_witness(
                authority, spec, padded, padded_dur
            )
            if not source_language_witness_srt:
                raise CurrentTextContinuationError(
                    "CURRENT_SOURCE_STAGE_SOURCE_LANGUAGE_WITNESS_MISSING"
                )
            from src.autoslice.continuation_subchecks import end_cold_read
            end_cold_read()  # finalization stages are provider-backed
            evidence = _finalize_text_evidence(
                spec=spec,
                durations=durations,
                srt_text=reviewed_srt,
                protected_name_source_srt=current_srt_text,
                chat_authority_audit=authority,
                transcript_entity_audit=authority["transcript_entity_audit"],
                referent_groups=entity_context.referent_groups,
                final_review_audit=resumed_review,
                song_name_candidates=draft.song_name_candidates,
                known_songs_path=adapters.profile_asset_file("known_songs"),
                session_topic_authorities=draft.session_topic_authorities,
                source_language_witness_srt=source_language_witness_srt,
                source_language_guard_reference=(_typed_source_language_reference(
                    source_language_witness_srt, snapshot['srt_text'], _source_language_effect_rows(authority))
                    if _needs_typed_language_reference(authority) else None),
                text_override_path=text_override_path,
                source_truth_ledger_path=source_truth_ledger_path,
                out_root=out_root,
                cid=cid,
                padded=padded,
                clip_context=snapshot["clip_context"],
            )
            current_srt_text = evidence.srt_text
            current_cues = evidence.cues
            current_authority_path = evidence.chat_authority_path
            current_checkpoint_srt_path = out_root / "padded.fresh.srt"
        else:
            current_srt_text = reviewed_srt
            current_cues = [cue for cue in parse_srt_cues(reviewed_srt) if cue.text.strip()]
            authority["final_output_srt_sha256"] = current_srt_sha256
            authority["post_transcript_entity_output_srt"] = reviewed_srt
            authority["post_transcript_entity_output_srt_sha256"] = current_srt_sha256
            _persist_review_document(current_authority_path, authority)
    required_boundary_owners = ledger_required_owner_contracts(
        spec=spec,
        durations=durations,
        ledger_path=source_truth_ledger_path,
    )
    required_boundary_owners.extend(_redelivery_baseline_boundary_owner(spec, durations))
    # The fresh producer freezes this input before boundary resolution.  Set
    # the same normal owner contract here; the historical v1 audit remains
    # evidence and is never treated as a final boundary verdict.
    from src.autoslice.human_sequence_alignment import required_owners as human_sequence_owners
    required_boundary_owners.extend(human_sequence_owners(human_source))
    spec["required_boundary_owners"] = required_boundary_owners
    boundary_target_ms, boundary_search_scope = freeze_required_boundary_owner_contract(
        spec=spec, durations=durations, chat_authority_audit=authority,
        required_boundary_owners=required_boundary_owners,
    )
    if isinstance(pending_scope, Mapping) and authority["final_review_audit"].get("infra_unresolved"):
        infra_scope = _source_context_infra_scope(
            authority["final_review_audit"], current_srt_text, boundary_search_scope,
        )
        authority["final_review_audit"]["source_context_infra_scope"] = infra_scope
        if infra_scope["blocking"]:
            raise SystemExit(
                "FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED: "
                f"cues {[row['cue_index'] for row in infra_scope['blocking']]} "
                "remain within the possible delivery window; refusing known-suspect text"
            )
    if (
        isinstance(pending_scope, Mapping)
        and authority["final_review_audit"].get("status") in {"APPLIED", "PARTIAL", "SKIPPED"}
    ):
        # The saved observation remains bound to the prior immutable input.
        # Do not replace that input checkpoint with a guard-rejected successor
        # or a FLAGGED status the source-stage reader cannot accept, before
        # the ordinary restart can consume its completed review.
        write_source_text_checkpoint(
            source_checkpoint_path, candidate_id=cid, srt_path=current_checkpoint_srt_path,
            authority_path=current_authority_path, context_path=snapshot["clip_context_path"], padded=padded,
        )
    frozen_boundary_receipt, boundary_review, replay, exact_replay = review_source_boundary(
        spec=spec, candidate_id=cid, cues=current_cues,
        boundary_target_ms=boundary_target_ms, boundary_search_scope=boundary_search_scope,
        current_owner_contract=authority.get("frozen_boundary_owner_contract"),
        authoritative_chat=authoritative_chat, clip_context=snapshot["clip_context"],
        available_local_source_context_end_ms=sum(durations),
    )
    validated_final_review = authority["final_review_audit"]
    validated_final_review["boundary_semantic_review"] = boundary_review
    if replay:
        validated_final_review["boundary_receipt_replay"] = {"source_full_window": replay}
    if exact_replay is not None:
        validated_final_review["reviewed_exact_source_interval_replay"] = exact_replay
    persist_review_audit(out_root / f"{cid}.review-flags.json", validated_final_review)
    if isinstance(pending_scope, Mapping):
        _persist_review_document(current_authority_path, authority)
    truth_ownership = resolve_truth_full_ownership(spec) or resolve_operator_text_full_ownership(
        spec
    )
    review_exact_final_srt = _build_exact_final_review_callback(
        spec=spec,
        out_root=out_root,
        cid=cid,
        padded=padded,
        adapters=adapters,
        authoritative_chat=authoritative_chat,
        clip_context=snapshot["clip_context"],
        final_review_audit=validated_final_review,
        verify_confusable_entity=entity_context.verify_confusable_entity,
        truth_ownership=truth_ownership,
        screen_read_probe=build_env_screen_read_probe(padded),
        frozen_boundary_receipt=frozen_boundary_receipt,
    )
    return TextPipelineResult(
        srt_text=current_srt_text,
        cues=current_cues,
        spans=draft.spans,
        transcriber=draft.transcriber,
        chat_authority_audit=authority,
        chat_authority_path=current_authority_path,
        clip_context=snapshot["clip_context"],
        clip_context_path=snapshot["clip_context_path"],
        review_exact_final_srt=review_exact_final_srt,
        boundary_context_cues=None,
    )


def _automatic_current_text_checkpoint(
    *,
    spec: dict,
    durations: list[int],
    padded: Path,
    padded_dur: int,
    host: str,
    cid: str,
    out_root: Path,
    substrate: str,
    correct: str,
    screen_text: bool,
    adapters: TextPipelineAdapters,
    merged: list[DanmakuItem],
    authoritative_chat: list[ChatEvidence],
    snapshot: Mapping[str, Any] | None = None,
    native_audio_state: PreparedNativeAudioState | None = None,
    witness_cache_roots: tuple[Path, ...] = (),
) -> CurrentTextContinuation | None:
    """Build the ordinary current checkpoint continuation when it is complete.

    ``padded.fresh.srt`` is the producer's source-domain text surface.  A
    partially written trio is a known failed checkpoint and must stop with an
    explicit reason; only the case where none of the trio exists falls through
    to the historical fresh transcription path.  Validation happens before
    ``_transcribe_draft`` so a drifted checkpoint cannot trigger VAD or a fresh
    provider-backed transcriber.
    """

    if snapshot is None:
        snapshot = _automatic_current_checkpoint_snapshot(
            spec=spec,
            durations=durations,
            padded=padded,
            padded_dur=padded_dur,
            cid=cid,
            out_root=out_root,
        )
    if snapshot is None:
        return None
    current_srt_path = snapshot["current_srt_path"]
    chat_authority_path = snapshot["chat_authority_path"]
    clip_context_path = snapshot["clip_context_path"]
    srt_sha256 = snapshot["srt_sha256"]
    srt_text = snapshot["srt_text"]
    cues = snapshot["cues"]
    padded_sha256 = snapshot["padded_sha256"]
    source_media_sha256s = snapshot["source_media_sha256s"]
    chat_authority = snapshot["chat_authority"]
    chat_authority_sha256 = snapshot["chat_authority_sha256"]
    clip_context = snapshot["clip_context"]
    clip_context_sha256 = snapshot["clip_context_sha256"]

    # A later delivery-grid surface may be complete and hash-bound, but it is
    # not the narrow automatic ingress contract.  The current source grid must
    # be the same bytes named by both normal source-domain authority pointers.
    native = snapshot.get("native_checkpoint")
    clock_kwargs: dict[str, object] = {}
    if isinstance(native, Mapping):
        clock_kwargs = _current_checkpoint_clock_bindings(native)
    elif (
        _continuation_normalize_sha(chat_authority.get("final_output_srt_sha256")) != srt_sha256
        or _continuation_normalize_sha(
            chat_authority.get("post_transcript_entity_output_srt_sha256")
        )
        != srt_sha256
    ):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_DELIVERY_GRID_UNSUPPORTED")
    final_review = chat_authority.get("final_review_audit")
    boundary_review = (
        final_review.get("boundary_semantic_review")
        if isinstance(final_review, Mapping)
        else None
    )
    if native is None and (not isinstance(boundary_review, Mapping) or boundary_review.get("review_scope") != "source_full_window"):
        raise CurrentTextContinuationError("CURRENT_AUTO_RESUME_SOURCE_BOUNDARY_SCOPE_UNSUPPORTED")

    # Reuse the existing continuation validators as a read-only preflight.
    # They remain the sole authority for pending/typed owner correctness; this
    # ingress does not synthesize or promote any receipt.
    clock_review, clock_reference = _continuation_source_clock_reference(
        _current_checkpoint_clock_namespace(clock_kwargs),
        chat_authority=chat_authority, srt_text=srt_text, srt_sha256=srt_sha256,
        clip_context=clip_context, candidate_id=cid,
    ) if native is not None else (None, None)
    validated_review = _continuation_validate_existing_authority(
        chat_authority,
        srt_text=srt_text,
        srt_sha256=srt_sha256,
        clip_context=clip_context,
        candidate_id=cid,
        source_clock_review=clock_review,
        source_clock_srt_sha256=(clock_reference.get("reviewed_srt_sha256") if clock_reference else None),
        allow_source_scope_input=True,
    )
    reference_cues = None
    if clock_kwargs.get("source_clock_srt_path") is not None:
        reference_cues = parse_srt_cues(clock_kwargs["source_clock_srt_path"].read_text(encoding="utf-8"))
    _continuation_project_delivery_to_source(
        cues,
        final_review_audit=clock_review or validated_review,
        reference_cues=reference_cues,
        authority=chat_authority,
    )
    _continuation_source_boundary_cues(
        chat_authority=chat_authority,
        clip_context=clip_context,
        final_review_audit=clock_review or validated_review,
        padded_dur=padded_dur,
        source_boundary_srt_path=clock_kwargs.get("source_boundary_srt_path"),
        expected_source_boundary_srt_sha256=clock_kwargs.get("expected_source_boundary_srt_sha256"),
        source_context_binding=_continuation_source_context_draft_binding(
            _current_checkpoint_clock_namespace(clock_kwargs), spec=spec,
            candidate_id=cid, padded_dur=padded_dur,
            source_media_sha256s=source_media_sha256s,
        ),
    )

    # This invokes only normal deterministic VAD and returns the normal lazy
    # transcriber.  With retained_srt_text it never calls that transcriber.
    draft = _transcribe_draft(
        spec=spec,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        substrate=substrate,
        correct=correct,
        screen_text=screen_text,
        merged=merged,
        adapters=adapters,
        retained_srt_text=srt_text,
    )
    return CurrentTextContinuation(
        current_srt_path=current_srt_path,
        chat_authority_path=chat_authority_path,
        clip_context_path=clip_context_path,
        padded_sha256="sha256:" + padded_sha256,
        source_media_sha256s=source_media_sha256s,
        spans=draft.spans,
        transcriber=draft.transcriber,
        authoritative_chat=authoritative_chat,
        native_audio_state=native_audio_state,
        witness_cache_roots=witness_cache_roots,
        expected_chat_authority_sha256="sha256:" + chat_authority_sha256,
        expected_clip_context_sha256="sha256:" + clip_context_sha256,
        **clock_kwargs,
    )


@cold_read_session_scope  # one producer text stage = one cold read of persisted authority
def run_text_pipeline(
    *,
    spec: dict,
    durations: list[int],
    padded: Path,
    padded_dur: int,
    host: str,
    text_override_path: Path | None,
    cid: str,
    out_root: Path,
    substrate: str,
    correct: str,
    screen_text: bool,
    adapters: TextPipelineAdapters,
    continuation: CurrentTextContinuation | None = None,
) -> TextPipelineResult:
    from src.autoslice.producer_package_finalization import preserve_postburn_observation
    from src.autoslice.producer_source_truth_checkpoint import FrozenTruthAssets
    adapters = FrozenTruthAssets(adapters, out_root,
        withheld=str(spec.get('human_truth_mode') or 'delivery') == 'withheld')
    preserve_postburn_observation(out_root, cid)
    from src.autoslice.retained_source_input import load_retained_source_input
    retained_input = load_retained_source_input(spec=spec, cid=cid, padded=padded, out_root=out_root)
    if retained_input is not None:
        if substrate != "existing_srt":
            raise ValueError("RETAINED_SOURCE_INPUT_EXISTING_SRT_REQUIRED")
        from src.autoslice.existing_subtitle_surface import _load_surface
        _load_surface(spec, padded=padded, padded_duration_ms=padded_dur)
    from src.autoslice.retained_preparation_checkpoint import (
        retained_attempt_present, load_completed_retained_review, consume_completed_retained_review,
        validate_completed_retained_context,
    )
    retained_attempt_before_load = retained_attempt_present(out_root, cid)
    completed_retained_review = None
    retained_reviewed_draft: dict[str, object] | None = None
    human_alignment = None
    if spec.get("retained_current_text") is not None:
        from src.autoslice.retained_reviewed_draft import load_retained_reviewed_draft

        retained_reviewed_draft = load_retained_reviewed_draft(
            spec=spec,
            durations=durations,
            padded=padded,
            padded_dur=padded_dur,
            out_root=out_root,
            cid=cid,
            validate_parent=_validate_retained_reviewed_parent,
        )
    native_audio_state = (
        retained_input.native if retained_input is not None
        else _retained_native_audio_state(retained_reviewed_draft)
    )
    witness_cache_roots = _retained_witness_cache_roots(retained_reviewed_draft)
    if continuation is not None:
        supplied_native = continuation.native_audio_state
        if native_audio_state is not None and supplied_native is not None:
            if (supplied_native.cache_root, supplied_native.budget_receipt) != (
                    native_audio_state.cache_root, native_audio_state.budget_receipt):
                raise NativeAudioAccountContinuationError("CURRENT_NATIVE_AUDIO_ACCOUNT_IDENTITY_DRIFT")
        if native_audio_state is None:
            native_audio_state = supplied_native
        witness_cache_roots = tuple(dict.fromkeys((*witness_cache_roots, *continuation.witness_cache_roots)))
    if native_audio_state is None:
        # Only the entry selects the ordinary fresh account. An active ledger
        # without its own receipt is not a fresh source and must not be rebased.
        if not (out_root / "native-audio-budget.json").is_file():
            _require_native_audio_account(padded, None)
        native_audio_state = PreparedNativeAudioState(out_root, out_root / "native-audio-budget.json")
    if continuation is not None:
        continuation = replace(continuation, native_audio_state=native_audio_state,
                               witness_cache_roots=witness_cache_roots)
        return _run_current_text_continuation(
            spec=spec,
            durations=durations,
            padded=padded,
            padded_dur=padded_dur,
            host=host,
            cid=cid,
            out_root=out_root,
            adapters=adapters,
            continuation=continuation,
        )
    if (retained_reviewed_draft is not None and retained_attempt_before_load
            and not any((out_root / f"{cid}.{suffix}.json").exists() for suffix in
                        ("current-text-checkpoint", "source-text-checkpoint", "partial-source-text-checkpoint"))):
        saved_context, _ = _continuation_read_json(
            out_root / f"{cid}.clip-context.json", label="RETAINED_PREPARATION_CONTEXT")
        completed_retained_review = load_completed_retained_review(
            retained=retained_reviewed_draft, out_root=out_root, cid=cid,
            clip_context=saved_context, required=True, spec=spec, durations=durations,
            padded=padded, padded_dur=padded_dur)
    merged, authoritative_chat = _collect_timeline_chat(spec, durations)
    source_stage_result = None
    automatic_continuation = None
    automatic_snapshot = None
    if retained_reviewed_draft is None:
        automatic_snapshot = _automatic_current_checkpoint_snapshot(
            spec=spec,
            durations=durations,
            padded=padded,
            padded_dur=padded_dur,
            cid=cid,
            out_root=out_root,
            source_truth_ledger_path=adapters.profile_asset_file('subtitle_truth_ledger'),
        )
        source_stage_result = _automatic_current_source_stage_checkpoint(
            spec=spec,
            durations=durations,
            padded=padded,
            padded_dur=padded_dur,
            host=host,
            text_override_path=text_override_path,
            cid=cid,
            out_root=out_root,
            substrate=substrate,
            correct=correct,
            screen_text=screen_text,
            adapters=adapters,
            merged=merged,
            authoritative_chat=authoritative_chat,
            snapshot=automatic_snapshot,
            native_audio_state=native_audio_state,
            witness_cache_roots=witness_cache_roots,
        )
    if source_stage_result is not None:
        return source_stage_result
    if retained_reviewed_draft is not None:
        current_manifest = out_root / f"{cid}.current-text-checkpoint.json"
        live_authority = out_root / f"{cid}.chat-authority.json"
        has_delivery = any(path.exists() or path.is_symlink() for path in (
            current_manifest, out_root / f"{cid}.source-text-checkpoint.json",
            out_root / f"{cid}.partial-source-text-checkpoint.json"))
        if not has_delivery and live_authority.exists():
            live, _ = _continuation_read_json(live_authority, label="CURRENT_CHAT_AUTHORITY", review=True)
            has_delivery = (live.get("final_review_audit") or {}).get("schema_version") == FINAL_REVIEW_SCHEMA_VERSION
        if has_delivery:
            automatic_snapshot = _automatic_current_checkpoint_snapshot(
                spec=spec, durations=durations, padded=padded, padded_dur=padded_dur,
                cid=cid, out_root=out_root, validated_retained=retained_reviewed_draft,
            )
            from src.autoslice.retained_reviewed_draft import validate_pending_entity_checkpoint
            validate_pending_entity_checkpoint(retained_reviewed_draft, automatic_snapshot)
        if str(spec.get("human_truth_mode") or "delivery") != "withheld":
            from src.autoslice.human_sequence_alignment import load_plans, apply_plans
            from src.autoslice.source_subtitle_truth import _render
            plans = load_plans(adapters.profile_asset_file("subtitle_truth_ledger"), spec=spec, durations=durations)
            source_text = str(retained_reviewed_draft.get("srt_text") or "")
            if plans and automatic_snapshot is not None:
                current = automatic_snapshot
                if (current.get('native_checkpoint') or {}).get('time_domain') == 'padded_local':
                    from src.autoslice.human_sequence_alignment import validate_records
                    validate_records(current['chat_authority'].get('human_sequence_alignment') or {},
                        ledger_path=adapters.profile_asset_file('subtitle_truth_ledger'),spec=spec,durations=durations)
                    current['verified_language_reference'] = _retained_language_reference(current,cid=cid)
                    _validate_current_source_stage_authority(current['chat_authority'],
                        spec=spec,durations=durations,srt_text=current['srt_text'],srt_sha256=current['srt_sha256'],
                        clip_context=current['clip_context'],source_truth_ledger_path=adapters.profile_asset_file('subtitle_truth_ledger'),
                        authoritative_chat=authoritative_chat,independent_support_srts=_load_independent_chat_support_srts(padded),
                        allow_pending_review=True,verified_language_reference=current['verified_language_reference'],
                        pending_finalization=bool(current.get('partial_source_text')),
                        completed_retained_input=current.get('completed_retained_input'))
                    source_text = current['srt_text']
                else:
                    clock_kwargs = (_current_checkpoint_clock_bindings(current['native_checkpoint'])
                        if isinstance(current.get('native_checkpoint'), Mapping) else {})
                    clock_review, clock_reference = _continuation_source_clock_reference(
                        _current_checkpoint_clock_namespace(clock_kwargs), chat_authority=current['chat_authority'],
                        srt_text=current['srt_text'], srt_sha256=current['srt_sha256'],
                        clip_context=current['clip_context'], candidate_id=cid)
                    current_review = _continuation_validate_existing_authority(
                        current["chat_authority"], srt_text=current["srt_text"], srt_sha256=current["srt_sha256"],
                        clip_context=current["clip_context"], candidate_id=cid,
                        source_clock_review=clock_review,
                        source_clock_srt_sha256=(clock_reference or {}).get('reviewed_srt_sha256'),
                        allow_source_scope_input=True,
                    )
                    projected, _ = _continuation_project_delivery_to_source(current["cues"], final_review_audit=clock_review or current_review,
                        authority=current["chat_authority"])
                    source_text = _render(projected, [cue.text for cue in projected])
            aligned, alignment = apply_plans(source_text, plans)
            if alignment["records"] and alignment["status"] == "APPLIED":
                # A new source generation retains the latest valid words,
                # changing only the independently aligned operator sequence.
                retained_reviewed_draft = copy.deepcopy(retained_reviewed_draft)
                retained_reviewed_draft["srt_text"] = aligned
                retained_reviewed_draft["projection"]["srt_text"] = aligned
                retained_reviewed_draft["projection"]["projected_srt_sha256"] = alignment["output_srt_sha256"]
                retained_reviewed_draft["proof"]["base_projected_srt_sha256"] = retained_reviewed_draft["proof"]["projected_srt_sha256"]
                retained_reviewed_draft["proof"]["projected_srt_sha256"] = alignment["output_srt_sha256"]
                retained_reviewed_draft["proof"]["human_sequence_alignment"] = alignment
                if automatic_snapshot is not None:
                    retained_reviewed_draft["parent_authority"] = copy.deepcopy(automatic_snapshot["chat_authority"])
                    live_path = out_root / f'{cid}.chat-authority.json'
                    if automatic_snapshot.get('source_successor') and live_path.is_file():
                        live, live_sha = _continuation_read_json(
                            live_path, label='SOURCE_SUCCESSOR_USAGE', review=True,
                        )
                        parent = retained_reviewed_draft['parent_authority']
                        if (live.get('post_transcript_entity_output_srt_sha256') != parent.get('post_transcript_entity_output_srt_sha256')
                                or (live.get('human_sequence_alignment') or {}).get('records')
                                != (parent.get('human_sequence_alignment') or {}).get('records')):
                            raise CurrentTextContinuationError('SOURCE_SUCCESSOR_USAGE_PARENT_DRIFT')
                        old_usage,new_usage = _retained_parent_cpa_usage_seed(parent),_retained_parent_cpa_usage_seed(live)
                        if new_usage['provider_adjudication_count'] > old_usage['provider_adjudication_count']:
                            parent['cpa_adjudication_usage'] = new_usage
                        retained_reviewed_draft['proof']['provider_usage_parent'] = {
                            'path':str(live_path),'sha256':'sha256:' + live_sha,'role':'CUMULATIVE_USAGE_ONLY'}
                    retained_reviewed_draft["proof"]["latest_current_parent"] = {
                        "authority_path":str(automatic_snapshot["chat_authority_path"]),
                        "authority_sha256":"sha256:" + automatic_snapshot["chat_authority_sha256"],
                        "srt_path":str(automatic_snapshot["current_srt_path"]),
                        "srt_sha256":"sha256:" + automatic_snapshot["srt_sha256"],
                    }
                human_alignment = alignment
                automatic_snapshot = None
        if (automatic_snapshot is not None
                and (automatic_snapshot.get('native_checkpoint') or {}).get('time_domain') == 'padded_local'):
            source_stage_result = _automatic_current_source_stage_checkpoint(
                spec=spec,durations=durations,padded=padded,padded_dur=padded_dur,host=host,
                text_override_path=text_override_path,cid=cid,out_root=out_root,substrate=substrate,
                correct=correct,screen_text=screen_text,adapters=adapters,merged=merged,
                authoritative_chat=authoritative_chat,snapshot=automatic_snapshot,
                native_audio_state=native_audio_state,witness_cache_roots=witness_cache_roots)
            if source_stage_result is not None:
                return source_stage_result
    if retained_reviewed_draft is None or automatic_snapshot is not None:
        automatic_continuation = _automatic_current_text_checkpoint(
            spec=spec,
            durations=durations,
            padded=padded,
            padded_dur=padded_dur,
            host=host,
            cid=cid,
            out_root=out_root,
            substrate=substrate,
            correct=correct,
            screen_text=screen_text,
            adapters=adapters,
            merged=merged,
            authoritative_chat=authoritative_chat,
            snapshot=automatic_snapshot,
            native_audio_state=native_audio_state,
            witness_cache_roots=witness_cache_roots,
        )
    if automatic_continuation is not None:
        return _run_current_text_continuation(
            spec=spec,
            durations=durations,
            padded=padded,
            padded_dur=padded_dur,
            host=host,
            cid=cid,
            out_root=out_root,
            adapters=adapters,
            continuation=automatic_continuation,
        )
    # No continuation: the fresh provider-backed stages are not a cold read.
    from src.autoslice.continuation_subchecks import end_cold_read
    end_cold_read()
    screen_read_probe = build_env_screen_read_probe(padded)
    draft = _transcribe_draft(
        spec=spec,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        substrate=substrate,
        correct=correct,
        screen_text=screen_text,
        merged=merged,
        adapters=adapters,
        retained_srt_text=(
            str(retained_reviewed_draft["srt_text"])
            if retained_reviewed_draft is not None
            else None
        ),
    )
    source_truth_ledger_path = (
        None
        if str(spec.get("human_truth_mode") or "delivery") == "withheld"
        else adapters.profile_asset_file("subtitle_truth_ledger")
    )
    _discarded_draft_truth_text, draft_source_truth_preview_audit = apply_source_subtitle_truth(
        draft.srt_text,
        spec=spec,
        durations=durations,
        ledger_path=source_truth_ledger_path,
    )
    draft_source_truth_preview = build_source_truth_preview_receipt(
        input_srt_text=draft.srt_text,
        source_truth_audit=draft_source_truth_preview_audit,
        stage="pre_entity_arbitration",
    )
    entity_context = _build_entity_verification_context(
        spec=spec,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        text_override_path=text_override_path,
        cid=cid,
        out_root=out_root,
        srt_text=draft.srt_text,
        authoritative_chat=authoritative_chat,
        adapters=adapters,
        native_audio_state=native_audio_state,
        witness_cache_roots=witness_cache_roots,
    )
    clip_context = build_clip_context(
        candidate_id=cid,
        spec=spec,
        draft_srt=draft.srt_text,
        authoritative_chat=authoritative_chat,
        topic_resolution=entity_context.topic_resolution_audit,
        session_topic_authorities=draft.session_topic_authorities,
        speech_memory_ledger_path=adapters.profile_asset_file("speech_memory_ledger"),
        source_truth_ledger_path=source_truth_ledger_path,
    )
    clip_context_path = out_root / f"{cid}.clip-context.json"
    if completed_retained_review is not None:
        validate_completed_retained_context(completed_retained_review, clip_context)
    write_clip_context(clip_context_path, clip_context)
    required_boundary_owners = ledger_required_owner_contracts(
        spec=spec,
        durations=durations,
        ledger_path=source_truth_ledger_path,
    )
    required_boundary_owners.extend(_redelivery_baseline_boundary_owner(spec, durations))
    from src.autoslice.human_sequence_alignment import required_owners as human_sequence_owners
    required_boundary_owners.extend(human_sequence_owners(human_alignment or {}))
    spec["required_boundary_owners"] = required_boundary_owners
    truth_ownership = resolve_truth_full_ownership(spec) or resolve_operator_text_full_ownership(
        spec
    )
    if completed_retained_review is not None:
        from src.autoslice.retained_preparation_checkpoint import completed_retained_input_authority
        restored_input = completed_retained_input_authority(
            completed_retained_review, input_srt_text=draft.srt_text, clip_context=clip_context)
        authority = EntityAuthorityResult(
            srt_text=draft.srt_text, chat_authority_audit=restored_input,
            transcript_entity_audit=restored_input["transcript_entity_audit"],
            handled_entity_cues=set())
    else:
        authority = _apply_entity_authority(
            srt_text=draft.srt_text,
            authoritative_chat=authoritative_chat,
            support_srts=draft.support_srts,
            referent_groups=entity_context.referent_groups,
            verify_confusable_entity=entity_context.verify_confusable_entity,
            code_switch_audit=draft.code_switch_audit,
            term_boundary_moves=draft.term_boundary_moves,
            session_topic_absorption_audits=draft.session_topic_absorption_audits,
            padded=padded,
            adapters=adapters,
            # Deterministic preview projects required truth onto the exact draft
            # cue grid.  Raw discovery windows are used only for unresolved
            # required failures; optional rows never become protection owners.
            source_truth_protected_cue_indexes=sorted(set(draft_source_truth_preview["protected_cue_indexes"]) | {
                i for i,cue in enumerate(parse_srt_cues(draft.srt_text),1)
                if any((cue.start_ms,cue.end_ms,cue.text) == (u["start_ms"],u["end_ms"],u["text"])
                       for p in (human_alignment or {}).get("records",[]) for u in p["units"])}),
            truth_full_ownership=truth_ownership,
            retained_reviewed_draft=retained_reviewed_draft is not None,
        )
    authority.chat_authority_audit["source_truth_preview_receipts"] = {
        "pre_entity_arbitration": draft_source_truth_preview,
    }
    authority.chat_authority_audit["structured_chat_binding_audit"] = (
        build_structured_chat_binding_audit(spec, authoritative_chat)
    )
    if retained_input is not None:
        authority.chat_authority_audit["retained_source_input"] = retained_input.proof
        authority.chat_authority_audit["cpa_adjudication_usage"] = retained_input.cpa_usage
    if retained_reviewed_draft is not None:
        # This is a projection/input witness for the new normal pass.  The
        # parent authority and its BLOCK history remain separate artifacts;
        # no prior review or release status is copied into this audit.
        authority.chat_authority_audit["retained_reviewed_draft"] = dict(
            retained_reviewed_draft["proof"]
        )
        if human_alignment is not None:
            authority.chat_authority_audit["human_sequence_alignment"] = human_alignment
        authority.chat_authority_audit["cpa_adjudication_usage"] = (
            _retained_parent_cpa_usage_seed(retained_reviewed_draft["parent_authority"])
        )
    if isinstance(authority.chat_authority_audit.get("cpa_adjudication_usage"), Mapping):
        from src.autoslice.semantic_request_history import preserve_partial_usage
        authority.chat_authority_audit["cpa_adjudication_usage"] = preserve_partial_usage(
            authority.chat_authority_audit["cpa_adjudication_usage"], authority.chat_authority_audit)
    _discarded_review_truth_text, review_source_truth_preview_audit = apply_source_subtitle_truth(
        authority.srt_text,
        spec=spec,
        durations=durations,
        ledger_path=source_truth_ledger_path,
    )
    review_source_truth_preview = build_source_truth_preview_receipt(
        input_srt_text=authority.srt_text,
        source_truth_audit=review_source_truth_preview_audit,
        stage="pre_correction_review",
    )
    authority.chat_authority_audit["source_truth_preview_receipts"]["pre_correction_review"] = (
        review_source_truth_preview
    )
    from src.autoslice.retained_reviewed_draft import (
        pending_entity_review_findings, require_pending_entity_review,
    )
    retained_entity_findings = pending_entity_review_findings(
        retained_reviewed_draft, srt_text=authority.srt_text,
    )
    pinned_replay_ownership = _pinned_replay_reviewed_text_ownership(spec)
    if completed_retained_review is not None:
        reviewed_srt, final_review_audit = consume_completed_retained_review(
            completed_retained_review, input_srt_text=authority.srt_text,
            authority=authority.chat_authority_audit, clip_context=clip_context)
    elif pinned_replay_ownership is not None and not retained_entity_findings:
        # 修复快路径（提速②）：审片员产出注定被 v2 精确重放覆盖。
        reviewed_srt = authority.srt_text
        final_review_audit = skipped_final_review_audit(
            "SKIPPED_PINNED_REPLAY", pinned_replay_ownership=pinned_replay_ownership
        )
    elif truth_ownership is not None and not retained_entity_findings:

        reviewed_srt = authority.srt_text
        final_review_audit = skipped_final_review_audit(
            "SKIPPED_TRUTH_FULL_OWNERSHIP", truth_full_ownership=truth_ownership
        )
    else:
        reviewed_srt, final_review_audit = _run_final_review(
            srt_text=authority.srt_text,
            source_media_sha256=(spec.get("source_piece_clock") or {}).get("padded_sha256"),
            chat_authority_audit=authority.chat_authority_audit,
            handled_entity_cues=authority.handled_entity_cues,
            verify_confusable_entity=entity_context.verify_confusable_entity,
            authoritative_chat=authoritative_chat,
            adapters=adapters,
            selection_hook=str(spec.get("selection_hook") or ""),
            referent_groups=entity_context.referent_groups,
            clip_context=clip_context,
            source_truth_protected_cue_indexes=(
                sorted(set(review_source_truth_preview["protected_cue_indexes"]) | {
                    i for i,cue in enumerate(parse_srt_cues(authority.srt_text),1)
                    if any((cue.start_ms,cue.end_ms,cue.text) == (u["start_ms"],u["end_ms"],u["text"])
                           for p in (human_alignment or {}).get("records",[]) for u in p["units"])})
            ),
            carryover_file=carryover_path(
                out_root,
                f"{cid}.retained-reviewed-draft"
                if retained_reviewed_draft is not None
                else cid,
            ),
            priority_raw_findings=[
                *retained_entity_findings,
                *_review_priority_candidates(padded, authority.srt_text),
            ],
            draft_fidelity_contexts=fidelity_kept_contexts(padded, authority.srt_text),
            screen_read_probe=screen_read_probe,
            initial_provider_adjudication_count=(
                (authority.chat_authority_audit.get("cpa_adjudication_usage") or {}).get("provider_adjudication_count")
            ),
        )
    # A completed normal correction decision is required for each recovered
    # question. Empty discovery, disabled review and disclosure alone cannot
    # silently settle the old obligation or reach a source checkpoint.
    require_pending_entity_review(
        retained_reviewed_draft, input_srt_text=authority.srt_text,
        output_srt_text=reviewed_srt, findings=retained_entity_findings,
        review=final_review_audit, out_root=out_root, cid=cid,
        authority=authority.chat_authority_audit,
    )
    if isinstance(final_review_audit.get("cpa_adjudication_usage"), Mapping):
        authority.chat_authority_audit["cpa_adjudication_usage"] = copy.deepcopy(final_review_audit["cpa_adjudication_usage"])
    evidence = _finalize_text_evidence(
        spec=spec,
        durations=durations,
        srt_text=reviewed_srt,
        protected_name_source_srt=authority.srt_text,
        chat_authority_audit=authority.chat_authority_audit,
        transcript_entity_audit=authority.transcript_entity_audit,
        referent_groups=entity_context.referent_groups,
        final_review_audit=final_review_audit,
        song_name_candidates=draft.song_name_candidates,
        known_songs_path=adapters.profile_asset_file("known_songs"),
        session_topic_authorities=draft.session_topic_authorities,
        source_language_witness_srt=draft.source_language_witness_srt,
        retained_reviewed_input=retained_reviewed_draft,
        text_override_path=text_override_path,
        source_truth_ledger_path=source_truth_ledger_path,
        out_root=out_root,
        cid=cid,
        padded=padded,
        clip_context=clip_context,
    )
    from src.autoslice.producer_text_checkpoint import write_source_text_checkpoint

    write_source_text_checkpoint(
        out_root / f"{cid}.source-text-checkpoint.json", candidate_id=cid,
        srt_path=out_root / "padded.fresh.srt", authority_path=evidence.chat_authority_path,
        context_path=clip_context_path, padded=padded,
    )
    boundary_target_ms, boundary_search_scope = freeze_required_boundary_owner_contract(
        spec=spec,
        durations=durations,
        chat_authority_audit=authority.chat_authority_audit,
        required_boundary_owners=required_boundary_owners,
    )
    if final_review_audit.get("infra_unresolved"):
        infra_scope = _source_context_infra_scope(
            final_review_audit, evidence.srt_text, boundary_search_scope,
        )
        final_review_audit["source_context_infra_scope"] = infra_scope
        if infra_scope["blocking"]:
            raise SystemExit(
                "FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED: "
                f"cues {[row['cue_index'] for row in infra_scope['blocking']]} "
                "remain within the possible delivery window; refusing known-suspect text"
            )
    boundary_context_cues = None
    if retained_reviewed_draft is not None:
        from src.autoslice.retained_reviewed_draft import build_retained_source_boundary_context

        boundary_context_cues, source_context_projection = build_retained_source_boundary_context(
            retained_reviewed_draft, evidence.srt_text,
        )
        final_review_audit["current_delivery_source_projection"] = source_context_projection[
            "current_delivery_source_projection"
        ]
        source_context_text = _render_cues_to_srt(boundary_context_cues)
        source_context_path = out_root / f"{cid}.source-boundary-context.srt"
        source_context_path.write_text(source_context_text, encoding="utf-8")
        final_review_audit["source_boundary_context"] = {
            "schema_version": "source-boundary-transcript-context.v1",
            "role": "SOURCE_DOMAIN_BOUNDARY_REVIEW_CONTEXT",
            "source_srt_path": str(source_context_path),
            "source_srt_sha256": "sha256:" + hashlib.sha256(source_context_text.encode()).hexdigest(),
            "source_cue_grid_sha256": cue_grid_sha256(boundary_context_cues),
            "fresh_source_review_required": True,
            "retained_source_projection": source_context_projection,
        }
    (
        frozen_boundary_receipt,
        final_review_audit["boundary_semantic_review"],
        source_boundary_replay,
        exact_interval_replay,
    ) = review_source_boundary(
        spec=spec,
        candidate_id=cid,
        cues=boundary_context_cues if boundary_context_cues is not None else evidence.cues,
        boundary_target_ms=boundary_target_ms,
        boundary_search_scope=boundary_search_scope,
        current_owner_contract=authority.chat_authority_audit.get("frozen_boundary_owner_contract"),
        authoritative_chat=authoritative_chat,
        clip_context=clip_context,
        available_local_source_context_end_ms=sum(durations),
    )
    if exact_interval_replay is not None:
        final_review_audit["reviewed_exact_source_interval_replay"] = exact_interval_replay
    if source_boundary_replay:


        final_review_audit["boundary_receipt_replay"] = {
            "source_full_window": source_boundary_replay,
        }
    persist_review_audit(out_root / f"{cid}.review-flags.json", final_review_audit)
    _persist_review_document(evidence.chat_authority_path, authority.chat_authority_audit)

    if retained_reviewed_draft is not None:
        # Preserve the actual full source witness and its wider transcript
        # separately from the completed source-stage display, before exact
        # delivery review replaces the working audit.
        write_source_text_checkpoint(
            out_root / f"{cid}.source-text-checkpoint.json", candidate_id=cid,
            srt_path=out_root / "padded.fresh.srt", authority_path=evidence.chat_authority_path,
            context_path=clip_context_path, padded=padded,
        )

    review_exact_final_srt = _build_exact_final_review_callback(
        spec=spec,
        out_root=out_root,
        cid=cid,
        padded=padded,
        adapters=adapters,
        authoritative_chat=authoritative_chat,
        clip_context=clip_context,
        final_review_audit=final_review_audit,
        verify_confusable_entity=entity_context.verify_confusable_entity,
        truth_ownership=truth_ownership,
        screen_read_probe=screen_read_probe,
        frozen_boundary_receipt=frozen_boundary_receipt,
    )

    return TextPipelineResult(
        srt_text=evidence.srt_text,
        cues=evidence.cues,
        spans=draft.spans,
        transcriber=draft.transcriber,
        chat_authority_audit=authority.chat_authority_audit,
        chat_authority_path=evidence.chat_authority_path,
        clip_context=clip_context,
        clip_context_path=clip_context_path,
        review_exact_final_srt=review_exact_final_srt,
        boundary_context_cues=boundary_context_cues,
    )
