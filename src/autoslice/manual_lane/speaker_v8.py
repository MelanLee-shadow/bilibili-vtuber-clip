"""Host or guest, per subtitle cue, for a linked (联动) clip (TASK-5 item 8; U110 联动必分, U114 二分, U115 两类样式).

The decision uses the frozen v8 classifier's own rule, not a threshold fitted on any truth labels: a unit is the host
when the classifier's top class is 李豆沙 and its probability reaches the v8 classifier's own 中档 minimum
(classifier.json tiers.mid.min_conf = 0.6).  Anything else is the guest colour: uncertain means guest (U114).
A final cue takes the probability mean over the ASR units it overlaps; a unit the model could not embed is ignored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict, cast

from src.autoslice.branding_intro import CHANNEL_PROFILE

if TYPE_CHECKING:
    from .config import SpeakerPeople
    from .lane_rules import SpeakerRules


class MillisecondSpan(TypedDict):
    start_ms: int
    end_ms: int


class SpeakerUnit(MillisecondSpan):
    id: str


class RawPrediction(TypedDict, total=False):
    ok: bool
    probs: dict[str, float]


class PredictionWithId(RawPrediction):
    id: str


class UnitDecision(TypedDict):
    host: bool
    conf: float
    top: str


class AcousticRow(TypedDict):
    speaker: str
    label_from: NotRequired[str]
    v8_speaker: NotRequired[str]
    clusters: NotRequired[dict[str, str]]
    review_route: NotRequired[str]
    v8_mid: NotRequired[str]
    speaker_confidence: NotRequired[str]
    speaker_why: NotRequired[str]


class SpeakerDecision(MillisecondSpan, AcousticRow, total=False):
    conf: float
    units: int
    host_prob: float | None
    source: str
    reviewer_truth_key: str
    switches_ms: list[int]
    switch_engine: str
    speaker_acoustic: str
    speaker_how: str
    speaker_cpa: str | None
    speaker_cpa_evidence: str
    speaker_cpa_reason: str
    speaker_cpa_refused: str
    split: list[SpeakerPart]
    split_refused: str
    screen_subtitles: list[str]
    screen_match: float
    video_subtitled: bool        # the watched video carries its own subtitles (step4_speaker._already_on_screen)
    on_screen: float             # the share of the cue's letters those subtitles show around it
    burned_from: str             # a host cue partly on the screen: its text, and the text that is burned instead
    burned_text: str


class IvanSpeakerLabel(TypedDict):
    abs_start_ms: int
    abs_end_ms: int
    speaker: NotRequired[str]
    truth_key: NotRequired[str]


class SpeakerChunk(TypedDict):
    id: str
    s: int
    e: int
    lw: int
    cue: NotRequired[int]
    seg: NotRequired[str]


class ChunkPrediction(TypedDict):
    s: int
    e: int
    host_prob: float


class MixedSpeakers(TypedDict):
    note: str
    other_span_s: list[float]
    minority: str


class EngineSegment(MillisecondSpan, total=False):
    speaker: str


class ClusterName(TypedDict):
    speaker: str
    host_ms: NotRequired[float]
    guest_ms: NotRequired[float]
    cos: NotRequired[float]


class ClusterCue(TypedDict):
    speaker: str
    spans: bool
    ms: dict[str, int]


class Centroid(TypedDict):
    cos: float


class SpeakerReview(TypedDict, total=False):
    speaker: str | None
    evidence: str | None
    quote: str | None
    n: int | str | None
    reason: str | None
    split: SpeakerSplit | None
    host_part: str | None        # of the words a self-subtitled video does not show: the ones the host says


class AdoptedSpeaker(TypedDict):
    speaker: str
    how: str
    refused: NotRequired[str]


class SpeakerSplit(TypedDict, total=False):
    first: str
    second: str
    first_speaker: str
    second_speaker: str


class SpeakerPart(MillisecondSpan):
    text: str
    speaker: str

# The labels come from the channel profile, the one definition the finished-media lane checks (FMR A3,
# review_package_ass_audit HOST_SPEAKER / GUEST_SPEAKER).  U115 allows the guest's name or 「连线」 on screen; the label
# is metadata (the renderer only picks the LDS / GUEST style from it), so the checked canonical label is written.
HOST = CHANNEL_PROFILE.host_speaker_label
GUEST_LABEL = CHANNEL_PROFILE.guest_speaker_label
V8_MID_MIN_CONF = 0.6        # classifier.json: tiers.mid.min_conf


def decide_units(preds: Mapping[str, RawPrediction], host_min: float = V8_MID_MIN_CONF
                 ) -> dict[str, UnitDecision]:
    """Per unit id: {"host": bool, "conf": float, "top": class} for every unit the model embedded (ok)."""
    out: dict[str, UnitDecision] = {}
    for unit_id, row in preds.items():
        if not row.get("ok"):
            continue
        probs = {str(k): float(v) for k, v in dict(row.get("probs") or {}).items()}
        if not probs:
            continue
        top = max(probs, key=probs.__getitem__)
        out[unit_id] = {"host": top == HOST and probs[top] >= host_min, "conf": probs[top], "top": top}
    return out


def cue_speakers(cues: Sequence[MillisecondSpan], units: Sequence[SpeakerUnit],
                 preds: Mapping[str, RawPrediction], host_min: float = V8_MID_MIN_CONF) -> list[SpeakerDecision]:
    """cues: [{start_ms,end_ms}] (cut-relative); units: [{id,start_ms,end_ms}] (ASR cues); preds: pred.json rows.

    Returns [{start_ms, end_ms, speaker, conf, units, host_prob}] where speaker is HOST or GUEST_LABEL.  A cue with no
    usable unit is the guest colour (uncertain means guest).  host_min: the host threshold (host_threshold()); each cue
    is decided from its own units only, never averaged with its neighbours (COACH e05 §10 第5款).
    """
    decided = decide_units(preds, host_min)
    out: list[SpeakerDecision] = []
    for cue in cues:
        c0, c1 = int(cue["start_ms"]), int(cue["end_ms"])
        weights: list[tuple[float, float, bool]] = []   # (overlap, conf of host class, host?)
        for unit in units:
            if unit["id"] not in decided:
                continue
            ov = min(c1, int(unit["end_ms"])) - max(c0, int(unit["start_ms"]))
            if ov <= 0:
                continue
            d = decided[unit["id"]]
            host_prob = float(preds[unit["id"]]["probs"].get(HOST, 0.0))
            weights.append((float(ov), host_prob, d["host"]))
        if not weights:
            out.append({"start_ms": c0, "end_ms": c1, "speaker": GUEST_LABEL, "conf": 0.0, "units": 0, "host_prob": None})
            continue
        total = sum(w for w, _, _ in weights)
        host_prob = sum(w * p for w, p, _ in weights) / total
        host = host_prob >= host_min and sum(w for w, _, h in weights if h) / total >= 0.5
        out.append({"start_ms": c0, "end_ms": c1, "speaker": HOST if host else GUEST_LABEL,
                    "conf": round(host_prob if host else 1 - host_prob, 4), "units": len(weights),
                    "host_prob": round(host_prob, 4)})
    return out


def people(session: SpeakerPeople) -> int:
    """How many people speak in the session: session.speaker_count when recorded, else its participant groups."""
    return int(session.get("speaker_count") or len(session.get("participants") or []) or 1)


def host_threshold(n_people: int, cfg: SpeakerRules) -> float:
    """COACH e05 §10 第1/2款 (lane_rules speaker): three or more people -> multi_person_host_min, else
    two_person_host_min."""
    many = n_people >= int(cfg["multi_person_from"])
    return float(cfg["multi_person_host_min"] if many else cfg["two_person_host_min"])


def mid_band(row: SpeakerDecision, n_people: int, cfg: SpeakerRules) -> str:
    """COACH e05 §10 第3款: "tie" when two people speak and the cue's v8 host probability lies in two_person_mid (the
    review reads it by turn-taking; unsure = guest); "guest_unless_content" when three or more speak and it lies between
    the two-person and the many-person thresholds (guest unless explicit content evidence); "" otherwise."""
    p = row.get("host_prob")
    if p is None or not cfg.get("review_mid"):
        return ""
    if n_people >= int(cfg["multi_person_from"]):
        lo, hi = float(cfg["two_person_host_min"]), float(cfg["multi_person_host_min"])
        return "guest_unless_content" if lo <= float(p) < hi else ""
    lo, hi = (float(x) for x in cfg["two_person_mid"])
    return "tie" if lo <= float(p) <= hi else ""


def apply_reviewer_labels(decided: Sequence[SpeakerDecision], cues_abs: Sequence[tuple[int, int]],
                      labels: Sequence[IvanSpeakerLabel]) -> list[SpeakerDecision]:
    """维护者's own labels win over the classifier where they hold (TASK-5 round 2: 有 维护者 标注的句子用标注，其余用 v8).

    cues_abs[i] is the absolute window of decided[i].  A label covers a cue when the overlap is more than half of the
    cue's duration; the largest such label wins.  The label's speaker name is mapped to HOST or GUEST_LABEL.
    """
    out: list[SpeakerDecision] = []
    for item, (c0, c1) in zip(decided, cues_abs, strict=True):
        duration = float(c1 - c0)
        best, best_ov = None, 0.0
        for lab in labels:
            ov = min(float(c1), float(lab["abs_end_ms"])) - max(float(c0), float(lab["abs_start_ms"]))
            if duration > 0 and ov > best_ov and ov > 0.5 * duration:
                best, best_ov = lab, ov
        row = cast(SpeakerDecision, dict(item, source="v8"))
        if best is not None:
            name = str(best.get("speaker") or "")
            row["speaker"] = HOST if name == HOST else GUEST_LABEL
            row["source"] = "维护者"
            row["reviewer_truth_key"] = str(best.get("truth_key") or "")
        out.append(row)
    return out


# ----- two people inside one sentence (U111, U129): v8 on short chunks of each cue, before the subtitles are decided ---
CHUNK_MS = 1200                       # chunk length; a cue shorter than two chunks is one chunk (no split possible)
GUEST_MAX_HOST_PROB = 1 - V8_MID_MIN_CONF   # symmetric to the host rule: a chunk is confidently the other speaker


def cue_chunks(cue_id: int, start_ms: int, end_ms: int, prefix: str) -> list[SpeakerChunk]:
    """~1.2 s chunks of one cue, as v8 units ({id, seg, s, e, lw, cue})."""
    n = max(1, round((end_ms - start_ms) / CHUNK_MS))
    step = (end_ms - start_ms) / n
    return [{"id": f"{prefix}-k{cue_id}-{i}", "s": round(start_ms + i * step), "e": round(start_ms + (i + 1) * step),
             "lw": round(start_ms + (i + 1) * step), "cue": cue_id} for i in range(n)]


def mixed_speakers(chunks: Sequence[ChunkPrediction]) -> MixedSpeakers | None:
    """When one cue holds a confident host chunk and a confident other-speaker chunk: {note, other_span_s}, where the
    other span covers the chunks of the side with fewer chunks (the interjection).  None otherwise."""
    host = [c for c in chunks if float(c["host_prob"]) >= V8_MID_MIN_CONF]
    other = [c for c in chunks if float(c["host_prob"]) <= GUEST_MAX_HOST_PROB]
    if not host or not other:
        return None
    minority, label = (other, "另一人") if len(other) <= len(host) else (host, HOST)
    span = [min(c["s"] for c in minority) / 1000, max(c["e"] for c in minority) / 1000]
    def fmt(xs: Sequence[ChunkPrediction]) -> str:
        return "、".join(f"{c['s'] / 1000:.1f}–{c['e'] / 1000:.1f}" for c in xs)

    return {"note": f"声纹（按约 1.2 秒分段）：{fmt(host)} 秒像{HOST}，{fmt(other)} 秒像另一人；这一句里可能有两个人说话，"
                    f"较短的一方（{label}）在 {span[0]:.1f}–{span[1]:.1f} 秒",
            "other_span_s": span, "minority": label}


# ----- the ASR engines' anonymous speaker clusters, named by the host's voiceprint (TASK-11 item 6, U144) -----------
# The commercial practice: cluster the clip into anonymous speakers first, then name each cluster from an enrolled
# voiceprint (pyannoteAI /identify, Speechmatics, Tencent, iFlytek).  Here the whole-clip MOSS (moss-transcribe-diarize
# -pro) and MAI responses already carry the clusters; the v8 host voiceprint names them.  A cue where the named cluster
# and v8 disagree, or whose time spans two clusters named differently, is not settled by either: low confidence, for
# CPA to read with the context (uncertain still means guest, U114).
SPAN_MIN_MS = 300            # a cluster counts inside a cue when it covers at least this much of it


def name_clusters(segments: Sequence[EngineSegment], decided: Sequence[SpeakerDecision]) -> dict[str, ClusterName]:
    """{cluster: {"speaker": HOST | GUEST_LABEL, "host_ms", "guest_ms"}}: each cluster is the speaker v8 heard for
    most of the cue time it overlaps (a tie is the guest).  segments: [{start_ms, end_ms, speaker}] of one engine;
    decided: cue_speakers rows."""
    weight: dict[str, dict[str, float]] = {}
    for seg in segments:
        cluster = seg.get("speaker")
        if not cluster:
            continue
        w = weight.setdefault(str(cluster), {"host_ms": 0.0, "guest_ms": 0.0})
        for cue in decided:
            ov = min(int(seg["end_ms"]), int(cue["end_ms"])) - max(int(seg["start_ms"]), int(cue["start_ms"]))
            if ov > 0:
                w["host_ms" if cue["speaker"] == HOST else "guest_ms"] += ov
    return {c: cast(ClusterName, dict(w, speaker=HOST if w["host_ms"] > w["guest_ms"] else GUEST_LABEL))
            for c, w in weight.items()}


def cue_clusters(cues: Sequence[MillisecondSpan], segments: Sequence[EngineSegment],
                 names: Mapping[str, ClusterName]) -> list[ClusterCue | None]:
    """Per cue: {"speaker": the named cluster with most overlap, "spans": the cue holds two clusters named differently}
    or None when no segment of the engine overlaps it."""
    out: list[ClusterCue | None] = []
    for cue in cues:
        by_label: dict[str, float] = {}
        for seg in segments:
            cluster = seg.get("speaker")
            if not cluster or str(cluster) not in names:
                continue
            ov = min(int(seg["end_ms"]), int(cue["end_ms"])) - max(int(seg["start_ms"]), int(cue["start_ms"]))
            if ov > 0:
                label = names[str(cluster)]["speaker"]
                by_label[label] = by_label.get(label, 0.0) + ov
        if not by_label:
            out.append(None)
            continue
        out.append({"speaker": max(by_label, key=by_label.__getitem__),
                    "spans": sum(1 for v in by_label.values() if v >= SPAN_MIN_MS) > 1,
                    "ms": {k: round(v) for k, v in by_label.items()}})
    return out


def name_by_voiceprint(centroids: Mapping[str, Centroid]) -> dict[str, ClusterName] | None:
    """8c1 (TASK-14, test pre-registration TEST-PREREG / RESULT sections 12-13): each MOSS cluster's centroid of CAM++
    segment embeddings against the host's enrolled voiceprint (enroll_lds_1, v8's own embedder); only the cluster with
    the highest cosine is the host, every other cluster is the guest.  None when there is no choice to make (fewer than
    two clusters): the caller keeps v8."""
    if len(centroids) < 2:
        return None
    top = max(centroids, key=lambda k: float(centroids[k]["cos"]))
    return {k: {"speaker": HOST if k == top else GUEST_LABEL, "cos": round(float(v["cos"]), 4)}
            for k, v in centroids.items()}


def fuse(decided: Sequence[SpeakerDecision], engines: Mapping[str, Sequence[ClusterCue | None]],
         primary: str = "moss") -> list[SpeakerDecision]:
    """The label is the primary engine's voiceprint-named cluster (8c1: held-out 7 older clips 491/556 against v8
    455/556, test three 239/279 against 234/279; thin: the test gain comes from one clip); v8 where that engine has no
    cluster for the cue.  A cue is "低" and carries why, for CPA to read with the context, when the label and v8
    disagree, when an engine hears two people inside it, or when another engine's cluster (MAI: an auxiliary signal
    only, never a label) disagrees with the label."""
    out: list[SpeakerDecision] = []
    for i, row in enumerate(decided):
        here = {name: cast(ClusterCue, rows[i]) for name, rows in engines.items() if rows[i] is not None}
        lead = here.get(primary)
        label = lead["speaker"] if lead else row["speaker"]
        why = []
        if lead is None and primary in engines:
            why.append(f"{primary} 簇没有覆盖本句，按 v8")
        elif lead is not None and lead["speaker"] != row["speaker"]:
            why.append(f"{primary} 簇（按主播声纹定名）判为{lead['speaker']}，v8 判为{row['speaker']}")
        why += [f"{k} 簇判为{v['speaker']}" for k, v in here.items() if k != primary and v["speaker"] != label]
        why += [f"{k} 簇显示本句里有两个人（{v['ms']} 毫秒）" for k, v in here.items() if v["spans"]]
        out.append(cast(SpeakerDecision, dict(row, speaker=label, v8_speaker=row["speaker"], clusters={k: v["speaker"] for k, v in here.items()},
                        label_from=primary if lead else "v8",
                        speaker_confidence="低" if why else "高", speaker_why="；".join(why))))
    return out


# ----- what a context reading may change (TASK-18, U150, U151): the acoustic label is the default ------------------
# The meeting-transcription practice (U144): the diarizer owns who spoke; a language model reading the transcript only
# corrects a speaker where the words themselves prove it.  How someone is addressed, who asks and who answers, a
# commenting tone or a third-person name have other readings (she sometimes names herself in the third person, U54),
# so they may only break an acoustic tie.  One definition for step 4 (speaker_review) and step 9 (review).
EVIDENCE_KINDS = ("自称", "只能出自一方")      # self-identification; content only one side could say
FIRST_PERSON = ("我", "咱", "俺", "本人")      # the quoted words must be the speaker speaking of themself


def acoustic_votes(row: AcousticRow) -> list[str]:
    """The acoustic routes heard for one fused cue: the label's own route (8c1 MOSS cluster, or v8 when MOSS gave none),
    v8 beside a cluster label, and every other engine's cluster (MAI)."""
    votes = [str(row["speaker"])]
    if row.get("label_from", "v8") != "v8":
        votes.append(str(row.get("v8_speaker") or row["speaker"]))
    votes += [str(v) for k, v in dict(row.get("clusters") or {}).items() if k != row.get("label_from")]
    return votes


