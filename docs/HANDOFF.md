# Current handoff

Updated: 2026-07-25 America/New_York (7/24 四条已公开上传；daily 上传链已打通)

> 本文件只记录尚未完成任务的恢复点，不复制流水线规则。步骤规则只读
> [pipeline/README.md](pipeline/README.md)，live runtime 只以
> `free:/opt/bilive/autoslice` 的当前 state/out/reports/process/lock 和公开面读回为准。

## 目标

1. 用当前流水线系统性修复 2026-07-22 五条已发李豆沙切片，exact 重跑
   `3573, 672, 1863, 1573, 1475`，抑制 `6577` 且不补位；全部封面走
   `screenshot_direct`。机器闭包、root 最终逐片观看和授权门通过后，只修复原 BV，不新建重复稿。
2. 2026-07-24 当日场次在修复后的流水线上正常交付（talk 首批在 219b111 下 0/10，
   6 个 `failed` 属于同一批 systemic 缺陷，部署新 commit 后按 fingerprint 变化自动 requeue）。

## 已完成

- 历史阶段（codex）：`ba06fe8`、`d995c84` 第一阶段；`f9223c0` 第二阶段并部署（08:23:21Z）；
  `219b111` 第三阶段并部署（09:44:19Z）。V13（f9223c0）与 V14（219b111）两轮 fresh 重跑均
  `recovery_incomplete`，5 尝试 0 交付。
- V14 五项失败已全部根因闭合（Claude 2026-07-24）：
  - 3573/1573：`BOUNDARY_RETRY_OWNER_SET_DRIFT` —— 跨尝试 owner 哈希绑定了 fresh-ASR 派生
    的 story-chat owner 几何（含 `entity_repair:1:52240:55120` 这类毫秒内嵌 ID），重转录后
    必然漂移；见证窗 retry 因此永不可能通过。
  - 672：`SOURCE_TRUTH_BOUNDARY_OWNER_SCOPE_STRADDLE` —— 开场真值 cue（672920）比
    semantic_start（672960）早 40ms，撞 immutable scope 起点。
  - 1863：exact pin 模式下推荐集合为空 —— fresh 收尾 cue [201370..202930] 越过官方 pin
    202720 共 210ms 被排除，前一 cue 又早于 pin−400ms 窗；LLM 四命题全 true 仍死局。
  - 1475：旧发布尾点 given_end=1543760 作为硬下限进推荐窗，而故事真实收尾 cue 止于
    77380（local），距下限 400ms 纯静音；正确切点被旧机器几何禁止。
  - 另发现当日 lane 回归：零 owner 冻结在 `producer_boundary_resolution` `min([])` 崩溃
    （全新场次无 ledger 真值时 100% 触发）。
- 本轮修复已提交：`41d9cb1`（系统性修复+测试+docs，含 codex 未提交的 500ms 开场容差）与
  `11139da`（7/9 speaker anchor 证据替换，见下）：
  - 跨尝试 drift 门改绑 `deterministic_owner_set_sha256`（source_subtitle_truth 子集）+
    scope SHA；ASR 派生 owner 按尝试各自冻结执行，回执披露 `asr_derived_owner_binding`；
  - `recommendation_eligibility` 共享确定性资格函数：pin 跨界收尾 cue（≤600ms，媒体仍锁
    pin）与静音间隙收尾 cue（≤400ms=tail-pad 桥，交付下界不动）两类有界放宽，review 与
    resolver 两侧同函数重算，`recommendation_relaxations` 留证；endpoint binding 按 grid
    containment 重算不信任自述；
  - 零 owner 冻结合法化（无 owner 下界）；
  - `PIECE_POST_MS` 32s→48s 且人工下界差额逐候选加入 post pad——首窗覆盖
    origin+30s+15s 常规需求，不再必然走整窗重转录 retry；
  - witness 证据要求写入 prompt（next_topic_separated=true 必须给推荐点之后的证据 cue）。
  - 定向与全量测试、ruff（新增文件零告警）、compileall 通过；docs/pipeline/30-boundary.md、
    40-subtitle-text.md 同步。
- immutable recovery source 三项 SHA（source state / official MP4 / BCUT）与 V13 建立前
  记录一致，未再变更；V12/V13/V14 全部保留为失败证据，未续跑未复制。
