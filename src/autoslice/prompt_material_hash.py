"""Content hash of a prompt as its evidence materials, split from prompt_materials so the lane can hash prompts
without importing the finding-context machinery that module also serves."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any

# These producers collect evidence by traversal, not chronology. Do not turn
# arbitrary arrays (word times, cues, edit spans, source windows) into sets.
EVIDENCE_BAGS = frozenset({
    ('stopped_finding_evidence', 'recorded_evidence'),
    ('current_support_summary', 'transcript_matches'),
})


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def material_multiset(values: Iterable[Any]) -> list[Any]:
    """Canonical bag preserving multiplicity and the complete member binding."""
    return sorted(values, key=_json)


def canonical_materials(value: Any, *, bag_paths: Collection[tuple[str, ...]] = EVIDENCE_BAGS, _path: tuple[Any, ...] = (),
                        _memo: dict[Any, Any] | None = None, _read_only: bool = False) -> Any:
    if _read_only and _memo is None:
        _memo = {}
    # Path matters only while it can still name a configured bag. Shared
    # ordinary subtrees can then retain one normalized graph node.
    relevant = _path if any(path[:len(_path)] == _path for path in bag_paths) else None
    key = (id(value), relevant)
    if _memo is not None and isinstance(value, (Mapping, list, tuple)) and key in _memo:
        return _memo[key]
    result: Any
    if isinstance(value, Mapping):
        result = {name: canonical_materials(child, bag_paths=bag_paths, _path=(*_path, name), _memo=_memo, _read_only=_read_only)
                  for name, child in value.items()}
    elif isinstance(value, (list, tuple)):
        rows = [canonical_materials(child, bag_paths=bag_paths, _path=(*_path, i), _memo=_memo, _read_only=_read_only)
                for i, child in enumerate(value)]
        result = material_multiset(rows) if _path in bag_paths else rows
    else:
        return value
    if _memo is not None:
        _memo[key] = result
    return result


def prompt_material_view(prompt: str) -> list[Any]:
    """Decode ordinary line-start JSON blocks; preserve every other byte.

    This also covers non-terminal prompts without teaching their consumers a
    second equality rule. JSON formatting/key order is representation; strings
    within a block and the surrounding instructions are not normalized.
    """
    from .discovery_evidence_presentation import expand_source_context
    decoder, end = json.JSONDecoder(), 0
    result: list[Any] = []
    for match in re.finditer(r'(?m)^[ \t]*[\[{]', prompt):
        start = match.end() - 1
        if start < end:
            continue
        try:
            value, stop = decoder.raw_decode(prompt, start)
        except ValueError:
            continue
        result.extend([prompt[end:start], canonical_materials(expand_source_context(value))])
        end = stop
    result.append(prompt[end:])
    return result


def prompt_material_sha256(prompt: str) -> str:
    return hashlib.sha256(_json(prompt_material_view(prompt)).encode()).hexdigest()
