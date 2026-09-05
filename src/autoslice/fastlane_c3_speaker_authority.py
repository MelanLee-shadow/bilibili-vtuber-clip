"""OSS stub: private C3 speaker authority is not redistributed."""
from __future__ import annotations

from pathlib import Path


ASSET = Path("assets/_template/speaker_authority.v1.json")


class C3SpeakerAuthorityUnavailableError(ValueError):
    """The private speaker authority is unavailable in an OSS snapshot."""


def load_c3_speaker_authority(*_args, **_kwargs):
    raise C3SpeakerAuthorityUnavailableError(
        "C3_PRIVATE_SPEAKER_AUTHORITY_UNAVAILABLE"
    )