- 2026-07-24 free ENOSPC 事故与处置（`11139da`）：磁盘 394G 满（qbittorrent 148G 未动；
  out/ 老日期 123G）。按容量惯例删除 out/2026-07-09..16 可重算媒体约 54G 后发现其中
  5 个 recut 被 speaker anchor 资产强引用（3 target + 2 donor），字节级不可恢复；已把
  anchor target/donor 改绑 upload manifest 见证的烧录交付件（`-c:a copy` 音轨 bit 级同源、
  时长与 recut 完全一致），原 path/sha 保留在每个 `media_substitution` 披露块，
  speaker_batch_plans 与测试 pin 同步。教训：删 out/ 媒体前必须先扫
  assets（anchors/batch_plans/talk_recoveries）引用。磁盘满的连锁症状：recorder status
  stale→runner fail-safe skip、deploy guard mkdir 报"guard already exists"（误导）。

## 当前工作树与权威 hash

- 只允许提交本轮 tracked 代码、资产、测试与 docs；不得碰用户/生成物：
  `lidousha/.recovery-archives/`、`uv.lock`、当前
  `lidousha/2026-07-22-full-rerun-review/` 未跟踪媒体/sidecar。
- committed authority hashes（219b111 起未变）：
  - recovery publication registry：
    `sha256:be9ffbd42008b94d9e47ea714e1fae5d032f576bb0e71841624df3b77ea53757`；
  - subtitle truth ledger：
    `sha256:794a4e2f45beae9e612ae884ca48eea2cd601d3c4537f05ae4fc024fd88a5749`；
  - final media review contract：
    `sha256:d51040d6d02931c328c31c6a8fa52b457e096c46d0959dfc7b3b8ea22a67cbd1`。
- immutable recovery source：
  - source state：
    `sha256:fc26e2d68f4d78f4420b3e49d79f791bd24e4c3bcf2740862e380e7af7108f54`；
  - official MP4：
    `sha256:0eb2778dc53e5eabbccae089e5db92d3fb3662d90e1dd2ddbe7765436718989a`；
  - BCUT：
    `sha256:edc0d233b49ce2beed6e82c9adae5ffbd76f58eae63b8d9448f268d8f1b30bce`。

## 进行中

已完成：`11139da` 于 `2026-07-24T19:34:12Z` 从干净 detached worktree 部署到 free，15 个关键
文件 SHA 逐一读回匹配，无 guard/DISABLED/staging 残留。fresh V15 已于 `19:35:28Z` 建立并启动：
`/opt/bilive/autoslice/recovery/2026-07-22/full-rerun-v15-screenshot-cover`，
plan 为 v7 exact-no-backfill 五项、`6577` 已抑制、1475 为 replacement、`upload_allowed=false`、
四处 `AUTO_UPLOAD` 均不存在、五项 `given_end_ms` 与 registry 一致；runner 以
`AUTOSLICE_COVER_MODE=screenshot` 运行。尚未宣称任何产物通过。

### V15 结果（2026-07-24 20:23Z，两轮 tick）

`talk 2/5 delivered`（V13 0/5、V14 0/5 → V15 2/5）。**边界层修复已被生产证明**：
五个候选全部越过 V13/V14 的边界死锁；1573 的见证窗重跑零 `OWNER_SET_DRIFT`；
1475 的源层审查逐字记录了 `silent_gap_closure_cue`（cue 39 收在 77380，旧下限 77780，
中间 400ms 机器验证为纯静音，尾垫桥补齐、下限未动），收尾句「再弹，再，再硬弹一弹」。

| 候选 | 结果 |
|---|---|
| 1475 | 交付，`release_gate=PASS` |
| 3573 | 首轮 provider 抖动，重试后 `CLEAN/PASS`（零 finding），交付 |
| 672 | provider 已消除；剩 cue85 `听到是吗`，声学裁定 current/proposed **双双 INCOMPATIBLE** |
| 1863 | cue100 `这切哈哈`，终审提议 `这期哈哈`；BCUT 官方与 AGY 两独立引擎均写 `这切` |
| 1573 | cue76 假名门 —— **已定位为门缺陷并修复**（`d217a40`），见下 |

