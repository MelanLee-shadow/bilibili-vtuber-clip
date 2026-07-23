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
闭环建议。最终目标 end 至少为 `max(manual_lower_bound, semantic_recommended_end)`。

所有 talk 包——包括带人工 end 的恢复包——都必须运行并通过完整
`boundary_semantic_review`，同时通过确定性 cue/syntax 门与上述四命题。人工下界与语义建议
都必须原样绑定进 boundary audit 与 StoryContract；缺失、推荐 cue 不在原 cue grid、
越过 30 秒上限、四命题任一不成立或最终字节没有 materialize 推荐终点均拒发。

## 冻结的 required owner

字幕源真值、已审 baseline 覆盖区以及已应用的 story/chat 修复，只要声明
`required=true`，都必须在定边界前冻结进 `frozen-boundary-owner-contract.v1`，逐项绑定
`owner_kind + owner_id + local_windows`。最终 boundary audit 必须原样携带同一 owner 列表，
且每个窗口完全落在最终 `[start,end)` 内。

旧候选边界若排除了 required owner，唯一合法结果是扩展边界、重新通过语义闭环和确定性门，
或以 `BOUNDARY_REQUIRED_OWNER_EXCLUDED` 阻断；不得把该 owner 在裁切后降级成
`NOT_REQUIRED`、`OUTSIDE_DELIVERY` 或普通 superseded 项。边界扩展仍受原语义 search origin
和 repair cap 约束；owner 下界本身不是把故事无限延长的授权，cap 内找不到干净闭环就拒发。

VAD 只描述物理连续性：通过 source/语义闭环后，“切点后仍有人声”只记非语义告警，
不得触发一直延到下一静音/下一 cue 的盲扩展；句法硬尾（如“因为”“跟第……”）仍阻断。

选择 scorecard 与边界 reviewer 当前都来自 CPA gpt-5.6 模型族，必须共享同一
`independence_group`，只算一个相关语义证人；“分开调用”不等于两票。
