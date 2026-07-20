---
name: bilive-autoslice-publish
description: "李豆沙(房间22966160)切片从候选到 B 站发布的项目权威标准：候选管线、字幕权威、标题/封面/元数据规范、本地交付布局、投稿通道、入合集、公开验证。任何投稿/交付动作前必读。"
---

# Bilive Autoslice Publish（项目权威版，2026-07-19 修订）

本文件是**项目内唯一权威**（对 codex 和 Claude 会话同等生效）。`~/.codex/skills/bilive-autoslice-publish/SKILL.md` 是历史版本，其中 season/switch 等段已过时——以本文件为准。修订依据：2026-06-19~22 codex 实证 + 2026-07-03~04 Claude 实证（含真实投稿 BV1qxMc6XEM9）+ 2026-07-13~14 批量发布/换源实证（tag 新口径、歌切无片头、编辑修正总则、验收 checklist）+ 2026-07-18 Z1 三句版固定谈话片头换版。

**快速导航（成片之后按此走，别再翻散落文档）**：标题→§标题/封面/元数据 + `.agent/skills/lidousha-title-style/SKILL.md`；封面→§封面样式表；tag→§标签；片头→§片头；投稿/入合集/验证→§发布流程；**已发稿件任何修正→§修正总则（编辑，绝不新传）**；每条发布完成与审计→§投稿后验收 checklist。

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
- **talk 成品字幕顺序（2026-07-10 起的强制契约）**：`聚合 ASR 句级毫秒时间轴 + draft → AGY 听音精修（可用时）→ CPA 对照裁决/词表/弹幕/SC 校正 → 全片人称定稿 → SOURCE_INTERVAL_TRUTH 源时间轴真值 → CAM++ 李豆沙声纹 + 全片语境二分色 → 可选 hash-bound 人工换人/拆句/抢话真值 → 分色 ASS → 烧录`。`scripts/free_asr_client.py`（必剪 bcut 主 / 剪映 jianying 备）仍拥有时间轴；AGY/CPA/人称层只改文本，不得重写时间。已知女主播（李豆沙、礼墨Sumi、安晚Awa 等）一律用“她”，已知男性用“他”，动物/物体用“它”，只有全文仍无法确认的人才用 `TA`；人称 pass 必须同时支持 `TA→她/他/它` 与错误性别代词 `→TA`。说话人阶段只消费已经 text-final 的 SRT，`--speaker-mode required` 且 fail closed；产出一份带 `[李豆沙]/[连线]` 的复核 SRT 和一份**无可见标签**的白/黄分色 ASS，烧录器只能使用 hash 匹配的该 ASS，不能临时重建成单色字幕。短句/阈值带必须被全片语境逐条回答，缺答只可由 hash-bound 人工真值补齐，否则整条拒绝交付；抢话 best effort，副说话人字词或区间不可靠时只保主说话人。人工改字必须重新跑说话人阶段再烧录，禁止直接改单个已烧录 SRT。**歌切字幕仍是 LRC 全局位移**，不进入 talk 说话人链（见 `docs/workflows/lidousha-song-finished-package-workflow.md`）。
- **精修证据分级（2026-07-19 强制）**：只有“直接音频精修”可作为独立声学 witness；API fallback 必须逐块绑定源视频、draft、refined 哈希和时间窗，最多记为 `context_bound_audio`，不得因 `rc=0` 就升级成独立 witness。字幕完整性保护按最小冲突片段回滚；cue 数量变化、删填充、拆并句不能触发整条 cue 回滚或整道 guard 跳过。无法用声学/语境/结构化 SC 证据裁决的高风险专名必须 hold，不能发布猜测。
- **VAD 时间轴 QA**：silero（free:/opt/bilive/vad/）只作正证据；唯一删除规则=卡住幻觉（重复文本+≥12s+零VAD）；起止吸附到语音岛。
- 精听分块 ≤5min/块（全段输入=agy 确定性空输出）；失败码区分 AGY_EMPTY_OUTPUT/AGY_TIMEOUT/AGY_FAILED_RC。
- 人工订正通道：Ivan 给出的字幕文本真值必须落到 `SOURCE_INTERVAL_TRUTH` 账本，以“源录像 basename + 源绝对毫秒区间”绑定，并通过 jump-cut piece 映射到每次重切；不得只绑 candidate id 或手改某个 SRT。账本条目必须区分“局部替换”和“整 cue 定稿”，应用失败、歧义命中或缺失 required 条目一律 fail closed。标题真值仍按对应 hash-bound 记录保存，不再烧配额重试。
- **词表是误听修复的第一通道**（Ivan 2026-07-04）：专名/近音误听（实例：一四二/伊索尔→`142`、刘彩→李豆沙的自称误听、小寺→小室、阿朵→Ado）本应由纠正 LLM 靠词表修复——成品里出现未收录的误听 = 词表缺口。处置：立即把该词加进 `assets/lidousha/glossary.txt`（含"不要写成X"反例），跑 `scripts/sync_lidousha_assets.sh` 同步 free，**用新词表重出该切片**，而不是手改单个字幕文件。特别注意主播第三人称自称"李豆沙/小李/豆沙"的近音人名（刘彩、李彩类）都是误听。
- **CPA 文本校正的四类共性错误**（Ivan 2026-07-04，已写进 glossary 文本规则 + `_cpa_correct_draft_cues` 提示词，成品里再出现即为规则未生效需查）：ASR/纯文本校正听不见音频，反复在这几类栽跟头，必须按语境修——① **同音词按语境推测**（偷渡vs掏兜、反杀vs反沙）；② **不把口语词臆造成国家/地名/生僻专名**（奶油苏丹→奶油苏打）；③ **英文/日文外来词保留原文罗马字**，不硬拼谐音汉字（cream soda≠库里瘦的）；④ **代词与指代对象一致**，动物(猴/熊/猫/宠物)用"它/它们"不用"他/她"（他胸口→它胸口 同类）。发现新类别就补进 glossary 文本规则并同步。
- **superchat/画面文字：词表优先，图像只补未知（Ivan 2026-07-04 实测修正）**：主播念 SC/醒目留言/画面标题时念的是画面卡片原文，SC **不在 blrec 弹幕 XML 里**（XML 只存滚动弹幕）。**但实测结论：词表才是可靠权威**——"沙特琳→沙豆李"实测靠词表(context)能稳修对；而 agy 视觉对**花体/艺术字 SC 卡片识别本身会错**（把"沙豆李"读成"大小姐姐姐"），开图像校正反而让 CPA 整行改写、盖过词表、结果更糟。所以：① **已知专名一律进词表，以词表为准**（`_cpa_correct_draft_cues` 默认 glossary+context+danmaku 已够，沙豆李/142/掏兜/苏打/cream soda/它 全靠这条链修对）；② 图像校正(`_agy_screen_text_lines`, `produce_slice_package --screen-text`)**默认关**，只在某条切片的意思依赖**词表还没有的**画面文字、且卡片清晰可读时才开，且它是补充不是权威、绝不能盖过词表名字。agy 视觉读画面是它在新架构唯一独占职责，但花体卡片不可靠，将来 OCR 也一样受此限。