**重要判定（两次自我推翻，勿再重犯）**：终审 `findings` 门**不是**过度限制，不要去放宽它。
它校验的是**对最终字节的独立重扫**，此层不能再改字节，`repaired:true` 只是沙盘裁定。
逐例查证结论：607_723 是首轮漏检的真错字（`粉团灯牌`→`粉丝灯牌`）留在成片里；
672 cue85 是现文本与提议**都不符音频**（闭合条件要求 current_fit∈{SUPPORTED,PLAUSIBLE}）。
两次都是门在保护质量。剩余阻断是**真实字幕问题**，正解是修字幕（ledger 真值条目 / 重出），
不是松门。

### 1573 假名门（已修，`d217a40`）

`梅杰克家的六更るり` 是 SC 打赏者真实用户名，逐字来自
`clip_context.structured_chat[8].sender`。source-language 守卫只认草稿转写与音译两种证人，
于是把"从平台记录恢复真实用户名"判成凭空引入外语。音频 `kana_similarity=0` 不构成反证——
主播用中文腔念日文假名 ID 是常态，用户名字形归平台记录所有。已让结构化弹幕 sender/gift 名
正向见证自身 kana（`structured_chat_name`），豁免精确且完全：cue 内每个假名都必须落在这类
名字里，掺入臆造日语仍 fail-closed。注意 chat-authority.json 里搜不到该名字，绑定在
clip-context.json —— 排查时别搜错文件。

### Round 6–7 收敛（e32db2c → bb69cef，round 8 跑中）

Round 6（35ea74e，Ivan 三处音频裁定入 ledger 后）：672/1475 交付；3573 provider
transient；1573 baseline 贝利对齐待提交；1863 出新病。Round 7（e32db2c）：3573/1475
交付；672/1863/1573 翻转失败。三案根因与裁定（bb69cef）：

- **1863 边界借字（新病种，两形态）**：SC 尾字「了」她没念。r6 独立转录连写到下一句
  「哎，现在几点了」，SequenceMatcher 让 SC 尾「了」借下一句同形字伪造 near-complete
  → 已修（e32db2c 边界孤立小块跨 ≥3 字 observed 插入剥离，披露
  `borrowed_boundary_blocks_stripped`）。r7 独立转录又抖动出真尾「了」（把「哎」听成
  「了」级别的网格抖动），gate 重开 → SC 注入 → baseline 拉回 → 终验死锁。正解
  （bb69cef）：replay audit 的 before→after 映射是因果证据（修复文本曾在字幕、被已
  验证 baseline 有意替换），此时 boundary owner 行退位
  `SUPERSEDED_BY_REDELIVERY_BASELINE`；无 revert 记录的有据修复仍 fail-closed
  （cannot-overwrite-supported-repair 测试保持通过）。
- **672 跨句借音错裁**：终审裁定「太礼墨Sumi了」是错的——AGY 9.25s 听证窗
  [280190..289440] 覆盖前句「观众只会猜礼墨Sumi」，heard=lin mo sumi le 系借前句
  音节顺从提案。Ivan 已审 baseline「礼墨太出名了」正确（因果承接前句）。672 是
  exact_interval_replay 包，mapping 无 before 字段拿不到 revert 因果证据，已落确认性
  真值 `20260722-nnll-limo-tai-chuming-r1`[948630..950530] 拦 correction pass 复提。
- **1573 贝利是我裁错的**：昨日「双引擎一致=贝利」实为两引擎同缩弱音节 dì 的相关
  错误（bèi-dì-lǐ 同听成 bèi-lì）——转录网格在声学难点上不独立，不构成共识证据。
  hash-bound 声学强裁（heard=dàn qí shí bèi dì lǐ...，置信 1.0，按音节计数）+
  relation authority 无贝利实体 + 语义承接，正确文本=「但其实背地里是被欺负的那种」
  （其实是真音节保留）。ledger r2 + baseline 已修订 rehash。**教训：转录共识 ≠ 独立
  证据；音节有无级分歧必须声学强裁仲裁。**

Round 8 预期：1863 走 revert 豁免、1573 truth/baseline 一致、672 truth 拦提案、
3573/1475 重验。已知残余风险（观察 round 8，不预防性工程）：exact-replay 包上
correction pass 若对 truth 替换后的通顺文本再标 suspect，提案 vs truth 会走
UNRESOLVED finding 阻断——真发生再修（修法参照 1573：finding 由 truth 裁决）。

