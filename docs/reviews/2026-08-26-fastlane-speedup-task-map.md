# 快车道发布与流水线提速：任务权威、总清单和当前阶段（2026-08-26）

> 本页是当前战役的 source-bound 任务地图和阶段检查点，不是新的流水线规则，也不放宽任何发布门。
> 规则仍以 `docs/pipeline/README.md` 及其 20/30/40/60/70/80/90 step、代码和资产强制层为准。
> 运行态事实必须重新读取；历史对话只用于确认 Ivan 的目标、内容裁定和授权边界。

## 1. 总目标与完成定义

本战役同时包含三条不能互相替代的主线：

1. **快车道发布**：完成 Claude 审片 authority 中固定的 21 项（18 talk，含 7b；3 song）。每片只修 Ivan 点名的错误，未点名内容冻结；#12/#13 的“其他小错”授权只限本片。已公开项永久跳过，剩余项按原审片顺序串行发布。
2. **提速与部署修复**：保留内容/安全门不变，把 candidate-private prepare、完整 no-target-write preflight、独立 provider/证人工作和跨机器验证并行化；把 state/deploy/upload/public reconciliation 保持为短租约、CAS、单写者串行收口。
3. **执行面迁移与交付**：短期允许 free、OCI3、Colab 并行做私有准备/验证；长期从 free 迁到 OCI3，但迁移时只能有一个 authoritative writer。最终更新正确的 GitHub 项目 `MelanLee-shadow/bilibili-vtuber-clip`，通过公开导出/泄漏扫描和 CI。

完成必须同时满足：

- 21 项中每一项都有 live public VERIFIED 证据，或有 Ivan 新鲜、精确的停止裁定；不得把 `review_ready`、私有 PASS0 或 worker 报告当作公开完成。
- 所有发布只走 `scripts/authorized_upload.py`；没有重复 BVID、raw uploader/API、legacy uploader 或手工 ledger/state 修改。
- 快车道全部 named fixes 与 exact media/subtitle/title/cover hash 绑定，canonical package audit 为 PASS，state-last CAS 成功，随后做 Creator/public/section/registry/ledger fresh readback。
- 提速能力不仅有代码和单测，还在当前部署 lineage 做至少一次生产等价、无副作用验收；并发失败不能改变串行语义或扩大 provider 调用。
- OCI3 录制漂移、运行时目录、依赖、调度、数据同步和唯一写者切换闭合；free 不再是项目执行地后，不能留下双写窗口。
- GitHub 只推公开导出树；正确 remote、最新 CI、HANDOFF 和本页状态一致。

## 2. 对话权威定位

### 2.1 Claude：21 项逐片内容裁定与直接上传授权

唯一内容 authority：

- `/Users/ivan/.claude/projects/-Users-ivan-Project-vtuber-slice/0df2296b-500a-4681-ab5e-6fb46dc39579.jsonl`
- physical line 947；timestamp `2026-08-19T00:08:52.249Z`；UUID `555195ed-ec18-418d-a311-558f7e54291f`
- raw line（含 LF）SHA-256 `e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa`
- decoded content SHA-256 `0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b`

后续直接发布授权：

- line 1643：Qixi 优先、不等待人工节点、全部授权快车道随后上传；raw SHA `2269c653fa6be7fb0c20df98a7348d5f5c57176e3e41fe80eb13b85c39307609`
- line 1745：继续，Qixi 优先，其余快车道随后；raw SHA `7f97b7f8b6a7ca9cad7f54e02836a185b41c5ea158d70cb31f0867a174f13329`

已固化的 bounded 衍生文档：

- `/Users/ivan/Project/vtuber-slice-c2-reconciliation-gap/docs/reviews/2026-08-19-ivan-review-batch-rulings.md`
- `/Users/ivan/Project/vtuber-slice-c2-reconciliation-gap/docs/reviews/2026-08-24-claude-fastlane-exhaustive-tail-scan.md`

这些证据说明：technical receipt、package audit、delegated-root review 是技术交付手续，不是新的 Ivan 看片节点；但它们也不允许伪造 provider/human review 或跳过当前 pipeline 的技术门。

### 2.2 Codex：提速任务、快车道接力与职责边界

直接 source thread：

