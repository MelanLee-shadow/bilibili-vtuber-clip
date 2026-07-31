"""Crash-safe, manifest-bound cover-only repair of one existing BVID.

This lane is intentionally narrower than :mod:`same_bv_repair`: it never
appends media and never changes the sole CID.  A plan freezes the complete
Creator/public/tags/exact-section snapshot, the old cover asset, the reviewed
new cover bytes, and the authorized manifest.  The edit payload is cloned
from a final Creator read and changes only ``cover``.

The archive edit is one-shot.  ``EDIT_INTENT`` is fsynced before the call; an
ambiguous response is reconciled only by live observation and is never
blindly retried.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from src.autoslice import same_bv_repair as repair
from src.autoslice.same_bv_cover_reconciliation import (
    normalise_cover_url,
    snapshot_with_current_cover_identity,
    snapshots_equivalent,
)


PLAN_SCHEMA = "same-bv-cover-repair-plan.v1"
JOURNAL_SCHEMA = "same-bv-cover-repair-journal.v1"
COMPLETED_SCHEMA = "same-bv-cover-repair-completed.v1"
JOURNAL_STATES = {
    "PLANNED",
    "COVER_UPLOAD_INTENT",
    "COVER_UPLOADED",
    "EDIT_INTENT",
    "EDIT_AMBIGUOUS",
    "PUBLIC_PENDING",
    "VERIFIED",
    "BLOCKED_DRIFT",
}
TERMINAL_STATES = {"VERIFIED", "BLOCKED_DRIFT"}
_TRANSITIONS = {
    "PLANNED": {"COVER_UPLOAD_INTENT", "BLOCKED_DRIFT"},
    # Uploading identical bytes may be retried after a process crash because
    # cover/up alone cannot change an archive; at worst it leaves an orphaned
    # immutable asset.  The archive edit below is deliberately not retryable.
    "COVER_UPLOAD_INTENT": {
        "COVER_UPLOAD_INTENT",
        "COVER_UPLOADED",
        "BLOCKED_DRIFT",
    },
    "COVER_UPLOADED": {"EDIT_INTENT", "BLOCKED_DRIFT"},
    "EDIT_INTENT": {
        "EDIT_AMBIGUOUS",
        "PUBLIC_PENDING",
        "VERIFIED",
        "BLOCKED_DRIFT",
    },
    "EDIT_AMBIGUOUS": {
        "EDIT_AMBIGUOUS",
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
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")


class CoverRepairError(repair.RepairError):
    """Cover-only repair authority or transaction is invalid."""


class CoverPlanInvalid(CoverRepairError):
    """The immutable cover-only plan cannot be trusted."""


class CoverJournalCorrupt(CoverRepairError):
    """The append-only cover journal was edited, truncated, or branched."""


class CoverAdapter(Protocol):
    def observe(self, bvid: str, section_id: int) -> dict[str, Any]: ...

    def prepare_cover(self, cover_path: Path) -> str: ...

    def edit_cover_only(
        self,
        bvid: str,
        *,
        expected_creator: Mapping[str, Any],
        cover_url: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class CoverRepairResult:
    state: str
    changed: bool
    message: str
    details: Mapping[str, Any]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    import hashlib

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _fsync_parent(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise CoverPlanInvalid(f"create-only path already exists: {path}") from exc
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
    _fsync_parent(path.parent)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CoverPlanInvalid(f"{label} unreadable: {path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise CoverPlanInvalid(f"{label} must be a JSON object: {path}")
    return payload


def _known_cover_asset(value: object) -> bool:
    canonical = normalise_cover_url(value)
    return bool(
        isinstance(canonical, str)
        and canonical.startswith("//bilibili-cover-asset/bfs/archive/")
    )


def _without_cover(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(snapshot_with_current_cover_identity(snapshot))
    for surface in ("creator", "public"):
        metadata = ((result.get(surface) or {}).get("metadata") or {})
        if isinstance(metadata, dict):
            metadata.pop("cover", None)
    return result


def _manifest_noncover_metadata(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    target = dict(repair._target_metadata(manifest))
    target.pop("cover", None)
    return target


def _cover_only_scope_binding_problems(
    manifest: Mapping[str, Any],
    *,
    predecessor_completed_path: Path | None,
) -> list[str]:
    """Bind an optional audit bridge to this exact cover transaction."""

    problems: list[str] = []
    package_attestation = manifest.get("package_attestation")
    entry = (
        package_attestation.get("cover_only_audit_scope")
        if isinstance(package_attestation, Mapping)
        else None
    )
    if entry is None:
        return problems
    if not isinstance(entry, Mapping):
        return ["cover-only audit scope package attestation is invalid"]
    if predecessor_completed_path is None:
        return [
            "cover-only audit scope requires the same explicit predecessor "
            "completed sidecar"
        ]
    scope_path = Path(str(entry.get("path") or ""))
    if scope_path.is_symlink() or not scope_path.is_absolute() or not scope_path.is_file():
        return ["cover-only audit scope file is missing or unsafe"]
    if repair.sha256_file(scope_path) != entry.get("sha256"):
        problems.append("cover-only audit scope hash drift")
    if scope_path.stat().st_size != entry.get("bytes"):
        problems.append("cover-only audit scope byte-size drift")
    try:
        scope = _load_json(scope_path, "cover-only audit scope")
    except CoverPlanInvalid as exc:
        return [str(exc)]
    predecessor = scope.get("predecessor")
    completed = (
        predecessor.get("completed")
        if isinstance(predecessor, Mapping)
        else None
    )
    predecessor_path = predecessor_completed_path.resolve()
    if (
        not isinstance(completed, Mapping)
        or completed.get("path") != str(predecessor_path)
        or completed.get("sha256") != repair.sha256_file(predecessor_path)
        or completed.get("bytes") != predecessor_path.stat().st_size
    ):
        problems.append(
            "cover-only audit scope predecessor differs from repair predecessor"
        )
    scope_authorization = scope.get("authorization")
    manifest_authorization = manifest.get("authorization") or {}
    if scope_authorization != {
        "by": manifest_authorization.get("by"),
        "quote": manifest_authorization.get("quote"),
    }:
        problems.append("cover-only audit scope authorization drifted")
    publication = scope.get("publication_target")
    authority = manifest.get("recovery_publication_authority") or {}
    if (
        not isinstance(publication, Mapping)
        or publication.get("bvid") != authority.get("bvid")
        or publication.get("aid") != authority.get("aid")
        or publication.get("original_cid") != authority.get("cid")
        or publication.get("authority_sha256")
        != authority.get("authority_sha256")
    ):
        problems.append("cover-only audit scope publication target drifted")
    frozen = scope.get("frozen_noncover")
    scope_metadata = (
        {
            "title": frozen.get("title"),
            "desc": frozen.get("description"),
            "tags": frozen.get("tags"),
            **dict(frozen.get("publish_policy") or {}),
        }
        if isinstance(frozen, Mapping)
        else None
    )
    if scope_metadata != _manifest_noncover_metadata(manifest):
        problems.append("cover-only audit scope frozen metadata drifted")
    scope_video = frozen.get("video") if isinstance(frozen, Mapping) else None
    manifest_video = manifest.get("video") or {}
    if not isinstance(scope_video, Mapping) or any(
        scope_video.get(key) != manifest_video.get(key)
        for key in ("sha256", "bytes")
    ):
        problems.append("cover-only audit scope video binding drifted")
    current = scope.get("current_package")
    scope_cover = current.get("cover") if isinstance(current, Mapping) else None
    manifest_cover = manifest.get("cover") or {}
    if not isinstance(scope_cover, Mapping) or any(
        scope_cover.get(key) != manifest_cover.get(key)
        for key in ("sha256", "bytes")
    ):
        problems.append("cover-only audit scope cover binding drifted")
    return list(dict.fromkeys(problems))


def _snapshot_plan_problems(
    snapshot: Mapping[str, Any],
    *,
    bvid: str,
    authority: Mapping[str, Any],
    target_metadata: Mapping[str, Any],
    section_id: int,
    expected_cid: int,
) -> list[str]:
    problems = repair._basic_snapshot_problems(
        snapshot, bvid=bvid, aid=authority.get("aid")
    )
    current = snapshot_with_current_cover_identity(snapshot)
    creator = current.get("creator") or {}
    public = current.get("public") or {}
    section = current.get("section") or {}
    videos = creator.get("videos") or []
    if creator.get("state") != 0:
        problems.append("Creator archive is not state=0")
    if len(videos) != 1:
        problems.append(
            f"cover-only repair requires exactly one Creator P, got {len(videos)}"
        )
    elif videos[0].get("cid") != expected_cid:
        problems.append("sole Creator CID differs from proven predecessor")
    if public.get("available") is not True:
        problems.append("public archive/tags are unavailable")
    elif len(videos) == 1:
        if public.get("state") != 0:
            problems.append("public archive is not state=0")
        if public.get("aid") != authority.get("aid"):
            problems.append("public AID differs from publication authority")
        if public.get("cid") != videos[0].get("cid"):
            problems.append("public CID differs from sole Creator CID")
        public_expected = {
            key: value
            for key, value in (creator.get("metadata") or {}).items()
            if key != "source"
        }
        if public.get("metadata") != public_expected:
            problems.append("Creator and public metadata disagree")
    if section.get("available") is not True:
        problems.append("exact section is unavailable")
    if section.get("section_id") != section_id:
        problems.append("exact section id mismatch")
    matches = section.get("matches") or []
    if len(matches) != 1:
        problems.append(
            f"cover-only repair requires one exact-section match, got {len(matches)}"
        )
    elif len(videos) == 1:
        if matches[0].get("aid") != authority.get("aid"):
            problems.append("section AID differs from publication authority")
        if matches[0].get("cid") != videos[0].get("cid"):
            problems.append("section CID differs from sole Creator CID")
        if matches[0].get("title") != (creator.get("metadata") or {}).get(
            "title"
        ):
            problems.append("section title differs from Creator title")
    creator_metadata = dict(creator.get("metadata") or {})
    old_cover = creator_metadata.pop("cover", None)
    if creator_metadata != dict(target_metadata):
        problems.append(
            "manifest target metadata differs from live archive outside cover"
        )
    if not _known_cover_asset(old_cover):
        problems.append("old Creator cover is not a known Bilibili asset")
    return list(dict.fromkeys(problems))


def _cover_predecessor_attestation(
    completed_path: Path,
    *,
    authority: Mapping[str, Any],
    bvid: str,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove a prior CID replacement while permitting only later cover drift.

    A historical full same-BV completion remains authoritative for the CID and
    all non-cover fields.  A prior legacy cover-only edit may legitimately
    make its saved cover URL stale, so the bridge accepts the fresh planning
    snapshot only when removing Creator/public ``cover`` makes it byte-for-
    byte equal to the completed snapshot.  The underlying predecessor replay
    still validates the old plan, journal, receipt, topology, and hashes.
    """

    completed_path = completed_path.resolve()
    completed = _load_json(completed_path, "same-BV predecessor completion")
    completed_snapshot = completed.get("live_snapshot")
    if not isinstance(completed_snapshot, Mapping):
        raise CoverPlanInvalid(
            "same-BV predecessor completion has no live snapshot"
        )
    if _without_cover(snapshot) != _without_cover(completed_snapshot):
        raise CoverPlanInvalid(
            "fresh cover planning state differs from predecessor outside cover"
        )
    proof_snapshot = copy.deepcopy(snapshot_with_current_cover_identity(snapshot))
    canonical_completed = snapshot_with_current_cover_identity(
        completed_snapshot
    )
    for surface in ("creator", "public"):
        proof_metadata = (
            (proof_snapshot.get(surface) or {}).get("metadata") or {}
        )
        completed_metadata = (
            (canonical_completed.get(surface) or {}).get("metadata") or {}
        )
        if isinstance(proof_metadata, dict):
            proof_metadata["cover"] = completed_metadata.get("cover")
    try:
        predecessor = repair._predecessor_completion_attestation(
            completed_path,
            authority=authority,
            bvid=bvid,
            snapshot=proof_snapshot,
        )
    except repair.PlanInvalid as exc:
        raise CoverPlanInvalid(
            f"same-BV predecessor completion rejected: {exc}"
        ) from exc
    predecessor["cover_only_bridge"] = {
        "schema_version": "same-bv-cover-predecessor-bridge.v1",
        "completed_creator_cover": (
            ((canonical_completed.get("creator") or {}).get("metadata") or {}).get(
                "cover"
            )
        ),
        "planning_creator_cover": (
            ((proof_snapshot.get("creator") or {}).get("metadata") or {}).get(
                "cover"
            )
        ),
        "rule": "ALL_FIELDS_EXCEPT_CREATOR_PUBLIC_COVER_EXACT",
    }
    # ``proof_snapshot`` currently carries the completed covers.  Bind the
    # actual planning cover separately rather than accidentally attesting the
    # substituted proof value.
    predecessor["cover_only_bridge"]["planning_creator_cover"] = (
        ((snapshot_with_current_cover_identity(snapshot).get("creator") or {}).get(
            "metadata"
        ) or {}).get("cover")
    )
    return predecessor


