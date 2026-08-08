# Current handoff

Updated: 2026-08-07 by Claude（8/7 鹅鸭杀场三症状通病修复会话）。7/31 以下旧节仅存历史。

## ⭐ 2026-08-08 晚 当前状态与接力任务（后来 agent 从这里开始）

**此刻在发生什么**：Ivan 正在本地 `lidousha/2026-08-07/*.speaker.srt` 里按 A/B 语法标注
说话人与文字错误（A=李豆沙、B=非李豆沙、句内多标记管到前一标记为止、不标=机器对，
同时直接改错字）。**在他说标完之前绝不碰这些文件。** 8/8 批 runner 自动处理中（只产不传）。

**代码/部署位**：分支 `claude/session-live-context`（生产唯一合法部署源；从 main 部署会
回滚 codex 17 提交）。部署波 6 = e658e79（优化②judge瞬断三件套）+ 2b65885（song
provider 门根修 + 游戏场配额 20 条/≥85 分 + 候选池 12→18 + TALK_ATTEMPT_CAP 10→20）
+ 本 HANDOFF，全套 3122 绿。部署用 `bash scripts/deploy_free_autoslice.sh free`（自动
排 tick 间隙，不断 8/8 流水线——Ivan 规则：8/8 健康就不打扰，出问题才先部署修复再自愈）。

**Ivan 审完标注后的任务队列（他定的执行序，逐条做）**：
1. **收割标注（先存证再动手）**：⚠️ 第一步先把每条候选的 pristine 机器产物从 free 归档
   （`out/<date>/<cid>/replacement_recuts/*.speaker-final.srt` + `padded_*.asr_draft.srt` +
   `padded_*.agy_refined.srt` → 存到本机法证目录）——重产会覆盖它们，不先存证法证就断根。
   然后对 Ivan 标注稿（本地 `lidousha/2026-08-07/*.speaker.srt`）与 pristine 版逐 cue diff。
   标记语法（Ivan 定义）：孤立的 ` A`=该段李豆沙、` B`=非李豆沙，句内多标记时每个标记
   管到上一标记为止，未标=机器标签对；标记外的文字差异=他改的错字。陷阱：只有被空格
   包围的孤立 A/B 是标记（`BW`/`OK` 里的字母不是）；行尾空格要剥。产出每候选真值工件
   （schema `ivan-speaker-truth-diff.v1`：per cue machine_label/machine_text/
   truth_segments[{text,label}]/text_changed/label_changed/mixed），并对 3 个含混说的 cue
   人工核对解析结果。此工件是后续一切法证与验收的唯一输入。
2. **根因法证（Ivan 核心问题：为什么错、以及为什么本来修得对却没修对——通病优先于修切片）**：
   对**每一处**文本错走五连问阶梯，每问都以 free 上的回执为证（引用原文，不许推测）：
   ① **候选在场吗**——重建该次 produce 的 prompt 视野：正确写法当时在不在
   glossary/roster/游戏语境/主题提示/timely 块里？（查 assets 当时版本 + free state 快照 +
   record.json 里的 env/sha 绑定；roster 是 prompt-only 候选、glossary 误听面才有机械车道——
   区分"没登记"和"登记了但只到 prompt 层"。）
   ② **草稿听对过吗**——BCUT draft 或 AGY refined 里出现过正确文本吗？出现过而终稿错
   =后段改坏（过度修正类，先例 cue11「我是小三」被修正链吞掉）——翻 fidelity-audit +
   chat-authority.json 的 final_review_audit applied repairs + exact_final_cpa_self_heal
   passes 找到**具体哪一遍**改坏的。
   ③ **召回触发了吗**——候选在场但从未被提案：召回缺口（先例 cue28 天云海→萱萱卡娅，
   review-flags 里该 cue 零 findings）。判定哪条召回车道该触发没触发（expected-value 对？
   confusable 组？语境关联召回？还是 roster prompt-only 的结构性局限——这类要提机制补案）。
   ④ **声学证人说了什么**——entity_verdicts/<hash>/ 的候选盲拼音 vs 真值：证人对而终稿错
   =裁决层病（哪个门/逃生口压过了它，先例 cue59 的
   CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT，注意守卫已收窄勿重复修）；
   证人也错=ASR 限度类（只落方向词条+错误历史台账，按保向铁律绝不全局替换）。
   ⑤ **权威冲突/回执断链/供应商断供**——两个权威振荡（先例 狍哥vs小炮哥）；回执从未落盘
   （先例 封面文案审 7/9 轮无响应存档）；judge 断供（e658e79 后 error_cascade 会留全链）。
   ⑥ **【Ivan 点名必查】是不是"刻意改错"——历史决策审计**：草稿/证人本来是对的，而某条
   **有意建的规则**主动把它改错了（≠bug，是规则按设计运行但在本案打错了）。诊断法：从
   applied repair 的 authority/receipt 反查到具体规则条目，再查该条目的**出处注记**
   （本仓纪律：glossary 词条/hard-meme-canon/expected-value 对/守卫/账本行全部带
   Ivan 裁定日期+原话或 commit）——找到是哪个历史决策、当时为什么这么定。然后二分：
   (е1) 决策本身仍对、只是机械套用超出了其记录的适用范围 → 工程修（收窄适用条件），
   不需要新裁定；(е2) 本案构成对决策本身的反例（当时的前提变了/范围划宽了）→
   **必须请 Ivan 重新裁定**，禁止 agent 自行推翻或扩大任何带 Ivan 出处的规则。
   本会话四个先例全是这个形状：cue59 词表压耳朵（7月有意机制→8/8 Ivan 重裁收窄成
   三逃生口）；uniform_host 统一涂色（7/13 裁定→8/7 Ivan 重裁撤销）；120px 字号硬下限
   （7/31 裁定→8/8 Ivan 重裁开窄例外）；狍哥方向（8/7 裁定→逐处音频仲裁边界待重申）。
   **(е2) 类的产出格式=决策卡片**，全部汇总进一份 docs/reviews/ 复裁清单让 Ivan 一轮批完：
   每张卡=原裁定（日期/原话/出处）→ 本案反例（回执证据引用）→ 冲突本质一句话 →
   建议选项（维持原判/收窄范围/撤销）。
   标签错另走：对照 speaker-final.json 的 analysis.decisions 逐 cue 证据源
   （campp/语义佐证/默认连线/短句门），假李豆沙必须归零，假连线按证据源分布定阈值/门修。
   **产出物**：`docs/reviews/` 法证报告（先例 2026-08-07-zsm-mishear-forensics.md 的
   per-cue 表格式），每处错必须落到 (a)已登记-时序问题 (b)登记但仲裁缺陷→**点名文件:行号**
   (c)未登记→当场补方向条目 (d)纯听错→台账 (e)历史决策所致→按⑥出决策卡片
   （е1 工程收窄 / е2 提请 Ivan 复裁）五类之一；凡判 (b) 的**必修**且配负向金丝雀
   （revert 修复测试必须变红——本会话七个先例：cue59 守卫/cue28 召回/cue11 过度修正/
   陈旧 spec 绕基线/owned_intervals 无人读/清单认错脸/song 瞬态码不识——全是这把梯子爬出来的）。