- `/Users/ivan/.codex/sessions/2026/08/23/rollout-2026-08-23T21-32-59-01a03166-3efe-7703-a8fe-633356140403.jsonl`
- Ivan `2026-08-24T01:33:53Z`：继续任务，重点是提速修复、快车道发布；root 负责规划/智力/审查，具体实现交给 Terra/Luna，最高 high。
- Ivan `2026-08-24T01:36:44Z`：流水线中值得提速并行的部分必须固化成本地文档；快车道来自 Claude 对话中的一批切片。
- Ivan `2026-08-24T12:11:27Z`：Claude 已穷举所有错误；修完点名项即可直接上传，所以叫快车道。
- Ivan `2026-08-25T05:13:51Z`：停止扩大工作面，写可恢复 handoff。

对应设计证据：

- `/Users/ivan/Project/vtuber-slice-c2-reconciliation-gap/docs/reviews/2026-08-23-pipeline-speedup-source-bound.md`
- 核心合同：P0 完整 no-target-write after-image + typed predicate matrix + sanitized receipt + readiness graph；P1 prepare/provider candidate-local 并行，state-last CAS、deploy、upload、public reconciliation 串行。
- 当前基线 focused 验证曾为 `212 passed`；这证明代码入口，不替代当前 free/OCI3/live/public 验收。

### 2.3 本 DSH 会话新增的直接 Ivan authority

1. **C3 boundary rebind**：只允许旧 34-cue grid `53788f…` 重绑定到 final 11-cue grid `3b7b513…`，endpoint 保持 `108940ms`；不改视频、字幕内容、封面或 endpoint，不授权 provider/state/deploy/upload。DSH session `session-896490c8-ae47-4531-a7d1-a0ab2d81999e`，user message `f08fd8ae-28c9-44f0-8648-21006686a8db`，canonical answer SHA `sha256:f68d837ed3c1efaae2e69f4322ecde2f3e5e0309cb055a2238e6d774b2d98546`。
2. **C9 title-cover joint QC consumption**：只对 title `334bb710…`、hook `241e2fb0…`、cover `4222118c…` 这组完全不变的精确字节，授权把 predecessor CPA redraw PASS + host-identity PASS 合成为 C9 专属 derived joint-QC consumption；不调用 provider、不改标题/封面。tool call `call_qBbVkqwbu1TfEsh1JKX7p8Xg|fc_0b3cd3bcdc4f5bfb016a8edacc2a6887d1a1bc868d5e96b950`，tool-result message `59201f5f-35ec-483a-9c63-a349f6e1d454`，seq `535090`，step `371`，turn `14`，observed `2026-08-26T12:38:49.999Z`，canonical answer SHA `sha256:6678e47d95a4a67fa834dcdcf667a44d8f8b8f16685baf1e17e8560268b9194f`。
3. **并行执行面**：当前允许同时使用 Colab、OCI3、free 做私有准备/验证，不必等待某一台；publication authority 和所有写入型收口仍只能单线串行。

## 3. 不可变执行边界

- 当前唯一 live authority：`free:/opt/bilive/autoslice`，直到有独立的 OCI3 cutover transaction 完成。
- 当前部署 commit：`981bc4abad395c2e00db7212ff8541f7ddc32ba0`。
- `/opt/bilive/autoslice/DISABLED` 必须保持 empty regular `0644`。
- 私有 prepare、测试、媒体 decode/QC、Colab/OCI3 验证可以并行；同一 candidate 的 authoritative state、deploy、package install、upload、registry/ledger reconciliation 不并行。
- free 同时只跑一个 media-producing job；Colab 上传最小显式文件集并在 verified fetch 后停 session；OCI3 不得成为第二个 publication writer。
- 不手工改 state/registry/ledger；不删未验证的源或恢复件；可恢复 offload 必须 source/destination tree seal 一致后才删 source。
- main checkout 的用户 untracked `--help.building/`、`.codex-tmp/` 不触碰。

## 4. 跨候选规则与根因任务

Claude line 947 不只授权 21 个交付件，也要求把下列规则固化到对应 authority、skill、词表或代码强制层。`PARTIAL` 表示已有候选/资产证明，但尚不能宣称对所有新产线生效；每条最终都要由当前 lineage 的代码、测试和 live canary 证明，而不是只引用历史对话。

