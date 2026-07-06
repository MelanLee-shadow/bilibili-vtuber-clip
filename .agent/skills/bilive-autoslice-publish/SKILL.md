---
name: bilive-autoslice-publish
description: "李豆沙(房间22966160)切片从候选到 B 站发布的项目权威标准：候选管线、字幕权威、标题/封面/元数据规范、本地交付布局、投稿通道、入合集、公开验证。任何投稿/交付动作前必读。"
---

# Bilive Autoslice Publish（项目权威版，2026-07-04 全面修订）

本文件是**项目内唯一权威**（对 codex 和 Claude 会话同等生效）。`~/.codex/skills/bilive-autoslice-publish/SKILL.md` 是历史版本，其中 season/switch 等段已过时——以本文件为准。修订依据：2026-06-19~22 codex 实证 + 2026-07-03~04 Claude 实证（含真实投稿 BV1qxMc6XEM9）。

## 目标状态（半自动）

1. `free` 录制直播 + 弹幕（blrec，`save_raw_danmaku=true`，弹幕在 `<日期>/sources/*.xml`）。
2. 候选发现与包装在 Mac 端管线完成（见"候选管线"节）。
3. 无人值守允许：候选切片、标题/封面草案、字幕草案；**无人值守禁止：烧录后直接投稿**（需人工批准，或 Ivan 对该条明确授权）。
4. 每条投稿都要 Ivan 明确授权；授权后按"发布流程"节走完全部步骤才算完成。

## Source Of Truth

- free 主机：`ssh free`；app 在 `/opt/bilive/app`（容器 `bilive_record` 内为 `/app`）；录像树 `/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/...`。
- B 站账号：烟雨平生一世（mid 55006782），凭据 `/opt/bilive/app/cookie.json`（登录 API 响应格式，data 里有 cookie_info/token_info）。
- 禁止打印 cookie、token、API key、含密钥的完整命令。
- 李豆沙知识资产（词表/标题风格/persona）：repo `assets/lidousha/`，改后 `scripts/sync_lidousha_assets.sh` 推 free。

## 候选管线（Mac 端，2026-07-03 后的现行标准）

Canonical 命令见 `docs/spark/2026-06-30-future-live-e2e-runbook.md`。要点：

