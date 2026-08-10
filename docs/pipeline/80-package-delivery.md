# 80 打包与交付

本文件是打包步骤的**分步权威**。入口：`src/autoslice/producer_package_finalization.py`。

- talk 车道成品强制前置 manifest 在册片头（当前 Z1/Z2 按主片 SHA-256 稳定轮换，fail-closed，`branding_intro.py`，manifest `assets/lidousha/intro/branding_intro.v1.json`）；**歌切不带片头**。验收必须按 record 的 `intro_id` 对照 manifest 的 hash/时长，不得写死任一 variant 的时长。`AUTOSLICE_BRANDING_INTRO=off` 仅测试/应急。
- 片头在最终烧录内拼接，下游 sha256 绑定 with-intro 字节；`.srt`/`.ass` sidecar 保持内容时间轴，偏移记 `burned_preview.branding_intro.intro_offset_ms`。
- 终态跨面校验：`producer_text_finalization.py::verify_chat_authority_final_surfaces`。
  所有 `required=true`、projection-bound 且通过 final-owner verification 的 source truth
  owner，与未被更高权威覆盖的 reviewed baseline mapping，必须在最终 clean SRT 和 speaker
  SRT 的精确投影时间窗逐项存活；`required:false` 只是 best-effort，不能计入 required owner
  或用来制造 owner PASS。discovery `local_windows` 仅用于定位，最终 owner 必须来自有效
  `source-truth-resolved-target-projection.v1`。文字/说话人 ASS 也必须绑定同一最终文本与
  hash；低权威 repair 只有在真实 owner 已通过后才能记为 superseded。projection 的连续
  多 cue 若在最终 hygiene 中被合并/重切，final-owner receipt 以连续窗口并集的 exact payload
  验真并披露 `source-truth-final-recue-coalescence.v1`；非连续窗口、额外邻句或非 exact
  payload 不得走该窄门。
- package auditor 按 resolver 的最终半开区间独立重算 source-truth 分类：完全 inside 的
  required truth 必须逐窗通过 final clean/speaker contract；完全 outside 的 truth 必须具有
  context-only receipt；straddle 或同一 truth 的 mixed inside/outside windows 一律拒绝。
  因此“全部 required truth 都在成片外”的合法包可有
  `required_truth_row_count=0`、`required_window_count=0`，但其
  `context_only_truth_row_count`、ID、逐窗关系和最终 interval 必须完整、可重算，不能靠空计数
  逃过审计。
- current/story-contract 的 talk/recovery item 必须把 `speaker_srt`、`ass_path` 两份真实字节
  连同 `speaker_srt_sha256`、`ass_sha256` 放进 package。两条路径都只能指向 package-relative
  regular file；绝对路径、越界、缺文件以及路径任一层 symlink 都拒绝。song lane 不进入这条
  talk speaker gate。
- 已在一个 host 完整冻结、随后复制到另一 host 的 talk 包，只能用
  `scripts/relocate_slice_package.py` 投影运行时 locator。调用方必须显式给出且物理核对
  source/destination package、candidate evidence root 与 deployed repo root；更具体的 package
  root 映射优先于 candidate root。工具只可改 record/publish/speaker 中明列的 locator 与由此
  必须更新的 standalone hash，禁止改分析、标题、封面、决策或其他 provenance。chat/context
  原始字节必须保持不变；chat 内 speaker hash 绑定迁移前 manifest，journal 显式证明
  before→after speaker 谱系。迁移前后都要重验 record/publish 全镜像、全部已声明 artifact
  hash、regular/non-symlink 路径和 embedded/standalone speaker 图；严格 journal 只接受固定
  document/evidence 集、allowed changed pointers 与
  `chat → context → pre-relocation speaker/publish/record → speaker → publish → record`
  提交顺序，并以 fsync/原子替换向前恢复。三份迁移前 JSON 原字节必须先作为 package 内
  preimage 固化；speaker preimage 还须等于 chat 内绑定的迁移前 manifest hash，恢复或
  `COMMITTED` 重放必须从这些 preimage 重新计算完整投影、changed pointers 与 after hash，
  不信 journal 自报。locator 另按字段绑定物理 root role：媒体/字幕/烧录/封面与 recut audit
  只能在 package，最终 filler audit 只能在 candidate，voiceprint profile 只能在 deployed
  repo；允许 candidate/repo 双来源的 speaker authority 也必须保留 preimage 的原 root，
  同 hash 副本不得跨 root 重绑。candidate evidence root 必须物理等于 package parent。
  禁止手改 JSON 路径、伪造 journal，或为凑审计器改产物名。
