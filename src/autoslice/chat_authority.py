"""Compatibility facade for structured chat evidence and finalization.

Implementation lives in the single-direction dependency chain
``chat_evidence -> chat_repair -> chat_proposals``.
"""

from __future__ import annotations

from src.autoslice.jingting_chunker import parse_srt_cues as parse_srt_cues
from src.autoslice.chat_evidence import (
    EntityVerifier as EntityVerifier,
    canonicalize_hard_surfaces as canonicalize_hard_surfaces,
    canonicalize_expected_value_surfaces as canonicalize_expected_value_surfaces,
    canonicalize_hard_meme_surfaces as canonicalize_hard_meme_surfaces,
    ChatEvidence as ChatEvidence,
    ReferentEntity as ReferentEntity,
    ReferentGroup as ReferentGroup,
    normalize_chat_text as normalize_chat_text,
    normalize_srt_payload_text as normalize_srt_payload_text,
    normalize_srt_payload_window as normalize_srt_payload_window,
    normalize_code_switch_surfaces as normalize_code_switch_surfaces,
    normalize_expected_value_surfaces as normalize_expected_value_surfaces,
    normalize_hard_meme_surfaces as normalize_hard_meme_surfaces,
    sanitize_chat_display_text as sanitize_chat_display_text,
    load_referent_groups as load_referent_groups,
    _coerce_referent_groups as _coerce_referent_groups,
    load_clip_opening_address_config as load_clip_opening_address_config,
    clip_opening_address_group as clip_opening_address_group,
    repetition_divergence_groups as repetition_divergence_groups,
    witness_disagreement_cues as witness_disagreement_cues,
    introduced_term_cues as introduced_term_cues,
    _entity_occurrences as _entity_occurrences,
    _request_sha256 as _request_sha256,
    _valid_sha256 as _valid_sha256,
    build_human_text_entity_verifier as build_human_text_entity_verifier,
    _srt_clock_ms as _srt_clock_ms,
    reconcile_pending_text_overrides as reconcile_pending_text_overrides,
    _validated_read_aloud_verdict as _validated_read_aloud_verdict,
    _validated_entity_verdict as _validated_entity_verdict,
    recording_start_epoch_ms as recording_start_epoch_ms,
    _event_epoch_ms as _event_epoch_ms,
    _send_time_ms as _send_time_ms,
    _danmaku_text as _danmaku_text,
    load_chat_jsonl as load_chat_jsonl,
    _srt_timestamp as _srt_timestamp,
    _render_srt as _render_srt,
)
from src.autoslice.reviewed_text_reconciliation import (
    reconcile_reviewed_text_override_conflicts as reconcile_reviewed_text_override_conflicts,
)
from src.autoslice.chat_repair import (
    registered_entity_names as registered_entity_names,
    revert_unregistered_entity_repairs as revert_unregistered_entity_repairs,
    reconcile_contradictory_entity_repairs as reconcile_contradictory_entity_repairs,
    _match_metrics as _match_metrics,
    _best_text_split as _best_text_split,
    _norm_with_map as _norm_with_map,
    _shift_boundary_punct as _shift_boundary_punct,
    _strip_unrenderable_for_subtitle as _strip_unrenderable_for_subtitle,
    _excess_is_mid_read_interjection as _excess_is_mid_read_interjection,
    _strip_interjections_once as _strip_interjections_once,
    _fragment_spoken_in as _fragment_spoken_in,
    _aligned_span_replacements as _aligned_span_replacements,
    _partial_question_patch as _partial_question_patch,
    _matched_read_prefix as _matched_read_prefix,
    _authority_tail_continues_in_next_cue as _authority_tail_continues_in_next_cue,
    _spoken_sender_alias as _spoken_sender_alias,
    _mask_compatible_sender as _mask_compatible_sender,
    _sender_thank_anchor as _sender_thank_anchor,
    _repair_sc_sender as _repair_sc_sender,
    _gift_thank_cue_match as _gift_thank_cue_match,
    _apply_gift_name_repairs as _apply_gift_name_repairs,
    apply_audio_entity_verification as apply_audio_entity_verification,
)
from src.autoslice.chat_proposals import (
    _find_best_read_aloud_candidate as _find_best_read_aloud_candidate,
    _read_aloud_support_scores as _read_aloud_support_scores,
    _ChatProposalDiscovery as _ChatProposalDiscovery,
    _resolve_chat_entity_proposal as _resolve_chat_entity_proposal,
    _arbitrate_read_aloud_near_match as _arbitrate_read_aloud_near_match,
    _discover_chat_proposals as _discover_chat_proposals,
    _AppliedChatProposals as _AppliedChatProposals,
    _apply_chat_proposals as _apply_chat_proposals,
    _apply_sc_sender_repairs as _apply_sc_sender_repairs,
    _apply_chat_coreference_repairs as _apply_chat_coreference_repairs,
    _ChatAuthorityAuditParts as _ChatAuthorityAuditParts,
    _finalize_chat_authority_output as _finalize_chat_authority_output,
    apply_authoritative_chat_evidence as apply_authoritative_chat_evidence,
)
