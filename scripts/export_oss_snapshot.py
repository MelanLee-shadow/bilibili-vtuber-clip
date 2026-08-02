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
    "tests/test_template_skeletons.py",        # import 导出器（随其剥离）
    "ops/blrec-patches/",                      # blrec 已退役
    "tests/test_blrec_live_watchdog.py",       # 孤儿测试(模块已剔)
    "tests/test_patch_autoslice_runner_recorder_status.py",
    "tests/test_repair_false_green_20260709.py",
    "tests/lidousha/test_authorized_upload.py",  # 运营态耦合测试
    "tests/lidousha/test_auto_review_shadow_pipeline.py",  # 运营态耦合测试
    "tests/lidousha/test_batch_speaker_review.py",  # 运营态耦合测试
    "tests/test_blrec_patches.py",  # 运营态耦合测试
    "tests/lidousha/test_branding_intro.py",  # 运营态耦合测试
    "tests/lidousha/test_build_recovery_review_manifest.py",  # 运营态耦合测试
    "tests/test_channel_profile.py",  # 运营态耦合测试
    "tests/lidousha/test_clip_context.py",  # 运营态耦合测试
    "tests/lidousha/test_cover_reference_authority.py",  # 运营态耦合测试
    "tests/lidousha/test_final_human_review.py",  # 运营态耦合测试
    "tests/lidousha/test_free_session_autoslice.py",  # 运营态耦合测试
    "tests/lidousha/test_review_package_audit.py",  # 运营态耦合测试
    "tests/lidousha/test_manual_title_repair_authority.py",  # 运营态耦合测试
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
    "tests/lidousha/test_source_subtitle_truth.py",  # 运营态耦合测试
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
    "scripts/ab_model_replay_closed_set.py",    # 重放私库历史裁决=OSS 不可用的 legacy
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
# 保留不动（功能词汇，见 OSS AGENTS.md「换频道剩余耦合」）：lidousha_centrality、
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
    (
        # 监控宿主：私库默认保持参考部署（launchd 既有调用不带 env）；OSS 默认
        # 本机——录制与切片同机是 quickstart 布局，远端宿主用 env 覆盖。
        "scripts/slice_monitor.py",
        'SSH_HOST = os.environ.get("AUTOSLICE_MONITOR_SSH_HOST", "free")',
        'SSH_HOST = os.environ.get("AUTOSLICE_MONITOR_SSH_HOST", "localhost")',
    ),
    (
        # VAD 脚本默认路径：私库默认保持参考部署（/opt/bilive/vad，生产不动）；
        # OSS 默认指仓内脚本——媒体在本机（quickstart 路径）时零配置可用，
        # 远端媒体宿主用 AUTOSLICE_VAD_SCRIPT 指向部署路径。
        "src/autoslice/subtitle_timing_qa.py",
        '        remote_script = os.environ.get(\n'
        '            "AUTOSLICE_VAD_SCRIPT", "/opt/bilive/vad/silero_vad_spans.py"\n'
        '        )',
        '        remote_script = os.environ.get("AUTOSLICE_VAD_SCRIPT") or str(\n'
        '            Path(__file__).resolve().parents[2]\n'
        '            / "scripts"\n'
        '            / "silero_vad_spans.py"\n'
        '        )',
    ),
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
    # --- 债务棘轮：authorized_upload 简介外化(-2)+合集ID强制配置化(+13)，账本
    #     同步为 2938 并在此说明（升行数=显式改账本，符合棘轮纪律）---
    (
        "tests/test_runtime_architecture.py",
        '"scripts/authorized_upload.py": 2_929,',
        '"scripts/authorized_upload.py": 2_938,',
    ),
    # --- 债务棘轮：导出器自身的账本行随文件剥离一并移除（私库保留该行） ---
    (
        "tests/test_runtime_architecture.py",
        "    # 2026-08-01 新记：OSS 发布整备（维护者 授权）把导出器扩成改名/patch/模板引擎；\n"
        "    # 私库专用构建工具，导出时自剥离，不进 OSS 面。\n"
        "    # 2026-08-02 +55：二轮测试修复（骨架逐键摘除治 governance:{} 必炸类、\n"
        "    # prompt 注入类模板全占位化、tag prompt JSON 契约）——维护者 8/2 /goal 授权；\n"
        "    # 测试 test_template_skeletons.py。\n"
        '    "scripts/export_oss_snapshot.py": 2_208,\n',
        "",
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
        ".agent/skills/official-replay-rescue/SKILL.md",
        "（`SOURCE_MEDIA_MISSING` 语义）；本文件只是操作配方。首例：2026-07-25\n"
        "19-20-00/19-50-00（commit 9dd7f4c，provenance 披露在 canonical 目录）。",
        "（`SOURCE_MEDIA_MISSING` 语义）；本文件只是操作配方。",
    ),
    (
        ".agent/skills/official-replay-rescue/SKILL.md",
        "2. **下载**（free）：BBDown + SESSDATA（`/opt/bilive/autoslice/vod_ingest_cookies.txt`）",
        "2. **下载**（录制主机）：BBDown + SESSDATA cookies"
        "（模板与校验见 [docs/credentials.md](../../../docs/credentials.md) 第 6 条）",
    ),
    (
        ".agent/skills/official-replay-rescue/SKILL.md",
        "   门：时长窗 + 全片视频流解码零损伤（模板见 `/opt/bilive/vod-rescue/2026-07-25/download.sh`）。",
        "   门：时长窗 + 全片视频流解码零损伤（rescue 脚本第 3 步会对终件独立复验这两条）。",
    ),
    (
        ".agent/skills/official-replay-rescue/SKILL.md",
        "   - 回放会在段边界**无缝吞秒**（7/25 实测 20:20↔20:50 间吞 4.3s）→ 双音频锚点",
        "   - 回放会在段边界**无缝吞秒**（幅度不可预测）→ 双音频锚点",
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
    # --- runner/producer 帮助文本：去示例频道/私有主机叙事（行数保持不变） ---
    (
        "scripts/session_autoslice.py",
        '"""Unattended post-stream auto-slice runner (runs ON the free host).',
        '"""Unattended post-stream auto-slice runner (runs ON the recording host).',
    ),
    (
        "scripts/session_autoslice.py",
        "维护者's goal (2026-07-05): when a 李豆沙 stream ends, free starts the FULL",
        "Design goal: when a stream ends, the recording host starts the FULL",
    ),
    (
        "scripts/session_autoslice.py",
        "        burn, REAL CPA cover, 李豆沙-style title) / song LRC lane with the strict",
        "        burn, REAL CPA cover, profile-style title) / song LRC lane with the strict",
    ),
    (
        "scripts/session_autoslice.py",
        "      → delivery under <repo>/lidousha/<date>/ + AUTOSLICE_SUMMARY.md with the",
        "      → delivery under <repo>/<output_directory>/<date>/ + AUTOSLICE_SUMMARY.md with the",
    ),
    (
        "scripts/session_autoslice.py",
        "HARD LESSONS BAKED IN (first real run, 2026-07-06):",
        "HARD LESSONS BAKED IN:",
    ),
    (
        "scripts/produce_slice_package.py",
        '  "selection_hook": "弹幕让李豆沙表演上下摇……", # selected main event; auto-title must retain it',
        '  "selection_hook": "弹幕让主播表演上下摇……", # selected main event; auto-title must retain it',
    ),
    (
        "scripts/produce_slice_package.py",
        '    {"remote_media": "<abs path on free>", "start_ms": ..., "end_ms": ...,',
        '    {"remote_media": "<abs path on recording host>", "start_ms": ..., "end_ms": ...,',
    ),
    (
        ".agent/skills/title-style/SKILL.md",
        "2. `../../../assets/lidousha/title_style.md`：自动标题 prompt 的风格/few-shot；",
        "2. 选中 profile 的 `title_style` 资产（默认 profile："
        "`../../../assets/lidousha/title_style.md`）：自动标题 prompt 的风格/few-shot；",
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
        "`assets/_template/` 是自动派生的骨架；复制后按其 README 的**分层**填充——\n"
        "身份四件套（词表/人设/标题风格/封面形象）由 agent 先问后写（模板内含问题\n"
        "清单与真实示例），字体与通用配置默认全给，其余 crawler 代填/运行时自长。\n"
        "注意：示例 profile `lidousha` 在全新 clone 里全量校验会因\n"
        "`voiceprint_profile.v1.json` 缺失而 BLOCKED——声纹属生物特征，不随开源仓分发，\n"
        "这是预期行为（先用 `--config-only`，或补齐你自己的声纹再全量校验）。\n"
        "\n"
        "Validate a committed profile and all of its runtime paths before use:",
    ),
    # ---- v4.2：具体事故案例 → 抽象机制（Ivan：记录通病，不记具体案例） ----
    (
        "docs/pipeline/10-source-recording.md",
        "但缓存一丢（如容器重启）字节即蒸发\n  （2026-07-25 两场次实损）。",
        "但缓存一丢（如容器重启）字节即蒸发。",
    ),
    (
        "docs/pipeline/20-selection.md",
        "## 同主题合并（维护者 2026-07-18 切片案 → 2026-07-19 新规）",
        "## 同主题合并",
    ),
    (
        "docs/pipeline/20-selection.md",
        "- **主题一致的候选尽量合并成一个切片**，不许把同一话题在源时间轴上相邻/交错的两段切成两条成品（案例：kmx 称呼两条切片同主题被分开切）。",
        "- **主题一致的候选尽量合并成一个切片**，不许把同一话题在源时间轴上相邻/交错的两段切成两条成品。",
    ),
    (
        "docs/pipeline/20-selection.md",
        "- 7/22 executable anchors：`auto_193450_3573_3665` 必须 Tier 1、有效分 75–85；\n"
        "  `auto_193450_5341_5459` 必须 Tier 2、有效分 50–60；前者必须稳定高于后者。\n"
        "  这两项只是量尺 canary，不构成 recovery allowlist；exact 集合只能来自当前 v7 plan 的\n"
        "  selection contract。",
        "- 校准资产内固定了一组量尺 canary 候选，各自锁定期望 Tier 与有效分区间，并锁定\n"
        "  相互间的分数排序；这些 canary 只用于验证评分器未漂移，不构成 recovery\n"
        "  allowlist；exact 集合只能来自当前 v7 plan 的 selection contract。",
    ),
    (
        "docs/pipeline/40-subtitle-text.md",
        "  不由主播读音决定——她用中文腔念日文假名 ID 是常态，音频 `kana_similarity=0` 不构成反证\n"
        "  （实例：2026-07-22 `auto_193450_1573_1672` cue76 `梅杰克家的六更るり`）。该豁免精确且完全：\n",
        "  不由主播读音决定——她用中文腔念日文假名 ID 是常态，音频 `kana_similarity=0` 不构成反证。\n"
        "  该豁免精确且完全：\n",
    ),
    (
        "docs/pipeline/40-subtitle-text.md",
        "  同形字（剥离结果披露在 `borrowed_boundary_blocks_stripped`）。1863 实案：SC 尾字「了」\n"
        "  她没念，独立转录连写到下一句「哎，现在几点了」，子序列对齐借同形「了」伪造出 near-complete\n"
        "  逐字朗读，SC 整行改写 applied 后又被 redelivery baseline 拉回，终验对该 exact_read 快照\n"
        "  永久失配。非逐字朗读（加字/漏字）一律保持已审口播文本，不注入 SC 原文。",
        "  同形字（剥离结果披露在 `borrowed_boundary_blocks_stripped`）。典型失败模式：尾字实际未\n"
        "  念出但被独立转录接续进下一句时，子序列对齐可能借邻句同形字伪造出 near-complete\n"
        "  逐字朗读，SC 整行改写 applied 后又被 redelivery baseline 拉回，导致对应 exact_read 快照\n"
        "  永久失配。非逐字朗读（加字/漏字）一律保持已审口播文本，不注入 SC 原文。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "设计原则（维护者 2026-07-19，源自 7/18 交付事故复盘 + 业界调研）：",
        "设计原则（源自交付事故复盘与业界调研）：",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "1. **检测≠裁决≠落地**，三层独立记账。7/18 事故的教训：检测层 6/6 全对，落地层全军覆没——事后审计必须能分清是哪层坏了（review-flags 的 `infra_unresolved` 字段就是这个用途）。",
        "1. **检测≠裁决≠落地**，三层独立记账。历史教训：检测层可能全部命中而落地层仍全军覆没——事后审计必须能分清是哪层坏了（review-flags 的 `infra_unresolved` 字段就是这个用途）。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "7. **漏听 recall**：选片钩子/弹幕/SC 里的词表专名在字幕零出现 → 审片员漏听检查（prompt 规则7）→ 插入提案 → 声学仲裁（插入永远走 T3，不进 T1）。**已知盲区（2026-07-19 合并条实证）**：专名在片内它处出现过时零出现触发器不响，单句漏听无人怀疑（kmx 0:49 案，最终走 维护者 审定 ledger 钉子）。改成逐句怀疑会假阳性爆炸；候选方向是「称呼/接话/突击等强语境句位 + 专名句位模板」的窄触发，进欠账。",
        "7. **漏听 recall**：选片钩子/弹幕/SC 里的词表专名在字幕零出现 → 审片员漏听检查（prompt 规则7）→ 插入提案 → 声学仲裁（插入永远走 T3，不进 T1）。**已知盲区**：专名在片内它处出现过时零出现触发器不响，单句漏听无人怀疑，最终只能走维护者审定 ledger 钉子兜底。改成逐句怀疑会假阳性爆炸；候选方向是「称呼/接话/突击等强语境句位 + 专名句位模板」的窄触发，进欠账。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "## 确定性怀疑编译器（2026-07-19 审片第二轮落地，`phonetic_scan.py`）",
        "## 确定性怀疑编译器（`phonetic_scan.py`）",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "7/18 五件套二审的教训机制化——人工审片能抓 kmx 变体靠的是「拼音近似 + 弹幕零背书」，这两条都能编译：",
        "人工审片二审阶段的教训机制化——人工审片能抓近音变体靠的是「拼音近似 + 弹幕零背书」，这两条都能编译：",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "- **词表拼音候选发现**（原欠账 #5）：逐 cue 滑窗 vs 注册实体 readings 的音节序列相似度；命中未注册面→临时混淆组送声学仲裁。校准夹具即 7/18 实案（皮毛熊/K头小/Q我熊→kmx、卖批/奶皮子→奶P、林更多→ありがとう）。已注册面/钦定词面（含被窗口包含）一律跳过——那是精确通道的辖区。cap=4/片、按分取前 N（噪声不许饿死真命中）。",
        "- **词表拼音候选发现**（原欠账 #5）：逐 cue 滑窗 vs 注册实体 readings 的音节序列相似度；命中未注册面→临时混淆组送声学仲裁。校准夹具取自真实历史误听案例（多组近音变体→登记词面映射）。已注册面/钦定词面（含被窗口包含）一律跳过——那是精确通道的辖区。cap=4/片、按分取前 N（噪声不许饿死真命中）。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "- **短语级重复分歧编译器**（原欠账 #0，维护者 说的「语义编译器」）：跨句 3-6 字 n-gram 重复 + 单字近音差 → 混淆组（抱/帮、零的人/零个人案）。语气词对儿（了/啦）不编、分歧字位落在钦定词面（侄女/和成天下）让位词表权威、长 gram 先行去重。",
        "- **短语级重复分歧编译器**（原欠账 #0，维护者 说的「语义编译器」）：跨句 3-6 字 n-gram 重复 + 单字近音差 → 混淆组（例如同音或近音字造成的短语重复分歧）。语气词对儿（了/啦）不编、分歧字位落在钦定词面（侄女/和成天下）让位词表权威、长 gram 先行去重。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "- **上线纪律（2026-07-20 修正）：两条 lane 目前为披露专用（phase 1）**——产出只写进 `post_semantic_entity_policy.*_disclosure_only` 审计字段供审片员/人工复核，不进声学仲裁、不产生改写。91_291 首跑实证：接入黑帧强制二选一后，仲裁顺从确认了「李小→立希」（滑窗骑在「小李小李」叠名上）和「粮之→祥子」两个伪候选并落盘改写——单次黑帧确认不满足保向铁律的证据门（本句音节+结构化弹幕/重复槽位支持）。扫描器已加叠名守卫（窗口与注册面出现位置重叠即跳过）。phase 2（见证充分的裁决通道）记欠账 #11。",
        "- **上线纪律：两条 lane 目前为披露专用（phase 1）**——产出只写进 `post_semantic_entity_policy.*_disclosure_only` 审计字段供审片员/人工复核，不进声学仲裁、不产生改写。首次接入黑帧强制二选一的生产实证表明：叠名场景下（滑窗骑在重复称呼上）单次黑帧确认会让仲裁顺从确认伪候选并落盘改写——单次黑帧确认不满足保向铁律的证据门（本句音节+结构化弹幕/重复槽位支持）。扫描器已加叠名守卫（窗口与注册面出现位置重叠即跳过）。phase 2（见证充分的裁决通道）记欠账 #11。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "## 答谢完整性 + 称呼串（2026-07-19，`chat_repair.py`/`surface_canon.py`）",
        "## 答谢完整性 + 称呼串（`chat_repair.py`/`surface_canon.py`）",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "- **断点吸附标点**（大叫案根因）：SC 对齐重排的相似度 DP 会把断点切进词中间；`_snap_split_to_punct` 把 ≤2 字尾巴挪过标点——完整的词在完整的 cue 里。",
        "- **断点吸附标点**：SC 对齐重排的相似度 DP 会把断点切进词中间；`_snap_split_to_punct` 把 ≤2 字尾巴挪过标点——完整的词在完整的 cue 里。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "- **谢谢还原**（十麻乃/快乐猫猫案）：SC 字幕卡式行丢答谢动词时按同时轴 draft 见证还原前缀（T1 见证语义）；draft 没听到不动。",
        "- **谢谢还原**：SC 字幕卡式行丢答谢动词时按同时轴 draft 见证还原前缀（T1 见证语义）；draft 没听到不动。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "- **未答谢披露**（0:23 打码礼物案）：窗口内送礼人/SC 发送者无答谢锚点→披露名单（不改写），给审片员和音频仲裁当「刚刚没念过的送礼人」候选。",
        "- **未答谢披露**：窗口内送礼人/SC 发送者无答谢锚点→披露名单（不改写），给审片员和音频仲裁当「刚刚没念过的送礼人」候选。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "## 预算分配铁律（2026-07-20「脑海里根本没有冒出熊猫二字啊」案）",
        "## 预算分配铁律",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "**一切有界预算必须先收集、后排序、再消费——绝不按到达序先到先得。**同一晚两处实证：拼音扫描器 cap 按扫描序截断时低分噪声饿死林更多（0.89）；念读近失仲裁帽（3）按证据序消费时，晚段真念读（score 0.624/precision 0.875，全场最高质量近失）被 t=818.9 的近似弹幕（0.594，还绑错了 cue）占掉末位名额，**静默出局、零审计痕迹**。修复：近失候选全量收集→同 cue 跨度去重（只留最高分）→按 (score, precision) 取前 3 送仲裁（`chat_proposals._discover_chat_proposals`）。诊断路径备忘：这类\"该修没修\"先查审计数组是否触帽（len==cap 即饥饿嫌疑），再复算 `_match_metrics` 对照发现门槛。",
        "**一切有界预算必须先收集、后排序、再消费——绝不按到达序先到先得。**按扫描/到达序截断会让低分噪声饿死高质量候选；按证据序消费也可能让晚到但更优的候选被早到的弱证据占掉末位名额，**静默出局、零审计痕迹**。修复：候选全量收集→同 cue 跨度去重（只留最高分）→按 (score, precision) 取前 N 送仲裁（`chat_proposals._discover_chat_proposals`）。诊断路径备忘：这类\"该修没修\"先查审计数组是否触帽（len==cap 即饥饿嫌疑），再复算 `_match_metrics` 对照发现门槛。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "**弹幕时间模型（维护者 2026-07-20 指正）**：事件时间戳＝发送时刻≠她看到的时刻——上屏渲染延迟随房间负载变化（脑海案实测 ~15s，她刚看到就立刻念了）。念读窗口的宽上界（90s）是这个物理事实的正确反映；排序永远按文本质量，不按时间贴近度（真没想到熊猫案的误绑抓在 +89.6s 窗口上沿、被音频正确否掉——窗口宽度的代价由质量排序+声学仲裁兜住，别用收窗口来治）。",
        "**弹幕时间模型**：事件时间戳＝发送时刻≠她看到的时刻——上屏渲染延迟随房间负载变化，可达十余秒量级。念读窗口的宽上界（90s）是这个物理事实的正确反映；排序永远按文本质量，不按时间贴近度（曾出现误绑发生在窗口上沿附近、靠音频正确否掉的情况——窗口宽度的代价由质量排序+声学仲裁兜住，别用收窗口来治）。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "## 钉子纪律（2026-07-19「只有kmx/十麻乃」拼贴病复盘）",
        "## 钉子纪律",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "维护者 指正=该句整体替换的锚，不是插入片段：钉子文本必须是**改正后的完整口播**（详见 principles §十三新增两条）。7/19 修复了两枚带病钉子并为四条片补 36 枚 review-round-2 钉子（生成器对本地终稿 dry-run 全过）。标题 authority 不属于本步骤；只读 [60-title.md](60-title.md)。",
        "维护者 指正=该句整体替换的锚，不是插入片段：钉子文本必须是**改正后的完整口播**（详见 principles §十三新增两条）。标题 authority 不属于本步骤；只读 [60-title.md](60-title.md)。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "6. **本地可疑度粗筛缺失**：调研结论第一优先级（PPL/pycorrector 漏斗），把昂贵 LLM/音频调用集中到高可疑行。当前每片全量过审片员，成本可接受，暂缓。7/19 追加动机：3Dlive 乱码段（「三丢下我怎么办」）这类重度 garble 需要先被粗筛点名，才轮得到带话题提示的音频重听。",
        "6. **本地可疑度粗筛缺失**：调研结论第一优先级（PPL/pycorrector 漏斗），把昂贵 LLM/音频调用集中到高可疑行。当前每片全量过审片员，成本可接受，暂缓。追加动机：重度乱码（garble）段需要先被粗筛点名，才轮得到带话题提示的音频重听。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "9. **幻听插入词的全量自动发现仍未完成**（2026-07-18 七星「为什么/偶像脸」案）：",
        "9. **幻听插入词的全量自动发现仍未完成**：",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "11. **动态候选的 phase 2 裁决通道**（2026-07-20 立希/祥子回归案）：披露专用的两条扫描 lane 要重新获得改写权，必须配「多证人门」——本句音节独立复核（非回声）、结构化弹幕/SC 佐证、或双源 ASR 一致中的至少两项；单次黑帧强制二选一永远不够。设计时同读 e46d36a 的 AGY 回声防御。",
        "11. **动态候选的 phase 2 裁决通道**：披露专用的两条扫描 lane 要重新获得改写权，必须配「多证人门」——本句音节独立复核（非回声）、结构化弹幕/SC 佐证、或双源 ASR 一致中的至少两项；单次黑帧强制二选一永远不够。设计时同读 AGY 回声防御的既有教训。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "12. **expected_entity 为空的修复绕过未注册回退守卫**（91_291 cue43 发生→发现案）：`revert_unregistered_entity_repairs` 对 expected 为空的行直接放行——句级重复分歧仲裁产生的无实体改写不受该守卫约束。补法：空 expected 的文本改写同样要求注册面或见证，否则回退披露。",
        "12. **expected_entity 为空的修复绕过未注册回退守卫**：`revert_unregistered_entity_repairs` 对 expected 为空的行直接放行——句级重复分歧仲裁产生的无实体改写不受该守卫约束。补法：空 expected 的文本改写同样要求注册面或见证，否则回退披露。",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "## 业界调研要点（2026-07-19，详见 commit 记录）",
        "## 业界调研要点",
    ),
    (
        "docs/pipeline/70-cover.md",
        "  2026-07-20 生态调研采用 2–12 字的原话/质问/反差梗字，配真实表情帧和更大的脸；",
        "  生态调研采用 2–12 字的原话/质问/反差梗字，配真实表情帧和更大的脸；",
    ),
    (
        "docs/pipeline/70-cover.md",
        "  初始 v2 `selected_treatment` 就必须是 `cpa_redraw`（7/26 1411 角落小人案），最终 v3",
        "  初始 v2 `selected_treatment` 就必须是 `cpa_redraw`，最终 v3",
    ),
    (
        "docs/pipeline/70-cover.md",
        "  （2026-07-24 “生豆角 / 熊猫头下播”案）必须改选；CPA 不能用“背景也许会画出道具”",
        "  必须改选；CPA 不能用“背景也许会画出道具”",
    ),
    (
        "docs/pipeline/70-cover.md",
        "  此前「多行换大字」的排版预算（左右分栏 8 行）与本合同从未对账，加上「无梗字即把\n"
        "  整段 `cover_text` 交平衡器」的自动回退，两者联乘产出过 3–8 行的成品：2026-07-24\n"
        "  至 07-29 的 30 条成品里 6 条违例，全部 `cover_text_mode=full`，punch 路径零违例。",
        "  此前「多行换大字」的排版预算（左右分栏 8 行）与本合同从未对账，加上「无梗字即把\n"
        "  整段 `cover_text` 交平衡器」的自动回退，两者联乘产出过 3–8 行的成品，且全部落在\n"
        "  `cover_text_mode=full` 路径，punch 路径零违例。",
    ),
    (
        "docs/pipeline/70-cover.md",
        "  2026-07-25 “让新3D永久保留 / 小李拒绝花钱”案会因第一行截掉“白色奶龙”对象而\n"
        "  fail closed；合格抽取可用““白色奶龙”表情 / 小李拒绝花钱”。**无合格短文案不回退整段**\n"
        "  （2026-07-31 修正：此处原写「回退完整 `cover_text`」，与上文 :44-45 的禁令直接矛盾，\n"
        "  这条政策缝正是白色奶龙案的成因）——有界重试让 CPA 改选更短的连续原文，仍无则候选降",
        "  首行切点若把紧随的关键指称对象整体排除在外，同样必须\n"
        "  fail closed；合格抽取需要完整保留该对象及其修饰内容。**无合格短文案不回退整段**\n"
        "  （修正：此处原写「回退完整 `cover_text`」，与上文 :44-45 的禁令直接矛盾，\n"
        "  这条政策缝正是这类截断问题的成因）——有界重试让 CPA 改选更短的连续原文，仍无则候选降",
    ),
    (
        "docs/pipeline/70-cover.md",
        "- talk 封面强调字号必须 `>=120px`；渲染低于该线直接报 `COVER_TITLE_TOO_SMALL`，交付包审计也必须阻断。不得用“文件完整/没有裁字”代替缩略图可读性验收；应缩短封面梗字或换更宽版式，禁止继续缩字（2026-07-22 当面对质封面 91px 回归案）。",
        "- talk 封面强调字号必须 `>=120px`；渲染低于该线直接报 `COVER_TITLE_TOO_SMALL`，交付包审计也必须阻断。不得用“文件完整/没有裁字”代替缩略图可读性验收；应缩短封面梗字或换更宽版式，禁止继续缩字。",
    ),
    (
        "docs/pipeline/70-cover.md",
        "  要求大得多的脸（2026-07-26 BV1E93L6rErV 案：固定 fit-crop 卡把嘴/下巴裁掉上了公开面），",
        "  要求大得多的脸（固定 fit-crop 卡可能把嘴/下巴裁掉，曾导致这类裁切上了公开面），",
    ),
    (
        "docs/pipeline/90-publish.md",
        "3b. 逐案放行（维护者 2026-07-26：「没有任何纪律要求必须5个全complete才能动BV，\n"
        "   修复时哪个好了就可以改哪个」）：批仍 `recovery_incomplete` 时，可用",
        "3b. 逐案放行：批仍 `recovery_incomplete` 时，可用",
    ),
    # ---- v4.6：调研文档的陈旧断言改为现役事实（方案早已集成为默认转写层） ----
    (
        "docs/bilibili-ai-subtitle-via-bcut.md",
        "# B站 AI 字幕原理 + 大厂免费 ASR 聚合方案（替代 whisper / agy 精听）",
        "# B站 AI 字幕原理 + 大厂免费 ASR 聚合方案（本仓现役中文转写层的调研底稿）",
    ),
    (
        "docs/bilibili-ai-subtitle-via-bcut.md",
        "> **日期化技术调研，不是当前操作手册。** 接口、模型、可用性、速度与下方“下一步”只代表\n"
        "> 2026-07-04/10 的观察；当前字幕链以\n"
        "> [pipeline/40-subtitle-text.md](pipeline/40-subtitle-text.md) 和 live adapter readback 为准。",
        "> **调研底稿，非当前操作手册。此方案后来已集成为本仓的默认中文转写层**：\n"
        "> `scripts/free_asr_client.py` 聚合必剪主源+剪映备源，whisper/agy 精听已退出\n"
        "> 中文链。接口可用性、速度与下方“下一步”只代表调研当时的观察；当前字幕链\n"
        "> 规则以 [pipeline/40-subtitle-text.md](pipeline/40-subtitle-text.md) 为准。",
    ),
    (
        "docs/bilibili-ai-subtitle-via-bcut.md",
        "可以直接白嫖；再聚合剪映作备源，就能整体替代中文管线里的 whisper 和 agy 精听",
        "可以直接白嫖；聚合剪映作备源后**已整体替代**中文管线里的 whisper 和 agy 精听",
    ),
    # ---- v4.8：表情包媒体随仓发布，代码注释同步 ----
    (
        "src/autoslice/cover_emote.py",
        "asset); the sticker media lives under the profile tree (``assets/<profile>/emote/``\n"
        "locally; ``AUTOSLICE_EMOTE_DIR`` overrides on the runner host).",
        "asset); the sticker media ships with this repo under the profile tree\n"
        "(``assets/<profile>/emote/``; override the root via ``AUTOSLICE_EMOTE_DIR``).",
    ),
    # ---- v4.7：prompt/控制流身份占位化（Ivan 放行：默认 profile 渲染字节不变） ----
    # 每处 = 字面身份 → CHANNEL_PROFILE 字段；default profile 下输出逐字节等于原文，
    # 由全套 pytest 相等性背书。外貌描述/频道梗释义等无 profile 字段者仍留在耦合清单。
    (
        "src/autoslice/cover_punch_semantics.py",
        "from src.autoslice.llm_client import LlmCall, extract_json_object",
        "from src.autoslice.llm_client import LlmCall, extract_json_object\n"
        "from src.autoslice.surface_canon import CHANNEL_PROFILE",
    ),
    (
        "src/autoslice/cover_punch_semantics.py",
        '        "你是李豆沙切片封面的最终文字语义裁决者。你没有音频或图像输入，"',
        '        f"你是{CHANNEL_PROFILE.display_name}切片封面的最终文字语义裁决者。你没有音频或图像输入，"',
    ),
    (
        "src/autoslice/source_fact_review.py",
        "from src.autoslice.surface_canon import (\n"
        "    canonicalize_hard_meme_surfaces,\n"
        "    hard_meme_surface_rules,\n"
        ")",
        "from src.autoslice.surface_canon import (\n"
        "    CHANNEL_PROFILE,\n"
        "    canonicalize_hard_meme_surfaces,\n"
        "    hard_meme_surface_rules,\n"
        ")",
    ),
    (
        "src/autoslice/source_fact_review.py",
        '        "你是李豆沙切片派生文案的 source-fact 最终裁决者。你只有文字输入，"',
        '        f"你是{CHANNEL_PROFILE.display_name}切片派生文案的 source-fact 最终裁决者。你只有文字输入，"',
    ),
    (
        "src/autoslice/final_review_auditor.py",
        "from src.autoslice.chat_evidence import (",
        "from src.autoslice.surface_canon import CHANNEL_PROFILE\n"
        "from src.autoslice.chat_evidence import (",
    ),
    (
        "src/autoslice/final_review_auditor.py",
        '_AUDIT_PROMPT = """你是李豆沙切片的终审审片员。',
        '_AUDIT_PROMPT = """你是{host_name}切片的终审审片员。',
    ),
    (
        "src/autoslice/final_review_auditor.py",
        "（她的自称专名是「李豆沙」和「小李」，两者平等；",
        "（她的自称专名是「{host_name}」和「{host_short}」，两者平等；",
    ),
    (
        "src/autoslice/final_review_auditor.py",
        "  温柔型李豆沙案，维护者 裁定）**：",
        "  温柔型{host_name}案，维护者 裁定）**：",
    ),
    (
        "src/autoslice/final_review_auditor.py",
        "    prompt = _AUDIT_PROMPT.format(\n        max_findings=MAX_FINDINGS,",
        "    prompt = _AUDIT_PROMPT.format(\n"
        "        host_name=CHANNEL_PROFILE.display_name,\n"
        "        host_short=CHANNEL_PROFILE.short_name,\n"
        "        max_findings=MAX_FINDINGS,",
    ),
    (
        "src/autoslice/subtitle_regression.py",
        "from src.autoslice.chat_authority import normalize_chat_text",
        "from src.autoslice.chat_authority import normalize_chat_text\n"
        "from src.autoslice.surface_canon import CHANNEL_PROFILE",
    ),
    (
        "src/autoslice/subtitle_regression.py",
        '_SPEAKER_LABEL = re.compile(r"^\\[(?:李豆沙|连线)\\]\\s*")',
        '_SPEAKER_LABEL = re.compile(\n'
        '    r"^\\[(?:"\n'
        "    + re.escape(CHANNEL_PROFILE.host_speaker_label)\n"
        '    + "|"\n'
        "    + re.escape(CHANNEL_PROFILE.guest_speaker_label)\n"
        '    + r")\\]\\s*"\n'
        ")",
    ),
    (
        "src/autoslice/self_reference_absorption.py",
        "from src.autoslice.jingting_chunker import parse_srt_cues",
        "from src.autoslice.jingting_chunker import parse_srt_cues\n"
        "from src.autoslice.surface_canon import CHANNEL_PROFILE",
    ),
    (
        "src/autoslice/self_reference_absorption.py",
        '_CANONICAL_NAMES = ("李豆沙", "小李", "豆沙")',
        "_CANONICAL_NAMES = tuple(CHANNEL_PROFILE.self_reference_aliases)",
    ),
    (
        "src/autoslice/self_reference_absorption.py",
        '    target = _phonetic_text("李豆沙")',
        "    target = _phonetic_text(CHANNEL_PROFILE.display_name)",
    ),
    (
        "src/autoslice/clip_context.py",
        "from src.autoslice.speech_memory_ledger import load_scoped_speech_memory",
        "from src.autoslice.speech_memory_ledger import load_scoped_speech_memory\n"
        "from src.autoslice.surface_canon import CHANNEL_PROFILE",
    ),
    (
        "src/autoslice/clip_context.py",
        '        speaker_id="lidousha",',
        "        speaker_id=CHANNEL_PROFILE.profile_id,",
    ),
    (
        "src/autoslice/clip_context.py",
        '        channel_id="lidousha",',
        "        channel_id=CHANNEL_PROFILE.profile_id,",
    ),
    (
        "src/autoslice/huozi_luanshua.py",
        "from typing import Iterable, Mapping, Sequence",
        "from typing import Iterable, Mapping, Sequence\n"
        "\n"
        "from src.autoslice.surface_canon import CHANNEL_PROFILE",
    ),
    (
        "src/autoslice/huozi_luanshua.py",
        '        if speaker != "lidousha" and not (speaker == "mixed" and has_trusted_ranges):',
        "        if speaker != CHANNEL_PROFILE.profile_id and not (speaker == \"mixed\" and has_trusted_ranges):",
    ),
    (
        "src/autoslice/huozi_luanshua.py",
        '                    speaker="lidousha",',
        "                    speaker=CHANNEL_PROFILE.profile_id,",
    ),
    (
        "src/autoslice/huozi_luanshua.py",
        '        if piece.get("speaker") != "lidousha":',
        '        if piece.get("speaker") != CHANNEL_PROFILE.profile_id:',
    ),
    (
        "src/autoslice/publish_staging.py",
        '                        "be the person labelled 李豆沙; do not hybridize her with "',
        '                        f"be the person labelled {CHANNEL_PROFILE.display_name}; do not hybridize her with "',
    ),
    (
        "src/autoslice/semantic_candidate_selector.py",
        "- dimensions 七项都打 0..4 整数：lidousha_centrality（李豆沙不可替代性）、stance_intensity、",
        "- dimensions 七项都打 0..4 整数：lidousha_centrality（{CHANNEL_PROFILE.display_name}不可替代性）、stance_intensity、",
    ),
    (
        "scripts/evaluate_speaker_phase1.py",
        "from pathlib import Path",
        "from pathlib import Path\n"
        "\n"
        "from src.autoslice.surface_canon import CHANNEL_PROFILE",
    ),
    (
        "scripts/evaluate_speaker_phase1.py",
        'TRUTH_TO_AUTO = {"lidousha": "李豆沙", "non_lidousha": "连线"}',
        "TRUTH_TO_AUTO = {\n"
        "    CHANNEL_PROFILE.profile_id: CHANNEL_PROFILE.host_speaker_label,\n"
        '    f"non_{CHANNEL_PROFILE.profile_id}": CHANNEL_PROFILE.guest_speaker_label,\n'
        "}",
    ),
    # ---- v4.7 棘轮对账：auditor prompt 注入 host 占位（import+2 kwargs = 模块
    #      +3 行、函数 +2 行），显式改账本 ----
    (
        "tests/test_runtime_architecture.py",
        '    ("src/autoslice/final_review_auditor.py", "audit_final_subtitles"): 407,',
        '    ("src/autoslice/final_review_auditor.py", "audit_final_subtitles"): 409,',
    ),
    (
        "tests/test_runtime_architecture.py",
        '    "src/autoslice/final_review_auditor.py": 3_433,',
        '    "src/autoslice/final_review_auditor.py": 3_436,',
    ),
    # ---- v4.5：合集 ID 账号专属，强制部署方自填（Ivan：绝不默认给我的合集） ----
    (
        "scripts/authorized_upload.py",
        "EXPECTED_SEASON_IDS = {\n"
        '    "talk": {"season_id": 8383206, "section_id": 9320779},\n'
        '    "song": {"season_id": 8410735, "section_id": 9364628},\n'
        "}",
        '_SEASON_IDS_ENV = "AUTOSLICE_SEASON_IDS"\n'
        "\n"
        "\n"
        "def expected_season_ids() -> dict:\n"
        '    """入集校验的合集/小节 ID——账号专属，强制部署方自填，无默认值。"""\n'
        "    raw = os.environ.get(_SEASON_IDS_ENV)\n"
        "    if not raw:\n"
        "        raise SystemExit(\n"
        '            "AUTOSLICE_SEASON_IDS 未配置：发布入集校验需要你自己账号的合集/小节 ID。\\n"\n'
        "            '格式：{\"talk\": {\"season_id\": 0, \"section_id\": 0}, '\n"
        "            '\"song\": {\"season_id\": 0, \"section_id\": 0}}\\n'\n"
        '            "获取：在创作中心手动把任一稿件加入目标合集，再用 "\n'
        '            "scripts/bili_archive_tool.py view 回读该稿件的 season_id/section_id。"\n'
        "        )\n"
        "    return json.loads(raw)",
    ),
    (
        "scripts/authorized_upload.py",
        '    expected_ids = EXPECTED_SEASON_IDS.get(str(block.get("lane") or ""))',
        '    expected_ids = expected_season_ids().get(str(block.get("lane") or ""))',
    ),
    (
        "scripts/authorized_upload.py",
        "    package_problems.extend(human_review.attach_final_human_review("
        "manifest, args.final_human_review, season_ids=EXPECTED_SEASON_IDS))",
        "    package_problems.extend(human_review.attach_final_human_review("
        "manifest, args.final_human_review, season_ids=expected_season_ids()))",
    ),
    (
        "tests/lidousha/test_authorized_upload_season.py",
        'def _isolated_default_upload_lock(tmp_path, monkeypatch):\n'
        '    monkeypatch.setattr(au, "DEFAULT_UPLOAD_LOCK", tmp_path / "default-upload.lock")',
        'def _isolated_default_upload_lock(tmp_path, monkeypatch):\n'
        '''    monkeypatch.setenv(
        "AUTOSLICE_SEASON_IDS",
        '{"talk": {"season_id": 8383206, "section_id": 9320779},'
        ' "song": {"season_id": 8410735, "section_id": 9364628}}',
    )
'''
        '    monkeypatch.setattr(au, "DEFAULT_UPLOAD_LOCK", tmp_path / "default-upload.lock")',
    ),
    # ---- v4.4：个别 patch 先于日期清理跑，处理规则难辨的语义 ----
    (
        "src/autoslice/song_name_pin.py",
        "2026-07-14 起 pypinyin 已装",
        "pypinyin 已装",
    ),
    # ---- v4.3：裸日期出处也去日期（保留"维护者拍板"事实，不留时间线） ----
    (
        "docs/pipeline/60-title.md",
        "## 歌切标题（铁律，维护者 2026-07-14 定、2026-07-19 重申）",
        "## 歌切标题（铁律，维护者定）",
    ),
    (
        "docs/pipeline/20-selection.md",
        "关键词只兜底（维护者 2026-07-03）。",
        "关键词只兜底（维护者拍板）。",
    ),
    (
        "docs/pipeline/80-package-delivery.md",
        "- tag 按成品字幕出（`upload_tag_policy.py`，维护者 2026-07-13）。",
        "- tag 按成品字幕出（`upload_tag_policy.py`，维护者拍板）。",
    ),
    (
        "docs/pipeline/70-cover.md",
        "- **分行权威等级（2026-07-31 立，机器已实现）**：切点合法性只由权威定义——作者显式",
        "- **分行权威等级（机器已实现）**：切点合法性只由权威定义——作者显式",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "0. ~~短语级重复分歧检测~~（2026-07-19 已落地，见上节）",
        "0. ~~短语级重复分歧检测~~（已落地，见上节）",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "5. ~~专名匹配纯精确~~（2026-07-19 已落地拼音候选发现层，见上节；",
        "5. ~~专名匹配纯精确~~（已落地拼音候选发现层，见上节；",
    ),
    (
        "docs/pipeline/41-semantic-repair.md",
        "7. ~~**语义 QA 评审文本≠最终交付文本**~~（2026-07-23 已闭环）：finalizer 现在逐项验证",
        "7. ~~**语义 QA 评审文本≠最终交付文本**~~（已闭环）：finalizer 现在逐项验证",
    ),
    (
        "docs/auto-review-architecture.md",
        "> 当前高层结构图，2026-07-24。本文件不定义准入、schema、阈值或状态机；现行规则只读",
        "> 当前高层结构图。本文件不定义准入、schema、阈值或状态机；现行规则只读",
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
        ["git", "-c", "core.quotepath=false", "ls-files"],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def classify(path: str) -> str:
    if any(path.startswith(prefix) for prefix in STRIP_PREFIXES):
        return "strip"
    if path in TEMPLATE_FILES:
        return "template" if TEMPLATE_FILES[path] != "keep" else "keep"
    if any(path.startswith(prefix) for prefix in TEMPLATE_DIRS):
        return "strip_templated_dir"
    if path.startswith("docs/oss/templates/"):
        return "template_source"  # 模板骨架源文，由 build_template_assets 消费
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


# ---------------------------------------------------------------------------
# v4.4：代码注释/docstring 的日期化出处清理（Ivan：决策理由保留，日期案名不留）。
# 只作用于 Python 的 COMMENT token 与 docstring 行、shell 的 # 注释行；
# 字符串字面量（schema epoch、功能默认值）天然不受影响。

_DATE = r"2026-\d{2}-\d{2}"
_COMMENT_DATE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 「维护者 2026-07-14 定、2026-07-19 重申」→「维护者」
    (re.compile(rf"维护者 {_DATE}(?:[ \t]*[/、,，][ \t]*{_DATE})*"), "维护者"),
    # 「(first: 2026-07-26 BV…)」「(first real run, 2026-07-06)」「(2026-07-25 measured: …)」
    (re.compile(rf"[（(][ \t]*first[^）)]*[）)][:：]?"), ""),
    # 「（2026-07-25 两场次实损)」「(2026-07-04 教训)」等叙事括注
    (
        re.compile(
            rf"[（(][ \t]*{_DATE}[^（）()]*?"
            r"(?:教训|案|实测|复盘|事故|指示|拍板|实损|lesson|audit|measured|proven"
            r"|BV[0-9A-Za-z]{6,})[^（）()]*[）)]"
        ),
        "",
    ),
    # 行首叙事日期：「2026-07-25 loss lesson:」「2026-07-09 起：」「2026-07-27：」
    (re.compile(rf"{_DATE}[ \t]*(?=(?:loss lesson|lesson|external audit|audit)\b)"), ""),
    (re.compile(rf"{_DATE}[ \t]*起[:：][ \t]*"), ""),
    (re.compile(rf"{_DATE}[ \t]*[:：][ \t]*"), ""),
    # 括号内裸日期「（2026-07-24）」
    (re.compile(rf"[（(][ \t]*{_DATE}[ \t]*[）)]"), ""),
    # 括号开头的日期：「（2026-07-13，由…」「(2026-07-22 1863:」→ 去日期保内容
    (re.compile(rf"([（(])[ \t]*{_DATE}[ \t]*[，,][ \t]*"), r"\1"),
    (re.compile(rf"([（(])[ \t]*{_DATE}[ \t]+"), r"\1"),
    # 收尾/逗号前的日期：「…2026-07-18)」「…2026-07-25，」
    (re.compile(rf"[ \t]*{_DATE}[ \t]*([，,）)])"), r"\1"),
    # 破折号缀「—— 2026-07-14 实战定稿」
    (re.compile(rf"[-—]{{2}}[ \t]*{_DATE}[ \t]*"), "——"),
    # 英文叙事「on 2026-07-18」「first hit 2026-07-06」「raised 2026-07-10」「delivered 2026-07-11」
    (re.compile(rf"[ \t]+on[ \t]+{_DATE}"), ""),
    (re.compile(rf"(first hit|raised|delivered|regression:)[ \t]+{_DATE}[ \t]*"), r"\1 "),
    # docstring/注释起始的日期「\"\"\"2026-07-16 实案抽象：」「# 2026-07-25 …」
    (re.compile(rf'("""|\'\'\')[ \t]*{_DATE}[ \t]*'), r"\1"),
    (re.compile(rf"(#+[ \t]*){_DATE}[ \t]+(?!起)"), r"\1"),
    # 句中孤立日期缀「（通用机制，2026-07-13）」已由前规则覆盖；剩余「，2026-07-13）」
    (re.compile(rf"[，,][ \t]*{_DATE}([ \t]*[）)])"), r"\1"),
    # 英文句中形态：「the 2026-07-29 incident」「observed 2026-07-19」「since 2026-08-02」
    (re.compile(rf"([Tt]he)[ \t]+{_DATE}[ \t]+"), r"\1 "),
    (re.compile(rf"(observed)[ \t]+{_DATE}[ \t]*"), r"\1 "),
    (re.compile(rf"[ \t]+since[ \t]+{_DATE}"), ""),
    (re.compile(rf"[Rr]eal[ \t]+{_DATE}[ \t]+case"), "real case"),
    (re.compile(rf"真实[ \t]*{_DATE}[ \t]*案例"), "真实案例"),
    # 兜底：剩余孤立日期直接摘除（pre-2026-07-10 之类带连字符的版本锚不受影响）
    (re.compile(rf"(?<![\d/-]){_DATE}(?![\d-])[ \t]*"), ""),
)

# 功能性日期行（解释代码里的真实默认值/兼容 cutover/账本政策锚）：整行跳过自动清理。
_COMMENT_DATE_KEEP = re.compile(
    rf"地平线|horizon|{_DATE}\s*之后|{_DATE}\s*起(?![:：])"
)


def _apply_comment_date_rules(fragment: str) -> str:
    if _COMMENT_DATE_KEEP.search(fragment):
        return fragment
    for pattern, replacement in _COMMENT_DATE_RULES:
        fragment = pattern.sub(replacement, fragment)
    return fragment


def _strip_py_comment_dates(text: str) -> tuple[str, int]:
    """重写 Python 源里注释与 docstring 的日期化出处；返回 (新文本, 改动行数)。"""
    import ast
    import io
    import tokenize as tok

    doc_lines: set[int] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text, 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant
            ) and isinstance(body[0].value.value, str):
                doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    comment_starts: dict[int, int] = {}
    try:
        for token in tok.generate_tokens(io.StringIO(text).readline):
            if token.type == tok.COMMENT:
                comment_starts[token.start[0]] = token.start[1]
    except tok.TokenizeError:
        return text, 0
    lines = text.splitlines(keepends=True)
    changed = 0
    for index, line in enumerate(lines, start=1):
        if "2026-" not in line:
            continue
        if index in doc_lines:
            new_line = _apply_comment_date_rules(line)
        elif index in comment_starts:
            col = comment_starts[index]
            new_line = line[:col] + _apply_comment_date_rules(line[col:])
        else:
            continue
        if new_line != line:
            if new_line.count("\n") != line.count("\n"):
                raise RuntimeError(f"date rule ate a newline: {line!r}")
            lines[index - 1] = new_line
            changed += 1
    return "".join(lines), changed


