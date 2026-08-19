"""终审结转的硬退出存活性（维护者 15:05Z 交棒清单第 7 项）。

交棒逐字：「硬退出丢 carryover(超时/崩溃跳过侧车落盘)」。

封存件只在整个 exact 终审门跑出结论时才写——那之前还有最多五轮自愈，每轮
都是一整遍声学 + LLM 扫描。runner 用 ``subprocess.run(timeout=5400)`` 派
produce，超时即 ``Popen.kill()``（SIGKILL），``finally`` 与信号处理器都救不
了；pass 内部被杀是物理事实，但**跨 pass**与**非 SystemExit 异常**这两条路
本来就该救得回来。checkpoint 就是那两条路的补丁：同源同谓词、零特权、原子
落盘、带完整性标记，且**只增不改**封存件。
"""

from __future__ import annotations

import json

import pytest

from src.autoslice import producer_text_pipeline
from src.autoslice.final_review_carryover import (
    CHECKPOINT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    carryover_checkpoint_path,
    carryover_path,
    checkpoint_final_review_carryover,
    load_final_review_carryover,
    load_replayable_final_review_carryover,
    persist_final_review_carryover,
)


def _audit(findings):
    return {"schema_version": "final-review-audit.v2", "findings": findings}


def _confirmed(cue, suspect, proposed, *, base=None):
    return {
        "cue_index": cue,
        "base_text_sha256": base or (f"{cue:x}" * 16)[:64],
        "kind": "context",
        "suspect": suspect,
        "replacement": proposed,
        "proposed_full_cue": proposed,
        "why": f"语境应是{proposed}",
        "exact_release_adjudication": {"repaired": True},
    }


def _rejected(cue, suspect, proposed):
    return {
        "cue_index": cue,
        "base_text_sha256": "f" * 64,
        "kind": "context",
        "suspect": suspect,
        "proposed_full_cue": proposed,
        "why": "keep-current",
        "exact_release_adjudication": {"repaired": False},
    }


# --- (a) 硬退出后下一轮仍消费得到 -------------------------------------------


def test_hard_exit_before_seal_still_hands_the_row_to_the_next_round(tmp_path):
    """SIGKILL 在 pass 之间落下：封存件从未被写，checkpoint 顶上。"""

    path = carryover_path(tmp_path, "auto_x")
    # pass 0 的终审确证了一条修复……
    assert checkpoint_final_review_carryover(path, _audit([_confirmed(32, "悄悄", "就去敲敲那些结晶")])) == 1
    # ……然后自愈第 2 轮撞上 5400s 墙被 SIGKILL：persist 永远没被调用。
    assert not path.exists()

    rows = load_replayable_final_review_carryover(path)
    assert [row["suspect"] for row in rows] == ["悄悄"]
    assert rows[0]["cue"] == 32
    assert "终审结转" in rows[0]["why"]


def test_checkpoint_accumulates_across_self_heal_passes(tmp_path):
    """每个 pass 只看得见自己的 findings；跨 pass 的确证必须叠加而不是互相顶掉。"""

    path = carryover_path(tmp_path, "auto_x")
    checkpoint_final_review_carryover(path, _audit([_confirmed(32, "悄悄", "敲敲结晶")]))
    checkpoint_final_review_carryover(path, _audit([_confirmed(51, "刮", "括一点")]))

    assert sorted(row["suspect"] for row in load_replayable_final_review_carryover(path)) == ["刮", "悄悄"]


def test_checkpoint_never_carries_a_row_the_seal_would_refuse(tmp_path):
    """同一个谓词：judge 裁 keep-current 的 finding 两边都不许落盘。"""

    path = carryover_path(tmp_path, "auto_x")
    audit = _audit([_rejected(52, "刮", "还能稍微刮一点")])

    assert checkpoint_final_review_carryover(path, audit) == 0
    assert not carryover_checkpoint_path(path).exists()
    assert persist_final_review_carryover(path, audit) == 0
    assert load_replayable_final_review_carryover(path) == []


def test_unexpected_exception_after_the_reviewer_returns_keeps_the_row(tmp_path):
    """非 SystemExit 异常绕过终审门的两处 persist——checkpoint 是唯一出口。"""

    path = carryover_path(tmp_path, "auto_x")

    def gate_pass(audit):
        checkpoint_final_review_carryover(path, audit)
        raise RuntimeError("EXACT_FINAL_GATE_CRASHED")

    with pytest.raises(RuntimeError):
        gate_pass(_audit([_confirmed(32, "悄悄", "敲敲结晶")]))

    assert [row["suspect"] for row in load_replayable_final_review_carryover(path)] == ["悄悄"]


# --- (b) 残缺件不被当完整件消费 ---------------------------------------------


