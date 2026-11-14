"""Evidence attachment (TASK-6, U123): every piece of evidence reaches the doubt it belongs to, or is recorded.

The relation: for every timed piece of evidence x (an ASR segment of jianying / MAI / MOSS, a blind-listening line, a
danmaku comment), x is attached to the cue(s) it belongs to and shown to CPA with that cue's doubt; what cannot be
attached is counted and named in PROCESS, never dropped silently.  The producers of the evidence call this module;
every adjudication mode (A, B, C, full) reads the attachments, so no consumer has its own time rule.

- ASR segment [start, end]: the cue it overlaps most (primary), plus every cue it overlaps or whose midpoint it spans
  (a segment that runs over two cues still reaches both).  Each of those cues receives only its own slice of the
  segment's text (TASK-17, `segment_slices`): a reading is the words said in that sentence, never the neighbour's
  sentence that the engine happened to put into the same segment.  A segment that overlaps no cue goes to the nearest
  cue as an orphan, whole: "BCUT 可能漏句".  A cue whose primary segments include one whose words are not in the cue's own text gets an
  "extra" note: another segment inside the cue (an interjection, often the other speaker), shown to CPA as such.
- Listening line: owned by the cue of the window it was heard in (a window carries one sentence, U111).  A line whose
  time lies outside its own window's audio is recorded as out of window, never attached to another cue.
- Danmaku sent at t: read aloud up to 90 s later (never earlier than READ_ALOUD_MIN_DELAY_MS after sending), viewer
  reactions up to 30 s after the sentence (docs/pipeline/41-semantic-repair.md, 弹幕时间模型, 维护者).  Per
  cue the comments sent in [start - 90, end + 30] are ranked by text quality and relevance to the cue's readings, never
  by time distance, and cut by count.  A comment that the cue reads closely but not exactly is a read-aloud hit: the
  cue becomes a doubt and the comment's original text goes with it (P1: 念弹幕先找弹幕原文).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from ..chat_read_aloud_match import READ_ALOUD_MIN_DELAY_MS, _match_metrics, normalize_chat_text
from . import doubt_policy
from .recorder_events import event_lookback_s, evidence_texts, evidence_view
from .recorder_events import Danmaku as Danmaku

if TYPE_CHECKING:
    from .normalize import TimedWord
    from .srt_text import SrtCue, TimedText

MIDPOINT_TOL_S = 0.3              # the old midpoint rule's tolerance, kept for the "spans the midpoint" attachment


class ReadAloudRef(TypedDict):
    text: str


class TimedComment(ReadAloudRef):
    dt_s: float


class ReadAloudHit(TimedComment):
    score: float
    coverage: float
    precision: float
    exact: bool


class RankedDanmaku(TimedComment):
    match: float
    quality: float


class AudioWindow(TypedDict):
    audio: list[float]
    cue_ids: list[int]


class AsrSegment(TypedDict):
    start_s: float
    end_s: NotRequired[float]
    text: NotRequired[str]
    speaker: NotRequired[str]


class AsrSlice(AsrSegment):
    seg: int


class AsrOrphan(TypedDict):
    segment: AsrSegment
    cue_id: int
    gap_s: float


class DetachedAsr(TypedDict):
    segment: AsrSegment
    cue_id: int
    seg: int


class SpeakerSegment(TypedDict):
    speaker: str
    start_s: float
    end_s: float
    text: str


class SourcedSpeakerSegment(SpeakerSegment):
    source: str


class WitnessLine(TypedDict):
    abs_start_s: float
    text: str
    end_s: NotRequired[float]
    speaker: NotRequired[str]
    owners: NotRequired[list[int]]
    parts: NotRequired[dict[int, str]]
    words: NotRequired[list[TimedWord]]
    speaker_segments: NotRequired[list[SpeakerSegment]]
EXTRA_COVERAGE = 0.5              # a segment whose words are less than half in the cue's text is another utterance
LINE_WINDOW_TOL_S = 0.5           # a listening line may start this much outside its window's audio (model timing)
DANMAKU_BEFORE_S = 90.0           # read-aloud upper bound (41-semantic-repair: 弹幕时间模型)
DANMAKU_AFTER_S = 30.0            # viewer reactions after the sentence
DANMAKU_PER_CUE = 12              # cut by count, after ranking
READ_ALOUD_MIN_CHARS = 4          # same floor as the main pipeline's read-aloud discovery (chat_proposals)
READ_ALOUD_SCORE = 0.6            # close but not exact: score from chat_repair._match_metrics
READ_ALOUD_COVERAGE = 0.6


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _gap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, max(a0, b0) - min(a1, b1))


def covered_fraction(part: str, whole: str) -> float:
    """How much of `part` (filler words removed) occurs in `whole`, in order.  1.0 when part has no content."""
    p, w = doubt_policy.sim_key(part), doubt_policy.sim_key(whole)
    if not p:
        return 1.0
    common = sum(b.size for b in SequenceMatcher(None, p, w, autojunk=False).get_matching_blocks())
    return common / len(p)


@dataclass
class AsrAttachment:
    """Segments of one ASR source attached to cues.  Every segment has at least one owner (unhandled == 0)."""
    source: str
    by_cue: dict[int, list[AsrSlice]] = field(default_factory=dict)
    orphans: list[AsrOrphan] = field(default_factory=list)
    extra: dict[int, list[AsrSlice]] = field(default_factory=dict)
    total: int = 0
    slices: list[dict[int, str]] = field(default_factory=list)       # per segment: {cue id: that cue's slice}
    segments: list[AsrSegment] = field(default_factory=list)
    detached: list[DetachedAsr] = field(default_factory=list)

    def detach(self, orphan: AsrOrphan) -> None:
        """An orphan becomes a line of its own or is not subtitled (lane_lines, e05 §3): it is no longer a reading of the
        cue it was attached to, and no longer told to CPA as a sentence BCUT may have missed.  Still counted as handled."""
        index = next(i for i, s in enumerate(self.segments) if s is orphan["segment"])
        cid = int(orphan["cue_id"])
        self.by_cue[cid] = [s for s in self.by_cue.get(cid, []) if s["seg"] != index]
        if not self.by_cue[cid]:
            del self.by_cue[cid]
        self.orphans = [o for o in self.orphans if o is not orphan]
        self.detached.append({"segment": orphan["segment"], "cue_id": cid, "seg": index})

    def text(self, cue_id: int) -> str:
        return " ".join(s["text"].strip() for s in self.by_cue.get(cue_id, []) if s["text"].strip()).strip()

    def notes(self, cue_id: int, *, trim: Callable[[str], str] | None = None) -> list[str]:
        """Another view of the same source's words, subject to the same neighbour ownership as its reading."""
        trim = trim or (lambda text: text)
        out = [f"{self.source} 在 {o['segment']['start_s']:.2f}–{o['segment']['end_s']:.2f} 秒有一段「{text}」"
               "，不在任何 BCUT 句的时间内，挂到最近的本句（BCUT 可能漏句）"
               for o in self.orphans if o["cue_id"] == cue_id and (text := trim(str(o["segment"]["text"])))]
        out.extend(f"{self.source} 在本句内另起一段 {s['start_s']:.2f}–{s['end_s']:.2f} 秒「{text}」，现稿里没有这些字"
                   "（可能是另一人插话，或 BCUT 漏听）"
                   for s in self.extra.get(cue_id, []) if (text := trim(str(s["text"]))))
        return out

    def stats(self) -> dict[str, int]:
        owned = {s["seg"] for segs in self.by_cue.values() for s in segs} | {d["seg"] for d in self.detached}
        return {"total": self.total, "attached": len(owned), "orphans": len(self.orphans),
                "orphan_cues": len({o["cue_id"] for o in self.orphans}), "extra_cues": len(self.extra),
                "unhandled": self.total - len(owned)}

    def process_line(self) -> str:
        st = self.stats()
        return (f"ASR 段挂接（{self.source}）：共 {st['total']} 段，已挂接 {st['attached']} 段（其中与任何句都不重叠、"
                f"挂到最近句的 {st['orphans']} 段，成为「BCUT 可能漏句」疑点 {st['orphan_cues']} 句）；句内另起一段 "
                f"{st['extra_cues']} 句；未处理 {st['unhandled']} 段")


