"""Chat proposal discovery, arbitration, and final application."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.chat_evidence import (
    ChatEvidence,
    EntityVerifier,
    ReferentGroup,
    _coerce_referent_groups,
    _entity_occurrences,
    _render_srt,
    _request_sha256,
    _validated_entity_verdict,
    _validated_read_aloud_verdict,
    canonicalize_hard_surfaces,
    normalize_chat_text,
    sanitize_chat_display_text,
)
from src.autoslice.chat_repair import (
    _aligned_span_replacements,
    _apply_gift_name_repairs,
    _authority_tail_continues_in_next_cue,
    _best_text_split,
    _excess_is_mid_read_interjection,
    _fragment_spoken_in,
    _mask_compatible_sender,
    _match_metrics,
    _matched_read_prefix,
    _partial_question_patch,
    _repair_sc_sender,
    _sender_thank_anchor,
    _shift_boundary_punct,
    _spoken_sender_alias,
    _strip_interjections_once,
    _strip_unrenderable_for_subtitle,
)

def _find_best_read_aloud_candidate(
    item: ChatEvidence,
    *,
    evidence: Sequence[ChatEvidence],
    cues: Sequence[Any],
    texts: Sequence[str],
    max_cues: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Find the safest exact span and the best audio-arbitration fallback."""

    authority_norm = normalize_chat_text(item.text)
    best: dict[str, Any] | None = None
    best_near: dict[str, Any] | None = None
    sender_anchor_indexes: set[int] = (
        {
            index
            for index, text in enumerate(texts)
            if _sender_thank_anchor(text, item.sender)
        }
        if item.kind == "superchat" and item.sender
        else set()
    )
    # SC 线程延续（2026-07-13 利安/无马懿 实案）：观众先用 SC 提问，随后
    # 同一人用普通弹幕接龙（弹幕名被打码，只能掩码兼容+时间窗联结）。
    # 这类弹幕是高先验念读对象——她会直接念出来。
    thread_anchor = bool(
        item.kind == "danmaku"
        and item.sender
        and any(
            other.kind == "superchat"
            and _mask_compatible_sender(other.sender, item.sender)
            and 0 <= item.offset_ms - other.offset_ms <= 600_000
            for other in evidence
        )
    )
    for start in range(len(cues)):
        if item.kind == "danmaku":
            if item.offset_ms >= 0:
                delay = cues[start].start_ms - item.offset_ms
                if delay < -2_000 or delay > 90_000:
                    continue
            elif cues[start].start_ms > 90_000:
                continue
        elif item.offset_ms >= 0 and cues[start].start_ms < item.offset_ms - 2_000:
            continue
        for count in range(1, min(max_cues, len(cues) - start) + 1):
            candidate_parts = texts[start : start + count]
            candidate = "".join(candidate_parts)
            cue_boundaries: set[int] = set()
            cursor = 0
            for part in candidate_parts[:-1]:
                cursor += len(part)
                cue_boundaries.add(cursor)
            score, ratio, coverage, precision, common = _match_metrics(item.text, candidate)
            preserved_suffix = _matched_read_prefix(
                item.text,
                candidate,
                cue_boundaries=cue_boundaries,
            )
            if preserved_suffix is not None:
                score, ratio, coverage, precision, common = _match_metrics(
                    item.text, preserved_suffix[0]
                )
            matched_candidate = preserved_suffix[0] if preserved_suffix is not None else candidate
            extent = len(normalize_chat_text(matched_candidate)) / max(1, len(authority_norm))
            full = (
                len(authority_norm) >= 4
                and score >= 0.68
                and coverage >= 0.60
                and precision >= 0.52
                and extent >= 0.82
                and common >= min(6, len(authority_norm))
            )
            # SC 线程弹幕按念读处理（Ivan：这是她念的弹幕，不需要听出来）。
            # 谐音梗让字符相似度结构性失效；线程、时间窗和长度构成直接支持。
            thread_delay_ms = (
                cues[start].start_ms - item.offset_ms if item.offset_ms >= 0 else None
            )
            thread_full = (
                thread_anchor
                and count <= 2
                and len(authority_norm) >= 4
                and thread_delay_ms is not None
                and -2_000 <= thread_delay_ms <= 45_000
                and 0.7 <= extent <= 1.4
                and common >= 2
            )
            if thread_full and not full:
                full = True
            if (
                full
                and len(normalize_chat_text(candidate)) > len(authority_norm) + 1
                and preserved_suffix is None
                and not _excess_is_mid_read_interjection(item.text, candidate)
            ):
                full = False
            partial = None
            if count == 1 and item.kind == "danmaku":
                near = item.offset_ms < 0 or cues[start].start_ms - item.offset_ms <= 20_000
                partial = _partial_question_patch(item.text, candidate) if near else None
            if not full and partial is None:
                # 文本 near-miss 交给原始音频二选一，不放宽 exact-span 本身。
                sender_anchored = bool(
                    item.kind == "superchat"
                    and sender_anchor_indexes
                    and any(0 <= start - index <= 4 for index in sender_anchor_indexes)
                )
                if (
                    item.kind == "danmaku"
                    and count <= 2
                    and len(authority_norm) >= 6
                    and score >= 0.55
                    and coverage >= 0.50
                    and common >= 4
                ) or (
                    sender_anchored
                    and count <= 4
                    and len(authority_norm) >= 4
                    and score >= 0.40
                    and coverage >= 0.35
                    and common >= 3
                ):
                    near_candidate = {
                        "evidence": item,
                        "start": start,
                        "count": count,
                        "score": score,
                        "ratio": ratio,
                        "coverage": coverage,
                        "precision": precision,
                        "common_chars": common,
                        "mode": "read_aloud_arbitration",
                        "replacement": None,
                        "preserved_suffix": None,
                        "sender_anchored": sender_anchored,
                    }
                    if best_near is None or (count, -score) < (
                        best_near["count"],
                        -best_near["score"],
                    ):
                        best_near = near_candidate
                continue
            if (
                full
                and start + count < len(cues)
                and _authority_tail_continues_in_next_cue(
                    item.text,
                    candidate,
                    texts[start + count],
                )
            ):
                continue
            proposal = {
                "evidence": item,
                "start": start,
                "count": count,
                "score": score,
                "ratio": ratio,
                "coverage": coverage,
                "precision": precision,
                "common_chars": common,
                "mode": "exact_span" if full else "question_particle_patch",
                "replacement": partial,
                "preserved_suffix": preserved_suffix,
                "thread_anchored": thread_full,
            }
            # The first viable cue span is safest: adding a later cue can
            # consume the beginning of an acoustic reply.
            if best is None or (count, -score) < (best["count"], -best["score"]):
                best = proposal
    return best, best_near


