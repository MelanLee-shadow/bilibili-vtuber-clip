"""Private package construction for already-published same-BV recovery.

This lane deliberately has no runner-state transition and no publication side
effect.  It converts a canonical reviewed-baseline private finalization into a
fully audited package under a create-only operator-private root.  The existing
``candidate_rejected`` replay transaction remains unchanged.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from src.autoslice.published_recovery_package_contract import (
    REGISTRY_REPO_PATH,
    REGISTRY_SHA256,
    build_published_recovery_package_receipt,
    load_published_recovery_state_authority,
)
from src.autoslice.recovery_title_authority import (
    build_recovery_publication_authorities,
    expected_recovery_publish_title,
)
from src.autoslice.reviewed_baseline_replay import (
    PrivateReplayFinalization,
    PrivateReplayPackage,
    ReplayLiveProjection,
    ReplayPlan,
    ReviewedBaselineReplayError,
    _copy_private_artifact,
    _copy_verified_tree,
    _replay_package_root,
    _safe_directory,
    flatten_and_audit_private_replay,
    project_private_finalization_to_live,
    regular_binding,
)

_SCHEMA = "published-same-bv-recovery-package.v1"


@dataclass(frozen=True, slots=True)
class PublishedRecoveryPreflight:
    date: str
    candidate_id: str
    bvid: str
    aid: int
    current_cid: int
    title: str
    state_path: Path
    state_sha256: str
    state_bytes: bytes
    source_record_sha256: str
    deployment_authority: Mapping[str, str]
    published_state_authority: Mapping[str, object]
    publication_authority: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PreparedPublishedRecoveryPackage:
    package: PrivateReplayPackage
    projection: ReplayLiveProjection
    receipt: Mapping[str, object]
    package_outer_root: Path | None


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _movable_audit_binding(value: dict[str, object]) -> dict[str, object]:
    from src.autoslice.package_audit_binding import audit_content_binding

    binding = audit_content_binding(value)
    binding.pop("root", None)
    return binding


def _load_object(data: bytes, *, reason: str) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewedBaselineReplayError(reason) from exc
    if not isinstance(value, dict):
        raise ReviewedBaselineReplayError(reason)
    return value


def _positive_int(value: object, *, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReviewedBaselineReplayError(reason)
    return value


def _state_preimage(runtime_root: Path, *, date: str) -> tuple[Path, bytes]:
    from src.autoslice.runner_state_writeback import read_exact_state_preimage

    state_path = runtime_root / "state" / f"{date}.json"
    try:
        raw = read_exact_state_preimage(state_path, runtime_root=runtime_root)
    except Exception as exc:
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_STATE_PREIMAGE_UNAVAILABLE"
        ) from exc
    if raw is None:
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_STATE_PREIMAGE_UNAVAILABLE"
        )
    return state_path, raw


def _deployment_authority(runtime_root: Path) -> dict[str, str]:
    from src.autoslice.producer_delivery_transaction import deployment_authority_binding

    try:
        return dict(deployment_authority_binding(runtime_root))
    except Exception as exc:
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_DEPLOYMENT_AUTHORITY_INVALID"
        ) from exc


def published_recovery_preflight(
    plan: ReplayPlan,
    *,
    runtime_root: Path,
    expected_bvid: str,
) -> PublishedRecoveryPreflight:
    """Bind one current published state row to committed same-BV authority."""

    runtime = _safe_directory(runtime_root)
    if not expected_bvid.startswith("BV") or len(expected_bvid) != 12:
        raise ReviewedBaselineReplayError("PUBLISHED_RECOVERY_BVID_INVALID")
    repo = _safe_directory(runtime / "repo")
    registry = repo / REGISTRY_REPO_PATH
    try:
        authorities = build_recovery_publication_authorities(
            candidate_ids={plan.candidate_id},
            registry_path=registry,
            expected_registry_sha256=REGISTRY_SHA256,
            repo_root=repo,
        )
    except Exception as exc:
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_AUTHORITY_INVALID"
        ) from exc
    authority = authorities[plan.candidate_id]
    authority_bvid = str(authority.get("bvid") or "")
    if authority_bvid != expected_bvid:
        raise ReviewedBaselineReplayError("PUBLISHED_RECOVERY_BVID_MISMATCH")
    try:
        state_authority = load_published_recovery_state_authority(
            repo_root=repo, candidate_id=plan.candidate_id
        )
    except Exception as exc:
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_STATE_AUTHORITY_INVALID"
        ) from exc

    state_path, raw = _state_preimage(runtime, date=plan.date)
    state = _load_object(raw, reason="PUBLISHED_RECOVERY_STATE_PREIMAGE_INVALID")
    picks = state.get("picks")
    if not isinstance(picks, list):
        raise ReviewedBaselineReplayError("PUBLISHED_RECOVERY_STATE_PICK_INVALID")
    matching = [
        row
        for row in picks
        if isinstance(row, dict)
        and str(row.get("candidate_id") or row.get("cid") or "")
        == plan.candidate_id
    ]
    if (
        len(matching) != 1
        or matching[0].get("status") != state_authority["status"]
    ):
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_STATE_PICK_PREIMAGE_DRIFT"
        )
    row = matching[0]
    reconciliation = row.get("publication_reconciliation")
    if not isinstance(reconciliation, Mapping):
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_RECONCILIATION_INVALID"
        )
    title = expected_recovery_publish_title(authority)
    aid = _positive_int(
        reconciliation.get("aid"), reason="PUBLISHED_RECOVERY_RECONCILIATION_INVALID"
    )
    current_cid = _positive_int(
        reconciliation.get("cid"), reason="PUBLISHED_RECOVERY_RECONCILIATION_INVALID"
    )
    if (
        row.get("status") != state_authority["status"]
        or row.get("bvid") != state_authority["bvid"]
        or state_authority["bvid"] != expected_bvid
        or row.get("aid") != state_authority["aid"]
        or state_authority["aid"] != aid
        or row.get("published_cid") != state_authority["published_cid"]
        or reconciliation.get("status")
        != state_authority["reconciliation_status"]
        or reconciliation.get("candidate_id") != plan.candidate_id
        or reconciliation.get("recording_date") != plan.date
        or reconciliation.get("bvid") != expected_bvid
        or current_cid != state_authority["reconciliation_cid"]
        or reconciliation.get("title") != state_authority["title"]
        or state_authority["title"] != title
        or authority.get("aid") != aid
        or authority.get("cid") != state_authority["publication_authority_cid"]
    ):
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_RECONCILIATION_DRIFT"
        )
    source_record = regular_binding(plan.record_path, label="RECOVERY_SOURCE_RECORD")
    deployment = _deployment_authority(runtime)
    return PublishedRecoveryPreflight(
        date=plan.date,
        candidate_id=plan.candidate_id,
        bvid=expected_bvid,
        aid=aid,
        current_cid=current_cid,
        title=title,
        state_path=state_path,
        state_sha256=_sha256(raw),
        state_bytes=raw,
        source_record_sha256=source_record.sha256,
        deployment_authority=deployment,
        published_state_authority=state_authority,
        publication_authority=authority,
    )


def _revalidate_state(
    preflight: PublishedRecoveryPreflight, *, runtime_root: Path
) -> None:
    path, raw = _state_preimage(runtime_root, date=preflight.date)
    if path != preflight.state_path or raw != preflight.state_bytes:
        raise ReviewedBaselineReplayError("PUBLISHED_RECOVERY_STATE_DRIFT")


def _revalidate_inputs(
    plan: ReplayPlan,
    preflight: PublishedRecoveryPreflight,
    *,
    runtime_root: Path,
) -> None:
    _revalidate_state(preflight, runtime_root=runtime_root)
    record = regular_binding(plan.record_path, label="RECOVERY_SOURCE_RECORD")
    if record.sha256 != preflight.source_record_sha256:
        raise ReviewedBaselineReplayError("PUBLISHED_RECOVERY_SOURCE_RECORD_DRIFT")
    runtime = _safe_directory(runtime_root)
    repo = _safe_directory(runtime / "repo")
    try:
        current = build_recovery_publication_authorities(
            candidate_ids={plan.candidate_id},
            registry_path=repo / REGISTRY_REPO_PATH,
            expected_registry_sha256=REGISTRY_SHA256,
            repo_root=repo,
        )[plan.candidate_id]
    except Exception as exc:
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_AUTHORITY_DRIFT"
        ) from exc
    if current != dict(preflight.publication_authority):
        raise ReviewedBaselineReplayError("PUBLISHED_RECOVERY_AUTHORITY_DRIFT")
    try:
        state_authority = load_published_recovery_state_authority(
            repo_root=repo, candidate_id=plan.candidate_id
        )
    except Exception as exc:
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_STATE_AUTHORITY_DRIFT"
        ) from exc
    if state_authority != dict(preflight.published_state_authority):
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_STATE_AUTHORITY_DRIFT"
        )
    try:
        deployment = _deployment_authority(runtime)
    except ReviewedBaselineReplayError as exc:
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_DEPLOYMENT_AUTHORITY_DRIFT"
        ) from exc
    if deployment != dict(preflight.deployment_authority):
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_DEPLOYMENT_AUTHORITY_DRIFT"
        )


def _private_destination(
    path: Path, *, runtime_root: Path, candidate_id: str
) -> tuple[Path, Path]:
    destination = path.absolute()
    if destination.name != candidate_id or destination.exists() or destination.is_symlink():
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_DESTINATION_NOT_CREATE_ONLY"
        )
    parent = _safe_directory(destination.parent)
    metadata = os.stat(parent, follow_symlinks=False)
    mode = stat.S_IMODE(metadata.st_mode)
    if mode != 0o700 or metadata.st_uid != os.geteuid():
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_DESTINATION_PARENT_NOT_PRIVATE"
        )
    runtime = runtime_root.absolute()
    if destination.is_relative_to(runtime):
        relative = destination.relative_to(runtime)
        if not relative.parts or relative.parts[0] in {
            "out", "state", "repo", "delivery", "locks", "journals"
        }:
            raise ReviewedBaselineReplayError(
                "PUBLISHED_RECOVERY_DESTINATION_FORBIDDEN"
            )
    return destination, parent


def _write_create_only(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _preflight_evidence(
    *, plan: ReplayPlan, preflight: PublishedRecoveryPreflight
) -> dict[str, object]:
    return {
        "schema_version": "published-same-bv-recovery-preflight.v1",
        "candidate_id": plan.candidate_id,
        "date": plan.date,
        "state_transition": "none",
        "upload_allowed": False,
        "publication_allowed": False,
        "same_bv_only": True,
        "target": {
            "bvid": preflight.bvid,
            "aid": preflight.aid,
            "current_state_cid": preflight.current_cid,
            "title": preflight.title,
        },
        "production_state": {
            "path": str(preflight.state_path),
            "sha256": preflight.state_sha256,
        },
        "deployment_authority": dict(preflight.deployment_authority),
        "published_state_authority": dict(preflight.published_state_authority),
        "publication_authority_sha256": str(
            preflight.publication_authority.get("authority_sha256") or ""
        ),
        "source_record_sha256": preflight.source_record_sha256,
    }


def _package_receipt(
    *,
    plan: ReplayPlan,
    package_root: Path,
    preflight: PublishedRecoveryPreflight,
    persisted: bool,
    reported_package_root: Path | None = None,
) -> dict[str, object]:
    video = regular_binding(package_root / f"{plan.candidate_id}.mp4", label="RECOVERY_VIDEO")
    subtitle = regular_binding(package_root / f"{plan.candidate_id}.srt", label="RECOVERY_SUBTITLE")
    package_audit = regular_binding(package_root / "package-audit.json", label="RECOVERY_PACKAGE_AUDIT")
    review_manifest = regular_binding(package_root / "review_manifest.json", label="RECOVERY_REVIEW_MANIFEST")
    recovery_preflight = regular_binding(
        package_root / f"{plan.candidate_id}.published-recovery-preflight.json",
        label="RECOVERY_PREFLIGHT_EVIDENCE",
    )
    package_receipt = regular_binding(
        package_root / f"{plan.candidate_id}.published-recovery-package-receipt.json",
        label="RECOVERY_PACKAGE_RECEIPT",
    )
    authority_sha = str(preflight.publication_authority.get("authority_sha256") or "")
    if not authority_sha.startswith("sha256:"):
        raise ReviewedBaselineReplayError("PUBLISHED_RECOVERY_AUTHORITY_INVALID")
    return {
        "schema_version": _SCHEMA,
        "status": "VERIFIED_PRIVATE_PACKAGE" if persisted else "READY_PRIVATE_PACKAGE",
        "candidate_id": plan.candidate_id,
        "date": plan.date,
        "state_transition": "none",
        "upload_allowed": False,
        "publication_allowed": False,
        "same_bv_only": True,
        "target": {
            "bvid": preflight.bvid,
            "aid": preflight.aid,
            "current_state_cid": preflight.current_cid,
            "title": preflight.title,
        },
        "production_state": {
            "path": str(preflight.state_path),
            "sha256": preflight.state_sha256,
        },
        "deployment_authority": dict(preflight.deployment_authority),
        "published_state_authority": dict(preflight.published_state_authority),
        "publication_authority_sha256": authority_sha,
        "source_record_sha256": preflight.source_record_sha256,
        "package_root": str(reported_package_root or package_root) if persisted else None,
        "artifacts": {
            "video": {"sha256": video.sha256, "bytes": video.size},
            "subtitle": {"sha256": subtitle.sha256, "bytes": subtitle.size},
            "package_audit": {
                "sha256": package_audit.sha256, "bytes": package_audit.size
            },
            "review_manifest": {
                "sha256": review_manifest.sha256, "bytes": review_manifest.size
            },
            "recovery_preflight": {
                "sha256": recovery_preflight.sha256,
                "bytes": recovery_preflight.size,
            },
            "package_receipt": {
                "sha256": package_receipt.sha256,
                "bytes": package_receipt.size,
            },
        },
    }


def _remove_owned_tree(path: Path, *, parent: Path) -> None:
    target = path.absolute()
    if target.parent != parent.absolute() or target.is_symlink():
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_TEMP_CLEANUP_UNSAFE"
        )
    for directory, directories, files in os.walk(target, topdown=False, followlinks=False):
        root = Path(directory)
        for name in files:
            item = root / name
            metadata = os.lstat(item)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ReviewedBaselineReplayError(
                    "PUBLISHED_RECOVERY_TEMP_CLEANUP_UNSAFE"
                )
            os.unlink(item)
        for name in directories:
            item = root / name
            metadata = os.lstat(item)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ReviewedBaselineReplayError(
                    "PUBLISHED_RECOVERY_TEMP_CLEANUP_UNSAFE"
                )
            os.rmdir(item)
    os.rmdir(target)


def _rename_noreplace(
    parent_fd: int, source_name: str, destination_name: str
) -> None:
    """Atomically rename one sibling directory without replacing a peer."""

    libc = ctypes.CDLL(None, use_errno=True)
    source = os.fsencode(source_name)
    destination = os.fsencode(destination_name)
    if hasattr(libc, "renameat2"):
        operation = libc.renameat2
        operation.argtypes = (
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        )
        result = operation(parent_fd, source, parent_fd, destination, 1)
    elif hasattr(libc, "renameatx_np"):
        operation = libc.renameatx_np
        operation.argtypes = (
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        )
        result = operation(parent_fd, source, parent_fd, destination, 4)
    else:
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_ATOMIC_NOREPLACE_UNSUPPORTED"
        )
    if result == 0:
        return
    number = ctypes.get_errno()
    if number == errno.EEXIST:
        raise ReviewedBaselineReplayError(
            "PUBLISHED_RECOVERY_DESTINATION_NOT_CREATE_ONLY"
        )
    raise OSError(number, os.strerror(number), destination_name)


def _materialize(
    *,
    plan: ReplayPlan,
    package: PrivateReplayPackage,
    destination_outer: Path,
    preflight: PublishedRecoveryPreflight,
    runtime_root: Path,
) -> tuple[Path, dict[str, object]]:
    destination, parent = _private_destination(
        destination_outer, runtime_root=runtime_root, candidate_id=plan.candidate_id
    )
    temporary = parent / f".{plan.candidate_id}.published-recovery-{uuid.uuid4().hex}"
    os.mkdir(temporary, mode=0o700)
    final_package = destination / "replacement_recuts"
    receipt: dict[str, object] | None = None
    try:
        staged_package = temporary / "replacement_recuts"
        _copy_verified_tree(package.root, staged_package)
        for suffix in ("chat-authority.json", "clip-context.json"):
            _copy_private_artifact(
                package.root / f"{plan.candidate_id}.{suffix}",
                temporary / f"{plan.candidate_id}.{suffix}",
            )
        from scripts.audit_lidousha_review_package import audit_package

        stored_audit = _load_object(
            (staged_package / "package-audit.json").read_bytes(),
            reason="PUBLISHED_RECOVERY_PACKAGE_AUDIT_INVALID",
        )
        staged_audit = audit_package(staged_package)
        if (
            staged_audit.get("passed") is not True
            or staged_audit.get("issue_count") != 0
            or _movable_audit_binding(staged_audit)
            != _movable_audit_binding(stored_audit)
        ):
            raise ReviewedBaselineReplayError(
                "PUBLISHED_RECOVERY_PACKAGE_AUDIT_DRIFT"
            )
        final_audit = dict(staged_audit)
        final_audit["root"] = str(final_package.resolve())
        (staged_package / "package-audit.json").write_bytes(_canonical(final_audit))
        repeated_audit = audit_package(staged_package)
        if (
            repeated_audit.get("passed") is not True
            or repeated_audit.get("issue_count") != 0
            or _movable_audit_binding(repeated_audit)
            != _movable_audit_binding(final_audit)
        ):
            raise ReviewedBaselineReplayError(
                "PUBLISHED_RECOVERY_PACKAGE_AUDIT_DRIFT"
            )
        _revalidate_inputs(plan, preflight, runtime_root=runtime_root)
        receipt = _package_receipt(
            plan=plan,
            package_root=staged_package,
            preflight=preflight,
            persisted=True,
            reported_package_root=final_package,
        )
        _write_create_only(
            temporary / "published-recovery-package.json", _canonical(receipt)
        )
        _revalidate_inputs(plan, preflight, runtime_root=runtime_root)
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_fd = os.open(parent, directory_flags)
        try:
            metadata = os.fstat(parent_fd)
            if (
                stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()
            ):
                raise ReviewedBaselineReplayError(
                    "PUBLISHED_RECOVERY_DESTINATION_PARENT_NOT_PRIVATE"
                )
            _rename_noreplace(parent_fd, temporary.name, destination.name)
        finally:
            os.close(parent_fd)
    except BaseException:
        if temporary.exists() and not temporary.is_symlink():
            _remove_owned_tree(temporary, parent=parent)
        raise
    assert receipt is not None
    return final_package, receipt


def prepare_published_recovery_package(
    plan: ReplayPlan,
    *,
    finalization: PrivateReplayFinalization,
    runtime_root: Path,
    preflight: PublishedRecoveryPreflight,
    package_outer_root: Path,
    persist: bool,
) -> PreparedPublishedRecoveryPackage:
    """Audit and optionally persist one no-state-transition private package."""

    if (
        preflight.candidate_id != plan.candidate_id
        or preflight.date != plan.date
    ):
        raise ReviewedBaselineReplayError("PUBLISHED_RECOVERY_PREFLIGHT_INVALID")
    _revalidate_inputs(plan, preflight, runtime_root=runtime_root)
    target_plan = replace(plan, package_root=package_outer_root.absolute())
    projection = project_private_finalization_to_live(
        target_plan,
        finalization=finalization,
        runtime_authority_root=runtime_root,
    )
    recovery_base = finalization.private_runtime_root / "published-recovery-base-package"
    _copy_verified_tree(_replay_package_root(plan), recovery_base)
    _write_create_only(
        recovery_base / f"{plan.candidate_id}.published-recovery-preflight.json",
        _canonical(_preflight_evidence(plan=plan, preflight=preflight)),
    )
    def add_package_receipt(package_root: Path) -> None:
        receipt = build_published_recovery_package_receipt(
            package_root=package_root,
            candidate_id=plan.candidate_id,
            recovery_publication_authority=preflight.publication_authority,
        )
        _write_create_only(
            package_root / f"{plan.candidate_id}.published-recovery-package-receipt.json",
            _canonical(receipt),
        )

    package = flatten_and_audit_private_replay(
        finalization,
        plan=target_plan,
        projection=projection,
        base_package_root=recovery_base,
        package_postprocessor=add_package_receipt,
    )
    _revalidate_inputs(plan, preflight, runtime_root=runtime_root)
    if persist:
        final_package, receipt = _materialize(
            plan=plan,
            package=package,
            destination_outer=package_outer_root,
            preflight=preflight,
            runtime_root=runtime_root,
        )
        outer = final_package.parent
    else:
        receipt = _package_receipt(
            plan=plan,
            package_root=package.root,
            preflight=preflight,
            persisted=False,
        )
        outer = None
    return PreparedPublishedRecoveryPackage(
        package=package,
        projection=projection,
        receipt=receipt,
        package_outer_root=outer,
    )
