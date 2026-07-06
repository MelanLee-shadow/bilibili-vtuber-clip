# vtuber-slice 交接（HANDOFF）

> 约定：每次实质进展或会话收尾更新本文件（五段：目标/已完成/进行中/阻塞/下一步）。
> 开工先读本文件 + AGENTS.md，别凭旧对话推断。

## 2026-07-05：李豆沙 7/5 直播 → 5 条上传-ready 候选切片（本地 no-upload）

**目标**：Ivan 要 7/5 直播上传-ready 候选切片到本地审；主控读全部候选池挑题，subagent 做出片体力活。

**已完成**：
- **5 条成品在 `lidousha/2026-07-05/`**（+ `OPEN_ME.md` + `index.html` 可视化过片）：A 数学讲成兄弟情虐恋(4:20，全场语义分最高)、B 以为小猪结果熊猫(0:31)、C 有没有李豆沙/缩成小点(0:54)、D 联动游戏名/聋子(1:07)、E 一本正经讲丧尸偶像/血鬼舞台(0:54)。每条 video+准字幕(cos/sin/OBS/VIVINOS等专名全准)+围绕李豆沙自动标题+CPA真图 gpt-image-2 封面。**5/5 verify PASS**（无词表泄漏、字幕行合规、含音视频轨、topic-closure 落完整句）。**未上传**。
- 选题：主控读 6 段生产候选池(98候选/57 talk)+jingting字幕+弹幕，观众视角+围绕李豆沙+题材多样挑 5；出片 subagent 各跑一条 `produce_slice_package.py --substrate aggregate_asr --correct cpa`（Ivan 2026-07-04 定的字幕架构）。
- **两个坑修好并入记忆**：(1) spec `remote_media` 必须 free **宿主** 123云盘 路径(`/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/...`)，**不是容器 `/app/Videos`**（脚本宿主侧 ffmpeg 无 docker exec）；FUSE 挂载~36s/小文件极慢；semantic_start/end 对准真实语音起止读 `padded.fresh.srt`，候选 start 含前置铺垫会 fail-close。记忆 `produce-slice-package-host-path-and-slow-mount`。(2) 封面字体 `ZCOOLKuaiLe` 把「自」渲成「白」形（自私→白私），A/D 封面钩子临时改写绕开(自私→还小气、自认→成了)，B站标题保留原字。记忆 `cover-font-zi-renders-as-bai`。
- B/E 初版边界飘（B 拖进无关banter、E overshoot climax），已收紧 semantic_end 重跑（缓存切复用，快）。

**进行中**：无（本节收尾已由 Fable 会话完成 2026-07-06 05:3xZ，Opus 会话勿重复执行）：
- **A(cos/sin) 已发布** BV1QzTS65Enf：单次 biliup rc=0、入小李切片(code 0)、封面换「自私」修正版（**ZCOOL 版实为白私字形**——放大核验过；修正版=得意黑整张，`_cover_evidence/*.zisi-smiley.png`；换封面触发复审后已回 state=0，公开 pic=b73d7446 新版 ✓）。证据 cossin.uploaded.json + cossin.public_verify.json，已 commit。
- **D 重做 ✓** 收束「这就是本周直播直播安排」（试了 3 个目标位：92k→"然后就这样"、95k→蹿到下话题"露总"且自动标题带双自字，最终 92k+钉标题「还没联动，小李先把盲聋哑念乱了」命中理想句）。
- **H 重做 ✓** 收束「也没有阻止这个展开啊」（完整句收题，避开谢SC杂段；比塞到 02:08 更干净）。
- **F2 真由来 ✓** 新增 `小丑鼻子的真正由来_猪鼻不见了.mp4`：19:30 段 06:35–09:13 = 猪猪侠盲盒→VTS 猪鼻不见了→借陆医生小丑鼻代替→"先用这个代替一下吧"收。原 F(生日/活动小丑)保留为续集。全段 BCUT 存 `pull/seg1930.full.bcut.srt`。
- **标题新规**：「直接」全局禁用（Ivan：直呼打咩>>直接打咩）——代码硬门+词库+skill+记忆全链固化，287 tests。
- B/C/E/G 未动。OPEN_ME.md 已补 2026-07-06 节。

**阻塞**：无。上传永远需 Ivan 逐条授权。

**下一步**：
1. **Ivan 审 `lidousha/2026-07-05/`**（浏览器开 `index.html` 过片）。
2. **封面字体 `自→白` 治本**（通病应入代码，勿单例补丁）：给 `scripts/run_auto_review_shadow_pipeline.py` 的 `_overlay_lidousha_cover_title` 逐字渲染加「自」→回退字体映射（现有"缺字回退"对字形错的「自」不触发），或整体换 `自` 正确的快乐体系字体（仍 fail-closed）。
3. 可选第二批：日语歌《男も女も恋してるべき》(20:30 段，走完整曲 LRC 全局位移 lane) + 备选 talk（米老鼠版权/被乌鸦俯冲/百合是工作/回不了家/木马/人设太帅，见 OPEN_ME）。
4. 未提交改动：本轮 `reports/lidousha-autoslice-20260705/`(specs+verify脚本)、`lidousha/2026-07-05/`(成品+OPEN_ME+index.html) + 之前所有未提交改动（Ivan 未让 commit）。

