"""Doubt voting and the final word on a doubt (MANUAL-SLICE-INTENT U57, U107, U108, U110; TASK-5).

Every cue is a doubt (the current text is only one reading among others).  The readings vote:
- the current text votes once, as family `current`;
- all Gemini listening is one family, `gemini` (3.8 Low and argon; a version number is not another vendor, U122);
- an ASR engine is its own family (jianying, moss, mai), whether it heard the whole clip or only the cue's window.
Readings form one cluster only when their original texts agree after trimming outer whitespace.
A cluster with two or more independent families is maintained.  Ties: the current text wins when it is among the
tied clusters; otherwise nothing is maintained and CPA decides between the tied readings.  Ties are never settled
by lexical order.

CPA chooses the word from the full context, with every candidate and its supporters in front of it.  CPA's choice
stands: nothing overrides it after the fact.  A doubt CPA cannot settle is still given its most likely reading,
marked low confidence (`confidence` = 低); it is never silently left as the current text.

A sentence CPA decides not to present as subtitles is recorded with `drop` and omitted by the writer.  Its words and
reason remain in the record; lexical shape does not override the decision.  Low-confidence meaning-affecting decisions
still carry the ordinary hold.  UNRESOLVED without usable text remains unresolved and stops the step.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, NotRequired, TypeVar, TypedDict, cast

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue
    from .evidence_attach import SourcedSpeakerSegment
    from .name_consistency import NameFix
    from .overlap_join import OverlapInfo
    from .work_lookup import LookupResult


class DecisionView(TypedDict, total=False):
    id: str
    state: str | None
    final_text: str | None
    affects_meaning: bool | None
    confidence: str | None
    dropped: bool | None


class Decision(DecisionView, total=False):
    """CPA decision fields consumed by the escalation and subtitle stages."""

    source: str
    maintained: bool
    drop_refused_written: bool
    drop_routed: bool
    drop_said: str


class NeighbourTrim(TypedDict):
    before: str
    after: str
    joins: list[OverlapInfo]


class MaintenanceView(TypedDict, total=False):
    maintained: bool
    reading: str
    families: list[str]
    candidates: dict[str, ClusterSummary]
    current_held: bool


class FinalDoubt(Decision, total=False):
    cue_id: int
    current: str
    reason_key: str | None
    other_speaker_text: str
    other_first: bool
    other_span_s: list[float]
    neighbour_trim: NeighbourTrim
    abs_start_ms: int
    abs_end_ms: int
    start_s: float
    end_s: float
    readings: dict[str, str]
    differing: list[str]
    maintenance: MaintenanceView
    candidates: list[Candidate]
    danmaku: list[str]
    asr_notes: list[str]
    read_aloud: str
    speaker_note: str
    speaker_segments: list[SourcedSpeakerSegment]
    reasons: list[str]
    reason: str | None
    basis: str | None
    cpa_call: str | None
    work_lookup: LookupResult
    first_pass: CpaAnswer
    settled_at_level: str
    unsettled: bool
    name_fix: list[NameFix]


class CpaAnswer(TypedDict, total=False):
    id: JsonValue
    state: JsonValue
    final_text: JsonValue
    confidence: JsonValue
    affects_meaning: JsonValue
    drop: JsonValue
    reason_key: JsonValue
    against_maintained: JsonValue
    reason: JsonValue
    basis: JsonValue
    cpa_call: JsonValue
    other_speaker_text: JsonValue
    other_first: JsonValue
    work_query: JsonValue
    listen_requests: JsonValue


_DecisionRow = TypeVar("_DecisionRow", bound=DecisionView)


class ReadingCluster(TypedDict):
    text: str
    key: str
    families: set[str]
    sources: list[str]


class ClusterSummary(TypedDict):
    families: list[str]
    sources: list[str]


class Maintenance(TypedDict):
    maintained: bool
    reading: str
    families: list[str]
    tie: bool
    current_held: bool
    candidates: dict[str, ClusterSummary]


class Candidate(TypedDict):
    text: str
    source: str
    sources: list[str]
    families: NotRequired[list[str]]
    supporters: NotRequired[int]

MIN_INDEPENDENT_FAMILIES = 2
DOUBT_STATES = frozenset({"KEEP", "ADOPT", "FIT_CONTEXT", "UNRESOLVED"})
# CPA's explicit "this sentence is not subtitled at all": ADOPT or FIT_CONTEXT, with this field true.
# The decision carries an empty final_text; this is not a fifth state.
DROP_FIELD = "drop"
# Descriptive labels for the audit record, not limits on CPA's drop decision.
DROP_TO_NOTHING = "nothing"
DROP_TO_REPEAT = "repeat"
DROP_TO_LONE_CHAR = "lone_char"
CURRENT_FAMILY = "current"
FILLERS = "嗯呃啊呀哦哈唉诶欸哎额"
GEMINI_FAMILY = "gemini"
# Listening slugs that are Gemini.  36h / 37m (3.6 / 3.7 Low) are no longer listened to; they only appear in old
# records and still count as the same family (U122).
GEMINI_SLUGS = frozenset({"38m", "argon", "36h", "37m"})


def norm_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).lower()


def sim_key(text: str) -> str:
    """Normalised comparison key, with filler words removed for the existing comparison consumers."""
    return "".join(ch for ch in norm_text(text) if ch not in FILLERS)


def witness_family(source: str) -> str | None:
    """Independent-source family of one reading key, or None when the key cannot vote.

    bcut is the current text itself and danmaku is a clue only, so neither votes here (the current text votes
    through `clusters`, as CURRENT_FAMILY).
    """
    if source.startswith("audio:"):
        slug = source[len("audio:"):].split("@", 1)[0]
        if slug in GEMINI_SLUGS or "gemini" in slug.lower():
            return GEMINI_FAMILY
        return "audio:" + slug
    if source == "jianying":
        return "asr:jianying"
    if source.startswith("asr:"):
        # one engine is one family whatever it heard: the whole clip (asr:mai) or only this sentence's window
        # (asr:mai@win, TASK-11); a re-recognition by the same vendor is not another independent source (U122)
        return source.split("@", 1)[0]
    return None


def voiced(text: str) -> bool:
    """A cue carries speech unless it is empty or laughter only (U112: 笑声 and 无实意的声音 are not subtitled).  The
    answer words 嗯 啊 吧 are speech and stay."""
    from .normalize import (
        strip_laughter,  # local import: normalize imports the surface canon, not this module
    )

    return bool(strip_laughter(str(text or "")).strip())


def shortest_cycle(key: str) -> str | None:
    """The shortest block whose repetition is exactly `key` (the unit a repeated sentence repeats), or None.
    One character for 当当当当当 / 咚咚咚; two or more for 不行不行 ("不行"), a word said twice."""
    n = len(key)
    for size in range(1, n // 2 + 1):
        if n % size == 0 and key[:size] * (n // size) == key:
            return key[:size]
    return None


def drop_kind(text: str) -> str:
    """Describe the text shape in the drop record; this classification does not decide whether to drop."""
    from . import content_compare
    from .normalize import strip_laughter

    body = strip_laughter(str(text or ""))
    key = content_compare.content_key(body)
    if not key:
        return DROP_TO_NOTHING
    if len(key) == 1:
        return DROP_TO_LONE_CHAR
    cycle = shortest_cycle(key)
    if cycle is None or len(cycle) != 1:
        return ""
    return DROP_TO_REPEAT


def is_dropped(doubt: DecisionView) -> bool:
    """A decided doubt whose sentence is not subtitled at all (TASK-32).  `decide` is the one producer and sets
    `dropped`; a row written before TASK-32 has no such key, so the older shape (ADOPT with no text) is not a drop.
    The state stays ADOPT — the sentence was settled — and the text is empty because nothing is written for it."""
    return doubt.get("dropped") is True and doubt.get("state") in {"ADOPT", "FIT_CONTEXT"} \
        and not str(doubt.get("final_text") or "").strip()


# Vote schemes (U109, U131: the counting rule is a heuristic; the one closest to the truth on the training set is the
# default).  u59: ASR engines and Gemini are listening sources, Gemini versions one family, the current text one vote.
# no_asr: ASR readings are candidates only (not counted).  no_current: the current text is not a vote.  list_only:
# candidates carry their sources only; CPA gets no "how many agree" hint.
VOTE_SCHEMES = ("u59", "no_asr", "no_current", "list_only")
DEFAULT_VOTE_SCHEME = "u59"


def vote_scheme() -> str:
    scheme = os.environ.get("MANUAL_LANE_VOTE_SCHEME", DEFAULT_VOTE_SCHEME)
    if scheme not in VOTE_SCHEMES:
        raise ValueError(f"MANUAL_LANE_VOTE_SCHEME must be one of {VOTE_SCHEMES}, got {scheme!r}")
    return scheme


def counts(family: str, scheme: str) -> bool:
    """Does a reading of this family count as a vote under the scheme?"""
    if family == CURRENT_FAMILY:
        return scheme not in ("no_current",)
    if family.startswith("asr:"):
        return scheme != "no_asr"
    return True


def clusters(readings: Mapping[str, str], current: str, *, scheme: str | None = None) -> list[ReadingCluster]:
    """Group identical original readings (outer whitespace aside), with factual per-scheme family counts.

    The first raw text is displayed; sources include voting and non-voting witnesses.  Family membership only says
    who gave this exact text, not whether it is semantically correct or should be selected.
    """
    scheme = scheme or vote_scheme()
    out: list[ReadingCluster] = []

    def add(text: str, family: str, source: str) -> None:
        key = text.strip()
        if not key:
            return
        voting = counts(family, scheme)
        for cluster in out:
            if key == cluster["key"]:
                if voting:
                    cluster["families"].add(family)
                cluster["sources"].append(source)
                return
        out.append({"text": text, "key": key, "families": {family} if voting else set(), "sources": [source]})

    add(current, CURRENT_FAMILY, "current")
    for source, text in readings.items():
        family = witness_family(source)
        if family is not None:
            add(text, family, source)
    return out


def maintenance(readings: Mapping[str, str], current: str) -> Maintenance:
    """The non-current reading held by two or more independent families (`maintained`), or none.  `current_held`
    says the current text itself is the one held (then nothing is maintained as a change).  Ties: see the module
    docstring; a tie that does not include the current text maintains nothing and is flagged `tie`."""
    cl = clusters(readings, current)
    best = max((len(c["families"]) for c in cl), default=0)   # no voting text at all: nothing to maintain
    summary = _summary(cl)
    none: Maintenance = {"maintained": False, "reading": "", "families": [], "tie": False, "current_held": False,
                         "candidates": summary}
    if best < MIN_INDEPENDENT_FAMILIES:
        return none
    tops = [c for c in cl if len(c["families"]) == best]
    if len(tops) > 1:
        current_tops = [c for c in tops if c["sources"][0] == "current"]
        if not current_tops:
            return cast(Maintenance, dict(none, tie=True))
        top = current_tops[0]
    else:
        top = tops[0]
    if top["sources"][0] == "current":
        return cast(Maintenance, dict(none, current_held=True, families=sorted(top["families"]), tie=len(tops) > 1))
    return {"maintained": True, "reading": top["text"], "families": sorted(top["families"]),
            "tie": len(tops) > 1, "current_held": False, "candidates": summary}


def _summary(cl: Sequence[ReadingCluster]) -> dict[str, ClusterSummary]:
    return {c["text"]: {"families": sorted(c["families"]), "sources": sorted(c["sources"])} for c in cl}


def candidates(readings: Mapping[str, str], current: str) -> list[Candidate]:
    """Every reading the case can be settled to: the current text first, then each distinct reading.  Each names the
    independent sources that give it (`supporters` = number of voting families, `families` = which).  Under the
    list_only scheme a candidate carries its sources only."""
    scheme = vote_scheme()
    out: list[Candidate] = []
    for c in clusters(readings, current, scheme=scheme):
        sources = sorted(c["sources"])
        row: Candidate = {"text": c["text"], "source": "current" if c["sources"][0] == "current" else sources[0],
                          "sources": sources}
        if scheme != "list_only":
            row["families"] = sorted(c["families"])
            row["supporters"] = len(c["families"])
        out.append(row)
    return out


VOTE_PROMPT = {
    "u59": ("- supporters：有几家独立来源给出它；families：是哪几家（音频模型 gemini 为一家，含 Gemini 3.8 与 argon；ASR 引擎 "
            "asr:jianying、asr:mai、asr:moss 各为一家；current 表示现稿本身的一票）；",
            "1. 两家及以上给出同一原文（supporters ≥ 2）可作为一项证据；结合所有原始读法与上下文独立判断，"
            "不必按票数选择。若不选维护读法，在 against_maintained 说明依据。"),
    "no_asr": ("- supporters：有几家独立的听音来源给出它；families：是哪几家（只算音频模型：gemini 为一家，含 Gemini 3.8 与 "
               "argon；current 表示现稿本身的一票）。ASR 引擎（jianying、asr:mai、asr:moss）的读法照常列为候选，写在 sources "
               "里，但不计入 supporters；",
               None),
    "no_current": ("- supporters：有几家独立来源给出它；families：是哪几家（音频模型 gemini 为一家，含 Gemini 3.8 与 argon；ASR "
                   "引擎 asr:jianying、asr:mai、asr:moss 各为一家）。现稿本身不算一票；",
                   None),
    "list_only": ("- sources：哪些来源给出了这个读法（current 为现稿；jianying、asr:mai、asr:moss 为识别引擎；audio:* 为音频模型"
                  "盲听）。这里不按家数计票；",
                  "1. 各候选只列出来源，不按家数计票：结合各来源、听写与全片上下文，自己判断最可能的写法。"),
}


def vote_prompt_fields() -> dict[str, str]:
    """The {supporters_doc} and {vote_rule} blocks of the adjudication prompt for the current scheme."""
    from .lane_rules import rules

    scheme = vote_scheme()
    doc, rule = VOTE_PROMPT[scheme]
    floor = int(rules("candidates").get("single_source_min_chars") or 0)
    addition = (f"\n单来源新增规则：相对现稿新增至少 {floor} 个实义字的一段话，必须有不止一家独立来源给出，"
                "否则不加进本句（final_text 和 other_speaker_text 同样适用），只作为低把握备选。"
                "这按段判断，不是整句大部分相同就能带入独有尾段。剪映、MAI、MOSS、Gemini 一律同口径；"
                "同厂家不同版本、整片/窗口识别、asr_notes、speaker_segments、分人听写算同一家，"
                "上下文、匿名簇和上一轮 CPA 答案不算独立来源。supporters=0 的候选不是应保留清单。"
                "两家以上独立来源支持的新增照常可选；说话人本人真实重复按现行重复规则保留，"
                "跨句归属证据不足时宁重复不丢字，不把重复次数差当作新内容。") if floor else ""
    exact = "候选仅按原文去首尾空白后是否完全相同归组；"
    if scheme != "list_only":
        exact += "supporters 是当前计票方案下给出该原文的独立来源家数；"
    exact += "这不是封闭答案，也不是机器认定不同读法的语义关系，结合完整证据自行判断。"
    return {"supporters_doc": doc + "\n" + exact, "vote_rule": cast(str, rule or VOTE_PROMPT["u59"][1]) + addition}


def decide(row: CpaAnswer | None, current: str, *, maintained: bool) -> Decision:
    """Final state of one doubt from CPA's row.  CPA's choice stands (TASK-5 item 3: no after-the-fact override).

    KEEP means CPA chose the current text.  ADOPT and FIT_CONTEXT carry the text CPA chose; without one they cannot be
    applied and become UNRESOLVED (the gate stops the clip).  UNRESOLVED with a text is CPA's best guess and becomes a
    low-confidence FIT_CONTEXT.  `maintained` is only recorded.

    CPA may explicitly omit a whole cue with ADOPT/FIT_CONTEXT and `drop` true.  Preserve its actual-word field in
    `drop_said`, leave `final_text` empty for the writer, and retain CPA's confidence and meaning judgment for holds.
    """
    row = row or {}
    raw_state = row.get("state")
    state = raw_state if raw_state in DOUBT_STATES else "UNRESOLVED"
    final_text = str(row.get("final_text") or "").strip()
    confidence = "低" if str(row.get("confidence") or "") == "低" else "高"
    dropped = False
    said = ""
    asked = False
    if state == "KEEP":
        final_text = current
    elif state in {"ADOPT", "FIT_CONTEXT"}:
        asked = row.get(DROP_FIELD) is True
        if asked:
            said, dropped, final_text = final_text, True, ""
        elif not final_text:                                 # no text and no explicit drop
            state = "UNRESOLVED"
    elif raw_state == "UNRESOLVED" and final_text:   # CPA said it could not settle, but gave its best guess
        state, confidence = "FIT_CONTEXT", "低"
    affects = row.get("affects_meaning") is not False
    return {"state": state, "final_text": final_text, "affects_meaning": affects,
            "source": "CPA", "maintained": maintained, "confidence": confidence, "dropped": dropped,
            "drop_refused_written": False, "drop_routed": False, "drop_said": said if asked else ""}





HOLD_RULE = ("定不下且影响意思（唯一不放行的情况）：结合全片证据和已选写法后，剩余的不确定性仍定不下"
             "（confidence 为「低」，或没有可用的写法），并且它会改变观众理解的句意"
             "（affects_meaning 不是 false；没有标注视同会改变）。曾有不同候选本身不等于仍影响意思。"
             "其余疑点（包括低把握但不影响意思的）按选定的写法直接采用。")


def is_hold(doubt: DecisionView) -> bool:
    """HOLD_RULE for one doubt row.  `affects_meaning` missing (a producer that does not say) counts as affecting the
    meaning: an unknown never widens what may be released."""
    undecided = doubt.get("state") == "UNRESOLVED" or str(doubt.get("confidence") or "") == "低"
    return undecided and doubt.get("affects_meaning") is not False


def holds(doubts: Sequence[_DecisionRow]) -> list[_DecisionRow]:
    return [d for d in doubts if is_hold(d)]


def repeated_runs(text: str, *, min_len: int = 3) -> list[str]:
    """Runs of `min_len` normalised characters that occur twice in `text`, not overlapping (e.g. 是真的 in
    他开始推谁是真的是真的杀呀)."""
    t = norm_text(text)
    runs: set[str] = set()
    for k in range(len(t) - min_len + 1):
        s = t[k:k + min_len]
        positions = [i for i in range(len(t) - min_len + 1) if t[i:i + min_len] == s]
        if len(positions) >= 2 and positions[1] >= positions[0] + min_len:
            runs.add(s)
    return sorted(runs)


def is_concatenation(final_text: str, candidate_texts: Sequence[str]) -> bool:
    """A chosen reading is a splice when it repeats a run of 3+ characters that no single candidate carries itself:
    the words were taken from two candidates and joined (他开始推谁是真的 + 是真的杀呀 -> 是真的 twice)."""
    runs = repeated_runs(final_text)
    if not runs:
        return False
    cand_norms = [norm_text(c) for c in candidate_texts]
    return any(not any(cn.count(s) >= 2 for cn in cand_norms) for s in runs)
