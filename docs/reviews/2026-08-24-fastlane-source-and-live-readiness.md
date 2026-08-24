# 2026-08-24 快车道来源与 live readiness 快照

本文件把 Claude 对话中的快车道命令、2026-08-19 批次裁定，以及 2026-08-24 在
`free` 上的只读核对固化为一个可复核的 source-bound ledger。它不是上传授权、不是
状态写入，也不是对未来状态的承诺；runtime、公开态和候选产物会漂移，使用前必须重新
读取 live authority。

## 1. 快车道命令的原始来源

来源文件（不复制 prompt、cookie、provider response 或其他秘密）：

`/Users/ivan/.claude/projects/-Users-ivan-Project-vtuber-slice/0df2296b-500a-4681-ab5e-6fb46dc39579.jsonl`

在该 JSONL 中直接检索到：

> 本文所有 JSONL `line` 的 SHA-256 均按**原始 JSONL line bytes（含 trailing LF）**重算；
> 不得使用去掉换行符的 hash 口径。line 947 的完整批次裁定 authority、user payload 中
> embedded `<system-reminder>` 的排除边界及语义起点，见批次裁定文档的 authority-boundary
> 注记；该 reminder 不构成 Ivan 的授权内容。

| 语义 | JSONL 位置与稳定锚点 |
|---|---|
| “今天晚上把我授权的快车道全部上传，不过优先七夕” | line 1643，`timestamp=2026-08-19T04:06:54.376Z`，`uuid=b95d4356-7ad2-4481-b4a7-0b7afa3c35b9`；整行 SHA-256 `2269c653fa6be7fb0c20df98a7348d5f5c57176e3e41fe80eb13b85c39307609`。同一命令的 queue-operation 在 line 1640，整行 SHA-256 `e5fbb4ae5d4f6e906f16095571ea1dbf44d924f4b92244950f51bf03d1a67334`。 |
| “七夕优先上传，其余快车道随后” | line 1745，`timestamp=2026-08-19T04:46:25.889Z`，`uuid=a79d6670-88b1-43c3-a688-3c9615c1da51`；整行 SHA-256 `7f97b7f8b6a7ca9cad7f54e02836a185b41c5ea158d70cb31f0867a174f13329`。同一命令的 queue-operation 在 line 1740，整行 SHA-256 `ebd218c69e614e36850c99ae6b4906ec501760687a9817d562293544c1d3ee64`。 |

这两条命令表达的是批次优先级和用户授权范围；它们不取消当前项目的最终人审、package
audit、authorized manifest、上传和公开闭环门。

同一 line 947 的候选级真值包括 Qixi：`0:14`「非常brasuki之类的日语」需交 Gemini，
`2:39`「播的有点压抑了」，并明确「这个切片时效性很强，优先修复优先上传」。同一
user payload 的全批授权原话为「以上我说的所有内容修复后都可以走快车道上传」，随后
才是「优先级顺序是我说时效性强的优先上传，然后按顺序走快车道上传」。这里仅固化
修复后快车道范围与顺序；不把原话扩张为 final-human receipt、same-BV receipt、
package completion 或已经发生的上传。

## 2. 批次 authority 与顺序

