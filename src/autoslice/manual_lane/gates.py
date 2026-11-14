"""The few checks that remain in the manual-lane pipeline.

Judgement (boundary, subtitle doubts, title, tags, review) belongs to CPA and is adopted directly.  What is
left here is either an 维护者 rule (dedupe: only a complete duplicate blocks; intro must be prepended; title
banned words; 维护者's own listening labels must still anchor to the same cue), a common-sense check on CPA's
cut, or a tool-output sanity check.  Every function is pure and returns a Verdict.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NotRequired, TypedDict, Unpack, cast

if TYPE_CHECKING:
    from .srt_text import SrtCue

DOUBT_STATES = frozenset({"KEEP", "ADOPT", "FIT_CONTEXT", "UNRESOLVED"})
# Common-sense bounds for a finished clip (boundary sanity, not a judgement of the content).
CLIP_MIN_SECONDS = 60.0   # TASK-5 item 9 (gap report P1: a talk clip is at least 60 s)
CLIP_MAX_SECONDS = 600.0
LABEL_ANCHOR_TOLERANCE_S = 0.5   # ASR timing may move a little when the cut moves; the anchor is still the same speech


class DedupeResult(TypedDict, total=False):
    decision: str | None


class CutProbe(TypedDict, total=False):
    duration: str | int | float


class GateDoubt(TypedDict, total=False):
    id: str
    state: str | None
    reason_key: str | None


class AnchorLabel(TypedDict):
    current: str
    abs_start_ms: int | float
    abs_end_ms: int | float
    doubt_id: NotRequired[str | None]


class SpeakerSession(TypedDict, total=False):
    speaker_mode: str | None
    enablement_evidence: str | None


class IntroResult(TypedDict, total=False):
    status: str
    intro_id: str
    intro_offset_ms: str | int | float | None


class VerdictEvidence(TypedDict, total=False):
    decision: str | None
    duration_s: float
    count: int
    matched: dict[int, AnchorLabel]
    speaker_mode: str | None
    enablement_evidence: str | None
    intro_id: str
    intro_offset_ms: str | int | float | None


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason_code: str = ""
    detail: str = ""
    evidence: VerdictEvidence = field(default_factory=VerdictEvidence)

    @staticmethod
    def passed(**evidence: Unpack[VerdictEvidence]) -> Verdict:
        return Verdict(True, "", "", cast(VerdictEvidence, dict(evidence)))

    @staticmethod
    def failed(code: str, detail: str = "", **evidence: Unpack[VerdictEvidence]) -> Verdict:
        return Verdict(False, code, detail, cast(VerdictEvidence, dict(evidence)))


# ----- stage 0 ------------------------------------------------------------------------------
def dedupe_gate(result: DedupeResult) -> Verdict:
    """H62 (维护者): only a complete duplicate blocks.  HAS_EXTRA and UNKNOWN are recorded and pass."""
    decision = result.get("decision")
    if decision == "FULL_DUPLICATE":
        return Verdict.failed("DEDUPE_FULL_DUPLICATE", "an external clip fully covers this interval")
    return Verdict.passed(decision=decision)


# ----- stage 1 ------------------------------------------------------------------------------
def boundary_sanity(cues: Sequence[SrtCue], *, start_cue: int, end_cue: int) -> Verdict:
    """Two common-sense checks on CPA's cut: both cues lie in this source segment, and the clip length is in a
    reasonable range (60 s to 10 min).  The cut itself is CPA's decision and is adopted as given."""
    by_id = {c["id"]: c for c in cues}
    if start_cue not in by_id or end_cue not in by_id or start_cue > end_cue:
        return Verdict.failed("BOUNDARY_OUTSIDE_SEGMENT", f"cut {start_cue}-{end_cue} not in this segment")
    duration = float(by_id[end_cue]["end_s"]) - float(by_id[start_cue]["start_s"])
    if not CLIP_MIN_SECONDS <= duration <= CLIP_MAX_SECONDS:
        return Verdict.failed("BOUNDARY_LENGTH_UNREASONABLE", f"{duration:.2f}s outside 30s-10min")
    return Verdict.passed(duration_s=round(duration, 3))


# ----- stage 2 ------------------------------------------------------------------------------
def cut_gate(*, probe: CutProbe, expected_seconds: float, tolerance: float = 0.1) -> Verdict:
    """Tool-output sanity: the cut must have the expected length."""
    duration = float(probe.get("duration", -1))
    if abs(duration - expected_seconds) > tolerance:
        return Verdict.failed("CUT_DURATION_MISMATCH", f"{duration:.3f}s vs {expected_seconds:.3f}s")
    return Verdict.passed(duration_s=round(duration, 3))


