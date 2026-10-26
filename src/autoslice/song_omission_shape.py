"""The one definition of a complete live song's omission shape.

维护者 (SONG-01 clarification): "理论上主播是会把歌唱完的，但是偶尔主播是
不会唱完，比如说他可能中间会缺一句缺两句。那么这种情况我希望我们的歌切检查程序
也能适应这种情况。"  SONG-08: the completeness gate must not be over-conservative.
SONG-06: the live arrangement follows what was actually performed.

A *hole* is a maximal run of unheard canonical lyric rows (non-lyric LRC
metadata is removed before this definition runs).  Every hole must be exactly
one kind:

* ``REPEATED_SECTION`` — at least ``MIN_REPEATED_SECTION_ROWS`` rows whose
  texts are sung contiguously elsewhere (may also end the LRC).
* ``SPOKEN_INTERLUDE`` — an internal hole (heard rows on both sides) whose
  live window carries host speech in the independent source transcript.
* ``SMALL_SKIP`` — any other internal hole of at most ``MAX_SMALL_SKIP_ROWS``
  rows.  It needs no reason.

Precedence is fixed: the most specific evidence first (repeated, spoken), the
reason-free small skip last.  Budgets count holes
by size, not by kind: internal holes of at most ``MAX_SMALL_SKIP_ROWS`` rows
are *small* (at most ``MAX_SMALL_SKIPS`` per song); every other hole is a
*section* (at most ``MAX_LIVE_ARRANGEMENT_OMITTED_BLOCKS``).  All holes
together stay within ``MAX_LIVE_ARRANGEMENT_OMITTED_ROWS`` /
``MAX_LIVE_ARRANGEMENT_OMITTED_RATIO``.  Head/middle/tail coverage, observed
opening/ending, the post-song transition and the instrumental-gap proof are
checked by the caller and are not part of this definition.

A hole must be really unsung: when the bound source ASR sings a hole row
inside the hole's live window, the observation missed performed lyrics and
is rejected, so another LRC variant can be selected (SUB-26: subtitles may
carry more, never drop her words).

Whatever the kind, unheard lyric rows never get a subtitle, and host speech
found in a hole's live window becomes spoken subtitles through
``song_spoken_interlude``.  The provider's classification label is an
observation only.  The completeness generator, the provider prompt, the cold
report readers and the subtitle consumer all use this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_common import (
    COMPLETE_LIVE_WITH_SPOKEN_INTERLUDE,
    LIVE_ARRANGEMENT_CLASSIFICATIONS,
    MAX_LIVE_ARRANGEMENT_OMITTED_BLOCKS,
    MAX_LIVE_ARRANGEMENT_OMITTED_RATIO,
    MAX_LIVE_ARRANGEMENT_OMITTED_ROWS,
    MIN_LIVE_ARRANGEMENT_DURATION_MS,
    MIN_LIVE_ARRANGEMENT_HEARD_RATIO,
    MIN_LIVE_ARRANGEMENT_HEARD_ROWS,
    normalize_lyric_text,
)
from src.autoslice.song_alignment import ANCHOR_MATCH_THRESHOLD, lyric_cue_similarity
from src.autoslice.song_spoken_interlude import source_spoken_interlude

SMALL_SKIP = "SMALL_SKIP"
REPEATED_SECTION = "REPEATED_SECTION"
SPOKEN_INTERLUDE = "SPOKEN_INTERLUDE"
HOLE_KINDS = (REPEATED_SECTION, SMALL_SKIP, SPOKEN_INTERLUDE)

MAX_SMALL_SKIP_ROWS = 2
MAX_SMALL_SKIPS = 3
MIN_REPEATED_SECTION_ROWS = 2

FULL_STUDIO_SEQUENCE = "FULL_STUDIO_SEQUENCE"
COMPLETE_LIVE_ARRANGEMENT = "COMPLETE_LIVE_ARRANGEMENT"
INCOMPLETE_OR_FRAGMENT = "INCOMPLETE_OR_FRAGMENT"

# Keys present only on reports derived with the bound source transcript.
SPEECH_WINDOWS_KEY = "speech_windows_ms"
SPEECH_CUES_KEY = "speech_cues"


def _range_label(kind: str, *, trailing: bool) -> str:
    # Historic labels are part of stored reports and must stay byte-stable.
    return f"{'TRAILING' if trailing else 'BOUNDED'}_{kind}"


@dataclass(frozen=True)
class OmissionShape:
    classification: str
    ranges: list[dict[str, object]]
    speech_cues: list[dict[str, object]]


def derive_omission_shape(
    *,
    lyric_rows: Sequence[Mapping[str, object]],
    omitted_indices: Sequence[int],
    performed: Sequence[tuple[int, int, int]],
    raw_index: Sequence[int],
    asr_anchor_cues: Sequence[SourceCue] | None,
    trailing_window_end_ms: int | None = None,
) -> OmissionShape:
    """Classify every hole and enforce the whole-song budgets.

    ``lyric_rows`` are the canonical lyric observations (metadata removed and
    renumbered); ``performed`` lists ``(lyric_index, start_ms, end_ms)`` in
    live order, repeats included, with source-cue bounds where anchored;
    ``raw_index`` maps a lyric index back to the original LRC row.  With
    ``asr_anchor_cues=None`` (legacy reports without a bound transcript) no
    speech or sung-lyric evidence is looked up and the output keeps the
    legacy shape exactly.  ``trailing_window_end_ms`` (the post-song
    transition) bounds the live window of a hole at the LRC end.
    """

    count = len(lyric_rows)
    holes: list[tuple[int, int]] = []
    for index in sorted(omitted_indices):
        if not holes or index != holes[-1][1] + 1:
            holes.append((index, index))
        else:
            holes[-1] = (holes[-1][0], index)

    def internal(hole: tuple[int, int]) -> bool:
        return 0 < hole[0] <= hole[1] < count - 1

    def small(hole: tuple[int, int]) -> bool:
        return internal(hole) and hole[1] - hole[0] + 1 <= MAX_SMALL_SKIP_ROWS

    if sum(not small(hole) for hole in holes) > MAX_LIVE_ARRANGEMENT_OMITTED_BLOCKS:
        raise ValueError("live arrangement has multiple omitted canonical blocks")
    if sum(small(hole) for hole in holes) > MAX_SMALL_SKIPS:
        raise ValueError(
            f"live arrangement has more than {MAX_SMALL_SKIPS} small omitted canonical holes"
        )
    omitted_count = len(omitted_indices)
    if (
        omitted_count > MAX_LIVE_ARRANGEMENT_OMITTED_ROWS
        or omitted_count / max(1, count) > MAX_LIVE_ARRANGEMENT_OMITTED_RATIO
    ):
        raise ValueError(
            "live arrangement omitted canonical block is too large: "
            f"{omitted_count}/{count} rows"
        )

    performed_texts = [normalize_lyric_text(str(lyric_rows[index].get("text") or ""))
                       for index, _start, _end in performed]
    ranges: list[dict[str, object]] = []
    all_speech: list[dict[str, object]] = []
    spoken = False
    for hole in holes:
        start_index, end_index = hole
        texts = [normalize_lyric_text(str(lyric_rows[i].get("text") or ""))
                 for i in range(start_index, end_index + 1)]
        repeated = len(texts) >= MIN_REPEATED_SECTION_ROWS and all(texts) and any(
            performed_texts[at: at + len(texts)] == texts
            for at in range(len(performed_texts) - len(texts) + 1)
        )
        windows: list[list[int]] = []
        speech: list[dict[str, object]] = []
        if asr_anchor_cues is not None and internal(hole):
            # The live point where this hole was skipped: a performed row
            # immediately followed (in live order) by the row after the hole.
            for (left, _ls, left_end), (right, right_start, _re) in zip(performed, performed[1:]):
                if left == start_index - 1 and right == end_index + 1 and left_end <= right_start:
                    windows.append([left_end, right_start])
            seen: set[str] = set()
            for window_start, window_end in windows:
                for cue in source_spoken_interlude(cues=asr_anchor_cues, start_ms=window_start,
                                                   end_ms=window_end, observations=lyric_rows):
                    if cue["cue_id"] not in seen:
                        seen.add(str(cue["cue_id"]))
                        speech.append(cue)
        if repeated:
            kind = REPEATED_SECTION
        elif internal(hole) and speech:
            kind = SPOKEN_INTERLUDE
            spoken = True
        elif small(hole):
            kind = SMALL_SKIP
        else:
            raise ValueError(
                f"canonical LRC line {raw_index[start_index]} was not affirmatively heard; "
                "live arrangement omitted block is not a repeated canonical section, "
                "a bounded small skip or a source-proven spoken interlude"
            )
        if asr_anchor_cues is not None:
            # A hole is a lyric that was really not sung. Independent source
            # ASR singing a hole row inside its live window means the
            # observation missed performed lyrics (SUB-26: never drop her
            # words), so this observation cannot be the selected variant.
            evidence_windows = list(windows)
            if end_index == count - 1 and trailing_window_end_ms is not None:
                evidence_windows += [[end, trailing_window_end_ms] for index, _start, end in performed[-1:]
                                     if index == start_index - 1 and end <= trailing_window_end_ms]
            _reject_sung_hole(texts, start_index, raw_index, evidence_windows, asr_anchor_cues)
        entry: dict[str, object] = {
            "start_lrc_index": start_index,
            "end_lrc_index": end_index,
            "line_count": end_index - start_index + 1,
            "kind": _range_label(kind, trailing=end_index == count - 1),
        }
        if asr_anchor_cues is not None and internal(hole):
            entry[SPEECH_WINDOWS_KEY] = windows
            entry[SPEECH_CUES_KEY] = speech
        all_speech.extend(speech)
        ranges.append(entry)

    if not holes:
        classification = FULL_STUDIO_SEQUENCE
    elif spoken:
        classification = COMPLETE_LIVE_WITH_SPOKEN_INTERLUDE
    else:
        classification = COMPLETE_LIVE_ARRANGEMENT
    return OmissionShape(classification, ranges, all_speech)


def _reject_sung_hole(texts, start_index, raw_index, windows, cues) -> None:
    for window_start, window_end in windows:
        for cue in cues:
            if not window_start <= cue.source_start_ms < cue.source_end_ms <= window_end:
                continue
            cue_text = normalize_lyric_text(cue.text)
            for offset, text in enumerate(texts):
                if text and lyric_cue_similarity(text, cue_text) >= ANCHOR_MATCH_THRESHOLD:
                    raise ValueError(
                        f"canonical LRC line {raw_index[start_index + offset]} is in an omitted block "
                        f"but source ASR cue {cue.cue_id} sings it; an observation that misses "
                        "performed lyrics cannot be the selected variant"
                    )


def stored_shape_uses_source_cues(stored_completeness: object) -> bool:
    """Whether a stored report was derived with the bound source transcript.

    Cold readers must re-derive in the same mode to compare by equality:
    legacy reports carry no speech keys and stay transcript-free.
    """
    if not isinstance(stored_completeness, Mapping):
        return False
    ranges = stored_completeness.get("omitted_ranges")
    return isinstance(ranges, list) and any(
        isinstance(entry, Mapping) and SPEECH_CUES_KEY in entry for entry in ranges
    )


def stored_shape_speech_cues(stored_completeness: object) -> list[Mapping[str, object]]:
    """Spoken subtitle rows of every hole, in source order."""
    if not isinstance(stored_completeness, Mapping):
        return []
    ranges = stored_completeness.get("omitted_ranges")
    rows = [cue for entry in (ranges if isinstance(ranges, list) else [])
            if isinstance(entry, Mapping)
            for cue in (entry.get(SPEECH_CUES_KEY) or [])]
    return sorted(rows, key=lambda row: (int(row["source_start_ms"]), int(row["source_end_ms"])))


def live_arrangement_prompt_contract() -> str:
    """Provider guidance rendered from the definition above (rule 7)."""
    return f"""Allowed classification labels: {', '.join(sorted(LIVE_ARRANGEMENT_CLASSIFICATIONS))}.
   Report each row's `heard` honestly; code recomputes completeness from the
   rows and the independent source transcript, and records your label only as
   an observation. Code requires at least {MIN_LIVE_ARRANGEMENT_HEARD_ROWS} heard lyric rows,
   at least {MIN_LIVE_ARRANGEMENT_HEARD_RATIO:.0%} canonical coverage and
   at least {MIN_LIVE_ARRANGEMENT_DURATION_MS // 1000} seconds of performance.
   Opening and ending must both be observed. A run of unheard lyric rows is a
   hole of exactly one kind: {REPEATED_SECTION} (at least {MIN_REPEATED_SECTION_ROWS} rows sung contiguously
   elsewhere), {SMALL_SKIP} (at most {MAX_SMALL_SKIP_ROWS} rows between heard rows; at most
   {MAX_SMALL_SKIPS} such holes) or {SPOKEN_INTERLUDE} (one internal block where the host
   talks instead, proven only from the source transcript). At most
   {MAX_LIVE_ARRANGEMENT_OMITTED_BLOCKS} hole may be larger than {MAX_SMALL_SKIP_ROWS} rows or touch the LRC end; all holes
   together stay within {MAX_LIVE_ARRANGEMENT_OMITTED_ROWS} rows and {MAX_LIVE_ARRANGEMENT_OMITTED_RATIO:.0%} of lyrics.
   Use {FULL_STUDIO_SEQUENCE} when every lyric row is heard, {COMPLETE_LIVE_WITH_SPOKEN_INTERLUDE}
   when a spoken block replaces lyrics, {COMPLETE_LIVE_ARRANGEMENT} for other complete
   holes, and otherwise {INCOMPLETE_OR_FRAGMENT}.
   Do not mark omitted lyrics heard to obtain a label. A whole-row
   performer/voice/production label is not a lyric; preserve its index and
   honestly report it unheard. A label never authorizes acceptance or
   overrides missing opening/ending, playback or singer uncertainty."""