- **语义召回**为主 lane（`--semantic-recall-llm-command`，观众视角），关键词 lane 只作 LLM 故障退路；弹幕突发 hints（`--danmaku-xml`）必带。
- **CPA 观众视角审查**：窗内真实弹幕 + 前后 90s 原文进证据；`VIEWER_CONTEXT_INCOMPLETE` 自动扩窗一次重审。
- **talk 成品字幕 = 聚合 ASR 基底 + CPA 纯文本校正**（Ivan 2026-07-04 定架构）：`scripts/free_asr_client.py`（必剪 bcut 主 / 剪映 jianying 备，快手已移除）出**句级毫秒时间轴 + draft 文本**（30min 段 ~17s），CPA(gpt-5.4-mini) 只按 `assets/lidousha/glossary.txt` 词表 + 同段弹幕改专名/梗词/谐音字，**LLM 只碰编号文本、时间轴按 ASR 原值拼回（结构性保时间轴）**。agy"听音频定时间轴"角色已废，仅作 `--correct agy`(多模态读帧,慢) 或 `agy_fresh`(ASR 全挂时兜底) 备选。为何用 CPA 不用 agy：校正是文本任务，CPA 是管线现成 judge、一次调用、拼回即保时间轴；agy 唯一独占价值(读画面像素)收窄为可选画面文字提取，将来可换 OCR。失败 fail-open 出裸 ASR draft。**歌切字幕 = LRC 全局位移**，聚合 ASR 只作参考轨（见 `docs/workflows/lidousha-song-finished-package-workflow.md`）。
- **VAD 时间轴 QA**：silero（free:/opt/bilive/vad/）只作正证据；唯一删除规则=卡住幻觉（重复文本+≥12s+零VAD）；起止吸附到语音岛。
- 精听分块 ≤5min/块（全段输入=agy 确定性空输出）；失败码区分 AGY_EMPTY_OUTPUT/AGY_TIMEOUT/AGY_FAILED_RC。
- 人工订正通道：Ivan 给出的字幕/标题真值即定稿，落 `*.human_corrections.json`，不再烧配额重试。
- **词表是误听修复的第一通道**（Ivan 2026-07-04）：专名/近音误听（实例：一四二/伊索尔→`142`、刘彩→李豆沙的自称误听、小寺→小室、阿朵→Ado）本应由纠正 LLM 靠词表修复——成品里出现未收录的误听 = 词表缺口。处置：立即把该词加进 `assets/lidousha/glossary.txt`（含"不要写成X"反例），跑 `scripts/sync_lidousha_assets.sh` 同步 free，**用新词表重出该切片**，而不是手改单个字幕文件。特别注意主播第三人称自称"李豆沙/小李/豆沙"的近音人名（刘彩、李彩类）都是误听。
- **CPA 文本校正的四类共性错误**（Ivan 2026-07-04，已写进 glossary 文本规则 + `_cpa_correct_draft_cues` 提示词，成品里再出现即为规则未生效需查）：ASR/纯文本校正听不见音频，反复在这几类栽跟头，必须按语境修——① **同音词按语境推测**（偷渡vs掏兜、反杀vs反沙）；② **不把口语词臆造成国家/地名/生僻专名**（奶油苏丹→奶油苏打）；③ **英文/日文外来词保留原文罗马字**，不硬拼谐音汉字（cream soda≠库里瘦的）；④ **代词与指代对象一致**，动物(猴/熊/猫/宠物)用"它/它们"不用"他/她"（他胸口→它胸口 同类）。发现新类别就补进 glossary 文本规则并同步。
- **superchat/画面文字：词表优先，图像只补未知（Ivan 2026-07-04 实测修正）**：主播念 SC/醒目留言/画面标题时念的是画面卡片原文，SC **不在 blrec 弹幕 XML 里**（XML 只存滚动弹幕）。**但实测结论：词表才是可靠权威**——"沙特琳→沙豆李"实测靠词表(context)能稳修对；而 agy 视觉对**花体/艺术字 SC 卡片识别本身会错**（把"沙豆李"读成"大小姐姐姐"），开图像校正反而让 CPA 整行改写、盖过词表、结果更糟。所以：① **已知专名一律进词表，以词表为准**（`_cpa_correct_draft_cues` 默认 glossary+context+danmaku 已够，沙豆李/142/掏兜/苏打/cream soda/它 全靠这条链修对）；② 图像校正(`_agy_screen_text_lines`, `produce_slice_package --screen-text`)**默认关**，只在某条切片的意思依赖**词表还没有的**画面文字、且卡片清晰可读时才开，且它是补充不是权威、绝不能盖过词表名字。agy 视觉读画面是它在新架构唯一独占职责，但花体卡片不可靠，将来 OCR 也一样受此限。

## 本地交付布局（Ivan 2026-07-04 明确）

给 Ivan 看的候选/成品一律放：

```text
<project>/lidousha/<YYYY-MM-DD>/
  <描述性名称>.mp4          # 候选预览或成品视频，扁平直放
  <描述性名称>.cover.png    # 封面
  README.md                 # 一句话索引（可选）
```

**不要深层嵌套**（reports/... 的管线证据树保留作 evidence，但浏览面必须是上面这个扁平目录）。预览用 copy-cut 快切即可（标注 ±2s 边界误差）。

## 标题 / 封面 / 元数据（发布硬标准）