def create_plan(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    bvid: str,
    snapshot: Mapping[str, Any],
    predecessor_completed_path: Path | None = None,
) -> dict[str, Any]:
    """Freeze one exact cover-only plan from a fresh four-surface snapshot."""

    manifest_path = manifest_path.resolve()
    if _BVID_RE.fullmatch(bvid) is None:
        raise CoverPlanInvalid(f"invalid existing BVID: {bvid!r}")
    try:
        authority = repair.validate_repair_publication_target(manifest, bvid)
    except repair.PlanInvalid as exc:
        raise CoverPlanInvalid(str(exc)) from exc
    season = manifest.get("season") or {}
    section_id = season.get("section_id")
    season_id = season.get("season_id")
    if not isinstance(section_id, int) or not isinstance(season_id, int):
        raise CoverPlanInvalid("cover-only repair requires exact season binding")
    target_metadata = _manifest_noncover_metadata(manifest)
    predecessor_completion = None
    expected_cid = authority.get("cid")
    if predecessor_completed_path is not None:
        predecessor_completion = _cover_predecessor_attestation(
            predecessor_completed_path,
            authority=authority,
            bvid=bvid,
            snapshot=snapshot,
        )
        expected_cid = predecessor_completion.get("new_cid")
    problems = _cover_only_scope_binding_problems(
        manifest,
        predecessor_completed_path=predecessor_completed_path,
    )
    if not isinstance(expected_cid, int) or isinstance(expected_cid, bool):
        raise CoverPlanInvalid("cover-only repair predecessor CID is invalid")
    problems.extend(
        _snapshot_plan_problems(
            snapshot,
            bvid=bvid,
            authority=authority,
            target_metadata=target_metadata,
            section_id=section_id,
            expected_cid=expected_cid,
        )
    )
    cover = manifest.get("cover") or {}
    cover_path = Path(str(cover.get("path") or ""))
    if not cover_path.is_absolute() or not cover_path.is_file():
        problems.append("manifest final cover is missing or not absolute")
    else:
        if repair.sha256_file(cover_path) != cover.get("sha256"):
            problems.append("manifest final cover hash drift")
        if cover_path.stat().st_size != cover.get("bytes"):
            problems.append("manifest final cover byte-size drift")
    if problems:
        raise CoverPlanInvalid("; ".join(dict.fromkeys(problems)))
    before = snapshot_with_current_cover_identity(snapshot)
    old_cover = ((before.get("creator") or {}).get("metadata") or {}).get(
        "cover"
    )
    plan = {
        "schema_version": PLAN_SCHEMA,
        "plan_id": uuid.uuid4().hex,
        "created_at": _now(),
        "bvid": bvid,
        "manifest": {
            "path": str(manifest_path),
            "sha256": repair.sha256_file(manifest_path),
        },
        "authorization": dict(manifest.get("authorization") or {}),
        "recovery_publication_authority": dict(authority),
        "package_attestation": copy.deepcopy(
            manifest.get("package_attestation") or {}
        ),
        "replacement_cover": {
            "path": str(cover_path.resolve()),
            "sha256": cover.get("sha256"),
            "bytes": cover.get("bytes"),
        },
        "old_cover_url": old_cover,
        "unchanged_cid": expected_cid,
        "unchanged_metadata": target_metadata,
        "season": {
            "season_id": season_id,
            "section_id": section_id,
            "season_title": season.get("season_title"),
        },
        "before": before,
    }
    if predecessor_completion is not None:
        plan["predecessor_completion"] = predecessor_completion
    validate_plan(plan, manifest=manifest)
    return plan


