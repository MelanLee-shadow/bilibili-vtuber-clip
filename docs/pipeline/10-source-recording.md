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
  迁移水位之后的文件，或已进入严格校验 Webhook 账本的精确相对路径，避免
  误扫历史 blrec 残件。空闲状态不得为了识别生产者而逐个读取历史 XML，也
  不得反复 ffprobe 历史 FLV；CloudDrive 冷文件读取会阻塞状态心跳。
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
- BililiveRecorder 首次连流可能先写一个只有 FLV header/0×0 H.264 stream 的
  连接残片，再在同一 session 立即打开正常文件。只有 adapter
  `adapter-state.json / source_dispositions` 中的 typed row 才能把这类原件从
  remux 车道排除：row 必须是
  `recording-connection-stub.v1 / IGNORED_CONNECTION_STUB /
  RECORDER_CONNECTION_STUB_NO_DECODABLE_VIDEO`，并同时绑定 source/XML 的当前
  stat+SHA-256、FileOpening/FileClosed ID 与时间、session、官方 XML（最多一条
  用户事件，XML 原字节及事件数均写入 typed row）、
  H.264 0×0 且 frame/packet 扫描均为空，以及同 session 的下一 opening。
  被排除文件必须是该 session 第一 opening，size <5 MiB、event duration 与
  open-close wall duration 均 <10 秒；下一 opening gap 必须在 0–2 秒内且已
  CLOSED，其 source size 必须匹配 webhook 与 finalized ledger，真实 MP4 SHA-256
  必须匹配 ledger 且为可探测的正尺寸双流媒体。少任一项都不得 ignore，仍走普通
  finalization 并 fail closed。
- typed row 不进入 `finalized`，不生成同 stem MP4，也绝不删除/移动 FLV/XML。
  创建 row 时只做一次 source/XML/后继 MP4 全字节 SHA-256 与媒体探测；row 另绑定
  四个文件的 size/mtime/ctime/device/inode/mode 指纹。adapter 每轮只重验 canonical
  row、当前 journal/finalized ledger 与这些不可变指纹，不得反复读取几百 MB 的历史
  MP4；任一内容、稳定 stat、ledger 或事件漂移都恢复为 status error，且不得自动重签
  旧 row。CloudFS 重挂会重建 device/inode；只有四个 exact path 位于同一个当前 FUSE
  mount、其余 stat/事件/ledger 全部不变时，adapter 才在 recorder idle 后启动受 deadline、
  PID start token 和重试上限约束的隔离子进程，重验 source/XML/后继 MP4 的历史 SHA-256。
  子进程 pending/timeout/error 均持久化并 fail closed，不阻塞 adapter 心跳；成功后写入
  `recording-source-fuse-identity-rebind.v1` canonical 链式回执；后续 adapter 只认最后
  回执的 exact namespace mount identity，inventory 跨容器时只认其 namespace-portable
  major:minor+FSTYPE+SOURCE 投影及 effective fingerprints。后继 FLV 的旧 row
  没有历史 SHA-256，因此回执必须明确记录 legacy promotion：只沿用 path、稳定 stat、
  webhook file size、finalized ledger source size 及后继 MP4 历史 SHA 交叉绑定，不能宣称
  后继 FLV 历史 hash 已匹配。本地文件系统 inode 漂移、跨 FUSE、mount identity 漂移而
  无新回执或任何非 device/inode 漂移一律继续 BLOCK。
  CloudFS 可能在 finalized 后重写后继 FLV 的 mtime；row 因此分别保存 ledger 的
  历史 mtime 与签发时当前 source fingerprint，并要求两者之后各自稳定，不要求这两个
  跨时刻 mtime 相等。后继 source size、当前 fingerprint、MP4 当前 fingerprint 与
  finalized target SHA 仍必须全部吻合。
  合法、可解码的短视频不满足 0×0+零 frame/packet 条件，仍按普通规则
  finalization。
- `scripts/free_session_autoslice.py` 只枚举封口后的
  `<ROOM>_*.mp4`；30 分钟段只是源容器，整场候选仍跨所有 segment 全局排序。

## 源健康与恢复

- `scripts/free_mount_watchdog.sh` 是 CloudDrive FUSE 与录制消费者的唯一启动/
  恢复门。健康判定必须同时满足：`findmnt -T` 的精确 TARGET 是 CloudDrive
  根、FSTYPE 是 `fuse*`、SOURCE 是 `CloudFS`，并且录制目录可读；普通 ext4
  目录即使 `ls` 成功也必须判失败。
