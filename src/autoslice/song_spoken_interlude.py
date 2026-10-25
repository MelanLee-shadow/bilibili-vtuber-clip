"""Source-transcript evidence and display text for a bounded live song interlude."""

from __future__ import annotations

from dataclasses import replace
import re
from typing import Mapping, Sequence

from src.autoslice.audience_text import assert_audience_text
from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_alignment import ANCHOR_MATCH_THRESHOLD, lyric_cue_similarity
from src.autoslice.song_common import (
    CHANNEL_PROFILE, is_lrc_non_lyric_metadata, normalize_lyric_text,
)
from src.autoslice.surface_canon import canonicalize_expected_value_surfaces
from src.autoslice.channel_profile import LABEL_RE


def checked_interlude_text(text: str) -> str:
    """Use the ordinary audience and explicit entity-spelling authority.

    This is deterministic spelling QA, not an acoustic or semantic verdict.
    When the ordinary song final-text CPA review runs, it reads the complete
    final SRT, including these spoken lines.
    Lyrics never enter this text-repair function.
    """
    text = text.strip()
    assert_audience_text(text, surface="subtitle")
    text, _entity_spelling_repairs = canonicalize_expected_value_surfaces(text)
    assert_audience_text(text, surface="subtitle")
    return text


def source_spoken_interlude(
    *, cues: Sequence[SourceCue], start_ms: int, end_ms: int,
    observations: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Collect real speech inside the gap; ASR lyrics/music labels are not speech.

    An unlabelled source transcript is interpreted only within the independently
    proven continuous, solo-host live performance. Explicit non-host speakers
    are never relabelled as the host. Source times and original text survive in
    the receipt; display normalization is independently reproducible.
    """
    lyrics = lyric_texts(observations)
    selected = []
    for cue in sorted(cues, key=lambda c: (c.source_start_ms, c.source_end_ms, c.cue_id)):
        if not start_ms <= cue.source_start_ms < cue.source_end_ms <= end_ms:
            continue
        text = host_speech_text(cue, lyrics)
        if text is None:
            continue
        selected.append({
            "cue_id": cue.cue_id, "source_start_ms": cue.source_start_ms,
            "source_end_ms": cue.source_end_ms, "source_text": cue.text.strip(),
            "text": checked_interlude_text(text), "speaker": CHANNEL_PROFILE.host_speaker_label,
        })
    return selected


def lyric_texts(observations: Sequence[Mapping[str, object]]) -> list[str]:
    return [normalize_lyric_text(str(r["text"])) for r in observations
            if not is_lrc_non_lyric_metadata(str(r["text"]))]


def host_speech_text(cue: SourceCue, lyrics: Sequence[str]) -> str | None:
    """The one test that a source cue is host speech rather than a lyric.

    Shared by speech inside holes and speech over the intro
    (``song_clip_bounds``).  Returns the label-free spoken text, or ``None``
    for ASR-transcribed lyrics, music/silence labels and other or unknown
    explicit speakers.
    """
    if cue.kind != "speech" or cue.speaker not in (None, CHANNEL_PROFILE.host_speaker_label):
        return None
    text = cue.text.strip()
    label = LABEL_RE.match(text)
    if label:
        if label.group(1) != CHANNEL_PROFILE.host_speaker_label:
            return None
        text = label.group(2).strip()
    elif text.startswith("["):
        # An unrecognized explicit source label is not evidence of the
        # host. Do not silently relabel it when materializing speech.
        return None
    norm = normalize_lyric_text(text)
    if (not norm or not re.search(r"[\w\u3400-\u9fff]", norm)
            or is_lrc_non_lyric_metadata(text)
            or re.fullmatch(r"[\[【(（\s]*(?:纯音乐|伴奏|静音|silence|music)[\]】)）\s]*", text, re.I)):
        return None
    if any(lyric_cue_similarity(lyric, norm) >= ANCHOR_MATCH_THRESHOLD for lyric in lyrics if lyric):
        return None
    return text


class SongSubtitleTimeline(list[tuple[int, str]]):
    """Existing lyric timeline plus source-clock spoken cues from the same proof."""

    def __init__(self, lyrics: Sequence[tuple[int, str]], speech: Sequence[Mapping[str, object]]):
        super().__init__(lyrics)
        self.spoken_cues = tuple(SourceCue(
            str(row["cue_id"]), int(row["source_start_ms"]), int(row["source_end_ms"]),
            checked_interlude_text(str(row["text"])), speaker=str(row["speaker"]),
        ) for row in speech)

    def relative_speech(self, start_ms: int) -> tuple[SourceCue, ...]:
        return tuple(replace(cue, source_start_ms=cue.source_start_ms - start_ms,
                             source_end_ms=cue.source_end_ms - start_ms) for cue in self.spoken_cues)