def _strip_sh_comment_dates(text: str) -> tuple[str, int]:
    lines = text.splitlines(keepends=True)
    changed = 0
    for index, line in enumerate(lines):
        if "2026-" not in line or not line.lstrip().startswith("#"):
            continue
        new_line = _apply_comment_date_rules(line)
        if new_line != line:
            if new_line.count("\n") != line.count("\n"):
                raise RuntimeError(f"date rule ate a newline: {line!r}")
            lines[index] = new_line
            changed += 1
    return "".join(lines), changed


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
            "schema_version": "source-subtitle-truth-ledger.v1",
            "notes": (
                "SOURCE_INTERVAL_TRUTH：已发布切片文本修复的唯一合法所有者。"
                "每条绑定源录像 sha + 毫秒区间 + replace_cue/replace_substring。"
                "本模板为空——真值属于你自己的录播。条目形状："
                "truth_id/source_recording_basename/source_sha256/interval_ms/"
                "action(replace_cue|replace_substring)/text/authority。"
            ),
            "entries": [],
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
        if "account_mid" in data:
            # 账号 UID 是部署专属标量：模板一律归零（对应 lane 会要求填真值）。
            data["account_mid"] = 0
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# assets/_template/：按 profiles/_template/profile.json 的资产清单，从默认
# profile 的同 key 资产派生"结构合法、内容为空"的骨架。文本资产给用途占位。

