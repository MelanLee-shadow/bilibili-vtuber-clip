# 李豆沙 自动切片监控 (lidousha auto-slice monitor)

监控 free 主机上 bilive 流水线对 **李豆沙 (房间 22966160)** 的自动切片是否正常，
出问题时自动救援能救的、其余报警给方案；并让流水线在切片后**出标题+封面但不投稿**。

## 组成

| 文件 | 作用 |
| --- | --- |
| `scripts/lidousha_slice_monitor.py` | 监控主程序。SSH 进 free，探针 + 判定 + 安全救援 + 报告 + 邮件。 |
| `scripts/lidousha_slice_monitor.sh` | LaunchAgent 调用的包装脚本（设 PATH、写 cron.log）。 |
| `~/Library/LaunchAgents/com.ivan.lidousha-slice-monitor.plist` | 每 5 分钟跑一次（macOS 上 cron 的正解）。 |
| `reports/slice_monitor/latest.md` / `latest.json` | 最新一次判定（**主通道，始终可靠**）。 |
| `reports/slice_monitor/history/` | 每次判定的历史快照。 |
| `reports/slice_monitor/state.json` | 状态（用于只在状态变化时发邮件、崩溃恢复标记）。 |
| `reports/slice_monitor/cron.log` | 每次运行的滚动日志。 |

## 正确的“无发布”流水线（监控据此判断）

1. `src.burn.scan` —— 出切片 `.flv` + 字幕，入队 sqlite `upload_queue`。
2. `src.upload.local_prepare` —— 从队列出 **标题 + 封面 + `.publish.json`(`upload_enabled:false`)**，**绝不投稿**。
3. `src.upload.upload` —— ⚠️ 老的**会真投稿**的进程，**必须始终关闭**。监控发现它在跑会自动杀掉。

标题风格写在 free 的 `/opt/bilive/config/bilive.toml` `[slice] slice_prompt`（已改成你的风格，原文件已备份为 `*.bak-slice-prompt-*`）。改风格只改这一处，对之后所有切片生效。

## 判定等级

- ✅ OK / 🟡 WARN：正常或仅低风险（磁盘偏低等）。
- 🟠 DEGRADED：切片在退化（upload 在跑被杀、scan 报错、staging 落后、配置回退坏链路、出片但没标题封面）。
- 🔴 DOWN：探测不到容器、blrec 没了、或**李豆沙在播但切片没跑**。

## 自动救援（“能自动救的就救”）

- **永远**：发现 `src.upload.upload` 在跑 → 立刻杀掉（投稿不可逆且你明确不发布）。
- **录制看门狗**：用 blrec API 读 `live_status`+`rec_total`。李豆沙在播但**两轮 rec_total 完全不增长**
  （= 在播却没录上，2026-06-22 空转 90 分钟那种）→ 自动重启录制器（recorder disable→enable）。
  注意：Videos 是 123云盘挂载、文件 mtime 不实时更新，所以**直播判定以 API 为准**，不看文件新鲜度。
- 李豆沙**在播**、**无脏 backlog**、且开关已打开（`AUTOSLICE_ENABLED`）→ 自动启动 `scan` + `local_prepare`。
- 之前被监控接管过、现在崩了 → 自动重启（崩溃恢复）。
- 其余（录制链路坏、需要动你手动处理的旧数据）→ **只报警 + 给方案**，不自动动。

## 录制格式 & fmp4 补丁

- 当前 `stream_format=fmp4`（已验证音视频正常：AAC-LC/48k/有声、h264 720p）。
- blrec 默认对这房间(无大会员、最高 qn=250)拿不到 fmp4，开播瞬间会单向回退到 10000 卡死。
  **已打补丁** `/opt/bilive/app/patch_blrec.py`（容器启动自动重打）：让它录服务端实际在推的那一档（降级容忍）。
- **flv 是兜底**且音频也已验证正常；6/20 的“无声”是那天那条流坏(AAC profile=-1)，不是 flv 通病。
  要切回 flv：改 `/opt/bilive/config/settings.toml` `stream_format="flv"` 后重启 2233 的 blrec 进程。
- `delete_source=never` 必须保持（坏 remux 也不会丢原始源）。改成 `auto` 监控会报 DEGRADED。

## 自动切片开关

切片管线冷启动需要你点头一次：`touch reports/slice_monitor/AUTOSLICE_ENABLED`。
打开后，李豆沙开播（且 backlog 干净）就会自动跑 scan+local_prepare 出切片/标题/封面（不投稿）。
`touch reports/slice_monitor/FORCE_START_SCAN` 可无视脏 backlog 强制启动 scan。

### 脏 backlog 防护（重要）

`scan` 会扫全盘。若某个旧日期目录（如 `2026-06-20`，你正在手动修的坏音轨那天）顶层还留着原始录播，
冷启动 scan 会把它一起重切。所以**有脏 backlog 时监控不会自动启动 scan**，只报警让你先归档/隔离。
真要强启：`touch reports/slice_monitor/FORCE_START_SCAN`（监控下一轮会强行启动 scan）。

## 通知

- **报告文件**（始终写）—— `latest.md` / `latest.json`，唯一告警通道。
- **邮件已停用**（Ivan 2026-06-22 要求；脚本里 `EMAIL_ENABLED=False`）。
  要重启邮件：把 `EMAIL_ENABLED=True`，并配 `~/.config/lidousha_monitor/smtp.json`
  （iCloud `smtp.mail.me.com:587` + App 专用密码；Apple Mail 从 launchd 后台调用不可靠）。

## 常用操作

```bash
# 立刻手动跑一次
python3 scripts/lidousha_slice_monitor.py

# 看最新状态
cat reports/slice_monitor/latest.md

# 看定时任务是否在册（状态 0 = 上次正常）
launchctl list | grep lidousha

# 立刻触发一次定时运行
launchctl kickstart -k gui/$(id -u)/com.ivan.lidousha-slice-monitor

# 停用 / 重新启用监控
launchctl bootout gui/$(id -u)/com.ivan.lidousha-slice-monitor
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ivan.lidousha-slice-monitor.plist
```

退出码：0=OK/WARN，1=DEGRADED，2=DOWN。
