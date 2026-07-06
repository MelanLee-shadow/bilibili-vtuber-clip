# vtuber-slice 交接（HANDOFF）

> 约定：每次实质进展或会话收尾更新本文件（五段：目标/已完成/进行中/阻塞/下一步）。
> 开工先读本文件 + AGENTS.md，别凭旧对话推断。

## 目标（2026-07-04）

无人值守/半自动的李豆沙直播切片流水线。本轮核心：Ivan 审 7/3 竖屏直播切片后给出多类字幕/标题/构图反馈，
要求**把每类反馈当成流水线通病在代码里修掉、并固化进单一权威**（绝不做单例修补）。
关键洞察（Ivan）：**每个 prompt 都只带了本轮反馈的几条规则，没带项目历史积累的全部原则——prompt 不够强，且这个病可能在多处**。
7/3 六条成品在 `lidousha/2026-07-03/` 待审。

## 已完成（2026-07-04 本轮）

1. **字幕校正原则单一权威化（治本）**：新建 `assets/lidousha/subtitle_correction_principles.md` = 字幕文本校正唯一权威，
   收敛了此前**只活在代码提示词里**的操作规则（幻听置空删除、SC价格忽略、花体字会误读别盖词表、口误保留、咳嗽删语气词留、
   时间配对±10s、歌切文本以LRC为权威、人工订正即真值、自称误听/kmx 元规则）。
   改 `gemini_slice_jingting.glossary()` → 返回 `术语表+全量原则`，**所有**校正提示词一处改动全局同步；
   CPA 校正/裁决删掉重复硬编码换指针；`glossary.txt` 瘦身为纯术语表；`sync_lidousha_assets.sh` 增推 principles。
2. **BCUT×AGY×CPA 裁决框架修正**（Ivan：BCUT 很准只专名弱，AGY 只补专名/同音字，不采纳 AGY 对普通措辞的改写）。
3. **P0 标题（subagent T）**：`title_style.md` 回灌全量权威；LLM 自动标题强制 12–30字/【李豆沙】前缀/7违禁词命中有界重试；
   **人工标题铁律直通**。**离谱**改两用词（只禁"X到离谱"后缀，留"越看越离谱"）。**P2 封面**注入 persona 身份。
4. **P1 术语门（subagent Q）**：新建 `scripts/lidousha_glossary_terms.py` 动态解析 30 canon+34 误听黑名单注入 judge，
   `terminology_ok` 不再空判（此前只喂 kmx 一词）。
5. **竖屏→横屏 pillarbox**：`_burn_preview_subtitles` 检测竖屏，模糊侧填充到 1920×1080。
6. **词表学习闭环验证（§十三）**：哄睡妈妈 kmx"给我总弄上妈妈了"声学不可恢复 → 加 glossary 种子 → 重出得"kmx就叫妈妈了"。
7. **通病·篇章级代词消解**：Ivan flag"他→TA（同学性别未知）"通用校正做不到（规则被长prompt淹没）→ 加专职 `_cpa_pronoun_ta_pass`
   （模型只判 cue 编号、代码确定性替换 他/她→TA、带 其他/他们 守卫、区分匿名未知 vs 具名已知如司马懿、门控+重试+fail-open）。
8. **CPA 用对 gpt-5.5（Ivan 纠正）**：根因——gpt-5.x 是原生 **Responses-API 推理模型**，我一直错用 `/chat/completions`（gpt-5.5 因此 503、gpt-5.4 空 content）。
   改 `scripts/llm_via_cpa.sh` 走 `POST /responses` + `reasoning.effort=medium` + max_output_tokens 16000 + 5 次重试（上游间歇返回空 reasoning-only 响应）。
   实测长prompt 8/8 稳定。一处改动，所有 CPA 任务（字幕裁决/代词/标题/艺术指导）升 gpt-5.5(medium)。硬约束：只走 CPA、不直连 sudocode、不动 CPA 服务。见记忆 `cpa-gpt5-responses-api`。