def segment_owners(cues: Sequence[SrtCue], start: float, end: float) -> tuple[list[int], bool, float]:
    """(owner cue ids with the primary first, is_orphan, gap to the nearest cue).  Never empty while cues exist."""
    if not cues:
        return [], True, 0.0
    best_id, best_ov = None, 0.0
    for c in cues:
        ov = _overlap(start, end, float(c["start_s"]), float(c["end_s"]))
        if ov > best_ov:
            best_id, best_ov = int(c["id"]), ov
    spans = [int(c["id"]) for c in cues
             if start - MIDPOINT_TOL_S <= (float(c["start_s"]) + float(c["end_s"])) / 2 <= end + MIDPOINT_TOL_S]
    if best_id is not None:
        return [best_id] + [i for i in spans if i != best_id], False, 0.0
    if spans:
        return spans, False, 0.0
    nearest = min(cues, key=lambda c: (_gap(start, end, float(c["start_s"]), float(c["end_s"])), float(c["start_s"])))
    return [int(nearest["id"])], True, _gap(start, end, float(nearest["start_s"]), float(nearest["end_s"]))


SPLIT_ANCHOR = 2                  # an aligned run of at least this many characters marks where a sentence sits
SPLIT_TIME_WEIGHT = 1.0           # a cut one second away from the sentence boundary costs one matched character
SPLIT_NO_BREAK = 0.5              # a cut where the engine wrote no break costs half a matched character
_BREAKS = frozenset("，。！？、；：,.!?;:…-—~～ 　")