# 身份四件套：采访引导式模板——格式契约 + agent 该问的问题 + 示例频道真实条目
# 作示范（Ivan 授权直接取材）。从少量播种起步，随运营积累，不要求一次写完。
_TEMPLATE_TEXT_PLACEHOLDERS = {
    "glossary": (
        "# 频道词表（glossary）——专名与误听黑名单\n\n"
        "写法（解析器契约）：每行一个 `- ` 条目；条目里「不要写成 X / 不要改成 X /\n"
        "听成 X 等」子句会被解析成误听黑名单，喂给字幕术语 QA 门。\n\n"
        "## agent 首次配置时，向频道主人问这些\n\n"
        "- 主播名/自称/常用昵称？粉丝团怎么称呼？\n"
        "- 有哪些固定梗词、口癖、圈内黑话？\n"
        "- 哪些专名 ASR 经常听错？错成什么样？\n"
        "- 常联动的主播/团体叫什么？\n\n"
        "## 真实示例（来自示例频道李豆沙，仅示范写法——删掉换成你的）\n\n"
        "- 梗词：掏兜（薅弹幕/抢钱/偷学技能那套玩笑里的\"掏兜/偷学掏兜技能\"）。"
        "不要写成偷渡、淘兜、掏斗。\n"
        "- 品牌/话题词：和成天下（槟榔品牌）。ASR 常误写成\"合成天下/何成天下\"，"
        "一律写成和成天下。\n\n"
        "## 积累纪律\n\n"
        "从 3–5 条起步就够开跑；之后每次误听裁定、每个新梗随手加一条。\n"
        "这个文件是运营沉淀出来的，不是一次写完的。\n"
    ),
    "persona": (
        "# 主播 persona（标题/封面/语义裁决共用）\n\n"
        "## agent 首次配置时，问频道主人\n\n"
        "- 身份一句话（平台/分区/内容特色）？\n"
        "- 形象要点（发色/标志特征/衍生形象）？封面绝对禁止画错什么？\n"
        "- 地域/语言特色？性格气质里最常被标题抓住的是什么？\n\n"
        "## 真实示例（李豆沙，仅示范颗粒度——换成你的）\n\n"
        "- 身份：B站虚拟主播，温柔声线唱歌 + 高能杂谈双修。\n"
        "- （示例）形象：虚拟熊猫少女——白发+头顶自带小熊猫耳（不是头套）；封面里必须是视觉主角。\n"
        "- 地域：湖南长沙，直播里会飙长沙话。\n\n"
        "从几条起步随运营补充；封面/标题产出质量直接由这个文件决定。\n"
    ),
    "title_style": (
        "# 标题风格语料（few-shot）\n\n"
        "铁律：**只收人工定稿的标题**，机器生成的旧标题绝不入库（会污染词库）。\n"
        "长度与违禁词的硬门在 `title_policy.json`，这里只管风格与范例。\n\n"
        "## agent 首次配置时，问频道主人\n\n"
        "- 贴 3–5 条你满意的历史标题（没有就先空着用默认风格跑，出稿后人工定几条再回填）。\n"
        "- 你讨厌什么味道的标题？（示例频道的答案：一切机器味词）\n\n"
        "## 真实示例（李豆沙，示范\"结构\"而非内容）\n\n"
        "- （真例）档案标题可用冒号\"引语：反应\"结构：`李豆沙第999次澄清：我不是奶皮！`\n"
        "- 封面嵌字从不用冒号，分句用换行：`唱完《旅行的意义》\\n才发现伴奏像KTV录的`\n"
    ),
    "psplive_roster": (
        "# 关联主播名册\n\n"
        "由 crawler 维护（`scripts/crawl_psplive_roster.py`；参考部署默认装 cron）。\n"
        "先留空，或手填几条常联动主播起步。\n"
    ),
    "cover_identity_prompt": (
        "描述主播视觉身份的英文 prompt（AI 封面生成用）。要素：发色发型、标志性\n"
        "特征、衍生形象名、必须保真的细节、绝对禁止的改动。\n\n"
        "agent 首次配置时问频道主人：发色？标志特征（耳朵/饰品/服装）？有没有\n"
        "Q 版衍生形象？封面绝对不能出现什么？\n\n"
        "真实示例（李豆沙首句，示范颗粒度）：\n"
        "Li Dousha is a cute anime VTuber whose signature look is a white PANDA hood\n"
        "（示例）with PANDA EARS over WHITE hair; her chibi/derivative form is '小李' (little Li).\n"
    ),
}