- **标题**：风格规范与 few-shot 见 `.agent/skills/lidousha-title-style/SKILL.md`。发布标题一律带【李豆沙】前缀；**歌切完整前缀"【李豆沙】豆沙歌，"**；Ivan 给定的标题一字不改（只补前缀）。封面嵌字不带【李豆沙】。
- **封面**：真 CPA gpt-image-2 出**无字背景**（fail-closed，禁抽帧冒充；CPA 在 Cloudflare 后必须带浏览器 UA；524/520 瞬时可重试），标题**永远由本地脚本手工叠字**。构图/表情/背景/配色改为**按条 persona 驱动的 art-direction**——`_lidousha_cover_art_direction` 用 `sha256(candidate_id)` 稳定轮换 + persona 关键词词表，确定性选出 {角色/表情/背景/版式/hook 色/hook 词}，可选 CPA judge 精修（fail-open + 护栏，永不吐舌/油滑/性感）；`_lidousha_cover_prompt` 出无字背景，`_overlay_lidousha_cover_title` 本地叠字（三者均在 `scripts/run_auto_review_shadow_pipeline.py`）。art-direction 是 **fail-OPEN**（挑不到就回落确定性 baseline），但封面**图像**仍 **fail-CLOSED**（只认真 CPA 出图）。AI 无字背景永存 `covers_ai_original/`——嵌字错了只本地重叠不重新出图。**主工作流真的调用它**：生产交付器 `scripts/produce_slice_package.py` → `_stage_publish_draft` → `_stage_lidousha_ai_cover` 就是这条链，art-direction LLM **恒开**（人工标题也精修封面表情/版式，fail-open），上传用的 `.cover.png` 就是它产出的那张；`run_auto_review_shadow_pipeline.py --publish-staging` / `run_full_session_selector_cpa_shadow.py` 同链。**一次性重做/单条出封面**用 `scripts/regenerate_lidousha_cover.py`（同一套函数的薄封装：给参考帧或 media + 标题 → 新封面；`--reuse-bg` 只重叠不重出图）。

  **李豆沙封面标题视觉样式（Ivan 2026-07-04 权威定稿，禁止发挥）**——底色/字体/描边是不可动的权威基线；版式/背景/hook 色按条轮换，避免每张封面一样：
  | 项 | 值 |
  |---|---|
  | 字体 | 默认 `ZCOOLKuaiLe-Regular.ttf`（站酷快乐体，`assets/lidousha/fonts/`）。**一张封面字体必须统一**（Ivan 2026-07-05）：若 ZCOOL 缺该标题任一字形（如"镚"→豆腐块），**整张封面**换成完整字体**得意黑 `SmileySans-Oblique.ttf`**（同目录，可爱+全 GB），**绝不在一张封面里混字体**；不同封面可不同字（`_cover_font_for_text` 选）。字体缺失仍 fail-closed 拒绝出封面 |
  | 基线字色 fill | 奶油白 `#FFF6D6` = (255,246,214)——不可动 |
  | 基线描边 stroke | 深海军蓝 `#12244F` = (18,36,79)——不可动；浅色字只上深外描边，饱和色再加白内描边增强 pop |
  | hook 词高亮 | 封面文案里最该抓的一个原词用 accent 色，其余仍是基线奶油白；accent 从 {黄/粉/紫/蓝/橙/红} 按 hash **轮换**（不再只有黄+粉；**不含奶油**——会和奶油底色撞、高亮隐形） |
  | 文字底 | 默认 `_COVER_TEXT_BACKING="outline"`：**只靠粗描边**（深海军蓝外 + 白内）把字从高饱和背景托出，像参考图那样**不加暗底盒子**。可选 `glow`（贴字柔和暗光晕，非盒子）加深；旧圆角暗卡 `card` **已停用**——显难看方框，且它只是早期"白字加白内描糊字"bug 的补丁，bug 修掉后就多余了 |
  | 版式 | 按 `_lidousha_cover_art_direction` 轮换：谈话 `left-split`/`right-split`/`banner`（一侧大半身胸上像或下中，另一侧/顶部留大彩字空区），歌切固定 `song-clean`（柔和肖像 + 干净留白侧）。**取代旧的"居中/下方条带 + `chest_safe_top_y=680` 胸线禁区"**——身位与标题空区已在出图阶段按版式分开 |
  | 背景 | 按情绪选池：谈话用忙 {pop-art-burst / halftone-dots / speed-lines}，歌/温柔用净 {soft-radial / clean-scenic}；不再永远 pop-art |
  | 表情 | 贴该切片**角色**：默认软萌清纯邻家女同学（被欺负又软软反击），机灵鬼怪/得意仅角色需要时（次要）；**永不吐舌头**，不油滑/挑衅/性感（角色→表情映射见 `assets/lidousha/persona.md`） |
  | 外观 | 服装/肤色/发型/配饰照该切片**参考帧**原样（她每场不同装）；身份锚点只有熊猫头/熊猫耳/白毛/小李 |
  | 角度 | 默认 `-4°` 轻微左倾（谈话版式另用 -3/-2° 增加变化） |
  | 文本 | 封面内标题**不带**【李豆沙】，歌切也**不带**"豆沙歌" |
  | **feed 裁剪安全区（极重要，Ivan 2026-07-05）** | B站 **feed/首页/推荐**把 16:9 真封面**横向中心裁成 ~4:3**（高度不裁，宽 1920→1440，**每侧切 240px**；个别面到 1:1）——靠边的字/脸被直接切。所有标题文字收进**中央 `x∈[260,1660]`**（`_COVER_SAFE_X0/X1`＝真 4:3 裁剪带 240/1680 再各留 20px 缓冲；曾用更窄的 320/1600 导致字被过度压小，Ivan 打回后放宽到真裁剪带）；出图 prompt 的 "FEED-SAFE FRAMING" 让脸/关键元素也收进中央 4:3、外侧 ~13% 只放背景。验证：`封面.crop((240,0,1680,1080))` 看 feed 实际显示，字/脸不缺即安全 |
  | **文字排版（Ivan 2026-07-05）** | **大字自适应填满分区**（`_fit_cover_lines` 试 1..max_lines 行取最大可容字号，上限~300px），**描边宽随字号按比例放大**（不固定，否则大字显描边细）；**钩子词 / 英文数字串(kmx/TPL/0.5) / 歌名《…》绝不断行**（`_wrap_even` 当原子），**歌名《…》独占完整一行**并作钩子高亮；**绝不把标题原文喂进出图 prompt**（否则 AI 会把标题画进背景=双重文字，`_lidousha_cover_prompt` 已去 title、强化 render-NO-text） |

  **废弃样式（禁用，会误导）**：`~/.codex/skills/lidousha-ai-cover/scripts/render_lidousha_ai_cover.py` 的旧色——黄字 `#FFF7AA`+青蓝描边 `#00648C`+黑阴影/高光；以及字幕的 sapphire `#0F52BA`（2026-07-04 曾误用）。都不是封面标题色。
