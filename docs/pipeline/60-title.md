# 60 标题

本文件是标题步骤的**分步权威**。LLM few-shot 语料与完整风格规范的强权威是
`assets/lidousha/title_style.md`（prompt 注入用的就是它）；本文件记录硬规则与强制层位置。

## 共享发布标题门

- 人工标题与自动标题都必须经过
  `title_policy.canonicalize_publish_title` 和
  `title_policy.publish_title_policy_violations`。同一门由
  `publish_staging.py`、package auditor 与 `authorized_upload.py` 分别调用；任何入口都
  不能靠 `title_llm_call=None` 或手工 JSON 绕过。
- 维护者 手定标题拥有**正文 authority**：正文逐字保留，不送 LLM 改写，也不套自动标题的
  selection-hook/机器味重写；它不拥有绕过频道 archive envelope 的权限。talk 最终统一补
  profile 的标题前缀（默认 profile：`【李豆沙】`），song 统一成精确目录式；两者都验 12–49 字、外层空白与括号/引号栈。
- `assets/lidousha/manual_title_overrides.v1.json` 存正文，不存一条可免检的“最终发布标题”。
- 单个候选若只有**公共生成文案**中的逐字专名裁决，而没有整份字幕/说话人的完整人工复审，
  只能使用
  `assets/lidousha/candidate_public_text_surface_authorities/<candidate>.public-text-surface-authority.v1.json`。
  它必须绑定 candidate、exact selected interval、带 padding 的 clip-context source pieces、source
  SHA、clip-context SHA、原 selection hook、被替换的机器标题，以及用户只授权的 exact
  substitution；冻结标题只能是该机器标题的最小字节替换，不得记成整题 维护者 手定标题。
  authority 仅约束 selection hook/title/cover/publication 的生成公共词面，不得修改或暗示已复审
  subtitle/speaker，不得解除 publication registry hold，也不得授权上传。title、封面 rendered
  lines、publish draft 与 package/current audit 必须消费并重验同一 authority/consumption；任一
  source/context/surface/receipt 漂移即阻断。`AUTOSLICE_HUMAN_TRUTH_MODE=withheld` 时 loader、
  prompt、staging、fingerprint 都不得读取或泄露该 authority，非法 truth mode 则 fail closed。
- 手定标题的 source-fact 门发现事实错误时仍默认 fail closed。例外只能是
  `assets/lidousha/manual_title_repair_authorities/` 中的单 candidate authority：它必须同时
  绑定原 title/hook、阻断该次修复的 source-fact receipt SHA、精确修正后的 title/hook 与用户
  授权原话/时间；任一不符即继续阻断。消费 receipt 必须随 publish staging 和 source-fact
  receipt 落入产物，不能成为通配的 CPA 改标题权。
- CPA 只对人工标题提出了**未获授权的改写**时，可用
  `assets/lidousha/authorities/<candidate>.manual-title-keep-authority.v1.json` 做一次候选级
  KEEP adjudication。它必须封存完整 FAILED receipt、原 title/hook、最终字幕、当次裁决使用的
  clip-context prompt、scorecard、speaker/entity evidence、维护者 的原题批准与发布授权；消费后写
  `PASS_WITH_RECORDED_DISSENT`，保留模型异议而不重掷 provider。任一 current bytes/hash/surface
  漂移都继续阻断，不能把这条门当成“人工标题永远正确”。fresh ASR 重建出的 clip-context
  只作为 diagnostic 另记 SHA；它不得替换、伪装或否决已经封存并绑定原 FAILED receipt 的裁决
  prompt。
- `auto_123655_1613_1676` 的 operator exact title/source-fact projection 只可消费
  `assets/lidousha/authorities/auto_123655_1613_1676.operator-exact-title-source-fact-authority.v1.json`。
  它逐字绑定 维护者 的 Claude JSONL 身份、line、时间和原话，及 exact title；并重放当前 SRT、
  final-review、chat、boundary、source media、StoryContract/context/scorecard 与完整 FAILED
  source-fact receipt。它只产生 `OPERATOR_EXACT_TITLE_WITH_RECORDED_SOURCE_FACT_DISSENT`：
  CPA 已支持的 `final_selection_hook` 是唯一 final hook；维护者 只裁定 exact title，
  `provider_pass_claim=false`，完整 CPA FAILED/REPAIR 回执仍保留，绝不可写成 provider PASS。
  此 authority 不授权字幕、媒体、边界、封面或上传；任一候选/标题/sidecar/hash 漂移都必须在
  provider 或封面调用前阻断。
