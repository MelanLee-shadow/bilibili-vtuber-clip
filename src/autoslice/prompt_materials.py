"""Representation equality for evidence, never an authorization predicate.

Only producer-owned bags may reorder. All members (including duplicates), source
bindings, ordered source arrays and normative prompt text remain significant.
Raw request/response digests continue to verify the original stored bytes.
"""
from __future__ import annotations

from .review_errors import ContractRejection

import hashlib
import json
import re
from collections.abc import Mapping

# These producers collect evidence by traversal, not chronology. Do not turn
# arbitrary arrays (word times, cues, edit spans, source windows) into sets.
EVIDENCE_BAGS = frozenset({
    ('stopped_finding_evidence', 'recorded_evidence'),
    ('current_support_summary', 'transcript_matches'),
})


def has_material_bag(value, *, _path=()):
    if isinstance(value, Mapping):
        return any(has_material_bag(child, _path=(*_path, key)) for key, child in value.items())
    if isinstance(value, list):
        return _path in EVIDENCE_BAGS or any(
            has_material_bag(child, _path=(*_path, i)) for i, child in enumerate(value))
    return False


def same_legacy_observation_materials(recorded, current):
    """v1 stop-proof evidence equality; rejected diagnostics grant no authority.

    Current owners have reopened every admitted observation. A changed reason
    for an already-excluded attempt is not a changed witness or a successful
    channel. Preserve the original rejection list in the original proof; never
    use this view for the target-channel exhaustion contract of new requests.
    """
    def view(value):
        return {key: (material_multiset(child) if key == 'observations' else child)
                for key, child in value.items() if key != 'rejected_observations'}
    return _json(view(recorded)) == _json(view(current))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def material_multiset(values):
    """Canonical bag preserving multiplicity and the complete member binding."""
    return sorted(values, key=_json)


def canonical_materials(value, *, bag_paths=EVIDENCE_BAGS, _path=()):
    if isinstance(value, Mapping):
        return {key: canonical_materials(child, bag_paths=bag_paths, _path=(*_path, key))
                for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        rows = [canonical_materials(child, bag_paths=bag_paths, _path=(*_path, i))
                for i, child in enumerate(value)]
        return material_multiset(rows) if _path in bag_paths else rows
    return value


def prompt_material_view(prompt):
    """Decode ordinary line-start JSON blocks; preserve every other byte.

    This also covers non-terminal prompts without teaching their consumers a
    second equality rule. JSON formatting/key order is representation; strings
    within a block and the surrounding instructions are not normalized.
    """
    decoder, end, result = json.JSONDecoder(), 0, []
    for match in re.finditer(r'(?m)^[ \t]*[\[{]', prompt):
        start = match.end() - 1
        if start < end:
            continue
        try:
            value, stop = decoder.raw_decode(prompt, start)
        except ValueError:
            continue
        result.extend([prompt[end:start], canonical_materials(value)])
        end = stop
    result.append(prompt[end:])
    return result


def prompt_material_sha256(prompt):
    return hashlib.sha256(_json(prompt_material_view(prompt)).encode()).hexdigest()


def same_prompt_materials(left, right):
    return left == right or _json(prompt_material_view(left)) == _json(prompt_material_view(right))


def terminal_material_index(data, *, named_materials=True):
    """Index exactly the materials rendered by either terminal producer.

    Named evidence objects carry their own IDs; recursively indexing those
    objects covers refrain support/session refrains and future named materials
    without inventing arbitrary dotted JSON-path aliases.
    """
    refs = {key: [key] for key, value in data.items() if value is not None}
    for section in ('context_sources', 'whole_session_and_session_context', 'stopped_finding_evidence'):
        for key in data.get(section) or {}:
            ref = key if section == 'context_sources' and key not in refs else section + '.' + key
            refs[ref] = [section, key]
    for i, row in enumerate(data.get('observations', [])):
        refs[row['observation_id']] = ['observations', i]
    for i, _ in enumerate(data.get('events', [])):
        refs['event:' + str(i)] = ['events', i]

    def visit(value, path):
        # Raw provider segment IDs are scoped to one observation, not global
        # citation IDs. Cite their bound observation rather than promoting an
        # opaque seg_001 from another source/window into a shared identifier.
        if 'raw_observation' in path or (path and path[0] == 'audio'):
            return
        if isinstance(value, Mapping):
            identity = value.get('id')
            if isinstance(identity, str) and identity:
                if identity in refs:
                    previous = data
                    for part in refs[identity]:
                        previous = previous[part]
                    if previous != value:
                        raise ContractRejection('TERMINAL_MATERIAL_ID_COLLISION')
                else:
                    refs[identity] = path
            for key, child in value.items():
                visit(child, [*path, key])
        elif isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, [*path, i])
    if named_materials:
        visit(data, [])
    return refs
