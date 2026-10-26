# 李豆沙频道资产

这里保存李豆沙频道的词面、名单、身份事实和编辑参考。资产由
[`profiles/lidousha/profile.json`](../../profiles/lidousha/profile.json) 注册，完整索引见
[`asset_catalog.v1.json`](asset_catalog.v1.json)。索引只描述归属和公开边界，不改变任何资产的权威性、路径或字节。

## 权威边界

执行规则仍以流水线分步文档为准：

- [选题与配额](../../docs/pipeline/20-selection.md)：metric、校准资产和按日期的配额资产由选题消费者读取。
- [字幕文本](../../docs/pipeline/40-subtitle-text.md) 与 [语义修复](../../docs/pipeline/41-semantic-repair.md)：词表、名单、字幕风格和语义证据的当前消费边界。
- [标题](../../docs/pipeline/60-title.md)：档案标题、歌切标题和封面文案的分工。
- [封面](../../docs/pipeline/70-cover.md)：源画面身份、场景构图和梗字事实边界。

资产只提供频道事实、词义、名单和风格参考，不复制上述文档的执行门。历史候选、恢复回执和发布登记保留原位置；它们不会因为被目录收录就变成当前默认规则。

## 2026 年 8 月至 10 月核对

- 选题资产已包含 2026-08-07 游戏场、2026-08-08 事件场和默认日额度的日期化授权；`slice_selection_metric.md` 的 v6 还记录了 2026-10-02 对完整落点的优先级调整。评分回执的 `VALID` 只代表评分结构可复算，不能单独当作可发布结论。
- `glossary.txt` 已包含已核实的同团人名、梗词和精确词条，并明确“小室”作为人名时的写法与普通“两小时”等同音词的语境区分。本次目录核对没有在活动资产中加入未经公开证据支持的新 9 月或 10 月词条。
- `title_style.md` 保留 2026-09-08 的标题生成取舍；封面梗字可以在事实有依据时语义改写、缩写或补充解释，不再被错误限定为标题连续片段。
- `persona.md` 保留当场外观和身份锚点；游戏或观看场景可以由当场画面承担视觉主位，但人物身份仍须来自该场源画面。
- 字幕的 2026-09 默认样式、观看他人视频时的显示去重、声源身份诊断和语义修复边界仍只在 40/41 步维护；本目录不另写一套规则。

本轮核对以现行消费者和上述五个入口为范围；它不替代后续新证据的审查。若出现新词或新风格事实，应先记录公开来源和适用范围，再更新对应活动资产。

## 分类与公开导出

`asset_catalog.v1.json` 将顶层资产分为五类：

| 分类 | 内容 | 默认公开处置 |
| --- | --- | --- |
| `active_channel_data` | 当前词面、名单、关系、选题校准和日期化授权 | `public_with_path_filter` |
| `active_style_reference` | 身份、字幕、标题、封面和交付风格参考 | `public_with_path_filter` |
| `historical_candidate_authority` | 候选、说话人、审阅和回放历史 | `internal_only` |
| `recovery` | 修复、迁移和恢复证据 | `internal_only` |
| `publication_registry` | 发布与归档登记 | `publication_registry_only` |

公开导出仍须对内容中的路径和隐私字段做过滤；目录本身不授予发布权限。历史或恢复资产不移动、不删除、不改写，以免破坏已有绑定。

## 现行消费者对照

| 消费面 | 直接入口 | 主要活动资产 |
| --- | --- | --- |
| 选题 | `docs/pipeline/20-selection.md`、`src/autoslice/selection_scorecard.py` | `slice_selection_metric.md`、`selection_score_calibration.v1.json`、`talk_quota_policy_authority.v1.json`、`game_glossary.v1.json` |
| 字幕与语义 | `src/autoslice/producer_text_pipeline.py`、40/41 步 | `glossary.txt`、实体/名单资产、`subtitle_correction_principles.md`、`subtitle_truth_ledger.v1.json`、timely term 资产 |
| 标题与标签 | `docs/pipeline/60-title.md`、`src/autoslice/title_policy.py`、`src/autoslice/upload_tag_policy.py` | `title_style.md`、`title_policy.json`、`upload_tag_policy.json`、`manual_title_overrides.v1.json` |
| 封面 | `docs/pipeline/70-cover.md` | `persona.md`、`cover_identity_prompt.txt`、`cover_art_direction.md`、`cover_reference_overrides.v1.json`、`emote_library.v1.json` |

维护活动资产时只改对应数据或风格文件，并在目录中补充来源和适用范围；执行逻辑的变化回到对应 pipeline step 和其强制消费者维护。
