#!/usr/bin/env python3
"""Crash-safe containment for the 2026-07-09 background-playback false green.

This is deliberately a date/incident-specific tool, not a general state editor.
It consumes a *fresh* negative selector ``summary.json`` and refuses to proceed
unless that result proves all of the following:

* the run was no-upload;
* its candidate, source bytes, source duration, and seed range are the pinned
  2026-07-09 incident retry, not merely an operator-named negative;
* the candidate was blocked as background/original playback and not Li Dousha;
* the song repair gate retained the same hard negative;
* no recut, cover, delivery, or upload artifact was materialized; and
* the upload ledger/process/artifact checks are clean for this run specifically.

The six existing authority files are compare-and-swapped as one roll-forward
transaction.  The repaired state is installed into ``.json.bak`` first and the
active state is the final commit marker.  A durable PREPARED journal plus staged
bytes makes a crash recoverable with ``--recover``; recovery never restores the
known false-green backup.

The default is a read-only plan.  ``--apply`` is required to create a transaction
and mutate files.  In either mode the autoslice DISABLED file must exist and this
tool itself holds ``runner.lock`` for the complete operation.
"""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


DATE = "2026-07-09"
TARGET_CANDIDATE_ID = "song_223019_166"
INCIDENT_RERUN_CANDIDATE_ID = "song_223019_166_mebukutoki_rerun_v4"
INCIDENT_SEGMENT_PATH = (
    "/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/2026-07-09/"
    "22966160_2026-07-09-22-30-19-.mp4"
)
INCIDENT_ANCHOR_START_MS = 166_220
INCIDENT_ANCHOR_END_MS = 321_760
INCIDENT_SEGMENT_DURATION_MS = 483_352
INCIDENT_RETRY_START_MS = 121_220
INCIDENT_RETRY_END_MS = 366_760
INCIDENT_FULL_SOURCE_DURATION_MS = 245_566
INCIDENT_LOCAL_ANCHOR_START_MS = INCIDENT_ANCHOR_START_MS - INCIDENT_RETRY_START_MS
INCIDENT_LOCAL_ANCHOR_END_MS = INCIDENT_ANCHOR_END_MS - INCIDENT_RETRY_START_MS
INCIDENT_FULL_SOURCE_RELATIVE = Path(
    f"out/{DATE}/{INCIDENT_RERUN_CANDIDATE_ID}/"
    f"{INCIDENT_RERUN_CANDIDATE_ID}_full_source.mp4"
)
INCIDENT_FULL_SOURCE_SHA256 = "706efcc51cfa37d2d3ce73cac5ea42ffe3c35a2c0654387428b20a7bf0b8e48c"
TAIL_MARKER = b"## 2026-07-10 Ivan \xe5\xae\xa1\xe7\x89\x87\xe7\x82\xb9\xe5\x90\x8d\xe6\x89\xa7\xe8\xa1\x8c"
REQUIRED_REASONS = {
    "SONG_BACKGROUND_PLAYBACK_ONLY",
    "SONG_NOT_LIDOUSHA_SINGING",
}
BACKGROUND_MODE = "ORIGINAL_OR_BACKGROUND_PLAYBACK"
BACKGROUND_REJECTION_MODES = frozenset(
    {BACKGROUND_MODE, "STREAMER_TALKING_OVER_MUSIC"}
)
V5_STATUS = "NEGATIVE_ACCEPTANCE_PASSED_NO_UPLOAD"
REVOCATION_STATUS = "REVOKED_FALSE_GREEN_BACKGROUND_PLAYBACK"
NO_UPLOAD_SNAPSHOT_SCHEMA = "autoslice-no-upload-preflight.v1"

AUTHORITY_TARGET_ORDER = (
    "state_backup",
    "v4_report",
    "superseded_v4_report",
    "latest_report",
    "summary",
    "active_state",
)
UPLOAD_MARKER_SUFFIXES = (
    ".publish.json",
    ".upload_manifest.json",
    ".uploaded.json",
    ".public_verify.json",
    ".authorized-upload.json",
)
UPLOAD_PROCESS_MARKERS = ("authorized_upload.py", "do_upload.sh", "biliup")


