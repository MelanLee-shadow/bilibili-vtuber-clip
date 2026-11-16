"""Cutting what does not touch the main line out of the middle of a finished clip: the pure part (no network, no media).

Two kinds of run of consecutive final cues are cut (prompts/trim.md): a thanks nobody picks up (for a gift, an SC, or a
name alone; kind "thanks"), and a stretch on a topic unrelated to the clip's main line that she leaves and comes back from
(a lottery in the middle of a talk, sorting out the stream; kind "topic"; owner 中间无关的话题…最好还是剪掉),
and a long stretch with no subtitle at all because step 3 found no speech in it, mostly her singing (kind "nospeech";
`silent_gaps` puts one row for it among the cues, so the model sees it and the same span machinery can cut it).
Both are found by two independent model answers; a cue is cut only when both answers propose it, and only when `guard`
lets the run through.  The
kept intervals of the clip clock, the final cues moved onto the shortened clock and the source-recording pieces to cut
are plain computations on the cue list.

`cues` are the final cues, `{'id', 'start_s', 'end_s', 'text'}` in time order on the cut-clip clock.  A cue number is
its 1-based position in the list (not its `id`); a span `(a, b)` is cue numbers a..b, both included.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, TypedDict, cast

from . import channel
from . import text_overlap
from . import compilation, doubt_policy, subtitle_track
from .recorder_events import Danmaku
from .srt_text import SrtCue, TimedText, TimeSpan
from .step_io import PROMPTS

if TYPE_CHECKING:
    from .step_import import ImportedDoubt

Span = tuple[int, int]


class NumberedCue(subtitle_track.TrackCue):
    """A cue with its `id`, as `moved` renumbers it."""

    id: int


class Veto(TypedDict):
    span: Span
    veto: str
    detail: str


class OriginalDoubt(doubt_policy.FinalDoubt, total=False):
    """A row of the finished clip's subs/doubts.json: step 3's fields plus the speaker fields step 4 adds."""

    alternatives: list[str]
    speaker: str | None
    speaker_confidence: str | None

# A stretch called an unrelated topic is "插进来的一段" only while it is short against the clip: more than this share of
# the clip is not an insert but a wrong main line or boundary (measured a 礼墨 clip's mid-talk lottery is 17%;
# a run of danmaku-requested acts in a 李豆沙 clip, which belongs to the clip, was proposed at 60%).
TOPIC_MAX_SHARE = 1 / 3
_ROUND_TOLERANCE_S = 0.0005   # windows are rounded to the millisecond; a cue edge may sit that far outside
_SHIFT_TOLERANCE_S = 0.05     # a Y cut snapped into silence may leave a cue this far outside the cut
# A stretch between two subtitles at least this long, holding sound step 3 removed as not speech (singing, humming), is
# shown to the trim model as one row (a talk clip held 160 s of her singing with no subtitle; the model saw
# only the two subtitles around it and could not cut it, and step 8 found no timing anchor in the middle of the clip).
GAP_MIN_S = 20.0
GAP = "gap"                      # the key that marks such a row
_EVENT_KIND = {"sc": "SC", "gift": "礼物", "guard": "上舰", "礼物": "礼物", "上舰": "上舰"}   # the offline trial's names


# ----- the prompt and the model answer -----------------------------------------------------------------
def prompt(hook: str, events_text: str, cues: Sequence[TimedText], *, run: int = 1) -> str:
    """prompts/trim.md with the clip's selection reason, SC / guard / gift records and numbered cues filled in, in the
    exact input shape the offline trial measured (seconds with one decimal, `（无记录）` for no records).  `run`: the
    second independent ask carries a closing `（第 2 遍）` line, as in the trial, so it is a distinct request."""
    lines = [f"{number} {float(c['start_s']):.1f}-{float(c['end_s']):.1f} {' '.join(str(c['text']).split())}"
             for number, c in enumerate(cues, 1)]
    template = channel.fill((PROMPTS / "trim.md").read_text(encoding="utf-8"))
    text = (template.replace("{hook}", hook).replace("{events}", events_text or "（无记录）")
            .replace("{cues}", "\n".join(lines)))
    return text if run == 1 else text.rstrip("\n") + f"\n（第 {run} 遍）\n"


KINDS = ("thanks", "topic", "nospeech")


def silent_gaps(cues: Sequence[subtitle_track.TrackCue], doubts: Sequence[OriginalDoubt], clip_start_ms: int
                ) -> list[subtitle_track.TrackCue]:
    """One row per stretch of at least GAP_MIN_S between two consecutive final cues in which step 3 removed what the
    recognisers wrote as not speech (`dropped` doubts, timed by abs_start_ms / abs_end_ms).  The row lies inside the
    stretch, far enough from both cues that a cut of it keeps the cues' own margins (`keep_windows`); its text says what
    it is, for the model.  Marked with GAP: true."""
    removed = [((int(d["abs_start_ms"]) - clip_start_ms) / 1000, (int(d["abs_end_ms"]) - clip_start_ms) / 1000,
                " ".join(str(d.get("current") or "").split()))
               for d in doubts if d.get("dropped") and type(d.get("abs_start_ms")) is int and type(d.get("abs_end_ms")) is int]
    rows: list[subtitle_track.TrackCue] = []
    for before, after in zip(cues, cues[1:]):
        low, high = float(before["end_s"]), float(after["start_s"])
        inside = [text for start, end, text in removed if low <= (start + end) / 2 <= high]
        if high - low < GAP_MIN_S or not inside:
            continue
        heard = "／".join(t for t in inside if t)[:40]
        rows.append(cast(subtitle_track.TrackCue, {
            "start_s": round(low + 2 * compilation.TAIL_MARGIN_S, 3), "end_s": round(high - 2 * compilation.HEAD_MARGIN_S, 3),
            "text": f"（这里 {high - low:.0f} 秒没有字幕：字幕那一步听过，判定这段声音不是讲话（多是唱歌、哼唱）而删去；"
                    f"删去前识别器写的是「{heard}」）", GAP: True}))
    return rows


def without_uncut_gaps(rows: Sequence[subtitle_track.TrackCue], spans: Sequence[Span]
                       ) -> tuple[list[subtitle_track.TrackCue], list[Span]]:
    """`rows` without the GAP rows no span cuts, and `spans` renumbered onto what is left: a gap row that stays is not a
    subtitle and must not reach the clip that is built."""
    cut = {n for a, b in spans for n in range(a, b + 1)}
    keep = [n for n, row in enumerate(rows, 1) if n in cut or not row.get(GAP)]
    new = {old: i for i, old in enumerate(keep, 1)}
    return [rows[n - 1] for n in keep], [(new[a], new[b]) for a, b in spans]


def parse_answer(answer: object, n_cues: int) -> list[Span]:
    """The spans of `{"remove": [{"start_cue": int, "end_cue": int, "kind": ..., ...}, ...]}`, by start; ValueError on
    anything else."""
    return sorted(parse_kinds(answer, n_cues))


def parse_kinds(answer: object, n_cues: int) -> dict[Span, str]:
    """Each span of the answer with its kind ("thanks" when the answer gives none, as before topics were asked about)."""
    if not isinstance(answer, dict) or not isinstance(answer.get("remove"), list):
        raise ValueError("answer must be an object whose 'remove' is a list")
    kinds: dict[Span, str] = {}
    for index, item in enumerate(answer["remove"]):
        if not isinstance(item, dict):
            raise ValueError(f"remove[{index}] is not an object")
        start, end = item.get("start_cue"), item.get("end_cue")
        if type(start) is not int or type(end) is not int:
            raise ValueError(f"remove[{index}]: start_cue and end_cue must be integers, got {start!r} and {end!r}")
        if not 1 <= start <= end <= n_cues:
            raise ValueError(f"remove[{index}]: need 1 <= start_cue <= end_cue <= {n_cues}, got {start}..{end}")
        kind = item.get("kind", "thanks")
        if kind not in KINDS:
            raise ValueError(f"remove[{index}]: kind must be one of {list(KINDS)}, got {kind!r}")
        kinds[(start, end)] = kind
    spans = sorted(kinds)
    for (a, b), (c, d) in zip(spans, spans[1:]):
        if c <= b:
            raise ValueError(f"remove spans {a}..{b} and {c}..{d} overlap")
    return kinds


def topic_cues(*answers: dict[Span, str]) -> frozenset[int]:
    """The cues every answer calls part of an unrelated topic (not a thanks)."""
    sets = [{n for (a, b), kind in kinds.items() if kind == "topic" for n in range(a, b + 1)} for kinds in answers]
    return frozenset(set.intersection(*sets)) if sets else frozenset()


def _runs(numbers: Sequence[int]) -> list[Span]:
    spans: list[Span] = []
    for n in sorted(numbers):
        if spans and n == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], n)
        else:
            spans.append((n, n))
    return spans


def _table(cues: Sequence[subtitle_track.TrackCue], marks: Sequence[subtitle_track.TrackCue]
           ) -> list[tuple[subtitle_track.TrackCue, int | None]]:
    """The final cues and the owner's marks in one table by time, each row with the number of the final cue it is (None: a mark)."""
    rows: list[tuple[subtitle_track.TrackCue, int | None]] = [(c, n) for n, c in enumerate(cues, 1)] + [(m, None) for m in marks]
    return sorted(rows, key=lambda r: (float(r[0]["start_s"]), float(r[0]["end_s"])))