# ----- stage 3 ------------------------------------------------------------------------------
def doubt_gate(doubts: Iterable[GateDoubt], *, released_reasons: set[str]) -> Verdict:
    """A doubt left UNRESOLVED stops the clip.  TASK-5: CPA must choose the most likely writing and mark it low
    confidence, so UNRESOLVED only means CPA gave no usable answer at all (see doubt_policy.decide): a tool output
    failure, not a judgement.  The judgement-level interception (定不下且影响意思) is doubt_policy.HOLD_RULE, applied
    at the package seal, not here."""
    rows = list(doubts)
    blocking: list[str] = []
    for d in rows:
        state = d.get("state")
        if state not in DOUBT_STATES:
            return Verdict.failed("DOUBT_STATE_MISSING", f"doubt {d.get('id')} state={state!r}")
        if state == "UNRESOLVED" and d.get("reason_key") not in released_reasons:
            blocking.append(str(d.get("id")))
    if blocking:
        return Verdict.failed("DOUBT_UNRESOLVED", ",".join(blocking))
    return Verdict.passed(count=len(rows))


def match_label_anchors(rows: Sequence[AnchorLabel], cues: Sequence[SrtCue], *, source_offset_s: float) -> dict[int, AnchorLabel]:
    """Each label's anchor is (source-segment absolute start and end, current text).  Returns {cue id: label} for
    the cue that holds the same speech now.  Raises KeyError with the labels that no cue matches."""
    matched: dict[int, AnchorLabel] = {}
    unmatched: list[str] = []
    for row in rows:
        best = None
        for cue in cues:
            if cue["text"] != row["current"]:
                continue
            start_ms = (source_offset_s + float(cue["start_s"])) * 1000
            end_ms = (source_offset_s + float(cue["end_s"])) * 1000
            if (abs(start_ms - row["abs_start_ms"]) <= LABEL_ANCHOR_TOLERANCE_S * 1000
                    and abs(end_ms - row["abs_end_ms"]) <= LABEL_ANCHOR_TOLERANCE_S * 1000):
                best = cue
                break
        if best is None:
            unmatched.append(f"{row.get('doubt_id')}@{row['abs_start_ms']}")
        else:
            matched[int(best["id"])] = row
    if unmatched:
        raise KeyError(",".join(unmatched))
    return matched


def reviewer_label_gate(rows: Sequence[AnchorLabel], cues: Sequence[SrtCue], *, source_offset_s: float) -> Verdict:
    """维护者's labels are anchored to the source segment's absolute time and the current text.  If no current cue
    holds that speech with that text, the label cannot be placed: stop (no CPA call is made)."""
    try:
        matched = match_label_anchors(rows, cues, source_offset_s=source_offset_s)
    except KeyError as exc:
        return Verdict.failed("REVIEWER_LABEL_STALE", f"no current cue matches the anchor of: {exc}")
    return Verdict.passed(count=len(rows), matched=matched)


# ----- stage 4 ------------------------------------------------------------------------------
def speaker_mode_gate(session: SpeakerSession) -> Verdict:
    mode = session.get("speaker_mode")
    if mode == "uniform_host":
        return Verdict.passed(speaker_mode=mode)
    if mode == "lds_guest":
        if not session.get("enablement_evidence"):
            return Verdict.failed("SPEAKER_MODE_NO_ENABLEMENT_EVIDENCE",
                                  "per-speaker colour requires a recorded manual enablement (10-30 onward)")
        return Verdict.passed(speaker_mode=mode, enablement_evidence=session["enablement_evidence"])
    return Verdict.failed("SPEAKER_MODE_UNKNOWN", f"speaker_mode={mode!r}")


# ----- stage 5 ------------------------------------------------------------------------------
def intro_gate(result: IntroResult) -> Verdict:
    """维护者's rule: a finished talk clip must have the branding intro prepended (fail-closed).  A channel whose intro
    manifest says `enabled: false` has none (prepend_intro.py reports NO_INTRO, offset 0): that is the channel's choice."""
    if result.get("status") == "NO_INTRO" and not result.get("intro_id") and result.get("intro_offset_ms") == 0:
        return Verdict.passed(intro_id=None, intro_offset_ms=0)
    if result.get("status") != "PREPENDED":
        return Verdict.failed("INTRO_NOT_PREPENDED", f"status={result.get('status')!r}")
    if not result.get("intro_id"):
        return Verdict.failed("INTRO_ID_MISSING")
    return Verdict.passed(intro_id=result["intro_id"], intro_offset_ms=result.get("intro_offset_ms"))


# ----- stage 6 ------------------------------------------------------------------------------
def title_banned(title: str) -> list[str]:
    """维护者's title prohibitions only (no 直接 / 当场, no 秒 followed by a Chinese character), on the editor's wording:
    the name of a work in 《》 is not checked.  Returns the rules a title breaks (empty = allowed)."""
    from src.autoslice import title_policy as tp      # the one owner of the vocabulary (assets title_policy.json)

    wording = tp._title_wording(title)
    problems = [w for w in tp._TITLE_BANNED_FILLER_WORDS if w in wording]
    if any(pattern.search(wording) for pattern in tp._TITLE_BANNED_REGEXES):
        problems.append("秒+汉字")
    return problems
