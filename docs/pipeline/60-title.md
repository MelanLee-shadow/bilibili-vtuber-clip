# 60 标题

本文件是标题步骤的**分步权威**。LLM few-shot 语料与完整风格规范的强权威是
`assets/lidousha/title_style.md`（prompt 注入用的就是它）；本文件记录硬规则与强制层位置。

## 歌切标题（铁律，Ivan 2026-07-14 定、2026-07-19 重申并三层强制）

- 格式固定：`【李豆沙】豆沙歌，《歌名》`。《歌名》前后**不加任何字**——禁止 `｜副标题`、hook 尾巴（"《宝贝》哄你睡觉"式）、"直播间唱"衬词。
- 《歌名》用边界/LRC 验证过的 canonical 歌名，不用 ASR 拼写。
- 强制层（三层，改规则先改这里）：
  1. profile 模板 schema 校验：`src/autoslice/channel_profile.py`（`song_plain_template` 必须恰为 `song_prefix + 《{song_title}》`，违规模板加载即报错）；
  2. 自动标题 choke point：`src/autoslice/title_policy.py::canonicalize_song_catalog_title`（带歌切前缀的自动标题一律折叠成目录式，`publish_staging._stage_publish_draft` 调用）；
  3. song lane canonical override：`src/autoslice/song_lane.py::_apply_canonical_song_title`（完整歌切最终以 canonical 歌名定形，覆盖记录 `title_before_canonical_override` 留审计）。

## 谈话标题

- 权威：`assets/lidousha/title_style.md`（结构谱系、词库、违禁词）+ `assets/lidousha/title_policy.json`（违禁词/长度的确定性门，`title_policy.py` 加载）。
- 核心原则：标题围绕李豆沙本人；替换成任何别的主播还成立的标题就是失败。
- Ivan 手定标题一字不改（`title_llm_call=None` 直通，不过任何门）。
- 自动标题强制【李豆沙】前缀、12–30 字（含前缀）、违禁词门 + selection hook 锚点校验（`publish_staging.py`）。

## 封面嵌字与标题的关系

- 封面文字 = 标题去前缀（歌切即 `《歌名》`，大字 banner）；从不用冒号，分句用换行。细则见 [70-cover.md](70-cover.md) 与 `.agent/skills/lidousha-title-style/SKILL.md` §档案标题 vs 封面嵌字。