- reviewed baseline 执行 exact interval replay 时，必须在 mapping 中 hash-bound 保留每个
  重叠输入 cue 的 replay 前文本、时间和 current cue index。最终权威若撤回较早的
  correction/owner，只有从这些去重后的 pre-replay cue 能重建出旧文本、且 replay 后文本
  精确等于 reviewed baseline 时，才可记为 causal revert/superseded；只看最终 cue 几何、
  baseline 命中或空的 `before` 字段都不能注销既有 owner。
- `review_package_ass_audit.py` 不能只看 ASS 存在或 hash：speaker SRT 还须匹配
  chat-authority 的 `final_speaker_srt_sha256`，ASS 须同时匹配 record
  `artifact_hashes.ass_sha256` 与 chat-authority `speaker_ass_sha256`。auditor 再从包内
  speaker SRT 按生产同一 `_layout_cue_for_display`、speaker ASS escaping 与厘秒 rounding
  重建全部 `Dialogue` events；event 数、start/end、完整文本和 LDS/GUEST style 必须逐项精确
  相等，缺失/非法 Dialogue 或任一投影漂移都阻断。
- Ivan 报告成片字幕问题时，先运行
  `scripts/plan_operator_subtitle_correction.py` 固化修复范围：未明确“问题已列完”的 1–2
  个问题视为抽样，必须整片重跑并复审；3 个及以上问题走
  `TARGETED_REPAIR_PLUS_SYSTEMIC_FIX`，只修所列位置和背后的共享流水线通病，不随机全片重跑。
  明确声明问题穷尽时，即使只有 1–2 个也可走定点修复。计划只决定复查范围，不放宽最终
  package、same-BV、人审或上传门。
- governed late source truth 是唯一允许在 expected-value choke point 之后覆盖词面的 lane，
  并且只接受
  `decision_authority=IVAN_OPERATOR_TRUTH` 的 `VERIFIED_ACTIVE` 行；除可重算的
  glossary expected-value canon 外，CPA/AGY/ASR/词表/structured event 只能作为
  `PROPOSED` 候选证据。package auditor 必须拒绝缺该字段或由
  非 operator authority 自升 active 的新行，防止旧机器 ledger 覆盖已经正确的 CPA 结果。
- 最终 SRT 先过 `lidousha-srt-release-policy.v1`：每个 block 必须被严格解析，连续编号、
  合法且正向的时间、至少 300ms、单调无 overlap、非空/非孤立标点/非单个汉字、媒体边界
  合法。producer、package auditor 与 uploader 各自重跑，不能复用一次自报结果。
- 审计闸是 `scripts/audit_lidousha_review_package.py`，当前输出必须为
  `lidousha-review-package-audit.v2`，policy epoch 必须精确等于
  `2026-07-31.final-artifact-gates.v5`。**schema 仍是 v2，epoch 才是 v5**；不要把仍合法的
  audit schema、`lidousha-cover-route-decision.v2`、`subtitle-redelivery-baseline.v2` 或
  `lidousha-branding-intro.v2` 机械改成 v5。audit 绑定 auditor/策略代码与关键资产的
  `policy_fingerprint`、auditor source hash 以及完整 portable `audited_inputs` 闭包；
  任一文件或政策漂移都使旧 audit 失效。单独一个 `passed: true` JSON 不是证据。