def validate_plan(
    plan: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    plan_path: Path | None = None,
) -> None:
    problems: list[str] = []
    if plan.get("schema_version") != PLAN_SCHEMA:
        problems.append("cover repair plan schema mismatch")
    if not isinstance(plan.get("plan_id"), str) or not plan.get("plan_id"):
        problems.append("cover repair plan_id missing")
    bvid = str(plan.get("bvid") or "")
    if _BVID_RE.fullmatch(bvid) is None:
        problems.append("cover repair BVID invalid")
    manifest_entry = plan.get("manifest") or {}
    manifest_file = Path(str(manifest_entry.get("path") or ""))
    if not manifest_file.is_absolute() or not manifest_file.is_file():
        problems.append("bound cover repair manifest missing or unsafe")
    elif repair.sha256_file(manifest_file) != manifest_entry.get("sha256"):
        problems.append("bound cover repair manifest hash drift")
    else:
        try:
            if _load_json(manifest_file, "bound cover repair manifest") != manifest:
                problems.append("runtime manifest differs from bound manifest")
        except CoverPlanInvalid as exc:
            problems.append(str(exc))
    try:
        authority = repair.validate_repair_publication_target(manifest, bvid)
    except repair.PlanInvalid as exc:
        problems.append(str(exc))
        authority = {}
    if plan.get("recovery_publication_authority") != authority:
        problems.append("cover repair publication authority drifted")
    if plan.get("package_attestation") != manifest.get("package_attestation"):
        problems.append("cover repair package attestation drifted")
    if plan.get("authorization") != manifest.get("authorization"):
        problems.append("cover repair authorization drifted")
    if plan.get("unchanged_metadata") != _manifest_noncover_metadata(manifest):
        problems.append("cover repair non-cover metadata drifted")
    replacement = plan.get("replacement_cover") or {}
    manifest_cover = manifest.get("cover") or {}
    cover_path = Path(str(replacement.get("path") or ""))
    if not cover_path.is_absolute() or not cover_path.is_file():
        problems.append("cover repair replacement is missing or unsafe")
    else:
        if repair.sha256_file(cover_path) != replacement.get("sha256"):
            problems.append("cover repair replacement hash drift")
        if cover_path.stat().st_size != replacement.get("bytes"):
            problems.append("cover repair replacement byte-size drift")
    if any(
        replacement.get(key) != manifest_cover.get(key)
        for key in ("sha256", "bytes")
    ) or str(cover_path) != str(Path(str(manifest_cover.get("path") or ""))):
        problems.append("cover repair replacement no longer matches manifest")
    season = plan.get("season") or {}
    if not isinstance(season.get("season_id"), int) or not isinstance(
        season.get("section_id"), int
    ):
        problems.append("cover repair season binding invalid")
    before = plan.get("before") or {}
    if plan.get("old_cover_url") != (
        ((before.get("creator") or {}).get("metadata") or {}).get("cover")
    ):
        problems.append("cover repair old cover binding drifted")
    predecessor = plan.get("predecessor_completion")
    expected_cid = authority.get("cid")
    if predecessor is not None:
        if not isinstance(predecessor, Mapping):
            problems.append("cover repair predecessor completion is invalid")
        else:
            completed_entry = predecessor.get("completed") or {}
            try:
                replayed_predecessor = _cover_predecessor_attestation(
                    Path(str(completed_entry.get("path") or "")),
                    authority=authority,
                    bvid=bvid,
                    snapshot=before,
                )
            except CoverPlanInvalid as exc:
                problems.append(str(exc))
            else:
                if predecessor != replayed_predecessor:
                    problems.append("cover repair predecessor binding drifted")
                expected_cid = replayed_predecessor.get("new_cid")
    scope_predecessor_path = None
    if isinstance(predecessor, Mapping):
        completed_entry = predecessor.get("completed")
        if isinstance(completed_entry, Mapping):
            scope_predecessor_path = Path(
                str(completed_entry.get("path") or "")
            )
    problems.extend(
        _cover_only_scope_binding_problems(
            manifest,
            predecessor_completed_path=scope_predecessor_path,
        )
    )
    if plan.get("unchanged_cid") != expected_cid:
        problems.append("cover repair unchanged CID differs from proven predecessor")
    if authority and isinstance(season.get("section_id"), int):
        problems.extend(
            _snapshot_plan_problems(
                before,
                bvid=bvid,
                authority=authority,
                target_metadata=plan.get("unchanged_metadata") or {},
                section_id=int(season["section_id"]),
                expected_cid=(
                    int(expected_cid)
                    if isinstance(expected_cid, int)
                    and not isinstance(expected_cid, bool)
                    else -1
                ),
            )
        )
    if plan_path is not None and not plan_path.is_file():
        problems.append("cover repair plan file missing")
    if problems:
        raise CoverPlanInvalid("; ".join(dict.fromkeys(problems)))