3. 通病修复 + 测试全绿 + 部署。
4. **统一重产**：所有带错候选按 reviewed-baseline+override 车道重产（陈旧 spec 教训：
   baseline 只在 runner 建 spec 时注入，手动重产必须用 free:/opt/bilive/autoslice/tmp/
   inject_baseline_and_produce.py 的产线函数注入法）；**贪生怕死 auto_223750_913_1322 要出
   说话人二分版**（现为统一色时代产物）。验收=剥标记逐字相等+假李豆沙=0。
5. **封面（Ivan 已拍板，2026-08-08 原话）**：「既然「我就是女同」只有带着「如果是我杀的」
   前提才成立为玩笑，那就加上呗……宽度限制可以换行啊，实在不行可以调小一点点字号。」
   执行两层：
   (i) **本候选 IVAN_EXPLICIT 文案授权**：auto_223750_913_1322 封面文案=完整梗
   「如果是我杀的我就是女同」，双行排版优先，实在放不下才允许字号在 120px 下限基础上
   **微调小一点点**（此为 Ivan 对 7/31「120px 硬下限」裁定在完整梗场景的窄例外，
   仅限降不下时的最后手段）。按 70-cover.md 的 IVAN_EXPLICIT full-text-cover contract
   流程落地（重出 manifest+CPA 联合质检那套，见旧节 cover-only lane 注意事项），
   出图仍过身份门（三分身缺陷警惕，法证报告里有那轮实图证据）。
   (ii) **通病修复**：梗字链的处理顺序病——超宽时先砍前提导致"两段拼不成一件事"反模式
   被误触发。修法：前提+梗点构成同一事件时允许**换行保全整梗**（宽度超限→先尝试双行，
   再尝试窄幅字号微调，最后才降候选），文案审的反模式判定要能识别"前提+punch"结构
   不当作两个独立片段。配法证报告里的三条最小修复（身份门字段一致性校验/逐轮持久化
   punch 审回执/耗尽理由字段）一起做，全部带测试。
6. **歌**：《一起长大》song_230754_1118 等 8/7 歌候选已被旧 bug 终态化；部署波 6 后用
   scripts/revive_rejected_candidates.py（fix-commit 2b65885）复活重产，付费链现可达。
7. **上传**：权宜授权已撤回——全部修好 + Ivan 明示后才走 authorized_upload
   （90-publish.md 契约；清单构建器已修好认说话人产物名，但批状态 processing 时会拒出清单，
   等批收线）。
8. **积压工程**（Ivan 已授权，按序）：优化①边界后移进程内重放（task#10）；
   优化③ERes2NetV2 嵌入试点**已在跑**（task#12，worker 于本机 Mac 离线回放（Ivan 指示：别抢 free 产线 CPU；音频一次性 scp）
   61+40 句双真值对比 CAM++，评判=假李豆沙=0 前提下假连线更少+短句区分度，产
   docs/reviews/2026-08-08-eres2netv2-pilot.md；赢则按 host_vocal_proof.py:638 sha 门
   +voiceprint_profile 重绑走切换流程）；SOTA 第二优先=pyannote segmentation-3.0
   句内子窗（治混说/重叠），排在 Ivan 标注收割的根因法证之后——他标的句内混说错误
   正是其判据（调研 docs/reviews/2026-08-08-diarization-sota-survey.md）；误听自动积累写路径
   （勘探完毕 docs/reviews/2026-08-08-mishear-mining-probe.md：382实例/246方向/99%新面，
   三条腿=自采矿+审阅收割+历史回填，晋升阈值≥3日期零反向待 Ivan 拍板）；
   final_review_auditor 强制拆解（账本第三抬触发）；producer_package_finalization 拆解（第二抬）。

**悬案/边界**：7/22 auto_200511_61_138 Ivan 裁定封存（真值只作数据，不编辑不上传）；
狍哥案 auto_220747_1271_1323 死于「狍哥 vs 小炮哥」实体振荡，需音频重裁（Ivan 输入）；
说话人等音质场准确率待 Ivan 的 7/22 裁定收割（工作表已标完 30s，全长版已给）。