| # | 全局任务 | 当前状态 | 与剩余候选的关系 |
|---|---|---|---|
| G1 | 看画面可知在播放视频/切片时，只做李豆沙字幕；视频内容不是交付字幕范畴；声纹/音质/音量用于防漏本人话 | `PARTIAL` | C3、C9、C10 仍是最终验收面 |
| G2 | 弹幕有梗和故意错写；一旦识别为弹幕，不主动“纠正”其字面 | `CODE/ASSET PRESENT, LIVE PARTIAL` | C1/C2 已公开；C7/C7b/C9 继续验证 |
| G3 | `；；`/“分号分号”是直播间专名，SC/弹幕中不得忽略或改成“封号” | `PRESENT`；truth ledger、C2/Qixi exact assets 已绑定 | 新候选回归仍须保留 |
| G4 | SC/弹幕引用与主播回应保持语义完整；不能硬拆，至少用空格/分隔表示说话面 | `PARTIAL` | C2 已公开；C7b/C12 的 chat authority 继续验证 |
| G5 | 未逐字念出弹幕、但直接回应，也属于弹幕修复证据 | `PARTIAL` | C1/C2 已公开；后续 regression 必须保留 |
| G6 | 念弹幕通常会念完整，除非被打断或原文即短句 | `PARTIAL` | C7b 的完整“脑控状态…”是当前 acceptance |
| G7 | 哼歌不做字幕，语义不流畅和日文哼唱是信号 | `PARTIAL` | C4/C5 已公开；C10 仍未闭合 |
| G8 | 疑似非中文交 Gemini；目标模型更新为 `gemini-3.7-flash`，先 canary 再全线 | `CANDIDATE EVIDENCE PRESENT`；Qixi 3.7/3.6 双模型 evidence 已在 lineage | C10 及新产线仍需 bounded live canary |
| G9 | 看作品时 crawler 查人物专名，不凭空猜（例：恋死→星兰） | `OPEN CURRENT AUDIT` | C3 仍未发布，且必须回答机制为何失守 |
| G10 | crawler 抓近月热点/歌/梗，不只抓 vtuber 人名 | `PARTIAL` | C5 已公开；通用热点 crawler 的 current live 行为仍要验证 |
| G11 | 排比“其实是”和后文呼应前文必须进入修复证据 | `PARTIAL` | C15 已公开；C14 的根因任务仍开着 |
| G12 | `妹感妈` 作为候选专名 | `CANDIDATE ASSET PRESENT` | C15 已公开；是否进入通用词表须避免无证据盲替 |
| G13 | 标题禁止无信息量“上头”；小李=李豆沙不能写成两人；风格遵循 Ivan 给出的世界观/信息密度标准 | `PARTIAL` | C1/C2 已公开；C10/C13/C16/C17 仍要验收 |
| G14 | “前辈”可指李豆沙戏称过去的自己 | `OPEN` | C10 source-bound 修复必须消费该语境 |
| G15 | 视频内李豆沙唱改词歌：用歌切方式与原曲对齐，只对齐节奏，歌词从屏幕读；视频外说话另做 | `OPEN DESIGN/IMPLEMENTATION`；当前 lineage 只找到裁定文档，未找到独立技能/代码闭环 | 案例视频 `BV1wF411g7YT` / 原曲 `BV1sZ4y127KT`；不得在 C10 中临时手搓不可复用逻辑 |
| G16 | 短期 free/OCI3 并行，长期全量迁 OCI3 放弃 free | `PARTIAL/BLOCKED` | 见第 5 节迁移与部署任务 |

Ivan 还要求回答 12 个“为什么没修成/为什么没入清单”问题。它们不是独立发布门，但答案必须在对应机制修复或证据中可追溯：

- C1：薇欧拉酱为何未修成；“主包给→主播给”是否是主动弹幕纠正。
- C2：标题为何把小李和李豆沙写成两个人。
- C7/C7b：冰美式/kmx 时间点误修归因，以及 kmx 为何被 `content_boundary` 排除。
- C6：从 scorecard 解释选题理由。
- C10：“arigatou”为什么在此前可处理日文的情况下仍听错。
- C11：Bonus 命名由来。
- C12：为何被 `chat_authority_final_artifact` 排除。
- C14：专名与后文呼应机制为何未生效。
- G10：crawler 是否实际抓热点。
- C19：Qixi 日语听写；已随公开件闭合。

## 5. 快车道 21 项总表

状态口径：`PUBLIC_VERIFIED` = live registry/state/public reconciliation 已确认；`PRIVATE_READY` = 私有技术闭环已完成但未部署/发布；`ACTIVE_BLOCKER` = 当前正在修的精确技术阻塞；`NOT_REOPENED` = 本战役尚未完成 source-bound 私有重放，不等于没有任务。