- `bilive_record`、`bililive_adapter`、`bililive_recorder` 固定使用
  `restart: on-failure:5`，不得用 `always`/`unless-stopped` 在 Docker daemon
  重启时抢在 CloudDrive 前启动。宿主
  `bilive-recording-consumers.service` 在开机时运行 watchdog；只有真实 CloudFS
  挂载通过后才 `docker compose up --force-recreate`；该 unit 以
  `PartOf=docker.service` 跟随显式 Docker service restart，并逐容器用
  `stat -f` 验证 `/app/Videos`、`/adapter/Videos`、`/rec/Videos` 都是 FUSE。
- 挂载失败时顺序固定为：先停三个消费者 → lazy-unmount → 将未挂载目录中的
  系统盘残件移动到 `/opt/bilive/mount-fallback-quarantine/` 保留 → 重启
  `clouddrive2` → 等真实 CloudFS → recreate 三个消费者 → 逐容器验证。不得
  删除残件，也不得在 ext4 目录上继续录制。`bilive-record-health` cron 也须先
  通过同一 `--probe-only` 门，避免健康报告反过来制造非空挂载点。
- **CloudFS 写缓存可读 ≠ 云端已持久化。** 录播姬直写 FUSE，字节先落
  clouddrive2 本地写缓存；若上传全部 Fatal（etag/md5 不一致、分片 URL 失效），
  FUSE 视图仍展示文件、选片照常通过，但缓存一丢（如容器重启）字节即蒸发
  （2026-07-25 两场次实损）。
- `scripts/clouddrive_upload_fatal_sentinel.sh`（部署为
  `/opt/bilive/autoslice/upload_fatal_sentinel.sh`，*/5 cron）扫描 clouddrive2
  日志中的 Fatal 上传错误：victim 仍可读时立即把字节抢救到
  `/opt/bilive/upload-fatal-rescue/`（守磁盘下限、不覆盖既有副本），并追加
  `reports/ALERT_UPLOAD_FATAL.txt`（NEEDS HUMAN）。抢救副本是止损证据；
  重新入云仍是人工决策。
- producer 源缺失语义（`producer_media._absent_source_media_error`）：挂载根
  不可读 → `SOURCE_RECORDING_ROOT_UNAVAILABLE`（基础设施等待，watchdog 修复
  后按 timer 重试）；挂载健康但源文件消失 → `SOURCE_MEDIA_MISSING`（终态
  `candidate_rejected/source_media_missing`，不自动复跑，复活只走 sanctioned
  `scripts/revive_rejected_candidates.py`）。sanctioned revival 必须写入与最新
  revival 审计块精确绑定的 `sanctioned-revival-retry.v1/PENDING` 凭证；requeue
  消费后改为 `QUEUED`，即使修复代码在复活前已经部署、相关 fingerprint 未再变化，
  或旧 lifetime 计数已到上限，也必须获得恰好一次实际生产机会。历史上已写
  failed/recoverable 却漏写该凭证的同一 revival，只能用脚本
  `--resume-unqueued-revival` 在 reason 与 fix commit 完全相同的条件下补证，不得手改
  state。unknown 失败只有一次有界重试，
  无限 timer 重试仅限 `INFRASTRUCTURE_WAIT_FAILURE_KINDS`
  （`runtime_prerequisite`/`provider_transient`）。若 date-level preflight 后 FUSE
  在 producer 读媒体途中断开，`OSError: Transport endpoint is not connected` /
  `State not recoverable` 仍须归类为
  `runtime_prerequisite/source_media_binding`，不能落入一次性 `producer_error`。
- producer 已经生成的 piece 只有在 provenance 精确绑定同一 source path、source
  SHA-256、窗口、输出路径，且当前 piece 输出哈希仍一致时才可复用。源路径仍为
  regular file 时直接记 `HASH_BOUND_CACHE_REUSED_SOURCE_PATH_PRESENT`，避免每次
  重试都从 CloudFS 全量重读数 GB 原片；若 source root 不可达则记
  `HASH_BOUND_CACHE_SOURCE_ROOT_UNAVAILABLE`。源根健康但文件明确缺失、
  provenance 不全、窗口或任一哈希不符时仍须 fail closed，不得把缓存当源文件
  缺失的旁路。
