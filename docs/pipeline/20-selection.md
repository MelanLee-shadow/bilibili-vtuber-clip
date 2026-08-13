# 20 候选召回与选题

本文件是选题步骤的**分步权威**。metric 细则的强权威是
`assets/lidousha/slice_selection_metric.md`（当前 v5）。模型只给档位和 cue 证据；
`src/autoslice/selection_scorecard.py` 负责 Tier 准入、固定算术、绝对分校准与最终排序，
禁止用 confidence 代替价值分。可执行校准资产是
`assets/lidousha/selection_score_calibration.v1.json`；profile 未注册、资产漂移、锚点越界
或锚点顺序颠倒都必须 fail closed，不能只在 prompt 里写“应该大约多少分”。
候选必须先由最终 resolved start/end 生成稳定 `candidate_id`，再以该 ID 查校准锚点；
禁止拿临时召回 ID 校准后再改名，否则同一内容会在重跑时随机失去 reviewed anchor。

## 链路

1. 语义召回为主：`select_semantic_session_candidates`（`src/autoslice/semantic_candidate_selector.py`），关键词只兜底（Ivan 2026-07-03）。
   - 单段超过 45 分钟时，必须按 30 分钟核心窗 + 前后各 2 分钟上下文重叠分别召回，再在整段范围按信心分去重排序；禁止把 2 小时字幕塞进一次调用后，把模型只返回前半场少数候选误当作整场无内容。
   - 每段候选池上限 12 是召回余量，不是交付配额；最终仍按每场 talk top-5 上限、scorecard 硬 Tier 与最低信心门筛选，不为凑数降门槛。
   - 已选候选若被边界、说话人或字幕 authority 的确定性安全门拒绝，保留拒绝记录但立即从已排序 backlog 补位；只有 provider/运行时等可恢复故障才占位等待，不能因一个不可交付候选把全场最终数量永久压低。
   - 上述补位只适用于普通生产。`RECOVERY_REVIEW` 若绑定
     `talk-selection-contract.v1 / EXACT_CANDIDATE_SET_NO_BACKFILL`，候选集合本身就是
     人工 authority：任一条失败必须保留原槽位为失败，不得从 backlog 偷换成另一条。
   - profile `upload_tag_policy.v2 / important_content_ips` 是高显著 IP/节目专名白名单；语义
     召回 prompt 从完整 cue 文本确定性列出命中的 canonical 名，首项为「战斗吧歌姬」。它只
     是 `audience_salience` / hook 选材信号，不自动授予 Tier/分数、不要求 hook 机械插词；
     偶然提及可忽略，最终 scorecard 仍由候选窗内证据和固定代码复算。
2. 弹幕证据分两层：`danmaku_evidence.py` 的爆发窗口只作热度 hints；语义召回另从
   当前 segment 的 XML 在每个 cue/shard 窗内构造有界 `request/question → reaction`
   互动链。后者只允许明确提问、动作要求或长文本重复开启链，reaction 不能独立充当
   trigger；打 call、唱歌欢呼和主播名应援不得仅凭重复量制造选题。证据必须携带
   `algorithm_id + policy/source/evidence SHA-256`，并受链数、反应组数和文本长度硬帽；
   XML 缺失或不可解析必须在 diagnostics 明示，不能退回“前三条弹幕样本”后自称完整。
3. CPA 观众视角审查：每个候选无条件过 `scripts/cpa_semantic_qa_llm.py` 判官（`viewer_context_ok` 语境自足性 + 自动扩窗建议），失败即 BLOCK（`live_source_review.py::_merge_cpa_semantic_review_into_decision`）。
4. 候选是内容锚点不是最终边界；边界由 [30-boundary.md](30-boundary.md) 决定。

## 同日场级配额

- 配额按候选的**源 segment 场**记账，不按 recording session 日期桶直接合并。
  `segment-scene-context.v1` 把源文件 stat、录制 metadata/XML 标题和 ffprobe
  `width/height/orientation` 耐久化到 state；缓存输入指纹漂移时重探，UNKNOWN 也在后续
  tick 重试但本 tick 仍按杂谈。事件场必须同时命中
  3D/生日/周年类标题语境且为横屏，竖屏永远是杂谈场；标题、探针缺失/不可读或方向
  unknown 都 fail closed 到杂谈场，不得静默放宽。
