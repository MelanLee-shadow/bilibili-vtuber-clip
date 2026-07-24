# 30 边界解析

本文件是边界步骤的**分步权威**。

- 候选是内容锚点不是最终边界；最终起止由源语境证据 + 边界解析器决定（AGENTS.md 项目方向）。
- 链路：`source_context_planner.py`（计划扩窗）→ `source_context_executor.py`（执行）→ `boundary_resolver.py` / `producer_boundary_resolution.py`（定稿）→ `live_source_review.py`（含 song_boundary 专线）。
- `semantic_start/end` 对准真实语音起止读 `padded.fresh.srt`；候选 start 包含必要前置铺垫。
- 边界红旗 → quarantine（runner 规则），不许带伤交付。
- CPA 判官的 `context_expand_before/after_ms` 扩窗建议在本步消费（自动扩窗后重审）。
- 为保护开头音素而保留的 pre-roll 可以有声无字；若上一 cue 只因裁切重叠而露出
  `<=300ms` 的不可读字幕残片，保留音频但删除该闪字。该判断必须发生在最短可读时长
  延展之前；即使上游已把 cue 预裁到恰好从成片 `0ms` 开始，也按同一规则处理，不能把
  250ms 的上题尾巴延展成 1s 的醒目假开场。超过此阈值的实质内容不得靠这条规则吞掉。

## 四命题边界门

talk 成片的最终 end 必须同时成立：

1. 候选内容锚点全部覆盖，不能为求句尾提前删掉已选内容；
2. 句法完整，`cue end`、标点或静音都不能单独证明一句说完；
3. 故事/回答/包袱已经落地；
4. 下一 cue 已被证明是下一条 SC、谢礼或另一话题，不能吞进本片。

hash-bound 人工 end 只表示“人工已确认至少要保留到这里”的**下界**，不是可绕过语义门的
绝对截断点。它不得早于候选 `end_ms`，不得砍掉 content anchor，也不得覆盖一个更晚的语义
闭环建议。这里必须区分两种量：`semantic closure cue end` 是 reviewer 选择的句尾，
`delivery coverage lower bound` 是最终媒体至少覆盖到的位置。若 coverage lower bound
恰好落在该 closure cue 后固定 400ms 尾气内，可保留 reviewer 选择的 cue，并由
`adaptive_tail_cut` 生成尾气；不得为了满足媒体覆盖下界而吞进下一句。实际尾气若被下一
cue/VAD guard 钳到下界之前，仍以 `BOUNDARY_REQUIRED_OWNER_EXCLUDED` /
`BOUNDARY_DELIVERY_LOWER_BOUND_EXCLUDED` 阻断，不能靠理论 400ms 放行。

source review、resolver 与有界 retry 必须共同消费并逐字段、逐 SHA 绑定同一份
`talk-boundary-search-scope.v1`，禁止各自从旧 candidate end 重新推导 cap：

- `semantic_search_origin_ms = max(semantic_target_ms, manual_lower_bound_ms,
  structured_payoff_ms)`；人工下界与已确认 payoff 属于语义搜索起点，可以把搜索原点向后移；
- `delivery_lower_bound_ms = max(semantic_search_origin_ms, required_owner_end_ms)`；
  required owner 只抬高交付/评审下界，不能移动搜索原点，也不能把 cap 滚动再加一次；
- `max_recommended_end_ms = semantic_search_origin_ms + repair_cap_ms`。reviewer 的推荐 end 必须
  同时不早于交付下界、不晚于该绝对 ceiling；
- retry 的 source full window 至少覆盖
  `max_recommended_end_ms + witness_reserve_ms`，再按 piece 映射回绝对 source 时间。reserve
  只供 reviewer 观察终点后的下一话题，不能扩大合法推荐 endpoint。

scope 缺失、SHA 漂移、resolver 重算不一致、source window 没覆盖 reserve，或 owner 下界已经
越过绝对 ceiling，都必须 fail closed；不得用旧的 `semantic_target + 60s` 或
`candidate end + 90s` 近似替代。

所有 talk 包——包括带人工 end 的恢复包——都必须通过**两层不同作用域的语义回执**，同时
通过确定性 cue/syntax 门与上述四命题。两层不能互相冒充，也不能把第一层的 cue ordinal
平移后当成最终成片证据。

1. `review_scope=source_full_window` 在 resolver 前运行。它消费当时完整的 source full-window
   cue grid，向推荐 endpoint 之后保留下一话题 cue，供 reviewer 直接证明
   `next_topic_separated`。request 与完整非空 cue grid 各自 hash-bound；resolver 只消费这份
   回执，并在 snap 后用 `talk-boundary-final-endpoint-binding.v1` 绑定 source 坐标中的推荐
   cue/ms、实际 `[final_start_ms, final_end_ms)`、closure 文本与 grid SHA。
2. resolver、裁切和 `_materialize_final_recut` 完成后，必须从**包内精确最终 SRT**重新解析
   delivery-local cue grid，再运行 `review_scope=final_delivery`。这一层必须按当前最终字幕
   重新判断 syntax/story，并把推荐 endpoint 绑定到 delivery 坐标（`final_start_ms=0`、
   `final_end_ms=成片内容时长`）的唯一最后 closure cue；它不能复用 source ordinal、request
   或 grid hash。

