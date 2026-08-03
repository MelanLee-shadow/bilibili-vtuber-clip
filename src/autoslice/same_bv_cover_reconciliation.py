"""Cover identity and one narrow same-BV false-block reconciliation.

Normal repair execution keeps ``BLOCKED_DRIFT`` terminal.  This module owns
the sole exception: an older normalizer compared Bilibili CDN hosts instead of
the immutable ``/bfs/archive/<hash>`` asset path.  Reconciliation is read-only
against Bilibili and appends one hash-bound correction row only after both the
original blocked snapshot and a fresh observation prove the exact target.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any, Mapping


BILIBILI_ARCHIVE_COVER_RE = re.compile(
    r"^/bfs/archive/[0-9A-Fa-f]{16,}(?:\.[A-Za-z0-9]+)?$"
)
COVER_ALIAS_RECONCILIATION_SCHEMA = (
    "same-bv-cover-alias-reconciliation.v1"
)
COVER_ALIAS_FALSE_BLOCK_REASON = (
    "post-swap observation is neither exact two-P nor exact single-new"
)


def scheme_independent_cover_url(value: object) -> object:
    """Remove only the scheme while preserving the exact host and path."""

    if not isinstance(value, str) or not value:
        return value
    candidate = value if not value.startswith("//") else "https:" + value
    parsed = urllib.parse.urlsplit(candidate)
    if not parsed.netloc:
        return value
    return f"//{parsed.netloc.lower()}{parsed.path}"


def normalise_cover_url(value: object) -> object:
    """Compare a known Bilibili cover asset independently of its CDN alias."""

    scheme_independent = scheme_independent_cover_url(value)
    if not isinstance(scheme_independent, str):
        return scheme_independent
    candidate = value if not str(value).startswith("//") else "https:" + str(value)
    parsed = urllib.parse.urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    known_bilibili_cdn = bool(
        host == "archive.biliimg.com"
        or host.endswith(".biliimg.com")
        or host.endswith(".hdslb.com")
    )
    if known_bilibili_cdn and BILIBILI_ARCHIVE_COVER_RE.fullmatch(parsed.path):
        return f"//bilibili-cover-asset{parsed.path.lower()}"
    return scheme_independent


def snapshot_with_current_cover_identity(
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-canonicalise stored snapshots made by older normalizer versions."""

    result = dict(snapshot)
    for surface_name in ("creator", "public"):
        raw_surface = snapshot.get(surface_name)
        if not isinstance(raw_surface, Mapping):
            continue
        surface = dict(raw_surface)
        raw_metadata = raw_surface.get("metadata")
        if isinstance(raw_metadata, Mapping):
            metadata = dict(raw_metadata)
            if "cover" in metadata:
                metadata["cover"] = normalise_cover_url(metadata.get("cover"))
            surface["metadata"] = metadata
        result[surface_name] = surface
    return result