**⭐ 本机优先原则（Ivan 2026-08-08 关停前指令）**：凡不需要 free 的工作一律在本机 Mac 执行——
标注收割/diff 解析、法证文档、测试套件、ERes2NetV2 等离线评测（本地 venv+一次性 scp 数据）、
矿藏后处理。free 只用于：产线函数注入重产、回执/状态拉取、部署、revive、上传。

**会话关停时的在飞状态（successor 需接手重启）**：
- ERes2NetV2 试点 worker 随会话死亡——已落 PARTIAL 报告 docs/reviews/2026-08-08-eres2netv2-pilot.md——**按其 §6 的精确续跑命令重启**（本机 venv/媒体/脚本全就位，含 Python3.11 陷阱与 5.77s 片头偏移教训；两模型分数不同尺度，阈值不可移植需重推导）。
- 封面 8 连败法证 worker 死于中途——其报告草稿已由 integrator 代提交
  （docs/reviews/2026-08-08-cover-punch-exhaustion-forensics.md，**未经原 worker 终检，
  successor 用前先核对逐轮回执引用是否完整**）。
- 部署波 6（e658e79 judge瞬断 + 2b658859 song门/配额20/85 + HANDOFF 链）：本地测试门
  3122 已过，关停时处于同步段——successor 第一件事：核对
  free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT 是否已到本分支 tip；没到就
  `bash scripts/deploy_free_autoslice.sh free` 重跑（幂等）。
- 8/8 批 runner 产线自动运行，与会话无关；Ivan 的 srt 标注仍在进行中。

**运营铁律（本会话学费）**：审阅交付=本地最小四件套（mp4/speaker.srt/cover.png/publish.json）；
worker 提示必须 edits-first+精确锚点（80 调用帽会被纯探索吃光）；同一 worktree 单 writer；
账本增行必须带 Ivan 出处；free 一切产线操作走 flock 礼让。

## 2026-08-07 live 状态

- **第二次部署 `26dfd83`（21:37Z）**：狍哥案 selection-rescore 车道全闭环（执行器挂
  delivery_recovery.requeue 尾部；rescore_pending 不进 produce）、动态主题提示通道
  （crawler+cron 06:47+prompt 块）、gitignore 裸 lidousha/ 规则锚定修复。全套 3084 绿。
  `auto_220747_1271_1323` 已经 revive 脚本复活（fix-commit 26dfd83），下个 tick 走新车道。
- 真善美 `auto_203735_555_680` 已用新流水线重产（PRODUCE_EXIT 0）：马有利/香香烧烤/萱萱卡娅
  落地、speaker 双样式烧录（29 主播/32 连线）。**待 Ivan 复核**：「有用→殉情」语义翻转、
  标题「李姐」称呼、疑标 cue「李姐很了解女人的」。
- 已知残留：rescore 车道 title 修正只入回执不进 given_title（worker 披露 #2）；provider
  失败重试无 backoff（每 tick 一次，CPA 长宕机时调用数无帽）；rebuilder 闭包卡参数根修
  （设计稿 §8）未做。

- **生产基线已在分支**：`codex/virtuareal-community-crawler`（beda5bf，8/7 18:05Z 部署，
  社区称呼 crawler 子系统）**未合回 main**。本会话工作分支
  `claude/session-live-context` 基于 beda5bf 之上（马有利/狍哥 roster+glossary、
  会话游戏语境通道、评分卡 rescore 设计稿），部署以该分支 tip 为准。
  **main 的下一次上游化必须合并这两串提交，不得从 main 直接部署（会回滚 codex 17 提交）。**
- **说话人二分已验证并已翻转**：CAM++ 二分 finalizer 对 8/7 `auto_203735_555_680` 冒烟
  READY（61 cue=25 主播+36 连线，LDS/GUEST 双样式）。8/7 部署（`78f64aa`，deploy 门内
  3036 全绿）后 runner cron 行已加 `env AUTOSLICE_SPEAKER_MODE=auto`（备份
  `/opt/bilive/autoslice/crontab.backup-20260807-speakermode`；翻转撤销 7/13 uniform 政策，
  不确定场 speaker_review_required 持留，符合 40-doc 口径；回滚=恢复备份 crontab）。
  roster 快照已手动刷新（马有利/狍哥/香香烧烤 已达生产 prompt）；8/7 游戏语境
  state=RESOLVED 鹅鸭杀（17 特征词面×90 次）。
- roster crawler 是每周日 06:12 cron；member_overrides 别名变更后需手动跑一次
  `scripts/crawl_psplive_roster.py --write /opt/bilive/autoslice/state/psplive_roster.json` 才达生产 prompt。
- 评分卡 rescore 状态机**设计稿**（未实施）：
  `docs/reviews/2026-08-07-source-fact-rescore-design.md`；`auto_220747_1271_1323`
  在实施前仍处 candidate_rejected 终态（revive 脚本救不了，见设计稿 §1）。
- 8/7 场联动台账行未写（参与者尾幼/星汐/萱萱卡娅/北柚香/汀汀汀有 roster+弹幕证据，
  紫妍/尤娜/天云海等非 PSP 成员写法未裁定）——等 Ivan 裁定后按 7/22 南町行格式补
  `session_relation_ledger.v1.json`。