- 视觉排版另由 `review_manifest.json.subtitle_visual_contract` 约束：历史/人工默认 18 字；
  autoslice Sapphire72 显式绑定 2 行/28 字上限。严格 SRT 结构门和视觉行宽门不可互相替代。
- 2026-07-22 起的新包按日期自动进入 StoryContract 严格审计（仍应显式声明 `story_contract_required=true`）、并必须声明 `run_mode` 与 `upload_allowed=false`；producer 的可选布尔值不能关闭新政策。审计器会用 record 中同一 StoryContract 重验最终 SRT、标题、封面文本及实际渲染行、南町专名/关系主张、字幕 hash 与 selection scorecard；封面内嵌的 contract 摘要也必须与 record 一致。任何旧字幕/旧标题/旧封面/旧 policy 字节混入都会把包判为不合规，而不是继续显示为当前成品。
- talk 包还必须携带并重算 `.clip-context.json`；record 的 artifact hash、StoryContract
  `clip_context_binding` 与 sidecar 内容必须三方一致。整片 draft 在 sidecar 内完整保存
  （60,000 字硬上限、禁止截断）；18,000 字 supplemental prompt 必须从 sidecar 重新渲染并与
  StoryContract 逐字相等，并以完整字节送入 boundary/final review；任何 12,000 字兼容切片、
  超预算或 prompt 重渲染漂移都拒发。topic resolution/scoped graph context 也必须留在同一
  digest 内。
- 字幕回归与 reviewed-baseline 是 candidate-scoped 可选权威：producer record 中对应 audit
  与 path 都非空时，恢复 manifest 必须要求包内 sidecar 且逐对象核对；两者都为 null 时必须
  显式投影为 `NOT_CONFIGURED`，不得为了满足打包器而伪造“已人工审阅”基线或空 PASS。
  audit/path 只出现一项、配置过却缺 sidecar，或包内 JSON 与 record 漂移，均 fail-closed。
  边界同理：human source endpoint 必须携带 typed `boundary_end_mode` 并与 boundary audit
  精确一致。`semantic_lower_bound` 只作为下界；`published_recall_anchor` 只把旧公开
  endpoint 作为有界重审中心，允许 CPA 在 15 秒内回剪掉未完成/换题尾巴；`exact_source_pin`
  则要求最终媒体 end 精确等于 pin，不能降级成下界。机器审计必须同时验证两份不同作用域的
  PASS 回执。
  `boundary_audit.boundary_semantic_review` 必须是
  `review_scope=source_full_window`，绑定 resolver 实际消费的完整 source grid、真实 post-end
  witness、source 推荐 end 和 snap 后 source final interval。
  `boundary_audit.final_delivery_boundary_semantic_review` 必须与 StoryContract
  `boundary_semantic_review` 逐字段相等，且为 materialize 后从包内最终 SRT 重跑所得的
  `review_scope=final_delivery`：它绑定 delivery-local grid、唯一最后 closure cue 与
  `[0, 内容时长)` endpoint，并携带 PASS 的 `talk-boundary-source-separation-witness.v1`。
  auditor 必须从包内 SRT 原始字节严格解码、重新解析 cue，并重算 final-delivery grid SHA、
  最后一条 cue 的 ordinal/end/text SHA；不能只检查回执内部三处 grid hash 彼此相等。
  auditor 必须重算该 witness 的 `source_review_sha256`，并核对其中 source request/grid SHA、
  推荐 end、source final interval 与第一层回执完全一致。两层 grid/ordinal/坐标不同，不能要求
  SHA 相等；缺任一层、scope 错、把 source 回执复制成 final、witness 漂移或任一 endpoint
  binding 非 PASS 均拒发。
