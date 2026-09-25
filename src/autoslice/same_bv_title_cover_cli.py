"""Normal authorized-upload CLI orchestration for title-and-cover same-BV edits."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from src.autoslice.same_bv_title_cover_adapter import BilibiliTitleCoverAdapter
from src.autoslice.same_bv_title_cover_authority import (
    TitleCoverAuthorityError,
    load_authority,
)
from src.autoslice.same_bv_title_cover_plan import (
    TitleCoverRepairError,
    create_plan,
    load_plan,
    write_plan,
)
from src.autoslice.same_bv_title_cover_reconciliation import (
    TitleCoverReconciliationError,
    reconcile,
)
from src.autoslice.same_bv_title_cover_repair import (
    initialise_journal,
    run as execute_repair,
    status as repair_status,
)
from src.autoslice.same_bv_title_cover_verification import verify_live as verify_live_receipt


_LOCK = Callable[[Path], AbstractContextManager[None]]
_ADAPTER_FACTORY = Callable[[Path, Path, str], object]


def _values_for_key(value: object, key: str) -> list[object]:
    result: list[object] = []
    if isinstance(value, Mapping):
        for current_key, current_value in value.items():
            if current_key == key:
                result.append(current_value)
            result.extend(_values_for_key(current_value, key))
    elif isinstance(value, list):
        for item in value:
            result.extend(_values_for_key(item, key))
    return result


def _unique_string(value: object, key: str) -> str:
    unique = {
        item
        for item in _values_for_key(value, key)
        if isinstance(item, str) and item
    }
    if len(unique) != 1:
        raise TitleCoverRepairError(
            f"{key} must have one unambiguous non-empty value; observed={sorted(unique)!r}"
        )
    return next(iter(unique))


def _unique_integer(value: object, key: str) -> int:
    unique = {item for item in _values_for_key(value, key) if type(item) is int}
    if len(unique) != 1:
        raise TitleCoverRepairError(
            f"{key} must have one unambiguous integer value; observed={sorted(unique)!r}"
        )
    return next(iter(unique))


def _promote_adapter(base: object) -> BilibiliTitleCoverAdapter:
    """Reuse the production session/http bindings without reopening credentials."""

    state = vars(base)
    if not state:
        raise TitleCoverRepairError("production repair adapter has no transferable state")
    promoted = BilibiliTitleCoverAdapter.__new__(BilibiliTitleCoverAdapter)
    vars(promoted).update(state)
    return promoted


def _adapter(args: Any, bvid: str, factory: _ADAPTER_FACTORY) -> BilibiliTitleCoverAdapter:
    base = factory(Path(args.cookie_json), Path(args.biliup_cookie_json), bvid)
    return _promote_adapter(base)


def _refuse(exc: Exception) -> int:
    print(f"REFUSE: {exc}", file=sys.stderr)
    return 2


def plan(
    args: Any,
    *,
    default_upload_lock: Path,
    exclusive_upload_lock: _LOCK,
    base_adapter_factory: _ADAPTER_FACTORY,
) -> int:
    try:
        authority_path = Path(args.authority).resolve()
        lock_path = Path(args.lock) if args.lock else default_upload_lock
        with exclusive_upload_lock(lock_path):
            authority = load_authority(authority_path)
            bvid = _unique_string(authority, "bvid")
            section_id = _unique_integer(authority, "section_id")
            snapshot = _adapter(args, bvid, base_adapter_factory).observe(bvid, section_id)
            value = create_plan(
                authority_path=authority_path,
                authority=authority,
                snapshot=snapshot,
            )
            if args.dry_run:
                print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
                return 0
            plan_path = Path(args.out).resolve()
            journal = Path(args.journal).resolve()
            write_plan(plan_path, value)
            initialise_journal(journal, plan_path, value)
        print(
            json.dumps(
                {
                    "status": "PLANNED",
                    "bvid": bvid,
                    "plan": str(plan_path),
                    "journal": str(journal),
                    "remote_mutation": False,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (TitleCoverAuthorityError, TitleCoverRepairError, OSError, ValueError) as exc:
        return _refuse(exc)


def run(
    args: Any,
    *,
    default_upload_lock: Path,
    exclusive_upload_lock: _LOCK,
    base_adapter_factory: _ADAPTER_FACTORY,
) -> int:
    try:
        plan_path = Path(args.plan).resolve()
        journal = Path(args.journal).resolve()
        value = load_plan(plan_path)
        bvid = _unique_string(value, "bvid")
        lock_path = Path(args.lock) if args.lock else default_upload_lock
        with exclusive_upload_lock(lock_path):
            result = execute_repair(
                plan_path=plan_path,
                journal=journal,
                adapter=_adapter(args, bvid, base_adapter_factory),
                wait_seconds=float(args.wait),
                poll_seconds=float(args.poll),
            )
        print(
            json.dumps(
                {
                    "state": result.state,
                    "changed": result.changed,
                    "message": result.message,
                    "details": result.details,
                },
                ensure_ascii=False,
            )
        )
        if result.state == "VERIFIED":
            return 0
        if result.state == "BLOCKED_DRIFT":
            return 5
        return 6
    except (TitleCoverRepairError, OSError, ValueError) as exc:
        return _refuse(exc)


def status(args: Any) -> int:
    try:
        result = repair_status(
            plan_path=Path(args.plan).resolve(),
            journal=Path(args.journal).resolve(),
        )
        print(
            json.dumps(
                {
                    "state": result.state,
                    "changed": result.changed,
                    "message": result.message,
                    "details": result.details,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (TitleCoverRepairError, OSError, ValueError) as exc:
        return _refuse(exc)


def verify_live(
    args: Any,
    *,
    default_upload_lock: Path,
    exclusive_upload_lock: _LOCK,
    base_adapter_factory: _ADAPTER_FACTORY,
    now: Callable[[], str],
) -> int:
    try:
        authority_path = Path(args.authority).resolve()
        plan_path = Path(args.plan).resolve()
        journal = Path(args.journal).resolve()
        completed_path = Path(args.out).resolve()
        reconciliation_path = Path(args.reconciliation_out).resolve()
        value = load_plan(plan_path)
        bvid = _unique_string(value, "bvid")
        lock_path = Path(args.lock) if args.lock else default_upload_lock
        with exclusive_upload_lock(lock_path):
            if not completed_path.exists():
                verify_live_receipt(
                    plan_path=plan_path,
                    journal=journal,
                    adapter=_adapter(args, bvid, base_adapter_factory),
                    out=completed_path,
                )
            receipt = reconcile(
                authority_path=authority_path,
                plan_path=plan_path,
                journal=journal,
                completed_path=completed_path,
                out=reconciliation_path,
                reconciled_at=now(),
            )
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except (
        TitleCoverAuthorityError,
        TitleCoverRepairError,
        TitleCoverReconciliationError,
        OSError,
        ValueError,
    ) as exc:
        return _refuse(exc)



def _authorized_upload_module():
    # Imported only after scripts.authorized_upload completed initialization;
    # this keeps the parser import cycle harmless and reuses the normal secrets,
    # lock and production adapter factories.
    import scripts.authorized_upload as authorized_upload

    return authorized_upload


def entry_plan(args: Any) -> int:
    authorized_upload = _authorized_upload_module()
    return plan(
        args,
        default_upload_lock=authorized_upload.DEFAULT_UPLOAD_LOCK,
        exclusive_upload_lock=authorized_upload.exclusive_upload_lock,
        base_adapter_factory=authorized_upload._same_bv_adapter,
    )


def entry_run(args: Any) -> int:
    authorized_upload = _authorized_upload_module()
    return run(
        args,
        default_upload_lock=authorized_upload.DEFAULT_UPLOAD_LOCK,
        exclusive_upload_lock=authorized_upload.exclusive_upload_lock,
        base_adapter_factory=authorized_upload._same_bv_adapter,
    )


def entry_status(args: Any) -> int:
    return status(args)


def entry_verify_live(args: Any) -> int:
    authorized_upload = _authorized_upload_module()
    return verify_live(
        args,
        default_upload_lock=authorized_upload.DEFAULT_UPLOAD_LOCK,
        exclusive_upload_lock=authorized_upload.exclusive_upload_lock,
        base_adapter_factory=authorized_upload._same_bv_adapter,
        now=authorized_upload.now,
    )
