"""Plan binding for a same-BV title-and-cover-only revision."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.same_bv_cover_reconciliation import (
    normalise_cover_url,
    snapshot_with_current_cover_identity,
)
from src.autoslice import same_bv_title_cover_authority as authority_binding


PLAN_SCHEMA = "same-bv-title-cover-repair-plan.v1"
_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}\Z")


class TitleCoverRepairError(RuntimeError):
    """Base class for title-cover planning/execution failures."""


class TitleCoverPlanInvalid(TitleCoverRepairError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path, label: str) -> dict[str, Any]:
    path = path.expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TitleCoverPlanInvalid(f"{label} is not a regular non-symlink file")
    path = path.resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise TitleCoverPlanInvalid(f"{label} is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TitleCoverPlanInvalid(f"{label} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise TitleCoverPlanInvalid(f"{label} must be an object")
    return value


def canonical_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    result = snapshot_with_current_cover_identity(copy.deepcopy(snapshot))
    for surface_name in ("creator", "public"):
        surface = result.get(surface_name)
        if not isinstance(surface, dict):
            continue
        metadata = surface.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("tags"), list):
            metadata["tags"] = sorted(metadata["tags"])
    return result


def baseline_snapshot_problems(
    snapshot: Mapping[str, Any], authority: Mapping[str, Any]
) -> list[str]:
    current = canonical_snapshot(snapshot)
    baseline = authority.get("baseline") or {}
    identity = baseline.get("identity") or {}
    metadata = baseline.get("metadata") or {}
    bvid, aid, cid = identity.get("bvid"), identity.get("aid"), identity.get("cid")
    title = baseline.get("old_title")
    creator = current.get("creator") or {}
    public = current.get("public") or {}
    section = current.get("section") or {}
    problems: list[str] = []
    if any(surface.get("available") is not True for surface in (creator, public, section)):
        problems.append("all Creator/public/section surfaces must be available")
    if (creator.get("bvid"), public.get("bvid")) != (bvid, bvid):
        problems.append("live BVID differs from baseline")
    if (creator.get("aid"), public.get("aid")) != (aid, aid):
        problems.append("live AID differs from baseline")
    if public.get("cid") != cid:
        problems.append("live public CID differs from baseline")
    if creator.get("state") != 0 or public.get("state") != 0:
        problems.append("live archive is not fully public")
    expected_creator = {
        "title": title,
        "desc": metadata.get("description"),
        "tags": sorted(metadata.get("tags") or []),
        "tid": metadata.get("tid"),
        "copyright": metadata.get("copyright"),
        "source": metadata.get("source"),
    }
    creator_metadata = dict(creator.get("metadata") or {})
    public_metadata = dict(public.get("metadata") or {})
    creator_cover = creator_metadata.pop("cover", None)
    public_cover = public_metadata.pop("cover", None)
    if creator_metadata != expected_creator:
        problems.append("Creator metadata differs outside the proposed revision")
    if public_metadata != {k: v for k, v in expected_creator.items() if k != "source"}:
        problems.append("public metadata differs outside the proposed revision")
    if (
        not isinstance(creator_cover, str)
        or not isinstance(public_cover, str)
        or normalise_cover_url(creator_cover) != normalise_cover_url(public_cover)
    ):
        problems.append("Creator/public baseline cover identity is unavailable or inconsistent")
    videos = creator.get("videos") or []
    if (
        not isinstance(videos, list)
        or len(videos) != 1
        or videos[0].get("cid") != cid
        or videos[0].get("title") != title
        or not isinstance(videos[0].get("filename"), str)
        or not videos[0].get("filename")
    ):
        problems.append("Creator page identity/title differs from baseline")
    matches = section.get("matches") or []
    expected_match = {"bvid": bvid, "aid": aid, "cid": cid, "title": title}
    if section.get("section_id") != metadata.get("section_id") or matches != [expected_match]:
        problems.append("exact section episode differs from baseline")
    return problems


def create_plan(
    *,
    authority_path: Path,
    authority: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    authority_path = authority_path.resolve()
    validated = authority_binding.validate_authority(authority)
    identity = (validated.get("baseline") or {}).get("identity") or {}
    bvid = str(identity.get("bvid") or "")
    if _BVID_RE.fullmatch(bvid) is None:
        raise TitleCoverPlanInvalid("authority BVID is invalid")
    problems = baseline_snapshot_problems(snapshot, validated)
    if problems:
        raise TitleCoverPlanInvalid("; ".join(problems))
    before = canonical_snapshot(snapshot)
    baseline = validated.get("baseline") or {}
    target = validated.get("target") or {}
    metadata = baseline.get("metadata") or {}
    plan = {
        "schema_version": PLAN_SCHEMA,
        "plan_id": uuid.uuid4().hex,
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "authority": {
            "path": str(authority_path),
            "sha256": sha256_file(authority_path),
            "authority_sha256": validated["authority_sha256"],
        },
        "candidate_id": baseline.get("candidate_id"),
        "bvid": bvid,
        "aid": identity.get("aid"),
        "unchanged_cid": identity.get("cid"),
        "season": {"season_id": metadata.get("season_id"), "section_id": metadata.get("section_id")},
        "old_title": baseline.get("old_title"),
        "target_title": target.get("title"),
        "replacement_cover": dict(target.get("cover") or {}),
        "old_cover_url": ((before.get("creator") or {}).get("metadata") or {}).get("cover"),
        "unchanged_metadata": dict(metadata),
        "media_identity": {
            "video_sha256": baseline.get("video_sha256"),
            "subtitle_sha256": baseline.get("subtitle_sha256"),
        },
        "authorization": dict(validated.get("authorization") or {}),
        "quality_evidence": copy.deepcopy(validated.get("quality_evidence") or {}),
        "before": before,
    }
    validate_plan(plan, authority=validated)
    return plan


def validate_plan(
    plan: Mapping[str, Any],
    *,
    authority: Mapping[str, Any] | None = None,
    plan_path: Path | None = None,
) -> dict[str, Any]:
    problems: list[str] = []
    if plan.get("schema_version") != PLAN_SCHEMA:
        problems.append("title-cover plan schema mismatch")
    if not isinstance(plan.get("plan_id"), str) or not plan.get("plan_id"):
        problems.append("title-cover plan_id missing")
    entry = plan.get("authority") or {}
    authority_path = Path(str(entry.get("path") or ""))
    try:
        loaded = authority_binding.load_authority(authority_path)
    except Exception as exc:
        problems.append(f"bound authority invalid: {exc}")
        loaded = {}
    else:
        if sha256_file(authority_path) != entry.get("sha256") or loaded.get("authority_sha256") != entry.get("authority_sha256"):
            problems.append("bound authority hash drift")
        if authority is not None and dict(authority) != loaded:
            problems.append("runtime authority differs from bound authority")
    baseline = loaded.get("baseline") or {}
    identity = baseline.get("identity") or {}
    target = loaded.get("target") or {}
    metadata = baseline.get("metadata") or {}
    expected = {
        "candidate_id": baseline.get("candidate_id"),
        "bvid": identity.get("bvid"),
        "aid": identity.get("aid"),
        "unchanged_cid": identity.get("cid"),
        "old_title": baseline.get("old_title"),
        "target_title": target.get("title"),
        "unchanged_metadata": metadata,
        "authorization": loaded.get("authorization"),
        "quality_evidence": loaded.get("quality_evidence"),
    }
    for key, value in expected.items():
        if plan.get(key) != value:
            problems.append(f"plan {key} drifted from authority")
    if plan.get("season") != {"season_id": metadata.get("season_id"), "section_id": metadata.get("section_id")}:
        problems.append("plan season binding drifted")
    if plan.get("media_identity") != {
        "video_sha256": baseline.get("video_sha256"),
        "subtitle_sha256": baseline.get("subtitle_sha256"),
    }:
        problems.append("plan media identity drifted")
    replacement = plan.get("replacement_cover") or {}
    if replacement != target.get("cover"):
        problems.append("replacement cover differs from authority")
    try:
        authority_binding.validate_descriptor(replacement, "replacement cover")
    except Exception as exc:
        problems.append(str(exc))
    before = plan.get("before") or {}
    if loaded:
        problems.extend(baseline_snapshot_problems(before, loaded))
    if plan.get("old_cover_url") != (((before.get("creator") or {}).get("metadata") or {}).get("cover")):
        problems.append("old cover URL binding drifted")
    if plan_path is not None and not plan_path.is_file():
        problems.append("title-cover plan file missing")
    if problems:
        raise TitleCoverPlanInvalid("; ".join(dict.fromkeys(problems)))
    return loaded


def write_plan(path: Path, plan: Mapping[str, Any]) -> None:
    authority = authority_binding.load_authority(Path(str((plan.get("authority") or {})["path"])))
    validate_plan(plan, authority=authority)
    create_json(path, plan)


def load_plan(path: Path) -> dict[str, Any]:
    return load_json(path, "title-cover plan")


# Bilibili preserves the original per-part title (our candidate id) while an
# archive metadata edit changes the public/Creator archive title.  The strict
# validator historically compared both fields with old_title.  Keep the real
# snapshot intact and normalize only this exact, authority-bound equivalence
# for validation; every other identity/title difference remains a blocker.
_baseline_snapshot_problems_strict = baseline_snapshot_problems


def _candidate_part_title_validation_view(snapshot, authority):
    baseline = authority.get("baseline") if isinstance(authority, dict) else None
    creator = snapshot.get("creator") if isinstance(snapshot, dict) else None
    metadata = creator.get("metadata") if isinstance(creator, dict) else None
    videos = creator.get("videos") if isinstance(creator, dict) else None
    if (
        not isinstance(baseline, dict)
        or not isinstance(metadata, dict)
        or not isinstance(videos, list)
        or not videos
        or not isinstance(videos[0], dict)
    ):
        return snapshot
    old_title = baseline.get("old_title")
    candidate_id = baseline.get("candidate_id")
    if (
        not isinstance(old_title, str)
        or not old_title
        or not isinstance(candidate_id, str)
        or not candidate_id
        or metadata.get("title") != old_title
        or videos[0].get("title") != candidate_id
    ):
        return snapshot
    normalized_video = {**videos[0], "title": old_title}
    normalized_creator = {**creator, "videos": [normalized_video, *videos[1:]]}
    return {**snapshot, "creator": normalized_creator}


def baseline_snapshot_problems(snapshot, authority):
    return _baseline_snapshot_problems_strict(
        _candidate_part_title_validation_view(snapshot, authority), authority
    )