def with_marks(cues: Sequence[subtitle_track.TrackCue], marks: Sequence[subtitle_track.TrackCue]
               ) -> tuple[list[subtitle_track.TrackCue], list[Span]]:
    """The final cues with the channel owner's 删 marks (his sentences that are no longer in the subtitles, same clock)
    put among them by time, and the runs of consecutive marks as spans of that table.  `moved` then leaves the marks out
    and shifts the rest, as for a model's spans."""
    rows = _table(cues, marks)
    return [c for c, _ in rows], _runs([n for n, (_, own) in enumerate(rows, 1) if own is None])


def kept_origin(cues: Sequence[subtitle_track.TrackCue], marks: Sequence[subtitle_track.TrackCue], spans: Sequence[Span]
                ) -> list[int | None]:
    """For each cue `moved` leaves of `with_marks(cues, marks)`'s table after `spans`, in its order: the number of the final cue
    in `cues` it is (None: a mark that was not cut).  Whatever is known of a final cue is thereby known of the cue made from it."""
    rows = _table(cues, marks)
    dropped = {n for a, b in _check_spans(spans, len(rows)) for n in range(a, b + 1)}
    return [own for n, (_, own) in enumerate(rows, 1) if n not in dropped]


def agreed(first: list[Span], second: list[Span]) -> list[Span]:
    """The cues both independent answers propose to cut, as runs of consecutive cue numbers."""
    def cues_of(spans: list[Span]) -> set[int]:
        return {n for a, b in spans for n in range(a, b + 1)}

    return _runs(sorted(cues_of(first) & cues_of(second)))


