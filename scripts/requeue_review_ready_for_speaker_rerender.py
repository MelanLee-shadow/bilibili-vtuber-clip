"""Requeue review_ready talk picks for a uniform_host speaker rerender.

维护者 逐字：「我刚刚看了一眼11号的切片，立刻发现一个严重问题，那就是
这些都是李豆沙单人直播，但是可能会偶尔有guest字幕混入。目前先把host改成单一
host，全部只有李豆沙一人直播，默认这样。重新做一下这些切片。」

生产 cron 已把 ``AUTOSLICE_SPEAKER_MODE`` 翻回 ``uniform_host``。仍未处理的是
一批已产出、未上传、非 hold 的 ``review_ready`` talk pick：它们是 ``auto``
speaker 模式下产的包，字幕可能混入 guest 归因，需要在 ``uniform_host`` 下原地
重做。仓里没有任何既有车道直接覆盖“把一条 review_ready pick 原地打回可恢复失
败”这件事——本脚本是这一条窄通道，唯一职责是把命名候选变成一条**production
既有 requeue 管线认识的、可恢复的失败 pick**，不做任何别的事。

## 为什么是“翻状态”而不是自己搬进 pending_talk

``requeue_stale_current_recovery_talks``（src/autoslice/delivery_recovery.py）
和 ``_recovery_queue_item`` 在把一条 pick 挪进 ``pending_talk`` 之前，要重新核
验磁盘证据（源分段文件、BCUT SRT、结构化弹幕绑定……）——这些证据只在 free 宿主
上、在真正的 tick 里才可靠可读。在这台隔离 worktree 里离线伪造那一套字段形状，
既做不到语义正确，也会把 ``talk_superseded_attempts`` 的归档行搞成两份不同形
状的重复实现。

仓内已有的、专门处理“把一条 review_ready pick 原地打回可恢复失败”这个场景的
先例是 ``scripts/revive_rejected_candidates.py --force-redo``（其帮助文本明确
写着 教训：“main lane 不会因为指纹变化自动重新 supersede 一条已经
ready 的 pick”）。它的手法只翻四个字段：``status`` → ``failed``、
``failure_recoverable`` → ``True``、追加一条 ``candidate-revival.v1`` revival、
写一个 ``sanctioned_revival_retry`` 标记。这四个字段是**字节级 load-bearing**
的——``src.autoslice.delivery_recovery._pending_sanctioned_revival_retry`` 只
认这个确切 schema/字段组合；下一次 tick 里 ``requeue_recoverable_talks`` 会把
它当"批准的复活重试"处理，无视 pipeline 指纹是否变化、无视终生重试预算上限，
安全地把它挪进 ``pending_talk`` 并让 ``_recovery_queue_item`` 现场重新核验证
据、写下真正的归档行。本脚本对这四个字段的写法与 revive_rejected_candidates.py
完全一致，不改名、不发明新 schema。

翻完之后这条候选的形状恰好等于
``src.autoslice.operator_processing_scope._named_recoverable_failed_pick``
判定的"未竟的可恢复失败 pick"——主会话随后补一张
``operator-processing-scope-grant.v2``（``RECOVER_NAMED_FAILED_PICKS``）就能
把该日期带回 tick 处理窗口。**本脚本自己绝不铸造这张 grant**——那是需要现场
核对 live state 的运维决定，不是这条窄通道的职责。

## 每候选校验（fail-closed，整批要么全部通过要么零写入）

- 候选在 ``state["picks"]`` 里存在且**恰好一行**；
- ``status == "review_ready"``；
- 不在 ``pending_talk``/``talk_backlog`` 里已有一份在途行（否则下次 tick 的
  ``existing_pending`` 去重会把这次翻转晾在原地，永远进不了队列）；
- ``src.autoslice.publication_registry.upload_block_reason`` 判定它未被
  ``published``/``hold_pending_review`` 挡住（含 runtime registry 合并，即
  已上传但仓库未提交的发布也会被挡）；
- 该行记录的包证据（``cover_path`` 指向的文件必须存在；且至少携带一个内容哈希
  字段 ``video_sha256``/``delivered_sha256``/``cover_sha256`` 之一）；
- ``--package-root`` 是一个存在的目录（粗粒度确认该日期的交付目录当下可读）。

## 事务与产物

本脚本从不删除或搬动任何媒体字节；旧包物理文件原地保留，被 production tick
的常规重产管线在同一 candidate_id 上覆盖（这与仓内既有 review_ready 重做的
唯一模式一致——供旧包的取证信息写进下面的审计侧车，供覆盖前留痕）。

除了四个 load-bearing 字段，每条候选还会得到一个独立的、自己的 schema 的审计
块 ``speaker_rerender_authority``（不影响 ``_pending_sanctioned_revival_retry``
的判据，纯审计）：写明 schema_version、维护者 逐字授权与时间戳、重做原因
（固定文案 ``"speaker_mode uniform_host rerender"``）、``--package-root``、旧
包记录的路径/哈希取证字段、以及本次操作时间。

state 原子写（tmp + fsync + rename）；全程自持 ``runner.lock``（不要在外层再
套 flock，会死锁）。默认 dry-run，只有 ``--apply`` 才写盘。

用法：
  python3 scripts/requeue_review_ready_for_speaker_rerender.py \
      --date \
      --candidate-id auto_173005_934_1166 \
      --package-root /opt/bilive/autoslice/lidousha/2026-08-11 \
      --authority-quote "目前先把host改成单一host，全部只有李豆沙一人直播，默认这样。重新做一下这些切片。" \
      --authority-timestamp T00:00:00Z \
      [--apply]
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice import publication_registry  # noqa: E402

REVIVAL_SCHEMA = "candidate-revival.v1"
SANCTIONED_REVIVAL_RETRY_SCHEMA = "sanctioned-revival-retry.v1"
SPEAKER_RERENDER_AUTHORITY_SCHEMA = "speaker-uniform-host-rerender-authority.v1"
RERENDER_REASON = "speaker_mode uniform_host rerender"
# 与 talk_delivery_recovery.requeue_recoverable_talks 里 existing_pending 的
# 去重口径一致：这两个集合表示"已经在排队"，不是"已经处理完"。
_QUEUED_COLLECTIONS = ("pending_talk", "talk_backlog")
_MIN_QUOTE_LEN = 8


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


def _row_candidate_id(row: object) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("candidate_id") or row.get("cid") or "")


def _queued_candidate_ids(state: dict) -> set[str]:
    found: set[str] = set()
    for key in _QUEUED_COLLECTIONS:
        rows = state.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            cid = _row_candidate_id(row)
            if cid:
                found.add(cid)
    return found


def _validate_candidate(
    cid: str,
    row: dict,
    *,
    date: str,
    package_root: Path,
    queued_ids: set[str],
) -> list[str]:
    """Return a list of refusal reasons (empty == this candidate is safe)."""

    problems: list[str] = []
    if row.get("status") != "review_ready":
        problems.append(f"status={row.get('status')!r} is not review_ready")
    if cid in queued_ids:
        problems.append(
            "candidate already has an active row in pending_talk/talk_backlog "
            "(would be dropped by the tick's own existing_pending dedupe)"
        )
    try:
        block_reason = publication_registry.upload_block_reason(
            cid, recording_date=date
        )
    except Exception as exc:  # noqa: BLE001 - registry read must fail closed
        problems.append(
            f"publication registry check failed: {type(exc).__name__}: {exc}"
        )
    else:
        if block_reason is not None:
            problems.append(
                f"publication registry blocks this candidate: {block_reason}"
            )
    cover_path_raw = row.get("cover_path")
    if not cover_path_raw or not Path(str(cover_path_raw)).is_file():
        problems.append(f"cover_path missing or unreadable: {cover_path_raw!r}")
    video_sha256 = row.get("video_sha256") or row.get("delivered_sha256")
    cover_sha256 = row.get("cover_sha256")
    if not video_sha256 and not cover_sha256:
        problems.append(
            "row carries no recorded content hash (video_sha256/"
            "delivered_sha256/cover_sha256) to audit the superseded package"
        )
    if not package_root.is_dir():
        problems.append(f"--package-root {package_root} is not a directory")
    return problems


def _build_mutation(
    cid: str,
    row: dict,
    *,
    date: str,
    package_root: Path,
    authority_quote: str,
    authority_timestamp: str,
    operated_at: str,
    expected_fix_commit: str,
) -> dict:
    """Return the field set to apply to ``row`` (not yet mutated)."""

    revival = {
        "schema_version": REVIVAL_SCHEMA,
        "revived_at": operated_at,
        "operator": "claude-worker-session",
        "reason": RERENDER_REASON,
        "expected_fix_commit": expected_fix_commit,
        "previous_status": row.get("status"),
        "previous_rejection_reason": row.get("rejection_reason"),
        "previous_failure_kind": row.get("failure_kind"),
    }
    sanctioned_revival_retry = {
        "schema_version": SANCTIONED_REVIVAL_RETRY_SCHEMA,
        "status": "PENDING",
        # ``revival_index`` is resolved against ``row["revivals"]`` at apply
        # time (len(existing) before append), not here.
        "revived_at": operated_at,
        "expected_fix_commit": expected_fix_commit,
    }
    speaker_rerender_authority = {
        "schema_version": SPEAKER_RERENDER_AUTHORITY_SCHEMA,
        "candidate_id": cid,
        "recording_date": date,
        "reason": RERENDER_REASON,
        "user_authorization": {
            "quote": authority_quote,
            "timestamp": authority_timestamp,
        },
        "old_status": row.get("status"),
        "old_package": {
            "package_root": str(package_root),
            "cover_path": row.get("cover_path"),
            "cover_sha256": row.get("cover_sha256"),
            "video_sha256": row.get("video_sha256") or row.get("delivered_sha256"),
            "title": row.get("title") or row.get("given_title"),
            "pipeline_fingerprint": row.get("pipeline_fingerprint"),
        },
        "operated_at": operated_at,
    }
    return {
        "revival": revival,
        "sanctioned_revival_retry": sanctioned_revival_retry,
        "speaker_rerender_authority": speaker_rerender_authority,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice")),
        help="autoslice base dir (state/<date>.json + runner.lock live here)",
    )
    parser.add_argument(
        "--candidate-id",
        action="append",
        required=True,
        dest="candidate_ids",
        help="repeatable; one or more review_ready talk candidate ids",
    )
    parser.add_argument(
        "--package-root",
        type=Path,
        required=True,
        help="delivered package directory for --date (coarse presence check)",
    )
    parser.add_argument("--authority-quote", required=True)
    parser.add_argument("--authority-timestamp", required=True)
    parser.add_argument("--runner-lock", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if len(args.authority_quote.strip()) < _MIN_QUOTE_LEN:
        print(
            "REFUSE: --authority-quote is too short to be a real authorization",
            file=sys.stderr,
        )
        return 2
    try:
        _dt.datetime.fromisoformat(args.authority_timestamp.replace("Z", "+00:00"))
    except ValueError:
        print(
            "REFUSE: --authority-timestamp is not a parseable ISO8601 timestamp",
            file=sys.stderr,
        )
        return 2

    # Keep the publication registry's runtime-merge lookup consistent with
    # --base regardless of the caller's ambient environment.
    os.environ["AUTOSLICE_BASE"] = str(args.base)

    state_path = args.base / "state" / f"{args.date}.json"
    lock_path = args.runner_lock or (args.base / "runner.lock")
    try:
        lock_handle = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        print(
            f"REFUSE: --base {args.base} does not look valid "
            f"(runner.lock unreachable at {lock_path}): {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"REFUSE: state unreadable at {state_path}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    if not isinstance(state, dict):
        print(f"REFUSE: state at {state_path} is not a JSON object", file=sys.stderr)
        return 2
    picks = state.get("picks")
    if not isinstance(picks, list):
        print("REFUSE: state['picks'] is not a list", file=sys.stderr)
        return 2

    wanted = list(dict.fromkeys(args.candidate_ids))
    by_id: dict[str, list[dict]] = {}
    for row in picks:
        cid = _row_candidate_id(row)
        if cid in wanted:
            by_id.setdefault(cid, []).append(row)

    all_problems: dict[str, list[str]] = {}
    for cid in wanted:
        matches = by_id.get(cid, [])
        if len(matches) != 1:
            all_problems[cid] = [
                f"matches {len(matches)} row(s) in state['picks'] (need exactly 1)"
            ]

    queued_ids = _queued_candidate_ids(state)
    for cid in wanted:
        if cid in all_problems:
            continue
        row = by_id[cid][0]
        problems = _validate_candidate(
            cid,
            row,
            date=args.date,
            package_root=args.package_root,
            queued_ids=queued_ids,
        )
        if problems:
            all_problems[cid] = problems

    if all_problems:
        print("REFUSE: batch rejected, zero writes; problems by candidate:", file=sys.stderr)
        for cid in wanted:
            if cid in all_problems:
                print(f"  {cid}:", file=sys.stderr)
                for p in all_problems[cid]:
                    print(f"    - {p}", file=sys.stderr)
        return 2

    operated_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    expected_fix_commit = f"speaker-uniform-host-rerender:{args.authority_timestamp}"

    plans = []
    for cid in wanted:
        row = by_id[cid][0]
        mutation = _build_mutation(
            cid,
            row,
            date=args.date,
            package_root=args.package_root,
            authority_quote=args.authority_quote,
            authority_timestamp=args.authority_timestamp,
            operated_at=operated_at,
            expected_fix_commit=expected_fix_commit,
        )
        plans.append((cid, row, mutation))
        print(f"requeue {cid}: review_ready -> failed(recoverable) for uniform_host rerender")
        old_package = mutation["speaker_rerender_authority"]["old_package"]
        print(f"  old package evidence: {old_package}")

    if args.apply:
        for cid, row, mutation in plans:
            revivals = row.setdefault("revivals", [])
            revivals.append(mutation["revival"])
            mutation["sanctioned_revival_retry"]["revival_index"] = len(revivals) - 1
            row["sanctioned_revival_retry"] = mutation["sanctioned_revival_retry"]
            row["status"] = "failed"
            row["failure_recoverable"] = True
            row["speaker_rerender_authority"] = mutation["speaker_rerender_authority"]
        _atomic_write(state_path, state)
        print(f"APPLIED: {len(plans)} candidate(s) requeued in {state_path}")
    else:
        print(f"DRY-RUN: {len(plans)} candidate(s) would be requeued (pass --apply)")

    print(
        "NOTE: this only makes the candidate a recoverable failed pick "
        f"(state['picks'] status=failed, failure_recoverable=True) for {args.date}. "
        "It does NOT admit the date into the tick's processing window. "
        "scripts/session_autoslice.py:list_dates() only processes the "
        "latest 3 recording dates plus status=='source_incomplete', an "
        "in-progress historical source recovery, or an admitted "
        "operator_processing_scope grant "
        "(src/autoslice/operator_processing_scope.py). If "
        f"{args.date} is not in the latest-3 window, mint an "
        "operator-processing-scope-grant.v2 (RECOVER_NAMED_FAILED_PICKS) "
        f"grant naming {wanted} for {args.date} before the next tick, or "
        "this candidate will sit as a recoverable failed pick indefinitely "
        "(no expiry on this transition by itself)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