def _read_aloud_support_scores(
    item: ChatEvidence,
    *,
    authority_norm: str,
    support_srt_texts: Sequence[str],
    max_cues: int,
) -> list[float]:
    """Collect independent transcript support for one structured chat item."""

    support_scores: list[float] = []
    for support_text in support_srt_texts:
        support_cues = [cue for cue in parse_srt_cues(support_text) if cue.text.strip()]
        best_support = 0.0
        for support_start in range(len(support_cues)):
            if item.kind == "danmaku" and item.offset_ms >= 0:
                delay = support_cues[support_start].start_ms - item.offset_ms
                if delay < -2_000 or delay > 90_000:
                    continue
            for support_count in range(
                1,
                min(max_cues, len(support_cues) - support_start) + 1,
            ):
                support_candidate = "".join(
                    cue.text
                    for cue in support_cues[
                        support_start : support_start + support_count
                    ]
                )
                support_score, _ratio, support_coverage, _precision, support_common = (
                    _match_metrics(item.text, support_candidate)
                )
                support_extent = len(normalize_chat_text(support_candidate)) / max(
                    1,
                    len(authority_norm),
                )
                # SCs are commonly paraphrased and degraded by ASR, so their
                # coverage floor is slightly lower than ordinary danmaku.
                support_coverage_floor = 0.65 if item.kind == "superchat" else 0.72
                if (
                    support_score >= 0.62
                    and support_coverage >= support_coverage_floor
                    and support_extent >= 0.82
                    and support_common >= min(6, len(authority_norm))
                ):
                    best_support = max(best_support, support_score)
                elif (
                    support_count == 1
                    and item.kind == "danmaku"
                    and _partial_question_patch(item.text, support_candidate) is not None
                ):
                    best_support = max(best_support, 0.62)
        if best_support:
            support_scores.append(best_support)
    return support_scores


@dataclass
class _ChatProposalDiscovery:
    proposals: list[dict[str, Any]] = field(default_factory=list)
    entity_verdicts: list[dict[str, Any]] = field(default_factory=list)
    entity_verdict_required: list[dict[str, Any]] = field(default_factory=list)
    pending_text_overrides: list[dict[str, Any]] = field(default_factory=list)
    superseded_chat_proposals: list[dict[str, Any]] = field(default_factory=list)
    read_aloud_arbitrations: list[dict[str, Any]] = field(default_factory=list)