本文件只记录会影响下一次操作的 live 状态。流水线规则只读
[`docs/pipeline/`](pipeline/README.md)。`review_ready`、本地 commit、旧 PID 或旧 handoff
都不是发布完成证明；已发布稿必须读取 Creator/public/section fresh-live 回执或 cover-only
receipt。

## 目标

保持 `free:/opt/bilive/autoslice` 正式 cron 不停，继续收敛 2026-07-25、07-26、07-29
及后续日期。用户已授权：修复稿可直接同 BV 编辑上传；当天新稿可权宜上传；指定旧稿重做后
可直接上传，无需再次等待审阅。

## Runtime authority

- 远端部署：`free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` =
  `a4e68048c548828707031b2b78671771ed373c82`（2026-07-31T18:03:48Z）。
  `DISABLED` 不存在；正式 runner cron 为每 10 分钟一次。
- **本地 `main` 领先部署一个 commit：`f616bbf`（deploy 全量测试门 + 架构债务账本），
  尚未部署。** 部署它之后，`scripts/deploy_free_autoslice.sh` 会在 dirty-tree 拒绝之后、
  任何远端动作之前跑 `pytest -q` 全量，红了拒绝部署；**无 marker 排除、无 bypass 开关**。
  当前全量为 `2910 passed / 0 failed / 0 skipped`（`f616bbf`，干净树复验）。
- 架构债务账本冻结于 2026-07-31：20 个超预算函数 / 12 个超预算模块
  （`tests/test_runtime_architecture.py`）。成员只减不增、逐项行数只降不升；
  7/31 之后新增任何一行必须带 Ivan 的批准出处。部署时会打印当前数字。
  最重的一项 `final_review_auditor.py:2030 adjudicate_context_finding` 764 行，
  留作单独 bounded 拆解 task。
- 2026-07-31T17:20Z 起 07-25 / 07-26 / 07-29 三天均已 `published` / 
  `published_with_failures`，pending talk/song 全空；`live=False` 无新直播。
  因此 `baaf250` 之后**没有任何新的 route decision 数据**，其对封面路线分布的影响未实测。

## 已完成

- 07-22 与用户点名的 07-24 修复均已上线：`BV1xgg462Env`、`BV1AD366DEd9`、
  `BV1Eo3L6zECt`、`BV1ec3A6bEWF`。07-24 的“礼墨”、0:13 怪叫、0:59
  “他一副”、1:23 “kmx 欺负人”均已按对应修复范围处理；专名与语境通病已进入
  glossary/CPA 最终裁决链。
- `auto_142942_496_618` 已用最新版流水线整片重跑并在原
  `BV1zk386LEjC` 完成同 BV 修复：CID `40453148097 -> 40455570336`，未新建 BV。
  最终字幕将已确认的日语人称统一为假名（`ぼく/おれ/あたし/おら/わたくし`），不再混用
  `boku/ore/atashi`；39/39 reviewed baseline mappings、native-script gate、
  source-truth owner gate、redelivery baseline owner gate 和 exact-final v2 均 PASS。
  最终视频/SRT/封面 SHA-256 分别为
  `70818f97e595a4a29d50d0ab7aa40877e04e3286970c36d448e2150d964dbd37`、
  `34dbb888e1bff78d2832c57360418ec9c890575aaf0d78c93ac0d74882a8d4cc`、
  `2f34fbc1bc28e2503e10f48b488c04ad9afac670b2a4a1ec29277e69d862d9ff`。
  CPA 主图身份复核确认李豆沙为主体，标题字面为“一声‘偶’让小李 / 日语人称翻译大会”。
  Creator/public/section 已回读新 CID；完成侧车为 `VERIFIED_FRESH_LIVE`。
  固化证据在
  `reports/authorized_uploads/2026-07-30-japanese-pronoun-r7/`（commit `f38bbad`）。
- 上述重跑暴露并修复了 baseline owner verifier 的真实漏洞：最终稿经过已审阅的
  日语 native-script canon 后，verifier 仍拿旧罗马音做字面比较。`bff2707` 让 expected
  baseline 经过同一 canon，并在 audit 中记录转换来源；无关文字漂移仍 fail-closed。
  `producer_text_finalization`、package finalization 与 integrity 共 88 个相关测试 PASS，
  修复已部署。
- `auto_192000_909_1014` 的“白色奶龙”封面已按用户提供的
  `/Users/ivan/Downloads/60bae315bc53a44bede0582389460d20475948079.png` 进行 cover-only
  修复：删除无关龙/3D 龙模型，在原位置使用白发、熊猫耳、头顶墨镜的李豆沙“白色奶龙”
  形象。原 `BV1s7326qEc9` 和 CID `40438664771` 均未改变；新公开 CDN 资产为
  `9994c2c75f31d0b1706be4e7709efe226ebcbca0.png`，本地目标与公开回下载 SHA-256 均为
  `8bd5fa4f47468f514dc32bc265896405a46289a31ea930ac748234a18c989c06`。
  CPA 主人公身份与白色奶龙专项视觉 QA 均 PASS，receipt 为 `VERIFIED_EDITED`；
  固化证据在
  `reports/cover_only_repairs/2026-07-30-auto_192000_909_1014-white-milk-dragon/`
  （commit `f2ea7eb`）。
- 其他已完成的同 BV/封面修复仍保持公开：粉色小姐姐 `BV1RPNR6dET9`、同事家开播
  `BV1Eo3L6zECt`、沙豆李投票 `BV1zzgd6JEHe`。下一次操作不应重复上传或新建替代 BV。