# 非身份的工艺资产：默认直接给示例频道的完整内容（Ivan：默认值全给，想改再改），
# 但通用规则里的身份词必须占位化——只有标注「示例/判例/真例/锚点」的行保留真名
# 作教学材料；未标记行经身份映射后不得残留身份词（硬校验，残留即导出失败）。
_TEMPLATE_COPY_DEFAULT = ("slice_selection_metric", "subtitle_correction_principles")
# 平台级 JSON 数据（非频道身份）：模板整份给全（Ivan：礼物名是平台固定专名）。
_TEMPLATE_COPY_VERBATIM_JSON = ("gift_names",)

# 骨架逐键摘除：empty_entries 会把 dict 值清成 {}，但有的门把「键不存在」与
# 「空对象」区别对待——真值台账治理门只放行 None，空 {} 让**每个新频道的第一
# 支切片必炸**（二轮真实测试 F31 实锤）。示例资产没有该键的形态才是可用形态。
_TEMPLATE_SKELETON_DROP_KEYS: dict[str, tuple[str, ...]] = {
    "subtitle_truth_ledger": ("governance",),
}
_TEMPLATE_COPY_HEADER = (
    "> 模板默认值：源自示例频道的完整方法论，身份已占位（标注「示例」的行保留\n"
    "> 真实条目作示范）。可直接使用；要改就按你频道的判断改。\n\n"
)

