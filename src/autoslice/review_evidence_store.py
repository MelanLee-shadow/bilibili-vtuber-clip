"""Compact audit representation; domain receipts keep their original bytes.

Only the existing Argon atomic blob writer publishes evidence. Expansion
verifies each blob before reconstructing the legacy object on which existing
hash/authority validators operate. No old files are rewritten on reads.
"""
from __future__ import annotations

from .review_errors import ContractRejection
from .continuation_subchecks import session_cached_validation, _session_validation_memo

from .validation_memo import (memoized_validation, capture_validation_dependencies,
                              replay_validation_dependencies, _graph_size)
import hashlib
import json
from pathlib import Path

from . import argon_escalation_storage as store

REF_SCHEMA = 'review-evidence-reference.v1'
LIMIT = 8_000
REFERENCE_FIELDS = frozenset({'request', 'check_request', 'terminal_check_request',
    'audio_gain_termination', 'no_gain_proof', 'stopped_finding', 'stopped_adjudication',
    'prior_audio_adjudication', 'terminal_review', 'pass_receipts', 'attempts',
    'rebuilt_finding', 'terminal_question_originals', 'whole_clip_current_srt',
    'adjudication'})


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def is_reference(value):
    return isinstance(value, dict) and value.get('schema_version') == REF_SCHEMA


@memoized_validation(lambda root, sha: str(root) + ':' + sha)
def _decoded_blob(root, sha):
    try:
        raw = store.get_blob(root, sha)
    except FileNotFoundError as exc:
        raise ContractRejection('REVIEW_EVIDENCE_BLOB_MISSING') from exc
    # Cache the compact source, never its recursively expanded history. The
    # validator still captures and rechecks this exact blob's inode and hash.
    return json.loads(raw)


def expand(value, *, store_root=None, _active=None, _memo=None, _references=None,
           _shared=False):
    """Expand only reference-bearing branches; inline branches retain identity."""
    active = set() if _active is None else _active
    memo = {} if _memo is None else _memo
    references = {} if _references is None else _references
    if isinstance(value, (dict, list, tuple)) and id(value) in memo:
        return memo[id(value)][1]
    if is_reference(value):
        if set(value) != {'schema_version', 'sha256', 'path'}:
            raise ContractRejection('REVIEW_EVIDENCE_REFERENCE_INVALID')
        sha = value['sha256']
        if not store.valid_sha(sha) or not isinstance(value['path'], str):
            raise ContractRejection('REVIEW_EVIDENCE_REFERENCE_INVALID')
        path = Path(value['path'])
        if (not path.is_absolute() or path.name != sha + '.bin'
                or path.parent.name != 'blobs' or path.parent.parent.name != 'evidence-store'):
            raise ContractRejection('REVIEW_EVIDENCE_REFERENCE_INVALID')
        if store_root is not None:
            root = Path(store_root).absolute()
            if root.name != 'evidence-store':
                raise ContractRejection('REVIEW_EVIDENCE_STORE_ROOT_INVALID')
            path = root / 'blobs' / path.name
        key = str(path)
        if key in active:
            raise ContractRejection('REVIEW_EVIDENCE_REFERENCE_CYCLE')
        # Only fields the publisher defines as immutable evidence may share
        # expanded payloads. Mutable finding/authority envelopes stay distinct.
        # Local aliases stay shared within one read. Across reads the bounded
        # session memo returns detached values and replays every nested binding.
        if _shared and key in references:
            cached, dependencies = references[key]
            if replay_validation_dependencies(dependencies):
                return cached
        active.add(key)
        try:
            session = _session_validation_memo() if _shared else None
            identity = ('review_evidence.blob', _decoded_blob, key,
                        str(Path(store_root).absolute()) if store_root is not None else None, REF_SCHEMA,
                        tuple(sorted(REFERENCE_FIELDS)))
            with capture_validation_dependencies() as dependencies:
                hit, result = session.recall(identity) if session is not None else (False, None)
                if not hit:
                    result = expand(_decoded_blob(path.parent.parent, sha),
                        store_root=store_root, _active=active, _memo=memo,
                        _references=references, _shared=_shared)
                    if session is not None:
                        session.remember(identity, result, tuple(dependencies.values()), evict=False, named_blobs=True)
            if _shared:
                references[key] = (result, tuple(dependencies.values()))
            return result
        finally:
            active.remove(key)
    result = value
    if isinstance(value, dict):
        for key, original in value.items():
            child = expand(original, store_root=store_root, _active=active, _memo=memo,
                _references=references, _shared=_shared or key in REFERENCE_FIELDS)
            if child is not original:
                if result is value:
                    result = dict(value)
                result[key] = child
    elif isinstance(value, (list, tuple)):
        for index, original in enumerate(value):
            child = expand(original, store_root=store_root, _active=active, _memo=memo,
                _references=references, _shared=_shared)
            if child is not original:
                if result is value:
                    result = list(value)
                result[index] = child
        if result is not value and isinstance(value, tuple):
            result = tuple(result)
    if isinstance(value, (dict, list, tuple)):
        if len(memo) >= 4096:
            memo.pop(next(iter(memo)))
        memo[id(value)] = (value, result)
    return result


