# 2026-08-24 快车道来源与 live readiness 快照

本文件把 Claude 对话中的快车道命令、2026-08-19 批次裁定，以及 2026-08-24 在
`free` 上的只读核对固化为一个可复核的 source-bound ledger。它不是上传授权、不是
状态写入，也不是对未来状态的承诺；runtime、公开态和候选产物会漂移，使用前必须重新
读取 live authority。

## 1. 快车道命令的原始来源

来源文件（不复制 prompt、cookie、provider response 或其他秘密）：

`/Users/ivan/.claude/projects/-Users-ivan-Project-vtuber-slice/0df2296b-500a-4681-ab5e-6fb46dc39579.jsonl`

在该 JSONL 中直接检索到：

| 语义 | JSONL 位置与稳定锚点 |
|---|---|
| “今天晚上把我授权的快车道全部上传，不过优先七夕” | line 1643，`timestamp=2026-08-19T04:06:54.376Z`，`uuid=b95d4356-7ad2-4481-b4a7-0b7afa3c35b9`；整行 SHA-256 `2269c653fa6be7fb0c20df98a7348d5f5c57176e3e41fe80eb13b85c39307609`。同一命令的 queue-operation 在 line 1640，整行 SHA-256 `e5fbb4ae5d4f6e906f16095571ea1dbf44d924f4b92244950f51bf03d1a67334`。 |
| “七夕优先上传，其余快车道随后” | line 1745，`timestamp=2026-08-19T04:46:25.889Z`，`uuid=a79d6670-88b1-43c3-a688-3c9615c1da51`；整行 SHA-256 `7f97b7f8b6a7ca9cad7f54e02836a185b41c5ea158d70cb31f0867a174f13329`。同一命令的 queue-operation 在 line 1740，整行 SHA-256 `ebd218c69e614e36850c99ae6b4906ec501760687a9817d562293544c1d3ee64`。 |

这两条命令表达的是批次优先级和用户授权范围；它们不取消当前项目的最终人审、package
audit、authorized manifest、上传和公开闭环门。

## 2. 批次 authority 与顺序

批次执行权威是 [`2026-08-19-ivan-review-batch-rulings.md`](2026-08-19-ivan-review-batch-rulings.md)。
正文候选表实际包含 21 个 candidate（18 talk、3 song），尽管文档标题仍写着“16 条”。
顺序固定为：Qixi candidate `auto_113022_354_496` 队首，然后按该表原始顺序；三首歌的
“可传”裁定不改变 Qixi-first 队列，也不把旧公开稿重新算成本轮完成。

提速契约见 [`2026-08-23-pipeline-speedup-source-bound.md`](2026-08-23-pipeline-speedup-source-bound.md)：
candidate-private prepare/package/QC 可在独立候选 stage 中并行，provider 受 semaphore 限流；
commit lease、formal state/journal 和 Bilibili uploader 保持短 lease/单 uploader 串行。
并行 prepare 不等于并行上传，也不改变队列顺序。

## 3. Live snapshot

Snapshot 采集基线：integration worktree
`/private/tmp/vtuber-slice-fastlane-release-20260824`，HEAD
`e008955718a4706ea1496141ff3d56205218fb5d`；free 的
`/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` 同为该 commit，读取时间为本轮盘点期间。
`/opt/bilive/autoslice/DISABLED` 存在。公开 BVID 通过 live registry/state 并用 Bilibili
public view API 交叉读取；无 BVID 不是“从未发布”的证明，只表示本次 authority 范围内未
发现当前可验证标识。

标记含义：`confirmed` 是本次实际 readback 观察；`unknown` 是没有足够当前证据；`blocker`
是已观察到的阻塞，不能靠 `review_ready`、旧 overnight report 或旧 BV 绕过。

本节最初采集于 `e008955...` pre-deploy snapshot；它保留为历史来源，C4 的当前结论以本文件
第 6 节 `5f90525` live acceptance 为准；第 5 节的 `18291ae` 仅作为历史 full-dry 记录。

