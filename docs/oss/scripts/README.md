# scripts/ 地图

62 个脚本按 lane 分组。刚上手真正会用到的只有七个：
`preflight.py`（部署体检：字体/ffmpeg/目录/凭据/VAD 一次查清）、
`validate_channel_profile.py`（配 profile）、`session_autoslice.py`（runner/冒烟）、
`produce_slice_package.py`（单候选产线）、`audit_review_package.py`（包审计）、
`build_final_human_review.py`（终审回执）、`authorized_upload.py`（唯一上传入口）。

## 录制 / 接入

| 脚本 | 用途 |
|---|---|
| `mount_watchdog.sh` | 云盘挂载看门狗（挂载死亡 → 冻结消费者，防吃坏字节） |
| `clouddrive_upload_fatal_sentinel.sh` | CloudFS 上传 Fatal 哨兵（录制字节丢失预警） |
| `danmaku_backup_listener.py` | 独立弹幕/礼物/SC 备份监听（录播姬挂掉时的证据兜底） |
| `free_asr_client.py` | 免费 ASR 聚合（必剪主源 + 剪映备源；词级毫秒时间轴） |
| `silero_vad_spans.py` | silero VAD 语音跨度导出（供时间轴 QA） |
| `transcribe_live_talk_via_agy.sh` / `transcribe_live_song_via_agy.sh` | 远端 AGY 听写作业投递（需 AGY，见 README 术语表） |
| `gemini_slice_jingting.py` | 分块精听转写 runner（profile-aware） |

## 产线（选题 → 校对 → 成品）

| 脚本 | 用途 |
|---|---|
| `session_autoslice.py` | 无人值守 runner：下播 → 召回 → 逐候选产包（`--once` cron 入口，`--smoke-segment` 单段冒烟） |
| `produce_slice_package.py` | 单候选产线（spec 驱动；字幕/说话人/标题/封面全链） |
| `run_auto_review_shadow_pipeline.py` | 共享自动评审管线库（多脚本 import 它） |
| `run_full_session_selector_cpa_shadow.py` | 全场语义选题 shadow 跑批 |
| `auto_review_shadow_daemon.py` | 评审 shadow 守护（轮询新交付） |
| `cpa_semantic_review.py` / `cpa_semantic_qa_llm.py` / `run_cpa_semantic_qa.py` | CPA 语义审查/QA 的三个入口（观众视角审查、LLM QA、mock 产物） |
| `profile_glossary_terms.py` | 把 profile 词表解析成 CPA 术语 QA 输入 |
| `crawl_timely_terms.py` / `crawl_psplive_roster.py` / `crawl_topic_entity_graph.py` | 词表/名册/实体图资产刷新（profile 驱动） |
| `huozi_luanshua.py` | 「活字乱刷」语音重组 lane（plan/verify/render 三段，证据绑定） |

## 评审 / 校对

| 脚本 | 用途 |
|---|---|
| `build_daily_review_manifest.py` | 按日构建人工评审清单 |
| `build_song_review_manifest.py` | 歌切评审清单 |
| `build_recovery_review_manifest.py` | 恢复流评审清单 |
| `build_final_human_review.py` | 终审证据模板/回执（create-only） |
| `build_cover_only_audit_scope.py` | 封面单项审计范围 |
| `audit_review_package.py` | 审片包 canonical 审计器（发布前必过） |
| `apply_subtitle_correction.py` / `apply_subtitle_text_overrides.py` | 应用已裁决的字幕修正/文本覆盖 |
| `apply_speaker_turn_overrides.py` | 应用已评审的说话人 turn |
| `plan_operator_subtitle_correction.py` | 生成操作员字幕修正计划 |
| `batch_speaker_review.py` / `build_speaker_blind_review.py` / `evaluate_speaker_phase1.py` | 说话人评审跑批 / 盲听页面 / 盲听评测 |
| `score_blind_subtitle.py` | 盲评字幕对照打分 |
| `list_gates.py` | 列出全部质量门（静态分析） |

## 修复 / 恢复（fail-closed 的逃生舱，全部 plan/hash 驱动）

| 脚本 | 用途 |
|---|---|
| `plan_recovery_review_rerun.py` | 建 no-upload 恢复重跑队列 |
| `resume_frozen_talk_package.py` | 从已审文本面确定性续产一个 talk 包 |
| `revive_rejected_candidates.py` | 复活 candidate_rejected（唯一 sanctioned 通道） |
| `rescue_from_official_replay.py` | 用官方回放重建丢失的录制段（分段时间映射） |
| `repair_reviewed_covers.py` / `repair_screenshot_cover.py` | 按已审计划修封面 / 截图路线封面重组 |
| `regenerate_channel_cover.py` | 重出单候选封面（profile 的 cover_regenerator 工具位） |
| `swap_video_p.py` | 同 BV 换源（biliup append 后置换 P） |

## 发布 / 稿件维护

| 脚本 | 用途 |
|---|---|
| `authorized_upload.py` | **唯一上传授权入口**（manifest 冻结 + 幂等账本 + 同 BV 修复 lane） |
| `do_upload.sh` | biliup 执行器（拒绝裸调，必须经 authorized_upload.py） |
| `suggest_upload_tags.py` | 按成品字幕出 tag 建议（policy 门） |
| `bili_archive_tool.py` | 已发稿件维护统一 CLI（view/编辑/换封面/入合集） |
| `bili_cover_edit.py` / `bili_update_tags.py` | 单项换封面 / 换 tag |

## 部署 / 运维

| 脚本 | 用途 |
|---|---|
| `preflight.py` | 部署体检：python/ffmpeg/字体/profile 资产/凭据 env/VAD/self-ssh 一次查清（`--live` 才真调 CPA） |
| `deploy_autoslice.sh` | 参考部署（校验→同步→回滚账本；按你的主机改写） |
| `sync_profile_assets.sh` | 同步 profile 资产到部署主机 |
| `validate_channel_profile.py` | profile 校验器（`--config-only` 起步，READY 才可跑） |
| `install_voiceprints.py` | 安装声纹参考（READY 的 voiceprint profile → 部署目录） |
| `pull_recent_autoslice.py` | 从部署主机拉回近期交付 |
| `slice_monitor.py` / `slice_monitor.sh` | 录制+切片监控（报告文件为唯一告警通道）与 launchd 包装 |
| `llm_via_cpa.sh` / `llm_via_free_groq.sh` | LLM 调用小工具（CPA / 免费 groq 层） |

## 评估（开发者工具）

| 脚本 | 用途 |
|---|---|
| `build_autoslice_eval_snapshot.py` | 从 git tree 构建不可变 eval 运行时 |
| `run_eval_base_once.sh` | 在 eval 沙箱里安全跑一轮 runner |