def _resolve_chat_entity_proposal(
    item: ChatEvidence,
    proposal: dict[str, Any],
    *,
    acoustic_span: str,
    entity_groups: Sequence[ReferentGroup],
    cues: Sequence[Any],
    entity_verifier: EntityVerifier | None,
    discovery: _ChatProposalDiscovery,
) -> bool:
    """Resolve a confusable entity proposal; return whether it was consumed."""

    matched_groups: list[tuple[ReferentGroup, dict[str, Any]]] = []
    for group in entity_groups:
        occurrences = _entity_occurrences(item.text, group)
        if occurrences:
            matched_groups.append(
                (
                    group,
                    {
                        "occurrences": occurrences,
                        "canonicals": sorted({row["canonical"] for row in occurrences}),
                    },
                )
            )
    if not matched_groups:
        return False

    cue_indexes = [
        index + 1
        for index in range(proposal["start"], proposal["start"] + proposal["count"])
    ]
    base_row = {
        "evidence_id": item.evidence_id,
        "kind": item.kind,
        "source": item.source,
        "source_sha256": item.source_sha256,
        "source_event_id": item.source_event_id,
        "source_offset_ms": item.offset_ms,
        "exact_text": item.text,
        "cue_indexes": cue_indexes,
        "matched_start_ms": cues[proposal["start"]].start_ms,
        "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
        "matched_audio_text": acoustic_span,
    }
    if (
        len(matched_groups) != 1
        or len(matched_groups[0][1]["canonicals"]) != 1
        or len(matched_groups[0][1]["occurrences"]) != 1
    ):
        discovery.entity_verdict_required.append(
            {
                **base_row,
                "reason_code": "ENTITY_VERDICT_AMBIGUOUS_CHAT_ENTITY",
            }
        )
        return True

    group, chat_match = matched_groups[0]
    chat_canonical = chat_match["canonicals"][0]
    chat_surface = chat_match["occurrences"][0]["surface"]
    acoustic_occurrences = _entity_occurrences(acoustic_span, group)
    # 平台结构化原文与已经过 AGY/CPA/词表的语义文本逐字同意 canonical
    # 时，后置声学模型没有未决问题可裁，不能把两份一致文本证据一起推翻。
    # 真实冲突（两边实体不同或弹幕只给了别名面）仍进入音频仲裁。
    if (
        chat_surface.lower() == chat_canonical.lower()
        and len(acoustic_occurrences) == 1
        and str(acoustic_occurrences[0]["canonical"]).lower() == chat_canonical.lower()
        and str(acoustic_occurrences[0]["surface"]).lower() == chat_canonical.lower()
    ):
        proposal["entity_group"] = group
        proposal["structured_chat_canonical"] = chat_canonical
        discovery.entity_verdicts.append(
            {
                **base_row,
                "reason_code": "ENTITY_CANONICAL_CORROBORATED_BY_CHAT_AND_SEMANTIC_TEXT",
                "structured_chat_canonical": chat_canonical,
                "semantic_text_canonical": acoustic_occurrences[0]["canonical"],
                "authority_kind": "structured_chat_plus_semantic_text",
            }
        )
        discovery.proposals.append(proposal)
        return True
    request: dict[str, Any] = {
        "schema_version": "chat-entity-verification-request.v1",
        **base_row,
        "structured_chat_canonical": chat_canonical,
        "structured_chat_surface": chat_surface,
        "candidate_entities": [
            {
                "canonical": entity.canonical,
                "surfaces": list(entity.surfaces),
                "readings": list(entity.readings),
            }
            for entity in group.entities
        ],
        "reason": group.reason,
    }
    request["request_sha256"] = _request_sha256(request)
    try:
        raw_verdict = entity_verifier(request) if entity_verifier is not None else None
    except Exception as exc:  # verifier failure is evidence, never permission
        raw_verdict = {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "UNCERTAIN",
            "reason_code": "ENTITY_VERIFIER_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }
    verdict = _validated_entity_verdict(raw_verdict, request=request, group=group)
    if verdict is None or verdict.get("status") != "RESOLVED":
        discovery.entity_verdict_required.append(
            {
                **base_row,
                "request": request,
                "verdict": verdict or raw_verdict,
                "reason_code": str(
                    (verdict or {}).get("reason_code") or "ENTITY_VERDICT_REQUIRED"
                ),
            }
        )
        return True

    resolved = {**base_row, "request": request, "verdict": verdict}
    discovery.entity_verdicts.append(resolved)
    if verdict["authority_kind"] == "ivan_text_override":
        discovery.pending_text_overrides.append(resolved)
        discovery.superseded_chat_proposals.append(
            {
                **base_row,
                "reason_code": "EXACT_CHAT_SUPERSEDED_BY_IVAN_TEXT_OVERRIDE",
                "structured_chat_canonical": chat_canonical,
                "resolved_canonical": verdict["canonical_entity"],
            }
        )
        return True

    proposal["entity_group"] = group
    proposal["entity_verdict"] = verdict
    proposal["structured_chat_canonical"] = chat_canonical
    if verdict["canonical_entity"] != chat_canonical:
        proposal["mode"] = "entity_only"
        discovery.superseded_chat_proposals.append(
            {
                **base_row,
                "reason_code": "EXACT_CHAT_REJECTED_BY_AUDIO_ENTITY_VERDICT",
                "structured_chat_canonical": chat_canonical,
                "resolved_canonical": verdict["canonical_entity"],
            }
        )
    discovery.proposals.append(proposal)
    return True


def _arbitrate_read_aloud_near_match(
    item: ChatEvidence,
    proposal: dict[str, Any],
    *,
    cues: Sequence[Any],
    texts: Sequence[str],
    entity_verifier: EntityVerifier,
    discovery: _ChatProposalDiscovery,
) -> None:
    """Run one raw-audio forced choice for a bounded near-match."""

    near_span = "".join(texts[proposal["start"] : proposal["start"] + proposal["count"]])
    request = {
        "schema_version": "chat-read-aloud-verification-request.v1",
        "evidence_id": item.evidence_id,
        "kind": item.kind,
        "source": item.source,
        "source_sha256": item.source_sha256,
        "source_event_id": item.source_event_id,
        "source_offset_ms": item.offset_ms,
        "exact_text": item.text,
        "cue_indexes": [
            index + 1
            for index in range(proposal["start"], proposal["start"] + proposal["count"])
        ],
        "matched_start_ms": cues[proposal["start"]].start_ms,
        "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
        "matched_audio_text": near_span,
        "candidate_entities": [
            {"canonical": item.text, "surfaces": [], "readings": []},
            {"canonical": near_span, "surfaces": [], "readings": []},
        ],
        "context_before": texts[proposal["start"] - 1] if proposal["start"] > 0 else "",
        "context_after": texts[proposal["start"] + proposal["count"]]
        if proposal["start"] + proposal["count"] < len(texts)
        else "",
        "reason": "danmaku near-miss read-aloud arbitration",
    }
    request["request_sha256"] = _request_sha256(request)
    try:
        raw_verdict = entity_verifier(request)
    except Exception as exc:  # verifier failure is evidence, never permission
        raw_verdict = {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "UNCERTAIN",
            "reason_code": "READ_ALOUD_VERIFIER_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }
    verdict = _validated_read_aloud_verdict(raw_verdict, request=request)
    arbitration_row = {
        "evidence_id": item.evidence_id,
        "kind": item.kind,
        "exact_text": item.text,
        "matched_audio_text": near_span,
        "cue_indexes": request["cue_indexes"],
        "matched_start_ms": request["matched_start_ms"],
        "matched_end_ms": request["matched_end_ms"],
        "score": round(proposal["score"], 4),
        "coverage": round(proposal["coverage"], 4),
        "sender_anchored": bool(proposal.get("sender_anchored")),
        "request_sha256": request["request_sha256"],
        "verdict": verdict if verdict is not None else raw_verdict,
    }
    if verdict is not None and verdict.get("canonical_entity") == item.text:
        arbitration_row["outcome"] = "authority_confirmed_by_audio"
        proposal["mode"] = "exact_span"
        proposal["read_aloud_verdict"] = verdict
        proposal["support_scores"] = []
        discovery.proposals.append(proposal)
    elif verdict is not None:
        arbitration_row["outcome"] = "acoustic_span_confirmed_by_audio"
        discovery.superseded_chat_proposals.append(
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "exact_text": item.text,
                "cue_indexes": request["cue_indexes"],
                "matched_start_ms": request["matched_start_ms"],
                "matched_end_ms": request["matched_end_ms"],
                "reason_code": "EXACT_CHAT_REJECTED_BY_READ_ALOUD_AUDIO",
            }
        )
    else:
        arbitration_row["outcome"] = "uncertain_no_change"
    discovery.read_aloud_arbitrations.append(arbitration_row)


def _discover_chat_proposals(
    evidence: Sequence[ChatEvidence],
    *,
    cues: Sequence[Any],
    texts: Sequence[str],
    entity_groups: Sequence[ReferentGroup],
    max_cues: int,
    support_srt_texts: Sequence[str],
    entity_verifier: EntityVerifier | None,
) -> _ChatProposalDiscovery:
    """Discover candidate spans while keeping every uncertain case fail-closed."""

    discovery = _ChatProposalDiscovery()
    arbitration_attempts = 0
    high_confidence_arbitration_attempts = 0
    for item in evidence:
        if item.kind == "gift":
            continue
        authority_norm = normalize_chat_text(item.text)
        if len(authority_norm) < 4:
            continue
        best, best_near = _find_best_read_aloud_candidate(
            item,
            evidence=evidence,
            cues=cues,
            texts=texts,
            max_cues=max_cues,
        )
        if best is not None:
            support_scores = _read_aloud_support_scores(
                item,
                authority_norm=authority_norm,
                support_srt_texts=support_srt_texts,
                max_cues=max_cues,
            )
            best["support_scores"] = support_scores
            acoustic_span = "".join(texts[best["start"] : best["start"] + best["count"]])
            if _resolve_chat_entity_proposal(
                item,
                best,
                acoustic_span=acoustic_span,
                entity_groups=entity_groups,
                cues=cues,
                entity_verifier=entity_verifier,
                discovery=discovery,
            ):
                continue
            if support_scores or best.get("thread_anchored"):
                discovery.proposals.append(best)
            elif (
                entity_verifier is not None
                and high_confidence_arbitration_attempts < 4
                and best["count"] <= 2
                and best["score"] >= 0.80
                and best["coverage"] >= 0.80
                and best["precision"] >= 0.70
            ):
                # A very close, same-time danmaku read can still differ in one
                # semantically important word (2026-07-16:
                # “soyo就是妈” -> “soyo是真妈”).  Independent ASR support is
                # often the same correlated mis-hearing, so do not silently
                # discard the structured exact text.  Spend a separate bounded
                # raw-audio arbitration budget; the exact chat is applied only
                # when the verifier selects it.
                high_confidence_arbitration_attempts += 1
                _arbitrate_read_aloud_near_match(
                    item,
                    best,
                    cues=cues,
                    texts=texts,
                    entity_verifier=entity_verifier,
                    discovery=discovery,
                )
        elif best_near is not None and entity_verifier is not None and arbitration_attempts < 3:
            arbitration_attempts += 1
            _arbitrate_read_aloud_near_match(
                item,
                best_near,
                cues=cues,
                texts=texts,
                entity_verifier=entity_verifier,
                discovery=discovery,
            )
    discovery.proposals.sort(
        key=lambda row: (row["score"], -row["count"]),
        reverse=True,
    )
    return discovery


@dataclass
class _AppliedChatProposals:
    occupied: set[int] = field(default_factory=set)
    applied: list[dict[str, Any]] = field(default_factory=list)
    applied_proposals: list[dict[str, Any]] = field(default_factory=list)
    entity_repairs: list[dict[str, Any]] = field(default_factory=list)
    coreference_anchors: list[dict[str, Any]] = field(default_factory=list)


def _apply_chat_proposals(
    proposals: Sequence[dict[str, Any]],
    *,
    cues: Sequence[Any],
    texts: list[str],
    discovery: _ChatProposalDiscovery,
) -> _AppliedChatProposals:
    """Apply non-overlapping proposals and record every resulting mutation."""

    result = _AppliedChatProposals()
    for proposal in proposals:
        indexes = set(range(proposal["start"], proposal["start"] + proposal["count"]))
        if indexes & result.occupied:
            continue
        item = proposal["evidence"]
        before = texts[proposal["start"] : proposal["start"] + proposal["count"]]
        if proposal["mode"] == "entity_only":
            group: ReferentGroup = proposal["entity_group"]
            expected_canonical = str(proposal["entity_verdict"]["canonical_entity"])
            source_occurrences = _entity_occurrences("".join(before), group)
            if len(source_occurrences) != 1:
                if source_occurrences and all(
                    row.get("canonical") == expected_canonical
                    and row.get("surface") == expected_canonical
                    for row in source_occurrences
                ):
                    discovery.entity_verdicts.append(
                        {
                            "evidence_id": item.evidence_id,
                            "cue_indexes": [index + 1 for index in sorted(indexes)],
                            "reason_code": "ENTITY_ALREADY_CANONICAL_EVERYWHERE",
                            "verdict": proposal["entity_verdict"],
                        }
                    )
                    continue
                discovery.entity_verdict_required.append(
                    {
                        "evidence_id": item.evidence_id,
                        "exact_text": item.text,
                        "cue_indexes": [index + 1 for index in sorted(indexes)],
                        "matched_start_ms": cues[proposal["start"]].start_ms,
                        "matched_end_ms": cues[
                            proposal["start"] + proposal["count"] - 1
                        ].end_ms,
                        "matched_audio_text": "".join(before),
                        "reason_code": "ENTITY_SLOT_AMBIGUOUS_FOR_MINIMAL_REPAIR",
                        "verdict": proposal["entity_verdict"],
                    }
                )
                continue
            candidates = sorted(
                (
                    (surface, entity.canonical)
                    for entity in group.entities
                    for surface in entity.surfaces
                    if surface
                ),
                key=lambda pair: len(pair[0]),
                reverse=True,
            )
            replacements = list(before)
            replaced_surface: str | None = None
            replaced_canonical: str | None = None
            for local_index, before_text in enumerate(before):
                match_row = next(
                    (
                        (surface, canonical)
                        for surface, canonical in candidates
                        if re.search(re.escape(surface), before_text, flags=re.IGNORECASE)
                    ),
                    None,
                )
                if match_row is None:
                    continue
                replaced_surface, replaced_canonical = match_row
                replacements[local_index] = re.sub(
                    re.escape(replaced_surface),
                    expected_canonical,
                    before_text,
                    count=1,
                    flags=re.IGNORECASE,
                )
                break
            if replaced_surface is None:
                discovery.entity_verdict_required.append(
                    {
                        "evidence_id": item.evidence_id,
                        "exact_text": item.text,
                        "cue_indexes": [index + 1 for index in sorted(indexes)],
                        "matched_start_ms": cues[proposal["start"]].start_ms,
                        "matched_end_ms": cues[
                            proposal["start"] + proposal["count"] - 1
                        ].end_ms,
                        "matched_audio_text": "".join(before),
                        "reason_code": "ENTITY_SLOT_NOT_FOUND_FOR_MINIMAL_REPAIR",
                        "verdict": proposal["entity_verdict"],
                    }
                )
                continue
            texts[proposal["start"] : proposal["start"] + proposal["count"]] = replacements
            result.occupied.update(indexes)
            result.entity_repairs.append(
                {
                    "evidence_id": item.evidence_id,
                    "kind": item.kind,
                    "source": item.source,
                    "source_sha256": item.source_sha256,
                    "source_event_id": item.source_event_id,
                    "source_offset_ms": item.offset_ms,
                    "cue_indexes": [index + 1 for index in sorted(indexes)],
                    "matched_start_ms": cues[proposal["start"]].start_ms,
                    "matched_end_ms": cues[
                        proposal["start"] + proposal["count"] - 1
                    ].end_ms,
                    "mode": "entity_only",
                    "expected_entity": expected_canonical,
                    "replaced_entity": replaced_canonical,
                    "replaced_surface": replaced_surface,
                    "before": before,
                    "after": replacements,
                    "verdict": proposal["entity_verdict"],
                }
            )
            result.coreference_anchors.append(
                {
                    "proposal": proposal,
                    "group": group,
                    "expected_canonical": expected_canonical,
                    "expected_surface": expected_canonical,
                }
            )
            continue

        if proposal["mode"] == "question_particle_patch":
            replacements = [proposal["replacement"]]
        else:
            span_end = proposal["start"] + proposal["count"]
            aligned = _aligned_span_replacements(
                item.text,
                before,
                prev_context="".join(
                    texts[max(0, proposal["start"] - 2) : proposal["start"]]
                ),
                next_context="".join(texts[span_end : span_end + 2]),
            )
            if aligned is not None:
                replacements, span_alignment = aligned
                proposal["span_alignment"] = span_alignment
            else:
                replacements = _best_text_split(
                    _strip_unrenderable_for_subtitle(item.text),
                    before,
                )
                if proposal.get("preserved_suffix") is not None:
                    replacements[-1] = (
                        replacements[-1].rstrip("，,。！？!? ")
                        + "，"
                        + proposal["preserved_suffix"][1]
                    )
                replacements = _shift_boundary_punct(replacements)
        texts[proposal["start"] : proposal["start"] + proposal["count"]] = replacements
        result.occupied.update(indexes)
        result.applied_proposals.append(proposal)
        if proposal.get("entity_group") is not None:
            result.coreference_anchors.append(
                {
                    "proposal": proposal,
                    "group": proposal["entity_group"],
                    "expected_canonical": proposal["structured_chat_canonical"],
                    "expected_surface": next(
                        row["surface"]
                        for row in _entity_occurrences(item.text, proposal["entity_group"])
                        if row["canonical"] == proposal["structured_chat_canonical"]
                    ),
                }
            )
        result.applied.append(
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "sender": item.sender,
                "source": item.source,
                "source_sha256": item.source_sha256,
                "source_event_id": item.source_event_id,
                "source_offset_ms": item.offset_ms,
                "exact_text": item.text,
                "cue_indexes": [index + 1 for index in sorted(indexes)],
                "matched_start_ms": cues[proposal["start"]].start_ms,
                "matched_end_ms": cues[
                    proposal["start"] + proposal["count"] - 1
                ].end_ms,
                "mode": proposal["mode"],
                "score": round(proposal["score"], 4),
                "coverage": round(proposal["coverage"], 4),
                "audio_transcript_support_count": len(
                    proposal.get("support_scores") or []
                ),
                "audio_transcript_support_scores": [
                    round(score, 4) for score in proposal.get("support_scores") or []
                ],
                "alignment_basis": (
                    "raw-audio-forced-choice.v1"
                    if proposal.get("entity_verdict") is not None
                    or proposal.get("read_aloud_verdict") is not None
                    else "audio-derived-transcript-proxy.v1"
                ),
                "entity_verdict": proposal.get("entity_verdict"),
                "read_aloud_verdict": proposal.get("read_aloud_verdict"),
                "span_alignment": proposal.get("span_alignment"),
                "thread_anchored": bool(proposal.get("thread_anchored")),
                "before": before,
                "after": replacements,
            }
        )
    return result


