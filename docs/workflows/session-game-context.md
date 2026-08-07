# 会话游戏语境（session game context）

## 目的与权威边界

8/7 鹅鸭杀联动场实锤：房间标题是无关短语、分区停在虚拟Singer、全场弹幕零次提游戏名，
但转写里满是职业词（法医/警长/通灵者/复仇者/跟踪者）。静态语境（番剧图谱/roster）对
"这场在玩什么"全盲。

本通道由 `src/autoslice/game_context.py` 实现：按会话日从**录制侧证据**确定性解析游戏，
把该游戏的审定词表作为候选闭集注入字幕修复 prompt（`gemini_slice_jingting.glossary()`
里的 `game_glossary_context()` 块）。候选闭集**只证明词面存在，绝不证明本句出现，
没有机械改字权限**；逐处仍由本句音频、结构化弹幕/SC 与 CPA 语境仲裁。

## 数据面

- 注册表：`assets/lidousha/game_glossary.v1.json`（schema `vtuber-slice.game-glossary.v1`）。
  每游戏：canonical/aliases、**带 https 来源引证**的 sources、`detection_surfaces`
  （多字特征词面）、`terms`（role/role_community/mechanic/map/faction）。人工审定资产，
  新游戏须配来源；误听面初期留空（两字游戏词一律交 CPA，不入机械车道）。
- 会话回执：`state/session_game_context/<date>.json`（schema `session-game-context.v1`），
  状态 `RESOLVED / NO_MATCH / AMBIGUOUS`，绑定注册表 sha 与输入 inventory 摘要。

## 检测规则（确定性）

按日扫描：录制 XML 元数据（blrec `metadata/*` 与录播姬 `BililiveRecorderRecordInfo`
两种布局的标题/分区）、全场弹幕文本、选片 BCUT 草稿（`cache/<date>/*.bcut.srt`）。

- 标题/分区命中游戏别名 → 单独即可 RESOLVED；
- 否则弹幕+草稿需 ≥3 个**不同**特征词面且合计 ≥6 次命中；
- 两个游戏同时达标 → AMBIGUOUS，不注入；
- 任何 IO/校验失败 → fail-open 保持无语境，绝不阻断产线。

Inventory（文件名+大小+mtime）不变时直接复用上次回执，直播夜每新增一段只重扫一次。

## 运行面

Runner 在 `child_env_for_date` 里绑定 `LIDOUSHA_SESSION_GAME_CONTEXT`(+`_SHA256`)；
blind（`AUTOSLICE_HUMAN_TRUTH_MODE=withheld`）时禁用，除非显式提供
`AUTOSLICE_BLIND_SESSION_GAME_CONTEXT`。AUTOSLICE_SUMMARY 有"游戏语境"披露行。

与 codex 社区通道的分工：streamer_registry/community_names 管**人物称呼**，
本通道管**游戏词汇**；同为 prompt 候选车道，互不覆盖。
