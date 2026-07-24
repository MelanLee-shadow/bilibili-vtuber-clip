# 60 标题

本文件是标题步骤的**分步权威**。LLM few-shot 语料与完整风格规范的强权威是
`assets/lidousha/title_style.md`（prompt 注入用的就是它）；本文件记录硬规则与强制层位置。

## 共享发布标题门

- 人工标题与自动标题都必须经过
  `title_policy.canonicalize_publish_title` 和
  `title_policy.publish_title_policy_violations`。同一门由
  `publish_staging.py`、package auditor 与 `authorized_upload.py` 分别调用；任何入口都
  不能靠 `title_llm_call=None` 或手工 JSON 绕过。
- Ivan 手定标题拥有**正文 authority**：正文逐字保留，不送 LLM 改写，也不套自动标题的
  selection-hook/机器味重写；它不拥有绕过频道 archive envelope 的权限。talk 最终统一补
  `【李豆沙】`，song 统一成精确目录式；两者都验 12–48 字、外层空白与括号/引号栈。
- `assets/lidousha/manual_title_overrides.v1.json` 存正文，不存一条可免检的“最终发布标题”。
- 已发布 same-BV 的媒体恢复不得裸抄旧 record 的 `title`，也不得靠操作员逐条补几个可选
  evidence 参数。唯一入口是 deployable
  `assets/lidousha/recovery_publication_authority.v1.json`：它把 exact recovery 集合中的每个
  candidate 同时绑定到原 BVID/AID/CID、已验证 public receipt SHA 与标题模式。planner 必须
  用显式 asset SHA 一次解析**完整队列**，缺任一 candidate 即拒绝；生成的
  `recovery-same-bv-publication-authority.v1` 随 queue→spec→record→publish draft→review
  manifest→authorized manifest→repair plan 全链传递。`verified_public_exact` 逐字保留已
  合规的公开标题；`ivan_manual_override` 则要求 registry 中的旧公开正文与现有 Ivan 手定
  正文完全相等，再统一补当前频道前缀。历史 `authorized-upload-public-verify.v2` receipt
  保留作 registry 的生成/本地复核证据；production 只依赖受管部署的 hash-bound asset，不
  依赖默认不部署的 `reports/`。任一 asset、身份、模式、标题或 surface 漂移都阻断。

## 歌切标题（铁律，Ivan 2026-07-14 定、2026-07-19 重申）

- 格式固定：`【李豆沙】豆沙歌，《歌名》`。《歌名》前后**不加任何字**——禁止 `｜副标题`、hook 尾巴（"《宝贝》哄你睡觉"式）、"直播间唱"衬词。
- 《歌名》用边界/LRC 验证过的 canonical 歌名，不用 ASR 拼写。
- 主要强制层：
  1. profile 模板 schema 校验：`src/autoslice/channel_profile.py`（`song_plain_template` 必须恰为 `song_prefix + 《{song_title}》`，违规模板加载即报错）；
  2. song lane canonical override：`src/autoslice/song_lane.py::_apply_canonical_song_title`；
  3. 上述共享 publication choke point、package audit 与 uploader 复验。

## 谈话标题

- 权威：`assets/lidousha/title_style.md`（结构谱系、词库、违禁词）+ `assets/lidousha/title_policy.json`（违禁词/长度的确定性门，`title_policy.py` 加载）。
- 核心原则：标题围绕李豆沙本人；替换成任何别的主播还成立的标题就是失败。
- 自动标题除共享门外，还受违禁词与 selection-hook 锚点约束；失败可做有界重写。
  人工正文不自动重写，但结构/长度不合规仍 fail closed 并要求修正文档 authority。
- recovery publication authority 是“修媒体时固定原 BV 身份与复用哪种已审标题来源”，不是
  自动标题。两种模式都不调用标题 LLM，且仍须通过 StoryContract 与共享发布标题门；staging
  分别写 `recovery_verified_same_bv_public_title / RESOLVED_RECOVERY_PUBLIC` 或
  `ivan_manual_override / RESOLVED_MANUAL`。
- `（）()/【】[]/《》/“”/‘’` 必须按栈正确成对；多余右符号、交叉闭合或缺右符号均记
  `unbalanced_title_marks`，并在任何封面调用前否决。

## 封面嵌字与标题的关系

- 封面文字 = 标题去前缀（歌切即 `《歌名》`，大字 banner）；从不用冒号，分句用换行。细则见 [70-cover.md](70-cover.md) 与 `.agent/skills/lidousha-title-style/SKILL.md` §档案标题 vs 封面嵌字。