def write_plan(path: Path, plan: Mapping[str, Any]) -> None:
    _create_json(path, plan)


def load_plan(path: Path) -> dict[str, Any]:
    return _load_json(path, "cover repair plan")


def _read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise CoverJournalCorrupt("cover repair journal has a partial final row")
    rows: list[dict[str, Any]] = []
    previous_hash: str | None = None
    last_state: dict[str, str] = {}
    last_plan_for_bvid: dict[str, str] = {}
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise CoverJournalCorrupt(
                f"cover repair journal row {line_no} is invalid JSON"
            ) from exc
        if not isinstance(row, dict) or row.get("schema_version") != JOURNAL_SCHEMA:
            raise CoverJournalCorrupt(
                f"cover repair journal row {line_no} schema mismatch"
            )
        claimed = row.get("row_sha256")
        unhashed = dict(row)
        unhashed.pop("row_sha256", None)
        if claimed != _sha256_json(unhashed):
            raise CoverJournalCorrupt(
                f"cover repair journal row {line_no} hash mismatch"
            )
        if row.get("seq") != line_no or row.get("prev_row_sha256") != previous_hash:
            raise CoverJournalCorrupt(
                f"cover repair journal row {line_no} chain mismatch"
            )
        plan_id = str(row.get("plan_id") or "")
        state = str(row.get("state") or "")
        bvid = str(row.get("bvid") or "")
        if state not in JOURNAL_STATES or not plan_id or not bvid:
            raise CoverJournalCorrupt(
                f"cover repair journal row {line_no} identity/state invalid"
            )
        previous_state = last_state.get(plan_id)
        if previous_state is None:
            if state != "PLANNED":
                raise CoverJournalCorrupt(
                    f"cover repair plan {plan_id} does not start at PLANNED"
                )
            prior_owner = last_plan_for_bvid.get(bvid)
            if prior_owner and last_state.get(prior_owner) not in TERMINAL_STATES:
                raise CoverJournalCorrupt(
                    f"BVID {bvid} is already owned by active plan {prior_owner}"
                )
            last_plan_for_bvid[bvid] = plan_id
        elif state not in _TRANSITIONS[previous_state]:
            raise CoverJournalCorrupt(
                f"illegal cover repair transition {previous_state}->{state}"
            )
        last_state[plan_id] = state
        previous_hash = claimed
        rows.append(row)
    return rows


