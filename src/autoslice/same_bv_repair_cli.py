"""Thin CLI orchestration for exceptional same-BV repair operations."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from src.autoslice import same_bv_live_verification
from src.autoslice import same_bv_repair as repair
from src.autoslice.same_bv_cover_reconciliation import (
    reconcile_cover_alias_false_block,
    snapshots_equivalent,
)


def repair_reconcile_blocked(args) -> int:
    """Correct one proven CDN-alias false block without remote mutation."""

    # Import lazily so scripts.authorized_upload can register this function as
    # a handler while its production dependencies remain monkeypatchable.
    from scripts import authorized_upload as upload

    plan_path = Path(args.plan).resolve()
    journal = Path(args.journal).resolve()
    lock_path = Path(args.lock) if args.lock else upload.DEFAULT_UPLOAD_LOCK
    with upload.exclusive_upload_lock(lock_path):
        plan, manifest, problems = upload._load_repair_manifest(plan_path)
        if problems or manifest is None or plan is None:
            for problem in problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 2
        result = reconcile_cover_alias_false_block(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=upload._same_bv_adapter(
                Path(args.cookie_json),
                Path(args.biliup_cookie_json),
                str(plan["bvid"]),
            ),
            commit=not args.dry_run,
        )
    print(
        json.dumps(
            {
                "state": result.state,
                "changed": result.changed,
                "message": result.message,
                "details": result.details,
                "dry_run": bool(args.dry_run),
                "remote_mutation": False,
            },
            ensure_ascii=False,
        )
    )
    if result.state == "VERIFIED":
        return 0
    return 5 if result.state == "BLOCKED_DRIFT" else 6


def repair_verify_live(args) -> int:
    """Freshly re-observe a VERIFIED repair and freeze a completed receipt."""

    from scripts import authorized_upload as upload

    return same_bv_live_verification.run_repair_verify_live(
        args,
        default_lock=upload.DEFAULT_UPLOAD_LOCK,
        exclusive_lock=upload.exclusive_upload_lock,
        load_repair_manifest=upload._load_repair_manifest,
        status_reader=upload.same_bv_repair_status,
        journal_entries=repair.plan_entries,
        adapter_factory=upload._same_bv_adapter,
        create_sidecar=upload._create_json_sidecar,
        sha256_file=upload.sha256_file,
        now=upload.now,
        observation_unavailable=repair.ObservationUnavailable,
        snapshots_equal=snapshots_equivalent,
    )
