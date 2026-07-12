from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution, BoundaryPolicy, TalkCue, resolve_talk_boundary
from src.autoslice.review_evidence import SourceCue

FULL_SESSION_CANDIDATE_SCHEMA_VERSION = "full-session-candidate.v1"

_SETUP_MARKERS = (
    "我跟你们说",
    "我跟你说",
    "我给你们讲",
    "有个事",
    "事情是这样",
    "你们知道",
    "为什么",
    "怎么",
)
_PAYOFF_MARKERS = ("结果", "最后", "哈哈", "笑", "离谱", "绷", "破防", "翻车", "怎么样")
_CLOSURE_MARKERS = ("哈哈", "笑了", "就很离谱", "就这样", "结束", "完了", "怎么样", "最后")
_CONNECTIVE_PREFIXES = ("然后", "所以", "但是", "因为", "结果", "接着", "后来", "而且", "不过")
_SONG_LIKE_MARKERS = (
    "啊啊",
    "啦啦",
    "君",
    "愛",
    "の",
    "baby",
    "just take",
    "唱了",
    "第八首",
    "最后の",
    "上天啊",
    "相爱",
    "爱情",
    "婚礼",
    "祝福你",
    "拥抱他",
)


@dataclass(frozen=True)
class FullSessionCandidate:
    """A source-timeline candidate selected from complete-session cues."""

    anchor: AnchorCandidate
    boundary: BoundaryResolution
    cues: tuple[SourceCue, ...]
    text_preview: str
    schema_version: str = FULL_SESSION_CANDIDATE_SCHEMA_VERSION
    content_type_hint: str = "talk"

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.anchor.candidate_id,
            "content_type_hint": self.content_type_hint,
            "requires_full_source_song_boundary_redo": self.content_type_hint == "song",
            "anchor_start_ms": self.anchor.anchor_start_ms,
            "anchor_end_ms": self.anchor.anchor_end_ms,
            "boundary_resolution": {
                "action": self.boundary.action.value,
                "resolved_start_ms": self.boundary.resolved_start_ms,
                "resolved_end_ms": self.boundary.resolved_end_ms,
                "start_boundary_score": self.boundary.start_boundary_score,
                "end_boundary_score": self.boundary.end_boundary_score,
                "reason_codes": list(self.boundary.reason_codes),
                "next_start_ms": self.boundary.next_start_ms,
                "next_end_ms": self.boundary.next_end_ms,
            },
            "text_preview": self.text_preview,
            "source_cues": [cue.to_manifest() for cue in self.cues],
        }

    def to_source_context_job(self, *, source_duration_ms: int) -> dict[str, object]:
        if self.content_type_hint == "song":
            start_ms = 0
            end_ms = source_duration_ms
        else:
            start_ms = max(0, self.boundary.next_start_ms if self.boundary.next_start_ms is not None else self.boundary.resolved_start_ms)
            end_ms = self.boundary.next_end_ms if self.boundary.next_end_ms is not None else self.boundary.resolved_end_ms
            end_ms = min(source_duration_ms, end_ms)
        return {
            "schema_version": "source-context-job-from-full-session-candidate.v1",
            "candidate_id": self.anchor.candidate_id,
            "content_type_hint": self.content_type_hint,
            "song_candidate": self.content_type_hint == "song",
            "requires_full_source_song_boundary_redo": self.content_type_hint == "song",
            "title": self.text_preview[:80],
            "timeline": {
                "source_duration_ms": source_duration_ms,
                "anchor_start_ms": self.anchor.anchor_start_ms,
                "anchor_end_ms": self.anchor.anchor_end_ms,
                "context_start_ms": start_ms,
                "context_end_ms": end_ms,
                "context_duration_ms": max(0, end_ms - start_ms),
            },
        }


