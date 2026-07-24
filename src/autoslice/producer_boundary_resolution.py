"""Runtime orchestration for deterministic talk-clip boundary resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.boundary_semantic_review import (
    boundary_search_scope_is_valid,
    build_boundary_search_scope,
    cue_grid_sha256,
)
from src.autoslice.boundary_endpoint_binding import (
    bind_final_semantic_endpoint as _bind_final_semantic_endpoint,
)
from src.autoslice.producer_boundary import (
    BOUNDARY_REPAIR_EXTEND_CAP_MS,
    LEAD_AIR_MS,
    MAX_BOUNDARY_REPAIRS,
    SNAP_AFTER_MS,
    TAIL_PAD_MS,
    adaptive_tail_cut,
    boundary_audit,
    boundary_red_flags,
    needs_tail_refinement,
    next_clean_closure,
    repair_start_for_straddler,
    snap_start_to_sentence,
    syntactic_tail_audit,
    tail_requires_forward_extension,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.subtitle_timing_qa import sanitize_cue_timing


@dataclass(frozen=True)
class BoundaryResolutionAdapters:
    accurate_recut_command: Callable[..., list[str]]
    run_command: Callable[..., None]


@dataclass(frozen=True)
class InitialBoundary:
    cues: list[object]
    target_start_rel: int
    snapped_start: int | None
    final_start: int
    target_rel: int
    closure_selection_lower_bound_ms: int
    repair_search_origin_ms: int
    repair_max_end_ms: int
    snapped_end: int
    closure_cue: object
    refinement_used: bool
    manual_end_authority: str | None
    semantic_review: dict | None
    required_boundary_owners: list[dict[str, object]]
    required_owner_start_ms: int | None
    required_owner_end_ms: int | None


@dataclass(frozen=True)
class BoundaryResolution:
    final_start: int
    final_end: int
    audit: dict
    sanitized_cues: list[SourceCue]
    timing_qa: dict


def _required_boundary_owner_contract(
    spec: Mapping[str, object],
    *,
    padded_dur: int,
) -> tuple[list[dict[str, object]], int | None, int | None]:
    raw_owners = spec.get("required_boundary_owners")
    if raw_owners is None:
        return [], None, None
    if not isinstance(raw_owners, list):
        raise SystemExit("BOUNDARY_REQUIRED_OWNER_CONTRACT_INVALID")
    owners: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    starts: list[int] = []
    ends: list[int] = []
    for raw_owner in raw_owners:
        if not isinstance(raw_owner, Mapping):
            raise SystemExit("BOUNDARY_REQUIRED_OWNER_CONTRACT_INVALID")
        kind = str(raw_owner.get("owner_kind") or "").strip()
        owner_id = str(raw_owner.get("owner_id") or "").strip()
        windows = raw_owner.get("local_windows")
        key = (kind, owner_id)
        if (
            raw_owner.get("required") is not True
            or not kind
            or not owner_id
            or key in seen
            or not isinstance(windows, list)
            or not windows
        ):
            raise SystemExit("BOUNDARY_REQUIRED_OWNER_CONTRACT_INVALID")
        normalized_windows: list[dict[str, int]] = []
        for window in windows:
            if not isinstance(window, Mapping):
                raise SystemExit("BOUNDARY_REQUIRED_OWNER_CONTRACT_INVALID")
            start = window.get("start_ms")
            end = window.get("end_ms")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or not 0 <= start < end <= padded_dur
            ):
                raise SystemExit("BOUNDARY_REQUIRED_OWNER_CONTRACT_INVALID")
            starts.append(start)
            ends.append(end)
            normalized_windows.append(
                {"start_ms": start, "end_ms": end}
            )
        seen.add(key)
        owners.append(
            {
                **dict(raw_owner),
                "owner_kind": kind,
                "owner_id": owner_id,
                "local_windows": normalized_windows,
            }
        )
    return owners, min(starts), max(ends)


def _snap_end_at_or_after(
    cue_ends_ms: list[int],
    lower_bound_ms: int,
    *,
    upper_bound_ms: int | None = None,
) -> int | None:
    """A hard owner/lower-bound can never snap backward out of scope."""

    upper_bound = (
        lower_bound_ms + SNAP_AFTER_MS
        if upper_bound_ms is None
        else min(lower_bound_ms + SNAP_AFTER_MS, upper_bound_ms)
    )
    candidates = [
        end
        for end in cue_ends_ms
        if lower_bound_ms <= end <= upper_bound
    ]
    return min(candidates) if candidates else None


def _select_initial_boundary(
    *,
    spec: dict,
    durations: list[int],
    padded: Path,
    padded_dur: int,
    out_root: Path,
    transcriber: Callable,
    cues: list[object],
    required_tail_end_ms: int | None,
    adapters: BoundaryResolutionAdapters,
    boundary_repair_extend_cap_ms: int = BOUNDARY_REPAIR_EXTEND_CAP_MS,
) -> InitialBoundary:
    (
        required_boundary_owners,
        required_owner_start_ms,
        required_owner_end_ms,
    ) = _required_boundary_owner_contract(spec, padded_dur=padded_dur)
    first_piece = spec["pieces"][0]
    target_start_rel = spec.get("semantic_start_ms", first_piece["start_ms"]) - first_piece["start_ms"]
    snapped_start = snap_start_to_sentence([c.start_ms for c in cues], target_start_rel)
    final_start = max(0, (snapped_start if snapped_start is not None else target_start_rel) - LEAD_AIR_MS)
    if required_owner_start_ms is not None:
        final_start = min(final_start, required_owner_start_ms)

    # 4b. Sentence-snap the END; a run-on cue near the closure triggers a
    #     fine-grained micro re-transcription of the tail so the closure
    #     sentence gets its own boundary.
    last_piece = spec["pieces"][-1]
    prior_piece_duration_ms = sum(durations[:-1])
    last_piece_start_ms = int(last_piece["start_ms"])
    target_rel = prior_piece_duration_ms + (
        int(spec["semantic_end_ms"]) - last_piece_start_ms
    )
    semantic_target_rel = target_rel
    structured_payoff_ms: int | None = None
    if (
        required_tail_end_ms is not None
        and semantic_target_rel < required_tail_end_ms <= semantic_target_rel + 15_000
    ):
        # A structured message appeared inside the selected event and the
        # transcript proves she finished reading it just after the semantic
        # target.  The read is the event payoff, not the next topic.
        structured_payoff_ms = required_tail_end_ms
        target_rel = structured_payoff_ms
    # 受监督终点只能扩展、不能截短语义候选，也不能代替独立语义终审。
    # given_end_ms 是人工审过的 source-timeline 下限；semantic review 仍须
    # PASS，最终取二者较晚者再贴句尾。这样一段自由文字 authority 不会
    # 再单独豁免续讲或把某句话切在中间。
    manual_end_authority: str | None = None
    manual_rel: int | None = None
    if spec.get("given_end_ms") is not None:
        manual_end_authority = str(spec.get("given_end_authority") or "").strip()
        if not manual_end_authority:
            raise SystemExit("MANUAL_END_AUTHORITY_MISSING")
        manual_end_ms = int(spec["given_end_ms"])
        if manual_end_ms < int(spec["semantic_end_ms"]):
            raise SystemExit("MANUAL_END_CANNOT_TRUNCATE_SEMANTIC_TARGET")
        manual_rel = prior_piece_duration_ms + (
            manual_end_ms - last_piece_start_ms
        )
        # The reviewed endpoint is a lower bound, not a replacement target.
        # Preserve a later structured-chat payoff already proven above.
        target_rel = max(target_rel, manual_rel)
    semantic_review = (
        dict(spec["boundary_semantic_review"])
        if isinstance(spec.get("boundary_semantic_review"), dict)
        else None
    )
    if semantic_review is None or semantic_review.get("status") != "PASS":
        reason_codes = (
            semantic_review.get("reason_codes")
            if isinstance(semantic_review, dict)
            else ["BOUNDARY_SEMANTIC_REVIEW_MISSING"]
        )
        if (
            isinstance(semantic_review, dict)
            and semantic_review.get("needs_more_context") is True
            and semantic_review.get("retry_scope")
            == "same_topic_continues"
        ):
            raise SystemExit(
                "BOUNDARY_CONTEXT_EXHAUSTED: "
                f"{json.dumps(reason_codes, ensure_ascii=False)} "
                f"max_forward_ms={semantic_review.get('max_forward_ms')} "
                "retry_scope=same_topic_continues"
            )
        raise SystemExit(
            "BOUNDARY_SEMANTIC_REVIEW_REQUIRED: "
            + json.dumps(reason_codes, ensure_ascii=False)
        )
    reviewed_grid_sha256 = semantic_review.get("cue_grid_sha256")
    if (
        reviewed_grid_sha256 is not None
        and reviewed_grid_sha256 != cue_grid_sha256(cues)
    ):
        raise SystemExit("BOUNDARY_SEMANTIC_CUE_GRID_MISMATCH")
    expected_search_scope = build_boundary_search_scope(
        semantic_target_ms=semantic_target_rel,
        manual_lower_bound_ms=manual_rel,
        structured_payoff_ms=structured_payoff_ms,
        required_owner_end_ms=required_owner_end_ms,
        repair_cap_ms=boundary_repair_extend_cap_ms,
        last_piece_start_ms=last_piece_start_ms,
        prior_piece_duration_ms=prior_piece_duration_ms,
    )
    spec_search_scope = spec.get("boundary_search_scope")
    review_search_scope = semantic_review.get("boundary_search_scope")
    production_scope_required = (
        semantic_review.get("schema_version")
        == "talk-boundary-semantic-review.v1"
        and semantic_review.get("review_scope") == "source_full_window"
    )
    bound_search_scope = (
        production_scope_required
        or spec_search_scope is not None
        or review_search_scope is not None
    )
    if bound_search_scope:
        if not (
            boundary_search_scope_is_valid(spec_search_scope)
            and boundary_search_scope_is_valid(review_search_scope)
        ):
            raise SystemExit(
                "BOUNDARY_SEMANTIC_SEARCH_SCOPE_INVALID"
            )
        if (
            dict(spec_search_scope) != expected_search_scope
            or dict(review_search_scope) != expected_search_scope
        ):
            raise SystemExit(
                "BOUNDARY_SEMANTIC_SEARCH_SCOPE_MISMATCH"
            )
        search_scope = expected_search_scope
    else:
        # Hand-built legacy specs in unit fixtures predate the bound scope.
        # Production always supplies both copies and is checked above.
        search_scope = expected_search_scope
    if search_scope.get("status") != "PASS":
        reasons = list(search_scope.get("reason_codes") or [])
        if "BOUNDARY_REQUIRED_OWNER_EXCLUDED" in reasons:
            raise SystemExit(
                "BOUNDARY_REQUIRED_OWNER_EXCLUDED: "
                f"required_end={required_owner_end_ms}ms exceeds "
                f"repair_max_end={search_scope['max_recommended_end_ms']}ms "
                f"(origin={search_scope['semantic_search_origin_ms']}ms)"
            )
        raise SystemExit(
            "BOUNDARY_SEMANTIC_SEARCH_SCOPE_BLOCKED: "
            + json.dumps(reasons, ensure_ascii=False)
        )
    recommended_end_ms = semantic_review.get("recommended_end_ms")
    minimum_recommended_end_ms = int(
        search_scope[
            (
                "minimum_recommended_end_ms"
                if bound_search_scope
                else "semantic_target_ms"
            )
        ]
    )
    if (
        isinstance(recommended_end_ms, bool)
        or not isinstance(recommended_end_ms, int)
        or recommended_end_ms < minimum_recommended_end_ms
        or recommended_end_ms
        > int(search_scope["max_recommended_end_ms"])
    ):
        raise SystemExit("BOUNDARY_SEMANTIC_RECOMMENDATION_INVALID")
    target_rel = max(
        int(search_scope["delivery_lower_bound_ms"]),
        recommended_end_ms,
    )
    # Manual/structured semantic authority owns the absolute repair budget.
    # A required owner remains only a delivery floor, and a later reviewer
    # recommendation cannot ratchet a fresh cap from itself.
    repair_search_origin_ms = int(
        search_scope["semantic_search_origin_ms"]
    )
    repair_max_end_ms = min(
        padded_dur,
        int(search_scope["max_recommended_end_ms"]),
    )
    if (
        required_owner_end_ms is not None
        and required_owner_end_ms > repair_max_end_ms
    ):
        raise SystemExit(
            "BOUNDARY_REQUIRED_OWNER_EXCLUDED: "
            f"required_end={required_owner_end_ms}ms exceeds "
            f"repair_max_end={repair_max_end_ms}ms "
            f"(origin={repair_search_origin_ms}ms)"
        )
    closure_selection_lower_bound_ms = target_rel
    semantic_cues = [
        cue for cue in cues if str(getattr(cue, "text", "") or "").strip()
    ]
    recommended_index = semantic_review.get("recommended_end_cue_index")
    recommended_cue = (
        semantic_cues[recommended_index - 1]
        if (
            isinstance(recommended_index, int)
            and not isinstance(recommended_index, bool)
            and 1 <= recommended_index <= len(semantic_cues)
        )
        else None
    )
    # A required media interval may end inside the deterministic tail air
    # after the semantically reviewed closure cue.  That interval is a final
    # media coverage floor, not authority to consume the next sentence.
    # Select the reviewed cue only when its exact identity is still present
    # and its normal tail can cover the entire delivery floor.  The repair
    # loop below independently verifies the actual (possibly clamped) final
    # media end, so this cannot waive a required owner or manual endpoint.
    if (
        recommended_cue is not None
        and int(recommended_cue.end_ms) == recommended_end_ms
        and recommended_end_ms
        < target_rel
        <= recommended_end_ms + TAIL_PAD_MS
    ):
        closure_selection_lower_bound_ms = recommended_end_ms
    snapped = _snap_end_at_or_after(
        [c.end_ms for c in cues],
        closure_selection_lower_bound_ms,
        upper_bound_ms=repair_max_end_ms,
    )
    refinement_used = False
    if needs_tail_refinement(
        cues,
        snapped_end=snapped,
        target_ms=closure_selection_lower_bound_ms,
    ):
        refinement_used = True
        refine_start = max(0, closure_selection_lower_bound_ms - 20_000)
        refine_end = min(
            padded_dur,
            closure_selection_lower_bound_ms + 15_000,
            repair_max_end_ms,
        )
        tail_clip = out_root / "tail_refine.mp4"
        adapters.run_command(adapters.accurate_recut_command(source_video=padded, output_media=tail_clip, start_ms=refine_start, duration_ms=refine_end - refine_start))
        tail_srt = transcriber(tail_clip, None)
        (out_root / "tail_refine.fresh.srt").write_text(tail_srt, encoding="utf-8")
        fine = [c for c in parse_srt_cues(tail_srt) if c.text.strip()]
        fine_lifted = [
            type(c)(index=c.index, start_ms=c.start_ms + refine_start, end_ms=c.end_ms + refine_start, text=c.text)
            for c in fine
        ]
        snapped = _snap_end_at_or_after(
            [c.end_ms for c in fine_lifted],
            closure_selection_lower_bound_ms,
            upper_bound_ms=repair_max_end_ms,
        )
        if snapped is not None:
            # Splice: fine cues replace coarse cues inside the refined window.
            cues = [c for c in cues if c.end_ms <= refine_start or c.start_ms >= refine_end] + fine_lifted
            cues.sort(key=lambda c: c.start_ms)
    if snapped is None:
        if (
            required_owner_end_ms is not None
            and required_owner_end_ms > repair_search_origin_ms
        ):
            raise SystemExit(
                "BOUNDARY_REQUIRED_OWNER_EXCLUDED: "
                "no sentence boundary in required-owner closure window "
                f"[{required_owner_end_ms},{repair_max_end_ms}]ms"
            )
        raise SystemExit(
            f"NO_SENTENCE_BOUNDARY_NEAR_TARGET: target={target_rel}ms; nearest cue ends="
            f"{sorted((c.end_ms for c in cues), key=lambda e: abs(e - target_rel))[:3]}"
        )
    closure_cue = next(c for c in cues if c.end_ms == snapped)
    return InitialBoundary(
        cues=cues,
        target_start_rel=target_start_rel,
        snapped_start=snapped_start,
        final_start=final_start,
        target_rel=target_rel,
        closure_selection_lower_bound_ms=(
            closure_selection_lower_bound_ms
        ),
        repair_search_origin_ms=repair_search_origin_ms,
        repair_max_end_ms=repair_max_end_ms,
        snapped_end=snapped,
        closure_cue=closure_cue,
        refinement_used=refinement_used,
        manual_end_authority=manual_end_authority,
        semantic_review=semantic_review,
        required_boundary_owners=required_boundary_owners,
        required_owner_start_ms=required_owner_start_ms,
        required_owner_end_ms=required_owner_end_ms,
    )


def _repair_boundary(
    *,
    cid: str,
    out_root: Path,
    padded_dur: int,
    spans: list[object],
    cues: list[object],
    target_start_rel: int,
    snapped_start: int | None,
    final_start: int,
    target_rel: int,
    closure_selection_lower_bound_ms: int,
    repair_search_origin_ms: int,
    repair_max_end_ms: int,
    snapped: int,
    closure_cue: object,
    refinement_used: bool,
    manual_end_authority: str | None,
    semantic_review: dict | None,
    required_boundary_owners: list[dict[str, object]],
    required_owner_start_ms: int | None,
    required_owner_end_ms: int | None,
    boundary_repair_extend_cap_ms: int,
) -> BoundaryResolution:
    audit_path = out_root / f"{cid}.boundary_audit.json"
    boundary_repairs: list[dict] = []
    recorded_tail_clamps: set[tuple[int, int]] = set()
    while True:
        tail_adjustment = adaptive_tail_cut(
            spans,
            cues=cues,
            snapped_end_ms=snapped,
            padded_dur_ms=padded_dur,
        )
        final_end = tail_adjustment["final_end_ms"]
        owner_failures = [
            {
                "owner_kind": owner["owner_kind"],
                "owner_id": owner["owner_id"],
                "window": window,
            }
            for owner in required_boundary_owners
            for window in owner["local_windows"]
            if (
                int(window["start_ms"]) < final_start
                or int(window["end_ms"]) > final_end
            )
        ]
        delivery_coverage_failure = (
            {
                "reason": "FINAL_MEDIA_END_BEFORE_DELIVERY_LOWER_BOUND",
                "delivery_lower_bound_ms": target_rel,
                "final_end_ms": final_end,
            }
            if final_end < target_rel
            else None
        )
        clamp_key = (snapped, final_end)
        if tail_adjustment["reason"] and clamp_key not in recorded_tail_clamps:
            boundary_repairs.append(
                {
                    "reason": tail_adjustment["reason"],
                    "snapped_end_ms": snapped,
                    "nominal_final_end_ms": tail_adjustment["nominal_end_ms"],
                    "final_end_ms": final_end,
                    "next_speech_island_start_ms": tail_adjustment["next_speech_island_start_ms"],
                    "next_speech_island_guard_ms": tail_adjustment["next_speech_island_guard_ms"],
                }
            )
            recorded_tail_clamps.add(clamp_key)
        audit = boundary_audit(
            spans,
            start_ms=final_start,
            cut_ms=final_end,
            start_snapped=snapped_start is not None,
            end_snapped=True,
        )
        audit.update(
            {
                "semantic_start_target_rel_ms": target_start_rel,
                "snapped_sentence_start_ms": snapped_start,
                "final_start_ms": final_start,
                "opening_sentence": next((c.text for c in cues if c.start_ms == snapped_start), None),
                "semantic_target_rel_ms": repair_search_origin_ms,
                "boundary_selection_lower_bound_ms": (
                    closure_selection_lower_bound_ms
                ),
                "delivery_coverage_lower_bound_ms": target_rel,
                "tail_pad_coverage_bridge": {
                    "status": (
                        "USED"
                        if closure_selection_lower_bound_ms < target_rel
                        else "NOT_NEEDED"
                    ),
                    "closure_lower_bound_ms": (
                        closure_selection_lower_bound_ms
                    ),
                    "delivery_lower_bound_ms": target_rel,
                    "maximum_tail_pad_ms": TAIL_PAD_MS,
                },
                "snapped_sentence_end_ms": snapped,
                "final_end_ms": final_end,
                "closure_sentence": closure_cue.text,
                "syntactic_tail_audit": syntactic_tail_audit(closure_cue.text),
                "tail_adjustment": tail_adjustment,
                "tail_refinement_used": refinement_used,
                "boundary_repair_search_origin_ms": repair_search_origin_ms,
                "boundary_repair_extend_cap_ms": boundary_repair_extend_cap_ms,
                "boundary_repair_max_end_ms": repair_max_end_ms,
                "required_boundary_owner_start_ms": required_owner_start_ms,
                "required_boundary_owner_end_ms": required_owner_end_ms,
                "boundary_repairs": boundary_repairs,
                "frozen_required_boundary_owner_count": len(
                    required_boundary_owners
                ),
                "frozen_required_boundary_owners": (
                    required_boundary_owners
                ),
                "required_boundary_owner_verification": {
                    "status": "FAIL" if owner_failures else "PASS",
                    "failures": owner_failures,
                },
                "delivery_coverage_verification": {
                    "status": (
                        "FAIL" if delivery_coverage_failure else "PASS"
                    ),
                    "failure": delivery_coverage_failure,
                },
            }
        )
        if manual_end_authority:
            audit["boundary_authority"] = (
                "human_source_reviewed_lower_bound_plus_semantic_review"
            )
            audit["manual_end_authority"] = manual_end_authority
            audit["boundary_semantic_review"] = semantic_review
        elif semantic_review is not None:
            audit["boundary_authority"] = (
                "correlated_semantic_review_plus_deterministic_guards"
            )
            audit["boundary_semantic_review"] = semantic_review
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if owner_failures:
            raise SystemExit(
                "BOUNDARY_REQUIRED_OWNER_EXCLUDED: "
                + json.dumps(owner_failures, ensure_ascii=False)
            )
        if delivery_coverage_failure:
            raise SystemExit(
                "BOUNDARY_DELIVERY_LOWER_BOUND_EXCLUDED: "
                + json.dumps(
                    delivery_coverage_failure, ensure_ascii=False
                )
            )
        if audit["verdict"] != "ok_sentence_boundary_cut":
            raise SystemExit(f"BOUNDARY_AUDIT_FAILED: {json.dumps(audit, ensure_ascii=False)}")
        source_cues = [
            SourceCue(f"fresh_{i:04d}", max(c.start_ms, final_start), min(c.end_ms, final_end), c.text.strip(), "zh", "speech", 1.0)
            for i, c in enumerate(cues, start=1)
            if c.start_ms < final_end and c.end_ms > final_start
        ]
        sanitized, timing_qa = sanitize_cue_timing(source_cues, spans, window_start_ms=final_start, window_end_ms=final_end)
        red_flags = boundary_red_flags(
            audit=audit,
            cues=cues,
            sanitized=sanitized,
            final_start_ms=final_start,
            final_end_ms=final_end,
            snapped_end_ms=snapped,
            closure_text=closure_cue.text,
        )
        if manual_end_authority or (
            semantic_review is not None and semantic_review.get("status") == "PASS"
        ):
            waived = [flag for flag in red_flags if flag.startswith("speech_continues_")]
            if waived:
                audit["physical_continuity_nonsemantic_flags"] = waived
                red_flags = [flag for flag in red_flags if not flag.startswith("speech_continues_")]
        if not red_flags:
            break
        semantic_review_conflict = any(
            flag.startswith("syntactically_incomplete_closure:")
            for flag in red_flags
        )
        forward_extension_eligible = (
            not semantic_review_conflict
            and tail_requires_forward_extension(
                cues, spans, snapped_end_ms=snapped, cut_ms=final_end
            )
        )
        audit["forward_extension_eligible"] = forward_extension_eligible
        audit["semantic_review_conflict"] = semantic_review_conflict
        repair: dict = {}
        flagged_repair_count = sum(1 for item in boundary_repairs if "flags" in item)
        if flagged_repair_count < MAX_BOUNDARY_REPAIRS:
            if "opens_mid_sentence" in red_flags:
                new_snap_start = repair_start_for_straddler(cues, final_start_ms=final_start)
                if new_snap_start is not None and new_snap_start != snapped_start:
                    repair["snapped_start_ms"] = new_snap_start
            if any(flag != "opens_mid_sentence" for flag in red_flags) and forward_extension_eligible:
                new_end = next_clean_closure(
                    cues,
                    spans,
                    after_ms=snapped,
                    padded_dur_ms=padded_dur,
                    cap_ms=boundary_repair_extend_cap_ms,
                    search_origin_ms=repair_search_origin_ms,
                )
                if new_end is not None:
                    repair["snapped_end_ms"] = new_end
        if not repair:
            audit["red_flags"] = red_flags
            retry_scope = "same_topic_continues" if forward_extension_eligible else "none"
            audit["boundary_context_retry_scope"] = retry_scope
            if (
                required_owner_end_ms is not None
                and required_owner_end_ms > repair_search_origin_ms
            ):
                audit["required_boundary_owner_verification"] = {
                    "status": "FAIL",
                    "failures": [
                        {
                            "reason": "NO_CLEAN_CLOSURE_WITHIN_REPAIR_CAP",
                            "required_end_ms": required_owner_end_ms,
                            "repair_search_origin_ms": repair_search_origin_ms,
                            "repair_max_end_ms": repair_max_end_ms,
                            "red_flags": red_flags,
                        }
                    ],
                }
                audit_path.write_text(
                    json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                raise SystemExit(
                    "BOUNDARY_REQUIRED_OWNER_EXCLUDED: "
                    "no clean closure in required-owner window "
                    f"[{required_owner_end_ms},{repair_max_end_ms}]ms"
                )
            audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise SystemExit(
                f"BOUNDARY_UNREPAIRABLE: {','.join(red_flags)} after {flagged_repair_count} repair(s); "
                f"snapped={snapped}ms target={target_rel}ms "
                f"extend_cap={boundary_repair_extend_cap_ms}ms retry_scope={retry_scope}"
            )
        boundary_repairs.append({"flags": red_flags, **repair})
        if "snapped_start_ms" in repair:
            snapped_start = repair["snapped_start_ms"]
            final_start = max(0, snapped_start - LEAD_AIR_MS)
            if required_owner_start_ms is not None:
                final_start = min(final_start, required_owner_start_ms)
        if "snapped_end_ms" in repair:
            snapped = repair["snapped_end_ms"]
            closure_cue = next(c for c in cues if c.end_ms == snapped)
    semantic_review, binding_reasons = _bind_final_semantic_endpoint(
        semantic_review=semantic_review,
        cues=cues,
        closure_cue=closure_cue,
        snapped_end_ms=snapped,
        final_start_ms=final_start,
        final_end_ms=final_end,
    )
    audit["boundary_semantic_review"] = semantic_review
    if binding_reasons:
        audit["red_flags"] = sorted(
            {
                *(audit.get("red_flags") or []),
                *binding_reasons,
            }
        )
        audit_path.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        raise SystemExit(
            "BOUNDARY_CONTEXT_EXHAUSTED: "
            f"{json.dumps(binding_reasons, ensure_ascii=False)} "
            f"max_forward_ms={boundary_repair_extend_cap_ms} "
            "retry_scope=same_topic_continues"
        )
    audit["red_flags"] = []
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return BoundaryResolution(
        final_start=final_start,
        final_end=final_end,
        audit=audit,
        sanitized_cues=sanitized,
        timing_qa=timing_qa,
    )

def resolve_producer_boundary(
    *,
    spec: dict,
    durations: list[int],
    padded: Path,
    padded_dur: int,
    cid: str,
    out_root: Path,
    transcriber: Callable,
    cues: list[object],
    spans: list[object],
    boundary_repair_extend_cap_ms: int,
    adapters: BoundaryResolutionAdapters,
    required_tail_end_ms: int | None = None,
) -> BoundaryResolution:
    initial = _select_initial_boundary(
        spec=spec,
        durations=durations,
        padded=padded,
        padded_dur=padded_dur,
        out_root=out_root,
        transcriber=transcriber,
        cues=cues,
        required_tail_end_ms=required_tail_end_ms,
        adapters=adapters,
        boundary_repair_extend_cap_ms=boundary_repair_extend_cap_ms,
    )
    return _repair_boundary(
        cid=cid,
        out_root=out_root,
        padded_dur=padded_dur,
        spans=spans,
        cues=initial.cues,
        target_start_rel=initial.target_start_rel,
        snapped_start=initial.snapped_start,
        final_start=initial.final_start,
        target_rel=initial.target_rel,
        closure_selection_lower_bound_ms=(
            initial.closure_selection_lower_bound_ms
        ),
        repair_search_origin_ms=initial.repair_search_origin_ms,
        repair_max_end_ms=initial.repair_max_end_ms,
        snapped=initial.snapped_end,
        closure_cue=initial.closure_cue,
        refinement_used=initial.refinement_used,
        manual_end_authority=initial.manual_end_authority,
        semantic_review=initial.semantic_review,
        required_boundary_owners=initial.required_boundary_owners,
        required_owner_start_ms=initial.required_owner_start_ms,
        required_owner_end_ms=initial.required_owner_end_ms,
        boundary_repair_extend_cap_ms=boundary_repair_extend_cap_ms,
    )
