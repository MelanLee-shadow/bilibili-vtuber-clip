"""Execution for a crash-safe existing-BV title-and-cover-only revision."""

from __future__ import annotations

import copy
import datetime as dt
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from src.autoslice.same_bv_cover_reconciliation import (
    normalise_cover_url,
    snapshots_equivalent,
)
from src.autoslice import same_bv_title_cover_journal as journal_binding
from src.autoslice import same_bv_title_cover_plan as plan_binding


COMPLETED_SCHEMA = "same-bv-title-cover-repair-completed.v1"
TitleCoverRepairError = plan_binding.TitleCoverRepairError
TitleCoverPlanInvalid = plan_binding.TitleCoverPlanInvalid
TitleCoverJournalCorrupt = journal_binding.TitleCoverJournalCorrupt
create_plan = plan_binding.create_plan
validate_plan = plan_binding.validate_plan
write_plan = plan_binding.write_plan
load_plan = plan_binding.load_plan


@dataclass(frozen=True)
class TitleCoverRepairResult:
    state: str
    changed: bool
    message: str
    details: dict[str, Any]


class TitleCoverAdapter(Protocol):
    def observe(self, bvid: str, section_id: int) -> dict[str, Any]: ...
    def prepare_cover(self, cover_path: Path) -> str: ...
    def edit_title_cover(
        self,
        bvid: str,
        *,
        expected_creator: Mapping[str, Any],
        target_title: str,
        cover_url: str,
    ) -> Mapping[str, Any]: ...
    def sync_section_title(
        self,
        bvid: str,
        section_id: int,
        *,
        expected_current_title: str,
        target_title: str,
    ) -> Mapping[str, Any]: ...


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def initialise_journal(
    journal: Path, plan_path: Path, plan: Mapping[str, Any]
) -> None:
    journal_binding.initialise_journal(
        journal, plan_path, plan, now=_now()
    )


def _append(
    journal: Path,
    *,
    plan_path: Path,
    plan: Mapping[str, Any],
    state: str,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    return journal_binding.append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state=state,
        details=details,
        now=_now(),
    )


def _known_cover_asset(value: object) -> bool:
    canonical = normalise_cover_url(value)
    return isinstance(canonical, str) and canonical.startswith(
        "//bilibili-cover-asset/bfs/archive/"
    )


def _target_snapshot(
    plan: Mapping[str, Any], cover_url: str
) -> dict[str, Any]:
    target = plan_binding.canonical_snapshot(plan.get("before") or {})
    title = plan["target_title"]
    canonical_cover = normalise_cover_url(cover_url)
    creator = target["creator"]
    creator["metadata"]["title"] = title
    creator["metadata"]["cover"] = canonical_cover
    creator["videos"] = [
        {**row, "title": title} for row in creator.get("videos") or []
    ]
    public = target["public"]
    public["metadata"]["title"] = title
    public["metadata"]["cover"] = canonical_cover
    section = target["section"]
    section["matches"] = [
        {**row, "title": title} for row in section.get("matches") or []
    ]
    return target


def _creator_kind(
    current: Mapping[str, Any],
    before: Mapping[str, Any],
    target: Mapping[str, Any],
) -> str:
    current_creator = copy.deepcopy(current.get("creator") or {})
    before_creator = copy.deepcopy(before.get("creator") or {})
    target_creator = copy.deepcopy(target.get("creator") or {})
    state = current_creator.pop("state", None)
    current_creator.pop("state_desc", None)
    for value in (before_creator, target_creator):
        value.pop("state", None)
        value.pop("state_desc", None)
    if state == 0 and current_creator == before_creator:
        return "old"
    if state == 0 and current_creator == target_creator:
        return "target"
    if state == -6 and current_creator == target_creator:
        return "target_pending"
    return "drift"


def transition_projection(
    snapshot: Mapping[str, Any],
    plan: Mapping[str, Any],
    uploaded_cover_url: str,
) -> tuple[str, list[str]]:
    current = plan_binding.canonical_snapshot(snapshot)
    before = plan_binding.canonical_snapshot(plan.get("before") or {})
    target = _target_snapshot(plan, uploaded_cover_url)
    creator_kind = _creator_kind(current, before, target)
    public_kind = (
        "target"
        if current.get("public") == target.get("public")
        else "old"
        if current.get("public") == before.get("public")
        else "drift"
    )
    section_kind = (
        "target"
        if current.get("section") == target.get("section")
        else "old"
        if current.get("section") == before.get("section")
        else "drift"
    )
    problems = []
    if creator_kind == "drift":
        problems.append("Creator changed outside title/cover")
    if public_kind == "drift":
        problems.append("public surface changed outside title/cover")
    if section_kind == "drift":
        problems.append("exact section changed outside title")
    if problems:
        return "drift", problems
    if (
        creator_kind == "target"
        and public_kind == "target"
        and section_kind == "target"
    ):
        return "target", []
    if (
        creator_kind == "target"
        and public_kind == "target"
        and section_kind == "old"
    ):
        return "section_pending", []
    return "pending", []