- 普通生产在 exact contract 短路之后，互斥优先级为
  `RESOLVED game > event > talk`，三类政策不叠加：游戏场保留既有 `20 / 第 6 席起 >=85`；
  事件场为 `15 / 第 6 席起 >=85`；杂谈场为默认 `5`。同一坍缩 session 内分别使用
  `game:`、`event:`、`talk:` scope，因此同日事件场的 15 席与杂谈场的 5 席独立计数。
- 裁定出处（Ivan 2026-08-09，裁定失落案重申）：「我记得我当时说过 88 这个 3D live 场
  放宽到 15 个,然后当天的杂谈场认为是独立的,自然有 5 个」。实现见
  `src/autoslice/segment_scene_context.py`、`src/autoslice/talk_quota_policy.py` 和
  `src/autoslice/candidate_selection.py`。

场次联动关系的现行 authority 是
`src/autoslice/session_relation_authority.py` +
`assets/lidousha/session_relation_ledger.v1.json`：它以日期/官方源 SHA/参与者绑定关系，
同时进入 selection、clip-context、StoryContract 与封面参与者门。开发旁路或历史报告中的
`NO_TRIGGER` 只表示该旁路没有触发，**不等于非联动**，也不能覆盖 ledger 的 `CONFIRMED`。

## 候选状态与人工点选

- `picks`、`pending_talk`、`talk_backlog`、拒绝记录必须互斥投影；一个 candidate
  只能处于 `CURRENT`、`PENDING`、`PENDING_COVER`、`FAILURE`、`MISSING` 或
  `OUTSIDE_EXACT_CONTRACT` 之一。已经成为 `CURRENT + COMPLIANT` 成品或终态拒绝的
  candidate 不得再次出现在“当前候补”。
  `not_selected` 是历史 prose，不是状态 authority，也不得参与补位。
- 每次拒绝必须保留 `failure_stage + rejection_reason + failure_evidence`；报告把它放在
  “候选门禁拒绝”，不能混进成品表只显示一个无解释的 `candidate_rejected`。
- 已标记 `selected_repair=true` 的字幕 authority 修复项若在
  `chat_authority_finalization` 被补位成 `candidate_rejected`，不能永久失联：只有字幕
  authority 专属 fingerprint（text pipeline、source truth、chat proposal、glossary 资产）
  发生变化时才自动恢复原候选，并可越过旧 lifetime 计数获得一次新代码尝试；无相关变化、
  普通拒绝或手写伪状态仍不得复活。
- 用户点名候补不篡改分数：追加 `USER_SELECTION_OVERRIDE`，记录原始 scorecard、
  baseline rank、实际 slot、被越过的基线候选与人工 authority。用户说外部已有重复但
  没有 BV 时，可直接 `SUPPRESSED_BY_USER`，但重复 claim 只能是
  `USER_ASSERTED_UNVERIFIED`，不得伪装成已验证站外重复。
- 用户指出某候选可能与已发布稿同题时，必须使用 deploy-sealed、candidate-scoped 的
  published-topic review authority 绑定候选与已发布 registry 行、两边 hook/scorecard、
  BVID 和公开标题。它只能把候选移入 `HUMAN_TOPIC_DEDUP_REVIEW`，不得自动宣称重复、
  改分、删除或授权上传；任一绑定漂移时保持 stale review hold，不能静默放行。
  若 Ivan 已完成同题人工审阅并决定继续生产，必须另有 deploy-sealed、candidate-scoped 的
  `published-topic-dedup-resolution.v1`，同时绑定原 authority 的 raw bytes/self-seal 和同一
  候选/已发稿/registry/public-title 事实；它只能解除该候选的 selection hold，且
  `upload_authorized=false`。resolution、原 authority 或任一当前绑定漂移时仍保持 stale hold；
  新投稿仍须单独通过当前 package audit、联合 QC 与 authorized-upload manifest。
  若人工判断后，候选又经 `semantic-evidence-scorecard-refresh-receipt.v1 / REFRESHED`
  合法更新 scorecard，旧 v1 resolution 保留作原始判断证据，不得改写；另加同候选的
  `published-topic-dedup-resolution.v2`，把旧 authority scorecard 与当前 scorecard 通过该
  refresh receipt 的 self-seal、old→new、候选窗口/路径、BCUT/XML/弹幕证据、provider
  contract 和完整 input-provenance SHA 严格串起。v2 只准该候选恢复生产；receipt、hook、
  窗口、日期、scene、源证据、authority、registry/BVID/public-title 任一漂移都继续 sticky
  stale，且 `upload_authorized=false` 不变。