def _fold(ch: str) -> str:
    return unicodedata.normalize("NFKC", ch).casefold()


def _counts(ch: str) -> bool:
    return unicodedata.category(ch)[0] not in "PZSC"


def _matched(a: str, b: str) -> int:
    return sum(blk.size for blk in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())


def _cut_points(raw: str, cut_texts: Sequence[str], times: Sequence[tuple[float, float]], start: float, end: float
                ) -> list[int]:
    """Where to cut `raw` (one engine segment over several consecutive sentences) so that piece j is sentence j's.

    The cuts are chosen among the places a sentence can end in the segment's text: a break the engine wrote
    (punctuation or a space), either edge of a run aligned to one sentence's own text, and the point the sentence
    boundary falls on in time (the segment's characters spread evenly over its time).  Never inside a word written in
    Latin letters or digits.  The chosen cuts maximise the characters each piece shares with its own sentence's text,
    less SPLIT_TIME_WEIGHT per second between each cut and its sentence boundary in time and SPLIT_NO_BREAK for a cut
    where the engine wrote no break.  Returns len(cut_texts) - 1 non-decreasing positions in `raw`."""
    pos = [i for i, ch in enumerate(raw) if _counts(ch)]
    key = "".join(_fold(raw[i]) for i in pos)
    keys = ["".join(_fold(ch) for ch in t if _counts(ch)) for t in cut_texts]
    n, k = len(key), len(keys)
    dur = max(1e-6, end - start)

    def safe(p: int) -> bool:   # not inside a run of Latin letters or digits written together
        return not (0 < p < n and key[p - 1].isascii() and key[p - 1].isalnum() and key[p].isascii()
                    and key[p].isalnum() and pos[p] == pos[p - 1] + 1)

    def at_time(t: float) -> int:
        return min(n, max(0, round((min(max(t, start), end) - start) / dur * n)))

    bounds_t = [(times[j][1] + times[j + 1][0]) / 2 for j in range(k - 1)]
    cand = {0, n}
    cand.update(p for p in range(1, n) if any(raw[i] in _BREAKS for i in range(pos[p - 1] + 1, pos[p])))
    for kj in keys:
        for blk in SequenceMatcher(None, kj, key, autojunk=False).get_matching_blocks():
            if blk.size >= SPLIT_ANCHOR:
                cand.update((blk.b, blk.b + blk.size))
    for t in bounds_t:
        p = at_time(t)
        cand.update(q for q in (p - 1, p, p + 1) if 0 <= q <= n)
    order = sorted(p for p in cand if safe(p))

    breaks = {0, n} | {p for p in range(1, n) if any(raw[i] in _BREAKS for i in range(pos[p - 1] + 1, pos[p]))}

    def cost_t(p: int, j: int) -> float:
        return (SPLIT_TIME_WEIGHT * abs(start + p / max(1, n) * dur - bounds_t[j])
                + (0.0 if p in breaks else SPLIT_NO_BREAK))

    # best[j][p]: the best score of pieces 0..j with piece j ending at p (the last piece always ends at n)
    best: list[dict[int, tuple[float, int]]] = [{p: (_matched(key[:p], keys[0]), -1) for p in order}]
    for j in range(1, k):
        row: dict[int, tuple[float, int]] = {}
        ends = [n] if j == k - 1 else order
        for q in ends:
            row[q] = max(((best[j - 1][p][0] - cost_t(p, j - 1) + _matched(key[p:q], keys[j]), p)
                          for p in order if p <= q), key=lambda x: (x[0], -x[1]))
        best.append(row)
    cuts_key, q = [], n
    for j in range(k - 1, 0, -1):
        q = best[j][q][1]
        cuts_key.append(q)
    cuts_key.reverse()
    return [pos[p] if p < n else len(raw) for p in cuts_key]


