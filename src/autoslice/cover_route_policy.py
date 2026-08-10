"""Per-clip cover route policy (screenshot direct / polish / CPA redraw).

抽自 `publish_staging.py`（2026-08-10 竖屏判据接线时）：路由是**纯决策**——只吃
已有回执（选帧分数、见证 verdict、源帧几何、梗字终审状态），不做 I/O、不碰产物。
把它单独成模块的直接原因是那份 god-file 的行数账本只准还不准欠，而这条判据的
新增重量本该落在策略层；顺带让"为什么这一条走了这条路线"能被单文件读完。

调用方 `publish_staging._decide_cover_treatment` 是本模块 `decide_cover_treatment`
的别名，保持既有 patch 面与调用点不变。
"""

from __future__ import annotations

from collections.abc import Mapping

from .cover_source_composition import (
    GAME_SCENE,
    VERTICAL_SOURCE_MIN_ASPECT_RATIO,
    source_composition_recommends_redraw,
    source_composition_scene_kind,
    source_composition_supports_subject,
    vertical_source_redraw_reason,
)


_COVER_TREATMENT_SCORE_HI = 4.5
_COVER_TREATMENT_SCORE_LO = 2.6
_COVER_SUBJECT_MAX_MOTION_DISPERSION = 0.50


def decide_cover_treatment(
    *,
    cover_mode: str,
    is_song: bool,
    punch_allowed: bool,
    frame_selection: Mapping[str, object] | None,
    verified_stream_frame: bool = False,
    relationship_visual_required: bool = False,
    relationship_source_verified: bool = False,
    thumbnail_text_requires_punch: bool = False,
    punch_semantic_status: str = "",
    source_composition_verification: Mapping[str, object] | None = None,
    source_frame_size: object = None,
    vertical_min_aspect_ratio: float = VERTICAL_SOURCE_MIN_ASPECT_RATIO,
) -> tuple[str, str]:
    """每条切片选封面路线（2026-07-21 Ivan：哪些适合全图 CPA 重做、哪些适合截图）。

    判据=表现力选帧最高分（"这条片有没有值得原样示人的真名场面"）：
    - 竖屏源 → cpa_redraw（Ivan 2026-08-10：竖屏直播通常不适合截图，只能重绘）
    - 歌切 / 选帧失败 → cpa_redraw（唱歌净美学 / 无帧可用）
    - 强名场面（≥4.5，或 ≥3.2 且命中情绪字幕段）→ screenshot_direct：真表情就是
      封面，重绘反而丢梗
    - 中等瞬间（≥2.6）→ screenshot_polish：保真帧构图，CPA 只清杂物修画质
    - 更低 → cpa_redraw：没有好瞬间，插画重做的承载力更强
    阈值标定自 2026-07-15/19 十七条本地成片修掉片头假峰后的分数分布
    （强：5.1-9.1；中：2.8-3.9；弱：1.9-2.1）。
    """

    scene_kind = source_composition_scene_kind(source_composition_verification)
    if cover_mode == "cpa":
        return "cpa_redraw", "mode=cpa (forced)"
    if is_song:
        return "cpa_redraw", "song keeps the clean CPA aesthetic"
    # 竖屏源（Ivan 2026-08-10 逐字裁定：「并不是所有的都需要截图，特别是竖屏直播，
    # 通常不适合截图，只能重绘。」）。16:9 成品窗口在竖版源上要么对准形心从眼睛处
    # 切断（auto_230125_960_1072，源 1920×3414），要么两侧大片模糊填充。判据必须排
    # 在关系分支/hash-bound 见证分支**之前**：那两条会无条件 return
    # screenshot_direct，放在后面等于对恢复重放整条失效——而恢复重放正是事故现场。
    vertical_reason = vertical_source_redraw_reason(
        source_frame_size,
        min_ratio=vertical_min_aspect_ratio,
    )
    if vertical_reason is not None:
        return "cpa_redraw", vertical_reason
    if relationship_visual_required:
        if cover_mode == "polish":
            return "screenshot_polish", "mode=polish (forced)"
        if relationship_source_verified:
            return (
                "screenshot_direct",
                "hash-bound source frame verifies all required participants",
            )
        return (
            "screenshot_direct",
            "relationship hook requires source-verified participants before composition scoring",
        )
    if verified_stream_frame:
        return (
            "screenshot_direct",
            "hash-bound source frame verifies all required participants",
        )
    # 2026-07-31：**几何否决**排在关系分支之后（witness 的提问是单人框架——只定位
    # 李豆沙、问"她能否裁成大主体"——双人同框里她天然不独占画面，
    # faithful_crop_can_make_dominant 很容易 false，与 70-cover.md 的
    # 「双人联动即使运动分数不高也可优先保留真实互动」直接冲突）。
    if source_composition_recommends_redraw(source_composition_verification):
        return (
            "cpa_redraw",
            "CPA source-composition witness reports the face is cut or cannot "
            "become the dominant subject",
        )
    # witness 不再无条件替换路由。此前它是 Mapping 就直接 return，导致下方整套
    # 标定阈值（4.5 / 2.6 / 0.50 弥散 / camera window）在有 witness 时**完全不可达**
    # ——Ivan 2026-07-21 拍板的名场面分路由被整体退役，封面路线退化成一次 CPA 二值
    # 判断。现在它降级为**置信输入**（见下方 subject_confident 的合成），
    # 标定分数恢复决定权。
    if frame_selection is None:
        return "cpa_redraw", "frame selection unavailable"
    candidates = frame_selection.get("candidates") or []
    best = float(candidates[0]["score"]) if candidates else 0.0
    emotional = bool(candidates and candidates[0].get("emotion"))
    # A narrow local motion box is not sufficient proof of a usable cover
    # subject when the rest of the scene is moving across most of the canvas.
    # The 2026-07-22 game-UI miss reported a plausible local box but a 0.6033
    # accumulated-motion footprint; screenshot polish consequently preserved a
    # mostly empty game panel with tiny avatars in one corner.  Prefer the CPA
    # big-face redraw whenever global motion is that dispersed.  Missing legacy
    # evidence remains compatible with the earlier subject-confidence gate.
    raw_dispersion = frame_selection.get("motion_dispersion_frac")
    try:
        motion_dispersion = float(raw_dispersion) if raw_dispersion is not None else None
    except (TypeError, ValueError):
        motion_dispersion = None
    # 置信 = 运动几何置信 **或** CPA witness 给出的合法主体证据（2026-07-31）。
    #
    # 几何置信在 Live2D 皮套画面上不可靠：7/24-7/29 实测 23 条有效样本里
    # `subject_confident` 探测器自己给 False 的有 16 条（70%），弥散帽 0.50 又把
    # 22 个实测值里的 9 个（41%）挡在外面——那个帽是 2026-07-22 一次游戏 UI 事故
    # （实测 0.6033）往下取的 n=1 标定，落在生产分布正中间，不是在切病态尾巴。
    # 结果 11 条切片在分数最高 9.0027 的情况下没进任何截图分支就被判重绘
    # （auto_202004_553_831：flag True、disp 0.5271，超帽 0.027）。
    #
    # 弥散帽保留但**只约束运动几何这个来源**（它 7/22 的原始射程）；witness 在场时
    # 合法点集由 CPA 视觉给定，不适用该帽。放宽路由不放宽验收——下游
    # polish_face_verification v2 与 FINAL_COVER_SUBJECT_PROMINENCE_FAILED
    # 仍会拒掉真正不合格的成品。
    geometry_confident = frame_selection.get("subject_confident") is True and (
        motion_dispersion is None or motion_dispersion <= _COVER_SUBJECT_MAX_MOTION_DISPERSION
    )
    subject_confident = geometry_confident or source_composition_supports_subject(
        source_composition_verification
    )
    thumbnail_punch_unavailable = (
        thumbnail_text_requires_punch
        and punch_semantic_status in {"FAILED", "NOT_APPLICABLE"}
    )
    forced_subject_unverified = (
        cover_mode in {"screenshot", "polish"} and not subject_confident
    )
    if thumbnail_punch_unavailable and forced_subject_unverified:
        return (
            "cpa_redraw",
            (
                f"mode={cover_mode} preference cannot authorize an unverified "
                "cover subject; thumbnail punch unavailable and full title is "
                "not a readable 1-2 line hook"
            ),
        )
    if thumbnail_punch_unavailable:
        return (
            "cpa_redraw",
            "thumbnail punch unavailable; full title is not a readable 1-2 line hook",
        )
    if forced_subject_unverified:
        return (
            "cpa_redraw",
            f"mode={cover_mode} preference cannot authorize an unverified cover subject",
        )
    if cover_mode == "screenshot":
        return "screenshot_direct", "mode=screenshot (forced)"
    if cover_mode == "polish":
        return "screenshot_polish", "mode=polish (forced)"
    if subject_confident and (best >= _COVER_TREATMENT_SCORE_HI or (emotional and best >= 3.2)):
        return "screenshot_direct", f"strong real moment (score={best:.2f})"
    if subject_confident and best >= _COVER_TREATMENT_SCORE_LO:
        return "screenshot_polish", f"usable moment + CPA touch-up (score={best:.2f})"
    if best >= _COVER_TREATMENT_SCORE_LO:
        # 游戏场小窗回归。**出处据实**：「截图修图优先于重绘」是 2026-07-25 03:34
        # 助手对 Ivan 提问的回答，不是 Ivan 的裁定；Ivan 当场没有反对，并在 03:38
        # 逐字授权了配套修复（「你可以现在开始做小窗裁剪」）。2026-08-09 02:20 另有
        # 逐字裁定：游戏截图封面不要求她占画面主要部分。2026-08-10：本分支在见证
        # 在场（正常 talk 生产恒真）时曾**逻辑不可达**——faithful_crop=false 在上面
        # 提前 return，=true 又把 subject_confident 抬成 True；游戏场受证
        # `supports_subject()==False` 之后它才第一次真正可达。
        if frame_selection.get("camera_window_bbox_frac"):
            if scene_kind == GAME_SCENE:
                lead = "game scene: host camera window visible; full game frame"
                return "screenshot_polish", f"{lead} + CPA touch-up (score={best:.2f})"
            return (
                "screenshot_polish",
                f"camera window crop + CPA touch-up (score={best:.2f})",
            )
        return "cpa_redraw", f"motion without confident cover subject (score={best:.2f})"
    return "cpa_redraw", f"no strong real moment (score={best:.2f})"
