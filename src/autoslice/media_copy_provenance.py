"""A replayable independent byte copy of an already validated media source.

This receipt records a copy, never a recut or a provider observation. The same
range source reader replays the original proof chain and every file it read.
"""
from __future__ import annotations

from .review_errors import ContractRejection

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import shutil

SCHEMA = "media-independent-copy-provenance.v1"
_READS: ContextVar[dict | None] = ContextVar("media_source_reads", default=None)
_REPLAY: ContextVar[tuple[str, ...]] = ContextVar("media_copy_replay", default=())
_STAT = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns",
         "st_mode", "st_uid", "st_gid", "st_nlink")


def value_sha(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def file_snapshot(path: Path, expected_sha256: str | None = None) -> dict:
    # Reuse the native relocation reader's no-link/open-fd/stable-stat checks.
    from src.autoslice.native_audio_budget_receipt import (
        _source_locator, _source_sha256_stable,
    )
    resolved = _source_locator(path, label="materialization")
    before_stat = resolved.stat()
    before = {key: getattr(before_stat, key) for key in _STAT}
    actual = _source_sha256_stable(resolved, label="materialization")
    after_stat = resolved.stat()
    after = {key: getattr(after_stat, key) for key in _STAT}
    if before != after or (expected_sha256 is not None and actual != expected_sha256):
        raise ContractRejection("MATERIALIZED_SOURCE_FILE_DRIFT")
    return {"path": str(resolved), "sha256": actual, "stat": after}


def record_source_read(path: Path, expected_sha256: str) -> None:
    """Observe only files actually consumed by the existing provenance reader."""
    reads = _READS.get()
    if reads is None:
        return
    row = file_snapshot(path, expected_sha256)
    previous = reads.setdefault(row["path"], row)
    if previous != row:
        raise ContractRejection("MATERIALIZED_SOURCE_CHANGED_DURING_VALIDATION")


@contextmanager
def source_reads():
    parent = _READS.get()
    rows = {}
    token = _READS.set(rows)
    try:
        yield rows
    finally:
        _READS.reset(token)
        if parent is not None:
            for path, row in rows.items():
                if parent.setdefault(path, row) != row:
                    raise ContractRejection("MATERIALIZED_SOURCE_CHANGED_DURING_VALIDATION")


def prepared_identity(*, completed_record: Mapping, clip_context: Mapping,
                      candidate_id: str) -> dict:
    return {"candidate_id": candidate_id,
            "completed_record_sha256": value_sha(completed_record),
            "clip_context_sha256": value_sha(clip_context)}


def _same_file(left: dict, right: dict) -> bool:
    return tuple(left["stat"][k] for k in ("st_dev", "st_ino")) == tuple(
        right["stat"][k] for k in ("st_dev", "st_ino"))



def _valid_file_row(row) -> bool:
    return (isinstance(row, Mapping) and isinstance(row.get("path"), str)
            and isinstance(row.get("sha256"), str) and len(row["sha256"]) == 64
            and isinstance(row.get("stat"), Mapping) and set(row["stat"]) == set(_STAT)
            and all(type(v) is int for v in row["stat"].values()))


def _valid_source_clock(clock, binding, identity) -> bool:
    if (not isinstance(clock, Mapping)
            or clock.get("schema_version") != "prepared-source-boundary-clock.v1"
            or clock.get("receipt_sha256") != value_sha(
                {k: v for k, v in clock.items() if k != "receipt_sha256"})
            or clock.get("candidate_id") != identity["candidate_id"]
            or clock.get("authority") != "SOURCE_CLOCK_ONLY"
            or clock.get("release_authorized") is not False
            or clock.get("acoustic_timeline_offset_changed") is not False
            or type(clock.get("source_boundary_timeline_offset_ms")) is not int):
        return False
    if binding.get("role") == "PRODUCER_FINAL_RECUT_FROM_RECORDING":
        interval = binding["final_recut_interval_ms"]
        return (clock.get("source_endpoint_ms") == interval
                and clock["source_boundary_timeline_offset_ms"] == interval[0])
    return clock["source_boundary_timeline_offset_ms"] == 0

def replay_copy_binding(current_request: Mapping, media_path: str, media_sha: str,
                        proof_path: Path, proof: Mapping) -> dict | None:
    """Fail closed through the shared source entry, including on cold replay."""
    from src.autoslice.argon_escalation_storage import read
    from src.autoslice.independent_context_adjudication import _range_media_source_binding

    active = _REPLAY.get()
    token = None
    try:
        media = str(Path(media_path).absolute())
        if media in active or len(active) >= 32:
            return None
        token = _REPLAY.set((*active, media))
        if (proof.get("schema_version") != SCHEMA
                or proof.get("operation") != "INDEPENDENT_BYTE_COPY"
                or proof.get("release_authorized") is not False
                or proof.get("provider_observation") is not False
                or proof.get("receipt_sha256") != value_sha(
                    {k: v for k, v in proof.items() if k != "receipt_sha256"})
                or json.loads(read(proof_path)) != proof):
            return None
        declared = current_request.get("clip_context_binding") or {}
        sources = declared.get("source_media_sha256s")
        identity = proof.get("prepared_identity")
        if (not isinstance(identity, Mapping) or not identity.get("candidate_id")
                or not isinstance(sources, list)
                or sorted({str(v).removeprefix("sha256:").lower() for v in sources})
                != proof.get("recording_sha256s")):
            return None
        if current_request.get("candidate_id", declared.get("candidate_id")) != identity["candidate_id"]:
            return None
        for candidate in (current_request.get("candidate_id"), declared.get("candidate_id")):
            if candidate is not None and candidate != identity["candidate_id"]:
                return None
        source, output = proof["source"], proof["output"]
        if (not _valid_file_row(source) or not _valid_file_row(output)
                or not isinstance(proof.get("source_reads"), list)
                or not all(_valid_file_row(row) for row in proof["source_reads"])):
            return None
        from src.autoslice.evidence_identity import content_view
        current_output = file_snapshot(Path(media), media_sha)
        current_source = file_snapshot(Path(source["path"]), media_sha)
        # Recorded rows bind content; independence is checked on the files
        # as they are now (a restored copy has a new inode by construction).
        if (output["path"] != media or output["sha256"] != media_sha
                or source["path"] == media or source["sha256"] != media_sha
                or current_output["stat"]["st_nlink"] != 1
                or _same_file(current_source, current_output)
                or content_view(current_output) != content_view(output)
                or content_view(current_source) != content_view(source)):
            return None
        with source_reads() as observed:
            record_source_read(Path(source["path"]), media_sha)
            original = _range_media_source_binding(
                {**current_request, "source_media_sha256": media_sha},
                source["path"], media_sha,
            )
        if (original is None or original != proof["source_binding"]
                or not _valid_source_clock(proof.get("source_clock"), original, identity)
                or [content_view(observed[k]) for k in sorted(observed)]
                != [content_view(row) for row in proof["source_reads"]]
                or proof["source_clock"].get("source_binding") != original
                or proof["source_clock"].get("media_path") != source["path"]
                or proof["source_clock"].get("media_sha256") != media_sha
                or proof["source_clock"].get("candidate_id") != identity["candidate_id"]):
            return None
        # Preserve the validated original role and crop interval. The additive
        # edge is source identity only and cannot move an acoustic timestamp.
        return {**original, "materialization": {
            "schema_version": SCHEMA, "operation": "INDEPENDENT_BYTE_COPY",
            "provenance_path": str(proof_path),
            "provenance_sha256": hashlib.sha256(read(proof_path)).hexdigest(),
            "source_path": source["path"], "output_path": media,
            "prepared_identity": dict(identity),
            "source_clock": proof["source_clock"],
            "release_authorized": False,
        }}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return None
    finally:
        if token is not None:
            _REPLAY.reset(token)


def materialize_media_copy(*, source: Path, target: Path, expected_sha256: str,
                           completed_record: Mapping, clip_context: Mapping,
                           candidate_id: str) -> Path:
    """Create once, or replay an existing valid copy; never adopt orphan bytes."""
    from src.autoslice.argon_escalation_storage import directory, put_json, read
    from src.autoslice.producer_recut_provenance import prepared_boundary_clock_binding

    source, target = source.absolute(), target.absolute()
    proof_path = target.with_suffix(".provenance.json")
    identity = prepared_identity(completed_record=completed_record,
                                 clip_context=clip_context, candidate_id=candidate_id)
    if os.path.lexists(target) or os.path.lexists(proof_path):
        if not target.is_file() or not proof_path.is_file():
            raise ContractRejection("MATERIALIZED_COPY_INCOMPLETE_OR_UNPROVEN")
        saved = json.loads(read(proof_path))
        if (saved.get("source", {}).get("path") != str(source)
                or saved.get("source", {}).get("sha256") != expected_sha256
                or saved.get("prepared_identity") != identity):
            raise ContractRejection("MATERIALIZED_COPY_CONSUMER_IDENTITY_DRIFT")
        prepared_boundary_clock_binding(media_path=target, completed_record=completed_record,
                                        clip_context=clip_context, candidate_id=candidate_id)
        return target
    directory(target.parent)
    with source_reads() as observed:
        clock = prepared_boundary_clock_binding(
            media_path=source, completed_record=completed_record,
            clip_context=clip_context, candidate_id=candidate_id)
    source_row = observed.get(str(source))
    if source_row is None or source_row["sha256"] != expected_sha256:
        raise ContractRejection("MATERIALIZED_COPY_SOURCE_IDENTITY_REQUIRED")
    # xb is create-only. A crash before the receipt leaves an explicit orphan,
    # which the replay path above rejects; it cannot be retrospectively signed.
    with source.open("rb") as original, target.open("xb") as copied:
        os.fchmod(copied.fileno(), 0o600)
        shutil.copyfileobj(original, copied, length=1024 * 1024)
        copied.flush()
        os.fsync(copied.fileno())
    output = file_snapshot(target, expected_sha256)
    if _same_file(source_row, output) or output["stat"]["st_nlink"] != 1:
        raise ContractRejection("MATERIALIZED_COPY_NOT_INDEPENDENT")
    for row in observed.values():
        if file_snapshot(Path(row["path"]), row["sha256"]) != row:
            raise ContractRejection("MATERIALIZED_COPY_SOURCE_CHAIN_DRIFT")
    receipt = {
        "schema_version": SCHEMA, "operation": "INDEPENDENT_BYTE_COPY",
        "source": source_row, "output": output,
        "source_binding": clock["source_binding"], "source_clock": clock,
        "prepared_identity": identity,
        "recording_sha256s": sorted({p["source_media_sha256"].removeprefix("sha256:").lower()
                                    for p in clip_context["pieces"]}),
        "source_reads": [observed[k] for k in sorted(observed)],
        "provider_observation": False, "release_authorized": False,
    }
    receipt["receipt_sha256"] = value_sha(receipt)
    put_json(proof_path, receipt)
    return target
