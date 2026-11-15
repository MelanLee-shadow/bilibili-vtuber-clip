"""The publication registry (which package is which published BV) is the one the public upload tool writes:
src/autoslice/lane_publish/registry.py.  A fresh installation has published nothing; a machine without the runtime
state directory cannot say, and raises rather than answer "nothing"."""

from __future__ import annotations

from typing import Any

from src.autoslice.lane_publish.registry import load


def load_publication_registry() -> dict[str, Any]:
    return load()


def _candidate_ids(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("_candidate_ids: the channel's publication authority is not part of this snapshot")
