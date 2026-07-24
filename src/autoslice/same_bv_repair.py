"""Crash-safe, manifest-bound replacement of one existing Bilibili archive.

This module deliberately does *not* have a "create archive" operation.  The
only media mutation exposed by :class:`BilibiliRepairAdapter` is
``biliup append -v <existing BV>``.  A durable hash-chained journal is written
before that one append call.  Once ``APPEND_INTENT`` exists, every resume path
is observation-only until the appended CID appears; append is never retried.

The second mutation is an idempotent Creator Center edit that keeps exactly the
new CID.  It may be retried with the same payload after code 21540 or an
ambiguous transport failure, but only while the live topology is still exactly
``[old P, planned new P]``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from src.autoslice import final_human_review as human_review
from src.autoslice.bilibili_member_api import BiliSession
from src.autoslice.recovery_title_authority import (
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority,
)

PLAN_SCHEMA = "same-bv-repair-plan.v2"
JOURNAL_SCHEMA = "same-bv-repair-journal.v1"
JOURNAL_STATES = {
    "PLANNED",
    "APPEND_INTENT",
    "APPEND_AMBIGUOUS",
    "TWO_P_READY",
    "SWAP_RETRYABLE",
    "CREATOR_SINGLE_NEW",
    "PUBLIC_PENDING",
    "VERIFIED",
    "BLOCKED_DRIFT",
}
TERMINAL_STATES = {"VERIFIED", "BLOCKED_DRIFT"}
RETRYABLE_EDIT_CODE = 21540
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")

_TRANSITIONS = {
    "PLANNED": {"APPEND_INTENT", "BLOCKED_DRIFT"},
    "APPEND_INTENT": {
        "APPEND_AMBIGUOUS",
        "TWO_P_READY",
        "BLOCKED_DRIFT",
    },
    "APPEND_AMBIGUOUS": {
        "APPEND_AMBIGUOUS",
        "TWO_P_READY",
        "BLOCKED_DRIFT",
    },
    "TWO_P_READY": {
        "TWO_P_READY",
        "SWAP_RETRYABLE",
        "CREATOR_SINGLE_NEW",
        "BLOCKED_DRIFT",
    },
    "SWAP_RETRYABLE": {
        "SWAP_RETRYABLE",
        "CREATOR_SINGLE_NEW",
        "BLOCKED_DRIFT",
    },
    "CREATOR_SINGLE_NEW": {
        "PUBLIC_PENDING",
        "VERIFIED",
        "BLOCKED_DRIFT",
    },
    "PUBLIC_PENDING": {
        "PUBLIC_PENDING",
        "VERIFIED",
        "BLOCKED_DRIFT",
    },
    "VERIFIED": set(),
    "BLOCKED_DRIFT": set(),
}


class RepairError(RuntimeError):
    """Base class for fail-closed repair errors."""


class PlanInvalid(RepairError):
    """The local plan, manifest, or bound artifacts are not trustworthy."""


class JournalCorrupt(RepairError):
    """The durable journal is malformed, reordered, truncated, or edited."""


class DuplicateBvid(RepairError):
    """A different repair plan already owns this BVID in the ledger."""


class ObservationUnavailable(RepairError):
    """A read-only remote observation could not be obtained."""


class RemoteMutationError(RepairError):
    """A remote mutation returned a structured failure."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class RepairAdapter(Protocol):
    """Minimal live interface.  There is intentionally no new-upload method."""

    def observe(self, bvid: str, section_id: int) -> dict[str, Any]:
        ...

    def append_existing(self, bvid: str, media_path: Path) -> None:
        ...

    def prepare_cover(self, cover_path: Path) -> str:
        ...

    def swap_keep_only(
        self,
        bvid: str,
        *,
        keep_cid: int,
        target_metadata: Mapping[str, Any],
        cover_url: str,
    ) -> Mapping[str, Any]:
        ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _row_hash(row_without_hash: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(row_without_hash)).hexdigest()


def _normalise_tags(value: object) -> list[str]:
    if isinstance(value, str):
        rows = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        rows = [str(part).strip() for part in value if str(part).strip()]
    else:
        rows = []
    # API ordering is not a semantic metadata distinction.  Duplicates are.
    return sorted(rows)


def _normalise_cover_url(value: object) -> object:
    """Compare Bilibili CDN asset identity, not harmless URL projection drift."""

    if not isinstance(value, str) or not value:
        return value
    candidate = value if not value.startswith("//") else "https:" + value
    parsed = urllib.parse.urlsplit(candidate)
    if not parsed.netloc:
        return value
    return f"//{parsed.netloc.lower()}{parsed.path}"


def _metadata_from_archive(archive: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": archive.get("title"),
        "desc": archive.get("desc"),
        "tags": _normalise_tags(archive.get("tag")),
        "tid": archive.get("tid"),
        "copyright": archive.get("copyright"),
        "source": archive.get("source"),
        "cover": _normalise_cover_url(archive.get("cover")),
    }


def _metadata_from_public(
    public: Mapping[str, Any], public_tags: object
) -> dict[str, Any]:
    return {
        "title": public.get("title"),
        "desc": public.get("desc"),
        "tags": _normalise_tags(public_tags),
        "tid": public.get("tid"),
        "copyright": public.get("copyright"),
        "cover": _normalise_cover_url(public.get("pic") or public.get("cover")),
    }


def _video_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cid": row.get("cid"),
        "filename": row.get("filename"),
        "title": row.get("title"),
    }


def _episode_rows(payload: object) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "episodes" and isinstance(value, list):
                rows.extend(dict(row) for row in value if isinstance(row, dict))
            else:
                rows.extend(_episode_rows(value))
    elif isinstance(payload, list):
        for value in payload:
            rows.extend(_episode_rows(value))
    return rows


