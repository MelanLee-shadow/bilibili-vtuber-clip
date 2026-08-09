# 20 候选召回与选题

本文件是选题步骤的**分步权威**。metric 细则的强权威是
`assets/lidousha/slice_selection_metric.md`（当前 v5）。模型只给档位和 cue 证据；
`src/autoslice/selection_scorecard.py` 负责 Tier 准入、固定算术、绝对分校准与最终排序，
禁止用 confidence 代替价值分。可执行校准资产是
`assets/lidousha/selection_score_calibration.v1.json`；profile 未注册、资产漂移、锚点越界
或锚点顺序颠倒都必须 fail closed，不能只在 prompt 里写“应该大约多少分”。
候选必须先由最终 resolved start/end 生成稳定 `candidate_id`，再以该 ID 查校准锚点；
禁止拿临时召回 ID 校准后再改名，否则同一内容会在重跑时随机失去 reviewed anchor。

## 链路

1. 语义召回为主：`select_semantic_session_candidates`（`src/autoslice/semantic_candidate_selector.py`），关键词只兜底（Ivan 2026-07-03）。
   - 单段超过 45 分钟时，必须按 30 分钟核心窗 + 前后各 2 分钟上下文重叠分别召回，再在整段范围按信心分去重排序；禁止把 2 小时字幕塞进一次调用后，把模型只返回前半场少数候选误当作整场无内容。
   - 每段候选池上限 12 是召回余量，不是交付配额；最终仍按每场 talk top-5 上限、scorecard 硬 Tier 与最低信心门筛选，不为凑数降门槛。
   - 已选候选若被边界、说话人或字幕 authority 的确定性安全门拒绝，保留拒绝记录但立即从已排序 backlog 补位；只有 provider/运行时等可恢复故障才占位等待，不能因一个不可交付候选把全场最终数量永久压低。
   - 上述补位只适用于普通生产。`RECOVERY_REVIEW` 若绑定
     `talk-selection-contract.v1 / EXACT_CANDIDATE_SET_NO_BACKFILL`，候选集合本身就是
     人工 authority：任一条失败必须保留原槽位为失败，不得从 backlog 偷换成另一条。
2. 弹幕热度 hints：`danmaku_evidence.py`（爆发窗口，选题信号，不改文本）。
3. CPA 观众视角审查：每个候选无条件过 `scripts/cpa_semantic_qa_llm.py` 判官（`viewer_context_ok` 语境自足性 + 自动扩窗建议），失败即 BLOCK（`live_source_review.py::_merge_cpa_semantic_review_into_decision`）。
4. 候选是内容锚点不是最终边界；边界由 [30-boundary.md](30-boundary.md) 决定。

## 同日场级配额

- 配额按候选的**源 segment 场**记账，不按 recording session 日期桶直接合并。
  `segment-scene-context.v1` 把源文件 stat、录制 metadata/XML 标题和 ffprobe
  `width/height/orientation` 耐久化到 state；缓存输入指纹漂移时重探，UNKNOWN 也在后续
  tick 重试但本 tick 仍按杂谈。事件场必须同时命中
  3D/生日/周年类标题语境且为横屏，竖屏永远是杂谈场；标题、探针缺失/不可读或方向
  unknown 都 fail closed 到杂谈场，不得静默放宽。
- 普通生产在 exact contract 短路之后，互斥优先级为
  `RESOLVED game > event > talk`，三类政策不叠加：游戏场保留既有 `20 / 第 6 席起 >=85`；
  事件场为 `15 / 第 6 席起 >=85`；杂谈场为默认 `5`。同一坍缩 session 内分别使用
  `game:`、`event:`、`talk:` scope，因此同日事件场的 15 席与杂谈场的 5 席独立计数。
- 裁定出处（Ivan 2026-08-09，裁定失落案重申）：「我记得我当时说过 88 这个 3D live 场
  放宽到 15 个,然后当天的杂谈场认为是独立的,自然有 5 个」。实现见
  `src/autoslice/segment_scene_context.py`、`src/autoslice/talk_quota_policy.py` 和
  `src/autoslice/candidate_selection.py`。

场次联动关系的现行 authority 是
`src/autoslice/session_relation_authority.py` +
`assets/lidousha/session_relation_ledger.v1.json`：它以日期/官方源 SHA/参与者绑定关系，
同时进入 selection、clip-context、StoryContract 与封面参与者门。开发旁路或历史报告中的
`NO_TRIGGER` 只表示该旁路没有触发，**不等于非联动**，也不能覆盖 ledger 的 `CONFIRMED`。

## 候选状态与人工点选

- `picks`、`pending_talk`、`talk_backlog`、拒绝记录必须互斥投影；一个 candidate
  只能处于 `CURRENT`、`PENDING`、`PENDING_COVER`、`FAILURE`、`MISSING` 或
  `OUTSIDE_EXACT_CONTRACT` 之一。已经成为 `CURRENT + COMPLIANT` 成品或终态拒绝的
  candidate 不得再次出现在“当前候补”。
  `not_selected` 是历史 prose，不是状态 authority，也不得参与补位。