- 同一 mid-tick 断挂若发生在 hash-bound structured-chat sidecar 读取，必须同样
  归类为 `runtime_prerequisite/source_media_binding`；requeue 在源 `stat`/`ffprobe`
  上遇到 FUSE `OSError` 时保留原失败行并退出本次恢复，不得让整个 tick traceback
  或把暂时不可见的 chat path 误报成确定性的 binding missing。
- 活着的容器不等于健康录制。直播中两轮无字节增长、状态过期、弹幕/录制长期
  未连接均须告警；受限重启只针对 `bililive_recorder`，不得复活 blrec。
- 终态库存硬门：runner 在任何“无新段”提前返回前运行
  `recording-inventory-audit.v1`；发现已封口的源没有同 stem MP4，状态只能是
  `source_incomplete`，不得进入 selection 或 `review_ready`。inventory 默认读取
  `/opt/bilive/recording/adapter-state.json`，并独立重验 typed connection-stub 的
  canonical integrity、source/XML/后继 MP4 指纹、webhook/session/time/stat 与
  successor finalized ledger/初始全字节 SHA 绑定。只有重验有效的 stub 记录 severity `WARN` 且整体仍
  `PASS`；任何其他无 MP4 FLV 或 disposition 漂移仍是 severity `BLOCK`。
- 对旧录制器遗留的 finalized HLS（同 stem `.m3u8` 有 `ENDLIST`、且只引用同
  stem `.m4s`，但缺 `.mp4`），runner 在库存审计前自动执行一次 no-clobber
  stream-copy 恢复：同文件系统 staging、双流 ffprobe、全包 packet scan、
  SHA-256、reservation 原子发布和 `legacy-hls-recovery.v1` 回执全部通过后才
  暴露 `.mp4`。外链 URI、非终态 playlist、缺音/视频、既有目标或回执冲突均
  fail closed，继续保持 `source_incomplete`，不得把人工 remux 当成无证据旁路。
  `source_incomplete` 日期即使已老于普通 latest-3 cron 窗口也必须继续进入
  tick；恢复成功后还要在 `source_recoveries` 已绑定且状态为 sealing/processing、
  pending 队列未清空、或 picks/songs 仍含可恢复失败、封面待补及 fingerprint
  可唤醒的失败行期间持续可见，避免刚从 `source_incomplete` 改成 `sealing`，或第一次
  生产落到 `review_ready_with_failures` 后就在下一 tick 前老化消失。只有所有恢复工作
  真正收敛到无 pending、无未解失败的 `review_ready` 才退出 latest-3 例外；不得借机把
  其他历史完成日期按新 fingerprint 全部重跑。
- 源完整性：视频流独立解码零损伤（`source_integrity.py`）；BLOCK≠ok、
  0 交付≠done。
- 杀开关：`touch /opt/bilive/autoslice/DISABLED`。
- 正式部署把 `ops/` 与 `scripts/src/assets/...` 一起纳入 committed tree manifest。
  外部 `/opt/bilive/recording/bililive_recorder_adapter.py` 必须由
  `scripts/deploy_free_autoslice.sh` 在 preimage/rollback 事务中从已切换的 commit
  原子安装并做 SHA-256 readback；不得手工 `cp`。只有 recorder fresh-idle、
  CloudFS host/container mount 都为绿时才能重启 `bililive_adapter`；安装后的任一
  deploy 失败必须原子恢复 preimage 并在同样安全门下重启，无法证明时保留
  `DISABLED` 与 deploy guard。
- adapter 外部字节未变化时，部署前状态必须 fresh、idle、
  `service_reachable=true` 且 `error=null`。只有待安装 adapter 字节确实变化时，才允许
  用单一修复例外越过旧 adapter 自己制造的错误：旧状态仍须 fresh/idle，且必须精确为
  `service_reachable=false` 与 `error` 前缀 `source disposition drift:`；同时 recorder 与
  adapter 运行、host/两容器 CloudFS、旧 host/container adapter SHA 以及独立 GraphQL
  idle 查询必须全部通过。其他错误一律拒绝。新字节重启后仍须等到 fresh clean 状态、
  新 SHA 与健康检查全绿；该例外不得成为普通 deploy 或 live-query bypass。
