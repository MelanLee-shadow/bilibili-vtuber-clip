"""OSS compatibility boundary for project-specific publication authority."""
from __future__ import annotations

from pathlib import Path

SCHEMA_VERSION = "qualified-publication-authority.v1"
AUTHORITY_RELATIVE_PATH = Path(
    "assets/_template/qualified_publication_authority.v1.json"
)
_UNAVAILABLE = "QUALIFIED_PUBLICATION_PRIVATE_AUTHORITY_UNAVAILABLE"


class PublicationAuthorityError(ValueError):
    """No project-specific publication grant is distributed in OSS."""


def load_qualified_publication_authority(
    repository_root: str | Path,
) -> dict[str, object]:
    del repository_root
    raise PublicationAuthorityError(_UNAVAILABLE)


__all__ = [
    "AUTHORITY_RELATIVE_PATH",
    "PublicationAuthorityError",
    "load_qualified_publication_authority",
]
