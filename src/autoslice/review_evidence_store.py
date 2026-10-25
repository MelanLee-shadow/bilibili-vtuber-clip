"""Compact audit representation; domain receipts keep their original bytes.

Only the existing Argon atomic blob writer publishes evidence. Expansion
verifies each blob before reconstructing the legacy object on which existing
hash/authority validators operate. No old files are rewritten on reads.
"""
from __future__ import annotations

from .review_errors import ContractRejection

from collections import Counter
from contextvars import ContextVar
from functools import wraps
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
_cold_active = ContextVar('review_evidence_cold_consumer', default=False)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def is_reference(value):
    return isinstance(value, dict) and value.get('schema_version') == REF_SCHEMA


def expand(value, *, store_root=None, _active=None):
    """Legacy inline values and referenced values reconstruct identically."""
    active = set() if _active is None else _active
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
        active.add(key)
        try:
            try:
                raw = store.get_blob(path.parent.parent, sha)
            except FileNotFoundError as exc:
                raise ContractRejection('REVIEW_EVIDENCE_BLOB_MISSING') from exc
            return expand(json.loads(raw), store_root=store_root, _active=active)
        finally:
            active.remove(key)
    if isinstance(value, dict):
        children = {k: expand(v, store_root=store_root, _active=active) for k, v in value.items()}
        return value if all(children[k] is v for k, v in value.items()) else children
    if isinstance(value, (list, tuple)):
        children = [expand(v, store_root=store_root, _active=active) for v in value]
        if all(a is b for a, b in zip(children, value)):
            return value
        return tuple(children) if isinstance(value, tuple) else children
    return value


def evidence_store_root(directory):
    """The one co-located store a compact audit in ``directory`` may reference."""
    return Path(directory) / 'evidence-store'


def review_from_bytes(payload, *, directory):
    """Decode exactly the captured bytes, using the co-located store if present."""
    root = evidence_store_root(directory)
    return expand(json.loads(payload), store_root=root if root.is_dir() else None)


def load_review(path):
    """Read inline or relocated audits without rewriting sealed references."""
    path = Path(path)
    return review_from_bytes(path.read_bytes(), directory=path.parent)


def cold_input(function):
    """Public cold-consumer seam, shared by all receipt owners."""
    @wraps(function)
    def consume(*args, **kwargs):
        if _cold_active.get():
            return function(*args, **kwargs)
        token = _cold_active.set(True)
        try:
            return function(*(expand(v) for v in args), **{k: expand(v) for k, v in kwargs.items()})
        finally:
            _cold_active.reset(token)
    return consume


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
        return {'schema_version': REF_SCHEMA, 'sha256': sha,
            'path': str(root / 'blobs' / (sha + '.bin'))}
    def pack(value, field=None, keep=False):
        if is_reference(value):
            # A successor must own its complete co-located closure. Merely
            # retaining a verified old absolute path becomes a dangling blob
            # once load_review relocates references to this document's store.
            return pack(expand(value), field, keep)
        original = value
        key = (id(value), field, keep)
        if isinstance(value, (dict, list, tuple)) and key in packed:
            return packed[key][1]
        if isinstance(value, dict):
            value = {k: ([pack(row, keep=True) for row in v]
                         if k in {'findings', 'unresolved_findings_disclosed'} and isinstance(v, list)
                         else pack(v, k, keep=k == 'final_review_audit'))
                     for k, v in value.items()}
        elif isinstance(value, (tuple, list)):
            value = [pack(v) for v in value]
        if not keep and (field in REFERENCE_FIELDS or len(encoded(value)) > LIMIT):
            value = reference(value)
        if isinstance(original, (dict, list, tuple)):
            packed[key] = (original, value)  # Retain identity until packing finishes.
        return value
    return pack(audit, keep=True)


def record_metrics(audit, *, prompts=()):
    large = Counter()
    def visit(value):
        if isinstance(value, (dict, list)):
            raw = encoded(value)
            if len(raw) > 20_000:
                large[hashlib.sha256(raw).hexdigest()] += 1
            for child in (value.values() if isinstance(value, dict) else value):
                visit(child)
    visit(audit)
    sizes = [len(json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2).encode()) for key in ('findings', 'unresolved_findings_disclosed')
        for row in audit.get(key, [])]
    return {'record_bytes': len(encoded(audit)), 'finding_bytes': sizes,
        'max_finding_bytes': max(sizes, default=0),
        'duplicate_subtrees_over_20kb': sum(n - 1 for n in large.values() if n > 1),
        'terminal_prompt_chars': [len(p) for p in prompts],
        'max_terminal_prompt_chars': max(map(len, prompts), default=0)}


def validate_record_metrics(metrics):
    if (metrics['max_finding_bytes'] > 100_000
            or metrics['duplicate_subtrees_over_20kb']
            or metrics['max_terminal_prompt_chars'] > 30_000):
        raise ContractRejection('REVIEW_RECORD_SIZE_LIMIT_EXCEEDED')


def persist_review(path, audit):
    """One atomic audit publisher for serial and parallel ordinary callers."""
    return persist_document(path, audit)


def project_document(document, output):
    """Use the same content addressing at every level of an authority document."""
    return compact_review(document, output)


def persist_document(path, document):
    path = Path(path)
    packed = project_document(document, path.parent)
    store.atomic_write(path, json.dumps(packed, ensure_ascii=False, sort_keys=True, indent=2).encode() + b'\n')
    return packed
