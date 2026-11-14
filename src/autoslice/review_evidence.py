from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


@dataclass(frozen=True)
class SourceCue:
    """A normalized source-timeline cue used by review evidence.

    The identity of a cue is its source-absolute timestamp.  Clip-relative
    timings are deliberately not represented here so replay/shadow evidence can
    be traced back to the original recording and re-cut deterministically.
    """

    cue_id: str
    source_start_ms: int
    source_end_ms: int
    text: str
    language: str | None = None
    kind: str = "speech"
    speaker: str | None = None
    confidence: float | None = None

    def to_manifest(self) -> dict[str, object]:
        return _drop_none(asdict(self))


def _drop_none(data: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in data.items() if value is not None}