- Gemini 路由审计确认代码顺序正确：AGY 仍是音频 witness 首选，三把 free Gemini key
  最近生成 canary 仍为 429；paid backup 的最新最小生成 canary 已恢复 HTTP 200，新的音频
  witness 任务会在 typed AGY/free-key failure 后使用 paid fallback。CPA 是最终文字/语义
  裁判，并是图像 witness 首选；CPA 不接音频不等于 CPA 不能看图。

## 当前正式队列

- 07-25：`review_ready`，pending talk/song 均为 0；6 个 talk 已就绪，1 个 song 进入状态。
  `song_192000_1321` 当前 deterministic packaging 拒绝原因为 active publish draft
  缺失且不属于 verified deferred-cover case，需要流水线补齐合法 draft authority，不能
  绕过门直接发布。
- 07-26：2026-07-31T00:10Z 为 `processing`，pending song = 1；7 个 talk 已就绪。
  多个 song tight-window 能识别歌曲，但扩大到 full-source 后仍缺 positive LRC boundary
  proof；runner 正在按 typed recoverable 规则重新入队，不等待用户。
- 07-29：`review_ready_with_failures`，pending talk/song 均为 0；5 个 talk pick。
  `auto_225056_814_887` 的 `subtitle_authority / chat_authority_finalization` 仍需流水线
  自行重建 authority。

## 阻塞

- **（已解，留防复发警示）2026-08-02 runner 三日期粘滞封锁事故**：verify-live
  把 1013 修复凭证的 runtime 登记路径写在了**可回收沙箱**里
  （recovery/2026-07-29/auto_225056_1013_1116-jyl-r2/repo/.../verification/
  same-bv-repair-completed.json），后续金丝雀 rm -rf 沙箱→登记悬空→全册
  校验失败→07-25/26/29 全 blocked。已按 sha 原字节恢复（1e684dec…），
  runner 自愈解封。**该沙箱路径在 runtime 登记被重写前不得删除**；耐久
  副本在 /opt/bilive/autoslice/reports/authorized_uploads/
  2026-07-31-1013-jiuyuanling/。代码级修复（repair-verify-live 把凭证落
  耐久目录再入登记）在 backlog。

没有需要 Ivan 补充的外部 blocker。

- **cover-only 新 lane 零生产执行（2026-07-31，最高优先）**：`28b3576` 新建
  `src/autoslice/same_bv_cover_repair.py`（1063 行状态机）并同时把旧路
  `scripts/bili_cover_edit.py` 改成无条件拒绝。全仓库没有
  `same_bv_cover_repair_ledger.jsonl`、没有 plan、没有 receipt——**该 lane 从未真实执行过**。
  网络层复用已验证的 `BilibiliRepairAdapter`，未验证的是 plan/journal/transition 状态机。
  在它完成一次真实执行验收前，如遇封面事故：升级 Ivan 裁决，**不许临场解除
  `bili_cover_edit.py` 的 fail-close**。
  **`cover-repair-plan --dry-run` 不是零成本冒烟**（2026-07-31 实测执行序）：先拿
  `upload.lock`，再硬要求 manifest 带 title-cover joint-QC 回执（`required=True`），
  然后才 `adapter.observe` 四面观察、才判 dry-run。而 **07-31 之前的所有历史
  manifest 都没有 `title_cover_qc` 字段**（该门 `061f8ed` 07-31 06:54 才落地），
  拿老 manifest 跑必然 `return 2`——那是 fail-closed 的正常行为，不是 bug。
  所以今后任何已发布稿的封面修复都必须**重出 manifest**：先跑 CPA 联合质检拿
  receipt（receipt 绑定的是**新封面字节**的 sha，所以新封面得先产出来），再
  `make-manifest --title-cover-qc` 冻结。首个真实执行待 Ivan 授权白色奶龙重做。
  未验证面已收窄（07-31 核实）：`observe` / `normalise_snapshot` / `prepare_cover`
  与生产已多次真实执行的 video same-BV lane 共用同一个 `BilibiliRepairAdapter`
  （`same_bv_repair.py:333`）；真正没跑过的只有 `edit_cover_only`（`28b3576` 新加）
  的组装与 cover 专属 plan/journal/transition 状态机。
- **封面路由：标定分数路由已被整体退役（2026-07-31 调查确认）**。
  `publish_staging.py:1961-1970` 在 composition witness 存在且未建议重绘时**无条件返回截图**，
  `:1939` 建议重绘时直接重绘；witness 生成条件 `:1573` `enforce_final_host_identity` 在正常
  talk 恒真。因此 `:1971-2035` 的全套阈值（4.5 / 2.6 / 0.50 弥散帽 / camera window）
  **在有 witness 时不可达**。7/24-7/29 实测（witness 上线前）：24 条里 cpa_redraw 14
  （58%，标定基线为 29%），11 条走同一条 `motion without confident cover subject`；
  拆分为几何 flag 自身 False 5 条、弥散超帽 5 条、flag True 但超帽 0.027 被否 1 条
  （`auto_202004_553_831`，score 9.0027 / disp 0.5271）。`subject_confident` 探测器在
  23 条有效样本里 70% 给 False。修法未落地。
