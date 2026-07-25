"""Revive fossilized candidate_rejected picks after their upstream fix landed.

candidate_rejected 是防 backfill 顶位的化石化终态（subtitle_authority 类不可
自动恢复失败）。当拦截原因已被上游修复（ledger 真值 / 门修复 / findings
回灌），rejected 不会自动 requeue——本脚本是唯一 sanctioned 复活通道：

- 只接受 status == candidate_rejected 的 pick；
- 翻回 status=failed + failure_recoverable=True，让既有的
  requeue_recoverable_deliveries 在下个 tick 按 fingerprint 变化拾起；
- 每次复活写入 revival 审计块（操作者/时间/理由/期望修复 commit）；
- 全程持 runner.lock（串行于 cron tick），state 原子写。

用法：
  python3 scripts/revive_rejected_candidates.py \
      --state /opt/bilive/autoslice/state/2026-07-24.json \
      --candidate auto_183122_607_723 --candidate auto_190124_199_480 \
      --reason "gift-name truth landed; findings memory now applies" \
      --fix-commit 8e8a740 [--apply]

无 --apply 为 dry-run（只打印将发生的变更）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

REVIVABLE_STATUSES = ("candidate_rejected", "review_ready")


def _atomic_write(path: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--candidate", action="append", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--fix-commit", required=True)
    parser.add_argument("--runner-lock", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--force-redo",
        action="store_true",
        help="also accept review_ready picks whose artifacts predate a "
        "required fix (main lane does NOT re-supersede ready picks on "
        "fingerprint change — 2026-07-25 lesson)",
    )
    args = parser.parse_args()

    lock_path = args.runner_lock or (args.state.parent.parent / "runner.lock")
    lock_handle = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

    state = json.loads(args.state.read_text(encoding="utf-8"))
    picks = state.get("picks") or []
    wanted = list(dict.fromkeys(args.candidate))
    by_id = {}
    for row in picks:
        cid = str(row.get("candidate_id") or "")
        if cid in wanted:
            by_id[cid] = row
    missing = [cid for cid in wanted if cid not in by_id]
    if missing:
        print(f"REFUSE: candidates not in state: {missing}", file=sys.stderr)
        return 2

    revived = []
    for cid in wanted:
        row = by_id[cid]
        allowed = (
            REVIVABLE_STATUSES if args.force_redo else REVIVABLE_STATUSES[:1]
        )
        if row.get("status") not in allowed:
            print(
                f"REFUSE: {cid} status={row.get('status')!r} not in {allowed}",
                file=sys.stderr,
            )
            return 2
        revival = {
            "schema_version": "candidate-revival.v1",
            "revived_at": _dt.datetime.now(_dt.timezone.utc).isoformat(
                timespec="seconds"
            ),
            "operator": "claude-root-session",
            "reason": args.reason,
            "expected_fix_commit": args.fix_commit,
            "previous_status": row.get("status"),
            "previous_rejection_reason": row.get("rejection_reason"),
            "previous_failure_kind": row.get("failure_kind"),
        }
        print(f"revive {cid}: {row.get('status')} -> failed(recoverable)")
        print(f"  was: {row.get('rejection_reason')} / {row.get('failure_kind')}")
        if args.apply:
            row["status"] = "failed"
            row["failure_recoverable"] = True
            row.setdefault("revivals", []).append(revival)
        revived.append(cid)

    if args.apply:
        _atomic_write(args.state, state)
        print(f"APPLIED: {len(revived)} candidate(s) revived in {args.state}")
    else:
        print(f"DRY-RUN: {len(revived)} candidate(s) would be revived (pass --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