# ----- the insurances ----------------------------------------------------------------------------------
def guard(spans: Sequence[Span], cues: Sequence[TimedText], *,
          thanks_words: Sequence[str] | None, clip_len_s: float, min_keep_s: float,
          topic: frozenset[int] = frozenset(), gaps: frozenset[int] = frozenset()) -> tuple[list[Span], list[Veto]]:
    """(spans that pass, vetoed spans as `{'span', 'veto', 'detail'}`).  Per span, the first rule that hits vetoes it:
    EDGE (holds the first or last cue: head and tail belong to the boundary step), NO_THANKS (no cue of the span has a
    thanks word; not asked of a span whose every cue both answers call an unrelated topic, `topic`, or is a `silent_gaps`
    row, `gaps`).  Then TOPIC_TOO_LONG: the topic spans that passed are vetoed when together they last more than
    TOPIC_MAX_SHARE of the clip (a span holding a gap row is not an inserted topic: no speech is not a wrong main line).  Then TOO_SHORT: if the passed spans leave less than `min_keep_s` of clip, all of them are vetoed.
    Whether a thanks is picked up afterwards (an SC read out, an answer, a joke) is the model's judgment in
    prompts/trim.md, asked twice; no code rule second-guesses it.  `thanks_words=None`: the spans are the channel owner's
    own cut marks (not a model's guess), so NO_THANKS does not apply; EDGE and TOO_SHORT cannot be done mechanically
    and still veto."""
    words = [text_overlap.key(w) for w in thanks_words or ()]
    if thanks_words is not None and not all(words):
        raise ValueError(f"thanks_words must not hold an empty word: {list(thanks_words)!r}")
    texts = [text_overlap.key(str(c["text"])) for c in cues]
    passed: list[Span] = []
    vetoed: list[Veto] = []
    for a, b in spans:
        inside = texts[a - 1:b]
        if a == 1 or b == len(cues):
            vetoed.append({"span": (a, b), "veto": "EDGE", "detail": f"span {a}..{b} holds the first or last of {len(cues)} cues"})
        elif (thanks_words is not None and not set(range(a, b + 1)) <= topic | gaps
              and not any(w in t for t in inside for w in words)):
            vetoed.append({"span": (a, b), "veto": "NO_THANKS", "detail": f"no cue of {a}..{b} holds {list(thanks_words)!r}"})
        else:
            passed.append((a, b))
    topical = [span for span in passed if set(range(span[0], span[1] + 1)) <= topic and not set(range(span[0], span[1] + 1)) & gaps]
    topic_s = sum(float(cues[b - 1]["end_s"]) - float(cues[a - 1]["start_s"]) for a, b in topical)
    if topical and topic_s > TOPIC_MAX_SHARE * clip_len_s:
        detail = f"{topic_s:.1f}s of unrelated topic is more than {TOPIC_MAX_SHARE:.0%} of the {clip_len_s:.1f}s clip"
        vetoed += [{"span": span, "veto": "TOPIC_TOO_LONG", "detail": detail} for span in topical]
        passed = [span for span in passed if span not in topical]
    if passed:
        kept = sum(end - start for start, end in keep_windows(cues, passed, clip_len_s))
        if kept < min_keep_s:
            detail = f"{kept:.3f}s of the clip would be kept, less than {min_keep_s}s"
            vetoed += [{"span": span, "veto": "TOO_SHORT", "detail": detail} for span in passed]
            passed = []
    return passed, vetoed