- source review、resolver、retry 与 boundary audit 还必须逐字段携带并验证同 SHA 的
  `talk-boundary-search-scope.v1`。`semantic_lower_bound` 中，人工下界/结构化 payoff 可移动
  semantic search origin，required owner 只抬 delivery floor，绝对 ceiling 固定为
  `search_origin + repair_cap`。`exact_source_pin` 中 origin/floor/max recommendation 均为
  pin、minimum recommendation 为 `pin-400ms`、forward 为 0；closure 必须在该窗内，最终媒体
  end 必须等于 pin，pin 后 cue 只能作 context witness、不得取得 owner。
  `published_recall_anchor` 中 origin 绑定旧公开 endpoint，但 floor 可向前最多 15 秒；
  required owner/structured payoff 仍可抬高 floor。三种模式的 retry
  source window 都须覆盖 ceiling 后的 witness reserve，但 reserve 不扩大 endpoint cap。
  任一 surface 缺 scope/mode、hash/重算漂移、从推荐 end 二次滚动加 cap、exact 最终 end 不等于
  pin，或 source witness 窗不足都拒发。
- chat authority 的 `frozen-boundary-owner-contract.v1` 与 record boundary audit 必须携带
  完全相同的 candidate-relative required owner 列表、`owner_eligibility_scope`、
  `owner_set_sha256` 与 `contract_sha256`。source truth 必须 `required=true` 且完整落在
  immutable story scope 才可入列；padded lead/post truth 仍修字但不是 owner，straddle
  fail closed。story/chat 必须 applied 且具备对应 typed ownership contract，其中整句
  exact-read 另须 whole-line gate 明示 `owner_eligible=true`，窄
  sender/gift/coreference/entity slot 不借用该整句字段。reviewed baseline 只在最终文字映射
  门验活，不得作为 boundary owner。retry 必须验证首轮 scope/owner token 未漂移。所有 owner
  window 都在最终边界内，
  `required_boundary_owner_verification=PASS` 且
  `delivery_coverage_verification=PASS` 且
  `final_boundary_required_exclusion_count=0`。owner end 若由已审 closure cue 后的固定尾气
  覆盖，audit 必须显式记录 `tail_pad_coverage_bridge=USED`，且
  `maximum_tail_pad_ms` 必须精确等于生产常量 400；仍须证明实际 final end 到达 coverage
  lower bound、bridge 差值不超过 400ms 且未带入下一 cue。裁掉 owner 后把它标成成片外不构成
  通过。
- correction pass 的 `final-review-audit.v1` 不是 package 放行证据。package 必须携带
  `final-review-audit.v2`，其 `reviewed_srt_sha256` 必须由包内最终 SRT 的原始字节重算，CRLF/LF
  等字节差异不得被 `read_text()` 规范化掩盖；discovery 完整、finding 合同合法且为空、
  release gate PASS，并携带 PASS 的 `subtitle-correction-mutation-audit.v1`、上述
  `final_delivery` semantic review、source separation witness 与 delivery-local endpoint
  binding。package auditor 还须把这份 final review 与 boundary audit / StoryContract 精确
  对齐；“第二遍零 finding”不能替代 correction mutation authority，source semantic PASS 也
  不能替代最终交付重审。provider/JSON 失败、null/non-list/all-invalid findings、任何未决项、
  raw-byte hash 漂移、缺失回执或 BLOCK 都阻断。
- correction pass 若为 typed `AUDITOR_UNAVAILABLE`，必须证明输入 SRT/chat audit 原子回滚、
  `findings=[]`、`applied_count=0`，并在 mutation audit 中保持
  `CORRECTION_DISCOVERY_INCOMPLETE`；后续 exact-final 空 finding 不得把它投影为 PASS。该状态
  只允许 runner 按 `provider_transient / final_review_correction_discovery` 有界重试。
- exact-final 声学回执必须包含 `subtitle-audio-timeline-binding.v1`，逐项证明
  delivery-local target/context 经 hash-bound `source_media_timeline_offset_ms` 映射到实际
  source-media target/crop；request hash、verdict、manifest 与缓存身份都必须绑定同一 offset。
  把 recut-local 时间直接裁 padded media、只在日志口头说明偏移、或复用未绑定 offset 的旧
  verdict/cache，均视为错误音频证据并拒发。