| # | candidate | Ivan 点名范围摘要 | 当前 live / 工程状态 | 下一道验收 |
|---|---|---|---|---|
| C1 | `2026-08-11 auto_173005_934_1166` | 薇欧拉/视频内声/弹幕原文/漏听/日语；指定标题 | `PUBLIC_VERIFIED` `BV1os8q61Eya` | 永久跳过，不重传 |
| C2 | `2026-08-13 auto_203011_328_389` | 小豆老公；；、弹幕回应、标题两人化；重做标题封面 | `PUBLIC_VERIFIED` `BV1gch36cEvN` | 永久跳过；最终公开导出时对齐 static/runtime 表述 |
| C3 | `2026-08-13 auto_220021_561_670` | 区分视频声/本人声、片尾曲不做字幕、星兰回应 | `ACTIVE_BLOCKER`；最新隔离 worktree HEAD `c4981739`，C3 相关测试 `27 passed`；canonical package/authority 输入仍缺失或 stale，不能形成 PASS0 | 补齐同一份 source-fact/clip-context authority → canonical audit 零 issue → private PASS0；不得回到逐 pointer locator 打补丁 |
| C4 | `2026-08-14 auto_113028_1602_1698` | 见面发现/日语、哼歌不做字幕、指定标题方向 | `PUBLIC_VERIFIED_SAME_BV` `BV1h7hg68E8Y`，AID `117154678638617`，current CID `41270641488` | 永久跳过，不创建第二 BVID |
| C5 | `2026-08-14 auto_113028_1271_1328` | niji 夏天/哼唱/热点 crawler | `PUBLIC_VERIFIED_SAME_BV` `BV1Sahj65ExM`，AID `117157295950921`，current CID `41267890237` | 永久跳过，不创建第二 BVID |
| C6 | `2026-08-14 auto_120032_753_816` | naruhodo ne；回答选题理由；重做标题封面 | `ACTIVE_BLOCKER`；隔离修复 HEAD `7372484a`；相关 focused `50 passed`；最终 sealed-input repeat（2 次）均越过原 `C6_EXACT_PLAN_COORDINATE_OR_IDENTITY_DRIFT`，稳定停在 `C6_EXACT_BOUNDARY_AUDIT_HASH_MISMATCH`，尚未整合/部署 | 保留 replay/stage 与 frozen-delivery split pins；authority owner 统一 record boundary hash 与 `753000..816000` coordinate frame 后，再做 provider-disabled PASS0 |
| C7 | `2026-08-14 auto_123036_727_785` | 0:21「不可以就要」（读弹幕） | `PRIVATE_READY`；deployed-lineage golden pilot `READY_TO_COMMIT`，full-dry rc `0`，all authoritative surfaces unchanged，stage empty；closure SHA `42c3b719…9b0b1` | C3/C6 后整合；apply 后对 current video `1a717d…` 做独立 Colab/OCI3 decode/QC，不能借历史不同 SHA 冒充 |
| C7b | `2026-08-14 auto_130040_201_255` | failed/content_boundary 复活；kmx/脑控完整弹幕/指定标题 | `ACTIVE_BLOCKER`；隔离 HEAD `440acaf1`，C7b/replay/finalizer focused `205 passed`；provider-disabled private full-dry 已验证六个 replay predicates PASS、`provider_callable_calls=0`、live pre/post 不变、stage 清理；provider-enabled full-dry 已越过 replay/finalizer gates，但 exact-final 停在 `FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE` | provider/JSON 依赖恢复后重跑 exact-final；在此之前不得 PRIVATE_READY、整合 deploy 或 upload |
| C8 | `2026-08-14 songvis_130040_670_11874655` | 园游会，过、可传 | `PUBLIC_VERIFIED` `BV1fr8P6REDP`，AID `117130116729073`，CID `41111260592` | 永久跳过 |
| C9 | `2026-08-15 auto_143025_1112_1285` | 视频内 vtuber 声/自带字幕全部弃掉 | `ACTIVE_BLOCKER`；source-action 66→41 cues 已穷举；Ivan 已授权 exact derived joint-QC；live row 仍 failed，successor 先前报 `C9_SUCCESSOR_CHAT_AUTHORITY_UNAVAILABLE` | 实现 hash-bound joint-QC consumption、chat successor、exact-final、failed-row state-last 路径；provider 必须保持 disabled |
| C10 | `2026-08-15 auto_143025_868_1094` | 视频内唱歌/直播说话分离、arigatou、前辈语义；重做标题封面 | `NOT_REOPENED`；live `candidate_rejected/final_review_findings`，多次 carryover/foreign-source 失败 | source-bound 收割已有外语/视频内声证据；私有 PASS0 后才进入顺序 |
| C11 | `2026-08-15 song_130012_1163` | 快乐星猫，过、可传；解释 Bonus | `PUBLIC_VERIFIED` `BV1pW8E6eEq5` | 永久跳过 |
| C12 | `2026-08-15 auto_130012_435_574` | failed/chat_authority 复活；写姐误改；片内其他小错 | `NOT_REOPENED`；live 没有 current picks，仅 superseded failed/chat-authority row；历史 artifact 不等于 current audit | exact failed-row adoption + exhaustive candidate-local text authority + package/QC/PASS0 |
| C13 | `2026-08-15 auto_123008_1017_1114` | recoverable 恢复；片内小错；指定标题 | `NOT_REOPENED`；live `candidate_rejected/foreign_source_transcription`，历史 cover/final-review blockers | 收割该片授权、恢复 foreign-source witness、标题/cover、PASS0 |
| C14 | `2026-08-17 auto_120026_125_253` | 单人不分色；kmx 多处专名；后文呼应根因 | `NOT_REOPENED`；live failed/boundary_semantic_review | 逐项 source-bound baseline 与机制根因测试；private PASS0 |
| C15 | `2026-08-17 auto_123655_771_844` | 女友感/kmx/其实是主人/妹感妈；指定标题 | `PUBLIC_VERIFIED` `BV133816tEiN` | 永久跳过 |
| C16 | `2026-08-17 auto_123655_1613_1676` | 0:19「温柔唱歌」；指定标题 | `NOT_REOPENED`；live static registry `hold_pending_review`，current picks rejected/source_fact_repair，历史 review_ready 不是 publication | 对齐 Ivan fastlane release authority 与 stale hold；source-fact/title package PASS0 后只走正式 release/upload |
| C17 | `2026-08-17 auto_113022_260_324` | story_contract 拒的优质片复活；指定标题 | `NOT_REOPENED`；live `candidate_rejected/final_review_findings`，后续 source_fact/foreign-source failures | exact failed-row adoption、title/story authority、private PASS0 |
| C18 | `2026-08-17 auto_143702_0_53` | 内容过；重做封面标题 | `PUBLIC_VERIFIED` `BV1Pi8P6FEzS` | 永久跳过 |
| C19 | `2026-08-17 auto_113022_354_496` | Qixi；日语、2:39；最优先 | `PUBLIC_VERIFIED` `BV1Ud8F6fECS` | 永久跳过 |
| C20 | `2026-08-17 song_133654_1170` | 泡沫，过、可传 | `PUBLIC_VERIFIED` `BV1Cn8E6iEf8` | 永久跳过 |