# ----- the clock ---------------------------------------------------------------------------------------
def _check_spans(spans: Sequence[Span], n_cues: int) -> list[Span]:
    ordered = sorted(spans)
    for a, b in ordered:
        if not 1 <= a <= b <= n_cues:
            raise ValueError(f"span {a}..{b} is outside cues 1..{n_cues}")
    for (a, b), (c, d) in zip(ordered, ordered[1:]):
        if c <= b + 1:
            raise ValueError(f"spans {a}..{b} and {c}..{d} touch or overlap")
    return ordered


def keep_windows(cues: Sequence[TimeSpan], spans: Sequence[Span], clip_len_s: float) -> list[tuple[float, float]]:
    """The intervals of the clip clock kept after the spans are cut out: from 0 to the first hole, between holes, and
    from the last hole to `clip_len_s`.  A hole's two cut points follow `compilation.cut_window_ms`: the kept speech plus
    TAIL_MARGIN_S (before the hole) or minus HEAD_MARGIN_S (after it), but never past the middle of the gap to the hole's
    nearest cue.  Rounded to the millisecond."""
    ordered = _check_spans(spans, len(cues))
    for a, b in ordered:
        if a == 1 or b == len(cues):
            raise ValueError(f"span {a}..{b} holds the first or last cue: head and tail are not cut here")
    edges = [0.0]
    for a, b in ordered:
        before, first, last, after = cues[a - 2], cues[a - 1], cues[b - 1], cues[b]
        kept_end, kept_start = float(before["end_s"]), float(after["start_s"])
        end = min(kept_end + compilation.TAIL_MARGIN_S, (kept_end + float(first["start_s"])) / 2)
        start = max(kept_start - compilation.HEAD_MARGIN_S, (float(last["end_s"]) + kept_start) / 2)
        edges += [round(max(end, kept_end), 3), round(min(start, kept_start), 3)]
    edges.append(round(float(clip_len_s), 3))
    windows = list(zip(edges[::2], edges[1::2]))
    for start, end in windows:
        if start >= end:
            raise ValueError(f"kept interval {start}..{end} is empty or reversed")
    for (_, end), (start, _) in zip(windows, windows[1:]):
        if start < end:
            raise ValueError(f"kept intervals overlap: one ends at {end}, the next starts at {start}")
    return windows