MID_REVIEW = "v8_mid"    # review_route of the rows step 4 builds when no engine gives clusters (step4_speaker._v8_mid_rows)


def mid_tie(row: AcousticRow) -> bool:
    """A cue of the v8-only mid-band review (no engine clusters; TASK-28, COACH e05 §10 第3款) whose v8 host probability
    lies in the two-person mid band: v8's label there is a coin toss, so the review reads the cue as an acoustic tie
    and the clip's lines show it as [?] (TASK-29 ruling 5).  Only rows of that review: with MOSS/MAI clusters (the
    production default, 8c1) the routes vote as before (TASK-29 ruling 1)."""
    return (row.get("review_route") == MID_REVIEW and row.get("v8_mid") == "tie"
            and row.get("speaker_confidence") == "低")


def acoustic_tie(row: AcousticRow) -> bool:
    """The acoustic routes split evenly between the host and the guest (with three routes there is always a side)."""
    if mid_tie(row):
        return True
    votes = acoustic_votes(row)
    return votes.count(HOST) * 2 == len(votes)


def _alnum(text: Any) -> str:
    return "".join(ch for ch in str(text) if ch.isalnum())


def content_evidence(answer: SpeakerReview | None, text: str) -> str:
    """Empty when the answer carries explicit content evidence for its speaker: evidence is one of EVIDENCE_KINDS and
    quote is copied from this cue's own words and has the speaker speaking of themself (FIRST_PERSON).  Otherwise the
    reason it is not evidence."""
    answer = answer or {}
    kind, quote = str(answer.get("evidence") or "").strip(), _alnum(answer.get("quote") or "")
    if kind not in EVIDENCE_KINDS:
        return f"依据不是明确的内容证据（{kind or '未写'}）"
    if not quote or quote not in _alnum(text):
        return "摘抄的证据不在本句原文里"
    if not any(p in quote.replace("我们", "").replace("咱们", "") for p in FIRST_PERSON):   # plural: not who spoke
        return "摘抄的证据里没有说话人说自己"
    return ""


