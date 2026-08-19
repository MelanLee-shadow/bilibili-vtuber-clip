"""不可读窗删除在终审自愈循环里的事务落盘（维护者 裁定的接线层）。

判据、守卫与回执形状全部在 ``unreadable_span_policy``；本模块只做两件事，
且刻意住在 ``producer_package_finalization`` 外面 —— 那个 god-file 的行数账本
（``tests/test_runtime_architecture.py``）的规矩是「新增逻辑的重量推给新模块，
调用点只留薄的一层」：

* ``stage_unreadable_cue_drop_pass`` —— 与 CPA 自愈**同款**的事务：账本先齐、
  活的 SRT 字节最后换，任何异常整组回滚到进 pass 前的精确字节；
* ``seal_unreadable_cue_drops`` —— 干净收尾时把删除审计封成 PASS、重跑一遍
  终审合同校验、落旁车回执（runner 只信落盘文件，不信 4000 字节 summary 尾窗）。

次序是刻意的：调用点把它排在 CPA 自愈**之后**，且只在「判官这一轮什么都改不动」
时才轮到本路 —— 删字幕是有损操作，永远是最后手段。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.autoslice.unreadable_cue_review import drop_sidecar_path
from src.autoslice.unreadable_span_policy import (
    UNREADABLE_CUE_DROP_MAX_PASSES,
    apply_unreadable_cue_drops,
    build_unreadable_cue_drop_audit,
    build_unreadable_cue_drop_pass,
)


def _finalization():
    # 延迟导入：低层原子写/回滚 helper 住在调用方模块里，顶层互相 import 会成环。
    from src.autoslice import producer_package_finalization

    return producer_package_finalization


def _json_text(document: Mapping[str, Any]) -> str:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )


def stage_unreadable_cue_drop_pass(
    *,
    reason_code: str,
    cpa_repairs: list,
    pass_budget_left: bool,
    final_text: str,
    audit: Mapping[str, Any],
    expected_srt_sha256: str,
    passes: list[dict[str, Any]],
    recut: Any,
    chat_authority_audit: dict,
    chat_authority_path: Path,
    review_audit_path: Path,
    snapshots: tuple[Any, dict, dict | None],
) -> list[dict[str, Any]] | None:
    """删掉本轮的不可读 cue 并把整组新字节落盘；不适用时返回 ``None``。

    ``None`` 严格表示「本路不适用，按今天照旧拦死」——调用方据此继续走结转 +
    ``FINAL_REVIEW_RELEASE_BLOCKED``。
    """

    if (
        reason_code != "FINAL_REVIEW_UNRESOLVED_FINDINGS"
        # CPA 判官真做得动就先走那条：本路只在判官改不动时才轮到。
        or cpa_repairs
        or not pass_budget_left
        or len(passes) >= UNREADABLE_CUE_DROP_MAX_PASSES
    ):
        return None
    dropped_text, drops = apply_unreadable_cue_drops(final_text, audit)
    if not drops:
        return None
    finalization = _finalization()
    file_bytes_before_pass, chat_authority_before_pass, baseline_before_pass = (
        snapshots
    )
    dropped_sha256 = hashlib.sha256(
        dropped_text.encode("utf-8")
    ).hexdigest()
    next_passes = [
        *passes,
        build_unreadable_cue_drop_pass(
            pass_index=len(passes) + 1,
            input_srt_sha256=expected_srt_sha256,
            output_srt_sha256="sha256:" + dropped_sha256,
            drops=drops,
        ),
    ]
    pending = build_unreadable_cue_drop_audit(
        passes=next_passes, status="REVIEW_PENDING"
    )
    staged_chat_authority = deepcopy(chat_authority_audit)
    staged_baseline = (
        deepcopy(recut.redelivery_baseline_audit)
        if recut.redelivery_baseline_audit is not None
        else None
    )
    try:
        staged_chat_authority["unreadable_cue_drops"] = pending
        staged_chat_authority["final_output_srt_sha256"] = dropped_sha256
        if staged_baseline is not None:
            staged_baseline[
                "post_unreadable_cue_drop_output_sha256"
            ] = dropped_sha256
            staged_baseline["unreadable_cue_drops"] = pending
        payloads: list[tuple[Path, bytes]] = [
            (review_audit_path, finalization._json_bytes(audit)),
            (
                chat_authority_path,
                finalization._json_bytes(staged_chat_authority),
            ),
        ]
        if (
            staged_baseline is not None
            and recut.redelivery_baseline_audit_path is not None
        ):
            payloads.append(
                (
                    recut.redelivery_baseline_audit_path,
                    finalization._json_bytes(staged_baseline),
                )
            )
        # 与 CPA 自愈同序：账本先齐，最后才换活的 SRT 字节。
        payloads.append(
            (recut.subtitle_path, dropped_text.encode("utf-8"))
        )
        for path, payload in payloads:
            finalization._write_bytes_atomic(path, payload)
        chat_authority_audit.clear()
        chat_authority_audit.update(staged_chat_authority)
        if (
            recut.redelivery_baseline_audit is not None
            and staged_baseline is not None
        ):
            recut.redelivery_baseline_audit.clear()
            recut.redelivery_baseline_audit.update(staged_baseline)
    except Exception:
        finalization._restore_file_bytes(file_bytes_before_pass)
        chat_authority_audit.clear()
        chat_authority_audit.update(chat_authority_before_pass)
        if (
            recut.redelivery_baseline_audit is not None
            and baseline_before_pass is not None
        ):
            recut.redelivery_baseline_audit.clear()
            recut.redelivery_baseline_audit.update(baseline_before_pass)
        raise
    return next_passes


def seal_unreadable_cue_drops(
    *,
    cid: str,
    passes: list[dict[str, Any]],
    expected_srt_sha256: str,
    audit: dict,
    chat_authority_audit: dict,
    recut: Any,
    review_audit_path: Path,
) -> dict[str, Any]:
    """干净收尾：封成 PASS、重校终审合同、落旁车回执与基线绑定。"""

    from src.autoslice.final_review_auditor import persist_review_audit
    from src.autoslice.final_review_contract import (
        validate_final_review_release,
    )

    sealed = build_unreadable_cue_drop_audit(
        passes=passes,
        status="PASS",
        final_srt_sha256=expected_srt_sha256,
    )
    chat_authority_audit["unreadable_cue_drops"] = sealed
    audit["unreadable_cue_drops"] = sealed
    validate_final_review_release(
        audit, expected_srt_sha256=expected_srt_sha256
    )
    persist_review_audit(review_audit_path, audit)
    # 旁车回执同时是 维护者 的审阅入口和 free 容量清理的引用锚点：produce 成功
    # 收尾时 summary 尾窗会截断，runner 只信落盘文件（speaker_guess 同款理由）。
    drop_sidecar_path(recut.subtitle_path.parent, cid).write_text(
        _json_text(sealed), encoding="utf-8"
    )
    if recut.redelivery_baseline_audit is not None:
        recut.redelivery_baseline_audit[
            "post_unreadable_cue_drop_output_sha256"
        ] = expected_srt_sha256.removeprefix("sha256:")
        recut.redelivery_baseline_audit["unreadable_cue_drops"] = sealed
        if recut.redelivery_baseline_audit_path is not None:
            recut.redelivery_baseline_audit_path.write_text(
                _json_text(recut.redelivery_baseline_audit),
                encoding="utf-8",
            )
    return sealed