- exact-final 的 CPA `NEITHER` 必须退回有界第三候选提案层；只有音频 target/context/offset
  几何完全相同才可签发 `candidate-free-witness-reuse.v1` 复用候选盲 witness。提案层无改字权，
  新候选仍须由第二次 CPA 闭集裁决与 typed mutation receipt 授权。
- 若 exact-final 通过 CPA 授权的同轮自愈修改 SRT，包只认自愈后的最后一次
  `final-review-audit.v2` 与 raw-byte SHA；`exact-final-cpa-self-heal-audit.v1` 必须记录每轮
  before/after SHA、cue ordinal、CPA decision authority、typed mutation receipt 和 timing
  immutable，并在 redelivery baseline 存在时由 baseline audit 记录 post-exact-final SHA。
  每个自愈修复还须登记成新的 final-surface owner；若它修改了同 cue、同精确
  时间窗的早期 correction owner，只有旧文本 SHA 经有序 CPA receipt 链可达最终
  cue SHA，且整份最终 SRT SHA 与 self-heal audit 相等时，才能把旧 owner 记为
  `SUPERSEDED_BY_EXACT_FINAL_CPA`。只是窗口重叠、文本不同或缺任一 typed receipt
  都不能注销旧 owner。exact-final 发生在 boundary owner set 冻结之后，因此新
  final-surface owner 必须带 typed `POST_BOUNDARY_FREEZE_FINAL_SURFACE_OWNER` 排除理由，
  并由 registration ledger 与 self-heal receipt 双重绑定；它不反向改写 frozen boundary
  owner set。中间 FLAGGED 回执不能作为最终放行证据，自愈后未重新
  exact-final、审计链缺字段或 final-surface 冲突未和解均阻断。
- 封面审计按 `cover_generation.route_decision.actual_treatment` 分支验真：所有路线都验
  最终 cover SHA 与 `lidousha-cover-rendered-text-pixels.v3`。包内必须同时有 final cover、
  `.cover.pre-overlay.png`、`.cover.title-mask.png`、`.cover.route-background.png`；auditor
  用 committed trusted font 和 `lidousha-cover-title-render-spec.v1` 重放背景 fit、glyph mask
  与 alpha composite，并要求重组结果逐像素等于最终 PNG。包外绝对路径、同名旁路文件、
  自报 bbox/font/字号都不能补证；关系型
  screenshot_direct 另验 full-frame/no-crop deterministic compositor proof，screenshot_polish
  与 CPA 另要求独立 final-participant verifier。CPA 只认真实 AI 调用与资产 hash；任何共用
  默认字段都不能跨路线充当证据。最终 package audit
  是上传 manifest/hash gate 的前置条件，不允许把“生成过 sidecar”当成合规。可移植交付包若无法访问
  record 中的远端 `final_cover` 路径，只允许回退到 manifest 明示的交付 `cover`，且该文件必须与
  record 的 `final_cover_sha256` 完全一致；不能按相似文件名或任意现存图片替代。