def _strip_edges(text: str) -> str:
    return text.strip().strip("".join(_BREAKS)).strip()


def segment_slices(cues: Sequence[SrtCue], start: float, end: float, text: str, *,
                   whole_utterance: bool = False) -> tuple[dict[int, str], bool, float]:
    """The one rule for a timed reading over the sentences (TASK-17): ({cue id: that cue's slice of the text}, is_orphan,
    gap).  The candidate cues are the segment's owners (segment_owners: the cue it overlaps most and the cues whose
    midpoint it spans) and every cue it overlaps in time; in time order they share the text by _cut_points.  A cue
    whose slice is empty does not receive the segment.  One candidate, or an orphan, takes the whole text.  When every
    slice comes out empty (a segment of punctuation only), the primary owner keeps the whole text, so no segment is
    ever left without an owner.  A diarized utterance (whole_utterance) stays whole at that same primary owner:
    splitting its words among the neighbouring main-speaker sentences would manufacture several interjections."""
    owners, orphan, gap = segment_owners(cues, start, end)
    if not owners:
        return {}, orphan, gap
    if orphan or whole_utterance:
        return {owners[0]: text}, orphan, gap
    span = [c for c in cues if int(c["id"]) in owners or _overlap(start, end, float(c["start_s"]), float(c["end_s"])) > 0]
    span.sort(key=lambda c: (float(c["start_s"]), float(c["end_s"])))
    if len(span) < 2:
        return {owners[0]: text}, False, 0.0
    cuts = _cut_points(text, [str(c.get("text", "")) for c in span],
                       [(float(c["start_s"]), float(c["end_s"])) for c in span], start, end)
    edges = [0, *cuts, len(text)]
    out = {}
    for j, c in enumerate(span):
        piece = _strip_edges(text[edges[j]:edges[j + 1]])
        if any(_counts(ch) for ch in piece):
            out[int(c["id"])] = piece
    return (out or {owners[0]: text}), False, 0.0


def is_interjection(cues: Sequence[SrtCue], segment: AsrSegment,
                    segments: Sequence[AsrSegment]) -> bool:
    """A source-local second voice overlapping the primary cue's better-matched voice.

    A speaker label alone never turns a long main-speaker reading into an indivisible aside.  Compare only
    overlapping segments of this source; anonymous clusters are neither identities nor independent witnesses.
    """
    if not segment.get("speaker"):
        return False
    start, end = float(segment["start_s"]), float(segment["end_s"])
    owners, _, _ = segment_owners(cues, start, end)
    if not owners:
        return False
    cue = next(c for c in cues if int(c["id"]) == owners[0])
    key = doubt_policy.sim_key(str(cue.get("text", "")))
    own = _matched(key, doubt_policy.sim_key(str(segment["text"])))
    return any(s.get("speaker") and s["speaker"] != segment["speaker"]
               and _overlap(start, end, float(s["start_s"]), float(s["end_s"])) > 0
               and _matched(key, doubt_policy.sim_key(str(s["text"]))) > own for s in segments)