def _plan_rows(
    journal: Path, plan_path: Path, plan: Mapping[str, Any]
) -> list[dict[str, Any]]:
    expected_path = str(plan_path.resolve())
    expected_sha = repair.sha256_file(plan_path)
    rows = []
    for row in _read_journal(journal):
        if row.get("plan_id") != plan.get("plan_id"):
            continue
        if (
            row.get("bvid") != plan.get("bvid")
            or row.get("plan_path") != expected_path
            or row.get("plan_sha256") != expected_sha
        ):
            raise CoverJournalCorrupt("cover repair journal plan binding drift")
        rows.append(row)
    return rows


def _append_journal(
    journal: Path,
    *,
    plan_path: Path,
    plan: Mapping[str, Any],
    state: str,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    journal = journal.resolve()
    journal.parent.mkdir(parents=True, exist_ok=True)
    rows = _read_journal(journal)
    own_rows = _plan_rows(journal, plan_path, plan) if rows else []
    if own_rows:
        previous_state = str(own_rows[-1]["state"])
        if state not in _TRANSITIONS[previous_state]:
            raise CoverJournalCorrupt(
                f"illegal cover repair transition {previous_state}->{state}"
            )
    elif state != "PLANNED":
        raise CoverJournalCorrupt("cover repair journal must start at PLANNED")
    row = {
        "schema_version": JOURNAL_SCHEMA,
        "seq": len(rows) + 1,
        "at": _now(),
        "prev_row_sha256": rows[-1]["row_sha256"] if rows else None,
        "plan_id": plan["plan_id"],
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": repair.sha256_file(plan_path),
        "bvid": plan["bvid"],
        "state": state,
        "details": dict(details),
    }
    row["row_sha256"] = _sha256_json(row)
    data = _canonical_json(row) + b"\n"
    fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while appending cover repair journal")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_parent(journal.parent)
    return row


def initialise_journal(
    journal: Path, plan_path: Path, plan: Mapping[str, Any]
) -> None:
    if _plan_rows(journal, plan_path, plan):
        raise CoverJournalCorrupt("cover repair plan is already journaled")
    _append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="PLANNED",
        details={"remote_mutation": False},
    )


