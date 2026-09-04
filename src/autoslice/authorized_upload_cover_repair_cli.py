"""Cover-only repair command flows kept separate from the publish CLI shell.

The public command entry point retains its parser, lock and adapter factories.
This module owns the cover-repair transaction orchestration so that adding the
cover-only lane cannot make the general upload entry point a god module.
"""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from src.autoslice import same_bv_cover_repair as cover_repair_binding
from src.autoslice import same_bv_repair as repair_binding


def load_manifest(
    plan_path: Path,
    *,
    load_and_verify: Callable[..., tuple[dict | None, list[str]]],
    title_cover_qc_problems: Callable[..., list[str]],
) -> tuple[dict | None, dict | None, list[str]]:
    try:
        plan = cover_repair_binding.load_plan(plan_path)
    except cover_repair_binding.CoverRepairError as exc:
        return None, None, [str(exc)]
    manifest_path = Path(str((plan.get("manifest") or {}).get("path") or ""))
    manifest, problems = load_and_verify(manifest_path, frozen_plan_resume=True)
    if manifest is None or problems:
        return plan, manifest, problems
    problems.extend(title_cover_qc_problems(manifest, required=True))
    try:
        cover_repair_binding.validate_plan(plan, manifest=manifest, plan_path=plan_path)
    except cover_repair_binding.CoverRepairError as exc:
        problems.append(str(exc))
    return plan, manifest, problems


def plan(
    args: Any,
    *,
    default_upload_lock: Path,
    exclusive_upload_lock: Callable[..., Any],
    load_and_verify: Callable[..., tuple[dict | None, list[str]]],
    title_cover_qc_problems: Callable[..., list[str]],
    same_bv_cover_adapter: Callable[..., Any],
) -> int:
    """Freeze one exact cover-only repair without changing remote state."""

    manifest_path = Path(args.manifest).resolve()
    journal = Path(args.journal).resolve()
    lock_path = Path(args.lock) if args.lock else default_upload_lock
    with exclusive_upload_lock(lock_path):
        manifest, problems = load_and_verify(manifest_path)
        if manifest is not None:
            problems.extend(repair_binding.repair_publication_target_problems(manifest, args.bvid))
            problems.extend(title_cover_qc_problems(manifest, required=True))
        if problems or manifest is None:
            for problem in problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 2
        adapter = same_bv_cover_adapter(Path(args.cookie_json))
        section_id = (manifest.get("season") or {}).get("section_id")
        if not isinstance(section_id, int):
            print("REFUSE: cover repair manifest has no exact section_id", file=sys.stderr)
            return 2
        snapshot = adapter.observe(args.bvid, section_id)
        repair_plan = cover_repair_binding.create_plan(
            manifest_path=manifest_path,
            manifest=manifest,
            bvid=args.bvid,
            snapshot=snapshot,
            predecessor_completed_path=(
                Path(args.predecessor_completed).resolve() if args.predecessor_completed else None
            ),
        )
        if args.dry_run:
            print(json.dumps(repair_plan, ensure_ascii=False, indent=2))
            return 0
        plan_path = Path(args.out).resolve()
        cover_repair_binding.write_plan(plan_path, repair_plan)
        cover_repair_binding.initialise_journal(journal, plan_path, repair_plan)
    print(
        json.dumps(
            {
                "status": "PLANNED",
                "bvid": args.bvid,
                "plan": str(plan_path),
                "journal": str(journal),
                "remote_mutation": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


def status(
    args: Any,
    *,
    load_cover_repair_manifest: Callable[[Path], tuple[dict | None, dict | None, list[str]]],
) -> int:
    """Validate local cover-only authority without remote access."""

    plan_path = Path(args.plan).resolve()
    journal = Path(args.journal).resolve()
    _plan, manifest, problems = load_cover_repair_manifest(plan_path)
    if manifest is None or problems:
        for problem in problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        return 2
    result = cover_repair_binding.status(plan_path=plan_path, journal=journal, manifest=manifest)
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


def run(
    args: Any,
    *,
    default_upload_lock: Path,
    exclusive_upload_lock: Callable[..., Any],
    load_cover_repair_manifest: Callable[[Path], tuple[dict | None, dict | None, list[str]]],
    same_bv_cover_adapter: Callable[..., Any],
) -> int:
    """Resume a cover-only edit under the shared publication lock."""

    plan_path = Path(args.plan).resolve()
    journal = Path(args.journal).resolve()
    lock_path = Path(args.lock) if args.lock else default_upload_lock
    lock = nullcontext() if args.dry_run else exclusive_upload_lock(lock_path)
    with lock:
        _plan, manifest, problems = load_cover_repair_manifest(plan_path)
        if manifest is None or problems:
            for problem in problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 2
        if args.dry_run:
            result = cover_repair_binding.status(
                plan_path=plan_path, journal=journal, manifest=manifest
            )
        else:
            result = cover_repair_binding.run(
                plan_path=plan_path,
                journal=journal,
                manifest=manifest,
                adapter=same_bv_cover_adapter(Path(args.cookie_json)),
                wait_seconds=args.wait,
                poll_seconds=args.poll,
            )
    print(
        json.dumps(
            {
                "state": result.state,
                "changed": result.changed,
                "message": result.message,
                "details": result.details,
                "dry_run": bool(args.dry_run),
            },
            ensure_ascii=False,
        )
    )
    if result.state == "VERIFIED":
        return 0
    return 5 if result.state == "BLOCKED_DRIFT" else 6


def verify_live(
    args: Any,
    *,
    default_upload_lock: Path,
    exclusive_upload_lock: Callable[..., Any],
    load_cover_repair_manifest: Callable[[Path], tuple[dict | None, dict | None, list[str]]],
    same_bv_cover_adapter: Callable[..., Any],
    reconcile_same_bv_cover_publication: Callable[..., dict],
    reconciliation_error: type[Exception],
    sha256_file: Callable[[Path], str],
) -> int:
    """Create a fresh readback receipt for a VERIFIED cover-only edit."""

    plan_path = Path(args.plan).resolve()
    journal = Path(args.journal).resolve()
    out = Path(args.out).resolve()
    lock_path = Path(args.lock) if args.lock else default_upload_lock
    with exclusive_upload_lock(lock_path):
        _plan, manifest, problems = load_cover_repair_manifest(plan_path)
        if manifest is None or problems:
            for problem in problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 2
        completed = cover_repair_binding.verify_live(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=same_bv_cover_adapter(Path(args.cookie_json)),
            out=out,
        )
        try:
            reconciliation = reconcile_same_bv_cover_publication(
                completed_path=out,
                manifest=manifest,
                manifest_path=Path(str((completed.get("manifest") or {})["path"])),
                reconciled_at=str(completed["verified_at"]),
            )
        except reconciliation_error as exc:
            print(
                "LIVE VERIFIED BUT LOCAL RECONCILIATION PENDING: "
                f"{exc}; re-run cover-repair-verify-live with the same completed path",
                file=sys.stderr,
            )
            return 6
    print(
        json.dumps(
            {
                "status": completed["status"],
                "bvid": completed["bvid"],
                "unchanged_cid": completed["unchanged_cid"],
                "completed_sidecar": str(out),
                "completed_sidecar_sha256": sha256_file(out),
                "publication_reconciliation": reconciliation,
                "remote_mutation": False,
            },
            ensure_ascii=False,
        )
    )
    return 0