def attach_asr(cues: Sequence[SrtCue], segments: Sequence[AsrSegment], *, source: str) -> AsrAttachment:
    """Attach every segment ({start_s, end_s, text}) of one ASR source to the cues (see the module docstring): each
    owner cue receives its own slice ({start_s, end_s, text, seg}: the segment's times, the slice's text)."""
    att = AsrAttachment(source=source, total=len(segments))
    primary: dict[int, list[AsrSlice]] = {}
    for index, seg in enumerate(segments):
        s = cast(AsrSegment, {"start_s": float(seg["start_s"]), "end_s": float(seg.get("end_s", seg["start_s"])),
                              "text": str(seg.get("text", "")),
                              **({"speaker": str(seg["speaker"])} if seg.get("speaker") else {})})
        slices, orphan, gap = segment_slices(cues, s["start_s"], s["end_s"], s["text"],
                                              whole_utterance=is_interjection(cues, s, segments))
        att.slices.append(slices)
        att.segments.append(s)
        for cid, piece in slices.items():
            att.by_cue.setdefault(cid, []).append(cast(AsrSlice, dict(s, text=piece, seg=index)))
        if orphan and slices:
            att.orphans.append({"segment": s, "cue_id": next(iter(slices)), "gap_s": round(gap, 2)})
        else:   # the "another utterance inside the cue" note keeps its rule: the segments the cue overlaps most
            head = segment_owners(cues, s["start_s"], s["end_s"])[0][0]
            if head in slices:
                primary.setdefault(head, []).append(cast(AsrSlice, dict(s, text=slices[head], seg=index)))
    for segs in att.by_cue.values():
        segs.sort(key=lambda x: x["start_s"])
    text_of = {int(c["id"]): str(c.get("text", "")) for c in cues}
    for cid, segs in primary.items():
        if len(segs) < 2:
            continue
        extra = [s for s in segs if doubt_policy.sim_key(s["text"]) and covered_fraction(s["text"], text_of[cid]) < EXTRA_COVERAGE]
        if extra:
            att.extra[cid] = sorted(extra, key=lambda x: x["start_s"])
    return att


def segments_from_srt_cues(cues: Sequence[TimedText]) -> list[AsrSegment]:
    return [{"start_s": float(c["start_s"]), "end_s": float(c["end_s"]), "text": str(c["text"])} for c in cues]


def own_witness_lines(cues: Sequence[SrtCue], lines_by_source: Mapping[str, list[WitnessLine]]
                      ) -> dict[str, AsrAttachment]:
    """MOSS / MAI lines ({abs_start_s, end_s, text}): each line gets `owners` and `parts` ({cue id: that cue's slice})
    from the same rule as jianying (segment_slices).  Returns the attachment per source (for its notes and PROCESS
    counts)."""
    out: dict[str, AsrAttachment] = {}
    for source, lines in lines_by_source.items():
        if not source.startswith("asr:"):
            continue
        segs = [cast(AsrSegment, {"start_s": ln["abs_start_s"], "end_s": ln.get("end_s", ln["abs_start_s"]),
                                  "text": ln["text"],
                                  **({"speaker": str(ln["speaker"])} if ln.get("speaker") else {})})
                                  for ln in lines]
        att = attach_asr(cues, segs, source=source)
        for ln, slices in zip(lines, att.slices, strict=True):
            ln["parts"] = dict(slices)
            ln["owners"] = list(slices)
        out[source] = att
    return out


def window_line_owner(window: AudioWindow, abs_start_s: float) -> int | None:
    """The cue a listening line belongs to: its window's cue, when the line's time lies within the window's audio."""
    a0, a1 = float(window["audio"][0]), float(window["audio"][1])
    if a0 - LINE_WINDOW_TOL_S <= abs_start_s <= a1 + LINE_WINDOW_TOL_S:
        return int(window["cue_ids"][0])
    return None