def status(
    *,
    plan_path: Path,
    journal: Path,
    manifest: Mapping[str, Any],
) -> CoverRepairResult:
    plan = load_plan(plan_path)
    validate_plan(plan, manifest=manifest, plan_path=plan_path)
    rows = _plan_rows(journal, plan_path, plan)
    if not rows:
        return CoverRepairResult(
            "PLANNED", False, "plan has no journal row", {"next_action": "INITIALISE"}
        )
    row = rows[-1]
    next_action = {
        "PLANNED": "UPLOAD_REVIEWED_COVER",
        "COVER_UPLOAD_INTENT": "REUPLOAD_SAME_BYTES_IF_ARCHIVE_UNCHANGED",
        "COVER_UPLOADED": "EDIT_COVER_ONCE",
        "EDIT_INTENT": "POLL_ONLY_NEVER_REEDIT",
        "EDIT_AMBIGUOUS": "POLL_ONLY_NEVER_REEDIT",
        "PUBLIC_PENDING": "POLL_ONLY_NEVER_REEDIT",
        "VERIFIED": "FRESH_VERIFY",
        "BLOCKED_DRIFT": "HUMAN_RECONCILIATION_REQUIRED",
    }[str(row["state"])]
    return CoverRepairResult(
        str(row["state"]),
        False,
        f"cover repair state is {row['state']}",
        {"next_action": next_action, "last_event": row, "event_count": len(rows)},
    )


def _uploaded_cover_url(rows: list[Mapping[str, Any]]) -> str | None:
    for row in reversed(rows):
        value = (row.get("details") or {}).get("uploaded_cover_url")
        if isinstance(value, str) and value:
            return value
    return None


def _transition_projection(
    snapshot: Mapping[str, Any],
    plan: Mapping[str, Any],
    uploaded_cover_url: str,
) -> tuple[str, list[str]]:
    """Classify a fresh snapshot as before/target/pending/drift."""

    before = snapshot_with_current_cover_identity(plan.get("before") or {})
    current = snapshot_with_current_cover_identity(snapshot)
    if snapshots_equivalent(current, before):
        return "before", []
    creator = current.get("creator") or {}
    public = current.get("public") or {}
    section = current.get("section") or {}
    before_creator = before.get("creator") or {}
    before_public = before.get("public") or {}
    before_section = before.get("section") or {}
    new_cover = normalise_cover_url(uploaded_cover_url)
    old_creator_cover = ((before_creator.get("metadata") or {}).get("cover"))
    old_public_cover = ((before_public.get("metadata") or {}).get("cover"))
    current_creator_cover = ((creator.get("metadata") or {}).get("cover"))
    current_public_cover = ((public.get("metadata") or {}).get("cover"))
    problems: list[str] = []
    if creator.get("available") is not True:
        return "pending", ["Creator observation unavailable"]
    if _without_cover({"creator": creator}).get("creator") != _without_cover(
        {"creator": before_creator}
    ).get("creator"):
        problems.append("Creator changed outside cover")
    if current_creator_cover not in (old_creator_cover, new_cover):
        problems.append("Creator cover is neither frozen old nor uploaded new asset")
    section_pending = section.get("available") is not True
    if not section_pending and section != before_section:
        problems.append("exact section changed during cover-only repair")
    public_pending = public.get("available") is not True
    if not public_pending:
        if _without_cover({"public": public}).get("public") != _without_cover(
            {"public": before_public}
        ).get("public"):
            problems.append("public archive changed outside cover")
        if current_public_cover not in (old_public_cover, new_cover):
            problems.append("public cover is neither frozen old nor uploaded new asset")
    if problems:
        return "drift", problems
    if (
        not public_pending
        and not section_pending
        and current_creator_cover == new_cover
        and current_public_cover == new_cover
    ):
        return "target", []
    return "pending", []


