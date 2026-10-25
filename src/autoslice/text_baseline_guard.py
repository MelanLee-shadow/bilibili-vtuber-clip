"""Keep speaker labels out of subtitle text-baseline authorities."""

from __future__ import annotations

from typing import Iterable

from src.autoslice.channel_profile import LABEL_RE


BASELINE_CONTAINS_SPEAKER_LABEL_PREFIX = (
    "BASELINE_CONTAINS_SPEAKER_LABEL_PREFIX"
)


def contains_speaker_label_prefix(values: str | Iterable[str]) -> bool:
    """Return true when any baseline text line starts with ``[speaker]``."""

    texts = (values,) if isinstance(values, str) else (str(value) for value in values)
    return any(
        LABEL_RE.match(line) is not None
        for text in texts
        for line in text.split("\n")
    )
