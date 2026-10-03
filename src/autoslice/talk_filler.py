"""Fail-closed filler-removal planning for discontinuous talk clips.

Semantic recall may propose removals, but this module is the deterministic
authorization boundary.  It either returns ordered retained source intervals
with an auditable join map or falls back to the original contiguous interval.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.autoslice.chat_evidence import normalize_chat_text
from src.autoslice.piece_roles import content_only


FILLER_PLAN_SCHEMA = "talk-filler-plan.v1"
FILLER_AUDIT_SCHEMA = "talk-filler-audit.v1"
MIN_TALK_EFFECTIVE_DURATION_MS = 60_000
MIN_AUTOMATIC_EFFECTIVE_DURATION_MS = 60_500
MIN_MODEL_CONFIDENCE = 0.94
MAX_AUTOMATIC_REMOVALS = 2
MAX_REVIEWED_REMOVALS = 3
MAX_SINGLE_REMOVAL_MS = 15_000
MAX_AUTOMATIC_REMOVED_RATIO = 0.15
MAX_AUTOMATIC_REMOVED_MS = 20_000
# Legacy merge-gap constants remain for input compatibility; selector recall no
# longer creates gap cuts, and the filler rejects such inputs without explicit
# user approval.
MAX_MERGE_GAP_MS = 600_000
MAX_MERGE_GAP_REMOVALS = 2
MIN_RETAINED_PIECE_MS = 6_000
MIN_DEAD_PAUSE_MS = 3_000
AUTOMATIC_EDGE_GUARD_MS = 5_000

_THANKS_RX = re.compile(
    r"(?:谢(?:谢|谢你|一下|大家|[^\s，。！？!?]{0,12}的)|感谢|"
    r"粉丝灯牌|钢蹦|钢镚|告白花束|音乐盒|舰长|上舰|礼物|"
    r"灵感多|阿里嘎多|ありがとう)",
    re.IGNORECASE,
)
_GIFT_OBJECT_RX = re.compile(
    r"(?:粉丝灯牌|钢蹦|钢镚|告白花束|音乐盒|舰长|上舰|礼物)",
    re.IGNORECASE,
)
_HOUSEKEEPING_RX = re.compile(
    r"(?:喝(?:一?(?:口|杯))?水|接水|倒水|上(?:个)?厕所|去厕所|洗手间|暂时离席|离席|离开一下|我去一下|马上回来)",
    re.IGNORECASE,
)
_SUBSTANTIVE_RX = re.compile(
    r"(?:为什么|怎么|请问|因为|所以|但是|然后|我想|问题|故事|视频|"
    r"唱歌|回应|结果|最后|其实|觉得)"
)
_SEMANTIC_DENY_FIELDS = (
    "contains_setup",
    "contains_cause",
    "contains_answer",
    "contains_punchline",
    "contains_referent_intro",
    "contains_correction",
    "contains_resolution",
    "later_dependency",
    "interaction_relevant",
    "meaning_or_stance_changed",
)
_GLOBAL_REQUIRED_TRUE_FIELDS = (
    "topic_complete",
    "cause_answer_chain_complete",
    "referents_resolved",
    "corrections_preserved",
    "punchline_preserved",
    "closing_resolution_preserved",
    "audience_interactions_consistent",
    "audience_feedback_preserved",
    "meaningful_repeats_preserved",
)

_AUTOMATIC_REMOVAL_REASONS = frozenset(
    {"gift_thanks", "unrelated_sc", "housekeeping"}
)
_BOUND_STRUCTURED_CHAT_STATUSES = frozenset(
    {"BOUND_DIRECT", "BOUND_SOURCE_ALIAS"}
)
_SHA256_RX = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _cue_text(cue: object) -> str:
    return " ".join(str(getattr(cue, "text", "") or "").split())


def _cue_row(cue: object, position: int) -> dict[str, object]:
    return {
        "position": position,
        "cue_id": str(getattr(cue, "cue_id", "") or ""),
        "start_ms": int(getattr(cue, "source_start_ms")),
        "end_ms": int(getattr(cue, "source_end_ms")),
        "text": _cue_text(cue),
    }


def _structured_chat_binding_receipt(
    binding: Mapping[str, object] | None,
) -> dict[str, object]:
    """Keep only the hash-bound fields needed for filler audit/prompt input."""

    if not isinstance(binding, Mapping):
        return {}
    return {
        key: binding[key]
        for key in (
            "chat_jsonl",
            "chat_jsonl_sha256",
            "chat_origin_epoch_ms",
            "chat_timeline_offset_ms",
            "structured_chat_required",
            "chat_binding_status",
            "chat_binding_authority",
            "chat_source_alias_id",
            "chat_canonical_recording_basename",
        )
        if key in binding
    }


def _load_structured_chat_sc_witness(
    binding: Mapping[str, object] | None,
    *,
    source_start_ms: int,
    source_end_ms: int,
    clip_start_ms: int,
    clip_end_ms: int,
    removed_cues: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], str | None]:
    """Load only hash-bound SC rows that can support an ``unrelated_sc`` cut.

    A model-provided reason is never enough: the normal runner must have bound a
    real JSONL sidecar, and the sidecar must contain a nearby SUPER_CHAT event.
    The bounded window allows for the speaker reading an SC shortly after it was
    sent while keeping unrelated session-wide SCs out of the evidence.
    """

    if not isinstance(binding, Mapping):
        return [], "structured_chat_binding_missing"
    if binding.get("structured_chat_required") is not True:
        return [], "structured_chat_binding_not_required"
    if str(binding.get("chat_binding_status") or "") not in _BOUND_STRUCTURED_CHAT_STATUSES:
        return [], "structured_chat_binding_unverified"
    raw_path = str(binding.get("chat_jsonl") or "").strip()
    declared_sha = str(binding.get("chat_jsonl_sha256") or "").strip().lower()
    path = Path(raw_path) if raw_path else None
    if path is None or path.is_symlink() or not path.is_file():
        return [], "structured_chat_jsonl_missing_or_not_regular"
    if not _SHA256_RX.fullmatch(declared_sha):
        return [], "structured_chat_jsonl_hash_missing_or_invalid"
    try:
        source_bytes = path.read_bytes()
        actual_sha = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
    except OSError:
        return [], "structured_chat_jsonl_unreadable"
    if actual_sha != declared_sha:
        return [], "structured_chat_jsonl_hash_mismatch"
    origin = binding.get("chat_origin_epoch_ms")
    offset = binding.get("chat_timeline_offset_ms")
    if (
        isinstance(origin, bool)
        or not isinstance(origin, int)
        or origin <= 0
        or isinstance(offset, bool)
        or not isinstance(offset, int)
    ):
        return [], "structured_chat_timeline_binding_invalid"
    try:
        from src.autoslice.chat_authority import load_chat_jsonl

        # Parse exactly the bytes whose digest was checked.  This prevents a
        # sidecar replacement between hashing and parsing from becoming SC
        # authorization evidence.
        rows = load_chat_jsonl(
            path,
            recording_start_ms=origin,
            source_bytes=source_bytes,
        )
    except (OSError, TypeError, ValueError):
        return [], "structured_chat_jsonl_parse_failed"

    # The event often precedes the spoken read by a few seconds.  Never widen
    # beyond the candidate itself; missing/uncertain association keeps the clip
    # contiguous.
    witness_start_ms = max(clip_start_ms, source_start_ms - 30_000)
    witness_end_ms = min(clip_end_ms, source_end_ms)
    witness: list[dict[str, object]] = []
    for row in rows:
        if row.kind != "superchat":
            continue
        event_offset_ms = int(row.offset_ms) + offset
        if not witness_start_ms <= event_offset_ms <= witness_end_ms:
            continue
        witness.append(
            {
                "offset_ms": event_offset_ms,
                "sender": str(row.sender or ""),
                "text": " ".join(str(row.text or "").split()),
                "source_event_id": str(row.source_event_id or ""),
                "source_sha256": str(row.source_sha256 or declared_sha),
            }
        )
    if not witness:
        return [], "structured_chat_sc_witness_missing"
    removed_text = normalize_chat_text(
        "".join(str(row.get("text") or "") for row in removed_cues)
    )
    if len(removed_text) < 4:
        return [], "structured_chat_removed_text_too_short"
    matched = [
        row
        for row in witness
        if len(normalize_chat_text(str(row.get("text") or ""))) >= 4
        and normalize_chat_text(str(row.get("text") or "")) in removed_text
    ]
    if not matched:
        return [], "structured_chat_sc_not_read_in_removed_text"
    for row in matched:
        row["removed_text_match"] = True
    return matched[:16], None


def _contiguous_plan(
    *,
    start_ms: int,
    end_ms: int,
    status: str,
    rejected: list[dict[str, object]],
    source_srt_sha256: str | None,
) -> dict[str, object]:
    return {
        "schema_version": FILLER_PLAN_SCHEMA,
        "status": status,
        "source_start_ms": start_ms,
        "source_end_ms": end_ms,
        "source_srt_sha256": source_srt_sha256,
        "effective_duration_ms": max(0, end_ms - start_ms),
        "removed_duration_ms": 0,
        "retained_intervals": [{"start_ms": start_ms, "end_ms": end_ms}],
        "removals": [],
        "rejected_proposals": rejected,
    }


def _proposal_interval(
    proposal: Mapping[str, object],
    cues: Sequence[object],
    *,
    clip_start_ms: int,
    clip_end_ms: int,
    structured_chat_binding: Mapping[str, object] | None = None,
) -> tuple[dict[str, object] | None, str | None]:
    try:
        first_position = int(proposal["start_cue"])
        last_position = int(proposal["end_cue"])
    except (KeyError, TypeError, ValueError):
        return None, "cue_range_invalid"
    if not 1 <= first_position <= last_position <= len(cues):
        return None, "cue_range_invalid"

    mode = str(proposal.get("mode") or "remove_cues")
    reason = str(proposal.get("reason") or "")
    if mode == "gap_only":
        # Legacy gap proposals remain parseable only so the deterministic
        # authorization path can reject them; no automatic reason admits this
        # mode after the three-category policy change.
        if last_position != first_position + 1:
            return None, "dead_pause_requires_adjacent_retained_cues"
        left = cues[first_position - 1]
        right = cues[last_position - 1]
        start_ms = int(getattr(left, "source_end_ms"))
        end_ms = int(getattr(right, "source_start_ms"))
        removed_cues: list[dict[str, object]] = []
        left_context = _cue_row(left, first_position)
        right_context = _cue_row(right, last_position)
    elif mode == "remove_cues":
        if first_position <= 1 or last_position >= len(cues):
            return None, "removal_needs_retained_context_on_both_sides"
        previous = cues[first_position - 2]
        following = cues[last_position]
        # Retain the surrounding pauses/reactions.  Permission to remove these
        # spoken cues does not authorize cutting all time between their neighbors.
        start_ms = int(getattr(cues[first_position - 1], "source_start_ms"))
        end_ms = int(getattr(cues[last_position - 1], "source_end_ms"))
        removed_cues = [
            _cue_row(cue, position)
            for position, cue in enumerate(
                cues[first_position - 1 : last_position],
                start=first_position,
            )
        ]
        left_context = _cue_row(previous, first_position - 1)
        right_context = _cue_row(following, last_position + 1)
    else:
        return None, "mode_not_supported"

    if not clip_start_ms < start_ms < end_ms < clip_end_ms:
        return None, "removal_not_strictly_inside_candidate"
    duration_ms = end_ms - start_ms
    if duration_ms > MAX_SINGLE_REMOVAL_MS:
        return None, "single_removal_too_long"

    structured_chat_witness: list[dict[str, object]] = []
    structured_chat_error: str | None = None
    if reason == "unrelated_sc":
        structured_chat_witness, structured_chat_error = _load_structured_chat_sc_witness(
            structured_chat_binding,
            source_start_ms=start_ms,
            source_end_ms=end_ms,
            clip_start_ms=clip_start_ms,
            clip_end_ms=clip_end_ms,
            removed_cues=removed_cues,
        )

    return (
        {
            "proposal_id": str(
                proposal.get("proposal_id")
                or f"{reason}_{first_position}_{last_position}"
            ),
            "mode": mode,
            "reason": reason,
            "source_start_ms": start_ms,
            "source_end_ms": end_ms,
            "removed_duration_ms": duration_ms,
            "model_confidence": float(proposal.get("confidence") or 0.0),
            "bridge_coherent": proposal.get("bridge_coherent") is True,
            "bridge_reason": str(proposal.get("bridge") or "").strip(),
            "semantic_checks": {
                "topic_relation": proposal.get("topic_relation"),
                **{
                    field: proposal.get(field)
                    for field in _SEMANTIC_DENY_FIELDS
                },
            },
            "acoustic_evidence": proposal.get("acoustic_evidence"),
            "structured_chat_binding": _structured_chat_binding_receipt(
                structured_chat_binding
            ),
            "structured_chat_witness": structured_chat_witness,
            "structured_chat_error": structured_chat_error,
            "removed_cues": removed_cues,
            "left_retained_context": left_context,
            "right_retained_context": right_context,
            "authority": "semantic_proposal_plus_deterministic_v1",
            "authorization_kind": "automatic",
        },
        None,
    )


def _reason_authorized(removal: Mapping[str, object]) -> str | None:
    reason = str(removal.get("reason") or "")
    mode = str(removal.get("mode") or "")
    removed_cues = removal.get("removed_cues")
    texts = [
        str(row.get("text") or "")
        for row in removed_cues
        if isinstance(row, Mapping)
    ] if isinstance(removed_cues, list) else []
    joined = " ".join(texts)
    semantic_checks = removal.get("semantic_checks")

    if reason == "gift_thanks" and mode == "remove_cues":
        if not _THANKS_RX.search(joined):
            return "gift_thanks_has_no_lexical_witness"
        if not _GIFT_OBJECT_RX.search(joined):
            return "gift_thanks_has_no_specific_gift_witness"
        if _SUBSTANTIVE_RX.search(joined):
            return "gift_thanks_contains_substantive_topic_language"
        if len(joined) > 120:
            return "gift_thanks_block_too_verbose"
        if not isinstance(semantic_checks, Mapping):
            return "semantic_discrete_checks_missing"
        if semantic_checks.get("topic_relation") != "unrelated":
            return "semantic_topic_relation_not_removable"
        if any(semantic_checks.get(field) is not False for field in _SEMANTIC_DENY_FIELDS):
            return "semantic_protected_role_or_dependency_not_denied"
        return None
    if reason == "housekeeping" and mode == "remove_cues":
        if not _HOUSEKEEPING_RX.search(joined):
            return "housekeeping_has_no_lexical_witness"
        if len(joined) > 120:
            return "housekeeping_block_too_verbose"
        if not isinstance(semantic_checks, Mapping):
            return "semantic_discrete_checks_missing"
        if semantic_checks.get("topic_relation") not in {"incidental", "unrelated"}:
            return "semantic_topic_relation_not_removable"
        if any(semantic_checks.get(field) is not False for field in _SEMANTIC_DENY_FIELDS):
            return "semantic_protected_role_or_dependency_not_denied"
        return None
    if reason == "unrelated_sc" and mode == "remove_cues":
        if not isinstance(semantic_checks, Mapping) or any(
            semantic_checks.get(field) is None
            for field in ("topic_relation", *_SEMANTIC_DENY_FIELDS)
        ):
            return "semantic_discrete_checks_missing"
        if semantic_checks.get("topic_relation") != "unrelated":
            return "semantic_topic_relation_not_unrelated"
        if any(semantic_checks.get(field) is not False for field in _SEMANTIC_DENY_FIELDS):
            return "semantic_protected_role_or_dependency_not_denied"
        if removal.get("structured_chat_error"):
            return str(removal["structured_chat_error"])
        witness = removal.get("structured_chat_witness")
        if not isinstance(witness, list) or not witness:
            return "structured_chat_sc_witness_missing"
        if any(
            not isinstance(row, Mapping) or row.get("removed_text_match") is not True
            for row in witness
        ):
            return "structured_chat_sc_not_read_in_removed_text"
        return None
    if reason not in _AUTOMATIC_REMOVAL_REASONS:
        return "automatic_reason_not_allowed"
    # The model must not be able to invent a new automatic reason or mode.
    if mode != "remove_cues":
        return "automatic_reason_mode_not_allowed"
    return "automatic_reason_not_allowed"


def _authorize_automatic_proposals(
    proposals: Sequence[Mapping[str, object]],
    cues: Sequence[object],
    *,
    start_ms: int,
    end_ms: int,
    structured_chat_binding: Mapping[str, object] | None,
    rejected: list[dict[str, object]],
) -> list[dict[str, object]]:
    accepted: list[dict[str, object]] = []
    for index, proposal in enumerate(proposals):
        confidence = proposal.get("confidence")
        confidence_value = (
            float(confidence)
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else 0.0
        )
        if confidence_value < MIN_MODEL_CONFIDENCE:
            rejected.append(
                {
                    "proposal_index": index,
                    "reason_code": "MODEL_CONFIDENCE_BELOW_THRESHOLD",
                    "confidence": confidence_value,
                }
            )
            continue
        if proposal.get("bridge_coherent") is not True or not str(
            proposal.get("bridge") or ""
        ).strip():
            rejected.append(
                {
                    "proposal_index": index,
                    "reason_code": "BRIDGE_COHERENCE_NOT_PROVEN",
                }
            )
            continue
        removal, error = _proposal_interval(
            proposal,
            cues,
            clip_start_ms=start_ms,
            clip_end_ms=end_ms,
            structured_chat_binding=structured_chat_binding,
        )
        if removal is None:
            rejected.append(
                {"proposal_index": index, "reason_code": str(error or "invalid")}
            )
            continue
        reason_error = _reason_authorized(removal)
        if reason_error:
            rejected.append(
                {
                    "proposal_index": index,
                    "proposal_id": removal["proposal_id"],
                    "reason_code": reason_error,
                    "reason": removal.get("reason"),
                }
            )
            continue
        accepted.append(removal)
    return accepted


def _authorize_reviewed_removals(
    reviewed: Sequence[Mapping[str, object]],
    *,
    start_ms: int,
    end_ms: int,
    rejected: list[dict[str, object]],
) -> list[dict[str, object]]:
    accepted: list[dict[str, object]] = []
    for index, row in enumerate(reviewed):
        authority = row.get("authority")
        if (
            not isinstance(authority, str)
            or not authority.strip()
            or not re.fullmatch(
                r"(?:user|维护者)_reviewed_\d{4}-\d{2}-\d{2}(?:[/:].+)?",
                authority.strip(),
            )
        ):
            rejected.append(
                {
                    "reviewed_index": index,
                    "proposal_id": row.get("proposal_id"),
                    "reason_code": "CONSERVATIVE_POLICY_REQUIRES_USER_APPROVAL",
                }
            )
            continue
        authority = authority.strip()
        try:
            removal_start = int(row["start_ms"])
            removal_end = int(row["end_ms"])
        except (KeyError, TypeError, ValueError):
            rejected.append(
                {
                    "reviewed_index": index,
                    "reason_code": "REVIEWED_INTERVAL_INVALID",
                }
            )
            continue
        if (
            not start_ms < removal_start < removal_end < end_ms
            or removal_end - removal_start > MAX_SINGLE_REMOVAL_MS
        ):
            rejected.append(
                {
                    "reviewed_index": index,
                    "reason_code": "REVIEWED_INTERVAL_OUT_OF_POLICY",
                }
            )
            continue
        accepted.append(
            {
                "proposal_id": str(
                    row.get("proposal_id") or f"reviewed_{index + 1}"
                ),
                "mode": "reviewed_exact_interval",
                "reason": str(row.get("reason") or "reviewed_filler"),
                "source_start_ms": removal_start,
                "source_end_ms": removal_end,
                "removed_duration_ms": removal_end - removal_start,
                "model_confidence": None,
                "bridge_coherent": True,
                "bridge_reason": str(row.get("bridge") or "维护者 reviewed jump"),
                "removed_cues": list(row.get("removed_cues") or []),
                "left_retained_context": row.get("left_retained_context"),
                "right_retained_context": row.get("right_retained_context"),
                "authority": authority,
                "authorization_kind": "reviewed",
            }
        )
    return accepted


def _authorize_merge_gap_removals(
    merge_gaps: Sequence[Mapping[str, object]],
    *,
    start_ms: int,
    end_ms: int,
    rejected: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Reject legacy selector gap cuts unless a separate user approval exists."""

    for index, row in enumerate(merge_gaps):
        rejected.append(
            {
                "merge_gap_index": index,
                "proposal_id": row.get("proposal_id"),
                "reason_code": "CONSERVATIVE_POLICY_REQUIRES_USER_APPROVAL",
            }
        )
    return []


