# 30 边界解析

本文件是边界步骤的**分步权威**。

- 候选是内容锚点不是最终边界；最终起止由源语境证据 + 边界解析器决定（AGENTS.md 项目方向）。
- 链路：`source_context_planner.py`（计划扩窗）→ `source_context_executor.py`（执行）→ `boundary_resolver.py` / `producer_boundary_resolution.py`（定稿）→ `live_source_review.py`（含 song_boundary 专线）。
- `semantic_start/end` 对准真实语音起止读 `padded.fresh.srt`；候选 start 含前置铺垫（memory `produce-slice-package-host-path-and-slow-mount`）。
- 边界红旗 → quarantine（runner 规则），不许带伤交付。
- CPA 判官的 `context_expand_before/after_ms` 扩窗建议在本步消费（自动扩窗后重审）。