## 本地交付布局（Ivan 2026-07-04 明确）

给 Ivan 看的候选/成品一律放：

```text
<project>/lidousha/<YYYY-MM-DD>/
  <描述性名称>.mp4          # 候选预览或成品视频，扁平直放
  <描述性名称>.srt          # 成品干净文本（无说话人前缀）
  <描述性名称>.speaker.srt  # 带说话人标签的复核字幕（talk）
  <描述性名称>.speaker.ass  # 实际烧录的分色字幕（talk，无可见标签）
  <描述性名称>.record.json  # 绑定文本/ASS/最终烧录 MP4 哈希的成品记录
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
- **标签（Ivan 2026-07-13 拍板口径，取代旧的固定 6 位）**：
  - **基础位只有 4 个**：`李豆沙,虚拟主播,虚拟UP主,直播切片`（VUP 与虚拟UP主全重复、VTuber 与虚拟主播近重复，已砍）；其余名额给内容位，**每稿封顶 12 个**（BV1EQNk6KErE 12 tag 编辑提交+读回实测），单 tag ≤20 字符、不得含逗号。
  - **生成链（已全自动，端到端 live）**：producer 把 `suggest_upload_tags` 结果冻结进成品 `<stem>.record.json` 的 `upload_tags`（status=OK 才算）→ `authorized_upload.py make-manifest` 自动读取（`--tags` 可覆盖、`--no-tags` 退出）→ `do_upload.sh` 以 manifest 的完整 tag 行投稿；无 tags 的旧 manifest 回退基础 4 位。
  - **两层来源**：Layer A 确定性专名层（glossary + psplive_roster 规则表，专名绝不让 LLM 发明）；Layer B CPA 内容层（标题+字幕全文出 3~6 个通用内容词，过长度/去重/防幻觉专名校验）。合并优先级：基础位 > 人工裁定 > 专名（标题命中优先）> 内容。
  - **口径红线**：专名 tag 只出可搜索**正主名**（南町/伊索尔/礼墨Sumi），梗形态（大N老师/142/lmsm/豆町）只作触发面不出 tag；内容词必须"贴内容 × 通用可搜"，过专没人搜的词硬毙（彩排/宠粉/玩梗/热情邀约/初次登场/脑补剧情/粉丝互动/线下合照类）；半梗半内容词（坏女人/宿敌恋人）放行。
  - **tag 按最终成品字幕出（铁律）**：换源/字幕修复后必须 `scripts/suggest_upload_tags.py` 重算 + 人工过目，再用 `scripts/bili_update_tags.py`（plan-driven，inspect→apply，title_expect 前缀守卫，tag-only 编辑）落到线上；已知误听的临时裁定走 batch 条目 `suppress_tags/add_tags` 人工通道。
  - **单稿人工元数据高于自动 tag 策略**：Ivan 在创作中心人工调整过的完整 tag 集，登记在 profile 可选资产 `manual_archive_metadata`（李豆沙现行为 `assets/lidousha/manual_archive_metadata.v1.json`）。登记项的 `preserve_on_metadata_edits=true` 时，封面/换源/标题/简介/合集等后续编辑必须从创作中心全量克隆并原样保留该 tag 集；不得因为它不含基础 4 位或与旧自动建议不同就判为漂移，也不得运行 tag 重算覆盖，除非 Ivan 明确授权替换该稿标签。
- **分区/属性**：tid=21（日常），copyright=2（转载），source=`https://live.bilibili.com/`。
- **片头（成片结构，投稿前最后一道结构门）**：**谈话/活字乱刷/重交付一律强制前置 2026-07-18 Z1 三句版**「李豆沙一直是零，不对，李豆沙一直是为爱做一」。画面契约：中间“不对”保留原画幅，前后两句为右下角李豆沙区域放大的 1080p 无广告画面。`assets/lidousha/intro/branding_intro.v1.json` 以 `intro_id=huozi-lidousha-shiling-budui-weiaizuoyi-z1-v2`、SHA-256 `bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5` 绑定；`src/autoslice/branding_intro.py` 在最终 burn 内拼接并 fail-closed，成品 record.json 必须有 `branding_intro.status=PREPENDED` + `intro_offset_ms`。生产字节固定在 `free:/opt/bilive/autoslice/assets/intro/lidousha-branding-intro.z1-budui-20260718.mp4`（repo 树外，deploy 不得删）。**歌切一律不带片头直接进歌（Ivan 2026-07-14，commit cf09597）**——歌选择器唯一入口按政策忽略 intro manifest。审计口径：talk 无片头或绑定的 intro_id/hash 不是当前值=违规；歌切带片头=违规（需无片头重烧+换源）。`AUTOSLICE_BRANDING_INTRO=off` 仅测试/应急，生产禁用。
- **合集（发布未入集 = 流程未完成；2026-07-20 起在上传工具链内强制）**：谈话 → `小李切片`；歌 → `小李歌唱`。合集 lane 由 `authorized_upload.py make-manifest` 按冻结标题确定性派生（歌切目录式前缀→歌合集，其余→切片合集）并写进 manifest（`--season none` 才可显式退出）；`upload` 投稿成功后自动等 state=0、现查 season/section ID、入集并**公开面复验**，rc=6=已投稿但入集未完成，用 `season-add --manifest` 幂等补挂/复验（证据落 `<stem>.season_verify.json`）。ID 永远现查不写死（工具即如此实现）；重复添加返回 20080=已在集，无害。