def _block(
    *,
    journal: Path,
    plan_path: Path,
    plan: Mapping[str, Any],
    reason: str,
    snapshot: Mapping[str, Any] | None = None,
) -> TitleCoverRepairResult:
    details: dict[str, Any] = {"reason": reason, "remote_mutation": False}
    if snapshot is not None:
        details["live_snapshot"] = plan_binding.canonical_snapshot(snapshot)
    row = _append(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="BLOCKED_DRIFT",
        details=details,
    )
    return TitleCoverRepairResult(
        "BLOCKED_DRIFT", False, reason, dict(row["details"])
    )


def status(*, plan_path: Path, journal: Path) -> TitleCoverRepairResult:
    plan = load_plan(plan_path)
    validate_plan(plan, plan_path=plan_path)
    rows = journal_binding.plan_rows(journal, plan_path, plan)
    if not rows:
        raise TitleCoverJournalCorrupt("plan has no journal row")
    row = rows[-1]
    return TitleCoverRepairResult(
        str(row["state"]),
        False,
        "local title-cover transaction state",
        dict(row.get("details") or {}),
    )


def _upload_cover(
    *,
    state: str,
    plan: Mapping[str, Any],
    plan_path: Path,
    journal: Path,
    adapter: TitleCoverAdapter,
) -> tuple[str | None, TitleCoverRepairResult | None]:
    bvid = str(plan["bvid"])
    section_id = int((plan.get("season") or {})["section_id"])
    snapshot = adapter.observe(bvid, section_id)
    if not snapshots_equivalent(
        plan_binding.canonical_snapshot(snapshot), plan.get("before") or {}
    ):
        return None, _block(
            journal=journal,
            plan_path=plan_path,
            plan=plan,
            reason="live state drifted before cover upload",
            snapshot=snapshot,
        )
    if state == "PLANNED":
        _append(
            journal,
            plan_path=plan_path,
            plan=plan,
            state="COVER_UPLOAD_INTENT",
            details={
                "replacement_cover_sha256": plan["replacement_cover"]["sha256"],
                "remote_mutation": "upload_cover_asset_only",
            },
        )
    try:
        uploaded_url = adapter.prepare_cover(
            Path(str(plan["replacement_cover"]["path"]))
        )
    except Exception as exc:
        result = TitleCoverRepairResult(
            "COVER_UPLOAD_INTENT",
            True,
            "cover asset upload outcome is retryable while live state remains exact",
            {"error_type": type(exc).__name__},
        )
        return None, result
    if not _known_cover_asset(uploaded_url):
        return None, _block(
            journal=journal,
            plan_path=plan_path,
            plan=plan,
            reason="cover upload returned an unrecognized asset identity",
        )
    _append(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="COVER_UPLOADED",
        details={"uploaded_cover_url": uploaded_url, "remote_mutation": "asset_only"},
    )
    return uploaded_url, None