def _select_removals(
    accepted: Sequence[dict[str, object]],
    *,
    start_ms: int,
    end_ms: int,
    rejected: list[dict[str, object]],
) -> tuple[list[dict[str, object]], int]:
    selected: list[dict[str, object]] = []
    original_duration_ms = end_ms - start_ms
    removed_duration_ms = 0
    automatic_removed_ms = 0
    automatic_removal_count = 0
    reviewed_removal_count = 0
    merge_gap_removal_count = 0
    for removal in accepted:
        kind = str(removal.get("authorization_kind") or "automatic")
        is_reviewed = kind == "reviewed"
        is_merge_gap = kind == "merge_gap"
        is_automatic = not is_reviewed and not is_merge_gap
        if is_reviewed and reviewed_removal_count >= MAX_REVIEWED_REMOVALS:
            rejected.append(
                {
                    "proposal_id": removal["proposal_id"],
                    "reason_code": "REVIEWED_REMOVAL_COUNT_CAP_EXCEEDED",
                }
            )
            continue
        if is_merge_gap and merge_gap_removal_count >= MAX_MERGE_GAP_REMOVALS:
            rejected.append(
                {
                    "proposal_id": removal["proposal_id"],
                    "reason_code": "MERGE_GAP_REMOVAL_COUNT_CAP_EXCEEDED",
                }
            )
            continue
        if is_automatic and automatic_removal_count >= MAX_AUTOMATIC_REMOVALS:
            rejected.append(
                {
                    "proposal_id": removal["proposal_id"],
                    "reason_code": "AUTOMATIC_REMOVAL_COUNT_CAP_EXCEEDED",
                }
            )
            continue
        if selected and int(removal["source_start_ms"]) < int(
            selected[-1]["source_end_ms"]
        ):
            rejected.append(
                {
                    "proposal_id": removal["proposal_id"],
                    "reason_code": "REMOVAL_INTERVAL_OVERLAP",
                }
            )
            continue
        if is_automatic and (
            int(removal["source_start_ms"]) - start_ms < AUTOMATIC_EDGE_GUARD_MS
            or end_ms - int(removal["source_end_ms"]) < AUTOMATIC_EDGE_GUARD_MS
        ):
            rejected.append(
                {
                    "proposal_id": removal["proposal_id"],
                    "reason_code": "AUTOMATIC_EDGE_GUARD_VIOLATED",
                }
            )
            continue
        # merge_gap 不受微剪预算约束（它移除的是同主题两段之间的整段
        # 无关内容，量级本来就大）；自动微剪预算只统计自动车道自身的量，
        # 不被 merge_gap 的大移除吃掉。
        proposed_automatic_removed = automatic_removed_ms + (
            int(removal["removed_duration_ms"]) if is_automatic else 0
        )
        if is_automatic and (
            proposed_automatic_removed / original_duration_ms
            > MAX_AUTOMATIC_REMOVED_RATIO
            or proposed_automatic_removed > MAX_AUTOMATIC_REMOVED_MS
        ):
            rejected.append(
                {
                    "proposal_id": removal["proposal_id"],
                    "reason_code": "AUTOMATIC_REMOVAL_BUDGET_EXCEEDED",
                }
            )
            continue
        selected.append(removal)
        removed_duration_ms += int(removal["removed_duration_ms"])
        automatic_removed_ms = proposed_automatic_removed
        if is_reviewed:
            reviewed_removal_count += 1
        elif is_merge_gap:
            merge_gap_removal_count += 1
        else:
            automatic_removal_count += 1
    return selected, removed_duration_ms


