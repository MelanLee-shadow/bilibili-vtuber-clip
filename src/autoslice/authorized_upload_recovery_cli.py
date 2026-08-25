"""Crash-safe completion for manifest-bound uploads.

The uploader process can create a remote archive before local sidecars or the
terminal ledger row are durable.  This module keeps that recovery path out of
the already-large public CLI while preserving its adapters and policy gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable


class UploadRecoveryError(RuntimeError):
    """An explicit-BVID recovery cannot prove the original upload binding."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_sidecar(path: Path, payload: dict) -> None:
    """Durably replace an idempotent sidecar without exposing partial JSON."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    body = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        try:
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while replacing upload sidecar")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    parent_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def manifest_ledger_binding(manifest: dict, manifest_path: Path) -> dict:
    """Project the immutable manifest fields stored by UPLOAD_ATTEMPT_STARTED."""

    attestation = manifest.get("package_attestation") or {}
    return {
        "artifact_id": manifest.get("artifact_id"),
        "video_sha256": (manifest.get("video") or {}).get("sha256"),
        "cover_sha256": (manifest.get("cover") or {}).get("sha256"),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": _sha256_file(manifest_path.resolve()),
        "manifest_version": manifest.get("manifest_version"),
        "package_audit_sha256": (attestation.get("package_audit") or {}).get("sha256"),
        "record_sha256": (attestation.get("record") or {}).get("sha256"),
        "subtitle_sha256": (attestation.get("subtitle") or {}).get("sha256"),
        "review_manifest_sha256": (attestation.get("review_manifest") or {}).get("sha256"),
        "title_cover_qc_sha256": (attestation.get("title_cover_qc") or {}).get("sha256"),
        "title": manifest.get("title"),
        "authorized_by": (manifest.get("authorization") or {}).get("by"),
        "authorization_quote": (manifest.get("authorization") or {}).get("quote"),
        "tags": ",".join(manifest.get("tags") or []),
    }


def _recoverable_started_row(
    ledger: Path,
    manifest: dict,
    manifest_path: Path,
    *,
    read_ledger: Callable[[Path], tuple[list[dict], list[str]]],
) -> dict:
    entries, problems = read_ledger(ledger)
    if problems:
        raise UploadRecoveryError("; ".join(problems))
    started: dict[str, dict] = {}
    finished: set[str] = set()
    for entry in entries:
        attempt_id = entry.get("attempt_id")
        if entry.get("event") == "UPLOAD_ATTEMPT_STARTED":
            started[str(attempt_id)] = entry
        elif entry.get("event") == "UPLOAD_ATTEMPT_FINISHED":
            finished.add(str(attempt_id))
    unresolved = [row for attempt_id, row in started.items() if attempt_id not in finished]
    if len(unresolved) != 1:
        raise UploadRecoveryError(
            f"explicit recovery requires exactly one unresolved upload intent, found {len(unresolved)}"
        )
    row = unresolved[0]
    expected = manifest_ledger_binding(manifest, manifest_path)
    changed = [key for key, value in expected.items() if row.get(key) != value]
    if changed:
        raise UploadRecoveryError(
            f"unresolved upload intent does not match this manifest: {changed}"
        )
    return row


def _started_row_sha256(row: dict) -> str:
    body = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


_LEDGER_STABLE_KEYS = (
    "attempt_id",
    "artifact_id",
    "video_sha256",
    "cover_sha256",
    "manifest",
    "manifest_sha256",
    "uploader",
    "package_audit_sha256",
    "record_sha256",
    "subtitle_sha256",
    "review_manifest_sha256",
    "title_cover_qc_sha256",
    "title",
    "authorized_by",
    "authorization_quote",
    "tags",
)


def append_publication_verified(
    ledger: Path,
    guard_row: dict,
    *,
    bvid: str,
    result: dict | None,
    manifest_path: Path,
    append_ledger: Callable[[Path, dict], None],
    now: Callable[[], str],
    public_verify_sidecar_path: Callable[[Path], Path],
) -> None:
    public_view = (result or {}).get("public_view") or {}
    stable = {
        key: guard_row.get(key)
        for key in _LEDGER_STABLE_KEYS
        if guard_row.get(key) is not None
    }
    append_ledger(
        ledger,
        {
            "event": "UPLOAD_PUBLICATION_VERIFIED",
            "at": now(),
            **stable,
            "rc": 0,
            "bvid": bvid,
            "aid": public_view.get("aid"),
            "cid": public_view.get("cid"),
            "public_verify_status": (result or {}).get("status"),
            "public_verify_sha256": _sha256_file(
                public_verify_sidecar_path(manifest_path)
            ),
        },
    )


def _verify_recovery_bvid_binding(
    manifest: dict,
    bvid: str,
    *,
    http: Callable[..., dict],
    view_api: str,
    tags_api: str,
    member_archive_view_api: str,
    expected_tid: int,
    expected_copyright: int,
    expected_source: str,
    normalise_tags: Callable[[object], list[str]],
) -> dict:
    problems: list[str] = []
    if not (
        isinstance(bvid, str)
        and len(bvid) == 12
        and bvid.startswith("BV")
        and bvid.isalnum()
    ):
        raise UploadRecoveryError("explicit recovery BVID has an invalid shape")
    public = http(view_api.format(bvid=bvid))
    public_data = public.get("data") or {}
    tags_payload = http(tags_api.format(bvid=bvid))
    public_tags = [
        str(row.get("tag_name") or "").strip()
        for row in (tags_payload.get("data") or [])
        if isinstance(row, dict) and str(row.get("tag_name") or "").strip()
    ]
    member = http(member_archive_view_api.format(bvid=bvid))
    archive = (member.get("data") or {}).get("archive") or {}
    aid, cid = public_data.get("aid"), public_data.get("cid")
    if public.get("code") != 0 or public_data.get("state") != 0:
        problems.append("public archive is not available")
    if not aid or not cid:
        problems.append("public archive has no stable aid/cid")
    for field, expected in (
        ("title", manifest.get("title")),
        ("desc", manifest.get("description")),
        ("tid", expected_tid),
        ("copyright", expected_copyright),
    ):
        if public_data.get(field) != expected:
            problems.append(f"public {field} mismatch")
    expected_tags = list(manifest.get("tags") or [])
    if public_tags != expected_tags and set(public_tags) != set(expected_tags):
        problems.append("public tags mismatch")
    if member.get("code") != 0 or not isinstance(archive, dict):
        problems.append("Creator archive is unavailable")
    if archive.get("bvid") != bvid:
        problems.append("Creator archive BVID mismatch")
    if archive.get("aid") != aid:
        problems.append("Creator/public aid mismatch")
    for field, expected in (
        ("title", manifest.get("title")),
        ("desc", manifest.get("description")),
        ("tid", expected_tid),
        ("copyright", expected_copyright),
        ("source", expected_source),
    ):
        if archive.get(field) != expected:
            problems.append(f"Creator archive {field} mismatch")
    member_tags = normalise_tags(archive.get("tag"))
    if member_tags != expected_tags and set(member_tags) != set(expected_tags):
        problems.append("Creator archive tags mismatch")
    if problems:
        raise UploadRecoveryError("; ".join(problems))
    return {"bvid": bvid, "aid": aid, "cid": cid}


def season_add(
    args: Any,
    *,
    default_upload_lock: Path,
    exclusive_upload_lock: Callable[..., Any],
    load_and_verify: Callable[..., tuple[dict | None, list[str]]],
    ledger_guard: Callable[..., tuple[str | None, dict | None, list[str]]],
    read_ledger: Callable[[Path], tuple[list[dict], list[str]]],
    append_ledger: Callable[[Path, dict], None],
    build_season_http: Callable[[Path], tuple[Callable[..., dict], str]],
    run_postpublish_verification: Callable[..., tuple[int, dict | None]],
    run_season_step: Callable[..., int],
    public_verify_sidecar_path: Callable[[Path], Path],
    normalise_tags: Callable[[object], list[str]],
    now: Callable[[], str],
    view_api: str,
    tags_api: str,
    member_archive_view_api: str,
    expected_tid: int,
    expected_copyright: int,
    expected_source: str,
) -> int:
    """Finish one posted manifest, including explicit-BVID crash recovery."""

    manifest_path = Path(args.manifest)
    ledger = Path(args.ledger)
    with exclusive_upload_lock(default_upload_lock):
        manifest, problems = load_and_verify(manifest_path)
        if problems:
            for problem in problems:
                print(f"WARN (season-add continues): {problem}", file=sys.stderr)
            if manifest is None:
                return 2
        status, row, ledger_problems = ledger_guard(
            ledger, manifest["video"]["sha256"]
        )
        if ledger_problems:
            for problem in ledger_problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 5

        bvid = str(args.bvid) if args.bvid else None
        guard_row: dict | None = None
        if bvid and status == "unresolved":
            if manifest.get("manifest_version") != 3:
                print("REFUSE: explicit unresolved recovery requires manifest v3", file=sys.stderr)
                return 5
            try:
                started_row = _recoverable_started_row(
                    ledger,
                    manifest,
                    manifest_path,
                    read_ledger=read_ledger,
                )
                http, _csrf = build_season_http(Path(args.cookie_json))
                binding = _verify_recovery_bvid_binding(
                    manifest,
                    bvid,
                    http=http,
                    view_api=view_api,
                    tags_api=tags_api,
                    member_archive_view_api=member_archive_view_api,
                    expected_tid=expected_tid,
                    expected_copyright=expected_copyright,
                    expected_source=expected_source,
                    normalise_tags=normalise_tags,
                )
            except UploadRecoveryError as exc:
                print(f"REFUSE: {exc}", file=sys.stderr)
                return 5
            except Exception as exc:
                print(
                    f"REFUSE: recovery readback failed: {type(exc).__name__}",
                    file=sys.stderr,
                )
                return 5
            guard_row = {
                "event": "UPLOAD_ATTEMPT_FINISHED",
                "at": now(),
                **{
                    key: value
                    for key, value in started_row.items()
                    if key not in {"event", "at"}
                },
                "uploader_rc": 0,
                "rc": 6,
                "bvid": bvid,
                "aid": binding["aid"],
                "cid": binding["cid"],
                "public_verify_status": "RECOVERY_BOUND_READONLY",
                "recovery_source": "season-add-explicit-bvid",
                "recovery_started_row_sha256": _started_row_sha256(started_row),
            }
            append_ledger(ledger, guard_row)
        elif bvid:
            if status not in {"uploaded", "posted_unverified"} or not row:
                if manifest.get("manifest_version") == 3:
                    print(
                        "REFUSE: manifest v3 explicit BVID has no matching upload ledger row",
                        file=sys.stderr,
                    )
                    return 5
            elif str(row.get("bvid") or "") != bvid:
                print("REFUSE: explicit BVID does not match the upload ledger", file=sys.stderr)
                return 5
            else:
                guard_row = row
        else:
            if status not in {"uploaded", "posted_unverified"} or not row or not row.get("bvid"):
                print(
                    "REFUSE: ledger has no successful upload with a bvid for this manifest's video; "
                    "pass --bvid explicitly if the post exists",
                    file=sys.stderr,
                )
                return 5
            bvid = str(row["bvid"])
            guard_row = row

        if manifest.get("manifest_version") == 3:
            rc, result = run_postpublish_verification(
                manifest, manifest_path, bvid, args
            )
            if rc == 0 and guard_row and guard_row.get("event") == "UPLOAD_ATTEMPT_FINISHED":
                append_publication_verified(
                    ledger,
                    guard_row,
                    bvid=bvid,
                    result=result,
                    manifest_path=manifest_path,
                    append_ledger=append_ledger,
                    now=now,
                    public_verify_sidecar_path=public_verify_sidecar_path,
                )
            return rc
        return run_season_step(manifest, manifest_path, bvid, args)