批次执行权威是 [`2026-08-19-ivan-review-batch-rulings.md`](2026-08-19-ivan-review-batch-rulings.md)。
正文候选表实际包含 21 个 candidate（18 talk、3 song；保留 `7b` 这一显式候选编号）。
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
| 1 | `2026-08-11/auto_173005_934_1166` / talk | `confirmed`: public `BV1os8q61Eya`, AID `117132650155234`, CID `41126267272`；Creator/section fresh listing 未确认 | source-fact refresh、cover recovery、public-text authority | `blocker`: ruling 要求 `主包给`，shipped SRT 仍 `主播给`；normal upload forbidden，无 human/same-BV receipt。详见 [#1–#3 dossier](2026-08-24-fastlane-1-3-source-bound.md)。 |
| 2 | `2026-08-13/auto_203011_328_389` / talk | `confirmed`: state pick `candidate_rejected/failed`；rerender receipt `QUEUED_SELECTED_REPAIR` + publication hold `hold_pending_review`；registry `hold_pending_review`；无 local BVID | selected-final-review recovery、subtitle override | `blocker`: 0:14、1:04、title/cover identity 三项 ruling 未在 current formal SRT/title/package 闭合；deployed override unrelated，provider/upload false。详见 [#1–#3 dossier](2026-08-24-fastlane-1-3-source-bound.md)。 |
| 3 | `2026-08-13/auto_220021_561_670` / talk | `confirmed`: 无 local BVID evidence；current recut SRT SHA `dfb2d77f133100f32f76c4f94dd56dae55db6ab02afdbcdcc2ba5d67dccf5704`；record subtitle hash stale/different | selected-final-review recovery、subtitle override | `blocker`: host/video 声音边界、`青兰`/`星兰` 与 `恋死→星兰` source binding 未闭合；cover downstream blocker 仍存在。详见 [#1–#3 dossier](2026-08-24-fastlane-1-3-source-bound.md)。 |
| 4 | `auto_113028_1602_1698` / talk | `confirmed`: 无当前公开 BVID；`5f90525` full-dry 一次 | operator reviewed subtitle baseline v2 | `confirmed`: `READY_TO_COMMIT`，`rc=0`，`upload_allowed=false` 仅因全局上传门；speaker `READY`、guess `null`。尚未 apply/upload。 |
| 5 | `auto_113028_1271_1328` / talk | `confirmed`: 无当前公开 BVID；`18291ae` full-dry 一次 | operator reviewed subtitle baseline v2 | `blocker`: stale binding 已越过，当前为 speaker guess requires human review；`upload_allowed=false`。 |
| 6 | `auto_120032_753_816` / talk | `confirmed`: 无当前公开 BVID；`18291ae` full-dry 一次 | reviewed baseline v2、public-text authority | `blocker`: exact delivery boundary `CONTENT_ANCHOR_NOT_COVERED` / final-boundary semantic block；`upload_allowed=false`。 |
| 7 | `auto_123036_727_785` / talk | `confirmed`: 无当前公开 BVID；`18291ae` full-dry 一次 | reviewed baseline v2 | `blocker`: stale binding 已越过，当前为 speaker guess requires human review；`upload_allowed=false`。 |
| 7b | `auto_130040_201_255` / talk | `confirmed`: 无当前公开 BVID | 未发现当前 repair authority | `blocker`: content-boundary 终审拒；需复活、重新冻结 truth。 |
| 8 | `songvis_130040_670` / song | `confirmed`: `BV1fr8P6REDP`；public + Creator + section 均 `VERIFIED_PUBLIC` | live public/Creator/section readback | `confirmed`: 已是公开态；从本轮 pending queue 移除，不重复上传。 |
| 9 | `auto_143025_1112_1285` / talk | `confirmed`: 无当前公开 BVID | 未发现当前 reviewed baseline/repair authority | `blocker`: 视频内字幕边界与灰泽修复未闭合。 |
| 10 | `auto_143025_868_1094` / talk | `confirmed`: 无当前公开 BVID | 未发现当前 reviewed baseline/repair authority | `blocker`: 应援歌/直播讲话分离及日文修复未闭合。 |
| 11 | `song_130012_1163` / song | `confirmed`: fresh public `BV1pW8E6eEq5`, AID `117120855709245`, CID `41058045650`；runtime registry 与 completed ledger 对齐 | public view + registry/ledger | `historical-public-only`: 本轮未 fresh 读取 Creator/exact section；旧公开稿不得重传。 |
| 12 | `auto_130012_435_574` / talk | `confirmed`: `BLOCKED_TERMINAL`, `REFRESH_HOOK_UNSUPPORTED`，无公开 BVID | revival/repair authority 未发现 | `blocker`: chat-authority 拒；需复活+修+发，当前不上传。 |
| 13 | `auto_123008_1017_1114` / talk | `confirmed`: current `candidate_rejected`, `rc=1`, `failure_recoverable=false`，无公开 BVID | subtitle authority unresolved | `blocker`: 人工真值须裁决 `おめでとう`（声学候选 `ありがとう`）；不得以旧“可恢复”说法替代当前终态。 |
| 14 | `auto_120026_125_253` / talk | `confirmed`: `boundary_semantic` failure，`failure_recoverable=false`，无公开 BVID；AI cover required | boundary/source-fact + cover authority 未闭合 | `blocker`: 需授权复活、边界/字幕人审及 AI cover；不上传。 |
| 15 | `auto_123655_771_844` / talk | `confirmed`: fresh public + Creator `BV133816tEiN`, AID `117138287300220`, CID `41157069221` | public/Creator readback；section 未独立 fresh read | `historical-public-only/blocker`: 旧稿不得重传；若修，只能 same-BV，需新 scope、包、人审和授权链。 |
| 16 | `auto_123655_1613_1676` / talk | `confirmed`: 无 verified BVID；当前 replacement package bytes 与 record 声明漂移 | source-fact/story-contract authority 未闭合 | `blocker`: 仅 locator；不得 review/package/transfer，先重生成 coherent package。 |
| 17 | `auto_113022_260_324` / talk | `confirmed`: `candidate_rejected/FLAGGED`, `rc=1`, `failure_recoverable=false`；replacement package bytes 与 record 漂移，无 BVID | recovery + subtitle authority 未闭合 | `blocker`: 仅 locator；需 recovery authority、重生成包及 cover/QC/人审，不得错误 transfer。 |
| 18 | `auto_143702_0_53` / talk | `confirmed`: fresh public + Creator + exact section `9320779` 一致：`BV1Pi8P6FEzS`, AID `117130183840023`, CID `41111389163`, state `0` | 三面 live readback | `historical-public-only`: 旧公开稿不得重传；新修复须另走 same-BV 链。 |
| 19 | `auto_113022_354_496` / talk/Qixi | `confirmed`: fresh public + Creator + exact section `9320779` 一致：`BV1Ud8F6fECS`, AID `117126140527747`, CID `41087534673`; Qixi queue head | successor package + pending human review | `blocker`: successor video/SRT/cover hashes已冻结但实际人审仍 `PENDING`；不得 same-BV apply/upload/宣称闭环。 |
| 20 | `song_133654_1170` / song | `confirmed`: fresh public + Creator + exact song section `9364628` 一致：`BV1Cn8E6iEf8`, AID `117120822222716`, CID `41057781970`, state `0` | 三面 live readback | `historical-public-only`: 旧公开稿不得重传；任何修复只走 same-BV。 |

## 4. Next wave

1. **Qixi**：完成真实最终播放/感知复核并生成 hash-bound human receipt；receipt 之前保持
   upload、same-BV apply 和 public closure 冻结。
2. **C4**：已在 `5f90525` 部署并完成一次 no-write full-dry，达到
   `READY_TO_COMMIT`；仍禁止绕过显式 commit lease、最终人审与上传门。**C5/C7** 当前补
   真实人审 speaker/source-fact receipt，**C6** 补 boundary 人审 receipt。禁止盲重试旧
   stage/旧 receipt。
3. **候选 #4/#5**：在独立 private stage 并行 prepare/package/QC；只产生候选私有产物，
   不写 formal state/journal，不调用上传器，等待最终标题/封面和授权 manifest。
4. **#1–#3 与 7b/#9/#10**：先按 [#1–#3 source-bound dossier](2026-08-24-fastlane-1-3-source-bound.md)
   完成 Ivan truth、exact correction 和 review receipt；之后仍须重新跑后续 package/commit/upload/public
   gates。其余候选按表顺序补齐复活 authority、song package/registry 缺口或
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

## 7. 2026-08-24 fresh live readback：#11–#20（wave evidence）

本节覆盖三组 private wave 报告的最新只读结果；`/private/tmp` 仅为本次 session evidence，
不是持久 authority，也没有因此发生 provider、state、registry、journal、upload 或 remote
写入。部署身份为 `free:/opt/bilive/autoslice` commit
`5f90525520530c9d7246d09718d209024b1181f5`；`DISABLED` 是 regular empty `0644`，
`AUTO_UPLOAD` 与 `deploy.guard` 均 absent（upload path blocked）。

- **#11**：fresh public view、runtime registry 和 completed ledger 对齐
  `BV1pW8E6eEq5` / AID `117120855709245` / CID `41058045650`；本轮 wave 未 fresh 读取
  Creator 或 exact section，因此不能过报为三面确认。既有公开稿不重传。
- **#12**：当前为 `BLOCKED_TERMINAL` / `REFRESH_HOOK_UNSUPPORTED`，无 BVID；需要有明确
  revival authority 后再修复、审包和发布。
- **#13**：当前 candidate 为 `candidate_rejected`，`rc=1`，`failure_recoverable=false`。
  `おめでとう` 与声学提出的 `ありがとう` 仍需人工真值裁决；旧的“可恢复”描述不构成当前
  authority。
- **#14**：当前在 `boundary_semantic` 失败，`failure_recoverable=false`，且 cover route
  为 `BLOCKED_AI_COVER_REQUIRED`；边界/字幕、人审和 AI cover 均未闭合。
- **#15**：fresh public + Creator 确认 `BV133816tEiN` / AID `117138287300220` /
  CID `41157069221`，但 current section membership 在该 wave 未独立 fresh read。任何后续
  修复只准 same-BV；不得 normal upload。
- **#16**：当前 replacement bytes 与 record 声明漂移：record `84516` bytes、SHA
  `eca63b1e4da1dea20c2e07ef63d1ceec8eaff52a4f351c190dad0c9c21f31104`；现 burned video
  `18147527` / `192ac163cec52dfcd8362c3eb71b0ca1c5cd93222573ce5bfa423e9d4d1f11aa`，SRT
  `1435` / `5669272b33595df688bc5ebaa329021727a37cd664e0c89821781efa5ee62ea7`，ASS
  `2423` / `027210f0ecd50d0ea37016381ffc0c3ff52ee78df1783b064a4d55e381eb1aa0`。仅作 locator，
  不得 review、package 或错误 transfer。
- **#17**：当前 replacement bytes 同样与 record 漂移：record `96998` bytes、SHA
  `a4c63eb311c651af7cb158b87c1a178ef1470a9e97e115d68624f3a4cd0aea24`；现 burned video
  `38657896` / `d81f8c0f580bce7dd9fc015e055fc7ffa3031015b3d2553500ecb8caf03cd23e`，SRT
  `1471` / `59203237381369ddcf964e8d0a503ea04cd6b4c9973f6cfeb3b53e70d80487e7`，ASS
  `2419` / `dd8167e12f5f2a20c8e50610ed89036d4fd8b83193a38ea534b9a7d9aae4be40`。仅作 locator，
  不得 review、package 或错误 transfer；先取得 recovery authority 并重生成 coherent package。
- **#18**：fresh public、Creator 与 exact talk section `9320779` 三面一致：
  `BV1Pi8P6FEzS` / AID `117130183840023` / CID `41111389163` / state `0`。这是旧公开稿，
  不是本轮新发布，不重传。
- **#19**：Qixi queue head；fresh public、Creator 与 exact talk section `9320779` 三面一致：
  `BV1Ud8F6fECS` / AID `117126140527747` / CID `41087534673`。successor exact bytes 为
  video `78,452,463` / `8539f49ecba77d12c69b51e3177f12e67d9ae39debebc762a997757e54cc488a`，
  SRT `0b712412cb2dcccb0c61dcadd0764cd37487d4c2ee4453e69824e029309052d9`，cover
  `e57c8c7af3b9346da468d54d5675319f3ace6a73760c3aa847808adbbb033642`。唯一可签署私有面为
  `/private/tmp/qixi-canonical-human-review-20260824/`；canonical root clean package audit
  `8b2dedb4acb993efa831812690596cd702444fd8648b89ea3ff50e15341b292d` `passed=true/0`，
  record `1a4e7ae28623c328cd184d4a7de870b3ad8070b520245bf334ce35ede09aeefe`、review manifest
  `659035c7e18bb1c0c3e56e2e18e38ac34d00fcf95bc3f3dafe78952559f56401`、pending template
  `023d6d3ac27b7c284a67035f9b7be3a72289c6561424e1dbc5a6be6c2390b922`。旧
  `/private/tmp/qixi-root-review-surface-20260824/` 的 record/audit/review-manifest sidecars
  `26eeeb6c…` / `1b92983e…` / `6ea8c5e1…` 是 mixed diagnostic surface，
  `REJECTED_FOR_SIGNING`，仅保留诊断，不应误称为 canonical 成品缺陷。实际 final human review
  仍为 `PENDING` template（不是 receipt），所以不得 same-BV apply、upload 或宣称修复闭环。
- **#20**：fresh public、Creator 与 exact song section `9364628` 三面一致：
  `BV1Cn8E6iEf8` / AID `117120822222716` / CID `41057781970` / state `0`。这是旧公开稿，
  不是本轮新发布，不重传；任何后续更正只走 same-BV。

结论：这些 readback 证明的是当前公开/阻塞面，不是本轮发布完成；旧公开不得重传，
`review_ready`、历史 ruling 或 pending template 均不能替代真实人审、package audit、
authorized manifest、same-BV/上传和 public/Creator/section 复核链。
