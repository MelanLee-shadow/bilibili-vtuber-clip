"""Derive a recall endpoint from a verified publication's real source chain.

This is a coordinate/identity bridge, never a semantic verdict.  A published
delivery-local duration or a padded-local endpoint cannot sign a source clock.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
from pathlib import Path

# Chain documents (authority, manifest, record, provenance, checkpoint) are
# small; bulk chat/review dumps above this are kept but never expanded.
CHAIN_DOCUMENT_MAX_BYTES = 16 * 1024**2


def _absolute_paths(value: object):
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _absolute_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from _absolute_paths(item)
    elif (isinstance(value, str) and value.startswith("/") and not value.startswith("//")
          and len(value) < 4096 and "\n" not in value):
        yield Path(value)


def _hash_bindings(value: object):
    if isinstance(value, Mapping):
        if isinstance(value.get("path"), str) and "sha256" in value:
            yield Path(value["path"])
        for item in value.values():
            yield from _hash_bindings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _hash_bindings(item)


def _chain_document(path: Path) -> list[object]:
    if path.suffix.lower() not in {".json", ".jsonl"} or path.stat().st_size > CHAIN_DOCUMENT_MAX_BYTES:
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        if path.suffix.lower() == ".jsonl":
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        return [json.loads(text)]
    except ValueError:
        return []


def published_recall_inputs(authority_path: Path, candidate_ids: Iterable[str], *,
                            within: Path | None = None) -> tuple[set[Path], bool]:
    """Every file a published candidate's recall or same-BV repair replays.

    Single definition for runtime GC protection.  It starts at the verified
    publication authority and follows each absolute path its chain documents
    name (evidence, manifest, record, clip context, cover/video, and the
    recut→padded→piece→recording provenance), plus the readers' fixed
    siblings: ``<media>.provenance.json``, the SOURCE checkpoint parent files
    of each ``output_root/<cid>`` directory, and a chat authority's evidence
    store.  Nothing is hashed.  ``within`` bounds the walk to one runtime root
    (never stat a FUSE recording mount).  ``complete`` is False when the
    authority or one of its own hash bindings is missing; callers fail closed.
    """
    from src.autoslice.review_evidence_store import evidence_store_root
    from src.autoslice.source_checkpoint_parent import checkpoint_parent_paths

    ids = {str(cid) for cid in candidate_ids if cid}
    authority_path = Path(authority_path)
    files: set[Path] = set()
    complete = authority_path.is_file() and not authority_path.is_symlink()
    if complete:
        documents = _chain_document(authority_path)
        complete = bool(documents) and isinstance(documents[0], Mapping) and all(
            path.is_file() for path in _hash_bindings(documents[0]))
    queue, seen = [authority_path], set()
    while queue:
        path = queue.pop()
        if path in seen or within is not None and within not in path.parents:
            continue
        seen.add(path)
        if path.is_symlink() or not path.is_file():
            continue
        files.add(path)
        if path.suffix.lower() not in {".json", ".jsonl"}:
            queue.append(path.with_suffix(".provenance.json"))
        if path.name.endswith(".chat-authority.json"):
            store = evidence_store_root(path.parent)
            if store.is_dir() and not store.is_symlink():
                files.update(p for p in store.rglob("*") if p.is_file() and not p.is_symlink())
        if path.parent.name in ids:
            queue.extend(checkpoint_parent_paths(path.parent, path.parent.name).values())
        for document in _chain_document(path):
            queue.extend(_absolute_paths(document))
    return files, complete


