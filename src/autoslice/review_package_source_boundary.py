"""Preserve the exact source review consumed by a final boundary witness."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from src.autoslice.boundary_semantic_review import semantic_review_sha256

ITEM_KEY = 'original_source_boundary_review'


def original_source_review(record: dict, *, snapshot_root: Path | None = None) -> dict | None:
    source = (record.get('boundary_audit') or {}).get('boundary_semantic_review') or {}
    final = (record.get('story_contract') or {}).get('boundary_semantic_review') or {}
    expected = (final.get('source_separation_witness') or {}).get('source_review_sha256')
    if not expected or semantic_review_sha256(source) == expected:
        return None
    embedded = final.get('source_boundary_review')
    if isinstance(embedded, dict) and semantic_review_sha256(embedded) == expected:
        return deepcopy(embedded)
    if snapshot_root is None:
        parent = record.get('chat_authority_audit_path')
        if not isinstance(parent, str):
            raise ValueError('original source boundary review is unavailable')
        snapshot_root = Path(parent).parent / 'current-text-input'
    for path in sorted(snapshot_root.glob('authority.*.json')):
        match = re.fullmatch(r'authority\.([0-9a-f]{64})\.json', path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != match[1]:
            continue
        # The filename binds the captured authority bytes.  Expand review
        # references only after that byte check so a compact nested audit is
        # consumed from the exact wire payload that was matched above.
        from src.autoslice.review_evidence_store import review_from_bytes

        authority = review_from_bytes(raw, directory=path.parent)
        if not isinstance(authority, dict):
            continue
        prior = (authority.get('final_review_audit') or {}).get('boundary_semantic_review')
        if not isinstance(prior, dict):
            continue
        if (prior.get('review_scope') != 'source_full_window' or prior.get('status') != 'PASS'
                or prior.get('request_sha256') != source.get('request_sha256')
                or prior.get('cue_grid_sha256') != source.get('cue_grid_sha256')):
            continue
        # The source-stage snapshot precedes deterministic endpoint binding.
        # Recover only that derived field, then require the COMPLETE original
        # preimage digest already frozen by the actual final-review witness.
        for candidate in (prior, {**prior, 'final_endpoint_binding': source.get('final_endpoint_binding')}):
            if semantic_review_sha256(candidate) == expected:
                return deepcopy(candidate)
    raise ValueError('original source boundary review preimage is unavailable')


def materialize_original_source_review(root: Path, item: dict, record: dict) -> None:
    review = original_source_review(record)
    if review is None:
        return
    digest = semantic_review_sha256(review).removeprefix('sha256:')
    path = root / ('source-boundary-review.' + digest + '.json')
    raw = (json.dumps(review, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    if path.exists() and (path.is_symlink() or path.read_bytes() != raw):
        raise ValueError('original source boundary review artifact drift')
    if not path.exists():
        with path.open('xb') as stream:
            stream.write(raw)
    item[ITEM_KEY] = path.name
    item.setdefault('sha256', {})[ITEM_KEY] = hashlib.sha256(raw).hexdigest()


def load_original_source_review(root: Path, item: dict) -> dict | None:
    relative = item.get(ITEM_KEY)
    if not isinstance(relative, str):
        return None
    path = root / relative
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            return None
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != str((item.get('sha256') or {}).get(ITEM_KEY)).removeprefix('sha256:'):
            return None
        result = json.loads(raw)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None