- 已完整人审字幕但 source-fact provider 只返回不可用形状时，只有 deploy-sealed
  `deterministic-text-narrowing.v1` 才可把候选的旧人工 title/hook 收窄为封存的唯一新 surface。
  authority 必须保存原 FAILED attempt、逐字渲染计划、字幕/entity/uniform-host/source 区间与
  维护者/Pro 证据；runtime 重建 transcript/scorecard/entity，并从 Git/deploy-sealed 独立资产重放
  原裁决 prompt 后才可确定性消费，且不得再调 provider。fresh ASR context 只另记 diagnostic SHA，
  不进入裁决 identity。当前唯一 50-codepoint 标题例外也只删除该 candidate、该 exact title/hook/receipt 的
  `publish_title_length_out_of_bounds`；其他标题规则与其他候选仍按 12–49 字 fail closed。
- 已发布 same-BV 的媒体恢复不得裸抄旧 record 的 `title`，也不得靠操作员逐条补几个可选
  evidence 参数。唯一入口是 deployable
  `assets/lidousha/recovery_publication_authority.v1.json`：它把 exact recovery 集合中的每个
  candidate 同时绑定到原 BVID/AID/CID、已验证 public receipt SHA 与标题模式。planner 必须
  用显式 asset SHA 一次解析**完整队列**，缺任一 candidate 即拒绝；生成的
  `recovery-same-bv-publication-authority.v1` 随 queue→spec→record→publish draft→review
  manifest→authorized manifest→repair plan 全链传递。`verified_public_exact` 逐字保留已
  合规的公开标题；`reviewer_manual_override` 则要求 registry 中的旧公开正文与现有 维护者 手定
  正文完全相等，再统一补当前频道前缀。历史 `authorized-upload-public-verify.v2` receipt
  保留作 registry 的生成/本地复核证据；production 只依赖受管部署的 hash-bound asset，不
  依赖默认不部署的 `reports/`。任一 asset、身份、模式、标题或 surface 漂移都阻断。
- 若 Creator/public 的 BVID/AID/CID/标题已一致，唯一问题是 exact section
  episode title 仍为历史旧值，可以生成明示披露该唯一差异的
  `recovery-publication-identity.v1 / VERIFIED_TITLE_AND_TARGET_IDENTITY`
  作为已发稿修复的身份/标题证据；它不冒充完整发布验收，也不发生写操作。
  `repair-plan` 仍须现场重验单 P 和 exact section 身份，并由 one-shot
  `SECTION_TITLE_SYNC` 在换片成功后闭环这个已披露差异。

## 歌切标题（铁律，维护者定）

- 格式固定（默认 profile 示例）：`【李豆沙】豆沙歌，《歌名》`。《歌名》前后**不加任何字**——禁止 `｜副标题`、hook 尾巴（"《宝贝》哄你睡觉"式）、"直播间唱"衬词。
- 《歌名》用边界/LRC 验证过的 canonical 歌名，不用 ASR 拼写。
- 主要强制层：
  1. profile 模板 schema 校验：`src/autoslice/channel_profile.py`（`song_plain_template` 必须恰为 `song_prefix + 《{song_title}》`，违规模板加载即报错）；
  2. song lane canonical override：`src/autoslice/song_lane.py::_apply_canonical_song_title`；
  3. 上述共享 publication choke point、package audit 与 uploader 复验。

## 谈话标题

- 权威：`assets/lidousha/title_style.md`（结构谱系、词库、违禁词）+ `assets/lidousha/title_policy.json`（违禁词/长度的确定性门，`title_policy.py` 加载）。
- 核心原则：标题围绕主播本人；替换成任何别的主播还成立的标题就是失败。
- 自动标题除共享门外，还受违禁词与 selection-hook 锚点约束；失败可做有界重写。
  人工正文不自动重写，但结构/长度不合规仍 fail closed 并要求修正文档 authority。