_TEMPLATE_IDENTITY_MAP = (("李豆沙", "主播"),)
# prompt 注入类模板的扩展占位映射（长 token 在前，防子串半替换）。
_TEMPLATE_IDENTITY_MAP_EXTENDED = (
    ("豆沙歌", "歌切栏目"),
    ("礼墨Sumi", "圈内人物A"),
    ("露蒂丝", "圈内人物B"),
    ("lycoris", "圈内人物C"),
    ("shadowlee", "圈内人物D"),
    ("李墨素", "圈内人物B"),
    ("kmx", "粉丝团"),
    ("小李", "主播"),
    ("熊猫头", "身份符号"),
    ("熊猫", "身份符号"),
    ("侄女", "频道梗词"),
    ("百合(GL)", "本频道核心题材"),
    ("百合/GL", "本频道核心题材"),
    ("百合", "核心题材"),
    ("GL", "核心题材"),
    ("南町", "某生态频道"),
    ("豆沙", "主播"),
)
# 内容会注入 prompt 的模板键：连「示例/真例」标注行也必须占位化——别人频道
# 的人名与真实标题留在里面会真的影响新频道的选题与标题声音。
_TEMPLATE_PROMPT_INJECTED_KEYS = ("slice_selection_metric", "title_style")
_TEMPLATE_EXAMPLE_MARKERS = ("示例", "判例", "真例", "锚点")
_TEMPLATE_IDENTITY_CHECK = re.compile(
    r"李豆沙|小李|(?<!红)豆沙|kmx|礼墨|露蒂丝|lycoris|shadowlee|萱萱|Kaya|掏兜|侄女"
    r"|和成天下|熊猫|钢镚|shadow|伊索尔|(?<![A-Za-z])Sol(?![A-Za-z])|(?<!\d)142(?!\d)"
    r"|星汐|南町"
)