| # | candidate / lane | 当前可验证状态 | baseline / repair authority | readiness 标记与最短结论 |
|---:|---|---|---|---|
| 1 | `auto_173005_934_1166` / talk | `confirmed`: public `BV1os8q61Eya`, AID `117132650155234`, CID `41126267272` | source-fact refresh、cover recovery、public-text authority | `unknown/blocker`: 旧公开稿不等于本轮修复完成；需重新冻结修复包与 same-BV 证据。 |
| 2 | `auto_203011_328_389` / talk | `confirmed`: 无当前公开 BVID | selected-final-review recovery、subtitle override | `blocker`: 拔智齿标题/字幕真值与最终 package 未闭合；需 Ivan truth。 |
| 3 | `auto_220021_561_670` / talk | `confirmed`: 无当前公开 BVID | selected-final-review recovery、subtitle override | `blocker`: 视频内人声/弹幕专名修复未闭合；需 Ivan truth。 |
| 4 | `auto_113028_1602_1698` / talk | `confirmed`: 无当前公开 BVID；`5f90525` full-dry 一次 | operator reviewed subtitle baseline v2 | `confirmed`: `READY_TO_COMMIT`，`rc=0`，`upload_allowed=false` 仅因全局上传门；speaker `READY`、guess `null`。尚未 apply/upload。 |
| 5 | `auto_113028_1271_1328` / talk | `confirmed`: 无当前公开 BVID；`18291ae` full-dry 一次 | operator reviewed subtitle baseline v2 | `blocker`: stale binding 已越过，当前为 speaker guess requires human review；`upload_allowed=false`。 |
| 6 | `auto_120032_753_816` / talk | `confirmed`: 无当前公开 BVID；`18291ae` full-dry 一次 | reviewed baseline v2、public-text authority | `blocker`: exact delivery boundary `CONTENT_ANCHOR_NOT_COVERED` / final-boundary semantic block；`upload_allowed=false`。 |
| 7 | `auto_123036_727_785` / talk | `confirmed`: 无当前公开 BVID；`18291ae` full-dry 一次 | reviewed baseline v2 | `blocker`: stale binding 已越过，当前为 speaker guess requires human review；`upload_allowed=false`。 |
| 7b | `auto_130040_201_255` / talk | `confirmed`: 无当前公开 BVID | 未发现当前 repair authority | `blocker`: content-boundary 终审拒；需复活、重新冻结 truth。 |
| 8 | `songvis_130040_670` / song | `confirmed`: `BV1fr8P6REDP`；public + Creator + section 均 `VERIFIED_PUBLIC` | live public/Creator/section readback | `confirmed`: 已是公开态；从本轮 pending queue 移除，不重复上传。 |
| 9 | `auto_143025_1112_1285` / talk | `confirmed`: 无当前公开 BVID | 未发现当前 reviewed baseline/repair authority | `blocker`: 视频内字幕边界与灰泽修复未闭合。 |
| 10 | `auto_143025_868_1094` / talk | `confirmed`: 无当前公开 BVID | 未发现当前 reviewed baseline/repair authority | `blocker`: 应援歌/直播讲话分离及日文修复未闭合。 |
| 11 | `song_130012_1163` / song | `confirmed`: public `BV1pW8E6eEq5`, AID `117120855709245`, CID `41058045650` | live registry/state/public API 一致 | `unknown`: 旧公开稿已核实；是否需要本轮新登记仍需确认，不能称 fastlane 新动作完成。 |
| 12 | `auto_130012_435_574` / talk | `confirmed`: state 有 `BLOCKED_TERMINAL`，无公开 BVID | 未发现当前 repair authority | `blocker`: chat-authority 拒；需复活+修+发。 |
| 13 | `auto_123008_1017_1114` / talk | `confirmed`: 无当前公开 BVID | 未发现当前 repair authority | `blocker`: 提督十秒需 recover+修+发。 |
| 14 | `auto_120026_125_253` / talk | `confirmed`: 无当前公开 BVID | 未发现当前 reviewed baseline/repair authority | `blocker`: 专名与后文呼应修复未闭合。 |
| 15 | `auto_123655_771_844` / talk | `confirmed`: public `BV133816tEiN`, AID `117138287300220`, CID `41157069221` | qixi/public-surface/cover authorities | `unknown/blocker`: 旧公开稿已核实；same-BV 修复闭环尚未证明。 |
| 16 | `auto_123655_1613_1676` / talk | `confirmed`: 无当前公开 BVID | operator exact title/source-fact authority | `unknown/blocker`: 仅 title authority，subtitle/package/Cover QC 未完整证明。 |
| 17 | `auto_113022_260_324` / talk | `confirmed`: state 曾 candidate_rejected/FLAGGED，无公开 BVID | 未发现当前 repair authority | `blocker`: story-contract 拒；需复活并重新冻结标题封面。 |
| 18 | `auto_143702_0_53` / talk | `confirmed`: public `BV1Pi8P6FEzS`, AID `117130183840023`, CID `41111389163` | live registry/state/public API 一致 | `unknown`: 旧公开稿已核实；不能由此证明本轮 fastlane 新动作完成。 |
| 19 | `auto_113022_354_496` / talk/Qixi | `confirmed`: public `BV1Ud8F6fECS`, AID `117126140527747`, CID `41087534673` | Qixi terminal/projection/branding/cover authorities | `blocker`: successor package 缺真实最终人审 receipt；Qixi 队首，暂不得 upload/same-BV apply。 |
| 20 | `song_133654_1170` / song | `confirmed`: public `BV1Cn8E6iEf8`, AID `117120822222716`, CID `41057781970` | live registry/state/public API 一致 | `unknown`: 旧公开稿已核实；是否需要本轮新登记仍需确认。 |

