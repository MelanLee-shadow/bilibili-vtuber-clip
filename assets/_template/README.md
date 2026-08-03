# assets/_template — 新频道最小资产骨架

由导出器从默认 profile 的资产**结构**自动派生（内容清空）。用法：

```bash
cp -r assets/_template "assets/<your-profile-id>"
```

- 每个文件的 schema 形状合法，但内容是空的/占位的：骨架只保证
  `validate_channel_profile.py` 的路径与形状检查通过，各 lane 首跑仍会按
  fail-closed 原则告诉你缺什么内容。
- 逐文件语义见 `profiles/README.md` 的对照表；参考实例见 `assets/lidousha/`
  （另一个频道的实战沉淀，只作 schema/风格参考，不要继承其专名）。
- `fonts/` 需要你自备可再分发的 CJK 字体（默认 profile 用 ZCOOL 快乐体 +
  得意黑，见其 fonts 目录与许可）。
- `voiceprint_profile.v1.json` 是 UNCONFIGURED 占位：声纹属于生物特征，须
  自己 enroll 后用 `scripts/install_voiceprints.py` 安装。

## 分层：谁在什么时候填什么

- **层 0 · 默认给全（agent 独立完成，开箱即用）**：`fonts/` 两个开源字体
  （直接用）、`title_policy.json`、`upload_tag_policy.json`、
  `subtitle_correction_principles.md`（示例频道完整口径，可改）、
  `bilibili_gift_names.v1.json`（B站平台礼物专名，直接用）、`intro/`（默认关）、
  `entity_confusables.json`/`known_songs.json`/`clip_opening_address.json`
  （积累类，空起步）。
- **层 0.5 · 框架直用、定义必改**：`slice_selection_metric.md`——分层框架与
  七维算术通用，但**第一层「频道命脉题材」的定义必须换成你频道自己的**；
  该文件会注入选题 prompt，判例人名已全部占位化——把占位角色换成你
  频道的真实对应者即可。
- **层 1 · 先问后写（agent 拿问题清单问频道主人，答完代写）**：
  `glossary.txt`、`persona.md`、`title_style.md`、`cover_identity_prompt.txt`
  ——每个文件内已写好该问的问题与示例；先写 3–5 条就能开跑，之后边用
  边攒，**不要求一次写完**。频道主人不在旁边时的代查证据源：
  B 站 `live_user/v1/Master/info?uid=` 免签给 room_id/粉丝勋章名（粉丝团
  称呼）；萌娘百科条目；**抽真实直播帧取证**（外貌/装饰以帧为准，文字
  资料常错）。代填的条目标注待频道主人拍板。
  **封面外貌事实三处必须同步改**：`profile.json` 的 `identity.cover_identity`
  九键、`persona.md`、`cover_identity_prompt.txt`——只改其一，封面身份
  终检会按不一致的那份把成品拦下（先抽帧、后写、三处一起写）。
- **层 2 · 你给种子，crawler 代填**：`timely_term_seeds/sources` → 
  `timely_terms`、`psplive_roster_sources` → `psplive_roster`、
  `topic_entity_graph`（参考部署默认装 cron；不走 deploy 就手动跑或自配）。
- **层 3 · 运行时/人工裁定自己长出来**：`subtitle_truth_ledger`、
  `session_relation_ledger`、`published_songs`、`speech_memory_ledger`、
  `selection_score_calibration`（随运营积累标定锚点）、各 manual/cover
  override（`manual_title_overrides`/`manual_archive_metadata`/
  `cover_reference_overrides`）与各评审目录。
- **层 4 · 用到对应功能才配**：`voiceprint_profile`（声纹栈）、启用片头。

## 首跑前最小清单

- `upload_tag_policy.json` / `title_policy.json` 骨架带中性默认值，**开箱可
  加载**（loader 在 import 时执行内容契约：base_tags 非空、banned_regexes
  槽位 0 存在、tag prompt 必须保留 {existing_tags}/{title}/{srt_text} 三个
  占位符）。先跑通，再替换成你的口径。
- `intro/branding_intro.v1.json` 默认 `enabled: false`（关闭片头）。启用前把
  schema_version 的 `REPLACE_ME` 改成你的 profile-id，并按
  `src/autoslice/branding_intro.py` 的契约补 `intros`。
- 各 JSON 中 `REPLACE_ME-…` 形态的 schema_version 都指 profile-scoped
  schema：改成 `<你的profile-id>-…`。
- 测试套件以**默认 profile** 为基准：跑 `pytest` 时不要设置
  `AUTOSLICE_PROFILE`（约 11 个用例直接断言示例 profile 的资产内容）。
