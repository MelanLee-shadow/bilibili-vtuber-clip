"""The publication authority (which clip is which published video, and the proofs a revision of a published video
must replay) is the operating state and safety chain of one channel.  It is not part of this public snapshot;
this module keeps the names the pipeline imports so that everything before publishing runs unchanged.

A fresh installation has published nothing: the registry is empty.
"""

from __future__ import annotations

from typing import Any


def load_publication_registry() -> dict[str, Any]:
    return {"entries": []}
