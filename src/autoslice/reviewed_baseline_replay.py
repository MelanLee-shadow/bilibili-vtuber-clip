"""Fail-closed preparation for reviewed-subtitle baseline package replays.

This is deliberately a Talk-only correction lane.  It can prove that an old
record's media bytes can be reconstructed from the sealed padded source and it
can replay the repository-sealed v2 reviewed baseline into a private stage.
It cannot turn that partial stage into a public package: speaker, burn,
title/cover, final-review and package-audit surfaces must all be rebuilt and
sealed before a separate state-last delivery transaction may install anything.

Keeping that distinction explicit prevents the historic failure mode where a
failed rerun overwrote ``*.recut.mp4`` before its record after-image existed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.autoslice.redelivery_subtitle_baseline import (
    apply_redelivery_subtitle_baseline,
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


@dataclass(frozen=True, slots=True)
class PrivateRenderedReplay:
    """Hash-bound private speaker/ASS/burn artifacts for a later after-image."""

    stage: Path
    reviewed_srt: RegularBinding
    speaker_srt: RegularBinding | None
    speaker_ass: RegularBinding | None
    speaker_manifest: RegularBinding | None
    burned_media: RegularBinding
    burned_preview: Mapping[str, object]
    branding_intro: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class PreparedReplayAfterImage:
    """Candidate-private after-image or a typed source-fact prerequisite."""

    status: str
    predicate_matrix: tuple[dict[str, str], ...]
    after_image: object | None


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


def _padded_from_provenance(recut_root: Path) -> tuple[Path, dict[str, Any]]:
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
        raise ReviewedBaselineReplayError("REPLAY_PADDED_SOURCE_ESCAPES_CANDIDATE")
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
    record = _load_json(regular_binding(record_path, label="RECORD"), label="RECORD")
    artifacts = record.get("artifact_hashes")
    expected = artifacts.get("video_sha256") if isinstance(artifacts, Mapping) else None
    if not isinstance(expected, str) or _SHA.fullmatch(expected) is None:
        raise ReviewedBaselineReplayError("REPLAY_RECORD_VIDEO_BINDING_INVALID")
    baseline = _baseline(repo_root=repo_root, candidate_id=candidate_id)
    config = baseline.config
    padded, final = _padded_from_provenance(recut_root)
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
        _fresh_srt_to_source_cues(
            diagnostic_text,
            window_start_ms=int(config["absolute_source_start_ms"]),
            duration_ms=(
                int(config["absolute_source_end_ms"])
                - int(config["absolute_source_start_ms"])
            ),
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ReviewedBaselineReplayError(
            "REPLAY_PIPELINE_DIAGNOSTIC_GEOMETRY_INVALID"
        ) from exc
    matrix = (
        {"predicate": "RECORD_OLD_VIDEO_SHA256", "status": "PASS"},
        {"predicate": "PADDED_SOURCE_BINDING", "status": "PASS"},
        {"predicate": "REVIEWED_BASELINE_V2_V3_LEDGER", "status": "PASS"},
        {"predicate": "PIPELINE_DIAGNOSTIC_HASH_AND_GEOMETRY", "status": "PASS"},
        {"predicate": "SPEAKER_ASS_BURN_REBUILD", "status": "PENDING_STAGE"},
        {"predicate": "TITLE_COVER_PRECONDITIONS", "status": "PENDING_STAGE"},
        {"predicate": "FINAL_REVIEW_AND_PACKAGE_AUDIT", "status": "PENDING_STAGE"},
        {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"},
    )
    return ReplayPlan(
        date, candidate_id, package_root, record_path, padded,
        start, end, expected, baseline, matrix,
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
    if not command:
        # Reuse the production accurate-recut command (including its coarse
        # seek and decode-before-trim path); a superficially equivalent local
        # ffmpeg spelling is not a safe source-bound replay authority.
        command = _accurate_reencode_recut_command(
            source_video=plan.padded_path,
            output_media=media,
            start_ms=plan.local_start_ms,
            duration_ms=plan.local_end_ms - plan.local_start_ms,
        )
    if command[-1] != str(media):
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
    config = plan.baseline.config
    diagnostic = config["operator_truth_lanes"]["pipeline_diagnostic"]
    diagnostic_path = plan.baseline.manifest_path.parent / str(diagnostic["path"])
    diagnostic_binding = regular_binding(diagnostic_path, label="PIPELINE_DIAGNOSTIC")
    try:
        text_value = _read_small_bytes(
            diagnostic_binding, label="PIPELINE_DIAGNOSTIC"
        ).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PIPELINE_DIAGNOSTIC_INVALID") from exc
    match = _PADDED.fullmatch(plan.padded_path.name)
    assert match is not None  # already proved while binding the plan
    padded_start = int(match.group(1))
    baseline_start = int(config["absolute_source_start_ms"])
    baseline_end = int(config["absolute_source_end_ms"])
    try:
        source_cues = _fresh_srt_to_source_cues(
            text_value, window_start_ms=baseline_start,
            duration_ms=baseline_end - baseline_start,
        )
    except ValueError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PIPELINE_DIAGNOSTIC_GEOMETRY_INVALID") from exc
    current_start = padded_start + plan.local_start_ms
    current_end = padded_start + plan.local_end_ms
    automatic = stage / "automatic-window.srt"
    _write_source_range_srt(source_cues, current_start, current_end, automatic)
    automatic_binding = regular_binding(automatic, label="AUTOMATIC_WINDOW")
    try:
        automatic_text = _read_small_bytes(automatic_binding, label="AUTOMATIC_WINDOW").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewedBaselineReplayError("REPLAY_AUTOMATIC_WINDOW_INVALID") from exc
    reviewed, audit = apply_redelivery_subtitle_baseline(
        automatic_text, config=config, spec_parent=plan.baseline.manifest_path.parent,
        current_source_start_ms=current_start,
        current_source_end_ms=current_end,
        current_source_recording_basename=str(config["source_recording_basename"]),
        current_source_sha256=str(config["source_sha256"]),
    )
    if audit.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
        raise ReviewedBaselineReplayError("REPLAY_BASELINE_APPLICATION_FAILED")
    automatic.unlink()
    _write_private(stage / "reviewed.srt", reviewed.encode("utf-8"))
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
    document["stage_sha256"] = _sha(_canonical(document))
    _write_private(stage / "stage.json", _canonical(document))
    return {"stage": str(stage), "stage_sha256": document["stage_sha256"], "predicate_matrix": document["predicate_matrix"]}


def render_private_replay(
    plan: ReplayPlan,
    *,
    stage: Path,
    speaker_mode: str,
    speaker_overrides: Path | None,
    speaker_python: Path,
    branding_intro: Mapping[str, object] | None,
    renderer: Callable[..., tuple[Path, Path, Path | None, Path | None, Path | None, object, Mapping[str, object]]] | None = None,
) -> PrivateRenderedReplay:
    """Reuse the correction renderer with the verified private recut override.

    This never names a package target.  It is the deterministic half of a
    full replay; the source-fact/publication after-image remains separately
    sealed before this material can be committed.
    """

    stage = _safe_directory(stage)
    record = _load_json(regular_binding(plan.record_path, label="RECORD"), label="RECORD")
    media = stage / "recut.mp4"
    reviewed = stage / "reviewed.srt"
    if regular_binding(media, label="STAGED_VIDEO").sha256 != plan.expected_video_sha256:
        raise ReviewedBaselineReplayError("REPLAY_OLD_RECORD_VIDEO_SHA256_MISMATCH")
    if renderer is None:
        from scripts.apply_subtitle_correction import _render_correction_in_staging
        renderer = _render_correction_in_staging
    try:
        render_dir, _srt, speaker_srt, speaker_ass, speaker_manifest, _speaker, reburn = renderer(
            record=record,
            srt=_read_small_bytes(regular_binding(reviewed, label="STAGED_SRT"), label="STAGED_SRT").decode("utf-8"),
            recut_dir=stage,
            candidate_id=plan.candidate_id,
            speaker_mode=speaker_mode,
            speaker_overrides=speaker_overrides,
            speaker_python=speaker_python,
            branding_intro=branding_intro,
            media_source=media,
            stage_parent=stage,
        )
    except Exception as exc:
        raise ReviewedBaselineReplayError("REPLAY_SPEAKER_ASS_BURN_FAILED") from exc
    if not isinstance(reburn, Mapping) or reburn.get("status") != "BURNED":
        raise ReviewedBaselineReplayError("REPLAY_SPEAKER_ASS_BURN_FAILED")
    raw_burned = reburn.get("path")
    if not isinstance(raw_burned, str):
        raise ReviewedBaselineReplayError("REPLAY_SPEAKER_ASS_BURN_FAILED")
    burned = regular_binding(Path(raw_burned), label="BURNED_MEDIA")
    if burned is None or not burned.path.is_relative_to(render_dir):
        raise ReviewedBaselineReplayError("REPLAY_SPEAKER_ASS_BURN_UNSAFE")
    return PrivateRenderedReplay(
        stage=render_dir,
        reviewed_srt=regular_binding(reviewed, label="STAGED_SRT"),
        speaker_srt=regular_binding(speaker_srt, label="SPEAKER_SRT") if speaker_srt else None,
        speaker_ass=regular_binding(speaker_ass, label="SPEAKER_ASS") if speaker_ass else None,
        speaker_manifest=(regular_binding(speaker_manifest, label="SPEAKER_MANIFEST") if speaker_manifest else None),
        burned_media=burned,
        burned_preview=dict(reburn),
        branding_intro=branding_intro,
    )


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


def audit_private_replay_package(*, plan: ReplayPlan, rendered: PrivateRenderedReplay) -> dict[str, object]:
    """Run the canonical auditor against a private complete-package copy.

    This proves exactly why an old package can or cannot be replayed before a
    public after-image exists.  It intentionally returns the auditor's typed
    blockers instead of weakening a title/cover/final-review gate.
    """

    from scripts.audit_lidousha_review_package import audit_package

    root = rendered.stage.parent / "package-audit-input"
    _copy_verified_tree(plan.package_root, root)
    record = _load_json(regular_binding(plan.record_path, label="RECORD"), label="RECORD")
    replacements = {
        record.get("media_path"): rendered.stage.parent / "recut.mp4",
        record.get("subtitle_path"): rendered.reviewed_srt.path,
        (record.get("burned_preview") or {}).get("path") if isinstance(record.get("burned_preview"), Mapping) else None: rendered.burned_media.path,
    }
    for raw_target, source in replacements.items():
        if not isinstance(raw_target, str):
            raise ReviewedBaselineReplayError("REPLAY_RECORD_TARGETS_INVALID")
        live_target = Path(raw_target)
        try:
            relative = live_target.relative_to(plan.package_root)
        except ValueError as exc:
            raise ReviewedBaselineReplayError("REPLAY_RECORD_TARGET_ESCAPES_PACKAGE") from exc
        target = root / relative
        if not target.is_file() or target.is_symlink():
            raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PACKAGE_TARGET_MISSING")
        target.unlink()
        _copy_private_artifact(source, target)
    result = audit_package(root)
    if not isinstance(result, dict):
        raise ReviewedBaselineReplayError("REPLAY_PACKAGE_AUDIT_INVALID")
    _write_private(rendered.stage.parent / "package-audit.json", _canonical(result))
    return result


def stage_private_publish_replay(
    plan: ReplayPlan,
    *,
    rendered: PrivateRenderedReplay,
    source_fact_llm: Callable[..., object],
) -> tuple[dict[str, object], RegularBinding]:
    """Use canonical publish staging with a validated byte-identical cover carry.

    The callback does not manufacture a cover receipt: it reuses only a cover
    generation already bound to the old record and verifies the selected image
    before canonical publish staging rebinds its StoryContract.
    """

    from src.autoslice.publish_staging import _stage_publish_draft
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.story_contract import cover_story_contract_binding

    record = _load_json(regular_binding(plan.record_path, label="RECORD"), label="RECORD")
    staging = record.get("publish_staging")
    if not isinstance(staging, Mapping):
        raise ReviewedBaselineReplayError("REPLAY_PUBLISH_STAGING_MISSING")
    title = staging.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ReviewedBaselineReplayError("REPLAY_FROZEN_TITLE_MISSING")
    generation = staging.get("cover_generation")
    if not isinstance(generation, Mapping):
        raise ReviewedBaselineReplayError("REPLAY_COVER_GENERATION_MISSING")
    cover_path = staging.get("cover_path")
    cover_sha = (record.get("artifact_hashes") or {}).get("cover_sha256") if isinstance(record.get("artifact_hashes"), Mapping) else None
    if not isinstance(cover_path, str) or not isinstance(cover_sha, str):
        raise ReviewedBaselineReplayError("REPLAY_COVER_BINDING_MISSING")
    cover = regular_binding(Path(cover_path), label="COVER")
    if cover is None or cover.sha256 != cover_sha or generation.get("final_cover_sha256") != cover_sha:
        raise ReviewedBaselineReplayError("REPLAY_COVER_BINDING_DRIFT")

    def carry_cover(updated: Mapping[str, object], **_kwargs: object) -> dict[str, object]:
        story = updated.get("story_contract")
        if not isinstance(story, Mapping):
            raise ReviewedBaselineReplayError("REPLAY_STORY_CONTRACT_MISSING")
        carried = dict(generation)
        carried["story_contract"] = cover_story_contract_binding(story)
        return {
            "status": "AI_COVER_READY", "cover_path": str(cover.path),
            "cover_sha256": cover.sha256, "cover_generation": carried,
            "reason_codes": [],
        }

    materialized = dict(record)
    materialized["status"] = "MATERIALIZED"
    materialized["media_path"] = str(rendered.stage.parent / "recut.mp4")
    materialized["subtitle_path"] = str(rendered.reviewed_srt.path)
    materialized["subtitle_ass_path"] = str(rendered.speaker_ass.path) if rendered.speaker_ass else None
    materialized["speaker_review_srt_path"] = str(rendered.speaker_srt.path) if rendered.speaker_srt else None
    hashes = dict(materialized.get("artifact_hashes") or {})
    hashes["video_sha256"] = plan.expected_video_sha256
    hashes["subtitle_sha256"] = rendered.reviewed_srt.sha256
    if rendered.speaker_ass is not None:
        hashes["ass_sha256"] = rendered.speaker_ass.sha256
    materialized["artifact_hashes"] = hashes
    cues = parse_srt_cues(rendered.reviewed_srt.path.read_text(encoding="utf-8"))
    staged = _stage_publish_draft(
        materialized, candidate_id=plan.candidate_id, title=title,
        cues=cues, run_ffmpeg=False, title_llm_call=None,
        art_direction_llm_call=None, skip_cover=False,
        selection_hook=str(staging.get("selection_hook") or record.get("selection_hook") or ""),
        stage_cover=carry_cover, source_fact_llm_call=source_fact_llm,
        private_artifact_root=None,
    )
    if not isinstance(staged, dict) or not isinstance(staged.get("publish_staging"), Mapping):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PUBLISH_FAILED")
    publish_path = Path(str(staged["publish_staging"].get("publish_json_path") or ""))
    binding = regular_binding(publish_path, label="PRIVATE_PUBLISH")
    if binding is None or not binding.path.is_relative_to(rendered.stage):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PUBLISH_UNSAFE")
    return staged, binding


def replay_publish_adapter(
    plan: ReplayPlan, *, source_fact_llm: Callable[..., object]
) -> Callable[..., dict[str, object]]:
    """Return the lane-owned finalizer adapter for frozen title/cover carry.

    ``finalize_producer_package`` still supplies the materialized record and
    source cues.  This wrapper is the only place where old publish surfaces
    are carried, after revalidating their hashes; ordinary producer callers
    cannot pass this capability.
    """

    def stage(record: Mapping[str, object], **kwargs: object) -> dict[str, object]:
        from src.autoslice.publish_staging import _stage_publish_draft
        from src.autoslice.story_contract import cover_story_contract_binding

        old = _load_json(regular_binding(plan.record_path, label="RECORD"), label="RECORD")
        old_staging = old.get("publish_staging")
        if not isinstance(old_staging, Mapping):
            raise ReviewedBaselineReplayError("REPLAY_PUBLISH_STAGING_MISSING")
        title = old_staging.get("title")
        generation = old_staging.get("cover_generation")
        cover_path = old_staging.get("cover_path")
        hashes = old.get("artifact_hashes")
        cover_sha = hashes.get("cover_sha256") if isinstance(hashes, Mapping) else None
        if not isinstance(title, str) or not isinstance(generation, Mapping) or not isinstance(cover_path, str) or not isinstance(cover_sha, str):
            raise ReviewedBaselineReplayError("REPLAY_FROZEN_PUBLICATION_INVALID")
        cover = regular_binding(Path(cover_path), label="COVER")
        if cover is None or cover.sha256 != cover_sha or generation.get("final_cover_sha256") != cover_sha:
            raise ReviewedBaselineReplayError("REPLAY_COVER_BINDING_DRIFT")

        def carry(updated: Mapping[str, object], **_unused: object) -> dict[str, object]:
            story = updated.get("story_contract")
            if not isinstance(story, Mapping):
                raise ReviewedBaselineReplayError("REPLAY_STORY_CONTRACT_MISSING")
            copied = dict(generation)
            copied["story_contract"] = cover_story_contract_binding(story)
            return {"status": "AI_COVER_READY", "cover_path": str(cover.path),
                    "cover_sha256": cover.sha256, "cover_generation": copied,
                    "reason_codes": []}

        return _stage_publish_draft(
            dict(record), candidate_id=plan.candidate_id, title=title,
            cues=kwargs["cues"], run_ffmpeg=bool(kwargs.get("run_ffmpeg")),
            title_llm_call=None, art_direction_llm_call=None, skip_cover=False,
            selection_hook=str(old_staging.get("selection_hook") or old.get("selection_hook") or ""),
            stage_cover=carry, source_fact_llm_call=source_fact_llm,
            private_artifact_root=None,
        ) or {}

    return stage


def synthesize_replay_spec_and_finalize_private(
    plan: ReplayPlan,
    *,
    stage: Path,
    speaker_python: Path,
    source_fact_llm: Callable[..., object],
    adapters: object,
    finalizer: Callable[..., int] | None = None,
) -> PrivateReplayFinalization:
    """Call the canonical producer finalizer in an isolated prepare-only root.

    The synthetic spec is deliberately derived from bound record/provenance
    fields only.  It leaves ``given_title`` and recovery authority null; the
    lane-owned publish adapter carries the already validated frozen surface.
    """

    from src.autoslice.producer_package_finalization import (
        ProducerFinalizationOptions,
        finalize_producer_package,
    )
    from src.autoslice.recut_materialization import _fresh_srt_to_source_cues

    stage = _safe_directory(stage)
    record_binding = regular_binding(plan.record_path, label="RECORD")
    record = _load_json(record_binding, label="RECORD")
    provenance_path = next(plan.package_root.joinpath("replacement_recuts").glob("*.recut.provenance.json"), None)
    if provenance_path is None:
        raise ReviewedBaselineReplayError("REPLAY_PROVENANCE_AMBIGUOUS")
    provenance = _load_json(regular_binding(provenance_path, label="PROVENANCE"), label="PROVENANCE")
    boundary = record.get("boundary_audit")
    timing = record.get("subtitle_timing_qa")
    story = record.get("story_contract")
    speaker_mode = record.get("speaker_mode")
    speaker_style = record.get("subtitle_style")
    burned = record.get("burned_preview")
    chat_path = record.get("chat_authority_audit_path")
    clip_path = record.get("clip_context_path")
    if (
        not isinstance(boundary, Mapping) or not isinstance(timing, Mapping)
        or not isinstance(story, Mapping) or not isinstance(chat_path, str)
        or not isinstance(clip_path, str) or speaker_mode not in {"auto", "uniform_host"}
        or not isinstance(speaker_style, str) or not speaker_style
    ):
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_INPUT_MISSING")
    chat = regular_binding(Path(chat_path), label="CHAT_AUTHORITY")
    clip = regular_binding(Path(clip_path), label="CLIP_CONTEXT")
    hashes = record.get("artifact_hashes")
    if chat is None or clip is None or not isinstance(hashes, Mapping) or hashes.get("chat_authority_audit_sha256") != chat.sha256:
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_AUTHORITY_DRIFT")
    runtime = stage / "finalizer-runtime"
    _mkdir_private(runtime)
    out_root = runtime / "out" / plan.date / plan.candidate_id
    out_root.mkdir(parents=True, mode=0o700)
    _copy_private_artifact(chat.path, out_root / f"{plan.candidate_id}.chat-authority.json")
    _copy_private_artifact(clip.path, out_root / f"{plan.candidate_id}.clip-context.json")
    diagnostic = plan.baseline.config["operator_truth_lanes"]["pipeline_diagnostic"]
    diagnostic_path = plan.baseline.manifest_path.parent / str(diagnostic["path"])
    diagnostic_text = _read_small_bytes(regular_binding(diagnostic_path, label="PIPELINE_DIAGNOSTIC"), label="PIPELINE_DIAGNOSTIC").decode("utf-8")
    source_cues = _fresh_srt_to_source_cues(
        diagnostic_text, window_start_ms=int(plan.baseline.config["absolute_source_start_ms"]),
        duration_ms=int(plan.baseline.config["absolute_source_end_ms"]) - int(plan.baseline.config["absolute_source_start_ms"]),
    )
    final_start = plan.local_start_ms
    final_end = plan.local_end_ms
    spec = {
        "date": plan.date, "candidate_id": plan.candidate_id, "output_root": str(out_root),
        "given_title": None, "recovery_publication_authority": None,
        "selection_hook": str(story.get("selection_hook") or ""),
        "selection_scorecard": story.get("selection_scorecard"),
        "session_relation_authority": story.get("session_relation_authority"),
        "story_contract": dict(story),
        "source_piece": provenance.get("final_recut"),
        "clip_context_path": str(out_root / f"{plan.candidate_id}.clip-context.json"),
        "clip_context": _load_json(clip, label="CLIP_CONTEXT"),
        "pieces": [provenance.get("final_recut", {})],
        "subtitle_redelivery_baseline": plan.baseline.config,
        "boundary_semantic_review": boundary.get("boundary_semantic_review"),
    }
    if story.get("candidate_id") != plan.candidate_id or not spec["selection_hook"]:
        raise ReviewedBaselineReplayError("REPLAY_STORY_CONTRACT_BINDING_DRIFT")
    spec_path = runtime / "replay-spec.json"
    _write_private(spec_path, _canonical(spec))
    options = ProducerFinalizationOptions(
        spec=spec_path, substrate="reviewed-baseline-replay", correct="reviewed-baseline",
        speaker_mode=str(speaker_mode), speaker_overrides=None,
        speaker_source_session_anchors=None, speaker_mixed_overlap_evidence=None,
        speaker_python=speaker_python, reuse_cover=True, prepare_only=True,
    )
    try:
        adapters = replace(
            adapters,
            stage_publish_draft=replay_publish_adapter(plan, source_fact_llm=source_fact_llm),
            delivery_root=lambda: runtime / "delivery",
        )
    except TypeError as exc:
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_ADAPTERS_INVALID") from exc
    run = finalizer or finalize_producer_package
    run(
        options=options, profile_id="lidousha", speaker_subtitle_style_id=speaker_style,
        spec=spec, cid=plan.candidate_id, out_root=out_root, host="localhost",
        padded=plan.padded_path, padded_provenance_path=provenance_path,
        piece_provenance_rows=[dict(provenance.get("final_recut") or {})],
        final_start=final_start, final_end=final_end, sanitized=source_cues,
        timing_qa=dict(timing), audit=dict(boundary), text_override_path=None,
        subtitle_regression_path=None, chat_authority_audit=_load_json(chat, label="CHAT_AUTHORITY"),
        chat_authority_path=out_root / f"{plan.candidate_id}.chat-authority.json",
        branding_intro=(dict(burned.get("branding_intro")) if isinstance(burned, Mapping) and isinstance(burned.get("branding_intro"), Mapping) else None),
        adapters=adapters,
    )
    prepared = sorted((runtime / ".producer-prepared" / "talk" / plan.candidate_id).glob("*/prepared.json"))
    if len(prepared) != 1:
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_PREPARED_HANDLE_MISSING")
    prepared_binding = regular_binding(prepared[0], label="PREPARED_HANDLE")
    if prepared_binding is None or not prepared_binding.path.is_relative_to(runtime):
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_PREPARED_HANDLE_UNSAFE")
    document = _load_json(prepared_binding, label="PREPARED_HANDLE")
    seal = document.get("prepared_sha256")
    if not isinstance(seal, str) or _SHA.fullmatch(seal) is None:
        raise ReviewedBaselineReplayError("REPLAY_FINALIZER_PREPARED_HANDLE_INVALID")
    return PrivateReplayFinalization(spec_path, runtime, prepared[0], seal)


def flatten_and_audit_private_replay(
    finalization: PrivateReplayFinalization, *, candidate_id: str,
) -> PrivateReplayPackage:
    """Flatten the canonical prepared handle and require the package auditor.

    This consumes only the sealed prepared manifest inside the private runtime;
    no delivery target, record, state, or upload surface is writable here.
    """

    from scripts.audit_lidousha_review_package import AUDIT_POLICY_EPOCH, audit_package
    from scripts.build_manual_review_manifest import build_manual
    from src.autoslice.producer_delivery_transaction import PreparedDelivery, _read_document

    handle = PreparedDelivery(finalization.private_runtime_root, "talk", candidate_id,
                              finalization.prepared_sha256.removeprefix("sha256:"), finalization.prepared_manifest)
    document = _read_document(handle)
    if document.get("lane") != "talk" or document.get("candidate_id") != candidate_id or document.get("upload_enabled") is not False:
        raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
    package = finalization.private_runtime_root / "flattened-package"
    _mkdir_private(package)
    suffixes = {
        "video": ".mp4", "subtitle": ".srt", "uniform_host_ass": ".final-sapphire72.ass",
        "speaker_srt": ".speaker.srt", "speaker_ass": ".speaker.ass",
        "speaker_manifest": ".speaker.json", "chat_authority": ".chat-authority.json",
        "redelivery_baseline": ".redelivery-baseline.json", "subtitle_regression": ".subtitle-regression.json",
        "filler_audit": ".filler-audit.json", "text_finalization": ".text-finalization.json",
        "clip_context": ".clip-context.json", "cover_title_mask": ".cover.title-mask.png",
        "cover_pre_overlay": ".cover.pre-overlay.png", "cover_route_background": ".cover.ai-bg.png",
        "publish": ".publish.json", "record": ".record.json", "cover": ".cover.png",
    }
    seen: set[str] = set()
    for entry in document.get("artifacts", []):
        if not isinstance(entry, Mapping):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_HANDLE_INVALID")
        role = entry.get("role")
        raw = entry.get("staged_path")
        if not isinstance(role, str) or not isinstance(raw, str) or role not in suffixes or role in seen:
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INVALID")
        seen.add(role)
        source = Path(raw)
        binding = regular_binding(source, label="PREPARED_ARTIFACT")
        if binding is None or binding.sha256 != entry.get("staged_sha256"):
            raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_DRIFT")
        _copy_private_artifact(source, package / f"{candidate_id}{suffixes[role]}")
    try:
        manifest = build_manual(package, operator="Codex root", note="Reviewed-baseline replay private preflight; upload remains disabled.")
    except Exception as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_MANIFEST_BLOCKED") from exc
    manifest_path = package / "review_manifest.json"
    _write_private(manifest_path, _canonical(manifest))
    audit = audit_package(package)
    if (
        audit.get("schema_version") != "lidousha-review-package-audit.v2"
        or audit.get("policy_epoch") != AUDIT_POLICY_EPOCH
        or audit.get("passed") is not True
        or audit.get("blocking_count") != 0
    ):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PACKAGE_AUDIT_BLOCKED")
    audit_path = package / "package-audit.json"
    _write_private(audit_path, _canonical(audit))
    return PrivateReplayPackage(
        package, regular_binding(manifest_path, label="REVIEW_MANIFEST"),
        regular_binding(audit_path, label="PACKAGE_AUDIT"),
        ({"predicate": "FINAL_REVIEW_PACKAGE_AUDIT", "status": "PASS"},
         {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"}),
    )


def prepare_replay_after_image(
    plan: ReplayPlan,
    *,
    staged: Mapping[str, object],
    rendered: PrivateRenderedReplay | None,
    runtime_root: Path,
    state_path: Path,
    source_fact_llm: Callable[..., object] | None,
) -> PreparedReplayAfterImage:
    """Build the lane-owned, no-upload after-image from sealed private inputs.

    A provider-free invocation returns a terminal ``NEEDS_PROVIDER`` matrix;
    it never writes state, delivery, record, publish, or journal.  When a
    caller injects the normal source-fact adapter, it must return the existing
    typed PASS receipt before we produce a commit-capable object.
    """

    from src.autoslice.reviewed_baseline_replay_transaction import (
        ReplayAfterImage,
        _prepare_artifacts,
        stream_binding,
    )
    from src.autoslice.runner_state_writeback import read_exact_state_preimage, state_bytes

    if rendered is None:
        return PreparedReplayAfterImage(
            "NEEDS_PROVIDER",
            tuple([*plan.matrix, {"predicate": "SOURCE_FACT_REVIEW", "status": "NEEDS_PROVIDER"}]),
            None,
        )
    audit = audit_private_replay_package(plan=plan, rendered=rendered)
    audit_passed = audit.get("passed") is True and audit.get("blocking_count") == 0
    audit_row = {
        "predicate": "FINAL_REVIEW_PACKAGE_AUDIT",
        "status": "PASS" if audit_passed else "BLOCKED",
    }
    record_binding = regular_binding(plan.record_path, label="RECORD")
    record = _load_json(record_binding, label="RECORD")
    if source_fact_llm is None:
        return PreparedReplayAfterImage(
            "NEEDS_PROVIDER",
            tuple([*plan.matrix, audit_row, {"predicate": "SOURCE_FACT_REVIEW", "status": "NEEDS_PROVIDER"}]),
            None,
        )
    try:
        published_record, publish_binding = stage_private_publish_replay(
            plan, rendered=rendered, source_fact_llm=source_fact_llm
        )
    except ReviewedBaselineReplayError:
        raise
    except Exception as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PUBLISH_FAILED") from exc
    staged_publish = published_record.get("publish_staging")
    source_fact = staged_publish.get("source_fact_review") if isinstance(staged_publish, Mapping) else None
    from src.autoslice.source_fact_review import source_fact_review_passes
    if not isinstance(source_fact, Mapping) or not source_fact_review_passes(source_fact):
        return PreparedReplayAfterImage(
            "BLOCKED",
            tuple([*plan.matrix, audit_row, {"predicate": "SOURCE_FACT_REVIEW", "status": "BLOCKED"}]),
            None,
        )
    if not audit_passed:
        return PreparedReplayAfterImage(
            "BLOCKED",
            tuple([*plan.matrix, audit_row, {"predicate": "SOURCE_FACT_REVIEW", "status": "PASS"}]),
            None,
        )
    raw_stage = staged.get("stage")
    if not isinstance(raw_stage, str) or Path(raw_stage) != rendered.stage.parent:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_DRIFT")
    private_root = rendered.stage.parent / "after-image"
    _mkdir_private(private_root)
    artifacts: dict[str, Path] = {}
    artifacts["recut.mp4"] = private_root / "recut.mp4"
    _copy_private_artifact(rendered.stage.parent / "recut.mp4", artifacts["recut.mp4"])
    artifacts["burned.mp4"] = private_root / "burned.mp4"
    _copy_private_artifact(rendered.burned_media.path, artifacts["burned.mp4"])
    artifacts["subtitle.srt"] = private_root / "subtitle.srt"
    _copy_private_artifact(rendered.reviewed_srt.path, artifacts["subtitle.srt"])
    if rendered.speaker_srt is not None:
        artifacts["speaker.srt"] = private_root / "speaker.srt"
        _copy_private_artifact(rendered.speaker_srt.path, artifacts["speaker.srt"])
    if rendered.speaker_ass is not None:
        artifacts["speaker.ass"] = private_root / "speaker.ass"
        _copy_private_artifact(rendered.speaker_ass.path, artifacts["speaker.ass"])
    if rendered.speaker_manifest is not None:
        artifacts["speaker.json"] = private_root / "speaker.json"
        _copy_private_artifact(rendered.speaker_manifest.path, artifacts["speaker.json"])
    after_record = dict(published_record)
    hashes = dict(record.get("artifact_hashes") or {})
    hashes["video_sha256"] = plan.expected_video_sha256
    hashes["subtitle_sha256"] = rendered.reviewed_srt.sha256
    if rendered.speaker_ass is not None:
        hashes["ass_sha256"] = rendered.speaker_ass.sha256
    after_record["artifact_hashes"] = hashes
    after_record["subtitle_source"] = str(record.get("subtitle_source") or "") + "+reviewed_redelivery_baseline"
    after_record["burned_preview"] = {
        **dict(rendered.burned_preview),
        "path": str(record.get("burned_preview", {}).get("path") if isinstance(record.get("burned_preview"), Mapping) else ""),
        "sha256": rendered.burned_media.sha256,
    }
    record_stage = private_root / "record.json"
    _write_private(record_stage, _canonical(after_record))
    artifacts["record.json"] = record_stage
    artifacts["publish.json"] = private_root / "publish.json"
    _copy_private_artifact(publish_binding.path, artifacts["publish.json"])
    media_target = record.get("media_path")
    subtitle_target = record.get("subtitle_path")
    burned_preview = record.get("burned_preview")
    burned_target = burned_preview.get("path") if isinstance(burned_preview, Mapping) else None
    if not isinstance(media_target, str) or not isinstance(subtitle_target, str) or not isinstance(burned_target, str):
        raise ReviewedBaselineReplayError("REPLAY_RECORD_TARGETS_INVALID")
    target_map: dict[str, Path] = {
        "recut.mp4": Path(media_target), "subtitle.srt": Path(subtitle_target),
        "burned.mp4": Path(burned_target), "record.json": plan.record_path,
    }
    old_publish = record.get("publish_staging")
    old_publish_path = old_publish.get("publish_json_path") if isinstance(old_publish, Mapping) else None
    if not isinstance(old_publish_path, str):
        raise ReviewedBaselineReplayError("REPLAY_PUBLISH_TARGET_MISSING")
    target_map["publish.json"] = Path(old_publish_path)
    optional_targets = {
        "speaker.srt": record.get("speaker_review_srt_path"),
        "speaker.ass": record.get("subtitle_ass_path"),
        "speaker.json": record.get("speaker_finalization_manifest_path"),
    }
    for role, target in optional_targets.items():
        if role in artifacts:
            if not isinstance(target, str):
                raise ReviewedBaselineReplayError("REPLAY_RECORD_TARGETS_INVALID")
            target_map[role] = Path(target)
    state_before = read_exact_state_preimage(state_path, runtime_root=runtime_root)
    if state_before is None:
        raise ReviewedBaselineReplayError("REPLAY_STATE_MISSING")
    try:
        state = json.loads(state_before.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewedBaselineReplayError("REPLAY_STATE_INVALID") from exc
    if not isinstance(state, dict) or not isinstance(state.get("picks"), list):
        raise ReviewedBaselineReplayError("REPLAY_STATE_INVALID")
    matches = [row for row in state["picks"] if isinstance(row, dict) and row.get("candidate_id") == plan.candidate_id]
    if len(matches) != 1 or matches[0].get("status") != "candidate_rejected":
        raise ReviewedBaselineReplayError("REPLAY_STATE_CANDIDATE_PREIMAGE_DRIFT")
    row = matches[0]
    row["status"] = "review_ready"
    row["record_path"] = str(plan.record_path)
    row["subtitle_sha256"] = rendered.reviewed_srt.sha256
    row["video_sha256"] = plan.expected_video_sha256
    state_after = state_bytes(state)
    deployed_commit = stream_binding(runtime_root / "repo" / "DEPLOYED_COMMIT", label="DEPLOYED_COMMIT")
    deployed_manifest = stream_binding(runtime_root / "repo" / "DEPLOYED_AUTHORITY_MANIFEST", label="DEPLOYED_MANIFEST")
    if deployed_commit is None or deployed_manifest is None:
        raise ReviewedBaselineReplayError("REPLAY_DEPLOYED_AUTHORITY_MISSING")
    after = ReplayAfterImage(
        date=plan.date, candidate_id=plan.candidate_id,
        deployed={"commit": deployed_commit.sha256, "authority_manifest_sha256": deployed_manifest.sha256},
        state_path=state_path, state_before=state_before, state_after=state_after,
        record_before_sha256=record_binding.sha256,
        stage_sha256=str(staged.get("stage_sha256") or ""),
        artifacts=_prepare_artifacts(stage_root=private_root, targets=target_map),
        upload_allowed=False,
    )
    # Do not expose a commit handle until the canonical package auditor has
    # examined a complete private review package.  The baseline lane cannot
    # fabricate that manifest or title/cover proof from a raw provider mapping.
    # The returned object therefore stays blocked unless the later package
    # materializer replaces this branch with its audited result.
    _ = after
    return PreparedReplayAfterImage(
        "BLOCKED",
        tuple([*plan.matrix, audit_row, {"predicate": "SOURCE_FACT_REVIEW", "status": "PASS"},
               {"predicate": "FINAL_REVIEW_PACKAGE_AUDIT", "status": "BLOCKED"},
               {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"}]),
        None,
    )