## 2026-07-06 第二轮收尾（Fable 接管续）

- **已发布 +2**（均单次 biliup+入小李切片+公开验证+证据 commit）：A cos/sin **BV1QzTS65Enf**（自私修正版封面）；D 联动 **BV1uCTS6kEuf**（Ivan 定稿标题「过期（？）女大终于迎来再次联动」，礼墨/安晚awa 词表闭环重出）。
- **staged 不上传**：百合长片 `yuri_full_arc_2000`（10:56 三段拼接，标题带《我在意的人不是男生》，百乃工/百破图已修，封面大字钩子）；F2 小丑由来（「猪鼻不见了，小李只好借小丑鼻顶上」）。旧 G/H/生日F/Opus重复由来片 → `_superseded/`。
- **规则升级**：标题禁词扩到 直接/当场/秒X（代码硬门+词库按 Ivan 真实标题重建+示例清洗）；作品讨论类标题必须带《作品名》；对话提炼的工作流偏好固化进 publish SKILL「Ivan 工作流偏好」节 + 记忆 ivan-implicit-workflow-preferences。
- **修的 bug**：llm_client 命令超时未包成 LlmCallError 会炸 produce（11 分钟长片踩到）→ 已修+测试；校正 CPA 超时 180→600s。288 tests。
- **待 Ivan**：①「牡丹原作者/百牡丹」正确写法（未确证未入词表）；②已上线 A 标题含「当场共鸣」（禁词规则前上传的），要不要热改。

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
- ~~7/5 直播切片轮·被会话切换中断~~ **已被 Opus 会话续跑完成**（见顶部「2026-07-05」节：5/5 交付 verify PASS）。遗留提醒仍有效：该批字幕用的 `--correct cpa` 非默认三段式 `bcut_agy_cpa`（Ivan 定的架构是三段式；上传前如对专名有疑虑可按默认重出）；封面「自→白」字形坑待治本（见该节下一步2）。
- **[无人值守自动切片 runner·已部署 2026-07-06 (Fable)]** Ivan 目标落地：**直播结束后 free 自动跑完整产出链，无需人开口**（上传仍关）。`scripts/free_session_autoslice.py` 部署在 `free:/opt/bilive/autoslice/`（repo 副本+cpa.env(600)+state/cache/logs/reports），**cron 每 10 分钟** flock 单飞 tick：blrec API(带RECORD_KEY) 判在播→在播/API失联一律跳过（fail-safe）；下播→逐新段 BCUT 转写→CPA 语义召回(带弹幕突发hints，LLM挂了退确定性兜底不静默)→**谈话 top5**（跨段轮转）逐条 `produce_slice_package --ssh-host localhost`（默认三段式 bcut_agy_cpa+真图封面）→**歌切每场至多2个、按窗口弹幕量最高排序**（Ivan 2026-07-05），走 `run_full_session_selector_cpa_shadow` LRC lane（anchor±180/150s 窗，完整性门 fail-closed，只交付 AUTO_UPLOAD 判定的）→交付 `repo/lidousha/<date>/`+`AUTOSLICE_SUMMARY.md`+报告文件。**杀开关** `touch /opt/bilive/autoslice/DISABLED`；7/1–7/5 已预认领（manual）不会被重跑；失败不自动重试（防 retry storm）。Mac 端 launchd `com.ivan.lidousha-autoslice-pull` 每 30 分钟 rsync 交付+报告回本地 `lidousha/`+`reports/slice_monitor/autoslice_free/`。冒烟：talk 全链 rc=0（cut→BCUT→AGY(ssh localhost)→CPA→烧录→真图封面→交付）✅、语义召回 lane ✅（选出的候选与 Opus 人工选题重合）、**歌切全链 ✅**（虫儿飞靶：窗口判 song、netease LRC 匹配 92.3%、门判 AUTO_UPLOAD、标题"温柔《虫儿飞》哄你睡觉"、成品+封面自动交付；Mac 睡眠期间 free cron 独立跳 17 tick=自主性证明）。歌切两坑已修：①窗口余量必须小（anchor−15s/+20s，宽了窗内召回会改判 talk）；②**QA 判官曾以"翻唱版权"BLOCK 合格歌切（0.74 分）——已校准口径**：翻唱是频道常规内容（24 条上传里 7 条豆沙歌），unsafe 维度只管隐私/引战/平台违规/第三方画面（`cpa_semantic_qa_llm.py` + 测试）。**选片 metric 已资产化**：`assets/lidousha/slice_selection_metric.md`（Opus 会话 v2 校准 + 24 条已上传标题提取的偏好 + 熊猫伪装/奶龙拒发反例）注入语义召回 prompt（回退链），sync 推 free，286 tests。为此 free 配置了 root 自我 ssh(ed25519)。记忆 `free-unattended-autoslice-runner`。
- 无本会话后台进程（free 上只有常驻 cron runner + 既有 jingting daemon）。

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
