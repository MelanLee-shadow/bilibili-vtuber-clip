"""Fail-closed preparation for reviewed-subtitle baseline package replays.

This Talk-only correction lane reconstructs sealed source bytes into a private
stage; speaker, burn, title/cover, final-review, audit, and state-last delivery
remain separate gates.
"""

from __future__ import annotations
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from src.autoslice.branding_intro import pin_existing_delivery_intro, require_branding_intro
from src.autoslice.c5_start_clamp import finalizer_authority_kwargs
from src.autoslice.fastlane_c9_source_stage import stage_c9_source_action_private_replay  # noqa: F401
from src.autoslice.fastlane_c7b_private_adapter import apply_replay_carry
from src.autoslice.fastlane_c7b_source_reconciliation import (
    C7bSourceReconciliationError, resolve_c7b_source_reconciliation,
)
from src.autoslice.recut_materialization import (
    _accurate_reencode_recut_command,
    _fresh_srt_to_source_cues,
    _write_source_range_srt,
)
from src.autoslice.reviewed_subtitle_baseline_registry import (
    ReviewedSubtitleBaseline,
    ReviewedSubtitleBaselineRegistryError,
    load_candidate_reviewed_subtitle_baseline,
)
from src.autoslice.redelivery_time_domain import (
    RedeliveryTimeDomainError,
    replay_diagnostic_duration_ms,
)
from src.autoslice.reviewed_baseline_replay_projection import (
    PreparedReplayAfterImage,
    canonical_talk_delivery_basename,
    exact_candidate_sidecar_target,
    validate_retained_projection,
)
from src.autoslice.reviewed_baseline_replay_authority import (
    RecordBoundFinalizerAuthority,
    resolve_record_bound_finalizer_authority,
)
from src.autoslice.reviewed_baseline_replay_stage_projection import (
    baseline_application_interval, prepare_stage_delivery_projection,
    stage_delivery_projection_receipt,
)
validate_c9_root_acceptance_envelope = None
REPLAY_STAGE_SCHEMA = "reviewed-baseline-replay-stage.v1"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CID = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_PADDED = re.compile(r"padded_(\d+)_(\d+)\.mp4\Z")
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ReviewedBaselineReplayError(RuntimeError):
    """A reviewed baseline cannot safely be prepared for package replay."""


@dataclass(frozen=True, slots=True)
class RegularBinding:
    path: Path
    sha256: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class ReplayPlan:
    date: str
    candidate_id: str
    package_root: Path
    record_path: Path
    padded_path: Path
    local_start_ms: int
    local_end_ms: int
    expected_video_sha256: str
    baseline: ReviewedSubtitleBaseline
    matrix: tuple[dict[str, str], ...]
    materialization_start_ms: int | None = None
    materialization_end_ms: int | None = None
    technical_media_path: Path | None = None


@dataclass(frozen=True, slots=True)
class PrivateReplayFinalization:
    """The canonical producer's private prepared-delivery result."""

    spec_path: Path
    private_runtime_root: Path
    prepared_manifest: Path
    prepared_sha256: str


@dataclass(frozen=True, slots=True)
class PrivateReplayPackage:
    root: Path
    review_manifest: RegularBinding
    package_audit: RegularBinding
    predicate_matrix: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class ReplayLiveProjection:
    """Private record/publish bytes with only canonical live locators.

    This is not a commit handle.  It is the lane-owned bridge between the
    producer's private prepared delivery and the later state-last transaction.
    """

    record: RegularBinding
    publish: RegularBinding
    chat: RegularBinding
    speaker_manifest: RegularBinding
    private_delivery_root: Path
    live_delivery_root: Path


@dataclass(frozen=True, slots=True)
class ReplayStateProjection:
    """Exact state after-bytes derived from one projected Talk package."""

    before: bytes
    after: bytes
    delivered: Mapping[str, Mapping[str, str]]


def _replay_package_root(plan: ReplayPlan) -> Path:
    """The flat package root below a candidate's immutable outer directory."""

    return plan.package_root / "replacement_recuts"


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                   separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _safe_directory(path: Path) -> Path:
    absolute = Path(path).absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        try:
            observed = os.lstat(cursor)
        except OSError as exc:
            raise ReviewedBaselineReplayError("REPLAY_PATH_UNAVAILABLE") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise ReviewedBaselineReplayError("REPLAY_PATH_UNSAFE")
    return absolute


def regular_binding(path: Path, *, label: str) -> RegularBinding:
    """Stream one regular file without retaining media bytes in memory."""

    path = Path(path)
    _safe_directory(path.parent)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_UNAVAILABLE") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_UNSAFE") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (
            before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode),
            before.st_size, before.st_mtime_ns, before.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode),
                opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != identity
        ):
            raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT") from exc
    observed = (
        after_fd.st_dev, after_fd.st_ino, stat.S_IMODE(after_fd.st_mode),
        after_fd.st_size, after_fd.st_mtime_ns, after_fd.st_ctime_ns,
    )
    named = (
        after_path.st_dev, after_path.st_ino, stat.S_IMODE(after_path.st_mode),
        after_path.st_size, after_path.st_mtime_ns, after_path.st_ctime_ns,
    )
    if observed != identity or named != identity or stat.S_ISLNK(after_path.st_mode):
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT")
    return RegularBinding(path, "sha256:" + digest.hexdigest(), *identity)


def _load_json(binding: RegularBinding, *, label: str) -> dict[str, Any]:
    payload = _read_small_bytes(binding, label=label)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_INVALID") from exc
    if not isinstance(value, dict):
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_INVALID")
    return value


def _read_small_bytes(binding: RegularBinding, *, label: str) -> bytes:
    """Return a bounded control file only when its stable binding still holds."""

    if binding.size > 4 * 1024 * 1024:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_TOO_LARGE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(binding.path, flags)
    except OSError as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_UNSAFE") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode), opened.st_size,
            opened.st_mtime_ns, opened.st_ctime_ns,
        ) != (
            binding.device, binding.inode, binding.mode, binding.size,
            binding.mtime_ns, binding.ctime_ns,
        ):
            raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if _sha(payload) != binding.sha256 or regular_binding(binding.path, label=label) != binding:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT")
    return payload


def _baseline(*, repo_root: Path, candidate_id: str) -> ReviewedSubtitleBaseline:
    root = repo_root / "assets" / "lidousha" / "reviewed_subtitle_baselines"
    try:
        value = load_candidate_reviewed_subtitle_baseline(
            root, candidate_id, repo_root=repo_root
        )
    except ReviewedSubtitleBaselineRegistryError as exc:
        raise ReviewedBaselineReplayError("REPLAY_BASELINE_INVALID") from exc
    if value is None:
        raise ReviewedBaselineReplayError("REPLAY_BASELINE_MISSING")
    config = value.config
    ownership = config.get("operator_text_full_ownership")
    lanes = config.get("operator_truth_lanes")
    if (
        config.get("schema_version") != "subtitle-redelivery-baseline.v2"
        or config.get("exact_interval_replay") is not True
        or not isinstance(ownership, Mapping)
        or ownership.get("schema_version")
        != "operator-reviewed-text-full-ownership-pin.v3"
        or not isinstance(lanes, Mapping)
        or not isinstance(lanes.get("decision_ledger"), Mapping)
    ):
        raise ReviewedBaselineReplayError("REPLAY_BASELINE_AUTHORITY_INCOMPLETE")
    return value


def _padded_from_provenance(
    recut_root: Path, *, date: str = "", candidate_id: str = "", record_sha256: str = ""
) -> tuple[Path, dict[str, Any]]:
    candidates = sorted(recut_root.glob("*.recut.provenance.json"))
    if len(candidates) != 1:
        raise ReviewedBaselineReplayError("REPLAY_PROVENANCE_AMBIGUOUS")
    provenance = _load_json(regular_binding(candidates[0], label="PROVENANCE"), label="PROVENANCE")
    final = provenance.get("final_recut")
    if not isinstance(final, Mapping):
        raise ReviewedBaselineReplayError("REPLAY_PROVENANCE_INVALID")
    raw = final.get("source_path")
    if not isinstance(raw, str):
        raise ReviewedBaselineReplayError("REPLAY_PROVENANCE_INVALID")
    source = Path(raw)
    if source.parent != recut_root.parent:
        expected_parent = Path("/opt/bilive/autoslice/out/2026-08-14/auto_130040_201_255")
        if not (
            date == "2026-08-14"
            and candidate_id == "auto_130040_201_255"
            and record_sha256 == "sha256:b875d8ddedaa47971249e3af057b218f45e62fb002ddcf6c9733cd38d5bfd8f5"
            and source.parent == expected_parent
            and source.name == "padded_191190_303140.mp4"
            and final.get("source_sha256") == "5b06a7bb19e22c0a8c368c83ee83170ff068b04d32fd07cf246859e8f09014f0"
        ):
            raise ReviewedBaselineReplayError("REPLAY_PADDED_SOURCE_ESCAPES_CANDIDATE")
        source = recut_root.parent / source.name
    return source, dict(final)


