"""Runtime orchestration for deterministic talk-clip boundary resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_boundary import (
    LEAD_AIR_MS,
    MAX_BOUNDARY_REPAIRS,
    adaptive_tail_cut,
    boundary_audit,
    boundary_red_flags,
    needs_tail_refinement,
    next_clean_closure,
    repair_start_for_straddler,
    snap_end_to_sentence,
    snap_start_to_sentence,
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
    snapped_end: int
    closure_cue: object
    refinement_used: bool
    manual_end_authority: bool


@dataclass(frozen=True)
class BoundaryResolution:
    final_start: int
    final_end: int
    audit: dict
    sanitized_cues: list[SourceCue]
    timing_qa: dict


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
) -> InitialBoundary:
    first_piece = spec["pieces"][0]
    target_start_rel = spec.get("semantic_start_ms", first_piece["start_ms"]) - first_piece["start_ms"]
    snapped_start = snap_start_to_sentence([c.start_ms for c in cues], target_start_rel)
    final_start = max(0, (snapped_start if snapped_start is not None else target_start_rel) - LEAD_AIR_MS)

    # 4b. Sentence-snap the END; a run-on cue near the closure triggers a
    #     fine-grained micro re-transcription of the tail so the closure
    #     sentence gets its own boundary.
    last_piece = spec["pieces"][-1]
    target_rel = sum(durations[:-1]) + (spec["semantic_end_ms"] - last_piece["start_ms"])
    semantic_target_rel = target_rel
    if (
        required_tail_end_ms is not None
        and semantic_target_rel < required_tail_end_ms <= semantic_target_rel + 15_000
    ):
        # A structured message appeared inside the selected event and the
        # transcript proves she finished reading it just after the semantic
        # target.  The read is the event payoff, not the next topic.
        target_rel = required_tail_end_ms
    # 受监督硬切（Ivan 2026-07-13 MUA 案：故事有语义落点但与下一话题零停顿
    # 衔接，续讲红旗永远拦截）。spec.given_end_ms = Ivan 人工授权的绝对终点：
    # 仍贴到最近的字幕句尾（±1.5s），仍走其余全部审计，仅豁免尾侧续讲红旗；
    # 出处记入 boundary audit（boundary_authority=ivan_manual_end）。
    manual_end_authority = False
    if spec.get("given_end_ms") is not None:
        manual_end_authority = True
        target_rel = sum(durations[:-1]) + (int(spec["given_end_ms"]) - last_piece["start_ms"])
    snapped = snap_end_to_sentence([c.end_ms for c in cues], target_rel)
    refinement_used = False
    if needs_tail_refinement(cues, snapped_end=snapped, target_ms=target_rel):
        refinement_used = True
        refine_start = max(0, target_rel - 20_000)
        refine_end = min(padded_dur, target_rel + 15_000)
        tail_clip = out_root / "tail_refine.mp4"
        adapters.run_command(adapters.accurate_recut_command(source_video=padded, output_media=tail_clip, start_ms=refine_start, duration_ms=refine_end - refine_start))
        tail_srt = transcriber(tail_clip, None)
        (out_root / "tail_refine.fresh.srt").write_text(tail_srt, encoding="utf-8")
        fine = [c for c in parse_srt_cues(tail_srt) if c.text.strip()]
        fine_lifted = [
            type(c)(index=c.index, start_ms=c.start_ms + refine_start, end_ms=c.end_ms + refine_start, text=c.text)
            for c in fine
        ]
        snapped = snap_end_to_sentence([c.end_ms for c in fine_lifted], target_rel)
        if snapped is not None:
            # Splice: fine cues replace coarse cues inside the refined window.
            cues = [c for c in cues if c.end_ms <= refine_start or c.start_ms >= refine_end] + fine_lifted
            cues.sort(key=lambda c: c.start_ms)
    if snapped is None:
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
        snapped_end=snapped,
        closure_cue=closure_cue,
        refinement_used=refinement_used,
        manual_end_authority=manual_end_authority,
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
    snapped: int,
    closure_cue: object,
    refinement_used: bool,
    manual_end_authority: bool,
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
                "semantic_target_rel_ms": target_rel,
                "snapped_sentence_end_ms": snapped,
                "final_end_ms": final_end,
                "closure_sentence": closure_cue.text,
                "tail_adjustment": tail_adjustment,
                "tail_refinement_used": refinement_used,
                "boundary_repair_search_origin_ms": target_rel,
                "boundary_repair_extend_cap_ms": boundary_repair_extend_cap_ms,
                "boundary_repair_max_end_ms": min(
                    padded_dur, target_rel + boundary_repair_extend_cap_ms
                ),
                "boundary_repairs": boundary_repairs,
            }
        )
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
        if manual_end_authority:
            waived = [flag for flag in red_flags if flag.startswith("speech_continues_")]
            if waived:
                audit["manual_end_waived_flags"] = waived
                audit["boundary_authority"] = "ivan_manual_end"
                red_flags = [flag for flag in red_flags if not flag.startswith("speech_continues_")]
        if not red_flags:
            break
        forward_extension_eligible = tail_requires_forward_extension(
            cues, spans, snapped_end_ms=snapped, cut_ms=final_end
        )
        audit["forward_extension_eligible"] = forward_extension_eligible
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
                    search_origin_ms=target_rel,
                )
                if new_end is not None:
                    repair["snapped_end_ms"] = new_end
        if not repair:
            audit["red_flags"] = red_flags
            retry_scope = "same_topic_continues" if forward_extension_eligible else "none"
            audit["boundary_context_retry_scope"] = retry_scope
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
        if "snapped_end_ms" in repair:
            snapped = repair["snapped_end_ms"]
            closure_cue = next(c for c in cues if c.end_ms == snapped)
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
        snapped=initial.snapped_end,
        closure_cue=initial.closure_cue,
        refinement_used=initial.refinement_used,
        manual_end_authority=initial.manual_end_authority,
        boundary_repair_extend_cap_ms=boundary_repair_extend_cap_ms,
    )
