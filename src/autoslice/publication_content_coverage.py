"""Public compatibility boundary: private merged-content proofs are not shipped.

Ordinary publications have no coverage. A declared coverage or successor cannot
be validated here and is rejected, never silently converted into an empty proof.
This file provides no generic multi-candidate approval or adapter-registration API.
"""
from __future__ import annotations

from collections.abc import Mapping

_UNAVAILABLE = "PUBLICATION_CONTENT_COVERAGE_ADAPTER_UNAVAILABLE"


def validate_publication_content_coverage(publication: Mapping) -> list[str]:
    if not isinstance(publication, Mapping):
        raise ValueError("publication content coverage publication must be an object")
    if publication.get("content_coverage") is not None:
        raise ValueError(_UNAVAILABLE)
    return []


