"""Fresh live verification for a completed same-BV repair."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable


def run_biliup_readonly_canary(
    cookie_json: Path,
    bvid: str,
    *,
    validate_cookie: Callable,
    biliup_bin: Path,
    run: Callable,
    repair_error: type[Exception],
) -> None:
    """Prove the append CLI can read the exact target without leaking output."""

    validate_cookie(cookie_json)
    completed = run(
        [
            str(biliup_bin),
            "-u",
            cookie_json.name,
            "show",
            bvid,
        ],
        cwd=str(cookie_json.parent),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise repair_error(
            "biliup read-only login canary failed before any append intent "
            f"(rc={completed.returncode})"
        )


def run_repair_status(
    args,
    *,
    load_repair_manifest: Callable,
    status_reader: Callable,
) -> int:
    """Validate and print local repair authority without remote access."""

    plan_path = Path(args.plan).resolve()
    journal = Path(args.journal).resolve()
    _plan, manifest, problems = load_repair_manifest(plan_path)
    if problems or manifest is None:
        for problem in problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        return 2
    result = status_reader(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
    )
    print(
        json.dumps(
            {
                "state": result.state,
                "message": result.message,
                "details": result.details,
                "remote_mutation": False,
            },
            ensure_ascii=False,
        )
    )
    if result.state == "VERIFIED":
        return 0
    return 5 if result.state == "BLOCKED_DRIFT" else 6


def run_repair_verify_live(
    args,
    *,
    default_lock: Path,
    exclusive_lock: Callable,
    load_repair_manifest: Callable,
    status_reader: Callable,
    journal_entries: Callable,
    adapter_factory: Callable,
    create_sidecar: Callable,
    sha256_file: Callable[[Path], str],
    now: Callable[[], str],
    observation_unavailable: type[Exception],
) -> int:
    """Re-observe a VERIFIED repair and create one byte-bound receipt."""

    plan_path = Path(args.plan).resolve()
    journal = Path(args.journal).resolve()
    out = Path(args.out).resolve()
    lock_path = Path(args.lock) if args.lock else default_lock
    with exclusive_lock(lock_path):
        plan, manifest, problems = load_repair_manifest(plan_path)
        if problems or manifest is None or plan is None:
            for problem in problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 2
        status = status_reader(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
        )
        if status.state != "VERIFIED":
            print(
                f"REFUSE: repair is {status.state}, not VERIFIED",
                file=sys.stderr,
            )
            return 5 if status.state == "BLOCKED_DRIFT" else 6
        rows = journal_entries(journal, plan_path, plan)
        if not rows or rows[-1].get("state") != "VERIFIED":
            print(
                "REFUSE: VERIFIED status has no terminal journal row",
                file=sys.stderr,
            )
            return 2
        verified_row = rows[-1]
        verified_details = verified_row.get("details") or {}
        expected_snapshot = verified_details.get("live_snapshot")
        if not isinstance(expected_snapshot, dict):
            print(
                "REFUSE: VERIFIED journal row has no bound live snapshot",
                file=sys.stderr,
            )
            return 2
        adapter = adapter_factory(
            Path(args.cookie_json),
            Path(args.biliup_cookie_json),
            str(plan["bvid"]),
        )
        try:
            fresh_snapshot = adapter.observe(
                str(plan["bvid"]),
                int((plan.get("season") or {})["section_id"]),
            )
        except observation_unavailable as exc:
            print(f"LIVE VERIFY PENDING: {exc}", file=sys.stderr)
            return 6
        if fresh_snapshot != expected_snapshot:
            print(
                json.dumps(
                    {
                        "status": "LIVE_DRIFT",
                        "bvid": plan["bvid"],
                        "remote_mutation": False,
                        "expected_verified_snapshot": expected_snapshot,
                        "fresh_snapshot": fresh_snapshot,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 5
        authority = plan.get("recovery_publication_authority") or {}
        completed = {
            "schema_version": "same-bv-repair-completed.v1",
            "status": "VERIFIED_FRESH_LIVE",
            "rc": 0,
            "verified_at": now(),
            "remote_mutation": False,
            "candidate_id": authority.get("candidate_id"),
            "bvid": plan["bvid"],
            "aid": authority.get("aid"),
            "new_cid": (
                ((fresh_snapshot.get("creator") or {}).get("videos") or [{}])[
                    0
                ].get("cid")
            ),
            "plan": {
                "path": str(plan_path),
                "sha256": sha256_file(plan_path),
                "plan_id": plan["plan_id"],
            },
            "manifest": dict(plan.get("manifest") or {}),
            "replacement": plan.get("replacement") or {},
            "verified_journal_row": {
                "journal_path": str(journal),
                "seq": verified_row.get("seq"),
                "at": verified_row.get("at"),
                "row_sha256": verified_row.get("row_sha256"),
            },
            "live_snapshot": fresh_snapshot,
        }
        create_sidecar(out, completed)
    print(
        json.dumps(
            {
                "status": completed["status"],
                "bvid": completed["bvid"],
                "new_cid": completed["new_cid"],
                "completed_sidecar": str(out),
                "completed_sidecar_sha256": sha256_file(out),
                "remote_mutation": False,
            },
            ensure_ascii=False,
        )
    )
    return 0
