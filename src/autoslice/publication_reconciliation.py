"""Idempotent post-publication reconciliation for registry and runner state.

The upload ledger is intentionally not an authority here.  New-BV
reconciliation accepts only the hash-bound manifest plus the complete
``VERIFIED_PUBLIC``/season/uploaded evidence closure.  Same-BV reconciliation
accepts only a create-only ``same-bv-repair-completed.v1`` receipt, or the
narrow unchanged-CID ``same-bv-cover-repair-completed.v1`` receipt.  Each
successful reconciliation writes a durable runtime registry overlay before it
projects the same truth into the deployed registry and every matching daily
state file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

import fcntl


RECONCILIATION_SCHEMA = "publication-reconciliation.v1"
RUNTIME_REGISTRY_SCHEMA = "publication-reconciliation-registry.v1"
STATIC_REGISTRY_SCHEMA = "publication-registry.v1"
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256_RX = re.compile(r"^[0-9a-f]{64}$")


class PublicationReconciliationError(ValueError):
    """A purported public completion cannot safely update local authority."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublicationReconciliationError(
            f"{label} unreadable: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PublicationReconciliationError(f"{label} is not an object")
    return value


def _sha_entry(path: Path) -> dict[str, object]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def _validate_sha_entry(value: object, label: str) -> Path:
    if not isinstance(value, Mapping):
        raise PublicationReconciliationError(f"{label} binding is missing")
    path = Path(str(value.get("path") or ""))
    if not path.is_file():
        raise PublicationReconciliationError(f"{label} missing: {path}")
    actual_sha = sha256_file(path)
    actual_bytes = path.stat().st_size
    if value.get("sha256") != actual_sha or value.get("bytes") != actual_bytes:
        raise PublicationReconciliationError(f"{label} hash/bytes drifted")
    return path.resolve()


