"""Derive a recall endpoint from a verified publication's real source chain.

This is a coordinate/identity bridge, never a semantic verdict.  A published
delivery-local duration or a padded-local endpoint cannot sign a source clock.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
from pathlib import Path

SCHEMA_VERSION = "first-publication-source-boundary.v1"
BOUNDARY_FIELDS = frozenset({
    "boundary_end_mode", "required_given_end_ms", "published_source_boundary",
})
# Chain documents (authority, manifest, record, provenance, checkpoint) are
# small; bulk chat/review dumps above this are kept but never expanded.
CHAIN_DOCUMENT_MAX_BYTES = 16 * 1024**2


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _sha(value: object) -> str:
    return str(value or "").removeprefix("sha256:")


def build_source_boundary(authority: Mapping[str, object]) -> dict[str, object]:
    """Replay the publication, its record, and the native single-source recut.

    The narrow bridge supports the normal one-recording representation.
    Multi-recording clocks need an explicit mapping, so they fail closed here.
    """
    from src.autoslice.new_bv_repair_authority import (
        _regular, replay_first_publication_manifest,
    )
    from src.autoslice.publication_reconciliation import (
        _load_object, _sha_entry, _validate_sha_entry,
    )
    from src.autoslice.independent_context_adjudication import (
        _file_sha256, _range_media_source_binding,
    )

    manifest = replay_first_publication_manifest(authority)
    attestation = manifest.get("package_attestation")
    _need(isinstance(attestation, Mapping), "first publication package binding missing")
    record_entry = attestation.get("record")
    record_path = _validate_sha_entry(record_entry, "published record")
    _regular(record_path)
    record = _load_object(record_path, "published record")
    candidate = authority.get("candidate_id")
    _need(record.get("candidate_id") == candidate, "published record candidate drifted")
    hashes = record.get("artifact_hashes")
    _need(isinstance(hashes, Mapping), "published record artifact hashes missing")
    video = manifest.get("video")
    _need(isinstance(video, Mapping), "published final video binding missing")
    _need(
        _sha(video.get("sha256")) == _sha(hashes.get("burned_video_sha256"))
        and len(_sha(video.get("sha256"))) == 64,
        "published final video/record binding drifted",
    )

    context_path = Path(str(record.get("clip_context_path") or ""))
    _regular(context_path)
    context_entry = _sha_entry(context_path)
    _need(
        _sha(context_entry["sha256"]) == _sha(hashes.get("clip_context_file_sha256")),
        "published source context bytes drifted",
    )
    context = _load_object(context_path, "published source context")
    pieces = context.get("pieces")
    _need(
        context.get("candidate_id") == candidate
        and context.get("recording_date") == authority.get("recording_date")
        and isinstance(pieces, list) and len(pieces) == 1
        and isinstance(pieces[0], Mapping),
        "published source candidate/date/single-clock binding invalid",
    )
    source_sha = pieces[0].get("source_media_sha256")
    media = Path(str(record.get("media_path") or ""))
    _regular(media)
    media_sha = _file_sha256(str(media))
    _need(
        media_sha is not None and media_sha == _sha(hashes.get("video_sha256")),
        "published recut bytes drifted",
    )
    native = _range_media_source_binding(
        {"source_media_sha256": media_sha,
         "clip_context_binding": {"source_media_sha256s": [source_sha]}},
        str(media), media_sha,
    )
    _need(
        isinstance(native, Mapping)
        and native.get("role") == "PRODUCER_FINAL_RECUT_FROM_RECORDING",
        "published recut source provenance invalid",
    )
    parent = native.get("parent_media_source_binding")
    _need(
        isinstance(parent, Mapping)
        and parent.get("role") == "PRODUCER_PADDED_MEDIA_FROM_RECORDING"
        and _sha(parent.get("recording_sha256")) == _sha(source_sha)
        and (parent.get("recording_start_ms"), parent.get("recording_end_ms"))
        == (pieces[0].get("start_ms"), pieces[0].get("end_ms")),
        "published source clock/context drifted",
    )
    start, end = native.get("absolute_source_start_ms"), native.get("absolute_source_end_ms")
    boundary = record.get("boundary_audit")
    interval = native.get("final_recut_interval_ms")
    _need(
        type(start) is int and type(end) is int and 0 <= start < end
        and isinstance(boundary, Mapping)
        and interval == [boundary.get("final_start_ms"), boundary.get("final_end_ms")]
        and record.get("start_ms") == 0
        and type(record.get("end_ms")) is int
        and record.get("duration_ms") == record["end_ms"] == end - start
        and interval[1] - interval[0] == end - start,
        "published delivery/padded/source interval drifted",
    )
    # Bind the original provenance bytes as well as the native derived clock.
    proof_path = Path(str(native["final_recut_provenance_path"]))
    _regular(proof_path)
    proof = _load_object(proof_path, "published recut provenance")
    source_piece = proof.get("source_piece")
    _need(isinstance(source_piece, Mapping), "published source clock is not single recording")
    source_path = Path(str(source_piece.get("source_path") or ""))
    _regular(source_path)
    source_entry = {"path": str(source_path), "sha256": _sha(source_sha),
                    "bytes": source_path.stat().st_size}
    _need(
        _sha_entry(record_path) == dict(record_entry)
        and _sha_entry(context_path) == context_entry
        and _sha_entry(proof_path)["sha256"] == native["final_recut_provenance_sha256"],
        "published evidence changed during replay",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate,
        "bvid": authority["bvid"],
        "source_clock": "recording_source",
        "absolute_source_interval_ms": [start, end],
        "record": dict(record_entry),
        "clip_context": context_entry,
        "source_media": source_entry,
        "source_provenance": dict(native),
    }


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


def publication_boundary_fields(authority: Mapping[str, object]) -> dict[str, object]:
    """Generate all boundary fields together; callers cannot choose an endpoint."""
    proof = build_source_boundary(authority)
    return {
        "boundary_end_mode": "published_recall_anchor",
        "required_given_end_ms": proof["absolute_source_interval_ms"][1],
        "published_source_boundary": proof,
    }


def validate_boundary_fields(authority: Mapping[str, object]) -> None:
    expected = publication_boundary_fields(authority)
    _need(
        {key: authority.get(key) for key in BOUNDARY_FIELDS} == expected,
        "first publication boundary mode/end/source binding drifted",
    )


def bind_published_recall_to_spec(spec: dict[str, object]) -> bool:
    """Normal producer ingress: derive fields and reject caller clock changes."""
    from src.autoslice.new_bv_repair_authority import (
        SCHEMA_VERSION as AUTHORITY_SCHEMA, validate_new_bv_repair_authority,
    )

    raw = spec.get("recovery_publication_authority")
    if not isinstance(raw, Mapping) or raw.get("schema_version") != AUTHORITY_SCHEMA:
        return False
    if not BOUNDARY_FIELDS.intersection(raw):
        return False  # Historical title-only authority remains title-only.
    authority = validate_new_bv_repair_authority(
        raw, candidate_id=str(spec.get("candidate_id") or ""),
        expected_final_title=spec.get("given_title"),
    )
    proof = authority["published_source_boundary"]
    pieces = spec.get("pieces")
    _need(
        isinstance(pieces, list) and len(pieces) == 1 and isinstance(pieces[0], Mapping),
        "published recall requires one bound source clock",
    )
    piece = pieces[0]
    _need(
        _sha(piece.get("source_media_sha256")) == proof["source_media"]["sha256"]
        and type(piece.get("start_ms")) is int and type(piece.get("end_ms")) is int
        and 0 <= piece["start_ms"] < authority["required_given_end_ms"] <= piece["end_ms"]
        and spec.get("date") == authority["recording_date"],
        "published recall source clock/hash/date drifted",
    )
    from src.autoslice.boundary_semantic_review import SEMANTIC_TAIL_TRIM_MAX_MS
    fields = {
        "semantic_tail_trim_cap_ms": SEMANTIC_TAIL_TRIM_MAX_MS,
        "given_end_ms": authority["required_given_end_ms"],
        "given_end_mode": "published_recall_anchor",
        "given_end_authority": "verified-first-publication:" + authority["authority_sha256"],
    }
    for key, expected in fields.items():
        _need(key not in spec or spec[key] == expected,
              "published recall spec mode/end/authority drifted")
    spec.update(fields)
    return True