def _apply_sc_sender_repairs(
    *,
    applied_proposals: Sequence[Mapping[str, Any]],
    evidence: Sequence[ChatEvidence],
    cues: Sequence[Any],
    texts: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Repair only the thank-name slot bound to an already matched SC body."""

    sender_repairs: list[dict[str, Any]] = []
    sender_verdict_required: list[dict[str, Any]] = []
    for proposal in applied_proposals:
        item = proposal["evidence"]
        if item.kind != "superchat" or not item.sender:
            continue
        repair_candidate: tuple[int, str] | None = None
        for index in range(proposal["start"] - 1, max(-1, proposal["start"] - 3), -1):
            if index < 0 or cues[proposal["start"]].start_ms - cues[index].end_ms > 10_000:
                break
            repaired = _repair_sc_sender(texts[index], item.sender)
            if repaired is not None:
                repair_candidate = (index, repaired)
                break
        if repair_candidate is None:
            continue
        index, repaired = repair_candidate
        matched_cue_start = cues[proposal["start"]].start_ms
        same_body_events = [
            other
            for other in evidence
            if other.kind == "superchat"
            and normalize_chat_text(other.text) == normalize_chat_text(item.text)
            and (other.offset_ms < 0 or matched_cue_start >= other.offset_ms - 2_000)
        ]
        event_identities = {
            str(other.source_event_id or other.evidence_id) for other in same_body_events
        }
        spoken_senders = {
            _spoken_sender_alias(other.sender).lower()
            for other in same_body_events
            if _spoken_sender_alias(other.sender)
        }
        if len(event_identities) > 1 and len(spoken_senders) > 1:
            sender_verdict_required.append(
                {
                    "evidence_id": item.evidence_id,
                    "source_event_id": item.source_event_id,
                    "thank_cue_index": index + 1,
                    "cue_indexes": [
                        index + 1
                        for index in range(
                            proposal["start"],
                            proposal["start"] + proposal["count"],
                        )
                    ],
                    "matched_start_ms": cues[proposal["start"]].start_ms,
                    "matched_end_ms": cues[
                        proposal["start"] + proposal["count"] - 1
                    ].end_ms,
                    "reason_code": "DUPLICATE_SC_BODY_SENDER_AMBIGUOUS",
                    "candidate_event_ids": sorted(event_identities),
                    "candidate_spoken_senders": sorted(spoken_senders),
                }
            )
            continue
        # The exact matched SC body identifies one concrete platform event;
        # its sender is direct authority for the narrow preceding name slot.
        before_text = texts[index]
        texts[index] = repaired
        sender_repairs.append(
            {
                "evidence_id": item.evidence_id,
                "source_event_id": item.source_event_id,
                "sender": item.sender,
                "spoken_sender": _spoken_sender_alias(item.sender),
                "cue_index": index + 1,
                "matched_start_ms": cues[index].start_ms,
                "matched_end_ms": cues[index].end_ms,
                "before": before_text,
                "after": repaired,
                "alignment_basis": "matched-superchat-body-plus-platform-sender.v1",
            }
        )
    return sender_repairs, sender_verdict_required


def _apply_chat_coreference_repairs(
    *,
    anchors: Sequence[Mapping[str, Any]],
    cues: Sequence[Any],
    texts: list[str],
    occupied: set[int],
) -> list[dict[str, Any]]:
    """Repair one immediate reply when its entity conflicts with a bound read."""

    repairs: list[dict[str, Any]] = []
    contrast_markers = ("不是", "而是", "还是", "或者", "对比", "相比")
    for anchor in anchors:
        proposal = anchor["proposal"]
        item = proposal["evidence"]
        group: ReferentGroup = anchor["group"]
        expected_canonical = anchor["expected_canonical"]
        expected = anchor["expected_surface"]
        alternatives = sorted(
            (
                surface
                for entity in group.entities
                if entity.canonical != expected_canonical
                for surface in entity.surfaces
            ),
            key=len,
            reverse=True,
        )
        if not alternatives:
            continue
        prior_end = cues[proposal["start"] + proposal["count"] - 1].end_ms
        reply_stop = min(
            len(cues),
            proposal["start"] + proposal["count"] + 2,
        )
        for index in range(proposal["start"] + proposal["count"], reply_stop):
            if index in occupied or cues[index].start_ms - prior_end > 8_000:
                break
            before_text = texts[index]
            expected_entity = next(
                entity for entity in group.entities if entity.canonical == expected_canonical
            )
            if (
                any(
                    surface.lower() in before_text.lower()
                    for surface in expected_entity.surfaces
                )
                or any(marker in before_text for marker in contrast_markers)
            ):
                break
            found = next(
                (alt for alt in alternatives if alt.lower() in before_text.lower()),
                None,
            )
            if found is None:
                break
            after_text = re.sub(
                re.escape(found),
                expected,
                before_text,
                flags=re.IGNORECASE,
            )
            texts[index] = after_text
            repairs.append(
                {
                    "evidence_id": item.evidence_id,
                    "expected_entity": expected,
                    "expected_canonical_entity": expected_canonical,
                    "replaced_confusable": found,
                    "cue_index": index + 1,
                    "matched_start_ms": cues[index].start_ms,
                    "matched_end_ms": cues[index].end_ms,
                    "before": before_text,
                    "after": after_text,
                }
            )
            break
    return repairs


@dataclass(frozen=True)
class _ChatAuthorityAuditParts:
    evidence_considered: int
    applied: list[dict[str, Any]]
    entity_verdicts: list[dict[str, Any]]
    entity_verdict_required: list[dict[str, Any]]
    pending_text_overrides: list[dict[str, Any]]
    superseded_chat_proposals: list[dict[str, Any]]
    read_aloud_arbitrations: list[dict[str, Any]]
    entity_repairs: list[dict[str, Any]]
    sender_repairs: list[dict[str, Any]]
    sender_verdict_required: list[dict[str, Any]]
    coreference_repairs: list[dict[str, Any]]
    gift_repairs: list[dict[str, Any]]


def _finalize_chat_authority_output(
    srt_text: str,
    *,
    cues: Sequence[Any],
    texts: list[str],
    parts: _ChatAuthorityAuditParts,
) -> tuple[str, dict[str, Any]]:
    """Render the repaired SRT and derive its fail-closed terminal audit."""

    gift_text_changed = any(
        row.get("outcome") == "gift_name_repaired" for row in parts.gift_repairs
    )
    output = (
        _render_srt(cues, texts)
        if (
            parts.applied
            or parts.entity_repairs
            or parts.sender_repairs
            or parts.coreference_repairs
            or gift_text_changed
        )
        else srt_text
    )
    for row in parts.applied:
        span_text = "".join(texts[index - 1] for index in row["cue_indexes"])
        expected = normalize_chat_text(row["exact_text"])
        alignment = row.get("span_alignment") or {}
        head = normalize_chat_text(alignment.get("dropped_duplicate_authority_head") or "")
        tail = normalize_chat_text(alignment.get("dropped_duplicate_authority_tail") or "")
        if head and expected.startswith(head):
            expected = expected[len(head) :]
        if tail and expected.endswith(tail):
            expected = expected[: len(expected) - len(tail)]
        span_lo = min(row["cue_indexes"]) - 1
        span_hi = max(row["cue_indexes"])
        dropped_ok = True
        if head:
            prev_context = "".join(texts[max(0, span_lo - 2) : span_lo])
            dropped_ok = dropped_ok and _fragment_spoken_in(
                str(alignment.get("dropped_duplicate_authority_head")),
                prev_context,
            )
        if tail:
            next_context = "".join(texts[span_hi : span_hi + 2])
            dropped_ok = dropped_ok and _fragment_spoken_in(
                str(alignment.get("dropped_duplicate_authority_tail")),
                next_context,
            )
        span_check = _strip_interjections_once(
            normalize_chat_text(span_text),
            alignment.get("preserved_span_interjections"),
        )
        row["survived"] = bool(expected) and expected in span_check and dropped_ok
    for row in parts.entity_repairs:
        span_text = "".join(texts[index - 1] for index in row["cue_indexes"])
        row["survived"] = normalize_chat_text(row["expected_entity"]) in normalize_chat_text(
            span_text
        )
    if parts.sender_verdict_required:
        status = "SC_SENDER_VERDICT_REQUIRED"
    elif parts.entity_verdict_required:
        status = "ENTITY_VERDICT_REQUIRED"
    elif parts.pending_text_overrides:
        status = "PENDING_TEXT_OVERRIDE"
    elif parts.applied and not all(row["survived"] for row in parts.applied):
        status = "FAILED"
    elif parts.entity_repairs and not all(
        row["survived"] for row in parts.entity_repairs
    ):
        status = "FAILED"
    elif parts.applied or parts.entity_repairs:
        status = "APPLIED_AND_VERIFIED"
    else:
        status = "NO_MATCH"
    audit = {
        "schema_version": "chat-authority-audit.v2",
        "status": status,
        "evidence_considered": parts.evidence_considered,
        "input_srt_sha256": hashlib.sha256(srt_text.encode("utf-8")).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "applied": parts.applied,
        "entity_verdicts": parts.entity_verdicts,
        "entity_verdict_required": parts.entity_verdict_required,
        "pending_text_overrides": parts.pending_text_overrides,
        "superseded_chat_proposals": parts.superseded_chat_proposals,
        "read_aloud_arbitrations": parts.read_aloud_arbitrations,
        "entity_repairs": parts.entity_repairs,
        "sender_repairs": parts.sender_repairs,
        "sender_verdict_required": parts.sender_verdict_required,
        "coreference_repairs": parts.coreference_repairs,
        "gift_repairs": parts.gift_repairs,
    }
    return output, audit


def apply_authoritative_chat_evidence(
    srt_text: str,
    evidence: Iterable[ChatEvidence],
    *,
    max_cues: int = 4,
    support_srt_texts: Sequence[str] = (),
    referent_groups: Sequence[ReferentGroup | Sequence[str]] = (),
    entity_verifier: EntityVerifier | None = None,
) -> tuple[str, dict]:
    """Apply only high-confidence exact read-aloud spans to an SRT.

    The match combines source time and character-sequence similarity.  It does
    not treat nearby chat as a command and never asks an LLM to execute it.
    Replies remain untouched, except for the narrow case where ASR merged a
    read question missing only its final particle with the reply in one cue.
    """

    # Normalize every caller, not only JSONL ingestion: XML, fixtures, and
    # future adapters must not be able to inject SRT blocks/control sequences
    # into the output even when their spoken words genuinely match the audio.
    # 梗词硬规范同样作用于证据文本（Ivan 2026-07-13：观众弹幕原文写「直女」
    # 也是同一个梗，逐字注入前先回正为「侄女」——否则 verbatim 权威会把草稿里
    # 已规范化的写法改回去）。
    evidence = [
        ChatEvidence(
            item.kind,
            item.offset_ms,
            canonicalize_hard_surfaces(sanitize_chat_display_text(item.text)),
            sanitize_chat_display_text(item.sender, max_chars=100),
            item.source,
            item.source_sha256,
            item.source_event_id,
        )
        for item in evidence
        if sanitize_chat_display_text(item.text)
    ]
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    # 位置门控组只属于全文本审计；chat 证据里这些高频普通词不构成实体槽位。
    entity_groups = [
        group for group in _coerce_referent_groups(referent_groups) if not group.positions
    ]
    discovery = _discover_chat_proposals(
        evidence,
        cues=cues,
        texts=texts,
        entity_groups=entity_groups,
        max_cues=max_cues,
        support_srt_texts=support_srt_texts,
        entity_verifier=entity_verifier,
    )
    proposals = discovery.proposals
    entity_verdicts = discovery.entity_verdicts
    entity_verdict_required = discovery.entity_verdict_required
    pending_text_overrides = discovery.pending_text_overrides
    superseded_chat_proposals = discovery.superseded_chat_proposals
    read_aloud_arbitrations = discovery.read_aloud_arbitrations
    applied_result = _apply_chat_proposals(
        proposals,
        cues=cues,
        texts=texts,
        discovery=discovery,
    )
    occupied = applied_result.occupied
    applied = applied_result.applied
    applied_proposals = applied_result.applied_proposals
    entity_repairs = applied_result.entity_repairs
    coreference_anchors = applied_result.coreference_anchors

    sender_repairs, sender_verdict_required = _apply_sc_sender_repairs(
        applied_proposals=applied_proposals,
        evidence=evidence,
        cues=cues,
        texts=texts,
    )

    gift_repairs = _apply_gift_name_repairs(evidence, cues, texts, entity_verifier=entity_verifier)

    coreference_repairs = _apply_chat_coreference_repairs(
        anchors=coreference_anchors,
        cues=cues,
        texts=texts,
        occupied=occupied,
    )

    return _finalize_chat_authority_output(
        srt_text,
        cues=cues,
        texts=texts,
        parts=_ChatAuthorityAuditParts(
            evidence_considered=len(evidence),
            applied=applied,
            entity_verdicts=entity_verdicts,
            entity_verdict_required=entity_verdict_required,
            pending_text_overrides=pending_text_overrides,
            superseded_chat_proposals=superseded_chat_proposals,
            read_aloud_arbitrations=read_aloud_arbitrations,
            entity_repairs=entity_repairs,
            sender_repairs=sender_repairs,
            sender_verdict_required=sender_verdict_required,
            coreference_repairs=coreference_repairs,
            gift_repairs=gift_repairs,
        ),
    )