## 4. Next wave

1. **Qixi**：完成真实最终播放/感知复核并生成 hash-bound human receipt；receipt 之前保持
   upload、same-BV apply 和 public closure 冻结。
2. **C4**：已在 `5f90525` 部署并完成一次 no-write full-dry，达到
   `READY_TO_COMMIT`；仍禁止绕过显式 commit lease、最终人审与上传门。**C5/C7** 当前补
   真实人审 speaker/source-fact receipt，**C6** 补 boundary 人审 receipt。禁止盲重试旧
   stage/旧 receipt。
3. **候选 #4/#5**：在独立 private stage 并行 prepare/package/QC；只产生候选私有产物，
   不写 formal state/journal，不调用上传器，等待最终标题/封面和授权 manifest。
4. **7b/#9/#10 与其余候选**：按表顺序补齐 Ivan truth、复活 authority、song package/registry 缺口或
   same-BV 证据；对已存在 BVID 的候选重新核对当前 Creator/public/section，不把旧 BV、旧
   registry、`review_ready` 或 overnight report 当作本轮发布完成。

本文件是 2026-08-24 的观察快照；任何下一波动作开始前必须重新读取 deployed commit、候选
private artifacts、state/record/registry、上传 ledger 以及 Bilibili public/Creator/section。

## 5. 2026-08-24 deploy 与 fastlane full-dry 回写

以下是本次 source-bound readback，而不是由状态词或 worker 自报 done 推断出的完成状态。
canonical deploy 已通过完整测试并实际到达 `free`：integration commit
`18291ae669af5f22394673655691395e5555e56b`，suite 为 `6452 passed in 292.05s`；远端
`/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` 回读同一 commit（deployed
`2026-08-24T05:25:57Z`），`DISABLED` 仍为 regular empty file `0644`，`AUTO_UPLOAD`
与 `deploy.guard` 均 absent。此处只证明代码部署和运行门状态，不证明任何候选已发布。

在该 deployed commit 上，C4/C5/C6/C7 各执行一次新的 `FULL_DRY_RUN`，均为
`exit_code=2`、`status=BLOCKED`、`upload_allowed=false`；每次只读 after-image，formal
state/record/journal 未发生变化，锁均释放。精确证据如下（远端路径是当前 session 的
private evidence，不能当作持久最终 authority；后续必须把 receipt 回写到正式审计面）：