当前计数：**10/21 已 PUBLIC_VERIFIED，11/21 尚未发布**。剩余串行顺序：

`C3 → C6 → C7 → C7b → C9 → C10 → C12 → C13 → C14 → C16 → C17`

若前项失败，后项仍可并行做私有准备，但不得越过既定 publication order。

## 6. 提速与部署任务总表

| 任务 | 历史/当前证据 | 当前判定 | 剩余动作 |
|---|---|---|---|
| candidate 级并行 | 生产早已有 `MAX_PARALLEL_PRODUCE=5` | `DONE`；它不是此前 57-call 慢点的解法 | 保持 provider semaphore 与资源上限 |
| microcue 独立见证 4 路并行 | commit `c1d8e97` 是 deployed `981bc4ab` 祖先 | `IMPLEMENTED` | 当前 lineage focused/full/live smoke 后保留 |
| AGY audio witness 并行预热 | 57 次原来是彼此独立的纯音频 witness，不是 CPA 判者；commit `293acae` 是 deployed 祖先；历史实测 144s→约36s | `IMPLEMENTED` | 用当前 provider/cache 版本重新 profile；失败必须静默回原串行路径 |
| 代词审计 Terra/低 effort | 12 样本、47 occurrence、四臂 188 判定结果一致；`05837bd`、`255b68e` 是 deployed 祖先 | `IMPLEMENTED_WITH_LIMITED_AB` | 当前 provider 版本做小流量 fail-closed canary，不扩大到内容判者 |
| 代词性别查表免调用 | 66 条/174 代词仅 2 可机械判，0/66 消掉整次调用；负向金丝雀曾失败 | `CLOSED_NO_GO` | 不合入约 700 行新 BLOCK 面；除非新 profile 改变结论 |
| 引文格式机械校验 | 早已是纯 Python；烧模型的是判者生成和 invalid shape 后重问 | `CLOSED_NOOP` | 不再把“机械化格式检查”冒充未完成提速项 |
| CPA 判者批量合并 | “57 次判者”前提被生产证据推翻；真正 57 次是 AGY witness。判者仍可能有独立子集，但未有当前 profile | `DEFERRED_MEASURE_FIRST` | 仅当 current trace 证明仍为主要瓶颈时，做单点缓存优先、逐项 ID、整批失败回退、真实 A/B；不得改顺序语义 |
| P0 full no-write after-image / typed matrix / sanitized receipt / readiness graph | speedup source doc + focused 212 pass；C7 已实证 `READY_TO_COMMIT` | `IMPLEMENTED, LIVE ACCEPTANCE PARTIAL` | 在 combined commit 对 C3/C6/C7b 复跑 production-equivalent no-write；全部错误一次暴露 |
| P1 prepare outside lock / provider slots / short CAS / single uploader | 当前代码入口和 focused tests 已存在 | `IMPLEMENTED, CAMPAIGN IN USE` | combined full suite；用两个 candidate-private prepare 并行 + 一个串行 commit canary 验证无竞态 |
| autoslice-only deploy 不因 unchanged adapter 等直播 | 当前 deploy 脚本有 `external_payload_unchanged_safe`，全外部 payload 相同则不碰 adapter；早期修复不是放宽内容门 | `CODE_PRESENT, LIVE STREAMING CANARY MISSING` | 在不改 adapter 的真实部署窗口做 no-op/auto-only canary；外部 payload drift 时仍 fail-closed |
| AGY 超时与 Gemini paid fallback | commit `90a29c3` 是 deployed 祖先；用户要求 AGY 快时不要无谓超时、失败有 Gemini 兜底 | `CODE_PRESENT` | 当前 key/provider 版本做 bounded canary；确认只在允许的最后层触发且 receipt 可审计 |
| free 磁盘与历史 scratch | historical runtime 已用 topology-aware seal offload；另有 7 个无引用 stale quarantine/tmp roots 完整 offload 后删除，post-delete audit 仍零引用/零 FD，protected/reference-bearing trees 保留 | `IMPROVED, STILL BELOW GATE`；free available `7301038080` bytes（约 7.30 GB），尚未达到 8–10 GiB | 只继续处理可恢复、非权威、无活跃进程且有完整 destination seal 的 exact allowlist；恢复至少 8–10 GiB headroom 后才媒体/部署 |
| OCI3 开发/验证机 | repo `23711fd4` clean；project `.venv` Python 3.13.5；111 GiB free | `PARTIAL` | 同步 combined commit；先作为测试/media verify surface，不写 publication authority |
| OCI3 录制与最终迁移 | `/opt/bilive/recording/status.json` 当前 `service_reachable=false`，source disposition drift；`/opt/bilive/autoslice` 无 repo/state/out | `BLOCKED` | 修 recorder drift、全套 protocol acceptance、同步 runtime/data、设计 create-only cutover；最后 maintenance window 内 free→OCI3 唯一写者切换 |
| Colab 并行 offload | 当前 active sessions = 0；C3 媒体 validation 已跑通并 verified fetch/stop | `AVAILABLE` | C7 current apply 后、C3/C6 新媒体需要时上传最小显式文件做 decode/QC；每次 verified fetch 后 stop |
| GitHub/CI | correct origin `MelanLee-shadow/bilibili-vtuber-clip`；最近 main CI success 是 `94ae575`（2026-08-19） | `PENDING_CURRENT_EXPORT` | combined private closure 后走 OSS/export allowlist、泄漏扫描、push；验证新 CI，不把 private authority/session/receipt 泄露到公开树 |

