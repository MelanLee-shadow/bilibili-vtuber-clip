# 41 语义修复引擎（[40-subtitle-text.md](40-subtitle-text.md) 的核心子权威）

设计原则（Ivan 2026-07-19，源自 7/18 交付事故复盘 + 业界调研）：

## 分层架构

```
检测层（谁发现错）          裁决层（谁决定改不改）        强制层（谁保证落地）
─────────────────          ─────────────────────        ─────────────────
确定性词表/弹幕/ledger  →  见证人规则(fidelity)      →  choke-point 替换
终审审片员 LLM          →  声学仲裁(两候选比较)      →  带伤交付闸
漏听 recall 检查        →  fail-closed 默认保留原文  →  runner 有界重试
```

1. **检测≠裁决≠落地**，三层独立记账。7/18 事故的教训：检测层 6/6 全对，落地层全军覆没——事后审计必须能分清是哪层坏了（review-flags 的 `infra_unresolved` 字段就是这个用途）。
2. **LLM 只报不改**（审片员）；改动按分层裁决落地（见下）；插入只对 source_backed_entity 放开（kmx 漏听案）。
3. **裁决分层（Ivan 2026-07-19「不能绑死 Gemini 额度、也不能老用付费key」）**：
   - **T0 确定性**：hard canon / 源真值 ledger / 弹幕逐字——零模型。
   - **T0.5 同音自动应用**：拼音无调全等（`homophone_fix`）——零外部调用。
   - **T1 见证近音**（`witnessed_near_homophone_fix`）：修复词面有词表/本片转写/结构化弹幕见证（`source_surface` 机制）+ 拼音相似度 ≥0.45 + **suspect 不是注册实体词面** → 纯文本应用，零外部调用。7/18 六案有五案属此层。
   - **T3 声学仲裁**：只剩实体 vs 实体选边（kmx/乒乓球、梦限大/Mujica 保向铁律）与拼音强变形（醉堆→这一堆型）。量级 ~1/10。
   - T2 备选未实施：免费 BCUT 对争议 span 重转写+拼音距离比对（「穷人声学见证」），T3 仍嫌贵时再上。
4. **infra 失败不是裁决**：provider 额度耗尽导致的 UNCERTAIN 不许当终局，producer 以 `FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED` 拒绝带伤交付，runner 按 provider_transient 有界重试。
5. **付费兜底**：同项失败≥3轮即可触发（额度类失败可同 run 连续补轮，`quota_exhausted_round`），每笔入帐。**Ivan 2026-07-19 明确否决冷却期类附加门**——控制付费用量靠 T1 分层缩减声学仲裁需求本身，不靠拖延付费。
6. **方言保真**：长沙话方言词（glossary「长沙话方言词保护」节）修复方向 = 方言原字 > 普通话意译 > 保留误听；通用中文纠错「归一到普通话」的默认方向在方言词上是反的。
7. **漏听 recall**：选片钩子/弹幕/SC 里的词表专名在字幕零出现 → 审片员漏听检查（prompt 规则7）→ 插入提案 → 声学仲裁（插入永远走 T3，不进 T1）。**已知盲区（2026-07-19 合并条实证）**：专名在片内它处出现过时零出现触发器不响，单句漏听无人怀疑（kmx 0:49 案，最终走 Ivan 审定 ledger 钉子）。改成逐句怀疑会假阳性爆炸；候选方向是「称呼/接话/突击等强语境句位 + 专名句位模板」的窄触发，进欠账。

## 模块指针

| 职责 | 模块 |
|---|---|
| 词表/专名权威 | `term_authority.py`、`assets/lidousha/glossary.txt`（含方言节）、`entity_confusables.json` |
| 弹幕/SC 证据修复 | `chat_proposals.py`、`chat_repair.py`（阈值 score≥0.68/coverage≥0.60/precision≥0.52） |
| 见证人规则 | `subtitle_fidelity.py`（glossary/拼音同音/音频见证/重复见证四选一，否则 revert） |
| 终审审片员 | `final_review_auditor.py`（发现器；同音自动应用+声学仲裁路由+插入契约） |
| 声学仲裁 | `entity_audio_verifier.py`（黑帧片段强制选边；quota 轮次+付费兜底） |
| 源真值 ledger | `source_subtitle_truth.py` + `subtitle_truth_ledger.v1.json`（Ivan 审定钉子，唯一不受 provider 故障影响的通道） |
| 付费兜底政策 | `gemini_backup_policy.py`（≥3轮 strikes + 日帽 + 入帐） |
| 梗词铁律 | `surface_canon.py`（直女→侄女等 hard canon） |

## 已知结构性欠账（按性价比排序，做前先读调研）

1. ~~付费兜底不可达~~（2026-07-19 已修，`2da11e9`）
2. ~~infra-UNCERTAIN 带伤交付~~（同上已修）
3. ~~方言零覆盖~~（同上已修，词表持续扩充）
4. ~~专名零召回无修复通道~~（同上已修：source-backed 插入）
5. **专名匹配纯精确**：glossary 误听面靠人工枚举（核酸天下案：新变体漏网）。方向：拼音编辑距离/音节序列匹配作为候选发现层（pypinyin 已装，`song_name_pin.py` 有先例），发现≠裁决，候选仍走声学仲裁。
6. **本地可疑度粗筛缺失**：调研结论第一优先级（PPL/pycorrector 漏斗），把昂贵 LLM/音频调用集中到高可疑行。当前每片全量过审片员，成本可接受，暂缓。
7. **语义QA评审文本≠最终交付文本**（选题阶段 vs 文本终稿时间线分离）——审片员已覆盖终稿面，风险有限，记录在案。
8. **歌词正文绕过词表链**（LRC 是歌词权威，影响面小，记录在案）。

## 业界调研要点（2026-07-19，详见 commit 记录）

- RLLM-CF（prompt-only 四步分解：预检→定位→拟音→验证，验证不过保留原文）与 LIR-ASR（拼音一致性硬约束候选池，消融证明该约束是防过改写的关键）与本引擎架构同构，可直接借鉴 prompt 设计。
- ASR-EC 基准警示：中文裸 prompting 纠错无效甚至有害——印证「LLM 只报不改+声学仲裁」路线。
- 必剪/剪映黑盒无热词接口；软热词（词表+钩子+弹幕实体注入 prompt）是现实替代，已落地。
- 长期选项：自建 FunASR SeACo-Paraformer/Qwen3-ASR（方言优先+热词解码），残留错误率压不下去再评估。
