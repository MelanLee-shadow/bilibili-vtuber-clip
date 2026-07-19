# 40 字幕文本链

本文件是字幕文本步骤的**分步权威**。入口：`src/autoslice/producer_text_pipeline.py::run_text_pipeline`
（生产唯一调用方 `scripts/produce_slice_package.py`）。

## 阶段顺序（真实调用序）

| # | 子阶段 | 模块 | 作用 |
|---|---|---|---|
| 1 | `_collect_timeline_chat` | `producer_chat_input.py` | 弹幕/SC 证据装载（XML 优先，jsonl 降级） |
| 2 | `_transcribe_draft` | ASR 适配器 + `session_topic_authority.py` + `term_boundary.py` | 转写草稿 + 场级话题实体吸收 + 词边界统一 |
| 3 | `_build_entity_verification_context` | `read_aloud_llm_verifier.py`、`entity_audio_verifier.py` | 实体仲裁闭包（人工 override → 朗读 LLM → 音频仲裁） |
| 4 | `_apply_entity_authority` | `self_reference_absorption.py`、`chat_proposals.py`、`subtitle_fidelity.py`、`chat_repair.py` | 自称吸收、弹幕权威修复、数字事实门、音频实体落地 |
| 5 | `_run_final_review` | `final_review_auditor.py` | LLM 终审 + 逐条声学复核路由 |
| 6 | `_finalize_text_evidence` | `subtitle_fidelity.py` 各 guard、`surface_canon.py`、`song_name_pin.py`、`source_subtitle_truth.py` | 语言保持/书名号/标点门、梗词定形、歌名钉、源真值投影（FAILED 即 SystemExit） |

语义修复引擎（专名/方言/语境不合适度）的设计与规则见
[41-semantic-repair.md](41-semantic-repair.md)——那是本步的核心子权威。

## 硬约束

- 上传语义修复永不放行未见证改写：改动必须有 glossary/拼音同音/弹幕/音频见证其一（`subtitle_fidelity.py` verdict 逻辑），否则 revert。
- 交付 `.srt`/`.ass` 走内容时间轴；片头偏移只记录在 `burned_preview.branding_intro.intro_offset_ms`（见 [80-package-delivery.md](80-package-delivery.md)）。
- 说话人统一李豆沙色（数据积累期，Ivan 2026-07-13），说话人不确定绝不拒发。