def select_full_session_candidates(
    cues: Sequence[SourceCue],
    *,
    max_candidates: int = 20,
    min_window_ms: int = 12_000,
    max_window_ms: int = 180_000,
    max_cues_per_window: int = 24,
    policy: BoundaryPolicy | None = None,
) -> list[FullSessionCandidate]:
    """Select setup→payoff→closure candidates from a complete source timeline.

    This is a recall-stage selector: it produces deterministic source-timeline
    anchors that are already likely to have natural boundaries, then lets the
    boundary resolver decide AUTO_UPLOAD/AUTO_RECUT/DROP.  It never publishes or
    renders media.
    """

    ordered = sorted(cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms, cue.cue_id))
    selected: list[FullSessionCandidate] = []
    for start_index, start_cue in enumerate(ordered):
        if len(selected) >= max_candidates:
            break
        if not _is_candidate_start(start_cue):
            continue
        for end_index in range(start_index + 1, min(len(ordered), start_index + max_cues_per_window)):
            window = tuple(ordered[start_index : end_index + 1])
            start_ms = window[0].source_start_ms
            end_ms = window[-1].source_end_ms
            duration_ms = end_ms - start_ms
            if duration_ms < min_window_ms:
                continue
            if duration_ms > max_window_ms:
                break
            text_preview = _join_text(window)
            if not _has_setup_payoff_closure(text_preview):
                continue
            song_like = _is_song_like_window(text_preview)
            candidate_id_prefix = "fullsong" if song_like else "fullctx"
            anchor = AnchorCandidate(candidate_id=f"{candidate_id_prefix}_{start_ms}_{end_ms}", anchor_start_ms=start_ms, anchor_end_ms=end_ms)
            if song_like:
                boundary = _song_anchor_boundary(anchor)
            else:
                boundary = resolve_talk_boundary(anchor, [_to_talk_cue(cue, index, window) for index, cue in enumerate(window)], policy=policy)
            if boundary.action not in {DecisionAction.AUTO_UPLOAD, DecisionAction.AUTO_RECUT}:
                continue
            candidate = FullSessionCandidate(
                anchor=anchor,
                boundary=boundary,
                cues=window,
                text_preview=text_preview,
                content_type_hint="song" if song_like else "talk",
            )
            if song_like and _duplicates_unresolved_song_fragment(candidate, selected):
                # This legacy setup/payoff selector can split one continuous
                # lyric run into several fragments.  Collapse only very close
                # fragments here; semantic and visual song lanes use the
                # ordinary real-overlap rule below, so distinct adjacent songs
                # with their own title evidence are not swallowed.
                continue
            if _overlaps_selected(candidate, selected):
                continue
            selected.append(candidate)
            break
    return selected


def _is_candidate_start(cue: SourceCue) -> bool:
    text = cue.text.strip()
    return bool(text) and not text.startswith(_CONNECTIVE_PREFIXES) and any(marker in text for marker in _SETUP_MARKERS)


def select_fallback_session_candidates(
    cues: Sequence[SourceCue],
    *,
    max_candidates: int = 3,
    min_run_ms: int = 45_000,
    max_gap_ms: int = 12_000,
    min_avg_cue_ms: int = 2_000,
    edge_trim_min_cue_ms: int = 3_000,
    min_cues: int = 6,
) -> list[FullSessionCandidate]:
    """Recall fallback for arbitrary real streams (repair-first goal).

    The primary selector keys on lidousha-specific setup markers and yields
    zero candidates on streams that phrase things differently — zero output is
    itself a failure of the unattended goal.  This fallback surfaces contiguous
    performance runs (dense, long cues — typical of singing) as song candidates
    so the song-repair layer can try to *prove* them instead of the pipeline
    producing nothing.  It stays recall-stage only: every candidate still has
    to earn its way through boundary proof, CPA QA, and the review gates.
    """

    ordered = sorted(cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms, cue.cue_id))
    runs: list[list[SourceCue]] = []
    current: list[SourceCue] = []
    for cue in ordered:
        if not current or cue.source_start_ms - current[-1].source_end_ms <= max_gap_ms:
            current.append(cue)
        else:
            runs.append(current)
            current = [cue]
    if current:
        runs.append(current)

    scored: list[tuple[int, list[SourceCue]]] = []
    for run in runs:
        # Chatty quips chained onto the performance edges (song lead-in banter)
        # are short; sung lines are long.  Trim edges to the performance core —
        # the anchor is recall-only, completeness is still proven by song repair.
        while run and (run[0].source_end_ms - run[0].source_start_ms) < edge_trim_min_cue_ms:
            run = run[1:]
        while run and (run[-1].source_end_ms - run[-1].source_start_ms) < edge_trim_min_cue_ms:
            run = run[:-1]
        if len(run) < min_cues:
            continue
        duration_ms = run[-1].source_end_ms - run[0].source_start_ms
        if duration_ms < min_run_ms:
            continue
        avg_cue_ms = sum(cue.source_end_ms - cue.source_start_ms for cue in run) / len(run)
        if avg_cue_ms < min_avg_cue_ms:
            continue
        if not _looks_like_performance(run):
            continue
        scored.append((duration_ms, run))

    scored.sort(key=lambda item: item[0], reverse=True)
    selected: list[FullSessionCandidate] = []
    song_spans: list[tuple[int, int]] = []
    for duration_ms, run in scored[:max_candidates]:
        start_ms = run[0].source_start_ms
        end_ms = run[-1].source_end_ms
        song_spans.append((start_ms, end_ms))
        anchor = AnchorCandidate(
            candidate_id=f"fallbacksong_{start_ms}_{end_ms}",
            anchor_start_ms=start_ms,
            anchor_end_ms=end_ms,
        )
        selected.append(
            FullSessionCandidate(
                anchor=anchor,
                boundary=_song_anchor_boundary(anchor),
                cues=tuple(run),
                text_preview=_join_text(run)[:160],
                content_type_hint="song",
            )
        )

    if len(selected) < max_candidates:
        selected.extend(
            _fallback_talk_candidates(
                ordered,
                song_spans=song_spans,
                max_candidates=max_candidates - len(selected),
            )
        )
    return selected


