"""Create-only refresh of B2's current audit, mechanical receipt, and manifest.

The accepted E428 media/subtitle/cover bytes are immutable.  This module copies
that package into a new namespace, projects only the repository-approved runtime
locators, and rebuilds current policy-bound delivery receipts.  It never edits
E428, contacts Bilibili/providers, installs source, or performs same-BV writes.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import Any, Mapping

from PIL import __version__ as pillow_version, features

from scripts import authorized_upload
from scripts.audit_review_package import audit_package
from scripts.build_recovery_review_manifest import build_manifest
from scripts.finalize_b2_caption_same_bv_recovery_package import (
    _authority,
    _project_documents,
    _state,
    _write_documents,
)
from src.autoslice.final_subtitle_audio_gate import validate_final_subtitle_audio_check
from src.autoslice.mechanical_delivery_review import (
    build_mechanical_receipt,
    validate_mechanical_receipt,
)
from src.autoslice.package_audit_binding import audit_binding
from src.autoslice.package_import_capacity import copy_capacity
from src.autoslice.review_evidence_store import review_from_bytes
from src.autoslice.title_cover_qc_locator_successor import (
    create_title_cover_qc_locator_successor,
)


CANDIDATE_ID = "auto_120029_1409_1578"
STEM = f"{CANDIDATE_ID}.recut.burned-final-sapphire72"
TITLE = "【李豆沙】打嗝视频里怎么还藏着雷公电母？！"
TARGET_BVID = "BV18dac6iEvJ"
SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")

FINAL_VIDEO_NAME = f"{STEM}.mp4"
FINAL_SRT_NAME = f"{STEM}.srt"
FINAL_ASS_NAME = f"{STEM}.final-sapphire72.ass"
FINAL_COVER_NAME = f"{STEM}.cover.png"
RECORD_NAME = f"{STEM}.record.json"
PUBLISH_NAME = f"{STEM}.publish.json"
CHAT_NAME = f"{STEM}.chat-authority.json"
CLIP_NAME = f"{STEM}.clip-context.json"
QC_NAME = f"{CANDIDATE_ID}.title-cover-joint-qc.json"
AUTHORIZED_MANIFEST_NAME = "B2-AUTHORIZED-SAME-BV-CURRENT.json"

EXPECTED_FINAL_HASHES = {
    FINAL_VIDEO_NAME: "2c309f6a23846ef7d5ed45513b73455fd55b093f1b269caf04e35d87a93ac498",
    FINAL_SRT_NAME: "a0856249250c46623468f5b7735027c7007efa0f88c6b838c5837fe65fdac57f",
    FINAL_ASS_NAME: "8867bbdb5b79635dc839828ee9ed23664c291b81804c8db29defa47f3807b5c1",
    FINAL_COVER_NAME: "338bf8327b3270a2ad1f23839fac4329c18ee24ce39a4ddb1aa674b485f62230",
}

# These are package-instance receipts, not immutable content evidence.  They
# must be rebuilt for the new root/current policy rather than copied as PASS.
STALE_TOP_LEVEL = frozenset(
    {
        "verification",
        "review_manifest.json",
        QC_NAME,
        "B2-AUTHORIZED-SAME-BV.json",
        "b2-caption-recovery-authority.json",
        "b2-caption-recovery-state.json",
        "b2-caption-terminal-recovery-package.json",
        "b2-v7-recovery-locator-successor.json",
    }
)
_METADATA_ALLOWANCE_BYTES = 16 * 1024 * 1024
_MIN_FREE_RESERVE_BYTES = 16 * 1024**3


class B2CurrentDeliveryRefreshError(RuntimeError):
    """The create-only current-delivery successor could not be proven."""


def _need(condition: bool, detail: str) -> None:
    if not condition:
        raise B2CurrentDeliveryRefreshError(detail)


def _pillow_runtime() -> dict[str, object]:
    def version(name: str) -> str | None:
        for function in (
            features.version_module,
            features.version_feature,
            features.version_codec,
        ):
            try:
                value = function(name)
            except (AttributeError, ValueError):
                continue
            if value is not None:
                return str(value)
        return None

    try:
        raqm_available: bool | None = features.check("raqm")
    except ValueError:
        raqm_available = None
    return {
        "python": sys.version,
        "executable": sys.executable,
        "pillow": pillow_version,
        "freetype2": version("freetype2"),
        "raqm": version("raqm"),
        "raqm_available": raqm_available,
        "harfbuzz": version("harfbuzz"),
        "fribidi": version("fribidi"),
    }


def _sha256(path: Path) -> str:
    digest = _evidence_file_sha256(path)
    return digest


def _json(path: Path, *, label: str, review: bool = False) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = (
            review_from_bytes(raw, directory=path.parent)
            if review
            else json.loads(raw.decode("utf-8"))
        )
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise B2CurrentDeliveryRefreshError(f"{label} is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise B2CurrentDeliveryRefreshError(f"{label} must be a JSON object: {path}")
    return value


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _create_only_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _create_only_json(path: Path, value: Mapping[str, object]) -> None:
    _create_only_bytes(path, _json_bytes(value))


def _real_directory(path: Path, *, label: str) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise B2CurrentDeliveryRefreshError(f"{label} is unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise B2CurrentDeliveryRefreshError(f"{label} must be a real directory: {path}")
    return resolved


def _walk_regular_files(root: Path) -> list[Path]:
    files: list[Path] = []

    def visit(directory: Path, *, top: bool) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if top and entry.name in STALE_TOP_LEVEL:
                    continue
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise B2CurrentDeliveryRefreshError(
                        f"source package contains a symlink: {path}"
                    )
                if stat.S_ISDIR(info.st_mode):
                    visit(path, top=False)
                elif stat.S_ISREG(info.st_mode):
                    files.append(path)
                else:
                    raise B2CurrentDeliveryRefreshError(
                        f"source package contains a special file: {path}"
                    )

    visit(root, top=True)
    return files


def copy_plan(source: Path, output: Path) -> list[dict[str, object]]:
    """Return the exact planned file allocations before creating ``output``."""

    source = _real_directory(source, label="source package")
    if os.path.lexists(output):
        raise B2CurrentDeliveryRefreshError(f"create-only output already exists: {output}")
    _real_directory(output.parent, label="output parent")
    entries = [
        {
            "status": "WOULD_COPY",
            "source": str(path),
            "destination": str(output / path.relative_to(source)),
            "bytes": path.stat().st_size,
        }
        for path in _walk_regular_files(source)
    ]
    entries.append(
        {
            "status": "WOULD_COPY",
            "source": "generated-current-delivery-metadata",
            "destination": str(output / ".planned-current-delivery-metadata"),
            "bytes": _METADATA_ALLOWANCE_BYTES,
        }
    )
    return entries


def _copy_capacity_with_project_floor(
    entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    volumes = copy_capacity(entries)
    for volume in volumes:
        planned = int(volume["planned_copy_bytes"])
        available = int(volume["available_bytes"])
        configured = int(volume["reserve_bytes"])
        reserve = max(configured, _MIN_FREE_RESERVE_BYTES)
        required = planned + reserve
        if available < required:
            raise B2CurrentDeliveryRefreshError(
                f"insufficient destination capacity: {volume['path']}: "
                f"available={available} required={required} "
                f"(copy={planned} reserve={reserve})"
            )
        volume["configured_reserve_bytes"] = configured
        volume["reserve_bytes"] = reserve
        volume["required_bytes"] = required
        volume["project_minimum_reserve_applied"] = configured < _MIN_FREE_RESERVE_BYTES
    return volumes


def _copy_package(source: Path, output: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return set(names).intersection(STALE_TOP_LEVEL) if Path(directory) == source else set()

    shutil.copytree(source, output, copy_function=shutil.copy2, ignore=ignore)


def _paths(output: Path) -> dict[str, Path]:
    return {
        "raw_video": output / f"{CANDIDATE_ID}.recut.mp4",
        "final_video": output / FINAL_VIDEO_NAME,
        "srt": output / FINAL_SRT_NAME,

        "plain_srt": output / FINAL_SRT_NAME,
        "ass": output / FINAL_ASS_NAME,
        "cover": output / FINAL_COVER_NAME,
        "ai_background": output / "covers_ai_original/B2-background.png",
        "title_mask": output / "covers_ai_original/B2-cover.title-mask.png",
        "reference": output / f"evidence/{CANDIDATE_ID}.host-only-v4-reference.png",
        "witness": output / f"evidence/{CANDIDATE_ID}.host-only-v4-witness.png",
        "builtin_provenance": output / "builtin_imagegen_source/source.json",
        "chat": output / CHAT_NAME,
        "clip": output / CLIP_NAME,
        "publish": output / PUBLISH_NAME,
        "record": output / RECORD_NAME,
        "audio_witness": output / f"{STEM}.subtitle-audio-witness.srt",
        "audio_provenance": output / f"{STEM}.subtitle-audio-provenance.json",
        "audio_correspondence": output / f"{STEM}.subtitle-audio-correspondence.json",
        "audio_raw": output / f"{STEM}.subtitle-audio-bcut.raw.json",
    }


def _require_artifacts(paths: Mapping[str, Path]) -> None:
    for label, path in paths.items():
        _need(
            path.is_file() and not path.is_symlink(), f"required artifact missing: {label}: {path}"
        )
    for name, expected in EXPECTED_FINAL_HASHES.items():
        actual = _sha256(
            paths[
                {
                    FINAL_VIDEO_NAME: "final_video",
                    FINAL_SRT_NAME: "srt",
                    FINAL_ASS_NAME: "ass",
                    FINAL_COVER_NAME: "cover",
                }[name]
            ]
        )
        _need(actual == expected, f"accepted artifact hash drift: {name}: {actual}")


def _validate_source_manifest(source: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _json(manifest_path, label="source authorized manifest")
    authority = manifest.get("recovery_publication_authority")
    video = manifest.get("video")
    cover = manifest.get("cover")
    if (
        manifest.get("schema_version") != "authorized-upload-manifest.v3"
        or not isinstance(authority, dict)
        or authority.get("candidate_id") != CANDIDATE_ID
        or authority.get("bvid") != TARGET_BVID
        or manifest.get("title") != TITLE
        or not isinstance(video, dict)
        or not isinstance(cover, dict)
        or video.get("sha256") != EXPECTED_FINAL_HASHES[FINAL_VIDEO_NAME]
        or cover.get("sha256") != EXPECTED_FINAL_HASHES[FINAL_COVER_NAME]
        or Path(str(video.get("path") or "")).resolve() != (source / FINAL_VIDEO_NAME).resolve()
        or Path(str(cover.get("path") or "")).resolve() != (source / FINAL_COVER_NAME).resolve()
    ):
        raise B2CurrentDeliveryRefreshError("source authorized manifest scope or bytes drift")
    authorization = manifest.get("authorization")
    tags = manifest.get("tags")
    if (
        not isinstance(authorization, dict)
        or authorization.get("by") != "维护者"
        or not str(authorization.get("quote") or "").strip()
        or not isinstance(tags, list)
        or not tags
        or not all(isinstance(tag, str) and tag.strip() == tag and tag for tag in tags)
    ):
        raise B2CurrentDeliveryRefreshError("source authorization or tags are invalid")
    return manifest


def _write_package_documents(
    *, source: Path, output: Path, paths: Mapping[str, Path], authority: dict[str, object]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    record = _json(paths["record"], label="record", review=True)
    publish = _json(paths["publish"], label="publish", review=True)
    chat = _json(paths["chat"], label="chat authority", review=True)
    _need(record.get("candidate_id") == CANDIDATE_ID, "record candidate drift")
    _need(publish.get("candidate_id") == CANDIDATE_ID, "publish candidate drift")
    _need(chat.get("candidate_id") == CANDIDATE_ID, "chat candidate drift")
    _need(publish.get("title") == TITLE, "publish title drift")
    projected = _project_documents(
        source=source,
        output=output,
        record=record,
        publish=publish,
        chat=chat,
        authority=authority,
        paths=paths,
    )
    return _write_documents(
        paths=paths,
        record=projected[0],
        publish=projected[1],
        chat=projected[2],
    )


def _make_authorized_manifest(
    *,
    package_root: Path,
    audit_path: Path,
    mechanical_path: Path,
    qc_path: Path,
    source_manifest: Mapping[str, Any],
) -> Path:
    output = package_root / "verification" / AUTHORIZED_MANIFEST_NAME
    _need(not os.path.lexists(output), f"authorized manifest output exists: {output}")
    args = argparse.Namespace(
        video=str(package_root / FINAL_VIDEO_NAME),
        cover=str(package_root / FINAL_COVER_NAME),
        package_audit=str(audit_path),
        title=TITLE,
        authorized_by="维护者",
        quote=str(source_manifest["authorization"]["quote"]),
        final_human_review=str(mechanical_path),
        title_cover_qc=str(qc_path),
        tags=",".join(source_manifest["tags"]),
        no_tags=False,
        season="talk",
        out=str(output),
    )
    rc = authorized_upload.make_manifest(args)
    _need(rc == 0 and output.is_file() and not output.is_symlink(), "make-manifest refused")
    manifest, problems = authorized_upload.load_and_verify(output)
    _need(
        manifest is not None and not problems,
        "current authorized manifest verify refused: " + "; ".join(problems),
    )
    authority = manifest.get("recovery_publication_authority")
    _need(
        isinstance(authority, dict) and authority.get("bvid") == TARGET_BVID,
        "manifest target drift",
    )
    return output


def refresh_current_delivery(
    *,
    source_package: Path,
    source_manifest_path: Path,
    output: Path,
    source_commit: str,
) -> dict[str, object]:
    """Build and locally verify a current-policy successor in an absent directory."""

    source = _real_directory(source_package, label="source package")
    output = output.absolute()
    _need(
        SOURCE_COMMIT_RE.fullmatch(source_commit) is not None, "source commit must be full 40-hex"
    )
    source_manifest_path = source_manifest_path.resolve(strict=True)
    _need(
        source_manifest_path.is_relative_to(source), "source manifest must be inside source package"
    )
    source_manifest = _validate_source_manifest(source, source_manifest_path)
    source_audit = audit_package(source)
    _need(
        source_audit.get("passed") is True
        and source_audit.get("blocking_issue_count") == 0
        and not source_audit.get("issues"),
        "source package fails the current canonical audit",
    )
    planned = copy_plan(source, output)
    capacity = _copy_capacity_with_project_floor(planned)
    _copy_package(source, output)

    paths = _paths(output)
    _require_artifacts(paths)
    authority = _authority()
    _need(authority.get("bvid") == TARGET_BVID, "same-BV authority target drift")
    record, publish, _chat = _write_package_documents(
        source=source, output=output, paths=paths, authority=authority
    )
    validate_final_subtitle_audio_check(record, paths["srt"], paths["final_video"], output)

    state = _state(authority)
    _create_only_json(output / "b2-caption-recovery-state.json", state)
    _create_only_json(
        output / "b2-caption-recovery-authority.json",
        {
            "schema_version": "b2-caption-same-bv-recovery-authority-consumption.v1",
            "candidate_id": CANDIDATE_ID,
            "registry_repo_path": authority["registry_repo_path"],
            "registry_sha256": authority["registry_sha256"],
            "source_commit": source_commit,
            "production_adopted": False,
            "recovery_publication_authority": authority,
        },
    )
    review_manifest = build_manifest(
        package_root=output,
        state=state,
        deployed_commit=source_commit,
        created_at=None,
    )
    _create_only_json(output / "review_manifest.json", review_manifest)

    qc_path = output / QC_NAME
    qc = create_title_cover_qc_locator_successor(
        source_package_root=source,
        source_receipt_path=source / QC_NAME,
        destination_package_root=output,
        output_path=qc_path,
        title=TITLE,
    )
    _need(qc.get("status") == "PASS" and qc.get("pass") is True, "projected QC did not pass")

    audit = audit_package(output)
    _need(
        audit.get("passed") is True
        and audit.get("blocking_issue_count") == 0
        and not audit.get("issues"),
        "current successor canonical audit failed",
    )
    verification = output / "verification"
    verification.mkdir(mode=0o700)
    audit_path = verification / "package-audit.json"
    _create_only_json(audit_path, audit)
    _need(audit_binding(audit) == audit_binding(audit_package(output)), "saved audit is stale")

    mechanical = build_mechanical_receipt(output, audit_path)
    validate_mechanical_receipt(
        mechanical,
        output,
        audit_path,
        publication_authority=authority,
    )
    mechanical_path = verification / "mechanical-delivery-review.json"
    _create_only_json(mechanical_path, mechanical)
    final_audit = audit_package(output)
    _need(
        final_audit.get("passed") is True
        and final_audit.get("blocking_issue_count") == 0
        and audit_binding(final_audit) == audit_binding(audit),
        "canonical audit drifted after mechanical receipt",
    )

    authorized_manifest = _make_authorized_manifest(
        package_root=output,
        audit_path=audit_path,
        mechanical_path=mechanical_path,
        qc_path=qc_path,
        source_manifest=source_manifest,
    )
    final_manifest, final_problems = authorized_upload.load_and_verify(authorized_manifest)
    _need(final_manifest is not None and not final_problems, "final local verify refused")
    _require_artifacts(paths)
    _need(
        record.get("recovery_publication_authority") == authority
        and publish.get("recovery_publication_authority") == authority,
        "same-BV authority surface drift",
    )

    return {
        "schema": "b2-current-delivery-refresh.v1",
        "status": "PRIVATE_CURRENT_AUDIT_MECHANICAL_MANIFEST_VERIFY_PASS_PRODUCTION_NOT_INSTALLED",
        "candidate_id": CANDIDATE_ID,
        "target_bvid": TARGET_BVID,
        "source_package": str(source),
        "output_package": str(output),
        "source_commit": source_commit,
        "production_adopted": False,
        "runtime": _pillow_runtime(),
        "source_current_audit": {
            "policy_fingerprint": source_audit["policy_fingerprint"],
            "issue_count": source_audit["issue_count"],
            "blocking_issue_count": source_audit["blocking_issue_count"],
        },
        "current_audit": {
            "path": str(audit_path),
            "sha256": _sha256(audit_path),
            "policy_fingerprint": audit["policy_fingerprint"],
            "issue_count": audit["issue_count"],
            "blocking_issue_count": audit["blocking_issue_count"],
        },
        "mechanical_review": {
            "path": str(mechanical_path),
            "sha256": _sha256(mechanical_path),
            "status": mechanical["status"],
            "fresh_human_full_playback_claimed": False,
            "new_upload_authorized": False,
        },
        "title_cover_qc": {
            "path": str(qc_path),
            "sha256": _sha256(qc_path),
            "status": qc["status"],
            "provider_calls": qc["locator_successor"]["provider_calls"],
        },
        "authorized_manifest": {
            "path": str(authorized_manifest),
            "sha256": _sha256(authorized_manifest),
            "problem_count": len(final_problems),
        },
        "artifacts": {
            name: {
                "path": str(output / name),
                "sha256": _sha256(output / name),
                "bytes": (output / name).stat().st_size,
            }
            for name in EXPECTED_FINAL_HASHES
        },
        "capacity": capacity,
        "operations": {
            "provider_calls": 0,
            "model_calls": 0,
            "media_renders": 0,
            "media_reencodes": 0,
            "network_calls": 0,
            "remote_writes": 0,
            "production_writes": 0,
            "same_bv_actions": 0,
            "uploads": 0,
        },
        "release_effect": "NO_RELEASE_AUTHORITY_CREATED; local current-policy preflight only",
        "next_action": "Install the exact source commit only after legitimate OCI3 capacity recovery, then rerun current verify and the live predecessor plan before any same-BV action.",
    }


def write_result_create_only(path: Path, value: Mapping[str, object]) -> None:
    _create_only_json(path, value)