## 发布流程（每步都有实证，2026-07-04）

0. **前提**：Ivan 对该条明确授权；成品与字幕已过审（人工或审批 manifest）。
1. **投稿通道**：`biliup-rs`（`free:/opt/bilive/bin/biliup` v0.2.4）。
   - cookie：从 `/opt/bilive/app/cookie.json` 的 `data{cookie_info,sso,token_info}` 组装 biliup cookies.json；
   - **`biliup renew` 会轮换 token——用后必须把新 cookie_info/token_info 写回 `/opt/bilive/app/cookie.json`**（先备份），否则生产端登录失效；
   - 上传命令带全部元数据（--title/--desc/--tag/--tid/--copyright/--source/--cover）；
   - 旧通道已死：bilitool 客户端提交接口被 B 站停用（分片能传、提交报"投稿工具已停用"）；`src.upload.upload` 禁止作无人值守 worker（会投稿并删文件）。
2. **入合集（2026-07-20 起由 `authorized_upload.py` 内建，不再手拼 API）**：`upload` 成功后自动完成「等 state=0 → 现查 ID → episodes/add → 公开面复验」，rc=6 时用 `season-add --manifest <m>` 幂等重跑到 `IN_SEASON_PUBLIC` 为止。API 合同已编码进工具（camelCase `sectionId`+`episodes`、20080=已在集、**绝不信 add 的 code 0，只信公开 view 的 `ugc_season`+`is_season_display`**）。背景坑（改工具前必读）：
   - **season/switch 已死（-404），不要用**（2026-06-22 与 2026-07-04 两次实证）；
   - **snake_case `section_id`+`episode` 会返回 code 0 假成功但不生效**——这就是公开复验是唯一真值的原因；
   - 改标题后合集条目标题可能滞留旧值，用 `season/section/episode/edit` 修。
   - **biliup 上传只跑一次,绝不为取 bvid 重跑**：rc=0 即投稿成功,bvid 从 stderr 的 `ResponseData{...bvid: String("BV..")}` 抓,或查 `GET member.bilibili.com/x/web/archives?pn=1&ps=10&status=is_pubing,pubed,not_pubed`。重跑上传=重复稿件(2026-07-04 犯过,传了两条充电器)。
   - **稿件删除需验证码(340022),无法 headless 删**：`/x/web/archive/delete` 报"验证码错误"。重复稿件只能 Ivan 在创作中心手动删——所以务必一次投准。
