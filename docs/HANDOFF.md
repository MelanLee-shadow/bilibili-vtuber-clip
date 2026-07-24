# Current handoff

Updated: 2026-07-23

> 只记录尚未完成的当前任务与恢复点。流水线规则见
> [pipeline/README.md](pipeline/README.md)；这里的 runtime/公开状态在继续操作前仍须 live
> readback，不能把本文件当现行证据。

## 目标

系统性修复 2026-07-22 五条李豆沙切片的字幕真值、长程语境、边界、候选状态、量化评分、
标题、封面和最终产物审计；用当前能力在隔离 recovery base 重跑、复核并覆盖本地旧审片包。
已发稿只允许同 BV 修复，不新建重复 BV。

## 已实现并通过本地验收（部署/成片仍须 live readback）

- stable candidate ID 后再执行严格 reviewed calibration；无效/漂移资产 fail closed；
- exact contract closure 与统一 terminal projection，candidate 不再同时出现在成品和候补；
- 未截断 60k 整片 context、完整送审而不再截到 12k 的 18k cue-aware supplemental prompt、
  时间采样聊天、topic graph scoped candidates 与 prompt 重渲染绑定；超预算 fail closed；
- correction pass `final-review-audit.v1` 与精确最终 SRT 放行回执
  `final-review-audit.v2` 分离，provider/JSON/空结构失败不再假绿；
- correction pass 的同音/近同音/字母正字法 mutation 必须有 cue/referent-bound typed textual
  authority receipt；纯声学、同片 transcript、宽泛 context/selection hook 只生成 candidate。
  exact v2 强制核对全部 applied mutation，第二遍零 finding 不能洗白无权改写；
- boundary reviewer 的 evidence 只能引用实际展示 cue；声明下一话题已分离时必须引用推荐终点
  之后的 witness。最终 snap 还须 PASS 的 `talk-boundary-final-endpoint-binding.v1` 精确绑定
  推荐 cue/ms，否则只允许 cap 内有界重审/重试或阻断；
- `boundary_repair_extend_cap_ms` 已从实际 production entry 只接入 boundary/final review；
  首轮 30 秒、受控重试最高 60 秒，架构 seam test 固定其不得误接相邻 entity-authority 调用；
- exact-final 不再用纯声学关闭同音/近同音/字母正字法 finding；结构化 SC 只有上一 cue
  exact 包含完整规范化前缀时才可去重，`0.8` fuzzy 不能吞掉极性词；
- source truth / reviewed baseline / story-chat required owner 冻结，边界只能完整覆盖或 BLOCK；
- 三条已确认字幕回归已进入 hash-bound source truth；partial structured-chat evidence 只能修复
  有声学/画面见证的槽位，不能再把整条 SC 扩写进字幕；
- required owner 只抬高合法结束下界，原 semantic/manual repair origin 与当前 spec 的有界
  repair cap 保持不变；cap 内找不到完整收束即阻断；
- package audit schema 仍为 `lidousha-review-package-audit.v2`，当前 policy epoch 已升为
  `2026-07-23.final-artifact-gates.v3`；
- 封面已使用 `lidousha-cover-rendered-text-pixels.v3`、deterministic render spec、
  committed trusted font 与 package-internal pre-overlay/mask/route-background 精确重组门；
- current talk/recovery package 的 speaker SRT 与 ASS 已改为包内双 hash，并由独立 auditor
  重放全部 Dialogue 文本、时轴和 style；
- 现行规则已收敛到 `docs/pipeline/`；根 AGENTS 与 publish skill 只保留步骤/操作入口。
- same-BV source state machine 已实现 `repair-plan / repair-run / repair-status`、hash-chain
  journal、append-at-most-once、固定 CID swap retry 和四面终态验证；专项测试与
  authorized/member API 合并测试已通过；API cookie 双形态由统一 fail-closed parser 处理，
  biliup append 使用另一个显式 top-level cookie 文件。

以上仍是共享 dirty worktree 中的本地能力；定向回归、全量测试、`git diff --check`、关键
模块编译、Markdown 链接与旧包负向 canary 都必须以本轮所有改动落地后的最新报告为准，
HANDOFF 不固化会被后续改动立即淘汰的通过项总数。旧 v8 仍应被当前 policy 阻断，不能沿用
早先某次 audit 结果。

## 当前恢复事实

- `2026-07-22-full-rerun-review` / v8 只证明旧流水线曾生成可审材料。它缺少当前
  final-review v2、required-owner、cover-pixels v3 与 epoch v3 的完整闭包，**不能视为当前
  合规，也不能直接上传或覆盖旧包**。
- 当前本地 v8 包内没有可移植的 `.speaker.srt/.speaker.ass`，五项 manifest 仍引用远端绝对
  ASS；新的 SRT→ASS 门会按预期阻断，必须随五条 recovery 重跑整包重建，不能补写 hash 假绿。
- exact talk 目标集合是 `3573, 672, 1863, 1573, 1475`；`6577` 被用户明确抑制，不允许普通
  backlog 补位。必须在新的隔离 recovery base 达成 closure COMPLETE 后再重建本地包。
- `3573/672` 继续使用 Ivan 现有 manual-title asset；`1863/1573/1475` 的同 BV 标题保留必须
  从已提交的 `authorized-upload-public-verify.v2` 生成 typed recovery public-title
  authority，禁止裸抄旧 record 或写进 Ivan 手定资产。v10 planner、record、publish draft、
  manifest 与 package audit 必须看到同一 authority 和源 receipt hash。
- 普通 7/22 state 的 source 仍不完整；恢复必须继续使用已验证的 official immutable recovery
  source，且不得覆盖普通 state/out。继续前复核当前源 SHA、磁盘、进程/锁和 target base 不存在。

## 进行中

1. 从 clean commit 部署到 `free`，读回 `DEPLOYED_COMMIT` 与实际文件 hash；
2. 在新 recovery base exact 重跑五条，closure COMPLETE 后重建 review manifest 和扁平包；
3. 对最终视频逐条复核字幕、边界、标题、封面、StoryContract 和 package audit，再覆盖本地旧包；
4. 以最终包生成同 BV dry plan；只有四面 live preflight 仍通过才执行修复并闭环验证。

## 当前约束

- same-BV 状态机已有本地测试证明，但这本身**不证明当前 production 已部署，也不证明五条
  线上稿件已经修复**。执行前必须 live 读回 `DEPLOYED_COMMIT` 与 `repair-plan --help`，并等
  五个合规最终包和真实 dry plan 就绪，才按 [pipeline/90-publish.md](pipeline/90-publish.md)
  执行；legacy append/swap/replace 入口仍禁止。
- 新 Pro 补充请求曾失败；可读的既有 Pro 回答已经用于设计，但不能把失败请求写成成功复核。

## 完成判据

当前代码通过全量与负向测试并部署读回；五条在新 base 产出同一政策字节、人工/自动复核通过、
本地旧包已覆盖；随后在当前 same-BV 状态机完成部署与真实 dry plan 后，对原 BVID 执行修复
并完成 public / public tags / Creator / section 四面验证。
