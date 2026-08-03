# ops/recording — 录制层参考部署

这是**参考部署的原样配置**（示例频道，房间 22966160），不是开箱即用模板。
拿去用之前逐项替换：

| 文件 | 作用 | 你要改什么 |
|---|---|---|
| `bililive_recorder.config.v3.json` | 录播姬（BililiveRecorder）房间配置 | `RoomId` 换成你的房间；输出路径 |
| `docker-compose.bililive-recorder.yml` | 录制器 + adapter 容器编排 | 房间号、录播根挂载（参考部署挂网盘路径，换成你的盘）、四个 env/config 挂载 |
| `bililive_recorder_adapter.py` | 录制事件适配器：webhook 对账、分段状态、健康账本 | argparse 默认房间/路径（或全部用参数传） |
| `bilive-recording-consumers.service` | 消费者 systemd 单元 | 路径对齐你的部署 |
| `bilive-record-health.cron` | 录制健康巡检 cron | 引用的 `record_health_audit.py` 是宿主机部署层脚本，**未随本仓分发**——先删掉该行，或按巡检语义（检查录制段新鲜度/挂载健康）自己实现 |

录制层的规则权威在 [docs/pipeline/10-source-recording.md](../../docs/pipeline/10-source-recording.md)：
源健康门、mount 看门狗语义、录制段命名约定（`<room_id>_YYYYMMDD-HH-MM-SS`）
都在那里。runner（`scripts/session_autoslice.py`）通过 adapter 的
recorder-neutral 状态文件感知"下播 + 新分段"，不直接读录制器私有状态。