def adopt_review(row: AcousticRow, answer: SpeakerReview | None, text: str) -> AdoptedSpeaker:
    """The label a context reading leaves on one fused cue: {"speaker", "how"[, "refused"]}.  The acoustic label stands
    unless the routes tie (then the reading decides, and 不确定 is the guest colour, U114) or the reading gives explicit
    content evidence (content_evidence).  An answer that is not the host or the guest, or is missing, changes nothing
    on a cue the acoustics decide."""
    said = str((answer or {}).get("speaker") or "").strip()
    current = str(row["speaker"])
    if acoustic_tie(row):
        if said in (HOST, GUEST_LABEL):
            return {"speaker": said, "how": "打平，按语境"}
        return {"speaker": GUEST_LABEL, "how": "打平，拿不准按嘉宾色"}
    if said not in (HOST, GUEST_LABEL) or said == current:
        return {"speaker": current, "how": "按声音证据"}
    why = content_evidence(answer, text)
    if why:
        return {"speaker": current, "how": "按声音证据", "refused": why}
    return {"speaker": said, "how": "内容证据"}


def switch_times(cue: MillisecondSpan, segments: Sequence[EngineSegment], names: Mapping[str, ClusterName]
                 ) -> list[int]:
    """The times (ms) inside a cue where the engine's named speaker changes: between two consecutive segments that
    overlap the cue and are named differently, at the end of the earlier one (or the later one's start when they
    overlap), kept only when it leaves SPAN_MIN_MS on both sides."""
    c0, c1 = int(cue["start_ms"]), int(cue["end_ms"])
    inside = sorted((s for s in segments if s.get("speaker") and str(s["speaker"]) in names
                     and min(c1, int(s["end_ms"])) - max(c0, int(s["start_ms"])) > 0), key=lambda s: int(s["start_ms"]))
    out = []
    for a, b in pairwise(inside):
        if names[str(a["speaker"])]["speaker"] == names[str(b["speaker"])]["speaker"]:
            continue
        t = min(int(a["end_ms"]), int(b["start_ms"])) if int(b["start_ms"]) < int(a["end_ms"]) else int(a["end_ms"])
        if c0 + SPAN_MIN_MS <= t <= c1 - SPAN_MIN_MS:
            out.append(t)
    return out