| lane | candidate | typed outcome | diagnostic receipt SHA-256 | private receipt evidence |
|---|---|---|---|---|
| C4 | `auto_113028_1602_1698` | `SOURCE_FACT_REVIEW=FAIL`: `REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW`；其余后续门未评估 | `sha256:fcb34138238272b166fca2a414e7387c2f8ed9889d9f4e74c4e6c4e11b4e49a0` | private parent receipt，raw SHA `eefa…7aa2` |
| C5 | `auto_113028_1271_1328` | 同 C4：旧 stale binding 已越过到 `SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW`，不上传 | `sha256:c10fc56a178e9cb590e7c947a14505ed6ebe429e05a51a1abc159798a414ee83` | private parent receipt，raw SHA `6d1f…fc819` |
| C6 | `auto_120032_753_816` | `EXACT_DELIVERY_BOUNDARY_REVIEW=FAIL` `CONTENT_ANCHOR_NOT_COVERED`，并有 `FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED`；到 boundary，未上传 | `sha256:1696b833d21033b7be6d21938ad8be8ca196ce06fa526269bcad9ad2fb0f5437` | private parent receipt，raw SHA `b9906bf04aea266518e9c5e8b62069c63c3ca94b235a8f3c7db9c1ec042e66e9` |
| C7 | `auto_123036_727_785` | 同 C4/C5：stale binding 已越过到 `SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW`；`state/record/journal` before/after SHA 一致，未上传 | `sha256:f72f61c1cde191fd501ac586fa5059820ffd5bcb536042a565671211357e14bc` | private parent receipt，raw SHA `adda56d8ead397862600368adc32d6a0ce7d6d9ae9d2b093599fd0b637f2863b` |

四个 private parent receipt 路径分别为：
`/opt/bilive/autoslice/private-fastlane-preflight/c4-auto_113028_1602_1698-18291ae669af5f22394673655691395e5555e56b-20260824T052944Z/`、
`/opt/bilive/autoslice/private-fastlane-preflight/c5-auto_113028_1271_1328-18291ae669af5f22394673655691395e5555e56b-20260824T052920Z/`、
`/opt/bilive/autoslice/private-fastlane-preflight/c6-auto_120032_753_816-18291ae669af5f22394673655691395e5555e56b-20260824T052920Z/`、
`/opt/bilive/autoslice/private-fastlane-preflight/c7-auto_123036_727_785-18291ae669af5f22394673655691395e5555e56b-20260824T052932Z/`。

C5/C7 的旧 stale source-fact binding 因新 successor carry 已不再是当前阻塞；对这两个候选，
当前阻塞是 speaker guess 必须由人审闭合。C6 的阻塞已推进到 exact delivery boundary，仍
需要其余人审/边界证据。C4 的 `b6e87cd` local actual-media reconciliation 已 PASS：
canonical `stage_replay` produced media SHA `0f5ee52f…6075`，stage manifest SHA
`7c58e33d…dc76`；private successor produced 20 cues，drops `8/12/13/14`，只改变
old cue 3；speaker SRT SHA `848d02…c43c`，ASS SHA `50eaa0…10f4`，manifest SHA
`e2a1fb…c2e2d`，addressee `PresentValid` SHA `e61606…f5`，formal state/record/journal
unchanged。此前 `abb211…` helper attempt 是错误路径证据，已丢弃，不代表当前状态。
上述 successor 状态是旧历史快照；随后已进入 `5f90525` canonical deploy，并由第 6 节的
C4 live full-dry readback 取代。该历史段落不构成当前 C4 阻塞或当前部署结论。

本地 human-review surfaces（均为当前 session worktree 的私有证据，状态为 `PENDING`
真实人审 receipt，不是 publication authority）：

- C5：`/private/tmp/fastlane-human-review-20260824/c5-auto_113028_1271_1328/`，模板 `review-template.v1.json`（PENDING），SHA-256 `6d37980362cf6a5aaef1be25ca19447e5e87a9f4c27c47f086202bfdfba28614`；reviewed SRT SHA-256 `9f33f247deb405b409d50f294bbfab08db7d5f43086241dd736fac0498faf64b`；raw receipt evidence SHA `6d1f…fc819`。
- C6：`/private/tmp/fastlane-human-review-20260824/c6-auto_120032_753_816/`，模板 `boundary-human-review.pending.json`（PENDING），SHA-256 `5223f013ab71323d7c244c902cb237dada1d4a968093225fec09edd69a51e525`；diagnostic raw SHA `b9906bf04aea266518e9c5e8b62069c63c3ca94b235a8f3c7db9c1ec042e66e9`。
- C7：`/private/tmp/fastlane-human-review-20260824/c7-auto_123036_727_785/`，模板 `verification/c7-human-review.pending.template.json`（PENDING），SHA-256 `467fef49f07ca44505cc7be5ca4005e29cd9a3f2ea09957fef2548efacd28633`；diagnostic raw SHA `adda56d8ead397862600368adc32d6a0ce7d6d9ae9d2b093599fd0b637f2863b`。

