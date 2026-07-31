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
    canonicalize_hard_surfaces,
    normalize_chat_text,
    sanitize_chat_display_text,
)
from src.autoslice.chat_read_aloud_candidates import (
    find_best_read_aloud_candidate as _find_best_read_aloud_candidate,
    partial_whole_line_rejection as _partial_whole_line_rejection,
    read_aloud_support_receipts as _read_aloud_support_receipts,
    read_aloud_support_scores,
)
from src.autoslice.read_aloud_arbitration import (
    _arbitrate_read_aloud_near_match as _arbitrate_read_aloud_near_match,
    proposal_alignment_basis,
    whole_line_exact_copy_gate,
)
from src.autoslice.chat_repair import (
    _aligned_span_replacements,
    _apply_gift_name_repairs,
    _best_text_split,
    _fragment_spoken_in,
    _shift_boundary_punct,
    _strip_interjections_once,
    _strip_unrenderable_for_subtitle,
)
from src.autoslice.chat_sender_repairs import (
    _apply_guard_sender_repairs,
    _apply_sc_sender_repairs,
    _apply_time_anchored_thanks_sender_repairs,
    audit_named_thanks_record_coverage,
)

_read_aloud_support_scores = read_aloud_support_scores


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
        index + 1 for index in range(proposal["start"], proposal["start"] + proposal["count"])
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
        "context_start_ms": max(
            0, cues[proposal["start"]].start_ms - 1_500
        ),
        "context_end_ms": (
            cues[proposal["start"] + proposal["count"] - 1].end_ms
            + 1_500
        ),
        "source_media_timeline_offset_ms": 0,
        "matched_audio_text": acoustic_span,
    }
    if (
        len(matched_groups) != 1
        or len(matched_groups[0][1]["canonicals"]) != 1
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
    chat_occurrences = chat_match["occurrences"]
    chat_surfaces = {
        str(row["surface"]).lower()
        for row in chat_occurrences
    }
    acoustic_occurrences = _entity_occurrences(acoustic_span, group)
    # 同一条结构化聊天里重复同一个规范词面，不是多个实体候选。若当前
    # 语义文本也逐槽保留了相同 canonical，直接记为双文本一致；否则单次
    # entity forced-choice 无法证明“出现几次/落在哪个槽”，必须保持未决。
    # 不能把整条 SC 交给 whole-line copy：主播可能只是读后改述，entity
    # 证据无权补入未逐字说出的其余正文。
    if len(chat_occurrences) != 1:
        repeated_same_surface = len(chat_surfaces) == 1
        repeated_canonical_surface = repeated_same_surface and all(
            str(row["surface"]).lower() == chat_canonical.lower()
            for row in chat_occurrences
        )
        semantic_repetition_matches = bool(
            repeated_canonical_surface
            and len(acoustic_occurrences) == len(chat_occurrences)
            and all(
                str(row["canonical"]).lower() == chat_canonical.lower()
                and str(row["surface"]).lower() == chat_canonical.lower()
                for row in acoustic_occurrences
            )
        )
        if semantic_repetition_matches:
            proposal["entity_group"] = group
            proposal["structured_chat_canonical"] = chat_canonical
            discovery.entity_verdicts.append(
                {
                    **base_row,
                    "reason_code": (
                        "ENTITY_REPETITION_CORROBORATED_BY_CHAT_AND_SEMANTIC_TEXT"
                    ),
                    "structured_chat_canonical": chat_canonical,
                    "structured_chat_occurrence_count": len(chat_occurrences),
                    "semantic_text_occurrence_count": len(acoustic_occurrences),
                    "authority_kind": "structured_chat_plus_semantic_text",
                }
            )
            if proposal.get("owner_eligible") is True:
                discovery.proposals.append(proposal)
            else:
                discovery.superseded_chat_proposals.append(
                    {
                        **_partial_whole_line_rejection(
                            item,
                            proposal,
                            cues=cues,
                            matched_audio_text=acoustic_span,
                        ),
                        "structured_chat_canonical": chat_canonical,
                    }
                )
            return True
        if not repeated_same_surface:
            discovery.entity_verdict_required.append(
                {
                    **base_row,
                    "reason_code": "ENTITY_VERDICT_AMBIGUOUS_CHAT_ENTITY",
                }
            )
            return True
        discovery.entity_verdict_required.append(
            {
                **base_row,
                "reason_code": (
                    "REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED"
                ),
                "structured_chat_canonical": chat_canonical,
                "structured_chat_occurrence_count": len(chat_occurrences),
                "semantic_text_occurrence_count": len(acoustic_occurrences),
            }
        )
        return True

    chat_surface = chat_occurrences[0]["surface"]
    # 平台结构化原文给出 canonical，且已经过 AGY/CPA/词表的语义文本
    # 命中同一实体时，后置声学模型没有未决 referent 可裁。别名面只在该
    # canonical 显式列入 uncertain_keep_canonicals 时走这条路；真实冲突
    # （如结构化 kmx、语义文本却命中乒乓球）仍进入音频仲裁。
    semantic_entity = acoustic_occurrences[0] if len(acoustic_occurrences) == 1 else None
    canonical_is_directionally_trusted = any(
        value.lower() == chat_canonical.lower() for value in group.uncertain_keep_canonicals
    )
    semantic_surface_is_canonical = bool(
        semantic_entity and str(semantic_entity["surface"]).lower() == chat_canonical.lower()
    )
    if (
        chat_surface.lower() == chat_canonical.lower()
        and semantic_entity is not None
        and str(semantic_entity["canonical"]).lower() == chat_canonical.lower()
        and (semantic_surface_is_canonical or canonical_is_directionally_trusted)
    ):
        proposal["entity_group"] = group
        proposal["structured_chat_canonical"] = chat_canonical
        reason_code = (
            "ENTITY_CANONICAL_CORROBORATED_BY_CHAT_AND_SEMANTIC_TEXT"
            if semantic_surface_is_canonical
            else "ENTITY_CANONICAL_CORROBORATED_BY_EXACT_CHAT_AND_REGISTERED_SEMANTIC_ALIAS"
        )
        discovery.entity_verdicts.append(
            {
                **base_row,
                "reason_code": reason_code,
                "structured_chat_canonical": chat_canonical,
                "semantic_text_canonical": semantic_entity["canonical"],
                "semantic_text_surface": semantic_entity["surface"],
                "authority_kind": "structured_chat_plus_semantic_text",
            }
        )
        if proposal.get("owner_eligible") is True:
            discovery.proposals.append(proposal)
            return True
        discovery.superseded_chat_proposals.append(
            {
                **_partial_whole_line_rejection(
                    item,
                    proposal,
                    cues=cues,
                    matched_audio_text=acoustic_span,
                ),
                "structured_chat_canonical": chat_canonical,
            }
        )
        if not semantic_surface_is_canonical:
            proposal["mode"] = "entity_only"
            proposal["entity_verdict"] = {
                "schema_version": "chat-entity-verdict.v1",
                "status": "RESOLVED",
                "canonical_entity": chat_canonical,
                "reason_code": reason_code,
                "authority_kind": "structured_chat_plus_semantic_text",
            }
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
                "reason_code": str((verdict or {}).get("reason_code") or "ENTITY_VERDICT_REQUIRED"),
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
    if proposal.get("owner_eligible") is True:
        discovery.proposals.append(proposal)
        return True
    discovery.superseded_chat_proposals.append(
        {
            **_partial_whole_line_rejection(
                item,
                proposal,
                cues=cues,
                matched_audio_text=acoustic_span,
            ),
            "structured_chat_canonical": chat_canonical,
            "resolved_canonical": verdict["canonical_entity"],
        }
    )
    if (
        len(acoustic_occurrences) == 1
        and str(acoustic_occurrences[0]["surface"]).lower() != chat_canonical.lower()
    ):
        proposal["mode"] = "entity_only"
        discovery.proposals.append(proposal)
    return True


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
    # 近失仲裁：先全量收集→按(score,precision)排序→同跨度去重→再花预算
    # （2026-07-20 脑海案：按到达序消费时晚段真念读被早段低分近失饿死）。
    near_queue: list[tuple[ChatEvidence, dict[str, Any]]] = []
    high_confidence_arbitration_attempts = 0
    for item in evidence:
        if item.kind in {"gift", "guard"}:
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
        support_receipts = _read_aloud_support_receipts(
            item,
            support_srt_texts=support_srt_texts,
            max_cues=max_cues,
        )
        if best is not None:
            best["support_receipts"] = support_receipts
            best["support_scores"] = [
                float(receipt["score"])
                for receipt in support_receipts
                if receipt.get("owner_eligible") is True
                or receipt.get("question_particle_patch_supported") is True
            ]
            acoustic_span = "".join(texts[best["start"] : best["start"] + best["count"]])
            whole_line_gate = whole_line_exact_copy_gate(
                item,
                best,
                acoustic_span,
            )
            best["owner_eligible"] = bool(whole_line_gate["owner_eligible"])
            best["whole_line_exact_copy_gate"] = whole_line_gate
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
            if best["mode"] == "question_particle_patch" and (
                best["support_scores"] or best.get("thread_anchored")
            ):
                discovery.proposals.append(best)
            elif best.get("owner_eligible") is True:
                discovery.proposals.append(best)
            elif (
                entity_verifier is not None
                and high_confidence_arbitration_attempts < 4
                and best["mode"] == "exact_span"
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
            elif best["mode"] == "exact_span" and support_receipts:
                discovery.superseded_chat_proposals.append(
                    _partial_whole_line_rejection(
                        item,
                        best,
                        cues=cues,
                        matched_audio_text=acoustic_span,
                    )
                )
                if item.kind == "superchat" and any(
                    receipt.get("source_truth_anchor_eligible") is True
                    for receipt in support_receipts
                ):
                    discovery.proposals.append(
                        {
                            **best,
                            "mode": "source_truth_anchor_only",
                        }
                    )
        elif best_near is not None and entity_verifier is not None:
            best_near["support_receipts"] = support_receipts
            best_near["support_scores"] = [
                float(receipt["score"])
                for receipt in support_receipts
                if receipt.get("owner_eligible") is True
            ]
            near_queue.append((item, best_near))
    best_by_span: dict[tuple[int, int], tuple[ChatEvidence, dict[str, Any]]] = {}
    for item, proposal in near_queue:
        span = (proposal["start"], proposal["count"])
        incumbent = best_by_span.get(span)
        if incumbent is None or (proposal["score"], proposal["precision"]) > (
            incumbent[1]["score"],
            incumbent[1]["precision"],
        ):
            best_by_span[span] = (item, proposal)
    ranked_near = sorted(
        best_by_span.values(),
        key=lambda row: (row[1]["score"], row[1]["precision"]),
        reverse=True,
    )
    for item, proposal in ranked_near[:3]:
        _arbitrate_read_aloud_near_match(
            item,
            proposal,
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
        if proposal["mode"] == "source_truth_anchor_only":
            result.applied_proposals.append(proposal)
            continue
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
                        "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
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
                        "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
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
                    "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
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

        if proposal["mode"] == "exact_span" and proposal.get("owner_eligible") is not True:
            discovery.superseded_chat_proposals.append(
                _partial_whole_line_rejection(
                    item,
                    proposal,
                    cues=cues,
                    matched_audio_text="".join(before),
                )
            )
            continue
        if proposal["mode"] == "question_particle_patch":
            replacements = [proposal["replacement"]]
        else:
            span_end = proposal["start"] + proposal["count"]
            aligned = _aligned_span_replacements(
                item.text,
                before,
                prev_context="".join(texts[max(0, proposal["start"] - 2) : proposal["start"]]),
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
                "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
                "mode": proposal["mode"],
                "score": round(proposal["score"], 4),
                "coverage": round(proposal["coverage"], 4),
                "audio_transcript_support_count": len(proposal.get("support_scores") or []),
                "audio_transcript_support_scores": [
                    round(score, 4) for score in proposal.get("support_scores") or []
                ],
                "audio_transcript_supports": [
                    dict(receipt) for receipt in proposal.get("support_receipts") or []
                ],
                "owner_eligible": bool(proposal.get("owner_eligible")),
                "whole_line_exact_copy_gate": proposal.get("whole_line_exact_copy_gate"),
                "alignment_basis": proposal_alignment_basis(proposal),
                "entity_verdict": proposal.get("entity_verdict"),
                "read_aloud_verdict": proposal.get("read_aloud_verdict"),
                "span_alignment": proposal.get("span_alignment"),
                "thread_anchored": bool(proposal.get("thread_anchored")),
                "before": before,
                "after": replacements,
            }
        )
    return result



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
            if any(
                surface.lower() in before_text.lower() for surface in expected_entity.surfaces
            ) or any(marker in before_text for marker in contrast_markers):
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
    thanks_record_coverage: list[dict[str, Any]]
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
            required_substring=expected,
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
    elif parts.entity_repairs and not all(row["survived"] for row in parts.entity_repairs):
        status = "FAILED"
    elif (
        parts.applied
        or parts.entity_repairs
        or parts.sender_repairs
        or parts.coreference_repairs
        or gift_text_changed
    ):
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
        "thanks_record_coverage": parts.thanks_record_coverage,
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
        entity_verifier=entity_verifier,
    )
    guard_sender_repairs, guard_sender_verdict_required = _apply_guard_sender_repairs(
        evidence,
        cues,
        texts,
        entity_verifier=entity_verifier,
    )
    sender_repairs.extend(guard_sender_repairs)
    sender_verdict_required.extend(guard_sender_verdict_required)
    time_anchor_repairs, time_anchor_verdicts = (
        _apply_time_anchored_thanks_sender_repairs(
            evidence=evidence,
            cues=cues,
            texts=texts,
            repaired_cue_indexes={
                int(row["cue_index"]) - 1 for row in sender_repairs
            },
        )
    )
    sender_repairs.extend(time_anchor_repairs)
    sender_verdict_required.extend(time_anchor_verdicts)
    thanks_record_coverage = audit_named_thanks_record_coverage(
        evidence=evidence,
        cues=cues,
        texts=texts,
        repaired_cue_indexes={
            int(row["cue_index"]) - 1 for row in sender_repairs
        },
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
            thanks_record_coverage=thanks_record_coverage,
            coreference_repairs=coreference_repairs,
            gift_repairs=gift_repairs,
        ),
    )
