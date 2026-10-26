"""Content-scoped upload holds; ledger integrity and account quota stay global.

The identity is derived from an attested record, never from the uploader exit
code. Historical rows remain unchanged and readable without their old files.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.clip_context import validate_clip_context
from src.autoslice.publication_identity import publication_identity


SCHEMA = "authorized-upload-content-identity.v1"
_SHA = re.compile(r"[0-9a-f]{64}")
_LOCATOR = re.compile(r"/out/(\d{4}-\d{2}-\d{2})/([^/]+)(?:/|$)")


def _sha(value: object) -> str:
    text = str(value or "").removeprefix("sha256:")
    return text if _SHA.fullmatch(text) else ""


def _locator(value: object) -> tuple[str, str]:
    match = _LOCATOR.search(str(value or ""))
    return (match.group(1), match.group(2)) if match else ("", "")


def _read_bound_record(manifest: Mapping) -> dict:
    attestation = manifest.get("package_attestation") or {}
    if not isinstance(attestation, Mapping):
        raise ValueError("upload package attestation is not an object")
    entry = attestation.get("record") or {}
    if not isinstance(entry, Mapping):
        raise ValueError("attested upload record binding is not an object")
    path = Path(str(entry.get("path") or ""))
    expected = _sha(entry.get("sha256"))
    if not expected:
        return {}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 << 20:
            raise ValueError("attested upload record unavailable")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("attested upload record hash drift")
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise ValueError("attested upload record is not an object")
        return record
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("attested upload record unavailable") from exc


def content_identity(manifest: Mapping) -> dict:
    """Freeze candidate aliases and source-clock windows from the hashed record."""
    attestation = manifest.get("package_attestation") or {}
    record = _read_bound_record(manifest)
    root = str(attestation.get("package_root") or "")
    story = record.get("story_contract") or {}
    locator_date, locator_candidate = _locator(root + "/")
    identity = publication_identity(record)
    candidates = set(identity.candidate_aliases)
    lineage = identity.lineage_aliases
    candidates.add(locator_candidate)
    date = str(record.get("recording_date") or (
        story.get("recording_date") if isinstance(story, Mapping) else ""
    ) or locator_date)
    # Ordinary producers bind clip-context's canonical payload SHA in the
    # hashed record. Its pieces use the recording clock, unlike record.start_ms
    # which starts at zero on the final media. Never confuse those clocks.
    context = {}
    if record.get("clip_context_path") and record.get("clip_context_payload_sha256"):
        path = Path(str(record["clip_context_path"]))
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 << 20:
                raise ValueError("bound upload clip context unavailable")
            context = json.loads(path.read_bytes())
            if not isinstance(context, Mapping):
                raise ValueError("bound upload clip context is not an object")
            context = validate_clip_context(context)
            if context["context_sha256"] != record["clip_context_payload_sha256"]:
                raise ValueError("bound upload clip context hash drift")
            if candidates - {""} and context.get("candidate_id") not in candidates:
                raise ValueError("bound upload clip context candidate mismatch")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("bound upload clip context unavailable") from exc
        candidates.add(str(context.get("candidate_id") or ""))
        date = str(context.get("recording_date") or date)
    windows: set[tuple[str, int, int]] = set()
    piece_groups = [context.get("pieces") or []]
    for key in ("pieces", "source_pieces", "source_piece_map"):
        piece_groups.append(record.get(key) or [])
    for pieces in piece_groups:
        if not isinstance(pieces, list):
            continue
        for piece in pieces:
            if not isinstance(piece, Mapping):
                continue
            source_sha = _sha(piece.get("source_media_sha256") or piece.get("source_sha256"))
            start = piece.get("source_start_ms", piece.get("start_ms"))
            end = piece.get("source_end_ms", piece.get("end_ms"))
            if source_sha and type(start) is int and type(end) is int and 0 <= start < end:
                windows.add((source_sha, start, end))
            elif pieces is context.get("pieces"):
                raise ValueError("bound upload clip context source window invalid")
    return {
        "schema_version": SCHEMA,
        "candidate_ids": sorted(candidates - {""}),
        "lineage_candidate_ids": sorted(lineage),
        "same_day_candidate_ids": sorted(identity.selector_aliases),
        "recording_date": date,
        "source_windows": [
            {"source_sha256": sha, "start_ms": start, "end_ms": end}
            for sha, start, end in sorted(windows)
        ],
        "package_locator": root,
    }


def identity_problems(value: object) -> list[str]:
    if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA:
        return ["upload content identity schema invalid"]
    candidates = value.get("candidate_ids")
    if (not isinstance(candidates, list) or any(not isinstance(x, str) or not x for x in candidates)
            or candidates != sorted(set(candidates))):
        return ["upload content identity candidate aliases invalid"]
    date = value.get("recording_date")
    lineage = value.get("lineage_candidate_ids", [])
    if (not isinstance(lineage, list) or any(not isinstance(x, str) or not x for x in lineage)
            or lineage != sorted(set(lineage)) or not set(lineage) <= set(candidates)):
        return ["upload content identity lineage aliases invalid"]
    same_day = value.get("same_day_candidate_ids", [])
    if (not isinstance(same_day, list) or any(not isinstance(x, str) or not x for x in same_day)
            or same_day != sorted(set(same_day)) or not set(same_day) <= set(lineage)):
        return ["upload content identity same-day aliases invalid"]
    if not isinstance(date, str) or (date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)):
        return ["upload content identity source date invalid"]
    if not isinstance(value.get("package_locator"), str):
        return ["upload content identity package locator invalid"]
    windows = value.get("source_windows")
    if not isinstance(windows, list):
        return ["upload content identity source windows invalid"]
    for window in windows:
        if (not isinstance(window, Mapping) or not _sha(window.get("source_sha256"))
                or type(window.get("start_ms")) is not int or type(window.get("end_ms")) is not int
                or not 0 <= window["start_ms"] < window["end_ms"]):
            return ["upload content identity source window invalid"]
    return []


def _row_identity(row: Mapping) -> dict:
    frozen = row.get("content_identity")
    if frozen is not None:
        return dict(frozen)
    # Enrich only from the exact historical manifest SHA. Missing historical
    # files do not create a dependency for unrelated products.
    path = Path(str(row.get("manifest") or ""))
    expected = _sha(row.get("manifest_sha256"))
    if expected:
        try:
            if not path.is_symlink() and path.is_file() and path.stat().st_size <= 2 << 20:
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() == expected:
                    manifest = json.loads(raw)
                    if isinstance(manifest, Mapping):
                        return content_identity(manifest)
        except (OSError, ValueError, TypeError):
            pass
    date, candidate = _locator(row.get("manifest"))
    return {"candidate_ids": [candidate] if candidate else [], "recording_date": date,
            "source_windows": [], "package_locator": str(path.parent) if candidate else ""}


def row_matches_target(
    row: Mapping, video_sha256: str, manifest: Mapping | None, *, identity: dict | None = None,
) -> bool:
    if row.get("video_sha256") == video_sha256:
        return True
    if manifest is None:
        return False
    attestation = manifest.get("package_attestation") or {}
    record_sha = (attestation.get("record") or {}).get("sha256")
    if _sha(record_sha) and _sha(row.get("record_sha256")) == _sha(record_sha):
        return True
    target = content_identity(manifest) if identity is None else identity
    previous = _row_identity(row)
    if (target.get("package_locator") and
            target["package_locator"] == previous.get("package_locator")):
        return True
    dates = target.get("recording_date"), previous.get("recording_date")
    lineage = set(target.get("lineage_candidate_ids") or []) | set(previous.get("lineage_candidate_ids") or [])
    # Old frozen identities did not distinguish selector aliases from other
    # lineage. Require a known equal date for their lineage aliases as well.
    same_day = set(target.get("same_day_candidate_ids", target.get("lineage_candidate_ids")) or []) | set(previous.get("same_day_candidate_ids", previous.get("lineage_candidate_ids")) or [])
    shared = set(target["candidate_ids"]) & set(previous.get("candidate_ids") or [])
    if all(dates) and dates[0] == dates[1] and shared:
        return True
    if shared & (lineage - same_day):
        return True
    # Selector offsets are local to a recording. Missing dates cannot prove
    # that a lineage alias names the same song; do not fall through to the
    # generic candidate intersection and accidentally accept it there.
    if (not all(dates) or dates[0] == dates[1]) and shared - same_day:
        return True
    return any(
        a["source_sha256"] == b["source_sha256"]
        and max(a["start_ms"], b["start_ms"]) < min(a["end_ms"], b["end_ms"])
        for a in target["source_windows"] for b in previous.get("source_windows") or []
    )


def guard_manifest(guard, ledger: Path, video_sha256: str, manifest: Mapping):
    """Keep legacy injected two-argument accessors usable without retrying calls."""
    parameters = inspect.signature(guard).parameters
    supports_manifest = "manifest" in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )
    return (guard(ledger, video_sha256, manifest=manifest) if supports_manifest
            else guard(ledger, video_sha256))