def moved(cues: Sequence[subtitle_track.TrackCue], spans: Sequence[Span],
          windows: Sequence[tuple[float, float]]) -> list[NumberedCue]:
    """The cues left after the spans, on the shortened clock: each cue lies whole inside one kept interval and moves
    earlier by the length cut out before that interval.  Other keys are kept; `id` is renumbered from 1."""
    dropped = {n for a, b in _check_spans(spans, len(cues)) for n in range(a, b + 1)}
    before = [0.0]                      # length of the kept intervals before each one
    for start, end in windows:
        before.append(before[-1] + end - start)
    out: list[NumberedCue] = []
    for number, cue in enumerate(cues, 1):
        if number in dropped:
            continue
        start, end = float(cue["start_s"]), float(cue["end_s"])
        for (low, high), offset in zip(windows, before):
            if low - _ROUND_TOLERANCE_S <= start and end <= high + _ROUND_TOLERANCE_S:
                break
        else:
            raise ValueError(f"cue {number} ({start}..{end}) does not lie inside one kept interval of {list(windows)}")
        out.append({**cue, "id": len(out) + 1, "start_s": round(offset + start - low, 3),
                    "end_s": round(offset + end - low, 3)})
    return out


def source_pieces(windows: Sequence[tuple[float, float]], clip_start_ms: int) -> list[tuple[int, int]]:
    """The kept intervals as millisecond intervals of the source recording."""
    return [(clip_start_ms + round(start * 1000), clip_start_ms + round(end * 1000)) for start, end in windows]


# ----- the pieces the command wires together -----------------------------------------------------------
def events_text(records: Sequence[Danmaku]) -> str:
    """The prompt's record lines for the SC / guard / gift records of the clip (`t_s` on the clip clock), in the exact
    line shape the offline trial measured: `{t_s:.1f}s {kind} {sender} {content}` by time, where content is the text and,
    for a gift or guard, `{gift_name}×{count} {text}` (count 1 when absent).  Records of other types (ordinary
    comments) are not records here.  No record: an empty string (`prompt` then writes `（无记录）`)."""
    rows = sorted((r for r in records if str(r.get("type", "")).lower() in _EVENT_KIND), key=lambda r: r["t_s"])
    lines = []
    for r in rows:
        content = r.get("text") or ""
        if r.get("gift_name"):
            content = f"{r['gift_name']}×{r.get('count', 1)} {content}".strip()
        lines.append(f"{float(r['t_s']):.1f}s {_EVENT_KIND[str(r['type']).lower()]} {r.get('sender', '')} {content}".rstrip())
    return "\n".join(lines)


def _offsets(windows: Sequence[tuple[float, float]]) -> list[float]:
    before = [0.0]                      # length of the kept intervals before each one
    for start, end in windows:
        before.append(before[-1] + end - start)
    return before


def moved_bcut(bcut_cues: Sequence[SrtCue], windows: Sequence[tuple[float, float]]) -> list[SrtCue]:
    """The clip's original dictation on the shortened clock.  A cue that lies whole inside a kept interval moves like
    `moved` does; one whose middle is in no kept interval is dropped; one that crosses an edge of the interval holding its
    middle is cut to that interval, then moved.  Other keys are kept; `id` is renumbered from 1."""
    before = _offsets(windows)
    out: list[SrtCue] = []
    for cue in bcut_cues:
        start, end = float(cue["start_s"]), float(cue["end_s"])
        middle = (start + end) / 2
        for (low, high), offset in zip(windows, before):
            if low <= middle <= high:
                lo, hi = max(start, low), min(end, high)
                out.append({**cue, "id": len(out) + 1, "start_s": round(offset + lo - low, 3),
                            "end_s": round(offset + hi - low, 3)})
                break
    return out


def shifted(cues: Sequence[NumberedCue], offset_s: float, length_s: float) -> list[NumberedCue]:
    """The cues moved earlier by `offset_s` (the start of the cut taken from the joined clip, which step 2 may have
    snapped away from the joined clip's zero).  A cue that then starts before -0.05 or ends after `length_s` + 0.05
    cannot be shown inside the cut: ValueError naming it.  Within that tolerance the edge is clamped to [0, length_s]."""
    out: list[NumberedCue] = []
    for number, cue in enumerate(cues, 1):
        start, end = float(cue["start_s"]) - offset_s, float(cue["end_s"]) - offset_s
        if start < -_SHIFT_TOLERANCE_S or end > length_s + _SHIFT_TOLERANCE_S:
            raise ValueError(f"cue {number} ({start:.3f}..{end:.3f} after the shift of {offset_s}) lies outside the "
                             f"cut of {length_s} s")
        out.append({**cue, "start_s": round(max(0.0, start), 3), "end_s": round(min(float(length_s), end), 3)})
    return out