def _retained_intervals(
    *,
    start_ms: int,
    end_ms: int,
    removals: Sequence[Mapping[str, object]],
) -> list[dict[str, int]]:
    retained: list[dict[str, int]] = []
    cursor = start_ms
    for removal in removals:
        removal_start = int(removal["source_start_ms"])
        removal_end = int(removal["source_end_ms"])
        retained.append({"start_ms": cursor, "end_ms": removal_start})
        cursor = removal_end
    retained.append({"start_ms": cursor, "end_ms": end_ms})
    return retained


def build_talk_filler_plan(
    *,
    start_ms: int,
    end_ms: int,
    cues: Sequence[object],
    proposals: Sequence[Mapping[str, object]] | None = None,
    proposal_srt_sha256: str | None = None,
    source_srt_path: Path | None = None,
    reviewed_removals: Sequence[Mapping[str, object]] | None = None,
    merge_gap_removals: Sequence[Mapping[str, object]] | None = None,
    structured_chat_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Authorize proposals and return a complete retained-interval plan.

    Automatic proposals are bound to the exact BCUT SRT that the model saw.
    Human-reviewed exact intervals use the same duration, overlap, and retained
    piece invariants but do not need model confidence or lexical heuristics.
    """

    rejected: list[dict[str, object]] = []
    actual_srt_sha256 = (
        _sha256(source_srt_path)
        if source_srt_path is not None and source_srt_path.is_file()
        else None
    )
    automatic = list(proposals or [])
    reviewed = list(reviewed_removals or [])
    if end_ms <= start_ms:
        return _contiguous_plan(
            start_ms=start_ms,
            end_ms=end_ms,
            status="contiguous_fallback_candidate_interval_invalid",
            rejected=[{"reason_code": "CANDIDATE_INTERVAL_INVALID"}],
            source_srt_sha256=actual_srt_sha256,
        )
    if automatic and (
        not proposal_srt_sha256
        or not actual_srt_sha256
        or proposal_srt_sha256 != actual_srt_sha256
    ):
        rejected.append(
            {
                "reason_code": "SOURCE_SRT_BINDING_MISMATCH",
                "expected": proposal_srt_sha256,
                "actual": actual_srt_sha256,
            }
        )
        automatic = []

    accepted = _authorize_automatic_proposals(
        automatic,
        cues,
        start_ms=start_ms,
        end_ms=end_ms,
        structured_chat_binding=structured_chat_binding,
        rejected=rejected,
    )
    accepted.extend(
        _authorize_reviewed_removals(
            reviewed,
            start_ms=start_ms,
            end_ms=end_ms,
            rejected=rejected,
        )
    )
    accepted.extend(
        _authorize_merge_gap_removals(
            list(merge_gap_removals or []),
            start_ms=start_ms,
            end_ms=end_ms,
            rejected=rejected,
        )
    )
    accepted.sort(
        key=lambda row: (
            int(row["source_start_ms"]),
            int(row["source_end_ms"]),
        )
    )
    original_duration_ms = end_ms - start_ms
    selected, removed_duration_ms = _select_removals(
        accepted,
        start_ms=start_ms,
        end_ms=end_ms,
        rejected=rejected,
    )
    retained = _retained_intervals(
        start_ms=start_ms,
        end_ms=end_ms,
        removals=selected,
    )

    if selected and any(
        row["end_ms"] - row["start_ms"] < MIN_RETAINED_PIECE_MS
        for row in retained
    ):
        rejected.extend(
            {
                "proposal_id": row["proposal_id"],
                "reason_code": "RETAINED_PIECE_TOO_SHORT",
            }
            for row in selected
        )
        selected = []
        retained = [{"start_ms": start_ms, "end_ms": end_ms}]
        removed_duration_ms = 0

    effective_duration_ms = original_duration_ms - removed_duration_ms
    required_effective_duration_ms = (
        MIN_AUTOMATIC_EFFECTIVE_DURATION_MS
        if any(row.get("authorization_kind") == "automatic" for row in selected)
        else MIN_TALK_EFFECTIVE_DURATION_MS
    )
    if selected and effective_duration_ms <= required_effective_duration_ms:
        rejected.extend(
            {
                "proposal_id": row["proposal_id"],
                "reason_code": "EFFECTIVE_DURATION_NOT_OVER_60S",
            }
            for row in selected
        )
        selected = []
        retained = [{"start_ms": start_ms, "end_ms": end_ms}]
        removed_duration_ms = 0
        effective_duration_ms = original_duration_ms

    elapsed_retained_ms = 0
    for index, removal in enumerate(selected):
        elapsed_retained_ms += (
            retained[index]["end_ms"] - retained[index]["start_ms"]
        )
        removal["content_output_jump_ms"] = elapsed_retained_ms

    status = "active" if selected else (
        "contiguous_fallback_srt_binding_failed"
        if any(
            row.get("reason_code") == "SOURCE_SRT_BINDING_MISMATCH"
            for row in rejected
        )
        else (
            "contiguous_fallback"
            if proposals or reviewed or rejected
            else "contiguous"
        )
    )
    return {
        "schema_version": FILLER_PLAN_SCHEMA,
        "status": status,
        "source_start_ms": start_ms,
        "source_end_ms": end_ms,
        "source_srt_sha256": actual_srt_sha256,
        "effective_duration_ms": effective_duration_ms,
        "removed_duration_ms": removed_duration_ms,
        "removed_ratio": (
            removed_duration_ms / original_duration_ms
            if original_duration_ms > 0
            else 0.0
        ),
        "retained_intervals": retained,
        "removals": selected,
        "rejected_proposals": rejected,
        "policy": {
            "minimum_effective_duration_ms_exclusive": MIN_TALK_EFFECTIVE_DURATION_MS,
            "minimum_automatic_effective_duration_ms_exclusive": MIN_AUTOMATIC_EFFECTIVE_DURATION_MS,
            "automatic_removal_reasons": sorted(_AUTOMATIC_REMOVAL_REASONS),
            "minimum_model_confidence": MIN_MODEL_CONFIDENCE,
            "maximum_automatic_removals": MAX_AUTOMATIC_REMOVALS,
            "maximum_reviewed_removals": MAX_REVIEWED_REMOVALS,
            "maximum_single_removal_ms": MAX_SINGLE_REMOVAL_MS,
            "maximum_automatic_removed_ratio": MAX_AUTOMATIC_REMOVED_RATIO,
            "maximum_automatic_removed_ms": MAX_AUTOMATIC_REMOVED_MS,
            "minimum_retained_piece_ms": MIN_RETAINED_PIECE_MS,
            "automatic_edge_guard_ms": AUTOMATIC_EDGE_GUARD_MS,
        },
    }


def verify_automatic_filler_plan(
    *,
    cues: Sequence[object],
    plan: Mapping[str, object],
    llm_call: Callable[[str], str],
) -> dict[str, object]:
    """Run the independent whole-plan semantic veto.

    The verifier may only return pass/fail.  It cannot create or move a cut.
    Invalid JSON, missing booleans, or any non-true invariant fail closed.
    """

    retained = plan.get("retained_intervals")
    removals = plan.get("removals")
    if not isinstance(retained, list) or not isinstance(removals, list):
        return {"status": "FAIL", "reason_code": "PLAN_STRUCTURE_INVALID"}
    if not any(
        isinstance(row, Mapping)
        and row.get("authorization_kind") == "automatic"
        for row in removals
    ):
        return {"status": "NOT_REQUIRED", "reason_code": "NO_AUTOMATIC_REMOVAL"}

    # New plans carry the exact candidate interval.  Older hand-built plans
    # used by replay/tests only carry retained pieces and removal boundaries;
    # recover that same source range without inventing a second coverage
    # contract.
    source_bounds: list[int] = []
    for interval in retained:
        if not isinstance(interval, Mapping):
            return {"status": "FAIL", "reason_code": "RETAINED_INTERVAL_INVALID"}
        try:
            source_bounds.extend(
                [int(interval["start_ms"]), int(interval["end_ms"])]
            )
        except (KeyError, TypeError, ValueError):
            return {"status": "FAIL", "reason_code": "RETAINED_INTERVAL_INVALID"}
    for removal in removals:
        if not isinstance(removal, Mapping):
            continue
        if removal.get("reason") == "unrelated_sc":
            witness = removal.get("structured_chat_witness")
            if not isinstance(witness, list) or not witness:
                return {
                    "status": "FAIL",
                    "reason_code": "STRUCTURED_CHAT_SC_WITNESS_MISSING",
                }
            if any(
                not isinstance(row, Mapping)
                or row.get("removed_text_match") is not True
                for row in witness
            ):
                return {
                    "status": "FAIL",
                    "reason_code": "STRUCTURED_CHAT_SC_NOT_READ_IN_REMOVED_TEXT",
                }
        try:
            source_bounds.extend(
                [
                    int(removal["source_start_ms"]),
                    int(removal["source_end_ms"]),
                ]
            )
        except (KeyError, TypeError, ValueError):
            continue
    try:
        source_start_ms = int(plan["source_start_ms"])
        source_end_ms = int(plan["source_end_ms"])
    except (KeyError, TypeError, ValueError):
        if not source_bounds:
            return {"status": "FAIL", "reason_code": "SOURCE_INTERVAL_UNAVAILABLE"}
        source_start_ms = min(source_bounds)
        source_end_ms = max(source_bounds)
    if source_end_ms <= source_start_ms:
        return {"status": "FAIL", "reason_code": "SOURCE_INTERVAL_INVALID"}

    source_transcript_parts: list[str] = []
    removed_transcript_parts: list[str] = []
    removal_rows: list[tuple[Mapping[str, object], int, int]] = []
    for removal in removals:
        if not isinstance(removal, Mapping):
            continue
        try:
            removal_start = int(removal["source_start_ms"])
            removal_end = int(removal["source_end_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if removal_start >= removal_end:
            continue
        removal_rows.append((removal, removal_start, removal_end))

    for position, cue in enumerate(cues, start=1):
        cue_start = int(getattr(cue, "source_start_ms"))
        cue_end = int(getattr(cue, "source_end_ms"))
        if not (cue_start < source_end_ms and cue_end > source_start_ms):
            continue
        matching_removals = [
            removal
            for removal, removal_start, removal_end in removal_rows
            if cue_start < removal_end and cue_end > removal_start
        ]
        label = (
            "REMOVED:"
            + ",".join(
                str(row.get("proposal_id") or "unknown")
                for row in matching_removals
            )
            if matching_removals
            else "RETAINED"
        )
        source_transcript_parts.append(
            f"[{label}] #{position} [{cue_start}-{cue_end}ms] {_cue_text(cue)}"
        )

    for removal, removal_start, removal_end in removal_rows:
        removed_lines = [
            f"#{position} [{int(getattr(cue, 'source_start_ms'))}-"
            f"{int(getattr(cue, 'source_end_ms'))}ms] {_cue_text(cue)}"
            for position, cue in enumerate(cues, start=1)
            if int(getattr(cue, "source_start_ms")) < removal_end
            and int(getattr(cue, "source_end_ms")) > removal_start
        ]
        if not removed_lines:
            plan_rows = removal.get("removed_cues")
            if isinstance(plan_rows, list):
                removed_lines = [
                    f"#{row.get('position')} [{row.get('start_ms')}-"
                    f"{row.get('end_ms')}ms] {row.get('text')}"
                    for row in plan_rows
                    if isinstance(row, Mapping)
                ]
        if not removed_lines:
            if removal.get("authorization_kind") == "automatic":
                return {"status": "FAIL", "reason_code": "REMOVED_TRANSCRIPT_MISSING"}
            removed_lines = ["(该删除区间没有可核对的原始字幕覆盖，必须 fail)"]
        removed_transcript_parts.append(
            f"<REMOVED {removal.get('proposal_id')} "
            f"{removal_start}-{removal_end}ms reason={removal.get('reason') or 'unknown'}>\n"
            + "\n".join(removed_lines)
            + "\n</REMOVED>"
        )

    structured_chat_parts: list[str] = []
    for removal, _removal_start, _removal_end in removal_rows:
        witness = removal.get("structured_chat_witness")
        if not isinstance(witness, list):
            continue
        rows = []
        for row in witness:
            if not isinstance(row, Mapping):
                continue
            rows.append(
                f"@{row.get('offset_ms')}ms sender={row.get('sender') or '(unknown)'} "
                f"event={row.get('source_event_id') or '(unknown)'} "
                f"removed_text_match={row.get('removed_text_match') is True}: "
                f"{row.get('text') or ''}"
            )
        if rows:
            structured_chat_parts.append(
                f"<STRUCTURED_CHAT_WITNESS {removal.get('proposal_id')}\n"
                + "\n".join(rows)
                + "\n</STRUCTURED_CHAT_WITNESS>"
            )

    if not source_transcript_parts:
        return {"status": "FAIL", "reason_code": "SOURCE_TRANSCRIPT_MISSING"}

    transcript_parts: list[str] = []
    for piece_index, interval in enumerate(retained):
        piece_start = int(interval["start_ms"])
        piece_end = int(interval["end_ms"])
        if piece_index:
            removal = removals[piece_index - 1]
            transcript_parts.append(
                f"<JUMP {removal.get('proposal_id')} "
                f"{removal.get('source_start_ms')}-{removal.get('source_end_ms')}ms>"
            )
        for position, cue in enumerate(cues, start=1):
            cue_start = int(getattr(cue, "source_start_ms"))
            cue_end = int(getattr(cue, "source_end_ms"))
            if cue_start < piece_end and cue_end > piece_start:
                transcript_parts.append(
                    f"#{position} [{cue_start}-{cue_end}ms] {_cue_text(cue)}"
                )

    prompt = """你是谈话切片的最终语义否决器。下面的字幕已按一个固定方案删除少量片段，
<JUMP> 是已确定且不可移动的跳点。你只能判断保留内容是否仍完整，不能建议新剪点或改写字幕。
逐项检查：主题上下文和铺垫是否完整、因果/问答链是否完整、所有代词指代是否有来源、
纠正/笑点/结论/自然衔接是否保留、观众反馈是否完整保留、弹幕互动是否仍然前后一致、
有意义的重复回应是否仍然保留、删除是否改变主播原意或立场。
尤其不要把 SC/礼物引发的实质回答、主题相关的谢礼物、笑点铺垫或收尾当作 unrelated aside；
观众反馈、接梗、二次回应和有意义的重复不能因为前面已经出现过就删除；
必须把原始选中区间和每个 REMOVED 段的原话与保留后的字幕逐一对照；只有确认被删段
确实属于允许的 gift_thanks、housekeeping，或有绑定 STRUCTURED_CHAT_WITNESS 的 unrelated_sc、
且 REMOVED 原话完整包含该 witness 的 SC 文本（removed_text_match=true），确认主播实际在读这条 SC；
不能只因候选附近存在另一条 SC 就放行，
与主题无关且没有承载上述受保护内容，且删除后保留文本仍完整时才 pass。
如果原始区间、删除段原话或字幕覆盖不完整，按不确定处理为 fail。

只输出 JSON：
{"topic_complete":true或false,"cause_answer_chain_complete":true或false,
"referents_resolved":true或false,"corrections_preserved":true或false,
"punchline_preserved":true或false,"closing_resolution_preserved":true或false,
"audience_interactions_consistent":true或false,"audience_feedback_preserved":true或false,
"meaningful_repeats_preserved":true或false,
"meaning_or_stance_changed":true或false,"pass":true或false,"reason":"简述"}

原始完整选中区间（所有可见原始字幕；REMOVED 标记仅表示固定删除区间）：
<SOURCE """ + f"{source_start_ms}-{source_end_ms}ms>\n" + ("\n".join(source_transcript_parts) or "(未提供原始字幕覆盖，必须 fail)") + """
</SOURCE>

固定删除区间及删除前原话：
""" + ("\n".join(removed_transcript_parts) or "(未提供删除区间原话，必须 fail)") + """

绑定结构化弹幕/SC 原始证据（只作核对输入，不能由模型自称替代）：
""" + ("\n".join(structured_chat_parts) or "(本计划没有绑定 SC 证据)") + """

保留后的字幕：
""" + "\n".join(transcript_parts)
    from src.autoslice.llm_client import extract_json_object

    try:
        payload = extract_json_object(llm_call(prompt))
    except Exception as exc:  # noqa: BLE001 - provider/parse failure is a veto
        return {
            "status": "FAIL",
            "reason_code": "GLOBAL_VERIFIER_UNAVAILABLE_OR_INVALID",
            "error": str(exc)[:500],
        }
    passed = (
        payload.get("pass") is True
        and payload.get("meaning_or_stance_changed") is False
        and all(payload.get(field) is True for field in _GLOBAL_REQUIRED_TRUE_FIELDS)
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "reason_code": (
            "GLOBAL_SEMANTIC_INVARIANTS_PASSED"
            if passed
            else "GLOBAL_SEMANTIC_INVARIANT_FAILED"
        ),
        "response": payload,
    }


def build_piece_specs(
    *,
    item: Mapping[str, object],
    plan: Mapping[str, object],
    pre_ms: int,
    post_ms: int,
) -> list[dict[str, object]]:
    retained = plan.get("retained_intervals")
    if not isinstance(retained, list) or not retained:
        raise ValueError("filler plan has no retained intervals")
    segment_duration_ms = int(item.get("seg_dur_ms") or 0)
    structured_chat_fields = (
        "chat_jsonl_sha256",
        "chat_origin_epoch_ms",
        "chat_timeline_offset_ms",
        "structured_chat_required",
        "chat_source_alias_id",
        "chat_canonical_recording_basename",
        "chat_binding_status",
        "chat_binding_authority",
    )
    pieces: list[dict[str, object]] = []
    for index, interval in enumerate(retained):
        if not isinstance(interval, Mapping):
            raise ValueError("retained interval must be an object")
        start_ms = int(interval["start_ms"])
        end_ms = int(interval["end_ms"])
        if index == 0:
            start_ms = max(0, start_ms - pre_ms)
        if index == len(retained) - 1:
            end_ms = min(segment_duration_ms, end_ms + post_ms) if segment_duration_ms else end_ms + post_ms
        pieces.append(
            {
                "remote_media": str(item["segment_path"]),
                "start_ms": start_ms,
                "end_ms": end_ms,
                **(
                    {"danmaku_xml_local": str(item["xml"])}
                    if item.get("xml")
                    else {}
                ),
                **(
                    {"chat_jsonl_local": str(item["chat_jsonl"])}
                    if item.get("chat_jsonl")
                    else {}
                ),
                **{
                    field: item[field]
                    for field in structured_chat_fields
                    if field in item
                },
            }
        )
    return pieces


def write_final_filler_audit(
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    final_start_ms: int,
    final_end_ms: int,
    piece_provenance_rows: Sequence[Mapping[str, object]],
    branding_intro: Mapping[str, object] | None,
    output_path: Path,
) -> Path | None:
    plan = spec.get("talk_filler_plan")
    if (
        not isinstance(plan, Mapping)
        or not str(plan.get("status") or "").startswith("active")
    ):
        return None
    # A boundary-witness-reserve piece (cross-segment, appended only to widen
    # the boundary review's forward context) is never one of the retained
    # content intervals this filler plan maps removals across — exclude it
    # from the parallel durations/provenance sequences so the invariant below
    # keeps meaning "gaps between the N content pieces", unchanged.
    pieces_raw = spec.get("pieces")
    if not isinstance(pieces_raw, list):
        raise RuntimeError("TALK_FILLER_AUDIT_PIECE_MAPPING_INVALID")
    content_durations = content_only(pieces_raw, durations)
    content_provenance_rows = content_only(pieces_raw, piece_provenance_rows)
    removals = plan.get("removals")
    if (
        not isinstance(removals, list)
        or len(content_durations) != len(removals) + 1
    ):
        raise RuntimeError("TALK_FILLER_AUDIT_PIECE_MAPPING_INVALID")

    intro_offset_ms = 0
    if isinstance(branding_intro, Mapping):
        video = branding_intro.get("video")
        if isinstance(video, Mapping):
            intro_offset_ms = int(video.get("duration_ms") or 0)

    finalized: list[dict[str, object]] = []
    concat_elapsed_ms = 0
    for index, removal in enumerate(removals):
        concat_elapsed_ms += int(content_durations[index])
        content_output_jump_ms = concat_elapsed_ms - final_start_ms
        row = dict(removal) if isinstance(removal, Mapping) else {}
        row.update(
            {
                "actual_concat_jump_ms": concat_elapsed_ms,
                "final_content_output_jump_ms": content_output_jump_ms,
                "delivered_output_jump_ms": (
                    intro_offset_ms + content_output_jump_ms
                    if 0 < content_output_jump_ms < final_end_ms - final_start_ms
                    else None
                ),
                "survives_final_boundary": (
                    0 < content_output_jump_ms < final_end_ms - final_start_ms
                ),
                "left_piece_output_sha256": content_provenance_rows[index].get(
                    "output_sha256"
                ),
                "right_piece_output_sha256": content_provenance_rows[
                    index + 1
                ].get("output_sha256"),
            }
        )
        finalized.append(row)

    document = {
        "schema_version": FILLER_AUDIT_SCHEMA,
        "candidate_id": spec.get("candidate_id"),
        "status": "FINALIZED",
        "source_srt_sha256": plan.get("source_srt_sha256"),
        "final_start_ms_on_concat": final_start_ms,
        "final_end_ms_on_concat": final_end_ms,
        "effective_content_duration_ms": final_end_ms - final_start_ms,
        "branding_intro_offset_ms": intro_offset_ms,
        "retained_intervals": plan.get("retained_intervals"),
        "removals": finalized,
        "rejected_proposals": plan.get("rejected_proposals"),
        "policy": plan.get("policy"),
        "piece_provenance": list(piece_provenance_rows),
    }
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def bind_final_filler_audit_to_burn(
    *,
    audit_path: Path | None,
    burned_preview: Mapping[str, object] | None,
) -> Path | None:
    """Bind delivered jump positions to the intro offset from the actual burn."""
    if audit_path is None:
        return None
    if not audit_path.is_file():
        raise RuntimeError("TALK_FILLER_AUDIT_MISSING_BEFORE_BURN_BINDING")

    intro_offset_ms = 0
    if isinstance(burned_preview, Mapping):
        branding_intro = burned_preview.get("branding_intro")
        if isinstance(branding_intro, Mapping):
            intro_offset_ms = int(branding_intro.get("intro_offset_ms") or 0)
            if intro_offset_ms < 0:
                raise RuntimeError("TALK_FILLER_AUDIT_NEGATIVE_INTRO_OFFSET")

    document = json.loads(audit_path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != FILLER_AUDIT_SCHEMA
        or document.get("status") != "FINALIZED"
    ):
        raise RuntimeError("TALK_FILLER_AUDIT_INVALID_BEFORE_BURN_BINDING")

    removals = document.get("removals")
    if not isinstance(removals, list):
        raise RuntimeError("TALK_FILLER_AUDIT_REMOVALS_INVALID")
    for removal in removals:
        if not isinstance(removal, dict):
            raise RuntimeError("TALK_FILLER_AUDIT_REMOVAL_INVALID")
        content_jump_ms = removal.get("final_content_output_jump_ms")
        survives = removal.get("survives_final_boundary") is True
        removal["delivered_output_jump_ms"] = (
            intro_offset_ms + int(content_jump_ms)
            if survives and isinstance(content_jump_ms, int)
            else None
        )

    document["branding_intro_offset_ms"] = intro_offset_ms
    audit_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return audit_path