def _result(row: Mapping[str, Any], message: str) -> CoverRepairResult:
    return CoverRepairResult(
        str(row["state"]), True, message, row.get("details") or {}
    )


def _block(
    *,
    journal: Path,
    plan_path: Path,
    plan: Mapping[str, Any],
    reason: str,
    snapshot: Mapping[str, Any] | None = None,
) -> CoverRepairResult:
    row = _append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="BLOCKED_DRIFT",
        details={
            "reason": reason,
            "live_snapshot": dict(snapshot or {}),
            "remote_mutation": False,
        },
    )
    return _result(row, reason)


def run(
    *,
    plan_path: Path,
    journal: Path,
    manifest: Mapping[str, Any],
    adapter: CoverAdapter,
    wait_seconds: float,
    poll_seconds: float,
) -> CoverRepairResult:
    """Resume the one-shot cover edit transaction."""

    plan = load_plan(plan_path)
    validate_plan(plan, manifest=manifest, plan_path=plan_path)
    rows = _plan_rows(journal, plan_path, plan)
    if not rows:
        raise CoverJournalCorrupt("cover repair plan has no initial journal row")
    state = str(rows[-1]["state"])
    if state in TERMINAL_STATES:
        return status(plan_path=plan_path, journal=journal, manifest=manifest)
    bvid = str(plan["bvid"])
    section_id = int((plan.get("season") or {})["section_id"])

    if state in {"PLANNED", "COVER_UPLOAD_INTENT"}:
        snapshot = adapter.observe(bvid, section_id)
        if not snapshots_equivalent(snapshot, plan.get("before") or {}):
            return _block(
                journal=journal,
                plan_path=plan_path,
                plan=plan,
                reason="live state drifted before reviewed cover upload",
                snapshot=snapshot,
            )
        _append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="COVER_UPLOAD_INTENT",
            details={
                "replacement_cover_sha256": plan["replacement_cover"]["sha256"],
                "retry_same_bytes": state == "COVER_UPLOAD_INTENT",
                "remote_mutation": "upload_cover_asset_only",
            },
        )
        try:
            uploaded_url = adapter.prepare_cover(
                Path(str(plan["replacement_cover"]["path"]))
            )
        except Exception as exc:
            return CoverRepairResult(
                "COVER_UPLOAD_INTENT",
                True,
                "cover asset upload outcome is retryable only while archive remains exact",
                {"error_type": type(exc).__name__, "remote_mutation": "ambiguous_asset_only"},
            )
        if not _known_cover_asset(uploaded_url):
            return _block(
                journal=journal,
                plan_path=plan_path,
                plan=plan,
                reason="cover upload returned an unrecognized asset identity",
            )
        row = _append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="COVER_UPLOADED",
            details={"uploaded_cover_url": uploaded_url, "remote_mutation": "asset_only"},
        )
        rows.append(row)
        state = "COVER_UPLOADED"

    uploaded_url = _uploaded_cover_url(_plan_rows(journal, plan_path, plan))
    if state == "COVER_UPLOADED":
        if not uploaded_url:
            raise CoverJournalCorrupt("COVER_UPLOADED has no uploaded cover URL")
        snapshot = adapter.observe(bvid, section_id)
        if not snapshots_equivalent(snapshot, plan.get("before") or {}):
            return _block(
                journal=journal,
                plan_path=plan_path,
                plan=plan,
                reason="live state drifted before one-shot cover edit",
                snapshot=snapshot,
            )
        _append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="EDIT_INTENT",
            details={
                "old_cover_url": plan["old_cover_url"],
                "uploaded_cover_url": uploaded_url,
                "unchanged_cid": plan["unchanged_cid"],
                "rule": "POLL_ONLY_NEVER_REEDIT",
                "remote_mutation": "cover_only_archive_edit_once",
            },
        )
        try:
            adapter.edit_cover_only(
                bvid,
                expected_creator=(plan.get("before") or {}).get("creator") or {},
                cover_url=uploaded_url,
            )
            outcome = "cover edit call returned"
        except Exception as exc:
            outcome = f"cover edit call raised {type(exc).__name__}"
        _append_journal(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="EDIT_AMBIGUOUS",
            details={
                "reason": outcome,
                "uploaded_cover_url": uploaded_url,
                "rule": "POLL_ONLY_NEVER_REEDIT",
                "remote_mutation": False,
            },
        )
        state = "EDIT_AMBIGUOUS"

    if not uploaded_url:
        raise CoverJournalCorrupt("post-edit cover repair has no uploaded cover URL")
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        snapshot = adapter.observe(bvid, section_id)
        projection, problems = _transition_projection(snapshot, plan, uploaded_url)
        if projection == "target":
            row = _append_journal(
                journal,
                plan_path=plan_path,
                plan=plan,
                state="VERIFIED",
                details={
                    "uploaded_cover_url": uploaded_url,
                    "unchanged_cid": plan["unchanged_cid"],
                    "live_snapshot": snapshot,
                    "remote_mutation": False,
                },
            )
            return _result(row, "cover-only repair reached exact target state")
        if projection == "drift":
            return _block(
                journal=journal,
                plan_path=plan_path,
                plan=plan,
                reason="; ".join(problems),
                snapshot=snapshot,
            )
        if projection == "pending" and state != "PUBLIC_PENDING":
            row = _append_journal(
                journal,
                plan_path=plan_path,
                plan=plan,
                state="PUBLIC_PENDING",
                details={
                    "uploaded_cover_url": uploaded_url,
                    "live_snapshot": snapshot,
                    "rule": "POLL_ONLY_NEVER_REEDIT",
                    "remote_mutation": False,
                },
            )
            state = "PUBLIC_PENDING"
        if time.monotonic() >= deadline:
            return CoverRepairResult(
                state,
                False,
                "cover edit submitted; fresh surfaces are still converging",
                {"uploaded_cover_url": uploaded_url, "remote_mutation": False},
            )
        time.sleep(max(0.05, poll_seconds))


