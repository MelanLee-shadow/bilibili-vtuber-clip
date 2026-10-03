"""Prepare one authorized upload manifest from a READY_TO_PREPARE row."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.autoslice.publication_authority import (
    PublicationAuthorityError,
    load_qualified_publication_authority,
)
from src.autoslice.publication_readiness import READY_TO_PREPARE

RESULT_SCHEMA_VERSION = "pipeline-publication-manifest-prepare-result.v1"
_REQUIRED_DEPENDENCIES = frozenset(
    {
        "record",
        "publish",
        "burned_video",
        "cover",
        "review_manifest",
        "package_audit",
        "title_cover_qc",
    }
)


def _result(status: str, **extra: object) -> dict[str, object]:
    return {"schema_version": RESULT_SCHEMA_VERSION, "status": status, **extra}


def _safe_regular(value: object, *, runtime_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("publication dependency path missing")
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValueError("publication dependency path must be absolute")
    try:
        relative = raw.absolute().relative_to(runtime_root)
    except ValueError as exc:
        raise ValueError("publication dependency escapes runtime") from exc
    cursor = runtime_root
    try:
        for part in relative.parts:
            cursor /= part
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("publication dependency path contains symlink")
            if cursor != raw and not stat.S_ISDIR(info.st_mode):
                raise ValueError("publication dependency ancestor is not directory")
        info = os.lstat(raw)
    except OSError as exc:
        raise ValueError("publication dependency unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("publication dependency must be single-link regular file")
    return raw.absolute()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("publication dependency JSON invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("publication dependency JSON root must be object")
    return value


def prepare_manifest_from_readiness(
    row: Mapping[str, object],
    *,
    repository_root: str | Path,
    runtime_root: str | Path,
    authorized_upload_call: Callable[[list[str]], int] | None = None,
    manifest_loader: Callable[[Path], tuple[dict | None, list[str]]] | None = None,
) -> dict[str, object]:
    if row.get("category") != READY_TO_PREPARE:
        return _result("BLOCKED_ROW", reason_codes=["ROW_NOT_READY_TO_PREPARE"])
    reasons = row.get("reason_codes")
    if reasons != ["LOCAL_PREPARATION_AVAILABLE"]:
        return _result("BLOCKED_ROW", reason_codes=["ROW_HAS_UNRESOLVED_REASONS"])
    candidate_id = str(row.get("candidate_id") or "")
    if not candidate_id:
        return _result("BLOCKED_ROW", reason_codes=["CANDIDATE_ID_MISSING"])
    dependencies = row.get("package_dependencies")
    if not isinstance(dependencies, Mapping) or not _REQUIRED_DEPENDENCIES <= set(
        dependencies
    ):
        return _result(
            "BLOCKED_ROW",
            reason_codes=["PREPARATION_DEPENDENCIES_MISSING"],
        )
    root = Path(runtime_root).expanduser().resolve(strict=True)
    try:
        paths = {
            key: _safe_regular(dependencies[key], runtime_root=root)
            for key in _REQUIRED_DEPENDENCIES
        }
        publish = _json(paths["publish"])
        record = _json(paths["record"])
        authority = load_qualified_publication_authority(repository_root)
    except (OSError, RuntimeError, ValueError, PublicationAuthorityError) as exc:
        return _result(
            "BLOCKED_INPUT",
            reason_codes=["MANIFEST_PREPARATION_INPUT_INVALID"],
            detail=str(exc)[:512],
        )
    if (
        str(publish.get("candidate_id") or candidate_id) != candidate_id
        or str(record.get("candidate_id") or candidate_id) != candidate_id
    ):
        return _result(
            "BLOCKED_INPUT",
            reason_codes=["MANIFEST_PREPARATION_IDENTITY_MISMATCH"],
        )
    title = str(publish.get("title") or "").strip()
    if not title:
        return _result(
            "BLOCKED_INPUT", reason_codes=["MANIFEST_PREPARATION_TITLE_MISSING"]
        )
    manifest = paths["burned_video"].with_suffix(".upload_manifest.json")
    if manifest.exists() or manifest.is_symlink():
        return _result(
            "BLOCKED_OUTPUT",
            reason_codes=["UPLOAD_MANIFEST_ALREADY_EXISTS"],
            manifest=str(manifest),
        )
    if authorized_upload_call is None or manifest_loader is None:
        from scripts import authorized_upload as uploader

        authorized_upload_call = authorized_upload_call or uploader.main
        manifest_loader = manifest_loader or uploader.load_and_verify
    argv = [
        "make-manifest",
        "--video",
        str(paths["burned_video"]),
        "--cover",
        str(paths["cover"]),
        "--package-audit",
        str(paths["package_audit"]),
        "--title",
        title,
        "--authorized-by",
        str(authority["authorized_by"]),
        "--quote",
        str(authority["verbatim_quote"]),
        "--title-cover-qc",
        str(paths["title_cover_qc"]),
        "--out",
        str(manifest),
    ]
    rc = authorized_upload_call(argv)
    if rc != 0:
        return _result(
            "PREPARATION_BLOCKED",
            rc=rc,
            argv=argv,
            manifest=str(manifest),
            authority=authority,
        )
    parsed, problems = manifest_loader(manifest)
    if parsed is None or problems:
        return _result(
            "PREPARED_MANIFEST_INVALID",
            reason_codes=["PREPARED_MANIFEST_VERIFY_FAILED"],
            detail=problems[:32],
            manifest=str(manifest),
            authority=authority,
        )
    return _result(
        "PREPARED",
        candidate_id=candidate_id,
        manifest=str(manifest),
        authority=authority,
        argv=argv,
        rc=0,
    )


__all__ = ["RESULT_SCHEMA_VERSION", "prepare_manifest_from_readiness"]
