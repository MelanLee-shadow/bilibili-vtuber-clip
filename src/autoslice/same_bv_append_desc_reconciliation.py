"""Append-time description truncation: one narrow same-BV false-block reconciliation.

biliup-cli 0.2.4 ``append`` re-submits the archive through the Bilibili app
client, which truncates a description longer than 250 characters.  That is a
property of append, not drift.  This module owns the pure ``desc`` comparison
and the sole exception that lets ``repair-reconcile-blocked`` move such a false
``BLOCKED_DRIFT`` to ``TWO_P_READY``.  It is read-only against Bilibili, never
touches PLANNED's exact comparison or the VERIFIED criteria, and appends one
hash-bound audit row only after both the blocked snapshot and a fresh
observation prove the exact old P plus one new P.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

# biliup-cli 0.2.4 ``append`` has no ``--submit`` option and re-submits the
# whole archive through the Bilibili app client, whose description limit is 250
# characters.  An original description longer than this is therefore truncated
# to its first 250 characters by the append itself (observed BV1hYx66AEr3).
BILIUP_APP_APPEND_DESC_LIMIT = 250
APPEND_TOPOLOGY_BLOCK_REASON = (
    "append reconciliation found unknown P/CID/metadata/section topology"
)
APPEND_DESC_RECONCILIATION_SCHEMA = (
    "same-bv-append-desc-truncation-reconciliation.v1"
)


def desc_matches_before(current: object, before: object) -> bool:
    """Exact ``desc``, or the app-append truncation of an over-limit original."""

    if current == before:
        return True
    if not isinstance(current, str) or not isinstance(before, str) or not current:
        return False
    if len(before) <= BILIUP_APP_APPEND_DESC_LIMIT:
        return False
    truncated = before[:BILIUP_APP_APPEND_DESC_LIMIT]
    return current in (truncated, truncated.rstrip())


def metadata_matches_before(current: object, before: object, *, allow_app_truncated_desc: bool) -> bool:
    """Metadata equality; only ``desc`` may differ, and only by append truncation."""

    if not allow_app_truncated_desc:
        return current == before
    if not isinstance(current, Mapping) or not isinstance(before, Mapping):
        return False
    if set(current) != set(before):
        return False
    return all(
        desc_matches_before(current.get(key), value) if key == "desc" else current.get(key) == value
        for key, value in before.items()
    )


def is_append_desc_reconciliation_transition(
    previous_row: Mapping[str, Any] | None,
    row: Mapping[str, Any],
) -> bool:
    """Validate the exceptional BLOCKED_DRIFT -> TWO_P_READY provenance."""

    if (
        not isinstance(previous_row, Mapping)
        or previous_row.get("state") != "BLOCKED_DRIFT"
        or row.get("state") != "TWO_P_READY"
    ):
        return False
    previous_details = previous_row.get("details")
    details = row.get("details")
    if not isinstance(previous_details, Mapping) or not isinstance(details, Mapping):
        return False
    reconciliation = details.get("reconciliation")
    if not isinstance(reconciliation, Mapping):
        return False
    return bool(
        previous_details.get("reason") == APPEND_TOPOLOGY_BLOCK_REASON
        and details.get("remote_mutation") is False
        and isinstance(details.get("new_video"), Mapping)
        and isinstance(details.get("live_snapshot"), Mapping)
        and reconciliation.get("schema_version") == APPEND_DESC_RECONCILIATION_SCHEMA
        and reconciliation.get("blocked_row_sha256") == previous_row.get("row_sha256")
        and reconciliation.get("blocked_seq") == previous_row.get("seq")
        and reconciliation.get("blocked_reason") == APPEND_TOPOLOGY_BLOCK_REASON
    )


def append_topology_check(
    snapshot: object,
    plan: Mapping[str, Any],
    *,
    expected_new: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str]:
    """Exact old P + one new P; only ``desc`` may carry the append truncation."""

    from src.autoslice import same_bv_repair as repair

    if not isinstance(snapshot, Mapping):
        return None, "snapshot is not an object"
    before_creator = (plan.get("before") or {}).get("creator") or {}
    problems = repair._basic_snapshot_problems(
        snapshot, bvid=str(plan["bvid"]), aid=before_creator.get("aid")
    )
    if problems:
        return None, "; ".join(problems)
    new_video = repair._two_p_new_video(
        snapshot, plan, expected_new=expected_new, allow_app_truncated_desc=True
    )
    if new_video is None:
        return None, "Creator is not the exact old P plus one new P with only desc truncation"
    if not repair._public_section_available(snapshot):
        return None, "public/section readback is unavailable"
    if not repair._before_public_section_exact(snapshot, plan, allow_app_truncated_desc=True):
        return None, "public/section differs from the planning snapshot beyond desc truncation"
    return new_video, ""


def reconcile_append_desc_truncation_block(
    *,
    plan_path: Path,
    journal: Path,
    manifest: Mapping[str, Any],
    adapter: Any,
    commit: bool,
) -> Any:
    """Move one append-time desc-truncation false block to TWO_P_READY.

    Read-only against Bilibili.  The blocked snapshot and a fresh observation
    must both be the exact old P plus the same single new P; ``desc`` is the
    only value allowed to differ, and only by the app-append truncation.  The
    swap then writes the full target description; VERIFIED is unchanged.
    """

    # Lazy import avoids a module cycle: same_bv_repair imports the pure
    # helpers above for every ordinary transaction.
    from src.autoslice import same_bv_repair as repair

    plan_path = plan_path.resolve()
    plan = repair.load_plan(plan_path)
    repair.validate_plan(plan, manifest=manifest, plan_path=plan_path)
    entries = repair.plan_entries(journal, plan_path, plan)
    if not entries or entries[-1].get("state") != "BLOCKED_DRIFT":
        state = str(entries[-1]["state"]) if entries else "PLANNED"
        return repair.RepairResult(
            state, False, "repair is not at the eligible BLOCKED_DRIFT row", {"remote_mutation": False}
        )
    blocked = entries[-1]
    if (
        (blocked.get("details") or {}).get("reason") != APPEND_TOPOLOGY_BLOCK_REASON
        or len(entries) < 2
        or entries[-2].get("state") not in {"APPEND_INTENT", "APPEND_AMBIGUOUS"}
    ):
        return repair.RepairResult(
            "BLOCKED_DRIFT",
            False,
            "blocked row is not the known post-append topology block",
            {"remote_mutation": False},
        )
    blocked_snapshot = (blocked.get("details") or {}).get("snapshot")
    blocked_new, problem = append_topology_check(blocked_snapshot, plan, expected_new=None)
    if blocked_new is None:
        return repair.RepairResult(
            "BLOCKED_DRIFT",
            False,
            f"blocked snapshot is not an exact old+new topology: {problem}",
            {"remote_mutation": False},
        )
    bvid = str(plan["bvid"])
    section_id = int((plan.get("season") or {})["section_id"])
    try:
        fresh_snapshot = adapter.observe(bvid, section_id)
    except repair.ObservationUnavailable as exc:
        return repair.RepairResult(
            "BLOCKED_DRIFT",
            False,
            f"fresh observation unavailable: {exc}",
            {"observation_unavailable": True, "remote_mutation": False},
        )
    fresh_new, problem = append_topology_check(fresh_snapshot, plan, expected_new=blocked_new)
    if fresh_new is None:
        return repair.RepairResult(
            "BLOCKED_DRIFT",
            False,
            f"fresh observation is not the same exact old+new topology: {problem}",
            {"remote_mutation": False},
        )
    before_desc = ((plan.get("before") or {}).get("creator") or {}).get("metadata", {}).get("desc")
    canonical_snapshot = repair._snapshot_with_current_cover_identity(fresh_snapshot)
    details = {
        "new_video": dict(fresh_new),
        "live_snapshot": canonical_snapshot,
        "reconciliation": {
            "schema_version": APPEND_DESC_RECONCILIATION_SCHEMA,
            "blocked_seq": blocked["seq"],
            "blocked_row_sha256": blocked["row_sha256"],
            "blocked_reason": APPEND_TOPOLOGY_BLOCK_REASON,
            "desc_limit": BILIUP_APP_APPEND_DESC_LIMIT,
            "before_desc_len": len(before_desc) if isinstance(before_desc, str) else None,
            "blocked_creator_desc_len": len(
                str(((blocked_snapshot.get("creator") or {}).get("metadata") or {}).get("desc") or "")
            ),
            "blocked_public_desc_len": len(
                str(((blocked_snapshot.get("public") or {}).get("metadata") or {}).get("desc") or "")
            ),
            "fresh_creator_desc_len": len(
                str((canonical_snapshot["creator"].get("metadata") or {}).get("desc") or "")
            ),
            "fresh_public_desc_len": len(
                str((canonical_snapshot["public"].get("metadata") or {}).get("desc") or "")
            ),
        },
        "remote_mutation": False,
    }
    if not commit:
        return repair.RepairResult(
            "TWO_P_READY",
            False,
            "dry-run proved safe reconciliation to TWO_P_READY; run repair-run to continue",
            {**details, "would_append_journal": True},
        )
    repair.append_journal(journal, plan_path=plan_path, plan=plan, state="TWO_P_READY", details=details)
    row = repair.plan_entries(journal, plan_path, plan)[-1]
    return repair._row_result(
        row,
        "reconciled append desc-truncation false block to TWO_P_READY; run repair-run to continue",
    )