# ----- danmaku ------------------------------------------------------------------------------------------------------
def _quality(text: str) -> float:
    """Text quality of a comment: distinct meaningful characters (a row of ? or one repeated character is low)."""
    norm = normalize_chat_text(text)
    return min(1.0, len(set(norm)) / 4.0)


def danmaku_for_cue(cue: TimedText, rows: Sequence[Danmaku], readings: Sequence[str],
                    *, limit: int = DANMAKU_PER_CUE) -> list[RankedDanmaku]:
    """Comments sent in [start - 90, end + 30], ranked by relevance to the cue's readings and text quality (never by
    time distance), one per distinct text, cut by count.  Each item: text, dt_s (send time minus cue start), match."""
    a, b = float(cue["start_s"]), float(cue["end_s"])
    texts = [t for t in readings if normalize_chat_text(t)]
    best: dict[str, RankedDanmaku] = {}
    for row in rows:
        t = float(row["t_s"])
        before = event_lookback_s(row["type"]) if "type" in row else DANMAKU_BEFORE_S
        if not (a - before <= t <= b + DANMAKU_AFTER_S):
            continue
        view = evidence_view(row)
        key = normalize_chat_text(view)
        if not key:
            continue
        match = max((_match_metrics(written, r)[0] for written in evidence_texts(row) for r in texts), default=0.0)
        item: RankedDanmaku = {"text": view, "dt_s": round(t - a, 1), "match": round(match, 3),
                               "quality": round(_quality(view), 2)}
        if key not in best or (item["match"], item["quality"]) > (best[key]["match"], best[key]["quality"]):
            best[key] = item
    ranked = sorted(best.values(), key=lambda x: (-x["match"], -x["quality"], -len(normalize_chat_text(x["text"]))))
    return ranked[:limit]


def danmaku_view(items: Sequence[TimedComment]) -> list[str]:
    """The strings shown to CPA and on the listening page: the text and when it was sent relative to the sentence."""
    out = []
    for it in items:
        dt = float(it["dt_s"])
        when = f"句前 {-dt:.0f} 秒" if dt < 0 else f"句起点后 {dt:.0f} 秒"
        out.append(f"{it['text']}（{when}发送）")
    return out


def read_aloud_hit(cue: TimedText, rows: Sequence[Danmaku]) -> ReadAloudHit | None:
    """The comment this cue most likely reads aloud, when the cue's text is close to it but not the same: sent in
    [start - 90, end - 2 s], at least 4 characters, score and coverage above the floor.  None otherwise."""
    a, b = float(cue["start_s"]), float(cue["end_s"])
    cue_norm = normalize_chat_text(str(cue.get("text", "")))
    if not cue_norm:
        return None
    best: ReadAloudHit | None = None
    for row in rows:
        if row.get("type") in {"gift", "guard"}:
            continue  # gift/guard labels and usernames are not read-aloud message bodies
        t = float(row["t_s"])
        before = event_lookback_s(row["type"]) if "type" in row else DANMAKU_BEFORE_S
        if not (a - before <= t <= b - READ_ALOUD_MIN_DELAY_MS / 1000):
            continue
        norm = normalize_chat_text(row["text"])
        if len(norm) < READ_ALOUD_MIN_CHARS:
            continue
        score, _ratio, coverage, precision, _common = _match_metrics(row["text"], str(cue["text"]))
        if score < READ_ALOUD_SCORE or coverage < READ_ALOUD_COVERAGE:
            continue
        cand: ReadAloudHit = {"text": str(row["text"]), "dt_s": round(t - a, 1), "score": round(score, 3),
                              "coverage": round(coverage, 3), "precision": round(precision, 3), "exact": norm in cue_norm}
        if best is None or (cand["score"], cand["precision"]) > (best["score"], best["precision"]):
            best = cand
    if best is None or best["exact"]:
        return None     # nothing close, or the cue already reads the comment exactly
    return best