- 汇总表时长必须优先使用 producer 最终 record 打印进 summary 的 `duration_ms`（边界自修复后的内容时长），其次才是 candidate 的 `effective_duration_ms`；原始选片锚点 `end_ms-start_ms` 只作旧状态兜底，不能把已延长的 5:00 成片仍显示成 4:32。
- 汇总中的封面路线必须从校验通过的 `lidousha-cover-route-decision.v2` 投影实际执行路线、是否调用/采用 AI、选中理由和两个未选路线的拒绝理由。内部兼容状态 `AI_COVER_READY` 仅表示封面 artifact 已就绪，绝不能被报告解释成 AI 生图；缺少有效 v2 证据时必须显示 UNKNOWN/缺证。
- `reporting.py` 是从既有 state/record 生成只读审片报告的投影层，不属于会改变选片、字幕、边界、标题、封面或媒体 bytes 的 proof closure；内容与歌切流水线指纹都必须排除它。报告变化直接重写报告，不得唤醒成片重制或无关失败重试。
- 已为 `CURRENT + COMPLIANT` 的历史审片包不会因宽流水线指纹变化被 cron 自动重做。确需全量重出时，只能在新的 `RECOVERY_REVIEW` base 运行 `scripts/plan_recovery_review_rerun.py`：普通 recovery base 要求源 state 字节 SHA-256、全部 CURRENT candidate allowlist、共同旧指纹和当前新指纹完全匹配。对 ordinary daily state 或有效 exact recovery state 中一个已发布候选的单片事故，则必须显式加 `--project-single-published-repair`，只允许一个 `--candidate-id`，禁止 suppression/replacement；历史 exact recovery 源只读兼容明确列出的 v5–v7 plan，plan 必须与 selection contract 完全一致并包含目标，投影出的新目标始终使用当前 v7。planner 把其他 active row 记录为 `SOURCE_STATE_UNCHANGED_OUTSIDE_REPAIR_TARGET` 后投影出隔离的 exact base，不得把未重跑的其他候选伪装成用户拒绝。若隔离 base 不复制多 GiB 的原录像，可同时用 `--target-recordings-root` 绑定现有 regular recording tree；该 root 中可见的日期目录必须恰好只有目标 date（可让该单个日期目录指向 canonical date），receipt 会冻结该路径，后续 runner 必须以同一 `AUTOSLICE_REC_ROOT` 运行。两种模式都要求 source/target 无 `AUTO_UPLOAD`；若 source 本身是 recovery base，其 `cpa.env` 可以是既有的外部权威 symlink，但 planner 必须先解析并验证最终目标是 regular file，再让 target 直接绑定该解析后的权威，禁止复制凭据或接受悬空/非普通文件目标；旧目标 record 完整降为 `SUPERSEDED + STALE_PIPELINE`，新项以 `selected_repair` 入队，随后仍由正常 runner 生成 CURRENT 成品。禁止把旧 `review_ready` 手改成 failed，也禁止在旧 base 原地覆盖。
- `delivery_rerun_plan.schema_version` 必须精确为
  `recovery-review-talk-rerun-plan.v7`；v6 及以下只作历史证据，不可执行。planner 必须以
  `registry_repo_path + registry_sha256` 绑定
  `assets/lidousha/recovery_publication_authority.v1.json`，其 registry entry 集合须与 exact
  queue 完全相等。每条 entry 同时冻结 `required_given_end_ms` 与 `boundary_end_mode`；planner
  从 registry 派生全量 end map、typed mode 和 authority，不接受操作员另输一套 endpoint。
  plan、pending item、spec、record、boundary audit 与 manifest 必须逐项保持相同 mode/ms。
  少/多 candidate、少/错 end、mode 或 authority 漂移都在 supersede 或产片前拒绝。后续自然
  fingerprint requeue 也只接受同一 v7 plan，并保持完整 publication authority。
- recovery plan 同时写入 exact-no-backfill selection contract；本地审片包只能在
  `exact-talk-contract-closure.v1.status=COMPLETE` 后逐 stem 重建。每个 contract ID 必须
  恰有一个 `rc=0 + CURRENT + COMPLIANT`，且无 pending、missing、failure、重复/冲突或
  outside-contract attempt。否则 runner 与报告保持 `recovery_incomplete`，不得覆盖旧本地包
  或沿用 `review_ready`。
- exact talk recovery 是 talk-only transaction：不得恢复、重排、补位或生产任何 song
  delivery。投影器必须清空 `pending_song`、`song_backlog` 与
  `song_selection_backlog`；runner 还须在 discovery、prioritize 与 song lane 边界重复
  fail-closed，并把发现的陈旧歌队列记为
  `exact-talk-recovery-song-scope-suppression.v1` 后清空。历史 `songs`/
  `song_superseded_attempts` 是证据，不得在普通执行时抹除；它们也不构成 exact talk 的工作量。