- 已有上述 v2 resolution 的单条停泊候选，只能由严格
  `operator-processing-scope-grant.v5 / RECOVER_NAMED_RESOLVED_TOPIC_DEDUP_HOLD`
  恢复：grant 必须恰好点名一个 CID、`upload_allowed=false`，且硬到期必须先于任何
  repository/receipt probe 生效。canonical probe 只有五态：`READY_TO_RELEASE` 才允许
  maintenance 首次且仅首次移出原 hold，并原子写入 durable、`upload_authorized=false`
  的 candidate-keyed release-marker ledger；ledger、entries 和每条 marker 都必须 exact-field
  校验并 self-seal。之后只有目标 marker 与原 hold/queue origin、当前排队行、v2 resolution
  raw bytes/self-seal、原 authority raw bytes/self-seal 及 refresh receipt old→new 全部精确
  一致，才算 `RELEASED_QUEUED`；可恢复 pick/封面待补期间是
  `RELEASED_RETRY_PENDING`，另有 `CONVERGED` 与 `BLOCKED`。任意普通队列行不能伪装成已释放；
  marker、行或任一绑定
  漂移都 `BLOCKED`，整块 scope fail closed 为空且不得再次 release；已有 entry 仍 active 时
  不得并行 release 另一候选。点名候选进入严格绑定的当前 Talk 终态后才 `CONVERGED`；旧
  terminal entry 必须留在 ledger 作历史证据，但不得阻塞同日之后另一候选的串行恢复。整个
  tick 冻结这一 CID 的 Talk allowlist，未点名 Talk 必须
  隔离并原样归还；`pending_song`、`song_backlog`、`song_selection_backlog`、`songs`、
  `song_superseded_attempts` 五个 Song 集合也必须逐项深等值保留，不得借 v5 发现、补位、
  恢复或生产。
  marker 的 current-row head 必须绑定完整行 SHA，并按封闭状态机前进：session annotation
  只准在同 collection 的当前行上改其专属字段；scorecard refresh、排序/说话人路由等只准在
  各自字段白名单内写 self-sealed queue→queue rebound；真正生产必须在状态落盘前写 exact
  queue→pick transition。未经该 transition 的同
  CID/同窗口 pick（即使自称 recoverable）一律 `BLOCKED`。产出成为
  `failure_recoverable=true` 的 typed failed pick 时属于 `RELEASED_RETRY_PENDING`，不是终态；
  v5 不走会原地多次持久化 pick 的 cover-only repair，`media_ready_cover_pending` 必须由受控
  重排/重产路径继续。
  generic recovery 真把 failed pick 重排进 Talk 队列后，必须在同一事务追加 self-sealed
  transition，绑定前一 head、完整 failed-pick SHA、新队列行 SHA 与该行的
  `recovery_source_record_sha256`，再原子推进 head；转换不唯一、身份漂移或写 ledger 失败都回滚
  并记 typed runtime block。只有严格身份相同且明确交付/拒绝/低分归档，或显式
  `failure_recoverable=false` 的 typed failure，才可 `CONVERGED`。
- 精确恢复契约持续压住普通 backlog，直到新的人工恢复计划显式替换；普通 backlog
  在报告里只能显示为 `OUTSIDE_EXACT_CONTRACT / INELIGIBLE`，不能伪装成当前候补。
