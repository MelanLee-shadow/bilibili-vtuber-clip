"""The native source-stage entity repair format shared by writer and readers.

A source-stage entity verdict and a later exact-final judge are distinct typed
owners. Neither is aliased to the other. Only the known unversioned source row
has a simple ingress conversion; its original bytes remain in the saved audit.
"""
from __future__ import annotations

import copy
import hashlib
from typing import Any, Mapping

SCHEMA = 'transcript-entity-repair.v1'
CLOCK = 'transcript-entity-input-srt'


def _binding(row: Mapping[str, Any]) -> dict[str, Any]:
    indexes, before, after = row.get('cue_indexes'), row.get('before'), row.get('after')
    start, end = row.get('matched_start_ms'), row.get('matched_end_ms')
    if not (row.get('mode') == 'transcript_entity_only'
            and isinstance(indexes, list) and len(indexes) == 1
            and type(indexes[0]) is int and indexes[0] > 0
            and isinstance(before, list) and len(before) == 1 and isinstance(before[0], str)
            and isinstance(after, list) and len(after) == 1 and isinstance(after[0], str)
            and type(start) is int and type(end) is int and 0 <= start < end):
        raise ValueError('CURRENT_TRANSCRIPT_ENTITY_FORMAT_INVALID')
    return {'coordinate_space': CLOCK, 'cue_index': indexes[0],
            'start_ms': start, 'end_ms': end,
            'before_sha256': hashlib.sha256(before[0].encode()).hexdigest(),
            'after_sha256': hashlib.sha256(after[0].encode()).hexdigest()}


def write_receipt(row: Mapping[str, Any]) -> dict[str, Any]:
    """Declare exact native shape before the ordinary audit is persisted."""
    return {**copy.deepcopy(dict(row)), 'schema_version': SCHEMA, 'cue_binding': _binding(row)}


def read_receipt(row: Mapping[str, Any]) -> dict[str, Any]:
    """One bounded legacy conversion, then one canonical reader contract."""
    if 'schema_version' not in row:
        # No JUDGED alias, invented verdict, cue renumbering or hash rewriting.
        canonical = write_receipt(row)
    else:
        canonical = copy.deepcopy(dict(row))
    if (canonical.get('schema_version') != SCHEMA
            or canonical.get('cue_binding') != _binding(canonical)):
        raise ValueError('CURRENT_TRANSCRIPT_ENTITY_FORMAT_BINDING_INVALID')
    return canonical