# 丰富模板的源文（docs/oss/templates/；起草规则：保留全部可参数化内容，
# 身份词只出现在标注「示例」的行）。存在即优先于其他生成分支。
_TEMPLATE_SOURCE_FILES = {
    "clip_opening_address": "clip_opening_address.json",
    "glossary": "glossary.txt",
    "timely_term_seeds": "timely_term_seeds.json",
    "timely_term_sources": "timely_term_sources.json",
    "persona": "persona.md",
    "title_style": "title_style.md",
    "cover_identity_prompt": "cover_identity_prompt.txt",
}
_TEMPLATE_SOURCE_DIR = REPO / "docs/oss/templates"

# 逐文件手工改写对（old 必须精确命中一次；作用于脱敏后的文本）。
_TEMPLATE_DOC_REWRITES: dict[str, tuple[tuple[str, str], ...]] = {
    "subtitle_correction_principles": (
        (
            "# 李豆沙字幕校正 prompt 原则",
            "# 字幕校正 prompt 原则",
        ),
        (
            "本文件是李豆沙切片**字幕文本校正 prompt** 的权威原则资产",
            "本文件是频道切片**字幕文本校正 prompt** 的权威原则资产",
        ),
        (
            "出处：由项目全历史积累汇总（glossary 文本规则、bilive-autoslice-publish / song-lyrics-timeline-aligner skill、CPA/AGY 提示词里反复踩坑写下的操作规则、以及各 memory）。",
            "出处：由示例频道全历史运营积累汇总。",
        ),
        (
            "- **专名平等铁律（维护者 2026-07-16，2026-07-28 补充边界）**：`kmx`、礼墨/Sumi、\n  萱萱卡娅/Kaya 以及名单里的任何**已登记词面彼此平等**。",
            "- **专名平等铁律**：名单里的任何**已登记词面彼此平等**（示例频道词面：kmx、礼墨/Sumi、萱萱卡娅/Kaya）。",
        ),
        (
            "- **元规则·自称误听**：凡来历不明、发音接近“小李/李豆沙/豆沙”的人名（刘彩/李彩/留下/刘禅/刘婵/里豆沙/李杜莎/流沙…），都是主播第三人称自称的误听，一律按语境修正为“小李/李豆沙”，**绝不当成刘禅等真实人名保留**。",
            "- **元规则·自称误听**：凡来历不明、发音接近主播各个自称的人名，都是主播第三人称自称的误听，一律按语境修正为对应自称，**绝不当成同音真实人名保留**（示例频道：刘彩/李彩/留下/刘禅/流沙…一律修正为“小李/李豆沙”）。",
        ),
        (
            "- **元规则·kmx**：只有本句实际音节、结构化弹幕/SC、同一接话链、重复专名槽或源时间真值支持时才写 **kmx**（一律小写）。停放熊/康姆叉/卡姆西/开姆克斯/秦伟雄/提莫怂/请问熊/KY小红等是已知误听面，只能提高 `kmx` 候选优先级，不能绕过本句证据；更不能因为 `kmx` 或礼墨/Sumi 在词表里，就让二者互相覆盖。单字「提」和常用词「提防」禁止作为全局误听面。",
            "- **元规则·粉丝团名**：只有本句实际音节、结构化弹幕/SC、同一接话链、重复专名槽或源时间真值支持时才写粉丝团名。已知误听面清单只能提高候选优先级，不能绕过本句证据；也不能因为两个词都在词表里就互相覆盖。单字和常用词禁止作为全局误听面（示例频道：粉丝团名 kmx 一律小写，误听面 停放熊/康姆叉/卡姆西 等；禁用面「提」「提防」）。",
        ),
        (
            "按整段话题选对的那个：讲抢钱/薅弹幕/偷学 → “掏兜”（非“偷渡”）；",
            "按整段话题选对的那个（示例频道：抢钱/薅弹幕语境 → 梗词“掏兜”，非“偷渡”）；",
        ),
        (
            "- **人设先验也是同音取舍的证据（维护者 2026-07-19，摸摸/么么案）**：李豆沙是可爱治愈妈妈型人设，不是诱惑型——安抚/哄人语境下的 mō/me 音优先判“**摸摸**”（像照顾小宝宝一样摸头），不是“么么”（飞吻）。同理其他同音多写在两个都通时按 persona 卡的性格倾向选；音频与人设先验冲突时以音频为准，拿不准只报不改。",
            "- **人设先验也是同音取舍的证据**：同音多写在两个都通时按 persona 卡的性格倾向选（示例频道为治愈妈妈型：安抚语境下的 mō 音优先判“摸摸”而非“么么”）；音频与人设先验冲突时以音频为准，拿不准只报不改。",
        ),
        (
            "- **主播用“她”（维护者 2026-07-10）**：明知是主播联动的场合，被指代的联动主播一律写**她/她们**（本圈主播默认女性没问题——联动三人全是女主播；“TA说是二”这类指打手势队友的都该是“她”）。同理，谈及其他 VTuber/主播时默认“她”，除非明确是男性。",
            "- **联动主播代词按你频道联动圈的实际构成默认**（示例频道联动圈全为女主播：被指代的联动主播一律写**她/她们**）；性别不明时按下面 TA 规则处理。",
        ),
        (
            "“钢镚”＝她对 2 元小额 SC 的玩梗称法，保留原词。",
            "主播对小额 SC 的玩梗称法保留原词（示例频道：“钢镚”＝2 元 SC）。",
        ),
        (
            "- 保留李豆沙的口癖、重复、吐槽语气、停顿感、直播间腔调，不要过度书面化。",
            "- 保留主播的口癖、重复、吐槽语气、停顿感、直播间腔调，不要过度书面化。",
        ),
        (
            "- **说话人别名**：对话中作为名字出现的精确词 `shadow` 是李豆沙的自称之一；说话人分离必须归入李豆沙，不得据此创造第四位说话人。不要把它与词表中的相关专名 `shadowlee` 混为一谈。",
            "- **说话人别名**：主播在词表登记的拼写别名自称必须归入主播本人，不得据此创造额外说话人（示例频道：`shadow` 是自称之一，勿与词表专名 `shadowlee` 混为一谈）。",
        ),
        (
            "- **自称平等铁律**（维护者 2026-07-13）：李豆沙/小李/豆沙是**平等的自称专名**，写音频里实际说的那一个，**绝不互相替换**（把清晰的「李豆沙」写成「小李」与听错同罪）。",
            "- **自称平等铁律**：主播的各个自称是**平等的自称专名**，写音频里实际说的那一个，**绝不互相替换**（示例频道：李豆沙/小李/豆沙；把清晰的「李豆沙」写成「小李」与听错同罪）。",
        ),
        (
            "「李豆沙」「小李」是平等自称专名，说哪个写哪个，绝不互换或\"统一风格\"。",
            "各个自称是平等专名，说哪个写哪个，绝不互换或\"统一风格\"。",
        ),
        (
            "匿名且性别无从判断的人（“我同学/一个朋友/那个人/kmx”，或查不到性别的名人）",
            "匿名且性别无从判断的人（示例：“我同学/一个朋友/那个人/kmx”，或查不到性别的名人）",
        ),
        (
            "（机制强制，`subtitle_fidelity` 守卫；2026-07-14 一九零/小李两案）",
            "（机制强制，`subtitle_fidelity` 守卫；示例：一九零/小李两类）",
        ),
        (
            "（维护者 2026-07-19，「只有kmx」案）",
            "（示例：「只有kmx」拼贴病）",
        ),
    ),
    "slice_selection_metric": (
        (
            "# 李豆沙切片选题 metric（通用 rubric 权威）",
            "# 切片选题 metric（通用 rubric 权威）\n"
            "\n"
            "> **必改项**：分层框架与七维算术直接可用，但**第一层「频道命脉题材」的\n"
            "> 定义必须换成你频道自己的**。本文件内容会注入语义召回 prompt，因此\n"
            "> 示例频道判例中的人名/题材已全部占位化（「圈内人物A」「粉丝团」等）\n"
            "> ——判例只示范打分思路，把占位角色换成你频道的真实对应者。",
        ),
        (
            "> v5，2026-07-22 维护者 校准 + Pro 独立复核：本文件继续定义偏好；",
            "> 本文件定义选题偏好；",
        ),
        (
            "> v4，2026-07-13 维护者 校准（学猫叫案：**装可爱表演 + 与观众互动感**必须更高分——\n> 「弹幕让她学猫叫，从喵喵、哈气演到嗷呜，最后急着强调自己是能一掌拍飞猫的熊」\n> 这类互动驱动的可爱演出 0.87 落选是错误。同日对账铁律：可恢复失败的原选手先复活，\n> 候补不上位、席位保留，分数高为准）。\n> v3，2026-07-06 维护者 校准（无人值守首批：生成器把「小猪自证」「幻想全职主播」排进 top5、\n> 把「弹幕拱她去找李墨素、她识破磕CP」划进落选。维护者：CP/百合类必须更高分）。\n> v2，2026-07-05 维护者 校准（「百合是工作」事件：高语义分题材被误判 niche 划走）。\n> 每一维都有 维护者 已上传/已拒绝的真实判例背书；改这里，语义召回 prompt 全局同步（勿在代码里另写一份）。",
            "> 本模板源自示例频道多轮真实上传/拒发判例的逐维校准；分层框架与七维算术通用，\n> **判例与人物请换成你频道自己的**。改这里，语义召回 prompt 全局同步（勿在代码里另写一份）。",
        ),
        (
            "不许因为低层候选「语义分略高 0.02」就把高层候选挤出 top-N（首批就是这么把李墨素 CP 挤掉的）。",
            "不许因为低层候选「语义分略高 0.02」就把高层候选挤出 top-N（示例频道判例：首批就是这样把第一层 CP 候选挤掉的）。",
        ),
        (
            "- **第一层（最高，\"看不腻\"）· 百合/CP/关系或完整互动表演链**：李豆沙的百合(GL)爱好、与具体人物(kmx/\n  礼墨Sumi/露蒂丝等)的 CP/磕CP/暧昧/关系拉扯/立场自白。**这类观众永远不腻**，是频道的命脉。\n  判例：「主播是一面镜子，李豆沙喜欢女生」「向kmx推荐恋死」「叛逆小李要喊kmx妈妈」；",
            "- **第一层（最高，\"看不腻\"）· 频道命脉题材（关系/立场或完整互动表演链）**：把本层定义成\n  **你频道观众永远看不腻的核心题材**。示例频道为百合(GL)与圈内人物(kmx/礼墨Sumi/露蒂丝等)\n  的 CP/关系拉扯/立场自白。判例（示例频道）：「主播是一面镜子，李豆沙喜欢女生」「向kmx推荐恋死」「叛逆小李要喊kmx妈妈」；",
        ),
        (
            "玩梗、自证（「不是小猪是小李」这类）。",
            "玩梗、自证（示例频道：「不是小猪是小李」这类）。",
        ),
        (
            "  7/6 应做未做：**「弹幕拱她去找礼墨Sumi，她识破你只是想磕CP」（CP）、「想成为真正拉拉还得五年高考十年模拟」（百合爱好）**。",
            "  判例（示例频道，应做未做）：**「弹幕拱她去找礼墨Sumi，她识破你只是想磕CP」（CP）、「想成为真正拉拉还得五年高考十年模拟」（百合爱好）**。",
        ),
        (
            "1. **围绕李豆沙本人（25%）**：不只表层身份符号（熊猫/名字/自我玩梗）",
            "1. **围绕主播本人（25%）**：不只表层身份符号（示例频道：熊猫/名字/自我玩梗）",
        ),
        (
            "   已上传真例：「李豆沙年龄歧视露蒂丝」「侄女卖姬太舒适了，直呼找到舒适区」。",
            "   已上传真例（示例频道）：「李豆沙年龄歧视露蒂丝」「侄女卖姬太舒适了，直呼找到舒适区」。",
        ),
        (
            "   已上传真例：「主播是一面镜子，李豆沙喜欢女生，kmx也喜欢女生」（GL 立场自白）。\n   **反例（维护者 拒发）**：「熊猫伪装成人类」「奶龙斗虫宇宙」——纯脑洞设定、无立场无关系钩子。",
            "   已上传真例（示例频道）：「主播是一面镜子，李豆沙喜欢女生，kmx也喜欢女生」。\n   反例·判例（示例频道，拒发）：「熊猫伪装成人类」「奶龙斗虫宇宙」——纯脑洞设定、无立场无关系钩子。",
        ),
        (
            "3. **受众兴趣对齐（15%）**：题材是不是她观众核心在意的——**百合/GL、她在追的作品、圈内人物（kmx/露蒂丝/lycoris/shadowlee）、生日/新皮肤/新表情等人设事件**。问「这是不是她观众在意的东西」，不只「这好不好笑」。\n   判例：「百合是工作」「百合漫画塞男角色」「小丑鼻子（生日事件）」被 维护者 点名要做；「shadowlee 的逆出道」「新哭哭表情」已上传。",
            "3. **受众兴趣对齐（15%）**：题材是不是**你频道观众**核心在意的——在追的作品、圈内人物、生日/新皮肤/新表情等人设事件。问「这是不是她观众在意的东西」，不只「这好不好笑」。\n   判例（示例频道）：核心题材为百合/GL 与圈内人物（kmx/露蒂丝/lycoris/shadowlee）；「百合是工作」「小丑鼻子（生日事件）」被点名要做；「shadowlee 的逆出道」「新哭哭表情」已上传。",
        ),
        (
            "4. **关系拉扯/互动（15%）**（历史上传最高频主题，24 条里 kmx 相关 5 条）：她与 kmx/弹幕/SC 的来回攻防、谁爱谁、辈分颠倒、互相推荐互相拆台。\n   已上传真例：「叛逆小李一定要喊kmx妈妈，kmx只好喊宝宝」「下播被kmx叫妈妈」「吵闹熊猫头到底爱不爱kmx，kmx反击早就不在意了」「向kmx推荐恋死，kmx反向推荐lycoris」。",
            "4. **关系拉扯/互动（15%）**（示例频道历史上传的最高频主题）：主播与粉丝团/弹幕/SC 的来回攻防、谁爱谁、辈分颠倒、互相推荐互相拆台。\n   已上传真例（示例频道）：「叛逆小李一定要喊kmx妈妈，kmx只好喊宝宝」「下播被kmx叫妈妈」「向kmx推荐恋死，kmx反向推荐lycoris」。",
        ),
        (
            "   **v4 扩展（维护者 2026-07-13 点名）：弹幕起哄→她照做表演的互动驱动内容属本维高分**——",
            "   **扩展：弹幕起哄→她照做表演的互动驱动内容属本维高分**——",
        ),
        (
            "   一掌拍飞猫的**熊**）是完整高分结构，不得因\"纯表演、无观点\"降档到第二层同质内容。\n   判例：「被问为什么总把自己猫塑，小李当场强调熊猫一掌就能打飞猫」conf 0.87 落选＝错误示范，此类应进层内前列。",
            "   反差收尾）是完整高分结构，不得因\"纯表演、无观点\"降档到第二层同质内容。\n   判例（示例频道）：「被问为什么总把自己猫塑，小李当场强调熊猫一掌就能打飞猫」落选＝错误示范，此类应进层内前列。",
        ),
        (
            "7.22 executable 对照锚点（机器值见 `selection_score_calibration.v1.json`）：",
            "对照锚点（机器值见 `selection_score_calibration.v1.json`；以下为示例频道锚点，换频道后用你自己的已审判例重建）：",
        ),
        (
            "- `auto_193450_3573_3665`「展示最喜欢的金发有角妹妹→被说像礼墨Sumi立刻否认→",
            "- 示例锚点 `auto_193450_3573_3665`「展示最喜欢的金发有角妹妹→被说像礼墨Sumi立刻否认→",
        ),
        (
            "- `auto_193450_5341_5459`「两人争论下播时没人挽留谁更可怜→李豆沙坦白留搭档只是",
            "- 示例锚点 `auto_193450_5341_5459`「两人争论下播时没人挽留谁更可怜→李豆沙坦白留搭档只是",
        ),
        (
            "## 歌切偏好（历史 7 首）\n\n温柔/哄睡/情绪钩子优先（虫儿飞哄睡、《宝贝》哄你睡觉、假装不知情的《年轮》、牵着你轮回）；",
            "## 歌切偏好\n\n按你频道的演唱风格定偏好（示例频道：温柔/哄睡/情绪钩子优先——虫儿飞哄睡、《宝贝》哄你睡觉、假装不知情的《年轮》）；",
        ),
        (
            "目录式铁律统一生成，固定为 `【李豆沙】豆沙歌，《歌名》`，本 metric 不得给标题添加 hook 或副标题。",
            "目录式铁律统一生成（profile 的歌切格式；示例频道为 `【李豆沙】豆沙歌，《歌名》`），本 metric 不得给标题添加 hook 或副标题。",
        ),
        (
            "  0.0x 就把第一层候选（CP/百合）挤出名额——首批 top5 就是这么错的。",
            "  0.0x 就把第一层候选挤出名额（示例频道判例：首批 top5 就是这么错的）。",
        ),
        (
            "## 歌切检测速判特征（维护者 2026-07-14）",
            "## 歌切检测速判特征",
        ),
        (
            "- **打 call 式弹幕 = 正在唱歌**：弹幕突然变成连刷\"李豆沙！李豆沙！\"这类**无实意、重复、打 call 节奏**的内容",
            "- **打 call 式弹幕 = 正在唱歌**：弹幕突然变成连刷主播名（示例：\"李豆沙！李豆沙！\"）这类**无实意、重复、打 call 节奏**的内容",
        ),
    ),
}