- state 的最终 status 必须来自 `batch_terminal_state.py` 的一次精确投影；future retry、
  部分 delivery 或报告层旧状态都不能盖过 incomplete exact closure。只有 closure COMPLETE
  才能投影 `review_ready` 并进入本地覆盖。
- exact recovery 重跑结束后必须用 `scripts/build_lidousha_recovery_review_manifest.py` 从最终 state 与 record **整份重建** `review_manifest.json`，禁止复用/手补上一轮清单。审计器必须比较 manifest item 与 record 的 candidate/title。`cover_route_attestations` 必须存在，candidate 集合须与 exact candidate 集合完全相等，并逐项重验 reference/final hash、method、完整 route decision 与 reference authority；缺失、额外、重复、旧标题、旧封面 hash 或旧路由证据漂移都要阻断上传。
- recovery 成品在 cover-only repair 后重建 manifest 时，builder 必须从当前 record 的
  `cover_generation` 逐项验真并刷新包根的 `.cover.pre-overlay.png`、
  `.cover.title-mask.png`、`.cover.route-background.png` 可移植副本。只允许使用当前 generation
  明示且 hash 匹配的 regular source；缺 source、symlink、hash 漂移继续 fail-closed，不能让上一版
  封面的回放附件阻断已绑定的新封面，也不能沿用旧附件假装通过。
- 重建的 recovery `review_manifest.json` 固定保持
  `status=finished_review_package_no_upload_pending_human_review` 与
  `upload_allowed=false`。current package audit 只证明机器可确定的结构、hash、投影与政策闭包；
  它不能证明人已完整播放最终烧录 MP4、逐句对齐音频/静音、确认结尾闭合或看过最终封面。
  因此 audit `passed=true`、state `review_ready`、本地包覆盖或该 pending-human manifest
  都不能转写为人工通过，更不能自行改成发布许可。
- exact same-BV repair 在生成 authorized manifest 前还必须有对**当前最终字节**的
  `lidousha-final-human-review.v2`；它不是 package auditor 的产物，也不改变 pending-human
  manifest 或 `upload_allowed=false`。receipt 必须绑定并重验 create-only 提交的
  `lidousha-final-human-review-evidence.v2` 路径、SHA-256 与字节数；reviewer 身份、exact
  points、八项检查、封面 claims、evidence v2、create-only builder 与全部漂移/权限规则只读
  [90-publish.md](90-publish.md)；80 步只负责保证 review manifest、audit、record 和三类最终
  artifact 已冻结且可供该复核逐字节绑定。
- committed `subtitle_review_points` 使用最终视频时间轴，任何窗口不得越过 record 中片头
  verification 绑定的真实 EOS（只容许与 canonical receipt 相同的 500ms 尾端取整余量）。
  evidence template 必须在创建时先做这项检查；禁止先生成一个不可能通过的模板，再把越界
  拖到 final-human receipt 阶段当成人工 blocker。尾部闭环窗口应在契约资产中明确收束到
  实际 EOS 内，不能依赖播放器播放不存在的媒体。
- final-human cover claim 必须来自当前 record 中与最终封面 SHA-256 互相绑定的
  `cover_generation.rendered_lines + rendered_text_pixels`；多人物源帧的
  `source_visible_claims` 只证明实际像素中的人物与表情。cover reference 的
  `narrative_presentation` 是创作指导，允许封面在完整故事原子中择取清晰主副标题，绝不能
  被收据直接升级为“最终 PNG 上实际显示了这些字”。缺当前 rendered-text 绑定必须拒发；
  只有在该字段上线前已冻结的历史 receipt 才按原 narrative claim 只读兼容。
- exact same-BV recovery 的包内必须额外携带 `.publish.json` regular file，并以 record
  `artifact_hashes.publish_draft_sha256` 绑定。state rerun plan 的
  `recovery_publication_authorities_by_candidate` 必须与 exact candidate 集合完全相等；
  record 顶层、record `publish_staging`、publish draft、manifest item 与 manifest 顶层 map
  必须携带同一个 `recovery-same-bv-publication-authority.v1`。auditor 重读受管部署的
  publication registry asset、复算 registry/authority SHA，并逐字比较 candidate、最终标题、
  BVID/AID/CID 与标题模式。缺任一候选、任一 surface、只有裸标题相等、publish hash 漂移或
  map 集合不等都拒发。