def snapshots_equivalent(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> bool:
    """Strict equality with only known Bilibili CDN cover aliases folded."""

    return snapshot_with_current_cover_identity(
        left
    ) == snapshot_with_current_cover_identity(right)


def cover_diff_is_known_alias_only(left: object, right: object) -> bool:
    canonical = normalise_cover_url(left)
    return bool(
        isinstance(canonical, str)
        and canonical.startswith("//bilibili-cover-asset/bfs/archive/")
        and canonical == normalise_cover_url(right)
        and scheme_independent_cover_url(left)
        != scheme_independent_cover_url(right)
    )


def is_cover_alias_reconciliation_transition(
    previous_row: Mapping[str, Any] | None,
    row: Mapping[str, Any],
) -> bool:
    """Validate the exceptional journal transition's immutable provenance."""

    if (
        not isinstance(previous_row, Mapping)
        or previous_row.get("state") != "BLOCKED_DRIFT"
        or row.get("state") not in {"PUBLIC_PENDING", "VERIFIED"}
    ):
        return False
    previous_details = previous_row.get("details")
    details = row.get("details")
    if not isinstance(previous_details, Mapping) or not isinstance(
        details, Mapping
    ):
        return False
    reconciliation = details.get("reconciliation")
    if not isinstance(reconciliation, Mapping):
        return False
    return bool(
        previous_details.get("reason") == COVER_ALIAS_FALSE_BLOCK_REASON
        and details.get("remote_mutation") is False
        and isinstance(details.get("new_video"), Mapping)
        and isinstance(details.get("cover_url"), str)
        and isinstance(details.get("live_snapshot"), Mapping)
        and reconciliation.get("schema_version")
        == COVER_ALIAS_RECONCILIATION_SCHEMA
        and reconciliation.get("blocked_row_sha256")
        == previous_row.get("row_sha256")
        and reconciliation.get("blocked_seq") == previous_row.get("seq")
        and reconciliation.get("blocked_reason")
        == COVER_ALIAS_FALSE_BLOCK_REASON
    )


def reconcile_cover_alias_false_block(
    *,
    plan_path: Path,
    journal: Path,
    manifest: Mapping[str, Any],
    adapter: Any,
    commit: bool,
) -> Any:
    """Correct one proven CDN-alias false block without a remote mutation."""

    # Lazy import avoids a module cycle: same_bv_repair imports the pure
    # normalizers above for every ordinary transaction.
    from src.autoslice import same_bv_repair as repair

    plan_path = plan_path.resolve()
    plan = repair.load_plan(plan_path)
    repair.validate_plan(plan, manifest=manifest, plan_path=plan_path)
    entries = repair.plan_entries(journal, plan_path, plan)
    if not entries or entries[-1].get("state") != "BLOCKED_DRIFT":
        state = str(entries[-1]["state"]) if entries else "PLANNED"
        return repair.RepairResult(
            state,
            False,
            "repair is not at the eligible BLOCKED_DRIFT row",
            {"remote_mutation": False},
        )
    blocked = entries[-1]
    blocked_details = blocked.get("details") or {}
    if (
        blocked_details.get("reason") != COVER_ALIAS_FALSE_BLOCK_REASON
        or len(entries) < 2
        or entries[-2].get("state") != "SWAP_RETRYABLE"
    ):
        return repair.RepairResult(
            "BLOCKED_DRIFT",
            False,
            "blocked row is not the known post-swap cover-alias false positive",
            {"remote_mutation": False},
        )
    blocked_snapshot = blocked_details.get("snapshot")
    new_video = repair._latest_detail(entries[:-1], "new_video")
    cover_url = repair._latest_detail(entries[:-1], "cover_url")
    if (
        not isinstance(blocked_snapshot, Mapping)
        or not isinstance(new_video, Mapping)
        or not isinstance(cover_url, str)
    ):
        raise repair.JournalCorrupt(
            "eligible blocked row lacks snapshot, new_video, or cover_url evidence"
        )
    blocked_creator_cover = (
        ((blocked_snapshot.get("creator") or {}).get("metadata") or {}).get(
            "cover"
        )
    )
    if not cover_diff_is_known_alias_only(blocked_creator_cover, cover_url):
        return repair.RepairResult(
            "BLOCKED_DRIFT",
            False,
            "blocked Creator cover is not a known CDN alias of the frozen cover",
            {"remote_mutation": False},
        )
    if not repair._target_creator_exact(
        blocked_snapshot,
        plan,
        new_video=new_video,
        cover_url=cover_url,
    ):
        return repair.RepairResult(
            "BLOCKED_DRIFT",
            False,
            "blocked snapshot has non-cover Creator drift",
            {"remote_mutation": False},
        )
    blocked_projection, blocked_problems = repair._public_section_state(
        blocked_snapshot,
        plan,
        new_video=new_video,
        cover_url=cover_url,
    )
    if blocked_projection == "drift":
        return repair.RepairResult(
            "BLOCKED_DRIFT",
            False,
            "blocked snapshot has public or section drift",
            {
                "problems": blocked_problems,
                "remote_mutation": False,
            },
        )

    bvid = str(plan["bvid"])
    section_id = int((plan.get("season") or {})["section_id"])
    fresh_snapshot = adapter.observe(bvid, section_id)
    if not repair._target_creator_exact(
        fresh_snapshot,
        plan,
        new_video=new_video,
        cover_url=cover_url,
    ):
        return repair.RepairResult(
            "BLOCKED_DRIFT",
            False,
            "fresh Creator observation is not exact single-new target state",
            {"remote_mutation": False},
        )
    fresh_projection, fresh_problems = repair._public_section_state(
        fresh_snapshot,
        plan,
        new_video=new_video,
        cover_url=cover_url,
    )
    if fresh_projection == "drift":
        return repair.RepairResult(
            "BLOCKED_DRIFT",
            False,
            "fresh public or section observation contains real drift",
            {
                "problems": fresh_problems,
                "remote_mutation": False,
            },
        )

    next_state = (
        "VERIFIED" if fresh_projection == "target" else "PUBLIC_PENDING"
    )
    canonical_snapshot = snapshot_with_current_cover_identity(fresh_snapshot)
    details = {
        "new_video": dict(new_video),
        "cover_url": cover_url,
        "live_snapshot": canonical_snapshot,
        "problems": fresh_problems,
        "reconciliation": {
            "schema_version": COVER_ALIAS_RECONCILIATION_SCHEMA,
            "blocked_seq": blocked["seq"],
            "blocked_row_sha256": blocked["row_sha256"],
            "blocked_reason": COVER_ALIAS_FALSE_BLOCK_REASON,
            "blocked_projection_after_current_normalisation": (
                blocked_projection
            ),
            "fresh_projection": fresh_projection,
            "blocked_creator_cover": blocked_creator_cover,
            "frozen_cover_url": cover_url,
            "canonical_cover_identity": normalise_cover_url(cover_url),
        },
        "remote_mutation": False,
    }
    if not commit:
        return repair.RepairResult(
            next_state,
            False,
            f"dry-run proved safe reconciliation to {next_state}",
            {**details, "would_append_journal": True},
        )
    repair.append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state=next_state,
        details=details,
    )
    row = repair.plan_entries(journal, plan_path, plan)[-1]
    return repair._row_result(
        row,
        f"reconciled cover CDN alias false block to {next_state}",
    )
