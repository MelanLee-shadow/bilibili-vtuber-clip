"""The one private C7b exception for a stale record media hash.

This is deliberately a loader, not a record migration: the predecessor record
is immutable and remains the authority for its boundary and all non-media
fields.  Only this exact candidate may obtain its technical replay bytes from
the independently hash-bound recut provenance.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


CANDIDATE_ID = "auto_130040_201_255"
RECORDING_DATE = "2026-08-14"
_ASSET = Path("assets/lidousha/fastlane_c7b_private") / (
    "auto_130040_201_255.source-media-reconciliation-authority.v1.json"
)
_PRIVATE_AUTHORITY = Path("assets/lidousha/fastlane_c7b_private") / (
    "auto_130040_201_255.private-authority.v1.json"
)
_SHA = "sha256:"


class C7bSourceReconciliationError(ValueError):
    """The narrow C7b technical-source exception is not fully bound."""


@dataclass(frozen=True, slots=True)
class C7bSourceReconciliation:
    expected_video_sha256: str
    actual_recut_path: Path
    materialization_start_ms: int
    materialization_end_ms: int


def _canonical(value: object) -> str:
    return _SHA + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def _mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise C7bSourceReconciliationError(code)
    return value


def _plain_sha(value: object, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise C7bSourceReconciliationError(code)
    return value


def _read_repo_document(repo_root: Path, relative: Path) -> tuple[Mapping[str, Any], str]:
    """Read one fixed authority document with no-follow, stable-byte binding."""
    try:
        root = repo_root.resolve(strict=True)
        if repo_root.is_symlink() or relative.is_absolute() or ".." in relative.parts:
            raise ValueError
        path = root / relative
        cursor = root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not stat.S_IMODE(before.st_mode) & 0o400 or stat.S_IMODE(before.st_mode) & 0o022:
            raise ValueError
        identity = (before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode), before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, ValueError) as exc:
        raise C7bSourceReconciliationError("C7B_SOURCE_AUTHORITY_UNAVAILABLE") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (
            opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode), opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns
        ) != identity:
            raise C7bSourceReconciliationError("C7B_SOURCE_AUTHORITY_DRIFT")
        chunks = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        after_fd = os.fstat(fd)
    except OSError as exc:
        raise C7bSourceReconciliationError("C7B_SOURCE_AUTHORITY_DRIFT") from exc
    finally:
        os.close(fd)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise C7bSourceReconciliationError("C7B_SOURCE_AUTHORITY_DRIFT") from exc
    after_identity = (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode), after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    if after_identity != identity or (
        after_fd.st_dev, after_fd.st_ino, stat.S_IMODE(after_fd.st_mode), after_fd.st_size, after_fd.st_mtime_ns, after_fd.st_ctime_ns
    ) != identity:
        raise C7bSourceReconciliationError("C7B_SOURCE_AUTHORITY_DRIFT")
    raw = b"".join(chunks)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C7bSourceReconciliationError("C7B_SOURCE_AUTHORITY_UNAVAILABLE") from exc
    return _mapping(value, "C7B_SOURCE_AUTHORITY_INVALID"), _SHA + hashlib.sha256(raw).hexdigest()


def resolve_c7b_source_reconciliation(
    *, repo_root: Path, date: str, candidate_id: str, record_binding: object,
    record: Mapping[str, object], provenance_binding: object,
    provenance: Mapping[str, object], padded_binding: object, actual_binding: object,
) -> C7bSourceReconciliation | None:
    """Resolve C7b only after every stale and replacement byte is exact."""

    if (date, candidate_id) != (RECORDING_DATE, CANDIDATE_ID):
        return None
    authority, authority_sha = _read_repo_document(repo_root, _ASSET)
    private, private_sha = _read_repo_document(repo_root, _PRIVATE_AUTHORITY)
    unsigned = dict(authority)
    declared_seal = unsigned.pop("canonical_self_sha256", None)
    if declared_seal != _canonical(unsigned):
        raise C7bSourceReconciliationError("C7B_SOURCE_AUTHORITY_SEAL_DRIFT")
    if set(authority) != {
        "schema_version", "candidate_id", "recording_date", "private_authority",
        "source_recording", "stale_record", "actual_recut", "provenance",
        "delivery_end_clamp", "delivery_start_clamp", "decision", "upload_allowed", "canonical_self_sha256",
    } or authority.get("schema_version") != "c7b-source-media-reconciliation-authority.v1" \
            or authority.get("candidate_id") != CANDIDATE_ID or authority.get("recording_date") != RECORDING_DATE \
            or authority.get("decision") != "PROVENANCE_BOUND_ACTUAL_RECUT_SUPERSEDES_STALE_RECORD_VIDEO_FOR_PRIVATE_REPLAY_SOURCE_ONLY" \
            or authority.get("upload_allowed") is not False:
        raise C7bSourceReconciliationError("C7B_SOURCE_AUTHORITY_INVALID")
    private_ref = _mapping(authority.get("private_authority"), "C7B_SOURCE_AUTHORITY_INVALID")
    source = _mapping(authority.get("source_recording"), "C7B_SOURCE_AUTHORITY_INVALID")
    stale = _mapping(authority.get("stale_record"), "C7B_SOURCE_AUTHORITY_INVALID")
    actual = _mapping(authority.get("actual_recut"), "C7B_SOURCE_AUTHORITY_INVALID")
    provenance_ref = _mapping(authority.get("provenance"), "C7B_SOURCE_AUTHORITY_INVALID")
    if (
        private_ref != {"path": _PRIVATE_AUTHORITY.as_posix(), "sha256": private_sha.removeprefix(_SHA)}
        or private.get("candidate_id") != CANDIDATE_ID or private.get("recording_date") != RECORDING_DATE
        or source != {"basename": "22966160_20260814-13-00-40.mp4", "sha256": "660f609ca46cf9b6a5d7618df290ffd8cf32e54677813be343067719d8616b54", "absolute_interval_ms": [191190, 303140]}
        or stale.get("path") != f"replacement_recuts/{CANDIDATE_ID}.record.json"
        or stale.get("sha256") != getattr(record_binding, "sha256", "").removeprefix(_SHA)
        or stale.get("declared_video_sha256") != str(_mapping(record.get("artifact_hashes"), "C7B_SOURCE_RECORD_INVALID").get("video_sha256", "")).removeprefix(_SHA)
        or stale.get("declared_video_sha256") != "19c4bfa64f0f3bed347b992b4059f48b6d92917308cb189b16d99362caab9068"
        or actual.get("path") != f"replacement_recuts/{CANDIDATE_ID}.recut.mp4"
        or _plain_sha(actual.get("sha256"), "C7B_SOURCE_AUTHORITY_INVALID") != "09c42e6cab8b35891f7b307dcfd4e23323073f30915ffc36bcbe2f33acdb1798"
        or getattr(actual_binding, "sha256", "").removeprefix(_SHA) != actual.get("sha256")
        or provenance_ref.get("path") != f"replacement_recuts/{CANDIDATE_ID}.recut.provenance.json"
        or provenance_ref.get("sha256") != getattr(provenance_binding, "sha256", "").removeprefix(_SHA)
        or provenance_ref.get("output_sha256") != actual.get("sha256")
        or provenance_ref.get("padded_sha256") != getattr(padded_binding, "sha256", "").removeprefix(_SHA)
        or provenance_ref.get("source_interval_ms") != [200940, 281830]
        or provenance_ref.get("technical_local_interval_ms") != [9750, 90640]
        or provenance_ref.get("record_local_interval_ms") != [9750, 63920]
    ):
        raise C7bSourceReconciliationError("C7B_SOURCE_BINDING_DRIFT")
    final = _mapping(provenance.get("final_recut"), "C7B_SOURCE_PROVENANCE_INVALID")
    boundary = _mapping(record.get("boundary_audit"), "C7B_SOURCE_RECORD_INVALID")
    if (
        final.get("output_sha256") != actual["sha256"] or final.get("source_sha256") != provenance_ref["padded_sha256"]
        or final.get("absolute_source_start_ms") != 200940 or final.get("absolute_source_end_ms") != 281830
        or final.get("start_ms") != 9750 or final.get("end_ms") != 90640
        or record.get("duration_ms") != 54170 or boundary.get("final_start_ms") != 9750 or boundary.get("final_end_ms") != 63920
    ):
        raise C7bSourceReconciliationError("C7B_SOURCE_PROVENANCE_DRIFT")
    actual_path = Path(getattr(actual_binding, "path", ""))
    if actual_path.name != f"{CANDIDATE_ID}.recut.mp4":
        raise C7bSourceReconciliationError("C7B_SOURCE_ACTUAL_RECUT_DRIFT")
    return C7bSourceReconciliation(_SHA + actual["sha256"], actual_path, 9750, 90640)


def resolve_c7b_delivery_end_clamp(
    *, repo_root: Path, candidate_id: str, recording_date: str | None,
    record_sha256: str, staged_media_sha256: str, final_start_ms: int,
    final_end_ms: int, source_ordinal: int, text: str, source_start_ms: int,
    source_end_ms: int,
) -> tuple[int, int] | None:
    """Authorize the one recorded C7b terminal display-time clamp."""

    if (candidate_id, recording_date) != (CANDIDATE_ID, RECORDING_DATE):
        return None
    authority, _authority_sha = _read_repo_document(repo_root, _ASSET)
    unsigned = dict(authority)
    if unsigned.pop("canonical_self_sha256", None) != _canonical(unsigned):
        raise C7bSourceReconciliationError("C7B_SOURCE_AUTHORITY_SEAL_DRIFT")
    clamp = _mapping(authority.get("delivery_end_clamp"), "C7B_SOURCE_AUTHORITY_INVALID")
    if clamp != {
        "source_ordinal": 27, "source_interval_ms": [62380, 64180],
        "delivery_end_ms": 63920, "text": "泡泡机，泡泡机、泡泡机",
    } or (
        record_sha256 != "sha256:b875d8ddedaa47971249e3af057b218f45e62fb002ddcf6c9733cd38d5bfd8f5"
        or staged_media_sha256 != "sha256:09c42e6cab8b35891f7b307dcfd4e23323073f30915ffc36bcbe2f33acdb1798"
        or (final_start_ms, final_end_ms) != (9750, 63920)
        or (source_ordinal, text, source_start_ms, source_end_ms) != (27, clamp["text"], 62380, 64180)
    ):
        raise C7bSourceReconciliationError("C7B_SOURCE_DELIVERY_END_CLAMP_DRIFT")
    return source_start_ms - final_start_ms, final_end_ms - final_start_ms


def resolve_c7b_delivery_start_clamp(**kwargs: object) -> tuple[int, int] | None:
    """Authorize C7b's one already-recorded leading display-time clamp."""
    if (kwargs.get("candidate_id"), kwargs.get("recording_date")) != (CANDIDATE_ID, RECORDING_DATE):
        return None
    authority, _ = _read_repo_document(Path(str(kwargs["repo_root"])), _ASSET)
    unsigned = dict(authority)
    if unsigned.pop("canonical_self_sha256", None) != _canonical(unsigned):
        raise C7bSourceReconciliationError("C7B_SOURCE_AUTHORITY_SEAL_DRIFT")
    clamp = _mapping(authority.get("delivery_start_clamp"), "C7B_SOURCE_AUTHORITY_INVALID")
    if clamp != {"source_ordinal": 5, "source_interval_ms": [8630, 12230], "delivery_start_ms": 9750, "text": "呵呵，李豆沙还是太好了"} or (
        kwargs.get("record_sha256") != "sha256:b875d8ddedaa47971249e3af057b218f45e62fb002ddcf6c9733cd38d5bfd8f5"
        or kwargs.get("staged_media_sha256") != "sha256:09c42e6cab8b35891f7b307dcfd4e23323073f30915ffc36bcbe2f33acdb1798"
        or (kwargs.get("final_start_ms"), kwargs.get("final_end_ms")) != (9750, 63920)
        or (kwargs.get("source_ordinal"), kwargs.get("text"), kwargs.get("source_start_ms"), kwargs.get("source_end_ms")) != (5, clamp["text"], 8630, 12230)
    ):
        raise C7bSourceReconciliationError("C7B_SOURCE_DELIVERY_START_CLAMP_DRIFT")
    return 0, int(kwargs["source_end_ms"]) - int(kwargs["final_start_ms"])
