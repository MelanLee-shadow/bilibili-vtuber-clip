# 会话主题提示（session theme hints）

## 目的与权威边界

不是所有直播都是游戏——静态语境（游戏词表/roster）对"这场在聊什么"可能全盲，
但主播自己的 B 站动态往往提前或同期点名了当天的主题（活动/公告/新话题）。

本通道由 `src/autoslice/streamer_dynamics.py` 实现：按频道 owner 的直播间 uid
抓取近期动态原文，把发布时间落在会话日 `[date-2d, date+1d]` 窗口内的动态作为
候选主题提示注入字幕修复 prompt（`gemini_slice_jingting.glossary()` 里的
`theme_hints_context()` 块）。主题提示**只是主题提示候选，绝不证明本句出现，
没有机械改字权限**；逐处仍由本句音频、结构化弹幕/SC 与语境仲裁。

## 数据面

- 快照：`state/streamer_dynamics.json`（schema `vtuber-slice.streamer-dynamics.v1`），
  由 `scripts/crawl_streamer_dynamics.py` 每日抓取写入。字段：generated_at/
  expires_at（48h TTL）/status/uid/room_id/items（每条 dynamic_id/published_at/
  仅结构化文本，无原始 HTML；控制字符已清理，指令形态文本一律拒绝）。
- uid 解析：`profiles/lidousha/profile.json` 的 `identity.room_id` →
  `https://api.live.bilibili.com/room/v1/Room/get_info` 拿 uid；动态列表走
  `https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space`（WBI 签名）。
  单轮 crawl 最多 3 次网络请求（room info + WBI nav bootstrap + 动态 feed）。
- 会话回执：`state/session_theme_hints/<date>.json`（schema
  `session-theme-hints.v1`），状态 `HINTS / NO_HINTS`。

## 运行面

Runner 在 `child_env_for_date` 里绑定 `LIDOUSHA_SESSION_THEME_HINTS`
(+`_SHA256`)；blind（`AUTOSLICE_HUMAN_TRUTH_MODE=withheld`）时禁用，除非显式
提供 `AUTOSLICE_BLIND_SESSION_THEME_HINTS`。快照缺失或过期时 fail-open：无提示，
绝不阻断产线。

Cron 由 `scripts/deploy_free_autoslice.sh` 与其余 crawler 同批托管（daily 06:47，
`streamer_dynamics_cron`），部署时自动幂等安装/替换，无需手动操作。

与 `session-game-context` 的分工：本通道管"这场在聊/办什么"，game context 管
"这场在玩什么"；同为 prompt 候选车道，互不覆盖。