def build_replay_plan(*, repo_root: Path, out_root: Path, date: str, candidate_id: str) -> ReplayPlan:
    """Bind one current failed package to its sealed reviewed baseline.

    This is read-only.  The returned matrix intentionally records the remaining
    final-package work rather than claiming a reviewed SRT is a release.
    """

    if not _DATE.fullmatch(date) or not _CID.fullmatch(candidate_id):
        raise ReviewedBaselineReplayError("REPLAY_IDENTITY_INVALID")
    repo_root = _safe_directory(repo_root)
    package_root = _safe_directory(out_root / date / candidate_id)
    recut_root = _safe_directory(package_root / "replacement_recuts")
    record_path = recut_root / f"{candidate_id}.record.json"
    record_binding = regular_binding(record_path, label="RECORD")
    record = _load_json(record_binding, label="RECORD")
    artifacts = record.get("artifact_hashes")
    expected = artifacts.get("video_sha256") if isinstance(artifacts, Mapping) else None
    if not isinstance(expected, str) or _SHA.fullmatch(expected) is None:
        raise ReviewedBaselineReplayError("REPLAY_RECORD_VIDEO_BINDING_INVALID")
    baseline = _baseline(repo_root=repo_root, candidate_id=candidate_id)
    config = baseline.config
    padded, final = _padded_from_provenance(
        recut_root, date=date, candidate_id=candidate_id, record_sha256=record_binding.sha256
    )
    padded_binding = regular_binding(padded, label="PADDED_SOURCE")
    declared_padded = final.get("source_sha256")
    if declared_padded != padded_binding.sha256.removeprefix("sha256:"):
        raise ReviewedBaselineReplayError("REPLAY_PADDED_SOURCE_DRIFT")
    match = _PADDED.fullmatch(padded.name)
    if match is None:
        raise ReviewedBaselineReplayError("REPLAY_PADDED_SOURCE_NAME_INVALID")
    padded_start, padded_end = (int(value) for value in match.groups())
    boundary = record.get("boundary_audit")
    start = boundary.get("final_start_ms") if isinstance(boundary, Mapping) else None
    end = boundary.get("final_end_ms") if isinstance(boundary, Mapping) else None
    if (
        isinstance(start, bool) or not isinstance(start, int)
        or isinstance(end, bool) or not isinstance(end, int)
        or not 0 <= start < end <= padded_end - padded_start
        or record.get("duration_ms") != end - start
    ):
        raise ReviewedBaselineReplayError("REPLAY_RECORD_TIMING_INVALID")
    try:
        diagnostic_duration_ms = replay_diagnostic_duration_ms(
            config,
            padded_start_ms=padded_start,
            padded_end_ms=padded_end,
            final_start_ms=start,
            final_end_ms=end,
        )
    except RedeliveryTimeDomainError as exc:
        raise ReviewedBaselineReplayError(str(exc)) from exc
    # Verify the attested baseline geometry before any private media write.
    # A v2 baseline may bind either the padded source or this record's exact
    # final interval, never an arbitrary third window.
    baseline_application_interval(
        config=config,
        padded_start_ms=padded_start,
        padded_end_ms=padded_end,
        final_start_ms=start,
        final_end_ms=end,
        error=ReviewedBaselineReplayError,
    )
    diagnostic = config.get("operator_truth_lanes", {}).get("pipeline_diagnostic")
    if not isinstance(diagnostic, Mapping) or not isinstance(diagnostic.get("path"), str):
        raise ReviewedBaselineReplayError("REPLAY_PIPELINE_DIAGNOSTIC_MISSING")
    diagnostic_path = baseline.manifest_path.parent / str(diagnostic["path"])
    diagnostic_binding = regular_binding(diagnostic_path, label="PIPELINE_DIAGNOSTIC")
    if diagnostic_binding.sha256.removeprefix("sha256:") != diagnostic.get("sha256"):
        raise ReviewedBaselineReplayError("REPLAY_PIPELINE_DIAGNOSTIC_DRIFT")
    try:
        diagnostic_text = _read_small_bytes(
            diagnostic_binding, label="PIPELINE_DIAGNOSTIC"
        ).decode("utf-8")
        # The diagnostic shares the reviewed baseline's explicit local grid.
        # DELIVERY_LOCAL grids are checked against the final media duration;
        # PIECE_LOCAL grids are checked against the padded source interval.
        _fresh_srt_to_source_cues(
            diagnostic_text,
            window_start_ms=0,
            duration_ms=diagnostic_duration_ms,
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ReviewedBaselineReplayError(
            "REPLAY_PIPELINE_DIAGNOSTIC_GEOMETRY_INVALID"
        ) from exc

    # C7b is the only permitted stale-record media exception.  It is resolved
    # only after the record, provenance, padded source and actual recut have
    # all been independently bound; generic candidates retain the ordinary
    # record-declared media path and hash.
    provenance_path = recut_root / f"{candidate_id}.recut.provenance.json"
    provenance_binding = regular_binding(provenance_path, label="PROVENANCE")
    provenance = _load_json(provenance_binding, label="PROVENANCE")
    actual_path = recut_root / f"{candidate_id}.recut.mp4"
    actual_binding = None
    if (date, candidate_id) == ("2026-08-14", "auto_130040_201_255"):
        try:
            actual_binding = regular_binding(actual_path, label="C7B_ACTUAL_RECUT")
        except ReviewedBaselineReplayError as exc:
            raise ReviewedBaselineReplayError("C7B_SOURCE_ACTUAL_RECUT_UNAVAILABLE") from exc
    try:
        reconciliation = resolve_c7b_source_reconciliation(
            repo_root=repo_root, date=date, candidate_id=candidate_id,
            record_binding=record_binding, record=record,
            provenance_binding=provenance_binding, provenance=provenance,
            padded_binding=padded_binding, actual_binding=actual_binding,
        )
    except C7bSourceReconciliationError as exc:
        raise ReviewedBaselineReplayError(str(exc)) from exc
    technical_expected = expected
    technical_start = technical_end = None
    technical_media = None
    if reconciliation is not None:
        technical_expected = reconciliation.expected_video_sha256
        technical_start = reconciliation.materialization_start_ms
        technical_end = reconciliation.materialization_end_ms
        technical_media = reconciliation.actual_recut_path
    matrix = (
        {"predicate": "RECORD_OLD_VIDEO_SHA256", "status": "PASS"},
        *(({"predicate": "C7B_PROVENANCE_BOUND_ACTUAL_RECUT", "status": "PASS"},) if reconciliation is not None else ()),
        {"predicate": "PADDED_SOURCE_BINDING", "status": "PASS"},
        {"predicate": "REVIEWED_BASELINE_V2_V3_LEDGER", "status": "PASS"},
        {"predicate": "REVIEWED_BASELINE_EXPLICIT_TIME_DOMAIN", "status": "PASS"},
        {"predicate": "PIPELINE_DIAGNOSTIC_HASH_AND_GEOMETRY", "status": "PASS"},
        {"predicate": "SPEAKER_ASS_BURN_REBUILD", "status": "PENDING_STAGE"},
        {"predicate": "TITLE_COVER_PRECONDITIONS", "status": "PENDING_STAGE"},
        {"predicate": "FINAL_REVIEW_AND_PACKAGE_AUDIT", "status": "PENDING_STAGE"},
        {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"},
    )
    return ReplayPlan(
        date, candidate_id, package_root, record_path, padded,
        start, end, technical_expected, baseline, matrix,
        technical_start, technical_end, technical_media,
    )


def _mkdir_private(path: Path) -> Path:
    _safe_directory(path.parent)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_EXISTS") from exc
    observed = os.lstat(path)
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o700:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_UNSAFE")
    return path


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise ReviewedBaselineReplayError("REPLAY_PRIVATE_WRITE_FAILED")
            view = view[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if stat.S_IMODE(os.lstat(path).st_mode) != 0o600:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_UNSAFE")


def stage_replay(
    plan: ReplayPlan, *, stage_parent: Path, run_command: Sequence[str] | None = None,
    runtime_authority_root: Path | None = None,
) -> dict[str, Any]:
    """Create a private recut/SRT/audit stage; no live target is named writable."""

    stage_parent = _safe_directory(stage_parent)
    seed = _sha(_canonical({
        "date": plan.date, "candidate_id": plan.candidate_id,
        "record": regular_binding(plan.record_path, label="RECORD").sha256,
        "baseline": "sha256:" + plan.baseline.config["sha256"],
    })).removeprefix("sha256:")
    stage = _mkdir_private(stage_parent / f"{plan.date}-{plan.candidate_id}-{seed[:16]}")
    media = stage / "recut.mp4"
    command = list(run_command or ())
    technical_media_path = getattr(plan, "technical_media_path", None)
    if technical_media_path is not None:
        if command:
            raise ReviewedBaselineReplayError("C7B_SOURCE_EXACT_BYTE_CARRY_COMMAND_FORBIDDEN")
        _copy_private_artifact(technical_media_path, media)
    elif not command:
        # Reuse the production accurate-recut command (including its coarse
        # seek and decode-before-trim path); a superficially equivalent local
        # ffmpeg spelling is not a safe source-bound replay authority.
        materialization_start_ms, materialization_end_ms = getattr(plan, "materialization_start_ms", None), getattr(plan, "materialization_end_ms", None)
        command = _accurate_reencode_recut_command(
            source_video=plan.padded_path,
            output_media=media,
            start_ms=materialization_start_ms if materialization_start_ms is not None else plan.local_start_ms,
            duration_ms=(materialization_end_ms - materialization_start_ms)
            if materialization_start_ms is not None and materialization_end_ms is not None
            else plan.local_end_ms - plan.local_start_ms,
        )
    if technical_media_path is None:
        if not command or command[-1] != str(media):
            raise ReviewedBaselineReplayError("REPLAY_COMMAND_TARGET_INVALID")
        completed = subprocess.run(command, check=False, capture_output=True)
        if completed.returncode != 0:
            raise ReviewedBaselineReplayError("REPLAY_ACCURATE_RECUT_FAILED")
    try:
        os.chmod(media, 0o600)
    except OSError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_UNSAFE") from exc
    rebuilt = regular_binding(media, label="STAGED_VIDEO")
    if rebuilt.sha256 != plan.expected_video_sha256:
        raise ReviewedBaselineReplayError("REPLAY_OLD_RECORD_VIDEO_SHA256_MISMATCH")
    c5_fields: dict[str, object] = {}
    if plan.candidate_id == "auto_113028_1271_1328" and plan.date == "2026-08-14" and runtime_authority_root is not None:
        from src.autoslice.c5_start_clamp import finalizer_authority_kwargs
        c5_fields = finalizer_authority_kwargs(candidate_id=plan.candidate_id, recording_date=plan.date, runtime_root=runtime_authority_root)
    elif plan.candidate_id == "auto_130040_201_255" and plan.date == "2026-08-14":
        c5_fields = {"recording_date": plan.date}
    cropped_bytes, audit, projection_descriptor = prepare_stage_delivery_projection(
        plan, stage, rebuilt, regular_binding=regular_binding, load_json=_load_json,
        read_small_bytes=_read_small_bytes, fresh_srt_to_source_cues=_fresh_srt_to_source_cues,
        write_source_range_srt=_write_source_range_srt, error=ReviewedBaselineReplayError,
        private_stage_authority_gate=True,
        **c5_fields,
    )
    _write_private(stage / "reviewed.srt", cropped_bytes)
    _write_private(stage / "redelivery-baseline.json", _canonical(audit))
    document: dict[str, Any] = {
        "schema_version": REPLAY_STAGE_SCHEMA,
        "date": plan.date,
        "candidate_id": plan.candidate_id,
        "record_path": str(plan.record_path),
        "record_sha256": regular_binding(plan.record_path, label="RECORD").sha256,
        "expected_video_sha256": plan.expected_video_sha256,
        "baseline_manifest": str(plan.baseline.manifest_path),
        "baseline_sha256": "sha256:" + str(plan.baseline.config["sha256"]),
        "artifacts": {
            "video": regular_binding(media, label="STAGED_VIDEO").sha256,
            "subtitle": regular_binding(stage / "reviewed.srt", label="STAGED_SRT").sha256,
            "baseline_audit": regular_binding(stage / "redelivery-baseline.json", label="STAGED_AUDIT").sha256,
        },
        "predicate_matrix": list(plan.matrix),
        "upload_allowed": False,
    }
    if projection_descriptor is not None:
        document["delivery_projection_receipt"] = projection_descriptor
    document["stage_sha256"] = _sha(_canonical(document))
    _write_private(stage / "stage.json", _canonical(document))
    return {"stage": str(stage), "stage_sha256": document["stage_sha256"], "predicate_matrix": document["predicate_matrix"]}

def _copy_private_artifact(source: Path, target: Path) -> RegularBinding:
    source_binding = regular_binding(source, label="PRIVATE_SOURCE")
    if target.exists() or target.is_symlink():
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_ARTIFACT_COLLISION")
    # Never hard-link a source-owned control artifact: chmod/fsync of the
    # private target would mutate the source inode's ctime/mode and invalidate
    # the very binding this replay is trying to preserve.
    try:
        shutil.copyfile(source, target)
    except OSError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_ARTIFACT_COPY_FAILED") from exc
    os.chmod(target, 0o600)
    target_binding = regular_binding(target, label="PRIVATE_TARGET")
    if target_binding.sha256 != source_binding.sha256:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_ARTIFACT_COPY_DRIFT")
    return target_binding


def _replace_private_artifact(source: Path, target: Path) -> RegularBinding:
    """Atomically overlay one regular file in a caller-owned private mirror."""

    _safe_directory(target.parent)
    try:
        observed = os.lstat(target)
    except FileNotFoundError:
        return _copy_private_artifact(source, target)
    except OSError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_UNSAFE") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_UNSAFE")
    temporary = target.with_name(f".{target.name}.replay-{os.getpid()}")
    copied = _copy_private_artifact(source, temporary)
    try:
        os.replace(temporary, target)
        descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    rebound = regular_binding(target, label="PRIVATE_TARGET")
    if rebound.sha256 != copied.sha256:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_DRIFT")
    return rebound


def _private_control_document(root: Path, *, label: str, document: Mapping[str, object]) -> Path:
    """Create one immutable small private control document for an overlay."""

    digest = hashlib.sha256(_canonical(document)).hexdigest()
    path = Path(root) / f".{label}-{digest}.json"
    _write_private(path, _canonical(document))
    return path


def _copy_deployed_authority(*, source_runtime_root: Path, private_runtime_root: Path) -> None:
    """Copy the already validated deployed seal into an isolated runtime.

    ``prepare_talk_delivery`` derives its runtime root from the output layout
    and independently validates this seal.  A private replay therefore may
    not invent a minimal placeholder manifest: it copies the stable, validated
    production binding supplied by the caller.
    """

    from src.autoslice.producer_delivery_transaction import deployment_authority_binding

    source_root = _safe_directory(source_runtime_root)
    expected = deployment_authority_binding(source_root)
    source_repo = _safe_directory(source_root / "repo")
    private_repo = _mkdir_private(private_runtime_root / "repo")
    manifest_binding = regular_binding(
        source_repo / "DEPLOYED_AUTHORITY_MANIFEST.json", label="DEPLOYED_MANIFEST"
    )
    manifest = _load_json(manifest_binding, label="DEPLOYED_MANIFEST")
    entries = manifest.get("entries")
    if not isinstance(entries, Mapping):
        raise ReviewedBaselineReplayError("REPLAY_DEPLOYED_AUTHORITY_COPY_INVALID")
    for name in ("DEPLOYED_COMMIT", "DEPLOYED_AUTHORITY_MANIFEST.json"):
        _copy_private_artifact(source_repo / name, private_repo / name)
    # The finalizer derives its repository root from its candidate-private
    # output layout.  Carry precisely the manifest-declared assets into that
    # root; a seal-only directory would later resolve a missing asset through
    # an ambient runtime (or fail merely because isolation worked).
    for raw_relative, entry in sorted(entries.items()):
        if not isinstance(raw_relative, str) or not isinstance(entry, Mapping):
            raise ReviewedBaselineReplayError("REPLAY_DEPLOYED_AUTHORITY_COPY_INVALID")
        relative = Path(raw_relative)
        source = source_repo / relative
        observed = regular_binding(source, label="DEPLOYED_ASSET")
        if entry.get("sha256") != observed.sha256 or entry.get("bytes") != observed.size:
            raise ReviewedBaselineReplayError("REPLAY_DEPLOYED_AUTHORITY_COPY_DRIFT")
        _copy_private_artifact(source, _private_relative_path(private_repo, relative))
    if deployment_authority_binding(private_runtime_root) != expected:
        raise ReviewedBaselineReplayError("REPLAY_DEPLOYED_AUTHORITY_COPY_DRIFT")


def _validated_padded_provenance(plan: ReplayPlan) -> Path:
    """Return the normal producer padded provenance bound to ``plan``."""

    path = plan.padded_path.with_suffix(".provenance.json")
    document = _load_json(regular_binding(path, label="PADDED_PROVENANCE"), label="PADDED_PROVENANCE")
    padded = regular_binding(plan.padded_path, label="PADDED_SOURCE")
    output = document.get("output_path")
    output_sha = document.get("output_sha256")
    inputs = document.get("inputs")
    if (
        not isinstance(output, str)
        or Path(output).resolve(strict=False) != plan.padded_path.resolve()
        or not isinstance(output_sha, str)
        or output_sha.removeprefix("sha256:") != padded.sha256.removeprefix("sha256:")
        or not isinstance(inputs, list)
        or not inputs
    ):
        raise ReviewedBaselineReplayError("REPLAY_PADDED_PROVENANCE_BINDING_DRIFT")
    return path


def _copy_verified_tree(source: Path, target: Path) -> None:
    """Copy a package only after rejecting all symlink/special traversal."""

    source = _safe_directory(source)
    for path in sorted(source.rglob("*")):
        observed = os.lstat(path)
        if stat.S_ISLNK(observed.st_mode) or not (stat.S_ISREG(observed.st_mode) or stat.S_ISDIR(observed.st_mode)):
            raise ReviewedBaselineReplayError("REPLAY_PACKAGE_TREE_UNSAFE")
        if stat.S_ISREG(observed.st_mode):
            regular_binding(path, label="PACKAGE_TREE")
    try:
        shutil.copytree(source, target, copy_function=shutil.copyfile)
    except OSError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PACKAGE_TREE_COPY_FAILED") from exc
    for path in sorted(target.rglob("*")):
        observed = os.lstat(path)
        if stat.S_ISLNK(observed.st_mode) or not (stat.S_ISREG(observed.st_mode) or stat.S_ISDIR(observed.st_mode)):
            raise ReviewedBaselineReplayError("REPLAY_PACKAGE_TREE_COPY_UNSAFE")
        if stat.S_ISREG(observed.st_mode):
            os.chmod(path, 0o600)


def _private_relative_path(root: Path, relative: Path) -> Path:
    """Create checked private parents for one package-relative carried input."""

    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReviewedBaselineReplayError("REPLAY_COVER_CARRY_PATH_INVALID")
    root = _safe_directory(root)
    cursor = root
    for part in relative.parts[:-1]:
        cursor /= part
        try:
            observed = os.lstat(cursor)
        except FileNotFoundError:
            os.mkdir(cursor, 0o700)
            observed = os.lstat(cursor)
        except OSError as exc:
            raise ReviewedBaselineReplayError("REPLAY_COVER_CARRY_PATH_INVALID") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise ReviewedBaselineReplayError("REPLAY_COVER_CARRY_PATH_INVALID")
    return root / relative


def _private_carried_cover_generation(
    plan: ReplayPlan, *, private_package: Path, cover_path: Path,
    cover_sha256: str, generation: Mapping[str, object],
    runtime_authority_root: Path | None = None,
) -> tuple[Path, dict[str, object]]:
    """Copy every hash-declared frozen cover input into the private runtime."""

    rows: list[tuple[tuple[str, ...], str, str]] = [
        ((), str(cover_path), cover_sha256),
        (("final_cover",), str(generation.get("final_cover") or ""), str(generation.get("final_cover_sha256") or "")),
        (("pre_overlay_path",), str(generation.get("pre_overlay_path") or ""), str(generation.get("pre_overlay_sha256") or "")),
        (("ai_background",), str(generation.get("ai_background") or ""), str(generation.get("ai_background_sha256") or "")),
        (("reference_image",), str(generation.get("reference_image") or ""), str(generation.get("cover_reference_sha256") or generation.get("reference_sha256") or "")),
    ]
    rendered = generation.get("rendered_text_pixels")
    if isinstance(rendered, Mapping):
        rows.extend([
            (("rendered_text_pixels", "mask_path"), str(rendered.get("mask_path") or ""), str(rendered.get("mask_sha256") or "")),
            (("rendered_text_pixels", "pre_overlay_path"), str(rendered.get("pre_overlay_path") or ""), str(rendered.get("pre_overlay_sha256") or "")),
        ])
    # Several schema fields intentionally name the same sealed source (for
    # example ``cover_path`` and ``final_cover``).  Validate *each* declared
    # hash, but copy a physical source only once: private staging is
    # create-only and a duplicate copy would turn an otherwise valid carried
    # cover into a collision.
    copied: dict[str, str] = {}
    for pointer, raw_path, expected in rows:
        if not raw_path or _SHA.fullmatch(expected) is None:
            raise ReviewedBaselineReplayError("REPLAY_COVER_BINDING_DRIFT")
        source = Path(raw_path)
        package_root = _replay_package_root(plan)
        if source.is_relative_to(package_root):
            relative = source.relative_to(package_root)
        else:
            # A small, deployed runtime asset (the #6 emote reference) may
            # be a valid frozen cover input without belonging to the old
            # candidate package.  Admit only the deployed runtime's ``assets``
            # tree; every byte is still regular/no-follow hash-bound and the
            # private copy becomes a package-local carried artifact.  This is
            # intentionally not a generic absolute-path import capability.
            if runtime_authority_root is None:
                raise ReviewedBaselineReplayError("REPLAY_COVER_CARRY_PATH_INVALID")
            assets = _safe_directory(Path(runtime_authority_root) / "assets")
            if not source.is_relative_to(assets):
                raise ReviewedBaselineReplayError("REPLAY_COVER_CARRY_PATH_INVALID")
            relative = Path(".replay-carried-runtime-assets") / source.relative_to(assets)
        binding = regular_binding(source, label="COVER_CARRY")
        if binding.sha256 != expected:
            raise ReviewedBaselineReplayError("REPLAY_COVER_BINDING_DRIFT")
        already = copied.get(raw_path)
        if already is not None:
            # The same regular file must map to exactly one package-relative
            # private target.  Its independently declared hash was checked
            # above, so no declaration is silently skipped.
            continue
        # ``plan.package_root`` is the candidate root while ``private_package``
        # is exactly its private ``replacement_recuts`` counterpart.  Relative
        # paths must therefore start at the flat package, not the candidate,
        # or every carried cover becomes ``replacement_recuts/replacement_recuts``.
        target = _private_relative_path(private_package, relative)
        _copy_private_artifact(source, target)
        copied[raw_path] = str(target)
    carried = deepcopy(dict(generation))
    for pointer, raw_path, _expected in rows:
        if pointer == ():
            continue
        parent: dict[str, object] = carried
        for key in pointer[:-1]:
            nested = parent.get(key)
            if not isinstance(nested, dict):
                raise ReviewedBaselineReplayError("REPLAY_COVER_BINDING_DRIFT")
            parent = nested
        parent[pointer[-1]] = copied[raw_path]
    return Path(copied[str(cover_path)]), carried


def replay_publish_adapter(
    plan: ReplayPlan, *, source_fact_llm: Callable[..., object], private_package: Path,
    runtime_authority_root: Path,
) -> Callable[..., dict[str, object]]:
    """Return the lane-owned frozen title/cover adapter."""

    from src.autoslice.reviewed_baseline_replay_publish import replay_publish_adapter as build

    return build(
        plan, source_fact_llm=source_fact_llm, private_package=private_package,
        runtime_authority_root=runtime_authority_root,
    )


def _replay_branding_intro(
    *, runtime_authority_root: Path, burned: object
) -> dict[str, object]:
    """Rebind one subtitle replay to the intro already sealed in its record."""

    recorded_intro = burned.get("branding_intro") if isinstance(burned, Mapping) else None
    if not isinstance(recorded_intro, Mapping):
        raise ReviewedBaselineReplayError("REPLAY_BRANDING_AUTHORITY_MISSING")
    try:
        return pin_existing_delivery_intro(
            require_branding_intro(runtime_authority_root / "repo"), recorded_intro
        )
    except Exception as exc:
        raise ReviewedBaselineReplayError("REPLAY_BRANDING_AUTHORITY_DRIFT") from exc


def _reconstruct_structured_chat(
    clip_context: Mapping[str, object], *, plan: ReplayPlan,
    source_media_sha256: str,
) -> tuple[object, ...]:
    """Rebuild exact-final chat context from the sealed clip-context rows."""
    from src.autoslice.reviewed_baseline_replay_authority import reconstruct_structured_chat

    return reconstruct_structured_chat(
        clip_context, candidate_id=plan.candidate_id, recording_date=plan.date,
        source_media_sha256=source_media_sha256, error=ReviewedBaselineReplayError,
    )


def _production_llm_call(*, runtime_root: Path, effort: str) -> Callable[[str], str]:
    """Use the ordinary CPA command transport with the runtime slot pool."""

    from src.autoslice.llm_client import (
        LlmConfig,
        build_llm_call,
        runtime_cpa_command_environment,
    )

    runtime = _safe_directory(runtime_root)
    bridge = runtime / "repo" / "scripts" / "llm_via_cpa.sh"
    return build_llm_call(LlmConfig(
        transport="command",
        command_template=(
            f"bash {shlex.quote(str(bridge))} {{prompt_file}} {{completion_file}} "
            f"'gpt-5.6-sol gpt-5.5 gpt-5.4' {effort} 1"
        ),
        timeout_seconds=600.0,
        runtime_root=str(runtime),
        command_child_env=runtime_cpa_command_environment(runtime),
    ))


def replay_exact_final_reviewer(
    plan: ReplayPlan,
    *, spec: Mapping[str, object], clip_context: Mapping[str, object],
    runtime_root: Path, out_root: Path,
    padded: Path | None = None,
    verify_confusable_entity: Callable | None = None,
    screen_read_probe: Callable[[int, int], Mapping[str, object]] | None = None,
    boundary_llm: Callable[[str], str] | None = None,
    final_llm: Callable[[str], str] | None = None,
    pronoun_llm: Callable[[str], str] | None = None,
    provider_invocation: Callable[[], None] | None = None,
) -> Callable[[str, Mapping[str, object], int, int], dict[str, object]]:
    """Construct the canonical fresh exact-final closure for replay.

    Typed exhaustive ownership skips only zero-mutation discovery; boundary
    and exact-release reviewers still run against the newly rendered SRT.
    """

    from scripts.gemini_slice_jingting import glossary as review_glossary
    from src.autoslice.delivery_fast_path import (
        discover_priority_findings, resolve_operator_text_full_ownership,
        skipped_final_review_audit,
    )
    from src.autoslice.llm_client import extract_json_object
    from src.autoslice.producer_boundary_review_stage import exact_delivery_correction_audit
    from src.autoslice.frozen_source_boundary_receipt import load_boundary_review_authorities
    from src.autoslice.producer_text_pipeline import (
        TextPipelineAdapters, _final_review_structured_context,
        _run_exact_final_release_review,
    )
    from src.autoslice.review_package_boundary_validators import semantic_boundary_review_is_valid
    from src.autoslice.screen_read_witness import build_env_screen_read_probe
    from src.autoslice.clip_context import clip_context_prompt_text
    ownership = resolve_operator_text_full_ownership(spec)
    if ownership is None:
        raise ReviewedBaselineReplayError("REPLAY_OPERATOR_TEXT_OWNERSHIP_INVALID")
    _frozen_boundary_receipt, frozen_source_review = load_boundary_review_authorities(
        spec,
        candidate_id=plan.candidate_id,
        current_owner_contract=ownership,
    )
    pieces = spec.get("pieces")
    if not isinstance(pieces, list) or len(pieces) != 1 or not isinstance(pieces[0], Mapping):
        raise ReviewedBaselineReplayError("REPLAY_EXACT_FINAL_SPEC_INVALID")
    source_sha = str(pieces[0].get("source_media_sha256") or "")
    authoritative_chat = _reconstruct_structured_chat(
        clip_context, plan=plan, source_media_sha256=source_sha,
    )
    runtime = _safe_directory(runtime_root)
    if verify_confusable_entity is None or padded is None:
        raise ReviewedBaselineReplayError("REPLAY_ENTITY_VERIFIER_MISSING")
    if screen_read_probe is None:
        screen_read_probe = build_env_screen_read_probe(padded)
    def tracked(call: Callable) -> Callable:
        if provider_invocation is None:
            return call
        return lambda *args, **kwargs: (provider_invocation(), call(*args, **kwargs))[1]

    boundary_call = tracked(boundary_llm or _production_llm_call(runtime_root=runtime, effort="medium"))
    final_call = tracked(final_llm or _production_llm_call(runtime_root=runtime, effort="medium"))
    pronoun_call = tracked(pronoun_llm or _production_llm_call(runtime_root=runtime, effort="low"))
    entity_call = tracked(verify_confusable_entity)
    screen_call = tracked(screen_read_probe)
    def unused(*_args: object, **_kwargs: object) -> None: return None
    adapters = TextPipelineAdapters(
        build_aggregate_transcriber=unused, build_agy_transcriber=unused,
        load_term_boundary_surfaces=unused, profile_asset_file=lambda _key: Path("/__replay_unused__"),
        review_glossary=review_glossary, topic_graph_disabled=lambda: True,
        topic_graph_path=unused, topic_graph_expected_sha256=lambda: "",
    )

    def review(
        final_srt_text: str, verified_authority_audit: Mapping[str, object],
        timeline_offset_ms: int, source_final_end_ms: int,
    ) -> dict[str, object]:
        correction = skipped_final_review_audit(
            "SKIPPED_TRUTH_FULL_OWNERSHIP", truth_full_ownership=ownership,
        )
        # Only immutable whole-source review seeds fresh delivery review;
        # prior final-delivery/CLEAN bytes are never a replayable substitute.
        source_boundary = spec.get("boundary_semantic_review")
        if (
            not isinstance(source_boundary, Mapping)
            or not semantic_boundary_review_is_valid(
                source_boundary, expected_scope="source_full_window"
            )
            or source_boundary.get("candidate_id") != plan.candidate_id
        ):
            raise ReviewedBaselineReplayError("REPLAY_SOURCE_BOUNDARY_REVIEW_INVALID")
        correction["boundary_semantic_review"] = dict(source_boundary)
        correction = exact_delivery_correction_audit(
            final_srt_text=final_srt_text, correction_audit=correction,
            source_final_start_ms=timeline_offset_ms,
            source_final_end_ms=source_final_end_ms,
            candidate_id=plan.candidate_id, recording_date=plan.date,
            selection_hook=str(spec.get("selection_hook") or ""),
            selection_scorecard=spec.get("selection_scorecard"),
            structured_context=_final_review_structured_context(
                selection_hook=str(spec.get("selection_hook") or ""),
                authoritative_chat=authoritative_chat,
            ),
            candidate_context=clip_context_prompt_text(clip_context),
            boundary_max_forward_ms=int(spec.get("boundary_repair_extend_cap_ms", 30_000)),
            llm_call=boundary_call, extract_json=extract_json_object, disabled=False,
            frozen_boundary_receipt=None,
            frozen_source_review=frozen_source_review,
        )
        findings, discovery = discover_priority_findings(
            final_srt_text, timeline_offset_ms=timeline_offset_ms,
            entity_verifier=None, out_root=out_root, cid=plan.candidate_id,
            truth_full_ownership=ownership,
        )
        return _run_exact_final_release_review(
            srt_text=final_srt_text, correction_audit=correction,
            adapters=adapters, authoritative_chat=authoritative_chat,
            selection_hook=str(spec.get("selection_hook") or ""),
            clip_context=clip_context, verify_confusable_entity=entity_call,
            verified_authority_audit=verified_authority_audit,
            timeline_offset_ms=timeline_offset_ms, screen_read_probe=screen_call,
            priority_raw_findings=findings, acoustic_discovery_audit=discovery,
            final_review_llm=final_call, pronoun_audit_llm=pronoun_call,
        )

    return review

def synthesize_replay_spec_and_finalize_private(
    plan: ReplayPlan,
    *,
    stage: Path,
    runtime_authority_root: Path,
    speaker_python: Path,
    source_fact_llm: Callable[..., object],
    adapters: object,
    finalizer: Callable[..., int] | None = None,
    exact_final_reviewer: Callable[[str, Mapping[str, object], int, int], dict[str, object]] | None = None,
    use_production_exact_final_reviewer: bool = False,
    exact_final_entity_verifier: Callable | None = None,
    exact_final_text_adapters: object | None = None,
    provider_invocation: Callable[[], None] | None = None,
    record_authority_resolver: Callable[..., RecordBoundFinalizerAuthority] | None = None,
    recovery_publication_authority: Mapping[str, object] | None = None,
) -> PrivateReplayFinalization:
    """Call the canonical producer finalizer in an isolated prepare-only root.
    The synthetic spec is deliberately derived from bound record/provenance
    fields. ``given_title`` stays null; an optional recovery authority must
    already be committed and validated by the published-recovery controller.
    """
    from src.autoslice.producer_package_finalization import (
        ProducerFinalizationOptions,
        finalize_producer_package,
    )
    from src.autoslice.recut_materialization import _fresh_srt_to_source_cues

    stage = _safe_directory(stage)
    projection_receipt_path, projection_receipt_sha256 = stage_delivery_projection_receipt(
        stage, load_json=_load_json, regular_binding=regular_binding,
        canonical=_canonical, sha=_sha, sha_pattern=_SHA,
        error=ReviewedBaselineReplayError,
    )
    record_binding = regular_binding(plan.record_path, label="RECORD")
    record = _load_json(record_binding, label="RECORD")
    provenance_path = next(plan.package_root.joinpath("replacement_recuts").glob("*.recut.provenance.json"), None)
    if provenance_path is None:
        raise ReviewedBaselineReplayError("REPLAY_PROVENANCE_AMBIGUOUS")
    provenance_binding = regular_binding(provenance_path, label="PROVENANCE")
    provenance = _load_json(provenance_binding, label="PROVENANCE")
    source_piece = provenance.get("source_piece")
    padded = provenance.get("padded")
    final_recut = provenance.get("final_recut")
    if not isinstance(source_piece, Mapping) or not isinstance(padded, Mapping) or not isinstance(final_recut, Mapping):
        raise ReviewedBaselineReplayError("REPLAY_PROVENANCE_INVALID")
    normalized_piece = dict(source_piece)
    source_sha = normalized_piece.get("source_media_sha256") or normalized_piece.get("source_media_binding") or normalized_piece.get("source_sha256")
    if not isinstance(source_sha, str) or _SHA.fullmatch(source_sha if source_sha.startswith("sha256:") else f"sha256:{source_sha}") is None:
        raise ReviewedBaselineReplayError("REPLAY_SOURCE_PIECE_BINDING_INVALID")
    normalized_piece["source_media_sha256"] = source_sha if source_sha.startswith("sha256:") else f"sha256:{source_sha}"
    source_path = normalized_piece.get("source_path")
    if not isinstance(source_path, str) or not source_path:
        raise ReviewedBaselineReplayError("REPLAY_SOURCE_PIECE_BINDING_INVALID")
    spec_piece = {
        "remote_media": source_path,
        "start_ms": normalized_piece.get("start_ms"),
        "end_ms": normalized_piece.get("end_ms"),
        "source_media_sha256": normalized_piece["source_media_sha256"],
    }
    if any(isinstance(spec_piece[key], bool) or not isinstance(spec_piece[key], int) for key in ("start_ms", "end_ms")) or spec_piece["end_ms"] <= spec_piece["start_ms"]:
        raise ReviewedBaselineReplayError("REPLAY_SOURCE_PIECE_BINDING_INVALID")
    padded_binding = regular_binding(plan.padded_path, label="PADDED_SOURCE")
    observed_padded_sha = final_recut.get("source_sha256")
    c7b_remote_padded = (
        plan.date == "2026-08-14" and plan.candidate_id == "auto_130040_201_255"
        and record_binding.sha256 == "sha256:b875d8ddedaa47971249e3af057b218f45e62fb002ddcf6c9733cd38d5bfd8f5"
        and provenance_binding.sha256 == "sha256:7b13b5fa7a7f862e90c6e07bbef51a1809b0b9fb2792aeef7dc42d45a866d626"
        and padded_binding.sha256 == "sha256:5b06a7bb19e22c0a8c368c83ee83170ff068b04d32fd07cf246859e8f09014f0"
        and plan.expected_video_sha256 == "sha256:09c42e6cab8b35891f7b307dcfd4e23323073f30915ffc36bcbe2f33acdb1798"
        and final_recut.get("source_path") == "/opt/bilive/autoslice/out/2026-08-14/auto_130040_201_255/padded_191190_303140.mp4"
    )
    allowed_padded_outputs = {None, str(plan.padded_path.resolve()), str(plan.padded_path)}
    if c7b_remote_padded:
        allowed_padded_outputs.add("/opt/bilive/autoslice/out/2026-08-14/auto_130040_201_255/padded_191190_303140.mp4")
    if (
        final_recut.get("source_path") != str(plan.padded_path.resolve()) and not c7b_remote_padded
        or not isinstance(observed_padded_sha, str)
        or observed_padded_sha.removeprefix("sha256:") != padded_binding.sha256.removeprefix("sha256:")
        or padded.get("output_path") not in allowed_padded_outputs
    ):
        raise ReviewedBaselineReplayError("REPLAY_PROVENANCE_BINDING_DRIFT")
    boundary = record.get("boundary_audit")
    timing = record.get("subtitle_timing_qa")
    story = record.get("story_contract")
    speaker_mode = record.get("speaker_mode")
    speaker_style = record.get("subtitle_style")
    burned = record.get("burned_preview")
    if (
        not isinstance(boundary, Mapping) or not isinstance(timing, Mapping)
        or not isinstance(story, Mapping) or speaker_mode not in {"auto", "uniform_host"}
        or not isinstance(speaker_style, str) or not speaker_style
    ):
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_INPUT_MISSING")
    authority_resolver = record_authority_resolver or resolve_record_bound_finalizer_authority
    authority = authority_resolver(
        plan=plan, record_binding=record_binding, record=record, runtime_authority_root=runtime_authority_root,
        source_media_sha256=normalized_piece["source_media_sha256"], regular_binding=regular_binding,
        safe_directory=_safe_directory, load_json=_load_json, error=ReviewedBaselineReplayError)
    chat = authority.chat
    clip = authority.clip_context
    private_runtime_root = _mkdir_private(stage / "finalizer-runtime")
    _copy_deployed_authority(
        source_runtime_root=runtime_authority_root,
        private_runtime_root=private_runtime_root,
    )
    # ``prepare_talk_delivery`` defines the runtime as ``output_root.parents[1]``.
    # Keep this layout intentional: <private-runtime>/<date>/<cid>.
    out_root = private_runtime_root / plan.date / plan.candidate_id
    out_root.mkdir(parents=True, mode=0o700)
    _copy_private_artifact(chat.path, out_root / f"{plan.candidate_id}.chat-authority.json")
    _copy_private_artifact(clip.path, out_root / f"{plan.candidate_id}.clip-context.json")
    # Source-fact/addressee evaluation must receive the same sealed release
    # grid that the successor materializes.  The diagnostic grid can contain
    # explicit operator drops and is therefore not a valid release transcript.
    release_text = _read_small_bytes(
        regular_binding(plan.baseline.baseline_path, label="RELEASE_TRUTH"),
        label="RELEASE_TRUTH",
    ).decode("utf-8")
    source_cues = _fresh_srt_to_source_cues(
        release_text, window_start_ms=0,
        duration_ms=spec_piece["end_ms"] - spec_piece["start_ms"],
    )
    final_start = plan.local_start_ms
    final_end = plan.local_end_ms
    from src.autoslice.reviewed_baseline_replay_setup import (
        build_reviewed_baseline_replay_spec,
        resolve_replay_exact_final_reviewer,
    )
    from src.autoslice.reviewed_text_only_speaker_successor import (
        build_text_only_speaker_successor_fields,
    )
    spec = build_reviewed_baseline_replay_spec(
        date=plan.date, candidate_id=plan.candidate_id, output_root=out_root,
        story=story, normalized_piece=normalized_piece, spec_piece=spec_piece,
        clip_context_path=out_root / f"{plan.candidate_id}.clip-context.json",
        clip_context=_load_json(clip, label="CLIP_CONTEXT"),
        baseline_config=plan.baseline.config, boundary=boundary,
        error_factory=ReviewedBaselineReplayError,
        recovery_publication_authority=recovery_publication_authority,
    )
    spec = apply_replay_carry(
        spec, candidate_id=plan.candidate_id, recording_date=plan.date,
        baseline_sha256="sha256:" + str(plan.baseline.config["sha256"]),
    )
    # A text-only reviewed baseline does not authorize new speaker decisions.
    # It may, however, strictly rebind a prior READY artifact when every label,
    # decision, boundary and media binding survives and the sealed ledger names
    # the sole text delta.  This wrapper is private-stage-only: it never points
    # at an installed package or writes a formal target.
    original_speaker_finalizer = getattr(adapters, "run_speaker_finalization", None)
    c5_fields = finalizer_authority_kwargs(
        candidate_id=plan.candidate_id, recording_date=plan.date,
        runtime_root=runtime_authority_root,
    )
    successor_fields = build_text_only_speaker_successor_fields(
        finalizer=finalizer, original_speaker_finalizer=original_speaker_finalizer,
        candidate_id=plan.candidate_id, record=record,
        old_record_sha256=record_binding.sha256,
        baseline_config=plan.baseline.config,
        baseline_manifest_parent=plan.baseline.manifest_path.parent,
        reviewed_baseline_path=plan.baseline.baseline_path,
        reviewed_baseline_sha256="sha256:" + str(plan.baseline.config["sha256"]),
        expected_media_sha256=plan.expected_video_sha256,
        delivery_projection_receipt_path=projection_receipt_path,
        delivery_projection_receipt_sha256=projection_receipt_sha256,
        regular_binding=regular_binding, replay_error=ReviewedBaselineReplayError,
        error_factory=ReviewedBaselineReplayError,
        **c5_fields,
    )
    spec_path = private_runtime_root / "replay-spec.json"
    _write_private(spec_path, _canonical(spec))
    options = ProducerFinalizationOptions(
        spec=spec_path, substrate="reviewed-baseline-replay", correct="reviewed-baseline",
        speaker_mode=str(speaker_mode), speaker_overrides=None,
        speaker_source_session_anchors=None, speaker_mixed_overlap_evidence=None,
        speaker_python=speaker_python, reuse_cover=True, prepare_only=True,
    )
    reviewer, exact_final_entity_verifier = resolve_replay_exact_final_reviewer(
        explicit_reviewer=exact_final_reviewer,
        fallback_reviewer=getattr(adapters, "run_exact_final_review", None),
        use_production=use_production_exact_final_reviewer,
        entity_verifier=exact_final_entity_verifier,
        text_adapters=exact_final_text_adapters, plan=plan, spec=spec,
        candidate_id=plan.candidate_id, spec_piece=spec_piece,
        padded=plan.padded_path, out_root=out_root,
        runtime_authority_root=runtime_authority_root,
        release_text_path=plan.baseline.baseline_path,
        read_text=lambda path: _read_small_bytes(
            regular_binding(path, label="RELEASE_TRUTH"), label="RELEASE_TRUTH"
        ).decode("utf-8"),
        reconstruct_chat=_reconstruct_structured_chat,
        replay_reviewer=replay_exact_final_reviewer,
        provider_invocation=provider_invocation,
        error_factory=ReviewedBaselineReplayError,
    )
    replacement_fields: dict[str, object] = {
        "stage_publish_draft": replay_publish_adapter(
            plan, source_fact_llm=source_fact_llm,
            private_package=out_root / "replacement_recuts",
            runtime_authority_root=runtime_authority_root,
        ),
        "delivery_root": lambda: private_runtime_root / "delivery",
        "run_exact_final_review": reviewer,
    }
    # A test-only custom finalizer owns all finalization behavior and historical
    # seams intentionally provide a smaller adapter dataclass.  Do not add an
    # unknown field to it.  The canonical production finalizer always receives
    # the strict successor wrapper above.
    replacement_fields.update(successor_fields)
    try:
        adapters = replace(
            adapters,
            **replacement_fields,
        )
    except TypeError as exc:
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_ADAPTERS_INVALID") from exc
    # The finalizer's exact-final gate is mandatory.  Replay callers must
    # supply a fresh provider-backed reviewer closure; an old CLEAN receipt is
    # never a substitute for reviewing the newly materialized SRT bytes.
    run = finalizer or finalize_producer_package
    branding_intro = _replay_branding_intro(
        runtime_authority_root=runtime_authority_root, burned=burned
    )
    run(
        options=options, profile_id="lidousha", speaker_subtitle_style_id=speaker_style,
        spec=spec, cid=plan.candidate_id, out_root=out_root, host="localhost",
        padded=plan.padded_path,
        padded_provenance_path=_validated_padded_provenance(plan),
        piece_provenance_rows=[normalized_piece],
        final_start=final_start, final_end=final_end, sanitized=source_cues,
        timing_qa=dict(timing), audit=dict(boundary), text_override_path=None,
        subtitle_regression_path=None, chat_authority_audit=_load_json(chat, label="CHAT_AUTHORITY"),
        chat_authority_path=out_root / f"{plan.candidate_id}.chat-authority.json",
        branding_intro=branding_intro,
        adapters=adapters,
    )
    prepared = sorted(
        (private_runtime_root / ".prepared-deliveries" / "talk" / plan.candidate_id)
        .glob("*/prepared.json")
    )
    if len(prepared) != 1:
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_PREPARED_HANDLE_MISSING")
    prepared_binding = regular_binding(prepared[0], label="PREPARED_HANDLE")
    if prepared_binding is None or not prepared_binding.path.is_relative_to(private_runtime_root):
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_PREPARED_HANDLE_UNSAFE")
    document = _load_json(prepared_binding, label="PREPARED_HANDLE")
    seal = document.get("prepared_sha256")
    if not isinstance(seal, str) or _SHA.fullmatch(seal) is None:
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_PREPARED_HANDLE_INVALID")
    return PrivateReplayFinalization(
        spec_path, private_runtime_root, prepared[0], seal
    )


def flatten_and_audit_private_replay(
    finalization: PrivateReplayFinalization, *, plan: ReplayPlan,
    projection: ReplayLiveProjection | None = None,
    base_package_root: Path | None = None,
    package_postprocessor: Callable[[Path], None] | None = None,
) -> PrivateReplayPackage:
    """Flatten the canonical prepared handle and require the package auditor.

    This consumes only the sealed prepared manifest inside the private runtime;
    no delivery target, record, state, or upload surface is writable here.
    """

    from scripts.audit_lidousha_review_package import AUDIT_POLICY_EPOCH, audit_package
    from scripts.build_manual_review_manifest import DailyManifestError, build_manual
    from src.autoslice.producer_delivery_transaction import PreparedDelivery, _read_document

    candidate_id = plan.candidate_id
    handle = PreparedDelivery(finalization.private_runtime_root, "talk", candidate_id,
                              finalization.prepared_sha256.removeprefix("sha256:"), finalization.prepared_manifest)
    document = _read_document(handle)
    if document.get("lane") != "talk" or document.get("candidate_id") != candidate_id or document.get("upload_enabled") is not False:
        raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
    # Audit the complete intended package closure, including nested carried
    # cover inputs.  A tiny flattened subset can pass while the real package
    # later has a different audited-input binding.
    package = finalization.private_runtime_root / "audited-live-package"
    _copy_verified_tree(base_package_root or _replay_package_root(plan), package)
    spec = _load_json(regular_binding(finalization.spec_path, label="PRIVATE_SPEC"), label="PRIVATE_SPEC")
    output_root = spec.get("output_root")
    if not isinstance(output_root, str):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_SPEC_INVALID")
    source_package = Path(output_root) / "replacement_recuts"
    if not source_package.is_relative_to(finalization.private_runtime_root):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_SPEC_INVALID")
    _safe_directory(source_package)
    carried_runtime_assets = source_package / ".replay-carried-runtime-assets"
    if carried_runtime_assets.exists() or carried_runtime_assets.is_symlink():
        # These are the sole extra files which need not appear as a delivery
        # artifact role: a hash-closed frozen runtime cover reference is used
        # by the carried cover generation but is not itself a deliverable.
        # Copy the complete safe subtree so the final projected package owns
        # exactly the bytes its mutable cover locator names.
        carried_target = package / carried_runtime_assets.name
        if carried_target.exists() or carried_target.is_symlink():
            raise ReviewedBaselineReplayError("REPLAY_CARRIED_ASSET_NAMESPACE_COLLISION")
        _copy_verified_tree(carried_runtime_assets, carried_target)
    seen: set[str] = set()
    projected_sources = (
        {
            "record": projection.record.path,
            "publish": projection.publish.path,
            "chat_authority": projection.chat.path,
            "speaker_manifest": projection.speaker_manifest.path,
        }
        if projection is not None else {}
    )
    copied: dict[str, Path] = {}
    for entry in document.get("artifacts", []):
        if not isinstance(entry, Mapping):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
        role = entry.get("role")
        raw = entry.get("source_path")
        staged_raw = entry.get("staged_path")
        if not isinstance(role, str) or not isinstance(raw, str) or not isinstance(staged_raw, str) or role in seen:
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INVALID")
        seen.add(role)
        raw_source = Path(raw)
        source = projected_sources.get(role, Path(staged_raw))
        binding = regular_binding(source, label="PREPARED_ARTIFACT")
        # Projected authority documents deliberately receive new locator and
        # dependency hashes.  All other artifacts must still equal the
        # prepared handle byte-for-byte.
        if binding is None or (role not in projected_sources and binding.sha256 != entry.get("staged_sha256")):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_DRIFT")
        # Preserve the private finalizer's exact canonical basename.  In
        # particular, historical producer records legitimately use
        # ``<cid>.recut.*`` rather than the older hand-built ``<cid>.*``
        # convention; deriving names from role suffixes would create a
        # package whose record points at files we never install.
        if raw_source.is_relative_to(source_package):
            relative = raw_source.relative_to(source_package)
            destination = package / relative
        else:
            if raw_source.parent != Path(output_root):
                raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INVALID")
            destination = package / raw_source.name
        _replace_private_artifact(source, destination)
        copied[role] = destination
    # The normal producer's runtime authority is ``<cid>.recut.*``.  The
    # portable manual-review builder intentionally consumes a flat same-stem
    # compatibility view and excludes ``*.recut.publish.json``.  Keep both:
    # canonical recut names are the record's live locators, while these
    # create-only aliases let the audit manifest describe the same sealed
    # bytes without inventing a second content surface.
    flat_suffixes = {
        "video": ".mp4", "subtitle": ".srt", "uniform_host_ass": ".final-sapphire72.ass",
        "speaker_srt": ".speaker.srt", "speaker_ass": ".speaker.ass",
        "speaker_manifest": ".speaker.json", "chat_authority": ".chat-authority.json",
        "redelivery_baseline": ".redelivery-baseline.json", "subtitle_regression": ".subtitle-regression.json",
        "filler_audit": ".filler-audit.json", "text_finalization": ".text-finalization.json",
        "clip_context": ".clip-context.json", "cover_title_mask": ".cover.title-mask.png",
        "cover_pre_overlay": ".cover.pre-overlay.png", "cover_route_background": ".cover.ai-bg.png",
        "publish": ".publish.json", "record": ".record.json", "cover": ".cover.png",
    }
    for role, source in copied.items():
        suffix = flat_suffixes.get(role)
        if suffix is None:
            continue
        target = package / f"{candidate_id}{suffix}"
        if target == source:
            continue
        _replace_private_artifact(source, target)
    if package_postprocessor is not None:
        package_postprocessor(package)
    try:
        manifest = build_manual(package, operator="Codex root", note="Reviewed-baseline replay private preflight; upload remains disabled.")
    except DailyManifestError as exc:
        reason_code = getattr(exc, "reason_code", None)
        if isinstance(reason_code, str) and re.fullmatch(
            r"SPEAKER_[A-Z0-9_]{2,159}", reason_code
        ):
            raise ReviewedBaselineReplayError(f"REPLAY_PRIVATE_MANIFEST_{reason_code}") from exc
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_MANIFEST_BLOCKED") from exc
    except Exception as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_MANIFEST_BLOCKED") from exc
    manifest_path = package / "review_manifest.json"
    _replace_private_artifact(
        _private_control_document(finalization.private_runtime_root, label="review-manifest", document=manifest), manifest_path,
    )
    audit = audit_package(package)
    if (
        audit.get("schema_version") != "lidousha-review-package-audit.v2"
        or audit.get("policy_epoch") != AUDIT_POLICY_EPOCH
        or audit.get("passed") is not True
        # ``authorized_upload`` binds informational issues as well as blocking
        # ones.  A private path in an INFO diagnostic would otherwise make the
        # stored receipt differ from the canonical post-install re-audit.
        or audit.get("issue_count") != 0
        or audit.get("blocking_issue_count") != 0
    ):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PACKAGE_AUDIT_BLOCKED")
    # The private mirror is a byte-for-byte simulation of the actual package
    # target.  Record the real target root; authorized_upload will rerun the
    # auditor after commit and compare this content binding.
    audit["root"] = str(_replay_package_root(plan).resolve())
    audit_path = package / "package-audit.json"
    _replace_private_artifact(
        _private_control_document(finalization.private_runtime_root, label="package-audit", document=audit), audit_path,
    )
    return PrivateReplayPackage(
        package, regular_binding(manifest_path, label="REVIEW_MANIFEST"),
        regular_binding(audit_path, label="PACKAGE_AUDIT"),
        ({"predicate": "FINAL_REVIEW_PACKAGE_AUDIT", "status": "PASS"},
         {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"}),
    )


def _profile_delivery_root(*, runtime_authority_root: Path, date: str) -> Path:
    """Derive the delivery namespace from deployed channel policy, never a row.

    Rejected historical rows intentionally have no ``delivered``/``summary``
    locator.  Treating either as a target authority would both block the replay
    and permit a caller-controlled historical path to become a delivery root.
    The channel profile in the deployed repository is the only authority for
    this lane's final delivery root.
    """

    from src.autoslice.channel_profile import ChannelProfileError, load_channel_profile

    runtime = _safe_directory(runtime_authority_root)
    repo = _safe_directory(runtime / "repo")
    try:
        profile = load_channel_profile(repo)
        root = _safe_directory(profile.delivery_root_for(repo))
    except (ChannelProfileError, OSError) as exc:
        raise ReviewedBaselineReplayError("REPLAY_DELIVERY_PROFILE_INVALID") from exc
    destination = root / date
    if destination.exists() or destination.is_symlink():
        return _safe_directory(destination)
    return destination


def _frozen_locator_allowlist(
    document: Mapping[str, object], *, kind: str, private_runtime_root: Path,
) -> dict[tuple[str, ...], str]:
    """Seal existing frozen absolute evidence values before relocation.

    The uniform-host contract only projects whitelisted mutable locators.  A
    finalizer record can also carry absolute producing-stage paths inside
    frozen command/provenance evidence.  Those values are not consumers: an
    exact pointer allow-list preserves them verbatim and rejects a later
    pointer/value drift.
    """

    from src.autoslice.package_relocation_contract import (
        _is_frozen_pointer,
        is_mutable_pointer,
        walk_strings,
    )

    allowed: dict[tuple[str, ...], str] = {}
    for pointer, value in walk_strings(document):
        if value.startswith("/") and not is_mutable_pointer(kind, pointer):
            if not _is_frozen_pointer(kind, pointer):
                raise ReviewedBaselineReplayError("REPLAY_PRIVATE_LOCATOR_UNPROJECTABLE")
            allowed[pointer] = value
    return allowed


def _project_document_locators(
    document: Mapping[str, object],
    *,
    kind: str,
    mappings: tuple[tuple[str, str], ...],
    private_runtime_root: Path,
) -> dict[str, object]:
    """Apply the shared pointer contract and prove frozen values did not move."""

    from src.autoslice.package_relocation_contract import (
        PackageRelocationError,
        project_uniform_host_locators,
    )

    frozen = _frozen_locator_allowlist(
        document, kind=kind, private_runtime_root=private_runtime_root,
    )
    try:
        projected = project_uniform_host_locators(
            document,
            kind=kind,
            mappings=mappings,
            source_workspace_root=str(private_runtime_root),
            frozen_source_roots=(str(private_runtime_root),),
            frozen_absolute_allowlist=frozen,
        )
    except PackageRelocationError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_LOCATOR_UNPROJECTABLE") from exc
    from src.autoslice.package_relocation_contract import is_mutable_pointer, walk_strings
    projected_strings = dict(walk_strings(projected))
    if any(projected_strings.get(pointer) != value for pointer, value in frozen.items()):
        raise ReviewedBaselineReplayError("REPLAY_FROZEN_LOCATOR_REWRITE")
    private_root = Path(private_runtime_root).absolute()
    for pointer, value in walk_strings(projected):
        if not value.startswith("/") or not Path(value).is_relative_to(private_root):
            continue
        # The contract must have rewritten every runtime locator.  A private
        # path may remain only as the exact immutable provenance value whose
        # pointer was explicitly frozen above.
        if is_mutable_pointer(kind, pointer) or frozen.get(pointer) != value:
            raise ReviewedBaselineReplayError(
                f"REPLAY_PRIVATE_LOCATOR_UNPROJECTABLE:{'/'.join(pointer)}"
            )
    return projected


def project_private_finalization_to_live(
    plan: ReplayPlan, *, finalization: PrivateReplayFinalization,
    runtime_authority_root: Path,
) -> ReplayLiveProjection:
    """Rewrite canonical private record/publish locators to existing targets.

    The allowed mappings are fixed by the private finalizer layout plus the
    deployed channel profile.  No caller or old rejected row supplies a
    target, and any remaining private runtime locator fails closed before a
    transaction is even constructed.
    """

    from src.autoslice.producer_delivery_transaction import PreparedDelivery, _read_document

    live_delivery_root = _profile_delivery_root(
        runtime_authority_root=runtime_authority_root, date=plan.date,
    )
    handle = PreparedDelivery(
        finalization.private_runtime_root, "talk", plan.candidate_id,
        finalization.prepared_sha256.removeprefix("sha256:"), finalization.prepared_manifest,
    )
    document = _read_document(handle)
    entries = document.get("artifacts")
    if not isinstance(entries, list):
        raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
    by_role: dict[str, Mapping[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("role"), str):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
        if entry["role"] in by_role:
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
        by_role[entry["role"]] = entry
    for role in ("record", "publish", "chat_authority", "speaker_manifest"):
        if role not in by_role:
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INVALID")
    spec = _load_json(regular_binding(finalization.spec_path, label="PRIVATE_SPEC"), label="PRIVATE_SPEC")
    private_out_raw = spec.get("output_root")
    if not isinstance(private_out_raw, str):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_SPEC_INVALID")
    private_out = Path(private_out_raw)
    private_delivery = finalization.private_runtime_root / "delivery" / plan.date
    if not private_out.is_relative_to(finalization.private_runtime_root):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_SPEC_INVALID")
    private_package = private_out / "replacement_recuts"
    if not private_package.is_relative_to(finalization.private_runtime_root):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_SPEC_INVALID")
    mappings = (
        (str(private_package), str(_replay_package_root(plan))),
        # ``private_out`` is the candidate root, not its date parent.  Mapping
        # it one level too high would put record-owned chat/clip sidecars next
        # to a sibling candidate and later make the state-last target map
        # reject its own projected record.
        (str(private_out), str(plan.package_root)),
        (
            str(finalization.private_runtime_root / "repo"),
            str(_safe_directory(runtime_authority_root) / "repo"),
        ),
    )
    projection = _mkdir_private(finalization.private_runtime_root / "live-projection")

    def staged_document(role: str, *, kind: str) -> dict[str, object]:
        staged_raw = by_role[role].get("staged_path")
        staged_sha = by_role[role].get("staged_sha256")
        if not isinstance(staged_raw, str) or not isinstance(staged_sha, str):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INVALID")
        staged = regular_binding(Path(staged_raw), label="PREPARED_ARTIFACT")
        if staged.sha256 != staged_sha:
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_DRIFT")
        original = _load_json(staged, label=f"PRIVATE_{role.upper()}")
        relocated = _project_document_locators(
            original, kind=kind, mappings=mappings,
            private_runtime_root=finalization.private_runtime_root,
        )
        if not isinstance(relocated, dict):
            raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PROJECTION_INVALID")
        return relocated

    def projected_source_target(role: str) -> Path:
        """Project the prepared source's exact package/candidate basename."""

        raw = by_role[role].get("source_path")
        if not isinstance(raw, str):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INVALID")
        source = Path(raw)
        if source.is_relative_to(private_package):
            relative = source.relative_to(private_package)
            if len(relative.parts) != 1:
                raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INVALID")
            return _replay_package_root(plan) / relative
        if source.parent == private_out:
            return plan.package_root / source.name
        raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INVALID")

    def seal_projected(name: str, document: Mapping[str, object]) -> RegularBinding:
        target = projection / f"{plan.candidate_id}.{name}.json"
        _write_private(target, _canonical(document))
        return regular_binding(target, label=f"PROJECTED_{name.upper()}")

    # These documents are dependencies of the record.  Never seal a record
    # while it still points at the private bytes that predated locator
    # projection.
    speaker_document = staged_document("speaker_manifest", kind="speaker")
    speaker = seal_projected("speaker", speaker_document)
    chat_document = staged_document("chat_authority", kind="chat")
    chat_speaker_sha = chat_document.get("speaker_manifest_sha256")
    if chat_speaker_sha is not None:
        if not isinstance(chat_speaker_sha, str):
            raise ReviewedBaselineReplayError("REPLAY_CHAT_SPEAKER_BINDING_INVALID")
        chat_document["speaker_manifest_sha256"] = speaker.sha256.removeprefix("sha256:")
    chat = seal_projected("chat", chat_document)
    publish_document = staged_document("publish", kind="publish")
    publish_hashes = publish_document.get("artifact_hashes")
    if (
        not isinstance(publish_hashes, Mapping)
        or not isinstance(publish_hashes.get("chat_authority_audit_sha256"), str)
    ):
        raise ReviewedBaselineReplayError("REPLAY_PUBLISH_MIRROR_BINDING_INVALID")
    rebound_publish_hashes = dict(publish_hashes)
    rebound_publish_hashes["chat_authority_audit_sha256"] = chat.sha256
    publish_document["artifact_hashes"] = rebound_publish_hashes
    publish = seal_projected("publish", publish_document)
    record_document = staged_document("record", kind="record")
    hashes = record_document.get("artifact_hashes")
    staging = record_document.get("publish_staging")
    if (
        not isinstance(hashes, Mapping) or not isinstance(staging, Mapping)
    ):
        raise ReviewedBaselineReplayError("REPLAY_RECORD_MIRROR_BINDING_INVALID")
    # The standalone projected manifest is canonical.  The record's embedded
    # mirror must be regenerated from it rather than trusted as a stale copy.
    record_document["speaker_finalization"] = speaker_document
    expected_chat_path = record_document.get("chat_authority_audit_path")
    expected_publish_path = staging.get("publish_json_path")
    expected_chat_target = projected_source_target("chat_authority")
    expected_publish_target = projected_source_target("publish")
    if (
        not isinstance(expected_chat_path, str)
        or not isinstance(expected_publish_path, str)
        or expected_chat_path != str(expected_chat_target)
        or expected_publish_path != str(expected_publish_target)
    ):
        raise ReviewedBaselineReplayError("REPLAY_RECORD_MIRROR_BINDING_INVALID")
    rebound_hashes = dict(hashes)
    if (
        not isinstance(rebound_hashes.get("chat_authority_audit_sha256"), str)
        or not isinstance(rebound_hashes.get("publish_draft_sha256"), str)
        or not isinstance(record_document.get("speaker_finalization_manifest_sha256"), str)
        or not isinstance(record_document.get("speaker_finalization_manifest_path"), str)
    ):
        raise ReviewedBaselineReplayError("REPLAY_RECORD_MIRROR_BINDING_INVALID")
    rebound_hashes["chat_authority_audit_sha256"] = chat.sha256
    rebound_hashes["publish_draft_sha256"] = publish.sha256
    if {
        key: value for key, value in rebound_hashes.items()
        if key != "publish_draft_sha256"
    } != rebound_publish_hashes:
        raise ReviewedBaselineReplayError("REPLAY_RECORD_PUBLISH_HASH_MIRROR_DRIFT")
    record_document["artifact_hashes"] = rebound_hashes
    record_document["speaker_finalization_manifest_sha256"] = speaker.sha256
    from src.autoslice.package_publish_mirror import (
        PUBLISH_NON_STAGING_KEYS,
        validate_publish_staging_mirror,
    )
    rebuilt_staging = {
        key: deepcopy(value)
        for key, value in publish_document.items()
        if key not in PUBLISH_NON_STAGING_KEYS
    }
    rebuilt_staging["publish_json_path"] = expected_publish_path
    rebuilt_staging["status"] = staging.get("status")
    record_document["publish_staging"] = rebuilt_staging
    try:
        validate_publish_staging_mirror(record_document, publish_document)
    except ValueError as exc:
        raise ReviewedBaselineReplayError("REPLAY_RECORD_MIRROR_BINDING_INVALID") from exc
    record = seal_projected("record", record_document)
    return ReplayLiveProjection(
        record=record, publish=publish, chat=chat, speaker_manifest=speaker,
        private_delivery_root=private_delivery, live_delivery_root=live_delivery_root,
    )


def _state_document(path: Path, *, runtime_root: Path) -> tuple[dict[str, object], bytes]:
    """Read the exact state authority through its canonical no-follow gate."""

    from src.autoslice.runner_state_writeback import read_exact_state_preimage

    try:
        raw = read_exact_state_preimage(path, runtime_root=runtime_root)
    except Exception as exc:
        raise ReviewedBaselineReplayError("REPLAY_STATE_PREIMAGE_UNAVAILABLE") from exc
    if raw is None:
        raise ReviewedBaselineReplayError("REPLAY_STATE_PREIMAGE_UNAVAILABLE")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewedBaselineReplayError("REPLAY_STATE_PREIMAGE_INVALID") from exc
    if not isinstance(document, dict):
        raise ReviewedBaselineReplayError("REPLAY_STATE_PREIMAGE_INVALID")
    return document, raw


def _delivery_artifacts_from_prepared(
    plan: ReplayPlan, *, finalization: PrivateReplayFinalization,
    projection: ReplayLiveProjection,
) -> dict[str, dict[str, str]]:
    """Map only sealed private delivery entries onto the profile delivery root."""

    from src.autoslice.producer_delivery_transaction import PreparedDelivery, _read_document

    handle = PreparedDelivery(
        finalization.private_runtime_root, "talk", plan.candidate_id,
        finalization.prepared_sha256.removeprefix("sha256:"), finalization.prepared_manifest,
    )
    document = _read_document(handle)
    rows = document.get("artifacts")
    if not isinstance(rows, list):
        raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
    expected_basename = canonical_talk_delivery_basename(
        str(_load_json(projection.record, label="PROJECTED_RECORD").get("story_contract", {}).get("selection_hook") or ""),
        plan.candidate_id,
        error=ReviewedBaselineReplayError,
    )
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
        role = row.get("role")
        private_target = row.get("target_path")
        staged = row.get("staged_path")
        digest = row.get("staged_sha256")
        if not all(isinstance(value, str) and value for value in (role, private_target, staged, digest)):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
        if role in result:
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
        private_path = Path(private_target)
        if not private_path.is_relative_to(projection.private_delivery_root):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_TARGET_INVALID")
        expected = projection.private_delivery_root / private_path.name
        if private_path != expected or not private_path.name.startswith(expected_basename + "."):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_TARGET_INVALID")
        binding = regular_binding(Path(staged), label="PREPARED_ARTIFACT")
        if binding.sha256 != digest:
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_DRIFT")
        result[role] = {
            "source": str(binding.path),
            "target": str(projection.live_delivery_root / private_path.name),
            "sha256": binding.sha256,
        }
    if not {"video", "subtitle", "cover", "record", "publish", "chat_authority", "speaker_manifest"}.issubset(result):
        raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INCOMPLETE")
    return result


def _prepared_package_names(
    plan: ReplayPlan, *, finalization: PrivateReplayFinalization,
) -> dict[str, str]:
    """Return each prepared role's exact canonical private package basename."""

    from src.autoslice.producer_delivery_transaction import PreparedDelivery, _read_document

    spec = _load_json(regular_binding(finalization.spec_path, label="PRIVATE_SPEC"), label="PRIVATE_SPEC")
    output_root = spec.get("output_root")
    if not isinstance(output_root, str):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_SPEC_INVALID")
    package = Path(output_root) / "replacement_recuts"
    handle = PreparedDelivery(
        finalization.private_runtime_root, "talk", plan.candidate_id,
        finalization.prepared_sha256.removeprefix("sha256:"), finalization.prepared_manifest,
    )
    document = _read_document(handle)
    rows = document.get("artifacts")
    if not isinstance(rows, list):
        raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
    names: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("role"), str) or not isinstance(row.get("source_path"), str):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
        role = str(row["role"])
        if role not in {"record", "publish", "chat_authority", "speaker_manifest", "clip_context"}:
            continue
        path = Path(str(row["source_path"]))
        if role in names or not path.is_relative_to(finalization.private_runtime_root):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
        if path.is_relative_to(package):
            relative = path.relative_to(package)
            names[role] = relative.as_posix()
        elif path.parent == Path(output_root):
            names[role] = path.name
        else:
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INVALID")
    return names


def project_replay_state_after(plan: ReplayPlan, *, runtime_root: Path, state_path: Path, finalization: PrivateReplayFinalization, projection: ReplayLiveProjection, package: PrivateReplayPackage | None = None, sealed_after_image: object | None = None) -> ReplayStateProjection:
    if (plan.date, plan.candidate_id) == ("2026-08-14", "auto_130040_201_255"):
        from src.autoslice.c7b_replay_projector import project_c7b_replay_state_after
        return project_c7b_replay_state_after(plan, runtime_root=runtime_root, state_path=state_path, finalization=finalization, projection=projection, package=package, sealed_after_image=sealed_after_image)
    from src.autoslice.reviewed_baseline_replay_state_projection import project_generic_replay_state_after
    return project_generic_replay_state_after(plan, runtime_root=runtime_root, state_path=state_path, finalization=finalization, projection=projection, package=package, sealed_after_image=sealed_after_image)


def build_replay_after_image(
    plan: ReplayPlan, *, runtime_root: Path, state_path: Path,
    finalization: PrivateReplayFinalization, package: PrivateReplayPackage,
    projection: ReplayLiveProjection,
):
    """Seal the lane-owned live target map for a fully audited private replay.

    This deliberately has no ``target`` or state JSON caller input.  The
    canonical producer handle declares delivery roles; the deployed profile
    declares delivery root; and the current record package declares the
    candidate package root.  The transaction receives only their resulting
    exact map plus one state-last after image.
    """

    from src.autoslice.producer_delivery_transaction import deployment_authority_binding
    from src.autoslice.reviewed_baseline_replay_transaction import (
        ReplayAfterImage, _remove_owned_stage,
        stage_lane_after_image_artifacts,
    )

    runtime = _safe_directory(runtime_root)
    # All fallible authority/state reads precede private transaction staging.
    # If any of these drift, no transaction-owned media namespace is created.
    deployed = deployment_authority_binding(runtime)
    try:
        private_deployed = deployment_authority_binding(finalization.private_runtime_root)
    except Exception as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_DEPLOYED_AUTHORITY_INVALID") from exc
    if private_deployed != deployed:
        # The private finalizer copied its seal before potentially long
        # provider work.  Never let an old-code package inherit a newer live
        # seal merely because the deployment changed while it was preparing.
        raise ReviewedBaselineReplayError("REPLAY_DEPLOYED_AUTHORITY_DRIFT")
    record_before = regular_binding(plan.record_path, label="RECORD").sha256
    stage_binding = regular_binding(package.package_audit.path, label="PACKAGE_AUDIT")
    state = project_replay_state_after(
        plan, runtime_root=runtime, state_path=state_path,
        finalization=finalization, projection=projection, package=package,
    )
    delivery = state.delivered
    sources: dict[str, Path] = {}
    targets: dict[str, Path] = {}

    projected_sources = {
        "record": projection.record.path,
        "publish": projection.publish.path,
        "chat_authority": projection.chat.path,
        "speaker_manifest": projection.speaker_manifest.path,
    }
    for role, row in delivery.items():
        source = projected_sources.get(role, Path(row["source"]))
        sources[f"delivery-{role}"] = source
        targets[f"delivery-{role}"] = Path(row["target"])

    # The candidate package contains the finalizer's ordinary package files
    # plus the manual manifest/audit.  Replace the four private JSON sources
    # with the projected bytes before they become a live consumer surface.
    names = _prepared_package_names(plan, finalization=finalization)
    if not {"record", "publish", "chat_authority", "speaker_manifest", "clip_context"}.issubset(names):
        raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INCOMPLETE")
    package_overrides = {
        names["record"]: projection.record.path,
        names["publish"]: projection.publish.path,
        names["chat_authority"]: projection.chat.path,
        names["speaker_manifest"]: projection.speaker_manifest.path,
    }
    package_files = sorted(
        path for path in package.root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if not package_files:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PACKAGE_EMPTY")
    package_relatives: set[str] = set()
    for index, source in enumerate(package_files):
        binding = regular_binding(source, label="PRIVATE_PACKAGE_ARTIFACT")
        relative = source.relative_to(package.root)
        relative_key = relative.as_posix()
        package_relatives.add(relative_key)
        role = f"package-{index:03d}-{hashlib.sha256(relative_key.encode()).hexdigest()[:16]}"
        if role in sources:
            raise ReviewedBaselineReplayError("REPLAY_AFTER_IMAGE_ROLE_COLLISION")
        sources[role] = package_overrides.get(relative_key, binding.path)
        targets[role] = _replay_package_root(plan) / relative
    # A private package which omitted one projected authority document cannot
    # be audited as the exact live after-image.
    if not set(package_overrides).issubset(package_relatives):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PACKAGE_INCOMPLETE")
    # These two record locators live beside the candidate package, not in the
    # flattened ``replacement_recuts`` directory.  Install their exact
    # projected copies as first-class targets rather than leaving a record to
    # point at a stale/private sidecar.
    record_document = _load_json(projection.record, label="PROJECTED_RECORD")
    chat_target = record_document.get("chat_authority_audit_path")
    clip_target = record_document.get("clip_context_path")
    if not isinstance(chat_target, str) or not isinstance(clip_target, str):
        raise ReviewedBaselineReplayError("REPLAY_RECORD_MIRROR_BINDING_INVALID")
    for role, source, target in (
        ("candidate-chat-authority", projection.chat.path, Path(chat_target)),
        ("candidate-clip-context", Path(delivery["clip_context"]["source"]), Path(clip_target)),
    ):
        exact_candidate_sidecar_target(
            plan.candidate_id, plan.package_root, role=role, target=target,
            error=ReviewedBaselineReplayError,
        )
        if target in targets.values():
            raise ReviewedBaselineReplayError("REPLAY_AFTER_IMAGE_TARGET_COLLISION")
        sources[role] = source
        targets[role] = target
    # The projected document may reference only already-existing deployed repo
    # authority or an exact target in this after-image.  A private finalizer
    # path hidden in a forgotten sidecar would otherwise survive until after
    # the private workspace is cleaned.
    from src.autoslice.package_relocation_contract import (
        CHAT_PATH_POINTERS,
        PUBLISH_PATH_POINTERS,
        RECORD_PATH_POINTERS,
        SPEAKER_PATH_POINTERS,
        get_value,
    )
    target_values = {str(path) for path in targets.values()}
    for document, pointers in (
        (record_document, RECORD_PATH_POINTERS),
        (_load_json(projection.publish, label="PROJECTED_PUBLISH"), PUBLISH_PATH_POINTERS),
        (_load_json(projection.chat, label="PROJECTED_CHAT"), CHAT_PATH_POINTERS),
        (_load_json(projection.speaker_manifest, label="PROJECTED_SPEAKER"), SPEAKER_PATH_POINTERS),
    ):
        for pointer in pointers:
            value = get_value(document, pointer)
            if not isinstance(value, str) or not value.startswith("/"):
                continue
            path = Path(value)
            if path.is_relative_to(_replay_package_root(plan)) or path.is_relative_to(plan.package_root):
                if str(path) not in target_values:
                    raise ReviewedBaselineReplayError("REPLAY_PROJECTED_LOCATOR_TARGET_MISSING")
    artifacts = stage_lane_after_image_artifacts(
        runtime_root=runtime, date=plan.date, candidate_id=plan.candidate_id,
        sources=sources, targets=targets,
    )
    try:
        return ReplayAfterImage(
            date=plan.date, candidate_id=plan.candidate_id,
            deployed=deployed, state_path=Path(state_path),
            state_before=state.before, state_after=state.after,
            record_before_sha256=record_before, stage_sha256=stage_binding.sha256,
            artifacts=artifacts, upload_allowed=False,
        )
    except BaseException:
        # The transaction stage has no journal until the commit lease.  Do not
        # leave a copied media namespace behind if a late lane-owned assembly
        # check/constructor fails after the copy primitive returned.
        _remove_owned_stage(artifacts[0].staged.path.parent, runtime_root=runtime)
        raise


def prepare_replay_after_image(
    plan: ReplayPlan, *, runtime_root: Path, state_path: Path,
    finalization: PrivateReplayFinalization,
):
    """Produce the sole lane-owned audited after-image for replay commit.

    The ordering is material: private locators and dependency hashes are
    projected first; only that self-contained projected clone may receive the
    manual manifest and final package audit.  Auditing the producer's private
    JSON and later replacing it would certify different bytes.
    """

    projection = project_private_finalization_to_live(
        plan, finalization=finalization, runtime_authority_root=runtime_root,
    )
    package = flatten_and_audit_private_replay(
        finalization, plan=plan, projection=projection,
    )
    after = build_replay_after_image(
        plan, runtime_root=runtime_root, state_path=state_path,
        finalization=finalization, package=package, projection=projection,
    )
    return PreparedReplayAfterImage(after=after, projection=projection)


def rebind_replay_after_image_state(
    plan: ReplayPlan, *, runtime_root: Path, state_path: Path,
    finalization: PrivateReplayFinalization, after: object,
    projection: ReplayLiveProjection,
):
    """Refresh only the state CAS image before a later serial batch commit.

    Candidate package preparation is intentionally concurrent.  The first
    successful candidate changes the shared state bytes, however, so a later
    candidate must derive its own state transition from those latest bytes
    before entering its short lease.  Artifact targets remain the already
    sealed candidate-private after-image and are not rebuilt here.
    """

    from src.autoslice.reviewed_baseline_replay_transaction import ReplayAfterImage
    from src.autoslice.producer_delivery_transaction import deployment_authority_binding

    if not isinstance(after, ReplayAfterImage):
        raise ReviewedBaselineReplayError("REPLAY_AFTER_IMAGE_INVALID")
    validate_retained_projection(
        finalization.private_runtime_root / "live-projection",
        (
            ("PROJECTED_RECORD", projection.record),
            ("PROJECTED_PUBLISH", projection.publish),
            ("PROJECTED_CHAT", projection.chat),
            ("PROJECTED_SPEAKER", projection.speaker_manifest),
        ),
        safe_directory=_safe_directory, regular_binding=regular_binding,
        error=ReviewedBaselineReplayError,
    )
    if (
        deployment_authority_binding(finalization.private_runtime_root)
        != deployment_authority_binding(runtime_root)
    ):
        raise ReviewedBaselineReplayError("REPLAY_DEPLOYMENT_AUTHORITY_DRIFT")
    state = project_replay_state_after(
        plan, runtime_root=runtime_root, state_path=state_path,
        finalization=finalization, projection=projection,
        sealed_after_image=after,
    )
    return replace(after, state_before=state.before, state_after=state.after)
