# 10 录制与源健康

本文件是源步骤的**分步权威**。运行时权威分为：

- 录制服务：`free:/opt/bilive/compose.yml` 中的 `bililive_recorder`；
- 录播姬配置：`free:/opt/bilive/bililive-recorder/config.json`；
- adapter 状态/账本：`free:/opt/bilive/recording/`；
- 自动切片部署：`free:/opt/bilive/autoslice/repo`（现查 `DEPLOYED_COMMIT`）；
- 原始录播：`free:/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/`。

## 唯一录制后端

- 生产唯一录制器是官方 BililiveRecorder/录播姬
  `ghcr.io/bililiverecorder/bililiverecorder:2.18.0`，部署锁定镜像 digest。
  `bilive_record` 仅保留为旧脚本的工具容器，里面不得启动 `blrec`。
  旧 `/etc/cron.d/bilive-live-watchdog` 必须不存在；否则它会绕过 compose，
  重新在工具容器中启动第二套录制器。
- 仓库配置权威为
  `ops/recording/bililive_recorder.config.v3.json` 和
  `ops/recording/docker-compose.bililive-recorder.yml`。旧
  `ops/blrec-patches/`、`ops/recording/blrec_live_watchdog.py` 只作事故回溯/
  回滚材料，不再是生产启动或恢复权威。
- 画质顺序固定为
  `avc10000 → avc400 → avc250`。即首发默认先要 AVC 原画，再要 AVC
  1080P 蓝光，最后才降到 720P。HEVC 在取得真实 codec-12 FLV 并通过当前
  FFmpeg、重封装、全包扫描和切片全链路前 fail closed，不进入生产优先级。
  配置优先级不等于实际拿到的
  清晰度；实际成片仍须用 ffprobe 的 codec、width、height 和录播姬日志验收，
  监控不得把“请求过 10000”写成“已录到 10000”。
- `NetworkTransportAllowedAddressFamily=Ipv4`（配置值 `1`）是生产硬门。
  free 没有可用 IPv6；不得依赖 .NET/DNS 的随机地址选择，否则会出现
  `streaming=true` 但直播流连接到不可达 IPv6、文件不增长的假活。
- Bilibili Cookie 只可存在于远端权限为 `0600` 的录播姬配置；不得进入仓库、
  compose、日志或报告。Cookie 登录失效时仍保持 10000 优先配置，但实际画质
  可能受 B 站匿名权限限制，必须如实告警/报告。
- 标准模式、30 分钟按时切段、普通弹幕原始数据、SC、礼物、上船均开启；
  输出名固定为
  `22966160_YYYYMMDD-HH-MM-SS.{flv,xml}`，日期目录按 `Asia/Shanghai`。
  文件 stem 末尾必须仍是紧凑时间戳，供候选与弹幕时间权威解析。

## Adapter 契约

- 本地权威：`ops/recording/bililive_recorder_adapter.py`。宿主机没有
  ffmpeg/ffprobe，因此 compose 用同一固定工具镜像启动独立
  `bililive_adapter` 服务；它只运行 adapter daemon，官方
  `bililive_recorder` 仍是唯一录制器，`bilive_record` 仍只是旧脚本工具
  容器。adapter 先持久化 Webhook v2 事件，再通过带 Basic Auth 的 compose
  私网 GraphQL
  每分钟 reconciliation，原子写宿主
  `/opt/bilive/recording/status.json`。
  宿主发布固定为 `127.0.0.1:23566`，不得把 2356 暴露到公网，也不得设置
  `BREC_HTTP_OPEN_ACCESS` 绕过录播姬的开放访问保护。
- runner 只信 schema 正确、房间匹配、180 秒内生成且
  `service_reachable=true` 的状态。状态缺失、过期、报错或 adapter 正在
  finalizing 均保持 live hold；不得猜测为下播并开始切片。
- GraphQL 首次同时报告 `streaming=false`、`recording=false` 后仍须连续稳定
  360 秒才允许封口；等待期间发布 `finalizing=true`。这道门用于吸收 CDN
  重连、直播状态短暂抖动与录播姬最后一次文件 flush，不能仅凭一次离线采样
  提前切半场。GraphQL 查询失败或相邻健康采样间隔超过 150 秒时，连续离线
  计时必须清零。
- 每个 FLV 必须有同路径 `FileClosed` 事件，且事件中的 `FileSize` 与磁盘
  一致，才可进入重封装。Webhook `EventId` 去重、允许乱序；事件先以 append
  + fsync 写入 0600 journal，再快速返回 204。Webhook 不到、路径复用、
  `FileOpening` 未关闭、事件大小不符或事件账本损坏都只能 fail closed。
- 只有 `streaming=false` 且 `recording=false` 时才允许封口。adapter 只处理
  迁移水位之后的文件，或带官方 `BililiveRecorder` XML 签名的文件，避免误扫
  历史 blrec 残件。
- 封口顺序固定为：

  1. FLV stream-copy 到同文件系统隐藏 staging 的 UUID `.mp4`；
  2. ffprobe 必须同时看到视频、音频、正时长，并完成输出 MP4 packet scan；
  3. 从 XML `raw` 恢复同 stem JSONL：`d → DANMU_MSG`、
     `sc → SUPER_CHAT_MESSAGE`、`gift → SEND_GIFT`、`guard → GUARD_BUY`；
  4. 从 `BililiveRecorderRecordInfo@start_time` 写 `.meta.json` 的
     `description.RecordStartTime`；
  5. sidecar 成功后 no-clobber 发布为 `<stem>.mp4`；CloudDrive 不支持
     hardlink/`RENAME_NOREPLACE`，因此实际降级为 adapter 独占 reservation
     下的同目录原子 rename。既有目标无可信 ledger 一律报 CONFLICT。

- 已有 MP4/JSONL/meta 禁止覆盖；同名内容不一致即 fail closed。原始 FLV/XML
  永不删除。活动文件、API 未知、XML 不完整、无音轨、重封装/探测失败都不得
  发布 MP4。
- `scripts/free_session_autoslice.py` 只枚举封口后的
  `<ROOM>_*.mp4`；30 分钟段只是源容器，整场候选仍跨所有 segment 全局排序。

## 源健康与恢复

- `scripts/free_mount_watchdog.sh` 是 CloudDrive FUSE 恢复权威：挂载失败时
  lazy-unmount → 重启 `clouddrive2` → 等宿主恢复 → 同时重启
  `bililive_recorder` 与 `bilive_record` 重新 bind mount → 分别验证
  `/rec/Videos` 与 `/app/Videos` 可读。任一消费者未恢复都只能报 PARTIAL。
- 活着的容器不等于健康录制。直播中两轮无字节增长、状态过期、弹幕/录制长期
  未连接均须告警；受限重启只针对 `bililive_recorder`，不得复活 blrec。
- 终态库存硬门：runner 在任何“无新段”提前返回前运行
  `recording-inventory-audit.v1`；发现已封口的源没有同 stem MP4，状态只能是
  `source_incomplete`，不得进入 selection 或 `review_ready`。
- 源完整性：视频流独立解码零损伤（`source_integrity.py`）；BLOCK≠ok、
  0 交付≠done。
- 杀开关：`touch /opt/bilive/autoslice/DISABLED`。