final-delivery SRT 已经裁掉终点后的 cue，因此它只能通过 PASS 的
`talk-boundary-source-separation-witness.v1` 继承“终点之后已进入下一话题”这一项证明。该
witness 必须绑定 source review 的规范 SHA、request SHA、source cue-grid SHA、推荐 end 及
实际 source final interval；它不替代对最终 SRT 的 syntax/story 重审。source review 没有真实
post-end witness、source interval 漂移、最终 reviewer 未选择 delivery 最后一条 cue，或任一
绑定不一致都拒发。

两层回执的 cue grid 与坐标系本来就不同，**不得要求两层 cue-grid SHA 相等**。应分别验证：
`boundary_audit.boundary_semantic_review` 是 `source_full_window`，而
`boundary_audit.final_delivery_boundary_semantic_review` 与 StoryContract 中的
`boundary_semantic_review` 是同一份 `final_delivery` 回执。source grid、snap 或 interval
变化会使 source 回执及其下游 witness 失效；materialize 后 SRT 的任何字节/cue 变化会使
final-delivery 回执与 exact-final 放行回执失效，均须从相应层重新评审，不能只重绑 hash。

两层 reviewer 的 `evidence_cue_indexes` 都必须是各自 hash-bound request 中实际展示的
`cues` 的非空子集；引用未展示行报 `BOUNDARY_EVIDENCE_CUES_INVALID`。source reviewer 声明
`next_topic_separated=true` 时，至少一条 evidence cue 必须在推荐 endpoint **之后**，否则报
`BOUNDARY_NEXT_TOPIC_WITNESS_MISSING`；final-delivery reviewer 仅在上述 source witness
完整 PASS 且推荐 delivery 最后一条 cue 时可没有片内 post-end cue。候选的 hash-bound
`clip_context_prompt` 按 [40-subtitle-text.md](40-subtitle-text.md) 的完整 18,000 字预算原样
展示，不能先截成 12,000 字；超预算或可见 cue 窗超限均 fail closed。

每一层 semantic PASS 都只对它实际推荐的 endpoint 有效，并各自需要 PASS 的
`talk-boundary-final-endpoint-binding.v1`。若 snap、repair 或 materialize 改变该层 endpoint，
旧 PASS 不能沿用；只能在原有 cap 内有界重审/重试，仍不一致则以
`BOUNDARY_SEMANTIC_ENDPOINT_CUE_MISMATCH` /
`BOUNDARY_SEMANTIC_ENDPOINT_MS_MISMATCH` 阻断。正常首轮 cap 为 30 秒；只有有界重试才可把
同一 spec 的 cap 提升到 60 秒，不能另开无上限扩窗。实际生产入口必须把
`boundary_repair_extend_cap_ms` 只接到边界/终审调用；接线缺失或误接到相邻实体 authority
阶段属于架构失败，并由 production-entry seam test 固定。

## 冻结的 required owner

只有 `required=true` 的字幕源真值、已审 baseline 覆盖区，以及具备相应 typed ownership
contract 的已应用 story/chat 修复，才可在定边界前冻结进
`frozen-boundary-owner-contract.v1`，逐项绑定
`owner_kind + owner_id + local_windows`。`required:false` source truth 只是 best-effort，
partial/proxy chat support、context-only verdict 与被拒 proposal 都不得取得 ownership；
其中整句 `exact_read` 还必须由 whole-line gate 明示 `owner_eligible=true`，缺失/False 即
拒绝。sender/gift/coreference/entity 等窄槽修复不借用该整句字段，而按各自 slot-scoped typed
contract 冻结。最终 boundary audit 必须原样携带同一 owner 列表，且每个窗口完全落在最终
`[start,end)` 内。

旧候选边界若排除了 required owner，唯一合法结果是扩展边界、重新通过语义闭环和确定性门，
或以 `BOUNDARY_REQUIRED_OWNER_EXCLUDED` 阻断；不得把该 owner 在裁切后降级成
`NOT_REQUIRED`、`OUTSIDE_DELIVERY` 或普通 superseded 项。边界扩展仍受原语义 search origin
和当前 spec 的 repair cap 约束；owner 下界本身不是把故事无限延长的授权，cap 内找不到干净
闭环就拒发。最终 audit 还必须分别记录并通过
`required_boundary_owner_verification` 与 `delivery_coverage_verification`；前者证明每个
owner window 在成片内，后者证明最终媒体 end 没有落在人工/结构化/owner 共同下界之前。

VAD 只描述物理连续性：通过 source/语义闭环后，“切点后仍有人声”只记非语义告警，
不得触发一直延到下一静音/下一 cue 的盲扩展；句法硬尾（如“因为”“跟第……”）仍阻断。

选择 scorecard 与边界 reviewer 当前都来自 CPA gpt-5.6 模型族，必须共享同一
`independence_group`，只算一个相关语义证人；“分开调用”不等于两票。