- 已由 committed publication registry 标为 `hold_pending_review` 的单条历史
  `review_ready + CURRENT + COMPLIANT` Talk，只有严格
  `operator-processing-scope-grant.v3 / RERENDER_NAMED_HELD_CURRENT_FOR_REVIEW`
  才能因候选级流水线指纹变化进入无上传审片重出。授权必须恰好点名一条并显式
  `upload_allowed=false`；tick 从入口冻结 Talk-only allowlist，确定性拒绝也不得中途恢复
  普通 Talk 补位、发现或任何 Song 工作。registry 未提交/未部署封印、hold 行不唯一、
  指纹未变、源/BCUT/chat 缺证或候选重复都 fail closed；到期则先于外部证据检查自动退出。
- 单条历史说话人人工停泊件只能由严格
  `operator-processing-scope-grant.v4 / RECOVER_NAMED_SPEAKER_MANUAL_REVIEW_HOLD`
  恢复。grant 必须恰好点名一条并显式 `upload_allowed=false`；候选必须是唯一、严格
  `speaker-manual-review-hold.v1 / PENDING_HUMAN_REVIEW`，且 `failure_kind=speaker_evidence`、
  `failure_recoverable=false`。恢复指纹不变或计算失败一律 BLOCK；指纹漂移或已经进入
  Talk 队列才保持 OUTSTANDING，只有最终交付/拒绝后 CONVERGED。scope 全程 Talk-only，
  五个 Song 集合的入口快照必须逐项深等值保留。
- `src/autoslice/candidate_selection.py::exact_talk_contract_closure` 是 exact 状态的共同
  判定器。终态验收要求：每个 contract ID 恰有一条 `rc=0 + CURRENT + COMPLIANT`
  delivery；没有 pending、failure、missing、重复/冲突记录或 outside-contract active pick。
  任一条件不成立都只能是 `recovery_incomplete`，不得保留/生成 `review_ready`。
- `src/autoslice/batch_terminal_state.py::project_terminal_batch_state` 是普通/恢复批次唯一
  终态投影器。retry 时间只是元数据，不能覆盖 exact closure；有 future retry 但 exact 集合
  未闭合时仍为 `recovery_incomplete`。`review_ready`、`review_ready_with_failures`、
  `review_ready_retry_wait`、`retry_wait` 与 `no_delivery` 只能由该投影器按当前 delivery、
  failure、cover-pending、exact closure 和 retry state 共同得出，报告层不得自行猜状态。
  只有严格晚于当前时间的 retry epoch 能产生 `*_retry_wait`；已到期的旧时间戳不能把批次
  永久伪装成“仍在等待”。
- exact contract 中的直接 gate 拒绝必须规范化为带 `failure_stage + failure_kind +
  failure_evidence + fingerprint` 的可重试失败，并标记合同禁止补位；不能留下永远唤不醒的
  `candidate_rejected`。

## 同主题合并（Ivan 2026-07-18 切片案 → 2026-07-19 新规）

- **主题一致的候选尽量合并成一个切片**，不许把同一话题在源时间轴上相邻/交错的两段切成两条成品（案例：kmx 称呼两条切片同主题被分开切）。
- 机制：候选定稿前跑主题聚合 pass——相邻候选（源时间间隔小）且主题/实体重叠高的合并为一个候选窗口，交给边界解析定最终起止。实现见 `src/autoslice/semantic_candidate_selector.py` 的 merge pass（落地记录 `merge_audit`）。
- 合并后的标题按合并主题起，不是拼接两个子标题。

## metric 硬维度（详见 metric 资产）

- 七维权重固定为 25/20/15/15/10/10/5；先验收 Tier 证据，再按有效分排序，最后才以 confidence 破同分。
- 有效分 = 固定七维 raw score − uncertainty penalty − same-session fatigue penalty。
  多样性只能在同一 Tier 内参与，不能让低 Tier 候选跨层超车。
