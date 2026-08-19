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
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.selection_scorecard import (  # noqa: E402
    selection_calibration_violations,
    selection_scorecard_is_valid,
)

REVIVABLE_STATUSES = ("candidate_rejected", "review_ready")
SANCTIONED_REVIVAL_RETRY_SCHEMA = "sanctioned-revival-retry.v1"
SONG_REVIVAL_SCHEMA = "song-candidate-revival.v1"
# ``requeue_recoverable_songs`` 只在 song_pipeline_fingerprint 变化时给一条
# blocked 记录新的尝试预算。复活必须显式声明「上游已修，这条的旧指纹作废」，
# 因此写入一个绝不可能等于任何真实指纹的哨兵值，而不是伪造一个假指纹。
SONG_REVIVAL_FINGERPRINT_PREFIX = "sanctioned-revival:"


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


def _revive_songs(
    *,
    state: dict,
    wanted: list[str],
    reason: str,
    fix_commit: str,
    apply: bool,
) -> int:
    """复活 state["songs"] 里的 candidate_rejected 歌切行。

    与 talk 的差别（不是同一套机制，不能复用 picks 分支）：
    - 记录在 ``state["songs"]``，不在 ``state["picks"]``；
    - 终态由 ``project_terminal_song_disposition`` 铸造，附
      ``song_terminal_disposition`` 收据，必须一并撤销，否则下一 tick 立刻
      重新铸造回 candidate_rejected；
    - 重新入队走 ``requeue_recoverable_songs``（需要 status ∈ {blocked,failed}
      且 song_pipeline_fingerprint 发生变化），不走 talk 的
      ``failure_recoverable`` + ``sanctioned_revival_retry`` 通道。
    """

    songs = [row for row in state.get("songs", []) if isinstance(row, dict)]
    by_id = {
        str(row.get("candidate_id") or ""): row
        for row in songs
        if str(row.get("candidate_id") or "") in wanted
    }
    missing = [cid for cid in wanted if cid not in by_id]
    if missing:
        print(f"REFUSE: song candidates not in state['songs']: {missing}", file=sys.stderr)
        return 2

    for cid in wanted:
        row = by_id[cid]
        if row.get("status") != "candidate_rejected":
            print(
                f"REFUSE: {cid} status={row.get('status')!r} is not candidate_rejected",
                file=sys.stderr,
            )
            return 2
        if row.get("delivered"):
            print(f"REFUSE: {cid} is already delivered", file=sys.stderr)
            return 2

    for cid in wanted:
        row = by_id[cid]
        disposition = row.get("song_terminal_disposition") or {}
        revival = {
            "schema_version": SONG_REVIVAL_SCHEMA,
            "revived_at": _dt.datetime.now(_dt.timezone.utc).isoformat(
                timespec="seconds"
            ),
            "operator": "claude-root-session",
            "reason": reason,
            "expected_fix_commit": fix_commit,
            "previous_status": row.get("status"),
            "previous_terminal_disposition": dict(disposition) if disposition else None,
            "previous_reason_codes": list(row.get("reason_codes") or []),
            "previous_song_pipeline_fingerprint": row.get("song_pipeline_fingerprint"),
        }
        print(f"revive song {cid}: candidate_rejected -> blocked")
        print(f"  was: {disposition.get('reason_codes')}")
        print(f"  withdrawing {len(revival['previous_reason_codes'])} stale reason code(s)")
        if apply:
            row["status"] = "blocked"
            row.pop("song_terminal_disposition", None)
            row.pop("decision", None)
            # 必须清空 reason_codes，否则复活当场作废：
            # requeue_recoverable_songs（delivery_recovery.py:1180-1192）在检查
            # status 之前先调用 project_terminal_song_disposition，后者只看
            # status ∈ {blocked,failed} + rc==0 + reason_codes 里还有
            # SONG_DETERMINISTIC_PROOF_REJECTION_CODES，就会立刻把这行重新铸成
            # candidate_rejected —— 实测复活后第一次 tick 就被打回。
            # 旧判据不是被抹掉，是被"撤回"：完整原文保存在 song_revivals 审计块
            # 的 previous_reason_codes 里，等这次新的尝试自己重新给出判据。
            row["reason_codes"] = []
            row["song_pipeline_fingerprint"] = (
                SONG_REVIVAL_FINGERPRINT_PREFIX + fix_commit
            )
            row.setdefault("song_revivals", []).append(revival)

    if apply:
        print(f"APPLIED: {len(wanted)} song candidate(s) revived")
    else:
        print(f"DRY-RUN: {len(wanted)} song candidate(s) would be revived (pass --apply)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--candidate", action="append", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--fix-commit", required=True)
    parser.add_argument("--runner-lock", type=Path, default=None)
    parser.add_argument(
        "--lane",
        choices=("talk", "song"),
        default="talk",
        help="talk 复活 state['picks']（默认，历史行为）；song 复活 "
        "state['songs'] 里被 project_terminal_song_disposition 铸成 "
        "candidate_rejected 的歌切行",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--resume-unqueued-revival",
        action="store_true",
        help=(
            "repair a prior sanctioned revival that was written as failed/"
            "recoverable but never received its one-shot requeue marker; "
            "requires the latest revival reason and fix commit to match"
        ),
    )
    parser.add_argument(
        "--force-redo",
        action="store_true",
        help="also accept review_ready picks whose artifacts predate a "
        "required fix (main lane does NOT re-supersede ready picks on "
        "fingerprint change — 2026-07-25 lesson)",
    )
    parser.add_argument(
        "--restore-selection-scorecard-from-spec",
        type=Path,
        help="restore one candidate's missing/invalid selection_scorecard from "
        "an existing candidate-matched spec after validating the scorecard "
        "and current calibration policy",
    )
    args = parser.parse_args()

    scorecard_restoration = None
    if args.restore_selection_scorecard_from_spec is not None:
        if len(args.candidate) != 1:
            print(
                "REFUSE: --restore-selection-scorecard-from-spec requires "
                "exactly one --candidate",
                file=sys.stderr,
            )
            return 2
        spec_path = args.restore_selection_scorecard_from_spec
        try:
            spec_bytes = spec_path.read_bytes()
            spec = json.loads(spec_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                "REFUSE: selection scorecard spec is unreadable or invalid: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            return 2
        candidate_id = args.candidate[0]
        if spec.get("candidate_id") != candidate_id:
            print(
                "REFUSE: selection scorecard spec candidate mismatch: "
                f"{spec.get('candidate_id')!r} != {candidate_id!r}",
                file=sys.stderr,
            )
            return 2
        scorecard = spec.get("selection_scorecard")
        if not selection_scorecard_is_valid(scorecard):
            print(
                "REFUSE: selection scorecard spec does not contain a valid "
                "scorecard",
                file=sys.stderr,
            )
            return 2
        calibration_violations = selection_calibration_violations(
            candidate_id,
            scorecard,
        )
        if calibration_violations:
            print(
                "REFUSE: selection scorecard violates current calibration "
                f"policy: {calibration_violations}",
                file=sys.stderr,
            )
            return 2
        scorecard_restoration = {
            "scorecard": dict(scorecard),
            "audit": {
                "schema_version": "selection-scorecard-restoration.v1",
                "source_spec_path": str(spec_path),
                "source_spec_sha256": "sha256:"
                + hashlib.sha256(spec_bytes).hexdigest(),
            },
        }

    lock_path = args.runner_lock or (args.state.parent.parent / "runner.lock")
    lock_handle = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

    state = json.loads(args.state.read_text(encoding="utf-8"))
    wanted = list(dict.fromkeys(args.candidate))

    if args.lane == "song":
        if args.restore_selection_scorecard_from_spec is not None or args.force_redo:
            print(
                "REFUSE: --restore-selection-scorecard-from-spec/--force-redo are "
                "talk-lane only",
                file=sys.stderr,
            )
            return 2
        rc = _revive_songs(
            state=state,
            wanted=wanted,
            reason=args.reason,
            fix_commit=args.fix_commit,
            apply=args.apply,
        )
        if rc == 0 and args.apply:
            _atomic_write(args.state, state)
            print(f"APPLIED to {args.state}")
        return rc

    picks = state.get("picks") or []
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
        latest_revival = (
            row.get("revivals")[-1]
            if isinstance(row.get("revivals"), list)
            and row.get("revivals")
            and isinstance(row.get("revivals")[-1], dict)
            else None
        )
        resume_existing = bool(
            args.resume_unqueued_revival
            and row.get("status") == "failed"
            and row.get("failure_recoverable") is True
            and isinstance(latest_revival, dict)
            and latest_revival.get("schema_version")
            == "candidate-revival.v1"
            and latest_revival.get("reason") == args.reason
            and latest_revival.get("expected_fix_commit") == args.fix_commit
            and not isinstance(row.get("sanctioned_revival_retry"), dict)
        )
        allowed = (
            REVIVABLE_STATUSES if args.force_redo else REVIVABLE_STATUSES[:1]
        )
        if row.get("status") not in allowed and not resume_existing:
            print(
                f"REFUSE: {cid} status={row.get('status')!r} not in {allowed}"
                " and is not an exactly matching unqueued revival",
                file=sys.stderr,
            )
            return 2
        if resume_existing:
            revival = latest_revival
        else:
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
        if scorecard_restoration is not None:
            if resume_existing:
                print(
                    "REFUSE: scorecard restoration cannot be resumed after "
                    "the original revival write",
                    file=sys.stderr,
                )
                return 2
            if selection_scorecard_is_valid(row.get("selection_scorecard")):
                print(
                    f"REFUSE: {cid} already has a valid selection_scorecard",
                    file=sys.stderr,
                )
                return 2
            restoration_audit = dict(scorecard_restoration["audit"])
            restoration_audit["restored_at"] = revival["revived_at"]
            revival["selection_scorecard_restoration"] = restoration_audit
        if resume_existing:
            print(f"resume unqueued revival {cid}: failed(recoverable)")
        else:
            print(f"revive {cid}: {row.get('status')} -> failed(recoverable)")
            print(
                f"  was: {row.get('rejection_reason')} / "
                f"{row.get('failure_kind')}"
            )
        if scorecard_restoration is not None:
            print(
                "  restore selection_scorecard from "
                f"{scorecard_restoration['audit']['source_spec_path']}"
            )
        if args.apply:
            if scorecard_restoration is not None:
                row["selection_scorecard"] = dict(
                    scorecard_restoration["scorecard"]
                )
            row["status"] = "failed"
            row["failure_recoverable"] = True
            if not resume_existing:
                row.setdefault("revivals", []).append(revival)
            revival_index = len(row.get("revivals") or []) - 1
            row["sanctioned_revival_retry"] = {
                "schema_version": SANCTIONED_REVIVAL_RETRY_SCHEMA,
                "status": "PENDING",
                "revival_index": revival_index,
                "revived_at": revival["revived_at"],
                "expected_fix_commit": revival["expected_fix_commit"],
            }
        revived.append(cid)

    if args.apply:
        _atomic_write(args.state, state)
        print(f"APPLIED: {len(revived)} candidate(s) revived in {args.state}")
    else:
        print(f"DRY-RUN: {len(revived)} candidate(s) would be revived (pass --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
