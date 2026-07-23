# 30 边界解析

本文件是边界步骤的**分步权威**。

- 候选是内容锚点不是最终边界；最终起止由源语境证据 + 边界解析器决定（AGENTS.md 项目方向）。
- 链路：`source_context_planner.py`（计划扩窗）→ `source_context_executor.py`（执行）→ `boundary_resolver.py` / `producer_boundary_resolution.py`（定稿）→ `live_source_review.py`（含 song_boundary 专线）。
- `semantic_start/end` 对准真实语音起止读 `padded.fresh.srt`；候选 start 含前置铺垫（memory `produce-slice-package-host-path-and-slow-mount`）。
- 边界红旗 → quarantine（runner 规则），不许带伤交付。
- CPA 判官的 `context_expand_before/after_ms` 扩窗建议在本步消费（自动扩窗后重审）。

## 四命题边界门

talk 成片的最终 end 必须同时成立：

1. 候选内容锚点全部覆盖，不能为求句尾提前删掉已选内容；
2. 句法完整，`cue end`、标点或静音都不能单独证明一句说完；
3. 故事/回答/包袱已经落地；
4. 下一 cue 已被证明是下一条 SC、谢礼或另一话题，不能吞进本片。

优先级：hash-bound source 人审绝对终点 > 语义 review + 确定性 cue/syntax 门。
VAD 只描述物理连续性：通过 source/语义闭环后，“切点后仍有人声”只记非语义告警，
不得触发一直延到下一静音/下一 cue 的盲扩展；句法硬尾（如“因为”“跟第……”）仍阻断。

选择 scorecard 与边界 reviewer 当前都来自 CPA gpt-5.6 模型族，必须共享同一
`independence_group`，只算一个相关语义证人；“分开调用”不等于两票。每个 talk 包须把
human endpoint 或完整 `boundary_semantic_review` 原样绑定进 boundary audit 与
StoryContract；缺失、推荐 cue 不在原 cue grid、越过 30 秒上限或四命题任一不成立均拒发。