9. **测试**：全量 `pytest tests/` → 282 passed（含下条封面改版 +5 用例）。
10. **封面改版（通病·点击率，另一会话主线，真 gpt-image-2 验证）**：旧"居中人物+单色标题横条+大留白"千篇一律 → 改成
   **大头胸像怼一侧 + 半屏多彩艺术字 + 波普/场景背景填满**，按切片轮换布局、表情贴角色、外观随切片真实皮肤。全落
   `scripts/run_auto_review_shadow_pipeline.py`：新 `_lidousha_cover_art_direction`+`LidoushaCoverArtDirection`（确定性基线
   `sha256(candidate_id)` 轮换 layout/hook + 关键词角色→表情映射，可选 CPA judge 精修 **fail-open+护栏**，护栏丢弃吐舌/性感/挑衅）；
   改写 `_lidousha_cover_prompt`（四布局 left/right-split/banner/song-clean + 表情 + 背景池谈话忙歌净 + **外观随参考帧不锁服装** +
   **永不吐舌**，保留 panda/小李/熊猫/16:9）；改写 `_overlay_lidousha_cover_title`（**只靠粗描边**托白字=参考图不加盒子(深色卡已停用=难看方框)、**按段选描边**修白字糊、
   多色钩子词高亮色池轮换不含cream、分区自适应字号，ZCOOLKuaiLe fail-closed 不动）。新 CLI `--cover-art-direction-llm-command`，
   `produce_slice_package.py`/`run_full_session_selector_cpa_shadow.py` 已接线（art-direction LLM 复用 `llm_via_cpa.sh`，即走 gpt-5.5/responses）。
   契约守住：真 gpt-image-2 无字背景 fail-closed 禁抽帧，`cover_generation` 的 `model/method/fallback_used/ai_background`+`cover_status` 键不动。
   同步 `persona.md`(加"封面表情/角色映射"节)+`bilive-autoslice-publish/SKILL.md` 封面节+两 docs。验证 `lidousha/2026-07-04/_封面改版评审/round3/`
   4 张真图：**3 套不同皮肤**(0702蓝棉衣/0703 JK/0629熊猫兜帽演出裙)各随其参考帧=外观随切片证据，表情贴角色无吐舌，四布局+繁简背景对比。
   记忆见 `lidousha-cover-redesign-halfbody`、`cpa-real-ai-cover-always`（含 CPA 图像模型 gpt-image-2↔gpt-image-1.5 会掉线的实测）。

**7/3 交付（`lidousha/2026-07-03/`，全部 1920×1080）**：哄睡妈妈(kmx✅/贡丸删✅/虫儿飞留✅ Ivan确认真实回应/再睡✅)、
称呼大战(倒反天罡✅/kmx✅/SC谢✅ + 定稿标题"叛逆小李一定要喊kmx妈妈，kmx只好喊宝宝")、熊猫伪装、奶龙斗虫、直播腔(写非说✅/TA)、
虫儿飞歌(切片不动，标题→"温柔《虫儿飞》清唱甜美哄睡"，封面已换字)。

## 已发布（2026-07-05，Ivan 逐条授权，biliup 各只跑一次，全部 state=0 + 入合集 + season_display=True）

- 称呼大战 **BV1tdMT64E47** → 小李切片
- 虫儿飞 **BV1CWMT6EE6c** → 小李歌唱（豆沙歌，round3 温柔清唱封面 + pillarbox 横屏）
- 哄睡妈妈 **BV1yWMT6EEHF** → 小李切片
- 直播腔 **BV1yWMT6EEWD** → 小李切片
封面均用最新流程 `scripts/regen_covers_latest_flow.py`（只重出封面不动字幕/标题）。证据存各 `<cid>.uploaded.json`。
上传经 `free:/opt/bilive/app/tmp_manual_upload/`（**不是** `upload.sh` 那个会投稿删文件的守护进程）。
合集 API 偶发 20111「编辑过于频繁」→ 隔 15s 重试；新稿 state=-30 审核中时不能入集，等 state=0。

## 已完成（2026-07-05 Fable 复审轮：审阅 7/4–7/5 全部 Opus 会话的指令与改动）

逐条对照 5 个会话（B站字幕研究/BCUT接入/通病大修/封面改版/7/5切片）的用户指令与落地代码。
**结论：架构与决策基本合格予以保留**（单一权威原则文件、glossary() 组合加载器、三段式字幕、代词专职 pass、
llm_via_cpa.sh /responses+UA+重试、标题强制+人工直通、封面新系统+feed安全区、上传纪律），282 tests 复跑全绿。
**复审修正的问题**：① free 资产漂移（词表旧版+principles 缺失）→ 已 sync+md5 核对；② `free_asr_client.py`
AUTO_CHAIN 仍含已下线的 kuaishou（与文档/对用户报告不符）→ 移出 auto 链（实现保留可显式调用）；③ skill 安全区数字
漂移（写 320/1600，代码实为 260/1660）→ 修正并注明教训；④ title skill「离谱」缺两用词标注 → 补齐；⑤ 删死代码
`_wrap_cover_title_lines`（旧冒号时代包装器）+ 修 `_overlay_lidousha_cover_title` 过期 docstring（还写着深色字卡）；
⑥ `bili_replace_covers.py` 从会话临时目录入库到 `scripts/`（换封面是复发需求，工具不能只活在 /tmp）。
**留档不改的历史事实**：充电器曾重复投稿（BV1WEMA6TES6，Ivan 已删）；诊断期一次直连 sudocode 上游+key 进过命令行
（已被 Ivan 立规禁止，repo 无残留，llm_via_cpa.sh 有 NEVER bypass 注释）；7/5 旗舰用了 `--correct cpa` 而非默认三段式（续跑时纠正）。

## 进行中

