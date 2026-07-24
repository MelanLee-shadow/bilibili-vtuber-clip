# Current handoff

Updated: 2026-07-23

> 只记录尚未完成的当前任务与恢复点。流水线规则见
> [pipeline/README.md](pipeline/README.md)；这里的 runtime/公开状态在继续操作前仍须 live
> readback，不能把本文件当现行证据。

## 目标

系统性修复 2026-07-22 五条李豆沙切片的字幕真值、长程语境、边界、候选状态、量化评分、
标题、封面和最终产物审计；用当前能力在隔离 recovery base 重跑、复核并覆盖本地旧审片包。
已发稿只允许同 BV 修复，不新建重复 BV。

## 已落地或正在收敛的本地能力（最终验收/部署/成片仍须 live readback）

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
- boundary semantic review 已拆成两层：`source_full_window` 保留 endpoint 后 cue，给
  resolver 提供 source 侧 closure/下一话题证据；`_materialize_final_recut` 后再从包内精确
  SRT 重跑 `final_delivery`，按最终字幕重新判断 syntax/story，并以 hash-bound source
  separation witness 继承 post-end 证明。两层 request/grid/ordinal/坐标分别绑定，不能要求
  SHA 相等，也不能把 source 回执平移成最终回执；final-review v2 另以原始 SRT bytes SHA
  绑定。语义 closure cue end 与媒体 delivery lower bound 已分型；下界只可由同一 closure
  cue 后实际落地的 400ms 尾气覆盖，尾气被 VAD/下一 cue 钳短仍硬阻断；
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
- final perceptual review receipt 已与机器 audit 分层：same-BV 必须绑定 committed exact
  review contract、package evidence、record/title/publication target、最终 video/SRT/cover、
  每候选 exact points 与八类带具体 evidence 的检查；任一漂移会在 manifest、plan 和 resume
  各层阻断。cover claims 只接受 record StoryContract authority 的 exact 集合。它不改变
  `upload_allowed=false`，也不授权新 BV；
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
- 隔离 v10
  `/opt/bilive/autoslice/recovery/2026-07-22/full-rerun-v10-final-artifact-v6`
  已在真实 production path 复现两个确定性通病后主动终止，未生成可交付整包且没有上传，
  **不得恢复、续跑、复制其回执或把它当作 v11 输入**：
  `3573` 的 reviewer 正确选择 cue 46 / `102610ms`，但旧 resolver 把媒体覆盖下界
  `103010ms` 错当成必须吞下一 cue 的语义下界；`672` 的 source full-window 回执使用 ordinal
  131，materialize 后三条 cue 被删除、最终 delivery ordinal 变为 128，旧链却把 source 回执
  继续当最终交付证明。扩到 30/60 秒都不能解决，不能放宽 endpoint binding。终止后已确认
  v10 专属进程组退出、target/global lock 可取得；磁盘余量上次观测约 19GB，重跑前必须 live
  复核。
- 针对上述 v10 证据的双层回执/raw-byte/package 补丁仍在共享 dirty worktree 收敛；先前
  `2134 passed` 发生在 fresh reviewer 揭示“materialize 后仍会改 cue”之前，**不是当前补丁的
  最终验收数**。必须以最终定向/全量回归、fresh review、clean commit 和部署读回为准。
- 下一次真实重跑必须新建 v11 隔离 base
  `/opt/bilive/autoslice/recovery/2026-07-22/full-rerun-v11-boundary-grid-final`；创建前确认路径
  不存在、源 SHA/磁盘/进程/锁仍健康。不得复用 v10 state/out/receipt。
- 本轮五条封面都锁定 `screenshot_direct`，不是因为 AI 功能未部署：`3573` 的源帧同时给出
  男性游戏角色与李豆沙/南町，`672/1863/1573` 给出联动双方与互动情绪，`1475` 还给出双人大笑
  和“别管，先弹了再说”弹幕。未直接出现在源帧中的“不熟”反转、对质/被收集、火锅/霸凌、
  大椅子和脑瓜崩动作只由封面文字/版式表达；不得把这些叙事写成 source-visible claim。
- 本轮 Ivan 已明确委托 Codex 在最新流水线重跑后自行 review，有信心时权宜上传；若实际由
  root 完成最终五片观看，receipt 必须如实写
  `reviewer_kind=delegated_root_agent`、`reviewed_by="Codex root"`，并原样保存
  `approval_quote="你在自己用修复的流水线过了一遍，自己review并修改后觉得有信心了之后可以权宜上传"`。
  只有 root 真的逐片完成 contract 中全部 points 后才能签出；不得写 Ivan 已人工观看。
- 672 的最终 perceptual contract 特别分开两点：0:13 附近是“前半无声、后半有真实语音，
  全段从未说我草”；1:48 附近是“整条 L 问句没有说话并须完全删除”。两点必须分别留下
  final-video evidence，不能以一个泛化 `silence_hallucination=PASS` 代替。
- 五条统一从受管部署的
  `assets/lidousha/recovery_publication_authority.v1.json` 生成 exact publication
  authority map：`3573/672` 的模式是现有 Ivan manual-title 正文，`1863/1573/1475` 的模式
  是已验证 public title 原样保留。当前 asset SHA 是
  `sha256:ae15fbfd2b72cbb577fcdda66f94bb2108b79dfb0954f6649bc775ef2e8a6118`；
  它还逐项冻结五个 Ivan-reviewed `required_given_end_ms`。v11 planner 必须以该 SHA 覆盖
  完整五项并从资产派生 end，plan schema 为 v7；record、publish draft、review/authorized
  manifest、package audit 与 same-BV repair plan 必须看到同一 authority。repair CLI 的 BVID
  还须在任何 adapter/observe 前与 authority 相等，live AID/CID 再与 authority 精确核对。
- 普通 7/22 state 的 source 仍不完整；恢复必须继续使用已验证的 official immutable recovery
  source，且不得覆盖普通 state/out。继续前复核当前源 SHA、磁盘、进程/锁和 target base 不存在。

## 进行中

1. 收敛双层边界回执与 raw-byte/package 绑定，完成定向/全量/负向测试及 fresh review 后提交；
2. 从该 clean commit 部署到 `free`，读回 `DEPLOYED_COMMIT` 与实际文件 hash；
3. 在全新 v11 recovery base exact 重跑五条，closure COMPLETE 后重建 review manifest 和扁平包；
4. 对最终视频逐条复核字幕、边界、标题、封面、StoryContract 和 package audit，再覆盖本地旧包；
5. 以最终包生成同 BV dry plan；只有四面 live preflight 仍通过才执行修复并闭环验证。

## 当前约束

- same-BV 状态机已有本地测试证明，但这本身**不证明当前 production 已部署，也不证明五条
  线上稿件已经修复**。执行前必须 live 读回 `DEPLOYED_COMMIT` 与 `repair-plan --help`，并等
  五个合规最终包和真实 dry plan 就绪，才按 [pipeline/90-publish.md](pipeline/90-publish.md)
  执行；legacy append/swap/replace 入口仍禁止。
- 新 Pro 补充请求曾失败；可读的既有 Pro 回答已经用于设计，但不能把失败请求写成成功复核。

## 完成判据

当前代码通过全量与负向测试并部署读回；五条在新 base 产出同一政策字节，current machine
package audit 与由实际 reviewer 完成的 final perceptual review receipt 两门均通过，本地旧包
已覆盖；随后在当前 same-BV 状态机完成部署与真实 dry plan 后，对原 BVID 执行修复并完成
public / public tags / Creator / section 四面验证。