- 每次拒绝必须保留 `failure_stage + rejection_reason + failure_evidence`；报告把它放在
  “候选门禁拒绝”，不能混进成品表只显示一个无解释的 `candidate_rejected`。
- 已标记 `selected_repair=true` 的字幕 authority 修复项若在
  `chat_authority_finalization` 被补位成 `candidate_rejected`，不能永久失联：只有字幕
  authority 专属 fingerprint（text pipeline、source truth、chat proposal、glossary 资产）
  发生变化时才自动恢复原候选，并可越过旧 lifetime 计数获得一次新代码尝试；无相关变化、
  普通拒绝或手写伪状态仍不得复活。
- 用户点名候补不篡改分数：追加 `USER_SELECTION_OVERRIDE`，记录原始 scorecard、
  baseline rank、实际 slot、被越过的基线候选与人工 authority。用户说外部已有重复但
  没有 BV 时，可直接 `SUPPRESSED_BY_USER`，但重复 claim 只能是
  `USER_ASSERTED_UNVERIFIED`，不得伪装成已验证站外重复。
- 精确恢复契约持续压住普通 backlog，直到新的人工恢复计划显式替换；普通 backlog
  在报告里只能显示为 `OUTSIDE_EXACT_CONTRACT / INELIGIBLE`，不能伪装成当前候补。
- `src/autoslice/candidate_selection.py::exact_talk_contract_closure` 是 exact 状态的共同
  判定器。终态验收要求：每个 contract ID 恰有一条 `rc=0 + CURRENT + COMPLIANT`
  delivery；没有 pending、failure、missing、重复/冲突记录或 outside-contract active pick。
  任一条件不成立都只能是 `recovery_incomplete`，不得保留/生成 `review_ready`。
- `src/autoslice/batch_terminal_state.py::project_terminal_batch_state` 是普通/恢复批次唯一
  终态投影器。retry 时间只是元数据，不能覆盖 exact closure；有 future retry 但 exact 集合
  未闭合时仍为 `recovery_incomplete`。`review_ready`、`review_ready_with_failures`、
  `review_ready_retry_wait`、`retry_wait` 与 `no_delivery` 只能由该投影器按当前 delivery、
  failure、cover-pending、exact closure 和 retry state 共同得出，报告层不得自行猜状态。
  只有严格晚于当前时间的 retry epoch 能产生 `*_retry_wait`；已到期的旧时间戳不能把批次
  永久伪装成“仍在等待”。
- exact contract 中的直接 gate 拒绝必须规范化为带 `failure_stage + failure_kind +
  failure_evidence + fingerprint` 的可重试失败，并标记合同禁止补位；不能留下永远唤不醒的
  `candidate_rejected`。

## 同主题合并（Ivan 2026-07-18 切片案 → 2026-07-19 新规）

- **主题一致的候选尽量合并成一个切片**，不许把同一话题在源时间轴上相邻/交错的两段切成两条成品（案例：kmx 称呼两条切片同主题被分开切）。
- 机制：候选定稿前跑主题聚合 pass——相邻候选（源时间间隔小）且主题/实体重叠高的合并为一个候选窗口，交给边界解析定最终起止。实现见 `src/autoslice/semantic_candidate_selector.py` 的 merge pass（落地记录 `merge_audit`）。
- 合并后的标题按合并主题起，不是拼接两个子标题。

## metric 硬维度（详见 metric 资产）

- 七维权重固定为 25/20/15/15/10/10/5；先验收 Tier 证据，再按有效分排序，最后才以 confidence 破同分。
- 有效分 = 固定七维 raw score − uncertainty penalty − same-session fatigue penalty。
  多样性只能在同一 Tier 内参与，不能让低 Tier 候选跨层超车。
- 围绕本人（含态度/立场/情绪，不只名字梗）；观点强度与受众兴趣（百合/GL）是硬维度；高语义分不许因 niche 压低。
- 报告必须同时显示 Tier 与有效分；缺 scorecard 的旧候选只能作为显式“未量化”候补，不能挤掉有效的 Tier 1/2。
- scorecard 的 cue 证据必须落在候选窗内；模型只填 0–4 档和证据，固定代码复算
  `raw_score`、罚分与 Tier 准入。任何手改后的算术不一致都使 scorecard 无效。
- 7/22 executable anchors：`auto_193450_3573_3665` 必须 Tier 1、有效分 75–85；
  `auto_193450_5341_5459` 必须 Tier 2、有效分 50–60；前者必须稳定高于后者。
  这两项只是量尺 canary，不构成 recovery allowlist；exact 集合只能来自当前 v7 plan 的
  selection contract。