## 7. 当前运行态检查点

读取时间：2026-08-26T17:38Z 本战役检查。

### free（唯一 live authority）

- deployed `981bc4abad395c2e00db7212ff8541f7ddc32ba0`
- `DISABLED`: empty regular `0644`
- filesystem：`394G / 394G`，available `7301038080` bytes（约 7.30 GB），99%；仍低于新媒体生产/部署准入所需的 8–10 GiB headroom
- recorder direct status：`service_reachable=true`、`streaming=false`、`recording=false`；历史 runtime report 仍记录 `19 closed recording(s) failed finalization`，尚未重新审计
- 未观察到 `free_session_autoslice`、`deploy_free_autoslice`、`authorized_upload.py` mutation process
- live SHA：static registry `503172e8…`；runtime registry `a197a19a…`；upload ledger `35b86573…`

### OCI3

- `/home/ubuntu/vtuber-slice` HEAD `23711fd419aff5da21b39b620b18f742e4ea4e72`，clean
- system Python 3.10.12；项目 `.venv` Python 3.13.5
- 111 GiB free
- `/opt/bilive/autoslice/DISABLED` empty regular `0644`，但无 runtime `repo/`、`state/`；不是 publication host
- recorder 当前 `service_reachable=false`、`streaming=false`、`recording=false`，因 2026-08-18 source disposition fingerprint drift
- 可恢复 offload 已存在于 `/home/ubuntu/private-offload/`；runtime seal `0600`，含 topology-aware historical runtime 与 cleanup bundle；它们不是 runtime authority