def _atomic_write_json(
    path: Path,
    payload: Mapping[str, object],
    *,
    preserve_state_backup: bool = False,
) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    body = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if preserve_state_backup and path.exists():
            os.replace(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _create_or_validate_authority(path: Path, expected: dict) -> dict:
    path = path.resolve()

    def stable_payload(value: object) -> object:
        if isinstance(value, Mapping):
            return {
                str(key): stable_payload(item)
                for key, item in value.items()
                if key
                not in {
                    "verified_at",
                    "uploaded_at",
                    "preupload_quota_evidence",
                    "public_verify_sha256",
                }
            }
        if isinstance(value, list):
            return [stable_payload(item) for item in value]
        return value

    def validate_existing() -> dict:
        actual = _load_object(path, "publication reconciliation authority")
        stable_fields = (
            "schema_version",
            "status",
            "candidate_id",
            "recording_date",
            "bvid",
            "aid",
            "cid",
            "title",
        )
        if any(actual.get(key) != expected.get(key) for key in stable_fields):
            raise PublicationReconciliationError(
                "existing publication reconciliation authority differs"
            )
        actual_evidence = actual.get("evidence")
        expected_evidence = expected.get("evidence")
        if not isinstance(actual_evidence, Mapping) or not isinstance(
            expected_evidence, Mapping
        ):
            raise PublicationReconciliationError(
                "existing publication reconciliation authority has no evidence"
            )
        if actual_evidence.get("manifest") != expected_evidence.get("manifest"):
            raise PublicationReconciliationError(
                "existing publication reconciliation manifest evidence differs"
            )
        for role in ("public_verify", "season_verify", "uploaded"):
            actual_role = actual_evidence.get(role)
            expected_role = expected_evidence.get(role)
            if (
                not isinstance(actual_role, Mapping)
                or not isinstance(expected_role, Mapping)
                or actual_role.get("path") != expected_role.get("path")
                or stable_payload(actual_role.get("payload"))
                != stable_payload(expected_role.get("payload"))
            ):
                raise PublicationReconciliationError(
                    f"existing publication reconciliation {role} evidence differs"
                )
        return actual

    if path.exists() or path.is_symlink():
        return validate_existing()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    body = (
        json.dumps(
            expected,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    created = False
    try:
        try:
            os.link(temporary, path)
            created = True
        except FileExistsError:
            return validate_existing()
    finally:
        temporary.unlink(missing_ok=True)
    if created:
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return expected


def _candidate_and_date(manifest: Mapping[str, object]) -> tuple[str, str]:
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, Mapping):
        raise PublicationReconciliationError(
            "manifest has no package_attestation"
        )
    record_entry = attestation.get("record")
    record_path = _validate_sha_entry(record_entry, "manifest record")
    record = _load_object(record_path, "manifest record")
    story = record.get("story_contract")
    candidate_id = (
        str(story.get("candidate_id") or "")
        if isinstance(story, Mapping)
        else ""
    )
    if not candidate_id:
        candidate_id = str(record.get("delivery_candidate_id") or "")
    if not candidate_id:
        raise PublicationReconciliationError(
            "manifest record has no publication candidate_id"
        )
    package_root = Path(str(attestation.get("package_root") or ""))
    dates = [part for part in package_root.parts if _DATE_RX.fullmatch(part)]
    if len(set(dates)) != 1:
        raise PublicationReconciliationError(
            "cannot resolve one recording date from package_root"
        )
    return candidate_id, dates[0]


def _manifest_binding(manifest: dict, manifest_path: Path) -> dict:
    manifest_path = manifest_path.resolve()
    if _load_object(manifest_path, "authorized manifest") != manifest:
        raise PublicationReconciliationError(
            "in-memory manifest differs from bound manifest bytes"
        )
    return _sha_entry(manifest_path)


def _normalise_tags(value: object) -> list[str]:
    if isinstance(value, str):
        return sorted(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list):
        return sorted(str(part).strip() for part in value if str(part).strip())
    return []


def _validate_new_public_evidence(
    *,
    manifest: dict,
    manifest_path: Path,
    bvid: str,
    public_verify_path: Path,
    season_verify_path: Path,
    uploaded_path: Path,
) -> tuple[dict, int, int]:
    manifest_binding = _manifest_binding(manifest, manifest_path)
    public = _load_object(public_verify_path, "public verify evidence")
    season = _load_object(season_verify_path, "season verify evidence")
    uploaded = _load_object(uploaded_path, "uploaded completion evidence")
    title = str(manifest.get("title") or "")
    video = manifest.get("video") if isinstance(manifest.get("video"), Mapping) else {}
    cover = manifest.get("cover") if isinstance(manifest.get("cover"), Mapping) else {}
    if (
        manifest.get("manifest_version") != 3
        or not title
        or not str(bvid or "")
        or not _SHA256_RX.fullmatch(str(video.get("sha256") or ""))
        or not _SHA256_RX.fullmatch(str(cover.get("sha256") or ""))
    ):
        raise PublicationReconciliationError(
            "authorized manifest publication identity is incomplete"
        )
    public_view = public.get("public_view")
    public_tags = public.get("public_tags")
    member = public.get("member_archive")
    section = public.get("section_api")
    expected = public.get("expected")
    if (
        public.get("schema_version")
        != "authorized-upload-public-verify.v2"
        or public.get("status") != "VERIFIED_PUBLIC"
        or public.get("bvid") != bvid
        or public.get("manifest_title") != title
        or public.get("problems") != []
        or not isinstance(expected, Mapping)
        or not isinstance(public_view, Mapping)
        or public_view.get("code") != 0
        or public_view.get("state") != 0
        or public_view.get("title") != title
        or public_view.get("desc") != expected.get("description")
        or public_view.get("tid") != expected.get("tid")
        or public_view.get("copyright") != expected.get("copyright")
        or not isinstance(public_view.get("aid"), int)
        or isinstance(public_view.get("aid"), bool)
        or not isinstance(public_view.get("cid"), int)
        or isinstance(public_view.get("cid"), bool)
        or not isinstance(member, Mapping)
        or member.get("aid") != public_view.get("aid")
        or member.get("bvid") not in (None, bvid)
        or member.get("title") != title
        or member.get("desc") != expected.get("description")
        or member.get("tid") != expected.get("tid")
        or member.get("copyright") != expected.get("copyright")
        or member.get("source") != expected.get("source")
        or expected.get("title") != title
        or _normalise_tags(public_tags) != _normalise_tags(expected.get("tags"))
        or _normalise_tags(member.get("tag"))
        != _normalise_tags(expected.get("tags"))
    ):
        raise PublicationReconciliationError(
            "public verify evidence is not a complete exact VERIFIED_PUBLIC closure"
        )
    season_block = manifest.get("season")
    if season_block is None:
        if (
            season.get("schema_version")
            != "authorized-upload-season-verify.v1"
            or season.get("status") != "SEASON_OPTED_OUT"
            or season.get("bvid") != bvid
            or season.get("season_binding") is not None
        ):
            raise PublicationReconciliationError(
                "season opt-out evidence is incomplete"
            )
    else:
        if (
            not isinstance(season_block, Mapping)
            or season.get("schema_version")
            != "authorized-upload-season-verify.v1"
            or season.get("status") != "IN_SEASON_PUBLIC"
            or season.get("bvid") != bvid
            or season.get("title") != title
            or season.get("aid") != public_view.get("aid")
            or season.get("cid") != public_view.get("cid")
            or public_view.get("is_season_display") is not True
            or not isinstance(section, Mapping)
            or section.get("code") != 0
            or section.get("episode_match_count") != 1
            or section.get("episode_titles") != [title]
        ):
            raise PublicationReconciliationError(
                "season/exact-section evidence is not complete"
            )
    if (
        uploaded.get("schema_version") != "authorized-upload-result.v3"
        or uploaded.get("status") != "VERIFIED_PUBLIC"
        or uploaded.get("bvid") != bvid
        or uploaded.get("aid") != public_view.get("aid")
        or uploaded.get("cid") != public_view.get("cid")
        or uploaded.get("title") != title
        or uploaded.get("video_sha256") != video.get("sha256")
        or uploaded.get("cover_sha256") != cover.get("sha256")
        or uploaded.get("manifest") != manifest_binding["path"]
        or uploaded.get("manifest_sha256") != manifest_binding["sha256"]
        or uploaded.get("public_verify")
        != str(public_verify_path.resolve())
        or uploaded.get("public_verify_sha256")
        != sha256_file(public_verify_path)
    ):
        raise PublicationReconciliationError(
            "uploaded completion evidence does not bind VERIFIED_PUBLIC bytes"
        )
    evidence = {
        "manifest": manifest_binding,
        "public_verify": {
            **_sha_entry(public_verify_path),
            "payload": public,
        },
        "season_verify": {
            **_sha_entry(season_verify_path),
            "payload": season,
        },
        "uploaded": {**_sha_entry(uploaded_path), "payload": uploaded},
    }
    return evidence, int(public_view["aid"]), int(public_view["cid"])


def _validate_same_bv_completed(
    *,
    completed_path: Path,
    manifest: dict,
    manifest_path: Path,
) -> tuple[dict, str, int | None, int]:
    completed = _load_object(completed_path, "same-BV completed authority")
    manifest_binding = _manifest_binding(manifest, manifest_path)
    completed_manifest = completed.get("manifest")
    snapshot = completed.get("live_snapshot")
    creator = snapshot.get("creator") if isinstance(snapshot, Mapping) else None
    public = snapshot.get("public") if isinstance(snapshot, Mapping) else None
    section = snapshot.get("section") if isinstance(snapshot, Mapping) else None
    videos = creator.get("videos") if isinstance(creator, Mapping) else None
    matches = section.get("matches") if isinstance(section, Mapping) else None
    new_cid = completed.get("new_cid")
    bvid = str(completed.get("bvid") or "")
    aid = completed.get("aid")
    if (
        completed.get("schema_version") != "same-bv-repair-completed.v1"
        or completed.get("status") != "VERIFIED_FRESH_LIVE"
        or completed.get("rc") != 0
        or not str(completed.get("candidate_id") or "")
        or not bvid
        or not isinstance(completed_manifest, Mapping)
        or completed_manifest.get("path") != manifest_binding["path"]
        or completed_manifest.get("sha256") != manifest_binding["sha256"]
        or not isinstance(new_cid, int)
        or isinstance(new_cid, bool)
        or not isinstance(videos, list)
        or creator.get("available") is not True
        or creator.get("bvid") not in (None, bvid)
        or creator.get("aid") != aid
        or len(videos) != 1
        or not isinstance(videos[0], Mapping)
        or videos[0].get("cid") != new_cid
        or not isinstance(public, Mapping)
        or public.get("available") is not True
        or public.get("bvid") not in (None, bvid)
        or public.get("aid") != aid
        or public.get("state") != 0
        or public.get("cid") != new_cid
        or not isinstance(section, Mapping)
        or section.get("available") is not True
        or not isinstance(matches, list)
        or len(matches) != 1
        or not isinstance(matches[0], Mapping)
        or matches[0].get("bvid") not in (None, bvid)
        or matches[0].get("aid") != aid
        or matches[0].get("cid") != new_cid
    ):
        raise PublicationReconciliationError(
            "same-BV completed authority is incomplete or inconsistent"
        )
    if aid is not None and (
        not isinstance(aid, int) or isinstance(aid, bool)
    ):
        raise PublicationReconciliationError("same-BV completed aid is invalid")
    return completed, bvid, aid, int(new_cid)


def _validate_same_bv_cover_completed(
    *,
    completed_path: Path,
    manifest: dict,
    manifest_path: Path,
) -> tuple[dict, str, int | None, int]:
    """Replay the cover-only plan/journal/fresh-live completion closure."""

    from src.autoslice.same_bv_cover_reconciliation import (
        normalise_cover_url,
        snapshots_equivalent,
    )
    from src.autoslice import same_bv_cover_repair as cover_transaction

    completed = _load_object(
        completed_path, "same-BV cover completed authority"
    )
    manifest_binding = _manifest_binding(manifest, manifest_path)
    completed_manifest = completed.get("manifest")
    new_cover = completed.get("new_cover")
    manifest_cover = manifest.get("cover")
    plan_entry = completed.get("plan")
    journal_entry = completed.get("verified_journal_row")
    snapshot = completed.get("live_snapshot")
    creator = snapshot.get("creator") if isinstance(snapshot, Mapping) else None
    public = snapshot.get("public") if isinstance(snapshot, Mapping) else None
    section = snapshot.get("section") if isinstance(snapshot, Mapping) else None
    videos = creator.get("videos") if isinstance(creator, Mapping) else None
    matches = section.get("matches") if isinstance(section, Mapping) else None
    cid = completed.get("unchanged_cid")
    bvid = str(completed.get("bvid") or "")
    aid = completed.get("aid")
    uploaded_cover = completed.get("uploaded_cover_url")
    if (
        completed.get("schema_version")
        != "same-bv-cover-repair-completed.v1"
        or completed.get("status") != "VERIFIED_FRESH_LIVE"
        or completed.get("rc") != 0
        or not str(completed.get("candidate_id") or "")
        or not bvid
        or not isinstance(completed_manifest, Mapping)
        or completed_manifest.get("path") != manifest_binding["path"]
        or completed_manifest.get("sha256") != manifest_binding["sha256"]
        or not isinstance(new_cover, Mapping)
        or not isinstance(manifest_cover, Mapping)
        or new_cover.get("path") != manifest_cover.get("path")
        or new_cover.get("sha256") != manifest_cover.get("sha256")
        or new_cover.get("bytes") != manifest_cover.get("bytes")
        or not isinstance(cid, int)
        or isinstance(cid, bool)
        or not isinstance(uploaded_cover, str)
        or not isinstance(videos, list)
        or not isinstance(creator, Mapping)
        or creator.get("available") is not True
        or creator.get("bvid") not in (None, bvid)
        or creator.get("aid") != aid
        or len(videos) != 1
        or not isinstance(videos[0], Mapping)
        or videos[0].get("cid") != cid
        or not isinstance(public, Mapping)
        or public.get("available") is not True
        or public.get("bvid") not in (None, bvid)
        or public.get("aid") != aid
        or public.get("state") != 0
        or public.get("cid") != cid
        or not isinstance(section, Mapping)
        or section.get("available") is not True
        or not isinstance(matches, list)
        or len(matches) != 1
        or not isinstance(matches[0], Mapping)
        or matches[0].get("bvid") not in (None, bvid)
        or matches[0].get("aid") != aid
        or matches[0].get("cid") != cid
        or matches[0].get("title") != manifest.get("title")
        or not isinstance(creator.get("metadata"), Mapping)
        or not isinstance(public.get("metadata"), Mapping)
        or creator["metadata"].get("title") != manifest.get("title")
        or public["metadata"].get("title") != manifest.get("title")
        or normalise_cover_url(creator["metadata"].get("cover"))
        != normalise_cover_url(uploaded_cover)
        or normalise_cover_url(public["metadata"].get("cover"))
        != normalise_cover_url(uploaded_cover)
        or not isinstance(plan_entry, Mapping)
        or not isinstance(journal_entry, Mapping)
    ):
        raise PublicationReconciliationError(
            "same-BV cover completed authority is incomplete or inconsistent"
        )
    if aid is not None and (
        not isinstance(aid, int) or isinstance(aid, bool)
    ):
        raise PublicationReconciliationError(
            "same-BV cover completed aid is invalid"
        )
    creator_public_metadata = {
        key: value
        for key, value in creator["metadata"].items()
        if key != "source"
    }
    if creator_public_metadata != public["metadata"]:
        raise PublicationReconciliationError(
            "same-BV cover completed Creator/public metadata disagree"
        )

    plan_path = Path(str(plan_entry.get("path") or ""))
    if (
        not plan_path.is_file()
        or sha256_file(plan_path) != plan_entry.get("sha256")
    ):
        raise PublicationReconciliationError(
            "same-BV cover completed plan binding drifted"
        )
    try:
        plan = cover_transaction.load_plan(plan_path)
        cover_transaction.validate_plan(
            plan, manifest=manifest, plan_path=plan_path
        )
    except cover_transaction.CoverRepairError as exc:
        raise PublicationReconciliationError(
            f"same-BV cover completed plan is invalid: {exc}"
        ) from exc
    if (
        plan.get("schema_version") != "same-bv-cover-repair-plan.v1"
        or plan.get("plan_id") != plan_entry.get("plan_id")
        or plan.get("bvid") != bvid
        or plan.get("unchanged_cid") != cid
        or plan.get("manifest") != completed_manifest
        or plan.get("replacement_cover") != new_cover
        or plan.get("old_cover_url") != completed.get("old_cover_url")
    ):
        raise PublicationReconciliationError(
            "same-BV cover completed plan content conflicts"
        )
    journal_path = Path(str(journal_entry.get("journal_path") or ""))
    if not journal_path.is_file():
        raise PublicationReconciliationError(
            "same-BV cover completed journal is missing"
        )
    try:
        journal_rows = cover_transaction._plan_rows(
            journal_path, plan_path, plan
        )
    except cover_transaction.CoverRepairError as exc:
        raise PublicationReconciliationError(
            f"same-BV cover completed journal is invalid: {exc}"
        ) from exc
    terminal_matches = [
        row
        for row in journal_rows
        if isinstance(row, Mapping)
        and row.get("seq") == journal_entry.get("seq")
        and row.get("row_sha256") == journal_entry.get("row_sha256")
        and row.get("plan_id") == plan.get("plan_id")
    ]
    if len(terminal_matches) != 1:
        raise PublicationReconciliationError(
            "same-BV cover completed terminal journal row is not unique"
        )
    terminal = terminal_matches[0]
    terminal_details = terminal.get("details")
    if (
        terminal.get("schema_version")
        != "same-bv-cover-repair-journal.v1"
        or terminal.get("state") != "VERIFIED"
        or terminal.get("at") != journal_entry.get("at")
        or not isinstance(terminal_details, Mapping)
        or terminal_details.get("uploaded_cover_url") != uploaded_cover
        or terminal_details.get("unchanged_cid") != cid
        or not isinstance(terminal_details.get("live_snapshot"), Mapping)
        or not snapshots_equivalent(
            terminal_details["live_snapshot"], snapshot
        )
    ):
        raise PublicationReconciliationError(
            "same-BV cover completed terminal journal evidence conflicts"
        )
    return completed, bvid, aid, int(cid)


def authority_sidecar_path(manifest_path: Path) -> Path:
    name = manifest_path.name
    suffix = ".upload_manifest.json"
    stem = name[: -len(suffix)] if name.endswith(suffix) else name
    return manifest_path.parent / f"{stem}.publication_reconciliation.json"


def runtime_registry_path(base: Path) -> Path:
    return base.resolve() / "state" / "publication_registry.runtime.v1.json"


def _state_roots(manifest: Mapping[str, object], base: Path) -> list[Path]:
    roots = {base.resolve()}
    attestation = manifest.get("package_attestation")
    package_root = Path(
        str(attestation.get("package_root") or "")
        if isinstance(attestation, Mapping)
        else ""
    ).resolve()
    parts = package_root.parts
    for index, part in enumerate(parts[:-1]):
        if part == "out" and index + 1 < len(parts):
            roots.add(Path(*parts[:index]))
    return sorted(roots, key=str)


@contextmanager
def _reconciliation_lock(base: Path):
    path = base.resolve() / "state" / "publication-reconciliation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def publication_row_is_verified(row: object) -> bool:
    if not isinstance(row, Mapping) or row.get("status") != "published":
        return False
    publication = row.get("publication_reconciliation")
    structurally_valid = bool(
        isinstance(publication, Mapping)
        and publication.get("schema_version") == RECONCILIATION_SCHEMA
        and publication.get("status")
        in {
            "VERIFIED_PUBLIC",
            "VERIFIED_SAME_BV",
            "VERIFIED_SAME_BV_COVER",
        }
        and str(publication.get("candidate_id") or "")
        == str(row.get("candidate_id") or row.get("cid") or "")
        and str(publication.get("bvid") or "") == str(row.get("bvid") or "")
        and isinstance(publication.get("authority"), Mapping)
    )
    if not structurally_valid:
        return False
    try:
        _validate_sha_entry(
            publication.get("authority"),
            "state publication reconciliation authority",
        )
    except (OSError, PublicationReconciliationError):
        return False
    return True


def project_publication_closure(state: Mapping[str, object]) -> dict:
    rows = [
        row
        for lane in ("picks", "songs")
        for row in (
            state.get(lane) if isinstance(state.get(lane), list) else []
        )
        if isinstance(row, Mapping)
    ]
    published = [row for row in rows if publication_row_is_verified(row)]
    if not published:
        return {
            "schema_version": "daily-publication-closure.v1",
            "status": "NOT_APPLICABLE",
            "published_candidate_ids": [],
            "ready_unpublished_candidate_ids": [],
            "unresolved_candidate_ids": [],
        }
    ready_statuses = {"ok", "review_ready", "quarantine"}
    ready = [
        row
        for row in rows
        if row not in published
        and (
            row.get("status") in ready_statuses
            or bool(row.get("delivered"))
        )
    ]
    unresolved = [row for row in rows if row not in published and row not in ready]
    pending = sum(
        len(state.get(key)) if isinstance(state.get(key), list) else 0
        for key in ("pending_talk", "pending_song")
    )
    if pending:
        status = "publication_in_progress"
    elif ready and unresolved:
        status = "ready_unpublished_with_failures"
    elif ready:
        status = "ready_unpublished"
    elif unresolved:
        status = "published_with_failures"
    else:
        status = "published"
    def candidate(row: Mapping[str, object]) -> str:
        return str(row.get("candidate_id") or row.get("cid") or "")

    return {
        "schema_version": "daily-publication-closure.v1",
        "status": status,
        "published_candidate_ids": sorted(candidate(row) for row in published),
        "ready_unpublished_candidate_ids": sorted(candidate(row) for row in ready),
        "unresolved_candidate_ids": sorted(candidate(row) for row in unresolved),
    }


def _apply_publication_to_state(
    state: dict,
    publication: Mapping[str, object],
) -> bool | None:
    candidate_id = str(publication.get("candidate_id") or "")
    matches: list[dict] = []
    for lane in ("picks", "songs"):
        values = state.get(lane)
        if not isinstance(values, list):
            continue
        matches.extend(
            row
            for row in values
            if isinstance(row, dict)
            and str(row.get("candidate_id") or row.get("cid") or "")
            == candidate_id
        )
    if not matches:
        return None
    if len(matches) != 1:
        raise PublicationReconciliationError(
            f"daily state has {len(matches)} rows for candidate {candidate_id}"
        )
    row = matches[0]
    if publication_row_is_verified(row):
        current = row.get("publication_reconciliation")
        if (
            isinstance(current, Mapping)
            and current.get("bvid") != publication.get("bvid")
        ):
            raise PublicationReconciliationError(
                "daily state publication BVID conflicts with reconciliation"
            )
    changed = False
    if row.get("status") != "published" and "prepublication_status" not in row:
        row["prepublication_status"] = row.get("status")
        changed = True
    intended = {
        "status": "published",
        "rc": 0,
        "bvid": publication.get("bvid"),
        "aid": publication.get("aid"),
        "published_cid": publication.get("cid"),
        "publication_reconciliation": dict(publication),
    }
    for key, value in intended.items():
        if row.get(key) != value:
            row[key] = value
            changed = True
    closure = project_publication_closure(state)
    if state.get("publication_closure") != closure:
        state["publication_closure"] = closure
        changed = True
    if state.get("status") != closure["status"]:
        state["status"] = closure["status"]
        changed = True
    return changed


def _update_state_file(
    state_path: Path,
    publication: Mapping[str, object],
) -> bool:
    if not state_path.is_file():
        return False
    for _attempt in range(5):
        before = state_path.read_bytes()
        try:
            state = json.loads(before.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PublicationReconciliationError(
                f"daily state unreadable: {state_path}: {exc}"
            ) from exc
        if not isinstance(state, dict):
            raise PublicationReconciliationError(
                f"daily state is not an object: {state_path}"
            )
        changed = _apply_publication_to_state(state, publication)
        if changed is None:
            return False
        if not changed:
            return True
        if state_path.read_bytes() != before:
            continue
        _atomic_write_json(state_path, state, preserve_state_backup=True)
        return True
    raise PublicationReconciliationError(
        f"daily state changed concurrently too many times: {state_path}"
    )


def _validate_runtime_registry(value: dict) -> list[dict]:
    if value.get("schema_version") != RUNTIME_REGISTRY_SCHEMA:
        raise PublicationReconciliationError(
            "runtime publication registry schema is invalid"
        )
    entries = value.get("entries")
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict) for entry in entries
    ):
        raise PublicationReconciliationError(
            "runtime publication registry entries are invalid"
        )
    return entries


def validate_runtime_registry_entry(
    entry: object,
    *,
    verify_authority: bool = True,
) -> dict:
    if not isinstance(entry, dict):
        raise PublicationReconciliationError(
            "runtime publication registry row is invalid"
        )
    publication = entry.get("publication_reconciliation")
    if (
        entry.get("status") != "published"
        or not str(entry.get("candidate_id") or "")
        or not _DATE_RX.fullmatch(str(entry.get("recording_date") or ""))
        or not str(entry.get("bvid") or "")
        or not isinstance(publication, Mapping)
        or publication.get("schema_version") != RECONCILIATION_SCHEMA
        or publication.get("status")
        not in {
            "VERIFIED_PUBLIC",
            "VERIFIED_SAME_BV",
            "VERIFIED_SAME_BV_COVER",
        }
        or publication.get("candidate_id") != entry.get("candidate_id")
        or publication.get("recording_date") != entry.get("recording_date")
        or publication.get("bvid") != entry.get("bvid")
    ):
        raise PublicationReconciliationError(
            "runtime publication registry row is not a verified projection"
        )
    if verify_authority:
        authority_path = _validate_sha_entry(
            publication.get("authority"),
            "runtime publication reconciliation authority",
        )
        authority = _load_object(
            authority_path, "runtime publication reconciliation authority"
        )
        if publication.get("status") == "VERIFIED_PUBLIC":
            valid_authority = (
                authority.get("schema_version")
                == "new-bv-publication-reconciliation-authority.v1"
                and authority.get("status") == "VERIFIED_PUBLIC"
                and authority.get("candidate_id") == entry.get("candidate_id")
                and authority.get("recording_date")
                == entry.get("recording_date")
                and authority.get("bvid") == entry.get("bvid")
                and authority.get("aid") == publication.get("aid")
                and authority.get("cid") == publication.get("cid")
            )
        elif publication.get("status") == "VERIFIED_SAME_BV":
            valid_authority = (
                authority.get("schema_version")
                == "same-bv-repair-completed.v1"
                and authority.get("status") == "VERIFIED_FRESH_LIVE"
                and authority.get("rc") == 0
                and authority.get("candidate_id") == entry.get("candidate_id")
                and authority.get("bvid") == entry.get("bvid")
                and authority.get("aid") == publication.get("aid")
                and authority.get("new_cid") == publication.get("cid")
            )
        else:
            valid_authority = (
                authority.get("schema_version")
                == "same-bv-cover-repair-completed.v1"
                and authority.get("status") == "VERIFIED_FRESH_LIVE"
                and authority.get("rc") == 0
                and authority.get("candidate_id") == entry.get("candidate_id")
                and authority.get("bvid") == entry.get("bvid")
                and authority.get("aid") == publication.get("aid")
                and authority.get("unchanged_cid") == publication.get("cid")
            )
        if not valid_authority:
            raise PublicationReconciliationError(
                "runtime publication registry authority content conflicts"
            )
    return entry


def _upsert_runtime_registry(
    path: Path,
    publication: Mapping[str, object],
) -> None:
    if path.is_file():
        registry = _load_object(path, "runtime publication registry")
        entries = _validate_runtime_registry(registry)
        for row in entries:
            validate_runtime_registry_entry(row)
    else:
        registry = {
            "schema_version": RUNTIME_REGISTRY_SCHEMA,
            "entries": [],
        }
        entries = registry["entries"]
    key = (
        publication.get("candidate_id"),
        publication.get("recording_date"),
    )
    matching = [
        row
        for row in entries
        if (row.get("candidate_id"), row.get("recording_date")) == key
    ]
    if len(matching) > 1:
        raise PublicationReconciliationError(
            "runtime publication registry has duplicate candidate/date rows"
        )
    if matching and matching[0].get("bvid") != publication.get("bvid"):
        raise PublicationReconciliationError(
            "runtime publication registry BVID conflict"
        )
    entry = {
        "candidate_id": publication.get("candidate_id"),
        "recording_date": publication.get("recording_date"),
        "status": "published",
        "bvid": publication.get("bvid"),
        "publication_reconciliation": dict(publication),
    }
    if matching:
        entries[entries.index(matching[0])] = entry
    else:
        entries.append(entry)
    registry["entries"] = sorted(
        entries,
        key=lambda row: (
            str(row.get("recording_date") or ""),
            str(row.get("candidate_id") or ""),
        ),
    )
    _atomic_write_json(path, registry)


def _assert_runtime_registry_compatible(
    path: Path,
    publication: Mapping[str, object],
) -> None:
    if not path.is_file():
        return
    registry = _load_object(path, "runtime publication registry")
    entries = _validate_runtime_registry(registry)
    for row in entries:
        validate_runtime_registry_entry(row)
    key = (
        publication.get("candidate_id"),
        publication.get("recording_date"),
    )
    matching = [
        row
        for row in entries
        if (row.get("candidate_id"), row.get("recording_date")) == key
    ]
    if len(matching) > 1:
        raise PublicationReconciliationError(
            "runtime publication registry has duplicate candidate/date rows"
        )
    if matching and matching[0].get("bvid") != publication.get("bvid"):
        raise PublicationReconciliationError(
            "runtime publication registry BVID conflict"
        )


def _upsert_static_registry(
    path: Path,
    publication: Mapping[str, object],
) -> None:
    registry = _load_object(path, "publication registry")
    if registry.get("schema_version") != STATIC_REGISTRY_SCHEMA:
        raise PublicationReconciliationError(
            "publication registry schema is invalid"
        )
    entries = registry.get("entries")
    if not isinstance(entries, list) or any(
        not isinstance(row, dict) for row in entries
    ):
        raise PublicationReconciliationError(
            "publication registry entries are invalid"
        )
    key = (
        publication.get("candidate_id"),
        publication.get("recording_date"),
    )
    matching = [
        row
        for row in entries
        if (row.get("candidate_id"), row.get("recording_date")) == key
    ]
    if len(matching) > 1:
        raise PublicationReconciliationError(
            "publication registry has duplicate candidate/date rows"
        )
    if matching:
        row = matching[0]
        if row.get("status") == "published" and row.get("bvid") != publication.get(
            "bvid"
        ):
            raise PublicationReconciliationError(
                "publication registry BVID conflict"
            )
    else:
        row = {
            "candidate_id": publication.get("candidate_id"),
            "recording_date": publication.get("recording_date"),
        }
        entries.append(row)
    row["status"] = "published"
    row["bvid"] = publication.get("bvid")
    row["publication_reconciliation"] = {
        "schema_version": RECONCILIATION_SCHEMA,
        "status": publication.get("status"),
        "authority": publication.get("authority"),
        "reconciled_at": publication.get("reconciled_at"),
    }
    row.setdefault(
        "note", "post-publish reconciliation; public authority hash-bound"
    )
    registry["entries"] = entries
    _atomic_write_json(path, registry)


def _assert_static_registry_compatible(
    path: Path,
    publication: Mapping[str, object],
) -> None:
    registry = _load_object(path, "publication registry")
    if registry.get("schema_version") != STATIC_REGISTRY_SCHEMA:
        raise PublicationReconciliationError(
            "publication registry schema is invalid"
        )
    entries = registry.get("entries")
    if not isinstance(entries, list) or any(
        not isinstance(row, dict) for row in entries
    ):
        raise PublicationReconciliationError(
            "publication registry entries are invalid"
        )
    key = (
        publication.get("candidate_id"),
        publication.get("recording_date"),
    )
    matching = [
        row
        for row in entries
        if (row.get("candidate_id"), row.get("recording_date")) == key
    ]
    if len(matching) > 1:
        raise PublicationReconciliationError(
            "publication registry has duplicate candidate/date rows"
        )
    if (
        matching
        and matching[0].get("status") == "published"
        and matching[0].get("bvid") != publication.get("bvid")
    ):
        raise PublicationReconciliationError(
            "publication registry BVID conflict"
        )


def _state_candidate_count(path: Path, candidate_id: str) -> int:
    if not path.is_file():
        return 0
    state = _load_object(path, "daily state")
    return sum(
        1
        for lane in ("picks", "songs")
        for row in (state.get(lane) if isinstance(state.get(lane), list) else [])
        if isinstance(row, Mapping)
        and str(row.get("candidate_id") or row.get("cid") or "")
        == candidate_id
    )


def _commit_projection(
    *,
    manifest: dict,
    publication: dict,
    base: Path,
    registry_path: Path,
) -> dict:
    with _reconciliation_lock(base):
        roots = _state_roots(manifest, base)
        for root in roots:
            _assert_runtime_registry_compatible(
                runtime_registry_path(root), publication
            )
        _assert_static_registry_compatible(registry_path, publication)
        state_paths = [
            root / "state" / f"{publication['recording_date']}.json"
            for root in roots
        ]
        counts = [
            _state_candidate_count(path, str(publication["candidate_id"]))
            for path in state_paths
        ]
        if any(count > 1 for count in counts) or not any(counts):
            raise PublicationReconciliationError(
                "daily state does not contain exactly one published candidate"
            )
        for root in roots:
            _upsert_runtime_registry(runtime_registry_path(root), publication)
        _upsert_static_registry(registry_path, publication)
        updated_states: list[str] = []
        for path in state_paths:
            if _update_state_file(path, publication):
                updated_states.append(str(path.resolve()))
        if not updated_states:
            raise PublicationReconciliationError(
                "no daily state contained the published candidate"
            )
    return {
        "schema_version": RECONCILIATION_SCHEMA,
        "status": publication["status"],
        "candidate_id": publication["candidate_id"],
        "recording_date": publication["recording_date"],
        "bvid": publication["bvid"],
        "state_paths": updated_states,
        "runtime_registry_paths": [
            str(runtime_registry_path(root)) for root in roots
        ],
        "registry_path": str(registry_path.resolve()),
    }


def reconcile_new_bv_publication(
    *,
    manifest: dict,
    manifest_path: Path,
    bvid: str,
    public_verify_path: Path,
    season_verify_path: Path,
    uploaded_path: Path,
    autoslice_base: Path,
    registry_path: Path,
    reconciled_at: str,
) -> dict:
    candidate_id, recording_date = _candidate_and_date(manifest)
    evidence, aid, cid = _validate_new_public_evidence(
        manifest=manifest,
        manifest_path=manifest_path,
        bvid=bvid,
        public_verify_path=public_verify_path,
        season_verify_path=season_verify_path,
        uploaded_path=uploaded_path,
    )
    authority_path = authority_sidecar_path(manifest_path)
    authority = {
        "schema_version": "new-bv-publication-reconciliation-authority.v1",
        "status": "VERIFIED_PUBLIC",
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "title": manifest.get("title"),
        "created_at": reconciled_at,
        "evidence": evidence,
    }
    authority = _create_or_validate_authority(authority_path, authority)
    effective_reconciled_at = str(
        authority.get("created_at") or reconciled_at
    )
    publication = {
        "schema_version": RECONCILIATION_SCHEMA,
        "status": "VERIFIED_PUBLIC",
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "title": manifest.get("title"),
        "authority": _sha_entry(authority_path),
        "reconciled_at": effective_reconciled_at,
    }
    return _commit_projection(
        manifest=manifest,
        publication=publication,
        base=autoslice_base,
        registry_path=registry_path,
    )


def reconcile_same_bv_publication(
    *,
    completed_path: Path,
    manifest: dict,
    manifest_path: Path,
    autoslice_base: Path,
    registry_path: Path,
    reconciled_at: str,
) -> dict:
    candidate_id, recording_date = _candidate_and_date(manifest)
    completed, bvid, aid, cid = _validate_same_bv_completed(
        completed_path=completed_path,
        manifest=manifest,
        manifest_path=manifest_path,
    )
    if completed.get("candidate_id") != candidate_id:
        raise PublicationReconciliationError(
            "same-BV completed candidate differs from manifest record"
        )
    publication = {
        "schema_version": RECONCILIATION_SCHEMA,
        "status": "VERIFIED_SAME_BV",
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "title": manifest.get("title"),
        "authority": _sha_entry(completed_path),
        "reconciled_at": reconciled_at,
    }
    return _commit_projection(
        manifest=manifest,
        publication=publication,
        base=autoslice_base,
        registry_path=registry_path,
    )


def reconcile_same_bv_cover_publication(
    *,
    completed_path: Path,
    manifest: dict,
    manifest_path: Path,
    autoslice_base: Path,
    registry_path: Path,
    reconciled_at: str,
) -> dict:
    candidate_id, recording_date = _candidate_and_date(manifest)
    completed, bvid, aid, cid = _validate_same_bv_cover_completed(
        completed_path=completed_path,
        manifest=manifest,
        manifest_path=manifest_path,
    )
    if completed.get("candidate_id") != candidate_id:
        raise PublicationReconciliationError(
            "same-BV cover completed candidate differs from manifest record"
        )
    publication = {
        "schema_version": RECONCILIATION_SCHEMA,
        "status": "VERIFIED_SAME_BV_COVER",
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "title": manifest.get("title"),
        "authority": _sha_entry(completed_path),
        "reconciled_at": reconciled_at,
    }
    return _commit_projection(
        manifest=manifest,
        publication=publication,
        base=autoslice_base,
        registry_path=registry_path,
    )


def apply_runtime_publications_to_state(
    *,
    date: str,
    state: dict,
    autoslice_base: Path,
) -> bool:
    path = runtime_registry_path(autoslice_base)
    if not path.is_file():
        return False
    registry = _load_object(path, "runtime publication registry")
    entries = _validate_runtime_registry(registry)
    changed = False
    for entry in entries:
        validate_runtime_registry_entry(entry)
        if entry.get("recording_date") != date:
            continue
        publication = entry.get("publication_reconciliation")
        assert isinstance(publication, Mapping)
        projected = _apply_publication_to_state(state, publication)
        if projected is not None:
            changed = projected or changed
    return changed
