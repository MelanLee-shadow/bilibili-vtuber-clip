"""Deterministic topic-closure boundary policy for finished talk clips."""

from __future__ import annotations

SNAP_BEFORE_MS = 6_000

SNAP_AFTER_MS = 9_000

START_SNAP_MS = 2_500

TAIL_PAD_MS = 400

NEXT_SPEECH_ISLAND_GUARD_MS = 100

LEAD_AIR_MS = 250

REFINE_IF_OFF_BY_MS = 2_500

REFINE_IF_CUE_LONGER_MS = 10_000

def snap_end_to_sentence(cue_ends_ms: list[int], target_ms: int) -> int | None:
    """Nearest transcription cue end to the semantic target — the cut must sit
    on a complete-sentence boundary or nowhere."""

    candidates = [end for end in cue_ends_ms if target_ms - SNAP_BEFORE_MS <= end <= target_ms + SNAP_AFTER_MS]
    if not candidates:
        return None
    return min(candidates, key=lambda end: abs(end - target_ms))

def snap_start_to_sentence(cue_starts_ms: list[int], target_ms: int) -> int | None:
    """Nearest sentence START to the intended opening — the clip must open on
    a complete sentence, with a little lead air, or nowhere."""

    candidates = [s for s in cue_starts_ms if abs(s - target_ms) <= START_SNAP_MS]
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs(s - target_ms))

def needs_tail_refinement(cues, *, snapped_end: int | None, target_ms: int) -> bool:
    """A run-on cue straddling the target, or a snap far off target, means the
    coarse cue grid cannot place the closure — do a fine micro-pass."""

    if snapped_end is None or abs(snapped_end - target_ms) > REFINE_IF_OFF_BY_MS:
        return True
    straddling = next((c for c in cues if c.start_ms < target_ms < c.end_ms), None)
    return bool(straddling and (straddling.end_ms - straddling.start_ms) > REFINE_IF_CUE_LONGER_MS)

def boundary_audit(spans, *, start_ms: int, cut_ms: int, start_snapped: bool, end_snapped: bool) -> dict:
    """Both cuts must sit on sentence boundaries; the audio at each cut is
    recorded honestly (speech flowing through a sentence-boundary cut is
    allowed, an unsnapped cut is not)."""

    crossing = next((s for s in spans if s.start_ms < cut_ms < s.end_ms), None)
    verdict = "ok_sentence_boundary_cut"
    if not start_snapped:
        verdict = "start_not_on_sentence_boundary"
    elif not end_snapped:
        verdict = "end_not_on_sentence_boundary"
    return {
        "schema_version": "boundary-audit.v1",
        "start_on_sentence_boundary": start_snapped,
        "end_on_sentence_boundary": end_snapped,
        "end_cut_inside_speech_island": bool(crossing),
        "end_island_continues_ms": (crossing.end_ms - cut_ms) if crossing else 0,
        "verdict": verdict,
    }

ISLAND_CONTINUES_FLAG_MS = 1_500

def _norm_cue_text(text: str) -> str:
    return "".join(str(text).split())

def boundary_red_flags(
    *,
    audit: dict,
    cues,
    sanitized,
    final_start_ms: int,
    final_end_ms: int,
    snapped_end_ms: int,
    closure_text: str,
) -> list[str]:
    """Deterministic boundary red flags (2026-07-09 external audit).

    A green boundary verdict only says both cuts SNAPPED to ASR cue boundaries;
    the real 7/6+7/9 deliveries showed that is not enough (cuts inside a still-
    running speech island, final SRT ending on a different sentence than the
    claimed closure, next-topic text flashing in the tail pad).  These checks
    need no LLM.  Policy (Ivan 2026-07-10): an unattended pipeline REPAIRS what
    its own auditors detect — flags drive the deterministic self-repair loop in
    main(); a clip that cannot be repaired fails closed (no delivery).  There
    is no deliver-and-ask-a-human quarantine state."""
    flags: list[str] = []
    continues_ms = int(audit.get("end_island_continues_ms") or 0)
    if continues_ms >= ISLAND_CONTINUES_FLAG_MS:
        flags.append(f"speech_continues_{continues_ms}ms_after_cut")
    if any(c.start_ms < final_start_ms - 50 and c.end_ms > final_start_ms + 300 for c in cues):
        flags.append("opens_mid_sentence")
    if any(snapped_end_ms <= c.start_ms < final_end_ms for c in cues):
        flags.append("next_sentence_enters_tail_pad")
    if sanitized and _norm_cue_text(sanitized[-1].text) != _norm_cue_text(closure_text):
        flags.append("closure_not_final_subtitle")
    return flags

BOUNDARY_REPAIR_EXTEND_CAP_MS = 30_000

BOUNDARY_REPAIR_EXTEND_CAP_MAX_MS = 60_000

MAX_BOUNDARY_REPAIRS = 3