剩余步骤：

1. V15 必须跑到 exact closure COMPLETE；任一 exact candidate 失败都不得补位或沿用旧产物。
   注意 V15 与主 cron 并发共享 AGY/CPA 配额，出现 provider transient 属可重试而非裁决；
2. 当日 2026-07-24 的 5 个 `failed`（4×pipeline_contract + 1×producer_error）依完整流水线
   fingerprint 变化在下个 cron tick 自动 requeue；4 个 `candidate_rejected`
   （3×外语转写门 + 1×段尾边界）不在 `TALK_RECOVERY_FAILURE_STATUSES` 内，保持 fail-closed
   不复跑；
3. COMPLETE 后整包重建 manifest/audit，先同步 staging，audit 与 rsync 空 diff 通过后才
   `--delete` 覆盖本地旧审片包；
4. root 完整播放五个最终烧录 MP4，按 committed exact points、八项检查与 StoryContract
   cover claims 出具真实 `delegated_root_agent` receipt；
5. receipt 与 authorized manifest 通过后，对五个原 BVID 执行
   `repair-plan --dry-run → repair-plan → repair-status → repair-run --dry-run → repair-run`，
   最后核对 public/public-tags/Creator/exact-section 四面。3573/672 线上标题当前缺
   `【李豆沙】`（人工标题曾绕过 envelope 门）；registry 以 `ivan_manual_override` 存 Ivan 手定
   正文，`canonicalize_publish_title` 会补前缀成 29/38 字，本轮修复应一并纠正这两条线上标题。

### 2026-07-25 深夜：7/24 四条已公开上传（16f4e0b 证据入库）

199=BV1jw3j6KE17、424=BV1E93L6rErV、537=BV1Eo3L6zEdU、607=BV1Eo3L6zECt，
全部 `VERIFIED_PUBLIC`，走完整 v3 链（package audit 零阻断 → make-manifest 绑
Ivan 原话 → upload → uploaded.json 哈希回执）。第 5+ 条待 beans/skill 收敛。

**daily 上传链本轮打通（b19dfae + 852627b，全部有测试）**：
- v3 合同要求 package audit，但 audit 各合同都是 recovery 形态写的。四个结构
  性错配逐一在病根处修：
  1. ASS audit 加 uniform_host 通道：冻结 record+chat authority 自证跳过
     speaker finalize（speaker_ass 双 None + speaker sha==text sha）时，正文
     SRT 以 host 说话人解析，**event parity 重放全量保留**（style=Default）；
     recovery 包带真 speaker 工件哈希，永不满足声明，严格路径不变。
  2. owner scope 校验复刻生产端 lead-tolerance 算式（story_start=semantic−500，
     容差上限锁常数），旧算式拒绝所有现行 scope。
  3. **ledger 前进等效**：包产出后 ledger 增长，仅当新增活跃条目与包源区间重
     叠且 truth_id 未被冻结审计见过才判 stale（须重产）；否则字节漂移不再阻断。
     无 provenance/解析失败一律 fail toward reproduction。
  4. 单字 cue 校验豁免封闭语气词类（哎/啊/呵…），实词碎片照旧拦。
- 新工具 `scripts/build_lidousha_daily_review_manifest.py`：单候选装配
  review_manifest（hash-bound、封面像素工件、chat authority 校 record sha 后
  复制入包、same-stem 上传家族字节副本）；batch 门收 processing、放行
  retry_wait（review_ready pick 主车道从不 re-supersede）。
- same-stem 合同：video stem X 要求包根直下 X.record.json/X.srt/X.cover.png，
  make-manifest 的 --cover 必须指 same-stem 副本。

### V15 r17→r18 根因链（237bc45 + 4839a6b，r19 已定时）

r17（b19dfae）五案全败=**resolution 侧 identity replay 重建 expected scope 漏传
baseline_tail_cap_ms**（13cbcbf 只修 is_valid，漏三方 dict 比较点）→ 全体
SCOPE_MISMATCH（237bc45 修，抽取 `_replayed_search_scope`）。