### Colab

- `mighty-colab ... sessions` 返回 0 active sessions
- 当前没有悬挂 C3/C7 session；可随时按最小文件集重新开验证任务

## 8. 当前阶段判断

本战役不是“已经做到发布，只差点按钮”，也不是“还在重新审片”。它处于：

> **Authority inventory 已完成；private repair/PASS0 阶段进行中；combined integration、single deploy 和剩余 11 项 publication 尚未开始。**

分阶段：

| Phase | 定义 | 状态 |
|---|---|---|
| A0 | Claude/Codex/DSH authority、21 项、公开防重、并行边界固化 | `COMPLETE`（本页） |
| A1 | C3/C6/C7/C7b/C9 私有 repair、canonical audit、PASS0 | `IN_PROGRESS`；C7 ready，C3/C6/C7b/C9 各有上表精确 blocker |
| A2 | 把 APPROVE 的 commits 合到单一 B1 branch，focused + full suite + diff/architecture/security review | `NOT_STARTED` |
| A3 | 一次 canonical deploy 到 free，production-equivalent private canary | `NOT_STARTED`；先恢复磁盘 |
| A4 | 按 C3→…→C17 串行 apply/upload/readback，跳过 10 个 public 项 | `NOT_STARTED` |
| A5 | OCI3 recorder/runtime 迁移与唯一写者 cutover | `PARTIAL/BLOCKED` |
| A6 | OSS/GitHub export、push、CI、HANDOFF、最终 independent review | `NOT_STARTED` |

## 9. 接下来按此顺序执行

1. **恢复 free 可用空间**：继续只处理可恢复历史 scratch，目标至少 8–10 GiB；验证 OCI3 destination tree seal 后再删 source。
2. **C3**：消费 `f5d04382` 的新 boundary authority，定位 source-fact receipt stale 的 exact role/hash，取得 canonical audit PASS0。
3. **C6**：保留 `7372484a` 的 plan/stage split-pin 修复；当前 provider-disabled overlay 已越过 identity drift，但停在 boundary hash/coordinate closure，不能继续放宽门或整合，需同一 provenance 重封 authority/record。
4. **C7b/C9 并行私有收口**：C7b second strong review + full-dry；C9 实现本会话授权的 exact derived joint-QC 与 chat/exact-final/state-last 路径。C7 保留 READY 状态并在 current artifact 出现后 offload decode/QC。
5. **整合与一次部署**：只 cherry-pick 通过 strong review 的 commits；跑 focused + full suite、architecture/debt gate、authority drift tests；root 批准一次 deploy。
6. **串行发布**：严格按剩余顺序；每一项完成 public/Creator/section/registry/ledger readback 后才推进下一项；上传失败不得自动创建第二 BVID。
7. **继续 C10/C12/C13/C14/C16/C17**：可在前项发布期间并行做 source-bound private preparation，但不越序公开。
8. **迁 OCI3**：先修 recorder drift，再建完整 runtime 和 protocol acceptance；maintenance cutover 后 free 降为只读/退役，绝不双写。
9. **公开交付**：运行公共导出与泄漏扫描，推 `MelanLee-shadow/bilibili-vtuber-clip`，等待新 CI green；更新 HANDOFF 与本页最终状态。
