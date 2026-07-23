# Current handoff

Updated: 2026-07-23

> 只记录尚未完成的当前任务与恢复点。流水线规则见
> [pipeline/README.md](pipeline/README.md)；这里的 runtime/公开状态在继续操作前仍须 live
> readback，不能把本文件当现行证据。

## 目标

系统性修复 2026-07-22 五条李豆沙切片的字幕真值、长程语境、边界、候选状态、量化评分、
标题、封面和最终产物审计；用当前能力在隔离 recovery base 重跑、复核并覆盖本地旧审片包。
已发稿只允许同 BV 修复，不新建重复 BV。

## 本地工作树已实现（尚不等于部署/成片）

- stable candidate ID 后再执行严格 reviewed calibration；无效/漂移资产 fail closed；
- exact contract closure 与统一 terminal projection，candidate 不再同时出现在成品和候补；
- 未截断 60k 整片 context、18k cue-aware supplemental prompt、时间采样聊天、topic graph
  scoped candidates 与 prompt 重渲染绑定；
- correction pass `final-review-audit.v1` 与精确最终 SRT 放行回执
  `final-review-audit.v2` 分离，provider/JSON/空结构失败不再假绿；
- source truth / reviewed baseline / story-chat required owner 冻结，边界只能完整覆盖或 BLOCK；
- 三条已确认字幕回归已进入 hash-bound source truth；partial structured-chat evidence 只能修复
  有声学/画面见证的槽位，不能再把整条 SC 扩写进字幕；
- required owner 只抬高合法结束下界，原 semantic/manual repair origin 与 30 秒 cap 保持不变；
  cap 内找不到完整收束即阻断；
- package audit schema 仍为 `lidousha-review-package-audit.v2`，当前 policy epoch 已升为
  `2026-07-23.final-artifact-gates.v3`；
- 封面已使用 `lidousha-cover-rendered-text-pixels.v3`、deterministic render spec、
  committed trusted font 与 package-internal pre-overlay/mask/route-background 精确重组门；
- 现行规则已收敛到 `docs/pipeline/`；根 AGENTS 与 publish skill 只保留步骤/操作入口。
- same-BV source state machine 已实现 `repair-plan / repair-run / repair-status`、hash-chain
  journal、append-at-most-once、固定 CID swap retry 和四面终态验证；专项测试与
  authorized/member API 合并测试已通过。

以上项目仍在共享 dirty worktree 中，最新一轮全部改动后的定向/全量测试尚未完成；不保留
旧的固定“通过 N 项测试”数字。

## 当前恢复事实

- `2026-07-22-full-rerun-review` / v8 只证明旧流水线曾生成可审材料。它缺少当前
  final-review v2、required-owner、cover-pixels v3 与 epoch v3 的完整闭包，**不能视为当前
  合规，也不能直接上传或覆盖旧包**。
- exact talk 目标集合是 `3573, 672, 1863, 1573, 1475`；`6577` 被用户明确抑制，不允许普通
  backlog 补位。必须在新的隔离 recovery base 达成 closure COMPLETE 后再重建本地包。
- 普通 7/22 state 的 source 仍不完整；恢复必须继续使用已验证的 official immutable recovery
  source，且不得覆盖普通 state/out。继续前复核当前源 SHA、磁盘、进程/锁和 target base 不存在。

## 进行中

1. 运行整合后的定向测试、全量 pytest、`git diff --check`、旧包负向 audit canary；
2. commit/deploy 到 `free`，读回 `DEPLOYED_COMMIT` 与实际文件 hash；
3. 在新 recovery base exact 重跑五条，closure COMPLETE 后重建 review manifest 和扁平包；
4. 对最终视频逐条复核字幕、边界、标题、封面、StoryContract 和 package audit，再覆盖本地旧包。

## 当前约束

- same-BV 状态机当前只在共享本地 worktree 得到测试证明，**尚未因此自动部署，也没有修复
  五条线上稿件**。必须等本轮整体测试、commit/deploy/live smoke、五个合规最终包和真实 dry
  plan 完成后，才按 [pipeline/90-publish.md](pipeline/90-publish.md) 执行；legacy
  append/swap/replace 入口仍禁止。
- 新 Pro 补充请求曾失败；可读的既有 Pro 回答已经用于设计，但不能把失败请求写成成功复核。

## 完成判据

当前代码通过全量与负向测试并部署读回；五条在新 base 产出同一政策字节、人工/自动复核通过、
本地旧包已覆盖；随后在当前 same-BV 状态机完成部署与真实 dry plan 后，对原 BVID 执行修复
并完成 public / public tags / Creator / section 四面验证。
