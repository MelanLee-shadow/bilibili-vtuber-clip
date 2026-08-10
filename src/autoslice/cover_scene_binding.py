"""Bind one clip's cover scene (talk vs game) to the evidence that exists.

Ivan 2026-08-09 02:20（逐字）：「事实上，如果是截图封面的话，当然不要求李豆沙在
画面里占主要部分，毕竟是游戏截图，只要截图足够有趣就行，主体肯定会会是游戏。」

这层只做"这一条是不是游戏场"的绑定，不新建任何探测器：

* 会话级 —— `src/autoslice/game_context.py` 已经在算的 `session-game-context.v1`
  （runner 通过 `bind_session_game_context` 绑进 `LIDOUSHA_SESSION_GAME_CONTEXT`）；
* 逐条 —— `src/autoslice/cover_frame_selection.py` 已经在算的
  `camera_window_bbox_frac`（固定位置的面捕小窗，正是"全屏游戏 + 角落小窗"版式）。

两条都拿到才是游戏场。任何缺失、读坏、schema 不合、status 非 RESOLVED，或者这一条
根本没有小窗（游戏场里的纯杂谈切片就是这样），都退回 talk——talk 面的提问、问卷与
回执形状逐字节不变，放宽面永远不会靠"猜"生效。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.cover_source_composition import (
    TALK_SCENE,
    resolve_cover_scene_kind,
    source_composition_scene_kind,
)


ENV_CONTEXT_PATH = "LIDOUSHA_SESSION_GAME_CONTEXT"
ENV_DISABLE = "LIDOUSHA_DISABLE_SESSION_GAME_CONTEXT"


def load_session_game_context() -> Mapping[str, object] | None:
    """Read the runner-bound session game context; fail-open to ``None``."""

    raw_path = os.environ.get(ENV_CONTEXT_PATH, "").strip()
    if not raw_path or os.environ.get(ENV_DISABLE) == "1":
        return None
    from src.autoslice.game_context import (
        GameContextError,
        validate_session_game_context,
    )

    try:
        path = Path(raw_path)
        if not path.is_file() or path.is_symlink():
            return None
        return validate_session_game_context(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, GameContextError):
        return None


def cover_scene_kind(frame_selection: Mapping[str, object] | None) -> str:
    """Classify one clip's cover scene from session + frame evidence."""

    return resolve_cover_scene_kind(
        session_game_context=load_session_game_context(),
        frame_selection=frame_selection,
    )


def source_composition_scene_kwargs(
    frame_selection: Mapping[str, object] | None,
) -> dict[str, str]:
    """Scene kwargs for the source-composition witness (empty on talk).

    talk 场**一个 kwarg 都不多传**：既有调用点、既有注入的 verifier double 与既有
    回执形状全部零影响，这就是保真钉的机器面。
    """

    scene = cover_scene_kind(frame_selection)
    return {} if scene == TALK_SCENE else {"scene_kind": scene}


def run_source_composition_witness(
    verifier,
    *,
    reference_path,
    reference_sha256: str,
    story_hook: str,
    title: str,
    base_url: str,
    api_key: str,
    frame_selection: Mapping[str, object] | None,
) -> dict[str, object]:
    """Invoke the source-composition witness under this clip's scene."""

    try:
        return dict(
            verifier(
                reference_path=reference_path,
                reference_sha256=reference_sha256,
                story_hook=story_hook,
                title=title,
                base_url=base_url,
                api_key=api_key,
                **source_composition_scene_kwargs(frame_selection),
            )
        )
    except Exception as exc:  # the caller fail-closes on any invalid receipt
        return {
            "status": "FAIL",
            "reason_code": "SOURCE_COMPOSITION_VERIFIER_EXCEPTION",
            "detail": f"{type(exc).__name__}: {exc}",
        }


def run_final_host_identity_witness(
    verifier,
    *,
    final_cover_path,
    final_cover_sha256: object,
    reference_path,
    base_url: str,
    api_key: str,
    cover_generation: Mapping[str, object],
) -> dict[str, object]:
    """Invoke the final host-identity gate under this cover's bound scene."""

    if verifier is None:
        return {"status": "FAIL", "reason_code": "HOST_IDENTITY_VERIFIER_MISSING"}
    return dict(
        verifier(
            final_cover_path=final_cover_path,
            final_cover_sha256=final_cover_sha256,
            reference_path=reference_path,
            base_url=base_url,
            api_key=api_key,
            **host_identity_scene_kwargs(cover_generation),
        )
    )


def host_identity_scene_kwargs(
    cover_generation: Mapping[str, object],
) -> dict[str, str]:
    """Scene kwargs for the final host-identity gate (empty on talk).

    终检的场景**不重新推导**，直接读那份已经 hash-bound 的 source-composition
    回执自称的 scene：终检必须问的是"这张成品是按哪套判据放行的"，重算一次会让
    env 或选帧在两步之间漂移时两端问不同的问卷。
    """

    scene = source_composition_scene_kind(
        cover_generation.get("source_composition_verification")
    )
    return {} if scene == TALK_SCENE else {"scene_kind": scene}