def _genericize_template_doc(key: str, text: str) -> str:
    for old, new in _TEMPLATE_DOC_REWRITES.get(key, ()):
        hits = text.count(old)
        if hits != 1:
            raise RuntimeError(
                f"template rewrite drifted ({key}): {hits} hits for {old[:50]!r}…"
            )
        text = text.replace(old, new)
    # 「标注示例行保留真名」只适用于纯教学文档。会**注入 prompt 的功能资产**
    # （选题 metric 进语义召回、标题风格进标题 prompt）里，别人频道的人名会真
    # 影响选题/标题（二轮真实测试 F12 实锤）——这类文件连标注行也占位化。
    no_exemption = key in _TEMPLATE_PROMPT_INJECTED_KEYS
    identity_map = (
        _TEMPLATE_IDENTITY_MAP + _TEMPLATE_IDENTITY_MAP_EXTENDED
        if no_exemption
        else _TEMPLATE_IDENTITY_MAP
    )
    out_lines: list[str] = []
    violations: list[str] = []
    for line in text.splitlines(keepends=True):
        marked = (not no_exemption) and any(
            marker in line for marker in _TEMPLATE_EXAMPLE_MARKERS
        )
        if not marked:
            for token, generic in identity_map:
                line = line.replace(token, generic)
            line = _apply_comment_date_rules(line)
            if _TEMPLATE_IDENTITY_CHECK.search(line):
                violations.append(line.strip()[:90])
        out_lines.append(line)
    if violations:
        raise RuntimeError(
            f"template {key} still has identity tokens on unmarked lines:\n  "
            + "\n  ".join(violations)
        )
    return "".join(out_lines)

_TEMPLATE_DIR_NOTES = {
    "fonts": (
        "烧录与封面渲染用字体。默认已含两个可再分发的开源字体（ZCOOL 快乐体、\n"
        "得意黑，均为 SIL OFL 许可），开箱即用；要换字体就替换文件本体。\n"
    ),
    "reviewed_subtitle_baselines": "已审字幕基线（同稿修复的精确重放用）：由发布/修复工具写入，人不手编。\n",
    "speaker_overrides": "逐候选说话人人工覆盖：由评审工具写入（apply_speaker_turn_overrides.py），无需手填。\n",
    "subtitle_regressions": "字幕回归钉子：每次修复裁定后由工具落盘，防止后续重跑回退已修文本。\n",
    "subtitle_text_overrides": "逐候选字幕文本覆盖：评审裁定的产物，由工具写入，无需手填。\n",
}


_IDENTITY_TOKENS = ("李豆沙", "lidousha", "豆沙", "kmx", "小李")
_PROFILE_SCHEMA_RE = re.compile(r"^lidousha([-.][A-Za-z0-9_.-]+)$")

# 这三个资产的骨架必须"可加载"而不只是"形状对"：loader 在 import 时就执行内容
# 契约（base_tags 非空、banned_regexes[0] 存在、prompt 三占位符），空壳会让新
# profile 连 --help 都起不来。值都是中性模板默认，供采用者替换。
_TEMPLATE_ASSET_JSON = {
    "upload_tag_policy": {
        "schema_version": "vtuber-slice.upload-tag-policy.v1",
        "base_tags": ["虚拟主播", "直播切片", "直播回放", "切片"],
        "max_tags_default": 12,
        "max_tag_chars": 20,
        "term_rules": [],
        "known_proper_surfaces_extra": [],
        "theme_allowed": [],
        "banned_content_tags": [],
        "content_prompt_template": (
            "你在为B站虚拟主播的直播切片选投稿标签(tag)。\n"
            "只出**通用、可搜索**的内容词——观众真的会在搜索框里搜的现成入口词"
            "（如：可爱 撒娇 破防 吐槽 社死 名场面 搞笑 反差萌 治愈 唱歌）；"
            "太专一于本条内容的描述词没人搜，一律不要。\n"
            "硬性规则：禁止输出任何人名/角色名/作品名/团体名（专名由另一套确定性"
            "规则处理）；每个标签2~6个字、不带标点；不与已定标签重复：{existing_tags}；"
            "每个标签配一句依据；宁缺毋滥。\n"
            '只输出严格 JSON：{{"tags": [{{"tag": "...", "why": "..."}}]}}\n\n'
            "标题: {title}\n\n字幕全文:\n{srt_text}"
        ),
    },
    "title_policy": {
        "schema_version": "vtuber-slice.title-policy.v1",
        "banned_hype_words": ["震惊"],
        "suffix_only_hype_words": ["哭"],
        "banned_filler_words": ["家人们"],
        "banned_regexes": ["秒[一-鿿]"],
        "min_length": 12,
        "max_length": 49,
        "max_attempts": 3,
        "generic_hook_words": ["直播", "精彩"],
        "meaningless_particles": ["的", "了"],
    },
    "branding_intro_manifest": {
        "schema_version": "REPLACE_ME-branding-intro.v2",
        "enabled": False,
        "policy": {},
        "rotation": {},
        "intros": [],
    },
}


