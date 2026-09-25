"""Fresh public/Creator/section closure for title-cover revisions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from src.autoslice.same_bv_cover_reconciliation import snapshots_equivalent
from src.autoslice import same_bv_title_cover_journal as journal_binding
from src.autoslice import same_bv_title_cover_plan as plan_binding


COMPLETED_SCHEMA = "same-bv-title-cover-repair-completed.v1"


def verify_live(
    *,
    plan_path: Path,
    journal: Path,
    adapter: Any,
    out: Path,
) -> dict[str, Any]:
    from src.autoslice.same_bv_title_cover_repair import transition_projection

    plan = plan_binding.load_plan(plan_path)
    plan_binding.validate_plan(plan, plan_path=plan_path)
    rows = journal_binding.plan_rows(journal, plan_path, plan)
    if not rows or rows[-1].get("state") != "VERIFIED":
        state = rows[-1].get("state") if rows else "PLANNED"
        raise plan_binding.TitleCoverRepairError(
            f"title-cover repair is {state}, not VERIFIED"
        )
    terminal = rows[-1]
    details = terminal.get("details") or {}
    uploaded_url = details.get("uploaded_cover_url")
    expected = details.get("live_snapshot")
    if not isinstance(uploaded_url, str) or not isinstance(expected, Mapping):
        raise journal_binding.TitleCoverJournalCorrupt(
            "VERIFIED row lacks cover URL/live snapshot"
        )
    fresh = adapter.observe(
        str(plan["bvid"]), int((plan.get("season") or {})["section_id"])
    )
    projection, problems = transition_projection(fresh, plan, uploaded_url)
    if projection != "target" or not snapshots_equivalent(
        plan_binding.canonical_snapshot(fresh), expected
    ):
        detail = "; ".join(problems) if problems else "snapshot drift"
        raise plan_binding.TitleCoverRepairError(
            f"fresh title-cover verification failed: {detail}"
        )
    completed = {
        "schema_version": COMPLETED_SCHEMA,
        "status": "VERIFIED_FRESH_LIVE",
        "verified_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "remote_mutation": False,
        "candidate_id": plan["candidate_id"],
        "bvid": plan["bvid"],
        "aid": plan["aid"],
        "unchanged_cid": plan["unchanged_cid"],
        "old_title": plan["old_title"],
        "new_title": plan["target_title"],
        "old_cover_url": plan["old_cover_url"],
        "uploaded_cover_url": uploaded_url,
        "new_cover": dict(plan["replacement_cover"]),
        "media_identity": dict(plan["media_identity"]),
        "authority": dict(plan["authority"]),
        "plan": {
            "path": str(plan_path.resolve()),
            "sha256": plan_binding.sha256_file(plan_path),
            "plan_id": plan["plan_id"],
        },
        "verified_journal_row": {
            "journal_path": str(journal.resolve()),
            "seq": terminal["seq"],
            "at": terminal["at"],
            "row_sha256": terminal["row_sha256"],
        },
        "live_snapshot": plan_binding.canonical_snapshot(fresh),
    }
    out = out.resolve()
    if out.exists() or out.is_symlink():
        existing = plan_binding.load_json(out, "title-cover completed receipt")
        stable_existing, stable_completed = dict(existing), dict(completed)
        stable_existing.pop("verified_at", None)
        stable_completed.pop("verified_at", None)
        if stable_existing != stable_completed:
            raise plan_binding.TitleCoverRepairError(
                "existing completion differs from fresh closure"
            )
        return existing
    plan_binding.create_json(out, completed)
    return completed