r18（237bc45）确认 scope 层已通（候选进入终审），残余两类：
- **1863 恒 BLOCK=尾锚轴不匹配**：exact pin 是媒体字节终点轴（202720），
  baseline 覆盖终点是字幕轴（202320），天然差一个 400ms 尾垫；cap 钳 pin →
  OWNER_EXCLUDED。**pin 模式跳过尾锚**（pin 权威更强；4839a6b+测试）。
- 672/1475/1573/3573 全部=**声学/correction provider 配额潮汐**：AGY 月配额死
  + Gemini 免费 3key 日配额尽 + 付费 backup 429（provider-failures.json 实
  锤）。fail-closed 全部正确（如 3573 cue7 和天依/洛天依 两读法都通，必须声学
  裁决）。免费 key UTC 07:00 重置。
- **r19 已定时**：free 端 systemd 一次性 timer `v15-r19-oneshot` 于 2026-07-26
  07:10 UTC 自动刷树+拉起（脚本 /opt/bilive/autoslice/v15_r19_launch.sh，日志
  $T/logs/r19-launcher.log）。
- 部署尸留教训：deploy 外层 5min 超时被杀 → DISABLED 尸留 → 第二次 deploy 视
  其为既有开关不清除 → 主 lane 静默停 20min。deploy 必须后台无短超时跑；已
  手动清除该尸留。backlog：deploy 起点发现 DISABLED 已存在时加警告输出。

### beans/skill 复活状态（第 5+ 条上传的来源）

- skill 850_940：终审已 CLEAN（7fcd94c truth 波生效），最近一轮死于 CloudFS
  挂载瞬死（realpath FileNotFoundError），failed+recoverable 自动重试中。
- beans 1209_1410：revive_rejected_candidates.py 已复活（candidate_rejected→
  failed+recoverable，fix-commit b19dfae），等 cron 重跑。
- 任一转 review_ready 即按同链上传（audit→make-manifest→upload→commit 证据）。

### 2026-07-25 夜间授权与机制账（Ivan 睡前指令）

**授权**：7/24 至少 5 条切片修复好后**权宜上传**（他睡前原话"做完了之后应该权益上传…
记得要上传5条切片，修复好了就上传"）；7/22 五连修好后执行同 BV 修复替换（旧授权）。
上传证据必须 commit。

**7/24 十候选全景**：2 delivered（豆角/技能）+4 findings-rejected（607 灯牌/199/537/
424_535）+2 外语门+2 段尾边界。rejected 是防 backfill 化石态、修好上游也不自动重跑——
新工具 scripts/revive_rejected_candidates.py 是唯一 sanctioned 复活通道（校验化石态、
翻 failed+recoverable、revival 审计块、flock+原子写；注意 runner 重写 pick 会丢
revival 块——审计持久性缺陷记 backlog）。

**今晚机制修复链**（每个都有实案+测试）：
- 语境关联召回 entity_context_recall.py（叹十七手案：熊类关联词+kmx 已确认→句首杂段送强裁；
  UNCERTAIN 保留不阻塞；确证改写。KO熊 变体（537 案）另加了 surfaces）。
- 审查员 prompt 双修（星座→新作案）：音近候选推理强制化（null=阻断无出路，穷尽近音才许）；
  self_ref 昵称语境豁免（游戏 ID 可含主播名，温柔型李豆沙案）。
- 终审结转 final_review_carryover.py（199/537 死循环案）：correction pass（可改字）与 exact
  终审（不可改）是两次互不通气的独立 LLM 扫描——B 声学确证的修复持久化 sidecar，下轮以
  raw 行进 A 的**同一解析循环**（stale 预过滤防 ALL_INVALID 分母污染；第一版绕过解析器
  直接 merge 曾炸 KeyError→CORRECTION_MUTATION_AUTHORITY_INVALID，勿回退）。
- CPA 画面见证 cpa_frame_witness.py（Ivan：看画面交给 CPA，AGY 主听）：hash-bound 单帧
  视觉问答；**gpt-5.6-sol 默认**（3 轮基准关键专名 3/3，terra 1/3，luna 会编造字幕禁用；
  视觉模型有轮间抖动，单轮基准不可靠）；CPA 只有 gpt-5.x 且必须 /responses+input_image
  （chat/completions 502）；帧降采样 1280w JPEG。已实战：读出温柔型李豆沙/投抱月。
