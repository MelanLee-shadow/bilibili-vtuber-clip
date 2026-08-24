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

| # | candidate / lane | 当前可验证状态 | baseline / repair authority | readiness 标记与最短结论 |
|---:|---|---|---|---|
| 1 | `auto_173005_934_1166` / talk | `confirmed`: public `BV1os8q61Eya`, AID `117132650155234`, CID `41126267272` | source-fact refresh、cover recovery、public-text authority | `unknown/blocker`: 旧公开稿不等于本轮修复完成；需重新冻结修复包与 same-BV 证据。 |
| 2 | `auto_203011_328_389` / talk | `confirmed`: 无当前公开 BVID | selected-final-review recovery、subtitle override | `blocker`: 拔智齿标题/字幕真值与最终 package 未闭合；需 Ivan truth。 |
| 3 | `auto_220021_561_670` / talk | `confirmed`: 无当前公开 BVID | selected-final-review recovery、subtitle override | `blocker`: 视频内人声/弹幕专名修复未闭合；需 Ivan truth。 |
| 4 | `auto_113028_1602_1698` / talk | `confirmed`: 无当前公开 BVID | operator reviewed subtitle baseline v2 | `unknown`: baseline 存在；标题/封面最终 authority 与 QC 未闭合。可进入 candidate-private prepare，不能上传。 |
| 5 | `auto_113028_1271_1328` / talk | `confirmed`: 无当前公开 BVID | operator reviewed subtitle baseline v2 | `unknown`: baseline 存在；标题/封面及 crawler 证据未闭合。可进入 candidate-private prepare，不能上传。 |
| 6 | `auto_120032_753_816` / talk | `confirmed`: 无当前公开 BVID | reviewed baseline v2、public-text authority | `blocker`: 新部署 full-dry 观察到 `SOURCE_FACT_REVIEW_MISSING`；等 code fix 后重验。 |
| 7 | `auto_123036_727_785` / talk | `confirmed`: 无当前公开 BVID | reviewed baseline v2 | `blocker`: 新部署 full-dry 观察到 source-fact binding stale/missing；等 code/state fix。 |
| 7b | `auto_130040_201_255` / talk | `confirmed`: 无当前公开 BVID | 未发现当前 repair authority | `blocker`: content-boundary 终审拒；需复活、重新冻结 truth。 |
| 8 | `songvis_130040_670` / song | `confirmed`: 未发现当前 registry/public BVID | 未发现当前 publication/repair authority | `unknown/blocker`: 裁定为“过，可传”，但本次未证明 package、title-cover QC、manifest 闭合。 |
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
2. **C6/C7**：分别修复 source-fact review 缺失与 stale binding；重新 deploy 后各做一次新的
   candidate-private full-dry，禁止盲重试旧 stage/旧 receipt。
3. **候选 #4/#5**：在独立 private stage 并行 prepare/package/QC；只产生候选私有产物，
   不写 formal state/journal，不调用上传器，等待最终标题/封面和授权 manifest。
4. **其余候选**：按表顺序补齐 Ivan truth、复活 authority、song package/registry 缺口或
   same-BV 证据；对已存在 BVID 的候选重新核对当前 Creator/public/section，不把旧 BV、旧
   registry、`review_ready` 或 overnight report 当作本轮发布完成。

本文件是 2026-08-24 的观察快照；任何下一波动作开始前必须重新读取 deployed commit、候选
private artifacts、state/record/registry、上传 ledger 以及 Bilibili public/Creator/section。
