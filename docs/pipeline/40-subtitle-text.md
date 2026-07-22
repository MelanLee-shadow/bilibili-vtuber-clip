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
- 音频二听只证明读音，不证明同音人名/称呼的汉字写法：带「小/老/阿」前缀或「神/老师/姐/哥/酱/桑/君/总/宝」后缀的同音换字，没有词表/源真值等文字权威就只回退该换字跨度，同 cue 其余有见证修复仍保留。`什么/怎么/为什么/谁/哪里/多少` 等疑问意图族发生变化则整 cue 回退，禁止把逐字字幕改成解释性提问。
- 源真值支持 `replace_cue` / `replace_substring` / `drop_cue`；`drop_cue` 只允许删除被 source-timeline 真值半开区间完整包含的 cue（仅容忍 120ms 编码/SRT 边界漂移）。任何实质性跨界均记 `DROP_CUE_STRADDLES_TRUTH_INTERVAL` 并 fail closed，禁止按“有重叠”整条删除。
- `replace_cue` 可附带经人工/黑屏纯音频听证确认的绝对源时间轴 `spoken_start_ms`：用于删除幻听前缀后把保留口播的字幕起点同步收紧。它只接受真值区间内、且唯一目标 cue 内的起点；多 cue、越界或非整型一律 fail closed。VAD 未检出本身仍不得推导这个起点。
- 已审字幕的重交付可在 spec 中声明哈希绑定的 `subtitle_redelivery_baseline`（仅 `--reuse-cover`）：源真值窗仍可改，窗外只继承旧版文本而保留本轮 BCUT 时间；新旧 cue 必须按时间强重叠一一映射。若 fresh ASR 把 `replace_substring` 的目标整段漏掉，文本阶段只可暂缓该失败，终稿必须先从绑定旧稿恢复稳定词面、再重放更高权威的源真值并重新跑结构门，仍不满足就拒发。哈希漂移、漏 cue、合并/拆分、歧义映射或二次真值失败一律拒发，禁止靠重掷模型碰运气。
- 书名号结构门在所有文本 authority（含源真值）之后再跑一次；合法跨 cue 配对单独记账，真正的 `UNRESOLVED_COMPLEX_IMBALANCE` 必须阻断 `review_ready`。
- source-language 整 cue 回退只适用于无中文的 Latin-language cue；中文口播里的 NN/L、NNLL、LLNNHHB 等 CP 顺序公式以及大写 `TA` 代词是标签/中文代词，不是外语段落，不得触发 mixed-language 拒发，也不得因 token 数下降把已删除的跨 cue 回声整句恢复。
- 交付 `.srt`/`.ass` 走内容时间轴；片头偏移只记录在 `burned_preview.branding_intro.intro_offset_ms`（见 [80-package-delivery.md](80-package-delivery.md)）。
- 说话人统一李豆沙色（数据积累期，Ivan 2026-07-13），说话人不确定绝不拒发。