- ~~**封面文案链有一道被绕过的强制门**~~ **已修复（`f1c018e` + `c80bee1`）**：
  分行权威等级已立法并机器化（切点只属作者显式 `\n` / CPA punch 段 / full-text
  contract / 已验证 word_atoms；平衡器绝不发明切点）、`max_lines` 按缩略图合同封顶、
  renderer 背带、lane 内容触发门、contract 四路径穿透、CLI typed rc≠0，以及
  `70-cover.md` 内部那条「:44-45 禁回退 vs :60 要回退」的政策缝。整数行数门
  `physical_text_line_count ∈ {1,2}` 的实质已被「渲染行 == CPA final_punch +
  每行 ≤9em」取代（原始记录保留在下方）。**存量 6 条违例一条未修**，见
  `docs/reviews/cover-text-violations-triage-20260731.md`，等 Ivan 逐条点名。
  原始诊断留档：`auto_192000_909_1014` 的 7/30 cover-only 修复
  `cover_punch: []`，且证据目录内**没有任何 `lidousha-cover-punch-semantic-review.v1` 回执**
  ——选择器根本没被调用，然后 fail-open 回退整段 `cover_text` 并被 renderer 静默换行成
  3 行（`"白色奶龙"` / `表情小李` / `拒绝花钱`），把「观众想让新3D永久保留」整段丢失。
  `docs/pipeline/70-cover.md:44-45` 明令禁止"回执为空或回执失败即放行长 cover_text"。
  该稿仍公开（`BV1s7326qEc9`，CID 未变）。修复以流水线为单位，不做单切片手写文案。
- `physical_text_line_count ∈ {1,2}`（`scripts/authorized_upload.py:889`、
  `docs/pipeline/90-publish.md:103`、`70-cover.md:55`）由 `9563266`/`061f8ed` 于 2026-07-31
  引入，**无 Ivan 裁定**，且被证明是错度量（同一张图 Ivan 按阅读单元数 2、render spec 数 3；
  好断法与烂断法在该门下同样通过）。待退役为"渲染行与 CPA `final_punch` 逐行相等"。

- 歌切 active draft 缺失、positive LRC boundary proof 缺失、provider transient、
  subtitle/chat authority 失败都属于流水线/操作层 blocker；应保持 typed retry 或修复
  authority，不得停下来等待用户，也不得为了“变绿”绕过发布门。
- `auto_152944_1411_1463` 的 cover repair 当前在 image request 前 fail-closed，以保留
  screenshot route；下一次应修复 route authority，而不是用未验证 AI 像素覆盖截图路线。
- `auto_142942_496_618` 的 r6 因 recovery 目录漏建 `logs/` 在 ASR 前失败，且继承旧 lifetime
  retry cap；r6 仅保留为操作失败证据。不要原地洗绿或重用其 fingerprint；成功 authority 是
  fresh r7。

## 进行中（2026-07-31 深夜 → 08-01 已收官，Claude/Fable 线）

- **1013 同 BV 修复（案 1）：已完成并公开验收（2026-08-01T07:19Z）**。
  BV154GA6vEyD 置换新 CID 40496401196（原 40468153258），
  `repair-verify-live` = VERIFIED_FRESH_LIVE，出版登记已记
  publication_reconciliation。4 处修正：cue9/11 久远澪老师（Ivan 指认 +
  BCUT 独立转写）、cue25 柏拉图（Ivan 确认，101 人舰长榜唯一近音）、
  cue39 伪装成→栽赃给（exact-final 声学回执 + BCUT 双听）。r19 产物金样：
  全片 diff 恰 4 处、时间轴零变化、标题逐字线上、封面字节复用 8dca…。
  人审为实证型（全片解码/静音扫描/8 帧亲验/6 段烧录窗 BCUT 复听），
  receipt 由 Claude root 以 delegated_root_agent 签出（ce5ae6f 扩展枚举）。
  全部证据入库 `reports/authorized_uploads/2026-07-31-1013-jiuyuanling-source/`
  （manifest/plan/completed/receipt/evidence/审计/评审 manifest）。
- **随案发现两条（均为既有特征，不挡置换，已列 backlog）**：
  (a) sidecar `.srt` 相对烧录字幕存在恒定 +6.2s（=intro_offset）位移，
  发布版与置换版同位——疑为烧录 ASS 与 sidecar 写盘各自加了一次片头偏移；
  修复属流水线项，勿在置换 lane 单独动。
  (b) 发布包 sidecar 文本与线上烧录像素在 cue9 本就不一致（sidecar
  「就问你老实说」vs 线上像素「9月林老师」）——delivery-divergence 家族
  （xinyi 案同族）新样本；本轮修复以音频仲裁为准不受影响，但基线=reviewed
  sidecar 的前提要意识到像素可能另有其文。
- **CPA 风暴已解（2026-08-01）**：根因是 oracle 上 CLIProxyAPI 进程病态
  （2d20h 长跑后），`systemctl --user restart cliproxyapi` 治愈，4/4 健康。
  400「当前分组不支持」是上游透传不是配置错。遗留给 Ivan：oracle 上
  `cliproxyapi-codex-warmup.service` 处于 failed；建议加周期重启 timer。
  （历史：r10-r13 四轮死于该风暴不同落点；停机期间全量测试必红→部署门
  连带锁死是政策耦合，非 bug。）
- **reuse-cover × recovery-manifest 证据结转 lane 本轮建成**（字幕-only 修复
  的永久基础设施）：`1878ee1` sidecar 结转 → `ecbb7cd` 身份重打 → `cc856cf`
  先结转后终验（拆鸡生蛋）→ `c192b97` StoryContract 投影重绑本轮 →
  `90f927a` **出版世代 v2 身份见证冻结结转条款**（r18 根因：见证 schema 已
  升 v3，对冻结字节重考 = live 政策重算，且视觉裁判同字节 r17 PASS/r18
  FAIL 彩票；现按 7/27 裁定直接结转 v2 PASS 见证，carry 丢弃三点补
  `carry_drop_reason` 披露）。离线已证明 r19 全链过门。
