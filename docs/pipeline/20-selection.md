# 20 候选召回与选题

本文件是选题步骤的**分步权威**。metric 细则的强权威是
`assets/lidousha/slice_selection_metric.md`（v2，Ivan 纠正版）。

## 链路

1. 语义召回为主：`select_semantic_session_candidates`（`src/autoslice/semantic_candidate_selector.py`），关键词只兜底（Ivan 2026-07-03）。
   - 单段超过 45 分钟时，必须按 30 分钟核心窗 + 前后各 2 分钟上下文重叠分别召回，再在整段范围按信心分去重排序；禁止把 2 小时字幕塞进一次调用后，把模型只返回前半场少数候选误当作整场无内容。
   - 每段候选池上限 12 是召回余量，不是交付配额；最终仍按每场 talk top-5 上限与最低信心门筛选，不为凑数降门槛。
2. 弹幕热度 hints：`danmaku_evidence.py`（爆发窗口，选题信号，不改文本）。
3. CPA 观众视角审查：每个候选无条件过 `scripts/cpa_semantic_qa_llm.py` 判官（`viewer_context_ok` 语境自足性 + 自动扩窗建议），失败即 BLOCK（`live_source_review.py::_merge_cpa_semantic_review_into_decision`）。
4. 候选是内容锚点不是最终边界；边界由 [30-boundary.md](30-boundary.md) 决定。

## 同主题合并（Ivan 2026-07-18 切片案 → 2026-07-19 新规）

- **主题一致的候选尽量合并成一个切片**，不许把同一话题在源时间轴上相邻/交错的两段切成两条成品（案例：kmx 称呼两条切片同主题被分开切）。
- 机制：候选定稿前跑主题聚合 pass——相邻候选（源时间间隔小）且主题/实体重叠高的合并为一个候选窗口，交给边界解析定最终起止。实现见 `src/autoslice/semantic_candidate_selector.py` 的 merge pass（落地记录 `merge_audit`）。
- 合并后的标题按合并主题起，不是拼接两个子标题。

## metric 硬维度（详见 metric 资产）

- 围绕本人（含态度/立场/情绪，不只名字梗）；观点强度与受众兴趣（百合/GL）是硬维度；高语义分不许因 niche 压低。