3. **修正总则（Ivan 2026-07-14 重申：已发稿件任何修正 = 编辑原稿，绝不上传新视频，绝不删稿）**：
   - **为什么**：新投稿吃**滚动 24h 配额**（当日 ~10 稿实测 code 21566"投稿过于频繁"，2h/25min 重试均不解）；**编辑不占配额、不限次数**；删稿有验证码墙（340022）headless 不可行，且 Ivan 定过"发布即快照"。Ivan 说"删除"时默认指**本地副本**，B 站旧稿不动（2026-07-14 澄清）。
   - **按修正对象选通道**（都在 free 上跑，凭 `/opt/bilive/app/cookie.json`）：
     | 修正对象 | 通道/工具 | 要点 |
     |---|---|---|
     | 视频内容（换源） | `biliup append --vid <BV> <修正文件>` → `scripts/swap_video_p.py <BV>` | 动手前 `authorized_upload.py verify` 校 manifest——审过的文件才许换上去；append 把修正版传为新分P → edit 全量提交 `videos=[新P]` 移除旧P → 重审(-30) 几分钟回 state=0；**BV/aid 不变、合集 episode 按 aid 存活、标题封面不动**；换源后 **tag 必须重算**（见§标签铁律） |
     | 仅封面 | `scripts/bili_replace_covers.py`（inspect→apply 两阶段） | bfs 上传新图 + 全量 edit 只动 cover + 读回验证 |
     | 仅 tag | `scripts/bili_update_tags.py`（plan-driven，inspect→apply） | plan 带完整替换 tag 行 + `title_expect` 防错稿守卫；≤12，被拒自动 10 个重试探测 |
     | 标题/desc 等元数据 | `GET x/vupre/web/archive/view?bvid=` → `POST x/vu/web/edit?csrf=` 全量提交 | title/desc/tag/cover/videos（带 filename+cid）一起回填，漏字段=清空；改标题后合集条目标题可能滞留旧值，用 `season/section/episode/edit` 修 |
   - 每次编辑后都要**公开验证**（§4）并更新证据文件；编辑返回 code 0 ≠ 生效。