7b、#9、#10 仍只有 private prep/package locator，不构成 publication authority：

- 7b `auto_130040_201_255`：`/private/tmp/fastlane-prep-20260824/7b-auto_130040_201_255/`；live content-boundary failed，`BLOCKED_AI_COVER_REQUIRED`，host identity unverified，text authority pending Ivan；背景候选 `REVIEW_REQUIRED`。
- #9 `auto_143025_1112_1285`：`/private/tmp/fastlane-prep-20260824/9-auto_143025_1112_1285/`；live state failed，5400s timeout 仅为 reported evidence；缺 human video-voice truth；record 的 `artifact_hashes.subtitle_sha256` 为 `4b0402…`，当前 `recut.srt` SHA 为 `3327cb186ba44785df255c544e5e7ac1ce989a3872ccb91958a7b43ba77823c7`，存在 mismatch。
- #10 `auto_143025_868_1094`：`/private/tmp/fastlane-prep-20260824/10-auto_143025_868_1094/`；live `candidate_rejected`（`subtitle_authority` / `final_review_findings`），三项 human review pending；record video SHA `185f…` 与当前 `4fc060…` 漂移；cover subject QC `PASS`。

发布顺序不变：Qixi-first，然后按 ruling table 原序；candidate-private prepare/package/QC
可以并行，但 publication、same-BV apply 和 uploader 不能并行跳过队列。Qixi
`auto_113022_354_496` 仍需真实 human receipt，当前 successor 仍 `no upload`。

## 6. 最新 live acceptance：5f90525

以下结论以当前 integration/deployed 的 exact readback 为准，覆盖前述旧 snapshot；不是由
worker done、commit 名称或 `review_ready` 推断：

- integration 与 free deployed exact commit：
  `5f90525520530c9d7246d09718d209024b1181f5`。
- authority manifest：v1，229 entries；built-in full suite：`6482 passed in 289.57s`。
- `/opt/bilive/autoslice/DISABLED` 为 regular empty `0644`；`AUTO_UPLOAD` 与 `deploy.guard`
  均 absent；source hash readback accepted。
- C4 `auto_113028_1602_1698` 只执行一次 `FULL_DRY_RUN`，`rc=0`、
  `READY_TO_COMMIT`、`upload_allowed=false`。每个 preparation predicate 为 `PASS`，唯一
  未放行项是 `UPLOAD_ALLOWED=PASS_FALSE`；speaker 为 `READY`，guess 为 `null`。
- C4 prepared receipt：
  `sha256:9dd0bec761755bbcc737f9f3f65efd11e7cd6af661b3980caff314513f270c`；speaker manifest：
  `sha256:ca8eeb715ba09a496219ef128e582476e07f020e068773cb109bd9f2006be9e9`。
  stdout private parent：
  `/opt/bilive/autoslice/private-fastlane-preflight/c4-auto_113028_1602_1698-5f90525-20260824T073301Z`；
  stage 已清理。
- formal state/record/publish 仍保持 before/after：
  `6878305f…d7cc8f48`、`70e669e5…c376c580`、`3ba60ef3…a9f4c01e`；无 apply、upload、journal。

修复链条应紧凑理解：a870 的 C4 full-dry 仍被生产 17-cue delivery 与 20-cue release 的
successor mismatch 阻断；4e5 修复了 24→20→17 projection 并到达 speaker `READY`，随后
触发 projected binding 错误。随后发现并修复 production-shaped publish 的 nested cover
hash 校验；5f90525 的 live `READY_TO_COMMIT` 结果是该 schema 修复已在运行面得到确认。
这里不把 4e5 的分支细节写成未经独立 readback 的 exact deploy 事实。

当前已知 blocker 仍为：Qixi 缺真实最终感知人审 receipt；C5/C7 缺 speaker/source-fact
人审 receipt；C6 缺 boundary 人审 receipt；7b、#9、#10 仍为 private prep，#8 已公开并从
pending queue 移除。上述 blocker 通过后仍须重新执行后续 package、commit、upload 与
public/Creator/section readback gates；`READY_TO_COMMIT` 不是 publication 完成，也不暗示
人审通过后必然可发。
