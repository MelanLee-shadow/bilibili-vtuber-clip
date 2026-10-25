"""Recomputable acoustic authority for exact-final convergence mutations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.acoustic_witness_protocol import (
    BLIND_PINYIN_PROTOCOL,
    CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL,
    contains_han_text,
    supported_witness_protocol,
    witness_protocol,
)
from src.autoslice.local_asr_target_evidence import MODELS, SCHEMA as NATIVE_SCHEMA


BINDING_SCHEMA = "subtitle-repair-acoustic-witness-binding.v1"
RANGE_WORD_PROJECTION_BINDING_SCHEMA = (
    "subtitle-repair-range-word-projection-binding.v1"
)
# Keep this as a separate field and schema.  A range observation is context
# evidence; its native word spans may project one current cue, but they never
# become the exact-crop witness stored under ``acoustic_witness_binding``.
RANGE_WORD_PROJECTION_FIELD = "range_word_projection_binding"
GATE_SCHEMA = "history-convergence-acoustic-witness-authority.v1"
REQUIRED_REASON = "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED"
DOWNGRADE_BRANCH = "HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY"
CPA_MORE_AUDIO_REASON = "CPA_REQUESTS_MORE_AUDIO"
HISTORY_AUDIO_RECOVERY_SCHEMA = (
    "history-convergence-acoustic-audio-recovery.v1"
)
HISTORY_AUDIO_GATE_INVALID_REASON = "HISTORY_CONVERGENCE_AUDIO_GATE_INVALID"
# A later judge that produced no decision: a provider outage, or the typed
# local retry-admission refusal (never dispatched). Both leave the earlier
# explicit audio request standing.
_NO_LATER_DECISION = frozenset({"JUDGE_UNAVAILABLE", "RETRY_BLOCKED"})
_CYCLE_RECEIPT_SCHEMA = "exact-final-cpa-cycle-adjudication.v1"
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_PINYIN = re.compile(r"^(?:[a-zü]+|\?)$")
_WITNESS_TEXT_CHANNELS = frozenset(
    {
        "candidate_entities",
        "candidate_id",
        "canonical_entity",
        "current_cue",
        "proposed_cue",
        "matched_audio_text",
        "context_before",
        "context_after",
        "suspect",
        "replacement",
        "rewritten_text",
        "current_fit",
        "proposed_fit",
    }
)
_CONVERGENCE_APPLY_CONTRACTS = {
    "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED": {
        "memo_key": "exact_final_cpa_convergence_memo",
        "memo_schema": "exact-final-cpa-convergence-memo.v1",
        "memo_status": "RESOLVED",
        "mutation_basis": "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED",
        "witness_status": "HISTORY_AND_CURRENT_EVIDENCE_BOUND",
        "judge_schema": "exact-final-cpa-convergence-judge.v1",
        "pre_key": "pre_convergence_adjudication",
    },
    "CPA_EXACT_FINAL_CYCLE_CLOSED_SET_APPLY": {
        "memo_key": "exact_final_cpa_cycle_memo",
        "memo_schema": "exact-final-cpa-cycle-memo.v1",
        "memo_status": "LOCKED",
        "mutation_basis": "CPA_EXACT_FINAL_CYCLE_CLOSED_SET_ADJUDICATION",
        "witness_status": "CYCLE_HISTORY_AND_EVIDENCE_BOUND",
        "judge_schema": "exact-final-cpa-cycle-judge.v1",
        "pre_key": "pre_cycle_adjudications",
    },
}
_CONVERGENCE_MUTATION_BASES = frozenset(
    contract["mutation_basis"]
    for contract in _CONVERGENCE_APPLY_CONTRACTS.values()
)
_CONVERGENCE_WITNESS_STATUSES = frozenset(
    contract["witness_status"]
    for contract in _CONVERGENCE_APPLY_CONTRACTS.values()
)
_CONVERGENCE_JUDGE_SCHEMAS = frozenset(
    contract["judge_schema"]
    for contract in _CONVERGENCE_APPLY_CONTRACTS.values()
)


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _digest(value: object) -> str:
    return str(value or "").removeprefix("sha256:")


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_check_request(
    request: object,
    *,
    current: str,
    proposed: str,
    window: tuple[int, int],
) -> bool:
    if not isinstance(request, Mapping):
        return False
    payload = dict(request)
    stored_sha = _digest(payload.pop("request_sha256", None))
    return bool(
        request.get("schema_version")
        == "subtitle-span-acoustic-check-request.v1"
        and len(stored_sha) == 64
        and stored_sha == _sha_json(payload)
        and request.get("current_cue") == current
        and request.get("proposed_cue") == proposed
        and request.get("base_text_sha256") == _sha_text(current)
        and request.get("matched_start_ms") == window[0]
        and request.get("matched_end_ms") == window[1]
    )


def _witness_bound(
    witness: Mapping[str, Any],
    *,
    request_sha256: str,
    reuse_target: Mapping[str, Any] | None,
    bound_request: Mapping[str, Any] | None,
) -> bool:
    """Exact request binding, or (reuse only) the shared G1 same-hearing judgment."""
    if witness.get("request_sha256") == request_sha256:
        return True
    if reuse_target is None:
        return False
    from src.autoslice.acoustic_witness_adjudication import same_hearing
    return same_hearing(witness, reuse_target, bound_request=bound_request)


def _valid_blind_witness(
    witness: Mapping[str, Any],
    *,
    request_sha256: str,
    reuse_target: Mapping[str, Any] | None = None,
    bound_request: Mapping[str, Any] | None = None,
) -> bool:
    """Blind pinyin receipt.  ``reuse_target``/``bound_request`` only for a saved
    hearing reused across requests with its own original request (G1); a fresh
    decision binding passes none and keeps the exact request hash."""
    heard = str(witness.get("heard_pinyin") or "").strip()
    tokens = heard.split()
    uncertain = witness.get("uncertain_positions")
    confidence = witness.get("confidence")
    syllable_count = witness.get("syllable_count")
    return bool(
        witness.get("schema_version")
        == "subtitle-span-acoustic-witness.v1"
        and witness.get("witness_protocol") == BLIND_PINYIN_PROTOCOL
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is True
        and _witness_bound(witness, request_sha256=request_sha256,
                           reuse_target=reuse_target, bound_request=bound_request)
        and bool(tokens)
        and all(_PINYIN.fullmatch(token) for token in tokens)
        and not any(key in witness for key in _WITNESS_TEXT_CHANNELS)
        and not contains_han_text(json.dumps(dict(witness), ensure_ascii=False))
        and isinstance(uncertain, list)
        and all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value < len(tokens)
            for value in uncertain
        )
        and isinstance(syllable_count, int)
        and not isinstance(syllable_count, bool)
        and syllable_count == len(tokens)
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0.0 <= float(confidence) <= 1.0
        and _valid_digest(witness.get("source_media_sha256"))
        and _valid_digest(witness.get("audio_clip_sha256"))
        and _valid_digest(witness.get("prompt_sha256"))
        and _valid_digest(witness.get("response_sha256"))
    )


def _valid_proposed_judge(
    judge: Mapping[str, Any],
    *,
    check_request_sha256: str,
    transcript_proposed: str | None = None,
) -> bool:
    similarities = judge.get("candidate_pinyin_similarity")
    choice_set = judge.get("choice_set")
    return bool(
        judge.get("schema_version") == "acoustic-witness-adjudication.v1"
        and judge.get("status") == "JUDGED"
        and judge.get("choice") == "PROPOSED"
        and judge.get("needs_audio_requested") is not True
        and judge.get("needs_audio_request_unresolved") is not True
        and not (
            "needs_audio_requested" not in judge
            and judge.get("needs_audio") is True
        )
        and (
            judge.get("decision_contract") == "current-proposed-neither.v1"
            if transcript_proposed is None
            else (
                judge.get("decision_contract") == "current-proposed-candidate-id-neither.v1"
                and judge.get("candidate_id") == "PROPOSAL"
                and judge.get("selected_candidate_text") == transcript_proposed
            )
        )
        and isinstance(choice_set, list)
        and all(isinstance(value, str) for value in choice_set)
        and set(choice_set) == {"CURRENT", "PROPOSED", "NEITHER"}
        and _digest(judge.get("check_request_sha256"))
        == check_request_sha256
        and isinstance(judge.get("reason"), str)
        and bool(str(judge.get("reason") or "").strip())
        and isinstance(similarities, Mapping)
        and set(similarities) == {"current", "proposed"}
        and all(
            value is None
            or (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0.0 <= float(value) <= 1.0
            )
            for value in (
                similarities.get("current"),
                similarities.get("proposed"),
            )
        )
        and _valid_digest(judge.get("prompt_sha256"))
        and _valid_digest(judge.get("completion_sha256"))
    )


def _valid_native_witness(
    witness: Mapping[str, Any], *, witness_request: Mapping[str, Any],
    bound_request: Mapping[str, Any] | None = None,
) -> bool:
    """Revalidate native crop provenance, without inventing phonetic support.

    ``bound_request`` (the saved row's own original witness request) only where
    a saved native hearing is reused across requests (G1): the exact request
    hash is then replaced by the shared same-hearing judgment; every crop,
    clock, receipt and byte check below is unchanged."""
    native = witness.get("native_observation")
    if not isinstance(native, Mapping):
        return False
    payload = dict(native)
    receipt_sha = payload.pop("receipt_sha256", None)
    offset = witness_request.get("source_media_timeline_offset_ms")
    start = witness_request.get("matched_start_ms")
    end = witness_request.get("matched_end_ms")
    if not all(type(v) is int for v in (offset, start, end)):
        return False
    physical = (start + offset, end + offset)
    provider = native.get("provider")
    transcript = witness.get("exact_transcript")
    return bool(
        witness.get("schema_version") == "subtitle-span-acoustic-witness.v1"
        and witness.get("witness_protocol") == CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is True
        and witness.get("audibility_basis") == "nonempty_provider_transcript"
        and _witness_bound(witness, request_sha256=str(witness_request.get("request_sha256") or ""),
                           reuse_target=witness_request if bound_request is not None else None,
                           bound_request=bound_request)
        and witness.get("candidate_exposure") == "none"
        and witness.get("authority") == "EVIDENCE_ONLY"
        and witness.get("mutation_authorized") is False
        and not any(key in witness for key in (*_WITNESS_TEXT_CHANNELS, "heard_pinyin", "confidence", "syllable_count"))
        and isinstance(transcript, str) and bool(transcript.strip())
        and native.get("schema_version") == NATIVE_SCHEMA
        and native.get("status") == "OBSERVED"
        and native.get("candidate_exposure") == "none"
        and native.get("authority") == "EVIDENCE_ONLY"
        and native.get("mutation_authorized") is False
        and native.get("crop_is_exact_target") is True
        and native.get("observation_scope") == "complete_exact_cue_no_padding"
        and native.get("transcript") == transcript
        and isinstance(provider, str) and provider in MODELS
        and native.get("model") == MODELS[provider]
        and witness.get("provider") == provider
        and witness.get("model") == native.get("model")
        and receipt_sha == _sha_json(payload)
        and 0 <= physical[0] < physical[1] <= physical[0] + 20_000
        and (native.get("target_start_ms"), native.get("target_end_ms")) == physical
        and (witness.get("audio_start_ms"), witness.get("audio_end_ms")) == physical
        and all(
            _valid_digest(witness.get(outer)) and witness.get(outer) == native.get(inner)
            for outer, inner in (
                ("source_media_sha256", "source_media_sha256"),
                ("audio_clip_sha256", "input_audio_sha256"),
                ("response_sha256", "response_sha256"),
            )
        )
    )


def build_acoustic_witness_binding(
    *,
    check_request: Mapping[str, Any],
    witness: Mapping[str, Any],
    judge: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Freeze all payloads needed to independently revalidate one witness."""

    current = check_request.get("current_cue")
    proposed = check_request.get("proposed_cue")
    start = check_request.get("matched_start_ms")
    end = check_request.get("matched_end_ms")
    if not (
        isinstance(current, str)
        and bool(current)
        and isinstance(proposed, str)
        and bool(proposed.strip())
        and isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end
        and _valid_check_request(
            check_request,
            current=current,
            proposed=proposed,
            window=(start, end),
        )
    ):
        return None
    try:
        witness_request = build_witness_request(check_request)
    except (KeyError, TypeError, ValueError):
        return None
    protocol = witness_protocol(witness)
    check_request_sha256 = _digest(check_request.get("request_sha256"))
    valid_observation = (
        _valid_blind_witness(
            witness, request_sha256=str(witness_request.get("request_sha256") or "")
        ) if protocol == BLIND_PINYIN_PROTOCOL else (
            protocol == CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL
            and _valid_native_witness(witness, witness_request=witness_request)
        )
    )
    if check_request.get("argon_escalation_evidence") is not None or witness.get("argon_escalation_evidence") is not None:
        from src.autoslice.argon_escalation_integration import valid_argon_witness
        valid_observation = valid_argon_witness(check_request, witness)
    if not (
        supported_witness_protocol(witness)
        and valid_observation
        and _valid_proposed_judge(
            judge,
            check_request_sha256=check_request_sha256,
            transcript_proposed=proposed if protocol == CANDIDATE_BLIND_TRANSCRIPT_PROTOCOL else None,
        )
    ):
        return None
    binding: dict[str, Any] = {
        "schema_version": BINDING_SCHEMA,
        "status": "PASS",
        "witness_protocol": protocol,
        "proposed_text_sha256": "sha256:" + _sha_text(proposed),
        "matched_start_ms": start,
        "matched_end_ms": end,
        "check_request": dict(check_request),
        "witness_request": witness_request,
        "witness": dict(witness),
        "judge": dict(judge),
    }
    binding["binding_sha256"] = "sha256:" + _sha_json(binding)
    return binding


def binding_from_adjudication(
    adjudication: object,
) -> dict[str, Any] | None:
    if not isinstance(adjudication, Mapping):
        return None
    request = adjudication.get("request")
    witness = adjudication.get("verdict")
    witness_judge = adjudication.get("witness_judge")
    judge = (
        witness_judge.get("judge")
        if isinstance(witness_judge, Mapping)
        else None
    )
    if not all(isinstance(row, Mapping) for row in (request, witness, judge)):
        return None
    return build_acoustic_witness_binding(
        check_request=request,
        witness=witness,
        judge=judge,
    )


def valid_acoustic_witness_binding(
    binding: object,
    *,
    proposed: str,
    window: tuple[int, int],
) -> bool:
    if not isinstance(binding, Mapping):
        return False
    if binding.get("schema_version") == RANGE_WORD_PROJECTION_BINDING_SCHEMA:
        # Keep the long-standing exact-crop contract intact while allowing
        # generic receipt consumers to dispatch the new, explicitly separate
        # range projection schema to its full validator.
        return valid_range_word_projection_binding(
            binding, proposed=proposed, window=window
        )
    payload = dict(binding)
    binding_sha = payload.pop("binding_sha256", None)
    check_request = binding.get("check_request")
    witness = binding.get("witness")
    judge = binding.get("judge")
    if not all(
        isinstance(row, Mapping)
        for row in (check_request, witness, judge)
    ):
        return False
    rebuilt = build_acoustic_witness_binding(
        check_request=check_request,
        witness=witness,
        judge=judge,
    )
    return bool(
        binding.get("schema_version") == BINDING_SCHEMA
        and binding.get("status") == "PASS"
        and binding.get("proposed_text_sha256")
        == "sha256:" + _sha_text(proposed)
        and binding.get("matched_start_ms") == window[0]
        and binding.get("matched_end_ms") == window[1]
        and binding_sha == "sha256:" + _sha_json(payload)
        and rebuilt == dict(binding)
    )


def _validated_range_observation(
    check_request: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Revalidate a saved range receipt before reading its word projection.

    The normal range validator is deliberately attempted first.  A text-only
    successor has a different whole-SRT hash, so the ordinary carryover
    adapter is then used to validate the immutable receipt against its bound
    SRT and compare the current range geometry.  Neither branch trusts the
    ``native_word_overlap_evidence`` copied into the caller's row.
    """

    current_srt = check_request.get("whole_clip_current_srt")
    if not isinstance(current_srt, str) or not current_srt.strip():
        return None
    try:
        from src.autoslice.independent_context_adjudication import (
            adapt_range_acoustic_context_observation,
            validate_range_acoustic_observation,
        )

        validated, _reason = validate_range_acoustic_observation(
            current_srt, check_request, observation
        )
        if validated is None:
            validated, _reason = adapt_range_acoustic_context_observation(
                current_srt, check_request, observation
            )
    except Exception:
        # The range validators are fail-closed, and a malformed or unreadable
        # receipt must not turn into a generic convergence BLOCK/acceptance
        # shortcut.  Keep this helper total for callers processing old runs.
        return None
    return validated if isinstance(validated, dict) else None


def _range_word_projection(
    validated: Mapping[str, Any],
    *,
    cue_index: int,
    proposed: str,
    window: tuple[int, int],
) -> dict[str, Any] | None:
    """Extract one cue's complete native word spans from a validated MAI row."""

    if (
        validated.get("provider") != "mai"
        or validated.get("authority") != "EVIDENCE_ONLY"
        or validated.get("mutation_authorized") is not False
        or validated.get("candidate_blind") is not True
        or validated.get("current_cue_index") != cue_index
    ):
        return None
    status = validated.get("status")
    original_status = validated.get("original_status")
    if status != "OBSERVED" and original_status != "OBSERVED":
        return None
    scope = str(validated.get("evidence_scope") or "")
    if not scope.startswith("RANGE_CONTEXT_ONLY"):
        return None
    if validated.get("transcript_scope") != "RANGE_WITH_NATIVE_WORD_OVERLAP":
        return None

    range_binding = validated.get("range")
    if not isinstance(range_binding, Mapping):
        broad_request = validated.get("witness_request")
        if not isinstance(broad_request, Mapping):
            broad_request = validated.get("original_witness_request")
        if not isinstance(broad_request, Mapping):
            return None
        range_binding = {
            key: broad_request.get(key)
            for key in (
                "cue_indexes",
                "matched_start_ms",
                "matched_end_ms",
                "context_start_ms",
                "context_end_ms",
                "source_media_timeline_offset_ms",
            )
        }
    range_indexes = range_binding.get("cue_indexes")
    if (
        not isinstance(range_indexes, list)
        or cue_index not in range_indexes
        or any(type(index) is not int for index in range_indexes)
    ):
        return None

    timing = validated.get("native_word_overlap_evidence")
    if not isinstance(timing, Mapping):
        return None
    table = timing.get("table")
    if not isinstance(table, Mapping) or table.get("provider") != "mai":
        return None
    rows = table.get("rows")
    if not isinstance(rows, list) or not rows:
        return None

    target_rows: list[tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = []
    grouped_other: dict[int, list[Mapping[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("cue_spans"), list):
            continue
        row_matches: list[Mapping[str, Any]] = []
        for span in row["cue_spans"]:
            if not isinstance(span, Mapping):
                continue
            number = span.get("n")
            if type(number) is not int:
                continue
            grouped_other.setdefault(number, []).append(span)
            if number == cue_index:
                row_matches.append(span)
        if row_matches:
            target_rows.append((row, row_matches))
    # A single current cue may be split across several native segments.  All
    # of its spans must be consumed; duplicate/ambiguous cue rows are rejected
    # below rather than collapsing them into a guessed transcript.
    if not target_rows:
        return None
    spans = [span for _row, row_spans in target_rows for span in row_spans]
    if not spans:
        return None

    start, end = window
    previous_start = previous_end = None
    seen_spans: set[tuple[int, int, str]] = set()
    texts: list[str] = []
    for span in spans:
        if span.get("n") != cue_index:
            return None
        span_start, span_end = span.get("start_ms"), span.get("end_ms")
        text = span.get("text")
        if (
            type(span_start) is not int
            or type(span_end) is not int
            or not start <= span_start < span_end <= end
            or not isinstance(text, str)
            or not text
        ):
            return None
        span_identity = (span_start, span_end, text)
        if span_identity in seen_spans:
            return None
        seen_spans.add(span_identity)
        if (
            previous_start is not None
            and (span_start < previous_start or span_end < (previous_end or span_start))
        ):
            return None
        previous_start, previous_end = span_start, span_end
        texts.append(text)
    projected_text = "".join(texts)
    try:
        from src.autoslice import acoustic_pinyin

        projected_tokens = acoustic_pinyin.text_pinyin_tokens(projected_text)
        proposed_tokens = acoustic_pinyin.text_pinyin_tokens(proposed)
    except Exception:
        return None
    if (
        projected_tokens is None
        or proposed_tokens is None
        or not projected_tokens
        or not proposed_tokens
        or projected_tokens != proposed_tokens
    ):
        return None
    # An identically heard phrase in a neighbouring cue is ambiguous even
    # when its native text uses a homophone or punctuation variant.  Compare
    # only the provider's native word timing after the exact pinyin contract;
    # never use a majority vote or phonetic reconstruction to fill it in.
    for number, other_spans in grouped_other.items():
        if number == cue_index or not other_spans:
            continue
        other_text = "".join(
            str(span.get("text"))
            for span in other_spans
            if isinstance(span.get("text"), str)
        )
        try:
            other_tokens = acoustic_pinyin.text_pinyin_tokens(other_text)
        except Exception:
            return None
        if other_tokens == proposed_tokens:
            return None
    return {
        "schema_version": "native-word-timed-cue-projection.v1",
        "cue_index": cue_index,
        "matched_start_ms": start,
        "matched_end_ms": end,
        "spans": [dict(span) for span in spans],
        "text": projected_text,
        "text_sha256": "sha256:" + _sha_text(projected_text),
        "proposed_text_sha256": "sha256:" + _sha_text(proposed),
        "text_pinyin_tokens": list(projected_tokens),
        "proposed_text_pinyin_tokens": list(proposed_tokens),
    }


def build_range_word_projection_binding(
    *,
    check_request: Mapping[str, Any],
    range_observation: Mapping[str, Any],
    proposed: str | None = None,
    window: tuple[int, int] | None = None,
) -> dict[str, Any] | None:
    """Bind a native MAI word-timed range projection to one current cue.

    This is intentionally a separate authority from
    :func:`build_acoustic_witness_binding`: the saved ``witness`` remains the
    broad, candidate-blind range observation and ``projection`` contains only
    the independently revalidated word spans that fall inside the current cue.
    """

    if not isinstance(check_request, Mapping) or not isinstance(range_observation, Mapping):
        return None
    current = check_request.get("current_cue")
    request_proposed = check_request.get("proposed_cue")
    start = check_request.get("matched_start_ms")
    end = check_request.get("matched_end_ms")
    cue_indexes = check_request.get("cue_indexes")
    if not (
        isinstance(current, str)
        and bool(current)
        and isinstance(request_proposed, str)
        and bool(request_proposed.strip())
        and type(start) is int
        and type(end) is int
        and 0 <= start < end
        and isinstance(cue_indexes, list)
        and len(cue_indexes) == 1
        and type(cue_indexes[0]) is int
        and cue_indexes[0] >= 1
        and isinstance(check_request.get("whole_clip_current_srt"), str)
        and bool(check_request.get("whole_clip_current_srt", "").strip())
        and _valid_check_request(
            check_request,
            current=current,
            proposed=request_proposed,
            window=(start, end),
        )
    ):
        return None
    if proposed is not None and proposed != request_proposed:
        return None
    if window is not None:
        try:
            if tuple(window) != (start, end):
                return None
        except TypeError:
            return None
    cue_index = cue_indexes[0]

    try:
        witness_request = build_witness_request(check_request)
    except (KeyError, TypeError, ValueError):
        return None
    validated = _validated_range_observation(check_request, range_observation)
    if validated is None:
        return None
    narrow_sha = str(witness_request.get("request_sha256") or "")
    if not _valid_digest(narrow_sha):
        return None
    single_sha = validated.get("single_cue_witness_request_sha256")
    current_narrow = validated.get("current_narrow_witness_request")
    current_narrow_sha = validated.get("current_narrow_witness_request_sha256")
    if single_sha is not None and _digest(single_sha) != narrow_sha:
        return None
    if current_narrow is not None:
        if not isinstance(current_narrow, Mapping) or current_narrow.get("request_sha256") != narrow_sha:
            return None
        if current_narrow_sha is not None and _digest(current_narrow_sha) != narrow_sha:
            return None
    projection = _range_word_projection(
        validated, cue_index=cue_index, proposed=request_proposed, window=(start, end)
    )
    if projection is None:
        return None
    original = validated.get("original_range_observation")
    if not isinstance(original, Mapping):
        original = validated
    range_binding = validated.get("range")
    broad_request = validated.get("witness_request")
    if not isinstance(range_binding, Mapping) and isinstance(broad_request, Mapping):
        range_binding = {
            key: broad_request.get(key)
            for key in (
                "cue_indexes",
                "matched_start_ms",
                "matched_end_ms",
                "context_start_ms",
                "context_end_ms",
                "source_media_timeline_offset_ms",
            )
        }
    if not isinstance(range_binding, Mapping) or not isinstance(broad_request, Mapping):
        broad_request = validated.get("original_witness_request")
    if not isinstance(range_binding, Mapping) or not isinstance(broad_request, Mapping):
        return None
    try:
        range_observation_sha = "sha256:" + _sha_json(range_observation)
        witness_sha = "sha256:" + _sha_json(validated)
        projection_sha = "sha256:" + _sha_json(projection)
    except (TypeError, ValueError):
        return None
    binding: dict[str, Any] = {
        "schema_version": RANGE_WORD_PROJECTION_BINDING_SCHEMA,
        "status": "PASS",
        "witness_protocol": "native_word_timed_range_context",
        "provider": "mai",
        "proposed_text_sha256": "sha256:" + _sha_text(request_proposed),
        "matched_start_ms": start,
        "matched_end_ms": end,
        "current_cue_index": cue_index,
        "check_request": deepcopy(dict(check_request)),
        "witness_request": deepcopy(dict(broad_request)),
        # The raw observation is what the normal range validator replays on
        # every validation.  It is intentionally not relabelled as exact.
        "range_observation": deepcopy(dict(range_observation)),
        "witness": deepcopy(dict(validated)),
        "original_range_observation": deepcopy(dict(original)),
        "projection": projection,
        "range_observation_sha256": range_observation_sha,
        "witness_sha256": witness_sha,
        "projection_sha256": projection_sha,
    }
    try:
        binding["binding_sha256"] = "sha256:" + _sha_json(binding)
    except (TypeError, ValueError):
        return None
    return binding


def valid_range_word_projection_binding(
    binding: object,
    *,
    proposed: str,
    window: tuple[int, int],
) -> bool:
    """Rebuild a range projection from its original receipt and raw files."""

    if not isinstance(binding, Mapping):
        return False
    payload = dict(binding)
    binding_sha = payload.pop("binding_sha256", None)
    request = binding.get("check_request")
    observation = binding.get("range_observation")
    if not isinstance(request, Mapping) or not isinstance(observation, Mapping):
        return False
    try:
        rebuilt = build_range_word_projection_binding(
            check_request=request,
            range_observation=observation,
            proposed=proposed,
            window=window,
        )
    except Exception:
        return False
    return bool(
        binding.get("schema_version") == RANGE_WORD_PROJECTION_BINDING_SCHEMA
        and binding.get("status") == "PASS"
        and binding.get("witness_protocol") == "native_word_timed_range_context"
        and binding.get("provider") == "mai"
        and binding.get("proposed_text_sha256") == "sha256:" + _sha_text(proposed)
        and binding.get("matched_start_ms") == window[0]
        and binding.get("matched_end_ms") == window[1]
        and binding_sha == "sha256:" + _sha_json(payload)
        and rebuilt == dict(binding)
    )


def range_word_projection_binding_from_adjudication(
    adjudication: object,
    *,
    proposed: str | None = None,
    window: tuple[int, int] | None = None,
) -> dict[str, Any] | None:
    """Find one current candidate-blind range receipt for an adjudication."""

    if not isinstance(adjudication, Mapping):
        return None
    request = adjudication.get("request")
    if not isinstance(request, Mapping):
        return None
    # A range projection is evidence for an already selected proposal.  When
    # the adjudication carries the ordinary acoustic judge, reject an
    # explicit/unresolved audio request and any non-PROPOSED choice.  The
    # normalized legacy ``needs_audio=true, needs_audio_requested=false`` is
    # intentionally accepted by the same contract used by exact witnesses.
    witness_judge = adjudication.get("witness_judge")
    judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
    if isinstance(judge, Mapping) and judge.get("schema_version") == "acoustic-witness-adjudication.v1":
        if (
            judge.get("choice") != "PROPOSED"
            or judge.get("needs_audio_requested") is True
            or judge.get("needs_audio_request_unresolved") is True
            or (
                judge.get("needs_audio") is True
                and "needs_audio_requested" not in judge
            )
        ):
            return None
        decision_contract = judge.get("decision_contract")
        if decision_contract == "current-proposed-neither.v1":
            # The original exact-final acoustic judge compares CURRENT and
            # PROPOSED as transcript surfaces, without a candidate identity.
            # Keep that contract intact when the same judge is carried by a
            # range observation.
            transcript_proposed = None
        elif decision_contract == "current-proposed-candidate-id-neither.v1":
            # Candidate-id judges must retain both the selected candidate
            # marker and its exact text; _valid_proposed_judge checks both.
            transcript_proposed = request.get("proposed_cue")
            if not isinstance(transcript_proposed, str):
                return None
        else:
            return None
        if not _valid_proposed_judge(
            judge,
            check_request_sha256=_digest(request.get("request_sha256")),
            transcript_proposed=transcript_proposed,
        ):
            return None
    observations = request.get("context_only_range_acoustic_observations")
    if not isinstance(observations, list):
        return None
    candidates: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        binding = build_range_word_projection_binding(
            check_request=request,
            range_observation=observation,
            proposed=proposed,
            window=window,
        )
        if binding is None:
            continue
        if valid_range_word_projection_binding(
            binding,
            proposed=str(request.get("proposed_cue") or ""),
            window=(
                request.get("matched_start_ms"),
                request.get("matched_end_ms"),
            ),
        ):
            candidates.append(binding)
    return candidates[0] if len(candidates) == 1 else None


def _pass_gate(
    *,
    source: str,
    binding: Mapping[str, Any],
    proposed: str,
    window: tuple[int, int],
    binding_field: str = "acoustic_witness_binding",
) -> dict[str, Any]:
    gate: dict[str, Any] = {
        "schema_version": GATE_SCHEMA,
        "status": "PASS",
        "source": source,
        "witness_protocol": binding.get("witness_protocol"),
        "proposed_text_sha256": "sha256:" + _sha_text(proposed),
        "matched_start_ms": window[0],
        "matched_end_ms": window[1],
        binding_field: dict(binding),
    }
    gate["gate_sha256"] = "sha256:" + _sha_json(gate)
    return gate


def convergence_witness_gate(
    *,
    proposed: str,
    window: tuple[int, int],
    findings: Iterable[Mapping[str, Any]],
    history: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prefer exact evidence, then a strictly projected native range witness."""

    findings = list(findings)
    for finding in findings:
        adjudication = finding.get("exact_release_adjudication")
        binding = binding_from_adjudication(adjudication)
        if (
            binding is not None
            and valid_acoustic_witness_binding(
                binding,
                proposed=proposed,
                window=window,
            )
        ):
            return _pass_gate(
                source="FRESH_OBSERVED",
                binding=binding,
                proposed=proposed,
                window=window,
            )
    # A candidate-blind range receipt may contain native MAI word timing that
    # uniquely projects the selected proposal into this cue.  Keep it behind
    # the exact-crop path and preserve its range identity in a separate field.
    for finding in findings:
        adjudication = finding.get("exact_release_adjudication")
        binding = range_word_projection_binding_from_adjudication(
            adjudication,
            proposed=proposed,
            window=window,
        )
        if (
            binding is not None
            and valid_range_word_projection_binding(
                binding,
                proposed=proposed,
                window=window,
            )
        ):
            return _pass_gate(
                source="FRESH_RANGE_WORD_PROJECTION",
                binding=binding,
                proposed=proposed,
                window=window,
                binding_field=RANGE_WORD_PROJECTION_FIELD,
            )
    for repair in reversed(list(history)):
        binding = repair.get("acoustic_witness_binding")
        if (
            repair.get("after") == proposed
            and valid_acoustic_witness_binding(
                binding,
                proposed=proposed,
                window=window,
            )
        ):
            assert isinstance(binding, Mapping)
            return _pass_gate(
                source="HISTORY_OBSERVED_REUSE",
                binding=binding,
                proposed=proposed,
                window=window,
            )
        range_binding = repair.get(RANGE_WORD_PROJECTION_FIELD)
        if (
            repair.get("after") == proposed
            and valid_range_word_projection_binding(
                range_binding,
                proposed=proposed,
                window=window,
            )
        ):
            assert isinstance(range_binding, Mapping)
            return _pass_gate(
                source="HISTORY_OBSERVED_REUSE",
                binding=range_binding,
                proposed=proposed,
                window=window,
                binding_field=RANGE_WORD_PROJECTION_FIELD,
            )
    return {
        "schema_version": GATE_SCHEMA,
        "status": "BLOCK",
        "reason_code": REQUIRED_REASON,
        "proposed_text_sha256": "sha256:" + _sha_text(proposed),
        "matched_start_ms": window[0],
        "matched_end_ms": window[1],
    }


def valid_convergence_witness_gate(
    gate: object,
    *,
    proposed: str,
    window: tuple[int, int],
) -> bool:
    if not isinstance(gate, Mapping):
        return False
    payload = dict(gate)
    gate_sha = payload.pop("gate_sha256", None)
    exact_binding = gate.get("acoustic_witness_binding")
    range_binding = gate.get(RANGE_WORD_PROJECTION_FIELD)
    source = gate.get("source")
    has_exact = isinstance(exact_binding, Mapping)
    has_range = isinstance(range_binding, Mapping)
    binding_valid = (
        has_exact
        and not has_range
        and valid_acoustic_witness_binding(
            exact_binding,
            proposed=proposed,
            window=window,
        )
    ) or (
        has_range
        and not has_exact
        and valid_range_word_projection_binding(
            range_binding,
            proposed=proposed,
            window=window,
        )
    )
    return bool(
        gate.get("schema_version") == GATE_SCHEMA
        and gate.get("status") == "PASS"
        and isinstance(source, str)
        and source in {
            "FRESH_OBSERVED",
            "FRESH_RANGE_WORD_PROJECTION",
            "HISTORY_OBSERVED_REUSE",
        }
        and gate.get("proposed_text_sha256")
        == "sha256:" + _sha_text(proposed)
        and gate.get("matched_start_ms") == window[0]
        and gate.get("matched_end_ms") == window[1]
        and gate_sha == "sha256:" + _sha_json(payload)
        and binding_valid
    )


def _history_audio_gate_candidate(
    adjudication: object,
    *,
    request: Mapping[str, Any] | None = None,
) -> tuple[str, Mapping[str, Any]] | None:
    if not isinstance(adjudication, Mapping):
        return None
    history_gate = adjudication.get("history_convergence_acoustic_witness")
    if isinstance(history_gate, Mapping):
        marker = (
            adjudication.get("policy_branch") == DOWNGRADE_BRANCH
            or history_gate.get("status") == "BLOCK"
            or history_gate.get("needs_audio") is True
            or history_gate.get("reason_code") == CPA_MORE_AUDIO_REASON
        )
        if marker:
            return "history_convergence_acoustic_witness", history_gate
    cycle_gate = adjudication.get("exact_final_cpa_cycle_adjudication")
    if isinstance(cycle_gate, Mapping):
        marker = (
            adjudication.get("policy_branch") == CPA_MORE_AUDIO_REASON
            or cycle_gate.get("status") == "BLOCK"
            or cycle_gate.get("needs_audio") is True
            or cycle_gate.get("reason_code") == CPA_MORE_AUDIO_REASON
        )
        if marker:
            return "exact_final_cpa_cycle_adjudication", cycle_gate
    missing_gate = adjudication.get("cpa_missing_proposal_convergence")
    # A settled (RESOLVED) convergence is no audio request; when the later
    # ordinary judge owns JUDGE_REQUESTS_MORE_AUDIO, that judge is the gate
    # (41/SUB-20: a real CPA needs_audio must reach the recovery owner).
    if isinstance(missing_gate, Mapping) and (
        missing_gate.get("needs_audio") is True
        or (adjudication.get("policy_branch") == "JUDGE_REQUESTS_MORE_AUDIO"
            and missing_gate.get("status") != "RESOLVED")
    ):
        saved_request = request
        if saved_request is None:
            saved_request = adjudication.get("request")
        if _valid_applied_successor_supersedes_missing_gate(
            adjudication,
            request=saved_request if isinstance(saved_request, Mapping) else None,
        ):
            # The nested candidate-less receipt is historical.  A later
            # normal CPA candidate with the existing legacy default
            # (needs_audio_requested=false) owns the current repair; it must
            # not be re-routed into semantic range recovery.
            return None
        # Other incomplete or drifted successors stay on the existing
        # fail-closed candidate path.
        return "cpa_missing_proposal_convergence", missing_gate
    witness_judge = adjudication.get("witness_judge")
    judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
    text_first = adjudication.get("text_first_judge")
    if (isinstance(text_first, Mapping) and text_first.get("needs_audio_requested") is True
            and isinstance(judge, Mapping) and judge.get("status") in _NO_LATER_DECISION):
        # A failed later call supplies no new decision. Retain the earlier
        # explicit audio request on its original complete candidate request.
        return "ordinary_text_first_judge", text_first
    if isinstance(judge, Mapping) and (
        judge.get("needs_audio_requested") is True
        or adjudication.get("policy_branch") == "JUDGE_REQUESTS_MORE_AUDIO"
    ):
        return "ordinary_witness_judge", judge
    recorder_gate = adjudication.get("recorder_needs_audio_gate")
    if isinstance(recorder_gate, Mapping):
        # A K04-stopped target whose own completed raw answer asked for audio.
        return RECORDER_GATE_SOURCE, recorder_gate
    return None


def _valid_ordinary_judge_audio_gate(
    gate: Mapping[str, Any], request: Mapping[str, Any] | None,
) -> bool:
    """Read the ordinary judge's explicit request as collection authority only.

    A legacy default ``needs_audio=true`` is insufficient.  Preserve the real
    judge and its complete original request; never synthesize a history/cycle
    receipt or use its candidate preference as a terminal text decision.
    """
    if not isinstance(request, Mapping):
        return False
    return bool(
        gate.get("schema_version") == "acoustic-witness-adjudication.v1"
        and gate.get("status") == "JUDGED"
        and gate.get("needs_audio") is True
        and gate.get("needs_audio_requested") is True
        and isinstance(gate.get("reason"), str) and bool(gate["reason"].strip())
        and all(_valid_digest(gate.get(key)) for key in (
            "prompt_sha256", "completion_sha256", "check_request_sha256",
        ))
        and _digest(gate.get("check_request_sha256")) == _digest(request.get("request_sha256"))
        and _valid_question_request(request)
    )


def _valid_question_request(request: object) -> bool:
    """A complete, self-hashing single-cue check request (no gate involved)."""
    if not isinstance(request, Mapping):
        return False
    current, proposed = request.get("current_cue"), request.get("proposed_cue")
    start, end = request.get("matched_start_ms"), request.get("matched_end_ms")
    indexes = request.get("cue_indexes")
    return bool(
        isinstance(current, str) and bool(current.strip())
        and isinstance(proposed, str)
        and type(start) is int and type(end) is int and 0 <= start < end
        and type(request.get("source_media_timeline_offset_ms")) is int
        and request.get("source_media_timeline_offset_ms") >= 0
        and isinstance(indexes, list) and len(indexes) == 1
        and type(indexes[0]) is int and indexes[0] >= 1
        and isinstance(request.get("whole_clip_current_srt"), str)
        and bool(request["whole_clip_current_srt"].strip())
        and _valid_check_request(request, current=current, proposed=proposed, window=(start, end))
    )


def _ordinary_gate_carries(
    gate: Mapping[str, Any],
    own_request: Mapping[str, Any] | None,
    current_request: Mapping[str, Any] | None,
) -> tuple[bool, dict[str, Any]]:
    """An ordinary judge's needs-audio answer, bound to the request it answered,
    is collection authority for ``current_request`` when both ask the same audio
    question (G6 ``same_audio_question``; admin ruling mode fields
    such as ``require_complete_utterance_support`` are recorded, not identity;
    any text change still needs the current judge under the current mode)."""
    if not _valid_ordinary_judge_audio_gate(gate, own_request):
        return False, {}
    if current_request is None or current_request is own_request or (
            _digest(current_request.get("request_sha256")) == _digest(own_request.get("request_sha256"))):
        return True, {}
    if not _valid_question_request(current_request):
        return False, {}
    from src.autoslice.audio_question_identity import same_audio_question
    return same_audio_question(own_request, current_request, compare_proposed=True)


RECORDER_GATE_SOURCE = "recorder_original_needs_audio"
_RECORDER_GATE_KEYS = frozenset({
    "schema_version", "status", "needs_audio", "issue_sha256", "physical_target", "row_relpath",
    "row_content_sha256", "row_request_sha256", "completion_sha256", "stop_reason", "reason",
    "cue_grid_sha256", "original_request", "authority", "mutation_authorized", "gate_sha256",
})


def _valid_recorder_needs_audio_gate(
    gate: Mapping[str, Any], request: Mapping[str, Any] | None,
) -> bool:
    """Re-open a recorder-backed pending-audio obligation (equal to a convergence gate).

    The gate cites the target's own COMPLETED raw CPA answer. Every binding is
    replayed from the original row file: its content sha256 (evidence_identity),
    the recorder's request digest and the raw answer bytes, the physical target
    (recorder source identity + source clock), the cue grid and the sealed
    candidate-less request. The raw answer must itself ask for audio. Any
    STARTED/ERROR/UNKNOWN row, drift or unreadable original makes it invalid.
    """
    import os
    from pathlib import Path
    from src.autoslice.evidence_identity import regular_file_sha256
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.llm_client import extract_json_object
    from src.autoslice.semantic_retry_progress import RECORDER_GATE_SCHEMA, RECORDER_ROOT, digest
    try:
        if not (isinstance(gate, Mapping) and set(gate) == _RECORDER_GATE_KEYS
                and gate["schema_version"] == RECORDER_GATE_SCHEMA and gate["status"] == "BLOCK"
                and gate["needs_audio"] is True and gate["mutation_authorized"] is False
                and gate["authority"] == "COLLECTION_ONLY"
                and gate["stop_reason"] in {"CONTINUATION_VALUE_PLAN_REQUIRED",
                                            "CONTINUATION_PLAN_ALREADY_UNRESOLVED"}
                and isinstance(gate["reason"], str) and gate["reason"].strip()
                and gate["gate_sha256"] == digest({k: v for k, v in gate.items() if k != "gate_sha256"})):
            return False
        original = gate["original_request"]
        unsigned = {k: v for k, v in original.items() if k != "request_sha256"}
        if _digest(original.get("request_sha256")) != digest(unsigned):
            return False
        target = gate["physical_target"]
        start, end, offset = (original.get(k) for k in (
            "matched_start_ms", "matched_end_ms", "source_media_timeline_offset_ms"))
        if (any(type(v) is not int for v in (start, end, offset))
                or (target["start_ms"], target["end_ms"]) != (start + offset, end + offset)
                or gate["issue_sha256"] != digest({"source": target["source"], "start_ms": target["start_ms"],
                                                    "end_ms": target["end_ms"]})):
            return False
        pieces = target["source"].get("pieces") if isinstance(target["source"], Mapping) else None
        recorded = sorted({str(p.get("source_media_sha256")).removeprefix("sha256:")
                           for p in pieces or [] if isinstance(p, Mapping) and p.get("source_media_sha256")})
        bound = sorted(str(x).removeprefix("sha256:") for x in
                       (original.get("clip_context_binding") or {}).get("source_media_sha256s") or [])
        if pieces is not None and recorded != bound:
            return False
        cues = [c for c in parse_srt_cues(str(original.get("whole_clip_current_srt") or "")) if c.text.strip()]
        indexes = original.get("cue_indexes")
        if (gate["cue_grid_sha256"] != digest([[c.start_ms, c.end_ms] for c in cues])
                or not isinstance(indexes, list) or len(indexes) != 1 or type(indexes[0]) is not int
                or not 1 <= indexes[0] <= len(cues)
                or (cues[indexes[0] - 1].start_ms, cues[indexes[0] - 1].end_ms) != (start, end)
                or cues[indexes[0] - 1].text != original.get("current_cue")):
            return False
        base = os.environ.get("AUTOSLICE_BASE")
        if not base:
            return False
        expected = RECORDER_ROOT / gate["issue_sha256"] / (gate["row_request_sha256"] + ".json")
        if Path(gate["row_relpath"]) != expected:
            return False
        path = Path(base) / expected
        if regular_file_sha256(path) != gate["row_content_sha256"]:
            return False
        # K22 is per request (41:「不完整 STARTED 保留未知，不盲重发。」): only
        # the cited row must be COMPLETED; an outstanding sibling request keeps
        # its UNKNOWN and is never re-sent, but does not close this gate.
        row = json.loads(path.read_bytes())
        completion = row.get("completion")
        if not (row.get("status") == "COMPLETED" and row.get("issue_sha256") == gate["issue_sha256"]
                and row.get("request_sha256") == gate["row_request_sha256"] == digest(row.get("request"))
                and isinstance(completion, str)
                and row.get("completion_sha256") == gate["completion_sha256"] == _sha_text(completion)):
            return False
        payload = extract_json_object(completion)
        if not (isinstance(payload, Mapping) and payload.get("needs_audio") is True):
            return False
        if request is not None:
            # Same audio question (G6): cue text, physical target and source;
            # cue ordinals and the local clock are representation.
            from src.autoslice.audio_question_identity import same_audio_question
            if not same_audio_question(original, request, compare_proposed=False)[0]:
                return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError, OSError):
        return False


def _valid_missing_proposal_audio_gate(
    gate: Mapping[str, Any], request: Mapping[str, Any] | None,
) -> bool:
    """Read an explicit candidate-less CPA request without inventing a gate.

    Unlike a history/cycle receipt, this ordinary receipt uses UNRESOLVED and
    has no judge_served_from_cache field on a fresh call. Its original schema,
    decision, full SRT and cue bindings remain intact; it grants collection only.
    A proposal re-ask (bootstrap) answer is accepted only through its
    recorder-backed raw row (``_valid_recorder_needs_audio_gate``).
    """
    from src.autoslice.semantic_retry_progress import RECORDER_GATE_SCHEMA
    if isinstance(gate, Mapping) and gate.get("schema_version") == RECORDER_GATE_SCHEMA:
        return _valid_recorder_needs_audio_gate(gate, request)
    if not (
        gate.get("schema_version") == "subtitle-missing-proposal-cpa-convergence.v1"
        and gate.get("status") == "UNRESOLVED"
        and gate.get("decision") == "UNRESOLVED"
        and gate.get("needs_audio") is True
        and gate.get("decision_authority") == "CPA_JUDGE"
        and gate.get("mutation_authorized") is False
        and gate.get("timing_immutable") is True
        and gate.get("reason_code") in {
            "CPA_CONVERGENCE_UNRESOLVED", "CPA_CONVERGENCE_KEEP_REQUIRES_AUDIO",
        }
        and isinstance(gate.get("reason"), str) and bool(gate["reason"].strip())
        and all(_valid_digest(gate.get(key)) for key in (
            "prompt_sha256", "completion_sha256", "context_sha256",
            "current_cue_sha256", "final_srt_sha256",
        ))
        and ("served_from_cache" not in gate or type(gate["served_from_cache"]) is bool)
    ):
        return False
    if request is None:
        return True
    current, proposed = request.get("current_cue"), request.get("proposed_cue")
    whole = request.get("whole_clip_current_srt")
    return bool(
        isinstance(current, str) and isinstance(proposed, str)
        # Original candidate-less receipts legitimately bind an empty proposal.
        # This gate grants collection only; the original request digest, cue and
        # complete SRT must still match.  Never invent a repair to obtain audio.
        and bool(current.strip()) and (proposed == "" or bool(proposed.strip()))
        and isinstance(whole, str)
        and bool(whole.strip())
        and request.get("cue_indexes") == [gate.get("cue_index")]
        and type(request.get("matched_start_ms")) is int
        and type(request.get("matched_end_ms")) is int
        and request.get("matched_start_ms") < request.get("matched_end_ms")
        and type(request.get("source_media_timeline_offset_ms")) is int
        and request.get("source_media_timeline_offset_ms") >= 0
        and _digest(gate.get("current_cue_sha256")) == _sha_text(current)
        and _digest(gate.get("final_srt_sha256")) == _sha_text(whole)
        and _valid_check_request(request, current=current, proposed=proposed,
                                 window=(request.get("matched_start_ms"),
                                         request.get("matched_end_ms")))
    )


def _valid_applied_successor_supersedes_missing_gate(
    adjudication: Mapping[str, Any],
    *,
    request: Mapping[str, Any] | None,
) -> bool:
    """Validate a normal applied successor before retiring an old gate.

    A retained candidate-less gate must not shadow a later ordinary CPA
    candidate.  The ordinary validator deliberately accepts the legacy
    ``needs_audio=true, needs_audio_requested=false`` default; only an
    explicit request marker (``true`` or unresolved) keeps that candidate from
    superseding the historical gate.
    """
    bound_request = request
    if bound_request is None:
        saved_request = adjudication.get("request")
        bound_request = saved_request if isinstance(saved_request, Mapping) else None
    if not isinstance(bound_request, Mapping):
        return False
    prior_request = adjudication.get("request")
    if not isinstance(prior_request, Mapping):
        return False
    # A fresh caller request may be re-created, but it must be the exact
    # request identity that produced this applied witness.  Field-by-field
    # checks below still reject a tampered payload carrying the old digest.
    if _digest(prior_request.get("request_sha256")) != _digest(
        bound_request.get("request_sha256")
    ):
        return False
    current = bound_request.get("current_cue")
    proposed = bound_request.get("proposed_cue")
    start = bound_request.get("matched_start_ms")
    end = bound_request.get("matched_end_ms")
    if not (
        isinstance(current, str)
        and bool(current.strip())
        and isinstance(proposed, str)
        and bool(proposed.strip())
        and type(start) is int
        and type(end) is int
        and 0 <= start < end
    ):
        return False
    cue_indexes = bound_request.get("cue_indexes")
    if not (
        isinstance(cue_indexes, list)
        and len(cue_indexes) == 1
        and type(cue_indexes[0]) is int
        and cue_indexes[0] >= 1
    ):
        return False
    if not (
        adjudication.get("schema_version") == "subtitle-span-adjudication.v1"
        and adjudication.get("status") == "OBSERVED"
        and adjudication.get("repaired") is True
        and adjudication.get("timing_immutable") is True
        and adjudication.get("decision_authority") == "CPA_JUDGE"
        and adjudication.get("witness_authority") == "EVIDENCE_ONLY"
        and adjudication.get("policy_branch")
        == "CPA_JUDGE_APPLY_TRANSCRIPT_CANDIDATE"
    ):
        return False
    mutation = adjudication.get("mutation_authority")
    if not (
        isinstance(mutation, Mapping)
        and mutation.get("schema_version")
        == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
        and isinstance(mutation.get("basis"), str)
        and bool(mutation.get("basis", "").strip())
    ):
        return False
    witness_judge = adjudication.get("witness_judge")
    judge = (
        witness_judge.get("judge")
        if isinstance(witness_judge, Mapping)
        else None
    )
    if not isinstance(judge, Mapping):
        return False
    request_sha = _digest(bound_request.get("request_sha256"))
    if len(request_sha) != 64:
        return False
    if not _valid_proposed_judge(
        judge,
        check_request_sha256=request_sha,
        transcript_proposed=proposed,
    ):
        return False
    # Retiring a candidate-less gate is a normal applied mutation decision.
    # Reuse the same witness binding that the finalizer uses; the outer
    # mutation/status fields alone are insufficient because a stale witness,
    # request, or native crop could otherwise make the old gate disappear.
    binding = binding_from_adjudication(adjudication)
    if not valid_acoustic_witness_binding(
        binding,
        proposed=proposed,
        window=(start, end),
    ):
        return False
    if not valid_convergence_mutation_authority(
        adjudication,
        proposed=proposed,
        window=(start, end),
    ):
        return False
    # Use the same downstream full-cue resolver that the normal finalizer
    # consumes.  Supplying the bound current SRT here makes the supersession
    # decision depend on the actual cue/request/SRT contract, rather than on a
    # second, weaker copy of the mutation rules.
    try:
        from src.autoslice.final_review_carryover import (
            adjudicated_proposed_full_cue,
        )

        proposed_full_cue = adjudicated_proposed_full_cue(
            {
                "cue_index": cue_indexes[0],
                "base_text_sha256": bound_request.get("base_text_sha256"),
                "proposed_full_cue": None,
                "repair_class": bound_request.get("repair_class") or "phonetic",
                "exact_release_adjudication": adjudication,
            },
            srt_text=bound_request.get("whole_clip_current_srt"),
        )
    except (KeyError, TypeError, ValueError):
        return False
    if proposed_full_cue != proposed:
        return False
    missing_gate = adjudication.get("cpa_missing_proposal_convergence")
    return (
        isinstance(missing_gate, Mapping)
        and _valid_missing_proposal_audio_gate(missing_gate, bound_request)
        and _history_audio_prior_request_matches(adjudication, bound_request)
    )


def _history_audio_request_matches(
    gate: Mapping[str, Any],
    request: Mapping[str, Any] | None,
) -> bool:
    if request is None:
        return True
    for key in ("matched_start_ms", "matched_end_ms"):
        if key in gate and gate.get(key) != request.get(key):
            return False
    current_sha = gate.get("current_cue_sha256")
    if current_sha is not None:
        current = request.get("current_cue")
        if not isinstance(current, str) or _digest(current_sha) != _sha_text(current):
            return False
    proposed_sha = gate.get("proposed_text_sha256")
    if proposed_sha is not None:
        proposed = request.get("proposed_cue")
        if not isinstance(proposed, str) or _digest(proposed_sha) != _sha_text(proposed):
            return False
    return True


def _history_audio_prior_request_matches(
    adjudication: Mapping[str, Any],
    request: Mapping[str, Any] | None,
) -> bool:
    """Check old request bindings without reusing the old request payload.

    Same audio question (G6): current/proposed/base text, the physical target
    (local window + its own offset) and the clip/source identity; the local
    clock representation is not identity."""
    prior = adjudication.get("request")
    if request is None or not isinstance(prior, Mapping):
        return True
    from src.autoslice.audio_question_identity import same_audio_question
    return bool(
        prior.get("schema_version")
        == "subtitle-span-acoustic-check-request.v1"
        and same_audio_question(prior, request, compare_proposed=True)[0]
    )


def _invalid_history_audio_recovery(
    *,
    source: str,
    reason: str = HISTORY_AUDIO_GATE_INVALID_REASON,
) -> dict[str, Any]:
    return {
        "schema_version": HISTORY_AUDIO_RECOVERY_SCHEMA,
        "status": "INVALID",
        "reason_code": reason,
        "gate_source": source,
    }


def _executed_judgment_audio_recovery(adjudication, request=None):
    """Read a still-pending original judgment after a sibling did not dispatch.

    The history is verified as persisted, and grants collection only. Never
    revive it after a real later decision or an applied mutation.
    """
    if not isinstance(adjudication, Mapping) or adjudication.get("repaired") is not False:
        return None
    later = (adjudication.get("witness_judge") or {}).get("judge") or {}
    if later.get("status") not in {None, *_NO_LATER_DECISION, "JUDGE_NO_NEW_EVIDENCE"}:
        return None
    if (adjudication.get("mutation_authority") or {}).get("status") == "PASS":
        return None
    history = adjudication.get("executed_span_judgments")
    current = request if request is not None else adjudication.get("request")
    if not isinstance(history, Mapping) or not isinstance(current, Mapping):
        return None
    from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
    try:
        validate_persisted_judge_scopes({"executed_span_judgments_required": True,
                                       "executed_span_judgments": history})
        if not _valid_check_request(current, current=current.get("current_cue"),
                proposed=current.get("proposed_cue"), window=(current.get("matched_start_ms"), current.get("matched_end_ms"))):
            return None
        for execution in reversed(history["expected_executions"]):
            judge = history["outcomes"][execution["execution_id"]]
            compiled = judge.get("span_compilation") or {}
            original = compiled.get("request") or {}
            from src.autoslice.audio_question_identity import same_audio_question
            if (not _valid_ordinary_judge_audio_gate(judge, original)
                    # Collection only (docstring above): the G6 question with the
                    # candidate; a rebuilt whole-clip context is not a new question.
                    or not same_audio_question(original, current, compare_proposed=True,
                                               strict_source=True)[0]
                    or original.get("span_termination_source_sha256") != current.get("span_termination_source_sha256")):
                continue
            return {"schema_version": HISTORY_AUDIO_RECOVERY_SCHEMA, "status": "BLOCK",
                    "reason_code": REQUIRED_REASON, "cpa_reason_code": CPA_MORE_AUDIO_REASON,
                    "needs_audio": True, "gate_source": "executed_span_judgments",
                    "gate": deepcopy(judge), "original_request": deepcopy(original),
                    "execution_id": execution["execution_id"], "history_sha256": history["history_sha256"],
                    "matched_start_ms": current["matched_start_ms"], "matched_end_ms": current["matched_end_ms"]}
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        return _invalid_history_audio_recovery(source="executed_span_judgments")
    return None


def history_audio_recovery_from_adjudication(
    adjudication: object,
    *,
    request: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Extract a real CPA ``needs_audio`` gate for a fresh witness request.

    The saved exact-final request is evidence about the old decision.  This
    helper returns only a typed recovery envelope; callers must bind that
    envelope to the newly built current-SRT request before dispatching.  An
    unresolved candidate-less CPA request stays BLOCK, while a later ordinary
    candidate may supersede that historical gate only after its complete
    request, witness, and mutation bindings validate.  An invalid/uncertain
    gate cannot be silently downgraded to an ordinary text-first decision.
    """
    candidate = _history_audio_gate_candidate(adjudication, request=request)
    if candidate is None:
        return _executed_judgment_audio_recovery(adjudication, request=request)
    source, gate = candidate
    if (source in {"ordinary_text_first_judge", "ordinary_witness_judge"}
            and isinstance(adjudication, Mapping)
            and isinstance(gate.get("span_compilation"), Mapping)):
        # The text-first gate can legitimately bind the pre-runtime request.
        # Do not replay it under a different hash; a verified original execution
        # for the current physical question may own recovery instead.
        bound = request if request is not None else adjudication.get("request")
        if isinstance(bound, Mapping) and gate.get("check_request_sha256") != bound.get("request_sha256"):
            from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
            try:
                validate_persisted_judge_scopes({"judge": gate})
                fallback = _executed_judgment_audio_recovery(adjudication, request=bound)
            except (ValueError, TypeError, KeyError, AttributeError, OSError):
                fallback = None
            if fallback is not None:
                return fallback
    if not isinstance(adjudication, Mapping):
        return _invalid_history_audio_recovery(source=source)

    if source in {"ordinary_witness_judge", "ordinary_text_first_judge"}:
        original_request = adjudication.get("request")
        bound_request = request if request is not None else original_request
        later = adjudication.get("witness_judge")
        later_judge = later.get("judge") if isinstance(later, Mapping) else None
        text_first_pending = (
            source == "ordinary_text_first_judge"
            and isinstance(later_judge, Mapping)
            and later_judge.get("status") in _NO_LATER_DECISION
            and adjudication.get("policy_branch") in {
                "JUDGE_REQUESTS_MORE_AUDIO", "JUDGE_UNCERTAIN_KEEP_CURRENT",
            }
            and not (isinstance(adjudication.get("mutation_authority"), Mapping)
                     and adjudication["mutation_authority"].get("status") == "PASS")
        )
        scoped_pending = False
        if source == "ordinary_witness_judge" and adjudication.get("policy_branch") in {
            "SPAN_LOCALIZATION_UNLOCATABLE", "SPAN_LOCALIZATION_RESULT_REQUIRED",
        }:
            from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
            try:
                scoped_pending = (validate_persisted_judge_scopes(gate) > 0
                    and (gate.get("span_compilation") or {}).get("blocked_reason")
                        == adjudication.get("policy_branch"))
            except (ValueError, TypeError, KeyError):
                scoped_pending = False
        if not (
            adjudication.get("schema_version") == "subtitle-span-adjudication.v1"
            and adjudication.get("status") in {"OBSERVED", "UNCERTAIN"}
            and adjudication.get("repaired") is False
            and adjudication.get("timing_immutable") is True
            and adjudication.get("decision_authority") == "CPA_JUDGE"
            and adjudication.get("witness_authority") == "EVIDENCE_ONLY"
            and (text_first_pending or (
                source == "ordinary_witness_judge"
                and (adjudication.get("policy_branch") == "JUDGE_REQUESTS_MORE_AUDIO" or scoped_pending)))
        ):
            return _invalid_history_audio_recovery(source=source)
        carries, question_deltas = _ordinary_gate_carries(gate, original_request, bound_request)
        if not carries:
            return _invalid_history_audio_recovery(source=source)
        envelope = {
            "schema_version": HISTORY_AUDIO_RECOVERY_SCHEMA, "status": "BLOCK",
            "reason_code": REQUIRED_REASON, "cpa_reason_code": CPA_MORE_AUDIO_REASON,
            "needs_audio": True, "gate_source": source, "gate": dict(gate),
            "matched_start_ms": bound_request.get("matched_start_ms"),
            "matched_end_ms": bound_request.get("matched_end_ms"),
        }
        if question_deltas:
            # Carried across requests by the same audio question (G6): keep the
            # request the gate answered and what differed, never re-sign it.
            envelope.update(original_request=deepcopy(dict(original_request)),
                            question_deltas=question_deltas)
        return envelope

    if source == RECORDER_GATE_SOURCE:
        mutation = adjudication.get("mutation_authority")
        if not (
            adjudication.get("schema_version") == "subtitle-span-adjudication.v1"
            and adjudication.get("repaired") is False
            and adjudication.get("status") in {"MISSING_PROPOSAL_UNRESOLVED", "UNCERTAIN"}
            and not (isinstance(mutation, Mapping) and mutation.get("status") == "PASS")
            and _valid_recorder_needs_audio_gate(gate, request)
        ):
            return _invalid_history_audio_recovery(source=source)
        original = gate["original_request"]
        return {
            "schema_version": HISTORY_AUDIO_RECOVERY_SCHEMA, "status": "BLOCK",
            "reason_code": REQUIRED_REASON, "cpa_reason_code": CPA_MORE_AUDIO_REASON,
            "needs_audio": True, "gate_source": source, "gate": dict(gate),
            "original_request": dict(original),
            "matched_start_ms": original.get("matched_start_ms"),
            "matched_end_ms": original.get("matched_end_ms"),
        }

    if source == "cpa_missing_proposal_convergence":
        if not (
            adjudication.get("status") == "UNCERTAIN"
            and adjudication.get("repaired") is False
            and adjudication.get("decision_authority") == "CPA_JUDGE"
            and adjudication.get("policy_branch") == "JUDGE_REQUESTS_MORE_AUDIO"
            and _valid_missing_proposal_audio_gate(gate, request)
            and _history_audio_prior_request_matches(adjudication, request)
        ):
            return _invalid_history_audio_recovery(source=source)
        envelope = {
            "schema_version": HISTORY_AUDIO_RECOVERY_SCHEMA, "status": "BLOCK",
            "reason_code": REQUIRED_REASON, "cpa_reason_code": CPA_MORE_AUDIO_REASON,
            "needs_audio": True, "gate_source": source, "gate": dict(gate),
        }
        if request is not None:
            envelope.update(matched_start_ms=request.get("matched_start_ms"),
                            matched_end_ms=request.get("matched_end_ms"))
        return envelope

    if source == "history_convergence_acoustic_witness":
        top_valid = (
            adjudication.get("status") == "UNCERTAIN"
            and adjudication.get("repaired") is False
            and adjudication.get("reason_code") == REQUIRED_REASON
            and adjudication.get("policy_branch") == DOWNGRADE_BRANCH
            and adjudication.get("decision_authority") == "CPA_PROPOSAL_ONLY"
            and adjudication.get("witness_authority")
            == "ACOUSTIC_WITNESS_REQUIRED"
        )
        gate_schema_valid = gate.get("schema_version") == GATE_SCHEMA
        prompt_sha = gate.get("judge_prompt_sha256")
        completion_sha = gate.get("judge_completion_sha256")
    else:
        top_policy = adjudication.get("policy_branch")
        top_decision_authority = adjudication.get("decision_authority")
        if top_policy is None:
            top_policy = gate.get("reason_code")
        # Older _attach_cycle_block retained the preceding ordinary judge's
        # CPA_JUDGE label while attaching a genuine CPA_JUDGE_REQUIRED gate.
        # Read that saved receipt without rewriting it.  The nested gate still
        # has to pass every needs_audio, digest, geometry and request check;
        # this compatibility grants evidence collection only, never a repair.
        if top_decision_authority in (None, "CPA_JUDGE"):
            top_decision_authority = gate.get("decision_authority")
        top_valid = (
            adjudication.get("repaired", False) is False
            and top_policy == CPA_MORE_AUDIO_REASON
            and top_decision_authority == "CPA_JUDGE_REQUIRED"
        )
        gate_schema_valid = gate.get("schema_version") == _CYCLE_RECEIPT_SCHEMA
        prompt_sha = gate.get("prompt_sha256")
        completion_sha = gate.get("judge_completion_sha256")

    original_reason = gate.get("reason_code")
    if original_reason == REQUIRED_REASON:
        original_reason = gate.get("cpa_reason_code")
    gate_valid = (
        top_valid
        and gate_schema_valid
        and gate.get("status") == "BLOCK"
        and gate.get("needs_audio") is True
        and original_reason == CPA_MORE_AUDIO_REASON
        and isinstance(gate.get("reason"), str)
        and bool(gate.get("reason", "").strip())
        and isinstance(gate.get("judge_served_from_cache"), bool)
        and _valid_digest(prompt_sha)
        and _valid_digest(completion_sha)
        and _history_audio_request_matches(gate, request)
        and _history_audio_prior_request_matches(adjudication, request)
    )
    if not gate_valid:
        return _invalid_history_audio_recovery(source=source)

    envelope: dict[str, Any] = {
        "schema_version": HISTORY_AUDIO_RECOVERY_SCHEMA,
        "status": "BLOCK",
        # Keep a canonical history reason at the recovery layer while the
        # nested gate retains the original CPA_REQUESTS_MORE_AUDIO reason.
        "reason_code": REQUIRED_REASON,
        "cpa_reason_code": CPA_MORE_AUDIO_REASON,
        "needs_audio": True,
        "gate_source": source,
        "gate": dict(gate),
    }
    if request is not None:
        envelope.update(
            matched_start_ms=request.get("matched_start_ms"),
            matched_end_ms=request.get("matched_end_ms"),
        )
    return envelope


def valid_history_audio_recovery(
    recovery: object,
    *,
    request: Mapping[str, Any],
) -> bool:
    """Validate a recovery envelope against the fresh current-SRT request."""
    if not isinstance(recovery, Mapping):
        return False
    gate = recovery.get("gate")
    if not isinstance(gate, Mapping):
        return False
    if recovery.get("gate_source") in {"ordinary_witness_judge", "ordinary_text_first_judge"}:
        original = recovery.get("original_request")
        gate_valid = _valid_ordinary_judge_audio_gate(gate, request) or (
            isinstance(original, Mapping)
            and _ordinary_gate_carries(gate, original, request)[0]
        )
        return bool(
            recovery.get("schema_version") == HISTORY_AUDIO_RECOVERY_SCHEMA
            and recovery.get("status") == "BLOCK"
            and recovery.get("reason_code") == REQUIRED_REASON
            and recovery.get("cpa_reason_code") == CPA_MORE_AUDIO_REASON
            and recovery.get("needs_audio") is True
            and recovery.get("matched_start_ms") == request.get("matched_start_ms")
            and recovery.get("matched_end_ms") == request.get("matched_end_ms")
            and gate_valid
        )
    typed_envelope = bool(
        recovery.get("schema_version") == HISTORY_AUDIO_RECOVERY_SCHEMA
        and recovery.get("status") == "BLOCK"
        and recovery.get("reason_code") == REQUIRED_REASON
        and recovery.get("cpa_reason_code") == CPA_MORE_AUDIO_REASON
        and recovery.get("needs_audio") is True
    )
    if recovery.get("gate_source") == RECORDER_GATE_SOURCE:
        # 41 §4 : the recorder original needs-audio gate is the same
        # strength as a convergence receipt and owns the same recovery, so its
        # envelope must validate here too (no-gain proof, dispatch guard).  The
        # gate re-reads its own row file and matches the question (G6).
        original = recovery.get("original_request")
        return bool(
            typed_envelope and isinstance(original, Mapping)
            and original == gate.get("original_request")
            and recovery.get("matched_start_ms") == original.get("matched_start_ms")
            and recovery.get("matched_end_ms") == original.get("matched_end_ms")
            and _valid_recorder_needs_audio_gate(gate, request)
        )
    if recovery.get("gate_source") == "cpa_missing_proposal_convergence":
        return bool(
            recovery.get("schema_version") == HISTORY_AUDIO_RECOVERY_SCHEMA
            and recovery.get("status") == "BLOCK"
            and recovery.get("reason_code") == REQUIRED_REASON
            and recovery.get("cpa_reason_code") == CPA_MORE_AUDIO_REASON
            and recovery.get("needs_audio") is True
            and recovery.get("matched_start_ms") == request.get("matched_start_ms")
            and recovery.get("matched_end_ms") == request.get("matched_end_ms")
            and _valid_missing_proposal_audio_gate(gate, request)
        )
    if not (
        recovery.get("schema_version") == HISTORY_AUDIO_RECOVERY_SCHEMA
        and recovery.get("status") == "BLOCK"
        and recovery.get("reason_code") == REQUIRED_REASON
        and recovery.get("cpa_reason_code") == CPA_MORE_AUDIO_REASON
        and recovery.get("needs_audio") is True
        and gate.get("status") == "BLOCK"
        and gate.get("needs_audio") is True
    ):
        return False
    source = recovery.get("gate_source")
    if source == "history_convergence_acoustic_witness":
        prompt_sha = gate.get("judge_prompt_sha256")
        completion_sha = gate.get("judge_completion_sha256")
        schema_valid = gate.get("schema_version") == GATE_SCHEMA
    elif source == "exact_final_cpa_cycle_adjudication":
        prompt_sha = gate.get("prompt_sha256")
        completion_sha = gate.get("judge_completion_sha256")
        schema_valid = gate.get("schema_version") == _CYCLE_RECEIPT_SCHEMA
    else:
        return False
    if not (
        schema_valid
        and isinstance(gate.get("reason"), str)
        and bool(gate.get("reason", "").strip())
        and isinstance(gate.get("judge_served_from_cache"), bool)
        and _valid_digest(prompt_sha)
        and _valid_digest(completion_sha)
    ):
        return False
    original_reason = gate.get("reason_code")
    if original_reason == REQUIRED_REASON:
        original_reason = gate.get("cpa_reason_code")
    if original_reason != CPA_MORE_AUDIO_REASON:
        return False
    if recovery.get("matched_start_ms") != request.get("matched_start_ms"):
        return False
    if recovery.get("matched_end_ms") != request.get("matched_end_ms"):
        return False
    return _history_audio_request_matches(gate, request)


def valid_convergence_mutation_authority(
    adjudication: Mapping[str, Any],
    *,
    proposed: str,
    window: tuple[int, int],
) -> bool:
    """Reject convergence provenance relabelled as an ordinary mutation."""

    from src.autoslice.span_phoneme_resolution import marked, valid_adjudication
    if marked(adjudication):
        return valid_adjudication(adjudication, proposed=proposed, window=window)
    raw_policy = adjudication.get("policy_branch")
    policy = raw_policy if isinstance(raw_policy, str) else None
    mutation = adjudication.get("mutation_authority")
    witness_judge = adjudication.get("witness_judge")
    judge = (
        witness_judge.get("judge")
        if isinstance(witness_judge, Mapping)
        else None
    )
    raw_basis = mutation.get("basis") if isinstance(mutation, Mapping) else None
    basis = raw_basis if isinstance(raw_basis, str) else None
    raw_witness_status = (
        witness_judge.get("witness_status")
        if isinstance(witness_judge, Mapping)
        else None
    )
    witness_status = (
        raw_witness_status if isinstance(raw_witness_status, str) else None
    )
    raw_judge_schema = judge.get("schema_version") if isinstance(judge, Mapping) else None
    judge_schema = raw_judge_schema if isinstance(raw_judge_schema, str) else None
    structural_marker = bool(
        any(
            key in adjudication
            for key in (
                "exact_final_cpa_convergence_memo",
                "exact_final_cpa_cycle_memo",
                "history_convergence_acoustic_witness",
                "pre_convergence_adjudication",
                "pre_cycle_adjudications",
            )
        )
        or policy in _CONVERGENCE_APPLY_CONTRACTS
        or basis in _CONVERGENCE_MUTATION_BASES
        or witness_status in _CONVERGENCE_WITNESS_STATUSES
        or judge_schema in _CONVERGENCE_JUDGE_SCHEMAS
    )
    if not structural_marker:
        return True
    if proposed == "":
        memo = adjudication.get("exact_final_cpa_cycle_memo")
        return bool(
            isinstance(memo, Mapping)
            and memo.get("schema_version") == "exact-final-cpa-cycle-memo.v1"
            and memo.get("status") == "LOCKED"
            and memo.get("choice_id") == "DROP"
            and memo.get("final_text") == ""
            and memo.get("final_text_sha256") == "sha256:" + _sha_text("")
            and memo.get("matched_start_ms") == window[0]
            and memo.get("matched_end_ms") == window[1]
            and "exact_final_cpa_convergence_memo" not in adjudication
            and "history_convergence_acoustic_witness" not in adjudication
            and policy == "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"
            and basis == "CPA_EXPLICIT_INAUDIBLE_DROP"
            and witness_status == "OBSERVED"
            and judge_schema == "acoustic-witness-adjudication.v1"
            and isinstance(judge, Mapping)
            and judge.get("status") == "JUDGED"
            and judge.get("choice") == "DROP"
        )
    contract = _CONVERGENCE_APPLY_CONTRACTS.get(policy or "")
    if contract is None:
        return False
    memo = adjudication.get(contract["memo_key"])
    other_memo_key = (
        "exact_final_cpa_cycle_memo"
        if contract["memo_key"] == "exact_final_cpa_convergence_memo"
        else "exact_final_cpa_convergence_memo"
    )
    if not (
        isinstance(memo, Mapping)
        and memo.get("schema_version") == contract["memo_schema"]
        and memo.get("status") == contract["memo_status"]
        and memo.get("final_text") == proposed
        and memo.get("final_text_sha256") == "sha256:" + _sha_text(proposed)
        and memo.get("matched_start_ms") == window[0]
        and memo.get("matched_end_ms") == window[1]
        and other_memo_key not in adjudication
        and contract["pre_key"] in adjudication
        and isinstance(mutation, Mapping)
        and mutation.get("basis") == contract["mutation_basis"]
        and witness_status == contract["witness_status"]
        and isinstance(judge, Mapping)
        and judge.get("schema_version") == contract["judge_schema"]
        and judge.get("status") == "JUDGED"
        and judge.get("choice") == "PROPOSED"
    ):
        return False
    top_gate = adjudication.get("history_convergence_acoustic_witness")
    memo_gate = memo.get("acoustic_witness_gate")
    return bool(
        top_gate == memo_gate
        and valid_convergence_witness_gate(
            top_gate,
            proposed=proposed,
            window=window,
        )
    )


def downgrade_convergence_finding(
    finding: Mapping[str, Any],
    *,
    gate: Mapping[str, Any],
) -> dict[str, Any]:
    row = dict(finding)
    row["repair_class"] = "disclosure_only"
    row["why"] = (
        "[历史收敛缺声学见证，降为 disclosure_only] "
        + str(row.get("why") or "")
    )[:240]
    adjudication = row.get("exact_release_adjudication")
    final = dict(adjudication) if isinstance(adjudication, Mapping) else {}
    final.update(
        status="UNCERTAIN",
        repaired=False,
        reason_code=REQUIRED_REASON,
        policy_branch=DOWNGRADE_BRANCH,
        decision_authority="CPA_PROPOSAL_ONLY",
        witness_authority="ACOUSTIC_WITNESS_REQUIRED",
        history_convergence_acoustic_witness=dict(gate),
        mutation_authority={
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "NOT_APPLIED",
            "basis": REQUIRED_REASON,
        },
    )
    row["exact_release_adjudication"] = final
    return row


def convergence_gate_from_adjudication(
    adjudication: Mapping[str, Any],
) -> object:
    gate = adjudication.get("history_convergence_acoustic_witness")
    if gate is not None:
        return gate
    for memo_key in (
        "exact_final_cpa_convergence_memo",
        "exact_final_cpa_cycle_memo",
    ):
        memo = adjudication.get(memo_key)
        if isinstance(memo, Mapping) and memo.get("acoustic_witness_gate") is not None:
            return memo.get("acoustic_witness_gate")
    return None


def build_self_heal_repair_receipt(
    *,
    finding: Mapping[str, Any],
    cue_index: int,
    current_text: str,
    proposed: str,
    matched_start_ms: int,
    matched_end_ms: int,
    adjudication: Mapping[str, Any],
    judge: Mapping[str, Any],
    mutation: Mapping[str, Any],
    request_sha256: str,
    is_drop: bool,
    inaudible_override_valid: bool,
) -> dict[str, object]:
    witness = adjudication.get("verdict")
    witness_judge = adjudication.get("witness_judge")
    acoustic = (
        {
            key: witness.get(key)
            for key in (
                "schema_version",
                "status",
                "request_sha256",
                "witness_protocol",
                "target_audible",
                "heard_pinyin",
                "syllable_count",
                "confidence",
                "source_media_sha256",
                "audio_clip_sha256",
            )
            if key in witness
        }
        if isinstance(witness, Mapping)
        else None
    )
    gate = convergence_gate_from_adjudication(adjudication)
    similarities = (
        witness_judge.get("candidate_pinyin_similarity")
        if isinstance(witness_judge, Mapping)
        else None
    )
    if not isinstance(similarities, Mapping):
        similarities = judge.get("candidate_pinyin_similarity")
    binding = binding_from_adjudication(adjudication)
    range_binding = None
    if binding is None and isinstance(gate, Mapping):
        bound = gate.get("acoustic_witness_binding")
        binding = dict(bound) if isinstance(bound, Mapping) else None
    if isinstance(gate, Mapping):
        bound_range = gate.get(RANGE_WORD_PROJECTION_FIELD)
        range_binding = (
            dict(bound_range) if isinstance(bound_range, Mapping) else None
        )
    execution_history = {}
    if adjudication.get("executed_span_judgments_required") is True:
        from src.autoslice.span_phoneme_resolution import validate_persisted_judge_scopes
        validate_persisted_judge_scopes(adjudication)
        execution_history = {"executed_span_judgments_required": True,
            "executed_span_judgments": deepcopy(adjudication["executed_span_judgments"])}
    return {
        **execution_history,
        **({"span_context_resolution": deepcopy(adjudication["span_context_resolution"])}
           if "span_context_resolution" in adjudication else {}),
        **({"origin_span_obligation": deepcopy(finding["origin_span_obligation"]),
            "origin_span_adjudication": deepcopy(adjudication)}
           if "origin_span_obligation" in finding else {}),
        "schema_version": "exact-final-cpa-self-heal.v1",
        "cue_index": cue_index,
        "matched_start_ms": matched_start_ms,
        "matched_end_ms": matched_end_ms,
        "before": current_text,
        "after": proposed,
        "before_sha256": "sha256:" + _sha_text(current_text),
        "after_sha256": "sha256:" + _sha_text(proposed),
        "finding_sha256": "sha256:" + _sha_json(finding),
        "request_sha256": "sha256:" + request_sha256,
        "decision_authority": "CPA_JUDGE",
        "action": "DROP_CUE" if is_drop else "REPLACE_CUE_TEXT",
        "repair_class": (
            adjudication.get("request", {}).get("repair_class")
            if isinstance(adjudication.get("request"), Mapping)
            else None
        ),
        "policy_branch": adjudication.get("policy_branch"),
        "acoustic_witness": acoustic,
        "candidate_pinyin_similarity": (
            dict(similarities) if isinstance(similarities, Mapping) else None
        ),
        "acoustic_witness_binding": binding,
        RANGE_WORD_PROJECTION_FIELD: range_binding,
        "history_convergence_acoustic_witness": gate,
        "judge": dict(judge),
        "drop_authority": (
            dict(adjudication["drop_authority"])
            if is_drop
            and isinstance(adjudication.get("drop_authority"), Mapping)
            else None
        ),
        "inaudible_witness_override": (
            dict(witness_judge["inaudible_witness_override"])
            if inaudible_override_valid
            and isinstance(witness_judge, Mapping)
            and isinstance(
                witness_judge.get("inaudible_witness_override"),
                Mapping,
            )
            else None
        ),
        "mutation_authority": dict(mutation),
        "timing_immutable": True,
    }