class RepairError(RuntimeError):
    """A fail-closed validation, CAS, or recovery error."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write(path: Path, payload: bytes, *, exclusive: bool = False) -> None:
    """Write bytes durably; exclusive writes are used for immutable evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def _open_directory_nofollow(path: Path) -> int:
    """Open an absolute directory one component at a time without symlinks."""

    absolute = _lexical_absolute(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(component, flags | nofollow, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


@contextmanager
def pinned_directories(paths: Sequence[Path]) -> Iterator[dict[Path, int]]:
    """Pin directory inodes so target writes cannot follow a swapped parent."""

    opened: dict[Path, int] = {}
    try:
        for raw_path in sorted({_lexical_absolute(path) for path in paths}, key=str):
            try:
                opened[raw_path] = _open_directory_nofollow(raw_path)
            except OSError as exc:
                raise RepairError(f"cannot pin non-symlink directory {raw_path}: {exc}") from exc
        yield opened
    finally:
        for fd in opened.values():
            os.close(fd)


def _read_regular_at(parent_fd: int, name: str, label: str) -> bytes:
    if Path(name).name != name:
        raise RepairError(f"unsafe pinned filename for {label}: {name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise RepairError(f"cannot open pinned {label}: {name}: {exc}") from exc
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise RepairError(f"pinned {label} is not a regular file: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _hash_regular_at(parent_fd: int, name: str, label: str, *, allow_absent: bool = False) -> str | None:
    try:
        return sha256_bytes(_read_regular_at(parent_fd, name, label))
    except RepairError as exc:
        cause = exc.__cause__
        if allow_absent and isinstance(cause, OSError) and cause.errno == errno.ENOENT:
            return None
        raise


def _atomic_install_at(parent_fd: int, name: str, payload: bytes) -> None:
    """Atomically install into a pinned directory inode using *at syscalls."""

    if Path(name).name != name:
        raise RepairError(f"unsafe pinned install filename: {name}")
    tmp_name = f".{name}.repair-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_name, flags, 0o600, dir_fd=parent_fd)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except Exception:
        try:
            os.unlink(tmp_name, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _is_regular_no_symlink(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def require_regular_file(path: Path, label: str) -> None:
    if not _is_regular_no_symlink(path):
        raise RepairError(f"{label} must be an existing regular non-symlink file: {path}")


def require_within(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise RepairError(f"{label} escapes its authority root: {path} not under {root}") from exc


def _lexical_absolute(path: Path) -> Path:
    """Normalize ``.``/``..`` without following any filesystem symlink."""

    return Path(os.path.abspath(os.fspath(path)))


def require_no_symlink_components(
    path: Path,
    root: Path,
    label: str,
    *,
    leaf_may_be_missing: bool = False,
) -> None:
    """Require a lexical in-root path whose existing components are not links."""

    root_abs = _lexical_absolute(root)
    path_abs = _lexical_absolute(path)
    try:
        relative = path_abs.relative_to(root_abs)
    except ValueError as exc:
        raise RepairError(f"{label} lexically escapes {root_abs}: {path_abs}") from exc
    components = (root_abs, *(root_abs / Path(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)))
    for index, component in enumerate(components):
        is_leaf = index == len(components) - 1
        try:
            mode = component.lstat().st_mode
        except OSError as exc:
            if leaf_may_be_missing and is_leaf:
                return
            raise RepairError(f"{label} component is missing/unreadable: {component}") from exc
        if stat.S_ISLNK(mode):
            raise RepairError(f"{label} contains a symlink component: {component}")


def _open_lock_file(path: Path, label: str) -> int:
    """Open/create a regular lock below a pinned, non-symlink parent."""

    parent_fd = _open_directory_nofollow(path.parent)
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        try:
            opened = os.fstat(fd)
            visible = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != visible.st_dev
                or opened.st_ino != visible.st_ino
            ):
                raise RepairError(f"{label} is not the visible regular lock inode: {path}")
            return fd
        except Exception:
            os.close(fd)
            raise
    finally:
        os.close(parent_fd)


@contextmanager
def runner_lock(base: Path) -> Iterator[None]:
    disabled = base / "DISABLED"
    require_regular_file(disabled, "kill switch")
    lock_path = base / "runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _open_lock_file(lock_path, "runner lock")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RepairError(f"runner lock is busy: {lock_path}") from exc
        # Re-check after acquiring the lock so a concurrent operator cannot
        # remove the kill switch between preflight and mutation.
        require_regular_file(disabled, "kill switch")
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def upload_lock(base: Path) -> Iterator[None]:
    """Exclude the manifest-bound uploader through no-upload COMMITTED.

    ``scripts/authorized_upload.py upload`` must take this same lock over its
    verify -> uploader subprocess -> ledger append critical section.  Taking it
    here closes the otherwise unavoidable gap between the final ledger check
    and the durable COMMITTED journal.
    """

    lock_path = base / "upload.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _open_lock_file(lock_path, "authorized uploader lock")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RepairError(f"authorized uploader lock is busy: {lock_path}") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_reason_set(value: object) -> set[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return set()
    return {item for item in value if isinstance(item, str)}


def _walk_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _walk_strings(item)


def _validate_background_performance(
    performance: Mapping[str, Any],
    *,
    observations: object,
    first_lyric_start_ms: int,
    last_lyric_end_ms: int,
) -> None:
    """Validate the fresh negative with the production singer contract.

    This incident tool must not carry a private copy of the AGY schema.  The
    producer now derives three aggregate singer claims from every canonical
    lyric row and checks that its three evidence timestamps cover the lyric
    head, middle, and tail.  Reusing that validator keeps containment coupled
    to the exact production contract that minted the negative result.
    """

    from src.autoslice.song_repair import validate_live_performance_observation

    error = validate_live_performance_observation(
        performance,
        first_lyric_start_ms=first_lyric_start_ms,
        last_lyric_end_ms=last_lyric_end_ms,
        observations=observations,
        require_ready=False,
    )
    if error is not None:
        raise RepairError(f"fresh negative live-performance evidence is invalid: {error}")
    if (
        performance.get("mode") not in BACKGROUND_REJECTION_MODES
        or performance.get("same_lidousha_live_singer_across_all_lyrics") is not False
        or performance.get("recorded_or_playback_vocal_present") is not True
    ):
        raise RepairError(
            "fresh negative does not prove recorded/background vocals and exclude "
            "the same live Li-Dousha singer across all lyrics"
        )


def _validate_agy_background_evidence(
    *,
    repair_report_path: Path,
    candidate_id: str,
    expected_source_origin_path: Path,
    expected_source_sha256: str,
    expected_source_duration_ms: int,
    expected_performance: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the report's negative to the one fresh AGY run and its raw rows."""

    from src.autoslice.song_repair import AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION

    agy_root = repair_report_path.parent / "agy_audio_lrc"
    if not agy_root.is_dir():
        raise RepairError(f"fresh negative is missing its AGY audio/LRC evidence directory: {agy_root}")
    manifests = sorted(agy_root.rglob("run.manifest.json"))
    if len(manifests) != 1:
        raise RepairError(f"fresh negative must have exactly one AGY run manifest, observed {len(manifests)}")
    manifest_path = manifests[0]
    require_regular_file(manifest_path, "fresh negative AGY run manifest")
    require_within(manifest_path, agy_root, "fresh negative AGY run manifest")
    manifest, manifest_payload = _load_json_bytes(manifest_path, "fresh negative AGY run manifest")
    artifacts = _as_mapping(manifest.get("artifacts"))
    if (
        manifest.get("schema_version") != "agy-audio-lrc-run.v1"
        or manifest.get("candidate_id") != candidate_id
        or manifest.get("provider") != "agy"
        or manifest.get("model") != "Gemini 3.5 Flash (High)"
        or isinstance(manifest.get("agy_rc"), bool)
        or manifest.get("agy_rc") != 0
        or manifest.get("provider_fallback_used") is not False
        or manifest.get("sandbox") is not True
        or set(artifacts)
        != {
            "source_path",
            "source_sha256",
            "source_duration_ms",
            "lrc_path",
            "lrc_sha256",
            "prompt_path",
            "prompt_sha256",
            "output_path",
            "output_sha256",
            "source_origin_path",
        }
    ):
        raise RepairError("fresh negative AGY run manifest is not a clean bound High-model run")

    try:
        declared_source_origin = Path(str(artifacts["source_origin_path"])).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RepairError("fresh negative AGY source origin is missing or invalid") from exc
    if declared_source_origin != expected_source_origin_path.resolve(strict=True):
        raise RepairError("fresh negative AGY source origin is not the selector source video")

    job_dir = manifest_path.parent
    expected_names = {
        "source_path": "input.mp4",
        "lrc_path": "source.lrc",
        "prompt_path": "prompt.md",
        "output_path": "alignment.json",
    }
    bound_paths: dict[str, Path] = {}
    bound_payloads: dict[str, bytes] = {}
    for path_key, expected_name in expected_names.items():
        artifact_path = Path(str(artifacts.get(path_key) or ""))
        require_regular_file(artifact_path, f"fresh negative AGY {path_key}")
        require_within(artifact_path, job_dir, f"fresh negative AGY {path_key}")
        if artifact_path.name != expected_name:
            raise RepairError(f"fresh negative AGY {path_key} has an unexpected filename")
        hash_key = path_key.replace("_path", "_sha256")
        if path_key == "source_path":
            artifact_sha256 = sha256_file(artifact_path)
        else:
            payload = artifact_path.read_bytes()
            bound_payloads[path_key] = payload
            artifact_sha256 = sha256_bytes(payload)
        if artifact_sha256 != artifacts.get(hash_key):
            raise RepairError(f"fresh negative AGY {path_key} hash does not match its manifest")
        if path_key == "source_path" and artifact_sha256 != expected_source_sha256:
            raise RepairError(
                "fresh negative AGY input media is not the immutable incident full-source bytes: "
                f"expected {expected_source_sha256}, observed {artifact_sha256}"
            )
        bound_paths[path_key] = artifact_path
    source_duration_ms = artifacts.get("source_duration_ms")
    if isinstance(source_duration_ms, bool) or not isinstance(source_duration_ms, int) or source_duration_ms <= 0:
        raise RepairError("fresh negative AGY source duration is invalid")
    if source_duration_ms != expected_source_duration_ms:
        raise RepairError(
            "fresh negative AGY source duration is not the immutable incident full-source duration: "
            f"expected {expected_source_duration_ms}, observed {source_duration_ms}"
        )

    raw_payload = bound_payloads["output_path"]
    try:
        raw = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RepairError("fresh negative AGY alignment is not valid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "record",
        "observations",
        "spot_checks",
        "live_performance",
        "post_song_talk_start_ms",
    }:
        raise RepairError("fresh negative AGY alignment top-level schema is invalid")
    if raw.get("schema_version") != AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION:
        raise RepairError("fresh negative AGY alignment schema version is stale")
    record = _as_mapping(raw.get("record"))
    if (
        set(record)
        != {"attempt_id", "candidate_id", "source_sha256", "lrc_sha256", "source_duration_ms"}
        or record.get("candidate_id") != candidate_id
        or record.get("attempt_id") != manifest.get("attempt_id")
        or record.get("source_sha256") != artifacts.get("source_sha256")
        or record.get("lrc_sha256") != artifacts.get("lrc_sha256")
        or record.get("source_duration_ms") != source_duration_ms
    ):
        raise RepairError("fresh negative AGY alignment record is not bound to the run manifest")
    raw_performance = _as_mapping(raw.get("live_performance"))
    if dict(raw_performance) != dict(expected_performance):
        raise RepairError("fresh negative report performance differs from raw AGY alignment")
    observations = raw.get("observations")
    if not isinstance(observations, list) or len(observations) < 8:
        raise RepairError("fresh negative AGY alignment lacks canonical lyric observations")
    heard_bounds: list[tuple[int, int]] = []
    for index, row in enumerate(observations):
        if not isinstance(row, Mapping) or row.get("heard") is not True:
            raise RepairError(f"fresh negative AGY lyric row {index} was not affirmatively heard")
        start_ms = row.get("live_start_ms")
        end_ms = row.get("live_end_ms")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or not 0 <= start_ms < end_ms <= source_duration_ms
        ):
            raise RepairError(f"fresh negative AGY lyric row {index} has invalid timing")
        heard_bounds.append((start_ms, end_ms))
    first_lyric_start_ms = heard_bounds[0][0]
    last_lyric_end_ms = heard_bounds[-1][1]
    if any(
        current[0] <= previous[0]
        for previous, current in zip(heard_bounds, heard_bounds[1:])
    ):
        raise RepairError("fresh negative AGY lyric starts are not strictly increasing")
    _validate_background_performance(
        raw_performance,
        observations=observations,
        first_lyric_start_ms=first_lyric_start_ms,
        last_lyric_end_ms=last_lyric_end_ms,
    )
    return {
        "run_manifest_path": str(manifest_path.resolve()),
        "run_manifest_sha256": sha256_bytes(manifest_payload),
        "raw_alignment_path": str(bound_paths["output_path"].resolve()),
        "raw_alignment_sha256": sha256_bytes(raw_payload),
        "source_duration_ms": source_duration_ms,
        "first_lyric_start_ms": first_lyric_start_ms,
        "last_lyric_end_ms": last_lyric_end_ms,
        "forensic_payloads": {
            "agy-run.manifest.json": manifest_payload,
            "agy-alignment.json": raw_payload,
            "agy-source.lrc": bound_payloads["lrc_path"],
            "agy-prompt.md": bound_payloads["prompt_path"],
        },
    }


def _load_json(path: Path, label: str) -> dict[str, Any]:
    value, _payload = _load_json_bytes(path, label)
    return value


def _load_json_bytes(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    require_regular_file(path, label)
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RepairError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RepairError(f"{label} must contain a JSON object: {path}")
    return value, payload


def _select_negative_record(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    records = summary.get("records")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], Mapping):
        raise RepairError("negative selector summary must contain exactly one record")
    return records[0]


def _forbidden_materialization(record: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    for key in (
        "materialized_recut",
        "cover_path",
        "cover",
        "delivered",
        "delivered_sidecars",
        "upload_manifest",
        "publish_manifest",
    ):
        if record.get(key):
            violations.append(key)
    return violations


def _validate_incident_source_context_job(
    job: Mapping[str, Any], *, incident: Mapping[str, Any]
) -> None:
    """Require the selector seed to be the immutable incident retry window.

    The operator-supplied run directory and a self-consistent AGY result are
    not incident identity.  Identity comes from the verified v4 report plus
    the pinned full-source artifact.  In particular, another background-song
    negative must not be able to revoke the 2026-07-09 false green.
    """

    if (
        job.get("schema_version") != "source-context-job-from-full-session-candidate.v1"
        or job.get("candidate_id") != incident["rerun_candidate_id"]
        or job.get("content_type_hint") != "song"
        or job.get("song_candidate") is not True
        or job.get("requires_full_source_song_boundary_redo") is not True
    ):
        raise RepairError("fresh negative source-context job is not the immutable incident song seed")
    timeline = _as_mapping(job.get("timeline"))
    expected_timeline = {
        "source_duration_ms": incident["full_source_duration_ms"],
        "anchor_start_ms": incident["local_anchor_start_ms"],
        "anchor_end_ms": incident["local_anchor_end_ms"],
        "context_start_ms": 0,
        "context_end_ms": incident["full_source_duration_ms"],
        "context_duration_ms": incident["full_source_duration_ms"],
    }
    mismatches = {
        key: {"expected": expected, "observed": timeline.get(key)}
        for key, expected in expected_timeline.items()
        if isinstance(timeline.get(key), bool) or timeline.get(key) != expected
    }
    if mismatches:
        raise RepairError(f"fresh negative seed timeline is not the incident retry range: {mismatches}")


def validate_negative_result(
    path: Path,
    *,
    incident: Mapping[str, Any],
    expected_source_sha256: str | None,
) -> dict[str, Any]:
    summary, summary_payload = _load_json_bytes(path, "fresh negative selector result")
    if summary.get("schema_version") != "full-session-selector-cpa-shadow-run.v1":
        raise RepairError("fresh negative selector result has the wrong schema version")
    if summary.get("no_upload") is not True:
        raise RepairError("fresh negative selector result must have no_upload=true")
    record = _select_negative_record(summary)
    candidate_id = str(record.get("candidate_id") or "")
    if candidate_id != incident["rerun_candidate_id"]:
        raise RepairError(
            "fresh negative candidate id is not the verified incident rerun candidate: "
            f"expected {incident['rerun_candidate_id']}, observed {candidate_id or '<missing>'}"
        )
    if record.get("decision_action") != "BLOCK":
        raise RepairError("fresh negative record is not decision_action=BLOCK")
    reasons = _as_reason_set(record.get("reason_codes"))
    if not REQUIRED_REASONS.issubset(reasons):
        raise RepairError(f"fresh negative reasons missing {sorted(REQUIRED_REASONS - reasons)}")
    if _forbidden_materialization(record):
        raise RepairError(f"fresh negative record materialized forbidden fields: {_forbidden_materialization(record)}")
    boundary = _as_mapping(record.get("boundary_resolution"))
    if boundary.get("action") != "BLOCK":
        raise RepairError("fresh negative boundary_resolution.action must be BLOCK")
    job = _as_mapping(record.get("source_context_job"))
    _validate_incident_source_context_job(job, incident=incident)
    gate = _as_mapping(job.get("song_repair_gate"))
    performance = _as_mapping(gate.get("live_performance"))
    if gate.get("status") != "BLOCKED":
        raise RepairError("fresh negative song_repair_gate.status must be BLOCKED")
    if performance.get("mode") not in BACKGROUND_REJECTION_MODES:
        raise RepairError(
            "fresh negative live-performance mode is not a recorded-vocal incident rejection: "
            f"expected one of {sorted(BACKGROUND_REJECTION_MODES)}, observed {performance.get('mode')}"
        )
    gate_reasons = _as_reason_set(gate.get("reason_codes"))
    if not REQUIRED_REASONS.issubset(gate_reasons):
        raise RepairError("fresh negative song repair gate lost the two hard performer reasons")
    candidate_dir = Path(str(record.get("candidate_dir") or ""))
    if not candidate_dir.is_dir():
        raise RepairError(f"fresh negative candidate_dir is missing: {candidate_dir}")
    if candidate_dir.resolve().parent != path.resolve().parent or candidate_dir.name != candidate_id:
        raise RepairError("fresh negative candidate_dir is not summary.parent/<candidate_id>")
    repair_report_path = Path(str(gate.get("repair_report_path") or ""))
    require_within(repair_report_path, candidate_dir, "fresh negative song repair report")
    if repair_report_path.name != f"{candidate_id}.song-repair.json":
        raise RepairError("fresh negative song repair report filename is not bound to the run candidate id")
    repair_report, repair_report_payload = _load_json_bytes(
        repair_report_path, "fresh negative song repair report"
    )
    if repair_report.get("schema_version") != "song-repair-report.v1" or repair_report.get("repaired") is not False:
        raise RepairError("fresh negative song repair report is not an unrepaired v1 report")
    if repair_report.get("song_boundary") is not None or repair_report.get("lyrics_alignment") is not None:
        raise RepairError("background rejection must not carry a song boundary or lyrics alignment claim")
    if not REQUIRED_REASONS.issubset(_as_reason_set(repair_report.get("reason_codes"))):
        raise RepairError("fresh negative song repair report lost the two hard performer reasons")
    report_performance = _as_mapping(repair_report.get("live_performance"))
    if report_performance.get("mode") not in BACKGROUND_REJECTION_MODES:
        raise RepairError("fresh negative song repair report is not bound to recorded-vocal playback")
    if dict(report_performance) != dict(performance):
        raise RepairError("fresh negative gate/report live-performance observations differ")
    source_path = Path(str(summary.get("source_video") or ""))
    require_regular_file(source_path, "fresh negative source video")
    require_no_symlink_components(
        source_path,
        Path(str(incident["base_path"])),
        "fresh negative incident full-source video",
    )
    if source_path.resolve() != Path(str(incident["full_source_path"])).resolve(strict=False):
        raise RepairError(
            "fresh negative source video is not the immutable incident full-source artifact: "
            f"expected {incident['full_source_path']}, observed {source_path.resolve()}"
        )
    source_sha = sha256_file(source_path)
    if source_sha != incident["full_source_sha256"]:
        raise RepairError(
            "fresh negative source hash is not the immutable incident full-source hash: "
            f"expected {incident['full_source_sha256']}, observed {source_sha}"
        )
    if (
        expected_source_sha256
        and expected_source_sha256.removeprefix("sha256:") != incident["full_source_sha256"]
    ):
        raise RepairError(
            "operator source-hash assertion disagrees with the immutable incident spec; "
            "the command-line value is not an authority"
        )
    agy_evidence = _validate_agy_background_evidence(
        repair_report_path=repair_report_path,
        candidate_id=candidate_id,
        expected_source_origin_path=source_path,
        expected_source_sha256=str(incident["full_source_sha256"]),
        expected_source_duration_ms=int(incident["full_source_duration_ms"]),
        expected_performance=performance,
    )
    inner = _as_mapping(summary.get("last_shadow_summary"))
    inner_records = inner.get("records")
    if isinstance(inner_records, list):
        for index, item in enumerate(inner_records):
            if isinstance(item, Mapping) and _forbidden_materialization(item):
                raise RepairError(
                    f"fresh negative inner record {index} materialized forbidden fields: {_forbidden_materialization(item)}"
                )
    if not isinstance(inner_records, list) or len(inner_records) != 1 or not isinstance(inner_records[0], Mapping):
        raise RepairError("fresh negative inner shadow summary must contain exactly one record")
    inner_record = inner_records[0]
    if (
        inner_record.get("decision_action") != "BLOCK"
        or not REQUIRED_REASONS.issubset(_as_reason_set(inner_record.get("reason_codes")))
        or _as_mapping(inner_record.get("boundary_resolution")).get("action") != "BLOCK"
        or _as_mapping(inner_record.get("source_context_job")).get("song_repair_gate") != gate
    ):
        raise RepairError("fresh negative inner/outer BLOCK evidence does not agree")
    # Only the fresh repair result may set this field.  Do not inherit a stale
    # job-manifest alignment that failed verification and merely survived next
    # to song_repair_gate.  Current background rejection exits before minting a
    # READY alignment, so this is normally false.
    lyrics_alignment_ready = False
    return {
        "summary": summary,
        "record": record,
        "candidate_id": candidate_id,
        "reason_codes": sorted(reasons),
        "performance": dict(performance),
        "repair_report_path": str(repair_report_path.resolve()),
        "repair_report_sha256": sha256_bytes(repair_report_payload),
        "repair_report_bytes": repair_report_payload,
        "result_path": str(path.resolve()),
        "result_sha256": sha256_bytes(summary_payload),
        "result_bytes": summary_payload,
        "source_video_path": str(source_path.resolve()),
        "source_video_sha256": source_sha,
        "candidate_dir": str(candidate_dir.resolve()),
        "lyrics_alignment_ready": lyrics_alignment_ready,
        "agy_evidence": agy_evidence,
        "incident_binding": {
            key: copy.deepcopy(incident[key])
            for key in (
                "segment_path",
                "start_ms",
                "end_ms",
                "original_anchor_start_ms",
                "original_anchor_end_ms",
                "segment_duration_ms",
                "candidate_id",
                "rerun_candidate_id",
                "full_source_path",
                "full_source_sha256",
                "full_source_duration_ms",
                "local_anchor_start_ms",
                "local_anchor_end_ms",
            )
        },
    }


def _valid_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _parse_upload_ledger_events(
    raw: bytes,
    *,
    label: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse the durable two-phase upload journal and reject ambiguity.

    Rows written before the two-phase protocol have no ``event`` key and are
    retained as legacy terminal records.  Any row that opts into the new
    protocol must be exact enough to pair STARTED -> FINISHED by attempt_id.
    """

    if raw and not raw.endswith(b"\n"):
        raise RepairError(f"{label} has a partial final row")
    rows: list[dict[str, Any]] = []
    row_hashes: list[str] = []
    started: dict[str, dict[str, Any]] = {}
    finished: set[str] = set()
    common_fields = (
        "artifact_id",
        "video_sha256",
        "cover_sha256",
        "manifest",
        "manifest_sha256",
        "uploader",
    )
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise RepairError(f"{label} row {line_no} is empty")
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise RepairError(f"{label} row {line_no} is invalid JSON") from exc
        if not isinstance(row, dict):
            raise RepairError(f"{label} row {line_no} is not a JSON object")
        rows.append(row)
        row_hashes.append(sha256_bytes(line))
        event = row.get("event")
        if event is None:
            # Historical terminal rows predate the two-phase journal.  They
            # remain eligible for duplicate/run-ref scanning below.
            continue
        if event not in {"UPLOAD_ATTEMPT_STARTED", "UPLOAD_ATTEMPT_FINISHED"}:
            raise RepairError(f"{label} row {line_no} has unknown upload event {event!r}")
        attempt_id = row.get("attempt_id")
        if (
            not _valid_hex(attempt_id, 32)
            or not isinstance(row.get("artifact_id"), str)
            or not str(row.get("artifact_id")).strip()
            or not _valid_hex(row.get("video_sha256"), 64)
            or not _valid_hex(row.get("cover_sha256"), 64)
            or not isinstance(row.get("manifest"), str)
            or not str(row.get("manifest")).startswith("/")
            or not _valid_hex(row.get("manifest_sha256"), 64)
            or not isinstance(row.get("uploader"), str)
            or not str(row.get("uploader")).strip()
            or not isinstance(row.get("at"), str)
            or not str(row.get("at")).strip()
        ):
            raise RepairError(f"{label} row {line_no} has a malformed two-phase upload event")
        assert isinstance(attempt_id, str)
        if event == "UPLOAD_ATTEMPT_STARTED":
            if attempt_id in started or attempt_id in finished or "rc" in row:
                raise RepairError(f"{label} row {line_no} duplicates/mangles STARTED {attempt_id}")
            started[attempt_id] = row
            continue
        if attempt_id not in started or attempt_id in finished:
            raise RepairError(f"{label} row {line_no} has an unpaired/duplicate FINISHED {attempt_id}")
        rc = row.get("rc")
        if isinstance(rc, bool) or not isinstance(rc, int):
            raise RepairError(f"{label} row {line_no} FINISHED event has invalid rc")
        if any(row.get(field) != started[attempt_id].get(field) for field in common_fields):
            raise RepairError(f"{label} row {line_no} FINISHED binding differs from STARTED")
        finished.add(attempt_id)
    unresolved = sorted(set(started) - finished)
    if unresolved:
        raise RepairError(
            f"{label} contains unfinished upload intent(s) requiring manual reconciliation: {unresolved}"
        )
    return rows, row_hashes


def _ledger_delta(ledger: Path, before_offset: int) -> tuple[int, list[dict[str, Any]], list[str]]:
    if before_offset < 0:
        raise RepairError("--ledger-before-offset cannot be negative")
    if not ledger.exists():
        if before_offset != 0:
            raise RepairError("upload ledger is absent but the supplied before-offset is non-zero")
        return 0, [], []
    require_regular_file(ledger, "upload ledger")
    raw = ledger.read_bytes()
    if before_offset > len(raw):
        raise RepairError(f"upload ledger shrank below before-offset ({len(raw)} < {before_offset})")
    if before_offset and raw[before_offset - 1 : before_offset] != b"\n":
        raise RepairError("upload ledger before-offset is not on a JSONL row boundary")
    delta = raw[before_offset:]
    rows, row_hashes = _parse_upload_ledger_events(delta, label="upload ledger delta")
    return len(raw), rows, row_hashes


def capture_no_upload_snapshot(
    *,
    base: Path,
    ledger: Path,
    run_id: str,
    run_root: Path,
    source_video: Path,
    output: Path,
) -> dict[str, Any]:
    if not run_id or not all(ch.isalnum() or ch in "-_" for ch in run_id):
        raise RepairError("planned run id is empty or unsafe")
    if run_root.exists():
        raise RepairError(f"planned acceptance run root must not exist before snapshot: {run_root}")
    require_regular_file(source_video, "planned negative source video")
    snapshot_root = (base / "forensics" / "no-upload-snapshots").resolve()
    try:
        output.resolve(strict=False).relative_to(snapshot_root)
    except ValueError as exc:
        raise RepairError(f"no-upload snapshot must be written under {snapshot_root}") from exc
    if output.exists():
        raise RepairError(f"no-upload snapshot already exists: {output}")
    if not ledger.exists():
        # Materialize a durable empty inode while holding upload.lock.  An
        # absent baseline cannot prove that a post-PREPARED ledger was not
        # created, published through, and rotated away before recovery.
        try:
            durable_write(ledger, b"", exclusive=True)
        except OSError as exc:
            raise RepairError(f"cannot create durable empty upload-ledger baseline: {ledger}: {exc}") from exc
    require_regular_file(ledger, "upload ledger")
    raw = ledger.read_bytes()
    _parse_upload_ledger_events(raw, label="pre-run upload ledger")
    ledger_stat = ledger.stat()
    ledger_existed = True
    ledger_dev = ledger_stat.st_dev
    ledger_ino = ledger_stat.st_ino
    captured_ns = time.time_ns()
    snapshot = {
        "schema_version": NO_UPLOAD_SNAPSHOT_SCHEMA,
        "captured_at": utc_now(),
        "captured_at_unix_ns": captured_ns,
        "planned_run_id": run_id,
        "planned_run_root": str(run_root.resolve(strict=False)),
        "source_video_path": str(source_video.resolve()),
        "source_video_sha256": sha256_file(source_video),
        "ledger_path": str(ledger.resolve(strict=False)),
        "ledger_existed": ledger_existed,
        "ledger_device": ledger_dev,
        "ledger_inode": ledger_ino,
        "ledger_before_offset": len(raw),
        "ledger_prefix_sha256": sha256_bytes(raw),
    }
    durable_write(output, json_bytes(snapshot), exclusive=True)
    os.chmod(output, 0o400)
    _fsync_dir(output.parent)
    return snapshot


def validate_no_upload_snapshot(
    snapshot_path: Path,
    *,
    negative: Mapping[str, Any],
    ledger: Path,
) -> dict[str, Any]:
    snapshot, snapshot_payload = _load_json_bytes(snapshot_path, "pre-run no-upload snapshot")
    if snapshot.get("schema_version") != NO_UPLOAD_SNAPSHOT_SCHEMA:
        raise RepairError("pre-run no-upload snapshot schema is invalid")
    if snapshot.get("ledger_existed") is not True:
        raise RepairError("pre-run snapshot lacks the required durable upload-ledger inode baseline")
    if snapshot.get("planned_run_id") != negative["candidate_id"]:
        raise RepairError("pre-run no-upload snapshot run id does not match the fresh negative")
    if Path(str(snapshot.get("planned_run_root") or "")).resolve() != Path(str(negative["result_path"])).parent.resolve():
        raise RepairError("pre-run no-upload snapshot run root does not match the fresh negative")
    if (
        Path(str(snapshot.get("source_video_path") or "")).resolve()
        != Path(str(negative["source_video_path"])).resolve()
        or snapshot.get("source_video_sha256") != negative["source_video_sha256"]
    ):
        raise RepairError("pre-run no-upload snapshot source binding does not match the fresh negative")
    if Path(str(snapshot.get("ledger_path") or "")).resolve(strict=False) != ledger.resolve(strict=False):
        raise RepairError("pre-run no-upload snapshot ledger path mismatch")
    result_mtime_ns = Path(str(negative["result_path"])).stat().st_mtime_ns
    captured_ns = snapshot.get("captured_at_unix_ns")
    if (
        isinstance(captured_ns, bool)
        or not isinstance(captured_ns, int)
        or captured_ns >= result_mtime_ns
        or snapshot_path.stat().st_mtime_ns >= result_mtime_ns
    ):
        raise RepairError("no-upload snapshot was not durably captured before the negative run result")
    before_offset = snapshot.get("ledger_before_offset")
    if isinstance(before_offset, bool) or not isinstance(before_offset, int) or before_offset < 0:
        raise RepairError("pre-run no-upload snapshot ledger offset is invalid")
    current = ledger.read_bytes() if ledger.exists() else b""
    _parse_upload_ledger_events(current, label="current upload ledger")
    if len(current) < before_offset or sha256_bytes(current[:before_offset]) != snapshot.get("ledger_prefix_sha256"):
        raise RepairError("upload ledger prefix no longer matches the pre-run snapshot")
    if snapshot.get("ledger_existed") is True:
        if not ledger.exists():
            raise RepairError("upload ledger disappeared after the pre-run snapshot")
        ledger_stat = ledger.stat()
        if ledger_stat.st_dev != snapshot.get("ledger_device") or ledger_stat.st_ino != snapshot.get("ledger_inode"):
            raise RepairError("upload ledger inode changed after the pre-run snapshot")
    return {
        **snapshot,
        "snapshot_path": str(snapshot_path.resolve()),
        "snapshot_sha256": sha256_bytes(snapshot_payload),
    }


def _scan_upload_processes(run_refs: Sequence[str]) -> tuple[str, list[str]]:
    proc = Path("/proc")
    if not proc.is_dir():
        return "UNAVAILABLE_NON_LINUX", []
    matches: list[str] = []
    for cmdline in proc.glob("[0-9]*/cmdline"):
        try:
            command = cmdline.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if not any(marker in command for marker in UPLOAD_PROCESS_MARKERS):
            continue
        if any(ref and ref in command for ref in run_refs):
            matches.append(f"pid={cmdline.parent.name} sha256:{sha256_bytes(command.encode())}")
    return "CHECKED", matches


def _inspect_run_no_upload(
    *, ledger: Path, before_offset: int, run_refs: Sequence[str], output_roots: Sequence[Path]
) -> dict[str, Any]:
    after_offset, rows, row_hashes = _ledger_delta(ledger, before_offset)
    ledger_matches = []
    for index, row in enumerate(rows):
        rendered = json.dumps(row, ensure_ascii=False, sort_keys=True)
        matched_refs = [ref for ref in run_refs if ref and ref in rendered]
        if matched_refs:
            ledger_matches.append({"delta_row": index, "matched_refs": matched_refs})

    marker_paths: list[str] = []
    for output_root in output_roots:
        if not output_root.is_dir():
            continue
        for marker in output_root.rglob("*"):
            if not marker.is_file():
                continue
            lower_name = marker.name.lower()
            delivery_tree = bool({"replacement_recuts", "publish", "delivery"}.intersection(marker.parts))
            forbidden_media = (
                lower_name.endswith(".cover.png")
                or lower_name.endswith(".final.mp4")
                or ("burned" in lower_name and lower_name.endswith(".mp4"))
                or (delivery_tree and marker.suffix.lower() in {".mp4", ".png", ".srt", ".ass"})
            )
            forbidden_json = marker.suffix.lower() == ".json" and any(
                marker.name.endswith(suffix) for suffix in UPLOAD_MARKER_SUFFIXES
            )
            if forbidden_media or forbidden_json:
                marker_paths.append(str(marker))
    marker_paths = sorted(set(marker_paths))
    process_status, process_matches = _scan_upload_processes(run_refs)
    if ledger_matches or marker_paths or process_matches:
        raise RepairError(
            "run-specific no-upload proof failed: "
            f"ledger_matches={ledger_matches}, marker_paths={marker_paths}, process_matches={process_matches}"
        )
    return {
        "ledger_after_offset": after_offset,
        "ledger_appended_rows": len(rows),
        "ledger_appended_row_sha256": row_hashes,
        "ledger_run_reference_matches": [],
        "run_upload_marker_paths": [],
        "upload_process_scan": process_status,
        "upload_process_run_reference_matches": [],
    }


def build_no_upload_proof(
    negative: Mapping[str, Any], ledger: Path, snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    candidate_id = str(negative["candidate_id"])
    candidate_dir = str(negative["candidate_dir"])
    result_path = str(negative["result_path"])
    run_refs = [
        candidate_id,
        candidate_dir,
        result_path,
        str(negative["result_sha256"]),
        str(negative["source_video_path"]),
        str(negative["source_video_sha256"]),
    ]
    output_roots = sorted({Path(candidate_dir), Path(result_path).parent})
    before_offset = int(snapshot["ledger_before_offset"])
    inspection = _inspect_run_no_upload(
        ledger=ledger,
        before_offset=before_offset,
        run_refs=run_refs,
        output_roots=output_roots,
    )
    require_regular_file(ledger, "transaction-time upload ledger")
    checkpoint_payload = ledger.read_bytes()
    _parse_upload_ledger_events(checkpoint_payload, label="transaction-time upload ledger")
    checkpoint_stat = ledger.stat()
    if (
        checkpoint_stat.st_dev != snapshot["ledger_device"]
        or checkpoint_stat.st_ino != snapshot["ledger_inode"]
    ):
        raise RepairError("upload ledger inode changed before the transaction checkpoint")
    return {
        "status": "PASSED_RUN_SCOPED",
        "run_id": candidate_id,
        "result_path": result_path,
        "result_sha256": negative["result_sha256"],
        "candidate_dir": candidate_dir,
        "source_video_path": negative["source_video_path"],
        "source_video_sha256": negative["source_video_sha256"],
        "pre_run_snapshot_path": snapshot["snapshot_path"],
        "pre_run_snapshot_sha256": snapshot["snapshot_sha256"],
        "pre_run_snapshot_captured_at": snapshot["captured_at"],
        "run_refs": run_refs,
        "output_roots": [str(path) for path in output_roots],
        "ledger_path": str(ledger.resolve()) if ledger.exists() else str(ledger.absolute()),
        "ledger_before_offset": before_offset,
        "ledger_prefix_sha256": snapshot["ledger_prefix_sha256"],
        "ledger_existed": snapshot["ledger_existed"],
        "ledger_device": snapshot["ledger_device"],
        "ledger_inode": snapshot["ledger_inode"],
        "transaction_checkpoint_offset": len(checkpoint_payload),
        "transaction_checkpoint_prefix_sha256": sha256_bytes(checkpoint_payload),
        "transaction_checkpoint_device": checkpoint_stat.st_dev,
        "transaction_checkpoint_inode": checkpoint_stat.st_ino,
        **inspection,
        "scope_note": "unrelated authorized uploader rows/processes are allowed; only this run id/path/hash are rejected",
    }


def revalidate_no_upload_guard(proof: Mapping[str, Any]) -> dict[str, Any]:
    snapshot_path = Path(str(proof.get("pre_run_snapshot_path") or ""))
    snapshot, snapshot_payload = _load_json_bytes(snapshot_path, "pre-run no-upload snapshot")
    if sha256_bytes(snapshot_payload) != proof.get("pre_run_snapshot_sha256"):
        raise RepairError("pre-run no-upload snapshot hash drifted")
    ledger = Path(str(proof.get("ledger_path") or ""))
    before_offset = proof.get("ledger_before_offset")
    if isinstance(before_offset, bool) or not isinstance(before_offset, int):
        raise RepairError("transaction no-upload ledger offset is invalid")
    current = ledger.read_bytes() if ledger.exists() else b""
    _parse_upload_ledger_events(current, label="transaction upload ledger")
    if len(current) < before_offset or sha256_bytes(current[:before_offset]) != proof.get("ledger_prefix_sha256"):
        raise RepairError("upload ledger prefix drifted since the pre-run snapshot")
    if proof.get("ledger_existed") is True:
        if not ledger.exists():
            raise RepairError("upload ledger disappeared during containment")
        ledger_stat = ledger.stat()
        if ledger_stat.st_dev != proof.get("ledger_device") or ledger_stat.st_ino != proof.get("ledger_inode"):
            raise RepairError("upload ledger inode changed during containment")
    checkpoint_offset = proof.get("transaction_checkpoint_offset")
    checkpoint_sha256 = proof.get("transaction_checkpoint_prefix_sha256")
    if (
        isinstance(checkpoint_offset, bool)
        or not isinstance(checkpoint_offset, int)
        or checkpoint_offset < before_offset
        or not _valid_hex(checkpoint_sha256, 64)
        or not ledger.exists()
    ):
        raise RepairError("transaction-time upload-ledger checkpoint is malformed")
    checkpoint_stat = ledger.stat()
    if (
        checkpoint_stat.st_dev != proof.get("transaction_checkpoint_device")
        or checkpoint_stat.st_ino != proof.get("transaction_checkpoint_inode")
        or len(current) < checkpoint_offset
        or sha256_bytes(current[:checkpoint_offset]) != checkpoint_sha256
    ):
        raise RepairError("transaction-time upload-ledger checkpoint drifted")
    if snapshot.get("planned_run_id") != proof.get("run_id"):
        raise RepairError("no-upload snapshot/transaction run id drift")
    run_refs = proof.get("run_refs")
    output_roots = proof.get("output_roots")
    if (
        not isinstance(run_refs, list)
        or not all(isinstance(item, str) and item for item in run_refs)
        or not isinstance(output_roots, list)
    ):
        raise RepairError("transaction no-upload guard is malformed")
    return _inspect_run_no_upload(
        ledger=ledger,
        before_offset=before_offset,
        run_refs=run_refs,
        output_roots=[Path(str(path)) for path in output_roots],
    )


def authority_paths(base: Path, repo_root: Path, superseded_report: Path) -> dict[str, Path]:
    expected_superseded_root = (repo_root / "lidousha" / DATE / "_superseded").resolve()
    if (
        superseded_report.resolve().parent != expected_superseded_root
        or "芽吹くとき" not in superseded_report.name
        or not superseded_report.name.endswith(".manual-rerun-report.json")
    ):
        raise RepairError(
            "--superseded-v4-report must be the exact 2026-07-09 _superseded 芽吹くとき manual report"
        )
    paths = {
        "active_state": base / "state" / f"{DATE}.json",
        "state_backup": base / "state" / f"{DATE}.json.bak",
        "v4_report": base / "reports" / "manual_rerun_2026-07-09_mebukutoki_v4.json",
        "superseded_v4_report": superseded_report,
        "latest_report": base / "reports" / "latest.md",
        "summary": repo_root / "lidousha" / DATE / "AUTOSLICE_SUMMARY.md",
    }
    for name, path in paths.items():
        require_regular_file(path, name)
        require_within(path, base if name not in {"superseded_v4_report", "summary"} else repo_root, name)
    return paths


def read_authority_inputs(paths: Mapping[str, Path]) -> tuple[dict[str, bytes], dict[str, str]]:
    payloads = {name: path.read_bytes() for name, path in paths.items()}
    hashes = {name: sha256_bytes(payload) for name, payload in payloads.items()}
    return payloads, hashes


def validate_known_false_green_inputs(inputs: Mapping[str, bytes], *, repo_root: Path) -> dict[str, Any]:
    """Refuse a path mix-up or a state/report that somebody already repaired."""

    if inputs["v4_report"] != inputs["superseded_v4_report"]:
        raise RepairError("the two pre-repair v4 authority copies are no longer byte-identical")
    try:
        report = json.loads(inputs["v4_report"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RepairError("the pre-repair v4 authority report is not valid UTF-8 JSON") from exc
    if not isinstance(report, Mapping):
        raise RepairError("the pre-repair v4 authority report is not a JSON object")
    try:
        active_state = json.loads(inputs["active_state"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RepairError("the pre-repair active state is not valid UTF-8 JSON") from exc
    if not isinstance(active_state, Mapping):
        raise RepairError("the pre-repair active state is not a JSON object")
    acceptance = _as_mapping(report.get("final_acceptance"))
    result = _as_mapping(report.get("result"))
    item = _as_mapping(report.get("item"))
    if (
        acceptance.get("status") != "ACCEPTED_NO_UPLOAD"
        or acceptance.get("state_repaired") is not True
        or result.get("song_complete") is not True
        or not result.get("delivered")
        or result.get("candidate_id") != INCIDENT_RERUN_CANDIDATE_ID
        or result.get("start_ms") != INCIDENT_RETRY_START_MS
        or result.get("end_ms") != INCIDENT_RETRY_END_MS
        or result.get("retried_full_source") is not True
    ):
        raise RepairError("the selected v4 files no longer have the known false-green acceptance shape")
    # Revoking metadata while leaving its MP4/cover/sidecars in the active
    # delivery root would still expose a false-green package to the Mac pull.
    # Containment is metadata-only by design, so require the previously moved
    # package files to already be absent (their actual _superseded report path
    # is recorded in the compact state tombstone).
    candidate_paths: list[str] = []
    for value in (
        result.get("delivered"),
        *_as_mapping(result.get("delivered_sidecars")).values(),
        _as_mapping(acceptance.get("cover")).get("path"),
        *_as_mapping(acceptance.get("evidence_sidecars")).values(),
    ):
        if isinstance(value, str) and value:
            candidate_paths.append(value)
    delivery_root = repo_root / "lidousha" / DATE
    state_songs = active_state.get("songs")
    state_target = [
        song
        for song in (state_songs if isinstance(state_songs, list) else [])
        if isinstance(song, Mapping) and song.get("candidate_id") == TARGET_CANDIDATE_ID
    ]
    if len(state_target) != 1:
        raise RepairError("pre-repair active state does not have exactly one target false-green song")
    candidate_paths.extend(_walk_strings(state_target[0]))
    still_active = []
    for value in candidate_paths:
        path = Path(value)
        try:
            path.resolve(strict=False).relative_to(delivery_root.resolve(strict=True))
        except (OSError, ValueError):
            continue
        if "_superseded" not in path.parts and path.exists():
            still_active.append(str(path))
    if still_active:
        raise RepairError(f"false-green package files still exist in the active delivery root: {still_active}")
    segment_path = item.get("segment_path")
    start_ms = item.get("anchor_start_ms")
    end_ms = item.get("anchor_end_ms")
    if (
        not isinstance(segment_path, str)
        or segment_path != INCIDENT_SEGMENT_PATH
        or isinstance(start_ms, bool)
        or start_ms != INCIDENT_ANCHOR_START_MS
        or isinstance(end_ms, bool)
        or end_ms != INCIDENT_ANCHOR_END_MS
    ):
        raise RepairError("v4 incident item does not identify the exact original 166220..321760ms source interval")
    segment_duration_ms = item.get("seg_dur_ms")
    if (
        isinstance(segment_duration_ms, bool)
        or segment_duration_ms != INCIDENT_SEGMENT_DURATION_MS
    ):
        raise RepairError(
            "v4 incident seg_dur_ms is not the immutable source-segment duration: "
            f"expected {INCIDENT_SEGMENT_DURATION_MS}, observed {segment_duration_ms}"
        )
    quarantine_start_ms = INCIDENT_RETRY_START_MS
    quarantine_end_ms = INCIDENT_RETRY_END_MS
    return {
        "segment_path": segment_path,
        "start_ms": quarantine_start_ms,
        "end_ms": quarantine_end_ms,
        "original_anchor_start_ms": start_ms,
        "original_anchor_end_ms": end_ms,
        "segment_duration_ms": segment_duration_ms,
        "candidate_id": TARGET_CANDIDATE_ID,
        "rerun_candidate_id": INCIDENT_RERUN_CANDIDATE_ID,
        "base_path": str(repo_root.parent.resolve()),
        "full_source_path": str((repo_root.parent / INCIDENT_FULL_SOURCE_RELATIVE).resolve(strict=False)),
        "full_source_sha256": INCIDENT_FULL_SOURCE_SHA256,
        "full_source_duration_ms": INCIDENT_FULL_SOURCE_DURATION_MS,
        "local_anchor_start_ms": INCIDENT_LOCAL_ANCHOR_START_MS,
        "local_anchor_end_ms": INCIDENT_LOCAL_ANCHOR_END_MS,
        "reason_code": "SONG_INTERVAL_REQUIRES_JOINT_SINGING_PROOF",
    }


def _clean_state_song(old: Mapping[str, Any], negative: Mapping[str, Any]) -> dict[str, Any]:
    preserved = {
        key: copy.deepcopy(old[key])
        for key in ("candidate_id", "segment", "start_ms", "end_ms", "danmaku", "window_classified_song")
        if key in old
    }
    performance = _as_mapping(negative["performance"])
    preserved.update(
        {
            "status": "blocked",
            "decision": "BLOCK",
            "reason_codes": sorted(REQUIRED_REASONS),
            "song_complete": False,
            "lyrics_alignment_ready": bool(negative["lyrics_alignment_ready"]),
            "full_source_performer_rejection": True,
            "preview": "\u80cc\u666f/\u539f\u66f2\u64ad\u653e\uff1b\u65b0\u8d1f\u4f8b\u5224\u5b9a\u975e\u674e\u8c46\u6c99\u73b0\u573a\u6f14\u5531",
            "live_performance_mode": performance.get("mode"),
            "live_performance_confidence": performance.get("confidence"),
            "song_repair_gate": {
                "status": "BLOCKED",
                "reason_codes": sorted(REQUIRED_REASONS),
                "live_performance": copy.deepcopy(performance),
                "repair_report_path": negative["repair_report_path"],
                "repair_report_sha256": negative["repair_report_sha256"],
            },
            "negative_acceptance": {
                "status": V5_STATUS,
                "run_id": negative["candidate_id"],
                "result_path": negative["result_path"],
                "result_sha256": negative["result_sha256"],
                "forensic_result_path": negative["forensic_result_path"],
                "source_video_path": negative["source_video_path"],
                "source_video_sha256": negative["source_video_sha256"],
                "forensic_repair_report_path": negative["forensic_repair_report_path"],
            },
        }
    )
    return preserved


def build_repaired_state(
    state_bytes: bytes,
    *,
    state_input_sha256: str,
    negative: Mapping[str, Any],
    no_upload: Mapping[str, Any],
    incident_interval: Mapping[str, Any],
    txid: str,
    tx_dir: Path,
    superseded_report: Path,
    repaired_at: str,
) -> dict[str, Any]:
    try:
        state = json.loads(state_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RepairError("active state is not valid UTF-8 JSON") from exc
    if not isinstance(state, dict) or state.get("status") != "review_ready":
        raise RepairError("active 2026-07-09 state must still be the review_ready false-green state")
    songs = state.get("songs")
    if not isinstance(songs, list) or len(songs) != 2:
        raise RepairError("incident repair expects exactly two 2026-07-09 song attempts")
    matching = [index for index, item in enumerate(songs) if isinstance(item, Mapping) and item.get("candidate_id") == TARGET_CANDIDATE_ID]
    if len(matching) != 1:
        raise RepairError(f"expected exactly one target song record {TARGET_CANDIDATE_ID}")
    old_song = songs[matching[0]]
    if not old_song.get("delivered") or old_song.get("song_complete") is not True:
        raise RepairError("target song no longer has the known delivered/song_complete false-green shape")
    songs[matching[0]] = _clean_state_song(old_song, negative)
    delivered_talk = sum(
        1
        for pick in state.get("picks", [])
        if isinstance(pick, Mapping) and pick.get("status") in {"ok", "review_ready", "quarantine"}
    )
    if delivered_talk != 5:
        raise RepairError(f"incident repair expects five retained talk deliveries, observed {delivered_talk}")
    if sum(1 for song in songs if isinstance(song, Mapping) and song.get("delivered")) != 0:
        raise RepairError("repaired state still contains a delivered song")
    if sum(1 for song in songs if isinstance(song, Mapping) and song.get("status") == "blocked") != 2:
        raise RepairError("repaired state must contain exactly two blocked songs")

    tombstones = state.get("superseded_song_records")
    if not isinstance(tombstones, list):
        tombstones = []
    tombstones = [
        item
        for item in tombstones
        if not (
            isinstance(item, Mapping)
            and (
                item.get("candidate_id") == TARGET_CANDIDATE_ID
                or "mebukutoki" in str(item.get("candidate_id") or "").lower()
                or "芽吹くとき" in str(item.get("candidate_id") or "")
            )
        )
    ]
    tombstones.append(
        {
            "candidate_id": TARGET_CANDIDATE_ID,
            "disposition": REVOCATION_STATUS,
            "repair_txid": txid,
            "prior_state_sha256": state_input_sha256,
            "forensic_backup_path": str(tx_dir / "backup" / "active_state.bin"),
            "superseded_directory": str(superseded_report.parent),
            "revoked_report_path": str(superseded_report),
        }
    )
    state["superseded_song_records"] = tombstones
    intervals = [
        item
        for item in state.get("song_quarantine_intervals", [])
        if isinstance(item, Mapping)
        and (
            Path(str(item.get("segment_path") or "")).name,
            item.get("original_anchor_start_ms", item.get("start_ms")),
            item.get("original_anchor_end_ms", item.get("end_ms")),
        )
        != (
            Path(str(incident_interval["segment_path"])).name,
            incident_interval["original_anchor_start_ms"],
            incident_interval["original_anchor_end_ms"],
        )
    ]
    intervals.append(
        {
            key: copy.deepcopy(incident_interval[key])
            for key in (
                "segment_path",
                "start_ms",
                "end_ms",
                "original_anchor_start_ms",
                "original_anchor_end_ms",
                "candidate_id",
                "reason_code",
            )
        }
    )
    state["song_quarantine_intervals"] = intervals
    state["songs"] = songs
    state["status"] = "review_ready"
    state["updated_at"] = repaired_at
    state["false_green_containment"] = {
        "status": "REPAIRED_FALSE_GREEN_ACTIVE_STATE_IS_TRANSACTION_COMMIT_MARKER",
        "repair_txid": txid,
        "repaired_at": repaired_at,
        "reason_codes": sorted(REQUIRED_REASONS),
        "negative_result_path": negative["result_path"],
        "negative_result_sha256": negative["result_sha256"],
        "no_upload_verification": copy.deepcopy(no_upload),
    }
    return state


def _render_reports_isolated(repo_root: Path, state: Mapping[str, Any]) -> tuple[bytes, bytes]:
    # Import only here so --help and recovery do not require the runner's full
    # dependency graph.  Both globals are patched: changing just cwd or BASE is
    # insufficient because write_reports writes the summary via REPO_ROOT.
    import scripts.free_session_autoslice as runner

    old_base, old_repo = runner.BASE, runner.REPO_ROOT
    with tempfile.TemporaryDirectory(prefix="repair-20260709-render-") as tmp:
        isolated = Path(tmp)
        try:
            runner.BASE = isolated / "base"
            runner.REPO_ROOT = isolated / "repo"
            runner.write_reports(DATE, copy.deepcopy(dict(state)))
            summary = (runner.REPO_ROOT / "lidousha" / DATE / "AUTOSLICE_SUMMARY.md").read_bytes()
            latest = (runner.BASE / "reports" / "latest.md").read_bytes()
            isolated_repo_bytes = str(runner.REPO_ROOT).encode("utf-8")
            if latest.count(isolated_repo_bytes) != 1:
                raise RepairError("isolated latest report did not contain exactly one generated repo path")
            latest = latest.replace(isolated_repo_bytes, str(repo_root).encode("utf-8"), 1)
        finally:
            runner.BASE, runner.REPO_ROOT = old_base, old_repo
    # Belt-and-suspenders: the live summary must not have been used as output.
    if not repo_root.is_dir():
        raise RepairError(f"repo root disappeared during isolated report rendering: {repo_root}")
    return summary, latest


def preserve_summary_tail(generated: bytes, original: bytes) -> tuple[bytes, str, int]:
    if original.count(TAIL_MARKER) != 1:
        raise RepairError("summary must contain the Ivan manual tail marker exactly once")
    tail = original[original.index(TAIL_MARKER) :]
    if TAIL_MARKER in generated:
        raise RepairError("isolated automatic report unexpectedly contains the manual tail marker")
    return generated.rstrip(b"\n") + b"\n\n" + tail, sha256_bytes(tail), len(tail)


def _report_tombstone(
    *,
    txid: str,
    repaired_at: str,
    original_path: Path,
    original_sha256: str,
    backup_path: Path,
    negative: Mapping[str, Any],
    no_upload: Mapping[str, Any],
    state_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": "autoslice-false-green-revocation.v1",
        "status": REVOCATION_STATUS,
        "repair_txid": txid,
        "revoked_at": repaired_at,
        "candidate_id": TARGET_CANDIDATE_ID,
        "reason_codes": sorted(REQUIRED_REASONS),
        "song_complete": False,
        "active_delivery": None,
        "fresh_negative_evidence": {
            "run_id": negative["candidate_id"],
            "result_path": negative["result_path"],
            "result_sha256": negative["result_sha256"],
            "forensic_result_path": negative["forensic_result_path"],
            "source_video_path": negative["source_video_path"],
            "source_video_sha256": negative["source_video_sha256"],
            "live_performance_mode": negative["performance"].get("mode"),
            "repair_report_path": negative["repair_report_path"],
            "repair_report_sha256": negative["repair_report_sha256"],
            "forensic_repair_report_path": negative["forensic_repair_report_path"],
            "incident_binding": copy.deepcopy(negative["incident_binding"]),
        },
        "run_scoped_no_upload_verification": copy.deepcopy(no_upload),
        "repaired_state_sha256": state_sha256,
        "forensic_original": {
            "authoritative_path": str(original_path),
            "sha256": original_sha256,
            "backup_path": str(backup_path),
        },
    }


def build_outputs(
    *,
    base: Path,
    repo_root: Path,
    paths: Mapping[str, Path],
    inputs: Mapping[str, bytes],
    input_hashes: Mapping[str, str],
    negative: Mapping[str, Any],
    no_upload: Mapping[str, Any],
    incident_interval: Mapping[str, Any],
    txid: str,
    tx_dir: Path,
    repaired_at: str,
) -> tuple[dict[str, bytes], bytes, dict[str, Any]]:
    state = build_repaired_state(
        inputs["active_state"],
        state_input_sha256=input_hashes["active_state"],
        negative=negative,
        no_upload=no_upload,
        incident_interval=incident_interval,
        txid=txid,
        tx_dir=tx_dir,
        superseded_report=paths["superseded_v4_report"],
        repaired_at=repaired_at,
    )
    state_payload = json_bytes(state)
    state_sha = sha256_bytes(state_payload)
    generated_summary, latest = _render_reports_isolated(repo_root, state)
    summary, tail_sha, tail_bytes = preserve_summary_tail(generated_summary, inputs["summary"])
    outputs: dict[str, bytes] = {
        "state_backup": state_payload,
        "v4_report": json_bytes(
            _report_tombstone(
                txid=txid,
                repaired_at=repaired_at,
                original_path=paths["v4_report"],
                original_sha256=input_hashes["v4_report"],
                backup_path=tx_dir / "backup" / "v4_report.bin",
                negative=negative,
                no_upload=no_upload,
                state_sha256=state_sha,
            )
        ),
        "superseded_v4_report": json_bytes(
            _report_tombstone(
                txid=txid,
                repaired_at=repaired_at,
                original_path=paths["superseded_v4_report"],
                original_sha256=input_hashes["superseded_v4_report"],
                backup_path=tx_dir / "backup" / "superseded_v4_report.bin",
                negative=negative,
                no_upload=no_upload,
                state_sha256=state_sha,
            )
        ),
        "latest_report": latest,
        "summary": summary,
        "active_state": state_payload,
    }
    v5 = {
        "schema_version": "manual-song-negative-acceptance.v1",
        "status": V5_STATUS,
        "date": DATE,
        "candidate_id": TARGET_CANDIDATE_ID,
        "repair_txid": txid,
        "finished_at": repaired_at,
        "decision": "BLOCK",
        "reason_codes": sorted(REQUIRED_REASONS),
        "song_complete": False,
        "full_source_performer_rejection": True,
        "live_performance": copy.deepcopy(negative["performance"]),
        "lyrics_alignment_ready": bool(negative["lyrics_alignment_ready"]),
        "fresh_negative_result": {
            "run_id": negative["candidate_id"],
            "path": negative["result_path"],
            "sha256": negative["result_sha256"],
            "forensic_path": negative["forensic_result_path"],
            "source_video_path": negative["source_video_path"],
            "source_video_sha256": negative["source_video_sha256"],
            "song_repair_report_path": negative["repair_report_path"],
            "song_repair_report_sha256": negative["repair_report_sha256"],
            "forensic_song_repair_report_path": negative["forensic_repair_report_path"],
            "incident_binding": copy.deepcopy(negative["incident_binding"]),
        },
        "no_upload_verification": copy.deepcopy(no_upload),
        "state_repair": {
            "active_state_path": str(paths["active_state"]),
            "state_backup_path": str(paths["state_backup"]),
            "repaired_state_sha256": state_sha,
            "summary_tail_sha256": tail_sha,
            "summary_tail_bytes": tail_bytes,
        },
    }
    return outputs, json_bytes(v5), v5


def _assert_expected_hashes(observed: Mapping[str, str], expected_file: Path | None) -> None:
    if expected_file is None:
        return
    expected = _load_json(expected_file, "operator expected-hashes file")
    missing = set(observed) - set(expected)
    if missing:
        raise RepairError(f"expected-hashes file is missing targets: {sorted(missing)}")
    mismatches = {
        name: {"expected": str(expected[name]), "observed": actual}
        for name, actual in observed.items()
        if str(expected[name]).removeprefix("sha256:") != actual
    }
    if mismatches:
        raise RepairError(f"operator CAS hashes no longer match: {mismatches}")


def collect_negative_evidence(negative: Mapping[str, Any], tx_dir: Path) -> dict[str, bytes]:
    """Copy small proof JSON/sidecars out of the disposable out/ tree."""

    evidence: dict[str, bytes] = {
        "fresh_negative_summary.json": bytes(negative["result_bytes"]),
        "song_repair_report.json": bytes(negative["repair_report_bytes"]),
    }
    agy = _as_mapping(negative.get("agy_evidence"))
    agy_payloads = _as_mapping(agy.get("forensic_payloads"))
    if set(agy_payloads) != {
        "agy-run.manifest.json",
        "agy-alignment.json",
        "agy-source.lrc",
        "agy-prompt.md",
    }:
        raise RepairError("validated AGY forensic payload set is incomplete")
    total = 0
    for name, raw_payload in agy_payloads.items():
        if not isinstance(raw_payload, bytes):
            raise RepairError(f"validated AGY forensic payload is not bytes: {name}")
        if len(raw_payload) > 5 * 1024 * 1024 or total + len(raw_payload) > 20 * 1024 * 1024:
            raise RepairError(f"fresh negative AGY evidence unexpectedly large: {name}")
        total += len(raw_payload)
        evidence[name] = raw_payload
    expected_paths = {
        "fresh_negative_summary.json": tx_dir / "evidence" / "fresh_negative_summary.json",
        "song_repair_report.json": tx_dir / "evidence" / "song_repair_report.json",
    }
    if str(negative["forensic_result_path"]) != str(expected_paths["fresh_negative_summary.json"]):
        raise RepairError("internal fresh-negative forensic path mismatch")
    if str(negative["forensic_repair_report_path"]) != str(expected_paths["song_repair_report.json"]):
        raise RepairError("internal song-repair forensic path mismatch")
    return evidence


def prepare_transaction(
    *,
    tx_dir: Path,
    paths: Mapping[str, Path],
    inputs: Mapping[str, bytes],
    input_hashes: Mapping[str, str],
    outputs: Mapping[str, bytes],
    v5_path: Path,
    v5_payload: bytes,
    evidence_payloads: Mapping[str, bytes],
    no_upload_guard: Mapping[str, Any],
    txid: str,
    repaired_at: str,
) -> dict[str, Any]:
    if tx_dir.exists():
        raise RepairError(f"transaction directory already exists: {tx_dir}")
    tx_dir.mkdir(parents=True, mode=0o700)
    os.chmod(tx_dir, 0o700)
    (tx_dir / "backup").mkdir(mode=0o700)
    (tx_dir / "stage").mkdir(mode=0o700)
    (tx_dir / "evidence").mkdir(mode=0o700)
    _fsync_dir(tx_dir.parent)
    targets: dict[str, dict[str, Any]] = {}
    for name in AUTHORITY_TARGET_ORDER:
        backup = tx_dir / "backup" / f"{name}.bin"
        staged = tx_dir / "stage" / f"{name}.bin"
        durable_write(backup, inputs[name], exclusive=True)
        durable_write(staged, outputs[name], exclusive=True)
        targets[name] = {
            "path": str(paths[name]),
            "input_sha256": input_hashes[name],
            "backup_path": str(backup),
            "backup_sha256": sha256_file(backup),
            "staged_path": str(staged),
            "staged_sha256": sha256_file(staged),
        }
    v5_staged = tx_dir / "stage" / "v5_report.bin"
    durable_write(v5_staged, v5_payload, exclusive=True)
    evidence_manifest: dict[str, dict[str, Any]] = {}
    for name, payload in sorted(evidence_payloads.items()):
        if Path(name).name != name:
            raise RepairError(f"unsafe forensic evidence filename: {name}")
        evidence_path = tx_dir / "evidence" / name
        durable_write(evidence_path, payload, exclusive=True)
        evidence_manifest[name] = {
            "path": str(evidence_path),
            "sha256": sha256_bytes(payload),
            "bytes": len(payload),
        }
    manifest = {
        "schema_version": "autoslice-containment-transaction.v1",
        "repair_txid": txid,
        "prepared_at": repaired_at,
        "date": DATE,
        "apply_order": list(AUTHORITY_TARGET_ORDER),
        "cas_target_count": len(targets),
        "targets": targets,
        "new_v5_report": {
            "path": str(v5_path),
            "must_be_absent": True,
            "staged_path": str(v5_staged),
            "staged_sha256": sha256_file(v5_staged),
        },
        "fresh_negative_evidence": evidence_manifest,
        "no_upload_guard": copy.deepcopy(dict(no_upload_guard)),
        "recovery_policy": "ROLL_FORWARD_ONLY_NEVER_RESTORE_FALSE_GREEN_BACKUP",
    }
    manifest_payload = json_bytes(manifest)
    durable_write(tx_dir / "manifest.json", manifest_payload, exclusive=True)
    durable_write(
        tx_dir / "journal.json",
        json_bytes(
            {
                "repair_txid": txid,
                "state": "PREPARED",
                "at": repaired_at,
                "manifest_sha256": sha256_bytes(manifest_payload),
            }
        ),
        exclusive=True,
    )
    _fsync_dir(tx_dir)
    return manifest


def _load_transaction(
    tx_dir: Path, *, expected_base: Path | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = tx_dir / "manifest.json"
    manifest, manifest_payload = _load_json_bytes(manifest_path, "transaction manifest")
    journal = _load_json(tx_dir / "journal.json", "transaction journal")
    if manifest.get("schema_version") != "autoslice-containment-transaction.v1":
        raise RepairError("unsupported containment transaction manifest")
    if manifest.get("repair_txid") != journal.get("repair_txid"):
        raise RepairError("transaction manifest/journal id mismatch")
    if journal.get("manifest_sha256") != sha256_bytes(manifest_payload):
        raise RepairError("transaction manifest is not bound to the durable journal")
    if manifest.get("apply_order") != list(AUTHORITY_TARGET_ORDER):
        raise RepairError("transaction apply order is not the audited state-backup-first/active-state-last order")
    if manifest.get("cas_target_count") != 6 or set(_as_mapping(manifest.get("targets"))) != set(AUTHORITY_TARGET_ORDER):
        raise RepairError("transaction does not contain the required six authority CAS targets")
    _validate_transaction_paths(tx_dir, manifest, expected_base=expected_base)
    return manifest, journal


def _validate_transaction_paths(
    tx_dir: Path,
    manifest: Mapping[str, Any],
    *,
    expected_base: Path | None = None,
) -> None:
    """Re-derive every recovery path; never grant authority from manifest text."""

    tx_dir = _lexical_absolute(tx_dir)
    base = _lexical_absolute(expected_base) if expected_base is not None else tx_dir.parent.parent
    expected_forensics = base / "forensics"
    if (
        tx_dir.parent != expected_forensics
        or not tx_dir.name.startswith("false-green-20260709-")
    ):
        raise RepairError("transaction directory is not the exact incident forensics layout")
    require_no_symlink_components(tx_dir, base, "transaction directory")
    repo_root = base / "repo"
    require_no_symlink_components(repo_root, base, "deployed repo root")
    superseded_root = repo_root / "lidousha" / DATE / "_superseded"
    require_no_symlink_components(superseded_root, repo_root, "superseded report directory")
    matches = sorted(superseded_root.glob("*芽吹くとき*.manual-rerun-report.json"))
    if len(matches) != 1:
        raise RepairError(f"expected one exact superseded 芽吹くとき report, observed {matches}")
    expected_targets = {
        "active_state": base / "state" / f"{DATE}.json",
        "state_backup": base / "state" / f"{DATE}.json.bak",
        "v4_report": base / "reports" / "manual_rerun_2026-07-09_mebukutoki_v4.json",
        "superseded_v4_report": matches[0],
        "latest_report": base / "reports" / "latest.md",
        "summary": repo_root / "lidousha" / DATE / "AUTOSLICE_SUMMARY.md",
    }
    targets = _as_mapping(manifest.get("targets"))
    for name, expected in expected_targets.items():
        require_no_symlink_components(expected, base, f"transaction authority target {name}")
        entry = _as_mapping(targets.get(name))
        if _lexical_absolute(Path(str(entry.get("path") or ""))) != _lexical_absolute(expected):
            raise RepairError(f"transaction target path injection/ref mismatch for {name}")
        for kind, root, suffix in (
            ("staged_path", tx_dir / "stage", f"{name}.bin"),
            ("backup_path", tx_dir / "backup", f"{name}.bin"),
        ):
            expected_sidecar = root / suffix
            require_no_symlink_components(expected_sidecar, tx_dir, f"transaction {kind} {name}")
            if _lexical_absolute(Path(str(entry.get(kind) or ""))) != _lexical_absolute(expected_sidecar):
                raise RepairError(f"transaction {kind} path injection for {name}")
    v5 = _as_mapping(manifest.get("new_v5_report"))
    expected_v5 = base / "reports" / "manual_rerun_2026-07-09_mebukutoki_v5.json"
    require_no_symlink_components(
        expected_v5,
        base,
        "transaction v5 target",
        leaf_may_be_missing=True,
    )
    if _lexical_absolute(Path(str(v5.get("path") or ""))) != _lexical_absolute(expected_v5):
        raise RepairError("transaction v5 target path injection")
    expected_v5_stage = tx_dir / "stage" / "v5_report.bin"
    require_no_symlink_components(expected_v5_stage, tx_dir, "transaction v5 staged path")
    if _lexical_absolute(Path(str(v5.get("staged_path") or ""))) != _lexical_absolute(expected_v5_stage):
        raise RepairError("transaction v5 staged path injection")
    evidence = _as_mapping(manifest.get("fresh_negative_evidence"))
    for name, raw_entry in evidence.items():
        if Path(name).name != name:
            raise RepairError(f"transaction evidence name escapes directory: {name}")
        entry = _as_mapping(raw_entry)
        expected_evidence = tx_dir / "evidence" / name
        require_no_symlink_components(expected_evidence, tx_dir, f"transaction evidence {name}")
        if _lexical_absolute(Path(str(entry.get("path") or ""))) != _lexical_absolute(expected_evidence):
            raise RepairError(f"transaction evidence path injection: {name}")
    guard = _as_mapping(manifest.get("no_upload_guard"))
    expected_ledger = base / "reports" / "upload_ledger.jsonl"
    require_no_symlink_components(
        expected_ledger,
        base,
        "transaction upload ledger",
        leaf_may_be_missing=True,
    )
    if _lexical_absolute(Path(str(guard.get("ledger_path") or ""))) != _lexical_absolute(expected_ledger):
        raise RepairError("transaction upload-ledger path injection")
    snapshot_path = Path(str(guard.get("pre_run_snapshot_path") or ""))
    snapshot_root = base / "forensics" / "no-upload-snapshots"
    require_no_symlink_components(snapshot_root, base, "transaction no-upload snapshot directory")
    require_no_symlink_components(snapshot_path, snapshot_root, "transaction no-upload snapshot")
    output_roots = guard.get("output_roots")
    if not isinstance(output_roots, list):
        raise RepairError("transaction no-upload output roots are malformed")
    acceptance_root = base / "out" / "acceptance"
    require_no_symlink_components(acceptance_root, base, "negative acceptance root")
    for raw_path in output_roots:
        require_no_symlink_components(
            Path(str(raw_path)), acceptance_root, "transaction no-upload output root"
        )


def _current_hash_or_absent(path: Path) -> str | None:
    if not path.exists():
        return None
    require_regular_file(path, "transaction target")
    return sha256_file(path)


def _roll_forward_directory_paths(tx_dir: Path, manifest: Mapping[str, Any]) -> list[Path]:
    paths = [tx_dir]
    for raw_entry in _as_mapping(manifest.get("targets")).values():
        entry = _as_mapping(raw_entry)
        paths.extend(
            Path(str(entry[key])).parent
            for key in ("path", "staged_path", "backup_path")
            if entry.get(key)
        )
    v5 = _as_mapping(manifest.get("new_v5_report"))
    paths.extend(
        Path(str(v5[key])).parent for key in ("path", "staged_path") if v5.get(key)
    )
    for raw_entry in _as_mapping(manifest.get("fresh_negative_evidence")).values():
        entry = _as_mapping(raw_entry)
        if entry.get("path"):
            paths.append(Path(str(entry["path"])).parent)
    return paths


def _pinned_fd_for(pinned: Mapping[Path, int], path: Path) -> int:
    key = _lexical_absolute(path.parent)
    try:
        return pinned[key]
    except KeyError as exc:
        raise RepairError(f"internal missing pinned directory for {path}") from exc


def _pinned_hash(
    pinned: Mapping[Path, int],
    path: Path,
    label: str,
    *,
    allow_absent: bool = False,
) -> str | None:
    return _hash_regular_at(
        _pinned_fd_for(pinned, path),
        path.name,
        label,
        allow_absent=allow_absent,
    )


def _pinned_read(pinned: Mapping[Path, int], path: Path, label: str) -> bytes:
    return _read_regular_at(_pinned_fd_for(pinned, path), path.name, label)


def _secure_preflight_roll_forward(
    manifest: Mapping[str, Any],
    pinned: Mapping[Path, int],
    *,
    recovery: bool,
) -> None:
    """Repeat all CAS/evidence checks through pinned directory descriptors."""

    targets = _as_mapping(manifest.get("targets"))
    for name in AUTHORITY_TARGET_ORDER:
        entry = _as_mapping(targets.get(name))
        target = Path(str(entry.get("path") or ""))
        current = _pinned_hash(pinned, target, f"target {name}", allow_absent=True)
        allowed = (
            {entry.get("input_sha256"), entry.get("staged_sha256")}
            if recovery
            else {entry.get("input_sha256")}
        )
        if current not in allowed:
            raise RepairError(
                f"pinned CAS drift for {name}: current={current}, allowed={sorted(str(x) for x in allowed)}"
            )
        staged = Path(str(entry.get("staged_path") or ""))
        if _pinned_hash(pinned, staged, f"staged {name}") != entry.get("staged_sha256"):
            raise RepairError(f"pinned staged bytes drifted for {name}")
        backup = Path(str(entry.get("backup_path") or ""))
        backup_sha = _pinned_hash(pinned, backup, f"forensic backup {name}")
        if backup_sha != entry.get("backup_sha256") or backup_sha != entry.get("input_sha256"):
            raise RepairError(f"pinned forensic backup mismatch for {name}")
    v5 = _as_mapping(manifest.get("new_v5_report"))
    v5_path = Path(str(v5.get("path") or ""))
    v5_current = _pinned_hash(pinned, v5_path, "v5 target", allow_absent=True)
    allowed_v5 = {None, v5.get("staged_sha256")} if recovery else {None}
    if v5_current not in allowed_v5:
        raise RepairError(f"pinned v5 report collision/drift: {v5_current}")
    v5_stage = Path(str(v5.get("staged_path") or ""))
    if _pinned_hash(pinned, v5_stage, "staged v5") != v5.get("staged_sha256"):
        raise RepairError("pinned staged v5 bytes drifted")
    for name, raw_entry in _as_mapping(manifest.get("fresh_negative_evidence")).items():
        entry = _as_mapping(raw_entry)
        evidence_path = Path(str(entry.get("path") or ""))
        evidence_payload = _pinned_read(pinned, evidence_path, f"forensic evidence {name}")
        if sha256_bytes(evidence_payload) != entry.get("sha256") or len(evidence_payload) != entry.get("bytes"):
            raise RepairError(f"pinned forensic evidence drifted: {name}")


def preflight_roll_forward(manifest: Mapping[str, Any], *, recovery: bool) -> None:
    targets = _as_mapping(manifest.get("targets"))
    for name in AUTHORITY_TARGET_ORDER:
        entry = _as_mapping(targets.get(name))
        path = Path(str(entry.get("path") or ""))
        current = _current_hash_or_absent(path)
        allowed = {entry.get("input_sha256"), entry.get("staged_sha256")} if recovery else {entry.get("input_sha256")}
        if current not in allowed:
            raise RepairError(f"CAS drift for {name}: current={current}, allowed={sorted(str(x) for x in allowed)}")
        staged = Path(str(entry.get("staged_path") or ""))
        require_regular_file(staged, f"staged {name}")
        if sha256_file(staged) != entry.get("staged_sha256"):
            raise RepairError(f"staged bytes drifted for {name}")
        backup = Path(str(entry.get("backup_path") or ""))
        require_regular_file(backup, f"forensic backup {name}")
        if sha256_file(backup) != entry.get("backup_sha256") or entry.get("backup_sha256") != entry.get("input_sha256"):
            raise RepairError(f"forensic backup mismatch for {name}")
    v5 = _as_mapping(manifest.get("new_v5_report"))
    v5_path = Path(str(v5.get("path") or ""))
    v5_current = _current_hash_or_absent(v5_path)
    allowed_v5 = {None, v5.get("staged_sha256")} if recovery else {None}
    if v5_current not in allowed_v5:
        raise RepairError(f"v5 report path collision/drift: {v5_path} sha256={v5_current}")
    evidence = _as_mapping(manifest.get("fresh_negative_evidence"))
    if not {"fresh_negative_summary.json", "song_repair_report.json"}.issubset(evidence):
        raise RepairError("transaction is missing immutable fresh-negative forensic evidence")
    for name, raw_entry in evidence.items():
        entry = _as_mapping(raw_entry)
        evidence_path = Path(str(entry.get("path") or ""))
        require_regular_file(evidence_path, f"forensic evidence {name}")
        if sha256_file(evidence_path) != entry.get("sha256") or evidence_path.stat().st_size != entry.get("bytes"):
            raise RepairError(f"forensic evidence drifted: {name}")
    revalidate_no_upload_guard(_as_mapping(manifest.get("no_upload_guard")))


def roll_forward(
    tx_dir: Path,
    *,
    recovery: bool,
    expected_base: Path | None = None,
) -> dict[str, Any]:
    manifest, journal = _load_transaction(tx_dir, expected_base=expected_base)
    targets = _as_mapping(manifest["targets"])
    v5 = _as_mapping(manifest["new_v5_report"])
    v5_path = Path(str(v5["path"]))
    with pinned_directories(_roll_forward_directory_paths(tx_dir, manifest)) as pinned:
        if journal.get("state") == "COMMITTED":
            mismatches = {
                name: _pinned_hash(
                    pinned,
                    Path(str(_as_mapping(targets[name])["path"])),
                    f"committed target {name}",
                    allow_absent=True,
                )
                for name in AUTHORITY_TARGET_ORDER
                if _pinned_hash(
                    pinned,
                    Path(str(_as_mapping(targets[name])["path"])),
                    f"committed target {name}",
                    allow_absent=True,
                )
                != _as_mapping(targets[name])["staged_sha256"]
            }
            v5_hash = _pinned_hash(pinned, v5_path, "committed v5", allow_absent=True)
            if mismatches or v5_hash != v5["staged_sha256"]:
                raise RepairError(
                    f"COMMITTED journal read-back mismatch: targets={mismatches}, v5={v5_hash}"
                )
            return manifest
        if journal.get("state") != "PREPARED":
            raise RepairError(f"transaction is neither PREPARED nor COMMITTED: {journal.get('state')}")

        preflight_roll_forward(manifest, recovery=recovery)
        _secure_preflight_roll_forward(manifest, pinned, recovery=recovery)
        no_upload_guard = _as_mapping(manifest.get("no_upload_guard"))
        for name in AUTHORITY_TARGET_ORDER:
            # Path checks detect stable/inter-step swaps; the pinned descriptor
            # makes even a last-instruction parent swap write to the original
            # audited directory inode rather than follow an external symlink.
            _validate_transaction_paths(tx_dir, manifest, expected_base=expected_base)
            if name == "active_state":
                revalidate_no_upload_guard(no_upload_guard)
            entry = _as_mapping(targets[name])
            path = Path(str(entry["path"]))
            current = _pinned_hash(pinned, path, f"roll-forward target {name}", allow_absent=True)
            if current != entry["staged_sha256"]:
                if current != entry["input_sha256"]:
                    raise RepairError(f"pinned CAS drift during roll-forward for {name}: {current}")
                staged_path = Path(str(entry["staged_path"]))
                staged_payload = _pinned_read(pinned, staged_path, f"staged {name}")
                try:
                    _atomic_install_at(_pinned_fd_for(pinned, path), path.name, staged_payload)
                except OSError as exc:
                    raise RepairError(f"pinned install failed for {name}: {exc}") from exc
                if _pinned_hash(pinned, path, f"installed target {name}") != entry["staged_sha256"]:
                    raise RepairError(f"pinned read-back hash mismatch after installing {name}")
            # state.bak is the first authoritative mutation, including ahead
            # of the newly-created v5 evidence report.
            if name == "state_backup":
                v5_current = _pinned_hash(pinned, v5_path, "v5 target", allow_absent=True)
                if v5_current is None:
                    v5_stage = Path(str(v5["staged_path"]))
                    v5_payload = _pinned_read(pinned, v5_stage, "staged v5")
                    try:
                        _atomic_install_at(_pinned_fd_for(pinned, v5_path), v5_path.name, v5_payload)
                    except OSError as exc:
                        raise RepairError(f"pinned v5 install failed: {exc}") from exc
                elif v5_current != v5["staged_sha256"]:
                    raise RepairError(f"pinned v5 report CAS drift during roll-forward: {v5_current}")

        expected_readback = {
            name: _as_mapping(targets[name])["staged_sha256"] for name in AUTHORITY_TARGET_ORDER
        }
        readback = {
            name: _pinned_hash(
                pinned,
                Path(str(_as_mapping(targets[name])["path"])),
                f"pre-journal target {name}",
            )
            for name in AUTHORITY_TARGET_ORDER
        }
        if readback["state_backup"] != readback["active_state"]:
            raise RepairError("pre-journal active state and state.bak differ")
        if readback != expected_readback or _pinned_hash(pinned, v5_path, "pre-journal v5") != v5["staged_sha256"]:
            raise RepairError(f"pre-journal pinned read-back hash mismatch: {readback}")

        # The uploader shares upload.lock, held by run() across this check and
        # COMMITTED, so it cannot create the final-check/journal race.
        _validate_transaction_paths(tx_dir, manifest, expected_base=expected_base)
        final_no_upload = revalidate_no_upload_guard(no_upload_guard)
        committed_at = utc_now()
        manifest_path = tx_dir / "manifest.json"
        journal_payload = json_bytes(
            {
                "repair_txid": manifest["repair_txid"],
                "state": "COMMITTED",
                "at": committed_at,
                "manifest_sha256": sha256_bytes(_pinned_read(pinned, manifest_path, "transaction manifest")),
                "no_upload_reverified_at": committed_at,
                "ledger_verified_through_offset": final_no_upload["ledger_after_offset"],
            }
        )
        try:
            _atomic_install_at(pinned[_lexical_absolute(tx_dir)], "journal.json", journal_payload)
        except OSError as exc:
            raise RepairError(f"pinned COMMITTED journal install failed: {exc}") from exc

        final_readback = {
            name: _pinned_hash(
                pinned,
                Path(str(_as_mapping(targets[name])["path"])),
                f"post-journal target {name}",
            )
            for name in AUTHORITY_TARGET_ORDER
        }
        if final_readback != expected_readback or _pinned_hash(pinned, v5_path, "post-journal v5") != v5["staged_sha256"]:
            raise RepairError(f"post-journal pinned read-back hash mismatch: {final_readback}")
        _validate_transaction_paths(tx_dir, manifest, expected_base=expected_base)
        return manifest


def _find_unfinished_transactions(forensics_root: Path) -> list[Path]:
    pending = []
    if not forensics_root.is_dir():
        return pending
    for journal_path in forensics_root.glob("false-green-20260709-*/journal.json"):
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending.append(journal_path.parent)
            continue
        if journal.get("state") != "COMMITTED":
            pending.append(journal_path.parent)
    return sorted(pending)


def _plan_payload(
    *,
    txid: str,
    tx_dir: Path,
    paths: Mapping[str, Path],
    input_hashes: Mapping[str, str],
    outputs: Mapping[str, bytes],
    v5_path: Path,
    v5_payload: bytes,
    negative: Mapping[str, Any],
    no_upload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "mode": "PLAN",
        "repair_txid": txid,
        "transaction_dir": str(tx_dir),
        "fresh_negative": {
            "run_id": negative["candidate_id"],
            "result_path": negative["result_path"],
            "result_sha256": negative["result_sha256"],
            "source_video_sha256": negative["source_video_sha256"],
            "incident_binding": copy.deepcopy(negative["incident_binding"]),
            "mode": negative["performance"].get("mode"),
            "reason_codes": negative["reason_codes"],
        },
        "run_scoped_no_upload": no_upload,
        "six_file_cas": [
            {
                "order": index,
                "name": name,
                "path": str(paths[name]),
                "input_sha256": input_hashes[name],
                "planned_sha256": sha256_bytes(outputs[name]),
            }
            for index, name in enumerate(AUTHORITY_TARGET_ORDER, 1)
        ],
        "new_v5_report": {
            "path": str(v5_path),
            "must_be_absent": True,
            "planned_sha256": sha256_bytes(v5_payload),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="prepare and roll forward the transaction")
    action.add_argument("--recover", type=Path, help="roll forward an existing PREPARED transaction")
    action.add_argument(
        "--capture-no-upload-snapshot",
        type=Path,
        help="before acceptance: bind ledger prefix + planned run/source into an immutable snapshot",
    )
    parser.add_argument("--base", type=Path, default=Path("/opt/bilive/autoslice"))
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--negative-result", type=Path, help="fresh negative selector summary.json")
    parser.add_argument("--superseded-v4-report", type=Path, help="exact _superseded v4 report path")
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--no-upload-snapshot", type=Path)
    parser.add_argument("--planned-run-id")
    parser.add_argument("--planned-run-root", type=Path)
    parser.add_argument("--planned-source-video", type=Path)
    parser.add_argument(
        "--expected-source-sha256",
        help="optional operator consistency check; immutable incident spec remains authoritative",
    )
    parser.add_argument("--expected-hashes", type=Path, help="optional operator-pinned six-target hash JSON")
    parser.add_argument("--transaction-id", help="test/forensic override; default is UTC+random")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    base = args.base.resolve()
    repo_root = (args.repo_root or (base / "repo")).resolve()
    expected_repo_root = (base / "repo").resolve()
    if repo_root != expected_repo_root:
        raise RepairError(
            f"incident containment only supports the deployed repo at {expected_repo_root}; got {repo_root}"
        )
    forensics_root = base / "forensics"
    with runner_lock(base), upload_lock(base):
        ledger = (args.ledger or (base / "reports" / "upload_ledger.jsonl")).resolve()
        if args.capture_no_upload_snapshot:
            if not args.planned_run_id or args.planned_run_root is None or args.planned_source_video is None:
                raise RepairError(
                    "snapshot capture requires --planned-run-id, --planned-run-root, and --planned-source-video"
                )
            snapshot = capture_no_upload_snapshot(
                base=base,
                ledger=ledger,
                run_id=args.planned_run_id,
                run_root=args.planned_run_root,
                source_video=args.planned_source_video,
                output=args.capture_no_upload_snapshot,
            )
            return {
                "mode": "NO_UPLOAD_SNAPSHOT_CAPTURED",
                "snapshot": str(args.capture_no_upload_snapshot),
                "snapshot_sha256": sha256_file(args.capture_no_upload_snapshot),
                "planned_run_id": snapshot["planned_run_id"],
                "ledger_before_offset": snapshot["ledger_before_offset"],
            }
        if args.recover:
            tx_dir = args.recover.resolve()
            require_within(tx_dir / "manifest.json", forensics_root, "recovery transaction")
            manifest = roll_forward(tx_dir, recovery=True, expected_base=base)
            return {
                "mode": "RECOVERED_OR_ALREADY_COMMITTED",
                "repair_txid": manifest["repair_txid"],
                "transaction_dir": str(tx_dir),
                "journal": str(tx_dir / "journal.json"),
            }

        pending = _find_unfinished_transactions(forensics_root)
        if pending:
            raise RepairError(f"unfinished containment transaction(s) require --recover first: {pending}")
        if (
            args.negative_result is None
            or args.superseded_v4_report is None
            or args.no_upload_snapshot is None
        ):
            raise RepairError(
                "--negative-result, --superseded-v4-report, and --no-upload-snapshot "
                "are required for plan/apply"
            )
        paths = authority_paths(base, repo_root, args.superseded_v4_report.resolve())
        inputs, input_hashes = read_authority_inputs(paths)
        incident_interval = validate_known_false_green_inputs(inputs, repo_root=repo_root)
        _assert_expected_hashes(input_hashes, args.expected_hashes)
        negative = validate_negative_result(
            args.negative_result.resolve(),
            incident=incident_interval,
            expected_source_sha256=args.expected_source_sha256,
        )
        snapshot = validate_no_upload_snapshot(args.no_upload_snapshot.resolve(), negative=negative, ledger=ledger)
        no_upload = build_no_upload_proof(negative, ledger, snapshot)
        txid = args.transaction_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:10]}"
        if not all(ch.isalnum() or ch in "-_" for ch in txid):
            raise RepairError("transaction id may contain only alphanumeric, dash, and underscore")
        tx_dir = forensics_root / f"false-green-20260709-{txid}"
        negative = dict(negative)
        negative["forensic_result_path"] = str(tx_dir / "evidence" / "fresh_negative_summary.json")
        negative["forensic_repair_report_path"] = str(tx_dir / "evidence" / "song_repair_report.json")
        evidence_payloads = collect_negative_evidence(negative, tx_dir)
        repaired_at = utc_now()
        outputs, v5_payload, _v5 = build_outputs(
            base=base,
            repo_root=repo_root,
            paths=paths,
            inputs=inputs,
            input_hashes=input_hashes,
            negative=negative,
            no_upload=no_upload,
            incident_interval=incident_interval,
            txid=txid,
            tx_dir=tx_dir,
            repaired_at=repaired_at,
        )
        v5_path = base / "reports" / "manual_rerun_2026-07-09_mebukutoki_v5.json"
        if v5_path.exists():
            raise RepairError(f"v5 report already exists; refusing overwrite: {v5_path}")
        plan = _plan_payload(
            txid=txid,
            tx_dir=tx_dir,
            paths=paths,
            input_hashes=input_hashes,
            outputs=outputs,
            v5_path=v5_path,
            v5_payload=v5_payload,
            negative=negative,
            no_upload=no_upload,
        )
        if not args.apply:
            return plan
        manifest = prepare_transaction(
            tx_dir=tx_dir,
            paths=paths,
            inputs=inputs,
            input_hashes=input_hashes,
            outputs=outputs,
            v5_path=v5_path,
            v5_payload=v5_payload,
            evidence_payloads=evidence_payloads,
            no_upload_guard=no_upload,
            txid=txid,
            repaired_at=repaired_at,
        )
        # Immediate six-file CAS after PREPARED.  A crash after this point is
        # recovered by roll-forward from the immutable stage, never rollback.
        preflight_roll_forward(manifest, recovery=False)
        roll_forward(tx_dir, recovery=False, expected_base=base)
        plan["mode"] = "COMMITTED"
        plan["journal"] = str(tx_dir / "journal.json")
        return plan


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except RepairError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
