# 公开流水线分步文档

本目录只描述可复用的软件契约。运行时状态、凭据、主机路径、人工会话和
候选历史不属于公开文档；它们必须由调用方在当前环境中提供并单独验证。

步骤顺序为：

1. [10-source-recording.md](10-source-recording.md)：固定源媒体和时间轴。
2. [20-selection.md](20-selection.md)：召回候选并记录评分证据。
3. [30-boundary.md](30-boundary.md)：解析完整内容边界。
4. [40-subtitle-text.md](40-subtitle-text.md) 与 [41-semantic-repair.md](41-semantic-repair.md)：生成并审查字幕。
5. [50-song-lane.md](50-song-lane.md)：处理歌切专用时间轴。
6. [60-title.md](60-title.md) 与 [70-cover.md](70-cover.md)：生成标题和封面。
7. [80-package-delivery.md](80-package-delivery.md)：冻结可交付包。
8. [90-publish.md](90-publish.md)：在授权后上传并核对公开结果。

每一步都应消费上一步的结构化结果和哈希。缺字段、来源漂移、证据不完整或
语义不确定时返回明确的 BLOCK/UNKNOWN，不能用默认值伪造通过。自动模型可以
召回或提出候选，但不能自行产生人工授权、发布权或事实真值。

代码、schema 和当前配置是具体实现的强权威；本目录说明模块边界和不变量，
不保存任何单次候选的台账、引语或部署状态。
