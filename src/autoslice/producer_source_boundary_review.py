"""Thin source-boundary review router for the producer text pipeline."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.clip_context import clip_context_prompt_text
from src.autoslice.final_review_auditor import _final_review_structured_context
from src.autoslice.frozen_source_boundary_receipt import (
    load_boundary_review_authorities,
)
from src.autoslice.llm_client import extract_json_object
from src.autoslice.producer_boundary_review_stage import (
    review_final_boundary_semantics,
)
from src.autoslice.producer_final_review_transport import (
    build_final_review_llm_call,
)
from src.autoslice.reviewed_exact_source_interval import (
    authority_from_spec,
    build_source_semantic_review,
)


def attach_current_timing_qa_projection(
    source_cues: Sequence[object],
    current_source_cues: Sequence[object],
    projection: dict[str, object],
    *,
    resolution: Mapping[str, object],
    resolution_sha256: str,
) -> None:
    """Join retained normal timing output to current text by exact geometry.

    The caller reads and hash-checks the original resolution.  This is proposal
    evidence for a fresh semantic review; it grants no closure or CLEAN state.
    """
    timing = resolution.get("timing_qa")
    sanitized = resolution.get("sanitized_cues")
    digest = resolution_sha256.removeprefix("sha256:")
    if (
        not isinstance(timing, Mapping)
        or timing.get("schema_version") != "subtitle-timing-qa.v3"
        or not isinstance(timing.get("actions"), list)
        or not isinstance(sanitized, list)
        or resolution.get("final_start") != projection.get("source_final_start_ms")
        or resolution.get("final_end") != projection.get("source_final_end_ms")
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("SOURCE_REVIEW_CURRENT_TIMING_RESOLUTION_INVALID")
    source_by_interval: dict[tuple[int, int], list[object]] = {}
    current_by_interval: dict[tuple[int, int], list[object]] = {}
    sanitized_by_id: dict[str, list[Mapping[str, object]]] = {}
    for cue in source_cues:
        source_by_interval.setdefault((cue.start_ms, cue.end_ms), []).append(cue)
    for cue in current_source_cues:
        current_by_interval.setdefault((cue.start_ms, cue.end_ms), []).append(cue)
    for row in sanitized:
        if not isinstance(row, Mapping) or not isinstance(row.get("cue_id"), str):
            raise ValueError("SOURCE_REVIEW_CURRENT_TIMING_SANITIZED_INVALID")
        sanitized_by_id.setdefault(row["cue_id"], []).append(row)
    rows = []
    seen_ids = set()
    for action in timing["actions"]:
        if not isinstance(action, Mapping) or action.get("action") != "retime":
            continue
        cue_id = action.get("cue_id")
        before, after = action.get("before"), action.get("after")
        if not isinstance(cue_id, str) or cue_id in seen_ids:
            raise ValueError("SOURCE_REVIEW_CURRENT_TIMING_ACTION_AMBIGUOUS")
        seen_ids.add(cue_id)
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            raise ValueError("SOURCE_REVIEW_CURRENT_TIMING_ACTION_INVALID")
        geometries = [before.get(k) for k in ("start_ms", "end_ms")]
        geometries += [after.get(k) for k in ("start_ms", "end_ms")]
        if any(isinstance(v, bool) or not isinstance(v, int) for v in geometries):
            raise ValueError("SOURCE_REVIEW_CURRENT_TIMING_ACTION_INVALID")
        original = source_by_interval.get(tuple(geometries[:2]), [])
        final = sanitized_by_id.get(cue_id, [])
        if len(original) != 1 or len(final) != 1:
            raise ValueError("SOURCE_REVIEW_CURRENT_TIMING_SOURCE_UNBOUND")
        source, final = original[0], final[0]
        start, end = final.get("source_start_ms"), final.get("source_end_ms")
        if (
            any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end))
            or not geometries[2] <= start < end <= geometries[3]
            or final.get("text") != source.text
            or action.get("text") != source.text[:40]
        ):
            raise ValueError("SOURCE_REVIEW_CURRENT_TIMING_SANITIZED_DRIFT")
        current = current_by_interval.get((start, end), [])
        if len(current) > 1:
            raise ValueError("SOURCE_REVIEW_CURRENT_TIMING_CURRENT_AMBIGUOUS")
        if not current:
            continue  # A dropped or differently retimed current row acquires no claim.
        current = current[0]
        rows.append({
            "timing_cue_id": cue_id,
            "source_cue_index": str(source.index),
            "source_start_ms": source.start_ms,
            "source_end_ms": source.end_ms,
            "source_text": source.text,
            "current_cue_index": str(current.index),
            "current_source_start_ms": start,
            "current_source_end_ms": end,
            "current_text_sha256": "sha256:" + hashlib.sha256(current.text.encode()).hexdigest(),
        })
    projection["retained_timing_qa_evidence"] = {
        "role": "CURRENT_TEXT_PROPOSAL_NOT_FINAL_CLEAN",
        "resolution_sha256": resolution_sha256,
        "source_cue_grid_sha256": cue_grid_sha256(source_cues),
        "projected_source_cue_grid_sha256": cue_grid_sha256(current_source_cues),
        "rows": rows,
    }


def _current_text_context(
    source_cues: Sequence[object],
    current_source_cues: Sequence[object],
    projection: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    """Expose current text at exact source intervals without rewriting raw cues."""
    if (
        projection.get("schema_version") != "delivery-to-source-timeline-projection.v1"
        or projection.get("status") != "PASS"
        or projection.get("cue_count") != len(current_source_cues)
        or projection.get("projected_source_cue_grid_sha256")
        != cue_grid_sha256(current_source_cues)
    ):
        raise ValueError("SOURCE_REVIEW_CURRENT_TEXT_PROJECTION_DRIFT")
    by_interval: dict[tuple[int, int], list[object]] = {}
    for cue in source_cues:
        by_interval.setdefault((cue.start_ms, cue.end_ms), []).append(cue)
    changes = []
    timing_changes = []
    unmatched = []
    timing = projection.get("retained_timing_qa_evidence")
    if timing is not None and (
        not isinstance(timing, Mapping)
        or timing.get("source_cue_grid_sha256") != cue_grid_sha256(source_cues)
        or timing.get("projected_source_cue_grid_sha256") != cue_grid_sha256(current_source_cues)
        or not isinstance(timing.get("rows"), list)
        or any(not isinstance(row, Mapping) for row in timing["rows"])
    ):
        raise ValueError("SOURCE_REVIEW_CURRENT_TIMING_PROJECTION_DRIFT")
    for cue in current_source_cues:
        matches = by_interval.get((cue.start_ms, cue.end_ms), [])
        if len(matches) != 1:
            mapped = [row for row in timing["rows"] if
                      row.get("current_cue_index") == str(cue.index)
                      and row.get("current_source_start_ms") == cue.start_ms
                      and row.get("current_source_end_ms") == cue.end_ms] if timing else []
            if len(mapped) == 1:
                row = mapped[0]
                if row.get("current_text_sha256") != "sha256:" + hashlib.sha256(cue.text.encode()).hexdigest():
                    raise ValueError("SOURCE_REVIEW_CURRENT_TIMING_TEXT_DRIFT")
                timing_changes.append({**row, "current_text": cue.text})
                continue
            unmatched.append(str(cue.index))
            continue
        source = matches[0]
        if source.text != cue.text:
            changes.append({
                "source_cue_index": int(source.index),
                "source_start_ms": source.start_ms,
                "source_end_ms": source.end_ms,
                "source_text": source.text,
                "current_text": cue.text,
            })
    evidence = {
        "role": "CURRENT_TEXT_PROPOSAL_NOT_FINAL_CLEAN",
        "source_cue_grid_sha256": cue_grid_sha256(source_cues),
        "delivery_cue_grid_sha256": projection.get("delivery_srt_cue_grid_sha256"),
        "projected_source_cue_grid_sha256": cue_grid_sha256(current_source_cues),
        "exact_interval_changes": changes,
        "unmatched_current_cue_indexes": unmatched,
        **({"timing_qa_interval_changes": timing_changes,
            "retained_timing_resolution_sha256": timing["resolution_sha256"]} if timing else {}),
    }
    if not changes and not timing_changes:
        return "", evidence
    context = (
        ("当前字幕续审证据：下列 current_text 是当前交付稿中已关联到源句的文字，"
         if timing_changes else
         "当前字幕续审证据：下列 current_text 是当前交付稿在完全相同源时间段的文字，")
        +
        "source_text 是保留的旧源听写。判断句法和故事时同时审视这份新文字证据，"
        "不要因旧听写乱码忽略现有修复，也不要自动认可新文字。"
        "这些文字尚未获得整片 CLEAN；若矛盾仍未解决，继续拒绝并说明疑点。"
        "只能选择绑定 cues 中的源 cue_index；此证据不增加推荐终点、不证明换题，"
        "不匹配的 current cue 不取得源语义证据资格。\n"
        + ("timing_qa_interval_changes 仅由保留的正常排版记录关联，源句及当前时间均保留，"
           "这是新文字提案，不能把排版延长当成说话延长或新增语义终点。\n"
           if timing_changes else "")
        + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    )
    return context, evidence


def review_source_boundary(
    *,
    spec: Mapping[str, object],
    candidate_id: str,
    cues: Sequence[object],
    boundary_target_ms: int,
    boundary_search_scope: Mapping[str, object],
    current_owner_contract: object,
    authoritative_chat: object,
    clip_context: object,
    available_local_source_context_end_ms: int,
    current_source_cues: Sequence[object] | None = None,
    current_delivery_projection: Mapping[str, object] | None = None,
) -> tuple[object, dict[str, object], dict[str, object], dict[str, object] | None]:
    """Return frozen receipt, current review, carry audit, and exact disclosure."""

    extension_proof = None
    if spec.get("source_witness_extension") is not None:
        from src.autoslice.source_witness_extension import consume_extension
        cues, clip_context, extension_proof = consume_extension(
            spec, cues=cues, scope=boundary_search_scope,
            owner_contract=current_owner_contract, clip_context=clip_context)
        available_local_source_context_end_ms = extension_proof["available_local_source_context_end_ms"]

    exact_authority = authority_from_spec(spec)
    if exact_authority is not None:
        review = build_source_semantic_review(
            spec=spec,
            authority=exact_authority,
            boundary_search_scope=boundary_search_scope,
        )
        disclosure = {
            "status": "PASS",
            "authority_sha256": exact_authority["authority_sha256"],
            "fresh_asr_role": "diagnostic_witness_only",
        }
        return None, review, {}, disclosure

    frozen_receipt, frozen_review = load_boundary_review_authorities(
        spec,
        candidate_id=candidate_id,
        current_owner_contract=current_owner_contract,
    )
    if extension_proof is not None:
        # The new complete grid requires a new ordinary review. Old receipts
        # remain history and cannot answer for previously unseen reserve.
        frozen_receipt, frozen_review = None, None
    replay: dict[str, object] = {}
    candidate_context = clip_context_prompt_text(clip_context)
    current_evidence = None
    if current_source_cues is not None:
        if current_delivery_projection is None:
            raise ValueError("SOURCE_REVIEW_CURRENT_TEXT_PROJECTION_MISSING")
        context, current_evidence = _current_text_context(
            cues, current_source_cues, current_delivery_projection
        )
        if context:
            candidate_context += "\n\n" + context
    llm_call = build_final_review_llm_call()
    # Bind producer evidence omitted from the rendered semantic prompt. A
    # changed source clock, owner or projection must not reuse the old response.
    llm_call.completed_review_binding = {  # type: ignore[attr-defined]
        "recording_date": spec.get("date"),
        "pieces": spec.get("pieces"),
        "current_owner_contract": current_owner_contract,
        "clip_context": clip_context,
        "current_delivery_projection": current_delivery_projection,
        "current_source_grid_sha256": (
            cue_grid_sha256(current_source_cues) if current_source_cues is not None else None
        ),
    }
    if extension_proof is not None:
        llm_call.completed_review_binding["source_witness_extension"] = extension_proof
    if spec.get("output_root"):
        llm_call.completed_review_root = (  # type: ignore[attr-defined]
            Path(str(spec["output_root"])).expanduser().absolute() / ".completed-review-observations"
        )
    review = review_final_boundary_semantics(
        cues=cues,
        boundary_target_ms=boundary_target_ms,
        candidate_id=candidate_id,
        selection_hook=str(spec.get("selection_hook") or ""),
        selection_scorecard=spec.get("selection_scorecard"),
        structured_context=_final_review_structured_context(
            selection_hook=str(spec.get("selection_hook") or ""),
            authoritative_chat=authoritative_chat,
        ),
        candidate_context=candidate_context,
        boundary_max_forward_ms=int(boundary_search_scope["recommendation_forward_ms"]),
        llm_call=llm_call,
        extract_json=extract_json_object,
        disabled=os.environ.get("AUTOSLICE_DISABLE_FINAL_REVIEW") == "1",
        boundary_search_scope=boundary_search_scope,
        available_local_source_context_end_ms=(available_local_source_context_end_ms),
        frozen_review=frozen_review,
        replay_audit=replay,
        **({"source_full_window_end_ms": available_local_source_context_end_ms}
           if extension_proof is not None else {}),
    )
    if extension_proof is not None:
        review["source_witness_extension"] = extension_proof
    if current_evidence is not None:
        review["current_delivery_text_evidence"] = current_evidence
    return frozen_receipt, review, replay, None
