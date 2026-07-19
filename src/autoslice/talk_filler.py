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


FILLER_PLAN_SCHEMA = "talk-filler-plan.v1"
FILLER_AUDIT_SCHEMA = "talk-filler-audit.v1"
MIN_TALK_EFFECTIVE_DURATION_MS = 45_000
MIN_AUTOMATIC_EFFECTIVE_DURATION_MS = 45_500
MIN_MODEL_CONFIDENCE = 0.94
MAX_AUTOMATIC_REMOVALS = 2
MAX_REVIEWED_REMOVALS = 3
MAX_SINGLE_REMOVAL_MS = 15_000
MAX_AUTOMATIC_REMOVED_RATIO = 0.15
MAX_AUTOMATIC_REMOVED_MS = 20_000
MIN_RETAINED_PIECE_MS = 6_000
MIN_DEAD_PAUSE_MS = 3_000
AUTOMATIC_EDGE_GUARD_MS = 5_000

_THANKS_RX = re.compile(
    r"(?:谢(?:谢|谢你|一下|大家|[^\s，。！？!?]{0,12}的)|感谢|"
    r"粉丝灯牌|钢蹦|钢镚|告白花束|音乐盒|舰长|上舰|礼物|"
    r"灵感多|阿里嘎多|ありがとう)",
    re.IGNORECASE,
)
_WELCOME_RX = re.compile(
    r"(?:^|[，。！？!?\s])(?:欢迎|晚上好|早上好|中午好|来了|来啦)"
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
)


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
        start_ms = int(getattr(previous, "source_end_ms"))
        end_ms = int(getattr(following, "source_start_ms"))
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
        if _SUBSTANTIVE_RX.search(joined):
            return "gift_thanks_contains_substantive_topic_language"
        if len(joined) > 120:
            return "gift_thanks_block_too_verbose"
        if not isinstance(semantic_checks, Mapping):
            return "semantic_discrete_checks_missing"
        if semantic_checks.get("topic_relation") not in {
            "incidental",
            "unrelated",
        }:
            return "semantic_topic_relation_not_removable"
        if any(semantic_checks.get(field) is not False for field in _SEMANTIC_DENY_FIELDS):
            return "semantic_protected_role_or_dependency_not_denied"
        return None
    if reason == "welcome_chatter" and mode == "remove_cues":
        if not _WELCOME_RX.search(joined):
            return "welcome_has_no_lexical_witness"
        if _SUBSTANTIVE_RX.search(joined) or len(joined) > 60:
            return "welcome_contains_substantive_topic_language"
        if not isinstance(semantic_checks, Mapping):
            return "semantic_discrete_checks_missing"
        if semantic_checks.get("topic_relation") not in {
            "incidental",
            "unrelated",
        }:
            return "semantic_topic_relation_not_removable"
        if any(semantic_checks.get(field) is not False for field in _SEMANTIC_DENY_FIELDS):
            return "semantic_protected_role_or_dependency_not_denied"
        return None
    if reason == "dead_pause" and mode == "gap_only":
        if int(removal["removed_duration_ms"]) < MIN_DEAD_PAUSE_MS:
            return "dead_pause_too_short"
        acoustic = removal.get("acoustic_evidence")
        required = (
            "asr_word_free",
            "vad_non_speech",
            "rms_silence",
            "no_laughter_or_applause",
            "no_music_occupancy",
            "no_danmaku_burst",
        )
        if not isinstance(acoustic, Mapping) or any(
            acoustic.get(field) is not True for field in required
        ):
            return "dead_pause_acoustic_proof_incomplete"
        return None
    if reason == "unrelated_aside":
        return "unrelated_aside_requires_later_semantic_authorizer"
    return "reason_or_mode_not_supported"


def _authorize_automatic_proposals(
    proposals: Sequence[Mapping[str, object]],
    cues: Sequence[object],
    *,
    start_ms: int,
    end_ms: int,
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
                "bridge_reason": str(row.get("bridge") or "Ivan reviewed jump"),
                "removed_cues": list(row.get("removed_cues") or []),
                "left_retained_context": row.get("left_retained_context"),
                "right_retained_context": row.get("right_retained_context"),
                "authority": str(row.get("authority") or "ivan_reviewed"),
                "authorization_kind": "reviewed",
            }
        )
    return accepted


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
    automatic_removal_count = 0
    reviewed_removal_count = 0
    for removal in accepted:
        is_reviewed = removal.get("authorization_kind") == "reviewed"
        if is_reviewed and reviewed_removal_count >= MAX_REVIEWED_REMOVALS:
            rejected.append(
                {
                    "proposal_id": removal["proposal_id"],
                    "reason_code": "REVIEWED_REMOVAL_COUNT_CAP_EXCEEDED",
                }
            )
            continue
        if not is_reviewed and automatic_removal_count >= MAX_AUTOMATIC_REMOVALS:
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
        if not is_reviewed and (
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
        proposed_removed = removed_duration_ms + int(removal["removed_duration_ms"])
        if not is_reviewed and (
            proposed_removed / original_duration_ms
            > MAX_AUTOMATIC_REMOVED_RATIO
            or proposed_removed > MAX_AUTOMATIC_REMOVED_MS
        ):
            rejected.append(
                {
                    "proposal_id": removal["proposal_id"],
                    "reason_code": "AUTOMATIC_REMOVAL_BUDGET_EXCEEDED",
                }
            )
            continue
        selected.append(removal)
        removed_duration_ms = proposed_removed
        if is_reviewed:
            reviewed_removal_count += 1
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
                "reason_code": "EFFECTIVE_DURATION_NOT_OVER_45S",
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

    transcript_parts: list[str] = []
    for piece_index, interval in enumerate(retained):
        if not isinstance(interval, Mapping):
            return {"status": "FAIL", "reason_code": "RETAINED_INTERVAL_INVALID"}
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
逐项检查：主题是否完整、因果/问答链是否完整、所有指代是否有来源、纠正/笑点/结论是否保留、
观众互动是否仍然前后一致、删除是否改变主播原意或立场。任一项不确定就 fail。

只输出 JSON：
{"topic_complete":true或false,"cause_answer_chain_complete":true或false,
"referents_resolved":true或false,"corrections_preserved":true或false,
"punchline_preserved":true或false,"closing_resolution_preserved":true或false,
"audience_interactions_consistent":true或false,
"meaning_or_stance_changed":true或false,"pass":true或false,"reason":"简述"}

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
    removals = plan.get("removals")
    if not isinstance(removals, list) or len(durations) != len(removals) + 1:
        raise RuntimeError("TALK_FILLER_AUDIT_PIECE_MAPPING_INVALID")

    intro_offset_ms = 0
    if isinstance(branding_intro, Mapping):
        video = branding_intro.get("video")
        if isinstance(video, Mapping):
            intro_offset_ms = int(video.get("duration_ms") or 0)

    finalized: list[dict[str, object]] = []
    concat_elapsed_ms = 0
    for index, removal in enumerate(removals):
        concat_elapsed_ms += int(durations[index])
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
                "left_piece_output_sha256": piece_provenance_rows[index].get(
                    "output_sha256"
                ),
                "right_piece_output_sha256": piece_provenance_rows[index + 1].get(
                    "output_sha256"
                ),
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
