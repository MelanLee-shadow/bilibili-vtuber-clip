"""何时重排一条"在等基础设施"的 talk 候选。

Ivan 2026-08-10 #9 逐字：「仅仅是一个CPA请求失败为什么会让整条候选判死…CPA
请求失败的逻辑是积极重试，而不是判候选死」。

"不判死"那一半 talk lane 早就有了：``provider_transient`` /
``runtime_prerequisite`` 属于 ``INFRASTRUCTURE_WAIT_FAILURE_KINDS``，永不落
``candidate_rejected``，到点必被 ``_talk_retry_decision`` 重排。缺的是**间隔**：
此前一律 ``SONG_INFRA_RETRY_BASE_SECONDS``（15 分钟）且不随次数增长，而一次
produce 要 15–55 分钟——于是一次多小时的上游故障里，每个候选都在"产完即刻重
产"，机时全烧在必然重复的失败上（2026-08-07 free state 实测：
``provider_transient`` 一天 8 条 superseded 尝试）。

本模块只决定"等多久"，不决定"要不要等"——可恢复性仍归 ``talk_lane`` 的分类面，
重排资格仍归 ``delivery_recovery._talk_retry_decision``。曲线本身不新造一条：
直接调用歌 lane 已在生产验证的 ``song_infra_retry_delay_seconds``（15m → 30m →
… → 6h 上限），两个 lane 早已共用 ``SONG_INFRA_RETRY_*`` 常量。**这是放慢重试，
不是放弃重试**：没有终止上限，故障持续多久就等多久。
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from src.autoslice import provider_failure as _provider_failure
from src.autoslice.runner_proxy import RunnerProxy

_runner = RunnerProxy()


def talk_infra_retry_delay_seconds(result: Mapping[str, object]) -> int:
    """这次失败之后应当等待的秒数。

    配额类失败直接从下一档起步：配额窗口按小时/天计（AGY 周配额尤甚），用 15
    分钟去撞一个还要几小时才开的窗口只是纯浪费一次整条重产。
    """

    completed_retries = int(result.get("talk_transient_retry_count") or 0)
    if result.get("failure_provider_class") == _provider_failure.QUOTA:
        completed_retries += 1
    return int(_runner.song_infra_retry_delay_seconds(completed_retries))


def talk_infra_retry_schedule(result: Mapping[str, object]) -> dict[str, object]:
    """``result`` 上要落盘的重排时刻三件套。

    ``next_retry_at_epoch`` 是 ``_talk_retry_decision`` 与
    ``scheduled_talk_retry_epoch`` 读的那一个字段；另外两个是给人看的。
    """

    delay = talk_infra_retry_delay_seconds(result)
    epoch = int(time.time()) + delay
    return {
        "retry_after_seconds": delay,
        "next_retry_at_epoch": epoch,
        "next_retry_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
    }


__all__ = ["talk_infra_retry_delay_seconds", "talk_infra_retry_schedule"]
