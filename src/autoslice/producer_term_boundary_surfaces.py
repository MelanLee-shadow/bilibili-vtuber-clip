"""Build protected term surfaces for the producer's early cue-boundary repair."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from src.autoslice.topic_entity_graph import load_topic_entity_graph


def load_term_boundary_surfaces(
    _spec: Mapping[str, object],
    *,
    approved_timely_terms: Callable[[], Iterable[Mapping[str, object]]],
    topic_graph_disabled: Callable[[], bool],
    topic_graph_path: Callable[[], Path],
    topic_graph_expected_sha256: Callable[[], str],
) -> list[str]:
    """Return permanent plus currently approved term spellings, without truth data."""

    from src.autoslice.term_authority import protected_terms

    surfaces: list[str] = sorted(protected_terms())
    for record in approved_timely_terms():
        surfaces.append(str(record.get("canonical") or ""))
        surfaces.extend(str(value) for value in record.get("readings") or [])
        surfaces.extend(str(value) for value in record.get("aliases") or [])
    if topic_graph_disabled():
        return surfaces

    graph_path = topic_graph_path()
    if not graph_path.is_file() or graph_path.is_symlink():
        return surfaces
    try:
        graph, _graph_sha = load_topic_entity_graph(
            graph_path,
            expected_sha256=topic_graph_expected_sha256(),
        )
        if dt.datetime.now(dt.timezone.utc) > dt.datetime.fromisoformat(
            graph["expires_at"]
        ):
            return surfaces
        # Full graph, not topic-resolved: the transcript itself is later used
        # for resolution, so spelling protection must precede that resolution.
        for entity in graph.get("entities") or []:
            surfaces.append(str(entity.get("canonical_zh") or ""))
            surfaces.extend(str(value) for value in entity.get("native_names") or [])
            surfaces.extend(str(value) for value in entity.get("aliases") or [])
    except (OSError, ValueError):
        pass
    return surfaces


__all__ = ["load_term_boundary_surfaces"]
