"""Repair-first song completeness with compatibility-stable orchestration.

Provider, alignment, performance, and shared evidence implementations live in
focused modules.  This module remains the public workflow and patch-seam facade.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.llm_client import LlmCall
from src.autoslice.review_evidence import SourceCue
from src.autoslice import song_alignment as _song_alignment
from src.autoslice import song_lrc_provider as _song_lrc_provider

from src.autoslice.song_common import (
    REPO_ROOT as REPO_ROOT,
    CHANNEL_PROFILE as CHANNEL_PROFILE,
    HOST_LYRIC_SUBJECT as HOST_LYRIC_SUBJECT,
    HOST_NOT_SINGING_REASON as HOST_NOT_SINGING_REASON,
    SONG_REPAIR_SCHEMA_VERSION as SONG_REPAIR_SCHEMA_VERSION,
    SHIFT_TOLERANCE_MS as SHIFT_TOLERANCE_MS,
    MAX_AUDIO_LRC_VARIANT_ATTEMPTS as MAX_AUDIO_LRC_VARIANT_ATTEMPTS,
    _NON_LYRIC_CHARS as _NON_LYRIC_CHARS,
    _LRC_VARIANT_MARKERS as _LRC_VARIANT_MARKERS,
    LIVE_PERFORMANCE_READY_MODE as LIVE_PERFORMANCE_READY_MODE,
    LIVE_PERFORMANCE_MODES as LIVE_PERFORMANCE_MODES,
    AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION as AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION,
    AGY_AUDIO_LRC_RUN_SCHEMA_VERSION as AGY_AUDIO_LRC_RUN_SCHEMA_VERSION,
    AGY_AUDIO_LRC_CANONICALIZATION_STRATEGY as AGY_AUDIO_LRC_CANONICALIZATION_STRATEGY,
    AGY_AUDIO_LRC_PROVIDER as AGY_AUDIO_LRC_PROVIDER,
    AGY_AUDIO_LRC_MODEL as AGY_AUDIO_LRC_MODEL,
    GEMINI_API_AUDIO_LRC_PROVIDER as GEMINI_API_AUDIO_LRC_PROVIDER,
    GEMINI_API_AUDIO_LRC_MODEL as GEMINI_API_AUDIO_LRC_MODEL,
    AGY_AUDIO_LRC_FALLBACK_FAILURE_CATEGORIES as AGY_AUDIO_LRC_FALLBACK_FAILURE_CATEGORIES,
    LYRIC_VOCAL_SUBJECTS as LYRIC_VOCAL_SUBJECTS,
    HOST_LYRIC_ROLES as HOST_LYRIC_ROLES,
    MIN_READY_SUNG_LYRIC_ROWS as MIN_READY_SUNG_LYRIC_ROWS,
    MIN_READY_SUNG_LYRIC_RATIO as MIN_READY_SUNG_LYRIC_RATIO,
    MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS as MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS,
    MAX_READY_SPOKEN_LYRIC_DURATION_MS as MAX_READY_SPOKEN_LYRIC_DURATION_MS,
    MAX_READY_SPOKEN_BLOCK_SPAN_MS as MAX_READY_SPOKEN_BLOCK_SPAN_MS,
    MAX_READY_SPOKEN_BLOCKS as MAX_READY_SPOKEN_BLOCKS,
    LYRIC_VOCAL_ASSERTION_KEYS as LYRIC_VOCAL_ASSERTION_KEYS,
    LIVE_ARRANGEMENT_CLASSIFICATIONS as LIVE_ARRANGEMENT_CLASSIFICATIONS,
    LIVE_ARRANGEMENT_TRANSITIONS as LIVE_ARRANGEMENT_TRANSITIONS,
    MIN_LIVE_ARRANGEMENT_HEARD_ROWS as MIN_LIVE_ARRANGEMENT_HEARD_ROWS,
    MIN_LIVE_ARRANGEMENT_HEARD_RATIO as MIN_LIVE_ARRANGEMENT_HEARD_RATIO,
    MIN_LIVE_ARRANGEMENT_DURATION_MS as MIN_LIVE_ARRANGEMENT_DURATION_MS,
    MAX_LIVE_ARRANGEMENT_OMITTED_ROWS as MAX_LIVE_ARRANGEMENT_OMITTED_ROWS,
    MAX_LIVE_ARRANGEMENT_OMITTED_RATIO as MAX_LIVE_ARRANGEMENT_OMITTED_RATIO,
    MAX_LIVE_ARRANGEMENT_OMITTED_BLOCKS as MAX_LIVE_ARRANGEMENT_OMITTED_BLOCKS,
    MAX_LIVE_ARRANGEMENT_INTERLINE_GAP_MS as MAX_LIVE_ARRANGEMENT_INTERLINE_GAP_MS,
    MAX_LIVE_ARRANGEMENT_OUTRO_MS as MAX_LIVE_ARRANGEMENT_OUTRO_MS,
    _LRC_CHINESE_CREDIT_HEADS as _LRC_CHINESE_CREDIT_HEADS,
    _LRC_ENGLISH_CREDIT_HEADS as _LRC_ENGLISH_CREDIT_HEADS,
    _LRC_ENGLISH_BY_CREDIT as _LRC_ENGLISH_BY_CREDIT,
    LrcProvider as LrcProvider,
    AudioLrcAligner as AudioLrcAligner,
    LivePerformanceRejected as LivePerformanceRejected,
    live_performance_failure_reason_codes as live_performance_failure_reason_codes,
    _normalized_credit_head as _normalized_credit_head,
    _is_chinese_credit_head as _is_chinese_credit_head,
    is_lrc_credit_metadata as is_lrc_credit_metadata,
    LrcLine as LrcLine,
    LrcResult as LrcResult,
    AudioLrcAlignmentRun as AudioLrcAlignmentRun,
    validate_audio_lrc_execution_metadata as validate_audio_lrc_execution_metadata,
    canonicalize_audio_lrc_observation as canonicalize_audio_lrc_observation,
    SongRepairAttempt as SongRepairAttempt,
    SongRepairResult as SongRepairResult,
    normalize_lyric_text as normalize_lyric_text,
    _singable_lrc_result as _singable_lrc_result,
    SelectedSong as SelectedSong,
    AudioSelectionOutcome as AudioSelectionOutcome,
    _is_int as _is_int,
    _lrc_fingerprint as _lrc_fingerprint,
)
from src.autoslice.song_alignment import (
    ASR_ANCHOR_MATCH_THRESHOLD as ASR_ANCHOR_MATCH_THRESHOLD,
    ASR_ANCHOR_MIN_MATCH_RATIO as ASR_ANCHOR_MIN_MATCH_RATIO,
    ASR_ANCHOR_MIN_MATCHES as ASR_ANCHOR_MIN_MATCHES,
    ASR_ANCHOR_MAX_RESIDUAL_IQR_MS as ASR_ANCHOR_MAX_RESIDUAL_IQR_MS,
    ASR_ANCHOR_AGY_AGREEMENT_TOLERANCE_MS as ASR_ANCHOR_AGY_AGREEMENT_TOLERANCE_MS,
    validate_audio_lrc_canonical_projection as validate_audio_lrc_canonical_projection,
    _performance_window as _performance_window,
    _cjk_clean_score as _cjk_clean_score,
    _build_lyric_queries as _build_lyric_queries,
    generate_llm_song_queries as generate_llm_song_queries,
    _align_lrc_to_cues as _align_lrc_to_cues,
    _median_offset as _median_offset,
    enforce_global_shift_alignment as enforce_global_shift_alignment,
    _residual_iqr_ms as _residual_iqr_ms,
    match_lrc_to_fresh_asr_anchor as match_lrc_to_fresh_asr_anchor,
    _parse_srt_timestamp_ms as _parse_srt_timestamp_ms,
    _parse_asr_anchor_srt as _parse_asr_anchor_srt,
    _sibling_asr_anchor_srt_path as _sibling_asr_anchor_srt_path,
    _lrc_identity_key as _lrc_identity_key,
    _lrc_title_family as _lrc_title_family,
    _matched_cue_ids as _matched_cue_ids,
    _matched_cue_overlap as _matched_cue_overlap,
    _preferred_title_keys as _preferred_title_keys,
    _lrc_title_is_exact_hint as _lrc_title_is_exact_hint,
    _lrc_variant_penalty as _lrc_variant_penalty,
    _lrc_span_ms as _lrc_span_ms,
    _audio_lrc_entry_sort_key as _audio_lrc_entry_sort_key,
    _audio_lrc_retry_candidates as _audio_lrc_retry_candidates,
    _audio_lrc_infrastructure_reason as _audio_lrc_infrastructure_reason,
    AudioLrcGroups as AudioLrcGroups,
    _group_audio_lrc_candidates as _group_audio_lrc_candidates,
)
from src.autoslice.song_performance import (
    _sha256_file as _sha256_file,
    _require_bound_artifact as _require_bound_artifact,
    load_audio_lrc_json_artifact as load_audio_lrc_json_artifact,
    derive_live_arrangement_completeness as derive_live_arrangement_completeness,
    _validate_audio_lrc_artifact_bindings as _validate_audio_lrc_artifact_bindings,
    AudioObservationAlignment as AudioObservationAlignment,
    _build_audio_observation_alignment as _build_audio_observation_alignment,
    _finalize_audio_lrc_selection as _finalize_audio_lrc_selection,
    _validated_audio_lrc_selection as _validated_audio_lrc_selection,
    _lyric_row_interval as _lyric_row_interval,
    _validate_lyric_vocal_observations as _validate_lyric_vocal_observations,
    validate_live_performance_observation as validate_live_performance_observation,
)
from src.autoslice.song_lrc_provider import (
    parse_lrc_text as parse_lrc_text,
    _coerce_lrc_results as _coerce_lrc_results,
    _strip_kugou_markup as _strip_kugou_markup,
    _kugou_identity_text as _kugou_identity_text,
    _kugou_candidate_matches_track as _kugou_candidate_matches_track,
    _filter_kugou_timed_metadata as _filter_kugou_timed_metadata,
    _parse_lrclib_id as _parse_lrclib_id,
    _lrclib_record_to_result as _lrclib_record_to_result,
)

_http_json = _song_lrc_provider._http_json
_http_json_value = _song_lrc_provider._http_json_value
_lrc_content_similarity = _song_alignment._lrc_content_similarity


def _sync_provider_patch_seams() -> None:
    _song_lrc_provider._http_json = _http_json
    _song_lrc_provider._http_json_value = _http_json_value


def build_netease_lrc_provider(*args, **kwargs):
    _sync_provider_patch_seams()
    return _song_lrc_provider.build_netease_lrc_provider(*args, **kwargs)


def build_lrclib_lrc_provider(*args, **kwargs):
    _sync_provider_patch_seams()
    return _song_lrc_provider.build_lrclib_lrc_provider(*args, **kwargs)


def build_kugou_lrc_provider(*args, **kwargs):
    _sync_provider_patch_seams()
    return _song_lrc_provider.build_kugou_lrc_provider(*args, **kwargs)


def build_composite_lrc_provider(*args, **kwargs):
    _sync_provider_patch_seams()
    return _song_lrc_provider.build_composite_lrc_provider(*args, **kwargs)


def fetch_lrclib_lrc(*args, **kwargs):
    _sync_provider_patch_seams()
    return _song_lrc_provider.fetch_lrclib_lrc(*args, **kwargs)


def fetch_netease_lrc(*args, **kwargs):
    _sync_provider_patch_seams()
    return _song_lrc_provider.fetch_netease_lrc(*args, **kwargs)


def _choose_audio_lrc_candidate(
    ranked: Sequence[tuple[float, LrcResult, list[dict[str, object]]]],
    *,
    pinned_lrc_results: Sequence[LrcResult],
    min_recall_ratio: float,
    min_margin: float,
    preferred_title_hints: Sequence[str] = (),
) -> LrcResult:
    _song_alignment._lrc_content_similarity = _lrc_content_similarity
    return _song_alignment._choose_audio_lrc_candidate(
        ranked,
        pinned_lrc_results=pinned_lrc_results,
        min_recall_ratio=min_recall_ratio,
        min_margin=min_margin,
        preferred_title_hints=preferred_title_hints,
    )

def _round_robin_unique_queries(
    query_groups: Sequence[Sequence[str]],
    *,
    max_queries: int,
) -> list[str]:
    """Schedule distinct discovery queries breadth-first across evidence lanes.

    Screen/visual hints are intentionally the first lane, but they are only a
    hint: one wrong title must not consume the whole query budget before the
    LLM-recognized title or audio-ASR lyric lines get a provider turn.  The
    global provider-call budget remains ``max_queries``.
    """

    limit = max(0, int(max_queries))
    if not limit:
        return []
    groups = [
        [query.strip() for query in group if isinstance(query, str) and query.strip()]
        for group in query_groups
    ]
    positions = [0] * len(groups)
    seen: set[str] = set()
    scheduled: list[str] = []
    while len(scheduled) < limit:
        progressed = False
        for group_index, group in enumerate(groups):
            while positions[group_index] < len(group):
                query = group[positions[group_index]]
                positions[group_index] += 1
                if query in seen:
                    continue
                seen.add(query)
                scheduled.append(query)
                progressed = True
                break
            if len(scheduled) >= limit:
                break
        if not progressed:
            break
    return scheduled
def _select_audio_lrc(
    *,
    candidate_id: str,
    ranked: list[tuple[float, LrcResult, list[dict[str, object]]]],
    prepared_pinned_lrc_results: list[LrcResult],
    extra_queries: Sequence[str],
    max_audio_lrc_attempts: int,
    source_media_path: Path | None,
    source_duration_ms: int,
    output_dir: Path,
    audio_lrc_aligner: AudioLrcAligner,
    asr_anchor_cues: Sequence[SourceCue],
    asr_anchor_srt_path: Path | None,
    prior_audio_lrc_runs: Sequence[AudioLrcAlignmentRun],
    min_matched_ratio: float,
    attempts: list[SongRepairAttempt],
) -> AudioSelectionOutcome | SongRepairResult:
    selected: SelectedSong | None = None
    audio_alignment_run: AudioLrcAlignmentRun | None = None
    audio_lrc_variant_attempts: list[dict[str, object]] = []
    if source_media_path is None:
        attempts.append(SongRepairAttempt("agy_audio_lrc_alignment", "FAILED", "source media is required"))
        return _finish(candidate_id, attempts, output_dir)
    resolved_asr_anchor_cues: Sequence[SourceCue] = asr_anchor_cues
    if not resolved_asr_anchor_cues:
        anchor_srt_candidate = asr_anchor_srt_path
        if anchor_srt_candidate is None:
            anchor_srt_candidate = _sibling_asr_anchor_srt_path(Path(source_media_path))
        if anchor_srt_candidate is not None and anchor_srt_candidate.is_file():
            resolved_asr_anchor_cues = _parse_asr_anchor_srt(anchor_srt_candidate)
    try:
        primary_lrc = _choose_audio_lrc_candidate(
            ranked,
            pinned_lrc_results=prepared_pinned_lrc_results,
            min_recall_ratio=0.20,
            min_margin=0.08,
            preferred_title_hints=extra_queries,
        )
    except ValueError as exc:
        attempts.append(SongRepairAttempt("agy_audio_lrc_identity", "FAILED", str(exc)))
        return _finish(
            candidate_id,
            attempts,
            output_dir,
            reason_codes=("SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS",),
        )
    verification_candidates = _audio_lrc_retry_candidates(
        ranked,
        primary=primary_lrc,
        preferred_title_hints=extra_queries,
        max_attempts=max_audio_lrc_attempts,
    )
    attempts.append(
        SongRepairAttempt(
            "agy_audio_lrc_identity",
            "SUCCESS",
            f"ranked {len(verification_candidates)} deduped LRC variant(s) for bounded audio proof "
            f"(hard cap {MAX_AUDIO_LRC_VARIANT_ATTEMPTS}); primary={primary_lrc.song_title!r} "
            f"({primary_lrc.source_ref})",
        )
    )
    for ordinal, lrc in enumerate(verification_candidates, start=1):
        ranked_entry = next(
            (entry for entry in ranked if entry[1].source_ref == lrc.source_ref),
            (0.0, lrc, []),
        )
        recall_ratio = float(ranked_entry[0])
        matched_cues = len(_matched_cue_ids(ranked_entry[2]))
        rank_detail = (
            f"variant {ordinal}/{len(verification_candidates)} title={lrc.song_title!r} "
            f"source={lrc.source_ref}; exact_title={_lrc_title_is_exact_hint(lrc, extra_queries)}; "
            f"variant_penalty={_lrc_variant_penalty(lrc)}; "
            f"ASR_support={matched_cues}/{len(lrc.lines)} distinct cues/lines "
            f"({recall_ratio:.0%} row recall)"
        )
        attempts.append(SongRepairAttempt("agy_audio_lrc_variant", "SUCCESS", rank_detail))
        # Revalidate a bounded historical receipt *before* spending another
        # AGY/Gemini call.  The strict converter re-hashes every receipt
        # artifact and proves its canonical LRC is exactly this selected LRC;
        # a stale or mismatched receipt simply falls through to a fresh call.
        current_source_sha = _sha256_file(Path(source_media_path)) if prior_audio_lrc_runs else None
        for prior in prior_audio_lrc_runs:
            if prior.source_sha256 != current_source_sha:
                continue
            try:
                prior_selected = _validated_audio_lrc_selection(
                    run=prior,
                    lrc=lrc,
                    candidate_id=candidate_id,
                    source_media_path=Path(source_media_path),
                    source_duration_ms=source_duration_ms,
                    min_matched_ratio=min_matched_ratio,
                    asr_anchor_cues=resolved_asr_anchor_cues,
                )
            except Exception as prior_exc:
                audio_lrc_variant_attempts.append(
                    {
                        "ordinal": ordinal,
                        "song_title": lrc.song_title,
                        "source_ref": lrc.source_ref,
                        "status": "PRIOR_RECEIPT_REJECTED",
                        "reason": f"{type(prior_exc).__name__}: {prior_exc}",
                    }
                )
                continue
            selected = prior_selected
            audio_alignment_run = prior
            audio_lrc_variant_attempts.append(
                {
                    "ordinal": ordinal,
                    "song_title": lrc.song_title,
                    "source_ref": lrc.source_ref,
                    "status": "PRIOR_RECEIPT_ACCEPTED",
                    "reason": "current validator accepted an earlier hash-bound receipt for the current source and canonical LRC",
                }
            )
            attempts.append(
                SongRepairAttempt(
                    "agy_audio_lrc_prior_receipt",
                    "SUCCESS",
                    f"reused current-validator PASS for source sha256:{prior.source_sha256[:12]} and LRC sha256:{prior.lrc_sha256[:12]}",
                )
            )
            break
        if selected is not None:
            break
        try:
            current_run = audio_lrc_aligner(
                Path(source_media_path),
                lrc,
                candidate_id,
                output_dir / "agy_audio_lrc" / f"variant-{ordinal:02d}",
            )
        except Exception as exc:
            infrastructure_reason = _audio_lrc_infrastructure_reason(exc)
            detail = f"{rank_detail}; runner failed: {type(exc).__name__}: {exc}"
            attempts.append(SongRepairAttempt("agy_audio_lrc_alignment", "FAILED", detail))
            audio_lrc_variant_attempts.append(
                {
                    "ordinal": ordinal,
                    "song_title": lrc.song_title,
                    "source_ref": lrc.source_ref,
                    "status": "INFRA_FAILED" if infrastructure_reason else "FAILED",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            # A provider timeout/quota/nonzero exit affects every variant.
            # Do not burn the remaining expensive attempts; preserve the
            # runner's cross-tick recoverable reason code.
            return _finish(
                candidate_id,
                attempts,
                output_dir,
                reason_codes=(infrastructure_reason or "SONG_AUDIO_LRC_ALIGNMENT_INVALID",),
            )
        try:
            current_selected = _validated_audio_lrc_selection(
                run=current_run,
                lrc=lrc,
                candidate_id=candidate_id,
                source_media_path=Path(source_media_path),
                source_duration_ms=source_duration_ms,
                min_matched_ratio=min_matched_ratio,
                asr_anchor_cues=resolved_asr_anchor_cues,
            )
        except LivePerformanceRejected as exc:
            detail = f"{rank_detail}; {type(exc).__name__}: {exc}"
            attempts.append(SongRepairAttempt("agy_audio_lrc_alignment", "FAILED", detail))
            audio_lrc_variant_attempts.append(
                {
                    "ordinal": ordinal,
                    "song_title": lrc.song_title,
                    "source_ref": lrc.source_ref,
                    "status": "PERFORMER_REJECTED",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            # A structurally valid same-audio performer veto is not an LRC
            # variant problem and must never be bypassed by another lyric.
            return _finish(
                candidate_id,
                attempts,
                output_dir,
                reason_codes=exc.reason_codes,
                live_performance=exc.performance,
            )
        except Exception as exc:
            detail = f"{rank_detail}; validator rejected this variant: {type(exc).__name__}: {exc}"
            attempts.append(SongRepairAttempt("agy_audio_lrc_alignment", "FAILED", detail))
            audio_lrc_variant_attempts.append(
                {
                    "ordinal": ordinal,
                    "song_title": lrc.song_title,
                    "source_ref": lrc.source_ref,
                    "status": "CONTENT_OR_ALIGNMENT_REJECTED",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        selected = current_selected
        audio_alignment_run = current_run
        audio_lrc_variant_attempts.append(
            {
                "ordinal": ordinal,
                "song_title": lrc.song_title,
                "source_ref": lrc.source_ref,
                "status": "ACCEPTED",
                "reason": "strict audio/LRC, live-performance, and global-shift proof passed",
            }
        )
        attempts.append(
            SongRepairAttempt(
                "agy_audio_lrc_alignment",
                "SUCCESS",
                f"{rank_detail}; current audio proves {selected[0]:.0%} of performed live-arrangement lines, "
                f"{selected[9]['classification']}, one global shift, and "
                "LIVE_STREAMER_SINGING performance mode",
            )
        )
        break
    if selected is None:
        attempts.append(
            SongRepairAttempt(
                "agy_audio_lrc_variants_exhausted",
                "FAILED",
                f"all {len(verification_candidates)} bounded, deduped LRC variant(s) failed strict audio proof",
            )
        )
        return _finish(
            candidate_id,
            attempts,
            output_dir,
            reason_codes=("SONG_AUDIO_LRC_ALIGNMENT_INVALID",),
        )
    assert selected is not None
    assert audio_alignment_run is not None
    return AudioSelectionOutcome(
        selected=selected,
        audio_alignment_run=audio_alignment_run,
        variant_attempts=audio_lrc_variant_attempts,
    )

def _select_text_lrc(
    *,
    ranked: list[tuple[float, LrcResult, list[dict[str, object]]]],
    window: Sequence[SourceCue],
    cues: Sequence[SourceCue],
    source_duration_ms: int,
    min_matched_ratio: float,
    match_threshold: float,
    pre_roll_ms: int,
    post_roll_ms: int,
    max_outro_ms: int,
    attempts: list[SongRepairAttempt],
) -> SelectedSong | None:
    selected: SelectedSong | None = None
    for raw_matched_ratio, lrc, raw_alignment in ranked if selected is None else ():
        if raw_matched_ratio < min_matched_ratio:
            continue
        # Strict process (song-lyrics-timeline-aligner skill): a real performance
        # of this LRC must be explainable by ONE global time shift
        # (clip_time = lrc_time + offset).  Greedy per-line matches that pile
        # onto the same cue or jump around in time are not evidence.
        # Two-pass: estimate the shift from the recall alignment, then re-align
        # constrained to that shift so lines whose text also appears in a false
        # start / abandoned take land on their shift-consistent occurrence.
        estimated_offset_ms = _median_offset(raw_alignment)
        if estimated_offset_ms is not None:
            constrained = _align_lrc_to_cues(
                lrc.lines,
                window,
                match_threshold=match_threshold,
                target_offset_ms=estimated_offset_ms,
                offset_tolerance_ms=SHIFT_TOLERANCE_MS,
            )
        else:
            constrained = raw_alignment
        alignment, offset_ms, shift_error = enforce_global_shift_alignment(alignment=constrained)
        if shift_error is not None:
            attempts.append(
                SongRepairAttempt(
                    "global_shift_check",
                    "FAILED",
                    f"{lrc.song_title!r}: {shift_error}",
                )
            )
            continue
        matched = [entry for entry in alignment if entry["matched_cue_id"] is not None]
        matched_ratio = len(matched) / len(alignment) if alignment else 0.0
        if matched_ratio < min_matched_ratio:
            attempts.append(
                SongRepairAttempt(
                    "global_shift_check",
                    "FAILED",
                    f"{lrc.song_title!r}: only {matched_ratio:.0%} of LRC lines survive the single-global-shift model (raw greedy match was {raw_matched_ratio:.0%})",
                )
            )
            continue
        attempts.append(
            SongRepairAttempt(
                "global_shift_check",
                "SUCCESS",
                f"{lrc.song_title!r}: offset {offset_ms}ms explains {matched_ratio:.0%} of LRC lines",
            )
        )
        head_missing = _leading_unmatched(alignment)
        tail_missing = _trailing_unmatched(alignment)
        if head_missing:
            attempts.append(
                SongRepairAttempt(
                    "song_completeness",
                    "FAILED",
                    f"{lrc.song_title!r}: song head missing from source: first {head_missing} LRC line(s) have no ASR match — the recording window starts mid-song, a complete-song slice is impossible from this source range",
                )
            )
            continue
        # Tolerate a SHORT unmatched tail: the ASR routinely drops the last few
        # sung lines of an outro (quiet fade, applause/'谢谢大家' bleed) even when
        # she sang the song in full (Ivan 2026-07-07 confirmed 《屑屑》 was complete
        # to 4:11 yet the ASR missed 3 tail lines → false BLOCK).  The LRC, not the
        # ASR, is the subtitle authority, so those lines still render.  A GENUINELY
        # cut-off song leaves many more unmatched tail lines and still fails.  The
        # HEAD stays strict — a mid-song start is truly unsliceable.
        tail_limit = max(3, len(alignment) // 12)
        if tail_missing > tail_limit:
            attempts.append(
                SongRepairAttempt(
                    "song_completeness",
                    "FAILED",
                    f"{lrc.song_title!r}: song tail missing from source: last {tail_missing} LRC line(s) have no ASR match (tolerated {tail_limit}) — the performance was cut off or the capture ended early",
                )
            )
            continue
        middle_gap = _longest_unmatched_run(alignment)
        middle_gap_limit = max(4, (len(alignment) * 3) // 10)
        if middle_gap > middle_gap_limit:
            attempts.append(
                SongRepairAttempt(
                    "song_completeness",
                    "FAILED",
                    f"{lrc.song_title!r}: song middle missing from source: {middle_gap} consecutive LRC line(s) have no shift-consistent ASR match (limit {middle_gap_limit}) — the performance is not continuously present in the capture",
                )
            )
            continue
        first_cue = next(entry for entry in alignment if entry["matched_cue_id"] is not None)
        last_cue = next(entry for entry in reversed(alignment) if entry["matched_cue_id"] is not None)
        first_cue_start = first_cue.get("cue_start_ms")
        last_cue_end = last_cue.get("cue_end_ms")
        if not isinstance(first_cue_start, int) or not isinstance(last_cue_end, int):
            attempts.append(
                SongRepairAttempt(
                    "song_boundary",
                    "FAILED",
                    f"{lrc.song_title!r}: matched cue timing is missing or non-integer",
                )
            )
            continue
        first_lyric_start_ms = first_cue_start
        last_lyric_end_ms = last_cue_end
        # A global shift is not sufficient proof when the implied LRC time zero
        # falls outside the captured source.  In particular, clamping a negative
        # offset to source 0 below would turn a recording that starts after the
        # song's instrumental intro into a false FULL_SONG_READY result.
        if not 0 <= offset_ms < source_duration_ms:
            attempts.append(
                SongRepairAttempt(
                    "song_boundary",
                    "FAILED",
                    f"{lrc.song_title!r}: nominal LRC zero {offset_ms}ms is outside source "
                    f"[0, {source_duration_ms})ms; cannot prove the complete song head",
                )
            )
            continue
        # ``offset_ms`` is where LRC time zero lands in the live source.  Start
        # from that nominal track boundary (plus a small safety lead), not from
        # ``first lyric - pre_roll``: the latter chopped the entire 7.7-second
        # instrumental intro from 2026-07-09 《芽吹くとき》.
        clip_start_ms = max(0, offset_ms - pre_roll_ms)
        # Keep the instrumental outro (后奏): a complete song does not end on the
        # last sung word.  The outro has no lyrics for ASR to anchor on, so extend
        # to just before the next spoken cue after the song (the post-song talk),
        # capped by max_outro_ms.  When the song ends the window, keep the full cap.
        next_cue_start_ms = min(
            (cue.source_start_ms for cue in cues if cue.source_start_ms > last_lyric_end_ms),
            default=None,
        )
        outro_cap_ms = last_lyric_end_ms + max_outro_ms
        outro_end_ms = outro_cap_ms if next_cue_start_ms is None else min(next_cue_start_ms, outro_cap_ms)
        # never cut the last sung word's natural tail
        outro_end_ms = max(outro_end_ms, last_lyric_end_ms + min(post_roll_ms, 1_000))
        clip_end_ms = min(source_duration_ms, outro_end_ms)
        if clip_end_ms <= clip_start_ms:
            attempts.append(
                SongRepairAttempt(
                    "song_boundary",
                    "FAILED",
                    f"{lrc.song_title!r}: matched LRC lines map to a non-monotonic cue order; cannot prove a valid full-song start/end boundary",
                )
            )
            continue
        attempts.append(
            SongRepairAttempt(
                "song_completeness",
                "SUCCESS",
                f"{lrc.song_title!r}: {matched_ratio:.0%} of LRC lines matched; head and tail covered",
            )
        )
        selected = (
            matched_ratio,
            lrc,
            alignment,
            matched,
            first_lyric_start_ms,
            last_lyric_end_ms,
            clip_start_ms,
            clip_end_ms,
            offset_ms,
            None,
            None,
        )
        break
    return selected

@dataclass(frozen=True)
class LrcDiscovery:
    candidates: list[LrcResult]
    prepared_pinned_results: list[LrcResult]
    queries: list[str]


def _discover_lrc_candidates(
    *,
    candidate_id: str,
    window: Sequence[SourceCue],
    anchor_start_ms: int,
    anchor_end_ms: int,
    output_dir: Path,
    lrc_provider: LrcProvider | None,
    hint_llm_call: LlmCall | None,
    extra_queries: Sequence[str],
    max_queries: int,
    max_lrc_candidates: int,
    pinned_lrc_results: Sequence[LrcResult],
    attempts: list[SongRepairAttempt],
) -> LrcDiscovery | SongRepairResult:
    explicit_queries = [q.strip() for q in extra_queries if isinstance(q, str) and q.strip()]
    llm_queries: list[str] = []
    if hint_llm_call is not None:
        # Semantic repair: garbled ASR defeats text search, but an LLM can often
        # still recognize the song from misheard lyrics and give clean queries.
        try:
            hints = generate_llm_song_queries(window, hint_llm_call)
        except Exception as exc:
            hints = []
            attempts.append(SongRepairAttempt("llm_song_hint", "FAILED", f"{type(exc).__name__}: {exc}"))
        else:
            if hints:
                attempts.append(SongRepairAttempt("llm_song_hint", "SUCCESS", f"guessed queries: {hints}"))
            else:
                attempts.append(SongRepairAttempt("llm_song_hint", "FAILED", "LLM returned no usable song guesses"))
        llm_queries.extend(hints)
    lyric_queries = _build_lyric_queries(window, anchor_start_ms, anchor_end_ms)
    deduped_queries = _round_robin_unique_queries(
        (explicit_queries, llm_queries, lyric_queries),
        max_queries=max_queries,
    )

    candidates: list[LrcResult] = []
    seen_refs: set[str] = set()
    prepared_pinned_lrc_results: list[LrcResult] = []
    for pinned in pinned_lrc_results:
        if not isinstance(pinned, LrcResult):
            continue
        prepared, excluded_metadata = _singable_lrc_result(pinned)
        if prepared.lines and prepared.source_ref not in seen_refs:
            seen_refs.add(prepared.source_ref)
            candidates.append(prepared)
            prepared_pinned_lrc_results.append(prepared)
            attempts.append(
                SongRepairAttempt(
                    "pinned_lrc",
                    "SUCCESS",
                    f"{prepared.song_title!r} ({prepared.source_ref}) pinned "
                    f"({len(prepared.lines)} singable LRC lines, {excluded_metadata} timed credit rows excluded) "
                    "— deterministic, ranked against search",
                )
            )
    provider_errors: list[str] = []
    provider_results: list[tuple[str, list[LrcResult]]] = []
    provider_candidate_cap = max(0, int(max_lrc_candidates))
    if lrc_provider is not None and len(candidates) < provider_candidate_cap:
        for query in deduped_queries:
            try:
                found = lrc_provider(query)
            except Exception as exc:  # provider failures must not crash review; they are recorded
                provider_errors.append(f"{query!r}: {type(exc).__name__}: {exc}")
                continue
            # A provider may return many near-identical search rows.  Retain at
            # most the existing global candidate budget from any one call;
            # admission below is breadth-first across calls.
            provider_results.append((query, _coerce_lrc_results(found)[:provider_candidate_cap]))

        # Round-robin the provider result rank too.  Previously the first query
        # could append all eight rows and prevent a later correct title from
        # entering the pool at all.  Query order still makes the visual result
        # first when it is right, while actual ASR/audio proof may overturn it.
        result_rank = 0
        while len(candidates) < provider_candidate_cap:
            progressed = False
            for _query, found_list in provider_results:
                if result_rank >= len(found_list):
                    continue
                progressed = True
                item = found_list[result_rank]
                prepared, _excluded_metadata = _singable_lrc_result(item)
                if prepared.lines and prepared.source_ref not in seen_refs:
                    seen_refs.add(prepared.source_ref)
                    candidates.append(prepared)
                    if len(candidates) >= provider_candidate_cap:
                        break
            if not progressed:
                break
            result_rank += 1
    if not candidates:
        detail = f"no LRC found for {len(deduped_queries)} queries {deduped_queries!r}"
        if provider_errors:
            detail += f"; provider errors: {'; '.join(provider_errors[:3])}"
        attempts.append(SongRepairAttempt("lrc_discovery", "FAILED", detail))
        return _finish(candidate_id, attempts, output_dir)
    attempts.append(
        SongRepairAttempt(
            "lrc_discovery",
            "SUCCESS",
            f"{len(candidates)} LRC candidate(s) from {len(deduped_queries)} queries",
        )
    )
    return LrcDiscovery(
        candidates=candidates,
        prepared_pinned_results=prepared_pinned_lrc_results,
        queries=deduped_queries,
    )


def _rank_lrc_candidates(
    *,
    candidates: list[LrcResult],
    window: Sequence[SourceCue],
    min_matched_ratio: float,
    match_threshold: float,
    attempts: list[SongRepairAttempt],
) -> list[tuple[float, LrcResult, list[dict[str, object]]]]:
    ranked: list[tuple[float, LrcResult, list[dict[str, object]]]] = []
    for candidate_lrc in candidates:
        candidate_alignment = _align_lrc_to_cues(candidate_lrc.lines, window, match_threshold=match_threshold)
        candidate_matched = [entry for entry in candidate_alignment if entry["matched_cue_id"] is not None]
        ratio = len(candidate_matched) / len(candidate_alignment) if candidate_alignment else 0.0
        ranked.append((ratio, candidate_lrc, candidate_alignment))
        attempts.append(
            SongRepairAttempt(
                "candidate_alignment",
                "SUCCESS" if ratio >= min_matched_ratio else "FAILED",
                f"{candidate_lrc.song_title!r} ({candidate_lrc.source_ref}): {ratio:.0%} of {len(candidate_alignment)} lines matched",
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked

def _materialize_song_repair(
    *,
    candidate_id: str,
    output_dir: Path,
    provider_name: str | None,
    selected: SelectedSong,
    audio_alignment_run: AudioLrcAlignmentRun | None,
    audio_lrc_variant_attempts: list[dict[str, object]],
    attempts: list[SongRepairAttempt],
) -> SongRepairResult:
    (
        matched_ratio,
        lrc,
        alignment,
        matched,
        first_lyric_start_ms,
        last_lyric_end_ms,
        clip_start_ms,
        clip_end_ms,
        offset_ms,
        arrangement_completeness,
        offset_provenance,
    ) = selected

    provider_label = provider_name or lrc.provider
    report_payload = {
        "schema_version": "lyrics-alignment-report.v1",
        "alignment_model": "external_lrc_global_shift.v1",
        "candidate_id": candidate_id,
        "provider": provider_label,
        "song_title": lrc.song_title,
        "artist": lrc.artist,
        "source_ref": lrc.source_ref,
        "matched_line_ratio": round(matched_ratio, 4),
        "matched_line_denominator": (
            "performed_live_arrangement_lines"
            if audio_alignment_run is not None
            else "singable_lrc_lines"
        ),
        "line_count": len(alignment),
        "matched_line_count": len(matched),
        "offset_ms": offset_ms,
        "nominal_lrc_zero_ms": offset_ms,
        "lyric_lines": [
            {
                **(
                    {"lrc_index": alignment[index].get("canonical_lrc_index")}
                    if audio_alignment_run is not None
                    else {}
                ),
                "lrc_time_ms": line.time_ms,
                "text": line.text,
            }
            for index, line in enumerate(lrc.lines)
        ],
        "first_lyric_start_ms": first_lyric_start_ms,
        "last_lyric_end_ms": last_lyric_end_ms,
        "alignment": alignment,
    }
    if audio_alignment_run is not None:
        canonical_rows = audio_alignment_run.payload["observations"]
        provenance = dict(offset_provenance or {})
        report_payload.update(
            {
                "evidence_source": "agy_audio_lrc",
                # Offset provenance (2026-07-11 fix): a fresh-ASR anchor,
                # when it earns enough matches with a tight residual spread
                # and agrees with the AGY median within 1500ms, replaces the
                # AGY-only offset as ``offset_ms``/``nominal_lrc_zero_ms``
                # above.  These fields keep both readings auditable.
                "offset_basis": provenance.get("offset_basis", "agy_median"),
                "agy_offset_ms": provenance.get("agy_offset_ms"),
                "asr_offset_ms": provenance.get("asr_offset_ms"),
                "asr_matched_line_count": provenance.get("asr_matched_line_count", 0),
                "audio_alignment_provider": audio_alignment_run.provider,
                "audio_alignment_model": audio_alignment_run.model,
                "audio_alignment_agy_rc": audio_alignment_run.rc,
                "audio_alignment_provider_fallback_used": audio_alignment_run.provider_fallback_used,
                "audio_alignment_agy_failure_category": audio_alignment_run.agy_failure_category,
                "audio_lrc_variant_attempts": audio_lrc_variant_attempts,
                "spot_checks": audio_alignment_run.payload["spot_checks"],
                "live_performance": audio_alignment_run.payload["live_performance"],
                "live_arrangement_observation": audio_alignment_run.payload["live_arrangement"],
                "arrangement_completeness": dict(arrangement_completeness or {}),
                "canonical_line_count": len(canonical_rows),
                "canonical_lyric_lines": [
                    {
                        "lrc_index": row["lrc_index"],
                        "lrc_time_ms": row["lrc_time_ms"],
                        "text": row["text"],
                    }
                    for row in canonical_rows
                ],
                "post_song_talk_start_ms": audio_alignment_run.payload["post_song_talk_start_ms"],
                "source_media_path": audio_alignment_run.source_origin_path,
                "source_media_sha256": audio_alignment_run.source_sha256,
                "audio_alignment_artifacts": {
                    "source_origin_path": audio_alignment_run.source_origin_path,
                    "source_path": audio_alignment_run.source_path,
                    "source_sha256": audio_alignment_run.source_sha256,
                    "source_duration_ms": audio_alignment_run.source_duration_ms,
                    "lrc_path": audio_alignment_run.lrc_path,
                    "lrc_sha256": audio_alignment_run.lrc_sha256,
                    "prompt_path": audio_alignment_run.prompt_path,
                    "prompt_sha256": audio_alignment_run.prompt_sha256,
                    "provider_raw_output_path": audio_alignment_run.provider_raw_output_path,
                    "provider_raw_output_sha256": audio_alignment_run.provider_raw_output_sha256,
                    "canonicalized_output_path": audio_alignment_run.output_path,
                    "canonicalized_output_sha256": audio_alignment_run.output_sha256,
                    # Compatibility aliases consumed by existing report
                    # verifiers.  These now name the exact-index canonicalized
                    # observation; the untouched provider bytes are above.
                    "raw_output_path": audio_alignment_run.output_path,
                    "raw_output_sha256": audio_alignment_run.output_sha256,
                    "run_manifest_path": audio_alignment_run.manifest_path,
                    "run_manifest_sha256": audio_alignment_run.manifest_sha256,
                    **(
                        {
                            "api_audio_path": audio_alignment_run.api_audio_path,
                            "api_audio_sha256": audio_alignment_run.api_audio_sha256,
                            "api_audio_duration_ms": audio_alignment_run.api_audio_duration_ms,
                        }
                        if audio_alignment_run.provider == GEMINI_API_AUDIO_LRC_PROVIDER
                        else {}
                    ),
                },
            }
        )
    report_path = output_dir / f"{candidate_id}.lyrics-alignment-report.json"
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    attempts.append(SongRepairAttempt("alignment_report", "SUCCESS", f"{report_path.name} sha256:{report_sha[:12]}…"))

    song_boundary = {
        "status": "FULL_SONG_READY",
        "source": f"song_repair.{provider_label}",
        "song_title": lrc.song_title,
        "song_start_ms": clip_start_ms,
        "nominal_lrc_zero_ms": offset_ms,
        "first_lyric_start_ms": first_lyric_start_ms,
        "last_lyric_end_ms": last_lyric_end_ms,
        "clip_start_ms": clip_start_ms,
        "clip_end_ms": clip_end_ms,
    }
    if arrangement_completeness is not None:
        song_boundary["completion_basis"] = arrangement_completeness["classification"]
    lyrics_alignment = {
        "status": "READY",
        "provider": provider_label,
        "model": (
            f"{provider_label}-agy-audio-lrc-global-shift-v1"
            if audio_alignment_run is not None
            else f"{provider_label}-lrc-global-shift-align-v2"
        ),
        "source": f"song_repair.{provider_label}",
        "external_lrc": lrc.source_ref,
        "matched_line_ratio": round(matched_ratio, 4),
        "offset_ms": offset_ms,
        "nominal_lrc_zero_ms": offset_ms,
        "alignment_report_path": str(report_path),
        "alignment_report_sha256": report_sha,
        "source_media_path": audio_alignment_run.source_origin_path if audio_alignment_run is not None else None,
        "source_media_sha256": audio_alignment_run.source_sha256 if audio_alignment_run is not None else None,
    }
    if arrangement_completeness is not None:
        lyrics_alignment["completion_basis"] = arrangement_completeness["classification"]
    return _finish(
        candidate_id,
        attempts,
        output_dir,
        repaired=True,
        song_boundary=song_boundary,
        lyrics_alignment=lyrics_alignment,
    )

def attempt_song_repair(
    *,
    candidate_id: str,
    cues: Sequence[SourceCue],
    anchor_start_ms: int,
    anchor_end_ms: int,
    source_duration_ms: int,
    output_dir: Path,
    lrc_provider: LrcProvider | None = None,
    provider_name: str | None = None,
    hint_llm_call: LlmCall | None = None,
    extra_queries: Sequence[str] = (),
    max_queries: int = 10,
    max_lrc_candidates: int = 8,
    min_matched_ratio: float = 0.55,
    match_threshold: float = 0.45,
    max_window_gap_ms: int = 30_000,
    pre_roll_ms: int = 1_500,
    post_roll_ms: int = 4_000,
    max_outro_ms: int = 22_000,
    pinned_lrc_results: Sequence[LrcResult] = (),
    source_media_path: Path | None = None,
    audio_lrc_aligner: AudioLrcAligner | None = None,
    max_audio_lrc_attempts: int = MAX_AUDIO_LRC_VARIANT_ATTEMPTS,
    asr_anchor_cues: Sequence[SourceCue] = (),
    asr_anchor_srt_path: Path | None = None,
    prior_audio_lrc_runs: Sequence[AudioLrcAlignmentRun] = (),
) -> SongRepairResult:
    """Try to repair a song candidate into a fully-proven full-song boundary.

    ``asr_anchor_cues`` are a fresh (non-AGY) ASR transcript of the same
    source media, used only to anchor the AGY-audio path's global lyric shift
    (see ``match_lrc_to_fresh_asr_anchor``); pass already-parsed cues when the
    caller has them (e.g. the shadow pipeline's ``source_srt``).  When empty,
    ``asr_anchor_srt_path`` (or a sibling-file lookup next to
    ``source_media_path``) is tried as a fallback.

    ``pinned_lrc_results`` are caller-supplied LRCs (e.g. a known recurring song
    matched by fingerprint) that are added to the candidate pool BEFORE flaky
    LLM/text discovery, so alignment ranking can still pick the right song when
    search misses it.  ``max_outro_ms`` caps the instrumental 后奏 (outro) kept
    after the last sung line up to the next spoken cue.
    """

    attempts: list[SongRepairAttempt] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    window = _performance_window(cues, anchor_start_ms, anchor_end_ms, max_gap_ms=max_window_gap_ms)
    if not window:
        attempts.append(SongRepairAttempt("performance_window", "FAILED", "no cues overlap the anchor window"))
        return _finish(candidate_id, attempts, output_dir)
    attempts.append(
        SongRepairAttempt(
            "performance_window",
            "SUCCESS",
            f"{len(window)} cues {window[0].source_start_ms}..{window[-1].source_end_ms}ms",
        )
    )

    if lrc_provider is None and not pinned_lrc_results:
        attempts.append(
            SongRepairAttempt(
                "lrc_discovery",
                "SKIPPED",
                "no LRC provider configured (pass lrc_provider / --lrc-provider to enable API repair)",
            )
        )
        return _finish(candidate_id, attempts, output_dir)

    discovery = _discover_lrc_candidates(
        candidate_id=candidate_id,
        window=window,
        anchor_start_ms=anchor_start_ms,
        anchor_end_ms=anchor_end_ms,
        output_dir=output_dir,
        lrc_provider=lrc_provider,
        hint_llm_call=hint_llm_call,
        extra_queries=extra_queries,
        max_queries=max_queries,
        max_lrc_candidates=max_lrc_candidates,
        pinned_lrc_results=pinned_lrc_results,
        attempts=attempts,
    )
    if isinstance(discovery, SongRepairResult):
        return discovery
    prepared_pinned_lrc_results = discovery.prepared_pinned_results
    ranked = _rank_lrc_candidates(
        candidates=discovery.candidates,
        window=window,
        min_matched_ratio=min_matched_ratio,
        match_threshold=match_threshold,
        attempts=attempts,
    )
    selected: SelectedSong | None = None
    audio_alignment_run: AudioLrcAlignmentRun | None = None
    audio_lrc_variant_attempts: list[dict[str, object]] = []
    if audio_lrc_aligner is not None:
        audio_outcome = _select_audio_lrc(
            candidate_id=candidate_id,
            ranked=ranked,
            prepared_pinned_lrc_results=prepared_pinned_lrc_results,
            extra_queries=extra_queries,
            max_audio_lrc_attempts=max_audio_lrc_attempts,
            source_media_path=source_media_path,
            source_duration_ms=source_duration_ms,
            output_dir=output_dir,
            audio_lrc_aligner=audio_lrc_aligner,
            asr_anchor_cues=asr_anchor_cues,
            asr_anchor_srt_path=asr_anchor_srt_path,
            prior_audio_lrc_runs=prior_audio_lrc_runs,
            min_matched_ratio=min_matched_ratio,
            attempts=attempts,
        )
        if isinstance(audio_outcome, SongRepairResult):
            return audio_outcome
        selected = audio_outcome.selected
        audio_alignment_run = audio_outcome.audio_alignment_run
        audio_lrc_variant_attempts = audio_outcome.variant_attempts
    elif ranked[0][0] < min_matched_ratio:
        matched_ratio, lrc, _alignment = ranked[0]
        if source_media_path is None or audio_lrc_aligner is None:
            attempts.append(
                SongRepairAttempt(
                    "lyrics_alignment",
                    "FAILED",
                    f"best of {len(ranked)} candidate(s) {lrc.song_title!r} matched only {matched_ratio:.0%} of LRC lines (need >= {min_matched_ratio:.0%}); likely a different song or too-noisy ASR",
                )
            )
            return _finish(candidate_id, attempts, output_dir)

    if selected is None:
        selected = _select_text_lrc(
            ranked=ranked,
            window=window,
            cues=cues,
            source_duration_ms=source_duration_ms,
            min_matched_ratio=min_matched_ratio,
            match_threshold=match_threshold,
            pre_roll_ms=pre_roll_ms,
            post_roll_ms=post_roll_ms,
            max_outro_ms=max_outro_ms,
            attempts=attempts,
        )

    if selected is None:
        return _finish(candidate_id, attempts, output_dir)

    return _materialize_song_repair(
        candidate_id=candidate_id,
        output_dir=output_dir,
        provider_name=provider_name,
        selected=selected,
        audio_alignment_run=audio_alignment_run,
        audio_lrc_variant_attempts=audio_lrc_variant_attempts,
        attempts=attempts,
    )
def _longest_unmatched_run(alignment: Sequence[Mapping[str, object]]) -> int:
    longest = 0
    current = 0
    for entry in alignment:
        if entry["matched_cue_id"] is None:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _leading_unmatched(alignment: Sequence[Mapping[str, object]], *, tolerance: int = 1) -> int:
    count = 0
    for entry in alignment:
        if entry["matched_cue_id"] is None:
            count += 1
        else:
            break
    return count if count > tolerance else 0


def _trailing_unmatched(alignment: Sequence[Mapping[str, object]], *, tolerance: int = 1) -> int:
    count = 0
    for entry in reversed(alignment):
        if entry["matched_cue_id"] is None:
            count += 1
        else:
            break
    return count if count > tolerance else 0
def _finish(
    candidate_id: str,
    attempts: list[SongRepairAttempt],
    output_dir: Path,
    *,
    repaired: bool = False,
    song_boundary: Mapping[str, object] | None = None,
    lyrics_alignment: Mapping[str, object] | None = None,
    reason_codes: Sequence[str] = (),
    live_performance: Mapping[str, object] | None = None,
) -> SongRepairResult:
    report_path = output_dir / f"{candidate_id}.song-repair.json"
    result = SongRepairResult(
        repaired=repaired,
        attempts=tuple(attempts),
        song_boundary=song_boundary,
        lyrics_alignment=lyrics_alignment,
        report_path=str(report_path),
        reason_codes=tuple(dict.fromkeys(str(code) for code in reason_codes if code)),
        live_performance=dict(live_performance) if live_performance else None,
    )
    report_path.write_text(json.dumps(result.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
