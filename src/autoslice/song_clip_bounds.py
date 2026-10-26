"""The one definition of where a song clip starts.

维护者: "歌切最完美的形态当然是按一首歌的长度，一首歌什么时候开始，我们的歌切
什么时候开始，一首歌什么时候结束，我们什么时候结束。但是有的时候主播可能会这个比如说
提前放音，或者说他晚了唱了，或者说他提前结束了之类的。……然后如果有的时候在前奏的时候
主播还在说话，那就需要把这个给延长一点，因为主播还在说话，你不能从一半开始，所以这个
会需要容忍一下。"

* Music start: the earliest of the LRC zero projected on the live clock
  (code-derived from BCUT anchors: the backing track follows the studio
  timeline, so a late first vocal never moves it), the first placed/anchored
  lyric onset, and a deterministic *earlier* audio onset in the hash-bound
  observation audio (sound running into the song after a proven silent gap,
  e.g. music played early).  A provider's self-reported time is never used.
* Host speech over the intro: a host speech cue (the shared
  ``song_spoken_interlude.host_speech_text`` test) overlapping
  ``[music start, first lyric)`` or ending at most ``SAME_UTTERANCE_GAP_MS``
  before the music start moves the start to the beginning of that utterance
  (consecutive cues at most ``SAME_UTTERANCE_GAP_MS`` apart), never more than
  ``INTRO_SPEECH_MAX_LEAD_MS`` before the music start.
* The start keeps ``RECOGNITION_TOLERANCE_MS`` before that point so the first
  sound is not clipped, clamped to 0.
* The end is the existing post-song transition (``song_performance``).

Both proof paths (audio/LRC and text/ASR) call ``song_clip_start``; the
evidence it returns is stored on the song boundary.
"""

from __future__ import annotations

from array import array
import hashlib
from pathlib import Path
import subprocess
import sys
from typing import Mapping, Sequence

from src.autoslice.boundary_semantic_review import SEMANTIC_FLOOR_SILENT_GAP_MS
from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_spoken_interlude import host_speech_text

CLIP_START_RULE = "song-clip-start.v1"
# The boundary step's machine-provable silent gap (30-boundary, =DELIVERY_TAIL_PAD_MS):
# cues closer than this belong to one utterance; a silence this long proves a pause.
SAME_UTTERANCE_GAP_MS = SEMANTIC_FLOOR_SILENT_GAP_MS
# 行政解释（需确认,）: at most 15s earlier for intro speech; 300ms
# recognition tolerance so the first sound is not cut.
INTRO_SPEECH_MAX_LEAD_MS = 15_000
RECOGNITION_TOLERANCE_MS = 300

AUDIO_ONSET_UNAVAILABLE = "UNAVAILABLE"
AUDIO_NO_EARLIER_ONSET = "NO_EARLIER_ONSET"
AUDIO_EARLIER_ONSET = "EARLIER_ONSET"
_SAMPLE_RATE_HZ = 16_000
_FRAME_MS = 10