def test_truncated_sidecar_is_absent_not_partially_consumed(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    persist_final_review_carryover(
        path, _audit([_confirmed(32, "悄悄", "敲敲结晶"), _confirmed(51, "刮", "括一点")])
    )
    whole = path.read_text(encoding="utf-8")

    path.write_text(whole[: len(whole) // 2], encoding="utf-8")
    assert load_final_review_carryover(path) == []
    assert load_replayable_final_review_carryover(path) == []


def test_row_dropped_from_a_parsable_payload_is_rejected_by_the_integrity_marker(tmp_path):
    """截断不一定破坏 JSON——数量/摘要对不上就当不存在，绝不半份消费。"""

    path = carryover_path(tmp_path, "auto_x")
    persist_final_review_carryover(
        path, _audit([_confirmed(32, "悄悄", "敲敲结晶"), _confirmed(51, "刮", "括一点")])
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["integrity"]["finding_count"] == 2
    payload["findings"] = payload["findings"][:1]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert load_final_review_carryover(path) == []
    assert load_replayable_final_review_carryover(path) == []


def test_damaged_checkpoint_never_hides_the_sealed_round(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    persist_final_review_carryover(path, _audit([_confirmed(32, "悄悄", "敲敲结晶")]))
    checkpoint_final_review_carryover(path, _audit([_confirmed(51, "刮", "括一点")]))
    carryover_checkpoint_path(path).write_text("{", encoding="utf-8")

    assert [row["suspect"] for row in load_replayable_final_review_carryover(path)] == ["悄悄"]


def test_checkpoint_is_never_read_as_a_sealed_round(tmp_path):
    """同轮重放（producer_package_finalization）走 sealed-only 通道，不得看见在途件。"""

    path = carryover_path(tmp_path, "auto_x")
    checkpoint_final_review_carryover(path, _audit([_confirmed(32, "悄悄", "敲敲结晶")]))

    assert load_final_review_carryover(path) == []
    assert (
        json.loads(carryover_checkpoint_path(path).read_text(encoding="utf-8"))[
            "schema_version"
        ]
        == CHECKPOINT_SCHEMA_VERSION
    )
    # persist 的 prior_rows 也只认封存件：在途件不得冒充「上一轮未消费」。
    assert (
        persist_final_review_carryover(
            path,
            {
                **_audit([]),
                "status": "AUDITOR_UNAVAILABLE",
                "correction_pass": {
                    "status": "AUDITOR_UNAVAILABLE",
                    "discovery": {"status": "AUDITOR_UNAVAILABLE"},
                },
            },
        )
        == 0
    )


def test_sealed_round_outranks_an_interrupted_one_on_the_same_row(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    row = _confirmed(32, "悄悄", "敲敲结晶")
    persist_final_review_carryover(path, _audit([row]))
    checkpoint_final_review_carryover(path, _audit([{**row, "why": "在途版本"}]))

    rows = load_replayable_final_review_carryover(path)
    assert len(rows) == 1
    assert "在途版本" not in rows[0]["why"]


# --- (c) 正常路径行为不变 ---------------------------------------------------


def test_legacy_production_sidecar_without_the_marker_still_loads(tmp_path):
    """free 上现存的 v1 侧车（无 integrity 块）必须照旧可读。"""

    path = carryover_path(tmp_path, "auto_213743_1635_1717")
    path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "findings": [
                    {
                        "cue": 27,
                        "base_text_sha256": "a" * 64,
                        "suspect": "早上",
                        "proposed_full_cue": "谢谢如果世上没有早……",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    assert len(load_final_review_carryover(path)) == 1
    assert len(load_replayable_final_review_carryover(path)) == 1


def test_clean_round_clears_both_the_seal_and_its_checkpoint(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    persist_final_review_carryover(path, _audit([_confirmed(32, "悄悄", "敲敲结晶")]))
    checkpoint_final_review_carryover(path, _audit([_confirmed(51, "刮", "括一点")]))

    assert persist_final_review_carryover(path, _audit([])) == 0
    assert not path.exists()
    assert not carryover_checkpoint_path(path).exists()
    assert load_replayable_final_review_carryover(path) == []


def test_no_checkpoint_means_the_consumer_sees_exactly_the_sealed_round(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    persist_final_review_carryover(
        path, _audit([_confirmed(32, "悄悄", "敲敲结晶"), _confirmed(51, "刮", "括一点")])
    )

    assert load_replayable_final_review_carryover(path) == load_final_review_carryover(path)


# --- 原子落盘：被杀在写的一半不许毁掉上一轮 -----------------------------------


def test_interrupted_write_leaves_the_previous_round_intact(tmp_path, monkeypatch):
    """``Path.write_text`` 原地截断——写到一半被杀就把上一轮未消费的行一起毁了。"""

    import src.autoslice.final_review_carryover as module

    path = carryover_path(tmp_path, "auto_x")
    persist_final_review_carryover(path, _audit([_confirmed(32, "悄悄", "敲敲结晶")]))
    before = path.read_bytes()

    def killed(*_args, **_kwargs):
        raise KeyboardInterrupt("SIGKILL 等价：落盘途中进程消失")

    monkeypatch.setattr(module.os, "replace", killed)
    with pytest.raises(KeyboardInterrupt):
        persist_final_review_carryover(path, _audit([_confirmed(51, "刮", "括一点")]))

    assert path.read_bytes() == before
    assert load_final_review_carryover(path)[0]["suspect"] == "悄悄"


def test_successful_write_leaves_no_temp_residue(tmp_path):
    path = carryover_path(tmp_path, "auto_x")
    persist_final_review_carryover(path, _audit([_confirmed(32, "悄悄", "敲敲结晶")]))
    checkpoint_final_review_carryover(path, _audit([_confirmed(51, "刮", "括一点")]))

    assert [p.name for p in tmp_path.glob("*.tmp")] == []


# --- 接线：删掉调用点这两条必须红 --------------------------------------------


def test_exact_final_reviewer_checkpoints_before_it_returns():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(producer_text_pipeline.run_text_pipeline))
    reviewer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "review_exact_final_srt"
    )
    calls = [
        node
        for node in ast.walk(reviewer)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        in {"_run_exact_final_release_review", "checkpoint_final_review_carryover"}
    ]
    assert [node.func.id for node in calls] == [
        "_run_exact_final_release_review",
        "checkpoint_final_review_carryover",
    ]


def test_correction_pass_consumes_the_replayable_carryover():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(producer_text_pipeline._run_final_review))
    loaders = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "load_replayable_final_review_carryover" in loaders
    assert "load_final_review_carryover" not in loaders