- **简介（两行，逐字）**：
  ```
  李豆沙个人主页：https://space.bilibili.com/1703797642
  李豆沙直播间：https://live.bilibili.com/22966160
  ```
- **标签**：`虚拟UP主,VTuber,直播切片,李豆沙,虚拟主播,VUP`
- **分区/属性**：tid=21（日常），copyright=2（转载），source=`https://live.bilibili.com/`。
- **合集（发布未入集 = 流程未完成）**：谈话 → `小李切片`（season 8383206 / 正片 section 9320779）；歌 → `小李歌唱`（season 8410735 / 正片 section 9364628）。ID 用前从创作中心现查（`GET member.bilibili.com/x2/creative/web/seasons?pn=1&ps=30`）。

## 发布流程（每步都有实证，2026-07-04）

0. **前提**：Ivan 对该条明确授权；成品与字幕已过审（人工或审批 manifest）。
1. **投稿通道**：`biliup-rs`（`free:/opt/bilive/bin/biliup` v0.2.4）。
   - cookie：从 `/opt/bilive/app/cookie.json` 的 `data{cookie_info,sso,token_info}` 组装 biliup cookies.json；
   - **`biliup renew` 会轮换 token——用后必须把新 cookie_info/token_info 写回 `/opt/bilive/app/cookie.json`**（先备份），否则生产端登录失效；
   - 上传命令带全部元数据（--title/--desc/--tag/--tid/--copyright/--source/--cover）；
   - 旧通道已死：bilitool 客户端提交接口被 B 站停用（分片能传、提交报"投稿工具已停用"）；`src.upload.upload` 禁止作无人值守 worker（会投稿并删文件）。