def _edit_archive_once(
    *,
    plan: Mapping[str, Any],
    plan_path: Path,
    journal: Path,
    adapter: TitleCoverAdapter,
    uploaded_url: str,
) -> TitleCoverRepairResult | None:
    bvid = str(plan["bvid"])
    section_id = int((plan.get("season") or {})["section_id"])
    snapshot = adapter.observe(bvid, section_id)
    if not snapshots_equivalent(
        plan_binding.canonical_snapshot(snapshot), plan.get("before") or {}
    ):
        return _block(
            journal=journal,
            plan_path=plan_path,
            plan=plan,
            reason="live state drifted before one-shot title-cover edit",
            snapshot=snapshot,
        )
    _append(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="EDIT_INTENT",
        details={
            "old_title": plan["old_title"],
            "target_title": plan["target_title"],
            "old_cover_url": plan["old_cover_url"],
            "uploaded_cover_url": uploaded_url,
            "unchanged_cid": plan["unchanged_cid"],
            "rule": "POLL_ONLY_NEVER_REEDIT",
            "remote_mutation": "title_cover_archive_edit_once",
        },
    )
    try:
        adapter.edit_title_cover(
            bvid,
            expected_creator=(plan.get("before") or {}).get("creator") or {},
            target_title=str(plan["target_title"]),
            cover_url=uploaded_url,
        )
        outcome = "title-cover edit call returned"
    except Exception as exc:
        outcome = f"title-cover edit call raised {type(exc).__name__}"
    _append(
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
    return None


def _sync_section_once(
    *,
    plan: Mapping[str, Any],
    plan_path: Path,
    journal: Path,
    adapter: TitleCoverAdapter,
) -> None:
    _append(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="SECTION_SYNC_INTENT",
        details={
            "expected_current_title": plan["old_title"],
            "target_title": plan["target_title"],
            "rule": "POLL_ONLY_NEVER_RESYNC",
            "remote_mutation": "section_title_edit_once",
        },
    )
    try:
        adapter.sync_section_title(
            str(plan["bvid"]),
            int((plan.get("season") or {})["section_id"]),
            expected_current_title=str(plan["old_title"]),
            target_title=str(plan["target_title"]),
        )
        outcome = "section title edit call returned"
    except Exception as exc:
        outcome = f"section title edit call raised {type(exc).__name__}"
    _append(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="SECTION_SYNC_AMBIGUOUS",
        details={
            "reason": outcome,
            "rule": "POLL_ONLY_NEVER_RESYNC",
            "remote_mutation": False,
        },
    )


def run(
    *,
    plan_path: Path,
    journal: Path,
    adapter: TitleCoverAdapter,
    wait_seconds: float,
    poll_seconds: float,
) -> TitleCoverRepairResult:
    plan = load_plan(plan_path)
    validate_plan(plan, plan_path=plan_path)
    rows = journal_binding.plan_rows(journal, plan_path, plan)
    if not rows:
        raise TitleCoverJournalCorrupt("plan has no initial journal row")
    state = str(rows[-1]["state"])
    if state in journal_binding.TERMINAL_STATES:
        return status(plan_path=plan_path, journal=journal)
    uploaded_url = journal_binding.uploaded_cover_url(rows)
    if state in {"PLANNED", "COVER_UPLOAD_INTENT"}:
        uploaded_url, result = _upload_cover(
            state=state,
            plan=plan,
            plan_path=plan_path,
            journal=journal,
            adapter=adapter,
        )
        if result is not None:
            return result
        state = "COVER_UPLOADED"
    if state == "COVER_UPLOADED":
        assert uploaded_url is not None
        result = _edit_archive_once(
            plan=plan,
            plan_path=plan_path,
            journal=journal,
            adapter=adapter,
            uploaded_url=uploaded_url,
        )
        if result is not None:
            return result
    if not uploaded_url:
        raise TitleCoverJournalCorrupt("post-upload transaction has no cover URL")
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        snapshot = adapter.observe(
            str(plan["bvid"]), int((plan.get("season") or {})["section_id"])
        )
        projection, problems = transition_projection(snapshot, plan, uploaded_url)
        if projection == "target":
            row = _append(
                journal,
                plan_path=plan_path,
                plan=plan,
                state="VERIFIED",
                details={
                    "uploaded_cover_url": uploaded_url,
                    "unchanged_cid": plan["unchanged_cid"],
                    "live_snapshot": plan_binding.canonical_snapshot(snapshot),
                    "remote_mutation": False,
                },
            )
            return TitleCoverRepairResult(
                "VERIFIED",
                True,
                "same-BV title-cover revision reached exact target state",
                dict(row["details"]),
            )
        if projection == "drift":
            return _block(
                journal=journal,
                plan_path=plan_path,
                plan=plan,
                reason="; ".join(problems),
                snapshot=snapshot,
            )
        rows = journal_binding.plan_rows(journal, plan_path, plan)
        state = str(rows[-1]["state"])
        if projection == "section_pending" and state not in {
            "SECTION_SYNC_INTENT",
            "SECTION_SYNC_AMBIGUOUS",
        }:
            _sync_section_once(
                plan=plan,
                plan_path=plan_path,
                journal=journal,
                adapter=adapter,
            )
            continue
        if state not in {
            "PUBLIC_PENDING",
            "SECTION_SYNC_INTENT",
            "SECTION_SYNC_AMBIGUOUS",
        }:
            _append(
                journal,
                plan_path=plan_path,
                plan=plan,
                state="PUBLIC_PENDING",
                details={
                    "live_snapshot": plan_binding.canonical_snapshot(snapshot),
                    "rule": "POLL_ONLY_NEVER_REEDIT",
                    "remote_mutation": False,
                },
            )
            state = "PUBLIC_PENDING"
        if time.monotonic() >= deadline:
            return TitleCoverRepairResult(
                state,
                False,
                "title-cover edit submitted; fresh surfaces are still converging",
                {"uploaded_cover_url": uploaded_url, "remote_mutation": False},
            )
        time.sleep(max(0.05, poll_seconds))


def verify_live(
    *,
    plan_path: Path,
    journal: Path,
    adapter: TitleCoverAdapter,
    out: Path,
) -> dict[str, Any]:
    from src.autoslice.same_bv_title_cover_verification import verify_live as verify

    return verify(
        plan_path=plan_path, journal=journal, adapter=adapter, out=out
    )