- exact-no-backfill 合同中的入选项失败时必须保留真实终态（`failed`、`boundary_unrepairable`、
  `speaker_review_required` 或 `speaker_evidence_insufficient`），并记录“合同禁止补位”；不得把它改写成代表可由候补替换的
  `candidate_rejected`。这样相关 failure-scoped fingerprint 变化后仍可自动重试。对于此规则上线前
  已被误标的记录，只允许在同一有效 exact contract 内、且保留上述 `rejected_status` 时迁移重试；
  普通 production 的 `candidate_rejected` 仍是终态，不能借此复活。
- authorized uploader 在任何副作用前重跑**当前** canonical package auditor、严格 SRT 与共享
  标题门，并要求重跑结果与 manifest 绑定的 v2 audit 完全一致；它不信任旧 audit 自报。
- **外部主机（wsl/Mac）产出的包只能经 `scripts/import_external_package.py` 进入交付链。**
  它们用同一份 repo、同一套 produce，包是完整的；卡的是跨主机导入——凭据、
  `publication_registry` 和 upload 读的 state 只在 free。该工具按序做：源包定位符契约校验
  → 逐文件 sha256 前后比对的字节搬运 → `slice-package-relocation.v2` 事务化路径规整 →
  持 `runner.lock` 的 state 绑定 → manifest → package audit → 标题+封面联合质检，
  typed 回执落 `<pkg>/<cid>.external-import-receipt.json`；不带 `--apply` 为 dry-run。
  用法：`python3 scripts/import_external_package.py --source <外部包的
  replacement_recuts 目录> --date <date> --candidate <cid> [--allow-new-pick] --apply`
  （跨主机传输不在工具内，先 rsync/scp 到 free 的暂存目录；暂存目录与目标目录必须不同）。
  硬边界：只走 talk 车道；只接受 pick 行缺失（需 `--allow-new-pick`）或已是
  `review_ready`+`rc=0` 的重绑，`candidate_rejected`/`failed` 必须先过
  `scripts/revive_rejected_candidates.py`；批级状态不在 manifest builder 白名单内时直接
  typed 拒绝而不修状态；**不做 `authorized_upload make-manifest`**，上传授权仍只走
  [90-publish.md](90-publish.md)。路径投影只动
  `package_relocation_contract.py` 白名单里的运行期定位符，冻结证据（`story_contract`、
  `cover_generation`、`boundary_audit`、`analysis` 等）逐字节保留产出主机的值；改写后的
  publish/speaker 哈希由 record 的 `artifact_hashes.publish_draft_sha256` 与
  `speaker_finalization_manifest_sha256` 重新绑定，chat-authority 里产出主机的
  `speaker_manifest_sha256` 不改写，只在事务日志记 `speaker_manifest_lineage`。
- 新 BV 与 exact same-BV repair 的两条发布 lane、权限边界、正式 receipt schema、live
  验收和执行顺序只读 [90-publish.md](90-publish.md)。打包步骤不得复制、放宽或自行推导发布
  准入，也不得把 package audit、pending-human manifest 或任意旧版/手写 receipt 当成授权。
- tag 按成品字幕重算（`upload_tag_policy.py` + `scripts/suggest_upload_tags.py`）。profile
  `upload-tag-policy.v2` 固定 4 个 base 位与最多 6 个 dynamic 位，总上限 10；专名（含
  `important_content_ips` 白名单中的高显著 IP/节目名）只由确定性 owner 从标题/最终 SRT 命中，
  trigger 标点/别名只作表面，输出 canonical 正主名。LLM 仍只提通用内容词，不能发明或重复
  专名；最终 6 个 dynamic 位按人工补充 → 确定性专名/IP → LLM 内容词的既有优先级竞争。