- **[Claude 封面会话·已完成 2026-07-05]** 全账号(mid 55006782)**24 条李豆沙切片封面全部按新流程重做并已替换上线**(只换封面,标题/标签/合集未动;24/24 `COVER_UPDATED=True`)。含大字自适应(多换行+均衡分行)、feed 4:3 安全区(文字 x260–1660)、缺字整张换字体(镚→得意黑)、去方框/去 baked-text/永不吐舌/外观随切片(老切片无本地源→用其当前线上封面当参考帧)。换封面脚本已入库 `scripts/bili_replace_covers.py`(在 free 上跑,inspect→apply 两段,只改封面) + `scripts/regenerate_lidousha_cover.py`(出图);账号里少前2游戏视频非切片未动;规则固化进 `bilive-autoslice-publish/SKILL.md` 封面节 + 记忆 `lidousha-cover-redesign-halfbody`。**遗留**:含长英文串(shadowlee)/超长标题的封面字号有物理下限,要更大需把封面文案缩成钩子短句(待 Ivan 定)。
- **[7/5 直播切片轮·被会话切换中断 2026-07-05]** 6 段录播(19:30–22:24,~2h54m,均有声;监控"无音轨"告警是对 raw m4s 的误报)已勘查;5 条候选 spec 已写好(`reports/lidousha-autoslice-20260705/finals/specs/`,remote_media 已修正为 host 侧 123云盘路径——**教训:produce 的 ffmpeg 在 free host 直跑,不走容器,spec 别写 /app/Videos 容器路径**)。旗舰 A(cos/sin 数学兄弟情,21:30 段)已交付 `lidousha/2026-07-05/豆沙把数学讲成兄弟情虐恋.mp4`(边界过/timing_qa 干净/自动标题"【李豆沙】cos比sin自私？小李当场共鸣"),**但**:①封面 `BLOCKED_AI_COVER_REQUIRED`(CPA 出图失败,fail-closed 正确没造假,需重出);②该条字幕用的是 `--correct cpa`(纯文本校正),**不是**默认三段式 `bcut_agy_cpa`——续跑 B/C/D/E 以及 A 重出时应回到默认三段式(专名靠 AGY 听音兜底)。B(小猪熊猫)/C(有没有李豆沙)/D(游戏名笼子)/E(丧尸偶像) 4 条 spec 就绪未产出。free/本地均无遗留进程。
- 无后台进程。

## 阻塞

- 无硬阻塞。上传永远需要 Ivan 逐条授权。
- 重复投稿 BV1WEMA6TES6（充电器）需 Ivan 在创作中心手动删（API 删档要验证码，无法 headless）。

## 下一步

1. **熊猫伪装 / 奶龙斗虫：Ivan 决定不发**（2026-07-05）。7/3 已发 4 条（称呼大战/虫儿飞/哄睡妈妈/直播腔），本场收工。
2. ~~sync 推 free~~ **已完成（2026-07-05 Fable 复审时执行）**：复审发现 free 上词表停在 7/4 19:43 旧版（缺 kmx 主语误听种子）、`lidousha_subtitle_principles.md` 从未推上去——已跑 `sync_lidousha_assets.sh`，三个文件 md5 与本地一致。教训：以后跑 sync 别 `>/dev/null 2>&1` 吞错，跑完 md5 核对。
3. **7/5 直播切片续跑**（见「进行中」，另一 Opus 会话在跑，别撞车）：A 重出封面+按默认 `bcut_agy_cpa` 复核字幕，B/C/D/E 按 spec 产出，全部拉到 `lidousha/2026-07-05/` 给 Ivan 审。上传永远逐条授权。**"仙童数学"已核实**（2026-07-05 Fable）：BCUT 声学层与 free 侧 ASR 两路独立听到 xiāntóng shùxué，上下文=她模仿数学UP收尾口播的自称，Ivan 确认写法"仙童数学"→已入 glossary（黑名单：先童/神童/线童数学）并 sync free，成品字幕现状即正确。
4. **封面改版验收**：Ivan 看 `lidousha/2026-07-04/_封面改版评审/round3/` 4 张 → 定稿后决定 commit。persona.md 目前只在 repo（sync 脚本不推它，free 端封面不消费它，无需推）。
   可选小调：`produce_slice_package.py` 人工标题时不跑 LLM 艺术指导（走确定性基线，仍贴角色）；要人工标题也精修就把 `art_direction_llm` 提出 `if not given_title`（一行）。
5. **commit 规矩（Ivan 2026-07-05）：授权上传的内容必须 commit**——已执行，commit `7dcfdfa`（84 files：全部管线代码/skill/资产 + 9 份 `*.uploaded.json` 上传证据含追溯补记的充电器 + 24 张换封面状态证据；`.gitignore` 已加 `!reports/**/*.uploaded.json` 例外；媒体不入库，hash 在证据里）。以后每次授权上传后：写 uploaded.json → commit。见记忆 `authorized-upload-must-commit`。