4. **公开验证（完成判据）**：`GET api.bilibili.com/x/web-interface/view?bvid=` 确认 `state=0`、标题、desc、`ugc_season.title` 与 `is_season_display=true`；标签用 `x/tag/archive/tags?bvid=`。证据存 `<clip>.public_verify.json` + `<clip>.uploaded.json`（bvid/aid/时间/工具/授权来源）到切片的 replacement_recuts 目录。
5. **证据入库（Ivan 规矩：授权上传的内容必须 commit，发布即快照）**：上传/换源/改封面/改 tag 的证据（`*.uploaded.json`、`*.public_verify.json`、results/manifest 文件）随批 commit（gitignore 已给 `*.uploaded.json` 留例外；媒体不入库）。幂等账本在 `free:/opt/bilive/autoslice/reports/upload_ledger.jsonl`（同一 artifact hash 重传=硬错误），launchd 镜像会拉回本地 `reports/slice_monitor/autoslice_free/`。

## 投稿后验收 checklist（每条发布/修正后过一遍；审计别人上传时逐项对抗式核对）

以 B 站**公开面+创作中心读回**为真值（工具返回 code 0 不算数）：

1. **稿件状态**：`state=0` 公开可见（-30=重审中可等；<0 其他值要查）；无同内容重复稿（同标题两个 BV=事故，报 Ivan 手动处理，headless 删不了）。
2. **标题**：带【李豆沙】前缀（歌切="【李豆沙】豆沙歌，"完整前缀）；Ivan 手定标题一字不改；无机器味词（直接/当场/秒X）与空洞标题党词；作品讨论类带《作品名》。细则见 `.agent/skills/lidousha-title-style/SKILL.md`。
3. **封面**：真 CPA 出图（非抽帧/模板）+ 本地叠字；样式表合规（奶油白字+深海军蓝描边、hook 词高亮、整张单字体、字号够大、feed 4:3 安全区 x∈[260,1660]）；表情不吐舌、不加当场没有的帽子/饰品/服装；多人场景主体=李豆沙；「自」字是否被 ZCOOL 渲成「白」形。
4. **tag**：基础 4 位在位 + 内容位合口径（§标签），≤12；换源过的稿件 tag 已按新字幕重算。
5. **简介**：两行逐字（主页+直播间）；tid=21、copyright=2、source。
6. **合集**：谈话在`小李切片`、歌在`小李歌唱`，`is_season_display=true`；改过标题的稿件合集条目标题未滞留旧值。
7. **片头**：talk 有 2026-07-18 Z1 三句版固定片头（record.json `PREPENDED`，intro_id/hash 与本节一致）；**歌切无片头**（2026-07-14 起）。
8. **修正方式**：所有修正走编辑通道（§修正总则），没有为修正新开 BV。
9. **授权链**：manifest 里有 Ivan 授权原话；上传方式=authorized_upload 通道（非裸 do_upload/biliup）。
10. **证据**：`uploaded.json`/`public_verify.json`/ledger 齐且已 commit。

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