_CHAT_MARKERS = (
    "吗",
    "吧",
    "呢",
    "哈哈",
    "什么",
    "为什么",
    "你们",
    "我们",
    "就是",
    "不是",
    "这个",
    "那个",
    "好了",
    "谢谢",
    "晚上好",
    "宝宝",
    "直播",
)


def _looks_like_performance(run: Sequence[SourceCue]) -> bool:
    """Dense chat also chains into long runs; sung lines are the ones that are
    long AND free of conversational particles.  Both signals must agree."""

    if not run:
        return False
    durations = sorted(cue.source_end_ms - cue.source_start_ms for cue in run)
    median_ms = durations[len(durations) // 2]
    if median_ms < 3_000:
        return False
    chatty = sum(1 for cue in run if any(marker in cue.text for marker in _CHAT_MARKERS))
    return chatty / len(run) < 0.35


def _fallback_talk_candidates(
    ordered: Sequence[SourceCue],
    *,
    song_spans: Sequence[tuple[int, int]],
    max_candidates: int,
    min_window_ms: int = 12_000,
    max_window_ms: int = 90_000,
    max_cues_per_window: int = 24,
) -> list[FullSessionCandidate]:
    """Talk recall without lidousha setup markers: any payoff-bearing window.

    A song stream whose songs cannot be proven complete must still be able to
    surface talk moments — zero session output is a pipeline failure, and the
    boundary resolver plus review gates still decide whether these publish.
    """

    def inside_song(cue: SourceCue) -> bool:
        return any(start <= cue.source_start_ms and cue.source_end_ms <= end for start, end in song_spans)

    talk_cues = [cue for cue in ordered if not inside_song(cue)]
    max_intra_gap_ms = 15_000
    min_start_cue_ms = 1_200
    selected: list[FullSessionCandidate] = []
    start_index = 0
    while start_index < len(talk_cues) and len(selected) < max_candidates:
        start_cue = talk_cues[start_index]
        # Orphan fragments (sub-second leftovers) and connective starts make
        # bad clip openings; a talk window also must not span a dead-air gap.
        if (
            start_cue.text.strip().startswith(_CONNECTIVE_PREFIXES)
            or start_cue.source_end_ms - start_cue.source_start_ms < min_start_cue_ms
        ):
            start_index += 1
            continue
        emitted = False
        for end_index in range(start_index + 1, min(len(talk_cues), start_index + max_cues_per_window)):
            window = talk_cues[start_index : end_index + 1]
            if window[-1].source_start_ms - window[-2].source_end_ms > max_intra_gap_ms:
                break
            duration_ms = window[-1].source_end_ms - window[0].source_start_ms
            if duration_ms > max_window_ms:
                break
            if duration_ms < min_window_ms:
                continue
            tail_text = _join_text(window[-2:])
            if not any(marker in tail_text for marker in _PAYOFF_MARKERS + _CLOSURE_MARKERS):
                continue
            anchor = AnchorCandidate(
                candidate_id=f"fallbacktalk_{window[0].source_start_ms}_{window[-1].source_end_ms}",
                anchor_start_ms=window[0].source_start_ms,
                anchor_end_ms=window[-1].source_end_ms,
            )
            boundary = resolve_talk_boundary(
                anchor, [_to_talk_cue(cue, index, window) for index, cue in enumerate(window)]
            )
            if boundary.action not in {DecisionAction.AUTO_UPLOAD, DecisionAction.AUTO_RECUT}:
                continue
            selected.append(
                FullSessionCandidate(
                    anchor=anchor,
                    boundary=boundary,
                    cues=tuple(window),
                    text_preview=_join_text(window)[:160],
                    content_type_hint="talk",
                )
            )
            start_index = end_index + 1
            emitted = True
            break
        if not emitted:
            start_index += 1
    return selected


def _song_anchor_boundary(anchor: AnchorCandidate) -> BoundaryResolution:
    return BoundaryResolution(
        candidate_id=anchor.candidate_id,
        action=DecisionAction.AUTO_RECUT,
        resolved_start_ms=anchor.anchor_start_ms,
        resolved_end_ms=anchor.anchor_end_ms,
        start_boundary_score=0.0,
        end_boundary_score=0.0,
        reason_codes=(
            "SONG_BOUNDARY_REDO_REQUIRED",
            "SONG_FULL_SOURCE_REQUIRED",
            "EXTERNAL_LRC_REQUIRED",
            "LYRIC_ORDER_PROOF_MISSING",
        ),
        next_start_ms=0,
        next_end_ms=None,
    )


def _has_setup_payoff_closure(text: str) -> bool:
    return (
        any(marker in text for marker in _SETUP_MARKERS)
        and any(marker in text for marker in _PAYOFF_MARKERS)
        and any(marker in text for marker in _CLOSURE_MARKERS)
    )


def _is_song_like_window(text: str) -> bool:
    compact = "".join(text.lower().split())
    if not compact:
        return True
    marker_hits = sum(compact.count(marker.lower()) for marker in _SONG_LIKE_MARKERS)
    repeated_vowels = compact.count("啊") + compact.count("啦")
    return marker_hits >= 2 or repeated_vowels / max(1, len(compact)) > 0.25


def _to_talk_cue(cue: SourceCue, index: int, window: Sequence[SourceCue]) -> TalkCue:
    text = cue.text.strip()
    previous = window[index - 1] if index > 0 else None
    starts_topic = index == 0 or (
        not text.startswith(_CONNECTIVE_PREFIXES)
        and previous is not None
        and any(marker in previous.text for marker in _CLOSURE_MARKERS)
    )
    has_payoff = any(marker in text for marker in _PAYOFF_MARKERS)
    ends_topic = any(marker in text for marker in _CLOSURE_MARKERS)
    open_loop_delta = 0
    if any(marker in text for marker in _SETUP_MARKERS) or "?" in text or "？" in text:
        open_loop_delta += 1
    if has_payoff or ends_topic:
        open_loop_delta -= 1
    return TalkCue(
        cue_id=cue.cue_id,
        start_ms=cue.source_start_ms,
        end_ms=cue.source_end_ms,
        text=text,
        starts_topic=starts_topic,
        ends_topic=ends_topic,
        has_payoff=has_payoff,
        open_loop_delta=open_loop_delta,
    )


def _overlaps_selected(candidate: FullSessionCandidate, selected: Sequence[FullSessionCandidate]) -> bool:
    start = candidate.boundary.resolved_start_ms
    end = candidate.boundary.resolved_end_ms
    for existing in selected:
        if candidate.content_type_hint == "song" and existing.content_type_hint == "song":
            overlap = min(
                candidate.anchor.anchor_end_ms, existing.anchor.anchor_end_ms
            ) - max(candidate.anchor.anchor_start_ms, existing.anchor.anchor_start_ms)
            # Song anchors are recall windows, not trusted final boundaries.
            # Any real temporal overlap is duplicate evidence for the same
            # performance; merely being adjacent (even by only a few seconds)
            # is not.  The old <180s gap rule swallowed distinct back-to-back
            # songs and was asymmetric for candidates emitted out of order.
            if overlap > 0:
                return True
            continue
        if existing.content_type_hint == "song" and candidate.content_type_hint != "song":
            anchor_gap = candidate.anchor.anchor_start_ms - existing.anchor.anchor_end_ms
            if 0 <= anchor_gap < 180_000 and _is_song_continuation_like(candidate.text_preview):
                return True
        overlap = min(end, existing.boundary.resolved_end_ms) - max(start, existing.boundary.resolved_start_ms)
        if overlap <= 0:
            continue
        shorter = min(end - start, existing.boundary.resolved_end_ms - existing.boundary.resolved_start_ms)
        if shorter > 0 and overlap / shorter >= 0.5:
            return True
    return False


def _duplicates_unresolved_song_fragment(
    candidate: FullSessionCandidate,
    selected: Sequence[FullSessionCandidate],
) -> bool:
    if candidate.content_type_hint != "song":
        return False
    for existing in selected:
        if existing.content_type_hint != "song":
            continue
        gap = max(
            0,
            max(candidate.anchor.anchor_start_ms, existing.anchor.anchor_start_ms)
            - min(candidate.anchor.anchor_end_ms, existing.anchor.anchor_end_ms),
        )
        if gap <= 12_000:
            return True
    return False


def _join_text(cues: Sequence[SourceCue]) -> str:
    return " ".join(cue.text.strip() for cue in cues if cue.text.strip())


def _is_song_continuation_like(text: str) -> bool:
    compact = "".join(text.lower().split())
    return any(marker.lower() in compact for marker in _SONG_LIKE_MARKERS)