def evidence_store_root(directory):
    """The one co-located store a compact audit in ``directory`` may reference."""
    return Path(directory) / 'evidence-store'


def _small_owner(result):
    # Large one-off mutable envelopes must not evict reusable immutable blobs
    # from the shared session budget. Admission never limits valid inputs.
    return _graph_size(result, 512 * 1024) is not None


@session_cached_validation('review_evidence.decode', cache_result=_small_owner, evict=False, named_blobs=True)
def _cold_decode(payload, *, store_root=None):
    return expand(json.loads(payload), store_root=store_root)


@session_cached_validation('review_evidence.expand', cache_result=_small_owner, evict=False, named_blobs=True)
def _cold_expand(value, *, store_root=None):
    return expand(value, store_root=store_root)


def compact_review(audit, output):
    root = evidence_store_root(Path(output).absolute())
    return _compact_review(audit, root)


def _compact_review(audit, root):
    """One representation for review rows AND their authority envelopes.

    Domain hashes are calculated on expanded values, never on these storage
    references. Repeated immutable input objects are packed once per document.
    """
    published = set()
    packed = {}
    def reference(value):
        raw = encoded(value)
        sha = hashlib.sha256(raw).hexdigest()
        if sha not in published:
            assert store.blob(root, raw) == sha
            published.add(sha)
        result = {'schema_version': REF_SCHEMA, 'sha256': sha,
            'path': str(root / 'blobs' / (sha + '.bin'))}
        return result, len(encoded(result))
    def sequence(rows):
        values = [row[0] for row in rows]
        return values, 2 + max(0, len(rows) - 1) + sum(row[1] for row in rows)
    def pack(value, field=None, keep=False):
        if is_reference(value):
            return pack(expand(value), field, keep)
        original = value
        key = (id(value), field, keep)
        if isinstance(value, (dict, list, tuple)) and key in packed:
            return packed[key][1]
        if isinstance(value, dict):
            # Sorting validates the same JSON key comparability as encoded().
            sorted(value)
            children = {k: (sequence([pack(row, keep=True) for row in v])
                         if k in {'findings', 'unresolved_findings_disclosed'} and isinstance(v, list)
                         else pack(v, k, keep=k == 'final_review_audit'))
                     for k, v in value.items()}
            value = {k: row[0] for k, row in children.items()}
            size = (2 + max(0, len(children) - 1)
                + sum(len(encoded({k: None})) - 7 + 1 + row[1] for k, row in children.items()))
        elif isinstance(value, (tuple, list)):
            value, size = sequence([pack(v) for v in value])
        else:
            size = len(encoded(value))
        if not keep and (field in REFERENCE_FIELDS or size > LIMIT):
            value, size = reference(value)
        result = (value, size)
        if isinstance(original, (dict, list, tuple)):
            packed[key] = (original, result)
        return result
    try:
        return pack(audit, keep=True)[0]
    finally:
        # Recursive pack closes over this memo. Release its original and
        # compacted graphs when the call returns or raises.
        packed.clear()


def project_document(document, output):
    """Use the same content addressing at every level of an authority document."""
    return compact_review(document, output)


def persist_document(path, document):
    path = Path(path)
    packed = project_document(document, path.parent)
    store.atomic_write(path, json.dumps(packed, ensure_ascii=False, sort_keys=True, indent=2).encode() + b'\n')
    return packed
