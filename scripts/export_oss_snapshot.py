#!/usr/bin/env python3
"""Export a clean open-source snapshot of the pipeline (v4, Ivan 2026-08-01 rules).

原则：产品只有产品。代码/产品文档/词表等劳动成果保留；真值台账、出版登记、
恢复权威、评审基线等**运营状态**剔除但保模板；开发过程记录（reports、
HANDOFF、reviews、spark、.agent、一次性事故脚本）剔除。未分类的新路径直接
报错——再导出时新增文件必须显式归类，防止未来无意泄漏。

v4 新增（profile 分离 + 发布整备）：
- RENAME_STEMS：流程面文件名/内部符号去频道名与私有主机名（精确 token 全树
  重写，两侧一致故不破坏等式；assets/ 不重写以保资产字节，仅 profiles/*.json
  跟随工具指针）。
- PATCHES：逐文件精确手术（PII、私有拓扑、死引用、运营叙事）；old 文本找不到
  或次数不符即导出失败，逼着改动跟着上游走。
- assets/_template/：从默认 profile 结构自动派生的最小骨架（empty_entries 同
  构变换 + 占位文本），消除新 profile 的 22 文件悬崖。
- rootmap 显式优先：docs/oss/** 最后落盘，覆盖同名 tracked 文件（.gitignore、
  README 等 OSS 版以 docs/oss 为准）。
- 脱敏修复：CJK 紧邻的维护者本名（如「待Ivan确认」）此前逃过 \\b 边界；改用
  lookaround。禁词扫描同步修复并追加私有邮箱/主机痕迹与旧 stem 全量。

导出后跑禁词扫描（私有身份/主机名/密钥形状/死引用/旧 stem），命中即失败。

重建 OSS 仓的规范身份（勿用真实邮箱——提交元数据是明文公开的；项目账号已
迁移至 MelanLee-shadow，见私库 6f9fc23）：
  git -c user.name=MelanLee-shadow \
      -c user.email=311905209+MelanLee-shadow@users.noreply.github.com \
      commit ...
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO.parent / "bilibili-vtuber-clip"

STRIP_PREFIXES = (
    "reports/",
    "docs/reviews/",
    "docs/spark/",
    "docs/history/",
    "docs/workflows/",
    "docs/HANDOFF.md",
    "docs/handoff-",
    "AGENTS.md",   # 私库操作者规则；OSS 版由 docs/oss/AGENTS.md 提供
    "CLAUDE.md",
    "scripts/export_oss_snapshot.py",          # 私库导出工具，不随 OSS 分发
    "ops/blrec-patches/",                      # blrec 已退役
    "tests/test_blrec_live_watchdog.py",       # 孤儿测试(模块已剔)
    "tests/test_patch_autoslice_runner_recorder_status.py",
    "tests/test_repair_false_green_20260709.py",
    "tests/test_authorized_upload.py",  # 运营态耦合测试
    "tests/test_auto_review_shadow_pipeline.py",  # 运营态耦合测试
    "tests/test_batch_speaker_review.py",  # 运营态耦合测试
    "tests/test_blrec_patches.py",  # 运营态耦合测试
    "tests/test_branding_intro.py",  # 运营态耦合测试
    "tests/test_build_lidousha_recovery_review_manifest.py",  # 运营态耦合测试
    "tests/test_channel_profile.py",  # 运营态耦合测试
    "tests/test_clip_context.py",  # 运营态耦合测试
    "tests/test_cover_reference_authority.py",  # 运营态耦合测试
    "tests/test_final_human_review.py",  # 运营态耦合测试
    "tests/test_free_session_autoslice.py",  # 运营态耦合测试
    "tests/test_lidousha_review_package_audit.py",  # 运营态耦合测试
    "tests/test_manual_title_repair_authority.py",  # 运营态耦合测试
    "tests/test_produce_slice_boundary.py",  # 运营态耦合测试
    "tests/test_producer_package_finalization.py",  # 运营态耦合测试
    "tests/test_publication_registry.py",  # 运营态耦合测试
    "tests/test_publish_staging_recovery_title.py",  # 运营态耦合测试
    "tests/test_recovery_planner_contract.py",  # 运营态耦合测试
    "tests/test_recovery_review_rerun.py",  # 运营态耦合测试
    "tests/test_recovery_title_authority.py",  # 运营态耦合测试
    "tests/test_repair_reviewed_covers.py",  # 运营态耦合测试
    "tests/test_review_package_title_audit.py",  # 运营态耦合测试
    "tests/test_reviewed_subtitle_baseline_registry.py",  # 运营态耦合测试
    "tests/test_semantic_candidate_selector.py",  # 运营态耦合测试
    "tests/test_session_relation_authority.py",  # 运营态耦合测试
    "tests/test_source_subtitle_truth.py",  # 运营态耦合测试
    "tests/test_speaker_finalizer.py",  # 运营态耦合测试
    "tests/test_subtitle_regression.py",  # 运营态耦合测试
    "tests/test_subtitle_text_overrides.py",  # 运营态耦合测试
    "tests/test_timely_terms.py",  # 运营态耦合测试
    "tests/test_title_policy.py",  # 运营态耦合测试
    "tests/test_upload_tag_policy.py",  # 运营态耦合测试
    "ops/recording/blrec_live_watchdog.py",
    "ops/recording/patch_autoslice_runner_recorder_status.py",  # blrec 迁移遗物
    "cleanup_manifests/",          # 运营清理台账
    "docs/audit-uploads-",          # 上传审计=运营记录
    "docs/pending-provisional-",    # 待办清单=运营记录
    "docs/plan-",                   # 开发计划=过程记录
    "docs/autoslice-capability-status",  # 状态快照=过程记录
    "docs/remote-first-autoslice-route.md",  # 私有主机拓扑
    "lidousha/",                    # 旧布局运营产物+遗留词表(已被assets取代)
    # ---- v4：开发过程记录（一次性事故脚本）不进发布 ----
    "scripts/repair_false_green_20260709.py",   # 2026-07-09 事故专用取证工具
    "scripts/launch_v15_recovery_once.sh",      # 2026-07-22 V15 一次性恢复 lane
    "tests/test_v15_recovery_launcher.py",      # 上者的耦合测试
    "scripts/regen_covers_latest_flow.py",      # 7/3 成品一次性重出封面
    "prompts/lidousha_videocaptioner_prompt.txt",  # 零引用孤儿(早于 CPA 词表链)
)

# 运营状态 → 模板化（内容剔除，形状保留）。值=模板生成器名。
TEMPLATE_FILES = {
    "assets/lidousha/publication_registry.v1.json": "registry",
    "assets/lidousha/subtitle_truth_ledger.v1.json": "truth_ledger",
    "assets/lidousha/final_media_review_contracts.v1.json": "review_contracts",
    "assets/lidousha/recovery_publication_authority.v1.json": "rpa_index",
    "assets/lidousha/session_relation_ledger.v1.json": "empty_entries",
    "assets/lidousha/speech_memory_ledger.v1.json": "empty_entries",
    "assets/lidousha/published_songs.v1.json": "empty_entries",
    "assets/lidousha/manual_title_overrides.v1.json": "empty_entries",
    "assets/lidousha/manual_archive_metadata.v1.json": "empty_entries",
    "assets/lidousha/cover_reference_overrides.v1.json": "empty_entries",
    "assets/lidousha/selection_score_calibration.v1.json": "keep",  # 标定=劳动成果
    "assets/lidousha/timely_terms.json": "empty_entries",  # 运行时缓存
}

# 整目录运营状态：剔除全部内容，保目录+模板说明。
TEMPLATE_DIRS = (
    "assets/lidousha/recovery_publication_authority_",  # 前缀匹配单文件们
    "assets/lidousha/reviewed_subtitle_baselines/",
    "assets/lidousha/subtitle_regressions/",
    "assets/lidousha/subtitle_text_overrides/",
    "assets/lidousha/subtitle_overrides/",
    "assets/lidousha/speaker_overrides/",
    "assets/lidousha/speaker_session_anchors/",
    "assets/lidousha/speaker_batch_plans/",
    "assets/lidousha/manual_title_repair_authorities/",
    "assets/lidousha/cover_repair_plans/",
    "assets/lidousha/talk_recoveries/",
    "assets/lidousha/voiceprint_profile.v1.json",  # 生物特征，绝不发布
)

# ---------------------------------------------------------------------------
# v4：流程面去频道名/去私有主机名。精确 token 全树重写（文件名 + 文本内容），
# 长 token 优先避免子串误伤。assets/** 不重写（资产字节权威）；profiles/**
# 参与内容重写（跟随 tools.cover_regenerator 等指针）。
# 保留不动（功能词汇，见 docs/profile-coupling.md）：lidousha_centrality、
# lidousha_role、human_reviewed_lidousha、verified_lidousha_voiceprint、
# `lidousha-*.v1` schema 串、assets/lidousha、profiles/lidousha、
# free_asr_client（此 free=免费）、llm_via_free_groq（同）。
RENAME_STEMS: tuple[tuple[str, str], ...] = (
    # scripts（含同名测试文件、docs、skills、pyproject 引用）
    ("build_lidousha_recovery_review_manifest", "build_recovery_review_manifest"),
    ("build_lidousha_cover_only_audit_scope", "build_cover_only_audit_scope"),
    ("build_lidousha_daily_review_manifest", "build_daily_review_manifest"),
    ("build_lidousha_song_review_manifest", "build_song_review_manifest"),
    ("build_lidousha_final_human_review", "build_final_human_review"),
    ("lidousha_auto_review_shadow_daemon", "auto_review_shadow_daemon"),
    ("audit_lidousha_review_package", "audit_review_package"),
    ("install_lidousha_voiceprints", "install_voiceprints"),
    ("regenerate_lidousha_cover", "regenerate_channel_cover"),  # 模板早已指向此名
    ("lidousha_glossary_terms", "profile_glossary_terms"),
    ("lidousha_slice_monitor", "slice_monitor"),
    ("lidousha-slice-monitor", "slice-monitor"),  # launchd label 连字符变体
    ("lidousha_monitor", "slice_monitor"),        # ~/.config/<name>/smtp.json
    ("sync_lidousha_assets", "sync_profile_assets"),
    ("free_session_autoslice", "session_autoslice"),
    ("deploy_free_autoslice", "deploy_autoslice"),
    ("free_do_upload", "do_upload"),
    ("free_mount_watchdog", "mount_watchdog"),
    ("free_silero_vad_spans", "silero_vad_spans"),
    ("lidousha-title-style", "title-style"),      # skill 目录
    ("lidousha-auto-review-architecture", "auto-review-architecture"),  # doc
    # src 内部符号（值已走 profile，仅名字带频道；两侧一致重写）
    ("_write_lidousha_sapphire_ass_from_srt", "_write_sapphire_ass_from_srt"),
    ("_lidousha_cover_art_direction", "_cover_art_direction"),
    ("_overlay_lidousha_cover_title", "_overlay_cover_title"),
    ("verify_lidousha_final_host_identity", "verify_final_host_identity"),
    ("verify_lidousha_source_composition", "verify_source_composition"),
    ("_stage_lidousha_ai_cover", "_stage_ai_cover"),
    ("_lidousha_cover_prompt", "_cover_prompt"),
    ("_lidousha_cover_text", "_cover_text"),
    ("_default_lidousha_profile", "_default_style_profile"),
    ("_ensure_lidousha_prefix", "_ensure_title_prefix"),
    ("_LIDOUSHA_TITLE_PREFIX", "_TITLE_PREFIX"),
    ("_lidousha_fontsdir", "_profile_fontsdir"),
    ("lidousha_phonetic_ratio", "host_phonetic_ratio"),
)

# 不参与 stem 重写的路径前缀（资产字节权威；profile manifest 例外参与）。
REWRITE_SKIP_PREFIXES = ("assets/",)

# ---------------------------------------------------------------------------
# v4：逐文件精确手术。路径为**重写后的 OSS 路径**；old 必须精确出现 count 次，
# 否则导出失败（上游变了必须回来改这张表，不允许静默漂移）。
# 类别：PII / 私有拓扑 / 死引用 / 运营叙事进规则文档。
PATCHES: tuple[tuple[str, str, str], ...] = (
    # --- PII：私人告警邮箱（邮件通道本就 disabled，置空为纯数据变更） ---
    (
        "scripts/slice_monitor.py",
        'EMAIL_TO = "hfnkzjbsbm@privaterelay.appleid.com"',
        'EMAIL_TO = ""  # 收件人由 smtp.json 的 to 字段提供；留空禁用兜底',
    ),
    # --- 死引用：已剥离的 HANDOFF 运维文档 ---
    (
        "scripts/slice_monitor.py",
        '"fix": "旧管线 2026-07-10 已退役（HANDOFF 有据）。若无人在调试，"',
        '"fix": "旧管线 2026-07-10 已退役。若无人在调试，"',
    ),
    (
        "scripts/slice_monitor.py",
        '"未自愈按 HANDOFF 2026-07-09 手动修挂载并重启 bilive_record。"})',
        '"未自愈需人工修复挂载并重启 bilive_record。"})',
    ),
    # --- 死引用：已剥离的 docs/spark runbook ---
    (
        "scripts/session_autoslice.py",
        "# command per docs/spark/2026-06-30-future-live-e2e-runbook.md.  The selector",
        "# command per the retired live-e2e runbook.  The selector",
    ),
    # --- 私有拓扑+死脚本名：member API 开头叙事 ---
    (
        "src/autoslice/bilibili_member_api.py",
        "此前 view/编辑/换封面/换源/入合集散落在 free 上的一次性脚本里\n"
        "（edit_replace_20260714.py、cover_only_edit.py、bandfan_finish.py …），\n"
        "cookie 格式、端点、编码的坑每写一次踩一次。",
        "此前 view/编辑/换封面/换源/入合集散落在宿主机的一次性脚本里，\n"
        "cookie 格式、端点、编码的坑每写一次踩一次。",
    ),
    # --- 死脚本名：tags 工具 docstring ---
    (
        "scripts/bili_update_tags.py",
        "Modeled on bili_replace_covers.py (2026-07-05 proven cover-only edits on this\naccount)",
        "Modeled on the earlier proven cover-only edit lane (member edits on this\naccount)",
    ),
    # --- 频道身份数据出脚本进环境：上传简介/tag 兜底 ---
    (
        "scripts/do_upload.sh",
        '# $4 = manifest 冻结的完整 tag 行（authorized_upload.py 传入, 含基础位; 上限12\n'
        '# 已实测）。无 tags 的旧 manifest 回退基础4位（维护者 2026-07-13 口径, 砍 VUP/VTuber）。\n'
        'TAGS="${4:-李豆沙,虚拟主播,虚拟UP主,直播切片}"\n'
        'DESC="李豆沙个人主页：https://space.bilibili.com/1703797642\n'
        '李豆沙直播间：https://live.bilibili.com/22966160"',
        '# $4 = manifest 冻结的完整 tag 行（authorized_upload.py 传入, 含基础位; 上限12\n'
        '# 已实测）。无 tags 的旧 manifest 回退 AUTOSLICE_FALLBACK_TAGS。\n'
        '# 频道简介/tag 兜底是频道身份数据：来自环境变量（见 .env.example），不硬编码。\n'
        'TAGS="${4:-${AUTOSLICE_FALLBACK_TAGS:-虚拟主播,直播切片}}"\n'
        'DESC="${AUTOSLICE_UPLOAD_DESC:-}"',
    ),
    # --- 频道身份数据出脚本进环境：authorized_upload 的简介常量（同 do_upload） ---
    (
        "scripts/authorized_upload.py",
        'SUBMISSION_DESCRIPTION = (\n'
        '    "李豆沙个人主页：https://space.bilibili.com/1703797642\\n"\n'
        '    "李豆沙直播间：https://live.bilibili.com/22966160"\n'
        ')',
        '# 稿件简介是频道身份数据：来自环境变量（见 .env.example），不硬编码。\n'
        'SUBMISSION_DESCRIPTION = os.environ.get("AUTOSLICE_UPLOAD_DESC", "")',
    ),
    # --- 死引用：huozi skill 指向未随仓分发的 workflow 文档 ---
    (
        ".agent/skills/huozi-luanshua/SKILL.md",
        "Read `../../../docs/workflows/huozi-luanshua.md` before running the workflow. "
        "Use `scripts/huozi_luanshua.py`;",
        "Use `scripts/huozi_luanshua.py`;",
    ),
    # --- 死引用：intro 资产 provenance 指向未随仓分发的 workflow 文档 ---
    (
        "assets/lidousha/intro/branding_intro.v1.json",
        '"workflow": "docs/workflows/huozi-luanshua.md",',
        '"workflow": ".agent/skills/huozi-luanshua/SKILL.md",',
    ),
    # --- 债务棘轮：authorized_upload 简介常量外化后少 2 行，账本同步收紧 ---
    (
        "tests/test_runtime_architecture.py",
        '"scripts/authorized_upload.py": 2_929,',
        '"scripts/authorized_upload.py": 2_927,',
    ),
    # --- 债务棘轮：被剥离脚本的例外条目同步移除 ---
    (
        "tests/test_runtime_architecture.py",
        "    # Incident-specific forensic repair retained as historical evidence, not a\n"
        "    # production runtime entry point.\n"
        '    Path("scripts/repair_false_green_20260709.py"),\n',
        "",
    ),
    # --- docs/pipeline/README.md：死概念引用（HANDOFF 不随 OSS 分发） ---
    (
        "docs/pipeline/README.md",
        "2. **其他任何文档（AGENTS.md、HANDOFF、workflow 文档、skill、memory）只允许放入口、",
        "2. **其他任何文档（AGENTS.md、skill、memory）只允许放入口、",
    ),
    # --- docs/pipeline/README.md：事故日记收敛为永恒规则；私有拓扑改约定 ---
    (
        "docs/pipeline/README.md",
        "   2026-07-31 一天之内三次实证同一个病——**门建好了，但消费位接错或没人知道它在**：\n"
        "   ① `composition witness` 为救援而建（返回 bbox 和 16:9 裁切实现），被接成先否决并\n"
        "   整体替换了标定分数路由；② 「生成前零额度阻断」被当成待建项，而\n"
        "   `publish_staging.py:1727` 早已实现且 reason code 更精确，新加的重复门只会遮蔽它；\n"
        "   ③ 「segments 1:1 模式」被当成待建项，而 punch 路径本就是 1:1。\n",
        "   反复实证的同一个病——**门建好了，但消费位接错或没人知道它在**：为救援而建的\n"
        "   witness 被接成整体替换既有路由；「待建的门」其实早已实现且 reason code 更精确，\n"
        "   新加的重复门只会遮蔽它；「待建的模式」其实本就是现行为。\n",
    ),
    (
        "docs/pipeline/README.md",
        "   `free:/opt/bilive/autoslice/{repo,state,out,reports}` 与 B 站公开/创作中心面。",
        "   部署主机 `$AUTOSLICE_BASE/{repo,state,out,reports}` 与 B 站公开/创作中心面。",
    ),
    # --- 10-source：私有主机/网盘路径块改部署约定 ---
    (
        "docs/pipeline/10-source-recording.md",
        "- 录制服务：`free:/opt/bilive/compose.yml` 中的 `bililive_recorder`；\n"
        "- 录播姬配置：`free:/opt/bilive/bililive-recorder/config.json`；\n"
        "- adapter 状态/账本：`free:/opt/bilive/recording/`；\n"
        "- 自动切片部署：`free:/opt/bilive/autoslice/repo`（现查 `DEPLOYED_COMMIT`）；\n"
        "- 原始录播：`free:/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/`。",
        "- 录制服务：部署主机 compose 中的 `bililive_recorder`（参考 `ops/recording/`）；\n"
        "- 录播姬配置：部署主机 `bililive-recorder/config.json`；\n"
        "- adapter 状态/账本：部署主机 `recording/`；\n"
        "- 自动切片部署：`$AUTOSLICE_BASE/repo`（现查 `DEPLOYED_COMMIT`）；\n"
        "- 原始录播：你的录播根目录（按 `<room_id>/` 分房间目录）。",
    ),
    (
        "docs/pipeline/10-source-recording.md",
        "  `22966160_YYYYMMDD-HH-MM-SS.{flv,xml}`，日期目录按 `Asia/Shanghai`。",
        "  `<room_id>_YYYYMMDD-HH-MM-SS.{flv,xml}`，日期目录按 `Asia/Shanghai`。",
    ),
    # --- 50/70：指向已剥离 docs/workflows|reviews 的死链 ---
    (
        "docs/pipeline/50-song-lane.md",
        "本文件是歌切步骤的**分步权威**。`docs/workflows/lidousha-song-finished-package-workflow.md`\n"
        "与 `.agent/skills/song-lyrics-timeline-aligner/SKILL.md` 只提供操作方法，不能覆盖本文件或\n"
        "当前 schema。",
        "本文件是歌切步骤的**分步权威**。`.agent/skills/song-lyrics-timeline-aligner/SKILL.md`\n"
        "只提供操作方法，不能覆盖本文件或当前 schema。",
    ),
    (
        "docs/pipeline/70-cover.md",
        "本文件是封面步骤的**分步权威**。`docs/workflows/*` 与 cover skill 只提供操作方法；",
        "本文件是封面步骤的**分步权威**。cover skill 只提供操作方法；",
    ),
    (
        "docs/pipeline/70-cover.md",
        "  分诊单见 `docs/reviews/cover-text-violations-triage-20260731.md`。\n",
        "",
    ),
    # --- 60-title：把默认 profile 的示例值标成示例 ---
    (
        "docs/pipeline/60-title.md",
        "  `【李豆沙】`，song 统一成精确目录式；",
        "  profile 的标题前缀（默认 profile：`【李豆沙】`），song 统一成精确目录式；",
    ),
    (
        "docs/pipeline/60-title.md",
        "- 格式固定：`【李豆沙】豆沙歌，《歌名》`。",
        "- 格式固定（默认 profile 示例）：`【李豆沙】豆沙歌，《歌名》`。",
    ),
    (
        "docs/pipeline/60-title.md",
        "- 核心原则：标题围绕李豆沙本人；替换成任何别的主播还成立的标题就是失败。",
        "- 核心原则：标题围绕主播本人；替换成任何别的主播还成立的标题就是失败。",
    ),
    # --- 90-publish：身份/合集/逐候选运营点位通用化 ---
    (
        "docs/pipeline/90-publish.md",
        "- talk 标题统一 `【李豆沙】` envelope，song 精确目录式；",
        "- talk 标题统一 profile 前缀 envelope（默认 profile：`【李豆沙】`），song 精确目录式；",
    ),
    (
        "docs/pipeline/90-publish.md",
        "  owner 固定 `reviewed_by=维护者`，本项目 root agent 固定\n"
        '  `reviewed_by="Codex root"`，human delegate 写实际姓名；',
        "  owner 固定 `reviewed_by=<owner 标识>`，root agent 固定\n"
        '  `reviewed_by="<root-agent 标识>"`，human delegate 写实际姓名；',
    ),
    (
        "docs/pipeline/90-publish.md",
        "泛化检查表无效。672 必须把 0:13“前半无声、后半有声、全段无我草”和 1:48“整条 L 问句\n"
        "  无声并删除”作为两个独立 exact point 验收；850 必须分别验收 0:13 怪叫空白、0:59\n"
        "  “他一副，一副”和 1:23“kmx欺负人”；1493 必须验收“下斗里”已按 维护者 裁定修为\n"
        "  粉丝队玩梗专名“沙豆李”。",
        "泛化检查表无效。逐 candidate 的 exact review point 来自 profile 的\n"
        "  final_media_review_contracts 资产（本仓只有空模板，示例见其 `_example` 条目）。",
    ),
    (
        "docs/pipeline/90-publish.md",
        "5. 合集 lane 由冻结标题确定：talk → `小李切片`，song → `小李歌唱`。发布未精确入集不算",
        "5. 合集 lane 由冻结标题确定（合集名属频道运营配置；默认 profile：talk →\n"
        "   `小李切片`，song → `小李歌唱`）。发布未精确入集不算",
    ),
    # --- skills：描述/示例通用化，私有拓扑改约定 ---
    (
        ".agent/skills/bilive-autoslice-publish/SKILL.md",
        'description: "操作、修复、审查或发布李豆沙 autoslice 成品；',
        'description: "操作、修复、审查或发布 autoslice 成品；',
    ),
    (
        ".agent/skills/huozi-luanshua/SKILL.md",
        "description: Build, repair, verify, or review 李豆沙 “活字乱刷” speech reconstructions "
        "from historical bilive/Bilibili recordings. Use when 维护者 asks to make a sentence from "
        "past speech, find longer natural source phrases, distinguish 李豆沙 from guests such as "
        "礼墨, suggest a minimally edited sentence that splices better, or produce no-upload "
        "comparison videos.",
        "description: Build, repair, verify, or review the host's “活字乱刷” speech "
        "reconstructions from historical bilive/Bilibili recordings. Use when asked to make a "
        "sentence from past speech, find longer natural source phrases, distinguish the host "
        "from guests, suggest a minimally edited sentence that splices better, or produce "
        "no-upload comparison videos.",
    ),
    (
        ".agent/skills/huozi-luanshua/SKILL.md",
        "Use the committed runtime at `free:/opt/bilive/autoslice/repo` plus its live cache and "
        "recordings as source authority.",
        "Use the committed runtime at `$AUTOSLICE_BASE/repo` on the deployment host plus its "
        "live cache and recordings as source authority.",
    ),
    (
        ".agent/skills/huozi-luanshua/SKILL.md",
        "verify every selected cue as 李豆沙.",
        "verify every selected cue as the host.",
    ),
    (
        ".agent/skills/huozi-luanshua/agents/openai.yaml",
        '"从李豆沙历史直播中高置信重组语音并交付可追溯试听候选"',
        '"从主播历史直播中高置信重组语音并交付可追溯试听候选"',
    ),
    (
        ".agent/skills/official-replay-rescue/SKILL.md",
        "space.bilibili.com/1703797642/lists",
        "space.bilibili.com/<UID>/lists",
    ),
    (
        ".agent/skills/title-style/SKILL.md",
        "description: 为李豆沙切片生成、审查或修复",
        "description: 为频道切片生成、审查或修复",
    ),
    (
        ".agent/skills/title-style/SKILL.md",
        "# 李豆沙标题操作方法",
        "# 标题操作方法",
    ),
    (
        ".agent/skills/title-style/SKILL.md",
        "   - talk 最终带 `【李豆沙】`；\n"
        "   - song 精确为 `【李豆沙】豆沙歌，《canonical歌名》`，绝无 hook/副标题；",
        "   - talk 最终带 profile 标题前缀（默认 profile：`【李豆沙】`）；\n"
        "   - song 精确为 profile 歌切格式（默认 profile：`【李豆沙】豆沙歌，《canonical歌名》`），"
        "绝无 hook/副标题；",
    ),
    (
        ".agent/skills/song-lyrics-timeline-aligner/SKILL.md",
        "9. Final title is exactly `【李豆沙】豆沙歌，《canonical歌名》`; cover text is exactly `《歌名》`.",
        "9. Final title is exactly the profile's song-title format (default profile: "
        "`【李豆沙】豆沙歌，《canonical歌名》`); cover text is exactly `《歌名》`.",
    ),
    # --- profiles/README：接上骨架资产 + 示例 profile 校验预期 ---
    (
        "profiles/README.md",
        'mkdir -p "profiles/$profile_id" "assets/$profile_id"\n'
        'cp profiles/_template/profile.json "profiles/$profile_id/profile.json"',
        'mkdir -p "profiles/$profile_id"\n'
        'cp -r assets/_template "assets/$profile_id"\n'
        'cp profiles/_template/profile.json "profiles/$profile_id/profile.json"',
    ),
    (
        "profiles/README.md",
        "Validate a committed profile and all of its runtime paths before use:",
        "`assets/_template/` 是自动派生的最小骨架（结构合法、内容为空）；复制后按上表逐文件\n"
        "填充。注意：示例 profile `lidousha` 在全新 clone 里全量校验会因\n"
        "`voiceprint_profile.v1.json` 缺失而 BLOCKED——声纹属生物特征，不随开源仓分发，\n"
        "这是预期行为（先用 `--config-only`，或补齐你自己的声纹再全量校验）。\n"
        "\n"
        "Validate a committed profile and all of its runtime paths before use:",
    ),
)

# ---------------------------------------------------------------------------

FORBIDDEN_PATTERNS = (
    r"aierlma",
    r"(?<![A-Za-z0-9_])[Ii]van(?![A-Za-z0-9_])",
    r"/Users/ivan\b",
    r"cpa\.[a-z0-9.-]+\.top",
    r"sk-[A-Za-z0-9]{16,}",
    r"\boracle\b.*ssh|ssh.*\boracle\b",
    # v4：私人邮箱痕迹 / 私有账号 UID / 已剥离私库路径 / 旧 stem 残留
    r"hfnkzjbsbm",
    r"privaterelay\.appleid",
    r"1703797642",
    r"docs/spark",
    r"docs/reviews/",
    r"docs/workflows/",
    r"(?-i:HANDOFF)",  # 全大写才算私库交接文档引用；"Handoff" 是普通英文词
)

FORBIDDEN_ALLOWLIST_SUFFIXES = (".ttf",)

BINARY_SUFFIXES = (".ttf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff",
                   ".woff2", ".pyc", ".onnx", ".zip", ".gz")

_SANITIZE_NAME = re.compile(r"(?<![A-Za-z0-9_])[Ii][Vv][Aa][Nn](?![A-Za-z0-9_])")


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def classify(path: str) -> str:
    if any(path.startswith(prefix) for prefix in STRIP_PREFIXES):
        return "strip"
    if path in TEMPLATE_FILES:
        return "template" if TEMPLATE_FILES[path] != "keep" else "keep"
    if any(path.startswith(prefix) for prefix in TEMPLATE_DIRS):
        return "strip_templated_dir"
    if path.startswith("docs/oss/"):
        return "rootmap"
    known_roots = (
        "src/", "scripts/", "tests/", "docs/pipeline/", "ops/",
        "assets/", "profiles/", "prompts/", ".agent/skills/",
        "docs/bilibili-ai-subtitle-via-bcut.md",
        "docs/lidousha-auto-review-architecture.md",
        ".gitignore", ".gitattributes", "pyproject.toml",
        "requirements", "README", "Makefile", "conftest.py",
    )
    if any(path.startswith(root) for root in known_roots):
        return "keep"
    return "unknown"


def _is_text_target(path: Path) -> bool:
    return path.suffix.lower() not in BINARY_SUFFIXES


def _sanitize_text(text: str) -> str:
    cleaned = text.replace("/Users/ivan", "/Users/op")
    # 统一映射为同一词，保证代码常量、casefold 匹配器与测试断言三方替换后
    # 仍自恰（保留身份枚举语义不破坏）。CJK 紧邻也必须命中，故用 lookaround。
    return _SANITIZE_NAME.sub("维护者", cleaned)


def _rename_tokens() -> list[tuple[str, str]]:
    return sorted(RENAME_STEMS, key=lambda pair: len(pair[0]), reverse=True)


def _rewrite_relpath(rel: str) -> str:
    if any(rel.startswith(prefix) for prefix in REWRITE_SKIP_PREFIXES):
        return rel
    out = rel
    for old, new in _rename_tokens():
        out = out.replace(old, new)
    return out


def _rewrite_content(rel: str, text: str) -> str:
    if any(rel.startswith(prefix) for prefix in REWRITE_SKIP_PREFIXES):
        # 资产字节权威不动；profile manifest 例外（工具指针要跟随重命名）。
        if not (rel.startswith("profiles/") and rel.endswith("profile.json")):
            return text
    for old, new in _rename_tokens():
        text = text.replace(old, new)
    return text


def _template_payload(kind: str, original: Path) -> str:
    if kind == "registry":
        data = {
            "schema_version": "publication-registry.v1",
            "notes": (
                "候选↔BV 出版登记：published 候选禁止再走新投稿（修复只允许 "
                "authorized_upload.py repair-* 的原 BV 修复链）；"
                "hold_pending_review 候选在放行前禁止任何上传。"
            ),
            "entries": [
                {
                    "_example": True,
                    "candidate_id": "auto_000000_0_0",
                    "recording_date": "2026-01-01",
                    "status": "published",
                    "bvid": "BV1xxxxxxxxx",
                    "note": "example entry — replace with your own uploads",
                }
            ],
        }
    elif kind == "truth_ledger":
        data = {
            "schema_version": "subtitle-truth-ledger.v1",
            "notes": (
                "SOURCE_INTERVAL_TRUTH：已发布切片文本修复的唯一合法所有者。"
                "每条绑定源录像 sha + 毫秒区间 + replace_cue/replace_substring。"
                "本模板为空——真值属于你自己的录播。"
            ),
            "entries": [
                {
                    "_example": True,
                    "truth_id": "example-0001",
                    "source_recording_basename": "your-recording.mp4",
                    "source_sha256": "0" * 64,
                    "interval_ms": [0, 1000],
                    "action": "replace_cue",
                    "text": "示例真值文本",
                    "authority": "operator adjudication note",
                }
            ],
        }
    elif kind == "review_contracts":
        data = {
            "schema_version": "lidousha-final-media-review-contracts.v1",
            "authority": (
                "per-candidate exact final-review points; timestamps are "
                "deliberately broad windows around the reported issue"
            ),
            "contracts": [
                {
                    "_example": True,
                    "candidate_id": "auto_000000_0_0",
                    "subtitle_review_points": [
                        {
                            "point_id": "example-point",
                            "final_video_start_ms": 0,
                            "final_video_end_ms": 5000,
                            "expectation": "描述评审者必须逐帧/逐句核对的点",
                        }
                    ],
                }
            ],
        }
    elif kind == "rpa_index":
        data = {
            "schema_version": "recovery-publication-authority-index.v1",
            "entries": [],
            "notes": "same-BV 修复的出版权威索引——运营状态，模板为空。",
        }
    else:  # empty_entries
        try:
            original_data = json.loads(original.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            original_data = {}
        data = {
            key: (
                []
                if isinstance(value, list)
                else {} if isinstance(value, dict) else value
            )
            for key, value in original_data.items()
            if key in ("schema_version", "notes", "authority")
            or not isinstance(original_data.get(key), (list, dict))
        } or {"schema_version": "unknown", "entries": []}
        for key, value in original_data.items():
            if isinstance(value, list):
                data[key] = []
            elif isinstance(value, dict):
                data[key] = {}
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# assets/_template/：按 profiles/_template/profile.json 的资产清单，从默认
# profile 的同 key 资产派生"结构合法、内容为空"的骨架。文本资产给用途占位。

_TEMPLATE_TEXT_PLACEHOLDERS = {
    "glossary": (
        "# 频道词表（glossary）\n"
        "# 每行一个专名条目；「不要写成/不要改成/听成 X 等」子句会被解析为误听黑名单。\n"
        "# 示例（替换成你的频道专名）：\n"
        "# - 正确专名A：ASR 常听成「错形甲」「错形乙」，不要写成「错形甲」。\n"
        "# - 正确专名B：主播的粉丝团名，不要改成「近音错形」。\n"
    ),
    "persona": (
        "# 主播 persona\n\n"
        "描述主播的形象、性格、口癖与禁忌（封面/标题生成会读取本文件）。\n"
    ),
    "title_style": (
        "# 标题风格语料\n\n"
        "自动标题的风格谱系与 few-shot 语料。只放你自己频道的定稿标题；\n"
        "机器生成的旧标题不要进词库。\n"
    ),
    "slice_selection_metric": (
        "# 选题度量\n\n"
        "描述你的频道「什么算好切片」：围绕主播本人、观点强度、受众兴趣等硬维度。\n"
    ),
    "subtitle_correction_principles": (
        "# 字幕校对原则\n\n"
        "你的频道的字幕修正裁决原则（证据优先级、专名保向等）。\n"
    ),
    "psplive_roster": (
        "# 关联主播名册\n\n"
        "与本频道同场/联动的主播与专名名册（occurrence-neutral）。\n"
    ),
    "cover_identity_prompt": (
        "描述主播的视觉身份（发色、服装、标志性特征），供 AI 封面生成使用。\n"
    ),
}


_IDENTITY_TOKENS = ("李豆沙", "lidousha", "豆沙", "kmx", "小李")


def _scrub_identity_strings(payload: str) -> str:
    """模板骨架里不允许残留示例频道身份：整值替换成占位符。"""

    def scrub(node):
        if isinstance(node, dict):
            return {key: scrub(value) for key, value in node.items()}
        if isinstance(node, list):
            return [scrub(item) for item in node]
        if isinstance(node, str) and any(tok in node for tok in _IDENTITY_TOKENS):
            return "REPLACE_ME（按你的频道改写；写法参考 assets/lidousha/ 的同名资产）"
        return node

    data = scrub(json.loads(payload))
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def build_template_assets(out_root: Path) -> int:
    template_manifest = json.loads(
        (REPO / "profiles/_template/profile.json").read_text(encoding="utf-8")
    )
    default_manifest = json.loads(
        (REPO / "profiles/lidousha/profile.json").read_text(encoding="utf-8")
    )
    template_files: dict[str, str] = template_manifest["assets"]["files"]
    default_files: dict[str, str] = default_manifest["assets"]["files"]
    default_root = REPO / default_manifest["assets"]["root"]
    target_root = out_root / "assets/_template"
    written = 0
    for key, rel_name in sorted(template_files.items()):
        target = target_root / rel_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if key == "voiceprint_profile":
            payload = json.dumps(
                {
                    "schema_version": "<profile-id>-voiceprint-profile.v1",
                    "configuration_status": "UNCONFIGURED",
                    "notes": (
                        "声纹 enroll 后由 install_voiceprints.py 安装；"
                        "references 必须绑定恰好三段参考音频。"
                    ),
                    "references": [],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        elif key in _TEMPLATE_TEXT_PLACEHOLDERS:
            payload = _TEMPLATE_TEXT_PLACEHOLDERS[key]
        elif rel_name.endswith((".md", ".txt")):
            payload = f"# {key}\n\n按 assets/_template/README.md 与默认 profile 的同名资产填充。\n"
        else:
            source_rel = default_files.get(key)
            source = default_root / source_rel if source_rel else None
            payload = _template_payload("empty_entries", source or Path("/nonexistent"))
            payload = _scrub_identity_strings(payload)
        target.write_text(payload, encoding="utf-8")
        written += 1
    for dir_rel in sorted(template_manifest["assets"]["directories"].values()):
        marker_dir = target_root / dir_rel
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "README.md").write_text(
            "运行时目录骨架：本目录存放每个部署自己的数据（字体或逐候选评审/修复文件）。\n",
            encoding="utf-8",
        )
        written += 1
    (target_root / "README.md").write_text(
        "# assets/_template — 新频道最小资产骨架\n"
        "\n"
        "由导出器从默认 profile 的资产**结构**自动派生（内容清空）。用法：\n"
        "\n"
        "```bash\n"
        "cp -r assets/_template \"assets/<your-profile-id>\"\n"
        "```\n"
        "\n"
        "- 每个文件的 schema 形状合法，但内容是空的/占位的：骨架只保证\n"
        "  `validate_channel_profile.py` 的路径与形状检查通过，各 lane 首跑仍会按\n"
        "  fail-closed 原则告诉你缺什么内容。\n"
        "- 逐文件语义见 `profiles/README.md` 的对照表；参考实例见 `assets/lidousha/`\n"
        "  （另一个频道的实战沉淀，只作 schema/风格参考，不要继承其专名）。\n"
        "- `fonts/` 需要你自备可再分发的 CJK 字体（默认 profile 用 ZCOOL 快乐体 +\n"
        "  得意黑，见其 fonts 目录与许可）。\n"
        "- `voiceprint_profile.v1.json` 是 UNCONFIGURED 占位：声纹属于生物特征，须\n"
        "  自己 enroll 后用 `scripts/install_voiceprints.py` 安装。\n",
        encoding="utf-8",
    )
    return written + 1


def main() -> int:
    out_root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)

    kept, templated, stripped, unknown = [], [], [], []
    rootmap_entries: list[str] = []
    templated_dirs_seen = set()
    for rel in tracked_files():
        kind = classify(rel)
        source = REPO / rel
        if kind == "rootmap":
            rootmap_entries.append(rel)  # 最后落盘，显式覆盖同名 tracked 文件
        elif kind == "keep":
            target_rel = _rewrite_relpath(rel)
            target = out_root / target_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if _is_text_target(target):
                try:
                    text = target.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    text = None
                if text is not None:
                    cleaned = _rewrite_content(rel, _sanitize_text(text))
                    if cleaned != text:
                        target.write_text(cleaned, encoding="utf-8")
            kept.append(target_rel)
        elif kind == "template":
            target = out_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = _template_payload(TEMPLATE_FILES[rel], source)
            payload = _sanitize_text(payload)
            target.write_text(payload, encoding="utf-8")
            templated.append(rel)
        elif kind == "strip_templated_dir":
            for prefix in TEMPLATE_DIRS:
                if rel.startswith(prefix):
                    templated_dirs_seen.add(prefix)
            stripped.append(rel)
        elif kind == "strip":
            stripped.append(rel)
        else:
            unknown.append(rel)

    if unknown:
        print("UNCLASSIFIED paths — refusing to export:")
        for rel in unknown:
            print("  ", rel)
        return 2

    # rootmap（docs/oss/**）最后写：OSS 版 README/AGENTS/.gitignore 等以此为准。
    for rel in rootmap_entries:
        target = out_root / rel.removeprefix("docs/oss/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, target)
        if _is_text_target(target):
            text = target.read_text(encoding="utf-8")
            cleaned = _rewrite_content(rel.removeprefix("docs/oss/"), _sanitize_text(text))
            if cleaned != text:
                target.write_text(cleaned, encoding="utf-8")
        kept.append(rel)

    template_asset_count = build_template_assets(out_root)

    patch_failures: list[str] = []
    for rel, old, new in PATCHES:
        target = out_root / rel
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            patch_failures.append(f"{rel}: file missing for patch")
            continue
        hits = text.count(old)
        if hits != 1:
            patch_failures.append(f"{rel}: expected 1 occurrence, found {hits}: {old[:60]!r}…")
            continue
        target.write_text(text.replace(old, new), encoding="utf-8")
    if patch_failures:
        print("PATCH FAILURES — source drifted, update PATCHES:")
        for row in patch_failures:
            print("  ", row)
        return 4

    for prefix in sorted(templated_dirs_seen):
        if prefix.endswith("/"):
            marker_dir = out_root / prefix
            marker_dir.mkdir(parents=True, exist_ok=True)
            (marker_dir / "README.md").write_text(
                "本目录存放运营状态（逐候选评审/修复文件），"
                "属于每个部署自己的数据，不随开源仓分发。\n",
                encoding="utf-8",
            )

    # 文件名检查：assets/profiles 之外不允许再出现频道名。
    name_violations = [
        str(p.relative_to(out_root))
        for p in out_root.rglob("*lidousha*")
        if not str(p.relative_to(out_root)).startswith(("assets/", "profiles/"))
    ]
    if name_violations:
        print("FILENAME VIOLATIONS — channel name outside profile dirs:")
        for row in name_violations:
            print("  ", row)
        return 5

    old_stem_pattern = re.compile(
        "|".join(re.escape(old) for old, _ in RENAME_STEMS)
    )
    violations = []
    lidousha_flow_files = 0
    pattern = re.compile("|".join(FORBIDDEN_PATTERNS), re.IGNORECASE)
    for path in out_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in FORBIDDEN_ALLOWLIST_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel_str = str(path.relative_to(out_root))
        if (
            "lidousha" in text
            and not rel_str.startswith(("assets/", "profiles/"))
        ):
            lidousha_flow_files += 1
        for line_number, line in enumerate(text.splitlines(), start=1):
            hit = pattern.search(line)
            if (
                hit
                and hit.group(0).lower() == "aierlma"
                and "aierlma521" not in line
                and rel_str in ("LICENSE", "README.md", "NOTICE")
            ):
                continue  # 作者署名是有意公开的内容；邮箱仍禁
            if hit:
                violations.append(
                    f"{rel_str}:{line_number}: {line.strip()[:110]}"
                )
                continue
            stem_hit = old_stem_pattern.search(line)
            if stem_hit and not rel_str.startswith(("assets/", "profiles/")):
                violations.append(
                    f"{rel_str}:{line_number}: OLD STEM {stem_hit.group(0)}: "
                    f"{line.strip()[:90]}"
                )
    report = {
        "kept": len(kept),
        "templated": len(templated),
        "stripped": len(stripped),
        "template_assets": template_asset_count,
        "patches": len(PATCHES),
        "lidousha_flow_files": lidousha_flow_files,
        "forbidden_hits": len(violations),
    }
    print(json.dumps(report, ensure_ascii=False))
    if violations:
        print("FORBIDDEN CONTENT — snapshot NOT publishable yet:")
        for row in violations[:60]:
            print("  ", row)
        return 3
    print(f"clean snapshot at {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
