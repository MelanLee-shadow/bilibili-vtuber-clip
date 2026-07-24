# Current handoff

Updated: 2026-07-24 America/New_York

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
