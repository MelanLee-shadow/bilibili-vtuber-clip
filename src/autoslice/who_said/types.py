"""The shapes `who_said` reads and writes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NotRequired, TypedDict


class Person(TypedDict):
    name: str
    aliases: NotRequired[list[str]]
    card: NotRequired[str]                      # free text: how they talk, what they are like, who they are to the others
    voice: NotRequired[list[Sequence[float]]]   # reference speaker embeddings (same embedder as the windows)
    same_session_voice: NotRequired[list[Sequence[float]]]   # ... recorded over the same sound paths as this recording
                                                # (her settled lines of another clip of the same session)


class Opinion(TypedDict):
    """One source's view of one line.  `person` None: the source does not say.  `weight` 0..1: how far the source
    itself trusts it (each source says what its number means).  `kind`: the source's own name for the reason
    (the context reading's cue, a plugged measurement's grade)."""
    person: str | None
    weight: float
    kind: str
    why: str
    not_people: NotRequired[list[str]]     # people the source rules out for the line (a line calling someone by name)