- 围绕本人（含态度/立场/情绪，不只名字梗）；观点强度与受众兴趣（百合/GL）是硬维度；高语义分不许因 niche 压低。
- 报告必须同时显示 Tier 与有效分；缺 scorecard 的旧候选只能作为显式“未量化”候补，不能挤掉有效的 Tier 1/2。
- scorecard 的 cue 证据必须落在候选窗内；模型只填 0–4 档和证据，固定代码复算
  `raw_score`、罚分与 Tier 准入。任何手改后的算术不一致都使 scorecard 无效。
- 7/22 executable anchors：`auto_193450_3573_3665` 必须 Tier 1、有效分 75–85；
  `auto_193450_5341_5459` 必须 Tier 2、有效分 50–60；前者必须稳定高于后者。
  这两项只是量尺 canary，不构成 recovery allowlist；exact 集合只能来自当前 v7 plan 的
  selection contract。

### selection metric v2 production shadow（不改 v1）

- `src/autoslice/selection_metric_v2_shadow.py` 只在报告阶段生成独立
  `SELECTION_METRIC_V2_SHADOW.json`，并在批次 Markdown 显示 v1/v2 对照；它不得写回
  candidate row，也不在 `prioritize`、配额、发布或上传调用链上。v1 scorecard/Tier/
  effective score 仍是唯一生产选片与排序口径。
- v2 只有在 `selection_metric_v2_shadow_inputs` 中存在 candidate-scoped typed input，
  且候选 ID/边界、有效 v1 scorecard、topic fingerprint 的 schema/policy/source hash，
  以及当前 `host_occupancy` schema/estimator/threshold/config/attribution 全部匹配时才写
  `AVAILABLE`。缺任何一项都写 `UNAVAILABLE + reason_codes`；不得从 hook、scene、名义
  “单人场”、`event_key` 或旧 centrality 分猜 topic/说话人。
- 当前 semantic recall 尚未把 topic claim 映射到最终 candidate，host occupancy 也尚无
  production runner producer；因此 bridge policy 必须写
  `production_input_producer_status=NOT_WIRED`，所有候选保持 `UNAVAILABLE`。手工填入外形像
  schema/SHA 的 mapping 不得把它升级为 `AVAILABLE`；只有 topic 与 host producer 各自落地
  source/runtime/boundary-bound receipt 并同步收紧 consumer 后，才能修订该状态。
- topic fatigue 只在同一 session 的当前 Talk 候选都有 typed topic input 时计算；覆盖不全
  必须 `SESSION_TOPIC_COVERAGE_INCOMPLETE`，不能把缺证候选当作不存在。所有 candidate、
  scorecard、topic、host-occupancy 与 bridge policy 都写 canonical SHA-256 绑定。
- production 尚无 hash-bound standalone proof atoms 时，shadow 只观察 broad path；不能把
  v1 的 `level=4` 直接升级成已验证单轴 OR。v2 仍标 `provisional_calibration`，无论
  AVAILABLE、GATED、PARKED 或 UNAVAILABLE，`decision_influence / selection_authorized /
  quota_authorized / release_gate / upload_authorized` 都必须为 false。

## 历史弹幕证据评分刷新

- 语义弹幕 evidence policy 变化不会自动改写旧 `picks`：历史尝试的 scorecard 已绑定旧
  story/package 证据，禁止原地换分。`segments_done` 也不能为了重评而清空，否则普通
  discovery 会重复追加整场候选。
- 只有 operator scope 点名且仍位于 `pending_talk` / `talk_backlog` 的候选，才可在生产前
  进入 candidate-scoped evidence refresh。刷新必须以原 candidate interval、BCUT SRT、
  XML、source identity 和未变 hook 重建证据与 scorecard，保存 old/new canonical hash、
  provider request/response contract 及 typed receipt；输入、provider 或绑定失败时停在
  prioritize/生产之前，不能沿用 stale card。
- semantic discovery 与上述 refresh 都不得让长驻 runner 直接读取 CloudFS XML bytes：
  数据读取必须在独立子进程完成，前后绑定 regular-file stat、大小（硬帽 128 MiB）与
  SHA-256，再经本地非 FUSE spool 交回。单次读取 30 秒到期后父进程不得等待卡在内核的
  child；同 source key 只保留一条活跃读取、全局最多四条，后续 tick 以 typed
  timeout/active/limit receipt 停车，不重复堆 orphan。