def adaptive_tail_cut(spans, *, snapped_end_ms: int, padded_dur_ms: int, cues=()) -> dict:
    """Keep normal tail air unless it would enter a distinct next VAD island.

    A sentence-end snap is already semantic closure evidence.  If VAD then
    observes a *new* island after that closure, consuming the island is not
    evidence that the closure was incomplete: it is usually the next turn or
    topic.  Leave a small guard before that island, but only when the silence
    gap is wide enough to distinguish two islands robustly.  A narrow gap is
    left to the existing continuation/open-loop repair checks.
    """

    nominal_end_ms = min(padded_dur_ms, snapped_end_ms + TAIL_PAD_MS)
    next_span = min(
        (span for span in spans if span.start_ms > snapped_end_ms),
        key=lambda span: span.start_ms,
        default=None,
    )
    next_cue = min(
        (cue for cue in cues if cue.start_ms >= snapped_end_ms),
        key=lambda cue: cue.start_ms,
        default=None,
    )
    gap_ms = next_span.start_ms - snapped_end_ms if next_span is not None else None
    final_end_ms = nominal_end_ms
    reason = None
    # Subtitle timing is the direct authority for whether text from the next
    # turn will flash in the disposable tail pad.  VAD can begin slightly
    # later than the ASR cue (7/12: cue=123470, island=123592), so a VAD-only
    # clamp still included 22ms of the next subtitle and failed its own audit.
    if next_cue is not None and next_cue.start_ms < nominal_end_ms:
        final_end_ms = max(
            snapped_end_ms,
            next_cue.start_ms - NEXT_SPEECH_ISLAND_GUARD_MS,
        )
        reason = "tail_clamped_before_next_subtitle"
    elif (
        next_span is not None
        and next_span.start_ms < nominal_end_ms
        and gap_ms is not None
        and gap_ms >= 2 * NEXT_SPEECH_ISLAND_GUARD_MS
    ):
        final_end_ms = min(nominal_end_ms, next_span.start_ms - NEXT_SPEECH_ISLAND_GUARD_MS)
        reason = "tail_clamped_before_next_speech_island"
    return {
        "nominal_end_ms": nominal_end_ms,
        "final_end_ms": final_end_ms,
        "tail_pad_ms": TAIL_PAD_MS,
        "next_speech_island_start_ms": next_span.start_ms if next_span is not None else None,
        "next_speech_island_gap_ms": gap_ms,
        "next_speech_island_guard_ms": NEXT_SPEECH_ISLAND_GUARD_MS,
        "next_subtitle_start_ms": next_cue.start_ms if next_cue is not None else None,
        "reason": reason,
    }

def tail_requires_forward_extension(cues, spans, *, snapped_end_ms: int, cut_ms: int) -> bool:
    """True only when speech already in progress crosses the proposed cut.

    A later, distinct VAD island is not a reason to swallow another topic.
    Forward repair is reserved for a continuation island that began before the
    snapped closure, or an ASR cue/open loop that visibly crosses the cut.
    """

    continuation_island = any(
        span.start_ms < snapped_end_ms and span.end_ms > cut_ms for span in spans
    )
    crossing_cue = any(
        cue.start_ms <= snapped_end_ms < cut_ms < cue.end_ms for cue in cues
    )
    return continuation_island or crossing_cue

def next_clean_closure(cues, spans, *, after_ms: int, padded_dur_ms: int,
                       cap_ms: int = BOUNDARY_REPAIR_EXTEND_CAP_MS,
                       search_origin_ms: int | None = None) -> int | None:
    """Earliest LATER sentence end whose cut raises no deterministic red flag:
    nothing starts inside its tail pad and no speech island runs
    ≥ ISLAND_CONTINUES_FLAG_MS past the cut.

    This is the unattended repair move (Ivan 2026-07-10): extend FORWARD to
    where the talk actually lands — never retract, which would drop the very
    content the pick was chosen for.  ``cap_ms`` is absolute from the original
    semantic target (``search_origin_ms``), so repeated repairs cannot ratchet
    the window forward indefinitely."""
    origin_ms = after_ms if search_origin_ms is None else search_origin_ms
    max_end_ms = min(padded_dur_ms, origin_ms + cap_ms)
    ends = sorted({c.end_ms for c in cues if after_ms < c.end_ms <= max_end_ms})
    for end in ends:
        cut = adaptive_tail_cut(
            spans,
            cues=cues,
            snapped_end_ms=end,
            padded_dur_ms=padded_dur_ms,
        )["final_end_ms"]
        if any(end <= c.start_ms < cut for c in cues):
            continue
        crossing = next((s for s in spans if s.start_ms < cut < s.end_ms), None)
        if crossing and crossing.end_ms - cut >= ISLAND_CONTINUES_FLAG_MS:
            continue
        return end
    return None

def repair_start_for_straddler(cues, *, final_start_ms: int) -> int | None:
    """Repair an ``opens_mid_sentence`` flag by opening on the straddling
    sentence's own start — include the whole sentence rather than slicing into
    it.  Returns that sentence's start_ms (the new snapped start; the caller
    re-derives final_start with lead air), or None when no cue straddles the
    opening."""
    straddler = next(
        (c for c in cues if c.start_ms < final_start_ms - 50 and c.end_ms > final_start_ms + 300),
        None,
    )
    if straddler is None:
        return None
    return straddler.start_ms