- redelivery 头尾**恒等锚**（1573 r10/r13 案）：头=baseline 覆盖起点（min 单向钳不够，
  fresh snap 前漂会 STRADDLES 死锁）；尾=推荐上限钳 baseline 覆盖终点（lower_bound 语义
  延伸会把 V13 排除的鼠标话题包回来）。同 BV 修复逐毫秒复刻已发布边界。
- 真值目标选择擦入豁免（1573 r12 案，apply 侧）：replace 目标 cue 的重叠**又小又短**
  （<50% 且 ≤400ms）判边界擦入踢出；实质跨 cue 内容保留；保护/覆盖路径维持保守 80ms；
  drop_cue 完整包含判定不动。终验 owner payload 的 majority 门（r11）是它的浅层前置。
- 百合作品关联即出（樱抱月案）：title_style.md+upload_tag_policy（adachi_shimamura）；
  相关（含间接：队友 ID=角色名组合）就进标题和 tag。
- 424_535 八条真值（Ivan 亲裁：温柔型李豆沙×4、新作×2、李豆沙（本物）cue19、樱抱月 cue27）。

### 架构决定（Ivan 2026-07-25 拍板，刮/乖/歪案后）：声学层降级为证人

原话要义：**声学裁决不能叫裁决，只是一层声学证据；不能让它出汉字，只能出疑似
拼音；后面的推理依靠 CPA。** 三案同病（省了/神了、我们/我、刮/乖/歪）：闭集声学
相容检查的 ±1 句语境窗残缺甚至被错字污染（乖在 context_before）、平行句不在窗内、
按设计禁止语篇推理——单窗音节报告被当成了终审法官。

目标形态（Phase 1 = correction/终审 context 裁决层，Phase 2 = entity/read-aloud 同型）：
1. 声学证人：纯听写模式（不给候选防引导），输出疑似拼音序列+置信，schema 更名
   witness；不判 fit、不出汉字。
2. 选字裁决归 CPA（sol）：输入=拼音证据+±3 句语境+平行句+结构化弹幕+词表+候选，
   输出=闭集选择+引用证据的理由。
3. 防顺从铁律保留：CPA 只能从闭集（现文本/提案/平行句读法）选或 UNCERTAIN，
   不得生成新字；选择的拼音须与 witness 拼音族相容（代码层校验）。
尚未实施——先完成 7/24 权宜上传（Ivan 行军令优先），随后做 Phase 1 手术。

## 约束与阻塞判据

- V8–V14 都只是历史或失败证据，不得续跑、复制 state/out/receipt、补 hash 或冒充
  current package。V15 也只有 exact closure COMPLETE 才能覆盖本地包。
- `6577` 永不补位；任一 exact candidate 失败都保持真实失败状态。
- 本轮五封面强制 screenshot 是内容选择：源帧能证明双人/角色/情绪；不代表 AI 生图功能未部署。
- recovery review manifest 始终 `upload_allowed=false`。新 BV 需要 `AUTO_UPLOAD`；本轮只走
  exact same-BV receipt/authorized-manifest lane，不创建 `AUTO_UPLOAD`。
- 感知 receipt 只有 root 实际完成五片全片观看后才能签，不能把机器 audit 或旧包观看冒充
  当前最终字节复核。
- `AUTOSLICE_SUMMARY.md` 仍是醒目标记的历史 V8 快照；只有 V15 成功并覆盖审片包后才从最终
  state/records 整份重生成，不能局部改旧数字伪造新历史。
- 已知但本轮不修（记录在案）：段尾候选的 witness reserve 无法跨 segment 文件取后文
  （1571_1804 类）；2026-07-19 `auto_163109_91_291` cover repair 因 delivery record
  title/upload binding mismatch 被 preflight 拦下。

## 完成判据

最新代码 clean commit 并部署读回；V15 exact 五项 closure COMPLETE；当前 package audit、
root final-human receipt 与本地镜像验证全部通过；五个原 BVID 完成 same-BV repair，且
public、public tags、Creator、exact section 四面一致；2026-07-24 当日批次在新代码下正常
交付或留下真实 fail-closed 记录；相关现行 docs/summary 与发布证据提交。
