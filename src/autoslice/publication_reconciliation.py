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
from pathlib import Path

from . import fastlane_c1_technical_receipt
from . import fastlane_c2_authorized_upload
from .repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)

from .publication_state_projection import (
    PUBLICATION_RECOVERY_PROJECTION_KIND,
    PUBLICATION_RECOVERY_SCHEMA,
    RECONCILIATION_SCHEMA,
    RUNTIME_REGISTRY_SCHEMA,
    PublicationReconciliationError,
    _DATE_RX,
    _apply_publication_to_state,
    _reconciliation_lock,
    _state_candidate_count,
    _state_roots,
    _update_state_file,
    _validate_runtime_registry,
    missing_state_publication_reconciliation_block,  # noqa: F401
    project_publication_closure,  # noqa: F401
    publication_recovery_sidecar_path,
    publication_row_is_verified,  # noqa: F401
    runtime_registry_path,
    validate_runtime_registry_entry,
)


STATIC_REGISTRY_SCHEMA = "publication-registry.v1"
_SHA256_RX = re.compile(r"^[0-9a-f]{64}$")
_DEPLOYED_REGISTRY_PROJECTION_MODE = "DEPLOYED_RUNTIME_OVERLAY"
_STATIC_REGISTRY_PROJECTION_MODE = "STATIC_REGISTRY"
_DEPLOYED_REGISTRY_MARKERS = (
    "DEPLOYED_COMMIT",
    "DEPLOYED_MANIFEST.json",
    "DEPLOYED_AUTHORITY_MANIFEST.json",
)


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

    def stable_role_payload(role: str, value: object) -> object:
        payload = stable_payload(value)
        if not isinstance(payload, dict):
            return payload
        if role == "public_verify" and isinstance(payload.get("public_tags"), list):
            # Public tag ordering may change between identical successful reads.
            # Sort without deduplication so missing/extra tags remain conflicts.
            payload["public_tags"] = sorted(payload["public_tags"])
        if (
            role == "season_verify"
            and payload.get("status") == "IN_SEASON_PUBLIC"
            and type(payload.get("season_add_code")) is int
            and (payload["season_add_code"], payload.get("season_add_message"))
            in ((0, "OK"), (20080, "当前稿件已存在在合集中"))
        ):
            # Retrying the same verified membership returns "already in season".
            # Preserve the original receipt; compare its completed outcome.
            payload["season_add_code"] = 0
            payload.pop("season_add_message", None)
        return payload

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
                or stable_role_payload(role, actual_role.get("payload"))
                != stable_role_payload(role, expected_role.get("payload"))
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


def _validate_review_manifest_candidate(
    review: Mapping[str, object], candidate_id: str
) -> None:
    """Require every present review-manifest identity surface to name one candidate."""
    identities: list[str] = []
    top_level = str(review.get("candidate_id") or "")
    if top_level:
        identities.append(top_level)

    if "exact_candidate_ids" in review:
        exact = review.get("exact_candidate_ids")
        if (
            not isinstance(exact, list)
            or len(exact) != 1
            or not isinstance(exact[0], str)
            or not exact[0]
        ):
            raise PublicationReconciliationError(
                "manifest review manifest candidate differs from record"
            )
        identities.append(exact[0])

    if "items" in review:
        items = review.get("items")
        if (
            not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], Mapping)
        ):
            raise PublicationReconciliationError(
                "manifest review manifest candidate differs from record"
            )
        item_candidate = str(items[0].get("candidate_id") or "")
        if not item_candidate:
            raise PublicationReconciliationError(
                "manifest review manifest candidate differs from record"
            )
        identities.append(item_candidate)

    selection = review.get("selection_contract")
    if isinstance(selection, Mapping) and "candidate_ids" in selection:
        selected = selection.get("candidate_ids")
        if (
            not isinstance(selected, list)
            or len(selected) != 1
            or not isinstance(selected[0], str)
            or not selected[0]
        ):
            raise PublicationReconciliationError(
                "manifest review manifest candidate differs from record"
            )
        identities.append(selected[0])

    if not identities or any(value != candidate_id for value in identities):
        raise PublicationReconciliationError(
            "manifest review manifest candidate differs from record"
        )