def _section_ids(payload: object) -> set[int]:
    ids: set[int] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"section_id", "sectionId"} and isinstance(value, int):
                ids.add(value)
            elif (
                key == "id"
                and isinstance(value, int)
                and (
                    "episodes" in payload
                    or "season_id" in payload
                    or "seasonId" in payload
                )
            ):
                ids.add(value)
            ids.update(_section_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            ids.update(_section_ids(value))
    return ids


def _episode_value(row: Mapping[str, Any], key: str) -> object:
    if row.get(key) is not None:
        return row.get(key)
    for nested_key in ("archive", "arc"):
        nested = row.get(nested_key)
        if isinstance(nested, Mapping) and nested.get(key) is not None:
            return nested.get(key)
    return None


def normalise_snapshot(
    *,
    bvid: str,
    section_id: int,
    creator_data: Mapping[str, Any],
    public_payload: Mapping[str, Any] | None,
    public_tags_payload: Mapping[str, Any] | None,
    section_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Convert the three Bilibili read surfaces into a strict stable shape."""

    archive = creator_data.get("archive") or {}
    videos = creator_data.get("videos") or []
    if not isinstance(archive, Mapping) or not isinstance(videos, list):
        raise ObservationUnavailable("Creator archive response shape is invalid")
    creator = {
        "available": True,
        "bvid": archive.get("bvid"),
        "aid": archive.get("aid"),
        "state": archive.get("state"),
        "state_desc": archive.get("state_desc"),
        "metadata": _metadata_from_archive(archive),
        "videos": [
            _video_row(row) for row in videos if isinstance(row, Mapping)
        ],
    }

    public_data: Mapping[str, Any] = {}
    public_available = False
    if isinstance(public_payload, Mapping) and public_payload.get("code") == 0:
        candidate = public_payload.get("data") or {}
        if isinstance(candidate, Mapping):
            public_data = candidate
            public_available = True
    tags: object = []
    tags_available = bool(
        isinstance(public_tags_payload, Mapping)
        and public_tags_payload.get("code") == 0
        and isinstance(public_tags_payload.get("data"), list)
    )
    if tags_available:
        tags = [
            row.get("tag_name")
            for row in (public_tags_payload.get("data") or [])
            if isinstance(row, Mapping)
        ]
    public = {
        # Title/CID without a trustworthy tag surface is not a complete public
        # metadata observation.  Planning fails; propagation waits.
        "available": public_available and tags_available,
        "bvid": public_data.get("bvid"),
        "aid": public_data.get("aid"),
        "cid": public_data.get("cid"),
        "state": public_data.get("state"),
        "metadata": _metadata_from_public(public_data, tags),
    }

    section_available = bool(
        isinstance(section_payload, Mapping) and section_payload.get("code") == 0
    )
    reported_section_ids = _section_ids(section_payload or {})
    if reported_section_ids and section_id not in reported_section_ids:
        section_available = False
    episodes = _episode_rows(section_payload or {}) if section_available else []
    matches = []
    for row in episodes:
        row_bvid = _episode_value(row, "bvid")
        row_aid = _episode_value(row, "aid")
        if row_bvid == bvid or (
            creator["aid"] is not None and row_aid == creator["aid"]
        ):
            matches.append(
                {
                    "bvid": row_bvid,
                    "aid": row_aid,
                    "cid": _episode_value(row, "cid"),
                    "title": _episode_value(row, "title")
                    or _episode_value(row, "episode_title"),
                }
            )
    section = {
        "available": section_available,
        "section_id": section_id,
        "matches": matches,
    }
    return {"creator": creator, "public": public, "section": section}


class BilibiliRepairAdapter:
    """Production adapter composed from the existing tested member session."""

    def __init__(
        self,
        *,
        session: BiliSession,
        http: Callable[[str], Mapping[str, Any]],
        view_url: str,
        tags_url: str,
        section_url: str,
    ) -> None:
        self.session = session
        self.http = http
        self.view_url = view_url
        self.tags_url = tags_url
        self.section_url = section_url

    def observe(self, bvid: str, section_id: int) -> dict[str, Any]:
        try:
            creator = self.session.archive_view(bvid)
        except Exception as exc:
            raise ObservationUnavailable(f"Creator archive unavailable: {exc}") from exc
        try:
            public = self.http(self.view_url.format(bvid=bvid))
        except Exception:
            public = None
        try:
            public_tags = self.http(self.tags_url.format(bvid=bvid))
        except Exception:
            public_tags = None
        try:
            section = self.http(self.section_url.format(section_id=section_id))
        except Exception:
            section = None
        return normalise_snapshot(
            bvid=bvid,
            section_id=section_id,
            creator_data=creator,
            public_payload=public,
            public_tags_payload=public_tags,
            section_payload=section,
        )

    def append_existing(self, bvid: str, media_path: Path) -> None:
        self.session.biliup_append(bvid, media_path)

    def prepare_cover(self, cover_path: Path) -> str:
        return self.session.cover_up(cover_path.read_bytes())

    def swap_keep_only(
        self,
        bvid: str,
        *,
        keep_cid: int,
        target_metadata: Mapping[str, Any],
        cover_url: str,
    ) -> Mapping[str, Any]:
        current = self.session.archive_view(bvid)
        payload = self.session.build_edit_payload(
            current,
            title=str(target_metadata["title"]),
            cover_url=cover_url,
            keep_only_cid=keep_cid,
            tag=",".join(target_metadata["tags"]),
            video_title=str(target_metadata["title"]),
        )
        for field in ("desc", "tid", "copyright", "source"):
            payload[field] = target_metadata[field]
        response = self.session.post_json(
            "https://member.bilibili.com/x/vu/web/edit"
            f"?csrf={urllib.parse.quote(self.session.csrf)}",
            payload,
        )
        code = response.get("code")
        if code != 0:
            raise RemoteMutationError(
                f"Creator edit failed: {response}",
                code=code if isinstance(code, int) else None,
            )
        return response


def _target_metadata(manifest: Mapping[str, Any]) -> dict[str, Any]:
    policy = manifest.get("publish_policy") or {}
    return {
        "title": manifest.get("title"),
        "desc": manifest.get("description"),
        "tags": _normalise_tags(manifest.get("tags")),
        "tid": policy.get("tid"),
        "copyright": policy.get("copyright"),
        "source": policy.get("source"),
        # The exact CDN URL is prepared and journaled immediately before edit.
        "cover": None,
    }


def package_recovery_publication_authority(
    record: Mapping[str, Any],
    review_item: Mapping[str, Any] | None,
    *,
    expected_final_title: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate an optional authority only when both package surfaces agree."""

    record_authority = record.get("recovery_publication_authority")
    review_authority = (
        review_item.get("recovery_publication_authority")
        if isinstance(review_item, Mapping)
        else None
    )
    if record_authority is None and review_authority is None:
        return None, []
    if record_authority is None or review_authority is None:
        return None, [
            "recovery_publication_authority must exist on both record and review item"
        ]
    if record_authority != review_authority:
        return None, [
            "record and review item recovery_publication_authority differ"
        ]
    story = record.get("story_contract")
    candidate_id = (
        str(story.get("candidate_id") or "").strip()
        if isinstance(story, Mapping)
        else ""
    )
    if not candidate_id:
        return None, [
            "recovery_publication_authority has no record story candidate_id"
        ]
    item_candidate_id = str((review_item or {}).get("candidate_id") or "").strip()
    if item_candidate_id and item_candidate_id != candidate_id:
        return None, [
            "review item candidate_id differs from record story candidate_id"
        ]
    try:
        authority = validate_recovery_publication_authority(
            record_authority,
            candidate_id=candidate_id,
            expected_final_title=expected_final_title,
        )
    except RecoveryTitleAuthorityError as exc:
        return None, [f"recovery_publication_authority invalid: {exc}"]
    if record_authority != authority:
        return None, [
            "record/review recovery_publication_authority is not canonical"
        ]
    return authority, []


def recovery_publication_package_problems(
    manifest: Mapping[str, Any],
    record: Mapping[str, Any],
    review_item: Mapping[str, Any] | None,
) -> list[str]:
    authority, problems = package_recovery_publication_authority(
        record,
        review_item,
        expected_final_title=str(manifest.get("title") or ""),
    )
    if manifest.get("recovery_publication_authority") != authority:
        problems.append(
            "manifest recovery_publication_authority does not exactly match "
            "the record/review package"
        )
    return problems


def attach_package_recovery_publication_authority(
    manifest: dict[str, Any],
    record: Mapping[str, Any],
    review_manifest_path: Path,
    video_path: Path,
) -> list[str]:
    """Attach the optional two-surface authority while constructing a manifest."""

    try:
        review = json.loads(review_manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"review manifest unreadable: {review_manifest_path} ({exc})"]
    if not isinstance(review, Mapping):
        return [f"review manifest must be a JSON object: {review_manifest_path}"]
    expected = video_path.resolve()
    root = review_manifest_path.parent.resolve()
    review_item = None
    for raw_item in review.get("items") or []:
        if not isinstance(raw_item, Mapping):
            continue
        raw_media = raw_item.get("media") or raw_item.get("video")
        if not isinstance(raw_media, str) or not raw_media:
            continue
        media = Path(raw_media)
        media = media.resolve() if media.is_absolute() else (root / media).resolve()
        if media == expected:
            review_item = raw_item
            break
    if review_item is None:
        return ["review_manifest has no item for the reviewed video"]
    authority, problems = package_recovery_publication_authority(
        record,
        review_item,
        expected_final_title=str(manifest.get("title") or ""),
    )
    if authority is not None:
        manifest["recovery_publication_authority"] = authority
    return problems


def _manifest_recovery_publication_authority(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    raw = manifest.get("recovery_publication_authority")
    candidate_id = (
        str(raw.get("candidate_id") or "").strip()
        if isinstance(raw, Mapping)
        else ""
    )
    if not candidate_id:
        raise PlanInvalid(
            "same-BV repair requires recovery_publication_authority"
        )
    try:
        authority = validate_recovery_publication_authority(
            raw,
            candidate_id=candidate_id,
            expected_final_title=str(manifest.get("title") or ""),
        )
    except RecoveryTitleAuthorityError as exc:
        raise PlanInvalid(
            f"recovery_publication_authority invalid: {exc}"
        ) from exc
    if dict(raw) != authority:
        raise PlanInvalid(
            "recovery_publication_authority is not canonical"
        )
    return authority


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanInvalid(f"{label} unreadable: {path} ({exc})") from exc
    if not isinstance(value, dict):
        raise PlanInvalid(f"{label} must be a JSON object: {path}")
    return value


def _final_human_review_attestation(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        return dict(
            human_review.replay_final_human_review_attestation(
                manifest
            )
        )
    except human_review.FinalHumanReviewError as exc:
        detail = f" ({exc.detail})" if exc.detail else ""
        raise PlanInvalid(
            "same-BV repair final_human_review rejected: "
            f"{exc.reason_code}{detail}"
        ) from exc


def validate_repair_publication_target(
    manifest: Mapping[str, Any],
    bvid: str,
) -> dict[str, Any]:
    """Reject the wrong existing archive before any adapter/network setup."""

    if manifest.get("manifest_version") != 3:
        raise PlanInvalid(
            "same-BV repair requires authorized-upload-manifest.v3"
        )
    authority = _manifest_recovery_publication_authority(manifest)
    _final_human_review_attestation(manifest)
    if bvid != authority.get("bvid"):
        raise PlanInvalid(
            "repair BVID differs from recovery_publication_authority"
        )
    return authority


def repair_publication_target_problems(
    manifest: Mapping[str, Any],
    bvid: str,
) -> list[str]:
    try:
        validate_repair_publication_target(manifest, bvid)
    except PlanInvalid as exc:
        return [str(exc)]
    return []


def _basic_snapshot_problems(
    snapshot: Mapping[str, Any],
    *,
    bvid: str,
    aid: int | None = None,
) -> list[str]:
    problems: list[str] = []
    creator = snapshot.get("creator") or {}
    public = snapshot.get("public") or {}
    section = snapshot.get("section") or {}
    if creator.get("available") is not True:
        problems.append("Creator archive is unavailable")
    if creator.get("bvid") != bvid:
        problems.append("Creator archive BVID mismatch")
    if not isinstance(creator.get("aid"), int):
        problems.append("Creator archive AID missing")
    if aid is not None and creator.get("aid") != aid:
        problems.append("Creator archive AID drift")
    videos = creator.get("videos")
    if not isinstance(videos, list):
        problems.append("Creator videos topology invalid")
    elif any(
        not isinstance(row, dict)
        or not isinstance(row.get("cid"), int)
        or not isinstance(row.get("filename"), str)
        or not row.get("filename")
        for row in videos
    ):
        problems.append("Creator video identity incomplete")
    elif len({row["cid"] for row in videos}) != len(videos):
        problems.append("Creator videos contain duplicate CID")
    if public.get("available") is True:
        if public.get("bvid") != bvid:
            problems.append("public BVID mismatch")
        if aid is not None and public.get("aid") != aid:
            problems.append("public AID drift")
    if section.get("available") is True:
        matches = section.get("matches")
        if not isinstance(matches, list):
            problems.append("section membership shape invalid")
        elif len(matches) > 1:
            problems.append("BVID/AID appears more than once in exact section")
    return problems


def create_plan(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    bvid: str,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze a read-only, exact single-P repair plan from live state."""

    manifest_path = manifest_path.resolve()
    if not _BVID_RE.fullmatch(bvid):
        raise PlanInvalid(f"invalid existing BVID: {bvid!r}")
    if manifest.get("manifest_version") != 3:
        raise PlanInvalid("same-BV repair requires authorized-upload-manifest.v3")
    authority = validate_repair_publication_target(manifest, bvid)
    final_human_review = _final_human_review_attestation(manifest)
    season = manifest.get("season")
    if not isinstance(season, Mapping):
        raise PlanInvalid("same-BV repair requires an exact season/section binding")
    season_id = season.get("season_id")
    section_id = season.get("section_id")
    if not isinstance(season_id, int) or not isinstance(section_id, int):
        raise PlanInvalid("manifest season_id/section_id are invalid")

    problems = _basic_snapshot_problems(snapshot, bvid=bvid)
    creator = snapshot.get("creator") or {}
    public = snapshot.get("public") or {}
    section = snapshot.get("section") or {}
    videos = creator.get("videos") or []
    if len(videos) != 1:
        problems.append(f"repair planning requires exactly 1 Creator P, got {len(videos)}")
    elif videos[0].get("cid") != authority.get("cid"):
        problems.append(
            "sole Creator CID differs from recovery_publication_authority"
        )
    if creator.get("aid") != authority.get("aid"):
        problems.append(
            "Creator AID differs from recovery_publication_authority"
        )
    if creator.get("state") != 0:
        problems.append("Creator archive is not state=0 during planning")
    if public.get("available") is not True:
        problems.append("public archive unavailable during planning")
    elif len(videos) == 1:
        if public.get("aid") != authority.get("aid"):
            problems.append(
                "public AID differs from recovery_publication_authority"
            )
        if public.get("cid") != authority.get("cid"):
            problems.append(
                "public CID differs from recovery_publication_authority"
            )
        if public.get("state") != 0:
            problems.append("public archive is not state=0 during planning")
        if public.get("cid") != videos[0].get("cid"):
            problems.append("public CID does not equal the sole Creator CID")
        creator_public_metadata = {
            key: value
            for key, value in (creator.get("metadata") or {}).items()
            if key != "source"
        }
        if public.get("metadata") != creator_public_metadata:
            problems.append("Creator and public metadata disagree during planning")
    if section.get("available") is not True:
        problems.append("exact section unavailable during planning")
    matches = section.get("matches") or []
    if len(matches) != 1:
        problems.append(
            f"planning requires exactly one exact-section membership, got {len(matches)}"
        )
    elif len(videos) == 1:
        if matches[0].get("aid") != authority.get("aid"):
            problems.append(
                "section AID differs from recovery_publication_authority"
            )
        if matches[0].get("cid") != authority.get("cid"):
            problems.append(
                "section CID differs from recovery_publication_authority"
            )
        if matches[0].get("cid") != videos[0].get("cid"):
            problems.append("section CID does not equal the sole Creator CID")
        if matches[0].get("bvid") not in (None, bvid):
            problems.append("section BVID mismatch")
        if matches[0].get("title") != (creator.get("metadata") or {}).get("title"):
            problems.append("section title disagrees with Creator title")
    if problems:
        raise PlanInvalid("; ".join(problems))

    manifest_sha = sha256_file(manifest_path)
    video = manifest.get("video") or {}
    cover = manifest.get("cover") or {}
    plan = {
        "schema_version": PLAN_SCHEMA,
        "plan_id": uuid.uuid4().hex,
        "created_at": _utc_now(),
        "bvid": bvid,
        "manifest": {
            "path": str(manifest_path),
            "sha256": manifest_sha,
        },
        "replacement": {
            "video": {
                "path": str(Path(str(video.get("path"))).resolve()),
                "sha256": video.get("sha256"),
                "bytes": video.get("bytes"),
            },
            "cover": {
                "path": str(Path(str(cover.get("path"))).resolve()),
                "sha256": cover.get("sha256"),
                "bytes": cover.get("bytes"),
            },
        },
        "authorization": {
            "by": (manifest.get("authorization") or {}).get("by"),
            "quote": (manifest.get("authorization") or {}).get("quote"),
        },
        "recovery_publication_authority": authority,
        "package_attestation": final_human_review,
        "season": {
            "season_id": season_id,
            "section_id": section_id,
            "season_title": season.get("season_title"),
        },
        "target_metadata": _target_metadata(manifest),
        "before": snapshot,
    }
    validate_plan(plan, manifest=manifest)
    return plan


def validate_plan(
    plan: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any] | None = None,
    plan_path: Path | None = None,
) -> None:
    problems: list[str] = []
    if plan.get("schema_version") != PLAN_SCHEMA:
        problems.append("repair plan schema mismatch")
    if not isinstance(plan.get("plan_id"), str) or not plan.get("plan_id"):
        problems.append("repair plan_id missing")
    bvid = plan.get("bvid")
    if not isinstance(bvid, str) or not _BVID_RE.fullmatch(bvid):
        problems.append("repair BVID invalid")
    plan_authority = plan.get("recovery_publication_authority")
    candidate_id = (
        str(plan_authority.get("candidate_id") or "").strip()
        if isinstance(plan_authority, Mapping)
        else ""
    )
    try:
        validated_plan_authority = validate_recovery_publication_authority(
            plan_authority,
            candidate_id=candidate_id,
            expected_final_title=str(
                (plan.get("target_metadata") or {}).get("title") or ""
            ),
        )
    except RecoveryTitleAuthorityError as exc:
        problems.append(
            f"repair recovery_publication_authority invalid: {exc}"
        )
        validated_plan_authority = None
    if validated_plan_authority is not None:
        if plan_authority != validated_plan_authority:
            problems.append(
                "repair recovery_publication_authority is not canonical"
            )
        if bvid != validated_plan_authority.get("bvid"):
            problems.append(
                "repair BVID differs from recovery_publication_authority"
            )
    plan_package_attestation = plan.get("package_attestation")
    if not isinstance(plan_package_attestation, Mapping):
        problems.append("repair final human review attestation is missing")
    else:
        try:
            replayed_plan_attestation = _final_human_review_attestation(
                {"package_attestation": plan_package_attestation}
            )
        except PlanInvalid as exc:
            problems.append(str(exc))
        else:
            if plan_package_attestation != replayed_plan_attestation:
                problems.append(
                    "repair final human review attestation is not canonical"
                )
    manifest_entry = plan.get("manifest") or {}
    manifest_path = Path(str(manifest_entry.get("path") or ""))
    bound_manifest: dict[str, Any] | None = None
    if not manifest_path.is_absolute() or not manifest_path.is_file():
        problems.append("bound manifest missing or not absolute")
    else:
        actual = sha256_file(manifest_path)
        if actual != manifest_entry.get("sha256"):
            problems.append("bound manifest hash drift")
        try:
            bound_manifest = _json_object(
                manifest_path, label="bound repair manifest"
            )
        except PlanInvalid as exc:
            problems.append(str(exc))
    replacement = plan.get("replacement") or {}
    for kind in ("video", "cover"):
        entry = replacement.get(kind) or {}
        path = Path(str(entry.get("path") or ""))
        if not path.is_absolute() or not path.is_file():
            problems.append(f"replacement {kind} missing or not absolute")
            continue
        if sha256_file(path) != entry.get("sha256"):
            problems.append(f"replacement {kind} hash drift")
        if path.stat().st_size != entry.get("bytes"):
            problems.append(f"replacement {kind} byte-size drift")
    season = plan.get("season") or {}
    if not isinstance(season.get("season_id"), int) or not isinstance(
        season.get("section_id"), int
    ):
        problems.append("repair season binding invalid")
    before = plan.get("before") or {}
    creator = before.get("creator") or {}
    videos = creator.get("videos") or []
    if len(videos) != 1 or not isinstance(videos[0].get("cid"), int):
        problems.append("repair plan does not freeze exactly one old CID")
    if manifest is not None:
        if bound_manifest is not None and manifest != bound_manifest:
            problems.append(
                "runtime manifest differs from the hash-bound manifest file"
            )
        try:
            manifest_authority = (
                _manifest_recovery_publication_authority(manifest)
            )
        except PlanInvalid as exc:
            problems.append(str(exc))
        else:
            if plan_authority != manifest_authority:
                problems.append(
                    "repair recovery_publication_authority drifted from manifest"
                )
        try:
            manifest_human_review = _final_human_review_attestation(
                manifest
            )
        except PlanInvalid as exc:
            problems.append(str(exc))
        else:
            if plan_package_attestation != manifest_human_review:
                problems.append(
                    "repair final human review attestation drifted from manifest"
                )
        if plan.get("target_metadata") != _target_metadata(manifest):
            problems.append("repair target metadata drifted from manifest")
        for kind in ("video", "cover"):
            if (replacement.get(kind) or {}).get("sha256") != (
                manifest.get(kind) or {}
            ).get("sha256"):
                problems.append(f"repair {kind} no longer matches manifest")
        auth = manifest.get("authorization") or {}
        if plan.get("authorization") != {
            "by": auth.get("by"),
            "quote": auth.get("quote"),
        }:
            problems.append("repair authorization drifted from manifest")
    if plan_path is not None and not plan_path.is_file():
        problems.append("repair plan file missing")
    if problems:
        raise PlanInvalid("; ".join(dict.fromkeys(problems)))


def validate_plan_problems(plan: Mapping[str, Any], *, manifest: Mapping[str, Any], plan_path: Path) -> list[str]:
    try:
        validate_plan(plan, manifest=manifest, plan_path=plan_path)
    except PlanInvalid as exc:
        return [str(exc)]
    return []


def write_plan(path: Path, plan: Mapping[str, Any]) -> None:
    """Create a plan without overwriting any prior authority."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise PlanInvalid(f"repair plan already exists: {path}") from exc
    try:
        data = payload.encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while creating repair plan")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_parent(path.parent)


def load_plan(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanInvalid(f"repair plan unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanInvalid("repair plan must be a JSON object")
    return value


def _fsync_parent(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _utc_now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise JournalCorrupt(f"repair journal unreadable: {exc}") from exc
    if raw and not raw.endswith(b"\n"):
        raise JournalCorrupt("repair journal has a partial final row")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise JournalCorrupt(f"repair journal is not UTF-8: {exc}") from exc

    entries: list[dict[str, Any]] = []
    previous_hash: str | None = None
    states_by_plan: dict[str, str] = {}
    bindings_by_plan: dict[str, tuple[str, str, str, str]] = {}
    bvid_owners: dict[str, str] = {}
    for line_no, line in enumerate(lines, start=1):
        if not line:
            raise JournalCorrupt(f"repair journal row {line_no} is empty")
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise JournalCorrupt(
                f"repair journal row {line_no} is invalid JSON"
            ) from exc
        if not isinstance(row, dict):
            raise JournalCorrupt(f"repair journal row {line_no} is not an object")
        supplied_hash = row.get("row_sha256")
        body = {key: value for key, value in row.items() if key != "row_sha256"}
        if supplied_hash != _row_hash(body):
            raise JournalCorrupt(f"repair journal row {line_no} hash mismatch")
        if row.get("schema_version") != JOURNAL_SCHEMA:
            raise JournalCorrupt(f"repair journal row {line_no} schema mismatch")
        if row.get("seq") != line_no:
            raise JournalCorrupt(f"repair journal row {line_no} sequence mismatch")
        if row.get("previous_row_sha256") != previous_hash:
            raise JournalCorrupt(f"repair journal row {line_no} chain mismatch")
        previous_hash = str(supplied_hash)
        plan_id = row.get("plan_id")
        state = row.get("state")
        bvid = row.get("bvid")
        binding = (
            str(row.get("plan_sha256") or ""),
            str(row.get("manifest_sha256") or ""),
            str(row.get("video_sha256") or ""),
            str(row.get("cover_sha256") or ""),
        )
        if not isinstance(plan_id, str) or not plan_id:
            raise JournalCorrupt(f"repair journal row {line_no} plan_id missing")
        if state not in JOURNAL_STATES:
            raise JournalCorrupt(f"repair journal row {line_no} state invalid")
        if not isinstance(bvid, str) or not _BVID_RE.fullmatch(bvid):
            raise JournalCorrupt(f"repair journal row {line_no} BVID invalid")
        owner = bvid_owners.setdefault(bvid, plan_id)
        if owner != plan_id:
            raise JournalCorrupt(
                f"duplicate BVID {bvid} is owned by plans {owner} and {plan_id}"
            )
        if plan_id in bindings_by_plan and bindings_by_plan[plan_id] != binding:
            raise JournalCorrupt(
                f"repair journal plan {plan_id} changed immutable bindings"
            )
        bindings_by_plan.setdefault(plan_id, binding)
        previous_state = states_by_plan.get(plan_id)
        if previous_state is None:
            if state != "PLANNED":
                raise JournalCorrupt(
                    f"repair journal plan {plan_id} does not begin PLANNED"
                )
        elif state not in _TRANSITIONS[previous_state]:
            raise JournalCorrupt(
                f"repair journal plan {plan_id} transition "
                f"{previous_state}->{state} is invalid"
            )
        states_by_plan[plan_id] = state
        entries.append(row)
    return entries


def _plan_binding(plan_path: Path, plan: Mapping[str, Any]) -> dict[str, str]:
    replacement = plan.get("replacement") or {}
    return {
        "plan_id": str(plan["plan_id"]),
        "bvid": str(plan["bvid"]),
        "plan_sha256": sha256_file(plan_path.resolve()),
        "manifest_sha256": str((plan.get("manifest") or {})["sha256"]),
        "video_sha256": str((replacement.get("video") or {})["sha256"]),
        "cover_sha256": str((replacement.get("cover") or {})["sha256"]),
    }


def append_journal(
    path: Path,
    *,
    plan_path: Path,
    plan: Mapping[str, Any],
    state: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if state not in JOURNAL_STATES:
        raise ValueError(f"unknown repair state {state}")
    entries = read_journal(path)
    previous_hash = entries[-1]["row_sha256"] if entries else None
    row: dict[str, Any] = {
        "schema_version": JOURNAL_SCHEMA,
        "seq": len(entries) + 1,
        "previous_row_sha256": previous_hash,
        "at": _utc_now(),
        **_plan_binding(plan_path, plan),
        "state": state,
        "details": dict(details or {}),
    }
    row["row_sha256"] = _row_hash(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while appending repair journal")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_parent(path.parent)
    return row


def plan_entries(
    journal: Path, plan_path: Path, plan: Mapping[str, Any]
) -> list[dict[str, Any]]:
    binding = _plan_binding(plan_path, plan)
    entries = read_journal(journal)
    for row in entries:
        if row["bvid"] == binding["bvid"] and row["plan_id"] != binding["plan_id"]:
            raise DuplicateBvid(
                f"{binding['bvid']} already has repair plan {row['plan_id']}"
            )
    selected = [row for row in entries if row["plan_id"] == binding["plan_id"]]
    for row in selected:
        for key in (
            "bvid",
            "plan_sha256",
            "manifest_sha256",
            "video_sha256",
            "cover_sha256",
        ):
            if row.get(key) != binding[key]:
                raise JournalCorrupt(
                    f"repair journal plan binding mismatch for {key}"
                )
    return selected


def assert_bvid_unowned(
    journal: Path, *, bvid: str, plan_id: str
) -> None:
    """Fail before plan-file creation when another plan already owns a BVID."""

    for row in read_journal(journal):
        if row["bvid"] == bvid and row["plan_id"] != plan_id:
            raise DuplicateBvid(
                f"{bvid} already has repair plan {row['plan_id']}"
            )


def initialise_journal(
    journal: Path, plan_path: Path, plan: Mapping[str, Any]
) -> dict[str, Any]:
    selected = plan_entries(journal, plan_path, plan)
    if selected:
        return selected[-1]
    return append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="PLANNED",
        details={"remote_mutation": False},
    )


def _latest_detail(entries: list[dict[str, Any]], key: str) -> object:
    for row in reversed(entries):
        details = row.get("details") or {}
        if key in details:
            return details[key]
    return None


def _before_creator_exact(snapshot: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    return (snapshot.get("creator") or {}) == (
        (plan.get("before") or {}).get("creator") or {}
    )


def _before_public_section_exact(
    snapshot: Mapping[str, Any], plan: Mapping[str, Any]
) -> bool:
    before = plan.get("before") or {}
    return (
        (snapshot.get("public") or {}) == (before.get("public") or {})
        and (snapshot.get("section") or {}) == (before.get("section") or {})
    )


def _public_section_available(snapshot: Mapping[str, Any]) -> bool:
    return bool(
        (snapshot.get("public") or {}).get("available") is True
        and (snapshot.get("section") or {}).get("available") is True
    )


def _two_p_new_video(
    snapshot: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    expected_new: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    creator = snapshot.get("creator") or {}
    before_creator = (plan.get("before") or {}).get("creator") or {}
    videos = creator.get("videos") or []
    old_videos = before_creator.get("videos") or []
    if len(videos) != 2 or len(old_videos) != 1:
        return None
    if videos[0] != old_videos[0]:
        return None
    if creator.get("bvid") != before_creator.get("bvid"):
        return None
    if creator.get("aid") != before_creator.get("aid"):
        return None
    if creator.get("metadata") != before_creator.get("metadata"):
        return None
    new_video = videos[1]
    if new_video.get("cid") == old_videos[0].get("cid"):
        return None
    if expected_new is not None and new_video != expected_new:
        return None
    return dict(new_video)


def _target_creator_exact(
    snapshot: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    new_video: Mapping[str, Any],
    cover_url: str,
) -> bool:
    creator = snapshot.get("creator") or {}
    before_creator = (plan.get("before") or {}).get("creator") or {}
    target = dict(plan.get("target_metadata") or {})
    target["cover"] = _normalise_cover_url(cover_url)
    videos = creator.get("videos") or []
    return bool(
        creator.get("available") is True
        and creator.get("bvid") == plan.get("bvid")
        and creator.get("aid") == before_creator.get("aid")
        and creator.get("metadata") == target
        and len(videos) == 1
        and videos[0].get("cid") == new_video.get("cid")
        and videos[0].get("filename") == new_video.get("filename")
        and videos[0].get("title") == target.get("title")
    )


def _public_section_state(
    snapshot: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    new_video: Mapping[str, Any],
    cover_url: str,
) -> tuple[str, list[str]]:
    """Return target/pending/drift with exact eventual CID+metadata checks."""

    problems = _basic_snapshot_problems(
        snapshot,
        bvid=str(plan["bvid"]),
        aid=((plan.get("before") or {}).get("creator") or {}).get("aid"),
    )
    public = snapshot.get("public") or {}
    section = snapshot.get("section") or {}
    if public.get("available") is not True or section.get("available") is not True:
        return "pending", problems
    matches = section.get("matches") or []
    if len(matches) != 1:
        problems.append(
            f"exact section has {len(matches)} BVID/AID matches, expected 1"
        )
        return "drift", problems
    before = plan.get("before") or {}
    before_public = before.get("public") or {}
    before_match = ((before.get("section") or {}).get("matches") or [{}])[0]
    target_metadata = dict(plan.get("target_metadata") or {})
    target_metadata["cover"] = _normalise_cover_url(cover_url)
    target_public_metadata = {
        key: value
        for key, value in target_metadata.items()
        if key != "source"
    }
    target_public = {
        "available": True,
        "bvid": plan["bvid"],
        "aid": ((before.get("creator") or {}).get("aid")),
        "cid": new_video["cid"],
        "state": 0,
        "metadata": target_public_metadata,
    }
    target_match = {
        "bvid": before_match.get("bvid"),
        "aid": ((before.get("creator") or {}).get("aid")),
        "cid": new_video["cid"],
        "title": target_metadata["title"],
    }
    public_is_target = public == target_public
    section_is_target = matches[0] == target_match
    if public_is_target and section_is_target and not problems:
        return "target", []

    def value_in_transition(
        current: Mapping[str, Any],
        old: Mapping[str, Any],
        new: Mapping[str, Any],
    ) -> bool:
        if set(current) != set(old) or set(current) != set(new):
            return False
        for key, value in current.items():
            old_value = old.get(key)
            new_value = new.get(key)
            if isinstance(value, Mapping):
                if not isinstance(old_value, Mapping) or not isinstance(
                    new_value, Mapping
                ):
                    return False
                if not value_in_transition(value, old_value, new_value):
                    return False
            elif value not in (old_value, new_value):
                return False
        return True

    if value_in_transition(public, before_public, target_public) and value_in_transition(
        matches[0], before_match, target_match
    ):
        return "pending", problems
    problems.append("public or section surface contains values outside before/target")
    return "drift", problems


def _block(
    journal: Path,
    plan_path: Path,
    plan: Mapping[str, Any],
    *,
    reason: str,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="BLOCKED_DRIFT",
        details={
            "reason": reason,
            "snapshot": dict(snapshot or {}),
            "remote_mutation": False,
        },
    )


@dataclass(frozen=True)
class RepairResult:
    state: str
    changed: bool
    message: str
    details: Mapping[str, Any]


def repair_status(
    *, plan_path: Path, journal: Path, manifest: Mapping[str, Any]
) -> RepairResult:
    plan = load_plan(plan_path)
    validate_plan(plan, manifest=manifest, plan_path=plan_path)
    entries = plan_entries(journal, plan_path, plan)
    if not entries:
        return RepairResult(
            state="PLANNED",
            changed=False,
            message="plan is valid but has not been journaled",
            details={"next_action": "INITIALISE"},
        )
    last = entries[-1]
    next_action = {
        "PLANNED": "APPEND_EXISTING_ONCE",
        "APPEND_INTENT": "POLL_ONLY_NEVER_REAPPEND",
        "APPEND_AMBIGUOUS": "POLL_ONLY_NEVER_REAPPEND",
        "TWO_P_READY": "SWAP_KEEP_NEW_CID",
        "SWAP_RETRYABLE": "POLL_THEN_RETRY_SAME_SWAP",
        "CREATOR_SINGLE_NEW": "VERIFY_PUBLIC_AND_SECTION",
        "PUBLIC_PENDING": "POLL_PUBLIC_AND_SECTION",
        "VERIFIED": "NONE",
        "BLOCKED_DRIFT": "HUMAN_RECONCILIATION_REQUIRED",
    }[last["state"]]
    return RepairResult(
        state=last["state"],
        changed=False,
        message=f"repair state is {last['state']}",
        details={
            "next_action": next_action,
            "last_event": last,
            "event_count": len(entries),
        },
    )


def _row_result(row: Mapping[str, Any], message: str | None = None) -> RepairResult:
    details = row.get("details") or {}
    return RepairResult(
        str(row["state"]),
        True,
        message or str(details.get("reason") or row["state"]),
        details,
    )


def _append_stage(
    *,
    state: str,
    snapshot: Mapping[str, Any],
    plan_path: Path,
    journal: Path,
    plan: Mapping[str, Any],
    adapter: RepairAdapter,
    bvid: str,
) -> RepairResult:
    """Start append once, or reconcile its permanently ambiguous outcome."""

    if state == "PLANNED":
        if snapshot != plan.get("before"):
            return _row_result(
                _block(
                    journal,
                    plan_path,
                    plan,
                    reason="live state drifted from the exact planning snapshot before append",
                    snapshot=snapshot,
                )
            )
        append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="APPEND_INTENT",
            details={
                "old_cid": plan["before"]["creator"]["videos"][0]["cid"],
                "remote_mutation": "append_existing_once",
            },
        )
        # BaseException deliberately escapes: the durable intent forbids retry.
        try:
            adapter.append_existing(
                bvid, Path(plan["replacement"]["video"]["path"])
            )
            outcome = "append call returned"
        except Exception as exc:
            outcome = f"append call raised {type(exc).__name__}: {exc}"
        row = append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="APPEND_AMBIGUOUS",
            details={
                "reason": outcome,
                "rule": "POLL_ONLY_NEVER_REAPPEND",
                "remote_mutation": False,
            },
        )
        return _row_result(
            row, "append outcome is reconciled only by live topology"
        )

    if _before_creator_exact(snapshot, plan):
        if state == "APPEND_INTENT":
            row = append_journal(
                journal,
                plan_path=plan_path,
                plan=plan,
                state="APPEND_AMBIGUOUS",
                details={
                    "reason": "one old P observed after durable append intent",
                    "rule": "POLL_ONLY_NEVER_REAPPEND",
                    "remote_mutation": False,
                },
            )
            return _row_result(row, "append remains ambiguous")
        return RepairResult(
            state,
            False,
            "only the old P is visible; poll only and never append again",
            {"rule": "POLL_ONLY_NEVER_REAPPEND"},
        )
    new_video = _two_p_new_video(snapshot, plan)
    if new_video is not None and not _public_section_available(snapshot):
        return RepairResult(
            state,
            False,
            "new P is visible but public/section readback is unavailable; poll only",
            {"rule": "POLL_ONLY_NEVER_REAPPEND"},
        )
    if new_video is not None and _before_public_section_exact(snapshot, plan):
        row = append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="TWO_P_READY",
            details={"new_video": new_video, "remote_mutation": False},
        )
        return _row_result(row, "exact old+new topology observed")
    return _row_result(
        _block(
            journal,
            plan_path,
            plan,
            reason="append reconciliation found unknown P/CID/metadata/section topology",
            snapshot=snapshot,
        )
    )


def _prepare_swap_cover(
    *,
    cover_url: str | None,
    new_video: Mapping[str, Any],
    plan_path: Path,
    journal: Path,
    plan: Mapping[str, Any],
    adapter: RepairAdapter,
) -> tuple[str | None, RepairResult | None]:
    if cover_url is not None:
        return cover_url, None
    try:
        prepared = adapter.prepare_cover(Path(plan["replacement"]["cover"]["path"]))
    except Exception as exc:
        row = append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="SWAP_RETRYABLE",
            details={
                "new_video": dict(new_video),
                "reason": f"cover preparation failed: {type(exc).__name__}: {exc}",
                "remote_mutation": False,
            },
        )
        return None, _row_result(row, "cover preparation is retryable")
    if not isinstance(prepared, str) or not prepared.startswith(
        ("http://", "https://", "//")
    ):
        return None, _row_result(
            _block(
                journal,
                plan_path,
                plan,
                reason="cover preparation returned an invalid URL",
            )
        )
    return prepared, None


def _swap_outcome(
    *,
    after: Mapping[str, Any] | None,
    mutation_error: Exception | None,
    new_video: Mapping[str, Any],
    cover_url: str,
    plan_path: Path,
    journal: Path,
    plan: Mapping[str, Any],
) -> RepairResult:
    if after is not None and _target_creator_exact(
        after, plan, new_video=new_video, cover_url=cover_url
    ):
        row = append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="CREATOR_SINGLE_NEW",
            details={
                "new_video": dict(new_video),
                "cover_url": cover_url,
                "swap_observation": (
                    "live success after ambiguous error"
                    if mutation_error is not None
                    else "live success"
                ),
                "remote_mutation": False,
            },
        )
        return _row_result(row, "Creator single-new verified")
    two_p = (
        _two_p_new_video(after, plan, expected_new=new_video)
        if after is not None
        else None
    )
    if after is not None and two_p is None:
        return _row_result(
            _block(
                journal,
                plan_path,
                plan,
                reason="post-swap observation is neither exact two-P nor exact single-new",
                snapshot=after,
            )
        )
    if after is not None and _public_section_available(after):
        if not _before_public_section_exact(after, plan):
            return _row_result(
                _block(
                    journal,
                    plan_path,
                    plan,
                    reason="post-swap public/section projection drifted",
                    snapshot=after,
                )
            )
    else:
        after = None
    if mutation_error is not None:
        code = (
            mutation_error.code
            if isinstance(mutation_error, RemoteMutationError)
            else None
        )
        retryable = code == RETRYABLE_EDIT_CODE or isinstance(
            mutation_error, (TimeoutError, OSError)
        )
        if not retryable:
            return _row_result(
                _block(
                    journal,
                    plan_path,
                    plan,
                    reason=(
                        "non-retryable swap error after unchanged topology: "
                        f"{type(mutation_error).__name__}: {mutation_error}"
                    ),
                    snapshot=after,
                )
            )
        reason = (
            f"retryable edit code {code}"
            if code is not None
            else f"ambiguous {type(mutation_error).__name__}"
        )
    else:
        reason = "edit returned but exact Creator projection is still pending"
    row = append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="SWAP_RETRYABLE",
        details={
            "new_video": dict(new_video),
            "cover_url": cover_url,
            "reason": reason,
            "remote_mutation": False,
        },
    )
    return _row_result(row, reason)


def _swap_stage(
    *,
    state: str,
    snapshot: Mapping[str, Any],
    new_video: Mapping[str, Any],
    cover_url: str | None,
    plan_path: Path,
    journal: Path,
    plan: Mapping[str, Any],
    adapter: RepairAdapter,
    bvid: str,
    section_id: int,
) -> RepairResult:
    """Verify exact two-P truth, then retry only one frozen keep-CID edit."""

    if cover_url is not None and _target_creator_exact(
        snapshot, plan, new_video=new_video, cover_url=cover_url
    ):
        row = append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="CREATOR_SINGLE_NEW",
            details={
                "new_video": dict(new_video),
                "cover_url": cover_url,
                "remote_mutation": False,
            },
        )
        return _row_result(row, "Creator is already single-new")
    if _two_p_new_video(snapshot, plan, expected_new=new_video) is None:
        return _row_result(
            _block(
                journal,
                plan_path,
                plan,
                reason="swap precondition drifted from exact old+new topology",
                snapshot=snapshot,
            )
        )
    if not _public_section_available(snapshot):
        return RepairResult(
            state,
            False,
            "exact two-P Creator topology is visible but public/section readback is unavailable",
            {"remote_mutation": False},
        )
    if not _before_public_section_exact(snapshot, plan):
        return _row_result(
            _block(
                journal,
                plan_path,
                plan,
                reason="swap precondition public/section projection drifted",
                snapshot=snapshot,
            )
        )
    cover_url, cover_result = _prepare_swap_cover(
        cover_url=cover_url,
        new_video=new_video,
        plan_path=plan_path,
        journal=journal,
        plan=plan,
        adapter=adapter,
    )
    if cover_result is not None:
        return cover_result
    assert cover_url is not None
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="SWAP_RETRYABLE",
        details={
            "new_video": dict(new_video),
            "cover_url": cover_url,
            "reason": (
                "exact swap intent prepared"
                if _latest_detail(
                    plan_entries(journal, plan_path, plan)[:-1], "cover_url"
                )
                is None
                else "retrying the exact same idempotent swap"
            ),
            "remote_mutation": "edit_keep_only_new_cid",
        },
    )
    mutation_error: Exception | None = None
    try:
        adapter.swap_keep_only(
            bvid,
            keep_cid=int(new_video["cid"]),
            target_metadata=plan["target_metadata"],
            cover_url=cover_url,
        )
    except Exception as exc:
        mutation_error = exc
    try:
        after = adapter.observe(bvid, section_id)
    except ObservationUnavailable:
        after = None
    return _swap_outcome(
        after=after,
        mutation_error=mutation_error,
        new_video=new_video,
        cover_url=cover_url,
        plan_path=plan_path,
        journal=journal,
        plan=plan,
    )


def _final_projection_stage(
    *,
    state: str,
    snapshot: Mapping[str, Any],
    new_video: Mapping[str, Any],
    cover_url: str,
    plan_path: Path,
    journal: Path,
    plan: Mapping[str, Any],
) -> RepairResult:
    if not _target_creator_exact(
        snapshot, plan, new_video=new_video, cover_url=cover_url
    ):
        return _row_result(
            _block(
                journal,
                plan_path,
                plan,
                reason="Creator single-new CID or target metadata drifted",
                snapshot=snapshot,
            )
        )
    projection, problems = _public_section_state(
        snapshot, plan, new_video=new_video, cover_url=cover_url
    )
    if projection == "target":
        row = append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="VERIFIED",
            details={
                "new_video": dict(new_video),
                "cover_url": cover_url,
                "live_snapshot": snapshot,
                "remote_mutation": False,
            },
        )
        return _row_result(
            row,
            "Creator/public/section all verify the same new CID and metadata",
        )
    if projection == "drift":
        return _row_result(
            _block(
                journal,
                plan_path,
                plan,
                reason="; ".join(problems)
                or "public/section projection drifted",
                snapshot=snapshot,
            )
        )
    if state == "PUBLIC_PENDING":
        return RepairResult(
            state,
            False,
            "public/section propagation pending",
            {"problems": problems},
        )
    row = append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="PUBLIC_PENDING",
        details={
            "new_video": dict(new_video),
            "cover_url": cover_url,
            "problems": problems,
            "remote_mutation": False,
        },
    )
    return _row_result(row, "public/section propagation pending")


def repair_step(
    *,
    plan_path: Path,
    journal: Path,
    manifest: Mapping[str, Any],
    adapter: RepairAdapter,
) -> RepairResult:
    """Advance at most one mutation boundary, always from current live truth."""

    plan_path = plan_path.resolve()
    plan = load_plan(plan_path)
    validate_plan(plan, manifest=manifest, plan_path=plan_path)
    last = initialise_journal(journal, plan_path, plan)
    entries = plan_entries(journal, plan_path, plan)
    state = str(last["state"])
    if state in TERMINAL_STATES:
        return RepairResult(
            state, False, "terminal repair state is idempotent", last.get("details") or {}
        )
    bvid = str(plan["bvid"])
    section_id = int((plan.get("season") or {})["section_id"])
    try:
        snapshot = adapter.observe(bvid, section_id)
    except ObservationUnavailable as exc:
        return RepairResult(
            state, False, str(exc), {"observation_unavailable": True}
        )
    if state in {"PLANNED", "APPEND_INTENT", "APPEND_AMBIGUOUS"}:
        return _append_stage(
            state=state,
            snapshot=snapshot,
            plan_path=plan_path,
            journal=journal,
            plan=plan,
            adapter=adapter,
            bvid=bvid,
        )
    new_video = _latest_detail(entries, "new_video")
    if not isinstance(new_video, Mapping):
        raise JournalCorrupt(f"{state} has no frozen new_video identity")
    cover_url = _latest_detail(entries, "cover_url")
    if cover_url is not None and (
        not isinstance(cover_url, str)
        or not cover_url.startswith(("http://", "https://", "//"))
    ):
        raise JournalCorrupt("journal cover_url detail is invalid")
    if state in {"TWO_P_READY", "SWAP_RETRYABLE"}:
        return _swap_stage(
            state=state,
            snapshot=snapshot,
            new_video=new_video,
            cover_url=cover_url,
            plan_path=plan_path,
            journal=journal,
            plan=plan,
            adapter=adapter,
            bvid=bvid,
            section_id=section_id,
        )
    if state in {"CREATOR_SINGLE_NEW", "PUBLIC_PENDING"} and isinstance(cover_url, str):
        return _final_projection_stage(
            state=state,
            snapshot=snapshot,
            new_video=new_video,
            cover_url=cover_url,
            plan_path=plan_path,
            journal=journal,
            plan=plan,
        )
    raise JournalCorrupt(f"unhandled repair state {state}")


def run_repair(
    *,
    plan_path: Path,
    journal: Path,
    manifest: Mapping[str, Any],
    adapter: RepairAdapter,
    wait_seconds: float = 0.0,
    poll_seconds: float = 10.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> RepairResult:
    """Drive until verified/blocked or no observable progress before deadline."""

    deadline = time.monotonic() + max(0.0, wait_seconds)
    last_signature: tuple[str, str] | None = None
    while True:
        result = repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        )
        if result.state in TERMINAL_STATES:
            return result
        signature = (result.state, result.message)
        if result.changed:
            if result.state in {"SWAP_RETRYABLE", "PUBLIC_PENDING"}:
                if time.monotonic() >= deadline:
                    return result
                sleeper(max(0.0, poll_seconds))
            # Keep advancing immediate, already-observable transitions without
            # sleeping.  The bounded guard catches a faulty adapter/state loop.
            last_signature = None
            continue
        if time.monotonic() >= deadline:
            return result
        if signature == last_signature:
            sleeper(max(0.0, poll_seconds))
        else:
            last_signature = signature
            sleeper(max(0.0, poll_seconds))


def preview_repair(*, plan_path: Path, journal: Path, manifest: Mapping[str, Any]) -> RepairResult:
    """Strict local dry-run: validate and report; perform no writes or HTTP."""

    return repair_status(plan_path=plan_path, journal=journal, manifest=manifest)