def _scrub_identity_strings(payload: str) -> str:
    """模板骨架里不允许残留示例频道身份。

    profile-scoped schema 串保形替换（``lidousha-x.v1`` → ``REPLACE_ME-x.v1``，
    采用者改成 ``<自己的profile-id>-x.v1``）；其余含身份的字符串整值换占位符。
    """

    def scrub(node):
        if isinstance(node, dict):
            return {key: scrub(value) for key, value in node.items()}
        if isinstance(node, list):
            return [scrub(item) for item in node]
        if isinstance(node, str):
            shaped = _PROFILE_SCHEMA_RE.match(node)
            if shaped:
                return "REPLACE_ME" + shaped.group(1)
            if any(tok in node for tok in _IDENTITY_TOKENS):
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
        source_name = _TEMPLATE_SOURCE_FILES.get(key)
        if source_name and (_TEMPLATE_SOURCE_DIR / source_name).is_file():
            payload = (_TEMPLATE_SOURCE_DIR / source_name).read_text(encoding="utf-8")
            if key in _TEMPLATE_PROMPT_INJECTED_KEYS:
                payload = _genericize_template_doc(key, _sanitize_text(payload))
        elif key in _TEMPLATE_ASSET_JSON:
            payload = json.dumps(
                _TEMPLATE_ASSET_JSON[key], ensure_ascii=False, indent=2
            ) + "\n"
        elif key == "voiceprint_profile":
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
        elif key in _TEMPLATE_COPY_DEFAULT:
            source_rel = default_files.get(key)
            body = (default_root / source_rel).read_text(encoding="utf-8")
            payload = _TEMPLATE_COPY_HEADER + _genericize_template_doc(
                key, _sanitize_text(body)
            )
        elif key in _TEMPLATE_COPY_VERBATIM_JSON:
            source_rel = default_files.get(key)
            body = (default_root / source_rel).read_text(encoding="utf-8")
            payload = _scrub_identity_strings(_sanitize_text(body))
        elif key in _TEMPLATE_TEXT_PLACEHOLDERS:
            payload = _TEMPLATE_TEXT_PLACEHOLDERS[key]
        elif rel_name.endswith((".md", ".txt")):
            payload = f"# {key}\n\n按 assets/_template/README.md 与默认 profile 的同名资产填充。\n"
        else:
            source_rel = default_files.get(key)
            source = default_root / source_rel if source_rel else None
            payload = _template_payload("empty_entries", source or Path("/nonexistent"))
            drop_keys = _TEMPLATE_SKELETON_DROP_KEYS.get(key)
            if drop_keys:
                skeleton = json.loads(payload)
                for drop in drop_keys:
                    skeleton.pop(drop, None)
                payload = json.dumps(skeleton, ensure_ascii=False, indent=2) + "\n"
            payload = _scrub_identity_strings(_sanitize_text(payload))
        target.write_text(payload, encoding="utf-8")
        written += 1
    for dir_key, dir_rel in sorted(template_manifest["assets"]["directories"].items()):
        marker_dir = target_root / dir_rel
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "README.md").write_text(
            _TEMPLATE_DIR_NOTES.get(
                dir_key, "运行时目录骨架：管线工具写入的部署数据，无需手填。\n"
            ),
            encoding="utf-8",
        )
        written += 1
        if dir_key == "fonts":
            for font in sorted((default_root / "fonts").glob("*.ttf")):
                shutil.copy2(font, marker_dir / font.name)
                written += 1
    # 全模板身份硬校验：任何产出的文本文件里，未标注「示例/判例/真例/锚点」的行
    # 不得残留身份词（含 WS2 扩充禁词表）。
    violations: list[str] = []
    for produced in target_root.rglob("*"):
        if not produced.is_file() or produced.suffix in BINARY_SUFFIXES:
            continue
        for line_number, line in enumerate(
            produced.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if any(marker in line for marker in _TEMPLATE_EXAMPLE_MARKERS):
                continue
            if _TEMPLATE_IDENTITY_CHECK.search(line):
                violations.append(
                    f"{produced.relative_to(target_root)}:{line_number}: {line.strip()[:80]}"
                )
    if violations:
        raise RuntimeError(
            "template assets still carry identity tokens on unmarked lines:\n  "
            + "\n  ".join(violations[:20])
        )

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
        "  自己 enroll 后用 `scripts/install_voiceprints.py` 安装。\n"
        "\n"
        "## 分层：谁在什么时候填什么\n"
        "\n"
        "- **层 0 · 默认给全（agent 独立完成，开箱即用）**：`fonts/` 两个开源字体\n"
        "  （直接用）、`title_policy.json`、`upload_tag_policy.json`、\n"
        "  `subtitle_correction_principles.md`（示例频道完整口径，可改）、\n"
        "  `bilibili_gift_names.v1.json`（B站平台礼物专名，直接用）、`intro/`（默认关）、\n"
        "  `entity_confusables.json`/`known_songs.json`/`clip_opening_address.json`\n"
        "  （积累类，空起步）。\n"
        "- **层 0.5 · 框架直用、定义必改**：`slice_selection_metric.md`——分层框架与\n"
        "  七维算术通用，但**第一层「频道命脉题材」的定义必须换成你频道自己的**；\n"
        "  该文件会注入选题 prompt，判例人名已全部占位化——把占位角色换成你\n"
        "  频道的真实对应者即可。\n"
        "- **层 1 · 先问后写（agent 拿问题清单问频道主人，答完代写）**：\n"
        "  `glossary.txt`、`persona.md`、`title_style.md`、`cover_identity_prompt.txt`\n"
        "  ——每个文件内已写好该问的问题与示例；先写 3–5 条就能开跑，之后边用\n"
        "  边攒，**不要求一次写完**。频道主人不在旁边时的代查证据源：\n"
        "  B 站 `live_user/v1/Master/info?uid=` 免签给 room_id/粉丝勋章名（粉丝团\n"
        "  称呼）；萌娘百科条目；**抽真实直播帧取证**（外貌/装饰以帧为准，文字\n"
        "  资料常错）。代填的条目标注待频道主人拍板。\n"
        "  **封面外貌事实三处必须同步改**：`profile.json` 的 `identity.cover_identity`\n"
        "  九键、`persona.md`、`cover_identity_prompt.txt`——只改其一，封面身份\n"
        "  终检会按不一致的那份把成品拦下（先抽帧、后写、三处一起写）。\n"
        "- **层 2 · 你给种子，crawler 代填**：`timely_term_seeds/sources` → \n"
        "  `timely_terms`、`psplive_roster_sources` → `psplive_roster`、\n"
        "  `topic_entity_graph`（参考部署默认装 cron；不走 deploy 就手动跑或自配）。\n"
        "- **层 3 · 运行时/人工裁定自己长出来**：`subtitle_truth_ledger`、\n"
        "  `session_relation_ledger`、`published_songs`、`speech_memory_ledger`、\n"
        "  `selection_score_calibration`（随运营积累标定锚点）、各 manual/cover\n"
        "  override（`manual_title_overrides`/`manual_archive_metadata`/\n"
        "  `cover_reference_overrides`）与各评审目录。\n"
        "- **层 4 · 用到对应功能才配**：`voiceprint_profile`（声纹栈）、启用片头。\n"
        "\n"
        "## 首跑前最小清单\n"
        "\n"
        "- `upload_tag_policy.json` / `title_policy.json` 骨架带中性默认值，**开箱可\n"
        "  加载**（loader 在 import 时执行内容契约：base_tags 非空、banned_regexes\n"
        "  槽位 0 存在、tag prompt 必须保留 {existing_tags}/{title}/{srt_text} 三个\n"
        "  占位符）。先跑通，再替换成你的口径。\n"
        "- `intro/branding_intro.v1.json` 默认 `enabled: false`（关闭片头）。启用前把\n"
        "  schema_version 的 `REPLACE_ME` 改成你的 profile-id，并按\n"
        "  `src/autoslice/branding_intro.py` 的契约补 `intros`。\n"
        "- 各 JSON 中 `REPLACE_ME-…` 形态的 schema_version 都指 profile-scoped\n"
        "  schema：改成 `<你的profile-id>-…`。\n"
        "- 测试套件以**默认 profile** 为基准：跑 `pytest` 时不要设置\n"
        "  `AUTOSLICE_PROFILE`（约 11 个用例直接断言示例 profile 的资产内容）。\n",
        encoding="utf-8",
    )
    return written + 1


def main() -> int:
    out_root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    tracked = tracked_files()
    unknown_pre = [rel for rel in tracked if classify(rel) == "unknown"]
    if unknown_pre:
        print("UNCLASSIFIED paths — refusing BEFORE touching the output dir:")
        for rel in unknown_pre:
            print("  ", rel)
        return 2
    final_root = out_root
    out_root = final_root.with_name(final_root.name + ".building")
    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)

    kept, templated, stripped, unknown = [], [], [], []
    rootmap_entries: list[str] = []
    templated_dirs_seen = set()
    for rel in tracked:
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
        elif kind == "template_source":
            kept.append(rel)  # 记账；内容由 build_template_assets 读取
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

    # 表情包媒体随仓发布（Ivan 2026-08-01 拍板）：50 个 png 按库清单逐一核 sha 后
    # 复制；缺失或漂移即导出失败。默认解析路径=仓内 assets/<profile>/emote。
    emote_lib = json.loads(
        (REPO / "assets/lidousha/emote_library.v1.json").read_text(encoding="utf-8")
    )
    emote_src = REPO / "assets/lidousha/emote"
    emote_files = 0
    for entry in emote_lib.get("emotes") or emote_lib.get("entries") or []:
        for file_key, sha_key in (("file", None), ("hd_file", "hd_sha256")):
            rel_name = entry.get(file_key)
            if not rel_name:
                continue
            source = emote_src / rel_name
            if not source.is_file():
                print(f"EMOTE MEDIA MISSING: {source}")
                return 6
            if sha_key and entry.get(sha_key):
                import hashlib as _hashlib

                if _hashlib.sha256(source.read_bytes()).hexdigest() != entry[sha_key]:
                    print(f"EMOTE MEDIA SHA DRIFT: {source}")
                    return 6
            target = out_root / "assets/lidousha/emote" / rel_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            emote_files += 1

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

    comment_date_lines = 0
    residual_comment_dates: list[str] = []
    for path in out_root.rglob("*"):
        if not path.is_file():
            continue
        rel_str = str(path.relative_to(out_root))
        if not rel_str.startswith(("src/", "scripts/", "tests/", "ops/")):
            continue
        if path.suffix == ".py":
            stripper = _strip_py_comment_dates
        elif path.suffix == ".sh" or rel_str.endswith(".cron"):
            stripper = _strip_sh_comment_dates
        else:
            continue
        text = path.read_text(encoding="utf-8")
        new_text, changed = stripper(text)
        if changed:
            path.write_text(new_text, encoding="utf-8")
            comment_date_lines += changed
        if "2026-" in new_text:
            probe, _ = stripper(new_text)
            for line_number, line in enumerate(new_text.splitlines(), start=1):
                if "2026-" in line and (
                    line.lstrip().startswith("#") or '"""' in line or "'''" in line
                ):
                    residual_comment_dates.append(f"{rel_str}:{line_number}: {line.strip()[:90]}")
            del probe

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
        if not str(p.relative_to(out_root)).startswith(("assets/", "profiles/", "tests/lidousha"))
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
        "emote_files": emote_files,
        "patches": len(PATCHES),
        "comment_date_lines_rewritten": comment_date_lines,
        "comment_date_residual": len(residual_comment_dates),
        "lidousha_flow_files": lidousha_flow_files,
        "forbidden_hits": len(violations),
    }
    print(json.dumps(report, ensure_ascii=False))
    if residual_comment_dates:
        print("residual dated comments (manual triage):")
        for row in residual_comment_dates[:25]:
            print("  ", row)
    if violations:
        print("FORBIDDEN CONTENT — snapshot NOT publishable yet:")
        for row in violations[:60]:
            print("  ", row)
        return 3
    # 全部检查通过后才原子替换正式目录；任何失败都不会破坏既有快照。
    if final_root.exists():
        shutil.rmtree(final_root)
    out_root.rename(final_root)
    print(f"clean snapshot at {final_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