- profile 中即使某个 canonical entity 只有一个 confusable group，也必须进入 producer 与
  `term_authority` 的共享实体上下文；不得因默认过滤 singleton 而让 `南天` 一类已登记误听
  在 transcript、hook、source-fact 与标题之间互相自证。登记只提供待裁决的 canonical/
  surface 关系，仍须由音频/人工 authority 判定：`南町nightin` 等合法复合面保留，普通
  false-positive 不机械替换；无法裁决时应阻断而不是生成自洽的错误标题。
- 自动 talk 标题 prompt 必须从**完整最终 SRT**（不是 600 字节选）扫描 profile
  `important_content_ips`，以「内容提及的重要 IP」字段携带 canonical 名、命中次数与表面证据，
  publish draft/staging 同步保存该 signal。它只供选材：内容中心时自然使用，偶然一提时可省略；
  不加入 required entity、不机械改标题、不因标题未出现该 IP 而报错。歌切目录式标题不接此信号。
- selection hook 属于生成摘要而非源字幕。恢复源 hash 导致旧 session-relation authority
  暂不可用时，如果最终 CPA/词表链已经在整片字幕中稳定落下登记规范专名，hook 内对应的
  **未登记**误听面可继承该 expected-value 证据机械规范化；普通短语 false-positive 必须
  保留，两个已登记专名冲突仍交 CPA，不能借字幕中任意出现一次就互换。
- **受话人归属判项（F12，维护者 2026-08-08 受骗片标题案纠错）**：source-fact 复审此前只验
  “这话说过没有”，不验“对谁说”，于是「李豆沙刚被劝别再受骗」这种把连线主持对上一位选手
  说的话安到主角头上的 hook 能整条通过。现在 `review_and_repair_source_facts` 额外接收一份
  **带说话人标签的最终转写**（`speaker-final.srt` 经 sha 绑定 + 与最终字幕逐条对齐才采用，
  `addressee_attribution.py`），判官必须对文案里每个「主角被 X / 主角对 X 说 Y」类归属断言
  产出 `SUPPORTED / WRONG_ADDRESSEE / UNVERIFIABLE`。缺该键即形状无效；
  `WRONG_ADDRESSEE` 与 `status=KEEP` 互斥（fail-closed 打回，判官必须给出去掉错归属的完整
  两份文案，走既有 REPAIR 复审环）；SUPPORTED/WRONG_ADDRESSEE 必须逐字引用
  `speaker_transcript:` 行。确定性反证：断言主语是主角（文案未点名连线）却只引用了**主角
  首次发言之前的连线台词**时，SUPPORTED 一律不成立。没有可信说话人标签（uniform_host 场、
  产物缺失或漂移）时只允许 `UNVERIFIABLE`，回执以
  `addressee_attribution_mode=unverifiable_no_speaker_transcript` 披露，**不拦发**。
  `SCHEMA_VERSION` 不变——它是已落盘回执的相等性锚。
- recovery publication authority 是“修媒体时固定原 BV 身份与复用哪种已审标题来源”，不是
  自动标题。两种模式都不调用标题 LLM，且仍须通过 StoryContract 与共享发布标题门；staging
  分别写 `recovery_verified_same_bv_public_title / RESOLVED_RECOVERY_PUBLIC` 或
  `reviewer_manual_override / RESOLVED_MANUAL`。
- `（）()/【】[]/《》/“”/‘’` 必须按栈正确成对；多余右符号、交叉闭合或缺右符号均记
  `unbalanced_title_marks`，并在任何封面调用前否决。

## 封面嵌字与标题的关系

- 封面文字 = 标题去前缀（歌切即 `《歌名》`，大字 banner）；从不用冒号，分句用换行。细则见 [70-cover.md](70-cover.md) 与 `.agent/skills/title-style/SKILL.md` §档案标题 vs 封面嵌字。
