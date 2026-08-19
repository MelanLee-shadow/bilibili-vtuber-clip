"""Post-song boundary contract: song end vs first host speech, plus its witness.

(`song_210131_1210` /《心型病毒》联唱案).  The v5 audio-observation
schema carried two millisecond fields that downstream code uses for two
*different* purposes:

* ``live_arrangement.post_song_transition_ms`` becomes ``clip_end_ms`` in
  ``song_performance._finalize_audio_lrc_selection`` — it must be where the
  **song** ends.
* ``post_song_talk_start_ms`` becomes the host-vocal prover's spoken anchor and
  the recut ceiling (``recut_materialization`` ``post_song_anchor_start_ms``) —
  it must be where the streamer actually **speaks**.

The old contract forced ``transition_ms == post_song_talk_start_ms`` for
``HOST_TALK`` and forbade any talk millisecond for ``INSTRUMENTAL_OUTRO_END``.
In a medley / 3D-live setlist those two moments are minutes apart, so the
truth was *inexpressible* and the model had to pick a lie:

* say the gap between two songs is host talk (clip end right, anchor wrong), or
* say the song's transition is the real talk minutes later (anchor right, clip
  end swallows the whole next song).

Worse, the second, honest form is *structurally illegal*: the outro cap in
``song_instrumental_proof`` (``MAX_PROVEN_LIVE_INSTRUMENTAL_GAP_MS``) rejects a
transition more than 120s after the last lyric.  So the per-variant retry loop
in ``song_repair`` keeps sampling until it gets a wrong-but-short answer.
Measured on one candidate, eight gemini-3.6-flash runs over identical audio:
five located the true talk (484000/484300/484500/484500/485500 ms — fresh ASR
puts「欢迎回来」at 486040 ms) and would all have been rejected; three answered
inside the inter-song gap (245000/245200/258000 ms) and the 258000 one shipped.

Every one of those eight passed the old "cross check".  That check compares two
fields the prompt *orders* the model to make equal, both emitted in one
response — it can only catch a model that fails to copy a number, never a model
that mislocates the boundary.  That is why "两个一起错" was undetectable.

This module therefore does two things:

1. ``validate_post_song_transition_pair`` makes the medley truth expressible:
   ``INSTRUMENTAL_OUTRO_END`` may now carry a *later* ``post_song_talk_start_ms``
   (song end < first speech), with an explicit ordering contract.  ``HOST_TALK``
   keeps its equality — that is the single-song case where the two moments
   genuinely coincide.
2. ``witness_post_song_talk_start`` adds the independent third signal the old
   check never had: the fresh (non-AGY, BCUT) full-source ASR transcript that
   the lyric global-shift anchor already consumes.  A number copied between two
   fields of one LLM response cannot satisfy it.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from src.autoslice.host_vocal_proof import SESSION_HOST_ANCHOR_WINDOW_MS

# The host-vocal prover cuts exactly one ``SESSION_HOST_ANCHOR_WINDOW_MS`` window
# starting at ``post_song_talk_start_ms`` as its first spoken-anchor candidate.
# Reusing that constant is deliberate: the witness asks whether that window --
# or the one immediately preceding it -- contains any transcribed audio at all.
# No new threshold is introduced.
POST_SONG_TALK_WITNESS_WINDOW_MS = SESSION_HOST_ANCHOR_WINDOW_MS


class PostSongTalkWitnessError(ValueError):
    """Fail-closed rejection of an uncorroborated post-song talk claim.

    ``ValueError`` on purpose: ``song_repair`` already treats a validator
    ``Exception`` as "this LRC variant is rejected, try the next one", so an
    uncorroborated boundary costs one variant instead of aborting the
    candidate.
    """


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_post_song_transition_pair(
    *,
    transition_kind: object,
    transition_ms: object,
    post_song_talk_start_ms: object,
    source_duration_ms: int,
) -> None:
    """Validate the (song-end, first-host-speech) pair.

    ``HOST_TALK``
        The song ends directly into speech, so the two moments coincide and the
        equality still holds.  This is the ordinary single-song case.

    ``INSTRUMENTAL_OUTRO_END``
        The song ends into instrumental.  The first host speech in this window
        either never happens (``None``, unchanged) or happens at/after the song
        ended — the 联唱/setlist case the old contract could not express.  The
        clip end stays bound to ``transition_ms`` (still policed by the untouched
        outro cap), while the spoken anchor gets the later, real millisecond.

    Raises ``ValueError`` with the historical messages so existing negative
    coverage keeps binding.
    """

    if transition_kind == "HOST_TALK":
        if not _is_int(post_song_talk_start_ms) or transition_ms != post_song_talk_start_ms:
            raise ValueError(
                "live arrangement host-talk transition is not bound to post_song_talk_start_ms"
            )
        return
    if transition_kind == "INSTRUMENTAL_OUTRO_END":
        if not _is_int(transition_ms):
            raise ValueError("live arrangement instrumental-outro transition is invalid")
        if post_song_talk_start_ms is None:
            return
        if (
            not _is_int(post_song_talk_start_ms)
            or not transition_ms <= post_song_talk_start_ms <= source_duration_ms
        ):
            raise ValueError("live arrangement instrumental-outro transition is invalid")
        return
    raise ValueError("live arrangement has no proven post-song transition")


def witness_post_song_talk_start(
    *,
    post_song_talk_start_ms: object,
    asr_cues: Sequence[object],
    require_witness: bool,
) -> Mapping[str, object] | None:
    """Corroborate a claimed post-song talk onset against fresh ASR.

    The witness is a *falsifier*, not a verifier: ASR transcribes singing and
    speech alike, so it cannot prove the claimed millisecond is talk.  What it
    can prove — and what the medley failure needs — is that a claimed onset sits
    inside a transcription void, with no transcribed audio within one host-anchor
    window on either side.  That is exactly the signature of a millisecond picked
    out of the silence between two songs.

    Measured separation over every production report that carried a talk
    millisecond (14 reports, →): the nearest transcribed
    audio was at most 6100 ms away for all 13 correct claims (nine of them within
    1100 ms, three landing inside a cue) and 11940 ms away for the one known
    wrong claim.  ``POST_SONG_TALK_WITNESS_WINDOW_MS`` (8000) separates them
    without inventing a threshold.

    ``require_witness`` decides what happens when no transcript is available at
    all.  It is ``True`` exactly for the newly-legal decoupled shape
    (``INSTRUMENTAL_OUTRO_END`` plus a later talk millisecond): that shape did
    not exist before, so demanding evidence for it regresses nothing and keeps
    the relaxation evidence-bound.  It is ``False`` for the legacy shapes, whose
    availability requirements are deliberately left exactly as they were —
    ``_validated_audio_lrc_selection`` has a named contract for running without
    fresh-ASR cues (AGY-median offset fallback), and turning transcript
    availability into a new blanket delivery gate for the whole song lane is a
    policy call for 维护者, not a side effect of this fix.  Production is not
    affected either way: the shadow pipeline returns ``DRAFT_SRT_MISSING``
    before song repair whenever the full-source ASR is missing, so a real run
    always arrives here with cues.

    Returns a witness record, or ``None`` when there is nothing to witness
    (``post_song_talk_start_ms`` is ``None``, i.e. this window never returns to
    speech, or no transcript exists under ``require_witness=False``).  Raises
    ``PostSongTalkWitnessError`` otherwise.
    """

    if post_song_talk_start_ms is None:
        return None
    if not _is_int(post_song_talk_start_ms):
        raise PostSongTalkWitnessError(
            "post-song talk millisecond is not an integer; it cannot be witnessed"
        )
    talk_ms = int(post_song_talk_start_ms)
    intervals = _cue_intervals(asr_cues)
    if not intervals:
        if not require_witness:
            return None
        raise PostSongTalkWitnessError(
            "post-song talk claim has no fresh-ASR witness available "
            "(no independent full-source transcript cues were supplied)"
        )
    window_lo = talk_ms - POST_SONG_TALK_WITNESS_WINDOW_MS
    window_hi = talk_ms + POST_SONG_TALK_WITNESS_WINDOW_MS
    nearest_ms: int | None = None
    for start_ms, end_ms in intervals:
        if start_ms <= window_hi and end_ms >= window_lo:
            distance_ms = 0 if start_ms <= talk_ms <= end_ms else min(
                abs(start_ms - talk_ms), abs(end_ms - talk_ms)
            )
            nearest_ms = distance_ms if nearest_ms is None else min(nearest_ms, distance_ms)
    if nearest_ms is None:
        gap_ms = _void_bounds(intervals, talk_ms)
        raise PostSongTalkWitnessError(
            f"post-song talk claim at {talk_ms}ms is not corroborated by any fresh-ASR "
            f"audio within {POST_SONG_TALK_WITNESS_WINDOW_MS}ms: the claimed onset sits "
            f"inside a transcription void {gap_ms} — in a medley or continuous setlist "
            "that void is the gap between two songs, not post-song host talk"
        )
    return {
        "witness": "fresh_asr_full_source_transcript",
        "post_song_talk_start_ms": talk_ms,
        "window_ms": POST_SONG_TALK_WITNESS_WINDOW_MS,
        "nearest_transcribed_audio_distance_ms": nearest_ms,
        "cue_count": len(intervals),
    }


def _cue_intervals(asr_cues: Sequence[object]) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    for cue in asr_cues or ():
        start_ms = getattr(cue, "source_start_ms", None)
        end_ms = getattr(cue, "source_end_ms", None)
        if not _is_int(start_ms) or not _is_int(end_ms) or end_ms < start_ms:
            continue
        intervals.append((int(start_ms), int(end_ms)))
    intervals.sort()
    return intervals


def _void_bounds(intervals: Sequence[tuple[int, int]], talk_ms: int) -> str:
    before = max((end_ms for _start, end_ms in intervals if end_ms <= talk_ms), default=None)
    after = min((start_ms for start_ms, _end in intervals if start_ms >= talk_ms), default=None)
    return f"[{'source start' if before is None else before}, {'source end' if after is None else after}]"
