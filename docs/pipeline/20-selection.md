# 20 候选召回与选题

本文件是选题步骤的**分步权威**。metric 细则的强权威是
`assets/lidousha/slice_selection_metric.md`（当前 v5）。模型只给档位和 cue 证据；
`src/autoslice/selection_scorecard.py` 负责 Tier 准入、固定算术与最终排序，禁止用 confidence 代替价值分。

## 链路

1. 语义召回为主：`select_semantic_session_candidates`（`src/autoslice/semantic_candidate_selector.py`），关键词只兜底（Ivan 2026-07-03）。
   - 单段超过 45 分钟时，必须按 30 分钟核心窗 + 前后各 2 分钟上下文重叠分别召回，再在整段范围按信心分去重排序；禁止把 2 小时字幕塞进一次调用后，把模型只返回前半场少数候选误当作整场无内容。
   - 每段候选池上限 12 是召回余量，不是交付配额；最终仍按每场 talk top-5 上限、scorecard 硬 Tier 与最低信心门筛选，不为凑数降门槛。
   - 已选候选若被边界、说话人或字幕 authority 的确定性安全门拒绝，保留拒绝记录但立即从已排序 backlog 补位；只有 provider/运行时等可恢复故障才占位等待，不能因一个不可交付候选把全场最终数量永久压低。
2. 弹幕热度 hints：`danmaku_evidence.py`（爆发窗口，选题信号，不改文本）。
3. CPA 观众视角审查：每个候选无条件过 `scripts/cpa_semantic_qa_llm.py` 判官（`viewer_context_ok` 语境自足性 + 自动扩窗建议），失败即 BLOCK（`live_source_review.py::_merge_cpa_semantic_review_into_decision`）。
4. 候选是内容锚点不是最终边界；边界由 [30-boundary.md](30-boundary.md) 决定。

## 候选状态与人工点选

- `picks`、`pending_talk`、`talk_backlog`、拒绝记录必须互斥投影；已经成为
  `CURRENT + COMPLIANT` 成品或终态拒绝的 candidate 不得再次出现在“当前候补”。
  `not_selected` 是历史 prose，不是状态 authority，也不得参与补位。
- 每次拒绝必须保留 `failure_stage + rejection_reason + failure_evidence`；报告把它放在
  “候选门禁拒绝”，不能混进成品表只显示一个无解释的 `candidate_rejected`。
- 用户点名候补不篡改分数：追加 `USER_SELECTION_OVERRIDE`，记录原始 scorecard、
  baseline rank、实际 slot、被越过的基线候选与人工 authority。用户说外部已有重复但
  没有 BV 时，可直接 `SUPPRESSED_BY_USER`，但重复 claim 只能是
  `USER_ASSERTED_UNVERIFIED`，不得伪装成已验证站外重复。

## 同主题合并（Ivan 2026-07-18 切片案 → 2026-07-19 新规）

- **主题一致的候选尽量合并成一个切片**，不许把同一话题在源时间轴上相邻/交错的两段切成两条成品（案例：kmx 称呼两条切片同主题被分开切）。
- 机制：候选定稿前跑主题聚合 pass——相邻候选（源时间间隔小）且主题/实体重叠高的合并为一个候选窗口，交给边界解析定最终起止。实现见 `src/autoslice/semantic_candidate_selector.py` 的 merge pass（落地记录 `merge_audit`）。
- 合并后的标题按合并主题起，不是拼接两个子标题。

## metric 硬维度（详见 metric 资产）

- 七维权重固定为 25/20/15/15/10/10/5；先验收 Tier 证据，再按有效分排序，最后才以 confidence 破同分。
- 围绕本人（含态度/立场/情绪，不只名字梗）；观点强度与受众兴趣（百合/GL）是硬维度；高语义分不许因 niche 压低。
- 报告必须同时显示 Tier 与有效分；缺 scorecard 的旧候选只能作为显式“未量化”候补，不能挤掉有效的 Tier 1/2。
- scorecard 的 cue 证据必须落在候选窗内；模型只填 0–4 档和证据，固定代码复算
  `raw_score`、罚分与 Tier 准入。任何手改后的算术不一致都使 scorecard 无效。