- **案 2（怕猫 181_480 分句）已结案：判不修（2026-08-01 音频仲裁）**。
  三层转写（BCUT fresh/asr_draft/agy_refined）一致：「猫」与「我也害怕」
  之间有 120ms 真实停顿（padded 24.61→24.73s），"我也害怕"语音落在 cue6
  窗口内。纯文本重切必造成 ~1s 音字错位（比"标点归属欠佳"更扎眼）；
  台账只有 replace_cue/replace_substring 两种文本动作，无重定时；全新重产
  被出版登记 fail-closed 挡死。现状=时间轴精确、断句语义欠佳，任何可行
  改动都是净退化，按 Ivan「能修就修，不能修就别动」判不动。

## 下一步

上一版的第 1–3 项（07-26 tick、`song_192000_1321` draft authority、
`auto_152944_1411_1463` cover route 与 `auto_225056_814_887` 的 subtitle/chat authority）
均已闭环，三天全部 published，不要重复处理。当前队列：

1. 部署 `f616bbf`，让部署测试门生效。这是后续所有改动的护航前提。
2. **封面文案链流水线修复**（不做单切片手写文案，Ivan 07-31 明确否决该方向）：封堵空 punch
   fail-open、renderer 静默换行改硬错误、引号左截断拒绝、上传闸与 cover-only lane 强制
   `rendered lines == CPA final_punch` 逐行相等、退役整数行数门。
3. ~~封面路由修复~~ **已落地 `9f51987`（2026-07-31）**：witness bbox 降为置信
   输入、`subject_confident = geometry ∨ source_composition`、几何否决移到
   relationship 分支之后；24 条历史样本离线重放通过。**剩余验收：接下来
   3–5 条真实生产切片作为 live 样本，人工核对路由选择与封面质量**（分布
   重放不能冒充质量验证）。
4. `auto_192000_909_1014` / `BV1s7326qEc9` 的封面重做：作为第 2、3 项修好后
   **cover-only lane 的首次真实执行验收**，由修好的流水线自动产出文案，不许抢跑。
5. `scripts/audit_lidousha_review_package.py:_audit_policy_fingerprint()` 解耦：它把 26 个
   源码模块的原始字节哈希进 `policy_fingerprint`，任何一行门代码修复都作废全部已冻结
   audit 并触发全量重审。改为显式 policy 版本号 + 脚本化迁移。
6. 后续封面继续执行最终实图检查：多人联动必须确认李豆沙主体；特殊梗必须绑定正确参考形象；
   CPA vision 为首选，公开 CDN 回下载需与目标图 hash 一致。
7. **切片生产提速（Ivan 2026-08-01 点名；已按 CPA 日志实证重排）**。
   oracle CPA 日志（~/.cli-proxy-api/logs/main.log gin 行）证明裁决期大头
   是 1-2 分钟**单发深推理调用**（修正/审片），并发救不了单发——原计划
   a（逐 cue 并发）降级为小赢项。已落地两项：
   - `90f927a` 复用封面不再重考身份见证（消灭整轮报废彩票）；
   - **`6261842`+`aebc1fb` pinned-replay 修复快路径**：v2 exact_interval
     _replay + verified_public_exact 双所有权成立时跳过审片员阶段（其产出
     注定被重放覆盖），零变更授权回执入 exact-final 终审；终审/边界评审
     原样保留。**r21 金丝雀：377s vs 老路径 694s（-46%），交付 SRT 字节
     等价（f96b6161…），discovery COMPLETE/release PASS。**
   教训（r20 烧一轮）：跳过阶段前必须穷举其输出对象的**传递性**消费者——
   final_review_audit 还作为 correction_audit 喂进终审做变更授权推导。
   剩余排序：新关键路径=转录/AGY 精听（~3-4min）→ 逐 cue 短调用串并发
   （小赢）→ 烧录∥封面。每项测试+金丝雀单独上，门链顺序不动。
   **luna 换 sol 试验结论（2026-08-02，Ivan 要求穷尽级验证后叫停）**：
   gpt-5.6-luna 已被上游启用；小样 A/B 闭集/念弹幕口味判决一致但边界
   评审分歧（sol 命中线上验收锚点），首轮金丝雀还出过一笔 luna 2m5s 500。
   曾短暂换链后按 Ivan 裁定回退（`ec414b4`），生产全线保持 sol。根本账：
   CPA 法官 lane 三周只有 5 次调用（配额收益≈0），量大的 lane 全是
   luna 已显分歧的深语义类。字节级重放工具已入库
   （`scripts/ab_model_replay_closed_set.py`，标准=重渲染 prompt sha 等于
   历史 judge_prompt_sha256），但现存语料 5/5 inputs_missing——若未来
   配额压力要重启此题，先给法官行持久化完整 request/witness 输入，攒够
   N≥30 真实案例再跑该工具。

## 固化规则

- 用户只指出 1–2 个问题且未说明问题穷尽：默认整片重跑；指出 3 个及以上问题：修指定位置及
  背后通病，不因这条规则再次整片重跑。
- 大部分 glossary 专名允许按期望收益机械替换；专名之间平等，两个已注册专名冲突时由 CPA
  结合文字上下文裁决。
- 已发布稿只走 `scripts/authorized_upload.py repair-*`。**封面单独修复的入口已于
  2026-07-31 更换**：`scripts/bili_cover_edit.py` 被 `28b3576` 改成开头无条件
  `return 2`（旧实现仅作 endpoint archaeology 保留），现行入口是
  `scripts/authorized_upload.py cover-repair-plan / cover-repair-run /
  cover-repair-verify-live`。禁止新建替代 BV、裸 API、legacy replace 或删除旧证据制造绿灯。

