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
- 本轮修复（待 commit 的工作树，含 codex 未提交的 500ms 开场容差，一并保留）：
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

1. clean commit 本轮修复，`deploy_free_autoslice.sh` 部署并读回 `DEPLOYED_COMMIT`
   （脚本自带 runner.lock 等待，当日批次跑完前不会切换）；
2. 部署后当日 2026-07-24 `failed` talk picks 依 fingerprint 变化自动 requeue，观察下一 tick
   交付（`candidate_rejected` 的 3×外语转写门与 1×段尾边界属设计内 fail-closed，不复跑）；
3. 新建 fresh V15：
   `/opt/bilive/autoslice/recovery/2026-07-22/full-rerun-v15-screenshot-cover`，
   由 v7 planner 从 immutable v2 source 与新部署 commit 重建，重算五项 per-candidate
   pipeline fingerprint，验证 exact-no-backfill、`6577` suppression、1475 replacement 与
   四处 `AUTO_UPLOAD` 不存在，以 `AUTOSLICE_COVER_MODE=screenshot` 跑到 exact closure
   COMPLETE；任一 exact candidate 失败都不得补位或沿用旧产物；
4. COMPLETE 后整包重建 manifest/audit，先同步 staging，audit 与 rsync 空 diff 通过后才
   `--delete` 覆盖本地旧审片包；
5. root 完整播放五个最终烧录 MP4，按 committed exact points、八项检查与 StoryContract
   cover claims 出具真实 `delegated_root_agent` receipt；
6. receipt 与 authorized manifest 通过后，对五个原 BVID 执行
   `repair-plan --dry-run → repair-plan → repair-status → repair-run --dry-run → repair-run`，
   最后核对 public/public-tags/Creator/exact-section 四面。

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
