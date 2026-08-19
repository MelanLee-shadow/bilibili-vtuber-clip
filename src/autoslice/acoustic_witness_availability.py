"""声学证人「可达性」契约（F21，维护者 直令）。

维护者 8/10：「wsl 上有 gemini key，没有 AGY 当然要 fall back 到 gemini key」。
配套 7/19 既有裁定：AGY 订阅 → 免费 3 key → 付费 backup 只是**同一 Gemini
模型的配额顺序**，按 provider 层拒证据的门 = 过度限制。

本模块只放两件小事，它们是 F21 之前把 wsl 产线的声学证人链整条打死的两个
纯接线缺陷：

1. **host 门谓词**：旧门问「``--ssh-host`` 是不是 localhost」。produce 姿势下
   ``prepare_source_media`` 已把 piece/padded 拉到**本机** out 目录，远端 host
   只决定源素材在哪切，不决定证人的音频窗在哪。于是 wsl 产线（host=free）
   恒定拿不到证人，链在第一步就断。
2. **typed UNCERTAIN 尾巴**：provider 全竭（或根本没有 provider）时旧代码下传
   裸 ``None``，下游 ``dict(None)`` 抛 TypeError、证词直接 invalid，法官永远
   不跑，findings 全 UNCERTAIN，``FINAL_REVIEW_UNRESOLVED_FINDINGS`` 拦死。

尾巴的 reason_code 是**专用**的：``AUDIO_VERIFIER_UNAVAILABLE`` 表示「本轮
证人链从未真正听过这段音频」，与 ``ENTITY_AUDIO_PROVIDER_FAILED``（provider
确实被调用过并失败，7/25 起既有语义）严格区分。裁决层据此把无声学改字路
在这一类证词上封死——见 ``acoustic_witness_adjudication.adjudicate_with_witness``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from src.autoslice.acoustic_witness_protocol import BLIND_PINYIN_PROTOCOL

WITNESS_SCHEMA = "subtitle-span-acoustic-witness.v1"
AUDIO_VERIFIER_UNAVAILABLE = "AUDIO_VERIFIER_UNAVAILABLE"


def witness_audio_locally_resolvable(padded: Path, *, host: str) -> bool:
    """证人所需的音频窗此刻在本地是否可解析（新 host 门谓词）。

    解析不到才降级；降级后调用方走 ``unavailable_acoustic_witness``，绝不再
    下传裸 ``None``。
    """

    if host in {"localhost", "127.0.0.1"}:
        return True
    try:
        return padded.is_file() and padded.stat().st_size > 0
    except OSError:
        return False


def unavailable_acoustic_witness(
    witness_request: Mapping[str, Any],
) -> dict[str, Any]:
    """无声学 provider 可达时唯一合法的、hash 绑定的 typed 证词。"""

    return {
        "schema_version": WITNESS_SCHEMA,
        "witness_protocol": BLIND_PINYIN_PROTOCOL,
        "request_sha256": witness_request.get("request_sha256"),
        "status": "UNCERTAIN",
        "reason_code": AUDIO_VERIFIER_UNAVAILABLE,
    }