## Ivan 工作流偏好（从对话反馈提炼，2026-07-06 固化；不需要他重复说）

- **元规则**：Ivan 的每条纠偏都是类规则，不是单例——当场提炼写进权威文件（本 skill / title_style / glossary / principles / metric），下次自动生效。他明确说过：一来一回的对话暗藏偏好和工作流习惯，要提取纳入工作流。
- **共性字幕事故的范围门**：Ivan 点名的切片和时间点只是 regression
  canary，不是修复范围。开始修复前必须同时从当日候选树、成功上传账本和当前
  BVID/换源记录反推完整集合，并逐条标成 `当前线上 / 未发布 / 被长版覆盖`。
  完成判据是“当前线上集合 = 审计集合 = current review index 集合”，少一条就
  只能报 `INCOMPLETE_SCOPE`。未发布候选也要检查共性规则；被覆盖的重叠短版
  记录检查结果但不得为了凑数重复烧录或产生第二份 current 交付。
- **外部视频说话轮次不是语言过滤**：不得用“日文/中文”或二分色的
  `[连线]` 标签单独决定删除。按完整 source turn 区分李豆沙本人、所看视频、
  游戏/系统公告和真实连线；所看视频的完整台词轮次排除（即使夹着中文），
  李豆沙本人穿插的日语保留，属于叙事本体的游戏/系统公告也保留。声纹可作
  证据，但模型标签在删 cue 前后不稳定时必须用 hash-bound reviewed turn
  封口，不能把不稳定标签当删除 authority。
- **上传授权语义**：Ivan 说「可以上传/直接上传」= 该条走完整发布链（biliup 单次 → 入合集 → 公开验证 state=0 → uploaded.json + public_verify.json → 按规矩 commit）；「不能上传」= 只 staged 交付；他没点名的候选 = 不做。
- **合并/长切片**：相关片段合并时按语义重推整体边界，叙事必须完整（触发点→发展→收束）；可用多 piece 掐纯岔话但保留桥接句（例：保留「日本那边作者没少干」桥进作者暴雷）；收束优先落在有立场/可引用的句子（例：「我们22966160是个温和派直播间」）。
- **作品讨论类切片**：标题必须带《作品名》；作品名从转写/弹幕双确证（例：《在意的人不是男生》= 弹幕原话 + 唱片店剧情描述吻合），确证不了就问 Ivan，不许猜。
- **专名闭环**：新专名（联动对象 礼墨Sumi/安晚awa、梗名 百破图/百乃工 等）出现即入 glossary/entity confusables → sync → 经完整正式流水线重出该切片；绝不手改字幕文件。混合字母专名（如 `kmx`，口语可念“kimo熊”）只能在发音近似、同段重复、结构化弹幕/SC 或明确语境至少一项支持时吸附；不得把单字“提”或常用词“提防”全局改成 `kmx`。SC sender 只有在主播同句出现“谢谢/感谢/念 SC”等动作锚点时才可覆盖同句人名，SC 卡片存在本身不能证明主播念了 sender。
- **新切/重切同管线**：任何“人工补切”“字幕修复”“同源重切”都必须调用当前 production text pipeline、完整性门禁、说话人链、回归 canary 和烧录哈希链；禁止以手工 ffmpeg/SRT 绕开最新流程。人工真值是流水线输入资产，不是流水线外的最终文件补丁。
- **标题语料铁律**：只有 Ivan 已上传/手定的标题算风格语料；free 生产线机器生成的旧标题不算（曾污染词库引入 直接/当场/疯狂/破防 等他从未用过的词）。
- **封面观感有分歧时**：推线上前把像素级证据摆给他（两版并排+指出差异），执行他的最终选择；封面可热换、可逆，别僵持。