### 2026-08-08 夜间权宜上传授权（Ivan 就寝前原话）
「你晚上把所有切片做完后可以执行权宜上传，我醒来后再进行审阅修改。」范围=8/7 两条
（真善美 auto_203735_555_680 真值版、交付件 auto_200736_298_383 重产版），前提=全部
fail-closed 门通过+真善美验收（Ivan 真值逐字相等/假李豆沙=0）。被拦项不传留证待晨审。
7/22 auto_200511_61_138 Ivan 已裁定封存不编辑不上传。上传后按 authorized-upload 惯例
commit 证据。之后执行工程优化①②③（任务卡 #10/#11/#12），完成前不碰新切片。

**2026-08-08 夜间执行结果：0/3 传出，全部 fail-closed 拦下，未强推。** 范围按
Ivan 原话「所有切片做完后」覆盖到 8/7 全部 review_ready talk（真善美另需真值验收，
本轮排除；`auto_223750_913_1322` cover_pending、`auto_220747_1271_1323`
candidate_rejected 本就不在范围）：`auto_200736_298_383`、`auto_210739_1142_1436`、
`auto_220747_488_680`。三条均在 package audit / manifest 构建阶段即被拦，
从未触达 `authorized_upload.py make-manifest/verify/upload`：

- `auto_200736_298_383`：`audit_lidousha_review_package.py` BLOCK（47 项）。根因是
  record.json `artifact_hashes.ass_sha256`/`burned_video_sha256` 与当前
  `.recut.final-sapphire72.ass`/`.recut.burned-final-sapphire72.mp4` 磁盘字节不一致
  （record mtime 晚于 ass 文件却仍不匹配，疑似 record 的 artifact_hashes 块本身滞后于
  某次后续重写，尚未查明是哪个环节）。今晚未进一步调查（不做新 produce、直播中）。
- `auto_210739_1142_1436` / `auto_220747_488_680`：`build_lidousha_daily_review_manifest.py`
  REFUSE `required package file missing: ...recut.burned-final-sapphire72.mp4`。
  两者 `replacement_recuts/` 下只有 `burned-final-speaker.mp4` +
  `speaker-final.ass/.srt/.json`（8/7 `AUTOSLICE_SPEAKER_MODE=auto` 翻转后的双样式
  产物），从未产出旧 `sapphire72` 统一主播样式烧录。`build_lidousha_daily_review_manifest.py:343,350`
  硬编码 `{stem}.burned-final-sapphire72.mp4`/`{stem}.final-sapphire72.ass`，翻转后未随之
  更新，两条案例同一根因。

证据落盘 `reports/authorized_uploads/2026-08-08-provisional-attempt-blocked/`
（package audit JSON、两条 manifest-builder 拒绝原文+目录清单）。下一步：先修
`build_lidousha_daily_review_manifest.py` 认识 speaker-mode 产物命名（或按 record
分支），再单独查 `200736_298_383` 的 hash 漂移根因；直播结束后再重试上传，仍不得
为了变绿而强推任一门。

**Part B（真善美 baseline-injected 复现，`tmp/reproduce_zsm8.log`）诊断结论**：
`PRODUCE_EXIT 1`，标记 `CHAT_AUTHORITY_FINAL_ARTIFACT_FAILED` 指向
`out/2026-08-07/auto_203735_555_680/auto_203735_555_680.chat-authority.json`；
`final_status=FINAL_ARTIFACTS_FAILED`，
`final_verification_failure=REDELIVERY_BASELINE_FINAL_OWNER_NOT_VERIFIED`。**不是
baseline 绑定/注入错误**：注入 sha（`49e63c6c...`）与 `redelivery_subtitle_baseline_audit`
一致，`status=APPLIED`，61/61 cue mapped，应用阶段 `failures=[]`。真正原因是**下游
覆盖**：`exact_final_cpa_self_heal`（`final_review_audit` 内的 CPA judge + AGY/Gemini
声学 witness 自愈通道）在 baseline 已正确应用之后，独立重新聆听并覆盖了 3 个已受
baseline 保护的 cue（52/58/59：「哈哈，我的信原来在你手里吗」→「我想死在你手里吗」、
「你懂吧」→「懂吗你」、「你知道我要偶遇偶遇」→「我要有遗言遗言」），与 Ivan 真值逐字
不符；两轮自愈的 `repairs` 明确记录了这三处改写。末端 owner 校验门正确拦下
（PRODUCE_EXIT 1），没有坏文本流出。根因：`redelivery_subtitle_baseline.py` 只写
`owned_intervals`，全仓库无任何读取/使用（`grep owned_intervals` 只命中写入行）——
自愈通道不知道、也不尊重 baseline 已拥有的区间。下一步：在
`exact_final_cpa_self_heal`/`final_review_audit` 里读并遵守 `owned_intervals`
（已被 reviewed baseline 覆盖的区间禁止自愈改写）+ 回归测试；修复须部署自当前分支
tip（`codex/virtuareal-community-crawler` 之上，**不是 main**，main 会回滚 17 个 codex
提交），直播结束后重跑复现，通过后再走真善美真值验收。

### 2026-08-08 晚：权宜上传授权已撤回（Ivan 原话「如果没有上传就可以先不上传了。我要先看8/7的切片审阅后再说」）
确认零上传发生。改为审阅优先：真善美真值终版（验收 text=0 diff/假李豆沙=0）已发 Ivan；
其余三条 8/7 talk 待其审阅。上传须 Ivan 审后重新明示。8/8 批产线继续（产≠传）。