def earlier_audio_onset_ms(samples: Sequence[int], *, window_start_ms: int, window_end_ms: int) -> int | None:
    """First sound after the last proven silence in ``[window_start, window_end)``.

    Uses the existing energy VAD frames (``host_occupancy``), which separate
    sound from silence but not voice from music.  Silence means below the VAD's
    absolute RMS floor, never merely quieter than its neighbours: a quiet bar
    inside continuous BGM or music is not a pause.  Returns ``None`` when the
    window has no silent gap of ``SAME_UTTERANCE_GAP_MS`` (nothing is proven)
    or when silence lasts up to ``window_end_ms``.
    """
    from src.autoslice.host_occupancy import VAD_ABSOLUTE_RMS_FLOOR, frame_rms

    flags = [value >= VAD_ABSOLUTE_RMS_FLOOR
             for value in frame_rms(samples, frame_length=_SAMPLE_RATE_HZ * _FRAME_MS // 1000)]
    min_silent_frames = SAME_UTTERANCE_GAP_MS // _FRAME_MS
    silent = 0
    onset_frame = None
    for index, sounding in enumerate(flags):
        if sounding:
            if silent >= min_silent_frames:
                onset_frame = index
            silent = 0
        else:
            silent += 1
    if silent >= min_silent_frames or onset_frame is None:
        return None
    onset_ms = window_start_ms + onset_frame * _FRAME_MS
    return onset_ms if onset_ms < window_end_ms else None


def _decode_window(path: Path, start_ms: int, end_ms: int) -> array | None:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{start_ms / 1000:.3f}",
             "-t", f"{(end_ms - start_ms) / 1000:.3f}", "-i", str(path), "-ac", "1",
             "-ar", str(_SAMPLE_RATE_HZ), "-f", "s16le", "pipe:1"],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    samples = array("h")
    samples.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def audio_onset_evidence(audio: tuple[Path, str] | None, *, estimate_ms: int) -> dict[str, object]:
    """Deterministic earlier-onset evidence from the hash-bound observation audio."""
    window = [max(0, estimate_ms - INTRO_SPEECH_MAX_LEAD_MS), estimate_ms]
    evidence: dict[str, object] = {"status": AUDIO_ONSET_UNAVAILABLE, "window_ms": window}
    if audio is None or window[1] <= window[0]:
        return evidence
    path, sha256 = audio
    try:
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != str(sha256).removeprefix("sha256:"):
            return evidence
    except OSError:
        return evidence
    evidence["audio_sha256"] = str(sha256).removeprefix("sha256:")
    samples = _decode_window(Path(path), *window)
    expected = (window[1] - window[0]) * _SAMPLE_RATE_HZ // 1000
    if samples is None or len(samples) < expected - _SAMPLE_RATE_HZ:
        return evidence
    onset = earlier_audio_onset_ms(samples, window_start_ms=window[0], window_end_ms=window[1])
    evidence.update(status=AUDIO_EARLIER_ONSET if onset is not None else AUDIO_NO_EARLIER_ONSET,
                    onset_ms=onset)
    return evidence


def song_clip_start(
    *,
    lrc_zero_ms: int,
    first_lyric_start_ms: int,
    cues: Sequence[SourceCue],
    lyrics: Sequence[str],
    audio_onset: Mapping[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    """Return the clip start and the evidence that produced it."""
    onset = (audio_onset or {}).get("onset_ms")
    candidates = [int(lrc_zero_ms), int(first_lyric_start_ms)]
    if (audio_onset or {}).get("status") == AUDIO_EARLIER_ONSET and isinstance(onset, int):
        candidates.append(onset)
    if any(value < 0 for value in candidates):
        raise ValueError("song clip start evidence is invalid")
    music_start = min(candidates)
    floor_ms = max(0, music_start - INTRO_SPEECH_MAX_LEAD_MS)
    speech = sorted((c for c in cues if host_speech_text(c, lyrics) is not None),
                    key=lambda c: (c.source_start_ms, c.source_end_ms, c.cue_id))
    used = {c.cue_id: c for c in speech if c.source_start_ms < first_lyric_start_ms
            and c.source_end_ms >= music_start - SAME_UTTERANCE_GAP_MS}
    start_point = min([music_start, *(c.source_start_ms for c in used.values())])
    while used:
        # Walk back through the same utterance: cues no more than one
        # proven-silent gap apart.
        earlier = {c.cue_id: c for c in speech if c.cue_id not in used
                   and c.source_start_ms < start_point
                   and c.source_end_ms >= start_point - SAME_UTTERANCE_GAP_MS}
        if not earlier:
            break
        used.update(earlier)
        start_point = min(start_point, *(c.source_start_ms for c in earlier.values()))
    clip_start_ms = max(floor_ms, start_point - RECOGNITION_TOLERANCE_MS, 0)
    evidence = {
        "rule": CLIP_START_RULE,
        "lrc_zero_ms": int(lrc_zero_ms),
        "first_lyric_start_ms": int(first_lyric_start_ms),
        "audio_onset": dict(audio_onset) if audio_onset is not None else {"status": AUDIO_ONSET_UNAVAILABLE},
        "music_start_ms": music_start,
        "intro_host_speech": [[c.cue_id, c.source_start_ms, c.source_end_ms]
                              for c in sorted(used.values(), key=lambda c: c.source_start_ms)],
        "speech_capped": start_point - RECOGNITION_TOLERANCE_MS < floor_ms,
        "clip_start_ms": clip_start_ms,
    }
    return clip_start_ms, evidence
