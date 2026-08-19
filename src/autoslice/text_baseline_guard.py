"""Keep speaker labels out of subtitle text-baseline authorities."""

from __future__ import annotations

import re
from typing import Iterable


BASELINE_CONTAINS_SPEAKER_LABEL_PREFIX = (
    "BASELINE_CONTAINS_SPEAKER_LABEL_PREFIX"
)
_SPEAKER_LABEL_PREFIX_RX = re.compile(r"^\[[^\]\r\n]+\] ?", re.MULTILINE)


def contains_speaker_label_prefix(values: str | Iterable[str]) -> bool:
    """Return true when any baseline text line starts with ``[speaker]``."""

    if isinstance(values, str):
        return _SPEAKER_LABEL_PREFIX_RX.search(values) is not None
    return any(
        _SPEAKER_LABEL_PREFIX_RX.search(str(value)) is not None
        for value in values
    )