def verify_live(
    *,
    plan_path: Path,
    journal: Path,
    manifest: Mapping[str, Any],
    adapter: CoverAdapter,
    out: Path,
) -> dict[str, Any]:
    """Freshly re-observe VERIFIED state and create one completion receipt."""

    plan = load_plan(plan_path)
    validate_plan(plan, manifest=manifest, plan_path=plan_path)
    rows = _plan_rows(journal, plan_path, plan)
    if not rows or rows[-1].get("state") != "VERIFIED":
        state = str(rows[-1].get("state")) if rows else "PLANNED"
        raise CoverRepairError(f"cover repair is {state}, not VERIFIED")
    terminal = rows[-1]
    details = terminal.get("details") or {}
    expected = details.get("live_snapshot")
    uploaded_url = details.get("uploaded_cover_url")
    if not isinstance(expected, Mapping) or not isinstance(uploaded_url, str):
        raise CoverJournalCorrupt("VERIFIED row lacks live snapshot/cover URL")
    fresh = adapter.observe(
        str(plan["bvid"]), int((plan.get("season") or {})["section_id"])
    )
    projection, problems = _transition_projection(fresh, plan, uploaded_url)
    if projection != "target" or not snapshots_equivalent(fresh, expected):
        detail = "; ".join(problems) if problems else "fresh snapshot drift"
        raise CoverRepairError(f"cover repair fresh verification failed: {detail}")
    completed = {
        "schema_version": COMPLETED_SCHEMA,
        "status": "VERIFIED_FRESH_LIVE",
        "rc": 0,
        "verified_at": _now(),
        "remote_mutation": False,
        "candidate_id": (
            plan.get("recovery_publication_authority") or {}
        ).get("candidate_id"),
        "bvid": plan["bvid"],
        "aid": (plan.get("recovery_publication_authority") or {}).get("aid"),
        "unchanged_cid": plan["unchanged_cid"],
        "old_cover_url": plan["old_cover_url"],
        "uploaded_cover_url": uploaded_url,
        "new_cover": dict(plan["replacement_cover"]),
        "plan": {
            "path": str(plan_path.resolve()),
            "sha256": repair.sha256_file(plan_path),
            "plan_id": plan["plan_id"],
        },
        "manifest": dict(plan["manifest"]),
        "verified_journal_row": {
            "journal_path": str(journal.resolve()),
            "seq": terminal.get("seq"),
            "at": terminal.get("at"),
            "row_sha256": terminal.get("row_sha256"),
        },
        "live_snapshot": fresh,
    }
    out = out.resolve()
    if out.exists() or out.is_symlink():
        existing = _load_json(out, "cover repair completed receipt")
        stable_existing = dict(existing)
        stable_completed = dict(completed)
        stable_existing.pop("verified_at", None)
        stable_completed.pop("verified_at", None)
        if stable_existing != stable_completed:
            raise CoverRepairError(
                "existing cover repair completion differs from fresh closure"
            )
        return existing
    _create_json(out, completed)
    return completed