2. **补挂合集**（投稿后，state=0 再操作）：
   ```text
   POST member.bilibili.com/x2/creative/web/season/section/episodes/add?csrf=<bili_jct>
   JSON 正文（必须 camelCase）：
   {"sectionId": <section_id>, "episodes": [{"aid":..., "cid":..., "title":"【李豆沙】...", "charging_pay": 0}]}
   ```
   - **season/switch 已死（-404），不要用**（2026-06-22 与 2026-07-04 两次实证）；
   - **snake_case `section_id`+`episode` 会返回 code 0 假成功但不生效**——必须公开验证；
   - 重复添加返回 code 20080（已在合集中）；改标题后合集条目标题可能滞留旧值，用 `season/section/episode/edit` 修。
   - **biliup 上传只跑一次,绝不为取 bvid 重跑**：rc=0 即投稿成功,bvid 从 stderr 的 `ResponseData{...bvid: String("BV..")}` 抓,或查 `GET member.bilibili.com/x/web/archives?pn=1&ps=10&status=is_pubing,pubed,not_pubed`。重跑上传=重复稿件(2026-07-04 犯过,传了两条充电器)。
   - **稿件删除需验证码(340022),无法 headless 删**：`/x/web/archive/delete` 报"验证码错误"。重复稿件只能 Ivan 在创作中心手动删——所以务必一次投准。
3. **元数据修正**（如需）：`GET member.bilibili.com/x/vupre/web/archive/view?bvid=` 取当前稿件 → `POST member.bilibili.com/x/vu/web/edit?csrf=` 全量提交（title/desc/tag/cover/videos 带 filename+cid）。
4. **公开验证（完成判据）**：`GET api.bilibili.com/x/web-interface/view?bvid=` 确认 `state=0`、标题、desc、`ugc_season.title` 与 `is_season_display=true`；标签用 `x/tag/archive/tags?bvid=`。证据存 `<clip>.public_verify.json` + `<clip>.uploaded.json`（bvid/aid/时间/工具/授权来源）到切片的 replacement_recuts 目录。

## Pitfalls（历次真实踩坑）

- 编辑/入集接口返回 code 0 ≠ 生效，公开验证是唯一真值。
- biliup renew 后不回写生产 cookie → 生产登录失效。
- `member.bilibili.com/x/web/archive/view` 是 404，用 `x/vupre/web/archive/view`。
- CPA 封面 403 "no access to model gpt-image-2"=token 权限问题找 Ivan；520/524=瞬时重试。
- 候选预览 copy-cut 有 ±2s keyframe 误差，别当成品边界。
- 弹幕密度只是召回信号；"弹幕密度高"不是可发布的 hook。

**封面教训（Ivan 明确要求记入，"错误不重犯是基本要求"，都真实踩过）：**
- **一张封面字体必须整张统一，禁止逐字混排字体。** 我曾对 ZCOOL 缺的"镚"字做逐字回退（只那一个字换成别的字体），Ivan 打回"字体不一致、很别扭"。规则：一张封面 = 一个字体；ZCOOL 打不出标题任一字就**整张**换完整字体（得意黑），不同封面之间可以不同字体。见 `_cover_font_for_text`。
- **封面字号必须够大（反复被打回同一错）。** 长标题不要缩成小字。靠 ①多换行（Ivan 窍门：侧栏多换行让字更大又塞进半边）②均衡分行（别把长尾堆最后一行、那行最宽反而把整体字号压小）③安全区别过度收窄。真要更大只能缩短封面文案成钩子短句。
- **feed/首页/推荐会把封面横向中心裁成 ~4:3（每侧切 240px），靠边的字/脸被直接切。** 所有文字收进中央 `x∈[260,1660]` 安全带（真裁剪带+20px 缓冲；别再过度收窄到 320/1600——那会把字压小，违反"字必须够大"）；验证用 `封面.crop((240,0,1680,1080))` 看 feed 实际显示。
- **绝不把标题原文喂进出图 prompt。** 否则 AI 会把标题当漫画字画进背景 → 和本地叠字重叠成双重文字（反沙那张犯过）。
- **歌名《…》绝不换行**，必须独占完整一行；英文/数字串（kmx/shadowlee/lycoris）、高亮钩子词也不可被拆到两行。
- **CPA 图像模型会变**：`gpt-image-2` 掉线会被 `gpt-image-1.5` 顶替（`/models` 里查），找 Ivan 开权限。