- v2 `RECOVER_NAMED_FAILED_PICKS` 只负责让旧 failed pick 经过原 maintenance 后回到 queue；
  它本身不重评分、不放宽配额、不授权上传。候选一旦回到 queue，仍须满足上面的刷新门。
- failed pick 回到 queue 时必须逐字节保留 state annotation 已验证并绑定源 segment 的
  `segment_scene_context`，不得重新猜 scene 或丢成空值；否则同 tick 的 8/8 Event 会错误
  回落普通 Talk 配额。Event 身份保留后仍只按其日期资产执行 15 席与第 6–15 席 `>=85`
  分数门；普通 Talk 继续按独立 scope 的默认 5 席，不从 Event 借配额。
- v1（点名已排队候选）与 v2（点名可恢复失败件）都是 **Talk-only lane capability**：
  runner 在 tick 入口冻结点名 allowlist，只恢复、重评分、生产和修封面这些 Talk；
  同日既有 `pending_song` / `song_backlog` 原样保留，
  不做 Song recovery、discovery、refill 或 production。即使点名 Talk 在本 tick 内被拒绝、
  grant 随即变成 `CONVERGED`，也不能在同一 tick 回填未点名 Talk。Song 必须另有独立的
  typed authority，不能借历史 Talk scope 搭车。
- 这条窄门只修复已知候选的历史评分，不保证找回旧 recall 从未生成的候选。后者需要
  per-segment transactional rediscovery/reconciliation，不能把本门夸大成整场重新发现。

## 修正 hook 后的独立 scorecard 重评分

- 人工 source fact/专名修正若改变了 `selection_hook` 的叙事事实，原
  scorecard 就是 stale；不得沿用旧分、手改算术字段，或只重跑标题/封面后将旧
  scorecard 宣称为 current。该窄门由
  `scripts/manual_source_fact_scorecard_rescore.py` 和
  `scripts/bind_source_fact_scorecard_rescore.py` 共同执行。
- 先在 `assets/lidousha/authorities/` 放置唯一、candidate-scoped 且已提交/已部署
  验真的 `candidate-source-fact-rescore-authority.v1`。authority 必须 hash-bound 绑定
  original/corrected hook、stale scorecard、人工审定的整份最终 SRT 与 cue 数、
  源录像 identity/绝对区间及 Ivan 权威原话。其 scope 只能允许 scorecard
  rescore；不得附带 provider 执行、runner state 修改或 publication manifest 权限。
- `manual_source_fact_scorecard_rescore.py` 默认只做无 provider 副作用的全量 preflight。
  correction-authority 模式的 `--correction-authority`/hash 与旧
  `--source-fact-receipt`/hash 模式互斥，不得把两条证据链混用。真实调用必须
  同时提供
  `AUTOSLICE_MANUAL_RESCORE_EXECUTION_AUTHORITY=SOURCE_FACT_RESCORE_AUTHORIZED` 与
  `--execute-provider-call`，并严格命中
  `source_fact_rescore_provenance.py` 锁定的 provider/model/transport/timeout/
  calibration contract。它以整份 reviewed SRT 的 `1..N` cues 重评，对输入文件前后
  重验 hash，只用 create-only 输出 `source-fact-rescore-scorecard.v1` receipt；不改
  旧 spec/source-fact receipt/state，也不授权发布。
- binder 必须同时验证旧 spec、rescore receipt 和 committed authority 的精确
  file hash，以及 candidate、corrected hook、stale-card hash、reviewed-SRT hash/
  cue 数和 source identity/区间的一致性。它不在原地修改，而是 create-only 生成
  含 `source_fact_scorecard_rescore_provenance` 的新 spec 与独立 rebind receipt；任一
  输出已存在就拒绝覆盖。后续 producer/record/StoryContract/publish staging 必须
  逐层保留并重算这一 provenance；发现 scorecard 已变但缺少有效 receipt、
  authority 字节漂移或任一嵌入/独立 surface 不一致时必须在生产前拒绝。