def _original_review_date(manifest, record, review, candidate_id):
    """Read the registered original target, not a new wrapper date or a C2 shape."""
    from src.autoslice import original_patch_package

    if review.get("schema_version") != original_patch_package.MANIFEST_SCHEMA:
        return None
    items = review.get("items")
    authority = manifest.get("recovery_publication_authority")
    if (
        not isinstance(items, list)
        or len(items) != 1
        or not isinstance(items[0], dict)
        or items[0].get("candidate_id") != candidate_id
        or record.get("recovery_publication_authority") != authority
        or items[0].get("recovery_publication_authority") != authority
    ):
        raise PublicationReconciliationError("original review candidate/authority differs")
    try:
        target = original_patch_package.validate_publication(
            authority,
            candidate_id=candidate_id,
            expected_final_title=str(manifest.get("title") or ""),
        )
    except (ValueError, OSError) as exc:
        raise PublicationReconciliationError("original review target is not registered") from exc
    date = target.get("recording_date")
    if not isinstance(date, str) or not _DATE_RX.fullmatch(date):
        raise PublicationReconciliationError("original target recording date invalid")
    if any(review[key] != date for key in ("date", "recording_date") if key in review):
        raise PublicationReconciliationError("original review recording date differs from target")
    return date


def _candidate_and_date(manifest: Mapping[str, object]) -> tuple[str, str]:
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, Mapping):
        raise PublicationReconciliationError(
            "manifest has no package_attestation"
        )
    record_entry = attestation.get("record")
    if record_entry is None and "c1_technical_receipt" in attestation:
        try:
            fastlane_c1_technical_receipt.validate_authorized_projection_manifest(
                manifest
            )
        except fastlane_c1_technical_receipt.C1TechnicalReceiptError as exc:
            raise PublicationReconciliationError(
                "C1 manifest cannot resolve publication candidate/date"
            ) from exc
        authority = manifest.get("recovery_publication_authority")
        if not isinstance(authority, Mapping):
            raise PublicationReconciliationError(
                "C1 manifest has no recovery publication authority"
            )
        candidate_id = str(authority.get("candidate_id") or "")
        recording_date = str(authority.get("recording_date") or "")
        if not candidate_id or not _DATE_RX.fullmatch(recording_date):
            raise PublicationReconciliationError(
                "C1 recovery publication authority has invalid candidate/date"
            )
        return candidate_id, recording_date
    record_path = _validate_sha_entry(record_entry, "manifest record")
    record = _load_object(record_path, "manifest record")
    package_root = Path(str(attestation.get("package_root") or ""))
    story = record.get("story_contract")
    candidate_id = (
        str(story.get("candidate_id") or "")
        if isinstance(story, Mapping)
        else ""
    )
    if not candidate_id:
        candidate_id = str(record.get("delivery_candidate_id") or "")
    is_verified_c2 = False
    if not candidate_id:
        c2_candidate_id = (
            fastlane_c2_authorized_upload.verified_c2_release_candidate_id(
                package_root, record
            )
        )
        if c2_candidate_id:
            # The C2 resolver replays its sealed bridge, but the manifest must
            # still bind the exact regular record directly inside that verified
            # package.  Do not let an equal-bytes external/symlinked record
            # stand in for the package member it audited.
            expected_record = (
                package_root
                / fastlane_c2_authorized_upload.bridge.RECORD_NAME
            ).resolve()
            supplied_record = (
                Path(str(record_entry.get("path") or ""))
                if isinstance(record_entry, Mapping)
                else Path()
            )
            if supplied_record.is_symlink() or record_path != expected_record:
                raise PublicationReconciliationError(
                    "C2 manifest record is not the verified package record"
                )
            candidate_id = c2_candidate_id
            is_verified_c2 = True
    if not candidate_id:
        raise PublicationReconciliationError(
            "manifest record has no publication candidate_id"
        )

    # C2's dated wrapper is not a recording-date authority.  Its bridge
    # verifies identity, while the manifest-bound review manifest is the only
    # permitted date source for reconciliation.
    if is_verified_c2:
        review_path = _validate_sha_entry(
            attestation.get("review_manifest"), "manifest review manifest"
        )
        review = _load_object(review_path, "manifest review manifest")
        _validate_review_manifest_candidate(review, candidate_id)
        review_dates = {
            str(review[key])
            for key in ("date", "recording_date")
            if key in review
        }
        if len(review_dates) != 1 or not _DATE_RX.fullmatch(
            next(iter(review_dates), "")
        ):
            raise PublicationReconciliationError(
                "manifest review manifest has no single valid recording date"
            )
        return candidate_id, next(iter(review_dates))

    parts = package_root.parts
    canonical_dates = {
        parts[index + 1]
        for index, part in enumerate(parts[:-1])
        if part in {"out", "lidousha"}
        and _DATE_RX.fullmatch(parts[index + 1])
    }
    if len(canonical_dates) > 1:
        raise PublicationReconciliationError(
            "package_root has conflicting canonical recording dates"
        )
    if canonical_dates:
        return candidate_id, next(iter(canonical_dates))

    # A portable reviewed clone can live below a dated recovery wrapper rather
    # than its original out/<date> or lidousha/<date> tree.  Its attested
    # review manifest is then the only current, hash-bound recording-date
    # authority; never let the wrapper date redirect reconciliation to a new
    # daily state.  Old manifests without this attestation retain the strict
    # legacy package-root fallback below.
    if "review_manifest" in attestation:
        review_path = _validate_sha_entry(
            attestation.get("review_manifest"), "manifest review manifest"
        )
        review = _load_object(review_path, "manifest review manifest")
        original_date = _original_review_date(manifest, record, review, candidate_id)
        if original_date is not None:
            return candidate_id, original_date
        _validate_review_manifest_candidate(review, candidate_id)
        review_dates = {
            str(review[key])
            for key in ("date", "recording_date")
            if key in review
        }
        if len(review_dates) != 1 or not _DATE_RX.fullmatch(
            next(iter(review_dates), "")
        ):
            raise PublicationReconciliationError(
                "manifest review manifest has no single valid recording date"
            )
        return candidate_id, next(iter(review_dates))

    # Legacy/test package roots can predate the canonical out/<date> and
    # lidousha/<date> layouts.  Retain their strict unique-date fallback, but
    # do not let an outer recovery wrapper date compete with a canonical
    # package-layout date.
    dates = {part for part in parts if _DATE_RX.fullmatch(part)}
    if len(dates) != 1:
        raise PublicationReconciliationError(
            "cannot resolve one recording date from package_root"
        )
    return candidate_id, next(iter(dates))


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