def switch_sides(cue: MillisecondSpan, switch_ms: int, segments: Sequence[EngineSegment],
                 names: Mapping[str, ClusterName]) -> tuple[str, str] | None:
    """Who the label engine hears before and after one of its switches inside a cue (the label rule, cue_clusters, on
    each side), or None when a side has no cluster."""
    sides = cue_clusters([{"start_ms": int(cue["start_ms"]), "end_ms": switch_ms},
                          {"start_ms": switch_ms, "end_ms": int(cue["end_ms"])}], segments, names)
    if sides[0] is None or sides[1] is None:
        return None
    return str(sides[0]["speaker"]), str(sides[1]["speaker"])


def split_at_switch(cue: MillisecondSpan, text: str, split: SpeakerSplit | None, switches: Sequence[int],
                    segments: Sequence[EngineSegment] = (), names: Mapping[str, ClusterName] | None = None
                    ) -> list[SpeakerPart] | None:
    """The one rule for cutting a cue between two speakers (U150, TASK-18).  CPA gives the two parts of the cue's text
    by context (where the words break); the cut is made at the label engine's speaker switch nearest the time the text
    cut falls at (by character share); each part's speaker is the label rule's own answer on that part's time
    (cue_clusters on the label engine's segments and names: the named cluster with most overlap), so the diarizer, not
    the reading, owns who spoke (U144).  None (no cut) when the parts do not rebuild the text, a speaker is not one of
    the two, both parts are one speaker, no switch lies inside the cue, a part has no cluster, or the label rule does
    not hear the two parts as the reading's two speakers in the reading's order."""
    if not split or not switches:
        return None
    first, second = str(split.get("first") or "").strip(), str(split.get("second") or "").strip()
    who = (str(split.get("first_speaker") or ""), str(split.get("second_speaker") or ""))
    if not first or not second or _alnum(first + second) != _alnum(text) or who[0] == who[1] \
            or not set(who) <= {HOST, GUEST_LABEL}:
        return None
    c0, c1 = int(cue["start_ms"]), int(cue["end_ms"])
    at = c0 + (c1 - c0) * len(_alnum(first)) / max(1, len(_alnum(text)))
    t = min(switches, key=lambda s: abs(s - at))
    if switch_sides(cue, t, segments, names or {}) != who:
        return None
    return [{"start_ms": c0, "end_ms": t, "text": first, "speaker": who[0]},
            {"start_ms": t, "end_ms": c1, "text": second, "speaker": who[1]}]