def _alternatives(doubt: OriginalDoubt) -> list[str]:
    if isinstance(doubt.get("alternatives"), list):
        return [str(a) for a in doubt["alternatives"]]
    shown = str(doubt.get("final_text") or doubt.get("current") or "")
    texts = [str(c["text"]) for c in doubt.get("candidates") or [] if str(c.get("text") or "")]
    return [t for t in dict.fromkeys(texts) if t != shown]


def carried_doubts(doubts: Sequence[OriginalDoubt], *, final_cues: Sequence[TimeSpan], bcut_cues: Sequence[SrtCue],
                   clip_start_ms: int, spans: Sequence[Span], windows: Sequence[tuple[float, float]],
                   new_cues: Sequence[TimeSpan], shift_s: float = 0.0) -> list[ImportedDoubt]:
    """The doubt table of the clip cut out of a finished one, as import-style rows (step_import._import_doubts shape).

    `doubts` are the original clip's rows; `final_cues` its final cues, `bcut_cues` its dictation (a step-3 row finds its
    time through its BCUT cue), `clip_start_ms` its cut start (absolute times), `spans` the cut cue numbers, `windows`
    the kept intervals, `new_cues` the new clip's final cues (the kept ones, moved: what `moved` then `shifted`
    gave) and `shift_s` what `shifted` took off.

    A doubt that sits on cut cues only, or on no cue and in no kept interval, is dropped.  Any other doubt becomes one
    row `trim-{id}` timed like the new cue(s) it sits on (on no cue: its own time, moved), with the original's current
    and final text, state, alternatives and basis.  Whether the row holds the package back is the original's
    (`doubt_policy.is_hold`): a holding row is `confidence` 低 with `affects_meaning` true, any other is 高 / false."""
    dropped = {n for a, b in _check_spans(spans, len(final_cues)) for n in range(a, b + 1)}
    new_index: dict[int, int] = {}                      # final cue number -> position in new_cues
    for number in range(1, len(final_cues) + 1):
        if number not in dropped:
            new_index[number] = len(new_index)
    if len(new_index) != len(new_cues):
        raise ValueError(f"{len(new_cues)} new cues for {len(new_index)} kept final cues")
    owned = subtitle_track.doubt_finals(cast("Sequence[subtitle_track.DoubtTime]", doubts), final_cues, bcut_cues, clip_start_ms)
    by_id = {int(c["id"]): c for c in bcut_cues if c.get("id") is not None}
    before = _offsets(windows)
    out: list[ImportedDoubt] = []
    for doubt in doubts:
        numbers = owned.get(str(doubt["id"]), [])
        if numbers:
            kept = [n for n in numbers if n not in dropped]
            if not kept:
                continue
            start, end = float(new_cues[new_index[kept[0]]]["start_s"]), float(new_cues[new_index[kept[-1]]]["end_s"])
        else:
            span = subtitle_track.doubt_span(cast("subtitle_track.DoubtTime", doubt), by_id, clip_start_ms)
            if span is None:
                raise ValueError(f"doubt {doubt['id']} has no time and sits on no final cue")
            middle = (span[0] + span[1]) / 2
            home = next(((low, high, offset) for (low, high), offset in zip(windows, before) if low <= middle <= high), None)
            if home is None:
                continue
            low, high, offset = home
            start = round(offset + max(span[0], low) - low - shift_s, 3)
            end = round(offset + min(span[1], high) - low - shift_s, 3)
        holds = doubt_policy.is_hold(doubt)
        row: ImportedDoubt = {"id": f"trim-{doubt['id']}", "cue_id": None, "start_s": start, "end_s": end,
                     "current": str(doubt.get("current") or ""), "state": doubt.get("state"),
                     "final_text": doubt.get("final_text"), "confidence": "低" if holds else "高",
                     "affects_meaning": bool(holds), "source": "trim",
                     "basis": str(doubt.get("basis") or doubt.get("reason") or "").strip() or f"片中剪除沿用 {doubt['id']}",
                     "alternatives": _alternatives(doubt), "speaker": doubt.get("speaker"),
                     "speaker_confidence": doubt.get("speaker_confidence"),
                     "meaning_basis": f"片中剪除：沿用原片疑点 {doubt['id']} 的放行判定（{'阻止封存' if holds else '不阻止封存'}）"}
        if doubt.get("dropped"):
            row["dropped"] = True
        if type(doubt.get("runs_same")) is int:     # the agreement that decided it (is_hold) goes with the wording
            row["runs_same"] = doubt["runs_same"]
        out.append(row)
    return out
