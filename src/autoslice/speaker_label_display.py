"""Project speaker metadata into ASS style and label-free production text."""

from __future__ import annotations

from dataclasses import dataclass
from src.autoslice.channel_profile import LABEL_RE


@dataclass(frozen=True)
class SpeakerDisplay:
    speaker: str | None
    style: str
    text: str


def speaker_display(
    text: str,
    *,
    host_speaker: str,
    speaker: str | None = None,
) -> SpeakerDisplay:
    """Labels select colour/style; production display never adds their names.

    Song SRT carries a leading label; reviewed talk cues already separate it
    into ``speaker`` and authoritative text. Strip any remaining leading label
    even in that representation: no renderer can opt into visible names.
    """
    while label := LABEL_RE.match(text):
        if speaker is None:
            speaker = label.group(1)
        text = label.group(2)
    style = "Default" if speaker is None else "LDS" if speaker == host_speaker else "GUEST"
    return SpeakerDisplay(speaker, style, text)