def _deployed_registry_binding(
    registry_path: Path,
) -> tuple[Path, Path] | None:
    """Return the bounded deployed-repository binding for one registry path.

    Production keeps the publication registry below exactly one channel
    directory under ``assets``.  Deriving the repository root from that fixed
    layout avoids searching arbitrary ancestors, while allowing ordinary
    temporary/Git fixtures outside the layout to retain their mutable static
    registry behavior.
    """

    path = Path(registry_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.name != "publication_registry.v1.json":
        return None
    channel_dir = path.parent
    assets_dir = channel_dir.parent
    repo_root = assets_dir.parent
    if assets_dir.name != "assets" or not channel_dir.name:
        return None
    relative = Path("assets") / channel_dir.name / path.name
    if repo_root / relative != path:
        return None

    marker_paths = [repo_root / name for name in _DEPLOYED_REGISTRY_MARKERS]
    if not any(marker.exists() or marker.is_symlink() for marker in marker_paths):
        return None
    for marker, name in (
        (marker_paths[0], _DEPLOYED_REGISTRY_MARKERS[0]),
        (marker_paths[2], _DEPLOYED_REGISTRY_MARKERS[2]),
    ):
        if marker.is_symlink() or not marker.is_file():
            raise PublicationReconciliationError(
                f"deployed publication registry marker is unavailable: {name}"
            )
    # DEPLOYED_MANIFEST.json is also a deployment marker when present, but
    # its complete tree/mode validation belongs to the deployment soak gate.
    return repo_root, relative


def _prepare_registry_projection(registry_path: Path) -> dict[str, object]:
    """Validate deployed static authority and describe the write mode.

    The returned readback is captured before any runtime/state projection is
    written.  In deployed mode the static registry is deliberately never
    rewritten; its exact deployed-manifest binding remains the immutable
    source while the runtime overlay carries new publication rows.
    """

    binding = _deployed_registry_binding(registry_path)
    if binding is None:
        return {"mode": _STATIC_REGISTRY_PROJECTION_MODE}
    repo_root, relative = binding
    path = Path(registry_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        authority = require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=relative,
            observed_bytes=path.read_bytes(),
        )
    except (OSError, UnicodeError, RepositoryAssetAuthorityError) as exc:
        raise PublicationReconciliationError(
            f"deployed publication registry authority is invalid: {exc}"
        ) from exc
    if authority.mode != "DEPLOYED_MANIFEST":
        raise PublicationReconciliationError(
            "deployed publication registry authority mode is invalid"
        )
    commit = authority.commit
    return {
        "mode": _DEPLOYED_REGISTRY_PROJECTION_MODE,
        "path": str(path.resolve()),
        "relative_path": relative.as_posix(),
        "commit": commit,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _registry_readback(path: Path) -> dict[str, object]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }




def _publication_identity(value: Mapping[str, object]) -> dict[str, object]:
    """Return the immutable part of a verified publication projection."""

    return {
        key: value.get(key)
        for key in (
            "schema_version",
            "status",
            "candidate_id",
            "recording_date",
            "bvid",
            "aid",
            "cid",
            "title",
            "authority",
        )
    }


def _assert_publication_identity_compatible(
    current: object,
    expected: Mapping[str, object],
    *,
    label: str,
) -> None:
    """Reject a different authority or publication tuple during a rerun."""

    if not isinstance(current, Mapping):
        raise PublicationReconciliationError(
            f"{label} has no publication reconciliation mapping"
        )
    current_identity = _publication_identity(current)
    expected_identity = _publication_identity(expected)
    # Older committed static rows carry only the status and authority pointer;
    # those fields still bind the immutable authority.  Runtime rows and new
    # recovery sidecars carry the complete tuple and are compared exactly.
    for key, current_value in current_identity.items():
        if key in {"aid", "cid", "title", "candidate_id", "recording_date", "bvid"}:
            if key not in current or current_value is None:
                continue
        if current_value != expected_identity.get(key):
            raise PublicationReconciliationError(
                f"{label} authority/publication tuple conflicts"
            )


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


_PUBLICATION_RECOVERY_ENTRY_KEYS = frozenset(
    {
        "candidate_id",
        "recording_date",
        "status",
        "rc",
        "cid",
        "published_cid",
        "bvid",
        "aid",
        "title",
        "publication_reconciliation",
    }
)
_PUBLICATION_RECOVERY_FORBIDDEN_KEYS = frozenset(
    {
        "picks",
        "songs",
        "pending_talk",
        "pending_song",
        "publication_closure",
        "prepublication_status",
    }
)


def _publication_recovery_entry(
    publication: Mapping[str, object],
) -> dict[str, object]:
    """Build one identity-preserving entry for a missing-state projection."""

    candidate_id = str(publication.get("candidate_id") or "")
    recording_date = str(publication.get("recording_date") or "")
    bvid = str(publication.get("bvid") or "")
    published_cid = publication.get("cid")
    if (
        not candidate_id
        or not _DATE_RX.fullmatch(recording_date)
        or not bvid
        or not isinstance(published_cid, int)
        or isinstance(published_cid, bool)
        or not isinstance(publication.get("authority"), Mapping)
    ):
        raise PublicationReconciliationError(
            "publication recovery projection identity is incomplete"
        )
    aid = publication.get("aid")
    if aid is not None and (not isinstance(aid, int) or isinstance(aid, bool)):
        raise PublicationReconciliationError(
            "publication recovery projection aid is invalid"
        )
    return {
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "status": "published",
        "rc": 0,
        # ``cid`` remains the source candidate identity.  The online numeric
        # CID is deliberately carried in a separate field below.
        "cid": candidate_id,
        "published_cid": published_cid,
        "bvid": bvid,
        "aid": aid,
        "title": publication.get("title"),
        "publication_reconciliation": dict(publication),
    }


def _validate_publication_recovery_entry(
    entry: object,
    *,
    recording_date: str,
    verify_authority: bool = True,
) -> dict:
    if not isinstance(entry, dict) or set(entry) != set(
        _PUBLICATION_RECOVERY_ENTRY_KEYS
    ):
        raise PublicationReconciliationError(
            "publication recovery projection entry shape is invalid"
        )
    candidate_id = str(entry.get("candidate_id") or "")
    if (
        not candidate_id
        or entry.get("recording_date") != recording_date
        or entry.get("status") != "published"
        or entry.get("rc") != 0
        or entry.get("cid") != candidate_id
        or not isinstance(entry.get("published_cid"), int)
        or isinstance(entry.get("published_cid"), bool)
        or not str(entry.get("bvid") or "")
    ):
        raise PublicationReconciliationError(
            "publication recovery projection entry identity is invalid"
        )
    aid = entry.get("aid")
    if aid is not None and (not isinstance(aid, int) or isinstance(aid, bool)):
        raise PublicationReconciliationError(
            "publication recovery projection entry aid is invalid"
        )
    validate_runtime_registry_entry(
        {
            "candidate_id": candidate_id,
            "recording_date": recording_date,
            "status": "published",
            "bvid": entry["bvid"],
            "publication_reconciliation": entry["publication_reconciliation"],
        },
        verify_authority=verify_authority,
    )
    publication = entry["publication_reconciliation"]
    assert isinstance(publication, Mapping)
    if (
        publication.get("candidate_id") != candidate_id
        or publication.get("recording_date") != recording_date
        or publication.get("bvid") != entry.get("bvid")
        or publication.get("aid") != aid
        or publication.get("cid") != entry.get("published_cid")
        or publication.get("title") != entry.get("title")
    ):
        raise PublicationReconciliationError(
            "publication recovery projection entry mapping conflicts"
        )
    return entry


def _validate_publication_recovery_projection(
    value: object,
    *,
    recording_date: str,
    verify_authority: bool = True,
) -> list[dict]:
    if not isinstance(value, dict):
        raise PublicationReconciliationError(
            "publication recovery projection is not an object"
        )
    if value.get("schema_version") != PUBLICATION_RECOVERY_SCHEMA:
        raise PublicationReconciliationError(
            "publication recovery projection schema is invalid"
        )
    if value.get("projection_kind") != PUBLICATION_RECOVERY_PROJECTION_KIND:
        raise PublicationReconciliationError(
            "publication recovery projection kind is invalid"
        )
    if value.get("recording_date") != recording_date:
        raise PublicationReconciliationError(
            "publication recovery projection date conflicts"
        )
    for key in (
        "original_state_status",
        "original_candidate_set",
        "day_completion",
    ):
        if value.get(key) != (
            "MISSING" if key == "original_state_status" else "UNKNOWN"
        ):
            raise PublicationReconciliationError(
                f"publication recovery projection {key} is invalid"
            )
    if _PUBLICATION_RECOVERY_FORBIDDEN_KEYS.intersection(value):
        raise PublicationReconciliationError(
            "publication recovery projection contains canonical state fields"
        )
    allowed = {
        "schema_version",
        "projection_kind",
        "recording_date",
        "original_state_status",
        "original_candidate_set",
        "day_completion",
        "entries",
    }
    if set(value) != allowed:
        raise PublicationReconciliationError(
            "publication recovery projection shape is invalid"
        )
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries:
        raise PublicationReconciliationError(
            "publication recovery projection entries are invalid"
        )
    validated = [
        _validate_publication_recovery_entry(
            entry,
            recording_date=recording_date,
            verify_authority=verify_authority,
        )
        for entry in entries
    ]
    candidate_keys = [entry["candidate_id"] for entry in validated]
    if len(set(candidate_keys)) != len(candidate_keys):
        raise PublicationReconciliationError(
            "publication recovery projection has duplicate candidate/date rows"
        )
    bvid_keys = [entry["bvid"] for entry in validated]
    if len(set(bvid_keys)) != len(bvid_keys):
        raise PublicationReconciliationError(
            "publication recovery projection has duplicate BVID targets"
        )
    return validated


def _repair_bound_object(entry: object, label: str) -> tuple[Path, dict]:
    """Read the SHA-bound plan/manifest format, which has no byte-count field."""

    if not isinstance(entry, Mapping):
        raise PublicationReconciliationError(f"{label} binding is missing")
    path = Path(str(entry.get("path") or ""))
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise PublicationReconciliationError(f"{label} missing or unsafe")
    if entry.get("sha256") != sha256_file(path) or (
        "bytes" in entry and entry["bytes"] != path.stat().st_size
    ):
        raise PublicationReconciliationError(f"{label} hash/bytes drifted")
    return path.resolve(), _load_object(path, label)


def _snapshot_matches_publication(
    snapshot: object, publication: Mapping[str, object]
) -> bool:
    """Require one exact media identity across Creator, public, and section."""

    if not isinstance(snapshot, Mapping):
        return False
    creator, public, section = (snapshot.get(k) for k in ("creator", "public", "section"))
    if not all(isinstance(v, Mapping) and v.get("available") is True
               for v in (creator, public, section)):
        return False
    videos, matches = creator.get("videos"), section.get("matches")
    if not all(isinstance(v, list) and len(v) == 1 and isinstance(v[0], Mapping)
               for v in (videos, matches)):
        return False
    return (
        all(row.get("bvid") == publication.get("bvid")
            and row.get("aid") == publication.get("aid")
            for row in (creator, public, matches[0]))
        and all(cid == publication.get("cid")
                for cid in (videos[0].get("cid"), public.get("cid"), matches[0].get("cid")))
        and public.get("state") == 0
    )


def _repair_successor_authority_valid(authority, manifest, incoming) -> bool:
    """Admit only existing typed repair authorities with their native proof."""
    if not isinstance(authority, Mapping):
        return False
    schema = authority.get("schema_version")
    if schema == "recovery-same-bv-publication-authority.v1":
        return True  # Existing path retains the full plan/before checks below.
    if schema != "original-fastlane-authorized-same-bv.v1":
        return False
    if manifest.get("recovery_publication_authority") != authority:
        return False
    from src.autoslice.original_patch_package import validate_publication

    try:
        validate_publication(
            authority,
            candidate_id=str(incoming.get("candidate_id") or ""),
            expected_final_title=str(manifest.get("title") or ""),
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return True


def _verified_same_bv_successor(
    current: Mapping[str, object], incoming: Mapping[str, object]
) -> bool:
    """Allow a changed recovery tuple only through its verified repair plan."""

    if _publication_identity(current) == _publication_identity(incoming):
        return False  # An exact rerun still uses the ordinary immutable comparison.
    if (
        incoming.get("status") != "VERIFIED_SAME_BV"
        or current.get("status") not in {
            "VERIFIED_PUBLIC", "VERIFIED_SAME_BV", "VERIFIED_SAME_BV_COVER"
        }
        or any(current.get(k) != incoming.get(k)
               for k in ("schema_version", "candidate_id", "recording_date", "bvid", "aid"))
        or any(not isinstance(v, int) or isinstance(v, bool) or v <= 0
               for v in (current.get("aid"), current.get("cid"), incoming.get("cid")))
        or current.get("cid") == incoming.get("cid")
    ):
        return False
    completed_path = _validate_sha_entry(
        incoming.get("authority"), "same-BV successor completed authority"
    )
    completed = _load_object(completed_path, "same-BV successor completed authority")
    manifest_path, manifest = _repair_bound_object(
        completed.get("manifest"), "same-BV successor manifest"
    )
    completed, bvid, aid, cid = _validate_same_bv_completed(
        completed_path=completed_path, manifest=manifest, manifest_path=manifest_path
    )
    candidate, date = _candidate_and_date(manifest)
    if (
        completed.get("remote_mutation") is not False
        or completed.get("candidate_id") != incoming.get("candidate_id")
        or (candidate, date, bvid, aid, cid) != (
            incoming.get("candidate_id"), incoming.get("recording_date"),
            incoming.get("bvid"), incoming.get("aid"), incoming.get("cid")
        )
        or manifest.get("title") != incoming.get("title")
        or not _snapshot_matches_publication(completed.get("live_snapshot"), incoming)
    ):
        return False
    _, plan = _repair_bound_object(completed.get("plan"), "same-BV successor plan")
    plan_binding = completed["plan"]
    authority = plan.get("recovery_publication_authority")
    return bool(
        plan.get("schema_version") == "same-bv-repair-plan.v2"
        and plan.get("plan_id")
        and plan.get("plan_id") == plan_binding.get("plan_id")
        and plan.get("bvid") == incoming.get("bvid")
        and plan.get("manifest") == completed.get("manifest")
        and plan.get("replacement") == completed.get("replacement")
        and isinstance(authority, Mapping)
        and _repair_successor_authority_valid(authority, manifest, incoming)
        and all(authority.get(k) == current.get(k) for k in ("candidate_id", "bvid", "aid"))
        # The recovery asset may predate an earlier repair. The plan's actual
        # before snapshot is the predecessor CID authority for this transition.
        and _snapshot_matches_publication(plan.get("before"), current)
    )


def _assert_publication_recovery_compatible(
    path: Path,
    publication: Mapping[str, object],
) -> None:
    """Validate an existing sidecar before any registry write takes place."""

    if not path.exists() and not path.is_symlink():
        return
    date = str(publication.get("recording_date") or "")
    projection = _load_object(path, "publication recovery projection")
    entries = _validate_publication_recovery_projection(
        projection, recording_date=date
    )
    incoming = _publication_recovery_entry(publication)
    for entry in entries:
        if entry["candidate_id"] == incoming["candidate_id"]:
            if _verified_same_bv_successor(entry["publication_reconciliation"], publication):
                continue
            _assert_publication_identity_compatible(
                entry["publication_reconciliation"],
                publication,
                label="publication recovery projection",
            )
            if entry["published_cid"] != incoming["published_cid"]:
                raise PublicationReconciliationError(
                    "publication recovery projection CID tuple conflicts"
                )
        elif entry["bvid"] == incoming["bvid"]:
            raise PublicationReconciliationError(
                "publication recovery projection BVID identity conflicts"
            )


def _upsert_publication_recovery_sidecar(
    path: Path,
    publication: Mapping[str, object],
) -> bool:
    """Create or append one missing-state projection entry idempotently."""

    recording_date = str(publication.get("recording_date") or "")
    incoming = _publication_recovery_entry(publication)
    if path.exists() or path.is_symlink():
        projection = _load_object(path, "publication recovery projection")
        entries = _validate_publication_recovery_projection(
            projection, recording_date=recording_date
        )
    else:
        projection = {
            "schema_version": PUBLICATION_RECOVERY_SCHEMA,
            "projection_kind": PUBLICATION_RECOVERY_PROJECTION_KIND,
            "recording_date": recording_date,
            "original_state_status": "MISSING",
            "original_candidate_set": "UNKNOWN",
            "day_completion": "UNKNOWN",
            "entries": [],
        }
        entries = []
    matching = [
        entry
        for entry in entries
        if entry["candidate_id"] == incoming["candidate_id"]
    ]
    if len(matching) > 1:
        raise PublicationReconciliationError(
            "publication recovery projection has duplicate candidate/date rows"
        )
    if matching:
        if _verified_same_bv_successor(matching[0]["publication_reconciliation"], publication):
            entries[entries.index(matching[0])] = incoming
            projection["entries"] = entries
            _atomic_write_json(path, projection)
            return True
        _assert_publication_identity_compatible(
            matching[0]["publication_reconciliation"],
            publication,
            label="publication recovery projection",
        )
        if any(
            matching[0].get(key) != incoming.get(key)
            for key in (
                "candidate_id",
                "recording_date",
                "status",
                "cid",
                "published_cid",
                "bvid",
                "aid",
                "title",
            )
        ):
            raise PublicationReconciliationError(
                "publication recovery projection candidate tuple conflicts"
            )
        return False
    if any(entry["bvid"] == incoming["bvid"] for entry in entries):
        raise PublicationReconciliationError(
            "publication recovery projection BVID identity conflicts"
        )
    entries = [*entries, incoming]
    entries.sort(key=lambda entry: str(entry["candidate_id"]))
    projection["entries"] = entries
    _atomic_write_json(path, projection)
    return True






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
        registry_projection = _prepare_registry_projection(registry_path)
        deployed_registry = (
            registry_projection.get("mode")
            == _DEPLOYED_REGISTRY_PROJECTION_MODE
        )
        state_paths = [
            root / "state" / f"{publication['recording_date']}.json"
            for root in roots
        ]
        backup_paths = [path.with_suffix(".json.bak") for path in state_paths]
        # A missing primary with any backup path is a state recovery
        # restore case.  Do not silently route it through the missing-state
        # sidecar; the backup must be restored by the established state
        # recovery path first.
        if any(
            not path.is_file() and (backup.exists() or backup.is_symlink())
            for path, backup in zip(state_paths, backup_paths, strict=True)
        ):
            raise PublicationReconciliationError(
                "daily state primary is missing but .bak exists; restore backup first"
            )
        state_present = []
        for path in state_paths:
            present = path.exists() or path.is_symlink()
            if present and not path.is_file():
                raise PublicationReconciliationError(
                    f"daily state path is not a regular file: {path}"
                )
            state_present.append(present)
        counts = [
            _state_candidate_count(path, str(publication["candidate_id"]))
            for path in state_paths
        ]
        all_state_missing = not any(state_present)
        if all_state_missing:
            # Validate existing recovery sidecars before touching either
            # registry.  This keeps authority/tuple drift from leaving a
            # partially updated projection on disk.
            recovery_paths = [
                publication_recovery_sidecar_path(
                    root, str(publication["recording_date"])
                )
                for root in roots
            ]
            for path in recovery_paths:
                _assert_publication_recovery_compatible(path, publication)
            for root in roots:
                _upsert_runtime_registry(runtime_registry_path(root), publication)
            if not deployed_registry:
                _upsert_static_registry(registry_path, publication)
            for path in recovery_paths:
                _upsert_publication_recovery_sidecar(path, publication)
            registry_projection["readback"] = _registry_readback(registry_path)
            return {
                "schema_version": RECONCILIATION_SCHEMA,
                "status": publication["status"],
                "candidate_id": publication["candidate_id"],
                "recording_date": publication["recording_date"],
                "bvid": publication["bvid"],
                "state_paths": [],
                "publication_recovery_paths": [
                    str(path.resolve()) for path in recovery_paths
                ],
                "candidate_projection_status": PUBLICATION_RECOVERY_PROJECTION_KIND,
                "day_state_status": "UNKNOWN",
                "runtime_registry_paths": [
                    str(runtime_registry_path(root)) for root in roots
                ],
                "registry_path": str(registry_path.resolve()),
                "registry_projection_mode": registry_projection["mode"],
                "registry_readback": registry_projection["readback"],
                "registry_projection": registry_projection,
            }
        if any(count > 1 for count in counts) or not any(counts):
            raise PublicationReconciliationError(
                "daily state does not contain exactly one published candidate"
            )
        for root in roots:
            _upsert_runtime_registry(runtime_registry_path(root), publication)
        if not deployed_registry:
            _upsert_static_registry(registry_path, publication)
        updated_states: list[str] = []
        for path in state_paths:
            if _update_state_file(path, publication):
                updated_states.append(str(path.resolve()))
        if not updated_states:
            raise PublicationReconciliationError(
                "no daily state contained the published candidate"
            )
        registry_projection["readback"] = _registry_readback(registry_path)
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
        "registry_projection_mode": registry_projection["mode"],
        "registry_readback": registry_projection["readback"],
        "registry_projection": registry_projection,
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
        if projected is None:
            raise PublicationReconciliationError(
                f"daily state does not contain runtime publication candidate "
                f"{entry.get('candidate_id')} for {date}"
            )
        changed = projected or changed
    return changed
